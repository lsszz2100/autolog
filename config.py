"""설정 및 포스팅 이력 관리 모듈 (config.py)
"""
import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path

# 포털 이름(한글). 소스에 상표명을 직접 쓰지 않으려고 유니코드 이스케이프로 둠
PORTAL_KO = "\ub124\uc774\ubc84"

# ── 경로 ──
# 개발 실행: 소스 폴더에 설정·데이터 저장
# 설치형(PyInstaller EXE): 프로그램 폴더는 업데이트 때 덮어쓰이므로 설정·데이터는 %LOCALAPPDATA%\NaverBlogStudio 에 저장
IS_FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = Path(sys.executable).resolve().parent if IS_FROZEN else Path(__file__).resolve().parent
USER_DIR = (Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "NaverBlogStudio") if IS_FROZEN else BASE_DIR
DATA_DIR = USER_DIR / "data"

# 설치형은 사용자 PC의 크롬이 아니라 프로그램 폴더에 함께 넣은 Chromium(ms-playwright)을 사용
# (playwright import 전에 설정돼야 하므로 config 모듈 로드 시점에 지정)
if IS_FROZEN and (BASE_DIR / "ms-playwright").is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BASE_DIR / "ms-playwright"))
# 브라우저 쿠키 암호화 방식이 OS마다 달라(Windows DPAPI vs Linux) 한 프로필을 WSL·Windows가 번갈아 쓰면
# 서로의 로그인 쿠키를 해독하지 못해 세션이 날아간다 → OS별로 프로필 분리
PROFILE_DIR = DATA_DIR / ("naver_profile_win" if os.name == "nt" else "naver_profile")
CONFIG_FILE = USER_DIR / "config.json"
HISTORY_FILE = DATA_DIR / "history.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR.mkdir(parents=True, exist_ok=True)


# ── 판매판 ──
# 크몽 판매판(kmong)은 검색 데이터 수집(자동완성·연관 검색어·상위 글)을 쓰지 않는다 (크몽 등록 정책).
# 설치 파일이 프로그램 폴더에 edition.txt를 넣고, 첫 실행 때 사용자 폴더에도 복사해 둔다
# (코드 업데이트·전체 설치 파일 업데이트 뒤에도 유지). 시험할 때는 AUTOLOG_EDITION 환경 변수.
def _read_edition() -> str:
    env = os.environ.get("AUTOLOG_EDITION", "").strip().lower()
    if env:
        return env
    for p in (BASE_DIR / "edition.txt", USER_DIR / "edition.txt"):
        try:
            val = p.read_text(encoding="utf-8").strip().lower()
        except Exception:
            continue
        if val:
            if p.parent == BASE_DIR and BASE_DIR != USER_DIR:
                try:
                    (USER_DIR / "edition.txt").write_text(val, encoding="utf-8")
                except Exception:
                    pass
            return val
    return "full"


EDITION = _read_edition()
NO_SEARCH_DATA = EDITION == "kmong"

DEFAULT_CONFIG = {
    "naver_id": "",
    "naver_pw": "",
    "blog_id": "",          # 비어있으면 naver_id와 동일하게 사용
    "headless": False,      # 브라우저 창 표시 여부 (True: 백그라운드, False: 화면 표시)
    "post_category": "",    # 기본 블로그 카테고리명 (선택)
    "gemini_api_key": "",   # Google Gemini API 키 (선택)
    "auto_thumbnail": True, # 카드뉴스형 썸네일 이미지 자동 생성 및 첨부
    "public_open": True,    # 전체공개 여부
    "min_interval_min": 30, # 발행(예약 포함) 간 최소 간격(분). 이 안에는 다른 글 1건까지만 허용. 0이면 제한 없음
    # 모든 글에 자동으로 넣는 고정 영역 (상호명·이름·전화번호·주소 등은 사용자가 입력한 그대로 입력)
    "header_text": "",      # 본문 맨 위 문구 (여러 줄)
    "footer_text": "",      # 본문 맨 아래(해시태그 위) 문구 (여러 줄)
    "map_query": "",        # 지도로 넣을 장소 검색어 (상호명 또는 주소). 비우면 지도 없음
    "map_position": "bottom",  # top: 상단 문구 아래, bottom: 하단 문구 아래
    # 금지 키워드: 이 말이 들어간 주제·상품·문장은 쓰지 않는다 (모든 계정 공통)
    "banned_keywords": [],
    # 여러 계정 (최대 5개). 위의 naver_id·blog_id·카테고리·고정 문구는 '선택된 계정'의 값이고,
    # 계정별 값은 accounts 에 보관 → 계정을 바꾸면 위 값이 그 계정 것으로 바뀜
    "accounts": [],
    "active_account": "",
}

