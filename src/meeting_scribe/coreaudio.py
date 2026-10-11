r"""macOS 的 Core Audio 橋接:把「系統聲音」做成一張普通的輸入卡。

**平台層的一部分**(只被 `plat_mac` 使用,別處不要直接 import 它)。設計與
選型理由見 `docs/spec/mac/03-現場收音-macOS.md` §3.3,驗收見 `07` 假設 2;
互動式的最小重現在 `scripts/probe_systemaudio.py`。

做法:`AudioHardwareCreateProcessTap` 建一個全系統的 tap,再用
`AudioHardwareCreateAggregateDevice` 把它包成一個**私有聚合裝置**。

⚠️ **選這條路的理由是它動不到錄音層**:包完之後,那個聚合裝置對 soundcard
而言就是**一張普通的輸入音效卡**——`_TrackRecorder`、`_WavWriter`、補零/修剪、
掉幀診斷、時間軸保險絲**一行都不用改**。而那一整套是掉過 4.6 分鐘音訊才調
出來的(`docs/dev/recording.md`),**能不動它就不要動它**。

⚠️ **`CATapDescription` 是 Objective-C 類別,不是 C 結構**:`AudioHardware
CreateProcessTap` 本身是 C 函式,但它的引數要走 ObjC runtime 才建得出來。
這裡用 ctypes 載 `libobjc` 直呼,**仍然不必裝 PyObjC**(規格 §3.3 那句
「Core Audio 是 C API」結論對、理由不精確,2026-09-25 訂正)。

⚠️ **`objc_msgSend` 在 arm64 上不可以當 variadic 用**:AAPCS64 對可變參數與
具名參數的傳遞方式不同,那是已知的當機來源。下面每一次呼叫都把簽章宣告成
**那一次的真實形狀**,不共用一份。

⚠️ **沒有權限時是完全靜默的**:tap 照樣建得起來、聚合裝置照樣出現在清單裡、
`OSStatus` 全是 0、錄出來的長度也完全正確——**就是一整段零**。所以取用之前
一定要先問 `has_permission()`,把「沒授權」與「授權了但沒聲音」分開:那是
兩種完全不同的處方(`03` §3.4)。
"""
import atexit
import ctypes
import ctypes.util
import logging
import os
import threading

logger = logging.getLogger(__name__)

#: 聚合裝置在使用者的音訊設定裡會叫這個名字。⚠️ **它是 private 的**(不會出現
#: 在別的 app 的裝置清單裡),但我們自己要靠名字把它找回來
DEVICE_NAME = "meeting-scribe 系統聲音"

_lock = threading.Lock()
#: 這個行程建出來、還沒清掉的 (聚合裝置, tap)。⚠️ **聚合裝置活得比行程久**
#: ——不清就會堆在使用者的音訊設定裡,所以另外掛 atexit 當最後一道
_live: list[tuple[int, int]] = []


def _load():
    objc = ctypes.CDLL(ctypes.util.find_library("objc"))
    ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    return objc, ca


def _send(objc, restype, argtypes, obj, sel, *args):
    """一次呼叫、一份簽章(見模組 docstring 那條 arm64 的警告)。"""
    proto = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
    fn = proto(ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value)
    return fn(obj, objc.sel_registerName(sel), *args)


def _obj(objc, obj, sel, *args):
    return _send(objc, ctypes.c_void_p, [ctypes.c_void_p] * len(args), obj, sel,
                 *[ctypes.c_void_p(a) for a in args])


def _nsstr(objc, s: str) -> int:
    return _send(objc, ctypes.c_void_p, [ctypes.c_char_p],
                 objc.objc_getClass(b"NSString"), b"stringWithUTF8String:",
                 ctypes.c_char_p(s.encode()))


def _nsnum(objc, n: int) -> int:
    return _send(objc, ctypes.c_void_p, [ctypes.c_int],
                 objc.objc_getClass(b"NSNumber"), b"numberWithInt:", n)


def _nsdict(objc, pairs) -> int:
    """NSDictionary 與 `CFDictionaryRef` 是 toll-free bridged,可以直接餵下去。"""
    d = _obj(objc, objc.objc_getClass(b"NSMutableDictionary"), b"dictionary")
    for key, val in pairs:
        _obj(objc, d, b"setObject:forKey:", val, _nsstr(objc, key))
    return d


def _nsarray(objc, items) -> int:
    a = _obj(objc, objc.objc_getClass(b"NSMutableArray"), b"array")
    for it in items:
        _obj(objc, a, b"addObject:", it)
    return a


class _Addr(ctypes.Structure):
    """`AudioObjectPropertyAddress`。⚠️ 三個 UInt32,順序不可調——排錯了是
    **靜默的越界讀**(`power._REASON_UNION` 踩過同一種)。"""

    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode(), "big")


def has_permission() -> bool:
    """「螢幕與系統音訊錄製」給了沒有。

    ⚠️ **這一支存在的唯一理由是「沒授權時完全不報錯」**:少了它,使用者拿到
    的是一整場靜音,而散會才會發現。
    ⚠️ **它問的是 TCC 資料庫的現況,不是這個行程的**:權限授給**發起呼叫的
    那個 App**(從終端機跑就記在終端機身上),而執行中的行程拿的是**啟動當下
    的快照**——剛在系統設定裡勾好,舊的視窗仍然是拒絕的。所以「這裡說有、
    錄起來還是零」是**會發生的**,錯誤訊息要寫「開完請重新啟動」。"""
    try:
        cg = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(cg.CGPreflightScreenCaptureAccess())
    except Exception:
        logger.debug("問不到螢幕/系統音訊錄製權限", exc_info=True)
        return True          # 問不出來就別擋路(真的沒聲音時另一層會抓到)


