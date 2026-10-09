import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import profile_calc
import report
from llm import to_draft
from nutrition import Draft, Item, Nutrition, Per100
from parser import parse_edit_lines, parse_quick
from storage import Storage


# --- профиль ---

def test_target_for_owner():
    assert round(profile_calc.bmr(60, 165, 30)) == 1320
    assert profile_calc.target(60, 165, 30) == 1350


# --- разбор простых записей ---

@pytest.mark.parametrize("text,kind,kcal,desc", [
    ("450 рацион обед", "quick", 450, "рацион обед"),
    ("+300 ужин", "quick", 300, "ужин"),
    ("1 100 рацион", "quick", 1100, "рацион"),
    ("-200 бег", "workout", 200, "бег"),
    ("- 350 ккал силовая", "workout", 350, "силовая"),
    ("520", "quick", 520, "еда"),
])
def test_quick(text, kind, kcal, desc):
    q = parse_quick(text)
    assert (q.kind, q.kcal, q.desc) == (kind, kcal, desc)


@pytest.mark.parametrize("text", ["100г творога", "150 г курицы", "овсянка 60г", "3 шт печенья", "1 л кефира"])
def test_not_quick(text):
    assert parse_quick(text) is None


@pytest.mark.parametrize("text,kcal,desc", [("2 яйца", 2, "яйца"), ("4 л-карнитин сироп", 4, "л-карнитин сироп")])
def test_ambiguous_small_number(text, kcal, desc):
    q = parse_quick(text)
    assert (q.kind, q.kcal, q.desc) == ("ambiguous", kcal, desc)


def test_edit_lines():
    assert parse_edit_lines("банан 150, -мёд\nсыр 30г") == [
        ("банан", 150, False), ("мёд", None, True), ("сыр", 30, False)]


# --- поиск по базам ---

@pytest.fixture
def st(tmp_path):
    return Storage(str(tmp_path / "t.db"))


@pytest.fixture
def usda(tmp_path):
    p = tmp_path / "usda.db"
    db = sqlite3.connect(p)
    db.executescript("""
        CREATE TABLE foods (id INTEGER PRIMARY KEY, description TEXT, kcal REAL, protein REAL, fat REAL, carbs REAL);
        CREATE VIRTUAL TABLE foods_fts USING fts5(description, content='foods', content_rowid='id');
        INSERT INTO foods VALUES (1, 'Bananas, raw', 89, 1.1, 0.3, 22.8);
        INSERT INTO foods VALUES (2, 'Banana chips', 519, 2.3, 33.6, 58.4);
        INSERT INTO foods VALUES (3, 'Buckwheat groats, roasted, cooked', 92, 3.4, 0.6, 19.9);
        INSERT INTO foods_fts(foods_fts) VALUES ('rebuild');
    """)
    db.commit()
    return p


def test_lookup_order(st, usda):
    nu = Nutrition(st, usda, use_off=False)
    assert nu.lookup("банан", "banana raw").match == "USDA: Bananas, raw"
    # уточнение, которого нет в базе, отбрасывается
    assert nu.lookup("гречка", "buckwheat boiled").kcal == 92
    st.put_food("Банан", 95)
    assert nu.lookup("банан", "banana raw").match == "мой справочник"
    assert nu.lookup("чак-чак", "chak chak") is None
    assert nu.lookup("л-карнитин", "l carnitine syrup") is None  # не «Bananas» по обрывку «l»…
    assert nu.lookup("л-карнитин", "l carnitine syrup", guess=15).match == "≈ оценка модели"


def test_draft_totals(st, usda):
    d = Nutrition(st, usda, use_off=False).fill(Draft([Item("банан", "banana raw", 120), Item("чак-чак", "x", 50)]))
    assert round(d.total()) == 107
    assert [i.name for i in d.missing()] == ["чак-чак"]


def test_llm_json():
    d = to_draft('{"items":[{"name_ru":"Овсянка","name_en":"oats dry","grams":60},'
                 '{"name_ru":"","name_en":"x","grams":10}]}', "text")
    assert [(i.name, i.grams) for i in d.items] == [("овсянка", 60)]
    with pytest.raises(ValueError):
        to_draft('{"items":[]}', "text")
    d = to_draft('{"items":[{"name_ru":"сироп","name_en":"syrup","grams":25,"kcal_100g":30,"user_kcal_100g":14}]}', "text")
    assert d.items[0].per100.kcal == 14 and round(d.items[0].kcal, 1) == 3.5


