"""Граммы -> ккал и БЖУ. Порядок поиска: личный справочник -> USDA (офлайн) -> Open Food Facts."""
import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from storage import Storage

OFF_URL = "https://world.openfoodfacts.org/cgi/search.pl"
OFF_HEADERS = {"User-Agent": "eat-burn/0.1 (personal calorie bot)"}

# совпадение из базы, которое расходится с оценкой модели больше чем в MAX_RATIO раз, — мимо
# («авокадо» -> «Oil, avocado», «борщ» -> сухая смесь)
MAX_RATIO = 2.5
# слова, из-за которых запись USDA — скорее всего не то, что имелось в виду (если их нет в запросе)
PENALTY_WORDS = {
    "oil", "dressing", "breaded", "battered", "dried", "dehydrated", "powder", "mix", "sauce",
    "chips", "babyfood", "infant", "fast", "restaurant", "frozen", "canned", "candied", "juice",
    "flour", "fried", "sweetened", "imitation", "substitute", "with", "concentrate", "syrup",
    "unprepared", "dry",
}
# в Open Food Facts по «шаурма» первым находится соус, по «борщ» — сухая смесь
OFF_SKIP = ("соус", "смесь", "приправ", "концентрат", "сух", "для приготовления", "заправк", "кетчуп", "майонез")


@dataclass
class Per100:
    kcal: float
    protein: float | None = None
    fat: float | None = None
    carbs: float | None = None
    match: str = ""  # откуда взяли: «мой справочник», «USDA: Bananas, raw»…


@dataclass
class Item:
    name: str          # по-русски, как показываем
    name_en: str
    grams: float
    per100: Per100 | None = None
    guess: float | None = None  # грубая оценка ккал/100 г от модели — для проверки и как запасной вариант

    def part(self, attr: str) -> float | None:
        if not self.per100:
            return None
        v = getattr(self.per100, attr)
        return None if v is None else v * self.grams / 100

    @property
    def kcal(self) -> float:
        return self.part("kcal") or 0.0

    def as_row(self) -> dict:
        return {"name": self.name, "grams": self.grams, "kcal": self.kcal,
                "protein": self.part("protein"), "fat": self.part("fat"),
                "carbs": self.part("carbs"), "match": self.per100.match if self.per100 else None}


@dataclass
class Draft:
    items: list[Item] = field(default_factory=list)
    source: str = "text"

    def missing(self) -> list[Item]:
        return [i for i in self.items if i.per100 is None]

    def total(self, attr: str = "kcal") -> float:
        return sum(i.part(attr) or 0 for i in self.items)


class Nutrition:
    def __init__(self, storage: Storage, usda_path: str | Path | None, use_off: bool = True):
        self.storage = storage
        self.usda = None
        if usda_path and Path(usda_path).exists():
            self.usda = sqlite3.connect(usda_path, check_same_thread=False)
        self.use_off = use_off

    def lookup(self, name_ru: str, name_en: str, guess: float | None = None) -> Per100 | None:
        row = self.storage.get_food(name_ru)
        if row:
            return Per100(row["kcal"], row["protein"], row["fat"], row["carbs"], "мой справочник")
        # укороченный запрос к USDA («cottage cheese pancakes» -> «cottage cheese») — уже другой продукт,
        # поэтому сначала полный запрос, потом Open Food Facts по русскому названию, и только потом укороченный
        found = (self._usda(name_en, guess, exact=True)
                 or (self._off(name_ru, guess) if self.use_off else None)
                 or self._usda(name_en, guess, exact=False))
        if found is None and guess:
            found = Per100(guess, match="≈ оценка модели")
        return found

    def fill(self, draft: Draft) -> Draft:
        for item in draft.items:
            if item.per100 is None:
                item.per100 = self.lookup(item.name, item.name_en, item.guess)
        return draft

    def _usda(self, name_en: str, guess: float | None = None, exact: bool = False) -> Per100 | None:
        if not self.usda:
            return None
        # обрывки вроде «l» из «l-carnitine» находят что угодно на «l» — выкидываем
        tokens = [t for t in re.findall(r"[a-z]+", name_en.lower()) if len(t) >= 3]
        if not tokens:
            return None
        # сначала все слова, потом отбрасываем уточнения с конца: «buckwheat groats boiled» -> «buckwheat groats»
        for n in ([len(tokens)] if exact else range(len(tokens) - 1, 0, -1)):
            q = " ".join(f'"{t}"*' for t in tokens[:n])
            rows = self.usda.execute(
                """SELECT f.description, f.kcal, f.protein, f.fat, f.carbs, bm25(foods_fts)
                   FROM foods_fts JOIN foods f ON f.id = foods_fts.rowid
                   WHERE foods_fts MATCH ? ORDER BY bm25(foods_fts) LIMIT 50""", (q,)).fetchall()
            scored = [(usda_score(r[0], r[5], tokens, r[1], guess), r) for r in rows if plausible(r[1], guess)]
            if scored:
                r = min(scored)[1]
                return Per100(r[1], r[2], r[3], r[4], f"USDA: {r[0]}")
        return None

    def _off(self, name: str, guess: float | None = None) -> Per100 | None:
        try:
            resp = httpx.get(OFF_URL, headers=OFF_HEADERS, timeout=8, params={
                "search_terms": name, "search_simple": 1, "action": "process", "json": 1,
                "page_size": 10, "fields": "product_name,nutriments"})
            products = resp.json().get("products", [])
        except (httpx.HTTPError, ValueError):
            return None
        for p in products:
            n = p.get("nutriments") or {}
            kcal = n.get("energy-kcal_100g")
            pname = str(p.get("product_name", "")).lower()
            if any(w in pname and w not in name.lower() for w in OFF_SKIP):
                continue
            if isinstance(kcal, (int, float)) and kcal > 0 and plausible(kcal, guess):
                return Per100(kcal, n.get("proteins_100g"), n.get("fat_100g"),
                              n.get("carbohydrates_100g"), f"OFF: {p.get('product_name', name)}")
        return None


def plausible(kcal: float, guess: float | None) -> bool:
    if not guess or kcal <= 0:
        return kcal >= 0
    return abs(math.log(kcal / guess)) <= math.log(MAX_RATIO)


def usda_score(desc: str, bm25: float, tokens: list[str], kcal: float, guess: float | None) -> float:
    """Меньше — лучше. Главное слово первым, без лишней обработки, коротко и близко к оценке модели."""
    d = desc.lower()
    words = set(re.findall(r"[a-z]+", d))
    first = d.split(",")[0]
    score = bm25 * 0.3 + len(d) / 60
    if tokens and tokens[0] in first:
        score -= 3
    score += 2 * len({w for w in words & PENALTY_WORDS if not any(w.startswith(t) for t in tokens)})
    if re.search(r"\b[A-Z]{3,}\b", desc):  # бренды: QUAKER, KRAFT…
        score += 2
    if guess and kcal > 0:
        score += 2 * abs(math.log(kcal / guess))
    return score
