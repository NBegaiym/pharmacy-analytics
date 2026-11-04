-- staffing_load.sql
-- Часы пик и загрузка кассиров -- docs/hypotheses.md, доп.гипотеза (связывает решения 2 и 4)
--
-- Идея: если в час пик работает столько же кассиров, сколько в "тихий" час, кассир либо
-- не успевает допродавать (меньше позиций в чеке -- сверить с marts.basket_by_hour), либо
-- простаивает. Учёта смен/часов работы в данных нет -- прокси активности сотрудника в
-- час = наличие хотя бы одного оформленного им чека в этот час.

CREATE SCHEMA IF NOT EXISTS marts;

-- Считаем в два шага: сперва по (аптека, час, ДЕНЬ), затем усредняем по дням --
-- иначе "чеков на сотрудника" получится суммой за весь месяц, а не реалистичной
-- почасовой нагрузкой (сотрудник за 30 дней в одном и том же часе -- это не 30 сотрудников).
CREATE OR REPLACE VIEW marts.staffing_load_by_hour AS
WITH by_day AS (
    SELECT
        s.aptek_id,
        s.posted_at::date                   AS sale_date,
        EXTRACT(HOUR FROM s.posted_at)::int AS hour_of_day,
        COUNT(DISTINCT s.doc_num)           AS receipts_count,
        COUNT(DISTINCT s.employee)          AS active_employees
    FROM staging.fact_sales s
    WHERE s.employee IS NOT NULL
    GROUP BY s.aptek_id, s.posted_at::date, EXTRACT(HOUR FROM s.posted_at)
)
SELECT
    by_day.aptek_id,
    ph.name AS aptek_name,
    by_day.hour_of_day,
    COUNT(*)                                  AS days_observed,
    ROUND(AVG(receipts_count)::numeric, 1)    AS avg_receipts_per_day,
    ROUND(AVG(active_employees)::numeric, 1)  AS avg_active_employees_per_day,
    ROUND((AVG(receipts_count) / NULLIF(AVG(active_employees), 0))::numeric, 2) AS avg_receipts_per_employee
FROM by_day
LEFT JOIN staging.dim_pharmacies ph ON ph.aptek_id = by_day.aptek_id
GROUP BY by_day.aptek_id, ph.name, by_day.hour_of_day;
