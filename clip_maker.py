"""체험단·기자단 숏폼 클립 만들기 (clip_maker.py)

사용자가 올린 사진·영상을 조합해 세로 9:16(1080x1920) 숏폼 영상을 만든다.
  · 한글 자막: 장면마다 1~2줄, 핵심 단어는 노란색 강조 (==단어==), 굵은 한글 글꼴(Pretendard, OFL)
  · 한국어 나레이션: Gemini 음성 합성(TTS) — 장면마다 따로 만들어 자막과 소리가 정확히 맞는다
  · 상단 제목(2줄)·협찬 표시·사진은 천천히 확대/축소(켄 번스), 영상은 흐린 배경 위에 가운데 배치
  · 대본(자막·나레이션)은 Gemini가 사진·영상 장면을 직접 보고 원어민 말투로 쓴다 (지어낸 경험 금지)

흐름 참고: harry0703/MoneyPrinterTurbo (MIT) — 소재 조합 → 대본 → TTS → 자막 → 합성. 코드는 이 프로그램에 맞게 새로 작성.
도구: ffmpeg(imageio-ffmpeg 배포본, PyPI) · Pretendard 글꼴 — 처음 클립을 만들 때 한 번 내려받아 확인(sha256) 후 저장.
"""
import base64
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import struct
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from config import DATA_DIR, USER_DIR

logger = logging.getLogger("naver.clip")

W, H, FPS = 1080, 1920, 30
TOOLS_DIR = USER_DIR / "tools"
WORK_DIR = DATA_DIR / "clip_work"
MAX_SCENES = 20
SECONDS_CHOICES = (15, 30, 45, 60)

# 처음 한 번 내려받는 도구 (주소·sha256 고정 — 다른 파일이면 쓰지 않는다)
FFMPEG_WHEEL = ("https://files.pythonhosted.org/packages/82/0c/a1d49f3dd2688bd750b4986f8e271ad11f8e729a896070ea25f587b44da8/"
                "imageio_ffmpeg-0.8.1-py3-none-win_amd64.whl",
                "f27ca4cb85e2d4946deedbce1a81245dfd942a2bcf94b53676fb3956aa95bf1b")
FONTS = {
    "Pretendard-ExtraBold.otf": ("https://cdn.jsdelivr.net/npm/pretendard@1.3.9/dist/public/static/Pretendard-ExtraBold.otf",
                                 "c35fe941b7568d52a96010561540e47f9d3948dfde66ba25bc1908233e0a40cd"),
    "Pretendard-Black.otf": ("https://cdn.jsdelivr.net/npm/pretendard@1.3.9/dist/public/static/Pretendard-Black.otf",
                             "94628b0bcea8936b6e5c30d98d685eb9bbaffb0fe2ed255542ecc656c248e021"),
}

TTS_MODELS = ["gemini-3.8-flash-lite-tts", "gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
TTS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# 나레이션 목소리 (Gemini 기본 목소리 중 한국어가 자연스러운 것)
VOICES = {
    "female_bright": ("Zephyr", "여성 · 밝고 경쾌"),
    "female_warm": ("Sulafat", "여성 · 따뜻하고 차분"),
    "female_young": ("Leda", "여성 · 젊고 생기 있게"),
    "male_friendly": ("Achird", "남성 · 친근하게"),
    "male_calm": ("Charon", "남성 · 차분한 정보형"),
    "male_upbeat": ("Puck", "남성 · 경쾌하게"),
    "none": ("", "나레이션 없음 (자막만)"),
}
TONES = {
    "review": ("친근한 후기", "친구에게 진짜 다녀온 곳을 추천하듯 자연스럽고 친근한 해요체",
               "warm, friendly and natural, like a Korean creator casually recommending a place to a friend"),
    "info": ("차분한 정보형", "핵심만 또박또박 알려 주는 차분한 해요체",
             "calm, clear and trustworthy, like a Korean info-channel narrator"),
    "hype": ("텐션 높은 숏폼", "첫 마디부터 시선을 끄는 빠르고 생기 있는 숏폼 말투 (과장·최고 표현은 금지)",
             "energetic, upbeat and fast-paced, like a popular Korean short-form creator"),
}

# 배경음악: FreePD.com 공공 저작물(CC0 — 상업 이용·재배포 가능, 출처 표시 불필요). 사이트가 닫혀 archive.org 사본에서 받는다.
# (이름, 파일, sha256, 앞 무음 건너뛰기 초, 원곡 평균 음량 dB)
BGM_BASE = "https://archive.org/download/abook-music-freepd-cc0/"
BGMS = {
    "bright": ("밝게 (Inspiration)", "fpd-878d4dee797d92ef.mp3",
               "a254227da398809c79a0c753073a3a92c7341672abc92bb0166b6326e126f0b4", 0.85, -13.0),
    "chill": ("잔잔하게 (Be Chillin)", "fpd-6c9a5c343a351b0f.mp3",
              "1abaa9455ced505f10306b34d1fe569a43be28809ae39e62f422c69f312a74cb", 1.7, -17.5),
    "energetic": ("신나게 (Energizing)", "fpd-c37643abec093971.mp3",
                  "6b6f002c9721debbdb4a870fd7a084ec920761a064ca0333ad603e6348a538a4", 0.0, -28.1),
    "emotional": ("감성 (Pina Colada)", "fpd-b71090110a54aaff.mp3",
                  "6f69cfae518c18b54ffe4995ea9b856c1db58811fc1f9429162d9a5f153aeafa", 0.75, -26.4),
}
BGM_AUTO = {"review": "bright", "info": "chill", "hype": "energetic"}     # '자동' = 말투에 맞춰
STYLES = ("shorts", "calm")     # shorts: 움직임·빠른 전환·후킹 문구·효과음·진행 막대 / calm: 사진 고정 + 디졸브

YELLOW = (255, 225, 77)
WHITE = (255, 255, 255)
_HL_RE = re.compile(r"==(.+?)==")


class ClipError(Exception):
    pass


# ─────────────────────────────── 도구 준비 ───────────────────────────────
def _no_window() -> dict:
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}       # CREATE_NO_WINDOW
    return {}


def _download(url: str, sha256: str, dest: Path, progress: Optional[Callable[[str], None]] = None, label: str = ""):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "AutoLog-ClipMaker"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        got = last = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
            got += len(chunk)
            if progress and total and got - last > (4 << 20):
                last = got
                progress(f"{label} 내려받는 중... {got * 100 // total}%")
    if h.hexdigest() != sha256:
        tmp.unlink(missing_ok=True)
        raise ClipError(f"{label or '도구'} 파일이 올바르지 않습니다 (확인값 불일치). 잠시 뒤 다시 시도해 주세요.")
    os.replace(tmp, dest)


def ffmpeg_exe(progress: Optional[Callable[[str], None]] = None) -> str:
    """ffmpeg 경로. 개발 실행이면 설치된 imageio_ffmpeg, 아니면 tools 폴더 (없으면 처음 한 번 내려받기)."""
    exe = TOOLS_DIR / "ffmpeg.exe"
    if exe.exists() and exe.stat().st_size > 1_000_000:
        return str(exe)
    if not getattr(sys, "frozen", False):
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    if sys.platform != "win32":
        found = shutil.which("ffmpeg")
        if found:
            return found
        raise ClipError("ffmpeg 를 찾을 수 없습니다.")
    if progress:
        progress("영상 도구(ffmpeg, 약 39MB)를 처음 한 번 내려받습니다...")
    whl = TOOLS_DIR / "imageio_ffmpeg.whl"
    try:
        _download(FFMPEG_WHEEL[0], FFMPEG_WHEEL[1], whl, progress, "영상 도구")
        with zipfile.ZipFile(whl) as z:
            name = next((n for n in z.namelist() if re.search(r"binaries/ffmpeg-win.*\.exe$", n)), None)
            if not name:
                raise ClipError("영상 도구 파일 안에 ffmpeg 가 없습니다.")
            tmp = exe.with_suffix(".part")
            with z.open(name) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            os.replace(tmp, exe)
    except urllib.error.URLError as e:
        raise ClipError(f"영상 도구를 내려받지 못했습니다. 인터넷 연결을 확인해 주세요. ({e})")
    finally:
        whl.unlink(missing_ok=True)
    logger.info(f"🎬 영상 도구 준비 완료: {exe}")
    return str(exe)


