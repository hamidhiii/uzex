import pytest

from app import db, verify
from app.guard import precheck
from app.lang import detect
from app.resolve import resolve_entity


# ------------------------------------------------------------------ db
@pytest.mark.parametrize("sql", [
    "DROP TABLE deals",
    "SELECT 1; DROP TABLE deals",
    "INSERT INTO deals VALUES (1)",
    "COPY deals TO 'x.csv'",
    "ATTACH 'x.db'",
    "SELECT * FROM read_csv('data/deals_2022.csv')",
    "SELECT getenv('LLM_API_KEY')",
    "PRAGMA database_list",
    "SET threads=1",
    "SELECT * FROM glob('*')",
    "UPDATE deals SET amount=0",
    "CREATE TABLE x AS SELECT 1",
    "INSTALL httpfs",
    "SELECT * FROM 'data/deals_2022.csv'",
])
def test_forbidden_sql(sql):
    with pytest.raises(Exception):
        db.run_sql(sql)


def test_select_ok_and_literals_ignored():
    r = db.run_sql("SELECT count(*) AS n FROM deals WHERE item_name ILIKE '%drop%' OR status = 'set'")
    assert r["columns"] == ["n"] and r["row_count"] == 1
    r = db.run_sql("SELECT replace(product, 'а', 'a') AS p FROM products LIMIT 3")
    assert r["row_count"] == 3


def test_truncation_and_dates():
    r = db.run_sql("SELECT deal_id, deal_date FROM deals ORDER BY deal_id LIMIT 100")
    assert r["truncated"] and r["row_count"] == 100 and len(r["rows"]) == 50
    assert r["rows"][0][1] == "03.01.2022"


def test_counts():
    assert db.run_sql("SELECT count(*) FROM deals")["rows"][0][0] == 500000


# ------------------------------------------------------------------ resolve
@pytest.mark.parametrize("text,product", [
    ("paxta", "Хлопок-сырец"), ("sement", "Портландцемент"), ("цемента", "Портландцемент"),
    ("хлопок сырец", "Хлопок-сырец"), ("bug‘doy", "Пшеница"), ("карбамида", "Карбамид"),
    ("АИ-92", "Бензин автомобильный"), ("солярка", "Дизельное топливо"), ("мазут", "Мазут топочный"),
    ("g‘isht", "Кирпич керамический"), ("shakar", "Сахар белый"), ("guruch", "Рис шлифованный"), ("un", "Мука пшеничная"),
])
def test_resolve_product(text, product):
    r = resolve_entity("product", text)
    assert r["found"] and r["candidates"][0]["product"] == product


@pytest.mark.parametrize("text", ["золото", "iPhone", "нефть", "oltin"])
def test_resolve_product_missing(text):
    assert not resolve_entity("product", text)["found"]


def test_resolve_company():
    r = resolve_entity("company", "996831451")
    assert r["found"] and r["candidates"][0]["name"] == "XUSIT POLIMER MCHJ"
    r = resolve_entity("company", "Хусит полимер")
    assert r["found"] and r["candidates"][0]["inn"] == "996831451"
    assert not resolve_entity("company", "123456789")["found"]


@pytest.mark.parametrize("text", ["bk45", "BK-045", "045", "BK045"])
def test_resolve_broker(text):
    r = resolve_entity("broker", text)
    assert r["found"] and r["candidates"][0]["broker_code"] == "BK045"


@pytest.mark.parametrize("text,uz", [
    ("Самаркандской области", "Samarqand viloyati"), ("Ташкент", "Toshkent shahri"),
    ("Ташкентская область", "Toshkent viloyati"), ("Fargona", "Farg‘ona viloyati"), ("Нукус", "Qoraqalpog‘iston Respublikasi"),
])
def test_resolve_region(text, uz):
    r = resolve_entity("region", text)
    assert r["found"] and r["candidates"][0]["region_uz"] == uz


# ------------------------------------------------------------------ lang
@pytest.mark.parametrize("q,lang", [
    ("Сколько сделок по карбамиду было заключено в 2025 году?", "ru"),
    ("Какой срок оплаты по контракту 101429?", "ru"),
    ("Компания XUSIT POLIMER плательщик НДС?", "ru"),
    ("2024-yilda paxta xom ashyosi bo‘yicha bitimlar summasi?", "uz"),
    ("BK045 brokeri qaysi viloyatdan?", "uz"),
    ("Sement qancha sotilgan?", "uz"),
    ("2025 йилда қанча битим тузилган?", "uz-cyrl"),
    ("Пахта бўйича энг кўп сотувчи ким?", "uz-cyrl"),
    ("Неча битим бекор килинган 2024 йилда?", "uz-cyrl"),
    ("salom", "uz"), ("assalomu alaykum", "uz"), ("nima gap", "uz"), ("XUSIT POLIMER", "uz"),
    ("Привет", "ru"), ("Сколько сделок у XUSIT POLIMER?", "ru"), ("ассалому алайкум", "uz-cyrl"),
    ("нима гап", "uz-cyrl"), ("нима кивоссан", "uz-cyrl"), ("салом", "uz-cyrl"), ("как дела", "ru"),
    ("Ким покупатель по сделке 280092?", "ru"), ("Цемент", "ru"), ("Какая средняя цена пшеницы?", "ru"),
])
def test_lang(q, lang):
    assert detect(q) == lang