def available() -> bool:
    """這台 macOS 有沒有 Process Tap 那組 API(14.2 起)。"""
    try:
        objc, _ca = _load()
        return bool(objc.objc_getClass(b"CATapDescription"))
    except Exception:
        logger.debug("載不到 Core Audio / ObjC runtime", exc_info=True)
        return False


class SystemAudio:
    """一次錄音用的系統聲音來源。用法:`open_source()` → `.name` → `.close()`。

    ⚠️ **一定要 `close()`**:聚合裝置註冊在 Core Audio 裡、活得比這個行程久,
    不清就會一個一個堆在使用者的音訊設定中。`atexit` 那道只是最後保險。"""

    def __init__(self, device: int, tap: int, name: str) -> None:
        self._device, self._tap, self.name = device, tap, name

    def close(self) -> None:
        with _lock:
            pair = (self._device, self._tap)
            if pair not in _live:
                return                      # 已經清過了(冪等)
            _live.remove(pair)
        _destroy(self._device, self._tap)


def _destroy(device: int, tap: int) -> None:
    """⚠️ **絕不拋**:它跑在收尾路徑上,而收尾失敗不該蓋掉真正的結果。"""
    try:
        _objc, ca = _load()
        ca.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]
        ca.AudioHardwareDestroyAggregateDevice(device)
        ca.AudioHardwareDestroyProcessTap.argtypes = [ctypes.c_uint32]
        ca.AudioHardwareDestroyProcessTap(tap)
    except Exception:
        logger.debug("清掉系統聲音裝置時出錯", exc_info=True)


@atexit.register
def _cleanup_all() -> None:
    with _lock:
        pending, _live[:] = list(_live), []
    for device, tap in pending:
        logger.debug("行程結束時補清一個系統聲音裝置(%s/%s)", device, tap)
        _destroy(device, tap)


def open_source() -> SystemAudio:
    """建一個全系統的 tap 並包成聚合裝置;失敗一律拋 `OSError`(附 OSStatus)。

    ⚠️ **UID 帶 pid**:硬退出留下的殘骸不會與這一次撞名,而撞名的症狀是
    「建不起來」或「接到上一次的殘骸上」,兩者都很難查。"""
    objc, ca = _load()
    cls = objc.objc_getClass(b"CATapDescription")
    if not cls:
        raise OSError("這台 macOS 沒有 Core Audio Process Tap(需要 14.2 以上)")

    empty = _obj(objc, objc.objc_getClass(b"NSArray"), b"array")
    desc = _obj(objc, _obj(objc, cls, b"alloc"),
                b"initMonoGlobalTapButExcludeProcesses:", empty)
    _obj(objc, desc, b"setName:", _nsstr(objc, DEVICE_NAME))
    # private = 不出現在別的 app 的裝置清單裡:使用者的音訊設定不該被我們弄亂
    _send(objc, None, [ctypes.c_bool], desc, b"setPrivate:", True)

    ca.AudioHardwareCreateProcessTap.restype = ctypes.c_int32
    ca.AudioHardwareCreateProcessTap.argtypes = [ctypes.c_void_p,
                                                 ctypes.POINTER(ctypes.c_uint32)]
    tap = ctypes.c_uint32(0)
    st = ca.AudioHardwareCreateProcessTap(ctypes.c_void_p(desc), ctypes.byref(tap))
    if st != 0:
        raise OSError(f"建立系統聲音擷取失敗(OSStatus {st})")

    try:
        uid = _tap_uid(ca, objc, tap.value)
        device = _create_aggregate(ca, objc, uid)
    except Exception:
        ca.AudioHardwareDestroyProcessTap.argtypes = [ctypes.c_uint32]
        ca.AudioHardwareDestroyProcessTap(tap.value)
        raise
    with _lock:
        _live.append((device, tap.value))
    logger.info("系統聲音來源已建立(聚合裝置 %s,tap %s)", device, tap.value)
    return SystemAudio(device, tap.value, DEVICE_NAME)


def _tap_uid(ca, objc, tap_id: int) -> str:
    """`kAudioTapPropertyUID`('tuid')——聚合裝置靠這個字串指回那個 tap。"""
    ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
    addr = _Addr(_fourcc("tuid"), _fourcc("glob"), 0)
    out = ctypes.c_void_p(0)
    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
    st = ca.AudioObjectGetPropertyData(
        ctypes.c_uint32(tap_id), ctypes.byref(addr), 0, None,
        ctypes.byref(size), ctypes.byref(out))
    if st != 0:
        raise OSError(f"取系統聲音擷取的識別碼失敗(OSStatus {st})")
    return _send(objc, ctypes.c_char_p, [], out.value, b"UTF8String").decode()


def _create_aggregate(ca, objc, tap_uid: str) -> int:
    d = _nsdict(objc, [
        ("name", _nsstr(objc, DEVICE_NAME)),
        ("uid", _nsstr(objc, f"com.meeting-scribe.systemaudio.{os.getpid()}")),
        ("private", _nsnum(objc, 1)),
        ("stacked", _nsnum(objc, 0)),
        ("taps", _nsarray(objc, [_nsdict(objc, [("uid", _nsstr(objc, tap_uid)),
                                                ("drift", _nsnum(objc, 0))])])),
        ("subdevices", _nsarray(objc, [])),
    ])
    ca.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32
    ca.AudioHardwareCreateAggregateDevice.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    dev = ctypes.c_uint32(0)
    st = ca.AudioHardwareCreateAggregateDevice(ctypes.c_void_p(d),
                                               ctypes.byref(dev))
    if st != 0:
        raise OSError(f"把系統聲音包成錄音裝置失敗(OSStatus {st})")
    return dev.value