MAX_ACCOUNTS = 5
# N사 아이디(이메일형 포함)·블로그 ID 허용 문자 (화면·주소에 넣을 때 안전하게)
import re as _re
ID_RE = _re.compile(r"^[A-Za-z0-9._@+-]{2,64}$")
BLOG_ID_RE = _re.compile(r"^[A-Za-z0-9_-]{0,64}$")
# 계정마다 따로 쓰는 설정
ACCOUNT_FIELDS = ("naver_id", "blog_id", "post_category", "header_text", "footer_text", "map_query", "map_position")

# 비밀값은 가능하면 OS 자격 증명 저장소(Windows 자격 증명 관리자 등)에 보관하고 config.json 에는 비워 둡니다.
# keyring 미설치 또는 백엔드가 없는 환경(WSL 등)에서는 기존처럼 config.json 에 저장합니다.
SECRET_KEYS = ("naver_pw", "gemini_api_key")
RETIRED_SECRETS = ("pollinations_api_key",)  # 1.0.18에서 뺀 AI 사진 키: 남아 있으면 지운다
KEYRING_SERVICE = "NaverBlogStudio"

def _keyring():
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
        if isinstance(keyring.get_keyring(), FailKeyring):
            return None
        return keyring
    except Exception:
        return None

def load_config():
    if not CONFIG_FILE.exists():
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        cfg = DEFAULT_CONFIG.copy()
    else:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            try:
                cfg = json.load(f)
            except Exception:
                cfg = {}
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)

    for k in RETIRED_SECRETS:
        cfg.pop(k, None)
    kr = _keyring()
    if kr:
        for k in SECRET_KEYS:
            try:
                val = kr.get_password(KEYRING_SERVICE, k)
            except Exception:
                val = None
            if val:
                cfg[k] = val
        for k in RETIRED_SECRETS:
            try:
                if kr.get_password(KEYRING_SERVICE, k):
                    kr.delete_password(KEYRING_SERVICE, k)
            except Exception:
                pass
    # 예전(계정 1개 시절) 비밀번호: 1번 계정에 계정별 비밀번호가 없을 때만 사용
    cfg["_legacy_pw"] = cfg.get("naver_pw", "")
    _migrate_accounts(cfg)
    _apply_active(cfg)
    return cfg


# ── 여러 계정 ──
def _migrate_accounts(cfg):
    """예전 설정(계정 1개)을 계정 목록의 1번으로 옮김. 기존 브라우저 프로필(로그인 세션)을 그대로 사용."""
    if not isinstance(cfg.get("accounts"), list):
        cfg["accounts"] = []
    if not cfg["accounts"] and cfg.get("naver_id"):
        acc = {k: cfg.get(k, DEFAULT_CONFIG.get(k, "")) for k in ACCOUNT_FIELDS}
        acc["slot"] = 1
        if cfg.get("naver_pw") and not _keyring():
            acc["naver_pw"] = cfg["naver_pw"]
        cfg["accounts"] = [acc]
        cfg["active_account"] = cfg["naver_id"]
        cfg.setdefault("legacy_account", cfg["naver_id"])


def find_account(cfg, naver_id):
    return next((a for a in cfg.get("accounts", []) if a.get("naver_id") == naver_id), None)


def _account_pw(acc) -> str:
    kr = _keyring()
    if kr:
        try:
            v = kr.get_password(KEYRING_SERVICE, f"naver_pw:{acc['naver_id']}")
            if v:
                return v
        except Exception:
            pass
    return acc.get("naver_pw", "")


