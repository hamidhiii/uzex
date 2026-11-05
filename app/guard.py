"""Предпроверки без LLM: даты вне диапазона, темы вне реестра, несуществующие ИНН / сделки / контракты."""
from __future__ import annotations

import datetime as dt
import re

from . import config, db
from .resolve import idx

RANGE = {
    "ru": f"В реестре есть сделки только с {config.DATA_START_STR} по {config.DATA_END_STR}.",
    "uz": f"Reyestrda faqat {config.DATA_START_STR} dan {config.DATA_END_STR} gacha bo‘lgan bitimlar bor.",
    "uz-cyrl": f"Реестрда фақат {config.DATA_START_STR} дан {config.DATA_END_STR} гача бўлган битимлар бор.",
}
SCOPE = {
    "ru": "В реестре есть только сделки, карточки контрактов, участники (ИНН, название, регион, вид деятельности, НДС), "
          "брокеры и курс доллара.",
    "uz": "Reyestrda faqat bitimlar, shartnoma kartochkalari, ishtirokchilar (STIR, nomi, viloyati, faoliyat turi, QQS), "
          "brokerlar va dollar kursi bor.",
    "uz-cyrl": "Реестрда фақат битимлар, шартнома карточкалари, иштирокчилар (СТИР, номи, вилояти, фаолият тури, ҚҚС), "
               "брокерлар ва доллар курси бор.",
}
T = {
    "period": {
        "ru": "**Данных за этот период нет.** {range}",
        "uz": "**Bu davr uchun ma’lumot yo‘q.** {range}",
        "uz-cyrl": "**Бу давр учун маълумот йўқ.** {range}",
    },
    "topic": {
        "ru": "**В данных биржи этого нет** ({what}). {scope}",
        "uz": "**Birja ma’lumotlarida bu yo‘q** ({what}). {scope}",
        "uz-cyrl": "**Биржа маълумотларида бу йўқ** ({what}). {scope}",
    },
    "inn": {
        "ru": "**Компании с ИНН {inn} в реестре нет.** Проверил справочники участников и брокеров.",
        "uz": "**STIR {inn} bo‘lgan kompaniya reyestrda yo‘q.** Ishtirokchilar va brokerlar ro‘yxati tekshirildi.",
        "uz-cyrl": "**СТИР {inn} бўлган компания реестрда йўқ.** Иштирокчилар ва брокерлар рўйхати текширилди.",
    },
    "deal": {
        "ru": "**Сделки с номером {n} в реестре нет.**",
        "uz": "**{n} raqamli bitim reyestrda yo‘q.**",
        "uz-cyrl": "**{n} рақамли битим реестрда йўқ.**",
    },
    "contract": {
        "ru": "**Контракта с номером {n} в реестре нет.**",
        "uz": "**{n} raqamli shartnoma reyestrda yo‘q.**",
        "uz-cyrl": "**{n} рақамли шартнома реестрда йўқ.**",
    },
    "offtopic": {
        "ru": "**Этот вопрос не относится к реестру сделок биржи.** Я отвечаю только по данным реестра UZEX "
              "(03.01.2022–30.10.2026): сделки, товары, объёмы, суммы и цены, контракты, компании-участники, брокеры, "
              "регионы и курс доллара.\nНапример: «Сколько тонн цемента продано в 2025 году?», «Кто крупнейший покупатель карбамида в 2023?», "
              "«BK045 brokeri qaysi viloyatdan?»",
        "uz": "**Bu savol birja bitimlari reyestriga tegishli emas.** Men faqat UZEX reyestri ma’lumotlari bo‘yicha javob beraman "
              "(03.01.2022–30.10.2026): bitimlar, tovarlar, hajm, summa va narxlar, shartnomalar, ishtirokchi kompaniyalar, brokerlar, "
              "viloyatlar va dollar kursi.\nMasalan: «2025-yilda necha tonna sement sotilgan?», «2023-yilda karbamidning eng yirik xaridori kim?», "
              "«BK045 brokeri qaysi viloyatdan?»",
        "uz-cyrl": "**Бу савол биржа битимлари реестрига тегишли эмас.** Мен фақат UZEX реестри маълумотлари бўйича жавоб бераман "
                   "(03.01.2022–30.10.2026): битимлар, товарлар, ҳажм, сумма ва нархлар, шартномалар, иштирокчи компаниялар, брокерлар, "
                   "вилоятлар ва доллар курси.\nМасалан: «2025 йилда неча тонна цемент сотилган?», «BK045 брокер қайси вилоятдан?»",
    },
    "broker": {
        "ru": "**Брокера {code} в реестре нет.** Коды брокеров: BK001–BK120.",
        "uz": "**{code} broker reyestrda yo‘q.** Broker kodlari: BK001–BK120.",
        "uz-cyrl": "**{code} брокер реестрда йўқ.** Брокер кодлари: BK001–BK120.",
    },
}

