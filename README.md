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
| общая зона UTC+03 | `UP03` |

**Вводить код вручную не обязательно.** В форме настройки достаточно написать
название региона — «Москва», «Рига», `moscow` — интеграция откроет страницу
региона на сайте, найдёт ссылку «API: JSON» и вытащит код сама. Русские
названия распознаются для Москвы, Санкт-Петербурга, Риги, Минска, Кишинёва,
Ростова-на-Дону, Екатеринбурга, Красноярска и Ташкента; для остальных пишите
латинский слаг так, как он выглядит в адресе страницы
(`.../magnetic_storms.html/<слаг>/`).

Если автоопределение не сработает, посмотрите код сами: откройте страницу
своего региона и наведитесь на ссылку «API: JSON» под графиком — там будет
`.../txt/kp_RAL5.json`.

Региональный код лучше зоны `UP03`: региональные файлы учитывают переход на
летнее время, а `UP03` — это жёстко зафиксированное смещение. Для Москвы
разницы нет (там круглый год UTC+3), а вот Рига зимой уходит на UTC+2.

Интеграция сначала пробует JSON, при недоступности автоматически переходит на
TXT — обе схемы разбираются одинаково.

## Установка

1. Скопируйте `custom_components/xras_geomagnetic` в `<config>/custom_components/`.
2. Перезапустите Home Assistant.
3. **Настройки → Устройства и службы → Добавить интеграцию → XRAS**.
4. Введите регион («Москва») или код (`RAL5`) и интервал опроса. Значение
   проверяется сразу: если данные не скачались или не разобрались, форма
   покажет ошибку.

Через HACS: *Custom repositories* → адрес репозитория → тип *Integration*.

## Сущности

| Сущность | Значение |
| --- | --- |
| `sensor.…_indeks_kp` | Текущий Kp — последний заполненный трёхчасовой интервал |
| `sensor.…_geomagnitnaia_obstanovka` | Спокойная / Слабовозмущённая / Возбуждённая / бури G1–G5 |
| `sensor.…_maksimum_kp_za_sutki` | `max_kp` за сегодня |
| `sensor.…_maksimum_kp_za_24_chasa` | Максимум факта за прошедшие сутки |
| `sensor.…_maksimum_kp_za_mesiats` | Максимум за текущий месяц; в атрибутах — история по суткам |
| `sensor.…_dnei_s_burei_v_etom_mesiatse` | Сколько суток месяца достигли Kp ≥ 5 |
| `sensor.…_prognoz_kp_na_24_chasa` | Максимум прогноза на ближайшие сутки |
| `sensor.…_prognoz_kp_na_troe_sutok` | Максимум прогноза на трое суток |
| `sensor.…_indeks_ap` | Ap (в атрибуте `forecast` — прогноз на завтра) |
| `sensor.…_potok_f10_7` | F10.7, s.f.u. (в атрибуте `forecast` — прогноз) |
| `sensor.…_chislo_solnechnykh_piaten` | `sn`, по умолчанию выключен |
| `binary_sensor.…_magnitnaia_buria` | `on` при Kp ≥ 5 сейчас |
| `binary_sensor.…_buria_ozhidaetsia` | `on`, если Kp ≥ 5 прогнозируется в ближайшие 24 ч |

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

Атрибуты `sensor.…_indeks_kp` также содержат `series` (факт за 3 суток) и
`forecast_series` (прогноз) — готовые ряды для графиков.

## Карточки

```yaml
type: entities
title: Геомагнитная обстановка
entities:
  - entity: sensor.xras_geomagnitnaia_obstanovka
  - entity: sensor.xras_indeks_kp
  - entity: sensor.xras_prognoz_kp_na_24_chasa
  - entity: sensor.xras_indeks_ap
  - entity: sensor.xras_potok_f10_7
  - entity: binary_sensor.xras_buria_ozhidaetsia
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
  - entity: sensor.xras_indeks_kp
    name: Факт
    type: column
    color: "#00e000"
    data_generator: |
      return entity.attributes.series
        .map(p => [new Date(p.time).getTime(), p.kp]);
  - entity: sensor.xras_indeks_kp
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
primary: "Kp {{ state_attr('sensor.xras_indeks_kp','kp_display') }}"
secondary: "{{ states('sensor.xras_geomagnitnaia_obstanovka') }}"
icon: mdi:magnet
icon_color: >
  {% set kp = states('sensor.xras_indeks_kp') | float(0) %}
  {% if kp >= 7 %} red
  {% elif kp >= 5 %} orange
  {% elif kp >= 4 %} amber
  {% else %} green {% endif %}
```

## Автоматизация

```yaml
automation:
  - alias: Предупреждение о магнитной буре
    triggers:
      - trigger: state
        entity_id: binary_sensor.xras_buria_ozhidaetsia
        to: "on"
    actions:
      - action: notify.mobile_app
        data:
          title: Ожидается магнитная буря
          message: >
            {{ state_attr('binary_sensor.xras_buria_ozhidaetsia','level') }},
            прогноз Kp до
            {{ states('sensor.xras_prognoz_kp_na_24_chasa') }}
```

## Вариант без интеграции (только YAML)

Если ставить компонент не хочется — штатный `rest`-сенсор. Данные в файле идут
от новых суток к старым, поэтому сегодняшние — это `data[0]`:

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
        state_class: measurement
      - name: Kp текущий
        unique_id: xras_kp_now
        state_class: measurement
        value_template: >
          {% set d = value_json.data[0] %}
          {% set slots = ['h21','h18','h15','h12','h09','h06','h03','h00'] %}
          {% set ns = namespace(v=none) %}
          {% for s in slots if ns.v is none and d[s] not in [none,'null'] %}
            {% set ns.v = d[s] | float %}
          {% endfor %}
          {{ ns.v if ns.v is not none else value_json.data[1].h21 | float }}
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
        state_class: measurement
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

## Замечания

* Данные принадлежат ИКИ РАН — указывайте источник и не опрашивайте сайт чаще
  раза в 5 минут (интеграция это ограничивает).
* Первоисточник Kp — NOAA (поле `source`), поэтому значения совпадают с
  NOAA SWPC.
* Пустые значения приходят строкой `"null"`: в трёхсуточном файле так
  выглядят ещё не наступившие трёхчасовые интервалы, в месячном — все дни
  до конца месяца. В ряды и в статистику они не попадают.
* Схемы файлов различаются: в `kp_` есть поля `k` и почасовые `h00`…`h21`,
  в `kpm_` их нет — только `time`, `f10`, `sn`, `ap`, `max_kp`. Порядок
  колонок TXT читается из шапки `# [0] time, ...`, а если её не окажется —
  раскладка определяется по числу колонок в строке.
* Поле `type` в обоих файлах одинаковое (`kp`), различить их по нему нельзя —
  интеграция ориентируется на имя файла.
* На сайте есть архив по месяцам: `kpm_RAL5_202506.json` — тот же формат плюс
  суффикс `_ГГГГММ`. Интеграция его не использует (Home Assistant хранит свою
  историю), но для разовой выгрузки прошлых месяцев он удобен.