def _apply_active(cfg):
    """선택된 계정의 값을 위쪽(기존 코드가 읽는) 설정 값으로 채움."""
    acc = find_account(cfg, cfg.get("active_account"))
    if not acc and cfg.get("accounts"):
        acc = cfg["accounts"][0]
        cfg["active_account"] = acc["naver_id"]
    if not acc:
        return
    for k in ACCOUNT_FIELDS:
        cfg[k] = acc.get(k, DEFAULT_CONFIG.get(k, ""))
    # 예전 단일 계정 비밀번호(naver_pw)는 1번 계정 것으로 사용
    pw = _account_pw(acc)
    if not pw and acc.get("slot") == 1:
        pw = cfg.get("_legacy_pw", "")
    cfg["naver_pw"] = pw


def account_profile_dir(cfg=None, naver_id=None) -> Path:
    """계정별 브라우저 프로필 폴더 (1번 = 예전 폴더 그대로 → 기존 로그인 유지)."""
    cfg = cfg or load_config()
    acc = find_account(cfg, naver_id or cfg.get("active_account"))
    slot = (acc or {}).get("slot", 1)
    d = PROFILE_DIR if slot == 1 else PROFILE_DIR.parent / f"{PROFILE_DIR.name}_{slot}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def add_account(cfg, naver_id: str, naver_pw: str, blog_id: str = "") -> str:
    """새 계정 추가 후 선택. 오류 시 사유 문자열, 성공 시 ""."""
    naver_id = naver_id.strip()
    if not naver_id or not naver_pw:
        return "아이디와 비밀번호를 입력해주세요."
    if not ID_RE.match(naver_id):
        return "아이디 형식이 올바르지 않습니다. (영문·숫자·._-@ 만 사용)"
    if not BLOG_ID_RE.match(blog_id.strip()):
        return "블로그 ID 형식이 올바르지 않습니다. (영문·숫자·_- 만 사용)"
    if find_account(cfg, naver_id):
        return "이미 등록된 계정입니다."
    if len(cfg["accounts"]) >= MAX_ACCOUNTS:
        return f"계정은 최대 {MAX_ACCOUNTS}개까지 등록할 수 있습니다."
    used = {a.get("slot") for a in cfg["accounts"]}
    slot = next(n for n in range(1, MAX_ACCOUNTS + 1) if n not in used)
    acc = {k: DEFAULT_CONFIG.get(k, "") for k in ACCOUNT_FIELDS}
    acc.update({"naver_id": naver_id, "blog_id": blog_id.strip(), "slot": slot})
    cfg["accounts"].append(acc)
    if slot != 1:
        # 예전에 같은 칸을 쓰던 계정의 로그인이 남아 있지 않게 새 프로필로 시작
        import shutil
        old = PROFILE_DIR.parent / f"{PROFILE_DIR.name}_{slot}"
        shutil.rmtree(old, ignore_errors=True)
    elif not cfg.get("legacy_account"):
        cfg["legacy_account"] = naver_id
    select_account(cfg, naver_id)
    cfg["naver_pw"] = naver_pw
    return ""


def select_account(cfg, naver_id: str) -> bool:
    if not find_account(cfg, naver_id):
        return False
    cfg["active_account"] = naver_id
    _apply_active(cfg)
    return True


def set_account_blog(naver_id: str, blog_id: str):
    """계정의 블로그 주소 저장 (로그인·발행 때 실제 주소를 확인해 자동 저장)."""
    if not BLOG_ID_RE.match(blog_id or ""):
        return
    cfg = load_config()
    acc = find_account(cfg, naver_id)
    if not acc or acc.get("blog_id") == blog_id:
        return
    acc["blog_id"] = blog_id
    if cfg.get("active_account") == naver_id:
        cfg["blog_id"] = blog_id
    save_config(cfg)


