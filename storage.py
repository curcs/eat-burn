"""SQLite: записи, продукты внутри приёмов пищи, личный справочник, вес, настройки."""
import sqlite3
from datetime import date, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,          -- meal | quick | workout
    descr TEXT NOT NULL,
    kcal REAL NOT NULL,
    protein REAL, fat REAL, carbs REAL,
    source TEXT NOT NULL         -- manual | text | photo
);
CREATE INDEX IF NOT EXISTS entries_day ON entries(day);
CREATE TABLE IF NOT EXISTS entry_items (
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    name TEXT NOT NULL, grams REAL NOT NULL,
    kcal REAL NOT NULL, protein REAL, fat REAL, carbs REAL,
    match TEXT
);
CREATE TABLE IF NOT EXISTS foods (
    name TEXT PRIMARY KEY,       -- в нижнем регистре, по-русски
    kcal REAL NOT NULL, protein REAL, fat REAL, carbs REAL
);
CREATE TABLE IF NOT EXISTS dishes (
    name TEXT PRIMARY KEY,       -- нормализованное название, как писала («цезарь жан-жак»)
    kcal REAL NOT NULL,          -- на порцию
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS water (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, day TEXT NOT NULL, ml REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS weights (day TEXT PRIMARY KEY, kg REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def norm(name: str) -> str:
    return " ".join(name.lower().replace("ё", "е").split())


class Storage:
    def __init__(self, path: str, day_start_hour: int = 4, active_baseline: float = 200):
        """day_start_hour: до этого часа еда считается во вчерашний день (перекус в 00:30 — это ещё вечер).
        active_baseline: сколько активных ккал за день часы насчитают и в сидячий день — это уже в норме."""
        self.active_baseline = active_baseline
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(dishes)")}
        for col in ("protein", "fat", "carbs"):  # БЖУ у блюд появились позже — досоздаём колонки
            if col not in cols:
                self.db.execute(f"ALTER TABLE dishes ADD COLUMN {col} REAL")
        self.day_start_hour = day_start_hour
        if self.get("day_start_hour") != str(day_start_hour):
            self._recompute_days()

    def day_of(self, when: datetime) -> date:
        return (when - timedelta(hours=self.day_start_hour)).date()

    def today(self) -> date:
        return self.day_of(datetime.now())

    def _recompute_days(self):
        """Граница дня поменялась — пересчитываем день у всех записей по их времени."""
        rows = self.db.execute("SELECT id, ts FROM entries").fetchall()
        with self.db:
            self.db.executemany("UPDATE entries SET day = ? WHERE id = ?",
                                [(self.day_of(datetime.fromisoformat(r["ts"])).isoformat(), r["id"]) for r in rows])
        self.set("day_start_hour", self.day_start_hour)

    # --- записи ---
    def add_entry(self, kind: str, descr: str, kcal: float, source: str,
                  protein=None, fat=None, carbs=None, items=(), when: datetime | None = None) -> int:
        when = when or datetime.now()
        with self.db:
            cur = self.db.execute(
                "INSERT INTO entries (ts, day, kind, descr, kcal, protein, fat, carbs, source)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (when.isoformat(timespec="seconds"), self.day_of(when).isoformat(), kind, descr,
                 kcal, protein, fat, carbs, source))
            eid = cur.lastrowid
            self.db.executemany(
                "INSERT INTO entry_items (entry_id, name, grams, kcal, protein, fat, carbs, match)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [(eid, i["name"], i["grams"], i["kcal"], i.get("protein"), i.get("fat"),
                  i.get("carbs"), i.get("match")) for i in items])
        return eid

    def get_entry(self, entry_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()

    def delete_entry(self, entry_id: int) -> dict | None:
        """Удаляет запись и отдаёт её целиком (с продуктами внутри) — чтобы можно было вернуть."""
        row = self.get_entry(entry_id)
        if not row:
            return None
        items = [dict(i) for i in self.db.execute("SELECT * FROM entry_items WHERE entry_id = ?", (entry_id,))]
        with self.db:
            self.db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        return {"entry": dict(row), "items": items}

    def restore_entry(self, saved: dict) -> None:
        e = saved["entry"]
        with self.db:
            self.db.execute(
                "INSERT INTO entries (id, ts, day, kind, descr, kcal, protein, fat, carbs, source)"
                " VALUES (:id, :ts, :day, :kind, :descr, :kcal, :protein, :fat, :carbs, :source)", e)
            self.db.executemany(
                "INSERT INTO entry_items (entry_id, name, grams, kcal, protein, fat, carbs, match)"
                " VALUES (:entry_id, :name, :grams, :kcal, :protein, :fat, :carbs, :match)", saved["items"])

    def set_entry_kcal(self, entry_id: int, kcal: float) -> None:
        """Новая цифра ккал; БЖУ меняем в той же пропорции — обычно это другая порция того же."""
        row = self.get_entry(entry_id)
        k = kcal / row["kcal"] if row and row["kcal"] else None
        scale = lambda v: None if v is None or k is None else round(v * k, 1)
        with self.db:
            self.db.execute("UPDATE entries SET kcal = ?, protein = ?, fat = ?, carbs = ? WHERE id = ?",
                            (kcal, scale(row["protein"]), scale(row["fat"]), scale(row["carbs"]), entry_id))

    def delete_last(self) -> sqlite3.Row | None:
        row = self.db.execute("SELECT * FROM entries ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            with self.db:
                self.db.execute("DELETE FROM entries WHERE id = ?", (row["id"],))
        return row

    def entries(self, day_from: date, day_to: date | None = None) -> list[sqlite3.Row]:
        day_to = day_to or day_from
        return self.db.execute(
            "SELECT * FROM entries WHERE day BETWEEN ? AND ? ORDER BY ts",
            (day_from.isoformat(), day_to.isoformat())).fetchall()

    def all_entries(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM entries ORDER BY ts").fetchall()

    def day_totals(self, day: date) -> dict:
        """burned — большее из ручных тренировок и «часы за день минус обычное движение»:
        прогулка с часов уже входит в их дневную цифру, дважды не считаем."""
        r = self.db.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN kind NOT IN ('workout', 'watch') THEN kcal END), 0) AS eaten,
                 COALESCE(SUM(CASE WHEN kind = 'workout' THEN kcal END), 0) AS burned,
                 MAX(CASE WHEN kind = 'watch' THEN kcal END) AS watch,
                 COALESCE(SUM(protein), 0) AS protein,
                 COALESCE(SUM(fat), 0) AS fat,
                 COALESCE(SUM(carbs), 0) AS carbs,
                 COUNT(*) AS n
               FROM entries WHERE day = ?""", (day.isoformat(),)).fetchone()
        t = dict(r)
        if t["watch"] is not None:
            t["burned"] = max(t["burned"], t["watch"] - self.active_baseline, 0)
        return t

    def set_watch(self, kcal: float, when: datetime | None = None) -> int:
        """Активные ккал с часов за день: новая цифра заменяет старую за тот же день."""
        when = when or datetime.now()
        with self.db:
            self.db.execute("DELETE FROM entries WHERE kind = 'watch' AND day = ?", (self.day_of(when).isoformat(),))
        return self.add_entry("watch", "часы за день", kcal, "watch", when=when)

    # --- справочник ---
    def get_food(self, name: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM foods WHERE name = ?", (name.strip().lower(),)).fetchone()

    def put_food(self, name: str, kcal: float, protein=None, fat=None, carbs=None):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO foods (name, kcal, protein, fat, carbs) VALUES (?,?,?,?,?)",
                (name.strip().lower(), kcal, protein, fat, carbs))

    # --- блюда на порцию ---
    def get_dish(self, name: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM dishes WHERE name = ?", (norm(name),)).fetchone()

    def put_dish(self, name: str, kcal: float, protein=None, fat=None, carbs=None):
        """Новая цифра ккал перезаписывает старую. Известные БЖУ сохраняются, если ккал те же
        (из списка заказов БЖУ не видно), и сбрасываются, если блюдо стало другим."""
        with self.db:
            self.db.execute(
                """INSERT INTO dishes (name, kcal, updated, protein, fat, carbs) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET kcal = excluded.kcal, updated = excluded.updated,
                     protein = COALESCE(excluded.protein, CASE WHEN kcal = excluded.kcal THEN protein END),
                     fat = COALESCE(excluded.fat, CASE WHEN kcal = excluded.kcal THEN fat END),
                     carbs = COALESCE(excluded.carbs, CASE WHEN kcal = excluded.kcal THEN carbs END)""",
                (norm(name), kcal, date.today().isoformat(), protein, fat, carbs))

    def find_dish(self, name: str) -> sqlite3.Row | None:
        """Как get_dish, но понимает обрезанные названия из списка заказов: «…лапша с шампиньо...»."""
        exact = self.get_dish(name)
        if exact or not name.endswith("..."):
            return exact
        prefix = norm(name[:-3])
        return next((d for d in self.dishes() if d["name"].startswith(prefix)), None)

    def find_dish_by_pfc(self, protein: float, fat: float, carbs: float) -> sqlite3.Row | None:
        """Блюдо с теми же БЖУ: OCR мог исказить название («мукитрубого»), а цифры БЖУ почти уникальны."""
        return next((d for d in self.dishes() if d["protein"] is not None
                     and abs(d["protein"] - protein) < 0.05 and abs(d["fat"] - fat) < 0.05
                     and abs(d["carbs"] - carbs) < 0.05), None)

    def search_dishes(self, query: str = "", limit: int = 8) -> list[sqlite3.Row]:
        """Блюда, в названии которых есть все слова запроса (можно начала слов: «сан пел»).
        Сначала то, что записывается чаще. Пустой запрос — просто самое частое."""
        words = norm(query).split()
        freq: dict[str, int] = {}
        for e in self.all_entries():
            freq[norm(e["descr"])] = freq.get(norm(e["descr"]), 0) + 1
        found = [d for d in self.dishes()
                 if all(any(w2.startswith(w) for w2 in d["name"].replace("-", " ").split()) or w in d["name"]
                        for w in words)]
        found.sort(key=lambda d: (-freq.get(d["name"], 0), d["name"]))
        return found[:limit]

    def eaten_on(self, day: date) -> set[str]:
        """Названия того, что уже записано за день, — чтобы не записать одно блюдо дважды."""
        return {norm(e["descr"]) for e in self.entries(day) if e["kind"] not in ("workout", "watch")}

    def dishes(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM dishes ORDER BY name").fetchall()

    def delete_dish(self, name: str) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM dishes WHERE name = ?", (norm(name),)).rowcount > 0

    # --- вес ---
    def add_weight(self, kg: float, day: date | None = None):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO weights (day, kg) VALUES (?,?)",
                            ((day or self.today()).isoformat(), kg))

    # --- вода ---
    def add_water(self, ml: float, when: datetime | None = None):
        when = when or datetime.now()
        with self.db:
            self.db.execute("INSERT INTO water (ts, day, ml) VALUES (?,?,?)",
                            (when.isoformat(timespec="seconds"), self.day_of(when).isoformat(), ml))

    def remove_last_water(self, day: date) -> float | None:
        r = self.db.execute("SELECT id, ml FROM water WHERE day = ? ORDER BY id DESC LIMIT 1",
                            (day.isoformat(),)).fetchone()
        if r:
            with self.db:
                self.db.execute("DELETE FROM water WHERE id = ?", (r["id"],))
        return r["ml"] if r else None

    def water_on(self, day: date) -> float:
        return self.db.execute("SELECT COALESCE(SUM(ml), 0) FROM water WHERE day = ?",
                               (day.isoformat(),)).fetchone()[0]

    def weighed_on(self, day: date) -> bool:
        return self.db.execute("SELECT 1 FROM weights WHERE day = ?", (day.isoformat(),)).fetchone() is not None

    def last_entry_time(self) -> datetime | None:
        r = self.db.execute("SELECT ts FROM entries ORDER BY ts DESC LIMIT 1").fetchone()
        return datetime.fromisoformat(r["ts"]) if r else None

    def last_weight(self) -> float | None:
        r = self.db.execute("SELECT kg FROM weights ORDER BY day DESC LIMIT 1").fetchone()
        return r["kg"] if r else None

    def weights(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM weights ORDER BY day").fetchall()

    # --- настройки ---
    def get(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else default

    def set(self, key: str, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(value)))