# --- итоги и xlsx ---

def test_day_remaining_with_workout(st, tmp_path):
    day = date(2026, 10, 6)
    at = datetime(2026, 10, 6, 13, 0)
    st.add_entry("quick", "рацион", 1100, "manual", when=at)
    st.add_entry("workout", "бег", 200, "manual", when=at)
    st.add_entry("meal", "банан 120г", 107, "text", 1.3, 0.4, 27.4,
                 [{"name": "банан", "grams": 120, "kcal": 107}], when=at)
    assert report.goal(st, day) == 1350
    assert report.remaining(st, day) == 1350 - 1207 + 200
    assert "осталось 343" in report.format_day(st, day)

    st.set("goal_manual", 1000)
    assert report.left_line(st, day).startswith("🟡 сверх нормы на 7, но ещё в дефиците")

    st.add_weight(57.5, day)
    wb = load_workbook(report.build_xlsx(st, tmp_path / "x.xlsx"))
    assert wb.sheetnames == ["Журнал", "Дни", "Вес"]
    assert wb["Журнал"].max_row == 4
    assert wb["Журнал"]["E3"].value == -200


def test_week_low_average_note(st):
    for d in range(1, 8):
        st.add_entry("quick", "рацион", 1100, "manual", when=datetime(2026, 10, d, 12))
    text = report.format_week(st, date(2026, 10, 7))
    assert "в среднем 1 100" in text and "ниже 1200" in text


def test_dishes(st):
    st.put_dish("Цезарь  Жан-Жак", 450)
    assert st.get_dish("цезарь жан-жак")["kcal"] == 450
    assert st.get_dish("цезарь") is None
    st.put_dish("цезарь жан-жак", 520)
    assert [(r["name"], r["kcal"]) for r in st.dishes()] == [("цезарь жан-жак", 520)]
    assert st.delete_dish("ЦЕЗАРЬ жан-жак") and not st.dishes()


def test_day_starts_at_4am(tmp_path):
    path = str(tmp_path / "d.db")
    old = Storage(path, day_start_hour=0)  # как было раньше: день по календарю
    old.add_entry("quick", "лимонад", 100, "manual", when=datetime(2026, 10, 8, 0, 18))
    assert old.entries(date(2026, 10, 8))

    st = Storage(path, day_start_hour=4)  # старые записи пересчитываются при старте
    assert st.entries(date(2026, 10, 7))[0]["descr"] == "лимонад"
    st.add_entry("quick", "завтрак", 300, "manual", when=datetime(2026, 10, 8, 4, 0))
    assert st.day_totals(date(2026, 10, 8))["eaten"] == 300


def test_edit_delete_restore(st):
    eid = st.add_entry("meal", "овсянка 60г", 228, "text", 8, 4, 40,
                       [{"name": "овсянка", "grams": 60, "kcal": 228}])
    st.set_entry_kcal(eid, 114)  # полпорции — БЖУ тоже пополам
    e = st.get_entry(eid)
    assert (e["kcal"], e["protein"], e["fat"], e["carbs"]) == (114, 4, 2, 20)
    saved = st.delete_entry(eid)
    assert st.get_entry(eid) is None
    st.restore_entry(saved)
    assert st.get_entry(eid)["kcal"] == 114
    assert st.db.execute("SELECT COUNT(*) FROM entry_items WHERE entry_id = ?", (eid,)).fetchone()[0] == 1


def test_watch_day(st):
    day = date(2026, 10, 9)
    at = datetime(2026, 10, 9, 21, 30)
    st.add_entry("quick", "рацион", 1300, "manual", when=at)
    st.add_entry("workout", "прогулка", 150, "manual", when=at)
    assert st.day_totals(day)["burned"] == 150
    st.set_watch(412, when=at)          # прогулка внутри: засчитываем 412 − 200, а не 150 + 212
    assert st.day_totals(day)["burned"] == 212
    st.set_watch(300, when=at)          # новая цифра за день заменяет старую; ручные 150 больше
    t = st.day_totals(day)
    assert (t["burned"], t["eaten"]) == (150, 1300)
    assert len([e for e in st.entries(day) if e["kind"] == "watch"]) == 1
    assert "⌚ часы: 300 активных, сверх обычных 100" in report.format_day(st, day)


