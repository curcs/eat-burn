import io
import sys
from pathlib import Path

import numpy as np
import zxingcpp
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from products import Product, decode_barcode, parse_label, parse_quantity


def test_barcode_from_photo():
    img = zxingcpp.write_barcode(zxingcpp.BarcodeFormat.EAN13, "4860019003043", width=400, height=150)
    buf = io.BytesIO()
    Image.fromarray(np.array(img)).save(buf, "PNG")
    assert decode_barcode(buf.getvalue()) == "4860019003043"
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), "white").save(buf, "PNG")
    assert decode_barcode(buf.getvalue()) is None


def test_quantity():
    assert parse_quantity("330 ml") == 330
    assert parse_quantity("0,33 л") == 330
    assert parse_quantity("50 г") == 50
    assert parse_quantity("1 kg") == 1000
    assert parse_quantity(None) is None and parse_quantity("6 x") is None


def test_label():
    p = parse_label("Пищевая ценность на 100 г: белки 4,5 г, жиры 22 г, углеводы 60,1 г. "
                    "Энергетическая ценность 456 ккал / 1908 кДж")
    assert (p.kcal, p.protein, p.fat, p.carbs) == (456, 4.5, 22, 60.1)
    # ккал не прочитались — считаем из БЖУ
    p = parse_label("белки - 0 г; жиры - 0 г; углеводы - 10,2 г")
    assert p.kcal == 41
    assert parse_label("Живой творог с запеченными фруктами") is None


def test_portion():
    p = Product("лимонад", 44, 0, 0, 10.2, 330)
    assert p.portion(330) == {"kcal": 145, "protein": 0, "fat": 0, "carbs": 33.7}
