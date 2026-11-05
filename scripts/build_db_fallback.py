"""Fallback-сборка data/uzex.duckdb строго по схеме ТЗ (раздел 3).

Нужна, пока нет базы от участника 2. Потом файл просто подменяется.
Запуск:  python scripts/build_db_fallback.py [--out data/uzex.duckdb] [--check]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

import duckdb
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.normalize import PRODUCTS, REGIONS, name_key, norm_class, norm_grade, product_by_hs  # noqa: E402

DATA = os.path.join(ROOT, "data")


def num(col: str) -> str:
    return f"TRY_CAST(regexp_replace(\"{col}\", '[\\s\\x{{00A0}}]', '', 'g') AS DOUBLE)"


def dt(col: str) -> str:
    return f"TRY_STRPTIME(NULLIF(\"{col}\", ''), '%d.%m.%Y')::DATE"


def build(out: str) -> None:
    tmp = out + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = duckdb.connect(tmp)
    rd = lambda f: f"read_csv('{os.path.join(DATA, f)}', all_varchar=true, header=true, quote='\"', escape='\"')"  # noqa: E731

    # ---------- справочники
    con.execute("CREATE TABLE regions(region_uz VARCHAR, region_ru VARCHAR, aliases VARCHAR)")
    con.executemany("INSERT INTO regions VALUES (?,?,?)", REGIONS)
    ru2uz = {ru: uz for uz, ru, _ in REGIONS}

    con.execute(f"CREATE TABLE usd_rates AS SELECT {dt('Дата')} AS rate_date, {num('Курс')} AS rate FROM {rd('usd_rates.csv')}")

    # ---------- контракты (нормализация в pandas — 6.6 тыс. строк)
    k = con.execute(f"SELECT * FROM {rd('contracts.csv')}").df()
    kk = pd.DataFrame({
        "contract_id": k["Номер контракта"].astype("int64"),
        "product_name_raw": k["Наименование продукта"],
        "product": [product_by_hs(h, n) for h, n in zip(k["Код ТН ВЭД"], k["Наименование продукта"])],
        "brand": k["Марка товара"].str.strip(),
        "size_raw": k["Размер товара"], "grade_raw": k["Сорт товара"], "type_raw": k["Тип товара"],
        "class_raw": k["Класс товара"],
        "grade": [norm_grade(g, n) for g, n in zip(k["Сорт товара"], k["Наименование продукта"])],
        "product_class": [norm_class(c, n) for c, n in zip(k["Класс товара"], k["Наименование продукта"])],
        "hs_code": k["Код ТН ВЭД"].str.strip(),
        "product_group": k["Группа товара"], "category": k["Категория товара"], "industry": k["Отрасль промышленности"],
        "unit": k["Единица измерения"],
        "lot": k["Лот"].str.replace(r"\s", "", regex=True).astype(float),
        "currency": k["Валюта"].map({"Узбекский сум": "UZS", "Доллар США": "USD"}),
        "base_price": k["Базисная цена"].str.replace(r"\s", "", regex=True).astype(float),
        "deposit_pct": k["Сумма залога"].str.extract(r"(\d+)")[0].astype(int),
        "delivery_basis": k["Базис поставки"], "packaging": k["Упаковка"],
        "region_ru": k["Регион"], "region_uz": k["Регион"].map(ru2uz),
        "warehouse_address": k["Адрес склада"],
        "trade_type": k["Тип торгов"], "contract_type": k["Тип контракта"], "contract_form": k["Форма контракта"],
        "payment_term_days": k["Срок оплаты"].astype(int), "delivery_term_days": k["Срок поставки"].astype(int),
        "seller_inn": k["ИНН продавца"].str.strip(),
        "reg_date": pd.to_datetime(k["Дата регистрации"], format="%d.%m.%Y").dt.date,
        "valid_until": pd.to_datetime(k["Действует до"], format="%d.%m.%Y").dt.date,
    })
    assert kk["product"].notna().all(), "контракт без product"
    assert kk["currency"].notna().all() and kk["region_uz"].notna().all()
    con.register("kk", kk)
    con.execute("CREATE TABLE contracts AS SELECT * FROM kk")

    # ---------- products (категории/единица — по самому частому значению в контрактах)
    rows = []
    for hs, prod, uz, syn in PRODUCTS:
        sub = kk[kk["product"] == prod]
        mode = lambda c: Counter(sub[c]).most_common(1)[0][0] if len(sub) else None  # noqa: E731
        rows.append((prod, hs, uz, syn, mode("category"), mode("product_group"), mode("industry"), mode("unit")))
    con.execute("CREATE TABLE products(product VARCHAR PRIMARY KEY, hs_code VARCHAR, name_uz VARCHAR, synonyms VARCHAR,"
                " category VARCHAR, product_group VARCHAR, industry VARCHAR, unit VARCHAR)")
    con.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?)", rows)

    # ---------- participants / brokers
    p = con.execute(f"SELECT * FROM {rd('participants.csv')}").df()
    pp = pd.DataFrame({
        "inn": p["ИНН"].str.strip(), "name": p["Наименование"], "name_key": p["Наименование"].map(name_key),
        "region_uz": p["Регион"], "region_ru": p["Регион"].map({uz: ru for uz, ru, _ in REGIONS}),
        "activity": p["Вид деятельности"], "role": p["Роль"], "vat_payer": p["Плательщик НДС"].eq("Да"),
    })
    con.register("pp", pp)
    con.execute("CREATE TABLE participants AS SELECT * FROM pp")

    b = con.execute(f"SELECT * FROM {rd('brokers.csv')}").df()
    bb = pd.DataFrame({
        "broker_code": b["Код брокера"].str.strip(), "name": b["Наименование"], "name_key": b["Наименование"].map(name_key),
        "inn": b["ИНН"].str.strip(), "region_uz": b["Регион"],
        "region_ru": b["Регион"].map({uz: ru for uz, ru, _ in REGIONS}),
    })
    con.register("bb", bb)
    con.execute("CREATE TABLE brokers AS SELECT * FROM bb")

    # ---------- deals
    deals_glob = os.path.join(DATA, "deals_*.csv")
    con.execute(f"""
    CREATE TABLE deals AS
    WITH r AS (SELECT * FROM read_csv('{deals_glob}', all_varchar=true, header=true, quote='"', escape='"', union_by_name=true)),
    p AS (
      SELECT
        CAST("Номер сделки" AS BIGINT) AS deal_id,
        {dt('Дата сделки')} AS deal_date,
        CAST("Время сделки" AS TIME) AS deal_time,
        "Тип торгов" AS trade_type,
        {num('Количество')} AS qty_lots,
        {num('Лот')} AS lot,
        "Единица измерения" AS unit,
        {num('Сумма сделки')} AS amount,
        {num('Стартовая цена')} AS start_price,
        CAST("Номер контракта" AS BIGINT) AS contract_id,
        "Наименование товара" AS item_name,
        "Регион" AS region_uz,
        "Тип контракта" AS contract_type,
        "Форма контракта" AS contract_form,
        "Состояние сделки" AS status,
        trim("ИНН продавца") AS seller_inn, trim("ИНН покупателя") AS buyer_inn,
        trim("Брокер продавца") AS seller_broker, trim("Брокер покупателя") AS buyer_broker,
        {dt('Дата оплаты')} AS pay_date, {dt('Дата поставки')} AS delivery_date,
        {num('Поставлено')} AS delivered_lots
      FROM r)
    SELECT
      p.deal_id, p.deal_date, p.deal_time,
      year(p.deal_date)::INT AS year, month(p.deal_date)::INT AS month,
      p.trade_type, p.qty_lots, p.lot, p.unit,
      p.qty_lots * p.lot AS volume,
      p.amount, c.currency, u.rate AS usd_rate,
      CASE WHEN c.currency = 'USD' THEN p.amount * u.rate ELSE p.amount END AS amount_uzs,
      CASE WHEN c.currency = 'USD' THEN p.amount ELSE p.amount / u.rate END AS amount_usd,
      p.start_price,
      p.amount / (p.qty_lots * p.lot) AS unit_price,
      (p.amount / (p.qty_lots * p.lot) / p.start_price - 1) * 100 AS price_change_pct,
      p.contract_id, p.item_name, c.product, c.hs_code, c.category, c.product_group, c.industry,
      c.grade, c.product_class, c.brand,
      p.region_uz, rg.region_ru,
      p.contract_type, p.contract_form, p.status,
      p.status = 'Аннулирован(Не оплачен)' AS is_cancelled,
      p.pay_date IS NOT NULL AS is_paid,
      p.status IN ('Исполнен', 'Исполнен(Частично)') AS is_completed,
      p.seller_inn, s.name AS seller_name, p.buyer_inn, bu.name AS buyer_name,
      p.seller_broker, p.buyer_broker,
      p.pay_date, p.delivery_date, p.delivered_lots,
      p.delivered_lots * p.lot AS delivered_volume,
      date_diff('day', p.deal_date, p.pay_date)::INT AS days_to_pay,
      date_diff('day', p.pay_date, p.delivery_date)::INT AS days_to_deliver
    FROM p
    JOIN contracts c USING (contract_id)
    LEFT JOIN usd_rates u ON u.rate_date = p.deal_date
    LEFT JOIN regions rg ON rg.region_uz = p.region_uz
    LEFT JOIN participants s ON s.inn = p.seller_inn
    LEFT JOIN participants bu ON bu.inn = p.buyer_inn
    ORDER BY p.deal_id
    """)

    # ---------- product_names
    con.execute("""
    CREATE TABLE product_names AS
    WITH a AS (
      SELECT product_name_raw AS raw_name, product, hs_code, count(*) AS contracts_count, 0 AS deals_count
      FROM contracts GROUP BY ALL
      UNION ALL
      SELECT item_name, product, hs_code, 0, count(*) FROM deals GROUP BY ALL)
    SELECT raw_name, product, any_value(hs_code) AS hs_code,
           sum(contracts_count)::INT AS contracts_count, sum(deals_count)::INT AS deals_count
    FROM a GROUP BY raw_name, product
    """)
    con.close()
    os.replace(tmp, out)
    print(f"built {out}")


def check(path: str) -> None:
    con = duckdb.connect(path, read_only=True)
    q = lambda s: con.execute(s).fetchall()  # noqa: E731
    errs = []

    def expect(name, got, want):
        ok = got == want
        print(("OK  " if ok else "FAIL"), name, got, "" if ok else f"(want {want})")
        if not ok:
            errs.append(name)

    expect("deals", q("SELECT count(*) FROM deals")[0][0], 500000)
    expect("by year", q("SELECT year, count(*) FROM deals GROUP BY 1 ORDER BY 1"),
           [(2022, 84299), (2023, 93985), (2024, 106647), (2025, 115763), (2026, 99306)])
    expect("contracts", q("SELECT count(*) FROM contracts")[0][0], 6616)
    expect("participants", q("SELECT count(*) FROM participants")[0][0], 14392)
    expect("brokers", q("SELECT count(*) FROM brokers")[0][0], 120)
    cols = ["deal_id", "deal_date", "amount", "amount_uzs", "product", "seller_inn", "buyer_inn", "region_ru",
            "currency", "usd_rate", "seller_name", "buyer_name"]
    expect("nulls", q("SELECT " + ",".join(f"count(*) FILTER (WHERE {c} IS NULL)" for c in cols) + " FROM deals")[0],
           tuple([0] * len(cols)))
    expect("products", q("SELECT count(DISTINCT product) FROM deals")[0][0], 41)
    expect("unknown product", q("SELECT count(*) FROM deals WHERE product NOT IN (SELECT product FROM products)")[0][0], 0)
    expect("usd deals", q("SELECT count(*), count(*) FILTER (WHERE contract_type='Экспортный') FROM deals WHERE currency='USD'")[0],
           (6590, 6590))
    expect("unit_price>0", q("SELECT count(*) FROM deals WHERE NOT unit_price > 0")[0][0], 0)
    print("status:", q("SELECT status, count(*) FROM deals GROUP BY 1 ORDER BY 2 DESC"))
    print("grade:", q("SELECT grade, count(*) FROM contracts GROUP BY 1 ORDER BY 2 DESC"))
    print("class:", q("SELECT product_class, count(*) FROM contracts GROUP BY 1 ORDER BY 2 DESC"))
    con.close()
    if errs:
        sys.exit("CHECK FAILED: " + ", ".join(errs))
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA, "uzex.duckdb"))
    ap.add_argument("--check", action="store_true", help="только проверить")
    a = ap.parse_args()
    if not a.check:
        build(a.out)
    check(a.out)
