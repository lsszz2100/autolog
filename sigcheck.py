"""업데이트 서명 확인 (sigcheck.py)

배포할 때 update.json(버전·파일 SHA256)에 개발자 개인 키로 Ed25519 서명을 붙이고,
프로그램은 내장된 공개 키로 서명을 확인한 뒤에만 업데이트한다.
→ 배포 저장소(GitHub) 계정이 털려도 서명 없는 가짜 업데이트는 설치되지 않음.

Ed25519 구현: RFC 8032 6장 참조 구현 (표준 라이브러리만 사용, 확인 1회 수십 ms).
개인 키는 packaging/signing_key.hex (배포물에 절대 포함하지 않음).
"""
import hashlib
import json

# 배포 서명 공개 키 (짝이 되는 개인 키: 개발 PC의 packaging/signing_key.hex)
PUBLIC_KEY_HEX = "6982ae0fa390336647577791ffb51bfeb30f807aeb997e539f6b83ff6d885a44"

_p = 2 ** 255 - 19
_q = 2 ** 252 + 27742317777372353535851937790883648493


def _inv(x):
    return pow(x, _p - 2, _p)


_d = -121665 * _inv(121666) % _p
_sqrt_m1 = pow(2, (_p - 1) // 4, _p)


def _h(s: bytes) -> int:
    return int.from_bytes(hashlib.sha512(s).digest(), "little") % _q


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C = 2 * P[3] * Q[3] * _d % _p
    D = 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F, G * H, F * G, E * H)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % _p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def _recover_x(y, sign):
    if y >= _p:
        return None
    x2 = (y * y - 1) * _inv(_d * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _sqrt_m1 % _p
    if (x * x - x2) % _p != 0:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_gy = 4 * _inv(5) % _p
_gx = _recover_x(_gy, 0)
_G = (_gx, _gy, 1, _gx * _gy % _p)


def _compress(P) -> bytes:
    zi = _inv(P[2])
    x, y = P[0] * zi % _p, P[1] * zi % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _p)


def _expand(seed: bytes):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    return _compress(_mul(_expand(seed)[0], _G))


def sign(seed: bytes, msg: bytes) -> bytes:
    a, prefix = _expand(seed)
    A = _compress(_mul(a, _G))
    r = _h(prefix + msg)
    Rs = _compress(_mul(r, _G))
    s = (r + _h(Rs + A + msg) * a) % _q
    return Rs + int.to_bytes(s, 32, "little")


def verify(public: bytes, msg: bytes, sig: bytes) -> bool:
    if len(public) != 32 or len(sig) != 64:
        return False
    A = _decompress(public)
    R = _decompress(sig[:32])
    if not A or not R:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _q:
        return False
    h = _h(sig[:32] + public + msg)
    return _equal(_mul(s, _G), _add(R, _mul(h, A)))


# ── update.json 서명 ──
def _canonical(manifest: dict) -> bytes:
    body = {k: v for k, v in manifest.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sign_manifest(manifest: dict, seed: bytes) -> dict:
    out = dict(manifest)
    out["sig"] = sign(seed, _canonical(manifest)).hex()
    return out


def verify_manifest(manifest: dict) -> bool:
    """서명이 내장 공개 키와 맞으면 True. 공개 키가 비어 있으면(개발 실행) 항상 False."""
    try:
        if not PUBLIC_KEY_HEX or not isinstance(manifest, dict):
            return False
        return verify(bytes.fromhex(PUBLIC_KEY_HEX), _canonical(manifest), bytes.fromhex(manifest.get("sig", "")))
    except Exception:
        return False