def remove_account(cfg, naver_id: str) -> bool:
    """계정 삭제: 목록·저장된 비밀번호·로그인 세션(브라우저 프로필)을 지움."""
    acc = find_account(cfg, naver_id)
    if not acc:
        return False
    profile = account_profile_dir(cfg, naver_id)
    cfg["accounts"] = [a for a in cfg["accounts"] if a is not acc]
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(KEYRING_SERVICE, f"naver_pw:{naver_id}")
        except Exception:
            pass
        if acc.get("slot") == 1:
            try:
                kr.delete_password(KEYRING_SERVICE, "naver_pw")
            except Exception:
                pass
    import shutil
    shutil.rmtree(profile, ignore_errors=True)
    if cfg.get("active_account") == naver_id:
        cfg["active_account"] = cfg["accounts"][0]["naver_id"] if cfg["accounts"] else ""
        if cfg["accounts"]:
            _apply_active(cfg)
        else:
            for k in ACCOUNT_FIELDS:
                cfg[k] = DEFAULT_CONFIG.get(k, "")
            cfg["naver_pw"] = ""
    return True


def _sync_active(cfg):
    """위쪽 설정 값(화면에서 바꾼 값)을 선택된 계정에 반영. 계정이 없고 아이디가 있으면 첫 계정으로 등록."""
    if not isinstance(cfg.get("accounts"), list):
        cfg["accounts"] = []
    acc = find_account(cfg, cfg.get("active_account"))
    if not acc and cfg.get("naver_id") and not cfg["accounts"]:
        acc = {"slot": 1}
        cfg["accounts"].append(acc)
        cfg["active_account"] = cfg["naver_id"]
        cfg.setdefault("legacy_account", cfg["naver_id"])
    if acc:
        for k in ACCOUNT_FIELDS:
            acc[k] = cfg.get(k, DEFAULT_CONFIG.get(k, ""))
        acc["naver_id"] = cfg["active_account"]   # 등록된 계정의 아이디는 바꾸지 않음
        cfg["naver_id"] = acc["naver_id"]
    return acc

def save_config(cfg):
    acc = _sync_active(cfg)
    kr = _keyring()
    # 비밀번호는 계정별로 저장 (선택된 계정 것만 화면에서 바뀜)
    pw = cfg.get("naver_pw", "")
    if acc and pw:
        stored = False
        if kr:
            try:
                kr.set_password(KEYRING_SERVICE, f"naver_pw:{acc['naver_id']}", pw)
                stored = True
            except Exception:
                pass
        if stored:
            acc.pop("naver_pw", None)
        else:
            acc["naver_pw"] = pw      # 자격 증명 저장소가 없으면 파일에 보관
    to_write = {k: v for k, v in cfg.items() if not k.startswith("_")}
    to_write["accounts"] = [dict(a) for a in cfg.get("accounts", [])]
    to_write["naver_pw"] = ""
    if kr:
        for k in SECRET_KEYS:
            if k == "naver_pw":
                continue
            val = to_write.get(k, "")
            try:
                if val:
                    kr.set_password(KEYRING_SERVICE, k, val)
                to_write[k] = ""  # 자격 증명 저장소에 보관되었으므로 파일에는 남기지 않음
            except Exception:
                pass  # 저장소 쓰기 실패 시 파일에 유지
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(to_write, f, indent=2, ensure_ascii=False)

def delete_secret(cfg, k):
    """비밀값 삭제 (자격 증명 저장소와 config.json 모두). save_config는 빈 값이면 저장소를 건드리지 않으므로 따로 지움."""
    cfg[k] = ""
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(KEYRING_SERVICE, k)
        except Exception:
            pass  # 저장된 적 없음
    save_config(cfg)

MAX_BANNED = 200


def parse_banned(text) -> list:
    """화면 입력(줄바꿈·쉼표 구분) 또는 목록 → 중복 없는 금지 키워드 목록."""
    items = text if isinstance(text, list) else _re.split(r"[\n,]", str(text or ""))
    out = []
    for w in items:
        w = _re.sub(r"\s+", " ", str(w)).strip()[:40]
        if w and w.lower() not in {x.lower() for x in out}:
            out.append(w)
    return out[:MAX_BANNED]


def banned_keywords(cfg=None) -> list:
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:
            return []
    return parse_banned(cfg.get("banned_keywords") or [])


def find_banned(text, words) -> str:
    """text 안에 들어 있는 첫 금지 키워드 (띄어쓰기·대소문자 무시). 없으면 ''."""
    if not words or not text:
        return ""
    flat = _re.sub(r"\s+", "", str(text)).lower()
    for w in words:
        k = _re.sub(r"\s+", "", w).lower()
        if k and k in flat:
            return w
    return ""


