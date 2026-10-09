"""Тексты сводок и xlsx."""
import html
import re
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

import profile_calc
from nutrition import Draft
from storage import Storage

LOW_AVG_KCAL = 1200
SHOW_PFC = False  # бжу в ответах бота; в базе и xlsx они есть всегда
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def goal(st: Storage, today: date | None = None) -> int:
    manual = st.get("goal_manual")
    if manual:
        return int(manual)
    today = today or date.today()
    return profile_calc.target(
        weight_kg=st.last_weight() or float(st.get("start_weight", 60)),
        height_cm=float(st.get("height_cm", 165)),
        age=today.year - int(st.get("birth_year", 1996)),
        sex=st.get("sex", "f"),
        activity=float(st.get("activity", 1.2)),
        deficit=float(st.get("deficit", 0.15)))


KCAL_PER_KG = 7700


def formula_tdee(st: Storage, today: date) -> float:
    """Расход без тренировок по формуле: базовый обмен × 1,2."""
    return profile_calc.bmr(st.last_weight() or float(st.get("start_weight", 60)),
                            float(st.get("height_cm", 165)), today.year - int(st.get("birth_year", 1996)),
                            st.get("sex", "f")) * float(st.get("activity", 1.2))


def adaptive(st: Storage, end: date, window_days: int = 28) -> dict:
    """Реальный расход по факту: сколько ела + как менялся вес (1 кг ≈ 7 700 ккал).
    {"ok": False, "why": ...}, пока данных мало: нужны ≥2 взвешивания через ≥14 дней
    и записанная еда хотя бы в 70% дней между ними (пропуски занижают «съедено»)."""
    start = end - timedelta(days=window_days - 1)
    ws = [(date.fromisoformat(w["day"]), w["kg"]) for w in st.weights()
          if start <= date.fromisoformat(w["day"]) <= end]
    if len(ws) < 2 or (ws[-1][0] - ws[0][0]).days < 14:
        have = (ws[-1][0] - ws[0][0]).days if len(ws) >= 2 else 0
        return {"ok": False, "why": f"нужны взвешивания хотя бы через 14 дней (сейчас {have} дн., "
                                    f"взвешиваний за 4 недели: {len(ws)})"}
    span = [ws[0][0] + timedelta(days=k) for k in range((ws[-1][0] - ws[0][0]).days + 1)]
    totals = [st.day_totals(d) for d in span]
    logged = [t for t in totals if t["eaten"] > 0]
    if len(logged) < 0.7 * len(span):
        return {"ok": False, "why": f"еда записана в {len(logged)} из {len(span)} дней, нужно хотя бы 70%"}
    # наклон веса методом наименьших квадратов: взвешиваний может быть несколько, берём тренд, а не две точки
    xs = [(d - ws[0][0]).days for d, _ in ws]
    mx, my = sum(xs) / len(xs), sum(k for _, k in ws) / len(ws)
    slope = sum((x - mx) * (k - my) for x, (_, k) in zip(xs, ws)) / sum((x - mx) ** 2 for x in xs)  # кг/день
    eaten = sum(t["eaten"] for t in logged) / len(logged)
    burned = sum(t["burned"] for t in logged) / len(logged)
    real = eaten - slope * KCAL_PER_KG
    expected = formula_tdee(st, end) + burned
    deficit = float(st.get("deficit", 0.15))
    return {"ok": True, "days": len(span), "logged": len(logged), "kg_per_week": slope * 7,
            "eaten": eaten, "burned": burned, "real": real, "expected": expected,
            "suggested": int(round(real * (1 - deficit) / 50) * 50)}


