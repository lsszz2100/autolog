"""라이선스·토큰 (licensing.py)

- 프로그램을 쓰려면 16자리 라이선스 키 필요. 키 확인·토큰 차감·N사 계정 등록은 라이선스 서버가 담당
  (어느 PC에서든 같은 장부 → 재설치·다른 PC로 토큰이 늘지 않음)
- 서버 응답은 Ed25519 서명 + 요청마다 새 nonce 로 확인 → 가짜 서버·재전송 응답 차단
- 키는 Windows 자격 증명 저장소(없으면 사용자 폴더)에 보관
- 한 키는 한 번에 PC 한 대 (이 설치본의 세션 번호). 다른 PC에서 로그인하면 여기는 로그아웃
- 발행 1건 = 100토큰: 발행 시작 전에 서버에서 선차감, 발행 실패 시 환불
- 디버거가 붙어 있으면 라이선스 확인 거부
"""
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

import sigcheck
from config import USER_DIR, _keyring, KEYRING_SERVICE

logger = logging.getLogger("naver.license")

SERVER = "https://nbs-license.leemanrank.workers.dev"
SERVER_PUBLIC_HEX = "b1a9f1cbd781756084062b8c420e2382d5fae3e2678c3dd672633c206686c74f"  # 라이선스 서버 응답 서명 공개 키
COST_PER_POST = 100
CACHE_FILE = USER_DIR / "license_cache.json"
SESSION_FILE = USER_DIR / "license_session.txt"
PENDING_OLD = USER_DIR / "license_pending.json"
KEY_FILE = USER_DIR / "license_key.txt"     # 자격 증명 저장소가 없을 때만 사용
KEYRING_NAME = "license_key"
REFRESH_SEC = 10 * 60
# 서버에 잠시 연결이 안 될 때 마지막 확인 결과를 쓰는 시간. 이 프로그램이 켜진 뒤 서버에서 직접 받은 확인만 센다
# (시간은 PC 시계가 아닌 켜진 뒤 흐른 시간으로 잼 → 시계를 되돌리거나 저장 파일을 옛것으로 바꿔도 연장되지 않음)
OFFLINE_GRACE_SEC = 30 * 60

_lock = threading.RLock()
_state = {"license": None, "checked_at": 0.0, "error": "", "live_at": None}   # live_at = 서버 확인을 받은 time.monotonic()


class LicenseError(RuntimeError):
    pass


def norm_key(key: str) -> str:
    return "".join(ch for ch in str(key or "").upper() if ch.isalnum())


def fmt_key(key: str) -> str:
    k = norm_key(key)
    return " ".join(k[i:i + 4] for i in range(0, len(k), 4))


# ── 키 보관 ──
def _load_key() -> str:
    kr = _keyring()
    if kr:
        try:
            v = kr.get_password(KEYRING_SERVICE, KEYRING_NAME)
            if v:
                return v
        except Exception:
            pass
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _save_key(key: str):
    kr = _keyring()
    if kr:
        try:
            if key:
                kr.set_password(KEYRING_SERVICE, KEYRING_NAME, key)
            else:
                kr.delete_password(KEYRING_SERVICE, KEYRING_NAME)
            KEY_FILE.unlink(missing_ok=True)
            return
        except Exception:
            pass
    if key:
        KEY_FILE.write_text(key, encoding="utf-8")
    else:
        KEY_FILE.unlink(missing_ok=True)


# ── 서버 호출 ──
def _verify_env(env: dict) -> dict:
    data, sig = env["data"], env["sig"]
    if not sigcheck.verify(bytes.fromhex(SERVER_PUBLIC_HEX), data.encode("utf-8"), bytes.fromhex(sig)):
        raise LicenseError("라이선스 서버 응답의 서명이 올바르지 않습니다.")
    return json.loads(data)


def _session() -> str:
    """이 설치본의 세션 번호 (한 키 = 한 번에 PC 한 대)."""
    try:
        v = SESSION_FILE.read_text(encoding="ascii").strip()
        if len(v) == 32 and all(c in "0123456789abcdef" for c in v):
            return v
    except Exception:
        pass
    v = secrets.token_hex(16)
    try:
        SESSION_FILE.write_text(v, encoding="ascii")
    except Exception:
        pass
    return v


