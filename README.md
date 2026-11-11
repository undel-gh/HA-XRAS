[![Validate](https://github.com/undel-gh/HA-XRAS/actions/workflows/main.yml/badge.svg)](https://github.com/undel-gh/HA-XRAS/actions/workflows/main.yml)
[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
![Home Assistant Custom Integration](https://img.shields.io/badge/Home%20Assistant-Custom%20Integration-18BCF2?style=flat-square)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[![Open your Home Assistant instance and open the HA-XRAS repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=undel-gh&repository=HA-XRAS&category=integration)

[**English**](#xras--geomagnetic-conditions-for-home-assistant) | [**Русский**](#xras--геомагнитная-обстановка-для-home-assistant)

# XRAS — Geomagnetic Conditions for Home Assistant

The integration uses data from the Laboratory of Solar Astronomy at the Space
Research Institute of the Russian Academy of Sciences (IKI RAS)
(<https://xras.ru>): actual and forecast Kp index values, as well as Ap, F10.7,
and the sunspot number.

## Data Sources

All files are located in `https://xras.ru/txt/` and are named
`<prefix>_<code>.json` (or `.txt`). The integration downloads four files:

| Prefix | Contents | Required |
| --- | --- | --- |
| `kp` | Kp for three days, actual values (three-hour intervals) | yes |
| `kpf` | Kp forecast for three days | no |
| `kpm` | Daily maximum Kp for the current month | no |
| `kpfl` | Daily maximum Kp for the month ahead | no |

### Region Code

`<code>` is either a time zone (`UP03` = UTC+03, `UM05` = UTC−05) or the code
of a specific region. The codes are opaque and cannot be guessed:

| Region | Code |
| --- | --- |
| Moscow | `RAL5` |
| Riga | `RIK0` |
| Norilsk | `NJ7D` |
| general UTC+03 zone | `UP03` |

**You do not have to enter the code manually.** During setup, the integration
loads the official XRAS region index at `https://xras.ru/js/regions`, which
contains the API code, display name, and Latin slug for each region. You can
therefore enter, for example, “Moscow”, “Krasnodar”, “New York”, `moscow`,
`saint_petersburg`, or the code itself, such as `RAL5`/`RIK0`. Name comparison
ignores case, `_`/`-` separators, and the difference between `ё`/`е`.

A successfully downloaded index is cached in Home Assistant memory for one
hour. If XRAS subsequently becomes temporarily unable to serve the index, the
last successful cache is used. For compatibility, legacy known Russian aliases
and explicit Latin slugs may also be resolved through the region page even
when the index is unavailable.

If one display name corresponds to multiple regions, the integration does not
choose the first one at random: the form asks you to specify a Latin slug or
API code. For example, the current XRAS index contains two regions named
“Hamilton” — `hamilton_canada` (`HD5U`) and `hamilton_new_zeland` (`HRFI`).

If automatic detection does not work, you can find the code yourself: open
your region's page and hover over the “API: JSON” link below the chart. It
will contain something like `.../txt/kp_RAL5.json`.

A regional code is preferable to the `UP03` zone: regional files account for
daylight saving time, whereas `UP03` is a fixed offset. For Moscow there is no
difference (it uses UTC+3 all year), but Riga switches to UTC+2 in winter.

The integration tries JSON first and automatically falls back to TXT if it is
unavailable; both formats are parsed identically. An unknown time zone is not
replaced with UTC: the required file is considered invalid so that the Kp value
for the wrong three-hour interval is not displayed.

## Installation

1. Copy `custom_components/xras_geomagnetic` to `<config>/custom_components/`.
2. Restart Home Assistant.
3. **Settings → Devices & services → Add integration → XRAS**.
4. Enter a region (“Moscow”) or code (`RAL5`) and the polling interval. The
   value is validated immediately: if the region is not found or the data
   cannot be downloaded or parsed, the form displays the corresponding error.

Via HACS: *Custom repositories* → repository URL → type *Integration*.

The region code is the identity of the config entry and cannot be changed
after creation. In **Configure**, you can change the polling interval and
enable historical loading of daily maximum Kp values. If you need a different
region, delete the entry and create a new one so that the histories of
different regions are not mixed.

When older version 2 config entries are updated, they are migrated
automatically: if the region was previously changed through Options, the code
actually in use is moved to the main configuration and becomes the entry's
`unique_id`. If this conflicts with an existing entry for the same region,
migration stops instead of creating a duplicate.

## Historical Kp Statistics

Historical loading is **disabled by default** so that installing the
integration does not create additional requests to XRAS. In **Configure**, the
“History to load, months” option can be set from `1` to `24`; `0` completely
disables further history synchronization. Recorder must be enabled for this
feature; without it, regular XRAS entities continue to work, but backfill does
not start.

The XRAS archive `kpm_<code>_YYYYMM` contains only the **maximum Kp for each
calendar day**. This file does not contain the original three-hour values, so
the integration does not mix the archive into the `kp_current` sensor's
Long-Term Statistics: doing so would distort its `min`/`mean`/`max`. Instead,
a separate external statistic is created:

```text
xras_geomagnetic:kp_daily_max_ral5
```

The last part is the lowercase region code (`RIK0` →
`xras_geomagnetic:kp_daily_max_rik0`). You can use this statistic directly in
Home Assistant cards, for example:

```yaml
type: statistics-graph
entities:
  - xras_geomagnetic:kp_daily_max_ral5
days_to_show: 365
period: day
stat_types:
  - max
```

No additional requests are made for the current month: daily maxima are taken
from the regular `kpm_<code>` file, which the coordinator already downloads.
For completed months, the `kpm_<code>_YYYYMM` archives are used: they are
requested sequentially with a pause of at least 5 minutes. Older, fully
imported months are not downloaded again. The most recent completed month is
deliberately checked against its archive once per manager lifecycle (and after
the transition to a new month), even if its last day is already in Recorder:
XRAS may switch the live `kpm_<code>` file to a new month before it contains
the final maximum for the last day. The archive check authoritatively
overwrites such a point. A partially imported older month remains a candidate
for recovery.

After queuing statistics in Recorder, the integration waits for the commit
barrier and reads the corresponding rows back. Only then is the current
snapshot considered synchronized, and an archived month increments the
successfully imported month counter. If Recorder has not saved the expected
rows, backfill enters the same `retry_wait` state instead of reporting success
prematurely.

After a network error, XRAS unavailability, or an ambiguous failure of one of
the archive formats, backfill pauses for at least 6 hours and does not skip the
failed month; the number of such network attempts is unlimited. If both formats
are available or absent (`404`), but the available content is invalid (a parse
error, a month mismatch, or an excessive size), up to **three attempts** are
made with a pause of at least 6 hours between them. After the third error, the
month is marked as `archive_invalid_skipped` until the integration is
restarted, and processing of the remaining months continues with the usual
interval of at least 5 minutes between requests.

A missing old archive (`404`) is remembered until the integration is
restarted. The exception is the **most recent completed month**: after the
transition to the next month, XRAS may publish its archive with a delay. On a
`404`, that month is marked as `fresh_archive_pending` and checked again no
sooner than 6 hours later; older months can still be imported during the
current pass. If the archive has not appeared by the next month transition,
its `404` is handled like that of a regular old archive. Retries run on
subsequent successful coordinator updates after the cooldown; there is no
separate permanent timer.

History errors do not affect regular XRAS entities. Diagnostics show
`archive_invalid_attempts`, `archive_invalid_skipped`,
`fresh_archive_pending`, `archive_not_found`, the backfill state, and the
reason for the last error. Attempt counters and skip lists live in manager
memory and are reset when the integration is restarted; statistics already
written are retained. The `history_backfill.archive_imported_months` field
contains a sorted list of `YYYY-MM` months successfully confirmed through
Recorder during the current manager lifecycle. This list and
`imported_archive_months_this_session` are reset when the integration is
restarted; the saved statistical data itself is not deleted.

Each value is written with a neutral time marker **around noon on the XRAS
source's calendar day**, rather than at a fixed `12:00 UTC`. First, `12:00` in
the source time zone is taken; then the marker is rounded down to a whole UTC
hour, as required for Recorder hourly statistics. Therefore, for a whole-hour
zone offset this is exactly 12:00 local time, while for UTC+05:30, for example,
it is 11:30 local time. The marker remains within the source day and does not
move the date to an adjacent day, even for UTC+12…UTC+14. This is **not the
time when the maximum occurred**: XRAS reports only the daily value and does
not report the three-hour interval in which it was reached. For the current
day, the point appears only after this already aligned marker has passed, so
no future statistical marker is created in Recorder.

Reducing the history depth or disabling backfill (`0`) stops further
synchronization of the corresponding older months, but does not delete
Long-Term Statistics already imported into Recorder.

## Entities

| Key | Meaning |
| --- | --- |
| `kp_current` | Current Kp only for the active three-hour interval |
| `storm_level` | Quiet / Active / G1–G5 storms |
| `kp_max_today` | `max_kp` for today |
| `kp_max_24h` | Maximum actual value over the past 24 hours |
| `kp_max_month` | Maximum for the current month; attributes contain daily history |
| `storm_days_month` | Number of days in the month that reached Kp ≥ 5 |
| `kp_forecast_max_24h` | Maximum forecast for the next 24 hours |
| `kp_forecast_max_3d` | Maximum of the available three-day forecast |
| `ap` | Ap (the `forecast` attribute contains the forecast strictly for tomorrow) |
| `f10` | F10.7, s.f.u. (the `forecast` attribute contains the forecast strictly for tomorrow) |
| `sn` | Sunspot number; disabled by default |
| `storm` | Binary sensor: `on` when Kp ≥ 5 in the current valid interval |
| `storm_forecast` | Binary sensor: `on` if Kp ≥ 5 is forecast within the next 24 hours |

Daily `Ap` is the average of eight three-hour `ap` indices, so it is not
declared as `state_class: measurement`: it is a daily aggregate, not a current
measurement. `F10.7` and `SN` retain `measurement` as the latest available
observed values. For all three values, if there is no new row for today, the
integration may show the latest available value from a previous day: this is
not a forecast and does not guarantee that the value is current for today.
After `state_class` is removed from `Ap`, new Long-Term Statistics are no
longer generated for this sensor; values already recorded by Recorder are
retained.

The current Kp sensor has `measured_at` and `valid_until` attributes. At a slot
boundary, the integration does not publish an artificial intermediate
`unknown`: after a short random delay of 5–60 seconds, it requests the new
slot. If XRAS has already updated, the state changes directly from the old Kp
to the new one; if the new slot is not yet available, it changes to `unknown`;
if a required request fails, the entities normally become `unavailable`. Up to
three retries are then made no more often than once every 5 minutes, also with
a small jitter. In the `unknown` state, the `measured_at` and `valid_until`
attributes retain the boundaries of the last actual slot, making it clear why
the value is no longer considered current.

If Home Assistant starts or a regular poll falls into a short “gap” between
slots, the integration starts the same bounded recovery if the last
`valid_until` expired no more than 15 minutes ago. Older gaps do not create
additional burst requests and wait for the regular polling interval.

### Entity ID

The exact `entity_id` is not part of the integration API. Entities use
`has_entity_name=True`: Home Assistant builds the initial object ID from the
device name (`XRAS {code}`) and the entity name. For languages that HA does not
use for native entity IDs (including Russian), the object ID uses the
translation in the default language, so a Russian display name does not imply
Cyrillic transliteration in the `entity_id`. IDs that have already been
created or manually renamed are preserved.

Therefore, before copying YAML, get the actual IDs from **Settings → Devices &
services → XRAS → Entities**. The examples below deliberately use explicit
placeholder IDs such as `sensor.your_kp_entity` — replace them with your own.

### Level Keys vs. Translated Text

The conditions level is stored everywhere as a key: `quiet`, `active`,
`g1`…`g5`. This applies both to the state of the geomagnetic conditions ENUM
sensor and to the `level_key` attributes of the binary sensors and forecast Kp.
In automation conditions, compare against the key: it does not depend on the
interface language and will not change when wording changes.

Home Assistant itself supplies the translated text (“Minor storm (G1)”) when
displaying the entity, so there is no need to duplicate it in attributes: if
the name is required in a template or notification, take it from the ENUM
sensor's state, where it is already localized:

```jinja
{{ states('sensor.your_storm_level_entity') }}
{{ state_attr('binary_sensor.your_storm_now_entity', 'level_key') }}
```

Entity and state names come from `translations/`, so they will be in English
in an English-language interface.

Monthly sensors depend on the `kpm_*` file: if it is unavailable, they remain
empty (`unknown`) rather than substituting values from the three-day file. The
availability status of the required and optional datasets can be viewed in the
integration entry's diagnostics.

### Level Scale

As on the website: **Quiet** — Kp < 4, **Active** — 4 ≤ Kp < 5, **G1…G5
storms** — Kp from 5 to 9. Verified against the colors of the monthly chart:
August 2 and 8 (Kp = 5.67) are red, August 3 and 4 (4.33 and 4.00) are orange,
and August 9 (3.67) is green.

### Kp Rounding and Notation

The Kp file contains fractional values in thirds: `1.33`, `1.67`, `2.67`,
`3.33`. The sensor state is the exact fractional value (which is more
convenient for automations, charts, and thresholds), while two human-readable
representations are available in attributes:

* `kp_display` — an integer rounded “half up” (`math.floor(kp + 0.5)`, via
  `decimal.ROUND_HALF_UP`). The built-in `round()` is unsuitable here because
  it rounds to even, meaning `round(2.5) == 2`. Exact halves do not occur in
  real data expressed in thirds, so this is future-proofing.
* `kp_notation` — the familiar notation in thirds, as used in summaries on the
  website: `1.67` → `2-`, `7.33` → `7+`, `8.00` → `8`.

The current Kp attributes also contain `series` (actual values for 3 days) and
`forecast_series` (forecast) — ready-made series for charts.

## Cards

```yaml
type: entities
title: Geomagnetic conditions
entities:
  - entity: sensor.your_storm_level_entity
  - entity: sensor.your_kp_entity
  - entity: sensor.your_kp_forecast_24h_entity
  - entity: sensor.your_ap_entity
  - entity: sensor.your_f107_entity
  - entity: binary_sensor.your_storm_forecast_entity
```

“Actual + forecast” chart ([ApexCharts Card](https://github.com/RomRider/apexcharts-card)):

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Kp index — actual and forecast
graph_span: 6d
span:
  start: day
  offset: "-3d"
series:
  - entity: sensor.your_kp_entity
    name: Actual
    type: column
    color: "#00e000"
    data_generator: |
      return entity.attributes.series
        .map(p => [new Date(p.time).getTime(), p.kp]);
  - entity: sensor.your_kp_entity
    name: Forecast
    type: column
    color: "#4a9eff"
    opacity: 0.5
    data_generator: |
      return entity.attributes.forecast_series
        .map(p => [new Date(p.time).getTime(), p.kp]);
yaxis:
  - min: 0
    max: 9
    decimals: 0
```

Tile with level-based color (mushroom):

```yaml
type: custom:mushroom-template-card
primary: "Kp {{ state_attr('sensor.your_kp_entity','kp_display') }}"
secondary: "{{ states('sensor.your_storm_level_entity') }}"
icon: mdi:magnet
icon_color: >
  {% set raw = states('sensor.your_kp_entity') %}
  {% if not is_number(raw) %} disabled
  {% else %}
    {% set kp = raw | float %}
    {% if kp >= 7 %} red
    {% elif kp >= 5 %} orange
    {% elif kp >= 4 %} amber
    {% else %} green {% endif %}
  {% endif %}
```

## Automation

```yaml
automation:
  - alias: Geomagnetic storm warning
    triggers:
      - trigger: state
        entity_id: binary_sensor.your_storm_forecast_entity
        to: "on"
    actions:
      - action: notify.mobile_app
        data:
          title: Geomagnetic storm expected
          message: >
            {{ states('sensor.your_storm_level_entity') }},
            forecast Kp up to
            {{ states('sensor.your_kp_forecast_24h_entity') }}
```

For a “storm has ended” notification, specify `to: "off"`, not just
`from: "on"`: then a temporary `unavailable` state caused by a network error
will not be interpreted as the end of the storm.

## Alternative without the Integration (YAML)

If you do not want to install the component, use the standard `rest` sensor.
The data in the file runs from newer days to older days. The example below is
specifically for the Moscow UTC+03 file; regions with a different time-zone
phase may have a different set of `hXX` fields and local-time rules.

```yaml
rest:
  - resource: https://xras.ru/txt/kp_RAL5.json
    scan_interval: 600
    headers:
      User-Agent: Home Assistant
    sensor:
      - name: Daily maximum Kp
        unique_id: xras_kp_max
        value_template: "{{ value_json.data[0].max_kp | float }}"
      - name: Current Kp
        unique_id: xras_kp_now
        state_class: measurement
        value_template: >
          {% set now_ts = as_timestamp(now()) %}
          {% set ns = namespace(v=none) %}
          {% for d in value_json.data if ns.v is none %}
            {% for h in [21,18,15,12,9,6,3,0] if ns.v is none %}
              {% set key = 'h%02d' | format(h) %}
              {% set raw = d[key] %}
              {% set start = as_timestamp(d.time ~ 'T' ~ ('%02d' | format(h)) ~ ':00:00+03:00') %}
              {% if raw not in [none, 'null'] and start <= now_ts < start + 10800 %}
                {% set ns.v = raw | float %}
              {% endif %}
            {% endfor %}
          {% endfor %}
          {{ ns.v if ns.v is not none else 'unknown' }}
      - name: Ap
        unique_id: xras_ap
        value_template: "{{ value_json.data[0].ap | int }}"
      - name: F10.7
        unique_id: xras_f107
        value_template: "{{ value_json.data[0].f10 | int }}"

  # Monthly export: data[0] is the 31st (still null); use the first non-empty day
  - resource: https://xras.ru/txt/kpm_RAL5.json
    scan_interval: 3600
    headers:
      User-Agent: Home Assistant
    sensor:
      - name: Monthly maximum Kp
        unique_id: xras_kp_month
        value_template: >
          {{ value_json.data
             | rejectattr('max_kp', 'eq', 'null')
             | map(attribute='max_kp') | map('float')
             | list | max }}
      - name: Storm days in month
        unique_id: xras_storm_days
        value_template: >
          {{ value_json.data
             | rejectattr('max_kp', 'eq', 'null')
             | map(attribute='max_kp') | map('float')
             | select('ge', 5) | list | count }}
```

Unlike the old approach of “taking the last non-empty hXX”, this template
checks that the selected Moscow slot actually covers the current moment. It is
still a simplified fallback and does not replace the integration parser for
regions with a different phase or DST.

## Development and Tests

CI runs pytest on three explicitly pinned Home Assistant profiles. Each
profile uses its own `pytest-homeassistant-custom-component`, which pins
compatible versions of Home Assistant and pytest:

| Profile | Home Assistant | Python | Requirements |
| --- | --- | --- | --- |
| minimum supported | `2024.12.0` | `3.12` | `requirements_test_minimum.txt` |
| production | `2026.10.1` | `3.14` | `requirements_test.txt` |
| latest stable | `2026.10.4` | `3.14` | `requirements_test_latest.txt` |

For a local run, choose the requirements file matching your Python version.
For example, the production profile requires Python 3.14:

```bash
python -m pip install -r requirements_test.txt
python -m pytest -q
```

The minimum profile additionally pins `pycares==4.5.0`: the HA 2024.12.0
dependency allows newer versions without an upper bound, while modern
`pycares` is already incompatible with the cleanup check in the old test
harness.

Fixtures in `tests/fixtures/` cover JSON/TXT, reverse date order, string
`"null"` values, named time zones, and regional slot phase (including UTC+07
with `h00` as a continuity point).

## Notes

* The data belongs to IKI RAS — credit the source and do not poll the site more
  often than once every 5 minutes (the integration enforces this; recovery
  after a slot boundary adds jitter and remains limited to a bounded number of
  attempts).
* HTTP response sizes are limited: up to 256 KiB for the JSON/TXT API, up to
  128 KiB for the region index, and up to 1 MiB for a region page. Larger
  responses are rejected. During setup, this appears as a regular
  `cannot_connect` error. At runtime, an oversized required `kp_*` response is
  treated as an update error, so entities become `unavailable` until the next
  successful cycle; an oversized optional dataset makes only its data
  unavailable. The exact reason remains in the debug log.
* The original source of Kp is NOAA (the `source` field), so the values match
  NOAA SWPC.
* Empty values are returned as the string `"null"`: in the three-day file,
  this is how future three-hour intervals appear; in the monthly file, all
  days through the end of the month appear this way. They are not included in
  series or statistics.
* `kp_` contains the fields `k` and local slots `hXX`. For time zones whose
  offset is not a multiple of three hours relative to UTC, the slot phase is
  shifted. For example, for UTC+07 the actual interval starts are `h01`,
  `h04`, …, `h22`, while XRAS uses `h00` as a chart continuity point and it is
  not considered a separate three-hour interval.
* XRAS provides one `tzone` field for the entire three-day file. If a regional
  file spans a transition to or from daylight saving time, the parser has to
  apply this single fixed offset to all days in the file. Consequently, slots
  on the other side of the DST transition may be shifted by one hour until the
  corresponding days leave the three-day window. After `tzone` changes, the
  other side of the transition may be shifted instead. This is a limitation of
  the source format; a zone code such as `UP03` additionally never changes its
  offset with the season.
* `kpm_` has no hourly slots — only `time`, `f10`, `sn`, `ap`, `max_kp`. The
  TXT column order is read from the `# [0] time, ...` header; fallback without
  a header is allowed only for known schemas and does not guess an unknown file
  width.
* The `type` field is the same in both files (`kp`), so it cannot be used to
  distinguish them — the integration relies on the filename.
* The website has a monthly archive: `kpm_RAL5_202506.json` — the same format
  plus the `_YYYYMM` suffix. When historical loading is enabled, the
  integration uses these files only for the separate daily maximum Kp
  statistic; the original three-hour series cannot be reconstructed from them.

[**English**](#xras--geomagnetic-conditions-for-home-assistant) | [**Русский**](#xras--геомагнитная-обстановка-для-home-assistant)

# XRAS — Геомагнитная обстановка для Home Assistant

Интеграция берёт данные Лаборатории солнечной астрономии ИКИ РАН
(<https://xras.ru>): факт и прогноз индекса Kp, а также Ap, F10.7 и число
солнечных пятен.

## Источники данных

Все файлы лежат в `https://xras.ru/txt/` и называются `<префикс>_<код>.json`
(или `.txt`). Интеграция скачивает четыре файла:

| Префикс | Что | Обязателен |
| --- | --- | --- |
| `kp` | Kp за трое суток, факт (трёхчасовые интервалы) | да |
| `kpf` | Kp на трое суток, прогноз | нет |
| `kpm` | Максимум Kp по суткам за текущий месяц | нет |
| `kpfl` | Максимум Kp по суткам на месяц вперёд | нет |

### Код региона

`<код>` — это либо часовая зона (`UP03` = UTC+03, `UM05` = UTC−05), либо код
конкретного региона. Коды непрозрачные, угадать их нельзя:

| Регион | Код |
| --- | --- |
| Москва | `RAL5` |
| Рига | `RIK0` |
| Норильск | `NJ7D` |
| общая зона UTC+03 | `UP03` |

**Вводить код вручную не обязательно.** При настройке интеграция загружает
официальный индекс XRAS `https://xras.ru/js/regions`, где для каждого региона
указаны API-код, отображаемое название и латинский slug. Поэтому можно ввести,
например, «Москва», «Краснодар», «Нью-Йорк», `moscow`, `saint_petersburg` или
сам код `RAL5`/`RIK0`. При сравнении названий не учитываются регистр,
разделители `_`/`-` и различие `ё`/`е`.

Успешно загруженный индекс кэшируется в памяти Home Assistant на один час. Если
после этого XRAS временно не отдаёт индекс, используется последний успешный
кэш. Для совместимости старые известные русские алиасы и явные латинские slug
могут быть разрешены через страницу региона даже при недоступном индексе.

Если одно отображаемое название соответствует нескольким регионам, интеграция
не выбирает первый случайно: форма попросит указать латинский slug или API-код.
Например, в текущем индексе XRAS есть два региона с названием «Гамильтон» —
`hamilton_canada` (`HD5U`) и `hamilton_new_zeland` (`HRFI`).

Если автоопределение не сработает, посмотрите код сами: откройте страницу
своего региона и наведитесь на ссылку «API: JSON» под графиком — там будет,
например, `.../txt/kp_RAL5.json`.

Региональный код лучше зоны `UP03`: региональные файлы учитывают переход на
летнее время, а `UP03` — это жёстко зафиксированное смещение. Для Москвы
разницы нет (там круглый год UTC+3), а вот Рига зимой уходит на UTC+2.

Интеграция сначала пробует JSON, при недоступности автоматически переходит на
TXT — обе схемы разбираются одинаково. Неизвестная часовая зона не
подменяется на UTC: обязательный файл считается некорректным, чтобы не
показывать Kp не того трёхчасового интервала.

## Установка

1. Скопируйте `custom_components/xras_geomagnetic` в `<config>/custom_components/`.
2. Перезапустите Home Assistant.
3. **Настройки → Устройства и службы → Добавить интеграцию → XRAS**.
4. Введите регион («Москва») или код (`RAL5`) и интервал опроса. Значение
   проверяется сразу: если регион не найден или данные не скачались/не
   разобрались, форма покажет соответствующую ошибку.

Через HACS: *Custom repositories* → адрес репозитория → тип *Integration*.

Код региона является идентичностью записи и после создания не меняется.
В **Настроить** можно изменить интервал опроса и включить историческую загрузку
суточных максимумов Kp. Если нужен другой регион, удалите запись и создайте
новую — так история разных регионов не смешивается.

При обновлении старых записей конфигурации версии 2 выполняется автоматическая
миграция: если регион ранее меняли через Options, фактически используемый код
переносится в основную конфигурацию и становится `unique_id` записи. При
конфликте с уже существующей записью того же региона миграция останавливается,
а не создаёт дубликат.

## Историческая статистика Kp

Историческая загрузка **выключена по умолчанию**, чтобы установка интеграции не
создавала дополнительные запросы к XRAS. В **Настроить** параметр «История для
загрузки, мес.» можно установить от `1` до `24`; `0` полностью отключает
дальнейшую синхронизацию истории. Для этой функции должен быть включён Recorder;
без него обычные сущности XRAS продолжают работать, а backfill не запускается.

Архив XRAS `kpm_<код>_ГГГГММ` содержит только **максимум Kp за календарные
сутки**. Исходных трёхчасовых значений в этом файле нет, поэтому интеграция не
подмешивает архив в Long-Term Statistics сенсора `kp_current`: это исказило бы
его `min`/`mean`/`max`. Вместо этого создаётся отдельная внешняя статистика
(external statistic):

```text
xras_geomagnetic:kp_daily_max_ral5
```

Последняя часть — код региона в нижнем регистре (`RIK0` →
`xras_geomagnetic:kp_daily_max_rik0`). Эту статистику можно указывать прямо в
карточках Home Assistant, например:

```yaml
type: statistics-graph
entities:
  - xras_geomagnetic:kp_daily_max_ral5
days_to_show: 365
period: day
stat_types:
  - max
```

Для текущего месяца дополнительные запросы не выполняются: суточные максимумы
берутся из обычного `kpm_<код>`, который coordinator и так загружает. Для
завершённых месяцев используются архивы `kpm_<код>_ГГГГММ`: они запрашиваются
последовательно с паузой не менее 5 минут. Старые полностью импортированные
месяцы не скачиваются снова. Самый свежий завершённый месяц намеренно один раз
за lifecycle менеджера (и после перехода на новый месяц) сверяется с архивом,
даже если его последний день уже есть в Recorder: XRAS может переключить live
`kpm_<код>` на новый месяц до того, как в нём оказался окончательный максимум
последнего дня. Архивная сверка авторитетно перезаписывает такую точку.
Частичный более старый месяц остаётся кандидатом на восстановление.

После постановки статистики в очередь Recorder интеграция ждёт commit barrier и
перечитывает соответствующие строки. Только после этого текущий snapshot
считается синхронизированным, а архивный месяц увеличивает счётчик успешно
импортированных месяцев. Если Recorder не сохранил ожидаемые строки, backfill
переходит в тот же `retry_wait`, а не сообщает преждевременный успех.

При сетевой ошибке, недоступности XRAS или неоднозначном сбое одного из
форматов архива backfill приостанавливается минимум на 6 часов и не обходит
сбойный месяц; число таких сетевых попыток не ограничено. Если оба формата
доступны или отсутствуют (`404`), но доступное содержимое некорректно
(ошибка разбора, несоответствие месяцу, превышение размера), выполняется до
**трёх попыток** с паузой минимум 6 часов между ними. После третьей ошибки
такой месяц помечается как `archive_invalid_skipped` до перезапуска
интеграции, а обработка остальных месяцев продолжается с обычным интервалом
не менее 5 минут между запросами.

Отсутствующий старый архив (`404`) запоминается до перезапуска интеграции.
Исключение — **самый свежий завершённый месяц**: после перехода на следующий
месяц XRAS может опубликовать архив с задержкой. При `404` этот месяц
отмечается как `fresh_archive_pending` и проверяется повторно не раньше чем
через 6 часов; старые месяцы при этом можно импортировать уже в текущем
проходе. Если архив так и не появился до очередного перехода месяца, его
`404` обрабатывается как для обычного старого архива. Повторные попытки
запускаются на следующих успешных обновлениях coordinator после cooldown;
это не отдельный постоянный таймер.

Ошибки истории не влияют на обычные сущности XRAS. Диагностика показывает
`archive_invalid_attempts`, `archive_invalid_skipped`,
`fresh_archive_pending`, `archive_not_found`, состояние backfill и причину
последней ошибки. Счётчики попыток и списки пропусков живут в памяти менеджера
и обнуляются при перезапуске интеграции; уже записанная статистика остаётся.
Поле
`history_backfill.archive_imported_months` содержит отсортированный список
месяцев `YYYY-MM`, успешно подтверждённых через Recorder в текущем lifecycle
менеджера. Список и `imported_archive_months_this_session` сбрасываются при
перезапуске интеграции; сами сохранённые статистические данные не удаляются.

Каждое значение записывается с нейтральным часовым маркером **около полудня в
календарных сутках источника XRAS**, а не с фиксированным `12:00 UTC`. Сначала
берётся `12:00` в зоне источника, затем метка выравнивается вниз на целый
UTC-час, как требуется для hourly statistics Recorder. Поэтому при целом
смещении зоны это ровно 12:00 local, а, например, при UTC+05:30 — 11:30 local.
Маркер при этом остаётся внутри исходных суток и не переносит дату на соседний
день даже для UTC+12…UTC+14. Это **не время достижения максимума**: XRAS
сообщает только суточное значение и не сообщает, в каком трёхчасовом интервале
оно было достигнуто. Для текущих суток точка появляется только после наступления
этого уже выровненного маркера, поэтому будущая статистическая метка в Recorder
не создаётся.

Уменьшение глубины истории или отключение backfill (`0`) прекращает дальнейшую
синхронизацию соответствующих старых месяцев, но не удаляет уже импортированную
Long-Term Statistics из Recorder.

## Сущности

| Ключ | Значение |
| --- | --- |
| `kp_current` | Текущий Kp только для действующего трёхчасового интервала |
| `storm_level` | Спокойная / Возбуждённая / бури G1–G5 |
| `kp_max_today` | `max_kp` за сегодня |
| `kp_max_24h` | Максимум факта за прошедшие 24 часа |
| `kp_max_month` | Максимум за текущий месяц; в атрибутах — история по суткам |
| `storm_days_month` | Сколько суток месяца достигли Kp ≥ 5 |
| `kp_forecast_max_24h` | Максимум прогноза на ближайшие 24 ч |
| `kp_forecast_max_3d` | Максимум доступного трёхсуточного прогноза |
| `ap` | Ap (в атрибуте `forecast` — прогноз строго на завтра) |
| `f10` | F10.7, s.f.u. (в атрибуте `forecast` — прогноз строго на завтра) |
| `sn` | Число солнечных пятен; по умолчанию выключен |
| `storm` | Бинарный сенсор: `on` при Kp ≥ 5 в текущем валидном интервале |
| `storm_forecast` | Бинарный сенсор: `on`, если Kp ≥ 5 прогнозируется в ближайшие 24 ч |

Суточный `Ap` — среднее восьми трёхчасовых индексов `ap`, поэтому он не
объявлен `state_class: measurement`: это агрегат за сутки, а не текущее
измерение. `F10.7` и `SN` сохраняют `measurement` как последние доступные
наблюдательные показатели. Для всех трёх значений при отсутствии новой строки
за сегодня интеграция может показывать последнее доступное значение за
предыдущие сутки: это не прогноз и не гарантия свежести за текущий день.
После удаления `state_class` у `Ap` новая Long-Term Statistics этого
сенсора не формируется; уже записанные значения Recorder сохраняются.

У текущего Kp есть атрибуты `measured_at` и `valid_until`. На границе слота
интеграция не публикует искусственный промежуточный `unknown`: после небольшой
случайной задержки 5–60 секунд выполняется запрос нового слота. Если XRAS уже
обновился, состояние переходит сразу со старого Kp на новый; если свежего
слота ещё нет — в `unknown`; при ошибке обязательного запроса сущности штатно
становятся `unavailable`. Затем выполняется до трёх повторов не чаще чем раз в
5 минут, также с небольшим jitter. При `unknown` атрибуты `measured_at` и
`valid_until` сохраняют границы последнего фактического слота, чтобы было видно,
почему значение уже не считается текущим.

Если Home Assistant запустился или обычный опрос попал в короткую «дыру» между
слотами, интеграция запускает тот же bounded recovery, если последний
`valid_until` истёк не более 15 минут назад. Более старые пропуски не создают
дополнительных burst-запросов и ждут штатного интервала опроса.

### Entity ID

Точный `entity_id` не является частью API интеграции. Сущности используют
`has_entity_name=True`: Home Assistant строит исходный object ID из имени
устройства (`XRAS {code}`) и имени сущности. Для языков, которые HA не
использует для native entity IDs (в том числе русского), для object ID берётся
перевод на языке по умолчанию, поэтому русское отображаемое имя не означает
кириллическую транслитерацию в `entity_id`. Уже созданные или вручную
переименованные ID при этом сохраняются.

Поэтому перед копированием YAML возьмите фактические ID из **Настройки →
Устройства и службы → XRAS → Сущности**. В примерах ниже намеренно используются
явные placeholder-ID вида `sensor.your_kp_entity` — замените их своими.

### Ключи уровней против переведённого текста

Уровень обстановки везде хранится ключом: `quiet`, `active`, `g1`…`g5`. Это
касается и состояния ENUM-сенсора геомагнитной обстановки, и атрибутов
`level_key` у бинарных сенсоров и у прогнозного Kp. В условиях автоматизаций
сравнивайте именно с ключом — он не зависит от языка интерфейса и не поедет
при смене формулировок.

Переведённый текст («Слабая буря (G1)») подставляет сам Home Assistant при
отображении сущности, поэтому дублировать его в атрибутах не нужно: если
название требуется в шаблоне или уведомлении, берите его из состояния
ENUM-сенсора — там оно уже локализовано:

```jinja
{{ states('sensor.your_storm_level_entity') }}
{{ state_attr('binary_sensor.your_storm_now_entity', 'level_key') }}
```

Названия сущностей и состояний берутся из `translations/`, поэтому в
англоязычном интерфейсе они будут английскими.

Месячные сенсоры зависят от файла `kpm_*`: если он недоступен, они останутся
пустыми (`unknown`), а не подставят значения за трое суток. Состояние
доступности обязательного и необязательных наборов можно посмотреть в
диагностике записи интеграции.

### Шкала уровней

Как на сайте: **Спокойная** — Kp < 4, **Возбуждённая** — 4 ≤ Kp < 5,
**бури G1…G5** — Kp от 5 до 9. Проверено по цветам месячного графика:
02 и 08 августа (Kp = 5.67) красные, 03 и 04 августа (4.33 и 4.00) оранжевые,
09 августа (3.67) — уже зелёное.

### Про округление и запись Kp

В файле Kp дробный — значения кратны трети: `1.33`, `1.67`, `2.67`, `3.33`.
Состояние сенсора — точное дробное значение (так удобнее для автоматизаций,
графиков и порогов), а в атрибутах лежат два «человеческих» представления:

* `kp_display` — целое, округление «половина вверх» (`math.floor(kp + 0.5)`,
  через `decimal.ROUND_HALF_UP`). Встроенный `round()` здесь не годится: он
  округляет к чётному, то есть `round(2.5) == 2`. На реальных данных третями
  ровные половины не встречаются, так что это защита на будущее.
* `kp_notation` — привычная запись третями, как в сводках на сайте:
  `1.67` → `2-`, `7.33` → `7+`, `8.00` → `8`.

Атрибуты текущего Kp также содержат `series` (факт за 3 суток) и
`forecast_series` (прогноз) — готовые ряды для графиков.

## Карточки

```yaml
type: entities
title: Геомагнитная обстановка
entities:
  - entity: sensor.your_storm_level_entity
  - entity: sensor.your_kp_entity
  - entity: sensor.your_kp_forecast_24h_entity
  - entity: sensor.your_ap_entity
  - entity: sensor.your_f107_entity
  - entity: binary_sensor.your_storm_forecast_entity
```

График «факт + прогноз» ([ApexCharts Card](https://github.com/RomRider/apexcharts-card)):

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Индекс Kp — факт и прогноз
graph_span: 6d
span:
  start: day
  offset: "-3d"
series:
  - entity: sensor.your_kp_entity
    name: Факт
    type: column
    color: "#00e000"
    data_generator: |
      return entity.attributes.series
        .map(p => [new Date(p.time).getTime(), p.kp]);
  - entity: sensor.your_kp_entity
    name: Прогноз
    type: column
    color: "#4a9eff"
    opacity: 0.5
    data_generator: |
      return entity.attributes.forecast_series
        .map(p => [new Date(p.time).getTime(), p.kp]);
yaxis:
  - min: 0
    max: 9
    decimals: 0
```

Плитка с цветом по уровню (mushroom):

```yaml
type: custom:mushroom-template-card
primary: "Kp {{ state_attr('sensor.your_kp_entity','kp_display') }}"
secondary: "{{ states('sensor.your_storm_level_entity') }}"
icon: mdi:magnet
icon_color: >
  {% set raw = states('sensor.your_kp_entity') %}
  {% if not is_number(raw) %} disabled
  {% else %}
    {% set kp = raw | float %}
    {% if kp >= 7 %} red
    {% elif kp >= 5 %} orange
    {% elif kp >= 4 %} amber
    {% else %} green {% endif %}
  {% endif %}
```

## Автоматизация

```yaml
automation:
  - alias: Предупреждение о магнитной буре
    triggers:
      - trigger: state
        entity_id: binary_sensor.your_storm_forecast_entity
        to: "on"
    actions:
      - action: notify.mobile_app
        data:
          title: Ожидается магнитная буря
          message: >
            {{ states('sensor.your_storm_level_entity') }},
            прогноз Kp до
            {{ states('sensor.your_kp_forecast_24h_entity') }}
```

Для уведомления «буря закончилась» задавайте `to: "off"`, а не только
`from: "on"`: тогда временная `unavailable` из-за сетевой ошибки не будет
интерпретирована как окончание бури.

## Вариант без интеграции (YAML)

Если ставить компонент не хочется — штатный `rest`-сенсор. Данные в файле идут
от новых суток к старым. Пример ниже рассчитан именно на московский файл
UTC+03; у регионов с другой фазой часового пояса набор полей `hXX` и правила
локального времени могут отличаться.

```yaml
rest:
  - resource: https://xras.ru/txt/kp_RAL5.json
    scan_interval: 600
    headers:
      User-Agent: Home Assistant
    sensor:
      - name: Kp максимум за сутки
        unique_id: xras_kp_max
        value_template: "{{ value_json.data[0].max_kp | float }}"
      - name: Kp текущий
        unique_id: xras_kp_now
        state_class: measurement
        value_template: >
          {% set now_ts = as_timestamp(now()) %}
          {% set ns = namespace(v=none) %}
          {% for d in value_json.data if ns.v is none %}
            {% for h in [21,18,15,12,9,6,3,0] if ns.v is none %}
              {% set key = 'h%02d' | format(h) %}
              {% set raw = d[key] %}
              {% set start = as_timestamp(d.time ~ 'T' ~ ('%02d' | format(h)) ~ ':00:00+03:00') %}
              {% if raw not in [none, 'null'] and start <= now_ts < start + 10800 %}
                {% set ns.v = raw | float %}
              {% endif %}
            {% endfor %}
          {% endfor %}
          {{ ns.v if ns.v is not none else 'unknown' }}
      - name: Ap
        unique_id: xras_ap
        value_template: "{{ value_json.data[0].ap | int }}"
      - name: F10.7
        unique_id: xras_f107
        value_template: "{{ value_json.data[0].f10 | int }}"

  # Месячная выгрузка: data[0] — 31-е число (ещё null), нужен первый непустой день
  - resource: https://xras.ru/txt/kpm_RAL5.json
    scan_interval: 3600
    headers:
      User-Agent: Home Assistant
    sensor:
      - name: Kp максимум за месяц
        unique_id: xras_kp_month
        value_template: >
          {{ value_json.data
             | rejectattr('max_kp', 'eq', 'null')
             | map(attribute='max_kp') | map('float')
             | list | max }}
      - name: Дней с бурей в месяце
        unique_id: xras_storm_days
        value_template: >
          {{ value_json.data
             | rejectattr('max_kp', 'eq', 'null')
             | map(attribute='max_kp') | map('float')
             | select('ge', 5) | list | count }}
```

В отличие от старого варианта «взять последний непустой hXX», этот шаблон
проверяет, что выбранный московский слот действительно покрывает текущий
момент. Он всё равно является упрощённым fallback и не заменяет парсер
интеграции для регионов с другой фазой/DST.

## Разработка и тесты

CI запускает pytest на трёх явно закреплённых профилях Home Assistant. Каждый
профиль использует свой `pytest-homeassistant-custom-component`, который пинит
совместимые версии Home Assistant и pytest:

| Профиль | Home Assistant | Python | Requirements |
| --- | --- | --- | --- |
| minimum supported | `2024.12.0` | `3.12` | `requirements_test_minimum.txt` |
| production | `2026.10.1` | `3.14` | `requirements_test.txt` |
| latest stable | `2026.10.4` | `3.14` | `requirements_test_latest.txt` |

Для локального запуска выберите файл requirements, соответствующий вашей
версии Python. Например, production-профиль требует Python 3.14:

```bash
python -m pip install -r requirements_test.txt
python -m pytest -q
```

Minimum-профиль дополнительно фиксирует `pycares==4.5.0`: у HA 2024.12.0
зависимость допускает более новые версии без верхней границы, а современный
`pycares` уже несовместим с cleanup-проверкой старого тестового harness.

Фикстуры в `tests/fixtures/` покрывают JSON/TXT, обратный порядок дат,
строковые `"null"`, именованные часовые зоны и региональную фазу слотов
(включая UTC+07 с `h00` как точкой непрерывности).

## Замечания

* Данные принадлежат ИКИ РАН — указывайте источник и не опрашивайте сайт чаще
  раза в 5 минут (интеграция это ограничивает; recovery после границы слота
  добавляет jitter и остаётся ограниченным числом попыток).
* HTTP-ответы ограничены по размеру: до 256 KiB для JSON/TXT API, до 128 KiB
  для индекса регионов и до 1 MiB для страницы региона. Ответ большего размера
  отклоняется. При настройке это выглядит как обычная ошибка `cannot_connect`.
  В рантайме переполнение обязательного `kp_*` считается ошибкой обновления,
  поэтому сущности становятся `unavailable` до следующего успешного цикла;
  переполнение необязательного набора делает недоступным только его данные.
  Точная причина остаётся в debug-логе.
* Первоисточник Kp — NOAA (поле `source`), поэтому значения совпадают с
  NOAA SWPC.
* Пустые значения приходят строкой `"null"`: в трёхсуточном файле так
  выглядят ещё не наступившие трёхчасовые интервалы, в месячном — все дни
  до конца месяца. В ряды и в статистику они не попадают.
* В `kp_` есть поля `k` и локальные слоты `hXX`. Для часовых зон, чьё смещение
  не кратно трём часам относительно UTC, фаза слотов сдвигается. Например,
  для UTC+07 реальные начала интервалов — `h01`, `h04`, …, `h22`, а `h00`
  используется XRAS как точка непрерывности графика и не считается отдельным
  трёхчасовым интервалом.
* XRAS передаёт одно поле `tzone` на весь трёхсуточный файл. Если региональный
  файл охватывает момент перехода на летнее/зимнее время, парсер вынужден
  применять это одно фиксированное смещение ко всем суткам файла. Поэтому
  слоты по другую сторону DST-перехода могут быть сдвинуты на один час, пока
  соответствующие сутки не выйдут из трёхсуточного окна. После смены `tzone`
  потенциально смещается уже другая сторона перехода. Это ограничение формата
  источника; код зоны вида `UP03` дополнительно вообще не меняет смещение по
  сезону.
* В `kpm_` нет почасовых слотов — только `time`, `f10`, `sn`, `ap`, `max_kp`.
  Порядок колонок TXT читается из шапки `# [0] time, ...`; fallback без шапки
  разрешён только для известных схем и не угадывает неизвестную ширину файла.
* Поле `type` в обоих файлах одинаковое (`kp`), различить их по нему нельзя —
  интеграция ориентируется на имя файла.
* На сайте есть архив по месяцам: `kpm_RAL5_202506.json` — тот же формат плюс
  суффикс `_ГГГГММ`. При включённой исторической загрузке интеграция использует
  эти файлы только для отдельной статистики суточного максимума Kp; восстановить
  из них исходный трёхчасовой ряд невозможно.

[**English**](#xras--geomagnetic-conditions-for-home-assistant) | [**Русский**](#xras--геомагнитная-обстановка-для-home-assistant)
