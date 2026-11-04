# -*- coding: utf-8 -*-
"""
Создание дашбордов CEO/COO в Metabase через REST API (Этап 5).

Metabase не хранит вопросы/дашборды в git — это внутренние объекты приложения. Этот скрипт
воспроизводит их программно: создаёт 26 SQL-вопросов поверх вьюх из sql/marts/, раскладывает
по двум дашбордам с сеткой 18 колонок, чинит визуализацию для графиков с двумя числовыми
колонками (Metabase не может сам угадать оси), группирует в коллекции и подключает фильтры
(Аптека — COO, Город — CEO) через template tags в native SQL.

Идемпотентность: скрипт создаёт НОВЫЕ карточки при каждом запуске (не ищет существующие
по имени) — при повторном запуске получится дубликат набора карточек/дашбордов. Это разовый
скрипт первоначальной настройки, не скрипт синхронизации.

Требует переменные окружения:
    METABASE_URL      -- напр. http://metabase-xxx.sslip.io
    METABASE_API_KEY  -- ключ с правами Administrators
                          (Admin -> Настройки -> Аутентификация -> API-ключи -> Создать)

Запуск:
    METABASE_URL=... METABASE_API_KEY=... python metabase/build_dashboards.py
"""

import json
import os
import sys
import urllib.error
import urllib.request
import uuid

MB_URL = os.environ.get("METABASE_URL", "").rstrip("/")
MB_KEY = os.environ.get("METABASE_API_KEY", "")
DB_ID = 2  # id базы pharmacy_analytics внутри Metabase (см. GET /api/database)

if not MB_URL or not MB_KEY:
    sys.exit("Нужны переменные окружения METABASE_URL и METABASE_API_KEY")


def call(method, path, body=None):
    url = MB_URL + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-api-key", MB_KEY)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def make_card(name, sql, display="table", viz=None, template_tags=None):
    native = {"query": sql}
    if template_tags:
        native["template-tags"] = template_tags
    status, card = call("POST", "/api/card", {
        "name": name,
        "dataset_query": {"type": "native", "database": DB_ID, "native": native},
        "display": display,
        "visualization_settings": viz or {},
        "collection_id": None,
    })
    if status not in (200, 202):
        print("FAILED:", name, status, str(card)[:300])
        return None
    print("created:", name, "-> card_id", card["id"])
    return card["id"]


def aptek_tag():
    """text template tag {{aptek}} -- частичное совпадение по aptek_name (ILIKE)."""
    return {"aptek": {"id": str(uuid.uuid4()), "name": "aptek", "display-name": "Аптека", "type": "text"}}


def city_tag():
    return {"city": {"id": str(uuid.uuid4()), "name": "city", "display-name": "Город", "type": "text"}}