def format_adaptive(st: Storage, end: date) -> str:
    a = adaptive(st, end)
    if not a["ok"]:
        return f"реальный расход пока не посчитать: {a['why']}"
    diff = a["real"] - a["expected"]
    verdict = ("формула и активность почти совпадают с реальностью" if abs(diff) < 100 else
               f"по факту тратишь на {n(abs(diff))} ккал {'больше' if diff > 0 else 'меньше'}, чем по формуле с активностью")
    return (f"<b>реальный расход</b> за {a['days']} дн. (еда записана в {a['logged']})\n"
            f"ела в среднем {n(a['eaten'])}, вес {a['kg_per_week']:+.2f} кг в неделю\n"
            f"→ тратишь около <b>{n(a['real'])}</b> ккал в день\n"
            f"по формуле с активностью {n(a['expected'])}: {verdict}\n\n"
            f"норма с тем же дефицитом была бы {n(a['suggested'])} (сейчас {n(goal(st, end))}). "
            "поставить: <code>/goal adapt</code>")


# чай и кофе идут в воду частично: (как называется, доля воды, объём чашки, если в записи его нет)
WATER_FROM_DRINKS = [
    (r"псыж", 1.0, 200),
    (r"эспрессо|espresso", 0.8, 30),
    (r"кофе|капучино|латте|раф\b|американо|флэт|coffee", 0.8, 250),
    (r"\bча[йюя]\b|чаю|\btea\b", 0.9, 250),
]


def _drink_water(name: str, grams: float | None) -> float:
    for pattern, share, cup in WATER_FROM_DRINKS:
        if re.search(pattern, name, re.I):
            return share * (grams or cup)
    return 0.0


def hydration(st: Storage, day: date) -> dict:
    """Вода за день: выпитая вода + доля из чая и кофе (объём из записи, иначе стандартная чашка)."""
    from_drinks = 0.0
    for e in st.entries(day):
        if e["kind"] in ("workout", "watch"):
            continue
        items = st.db.execute("SELECT name, grams FROM entry_items WHERE entry_id = ?", (e["id"],)).fetchall()
        if items:  # «кускус 150г, чай 300г» — смотрим каждый продукт
            from_drinks += sum(_drink_water(i["name"], i["grams"]) for i in items)
        else:
            m = re.search(r"(\d+)\s*(?:г|мл|ml)\b", e["descr"])
            from_drinks += _drink_water(e["descr"], float(m.group(1)) if m else None)
    plain = st.water_on(day)
    return {"water": plain, "drinks": from_drinks, "total": plain + from_drinks}


def liters(ml: float) -> str:
    return f"{ml / 1000:.2f} л".replace(".", ",")


SWEET_DRINKS = ("лимонад", "санпел", "sanpell", "боржоми", "кола", "cola", "сок ", "спрайт", "фанта")


def day_factors(st: Storage, day: date) -> dict:
    """Что было в этот день: из этого /patterns ищет отличия дней с симптомами от обычных."""
    t = st.day_totals(day)
    food = [e for e in st.entries(day) if e["kind"] not in ("workout", "watch")]
    first = min((int(e["ts"][11:13]) + int(e["ts"][14:16]) / 60 for e in food
                 if int(e["ts"][11:13]) >= st.day_start_hour), default=None)
    late = sum(e["kcal"] for e in food if int(e["ts"][11:13]) >= 21 or int(e["ts"][11:13]) < st.day_start_hour)
    drinks = sum(e["kcal"] for e in food if any(w in e["descr"].lower() for w in SWEET_DRINKS))
    return {"сладкие напитки, ккал": drinks, "первая еда, час": first, "вода, л": hydration(st, day)["total"] / 1000,
            "углеводы, г": t["carbs"], "белок, г": t["protein"], "всего съедено, ккал": t["eaten"],
            "после 21:00, ккал": late, "активность, ккал": t["burned"]}


