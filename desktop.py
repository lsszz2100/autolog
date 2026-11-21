"""데스크톱 실행 지원 모듈 (desktop.py)

- 대시보드를 브라우저 탭 대신 독립 프로그램 창(Edge WebView2, pywebview)으로 표시
- 중복 실행 방지: 실행 중인 인스턴스의 포트를 instance.json 에 기록 → 두 번째 실행은 기존 창을 앞으로 띄우고 종료
- 콘솔 없는 EXE(windowed)에서 stdout/stderr 가 None 이면 로그 파일로 연결 (Playwright 가 stderr fileno 를 요구)
"""
import os
import sys
import json
import time
import socket
import logging
import threading
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import USER_DIR

from version import PRODUCT_NAME
APP_TITLE = PRODUCT_NAME
APP_SIGNATURE = "NaverBlogStudio"
LOG_DIR = USER_DIR / "logs"
INSTANCE_FILE = USER_DIR / "instance.json"


def setup_logging_and_streams():
    """콘솔이 없으면 표준 출력을 파일로 돌리고, 'naver' 로거를 회전 로그 파일에도 기록."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        stream = open(LOG_DIR / "console.log", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
    handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger("naver").addHandler(handler)


def pick_port(preferred: int = 8765) -> int:
    """선호 포트가 비어 있으면 사용, 다른 프로그램이 쓰고 있으면 OS가 고른 빈 포트."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise OSError("사용 가능한 포트가 없습니다.")


def read_instance() -> dict:
    try:
        return json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _request(port: int, path: str, method: str = "GET", timeout: float = 2.0, token: str = ""):
    headers = {"Content-Type": "application/json"} if method == "POST" else {}
    if token:
        headers["X-NBS-Token"] = token
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=b"{}" if method == "POST" else None,
        headers=headers,
    )
    return urllib.request.urlopen(req, timeout=timeout)


def find_running_instance():
    """기록된 포트에서 이 프로그램이 응답하면 그 포트를 반환 (다른 프로그램이 같은 포트를 써도 오인하지 않음)."""
    try:
        port = int(json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))["port"])
        with _request(port, "/api/ping") as r:
            if json.loads(r.read().decode("utf-8")).get("app") == APP_SIGNATURE:
                return port
    except Exception:
        pass
    return None


def activate_instance(port: int) -> bool:
    try:
        with _request(port, "/api/focus", method="POST", token=read_instance().get("token", "")):
            return True
    except Exception:
        return False


def write_instance(port: int, token: str = ""):
    """실행 정보 (사용자 본인 폴더에만 저장 → 같은 PC의 다른 Windows 사용자는 접속 열쇠를 읽을 수 없음)."""
    INSTANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    INSTANCE_FILE.write_text(json.dumps({"port": port, "pid": os.getpid(), "token": token}), encoding="utf-8")


def clear_instance():
    try:
        if json.loads(INSTANCE_FILE.read_text(encoding="utf-8")).get("pid") == os.getpid():
            INSTANCE_FILE.unlink()
    except Exception:
        pass


def message_box(text: str, title: str = APP_TITLE, yes_no: bool = False) -> bool:
    """Windows 기본 메시지 상자 (GUI 스레드에서도 안전). yes_no=True면 '예' 선택 시 True."""
    if os.name != "nt":
        print(f"[{title}] {text}")
        return True
    import ctypes
    MB_YESNO, MB_ICONWARNING, MB_ICONINFO, MB_TOPMOST, IDYES = 0x4, 0x30, 0x40, 0x40000, 6
    flags = (MB_YESNO | MB_ICONWARNING) if yes_no else MB_ICONINFO
    return ctypes.windll.user32.MessageBoxW(None, text, title, flags | MB_TOPMOST) == IDYES if yes_no else True


