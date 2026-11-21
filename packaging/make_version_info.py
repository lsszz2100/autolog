"""version.py 의 APP_VERSION 으로 PyInstaller 버전 정보 파일(version_info.txt)과 appver.txt(배치 파일용) 생성."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from version import APP_VERSION, UPDATE_REPO, REQUIRED_RUNTIME  # noqa: E402

ver = APP_VERSION
(HERE / "appver.txt").write_text(ver, encoding="ascii")
(HERE / "repo.txt").write_text(UPDATE_REPO, encoding="ascii")
(HERE / "runtime.txt").write_text(str(REQUIRED_RUNTIME), encoding="ascii")
nums = (list(int(x) for x in ver.split(".")[:3]) + [0, 0, 0])[:3] + [0]
t = tuple(nums)
(HERE / "version_info.txt").write_text(f"""# UTF-8 (make_version_info.py 가 생성 — 직접 수정하지 말 것)
VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041204B0', [
      StringStruct('CompanyName', '오토로그'),
      StringStruct('FileDescription', '오토로그 - AI 블로그 자동 발행'),
      StringStruct('FileVersion', '{ver}'),
      StringStruct('InternalName', 'NaverBlogStudio'),
      StringStruct('OriginalFilename', 'NaverBlogStudio.exe'),
      StringStruct('ProductName', '오토로그 AutoLog'),
      StringStruct('ProductVersion', '{ver}')])]),
    VarFileInfo([VarStruct('Translation', [1042, 1200])])
  ]
)
""", encoding="utf-8")
print("version_info.txt ->", ver)
