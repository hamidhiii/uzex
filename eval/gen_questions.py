"""Шаблонные вопросы с эталоном, посчитанным pandas ПРЯМО ПО ИСХОДНЫМ CSV (без DuckDB и без кода агента).

Запуск: python eval/gen_questions.py  ->  eval/questions_gen.jsonl
"""
from __future__ import annotations

import glob
import json
import os

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "eval", "questions_gen.jsonl")

# независимая копия соответствия ТН ВЭД -> товар (из ТЗ, раздел 3.3)
HS = {"100199": "Пшеница", "110100": "Мука пшеничная", "170199": "Сахар белый", "252329": "Портландцемент",
      "271012": "Бензин автомобильный", "310210": "Карбамид", "520100": "Хлопок-сырец", "721420": "Арматура стальная",
      "151229": "Масло хлопковое", "690410": "Кирпич керамический", "740311": "Катод медный", "100630": "Рис шлифованный"}


def num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(r"\s", "", regex=True), errors="coerce")


def load():
    d = pd.concat([pd.read_csv(f, dtype=str) for f in sorted(glob.glob(os.path.join(DATA, "deals_*.csv")))], ignore_index=True)
    k = pd.read_csv(os.path.join(DATA, "contracts.csv"), dtype=str)
    p = pd.read_csv(os.path.join(DATA, "participants.csv"), dtype=str)
    b = pd.read_csv(os.path.join(DATA, "brokers.csv"), dtype=str)
    r = pd.read_csv(os.path.join(DATA, "usd_rates.csv"), dtype=str)
    r["date"] = pd.to_datetime(r["Дата"], format="%d.%m.%Y")
    r["rate"] = num(r["Курс"])
    d["date"] = pd.to_datetime(d["Дата сделки"], format="%d.%m.%Y")
    d["year"] = d["date"].dt.year
    d["month"] = d["date"].dt.month
    for c in ["Количество", "Сумма сделки", "Лот", "Стартовая цена"]:
        d[c] = num(d[c])
    kk = k.set_index("Номер контракта")
    d["hs"] = d["Номер контракта"].map(kk["Код ТН ВЭД"])
    d["cur"] = d["Номер контракта"].map(kk["Валюта"])
    d["prod"] = d["hs"].map(HS)
    d = d.merge(r[["date", "rate"]], on="date", how="left")
    d["uzs"] = d["Сумма сделки"].where(d["cur"] == "Узбекский сум", d["Сумма сделки"] * d["rate"])
    d["usd"] = d["Сумма сделки"].where(d["cur"] == "Доллар США", d["Сумма сделки"] / d["rate"])
    d["vol"] = d["Количество"] * d["Лот"]
    d["price"] = d["Сумма сделки"] / d["vol"]
    d["canc"] = d["Состояние сделки"] == "Аннулирован(Не оплачен)"
    return d, k, p, b, r