def _tray_image():
    """트레이 아이콘 (초록 바탕 흰 N)."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(3, 199, 90, 255))
    try:
        font = ImageFont.truetype("arialbd.ttf", 40)
    except Exception:
        font = ImageFont.load_default()
    d.text((32, 33), "N", fill="white", font=font, anchor="mm")
    return img


class AppWindow:
    """pywebview 창 래퍼. WebView2를 쓸 수 없으면 start()가 False를 반환 (호출 측이 브라우저로 대체)."""

    def __init__(self, url: str, is_busy):
        self.url = url
        self.is_busy = is_busy  # 발행·로그인 등 작업 중이면 True를 반환하는 함수
        self.window = None
        self.allow_close = False   # True면 종료 창 없이 바로 닫힘 (바로 종료·업데이트 재시작)
        self.tray = None
        self.auto_quit = False     # 트레이 숨김 중: 작업이 끝나면 자동 종료

    def focus(self):
        w = self.window
        if not w:
            return
        # 트레이에 숨어 있었다면 다시 창으로 (작업 후 자동 종료는 취소)
        self.auto_quit = False
        self._stop_tray()
        try:
            w.restore()
            w.show()
            # 다른 창 뒤에 있을 때 앞으로 가져오기.
            # w.on_top은 쓰지 않는다: 다른 스레드에서 창 속성을 직접 바꿔 프로그램 전체가 멈춘다 (1.0.19에서 확인)
            self._raise_window()
        except Exception:
            pass

    def _raise_window(self):
        if os.name != "nt":
            return
        import ctypes
        hwnd = ctypes.c_void_p(int(self.window.native.Handle.ToInt64()))
        SWP_NOSIZE_NOMOVE_NOACTIVATE = 0x0013
        for insert_after in (-1, -2):   # HWND_TOPMOST로 올렸다가 HWND_NOTOPMOST로 되돌림
            ctypes.windll.user32.SetWindowPos(hwnd, ctypes.c_void_p(insert_after), 0, 0, 0, 0, SWP_NOSIZE_NOMOVE_NOACTIVATE)

    def _on_closing(self):
        """X 버튼: 바로 닫지 않고 화면 안 종료 창(바로 종료 / 트레이로 숨기고 작업 후 종료 / 종료하지 않음)을 띄움."""
        if self.allow_close:
            return True
        threading.Thread(target=self._ask_close, daemon=True).start()
        return False

    def _ask_close(self):
        try:
            self.window.evaluate_js(f"openCloseDialog({json.dumps(bool(self.is_busy()))})")
        except Exception:
            # 화면 스크립트를 쓸 수 없으면 기본 확인 창
            busy = self.is_busy()
            if message_box(("발행·로그인 작업이 진행 중입니다.\n지금 종료하면 작업이 중단됩니다. " if busy else "")
                           + "프로그램을 종료할까요?", yes_no=True):
                self.quit()

    def quit(self):
        """바로 종료 (창을 닫으면 프로그램 종료 흐름으로)."""
        self.allow_close = True
        self._stop_tray()
        try:
            self.window.destroy()
        except Exception:
            os._exit(0)

    # ── 트레이 ──
    def hide_to_tray(self) -> bool:
        """창을 숨기고 알림 영역(트레이)에 아이콘 표시. 진행 중인 작업이 끝나면 자동 종료."""
        try:
            import pystray
        except Exception as e:
            logging.getLogger("naver.desktop").warning(f"트레이 아이콘을 쓸 수 없습니다: {e}")
            return False
        self.auto_quit = True
        icon = pystray.Icon(
            APP_SIGNATURE, _tray_image(), f"{APP_TITLE} - 작업 진행 중 (끝나면 자동 종료)",
            menu=pystray.Menu(
                pystray.MenuItem("창 열기", lambda: self.focus(), default=True),
                pystray.MenuItem("지금 종료 (작업 중단)", lambda: self.quit()),
            ))
        self.tray = icon
        icon.run_detached()
        try:
            self.window.hide()
        except Exception:
            pass
        try:
            icon.notify("창을 숨겼습니다. 작업이 끝나면 자동으로 종료됩니다.", APP_TITLE)
        except Exception:
            pass
        threading.Thread(target=self._quit_when_idle, daemon=True).start()
        return True

    def _quit_when_idle(self):
        while self.auto_quit:
            if not self.is_busy():
                if not self.auto_quit:
                    return
                try:
                    if self.tray:
                        self.tray.notify("작업이 끝나 프로그램을 종료합니다.", APP_TITLE)
                except Exception:
                    pass
                time.sleep(3)
                if self.auto_quit:
                    self.quit()
                return
            time.sleep(2)

    def _stop_tray(self):
        icon, self.tray = self.tray, None
        if icon:
            try:
                icon.stop()
            except Exception:
                pass

    def start(self, storage_dir: Path) -> bool:
        try:
            import webview
        except Exception as e:
            logging.getLogger("naver.desktop").warning(f"프로그램 창 모듈을 불러오지 못했습니다: {e}")
            return False
        try:
            self.window = webview.create_window(APP_TITLE, self.url, width=1420, height=960, min_size=(1000, 700),
                                                text_select=True)
            self.window.events.closing += self._on_closing
            storage_dir.mkdir(parents=True, exist_ok=True)
            # private_mode=False: 입력값(예약 시각 등 localStorage)을 창을 다시 열어도 유지
            webview.start(gui="edgechromium", private_mode=False, storage_path=str(storage_dir))
            return True
        except Exception as e:
            logging.getLogger("naver.desktop").warning(f"WebView2 창을 열지 못했습니다: {e}")
            return False
