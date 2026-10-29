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

**Вводить код вручную не обязательно.** В форме настройки достаточно написать
название региона — «Москва», «Рига», «Норильск», `moscow` — интеграция откроет
страницу региона на сайте, найдёт ссылку «API: JSON» и вытащит код сама.
Русские названия распознаются для Москвы, Санкт-Петербурга, Риги, Минска,
Кишинёва, Ростова-на-Дону, Екатеринбурга, Красноярска, Норильска и Ташкента;
для остальных пишите латинский слаг так, как он выглядит в адресе страницы
(`.../magnetic_storms.html/<слаг>/`).

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
В **Настроить** можно изменить только интервал опроса. Если нужен другой
регион, удалите запись и создайте новую — так история сущностей разных
регионов не смешивается.

При обновлении старых записей конфигурации версии 2 выполняется автоматическая
миграция: если регион ранее меняли через Options, фактически используемый код
переносится в основную конфигурацию и становится `unique_id` записи. При
конфликте с уже существующей записью того же региона миграция останавливается,
а не создаёт дубликат.

## Сущности

| Сущность | Значение |
| --- | --- |
| `sensor.…_indeks_kp` | Текущий Kp только для действующего трёхчасового интервала |
| `sensor.…_geomagnitnaia_obstanovka` | Спокойная / Возбуждённая / бури G1–G5 |
| `sensor.…_maksimum_kp_za_sutki` | `max_kp` за сегодня |
| `sensor.…_maksimum_kp_za_24_chasa` | Максимум факта за прошедшие 24 часа |
| `sensor.…_maksimum_kp_za_mesiats` | Максимум за текущий месяц; в атрибутах — история по суткам |
| `sensor.…_dnei_s_burei_v_etom_mesiatse` | Сколько суток месяца достигли Kp ≥ 5 |
| `sensor.…_prognoz_kp_na_24_chasa` | Максимум прогноза на ближайшие 24 часа |
| `sensor.…_prognoz_kp_na_troe_sutok` | Максимум доступного трёхсуточного прогноза |
| `sensor.…_indeks_ap` | Ap (в атрибуте `forecast` — прогноз строго на завтра) |
| `sensor.…_potok_f10_7` | F10.7, s.f.u. (в атрибуте `forecast` — прогноз строго на завтра) |
| `sensor.…_chislo_solnechnykh_piaten` | `sn`, по умолчанию выключен |
| `binary_sensor.…_magnitnaia_buria` | `on` при Kp ≥ 5 в текущем валидном интервале |
| `binary_sensor.…_buria_ozhidaetsia` | `on`, если Kp ≥ 5 прогнозируется в ближайшие 24 ч |

У текущего Kp есть атрибуты `measured_at` и `valid_until`. На границе слота
интеграция не публикует искусственный промежуточный `unknown`: после небольшой
случайной задержки 5–60 секунд выполняется запрос нового слота. Если XRAS уже
обновился, состояние переходит сразу со старого Kp на новый; если свежего
слота ещё нет — в `unknown`; при ошибке обязательного запроса сущности штатно
становятся `unavailable`. Затем выполняется до трёх повторов не чаще чем раз в
5 минут, также с небольшим jitter. При `unknown` атрибуты `measured_at` и
`valid_until` сохраняют границы последнего фактического слота, чтобы было видно,
почему значение уже не считается текущим.

Entity ID создаёт Home Assistant из имени записи, перевода сущности и языка,
который был активен при создании. Поэтому точный slug не является частью API
интеграции и может отличаться между установками. Ниже используются реальные
примерные ID для русскоязычной записи RAL5; перед копированием YAML замените их
на ID из **Настройки → Устройства и службы → XRAS → Сущности**.

### Ключи уровней против переведённого текста

Уровень обстановки везде хранится ключом: `quiet`, `active`, `g1`…`g5`. Это
касается и состояния `sensor.…_geomagnitnaia_obstanovka`, и атрибутов
`level_key` у бинарных сенсоров и у прогнозного Kp. В условиях автоматизаций
сравнивайте именно с ключом — он не зависит от языка интерфейса и не поедет
при смене формулировок.

