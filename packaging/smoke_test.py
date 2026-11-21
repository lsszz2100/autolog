"""배포 전 자동 점검 (make_release.py 가 업로드 전에 실행). 실패하면 배포 중단.

1) 프로그램 코드 전체 문법 검사(컴파일)  2) 대시보드를 화면 없이 띄워 스크립트 오류가 없는지,
버전 표시가 정상인지 확인 (화면 스크립트 오류는 사용자 화면 전체를 멈추게 하므로 반드시 차단)
"""
import os
import sys
import threading
import py_compile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# --code-dir <폴더>: 그 폴더의 코드(컴파일된 .pyd)를 먼저 불러와 점검
CODE_DIR = ""
if "--code-dir" in sys.argv:
    i = sys.argv.index("--code-dir")
    CODE_DIR = sys.argv[i + 1]
    del sys.argv[i:i + 2]
    sys.path.insert(0, CODE_DIR)
cache = ROOT / "packaging" / ".chromium-cache"
if cache.is_dir() and sys.platform == "win32":
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(cache))


def main(modules):
    for m in modules:
        py_compile.compile(str(ROOT / f"{m}.py"), doraise=True)
    print(f"  ✔ 코드 문법 검사 통과 ({len(modules)}개 파일)")
    if CODE_DIR:
        # 실제 배포되는 기계어 코드(.pyd)로 점검
        import app as _a
        if not str(getattr(_a, "__file__", "")).endswith(".pyd"):
            print(f"  ❌ 컴파일된 코드가 아니라 원본을 불러왔습니다: {_a.__file__}")
            sys.exit(1)
        print("  ✔ 컴파일된 코드(.pyd)로 점검합니다")

    import app, desktop  # noqa: E401
    from version import APP_VERSION
    from http.server import ThreadingHTTPServer
    port = desktop.pick_port(0)
    app.set_allowed_port(port)
    srv = ThreadingHTTPServer(("127.0.0.1", port), app.RequestHandler)
    srv.RequestHandlerClass.log_message = lambda *a, **k: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from playwright.sync_api import sync_playwright
    errors = []
    try:
        with sync_playwright() as pw:
            launch_args = {"headless": True}
            if sys.platform == "win32":
                launch_args["channel"] = "chromium"
            b = pw.chromium.launch(**launch_args)
            page = b.new_page()
            page.on("pageerror", lambda e: errors.append(f"스크립트 오류: {e}"))
            page.goto(f"http://127.0.0.1:{port}/?t={app.API_TOKEN}", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            ver = page.inner_text("#app-version")
            from version import DISPLAY_VERSION
            if ver != f"v{DISPLAY_VERSION}":
                errors.append(f"버전 표시 이상: '{ver}' (기대 v{APP_VERSION})")
            for fn in ("pollUpdate", "startPublish", "generateAiDraft", "applySchedule", "loadData", "addPhotos", "placePhotos", "removePhoto", "openCloseDialog", "chooseClose", "selectAccount", "activateLicense", "refreshLicense", "showLicenseScreen", "addToQueue", "loadSchedules", "cancelSchedule", "previewBatch", "startBatch", "renderBatchStatus", "retrySchedule", "resumeQueue", "startNeighbor", "stopNeighbor", "pauseNeighbor", "loadNeighborConfig", "loadNeighborHistory", "switchMainTab", "restoreMainTab", "runCancelUnanswered", "runCleanGhostBuddies", "runManageReceivedRequests", "clipScript", "clipMake", "renderClipScenes"):
                if not page.evaluate(f"typeof {fn} === 'function'"):
                    errors.append(f"화면 함수 없음: {fn}")
            b.close()
    finally:
        srv.shutdown()
    if errors:
        print("  ❌ 대시보드 점검 실패:\n   - " + "\n   - ".join(errors))
        sys.exit(1)
    print("  ✔ 대시보드 점검 통과 (스크립트 오류 없음, 버전 표시 정상)")


if __name__ == "__main__":
    main(sys.argv[1:])
