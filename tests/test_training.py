import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from training import Exercise, Gym, format_plan, format_progress, group_of, parse_line, parse_session, suggest

TRAINER = """Жим платформы лежа 50кг*15*3
Сгибание ног лежа 20кг*15*3
Разведение ног 3D 20кг*15-15кг*15*2
Жим от груди 10кг*15-10кг*12-10кг*10
Тяга вертикальная 22,7кг*15-24,7*12-22,7*15
Жим вверх 5кг*15-12-10
Горизонтальная тяга 18,1*15-22,7*12-18,1*15
Пресс в тренажере 10кг*15-15*15*2"""


def test_parse_lines():
    assert parse_line("Жим платформы лежа 50кг*15*3") == Exercise("жим платформы лежа", [(50, 15)] * 3)
    assert parse_line("Разведение ног 3D 20кг*15-15кг*15*2").sets == [(20, 15), (15, 15), (15, 15)]
    assert parse_line("Жим вверх 5кг*15-12-10").sets == [(5, 15), (5, 12), (5, 10)]
    assert parse_line("Тяга вертикальная 22,7кг*15-24,7*12-22,7*15").sets == [(22.7, 15), (24.7, 12), (22.7, 15)]
    assert parse_line("Пресс в тренажере 10кг*15-15*15*2").sets == [(10, 15), (15, 15), (15, 15)]
    assert parse_line("овсянка 60г с бананом") is None
    assert len(parse_session(TRAINER)) == 8
    assert parse_session("450 рацион обед") == [] and parse_session("2 яйца и тост") == []


def test_groups():
    assert group_of("жим платформы лежа") == "ноги"
    assert group_of("жим от груди") == "жимы" and group_of("тяга вертикальная") == "спина"
    assert group_of("пресс в тренажере") == "пресс"


def test_plan_and_progress():
    gym = Gym(sqlite3.connect(":memory:"))
    gym.add_session(date(2026, 10, 21), parse_session(TRAINER), "trainer")
    plan = {p.name: p for p in suggest(gym, date(2026, 10, 27))}
    assert plan["жим платформы лежа"].weight == 52.5           # 50 × 15 × 3 — можно прибавить
    assert plan["жим вверх"].weight == 5                       # 15-12-10 — держим вес
    assert plan["тяга вертикальная"].sets == [(22.7, 15), (24.7, 12), (22.7, 15)]  # пирамида — как было
    assert len(plan) <= 8 and "пресс в тренажере" in plan
    assert "только упражнения, которые были с тренером" in format_plan(list(plan.values()))
    gym.add_session(date(2026, 10, 23), [Exercise("жим платформы лежа", [(52.5, 15)] * 3)], "me")
    assert gym.records([Exercise("жим платформы лежа", [(55, 12)])], date(2026, 10, 27)) == \
        ["жим платформы лежа: 55 кг, было 52,5 💪"]
    text = format_progress(gym, "платформ")
    assert "21.10  50 кг × 15 × 3 · объём 2 250 кг" in text and "23.10  52,5 кг × 15 × 3" in text


def test_alias():
    gym = Gym(sqlite3.connect(":memory:"))
    gym.add_session(date(2026, 10, 25), parse_session(TRAINER), "trainer")
    gym.add_alias("тяга вертикальная", "вертикальная тяга со средней рукояткой")
    gym.add_session(date(2026, 10, 28), [Exercise("тяга вертикальная", [(24.7, 15)])], "trainer")
    assert len(gym.history("вертикальная тяга со средней рукояткой")) == 2
    assert "тяга вертикальная" not in gym.exercises()


def test_overview():
    from training import format_overview, plural
    gym = Gym(sqlite3.connect(":memory:"))
    gym.add_session(date(2026, 10, 21), [Exercise("сгибание ног лежа", [(20, 15)] * 2),
                                         Exercise("пресс в тренажере", [(10, 15)] * 2)], "trainer")
    gym.add_session(date(2026, 10, 25), [Exercise("сгибание ног лежа", [(20, 15)] * 3),
                                         Exercise("пресс в тренажере", [(10, 15), (15, 15), (15, 15)]),
                                         Exercise("ягодичный мост", [(35, 15)] * 2)], "trainer")
    text = format_overview(gym)
    assert "2 тренировки" in text and "сгибание ног лежа: объём 600 → 900 кг (+50%)" in text
    assert "пресс в тренажере: 10 → 15 кг 💪" in text and "ягодичный мост: 35 кг × 15" in text
    assert "<i>ноги</i>" in text and "подходов по группам: ноги 7, пресс 5" in text
    assert [plural(k, "тренировка", "тренировки", "тренировок") for k in (1, 2, 5, 11, 22)] == \
        ["тренировка", "тренировки", "тренировок", "тренировок", "тренировки"]


def test_machine_photos():
    from training import image_search_url
    gym = Gym(sqlite3.connect(":memory:"))
    gym.add_session(date(2026, 10, 25), parse_session(TRAINER), "trainer")
    assert gym.find_exercise("жим платформ") == ["жим платформы лежа"]
    assert len(gym.find_exercise("тяга")) == 2
    gym.set_photo("жим платформы лежа", "FILEID")
    assert gym.photo("жим платформы лежа") == "FILEID" and gym.photo("жим вверх") is None
    assert image_search_url("жим вверх").startswith("https://yandex.ru/images/search?text=%D1%82")