def main():
    d, k, p, b, r = load()
    Q = []

    def add(lang, cat, q, exp, alt=None, note="", check=""):
        Q.append({"id": f"g{len(Q) + 1:02d}", "lang": lang, "category": cat, "question": q, "expected": exp,
                  "alt_values": alt or [], "note": note, "check_code": check})

    N = lambda v, tol=0.005: {"type": "number", "value": float(v), "tolerance": tol}  # noqa: E731
    T = lambda v: {"type": "text", "value": str(v)}  # noqa: E731
    ND = {"type": "no_data"}

    # ---------- агрегаты: число сделок
    for lang, prod, word, y in [("ru", "Карбамид", "карбамиду", 2025), ("uz", "Карбамид", "karbamid", 2024),
                                ("uz-cyrl", "Портландцемент", "цемент", 2023), ("ru", "Хлопок-сырец", "хлопку-сырцу", 2024),
                                ("uz", "Пшеница", "bug‘doy", 2022)]:
        s = d[(d["prod"] == prod) & (d["year"] == y)]
        q = {"ru": f"Сколько сделок по {word} было заключено в {y} году?",
             "uz": f"{y}-yilda {word} bo‘yicha nechta bitim tuzilgan?",
             "uz-cyrl": f"{y} йилда {word} бўйича нечта битим тузилган?"}[lang]
        add(lang, "aggregate", q, N(len(s), 0), [int((~s["canc"]).sum())], "всего / без аннулированных",
            f"prod=={prod!r}&year=={y}")

    # ---------- объём в тоннах по региону
    for lang, prod, word, reg_q, reg_uz, y in [
        ("ru", "Портландцемент", "цемента", "Самаркандской области", "Samarqand viloyati", 2025),
        ("uz", "Карбамид", "karbamid", "Toshkent viloyatida", "Toshkent viloyati", 2024),
        ("ru", "Пшеница", "пшеницы", "Кашкадарьинской области", "Qashqadaryo viloyati", 2023)]:
        s = d[(d["prod"] == prod) & (d["year"] == y) & (d["Регион"] == reg_uz) & (d["Единица измерения"] == "тонна")]
        q = {"ru": f"Сколько тонн {word} продано в {reg_q} в {y} году?",
             "uz": f"{y}-yilda {reg_q} necha tonna {word} sotilgan?"}[lang]
        add(lang, "aggregate", q, N(s["vol"].sum()), [float(s.loc[~s["canc"], "vol"].sum())], "тонны всего / без аннулированных")

    # ---------- сумма в сумах
    for lang, prod, word, y in [("uz", "Хлопок-сырец", "paxta xom ashyosi", 2024), ("ru", "Сахар белый", "сахару", 2025),
                                ("uz-cyrl", "Карбамид", "карбамид", 2022)]:
        s = d[(d["prod"] == prod) & (d["year"] == y)]
        q = {"uz": f"{y}-yilda {word} bo‘yicha bitimlar summasi qancha?",
             "ru": f"На какую сумму заключено сделок по {word} в {y} году?",
             "uz-cyrl": f"{y} йилда {word} бўйича битимлар суммаси қанча?"}[lang]
        add(lang, "aggregate", q, N(s["uzs"].sum()), [float(s.loc[~s["canc"], "uzs"].sum())], "сум всего / без аннулированных")

    # ---------- топ-N
    for lang, prod, word, y in [("ru", "Карбамид", "карбамида", 2023), ("uz", "Портландцемент", "sement", 2025)]:
        s = d[(d["prod"] == prod) & (d["year"] == y)].groupby("ИНН покупателя")["uzs"].sum().sort_values(ascending=False)
        q = {"ru": f"Кто крупнейший покупатель {word} в {y} году по сумме сделок?",
             "uz": f"{y}-yilda {word} bo‘yicha eng yirik xaridor kim (summa bo‘yicha)?"}[lang]
        add(lang, "top", q, T(s.index[0]), [], "ИНН топ-1")
    s = d[d["year"] == 2024].groupby("Брокер покупателя").size().sort_values(ascending=False)
    add("ru", "broker", "Какой брокер провёл больше всего сделок на стороне покупателя в 2024 году?", T(s.index[0]))
    s = d[d["year"] == 2025].groupby("Регион").size().sort_values(ascending=False)
    add("uz-cyrl", "top", "2025 йилда энг кўп битим тузилган вилоят қайси?", T(s.index[0]))
    s = d[(d["year"] == 2026)].groupby("ИНН продавца")["uzs"].sum().sort_values(ascending=False)
    add("uz", "top", "2026-yilda eng ko‘p sotgan sotuvchi kim (summa bo‘yicha)?", T(s.index[0]))

    # ---------- цены
    s = d[(d["prod"] == "Пшеница") & (d["year"] == 2025) & (d["month"] == 3) & (d["Единица измерения"] == "тонна") & (d["cur"] == "Узбекский сум")]
    add("ru", "price", "Средняя цена за тонну пшеницы в марте 2025 года?", N(s["price"].mean(), 0.01),
        [float(s["Сумма сделки"].sum() / s["vol"].sum())], "среднее / средневзвешенная")
    s = d[(d["prod"] == "Бензин автомобильный") & (d["year"] == 2026) & d["Наименование товара"].str.contains("АИ-92", case=False)
          & (d["Единица измерения"] == "литр")]
    add("ru", "price", "Максимальная цена бензина АИ-92 за литр в 2026 году?", N(s["price"].max(), 0.005))
    s = d[(d["prod"] == "Хлопок-сырец") & (d["year"] == 2025) & (d["Единица измерения"] == "килограмм")]
    add("uz", "price", "2025-yilda paxta xom ashyosining 1 kg uchun o‘rtacha narxi qancha?", N(s["price"].mean(), 0.01),
        [float(s["Сумма сделки"].sum() / s["vol"].sum())])
    s = d[(d["prod"] == "Сахар белый") & (d["year"] == 2024) & (d["Единица измерения"] == "тонна")]
    add("uz-cyrl", "price", "2024 йилда шакарнинг бир тонна учун ўртача нархи қанча?", N(s["price"].mean(), 0.01),
        [float(s["Сумма сделки"].sum() / s["vol"].sum())])

    # ---------- экспорт и валюта
    s = d[(d["Тип контракта"] == "Экспортный") & (d["year"] == 2024)]
    add("ru", "export", "Сумма экспортных сделок в долларах за 2024 год?", N(s["usd"].sum()), [float(s.loc[~s["canc"], "usd"].sum())])
    add("uz", "export", "2025-yilda nechta eksport bitimi bo‘lgan?", N(int(((d["Тип контракта"] == "Экспортный") & (d["year"] == 2025)).sum()), 0),
        [int(((d["Тип контракта"] == "Экспортный") & (d["year"] == 2025) & ~d["canc"]).sum())])
    add("ru", "export", "Курс доллара на 15.03.2024?", N(r.loc[r["date"] == "2024-03-15", "rate"].iloc[0], 0.0005))
    add("uz", "export", "2023-yil 1-iyuldagi dollar kursi qancha?", N(r.loc[r["date"] == "2023-07-01", "rate"].iloc[0], 0.0005))

    # ---------- статусы
    s = d[d["year"] == 2025]
    add("ru", "status", "Какая доля сделок аннулирована в 2025 году?", N(100 * s["canc"].mean(), 0.01), [float(s["canc"].mean())], "в %")
    add("ru", "status", "Сколько сделок по хлопку-сырцу исполнено частично?",
        N(int(((d["prod"] == "Хлопок-сырец") & (d["Состояние сделки"] == "Исполнен(Частично)")).sum()), 0))
    add("uz", "status", "2024-yilda nechta bitim bekor qilingan?", N(int((d["canc"] & (d["year"] == 2024)).sum()), 0))
    add("uz-cyrl", "status", "Ҳозир нечта битим тўловни кутмоқда?", N(int((d["Состояние сделки"] == "Ждёт оплату").sum()), 0))

    # ---------- карточка контракта
    kk = k.set_index("Номер контракта")
    for lang, cid in [("ru", "101429"), ("uz", "101429"), ("ru", "102557")]:
        q = {"ru": f"Какой срок оплаты по контракту {cid}?", "uz": f"{cid}-shartnoma bo‘yicha to‘lov muddati necha kun?"}[lang]
        add(lang, "contract", q, N(int(kk.loc[cid, "Срок оплаты"]), 0))
    add("ru", "contract", "Какой базис поставки по контракту 100002?", T(kk.loc["100002", "Базис поставки"]))
    add("uz", "contract", "100002-shartnomaning bazis narxi qancha?", N(num(pd.Series([kk.loc["100002", "Базисная цена"]]))[0], 0.001))

    # ---------- компании
    pp = p.set_index("ИНН")
    add("ru", "company", "В каком регионе компания с ИНН 996831451?", T(pp.loc["996831451", "Регион"]))
    add("uz", "company", "996831451 STIRli kompaniyaning faoliyat turi qanday?", T(pp.loc["996831451", "Вид деятельности"]))
    s = d[(d["ИНН покупателя"] == "996831451") | (d["ИНН продавца"] == "996831451")]
    add("ru", "company", "Сколько сделок у компании XUSIT POLIMER?", N(len(s), 0), [int((~s["canc"]).sum())])
    add("ru", "company", "Компания XUSIT POLIMER — плательщик НДС?", T({"Да": "да", "Нет": "не"}[pp.loc["996831451", "Плательщик НДС"]]))

    # ---------- брокеры
    bb = b.set_index("Код брокера")
    add("uz", "broker", "BK045 brokeri qaysi viloyatdan?", T(bb.loc["BK045", "Регион"]))
    add("ru", "broker", "Как называется брокер BK007?", T(bb.loc["BK007", "Наименование"].strip('"').split('"')[0] if '"' in bb.loc["BK007", "Наименование"] else bb.loc["BK007", "Наименование"].split(" MCHJ")[0]))
    add("uz-cyrl", "broker", "BK012 брокер 2025 йилда сотувчи томонида нечта битим қилган?",
        N(int(((d["Брокер продавца"] == "BK012") & (d["year"] == 2025)).sum()), 0))

    # ---------- сделка по номеру
    dd = d.set_index("Номер сделки")
    add("ru", "deal", "Кто покупатель по сделке 280092?", T(dd.loc["280092", "ИНН покупателя"]))
    add("ru", "deal", "Какой статус у сделки 280092?", T("Аннулирован"))
    add("uz", "deal", "387831 raqamli bitim summasi qancha?", N(dd.loc["387831", "Сумма сделки"], 0.001))

    # ---------- ловушки
    for lang, q in [
        ("ru", "Сколько сделок было в 2021 году?"), ("ru", "Сколько золота продано на бирже?"),
        ("ru", "Какая прибыль у компании с ИНН 996831451?"), ("ru", "Кто директор компании XUSIT POLIMER?"),
        ("ru", "Прогноз цены цемента на 2027 год"), ("ru", "Сколько сделок было 15.11.2026?"),
        ("ru", "В каком регионе компания с ИНН 123456789?"), ("uz", "2021-yilda nechta bitim bo‘lgan?"),
        ("uz", "Oltin qancha sotilgan?"), ("uz-cyrl", "Нефть қанча сотилган?"), ("ru", "Сколько стоит iPhone на бирже?"),
        ("ru", "Кто покупатель по сделке 100000?"), ("uz", "BK150 brokeri qaysi viloyatdan?"),
        ("ru", "Какой телефон у компании XUSIT POLIMER?"), ("ru", "Сколько сделок по хлопку было в ноябре 2026 года?"),
        ("ru", "Почему в 2024 году выросло число аннулированных сделок?"),
    ]:
        add(lang, "trap", q, ND)

    # альтернативные написания текстовых ответов (регион по-русски и т.п.)
    reg_ru = {"Toshkent shahri": ["г. Ташкент", "Тошкент шаҳри", "Ташкент"], "Buxoro viloyati": ["Бухарская", "Бухоро"],
              "Surxondaryo viloyati": ["Сурхандарьинская", "Сурхондарё"]}
    for x in Q:
        if x["expected"].get("type") == "text" and x["expected"]["value"] in reg_ru:
            x["alt_values"] = reg_ru[x["expected"]["value"]]
    with open(OUT, "w", encoding="utf-8") as f:
        for x in Q:
            f.write(json.dumps(x, ensure_ascii=False, default=float) + "\n")
    print(f"{len(Q)} вопросов -> {OUT}")


if __name__ == "__main__":
    main()
