# ETL-пайплайн (Этапы 1-4): профилирование -> очистка -> загрузка в Postgres.
# Образ содержит только код -- сырые данные (data/raw/) в него НЕ запекаются (слишком большие,
# получаются по SFTP отдельно) -- на этапе docker run прокидывать их volume-mount'ом.
#
# Сборка:
#   docker build -t pharmacy-analytics-etl .
#
# Запуск конкретного этапа (пример — полная загрузка в БД):
#   docker run --rm \
#     -v "$(pwd)/data:/app/data" \
#     -v "$(pwd)/.env:/app/.env:ro" \
#     pharmacy-analytics-etl python etl/03_load_to_db.py
#
# Другие этапы аналогично: etl/01_profiling.py, etl/02_clean_transform.py.
# sql/marts/*.sql применяются через psql, не через этот образ (см. README.md).

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    pandas \
    openpyxl \
    pyarrow \
    sqlalchemy \
    psycopg2-binary \
    python-dotenv

COPY etl/ ./etl/
COPY sql/ ./sql/

CMD ["python", "etl/01_profiling.py"]
