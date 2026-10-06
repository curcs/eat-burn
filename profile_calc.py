"""Норма калорий по формуле Миффлина — Сан Жеора."""


def bmr(weight_kg: float, height_cm: float, age: int, sex: str = "f") -> float:
    """Базовый обмен: сколько тратится в покое."""
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base - 161 if sex == "f" else base + 5


def target(weight_kg: float, height_cm: float, age: int, sex: str = "f",
           activity: float = 1.2, deficit: float = 0.15) -> int:
    """Дневная норма с дефицитом, округлённая до 50 ккал.

    activity=1.2 (сидячий) специально: тренировки пишутся отдельно
    и возвращаются в остаток, иначе посчитаются дважды.
    """
    tdee = bmr(weight_kg, height_cm, age, sex) * activity
    return int(round(tdee * (1 - deficit) / 50) * 50)