def test_patterns(st):
    from datetime import timedelta
    end = date(2026, 10, 30)
    assert not report.patterns(st, end)["ok"]
    for k in range(12):
        d = datetime(2026, 10, 19, 10) + timedelta(days=k)
        headache = k % 3 == 0  # каждый третий день: поздний завтрак и две банки лимонада
        first = d.replace(hour=14 if headache else 9)
        st.add_entry("quick", "рацион", 1100, "manual", when=first)
        if headache:
            st.add_entry("quick", "лимонад боржоми", 149, "manual", when=d.replace(hour=16))
            st.add_entry("quick", "лимонад боржоми", 149, "manual", when=d.replace(hour=23))
        st.add_symptom("голова" if headache else "норм", when=d.replace(hour=22))
    p = report.patterns(st, end)
    assert p["ok"] and p["bad"] == 4 and p["good"] == 8
    top = {r["factor"] for r in p["rows"][:3]}
    assert {"сладкие напитки, ккал", "первая еда, час"} <= top
    text = report.format_patterns(st, end)
    assert "первая еда, час: 14:00 против 9:00" in text and "298 против 0" in text


def test_symptom_overrides_ok(st):
    st.add_symptom("норм", datetime(2026, 10, 9, 22))
    st.add_symptom("давление", datetime(2026, 10, 9, 22, 5))
    assert st.symptoms_on(date(2026, 10, 9)) == {"давление"}


def test_water(st):
    day = date(2026, 10, 9)
    st.add_water(250, datetime(2026, 10, 9, 10))
    st.add_water(500, datetime(2026, 10, 9, 13))
    st.add_water(250, datetime(2026, 10, 10, 2))  # до 4 утра — ещё вчера
    assert st.water_on(day) == 1000
    assert st.remove_last_water(day) == 250 and st.water_on(day) == 750
    assert "💧 0,75 л" in report.format_day(st, day)


def test_undo(st):
    st.add_entry("quick", "a", 500, "manual")
    st.add_entry("meal", "b", 300, "text", items=[{"name": "b", "grams": 100, "kcal": 300}])
    assert st.delete_last()["descr"] == "b"
    assert st.db.execute("SELECT COUNT(*) FROM entry_items").fetchone()[0] == 0


def test_week_chart_png(st):
    st.add_entry("quick", "рацион", 1400, "manual", when=datetime(2026, 10, 8, 12))
    png = report.week_chart(st, date(2026, 10, 9))
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000


def test_adaptive_tdee(st):
    from datetime import timedelta
    start = date(2026, 9, 12)
    assert not report.adaptive(st, date(2026, 10, 9))["ok"]
    for k in range(28):
        st.add_entry("quick", "рацион", 1400, "manual", when=datetime(2026, 9, 12, 12) + timedelta(days=k))
    # −0,25 кг в неделю: 0,25 × 7700 / 7 = 275 ккал дефицита в день -> расход ≈ 1675
    for k in (0, 7, 14, 21, 27):
        st.add_weight(round(58 - 0.25 * k / 7, 3), start + timedelta(days=k))
    a = report.adaptive(st, date(2026, 10, 9))
    assert a["ok"] and abs(a["real"] - 1675) < 2 and abs(a["kg_per_week"] + 0.25) < 0.001
    assert a["suggested"] == 1400  # 1675 × 0,85 ≈ 1424 -> 1400
    assert "тратишь около <b>1 675</b>" in report.format_adaptive(st, date(2026, 10, 9))


def test_adaptive_needs_logging(st):
    st.add_weight(58, date(2026, 9, 20))
    st.add_weight(57.5, date(2026, 10, 9))
    st.add_entry("quick", "рацион", 1300, "manual", when=datetime(2026, 10, 1, 12))
    a = report.adaptive(st, date(2026, 10, 9))
    assert not a["ok"] and "70%" in a["why"]


