"""Продукты в упаковке: штрихкод -> Open Food Facts, или фото этикетки «пищевая ценность» -> OCR.
Всё на 100 г; сколько съела, спрашиваем отдельно."""
import io
import re
from dataclasses import dataclass

import httpx
import zxingcpp
from PIL import Image, ImageOps

OFF_PRODUCT = "https://world.openfoodfacts.org/api/v2/product/{}.json"
HEADERS = {"User-Agent": "eat-burn/0.1 (personal calorie bot)"}


@dataclass
class Product:
    name: str
    kcal: float                    # на 100 г / 100 мл
    protein: float | None = None
    fat: float | None = None
    carbs: float | None = None
    package_g: float | None = None  # вся упаковка, если известно
    source: str = ""                # «штрихкод 4860019003043» / «этикетка»

    def portion(self, grams: float) -> dict:
        k = grams / 100
        part = lambda v: None if v is None else round(v * k, 1)
        return {"kcal": round(self.kcal * k), "protein": part(self.protein), "fat": part(self.fat),
                "carbs": part(self.carbs)}


RETAIL = zxingcpp.BarcodeFormat.EAN13 | zxingcpp.BarcodeFormat.EAN8 | zxingcpp.BarcodeFormat.UPCA | zxingcpp.BarcodeFormat.UPCE


def _variants(img: Image.Image):
    """Telegram ужимает фото до ~1280 px, и мелкий штрихкод в кадре теряет штрихи. Пробуем по очереди:
    как есть, контрастный ч/б, увеличенный, а потом куски кадра с увеличением (штрихкод где-то в одном из них)."""
    gray = ImageOps.autocontrast(img.convert("L"))
    yield img
    yield gray
    yield gray.resize((gray.width * 2, gray.height * 2), Image.LANCZOS)
    for n in (2, 3):
        tw, th = int(gray.width / n * 1.5), int(gray.height / n * 1.5)  # с перекрытием, чтобы не разрезать код
        for i in range(n):
            for j in range(n):
                x = min(int(i * gray.width / n), gray.width - tw) if tw < gray.width else 0
                y = min(int(j * gray.height / n), gray.height - th) if th < gray.height else 0
                tile = gray.crop((x, y, x + tw, y + th))
                yield tile.resize((tile.width * 3, tile.height * 3), Image.LANCZOS)


def decode_barcode(image: bytes) -> str | None:
    img = Image.open(io.BytesIO(image)).convert("RGB")
    for variant in _variants(img):
        for binarizer in (zxingcpp.Binarizer.LocalAverage, zxingcpp.Binarizer.GlobalHistogram):
            for r in zxingcpp.read_barcodes(variant, formats=RETAIL, binarizer=binarizer):
                if r.text.isdigit() and r.valid if hasattr(r, "valid") else r.text.isdigit():
                    return r.text
    return None


def parse_quantity(q: str | None) -> float | None:
    """«330 ml», «0,33 л», «50 г», «1 kg» -> граммы (мл считаем граммами)."""
    if not q:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|кг|l|л|ml|мл|g|г)\b", q.lower())
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return v * 1000 if m.group(2) in ("kg", "кг", "l", "л") else v


def off_product(code: str) -> Product | None:
    try:
        r = httpx.get(OFF_PRODUCT.format(code), headers=HEADERS, timeout=10, params={
            "fields": "product_name,product_name_ru,brands,quantity,nutriments"})
        p = r.json().get("product") or {}
    except (httpx.HTTPError, ValueError):
        return None
    n = p.get("nutriments") or {}
    kcal = n.get("energy-kcal_100g")
    if not isinstance(kcal, (int, float)):
        return None
    name = p.get("product_name_ru") or p.get("product_name") or f"продукт {code}"
    brand = (p.get("brands") or "").split(",")[0].strip()
    if brand and brand.lower() not in name.lower():
        name = f"{name} {brand}"
    return Product(" ".join(name.lower().replace("*", "").split()), float(kcal), n.get("proteins_100g"),
                   n.get("fat_100g"), n.get("carbohydrates_100g"), parse_quantity(p.get("quantity")),
                   f"штрихкод {code}")


# «белки 3,2 г», «жиры - 0», «углеводы: 10,2 г», «энергетическая ценность 45 ккал / 190 кДж»
_NUM = r"[:\-—–\s]*(\d+(?:[.,]\d+)?)"


def parse_label(text: str) -> Product | None:
    t = text.lower().replace("ё", "е")
    if not all(w in t for w in ("белк", "жир", "углевод")):
        return None
    def grab(word: str) -> float | None:
        m = re.search(word + r"[а-я]*" + _NUM, t)
        return float(m.group(1).replace(",", ".")) if m else None
    p, f, c = grab("белк"), grab("жир"), grab("углевод")
    kcal = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ккал|kcal)", t)
    kcal = float(kcal.group(1).replace(",", ".")) if kcal else None
    if None not in (p, f, c):
        calc = 4 * p + 9 * f + 4 * c
        if kcal is None or abs(kcal - calc) > max(15, 0.2 * calc):  # ккал не прочитались — считаем из БЖУ
            kcal = round(calc)
    if kcal is None:
        return None
    return Product("", kcal, p, f, c, source="этикетка")
