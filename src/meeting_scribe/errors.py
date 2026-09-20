class UserFacingError(RuntimeError):
    """自家模組精心撰寫的繁中錯誤訊息,app 層可原樣顯示給使用者。

    以型別區分訊息來源:第三方函式庫(sherpa-onnx / ctranslate2 / HF 等)
    的原生例外同樣可能是 RuntimeError,但內容是 cryptic 英文,不得直接
    面對使用者——那些必須包裝成本型別、或走「未預期的錯誤」通用文案。
    """


# ---------------------------------------------------------------------------
# 原生元件載入失敗(環境問題,不是程式問題)


def missing_dll(e: BaseException) -> bool:
    """這個例外是不是「依賴的 DLL 找不到」。

    兩個來源都要認:`import` 進去時 CPython 把 OSError 轉成的
    `ImportError: DLL load failed while importing X: …`(**沒有** winerror
    屬性,只剩訊息),以及 `ctypes.WinDLL()` 自己丟的 `OSError` WinError 126。
    ⚠️ **不比對「找不到指定的模組」那句話**:它是 Windows 依顯示語言本地化的,
    英文版機器上是 `The specified module could not be found.`。"""
    return "DLL load failed" in str(e) or getattr(e, "winerror", None) == 126


def native_engine_error(e: BaseException) -> UserFacingError:
    r"""把「AI 元件的原生模組載不起來」翻成繁中。

    ⚠️ **這是環境問題不是程式問題**,而它的原文是全專案最 cryptic 的一種:
    `ImportError: DLL load failed while importing onnxruntime_pybind11_state:
    找不到指定的模組。`——2026-09-20 在一台乾淨的虛擬機上踩到,使用者看到的
    是「收尾時出錯」外加記錄檔裡一整段堆疊,沒有任何人看得出來要去裝什麼。

    ⚠️ **兩種病因、處方不同,所以分開講**:「找不到指定的模組」是**依賴的 DLL
    不在**(絕大多數是 Microsoft Visual C++ 執行階段——虛擬機、剛重灌、公司的
    精簡映像都常常沒有);其餘的 ImportError 則是**套件本身沒裝好**(安裝中斷、
    防毒把檔案隔離),那要重跑安裝。把兩種寫成同一句,照著做的人有一半會白做。
    """
    if missing_dll(e):
        return UserFacingError(
            "AI 元件無法在這台電腦上載入:缺少它需要的系統元件。請安裝 "
            "Microsoft Visual C++ 2015-2022 可轉散發套件(x64)後重新啟動程式"
            "——虛擬機器、剛重灌或精簡安裝的 Windows 常常沒有預裝;"
            "裝了仍然失敗請重新執行「安裝.bat」"
        )
    return UserFacingError(
        "AI 元件沒有安裝完整,無法載入:請重新執行「安裝.bat」"
        "(安裝過程中斷、或檔案被防毒軟體隔離都會這樣)"
    )
