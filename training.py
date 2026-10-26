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
CREATE TABLE IF NOT EXISTS gym_aliases (  -- как тренер пишет коротко -> как называем в журнале
    alias TEXT PRIMARY KEY, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gym_photos (   -- фото тренажёра из твоего зала: file_id в Telegram
    exercise TEXT PRIMARY KEY, file_id TEXT NOT NULL
);
"""

NUM = r"\d+(?:[.,]\d+)?"
_START = re.compile(rf"{NUM}\s*(?:кг)?\s*\*")   # где начинаются подходы: «50кг*», «24,7*»
TARGET_REPS = 15                                # тренер обычно даёт по 12–15

# группа -> как узнать по названию; порядок важен: «жим платформы» — это ноги, а не жим от груди
GROUPS = [
    ("ноги", r"платформ|ног|присед|выпад|ягодиц|ягодич|мост|гакк|икр"),
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

    def add_alias(self, alias: str, name: str):
        """«тяга вертикальная» у тренера = «вертикальная тяга со средней рукояткой»: переименовываем и прошлое."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO gym_aliases (alias, name) VALUES (?,?)", (alias, name))
            self.db.execute("UPDATE gym_sets SET exercise = ? WHERE exercise = ?", (name, alias))

    def canonical(self, name: str) -> str:
        row = self.db.execute("SELECT name FROM gym_aliases WHERE alias = ?", (name,)).fetchone()
        return row[0] if row else name

    def find_exercise(self, query: str) -> list[str]:
        """Упражнения журнала, в названии которых есть все слова запроса (можно начала слов)."""
        words = query.lower().replace("ё", "е").split()
        return [n for n in self.exercises()
                if all(any(w2.startswith(w) for w2 in n.replace("ё", "е").split()) for w in words)]

    def set_photo(self, exercise: str, file_id: str):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO gym_photos (exercise, file_id) VALUES (?,?)", (exercise, file_id))

    def photo(self, exercise: str) -> str | None:
        row = self.db.execute("SELECT file_id FROM gym_photos WHERE exercise = ?", (exercise,)).fetchone()
        return row[0] if row else None


    def add_session(self, day: date, exercises: list[Exercise], source: str) -> int:
        exercises = [Exercise(self.canonical(e.name), e.sets) for e in exercises]
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


def image_search_url(exercise: str) -> str:
    """Поиск картинок в Яндексе: как в целом выглядит тренажёр, пока нет своего фото."""
    from urllib.parse import quote
    return "https://yandex.ru/images/search?text=" + quote(f"тренажер {exercise}")

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


def _best(sets: list[tuple[float | None, int]]) -> tuple[float | None, int]:
    """Лучший подход: самый большой вес, при равенстве — больше повторов."""
    return max(sets, key=lambda s: ((s[0] or 0), s[1]))


def _volume(sets) -> float:
    return sum((w or 0) * r for w, r in sets)


def kg(x: float) -> str:
    return f"{round(x):,}".replace(",", " ")


def plural(k: int, one: str, few: str, many: str) -> str:
    """1 тренировка, 2 тренировки, 5 тренировок."""
    if k % 10 == 1 and k % 100 != 11:
        return one
    return few if 2 <= k % 10 <= 4 and not 12 <= k % 100 <= 14 else many


def format_overview(gym: Gym) -> str:
    """/progress без слов: прогресс там, где он есть, всё, что умеешь, с максимумами, и короткий анализ."""
    sessions = gym.db.execute("SELECT id, day, source FROM gym_sessions ORDER BY day, id").fetchall()
    if not sessions:
        return "журнал пуст: перешли мне сообщение тренера с тренировкой"
    names = sorted(gym.exercises(), key=lambda n: ([g for g, _ in GROUPS] + ["другое"]).index(group_of(n)))

    progress, steady, skills = [], [], {}
    for name in names:
        hist = gym.history(name)
        best_w, best_r = _best([s for _, sets in hist for s in sets])
        skills.setdefault(group_of(name), []).append(
            f"• {name}: {fmt_kg(best_w)} кг × {best_r}" + (f" · {len(hist)} раза" if len(hist) > 1 else ""))
        if len(hist) < 2:
            continue
        (d0, first), (d1, last) = hist[0], hist[-1]
        w0, w1 = _best(first)[0] or 0, _best(last)[0] or 0
        v0, v1 = _volume(first), _volume(last)
        if w1 > w0:
            progress.append(f"• {name}: {fmt_kg(w0)} → {fmt_kg(w1)} кг 💪")
        elif v1 > v0 * 1.05:
            progress.append(f"• {name}: объём {kg(v0)} → {kg(v1)} кг (+{round((v1 / v0 - 1) * 100)}%)")
        else:
            steady.append(name)

    vols = []
    for sid, day, source in sessions:
        v = gym.db.execute("SELECT COALESCE(SUM(COALESCE(weight, 0) * reps), 0) FROM gym_sets WHERE session_id = ?",
                           (sid,)).fetchone()[0]
        vols.append(f"{day[8:10]}.{day[5:7]} {kg(v)} кг" + (" (сама)" if source != "trainer" else ""))
    trainer = sum(1 for s in sessions if s[2] == "trainer")
    groups_done = {g for g in skills}
    missing = [g for g in ("ноги", "спина", "жимы", "пресс") if g not in groups_done]

    out = [f"<b>силовые</b>: {len(sessions)} {plural(len(sessions), 'тренировка', 'тренировки', 'тренировок')} "
           f"({trainer} с тренером, {len(sessions) - trainer} сама), "
           f"{sessions[0][1][8:10]}.{sessions[0][1][5:7]}–{sessions[-1][1][8:10]}.{sessions[-1][1][5:7]}".replace("–", "-")]
    out.append("\n<b>📈 прогресс</b>\n" + ("\n".join(progress) if progress else
               "пока не видно: по большинству упражнений была одна тренировка. через пару недель будет с чем сравнить"))
    if steady:
        out.append("держатся на месте: " + ", ".join(steady))
    out.append("\n<b>🏋️ что умеешь и максимумы</b>")
    for g, lines in skills.items():
        out.append(f"<i>{g}</i>\n" + "\n".join(lines))
    out.append("\n<b>📊 объём за тренировку</b>\n" + " · ".join(vols))
    per_group: dict[str, int] = {}
    for (name, cnt) in gym.db.execute("SELECT exercise, COUNT(*) FROM gym_sets GROUP BY exercise").fetchall():
        per_group[group_of(name)] = per_group.get(group_of(name), 0) + cnt
    out.append("подходов по группам: " + ", ".join(f"{g} {c}" for g, c in sorted(per_group.items(), key=lambda x: -x[1])))
    if missing:
        out.append(f"\nпока не было: {', '.join(missing)}")
    out.append("\nподробно по упражнению: <code>/progress тяга</code>")
    return "\n".join(out)
