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
