r"""介面文字的多國語言:繁體中文(原文)、簡體中文、English。

**動到介面文案之前先讀 `docs/dev/i18n.md`**(決策、與 iOS 版對齊的做法、分期)。

用法是把**繁中原文**包一層:`t("開始錄音")`、`t("最多 {n} 人", n=30)`。⚠️ **原文就是鍵**
(同 iOS 版的 `L("…")`):不另外發明鍵名,繁中的畫面因此一個字都不會變,而找某句英文是
誰的翻譯,搜原文就找得到。譯文在 `locales/en.json`(人工維護)與 `locales/zh_CN.json`
(**產物**,`scripts/make_zh_cn.py` 從繁中自動轉,不要手改)。

⚠️ **語言在第一次 `t()` 時決定、之後整個行程不變**:介面語言改了要重開程式才生效
(使用者 2026-10-21 選的)。這讓模組層的常數(分頁名、模式說明)可以直接寫 `t(...)`;
即時切換的話,那些常數全得改成函式,而分頁是「用到才建」的,沒建的那幾頁也追不到。

⚠️ **這裡管的是「介面語言」,不是「會議語言」**:逐字稿 md 裡的字(講者標籤、〔〕標記、
段落標題)跟著會議語言走,英文介面的人也可能錄繁中會議——那一份字串表另外放,
不經過 `t()`。

⚠️ **記錄檔不翻**(使用者 2026-10-21):`logger` 的訊息照舊寫繁中,不要包 `t()`——
除錯時看到三種語言的記錄只會更難查。
"""
from __future__ import annotations

import json
import logging
import os
from functools import cache
from pathlib import Path

logger = logging.getLogger(__name__)

ZH_TW, ZH_CN, EN = "zh_TW", "zh_CN", "en"
LANGS = (ZH_TW, ZH_CN, EN)
SOURCE = ZH_TW

# 介面語言選單上的名字:**每一種都用它自己的語言寫**(看不懂目前語言的人才找得到自己的)。
NATIVE_NAMES = {ZH_TW: "繁體中文", ZH_CN: "简体中文", EN: "English"}

# 開發與測試用的覆寫(`tests/conftest.py` 釘成繁中;要看英文版就
# `MEETING_SCRIBE_UI_LANG=en uv run python -m meeting_scribe.desktop`)。⚠️ 不是給使用者的
# 設定——那一份落在 `settings.KEY_UI_LANGUAGE`。
ENV_OVERRIDE = "MEETING_SCRIBE_UI_LANG"

_LOCALES = Path(__file__).parent / "locales"
_current: str | None = None


def match(tags: list[str]) -> str:
    r"""從系統的語言偏好清單挑介面語言:**清單裡第一個我們有的勝出,一個都沒有就英文**。

    與 iOS 挑 App 語言的規則相同(使用者 2026-10-17 在 iOS 定的「其他語言顯示英文」,
    2026-10-21 桌面版照用)。⚠️ **要走完整份清單,不是只看第一個**:「日文、繁中」的人
    拿到的是繁中,不是英文。
    ⚠️ 中文要看**書寫系統**不是地區:`zh-Hant-*`、`zh-TW`、`zh-HK`、`zh-MO` 是繁中;
    `zh-Hans-*`、`zh-CN`、`zh-SG` 與光一個 `zh` 是簡中(macOS 的清單兩種寫法都會出現,
    這台實測是 `zh-TW`)。"""
    for tag in tags:
        parts = tag.replace("_", "-").split("-")
        head = parts[0].lower()
        rest = {p.lower() for p in parts[1:]}
        if head == "zh":
            if "hant" in rest or rest & {"tw", "hk", "mo"}:
                return ZH_TW
            return ZH_CN
        if head == "en":
            return EN
    return EN


def system_language() -> str:
    """作業系統的語言偏好對應到哪一種介面語言。"""
    from meeting_scribe import plat

    return match(plat.preferred_languages())


def chosen() -> str | None:
    """使用者在 🌐 選單上選過的語言;沒選過(或檔案裡是不認得的值)回 `None`。"""
    from meeting_scribe import settings

    lang = settings.get(settings.KEY_UI_LANGUAGE)
    return lang if lang in LANGS else None


def choose(lang: str) -> None:
    """記下介面語言(下次開程式生效)。⚠️ **不改這一次的 `current()`**,理由見模組 docstring。"""
    from meeting_scribe import settings

    if lang not in LANGS:
        raise ValueError(lang)
    settings.set(settings.KEY_UI_LANGUAGE, lang)


def current() -> str:
    """這個行程用哪一種介面語言(第一次問才決定,之後不變)。"""
    global _current
    if _current is None:
        env = os.environ.get(ENV_OVERRIDE)
        if env in LANGS:
            _current = env
        else:
            _current = chosen() or system_language()
        logger.info("介面語言:%s", _current)
    return _current


def force(lang: str | None) -> None:
    """測試與開發用:直接指定這個行程的介面語言(`None` = 下次再重新決定)。"""
    if lang is not None and lang not in LANGS:
        raise ValueError(lang)
    global _current
    _current = lang


@cache
def table(lang: str) -> dict[str, str]:
    """某一種語言的譯文表(繁中原文 → 譯文)。繁中沒有表(原文就是答案)。

    ⚠️ **讀不到就當成空表、退回原文**:譯文檔壞了的話,畫面退回繁中,也好過整支程式起不來
    ——但一定要留一行記錄,否則「英文版怎麼一半是中文」查不到原因。"""
    if lang == SOURCE:
        return {}
    try:
        return json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 見 docstring
        logger.warning("讀不到 %s 的譯文表,介面退回繁中", lang, exc_info=True)
        return {}


def translate(text: str, lang: str, /, **kw) -> str:
    """`t()` 的本體,語言由呼叫端指定(測試與產生器用)。"""
    out = table(lang).get(text, text)
    return out.format(**kw) if kw else out


def t(text: str, /, **kw) -> str:
    r"""把繁中原文換成目前介面語言的那一句;帶參數就順手 `format`。

    ⚠️ **第一個參數一定要是字串字面值**:`tests/test_i18n.py` 用 AST 掃全 repo 的 `t("…")`
    比對 `locales/en.json`,傳變數進來就逃過檢查,英文版那一句會安靜地變回中文。
    ⚠️ **沒有參數就不 `format`**:原文裡本來就可能有 `{` `}`(例如說明文字講 JSON)。"""
    return translate(text, current(), **kw)
