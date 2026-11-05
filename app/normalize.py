"""Нормализация: товары по ТН ВЭД, сорт, класс, name_key, регионы.

Используется и fallback-сборщиком базы, и поиском сущностей (resolve.py),
чтобы ключи вопроса и базы строились одной и той же функцией.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- товары
# hs_code -> (product, name_uz, synonyms)
PRODUCTS: list[tuple[str, str, str, str]] = [
    ("100199", "Пшеница", "bug‘doy", "пшеница продовольственная"),
    ("100630", "Рис шлифованный", "guruch", "рис"),
    ("110100", "Мука пшеничная", "un; bug‘doy uni", "мука"),
    ("151219", "Масло подсолнечное", "kungaboqar yog‘i", "подсолнечное масло"),
    ("151229", "Масло хлопковое", "paxta yog‘i", "хлопковое масло"),
    ("170199", "Сахар белый", "shakar", "сахар; сахар-песок"),
    ("220190", "Вода питьевая", "ichimlik suvi", "питьевая вода; вода"),
    ("230610", "Шрот хлопковый", "paxta shroti; kunjara", "шрот хлопковых семян; шрот"),
    ("230990", "Комбикорм", "omuxta yem", "кормосмесь"),
    ("250100", "Соль пищевая", "osh tuzi; tuz", "соль; соль поваренная"),
    ("252020", "Гипс строительный", "gips", "гипс"),
    ("252210", "Известь", "ohak", "известь негашеная; известь строительная"),
    ("252329", "Портландцемент", "sement; portlandsement", "цемент; цемент портландский"),
    ("271012", "Бензин автомобильный", "benzin", "бензин; АИ-80; АИ-92; АИ-95"),
    ("271019", "Дизельное топливо", "dizel yoqilg‘isi; dizel", "солярка; топливо дизельное; ДТ; дизель"),
    ("271019", "Мазут топочный", "mazut", "мазут"),
    ("271112", "Сжиженный газ", "suyultirilgan gaz", "пропан-бутан; СУГ; газ сжиженный"),
    ("271320", "Битум нефтяной", "bitum", "битум; БНД"),
    ("280421", "Аргон", "argon", "аргон газообразный"),
    ("280430", "Азот", "azot", ""),
    ("280700", "Серная кислота", "sulfat kislota", "кислота серная"),
    ("281511", "Сода каустическая", "kaustik soda", "каустик"),
    ("310210", "Карбамид", "karbamid; mochevina", "мочевина"),
    ("310230", "Аммиачная селитра", "ammiakli selitra", "селитра"),
    ("320810", "Эмаль алкидная", "emal; bo‘yoq", "эмаль; ПФ-115; краска"),
    ("390120", "Полиэтилен", "polietilen", "ПЭ; полиэтилен гранулированный"),
    ("390210", "Полипропилен", "polipropilen", "ПП"),
    ("390410", "Поливинилхлорид", "polivinilxlorid; PVX", "ПВХ"),
    ("391721", "Трубы полиэтиленовые", "polietilen quvurlar; quvur", "труба ПЭ; трубы ПЭ; труба"),
    ("440711", "Пиломатериалы", "yog‘och taxta; pilomaterial", "пиломатериал обрезной; доска"),
    ("520100", "Хлопок-сырец", "paxta xom ashyosi; xom paxta; paxta", "хлопок; хлопок сырец"),
    ("520512", "Пряжа хлопчатобумажная", "paxta ipi; ip-kalava", "пряжа; пряжа хлопковая"),
    ("690410", "Кирпич керамический", "g‘isht", "кирпич"),
    ("700529", "Стекло листовое", "shisha; oyna", "стекло; стекло флоат"),
    ("720853", "Лист стальной горячекатаный", "po‘lat list", "лист стальной; листовой прокат"),
    ("721420", "Арматура стальная", "armatura", "арматура"),
    ("721710", "Проволока арматурная", "armatura simi; sim", "проволока; Вр-1"),
    ("740311", "Катод медный", "mis katod", "медный катод; медь"),
    ("760421", "Профиль алюминиевый", "alyuminiy profil", "алюминиевый профиль"),
    ("848210", "Подшипник", "podshipnik", ""),
    ("854449", "Кабель силовой", "kuch kabeli; kabel", "кабель"),
]
HS_TO_PRODUCT = {hs: p for hs, p, _, _ in PRODUCTS if hs != "271019"}


def product_by_hs(hs_code: str, name: str) -> str | None:
    hs = (hs_code or "").strip()
    if hs == "271019":
        return "Мазут топочный" if "мазут" in (name or "").lower() else "Дизельное топливо"
    return HS_TO_PRODUCT.get(hs)


# ---------------------------------------------------------------- регионы
REGIONS: list[tuple[str, str, str]] = [
    ("Toshkent shahri", "г. Ташкент", "Ташкент; город Ташкент; Toshkent; Tashkent; Тошкент шаҳри; Toshkent sh"),
    ("Toshkent viloyati", "Ташкентская область", "Тошкент вилояти; Ташкентская обл"),
    ("Andijon viloyati", "Андижанская область", "Андижан; Andijon; Андижон"),
    ("Buxoro viloyati", "Бухарская область", "Бухара; Buxoro; Бухоро"),
    ("Farg‘ona viloyati", "Ферганская область", "Фергана; Fargona; Farg'ona; Фарғона"),
    ("Jizzax viloyati", "Джизакская область", "Джизак; Jizzax; Жиззах"),
    ("Namangan viloyati", "Наманганская область", "Наманган"),
    ("Navoiy viloyati", "Навоийская область", "Навои; Navoiy; Навоий"),
    ("Qashqadaryo viloyati", "Кашкадарьинская область", "Кашкадарья; Карши; Qarshi; Қашқадарё"),
    ("Qoraqalpog‘iston Respublikasi", "Республика Каракалпакстан", "Каракалпакстан; Нукус; Qoraqalpogiston; Nukus; Қорақалпоғистон"),
    ("Samarqand viloyati", "Самаркандская область", "Самарканд; Samarqand; Самарқанд"),
    ("Sirdaryo viloyati", "Сырдарьинская область", "Сырдарья; Гулистан; Sirdaryo; Guliston; Сирдарё"),
    ("Surxondaryo viloyati", "Сурхандарьинская область", "Сурхандарья; Термез; Surxondaryo; Termiz; Сурхондарё"),
    ("Xorazm viloyati", "Хорезмская область", "Хорезм; Ургенч; Xorazm; Urganch; Хоразм"),
]

# ---------------------------------------------------------------- сорт / класс
_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}
_WORD = {"первый": "1", "второй": "2", "третий": "3"}
_GRADE_TOP = re.compile(r"высш\w*\s+сорт|(?<!\w)в/с(?!\w)")
_GRADE = re.compile(r"(?<!\w)(iii|ii|i|[123]|первый|второй|третий)\s+сорт")
_CLASS = re.compile(r"(?<!\w)(iii|iv|ii|i|v|[1-5])\s+класс")


def _prep(s: str | None) -> str:
    if not isinstance(s, str):
        return ""
    return s.lower().replace("ё", "е").replace("-", " ")


def norm_grade(col: str | None, name: str | None = None) -> str | None:
    for src in (col, name):
        t = _prep(src)
        if not t:
            continue
        if _GRADE_TOP.search(t):
            return "высший"
        m = _GRADE.search(t)
        if m:
            g = m.group(1)
            return _ROMAN.get(g) or _WORD.get(g) or g
    return None


def norm_class(col: str | None, name: str | None = None) -> str | None:
    for src in (col, name):
        t = _prep(src)
        if not t:
            continue
        m = _CLASS.search(t)
        if m:
            g = m.group(1)
            return _ROMAN.get(g, g)
    return None


# ---------------------------------------------------------------- name_key
_TR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z",
    "и": "i", "й": "y", "к": "k", "қ": "q", "л": "l", "м": "m", "н": "n", "о": "o", "ў": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ҳ": "h", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "sh", "ъ": "", "ь": "", "ы": "i", "э": "e", "ю": "yu", "я": "ya",
}
_QUOTES = re.compile(r"[‘’'ʼ`\"«»ʻ“”]")
_LEGAL = re.compile(
    r"\b(masuliyati cheklangan jamiyati|mas uliyati cheklangan jamiyati|mchj|qk|xk|ok|aj|ooo|ao|mchzh)\b"
)


def translit(s: str) -> str:
    return "".join(_TR.get(ch, ch) for ch in s.lower())


def name_key(s: str | None) -> str:
    t = translit(s if isinstance(s, str) else "")
    t = _QUOTES.sub("", t)
    t = re.sub(r"[^\w\s]", " ", t)  # прочая пунктуация -> пробел
    t = _LEGAL.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def norm_text(s: str | None) -> str:
    """Нормализация свободного текста для поиска товаров/регионов."""
    t = (s or "").lower().replace("ё", "е")
    t = re.sub(r"[‘’'ʼ`ʻ]", "", t)
    t = re.sub(r"[-_/()\"«»,.]", " ", t)
    return re.sub(r"\s+", " ", t).strip()
