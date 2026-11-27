"""프로그램 버전·업데이트 배포처 (단일 기준). packaging\\release.bat 이 이 값을 읽어 빌드·배포한다."""
APP_VERSION = "1.0.65"
# 화면에 보이는 버전. 라이선스 업데이트처럼 버전을 바꾸지 않는 배포는 APP_VERSION(내부 번호)만 올리고 이 값은 그대로 둔다
DISPLAY_VERSION = "1.0.39"
# 판매용 제품 이름 (화면·창 제목·설치 프로그램에 표시). 내부 폴더·파일 이름(NaverBlogStudio)은 업데이트 호환 때문에 그대로
PRODUCT_NAME = "오토로그"
PRODUCT_NAME_EN = "AutoLog"
PRODUCT_TAGLINE = "AI 블로그 자동 발행 스튜디오"
# 업데이트 설치 파일을 올리는 GitHub 공개 저장소 "소유자/저장소" (소스 코드는 올리지 않음)
UPDATE_REPO = "lsszz2100/autolog-releases"
# 이 코드가 요구하는 기반(Python·라이브러리·Chromium) 번호. requirements.txt / spec / launcher.py 를 바꿨을 때만 올리고,
# 그때는 packaging\release_full.bat(전체 설치 파일)로 배포. 평소 코드 수정은 packaging\release.bat(코드만)으로 배포.
REQUIRED_RUNTIME = 4
