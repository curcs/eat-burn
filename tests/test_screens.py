import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screens import Dish, parse_card, parse_list
from storage import Storage

# так tesseract читает карточку блюда BeFit
CARD = """2-й завтрак

197 24.3 5.9 11.6 150
Ккал Б ж У Гр
Живой творог с запеченными
фруктами
творог, молоко, яблоки, груши, корица,
мед
"""

# а так — левую колонку списка заказов после повышения контраста
LIST_COLUMN = """23:00 4
Мои 3
Доставленные
AH®
Панкейки из муки
грубого помола с
джемом
1-й завтрак 248 Ккал
АН®
Куриные биточки на
пару и гречневая
лапша с шампиньо...
Обед 225 Ккал
Главная Заказать
"""


def test_card():
    d = parse_card(CARD)
    assert d == Dish("живой творог с запеченными фруктами", 197, "2-й завтрак", 24.3, 5.9, 11.6, 150)
    assert d.consistent()
    assert not Dish("x", 107, "", 24.3, 5.9, 11.6).consistent()  # OCR прочитал 197 как 107
    assert parse_card(LIST_COLUMN) is None


def test_list():
    assert [(d.name, d.kcal, d.meal) for d in parse_list(LIST_COLUMN)] == [
        ("панкейки из муки грубого помола с джемом", 248, "1-й завтрак"),
        ("куриные биточки на пару и гречневая лапша с шампиньо...", 225, "обед"),
    ]
    assert parse_list(CARD) == []


def test_dish_memory_keeps_pfc(tmp_path):
    st = Storage(str(tmp_path / "s.db"))
    st.put_dish("куриные биточки на пару и гречневая лапша с шампиньонами", 225, 20, 5, 25)
    st.put_dish("куриные биточки на пару и гречневая лапша с шампиньонами", 225)  # из списка, без БЖУ
    found = st.find_dish("куриные биточки на пару и гречневая лапша с шампиньо...")
    assert (found["kcal"], found["protein"]) == (225, 20)
    st.put_dish("куриные биточки на пару и гречневая лапша с шампиньонами", 300)  # блюдо поменялось
    assert st.get_dish("куриные биточки на пару и гречневая лапша с шампиньонами")["protein"] is None


def test_search_dishes(tmp_path):
    st = Storage(str(tmp_path / "f.db"))
    for name, kcal in [("лимонад", 100), ("sanpellegrino гранат", 125), ("санпеллегрино", 125),
                       ("капучино на овсяном", 135), ("лимонад тархун", 90)]:
        st.put_dish(name, kcal)
    for _ in range(3):
        st.add_entry("quick", "лимонад тархун", 90, "manual")
    assert [d["name"] for d in st.search_dishes("лимон")] == ["лимонад тархун", "лимонад"]  # частое выше
    assert [d["name"] for d in st.search_dishes("сан пел")] == ["санпеллегрино"]
    assert st.search_dishes("кап")[0]["kcal"] == 135
    assert st.search_dishes()[0]["name"] == "лимонад тархун"
    assert st.search_dishes("борщ") == []


def test_eaten_on(tmp_path):
    st = Storage(str(tmp_path / "e.db"))
    st.add_entry("quick", "живой творог с запеченными фруктами", 197, "screen", when=datetime(2026, 10, 9, 11))
    assert "живой творог с запеченными фруктами" in st.eaten_on(date(2026, 10, 9))