def patterns(st: Storage, end: date, days: int = 42) -> dict:
    """Сравнивает дни с симптомами и дни «норм». Нужно ≥10 отмеченных дней, из них ≥3 с симптомами и ≥3 «норм»."""
    labeled = []
    for k in range(days):
        d = end - timedelta(days=k)
        s = st.symptoms_on(d)
        if s and st.day_totals(d)["eaten"] > 0:
            labeled.append((d, s))
    bad = [d for d, s in labeled if s - {"норм"}]
    ok = [d for d, s in labeled if s == {"норм"}]
    if len(labeled) < 10 or len(bad) < 3 or len(ok) < 3:
        return {"ok": False, "why": f"отмечено дней: {len(labeled)} (нужно 10), с симптомами {len(bad)}, "
                                    f"нормальных {len(ok)} (нужно хотя бы по 3)"}
    fb, fo = [day_factors(st, d) for d in bad], [day_factors(st, d) for d in ok]
    rows = []
    for name in fb[0]:
        a = [f[name] for f in fb if f[name] is not None]
        b = [f[name] for f in fo if f[name] is not None]
        if len(a) < 2 or len(b) < 2:
            continue
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        var = (sum((x - ma) ** 2 for x in a) + sum((x - mb) ** 2 for x in b)) / max(1, len(a) + len(b) - 2)
        effect = (ma - mb) / var ** 0.5 if var > 0 else 0  # насколько различие больше обычного разброса
        rows.append({"factor": name, "bad": ma, "ok": mb, "effect": effect})
    rows.sort(key=lambda r: -abs(r["effect"]))
    kinds = {}
    for _, s in labeled:
        for k in s - {"норм"}:
            kinds[k] = kinds.get(k, 0) + 1
    return {"ok": True, "labeled": len(labeled), "bad": len(bad), "good": len(ok), "kinds": kinds, "rows": rows}


def format_patterns(st: Storage, end: date) -> str:
    p = patterns(st, end)
    if not p["ok"]:
        return ("закономерности пока не посчитать: " + p["why"] + "\n"
                "отмечай самочувствие кнопками в вечернем итоге, через пару недель хватит")
    fmt = lambda name, v: f"{int(v)}:{int(round(v % 1 * 60)):02d}" if "час" in name else (
        f"{v:.2f}".replace(".", ",") if name.endswith(", л") else n(v))
    lines = []
    for r in p["rows"][:5]:
        strength = "заметно" if abs(r["effect"]) >= 0.8 else "немного" if abs(r["effect"]) >= 0.4 else "почти не"
        lines.append(f"• {r['factor']}: {fmt(r['factor'], r['bad'])} против {fmt(r['factor'], r['ok'])}  ({strength} отличается)")
    kinds = ", ".join(f"{k} {v}" for k, v in p["kinds"].items())
    return (f"<b>самочувствие</b> за {p['labeled']} отмеченных дней: с симптомами {p['bad']} ({kinds}), норм {p['good']}\n\n"
            "в дни с симптомами против обычных:\n" + "\n".join(lines) +
            "\n\nэто наблюдения, а не причины: дней мало, и всё связано со всем. "
            "но с этим удобно идти к врачу")


def remaining(st: Storage, day: date) -> int:
    t = st.day_totals(day)
    return round(goal(st, day) - t["eaten"] + t["burned"])


def esc(s: str) -> str:
    """Текст из баз и от пользователя -> безопасный для Telegram HTML."""
    return html.escape(html.unescape(str(s)), quote=False)


def n(x: float) -> str:
    return f"{round(x):,}".replace(",", " ")


def left_line(st: Storage, day: date) -> str:
    left = remaining(st, day)
    return f"осталось {n(left)} ккал" if left >= 0 else f"перебор {n(-left)} ккал"


def format_draft(d: Draft, st: Storage, day: date) -> str:
    lines = []
    for i in d.items:
        if i.per100:
            size = "порция" if i.portion else f"{n(i.grams)} г"
            lines.append(f"• {esc(i.name)} {size} · {n(i.kcal)} ккал  <i>({esc(i.per100.match)})</i>")
        else:
            lines.append(f"• {esc(i.name)} {n(i.grams)} г · ❓")
    total = d.total()
    pfc = f" · бжу {n(d.total('protein'))}/{n(d.total('fat'))}/{n(d.total('carbs'))}" if SHOW_PFC else ""
    after = remaining(st, day) - total
    tail = f"останется {n(after)}" if after >= 0 else f"перебор {n(-after)}"
    return "\n".join(lines) + f"\n\n<b>итого {n(total)} ккал</b>{pfc}\nпосле этого {tail}"


