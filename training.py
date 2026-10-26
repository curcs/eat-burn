"""Силовые: журнал из сообщений тренера, план для самостоятельной тренировки и прогресс по упражнениям.

Формат тренера: «Жим платформы лежа 50кг*15*3», «Жим вверх 5кг*15-12-10», «Тяга 22,7кг*15-24,7*12-22,7*15».
Подход отделяется «-»: «вес*повторы*подходы», «вес*повторы» или просто «повторы» с тем же весом.
План строится только из упражнений, которые уже были с тренером: технику знаешь, новых бот не придумывает.
"""
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS gym_sessions (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL,
    source TEXT NOT NULL            -- trainer | me | plan
);
CREATE TABLE IF NOT EXISTS gym_sets (
    session_id INTEGER NOT NULL REFERENCES gym_sessions(id) ON DELETE CASCADE,
    exercise TEXT NOT NULL, set_no INTEGER NOT NULL, weight REAL, reps INTEGER NOT NULL
);
"""

NUM = r"\d+(?:[.,]\d+)?"
_START = re.compile(rf"{NUM}\s*(?:кг)?\s*\*")   # где начинаются подходы: «50кг*», «24,7*»
TARGET_REPS = 15                                # тренер обычно даёт по 12–15

# группа -> как узнать по названию; порядок важен: «жим платформы» — это ноги, а не жим от груди
GROUPS = [
    ("ноги", r"платформ|ног|присед|выпад|ягодиц|гакк|икр"),
    ("спина", r"тяг|подтяг|гребл|гиперэкст"),
    ("жимы", r"жим|груд|отжим|бабочк|плеч|дельт|махи"),
    ("руки", r"бицепс|трицепс|рук"),
    ("пресс", r"пресс|планк|скручив"),
]
PLAN_SHAPE = {"ноги": 3, "спина": 2, "жимы": 2, "руки": 0, "пресс": 1}  # как у тренера: всё тело, 6–8 упражнений


@dataclass
class Exercise:
    name: str
    sets: list[tuple[float | None, int]]  # (вес, повторы)


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def parse_line(line: str) -> Exercise | None:
    m = _START.search(line)
    if not m:
        return None
    name = " ".join(line[:m.start()].split()).strip(" -:").lower()
    if not name:
        return None
    sets, weight = [], None
    for seg in line[m.start():].split("-"):
        nums = re.findall(NUM, seg)
        has_kg = "кг" in seg
        if not nums:
            continue
        if len(nums) == 1 and not has_kg:          # «12» — повторы с тем же весом
            sets.append((weight, int(_num(nums[0]))))
        elif len(nums) == 1:                       # «20кг» без повторов — странно, пропускаем
            continue
        else:
            weight, reps = _num(nums[0]), int(_num(nums[1]))
            count = int(_num(nums[2])) if len(nums) > 2 else 1
            sets += [(weight, reps)] * count
    return Exercise(name, sets) if sets else None


def parse_session(text: str) -> list[Exercise]:
    """Сообщение тренера целиком. Если упражнений меньше двух — это, скорее всего, не тренировка."""
    out = [e for e in (parse_line(l) for l in text.splitlines()) if e]
    return out if len(out) >= 2 else []


def group_of(name: str) -> str:
    return next((g for g, rx in GROUPS if re.search(rx, name)), "другое")


class Gym:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript(SCHEMA)

    def add_session(self, day: date, exercises: list[Exercise], source: str) -> int:
        with self.db:
            sid = self.db.execute("INSERT INTO gym_sessions (day, source) VALUES (?,?)",
                                  (day.isoformat(), source)).lastrowid
            self.db.executemany(
                "INSERT INTO gym_sets (session_id, exercise, set_no, weight, reps) VALUES (?,?,?,?,?)",
                [(sid, e.name, k + 1, w, r) for e in exercises for k, (w, r) in enumerate(e.sets)])
        return sid

    def delete_session(self, sid: int):
        with self.db:
            self.db.execute("DELETE FROM gym_sessions WHERE id = ?", (sid,))

    def history(self, exercise: str) -> list[tuple[str, list[tuple[float | None, int]]]]:
        """[(день, подходы)] по упражнению, от старых к новым."""
        rows = self.db.execute(
            """SELECT s.id, s.day, g.weight, g.reps FROM gym_sets g JOIN gym_sessions s ON s.id = g.session_id
               WHERE g.exercise = ? ORDER BY s.day, s.id, g.set_no""", (exercise,)).fetchall()
        out: dict[int, tuple[str, list]] = {}
        for sid, day, w, r in rows:
            out.setdefault(sid, (day, []))[1].append((w, r))
        return list(out.values())

    def exercises(self, since: date | None = None) -> dict[str, int]:
        """Упражнение -> сколько раз было (с даты)."""
        rows = self.db.execute(
            """SELECT g.exercise, COUNT(DISTINCT s.id) FROM gym_sets g JOIN gym_sessions s ON s.id = g.session_id
               WHERE s.day >= ? GROUP BY g.exercise""", ((since or date(2000, 1, 1)).isoformat(),)).fetchall()
        return dict(rows)

    def records(self, session_exercises: list[Exercise], before_day: date) -> list[str]:
        """Новые максимумы по весу по сравнению со всем, что было раньше этого дня."""
        out = []
        for e in session_exercises:
            prev = self.db.execute(
                """SELECT MAX(g.weight) FROM gym_sets g JOIN gym_sessions s ON s.id = g.session_id
                   WHERE g.exercise = ? AND s.day < ?""", (e.name, before_day.isoformat())).fetchone()[0]
            top = max((w for w, _ in e.sets if w is not None), default=None)
            if prev is not None and top is not None and top > prev:
                out.append(f"{e.name}: {fmt_kg(top)} кг, было {fmt_kg(prev)} 💪")
        return out


def fmt_kg(w: float | None) -> str:
    if w is None:
        return "свой вес"
    return f"{w:g}".replace(".", ",")


def step_for(weight: float) -> float:
    """Шаг прибавки: на тренажёрах обычно 2,5 кг, на лёгких гантелях — 1."""
    return 1.0 if weight < 10 else 2.5


@dataclass
class PlanItem:
    name: str
    sets: list[tuple[float | None, int]]
    why: str

    @property
    def weight(self) -> float | None:
        return max((w for w, _ in self.sets if w is not None), default=None)

    def as_exercise(self) -> Exercise:
        return Exercise(self.name, list(self.sets))


def format_sets(sets: list[tuple[float | None, int]]) -> str:
    """[(50,15)]*3 -> «50 кг × 15 × 3»; пирамида -> «22,7×15 - 24,7×12 - 22,7×15»."""
    weights = {w for w, _ in sets}
    if len(weights) == 1:
        w = next(iter(weights))
        reps = [r for _, r in sets]
        tail = f"{reps[0]} × {len(reps)}" if len(set(reps)) == 1 else "-".join(map(str, reps))
        return f"{fmt_kg(w)} кг × {tail}"
    return " - ".join(f"{fmt_kg(w)}×{r}" for w, r in sets)


def suggest(gym: Gym, today: date, weeks: int = 4) -> list[PlanItem]:
    """План самостоятельной тренировки: самые частые упражнения последних недель по группам мышц,
    вес — по последнему разу, +шаг, если в прошлый раз все подходы были на максимум повторов."""
    recent = gym.exercises(today - timedelta(weeks=weeks)) or gym.exercises()
    by_group: dict[str, list[str]] = {}
    for name, cnt in sorted(recent.items(), key=lambda x: -x[1]):
        by_group.setdefault(group_of(name), []).append(name)
    picked = [n for g, k in PLAN_SHAPE.items() for n in by_group.get(g, [])[:k]]
    plan = []
    for name in picked:
        _, sets = gym.history(name)[-1]
        weights = {w for w, _ in sets}
        w = next(iter(weights))
        if len(weights) == 1 and w is not None and len(sets) >= 3 and min(r for _, r in sets) >= TARGET_REPS:
            new = w + step_for(w)  # всё сделано на максимум повторов с одним весом — можно чуть тяжелее
            plan.append(PlanItem(name, [(new, 12)] * 3, f"в прошлый раз {format_sets(sets)} — прибавляем"))
        else:  # самой безопаснее повторить, как было с тренером
            plan.append(PlanItem(name, list(sets), "как в прошлый раз"))
    return plan


def format_plan(plan: list[PlanItem]) -> str:
    lines = [f"• {p.name}: {format_sets(p.sets)}\n   <i>{p.why}</i>" for p in plan]
    return ("<b>самостоятельная тренировка, всё тело</b>\n" + "\n".join(lines) +
            "\n\nтолько упражнения, которые были с тренером. если что-то болит, не через боль 💛")


def format_progress(gym: Gym, query: str, limit: int = 8) -> str:
    names = [n for n in gym.exercises() if all(w in n for w in query.lower().split())]
    if not names:
        return f"не нашла упражнение «{query}». все упражнения: /progress без слов"
    out = []
    for name in names[:3]:
        out.append(f"<b>{name}</b>")
        for day, sets in gym.history(name)[-limit:]:
            volume = round(sum((w or 0) * r for w, r in sets))
            out.append(f"{day[8:10]}.{day[5:7]}  {format_sets(sets)}"
                       + (f" · объём {volume:,} кг".replace(",", " ") if volume else ""))
    return "\n".join(out)
