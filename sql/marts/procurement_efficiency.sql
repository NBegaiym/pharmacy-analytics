-- procurement_efficiency.sql
-- Разброс закупочных цен по поставщикам на один товар -- docs/hypotheses.md, CEO/доп.гипотезы
--
-- Прямой сигнал "есть с кем поторговаться" или сменить поставщика: если один и тот же
-- WARE_ID закупается у разных поставщиков по заметно разной цене, дешёвый вариант можно
-- масштабировать на всю сеть.

CREATE SCHEMA IF NOT EXISTS marts;

CREATE OR REPLACE VIEW marts.procurement_price_by_supplier AS
SELECT
    r.ware_id,
    pr.name AS ware_name,
    r.supplier,
    COUNT(*)                              AS receipts_count,
    ROUND(AVG(r.cost_price)::numeric, 2)  AS avg_cost_price,
    MIN(r.cost_price)                      AS min_cost_price,
    MAX(r.cost_price)                      AS max_cost_price
FROM staging.fact_receipts r
LEFT JOIN staging.dim_products pr ON pr.ware_id = r.ware_id
GROUP BY r.ware_id, pr.name, r.supplier;

CREATE OR REPLACE VIEW marts.procurement_price_variance AS
SELECT
    ware_id,
    ware_name,
    COUNT(DISTINCT supplier)                                       AS suppliers_count,
    MIN(avg_cost_price)                                             AS cheapest_supplier_price,
    MAX(avg_cost_price)                                             AS priciest_supplier_price,
    ROUND((MAX(avg_cost_price) / NULLIF(MIN(avg_cost_price), 0))::numeric, 2) AS max_min_ratio
FROM marts.procurement_price_by_supplier
GROUP BY ware_id, ware_name
HAVING COUNT(DISTINCT supplier) > 1
ORDER BY max_min_ratio DESC NULLS LAST;
