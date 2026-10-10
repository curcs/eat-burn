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
import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import report
import products
import screens
from llm import LLM, LLMError
from nutrition import Draft, Nutrition, fix_generic_coffee
from parser import parse_edit_lines, parse_quick
from storage import Storage, norm

import config

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)


def add_homebrew_to_path():
    """launchd запускает бота с урезанным PATH: без Homebrew нет tesseract (скрины, этикетки) и ffmpeg (голосовые)."""
    for folder in ("/opt/homebrew/bin", "/usr/local/bin"):
        if folder not in os.environ.get("PATH", "").split(":"):
            os.environ["PATH"] = os.environ.get("PATH", "") + ":" + folder


add_homebrew_to_path()

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("eat-burn")

report.SHOW_PFC = getattr(config, "SHOW_PFC", False)
st = Storage(str(DATA / "eatburn.db"), getattr(config, "DAY_START_HOUR", 4),
             getattr(config, "ACTIVE_BASELINE", 200))
nutr = Nutrition(st, DATA / "usda.db")
llm = LLM(getattr(config, "TEXT_MODEL", "qwen2.5:7b"), getattr(config, "VISION_MODEL", "gemma3:4b"))

for key in ("height_cm", "birth_year", "start_weight", "sex"):
    if st.get(key) is None and hasattr(config, key.upper()):
        st.set(key, getattr(config, key.upper()))

HELP = """пиши, что съела или сожгла:

<code>450 рацион обед</code>: ккал числом, запишу сразу
<code>-200 бег</code>: тренировка, верну ккал в остаток
<code>овсянка 60г, банан, ложка мёда</code>: посчитаю по базе и спрошу ✅
📷 фото тарелки: то же самое, подпись к фото поможет («гречка 200г»)
📷 скрин или бумажное меню рациона: прочитаю калории
📷 штрихкод или этикетка «пищевая ценность»: посчитаю по граммам. штрихкод снимай крупно, а если не читается, отправь фото файлом
<code>часы 412</code>: активные ккал за день с apple watch
<code>стакан воды</code>, <code>вода 500</code>: 💧 вода; чай и кофе считаю в воду сами (90% и 80%)

граммы пиши с «г»: <code>творог 150г</code>. просто <code>150 творог</code> я пойму как 150 ккал

/today: сегодня и остаток, у каждой записи ✏️ и 🗑
/week: неделя по дням и график
/undo: удалить последнюю запись
/weight 57.5: записать вес, норма пересчитается
/goal 1300: своя норма (/goal auto вернёт расчёт по формуле)
/food чак-чак 450: ккал на 100 г в мой справочник
/f лимонад: найти блюдо в справочнике и записать в одно нажатие (/f без слов: самое частое)
/dishes: мои блюда. <code>450 цезарь жан-жак</code> запоминается, потом хватит <code>цезарь жан-жак</code>
<code>болит голова</code>, <code>давление</code>, <code>слабость</code>: запишу самочувствие
🎙 голосовое: расшифрую и запишу как текст
/patterns: что отличает дни с головной болью от обычных
/tdee: реальный расход по весу и еде (нужно 2+ недели взвешиваний)
/remind: напоминания про вес и еду (/remind off выключить)
/file: табличка"""


KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ записать", callback_data="ok"),
    InlineKeyboardButton("✏️ поправить", callback_data="edit"),
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
        await reply(update, f"не нашла «{report.esc(missing[0].name)}». сколько в нём ккал на 100 г? "
                            "напиши число, я запомню")
        return
    ud["awaiting"] = None
    eaten = st.eaten_names_on(st.today())
    dups = [i.name for i in draft.items if norm(i.name) in eaten]
    warn = (f"\n\n⚠️ уже записано сегодня: {report.esc(', '.join(dups))}. если это повтор, убери через ✏️"
            if dups else "")
    msg = await reply(update, report.format_draft(draft, st, st.today()) + warn, reply_markup=KEYBOARD)
    ud["draft_msg"] = msg.message_id


async def parse_and_show(update, context, fn, *args):
    await update.effective_chat.send_action("typing")
    try:
        draft = await asyncio.to_thread(fn, *args)
        if fn == llm.parse_text:
            fix_generic_coffee(draft, args[0])
    except LLMError as e:
        log.warning("llm: %s", e)
        await reply(update, "модель сейчас не справилась 😕 напиши ккал числом: <code>350 овсянка</code>")
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
    if q.data.startswith("sl"):
        await on_list_callback(update, context)
        return
    if q.data.startswith("fd:"):
        await on_find_callback(update, context)
        return
    if q.data.startswith("pr:"):
        await on_product_callback(update, context)
        return
    if q.data.startswith(("ed:", "dl:")) or q.data == "un":
        await on_entry_callback(update, context)
        return
    if q.data.startswith("sy:"):
        await on_symptom_callback(update, context)
        return
    if q.data.startswith("wt"):
        ml = float(q.data[3:]) if q.data.startswith("wt:") else GLASS_ML
        st.add_water(ml)
        await q.answer(f"💧 +{report.n(ml)} мл, {water_today_text()}")
        return
    if q.data.startswith("amb_"):
        amb = ud.pop("amb", None)
        await q.answer()
        await q.edit_message_reply_markup(None)
        if amb is None:
            return
        if q.data == "amb_kcal":
            st.add_entry("quick", amb["desc"], amb["kcal"], "manual")
            st.put_dish(amb["desc"], amb["kcal"])
            await reply(update, f"👌 {amb['kcal']} ккал. {report.left_line(st, st.today())}")
        else:
            await parse_and_show(update, context, llm.parse_text, amb["text"])
        return
    if ud.get("dish") and q.message.message_id == ud.get("draft_msg"):
        await on_dish_callback(update, context)
        return
    draft: Draft | None = ud.get("draft")
    if draft is None or q.message.message_id != ud.get("draft_msg"):
        await q.answer("этот черновик уже неактуален")
        await q.edit_message_reply_markup(None)
        return
    await q.answer()
    if q.data == "ok":
        descr = ", ".join(i.name if i.portion else f"{i.name} {round(i.grams)}г" for i in draft.items)
        st.add_entry("meal", descr, draft.total(), draft.source,
                     draft.total("protein"), draft.total("fat"), draft.total("carbs"),
                     [i.as_row() for i in draft.items])
        ud.pop("draft"), ud.pop("draft_msg")
        await q.edit_message_text(q.message.text_html + f"\n\n👌 записала. <b>{report.left_line(st, st.today())}</b>",
                                  parse_mode=ParseMode.HTML)
    elif q.data == "edit":
        ud["awaiting"] = "edit"
        await q.edit_message_reply_markup(None)
        await reply(update, "напиши, что поменять, можно своими словами:\n"
                            "<code>банан 150</code> · <code>-мёд</code> · <code>добавь сыр 30г</code>\n"
                            "<code>там 14 ккал на 100 мл, выпила 25 г</code>")
    elif q.data == "no":
        ud.pop("draft"), ud.pop("draft_msg")
        await q.edit_message_text(q.message.text_html + "\n\n❌ не записала", parse_mode=ParseMode.HTML)


