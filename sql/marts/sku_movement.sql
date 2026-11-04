-- sku_movement.sql
-- Решение 3 (товар увезти/привезти) + риск списания + лаг "приход -> первая продажа" +
-- согласованность розничных цен по сети (docs/hypotheses.md, COO/Решение 3, CEO/доп.гипотезы)
--
-- Источник: staging.fact_stock_daily хранится на уровне ПАРТИИ товара (PART_ID), не SKU,
-- и это 12.7M строк (см. docs/data_profile.md, п.4.1) -- поэтому агрегация до (аптека,
-- товар, день), а затем до (аптека, товар) за весь период -- MATERIALIZED VIEW, а не
-- обычная вьюха: как обычная вьюха она пересчитывала бы всю цепочку агрегации заново на
-- каждый запрос из Metabase, что на этом объёме занимает больше минуты и не подходит для
-- интерактивного дашборда. Здесь -- разовая аналитика по снепшоту за апрель 2023, поэтому
-- обновлять материализацию нужно только после перезагрузки staging (см. REFRESH в конце файла).
--
-- Применять ПОСЛЕ загрузки staging (etl/03_load_to_db.py). store_health.sql зависит от
-- marts.sku_movement -- применять этот файл раньше store_health.sql.

CREATE SCHEMA IF NOT EXISTS marts;

DROP MATERIALIZED VIEW IF EXISTS marts.sku_movement CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marts.stock_daily_by_sku CASCADE;

-- Шаг 1: остатки/движение на уровне (аптека, товар, день).
CREATE MATERIALIZED VIEW marts.stock_daily_by_sku AS
SELECT
    aptek_id,
    ware_id,
    date,
    SUM(qty_open)         AS qty_open,
    SUM(qty_received)     AS qty_received,
    SUM(qty_sold)         AS qty_sold,
    SUM(qty_returned)     AS qty_returned,
    SUM(qty_writeoff)     AS qty_writeoff,
    SUM(qty_transfer_in)  AS qty_transfer_in,
    SUM(qty_transfer_out) AS qty_transfer_out,
    SUM(qty_close)        AS qty_close,
    AVG(cost_price)       AS avg_cost_price,
    AVG(retail_price)     AS avg_retail_price
FROM staging.fact_stock_daily
GROUP BY aptek_id, ware_id, date;

CREATE INDEX ix_stock_daily_by_sku_aptek_ware ON marts.stock_daily_by_sku (aptek_id, ware_id);
CREATE INDEX ix_stock_daily_by_sku_date ON marts.stock_daily_by_sku (date);

