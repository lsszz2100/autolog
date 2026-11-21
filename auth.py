"""N사 자동 로그인 및 세션 관리 모듈 (auth.py)

Mublo Content Studio 역공학 기반 상용급 N사 안티봇 우회 및 자동 로그인 엔진:
1. bvsd 키로깅 추적 우회: _type_with_real_shift (대문자 및 특수문자 Shift 키 결합 이벤트 완전 재현)
2. JS DOM 직접 주입 전면 배제 (N사 암호화 필드 깨짐 및 봇 탐지 원천 차단)
3. 로그인 유지(#loginStay) 체크 및 IP 보안(#switchIP) 최적화
4. 최신 N사 로그인 버튼 (#loginBtn_column, #loginBtn_row, #log.login, button[type=submit]) 자동 클릭
5. 브라우저/기기 등록 모달 자동 돌파 및 팝업 소멸
6. 영구 세션 쿠키(NID_AUT, NID_SES) 보존
"""
import time
import logging
import os
import re
from playwright.sync_api import Page, BrowserContext

logger = logging.getLogger("naver.auth")

_SHIFTED_SYMBOL_CODES = {
    '~': 'Backquote',
    '!': 'Digit1',
    '@': 'Digit2',
    '#': 'Digit3',
    '$': 'Digit4',
    '%': 'Digit5',
    '^': 'Digit6',
    '&': 'Digit7',
    '*': 'Digit8',
    '(': 'Digit9',
    ')': 'Digit0',
    '_': 'Minus',
    '+': 'Equal',
    '{': 'BracketLeft',
    '}': 'BracketRight',
    '|': 'Backslash',
    ':': 'Semicolon',
    '"': 'Quote',
    '<': 'Comma',
    '>': 'Period',
    '?': 'Slash'
}

def _type_with_real_shift(page: Page, text: str, delay_ms: int = 30):
    """
    N사 bvsd(Bot Verification & Security Detection) 키로거 우회:
    대문자나 특수문자 입력 시 단순 유니코드 삽입이 아닌 실제 물리적 Shift 키 결합 이벤트를 브라우저에 발생시킵니다.
    """
    for ch in text:
        code = None
        if ch.isascii() and ch.isalpha() and ch.isupper():
            code = f"Key{ch}"
        elif ch in _SHIFTED_SYMBOL_CODES:
            code = _SHIFTED_SYMBOL_CODES[ch]

        if code:
            page.keyboard.press(f"Shift+{code}", delay=delay_ms)
        else:
            page.keyboard.type(ch, delay=delay_ms)

def is_logged_in(context: BrowserContext) -> bool:
    """현재 브라우저 컨텍스트에 유효한 N사 인증 쿠키(NID_AUT, NID_SES)가 있는지 확인합니다."""
    try:
        cookies = context.cookies(["https://naver.com", "https://nid.naver.com", "https://blog.naver.com"])
        cookie_names = {c["name"] for c in cookies}
        return ("NID_AUT" in cookie_names) and ("NID_SES" in cookie_names)
    except Exception as e:
        logger.warning(f"쿠키 확인 중 예외: {e}")
        return False

def profile_login_state(profile_dir) -> bool:
    """브라우저를 띄우지 않고 프로필 쿠키 DB에서 N사 로그인 쿠키(NID_AUT·NID_SES)가 살아 있는지 확인.
    (쿠키 값은 암호화돼 있지만 이름·만료 시각은 평문이라 판정 가능) — 대시보드 로그인 표시용"""
    import shutil
    import sqlite3
    import tempfile
    import time as _time
    from pathlib import Path
    db = Path(profile_dir) / "Default" / "Network" / "Cookies"
    if not db.exists():
        return False
    tmp = Path(tempfile.gettempdir()) / f"nbs_cookies_{os.getpid()}.db"
    try:
        shutil.copy2(db, tmp)  # 브라우저 사용 중 잠김 방지
        # Chromium 시간: 1601-01-01 기준 마이크로초
        now = int((_time.time() + 11644473600) * 1_000_000)
        con = sqlite3.connect(str(tmp))
        try:
            rows = con.execute(
                "SELECT name FROM cookies WHERE host_key LIKE '%naver.com' AND name IN ('NID_AUT','NID_SES') "
                "AND (expires_utc = 0 OR expires_utc > ?)", (now,)).fetchall()
        finally:
            con.close()
        names = {r[0] for r in rows}
        return {"NID_AUT", "NID_SES"} <= names
    except Exception as e:
        logger.debug(f"프로필 쿠키 확인 실패: {e}")
        return db.exists()
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def check_login_status(page: Page) -> bool:
    """실제 페이지 접근을 통해 로그인 상태를 검증합니다."""
    try:
        page.goto("https://blog.naver.com", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1000)
        if "nidlogin.login" in page.url or "nid.naver.com" in page.url:
            return False
        return is_logged_in(page.context)
    except Exception as e:
        logger.warning(f"로그인 상태 확인 중 예외: {e}")
        return False