def font_path(name: str = "Pretendard-ExtraBold.otf", progress: Optional[Callable[[str], None]] = None) -> str:
    """굵은 한글 글꼴. 내려받기에 실패하면 Windows 기본 '맑은 고딕 굵게'를 쓴다."""
    p = TOOLS_DIR / "fonts" / name
    if p.exists():
        return str(p)
    try:
        if progress:
            progress("자막 글꼴을 처음 한 번 내려받습니다...")
        _download(FONTS[name][0], FONTS[name][1], p, None, "자막 글꼴")
        return str(p)
    except Exception as e:
        logger.warning(f"자막 글꼴 내려받기 실패 → 맑은 고딕 사용: {e}")
    for cand in (r"C:\Windows\Fonts\malgunbd.ttf", r"C:\Windows\Fonts\malgun.ttf",
                 "/mnt/c/Windows/Fonts/malgunbd.ttf"):
        if os.path.exists(cand):
            return cand
    raise ClipError("한글 글꼴을 찾을 수 없습니다.")


def _run(args: List[str], timeout: int = 600) -> subprocess.CompletedProcess:
    r = subprocess.run(args, capture_output=True, timeout=timeout, **_no_window())
    if r.returncode != 0:
        tail = r.stderr.decode("utf-8", "ignore").strip().splitlines()[-6:]
        raise ClipError("영상 처리 실패: " + " / ".join(tail)[-400:])
    return r


def probe(ff: str, path: str) -> Dict:
    """영상 길이·크기·소리 유무 (ffmpeg -i 출력에서 읽음)."""
    r = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, timeout=60, **_no_window())
    err = r.stderr.decode("utf-8", "ignore")
    info = {"duration": 0.0, "w": 0, "h": 0, "audio": " Audio: " in err, "rotate": 0}
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    if m:
        info["w"], info["h"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"rotate\s*:\s*(-?\d+)|rotation of (-?[\d.]+)", err)
    if m:
        rot = abs(int(float(m.group(1) or m.group(2)))) % 180
        if rot == 90:
            info["w"], info["h"] = info["h"], info["w"]
    return info


# ─────────────────────────────── 소재 ───────────────────────────────
def media_items(photos: List[Tuple[str, str]], videos: Dict[int, Dict]) -> List[Dict]:
    """[{key:'사진1', kind, n, path}] — 사진 먼저, 영상 다음 (올린 순서)."""
    items = [{"key": f"사진{i}", "kind": "사진", "n": i, "path": p} for i, (_, p) in enumerate(photos, 1)]
    items += [{"key": f"영상{n}", "kind": "영상", "n": n, "path": v["path"], "title": v.get("title", "")}
              for n, v in sorted(videos.items()) if v.get("path")]
    return items


