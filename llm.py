"""Ollama: текст или фото -> список продуктов с граммами. Калории модель НЕ считает."""
import base64
import json

import httpx

from nutrition import Draft, Item, Per100

OLLAMA_URL = "http://localhost:11434/api/chat"

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"name_ru": {"type": "string"}, "name_en": {"type": "string"},
                       "grams": {"type": "number"}, "kcal_100g": {"type": "number"}},
        "required": ["name_ru", "name_en", "grams", "kcal_100g"]}}},
    "required": ["items"],
}

PROMPT = """Ты разбираешь запись о еде на отдельные продукты.
Для каждого продукта верни:
- name_ru: короткое название по-русски в именительном падеже ("овсянка", "банан", "мёд");
- name_en: название в стиле базы USDA, 1-3 слова, главное слово первым, потом состояние:
  "oats dry", "banana raw", "honey", "chicken breast cooked", "buckwheat cooked", "egg whole boiled";
- grams: вес в граммах. Если вес указан — бери его. Если нет — типичная порция:
  банан 120, яблоко 180, яйцо 55, столовая ложка мёда/масла 20/15, чайная ложка 7,
  кусок хлеба 30, тарелка супа 300, гарнир 150, кусок мяса/рыбы 120, чашка кофе с молоком 250;
- kcal_100g: примерная калорийность на 100 г в готовом виде (банан 90, борщ 50, гречка варёная 110).
Штуки переводи в граммы и умножай на количество: яйцо 55, сырник 50, блин 50, котлета 80, печенье 12,
ломтик сыра 20, мандарин 80 ("три мандарина" = 240), кусок пиццы 120, плитка шоколада 90 ("пол шоколадки" = 45),
чашка чая 250.
Готовое блюдо без состава ("борщ", "сырники", "пицца", "цезарь") — один продукт.
Блюдо "с чем-то", что входит в его рецепт, — тоже один продукт, не дели: "салат цезарь с курицей",
"паста с курицей", "гречка с грибами". Отдельно — только то, что едят отдельно: "тост с авокадо" -> тост и авокадо.
Напиток "на овсяном/миндальном/коровьем" — это молоко в напитке, один продукт, не овсянка.

Словарик name_en: гречка = buckwheat groats cooked, рис = rice white cooked, овсянка = oats dry,
творог = cottage cheese, ягоды = strawberries raw, авокадо = avocado raw, сметана = sour cream, кефир = kefir, ряженка = kefir,
хлеб чёрный = rye bread, хлеб белый = white bread, курица = chicken breast roasted, куриная грудка = chicken breast roasted,
индейка = turkey breast cooked, говядина = beef cooked, лосось = salmon cooked,
картошка = potato boiled, макароны = pasta cooked, сырники = cottage cheese pancakes,
борщ = borscht, капучино = cappuccino, салат цезарь = caesar salad.
Кофейные напитки называй как написано, не сокращай до «кофе»: флэт уайт, капучино, латте, раф, американо, эспрессо.
Объём кофе, если не указан: эспрессо 30, флэт уайт 180, капучино 250, латте 300, раф 300, американо 250.

Примеры:
"2 сырника со сметаной" -> [{"name_ru":"сырники","name_en":"cottage cheese pancakes","grams":100,"kcal_100g":220},
  {"name_ru":"сметана","name_en":"sour cream","grams":30,"kcal_100g":200}]
"латте на овсяном" -> [{"name_ru":"латте на овсяном молоке","name_en":"latte oat milk","grams":300,"kcal_100g":45}]
Ответ строго JSON по схеме."""

PHOTO_PROMPT = PROMPT + """
На фото тарелка с едой. Определи блюда/продукты и оцени вес каждого по размеру порции.
Если есть подпись пользователя — она важнее твоей оценки."""


EDIT_SCHEMA = json.loads(json.dumps(SCHEMA))
EDIT_SCHEMA["properties"]["items"]["items"]["properties"]["user_kcal_100g"] = {"type": "number"}
EDIT_SCHEMA["properties"]["items"]["items"]["required"].append("user_kcal_100g")

EDIT_PROMPT = """Есть список продуктов приёма пищи (JSON) и правка пользователя свободным текстом.
Верни ПОЛНЫЙ обновлённый список по той же схеме.
- Продукты, которых правка не касается, верни без изменений.
- "убери X" — удали продукт. "добавь X" или новый продукт — добавь (name_en и kcal_100g как обычно).
- Новый вес ("выпила 25 г", "банан 150") — поменяй grams. Миллилитры считай граммами.
- Если пользователь назвал калорийность продукта ("14 ккал на 100 мл") — запиши её в user_kcal_100g.
  Если назвал калорийность всей порции ("там было 50 ккал") — user_kcal_100g = 50 / grams * 100.
  Если калорийность не называл — user_kcal_100g = 0.
Ответ строго JSON по схеме."""