def _dismiss_overlays(page: Page):
    """로그인 전후 나타나는 방해 레이어/팝업들을 닫습니다."""
    selectors = [
        "#new\\.dontsave",
        "button:has-text('등록안함')",
        "a:has-text('등록안함')",
        "button:has-text('나중에')",
        "button:has-text('닫기')",
        "button:has-text('취소')",
        "a:has-text('닫기')"
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=250):
                logger.info(f"방해 팝업 감지 및 클릭: {sel}")
                loc.click(timeout=800)
                page.wait_for_timeout(300)
        except Exception:
            pass

def _configure_naver_login_options(page: Page):
    """로그인 유지(#loginStay) 체크 및 IP 보안 해제(세션 튕김 방지)"""
    try:
        page.evaluate("""
            () => {
                const stay = document.querySelector('#loginStay, #keep');
                if (stay && !stay.checked) { stay.click(); }
                const ip = document.querySelector('#switchIP');
                if (ip && ip.checked) { ip.click(); }
            }
        """)
    except Exception:
        pass

def _click_naver_login_button(page: Page):
    """가시적 N사 로그인 버튼 탐색 및 클릭"""
    selectors = [
        '#loginBtn_column',
        '#loginBtn_row',
        '#log\\.login',
        '.btn_login',
        'button.btn_login',
        "button[type='submit']",
        "input[type='submit']"
    ]

    for sel in selectors:
        try:
            candidate = page.locator(sel).first
            if candidate.count() > 0 and candidate.is_visible(timeout=300):
                logger.info(f"로그인 버튼 클릭 시도 (셀렉터: {sel})")
                candidate.click(timeout=2500)
                return True
        except Exception:
            continue

    # Fallback: Enter 키 입력
    page.wait_for_timeout(200)
    try:
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False

def _auto_click_browser_registration(page: Page) -> bool:
    """새로운 기기/브라우저 등록 화면 감지 시 자동 수락 또는 돌파"""
    try:
        body_text = page.evaluate("() => document.body ? (document.body.innerText || '') : ''")
    except Exception:
        return False

    markers = ['브라우저 등록', '기기 등록', '새로운 기기', '새로운 환경', '보안 확인', '등록하시겠습니까', '등록하면']
    if not any(m in body_text for m in markers):
        return False

    logger.info("기기/브라우저 등록 확인 화면 감지 -> 자동 승인 진행")
    button_texts = ['브라우저 등록', '등록', '등록하기', '이 브라우저 등록', '기기 등록', '확인', '예', '동의', '계속', '다음', '등록안함']
    contexts = [page] + list(page.frames)

    for ctx in contexts:
        for text in button_texts:
            try:
                loc = ctx.locator(f"button:has-text('{text}'), a:has-text('{text}')").first
                if loc.count() > 0 and loc.is_visible(timeout=300):
                    loc.click(timeout=1500)
                    page.wait_for_timeout(1000)
                    return True
            except Exception:
                continue

    return False

