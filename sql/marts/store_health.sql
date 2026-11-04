-- store_health.sql
-- Решение 1 (закрыть аптеку или оставить) + сетевой P&L (docs/hypotheses.md, Дашборд 1 CEO)
--
-- Зависит от marts.sku_movement (sku_movement.sql) -- применять после него.
-- store_health_summary строится JOIN (не LEFT JOIN) от продаж -- поэтому в неё попадают
-- только реально работавшие в апреле аптеки (122-129), а не весь справочник (589 записей,
-- из них 463 неактивны в апреле, см. docs/data_profile.md п.3).

CREATE SCHEMA IF NOT EXISTS marts;

-- Динамика выручки/маржи/чеков по аптеке по дням.
CREATE OR REPLACE VIEW marts.store_health_daily AS
SELECT
    s.aptek_id,
    ph.name AS aptek_name,
    ph.city,
    ph.legal_entity,
    s.posted_at::date AS sale_date,
    COUNT(DISTINCT s.doc_num)                                    AS receipts_count,
    SUM(s.qty)                                                    AS units_sold,
    SUM(s.price * s.qty)                                          AS revenue,
    SUM((s.price - s.cost_price) * s.qty)                         AS gross_margin,
    ROUND((SUM(s.price * s.qty) / NULLIF(COUNT(DISTINCT s.doc_num), 0))::numeric, 2) AS avg_receipt_value
FROM staging.fact_sales s
LEFT JOIN staging.dim_pharmacies ph ON ph.aptek_id = s.aptek_id
GROUP BY s.aptek_id, ph.name, ph.city, ph.legal_entity, s.posted_at::date;

-- Сводка по аптеке за весь период -- рейтинг, матрица "здоровья", стабильность, неликвид, списания.
CREATE OR REPLACE VIEW marts.store_health_summary AS
WITH sales_agg AS (
    SELECT
        aptek_id,
        COUNT(DISTINCT doc_num)         AS receipts_count,
        COUNT(DISTINCT posted_at::date) AS active_days,
        SUM(qty)                        AS units_sold,
        SUM(price * qty)                AS revenue,
        SUM((price - cost_price) * qty) AS gross_margin
    FROM staging.fact_sales
    GROUP BY aptek_id
),
stability AS (
    SELECT
        aptek_id,
        AVG(revenue)    AS avg_daily_revenue,
        STDDEV(revenue) AS stddev_daily_revenue
    FROM marts.store_health_daily
    GROUP BY aptek_id
),
-- Деньги, замороженные в неликвиде: остаток есть, а продаж по товару в этой аптеке за
-- весь период не было ни одной штуки (docs/hypotheses.md, CEO/Решение 1).
dead_stock AS (
    SELECT
        aptek_id,
        SUM(current_stock * last_cost_price) AS dead_stock_value
    FROM marts.sku_movement
    WHERE qty_sold_total = 0 AND current_stock > 0
    GROUP BY aptek_id
),
writeoffs AS (
    SELECT
        aptek_id,
        SUM(qty_writeoff_total * last_cost_price) AS writeoff_value
    FROM marts.sku_movement
    GROUP BY aptek_id
)
SELECT
    ph.aptek_id,
    ph.name AS aptek_name,
    ph.city,
    ph.legal_entity,
    sa.receipts_count,
    sa.active_days,
    sa.units_sold,
    sa.revenue,
    sa.gross_margin,
    ROUND((sa.gross_margin / NULLIF(sa.revenue, 0) * 100)::numeric, 1)              AS margin_pct,
    ROUND((sa.revenue / NULLIF(sa.receipts_count, 0))::numeric, 2)                  AS avg_receipt_value,
    -- Коэффициент вариации дневной выручки: выше -- нестабильнее продажи внутри месяца.
    ROUND((st.stddev_daily_revenue / NULLIF(st.avg_daily_revenue, 0))::numeric, 3)  AS revenue_volatility_cv,
    COALESCE(ds.dead_stock_value, 0)                                                AS dead_stock_value,
    COALESCE(wo.writeoff_value, 0)                                                  AS writeoff_value,
    ROUND((COALESCE(wo.writeoff_value, 0) / NULLIF(sa.revenue, 0) * 100)::numeric, 2) AS writeoff_pct_of_revenue
FROM staging.dim_pharmacies ph
JOIN sales_agg sa ON sa.aptek_id = ph.aptek_id
LEFT JOIN stability st ON st.aptek_id = ph.aptek_id
LEFT JOIN dead_stock ds ON ds.aptek_id = ph.aptek_id
LEFT JOIN writeoffs wo ON wo.aptek_id = ph.aptek_id;

-- Динамика внутри месяца: выручка первой недели vs последней (докaзательство роста/падения
-- без полноценной истории по месяцам, которой у нас нет -- см. data_profile.md п.4.2).
CREATE OR REPLACE VIEW marts.store_week_over_week AS
WITH bounds AS (
    SELECT MIN(posted_at::date) AS period_start, MAX(posted_at::date) AS period_end
    FROM staging.fact_sales
),
weekly AS (
    SELECT
        s.aptek_id,
        CASE
            WHEN s.posted_at::date < b.period_start + 7 THEN 'first_week'
            WHEN s.posted_at::date >= b.period_end - 6  THEN 'last_week'
        END AS week_bucket,
        SUM(s.price * s.qty) AS revenue
    FROM staging.fact_sales s
    CROSS JOIN bounds b
    WHERE s.posted_at::date < b.period_start + 7 OR s.posted_at::date >= b.period_end - 6
    GROUP BY s.aptek_id, week_bucket
)
SELECT
    aptek_id,
    MAX(revenue) FILTER (WHERE week_bucket = 'first_week') AS first_week_revenue,
    MAX(revenue) FILTER (WHERE week_bucket = 'last_week')  AS last_week_revenue,
    ROUND((
        (MAX(revenue) FILTER (WHERE week_bucket = 'last_week') - MAX(revenue) FILTER (WHERE week_bucket = 'first_week'))
        / NULLIF(MAX(revenue) FILTER (WHERE week_bucket = 'first_week'), 0) * 100
    )::numeric, 1) AS wow_change_pct
FROM weekly
GROUP BY aptek_id;

-- P&L-подобная сводка сети целиком (docs/hypotheses.md, CEO/доп.гипотезы).
-- Kaspi-комиссия -- только сетевой агрегат, см. docs/data_profile.md п.2.1.
-- Источник комиссии -- fee_kaspi_pay_total, НЕ fee_total: колонка "Комиссия за операции (т)"
-- (fee_total) пустая у 100% строк во всех 5 Kaspi-файлах, реальная комиссия хранится в
-- "Комиссия Kaspi Pay (т)" (найдено при построении вьюхи). В сырых данных комиссия
-- записана отрицательным числом (как списание) -- берём ABS() для читаемости в Metabase.
CREATE OR REPLACE VIEW marts.network_pnl_summary AS
SELECT
    SUM(h.revenue)                                                     AS network_revenue,
    SUM(h.gross_margin)                                                 AS network_gross_margin,
    ROUND((SUM(h.gross_margin) / NULLIF(SUM(h.revenue), 0) * 100)::numeric, 1) AS network_margin_pct,
    SUM(h.writeoff_value)                                               AS network_writeoff_value,
    SUM(h.dead_stock_value)                                             AS network_dead_stock_value,
    (SELECT ABS(SUM(fee_kaspi_pay_total)) FROM staging.fact_kaspi_payments) AS network_kaspi_fees
FROM marts.store_health_summary h;