def test_draft_uses_dish_portion(st, usda):
    st.put_dish("санпелегрино гранат апельсин", 125, 0.3, 0, 30.4)
    d = Nutrition(st, usda, use_off=False).fill(Draft([Item("санпелегрино", "sanpellegrino soda", 330),
                                                        Item("банан", "banana raw", 120)]))
    sanpe, banana = d.items
    assert sanpe.name == "санпелегрино гранат апельсин" and sanpe.kcal == 125 and sanpe.part("carbs") == 30.4
    assert banana.portion is None and round(banana.kcal) == 107
    assert "санпелегрино гранат апельсин порция · 125 ккал" in report.format_draft(d, st, date(2026, 10, 9))


def test_hydration_tea_coffee(st):
    day = date(2026, 10, 9)
    at = datetime(2026, 10, 9, 12)
    st.add_water(400, at)
    st.add_entry("meal", "чай чёрный 500г", 0, "text", items=[{"name": "чай чёрный", "grams": 500, "kcal": 0}], when=at)
    st.add_entry("quick", "капучино на овсяном", 135, "manual", when=at)   # без объёма: чашка 250
    st.add_entry("meal", "эспрессо 30г", 1, "text", items=[{"name": "эспрессо", "grams": 30, "kcal": 1}], when=at)
    st.add_entry("quick", "чак-чак", 300, "manual", when=at)               # не напиток
    h = report.hydration(st, day)
    assert h["water"] == 400 and h["drinks"] == 0.9 * 500 + 0.8 * 250 + 0.8 * 30
    assert "💧 1,07 л (из них чай и кофе 0,67 л)" in report.format_day(st, day)


def test_water_phrases():
    import bot
    cases = {"стакан воды": 200, "2 стакана воды": 400, "Выпила стакан воды.": 200, "полбутылки воды": 250,
             "бутылка воды": 500, "кружка воды": 300, "вода": 200, "вода 0,5 л": 500, "💧": 200}
    for text, ml in cases.items():
        assert bot.water_amount(text) == ("+", ml), text
    assert bot.water_amount("-вода") == ("-", None)
    assert bot.water_amount("стакан кефира") is None and bot.water_amount("водка") is None


def test_flavoured_water_is_water():
    import bot
    assert bot.water_amount("боржоми аромати") == ("+", 200)
    assert bot.water_amount("аромати 500") == ("+", 500)
    assert bot.water_amount("минералка") == ("+", 200)
    assert bot.water_amount("лимонад боржоми") is None


def test_three_levels(st):
    day = date(2026, 10, 9)
    at = datetime(2026, 10, 9, 12)
    maint = report.formula_tdee(st, day)            # ~1584 для профиля по умолчанию; норма 1350
    st.add_entry("quick", "рацион", 1300, "manual", when=at)
    assert report.level(st, day)[0] == "норма" and report.left_line(st, day) == "осталось 50 ккал"
    st.add_entry("quick", "лимонад", 149, "manual", when=at)
    assert report.level(st, day)[0] == "дефицит"
    st.add_entry("quick", "блинчики", 294, "manual", when=at)
    kind, over_goal, over_maint = report.level(st, day)
    assert kind == "перебор" and round(over_maint) == round(1743 - maint)
    assert report.left_line(st, day).startswith("🔺 перебор: на ")


def test_activity_hint(st):
    assert report.minutes_for(254, 3.5, 58) == 100   # ходьба: 2,54 ккал/мин сверх покоя
    assert report.minutes_for(203, 5.0, 58) == 50    # силовая: 4,06 ккал/мин
    assert report.hm(135) == "2 ч 15 мин" and report.hm(60) == "1 ч" and report.hm(45) == "45 мин"
    day = date(2026, 10, 9)
    st.add_entry("quick", "рацион", 1300, "manual", when=datetime(2026, 10, 9, 12))
    assert report.activity_hint(st, day) == ""
    st.add_entry("quick", "блинчики", 294, "manual", when=datetime(2026, 10, 9, 13))
    assert "быстрой ходьбы" in report.activity_hint(st, day) and "силовой" in report.format_day(st, day)
