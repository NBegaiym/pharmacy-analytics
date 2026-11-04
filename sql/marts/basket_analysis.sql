-- basket_analysis.sql
-- Решение 4 (сколько позиций в чеке, почему, как увеличить) -- docs/hypotheses.md, Дашборд 2 COO
--
-- Один чек = один DOCNUM в рамках аптеки (см. docs/data_profile.md: DOCNUM уникален в
-- рамках аптеки). "Позиций в чеке" = COUNT(*) строк чека -- НЕ SUM(QUANT): QUANT дробный
-- в 7.8% строк продаж (товары, продаваемые частями упаковки), сумма количества не равна
-- количеству разных позиций (см. docs/data_profile.md, п.9).

CREATE SCHEMA IF NOT EXISTS marts;

CREATE OR REPLACE VIEW marts.receipt_basket AS
SELECT
    s.aptek_id,
    ph.name AS aptek_name,
    s.doc_num,
    s.employee,
    MIN(s.posted_at)          AS receipt_at,
    COUNT(*)                  AS items_count,        -- позиций (строк) в чеке
    COUNT(DISTINCT s.ware_id) AS distinct_sku_count,  -- уникальных товаров (если строка на 1 SKU не одна)
    SUM(s.qty)                AS total_units,
    SUM(s.price * s.qty)      AS receipt_value
FROM staging.fact_sales s
LEFT JOIN staging.dim_pharmacies ph ON ph.aptek_id = s.aptek_id
GROUP BY s.aptek_id, ph.name, s.doc_num, s.employee;

-- Динамика позиций в чеке по часам дня / дням недели (docs/hypotheses.md, Решение 4).
CREATE OR REPLACE VIEW marts.basket_by_hour AS
SELECT
    aptek_id,
    EXTRACT(HOUR FROM receipt_at)::int   AS hour_of_day,
    EXTRACT(ISODOW FROM receipt_at)::int AS day_of_week,  -- 1=понедельник .. 7=воскресенье
    COUNT(*)                             AS receipts_count,
    ROUND(AVG(items_count)::numeric, 2)  AS avg_items_per_receipt,
    ROUND(AVG(receipt_value)::numeric, 2) AS avg_receipt_value
FROM marts.receipt_basket
GROUP BY aptek_id, EXTRACT(HOUR FROM receipt_at), EXTRACT(ISODOW FROM receipt_at);

-- Сводка по аптеке: средний чек, позиций в чеке, корреляция размера чека с выручкой.
CREATE OR REPLACE VIEW marts.basket_summary_by_store AS
SELECT
    aptek_id,
    aptek_name,
    COUNT(*)                                            AS receipts_count,
    ROUND(AVG(items_count)::numeric, 2)                 AS avg_items_per_receipt,
    ROUND(AVG(receipt_value)::numeric, 2)                AS avg_receipt_value,
    ROUND(CORR(items_count, receipt_value)::numeric, 3)  AS items_value_correlation
FROM marts.receipt_basket
GROUP BY aptek_id, aptek_name;

-- Позиции в чеке по сотруднику -- для сравнения с employee_performance.avg_items_per_receipt.
CREATE OR REPLACE VIEW marts.basket_by_employee AS
SELECT
    aptek_id,
    employee,
    COUNT(*)                            AS receipts_count,
    ROUND(AVG(items_count)::numeric, 2) AS avg_items_per_receipt
FROM marts.receipt_basket
WHERE employee IS NOT NULL
GROUP BY aptek_id, employee;

-- Товары-"одиночки": почти всегда покупают в чеке из 1 позиции -- кандидаты на
-- кросс-продажу/выкладку у кассы (docs/hypotheses.md, Решение 4).
CREATE OR REPLACE VIEW marts.single_item_products AS
WITH item_receipts AS (
    SELECT s.ware_id, rb.items_count
    FROM staging.fact_sales s
    JOIN marts.receipt_basket rb ON rb.doc_num = s.doc_num AND rb.aptek_id = s.aptek_id
)
SELECT
    ir.ware_id,
    pr.name AS ware_name,
    COUNT(*)                                             AS times_sold,
    COUNT(*) FILTER (WHERE ir.items_count = 1)            AS times_sold_alone,
    ROUND(
        (COUNT(*) FILTER (WHERE ir.items_count = 1))::numeric / NULLIF(COUNT(*), 0) * 100
    , 1) AS pct_sold_alone
FROM item_receipts ir
LEFT JOIN staging.dim_products pr ON pr.ware_id = ir.ware_id
GROUP BY ir.ware_id, pr.name
HAVING COUNT(*) >= 20  -- отсекаем товары с малой статистикой
ORDER BY pct_sold_alone DESC;

-- Топ пар товаров в одном чеке (self-join по doc_num) -- база для допродажи "вместе с X часто берут Y".
-- MATERIALIZED: self-join по 826K строк продаж занимает ~20 сек -- многовато для карточки
-- дашборда, которая должна открываться быстро. Обновлять после перезагрузки staging:
--   REFRESH MATERIALIZED VIEW marts.product_pair_affinity;
DROP MATERIALIZED VIEW IF EXISTS marts.product_pair_affinity;
CREATE MATERIALIZED VIEW marts.product_pair_affinity AS
SELECT
    a.ware_id AS ware_id_1,
    pr1.name  AS ware_name_1,
    b.ware_id AS ware_id_2,
    pr2.name  AS ware_name_2,
    COUNT(DISTINCT a.doc_num) AS times_bought_together
FROM staging.fact_sales a
JOIN staging.fact_sales b
    ON a.doc_num = b.doc_num AND a.aptek_id = b.aptek_id AND a.ware_id < b.ware_id
LEFT JOIN staging.dim_products pr1 ON pr1.ware_id = a.ware_id
LEFT JOIN staging.dim_products pr2 ON pr2.ware_id = b.ware_id
GROUP BY a.ware_id, pr1.name, b.ware_id, pr2.name
HAVING COUNT(DISTINCT a.doc_num) >= 10
ORDER BY times_bought_together DESC;