def _thumb_b64(img) -> str:
    from PIL import Image
    im = img.convert("RGB")
    im.thumbnail((512, 512), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def _open_photo(path: str):
    from PIL import Image, ImageOps
    im = Image.open(path)
    return ImageOps.exif_transpose(im).convert("RGB")


def _video_frame(ff: str, path: str, at: float) -> Optional[str]:
    from PIL import Image
    out = WORK_DIR / f"frame_{abs(hash((path, at))) % 10**9}.jpg"
    try:
        _run([ff, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", path, "-frames:v", "1",
              "-vf", "scale=512:-2", str(out)], timeout=60)
        return _thumb_b64(Image.open(out))
    except Exception:
        return None
    finally:
        out.unlink(missing_ok=True)


# ─────────────────────────────── 대본 ───────────────────────────────
def _rules_block(rules: Optional[Dict]) -> str:
    """가이드에서 뽑은 클립 조건 → 대본 프롬프트용 '반드시 지킬 것' 목록."""
    if not rules:
        return ""
    out = []
    if rules.get("min_seconds") or rules.get("max_seconds"):
        out.append(f"- 영상 길이: {rules.get('min_seconds') or '제한 없음'}초 이상 ~ {rules.get('max_seconds') or '제한 없음'}초 이하")
    if rules.get("keywords"):
        out.append("- 자막·나레이션·상단 제목 중 어딘가에 꼭 넣을 키워드(그대로): " + ", ".join(rules["keywords"]))
    if rules.get("phrases"):
        out.append("- 꼭 넣을 문구(글자 그대로, 짧으면 마지막 장면 자막에, 길면 클립 설명에): " + " / ".join(rules["phrases"]))
    if rules.get("must_show"):
        out.append("- 꼭 보여 줄 장면(소재 중 맞는 사진·영상을 골라 배치): " + ", ".join(rules["must_show"]))
    if rules.get("first_scene"):
        out.append("- 첫 장면 조건: " + rules["first_scene"])
    if rules.get("forbidden"):
        out.append("- 쓰면 안 되는 말: " + ", ".join(rules["forbidden"]))
    if rules.get("hashtags"):
        out.append("- 클립 설명 해시태그(그대로): " + " ".join(rules["hashtags"]))
    if rules.get("description_notes"):
        out.append("- 클립 설명 조건: " + rules["description_notes"])
    for o in rules.get("other") or []:
        out.append("- " + o)
    return ("\n[가이드 클립 조건 — 아래 구성·말투 규칙보다 우선, 반드시 지킬 것]\n" + "\n".join(out) + "\n") if out else ""


def _script_prompt(items: List[Dict], seconds: int, tone: str, guide: str, notes: str, brand: str,
                   post_title: str, post_body: str, n_scenes: int, rules: Optional[Dict] = None, fix: str = "",
                   style: str = "shorts") -> str:
    tone_ko = TONES.get(tone, TONES["review"])[1]
    labels = "\n".join(f"- {it['key']}: {'사진' if it['kind'] == '사진' else '영상'}"
                       + (f" (영상 길이 {it.get('duration', 0):.0f}초, 제목 '{it.get('title', '')}')" if it["kind"] == "영상" else "")
                       for it in items)
    return f"""당신은 한국 인스타그램 릴스·유튜브 쇼츠·블로그 클립을 만드는 한국인 숏폼 PD입니다.
첨부한 이미지는 순서대로 아래 소재의 실제 장면입니다(영상은 중간 장면 한 컷). 이 소재들로 약 {seconds}초짜리 세로 숏폼 클립의 대본을 써 주세요.

[소재 목록 — 첨부 이미지 순서와 같음]
{labels}

[참고 자료]
업체·제품명: {brand or '(없음)'}
블로그 글 제목: {post_title or '(없음)'}
업체·대행사 가이드:
{(guide or '(없음)')[:4000]}
내가 직접 겪은 내용 메모:
{(notes or '(없음)')[:2000]}
블로그 원고 일부:
{(post_body or '(없음)')[:3000]}
{_rules_block(rules)}{fix}
[구성]
- 전체 길이 약 {seconds}초. 장면 {n_scenes}개 안팎(최대 {MAX_SCENES}개), 한 장면은 약 {3 if style == "shorts" else 4}초. 장면 하나 = 소재 하나.
{_SHORTS_RULES if style == "shorts" else ""}
- 소재는 모두 한 번씩 쓰세요. 소재가 장면 수보다 적으면 같은 소재를 다른 장면에 한 번 더 써도 됩니다(연달아 같은 소재는 금지). 순서는 이야기 흐름에 맞게.
- 첫 장면 나레이션은 꼭 존댓말 인사로 시작: "안녕하세요! 오늘 소개해 드릴 제품은 ○○예요."처럼 (제품이 아니면 "오늘 소개해 드릴 곳은" 등 소재에 맞게). 그 장면 자막은 제품·장소 이름이 들어간 짧은 소개.
- 마지막 장면 나레이션은 꼭 "자세한 내용은 블로그와 클립 설명에 있는 링크를 꼭 참고해 주세요."와 같은 뜻으로 마무리 (말은 자연스럽게 바꿔도 됨). 그 장면 자막도 "자세한 건 ==링크==에서"처럼 링크 안내.

[말투 — 한국어 원어민 수준]
- 나레이션: {tone_ko}. 실제 한국인이 말하듯 짧게 끊어 말하고, 번역투("~하는 것은", "~에 대해", "~를 통해", "그것은") 금지.
- 나레이션은 장면당 1~2문장, 공백 포함 18~26자 정도(소리 내 읽어 약 3.5초). 나레이션을 모두 합쳐 공백 포함 {int(seconds * 4.2)}~{int(seconds * 4.8)}자를 넘지 마세요. 숫자·가격은 소리 내 읽기 쉬운 한글로 (예: 12,000원 → 만 이천 원).
- 자막: 나레이션을 더 짧게 줄인 핵심 문장. 1~2줄, 줄당 공백 포함 13자 이내. 장면마다 가장 중요한 단어 하나를 ==단어== 로 감싸 강조. 숫자·용량·가격은 자막에서는 아라비아 숫자 그대로 (예: 1+1, 150ml, 12,000원 — 한글로 풀어 쓰는 건 나레이션만). 이모지 금지.
- 상단 제목: 2줄, 줄당 공백 포함 11자 이내. 검색 키워드(업체·지역·제품)가 들어가게, 핵심 단어 하나 ==강조==.
- 클립 설명(description): 클립을 올릴 때 설명란에 붙여 넣을 글. 2~4문장 소개 + "🔗 자세한 후기: 블로그 링크" 한 줄 + 해시태그 5~10개(가이드 해시태그가 있으면 그것 먼저). 줄바꿈은 \\n.

[사실 규칙]
- 사진·영상에 보이는 것, 메모·가이드·원고에 있는 사실만 쓰세요. 맛·가격·위치·경험을 지어내지 마세요.
- "내돈내산"·"광고 아님" 같은 대가 관련 표현·해시태그는 메모·가이드에 그렇게 적혀 있을 때만 (체험단·협찬 글이 많음).
- "최고", "1위", "무조건", "100%" 같은 과장·단정 표현 금지. 가이드의 필수 키워드는 자연스럽게 1번 이상 넣으세요.

아래 JSON 하나만 출력하세요 (설명 문장 금지):
{{"hook": "후킹 문구",
  "title_lines": ["제목 1줄", "제목 2줄"],
  "scenes": [{{"media": "사진1", "caption": "자막 ==강조== 문장", "narration": "나레이션 문장"}}],
  "description": "클립 설명란 글"}}"""


_SHORTS_RULES = """[쇼츠 연출 — 조회수 잘 나오는 한국 숏폼처럼. 아래 말투 규칙과 다르면 이 규칙이 우선]
- 첫 1.5초가 승부: hook(후킹 문구)은 화면 가운데 크게 뜨는 8~14자 한 줄. 궁금증·숫자·반전·구체적 장면으로 멈추게 (예: "송도에 ==동굴== 바베큐집?", "이 가격에 ==이 양==?"). 과장·최고·1위 금지, 사실만.
- 첫 장면 나레이션은 존댓말 인사 뒤에 바로 후킹 한마디 (예: "안녕하세요! 송도에 동굴 콘셉트 바베큐집이 있다는 거 아세요?"). 소개는 질질 끌지 말 것.
- 가장 눈길 끄는 소재(대표 메뉴·완성 장면·분위기 좋은 컷)를 첫 장면에. 그다음 장면마다 정보 하나씩: 메뉴 이름·가격·양·맛 포인트·분위기·위치·꿀팁. 같은 말 반복 금지.
- 장면은 짧고 빠르게: 나레이션은 장면당 한 문장, 공백 포함 12~22자. 자막은 그 문장의 핵심 6~12자.
- 끝 직전 장면은 한 줄 요약·추천 대상 (예: "데이트 코스로 딱이에요"), 마지막은 링크 안내.
"""


def make_script(api_key: str, items: List[Dict], seconds: int = 30, tone: str = "review", guide: str = "",
                notes: str = "", brand: str = "", post_title: str = "", post_body: str = "",
                rules: Optional[Dict] = None, style: str = "shorts") -> Dict:
    import gemini_client
    from ai import WRITER_MODELS
    if not (api_key or "").strip():
        raise ClipError("Gemini API 키가 필요합니다. [AI 블로그 자동 발행] 탭에서 키를 넣어 주세요.")
    if not items:
        raise ClipError("사진이나 영상을 먼저 올려 주세요.")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    ff = ffmpeg_exe() if any(it["kind"] == "영상" for it in items) else ""
    images = []
    for it in items:
        b64 = None
        if it["kind"] == "사진":
            try:
                b64 = _thumb_b64(_open_photo(it["path"]))
            except Exception:
                b64 = None
        else:
            info = probe(ff, it["path"])
            it["duration"] = info["duration"]
            b64 = _video_frame(ff, it["path"], max(0.0, min(info["duration"] / 2, 3.0)))
        if b64:
            images.append(("image/jpeg", b64))
    seconds = max(10, min(int(seconds or 30), 90))
    seconds = target_seconds(seconds, rules)
    n_scenes = max(3, min(MAX_SCENES, round(seconds / (3.4 if style == "shorts" else 5))))
    valid = {it["key"] for it in items}

    def accept(text: str):
        d = gemini_client.extract_json(text)
        if not isinstance(d, dict) or not isinstance(d.get("scenes"), list):
            return None
        return d if any(_media_key(s.get("media")) in valid for s in d["scenes"] if isinstance(s, dict)) else None

    fix = ""
    for attempt in range(2):          # 가이드 키워드·문구가 빠지면 한 번 더 고쳐 쓰게 한다
        data, model = gemini_client.generate(
            api_key.strip(), _script_prompt(items, seconds, tone, guide, notes, brand, post_title, post_body, n_scenes,
                                            rules, fix, style),
            WRITER_MODELS, generation_config={"temperature": 0.8, "maxOutputTokens": 4000}, timeout=60,
            accept=accept, images=images[:16])
        logger.info(f"🎬 클립 대본 생성 ({model})")
        script = clean_script(data, items)
        missing = [c["item"] for c in check_script(script, rules) if c["ok"] is False and c["kind"] in ("keyword", "phrase", "forbidden")]
        if not missing:
            break
        fix = "\n[지난 대본에서 지키지 못한 가이드 조건 — 이번에는 꼭 지킬 것]\n" + "\n".join("- " + m for m in missing) + "\n"
    sc = script["scenes"]
    if sc and sc[0]["narration"] and not sc[0]["narration"].startswith("안녕하세요"):
        first = re.sub(r"^안녕[!~.,\s]*", "", sc[0]["narration"])       # 반말 인사(안녕!)는 존댓말로
        sc[0]["narration"] = ("안녕하세요! " + first)[:120]
    if len(sc) > 1 and not ("링크" in sc[-1]["narration"] and "블로그" in sc[-1]["narration"]):
        sc[-1]["narration"] = "자세한 내용은 블로그와 클립 설명에 있는 링크를 꼭 참고해 주세요."
    return script


# ─────────────────────────────── 가이드 맞춤 (체험단·기자단 클립 조건) ───────────────────────────────
_RULE_LISTS = ("keywords", "phrases", "must_show", "forbidden", "hashtags", "other")


def normalize_rules(d) -> Dict:
    """가이드 조건 JSON 검사·정리 (화면에서 돌아온 값도 여기를 거친다)."""
    d = d if isinstance(d, dict) else {}

    def num(v):
        try:
            v = int(float(v))
            return v if 3 <= v <= 600 else None
        except (TypeError, ValueError):
            return None

    def lst(v, limit=60, n=12):
        if isinstance(v, str):
            v = re.split(r"[,\n]", v)
        out = []
        for x in v if isinstance(v, list) else []:
            x = re.sub(r"\s+", " ", str(x or "")).strip()[:limit]
            if x and x not in out:
                out.append(x)
        return out[:n]

    r = {"min_seconds": num(d.get("min_seconds")), "max_seconds": num(d.get("max_seconds")),
         "sponsor_required": bool(d.get("sponsor_required")),
         "sponsor_text": re.sub(r"\s+", " ", str(d.get("sponsor_text") or "")).strip()[:20],
         "narration": d.get("narration") if d.get("narration") in ("required", "forbidden") else "any",
         "first_scene": re.sub(r"\s+", " ", str(d.get("first_scene") or "")).strip()[:120],
         "description_notes": re.sub(r"\s+", " ", str(d.get("description_notes") or "")).strip()[:300]}
    for k in _RULE_LISTS:
        r[k] = lst(d.get(k), 200 if k in ("phrases", "other") else 60)
    r["hashtags"] = ["#" + h.lstrip("#").replace(" ", "") for h in r["hashtags"] if h.lstrip("#").strip()]
    if r["min_seconds"] and r["max_seconds"] and r["min_seconds"] > r["max_seconds"]:
        r["min_seconds"], r["max_seconds"] = r["max_seconds"], r["min_seconds"]
    r["found"] = any(r[k] for k in ("min_seconds", "max_seconds", "sponsor_required", "first_scene", "description_notes",
                                    *_RULE_LISTS)) or r["narration"] != "any"
    return r


def _rules_by_regex(guide: str) -> Dict:
    """AI 없이도 잡히는 기본 조건 (초·해시태그·협찬 표기)."""
    g = guide or ""
    r: Dict = {"hashtags": re.findall(r"#[^\s#,]+", g)}
    clip_part = "\n".join(l for l in g.splitlines() if re.search(r"클립|영상|숏폼|릴스|쇼츠", l)) or ""
    m = re.search(r"(\d{1,3})\s*초\s*(?:이상|넘게|부터)", clip_part)
    if m:
        r["min_seconds"] = int(m.group(1))
    m = re.search(r"(\d{1,3})\s*초\s*(?:이하|이내|미만|까지)", clip_part)
    if m:
        r["max_seconds"] = int(m.group(1))
    m = re.search(r"(\d{1,3})\s*[~\-]\s*(\d{1,3})\s*초", clip_part)
    if m:
        r["min_seconds"], r["max_seconds"] = int(m.group(1)), int(m.group(2))
    if re.search(r"(협찬|제공|광고)\s*(표기|표시|문구|명시)", clip_part):
        r["sponsor_required"] = True
    return r


def guide_rules(api_key: str, guide: str) -> Dict:
    """업체·대행사 가이드에서 '클립 영상' 조건만 뽑는다. AI가 안 되면 정규식으로 기본 조건만."""
    guide = (guide or "").strip()
    if not guide:
        return normalize_rules({})
    base = _rules_by_regex(guide)
    if not (api_key or "").strip():
        return normalize_rules(base)
    import gemini_client
    from ai import WRITER_MODELS
    prompt = f"""아래는 블로그 체험단·기자단 업체 가이드입니다. 이 중 '클립·숏폼 영상'을 만들 때 지켜야 할 조건만 뽑아 JSON으로 주세요.
블로그 글(본문)에만 해당하는 조건(글자 수, 사진 장수 등)은 빼고, 영상·클립 조건이 따로 없으면 업체명·필수 키워드·필수 문구·금지어·해시태그처럼 영상에도 적용될 공통 조건만 넣으세요.
가이드에 없는 조건은 지어내지 말고 빈 값으로 두세요. 키워드·문구·해시태그는 가이드 글자 그대로.

[가이드]
{guide[:6000]}

JSON 하나만 출력 (설명 금지):
{{"min_seconds": 숫자 또는 null, "max_seconds": 숫자 또는 null,
  "sponsor_required": true/false, "sponsor_text": "영상에 표시할 협찬 문구(20자 이내, 가이드에 있을 때만)",
  "narration": "required" | "forbidden" | "any",
  "keywords": ["영상에 꼭 나올 키워드"], "phrases": ["꼭 넣을 문구"], "must_show": ["꼭 보여 줄 장면"],
  "first_scene": "첫 장면 조건(있으면)", "forbidden": ["금지어"], "hashtags": ["#해시태그"],
  "description_notes": "클립 설명란 조건(링크·업체 정보 등, 있으면)", "other": ["그 밖의 영상 조건"]}}"""
    try:
        data, _ = gemini_client.generate(api_key.strip(), prompt, WRITER_MODELS,
                                         generation_config={"temperature": 0.1, "maxOutputTokens": 2000}, timeout=40,
                                         accept=lambda t: gemini_client.extract_json(t) if isinstance(gemini_client.extract_json(t), dict) else None)
    except Exception as e:
        logger.warning(f"가이드 클립 조건 읽기 실패 → 기본 조건만 사용: {e}")
        return normalize_rules(base)
    data = data if isinstance(data, dict) else {}
    for k, v in base.items():            # 정규식으로 확실히 잡힌 값은 보탠다
        if k == "hashtags":
            data["hashtags"] = list(dict.fromkeys(list(data.get("hashtags") or []) + v))
        elif not data.get(k):
            data[k] = v
    return normalize_rules(data)


def target_seconds(seconds: int, rules: Optional[Dict]) -> int:
    """고른 길이를 가이드 범위 안으로 (범위 가운데보다 조금 넉넉하게)."""
    if not rules:
        return seconds
    lo, hi = rules.get("min_seconds"), rules.get("max_seconds")
    if lo and seconds < lo + 2:
        seconds = lo + 3
    if hi and seconds > hi - 2:
        seconds = max(hi - 3, lo or 5)
    return max(8, min(seconds, 90))


def estimate_seconds(script: Dict, voice: str = "female_bright") -> float:
    """대본으로 대략의 영상 길이 계산 (한국어 나레이션 약 7자/초, 1.1배속)."""
    total = 0.0
    for s in script.get("scenes") or []:
        chars = len(re.sub(r"\s", "", s.get("narration") or ""))
        if voice != "none" and chars:
            d = chars / 5.4 / TEMPO + 0.25
        else:
            d = 1.4 + 0.16 * len(re.sub(r"\s|==", "", s.get("caption") or ""))
        total += max(2.2, min(d, 9.0)) + XFADE
    return round(max(0.0, total - XFADE), 1)


def _norm(t: str) -> str:
    return re.sub(r"[\s=#]", "", t or "").lower()


def check_script(script: Dict, rules: Optional[Dict], sponsor: Optional[str] = None, voice: Optional[str] = None,
                 seconds: Optional[float] = None) -> List[Dict]:
    """가이드 조건을 지켰는지 항목별로 확인. ok: True 지킴 / False 안 지킴 / None 직접 확인 필요."""
    if not rules or not rules.get("found"):
        return []
    sc = script.get("scenes") or []
    body = " ".join([*(script.get("title_lines") or []), *(s.get("caption", "") for s in sc), *(s.get("narration", "") for s in sc)])
    desc = script.get("description") or ""
    desc_text = re.sub(r"#\S+", "", desc)                     # 해시태그로만 들어간 건 문구로 치지 않음
    nb, nall = _norm(body), _norm(" ".join([body, desc_text, sponsor or ""]))
    out: List[Dict] = []
    for k in rules.get("keywords") or []:
        out.append({"kind": "keyword", "item": f"키워드 '{k}'", "ok": _norm(k) in nb})
    for ph in rules.get("phrases") or []:
        out.append({"kind": "phrase", "item": f"문구 '{ph[:30]}'", "ok": _norm(ph) in nall})
    for w in rules.get("forbidden") or []:
        out.append({"kind": "forbidden", "item": f"금지어 '{w}' 안 쓰기", "ok": _norm(w) not in nall})
    if rules.get("hashtags"):
        miss = [h for h in rules["hashtags"] if _norm(h) not in _norm(desc)]
        out.append({"kind": "hashtag", "item": "클립 설명 해시태그", "ok": not miss,
                    "detail": ("빠짐: " + " ".join(miss)) if miss else ""})
    if rules.get("sponsor_required") and sponsor is not None:
        out.append({"kind": "sponsor", "item": "협찬 표시", "ok": bool(sponsor.strip())})
    if voice is not None and rules.get("narration") == "required":
        out.append({"kind": "narration", "item": "나레이션 넣기", "ok": voice != "none"})
    if voice is not None and rules.get("narration") == "forbidden":
        out.append({"kind": "narration", "item": "나레이션 없이 (자막만)", "ok": voice == "none"})
    lo, hi = rules.get("min_seconds"), rules.get("max_seconds")
    if (lo or hi) and seconds is not None:
        ok = (not lo or seconds >= lo) and (not hi or seconds <= hi)
        out.append({"kind": "length", "item": f"길이 {lo or ''}~{hi or ''}초", "ok": ok, "detail": f"{seconds:.1f}초"})
    for m in rules.get("must_show") or []:
        out.append({"kind": "show", "item": f"'{m}' 장면 넣기", "ok": None, "detail": "장면 사진을 직접 확인해 주세요"})
    if rules.get("first_scene"):
        out.append({"kind": "show", "item": "첫 장면: " + rules["first_scene"][:40], "ok": None, "detail": "직접 확인"})
    return out


def _media_key(v) -> str:
    m = re.match(r"\s*(사진|영상)\s*(\d{1,2})", str(v or ""))
    return f"{m.group(1)}{int(m.group(2))}" if m else ""


def _clean_text(s, limit: int) -> str:
    s = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF]", "", str(s or ""))     # 이모지 (글꼴에 없음)
    s = re.sub(r"(\*\*|__|\{\{[^:{}]{1,4}:|\}\})", "", s)
    return re.sub(r"[ \t]+", " ", s).strip()[:limit]


