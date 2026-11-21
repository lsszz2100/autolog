"""N사 자동 로그인 및 스마트에디터 ONE 자동 포스팅 메인 CLI 스크립트 (main.py)
"""
import argparse
import getpass
import logging
from datetime import datetime
from playwright.sync_api import sync_playwright

from config import load_config, save_config, add_history, check_publish_limits, PROFILE_DIR, CONFIG_FILE
from browser import launch_naver_context, first_page
from auth import is_logged_in, check_login_status, login_naver
from publisher import SmartEditorPublisher
from thumbnail import generate_blog_thumbnail

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("naver.main")

def run(args):
    cfg = load_config()

    # 인자로 넘어온 설정 우선 적용
    naver_id = args.id or cfg.get("naver_id")
    if args.pw:
        logger.warning("⚠️ --pw 인자는 셸 기록·프로세스 목록에 비밀번호가 남습니다. 웹 UI에 저장하거나 실행 중 입력을 권장합니다.")
    naver_pw = args.pw or cfg.get("naver_pw")
    if naver_id and not naver_pw:
        naver_pw = getpass.getpass("N사 비밀번호 (화면에 표시되지 않음): ")
    blog_id = args.blog_id or cfg.get("blog_id") or naver_id
    category = args.category or cfg.get("post_category", "")
    headless = args.headless if args.headless is not None else cfg.get("headless", False)

    if not naver_id or not naver_pw:
        logger.error("❌ N사 계정 정보(ID, PW)가 설정되지 않았습니다.")
        logger.info(f"웹 UI 또는 config.json({CONFIG_FILE})에 설정하거나 --id 인자로 전달해주세요.")
        return

    # 웹 UI와 같은 발행 간격/일일 제한 적용 (로그인 전용 모드 제외)
    if not args.login_only:
        limit_msg = check_publish_limits(datetime.now(), cfg)
        if limit_msg:
            logger.error(f"❌ 발행 제한: {limit_msg}")
            return

    # 설정 변경사항 저장
    cfg["naver_id"] = naver_id
    cfg["naver_pw"] = naver_pw
    cfg["blog_id"] = blog_id
    if category:
        cfg["post_category"] = category
    save_config(cfg)

    logger.info(f"🚀 N사 자동화 프로세스 시작 (계정: {naver_id}, 블로그: {blog_id})")
    logger.info(f"📁 세션 프로필 경로: {PROFILE_DIR}")

    with sync_playwright() as pw:
        logger.info(f"브라우저 컨텍스트 실행 (headless={headless})...")
        context = launch_naver_context(pw, headless=headless, extra_args=["--start-maximized"])
        page = first_page(context)

        try:
            # 1. 로그인 상태 확인
            logger.info("기존 로그인 세션 확인 중...")
            if not is_logged_in(context) or not check_login_status(page):
                logger.info("기존 세션이 없거나 만료되었습니다. 로그인을 진행합니다.")
                ok = login_naver(page, naver_id, naver_pw)
                if not ok:
                    logger.error("❌ 로그인에 실패했습니다. 종료합니다.")
                    return
            else:
                logger.info("✅ 기존 로그인 세션이 유효합니다! (자동 로그인 생략)")

            if args.login_only:
                logger.info("로그인 전용 모드 완료. 세션이 프로필 폴더에 저장되었습니다.")
                return

            # 2. 포스팅 내용 준비
            if args.keyword:
                from ai import generate_blog_post
                style = args.style or "info"
                title, contents, tags = generate_blog_post(args.keyword, style=style)
            else:
                title = args.title or "N사 블로그 자동 포스팅 테스트"
                if args.file:
                    with open(args.file, "r", encoding="utf-8") as f:
                        contents = [line.strip() for line in f if line.strip()]
                elif args.content:
                    contents = [line.strip() for line in args.content.split("\\n") if line.strip()]
                else:
                    contents = [
                        "안녕하세요! N사 스마트에디터 ONE 자동 포스팅 시스템 테스트 글입니다.",
                        "본 포스팅은 Playwright 자동화 엔진과 스텔스 안티봇 우회 모듈을 통해 작성되었습니다.",
                        "■ 주요 특징",
                        "- N사 캡차 우회 로그인 및 영구 세션 재사용",
                        "- 스마트에디터 ONE DOM 정밀 제어 및 서식 최적화",
                        "- 불필요한 백도어와 애드웨어를 완전히 제거한 클린 엔진",
                        "감사합니다. 앞으로 유익한 정보로 찾아뵙겠습니다!"
                    ]
                tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else ["블로그자동화", "스마트에디터ONE", "파이썬포스팅"]

            # 썸네일 이미지 처리
            image_paths = []
            if args.thumb:
                try:
                    thumb_path = generate_blog_thumbnail(title=title, tag_text=tags[0] if tags else "BLOG")
                    image_paths.append(thumb_path)
                    logger.info("대표 썸네일 이미지 자동 생성 완료.")
                except Exception as e:
                    logger.warning(f"썸네일 생성 실패: {e}")

            # 3. 스마트에디터 ONE 글 작성 및 발행
            publisher = SmartEditorPublisher(page, blog_id=blog_id)
            published_url = publisher.publish(
                title=title,
                contents=contents,
                tags=tags,
                category_name=category,
                image_paths=image_paths,
                public_open=True
            )

            # 이력 저장
            add_history(title=title, url=published_url, category=category, tags=",".join(tags))

            print("\n" + "="*60)
            print("🎉 포스팅 발행이 성공적으로 완료되었습니다!")
            print(f"🔗 발행 글 URL: {published_url}")
            print("="*60 + "\n")

        finally:
            page.wait_for_timeout(3000)
            context.close()

def main():
    parser = argparse.ArgumentParser(description="AutoLog blog publisher")
    parser.add_argument("--id", help="N사 아이디")
    parser.add_argument("--pw", help="N사 비밀번호 (비권장: 셸 기록에 남음. 생략 시 저장값 사용 또는 입력 요청)")
    parser.add_argument("--blog-id", help="N사 블로그 ID (기본값: N사 아이디)")
    parser.add_argument("--category", help="블로그 카테고리명")
    parser.add_argument("--title", help="포스팅 제목")
    parser.add_argument("--content", help="포스팅 본문 (줄바꿈: \\n)")
    parser.add_argument("--file", help="포스팅 본문 텍스트 파일 경로")
    parser.add_argument("--tags", help="콤마(,)로 구분된 태그 목록")
    parser.add_argument("--keyword", help="AI 자동 원고 생성 키워드 (예: '에어프라이어 요리')")
    parser.add_argument("--style", default="info", choices=["info", "review", "tips", "compare", "howto", "qna", "checklist", "issue"], help="AI 글 구성 템플릿")
    parser.add_argument("--thumb", action="store_true", help="고화질 1:1 대표 썸네일 자동 생성 및 첨부")
    parser.add_argument("--headless", action="store_true", default=None, help="브라우저 화면을 띄우지 않고 백그라운드 실행")
    parser.add_argument("--login-only", action="store_true", help="글을 쓰지 않고 로그인 및 세션 저장만 수행")

    args = parser.parse_args()
    run(args)

if __name__ == "__main__":
    main()
