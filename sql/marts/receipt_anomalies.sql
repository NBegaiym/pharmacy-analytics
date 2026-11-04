-- receipt_anomalies.sql
-- Аномалии в чеках как индикатор фрода/ошибок кассира -- docs/hypotheses.md, доп.гипотеза COO
--
-- Список для проверки, НЕ готовый повод увольнять -- единичный случай ничего не значит,
-- повторяющийся паттерн у одного сотрудника/аптеки -- повод разобраться.
--
-- Сигнал "дублирующиеся чеки" из docs/hypotheses.md сюда НЕ включён: полные дубли строк
-- уже удалены на Этапе 3 (etl/02_clean_transform.py, ~0.06% продаж) -- в staging их нет.
-- Сигнал "возвраты на сотрудника через Kaspi" тоже не включён -- Kaspi нельзя достоверно
-- привязать к сотруднику/чеку, см. docs/data_profile.md, п.2.1.

CREATE SCHEMA IF NOT EXISTS marts;

CREATE OR REPLACE VIEW marts.receipt_anomalies AS
WITH receipt_stats AS (
    SELECT
        s.aptek_id,
        s.employee,
        s.doc_num,
        MIN(s.created_at)                                            AS created_at,
        MIN(s.posted_at)                                             AS posted_at,
        SUM(s.price * s.qty)                                          AS receipt_value,
        BOOL_OR(s.price != s.price_alt)                               AS has_price_mismatch,
        EXTRACT(EPOCH FROM (MIN(s.posted_at) - MIN(s.created_at))) / 60 AS creation_to_posting_minutes
    FROM staging.fact_sales s
    GROUP BY s.aptek_id, s.employee, s.doc_num
),
employee_stats AS (
    SELECT
        aptek_id,
        employee,
        AVG(receipt_value)    AS avg_receipt_value,
        STDDEV(receipt_value) AS stddev_receipt_value
    FROM receipt_stats
    WHERE employee IS NOT NULL
    GROUP BY aptek_id, employee
)
SELECT
    rs.aptek_id,
    ph.name AS aptek_name,
    rs.employee,
    rs.doc_num,
    ROUND(rs.receipt_value::numeric, 2)              AS receipt_value,
    rs.has_price_mismatch,
    ROUND(rs.creation_to_posting_minutes::numeric, 1) AS creation_to_posting_minutes,
    (rs.receipt_value > es.avg_receipt_value + 3 * es.stddev_receipt_value) AS is_unusually_large
FROM receipt_stats rs
LEFT JOIN staging.dim_pharmacies ph ON ph.aptek_id = rs.aptek_id
LEFT JOIN employee_stats es ON es.aptek_id = rs.aptek_id AND es.employee = rs.employee;

-- Свод по сотруднику -- для дашборда: сколько раз и каких сигналов накопилось.
CREATE OR REPLACE VIEW marts.receipt_anomalies_by_employee AS
SELECT
    aptek_id,
    aptek_name,
    employee,
    COUNT(*)                                            AS receipts_count,
    COUNT(*) FILTER (WHERE has_price_mismatch)           AS price_mismatch_count,
    COUNT(*) FILTER (WHERE is_unusually_large)           AS unusually_large_count,
    ROUND(AVG(creation_to_posting_minutes)::numeric, 1)  AS avg_creation_to_posting_minutes
FROM marts.receipt_anomalies
WHERE employee IS NOT NULL
GROUP BY aptek_id, aptek_name, employee;