class LLMError(Exception):
    pass


YANDEX_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"


class LLM:
    """Текст — локальная модель в Ollama или YandexGPT (yandex=(folder_id, api_key, модель)); фото — всегда Ollama."""

    def __init__(self, text_model: str = "qwen2.5:7b", vision_model: str = "gemma3:4b",
                 url: str = OLLAMA_URL, timeout: float = 120, yandex: tuple[str, str, str] | None = None):
        self.text_model, self.vision_model = text_model, vision_model
        self.url, self.timeout = url, timeout
        self.yandex = yandex

    def parse_text(self, text: str) -> Draft:
        return self._ask(self.text_model, PROMPT, text, None, "text")

    def parse_photo(self, image: bytes, caption: str = "") -> Draft:
        return self._ask(self.vision_model, PHOTO_PROMPT,
                         f"Подпись: {caption}" if caption else "Что на фото?", image, "photo")

    def edit(self, draft: Draft, instruction: str) -> Draft:
        """Правка черновика своими словами. Продукты с user_kcal_100g приходят уже с per100 «со слов»."""
        current = json.dumps({"items": [{"name_ru": i.name, "name_en": i.name_en, "grams": i.grams,
                                         "kcal_100g": i.guess or 0, "user_kcal_100g": 0}
                                        for i in draft.items]}, ensure_ascii=False)
        new = self._ask(self.text_model, EDIT_PROMPT, f"Список: {current}\nПравка: {instruction}",
                        None, draft.source, EDIT_SCHEMA)
        old = {i.name: i for i in draft.items}
        for item in new.items:
            prev = old.get(item.name)
            if item.per100 is None and prev is not None:
                item.per100 = prev.per100  # граммы могли поменяться, калорийность на 100 г — нет
                item.portion = prev.portion
        return new

    def _ask(self, model, system, user, image, source, schema=SCHEMA) -> Draft:
        if self.yandex and not image:
            return self._ask_yandex(system, user, source, schema)
        msg = {"role": "user", "content": user}
        if image:
            msg["images"] = [base64.b64encode(image).decode()]
        body = {"model": model, "stream": False, "format": schema, "options": {"temperature": 0},
                "messages": [{"role": "system", "content": system}, msg]}
        last_err = None
        for _ in range(2):  # одна повторная попытка, если пришёл мусор
            try:
                r = httpx.post(self.url, json=body, timeout=self.timeout)
                r.raise_for_status()
                return to_draft(r.json()["message"]["content"], source)
            except httpx.HTTPError as e:
                raise LLMError(f"Ollama недоступна: {e}") from e
            except (ValueError, KeyError, TypeError) as e:
                last_err = e
        raise LLMError(f"модель ответила не по формату: {last_err}")

    def _ask_yandex(self, system, user, source, schema) -> Draft:
        """YandexGPT в Yandex Cloud: ответ строго по JSON-схеме, как у Ollama."""
        folder, key, model = self.yandex
        body = {"modelUri": f"gpt://{folder}/{model}/latest",
                "completionOptions": {"stream": False, "temperature": 0, "maxTokens": "1000"},
                "jsonSchema": {"schema": schema},
                "messages": [{"role": "system", "text": system}, {"role": "user", "text": user}]}
        headers = {"Authorization": f"Api-Key {key}", "x-folder-id": folder}
        last_err = None
        for _ in range(2):
            try:
                r = httpx.post(YANDEX_URL, json=body, headers=headers, timeout=self.timeout)
                if r.status_code >= 400:
                    raise LLMError(f"YandexGPT {r.status_code}: {r.text[:300]}")
                text = r.json()["result"]["alternatives"][0]["message"]["text"]
                return to_draft(text.strip().removeprefix("```json").removeprefix("```").removesuffix("```"), source)
            except httpx.HTTPError as e:
                raise LLMError(f"YandexGPT недоступен: {e}") from e
            except (ValueError, KeyError, TypeError, IndexError) as e:
                last_err = e
        raise LLMError(f"YandexGPT ответил не по формату: {last_err}")


def to_draft(content: str, source: str) -> Draft:
    data = json.loads(content)
    items = []
    for i in data["items"]:
        if float(i.get("grams") or 0) <= 0 or not str(i.get("name_ru", "")).strip():
            continue
        user_kcal = float(i.get("user_kcal_100g") or 0)
        items.append(Item(str(i["name_ru"]).strip().lower(), str(i["name_en"]).strip().lower(), float(i["grams"]),
                          per100=Per100(user_kcal, match="со слов") if user_kcal > 0 else None,
                          guess=float(i["kcal_100g"]) if i.get("kcal_100g") else None))
    if not items:
        raise ValueError("пустой список продуктов")
    return Draft(items, source)