# --- блюда на порцию: «450 цезарь жан-жак» запоминается, потом хватает «цезарь жан-жак» ---

def pfc_line(d) -> str:
    """« · бжу 24/6/12», если БЖУ известны и их показ включён (SHOW_PFC в config.py)."""
    if not report.SHOW_PFC or d.get("protein") is None:
        return ""
    return f" · бжу {report.n(d['protein'])}/{report.n(d['fat'])}/{report.n(d['carbs'])}"


def dish_dict(row) -> dict:
    return {k: row[k] for k in ("name", "kcal", "protein", "fat", "carbs")}


async def show_dish(update: Update, context: ContextTypes.DEFAULT_TYPE, dish: dict, note: str = "как в прошлый раз"):
    ud = context.user_data
    ud.pop("draft", None)
    ud["dish"] = dish
    ud["awaiting"] = "dish"
    tail = report.balance_text(st, st.today(), extra=dish["kcal"]).replace("осталось", "останется")
    msg = await reply(update, f"{report.esc(dish['name'])} · <b>{report.n(dish['kcal'])} ккал</b>{pfc_line(dish)}, {note}\n"
                              f"после этого: {tail}\n\nдругая цифра? просто пришли число",
                      reply_markup=KEYBOARD)
    ud["draft_msg"] = msg.message_id


def record_dish(context: ContextTypes.DEFAULT_TYPE, kcal: float, source: str = "manual") -> str:
    ud = context.user_data
    dish = ud.pop("dish")
    ud.pop("draft_msg", None)
    ud["awaiting"] = None
    if kcal != dish["kcal"]:  # другая цифра — БЖУ от старой уже не подходят
        dish.update(kcal=kcal, protein=None, fat=None, carbs=None)
        st.put_dish(dish["name"], kcal)
    st.add_entry("quick", dish["name"], kcal, source, dish.get("protein"), dish.get("fat"), dish.get("carbs"))
    return (f"👌 записала {report.esc(dish['name'])} {report.n(kcal)} ккал{pfc_line(dish)}\n"
            f"<b>{report.left_line(st, st.today())}</b>")


async def on_dish_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    ud = context.user_data
    await q.answer()
    if q.data == "ok":
        await q.edit_message_text(record_dish(context, ud["dish"]["kcal"]), parse_mode=ParseMode.HTML)
    elif q.data == "edit":
        await q.edit_message_reply_markup(None)
        await reply(update, "сколько ккал в этот раз? пришли число")
    elif q.data == "no":
        ud.pop("dish"), ud.pop("draft_msg")
        ud["awaiting"] = None
        await q.edit_message_text(q.message.text_html + "\n\n❌ не записала", parse_mode=ParseMode.HTML)


# --- сообщения ---

# --- самочувствие ---

SYMPTOMS = [  # (как пишут, что записываем)
    (r"(?:болит\s+голова|голова\s+болит|головн\w*\s+бол\w*|мигрен\w*)", "голова"),
    (r"(?:кружится\s+голова|голова\s+кружится|головокружени\w*|(?:низко\w*\s+|упало\s+)?давлени\w*"
     r"(?:\s+(?:низко\w*|упало|упал\w*|падает))?)", "давление"),
    (r"(?:слабост\w*|нет\s+сил)", "слабость"),
    (r"(?:(?:очень\s+|совсем\s+)?(?:плохо|мало|почти\s+не)\s+спал\w*|не\s+выспал\w*|бессонниц\w*|"
     r"уснул\w*\s+(?:только|поздно)(?:\s+(?:в|около)\s+\w+)?)", "сон"),
]
SYMPTOM_EMOJI = {"норм": "👍", "голова": "🤕", "давление": "😵", "слабость": "😩", "сон": "😴"}
# сообщение — рассказ о самочувствии: записываем целиком как заметку и еду из него не вытаскиваем
_WELLBEING = re.compile(r"самочувстви|чувствую\s+себя|хотела\s+рассказать|состояние", re.I)


def extract_symptoms(text: str) -> tuple[list[str], str]:
    """«съела творог, голова болит» -> (["голова"], «съела творог»)."""
    kinds = []
    for pattern, kind in SYMPTOMS:
        if re.search(pattern, text, re.I):
            kinds.append(kind)
            text = re.sub(pattern, " ", text, flags=re.I)
    rest = re.sub(r"(?:^|[,.;!\s])(?:и|а|ещё|еще|сильно|немного|чуть|очень|опять|снова|у меня)(?=[,.;!\s]|$)", " ", text, flags=re.I)
    return kinds, rest.strip(" ,.;!-") if re.search(r"\w", rest) else ""