# ---------------------------------------------------------------------------
# CEO — 12 карточек (docs/hypotheses.md, Дашборд 1)
# (name, sql, display, viz, size_x, size_y, template_tags)
# ---------------------------------------------------------------------------
CEO_CARDS = [
    ("Выручка сети (мес.)",
     'SELECT network_revenue AS "Выручка" FROM marts.network_pnl_summary',
     "scalar", None, 6, 4, None),

    ("Маржа сети, %",
     'SELECT network_margin_pct AS "Маржа %" FROM marts.network_pnl_summary',
     "scalar", None, 6, 4, None),

    ("Доля безнала по сети, %",
     'SELECT cashless_share_pct AS "Безнал %" FROM marts.network_cashless_share',
     "scalar", None, 6, 4, None),

    ("Динамика выручки сети по дням", """
        SELECT sale_date AS "Дата", SUM(revenue) AS "Выручка"
        FROM marts.store_health_daily GROUP BY sale_date ORDER BY sale_date
    """, "line", None, 18, 6, None),

    # Дальше -- 5 карточек с фильтром "Город" (частичное совпадение по city).
    ("Рейтинг аптек по выручке и марже", """
        SELECT aptek_name AS "Аптека", city AS "Город", revenue AS "Выручка",
               margin_pct AS "Маржа %", receipts_count AS "Чеков"
        FROM marts.store_health_summary WHERE 1=1 [[AND city ILIKE '%' || {{city}} || '%']]
        ORDER BY revenue DESC
    """, "table", None, 18, 10, city_tag()),

    ("Матрица здоровья аптеки (выручка x маржа)", """
        SELECT aptek_name AS "Аптека", revenue AS "Выручка", margin_pct AS "Маржа %",
               receipts_count AS "Чеков", city
        FROM marts.store_health_summary WHERE 1=1 [[AND city ILIKE '%' || {{city}} || '%']]
    """, "scatter", {"graph.dimensions": ["Выручка"], "graph.metrics": ["Маржа %"], "scatter.bubble": "Чеков"},
     9, 8, city_tag()),

    ("Стабильность продаж (коэф. вариации выручки по дням)", """
        SELECT aptek_name AS "Аптека", revenue_volatility_cv AS "Волатильность", city
        FROM marts.store_health_summary WHERE 1=1 [[AND city ILIKE '%' || {{city}} || '%']]
        ORDER BY revenue_volatility_cv DESC NULLS LAST LIMIT 20
    """, "bar", None, 9, 8, city_tag()),

    ("Деньги, замороженные в неликвиде", """
        SELECT aptek_name AS "Аптека", dead_stock_value AS "Неликвид", city
        FROM marts.store_health_summary WHERE 1=1 [[AND city ILIKE '%' || {{city}} || '%']]
        ORDER BY dead_stock_value DESC NULLS LAST LIMIT 20
    """, "bar", None, 9, 8, city_tag()),

    ("Списания, % от выручки", """
        SELECT aptek_name AS "Аптека", writeoff_pct_of_revenue AS "Списания %", city
        FROM marts.store_health_summary WHERE 1=1 [[AND city ILIKE '%' || {{city}} || '%']]
        ORDER BY writeoff_pct_of_revenue DESC NULLS LAST LIMIT 20
    """, "bar", None, 9, 8, city_tag()),

    ("Динамика внутри месяца: 1-я неделя vs последняя", """
        SELECT p.name AS "Аптека", w.first_week_revenue AS "1-я неделя",
               w.last_week_revenue AS "Последняя неделя", w.wow_change_pct AS "Изменение %"
        FROM marts.store_week_over_week w JOIN staging.dim_pharmacies p ON p.aptek_id = w.aptek_id
        ORDER BY w.wow_change_pct DESC NULLS LAST LIMIT 20
    """, "table", None, 18, 10, None),

    ("Комиссия Kaspi по файлу-выписке", """
        SELECT source_account AS "Файл", fee_total AS "Комиссия"
        FROM marts.payment_mix_summary WHERE operation_type = 'Покупка' ORDER BY fee_total DESC
    """, "bar", None, 9, 8, None),

    ("Разброс закупочных цен по поставщикам", """
        SELECT ware_name AS "Товар", suppliers_count AS "Поставщиков",
               cheapest_supplier_price AS "Мин. цена", priciest_supplier_price AS "Макс. цена",
               max_min_ratio AS "Разброс"
        FROM marts.procurement_price_variance ORDER BY max_min_ratio DESC LIMIT 20
    """, "table", None, 18, 10, None),
]

