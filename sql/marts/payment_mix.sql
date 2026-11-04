-- payment_mix.sql
-- Комиссии Kaspi, доля безнала, доля возвратов -- docs/hypotheses.md, доп.гипотезы CEO/COO
--
-- ВАЖНО: только сетевой/файловый агрегат. Kaspi-выписки (08-12) невозможно достоверно
-- привязать к конкретной аптеке/сотруднику/юрлицу -- см. docs/data_profile.md, п.2.1.
-- "source_account" -- ярлык файла-выписки (юрлицо_001..005 по имени файла), НЕ
-- подтверждённое юрлицо из справочника аптек (staging.dim_pharmacies.legal_entity).
--
-- Источник комиссии -- fee_kaspi_pay_total, НЕ fee_total: колонка "Комиссия за операции (т)"
-- пустая у 100% строк во всех 5 файлах (найдено при построении store_health.sql). Комиссия
-- в сырых данных отрицательная (как списание) -- везде берём ABS() для читаемости.

CREATE SCHEMA IF NOT EXISTS marts;

CREATE OR REPLACE VIEW marts.payment_mix_daily AS
SELECT
    operation_date,
    source_account,
    operation_type,
    payment_type,
    COUNT(*)                          AS operations_count,
    SUM(amount)                        AS amount_total,
    ABS(SUM(fee_kaspi_pay_total))      AS fee_total,
    ROUND((ABS(SUM(fee_kaspi_pay_total)) / NULLIF(SUM(amount), 0) * 100)::numeric, 3) AS fee_pct_of_amount
FROM staging.fact_kaspi_payments
GROUP BY operation_date, source_account, operation_type, payment_type;

CREATE OR REPLACE VIEW marts.payment_mix_summary AS
SELECT
    source_account,
    operation_type,
    COUNT(*)                     AS operations_count,
    SUM(amount)                   AS amount_total,
    ABS(SUM(fee_kaspi_pay_total)) AS fee_total,
    ROUND((ABS(SUM(fee_kaspi_pay_total)) / NULLIF(SUM(amount), 0) * 100)::numeric, 3) AS fee_pct_of_amount
FROM staging.fact_kaspi_payments
GROUP BY source_account, operation_type;

-- Доля безнала по сети: SUM(Kaspi "Покупка") / SUM(выручка сети из fact_sales).
-- Наличные в данных не имеют прямого признака -- методологически считаются как остаток
-- (см. docs/data_profile.md, п.15).
CREATE OR REPLACE VIEW marts.network_cashless_share AS
SELECT
    (SELECT SUM(amount) FROM staging.fact_kaspi_payments WHERE operation_type = 'Покупка') AS kaspi_purchases_total,
    (SELECT SUM(price * qty) FROM staging.fact_sales)                                       AS network_revenue_total,
    ROUND((
        (SELECT SUM(amount) FROM staging.fact_kaspi_payments WHERE operation_type = 'Покупка')
        / NULLIF((SELECT SUM(price * qty) FROM staging.fact_sales), 0) * 100
    )::numeric, 1) AS cashless_share_pct;

-- Доля возвратов по Kaspi -- только сетевой показатель, без привязки к сотруднику/точке
-- (docs/hypotheses.md, доп.гипотеза "аномалии в чеках").
CREATE OR REPLACE VIEW marts.network_return_rate AS
SELECT
    source_account,
    COUNT(*) FILTER (WHERE operation_type = 'Возврат')  AS returns_count,
    COUNT(*) FILTER (WHERE operation_type = 'Покупка')  AS purchases_count,
    ROUND((
        (COUNT(*) FILTER (WHERE operation_type = 'Возврат'))::numeric
        / NULLIF(COUNT(*) FILTER (WHERE operation_type = 'Покупка'), 0) * 100
    )::numeric, 2) AS return_rate_pct
FROM staging.fact_kaspi_payments
GROUP BY source_account;