def symptoms_keyboard() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(f"{e} {k}", callback_data=f"sy:{k}") for k, e in SYMPTOM_EMOJI.items()]
    return InlineKeyboardMarkup([buttons[:3], buttons[3:]])


async def on_symptom_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    kind = q.data[3:]
    st.add_symptom(kind)
    await q.answer(f"{SYMPTOM_EMOJI[kind]} записала")
    await q.edit_message_reply_markup(None)
    await reply(update, f"самочувствие сегодня: {SYMPTOM_EMOJI[kind]} {kind}" +
                ("" if kind == "норм" else ". береги себя 💛 если что, допиши ещё кнопкой или словами"))


@owner_only
async def cmd_patterns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, report.format_patterns(st, st.today()))


# --- голосовые ---

_whisper = None


def transcribe(audio: bytes) -> str:
    """Локальный whisper (модель small, как в tg2obsidian). Подсказка из справочника — чтобы «поке» не стало «покис»."""
    global _whisper
    import tempfile
    import warnings
    import whisper
    warnings.filterwarnings("ignore")
    if _whisper is None:
        _whisper = whisper.load_model(getattr(config, "WHISPER_MODEL", "small"))
    prompt = "Дневник питания. " + ", ".join(d["name"] for d in st.search_dishes(limit=20)) + "."
    with tempfile.NamedTemporaryFile(suffix=".ogg") as f:
        f.write(audio)
        f.flush()
        return _whisper.transcribe(f.name, language="ru", fp16=False, initial_prompt=prompt)["text"].strip()


@owner_only
async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice or update.message.audio
    await update.effective_chat.send_action("typing")
    audio = bytes(await (await voice.get_file()).download_as_bytearray())
    try:
        text = await asyncio.to_thread(transcribe, audio)
    except Exception as e:
        log.warning("whisper: %s", e)
        await reply(update, "не получилось расшифровать голосовое 😕 напиши текстом")
        return
    if not text:
        await reply(update, "ничего не расслышала, попробуй ещё раз")
        return
    await reply(update, f"🎙 «{report.esc(text)}»")
    await handle_text(update, context, text)


# --- сообщения ---

@owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_text(update, context, update.message.text.strip())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Общий разбор для текста и расшифрованных голосовых."""
    log.info("сообщение: %s (ждём: %s)", text, context.user_data.get("awaiting"))
    if await on_keyboard_button(update, context, text):
        context.user_data["awaiting"] = None
        return
    ud = context.user_data
    awaiting = ud.get("awaiting")

    kinds, rest = extract_symptoms(text)
    if _WELLBEING.search(text):
        for k in kinds:
            st.add_symptom(k)
        st.add_symptom("заметка", note=text)
        found = ", ".join(f"{SYMPTOM_EMOJI[k]} {k}" for k in kinds)
        await reply(update, "записала про самочувствие" + (f": {found}" if found else "") +
                    ". береги себя 💛\nеду из этого сообщения не записываю: если что-то съела, напиши отдельно")
        return
    if kinds:
        for k in kinds:
            st.add_symptom(k)
        await reply(update, "записала самочувствие: " + ", ".join(f"{SYMPTOM_EMOJI[k]} {k}" for k in kinds) +
                    ("" if rest else ". береги себя 💛"))
        if not rest:
            return
        text = rest

    if awaiting == "weight" and await on_weight_reply(update, context, text):
        return
    if awaiting == "product" and ud.get("product") and await on_product_reply(update, context, text):
        return
    if awaiting == "entry_kcal":
        ud["awaiting"] = None
        m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(?:ккал|kcal)?\s*", text)
        entry_id = ud.pop("edit_entry", None)
        if m and entry_id and st.get_entry(entry_id):
            st.set_entry_kcal(entry_id, float(m.group(1).replace(",", ".")))
            e = st.get_entry(entry_id)
            await reply(update, f"✏️ {report.esc(e['descr'])} теперь {report.n(e['kcal'])} ккал{pfc_line(dict(e))}\n"
                                f"<b>{report.left_line(st, st.today())}</b>")
            return

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
                await reply(update, "не поняла правку 😕 попробуй так: <code>сироп 25</code>, "
                                    "а калорийность отдельно: <code>/food л-карнитин сироп 14</code>")
                return
            for item in draft.items:  # названную калорийность запоминаем на будущее
                if item.per100 and item.per100.match == "со слов":
                    st.put_food(item.name, item.per100.kcal)
                    item.per100.match = "со слов, запомнила"
        await asyncio.to_thread(nutr.fill, draft)
        await show_draft(update, context, draft)
        return

    if await on_water(update, text) or await on_watch(update, text):
        return

    quick = parse_quick(text)
    if quick and quick.kind == "ambiguous":
        ud["awaiting"] = None
        ud["amb"] = {"kcal": quick.kcal, "desc": quick.desc, "text": text}
        await reply(update, f"«{report.esc(text)}»: это {quick.kcal} ккал или {quick.kcal} шт?",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(f"{quick.kcal} ккал", callback_data="amb_kcal"),
                        InlineKeyboardButton(f"{quick.kcal} шт, посчитать", callback_data="amb_count")]]))
        return
    if quick:
        ud["awaiting"] = None
        st.add_entry(quick.kind, quick.desc, quick.kcal, "manual")
        if quick.kind == "quick" and quick.desc != "еда":
            st.put_dish(quick.desc, quick.kcal)
        await update.message.set_reaction("👌")
        await reply(update, report.left_line(st, st.today()))
        return

    dish = st.find_dish(text)
    if dish:
        await show_dish(update, context, dish_dict(dish))
        return
    # «лимонад» -> «лимонад боржоми аджарский мандарин»: по словам нашлось ровно одно блюдо — предлагаем его
    similar = st.search_dishes(text, limit=2)
    if len(similar) == 1:
        await show_dish(update, context, dish_dict(similar[0]),
                        "из справочника. не то? жми ❌ и напиши подробнее, например с граммами")
        return

    await parse_and_show(update, context, llm.parse_text, text)


@owner_only
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # картинка файлом приходит без сжатия Telegram — штрихкоды и мелкий текст читаются лучше
    log.info("фото%s", " файлом" if update.message.document else "")
    src = update.message.photo[-1] if update.message.photo else update.message.document
    image = bytes(await (await src.get_file()).download_as_bytearray())
    await update.effective_chat.send_action("typing")
    caption = update.message.caption or ""
    code = await asyncio.to_thread(products.decode_barcode, image)
    if not code:  # последнее фото без найденного штрихкода — чтобы было на чём разбираться, если он там был
        (DATA / "last_photo_no_barcode.jpg").write_bytes(image)
    if code:
        prod = await asyncio.to_thread(products.off_product, code)
        if prod:
            await show_product(update, context, prod)
        else:
            await reply(update, f"штрихкод {code} нашла, но в Open Food Facts его нет 😕\n"
                                "пришли фото этикетки с пищевой ценностью, посчитаю по ней")
        return
    try:
        screen = await asyncio.to_thread(screens.read_screenshot, image)
    except Exception as e:  # нет tesseract, битая картинка — просто считаем, что это фото еды
        log.warning("ocr: %s", e)
        screen = None
    if screen and screen.kind == "label":
        prod = screen.label
        m = re.match(r"^\s*(.*?)\s*(\d+(?:[.,]\d+)?)?\s*(?:г|гр|мл|g|ml)?\s*$", caption)
        prod.name = (m.group(1) if m else caption).strip().lower()
        grams = float(m.group(2).replace(",", ".")) if m and m.group(2) else None
        await show_product(update, context, prod, grams)
        return
    if screen and screen.kind == "card":
        await on_card(update, context, screen.dishes[0])
    elif screen:
        await on_list(update, context, screen.dishes, menu=screen.kind == "menu")
    else:
        await parse_and_show(update, context, llm.parse_photo, image, update.message.caption or "")


# --- продукты в упаковке: штрихкод или этикетка ---

async def show_product(update: Update, context: ContextTypes.DEFAULT_TYPE, prod: products.Product,
                       grams: float | None = None):
    """Знаем ккал на 100 г — спрашиваем, сколько съела. Граммы из подписи к фото записываем сразу."""
    ud = context.user_data
    ud["product"] = prod
    if grams:
        await reply(update, record_product(context, grams))
        return
    ud["awaiting"] = "product"
    pfc = pfc_line({"protein": prod.protein, "fat": prod.fat, "carbs": prod.carbs})
    title = report.esc(prod.name) if prod.name else "по этикетке"
    ask = "сколько съела? пришли граммы" if prod.name else "сколько съела и как назвать? например <code>батончик 40</code>"
    buttons = []
    if prod.package_g:
        g = prod.package_g
        buttons.append([InlineKeyboardButton(f"вся упаковка, {report.n(g)} г · {prod.portion(g)['kcal']}", callback_data="pr:all"),
                        InlineKeyboardButton(f"половина · {prod.portion(g / 2)['kcal']}", callback_data="pr:half")])
    buttons.append([InlineKeyboardButton("❌", callback_data="pr:no")])
    await reply(update, f"{title}\n<b>{report.n(prod.kcal)} ккал на 100 г</b>{pfc}  <i>({prod.source})</i>\n\n{ask}",
                reply_markup=InlineKeyboardMarkup(buttons))


def record_product(context: ContextTypes.DEFAULT_TYPE, grams: float, name: str = "") -> str:
    ud = context.user_data
    prod: products.Product = ud.pop("product")
    ud["awaiting"] = None
    prod.name = name or prod.name or "продукт по этикетке"
    st.put_food(prod.name, prod.kcal, prod.protein, prod.fat, prod.carbs)  # дальше «… 50г» найдётся без фото
    part = prod.portion(grams)
    if prod.package_g and abs(grams - prod.package_g) < 1:
        st.put_dish(prod.name, part["kcal"], part["protein"], part["fat"], part["carbs"])  # «вся банка» — блюдо
    st.add_entry("quick", f"{prod.name} {report.n(grams)}г", part["kcal"], "label",
                 part["protein"], part["fat"], part["carbs"])
    return (f"👌 {report.esc(prod.name)} {report.n(grams)} г · {part['kcal']} ккал{pfc_line(part)}\n"
            f"<b>{report.left_line(st, st.today())}</b>")


async def on_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    prod = context.user_data.get("product")
    await q.answer()
    await q.edit_message_reply_markup(None)
    if prod is None:
        return
    if q.data == "pr:no":
        context.user_data.pop("product")
        context.user_data["awaiting"] = None
        await reply(update, "❌ не записала")
        return
    g = prod.package_g if q.data == "pr:all" else prod.package_g / 2
    await reply(update, record_product(context, g))


async def on_product_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """«40», «40 г», «батончик 40». False — это не ответ про граммы."""
    m = re.fullmatch(r"\s*(?P<name>[^\d]*?)\s*(?P<g>\d+(?:[.,]\d+)?)\s*(?:г|гр|мл|g|ml)?\s*", text)
    if not m:
        context.user_data.pop("product", None)
        context.user_data["awaiting"] = None
        return False
    await reply(update, record_product(context, float(m.group("g").replace(",", ".")), m.group("name").lower()))
    return True


# --- скриншоты рационов ---

async def on_card(update: Update, context: ContextTypes.DEFAULT_TYPE, d: screens.Dish):
    """Подробная карточка: БЖУ сходятся с ккал и блюда ещё нет за сегодня — записываем сразу."""
    st.put_dish(d.name, d.kcal, d.protein, d.fat, d.carbs)  # в справочник сразу, блюда повторяются
    dish = {"name": d.name, "kcal": d.kcal, "protein": d.protein, "fat": d.fat, "carbs": d.carbs}
    if norm(d.name) in st.eaten_on(st.today()):
        await show_dish(update, context, dish, "уже записано сегодня, записать ещё раз?")
    elif not d.consistent():
        await show_dish(update, context, dish, "проверь цифры, похоже, я что-то не так прочитала")
    else:
        context.user_data["dish"] = dish
        await update.message.set_reaction("👌")
        grams = f" · {report.n(d.grams)} г" if d.grams else ""
        await reply(update, record_dish(context, d.kcal, "screen").replace("ккал", f"ккал{grams}", 1))


def list_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{'✅' if it['on'] else '⬜'} {it['name'][:30]} · {report.n(it['kcal'])}",
                                  callback_data=f"sl:{k}")] for k, it in enumerate(items)]
    n_on = sum(it["on"] for it in items)
    rows.append([InlineKeyboardButton(f"записать ({n_on})", callback_data="sl_ok"),
                 InlineKeyboardButton("❌", callback_data="sl_no")])
    return InlineKeyboardMarkup(rows)


async def on_list(update: Update, context: ContextTypes.DEFAULT_TYPE, dishes: list[screens.Dish], menu: bool = False):
    """Список заказов или бумажное меню: блюд несколько, съедено может быть не всё — даём выбрать галочками.
    Меню на день обычно приходит до еды, поэтому там по умолчанию ничего не отмечено."""
    eaten = st.eaten_on(st.today())
    items = []
    for d in dishes:
        known = st.find_dish(d.name)
        if not known and d.protein is not None:
            known = st.find_dish_by_pfc(d.protein, d.fat, d.carbs)
        # «…с шампиньо...» или «мукитрубого» -> название из справочника
        name = known["name"] if known else d.name or f"{d.meal or 'блюдо'} {report.n(d.kcal)} ккал"
        st.put_dish(name, d.kcal, d.protein, d.fat, d.carbs)
        dish = dish_dict(st.get_dish(name))
        dish.update(meal=d.meal, on=not menu and norm(name) not in eaten)
        items.append(dish)
    if menu:
        text = (f"добавила {len(items)} блюд из меню в справочник 📋\n"
                "отметь, что уже съела, или записывай потом через /f")
    else:
        note = "\n\nсняла галочки с того, что уже записано сегодня" if not all(i["on"] for i in items) else ""
        text = f"нашла {len(items)} блюд, отметь, что съела:{note}"
    msg = await reply(update, text, reply_markup=list_keyboard(items))
    context.user_data["screen_list"] = {"items": items, "msg": msg.message_id}


async def on_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    sl = context.user_data.get("screen_list")
    if not sl or q.message.message_id != sl["msg"]:
        await q.answer("этот список уже неактуален")
        await q.edit_message_reply_markup(None)
        return
    await q.answer()
    if q.data.startswith("sl:"):
        it = sl["items"][int(q.data[3:])]
        it["on"] = not it["on"]
        await q.edit_message_reply_markup(list_keyboard(sl["items"]))
        return
    context.user_data.pop("screen_list")
    if q.data == "sl_no":
        await q.edit_message_text("❌ не записала")
        return
    chosen = [it for it in sl["items"] if it["on"]]
    if not chosen:
        await q.edit_message_text("ок, ничего не записала. блюда остались в справочнике, ищи через /f")
        return
    for it in chosen:
        st.add_entry("quick", it["name"], it["kcal"], "screen", it["protein"], it["fat"], it["carbs"])
    lines = "\n".join(f"• {report.esc(it['name'])} · {report.n(it['kcal'])}{pfc_line(it)}" for it in chosen)
    await q.edit_message_text(f"👌 записала:\n{lines or 'ничего'}\n\n<b>{report.left_line(st, st.today())}</b>",
                              parse_mode=ParseMode.HTML)


# --- команды ---

@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, f"норма сейчас {report.goal(st)} ккал в день\n\n" + HELP, reply_markup=MAIN_KEYBOARD)


# подсказки при вводе «/» и в кнопке «Меню»
COMMANDS = [
    ("today", "сегодня и остаток, правка записей"),
    ("f", "найти блюдо и записать одним нажатием"),
    ("week", "неделя и график"),
    ("undo", "удалить последнюю запись"),
    ("weight", "записать вес: /weight 57.5"),
    ("dishes", "мои блюда"),
    ("patterns", "самочувствие и закономерности"),
    ("tdee", "реальный расход по весу"),
    ("goal", "норма калорий"),
    ("remind", "напоминания"),
    ("food", "ккал на 100 г в справочник"),
    ("file", "табличка xlsx"),
    ("help", "что я умею"),
]

# постоянные кнопки под полем ввода: текст кнопки -> что сделать
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("📊 сегодня"), KeyboardButton("🔎 частое"), KeyboardButton("💧 стакан воды")],
     [KeyboardButton("📅 неделя"), KeyboardButton("↩️ отменить последнее"), KeyboardButton("❓ помощь")]],
    resize_keyboard=True, is_persistent=True, input_field_placeholder="что съела или сожгла?")


async def on_keyboard_button(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    actions = {"📊 сегодня": cmd_today, "📅 неделя": cmd_week, "↩️ отменить последнее": cmd_undo,
               "❓ помощь": cmd_start, "🔎 частое": cmd_find}
    if text in actions:
        context.args = []
        await actions[text](update, context)
        return True
    if text == "💧 стакан воды":
        return await on_water(update, "стакан воды")
    return False


async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
    if st.get("keyboard_sent") != "1":  # один раз прислать кнопки, дальше они живут в чате сами
        await app.bot.send_message(config.OWNER_ID, "добавила кнопки внизу и подсказки при вводе /", reply_markup=MAIN_KEYBOARD)
        st.set("keyboard_sent", "1")


GLASS_ML = 200
WATER_BUTTONS = [("стакан", 200), ("бутылка", 330), ("бутылка", 500)]
_WATER = re.compile(r"^\s*(?P<sign>[+-])?\s*(?:вод[аыу]|💧|(?:боржоми\s+)?аромати|минералк\w*|минеральн\w*\s+вод\w*)\s*(?P<n>\d+(?:[.,]\d+)?)?\s*(?P<u>мл|ml|л|l)?\s*$", re.I)
# «стакан воды», «2 стакана воды», «полбутылки воды», «кружка воды»
_WATER_PORTION = re.compile(r"^\s*(?:выпила\s+)?(?P<count>\d+|один|одну|два|две|три|пол)?\s*-?\s*"
                            r"(?P<what>стакан\w*|кружк\w*|бутыл\w*|бутылочк\w*)\s+воды\s*$", re.I)
PORTION_ML = {"стакан": 200, "кружк": 300, "бутыл": 500}
COUNT_WORDS = {"один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "пол": 0.5}


def water_amount(text: str) -> tuple[str, float | None] | None:
    """(«+», мл) или («-», None); None — это не про воду."""
    text = text.strip(" .!")  # из голосового приходит «Выпила стакан воды.»
    m = _WATER_PORTION.match(text)
    if m:
        c = m.group("count")
        count = float(c) if c and c.isdigit() else COUNT_WORDS.get((c or "").lower(), 1)
        size = next(ml for k, ml in PORTION_ML.items() if m.group("what").lower().startswith(k))
        return "+", count * size
    m = _WATER.match(text)
    if not m:
        return None
    if m.group("sign") == "-":
        return "-", None
    ml = float(m.group("n").replace(",", ".")) if m.group("n") else GLASS_ML
    if m.group("u") in ("л", "l") or (m.group("n") and ml < 10):
        ml *= 1000
    return "+", ml


def water_today_text() -> str:
    h = report.hydration(st, st.today())
    tea = f", из них чай и кофе {report.liters(h['drinks'])}" if h["drinks"] else ""
    return f"сегодня {report.liters(h['total'])}{tea}"


_WATCH = re.compile(r"^\s*(?:⌚|часы|активн\w*|apple watch)(?:\s+за\s+день)?\s*:?\s*(\d{1,4})\s*(?:ккал|kcal)?\s*$", re.I)


async def on_watch(update: Update, text: str) -> bool:
    """Активные ккал за день с Apple Watch: в остаток идёт то, что сверх обычного движения."""
    m = _WATCH.match(text)
    if not m:
        return False
    kcal = float(m.group(1))
    st.set_watch(kcal)
    t = st.day_totals(st.today())
    extra = max(kcal - st.active_baseline, 0)
    manual = t["burned"] if t["burned"] > extra else 0
    note = (f"ручные тренировки сегодня больше ({report.n(manual)}), засчитала их" if manual
            else f"сверх обычных {report.n(st.active_baseline)} → +{report.n(extra)} в остаток")
    await update.message.set_reaction("👌")
    await reply(update, f"⌚ часы: {report.n(kcal)} активных ккал, {note}\n<b>{report.left_line(st, st.today())}</b>")
    return True


async def on_water(update: Update, text: str) -> bool:
    """«вода», «+вода», «💧» — стакан 200 мл; «вода 500», «вода 0,5 л», «2 стакана воды»; «-вода» — убрать последний."""
    amount = water_amount(text)
    if amount is None:
        return False
    sign, ml = amount
    if sign == "-":
        ml = st.remove_last_water(st.today())
        note = f"убрала {report.n(ml)} мл" if ml else "сегодня воды ещё не было"
    else:
        st.add_water(ml)
        note = f"+{report.n(ml)} мл"
    await reply(update, f"💧 {note}, {water_today_text()}", reply_markup=water_keyboard())
    return True


def water_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"💧 {name} {ml}", callback_data=f"wt:{ml}")
                                  for name, ml in WATER_BUTTONS]])


def entries_keyboard(day: date) -> InlineKeyboardMarkup | None:
    """У каждой записи дня: ✏️ поменять ккал и 🗑 удалить. Внизу — стакан воды."""
    rows = []
    for e in st.entries(day):
        sign = "−" if e["kind"] in ("workout", "watch") else ""
        label = f"✏️ {e['ts'][11:16]} {e['descr'][:22]} · {sign}{report.n(e['kcal'])}"
        rows.append([InlineKeyboardButton(label, callback_data=f"ed:{e['id']}"),
                     InlineKeyboardButton("🗑", callback_data=f"dl:{e['id']}")])
    rows += water_keyboard().inline_keyboard
    return InlineKeyboardMarkup(rows)


@owner_only
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    day = st.today()
    await reply(update, report.format_day(st, day), reply_markup=entries_keyboard(day))


async def on_entry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ed:<id> — ждём новую цифру ккал; dl:<id> — удаляем сразу, с кнопкой «вернуть»; un — вернуть."""
    q = update.callback_query
    ud = context.user_data
    await q.answer()
    if q.data == "un":
        saved = ud.pop("deleted", None)
        if saved and not st.get_entry(saved["entry"]["id"]):
            st.restore_entry(saved)
            await q.edit_message_text(f"↩️ вернула {report.esc(saved['entry']['descr'])}. "
                                      f"<b>{report.left_line(st, st.today())}</b>", parse_mode=ParseMode.HTML)
        return
    entry_id = int(q.data[3:])
    e = st.get_entry(entry_id)
    if not e:
        await q.answer("этой записи уже нет")
        return
    if q.data.startswith("dl:"):
        ud["deleted"] = st.delete_entry(entry_id)
        await reply(update, f"🗑 удалила {report.esc(e['descr'])} · {report.n(e['kcal'])}. "
                            f"<b>{report.left_line(st, st.today())}</b>",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ вернуть", callback_data="un")]]))
    else:
        ud["awaiting"] = "entry_kcal"
        ud["edit_entry"] = entry_id
        await reply(update, f"{report.esc(e['descr'])}: сейчас {report.n(e['kcal'])} ккал. "
                            "сколько должно быть? пришли число")


@owner_only
async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, report.format_week(st, st.today()))
    png = await asyncio.to_thread(report.week_chart, st, st.today())
    await update.effective_message.reply_photo(png)