def clean_script(data: Dict, items: List[Dict]) -> Dict:
    """대본 검사·정리 (화면에서 고친 대본도 여기를 거친다)."""
    valid = {it["key"] for it in items}
    title = [_clean_text(t, 24) for t in (data.get("title_lines") or []) if str(t or "").strip()][:2]
    scenes = []
    for s in data.get("scenes") or []:
        if not isinstance(s, dict):
            continue
        key = _media_key(s.get("media"))
        if key not in valid:
            continue
        cap = _clean_text(s.get("caption"), 40)
        nar = _clean_text(s.get("narration"), 120).replace("==", "")
        if not cap and not nar:
            continue
        scenes.append({"media": key, "caption": cap, "narration": nar})
        if len(scenes) >= MAX_SCENES:
            break
    if not scenes:
        raise ClipError("대본에 쓸 수 있는 장면이 없습니다. 다시 만들어 주세요.")
    desc = str(data.get("description") or "").replace("\\n", "\n")
    desc = re.sub(r"[ \t]+", " ", re.sub(r"\*\*|==", "", desc)).strip()[:1500]
    hook = _clean_text(data.get("hook"), 24)
    return {"hook": hook, "title_lines": title, "scenes": scenes, "description": desc}


# ─────────────────────────────── 나레이션 (Gemini TTS) ───────────────────────────────
# wave 모듈은 설치본(기반 1.0.14)에 들어 있지 않다 → WAV 머리말을 직접 쓰고 읽는다
_RATE = 24000