def public_config(cfg):
    """웹 UI로 내보낼 설정 (비밀값 제거, 저장 여부만 표시)."""
    out = {k: v for k, v in cfg.items() if k not in SECRET_KEYS and k != "accounts" and not k.startswith("_")}
    out["accounts"] = [{k: a.get(k, "") for k in ("naver_id", "blog_id")} for a in cfg.get("accounts", [])]
    for k in SECRET_KEYS:
        out[f"has_{k}"] = bool(cfg.get(k))
    return out

def load_history():
    """발행 이력 목록을 반환합니다 (최신순)."""
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def add_history(title: str, url: str, category: str = "", tags: str = "", scheduled_at: str = "", account: str = ""):
    """발행 성공 이력을 추가합니다. 예약 발행은 url 없이 scheduled_at('YYYY-MM-DD HH:MM')을 기록합니다."""
    items = load_history()
    new_item = {
        "id": int(time.time()),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "title": title,
        "url": url,
        "category": category,
        "tags": tags,
        "scheduled_at": scheduled_at,
        "account": account
    }
    items.insert(0, new_item)
    # 최대 100개 유지
    items = items[:100]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    return new_item


def mark_history_like(url: str, done: bool):
    """예약 글 공감 결과 기록: 끝났으면 liked=True, 실패면 like_tries 하나 늘림."""
    items = load_history()
    for i in items:
        if i.get("url") == url:
            if done:
                i["liked"] = True
            else:
                i["like_tries"] = int(i.get("like_tries") or 0) + 1
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def clear_history():
    """발행 이력 전체 삭제 (프로그램 목록만 지움. 블로그에 올라간 글은 그대로)."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump([], f)


def remove_history(url: str = "", title: str = "", scheduled_at: str = ""):
    """예약 취소된 글을 발행 이력에서 지움 (발행 간격·일일 제한 계산에서 빠지게)."""
    items = load_history()
    keep = [i for i in items if not ((url and i.get("url") == url) or
                                     (title and scheduled_at and i.get("title") == title and i.get("scheduled_at") == scheduled_at))]
    if len(keep) != len(items):
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(keep, f, indent=2, ensure_ascii=False)


def interval_conflict(target: datetime, times: list, gap_min: int) -> str:
    """발행 간격 검사. 간격(gap_min분) 안에는 다른 글 1건까지만 허용한다(2건이 붙는 것까지).
    새 글 곁에 이미 1건이 있는데 그 글 곁에도 다른 글이 있으면(3건이 몰림) 막는다. 하루 건수 제한은 없다."""
    if gap_min <= 0:
        return ""
    gap = gap_min * 60
    uniq = sorted(set(times))
    near = [t for t in uniq if abs((target - t).total_seconds()) < gap]
    if not near:
        return ""
    crowded = len(near) >= 2 or any(o != near[0] and abs((near[0] - o).total_seconds()) < gap for o in uniq)
    if crowded:
        return (f"다른 발행({near[0]:%m-%d %H:%M})과 {gap_min}분 이내이고, 그 사이에 이미 다른 글이 붙어 있습니다. "
                f"{gap_min}분 안에는 1건만 더 올릴 수 있으니 시각을 조정해 주세요.")
    return ""


def check_publish_limits(target: datetime, cfg: dict) -> str:
    """
    발행 간격 제한을 검사합니다. 위반 시 사유 문자열, 통과 시 "" 를 반환합니다.
    각 이력의 기준 시각은 예약 시각(scheduled_at)이 있으면 그것, 없으면 실제 발행 시각(time)입니다.
    """
    times = []
    me = cfg.get("naver_id", "")
    legacy = cfg.get("legacy_account", "")
    for item in load_history():
        # 계정별로 따로 셈 (계정 기록이 없는 예전 이력은 처음 쓰던 계정 것)
        if me and (item.get("account") or legacy) != me:
            continue
        raw = item.get("scheduled_at") or item.get("time", "")
        try:
            times.append(datetime.strptime(raw[:16], "%Y-%m-%d %H:%M"))
        except ValueError:
            continue
    return interval_conflict(target, times, int(cfg.get("min_interval_min", 0) or 0))
