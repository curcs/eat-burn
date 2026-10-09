"""Скриншоты готовых рационов (BeFit и похожие) -> блюда с ккал и БЖУ. Локально, через tesseract.

Два формата:
- карточка блюда: строка «197 24.3 5.9 11.6 150» над подписями «Ккал Б Ж У Гр», ниже название;
- список заказов: в каждой карточке название, ниже «1-й завтрак 248 Ккал», карточки в две колонки.
"""
import io
import re
import subprocess
from dataclasses import dataclass

from PIL import Image

MEAL = r"(?:\d-?й\s+)?(?:завтрак|обед|ужин|полдник|перекус)"
_LABELS = re.compile(r"ккал.*\bб\b.*\bж\b.*\bу\b", re.I)
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_LIST_KCAL = re.compile(r"^(?P<meal>.*?)\s*(?P<kcal>\d{2,4})\s*[кk]кал\s*$", re.I)
_TEXTY = re.compile(r"^[А-Яа-яЁё][А-Яа-яЁё\s,\-«»\".]*(?:\.\.\.|…)?$")


@dataclass
class Dish:
    name: str
    kcal: float
    meal: str = ""
    protein: float | None = None
    fat: float | None = None
    carbs: float | None = None
    grams: float | None = None

    def consistent(self) -> bool:
        """Ккал сходятся с БЖУ (4·Б + 9·Ж + 4·У)? Иначе OCR, скорее всего, ошибся в цифре."""
        if None in (self.protein, self.fat, self.carbs):
            return False
        calc = 4 * self.protein + 9 * self.fat + 4 * self.carbs
        return abs(calc - self.kcal) <= max(15, 0.12 * self.kcal)


@dataclass
class Screen:
    kind: str  # "card" или "list"
    dishes: list[Dish]


def ocr(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    r = subprocess.run(["tesseract", "-", "-", "-l", "rus+eng", "--psm", "6"],
                       input=buf.getvalue(), capture_output=True, timeout=60)
    return r.stdout.decode("utf-8", "ignore")


def read_screenshot(image: bytes) -> Screen | None:
    """None — это не скрин рациона (наверное, фото тарелки)."""
    img = Image.open(io.BytesIO(image)).convert("L")
    card = parse_card(ocr(img))
    if card:
        return Screen("card", [card])
    # бледный серый текст и две колонки: увеличиваем, повышаем контраст, читаем колонки по отдельности
    w, h = img.size
    dishes = []
    for box in ((0, 0, w // 2, h), (w // 2, 0, w, h)):
        col = img.crop(box).resize(((box[2] - box[0]) * 3, h * 3), Image.LANCZOS)
        dishes += parse_list(ocr(col.point(lambda p: 0 if p < 215 else 255)))
    return Screen("list", dishes) if dishes else None


def _clean(name: str) -> str:
    return " ".join(name.replace("…", "...").split()).strip(" ,").lower()


def parse_card(text: str) -> Dish | None:
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        if not _LABELS.search(line):
            continue
        nums_line = next((l for l in reversed(lines[:i]) if l), "")
        nums = [float(x.replace(",", ".")) for x in _NUM.findall(nums_line)]
        if len(nums) < 4:
            return None
        name_lines = []
        for l in lines[i + 1:]:
            if not l:
                if name_lines:
                    break
                continue
            if name_lines and l.count(",") >= 2:  # «творог, молоко, яблоки…» — это уже состав
                break
            name_lines.append(l)
        meal = next((m.group(0) for l in lines[:i] if (m := re.search(MEAL, l, re.I))), "")
        return Dish(_clean(" ".join(name_lines)), nums[0], meal.lower(), nums[1], nums[2], nums[3],
                    nums[4] if len(nums) > 4 else None)
    return None


def parse_list(text: str) -> list[Dish]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    dishes = []
    for i, line in enumerate(lines):
        m = _LIST_KCAL.match(line)
        if not m or not re.fullmatch(MEAL, m.group("meal").strip(), re.I):
            continue
        name_lines = []
        for l in reversed(lines[:i]):  # название — текстовые строки прямо над «завтрак 248 ккал»
            if not _TEXTY.match(l) or len(l) < 3 or len(name_lines) == 4:
                break
            name_lines.insert(0, l)
        if name_lines:
            dishes.append(Dish(_clean(" ".join(name_lines)), float(m.group("kcal")), m.group("meal").strip().lower()))
    return dishes
