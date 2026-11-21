"""오토로그(AutoLog) — AI 블로그 자동 발행 스튜디오 서버 (app.py)

- 외부 웹 프레임워크(Flask/FastAPI) 의존성 없는 Python 표준 라이브러리(http.server) 기반
- 실시간 브라우저/라이브러리 자가 진단 및 원클릭 복구 설치
- 스마트에디터 ONE 포스팅 (문단 타이핑, 썸네일 이미지 첨부, 카테고리 선택, 태그 등록)
- AI 맞춤 원고 생성 (정보형, 후기형, 꿀팁형) & 썸네일 자동 카드뉴스 생성
- 발행 이력(History) 보존 및 링크 바로가기
"""
import os
import random
import re
from datetime import datetime, timedelta
import sys
import json
import time
import logging
import threading
import secrets
import hmac
from http.cookies import SimpleCookie
import subprocess
import shutil
from pathlib import Path
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Windows(Git Bash 등)에서 stdout이 cp949면 이모지 출력 시 UnicodeEncodeError로 서버가 바로 종료됨
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import PORTAL_KO, load_config, save_config, public_config, load_history, add_history, check_publish_limits, PROFILE_DIR, BASE_DIR, IS_FROZEN
import config as config_mod
import licensing
from ai import generate_blog_post
import gemini_client
import updater
from version import APP_VERSION

PORT = 8765          # 실행 시 desktop.pick_port()로 확정 (8765 사용 중이면 빈 포트)
LOG_HISTORY = []
APP_WINDOW = None    # 프로그램 창 모드일 때 desktop.AppWindow

# 크몽판: 로그·상태 문구에서 플랫폼 이름을 '블로그'로 표시 (긴 문구부터 바꿈)
_LOG_TEXT = [
    ("N사 인증 세션 쿠키(NID_AUT, NID_SES)", "로그인 세션"),
    (" (PostView 및 logNo 감시)", ""),
    ("포스팅 URL(PostView)", "포스팅 URL"),
    ("스마트에디터 ONE", "블로그 에디터"),
    ("스마트에디터", "블로그 에디터"),
    ("N사 블로그", "블로그"),
    ("N사 자동화", "블로그 자동화"),
    ("N사 자동완성 API", "키워드 조회"),
    ("N사 황금키워드", "추천 키워드"),
    ("N사 검색", "검색"),
    ("서로이웃", "이웃"),
    ("서이추", "이웃 신청"),
    ("N사 ", "블로그 "),
    ("N사", "블로그"),
    (PORTAL_KO, "포털"),
]

def _blog_text(text):
    if not config_mod.NO_SEARCH_DATA or not isinstance(text, str):
        return text
    for a, b in _LOG_TEXT:
        text = text.replace(a, b)
    return text

def _scrub_texts(obj):
    """응답 안의 msg·message·error 문구만 바꾼다 (글 제목·본문 등 사용자 데이터는 그대로)."""
    if isinstance(obj, dict):
        return {k: (_blog_text(v) if k in ("msg", "message", "error") and isinstance(v, str) else _scrub_texts(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_texts(v) for v in obj]
    return obj


class MemoryLogHandler(logging.Handler):
    def emit(self, record):
        msg = _blog_text(self.format(record))
        LOG_HISTORY.append({"time": time.strftime("%H:%M:%S"), "msg": msg, "level": record.levelname})
        if len(LOG_HISTORY) > 300:
            LOG_HISTORY.pop(0)

logger = logging.getLogger("naver.app")
formatter = logging.Formatter("%(message)s")
mem_handler = MemoryLogHandler()
mem_handler.setFormatter(formatter)

naver_logger = logging.getLogger("naver")
naver_logger.setLevel(logging.INFO)
naver_logger.addHandler(mem_handler)

CURRENT_TASK = {"status": "idle", "message": "대기 중", "url": None}

def check_environment_status():
    """Python 라이브러리 및 Playwright Chromium 브라우저 설치 상태를 검사합니다."""
    status = {
        "playwright_installed": False,
        "chromium_installed": False,
        "requests_installed": False,
        "pillow_installed": False,
        "all_ready": False,
        "error_msg": ""
    }
    try:
        import playwright
        status["playwright_installed"] = True
    except ImportError:
        status["error_msg"] = "playwright 모듈이 설치되어 있지 않습니다."

    try:
        import requests
        status["requests_installed"] = True
    except ImportError:
        pass

    try:
        import PIL
        status["pillow_installed"] = True
    except ImportError:
        pass

    if status["playwright_installed"]:
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            path = pw.chromium.executable_path
            pw.stop()
            if path and os.path.exists(path):
                status["chromium_installed"] = True
            else:
                status["error_msg"] = "Chromium 브라우저 바이너리가 없습니다."
        except Exception as e:
            status["error_msg"] = f"Chromium 검사 오류: {e}"

    status["all_ready"] = status["playwright_installed"] and status["chromium_installed"]
    return status

def test_gemini_api_key(api_key: str) -> dict:
    """Gemini API 키 연결 상태 및 지원 모델을 실시간 진단합니다."""
    if not api_key.strip():
        return {"ok": False, "msg": "API 키가 비어 있습니다."}
    try:
        _, m = gemini_client.generate(api_key, "Ping test, reply OK",
                                      ["gemini-3.5-flash-lite", "gemini-3-flash-preview", "gemini-3.5-flash"],
                                      timeout=12, verbose=False)
        return {"ok": True, "model": m, "msg": f"연결 성공! ({m} 모델 정상 작동)"}
    except gemini_client.GeminiError as e:
        if e.invalid_key:
            return {"ok": False, "msg": "유효하지 않은 API 키입니다. 키 값을 확인해주세요."}
        return {"ok": False, "msg": "Google 서버 일시 지연 또는 모델 과부하 상태입니다."}

def fetch_naver_categories(blog_id: str) -> list:
    """
    N사 모바일 API(CategoryList)를 호출하여 해당 블로그의 전체 카테고리 목록을 수집합니다.
    """
    blog_id = (blog_id or "").strip()
    if not blog_id or not config_mod.BLOG_ID_RE.match(blog_id):
        return []
    url = f"https://m.blog.naver.com/rego/CategoryList.naver?blogId={blog_id}"
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)",
            "Referer": f"https://m.blog.naver.com/{blog_id.strip()}"
        })
        with urllib.request.urlopen(req, timeout=6) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
            if "{" in text:
                text = text[text.find("{"):]
            data = json.loads(text)
            raw_list = data.get("result", {}).get("mylogCategoryList", [])
            categories = []
            names_by_no = {it.get("categoryNo"): (it.get("categoryName") or "").strip() for it in raw_list}
            for item in raw_list:
                if item.get("divisionLine"):
                    continue
                name = item.get("categoryName", "").strip()
                c_no = item.get("categoryNo")
                is_child = item.get("childCategory", False)
                if name and name != "구분선":
                    # 동명 하위 카테고리(Android 등) 구분을 위해 상위 카테고리명을 함께 표시
                    parent = names_by_no.get(item.get("parentCategoryNo"), "")
                    child_label = "\u00a0\u00a0ㄴ " + name + (f" ({parent})" if parent else "")
                    categories.append({
                        "name": name,
                        "display_name": child_label if is_child else name,
                        "no": c_no,
                        "is_child": is_child
                    })
            return categories
    except Exception as e:
        logger.warning(f"카테고리 목록 수집 실패 ({blog_id}): {e}")
        return []

def run_install_task():
    """백그라운드에서 필수 라이브러리 및 Chromium 브라우저를 자동 설치합니다."""
    global CURRENT_TASK
    if IS_FROZEN:
        # 설치형 EXE에는 라이브러리와 Chromium이 이미 포함돼 있음 (sys.executable이 EXE 자신이라 pip 실행 불가)
        logger.info("✔ 설치형 프로그램에는 필요한 라이브러리와 브라우저가 이미 포함되어 있습니다.")
        CURRENT_TASK = {"status": "success", "message": "필수 구성요소 포함됨", "url": None}
        return
    CURRENT_TASK = {"status": "running", "message": "필수 라이브러리 및 브라우저 설치 진행 중...", "url": None}
    logger.info("▶ 필수 컴포넌트 자동 설치를 시작합니다...")

    py_cmd = sys.executable or "python3"
    req_file = BASE_DIR / "requirements.txt"

    try:
        # 1. pip install -r requirements.txt
        logger.info(f"1) Python 패키지 설치 중 ({req_file.name})...")
        cmd1 = [py_cmd, "-m", "pip", "install", "--break-system-packages", "-r", str(req_file)]
        proc1 = subprocess.run(cmd1, capture_output=True, text=True)
        if proc1.returncode != 0:
            # --break-system-packages 없이 재시도
            cmd1_retry = [py_cmd, "-m", "pip", "install", "-r", str(req_file)]
            proc1 = subprocess.run(cmd1_retry, capture_output=True, text=True)

        if proc1.returncode == 0:
            logger.info("✔ Python 라이브러리 설치 완료!")
        else:
            logger.warning(f"pip 설치 경고: {proc1.stderr.strip()[:200]}")

        # 2. playwright install chromium
        logger.info("2) Playwright Chromium 브라우저 다운로드 및 설치 중...")
        cmd2 = [py_cmd, "-m", "playwright", "install", "chromium"]
        proc2 = subprocess.run(cmd2, capture_output=True, text=True)
        if proc2.returncode == 0:
            logger.info("✔ Chromium 브라우저 설치 완료!")
            logger.info("🎉 모든 필수 라이브러리 및 브라우저가 완벽하게 준비되었습니다!")
            CURRENT_TASK = {"status": "success", "message": "필수 환경 설정 완료", "url": None}
        else:
            logger.error(f"❌ Chromium 설치 실패: {proc2.stderr.strip()[:200]}")
            CURRENT_TASK = {"status": "error", "message": "Chromium 설치 실패", "url": None}

    except Exception as e:
        logger.error(f"❌ 설치 중 오류: {e}")
        CURRENT_TASK = {"status": "error", "message": str(e), "url": None}

def run_login_task():
    global CURRENT_TASK
    cfg = load_config()
    naver_id = cfg.get("naver_id")
    naver_pw = cfg.get("naver_pw")
    profile = config_mod.account_profile_dir(cfg)   # 시작 시점 계정으로 고정

    if not naver_id or not naver_pw:
        logger.error("❌ 아이디와 비밀번호를 먼저 입력하고 저장해주세요.")
        CURRENT_TASK = {"status": "error", "message": "계정 정보 누락", "url": None}
        return

    CURRENT_TASK = {"status": "running", "message": "로그인 진행 중...", "url": None}
    logger.info(f"▶ N사 로그인 시작 (계정: {naver_id})")

    try:
        from playwright.sync_api import sync_playwright
        from browser import launch_naver_context, first_page
        from auth import login_naver
    except ImportError as ie:
        logger.error("❌ Playwright 패키지가 설치되어 있지 않습니다.")
        logger.error("상단의 [⚡ 필수 컴포넌트 자동 설치] 버튼을 눌러주세요.")
        CURRENT_TASK = {"status": "error", "message": f"라이브러리 누락: {ie}", "url": None}
        return

    try:
        with sync_playwright() as pw:
            # 로그인 창은 캡차 대응을 위해 항상 화면에 표시
            context = launch_naver_context(pw, headless=False, profile_dir=profile)
            page = first_page(context)

            ok = login_naver(page, naver_id, naver_pw)
            time.sleep(2)
            if ok:
                from auth import detect_own_blog_id
                real = detect_own_blog_id(page)
                if real:
                    if real != (cfg.get("blog_id") or naver_id):
                        logger.info(f"📝 블로그 주소 확인: blog.naver.com/{real} (아이디와 블로그 주소가 달라 자동 설정)")
                    config_mod.set_account_blog(naver_id, real)
            context.close()

            if ok:
                logger.info(f"🎉 N사 로그인 및 세션 저장이 완료되었습니다! (계정: {naver_id})")
                import schedule_queue
                schedule_queue.resume()
                CURRENT_TASK = {"status": "success", "message": "로그인 성공 (세션 유지됨)", "url": None}
            else:
                logger.error("❌ 로그인 실패. 아이디/비밀번호 또는 보안 인증을 확인해주세요.")
                CURRENT_TASK = {"status": "error", "message": "로그인 실패", "url": None}
    except Exception as e:
        logger.error(f"❌ 로그인 중 오류 발생: {e}")
        CURRENT_TASK = {"status": "error", "message": str(e), "url": None}

# ── 예약 목록 처리 (뒤에서 한 건씩) ──
def run_cancel_task(item: dict) -> bool:
    """N사에 예약된 글 삭제 (그 예약의 계정으로). 토큰은 돌려주지 않음."""
    global CURRENT_TASK
    import schedule_queue
    cfg = load_config()
    if item.get("account") and not config_mod.select_account(cfg, item["account"]):
        schedule_queue.mark_failed(item["id"], "계정이 삭제되어 N사 예약을 취소할 수 없습니다. N사 블로그에서 직접 삭제해 주세요.")
        return False
    title = item.get("posted_title") or item["title"]
    when = datetime.fromisoformat(item["schedule_at"])
    CURRENT_TASK = {"status": "running", "message": f"예약 취소 중: {title}", "url": None}
    logger.info(f"▶ N사 예약 취소: '{title}' ({when:%m-%d %H:%M}, 계정 {cfg.get('naver_id')})")
    try:
        from playwright.sync_api import sync_playwright
        from browser import launch_naver_context, first_page
        from auth import is_logged_in, check_login_status, login_naver
        from rich_publisher import RichSmartEditorPublisher
        with sync_playwright() as pw:
            context = launch_naver_context(pw, headless=bool(cfg.get("headless", False)),
                                           profile_dir=config_mod.account_profile_dir(cfg))
            page = first_page(context)
            if not is_logged_in(context) or not check_login_status(page):
                if not login_naver(page, cfg.get("naver_id"), cfg.get("naver_pw")):
                    context.close()
                    raise RuntimeError("N사 로그인 실패")
            ok = RichSmartEditorPublisher(page, blog_id=cfg.get("blog_id") or cfg.get("naver_id")).cancel_scheduled(title, when)
            context.close()
        if not ok:
            raise RuntimeError("N사 예약 목록에서 삭제를 확인하지 못했습니다.")
        schedule_queue.mark_canceled(item["id"])
        config_mod.remove_history(url=item.get("url", ""), title=title, scheduled_at=when.strftime("%Y-%m-%d %H:%M"))
        logger.info("✔ 예약 취소 완료 (사용한 100토큰은 돌려받지 않습니다)")
        CURRENT_TASK = {"status": "success", "message": "예약 취소 완료", "url": None}
        return True
    except Exception as e:
        logger.error(f"❌ 예약 취소 실패: {e}")
        schedule_queue.mark_failed(item["id"], f"예약 취소 실패: {e} — N사 블로그에서 직접 확인해 주세요.")
        CURRENT_TASK = {"status": "error", "message": f"예약 취소 실패: {e}", "url": None}
        return False


QUEUE_UPLOAD_GAP = (360, 720)   # 예약 글을 에디터에 연달아 올릴 때 사이 간격(초, 6~12분) — 몰아 올리면 보호조치 위험
_last_upload_at = 0.0
_next_upload_gap = 0.0
QUEUE_FAIL_LIMIT = 3     # 연속으로 이만큼 실패하면 멈춘다 (같은 원인으로 남은 글까지 실패시키지 않게)
_queue_fails = 0
_batch_topics = {}       # (묶음, 키워드) → 아직 쓰지 않은 주제 (재시작하면 다시 받아 옴)
AUTO_STYLES = ("info", "tips", "compare", "howto", "qna", "checklist")   # 체험 없이 쓸 수 있는 글 구성만


def _cfg_for(account: str) -> dict:
    cfg = load_config()
    if account:
        config_mod.select_account(cfg, account)
    return cfg


def all_used_titles() -> list:
    """지금까지 발행했거나 예약해 둔 모든 글 제목 (최신순). 주제 추천에서 같은 주제를 빼는 데 쓴다."""
    import schedule_queue
    titles = [h.get("title", "") for h in load_history()]
    try:
        titles += schedule_queue.all_titles()
    except Exception as e:
        logger.warning(f"예약 목록 제목 읽기 실패: {e}")
    return [t for t in dict.fromkeys(titles) if t and t.strip()]


def batch_topics(picks: list, api_key: str) -> list:
    """picks(글마다 쓸 키워드)와 같은 순서의 주제 목록. 글 수만큼 뽑지 못하면 ValueError (키워드를 제목으로 쓰지 않는다)."""
    import topic_service
    words = config_mod.banned_keywords()
    for kw in dict.fromkeys(picks):
        hit = config_mod.find_banned(kw, words)
        if hit:
            raise ValueError(f"키워드 '{kw}'에 금지 키워드 '{hit}'이(가) 들어 있습니다. 빼고 다시 시도해 주세요. (예약은 추가되지 않았고 토큰도 차감되지 않았습니다)")
    used = all_used_titles()
    need = {kw: picks.count(kw) for kw in dict.fromkeys(picks)}

    def fetch(kw):
        try:
            return kw, topic_service.topics_for_batch(kw, api_key, need[kw], used)
        except Exception as e:
            logger.warning(f"'{kw}' 제목 뽑기 실패: {e}")
            return kw, []

    with ThreadPoolExecutor(max_workers=4) as pool:
        found = dict(pool.map(fetch, need))
    short = [kw for kw, n in need.items() if len(found[kw]) < n]
    if short:
        raise ValueError(f"'{short[0]}' 제목을 글 수만큼 뽑지 못했습니다. 잠시 뒤 다시 시도해 주세요. (예약은 추가되지 않았고 토큰도 차감되지 않았습니다)")
    return [found[kw].pop(0) for kw in picks]


def _next_topic(auto: dict, api_key: str):
    """제목 없이 저장된 이전 버전의 자동 예약용: 이 묶음에서 아직 쓰지 않은 주제 하나 (없으면 None → 키워드만으로 작성)."""
    import schedule_queue
    import topic_service
    key = (auto["batch"], auto["keyword"])
    if not _batch_topics.get(key):
        need = schedule_queue.same_keyword_waiting(*key)
        used = schedule_queue.used_titles(auto["batch"]) + all_used_titles()
        try:
            _batch_topics[key] = topic_service.topics_for_batch(auto["keyword"], api_key, max(need, 1), used)
        except Exception as e:
            logger.warning(f"주제 추천 실패 ({e}) — 키워드로 바로 작성합니다.")
            _batch_topics[key] = []
    return _batch_topics[key].pop(0) if _batch_topics[key] else None


def clean_draft(title: str, contents: list, tags: list) -> tuple:
    """AI 원고 정리: 서식 기호 제거, 본문의 해시태그 줄 제거 (태그는 따로), 끝의 빈 줄 제거."""
    from publisher import clean_markdown_text, normalize_naver_tags
    from text_style import is_hashtag_line
    from shopping_service import link_from_marker
    lines = [l.strip() if link_from_marker(l) else clean_markdown_text(l) for l in contents if not is_hashtag_line(l)]
    while lines and not lines[-1].strip():
        lines.pop()
    return clean_markdown_text(title), lines, normalize_naver_tags(tags)


def quota_resume_time(now: datetime = None) -> datetime:
    """Gemini 하루 사용량은 태평양 자정(한국 16시 또는 17시)에 초기화 → 다음 한국 17:05 (PC 시계 기준)."""
    now = now or datetime.now()
    t = now.replace(hour=17, minute=5, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def connect_write(cfg: dict, api_key: str, keyword: str = "", product_id=None, memo: str = "",
                  user_notes: str = "", extra_photos=(), require_ai: bool = False, main_keyword: str = "", item=None):
    """쇼핑커넥트 상품 글: 계정 브라우저로 상품 선택·링크 발급·상품 페이지 캡처(3장 이상) → 원고 작성 → 사진 자리 배치.
    반환 (제목, 본문 줄, 태그, 캡처 사진 [{id, desc, thumb}], 상품 정보). 브라우저를 쓰므로 TASK_LOCK 안에서 호출."""
    import shopping_connect
    import user_media
    profile = config_mod.account_profile_dir(cfg)
    blog_id = cfg.get("blog_id") or cfg.get("naver_id")
    prod, shots = shopping_connect.prepare_product(profile, blog_id, api_key, keyword=keyword, product_id=product_id, item=item)
    if memo:
        prod["memo"] = memo
    photos = list(extra_photos) + [(p["id"], user_media.photo_path(p["id"])) for p in shots]
    descs = [user_media.describe_photo(pid, api_key) for pid, _ in extra_photos] + [p["desc"] for p in shots]
    main_kw = shopping_connect.short_keyword(prod["name"], main_keyword or (keyword if not product_id else ""))
    title, contents, tags = generate_blog_post(main_kw, style="shopping", api_key=api_key, user_notes=user_notes,
                                               photo_descs=descs, product=prod, require_ai=require_ai)
    title, contents, tags = clean_draft(title, contents, tags)
    contents = place_user_photos(contents, photos, api_key)
    return title, contents, tags, shots, prod


def _lock_account_browser(account: str = ""):
    """글쓰기 화면에서 계정 브라우저를 쓰기 전: 다른 작업·같은 계정 이웃 순회가 없을 때만 잠금을 잡는다. 실패하면 이유 문자열."""
    try:
        import neighbor_service
        nb = neighbor_service.NeighborBot.get_instance()
        if nb.state in ("running", "paused") and nb.current_profile_dir:
            cfg = config_mod.load_config()
            target = config_mod.account_profile_dir(cfg, account or cfg.get("active_account") or "")
            if target.resolve() == nb.current_profile_dir.resolve():
                return "이 계정으로 이웃 순회가 실행 중입니다. 이웃 순회를 멈춘 뒤 다시 시도해 주세요."
    except Exception:
        pass
    if not TASK_LOCK.acquire(timeout=5):
        return "다른 작업(발행·로그인·예약 등록)이 진행 중입니다. 잠시 후 다시 시도해 주세요."
    return ""


def write_auto_item(item: dict) -> bool:
    """자동 예약 항목의 원고 작성. 성공하면 True."""
    global CURRENT_TASK
    import schedule_queue
    auto = item["auto"]
    keyword = auto["keyword"]
    schedule_queue.mark_writing(item["id"])
    CURRENT_TASK = {"status": "running", "message": f"자동 예약: '{keyword}' 글 작성 중...", "url": None}
    logger.info(f"🎲 자동 예약 글 작성: '{keyword}' → {item['schedule_at'].replace('T', ' ')} 발행 예정")
    saved_topic = auto.get("topic")
    topic = saved_topic
    try:
        api_key = _cfg_for(item.get("account", "")).get("gemini_api_key", "").strip()
        topic = saved_topic or _next_topic(auto, api_key)
        product = (topic or {}).get("product") if isinstance((topic or {}).get("product"), dict) else None
        connect_photos = None
        if (topic or {}).get("connect"):
            # 쇼핑커넥트 자동: 키워드로 계정 상품을 골라 링크 발급·캡처·글 작성
            cfg_acc = _cfg_for(item.get("account", ""))
            raw = topic.get("connect_item") if isinstance(topic.get("connect_item"), dict) else None
            if raw:
                # 상품 목록에서 골라 예약한 상품: 그 상품으로 링크 발급·캡처·글 작성
                title, contents, tags, shots, prod = connect_write(
                    cfg_acc, api_key, keyword=str(topic.get("search_keyword") or ""), product_id=str(raw.get("affiliateProductId")),
                    main_keyword=str(topic.get("search_keyword") or "")[:30], require_ai=True, item=raw,
                    memo=str(topic.get("memo") or ""))
            else:
                title, contents, tags, shots, prod = connect_write(cfg_acc, api_key, keyword=keyword, require_ai=True)
            connect_photos = [p["id"] for p in shots]
        elif product:
            # 쇼핑커넥트 상품 글: 제목은 AI가 상품 소개형으로 새로 짓는다
            title, contents, tags = generate_blog_post(product["name"][:40], style="shopping", api_key=api_key,
                                                       require_ai=True, product=product)
        else:
            style = (topic or {}).get("style") if (topic or {}).get("style") in AI_STYLES else random.choice(AUTO_STYLES)
            title, contents, tags = generate_blog_post((topic or {}).get("main_keyword") or keyword, style=style,
                                                       api_key=api_key, topic=topic, require_ai=True)
        if saved_topic and not product and connect_photos is None:
            title = saved_topic["title"]     # 목록에 보여 준 제목 그대로 올린다
        title, contents, tags = clean_draft(title, contents, tags)
        schedule_queue.save_written(item["id"], title, "\n".join(contents), ", ".join(tags), (topic or {}).get("title", ""))
        if connect_photos is not None:
            schedule_queue._update(item["id"], photo_ids=connect_photos)
        return True
    except Exception as e:
        if topic and not saved_topic:
            _batch_topics.setdefault((auto["batch"], keyword), []).insert(0, topic)
        if gemini_client.is_daily_quota(e):
            resume = quota_resume_time()
            schedule_queue.write_failed(item["id"], f"{gemini_client.DAILY_QUOTA_MSG} — {resume:%m-%d %H:%M} 이후 자동으로 이어서 작성",
                                        wait_until=resume)
            logger.warning(f"⏸ {gemini_client.DAILY_QUOTA_MSG}. 자동 예약 글 작성은 {resume:%m-%d %H:%M} 이후 자동으로 이어서 진행합니다.")
            CURRENT_TASK = {"status": "error", "message": f"{gemini_client.DAILY_QUOTA_MSG} — {resume:%H:%M} 이후 자동 재개", "url": None}
            return False
        # 판매 끝난 상품처럼 다시 해도 안 되는 실패는 바로 실패 처리 (5분마다 헛되이 다시 시도하지 않게)
        again = schedule_queue.write_failed(item["id"], e, max_tries=1 if "발급할 수 없습니다" in str(e) else 3)
        logger.error(f"❌ 자동 예약 글 작성 실패: {e}" + (" (5분 뒤 다시 시도)" if again else ""))
        CURRENT_TASK = {"status": "error", "message": f"글 작성 실패: {e}", "url": None}
        return False


def run_queue_once() -> bool:
    """예약 목록에서 할 일 하나 처리 (취소 우선, 다음 등록). 처리했으면 True. TASK_LOCK 안에서 호출."""
    global _queue_fails
    import schedule_queue
    item = schedule_queue.next_cancel()
    if item:
        run_cancel_task(item)
        return True
    if not licensing.is_usable() or schedule_queue.paused():
        return False
    item = schedule_queue.next_queued(_cfg_for)
    if not item:
        return False
    # 이웃 순회가 동일한 계정의 프로필을 사용 중인지 검사 (다른 계정이면 대기 없이 동시 진행)
    try:
        import neighbor_service
        nb = neighbor_service.NeighborBot.get_instance()
        if nb.state in ("running", "paused") and nb.current_profile_dir:
            cfg = config_mod.load_config()
            target_acc = item.get("account") or cfg.get("active_account") or ""
            target_profile = config_mod.account_profile_dir(cfg, target_acc)
            if target_profile.resolve() == nb.current_profile_dir.resolve():
                logger.info(f"⏳ 예약 목록 대기: 계정 '{target_acc}'(으)로 이웃 순회가 실행 중입니다. (다른 계정 순회 시에는 동시 발행 가능)")
                return False
    except Exception:
        pass
    if item.get("auto") and not item.get("content"):
        if not write_auto_item(item):
            _queue_fails += 1
            if _queue_fails >= QUEUE_FAIL_LIMIT:
                schedule_queue.pause(f"글 작성이 연속으로 실패해 멈췄습니다: {(CURRENT_TASK or {}).get('message', '')}")
            return True
        return True    # 등록은 다음 차례에 (시각이 밀렸으면 그때 다시 잡음)
    # 직전 등록과 간격 두기 (발행 시각이 30분 안으로 다가온 글은 바로)
    global _last_upload_at, _next_upload_gap
    try:
        soon = (datetime.fromisoformat(item["schedule_at"]) - datetime.now()).total_seconds() < 1800
    except Exception:
        soon = False
    if not soon and time.time() - _last_upload_at < _next_upload_gap:
        return False
    _last_upload_at = time.time()
    _next_upload_gap = random.uniform(*QUEUE_UPLOAD_GAP)
    schedule_queue.mark_registering(item["id"])
    logger.info(f"📋 예약 목록 등록: '{item['title']}' → {item['schedule_at'].replace('T', ' ')} (계정 {item.get('account')})")
    run_publish_task(item["title"], item["content"], item.get("tags", ""), item.get("category", ""),
                     bool(item.get("auto_thumb", True)), None, bool(item.get("public_open", True)), item["schedule_at"],
                     item.get("photo_ids") or [], account=item.get("account", ""), skip_limits=bool(item.get("auto")))
    t = CURRENT_TASK or {}
    if t.get("status") == "success" and t.get("scheduled"):
        schedule_queue.mark_scheduled(item["id"], t.get("url") or "", t.get("title", ""), t.get("when", ""))
        _queue_fails = 0
    elif t.get("need_user"):
        # 사람이 풀어야 하는 문제: 이 글도 남은 글도 그대로 두고 멈춘다
        schedule_queue.mark_queued(item["id"], t.get("message") or "")
        schedule_queue.pause(t.get("message") or "확인이 필요합니다.")
    else:
        schedule_queue.mark_failed(item["id"], t.get("message") or "등록 실패")
        if t.get("unconfirmed"):
            schedule_queue._update(item["id"], unconfirmed=True)
        _queue_fails += 1
        if _queue_fails >= QUEUE_FAIL_LIMIT:
            schedule_queue.pause(f"등록이 연속으로 실패해 멈췄습니다: {t.get('message') or ''}")
    return True


def resume_queue():
    global _queue_fails
    import schedule_queue
    _queue_fails = 0
    schedule_queue.resume()
    logger.info("▶ 예약 작업을 다시 시작합니다.")


def _keep_awake(on: bool):
    """예약 작업이 남아 있는 동안 PC가 절전으로 들어가지 않게 한다 (화면은 꺼져도 됨)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


def start_queue_worker():
    import schedule_queue
    schedule_queue.recover_after_restart()

    def loop():
        while True:
            time.sleep(15)
            _keep_awake(schedule_queue.pending_count() > 0 and not schedule_queue.paused())
            # 한 건마다 잠금을 풀어, 묶음 예약 중에도 직접 발행·로그인을 끼워 넣을 수 있게 한다
            if not TASK_LOCK.acquire(blocking=False):
                continue
            try:
                run_queue_once()
            except Exception as e:
                logger.error(f"예약 목록 처리 오류: {e}")
            finally:
                TASK_LOCK.release()
    threading.Thread(target=loop, daemon=True).start()


def is_busy() -> bool:
    """발행·로그인 작업 중이거나 예약 목록에 처리할 일이 남아 있거나 이웃 순회가 진행 중이면 True."""
    import schedule_queue
    neighbor_busy = False
    try:
        import neighbor_service
        neighbor_busy = neighbor_service.NeighborBot.get_instance().state in ("running", "paused")
    except Exception:
        pass
    return TASK_LOCK.locked() or schedule_queue.pending_count() > 0 or neighbor_busy


def status_payload(cfg: dict) -> dict:
    """화면용 설정 + 계정 목록(계정별 로그인 상태)."""
    from auth import profile_login_state
    accounts = []
    for idx, a in enumerate(cfg.get("accounts", []), 1):
        try:
            logged = profile_login_state(config_mod.account_profile_dir(cfg, a["naver_id"]))
        except Exception:
            logged = False
        accounts.append({
            "naver_id": a["naver_id"],
            "blog_id": a.get("blog_id", ""),
            "slot": a.get("slot", idx),
            "logged_in": logged
        })
    active = next((a for a in accounts if a["naver_id"] == cfg.get("active_account")), None)
    return {
        "config": public_config(cfg),
        "is_logged_in": bool(active and active["logged_in"]),
        "accounts": accounts,
        "active_account": cfg.get("active_account", ""),
        "max_accounts": config_mod.MAX_ACCOUNTS,
    }


def fixed_area_lines(cfg: dict):
    """설정의 상단/하단 고정 문구와 지도 위치 → 퍼블리셔에 넘길 줄 목록 ('[지도]' 줄 = 지도 삽입 위치)."""
    head = [l.rstrip() for l in (cfg.get("header_text") or "").replace("\r\n", "\n").split("\n")]
    foot = [l.rstrip() for l in (cfg.get("footer_text") or "").replace("\r\n", "\n").split("\n")]
    head = head if any(head) else []
    foot = foot if any(foot) else []
    if (cfg.get("map_query") or "").strip():
        if cfg.get("map_position") == "top":
            head = head + ["[지도]"]
        else:
            foot = foot + ["[지도]"]
    return head, foot


def user_photo_list(photo_ids) -> list:
    """화면이 보낸 사진 id 목록 → 실제 있는 사진 [(id, 경로)] (올린 순서 = 사진 번호)."""
    import user_media
    out = []
    for pid in (photo_ids or [])[:user_media.MAX_PHOTOS]:
        path = user_media.photo_path(pid)
        if path:
            out.append((pid, path))
    return out


REVIEW_MAX_PHOTOS = 30
REVIEW_MAX_VIDEOS = 10
REVIEW_MAX_CLIPS = 10


def review_photo_list(photo_ids) -> list:
    """체험단·기자단 글 사진 [(id, 경로)] — 올린 순서 = 원고 순서 (최대 30장)."""
    import user_media
    out = []
    for pid in (photo_ids if isinstance(photo_ids, list) else [])[:REVIEW_MAX_PHOTOS]:
        path = user_media.photo_path(pid)
        if path:
            out.append((pid, path))
    return out


def review_video_map(videos) -> dict:
    """[{id, title}] → {번호: {path, title}} (올린 순서)."""
    import user_media
    out = {}
    for v in (videos if isinstance(videos, list) else [])[:REVIEW_MAX_VIDEOS]:
        if not isinstance(v, dict):
            continue
        path = user_media.video_path(str(v.get("id", "")))
        if path:
            out[len(out) + 1] = {"path": path, "title": str(v.get("title") or "")[:40]}
    return out


def review_clip_map(clips) -> dict:
    """[{id, title}] → {번호: {id, title}} (고른 순서)."""
    out = {}
    for c in (clips if isinstance(clips, list) else [])[:REVIEW_MAX_CLIPS]:
        if isinstance(c, dict) and str(c.get("id", "")).isdigit():
            out[len(out) + 1] = {"id": str(c["id"]), "title": str(c.get("title") or "")[:100]}
    return out


def place_user_photos(lines: list, photos: list, api_key: str) -> list:
    """본문에 표시 없는 사용자 사진을 내용에 맞는 자리에 '[사진:N]' 줄로 배치."""
    import user_media
    descs = {i: user_media.describe_photo(pid, api_key) for i, (pid, _) in enumerate(photos, 1)}
    return user_media.auto_place_photos(lines, len(photos), descs, api_key)


def run_publish_task(*args, **kwargs):
    """발행 작업. 시작 전에 선차감한 토큰은 발행이 성공하지 못하면 환불."""
    holder = {}
    try:
        _run_publish_task(*args, _license=holder, **kwargs)
    finally:
        if holder.get("rid") and (CURRENT_TASK or {}).get("status") != "success":
            licensing.refund(holder["rid"])


def _run_publish_task(title: str, content: str, tags_str: str, category: str = "",
                      auto_thumb: bool = True, custom_images: list = None, public_open: bool = True,
                      schedule_at: str = "", photo_ids: list = None, _license: dict = None, account: str = "",
                      skip_limits: bool = False, review: dict = None):
    global CURRENT_TASK
    cfg = load_config()
    if account and account != cfg.get("active_account"):
        # 예약 목록: 화면에서 고른 계정과 상관없이 그 예약의 계정으로 (설정 파일은 바꾸지 않음)
        if not config_mod.select_account(cfg, account):
            CURRENT_TASK = {"status": "error", "message": f"계정 '{account}'이(가) 삭제되어 발행할 수 없습니다.", "url": None}
            return
    naver_id = cfg.get("naver_id")
    naver_pw = cfg.get("naver_pw")
    blog_id = cfg.get("blog_id") or naver_id
    cat_to_use = category or cfg.get("post_category", "")
    profile = config_mod.account_profile_dir(cfg)   # 발행 도중 계정을 바꿔도 이 계정으로 발행

    if not naver_id:
        logger.error("❌ N사 계정이 설정되지 않았습니다.")
        CURRENT_TASK = {"status": "error", "message": "계정 미설정", "url": None}
        return

    # 예약 시각 파싱(웹 UI datetime-local 'YYYY-MM-DDTHH:MM') 및 발행 간격/일일 제한 검사
    from publisher import SmartEditorPublisher
    schedule_dt = None
    if schedule_at:
        try:
            schedule_dt = SmartEditorPublisher.normalize_schedule_time(datetime.fromisoformat(schedule_at))
        except ValueError:
            CURRENT_TASK = {"status": "error", "message": f"예약 시각 형식 오류: {schedule_at}", "url": None}
            return
        if schedule_dt <= datetime.now():
            CURRENT_TASK = {"status": "error", "message": "예약 시각이 현재보다 이전입니다.", "url": None}
            return
    # 자동 예약은 사용자가 정한 건수·간격으로 시각을 이미 배치했으므로 기본 제한을 다시 적용하지 않음
    limit_msg = "" if skip_limits else check_publish_limits(schedule_dt or datetime.now(), cfg)
    if limit_msg:
        logger.error(f"❌ 발행 제한: {limit_msg}")
        CURRENT_TASK = {"status": "error", "message": limit_msg, "url": None}
        return

    # 라이선스: 이 N사 계정이 라이선스에 등록돼 있어야 하고, 시작 전에 100토큰 선차감 (실패하면 환불)
    try:
        licensing.account_add(naver_id)
        _license["rid"] = licensing.reserve(naver_id)
    except (licensing.LicenseError, ConnectionError) as e:
        logger.error(f"❌ 라이선스: {e}")
        CURRENT_TASK = {"status": "error", "message": str(e), "url": None}
        return

    CURRENT_TASK = {"status": "running", "message": "블로그 포스팅 진행 중...", "url": None}
    logger.info(f"▶ 스마트에디터 ONE 포스팅 시작 (계정: {naver_id}, 블로그: {blog_id})")
    logger.info(f"제목: {title}")
    if cat_to_use:
        logger.info(f"카테고리: {cat_to_use}")

    # 줄바꿈 및 문단 간격 보존 (빈 줄 버리지 않음)
    contents = [line.strip() for line in content.split("\n")]
    
    # 태그 목록 정리 (중복 해시태그 방지: 본문 말단 추가는 퍼블리셔가 단 1회 전담)
    from publisher import clean_markdown_text, normalize_naver_tags
    clean_title = clean_markdown_text(title)
    
    raw_tag_list = tags_str.replace(",", " ").split()
    tags = normalize_naver_tags(raw_tag_list)

    if review:
        # 체험단·기자단 글: 원고 표기 그대로 (자동 사진·인용구·스티커 없이), 사진·영상·클립은 [사진:N] 등 자리에 차례대로
        import review_post
        blocks = review_post.build_blocks(contents, review.get("photos") or {}, review.get("videos") or {},
                                          review.get("clips") or {})
        logger.info(f"📝 체험단·기자단 글: 사진 {len(review.get('photos') or {})}장 · 영상 {len(review.get('videos') or {})}개 · "
                    f"클립 {len(review.get('clips') or {})}개")
        _publish_in_browser(cfg, naver_id, naver_pw, blog_id, cat_to_use, profile, clean_title, contents, tags,
                            tags_str, [], {}, None, public_open, schedule_dt,
                            blocks_override=blocks, review_center=bool(review.get("center")))
        return

    # 이미지 목록 구성 (적정 6장: 대표 1장 + 콜라주 2세트 + 마무리 1장)
    image_paths = []
    if custom_images:
        image_paths.extend([p for p in custom_images if os.path.exists(p)])

    # 사용자가 올린 사진 우선: 본문 '[사진:N]' 자리(없으면 내용에 맞춰 자동)에 넣고, 모자란 장수만 AI 사진으로
    photos = user_photo_list(photo_ids)
    user_photos = {}
    if photos:
        contents = place_user_photos(contents, photos, cfg.get("gemini_api_key", ""))
        user_photos = {i: path for i, (_, path) in enumerate(photos, 1)}
        logger.info(f"✔ 내 사진 {len(photos)}장 우선 배치 (본문 내용에 맞는 자리)")
    need_ai = 6 - len(user_photos) - len(image_paths)

    # AI 사진은 뒤에서 만들면서 그동안 브라우저 실행·로그인·제목 입력을 먼저 진행하고,
    # 에디터는 사진 블록 차례에 그 사진만 기다린다 (원고 만들 때 미리 시작한 작업이 있으면 이어서 사용)
    image_job = None
    import image_service
    if auto_thumb and need_ai > 0:
        try:
            image_job = image_service.take_prefetch(clean_title, need_ai)
            if image_job is not None:
                logger.info(f"⚡ 원고 작성 때 미리 만들기 시작한 사진 사용 ({image_job.ready_count()}/{need_ai}장 준비됨)")
            else:
                first_tag = tags[0] if tags else clean_title.split()[0]
                image_job = image_service.ImageJob(first_tag, clean_title, contents,
                                                   cfg.get("gemini_api_key", ""), need_ai).start()
            image_paths = image_paths + image_job.paths[:need_ai]
        except Exception as e:
            logger.warning(f"이미지 자동 생성 실패: {e}")
            image_job = None
    else:
        image_service.take_prefetch("", 0)  # 쓰지 않는 미리 만들기는 멈춤
    try:
        _publish_in_browser(cfg, naver_id, naver_pw, blog_id, cat_to_use, profile, clean_title, contents, tags,
                            tags_str, image_paths, user_photos, image_job, public_open, schedule_dt)
    finally:
        if image_job is not None:
            image_job.cancel()


PUBLISH_TRIES = 3


def _publish_in_browser(cfg, naver_id, naver_pw, blog_id, cat_to_use, profile, clean_title, contents, tags,
                        tags_str, image_paths, user_photos, image_job, public_open, schedule_dt,
                        blocks_override=None, review_center=False):
    """브라우저를 새로 띄워 발행. 실패하면 새 화면에서 다시 시도한다.
    단, 이미 올라갔을 수 있거나(PublishUnconfirmed) 사람이 풀어야 하는 경우(NeedUserAction)는 다시 시도하지 않는다."""
    global CURRENT_TASK
    try:
        from publisher import NeedUserAction, PublishUnconfirmed
        from playwright.sync_api import sync_playwright  # noqa: F401 (라이브러리 설치 확인)
    except ImportError as ie:
        logger.error("❌ Playwright 패키지가 설치되어 있지 않습니다.")
        logger.error("상단의 [⚡ 필수 컴포넌트 자동 설치] 버튼을 눌러주세요.")
        CURRENT_TASK = {"status": "error", "message": f"라이브러리 누락: {ie}", "url": None}
        return

    for attempt in range(1, PUBLISH_TRIES + 1):
        try:
            _publish_once(cfg, naver_id, naver_pw, blog_id, cat_to_use, profile, clean_title, contents, tags,
                          tags_str, image_paths, user_photos, image_job, public_open, schedule_dt,
                          blocks_override=blocks_override, review_center=review_center)
            return
        except NeedUserAction as e:
            logger.error(f"❌ 직접 확인이 필요합니다: {e}")
            CURRENT_TASK = {"status": "error", "message": str(e), "url": None, "need_user": True}
            return
        except PublishUnconfirmed as e:
            logger.error(f"❌ {e}")
            CURRENT_TASK = {"status": "error", "message": str(e), "url": None, "unconfirmed": True}
            return
        except Exception as e:
            if attempt == PUBLISH_TRIES:
                logger.error(f"❌ 포스팅 중 오류: {e}")
                CURRENT_TASK = {"status": "error", "message": str(e), "url": None}
                return
            logger.warning(f"⚠️ 시도 {attempt}/{PUBLISH_TRIES} 실패: {str(e)[:200]}")
            logger.info("🔁 글쓰기 화면을 새로 열어 다시 시도합니다...")
            CURRENT_TASK = {"status": "running", "message": f"다시 시도하는 중 ({attempt + 1}/{PUBLISH_TRIES})...", "url": None}
            time.sleep(5 * attempt)


def _publish_once(cfg, naver_id, naver_pw, blog_id, cat_to_use, profile, clean_title, contents, tags,
                  tags_str, image_paths, user_photos, image_job, public_open, schedule_dt,
                  blocks_override=None, review_center=False):
    global CURRENT_TASK
    from playwright.sync_api import sync_playwright
    from browser import launch_naver_context, first_page
    from auth import is_logged_in, check_login_status, login_naver, detect_own_blog_id
    from publisher import NeedUserAction
    from rich_publisher import RichSmartEditorPublisher, build_auto_rich_blocks

    with sync_playwright() as pw:
        # 발행은 설정값 따름 (로그인 창은 캡차 대응 위해 항상 표시)
        context = launch_naver_context(pw, headless=bool(cfg.get("headless", False)), profile_dir=profile)
        try:
            page = first_page(context)

            # 1. 로그인 확인 및 필요 시 자동 재로그인
            if not is_logged_in(context) or not check_login_status(page):
                logger.info("세션 갱신을 위해 재로그인을 진행합니다...")
                if not login_naver(page, naver_id, naver_pw):
                    raise NeedUserAction(f"계정 {naver_id} 로그인에 실패했습니다. 화면의 [로그인] 버튼으로 다시 로그인해 주세요.")

            # 1-1. 실제 블로그 주소 확인 (아이디와 블로그 주소가 다른 계정 대응).
            #      사용자가 다른 블로그 ID를 직접 적어 둔 경우(관리하는 다른 블로그)는 그대로 사용
            real = detect_own_blog_id(page)
            configured = (cfg.get("blog_id") or "").strip()
            if real and real != blog_id and (not configured or configured == naver_id):
                logger.info(f"📝 블로그 주소 확인: blog.naver.com/{real} (아이디 {naver_id}와 달라 이 주소로 발행)")
                blog_id = real
                config_mod.set_account_blog(naver_id, real)

            # 2. 리치 인터리브드 블록 트리 자동 구성
            if blocks_override is not None:
                blocks = blocks_override
            else:
                blocks = build_auto_rich_blocks(title=clean_title, lines=contents, image_paths=image_paths,
                                                user_photos=user_photos or None)
                logger.info(f"✔ 상용급 리치 블록 {len(blocks)}개 조립 완료 (인용구 + 2장 콜라주 결합)")

            # 3. 스마트에디터 ONE 리치 포스팅 실행
            publisher = RichSmartEditorPublisher(page, blog_id=blog_id)
            publisher.image_job = image_job
            publisher.review_center = review_center
            # 체험단·기자단 글은 가이드·원고 그대로 (상단·하단 고정 문구를 넣지 않음)
            header_lines, footer_lines = ([], []) if blocks_override is not None else fixed_area_lines(cfg)
            publisher.map_query = (cfg.get("map_query") or "").strip()
            try:
                published_url = publisher.publish_rich_layout(
                    title=clean_title,
                    blocks=blocks,
                    tags=tags,
                    category_name=cat_to_use,
                    public_open=public_open,
                    schedule_at=schedule_dt,
                    header_lines=header_lines,
                    footer_lines=footer_lines
                )
            except Exception:
                publisher._save_diagnostic("publish_error")   # 실패 원인을 나중에 볼 수 있게
                raise
            time.sleep(3)
        finally:
            try:
                context.close()
            except Exception:
                pass

    if image_job is not None:
        logger.info(f"✔ 본문 맥락 일치 고화질 실사 이미지 {image_job.ready_count()}장 사용")

    # 4. 발행 이력 저장
    cat_label = re.sub(r"^\d+_", "", cat_to_use)
    if published_url.startswith("scheduled:"):
        when, _, post_url = published_url.split(":", 1)[1].partition("|")
        add_history(title=clean_title, url=post_url, category=cat_label, tags=tags_str, scheduled_at=when,
                    account=naver_id)
        logger.info(f"🎉 예약 발행 등록 완료! 발행 예정: {when}")
        CURRENT_TASK = {"status": "success", "message": f"예약 발행 등록 완료 ({when})", "url": post_url or None, "scheduled": True,
                        "title": clean_title, "when": when}
        return
    add_history(title=clean_title, url=published_url, category=cat_label, tags=tags_str, account=naver_id)

    logger.info(f"🎉 리치 포스팅 완료! 링크: {published_url}")
    CURRENT_TASK = {"status": "success", "message": "리치 포스팅 완료", "url": published_url}

# 크몽 판매판: 검색 데이터를 쓰지 않으므로 주제 추천 안내 문구를 바꿔서 보낸다
_NO_SEARCH_TEXT = [
    ("🎯 이 키워드로 최고의 주제 추천받기 (AI + 검색 최적화)", "🎯 이 키워드로 글 주제 추천받기 (AI)"),
    ("⏳ 검색 데이터 분석 중... (약 15~30초)", "⏳ AI가 주제를 고르는 중... (약 10~20초)"),
    ("🔎 연관 검색어와 상위 글을 살펴보는 중입니다...", "💡 AI가 이 키워드로 쓸 만한 주제를 고르는 중입니다..."),
    ("'AI가 검색 데이터를 보고 추천' : '검색 데이터 기준 추천 (Gemini 키를 넣으면 AI가 더 정교하게 추천)'",
     "'AI 추천' : '기본 추천 (Gemini 키를 넣으면 AI가 주제에 맞게 추천)'"),
    ("점수 = 검색 수요(자동완성·함께 찾는 검색어) + 낮은 경쟁(제목에 같은 키워드를 쓴 상위 글 비율) + 빈틈(3개월 넘은 상위 글 비율). ",
     "점수 = AI가 본 독자 관심도 · 유용성 · 시기 적합성. "),
    ("// ── 주제 추천 (검색 데이터 + AI) ──", "// ── 주제 추천 (AI) ──"),
    # 플랫폼 이름·서비스 이름도 일반 표현으로 (크몽: 연상 표현 불가)
    ("스마트에디터 ONE 자동 포스팅", "블로그 자동 포스팅"),
    ("본문 내용 (스마트에디터에 문단 서식으로 자동 작성)", "본문 내용 (블로그에 문단 서식으로 자동 작성)"),
    ("🚀 스마트에디터 ONE 즉시 자동 발행", "🚀 블로그에 즉시 자동 발행"),
    ("N사 지도 검색의 첫 번째 결과", "지도 검색의 첫 번째 결과"),
    ("N사 계정", "블로그 계정"),
    ("N사에 ", "블로그에 "),
    ("N사 예약 취소", "블로그 예약 취소"),
    ('placeholder="naver_id"', 'placeholder="아이디"'),
    # 크몽 비승인 사유: 플랫폼 이름·연상어와 우회·회피 표현 제거
    ("최대 100자 N사 엄수", "최대 100자"),
    ("서로이웃(BothBuddy)", "이웃"),
    ("경쟁사/인플루언서", "관심 블로그"),
    ("서로이웃", "이웃"),
    ("서이추", "이웃 신청"),
    ("안티봇 스텔스 적용", "계정 보호 대기시간 적용"),
    ("동일 멘트 반복 스팸 탐지를 완벽히 우회합니다.", "같은 멘트가 반복되지 않도록 다양하게 선택됩니다."),
    (PORTAL_KO + " 블로그 관리자", "블로그 관리자"),
    (PORTAL_KO + " 이웃 한도", "이웃 한도"),
    ("N사 ", "블로그 "),
    ("N사", "블로그"),
    (PORTAL_KO, "포털"),
    # 강조색: 초록(#03C75A) 대신 남색
    ("#03C75A", "#4F46E5"), ("#029f48", "#4338CA"), ("rgba(3, 199, 90,", "rgba(79, 70, 229,"),
]


def _page_html() -> str:
    html = HTML_TEMPLATE
    if config_mod.NO_SEARCH_DATA:
        for a, b in _NO_SEARCH_TEXT:
            html = html.replace(a, b)
    return html


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>오토로그 - AI 블로그 자동 발행 스튜디오</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
  <style>
    :root {
      --naver-green: #03C75A;
      --naver-dark: #029f48;
      --bg-color: #f8fafc;
      --card-bg: #ffffff;
      --text-main: #0f172a;
      --text-muted: #64748b;
      --border: #e2e8f0;
      --accent: #2563eb;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: Pretendard, sans-serif; }
    body { background: var(--bg-color); color: var(--text-main); padding: 24px; }
    .container { max-width: 1140px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }
    header { display: flex; align-items: center; justify-content: space-between; padding-bottom: 8px; border-bottom: 1px solid var(--border); }
    h1 { font-size: 22px; font-weight: 800; color: #0f172a; display: flex; align-items: center; gap: 10px; }
    .badge-clean { background: #e0f2fe; color: #0369a1; padding: 4px 10px; border-radius: 9999px; font-size: 12px; font-weight: 600; }
    
    .env-banner { display: none; background: #fffbeb; border: 1px solid #fde68a; border-radius: 12px; padding: 14px 18px; margin-bottom: 8px; }
    .env-banner.show { display: flex; align-items: center; justify-content: space-between; }
    .env-banner-text { color: #92400e; font-size: 14px; font-weight: 600; display: flex; align-items: center; gap: 8px; }

    .grid { display: grid; grid-template-columns: 370px 1fr; gap: 20px; }
    .card { background: var(--card-bg); border-radius: 14px; padding: 20px; border: 1px solid var(--border); box-shadow: 0 4px 6px -1px rgba(0,0,0,0.03); }
    .card-title { font-size: 15px; font-weight: 700; margin-bottom: 14px; display: flex; align-items: center; justify-content: space-between; }
    .form-group { margin-bottom: 14px; }
    label { display: block; font-size: 13px; font-weight: 600; color: var(--text-muted); margin-bottom: 6px; }
    input[type="text"], input[type="password"], select, textarea {
      width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px; font-size: 14px; outline: none; transition: border-color 0.2s; background: white;
    }
    input:focus, select:focus, textarea:focus { border-color: var(--naver-green); }
    textarea { height: 190px; resize: vertical; line-height: 1.6; }
    
    .checkbox-label { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; color: var(--text-main); cursor: pointer; user-select: none; }
    .checkbox-label input { width: 16px; height: 16px; accent-color: var(--naver-green); }

    .app-version { font-size: 12px; font-weight: 600; color: var(--text-muted); margin-left: 4px; }
    .update-banner { background: #ecfdf5; border: 1px solid #86efac; border-radius: 12px; padding: 14px 16px; margin-bottom: 16px; }
    .update-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
    .update-title { font-size: 15px; font-weight: 700; color: #166534; }
    .update-sub { font-size: 12px; color: #15803d; margin-top: 2px; white-space: pre-line; }
    .update-progress-wrap { margin-top: 12px; }
    .update-progress { height: 12px; background: #d1fae5; border-radius: 999px; overflow: hidden; }
    .update-progress-bar { height: 100%; width: 0%; background: var(--naver-green); transition: width 0.4s; }
    .update-progress-text { font-size: 12px; color: #166534; margin-top: 6px; font-weight: 600; }
    .update-actions { display: flex; gap: 8px; }
    .btn-ghost { background: #fff; color: #334155; border: 1px solid #cbd5e1; }
    .btn-ghost:hover { background: #f1f5f9; }
    .update-notes-label { margin-top: 10px; font-size: 12px; font-weight: 700; color: #166534; }
    .update-notes { margin-top: 4px; padding: 10px 12px; background: #fff; border: 1px solid #bbf7d0; border-radius: 8px;
      font-size: 13px; line-height: 1.6; color: var(--text-main); white-space: pre-line; max-height: 160px; overflow-y: auto; }
    .update-chip { margin-left: 8px; padding: 3px 10px; font-size: 12px; background: #dcfce7; color: #166534; border: 1px solid #86efac; border-radius: 999px; vertical-align: middle; }
    .update-chip:hover { background: #bbf7d0; }
    .update-chip:disabled { background: #f1f5f9; color: #94a3b8; border-color: #e2e8f0; cursor: default; }
    .new-badge { display: inline-block; margin-left: 6px; padding: 1px 7px; font-size: 10.5px; font-weight: 800; letter-spacing: .3px;
                 color: #fff; background: #ef4444; border-radius: 999px; vertical-align: middle; cursor: pointer; }
    .modal-backdrop { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); display: flex; align-items: center; justify-content: center; z-index: 3000; padding: 16px; }
    .modal-card { background: #fff; border-radius: 14px; padding: 22px 24px; width: 100%; max-width: 480px; box-shadow: 0 20px 50px rgba(0,0,0,0.25); }
    .modal-title { font-size: 17px; font-weight: 800; color: var(--text-main); margin-bottom: 12px; }
    .modal-list { margin: 0 0 18px 18px; padding: 0; font-size: 13px; line-height: 1.8; color: #334155; }
    .modal-actions { display: flex; justify-content: flex-end; gap: 8px; }
    .update-banner.is-error { background: #fef2f2; border-color: #fca5a5; }
    .update-banner.is-error .update-title, .update-banner.is-error .update-sub, .update-banner.is-error .update-progress-text { color: #b91c1c; }

    .fixed-text { width: 100%; min-height: 70px; resize: vertical; font-size: 13px; line-height: 1.6; }
    .fixed-hint { font-size: 12px; color: var(--text-muted); margin-top: 6px; }

    .schedule-box { background: #fff; border: 1px solid #cbd5e1; border-radius: 10px; padding: 12px; display: flex; flex-direction: column; gap: 10px; }
    .schedule-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; font-size: 14px; font-weight: 700; color: var(--text-main); }
    .schedule-hint { font-size: 12px; font-weight: 500; color: var(--text-muted); }
    .schedule-row { display: flex; gap: 8px; align-items: stretch; }
    .schedule-input { flex: 1; min-width: 0; height: 44px; padding: 0 12px; font-size: 15px; font-weight: 600; color: var(--text-main);
      border: 2px solid #94a3b8; border-radius: 8px; background: #fff; cursor: pointer; }
    .schedule-input:hover { border-color: var(--naver-green); }
    .schedule-input:focus { outline: none; border-color: var(--naver-green); box-shadow: 0 0 0 3px rgba(3, 199, 90, 0.2); }
    .schedule-input::-webkit-calendar-picker-indicator { width: 22px; height: 22px; cursor: pointer; }
    .schedule-clear { padding: 0 14px; border: 1px solid #fca5a5; background: #fff; color: #b91c1c; font-size: 13px; }
    .schedule-clear:hover { background: #fef2f2; }
    .schedule-presets { display: flex; flex-wrap: wrap; gap: 6px; }
    .queue-add { margin-top: 10px; width: 100%; padding: 10px; font-size: 13.5px; font-weight: 700; border: 1px solid #93c5fd; background: #eff6ff; color: #1d4ed8; border-radius: 8px; cursor: pointer; }
    .queue-add:hover { background: #dbeafe; }
    .token-warn { margin-top: 8px; font-size: 12px; line-height: 1.6; color: #92400e; background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 8px 10px; }
    .sch-table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .sch-table th { text-align: left; font-size: 12px; color: var(--text-muted); font-weight: 700; padding: 6px 8px; border-bottom: 1px solid var(--border); }
    .sch-table td { padding: 8px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }
    .sch-badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11.5px; font-weight: 700; white-space: nowrap; }
    .sch-btn { padding: 4px 10px; font-size: 12px; border-radius: 6px; border: 1px solid var(--border); background: #fff; cursor: pointer; white-space: nowrap; }
    .sch-btn.danger { color: #b91c1c; border-color: #fecaca; }
    .batch-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .batch-grid label { display: block; font-size: 12.5px; font-weight: 700; color: #334155; margin-bottom: 4px; }
    .batch-grid input, .batch-grid select { width: 100%; height: 38px; padding: 0 10px; border: 1px solid #94a3b8; border-radius: 8px; font-size: 14px; background: #fff; box-sizing: border-box; }
    .batch-slots { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0; }
    .batch-slot { padding: 4px 9px; border-radius: 999px; background: #eff6ff; color: #1d4ed8; font-size: 12.5px; font-weight: 700; }
    .batch-note { padding: 10px 12px; border-radius: 8px; font-size: 13px; font-weight: 600; line-height: 1.6; margin-top: 10px; }
    .batch-note.run { background: #eff6ff; color: #1e40af; }
    .batch-note.done { background: #ecfdf5; color: #065f46; }
    .batch-note.stop { background: #fef2f2; color: #991b1b; }
    .sch-err { font-size: 11.5px; color: #b91c1c; margin-top: 2px; }
    .schedule-presets button { padding: 8px 12px; font-size: 13px; background: #f1f5f9; color: #334155; border: 1px solid #cbd5e1; }
    .schedule-presets button:hover { background: #e2e8f0; border-color: var(--naver-green); color: var(--naver-dark); }
    .schedule-status { padding: 10px 12px; border-radius: 8px; font-size: 13px; font-weight: 600; }

    button {
      cursor: pointer; border: none; border-radius: 8px; font-weight: 600; padding: 10px 16px; font-size: 14px; transition: all 0.2s;
    }
    .btn-green { background: var(--naver-green); color: white; }
    .btn-green:hover { background: var(--naver-dark); }
    .btn-blue { background: #3b82f6; color: white; }
    .btn-blue:hover { background: #2563eb; }
    .btn-amber { background: #f59e0b; color: white; }
    .btn-amber:hover { background: #d97706; }
    .btn-outline { background: white; border: 1px solid var(--border); color: var(--text-main); }
    .btn-outline:hover { background: #f8fafc; border-color: #cbd5e1; }
    .account-tabs { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px; }
    .account-tab { display:inline-flex; align-items:center; gap:6px; padding:6px 10px; border-radius:999px; border:1px solid var(--border);
                   background:#fff; font-size:12.5px; font-weight:700; color:#334155; cursor:pointer; max-width:100%; }
    .account-tab:hover { border-color:#94a3b8; }
    .account-tab.active { background:#ecfdf5; border-color:#03C75A; color:#065f46; box-shadow:0 0 0 2px rgba(3,199,90,.15); }
    .account-tab .dot { width:8px; height:8px; border-radius:50%; background:#cbd5e1; flex:none; }
    .account-tab .dot.on { background:#03C75A; }
    .account-tab.add { border-style:dashed; color:#2563eb; font-weight:700; }
    .account-tab.add.active { background:#eff6ff; border-color:#2563eb; color:#1d4ed8; box-shadow:none; }
    .account-hint { font-size:11.5px; color:#475569; background:#f8fafc; border:1px solid var(--border); border-radius:8px; padding:8px 10px; margin-bottom:12px; line-height:1.5; }
    .account-actions { display:flex; gap:8px; margin-bottom:14px; }
    .link-danger { background:none; border:none; color:#b91c1c; font-size:12px; font-weight:600; cursor:pointer; padding:0; text-decoration:underline; }
    /* 라이선스 시작 화면 */
    .license-screen { position: fixed; inset: 0; z-index: 2000; display: flex; align-items: center; justify-content: center;
                      background: radial-gradient(1200px 600px at 50% -10%, #dcfce7 0%, #f8fafc 55%) ; padding: 16px; }
    .license-card { width: 100%; max-width: 560px; background: #fff; border: 1px solid var(--border); border-radius: 20px;
                    box-shadow: 0 20px 60px rgba(15,23,42,.10); padding: 40px 36px 32px; text-align: center; }
    .license-logo { font-size: 26px; font-weight: 800; color: #0f172a; letter-spacing: -0.3px; }
    .license-logo .n { display: inline-flex; width: 40px; height: 40px; border-radius: 10px; background: var(--naver-green); color: #fff;
                       align-items: center; justify-content: center; margin-right: 8px; vertical-align: -6px; }
    .license-sub { margin: 10px 0 28px; color: var(--text-muted); font-size: 14px; line-height: 1.6; }
    .lic-boxes { display: flex; align-items: center; justify-content: center; gap: 8px; }
    .lic-boxes .dash { color: #cbd5e1; font-weight: 700; }
    .lic-box { width: 96px !important; height: 54px; text-align: center; font-size: 22px !important; font-weight: 800; letter-spacing: 3px;
               font-family: Consolas, 'D2Coding', monospace !important; text-transform: uppercase; border: 2px solid #cbd5e1 !important;
               border-radius: 12px !important; background: #f8fafc; color: #0f172a; padding: 0 !important; }
    .lic-box:focus { border-color: var(--naver-green) !important; background: #fff; outline: none; box-shadow: 0 0 0 3px rgba(3,199,90,.15); }
    .lic-box.err { border-color: #ef4444 !important; }
    .lic-login { margin-top: 24px; width: 100%; padding: 14px; font-size: 16px; }
    .lic-msg { min-height: 20px; margin-top: 12px; font-size: 13px; font-weight: 600; }
    .lic-msg.err { color: #b91c1c; }
    .lic-msg.ok { color: #047857; }
    .lic-foot { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--border); font-size: 12px; color: var(--text-muted); line-height: 1.7; }
    .lic-foot b { color: #334155; }
    .lic-update { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 20px; padding: 10px 14px;
                  border-radius: 10px; background: #eff6ff; border: 1px solid #bfdbfe; color: #1e40af; font-size: 13px; font-weight: 700; text-align: left; }
    .lic-update button { flex: none; padding: 7px 12px; font-size: 12.5px; }
    .license-chip { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 999px; border: 1px solid #bbf7d0;
                    background: #f0fdf4; color: #065f46; font-size: 12.5px; font-weight: 700; cursor: pointer; margin-right: 8px; }
    .license-chip.low { background: #fff7ed; border-color: #fed7aa; color: #9a3412; }
    .header-right { display: flex; align-items: center; }
    .close-card { max-width: 420px; }
    .close-busy { font-size: 13px; font-weight: 700; color: #b45309; background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 8px 10px; margin-bottom: 12px; }
    .close-opt { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; width: 100%; text-align: left; padding: 12px 14px; margin-top: 8px;
                 border: 1px solid var(--border); border-radius: 10px; background: #fff; cursor: pointer; font-size: 14px; color: var(--text-main); }
    .close-opt span { font-size: 12px; color: var(--text-muted); }
    .close-opt:hover:not(:disabled) { border-color: #94a3b8; background: #f8fafc; }
    .close-opt.danger b { color: #b91c1c; }
    .close-opt:disabled { opacity: .45; cursor: default; }
    .topic-btn { margin-top:8px; padding:9px; font-size:13px; font-weight:800; color:#7c3aed; border-color:#ddd6fe; background:#faf5ff; }
    .topic-btn:hover:not(:disabled) { background:#f3e8ff; border-color:#c4b5fd; }
    .topic-btn:disabled { opacity:.6; cursor:default; }
    .topic-panel { margin-top:10px; border:1px solid #e9d5ff; background:#fdfaff; border-radius:10px; padding:10px; }
    .topic-head { display:flex; justify-content:space-between; align-items:center; font-size:12px; color:#6b21a8; font-weight:800; margin-bottom:8px; gap:8px; }
    .topic-head .muted { font-weight:600; color:#64748b; }
    .topic-card { background:#fff; border:1px solid var(--border); border-radius:10px; padding:10px 11px; margin-bottom:8px; cursor:pointer; transition:border-color .15s, box-shadow .15s; }
    .topic-card:hover { border-color:#a78bfa; box-shadow:0 2px 8px rgba(124,58,237,.10); }
    .topic-card.sel { border-color:#7c3aed; box-shadow:0 0 0 2px #ddd6fe; }
    .topic-top { display:flex; gap:9px; align-items:flex-start; }
    .topic-score { flex:0 0 auto; width:42px; height:42px; border-radius:10px; display:flex; flex-direction:column; align-items:center; justify-content:center;
                   font-size:17px; font-weight:900; color:#fff; background:#7c3aed; line-height:1; }
    .topic-score small { font-size:9px; font-weight:700; margin-top:2px; opacity:.9; }
    .topic-score.mid { background:#8b5cf6; } .topic-score.low { background:#a8a2c0; }
    .topic-title { font-size:13.5px; font-weight:800; color:var(--text-main); line-height:1.4; }
    .topic-sub { font-size:11.5px; color:#475569; margin-top:3px; line-height:1.45; }
    .topic-chips { display:flex; flex-wrap:wrap; gap:4px; margin-top:6px; }
    .tchip { font-size:10.5px; font-weight:700; padding:2px 7px; border-radius:999px; background:#f1f5f9; color:#475569; }
    .tchip.kw { background:#ede9fe; color:#6d28d9; } .tchip.good { background:#dcfce7; color:#166534; } .tchip.bad { background:#fee2e2; color:#b91c1c; }
    .topic-angle { font-size:11.5px; color:#334155; margin-top:6px; background:#f8fafc; border-radius:6px; padding:5px 7px; line-height:1.45; }
    .shop-pager { display:flex; gap:4px; justify-content:center; flex-wrap:wrap; padding:8px; border-top:1px solid #f1f5f9; }
    .shop-pager button { min-width:30px; padding:4px 8px; font-size:12px; border:1px solid #e2e8f0; background:#fff; border-radius:6px; cursor:pointer; color:#334155; }
    .shop-pager button.on { background:#16a34a; border-color:#16a34a; color:#fff; font-weight:800; }
    .shop-pager button:disabled:not(.on) { opacity:.4; cursor:default; }
    .batch-picked { font-size:12.5px; background:#f5f3ff; border:1px solid #ddd6fe; color:#5b21b6; border-radius:8px; padding:9px 10px; margin-bottom:10px; line-height:1.6; }
    .batch-picked ol { margin:4px 0 0 18px; padding:0; color:#334155; }
    .batch-picked li { display:flex; gap:6px; align-items:center; }
    .batch-picked li span { flex:1; min-width:0; }
    .batch-picked li button { border:none; background:none; color:#7c3aed; font-weight:800; cursor:pointer; }
    .topic-pick { display:flex; align-items:center; gap:5px; font-size:11.5px; font-weight:700; color:#7c3aed; margin-top:6px; cursor:pointer; }
    .topic-pick input { width:auto; margin:0; }
    .topic-bar { position:sticky; bottom:0; margin-top:8px; padding:8px; background:#fdfaff; border-top:1px solid #e9d5ff; display:flex; gap:8px; align-items:center; font-size:12px; }
    .topic-selected { margin-top:8px; font-size:12px; background:#f5f3ff; border:1px solid #ddd6fe; color:#5b21b6; border-radius:8px; padding:7px 9px; display:flex; gap:8px; align-items:center; }
    .topic-selected b { flex:1; min-width:0; }
    .topic-selected button { border:none; background:none; color:#7c3aed; font-weight:800; cursor:pointer; font-size:13px; }
    .user-box { background:#f0fdf4; border:1px solid #bbf7d0; border-radius:10px; padding:12px; margin-bottom:14px; }
    .user-box-title { font-size:13px; font-weight:800; color:#166534; margin-bottom:4px; }
    .user-box-hint { font-size:11.5px; color:#475569; margin-bottom:10px; line-height:1.5; }
    .user-box textarea { height:110px; font-size:13px; }
    .file-btn { padding:5px 10px; font-size:12px; border-radius:8px; cursor:pointer; }
    .photo-drop { border:2px dashed #86efac; border-radius:10px; background:#fff; padding:14px 10px; text-align:center;
                  font-size:12.5px; color:#166534; font-weight:600; cursor:pointer; }
    .photo-drop.drag { background:#dcfce7; border-color:#22c55e; }
    .photo-list { display:grid; grid-template-columns:repeat(4, 1fr); gap:6px; margin-top:8px; }
    .photo-item { position:relative; border-radius:8px; overflow:hidden; border:1px solid var(--border); background:#f1f5f9; aspect-ratio:1/1; }
    .photo-item img { width:100%; height:100%; object-fit:cover; display:block; }
    .photo-num { position:absolute; left:4px; top:4px; background:#166534; color:#fff; font-size:11px; font-weight:800;
                 border-radius:9999px; min-width:20px; height:20px; line-height:20px; text-align:center; padding:0 5px; }
    .photo-del { position:absolute; right:3px; top:3px; width:20px; height:20px; border:none; border-radius:9999px;
                 background:rgba(15,23,42,.7); color:#fff; font-size:12px; line-height:20px; cursor:pointer; padding:0; }
    .photo-state { position:absolute; left:0; right:0; bottom:0; background:rgba(15,23,42,.65); color:#fff; font-size:10px;
                   padding:2px 4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .photo-state.err { background:rgba(185,28,28,.85); }
    .btn-block { width: 100%; }
    .btn-group { display: flex; gap: 8px; }

    .console { background: #0f172a; color: #94a3b8; border-radius: 10px; padding: 14px; font-family: monospace; font-size: 12px; height: 160px; overflow-y: auto; line-height: 1.5; }
    .log-line { margin-bottom: 3px; word-break: break-all; }
    .log-INFO { color: #f1f5f9; }
    .log-WARNING { color: #fbbf24; }
    .log-ERROR { color: #f87171; font-weight: bold; }

    .status-pill { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; padding: 4px 12px; border-radius: 9999px; background: #fee2e2; color: #b91c1c; font-weight: 600; }
    .status-pill.logged-in { background: #dcfce7; color: #15803d; }
    .status-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }

    .post-result { display: none; margin-top: 14px; padding: 12px; background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 8px; color: #065f46; font-size: 13px; }
    .post-result a { color: #059669; font-weight: bold; text-decoration: underline; margin-left: 6px; }

    .history-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 10px; }
    .history-table th, .history-table td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); }
    .history-table th { background: #f8fafc; color: var(--text-muted); font-weight: 600; }
    .history-table tr:hover { background: #f1f5f9; }
    .history-table a { color: var(--naver-green); text-decoration: none; font-weight: 600; }
    .history-table a:hover { text-decoration: underline; }

    /* 🤝 N사 자동 이웃 순회 스타일 */
    .neighbor-header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; }
    .neighbor-status-badge { display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px; border-radius: 9999px; font-size: 13px; font-weight: 700; }
    .neighbor-status-idle { background: #f1f5f9; color: #475569; }
    .neighbor-status-running { background: #dcfce7; color: #166534; border: 1px solid #86efac; }
    .neighbor-status-paused { background: #fef9c3; color: #854d0e; border: 1px solid #fde047; }
    .neighbor-status-stopping { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }
    .neighbor-stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 16px; }
    .neighbor-stat-card { background: #f8fafc; border: 1px solid var(--border); border-radius: 10px; padding: 10px 14px; text-align: center; }
    .neighbor-stat-val { font-size: 18px; font-weight: 800; color: #0f172a; margin-top: 2px; }
    .neighbor-stat-lbl { font-size: 11.5px; font-weight: 600; color: var(--text-muted); }
    .neighbor-mode-btn { flex: 1; min-width: 140px; padding: 10px 12px; border: 1.5px solid var(--border); border-radius: 8px; background: #fff; font-size: 13px; font-weight: 600; color: var(--text-main); cursor: pointer; text-align: center; transition: all 0.2s; }
    .neighbor-mode-btn.active { border-color: var(--naver-green); background: #f0fdf4; color: var(--naver-dark); font-weight: 700; box-shadow: 0 0 0 2px rgba(3,199,90,0.15); }
    .neighbor-ctrl-btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 10px 18px; font-size: 14px; font-weight: 700; border-radius: 8px; border: none; cursor: pointer; transition: all 0.2s; }
    .neighbor-ctrl-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .neighbor-log-box { height: 160px; overflow-y: auto; background: #0f172a; color: #f8fafc; font-family: monospace; font-size: 12px; padding: 12px 14px; border-radius: 8px; line-height: 1.6; }
    .neighbor-log-line { margin-bottom: 3px; word-break: break-all; }
    .neighbor-log-time { color: #94a3b8; margin-right: 6px; }
    .neighbor-log-info { color: #e2e8f0; }
    .neighbor-log-warning { color: #fde047; }
    .neighbor-log-error { color: #f87171; }
    .pulse-dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; display: inline-block; animation: neighborPulse 1.4s infinite; }
    @keyframes neighborPulse { 0% { opacity: 0.3; transform: scale(0.9); } 50% { opacity: 1; transform: scale(1.15); } 100% { opacity: 0.3; transform: scale(0.9); } }
    .badge-act { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 700; margin-right: 4px; }
    .badge-sympathy { background: #fee2e2; color: #dc2626; }
    .badge-comment { background: #dbeafe; color: #2563eb; }
    .badge-buddy { background: #dcfce7; color: #166534; }

    /* ── 상단 대메뉴 탭 네비게이션 ── */
    .main-nav-tabs {
      display: flex;
      gap: 8px;
      border-bottom: 2px solid var(--border);
      margin-bottom: 4px;
      padding-bottom: 0;
    }
    .main-nav-tab {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 12px 24px;
      font-size: 15px;
      font-weight: 700;
      color: var(--text-muted);
      background: transparent;
      border: none;
      border-bottom: 3px solid transparent;
      cursor: pointer;
      margin-bottom: -2px;
      transition: all 0.2s ease;
      position: relative;
    }
    .main-nav-tab:hover {
      color: var(--text-main);
      background: #f1f5f9;
      border-radius: 8px 8px 0 0;
    }
    .main-nav-tab.active {
      color: var(--naver-green);
      border-bottom-color: var(--naver-green);
      background: #f0fdf4;
      border-radius: 8px 8px 0 0;
    }
    .tab-icon {
      font-size: 18px;
    }
    .tab-pulse-indicator {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #22c55e;
      display: inline-block;
      margin-left: 2px;
      animation: neighborPulse 1.4s infinite;
    }
    .rv-mode { display:flex; gap:6px; margin:10px 0 14px; }
    .rv-mode-btn { flex:1; padding:9px 6px; font-size:13px; font-weight:700; border-radius:8px; border:1px solid var(--border); background:white; color:var(--text-main); cursor:pointer; }
    .rv-mode-btn.active { background:#ecfdf5; border-color:#10b981; color:#065f46; }
    .rv-media-list { display:flex; flex-direction:column; gap:6px; margin-top:8px; max-height:340px; overflow:auto; }
    .rv-item { display:flex; align-items:center; gap:6px; padding:5px 6px; border:1px solid #f1f5f9; border-radius:8px; font-size:12px; margin:0; font-weight:400; }
    .rv-item img { width:42px; height:42px; object-fit:cover; border-radius:6px; flex:0 0 auto; }
    .rv-item .rv-ph { width:42px; text-align:center; font-size:20px; flex:0 0 auto; }
    .rv-item .rv-num { min-width:18px; text-align:center; color:#059669; flex:0 0 auto; }
    .rv-item .rv-name { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; word-break:break-all; }
    .rv-item .rv-name input { margin-top:3px; padding:3px 6px; font-size:12px; }
    .rv-clip-opts { display:flex; flex-wrap:wrap; gap:8px 14px; margin-top:8px; font-size:12.5px; }
    .rv-clip-opts label { display:flex; align-items:center; gap:6px; margin:0; font-weight:600; }
    .rv-clip-opts select { width:auto; padding:4px 8px; font-size:12.5px; }
    .clip-scenes { display:flex; flex-direction:column; gap:6px; margin-top:6px; max-height:420px; overflow:auto; }
    .clip-scene { display:grid; grid-template-columns:46px 1fr auto; gap:6px; align-items:start; padding:6px; border:1px solid #f1f5f9; border-radius:8px; }
    .clip-scene img, .clip-scene .ph { width:46px; height:46px; object-fit:cover; border-radius:6px; background:#f1f5f9; display:flex; align-items:center; justify-content:center; font-size:20px; }
    .clip-scene select { padding:3px 6px; font-size:11.5px; margin-bottom:3px; width:auto; }
    .clip-scene input { padding:4px 8px; font-size:12.5px; margin-top:3px; }
    .clip-scene .btns { display:flex; flex-direction:column; gap:3px; }
    .clip-scene .btns button { padding:2px 7px; font-size:11px; border:1px solid var(--border); background:white; border-radius:6px; cursor:pointer; }
    .rv-item button { flex:0 0 auto; padding:3px 7px; font-size:11.5px; border:1px solid var(--border); background:white; border-radius:6px; cursor:pointer; }
    .rv-item button:disabled { opacity:.35; cursor:default; }
    .rv-toolbar { display:flex; flex-wrap:wrap; gap:4px; margin-bottom:6px; }
    .rv-toolbar button, .rv-toolbar select { padding:4px 8px; font-size:12px; border:1px solid var(--border); background:white; border-radius:6px; cursor:pointer; width:auto; }
    .tab-content {
      display: flex;
      flex-direction: column;
      gap: 20px;
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>
        <span style="color:var(--naver-green)">A</span> 오토로그 <span style="font-size:13px;font-weight:600;color:var(--text-muted)">AutoLog</span>
        <span class="badge-clean">고속 자동 포스팅 & AI 썸네일 엔진</span>
        <span id="app-version" class="app-version"></span>
        <span id="new-badge" class="new-badge" style="display:none;" onclick="openUpdateConfirm()">NEW</span>
        <button type="button" id="update-chip" class="update-chip" disabled onclick="openUpdateConfirm()">최신 버전</button>
      </h1>
      <div class="header-right">
        <button type="button" id="license-chip" class="license-chip" style="display:none;" onclick="licenseMenu()" title="라이선스 정보 / 변경"></button>
        <div id="session-badge" class="status-pill">
          <span class="status-dot"></span> <span id="session-text">세션 확인 중...</span>
        </div>
      </div>
    </header>

    <!-- 시작 화면: 라이선스 키 입력 (맞는 키로 로그인해야 사용 가능) -->
    <div id="license-screen" class="license-screen" style="display:none;">
      <div class="license-card">
        <div id="lic-update" class="lic-update" style="display:none;">
          <span id="lic-update-text">새 버전이 있습니다.</span>
          <button type="button" class="btn-blue" onclick="openUpdateConfirm()">지금 업데이트</button>
        </div>
        <div class="license-logo"><span class="n">A</span>오토로그</div>
        <div class="license-sub">발급받은 <b>라이선스 키 16자리</b>를 입력하고 로그인해 주세요.<br>키를 복사해 첫 칸에 붙여 넣으면 자동으로 나뉘어 들어갑니다.</div>
        <div class="lic-boxes" id="lic-boxes">
          <input class="lic-box" maxlength="4" autocomplete="off" spellcheck="false" aria-label="라이선스 키 1번째 4자리">
          <span class="dash">-</span>
          <input class="lic-box" maxlength="4" autocomplete="off" spellcheck="false" aria-label="라이선스 키 2번째 4자리">
          <span class="dash">-</span>
          <input class="lic-box" maxlength="4" autocomplete="off" spellcheck="false" aria-label="라이선스 키 3번째 4자리">
          <span class="dash">-</span>
          <input class="lic-box" maxlength="4" autocomplete="off" spellcheck="false" aria-label="라이선스 키 4번째 4자리">
        </div>
        <button type="button" id="lic-login" class="btn-green lic-login" onclick="activateLicense()">로그인</button>
        <div id="lic-msg" class="lic-msg"></div>
        <div class="lic-foot">블로그 글 1개 발행 = <b>100토큰</b> · 30일 6,000 / 40일 8,500 / 50일 12,000토큰<br>
          라이선스 하나로 여러 PC에서 사용할 수 있고, N사 계정은 라이선스당 최대 5개까지 등록됩니다.</div>
      </div>
    </div>

    <!-- 업데이트 완료 안내 (업데이트 후 자동 재시작 직후) -->
    <div id="updated-banner" class="update-banner" style="display:none;">
      <div class="update-row">
        <div>
          <div class="update-title" id="updated-title"></div>
          <div class="update-sub" id="updated-sub"></div>
        </div>
        <button type="button" class="btn-green" onclick="ackUpdated()">확인</button>
      </div>
      <div class="update-notes" id="updated-notes" style="display:none;"></div>
    </div>

    <!-- 새 업데이트 알림 (새 버전이 있을 때만, 사용자가 원할 때 업데이트) -->
    <div id="update-banner" class="update-banner" style="display:none;">
      <div class="update-row">
        <div>
          <div class="update-title" id="update-title"></div>
          <div class="update-sub" id="update-sub"></div>
        </div>
        <div class="update-actions" id="update-actions">
          <button type="button" class="btn-ghost" id="update-later" onclick="dismissUpdate()">나중에</button>
          <button type="button" class="btn-green" id="update-btn" onclick="openUpdateConfirm()">지금 업데이트</button>
        </div>
      </div>
      <div class="update-notes-label" id="update-notes-label">변경 내용</div>
      <div class="update-notes" id="update-notes"></div>
      <div id="update-progress-wrap" class="update-progress-wrap" style="display:none;">
        <div class="update-progress"><div id="update-progress-bar" class="update-progress-bar"></div></div>
        <div class="update-progress-text" id="update-progress-text"></div>
      </div>
    </div>

    <!-- 업데이트 시작 확인 -->
    <div id="update-modal" class="modal-backdrop" style="display:none;">
      <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="update-modal-title">
        <div class="modal-title" id="update-modal-title"></div>
        <div class="update-notes-label">변경 내용</div>
        <div class="update-notes" id="update-modal-notes" style="margin-bottom:14px;"></div>
        <ul class="modal-list">
          <li id="update-modal-size">새 버전을 내려받아 설치합니다.</li>
          <li><b>설치하는 동안 프로그램이 잠시 닫혔다가, 설치가 끝나면 자동으로 다시 시작됩니다.</b></li>
          <li>설정·로그인 정보·발행 이력은 그대로 유지됩니다.</li>
          <li>작성 중인 원고가 있다면 미리 복사해 두세요.</li>
        </ul>
        <div class="modal-actions">
          <button type="button" class="btn-ghost" onclick="closeUpdateConfirm(); dismissUpdate();">나중에</button>
          <button type="button" class="btn-green" onclick="startUpdate()">지금 업데이트</button>
        </div>
      </div>
    </div>

    <!-- 자동 예약 완료 안내 -->
    <div id="batch-done-modal" class="modal-backdrop" style="display:none;">
      <div class="modal-card close-card" role="alertdialog" aria-modal="true" aria-labelledby="batch-done-title">
        <div class="modal-title" id="batch-done-title"></div>
        <div id="batch-done-body" style="font-size:14px; line-height:1.7; color:#334155; margin:8px 0 14px;"></div>
        <button type="button" class="btn-green btn-block" onclick="document.getElementById('batch-done-modal').style.display='none'">확인</button>
      </div>
    </div>

    <!-- 종료 창 (X 버튼) -->
    <div id="close-modal" class="modal-backdrop" style="display:none;">
      <div class="modal-card close-card" role="dialog" aria-modal="true" aria-labelledby="close-modal-title">
        <div class="modal-title" id="close-modal-title">프로그램을 종료할까요?</div>
        <div id="close-busy" class="close-busy" style="display:none;">⏳ 지금 발행·로그인 작업 또는 예약 목록 등록이 진행 중입니다.</div>
        <button type="button" class="close-opt danger" id="close-quit" onclick="chooseClose('quit')">
          <b>바로 종료</b><span id="close-quit-desc">프로그램을 닫습니다.</span>
        </button>
        <button type="button" class="close-opt" id="close-tray" onclick="chooseClose('tray')">
          <b>트레이로 숨기고 작업이 끝나면 종료</b><span id="close-tray-desc">창은 숨기고 작업은 계속합니다. 끝나면 자동으로 종료돼요.</span>
        </button>
        <button type="button" class="close-opt" onclick="closeCloseDialog()">
          <b>종료하지 않음</b><span>계속 사용합니다.</span>
        </button>
      </div>
    </div>

    <!-- 필수 컴포넌트 자동 설치 배너 -->
    <div id="env-banner" class="env-banner">
      <div class="env-banner-text">
        <span>⚠️ 필수 브라우저/라이브러리 세팅이 감지되지 않았습니다. 원클릭으로 즉시 설치할 수 있습니다.</span>
      </div>
      <button class="btn-amber" onclick="installDependencies()">⚡ 필수 컴포넌트 자동 설치</button>
    </div>

    <!-- 상단 대메뉴 탭 네비게이션 -->
    <div class="main-nav-tabs">
      <button type="button" class="main-nav-tab active" id="tab-btn-publish" onclick="switchMainTab('publish')">
        <span class="tab-icon">🚀</span>
        <span>AI 블로그 자동 발행 & 예약</span>
      </button>
      <button type="button" class="main-nav-tab" id="tab-btn-neighbor" onclick="switchMainTab('neighbor')">
        <span class="tab-icon">🤝</span>
        <span>자동 이웃 순회 & 소통 품앗이</span>
        <span id="tab-neighbor-indicator" class="tab-pulse-indicator" style="display:none;" title="이웃 순회 실행 중"></span>
      </button>
      <button type="button" class="main-nav-tab" id="tab-btn-review" onclick="switchMainTab('review')">
        <span class="tab-icon">📝</span>
        <span>체험단·기자단</span>
      </button>
    </div>

    <!-- [탭 1] AI 블로그 자동 발행 & 예약 -->
    <div id="tab-content-publish" class="tab-content">
      <div class="grid">
      <!-- 좌측 패널: 계정/환경 및 AI 생성 -->
      <div style="display:flex; flex-direction:column; gap:20px;">
        <!-- 1. 계정 설정 -->
        <div class="card">
          <div class="card-title">
            <span>N사 계정 <span style="font-size:12px; color:var(--text-muted); font-weight:600;">(최대 5개)</span></span>
            <button class="btn-outline" id="account-save-btn" style="padding:4px 10px; font-size:12px;" onclick="saveConfig()">저장</button>
          </div>
          <!-- 계정 선택: 누르면 그 계정으로 전환 (블로그·카테고리·고정 문구·로그인 상태가 계정별로 바뀜) -->
          <div id="account-tabs" class="account-tabs"></div>
          <div id="account-hint" class="account-hint" style="display:none;"></div>
          <div class="form-group">
            <label>아이디</label>
            <input type="text" id="naver_id" placeholder="naver_id">
          </div>
          <div class="form-group">
            <label>비밀번호</label>
            <input type="password" id="naver_pw" placeholder="••••••••">
          </div>
          <div class="form-group">
            <label>블로그 ID (공백 시 아이디 사용)</label>
            <input type="text" id="blog_id" placeholder="blog_id (선택)" onchange="refreshCategories()">
          </div>
          <div id="account-new-actions" class="account-actions" style="display:none;">
            <button type="button" class="btn-green" style="flex:2; padding:9px; font-size:13px;" onclick="addAccount()">➕ 계정 추가 후 로그인</button>
            <button type="button" class="btn-outline" style="flex:1; padding:9px; font-size:13px;" id="account-cancel-btn" onclick="cancelAddAccount()">취소</button>
          </div>
          <div id="account-del-wrap" style="text-align:right; margin:-6px 0 12px; display:none;">
            <button type="button" class="link-danger" onclick="deleteAccount()">이 계정 삭제</button>
          </div>
          <div class="form-group" style="background:#f1f5f9; padding:12px; border-radius:10px; border:1px solid #cbd5e1; margin-top:14px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
              <label style="margin-bottom:0; font-weight:700; color:#0f172a;">🔑 Google Gemini API Key</label>
              <span id="gemini-status" style="font-size:11px; font-weight:600; color:#2563eb;">인공지능 원고 생성용</span>
            </div>
            <input type="password" id="gemini_api_key" placeholder="AQ.Ab8... 또는 AIzaSy... (API 키 입력)">
            <div style="display:flex; gap:8px; margin-top:8px;">
              <button type="button" class="btn-outline" style="flex:1; padding:7px; font-size:12px; font-weight:700;" onclick="saveGeminiKey()">💾 키 전용 저장</button>
              <button type="button" class="btn-blue" style="flex:1; padding:7px; font-size:12px; font-weight:700;" onclick="testGeminiKey()">⚡ 키 연결 진단</button>
            </div>
            <div id="gemini-test-msg" style="font-size:12px; margin-top:8px; padding:6px 8px; border-radius:6px; display:none;"></div>
          </div>
          <button class="btn-outline btn-block" onclick="startLogin()"><span id="login-btn-text">🔐 선택한 계정 로그인 (세션 영구 저장)</span></button>
        </div>

        <!-- 2. AI 자동 원고 생성 -->
        <div class="card">
          <div class="card-title">🤖 최신 AI 원고 생성기</div>
          <div class="form-group">
            <label>키워드 / 포스팅 주제</label>
            <input type="text" id="ai_keyword" placeholder="예: 무선 이어폰 추천, 봄나들이 명소" oninput="onKeywordInput()" onkeydown="if(event.key==='Enter'){event.preventDefault();recommendTopics();}">
            <button type="button" id="topic-btn" class="btn-outline btn-block topic-btn" onclick="recommendTopics()">🎯 이 키워드로 최고의 주제 추천받기 (AI + 검색 최적화)</button>
            <div id="topic-selected" class="topic-selected" style="display:none;"></div>
            <div id="topic-panel" class="topic-panel" style="display:none;"></div>
          </div>
          <div class="form-group">
            <label>글 구성 스타일</label>
            <select id="ai_style">
              <option value="info">📘 정보형 총정리 가이드 (핵심 요약+체크포인트)</option>
              <option value="review">⭐ 내돈내산 솔직 후기 (장단점+비교 분석)</option>
              <option value="tips">💡 실전 꿀팁 & 노하우 BEST 5 (실생활 적용)</option>
              <option value="compare">⚖️ 비교 분석형 (차이점+상황별 추천)</option>
              <option value="howto">🧭 단계별 방법 가이드 (준비물+순서+실수 방지)</option>
              <option value="qna">❓ 궁금증 Q&A형 (질문 소제목+바로 답변)</option>
              <option value="checklist">✅ 체크리스트형 (항목별 확인법+요약)</option>
              <option value="issue">📰 최신 이슈·제도 정리형 (대상·일정·신청법)</option>
            </select>
          </div>
          <!-- 쇼핑커넥트: 상품 소개 글 + 수수료 안내 + 상품 링크 카드 -->
          <div class="user-box">
            <label style="display:flex; align-items:center; gap:8px; margin:0; cursor:pointer; font-weight:800;">
              <input type="checkbox" id="shop_on" onchange="toggleShop()" style="width:auto; margin:0;"> 🛒 쇼핑커넥트 상품 글로 쓰기
            </label>
            <div id="shop-fields" style="display:none; margin-top:10px;">
              <div class="user-box-hint">선택한 계정의 쇼핑커넥트 상품을 찾아 고르면, 원고 생성 때 링크 발급 → 상품 페이지 캡처(3장 이상, 본문 사진) → 상품 소개 글 작성까지 자동으로 합니다. 도입 뒤·맺음말 앞에 공식 쇼핑커넥트 상품 카드, 해시태그 앞에 수수료 안내 문구가 들어갑니다.</div>
              <div class="form-group">
                <label>내 계정 쇼핑커넥트 상품 찾기</label>
                <div style="display:flex; gap:6px;">
                  <input type="text" id="shop_kw" placeholder="예) 가습기 (비우면 옆 분야로 찾기)" style="flex:1; min-width:0;" onkeydown="if(event.key==='Enter'){event.preventDefault();searchShop(document.getElementById('shop-search-btn'));}">
                  <select id="shop_cat" title="검색어를 비우면 이 분야로 찾습니다" style="flex:0 0 auto; width:auto; max-width:150px;" onchange="document.getElementById('shop_kw').value='';searchShop(document.getElementById('shop-search-btn'));"></select>
                  <button type="button" id="shop-search-btn" class="btn-outline" style="flex:0 0 auto; padding:6px 10px; font-size:12px;" onclick="searchShop(this)">상품 찾기</button>
                </div>
                <div id="shop-results" style="display:none; margin-top:8px; border:1px solid var(--border, #e2e8f0); border-radius:8px;"></div>
                <div id="shop-picked" style="display:none; margin-top:8px; font-size:12.5px; padding:8px; border-radius:8px; background:#f0fdf4; color:#166534;"></div>
              </div>
              <div style="font-size:12px; color:#64748b; margin:4px 0 8px;">또는 이미 발급받은 링크를 직접 넣기:</div>
              <div class="form-group">
                <label>쇼핑커넥트 상품 링크</label>
                <div style="display:flex; gap:6px;">
                  <input type="text" id="shop_url" placeholder="https://naver.me/..." style="flex:1; min-width:0;" oninput="shopPrice=''; document.getElementById('shop-info').innerText='';">
                  <button type="button" class="btn-outline" style="flex:0 0 auto; padding:6px 10px; font-size:12px;" onclick="fetchShop(this)">상품 정보 읽기</button>
                </div>
              </div>
              <div class="form-group">
                <label>상품명 <span style="font-weight:600; color:#64748b;">(비우면 링크에서 자동으로 읽음)</span></label>
                <input type="text" id="shop_name" placeholder="예) OO 무선 청소기 V12">
                <div id="shop-info" style="font-size:12px; color:#64748b; margin-top:4px;"></div>
              </div>
              <div class="form-group" style="margin-bottom:0;">
                <label>상품 특징 메모 (선택)</label>
                <textarea id="shop_memo" placeholder="예) 구성품, 사이즈, 장점, 이런 분께 추천... (직접 써 본 경험은 여기 적은 만큼만 글에 들어갑니다)"></textarea>
              </div>
            </div>
          </div>
          <!-- 내 자료: 사용자가 직접 준 글·사진을 원고에 가장 먼저 반영 -->
          <div class="user-box">
            <div class="user-box-title">📎 내 자료 (최우선 반영)</div>
            <div class="user-box-hint">여기 적은 글과 올린 사진 내용이 원고에 가장 먼저 들어갑니다. 사진은 본문 내용에 맞는 자리에 자동 배치되고, 모자란 장수만 AI 사진으로 채웁니다.</div>
            <div class="form-group">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                <label style="margin-bottom:0;">내 글·메모</label>
                <button type="button" class="btn-outline file-btn" onclick="document.getElementById('notes_file').click()">📄 글 파일 불러오기</button>
              </div>
              <input type="file" id="notes_file" accept=".txt,.md,text/plain" style="display:none;" onchange="loadNotesFile(this)">
              <textarea id="user_notes" placeholder="예) 가게 이름, 방문 날짜, 먹은 메뉴와 가격, 직접 느낀 점, 꼭 넣고 싶은 정보..."></textarea>
            </div>
            <div class="form-group" style="margin-bottom:0;">
              <label>내 사진 <span id="photo-count" style="font-weight:600; color:#64748b;">(0/10)</span></label>
              <div id="photo-drop" class="photo-drop" onclick="document.getElementById('photo_input').click()">📷 사진을 끌어다 놓거나 눌러서 선택 (여러 장 가능)</div>
              <input type="file" id="photo_input" accept="image/*" multiple style="display:none;" onchange="addPhotos(this.files); this.value='';">
              <div id="photo-list" class="photo-list"></div>
              <button type="button" id="place-btn" class="btn-outline btn-block" style="margin-top:8px; padding:8px; font-size:12.5px; display:none;" onclick="placePhotos()">📍 지금 본문에 사진 자리 자동 배치</button>
            </div>
          </div>
          <button class="btn-blue btn-block" onclick="generateAiDraft()">⚡ 1,500자+ 원고 생성 (원클릭)</button>
        </div>

        <!-- 고정 문구·지도: 모든 글에 자동 삽입 -->
        <div class="card">
          <div class="card-title">
            <span>📌 고정 문구·지도 (모든 글에 자동 삽입)</span>
            <button class="btn-outline" style="padding:4px 10px; font-size:12px;" onclick="saveFixed()">저장</button>
          </div>
          <div class="form-group">
            <label>상단 문구 (본문 맨 위)</label>
            <textarea id="header_text" class="fixed-text" placeholder="예) 안녕하세요, ○○공방입니다."></textarea>
          </div>
          <div class="form-group">
            <label>하단 문구 (본문 맨 아래, 해시태그 위)</label>
            <textarea id="footer_text" class="fixed-text" placeholder="예)&#10;상호명: ○○공방&#10;대표: 홍길동&#10;전화: 010-0000-0000&#10;주소: 서울시 ○○구 ○○로 00"></textarea>
          </div>
          <div class="form-group">
            <label>지도 (장소 검색어: 상호명 또는 주소, 비우면 지도 없음)</label>
            <div style="display:flex; gap:8px;">
              <input type="text" id="map_query" placeholder="예) 서울특별시청 / 서울 중구 세종대로 110" style="flex:1; min-width:0;">
              <select id="map_position" style="width:120px; padding:8px; border-radius:8px; border:1px solid #94a3b8;">
                <option value="bottom">하단 문구 아래</option>
                <option value="top">상단 문구 아래</option>
              </select>
            </div>
            <div class="fixed-hint">N사 지도 검색의 첫 번째 결과가 들어갑니다. 정확한 상호명·주소를 입력하세요.</div>
          </div>
          <div id="fixed-msg" class="fixed-hint" style="display:none;"></div>
        </div>

        <!-- 금지 키워드: 이 말이 들어간 주제·상품·내용은 쓰지 않음 -->
        <div class="card">
          <div class="card-title">
            <span>🚫 금지 키워드 (이 내용은 쓰지 않음)</span>
            <button class="btn-outline" style="padding:4px 10px; font-size:12px;" onclick="saveBanned()">저장</button>
          </div>
          <div class="form-group">
            <textarea id="banned_keywords" class="fixed-text" placeholder="한 줄에 하나씩, 또는 쉼표로 구분&#10;예)&#10;다이어트약&#10;도박, 대출"></textarea>
            <div class="fixed-hint">이 말이 들어간 키워드·추천 주제·쇼핑 상품은 건너뛰고, 원고에도 쓰지 않습니다. 모든 계정에 같이 적용됩니다.</div>
          </div>
          <div id="banned-msg" class="fixed-hint" style="display:none;"></div>
        </div>
      </div>

      <!-- 우측 패널: 원고 편집 및 즉시 포스팅 -->
      <div style="display:flex; flex-direction:column; gap:20px;">
        <div class="card">
          <div class="card-title">
            <span>스마트에디터 ONE 자동 포스팅</span>
            <div id="task-indicator" style="font-size:12px; color:var(--text-muted); font-weight:600;">대기 중</div>
          </div>

          <!-- 카테고리 자동 선택 드롭다운 -->
          <div class="form-group" style="background:#f8fafc; padding:12px; border-radius:10px; border:1px solid #cbd5e1; margin-bottom:14px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
              <label style="margin-bottom:0; font-weight:700; color:#0f172a;">📂 발행 카테고리 선택</label>
              <button type="button" class="btn-outline" style="padding:2px 8px; font-size:11px; font-weight:600;" onclick="refreshCategories()">🔄 카테고리 새로고침</button>
            </div>
            <select id="post_category" onchange="saveConfig()" style="width:100%; padding:9px 12px; border-radius:8px; border:1px solid #94a3b8; font-size:14px; background:#fff; font-weight:600; color:#1e293b; outline:none; cursor:pointer;">
              <option value="">(기본 카테고리로 자동 발행)</option>
            </select>
          </div>

          <div class="form-group">
            <label>포스팅 제목</label>
            <input type="text" id="post_title" placeholder="제목을 입력하세요">
          </div>
          <div class="form-group">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
              <label style="margin-bottom:0;">본문 내용 (스마트에디터에 문단 서식으로 자동 작성)</label>
              <span id="char-count" style="font-size:12px; font-weight:700; color:#0369a1; background:#e0f2fe; padding:2px 8px; border-radius:9999px;">0자 (공백제외 0자)</span>
            </div>
            <textarea id="post_content" oninput="updateCharCount()" placeholder="본문 내용을 입력하세요..."></textarea>
            <div id="photo-marker-hint" style="display:none; font-size:11.5px; color:#166534; margin-top:6px;">📷 본문의 [사진:번호] 줄 = 그 번호 사진이 들어갈 자리입니다. 줄을 옮기면 사진 위치가 바뀝니다. (표시가 없는 사진은 발행할 때 내용에 맞는 자리에 자동 배치, 사진을 빼려면 사진 목록에서 ✕)</div>
          </div>
          <div class="form-group">
            <label>해시태그 (단어 내 공백 없이 입력 ➡️ 본문 하단에 #태그1 #태그2 로 자동 생성)</label>
            <input type="text" id="post_tags" placeholder="정보, 보호, 청라맛집 (또는 #정보 #보호)">
          </div>
          
          <div style="display:flex; flex-direction:column; gap:10px; margin-bottom:14px; background:#f8fafc; padding:14px; border-radius:10px; border:1px solid var(--border);">
            <label class="checkbox-label">
              <input type="checkbox" id="auto_thumb" checked>
              <span>✨ <b>상용급 리치 레이아웃 & 맞춤 실사 사진 자동 조달</b> (인용구 1줄 + 대표 1장 + 2장 콜라주 2세트 + 마무리 1장 = 총 6장 실사 사진 자동 배치)</span>
            </label>
            <label class="checkbox-label">
              <input type="checkbox" id="public_open" checked>
              <span>🌍 전체공개 (댓글/공감 허용)</span>
            </label>
            <!-- 예약 발행: 체크박스 라벨(.checkbox-label input = 16px)과 분리해야 날짜 입력칸이 찌그러지지 않음 -->
            <div class="schedule-box">
              <div class="schedule-head">
                <span>⏰ 예약 발행</span>
                <span class="schedule-hint">비워두면 즉시 발행 · 10분 단위 올림</span>
              </div>
              <div class="schedule-row">
                <input type="datetime-local" id="schedule_at" class="schedule-input" step="600"
                       onchange="applySchedule()" onclick="openPicker(this)" aria-label="예약 발행 날짜와 시각">
                <button type="button" id="schedule-clear" class="schedule-clear" onclick="clearSchedule()" style="display:none;">예약 해제</button>
              </div>
              <div class="schedule-presets">
                <button type="button" onclick="presetSchedule('1h')">+1시간</button>
                <button type="button" onclick="presetSchedule('3h')">+3시간</button>
                <button type="button" onclick="presetSchedule('tom9')">내일 오전 9시</button>
                <button type="button" onclick="presetSchedule('tom18')">내일 오후 6시</button>
              </div>
              <div id="schedule-status" class="schedule-status" style="display:none;"></div>
              <button type="button" class="queue-add" onclick="addToQueue()">📋 예약 목록에 추가 (여러 건 예약)</button>
              <div class="token-warn">⚠️ 예약 목록의 글은 차례대로 N사에 자동 등록되고, 등록될 때 <b>100토큰</b>이 차감됩니다.
                <b>N사에 예약 등록된 뒤에는 취소해도 토큰을 돌려받을 수 없으니 신중하게 선택해 주세요.</b></div>
            </div>
          </div>

          <button id="publish-btn" class="btn-green btn-block" style="padding:14px; font-size:15px;" onclick="startPublish()">
            🚀 스마트에디터 ONE 즉시 자동 발행
          </button>
          
          <div id="publish-result" class="post-result">
            🎉 발행 성공! <a id="publish-link" href="#" target="_blank">글 보러가기</a>
          </div>
        </div>

        <!-- 키워드 자동 예약 -->
        <div class="card">
          <div class="card-title"><span>🎲 키워드 자동 예약</span></div>
          <div id="batch-picked" class="batch-picked" style="display:none;"></div>
          <div id="batch-shop-picked" class="batch-picked" style="display:none;"></div>
          <div class="form-group" id="batch-kw-group">
            <label>키워드 (쉼표나 줄바꿈으로 구분, 고르게 나눠서 씁니다)</label>
            <textarea id="batch_keywords" class="fixed-text" placeholder="예) 제주도 여행, 전기차 보조금, 다이어트 식단"></textarea>
          </div>
          <label style="display:flex; align-items:flex-start; gap:8px; margin:0 0 10px; cursor:pointer; font-size:13px;">
            <input type="checkbox" id="batch_connect" style="width:auto; margin:3px 0 0;">
            <span><b>🛒 쇼핑커넥트 자동</b> — 이 계정의 쇼핑커넥트 상품 중 아직 쓰지 않은 상품을 무작위로 골라 링크 발급·상품 페이지 캡처·상품 소개 글까지 작성해 예약합니다. 키워드를 비워 두면 여러 쇼핑 분야에서, 적으면 그 분야에서 고릅니다.</span>
          </label>
          <div class="form-group">
            <label>🛒 쇼핑커넥트 상품으로 예약 (선택 — 적으면 키워드 대신 상품 한 줄 = 글 하나)</label>
            <textarea id="batch_products" class="fixed-text" oninput="onBatchProducts()" placeholder="한 줄에 하나: 링크 | 상품명 | 특징 메모 (상품명·메모는 생략 가능)&#10;예) https://naver.me/abc | OO 무선 청소기 | 흡입력 강함, 가벼움"></textarea>
          </div>
          <div class="batch-grid">
            <div><label for="batch_count">예약할 글 수</label><input type="number" id="batch_count" min="1" max="24" value="10"></div>
            <div><label for="batch_date">날짜</label><input type="date" id="batch_date"></div>
            <div><label for="batch_start">시작 시간</label><select id="batch_start"></select></div>
            <div><label for="batch_end">끝 시간</label><select id="batch_end"></select></div>
            <div style="grid-column:1 / -1;"><label for="batch_gap">글 사이 최소 간격</label>
              <select id="batch_gap"><option value="60">1시간</option><option value="90">1시간 30분</option><option value="120">2시간</option><option value="180">3시간</option></select></div>
          </div>
          <button type="button" class="queue-add" onclick="previewBatch()">🎲 예약 시각 뽑기</button>
          <div id="batch-preview" style="display:none;">
            <div id="batch-slots" class="batch-slots"></div>
            <div style="display:flex; gap:8px;">
              <button type="button" class="btn-outline" style="flex:0 0 auto;" onclick="previewBatch()">다시 뽑기</button>
              <button type="button" id="batch-start-btn" class="btn-green" style="flex:1;" onclick="startBatch()">이 시각으로 자동 예약 시작</button>
            </div>
          </div>
          <div id="batch-status" class="batch-note" style="display:none;"></div>
          <div class="token-warn">카테고리·공개 설정·사진은 위 자동 포스팅의 선택을 따릅니다. 글을 쓰고 등록하는 동안(1건에 약 5분)은 프로그램을 켜 두세요.
            <b>모두 등록되면 프로그램을 꺼도 블로그에서 예약대로 발행됩니다.</b> 등록될 때 1건당 100토큰이 차감됩니다.</div>
        </div>

        <!-- 예약 목록 -->
        <div class="card">
          <div class="card-title">
            <span>📋 예약 목록 <span id="sch-count" style="font-size:12px; color:var(--text-muted); font-weight:600;"></span></span>
            <span style="display:flex; gap:6px;">
              <button class="btn-outline" style="padding:4px 10px; font-size:12px;" onclick="loadSchedules()">새로고침</button>
              <button class="btn-outline" style="padding:4px 10px; font-size:12px; color:#dc2626; border-color:#fca5a5;" onclick="clearPublishedSchedules()">발행된 것 전체 삭제</button>
            </span>
          </div>
          <table class="sch-table">
            <thead><tr><th style="width:100px;">예약 시각</th><th style="width:100px;">계정</th><th>제목</th><th style="width:78px;">상태</th><th style="width:84px;"></th></tr></thead>
            <tbody id="sch-body"><tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:16px;">예약 목록이 비어 있습니다.</td></tr></tbody>
          </table>
        </div>

        <!-- 실시간 터미널 콘솔 로그 -->
        <div class="card" style="padding:16px;">
          <div class="card-title" style="margin-bottom:8px; font-size:13px; color:var(--text-muted);">
            실시간 자동화 엔진 로그
          </div>
          <div id="console" class="console"></div>
        </div>
      </div>
    </div>

    <!-- 하단: 최근 발행 이력 (History) -->
    <div class="card">
      <div class="card-title">
        <span>📋 최근 발행된 블로그 포스팅 목록</span>
        <span style="display:flex; gap:6px;">
          <button class="btn-outline" style="padding:4px 8px; font-size:12px;" onclick="loadHistory()">새로고침</button>
          <button class="btn-outline" style="padding:4px 8px; font-size:12px; color:#dc2626; border-color:#fca5a5;" onclick="clearPublishHistory()">전체 삭제</button>
        </span>
      </div>
      <div style="overflow-x:auto;">
        <table class="history-table">
          <thead>
            <tr>
              <th style="width:160px;">발행 일시</th>
              <th style="width:110px;">계정</th>
              <th>글 제목</th>
              <th style="width:120px;">카테고리</th>
              <th style="width:120px;">링크</th>
            </tr>
          </thead>
          <tbody id="history-body">
            <tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:20px;">발행 이력이 없습니다.</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div> <!-- [끝] tab-content-publish -->

  <!-- [탭 2] 자동 이웃 순회 & 소통 품앗이 -->
  <!-- [탭 3] 체험단·기자단 -->
  <div id="tab-content-review" class="tab-content" style="display:none;">
    <div class="grid">
      <div style="display:flex; flex-direction:column; gap:20px;">
        <div class="card">
          <div class="card-title"><span>📝 체험단·기자단 글</span></div>
          <div class="user-box-hint" id="rv-account-hint">발행 계정·카테고리는 [AI 블로그 자동 발행] 탭에서 고른 것을 씁니다.</div>
          <div class="rv-mode">
            <button type="button" id="rv-mode-guide" class="rv-mode-btn active" onclick="setReviewMode('guide')">🧭 가이드 원고</button>
            <button type="button" id="rv-mode-direct" class="rv-mode-btn" onclick="setReviewMode('direct')">✍️ 내 원고 그대로</button>
          </div>
          <div id="rv-guide-box">
            <div class="form-group">
              <label>업체·대행사 가이드</label>
              <textarea id="rv_guide" rows="12" placeholder="받은 가이드를 그대로 붙여 넣으세요. (제목 키워드, 필수 키워드, 필수 문구, 해시태그, 사진·영상 위치, 금지 사항 등)"></textarea>
            </div>
            <div class="form-group">
              <label>업체·제품명 <span style="font-weight:400; color:var(--text-muted);">(선택)</span></label>
              <input type="text" id="rv_brand" placeholder="예) OO카페 송도점">
            </div>
            <div class="form-group">
              <label>내가 겪은 내용 메모 <span style="font-weight:400; color:var(--text-muted);">(선택, 원고에 사실로 반영)</span></label>
              <textarea id="rv_notes" rows="4" placeholder="예) 평일 오후 방문, 대기 없이 바로 입장, 크림라떼가 가장 맛있었음"></textarea>
            </div>
            <button type="button" id="rv-draft-btn" class="btn-green" style="width:100%;" onclick="reviewDraft(this)">🧭 가이드대로 원고 만들기 (본문 1,500자 이상)</button>
            <div id="rv-checklist" style="display:none; margin-top:10px; font-size:12px; color:#475569; background:#f8fafc; border-radius:8px; padding:8px 10px;"></div>
          </div>
          <div id="rv-direct-box" style="display:none;">
            <div class="user-box-hint">아래 [제목]·[본문]·[해시태그]에 쓴 그대로 올립니다. 본문에 사진 자리([사진:1])가 없으면 올린 사진을 문단 사이에 1번부터 차례대로 넣습니다.</div>
          </div>
        </div>

        <div class="card">
          <div class="card-title"><span>📷 사진·🎬 영상·🎞 클립</span></div>
          <div class="form-group">
            <label>사진 <span style="font-weight:400; color:var(--text-muted);">(최대 30장, 올린 순서대로 원고에 들어갑니다)</span></label>
            <input type="file" id="rv_photo_input" accept="image/*" multiple style="display:none;" onchange="addReviewPhotos(this.files); this.value='';">
            <button type="button" class="btn-outline" style="width:100%;" onclick="document.getElementById('rv_photo_input').click()">사진 올리기</button>
            <div id="rv-photos" class="rv-media-list"></div>
          </div>
          <div class="form-group">
            <label>영상 <span style="font-weight:400; color:var(--text-muted);">(최대 10개, 4GB 이하)</span></label>
            <input type="file" id="rv_video_input" accept="video/*,.mp4,.mov,.m4v,.avi,.wmv,.mkv,.webm" multiple style="display:none;" onchange="addReviewVideos(this.files); this.value='';">
            <button type="button" class="btn-outline" style="width:100%;" onclick="document.getElementById('rv_video_input').click()">영상 올리기</button>
            <div id="rv-videos" class="rv-media-list"></div>
          </div>
          <div class="form-group" style="margin-bottom:0;">
            <label>내 클립 <span style="font-weight:400; color:var(--text-muted);">(이 계정 블로그의 클립을 골라 넣기)</span></label>
            <button type="button" id="rv-clip-btn" class="btn-outline" style="width:100%;" onclick="loadReviewClips(this)">내 클립 불러오기</button>
            <div id="rv-clips" class="rv-media-list"></div>
          </div>
        </div>

        <!-- 숏폼 클립 만들기: 올린 사진·영상 + 한글 자막 + 한국어 나레이션 → 세로 영상 -->
        <div class="card">
          <div class="card-title"><span>🎞 숏폼 클립 만들기</span></div>
          <div class="user-box-hint">위에 올린 사진·영상을 조합해 <b>세로 9:16 숏폼</b>을 만듭니다. AI가 장면을 직접 보고 <b>한글 자막</b>과 <b>한국어 나레이션</b> 대본을 쓰고, 고친 대본대로 영상을 만듭니다. 처음 한 번은 영상 도구(약 39MB)를 내려받습니다.</div>
          <div class="rv-clip-opts">
            <label>길이 <select id="clip_seconds"><option value="15">약 15초</option><option value="30" selected>약 30초</option><option value="45">약 45초</option><option value="60">약 60초</option></select></label>
            <label>말투 <select id="clip_tone"><option value="review" selected>친근한 후기</option><option value="info">차분한 정보형</option><option value="hype">텐션 높은 숏폼</option></select></label>
            <label>목소리 <select id="clip_voice">
              <option value="female_bright" selected>여성 · 밝고 경쾌</option><option value="female_warm">여성 · 따뜻하고 차분</option><option value="female_young">여성 · 젊고 생기 있게</option>
              <option value="male_friendly">남성 · 친근하게</option><option value="male_calm">남성 · 차분한 정보형</option><option value="male_upbeat">남성 · 경쾌하게</option>
              <option value="none">나레이션 없음 (자막만)</option></select></label>
            <label><input type="checkbox" id="clip_sponsor" style="width:auto;margin:0 4px 0 0;">협찬 표시 <input type="text" id="clip_sponsor_text" value="제품 제공 · 협찬" maxlength="20" style="width:130px;padding:4px 8px;font-size:12px;"></label>
          </div>
          <div style="display:flex;gap:6px;margin-top:8px;">
            <button type="button" id="clip-script-btn" class="btn-outline" style="flex:1;" onclick="clipScript(this)">✍️ AI로 클립 대본 만들기 (자막·나레이션)</button>
            <button type="button" class="btn-outline" style="flex:0 0 auto;" onclick="clipManual()" title="사진·영상마다 빈 장면을 만들어 직접 씁니다">직접 쓰기</button>
          </div>
          <div id="clip-editor" style="display:none;margin-top:10px;">
            <div class="form-group">
              <label>상단 제목 <span style="font-weight:400;color:#64748b;">(2줄, ==단어== 는 노란색 강조)</span></label>
              <input type="text" id="clip_title1" maxlength="24" placeholder="제목 1줄">
              <input type="text" id="clip_title2" maxlength="24" placeholder="제목 2줄" style="margin-top:4px;">
            </div>
            <label>장면 <span style="font-weight:400;color:#64748b;">(자막은 짧게, 나레이션은 소리 내 읽을 문장)</span></label>
            <div id="clip-scenes" class="clip-scenes"></div>
            <button type="button" class="btn-outline" style="padding:4px 10px;font-size:12px;margin-top:6px;" onclick="clipAddScene()">+ 장면 추가</button>
            <button type="button" id="clip-make-btn" class="btn-green" style="width:100%;margin-top:10px;" onclick="clipMake(this)">🎬 이 대본으로 클립 만들기</button>
          </div>
          <div id="clip-status" class="user-box-hint" style="display:none;margin-top:8px;"></div>
          <div id="clip-result" style="display:none;margin-top:10px;text-align:center;">
            <video id="clip-video" controls playsinline preload="metadata" style="width:100%;max-width:300px;border-radius:12px;background:#000;"></video>
            <div style="display:flex;gap:6px;justify-content:center;margin-top:8px;flex-wrap:wrap;">
              <button type="button" class="btn-outline" style="padding:6px 12px;font-size:12.5px;" onclick="clipAddToPost()">➕ 글에 넣기 (영상 목록에 추가)</button>
              <button type="button" class="btn-outline" style="padding:6px 12px;font-size:12.5px;" onclick="postJSON('/api/review/clip_folder', {}).catch(e => alert(e.message))">📂 저장 폴더 열기</button>
            </div>
            <div class="user-box-hint" id="clip-saved" style="margin-top:6px;"></div>
          </div>
        </div>
      </div>

      <div style="display:flex; flex-direction:column; gap:20px;">
        <div class="card">
          <div class="card-title"><span>원고</span><span id="rv-chars" style="font-size:12px; font-weight:600; color:var(--text-muted);"></span></div>
          <div class="form-group">
            <label>제목</label>
            <input type="text" id="rv_title" maxlength="100" placeholder="제목">
          </div>
          <div class="form-group">
            <label>본문</label>
            <div class="rv-toolbar">
              <button type="button" onclick="rvWrap('**','**')" title="굵게"><b>굵게</b></button>
              <button type="button" onclick="rvWrap('==','==')" title="형광펜"><span style="background:#fff593;">형광펜</span></button>
              <button type="button" onclick="rvWrap('__','__')" title="밑줄"><u>밑줄</u></button>
              <select onchange="if(this.value){rvWrap('{{'+this.value+':','}}');} this.value='';" title="글자색">
                <option value="">글자색</option><option>빨강</option><option>주황</option><option>노랑</option><option>초록</option>
                <option>파랑</option><option>남색</option><option>보라</option><option>분홍</option><option>회색</option>
              </select>
              <button type="button" onclick="rvLinePrefix('## ')">소제목</button>
              <button type="button" onclick="rvLinePrefix('> ')">인용구</button>
              <button type="button" onclick="rvInsertLine('[구분선]')">구분선</button>
              <button type="button" onclick="rvInsertLine('[스티커]')">스티커</button>
              <button type="button" onclick="const q = prompt('지도에서 찾을 가게·장소 이름 (비우면 [AI 블로그 자동 발행] 탭의 지도 검색어)', document.getElementById('rv_brand').value || ''); if (q !== null) rvInsertLine(q.trim() ? '[지도:' + q.trim().slice(0, 60) + ']' : '[지도]');">지도</button>
            </div>
            <textarea id="rv_content" rows="22" oninput="rvCount()" placeholder="본문. **굵게** ==형광펜== __밑줄__ {{빨강:글자색}} / ## 소제목 / > 인용구 / [사진:1] [영상:1] [클립:1] [구분선] [스티커] [지도:가게 이름] [링크:https://주소]"></textarea>
          </div>
          <div class="form-group">
            <label>해시태그 <span style="font-weight:400; color:var(--text-muted);">(쉼표나 띄어쓰기로 구분, 최대 30개)</span></label>
            <input type="text" id="rv_tags" placeholder="예) 송도카페, 송도맛집">
          </div>
          <div style="display:flex; flex-wrap:wrap; gap:14px; align-items:center; font-size:13px; margin-bottom:10px;">
            <label style="display:flex; align-items:center; gap:6px; margin:0;"><input type="checkbox" id="rv_center" style="width:auto; margin:0;"> 글 전체 가운데 정렬</label>
            <label style="display:flex; align-items:center; gap:6px; margin:0;"><input type="checkbox" id="rv_public" checked style="width:auto; margin:0;"> 전체 공개</label>
            <label style="display:flex; align-items:center; gap:6px; margin:0;">예약 <input type="datetime-local" id="rv_schedule" step="600" style="width:auto; padding:4px 6px;"></label>
          </div>
          <button type="button" id="rv-publish-btn" class="btn-green" style="width:100%;" onclick="reviewPublish(this)">🚀 발행하기 (예약 시각을 넣으면 예약 발행)</button>
          <div id="rv-status" style="margin-top:8px; font-size:12.5px; color:#475569;"></div>
        </div>
      </div>
    </div>
  </div>

  <div id="tab-content-neighbor" class="tab-content" style="display:none;">
    <!-- 🤝 N사 자동 이웃 순회 & 소통 품앗이 엔진 -->
    <div class="card" id="neighbor-card">
      <div class="neighbor-header">
        <div style="display:flex; align-items:center; gap:10px;">
          <span style="font-size:17px; font-weight:800; color:#0f172a;">🤝 N사 자동 이웃 순회 & 소통 품앗이</span>
          <span id="neighbor-status-badge" class="neighbor-status-badge neighbor-status-idle">
            <span>대기 중</span>
          </span>
        </div>
        <div style="display:flex; gap:8px;">
          <button type="button" class="btn-outline" style="padding:6px 12px; font-size:12.5px;" onclick="saveNeighborConfig(true)">💾 설정 저장</button>
          <button type="button" class="btn-outline" style="padding:6px 12px; font-size:12.5px;" onclick="loadNeighborHistory()">📋 순회 이력 새로고침</button>
        </div>
      </div>

      <!-- 실시간 통계 카운터 카드 -->
      <div class="neighbor-stats-grid">
        <div class="neighbor-stat-card">
          <div class="neighbor-stat-lbl">❤️ 오늘 공감(좋아요)</div>
          <div class="neighbor-stat-val" id="stat-sympathy">0 / 100</div>
        </div>
        <div class="neighbor-stat-card">
          <div class="neighbor-stat-lbl">💬 오늘 맞춤 댓글</div>
          <div class="neighbor-stat-val" id="stat-comment">0 / 50</div>
        </div>
        <div class="neighbor-stat-card">
          <div class="neighbor-stat-lbl">👥 오늘 서로이웃 신청</div>
          <div class="neighbor-stat-val" id="stat-buddy">0 / 50</div>
        </div>
        <div class="neighbor-stat-card">
          <div class="neighbor-stat-lbl">🧭 오늘 총 방문 포스팅</div>
          <div class="neighbor-stat-val" id="stat-total">0건</div>
        </div>
      </div>

      <!-- 👤 순회 실행 계정 선택 (블로그 자동 예약/발행과 다른 계정 분리 지원) -->
      <div style="background:#f1f5f9; border:1px solid #cbd5e1; border-radius:12px; padding:16px; margin-bottom:16px;">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;">
          <div>
            <div style="display:flex; align-items:center; gap:8px;">
              <span style="font-size:14.5px; font-weight:800; color:#0f172a;">👤 순회 실행 계정 선택</span>
              <span style="font-size:11px; background:#e0e7ff; color:#3730a3; padding:2px 8px; border-radius:10px; font-weight:700;">멀티 계정 독립 실행</span>
            </div>
            <div style="font-size:12px; color:var(--text-muted); margin-top:3px;">
              블로그 자동 예약·발행 계정과 다른 부계정 또는 서브 아이디를 지정하여 동시에 독립적으로 소통 품앗이를 진행할 수 있습니다.
            </div>
          </div>
          <div style="min-width:280px;">
            <select id="nb_account_select" style="padding:9px 12px; font-size:13.5px; font-weight:700; width:100%; border:1px solid var(--border); border-radius:8px; background:#ffffff; color:#0f172a;" onchange="onNeighborAccountChange()">
              <option value="">(기본) 현재 활성 계정 사용</option>
            </select>
          </div>
        </div>
        <div id="nb-account-status-msg" style="margin-top:10px; font-size:12px; color:#475569; display:flex; align-items:center; gap:6px; padding:6px 10px; background:#ffffff; border-radius:6px; border:1px solid #e2e8f0;">
          <span class="dot" id="nb-account-dot"></span>
          <span id="nb-account-text">순회 실행 계정 정보를 확인 중입니다...</span>
        </div>
      </div>

      <!-- 타깃 발굴 모드 선택 -->
      <div style="background:#f8fafc; border:1px solid var(--border); border-radius:12px; padding:16px; margin-bottom:16px;">
        <label style="font-weight:700; color:#0f172a; margin-bottom:8px;">🎯 타깃 발굴 모드 선택</label>
        <div class="neighbor-mode-selector" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:10px; margin-bottom:12px;">
          <button type="button" id="nb-mode-feed" class="neighbor-mode-btn active" onclick="setNeighborMode('feed')">
            📻 <b>내 이웃 새글 피드</b><br><span style="font-size:11.5px; color:var(--text-muted); font-weight:500;">기존 이웃 글 실시간 소통 & 맞공감 품앗이</span>
          </button>
          <button type="button" id="nb-mode-keyword" class="neighbor-mode-btn" onclick="setNeighborMode('keyword')">
            🔍 <b>키워드 검색 발굴</b><br><span style="font-size:11.5px; color:var(--text-muted); font-weight:500;">관심 키워드 검색 기반 새 이웃·서이추 발굴</span>
          </button>
          <button type="button" id="nb-mode-target" class="neighbor-mode-btn" onclick="setNeighborMode('target')">
            🎯 <b>타깃 블로그 발굴</b><br><span style="font-size:11.5px; color:var(--text-muted); font-weight:500;">경쟁사/인플루언서 댓글 작성자(찐 소통러) 추출</span>
          </button>
          <button type="button" id="nb-mode-recommend" class="neighbor-mode-btn" onclick="setNeighborMode('recommend')">
            🧭 <b>추천 탐색 피드</b><br><span style="font-size:11.5px; color:var(--text-muted); font-weight:500;">N사 실시간 추천/인기 블로그 순회</span>
          </button>
        </div>

        <div id="nb-keyword-box" class="form-group" style="display:none; margin-bottom:12px;">
          <label>발굴 타깃 키워드 (쉼표로 구분)</label>
          <input type="text" id="nb_keywords" placeholder="일상, 맛집, 육아, 리뷰, 소통, 서이추, 여행, 취미, 재테크">
          <div style="font-size:12px; color:var(--text-muted); margin-top:4px;">N사 모바일 검색 및 스마트블록 검색을 통해 해당 키워드의 최신 글 작성자를 발굴합니다.</div>
        </div>

        <div id="nb-target-box" class="form-group" style="display:none; margin-bottom:12px;">
          <label>🎯 벤치마킹 타깃 블로그 ID / URL (쉼표로 구분)</label>
          <input type="text" id="nb_target_blogs" placeholder="예: competitor1, target_id2, https://blog.naver.com/influencer_id">
          <div style="font-size:12px; color:var(--text-muted); margin-top:4px;">경쟁사 또는 내 분야 인기 블로그의 최근 글에 댓글을 남긴 <strong>'실제 활성 소통 유저'</strong>를 자동으로 찾아가 서로이웃과 품앗이를 진행합니다.</div>
        </div>
      </div>

      <!-- 액션 및 인공지능 세부 설정 (2열 그리드) -->
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px;">
        <!-- 좌: 액션 활성화 & 확률 -->
        <div style="background:#f8fafc; border:1px solid var(--border); border-radius:12px; padding:16px;">
          <div style="font-size:14px; font-weight:700; color:#0f172a; margin-bottom:12px;">⚙️ 소통 액션 & 실행 확률</div>
          
          <div style="display:flex; flex-direction:column; gap:12px;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
              <label class="checkbox-label" style="margin-bottom:0;">
                <input type="checkbox" id="nb_act_sympathy" checked>
                <span>❤️ 공감(좋아요) 누르기</span>
              </label>
              <div style="display:flex; align-items:center; gap:6px;">
                <input type="number" id="nb_prob_sympathy" min="10" max="100" step="5" value="90" style="width:65px; padding:6px; font-size:13px; font-weight:700; text-align:center;">
                <span style="font-size:13px; font-weight:600;">%</span>
              </div>
            </div>

            <div style="display:flex; justify-content:space-between; align-items:center;">
              <label class="checkbox-label" style="margin-bottom:0;">
                <input type="checkbox" id="nb_act_comment" checked>
                <span>💬 맞춤형 댓글 작성</span>
              </label>
              <div style="display:flex; align-items:center; gap:6px;">
                <input type="number" id="nb_prob_comment" min="10" max="100" step="5" value="40" style="width:65px; padding:6px; font-size:13px; font-weight:700; text-align:center;">
                <span style="font-size:13px; font-weight:600;">%</span>
              </div>
            </div>

            <div style="display:flex; justify-content:space-between; align-items:center;">
              <label class="checkbox-label" style="margin-bottom:0;">
                <input type="checkbox" id="nb_act_buddy" checked>
                <span>👥 서로이웃(BothBuddy) 신청</span>
              </label>
              <div style="display:flex; align-items:center; gap:6px;">
                <input type="number" id="nb_prob_buddy" min="10" max="100" step="5" value="35" style="width:65px; padding:6px; font-size:13px; font-weight:700; text-align:center;">
                <span style="font-size:13px; font-weight:600;">%</span>
              </div>
            </div>

            <div class="form-group" style="margin-top:6px; margin-bottom:0;">
              <label style="font-size:12.5px;">댓글 생성 엔진 모드</label>
              <select id="nb_comment_mode" style="padding:8px 10px; font-size:13px;">
                <option value="ai">🤖 Google Gemini AI 맞춤 생성 (본문 자동 분석·칭찬)</option>
                <option value="mixed">⚡ AI 우선 + 실패 시 템플릿 폴백</option>
                <option value="template">📄 준비된 정성 템플릿 문구 랜덤 치환</option>
              </select>
            </div>
          </div>
        </div>

        <!-- 우: N사 어뷰징 방지 & 안티봇 안전 세팅 -->
        <div style="background:#f8fafc; border:1px solid var(--border); border-radius:12px; padding:16px;">
          <div style="font-size:14px; font-weight:700; color:#0f172a; margin-bottom:12px;">🛡️ N사 계정 보호 & 안전 설정</div>
          <div style="display:flex; flex-direction:column; gap:12px;">
            <div>
              <label style="font-size:12.5px;">작업 간격 랜덤 지연 시간 (초)</label>
              <div style="display:flex; align-items:center; gap:8px;">
                <input type="number" id="nb_delay_min" min="40" max="300" value="45" style="width:75px; padding:6px; font-size:13px; font-weight:700; text-align:center;">
                <span style="font-size:13px; color:var(--text-muted); font-weight:600;">초 ~</span>
                <input type="number" id="nb_delay_max" min="70" max="600" value="90" style="width:75px; padding:6px; font-size:13px; font-weight:700; text-align:center;">
                <span style="font-size:13px; color:var(--text-muted); font-weight:600;">초 사이 랜덤 대기</span>
              </div>
              <div style="font-size:11.5px; color:var(--text-muted); margin-top:4px;">보호조치 예방을 위해 최소 40초 · 최대는 최소+30초 이상으로 맞춥니다. 시간당 공감 40 · 댓글 12 · 이웃 신청 10회를 넘지 않고, 8~14개 글마다 4~9분 쉽니다.</div>
            </div>

            <div>
              <label style="font-size:12.5px;">성과 없을 때 자동 정지</label>
              <div style="display:flex; align-items:center; gap:8px;">
                <input type="number" id="nb_stall_min" min="0" max="120" value="10" style="width:75px; padding:6px; font-size:13px; font-weight:700; text-align:center;">
                <span style="font-size:12.5px; color:var(--text-muted); font-weight:600;">분 동안 공감·댓글·이웃 신청이 한 건도 안 되면 멈추고 안내 (0 = 끔)</span>
              </div>
            </div>

            <div>
              <label style="font-size:12.5px;">하루 한도 (<span id="nb-limit-acct">선택한 계정</span> 전용 · 자정에 0부터 다시 시작)</label>
              <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                <span style="font-size:12px; color:var(--text-muted); font-weight:600;">공감</span>
                <input type="number" id="nb_lim_sympathy" min="0" max="1000" value="100" style="width:70px; padding:6px; font-size:13px; font-weight:700; text-align:center;">
                <span style="font-size:12px; color:var(--text-muted); font-weight:600;">댓글</span>
                <input type="number" id="nb_lim_comment" min="0" max="1000" value="50" style="width:70px; padding:6px; font-size:13px; font-weight:700; text-align:center;">
                <span style="font-size:12px; color:var(--text-muted); font-weight:600;">서이추</span>
                <input type="number" id="nb_lim_buddy" min="0" max="1000" value="50" style="width:70px; padding:6px; font-size:13px; font-weight:700; text-align:center;">
              </div>
            </div>

            <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:10px; padding:12px;">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                <label style="font-size:13px; font-weight:700; color:#0f172a; margin-bottom:0;">👥 서로이웃 신청 멘트 생성 모드</label>
                <span style="font-size:11px; color:#16a34a; font-weight:700;">최대 100자 N사 엄수</span>
              </div>
              <select id="nb_buddy_mode" style="padding:8px 10px; font-size:13px; font-weight:600; width:100%; margin-bottom:8px;" onchange="updateBuddyModeUI()">
                <option value="ai">🤖 Google Gemini AI 맞춤 생성 (포스팅 본문·관심사 자동 분석)</option>
                <option value="mixed">⚡ AI 맞춤 생성 우선 + 실패 시 다채로운 템플릿 풀 순환</option>
                <option value="templates">🔀 다채로운 정성 템플릿 풀 랜덤 순환 (스팸 방지)</option>
                <option value="custom">✏️ 직접 입력한 단일 멘트 사용</option>
              </select>

              <!-- 단일 입력 (custom 모드) -->
              <div id="nb-buddy-custom-box" style="display:none; margin-top:6px;">
                <label style="font-size:11.5px; color:var(--text-muted); margin-bottom:4px; display:block;">직접 입력 멘트 ({닉네임} 자동 치환)</label>
                <input type="text" id="nb_buddy_msg" value="안녕하세요 {닉네임}님! 좋은 글 잘 보고 소통하고 싶어 서로이웃 신청드립니다. 자주 왕래하며 소통해요 :)" style="font-size:12.5px;">
              </div>

              <!-- 템플릿 풀 (templates & mixed 모드) -->
              <div id="nb-buddy-templates-box" style="display:none; margin-top:6px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                  <label style="font-size:11.5px; color:var(--text-muted); margin-bottom:0;">다채로운 템플릿 풀 (한 줄에 하나씩, {닉네임} 치환)</label>
                  <button type="button" class="btn-outline" style="padding:2px 8px; font-size:11px;" onclick="resetBuddyTemplates()">기본값 복원</button>
                </div>
                <textarea id="nb_buddy_templates" rows="4" style="width:100%; font-size:12px; padding:6px 8px; border-radius:6px; border:1px solid var(--border); font-family:inherit; resize:vertical; line-height:1.4;" placeholder="신청 멘트를 한 줄에 하나씩 입력해주세요."></textarea>
                <div style="font-size:11px; color:var(--text-muted); margin-top:3px;">※ 순회 시마다 템플릿이 무작위로 선택되어 동일 멘트 반복 스팸 탐지를 완벽히 우회합니다.</div>
              </div>

              <!-- AI 모드 안내 (ai & mixed 모드) -->
              <div id="nb-buddy-ai-box" style="display:block; margin-top:6px; padding:8px 10px; background:#eff6ff; border-radius:6px; border:1px solid #bfdbfe; font-size:11.5px; color:#1e40af; line-height:1.4;">
                💡 <b>AI 자동 분석 생성</b>: 방문한 포스팅의 제목과 본문을 Gemini가 정밀 분석하여 작성자의 관심사/주제를 1단어 언급하고, 40~85자의 정중한 맞춤 신청 멘트를 실시간으로 제작합니다.
              </div>
            </div>

            <div style="display:flex; justify-content:space-between; align-items:center; padding-top:4px;">
              <label class="checkbox-label" style="margin-bottom:0;">
                <input type="checkbox" id="nb_headless" checked>
                <span>👻 백그라운드 무인 실행 (창 숨김)</span>
              </label>
              <span style="font-size:11.5px; color:#15803d; font-weight:600;">안티봇 스텔스 적용</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 제어 버튼 바 -->
      <div style="display:flex; gap:10px; flex-wrap:wrap; margin-bottom:16px;">
        <button type="button" id="nb-start-btn" class="neighbor-ctrl-btn btn-green" style="flex:2;" onclick="startNeighbor()">
          ▶ 자동 이웃 순회 시작
        </button>
        <button type="button" id="nb-pause-btn" class="neighbor-ctrl-btn btn-outline" style="flex:1; display:none;" onclick="pauseNeighbor()">
          ⏸ 일시정지
        </button>
        <button type="button" id="nb-stop-btn" class="neighbor-ctrl-btn btn-outline" style="flex:1; color:#dc2626; border-color:#fca5a5; display:none;" onclick="stopNeighbor()">
          ⏹ 순회 중지
        </button>
      </div>

      <!-- 하위: 실시간 순회 작업 로그 & 최근 소통 이력 2분할 뷰 -->
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px;">
        <!-- 좌: 실시간 로그 -->
        <div>
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
            <label style="margin-bottom:0; font-weight:700;">📡 실시간 순회 작업 로그</label>
            <span id="nb-cur-target" style="font-size:11.5px; color:#2563eb; font-weight:600; text-overflow:ellipsis; overflow:hidden; white-space:nowrap; max-width:240px;"></span>
          </div>
          <div id="nb-stop-reason" style="display:none; margin-bottom:8px; padding:10px 12px; border-radius:8px; background:#fff7ed; border:1px solid #fdba74; color:#9a3412; font-size:12.5px; font-weight:600; line-height:1.5;"></div>
          <div id="neighbor-console" class="neighbor-log-box"></div>
        </div>

        <!-- 우: 최근 소통 이력 -->
        <div>
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
            <label style="margin-bottom:0; font-weight:700;">👥 최근 소통 이웃 내역</label>
            <button type="button" class="link-danger" style="font-size:11.5px;" onclick="clearNeighborHistory()">기록 비우기</button>
          </div>
          <div style="height:160px; overflow-y:auto; border:1px solid var(--border); border-radius:8px; background:#fff;">
            <table class="sch-table" style="font-size:12px;">
              <thead>
                <tr>
                  <th style="width:65px;">시간</th>
                  <th style="width:85px;">작성자</th>
                  <th>글 제목</th>
                  <th style="width:105px;">액션</th>
                </tr>
              </thead>
              <tbody id="neighbor-history-body">
                <tr><td colspan="4" style="text-align:center; color:var(--text-muted); padding:20px;">순회 이력이 없습니다.</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- 🧹 [오토로그 스마트 이웃 관리 1] 무응답 서로이웃 신청 일괄 정리 (5,000명 한도 관리) -->
    <div class="card" style="margin-top:20px; border-left:4px solid #6366f1;">
      <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;">
        <div>
          <div style="display:flex; align-items:center; gap:8px;">
            <span style="font-size:15px; font-weight:800; color:#0f172a;">🧹 무응답 보낸 서로이웃 신청 일괄 정리</span>
            <span style="font-size:11px; background:#e0e7ff; color:#3730a3; padding:2px 8px; border-radius:10px; font-weight:700;">5,000명 한도 잠김 방어</span>
          </div>
          <div style="font-size:12px; color:var(--text-muted); margin-top:3px;">
            상대방이 수락하지 않고 방치한 보낸 신청을 일괄 취소하여 이웃 한도(내가 추가한 이웃 + 보낸 신청 = 5,000명)를 즉시 복원합니다.
          </div>
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
          <select id="nb_cancel_days" style="padding:7px 10px; font-size:12.5px; border-radius:8px; border:1px solid #cbd5e1; background:#fff; font-weight:600;">
            <option value="0">모든 보낸 신청 일괄 취소</option>
            <option value="3">3일 이상 무응답 신청 취소</option>
            <option value="7" selected>7일 이상 무응답 신청 취소 (추천)</option>
            <option value="14">14일 이상 무응답 신청 취소</option>
            <option value="30">30일 이상 무응답 신청 취소</option>
          </select>
          <button type="button" class="btn-primary" id="nb-cancel-btn" style="padding:7px 14px; font-size:12.5px; background:#4f46e5; border-radius:8px;" onclick="runCancelUnanswered()">🧹 무응답 신청 취소 실행</button>
        </div>
      </div>
      <div id="nb-cancel-result" style="display:none; margin-top:12px; padding:10px 14px; border-radius:8px; font-size:12.5px;"></div>
    </div>

    <!-- 👻 [오토로그 스마트 이웃 관리 2] 휴면·유령 이웃 정리기 (Ghost Cleaner) -->
    <div class="card" style="margin-top:16px; border-left:4px solid #f59e0b;">
      <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;">
        <div style="flex:1; min-width:280px;">
          <div style="display:flex; align-items:center; gap:8px;">
            <span style="font-size:15px; font-weight:800; color:#0f172a;">👻 휴면·유령 이웃 자동 정리기</span>
            <span style="font-size:11px; background:#fef3c7; color:#92400e; padding:2px 8px; border-radius:10px; font-weight:700;">유령 이웃 방출 &amp; 한도 복원</span>
          </div>
          <div style="font-size:12px; color:var(--text-muted); margin-top:3px;">
            오랫동안 새 글을 쓰지 않거나 소통이 끊긴 휴면 계정을 조회하여 일괄 삭제합니다. (보호할 이웃 아이디는 제외)
          </div>
        </div>
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
          <select id="nb_ghost_months" style="padding:7px 10px; font-size:12.5px; border-radius:8px; border:1px solid #cbd5e1; background:#fff; font-weight:600;">
            <option value="3">3개월 이상 미활동 이웃</option>
            <option value="6" selected>6개월 이상 미활동 이웃 (추천)</option>
            <option value="12">1년 이상 미활동 이웃</option>
          </select>
          <input type="text" id="nb_ghost_whitelist" placeholder="보호할 ID (쉼표 구분)" style="padding:7px 10px; font-size:12.5px; border-radius:8px; border:1px solid #cbd5e1; width:160px; background:#fff;">
          <button type="button" class="btn-primary" id="nb-ghost-btn" style="padding:7px 14px; font-size:12.5px; background:#d97706; border-radius:8px;" onclick="runCleanGhostBuddies()">👻 유령 이웃 정리 실행</button>
        </div>
      </div>
      <div id="nb-ghost-result" style="display:none; margin-top:12px; padding:10px 14px; border-radius:8px; font-size:12.5px;"></div>
    </div>

    <!-- 📬 [오토로그 스마트 이웃 관리 3] 받은 서로이웃 신청 스마트 관리 -->
    <div class="card" style="margin-top:16px; border-left:4px solid #10b981;">
      <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;">
        <div style="flex:1; min-width:280px;">
          <div style="display:flex; align-items:center; gap:8px;">
            <span style="font-size:15px; font-weight:800; color:#0f172a;">📬 받은 서로이웃 신청 스마트 관리</span>
            <span style="font-size:11px; background:#d1fae5; color:#065f46; padding:2px 8px; border-radius:10px; font-weight:700;">스팸 자동 거절 &amp; 정상 1클릭 수락</span>
          </div>
          <div style="font-size:12px; color:var(--text-muted); margin-top:3px;">
            나에게 들어온 서로이웃 신청을 검토하여 광고·스팸·기본 멘트는 자동 거절하고 정상 유저는 1클릭으로 일괄 수락합니다.
          </div>
        </div>
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
          <select id="nb_recv_action" style="padding:7px 10px; font-size:12.5px; border-radius:8px; border:1px solid #cbd5e1; background:#fff; font-weight:600;">
            <option value="accept_filtered" selected>스팸 거절 + 정상 신청 일괄 수락 (추천)</option>
            <option value="reject_spam_only">스팸 키워드 신청만 거절</option>
            <option value="accept_all">모든 신청 일괄 수락</option>
          </select>
          <label style="font-size:12px; display:inline-flex; align-items:center; gap:4px; cursor:pointer;">
            <input type="checkbox" id="nb_recv_reject_default" checked style="accent-color:#10b981;">
            <span>기본 멘트 거절</span>
          </label>
          <button type="button" class="btn-primary" id="nb-recv-btn" style="padding:7px 14px; font-size:12.5px; background:#059669; border-radius:8px;" onclick="runManageReceivedRequests()">📬 받은 신청 자동 관리 실행</button>
        </div>
      </div>
      <div style="margin-top:8px;">
        <input type="text" id="nb_recv_keywords" value="대출, 부업, 재택, 카지노, 토토, 코인, 투자, 수익, 마케팅, 원고료, 상위노출, 광고, 체험단" style="width:100%; padding:6px 10px; font-size:11.5px; border-radius:6px; border:1px solid #e2e8f0; background:#f8fafc; color:#475569;" title="스팸 필터링 키워드 (쉼표 구분)">
      </div>
      <div id="nb-recv-result" style="display:none; margin-top:12px; padding:10px 14px; border-radius:8px; font-size:12.5px;"></div>
    </div>

  </div> <!-- [끝] tab-content-neighbor -->
  </div>

  <!-- 자동 업데이트: 메인 스크립트와 분리 → 다른 화면 코드에 오류가 있어도 업데이트 알림·설치는 동작 -->
  <script>
    async function updPost(path) {
      const res = await fetch(path, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}' });
      let data = {};
      try { data = await res.json(); } catch (e) {}
      if (!res.ok) throw new Error(data.message || `요청 실패 (HTTP ${res.status})`);
      return data;
    }
    // ── 자동 업데이트: 새 버전 알림 → 사용자가 원할 때 업데이트 → 설치 후 자동 재시작 → 완료 안내 ──
    let updateTimer = null;
    let lastUpdate = {};
    let updateDismissed = false;   // [나중에] (이번 실행 동안만, 다음 실행 때 다시 알림)
    let autoPrompted = '';         // 새 버전을 찾으면 알림 창을 자동으로 1회 띄움 (버전별)
    // 화면에는 앞 세 자리만 (네 번째 자리는 라이선스 업데이트용 내부 번호)
    function shortVer(v) { return String(v || '').split('.').slice(0, 3).join('.'); }
    function renderUpdate(u) {
      lastUpdate = u;
      document.getElementById('app-version').innerText = u.current ? `v${u.display || shortVer(u.current)}` : '';
      // 업데이트 완료 안내
      const done = document.getElementById('updated-banner');
      if (u.just_updated) {
        const j = u.just_updated;
        done.style.display = 'block';
        done.classList.toggle('is-error', !j.ok);
        // 화면 버전이 그대로면 '라이선스 업데이트' (내부 번호만 올라간 배포)
        const sameVer = (j.from_display || shortVer(j.from)) === (u.display || shortVer(j.to));
        // 화면 버전이 같아도 변경 내용이 라이선스와 무관하면 일반 안내 (예: 안정성 업데이트)
        const licenseUpd = sameVer && (!j.notes || j.notes.includes('라이선스'));
        document.getElementById('updated-title').innerText = j.ok
          ? (licenseUpd ? '✅ 라이선스 업데이트가 완료되었습니다' : sameVer ? '✅ 업데이트가 완료되었습니다' : `✅ v${u.display || shortVer(j.to)}(으)로 업데이트가 완료되었습니다`)
          : (licenseUpd ? '⚠️ 라이선스 업데이트가 완료되지 않았습니다' : '⚠️ 업데이트가 완료되지 않았습니다');
        document.getElementById('updated-sub').innerText = j.ok
          ? (sameVer ? '설치가 끝나 프로그램이 자동으로 다시 시작되었습니다.' : `설치가 끝나 프로그램이 자동으로 다시 시작되었습니다. (이전 버전 v${j.from_display || shortVer(j.from)})`)
          : `상단 알림에서 다시 업데이트해 주세요.`;
        const notes = document.getElementById('updated-notes');
        notes.style.display = j.ok && j.notes ? 'block' : 'none';
        notes.innerText = j.notes || '';
      } else {
        done.style.display = 'none';
      }
      // 새 업데이트 알림
      const banner = document.getElementById('update-banner');
      const chip = document.getElementById('update-chip');
      const busy = ['downloading', 'verifying', 'installing'].includes(u.state);
      const show = busy || u.state === 'error' || (u.available && !updateDismissed);
      // 버전 옆 업데이트 버튼: 새 버전이 있으면 활성화(+ NEW 표시), 최신이면 비활성화
      const canUpdate = u.available && !busy;
      chip.disabled = !canUpdate;
      chip.innerText = busy ? '업데이트 중...' : (u.available ? `⬆ ${u.label || 'v' + (u.latest_display || shortVer(u.latest)) + ' 업데이트'}` : '최신 버전');
      chip.title = u.available ? '눌러서 새 버전으로 업데이트' : (u.checked_at ? `최신 버전입니다 (확인: ${u.checked_at})` : '업데이트 확인 중...');
      // 라이선스 화면에서도 업데이트할 수 있게 (키가 없거나 만료돼도 업데이트는 가능)
      const lu = document.getElementById('lic-update');
      if (lu) {
        lu.style.display = u.available || busy || u.state === 'error' ? 'flex' : 'none';
        lu.querySelector('button').style.display = busy ? 'none' : 'inline-block';
        document.getElementById('lic-update-text').innerText =
          u.state === 'error' ? `⚠️ 업데이트 실패: ${u.error}`
          : busy ? (u.state === 'downloading' ? `⬇ 업데이트 받는 중... ${u.progress}%` : '⏳ 업데이트 적용 중... 잠시 후 자동으로 다시 시작됩니다.')
          : (u.label ? `🔔 ${u.label}가 있습니다.` : `🔔 새 버전 v${u.latest_display || shortVer(u.latest)}이(가) 있습니다.`);
      }
      const nb = document.getElementById('new-badge');
      nb.style.display = u.available ? 'inline-block' : 'none';
      nb.title = u.available ? (u.label || `새 버전 v${u.latest_display || shortVer(u.latest)}`) : '';
      banner.style.display = show ? 'block' : 'none';
      if (show) {
        banner.classList.toggle('is-error', u.state === 'error');
        document.getElementById('update-title').innerText = u.state === 'error'
          ? '⚠️ 업데이트 중 문제가 발생했습니다'
          : (u.label ? `🔔 ${u.label}가 있습니다` : `🔔 새 버전 v${u.latest_display || shortVer(u.latest)} 업데이트가 있습니다 (현재 v${u.display || shortVer(u.current)})`);
        document.getElementById('update-sub').innerText = u.state === 'error' ? u.error
          : busy ? '업데이트를 진행하고 있습니다. 창을 닫지 마세요.'
          : (u.can_install ? `원하실 때 업데이트하세요. ${u.kind === 'code' ? `바뀐 부분만 받습니다 (${u.size_mb}MB, 몇 초).` : `전체 설치 파일을 받습니다 (${u.size_mb}MB).`} 설치가 끝나면 프로그램이 자동으로 다시 시작됩니다.`
                           : '개발 실행에서는 설치할 수 없습니다. (설치형 프로그램에서 업데이트)');
        document.getElementById('update-notes').innerText = u.notes || '';
        document.getElementById('update-notes-label').style.display = u.notes && u.state !== 'error' ? 'block' : 'none';
        document.getElementById('update-notes').style.display = u.notes && u.state !== 'error' ? 'block' : 'none';
        document.getElementById('update-actions').style.display = busy ? 'none' : 'flex';
        document.getElementById('update-later').style.display = u.state === 'error' ? 'none' : 'inline-block';
        const btn = document.getElementById('update-btn');
        btn.disabled = !u.can_install;
        btn.innerText = u.state === 'error' ? '다시 시도' : '지금 업데이트';
        const wrap = document.getElementById('update-progress-wrap');
        wrap.style.display = busy ? 'block' : 'none';
        if (busy) {
          const pct = u.state === 'downloading' ? u.progress : 100;
          document.getElementById('update-progress-bar').style.width = pct + '%';
          document.getElementById('update-progress-text').innerText =
            u.state === 'downloading' ? `1/3 다운로드 중... ${u.progress}% (${u.downloaded_mb} / ${u.total_mb} MB)`
            : u.state === 'verifying' ? '2/3 내려받은 파일 확인 중...'
            : (u.kind === 'code' ? '3/3 새 코드를 적용했습니다. 프로그램이 잠시 닫혔다가 자동으로 다시 시작됩니다.'
                                  : '3/3 설치를 시작합니다. 프로그램이 잠시 닫혔다가 설치가 끝나면 자동으로 다시 시작됩니다.');
        }
      }
      // 새 버전 발견 시 알림 창 자동 표시 (같은 버전은 실행 중 1번만)
      if (u.available && u.can_install && !busy && u.state !== 'error' && autoPrompted !== u.latest) {
        autoPrompted = u.latest;
        openUpdateConfirm();
      }
      clearTimeout(updateTimer);
      // 첫 확인이 끝나기 전에는 2초 간격, 진행 중 0.7초, 평소 30초
      updateTimer = setTimeout(pollUpdate, busy ? 700 : (!u.checked_at ? 2000 : 30000));
    }
    async function pollUpdate() {
      try {
        const res = await fetch('/api/update/status');
        renderUpdate(await res.json());
      } catch (e) {
        updateTimer = setTimeout(pollUpdate, 3000);
      }
    }
    function dismissUpdate() { updateDismissed = true; renderUpdate(lastUpdate); }
    function showUpdateBanner() { updateDismissed = false; renderUpdate(lastUpdate); }
    function openUpdateConfirm() {
      document.getElementById('update-modal-title').innerText = lastUpdate.label ? `🔔 ${lastUpdate.label}가 있습니다` : `🔔 새 버전 v${lastUpdate.latest_display || shortVer(lastUpdate.latest)} 업데이트가 있습니다`;
      document.getElementById('update-modal-notes').innerText = lastUpdate.notes || '변경 내용이 등록되지 않았습니다.';
      document.getElementById('update-modal-size').innerText = lastUpdate.kind === 'code'
        ? `바뀐 부분만 내려받아 적용합니다. (${lastUpdate.size_mb}MB, 보통 몇 초)`
        : `전체 설치 파일을 내려받아 설치합니다. (${lastUpdate.size_mb}MB, 인터넷 속도에 따라 1~3분)`;
      document.getElementById('update-modal').style.display = 'flex';
    }
    function closeUpdateConfirm() { document.getElementById('update-modal').style.display = 'none'; }
    async function startUpdate() {
      closeUpdateConfirm();
      try {
        renderUpdate(await updPost('/api/update/install'));
      } catch (e) {
        alert(e.message);
      }
    }
    async function ackUpdated() {
      try { renderUpdate(await updPost('/api/update/ack')); } catch (e) {}
    }

    pollUpdate();
  </script>
  <script>
    // 서버/외부에서 온 문자열은 반드시 esc()로 이스케이프한 뒤 innerHTML에 넣는다 (참고 글 제목 등 외부 값이 로그에 섞임)
    function esc(v) {
      return String(v ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function safeUrl(u) {
      return /^https?:\/\//i.test(u || '') ? u : '';
    }
    // 모든 POST는 JSON으로 (서버가 다른 사이트발 요청 차단을 위해 application/json만 허용)
    async function postJSON(path, body = {}) {
      const res = await fetch(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      });
      let data = {};
      try { data = await res.json(); } catch (e) {}
      if (res.status === 402 && data.license_required && typeof showLicenseScreen === 'function') {
        showLicenseScreen('라이선스를 다시 확인해 주세요.');
      }
      if (!res.ok) {
        throw new Error(data.message || `요청 실패 (HTTP ${res.status})`);
      }
      return data;
    }
    async function checkEnv() {
      try {
        const res = await fetch('/api/env_check');
        const data = await res.json();
        const banner = document.getElementById('env-banner');
        if (!data.all_ready) {
          banner.classList.add('show');
        } else {
          banner.classList.remove('show');
        }
      } catch (e) {}
    }

    async function installDependencies() {
      alert('필수 라이브러리 및 브라우저 자동 설치를 시작합니다. 하단 실행 로그를 확인해 주세요.');
      try { await postJSON('/api/install_dependencies'); } catch (e) { alert(e.message); }
    }

    async function loadCategories(selectedCategory = '') {
      const blog_id = document.getElementById('blog_id').value.trim() || document.getElementById('naver_id').value.trim();
      const selectEl = document.getElementById('post_category');
      if (!blog_id) {
        selectEl.innerHTML = '<option value="">(블로그 ID를 먼저 입력해주세요)</option>';
        return;
      }
      try {
        const res = await fetch('/api/categories?blog_id=' + encodeURIComponent(blog_id));
        const data = await res.json();
        if (data.ok && data.categories && data.categories.length > 0) {
          let html = '<option value="">(기본 카테고리로 자동 발행)</option>';
          data.categories.forEach(c => {
            // 값은 에디터 항목 id와 같은 '{번호}_{이름}' 형식 (동명 하위 카테고리 구분)
            const val = `${c.no}_${c.name}`;
            const isSel = (val === selectedCategory || c.name === selectedCategory) ? 'selected' : '';
            html += `<option value="${esc(val)}" ${isSel}>${esc(c.display_name || c.name)}</option>`;
          });
          selectEl.innerHTML = html;
        } else {
          selectEl.innerHTML = '<option value="">(카테고리 없음 또는 비공개)</option>';
        }
      } catch (e) {
        console.error('카테고리 로드 실패:', e);
      }
    }

    async function refreshCategories() {
      const currentVal = document.getElementById('post_category').value;
      await loadCategories(currentVal);
    }

    // 로그인 표시: 시작 시 + 30초마다 + 로그인·발행 작업이 끝날 때 다시 확인 (예전엔 시작 시 1번뿐이라
    // 프로그램을 켠 뒤 로그인하면 계속 '미로그인'으로 남았음)
    function renderSession(loggedIn) {
      const badge = document.getElementById('session-badge');
      const text = document.getElementById('session-text');
      const who = accountState.active ? ` · ${accountState.active}` : '';
      badge.className = loggedIn ? 'status-pill logged-in' : 'status-pill';
      text.innerText = loggedIn ? `세션 활성화 (로그인 완료${who})` : (accountState.active ? `미로그인${who} (로그인 필요)` : '계정 없음 (계정 추가 필요)');
    }
    async function refreshSession() {
      if (!licenseOK) return;
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        applyAccounts(data);
        renderSession(data.is_logged_in);
      } catch (e) {}
    }

    // ── 여러 계정 ──
    let accountState = { accounts: [], active: '', max: 3 };
    let accountMode = 'view';      // 'view' | 'new'(계정 추가 입력 중)
    let legacyAccount = '';

    function applyAccounts(data) {
      accountState = { accounts: data.accounts || [], active: data.active_account || '', max: data.max_accounts || 5 };
      legacyAccount = (data.config && data.config.legacy_account) || '';
      if (!accountState.accounts.length) accountMode = 'new';
      renderAccounts();
      if (typeof updateNeighborAccountSelect === 'function') updateNeighborAccountSelect();
    }

    function renderAccounts() {
      const st = accountState;
      const tabs = st.accounts.map(a => {
        const active = accountMode === 'view' && a.naver_id === st.active;
        return `<button type="button" class="account-tab${active ? ' active' : ''}" data-id="${esc(a.naver_id)}" onclick="selectAccount(this.dataset.id)" title="${a.logged_in ? '로그인됨' : '로그인 필요'}">`
          + `<span class="dot${a.logged_in ? ' on' : ''}"></span>${esc(a.naver_id)}</button>`;
      });
      if (st.accounts.length < st.max) {
        tabs.push(`<button type="button" class="account-tab add${accountMode === 'new' ? ' active' : ''}" onclick="startAddAccount()">+ 계정 추가 (${st.accounts.length}/${st.max})</button>`);
      }
      document.getElementById('account-tabs').innerHTML = tabs.join('');
      const isNew = accountMode === 'new';
      const hint = document.getElementById('account-hint');
      hint.style.display = isNew ? 'block' : 'none';
      hint.innerText = '새 계정의 아이디·비밀번호를 입력하고 [계정 추가 후 로그인]을 누르면 바로 로그인 창이 열립니다. 보안 인증이 나오면 열린 창에서 직접 완료해 주세요. 계정마다 블로그·카테고리·고정 문구·로그인이 따로 저장됩니다.';
      document.getElementById('account-new-actions').style.display = isNew ? 'flex' : 'none';
      document.getElementById('account-cancel-btn').style.display = st.accounts.length ? 'block' : 'none';
      document.getElementById('account-del-wrap').style.display = !isNew && st.active ? 'block' : 'none';
      document.getElementById('account-save-btn').style.display = isNew ? 'none' : 'inline-block';
      const idInput = document.getElementById('naver_id');
      idInput.readOnly = !isNew;
      idInput.style.background = isNew ? '' : '#f1f5f9';
      idInput.title = isNew ? '' : '등록된 계정의 아이디는 바꿀 수 없습니다. 다른 아이디는 [+ 계정 추가]로 등록하세요.';
    }

    function startAddAccount() {
      if (accountState.accounts.length >= accountState.max) { alert(`계정은 최대 ${accountState.max}개까지 등록할 수 있습니다.`); return; }
      accountMode = 'new';
      ['naver_id', 'naver_pw', 'blog_id'].forEach(id => { document.getElementById(id).value = ''; });
      document.getElementById('naver_pw').placeholder = '비밀번호';
      renderAccounts();
      document.getElementById('naver_id').focus();
    }

    function cancelAddAccount() {
      accountMode = 'view';
      loadData();
    }

    async function addAccount() {
      const naver_id = document.getElementById('naver_id').value.trim();
      const naver_pw = document.getElementById('naver_pw').value.trim();
      const blog_id = document.getElementById('blog_id').value.trim();
      if (!naver_id || !naver_pw) { alert('아이디와 비밀번호를 입력해주세요.'); return; }
      try {
        await postJSON('/api/accounts/add', { naver_id, naver_pw, blog_id });
        accountMode = 'view';
        await loadData();
      } catch (e) { alert(e.message); }
    }

    async function selectAccount(naver_id) {
      if (accountMode === 'view' && naver_id === accountState.active) return;
      try {
        await postJSON('/api/accounts/select', { naver_id });
        accountMode = 'view';
        await loadData();
      } catch (e) { alert(e.message); }
    }

    async function deleteAccount() {
      const id = accountState.active;
      if (!id) return;
      if (!confirm(`'${id}' 계정을 삭제할까요?\n저장된 비밀번호와 로그인 정보, 이 계정의 블로그·고정 문구 설정이 지워집니다. (발행 이력은 남습니다)`)) return;
      try {
        await postJSON('/api/accounts/delete', { naver_id: id });
        await loadData();
      } catch (e) { alert(e.message); }
    }
    setInterval(refreshSession, 30000);

    async function loadData() {
      const res = await fetch('/api/status');
      const data = await res.json();
      applyAccounts(data);
      document.getElementById('naver_id').value = accountMode === 'new' ? '' : (data.config.naver_id || '');
      document.getElementById('naver_pw').value = '';
      document.getElementById('naver_pw').placeholder = accountMode !== 'new' && data.config.has_naver_pw ? '저장됨 (변경할 때만 입력)' : '••••••••';
      document.getElementById('blog_id').value = accountMode === 'new' ? '' : (data.config.blog_id || '');
      document.getElementById('gemini_api_key').value = '';
      document.getElementById('gemini_api_key').placeholder = data.config.has_gemini_api_key ? '저장됨 (변경할 때만 입력)' : 'AQ.Ab8... 또는 AIzaSy... (API 키 입력)';

      renderSession(data.is_logged_in);

      document.getElementById('header_text').value = data.config.header_text || '';
      document.getElementById('footer_text').value = data.config.footer_text || '';
      document.getElementById('map_query').value = data.config.map_query || '';
      document.getElementById('map_position').value = data.config.map_position === 'top' ? 'top' : 'bottom';
      document.getElementById('banned_keywords').value = (data.config.banned_keywords || []).join('\n');
      await loadCategories(data.config.post_category || '');
      checkEnv();
      loadHistory();
      loadNeighborConfig();
      loadNeighborHistory();
      pollNeighborStatus();
      restoreMainTab();
    }

    async function loadHistory() {
      try {
        const res = await fetch('/api/history');
        const items = await res.json();
        const tbody = document.getElementById('history-body');
        if (!items || items.length === 0) {
          tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:20px;">발행 이력이 없습니다.</td></tr>';
          return;
        }
        tbody.innerHTML = items.map(item => `
          <tr>
            <td style="color:var(--text-muted); font-size:12px;">${esc(item.time)}</td>
            <td style="font-size:12px; color:#334155;">${esc(item.account || legacyAccount || '')}</td>
            <td style="font-weight:600;">${esc(item.title)}</td>
            <td><span class="badge-clean">${esc(item.category || '기본')}</span></td>
            <td>${item.scheduled_at ? `⏰ ${esc(item.scheduled_at)} ` : ''}${safeUrl(item.url) ? `<a href="${esc(safeUrl(item.url))}" target="_blank" rel="noopener noreferrer">보기 ↗</a>` : ''}</td>
          </tr>
        `).join('');
      } catch(e) {}
    }

    async function clearPublishHistory() {
      if (!confirm('발행 이력 목록을 모두 삭제하시겠습니까?\n\n- 이 프로그램의 목록만 지워지며, N사 블로그에 올라간 글은 그대로 남습니다.\n- 하루 발행 제한·발행 간격 계산에 쓰이던 기록도 함께 사라집니다.')) return;
      try {
        await postJSON('/api/history/clear');
        loadHistory();
      } catch (e) {
        alert(e.message);
      }
    }

    async function saveFixed() {
      const msg = document.getElementById('fixed-msg');
      try {
        await postJSON('/api/save_fixed', {
          header_text: document.getElementById('header_text').value,
          footer_text: document.getElementById('footer_text').value,
          map_query: document.getElementById('map_query').value.trim(),
          map_position: document.getElementById('map_position').value,
        });
        msg.style.display = 'block'; msg.style.color = '#15803d';
        msg.innerText = '✅ 저장되었습니다. 다음 발행부터 모든 글에 자동으로 들어갑니다.';
      } catch (e) {
        msg.style.display = 'block'; msg.style.color = '#b91c1c'; msg.innerText = '❌ ' + e.message;
      }
    }

    async function saveBanned() {
      const msg = document.getElementById('banned-msg');
      try {
        const r = await postJSON('/api/save_banned', { banned_keywords: document.getElementById('banned_keywords').value });
        document.getElementById('banned_keywords').value = (r.banned_keywords || []).join('\n');
        msg.style.display = 'block'; msg.style.color = '#15803d';
        msg.innerText = `✅ 금지 키워드 ${(r.banned_keywords || []).length}개 저장됨. 다음 글부터 적용됩니다.`;
      } catch (e) {
        msg.style.display = 'block'; msg.style.color = '#b91c1c'; msg.innerText = '❌ ' + e.message;
      }
    }

    async function saveConfig(silent = false) {
      if (accountMode === 'new') {
        if (!silent) await addAccount();
        return;
      }
      const naver_id = document.getElementById('naver_id').value.trim();
      const naver_pw = document.getElementById('naver_pw').value.trim();
      const blog_id = document.getElementById('blog_id').value.trim();
      const post_category = document.getElementById('post_category').value.trim();
      const gemini_api_key = document.getElementById('gemini_api_key').value.trim();
      await postJSON('/api/save_config', { naver_id, naver_pw, blog_id, post_category, gemini_api_key });
      if (!silent) {
        alert('설정이 저장되었습니다.');
      }
    }

    async function saveGeminiKey() {
      const gemini_api_key = document.getElementById('gemini_api_key').value.trim();
      if (!gemini_api_key) {
        alert('API 키를 입력해주세요.');
        return;
      }
      try {
        await postJSON('/api/save_gemini_key', { gemini_api_key });
        alert('✔ Google Gemini API 키가 안전하게 저장되었습니다!');
      } catch (e) {
        alert(e.message);
      }
    }

    async function testGeminiKey() {
      const msgEl = document.getElementById('gemini-test-msg');
      msgEl.style.display = 'block';
      msgEl.style.background = '#f8fafc';
      msgEl.style.color = '#475569';
      msgEl.style.border = '1px solid #cbd5e1';
      msgEl.innerHTML = '⏳ <b>Google AI 서버에 키 검증 요청 중...</b>';

      const gemini_api_key = document.getElementById('gemini_api_key').value.trim();
      try {
        const data = await postJSON('/api/test_gemini_key', { gemini_api_key });
        if (data.ok) {
          msgEl.style.background = '#ecfdf5';
          msgEl.style.color = '#15803d';
          msgEl.style.border = '1px solid #a7f3d0';
          msgEl.innerHTML = '✔ <b>' + esc(data.msg) + '</b>';
        } else {
          msgEl.style.background = '#fef2f2';
          msgEl.style.color = '#b91c1c';
          msgEl.style.border = '1px solid #fecaca';
          msgEl.innerHTML = '❌ <b>' + esc(data.msg) + '</b>';
        }
      } catch (e) {
        msgEl.style.background = '#fef2f2';
        msgEl.style.color = '#b91c1c';
        msgEl.style.border = '1px solid #fecaca';
        msgEl.innerHTML = '❌ 네트워크 진단 오류: ' + esc(e.message || e);
      }
    }

    async function startLogin() {
      if (accountMode === 'new') { await addAccount(); return; }   // 추가하면서 바로 로그인 시작
      await saveConfig();
      document.getElementById('publish-result').style.display = 'none';
      try { await postJSON('/api/login'); } catch (e) { alert(e.message); }
    }

    // ── 주제 추천 (검색 데이터 + AI) ──
    let topicData = null, selectedTopic = null;
    const pickedTopics = new Map();   // 제목 → 주제 (키워드를 바꿔 다시 추천받아도 담은 것은 유지)
    function togglePick(i, on) {
      const t = topicData && topicData.topics[i];
      if (!t) return;
      if (on) pickedTopics.set(t.title, t); else pickedTopics.delete(t.title);
      renderTopics(); renderPicked();
    }
    function renderPicked() {
      const box = document.getElementById('batch-picked');
      const n = pickedTopics.size;
      document.getElementById('batch-kw-group').style.display = (n || batchProductLines() || shopQueue.size) ? 'none' : '';
      const cnt = document.getElementById('batch_count');
      cnt.disabled = n > 0 || shopQueue.size > 0;
      if (n) cnt.value = n;
      if (!n) { box.style.display = 'none'; box.innerHTML = ''; batchPlan = null; document.getElementById('batch-preview').style.display = 'none'; return; }
      box.innerHTML = `📅 추천 주제 <b>${n}개</b>를 예약합니다 (키워드 입력 대신 사용)<ol>`
        + [...pickedTopics.values()].map(t => `<li><span>${esc(t.title)}</span><button type="button" title="빼기" data-t="${esc(t.title)}" onclick="unpick(this.dataset.t)">✕</button></li>`).join('')
        + '</ol>';
      box.style.display = 'block';
      batchPlan = null; document.getElementById('batch-preview').style.display = 'none';
    }
    function unpick(title) { pickedTopics.delete(title); if (topicData) renderTopics(); renderPicked(); }
    function goBatch() { document.getElementById('batch-picked').scrollIntoView({ behavior: 'smooth', block: 'center' }); }
    function scoreClass(n) { return n >= 70 ? '' : (n >= 50 ? 'mid' : 'low'); }
    function levelChip(label, lv, goodWhen) {
      const cls = lv === goodWhen ? 'good' : (lv === '보통' || lv === '확인 못 함' ? '' : 'bad');
      return `<span class="tchip ${cls}">${label} ${esc(lv || '-')}</span>`;
    }
    async function recommendTopics() {
      const keyword = document.getElementById('ai_keyword').value.trim();
      if (!keyword) { alert('키워드를 먼저 입력해주세요.'); return; }
      const btn = document.getElementById('topic-btn');
      const panel = document.getElementById('topic-panel');
      btn.disabled = true;
      btn.innerText = '⏳ 검색 데이터 분석 중... (약 15~30초)';
      panel.style.display = 'block';
      panel.innerHTML = '<div class="topic-head"><span>🔎 연관 검색어와 상위 글을 살펴보는 중입니다...</span></div>';
      try {
        const gemini_api_key = document.getElementById('gemini_api_key').value.trim();
        topicData = await postJSON('/api/topic_recommend', { keyword, gemini_api_key });
        renderTopics();
      } catch (e) {
        panel.innerHTML = `<div class="topic-head"><span>⚠️ ${esc(e.message || e)}</span></div>`;
      } finally {
        btn.disabled = false;
        btn.innerText = '🎯 이 키워드로 최고의 주제 추천받기 (AI + 검색 최적화)';
      }
    }
    function renderTopics() {
      const panel = document.getElementById('topic-panel');
      const d = topicData;
      if (!d || !(d.topics || []).length) { panel.innerHTML = '<div class="topic-head"><span>추천할 주제를 찾지 못했습니다. 다른 키워드로 시도해 보세요.</span></div>'; return; }
      const how = d.ai ? 'AI가 검색 데이터를 보고 추천' : '검색 데이터 기준 추천 (Gemini 키를 넣으면 AI가 더 정교하게 추천)';
      let h = `<div class="topic-head"><span>🎯 '${esc(d.keyword)}' 추천 주제 ${d.topics.length}개 <span class="muted">· ${how}</span></span>
               <button type="button" class="btn-outline" style="padding:3px 8px; font-size:11px; white-space:nowrap; flex:0 0 auto;" onclick="document.getElementById('topic-panel').style.display='none'">접기</button></div>`;
      d.topics.forEach((t, i) => {
        const m = t.metrics || {};
        const sel = selectedTopic && selectedTopic.title === t.title ? ' sel' : '';
        h += `<div class="topic-card${sel}" onclick="selectTopic(${i})">
          <div class="topic-top">
            <div class="topic-score ${scoreClass(t.score)}">${t.score}<small>점</small></div>
            <div style="flex:1; min-width:0;">
              <div class="topic-title">${esc(t.title)}</div>
              <div class="topic-sub">${esc(t.reason || '')}</div>
            </div>
          </div>
          <div class="topic-chips">
            <span class="tchip kw">🔑 ${esc(t.main_keyword)}</span>
            <span class="tchip">📝 ${esc(t.style_name || '')}</span>
            ${m.demand_level ? levelChip('검색 수요', m.demand_level, '높음') : ''}
            ${m.competition_level ? levelChip('경쟁', m.competition_level, '낮음') : ''}
            ${m.gap_level ? levelChip('빈틈', m.gap_level, '높음') : ''}
            ${m.badge ? `<span class="tchip good">🔥 ${esc(m.badge)}</span>` : ''}
          </div>
          ${t.angle ? `<div class="topic-angle">💡 차별화: ${esc(t.angle)}</div>` : ''}
          <label class="topic-pick" onclick="event.stopPropagation()"><input type="checkbox" ${pickedTopics.has(t.title) ? 'checked' : ''} onchange="togglePick(${i}, this.checked)"> 📅 이 주제로 예약 담기</label>
        </div>`;
      });
      if (pickedTopics.size) h += `<div class="topic-bar"><span style="flex:1;">📅 <b>${pickedTopics.size}개</b> 담음</span><button type="button" class="btn-outline" style="padding:4px 10px; font-size:12px;" onclick="pickedTopics.clear(); renderTopics(); renderPicked();">비우기</button><button type="button" class="btn-green" style="padding:6px 12px; font-size:12px;" onclick="goBatch()">예약하러 가기 ↓</button></div>`;
      h += '<div style="font-size:11px; color:#64748b; line-height:1.5;">점수 = 검색 수요(자동완성·함께 찾는 검색어) + 낮은 경쟁(제목에 같은 키워드를 쓴 상위 글 비율) + 빈틈(3개월 넘은 상위 글 비율). 주제를 누르면 키워드·글 구성이 자동으로 채워집니다.</div>';
      panel.innerHTML = h;
    }
    function selectTopic(i) {
      const t = topicData && topicData.topics[i];
      if (!t) return;
      selectedTopic = t;
      document.getElementById('ai_keyword').value = t.main_keyword;
      const sel = document.getElementById('ai_style');
      if ([...sel.options].some(o => o.value === t.style)) sel.value = t.style;
      const box = document.getElementById('topic-selected');
      box.innerHTML = `<span>✅</span><b>선택한 주제: ${esc(t.title)}</b><button type="button" title="선택 해제" onclick="clearTopic()">✕</button>`;
      box.style.display = 'flex';
      renderTopics();
    }
    function clearTopic() {
      selectedTopic = null;
      document.getElementById('topic-selected').style.display = 'none';
      if (topicData) renderTopics();
    }
    function onKeywordInput() {
      // 선택한 주제와 다른 키워드로 바꾸면 선택 해제
      if (selectedTopic && document.getElementById('ai_keyword').value.trim() !== selectedTopic.main_keyword) clearTopic();
    }

    let shopPrice = '';
    let shopPicked = null;     // 내 계정 쇼핑커넥트에서 고른 상품
    const SHOP_CATS = ["생활용품", "주방용품", "청소용품", "수납정리", "욕실용품", "침구", "인테리어소품", "조명", "주방가전", "생활가전",
      "계절가전", "음향기기", "휴대폰 액세서리", "컴퓨터 주변기기", "스킨케어", "헤어케어", "바디케어", "건강식품", "영양제",
      "다이어트식품", "간편식", "커피", "차", "간식", "캠핑용품", "등산용품", "여행용품", "운동용품", "홈트레이닝", "자전거용품",
      "유아용품", "육아용품", "완구", "반려동물용품", "문구", "가방", "지갑", "신발", "모자", "차량용품", "공구", "원예용품"];
    (function () {
      const sel = document.getElementById('shop_cat');
      if (sel) sel.innerHTML = SHOP_CATS.map(c => `<option value="${c}">${c}</option>`).join('')
        + '<option value="__issued__">내가 링크 발급한 상품</option>';
    })();
    let shopItems = [];
    let shopRes = null;        // 마지막 검색 결과 {keyword, page, pages, total, mode}
    const shopQueue = new Map();   // 예약에 담은 상품 (상품 번호 → 상품). 쪽을 넘겨도 유지
    async function searchShop(btn, page) {
      // 검색어를 비우면 옆에서 고른 분야로 찾는다 ('내가 링크 발급한 상품'을 고르면 발급 목록)
      const cat = document.getElementById('shop_cat').value;
      const keyword = page ? (shopRes ? shopRes.keyword : '') : (document.getElementById('shop_kw').value.trim() || (cat === '__issued__' ? '' : cat));
      page = page || 1;
      btn = btn || document.getElementById('shop-search-btn');
      const sbtn = document.getElementById('shop-search-btn');
      const label = '상품 찾기';
      sbtn.disabled = true; sbtn.innerText = '찾는 중...';
      const box = document.getElementById('shop-results');
      box.querySelectorAll('.shop-pager button').forEach(b => b.disabled = true);
      try {
        const d = await postJSON('/api/shopping/search', { keyword, page });
        shopItems = d.items || [];
        shopRes = { keyword, page: d.page || page, pages: d.pages || 1, total: d.total || 0, mode: d.mode };
        renderShopResults();
      } catch (e) { alert(e.message || e); } finally { sbtn.disabled = false; sbtn.innerText = label;
        box.querySelectorAll('.shop-pager button').forEach(b => b.disabled = false); }
    }
    function shopPager() {
      if (!shopRes || shopRes.pages <= 1) return '';
      const cur = shopRes.page, last = shopRes.pages;
      let from = Math.max(1, cur - 2), to = Math.min(last, from + 4);
      from = Math.max(1, to - 4);
      const b = (p, txt, on) => `<button type="button" ${on ? 'class="on"' : ''} ${p < 1 || p > last || p === cur && !on ? 'disabled' : ''} onclick="searchShop(null, ${p})">${txt}</button>`;
      let h = '<div class="shop-pager">' + b(1, '«') + b(cur - 1, '‹');
      for (let p = from; p <= to; p++) h += b(p, p, p === cur);
      return h + b(cur + 1, '›') + b(last, '»') + '</div>';
    }
    function renderShopResults() {
      const box = document.getElementById('shop-results');
      box.style.display = 'block';
      const r = shopRes || {};
      const head = shopItems.length
        ? `<div style="padding:7px 9px; font-size:12px; color:#475569; background:#f8fafc; border-bottom:1px solid #f1f5f9;">${r.mode === 'issued' ? '링크 발급한 상품' : `'${esc(r.keyword)}' 검색`} 전체 <b>${Number(r.total).toLocaleString()}개</b> · ${r.page}/${r.pages}쪽 · <span style="color:#7c3aed;">📅 체크하면 예약 목록에 담깁니다</span></div>`
        : '';
      const list = shopItems.length ? shopItems.map((it, i) =>
          `<div style="display:flex; gap:8px; align-items:center; padding:8px; border-bottom:1px solid #f1f5f9;">`
          + `<input type="checkbox" title="예약에 담기" style="width:auto; margin:0; flex:0 0 auto;" ${shopQueue.has(it.affiliate_product_id) ? 'checked' : ''} onchange="toggleShopQueue(${i}, this.checked)">`
          + (it.image ? `<img src="${esc(it.image)}" style="width:44px; height:44px; object-fit:cover; border-radius:6px; flex:0 0 auto;">` : '')
          + `<div style="flex:1; min-width:0; font-size:12.5px;"><div style="font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${esc(it.name)}</div>`
          + `<div style="color:#64748b;">${esc(it.store)} · ${esc(it.price)} · <b style="color:#ea580c;">수수료 ${it.commission}%</b>${it.used ? ' · <span style="color:#dc2626;">이미 쓴 상품</span>' : ''}</div>`
          + (it.url ? `<div style="color:#16a34a; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">🔗 <a href="${esc(it.url)}" target="_blank" style="color:#16a34a;">${esc(it.url)}</a> <a href="javascript:void(0)" onclick="copyShopLink(${i})" style="color:#475569;">[복사]</a></div>` : '')
          + `</div>`
          + (it.url ? '' : `<button type="button" class="btn-outline shop-issue-btn" style="flex:0 0 auto; padding:4px 8px; font-size:12px;" onclick="issueShop([${i}], this)">링크 발급</button>`)
          + `<button type="button" class="btn-outline" style="flex:0 0 auto; padding:4px 10px; font-size:12px;" onclick="pickShop(${i})">선택</button></div>`).join('')
        : `<div style="padding:10px; font-size:12.5px; color:#64748b;">${r.mode === 'issued'
            ? '이 계정에서 링크를 발급한 상품이 아직 없습니다. 옆 분야를 고르거나 상품 키워드(예: 가습기)를 넣고 [상품 찾기]를 눌러 주세요.'
            : '찾은 상품이 없습니다. 다른 키워드로 찾아 주세요.'}</div>`;
      const bar = shopQueue.size ? `<div class="topic-bar" style="margin:0; border-radius:0 0 8px 8px;"><span style="flex:1;">📅 예약에 <b>${shopQueue.size}개</b> 담음</span>`
        + `<button type="button" class="btn-outline" style="padding:4px 10px; font-size:12px;" onclick="issueQueued(this)">담은 상품 링크 발급</button>`
        + `<button type="button" class="btn-outline" style="padding:4px 10px; font-size:12px;" onclick="shopQueue.clear(); renderShopResults(); renderShopPicked();">비우기</button>`
        + `<button type="button" class="btn-green" style="padding:6px 12px; font-size:12px;" onclick="goShopBatch()">예약하러 가기 ↓</button></div>` : '';
      box.innerHTML = head + `<div style="max-height:320px; overflow:auto;">${list}</div>` + shopPager() + bar;
    }
    function applyShopLinks(links) {
      for (const it of shopItems) if (links[it.affiliate_product_id]) it.url = links[it.affiliate_product_id];
      for (const [id, q] of shopQueue) if (links[id]) q.url = links[id];
    }
    async function postShopIssue(ids, btn) {
      const old = btn ? btn.innerText : '';
      if (btn) { btn.disabled = true; btn.innerText = '발급 중...'; }
      try {
        const d = await postJSON('/api/shopping/issue', { ids });
        applyShopLinks(d.links || {});
        renderShopResults();
        const n = Object.keys(d.links || {}).length;
        if (n < ids.length) alert(`${ids.length}개 중 ${n}개만 발급되었습니다. 나머지는 판매가 끝났거나 쇼핑커넥트 대상에서 빠진 상품입니다.`);
      } catch (e) { alert(e.message || e); if (btn) { btn.disabled = false; btn.innerText = old; } }
    }
    function issueShop(idx, btn) { postShopIssue(idx.map(i => shopItems[i].affiliate_product_id), btn); }
    function issueQueued(btn) {
      const ids = [...shopQueue.values()].filter(q => !q.url).map(q => q.affiliate_product_id);
      if (!ids.length) { alert('담은 상품은 모두 링크가 발급되어 있습니다.'); return; }
      postShopIssue(ids, btn);
    }
    function copyShopLink(i) {
      const u = shopItems[i] && shopItems[i].url;
      if (!u) return;
      (navigator.clipboard ? navigator.clipboard.writeText(u) : Promise.reject()).then(() => alert('링크를 복사했습니다: ' + u))
        .catch(() => prompt('아래 링크를 복사해 주세요.', u));
    }
    function toggleShopQueue(i, on) {
      const it = shopItems[i];
      if (!it) return;
      if (on && !it.raw) { alert('이 상품은 정보가 부족해 예약에 담을 수 없습니다. [선택]으로 바로 써 주세요.'); renderShopResults(); return; }
      if (on && shopQueue.size >= 24) { alert('한 번에 24개까지 담을 수 있습니다.'); renderShopResults(); return; }
      if (on) shopQueue.set(it.affiliate_product_id, { ...it, search_keyword: shopRes ? shopRes.keyword : '' });
      else shopQueue.delete(it.affiliate_product_id);
      renderShopResults(); renderShopPicked();
    }
    function unqueueShop(id) { shopQueue.delete(id); if (shopRes) renderShopResults(); renderShopPicked(); }
    function goShopBatch() { renderShopPicked(); document.getElementById('batch-shop-picked').scrollIntoView({ behavior: 'smooth', block: 'center' }); }
    function renderShopPicked() {
      const box = document.getElementById('batch-shop-picked');
      const n = shopQueue.size;
      const other = pickedTopics.size || batchProductLines();
      document.getElementById('batch-kw-group').style.display = (n || other) ? 'none' : '';
      const cnt = document.getElementById('batch_count');
      cnt.disabled = !!(n || other);
      if (n && !other) cnt.value = n;
      batchPlan = null; document.getElementById('batch-preview').style.display = 'none';
      if (!n) { box.style.display = 'none'; box.innerHTML = ''; return; }
      box.innerHTML = `🛒 쇼핑커넥트 상품 <b>${n}개</b>를 하나씩 상품 글로 예약합니다 (글 쓸 때 링크 발급·상품 사진 캡처)`
        + (other ? '<br><b style="color:#dc2626;">추천 주제나 상품 링크 줄이 있으면 그쪽이 먼저 쓰입니다. 비우고 예약해 주세요.</b>' : '') + '<ol>'
        + [...shopQueue.values()].map(t => `<li><span>${esc(t.name)} <span style="color:#ea580c;">(수수료 ${t.commission}%)</span></span><button type="button" title="빼기" data-t="${esc(t.affiliate_product_id)}" onclick="unqueueShop(this.dataset.t)">✕</button></li>`).join('')
        + '</ol>';
      box.style.display = 'block';
    }
    function pickShop(i) {
      shopPicked = shopItems[i];
      document.getElementById('shop_name').value = shopPicked.name;
      document.getElementById('shop_url').value = shopPicked.url || '';
      shopPrice = shopPicked.price || '';
      const box = document.getElementById('shop-picked');
      box.style.display = 'block';
      box.innerHTML = `✔ 고른 상품: <b>${esc(shopPicked.name)}</b> (${esc(shopPicked.price)}, 수수료 ${shopPicked.commission}%) `
        + `<button type="button" class="btn-outline" style="padding:2px 8px; font-size:11px; margin-left:4px;" onclick="unpickShop()">해제</button>`;
      document.getElementById('shop-results').style.display = 'none';
    }
    function unpickShop() { shopPicked = null; document.getElementById('shop-picked').style.display = 'none'; }
    function addServerPhotos(list) {
      for (const ph of (list || [])) {
        if (userPhotos.filter(p => p.state !== 'err').length >= MAX_PHOTOS) break;
        userPhotos.push({ key: ++photoSeq, name: '상품 캡처', thumb: ph.thumb || '', id: ph.id, desc: ph.desc || '', state: 'ok', msg: '' });
      }
      renderPhotos();
    }
    function toggleShop() {
      document.getElementById('shop-fields').style.display = document.getElementById('shop_on').checked ? 'block' : 'none';
    }
    async function fetchShop(btn) {
      const url = document.getElementById('shop_url').value.trim();
      if (!url) { alert('상품 링크를 먼저 입력해주세요.'); return; }
      const label = btn.innerText;
      btn.disabled = true; btn.innerText = '읽는 중...';
      const info = document.getElementById('shop-info');
      try {
        const d = await postJSON('/api/shopping/fetch', { url });
        if (!d.ok) { shopPrice = ''; info.innerText = d.message; return; }
        if (d.product) {
          // 내 계정 발급 목록에서 찾은 쇼핑커넥트 상품 → 고른 상품으로 (링크 그대로, 상품 사진 캡처까지 자동)
          shopItems = [d.product]; pickShop(0);
          document.getElementById('shop_url').value = url;
          info.innerText = `발급 목록에서 찾은 상품: ${d.name}` + (d.price ? ` / ${d.price}` : '');
          return;
        }
        if (!document.getElementById('shop_name').value.trim()) document.getElementById('shop_name').value = d.name;
        shopPrice = d.price || '';
        info.innerText = `읽은 상품: ${d.name}` + (d.price ? ` / ${d.price}` : '');
      } catch (e) { info.innerText = e.message || String(e); } finally { btn.disabled = false; btn.innerText = label; }
    }

    async function generateAiDraft() {
      const keyword = document.getElementById('ai_keyword').value.trim();
      const style = document.getElementById('ai_style').value;
      const gemini_api_key = document.getElementById('gemini_api_key').value.trim();
      const shopOn = document.getElementById('shop_on').checked;
      const product = shopOn && shopPicked ? {
        affiliate_product_id: String(shopPicked.affiliate_product_id),
        name: shopPicked.name,
        raw: shopPicked.raw || null,
        search_keyword: shopRes ? shopRes.keyword : document.getElementById('shop_kw').value.trim(),
        memo: document.getElementById('shop_memo').value.trim(),
      } : shopOn && !document.getElementById('shop_url').value.trim() ? {
        random: true,
        search_keyword: document.getElementById('shop_kw').value.trim(),
        memo: document.getElementById('shop_memo').value.trim(),
      } : shopOn ? {
        url: document.getElementById('shop_url').value.trim(),
        name: document.getElementById('shop_name').value.trim(),
        memo: document.getElementById('shop_memo').value.trim(),
        price: shopPrice,
      } : null;
      if (product && product.random && !confirm(`고른 상품이 없어 ${product.search_keyword ? `'${product.search_keyword}' 분야에서` : '내 계정 쇼핑커넥트 상품 중'} 아직 쓰지 않은 상품을 무작위로 하나 골라 쓸까요?`)) return;
      if (!keyword && !product) { alert('키워드를 입력해주세요.'); return; }
      
      const btn = event.target;
      const originalText = btn.innerText;
      btn.disabled = true;
      btn.innerText = product && (product.affiliate_product_id || product.random) ? '⏳ 링크 발급·상품 캡처·원고 작성 중... (1~2분, 브라우저 창이 잠깐 뜹니다)'
        : '⏳ Gemini AI 고품질 원고 생성 중... (약 5~10초 소요)';

      try {
        if (photosBusy()) { alert('사진을 올리는 중입니다. 잠시 후 다시 눌러주세요.'); return; }
        const user_notes = document.getElementById('user_notes').value.trim();
        const topic = selectedTopic && selectedTopic.main_keyword === keyword ? selectedTopic : null;
        const data = await postJSON('/api/ai_draft', { keyword, style, gemini_api_key, user_notes, photo_ids: photoIds(), topic, product });
        document.getElementById('post_title').value = data.title;
        document.getElementById('post_content').value = data.contents.join('\n');
        document.getElementById('post_tags').value = (data.tags || []).join(', ');
        if (data.photos && data.photos.length) addServerPhotos(data.photos);
        updateCharCount();
      } catch (e) {
        alert('원고 생성 중 오류: ' + (e.message || e));
      } finally {
        btn.disabled = false;
        btn.innerText = originalText;
      }
    }

    // ── 라이선스 ──
    let licenseOK = false;
    let appStarted = false;
    function licBoxes() { return Array.from(document.querySelectorAll('.lic-box')); }
    function licClean(v) { return String(v || '').toUpperCase().replace(/[^A-Z0-9]/g, ''); }
    function licFill(text, start = 0) {
      const boxes = licBoxes();
      let t = licClean(text);
      for (let i = start; i < boxes.length && t.length; i++) { boxes[i].value = t.slice(0, 4); t = t.slice(4); }
      const next = boxes.find(b => b.value.length < 4) || boxes[boxes.length - 1];
      next.focus();
    }
    (function setupLicenseBoxes() {
      licBoxes().forEach((box, i, boxes) => {
        box.addEventListener('paste', e => {
          e.preventDefault();
          const text = (e.clipboardData || window.clipboardData).getData('text');
          licFill(text, licClean(text).length > 4 ? 0 : i);
          if (licBoxes().map(b => b.value).join('').length === 16) activateLicense();
        });
        box.addEventListener('input', () => {
          const v = licClean(box.value);
          if (v.length > 4) { licFill(v, i); return; }
          box.value = v;
          box.classList.remove('err');
          if (v.length === 4 && i < boxes.length - 1) boxes[i + 1].focus();
        });
        box.addEventListener('keydown', e => {
          if (e.key === 'Backspace' && !box.value && i > 0) { boxes[i - 1].focus(); e.preventDefault(); }
          if (e.key === 'Enter') activateLicense();
        });
      });
    })();

    function licMsg(text, ok) {
      const el = document.getElementById('lic-msg');
      el.className = 'lic-msg ' + (ok ? 'ok' : 'err');
      el.innerText = text || '';
    }

    function showLicenseScreen(message) {
      licenseOK = false;
      document.getElementById('license-screen').style.display = 'flex';
      document.getElementById('license-chip').style.display = 'none';
      if (message) licMsg(message, false);
      setTimeout(() => (licBoxes().find(b => b.value.length < 4) || licBoxes()[0]).focus(), 50);
    }

    function renderLicenseChip(st) {
      const chip = document.getElementById('license-chip');
      const lic = st && st.license;
      if (!lic) { chip.style.display = 'none'; return; }
      let days = '';
      if (lic.expires_at) {
        const d = Math.ceil((lic.expires_at * 1000 - Date.now()) / 86400000);
        days = ` · D-${Math.max(d, 0)}`;
      }
      const tokens = lic.unlimited ? '무제한' : `${Number(lic.tokens_left).toLocaleString()} 토큰`;
      chip.innerText = `🪙 ${tokens}${days}`;
      chip.classList.toggle('low', !lic.unlimited && lic.tokens_left < 500);
      chip.title = `라이선스 ${st.key_hint} · 글 1개 = ${st.cost_per_post}토큰` + (lic.expires_at ? ` · 만료 ${new Date(lic.expires_at * 1000).toLocaleString()}` : ' · 기간 제한 없음')
        + ` · N사 계정 ${lic.accounts_used}/${lic.max_accounts}`;
      chip.style.display = 'inline-flex';
    }

    function startApp() {
      if (appStarted) return;
      appStarted = true;
      loadData();
      restoreSchedule();
      initBatchForm();
      loadSchedules();
      setInterval(() => { if (licenseOK) loadSchedules(); }, 15000);
    }

    // ── 예약 목록 ──
    const SCH_STATE = {
      queued: ['대기 중', '#475569', '#f1f5f9'], writing: ['글 작성 중', '#6d28d9', '#ede9fe'], registering: ['등록 중', '#1d4ed8', '#dbeafe'],
      scheduled: ['예약됨', '#047857', '#d1fae5'], published: ['발행됨', '#065f46', '#ecfdf5'],
      canceling: ['취소 중', '#b45309', '#fef3c7'], canceled: ['취소됨', '#64748b', '#f1f5f9'], failed: ['실패', '#b91c1c', '#fee2e2'],
    };
    function schWhen(s) { return String(s || '').replace('T', ' ').slice(5, 16); }
    async function loadSchedules() {
      try {
        const res = await fetch('/api/schedules');
        if (!res.ok) return;
        const data = await res.json();
        const items = data.items || [];
        renderBatchStatus(data);
        document.getElementById('sch-count').innerText = items.length ? `(${items.length}건${data.pending ? ` · 처리 대기 ${data.pending}건` : ''})` : '';
        const body = document.getElementById('sch-body');
        if (!items.length) {
          body.innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:16px;">예약 목록이 비어 있습니다.</td></tr>';
          return;
        }
        body.innerHTML = items.map(it => {
          const [label, fg, bg] = SCH_STATE[it.status] || [it.status, '#334155', '#f1f5f9'];
          const title = safeUrl(it.url) ? `<a href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener noreferrer">${esc(it.title)}</a>` : esc(it.title);
          let btn = '';
          if (it.status === 'queued') btn = `<button class="sch-btn" data-id="${esc(it.id)}" onclick="cancelSchedule(this.dataset.id, false)">빼기</button>`;
          else if (it.status === 'scheduled') btn = `<button class="sch-btn danger" data-id="${esc(it.id)}" onclick="cancelSchedule(this.dataset.id, true)">예약 취소</button>`;
          else if (['published', 'canceled', 'failed'].includes(it.status)) btn = `<button class="sch-btn" data-id="${esc(it.id)}" onclick="removeSchedule(this.dataset.id)">지우기</button>`;
          if (it.status === 'failed' && !it.unconfirmed) btn += `<button class="sch-btn" style="margin-top:4px;" data-id="${esc(it.id)}" onclick="retrySchedule(this.dataset.id)">다시 시도</button>`;
          return `<tr><td style="font-weight:700;">${esc(schWhen(it.schedule_at))}</td><td style="font-size:12px;">${esc(it.account || '')}</td>`
            + `<td>${title}${it.error ? `<div class="sch-err">${esc(it.error)}</div>` : ''}</td>`
            + `<td><span class="sch-badge" style="color:${fg}; background:${bg};">${label}</span></td><td>${btn}</td></tr>`;
        }).join('');
      } catch (e) {}
    }

    // ── 키워드 자동 예약 ──
    let batchPlan = null;
    function batchProductLines() {
      return document.getElementById('batch_products').value.split('\n').filter(l => l.trim()).length;
    }
    function onBatchProducts() {
      const n = batchProductLines();
      if (n) document.getElementById('batch_count').value = n;
      document.getElementById('batch_count').disabled = !!(n || pickedTopics.size || shopQueue.size);
      document.getElementById('batch-kw-group').style.display = (n || pickedTopics.size || shopQueue.size) ? 'none' : '';
    }
    function batchForm() {
      return {
        keywords: document.getElementById('batch_keywords').value,
        connect: document.getElementById('batch_connect').checked,
        products: batchProductLines() ? document.getElementById('batch_products').value : '',
        topics: batchProductLines() ? [] : [...pickedTopics.values()],
        shop_items: [...shopQueue.values()].map(t => ({ affiliate_product_id: t.affiliate_product_id, raw: t.raw, search_keyword: t.search_keyword })),
        count: batchProductLines() || pickedTopics.size || shopQueue.size || Number(document.getElementById('batch_count').value),
        date: document.getElementById('batch_date').value,
        start_hour: Number(document.getElementById('batch_start').value),
        end_hour: Number(document.getElementById('batch_end').value),
        gap_min: Number(document.getElementById('batch_gap').value),
      };
    }
    function initBatchForm() {
      const hours = (from, to, sel) => { let h = ''; for (let i = from; i <= to; i++) h += `<option value="${i}"${i === sel ? ' selected' : ''}>${i}시</option>`; return h; };
      document.getElementById('batch_start').innerHTML = hours(0, 23, 8);
      document.getElementById('batch_end').innerHTML = hours(1, 24, 23);
      // 오늘 남은 시간이 모자라면 내일로 (10개 × 1시간 기준)
      const d = new Date();
      if (d.getHours() > 12) d.setDate(d.getDate() + 1);
      const pad = (n) => String(n).padStart(2, '0');
      document.getElementById('batch_date').value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
      try { document.getElementById('batch_keywords').value = localStorage.getItem('nbs_batch_keywords') || ''; } catch (e) {}
    }
    async function previewBatch() {
      try {
        const form = batchForm();
        const plan = await postJSON('/api/batch', form);
        batchPlan = { form, slots: plan.slots };
        document.getElementById('batch-slots').innerHTML = plan.slots.map(s => `<span class="batch-slot">${esc(s.slice(5, 16).replace('T', ' '))}</span>`).join('');
        document.getElementById('batch-preview').style.display = 'block';
        if (!form.topics.length) { try { localStorage.setItem('nbs_batch_keywords', form.keywords); } catch (e) {} }
      } catch (e) {
        document.getElementById('batch-preview').style.display = 'none';
        alert(e.message || e);
      }
    }
    async function startBatch() {
      if (!batchPlan) return;
      const n = batchPlan.slots.length;
      if (!confirm(`글 ${n}개를 자동으로 작성해 예약할까요?\n\n`
        + (batchPlan.form.connect && !batchPlan.form.products && !batchPlan.form.topics.length && !batchPlan.form.shop_items.length ? `· 글마다 이 계정의 쇼핑커넥트 상품 중 아직 쓰지 않은 상품을 무작위로 골라(${batchPlan.form.keywords.trim() ? '적은 키워드 분야에서' : '여러 쇼핑 분야에서'}) 링크 발급·상품 페이지 캡처 후 상품 소개 글을 씁니다. (글마다 브라우저 창이 잠깐 뜹니다)\n`
          : batchPlan.form.products ? `· 쇼핑커넥트 상품 ${n}개로 상품 소개 글을 하나씩 씁니다. (수수료 안내 문구·상품 링크 카드 자동 포함)\n`
          : batchPlan.form.topics.length ? `· 고른 추천 주제 ${n}개를 그대로 예약 목록에 넣습니다.\n`
          : batchPlan.form.shop_items.length ? `· 담은 쇼핑커넥트 상품 ${n}개로 상품 소개 글을 하나씩 씁니다. 글마다 링크 발급·상품 사진 캡처를 하며 브라우저 창이 잠깐 뜹니다.\n` : `· 먼저 키워드로 서로 다른 제목 ${n}개를 뽑아 예약 목록에 넣습니다. (1분 정도 걸릴 수 있습니다)\n`)
        + `· 약 ${n * 5}분 동안 차례대로 글을 쓰고 블로그에 예약 등록합니다.\n`
        + `· 등록될 때 1건당 100토큰이 차감되고, 등록 뒤 취소해도 돌려받을 수 없습니다.\n`
        + `· 원고를 미리 검토하지 않고 바로 예약됩니다.`)) return;
      const btn = document.getElementById('batch-start-btn');
      const btnLabel = btn.innerText;
      btn.disabled = true;
      btn.innerText = batchPlan.form.products ? `상품 ${n}개 정보 읽는 중...` : `제목 ${n}개 뽑는 중...`;
      try {
        var batchPlan_shop = !batchPlan.form.topics.length && !batchPlan.form.products && batchPlan.form.shop_items.length > 0;
        await postJSON('/api/batch', {
          ...batchPlan.form, start: true, slots: batchPlan.slots,
          category: document.getElementById('post_category').value,
          public_open: document.getElementById('public_open').checked,
          auto_thumb: document.getElementById('auto_thumb').checked,
        });
        batchPlan = null;
        document.getElementById('batch-preview').style.display = 'none';
        if (pickedTopics.size) { pickedTopics.clear(); renderPicked(); if (topicData) renderTopics(); }
        else if (batchPlan_shop) { shopQueue.clear(); renderShopPicked(); if (shopRes) renderShopResults(); }
        loadSchedules();
      } catch (e) { alert(e.message || e); } finally { btn.disabled = false; btn.innerText = btnLabel; }
    }
    let batchRunning = '';   // 이 실행에서 진행 중인 것을 본 묶음 (끝났을 때 한 번만 안내)
    function announceBatchDone(b) {
      const allDone = b.done === b.total;
      const skipped = b.canceled ? `\n(목록에서 뺀 ${b.canceled}건은 등록하지 않았습니다)` : '';
      document.getElementById('batch-done-title').innerText = allDone ? `✅ ${b.total}건 예약 완료` : `⚠️ ${b.done}건 예약, ${b.failed}건 실패`;
      document.getElementById('batch-done-body').innerText = allDone
        ? `모든 글이 블로그에 예약되었습니다.\n이제 프로그램을 종료해도 예약된 시각에 자동으로 발행됩니다.${skipped}`
        : `예약된 ${b.done}건은 프로그램을 종료해도 예약된 시각에 발행됩니다.\n실패한 ${b.failed}건은 예약 목록에서 사유를 확인하고 [다시 시도]를 눌러 주세요.${skipped}`;
      document.getElementById('batch-done-modal').style.display = 'flex';
      postJSON('/api/focus', { soft: true }).catch(() => {});   // 창이 뒤에 있으면 앞으로
    }
    function renderBatchStatus(data) {
      const box = document.getElementById('batch-status');
      const b = data.batch;
      if (b && b.waiting) batchRunning = b.id;
      else if (b && batchRunning === b.id) { batchRunning = ''; announceBatchDone(b); }
      let cls = '', html = '';
      if (data.paused) {
        cls = 'stop';
        html = `⏸ 자동 예약을 멈췄습니다: ${esc(data.paused)} <button type="button" class="sch-btn" onclick="resumeQueue()">문제를 해결했어요, 다시 시작</button>`;
      } else if (b && b.waiting) {
        cls = 'run';
        html = `⏳ ${b.done}/${b.total}건 등록 완료${b.failed ? ` · 실패 ${b.failed}건` : ''} — 끝날 때까지 프로그램을 켜 두세요. (꺼도 다음 실행 때 이어서 진행합니다)`;
      } else if (b && b.done && b.done === b.total) {
        cls = 'done';
        html = `✅ ${b.total}건 모두 블로그에 예약되었습니다. 이제 프로그램을 꺼도 예약대로 발행됩니다.`;
      } else if (b && b.failed) {
        cls = 'stop';
        html = `⚠️ ${b.done}건 예약, ${b.failed}건 실패. 예약 목록에서 실패 사유를 확인하고 [다시 시도]를 눌러 주세요.`;
      }
      box.className = 'batch-note ' + cls;
      box.innerHTML = html;
      box.style.display = html ? 'block' : 'none';
    }
    async function resumeQueue() {
      try { await postJSON('/api/schedules/resume'); loadSchedules(); } catch (e) { alert(e.message); }
    }
    async function retrySchedule(id) {
      try { await postJSON('/api/schedules/retry', { id }); loadSchedules(); } catch (e) { alert(e.message); }
    }

    async function addToQueue() {
      const title = document.getElementById('post_title').value.trim();
      const content = document.getElementById('post_content').value.trim();
      const schedule_at = document.getElementById('schedule_at').value;
      if (!title || !content) { alert('제목과 본문을 입력해주세요.'); return; }
      if (!schedule_at) { alert('예약 시각을 먼저 선택해 주세요.'); document.getElementById('schedule_at').focus(); return; }
      if (!renderSchedule()) { alert('예약 시각이 지났거나 너무 가깝습니다. 예약 시각을 다시 선택해 주세요.'); return; }
      if (photosBusy()) { alert('사진을 올리는 중입니다. 잠시 후 다시 눌러주세요.'); return; }
      if (!confirm('이 글을 예약 목록에 추가할까요?\n\n'
        + '· 차례대로 N사에 자동 예약 등록되며, 등록될 때 100토큰이 차감됩니다.\n'
        + '· N사에 예약 등록된 뒤에는 취소해도 토큰을 돌려받을 수 없습니다.\n'
        + '· 신중하게 선택해 주세요.')) return;
      try {
        await postJSON('/api/schedules/add', {
          title, content, schedule_at,
          tags: document.getElementById('post_tags').value.trim(),
          category: document.getElementById('post_category').value.trim(),
          auto_thumb: document.getElementById('auto_thumb').checked,
          public_open: document.getElementById('public_open').checked,
          photo_ids: photoIds(),
        });
        // 다음 글을 바로 쓸 수 있게 입력칸 비우기 (올린 사진 파일은 예약이 끝날 때까지 보관)
        ['post_title', 'post_content', 'post_tags'].forEach(id => { document.getElementById(id).value = ''; });
        userPhotos = []; renderPhotos(); updateCharCount(); clearSchedule();
        loadSchedules();
        alert('예약 목록에 추가했습니다. 차례대로 N사에 등록됩니다.');
      } catch (e) { alert(e.message); }
    }

    async function cancelSchedule(id, spent) {
      const msg = spent
        ? '⚠️ 이 글은 이미 N사에 예약 등록되어 100토큰이 사용되었습니다.\n예약을 취소해도 토큰은 돌려받을 수 없습니다.\n\n정말 예약을 취소할까요?'
        : '이 예약을 목록에서 뺄까요?\n(아직 N사에 등록 전이라 토큰은 차감되지 않았습니다)';
      if (!confirm(msg)) return;
      try {
        const r = await postJSON('/api/schedules/cancel', { id });
        if (r.remote) alert('N사 예약 취소를 진행합니다. 잠시 후 목록에서 확인해 주세요.\n(사용한 100토큰은 돌려받지 않습니다)');
        loadSchedules();
      } catch (e) { alert(e.message); }
    }

    async function clearPublishedSchedules() {
      if (!confirm('이미 발행된 예약을 목록에서 모두 지우시겠습니까?\n(블로그에 올라간 글은 그대로 남고, 이 목록에서만 사라집니다. 대기·예약 중인 항목은 지워지지 않습니다.)')) return;
      try {
        const r = await postJSON('/api/schedules/remove_published');
        loadSchedules();
        alert(`${r.removed || 0}건을 지웠습니다.`);
      } catch (e) { alert(e.message); }
    }

    async function removeSchedule(id) {
      try { await postJSON('/api/schedules/remove', { id }); loadSchedules(); } catch (e) { alert(e.message); }
    }

    async function refreshLicense() {
      try {
        const st = await (await fetch('/api/license/status')).json();
        if (st.usable) {
          licenseOK = true;
          document.getElementById('license-screen').style.display = 'none';
          renderLicenseChip(st);
          startApp();
        } else {
          const lic = st.license;
          let why = st.error || '';
          if (lic && lic.expired) why = '라이선스 기간이 끝났습니다. 새 라이선스 키를 입력해 주세요.';
          else if (lic && lic.status === 'revoked') why = '사용이 중지된 라이선스입니다. 관리자에게 문의해 주세요.';
          showLicenseScreen(st.has_key ? why : '');
        }
        return st;
      } catch (e) {
        showLicenseScreen('프로그램 상태를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.');
      }
    }

    async function activateLicense() {
      const key = licBoxes().map(b => b.value).join('');
      if (key.length !== 16) {
        licBoxes().forEach(b => b.classList.toggle('err', b.value.length < 4));
        licMsg('라이선스 키 16자리를 모두 입력해 주세요.', false);
        return;
      }
      const btn = document.getElementById('lic-login');
      btn.disabled = true; btn.innerText = '확인 중...';
      try {
        await postJSON('/api/license/activate', { key });
        licMsg('라이선스가 확인되었습니다.', true);
        licBoxes().forEach(b => { b.value = ''; });
        await refreshLicense();
      } catch (e) {
        licBoxes().forEach(b => b.classList.add('err'));
        licMsg(e.message, false);
      } finally {
        btn.disabled = false; btn.innerText = '로그인';
      }
    }

    async function licenseMenu() {
      const chip = document.getElementById('license-chip');
      if (!confirm(chip.title.replace(/ · /g, '\n') + '\n\n다른 라이선스 키로 바꿀까요? (지금 키에서 로그아웃)')) return;
      try {
        await postJSON('/api/license/logout');
        showLicenseScreen('');
      } catch (e) { alert(e.message); }
    }

    // ── 종료 창 (X 버튼) ──
    function openCloseDialog(busy) {
      document.getElementById('close-busy').style.display = busy ? 'block' : 'none';
      document.getElementById('close-quit-desc').innerText = busy ? '진행 중인 작업이 중단됩니다.' : '프로그램을 닫습니다.';
      const tray = document.getElementById('close-tray');
      tray.disabled = !busy;
      document.getElementById('close-tray-desc').innerText = busy
        ? '창은 숨기고 작업은 계속합니다. 끝나면 자동으로 종료돼요.'
        : '진행 중인 작업이 없어 선택할 수 없습니다.';
      document.getElementById('close-modal').style.display = 'flex';
    }
    function closeCloseDialog() { document.getElementById('close-modal').style.display = 'none'; }
    async function chooseClose(mode) {
      try {
        await postJSON('/api/app/close', { mode });
        closeCloseDialog();
      } catch (e) { alert(e.message); }
    }
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && document.getElementById('close-modal').style.display === 'flex') closeCloseDialog();
    });

    // ── 내 자료: 사진·글 ──
    const MAX_PHOTOS = 10;
    let userPhotos = [];   // {key, name, thumb, id, desc, state: 'up'|'ok'|'err', msg}
    let photoSeq = 0;

    function photoIds() { return userPhotos.filter(p => p.state === 'ok').map(p => p.id); }
    function photosBusy() { return userPhotos.some(p => p.state === 'up'); }

    function renderPhotos() {
      const ok = userPhotos.filter(p => p.state !== 'err');
      document.getElementById('photo-count').textContent = '(' + ok.length + '/' + MAX_PHOTOS + ')';
      document.getElementById('place-btn').style.display = photoIds().length ? 'block' : 'none';
      document.getElementById('photo-marker-hint').style.display = photoIds().length ? 'block' : 'none';
      let n = 0;
      document.getElementById('photo-list').innerHTML = userPhotos.map((p, i) => {
        const num = p.state === 'err' ? '!' : String(++n);
        let state = '';
        if (p.state === 'up') state = '<div class="photo-state">올리는 중...</div>';
        else if (p.state === 'err') state = '<div class="photo-state err">' + esc(p.msg || '실패') + '</div>';
        else if (p.desc) state = '<div class="photo-state">' + esc(p.desc) + '</div>';
        return '<div class="photo-item" title="' + esc(p.desc || p.name || '') + '">'
          + (p.thumb ? '<img src="' + p.thumb + '" alt="">' : '')
          + '<span class="photo-num">' + num + '</span>'
          + '<button type="button" class="photo-del" onclick="removePhoto(' + p.key + ')">✕</button>'
          + state + '</div>';
      }).join('');
    }

    function loadImage(file) {
      return new Promise((resolve, reject) => {
        const url = URL.createObjectURL(file);
        const img = new Image();
        img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
        img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('읽을 수 없는 사진 형식')); };
        img.src = url;
      });
    }

    function drawScaled(img, maxSide, quality) {
      const scale = Math.min(1, maxSide / Math.max(img.naturalWidth, img.naturalHeight));
      const c = document.createElement('canvas');
      c.width = Math.max(1, Math.round(img.naturalWidth * scale));
      c.height = Math.max(1, Math.round(img.naturalHeight * scale));
      const ctx = c.getContext('2d');
      ctx.fillStyle = '#fff';
      ctx.fillRect(0, 0, c.width, c.height);
      ctx.drawImage(img, 0, 0, c.width, c.height);
      return c.toDataURL('image/jpeg', quality);
    }

    async function addPhotos(files) {
      const list = Array.from(files || []).filter(f => (f.type || '').startsWith('image/') || /\.(jpe?g|png|webp|gif|bmp)$/i.test(f.name));
      if (!list.length) return;
      const room = MAX_PHOTOS - userPhotos.filter(p => p.state !== 'err').length;
      if (list.length > room) alert('사진은 최대 ' + MAX_PHOTOS + '장까지 올릴 수 있습니다. 앞의 ' + Math.max(room, 0) + '장만 올립니다.');
      const jobs = list.slice(0, Math.max(room, 0)).map(f => {
        const p = { key: ++photoSeq, name: f.name, thumb: '', id: '', desc: '', state: 'up', msg: '' };
        userPhotos.push(p);
        return [p, f];
      });
      renderPhotos();
      for (const [p, f] of jobs) {
        try {
          const img = await loadImage(f);
          p.thumb = drawScaled(img, 240, 0.8);
          renderPhotos();
          const data = drawScaled(img, 1600, 0.88);
          const res = await postJSON('/api/upload_photo', { data, name: f.name });
          p.id = res.id; p.desc = res.desc || ''; p.state = 'ok';
        } catch (e) {
          p.state = 'err';
          p.msg = /HEIC|형식/i.test(String(e.message)) || /\.heic$/i.test(f.name) ? 'JPG로 바꿔 올려주세요' : (e.message || '업로드 실패');
        }
        renderPhotos();
      }
    }

    // 사진을 빼면 본문의 [사진:번호] 줄도 같이 정리 (뒤 번호는 하나씩 당김)
    function renumberMarkers(removedNum) {
      const ta = document.getElementById('post_content');
      if (!ta.value) return;
      const out = [];
      for (const line of ta.value.split('\n')) {
        const m = line.trim().match(/^\[사진\s*:?\s*(\d{1,2})\]$/);
        if (!m) { out.push(line); continue; }
        const n = parseInt(m[1], 10);
        if (n === removedNum) continue;
        out.push(n > removedNum ? '[사진:' + (n - 1) + ']' : line);
      }
      ta.value = out.join('\n');
      updateCharCount();
    }

    async function removePhoto(key) {
      const idx = userPhotos.findIndex(p => p.key === key);
      if (idx < 0) return;
      const p = userPhotos[idx];
      if (p.state === 'ok') {
        const num = userPhotos.slice(0, idx + 1).filter(x => x.state === 'ok').length;
        renumberMarkers(num);
        try { await postJSON('/api/delete_photo', { id: p.id }); } catch (e) {}
      }
      userPhotos.splice(idx, 1);
      renderPhotos();
    }

    async function placePhotos() {
      const ta = document.getElementById('post_content');
      if (!ta.value.trim()) { alert('먼저 본문을 입력하거나 원고를 생성해주세요.'); return; }
      if (photosBusy()) { alert('사진을 올리는 중입니다. 잠시 후 다시 눌러주세요.'); return; }
      const btn = document.getElementById('place-btn');
      const orig = btn.innerText;
      btn.disabled = true; btn.innerText = '⏳ 사진과 본문 내용을 맞춰 보는 중...';
      try {
        const res = await postJSON('/api/place_photos', { content: ta.value, photo_ids: photoIds(), reset: true });
        ta.value = res.contents.join('\n');
        updateCharCount();
      } catch (e) {
        alert('사진 배치 중 오류: ' + (e.message || e));
      } finally {
        btn.disabled = false; btn.innerText = orig;
      }
    }

    function loadNotesFile(input) {
      const f = input.files && input.files[0];
      input.value = '';
      if (!f) return;
      if (f.size > 200000) { alert('글 파일이 너무 큽니다 (200KB 이하).'); return; }
      const reader = new FileReader();
      reader.onload = () => {
        const ta = document.getElementById('user_notes');
        const text = String(reader.result || '').trim();
        ta.value = ta.value.trim() ? ta.value.trim() + '\n\n' + text : text;
      };
      reader.onerror = () => alert('글 파일을 읽지 못했습니다.');
      reader.readAsText(f, 'utf-8');
    }

    (function setupPhotoDrop() {
      const drop = document.getElementById('photo-drop');
      if (!drop) return;
      ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('drag'); }));
      ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('drag'); }));
      drop.addEventListener('drop', e => addPhotos(e.dataTransfer.files));
    })();

    function updateCharCount() {
      const text = document.getElementById('post_content').value || '';
      const withSpaces = text.length;
      const noSpaces = text.replace(/\s+/g, '').length;
      const badge = document.getElementById('char-count');
      badge.innerText = `${withSpaces.toLocaleString()}자 (공백제외 ${noSpaces.toLocaleString()}자)`;
      if (noSpaces >= 1500) {
        badge.style.color = '#15803d';
        badge.style.background = '#dcfce7';
      } else {
        badge.style.color = '#0369a1';
        badge.style.background = '#e0f2fe';
      }
    }

    // ── 예약 발행: 선택값 저장(새로고침 유지) + 실제 반영 시각 표시 ──
    const SCHEDULE_KEY = 'nbs_schedule_at';
    const WEEK = ['일','월','화','수','목','금','토'];
    function roundUp10(d) {
      const r = new Date(d.getTime());
      r.setSeconds(0, 0);
      if (d.getSeconds() || d.getMilliseconds() || r.getMinutes() % 10) {
        r.setMinutes(Math.floor(r.getMinutes() / 10) * 10 + 10);
      }
      return r;
    }
    function fmtSchedule(d) {
      const p = n => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}(${WEEK[d.getDay()]}) ${p(d.getHours())}:${p(d.getMinutes())}`;
    }
    function toLocalInput(d) {
      const p = n => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
    }
    function renderSchedule() {
      const input = document.getElementById('schedule_at');
      const box = document.getElementById('schedule-status');
      const btn = document.getElementById('publish-btn');
      const clr = document.getElementById('schedule-clear');
      if (!input.value) {
        box.style.display = 'none';
        clr.style.display = 'none';
        btn.innerText = '🚀 스마트에디터 ONE 즉시 자동 발행';
        return true;
      }
      const when = roundUp10(new Date(input.value));
      const mins = Math.round((when - new Date()) / 60000);
      box.style.display = 'block';
      clr.style.display = 'block';
      if (mins < 10) {
        box.style.background = '#fee2e2'; box.style.color = '#b91c1c'; box.style.border = '1px solid #fca5a5';
        box.innerText = `⚠️ ${fmtSchedule(when)}은(는) 지났거나 너무 가깝습니다. 지금부터 10분 이후 시각을 선택하세요.`;
        btn.innerText = '🚀 스마트에디터 ONE 즉시 자동 발행';
        return false;
      }
      const h = Math.floor(mins / 60), m = mins % 60;
      const left = h >= 24 ? `${Math.floor(h/24)}일 ${h%24}시간 후` : (h ? `${h}시간 ${m}분 후` : `${m}분 후`);
      const rounded = toLocalInput(when) !== input.value ? ' · 10분 단위로 올림 적용' : '';
      box.style.background = '#dcfce7'; box.style.color = '#166534'; box.style.border = '1px solid #86efac';
      box.innerText = `✅ 예약 설정 저장됨: ${fmtSchedule(when)} 발행 예정 (${left}${rounded})`;
      btn.innerText = `⏰ ${fmtSchedule(when)} 예약 발행`;
      return true;
    }
    function applySchedule() {
      const v = document.getElementById('schedule_at').value;
      try { v ? localStorage.setItem(SCHEDULE_KEY, v) : localStorage.removeItem(SCHEDULE_KEY); } catch (e) {}
      renderSchedule();
    }
    // 입력칸 어디를 눌러도 달력이 열리게 (아이콘만 누를 수 있던 문제)
    function openPicker(el) {
      try { if (el.showPicker) el.showPicker(); } catch (e) {}
    }
    function presetSchedule(kind) {
      const d = new Date();
      if (kind === '1h') d.setHours(d.getHours() + 1);
      else if (kind === '3h') d.setHours(d.getHours() + 3);
      else {
        d.setDate(d.getDate() + 1);
        d.setHours(kind === 'tom9' ? 9 : 18, 0, 0, 0);
      }
      document.getElementById('schedule_at').value = toLocalInput(roundUp10(d));
      applySchedule();
    }
    function clearSchedule() {
      document.getElementById('schedule_at').value = '';
      applySchedule();
    }
    function restoreSchedule() {
      let v = '';
      try { v = localStorage.getItem(SCHEDULE_KEY) || ''; } catch (e) {}
      document.getElementById('schedule_at').value = v;
      renderSchedule();
    }
    // 남은 시간 표시 갱신
    setInterval(() => { if (document.getElementById('schedule_at').value) renderSchedule(); }, 30000);

    async function startPublish() {
      const title = document.getElementById('post_title').value.trim();
      const content = document.getElementById('post_content').value.trim();
      const tags = document.getElementById('post_tags').value.trim();
      const category = document.getElementById('post_category').value.trim();
      const auto_thumb = document.getElementById('auto_thumb').checked;
      const public_open = document.getElementById('public_open').checked;
      const schedule_at = document.getElementById('schedule_at').value;

      if (!title || !content) { alert('제목과 본문을 입력해주세요.'); return; }
      if (photosBusy()) { alert('사진을 올리는 중입니다. 잠시 후 다시 눌러주세요.'); return; }
      if (!renderSchedule()) { alert('예약 시각이 지났거나 너무 가깝습니다. 예약 시각을 다시 선택하거나 [예약 해제]를 눌러주세요.'); return; }

      document.getElementById('publish-result').style.display = 'none';
      try {
        await postJSON('/api/publish', { title, content, tags, category, auto_thumb, public_open, schedule_at, photo_ids: photoIds() });
      } catch (e) {
        alert(e.message);
      }
    }

    let lastTaskStatus = '';
    async function pollLogs() {
      try {
        const res = await fetch('/api/logs');
        const data = await res.json();
        const cons = document.getElementById('console');
        cons.innerHTML = data.logs.map(l => `<div class="log-line log-${esc(l.level)}">[${esc(l.time)}] ${esc(l.msg)}</div>`).join('');
        cons.scrollTop = cons.scrollHeight;

        const ind = document.getElementById('task-indicator');
        ind.innerText = data.task.message;
        if (data.task.status !== lastTaskStatus) {
          // 작업이 끝나면 계정 정보(로그인 때 확인한 실제 블로그 주소)·카테고리·토큰·예약 목록 다시 불러오기
          if (lastTaskStatus === 'running') { refreshLicense(); if (appStarted) loadData(); if (typeof loadSchedules === 'function') loadSchedules(); }
          lastTaskStatus = data.task.status;
        }
        if (data.task.status === 'success' && data.task.url) {
          const resBox = document.getElementById('publish-result');
          const link = document.getElementById('publish-link');
          link.href = safeUrl(data.task.url) || '#';
          resBox.firstChild.textContent = data.task.scheduled ? '⏰ 예약 등록 완료! ' : '🎉 발행 성공! ';
          resBox.style.display = 'block';
          loadHistory();
        }
        // 예약 등록이 끝나면 같은 시각으로 다시 발행되지 않도록 예약값 비움
        if (data.task.status === 'success' && data.task.scheduled && document.getElementById('schedule_at').value) {
          clearSchedule();
        }
      } catch (e) {}
    }

    // ── 상단 대메뉴 탭 전환 ──
    function switchMainTab(tab) {
      if (!['publish', 'neighbor', 'review'].includes(tab)) tab = 'publish';
      for (const t of ['publish', 'neighbor', 'review']) {
        const b = document.getElementById('tab-btn-' + t);
        const c = document.getElementById('tab-content-' + t);
        if (b) b.classList.toggle('active', t === tab);
        if (c) c.style.display = t === tab ? 'flex' : 'none';
      }
      if (tab === 'review') rvAccountHint();
      try { localStorage.setItem('autolog_main_tab', tab); } catch (e) {}
    }

    // ── 체험단·기자단 ──
    let rvMode = 'guide';
    const rvPhotos = [];      // {key, name, thumb, id, state, msg}
    const rvVideos = [];      // {key, name, id, title, state, pct, msg}
    let rvClipList = [];      // 불러온 내 클립
    const rvClips = [];       // 고른 클립 (고른 순서)
    let rvSeq = 0;
    const RV_MAX_PHOTOS = 30, RV_MAX_VIDEOS = 10, RV_MAX_CLIPS = 10, RV_MIN_CHARS = 1500;

    function rvAccountHint() {
      const el = document.getElementById('rv-account-hint');
      const acc = (typeof accountState !== 'undefined' && accountState.active) || '';
      if (el) el.innerHTML = `발행 계정: <b>${esc(acc || '(계정 없음)')}</b> · 계정·카테고리는 [AI 블로그 자동 발행] 탭에서 고른 것을 씁니다.`;
    }
    function setReviewMode(m) {
      rvMode = m;
      document.getElementById('rv-mode-guide').classList.toggle('active', m === 'guide');
      document.getElementById('rv-mode-direct').classList.toggle('active', m === 'direct');
      document.getElementById('rv-guide-box').style.display = m === 'guide' ? 'block' : 'none';
      document.getElementById('rv-direct-box').style.display = m === 'direct' ? 'block' : 'none';
      rvCount();
    }

    // 본문 글자 수 (공백·표기·자리 표시 제외) — review_post.body_chars 와 같은 기준
    function rvPlain(line) {
      return line.replace(/\*\*|__|==/g, '').replace(/\{\{(빨강|빨간|주황|노랑|초록|파랑|파란|남색|보라|분홍|회색|검정):/g, '').replace(/\}\}/g, '');
    }
    function rvCount() {
      let n = 0;
      for (const raw of document.getElementById('rv_content').value.split('\n')) {
        let s = raw.trim();
        if (!s || /^\[(사진|영상|클립):\s*\d{1,2}\]$|^\[링크:\s*https?:\/\/\S+\]$|^\[스티커(:\s*(인사|포인트|주의|감사))?\]$|^\[구분선\]$|^\[지도(:[^\]]{1,60})?\]$/.test(s)) continue;
        if (s.startsWith('## ')) s = s.slice(3); else if (s.startsWith('> ')) s = s.slice(2);
        n += rvPlain(s).replace(/\s+/g, '').length;
      }
      const el = document.getElementById('rv-chars');
      const short = rvMode === 'guide' && n < RV_MIN_CHARS;
      el.innerHTML = `본문 ${n.toLocaleString()}자` + (rvMode === 'guide' ? (short ? ` <span style="color:#dc2626;">(${RV_MIN_CHARS.toLocaleString()}자 이상 필요)</span>` : ' ✔') : '');
      return n;
    }

    // 본문 꾸밈 단추
    function rvWrap(a, b) {
      const ta = document.getElementById('rv_content');
      const s = ta.selectionStart, e = ta.selectionEnd, v = ta.value;
      const sel = v.slice(s, e) || '글자';
      ta.value = v.slice(0, s) + a + sel + b + v.slice(e);
      ta.focus(); ta.selectionStart = s + a.length; ta.selectionEnd = s + a.length + sel.length;
      rvCount();
    }
    function rvLinePrefix(p) {
      const ta = document.getElementById('rv_content');
      const v = ta.value, s = ta.selectionStart;
      const ls = v.lastIndexOf('\n', s - 1) + 1;
      const line = v.slice(ls);
      if (line.startsWith(p)) return;
      ta.value = v.slice(0, ls) + p + v.slice(ls).replace(/^(## |> )/, '');
      ta.focus(); ta.selectionStart = ta.selectionEnd = s + p.length;
      rvCount();
    }
    function rvInsertLine(text) {
      const ta = document.getElementById('rv_content');
      const v = ta.value, s = ta.selectionStart;
      const before = v.slice(0, s), after = v.slice(s);
      const pre = before && !before.endsWith('\n') ? '\n' : '';
      const post = after.startsWith('\n') ? '' : '\n';
      ta.value = before + pre + text + post + after;
      const pos = (before + pre + text + post).length;
      ta.focus(); ta.selectionStart = ta.selectionEnd = pos;
      rvCount();
    }

    // 사진
    function renderReviewPhotos() {
      const box = document.getElementById('rv-photos');
      box.innerHTML = rvPhotos.map((p, i) => `<div class="rv-item">`
        + `<b class="rv-num">${i + 1}</b>`
        + (p.thumb ? `<img src="${p.thumb}">` : '<span class="rv-ph">📷</span>')
        + `<span class="rv-name">${esc(p.name)}<br><small>${p.state === 'up' ? '올리는 중...' : p.state === 'err' ? '<span style="color:#dc2626;">' + esc(p.msg) + '</span>' : '올림'}</small></span>`
        + `<button type="button" title="본문 커서 자리에 넣기" onclick="rvInsertLine('[사진:${i + 1}]')">넣기</button>`
        + `<button type="button" title="앞으로" onclick="rvMove(rvPhotos, ${i}, -1, renderReviewPhotos, '사진')" ${i ? '' : 'disabled'}>▲</button>`
        + `<button type="button" title="뒤로" onclick="rvMove(rvPhotos, ${i}, 1, renderReviewPhotos, '사진')" ${i < rvPhotos.length - 1 ? '' : 'disabled'}>▼</button>`
        + `<button type="button" title="빼기" onclick="rvRemove(rvPhotos, ${i}, renderReviewPhotos, '사진')">✕</button></div>`).join('');
    }
    async function addReviewPhotos(files) {
      const list = Array.from(files || []).filter(f => (f.type || '').startsWith('image/') || /\.(jpe?g|png|webp|gif|bmp)$/i.test(f.name));
      const room = RV_MAX_PHOTOS - rvPhotos.length;
      if (list.length > room) alert(`사진은 최대 ${RV_MAX_PHOTOS}장까지 올릴 수 있습니다. 앞의 ${Math.max(room, 0)}장만 올립니다.`);
      // 파일 이름 순서대로 (휴대폰 사진은 찍은 순서)
      const jobs = list.slice(0, Math.max(room, 0)).sort((a, b) => a.name.localeCompare(b.name, 'ko', { numeric: true })).map(f => {
        const p = { key: ++rvSeq, name: f.name, thumb: '', id: '', state: 'up', msg: '' };
        rvPhotos.push(p);
        return [p, f];
      });
      renderReviewPhotos();
      for (const [p, f] of jobs) {
        try {
          const img = await loadImage(f);
          p.thumb = drawScaled(img, 160, 0.8);
          renderReviewPhotos();
          const res = await postJSON('/api/upload_photo', { data: drawScaled(img, 1600, 0.9), name: f.name, describe: false });
          p.id = res.id; p.state = 'ok';
        } catch (e) {
          p.state = 'err';
          p.msg = /\.heic$/i.test(f.name) ? 'JPG로 바꿔 올려주세요' : (e.message || '업로드 실패');
        }
        renderReviewPhotos();
      }
    }
    // 순서를 바꾸거나 빼면 본문의 [사진:N] 번호도 같이 맞춘다
    function rvRenumber(kind, map) {
      const ta = document.getElementById('rv_content');
      const re = new RegExp('^\\[' + kind + ':\\s*(\\d{1,2})\\]$');
      ta.value = ta.value.split('\n').filter(l => { const m = l.trim().match(re); return !m || map[+m[1]] !== 0; })
        .map(l => { const m = l.trim().match(re); return m && map[+m[1]] ? `[${kind}:${map[+m[1]]}]` : l; }).join('\n');
      rvCount();
    }
    function rvMove(arr, i, d, render, kind) {
      const j = i + d;
      if (j < 0 || j >= arr.length) return;
      [arr[i], arr[j]] = [arr[j], arr[i]];   // 본문 자리는 그대로: 1번 자리에는 언제나 1번 사진 (순서대로)
      render();
    }
    function rvRemove(arr, i, render, kind) {
      const it = arr[i];
      arr.splice(i, 1);
      const map = { [i + 1]: 0 };
      for (let k = i + 2; k <= arr.length + 1; k++) map[k] = k - 1;
      rvRenumber(kind, map);
      if (it && it.id && kind === '사진') postJSON('/api/delete_photo', { id: it.id }).catch(() => {});
      if (it && it.id && kind === '영상') postJSON('/api/delete_video', { id: it.id }).catch(() => {});
      render();
    }

    // 영상
    function renderReviewVideos() {
      const box = document.getElementById('rv-videos');
      box.innerHTML = rvVideos.map((v, i) => `<div class="rv-item">`
        + `<b class="rv-num">${i + 1}</b><span class="rv-ph">🎬</span>`
        + `<span class="rv-name">${esc(v.name)}<br><small>${v.state === 'up' ? `올리는 중 ${v.pct}%` : v.state === 'err' ? '<span style="color:#dc2626;">' + esc(v.msg) + '</span>' : '올림'}</small>`
        + `<input type="text" maxlength="40" placeholder="영상 제목 (40자)" value="${esc(v.title)}" oninput="rvVideos[${i}].title=this.value"></span>`
        + `<button type="button" title="본문 커서 자리에 넣기" onclick="rvInsertLine('[영상:${i + 1}]')">넣기</button>`
        + `<button type="button" title="앞으로" onclick="rvMove(rvVideos, ${i}, -1, renderReviewVideos, '영상')" ${i ? '' : 'disabled'}>▲</button>`
        + `<button type="button" title="뒤로" onclick="rvMove(rvVideos, ${i}, 1, renderReviewVideos, '영상')" ${i < rvVideos.length - 1 ? '' : 'disabled'}>▼</button>`
        + `<button type="button" title="빼기" onclick="rvRemove(rvVideos, ${i}, renderReviewVideos, '영상')">✕</button></div>`).join('');
    }
    function uploadVideoFile(v, f) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/upload_video');
        xhr.setRequestHeader('Content-Type', 'application/json');   // 서버의 위조 요청 방지 규칙 (내용은 파일 그대로)
        xhr.setRequestHeader('X-File-Name', encodeURIComponent(f.name));
        xhr.upload.onprogress = e => { if (e.lengthComputable) { v.pct = Math.floor(e.loaded * 100 / e.total); renderReviewVideos(); } };
        xhr.onload = () => {
          let d = {};
          try { d = JSON.parse(xhr.responseText); } catch (e) {}
          xhr.status === 200 && d.ok ? resolve(d) : reject(new Error(d.message || `업로드 실패 (HTTP ${xhr.status})`));
        };
        xhr.onerror = () => reject(new Error('업로드 실패'));
        xhr.send(f);
      });
    }
    async function addReviewVideos(files) {
      const list = Array.from(files || []);
      const room = RV_MAX_VIDEOS - rvVideos.length;
      if (list.length > room) alert(`영상은 최대 ${RV_MAX_VIDEOS}개까지 올릴 수 있습니다.`);
      const jobs = list.slice(0, Math.max(room, 0)).map(f => {
        const v = { key: ++rvSeq, name: f.name, id: '', title: f.name.replace(/\.[^.]+$/, '').slice(0, 40), state: 'up', pct: 0, msg: '' };
        rvVideos.push(v);
        return [v, f];
      });
      renderReviewVideos();
      for (const [v, f] of jobs) {
        try { const d = await uploadVideoFile(v, f); v.id = d.id; v.state = 'ok'; }
        catch (e) { v.state = 'err'; v.msg = e.message; }
        renderReviewVideos();
      }
    }

    // 내 클립
    async function loadReviewClips(btn) {
      btn.disabled = true; btn.innerText = '불러오는 중... (20초쯤)';
      try {
        const d = await postJSON('/api/review/clips', {});
        rvClipList = d.clips || [];
        if (!rvClipList.length) alert('이 계정 블로그에 클립이 없습니다.');
        renderReviewClips();
      } catch (e) { alert(e.message || e); }
      finally { btn.disabled = false; btn.innerText = '내 클립 다시 불러오기'; }
    }
    function renderReviewClips() {
      const box = document.getElementById('rv-clips');
      box.innerHTML = rvClipList.map((c, i) => {
        const at = rvClips.findIndex(x => x.id === c.id);
        return `<label class="rv-item" style="cursor:pointer;">`
          + `<input type="checkbox" style="width:auto; margin:0;" ${at >= 0 ? 'checked' : ''} onchange="toggleReviewClip(${i}, this.checked)">`
          + (at >= 0 ? `<b class="rv-num">${at + 1}</b>` : '')
          + (c.thumb ? `<img src="${esc(c.thumb)}">` : '')
          + `<span class="rv-name">${esc(c.title)}<br><small>${esc(c.date)}</small></span>`
          + (at >= 0 ? `<button type="button" onclick="event.preventDefault(); rvInsertLine('[클립:${at + 1}]')">넣기</button>` : '')
          + `</label>`;
      }).join('');
    }
    function toggleReviewClip(i, on) {
      const c = rvClipList[i];
      const at = rvClips.findIndex(x => x.id === c.id);
      if (on && at < 0) {
        if (rvClips.length >= RV_MAX_CLIPS) { alert(`클립은 최대 ${RV_MAX_CLIPS}개까지 넣을 수 있습니다.`); renderReviewClips(); return; }
        rvClips.push({ id: c.id, title: c.title });
      } else if (!on && at >= 0) {
        rvClips.splice(at, 1);
        const map = { [at + 1]: 0 };
        for (let k = at + 2; k <= rvClips.length + 1; k++) map[k] = k - 1;
        rvRenumber('클립', map);
      }
      renderReviewClips();
    }

    function rvMedia() {
      if (rvPhotos.some(p => p.state === 'up') || rvVideos.some(v => v.state === 'up')) throw new Error('사진·영상을 올리는 중입니다. 다 올라간 뒤 다시 눌러 주세요.');
      return {
        photo_ids: rvPhotos.filter(p => p.state === 'ok').map(p => p.id),
        videos: rvVideos.filter(v => v.state === 'ok').map(v => ({ id: v.id, title: v.title })),
        clips: rvClips.map(c => ({ id: c.id, title: c.title })),
      };
    }

    async function reviewDraft(btn) {
      const guide = document.getElementById('rv_guide').value.trim();
      if (guide.length < 20) { alert('가이드 내용을 붙여 넣어 주세요.'); return; }
      if (document.getElementById('rv_content').value.trim() && !confirm('지금 원고를 새 원고로 바꿀까요?')) return;
      let media;
      try { media = rvMedia(); } catch (e) { alert(e.message); return; }
      const old = btn.innerText;
      btn.disabled = true; btn.innerText = '⏳ 가이드를 읽고 원고를 쓰는 중... (1~2분)';
      try {
        const d = await postJSON('/api/review/draft', {
          guide, brand: document.getElementById('rv_brand').value.trim(), notes: document.getElementById('rv_notes').value.trim(), ...media });
        document.getElementById('rv_title').value = d.title || '';
        document.getElementById('rv_content').value = (d.contents || []).join('\n');
        document.getElementById('rv_tags').value = (d.tags || []).join(', ');
        const cl = document.getElementById('rv-checklist');
        cl.style.display = (d.checklist || []).length ? 'block' : 'none';
        cl.innerHTML = '<b>가이드 반영 확인</b><br>' + (d.checklist || []).map(x => '✔ ' + esc(x)).join('<br>')
          + '<br><span style="color:#b45309;">발행 전에 원고를 꼭 한 번 읽고, 가이드와 다른 곳은 고쳐 주세요.</span>';
        rvCount();
      } catch (e) { alert(e.message || e); }
      finally { btn.disabled = false; btn.innerText = old; }
    }

    // ── 숏폼 클립 만들기 ──
    let clipScenes = [], clipMediaKeys = [], clipLast = null;
    function clipMediaThumb(key) {
      const m = /^(사진|영상)(\d+)$/.exec(key || '');
      if (m && m[1] === '사진' && rvPhotos[+m[2] - 1] && rvPhotos[+m[2] - 1].thumb) return `<img src="${rvPhotos[+m[2] - 1].thumb}">`;
      return `<span class="ph">${m && m[1] === '영상' ? '🎬' : '📷'}</span>`;
    }
    function clipKeys() {
      return rvPhotos.map((p, i) => '사진' + (i + 1)).concat(rvVideos.map((v, i) => '영상' + (i + 1)));
    }
    function renderClipScenes() {
      const keys = clipKeys();
      document.getElementById('clip-scenes').innerHTML = clipScenes.map((sc, i) => `<div class="clip-scene">`
        + clipMediaThumb(sc.media)
        + `<div><select onchange="clipScenes[${i}].media=this.value; renderClipScenes();">`
        + keys.map(k => `<option ${k === sc.media ? 'selected' : ''}>${k}</option>`).join('') + `</select>`
        + ` <small style="color:#94a3b8;">장면 ${i + 1}</small>`
        + `<input type="text" maxlength="40" placeholder="자막 (==강조==)" value="${esc(sc.caption)}" oninput="clipScenes[${i}].caption=this.value">`
        + `<input type="text" maxlength="120" placeholder="나레이션" value="${esc(sc.narration)}" oninput="clipScenes[${i}].narration=this.value"></div>`
        + `<div class="btns"><button type="button" onclick="clipMove(${i},-1)" ${i ? '' : 'disabled'}>▲</button>`
        + `<button type="button" onclick="clipMove(${i},1)" ${i < clipScenes.length - 1 ? '' : 'disabled'}>▼</button>`
        + `<button type="button" onclick="clipScenes.splice(${i},1); renderClipScenes();">✕</button></div></div>`).join('');
    }
    function clipManual() {
      const keys = clipKeys();
      if (!keys.length) { alert('사진이나 영상을 먼저 올려 주세요.'); return; }
      if (clipScenes.length && !confirm('지금 대본을 지우고 사진·영상마다 빈 장면으로 새로 시작할까요?')) return;
      clipScenes = keys.slice(0, 20).map(k => ({ media: k, caption: '', narration: '' }));
      document.getElementById('clip-editor').style.display = 'block';
      renderClipScenes();
    }
    function clipMove(i, d) {
      const j = i + d;
      if (j < 0 || j >= clipScenes.length) return;
      [clipScenes[i], clipScenes[j]] = [clipScenes[j], clipScenes[i]];
      renderClipScenes();
    }
    function clipAddScene() {
      const keys = clipKeys();
      if (!keys.length) { alert('사진이나 영상을 먼저 올려 주세요.'); return; }
      if (clipScenes.length >= 20) { alert('장면은 20개까지입니다.'); return; }
      clipScenes.push({ media: keys[Math.min(clipScenes.length, keys.length - 1)], caption: '', narration: '' });
      renderClipScenes();
    }
    function clipMediaPayload() {
      const media = rvMedia();
      if (!media.photo_ids.length && !media.videos.length) throw new Error('사진이나 영상을 먼저 올려 주세요.');
      if (rvPhotos.some(p => p.state === 'up') || rvVideos.some(v => v.state === 'up')) throw new Error('사진·영상을 올리는 중입니다. 다 올라간 뒤 눌러 주세요.');
      return { photo_ids: media.photo_ids, videos: media.videos };
    }
    async function clipScript(btn) {
      let media;
      try { media = clipMediaPayload(); } catch (e) { alert(e.message); return; }
      if (clipScenes.length && !confirm('지금 대본을 새 대본으로 바꿀까요?')) return;
      const old = btn.innerText;
      btn.disabled = true; btn.innerText = '⏳ AI가 장면을 보고 대본을 쓰는 중... (30초~1분)';
      try {
        const d = await postJSON('/api/review/clip_script', {
          ...media, seconds: +document.getElementById('clip_seconds').value, tone: document.getElementById('clip_tone').value,
          guide: document.getElementById('rv_guide').value, notes: document.getElementById('rv_notes').value,
          brand: document.getElementById('rv_brand').value, title: document.getElementById('rv_title').value,
          content: document.getElementById('rv_content').value });
        const sc = d.script || {};
        document.getElementById('clip_title1').value = (sc.title_lines || [])[0] || '';
        document.getElementById('clip_title2').value = (sc.title_lines || [])[1] || '';
        clipScenes = (sc.scenes || []).map(x => ({ media: x.media, caption: x.caption || '', narration: x.narration || '' }));
        document.getElementById('clip-editor').style.display = 'block';
        renderClipScenes();
      } catch (e) { alert(e.message || e); }
      finally { btn.disabled = false; btn.innerText = old; }
    }
    async function clipMake(btn) {
      let media;
      try { media = clipMediaPayload(); } catch (e) { alert(e.message); return; }
      const scenes = clipScenes.filter(x => (x.caption || '').trim() || (x.narration || '').trim());
      if (!scenes.length) { alert('장면을 1개 이상 넣어 주세요.'); return; }
      const voice = document.getElementById('clip_voice').value;
      if (voice !== 'none' && scenes.some(x => !(x.narration || '').trim()) && !confirm('나레이션이 빈 장면은 자막만 나옵니다. 계속할까요?')) return;
      btn.disabled = true;
      const st = document.getElementById('clip-status');
      st.style.display = 'block'; st.innerText = '⏳ 클립 만들기 시작...';
      document.getElementById('clip-result').style.display = 'none';
      try {
        await postJSON('/api/review/clip_make', {
          ...media, voice, tone: document.getElementById('clip_tone').value,
          sponsor: document.getElementById('clip_sponsor').checked ? document.getElementById('clip_sponsor_text').value.trim() : '',
          script: { title_lines: [document.getElementById('clip_title1').value, document.getElementById('clip_title2').value].filter(x => x.trim()), scenes } });
      } catch (e) { alert(e.message || e); btn.disabled = false; st.style.display = 'none'; return; }
      const tick = async () => {
        let d;
        try { d = await postJSON('/api/review/clip_status', {}); } catch (e) { return setTimeout(tick, 3000); }
        if (d.state === 'running') { st.innerText = `⏳ ${d.msg || '만드는 중...'} (${d.pct || 0}%)`; return setTimeout(tick, 1500); }
        btn.disabled = false;
        if (d.state === 'done' && d.result) {
          clipLast = d.result;
          st.innerHTML = `✅ 클립 완성 — ${d.result.seconds}초 · 장면 ${d.result.scenes}개`;
          const v = document.getElementById('clip-video');
          v.src = '/api/review/clip_file?id=' + encodeURIComponent(d.result.id) + '&_=' + Date.now() + '#t=0.1';
          document.getElementById('clip-result').style.display = 'block';
          document.getElementById('clip-saved').innerText = d.result.saved ? '저장 위치: ' + d.result.saved : '';
        } else {
          st.innerHTML = '<span style="color:#dc2626;">❌ ' + esc(d.error || '클립을 만들지 못했습니다.') + '</span>';
        }
      };
      setTimeout(tick, 1200);
    }
    function clipAddToPost() {
      if (!clipLast) return;
      if (rvVideos.some(v => v.id === clipLast.id)) { alert('이미 영상 목록에 넣었습니다.'); return; }
      if (rvVideos.length >= RV_MAX_VIDEOS) { alert(`영상은 최대 ${RV_MAX_VIDEOS}개까지입니다.`); return; }
      rvVideos.push({ key: ++rvSeq, name: clipLast.name, id: clipLast.id, title: (clipLast.title || '숏폼 클립').slice(0, 40), state: 'ok', pct: 100, msg: '' });
      renderReviewVideos();
      renderClipScenes();
      alert(`영상 ${rvVideos.length}번으로 넣었습니다. 본문에 [영상:${rvVideos.length}] 자리를 넣으면 그 자리에 들어갑니다.`);
    }

    async function reviewPublish(btn) {
      const title = document.getElementById('rv_title').value.trim();
      const content = document.getElementById('rv_content').value;
      if (!title || !content.trim()) { alert('제목과 본문을 넣어 주세요.'); return; }
      const n = rvCount();
      if (rvMode === 'guide' && n < RV_MIN_CHARS) { alert(`가이드 원고는 본문 ${RV_MIN_CHARS.toLocaleString()}자 이상이어야 합니다. (지금 ${n.toLocaleString()}자)`); return; }
      let media;
      try { media = rvMedia(); } catch (e) { alert(e.message); return; }
      const schedule_at = document.getElementById('rv_schedule').value;
      const msg = `${schedule_at ? schedule_at.replace('T', ' ') + ' 예약 발행' : '바로 발행'}합니다.\n사진 ${media.photo_ids.length}장 · 영상 ${media.videos.length}개 · 클립 ${media.clips.length}개 (100토큰)`;
      if (!confirm(msg)) return;
      btn.disabled = true;
      const st = document.getElementById('rv-status');
      try {
        await postJSON('/api/review/publish', {
          mode: rvMode, title, content, tags: document.getElementById('rv_tags').value,
          category: document.getElementById('post_category').value.trim(),
          center: document.getElementById('rv_center').checked, public_open: document.getElementById('rv_public').checked,
          schedule_at, ...media });
        st.innerText = '⏳ 발행 중... (영상이 있으면 업로드 시간만큼 더 걸립니다)';
        const tick = async () => {
          try {
            const d = await (await fetch('/api/logs')).json();
            const t = d.task || {};
            if (t.status === 'running') { st.innerText = '⏳ ' + (t.message || '발행 중...'); return setTimeout(tick, 3000); }
            if (t.status === 'success') st.innerHTML = '✅ ' + esc(t.message || '완료') + (t.url ? ` <a href="${esc(t.url)}" target="_blank">글 보기</a>` : '');
            else st.innerHTML = '<span style="color:#dc2626;">❌ ' + esc(t.message || '실패') + '</span>';
            btn.disabled = false;
          } catch (e) { setTimeout(tick, 5000); }
        };
        setTimeout(tick, 3000);
      } catch (e) { alert(e.message || e); btn.disabled = false; }
    }

    function restoreMainTab() {
      try {
        const saved = localStorage.getItem('autolog_main_tab');
        if (saved === 'neighbor' || saved === 'review') switchMainTab(saved);
      } catch (e) {}
    }

    // ── N사 자동 이웃 순회 관리 스크립트 ──
    let neighborMode = 'feed';
    let neighborStatus = { state: 'idle' };

    const DEFAULT_BUDDY_TEMPLATES = [
      "안녕하세요 {닉네임}님! 정성스러운 포스팅 잘 보고 갑니다. 관심사가 비슷해 자주 소통하고 싶어 신청드려요 :)",
      "{닉네임}님 올려주신 유익한 포스팅 재미있게 읽었습니다. 앞으로 좋은 이웃으로 친하게 지내요~",
      "{닉네임}님 피드 글들이 알차서 인상 깊네요! 함께 진솔하게 소통하는 이웃이 되고 싶어 신청합니다 ^^",
      "안녕하세요 {닉네임}님, 좋은 글 공유 감사드려요. 서로 긍정적인 자극 주고받는 찐이웃 되고 싶어요!",
      "{닉네임}님 글 꼼꼼히 읽고 공감 누르고 갑니다! 앞으로도 자주 왕래하며 따뜻하게 소통해요 :)",
      "포스팅 분위기가 너무 좋아서 한참 머물다 갑니다 {닉네임}님! 서로이웃 맺고 일상 함께 나누고 싶어요~",
      "안녕하세요 {닉네임}님! 알찬 꿀팁 가득한 글 잘 보았습니다. 서로 응원하며 소통하는 서로이웃 부탁드려요!",
      "{닉네임}님 반가워요! 진정성 있는 포스팅에 반해 이웃 신청드립니다. 자주 찾아뵙고 소통할게요 :)"
    ];

    function updateNeighborAccountSelect(selectedId) {
      const sel = document.getElementById('nb_account_select');
      if (!sel) return;
      const curVal = (selectedId !== undefined) ? selectedId : (sel.value || '');
      let opts = '<option value="">(기본) 현재 활성 계정 사용</option>';
      (accountState.accounts || []).forEach((a, idx) => {
        const slotNum = a.slot || (idx + 1);
        const isAct = (a.naver_id === accountState.active) ? ' (현재 활성)' : '';
        const logStat = a.logged_in ? '🟢 로그인됨' : '⚪ 로그인 필요';
        opts += `<option value="${esc(a.naver_id)}">[${slotNum}번] ${esc(a.naver_id)}${isAct} [${logStat}]</option>`;
      });
      sel.innerHTML = opts;
      if (curVal !== undefined) {
        sel.value = curVal;
      }
      updateNeighborAccountStatusText();
    }

    function updateNeighborAccountStatusText() {
      const sel = document.getElementById('nb_account_select');
      const dot = document.getElementById('nb-account-dot');
      const txt = document.getElementById('nb-account-text');
      if (!sel || !txt || !dot) return;

      const chosenId = sel.value.trim();
      const effId = chosenId || accountState.active || '';
      const acc = (accountState.accounts || []).find(a => a.naver_id === effId);
      const slotNum = (acc && acc.slot) ? acc.slot : 1;
      const isDiff = chosenId && chosenId !== accountState.active;
      const isDefault = !chosenId;

      if (!effId) {
        dot.className = 'dot';
        txt.innerHTML = '등록된 N사 계정이 없습니다. 먼저 1번 탭에서 계정을 추가해주세요.';
        return;
      }

      const accountLabel = isDefault ? `(기본 활성 계정) <b>${esc(effId)}</b> [${slotNum}번]` : `선택 계정: <b>${esc(effId)}</b> [${slotNum}번]`;

      if (acc && acc.logged_in) {
        dot.className = 'dot on';
        txt.innerHTML = `순회 계정: ${accountLabel} 🟢 로그인 정상` +
          (isDiff ? ' — <span style="color:#2563eb; font-weight:700;">✨ 발행 계정과 분리됨 (독립 병렬 실행 가능)</span>' : ' — (발행 계정과 동일)');
      } else {
        dot.className = 'dot';
        txt.innerHTML = `순회 계정: ${accountLabel} ⚪ 로그인 필요 (이웃 순회 전 로그인이 필요합니다)` +
          (isDiff ? ' — <span style="color:#2563eb; font-weight:700;">✨ 발행 계정과 분리됨</span>' : '');
      }
    }

    let nbAccountLimits = {};
    let nbSharedLimits = {sympathy: 100, comment: 50, buddy_request: 50};
    function nbCurrentAccountKey() {
      const sel = document.getElementById('nb_account_select');
      return (sel && sel.value) || accountState.active || '';
    }
    function readNbLimitInputs() {
      const n = (id, d) => { const v = parseInt(document.getElementById(id).value); return isNaN(v) ? d : Math.max(0, v); };
      return {sympathy: n('nb_lim_sympathy', 100), comment: n('nb_lim_comment', 50), buddy_request: n('nb_lim_buddy', 50)};
    }
    function showNbLimits() {
      const key = nbCurrentAccountKey();
      const lim = Object.assign({}, nbSharedLimits, nbAccountLimits[key] || {});
      document.getElementById('nb_lim_sympathy').value = lim.sympathy;
      document.getElementById('nb_lim_comment').value = lim.comment;
      document.getElementById('nb_lim_buddy').value = lim.buddy_request;
      document.getElementById('nb-limit-acct').innerText = key || '선택한 계정';
    }
    let nbLimitKey = '';
    function onNeighborAccountChange() {
      // 바꾸기 전 계정의 한도를 먼저 보관한 뒤 새 계정 값을 표시
      if (nbLimitKey) nbAccountLimits[nbLimitKey] = readNbLimitInputs();
      showNbLimits();
      nbLimitKey = nbCurrentAccountKey();
      updateNeighborAccountStatusText();
      saveNeighborConfig(false);
    }

    function updateBuddyModeUI() {
      const mode = document.getElementById('nb_buddy_mode').value;
      const customBox = document.getElementById('nb-buddy-custom-box');
      const tmplBox = document.getElementById('nb-buddy-templates-box');
      const aiBox = document.getElementById('nb-buddy-ai-box');
      if (customBox) customBox.style.display = (mode === 'custom') ? 'block' : 'none';
      if (tmplBox) tmplBox.style.display = (mode === 'templates' || mode === 'mixed') ? 'block' : 'none';
      if (aiBox) aiBox.style.display = (mode === 'ai' || mode === 'mixed') ? 'block' : 'none';
    }

    function resetBuddyTemplates() {
      const ta = document.getElementById('nb_buddy_templates');
      if (ta) ta.value = DEFAULT_BUDDY_TEMPLATES.join('\n');
      saveNeighborConfig(false);
    }

    function setNeighborMode(mode) {
      neighborMode = mode;
      ['feed', 'keyword', 'target', 'recommend'].forEach(m => {
        const btn = document.getElementById(`nb-mode-${m}`);
        if (btn) btn.classList.toggle('active', m === mode);
      });
      const kwBox = document.getElementById('nb-keyword-box');
      if (kwBox) kwBox.style.display = (mode === 'keyword') ? 'block' : 'none';
      const tgBox = document.getElementById('nb-target-box');
      if (tgBox) tgBox.style.display = (mode === 'target') ? 'block' : 'none';
    }

    async function loadNeighborConfig() {
      try {
        const res = await fetch('/api/neighbor/config');
        if (!res.ok) return;
        const cfg = await res.json();
        
        updateNeighborAccountSelect(cfg.account_id || '');
        nbAccountLimits = cfg.account_limits || {};
        nbSharedLimits = Object.assign(nbSharedLimits, cfg.daily_limits || {});
        showNbLimits();
        nbLimitKey = nbCurrentAccountKey();
        setNeighborMode(cfg.discovery_mode || 'feed');
        if (cfg.keywords && Array.isArray(cfg.keywords)) {
          document.getElementById('nb_keywords').value = cfg.keywords.join(', ');
        }
        if (cfg.target_blogs && Array.isArray(cfg.target_blogs)) {
          const tbInput = document.getElementById('nb_target_blogs');
          if (tbInput) tbInput.value = cfg.target_blogs.join(', ');
        }
        
        const en = cfg.enabled_actions || {};
        document.getElementById('nb_act_sympathy').checked = en.sympathy !== false;
        document.getElementById('nb_act_comment').checked = en.comment !== false;
        document.getElementById('nb_act_buddy').checked = en.buddy_request !== false;

        const pr = cfg.probabilities || {};
        document.getElementById('nb_prob_sympathy').value = Math.round((pr.sympathy ?? 0.9) * 100);
        document.getElementById('nb_prob_comment').value = Math.round((pr.comment ?? 0.4) * 100);
        document.getElementById('nb_prob_buddy').value = Math.round((pr.buddy_request ?? 0.35) * 100);

        if (cfg.comment_mode) document.getElementById('nb_comment_mode').value = cfg.comment_mode;
        if (cfg.buddy_mode) document.getElementById('nb_buddy_mode').value = cfg.buddy_mode;
        if (cfg.buddy_message_template) document.getElementById('nb_buddy_msg').value = cfg.buddy_message_template;

        const bTemplates = (cfg.buddy_templates && cfg.buddy_templates.length > 0) ? cfg.buddy_templates : DEFAULT_BUDDY_TEMPLATES;
        const ta = document.getElementById('nb_buddy_templates');
        if (ta) ta.value = bTemplates.join('\n');

        updateBuddyModeUI();
        
        const dl = cfg.delays || {};
        if (dl.min_seconds) document.getElementById('nb_delay_min').value = dl.min_seconds;
        if (dl.max_seconds) document.getElementById('nb_delay_max').value = dl.max_seconds;
        if (cfg.stall_minutes != null) document.getElementById('nb_stall_min').value = cfg.stall_minutes;
        document.getElementById('nb_headless').checked = cfg.auto_headless !== false;
      } catch (e) {
        console.error('이웃 순회 설정 로드 실패:', e);
      }
    }

    function gatherNeighborConfig() {
      const kwStr = document.getElementById('nb_keywords').value.trim();
      const kwList = kwStr ? kwStr.split(',').map(s => s.trim()).filter(Boolean) : ['일상', '맛집', '소통'];
      const tgStr = (document.getElementById('nb_target_blogs') ? document.getElementById('nb_target_blogs').value : '').trim();
      const tgList = tgStr ? tgStr.split(',').map(s => s.trim()).filter(Boolean) : [];
      const rawTemplates = (document.getElementById('nb_buddy_templates').value || '').split('\n').map(s => s.trim()).filter(Boolean);
      const bTemplates = rawTemplates.length > 0 ? rawTemplates : DEFAULT_BUDDY_TEMPLATES;
      const accSel = document.getElementById('nb_account_select');

      return {
        account_id: accSel ? accSel.value.trim() : '',
        daily_limits: nbSharedLimits,
        account_limits: (() => { const k = nbCurrentAccountKey(); const m = Object.assign({}, nbAccountLimits); if (k) m[k] = readNbLimitInputs(); nbAccountLimits = m; return m; })(),
        discovery_mode: neighborMode,
        keywords: kwList,
        target_blogs: tgList,
        enabled_actions: {
          sympathy: document.getElementById('nb_act_sympathy').checked,
          comment: document.getElementById('nb_act_comment').checked,
          buddy_request: document.getElementById('nb_act_buddy').checked,
        },
        probabilities: {
          sympathy: Math.min(1.0, Math.max(0.05, parseInt(document.getElementById('nb_prob_sympathy').value || '90') / 100)),
          comment: Math.min(1.0, Math.max(0.05, parseInt(document.getElementById('nb_prob_comment').value || '40') / 100)),
          buddy_request: Math.min(1.0, Math.max(0.05, parseInt(document.getElementById('nb_prob_buddy').value || '35') / 100)),
        },
        comment_mode: document.getElementById('nb_comment_mode').value,
        buddy_mode: document.getElementById('nb_buddy_mode').value,
        buddy_message_template: document.getElementById('nb_buddy_msg').value.trim(),
        buddy_templates: bTemplates,
        delays: {
          min_seconds: Math.max(40, parseInt(document.getElementById('nb_delay_min').value || '45')),
          max_seconds: Math.max(70, parseInt(document.getElementById('nb_delay_max').value || '90')),
        },
        auto_headless: document.getElementById('nb_headless').checked,
        stall_minutes: Math.max(0, parseInt(document.getElementById('nb_stall_min').value || '10')),
      };
    }

    async function runCancelUnanswered() {
      const btn = document.getElementById('nb-cancel-btn');
      const resDiv = document.getElementById('nb-cancel-result');
      const days = parseInt(document.getElementById('nb_cancel_days').value || '7');
      const accSel = document.getElementById('nb_account_select');
      const accountId = accSel ? accSel.value.trim() : '';

      const dayText = days === 0 ? '모든 보낸 신청' : `${days}일 이상 지난 무응답 신청`;
      if (days < 1) { alert('기준 일수는 1일 이상으로 정해 주세요.'); return; }
      resDiv.style.display = 'block'; resDiv.style.background = '#f1f5f9'; resDiv.style.color = '#334155';
      resDiv.innerText = '보낸 신청 목록을 확인하는 중입니다 (신청이 많으면 2~3분 걸립니다)...'; btn.disabled = true;
      let pv; try { pv = await postJSON('/api/neighbor/cancel_sent_requests', { account_id: accountId, older_than_days: days, dry_run: true }); } finally { btn.disabled = false; }
      if (!pv.ok) { resDiv.style.background = '#fef2f2'; resDiv.style.color = '#991b1b'; resDiv.innerHTML = `❌ <b>실패:</b> ${pv.message}`; return; }
      resDiv.innerText = pv.message;
      if (!pv.target_count) return;
      if (!confirm(`${pv.message.replace(' (미리 보기)', '')}\n\n이 ${pv.target_count}건을 취소할까요?`)) return;

      btn.disabled = true;
      btn.innerText = '취소 처리 중...';
      resDiv.style.display = 'block';
      resDiv.style.background = '#f1f5f9';
      resDiv.style.color = '#334155';
      resDiv.innerText = '보낸 신청을 취소하는 중입니다 (건수가 많으면 몇 분 걸립니다)...';

      try {
        const res = await postJSON('/api/neighbor/cancel_sent_requests', {
          account_id: accountId,
          older_than_days: days
        });
        if (res.ok) {
          resDiv.style.background = '#ecfdf5';
          resDiv.style.color = '#065f46';
          resDiv.innerHTML = `✅ <b>정리 완료!</b> ${res.message}`;
        } else {
          resDiv.style.background = '#fef2f2';
          resDiv.style.color = '#991b1b';
          resDiv.innerHTML = `❌ <b>실패:</b> ${res.message}`;
        }
      } catch (e) {
        resDiv.style.background = '#fef2f2';
        resDiv.style.color = '#991b1b';
        resDiv.innerHTML = `❌ <b>오류:</b> ${e.message}`;
      } finally {
        btn.disabled = false;
        btn.innerText = '🧹 무응답 신청 취소 실행';
      }
    }

    async function runCleanGhostBuddies() {
      const btn = document.getElementById('nb-ghost-btn');
      const resDiv = document.getElementById('nb-ghost-result');
      const months = parseInt(document.getElementById('nb_ghost_months').value || '6');
      const wlRaw = document.getElementById('nb_ghost_whitelist').value || '';
      const whitelist = wlRaw.split(',').map(s => s.trim()).filter(Boolean);
      const accSel = document.getElementById('nb_account_select');
      const accountId = accSel ? accSel.value.trim() : '';

      const mText = months === 0 ? '모든 이웃 (보호 대상 제외)' : `${months}개월 이상 미활동 이웃`;
      if (months < 1) { alert('기준은 1개월 이상으로 정해 주세요.'); return; }
      resDiv.style.display = 'block'; resDiv.style.background = '#f1f5f9'; resDiv.style.color = '#334155';
      resDiv.innerText = '이웃 목록 전체에서 새 글이 오래 없는 이웃을 찾는 중입니다 (이웃 4,000명 기준 약 6분)...'; btn.disabled = true;
      let pv; try { pv = await postJSON('/api/neighbor/clean_ghost_buddies', { account_id: accountId, inactive_months: months, whitelist, dry_run: true }); } finally { btn.disabled = false; }
      if (!pv.ok) { resDiv.style.background = '#fef2f2'; resDiv.style.color = '#991b1b'; resDiv.innerHTML = `❌ <b>실패:</b> ${pv.message}`; return; }
      resDiv.innerHTML = pv.message + (pv.sample && pv.sample.length ? `<br><small>예: ${pv.sample.slice(0, 5).join(', ')}</small>` : '');
      if (!pv.target_count) return;
      if (!confirm(`${pv.message.replace(' (미리 보기)', '')}\n\n이 ${pv.target_count}명을 이웃에서 삭제할까요? 삭제한 이웃은 되돌릴 수 없습니다.`)) return;

      btn.disabled = true;
      btn.innerText = '정리 처리 중...';
      resDiv.style.display = 'block';
      resDiv.style.background = '#f1f5f9';
      resDiv.style.color = '#334155';
      resDiv.innerHTML = '이웃을 정리하는 중입니다 (몇 분 걸릴 수 있습니다)...';

      try {
        const res = await postJSON('/api/neighbor/clean_ghost_buddies', {
          account_id: accountId,
          inactive_months: months,
          whitelist: whitelist
        });
        if (res.ok) {
          resDiv.style.background = '#ecfdf5';
          resDiv.style.color = '#065f46';
          resDiv.innerHTML = `✅ <b>정리 완료!</b> ${res.message}`;
        } else {
          resDiv.style.background = '#fef2f2';
          resDiv.style.color = '#991b1b';
          resDiv.innerHTML = `❌ <b>실패:</b> ${res.message}`;
        }
      } catch (e) {
        resDiv.style.background = '#fef2f2';
        resDiv.style.color = '#991b1b';
        resDiv.innerHTML = `❌ <b>오류:</b> ${e.message}`;
      } finally {
        btn.disabled = false;
        btn.innerText = '👻 유령 이웃 정리 실행';
      }
    }

    async function runManageReceivedRequests() {
      const btn = document.getElementById('nb-recv-btn');
      const resDiv = document.getElementById('nb-recv-result');
      const actionType = document.getElementById('nb_recv_action').value || 'accept_filtered';
      const rejectDefault = document.getElementById('nb_recv_reject_default').checked;
      const kwRaw = document.getElementById('nb_recv_keywords').value || '';
      const keywords = kwRaw.split(',').map(s => s.trim()).filter(Boolean);
      const accSel = document.getElementById('nb_account_select');
      const accountId = accSel ? accSel.value.trim() : '';

      resDiv.style.display = 'block'; resDiv.style.background = '#f1f5f9'; resDiv.style.color = '#334155';
      resDiv.innerText = '받은 신청을 확인하는 중입니다...'; btn.disabled = true;
      let pv; try { pv = await postJSON('/api/neighbor/manage_received_requests', { account_id: accountId, action_type: actionType, reject_default_message: rejectDefault, spam_keywords: keywords, dry_run: true }); } finally { btn.disabled = false; }
      if (!pv.ok) { resDiv.style.background = '#fef2f2'; resDiv.style.color = '#991b1b'; resDiv.innerHTML = `❌ <b>실패:</b> ${pv.message}`; return; }
      resDiv.innerText = pv.message;
      if (!pv.accept_target && !pv.reject_target) return;
      if (!confirm(`${pv.message.replace(' (미리 보기)', '')}\n\n이대로 처리할까요?`)) return;

      btn.disabled = true;
      btn.innerText = '관리 처리 중...';
      resDiv.style.display = 'block';
      resDiv.style.background = '#f1f5f9';
      resDiv.style.color = '#334155';
      resDiv.innerHTML = '받은 신청을 처리하는 중입니다...';

      try {
        const res = await postJSON('/api/neighbor/manage_received_requests', {
          account_id: accountId,
          action_type: actionType,
          reject_default_message: rejectDefault,
          spam_keywords: keywords
        });
        if (res.ok) {
          resDiv.style.background = '#ecfdf5';
          resDiv.style.color = '#065f46';
          resDiv.innerHTML = `✅ <b>처리 완료!</b> ${res.message}`;
        } else {
          resDiv.style.background = '#fef2f2';
          resDiv.style.color = '#991b1b';
          resDiv.innerHTML = `❌ <b>실패:</b> ${res.message}`;
        }
      } catch (e) {
        resDiv.style.background = '#fef2f2';
        resDiv.style.color = '#991b1b';
        resDiv.innerHTML = `❌ <b>오류:</b> ${e.message}`;
      } finally {
        btn.disabled = false;
        btn.innerText = '📬 받은 신청 자동 관리 실행';
      }
    }

    async function saveNeighborConfig(showAlert = false) {
      const cfg = gatherNeighborConfig();
      try {
        await postJSON('/api/neighbor/config', cfg);
        if (showAlert) alert('이웃 순회 설정이 저장되었습니다.');
      } catch (e) {
        alert('설정 저장 실패: ' + e.message);
      }
    }

    async function startNeighbor() {
      const cfg = gatherNeighborConfig();
      try {
        const res = await postJSON('/api/neighbor/start', cfg);
        if (res.ok) {
          pollNeighborStatus();
        } else {
          alert(res.message);
        }
      } catch (e) {
        alert(e.message);
      }
    }

    async function stopNeighbor() {
      try {
        const res = await postJSON('/api/neighbor/stop');
        if (res.ok) {
          pollNeighborStatus();
        } else {
          alert(res.message);
        }
      } catch (e) {
        alert(e.message);
      }
    }

    async function pauseNeighbor() {
      try {
        const res = await postJSON('/api/neighbor/pause');
        if (res.ok) {
          pollNeighborStatus();
        } else {
          alert(res.message);
        }
      } catch (e) {
        alert(e.message);
      }
    }

    async function pollNeighborStatus() {
      try {
        const res = await fetch('/api/neighbor/status');
        if (!res.ok) return;
        const data = await res.json();
        neighborStatus = data;
        renderNeighborStatus(data);
      } catch (e) {}
    }

    let lastStopReasonAt = 0;
    function renderNeighborStatus(data) {
      const sr = document.getElementById('nb-stop-reason');
      if (sr) {
        if (data.stop_reason && (data.state || 'idle') === 'idle') {
          sr.style.display = 'block'; sr.innerText = '⏹ ' + data.stop_reason;
          if (data.stop_reason_at && data.stop_reason_at !== lastStopReasonAt && lastStopReasonAt !== 0) { if (typeof toast === "function") toast(data.stop_reason); else alert(data.stop_reason); }
          lastStopReasonAt = data.stop_reason_at || lastStopReasonAt;
        } else if ((data.state || 'idle') === 'running') { sr.style.display = 'none'; }
        if (!lastStopReasonAt) lastStopReasonAt = data.stop_reason_at || -1;
      }
      const badge = document.getElementById('neighbor-status-badge');
      const startBtn = document.getElementById('nb-start-btn');
      const pauseBtn = document.getElementById('nb-pause-btn');
      const stopBtn = document.getElementById('nb-stop-btn');
      const curTarget = document.getElementById('nb-cur-target');

      const state = data.state || 'idle';
      badge.className = `neighbor-status-badge neighbor-status-${state}`;
      const tabInd = document.getElementById('tab-neighbor-indicator');
      if (tabInd) tabInd.style.display = (state === 'running') ? 'inline-block' : 'none';
      
      if (state === 'running') {
        badge.innerHTML = `<span class="pulse-dot"></span><span>순회 중</span>`;
        startBtn.style.display = 'none';
        pauseBtn.style.display = 'inline-flex';
        pauseBtn.innerText = '⏸ 일시정지';
        stopBtn.style.display = 'inline-flex';
        stopBtn.disabled = false;
      } else if (state === 'paused') {
        badge.innerHTML = `<span>일시 정지됨</span>`;
        startBtn.style.display = 'none';
        pauseBtn.style.display = 'inline-flex';
        pauseBtn.innerText = '▶ 재개하기';
        stopBtn.style.display = 'inline-flex';
        stopBtn.disabled = false;
      } else if (state === 'stopping') {
        badge.innerHTML = `<span>정지 중...</span>`;
        startBtn.style.display = 'none';
        pauseBtn.style.display = 'none';
        stopBtn.style.display = 'inline-flex';
        stopBtn.disabled = true;
      } else {
        badge.innerHTML = `<span>대기 중</span>`;
        startBtn.style.display = 'inline-flex';
        pauseBtn.style.display = 'none';
        stopBtn.style.display = 'none';
        stopBtn.disabled = false;
      }

      if (data.current_target) {
        curTarget.innerText = `현재 타깃: ${data.current_target.blog_id} (${data.current_target.title || ''})`;
      } else {
        curTarget.innerText = '';
      }

      // 통계 카운터 렌더링
      const counts = data.today_counts || {};
      const limits = data.daily_limits || {};
      document.getElementById('stat-sympathy').innerText = `${counts.sympathy || 0} / ${limits.sympathy ?? 100}`;
      document.getElementById('stat-comment').innerText = `${counts.comment || 0} / ${limits.comment ?? 50}`;
      document.getElementById('stat-buddy').innerText = `${counts.buddy_request || 0} / ${limits.buddy_request ?? 50}`;
      document.getElementById('stat-total').innerText = `${counts.total_visited || 0}건`;

      // 로그 콘솔 렌더링
      const logBox = document.getElementById('neighbor-console');
      if (data.recent_logs && data.recent_logs.length > 0) {
        logBox.innerHTML = data.recent_logs.map(l => 
          `<div class="neighbor-log-line neighbor-log-${esc(l.level)}"><span class="neighbor-log-time">[${esc(l.time)}]</span>${esc(l.msg)}</div>`
        ).join('');
        logBox.scrollTop = logBox.scrollHeight;
      }
    }

    async function loadNeighborHistory() {
      try {
        const res = await fetch('/api/neighbor/history');
        if (!res.ok) return;
        const items = await res.json();
        const tbody = document.getElementById('neighbor-history-body');
        if (!items || items.length === 0) {
          tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color:var(--text-muted); padding:20px;">순회 이력이 없습니다.</td></tr>';
          return;
        }
        tbody.innerHTML = items.slice(-50).reverse().map(it => {
          const acts = it.actions || [];
          let actBadges = '';
          if (acts.includes('sympathy')) actBadges += '<span class="badge-act badge-sympathy">공감</span>';
          if (acts.includes('comment')) actBadges += '<span class="badge-act badge-comment">댓글</span>';
          if (acts.includes('buddy_request')) actBadges += '<span class="badge-act badge-buddy">서이추</span>';
          if (!actBadges) actBadges = '<span style="color:var(--text-muted); font-size:11px;">방문</span>';

          const titleLink = safeUrl(it.url) 
            ? `<a href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener noreferrer" style="color:var(--text-main); text-decoration:none; font-weight:600;">${esc(it.title || it.log_no)}</a>`
            : esc(it.title || it.log_no);

          return `<tr>
            <td style="color:var(--text-muted);">${esc(it.time || '')}</td>
            <td style="font-weight:600; color:#334155;">${esc(it.author || it.blog_id)}</td>
            <td style="max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${titleLink}</td>
            <td>${actBadges}</td>
          </tr>`;
        }).join('');
      } catch (e) {}
    }

    async function clearNeighborHistory() {
      if (!confirm('순회 이력을 모두 비우시겠습니까?')) return;
      try {
        await postJSON('/api/neighbor/history/clear');
        loadNeighborHistory();
      } catch (e) {
        alert(e.message);
      }
    }

    // 시작: 라이선스 확인 → 통과하면 화면 데이터 불러오기. 1분마다 남은 토큰·기간 갱신
    refreshLicense();
    setInterval(() => { if (licenseOK) refreshLicense(); }, 60000);
    setInterval(pollLogs, 1000);
    setInterval(() => { if (licenseOK) pollNeighborStatus(); }, 2000);
  </script>
</body>
</html>
"""

# ── 로컬 대시보드 보안 ──
# 127.0.0.1 바인딩만으로는 부족: 사용자가 연 다른 웹사이트가 브라우저를 통해 이 서버로 요청을 보낼 수 있음(CSRF),
# DNS 리바인딩으로 응답을 읽을 수도 있음. → Host/Origin/Sec-Fetch-Site/Content-Type 검사로 같은 출처 요청만 허용.
ALLOWED_HOSTS = set()
ALLOWED_ORIGINS = set()


def set_allowed_port(port: int):
    global PORT, ALLOWED_HOSTS, ALLOWED_ORIGINS
    PORT = port
    ALLOWED_HOSTS = {f"127.0.0.1:{port}", f"localhost:{port}"}
    ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}


set_allowed_port(PORT)
# 실행할 때마다 새로 만드는 접속 열쇠: 프로그램 창(주소의 ?t=)으로만 전달 → 쿠키로 보관.
# 같은 PC의 다른 Windows 사용자·프로그램이 127.0.0.1 로 이 서버를 조작하지 못하게 함
API_TOKEN = secrets.token_urlsafe(24)
TOKEN_COOKIE = "nbs_t"
# 라이선스 없이도 쓸 수 있는 API (라이선스 입력·업데이트·창 제어)
LICENSE_FREE = {"/api/ping", "/api/license/status", "/api/license/activate", "/api/license/logout",
                "/api/update/status", "/api/update/check", "/api/update/install", "/api/update/ack",
                "/api/focus", "/api/app/close", "/api/logs"}
MAX_BODY_BYTES = 1_000_000
MAX_UPLOAD_BYTES = 12_000_000   # 사진 1장 업로드 (화면에서 1600px로 줄여 보내므로 보통 1MB 이하)
# 화면에서 고를 수 있는 글 구성 템플릿 8종 (ai.STYLE_GUIDES와 같아야 함 — 예전엔 3종만 허용돼 새 템플릿이 정보형으로 바뀌었음)
AI_STYLES = {"info", "review", "tips", "compare", "howto", "qna", "checklist", "issue", "shopping"}

# 로그인·발행·설치는 같은 브라우저 프로필/환경을 쓰므로 동시에 하나만 실행
TASK_LOCK = threading.Lock()


def start_exclusive_task(target, *args, **kwargs) -> bool:
    try:
        import neighbor_service
        nb = neighbor_service.NeighborBot.get_instance()
        if nb.state in ("running", "paused"):
            nb_acc = getattr(nb, "current_account", "") or ""
            nb_profile = getattr(nb, "current_profile_dir", None)

            if target == run_install_task:
                logger.warning("이웃 순회가 실행 중입니다. 라이브러리 설치 전 이웃 순회를 먼저 정지해 주세요.")
                return False

            # 대상 계정 확인
            cfg = config_mod.load_config()
            target_acc = kwargs.get("account") or cfg.get("active_account") or ""
            target_profile = config_mod.account_profile_dir(cfg, target_acc)

            # 같은 프로필(동일 계정)인 경우에만 크롬 프로세스 충돌 방지를 위해 차단
            if nb_profile and target_profile and (nb_profile.resolve() == target_profile.resolve()):
                logger.warning(f"계정 '{target_acc}'(으)로 이웃 순회가 실행 중입니다. 동일 계정 작업 전 이웃 순회를 먼저 정지해 주세요.")
                return False
            logger.info(f"이웃 순회(계정: {nb_acc})와 발행/로그인(계정: {target_acc})의 계정이 분리되어 있어 동시 실행을 허용합니다.")
    except Exception as e:
        logger.debug(f"start_exclusive_task 계정 검사 예외: {e}")
    if not TASK_LOCK.acquire(blocking=False):
        return False

    def _run():
        try:
            target(*args, **kwargs)
        finally:
            TASK_LOCK.release()

    threading.Thread(target=_run, daemon=True).start()
    return True


class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 1초마다 오는 /api/logs 폴링 접근 로그로 콘솔이 넘치지 않게 (콘솔 없는 EXE에선 stderr가 파일)
        if sys.stderr and "/api/logs" not in str(args[0] if args else ""):
            super().log_message(format, *args)

    def _common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def _send_json(self, data, code=200):
        if config_mod.NO_SEARCH_DATA:
            data = _scrub_texts(data)   # 크몽판: 안내·로그·오류 문구의 플랫폼 이름을 일반 표현으로
        elif isinstance(data, dict) and isinstance(data.get("message"), str):
            data = {**data, "message": _blog_text(data["message"])}
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)

    def _forbidden_reason(self, is_post: bool) -> str:
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            return "허용되지 않은 Host"
        origin = self.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return "허용되지 않은 Origin"
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            return "다른 사이트에서 온 요청"
        if is_post and self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return "JSON 요청만 허용"
        return ""

    def _token_ok(self, query_token: str = "") -> bool:
        cands = [self.headers.get("X-NBS-Token", ""), query_token]
        try:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            if TOKEN_COOKIE in c:
                cands.append(c[TOKEN_COOKIE].value)
        except Exception:
            pass
        return any(t and hmac.compare_digest(t, API_TOKEN) for t in cands)

    def _send_video_file(self, vid: str):
        """올린 영상·만든 클립 미리 보기 (영상 플레이어의 구간 요청 Range 지원)."""
        import user_media
        path = user_media.video_path(vid)
        if not path or not path.lower().endswith(".mp4"):
            self.send_response(404)
            self.end_headers()
            return
        size = os.path.getsize(path)
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(size - int(m.group(2)), 0)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self._common_headers()
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            pass

    def _reject(self, code: int, reason: str):
        logger.warning(f"⛔ 요청 차단 ({reason}): {self.command} {urlparse(self.path).path}")
        self._send_json({"ok": False, "message": reason}, code)

    def do_GET(self):
        url = urlparse(self.path)
        reason = self._forbidden_reason(is_post=False)
        if reason:
            return self._reject(403, reason)
        if url.path == "/" or url.path == "/index.html":
            if not self._token_ok(parse_qs(url.query).get("t", [""])[0]):
                body = "<meta charset='utf-8'><p style='font-family:sans-serif'>오토로그 프로그램 창에서 열어 주세요.</p>".encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._common_headers()
                self.end_headers()
                self.wfile.write(body)
                return
            body = _page_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", f"{TOKEN_COOKIE}={API_TOKEN}; Path=/; HttpOnly; SameSite=Strict")
            self._common_headers()
            self.end_headers()
            self.wfile.write(body)
        elif url.path != "/api/ping" and not self._token_ok():
            return self._reject(403, "접속 열쇠 없음")
        elif url.path.startswith("/api/") and url.path not in LICENSE_FREE and not licensing.is_usable():
            return self._send_json({"ok": False, "license_required": True, "message": "라이선스 키를 입력해 주세요."}, 402)
        elif url.path == "/api/license/status":
            self._send_json(licensing.refresh())
        elif url.path == "/api/schedules":
            import schedule_queue
            self._send_json({"items": schedule_queue.list_items(), "pending": schedule_queue.pending_count(),
                             "batch": schedule_queue.batch_status(), "paused": schedule_queue.paused()})
        elif url.path == "/api/status":
            cfg = load_config()
            self._send_json(status_payload(cfg))
        elif url.path == "/api/env_check":
            env_status = check_environment_status()
            self._send_json(env_status)
        elif url.path == "/api/history":
            items = load_history()
            self._send_json(items)
        elif url.path == "/api/categories":
            qs = parse_qs(url.query)
            cfg = load_config()
            target_blog = qs.get("blog_id", [None])[0] or cfg.get("blog_id") or cfg.get("naver_id")
            cats = fetch_naver_categories(target_blog) if target_blog else []
            self._send_json({"ok": True, "categories": cats})
        elif url.path == "/api/ping":
            # 중복 실행 확인용 서명 (다른 프로그램이 같은 포트를 써도 구분)
            self._send_json({"app": "NaverBlogStudio"})
        elif url.path == "/api/update/status":
            self._send_json(updater.status())
        elif url.path == "/api/logs":
            self._send_json({
                "logs": LOG_HISTORY[-80:],
                "task": {**CURRENT_TASK, "message": _blog_text(CURRENT_TASK.get("message"))}
            })
        elif url.path == "/api/neighbor/status":
            import neighbor_service
            self._send_json(neighbor_service.NeighborBot.get_instance().get_status())
        elif url.path == "/api/neighbor/config":
            import neighbor_service
            self._send_json(neighbor_service.load_neighbor_config())
        elif url.path == "/api/neighbor/history":
            import neighbor_service
            self._send_json(neighbor_service.load_neighbor_history()[-100:])
        elif url.path == "/api/neighbor/blacklist":
            import neighbor_service
            self._send_json(neighbor_service.load_neighbor_blacklist())
        elif url.path == "/api/review/clip_file":
            self._send_video_file(parse_qs(url.query).get("id", [""])[0])
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        url = urlparse(self.path)
        reason = self._forbidden_reason(is_post=True)
        if reason:
            return self._reject(403, reason)
        if not self._token_ok():
            return self._reject(403, "접속 열쇠 없음")
        if url.path not in LICENSE_FREE and not licensing.is_usable():
            return self._send_json({"ok": False, "license_required": True, "message": "라이선스 키를 입력해 주세요."}, 402)
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._reject(400, "잘못된 Content-Length")
        if url.path == "/api/upload_video":
            # 동영상은 크기가 커서 JSON이 아니라 파일 내용 그대로 받는다 (Content-Type 은 위조 요청 방지를 위해 JSON 으로 보냄)
            import user_media
            from urllib.parse import unquote
            name = unquote(self.headers.get("X-File-Name", ""))[:200]
            try:
                meta = user_media.save_video_stream(self.rfile, length, name)
            except ValueError as ve:
                self.close_connection = True
                return self._send_json({"ok": False, "message": str(ve)}, 400)
            logger.info(f"🎬 동영상 업로드: {meta['name']} ({meta['size'] / 1048576:.0f}MB)")
            return self._send_json({"ok": True, "id": meta["id"], "name": meta["name"], "size": meta["size"]})
        if length > (MAX_UPLOAD_BYTES if url.path == "/api/upload_photo" else MAX_BODY_BYTES):
            return self._reject(413, "요청 본문이 너무 큽니다")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
            if not isinstance(payload, dict):
                raise ValueError("object 아님")
        except ValueError:
            return self._reject(400, "잘못된 JSON")

        if url.path == "/api/license/activate":
            key = payload.get("key", "")
            if not isinstance(key, str) or len(key) > 64:
                return self._send_json({"ok": False, "message": "잘못된 입력 형식"}, 400)
            try:
                return self._send_json({"ok": True, **licensing.activate(key)})
            except licensing.LicenseError as e:
                return self._send_json({"ok": False, "message": str(e)}, 400)
            except ConnectionError:
                return self._send_json({"ok": False, "message": "라이선스 서버에 연결할 수 없습니다. 인터넷 연결을 확인해 주세요."}, 503)

        elif url.path == "/api/license/logout":
            if TASK_LOCK.locked():
                return self._send_json({"ok": False, "message": "발행·로그인 작업이 끝난 뒤 라이선스를 바꿔 주세요."}, 409)
            licensing.logout()
            logger.info("🔑 라이선스 로그아웃")
            self._send_json({"ok": True, **licensing.status()})

        elif url.path == "/api/save_config":
            cfg = load_config()
            cfg["naver_id"] = payload.get("naver_id", "").strip()
            # 비밀번호/키는 화면에 다시 내려보내지 않으므로, 빈 값이면 기존 저장값 유지
            cfg["naver_pw"] = payload.get("naver_pw", "").strip() or cfg.get("naver_pw", "")
            bid = str(payload.get("blog_id", "")).strip()
            if not config_mod.BLOG_ID_RE.match(bid):
                return self._send_json({"ok": False, "message": "블로그 ID 형식이 올바르지 않습니다. (영문·숫자·_- 만 사용)"}, 400)
            nid = str(payload.get("naver_id", "")).strip()
            if nid and not config_mod.ID_RE.match(nid):
                return self._send_json({"ok": False, "message": "아이디 형식이 올바르지 않습니다."}, 400)
            cfg["blog_id"] = bid
            cfg["post_category"] = payload.get("post_category", "").strip()
            cfg["gemini_api_key"] = payload.get("gemini_api_key", "").strip() or cfg.get("gemini_api_key", "")
            save_config(cfg)
            self._send_json({"ok": True})

        elif url.path in ("/api/accounts/add", "/api/accounts/select", "/api/accounts/delete"):
            if TASK_LOCK.locked():
                return self._send_json({"ok": False, "message": "발행·로그인 작업이 끝난 뒤 계정을 바꿔 주세요."}, 409)
            nid = payload.get("naver_id", "")
            if not isinstance(nid, str) or not nid.strip():
                return self._send_json({"ok": False, "message": "아이디가 없습니다."}, 400)
            nid = nid.strip()[:64]
            cfg = load_config()
            if url.path == "/api/accounts/add":
                pw, bid = payload.get("naver_pw", ""), payload.get("blog_id", "")
                if not isinstance(pw, str) or not isinstance(bid, str):
                    return self._send_json({"ok": False, "message": "잘못된 입력 형식"}, 400)
                err = config_mod.add_account(cfg, nid, pw.strip(), bid[:64])
                if err:
                    return self._send_json({"ok": False, "message": err}, 400)
                try:
                    licensing.account_add(nid)   # 라이선스당 계정 5개 (다른 PC 포함)
                except licensing.LicenseError as e:
                    return self._send_json({"ok": False, "message": str(e)}, 400)
                except ConnectionError:
                    return self._send_json({"ok": False, "message": "라이선스 서버에 연결할 수 없어 계정을 추가할 수 없습니다."}, 503)
                save_config(cfg)
                logger.info(f"➕ 계정 추가: {nid} (로그인을 진행합니다)")
                # 등록 후 바로 로그인 (보안 인증이 나오면 열린 창에서 직접 완료)
                start_exclusive_task(run_login_task)
            elif url.path == "/api/accounts/select":
                if not config_mod.select_account(cfg, nid):
                    return self._send_json({"ok": False, "message": "등록되지 않은 계정입니다."}, 404)
                save_config(cfg)
                logger.info(f"👤 계정 선택: {nid}")
            else:
                if not config_mod.remove_account(cfg, nid):
                    return self._send_json({"ok": False, "message": "등록되지 않은 계정입니다."}, 404)
                save_config(cfg)
                licensing.account_remove(nid)
                logger.info(f"🗑 계정 삭제: {nid} (저장된 비밀번호·로그인 정보 삭제)")
            self._send_json({"ok": True, **status_payload(load_config())})

        elif url.path == "/api/save_fixed":
            # 상단/하단 고정 문구·지도 (사용자가 입력한 그대로 저장, 길이만 제한)
            cfg = load_config()
            for k, limit in (("header_text", 2000), ("footer_text", 2000), ("map_query", 100)):
                v = payload.get(k, "")
                if not isinstance(v, str):
                    return self._send_json({"ok": False, "message": "잘못된 입력 형식"}, 400)
                cfg[k] = v[:limit]
            cfg["map_position"] = "top" if payload.get("map_position") == "top" else "bottom"
            save_config(cfg)
            self._send_json({"ok": True})

        elif url.path == "/api/save_banned":
            # 금지 키워드 (줄바꿈·쉼표 구분, 모든 계정 공통)
            v = payload.get("banned_keywords", "")
            if not isinstance(v, (str, list)):
                return self._send_json({"ok": False, "message": "잘못된 입력 형식"}, 400)
            cfg = load_config()
            cfg["banned_keywords"] = config_mod.parse_banned(v)
            save_config(cfg)
            logger.info(f"🚫 금지 키워드 {len(cfg['banned_keywords'])}개 저장")
            self._send_json({"ok": True, "banned_keywords": cfg["banned_keywords"]})

        elif url.path == "/api/save_gemini_key":
            cfg = load_config()
            key_val = str(payload.get("gemini_api_key", "")).strip()
            if not key_val:
                # 빈 값으로 기존 키가 지워지지 않게
                return self._send_json({"ok": False, "message": "API 키가 비어 있습니다."}, 400)
            cfg["gemini_api_key"] = key_val
            save_config(cfg)
            logger.info("✔ Google Gemini API 키가 안전하게 저장되었습니다.")
            self._send_json({"ok": True, "message": "Gemini API 키 저장 완료"})

        elif url.path == "/api/test_gemini_key":
            key_val = payload.get("gemini_api_key", "").strip()
            if not key_val:
                cfg = load_config()
                key_val = cfg.get("gemini_api_key", "").strip()
            result = test_gemini_api_key(key_val)
            self._send_json(result)

        elif url.path == "/api/focus":
            # 두 번째 실행 시 기존 프로그램 창을 앞으로
            # soft: 트레이로 숨기고 '끝나면 종료'를 고른 경우엔 그 선택을 취소하지 않는다
            if APP_WINDOW and not (payload.get("soft") and getattr(APP_WINDOW, "auto_quit", False)):
                APP_WINDOW.focus()
            self._send_json({"ok": bool(APP_WINDOW)})

        elif url.path == "/api/schedules/add":
            import schedule_queue
            title, content, tags, category = (payload.get(k, "") for k in ("title", "content", "tags", "category"))
            schedule_at = str(payload.get("schedule_at") or "").strip()
            if not all(isinstance(v, str) for v in (title, content, tags, category)):
                return self._send_json({"ok": False, "message": "잘못된 입력 형식"}, 400)
            if len(title) > 300 or len(content) > 60000 or len(tags) > 2000:
                return self._send_json({"ok": False, "message": "제목/본문/태그 길이가 너무 깁니다."}, 400)
            cfg = load_config()
            if not cfg.get("naver_id"):
                return self._send_json({"ok": False, "message": "N사 계정을 먼저 추가해 주세요."}, 400)
            # 토큰: 대기 중인 예약까지 모두 등록할 만큼 남아 있어야 추가
            lic = (licensing.status().get("license") or {})
            if not lic.get("unlimited"):
                waiting = sum(1 for it in schedule_queue.list_items() if it["status"] in ("queued", "registering"))
                need = (waiting + 1) * licensing.COST_PER_POST
                if lic.get("tokens_left", 0) < need:
                    return self._send_json({"ok": False, "message": f"토큰이 부족합니다. 남은 토큰 {lic.get('tokens_left', 0):,} / "
                                            f"필요 {need:,} (등록 대기 {waiting}건 포함, 1건당 {licensing.COST_PER_POST}토큰)"}, 400)
            ids = payload.get("photo_ids") if isinstance(payload.get("photo_ids"), list) else []
            try:
                item = schedule_queue.add({"title": title, "content": content, "tags": tags, "category": category,
                                           "auto_thumb": bool(payload.get("auto_thumb", True)),
                                           "public_open": bool(payload.get("public_open", True)), "photo_ids": ids},
                                          schedule_at, cfg)
            except ValueError as ve:
                return self._send_json({"ok": False, "message": str(ve)}, 400)
            logger.info(f"📋 예약 목록에 추가: '{item['title']}' → {item['schedule_at'].replace('T', ' ')} (계정 {item['account']})")
            self._send_json({"ok": True, "item": {k: v for k, v in item.items() if k not in ("content", "tags")}})

        elif url.path == "/api/batch":
            # start 없음: 시각 미리보기 / start: 미리 본 시각 그대로 자동 예약 시작
            import schedule_queue
            cfg = load_config()
            raw = payload.get("keywords", "")
            if not isinstance(raw, str):
                return self._send_json({"ok": False, "message": "잘못된 입력 형식"}, 400)
            keywords = list(dict.fromkeys(k.strip()[:50] for k in re.split(r"[,\n]", raw) if k.strip()))[:20]
            # 추천받은 주제를 골라 바로 예약: 키워드 대신 주제 목록을 그대로 쓴다 (글 수 = 고른 주제 수)
            chosen = []
            for t in (payload.get("topics") if isinstance(payload.get("topics"), list) else [])[:24]:
                if not isinstance(t, dict):
                    continue
                title = re.sub(r"\s+", " ", str(t.get("title", ""))).strip()[:100]
                if not title or any(c["title"] == title for c in chosen):
                    continue
                c = {k: re.sub(r"\s+", " ", str(t.get(k, "") or "")).strip()[:300]
                     for k in ("main_keyword", "style", "style_name", "angle", "intent", "reason")}
                c["title"] = title
                c["main_keyword"] = c["main_keyword"][:50] or title[:50]
                c["sub_keywords"] = [str(x).strip()[:40] for x in (t.get("sub_keywords") or []) if str(x).strip()][:6] \
                    if isinstance(t.get("sub_keywords"), list) else []
                try:
                    c["score"] = max(0, min(100, int(t.get("score", 70))))
                except (TypeError, ValueError):
                    c["score"] = 70
                chosen.append(c)
            # 쇼핑커넥트 상품 목록에서 골라 담은 상품: 상품 하나 = 글 하나 (글 쓸 때 링크 발급·캡처)
            shop_items = payload.get("shop_items") if isinstance(payload.get("shop_items"), list) else []
            if shop_items and not chosen:
                import shopping_connect
                from config import banned_keywords, find_banned
                words = banned_keywords()
                seen = set()
                for si in shop_items[:24]:
                    raw = shopping_connect.clean_raw_item((si or {}).get("raw")) if isinstance(si, dict) else None
                    if not raw or raw["affiliateProductId"] in seen:
                        continue
                    seen.add(raw["affiliateProductId"])
                    raw["affiliateProductId"] = str(si.get("affiliate_product_id") or raw["affiliateProductId"])
                    name = re.sub(r"\s+", " ", raw.get("productName") or "").strip()
                    if find_banned(name, words):
                        return self._send_json({"ok": False, "message": f"금지 키워드가 든 상품이 있습니다: {name[:40]}"}, 400)
                    skw = re.sub(r"\s+", " ", str(si.get("search_keyword") or "")).strip()[:30]
                    chosen.append({"title": f"[쇼핑커넥트] {name[:80]} (작성 전)",
                                   "main_keyword": shopping_connect.short_keyword(name, skw)[:50] or name[:50],
                                   "connect": True, "connect_item": raw, "search_keyword": skw})
                if not chosen:
                    return self._send_json({"ok": False, "message": "담은 상품 정보를 읽지 못했습니다. 상품을 다시 찾아 담아 주세요."}, 400)
            connect_auto = bool(payload.get("connect")) and not chosen
            # 쇼핑커넥트 상품 자동 예약: 상품 한 줄 = 글 하나 (상품명은 비어 있으면 링크에서 읽는다)
            products_raw = payload.get("products", "")
            if isinstance(products_raw, str) and products_raw.strip():
                import shopping_service
                from concurrent.futures import ThreadPoolExecutor
                try:
                    rows = shopping_service.parse_product_lines(products_raw)
                    import shopping_connect
                    conn_rows = [r for r in rows if shopping_connect.is_connect_link(r["url"])]
                    if payload.get("start") and conn_rows:
                        # 쇼핑커넥트 발급 링크: 발급 목록에서 상품을 찾아 상품 사진 캡처까지 하는 자동 글로 예약
                        why = _lock_account_browser()
                        if why:
                            return self._send_json({"ok": False, "message": why}, 409)
                        try:
                            hits = shopping_connect.lookup_links(config_mod.account_profile_dir(cfg), cfg.get("blog_id") or cfg.get("naver_id"),
                                                                 [r["url"] for r in conn_rows])
                        except Exception as e:
                            return self._send_json({"ok": False, "message": str(e)}, 400)
                        finally:
                            TASK_LOCK.release()
                        missing = [r["url"] for r in conn_rows if r["url"] not in hits or not hits[r["url"]].get("raw")]
                        if missing:
                            return self._send_json({"ok": False, "message": f"'{cfg.get('naver_id')}' 계정의 발급 목록에 없는 링크가 있습니다: "
                                                    + ", ".join(missing[:3]) + " — 링크를 발급한 계정을 골라 주세요."}, 400)
                        for r in conn_rows:
                            h = hits[r["url"]]
                            chosen.append({"title": f"[쇼핑커넥트] {h['name'][:80]} (작성 전)",
                                           "main_keyword": shopping_connect.short_keyword(h["name"])[:50] or h["name"][:50],
                                           "connect": True, "connect_item": h["raw"], "search_keyword": "", "memo": r.get("memo", "")[:3000]})
                    rows = [r for r in rows if not shopping_connect.is_connect_link(r["url"])] if payload.get("start") else rows
                    if payload.get("start"):
                        with ThreadPoolExecutor(max_workers=6) as ex:
                            futs = [ex.submit(shopping_service.normalize_product, r, not r["name"]) for r in rows]
                            prods = [f.result() for f in futs]
                    else:
                        prods = [{**r, "name": r["name"] or "(링크에서 읽음)"} for r in rows]
                except ValueError as ve:
                    return self._send_json({"ok": False, "message": str(ve)}, 400)
                chosen += [{"title": p["name"][:100], "main_keyword": p["name"][:50], "style": "shopping", "product": p}
                           for p in prods]
            if chosen:
                keywords = list(dict.fromkeys(c["main_keyword"] for c in chosen))
                payload["count"] = len(chosen)
            try:
                count, start_h, end_h, gap = (int(payload.get(k)) for k in ("count", "start_hour", "end_hour", "gap_min"))
                day = datetime.fromisoformat(str(payload.get("date"))).date()
            except (TypeError, ValueError):
                return self._send_json({"ok": False, "message": "입력값을 확인해 주세요."}, 400)
            today = datetime.now().date()
            problem = ("키워드를 1개 이상 입력해 주세요. (쇼핑커넥트 자동은 비워 두면 상품을 무작위로 고릅니다)" if not keywords and not connect_auto
                       else "글 수는 1~24개로 입력해 주세요." if not 1 <= count <= 24
                       else "시작 시간이 끝 시간보다 빨라야 합니다." if not 0 <= start_h < end_h <= 24
                       else "최소 간격은 60분 이상이어야 합니다." if not 60 <= gap <= 720
                       else "날짜는 오늘부터 30일 안으로 선택해 주세요." if not 0 <= (day - today).days <= 30
                       else "블로그 계정을 먼저 추가해 주세요." if not cfg.get("naver_id")
                       else "자동 예약은 AI가 글을 써야 합니다. Gemini API 키를 먼저 저장해 주세요."
                       if len(cfg.get("gemini_api_key", "").strip()) <= 10 else "")
            if problem:
                return self._send_json({"ok": False, "message": problem}, 400)
            lic = (licensing.status().get("license") or {})
            if not lic.get("unlimited"):
                waiting = sum(1 for it in schedule_queue.list_items() if it["status"] in schedule_queue.WAITING)
                need = (waiting + count) * licensing.COST_PER_POST
                if lic.get("tokens_left", 0) < need:
                    return self._send_json({"ok": False, "message": f"토큰이 부족합니다. 남은 토큰 {lic.get('tokens_left', 0):,} / "
                                            f"필요 {need:,} (등록 대기 {waiting}건 포함, 1건당 {licensing.COST_PER_POST}토큰)"}, 400)
            if not payload.get("start"):
                try:
                    slots = schedule_queue.preview_slots(day, start_h, end_h, count, gap, cfg)
                except ValueError as ve:
                    return self._send_json({"ok": False, "message": str(ve)}, 400)
                return self._send_json({"ok": True, "slots": [t.isoformat(timespec="minutes") for t in slots],
                                        "keywords": keywords, "cost": count * licensing.COST_PER_POST})
            try:
                slots = sorted(datetime.fromisoformat(str(t)) for t in payload.get("slots") or [])
            except ValueError:
                slots = []
            if len(slots) != count:
                return self._send_json({"ok": False, "message": "예약 시각을 다시 뽑아 주세요."}, 400)
            options = {"category": str(payload.get("category") or cfg.get("post_category", ""))[:200],
                       "public_open": bool(payload.get("public_open", True)),
                       "auto_thumb": bool(payload.get("auto_thumb", True))}
            if chosen:
                random.shuffle(chosen)       # 올라가는 순서만 섞는다 (같은 주제 몰림 방지)
                picks, topics = [c["main_keyword"] for c in chosen], chosen
            elif connect_auto:
                # 쇼핑커넥트 자동: 글 쓸 때 키워드로 계정 상품을 골라 링크 발급·캡처 (제목은 그때 정함)
                # 키워드가 없으면 글 쓸 때마다 아직 쓰지 않은 상품을 무작위로 고른다
                picks = schedule_queue.pick_keywords(keywords, count) if keywords else [""] * count
                topics = [{"title": f"[쇼핑커넥트] {kw + ' ' if kw else '무작위 '}상품 글 (작성 전)", "main_keyword": kw, "connect": True}
                          for kw in picks]
            else:
                picks = schedule_queue.pick_keywords(keywords, count)
                try:
                    topics = batch_topics(picks, cfg.get("gemini_api_key", "").strip())
                except ValueError as ve:
                    return self._send_json({"ok": False, "message": str(ve)}, 400)
            schedule_queue.resume()
            new = schedule_queue.add_batch(picks, topics, slots, gap, (start_h, end_h), options, cfg)
            logger.info(f"🎲 키워드 자동 예약 시작: {len(new)}건 ({', '.join(keywords) or '쇼핑커넥트 무작위 상품'})")
            self._send_json({"ok": True, "count": len(new)})

        elif url.path == "/api/schedules/resume":
            resume_queue()
            self._send_json({"ok": True})

        elif url.path == "/api/schedules/retry":
            import schedule_queue
            why = schedule_queue.retry(str(payload.get("id", "")))
            if why:
                return self._send_json({"ok": False, "message": why}, 400)
            self._send_json({"ok": True})

        elif url.path == "/api/schedules/remove_published":
            import schedule_queue
            self._send_json({"ok": True, "removed": schedule_queue.remove_published()})

        elif url.path in ("/api/schedules/cancel", "/api/schedules/remove"):
            import schedule_queue
            sid = payload.get("id")
            if not isinstance(sid, str) or not sid:
                return self._send_json({"ok": False, "message": "잘못된 입력 형식"}, 400)
            if url.path == "/api/schedules/remove":
                if not schedule_queue.remove(sid):
                    return self._send_json({"ok": False, "message": "끝난 항목(발행됨·취소됨·실패)만 지울 수 있습니다."}, 400)
                return self._send_json({"ok": True})
            r = schedule_queue.request_cancel(sid)
            if r == "removed":
                logger.info("📋 등록 전 예약을 목록에서 뺐습니다 (토큰 차감 없음)")
                return self._send_json({"ok": True, "remote": False})
            if r == "remote":
                logger.info("📋 N사 예약 취소를 요청했습니다 (곧 처리, 사용한 토큰은 돌려받지 않음)")
                return self._send_json({"ok": True, "remote": True})
            return self._send_json({"ok": False, "message": r}, 400)

        elif url.path == "/api/app/close":
            # 종료 창 선택: quit = 바로 종료, tray = 트레이로 숨기고 작업이 끝나면 종료
            mode = payload.get("mode")
            if not APP_WINDOW:
                return self._send_json({"ok": False, "message": "프로그램 창 모드에서만 사용할 수 있습니다."}, 400)
            if mode == "quit":
                self._send_json({"ok": True})
                threading.Thread(target=APP_WINDOW.quit, daemon=True).start()
            elif mode == "tray":
                if not is_busy():
                    return self._send_json({"ok": False, "message": "진행 중인 작업이 없습니다."}, 400)
                ok = APP_WINDOW.hide_to_tray()
                self._send_json({"ok": ok, "message": "" if ok else "트레이 아이콘을 만들 수 없습니다."}, 200 if ok else 500)
            else:
                return self._send_json({"ok": False, "message": "잘못된 선택"}, 400)

        elif url.path == "/api/update/ack":
            updater.ack_just_updated()
            self._send_json(updater.status())

        elif url.path == "/api/update/check":
            self._send_json(updater.check(force=True))

        elif url.path == "/api/update/install":
            if TASK_LOCK.locked():
                return self._send_json({"ok": False, "message": "발행·로그인 작업이 끝난 뒤 업데이트해 주세요."}, 409)
            err = updater.install(shutdown_app)
            if err:
                return self._send_json({"ok": False, "message": err}, 400)
            self._send_json(updater.status())

        elif url.path == "/api/install_dependencies":
            if not start_exclusive_task(run_install_task):
                return self._send_json({"ok": False, "message": "다른 작업이 진행 중입니다."}, 409)
            self._send_json({"ok": True, "message": "설치 작업 시작"})

        elif url.path == "/api/login":
            if not start_exclusive_task(run_login_task):
                return self._send_json({"ok": False, "message": "다른 작업이 진행 중입니다."}, 409)
            self._send_json({"ok": True, "message": "로그인 작업 시작"})

        elif url.path == "/api/ai_draft":
            keyword = str(payload.get("keyword", "")).strip()[:100]
            style = payload.get("style", "info")
            product = None
            rnd = isinstance(payload.get("product"), dict) and payload["product"].get("random") is True
            if rnd or isinstance(payload.get("product"), dict) and str(payload["product"].get("affiliate_product_id") or "").isdigit():
                # 내 계정 쇼핑커넥트에서 고른 상품: 링크 발급·캡처까지 해서 작성
                cfg = load_config()
                gemini_key = payload.get("gemini_api_key", "").strip() or cfg.get("gemini_api_key", "").strip()
                why = _lock_account_browser()
                if why:
                    return self._send_json({"ok": False, "message": why}, 409)
                try:
                    p = payload["product"]
                    extra = user_photo_list(payload.get("photo_ids") if isinstance(payload.get("photo_ids"), list) else [])
                    notes = payload.get("user_notes", "")
                    title, contents, tags, shots, prod = connect_write(
                        cfg, gemini_key, keyword=str(p.get("search_keyword", "") or "")[:60],
                        product_id=None if rnd else str(p["affiliate_product_id"]),
                        item=p.get("raw") if isinstance(p.get("raw"), dict) else None,
                        main_keyword=str(p.get("search_keyword", "") or payload.get("keyword", ""))[:30],
                        memo=str(p.get("memo", ""))[:3000], user_notes=notes[:20000] if isinstance(notes, str) else "",
                        extra_photos=extra)
                except Exception as e:
                    logger.warning(f"쇼핑커넥트 원고 준비 실패: {e}")
                    return self._send_json({"ok": False, "message": str(e)}, 400)
                finally:
                    TASK_LOCK.release()
                return self._send_json({"title": title, "contents": contents, "tags": tags, "photos": shots,
                                        "product": {k: prod.get(k) for k in ("name", "url", "price", "store")}})
            if isinstance(payload.get("product"), dict):
                import shopping_service
                try:
                    product = shopping_service.normalize_product(payload["product"])
                except ValueError as ve:
                    return self._send_json({"ok": False, "message": str(ve)}, 400)
                keyword = keyword or product["name"][:40]
                style = "shopping"
            if not keyword:
                return self._send_json({"ok": False, "message": "키워드를 입력해주세요."}, 400)
            if style not in AI_STYLES:
                style = "info"
            cfg = load_config()
            gemini_key = payload.get("gemini_api_key", "").strip() or cfg.get("gemini_api_key", "").strip()
            user_notes = payload.get("user_notes", "")
            user_notes = user_notes[:20000] if isinstance(user_notes, str) else ""
            photos = user_photo_list(payload.get("photo_ids") if isinstance(payload.get("photo_ids"), list) else [])
            import user_media
            descs = [user_media.describe_photo(pid, gemini_key) for pid, _ in photos]
            topic = payload.get("topic") if isinstance(payload.get("topic"), dict) else None
            title, contents, tags = generate_blog_post(keyword, style=style, api_key=gemini_key,
                                                       user_notes=user_notes, photo_descs=descs,
                                                       topic=None if product else topic, product=product)
            clean_title, clean_contents, norm_tags = clean_draft(title, contents, tags)
            if photos:
                clean_contents = place_user_photos(clean_contents, photos, gemini_key)

            # 원고를 검토하는 동안 사진을 미리 만들기 시작 (발행 때 제목이 같으면 그대로 이어서 사용)
            try:
                import image_service
                first_tag = norm_tags[0] if norm_tags else (clean_title.split() or [keyword])[0]
                image_service.start_prefetch(first_tag, clean_title, clean_contents, gemini_key,
                                             6 - len(photos))
            except Exception as e:
                logger.warning(f"사진 미리 만들기 시작 실패: {e}")

            self._send_json({"title": clean_title, "contents": clean_contents, "tags": norm_tags})

        elif url.path == "/api/shopping/search":
            import shopping_connect
            keyword = str(payload.get("keyword", "")).strip()[:60]
            try:
                page_no = max(1, min(500, int(payload.get("page") or 1)))
            except (TypeError, ValueError):
                page_no = 1
            cfg = load_config()
            if not cfg.get("naver_id"):
                return self._send_json({"ok": False, "message": "블로그 계정을 먼저 추가해 주세요."}, 400)
            why = _lock_account_browser()
            if why:
                logger.warning(f"쇼핑커넥트 상품 찾기 보류: {why}")
                return self._send_json({"ok": False, "message": why}, 409)
            try:
                res = shopping_connect.search_products(config_mod.account_profile_dir(cfg),
                                                       cfg.get("blog_id") or cfg.get("naver_id"), keyword, page_no)
            except Exception as e:
                logger.warning(f"쇼핑커넥트 상품 찾기 실패 (계정 {cfg.get('naver_id')}, '{keyword}' {page_no}쪽): {e}")
                return self._send_json({"ok": False, "message": str(e)}, 400)
            finally:
                TASK_LOCK.release()
            self._send_json({"ok": True, **res, "keyword": keyword, "account": cfg.get("naver_id")})

        elif url.path == "/api/shopping/issue":
            import shopping_connect
            ids = [str(x) for x in (payload.get("ids") or []) if str(x).isdigit()][:24]
            if not ids:
                return self._send_json({"ok": False, "message": "링크를 발급할 상품을 골라 주세요."}, 400)
            cfg = load_config()
            if not cfg.get("naver_id"):
                return self._send_json({"ok": False, "message": "블로그 계정을 먼저 추가해 주세요."}, 400)
            why = _lock_account_browser()
            if why:
                return self._send_json({"ok": False, "message": why}, 409)
            try:
                links = shopping_connect.issue_links(config_mod.account_profile_dir(cfg),
                                                     cfg.get("blog_id") or cfg.get("naver_id"), ids)
            except Exception as e:
                logger.warning(f"쇼핑커넥트 링크 발급 실패 (계정 {cfg.get('naver_id')}): {e}")
                return self._send_json({"ok": False, "message": str(e)}, 400)
            finally:
                TASK_LOCK.release()
            self._send_json({"ok": True, "links": links})

        elif url.path == "/api/shopping/fetch":
            import shopping_service
            link = shopping_service.clean_url(payload.get("url", ""))
            if not link:
                return self._send_json({"ok": False, "message": "링크(https://...)를 입력해 주세요."}, 400)
            import shopping_connect
            if shopping_connect.is_connect_link(link):
                # 쇼핑커넥트 발급 링크: 열지 않고 이 계정의 발급 목록에서 같은 링크를 찾아 상품 정보를 쓴다
                cfg = load_config()
                why = _lock_account_browser()
                if why:
                    return self._send_json({"ok": False, "message": why})
                try:
                    hit = shopping_connect.lookup_links(config_mod.account_profile_dir(cfg),
                                                        cfg.get("blog_id") or cfg.get("naver_id"), [link]).get(link)
                except Exception as e:
                    logger.warning(f"쇼핑커넥트 링크 찾기 실패: {e}")
                    return self._send_json({"ok": False, "message": str(e)})
                finally:
                    TASK_LOCK.release()
                if not hit:
                    return self._send_json({"ok": False, "message": f"이 링크를 '{cfg.get('naver_id')}' 계정의 발급 목록에서 찾지 못했습니다. "
                                            "링크를 발급한 계정을 고른 뒤 다시 눌러 주세요. (또는 상품명을 직접 입력)"})
                return self._send_json({"ok": True, "name": hit["name"], "price": hit["price"], "product": hit})
            info = shopping_service.fetch_product(link)
            if not info["name"]:
                return self._send_json({"ok": False, "message": "상품 정보를 자동으로 읽지 못했습니다. 상품명을 직접 입력해 주세요."})
            self._send_json({"ok": True, **info})

        elif url.path == "/api/topic_recommend":
            keyword = str(payload.get("keyword", "")).strip()[:50]
            if not keyword:
                return self._send_json({"ok": False, "message": "키워드를 입력해주세요."}, 400)
            cfg = load_config()
            gemini_key = str(payload.get("gemini_api_key", "")).strip() or cfg.get("gemini_api_key", "").strip()
            import topic_service
            try:
                self._send_json(topic_service.recommend_topics(keyword, gemini_key, used_titles=all_used_titles()))
            except Exception as e:
                logger.warning(f"주제 추천 실패: {e}")
                self._send_json({"ok": False, "message": f"주제 추천 중 오류: {e}"}, 500)

        elif url.path == "/api/upload_photo":
            import user_media
            data, name = payload.get("data", ""), payload.get("name", "")
            if not isinstance(data, str) or not data or not isinstance(name, str):
                return self._send_json({"ok": False, "message": "사진 데이터가 없습니다."}, 400)
            try:
                meta = user_media.save_photo(data, name)
            except ValueError as ve:
                return self._send_json({"ok": False, "message": str(ve)}, 400)
            desc = user_media.describe_photo(meta["id"], load_config().get("gemini_api_key", "")) \
                if payload.get("describe", True) is not False else ""
            logger.info(f"📷 사진 업로드: {name or meta['id'][:8]}" + (f" — {desc}" if desc else ""))
            self._send_json({"ok": True, "id": meta["id"], "desc": desc})

        elif url.path == "/api/delete_video":
            import user_media
            user_media.delete_video(str(payload.get("id", "")))
            self._send_json({"ok": True})

        elif url.path == "/api/review/clips":
            import review_post
            cfg = load_config()
            if not cfg.get("naver_id"):
                return self._send_json({"ok": False, "message": "블로그 계정을 먼저 추가해 주세요."}, 400)
            why = _lock_account_browser()
            if why:
                return self._send_json({"ok": False, "message": why}, 409)
            try:
                clips = review_post.list_my_clips(config_mod.account_profile_dir(cfg), cfg.get("blog_id") or cfg.get("naver_id"))
            except Exception as e:
                logger.warning(f"내 클립 불러오기 실패: {e}")
                return self._send_json({"ok": False, "message": f"내 클립을 불러오지 못했습니다: {e}"}, 400)
            finally:
                TASK_LOCK.release()
            self._send_json({"ok": True, "clips": clips, "account": cfg.get("naver_id")})

        elif url.path == "/api/review/draft":
            # 가이드 원고 만들기 (가이드 해석 → 1,500자 이상, 사진·영상·클립은 올린 순서대로 자리 표시)
            import review_post
            import user_media
            guide = payload.get("guide", "")
            notes = payload.get("notes", "")
            brand = payload.get("brand", "")
            if not all(isinstance(v, str) for v in (guide, notes, brand)):
                return self._send_json({"ok": False, "message": "잘못된 입력 형식"}, 400)
            why = review_post.check_guide_text(guide)
            if why:
                return self._send_json({"ok": False, "message": why}, 400)
            cfg = load_config()
            key = cfg.get("gemini_api_key", "").strip()
            photos = review_photo_list(payload.get("photo_ids"))
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as ex:      # 사진 설명 (한 번 만든 설명은 저장돼 다시 쓰임)
                descs = list(ex.map(lambda ph: user_media.describe_photo(ph[0], key), photos))
            n_videos = len(review_video_map(payload.get("videos")))
            clips = review_clip_map(payload.get("clips"))
            try:
                title, lines, tags, checklist = review_post.generate_from_guide(
                    guide, key, notes=notes, brand=brand, photo_descs=descs, n_videos=n_videos,
                    clip_titles=[c["title"] for _, c in sorted(clips.items())])
            except Exception as e:
                logger.warning(f"체험단 가이드 원고 생성 실패: {e}")
                return self._send_json({"ok": False, "message": str(e)}, 400)
            chars = review_post.body_chars(lines)
            logger.info(f"📝 체험단 가이드 원고: '{title}' 본문 {chars:,}자, 해시태그 {len(tags)}개")
            self._send_json({"ok": True, "title": title, "contents": lines, "tags": tags, "checklist": checklist,
                             "chars": chars, "min_chars": review_post.MIN_GUIDE_CHARS})

        elif url.path == "/api/review/publish":
            import review_post
            title = payload.get("title", "")
            content = payload.get("content", "")
            tags = payload.get("tags", "")
            category = payload.get("category", "")
            schedule_at = str(payload.get("schedule_at") or "").strip()
            mode = payload.get("mode", "direct")
            if not all(isinstance(v, str) for v in (title, content, tags, category)):
                return self._send_json({"ok": False, "message": "잘못된 입력 형식"}, 400)
            if not title.strip() or not content.strip():
                return self._send_json({"ok": False, "message": "제목과 본문을 넣어 주세요."}, 400)
            if len(title) > 300 or len(content) > 60000 or len(tags) > 3000:
                return self._send_json({"ok": False, "message": "제목/본문/태그 길이가 너무 깁니다."}, 400)
            photos = review_photo_list(payload.get("photo_ids"))
            videos = review_video_map(payload.get("videos"))
            clips = review_clip_map(payload.get("clips"))
            counts = {"사진": len(photos), "영상": len(videos), "클립": len(clips)}
            lines, body_tags = review_post.prepare_direct(content, counts)
            if mode == "guide" and review_post.body_chars(lines) < review_post.MIN_GUIDE_CHARS:
                return self._send_json({"ok": False, "message": f"가이드 원고는 본문 {review_post.MIN_GUIDE_CHARS:,}자 이상이어야 합니다 "
                                                              f"(지금 {review_post.body_chars(lines):,}자)."}, 400)
            all_tags = review_post.merge_tags(review_post.parse_tags(tags), body_tags)
            review = {"photos": {i: path for i, (_, path) in enumerate(photos, 1)}, "videos": videos, "clips": clips,
                      "center": bool(payload.get("center"))}
            if not start_exclusive_task(run_publish_task, title.strip(), "\n".join(lines), ", ".join(all_tags), category,
                                        False, None, bool(payload.get("public_open", True)), schedule_at, [],
                                        review=review):
                return self._send_json({"ok": False, "message": "다른 작업(발행/로그인/설치)이 진행 중입니다."}, 409)
            self._send_json({"ok": True, "message": "체험단·기자단 글 발행 시작"})

        elif url.path == "/api/review/clip_script":
            # 숏폼 클립 대본: 사진·영상 장면을 Gemini가 보고 자막·나레이션을 쓴다
            import clip_maker
            items = clip_maker.media_items(review_photo_list(payload.get("photo_ids")), review_video_map(payload.get("videos")))
            texts = {k: payload.get(k, "") for k in ("guide", "notes", "brand", "title", "content")}
            if not all(isinstance(v, str) for v in texts.values()):
                return self._send_json({"ok": False, "message": "잘못된 입력 형식"}, 400)
            try:
                seconds = int(payload.get("seconds") or 30)
            except (TypeError, ValueError):
                seconds = 30
            try:
                script = clip_maker.make_script(load_config().get("gemini_api_key", ""), items, seconds,
                                                str(payload.get("tone") or "review"), texts["guide"], texts["notes"],
                                                texts["brand"], texts["title"], texts["content"])
            except Exception as e:
                logger.warning(f"클립 대본 만들기 실패: {e}")
                return self._send_json({"ok": False, "message": str(e)}, 400)
            self._send_json({"ok": True, "script": script, "media": [it["key"] for it in items]})

        elif url.path == "/api/review/clip_make":
            # 숏폼 클립 만들기 (백그라운드): 대본 → 나레이션 → 장면 합성 → 올린 영상 목록에 추가할 수 있게 저장
            import clip_maker
            import user_media
            items = clip_maker.media_items(review_photo_list(payload.get("photo_ids")), review_video_map(payload.get("videos")))
            script = payload.get("script")
            if not isinstance(script, dict):
                return self._send_json({"ok": False, "message": "대본이 없습니다."}, 400)
            try:
                script = clip_maker.clean_script(script, items)
            except clip_maker.ClipError as e:
                return self._send_json({"ok": False, "message": str(e)}, 400)
            voice = str(payload.get("voice") or "female_bright")
            tone = str(payload.get("tone") or "review")
            sponsor = str(payload.get("sponsor") or "")[:20]
            key = load_config().get("gemini_api_key", "")
            if voice != "none" and not key.strip():
                return self._send_json({"ok": False, "message": "나레이션에는 Gemini API 키가 필요합니다. '나레이션 없음'을 고르거나 키를 넣어 주세요."}, 400)
            name = re.sub(r'[\\/:*?"<>|=]+', "", " ".join(script.get("title_lines") or []))[:40].strip() or "클립"

            def job(progress):
                res = clip_maker.build_clip(key, script, items, voice=voice, tone=tone, sponsor=sponsor, progress=progress)
                saved = ""
                try:     # 클립 업로드용으로 '동영상\오토로그 클립' 폴더에도 복사
                    folder = Path.home() / "Videos" / "오토로그 클립"
                    folder.mkdir(parents=True, exist_ok=True)
                    dst = folder / f"{name}_{time.strftime('%m%d_%H%M')}.mp4"
                    shutil.copyfile(res["path"], dst)
                    saved = str(dst)
                except Exception as e:
                    logger.warning(f"클립 사본 저장 실패: {e}")
                meta = user_media.save_video_file(res["path"], f"{name}.mp4")
                logger.info(f"🎬 클립 완성: {name} ({res['seconds']}초, 장면 {res['scenes']}개)")
                return {"id": meta["id"], "name": meta["name"], "title": name, "seconds": res["seconds"],
                        "scenes": res["scenes"], "saved": saved}

            if not clip_maker.start_job(job):
                return self._send_json({"ok": False, "message": "클립을 만드는 중입니다. 끝난 뒤 다시 눌러 주세요."}, 409)
            self._send_json({"ok": True})

        elif url.path == "/api/review/clip_status":
            import clip_maker
            self._send_json({"ok": True, **clip_maker.job_status()})

        elif url.path == "/api/review/clip_folder":
            folder = Path.home() / "Videos" / "오토로그 클립"
            folder.mkdir(parents=True, exist_ok=True)
            try:
                os.startfile(str(folder))   # type: ignore[attr-defined]
            except Exception as e:
                return self._send_json({"ok": False, "message": f"폴더를 열 수 없습니다: {e}"}, 400)
            self._send_json({"ok": True})

        elif url.path == "/api/delete_photo":
            import user_media
            user_media.delete_photo(str(payload.get("id", "")))
            self._send_json({"ok": True})

        elif url.path == "/api/place_photos":
            content = payload.get("content", "")
            ids = payload.get("photo_ids")
            if not isinstance(content, str) or not isinstance(ids, list) or len(content) > 60000:
                return self._send_json({"ok": False, "message": "잘못된 입력 형식"}, 400)
            photos = user_photo_list(ids)
            if not photos:
                return self._send_json({"ok": False, "message": "올린 사진이 없습니다."}, 400)
            lines = [l.rstrip() for l in content.replace("\r\n", "\n").split("\n")]
            if payload.get("reset"):
                import user_media
                lines = [l for l in lines if user_media.photo_marker(l) is None]
            lines = place_user_photos(lines, photos, load_config().get("gemini_api_key", ""))
            self._send_json({"ok": True, "contents": lines})

        elif url.path == "/api/publish":
            title = payload.get("title", "")
            content = payload.get("content", "")
            tags = payload.get("tags", "")
            category = payload.get("category", "")
            auto_thumb = payload.get("auto_thumb", True)
            public_open = payload.get("public_open", True)
            schedule_at = str(payload.get("schedule_at") or "").strip()
            photo_ids = payload.get("photo_ids") if isinstance(payload.get("photo_ids"), list) else []
            if not all(isinstance(v, str) for v in (title, content, tags, category)):
                return self._send_json({"ok": False, "message": "잘못된 입력 형식"}, 400)
            if len(title) > 300 or len(content) > 60000 or len(tags) > 2000:
                return self._send_json({"ok": False, "message": "제목/본문/태그 길이가 너무 깁니다."}, 400)

            if not start_exclusive_task(run_publish_task, title, content, tags, category,
                                        bool(auto_thumb), None, bool(public_open), schedule_at,
                                        [p for p in photo_ids if isinstance(p, str)]):
                return self._send_json({"ok": False, "message": "다른 작업(발행/로그인/설치)이 진행 중입니다."}, 409)
            self._send_json({"ok": True, "message": "포스팅 작업 시작"})

        elif url.path == "/api/neighbor/start":
            import neighbor_service
            nb = neighbor_service.NeighborBot.get_instance()
            cfg = config_mod.load_config()
            nb_cfg = neighbor_service.load_neighbor_config()
            if isinstance(payload, dict):
                nb_cfg.update(payload)
            target_acc = (nb_cfg.get("account_id") or "").strip() or cfg.get("active_account") or ""
            target_profile = config_mod.account_profile_dir(cfg, target_acc)

            # TASK_LOCK이 걸려있을 때: 동일한 계정의 작업인지 확인
            if TASK_LOCK.locked():
                cur_task = CURRENT_TASK or {}
                cur_acc = cur_task.get("account") or cfg.get("active_account") or ""
                cur_profile = config_mod.account_profile_dir(cfg, cur_acc)
                if cur_profile.resolve() == target_profile.resolve():
                    return self._send_json({"ok": False, "message": f"계정 '{target_acc}'(으)로 발행/로그인이 진행 중입니다. 완료 후 시작해 주세요."}, 409)

            ok, msg = nb.start(payload if isinstance(payload, dict) else None)
            self._send_json({"ok": ok, "message": msg})

        elif url.path == "/api/neighbor/stop":
            import neighbor_service
            ok, msg = neighbor_service.NeighborBot.get_instance().stop()
            self._send_json({"ok": ok, "message": msg})

        elif url.path == "/api/neighbor/pause":
            import neighbor_service
            ok, msg = neighbor_service.NeighborBot.get_instance().pause()
            self._send_json({"ok": ok, "message": msg})

        elif url.path == "/api/neighbor/config":
            if not isinstance(payload, dict):
                return self._send_json({"ok": False, "message": "잘못된 설정 데이터입니다."}, 400)
            import neighbor_service
            neighbor_service.save_neighbor_config(payload)
            self._send_json({"ok": True, "message": "이웃 순회 설정이 저장되었습니다."})

        elif url.path == "/api/history/clear":
            if TASK_LOCK.locked():
                return self._send_json({"ok": False, "message": "발행 작업이 진행 중입니다. 끝난 뒤 다시 시도해 주세요."}, 409)
            config_mod.clear_history()
            self._send_json({"ok": True, "message": "발행 이력이 모두 삭제되었습니다."})

        elif url.path == "/api/neighbor/history/clear":
            import neighbor_service
            neighbor_service.save_neighbor_history([])
            self._send_json({"ok": True, "message": "이웃 순회 이력이 초기화되었습니다."})

        elif url.path == "/api/neighbor/blacklist/add":
            target_id = str(payload.get("id", "")).strip()
            if not target_id:
                return self._send_json({"ok": False, "message": "블로그 ID를 입력해주세요."}, 400)
            import neighbor_service
            bl = neighbor_service.load_neighbor_blacklist()
            if target_id not in bl:
                bl.append(target_id)
                neighbor_service.save_neighbor_blacklist(bl)
            self._send_json({"ok": True, "blacklist": bl})

        elif url.path == "/api/neighbor/blacklist/remove":
            target_id = str(payload.get("id", "")).strip()
            import neighbor_service
            bl = neighbor_service.load_neighbor_blacklist()
            bl = [x for x in bl if x != target_id]
            neighbor_service.save_neighbor_blacklist(bl)
            self._send_json({"ok": True, "blacklist": bl})

        elif url.path == "/api/neighbor/cancel_sent_requests":
            import neighbor_service
            acc_id = str(payload.get("account_id", "")).strip()
            days = int(payload.get("older_than_days", 7))
            res = neighbor_service.cancel_unanswered_buddy_requests(account_id=acc_id, older_than_days=days,
                                                                    dry_run=bool(payload.get("dry_run")))
            self._send_json(res)

        elif url.path == "/api/neighbor/clean_ghost_buddies":
            import neighbor_service
            acc_id = str(payload.get("account_id", "")).strip()
            months = int(payload.get("inactive_months", 6))
            whitelist = payload.get("whitelist", [])
            res = neighbor_service.clean_ghost_buddies(   # 이웃 목록 전체를 본다(예전엔 앞 5페이지만 봤음)
                account_id=acc_id,
                inactive_months=months,
                whitelist=whitelist,
                dry_run=bool(payload.get("dry_run")),
                keep_mutual=bool(payload.get("keep_mutual")),
            )
            self._send_json(res)

        elif url.path == "/api/neighbor/manage_received_requests":
            import neighbor_service
            acc_id = str(payload.get("account_id", "")).strip()
            action_type = str(payload.get("action_type", "accept_filtered")).strip()
            keywords = payload.get("spam_keywords", None)
            reject_default = bool(payload.get("reject_default_message", True))
            res = neighbor_service.manage_received_buddy_requests(
                account_id=acc_id,
                action_type=action_type,
                spam_keywords=keywords,
                reject_default_message=reject_default,
                dry_run=bool(payload.get("dry_run")),
            )
            self._send_json(res)

        else:
            self.send_response(404)
            self.end_headers()

def shutdown_app():
    """업데이트 설치를 위해 프로그램 종료 (창 모드면 창을 닫아 정상 종료 흐름으로)."""
    if APP_WINDOW and APP_WINDOW.window:
        try:
            APP_WINDOW.allow_close = True   # 종료 창 없이 바로 닫기
            APP_WINDOW._stop_tray()
            APP_WINDOW.window.destroy()
            return
        except Exception:
            pass
    import desktop
    desktop.clear_instance()
    os._exit(0)


def main():
    global APP_WINDOW
    import desktop
    from config import USER_DIR
    desktop.setup_logging_and_streams()
    try:
        import user_media, schedule_queue
        user_media.cleanup_old(keep=schedule_queue.photo_ids_in_use())
    except Exception:
        pass

    # 이미 실행 중이면 기존 창(또는 대시보드)을 앞으로 띄우고 종료
    running = desktop.find_running_instance()
    if running:
        if not desktop.activate_instance(running):
            webbrowser.open(f"http://127.0.0.1:{running}/?t={desktop.read_instance().get('token', '')}")
        return

    # 8765가 다른 프로그램에 점유돼 있으면 빈 포트로 (창이 포트를 알아서 연결하므로 사용자는 신경 쓸 필요 없음)
    port = desktop.pick_port(8765)
    set_allowed_port(port)
    # 로컬 전용 바인딩 (같은 네트워크의 다른 기기에서 접근 불가)
    server = ThreadingHTTPServer(("127.0.0.1", port), RequestHandler)
    url = f"http://127.0.0.1:{port}/?t={API_TOKEN}"
    desktop.write_instance(port, API_TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"오토로그 {APP_VERSION} 시작: http://127.0.0.1:{port}")
    updater.detect_just_updated()
    updater.cleanup_old_downloads()
    updater.start_background_checks()
    licensing.start_background()
    start_queue_worker()

    use_window = os.name == "nt" and "--browser" not in sys.argv
    try:
        if use_window:
            APP_WINDOW = desktop.AppWindow(url, is_busy=is_busy)
            if APP_WINDOW.start(USER_DIR / "webview"):
                return  # 창을 닫으면 종료
            APP_WINDOW = None
            logger.warning("프로그램 창(WebView2)을 열 수 없어 기본 브라우저로 엽니다.")
        webbrowser.open(url)
        if IS_FROZEN:
            # 콘솔 없는 EXE: 메시지 상자를 닫을 때까지 실행 유지
            desktop.message_box("오토로그가 기본 브라우저에서 실행 중입니다.\n\n"
                                "[확인]을 누르면 프로그램이 종료됩니다.")
        else:
            print("종료하려면 Ctrl+C")
            threading.Event().wait()
    except KeyboardInterrupt:
        print("\n프로그램을 종료합니다.")
    finally:
        desktop.clear_instance()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
