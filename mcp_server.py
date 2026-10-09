"""MCP-сервер eat-burn: даёт Claude читать дневник питания. Только чтение, только локально (stdio).

    claude mcp add eat-burn -s user -- <путь>/venv/bin/python <путь>/mcp_server.py
"""
import re
from datetime import date, timedelta
from pathlib import Path

from mcp.server.mcpserver import MCPServer

import report
from storage import Storage

try:
    import config
except ImportError:
    config = None

ROOT = Path(__file__).resolve().parent
st = Storage(str(ROOT / "data" / "eatburn.db"), getattr(config, "DAY_START_HOUR", 4),
             getattr(config, "ACTIVE_BASELINE", 200))
mcp = MCPServer("eat-burn")


def plain(html: str) -> str:
    return re.sub(r"</?(b|i|code|pre)>", "", html).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def day_arg(day: str | None) -> date:
    return date.fromisoformat(day) if day else st.today()


@mcp.tool()
def day_summary(day: str | None = None) -> str:
    """Записи за день (YYYY-MM-DD, по умолчанию сегодня), итоги, БЖУ, вода, остаток до нормы."""
    return plain(report.format_day(st, day_arg(day), str(day_arg(day))))


@mcp.tool()
def week_summary(end: str | None = None) -> str:
    """Неделя, которая заканчивается в end (по умолчанию сегодня): ккал по дням, средняя, вес."""
    return plain(report.format_week(st, day_arg(end)))


@mcp.tool()
def entries(start: str, end: str | None = None) -> list[dict]:
    """Все записи за период: время, тип (meal/quick/workout/watch), описание, ккал, БЖУ."""
    rows = st.entries(date.fromisoformat(start), day_arg(end))
    return [{k: r[k] for k in ("ts", "day", "kind", "descr", "kcal", "protein", "fat", "carbs")} for r in rows]


@mcp.tool()
def daily_totals(days: int = 14) -> list[dict]:
    """Итоги по дням за последние N дней: съедено, активность, норма, остаток, белок, вода."""
    out = []
    for k in range(days - 1, -1, -1):
        d = st.today() - timedelta(days=k)
        t = st.day_totals(d)
        out.append({"day": str(d), "eaten": round(t["eaten"]), "burned": round(t["burned"]),
                    "goal": report.goal(st, d), "remaining": report.remaining(st, d),
                    "protein": round(t["protein"]), "water_ml": round(report.hydration(st, d)["total"]), "entries": t["n"]})
    return out


@mcp.tool()
def weights() -> list[dict]:
    """Все взвешивания: день и вес в кг."""
    return [{"day": w["day"], "kg": w["kg"]} for w in st.weights()]


@mcp.tool()
def search_dishes(query: str = "") -> list[dict]:
    """Блюда из справочника (ккал и БЖУ на порцию). Пустой запрос — самые частые."""
    return [{k: d[k] for k in ("name", "kcal", "protein", "fat", "carbs")} for d in st.search_dishes(query, limit=20)]


@mcp.tool()
def real_expenditure() -> str:
    """Реальный расход по весу и записанной еде за 4 недели против формулы — или почему пока не посчитать."""
    return plain(report.format_adaptive(st, st.today()))


if __name__ == "__main__":
    mcp.run()