def format_day(st: Storage, day: date, title: str = "сегодня") -> str:
    t = st.day_totals(day)
    g = goal(st, day)
    rows = []
    for e in st.entries(day):
        if e["kind"] == "watch":
            extra = max(e["kcal"] - st.active_baseline, 0)
            rows.append(f"⌚ часы: {n(e['kcal'])} активных, сверх обычных {n(extra)}")
            continue
        sign = "−" if e["kind"] == "workout" else ""
        rows.append(f"{e['ts'][11:16]}  {sign}{n(e['kcal'])}  {esc(e['descr'])}")
    body = "\n".join(rows) or "пока пусто"
    summary = [f"съедено {n(t['eaten'])} · тренировки +{n(t['burned'])} · норма {n(g)}",
               f"бжу {n(t['protein'])}/{n(t['fat'])}/{n(t['carbs'])}" if SHOW_PFC else "",
               water_line(st, day), f"<b>{left_line(st, day)}</b>"]
    return f"<b>{title}</b>\n{body}\n\n" + "\n".join(s for s in summary if s)


def water_line(st: Storage, day: date) -> str:
    h = hydration(st, day)
    if not h["total"]:
        return ""
    tea = f" (из них чай и кофе {liters(h['drinks'])})" if h["drinks"] else ""
    return f"💧 {liters(h['total'])}{tea}"


def format_week(st: Storage, end: date) -> str:
    start = end - timedelta(days=6)
    lines, eaten_days = [], []
    for k in range(7):
        d = start + timedelta(days=k)
        t = st.day_totals(d)
        if not t["n"]:
            lines.append(f"{WEEKDAYS[d.weekday()]} {d:%d.%m}  ·")
            continue
        left = remaining(st, d)
        mark = "✅" if left >= 0 else "🔺"
        burn = f" +{n(t['burned'])}" if t["burned"] else ""
        lines.append(f"{WEEKDAYS[d.weekday()]} {d:%d.%m}  {n(t['eaten'])}{burn}  {mark} {n(left)}")
        if t["eaten"]:
            eaten_days.append(t["eaten"])
    text = f"<b>неделя {start:%d.%m}-{end:%d.%m}</b>\n<pre>" + "\n".join(lines) + "</pre>"
    if eaten_days:
        avg = sum(eaten_days) / len(eaten_days)
        text += f"\nв среднем {n(avg)} ккал в день при норме {n(goal(st, end))}"
        if avg < LOW_AVG_KCAL:
            text += (f"\n\n💛 средняя ниже {LOW_AVG_KCAL}, это уже жёсткий дефицит. "
                     "если так неделями, стоит добавить немного еды")
    w = st.weights()
    if len(w) >= 2:
        text += f"\nвес: {w[0]['kg']} → {w[-1]['kg']} кг"
    a = adaptive(st, end)
    if a["ok"]:
        text += f"\nреальный расход около {n(a['real'])} ккал в день, подробнее /tdee"
    return text


# цвета из эталонной палитры dataviz: одна серия — slot 1, текст — токены текста, не цвет серии
C_SURFACE, C_TEXT, C_MUTED, C_GRID, C_SERIES = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e0", "#2a78d6"