@owner_only
async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = st.delete_last()
    if not row:
        await reply(update, "удалять нечего")
        return
    await reply(update, f"удалила: {row['day']} {report.esc(row['descr'])} ({round(row['kcal'])} ккал)\n"
                        f"{report.left_line(st, st.today())}")


@owner_only
async def cmd_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        kg = float(context.args[0].replace(",", "."))
    except (IndexError, ValueError):
        w = st.last_weight()
        await reply(update, f"напиши так: <code>/weight 57.5</code>" + (f"\nпоследний вес {w} кг" if w else ""))
        return
    st.add_weight(kg)
    note = " (это своя норма, /goal auto пересчитает по весу)" if st.get("goal_manual") else ""
    await reply(update, f"записала {kg} кг. норма {report.goal(st)} ккал{note}")


@owner_only
async def cmd_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = context.args[0] if context.args else ""
    if arg == "auto":
        st.set("goal_manual", "")
    elif arg == "adapt":
        a = report.adaptive(st, st.today())
        if not a["ok"]:
            await reply(update, f"пока рано: {a['why']}. подробнее: /tdee")
            return
        st.set("goal_manual", a["suggested"])
    elif arg.isdigit():
        st.set("goal_manual", int(arg))
    else:
        await reply(update, f"норма {report.goal(st)} ккал. поменять: <code>/goal 1300</code>, "
                            "<code>/goal auto</code> (по формуле) или <code>/goal adapt</code> (по реальному расходу)")
        return
    await reply(update, f"норма {report.goal(st)} ккал. {report.left_line(st, st.today())}")