# ---------------------------------------------------------------------------
# COO — 14 карточек (docs/hypotheses.md, Дашборд 2)
# ---------------------------------------------------------------------------
COO_CARDS = [
    ("Активных сотрудников",
     'SELECT count(DISTINCT employee) AS "Сотрудников" FROM marts.employee_performance',
     "scalar", None, 6, 4, None),

    ("Средние позиции в чеке по сети",
     'SELECT ROUND(AVG(avg_items_per_receipt)::numeric,2) AS "Позиций в чеке" FROM marts.basket_summary_by_store',
     "scalar", None, 6, 4, None),

    ("Средний чек по сети",
     'SELECT ROUND(AVG(avg_receipt_value)::numeric,2) AS "Средний чек" FROM marts.basket_summary_by_store',
     "scalar", None, 6, 4, None),

    # Дальше -- карточки с фильтром "Аптека" (частичное совпадение по aptek_name).
    ("Рейтинг сотрудников относительно своей аптеки", """
        SELECT aptek_name AS "Аптека", employee AS "Сотрудник", revenue AS "Выручка",
               pct_of_aptek_avg_revenue AS "% от среднего по аптеке",
               avg_items_per_receipt AS "Позиций в чеке",
               is_multi_aptek_employee AS "Разъездной"
        FROM marts.employee_performance WHERE 1=1 [[AND aptek_name ILIKE '%' || {{aptek}} || '%']]
        ORDER BY pct_of_aptek_avg_revenue ASC NULLS LAST LIMIT 30
    """, "table", None, 18, 10, aptek_tag()),

    ("Дни запаса по товару (избыток -> кандидат на вывоз)", """
        SELECT aptek_name AS "Аптека", ware_name AS "Товар", avg_daily_sales AS "Продаж/день",
               current_stock AS "Остаток", days_of_cover AS "Дней запаса"
        FROM marts.sku_movement WHERE qty_sold_total > 0 [[AND aptek_name ILIKE '%' || {{aptek}} || '%']]
        ORDER BY days_of_cover DESC NULLS LAST LIMIT 20
    """, "table", None, 18, 10, aptek_tag()),

    ("Дефицит товара (кандидат на завоз)", """
        SELECT aptek_name AS "Аптека", ware_name AS "Товар", days_out_of_stock AS "Дней без остатка",
               qty_sold_total AS "Продано за период"
        FROM marts.sku_movement WHERE days_out_of_stock > 0 [[AND aptek_name ILIKE '%' || {{aptek}} || '%']]
        ORDER BY days_out_of_stock DESC LIMIT 20
    """, "table", None, 18, 10, aptek_tag()),

    ("Риск списания по сроку годности", """
        SELECT aptek_name AS "Аптека", ware_name AS "Товар", pct_sold_near_expiry AS "% продаж с близким сроком"
        FROM marts.sku_movement WHERE pct_sold_near_expiry IS NOT NULL [[AND aptek_name ILIKE '%' || {{aptek}} || '%']]
        ORDER BY pct_sold_near_expiry DESC LIMIT 20
    """, "table", None, 18, 10, aptek_tag()),

    ("Лаг \"приход -> первая продажа\"", """
        SELECT aptek_name AS "Аптека", ware_name AS "Товар", days_receipt_to_first_sale AS "Дней до первой продажи"
        FROM marts.sku_movement WHERE days_receipt_to_first_sale IS NOT NULL [[AND aptek_name ILIKE '%' || {{aptek}} || '%']]
        ORDER BY days_receipt_to_first_sale DESC LIMIT 20
    """, "table", None, 18, 10, aptek_tag()),

    # Сетевые (без фильтра по аптеке) -- гистограмма и почасовая динамика.
    ("Распределение позиций в чеке", """
        SELECT items_count AS "Позиций в чеке", count(*) AS "Чеков"
        FROM marts.receipt_basket GROUP BY items_count ORDER BY items_count LIMIT 15
    """, "bar", {"graph.dimensions": ["Позиций в чеке"], "graph.metrics": ["Чеков"]}, 9, 8, None),

    ("Позиций в чеке по часам дня", """
        SELECT hour_of_day AS "Час", ROUND(AVG(avg_items_per_receipt)::numeric,2) AS "Позиций в чеке"
        FROM marts.basket_by_hour GROUP BY hour_of_day ORDER BY hour_of_day
    """, "line", {"graph.dimensions": ["Час"], "graph.metrics": ["Позиций в чеке"]}, 9, 8, None),

    ("Товары-\"одиночки\" (кандидаты на кросс-продажу)", """
        SELECT ware_name AS "Товар", times_sold AS "Продаж", pct_sold_alone AS "% в одиночном чеке"
        FROM marts.single_item_products ORDER BY pct_sold_alone DESC LIMIT 20
    """, "table", None, 18, 10, None),

    ("Топ связок товаров в одном чеке", """
        SELECT ware_name_1 AS "Товар 1", ware_name_2 AS "Товар 2", times_bought_together AS "Раз вместе"
        FROM marts.product_pair_affinity ORDER BY times_bought_together DESC LIMIT 20
    """, "table", None, 18, 10, None),

    ("Загрузка кассиров по часам", """
        SELECT hour_of_day AS "Час", ROUND(AVG(avg_receipts_per_employee)::numeric,1) AS "Чеков на сотрудника"
        FROM marts.staffing_load_by_hour GROUP BY hour_of_day ORDER BY hour_of_day
    """, "bar", {"graph.dimensions": ["Час"], "graph.metrics": ["Чеков на сотрудника"]}, 9, 8, None),

    ("Аномалии в чеках по сотруднику", """
        SELECT aptek_name AS "Аптека", employee AS "Сотрудник", receipts_count AS "Чеков",
               price_mismatch_count AS "Расхожд. цены", unusually_large_count AS "Крупные чеки"
        FROM marts.receipt_anomalies_by_employee WHERE unusually_large_count > 0
        [[AND aptek_name ILIKE '%' || {{aptek}} || '%']]
        ORDER BY unusually_large_count DESC LIMIT 20
    """, "table", None, 9, 8, aptek_tag()),
]


