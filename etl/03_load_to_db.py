"""
Загрузка очищенных таблиц (data/processed/*.parquet) в Postgres как staging-таблицы (Этап 4).

Схема `staging` — сырые (но уже типизированные и очищенные на Этапе 3) таблицы 1:1 к parquet-файлам.
Поверх них в sql/marts/ лежат вьюхи с бизнес-агрегатами для Metabase — сюда никакой бизнес-логики
не добавляем, только загрузка.

Загружаем через COPY (быстрее на порядки, чем построчный INSERT/pandas.to_sql на 12.7M строк файла
остатков). Большой файл (fact_stock_daily) читаем и грузим пачками через pyarrow, не целиком в память.

Подключение — из .env в корне репозитория (шаблон — .env.example).

Запуск:
    .venv/Scripts/python.exe etl/03_load_to_db.py
"""

import io
import logging
import time
from pathlib import Path

import pandas as pd
import psycopg2
import pyarrow.parquet as pq
from dotenv import dotenv_values

ROOT_DIR = Path(__file__).resolve().parent.parent
PROCESSED_DIR = ROOT_DIR / "data" / "processed"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("load_to_db")

BATCH_SIZE = 500_000  # для потоковой загрузки fact_stock_daily


def get_connection():
    env = dotenv_values(ROOT_DIR / ".env")
    return psycopg2.connect(
        host=env["POSTGRES_HOST"],
        port=env["POSTGRES_PORT"],
        dbname=env["POSTGRES_DB"],
        user=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
    )


# ---------------------------------------------------------------------------
# DDL: схема каждой staging-таблицы задана явно (а не выведена из dtypes
# автоматически) -- так проще проверить и прочитать, что именно грузим.
# ---------------------------------------------------------------------------

TABLE_DDL = {
    "dim_products": """
        CREATE TABLE staging.dim_products (
            ware_id      TEXT PRIMARY KEY,
            name         TEXT,
            manufacturer TEXT,
            country      TEXT,
            barcode      TEXT,
            mnn          TEXT
        )
    """,
    "dim_pharmacies": """
        CREATE TABLE staging.dim_pharmacies (
            aptek_id     TEXT PRIMARY KEY,
            name         TEXT,
            address      TEXT,
            legal_entity TEXT,
            price_region TEXT,
            oblast       TEXT,
            city         TEXT
        )
    """,
    "fact_kaspi_payments": """
        CREATE TABLE staging.fact_kaspi_payments (
            pos_address         TEXT,
            pos_id              TEXT,
            terminal_id         TEXT,
            brand               TEXT,
            doc_ref_raw         TEXT,   -- НЕ внешний ключ, см. docs/data_profile.md п.2.1
            operation_date      DATE,
            operation_time      TEXT,
            operation_type      TEXT,
            payment_type        TEXT,
            amount              BIGINT,
            settlement_amount   BIGINT,
            fee_total           DOUBLE PRECISION,
            fee_pct             DOUBLE PRECISION,
            fee_kaspi_pay_total DOUBLE PRECISION,
            fee_kaspi_pay_pct   DOUBLE PRECISION,
            source_account      TEXT    -- ярлык файла-выписки, не подтверждённое юрлицо
        )
    """,
    "fact_sales": """
        CREATE TABLE staging.fact_sales (
            created_at   TIMESTAMP,
            posted_at    TIMESTAMP,
            owner_id     TEXT,
            employee     TEXT,
            supplier     TEXT,
            aptek_id     TEXT,
            doc_num      TEXT,
            doc_vnum     TEXT,
            doc_type     TEXT,
            ware_id      TEXT,
            price        DOUBLE PRECISION,
            qty          DOUBLE PRECISION,
            expiry_date  TIMESTAMP,
            price_alt    DOUBLE PRECISION,
            cost_price   DOUBLE PRECISION
        )
    """,
    "fact_receipts": """
        CREATE TABLE staging.fact_receipts (
            created_at   TIMESTAMP,
            posted_at    TIMESTAMP,
            owner_id     TEXT,
            employee     TEXT,
            supplier     TEXT,
            aptek_id     TEXT,
            doc_num      TEXT,
            doc_vnum     TEXT,
            doc_type     TEXT,
            ware_id      TEXT,
            price        DOUBLE PRECISION,
            qty          DOUBLE PRECISION,
            expiry_date  TIMESTAMP,
            price_alt    DOUBLE PRECISION,
            cost_price   DOUBLE PRECISION
        )
    """,
    "fact_stock_daily": """
        CREATE TABLE staging.fact_stock_daily (
            date             DATE,
            aptek_id         TEXT,
            part_id          TEXT,
            ware_id          TEXT,
            supplier         TEXT,
            qty_open         DOUBLE PRECISION,
            qty_received     DOUBLE PRECISION,
            qty_sold         DOUBLE PRECISION,
            qty_returned     DOUBLE PRECISION,
            qty_writeoff     DOUBLE PRECISION,
            qty_transfer_in  DOUBLE PRECISION,
            qty_transfer_out DOUBLE PRECISION,
            qty_close        DOUBLE PRECISION,
            cost_price       DOUBLE PRECISION,
            retail_price     DOUBLE PRECISION
        )
    """,
}