def _pcm_to_wav(raw: bytes, path: Path):
    """Gemini TTS 원본(16bit·모노·24kHz PCM) → WAV 파일."""
    n = len(raw)
    head = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + n, b"WAVE", b"fmt ", 16, 1, 1,
                       _RATE, _RATE * 2, 2, 16, b"data", n)
    Path(path).write_bytes(head + raw)


def wav_seconds(path: Path) -> float:
    """PCM WAV 길이(초). 'fmt '·'data' 덩어리를 찾아 계산."""
    data = Path(path).read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("WAV 파일이 아닙니다")
    i, byte_rate, size = 12, 0, None
    while i + 8 <= len(data):
        cid, clen = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        if cid == b"fmt ":
            byte_rate = struct.unpack("<I", data[i + 16:i + 20])[0]
        elif cid == b"data":
            size = min(clen, len(data) - i - 8)
            break
        i += 8 + clen + (clen & 1)
    if not byte_rate or size is None:
        raise ValueError("WAV 머리말을 읽지 못했습니다")
    return size / float(byte_rate)


def tts(api_key: str, text: str, voice: str, tone: str, out: Path) -> float:
    """한국어 나레이션 한 문장 → WAV. 반환: 길이(초)."""
    import gemini_client
    # 말투 지시문을 붙이면 지시문까지 소리 내 읽는다(시험 확인) → 문장만 보낸다. 말투는 목소리·대본 문장으로 정한다.
    body = {"contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
    last = ""
    for model in TTS_MODELS:
        for attempt in range(2):
            req = urllib.request.Request(TTS_ENDPOINT.format(model=model), data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json", "x-goog-api-key": api_key.strip()})
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    d = json.loads(r.read().decode("utf-8"))
                part = next((p for p in d["candidates"][0]["content"]["parts"] if p.get("inlineData")), None)
                if not part:
                    last = f"{model} (소리 없음)"
                    break
                raw = base64.b64decode(part["inlineData"]["data"])
                if raw[:4] == b"RIFF":
                    out.write_bytes(raw)
                else:
                    _pcm_to_wav(raw, out)
                sec = wav_seconds(out)
                if sec > 2.5 + 0.45 * len(re.sub(r"\s", "", text)):     # 문장보다 훨씬 길면 다른 말까지 읽은 것
                    last = f"{model} (나레이션 길이 이상 {sec:.1f}초)"
                    break
                return sec
            except urllib.error.HTTPError as he:
                detail = he.read().decode("utf-8", "ignore")
                if he.code == 400 and "API_KEY_INVALID" in detail:
                    raise ClipError("유효하지 않은 Gemini API 키입니다.")
                last = f"{model} (HTTP {he.code})"
                if he.code == 429 and "PerDay" in detail:
                    break
                if he.code in (429, 500, 503) and attempt == 0:
                    time.sleep(4)
                    continue
                break
            except Exception as e:
                last = f"{model} ({type(e).__name__})"
                break
    if "429" in last:
        raise ClipError(f"나레이션 사용량 한도에 걸렸습니다. {gemini_client.DAILY_QUOTA_MSG} — '나레이션 없음'으로 만들거나 나중에 다시 해 주세요.")
    raise ClipError(f"나레이션을 만들지 못했습니다: {last}")


# ─────────────────────────────── 화면 (자막·제목) ───────────────────────────────
def _segments(text: str) -> List[Tuple[str, bool]]:
    """'가나 ==다라== 마' → [('가나 ', False), ('다라', True), (' 마', False)]"""
    out, pos = [], 0
    for m in _HL_RE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return [(t, hl) for t, hl in out if t]


def _wrap(text: str, font, max_w: int, max_lines: int) -> List[List[Tuple[str, bool]]]:
    """강조 표시를 지키며 단어 단위로 줄바꿈 (긴 단어는 글자 단위)."""
    words: List[Tuple[str, bool]] = []
    for seg, hl in _segments(text):
        for i, w in enumerate(re.split(r"(\s+)", seg)):
            if w:
                words.append((w, hl))
    lines: List[List[Tuple[str, bool]]] = [[]]

    def width(line):
        return sum(font.getlength(t) for t, _ in line)

    for w, hl in words:
        if w.isspace():
            if lines[-1]:
                lines[-1].append((" ", hl))
            continue
        if width(lines[-1] + [(w, hl)]) <= max_w or not lines[-1]:
            if font.getlength(w) > max_w:          # 한 단어가 줄보다 길면 글자 단위로 자른다
                for ch in w:
                    if width(lines[-1] + [(ch, hl)]) > max_w and lines[-1]:
                        lines.append([])
                    lines[-1].append((ch, hl))
            else:
                lines[-1].append((w, hl))
        else:
            while lines[-1] and lines[-1][-1][0] == " ":
                lines[-1].pop()
            lines.append([(w, hl)])
    lines = [ln for ln in lines if ln]
    for ln in lines:
        while ln and ln[-1][0] == " ":
            ln.pop()
    return lines[:max_lines]


def _draw_lines(draw, lines, font, cy: int, stroke: int, line_gap: float = 1.22):
    if not lines:
        return
    asc, desc = font.getmetrics()
    lh = int((asc + desc) * line_gap)
    top = cy - lh * len(lines) // 2
    for i, ln in enumerate(lines):
        lw = sum(font.getlength(t) for t, _ in ln)
        x = (W - lw) / 2
        y = top + i * lh
        for t, hl in ln:
            draw.text((x, y), t, font=font, fill=YELLOW if hl else WHITE, stroke_width=stroke, stroke_fill=(0, 0, 0))
            x += font.getlength(t)


def overlay_png(title_lines: List[str], caption: str, sponsor: str, out: Path, fonts: Dict[str, str],
                caption_out: Optional[Path] = None):
    """한 장면의 글자 층 (투명 PNG 1080x1920): 위·아래 어둡게, 상단 제목, 협찬 표시, 자막.
    caption_out 을 주면 자막은 따로 그 파일에 (쇼츠식: 자막이 튀어나오는 움직임)."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shade = Image.new("L", (1, H), 0)
    for y in range(H):
        a = 0
        if y < 600:
            a = int(190 * (1 - y / 600) ** 1.4)
        elif y > 1250:
            a = int(170 * ((y - 1250) / (H - 1250)) ** 1.3)
        shade.putpixel((0, y), a)
    black = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    black.putalpha(shade.resize((W, H)))
    img = Image.alpha_composite(img, black)
    d = ImageDraw.Draw(img)
    if title_lines:
        tf = ImageFont.truetype(fonts["title"], 86)
        lines = []
        for t in title_lines[:2]:
            lines += _wrap(t, tf, 980, 1)
        _draw_lines(d, lines, tf, 330, 6, 1.18)
    if sponsor:
        sf = ImageFont.truetype(fonts["caption"], 34)
        tw = sf.getlength(sponsor)
        d.rounded_rectangle((44, 92, 44 + tw + 40, 92 + 58), radius=29, fill=(255, 255, 255, 215))
        d.text((64, 99), sponsor, font=sf, fill=(30, 30, 30))
    if caption:
        cf = ImageFont.truetype(fonts["caption"], 78 if caption_out else 74)
        if caption_out:
            cap = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            _draw_lines(ImageDraw.Draw(cap), _wrap(caption, cf, 960, 2), cf, 1560, 10)
            cap.save(caption_out)
        else:
            _draw_lines(d, _wrap(caption, cf, 960, 2), cf, 1570, 9)
    img.save(out)


def hook_png(hook: str, out: Path, fonts: Dict[str, str]):
    """첫 장면 가운데에 크게 뜨는 후킹 문구 (반투명 검은 띠 + 큰 글자)."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hf = ImageFont.truetype(fonts["title"], 108)
    lines = _wrap(hook, hf, 980, 2)
    lh = int(108 * 1.2)
    top = 900 - lh * len(lines) // 2
    ImageDraw.Draw(img).rounded_rectangle((30, top - 40, W - 30, top + lh * len(lines) + 30), radius=36, fill=(0, 0, 0, 150))
    _draw_lines(ImageDraw.Draw(img), lines, hf, 900, 10, 1.2)
    img.save(out)


# ─────────────────────────────── 장면 합성 ───────────────────────────────
def _photo_base(path: str, out: Path, scale: float = 1.5):
    """사진 → 1.5배 크기 바탕(흐린 배경 + 가운데 사진). 세로 사진은 화면을 꽉 채운다."""
    from PIL import Image, ImageFilter, ImageEnhance
    im = _open_photo(path)
    bw, bh = int(W * scale), int(H * scale)
    r = im.width / im.height
    if im.height / im.width >= 1.5:
        s = max(bw / im.width, bh / im.height)
        im2 = im.resize((max(bw, int(im.width * s)), max(bh, int(im.height * s))), Image.LANCZOS)
        left, top = (im2.width - bw) // 2, (im2.height - bh) // 2
        im2.crop((left, top, left + bw, top + bh)).save(out, quality=92)
        return
    s = max(bw / im.width, bh / im.height)
    bg = im.resize((int(im.width * s) + 1, int(im.height * s) + 1), Image.BILINEAR)
    left, top = (bg.width - bw) // 2, (bg.height - bh) // 2
    bg = bg.crop((left, top, left + bw, top + bh)).filter(ImageFilter.GaussianBlur(45))
    bg = ImageEnhance.Brightness(bg).enhance(0.55)
    if r > BOX_RATIO:                    # 가로 사진은 가운데를 잘라 화면에 크게 (숏폼 방식)
        cw = int(im.height * BOX_RATIO)
        im = im.crop(((im.width - cw) // 2, 0, (im.width - cw) // 2 + cw, im.height))
        r = BOX_RATIO
    fw = bw
    fh = int(fw / r)
    if fh > int(bh * 0.66):
        fh = int(bh * 0.66)
        fw = int(fh * r)
    fg = im.resize((fw, fh), Image.LANCZOS)
    bg.paste(fg, ((bw - fw) // 2, (bh - fh) // 2 - int(bh * 0.02)))
    bg.save(out, quality=92)


BOX_RATIO = 1.08          # 가로 소재는 가로:세로 1.08 까지만 보이게 가운데를 자른다 (1080x1000)

_ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-color_range", "tv", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", "-video_track_timescale", "15360"]


def _scene_audio_args(nar: Optional[Path], dur: float, idx_in: int, lead: float = 0.0) -> Tuple[List[str], str]:
    delay = f"adelay={int(lead * 1000)}:all=1," if lead > 0 else ""
    if nar:
        return (["-i", str(nar)],
                f"[{idx_in}:a]aformat=sample_rates=44100:channel_layouts=stereo,atempo={TEMPO},{delay}apad,atrim=0:{dur:.3f},"
                f"afade=t=out:st={max(dur - 0.08, 0):.3f}:d=0.08[a]")
    return (["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=44100:cl=stereo"], f"[{idx_in}:a]anull[a]")


def render_photo_scene(ff: str, item: Dict, ovl: Path, nar: Optional[Path], dur: float, out: Path, lead: float = 0.0):
    """사진은 움직이지 않게 고정 (확대 애니메이션은 화면이 떨려 보여서 뺌). 장면 전환은 build_clip 에서 부드럽게."""
    base = out.with_suffix(".jpg")
    _photo_base(item["path"], base, scale=1.0)
    a_in, a_filter = _scene_audio_args(nar, dur, 2, lead)
    fc = (f"[0:v]scale={W}:{H},scale=in_range=pc:out_range=tv,format=yuv420p,fps={FPS},setsar=1[bg];"
          f"[bg][1:v]overlay=0:0,format=yuv420p[v];{a_filter}")
    _run([ff, "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-t", f"{dur:.3f}", "-i", str(base),
          "-loop", "1", "-t", f"{dur:.3f}", "-i", str(ovl),
          *a_in, "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}", *_ENC, str(out)])
    base.unlink(missing_ok=True)


MOTIONS = ("punch", "pan_l", "push", "rise", "pan_r", "pull")


def _zoompan(kind: str, n: int) -> Tuple[str, str, str]:
    """움직임 종류 → ffmpeg zoompan 식 (z, x, y). on = 프레임 번호.
    punch: 탁 당겨 들어간 뒤 천천히 더 / push: 천천히 다가감 / pull: 물러남 / pan: 좌우 / rise: 위아래."""
    p = f"(on/{max(n - 1, 1)})"
    sm = f"({p}*{p}*(3-2*{p}))"

    def eo(x: str) -> str:                       # 처음 빠르고 끝에 느려지는 곡선
        return f"(1-pow(1-min({x}\\,1)\\,3))"
    z, dx, dy = "1.16", "0", "0"
    if kind == "punch":
        z = f"1+0.16*{eo(f'on/{FPS}/0.32')}+0.06*{p}"
    elif kind == "push":
        z = f"1.02+0.16*{sm}"
    elif kind == "pull":
        z = f"1.2-0.17*{eo(f'{p}*1.2')}"
    elif kind == "pan_l":
        dx = f"(0.85-1.7*{sm})"
    elif kind == "pan_r":
        dx = f"(-0.85+1.7*{sm})"
    elif kind == "rise":
        dy = f"(0.7-1.4*{sm})"
    return z, f"(iw-iw/zoom)/2*(1+{dx})", f"(ih-ih/zoom)/2*(1+{dy})"


ZP_MULT = 2              # 사진을 2배로 키운 뒤 잘라 움직여 떨림을 없앤다 (3배는 2배 넘게 느림)


def render_photo_scene_motion(ff: str, item: Dict, ovl: Path, cap: Optional[Path], hook: Optional[Path],
                              nar: Optional[Path], dur: float, out: Path, lead: float, motion: str):
    """쇼츠식 사진 장면: 확대·이동 + 자막이 아래에서 올라오며 나타남 + (첫 장면) 가운데 후킹 문구."""
    from PIL import Image
    S = 1.25
    base = out.with_suffix(".jpg")
    _photo_base(item["path"], base, scale=S)
    with Image.open(base) as im:
        bw, bh = im.size
    n = max(1, int(round(dur * FPS)))
    z, x, y = _zoompan(motion, n)
    inputs = ["-i", str(base), "-loop", "1", "-t", f"{dur:.3f}", "-i", str(ovl)]
    fc = [f"[0:v]scale={bw * ZP_MULT}:{bh * ZP_MULT}:flags=bicubic,"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={W}x{H}:fps={FPS},setsar=1[bg]",
          "[bg][1:v]overlay=0:0[m0]"]
    last, idx = "[m0]", 2
    if cap:
        inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(cap)]
        st = lead + 0.05
        fc.append(f"[{idx}:v]format=rgba,fade=t=in:st={st:.2f}:d=0.2:alpha=1[cap]")
        fc.append(f"{last}[cap]overlay=x=0:y='46*pow(1-clip((t-{st:.2f})/0.28\\,0\\,1)\\,3)'[m1]")
        last, idx = "[m1]", idx + 1
    if hook:
        inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(hook)]
        fc.append(f"[{idx}:v]format=rgba,fade=t=in:st=0:d=0.18:alpha=1,fade=t=out:st=1.85:d=0.35:alpha=1[hk]")
        fc.append(f"{last}[hk]overlay=x=0:y='-70*pow(1-clip(t/0.3\\,0\\,1)\\,3)'[m2]")
        last, idx = "[m2]", idx + 1
    a_in, a_filter = _scene_audio_args(nar, dur, idx, lead)
    fc.append(f"{last}scale=in_range=pc:out_range=tv,format=yuv420p[v]")
    fc.append(a_filter)
    try:
        _run([ff, "-hide_banner", "-loglevel", "error", "-y", *inputs, *a_in, "-filter_complex", ";".join(fc),
              "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}", *_ENC, str(out)])
    finally:
        base.unlink(missing_ok=True)


def render_video_scene(ff: str, item: Dict, ovl: Path, nar: Optional[Path], dur: float, out: Path, lead: float = 0.0):
    info = item.get("info") or probe(ff, item["path"])
    portrait = info["w"] and info["h"] / max(info["w"], 1) >= 1.5
    start = 0.0
    if info["duration"] > dur + 1.0:
        start = min(1.0, info["duration"] - dur)          # 첫 1초(흔들림)는 건너뛰기
    box_h = int(W / BOX_RATIO)
    landscape = info["w"] and info["w"] / max(info["h"], 1) > BOX_RATIO
    fg = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}" if portrait
          else f"scale=-2:{box_h},crop={W}:{box_h}" if landscape
          else f"scale={W}:{int(H * 0.66)}:force_original_aspect_ratio=decrease")
    v = (f"[0:v]fps={FPS},split[s1][s2];"
         f"[s1]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=40,eq=brightness=-0.2[bg];"
         f"[s2]{fg}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2-38,setsar=1[m];[m][1:v]overlay=0:0,scale=out_range=tv,format=yuv420p[v]")
    inputs = [ff, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.2f}", "-stream_loop", "-1",
              "-t", f"{dur:.3f}", "-i", item["path"], "-loop", "1", "-t", f"{dur:.3f}", "-i", str(ovl)]
    if nar:
        inputs += ["-i", str(nar)]
        delay = f"adelay={int(lead * 1000)}:all=1," if lead > 0 else ""
        a = (f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,atempo={TEMPO},{delay}apad,atrim=0:{dur:.3f}[n]")
        if info["audio"]:     # 현장 소리는 작게 깔기
            a += (f";[0:a]aformat=sample_rates=44100:channel_layouts=stereo,volume=0.12,apad,atrim=0:{dur:.3f}[o];"
                  f"[n][o]amix=inputs=2:duration=first:normalize=0[a]")
        else:
            a = a.replace("[n]", "[a]")
    elif info["audio"]:
        a = f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,apad,atrim=0:{dur:.3f}[a]"
    else:
        inputs += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
        a = "[2:a]anull[a]"
    _run(inputs + ["-filter_complex", f"{v};{a}", "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}", *_ENC, str(out)])


def _scene_seconds(item: Dict, caption: str, nar_sec: float, style: str = "calm") -> float:
    if nar_sec:
        d = nar_sec + (0.15 if style == "shorts" else 0.25)
    else:
        d = (1.1 if style == "shorts" else 1.4) + 0.16 * len(re.sub(r"\s|==", "", caption))
    d = max(1.8 if style == "shorts" else 2.2, min(d, 9.0))
    if item["kind"] == "영상" and not nar_sec:
        vd = (item.get("info") or {}).get("duration") or 0
        if vd:
            d = max(2.2, min(vd, 6.0))
    return round(d, 3)


XFADE = 0.3              # 장면 사이 부드러운 전환(디졸브) 길이(초)
TEMPO = 1.1              # 나레이션 말 빠르기 (숏폼 기본 속도에 맞춰 조금 빠르게)


XFADE_SHORTS = 0.25
# 쇼츠식 전환 (장면마다 돌아가며). 화면이 번쩍이거나 어지러운 것은 뺐다
TRANSITIONS = ("slideleft", "zoomin", "slideup", "smoothleft", "circleopen", "slideright", "smoothup", "wipeleft")


def _join_crossfade(ff: str, parts: List[Path], durs: List[float], out: Path,
                    style: str = "calm") -> Tuple[float, List[float]]:
    """장면 조각을 전환 효과로 이어 붙인다. 반환: (전체 길이(초), 전환 시작 시각들)."""
    xf = XFADE_SHORTS if style == "shorts" else XFADE
    if len(parts) == 1:
        shutil.copy(parts[0], out)
        return round(durs[0], 1), []
    args = [ff, "-hide_banner", "-loglevel", "error", "-y"]
    for p in parts:
        args += ["-i", str(p)]
    fc, v, a, length, offs = [], "[0:v]", "[0:a]", durs[0], []
    for k in range(1, len(parts)):
        off = length - xf
        offs.append(off)
        tr = TRANSITIONS[(k - 1) % len(TRANSITIONS)] if style == "shorts" else "fade"
        nv, na = f"[v{k}]", f"[a{k}]"
        fc.append(f"{v}[{k}:v]xfade=transition={tr}:duration={xf}:offset={off:.3f}{nv}")
        fc.append(f"{a}[{k}:a]acrossfade=d={xf}:c1=tri:c2=tri{na}")
        v, a, length = nv, na, length + durs[k] - xf
    _run(args + ["-filter_complex", ";".join(fc), "-map", v, "-map", a, *_ENC, "-movflags", "+faststart", str(out)],
         timeout=1200)
    return round(length, 1), offs


def bgm_path(key: str, progress: Optional[Callable[[str], None]] = None) -> Optional[Path]:
    """배경음악 파일 (처음 한 번 내려받아 확인 후 저장). 못 받으면 None — 배경음악 없이 만든다."""
    if key not in BGMS:
        return None
    label, name, sha, _, _ = BGMS[key]
    dest = TOOLS_DIR / "bgm" / name
    if dest.exists() and dest.stat().st_size > 100000:
        return dest
    try:
        if progress:
            progress(f"배경음악 '{label}'을 처음 한 번 내려받습니다...")
        _download(BGM_BASE + name, sha, dest, progress, "배경음악")
        return dest
    except Exception as e:
        logger.warning(f"배경음악을 받지 못해 배경음악 없이 만듭니다: {e}")
        return None


def _finish(ff: str, src: Path, out: Path, total: float, bgm: Optional[Tuple[Path, float, float]],
            whooshes: List[float], bar: bool):
    """마무리: 배경음악(나레이션 나올 땐 자동으로 작게) + 전환 효과음 + 위쪽 진행 막대."""
    args = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    fc: List[str] = []
    mix = ["[0:a]"]
    if bgm:
        path, start, gain = bgm
        args += ["-stream_loop", "-1", "-ss", f"{start:.2f}", "-i", str(path)]
        fade = max(total - 1.5, 0)
        fc.append(f"[0:a]asplit=2[nar][sc]")
        fc.append(f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
                  f"volume={gain:.1f}dB,afade=t=in:d=0.6,afade=t=out:st={fade:.3f}:d=1.5[bg0]")
        fc.append("[bg0][sc]sidechaincompress=threshold=0.025:ratio=5:attack=20:release=400[bg]")
        mix = ["[nar]", "[bg]"]
    if whooshes:
        n = len(whooshes)
        fc.append(f"anoisesrc=d=0.4:c=pink:r=44100:a=0.6,highpass=f=500,lowpass=f=7000,"
                  f"afade=t=in:d=0.15:curve=exp,afade=t=out:st=0.15:d=0.25,volume=0.32,"
                  f"aformat=sample_rates=44100:channel_layouts=stereo,asplit={n}" + "".join(f"[w{i}]" for i in range(n)))
        for i, t in enumerate(whooshes):
            ms = max(0, int((t - 0.1) * 1000))
            fc.append(f"[w{i}]adelay={ms}:all=1[wd{i}]")
            mix.append(f"[wd{i}]")
    if len(mix) > 1:
        fc.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0,alimiter=limit=0.95[a]")
    else:
        fc.append("[0:a]anull[a]")
    if bar:
        fc.append(f"color=c=0xFFE14D:s={W}x9:r={FPS}[bar];"
                  f"[0:v][bar]overlay=x='-w+W*t/{max(total, 0.1):.3f}':y=0:shortest=1,format=yuv420p[v]")
        vmap = "[v]"
    else:
        vmap = "0:v"
    _run(args + ["-filter_complex", ";".join(fc), "-map", vmap, "-map", "[a]", "-t", f"{total:.3f}",
                 *(_ENC if bar else ["-c:v", "copy", *_ENC[_ENC.index("-c:a"):]]), "-movflags", "+faststart", str(out)],
         timeout=1200)


# ─────────────────────────────── 전체 만들기 ───────────────────────────────
def build_clip(api_key: str, script: Dict, items: List[Dict], voice: str = "female_bright", tone: str = "review",
               sponsor: str = "", progress: Optional[Callable[[str, int], None]] = None,
               min_seconds: Optional[int] = None, style: str = "shorts", bgm: str = "auto") -> Dict:
    """대본 → mp4. 반환 {path, seconds, scenes}."""
    def say(msg: str, pct: int):
        logger.info(f"🎬 {msg}")
        if progress:
            progress(msg, pct)

    script = clean_script(script, items)
    style = style if style in STYLES else "shorts"
    xf = XFADE_SHORTS if style == "shorts" else XFADE
    bgm_key = BGM_AUTO.get(tone, "bright") if bgm == "auto" else bgm
    by_key = {it["key"]: it for it in items}
    ff = ffmpeg_exe(lambda m: say(m, 3))
    fonts = {"caption": font_path("Pretendard-ExtraBold.otf", lambda m: say(m, 5)),
             "title": font_path("Pretendard-Black.otf")}
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="clip_", dir=str(WORK_DIR)))
    voice_name = VOICES.get(voice, VOICES["female_bright"])[0]
    scenes = script["scenes"]
    try:
        for s in scenes:
            it = by_key[s["media"]]
            if it["kind"] == "영상" and "info" not in it:
                it["info"] = probe(ff, it["path"])
        # 1) 나레이션 (장면마다 — 자막과 정확히 맞게)
        nar_files: List[Optional[Path]] = [None] * len(scenes)
        nar_secs = [0.0] * len(scenes)
        if voice_name:
            from concurrent.futures import ThreadPoolExecutor
            todo = [(i, s) for i, s in enumerate(scenes) if s["narration"]]
            done = 0

            def one(arg):
                i, s = arg
                p = work / f"nar_{i:02d}.wav"
                return i, p, tts(api_key, s["narration"], voice_name, tone, p)

            with ThreadPoolExecutor(max_workers=3) as ex:
                for i, p, sec in ex.map(one, todo):
                    nar_files[i], nar_secs[i] = p, sec / TEMPO
                    done += 1
                    say(f"나레이션 만드는 중... ({done}/{len(todo)})", 8 + int(30 * done / max(len(todo), 1)))
        # 2) 장면마다 영상 조각
        parts, durs = [], []
        plan = [_scene_seconds(by_key[s["media"]], s["caption"], nar_secs[i], style) + (xf if i > 0 else 0.0)
                + (xf if i < len(scenes) - 1 else 0.0) for i, s in enumerate(scenes)]
        est = sum(plan) - xf * (len(plan) - 1)
        if min_seconds and est < min_seconds + 0.5:          # 가이드 최소 길이: 마지막 장면을 조금 더 보여 준다
            plan[-1] += min_seconds + 0.5 - est
        def make_part(i: int) -> Path:
            sc = scenes[i]
            it = by_key[sc["media"]]
            lead = xf if i > 0 else 0.0                       # 전환되는 동안 나레이션이 겹치지 않게 살짝 늦게 시작
            dur = plan[i]
            ovl = work / f"ovl_{i:02d}.png"
            part = work / f"part_{i:02d}.mp4"
            if style == "shorts" and it["kind"] == "사진":
                cap = work / f"cap_{i:02d}.png" if sc["caption"] else None
                overlay_png(script["title_lines"], sc["caption"], sponsor, ovl, fonts, caption_out=cap)
                hook = None
                if i == 0 and script.get("hook"):
                    hook = work / "hook.png"
                    hook_png(script["hook"], hook, fonts)
                render_photo_scene_motion(ff, it, ovl, cap, hook, nar_files[i], dur, part, lead,
                                          MOTIONS[i % len(MOTIONS)])
            elif it["kind"] == "사진":
                overlay_png(script["title_lines"], sc["caption"], sponsor, ovl, fonts)
                render_photo_scene(ff, it, ovl, nar_files[i], dur, part, lead)
            else:
                overlay_png(script["title_lines"], sc["caption"], sponsor, ovl, fonts)
                render_video_scene(ff, it, ovl, nar_files[i], dur, part, lead)
            return part

        from concurrent.futures import ThreadPoolExecutor
        workers = max(1, min(4, (os.cpu_count() or 2) // 2))
        done_n = 0
        say(f"장면 합치는 중... (0/{len(scenes)})", 40)
        with ThreadPoolExecutor(max_workers=workers) as ex:       # 장면 여러 개를 동시에 (그림 처리·인코딩이 병렬로 돈다)
            futs = [ex.submit(make_part, i) for i in range(len(scenes))]
            for f in futs:
                parts.append(f.result())
                done_n += 1
                say(f"장면 합치는 중... ({done_n}/{len(scenes)})", 40 + int(48 * done_n / len(scenes)))
        durs = list(plan)
        # 3) 이어 붙이기
        say("장면 잇는 중...", 90)
        joined = work / "joined.mp4"
        total, offs = _join_crossfade(ff, parts, durs, joined, style)
        say("배경음악·마무리 넣는 중...", 95)
        music = None
        if bgm_key in BGMS:
            bp = bgm_path(bgm_key, lambda m: say(m, 95))
            if bp:
                _, _, _, start, mean = BGMS[bgm_key]
                target = -27.0 if voice_name else -19.0       # 나레이션이 있으면 더 작게 (말할 땐 자동으로 더 줄어듦)
                music = (bp, start, max(-24.0, min(14.0, target - mean)))
        out = work / "clip.mp4"
        if music or style == "shorts":
            _finish(ff, joined, out, total, music, offs if style == "shorts" else [], style == "shorts")
        else:
            os.replace(joined, out)
        final = WORK_DIR / f"clip_{int(time.time())}.mp4"
        os.replace(out, final)
        say("완료", 100)
        return {"path": str(final), "seconds": round(total, 1), "scenes": len(scenes)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ─────────────────────────────── 백그라운드 작업 ───────────────────────────────
_JOB_LOCK = threading.Lock()
JOB: Dict = {"state": "idle", "msg": "", "pct": 0, "result": None, "error": ""}


def job_status() -> Dict:
    return dict(JOB)


def start_job(fn: Callable[[Callable[[str, int], None]], Dict]) -> bool:
    """fn(progress) 를 백그라운드로 실행. 이미 만드는 중이면 False."""
    with _JOB_LOCK:
        if JOB["state"] == "running":
            return False
        JOB.update(state="running", msg="준비 중...", pct=1, result=None, error="")

    def progress(msg: str, pct: int):
        JOB.update(msg=msg, pct=max(1, min(int(pct), 100)))

    def run():
        try:
            res = fn(progress)
            JOB.update(state="done", result=res, msg="완료", pct=100)
        except ClipError as e:
            JOB.update(state="error", error=str(e))
        except Exception as e:
            logger.exception("클립 만들기 실패")
            JOB.update(state="error", error=f"클립을 만들지 못했습니다: {e}")

    threading.Thread(target=run, daemon=True, name="clip-maker").start()
    return True


def cleanup_old(days: int = 7):
    """작업 폴더의 오래된 클립 정리."""
    if not WORK_DIR.exists():
        return
    cut = time.time() - days * 86400
    for p in WORK_DIR.iterdir():
        try:
            if p.stat().st_mtime < cut:
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
        except OSError:
            pass
