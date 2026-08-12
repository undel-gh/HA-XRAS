"""Константы интеграции XRAS Geomagnetic."""

from __future__ import annotations

DOMAIN = "xras_geomagnetic"

BASE_URL = "https://xras.ru/txt"

# Наборы данных: ключ -> (префикс файла, обязателен ли)
DATASETS: dict[str, tuple[str, bool]] = {
    "fact": ("kp", True),                # Kp за трое суток, факт
    "forecast": ("kpf", False),          # Kp на трое суток, прогноз
    "month": ("kpm", False),             # суточные максимумы за текущий месяц
    "month_forecast": ("kpfl", False),   # суточные максимумы на месяц вперёд
}

CONF_CODE = "code"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_CODE = "UP03"
DEFAULT_SCAN_INTERVAL = 600
MIN_SCAN_INTERVAL = 300

USER_AGENT = "HomeAssistant-xras_geomagnetic/2.0 (+https://www.home-assistant.io)"

# Шкала как на xras.ru: до 4 — спокойная, 4…5 — возбуждённая, 5+ — бури G1–G5
STORM_LEVELS: list[tuple[float, str, str]] = [
    (0.0, "quiet", "Спокойная"),
    (4.0, "active", "Возбуждённая"),
    (5.0, "g1", "Слабая буря (G1)"),
    (6.0, "g2", "Средняя буря (G2)"),
    (7.0, "g3", "Сильная буря (G3)"),
    (8.0, "g4", "Очень сильная буря (G4)"),
    (9.0, "g5", "Экстремальная буря (G5)"),
]

STORM_THRESHOLD = 5.0
