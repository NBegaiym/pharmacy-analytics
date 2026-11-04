# pharmacy-analytics

Аналитика розницы аптечной сети: профилирование и очистка сырых SFTP-выгрузок, Postgres,
SQL-вьюхи под дашборды, Metabase для CEO/COO. Контекст и постановка задачи — в `docs/`.

## Как это устроено

```
data/raw/        -- сырые файлы с SFTP (в git не попадают, см. .gitignore)
data/processed/  -- результат Этапа 3 (parquet, в git не попадают -- регенерируются etl/02)
etl/
  01_profiling.py         -- профилирование сырых данных -> печатает отчёт в stdout
  02_clean_transform.py   -- очистка/типизация -> data/processed/*.parquet
  03_load_to_db.py        -- загрузка parquet в Postgres, схема staging
sql/marts/         -- SQL-вьюхи поверх staging для Metabase (см. ниже)
docs/
  data_profile.md  -- что за таблицы, что в них, проблемы качества данных (Этап 1)
  hypotheses.md    -- гипотезы дашбордов CEO/COO, разбивка по вьюхам (Этап 2)
  report.md        -- пишется вручную, не трогать
```

Полный разбор данных, найденных проблем качества и решений по очистке — в
[`docs/data_profile.md`](docs/data_profile.md). Что именно показывает каждый дашборд и почему —
в [`docs/hypotheses.md`](docs/hypotheses.md).

## Как запустить с нуля

### 1. Python-окружение

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install pandas openpyxl pyarrow sqlalchemy psycopg2-binary python-dotenv
```

### 2. Положить сырые файлы

Скачать выгрузку по SFTP в `data/raw/` (13 файлов, см. состав в `docs/data_profile.md`, раздел 1).

### 3. Postgres

Нужна база Postgres (локально или в Coolify) и отдельная роль (не системный суперпользователь).
Параметры подключения берутся из `.env` в корне репозитория (шаблон — `.env.example`):

```
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=pharmacy_analytics
POSTGRES_USER=pharmacy_app
POSTGRES_PASSWORD=...
```

### 4. Прогнать пайплайн по порядку

```
.venv/Scripts/python.exe etl/01_profiling.py        # ~2 мин, печатает отчёт в консоль
.venv/Scripts/python.exe etl/02_clean_transform.py   # ~3.5 мин, пишет data/processed/*.parquet
.venv/Scripts/python.exe etl/03_load_to_db.py        # ~4 мин, грузит в Postgres (схема staging)
```

Тот же пайплайн — в Docker, без локальной установки Python (см. `Dockerfile`; собран, но не
пересобирался локально — на этой машине нет Docker, сборку проверяет Coolify/CI):

```
docker build -t pharmacy-analytics-etl .
docker run --rm \
    -v "$(pwd)/data:/app/data" \
    -v "$(pwd)/.env:/app/.env:ro" \
    pharmacy-analytics-etl python etl/03_load_to_db.py
```
Образ содержит только код (`etl/`, `sql/`) — `data/raw/` и `.env` пробрасываются volume-mount'ом
при запуске, в образ не запекаются (см. `.dockerignore`).

### 5. Применить SQL-вьюхи

Применять **строго в этом порядке** — `store_health.sql` и часть остальных читают из
`marts.sku_movement`, который создаётся в `sku_movement.sql`:

```
psql -U pharmacy_app -h <host> -d pharmacy_analytics -f sql/marts/sku_movement.sql
psql -U pharmacy_app -h <host> -d pharmacy_analytics -f sql/marts/store_health.sql
psql -U pharmacy_app -h <host> -d pharmacy_analytics -f sql/marts/employee_performance.sql
psql -U pharmacy_app -h <host> -d pharmacy_analytics -f sql/marts/basket_analysis.sql
psql -U pharmacy_app -h <host> -d pharmacy_analytics -f sql/marts/payment_mix.sql
psql -U pharmacy_app -h <host> -d pharmacy_analytics -f sql/marts/procurement_efficiency.sql
psql -U pharmacy_app -h <host> -d pharmacy_analytics -f sql/marts/staffing_load.sql
psql -U pharmacy_app -h <host> -d pharmacy_analytics -f sql/marts/receipt_anomalies.sql
```

`sku_movement.sql` и часть `basket_analysis.sql` (`product_pair_affinity`) создают
`MATERIALIZED VIEW` — обычные вьюхи над 12.7M-строчным файлом остатков пересчитывались бы
на каждый запрос из Metabase (проверено: 2+ минуты на запрос). После перезагрузки staging
их нужно обновить:

```sql
REFRESH MATERIALIZED VIEW marts.stock_daily_by_sku;
REFRESH MATERIALIZED VIEW marts.sku_movement;
REFRESH MATERIALIZED VIEW marts.product_pair_affinity;
```

Повторный запуск `sku_movement.sql` делает `DROP MATERIALIZED VIEW ... CASCADE` и пересоздаёт
всё заново (~2-3 мин) — после этого нужно заново применить `store_health.sql` (он зависит от
`marts.sku_movement`).

### 6. Metabase

Подключить Metabase к базе `pharmacy_analytics`, схема `marts` — там 24 готовые вьюхи под
обе панели (CEO и COO), разбивка по картам дашборда — в `docs/hypotheses.md`.

Сами дашборды (26 карточек, раскладка, фильтры) созданы не руками в UI, а скриптом через
REST API Metabase — `metabase/build_dashboards.py`. Metabase не хранит вопросы/дашборды в
git, поэтому это единственный способ иметь их воспроизводимыми кодом:

```
METABASE_URL=http://<host> METABASE_API_KEY=<ключ с правами Administrators> \
    python metabase/build_dashboards.py
```

API-ключ — Admin → Настройки → Аутентификация → API-ключи → Создать. Скрипт создаёт НОВЫЙ
набор карточек при каждом запуске (не ищет существующие по имени) — это скрипт первоначальной
настройки, не синхронизации.

## Что не входит в этот репозиторий

Это публичная копия тестового задания для портфолио. Реквизиты доступа (`credentials/`, `.env`)
и исходные данные (`data/`) не публикуются, поэтому пайплайн и дашборды нельзя запустить только
из этого репозитория. Развёрнутые при выполнении задания Metabase и Postgres больше не доступны.
