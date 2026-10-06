"""
eat-burn: телеграм-бот подсчёта калорий с дневной нормой и остатком.

Как писать боту:
    450 рацион обед           еда числом, записывается сразу
    -200 бег                  тренировка, возвращает ккал в остаток
    овсянка 60г, банан, мёд   локальная модель разберёт, бот посчитает по базе и спросит ✅
    фото тарелки              то же, подпись к фото — подсказка («гречка 200г»)

Команды: /today, /week, /undo, /weight 57.5, /goal 1300 | auto, /food чак-чак 450, /file
Каждый вечер в 22:00 итог дня, по воскресеньям в 21:00 — неделя и xlsx.
"""
import asyncio
import logging
import re
from datetime import date, datetime, time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import report
from llm import LLM, LLMError
from nutrition import Draft, Nutrition
from parser import parse_edit_lines, parse_quick
from storage import Storage

import config

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("eat-burn")

st = Storage(str(DATA / "eatburn.db"))
nutr = Nutrition(st, DATA / "usda.db")
llm = LLM(getattr(config, "TEXT_MODEL", "qwen2.5:7b"), getattr(config, "VISION_MODEL", "gemma3:4b"))

for key in ("height_cm", "birth_year", "start_weight", "sex"):
    if st.get(key) is None and hasattr(config, key.upper()):
        st.set(key, getattr(config, key.upper()))

HELP = """Пиши, что съела или сожгла:

<code>450 рацион обед</code> — ккал числом, запишу сразу
<code>-200 бег</code> — тренировка, вернёт ккал в остаток
<code>овсянка 60г, банан, ложка мёда</code> — посчитаю по базе и спрошу ✅
📷 фото тарелки — то же, подпись к фото поможет («гречка 200г»)

Граммы пиши с «г»: <code>творог 150г</code>. Просто <code>150 творог</code> — это 150 ккал.

/today — сегодня и остаток
/week — неделя по дням
/undo — удалить последнюю запись
/weight 57.5 — записать вес, норма пересчитается
/goal 1300 — своя норма (/goal auto — снова по формуле)
/food чак-чак 450 — ккал на 100 г в мой справочник
/dishes — мои блюда: <code>450 цезарь жан-жак</code> запоминается, потом хватит <code>цезарь жан-жак</code>
/file — табличка"""

KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Записать", callback_data="ok"),
    InlineKeyboardButton("✏️ Править", callback_data="edit"),
    InlineKeyboardButton("❌", callback_data="no"),
]])


def owner_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id == config.OWNER_ID:
            return await fn(update, context)
    return wrapper


async def reply(update: Update, text: str, **kw):
    return await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, **kw)


# --- черновики приёма пищи ---

async def show_draft(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: Draft):
    """Если какого-то продукта нет в базах — сначала спрашиваем ккал на 100 г, потом показываем черновик."""
    ud = context.user_data
    ud["draft"] = draft
    missing = draft.missing()
    if missing:
        ud["awaiting"] = "per100"
        await reply(update, f"Не нашла «{report.esc(missing[0].name)}». Сколько в нём ккал на 100 г? "
                            "Напиши число — запомню.")
        return
    ud["awaiting"] = None
    msg = await reply(update, report.format_draft(draft, st, date.today()), reply_markup=KEYBOARD)
    ud["draft_msg"] = msg.message_id


async def parse_and_show(update, context, fn, *args):
    await update.effective_chat.send_action("typing")
    try:
        draft = await asyncio.to_thread(fn, *args)
    except LLMError as e:
        log.warning("llm: %s", e)
        await reply(update, "Модель сейчас не справилась 😕 Напиши ккал числом: <code>350 овсянка</code>")
        return
    await asyncio.to_thread(nutr.fill, draft)
    await show_draft(update, context, draft)


