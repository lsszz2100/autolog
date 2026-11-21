"""라이선스 관리 도구 (개발 PC 전용 — 배포물에 포함하지 않음)

서버에는 발급·관리 기능이 없다. 이 도구가 Cloudflare 계정으로 로그인된 wrangler 로
D1 데이터베이스에 직접 기록한다 → Cloudflare 계정 + 이 PC의 비밀값(PEPPER)이 모두 있어야 발급 가능.

  python3 packaging/license_admin.py issue 30 [메모]        # 30일·6,000토큰 키 발급 (40 / 50 도 같음)
  python3 packaging/license_admin.py issue 30 --tokens 1000 [메모]   # 토큰 수 직접 지정
  python3 packaging/license_admin.py issue admin <16자리> [메모]
  python3 packaging/license_admin.py list                   # 목록 (남은 토큰·만료·계정 수·발행 수)
  python3 packaging/license_admin.py revoke <키>            # 사용 정지 (activate 로 해제)
  python3 packaging/license_admin.py add_tokens <키> <수>   # 토큰 추가(음수면 차감)
  python3 packaging/license_admin.py extend <키> <일수>     # 기간 연장
  python3 packaging/license_admin.py reset_accounts <키>    # 등록된 블로그 계정 초기화
  python3 packaging/license_admin.py logout_pc <키>         # 로그인된 PC 해제 (다음 로그인 PC가 사용)
  python3 packaging/license_admin.py delete <키>            # 키 삭제

발급한 키 원문은 packaging/license_server/issued.json 에만 기록 (서버에는 해시만 저장).
"""
import hashlib
import hmac
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER_DIR = HERE / "license_server"
SECRETS = SERVER_DIR / ".secrets.json"
ISSUED = SERVER_DIR / "issued.json"
DB = "nbs-licenses"
PLANS = {"30": (30, 6000), "40": (40, 8500), "50": (50, 12000), "admin": (0, 0)}
# 헷갈리는 글자(0/O, 1/I/L) 제외한 영문 대문자 + 숫자
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def norm(key: str) -> str:
    return "".join(ch for ch in key.upper() if ch.isalnum())


def fmt(key: str) -> str:
    k = norm(key)
    return " ".join(k[i:i + 4] for i in range(0, 16, 4))


def new_key() -> str:
    while True:
        k = "".join(secrets.choice(ALPHABET) for _ in range(16))
        if any(c.isdigit() for c in k) and any(c.isalpha() for c in k):
            return k


def key_hash(key: str) -> str:
    pepper = json.loads(SECRETS.read_text())["PEPPER"]
    return hmac.new(pepper.encode(), ("key:" + norm(key)).encode(), hashlib.sha256).hexdigest()


def q(v) -> str:
    return str(int(v)) if isinstance(v, (int, bool)) else "'" + str(v).replace("'", "''") + "'"