# тема -> (регулярка, описание ru/uz/uz-cyrl)
TOPICS = [
    (r"прибыл|рентабельн|маржинальн|foyda|фойда|рентабел|sof daromad",
     {"ru": "прибыль", "uz": "foyda", "uz-cyrl": "фойда"}),
    (r"себестоим|tannarx|таннарх",
     {"ru": "себестоимость", "uz": "tannarx", "uz-cyrl": "таннарх"}),
    (r"директор|руководител|учредител|владел|генеральн\w* директор|rahbar|direktor|ta[‘’'ʻ]?sischi|egasi|раҳбар|директори|муассис",
     {"ru": "руководители и владельцы", "uz": "rahbarlar va egalar", "uz-cyrl": "раҳбарлар ва эгалар"}),
    (r"телефон|telefon|e-?mail|электронн\w* почт|сайт компании|veb-?sayt|контакт|kontakt",
     {"ru": "контакты", "uz": "kontaktlar", "uz-cyrl": "контактлар"}),
    (r"прогноз|спрогнозир|предскаж|будет стоить|prognoz|bashorat|башорат|прогнози",
     {"ru": "прогнозы", "uz": "prognozlar", "uz-cyrl": "прогнозлар"}),
    (r"лондонск|\blme\b|чикагск|\bcme\b|nyse|nasdaq|московск\w* бирж|тошкент фонд|ташкентск\w* фондов|фондов\w* бирж|"
     r"fond birja|криптобирж|binance|bitcoin|биткоин",
     {"ru": "другие биржи", "uz": "boshqa birjalar", "uz-cyrl": "бошқа биржалар"}),
    (r"зарплат|сотрудник|численност\w* персонал|xodimlar soni|ish haqi|иш ҳақи|ходимлар",
     {"ru": "сотрудники и зарплаты", "uz": "xodimlar va ish haqi", "uz-cyrl": "ходимлар ва иш ҳақи"}),
]

MONTHS = {
    1: r"январ|yanvar|январь", 2: r"феврал|fevral", 3: r"март|mart", 4: r"апрел|aprel", 5: r"\bма[йяе]\b|may",
    6: r"июн|iyun|июнь", 7: r"июл|iyul", 8: r"август|avgust", 9: r"сентябр|sentabr|sentyabr",
    10: r"октябр|oktabr|oktyabr", 11: r"ноябр|noyabr", 12: r"декабр|dekabr",
}

_YEAR = re.compile(r"(?<![\d.,\s]\d)(?<![\d.,])((?:19|20)\d{2})(?![\d.,]\d)(?!\d)")
_YEAR_AFTER = re.compile(r"^\s*(?:-|–)?\s*(?:год|г\.|гг|г\b|yil|йил|y\.|year)", re.I)
_YEAR_BEFORE = re.compile(r"(?:\b(?:в|во|за|с|со|по|до|от|на|между|и|in|year|yil|йил)\s+|[(\-–])$", re.I)
_NOT_YEAR_AFTER = re.compile(r"^\s*(?:тонн|т\b|кг|килограмм|литр|л\b|штук|шт|сум|so[‘’'ʻ]?m|сўм|usd|\$|долл|dollar|лот|%|м|куб|"
                             r"сдел|bitim|битим|контракт|shartnoma|раз|marta|тыс|ming|млн|mln|млрд|mlrd|ед)", re.I)
_DATE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)")
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


# основы слов предметной области (RU — по исходному тексту, UZ — по транслитерации в латиницу)
_DOMAIN_RU = ("сделк", "сделок", "торг", "бирж", "товар", "продаж", "продан", "прода", "куп", "покуп", "сумм", "объем", "объём",
              "тонн", "килограм", "литр", "кубомет", "куб", "штук", "цен", "стоим", "курс", "доллар", "валют", "экспорт", "импорт",
              "контракт", "договор", "лот", "аннулир", "отмен", "исполн", "оплат", "постав", "статус", "брокер", "компан", "фирм",
              "организац", "предприят", "инн", "ндс", "регион", "област", "город", "участник", "покупател", "продавц", "продавец",
              "аукцион", "спот", "форвард", "фьючерс", "залог", "базис", "склад", "марк", "сорт", "класс", "реестр", "тнвэд", "тн вэд")
