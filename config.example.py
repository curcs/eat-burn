BOT_TOKEN = "123456789:AAH..."   # токен от @BotFather (новый бот, не debit&credit)
OWNER_ID = 123456789             # твой telegram id (узнать у @userinfobot)

# профиль для нормы (Миффлин — Сан Жеор, дефицит 15%)
HEIGHT_CM = 165
BIRTH_YEAR = 1996
START_WEIGHT = 60
SEX = "f"

# модели Ollama
TEXT_MODEL = "qwen2.5:7b"
VISION_MODEL = "gemma3:4b"

# день начинается в 4 утра: перекус в 00:30 считается во вчерашний день
DAY_START_HOUR = 4

# напоминания: вес раз в неделю утром, еда — если REMIND_GAP_HOURS часов ничего не записано
REMIND_WEIGHT_DAY = "среда"     # вес раз в неделю, в этот день
REMIND_WEIGHT_AT = "09:00"
REMIND_FOOD_AT = ["13:30", "17:30", "20:30"]
REMIND_GAP_HOURS = 4

# Apple Watch: «часы 412» — активные ккал за день. Столько часы насчитают и в сидячий день, это уже в норме
ACTIVE_BASELINE = 200

# голосовые: модель whisper (tiny, base, small, medium). small — нормальный русский и быстро на Apple Silicon
WHISPER_MODEL = "small"