def quick_edit(draft: Draft, text: str) -> bool:
    """«банан 150», «-мёд» без модели. Если хоть одна строка не такая — ничего не трогаем, правку разберёт модель."""
    edits = []
    for name, grams, delete in parse_edit_lines(text):
        item = next((i for i in draft.items if i.name == name or i.name.startswith(name + " ")), None)
        if item is None or not (delete or grams):
            return False
        edits.append((item, grams, delete))
    if not edits or len(edits) != len([s for s in re.split(r"[\n,;]+", text) if s.strip()]):
        return False
    for item, grams, delete in edits:
        if delete:
            draft.items.remove(item)
        else:
            item.grams = grams
    return True


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != config.OWNER_ID:
        return
    ud = context.user_data
    if q.data.startswith("amb_"):
        amb = ud.pop("amb", None)
        await q.answer()
        await q.edit_message_reply_markup(None)
        if amb is None:
            return
        if q.data == "amb_kcal":
            st.add_entry("quick", amb["desc"], amb["kcal"], "manual")
            st.put_dish(amb["desc"], amb["kcal"])
            await reply(update, f"👌 {amb['kcal']} ккал. {report.left_line(st, date.today())}")
        else:
            await parse_and_show(update, context, llm.parse_text, amb["text"])
        return
    if ud.get("dish") and q.message.message_id == ud.get("draft_msg"):
        await on_dish_callback(update, context)
        return
    draft: Draft | None = ud.get("draft")
    if draft is None or q.message.message_id != ud.get("draft_msg"):
        await q.answer("Этот черновик уже неактуален")
        await q.edit_message_reply_markup(None)
        return
    await q.answer()
    if q.data == "ok":
        descr = ", ".join(f"{i.name} {round(i.grams)}г" for i in draft.items)
        st.add_entry("meal", descr, draft.total(), draft.source,
                     draft.total("protein"), draft.total("fat"), draft.total("carbs"),
                     [i.as_row() for i in draft.items])
        ud.pop("draft"), ud.pop("draft_msg")
        await q.edit_message_text(q.message.text_html + f"\n\n👌 Записала. <b>{report.left_line(st, date.today())}</b>",
                                  parse_mode=ParseMode.HTML)
    elif q.data == "edit":
        ud["awaiting"] = "edit"
        await q.edit_message_reply_markup(None)
        await reply(update, "Напиши, что поменять, можно своими словами:\n"
                            "<code>банан 150</code> · <code>-мёд</code> · <code>добавь сыр 30г</code>\n"
                            "<code>там 14 ккал на 100 мл, выпила 25 г</code>")
    elif q.data == "no":
        ud.pop("draft"), ud.pop("draft_msg")
        await q.edit_message_text(q.message.text_html + "\n\n❌ Не записала", parse_mode=ParseMode.HTML)


# --- блюда на порцию: «450 цезарь жан-жак» запоминается, потом хватает «цезарь жан-жак» ---

async def show_dish(update: Update, context: ContextTypes.DEFAULT_TYPE, dish):
    ud = context.user_data
    ud.pop("draft", None)
    ud["dish"] = {"name": dish["name"], "kcal": dish["kcal"]}
    ud["awaiting"] = "dish"
    after = report.remaining(st, date.today()) - dish["kcal"]
    tail = f"останется {report.n(after)}" if after >= 0 else f"перебор {report.n(-after)}"
    msg = await reply(update, f"{report.esc(dish['name'])} · <b>{report.n(dish['kcal'])} ккал</b>, как в прошлый раз\n"
                              f"После этого {tail}\n\nДругая цифра? Просто пришли число.",
                      reply_markup=KEYBOARD)
    ud["draft_msg"] = msg.message_id


def record_dish(context: ContextTypes.DEFAULT_TYPE, kcal: float) -> str:
    ud = context.user_data
    dish = ud.pop("dish")
    ud.pop("draft_msg", None)
    ud["awaiting"] = None
    st.add_entry("quick", dish["name"], kcal, "manual")
    if kcal != dish["kcal"]:
        st.put_dish(dish["name"], kcal)
    return f"👌 Записала {report.esc(dish['name'])} {report.n(kcal)} ккал. <b>{report.left_line(st, date.today())}</b>"