_DOMAIN_UZ = ("bitim", "savdo", "birja", "tovar", "mahsulot", "sotil", "sotib", "sotgan", "sotuv", "xarid", "summa", "hajm",
              "tonna", "kilogramm", "litr", "narx", "kurs", "dollar", "valyuta", "som", "eksport", "import", "shartnoma", "kontrakt",
              "lot", "bekor", "ijro", "tolov", "tolan", "yetkaz", "holat", "broker", "kompaniya", "korxona", "firma", "stir", "qqs",
              "viloyat", "hudud", "shahar", "ishtirokchi", "auksion", "spot", "forvard", "fyuchers", "garov", "ombor", "marka",
              "nav", "sinf", "reyestr", "usd", "uzs")
_GREETING = re.compile(r"^\s*(привет|здравствуй\w*|салам|salom|assalomu alaykum|ассалому алайкум|hello|hi|hey|добрый (день|вечер)|"
                       r"кто ты|ты кто|sen kimsan|siz kimsiz|сен кимсан|что ты умеешь|nima qila olasan)\b", re.I)


_OFFTOPIC = re.compile(r"погод|прогноз погоды|ob-?havo|обҳаво|анекдот|latifa|рецепт|retsept|приготов|стих|she[‘’'ʻ]?r\b|"
                       r"президент|prezident|футбол|futbol|фильм|kino|песн|qo[‘’'ʻ]?shiq|переведи|tarjima qil|напиши код|"
                       r"\d+\s*[+*/]\s*\d+", re.I)


def _domain_hit(q: str) -> bool:
    from .normalize import norm_text, translit
    from .resolve import resolve_company, resolve_product, resolve_region
    t_ru = norm_text(q)
    t_uz = norm_text(translit(t_ru))
    words = set(t_ru.split()) | set(t_uz.split())
    if re.search(r"\d{5,}|(?i:\bbk\s*-?\d{1,3}\b|\bбк\s*-?\d{1,3}\b)", q):
        return True  # ИНН, номер сделки/контракта, код брокера
    for w in words:
        if any(w.startswith(st) for st in _DOMAIN_RU if len(st) >= 4) or w in {st for st in _DOMAIN_RU if len(st) < 4}:
            return True
        if any(w.startswith(st) for st in _DOMAIN_UZ if len(st) >= 4) or w in {st for st in _DOMAIN_UZ if len(st) < 4}:
            return True
    # товар / регион / компания по справочникам — по каждому слову и по всему тексту
    for w in words:
        if len(w) >= 3 and (resolve_product(w)["found"] or resolve_region(w)["found"]):
            return True
    if resolve_product(q)["found"] or resolve_region(q)["found"]:
        return True
    c = resolve_company(q)
    return bool(c["found"] and c["candidates"][0]["score"] >= 90)


def _no_data(lang: str, key: str, **kw) -> dict:
    kw.setdefault("range", RANGE[lang])
    kw.setdefault("scope", SCOPE[lang])
    return {"status": "no_data", "answer": T[key][lang].format(**kw), "reason": key}


def _years(q: str) -> list[int]:
    out = []
    for m in _YEAR.finditer(q):
        after, before = q[m.end():], q[:m.start()]
        if _YEAR_AFTER.match(after) or (_YEAR_BEFORE.search(before) and not _NOT_YEAR_AFTER.match(after)):
            out.append(int(m.group(1)))
    return out