def week_chart(st: Storage, end: date) -> bytes:
    """PNG: съедено по дням (столбцы) против «можно сегодня» = норма + активность (пунктир).
    Вес — отдельной панелью снизу, если записей хотя бы две (никаких двух шкал на одном графике)."""
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates
    import matplotlib.pyplot as plt

    days = [end - timedelta(days=6 - k) for k in range(7)]
    totals = [st.day_totals(d) for d in days]
    eaten = [t["eaten"] for t in totals]
    allowed = [goal(st, d) + t["burned"] for d, t in zip(days, totals)]
    weights = [w for w in st.weights() if days[0] - timedelta(days=21) <= date.fromisoformat(w["day"]) <= end]

    rows = 2 if len(weights) >= 2 else 1
    fig, axes = plt.subplots(rows, 1, figsize=(7, 4.2 if rows == 1 else 6.2), dpi=150,
                             gridspec_kw={"height_ratios": [3, 1.4][:rows]}, facecolor=C_SURFACE)
    axes = [axes] if rows == 1 else list(axes)
    ax = axes[0]
    xs = list(range(7))
    ax.bar(xs, eaten, width=0.56, color=C_SERIES, zorder=2)
    ax.step([-0.5, *xs, 6.5], [allowed[0], *allowed, allowed[-1]], where="mid",
            color=C_MUTED, linewidth=2, linestyle=(0, (4, 3)), zorder=3)
    for x, e, a in zip(xs, eaten, allowed):
        if e > a:
            ax.text(x, e + 25, f"+{n(e - a)}", ha="center", va="bottom", fontsize=9, color=C_TEXT)
    ax.text(6.55, allowed[-1], "можно\nс активностью", va="center", ha="left", fontsize=8, color=C_MUTED)
    ax.set_xticks(xs, [f"{WEEKDAYS[d.weekday()]}\n{d:%d.%m}" for d in days])
    days_with_food = sum(1 for e in eaten if e) or 1
    ax.set_title(f"съедено за неделю, ккал · в среднем {n(sum(eaten) / days_with_food)}",
                 loc="left", fontsize=11, color=C_TEXT)

    if rows == 2:
        wa = axes[1]
        wd = [date.fromisoformat(w["day"]) for w in weights]
        wa.plot(wd, [w["kg"] for w in weights], color=C_SERIES, linewidth=2, marker="o", markersize=5, zorder=2)
        wa.set_title("вес, кг", loc="left", fontsize=10, color=C_TEXT)
        wa.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d.%m"))
        wa.text(wd[-1], weights[-1]["kg"], f"  {weights[-1]['kg']}", va="center", fontsize=9, color=C_TEXT)

    for a in axes:
        a.set_facecolor(C_SURFACE)
        a.grid(axis="y", color=C_GRID, linewidth=0.8, zorder=0)
        a.tick_params(colors=C_MUTED, labelsize=8, length=0)
        for side in ("top", "right", "left"):
            a.spines[side].set_visible(False)
        a.spines["bottom"].set_color(C_GRID)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=C_SURFACE)
    plt.close(fig)
    return buf.getvalue()


def build_xlsx(st: Storage, path: Path) -> Path:
    wb = Workbook()
    bold = Font(bold=True)

    ws = wb.active
    ws.title = "Журнал"
    ws.append(["Дата", "Время", "Тип", "Описание", "Ккал", "Белки", "Жиры", "Углеводы", "Источник"])
    kinds = {"meal": "еда", "quick": "еда", "workout": "тренировка", "watch": "часы за день"}
    for e in st.all_entries():
        kcal = -e["kcal"] if e["kind"] in ("workout", "watch") else e["kcal"]
        ws.append([e["day"], e["ts"][11:16], kinds[e["kind"]], e["descr"], round(kcal),
                   *(round(e[k], 1) if e[k] is not None else None for k in ("protein", "fat", "carbs")),
                   e["source"]])

    days = sorted({e["day"] for e in st.all_entries()})
    ws2 = wb.create_sheet("Дни")
    ws2.append(["Дата", "Съедено", "Тренировки", "Норма", "Остаток", "Белки", "Жиры", "Углеводы"])
    for d in days:
        dd = date.fromisoformat(d)
        t = st.day_totals(dd)
        ws2.append([d, round(t["eaten"]), round(t["burned"]), goal(st, dd), remaining(st, dd),
                    round(t["protein"]), round(t["fat"]), round(t["carbs"])])

    ws3 = wb.create_sheet("Вес")
    ws3.append(["Дата", "Вес, кг"])
    for w in st.weights():
        ws3.append([w["day"], w["kg"]])

    for sheet in wb.worksheets:
        for c in sheet[1]:
            c.font = bold
        sheet.column_dimensions["A"].width = 12
    ws.column_dimensions["D"].width = 40
    wb.save(path)
    return path