@owner_only
async def cmd_tdee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, report.format_adaptive(st, st.today()))


@owner_only
async def cmd_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.match(r"^(.+?)\s+(\d+(?:[.,]\d+)?)$", " ".join(context.args))
    if not m:
        await reply(update, "напиши так: <code>/food чак-чак 450</code> (ккал на 100 г)")
        return
    st.put_food(m.group(1), float(m.group(2).replace(",", ".")))
    await reply(update, f"запомнила: {report.esc(m.group(1).lower())}, {m.group(2)} ккал на 100 г")


@owner_only
async def cmd_dishes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = st.dishes()
    if not rows:
        await reply(update, "пока пусто. блюда запоминаются, когда пишешь с цифрой: <code>450 цезарь жан-жак</code>")
        return
    await reply(update, "<b>мои блюда</b> (ккал на порцию)\n" +
                "\n".join(f"{report.n(r['kcal'])}  {report.esc(r['name'])}" for r in rows) +
                "\n\nудалить: <code>/dish_del название</code>")


@owner_only
async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/f лимонад — блюда из справочника кнопками, нажатие сразу записывает. /f без слов — самое частое."""
    query = " ".join(context.args)
    rows = st.search_dishes(query)
    if not rows:
        await reply(update, f"в справочнике нет «{report.esc(query)}». "
                            "запиши один раз с цифрой, например <code>100 лимонад</code>, и я запомню")
        return
    title = f"нашла по «{report.esc(query)}»" if query else "чаще всего записываешь"
    await reply(update, f"{title}. нажми, чтобы записать:", reply_markup=found_keyboard(context.user_data, rows))


def found_keyboard(user_data: dict, rows) -> InlineKeyboardMarkup:
    """Кнопки «записать одним нажатием»; сами блюда запоминаем в user_data для on_find_callback."""
    found = [dish_dict(r) for r in rows]
    user_data["found"] = found
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{d['name'][:34]} · {report.n(d['kcal'])}",
                                                       callback_data=f"fd:{k}")] for k, d in enumerate(found)])


async def on_find_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопки не пропадают: два лимонада — два нажатия."""
    q = update.callback_query
    found = context.user_data.get("found") or []
    k = int(q.data[3:])
    if k >= len(found):
        await q.answer("этот список уже неактуален")
        return
    d = found[k]
    st.add_entry("quick", d["name"], d["kcal"], "manual", d["protein"], d["fat"], d["carbs"])
    left = report.left_line(st, st.today())
    await q.answer(f"👌 {d['name'][:40]} · {report.n(d['kcal'])}. {left}")
    await reply(update, f"👌 {report.esc(d['name'])} · {report.n(d['kcal'])} ккал{pfc_line(d)}\n<b>{left}</b>")


