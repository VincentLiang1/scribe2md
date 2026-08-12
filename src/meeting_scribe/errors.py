class UserFacingError(RuntimeError):
    """自家模組精心撰寫的繁中錯誤訊息,app 層可原样顯示給使用者。

    以型別區分訊息來源:第三方函式庫(sherpa-onnx / ctranslate2 / HF 等)
    的原生例外同樣可能是 RuntimeError,但內容是 cryptic 英文,不得直接
    面對使用者——那些必須包裝成本型別、或走「未預期的錯誤」通用文案。
    """
