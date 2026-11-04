"""
Очистка и приведение типов сырых данных (Этап 3).

Вход:  data/raw/*
Выход: data/processed/*.parquet — типизированные, дедуплицированные таблицы,
       готовые к загрузке в Postgres (см. etl/03_load_to_db.py).

Все решения по очистке основаны на профилировании (docs/data_profile.md) —
там же обоснование, почему определённые поля отброшены/переименованы.
Каждый шаг логируется: что сделано, сколько строк затронуто и почему.

Сознательно НЕ делаем здесь (см. docs/data_profile.md, п.4.1):
- агрегацию файла 13 с уровня партии (PART_ID) до уровня SKU — это SQL-вьюха
  sql/marts/sku_movement.sql, а не pandas (файл слишком большой для комфортной
  работы в памяти, и SUM по (Дата, Аптека, WARE_ID) естественно ложится на SQL);
- любые бизнес-агрегаты уровня "дашборд" (P&L, оборачиваемость и т.п.) — это
  вьюхи в sql/marts/.

Здесь — только: типы, дубли, пропуски, отбрасывание нерелевантных колонок.

Запуск:
    .venv/Scripts/python.exe etl/02_clean_transform.py
"""

import logging
import re
import time
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
PROCESSED_DIR = Path(__file__).resolve().parent.parent / "data" / "processed"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

CSV_ENCODING = "utf-8-sig"
CSV_SEP = ";"
CHUNK_SIZE = 1_000_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("clean_transform")


# ---------------------------------------------------------------------------
# Общие помощники
# ---------------------------------------------------------------------------

def to_numeric_comma(series: pd.Series) -> pd.Series:
    """Числа с запятой как разделителем дробной части (как в исходных CSV) -> float."""
    return pd.to_numeric(series.astype(str).str.replace(",", ".", regex=False), errors="coerce")


def write_parquet(df: pd.DataFrame, filename: str) -> None:
    path = PROCESSED_DIR / filename
    df.to_parquet(path, index=False)
    log.info("Записано %s строк -> %s", f"{len(df):,}", path.name)


# ---------------------------------------------------------------------------
# 03 — справочник товаров
# ---------------------------------------------------------------------------

def clean_products() -> pd.DataFrame:
    log.info("=== 03. Справочник товаров -> dim_products ===")
    df = pd.read_excel(RAW_DIR / "03_справочник_товаров.xlsx").rename(columns={
        "WARE_ID": "ware_id", "SNAME": "name", "SIZG": "manufacturer",
        "SCOUNTRY": "country", "BARCODE": "barcode", "MNN": "mnn",
    })

    df["ware_id"] = df["ware_id"].astype(str).str.strip()
    # Int64 (nullable) -> str, чтобы не словить экспоненциальную запись длинных штрихкодов
    df["barcode"] = df["barcode"].astype("Int64").astype(str)

    # .astype(str) с исходными данными: часть текстовых колонок в xlsx содержит
    # единичные числовые/смешанные значения (Excel хранит их как int/float, не str),
    # из-за чего pyarrow падает при записи object-колонки со смешанными типами.
    missing_name = df["name"].isna().sum()
    df["name"] = df["name"].fillna("Товар " + df["ware_id"]).astype(str)
    log.info("Товары без названия: %s шт. -> подставлено 'Товар <WARE_ID>'", missing_name)

    for col in ["manufacturer", "country"]:
        missing = df[col].isna().sum()
        df[col] = df[col].fillna("Не указано").astype(str)
        log.info("'%s': пропуски %s шт. (%.1f%%) -> 'Не указано'", col, missing, missing / len(df) * 100)

    before = len(df)
    df = df.drop_duplicates(subset="ware_id", keep="first")
    log.info("Дублей по ware_id удалено: %s", before - len(df))

    write_parquet(df, "dim_products.parquet")
    return df


# ---------------------------------------------------------------------------
# 05 + 07 — справочник аптек (04 и 06 не используем, см. docs/data_profile.md)
# ---------------------------------------------------------------------------