def sql(statement: str) -> list:
    for attempt in range(3):
        r = subprocess.run(["npx", "-y", "wrangler@4", "d1", "execute", DB, "--remote", "--json", "--command", statement],
                           cwd=SERVER_DIR, capture_output=True, text=True, timeout=180)
        try:
            out = json.loads(r.stdout[r.stdout.index("["):])
            break
        except Exception:
            # Cloudflare 로그인 열쇠 자동 갱신 순간의 일시 오류(7403 등)는 잠시 후 다시 시도
            if attempt < 2:
                time.sleep(3)
                continue
            sys.exit(f"데이터베이스 작업 실패:\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
    if not all(x.get("success") for x in out):
        sys.exit(f"데이터베이스 작업 실패: {out}")
    return out[0].get("results", []), out[0].get("meta", {})


def _issued() -> list:
    try:
        return json.loads(ISSUED.read_text(encoding="utf-8"))
    except Exception:
        return []


def _when(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "무기한"


def issue(plan: str, key: str = "", memo: str = "", tokens_override: int = None):
    if plan not in PLANS:
        sys.exit("종류는 30 / 40 / 50 / admin 중 하나입니다.")
    key = norm(key) if key else new_key()
    if len(key) != 16:
        sys.exit("키는 16자리여야 합니다.")
    days, tokens = PLANS[plan]
    if tokens_override is not None and plan != "admin":
        tokens = int(tokens_override)
    t = int(time.time())
    exp = t + days * 86400 if days else 0
    _, meta = sql("INSERT OR IGNORE INTO licenses (key_hash, plan, unlimited, tokens_total, tokens_left, issued_at, expires_at, "
                  f"status, memo, key_hint) VALUES ({q(key_hash(key))}, {q(plan)}, {q(plan == 'admin')}, {q(tokens)}, {q(tokens)}, "
                  f"{q(t)}, {q(exp)}, 'active', {q(memo[:200])}, {q(key[-4:])})")
    if not meta.get("changes"):
        sys.exit("이미 있는 키입니다.")
    items = _issued()
    items.append({"key": fmt(key), "plan": plan, "memo": memo, "issued": _when(t), "expires": _when(exp),
                  "tokens": "무제한" if plan == "admin" else tokens})
    ISSUED.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"발급 완료: {fmt(key)}  | {plan}{'' if plan == 'admin' else '일'} | 토큰 {items[-1]['tokens']} | 만료 {items[-1]['expires']}"
          + (f" | {memo}" if memo else ""))


def list_all():
    rows, _ = sql("SELECT l.*, (SELECT COUNT(*) FROM accounts a WHERE a.key_hash = l.key_hash) AS accounts, "
                  "(SELECT COUNT(*) FROM usage u WHERE u.key_hash = l.key_hash AND u.state != 'refunded') AS posts "
                  "FROM licenses l ORDER BY issued_at DESC")
    keys = {norm(i["key"])[-4:]: i["key"] for i in _issued()}
    t = time.time()
    print(f"{'키':<21}{'종류':<7}{'상태':<9}{'남은 토큰':>13}{'발행':>5}{'계정':>5}  {'만료':<17}{'최근 사용':<17}메모")
    for l in rows:
        tokens = "무제한" if l["unlimited"] else f"{l['tokens_left']:,}/{l['tokens_total']:,}"
        status = "expired" if l["expires_at"] and l["expires_at"] <= t else l["status"]
        print(f"{keys.get(l['key_hint'], '…' + l['key_hint']):<21}{l['plan']:<7}{status:<9}{tokens:>13}{l['posts']:>5}{l['accounts']:>5}  "
              f"{_when(l['expires_at']):<17}{_when(l['last_used']) if l['last_used'] else '-':<17}{l['memo']}")


def update(key: str, action: str, n: int = 0):
    kh = q(key_hash(key))
    stmts = {
        "revoke": f"UPDATE licenses SET status = 'revoked' WHERE key_hash = {kh}",
        "activate": f"UPDATE licenses SET status = 'active' WHERE key_hash = {kh}",
        "add_tokens": f"UPDATE licenses SET tokens_left = MAX(tokens_left + {q(n)}, 0), tokens_total = MAX(tokens_total + {q(n)}, 0) WHERE key_hash = {kh}",
        "extend": f"UPDATE licenses SET expires_at = CASE WHEN expires_at = 0 THEN 0 ELSE MAX(expires_at, {q(int(time.time()))}) + {q(n * 86400)} END WHERE key_hash = {kh}",
        "reset_accounts": f"DELETE FROM accounts WHERE key_hash = {kh}",
        "logout_pc": f"UPDATE licenses SET session_id = '' WHERE key_hash = {kh}",
        "delete": f"DELETE FROM accounts WHERE key_hash = {kh}; DELETE FROM licenses WHERE key_hash = {kh}",
    }
    exists, _ = sql(f"SELECT plan FROM licenses WHERE key_hash = {kh}")
    if not exists:
        sys.exit("없는 키입니다.")
    sql(stmts[action])
    print(f"완료: {fmt(key)} {action}" + (f" {n}" if n else ""))


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    cmd = a[0]
    if cmd == "issue":
        override = None
        if "--tokens" in a:
            i = a.index("--tokens")
            override = int(a[i + 1])
            del a[i:i + 2]
        plan = a[1]
        if plan == "admin" or (len(a) > 2 and len(norm(a[2])) == 16 and any(c.isdigit() for c in a[2])):
            issue(plan, a[2] if len(a) > 2 else "", " ".join(a[3:]), override)
        else:
            issue(plan, "", " ".join(a[2:]), override)
    elif cmd == "list":
        list_all()
    elif cmd in ("revoke", "activate", "reset_accounts", "logout_pc", "delete"):
        update(" ".join(a[1:]), cmd)
    elif cmd in ("add_tokens", "extend"):
        update(" ".join(a[1:-1]), cmd, int(a[-1]))
    else:
        sys.exit(__doc__)
