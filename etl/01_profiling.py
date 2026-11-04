"""
Профилирование исходных данных из data/raw/.

Для каждого файла определяем: формат, кол-во строк, колонки, типы,
диапазоны дат, дубликаты, доли пропусков, кардинальность ключевых полей.
Также проверяем связи между таблицами (по каким полям джойнятся).

Большие файлы (продажи ~830K строк, остатки ~12.7M строк) читаем чанками,
чтобы не упираться в память. Числа с запятой как разделителем дробной части
и дата/время оставляем строками на этом этапе — приведение типов делает
02_clean_transform.py, здесь мы только смотрим "как есть".

Запуск:
    .venv/Scripts/python.exe etl/01_profiling.py
Печатает отчёт в stdout. Числа из этого отчёта легли в основу docs/data_profile.md.
"""

from pathlib import Path

import pandas as pd

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

CSV_ENCODING = "utf-8-sig"
CSV_SEP = ";"
CHUNK_SIZE = 1_000_000


def hr(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def null_share(df: pd.DataFrame) -> pd.Series:
    return (df.isna().mean() * 100).round(2)


# ---------------------------------------------------------------------------
# 01 / 02 — журналы продаж и прихода (одинаковая схема, читаем чанками)
# ---------------------------------------------------------------------------

def profile_transaction_log(fname: str, label: str) -> None:
    hr(f"{label} — {fname}")

    path = RAW_DIR / fname
    print(f"Размер файла: {path.stat().st_size / 1e6:.1f} MB")

    n_rows = 0
    null_counts = None
    dup_rows = 0
    aptek_ids = set()
    ware_ids = set()
    usernames = set()
    docnums = set()
    doc_types = set()
    date_min, date_max = None, None
    quant_fraction_rows = 0
    quant_negative_or_zero = 0
    price_mismatch_rows = 0
    price_nonpositive_rows = 0
    godendo_plausible_rows = 0
    godendo_total_parsed = 0
    employee_aptek_pairs = set()  # (USERNAME, APTEKA_ID) для проверки, работает ли сотрудник в 1 аптеке

    for chunk in pd.read_csv(
        path, sep=CSV_SEP, encoding=CSV_ENCODING, dtype=str, chunksize=CHUNK_SIZE
    ):
        n_rows += len(chunk)

        counts = chunk.isna().sum()
        null_counts = counts if null_counts is None else null_counts + counts

        dup_rows += chunk.duplicated().sum()

        aptek_ids.update(chunk["APTEKA_ID"].dropna().unique())
        ware_ids.update(chunk["WARE_ID"].dropna().unique())
        usernames.update(chunk["USERNAME"].dropna().unique())
        docnums.update(chunk["DOCNUM"].dropna().unique())
        doc_types.update(chunk["TIP_DOCUMENTA"].dropna().unique())

        dates = pd.to_datetime(chunk["DATA"], errors="coerce")
        chunk_min, chunk_max = dates.min(), dates.max()
        date_min = chunk_min if date_min is None else min(date_min, chunk_min)
        date_max = chunk_max if date_max is None else max(date_max, chunk_max)

        quant = chunk["QUANT"].str.replace(",", ".", regex=False).astype(float)
        quant_fraction_rows += (quant % 1 != 0).sum()
        quant_negative_or_zero += (quant <= 0).sum()

        price_mismatch_rows += (chunk["DD_PRICE"] != chunk["P_PRICE"]).sum()
        dd_price = chunk["DD_PRICE"].str.replace(",", ".", regex=False).astype(float)
        price_nonpositive_rows += (dd_price <= 0).sum()

        if "GODENDO" in chunk.columns:
            godendo = pd.to_datetime(chunk["GODENDO"], errors="coerce")
            godendo_total_parsed += godendo.notna().sum()
            godendo_plausible_rows += godendo.dt.year.between(2023, 2035).sum()

        if "USERNAME" in chunk.columns:
            pairs = chunk.dropna(subset=["USERNAME"])[["USERNAME", "APTEKA_ID"]]
            employee_aptek_pairs.update(map(tuple, pairs.itertuples(index=False, name=None)))

    print(f"Строк: {n_rows:,}")
    print(f"Диапазон дат (DATA): {date_min} .. {date_max}")
    print(f"Полных дублей строк: {dup_rows:,} ({dup_rows / n_rows:.2%})")
    print(f"Уникальных аптек (APTEKA_ID): {len(aptek_ids)}")
    print(f"Уникальных товаров (WARE_ID): {len(ware_ids)}")
    print(f"Уникальных сотрудников (USERNAME): {len(usernames)}")
    print(f"Уникальных документов (DOCNUM): {len(docnums):,}")
    print(f"Типы документов (TIP_DOCUMENTA): {sorted(doc_types)}")
    print(f"QUANT — дробные значения: {quant_fraction_rows:,} ({quant_fraction_rows / n_rows:.2%})")
    print(f"QUANT — <= 0: {quant_negative_or_zero:,}")
    print(f"DD_PRICE != P_PRICE: {price_mismatch_rows:,} ({price_mismatch_rows / n_rows:.2%})")
    print(f"DD_PRICE <= 0: {price_nonpositive_rows:,}")
    if godendo_total_parsed:
        print(f"GODENDO — доля в правдоподобном диапазоне [2023, 2035] среди распарсенных: "
              f"{godendo_plausible_rows / godendo_total_parsed:.2%}")
    if employee_aptek_pairs:
        emp_to_apteks = {}
        for user, aptek in employee_aptek_pairs:
            emp_to_apteks.setdefault(user, set()).add(aptek)
        multi_aptek_employees = sum(1 for apteks in emp_to_apteks.values() if len(apteks) > 1)
        print(f"Сотрудников, встречающихся в >1 аптеке: {multi_aptek_employees} из {len(emp_to_apteks)}")
    print("\nДоля пропусков по колонкам (%):")
    print((null_counts / n_rows * 100).round(2).sort_values(ascending=False).to_string())


# ---------------------------------------------------------------------------
# Справочники (маленькие xlsx, читаем целиком)
# ---------------------------------------------------------------------------

def profile_excel(fname: str, label: str, header: int = 0, sheet_name=0) -> pd.DataFrame:
    hr(f"{label} — {fname}")
    path = RAW_DIR / fname
    df = pd.read_excel(path, header=header, sheet_name=sheet_name)
    print(f"Строк: {len(df):,}")
    print(f"Колонки: {list(df.columns)}")
    print("\nДоля пропусков по колонкам (%):")
    print(null_share(df).sort_values(ascending=False).to_string())
    for col in df.columns:
        if df[col].duplicated().sum() > 0 and df[col].nunique() < len(df):
            print(f"Дублей в колонке {col!r}: {df[col].duplicated().sum()}")
    return df


def profile_kaspi_files() -> None:
    hr("08-12 — выписки Kaspi по юрлицам")
    kaspi_files = sorted(RAW_DIR.glob("*юрлицо*kaspi*.xlsx"))
    for path in kaspi_files:
        df = pd.read_excel(path, header=6)
        n_rows = len(df)
        op_types = df["Тип операции"].value_counts().to_dict()
        total_sum = pd.to_numeric(df["Сумма операции (т)"], errors="coerce").sum()
        print(f"{path.name}: строк={n_rows:,}, типы операций={op_types}, "
              f"сумма операций={total_sum:,.0f}")


def check_kaspi_join_keys(sales_docnums: set, aptek_dict: pd.DataFrame) -> None:
    """Проверяем, можно ли достоверно связать Kaspi-выписку с конкретной аптекой/юрлицом.

    Первая проверка ("доля DOCNUM Kaspi, найденных в DOCNUM продаж") ошибочно выглядела
    как 100% совпадение — но это статистический артефакт: в колонке "№ документа
    Покупки/Возврата" очень мало уникальных значений (несколько десятков на файл при
    десятках тысяч строк), то есть это не номер конкретного чека. Поэтому здесь мы явно
    печатаем кардинальность колонки, чтобы ошибку было видно из отчёта, а не только из
    ручной проверки.
    """
    hr("Проверка связи Kaspi <-> продажи/аптеки (ключи)")
    path = RAW_DIR / "08_юрлицо_001_kaspi_апрель_2023.xlsx"
    df = pd.read_excel(path, header=6)
    doc_col = "№ документа Покупки/Возврата"
    docs = df[doc_col].dropna().astype(str)

    overlap = docs.isin(sales_docnums).mean()
    print(f"Доля значений '{doc_col}' (юрлицо 001), найденных в DOCNUM продаж: {overlap:.2%}")
    print(f"Уникальных значений в этой колонке: {docs.nunique()} при {len(docs):,} строках "
          f"(в среднем каждое значение повторяется {len(docs) / docs.nunique():.0f} раз)")
    print("=> Высокое совпадение — статистический артефакт низкой кардинальности "
          "(значения вида DOC0000001..060 совпадают с началом диапазона DOCNUM продаж), "
          "а НЕ реальная связь чек-в-чек. Использовать эту колонку как внешний ключ нельзя.")

    addr_kaspi = set(df["Адрес точки продаж"].dropna().unique())
    addr_dict = set(aptek_dict["Адрес"].dropna().unique())
    print(f"\nПересечение адресов точек продаж Kaspi и адресов из справочника аптек: "
          f"{len(addr_kaspi & addr_dict)} из {len(addr_kaspi)} — разные псевдонимизированные пулы.")

    kaspi_legal_names = set()
    for kfile in sorted(RAW_DIR.glob("*юрлицо*kaspi*.xlsx")):
        meta = pd.read_excel(kfile, header=None, nrows=5)
        kaspi_legal_names.add(str(meta.iloc[4, 2]))
    dict_legal_names = set(aptek_dict["Юр. лицо"].dropna().unique())
    print(f"'Юрлицо' внутри Kaspi-файлов: {sorted(kaspi_legal_names)}")
    print(f"'Юр. лицо' в справочнике аптек: {sorted(dict_legal_names)}")
    print(f"Совпадений: {len(kaspi_legal_names & dict_legal_names)} из {len(kaspi_legal_names)}")
    print("\n=> ИТОГ: у Kaspi-выписок (08-12) нет достоверного ключа связи с конкретной аптекой, "
          "сотрудником или юрлицом из остальных файлов. Используем их только как сетевой/файловый "
          "агрегат (payment_mix), не разбивая по аптеке/сотруднику.")


# ---------------------------------------------------------------------------
# 13 — ежедневные остатки (2.9 GB, читаем чанками, один проход = все метрики)
# ---------------------------------------------------------------------------

def profile_daily_balances() -> None:
    fname = "13_ежедневные_остатки_апрель_2023.csv"
    hr(f"13 — Ежедневные остатки — {fname}")

    path = RAW_DIR / fname
    print(f"Размер файла: {path.stat().st_size / 1e9:.2f} GB")

    n_rows = 0
    aptek_names = set()
    profile_ids = set()
    ware_ids = set()
    dates = set()
    null_counts = None
    neg_end_balance = 0
    dup_grain_rows = 0  # дубли по (Дата, Аптека, WARE_ID) -- ожидаемо много, т.к. грануляция = партия (PART_ID)

    for chunk in pd.read_csv(
        path, sep=CSV_SEP, encoding=CSV_ENCODING, dtype=str, chunksize=CHUNK_SIZE
    ):
        n_rows += len(chunk)
        aptek_names.update(chunk["Аптека"].dropna().unique())
        profile_ids.update(chunk["G$PROFILE_ID"].dropna().unique())
        ware_ids.update(chunk["WARE_ID"].dropna().unique())
        dates.update(chunk["Дата"].dropna().unique())

        counts = chunk.isna().sum()
        null_counts = counts if null_counts is None else null_counts + counts

        end_balance = chunk["Остаток на конец дня"].str.replace(",", ".", regex=False).astype(float)
        neg_end_balance += (end_balance < 0).sum()

        grain_key = chunk["Дата"] + "|" + chunk["Аптека"] + "|" + chunk["WARE_ID"]
        dup_grain_rows += grain_key.duplicated().sum()

    print(f"Строк: {n_rows:,}")
    print(f"Уникальных аптек (Аптека): {len(aptek_names)}")
    print(f"Уникальных профилей (G$PROFILE_ID): {len(profile_ids)}")
    print(f"Уникальных товаров (WARE_ID): {len(ware_ids)}")
    print(f"Уникальных дат: {len(dates)} ({min(dates)} .. {max(dates)})")
    print(f"Строк с отрицательным остатком на конец дня: "
          f"{neg_end_balance:,} ({neg_end_balance / n_rows:.2%})")
    print(f"Дублей по ключу (Дата, Аптека, WARE_ID): "
          f"{dup_grain_rows:,} ({dup_grain_rows / n_rows:.2%})")
    print("=> Реальная гранулярность файла — партия товара (PART_ID), а не SKU. "
          "Для метрик на уровне товара нужна агрегация SUM по (Дата, Аптека, WARE_ID).")
    print("\nДоля пропусков по колонкам (%):")
    print((null_counts / n_rows * 100).round(2).sort_values(ascending=False).to_string())


# ---------------------------------------------------------------------------
# Кросс-табличные проверки
# ---------------------------------------------------------------------------

def cross_check_pharmacies(sales_aptek_ids: set, prihod_aptek_ids: set, aptek_dict: pd.DataFrame) -> None:
    hr("Кросс-проверка: активные аптеки vs справочник аптек")
    active = sales_aptek_ids | prihod_aptek_ids
    listed = set(aptek_dict["Код"].astype(str))
    print(f"Активных аптек (были продажи или приход в апреле 2023): {len(active)}")
    print(f"Аптек в справочнике (05): {len(listed)}")
    print(f"В справочнике, но неактивны в апреле: {len(listed - active)}")
    print(f"Активны, но отсутствуют в справочнике: {len(active - listed)}")


def cross_check_products(sales_ware_ids: set, prihod_ware_ids: set, product_dict: pd.DataFrame) -> None:
    hr("Кросс-проверка: товары в транзакциях vs справочник товаров")
    used = sales_ware_ids | prihod_ware_ids
    listed = set(product_dict["WARE_ID"].astype(str))
    print(f"Товаров, встречающихся в продажах/приходе: {len(used)}")
    print(f"Товаров в справочнике (03): {len(listed)}")
    print(f"Используются, но нет в справочнике: {len(used - listed)}")


def cross_check_city_dicts() -> None:
    hr("Кросс-проверка: конфликт городов между справочниками 05 и 07")
    apt5 = pd.read_excel(RAW_DIR / "05_справочник_аптек.xlsx")
    apt7 = pd.read_excel(RAW_DIR / "07_справочник_аптек_города.xlsx")
    merged = apt5.merge(apt7, left_on="Код", right_on="SlCode", how="inner")
    conflict = merged["Город"] != merged["Город "]
    print(f"Пересекающихся кодов аптек: {len(merged)}")
    print(f"Из них с расхождением города между 05 и 07: {conflict.sum()} ({conflict.mean():.1%})")


def main() -> None:
    pd.set_option("display.max_rows", 100)

    # --- Журналы транзакций (большие, чанками) ---
    profile_transaction_log("01_продажи_апрель_2023.csv", "01. Продажи (чек ККМ)")
    profile_transaction_log("02_приход_апрель_2023.csv", "02. Приход от поставщика")

    # --- Справочники товаров ---
    product_dict = profile_excel("03_справочник_товаров.xlsx", "03. Справочник товаров (полный)")
    profile_excel("04_справочник_товаров_краткий.xlsx", "04. Справочник товаров (краткий)")

    # --- Справочники аптек ---
    aptek_dict = profile_excel("05_справочник_аптек.xlsx", "05. Справочник аптек")
    profile_excel("06_справочник_аптек_коды.xlsx", "06. Справочник аптек — коды")
    profile_excel("07_справочник_аптек_города.xlsx", "07. Справочник аптек — города")

    # --- Kaspi выписки ---
    profile_kaspi_files()

    # --- Ежедневные остатки (2.9 GB) ---
    profile_daily_balances()

    # --- Кросс-табличные проверки ключей ---
    sales_aptek_ids = set(
        pd.read_csv(RAW_DIR / "01_продажи_апрель_2023.csv", sep=CSV_SEP, encoding=CSV_ENCODING,
                    dtype=str, usecols=["APTEKA_ID"])["APTEKA_ID"].unique()
    )
    prihod_aptek_ids = set(
        pd.read_csv(RAW_DIR / "02_приход_апрель_2023.csv", sep=CSV_SEP, encoding=CSV_ENCODING,
                    dtype=str, usecols=["APTEKA_ID"])["APTEKA_ID"].unique()
    )
    sales_ware_ids = set(
        pd.read_csv(RAW_DIR / "01_продажи_апрель_2023.csv", sep=CSV_SEP, encoding=CSV_ENCODING,
                    dtype=str, usecols=["WARE_ID"])["WARE_ID"].unique()
    )
    prihod_ware_ids = set(
        pd.read_csv(RAW_DIR / "02_приход_апрель_2023.csv", sep=CSV_SEP, encoding=CSV_ENCODING,
                    dtype=str, usecols=["WARE_ID"])["WARE_ID"].unique()
    )
    sales_docnums = set(
        pd.read_csv(RAW_DIR / "01_продажи_апрель_2023.csv", sep=CSV_SEP, encoding=CSV_ENCODING,
                    dtype=str, usecols=["DOCNUM"])["DOCNUM"].unique()
    )

    cross_check_pharmacies(sales_aptek_ids, prihod_aptek_ids, aptek_dict)
    cross_check_products(sales_ware_ids, prihod_ware_ids, product_dict)
    cross_check_city_dicts()
    check_kaspi_join_keys(sales_docnums, aptek_dict)

    hr("Готово")


if __name__ == "__main__":
    main()
