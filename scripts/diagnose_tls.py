# -*- coding: utf-8 -*-
r"""HTTPS 憑證診斷:一次分辨「公司做 TLS 攔截」還是「Windows 根憑證沒到位」。

用法(在出問題的那台電腦上,把整段輸出貼回來即可):

    uv run python scripts/diagnose_tls.py

存在的理由是**那兩種病因的處方相反**(見 docs/dev/runtime.md):根憑證沒補到
要疊 certifi,TLS 攔截要改用作業系統原生驗證,而兩者的錯誤訊息一模一樣
(`unable to get local issuer certificate`)。分辨的關鍵是**對方憑證的簽發者**
——是公開 CA 就是前者,是公司的名字就是後者。

純唯讀:不下載、不改任何設定,也不動 %LOCALAPPDATA% 底下的模型快取。
"""
import os
import socket
import ssl
import sys
import tempfile

HOSTS = ["github.com", "objects.githubusercontent.com", "huggingface.co"]


def peer_issuer(host: str) -> str:
    """關掉驗證只為了「看一眼對方是誰簽的」——這是分辨兩種病因的唯一線索。

    ⚠️ 驗證關掉時 `getpeercert()` 回空 dict(Python 的設計),所以只能自己
    取 DER 再解碼;3.13 才有 `get_unverified_chain()`,這裡是 3.12。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, 443), timeout=15) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                der = ss.getpeercert(binary_form=True)
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write(ssl.DER_cert_to_PEM_cert(der))
            path = f.name
        try:
            info = ssl._ssl._test_decode_cert(path)  # noqa: SLF001 - 診斷腳本專用
        finally:
            os.unlink(path)
        issuer = ", ".join(v for rdn in info.get("issuer", ()) for _, v in rdn)
        subject = ", ".join(v for rdn in info.get("subject", ()) for _, v in rdn)
        return f"簽發者={issuer} | 網站={subject}"
    except Exception as e:
        return f"取不到({type(e).__name__}: {e})"


def _stacked_context(cafile: str) -> ssl.SSLContext:
    """Windows 存放區 + certifi 的疊加,也就是工具第一層之後的實際狀態。

    ⚠️ `SSL_CERT_FILE` 要在建 context **之前**設好:`load_default_certs()`
    是先載 Windows 存放區、再 `set_default_verify_paths()` 去吃這個變數,
    事後才設對已經建好的 context 毫無作用。"""
    old = os.environ.get("SSL_CERT_FILE")
    os.environ["SSL_CERT_FILE"] = cafile
    try:
        return ssl.create_default_context()
    finally:
        if old is None:
            del os.environ["SSL_CERT_FILE"]
        else:
            os.environ["SSL_CERT_FILE"] = old


def probe(host: str, ctx: ssl.SSLContext, label: str) -> bool:
    try:
        with socket.create_connection((host, 443), timeout=15) as s:
            with ctx.wrap_socket(s, server_hostname=host):
                pass
        print(f"    [通過] {label}")
        return True
    except Exception as e:
        print(f"    [失敗] {label} -> {type(e).__name__}: {e}")
        return False


def main() -> int:
    print("=" * 70)
    print("Python:", sys.version)
    print("執行檔:", sys.executable)
    proxies = {k: v for k, v in os.environ.items() if k.lower().endswith("_proxy")}
    print("代理環境變數:", proxies or "(無)")
    try:
        from urllib.request import getproxies

        print("urllib 看到的代理:", getproxies() or "(無)")
    except Exception as e:  # pragma: no cover - 診斷腳本的防呆
        print("urllib 代理查詢失敗:", e)
    print("SSL_CERT_FILE:", os.environ.get("SSL_CERT_FILE") or "(未設)")
    try:
        # 數字很小(開發機 58 張)就是「用到才補」那種:certifi 有 150+
        print("Windows ROOT 存放區裡 Python 看得到的憑證數:",
              len(ssl.enum_certificates("ROOT")))
    except Exception as e:
        print("enum_certificates 失敗:", e)

    try:
        import certifi

        print("certifi:", certifi.where())
    except Exception as e:
        certifi = None
        print("certifi 不可用:", e)
    try:
        import truststore
    except Exception as e:
        truststore = None
        print("truststore 不可用:", e)

    for host in HOSTS:
        print("-" * 70)
        print(f"■ {host}")
        print("   ", peer_issuer(host))
        probe(host, ssl.create_default_context(),
              "只有 Windows 憑證存放區 ← v0.8.5 以前的工具走這條")
        if certifi is not None:
            probe(host, ssl.create_default_context(cafile=certifi.where()),
                  "只有 certifi 憑證包")
            probe(host, _stacked_context(certifi.where()),
                  "Windows 存放區 + certifi ← 現在的工具走這條(第一層)")
        if truststore is not None:
            probe(host, truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
                  "作業系統原生驗證 ← 撞到憑證錯誤時工具會改走這條(第二層)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