@owner_only
async def cmd_dish_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = " ".join(context.args)
    if name and st.delete_dish(name):
        await reply(update, f"удалила «{report.esc(name)}»")
    else:
        await reply(update, "не нашла такое блюдо, список тут: /dishes")


async def send_file(bot, chat_id: int):
    path = report.build_xlsx(st, DATA / f"eat-burn-{st.today()}.xlsx")
    with open(path, "rb") as f:
        await bot.send_document(chat_id, f, filename=path.name)


@owner_only
async def cmd_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_file(context.bot, update.effective_chat.id)


@owner_only
async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args or [""])[0]
    if arg in ("on", "off"):
        st.set("remind", arg)
    state = "включены" if st.get("remind", "on") == "on" else "выключены"
    await reply(update, f"напоминания {state}: вес раз в неделю ({WEEKDAYS_RU[WEIGHT_DAY]}, {WEIGHT_AT:%H:%M}), еда в "
                        f"{', '.join(f'{t:%H:%M}' for t in FOOD_AT)}, если {NUDGE_GAP_H} ч ничего не записывала\n"
                        "<code>/remind off</code> выключить, <code>/remind on</code> включить")


# --- расписание ---

def parse_hhmm(s: str, tz) -> time:
    h, m = s.split(":")
    return time(int(h), int(m), tzinfo=tz)


