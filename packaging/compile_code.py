"""프로그램 코드 기계어 컴파일 (리버싱 방지)

배포하는 프로그램 코드(*.py)를 Cython + MSVC 로 네이티브 확장(*.pyd)으로 바꿔
packaging\\compiled 에 모은다. 설치 파일·코드 업데이트에는 .py 원문 대신 이 .pyd 만 들어간다.
- version.py 만 평문 유지 (버전 번호와 배포처만 있음, 시작 프로그램·업데이터가 글자로 읽음)
- 문서 문자열(docstring) 제거, 소스 줄 정보 미포함

사용: <빌드 venv python> packaging\\compile_code.py   (build_installer.bat / make_release.py 가 호출)
필요: pip install cython setuptools, Visual Studio C++ 빌드 도구
"""
import json
import shutil
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "compiled"
TMP = HERE / ".compile-tmp"
PLAIN = {"version"}


def main(modules):
    from Cython.Build import cythonize
    from Cython.Compiler import Options
    from setuptools import Extension
    from setuptools.dist import Distribution

    Options.docstrings = False
    Options.emit_code_comments = False
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.rmtree(TMP, ignore_errors=True)
    OUT.mkdir(parents=True)
    TMP.mkdir(parents=True)
    # 소스를 임시 폴더로 복사해 컴파일 (.c 파일이 소스 폴더에 생기지 않게)
    src = TMP / "src"
    src.mkdir()
    targets = [m for m in modules if m not in PLAIN]
    for m in targets:
        shutil.copy2(ROOT / f"{m}.py", src / f"{m}.py")
    exts = [Extension(m, [str(src / f"{m}.py")]) for m in targets]
    ext_modules = cythonize(
        exts, quiet=True, nthreads=0, build_dir=str(TMP / "c"),
        compiler_directives={"language_level": 3, "annotation_typing": False, "binding": True,
                             "embedsignature": False, "always_allow_keywords": True},
    )
    dist = Distribution({"name": "nbs", "ext_modules": ext_modules})
    cmd = dist.get_command_obj("build_ext")
    cmd.build_lib = str(OUT)
    cmd.build_temp = str(TMP / "obj")
    cmd.inplace = False
    cmd.ensure_finalized()
    cmd.run()
    for m in PLAIN & set(modules):
        shutil.copy2(ROOT / f"{m}.py", OUT / f"{m}.py")
    built = sorted(p.name for p in OUT.glob("*.pyd"))
    missing = [m for m in targets if not any(n.startswith(m + ".") for n in built)]
    if missing:
        sys.exit(f"[오류] 컴파일되지 않은 모듈: {missing}")
    shutil.rmtree(TMP, ignore_errors=True)
    write_manifest(OUT)
    print(f"  ✔ 코드 기계어 컴파일 완료: {len(built)}개 .pyd (+ version.py)")
    return OUT


def write_manifest(out: Path):
    """위조 방지: 배포 파일 지문(SHA256) 목록에 개발자 서명. 시작 프로그램(launcher)이 켤 때마다 확인."""
    import hashlib
    sys.path.insert(0, str(ROOT))
    import sigcheck
    seed = bytes.fromhex((HERE / "signing_key.hex").read_text(encoding="ascii").strip())
    if sigcheck.public_key(seed).hex() != sigcheck.PUBLIC_KEY_HEX:
        sys.exit("[오류] 서명 키가 프로그램의 공개 키와 맞지 않습니다.")
    import re
    ver = re.search(r'APP_VERSION\s*=\s*"([\d.]+)"', (out / "version.py").read_text(encoding="utf-8")).group(1)
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.glob("*.pyd")) + [out / "version.py"]}
    data = json.dumps({"version": ver, "files": files}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    (out / "manifest.json").write_bytes(data)
    (out / "manifest.sig").write_text(sigcheck.sign(seed, data).hex(), encoding="ascii")
    print(f"  ✔ 위조 방지 서명 완료: 파일 {len(files)}개")


def shipped_files(out: Path = OUT):
    """설치 파일·코드 업데이트에 넣을 파일 목록 (.pyd + version.py + 서명된 파일 목록)."""
    return sorted(out.glob("*.pyd")) + [out / "version.py", out / "manifest.json", out / "manifest.sig"]


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    from make_release import CODE_MODULES
    main(CODE_MODULES)