# Индексы под будущие вьюхи в sql/marts/ (группировки/джойны по этим полям).
TABLE_INDEXES = {
    "fact_sales": [
        "CREATE INDEX ix_fact_sales_aptek ON staging.fact_sales (aptek_id)",
        "CREATE INDEX ix_fact_sales_ware ON staging.fact_sales (ware_id)",
        "CREATE INDEX ix_fact_sales_doc ON staging.fact_sales (doc_num)",
        "CREATE INDEX ix_fact_sales_posted_at ON staging.fact_sales (posted_at)",
    ],
    "fact_receipts": [
        "CREATE INDEX ix_fact_receipts_aptek ON staging.fact_receipts (aptek_id)",
        "CREATE INDEX ix_fact_receipts_ware ON staging.fact_receipts (ware_id)",
    ],
    "fact_stock_daily": [
        "CREATE INDEX ix_fact_stock_daily_aptek_ware_date ON staging.fact_stock_daily (aptek_id, ware_id, date)",
        "CREATE INDEX ix_fact_stock_daily_date ON staging.fact_stock_daily (date)",
    ],
}

# Порядок важен: dim_* грузятся до fact_*, у которых на них FOREIGN KEY.
LOAD_ORDER = [
    "dim_products",
    "dim_pharmacies",
    "fact_kaspi_payments",
    "fact_sales",
    "fact_receipts",
    "fact_stock_daily",
]


def copy_dataframe(cur, df: pd.DataFrame, table: str) -> None:
    """Заливает df в staging.<table> через COPY (быстрее пакетных INSERT на порядки).

    NULL передаём через пустое неэкранированное поле -- это дефолтный маркер NULL
    для FORMAT csv в Postgres, поэтому явный NULL '...' в COPY не нужен (и не рискуем
    ошибиться в экранировании обратного слэша, как было бы с форматом text).
    """
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="")
    buf.seek(0)
    cur.copy_expert(f"COPY staging.{table} FROM STDIN WITH (FORMAT csv)", buf)


def load_small_table(conn, name: str) -> None:
    path = PROCESSED_DIR / f"{name}.parquet"
    df = pd.read_parquet(path)
    with conn.cursor() as cur:
        copy_dataframe(cur, df, name)
    conn.commit()
    log.info("%s: загружено %s строк", name, f"{len(df):,}")


def load_stock_daily_streaming(conn) -> None:
    """fact_stock_daily -- 12.7M строк, грузим пачками через pyarrow, не читая всё в память."""
    path = PROCESSED_DIR / "fact_stock_daily.parquet"
    pf = pq.ParquetFile(path)
    total = 0
    t0 = time.time()
    with conn.cursor() as cur:
        for batch in pf.iter_batches(batch_size=BATCH_SIZE):
            df = batch.to_pandas()
            copy_dataframe(cur, df, "fact_stock_daily")
            total += len(df)
            conn.commit()
            log.info("fact_stock_daily: загружено %s строк (%.0f сек)", f"{total:,}", time.time() - t0)
    log.info("fact_stock_daily: готово, всего %s строк за %.0f сек", f"{total:,}", time.time() - t0)


def main() -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS staging CASCADE")
            cur.execute("CREATE SCHEMA staging")
            for name in LOAD_ORDER:
                cur.execute(TABLE_DDL[name])
        conn.commit()
        log.info("Схема staging и таблицы созданы")

        for name in LOAD_ORDER:
            if name == "fact_stock_daily":
                load_stock_daily_streaming(conn)
            else:
                load_small_table(conn, name)

        with conn.cursor() as cur:
            for name, statements in TABLE_INDEXES.items():
                for stmt in statements:
                    cur.execute(stmt)
        conn.commit()
        log.info("Индексы созданы")

        with conn.cursor() as cur:
            cur.execute("ANALYZE")
        conn.commit()
        log.info("ANALYZE выполнен, загрузка завершена")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