def create_cards_and_layout(cards_spec):
    """Создаёт карточки и раскладывает по сетке шириной 18 колонок.

    Возвращает (dashcards, filter_param) -- filter_param описывает единый параметр
    дашборда (Аптека или Город), если хотя бы одна карточка использует фильтр.
    """
    dashcards = []
    row, col, row_height = 0, 0, 0
    tmp_id = -1
    filter_param = None  # {"tag_name": ..., "display_name": ..., "param_id": ...}

    for name, sql, display, viz, size_x, size_y, tags in cards_spec:
        card_id = make_card(name, sql.strip(), display, viz, tags)
        if card_id is None:
            continue

        if col + size_x > 18:
            row += row_height
            col, row_height = 0, 0

        dashcard = {"id": tmp_id, "card_id": card_id, "row": row, "col": col,
                    "size_x": size_x, "size_y": size_y}

        if tags:
            tag_name = list(tags.keys())[0]
            if filter_param is None:
                filter_param = {
                    "param_id": str(uuid.uuid4()),
                    "tag_name": tag_name,
                    "display_name": tags[tag_name]["display-name"],
                }
            dashcard["parameter_mappings"] = [{
                "parameter_id": filter_param["param_id"],
                "card_id": card_id,
                "target": ["variable", ["template-tag", filter_param["tag_name"]]],
            }]

        dashcards.append(dashcard)
        tmp_id -= 1
        col += size_x
        row_height = max(row_height, size_y)

    return dashcards, filter_param


def build_dashboard(name, description, cards_spec):
    status, dash = call("POST", "/api/dashboard", {"name": name, "description": description, "collection_id": None})
    if status not in (200, 202):
        print("FAILED to create dashboard:", name, status, dash)
        return None
    dash_id = dash["id"]
    print("created dashboard:", name, "-> id", dash_id)

    dashcards, filter_param = create_cards_and_layout(cards_spec)
    body = {"dashcards": dashcards}
    if filter_param:
        body["parameters"] = [{
            "id": filter_param["param_id"],
            "name": filter_param["display_name"],
            "slug": filter_param["tag_name"],
            "type": "string/=",
        }]
    status, result = call("PUT", f"/api/dashboard/{dash_id}", body)
    print("update dashboard cards:", status)
    return dash_id


def organize_into_collections(ceo_dash_id, coo_dash_id, ceo_card_ids, coo_card_ids):
    status, ceo_coll = call("POST", "/api/collection", {"name": "CEO — Стратегический", "color": "#509EE3"})
    status2, coo_coll = call("POST", "/api/collection", {"name": "COO — Операционный", "color": "#88BF4D"})
    if not (isinstance(ceo_coll, dict) and isinstance(coo_coll, dict)):
        print("collection creation failed")
        return
    for cid in ceo_card_ids:
        call("PUT", f"/api/card/{cid}", {"collection_id": ceo_coll["id"]})
    for cid in coo_card_ids:
        call("PUT", f"/api/card/{cid}", {"collection_id": coo_coll["id"]})
    call("PUT", f"/api/dashboard/{ceo_dash_id}", {"collection_id": ceo_coll["id"]})
    call("PUT", f"/api/dashboard/{coo_dash_id}", {"collection_id": coo_coll["id"]})
    print("moved dashboards + cards into dedicated collections")


def main():
    ceo_dash_id = build_dashboard("CEO — Стратегический", "Сеть целиком, тренды, сравнение аптек", CEO_CARDS)
    coo_dash_id = build_dashboard("COO — Операционный", "Конкретные аптеки, сотрудники, товары, чек", COO_CARDS)

    # id-шники карточек не возвращаются напрямую из build_dashboard -- перечитываем со свежесозданных
    # дашбордов, чтобы разложить по коллекциям.
    _, ceo_dash = call("GET", f"/api/dashboard/{ceo_dash_id}")
    _, coo_dash = call("GET", f"/api/dashboard/{coo_dash_id}")
    ceo_card_ids = [dc["card_id"] for dc in ceo_dash["dashcards"]]
    coo_card_ids = [dc["card_id"] for dc in coo_dash["dashcards"]]

    organize_into_collections(ceo_dash_id, coo_dash_id, ceo_card_ids, coo_card_ids)
    print("\nГотово. CEO dashboard id:", ceo_dash_id, "COO dashboard id:", coo_dash_id)


if __name__ == "__main__":
    main()
