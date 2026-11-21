# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 빌드 설정 (packaging\build_installer.bat 에서 호출)
# - onedir: 실행 속도·백신 오탐 측면에서 onefile보다 유리. 설치 프로그램(Inno Setup)이 폴더째 설치
# - Chromium(ms-playwright)은 빌드 후 dist 폴더에 따로 내려받아 넣음 (config.py가 실행 시 경로 지정)
from glob import glob
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = SPECPATH + "\\.."
# 배포 코드는 기계어로 컴파일한 .pyd (packaging\compile_code.py 결과). 원본 .py 는 넣지 않음 (리버싱 방지)
COMPILED = ROOT + "\\packaging\\compiled"
compiled_code = [(p, "appcode") for p in glob(COMPILED + "\\*.pyd")] + [
    (COMPILED + "\\" + n, "appcode") for n in ("version.py", "manifest.json", "manifest.sig")]
if len(compiled_code) < 10:
    raise SystemExit("컴파일된 코드가 없습니다. packaging\\compile_code.py 를 먼저 실행하세요.")

pw_datas, pw_binaries, pw_hidden = collect_all("playwright")
# 프로그램 창(pywebview + Edge WebView2, .NET 연동 pythonnet)
wv_datas, wv_binaries, wv_hidden = collect_all("webview")
pn_datas, pn_binaries, pn_hidden = collect_all("pythonnet")
cl_datas, cl_binaries, cl_hidden = collect_all("clr_loader")

# 프로그램 코드(자주 바뀜): 실행 파일에 묶지 않고 _internal\appcode\*.py 로 배치 → 코드만 업데이트 가능
# (분석에는 포함해 이 코드들이 쓰는 라이브러리는 기반에 들어가게 하고, 아래에서 PYZ에서만 뺌)
local_modules = [
    "app", "ai", "auth", "browser", "config", "desktop", "gemini_client", "updater", "version", "image_service",
    "keyword_service", "publisher", "reference_service", "rich_publisher", "stealth", "text_style", "thumbnail", "user_media", "sigcheck", "licensing", "schedule_queue", "topic_service", "neighbor_service", "shopping_service", "shopping_connect", "review_post", "clip_maker",
]
# 이후 코드 업데이트에서 새로 쓸 수 있는 표준 라이브러리를 미리 포함 (없으면 전체 설치 업데이트가 필요해짐)
stdlib_extra = [
    "csv", "sqlite3", "zipfile", "tarfile", "shutil", "uuid", "hashlib", "hmac", "secrets", "base64", "html", "html.parser",
    "xml.etree.ElementTree", "email", "email.mime.text", "email.mime.multipart", "smtplib", "mimetypes", "difflib",
    "textwrap", "unicodedata", "statistics", "random", "math", "fractions", "decimal", "calendar", "zoneinfo",
    "concurrent.futures", "queue", "asyncio", "dataclasses", "enum", "functools", "itertools", "collections",
    "typing", "pathlib", "glob", "fnmatch", "tempfile", "gzip", "bz2", "lzma", "pickle", "copy", "pprint",
    "string", "struct", "ctypes", "ctypes.wintypes", "winreg", "platform", "locale", "gettext", "argparse",
    "configparser", "logging.handlers", "http.cookiejar", "urllib.parse", "urllib.request", "ssl", "socket", "select",
    "json", "re", "datetime", "time", "threading", "subprocess", "webbrowser",
]

a = Analysis(
    [ROOT + "\\launcher.py"],
    pathex=[ROOT],
    binaries=pw_binaries + wv_binaries + pn_binaries + cl_binaries,
    datas=pw_datas + wv_datas + pn_datas + cl_datas
        + compiled_code
        + [(ROOT + "\\packaging\\runtime.txt", ".")],
    hiddenimports=local_modules + stdlib_extra + pw_hidden + wv_hidden + pn_hidden + cl_hidden + ["clr"]
        + collect_submodules("keyring.backends")
        + collect_submodules("pystray")
        + ["win32ctypes.core", "win32ctypes.pywin32"],
    excludes=["tkinter", "unittest", "pydoc_data", "test"],
    noarchive=False,
)
# 프로그램 코드는 PYZ에서 제외 (appcode 폴더의 .py가 sys.path로 로드되도록)
a.pure = [x for x in a.pure if x[0] not in local_modules]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NaverBlogStudio",
    icon=ROOT + "\\packaging\\app.ico",
    console=False,         # 콘솔 없이 프로그램 창(WebView2)만 표시. 로그는 %LOCALAPPDATA%\\NaverBlogStudio\\logs
    upx=False,             # UPX 압축은 백신 오탐을 늘려 사용 안 함
    version=ROOT + "\\packaging\\version_info.txt",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    upx=False,
    name="NaverBlogStudio",
)
