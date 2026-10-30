"""Разбор выгрузок xras.ru (формат version 1.1).

JSON сейчас содержит, например, ``tzone = "Moscow (UTC+03)"`` и массив
``data`` в обратном хронологическом порядке. Парсер сортирует сутки по
возрастанию даты и приводит JSON/TXT к единому объекту :class:`Dataset`.

У регионов, чьё локальное смещение не кратно трём часам, XRAS добавляет
``h00`` как точку непрерывности графика. Например, для UTC+07 реальные начала
трёхчасовых интервалов — ``h01, h04, ... h22``; ``h00`` дублирует значение
интервала, начавшегося накануне. ``Dataset.series`` отбрасывает такие
служебные точки по фазе часового пояса.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

# Запасной порядок для коротких TXT без строки с описанием колонок.
FALLBACK_COLUMNS: dict[int, list[str]] = {
    5: ["time", "f10", "sn", "ap", "max_kp"],
    6: ["time", "f10", "sn", "ap", "k", "max_kp"],
}

_BASE_KP_COLUMNS = ["time", "f10", "sn", "ap", "k", "max_kp"]
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
    slots: dict[int, float] = field(default_factory=dict)  # локальный час -> Kp


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
        valid_hours = _valid_slot_hours(self.tz)

        for day in self.days:
            for hour, kp in sorted(day.slots.items()):
                if valid_hours is not None and hour not in valid_hours:
                    continue
                ts = datetime(
                    day.date.year,
                    day.date.month,
                    day.date.day,
                    hour,
                    tzinfo=self.tz,
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
    """«Moscow (UTC+03)», «UTC+03» или «UP03» -> объект timezone."""
    if not value:
        return None

    text = value.strip()
    match = _TZONE_RE.search(text)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        try:
            return timezone(
                sign
                * timedelta(
                    hours=int(match.group(2)),
                    minutes=int(match.group(3) or 0),
                )
            )
        except ValueError:
            return None

    match = _CODE_RE.match(text)
    if match:
        sign = 1 if match.group(1).upper() == "P" else -1
        try:
            return timezone(
                sign
                * timedelta(
                    hours=int(match.group(2)),
                    minutes=int(match.group(3) or 0),
                )
            )
        except ValueError:
            return None

    return None


def _valid_slot_hours(tz: timezone) -> set[int] | None:
    """Вернуть локальные часы начала 3-часовых UTC-интервалов.

    XRAS именует слоты только по целому часу. Для смещений с минутами безопасно
    не фильтровать: по текущему формату нельзя восстановить точную фазу.
    """
    offset = tz.utcoffset(None)
    if offset is None:
        return None
    seconds = int(offset.total_seconds())
    if seconds % 3600:
        return None
    phase = (seconds // 3600) % 3
    return {hour for hour in range(24) if hour % 3 == phase}


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
        hour = int(match.group(1))
        if hour > 23:
            continue
        kp = _to_float(value)
        if kp is not None:
            day.slots[hour] = kp

    return day


def _resolve_tz(dataset: Dataset, tz_hint: str | None) -> timezone:
    tz = tz_from_string(dataset.tzone) or tz_from_string(tz_hint)
    if tz is None:
        raise XrasParseError(
            f"не удалось определить часовую зону: {dataset.tzone or tz_hint!r}"
        )
    return tz


def _fallback_columns(
    width: int, dataset: Dataset, tz_hint: str | None
) -> list[str] | None:
    if width in FALLBACK_COLUMNS:
        return FALLBACK_COLUMNS[width]

    if width not in (14, 15):
        return None

    try:
        tz = _resolve_tz(dataset, tz_hint)
    except XrasParseError:
        return None

    valid_hours = _valid_slot_hours(tz)
    if valid_hours is None:
        return None

    slots = [f"h{hour:02d}" for hour in sorted(valid_hours)]
    if width == 15 and 0 not in valid_hours:
        # Региональный TXT добавляет h00 как точку непрерывности графика.
        slots.insert(0, "h00")

    names = [*_BASE_KP_COLUMNS, *slots]
    return names if len(names) == width else None


def _finalise(dataset: Dataset, tz_hint: str | None) -> Dataset:
    dataset.tz = _resolve_tz(dataset, tz_hint)
    valid_hours = _valid_slot_hours(dataset.tz)
    for day in dataset.days:
        if day.max_kp is not None or not day.slots:
            continue
        values = [
            kp
            for hour, kp in day.slots.items()
            if valid_hours is None or hour in valid_hours
        ]
        if values:
            day.max_kp = max(values)

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
        names = columns or _fallback_columns(len(parts), dataset, tz_hint)
        if not names:
            raise XrasParseError(
                f"неизвестное число колонок TXT без шапки: {len(parts)}"
            )
        if len(parts) != len(names):
            raise XrasParseError(
                f"число колонок TXT не совпадает с шапкой: {len(parts)} != {len(names)}"
            )
        row = dict(zip(names, parts, strict=True))
        day = _day_from_mapping(row)
        if day:
            dataset.days.append(day)

    return _finalise(dataset, tz_hint)
