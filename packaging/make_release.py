"""릴리스 생성·업로드 (release.bat / release_full.bat 에서 호출)

  python packaging\\make_release.py code   # 바뀐 프로그램 코드만 (빌드 없음, 1분 이내)
  python packaging\\make_release.py full   # 전체 설치 파일 + 코드 (build_installer.bat 실행 후)

산출물(packaging\\output\\release-<버전>\\): NaverBlogStudio-code-<버전>.zip, update.json
full 이면 설치 파일과 .sha256(1.2.x 업데이트 호환)도 함께 업로드.
"""
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from version import APP_VERSION, UPDATE_REPO, REQUIRED_RUNTIME, DISPLAY_VERSION  # noqa: E402

# 업데이트로 교체되는 프로그램 코드 (AutoLog.spec 의 local_modules 와 같아야 함)
CODE_MODULES = [
    "app", "ai", "auth", "browser", "config", "desktop", "gemini_client", "updater", "version", "image_service",
    "keyword_service", "publisher", "reference_service", "rich_publisher", "stealth", "text_style", "thumbnail", "user_media", "sigcheck", "licensing", "schedule_queue", "topic_service", "neighbor_service", "shopping_service", "shopping_connect", "travel_connect", "review_post", "clip_maker", "clip_upload",
]
LAST_FULL = HERE / "last_full_release.txt"
# 마지막 전체 설치 파일 정보. 코드만 배포할 때도 이 설치 파일을 함께 올려야
# 아직 기반(runtime)이 낮은 고객이 '전체 설치 파일'로 따라올 수 있다 (업데이트는 최신 릴리스 파일만 봄)
LAST_SETUP = HERE / "last_full_setup.json"


