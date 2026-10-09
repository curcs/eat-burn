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
CREATE TABLE IF NOT EXISTS weights (day TEXT PRIMARY KEY, kg REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def norm(name: str) -> str:
    return " ".join(name.lower().replace("ё", "е").split())


class Storage:
    def __init__(self, path: str, day_start_hour: int = 4):
        """day_start_hour: до этого часа еда считается во вчерашний день (перекус в 00:30 — это ещё вечер)."""
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
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
        r = self.db.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN kind != 'workout' THEN kcal END), 0) AS eaten,
                 COALESCE(SUM(CASE WHEN kind = 'workout' THEN kcal END), 0) AS burned,
                 COALESCE(SUM(protein), 0) AS protein,
                 COALESCE(SUM(fat), 0) AS fat,
                 COALESCE(SUM(carbs), 0) AS carbs,
                 COUNT(*) AS n
               FROM entries WHERE day = ?""", (day.isoformat(),)).fetchone()
        return dict(r)

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

    def put_dish(self, name: str, kcal: float):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO dishes (name, kcal, updated) VALUES (?,?,?)",
                            (norm(name), kcal, date.today().isoformat()))

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
