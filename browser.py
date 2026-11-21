"""N사 전용 브라우저 컨텍스트 실행 공용 모듈 (browser.py)

app.py(로그인·발행), main.py(CLI)가 같은 프로필·옵션·스텔스 설정으로 브라우저를 띄우도록 통일.
"""
from config import account_profile_dir
from stealth import apply_stealth

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-infobars",
    "--lang=ko-KR",
]


def launch_naver_context(pw, headless: bool = False, extra_args=(), profile_dir=None):
    """로그인 세션이 저장된 영구 프로필로 Chromium을 띄우고 스텔스 패치를 적용한 컨텍스트를 반환.
    profile_dir: 계정별 프로필 (없으면 선택된 계정)."""
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir or account_profile_dir()),
        headless=headless,
        # headless에서도 별도 headless-shell 대신 정식 Chromium(새 headless 모드) 사용
        # → 설치 프로그램에 headless-shell(약 280MB)을 넣지 않아도 됨
        channel="chromium",
        args=BROWSER_ARGS + list(extra_args),
        locale="ko-KR",
        timezone_id="Asia/Seoul",
        viewport={"width": 1280, "height": 900},
    )
    apply_stealth(context)
    return context


def first_page(context):
    return context.pages[0] if context.pages else context.new_page()