def carried_setup():
    """현재 기반 번호와 같은 마지막 전체 설치 파일 (manifest 항목, 로컬 경로). 없으면 (None, None)."""
    try:
        info = json.loads(LAST_SETUP.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    setup = HERE / "output" / info.get("name", "")
    if int(info.get("runtime", 0)) != REQUIRED_RUNTIME or not setup.is_file():
        return None, None
    if setup.stat().st_size != info.get("size") or sha256(setup) != info.get("sha256"):
        sys.exit(f"[오류] {setup.name} 이(가) 마지막 전체 배포 때와 다릅니다. 같은 파일을 두거나 release_full.bat 으로 배포하세요.")
    return {k: info[k] for k in ("name", "sha256", "size")}, setup


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def gh_exe() -> str:
    found = shutil.which("gh")
    if found:
        return found
    pat = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages", "GitHub.cli_*", "bin", "gh.exe")
    hits = glob.glob(pat)
    if hits:
        return hits[0]
    hits = glob.glob("/mnt/c/Users/*/AppData/Local/Microsoft/WinGet/Packages/GitHub.cli_*/bin/gh.exe")
    if hits:
        return hits[0]
    sys.exit("[오류] GitHub CLI(gh)를 찾을 수 없습니다. winget install GitHub.cli 후 gh auth login")


def main(kind: str):
    if kind == "attach-setup":
        return attach_setup()
    if kind == "open":       # 이미 배포한 최신 버전을 이 PC가 받도록 잠깐 공개
        return open_for_my_update(gh_exe())
    if kind == "close":      # 비상용: 바로 비공개로
        return set_visibility(gh_exe(), "private")
    if kind not in ("code", "full"):
        sys.exit("사용법: make_release.py code|full|attach-setup|open|close")
    if not UPDATE_REPO:
        sys.exit("[오류] version.py 의 UPDATE_REPO 가 비어 있습니다.")
    gh = gh_exe()
    # 같은 버전 재배포 방지 (버전을 올리지 않고 실행한 경우)
    if subprocess.run([gh, "release", "view", f"v{APP_VERSION}", "--repo", UPDATE_REPO],
                      capture_output=True, stdin=subprocess.DEVNULL).returncode == 0:
        sys.exit(f"[오류] v{APP_VERSION} 은(는) 이미 배포되어 있습니다. version.py 의 APP_VERSION 을 올려 주세요.")

    key_file = HERE / "signing_key.hex"
    if not key_file.exists():
        sys.exit("[오류] 서명 키(packaging\\signing_key.hex)가 없습니다. 이 키가 없으면 설치된 프로그램이 업데이트를 받지 않습니다.")
    sys.path.insert(0, str(ROOT))
    import sigcheck
    seed = bytes.fromhex(key_file.read_text(encoding="ascii").strip())
    if sigcheck.public_key(seed).hex() != sigcheck.PUBLIC_KEY_HEX:
        sys.exit("[오류] 서명 키가 프로그램에 들어 있는 공개 키와 맞지 않습니다.")

    print("[컴파일] 프로그램 코드를 기계어(.pyd)로 변환...")
    if subprocess.run([sys.executable, str(HERE / "compile_code.py")], stdin=subprocess.DEVNULL).returncode != 0:
        sys.exit("[중단] 코드 컴파일 실패")
    compiled = HERE / "compiled"
    print("[점검] 배포 전 자동 점검 (컴파일된 코드)...")
    if subprocess.run([sys.executable, str(HERE / "smoke_test.py"), "--code-dir", str(compiled), *CODE_MODULES], stdin=subprocess.DEVNULL).returncode != 0:
        sys.exit("[중단] 점검에 실패해 배포하지 않았습니다. 오류를 고친 뒤 다시 실행하세요.")

    out = HERE / "output" / f"release-{APP_VERSION}"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    code_name = f"NaverBlogStudio-code-{APP_VERSION}.zip"
    code_zip = out / code_name
    # 원본 .py 는 넣지 않고 컴파일된 .pyd + version.py 만 (리버싱 방지)
    from compile_code import shipped_files
    with zipfile.ZipFile(code_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for f in shipped_files(compiled):
            z.write(f, f.name)
    files = [code_zip]

    notes_file = HERE / "release_notes.txt"
    notes = notes_file.read_text(encoding="utf-8").strip() if notes_file.exists() else f"AutoLog {APP_VERSION}"
    manifest = {
        "version": APP_VERSION,
        "display_version": DISPLAY_VERSION,
        "runtime": REQUIRED_RUNTIME,
        "notes": notes,
        "code": {"name": code_name, "sha256": sha256(code_zip), "size": code_zip.stat().st_size},
        "setup": None,
    }
    if kind == "full":
        setup = HERE / "output" / f"NaverBlogStudio-Setup-{APP_VERSION}.exe"
        if not setup.exists():
            sys.exit(f"[오류] 설치 파일이 없습니다: {setup} (build_installer.bat 먼저 실행)")
        digest = sha256(setup)
        manifest["setup"] = {"name": setup.name, "sha256": digest, "size": setup.stat().st_size}
        sha_file = out / (setup.name + ".sha256")
        sha_file.write_text(f"{digest}  {setup.name}\n", encoding="ascii")
        files += [setup, sha_file]
        LAST_SETUP.write_text(json.dumps({**manifest["setup"], "runtime": REQUIRED_RUNTIME}, indent=2), encoding="utf-8")
    else:
        carry, setup = carried_setup()
        if carry:
            manifest["setup"] = carry
            files.append(setup)
            print(f"[포함] 기반이 낮은 고객용 전체 설치 파일 {setup.name} 도 함께 올립니다.")
        elif REQUIRED_RUNTIME > 1:
            print("[주의] 현재 기반용 전체 설치 파일 정보가 없어, 기반이 낮은 고객은 이 버전을 받지 못합니다.")

    # 개발자 서명 (설치된 프로그램이 이 서명을 확인한 뒤에만 업데이트)
    manifest = sigcheck.sign_manifest(manifest, seed)
    (out / "update.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    files.append(out / "update.json")

    body = notes
    if kind == "code":
        last = LAST_FULL.read_text(encoding="utf-8").strip() if LAST_FULL.exists() else ""
        body += ("\n\n※ 이 버전은 바뀐 코드만 담은 업데이트입니다. 설치된 프로그램에서 [업데이트]를 누르면 몇 초 만에 적용됩니다."
                 + (f"\n※ 처음 설치하실 분은 설치 파일을 여기서 받으세요: {last}" if last else ""))
    notes_tmp = out / "notes.md"
    notes_tmp.write_text(body, encoding="utf-8")

    print(f"[배포] v{APP_VERSION} ({'바뀐 코드만' if kind == 'code' else '전체 설치 파일'}) 업로드: "
          + ", ".join(f"{f.name} ({round(f.stat().st_size / 1048576, 2)}MB)" for f in files))
    r = subprocess.run([gh, "release", "create", f"v{APP_VERSION}", *map(str, files), "--repo", UPDATE_REPO,
                        "--title", f"AutoLog {APP_VERSION}", "--notes-file", str(notes_tmp), "--latest"],
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        sys.exit("[실패] GitHub 업로드 실패")
    url = f"https://github.com/{UPDATE_REPO}/releases/tag/v{APP_VERSION}"
    if kind == "full":
        LAST_FULL.write_text(url, encoding="utf-8")
    print(f"배포 완료: {url}")
    open_for_my_update(gh)


def attach_setup():
    """이미 코드만 배포한 현재 버전에 마지막 전체 설치 파일을 덧붙인다 (update.json 다시 서명)."""
    gh = gh_exe()
    carry, setup = carried_setup()
    if not carry:
        sys.exit("[오류] 덧붙일 전체 설치 파일 정보가 없습니다 (last_full_setup.json, packaging\\output).")
    out = HERE / "output" / f"release-{APP_VERSION}"
    manifest_file = out / "update.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("version") != APP_VERSION:
        sys.exit("[오류] update.json 버전이 version.py 와 다릅니다.")
    import sigcheck
    seed = bytes.fromhex((HERE / "signing_key.hex").read_text(encoding="ascii").strip())
    manifest.pop("sig", None)
    manifest["setup"] = carry
    manifest = sigcheck.sign_manifest(manifest, seed)
    if not sigcheck.verify_manifest(manifest):
        sys.exit("[오류] 서명 확인 실패")
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # 설치 파일을 먼저 올리고 나서 update.json 교체 (중간에 확인한 프로그램이 없는 파일을 가리키지 않게)
    for f in (setup, manifest_file):
        print(f"[업로드] {f.name} ({round(f.stat().st_size / 1048576, 2)}MB)", flush=True)
        if subprocess.run([gh, "release", "upload", f"v{APP_VERSION}", str(f), "--clobber", "--repo", UPDATE_REPO]).returncode != 0:
            sys.exit("[실패] GitHub 업로드 실패")
    print(f"완료: v{APP_VERSION} 에 {setup.name} 를 덧붙였습니다.")
    open_for_my_update(gh)


def set_visibility(gh: str, vis: str) -> bool:
    r = subprocess.run([gh, "repo", "edit", UPDATE_REPO, "--visibility", vis, "--accept-visibility-change-consequences"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        print(f"[오류] 배포 저장소를 {vis} 로 바꾸지 못했습니다: {(r.stderr or r.stdout).strip()[-300:]}")
        return False
    print(f"[저장소] {UPDATE_REPO} → {'공개' if vis == 'public' else '비공개'}", flush=True)
    return True


def _my_app_version():
    """이 PC에 켜져 있는 오토로그의 현재 버전 (꺼져 있으면 None)."""
    import urllib.request
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "NaverBlogStudio" / "instance.json"
    for inst in (local, ROOT / "instance.json"):
        try:
            info = json.loads(inst.read_text(encoding="utf-8"))
            origin = f"http://127.0.0.1:{int(info['port'])}"
            req = urllib.request.Request(f"{origin}/api/update/status", headers={"Origin": origin,
                                         "X-NBS-Token": info.get("token", "")})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode("utf-8")).get("current")
        except Exception:
            continue
    return None


# 2026-11-21: 크몽 판매 시작 - 구매자 프로그램이 업데이트를 받아야 하므로 배포 저장소는 늘 공개로 둔다.
# 다시 '평소 비공개'로 돌리려면 False (비상 닫기는 make_release.py close).
KEEP_PUBLIC = True


def open_for_my_update(gh: str, wait_min: int = 30):
    """배포 저장소를 공개로 두고 이 PC가 새 버전을 받는지 확인한다. KEEP_PUBLIC 이 False면 끝나고 다시 닫는다."""
    import time
    if not set_visibility(gh, "public"):
        return
    try:
        notify_running_app()
        print(f"[대기] 이 PC의 오토로그에서 '업데이트'를 눌러 설치해 주세요. 설치가 확인되면 바로 비공개로 되돌립니다 "
              f"(최대 {wait_min}분, 그만 기다리려면 Ctrl+C).", flush=True)
        end = time.time() + wait_min * 60
        while time.time() < end:
            if _my_app_version() == APP_VERSION:
                print(f"[확인] 이 PC가 v{APP_VERSION} 로 업데이트되었습니다.")
                break
            time.sleep(10)
        else:
            print(f"[알림] {wait_min}분 안에 이 PC의 업데이트를 확인하지 못했습니다. "
                  "나중에 받으려면 packaging\\release_open.bat 을 실행하세요.")
    except KeyboardInterrupt:
        print("[중단] 기다리기를 멈춥니다.")
    finally:
        if not KEEP_PUBLIC:
            set_visibility(gh, "private")


def notify_running_app():
    """GitHub에 새 버전이 보이면(보통 1분 이내) 이 PC에서 켜져 있는 프로그램에 바로 확인시켜
    10분 주기를 기다리지 않고 업데이트 알림이 뜨게 한다."""
    import time
    import urllib.request
    manifest_url = f"https://github.com/{UPDATE_REPO}/releases/latest/download/update.json"
    print("[알림] GitHub 반영 대기 중 (보통 1분 이내)...", flush=True)
    for _ in range(36):
        try:
            with urllib.request.urlopen(urllib.request.Request(manifest_url, headers={"Cache-Control": "no-cache"}),
                                        timeout=15) as r:
                if json.loads(r.read().decode("utf-8")).get("version") == APP_VERSION:
                    break
        except Exception:
            pass
        time.sleep(5)
    else:
        print("[알림] 3분 안에 GitHub 반영을 확인하지 못했습니다. 프로그램이 10분 안에 스스로 확인합니다.")
        return
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "NaverBlogStudio" / "instance.json"
    sent = False
    for inst in (local, ROOT / "instance.json"):
        try:
            info = json.loads(inst.read_text(encoding="utf-8"))
            port = int(info["port"])
            origin = f"http://127.0.0.1:{port}"
            req = urllib.request.Request(f"{origin}/api/update/check", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json", "Origin": origin,
                                                  "X-NBS-Token": info.get("token", "")})
            with urllib.request.urlopen(req, timeout=20) as r:
                st = json.loads(r.read().decode("utf-8"))
            print(f"[알림] 켜져 있는 프로그램(v{st.get('current')})에 새 버전 v{st.get('latest')} 알림 표시 요청 완료")
            sent = True
        except Exception:
            continue
    if not sent:
        print("[알림] 이 PC에 켜져 있는 프로그램이 없습니다. 다음에 켤 때 바로 업데이트 알림이 뜹니다.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "code")