async def on_dish_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    ud = context.user_data
    await q.answer()
    if q.data == "ok":
        await q.edit_message_text(record_dish(context, ud["dish"]["kcal"]), parse_mode=ParseMode.HTML)
    elif q.data == "edit":
        await q.edit_message_reply_markup(None)
        await reply(update, "Сколько ккал в этот раз? Пришли число.")
    elif q.data == "no":
        ud.pop("dish"), ud.pop("draft_msg")
        ud["awaiting"] = None
        await q.edit_message_text(q.message.text_html + "\n\n❌ Не записала", parse_mode=ParseMode.HTML)


# --- сообщения ---

@owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ud = context.user_data
    awaiting = ud.get("awaiting")

    if awaiting == "per100" and ud.get("draft"):
        m = re.match(r"^\s*(\d+(?:[.,]\d+)?)", text)
        if m:
            item = ud["draft"].missing()[0]
            kcal = float(m.group(1).replace(",", "."))
            st.put_food(item.name, kcal)
            await asyncio.to_thread(nutr.fill, ud["draft"])
            await show_draft(update, context, ud["draft"])
            return
        ud["awaiting"] = None  # прислали что-то другое — забываем черновик и обрабатываем как новое

    if awaiting == "dish" and ud.get("dish"):
        m = re.fullmatch(r"\s*(\d{1,2}\s\d{3}|\d+)\s*(?:ккал|kcal)?\s*", text)
        if m:
            await reply(update, record_dish(context, float(m.group(1).replace(" ", ""))))
            return
        ud.pop("dish"), ud.pop("draft_msg", None)  # прислали что-то другое — блюдо не записываем
        ud["awaiting"] = None

    if awaiting == "edit" and ud.get("draft"):
        draft: Draft = ud["draft"]
        if not quick_edit(draft, text):
            await update.effective_chat.send_action("typing")
            try:
                draft = await asyncio.to_thread(llm.edit, draft, text)
            except LLMError as e:
                log.warning("llm edit: %s", e)
                await reply(update, "Не поняла правку 😕 Попробуй так: <code>сироп 25</code>, "
                                    "а калорийность — <code>/food л-карнитин сироп 14</code>")
                return
            for item in draft.items:  # названную калорийность запоминаем на будущее
                if item.per100 and item.per100.match == "со слов":
                    st.put_food(item.name, item.per100.kcal)
                    item.per100.match = "со слов, запомнила"
        await asyncio.to_thread(nutr.fill, draft)
        await show_draft(update, context, draft)
        return

    quick = parse_quick(text)
    if quick and quick.kind == "ambiguous":
        ud["awaiting"] = None
        ud["amb"] = {"kcal": quick.kcal, "desc": quick.desc, "text": text}
        await reply(update, f"«{report.esc(text)}» — это {quick.kcal} ккал или {quick.kcal} шт?",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(f"{quick.kcal} ккал", callback_data="amb_kcal"),
                        InlineKeyboardButton(f"{quick.kcal} шт — посчитать", callback_data="amb_count")]]))
        return
    if quick:
        ud["awaiting"] = None
        st.add_entry(quick.kind, quick.desc, quick.kcal, "manual")
        if quick.kind == "quick" and quick.desc != "еда":
            st.put_dish(quick.desc, quick.kcal)
        await update.message.set_reaction("👌")
        await reply(update, report.left_line(st, date.today()))
        return

    dish = st.get_dish(text)
    if dish:
        await show_dish(update, context, dish)
        return

    await parse_and_show(update, context, llm.parse_text, text)


@owner_only
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = await update.message.photo[-1].get_file()
    image = bytes(await photo.download_as_bytearray())
    await parse_and_show(update, context, llm.parse_photo, image, update.message.caption or "")


# --- команды ---

@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, f"Норма сейчас {report.goal(st)} ккал в день.\n\n" + HELP)


@owner_only
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, report.format_day(st, date.today()))


@owner_only
async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, report.format_week(st, date.today()))


