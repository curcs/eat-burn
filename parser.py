"""Простые записи без модели: «450 рацион обед», «-200 бег»."""
import re
from dataclasses import dataclass

# число (можно «1 100»), необязательное «ккал», потом описание.
# Если после числа идёт единица веса/штук («100г творога») — это еда словами.
_QUICK = re.compile(
    r"""^\s*(?P<sign>[+-])?\s*
        (?P<num>\d{1,2}\s\d{3}|\d+)
        \s*(?:ккал|kcal|кк)?
        (?P<rest>(?:\s+.*)?)$""",
    re.X | re.I | re.S,
)
# единица — отдельным словом: «4 л-карнитин» — это не литры
_UNIT = re.compile(r"^\s*(г|гр|грамм\w*|g|кг|мл|ml|л|шт\w*|ложк\w*|стакан\w*|кус\w*)(?=$|[\s.,;])", re.I)

MIN_QUICK_KCAL = 30  # меньше — то ли «4 ккал», то ли «2 яйца»: спрашиваем


@dataclass
class Quick:
    kind: str  # "quick" (еда числом), "workout" или "ambiguous" (мало — ккал или штуки?)
    kcal: int
    desc: str


def parse_quick(text: str) -> Quick | None:
    m = _QUICK.match(text)
    if not m:
        return None
    rest = m.group("rest").strip()
    if _UNIT.match(rest):
        return None
    kcal = int(m.group("num").replace(" ", ""))
    if m.group("sign") == "-":
        return Quick("workout", kcal, rest or "тренировка")
    if kcal < MIN_QUICK_KCAL and rest:
        return Quick("ambiguous", kcal, rest)
    return Quick("quick", kcal, rest or "еда")


_EDIT_LINE = re.compile(r"^\s*(?P<del>-)?\s*(?P<name>[^\d]+?)\s*(?P<grams>\d+)?\s*(?:г|гр|g)?\s*$", re.I)


def parse_edit_lines(text: str) -> list[tuple[str, int | None, bool]]:
    """Строки правки черновика: «банан 150», «-мёд». -> [(имя, граммы, удалить)]."""
    out = []
    for line in re.split(r"[\n,;]+", text):
        m = _EDIT_LINE.match(line)
        if m and m.group("name").strip():
            g = m.group("grams")
            out.append((m.group("name").strip().lower(), int(g) if g else None, bool(m.group("del"))))
    return out