def login_naver(page: Page, naver_id: str, naver_pw: str) -> bool:
    """
    Mublo 역공학 기반 실전 N사 자동 로그인:
    - 물리적 키 클릭 및 fill('') 비우기
    - _type_with_real_shift를 통한 인간 타이핑 및 Shift 결합키 재현
    - 로그인 유지 설정 후 제출
    - 브라우저 등록 모달 자동 돌파
    """
    logger.info("N사 로그인 페이지로 이동합니다...")
    page.goto("https://nid.naver.com/nidlogin.login?mode=form&url=https://www.naver.com/", wait_until="domcontentloaded", timeout=25000)
    page.wait_for_timeout(1200)

    _dismiss_overlays(page)

    logger.info(f"계정 자격증명 실전 타이핑 중 (계정: {naver_id})...")

    # 1. 아이디 입력 (클릭 -> 비우기 -> Shift 결합 타이핑)
    try:
        id_loc = page.locator("#id").first
        id_loc.click(timeout=2000)
        id_loc.fill("")
        _type_with_real_shift(page, naver_id, delay_ms=30)
    except Exception as e:
        logger.warning(f"아이디 입력 중 예외: {e}")
        page.keyboard.type(naver_id, delay=30)

    page.wait_for_timeout(300)

    # 2. 비밀번호 입력 (클릭 -> 비우기 -> Shift 결합 타이핑)
    try:
        pw_loc = page.locator("#pw").first
        pw_loc.click(timeout=2000)
        pw_loc.fill("")
        _type_with_real_shift(page, naver_pw, delay_ms=30)
    except Exception as e:
        logger.warning(f"비밀번호 입력 중 예외: {e}")
        page.keyboard.type(naver_pw, delay=30)

    page.wait_for_timeout(400)

    # 3. 로그인 유지 옵션 활성화
    _configure_naver_login_options(page)
    page.wait_for_timeout(300)

    # 4. 로그인 버튼 클릭 실행
    logger.info("로그인 제출 실행...")
    _click_naver_login_button(page)

    # 5. 로그인 완료 또는 보안 확인 대기
    logger.info("로그인 완료 및 리다이렉트 대기 중...")
    start_time = time.time()
    
    while time.time() - start_time < 20:
        page.wait_for_timeout(1000)
        curr_url = page.url

        # 기기 등록 및 오버레이 처리
        _dismiss_overlays(page)
        _auto_click_browser_registration(page)

        # 캡차 또는 2단계 인증 감지
        page_html = page.content().lower()
        if "captcha" in page_html or page.locator("#captcha, img[alt*='captcha']").count() > 0:
            logger.warning("⚠️ 자동입력 방지문자(CAPTCHA) 또는 2단계 인증이 감지되었습니다!")
            logger.warning("화면에 열려 있는 브라우저 창에서 직접 인증을 완료해 주세요 (최대 60초 대기)...")
            try:
                page.wait_for_url(lambda u: "nidlogin.login" not in u and "nid.naver.com" not in u, timeout=60000)
            except Exception:
                pass
            break

        # 정상 로그인되어 N사 메인 또는 블로그로 넘어갔는지 확인
        if ("nidlogin.login" not in curr_url) and ("nid.naver.com" not in curr_url):
            logger.info("✔ 로그인 성공 (페이지 리다이렉트 완료)!")
            break

    page.wait_for_timeout(2000)
    _dismiss_overlays(page)
    _auto_click_browser_registration(page)

    success = is_logged_in(page.context)
    if success:
        logger.info("🎉 N사 인증 세션 쿠키(NID_AUT, NID_SES) 확인 완료!")
    else:
        if "nidlogin" not in page.url:
            success = check_login_status(page)
        if not success:
            logger.error("❌ 로그인 실패: 인증 세션이 확인되지 않았습니다. 아이디/비밀번호 또는 보안 인증을 확인해주세요.")
            
    return success


def detect_own_blog_id(page: Page) -> str:
    """로그인된 계정의 실제 블로그 주소(ID) 확인. N사 아이디와 블로그 주소가 다른 계정이 있음 (예: 아이디 abc, 블로그 blog.naver.com/xyz).
    'MyBlog.naver' 는 로그인한 사람의 블로그로 이동한다. 못 찾으면 ""."""
    try:
        page.goto("https://blog.naver.com/MyBlog.naver", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)
        m = re.match(r"https://blog\.naver\.com/([A-Za-z0-9_-]{2,40})(?:[/?#]|$)", page.url)
        if m and not m.group(1).endswith(".naver"):
            return m.group(1)
        for f in page.frames:
            m = re.search(r"PostList\.naver\?blogId=([A-Za-z0-9_-]{2,40})", f.url)
            if m:
                return m.group(1)
    except Exception as e:
        logger.warning(f"블로그 주소 확인 실패: {e}")
    return ""

