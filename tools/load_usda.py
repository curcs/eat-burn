"""Скачивает USDA FoodData Central (SR Legacy + Foundation) и собирает data/usda.db.

    ./venv/bin/python tools/load_usda.py

Один раз, ~10 МБ архивов. Дальше поиск продуктов работает офлайн.
"""
import csv
import io
import sqlite3
import sys
import zipfile
from pathlib import Path

import httpx

DATASETS = [
    "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_sr_legacy_food_csv_2018-04.zip",
    "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_foundation_food_csv_2025-04-24.zip",
]
# id нутриентов: энергия (ккал, у Foundation часто только Atwater 2047/2048), белок, жир, углеводы
KCAL_IDS = ("1008", "2048", "2047")
MACRO_IDS = {"1003": "protein", "1004": "fat", "1005": "carbs"}

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "usda.db"


def read_csv(zf: zipfile.ZipFile, name: str):
    path = next(n for n in zf.namelist() if n.endswith("/" + name) or n == name)
    with zf.open(path) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))


def load(zf: zipfile.ZipFile, rows: dict):
    foods = {r["fdc_id"]: r["description"] for r in read_csv(zf, "food.csv")
             if r["data_type"] in ("sr_legacy_food", "foundation_food")}
    nutr = {}
    for r in read_csv(zf, "food_nutrient.csv"):
        if r["fdc_id"] in foods and (r["nutrient_id"] in KCAL_IDS or r["nutrient_id"] in MACRO_IDS):
            try:
                nutr.setdefault(r["fdc_id"], {})[r["nutrient_id"]] = float(r["amount"])
            except ValueError:
                pass
    for fid, desc in foods.items():
        n = nutr.get(fid, {})
        kcal = next((n[k] for k in KCAL_IDS if k in n), None)
        if kcal is None:
            continue
        rows[desc.lower()] = (desc, kcal, n.get("1003"), n.get("1004"), n.get("1005"))


def main():
    OUT.parent.mkdir(exist_ok=True)
    rows: dict = {}
    for url in DATASETS:
        print("скачиваю", url.rsplit("/", 1)[-1])
        data = httpx.get(url, follow_redirects=True, timeout=120).content
        load(zipfile.ZipFile(io.BytesIO(data)), rows)
    OUT.unlink(missing_ok=True)
    db = sqlite3.connect(OUT)
    db.executescript("""
        CREATE TABLE foods (id INTEGER PRIMARY KEY, description TEXT, kcal REAL,
                            protein REAL, fat REAL, carbs REAL);
        CREATE VIRTUAL TABLE foods_fts USING fts5(description, content='foods', content_rowid='id');
    """)
    db.executemany("INSERT INTO foods (description, kcal, protein, fat, carbs) VALUES (?,?,?,?,?)",
                   rows.values())
    db.execute("INSERT INTO foods_fts(foods_fts) VALUES ('rebuild')")
    db.commit()
    print(f"готово: {len(rows)} продуктов -> {OUT}")


if __name__ == "__main__":
    sys.exit(main())