@owner_only
async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = st.delete_last()
    if not row:
        await reply(update, "Удалять нечего")
        return
    await reply(update, f"Удалила: {row['day']} {report.esc(row['descr'])} ({round(row['kcal'])} ккал)\n"
                        f"{report.left_line(st, date.today())}")


@owner_only
async def cmd_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        kg = float(context.args[0].replace(",", "."))
    except (IndexError, ValueError):
        w = st.last_weight()
        await reply(update, f"Напиши так: <code>/weight 57.5</code>" + (f"\nПоследний вес {w} кг" if w else ""))
        return
    st.add_weight(kg)
    note = " (своя норма, /goal auto — пересчитать по весу)" if st.get("goal_manual") else ""
    await reply(update, f"Записала {kg} кг. Норма {report.goal(st)} ккал{note}")


@owner_only
async def cmd_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = context.args[0] if context.args else ""
    if arg == "auto":
        st.set("goal_manual", "")
    elif arg.isdigit():
        st.set("goal_manual", int(arg))
    else:
        await reply(update, f"Норма {report.goal(st)} ккал. Поменять: <code>/goal 1300</code> или <code>/goal auto</code>")
        return
    await reply(update, f"Норма {report.goal(st)} ккал. {report.left_line(st, date.today())}")


@owner_only
async def cmd_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.match(r"^(.+?)\s+(\d+(?:[.,]\d+)?)$", " ".join(context.args))
    if not m:
        await reply(update, "Напиши так: <code>/food чак-чак 450</code> (ккал на 100 г)")
        return
    st.put_food(m.group(1), float(m.group(2).replace(",", ".")))
    await reply(update, f"Запомнила: {report.esc(m.group(1).lower())} — {m.group(2)} ккал на 100 г")


@owner_only
async def cmd_dishes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = st.dishes()
    if not rows:
        await reply(update, "Пока пусто. Блюда запоминаются, когда пишешь с цифрой: <code>450 цезарь жан-жак</code>")
        return
    await reply(update, "<b>Мои блюда</b> (ккал на порцию)\n" +
                "\n".join(f"{report.n(r['kcal'])}  {report.esc(r['name'])}" for r in rows) +
                "\n\nУдалить: <code>/dish_del название</code>")


@owner_only
async def cmd_dish_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = " ".join(context.args)
    if name and st.delete_dish(name):
        await reply(update, f"Удалила «{report.esc(name)}»")
    else:
        await reply(update, "Не нашла такое блюдо, список — /dishes")


async def send_file(bot, chat_id: int):
    path = report.build_xlsx(st, DATA / f"eat-burn-{date.today()}.xlsx")
    with open(path, "rb") as f:
        await bot.send_document(chat_id, f, filename=path.name)


@owner_only
async def cmd_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_file(context.bot, update.effective_chat.id)


# --- расписание ---

async def job_daily(context: ContextTypes.DEFAULT_TYPE):
    if st.day_totals(date.today())["n"]:
        await context.bot.send_message(config.OWNER_ID, report.format_day(st, date.today(), "Итог дня"),
                                       parse_mode=ParseMode.HTML)


async def job_weekly(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(config.OWNER_ID, report.format_week(st, date.today()),
                                   parse_mode=ParseMode.HTML)
    await send_file(context.bot, config.OWNER_ID)


def main():
    app = Application.builder().token(config.BOT_TOKEN).build()
    for name, fn in [("start", cmd_start), ("help", cmd_start), ("today", cmd_today), ("week", cmd_week),
                     ("undo", cmd_undo), ("weight", cmd_weight), ("goal", cmd_goal), ("food", cmd_food), ("dishes", cmd_dishes), ("dish_del", cmd_dish_del),
                     ("file", cmd_file)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    tz = datetime.now().astimezone().tzinfo
    app.job_queue.run_daily(job_daily, time(22, 0, tzinfo=tz))
    app.job_queue.run_daily(job_weekly, time(21, 0, tzinfo=tz), days=(0,))  # 0 = воскресенье в PTB
    log.info("eat-burn запущен, норма %s ккал", report.goal(st))
    app.run_polling()


if __name__ == "__main__":
    main()