-- Шаг 2: сводка за весь период по (аптека, товар) -- главная вьюха под решение 3.
CREATE MATERIALIZED VIEW marts.sku_movement AS
WITH period AS (
    SELECT
        MIN(date) AS period_start,
        MAX(date) AS period_end,
        (MAX(date) - MIN(date) + 1) AS period_days
    FROM staging.fact_stock_daily
),
-- Сумма продаж/приходов/списаний/перемещений за ВЕСЬ период -- эти данные за 30.04 надёжны.
-- qty_writeoff: в сырых данных встречаются и положительные, и отрицательные значения
-- почти поровну (475 707 против 472 501 дней по сети), в сумме гасящие друг друга почти
-- до нуля -- похоже на технические корректировки, а не только сами списания. Берём только
-- ПОЛОЖИТЕЛЬНУЮ часть как факт реального списания (потерянные деньги), отрицательные
-- значения (вероятно, сторно/корректировки) в эту метрику не включаем.
agg_full AS (
    SELECT
        aptek_id,
        ware_id,
        SUM(qty_sold)                                   AS qty_sold_total,
        SUM(qty_received)                                AS qty_received_total,
        SUM(qty_writeoff) FILTER (WHERE qty_writeoff > 0) AS qty_writeoff_total,
        SUM(qty_transfer_in)                             AS qty_transfer_in_total,
        SUM(qty_transfer_out)                            AS qty_transfer_out_total
    FROM marts.stock_daily_by_sku
    GROUP BY aptek_id, ware_id
),
-- "Текущий остаток"/последняя цена/дни дефицита -- НЕ за весь период, а на последний
-- НАДЁЖНЫЙ день. Обнаружено при построении вьюхи: в последний день периода (30.04)
-- Остаток_на_конец_дня = 0 буквально у 100% строк без исключения (888 283 из 888 283) --
-- это явный артефакт выгрузки (реальный остаток так не бывает у всей сети одновременно),
-- а не факт по товару. День перед ним (29.04) такой аномалии не показывает. Поэтому берём
-- последний день СТРОГО ДО периода в целом, исключая финальный день выгрузки.
agg_reliable AS (
    SELECT
        d.aptek_id,
        d.ware_id,
        COUNT(*) FILTER (WHERE d.qty_close <= 0)                 AS days_out_of_stock,
        (ARRAY_AGG(d.qty_close ORDER BY d.date DESC))[1]         AS current_stock,
        (ARRAY_AGG(d.avg_cost_price ORDER BY d.date DESC))[1]    AS last_cost_price,
        (ARRAY_AGG(d.avg_retail_price ORDER BY d.date DESC))[1]  AS last_retail_price
    FROM marts.stock_daily_by_sku d
    CROSS JOIN period per
    WHERE d.date < per.period_end
    GROUP BY d.aptek_id, d.ware_id
),
agg AS (
    SELECT
        f.aptek_id,
        f.ware_id,
        f.qty_sold_total,
        f.qty_received_total,
        f.qty_writeoff_total,
        f.qty_transfer_in_total,
        f.qty_transfer_out_total,
        r.days_out_of_stock,
        r.current_stock,
        r.last_cost_price,
        r.last_retail_price
    FROM agg_full f
    LEFT JOIN agg_reliable r ON r.aptek_id = f.aptek_id AND r.ware_id = f.ware_id
),
-- Прокси риска списания: срок годности остатка напрямую не выгружен (см. data_profile.md),
-- поэтому берём долю ПРОДАННЫХ штук товара с сроком годности <= 60 дней от даты продажи.
expiry AS (
    SELECT
        aptek_id,
        ware_id,
        SUM(qty) FILTER (WHERE expiry_date <= posted_at + INTERVAL '60 days') AS qty_sold_near_expiry,
        SUM(qty)                                                               AS qty_sold_with_expiry_known
    FROM staging.fact_sales
    WHERE expiry_date IS NOT NULL
    GROUP BY aptek_id, ware_id
),
first_sale AS (
    SELECT aptek_id, ware_id, MIN(posted_at) AS first_sale_at
    FROM staging.fact_sales
    GROUP BY aptek_id, ware_id
),
first_receipt AS (
    SELECT aptek_id, ware_id, MIN(posted_at) AS first_receipt_at
    FROM staging.fact_receipts
    GROUP BY aptek_id, ware_id
)
SELECT
    a.aptek_id,
    ph.name AS aptek_name,
    ph.city,
    a.ware_id,
    pr.name AS ware_name,
    pr.manufacturer,
    pr.mnn,
    per.period_days,
    a.qty_sold_total,
    ROUND((a.qty_sold_total / NULLIF(per.period_days, 0))::numeric, 3) AS avg_daily_sales,
    a.current_stock,
    ROUND(
        (a.current_stock / NULLIF(a.qty_sold_total / NULLIF(per.period_days, 0), 0))::numeric
    , 1) AS days_of_cover,
    a.days_out_of_stock,
    COALESCE(a.qty_writeoff_total, 0) AS qty_writeoff_total,
    a.qty_transfer_in_total,
    a.qty_transfer_out_total,
    a.last_cost_price,
    a.last_retail_price,
    ROUND((e.qty_sold_near_expiry / NULLIF(e.qty_sold_with_expiry_known, 0) * 100)::numeric, 1) AS pct_sold_near_expiry,
    fr.first_receipt_at,
    fs.first_sale_at,
    -- Может быть отрицательным: товар продавался с остатка, доставленного ДО апреля
    -- (данные -- один месяц, см. docs/data_profile.md). Это не ошибка, а ограничение периода.
    EXTRACT(DAY FROM fs.first_sale_at - fr.first_receipt_at) AS days_receipt_to_first_sale