Переведённый текст («Слабая буря (G1)») подставляет сам Home Assistant при
отображении сущности, поэтому дублировать его в атрибутах не нужно: если
название требуется в шаблоне или уведомлении, берите его из состояния
ENUM-сенсора — там оно уже локализовано:

```jinja
{{ states('sensor.geomagnitnaia_obstanovka_ral5_geomagnitnaia_obstanovka') }}
{{ state_attr('binary_sensor.geomagnitnaia_obstanovka_ral5_magnitnaia_buria', 'level_key') }}
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

Атрибуты `sensor.…_indeks_kp` также содержат `series` (факт за 3 суток) и
`forecast_series` (прогноз) — готовые ряды для графиков.

## Карточки

```yaml
type: entities
title: Геомагнитная обстановка
entities:
  - entity: sensor.geomagnitnaia_obstanovka_ral5_geomagnitnaia_obstanovka
  - entity: sensor.geomagnitnaia_obstanovka_ral5_indeks_kp
  - entity: sensor.geomagnitnaia_obstanovka_ral5_prognoz_kp_na_24_chasa
  - entity: sensor.geomagnitnaia_obstanovka_ral5_indeks_ap
  - entity: sensor.geomagnitnaia_obstanovka_ral5_potok_f10_7
  - entity: binary_sensor.geomagnitnaia_obstanovka_ral5_buria_ozhidaetsia
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
  - entity: sensor.geomagnitnaia_obstanovka_ral5_indeks_kp
    name: Факт
    type: column
    color: "#00e000"
    data_generator: |
      return entity.attributes.series
        .map(p => [new Date(p.time).getTime(), p.kp]);
  - entity: sensor.geomagnitnaia_obstanovka_ral5_indeks_kp
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
primary: "Kp {{ state_attr('sensor.geomagnitnaia_obstanovka_ral5_indeks_kp','kp_display') }}"
secondary: "{{ states('sensor.geomagnitnaia_obstanovka_ral5_geomagnitnaia_obstanovka') }}"
icon: mdi:magnet
icon_color: >
  {% set raw = states('sensor.geomagnitnaia_obstanovka_ral5_indeks_kp') %}
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
        entity_id: binary_sensor.geomagnitnaia_obstanovka_ral5_buria_ozhidaetsia
        to: "on"
    actions:
      - action: notify.mobile_app
        data:
          title: Ожидается магнитная буря
          message: >
            {{ states('sensor.geomagnitnaia_obstanovka_ral5_geomagnitnaia_obstanovka') }},
            прогноз Kp до
            {{ states('sensor.geomagnitnaia_obstanovka_ral5_prognoz_kp_na_24_chasa') }}
```

Для уведомления «буря закончилась» задавайте `to: "off"`, а не только
`from: "on"`: тогда временная `unavailable` из-за сетевой ошибки не будет
интерпретирована как окончание бури.

## Вариант без интеграции (только YAML)

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

Тесты используют закреплённые версии `pytest` и
`pytest-homeassistant-custom-component` и запускаются тем же workflow, что
Hassfest и HACS validation:

```bash
python -m pip install -r requirements_test.txt
python -m pytest -q
```

Фикстуры в `tests/fixtures/` покрывают JSON/TXT, обратный порядок дат,
строковые `"null"`, именованные часовые зоны и региональную фазу слотов
(включая UTC+07 с `h00` как точкой непрерывности).

## Замечания

* Данные принадлежат ИКИ РАН — указывайте источник и не опрашивайте сайт чаще
  раза в 5 минут (интеграция это ограничивает; recovery после границы слота
  добавляет jitter и остаётся ограниченным числом попыток).
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
* В `kpm_` нет почасовых слотов — только `time`, `f10`, `sn`, `ap`, `max_kp`.
  Порядок колонок TXT читается из шапки `# [0] time, ...`; fallback без шапки
  разрешён только для известных схем и не угадывает неизвестную ширину файла.
* Поле `type` в обоих файлах одинаковое (`kp`), различить их по нему нельзя —
  интеграция ориентируется на имя файла.
* На сайте есть архив по месяцам: `kpm_RAL5_202506.json` — тот же формат плюс
  суффикс `_ГГГГММ`. Интеграция его не использует (Home Assistant хранит свою
  историю), но для разовой выгрузки прошлых месяцев он удобен.