def _debugged() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        if k32.IsDebuggerPresent():
            return True
        flag = ctypes.c_int(0)
        k32.CheckRemoteDebuggerPresent(k32.GetCurrentProcess(), ctypes.byref(flag))
        return bool(flag.value)
    except Exception:
        return False


def _call(path: str, body: dict, timeout: float = 15) -> dict:
    if _debugged():
        raise LicenseError("디버거가 감지되어 라이선스를 확인할 수 없습니다.")
    nonce = secrets.token_hex(16)
    req = urllib.request.Request(
        SERVER + path, data=json.dumps({**body, "nonce": nonce, "session": _session()}).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "NaverBlogStudio"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        if e.code not in (200, 429):
            raise LicenseError(f"라이선스 서버 오류 (HTTP {e.code})")
    except Exception as e:
        raise ConnectionError(f"라이선스 서버에 연결할 수 없습니다: {e}")
    try:
        env = json.loads(raw)
        out = _verify_env(env)
    except LicenseError:
        raise
    except Exception:
        raise LicenseError("라이선스 서버 응답 형식이 올바르지 않습니다.")
    if out.get("nonce") != nonce:
        raise LicenseError("라이선스 서버 응답이 이 요청의 것이 아닙니다.")
    out["_env"] = env   # 서명된 원본 (캐시용)
    return out


def _remember(resp: dict):
    lic = resp.get("license")
    if lic is not None:
        with _lock:
            _state.update(license=lic, checked_at=float(resp.get("server_time") or time.time()), error="",
                          live_at=time.monotonic())
        try:
            # 서버 서명이 붙은 원본 그대로 저장 → 파일을 고치면 불러올 때 서명 확인에서 걸림
            CACHE_FILE.write_text(json.dumps(resp["_env"]), encoding="utf-8")
        except Exception:
            pass


def _load_cache():
    try:
        out = _verify_env(json.loads(CACHE_FILE.read_text(encoding="utf-8")))
        if out.get("license") is not None:
            with _lock:
                if not _state["license"]:
                    _state.update(license=out["license"], checked_at=float(out.get("server_time", 0)))
    except Exception:
        pass


# ── 공개 함수 ──
def activate(key: str) -> dict:
    """키 확인 후 사용 가능하면 저장. 반환: 화면용 상태. 실패 시 LicenseError/ConnectionError."""
    k = norm_key(key)
    if len(k) != 16:
        raise LicenseError("라이선스 키 16자리를 모두 입력해 주세요.")
    resp = _call("/v1/check", {"key": k, "take_over": True})   # 이 PC로 로그인 (다른 PC는 로그아웃)
    if not resp.get("ok"):
        code = resp.get("code")
        msg = resp.get("msg") or {"expired": "기간이 끝난 라이선스입니다.", "revoked": "사용이 중지된 라이선스입니다."}.get(code, "사용할 수 없는 라이선스입니다.")
        raise LicenseError(msg)
    _save_key(k)
    _remember(resp)
    logger.info(f"🔑 라이선스 확인 완료 (…{k[-4:]})")
    return status()


def logout():
    _save_key("")
    with _lock:
        _state.update(license=None, checked_at=0.0, error="", live_at=None)
    try:
        CACHE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def refresh(force: bool = False) -> dict:
    """서버에서 최신 상태 확인 (10분 이내 확인했으면 생략). 연결 실패 시 마지막 결과 유지."""
    key = _load_key()
    if not key:
        return status()
    with _lock:
        fresh = _state["license"] and _state["live_at"] is not None and time.monotonic() - _state["live_at"] < REFRESH_SEC
    if fresh and not force:
        return status()
    try:
        resp = _call("/v1/check", {"key": key})
        if resp.get("code") in ("invalid", "session_moved"):
            logout()
            with _lock:
                _state["error"] = resp.get("msg") or "라이선스를 다시 입력해 주세요."
            logger.warning(f"🔑 라이선스 로그아웃: {_state['error']}")
            return status()
        _remember(resp)
        if not resp.get("ok"):
            with _lock:
                _state["error"] = resp.get("msg") or ""
    except ConnectionError as e:
        with _lock:
            _state["error"] = str(e)
    except LicenseError as e:
        with _lock:
            _state["error"] = str(e)
    return status()


def status() -> dict:
    """화면·사용 가능 여부 판단용 상태."""
    key = _load_key()
    with _lock:
        lic = _state["license"]
        live = _state["live_at"]
        err = _state["error"]
        server_now = _state["checked_at"] + (time.monotonic() - live if live is not None else 0)
    # 저장 파일(캐시)만으로는 쓸 수 없다: 이번 실행에서 서버가 '사용 가능'이라고 답한 뒤 30분 안이어야 한다
    usable = bool(key and lic and lic.get("usable") and live is not None and time.monotonic() - live < OFFLINE_GRACE_SEC)
    if lic and lic.get("expires_at") and lic["expires_at"] <= max(time.time(), server_now):   # 만료도 서버 시각 기준
        usable = False
    out = {"has_key": bool(key), "key_hint": f"…{key[-4:]}" if key else "", "usable": usable, "error": err,
           "license": lic, "cost_per_post": COST_PER_POST}
    if lic:
        out["enough_tokens"] = bool(lic.get("unlimited") or lic.get("tokens_left", 0) >= COST_PER_POST)
    return out


def is_usable() -> bool:
    return status()["usable"]


def reserve(account: str = "") -> str:
    """발행 시작 전 서버에서 100토큰 선차감 (반드시 서버 확인 — 저장된 결과로는 불가). 반환: 요청 번호(환불용)."""
    key = _load_key()
    if not key:
        raise LicenseError("라이선스 키를 먼저 입력해 주세요.")
    rid = secrets.token_hex(16)
    try:
        resp = _call("/v1/reserve", {"key": key, "request_id": rid, "account": account})
    except ConnectionError:
        raise LicenseError("라이선스 서버에 연결할 수 없어 발행할 수 없습니다. 인터넷 연결을 확인해 주세요.")
    if resp.get("code") == "session_moved":
        logout()
        with _lock:
            _state["error"] = resp.get("msg", "")
    else:
        _remember(resp)
    if not resp.get("ok"):
        raise LicenseError(resp.get("msg") or "라이선스를 확인할 수 없습니다.")
    lic = resp.get("license") or {}
    logger.info(f"🪙 토큰 {COST_PER_POST} 사용 → 남은 토큰 {'무제한' if lic.get('unlimited') else format(lic.get('tokens_left', 0), ',')}")
    return rid


def refund(request_id: str):
    """발행이 실패하면 선차감한 토큰 환불 (1시간 이내, 하루 5회까지)."""
    if not request_id:
        return
    try:
        resp = _call("/v1/refund", {"key": _load_key(), "request_id": request_id})
        _remember(resp)
        if resp.get("ok"):
            logger.info(f"🪙 발행이 완료되지 않아 토큰 {COST_PER_POST}을 돌려받았습니다.")
        else:
            logger.warning(f"토큰 환불 불가: {resp.get('msg')}")
    except Exception as e:
        logger.warning(f"토큰 환불 실패: {e}")


def account_add(naver_id: str):
    """라이선스에 N사 계정 등록 (라이선스당 최대 5개, 다른 PC 포함). 초과 시 LicenseError."""
    resp = _call("/v1/accounts/add", {"key": _load_key(), "account": naver_id.lower()})
    _remember(resp)
    if not resp.get("ok"):
        raise LicenseError(resp.get("msg") or "계정을 등록할 수 없습니다.")


def account_remove(naver_id: str):
    try:
        _remember(_call("/v1/accounts/remove", {"key": _load_key(), "account": naver_id.lower()}))
    except Exception as e:
        logger.warning(f"라이선스 계정 등록 해제 실패: {e}")


def start_background():
    """시작 시 캐시 불러오기 + 10분마다 상태 확인·밀린 차감 재전송."""
    _load_cache()

    try:
        PENDING_OLD.unlink(missing_ok=True)   # 예전 버전의 '나중 차감' 기록 정리
    except Exception:
        pass

    def loop():
        while True:
            try:
                refresh(force=True)
            except Exception:
                pass
            time.sleep(REFRESH_SEC)
    threading.Thread(target=loop, daemon=True).start()
