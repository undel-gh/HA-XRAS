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

# ВАЖНО: держать в одном значении с "version" в manifest.json
VERSION = "2.2.0"
USER_AGENT = f"HomeAssistant-xras_geomagnetic/{VERSION} (+https://www.home-assistant.io)"

# Шкала как на xras.ru: до 4 — спокойная, 4…5 — возбуждённая, 5+ — бури G1–G5.
# Названия здесь не храним: они живут в translations/*.json как состояния ENUM.
STORM_LEVELS: list[tuple[float, str]] = [
    (0.0, "quiet"),
    (4.0, "active"),
    (5.0, "g1"),
    (6.0, "g2"),
    (7.0, "g3"),
    (8.0, "g4"),
    (9.0, "g5"),
]

STORM_THRESHOLD = 5.0