FROM agg a
CROSS JOIN period per
LEFT JOIN staging.dim_pharmacies ph ON ph.aptek_id = a.aptek_id
LEFT JOIN staging.dim_products pr ON pr.ware_id = a.ware_id
LEFT JOIN expiry e ON e.aptek_id = a.aptek_id AND e.ware_id = a.ware_id
LEFT JOIN first_sale fs ON fs.aptek_id = a.aptek_id AND fs.ware_id = a.ware_id
LEFT JOIN first_receipt fr ON fr.aptek_id = a.aptek_id AND fr.ware_id = a.ware_id;

CREATE INDEX ix_sku_movement_aptek ON marts.sku_movement (aptek_id);
CREATE INDEX ix_sku_movement_ware ON marts.sku_movement (ware_id);

-- Согласованность розничных цен по сети: один и тот же товар может продаваться по разной
-- цене в разных аптеках/в разное время (docs/hypotheses.md, CEO/доп.гипотезы).
-- Обычная (не материализованная) вьюха: считается напрямую от fact_sales, без тяжёлой
-- цепочки агрегации остатков, поэтому пересчёт "на лету" достаточно быстрый.
CREATE OR REPLACE VIEW marts.price_consistency AS
SELECT
    s.ware_id,
    pr.name AS ware_name,
    COUNT(DISTINCT s.aptek_id)                          AS apteks_selling,
    MIN(s.price)                                         AS min_price,
    MAX(s.price)                                         AS max_price,
    ROUND((MAX(s.price) / NULLIF(MIN(s.price), 0))::numeric, 2) AS max_min_price_ratio,
    ROUND(STDDEV(s.price)::numeric, 2)                   AS price_stddev
FROM staging.fact_sales s
LEFT JOIN staging.dim_products pr ON pr.ware_id = s.ware_id
GROUP BY s.ware_id, pr.name
HAVING COUNT(*) > 1
ORDER BY max_min_price_ratio DESC NULLS LAST;

-- Индекс дисбаланса остатков по сети: по действующему веществу (MNN), чтобы сравнивать
-- аналоги, а не только идентичный SKU (docs/hypotheses.md, CEO/Решение 3 сетевой срез).
-- Читает из materialized marts.sku_movement -- быстро.
CREATE OR REPLACE VIEW marts.stock_imbalance_by_mnn AS
WITH by_mnn_aptek AS (
    SELECT
        pr.mnn,
        sm.aptek_id,
        SUM(sm.qty_sold_total) AS qty_sold_total,
        SUM(sm.current_stock)  AS current_stock
    FROM marts.sku_movement sm
    LEFT JOIN staging.dim_products pr ON pr.ware_id = sm.ware_id
    WHERE pr.mnn IS NOT NULL
    GROUP BY pr.mnn, sm.aptek_id
),
mnn_stats AS (
    SELECT mnn, AVG(current_stock) AS network_avg_stock, STDDEV(current_stock) AS network_stddev_stock
    FROM by_mnn_aptek
    GROUP BY mnn
)
SELECT
    b.mnn,
    b.aptek_id,
    b.qty_sold_total,
    b.current_stock,
    ROUND(m.network_avg_stock::numeric, 1) AS network_avg_stock,
    CASE
        WHEN b.qty_sold_total = 0 AND b.current_stock > 0 AND b.current_stock > m.network_avg_stock
            THEN 'избыток'
        WHEN b.current_stock <= 0 AND b.qty_sold_total > 0
            THEN 'дефицит'
        ELSE 'норма'
    END AS balance_status
FROM by_mnn_aptek b
JOIN mnn_stats m ON m.mnn = b.mnn;

-- Чтобы обновить данные материализованных вьюх этого файла после перезагрузки staging:
--   REFRESH MATERIALIZED VIEW marts.stock_daily_by_sku;
--   REFRESH MATERIALIZED VIEW marts.sku_movement;
-- (в этом порядке -- sku_movement читает из stock_daily_by_sku)