def _dates(q: str) -> list[dt.date]:
    out = []
    for m in _DATE.finditer(q):
        try:
            out.append(dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
        except ValueError:
            pass
    for m in _ISO.finditer(q):
        try:
            out.append(dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    return out


_MONTH_ANY = "|".join(f"(?P<m{n}>{pat})" for n, pat in MONTHS.items())
_VDATE = re.compile(
    r"(?:(?P<yb>20\d{2})\s*(?:-|–)?\s*(?:yil\w*|йил\w*|год\w*|г\.)?\s*,?\s*)?"
    r"(?<!\d)(?P<d>\d{1,2})\s*(?:-|–)?\s*(?:го\s+)?(?:" + _MONTH_ANY + r")\w*"
    r"(?:\s*(?:oyi\w*\s*|ойи\w*\s*)?(?:,\s*)?(?P<ya>20\d{2})(?!\d))?", re.I)


def _vdates(q: str) -> list[tuple[dt.date, tuple[int, int]]]:
    """«15 ноября 2026», «2026-yil 15-noyabr», «15-noyabr 2026-yil» -> дата и её позиция."""
    out = []
    for m in _VDATE.finditer(q):
        y = m.group("ya") or m.group("yb")
        if not y:
            continue
        mnum = next(n for n in MONTHS if m.group(f"m{n}"))
        try:
            out.append((dt.date(int(y), mnum, int(m.group("d"))), m.span()))
        except ValueError:
            pass
    return out


def _month_year(q: str) -> list[tuple[int, int]]:
    """«в ноябре 2026», «2026-yil noyabr» -> (2026, 11)."""
    out = []
    ql = q.lower()
    for mnum, pat in MONTHS.items():
        for m in re.finditer(pat, ql):
            after = re.match(r"^\w*\s*(?:oyi\w*\s*|ойи\w*\s*)?(20\d{2})(?!\d)", ql[m.end(): m.end() + 25])
            before = re.search(r"(?<!\d)(20\d{2})\s*(?:-|–)?\s*(?:yil\w*|йил\w*|год\w*|г\.)?\s*$", ql[max(0, m.start() - 20): m.start()])
            y = after or before
            if y:
                out.append((int(y.group(1)), mnum))
    return out


def precheck(question: str, lang: str) -> dict | None:
    """Вернуть готовый no_data-ответ или None (пропустить к агенту)."""
    q = question or ""
    ql = q.lower()

    # 1. темы вне реестра
    for pat, what in TOPICS:
        if re.search(pat, ql):
            return _no_data(lang, "topic", what=what[lang])

    # 1б. вопрос вообще не о реестре (приветствие, посторонняя тема)
    if _GREETING.search(q) or _OFFTOPIC.search(ql) or not _domain_hit(q):
        return _no_data(lang, "offtopic")

    # 2. даты и периоды
    vd = _vdates(q)
    dates = _dates(q) + [d for d, _ in vd]
    date_spans = [m.span() for m in _DATE.finditer(q)] + [m.span() for m in _ISO.finditer(q)] + [sp for _, sp in vd]
    q_wo_dates = q
    for a, b in sorted(date_spans, reverse=True):
        q_wo_dates = q_wo_dates[:a] + " " + q_wo_dates[b:]
    years = _years(q_wo_dates)
    mys = _month_year(q_wo_dates)
    in_range = []
    for d in dates:
        in_range.append(config.DATA_START <= d <= config.DATA_END)
    my_years = {y for y, _ in mys}
    for y in years:
        if y in my_years:  # год уже учтён вместе с месяцем
            continue
        in_range.append(config.MIN_YEAR <= y <= config.MAX_YEAR)
    for y, m in mys:
        in_range.append((2022, 1) <= (y, m) <= (2026, 10))
    if in_range and not any(in_range):
        return _no_data(lang, "period")
    # конкретная дата вне диапазона — даже если рядом есть год из диапазона
    if dates and not any(config.DATA_START <= d <= config.DATA_END for d in dates) \
            and not re.search(r"курс|kurs|курси", ql):
        return _no_data(lang, "period")

    # 3. ИНН
    I = idx()
    for m in re.finditer(r"(?<!\d)(\d{9})(?!\d)", q):
        inn = m.group(1)
        if inn not in I["by_inn"] and inn not in I["broker_by_inn"]:
            return _no_data(lang, "inn", inn=inn)

    # 4. брокер по коду
    for m in re.finditer(r"(?i)\b(?:bk|бк)\s*-?\s*0*(\d{1,3})\b", q):
        code = f"BK{int(m.group(1)):03d}"
        if code not in I["broker_by_code"]:
            return _no_data(lang, "broker", code=code)

    # 5. номер сделки / контракта
    for m in re.finditer(r"(?i)(сделк\w*|битим\w*|bitim\w*|deal)\s*(?:№|n|#|nomer|номер|raqam\w*|рақам\w*)?\s*(\d{5,7})(?!\d)"
                         r"|(?<!\d)(\d{5,7})\s*(?:-|–)?\s*(?:raqamli|рақамли|sonli|сонли|-?son)?\s*(?:bitim|битим|сделк)", q):
        n = int(m.group(2) or m.group(3))
        if not db.scalar("SELECT 1 FROM deals WHERE deal_id = ?", [n]):
            return _no_data(lang, "deal", n=n)
    for m in re.finditer(r"(?i)(контракт\w*|shartnoma\w*|шартнома\w*|договор\w*)\s*(?:№|n|#|nomer|номер|raqam\w*)?\s*(\d{5,7})(?!\d)"
                         r"|(?<!\d)(\d{5,7})\s*(?:-|–)?\s*(?:raqamli|рақамли|sonli)?\s*(?:shartnoma|шартнома|контракт)", q):
        n = int(m.group(2) or m.group(3))
        if not db.scalar("SELECT 1 FROM contracts WHERE contract_id = ?", [n]):
            return _no_data(lang, "contract", n=n)
    return None
