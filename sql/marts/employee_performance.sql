-- employee_performance.sql
-- Решение 2 (уволить сотрудника или оставить) -- docs/hypotheses.md, Дашборд 2 COO
--
-- Все метрики считаются по паре (сотрудник, аптека), а не только по сотруднику: 11-17
-- человек в месяц встречаются в нескольких аптеках (см. docs/data_profile.md, п.10) --
-- у таких сотрудников нет одной "домашней" аптеки, агрегировать по нему одному было бы
-- некорректно. Сравнение -- ОТНОСИТЕЛЬНО своей аптеки (% от среднего по аптеке), а не по
-- сети целиком: крупная аптека всегда даст больше трафика, это не заслуга кассира.

CREATE SCHEMA IF NOT EXISTS marts;

CREATE OR REPLACE VIEW marts.employee_performance AS
WITH emp_aptek AS (
    SELECT
        s.employee,
        s.aptek_id,
        COUNT(DISTINCT s.doc_num)                       AS receipts_count,
        COUNT(DISTINCT s.posted_at::date)                AS active_days,
        SUM(s.qty)                                        AS units_sold,
        SUM(s.price * s.qty)                              AS revenue,
        SUM((s.price - s.cost_price) * s.qty)             AS gross_margin,
        SUM(s.price * s.qty) / NULLIF(COUNT(DISTINCT s.doc_num), 0)        AS avg_receipt_value,
        COUNT(*)::numeric / NULLIF(COUNT(DISTINCT s.doc_num), 0)           AS avg_items_per_receipt,
        SUM(s.price * s.qty) / NULLIF(COUNT(DISTINCT s.posted_at::date), 0) AS revenue_per_active_day
    FROM staging.fact_sales s
    WHERE s.employee IS NOT NULL
    GROUP BY s.employee, s.aptek_id
),
aptek_avg AS (
    SELECT
        aptek_id,
        AVG(revenue_per_active_day) AS aptek_avg_revenue_per_active_day,
        AVG(avg_items_per_receipt)  AS aptek_avg_items_per_receipt,
        AVG(avg_receipt_value)      AS aptek_avg_receipt_value
    FROM emp_aptek
    GROUP BY aptek_id
),
employee_multi_aptek AS (
    SELECT employee, COUNT(DISTINCT aptek_id) AS apteks_count
    FROM emp_aptek
    GROUP BY employee
)
SELECT
    e.employee,
    e.aptek_id,
    ph.name AS aptek_name,
    e.receipts_count,
    e.active_days,
    e.units_sold,
    ROUND(e.revenue::numeric, 2)                   AS revenue,
    ROUND(e.gross_margin::numeric, 2)              AS gross_margin,
    ROUND(e.avg_receipt_value::numeric, 2)         AS avg_receipt_value,
    ROUND(e.avg_items_per_receipt::numeric, 2)     AS avg_items_per_receipt,
    ROUND(e.revenue_per_active_day::numeric, 2)    AS revenue_per_active_day,
    ROUND((e.revenue_per_active_day / NULLIF(aa.aptek_avg_revenue_per_active_day, 0) * 100)::numeric, 1)
        AS pct_of_aptek_avg_revenue,
    ROUND((e.avg_items_per_receipt / NULLIF(aa.aptek_avg_items_per_receipt, 0) * 100)::numeric, 1)
        AS pct_of_aptek_avg_basket_size,
    ROUND((e.avg_receipt_value / NULLIF(aa.aptek_avg_receipt_value, 0) * 100)::numeric, 1)
        AS pct_of_aptek_avg_receipt_value,
    (m.apteks_count > 1) AS is_multi_aptek_employee
FROM emp_aptek e
LEFT JOIN staging.dim_pharmacies ph ON ph.aptek_id = e.aptek_id
LEFT JOIN aptek_avg aa ON aa.aptek_id = e.aptek_id
LEFT JOIN employee_multi_aptek m ON m.employee = e.employee;