def clean_pharmacies() -> pd.DataFrame:
    log.info("=== 05+07. Справочник аптек -> dim_pharmacies ===")
    apt = pd.read_excel(RAW_DIR / "05_справочник_аптек.xlsx").rename(columns={
        "Код": "aptek_id", "Название": "name", "Адрес": "address",
        "Юр. лицо": "legal_entity", "Регион прайса": "price_region",
        "Область": "oblast", "Город": "city_05",
    })
    apt7 = pd.read_excel(RAW_DIR / "07_справочник_аптек_города.xlsx").rename(columns={
        "SlCode": "aptek_id", "Город ": "city_07",
    })[["aptek_id", "city_07"]]

    apt["aptek_id"] = apt["aptek_id"].astype(str)
    apt7["aptek_id"] = apt7["aptek_id"].astype(str)
    df = apt.merge(apt7, on="aptek_id", how="left")

    both_known = df["city_05"].notna() & df["city_07"].notna()
    conflict = both_known & (df["city_05"] != df["city_07"])
    log.info("Город: справочники 05 и 07 расходятся у %s аптек из %s пересекающихся -> "
             "берём значение из 07 (там нет пропусков, см. docs/data_profile.md)",
             conflict.sum(), both_known.sum())
    df["city"] = df["city_07"].fillna(df["city_05"])
    df = df.drop(columns=["city_05", "city_07"])

    for col in ["legal_entity", "price_region", "oblast", "city"]:
        missing = df[col].isna().sum()
        df[col] = df[col].fillna("Не указано").astype(str)
        log.info("'%s': пропуски %s шт. (%.1f%%) -> 'Не указано'", col, missing, missing / len(df) * 100)

    # Адрес оставляем NULL: свободный текст, не используется как категориальный разрез в дашбордах.
    log.info("'address': оставлено NULL без замены — %s шт. (не группируем по адресу)",
             df["address"].isna().sum())

    before = len(df)
    df = df.drop_duplicates(subset="aptek_id", keep="first")
    log.info("Дублей по aptek_id удалено: %s", before - len(df))

    write_parquet(df, "dim_pharmacies.parquet")
    return df


# ---------------------------------------------------------------------------
# 08-12 — Kaspi (см. docs/data_profile.md п.2.1: нет ключа связи с остальными
# таблицами, используем только как сетевой агрегат)
# ---------------------------------------------------------------------------

KASPI_KEEP_COLUMNS = {
    "Адрес точки продаж": "pos_address",
    "Идентификатор точки": "pos_id",
    "ID терминала": "terminal_id",
    "Бренд": "brand",
    "№ документа Покупки/Возврата": "doc_ref_raw",  # НЕ ключ, см. data_profile.md п.2.1
    "Дата операции": "operation_date",
    "Время": "operation_time",
    "Тип операции": "operation_type",
    "Тип оплаты": "payment_type",
    "Сумма операции (т)": "amount",
    "Сумма к зачислению/ списанию (т)": "settlement_amount",
    "Комиссия за операции (т)": "fee_total",
    "Комиссия за операции (%)": "fee_pct",
    "Комиссия Kaspi Pay (т)": "fee_kaspi_pay_total",
    "Комиссия Kaspi Pay (%)": "fee_kaspi_pay_pct",
}