TZ = datetime.now().astimezone().tzinfo
WEIGHT_AT = parse_hhmm(getattr(config, "REMIND_WEIGHT_AT", "09:00"), TZ)
# вес раз в неделю: каждый день он скачет на 0,5–2 кг от воды и соли, а динамику видно и по неделям
WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEIGHT_DAY = WEEKDAYS_RU.index(getattr(config, "REMIND_WEIGHT_DAY", "среда"))
FOOD_AT = [parse_hhmm(s, TZ) for s in getattr(config, "REMIND_FOOD_AT", ["13:30", "17:30", "20:30"])]
NUDGE_GAP_H = getattr(config, "REMIND_GAP_HOURS", 4)
WEIGHT_REPLY_UNTIL_H = 12  # до полудня число после напоминания — это вес


async def job_weight(context: ContextTypes.DEFAULT_TYPE):
    if st.get("remind", "on") != "on" or st.weighed_on(st.today()):
        return
    last_w = st.weights()
    if last_w and (st.today() - date.fromisoformat(last_w[-1]["day"])).days < 6:
        return  # на этой неделе уже взвешивалась
    context.application.user_data[config.OWNER_ID]["awaiting"] = "weight"
    last = st.last_weight()
    hint = f" (в прошлый раз {last} кг)" if last else ""
    await context.bot.send_message(config.OWNER_ID, f"доброе утро ☀️ сегодня день взвешивания: утром, натощак{hint}\n"
                                                    "просто пришли число, например <code>57.5</code>",
                                   parse_mode=ParseMode.HTML)


async def job_nudge(context: ContextTypes.DEFAULT_TYPE):
    """Напоминаем, только если давно ничего не записывала — когда всё ведёшь, бот молчит."""
    last = st.last_entry_time()
    if st.get("remind", "on") != "on" or (last and datetime.now() - last < timedelta(hours=NUDGE_GAP_H)):
        return
    since = f"с {last:%H:%M} " if last and last.date() == datetime.now().date() else "сегодня "
    ud = context.application.user_data[config.OWNER_ID]
    await context.bot.send_message(
        config.OWNER_ID,
        f"{since}ничего не записано. что ела или была активность? 🍽\n"
        f"{report.left_line(st, st.today())}\n\nчастое ниже, остальное просто напиши",
        reply_markup=found_keyboard(ud, st.search_dishes(limit=5)))


async def on_weight_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Ответ числом на утреннее напоминание о весе. False — это не вес, обрабатываем как обычно."""
    context.user_data["awaiting"] = None
    m = re.fullmatch(r"\s*(\d{2,3}(?:[.,]\d{1,2})?)\s*(?:кг|kg)?\s*", text)
    if not m or datetime.now().hour >= WEIGHT_REPLY_UNTIL_H:
        return False
    kg = float(m.group(1).replace(",", "."))
    if not 30 <= kg <= 150:
        return False
    st.add_weight(kg, st.today())
    w = st.weights()
    trend = f", неделю назад было {w[-2]['kg']}" if len(w) >= 2 else ""
    await update.message.set_reaction("👌")
    await reply(update, f"записала {kg} кг{trend}. норма {report.goal(st)} ккал")
    return True

async def job_daily(context: ContextTypes.DEFAULT_TYPE):
    day = st.today()
    if st.day_totals(day)["n"]:
        await context.bot.send_message(config.OWNER_ID, report.format_day(st, day, "итог дня"),
                                       parse_mode=ParseMode.HTML)
    if not st.symptoms_on(day):  # для /patterns нужны и плохие, и нормальные дни
        await context.bot.send_message(config.OWNER_ID, "как самочувствие сегодня?", reply_markup=symptoms_keyboard())


async def job_weekly(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(config.OWNER_ID, report.format_week(st, st.today()),
                                   parse_mode=ParseMode.HTML)
    await context.bot.send_photo(config.OWNER_ID, await asyncio.to_thread(report.week_chart, st, st.today()))
    await send_file(context.bot, config.OWNER_ID)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Сетевые обрывы Telegram — одной строкой, бот сам переподключится. Остальное — с трейсом."""
    from telegram.error import NetworkError
    if isinstance(context.error, NetworkError):
        log.warning("telegram: %s", context.error)
    else:
        log.error("ошибка при обработке", exc_info=context.error)


def main():
    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()
    app.add_error_handler(on_error)
    for name, fn in [("start", cmd_start), ("help", cmd_start), ("today", cmd_today), ("week", cmd_week),
                     ("undo", cmd_undo), ("weight", cmd_weight), ("goal", cmd_goal), ("food", cmd_food), ("dishes", cmd_dishes), ("dish_del", cmd_dish_del),
                     ("f", cmd_find), ("find", cmd_find),
                     ("file", cmd_file), ("remind", cmd_remind), ("tdee", cmd_tdee), ("patterns", cmd_patterns)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    tz = datetime.now().astimezone().tzinfo
    app.job_queue.run_daily(job_daily, time(22, 0, tzinfo=tz))
    app.job_queue.run_daily(job_weekly, time(21, 0, tzinfo=tz), days=(0,))  # 0 = воскресенье в PTB
    app.job_queue.run_daily(job_weight, WEIGHT_AT, days=((WEIGHT_DAY + 1) % 7,))  # в PTB 0 = воскресенье
    for t in FOOD_AT:
        app.job_queue.run_daily(job_nudge, t)
    log.info("eat-burn запущен, норма %s ккал", report.goal(st))
    app.run_polling()


if __name__ == "__main__":
    main()
