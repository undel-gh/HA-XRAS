"""Разбор выгрузок xras.ru (формат version 1.1).

JSON:
    {"version":"1.1","type":"kp","tzone":"UTC+03","source":"noaa","N":"3",
     "data":[{"time":"2026-08-12","f10":"96","sn":"62","ap":"9","k":"2.67",
              "max_kp":"2.67","h00":"1.33",...,"h21":"null"}, ...]}

TXT: тот же набор полей, строки разделены символом «|», порядок колонок
описан в шапке строкой вида «# [0] time, [1] f10, ...».

Результат приводится к единому объекту Dataset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

# Запасной порядок колонок TXT — если шапка «# [0] time, ...» отсутствует.
# Ключ — число колонок в строке: месячная выгрузка короче трёхсуточной.
FALLBACK_COLUMNS: dict[int, list[str]] = {
    5: ["time", "f10", "sn", "ap", "max_kp"],
    6: ["time", "f10", "sn", "ap", "k", "max_kp"],
    14: [
        "time", "f10", "sn", "ap", "k", "max_kp",
        "h00", "h03", "h06", "h09", "h12", "h15", "h18", "h21",
    ],
}

_SLOT_RE = re.compile(r"^h(\d{2})$")
_COLUMN_RE = re.compile(r"\[(\d+)\]\s*([A-Za-z0-9_]+)")
_TZONE_RE = re.compile(r"UTC\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", re.IGNORECASE)
_CODE_RE = re.compile(r"^U([PM])(\d{2})(\d{2})?$", re.IGNORECASE)

_NULLS = {"", "null", "none", "nan", "-", "—", "n/a"}


class XrasParseError(ValueError):
    """Файл не удалось разобрать."""


@dataclass
class Day:
    """Одни сутки из выгрузки."""

    date: date
    f10: float | None = None
    sn: float | None = None
    ap: float | None = None
    k: float | None = None
    max_kp: float | None = None
    slots: dict[int, float] = field(default_factory=dict)  # час начала -> Kp


@dataclass
class Dataset:
    """Разобранный файл целиком."""

    type: str = ""
    version: str = ""
    tzone: str = ""
    source: str = ""
    tz: timezone = timezone.utc
    days: list[Day] = field(default_factory=list)

    @property
    def series(self) -> list[dict]:
        """Трёхчасовой ряд Kp с привязкой ко времени."""
        points: list[dict] = []
        for day in self.days:
            for hour, kp in sorted(day.slots.items()):
                ts = datetime(
                    day.date.year, day.date.month, day.date.day, hour, tzinfo=self.tz
                )
                points.append({"ts": ts, "kp": kp})
        points.sort(key=lambda point: point["ts"])
        return points

    @property
    def daily(self) -> list[dict]:
        """Суточные максимумы."""
        return [
            {"date": day.date, "kp": day.max_kp}
            for day in self.days
            if day.max_kp is not None
        ]

    def day_for(self, target: date) -> Day | None:
        return next((day for day in self.days if day.date == target), None)


# --- вспомогательные преобразования -------------------------------------


def tz_from_string(value: str | None) -> timezone | None:
    """«UTC+03» или «UP03» -> объект timezone."""
    if not value:
        return None

    text = value.strip()
    match = _TZONE_RE.search(text)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        return timezone(
            sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0))
        )

    match = _CODE_RE.match(text)
    if match:
        sign = 1 if match.group(1).upper() == "P" else -1
        return timezone(
            sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0))
        )

    return None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if text.lower() in _NULLS:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if text.lower() in _NULLS:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _day_from_mapping(row: dict[str, Any]) -> Day | None:
    lowered = {str(key).strip().lower(): value for key, value in row.items()}

    day_date = _to_date(lowered.get("time") or lowered.get("date"))
    if day_date is None:
        return None

    day = Day(
        date=day_date,
        f10=_to_float(lowered.get("f10") or lowered.get("f107")),
        sn=_to_float(lowered.get("sn")),
        ap=_to_float(lowered.get("ap")),
        k=_to_float(lowered.get("k")),
        max_kp=_to_float(lowered.get("max_kp") or lowered.get("maxkp")),
    )

    for key, value in lowered.items():
        match = _SLOT_RE.match(key)
        if not match:
            continue
        kp = _to_float(value)
        if kp is not None:
            day.slots[int(match.group(1))] = kp

    if day.max_kp is None and day.slots:
        day.max_kp = max(day.slots.values())

    return day


def _finalise(dataset: Dataset, tz_hint: str | None) -> Dataset:
    dataset.tz = (
        tz_from_string(dataset.tzone) or tz_from_string(tz_hint) or timezone.utc
    )
    dataset.days.sort(key=lambda day: day.date)
    if not dataset.days:
        raise XrasParseError("в файле нет ни одних суток")
    return dataset


# --- собственно разбор ---------------------------------------------------


def parse_json(payload: Any, tz_hint: str | None = None) -> Dataset:
    if not isinstance(payload, dict):
        raise XrasParseError("ожидался объект JSON")

    error = str(payload.get("error") or "").strip()
    if error:
        raise XrasParseError(f"сервер вернул ошибку: {error}")

    rows = payload.get("data")
    if not isinstance(rows, list):
        raise XrasParseError("в JSON нет массива data")

    dataset = Dataset(
        type=str(payload.get("type") or ""),
        version=str(payload.get("version") or ""),
        tzone=str(payload.get("tzone") or ""),
        source=str(payload.get("source") or ""),
    )

    for row in rows:
        if not isinstance(row, dict):
            continue
        day = _day_from_mapping(row)
        if day:
            dataset.days.append(day)

    return _finalise(dataset, tz_hint)


def parse_txt(text: str, tz_hint: str | None = None) -> Dataset:
    dataset = Dataset()
    columns: list[str] | None = None
    rows: list[list[str]] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("#"):
            comment = stripped.lstrip("#").strip()

            # Строка с описанием колонок: «[0] time, [1] f10, ...»
            found = _COLUMN_RE.findall(comment)
            if len(found) >= 3:
                ordered = sorted((int(index), name) for index, name in found)
                columns = [name.lower() for _, name in ordered]
                continue

            if ":" in comment:
                key, _, value = comment.partition(":")
                key, value = key.strip().lower(), value.strip()
                if key == "tzone":
                    dataset.tzone = value
                elif key == "type":
                    dataset.type = value
                elif key == "version":
                    dataset.version = value
                elif key == "source":
                    dataset.source = value
                elif key == "error" and value:
                    raise XrasParseError(f"сервер вернул ошибку: {value}")
            continue

        parts = [part.strip() for part in stripped.split("|")]
        if len(parts) >= 2:
            rows.append(parts)

    if not rows:
        raise XrasParseError("в TXT нет строк с данными")

    for parts in rows:
        names = columns or FALLBACK_COLUMNS.get(len(parts))
        if not names:
            # Неизвестная ширина — берём самую длинную раскладку и что влезет
            names = FALLBACK_COLUMNS[14]
        row = {names[i]: parts[i] for i in range(min(len(names), len(parts)))}
        day = _day_from_mapping(row)
        if day:
            dataset.days.append(day)

    return _finalise(dataset, tz_hint)