# ------------------------------------------------------------------ guard
@pytest.mark.parametrize("q", [
    "Сколько сделок было в 2021 году?", "Какая прибыль у компании с ИНН 996831451?",
    "Кто директор компании XUSIT POLIMER?", "Прогноз цены цемента на 2027", "Сделки за 15.11.2026",
    "Компания с ИНН 123456789", "2021-yilda nechta bitim bo‘lgan?", "Кто покупатель по сделке 100000?",
    "Контракт 999999", "Сделки в ноябре 2026", "BK150 brokeri qaysi viloyatdan?",
    "2026-yil 15-noyabr kuni nechta bitim tuzilgan?", "Сколько сделок 31 октября 2026 года?",
])
def test_guard_blocks(q):
    assert precheck(q, detect(q))["status"] == "no_data"


@pytest.mark.parametrize("q", [
    "Сколько сделок больше 2000 тонн в 2025 году?", "Кто покупатель по сделке 280092 и какой у неё статус?",
    "101429-shartnoma bo‘yicha to‘lov muddati necha kun?", "Курс доллара на 15.03.2024",
    "Средняя цена за тонну пшеницы в марте 2025", "Сделки с января 2021 по март 2022", "Адрес склада по контракту 101429",
    "BK045 brokeri qaysi viloyatdan?", "Сколько сделок 15 мая 2025?", "2025 йил 3 март куни нечта битим тузилган?",
    "Сделки 30 октября 2026",
])
def test_guard_passes(q):
    assert precheck(q, detect(q)) is None


# ------------------------------------------------------------------ verify
def _allowed(*results, question=""):
    a = verify.Allowed()
    for r in results:
        a.add_result(r)
    a.add_question(question)
    return a


def test_verify_numbers_ok():
    a = _allowed({"rows": [[1234567.891, 0.0674, 1520, "АИ-92"]], "row_count": 1})
    for ans in ["Сумма **1 234 567,89 сум**", "≈ **1,23 млн сум**", "Доля **6,74%**", "**1 520** сделок",
                "Бензин АИ-92", "сумма 1234567.89", "1,2 млн"]:
        assert verify.check(ans, a).ok, ans


def test_verify_billions():
    a = _allowed({"rows": [[1234567890.0]], "row_count": 1})
    assert verify.check("Итого **1,2 млрд сум**", a).ok
    assert verify.check("Итого **1,23 млрд сум**", a).ok
    assert not verify.check("Итого **1,5 млрд сум**", a).ok


def test_verify_fabricated():
    a = _allowed({"rows": [[1520, 33.5]], "row_count": 1})
    r = verify.check("Продано **2 750 тонн**, средняя цена 33,5", a)
    assert not r.ok and "2 750" in r.unverified


def test_verify_inn_and_broker():
    a = _allowed({"rows": [["996831451", "XUSIT POLIMER MCHJ", "BK045"]], "row_count": 1})
    assert verify.check("Покупатель XUSIT POLIMER MCHJ (ИНН 996831451), брокер BK045", a).ok
    r = verify.check("Покупатель ИНН 912345678, брокер BK046", a)
    assert not r.ok and "912345678" in r.unverified and "BK046" in r.unverified


def test_verify_question_numbers_and_years():
    a = _allowed({"rows": [[5]], "row_count": 1}, question="Топ-3 покупателя карбамида в 2023")
    assert verify.check("Топ-3 в 2023 году: всего 5", a).ok


def test_verify_dates():
    a = _allowed({"rows": [["15.03.2024", 12500.5]], "row_count": 1})
    assert verify.check("Курс на 15.03.2024 — **12 500,5 сум**", a).ok
    assert not verify.check("Курс на 16.03.2024 — **12 500,5 сум**", a).ok


# ------------------------------------------------------------------ вне темы
@pytest.mark.parametrize("q", [
    "rentoo nima", "Как приготовить плов?", "Какая погода в Ташкенте?", "Toshkentda ob-havo qanday?", "Привет", "salom",
    "Кто президент Узбекистана?", "Python nima?", "Напиши стихотворение", "Сколько будет 2+2?", "Кто ты?",
])
def test_guard_offtopic(q):
    r = precheck(q, detect(q))
    assert r and r["reason"] == "offtopic"


@pytest.mark.parametrize("q", [
    "paxta qancha?", "XUSIT POLIMER", "Самарканд 2024", "996831451", "Канча битим 2026 йилда бекор килинган?",
    "Сколько золота продано на бирже?", "Oltin qancha sotilgan?", "Сколько стоит iPhone на бирже?",
    "Сколько тонн цемента продано в Ташкенте в 2025?", "Средняя цена 1 кг хлопка в 2025",
])
def test_guard_not_offtopic(q):
    r = precheck(q, detect(q))
    assert not (r and r["reason"] == "offtopic")