def clean_kaspi_payments() -> pd.DataFrame:
    log.info("=== 08-12. Kaspi -> fact_kaspi_payments (только сетевой агрегат) ===")
    files = sorted(RAW_DIR.glob("*юрлицо*kaspi*.xlsx"))
    frames = []
    for path in files:
        raw = pd.read_excel(path, header=6)
        # "source_account" -- это ярлык файла-выписки, НЕ подтверждённое юрлицо
        # из справочника аптек (см. docs/data_profile.md, п.2.1).
        raw["source_account"] = re.search(r"юрлицо_\d+", path.name).group(0)
        frames.append(raw)
    raw = pd.concat(frames, ignore_index=True)
    log.info("Прочитано %s строк из %s файлов Kaspi", f"{len(raw):,}", len(files))

    dropped = sorted(set(raw.columns) - set(KASPI_KEEP_COLUMNS) - {"source_account"})
    log.info("Отброшены колонки, не нужные ни одной гипотезе из docs/hypotheses.md "
             "(бонусы, Kaspi Доставка, кредит на покупки и т.п.): %s", dropped)

    df = raw[list(KASPI_KEEP_COLUMNS) + ["source_account"]].rename(columns=KASPI_KEEP_COLUMNS)

    df["operation_date"] = pd.to_datetime(df["operation_date"], format="%d.%m.%Y", errors="coerce")
    numeric_cols = ["amount", "settlement_amount", "fee_total", "fee_pct",
                     "fee_kaspi_pay_total", "fee_kaspi_pay_pct"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # NaN в колонках комиссии значит "комиссия не применялась к этой операции"
    # (например, к возврату), а не "неизвестно" -- поэтому 0, а не пропуск.
    fee_cols = ["fee_total", "fee_pct", "fee_kaspi_pay_total", "fee_kaspi_pay_pct"]
    filled = df[fee_cols].isna().sum().to_dict()
    df[fee_cols] = df[fee_cols].fillna(0)
    log.info("Комиссии: пропуски заполнены нулём (нет применимой комиссии): %s", filled)

    before = len(df)
    df = df.drop_duplicates()
    log.info("Удалено полных дублей строк: %s", before - len(df))

    write_parquet(df, "fact_kaspi_payments.parquet")
    return df


# ---------------------------------------------------------------------------
# 01 / 02 — продажи и приход (одинаковая схема)
# ---------------------------------------------------------------------------

TRANSACTION_LOG_COLUMNS = [
    "VH_DATA", "DATA", "OWNER_ID", "USERNAME", "POSTAVSHIK", "APTEKA_ID",
    "DOCNUM", "VNUM", "TIP_DOCUMENTA", "WARE_ID", "DD_PRICE", "QUANT",
    "GODENDO", "P_PRICE", "PRICE_ZAKUP",
]
# APTEKA_NAME и WARE_NAME не читаем -- это денормализованный текст, который
# уже есть в dim_pharmacies/dim_products и джойнится по id (звёздная схема).
# DOC_CAPTION -- текстовый дубль TIP_DOCUMENTA+DOCNUM, не несёт новой информации.
# DATE_FIRST_PRIHOD -- пусто на 96-99% строк (см. data_profile.md), не берём.

TRANSACTION_LOG_RENAME = {
    "VH_DATA": "created_at", "DATA": "posted_at", "OWNER_ID": "owner_id",
    "USERNAME": "employee", "POSTAVSHIK": "supplier", "APTEKA_ID": "aptek_id",
    "DOCNUM": "doc_num", "VNUM": "doc_vnum", "TIP_DOCUMENTA": "doc_type",
    "WARE_ID": "ware_id", "DD_PRICE": "price", "QUANT": "qty",
    "GODENDO": "expiry_date", "P_PRICE": "price_alt", "PRICE_ZAKUP": "cost_price",
}


def clean_transaction_log(fname: str, out_name: str, label: str) -> pd.DataFrame:
    log.info("=== %s -> %s ===", label, out_name)
    path = RAW_DIR / fname

    chunks = [
        chunk for chunk in pd.read_csv(
            path, sep=CSV_SEP, encoding=CSV_ENCODING, dtype=str,
            usecols=TRANSACTION_LOG_COLUMNS, chunksize=CHUNK_SIZE,
        )
    ]
    df = pd.concat(chunks, ignore_index=True)
    log.info("Прочитано строк: %s", f"{len(df):,}")
    df = df.rename(columns=TRANSACTION_LOG_RENAME)

    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    df["posted_at"] = pd.to_datetime(df["posted_at"], errors="coerce")

    for col in ["price", "price_alt", "qty", "cost_price"]:
        df[col] = to_numeric_comma(df[col])

    df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce")
    bad_expiry = (df["expiry_date"].dt.year < 2000) | (df["expiry_date"].dt.year > 2100)
    n_bad_expiry = int(bad_expiry.fillna(False).sum())
    df["expiry_date"] = df["expiry_date"].mask(bad_expiry.fillna(False))
    log.info("expiry_date: обнулено %s дат вне диапазона [2000, 2100] "
             "(явные артефакты — см. data_profile.md, п.4.6)", n_bad_expiry)

    # doc_vnum почти всегда дублирует doc_num -- проверяем и не тащим лишнюю колонку, если так.
    vnum_match_share = (df["doc_vnum"] == df["doc_num"]).mean()
    if vnum_match_share > 0.99:
        df = df.drop(columns=["doc_vnum"])
        log.info("doc_vnum совпадает с doc_num в %.1f%% строк -> колонка избыточна, не переносим", vnum_match_share * 100)
    else:
        log.info("doc_vnum расходится с doc_num в %.1f%% строк -> оставляем обе колонки", (1 - vnum_match_share) * 100)

    before = len(df)
    df = df.drop_duplicates()
    log.info("Удалено полных дублей строк: %s (%.2f%%)", before - len(df), (before - len(df)) / before * 100)

    before = len(df)
    bad_rows = (df["qty"] <= 0) | (df["price"] <= 0)
    n_bad = int(bad_rows.sum())
    df = df[~bad_rows]
    log.info("Отфильтровано строк с qty<=0 или price<=0: %s из %s", n_bad, before)

    write_parquet(df, out_name)
    return df


# ---------------------------------------------------------------------------
# 13 — ежедневные остатки (2.9 GB, потоковая обработка чанками)
# ---------------------------------------------------------------------------

STOCK_DAILY_COLUMNS = [
    "Дата", "G$PROFILE_ID", "PART_ID", "WARE_ID", "Поставщик",
    "Остаток на начало дня", "Приход (шт)", "Продажи (шт)", "Возвраты (шт)",
    "Списания (шт)", "Приход перемещение (шт)", "Расход перемещение (шт)",
    "Остаток на конец дня", "Закупочная цена", "Розничная цена",
]
# Аптека/Товар/ШК/Изготовитель не читаем -- денормализованный текст, уже есть
# в dim_pharmacies/dim_products (джойн по aptek_id/ware_id).
# SAGENT_LAST не берём -- назначение неоднозначно (не всегда равен Поставщик),
# не задействован ни в одной гипотезе из docs/hypotheses.md.

STOCK_DAILY_RENAME = {
    "Дата": "date", "G$PROFILE_ID": "aptek_id", "PART_ID": "part_id", "WARE_ID": "ware_id",
    "Поставщик": "supplier", "Остаток на начало дня": "qty_open", "Приход (шт)": "qty_received",
    "Продажи (шт)": "qty_sold", "Возвраты (шт)": "qty_returned", "Списания (шт)": "qty_writeoff",
    "Приход перемещение (шт)": "qty_transfer_in", "Расход перемещение (шт)": "qty_transfer_out",
    "Остаток на конец дня": "qty_close", "Закупочная цена": "cost_price", "Розничная цена": "retail_price",
}
STOCK_DAILY_NUMERIC_COLS = [
    "qty_open", "qty_received", "qty_sold", "qty_returned", "qty_writeoff",
    "qty_transfer_in", "qty_transfer_out", "qty_close", "cost_price", "retail_price",
]


def clean_stock_daily() -> None:
    log.info("=== 13. Ежедневные остатки -> fact_stock_daily (потоково, чанками по %s) ===", f"{CHUNK_SIZE:,}")
    log.info("Гранулярность строки — партия товара (PART_ID), НЕ SKU. Больше половины строк имеют "
             "отрицательный qty_close -- это нормально на уровне партии (см. data_profile.md, п.4.1). "
             "Агрегация SUM до (date, aptek_id, ware_id) делается SQL-вьюхой sku_movement.sql, не здесь.")

    path = RAW_DIR / "13_ежедневные_остатки_апрель_2023.csv"
    out_path = PROCESSED_DIR / "fact_stock_daily.parquet"

    writer = None
    seen_hashes: set = set()
    total_in = total_out = total_dupes = 0
    t0 = time.time()

    for chunk in pd.read_csv(
        path, sep=CSV_SEP, encoding=CSV_ENCODING, dtype=str,
        usecols=STOCK_DAILY_COLUMNS, chunksize=CHUNK_SIZE,
    ):
        total_in += len(chunk)
        chunk = chunk.rename(columns=STOCK_DAILY_RENAME)
        chunk["date"] = pd.to_datetime(chunk["date"], format="%d.%m.%Y", errors="coerce")
        for col in STOCK_DAILY_NUMERIC_COLS:
            # .astype("float64") принудительно -- иначе чанк без NaN/дробных значений
            # в колонке выводится pandas как int64, а другой чанк той же колонки как
            # float64, и ParquetWriter падает на расхождении схемы между чанками.
            chunk[col] = to_numeric_comma(chunk[col]).astype("float64")

        # Дедуп по хэшу строки -- ловит и дубли внутри чанка, и дубли между чанками,
        # не храня в памяти сами строки (файл слишком большой для drop_duplicates() целиком).
        row_hash = pd.util.hash_pandas_object(chunk, index=False)
        is_dup = row_hash.isin(seen_hashes) | row_hash.duplicated()
        total_dupes += int(is_dup.sum())
        chunk = chunk[~is_dup]
        seen_hashes.update(row_hash[~is_dup].tolist())

        table = pa.Table.from_pandas(chunk, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(out_path, table.schema)
        writer.write_table(table)
        total_out += len(chunk)

    if writer is not None:
        writer.close()

    log.info("Обработано строк: %s, отброшено дублей: %s, записано: %s -> %s (%.0f сек)",
             f"{total_in:,}", f"{total_dupes:,}", f"{total_out:,}", out_path.name, time.time() - t0)


# ---------------------------------------------------------------------------

def main() -> None:
    t0 = time.time()
    clean_products()
    clean_pharmacies()
    clean_kaspi_payments()
    clean_transaction_log("01_продажи_апрель_2023.csv", "fact_sales.parquet", "01. Продажи")
    clean_transaction_log("02_приход_апрель_2023.csv", "fact_receipts.parquet", "02. Приход")
    clean_stock_daily()
    log.info("Готово за %.0f сек. Очищенные таблицы лежат в %s", time.time() - t0, PROCESSED_DIR)


if __name__ == "__main__":
    main()
