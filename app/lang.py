"""Определение языка вопроса: ru | uz | uz-cyrl."""
from __future__ import annotations

import re

_UZ_CYRL_LETTERS = re.compile(r"[ўқғҳЎҚҒҲ]")
_UZ_CYRL_WORDS = re.compile(
    r"\b(қанча|неча|нечта|йил\w*|қайси|битим\w*|шартнома\w*|вилоят\w*|бўйича|ким|қачон|нима|ва|учун|"
    r"ассалому|алайкум|салом|раҳмат|рахмат|гап|нима|яхши\w*|сотилган|сотиб|олинган|миқдори|суммаси|нархи|тонна|кўп|энг|ҳажми|бўлган|канча|кайси|буйича|компания\w*си)\b",
    re.I,
)
_UZ_LAT = re.compile(
    r"(o[‘’'ʻ`]|g[‘’'ʻ`])|\b(qancha|nechta|necha|qaysi|yil\w*|bitim\w*|shartnoma\w*|viloyat\w*|bo[‘’'ʻ`]?yicha|"
    r"kim|qachon|nima|uchun|sotilgan|sotib|eng|ko[‘’'ʻ`]p|hajmi|summasi|narxi|kompaniya\w*|ma[‘’'ʻ`]lumot|"
    r"salom|assalomu|alaykum|rahmat|brokeri?|xaridor\w*|sotuvchi\w*|holat\w*|bekor|to[‘’'ʻ`]lov|muddati|o[‘’'ʻ`]rtacha|jami|umumiy|"
    r"paxta|sement|bug[‘’'ʻ`]?doy|tonna|qilingan|bo[‘’'ʻ`]lgan|mavjud|haqida|qaysi|ichida|bilan|dan|da)\b"
    r"|\d{4}-yil",
    re.I,
)
_UZ_WEAK = {"ва", "ким", "нима", "тонна", "гап", "салом"}
# разговорные узбекские окончания: -сан, -сиз, -ман, -япти, -воссан, -ди(м)…
_UZ_CYRL_SUFFIX = re.compile(r"\b\w+(?:ссан|япсан|япсиз|япман|япти|япмиз|вотти|воссан|опти|гансан|мисиз|мисан|дингиз|ингизми|сизми|санми)\b")
# русские служебные слова и основы предметной области; ы/щ в узбекской кириллице почти не встречаются
_RU_WORDS = re.compile(
    r"\b(в|во|на|по|за|с|со|и|или|не|нет|да|что|как|кто|где|когда|почему|сколько|какой|какая|какое|какие|каков\w*|ли|"
    r"был\w*|есть|это|мне|ты|вы|привет|здравствуй\w*|пожалуйста|спасибо|дела|году|года|годы|сдел\w*|покупател\w*|"
    r"продав\w*|продан\w*|куп\w*|аннулир\w*|договор\w*|регион\w*|област\w*|средн\w*|цен\w*|сумм\w*|доля|топ)\b")
_RU_LETTERS = re.compile(r"[ыщ]")
_CYRL = re.compile(r"[а-яёА-ЯЁ]")
_LAT = re.compile(r"[a-zA-Z]")


def detect(text: str) -> str:
    t = text or ""
    if _UZ_CYRL_LETTERS.search(t):
        return "uz-cyrl"
    n_cyr = len(_CYRL.findall(t))
    n_lat = len(_LAT.findall(t))
    if n_cyr >= n_lat and n_cyr > 0:
        # кириллица без ўқғҳ: сравниваем узбекские и русские признаки
        tl = t.lower()
        words = _UZ_CYRL_WORDS.findall(tl)
        weak = sum(1 for w in words if w in _UZ_WEAK)
        uz = 2 * (len(words) - weak) + weak + len(_UZ_CYRL_SUFFIX.findall(tl))
        ru = len(_RU_WORDS.findall(tl)) + 2 * bool(_RU_LETTERS.search(tl))
        return "uz-cyrl" if uz > ru else "ru"
    if n_lat > 0:
        # латиница без кириллицы — это узбекский (русский латиницей не пишут; языки бота: ru | uz | uz-cyrl).
        # «salom», «assalomu alaykum», «XUSIT POLIMER» -> uz
        return "uz"
    return "ru"
