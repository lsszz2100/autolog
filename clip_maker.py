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
import urllib.request
import wave
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
              for n, v in sorted(videos.items())]
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
def _script_prompt(items: List[Dict], seconds: int, tone: str, guide: str, notes: str, brand: str,
                   post_title: str, post_body: str, n_scenes: int) -> str:
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

[구성]
- 장면 {n_scenes}개 안팎(최대 {MAX_SCENES}개). 장면 하나 = 소재 하나. 한 장면은 약 3~4초(영상 소재는 최대 6초).
- 소재는 될 수 있으면 모두 한 번씩 쓰고, 같은 소재를 두 번 쓰지 마세요. 순서는 이야기 흐름에 맞게 정해도 됩니다.
- 첫 장면은 1~2초 안에 시선을 붙잡는 한마디(궁금증·공감 질문·핵심 장점). 마지막 장면은 "자세한 후기는 블로그에서 확인해 보세요"처럼 자연스러운 마무리.

[말투 — 한국어 원어민 수준]
- 나레이션: {tone_ko}. 실제 한국인이 말하듯 짧게 끊어 말하고, 번역투("~하는 것은", "~에 대해", "~를 통해", "그것은") 금지.
- 나레이션은 장면당 1~2문장, 공백 포함 18~32자 정도(소리 내 읽어 3~4초). 숫자·가격은 소리 내 읽기 쉬운 한글로 (예: 12,000원 → 만 이천 원).
- 자막: 나레이션을 더 짧게 줄인 핵심 문장. 1~2줄, 줄당 공백 포함 13자 이내. 장면마다 가장 중요한 단어 하나를 ==단어== 로 감싸 강조. 숫자·용량·가격은 자막에서는 아라비아 숫자 그대로 (예: 1+1, 150ml, 12,000원 — 한글로 풀어 쓰는 건 나레이션만). 이모지 금지.
- 상단 제목: 2줄, 줄당 공백 포함 11자 이내. 검색 키워드(업체·지역·제품)가 들어가게, 핵심 단어 하나 ==강조==.

[사실 규칙]
- 사진·영상에 보이는 것, 메모·가이드·원고에 있는 사실만 쓰세요. 맛·가격·위치·경험을 지어내지 마세요.
- "최고", "1위", "무조건", "100%" 같은 과장·단정 표현 금지. 가이드의 필수 키워드는 자연스럽게 1번 이상 넣으세요.

아래 JSON 하나만 출력하세요 (설명 문장 금지):
{{"title_lines": ["제목 1줄", "제목 2줄"],
  "scenes": [{{"media": "사진1", "caption": "자막 ==강조== 문장", "narration": "나레이션 문장"}}]}}"""


def make_script(api_key: str, items: List[Dict], seconds: int = 30, tone: str = "review", guide: str = "",
                notes: str = "", brand: str = "", post_title: str = "", post_body: str = "") -> Dict:
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
    seconds = seconds if seconds in SECONDS_CHOICES else 30
    n_scenes = max(3, min(MAX_SCENES, len(items), round(seconds / 3.5)))
    valid = {it["key"] for it in items}

    def accept(text: str):
        d = gemini_client.extract_json(text)
        if not isinstance(d, dict) or not isinstance(d.get("scenes"), list):
            return None
        return d if any(_media_key(s.get("media")) in valid for s in d["scenes"] if isinstance(s, dict)) else None

    data, model = gemini_client.generate(
        api_key.strip(), _script_prompt(items, seconds, tone, guide, notes, brand, post_title, post_body, n_scenes),
        WRITER_MODELS, generation_config={"temperature": 0.8, "maxOutputTokens": 4000}, timeout=60,
        accept=accept, images=images[:16])
    logger.info(f"🎬 클립 대본 생성 ({model})")
    return clean_script(data, items)


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
    return {"title_lines": title, "scenes": scenes}


# ─────────────────────────────── 나레이션 (Gemini TTS) ───────────────────────────────
def _pcm_to_wav(raw: bytes, path: Path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(raw)


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)


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


def overlay_png(title_lines: List[str], caption: str, sponsor: str, out: Path, fonts: Dict[str, str]):
    """한 장면의 글자 층 (투명 PNG 1080x1920): 위·아래 어둡게, 상단 제목, 협찬 표시, 자막."""
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
        cf = ImageFont.truetype(fonts["caption"], 74)
        _draw_lines(d, _wrap(caption, cf, 960, 2), cf, 1570, 9)
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


def _scene_audio_args(nar: Optional[Path], dur: float, idx_in: int) -> Tuple[List[str], str]:
    if nar:
        return (["-i", str(nar)],
                f"[{idx_in}:a]aformat=sample_rates=44100:channel_layouts=stereo,apad,atrim=0:{dur:.3f},"
                f"afade=t=out:st={max(dur - 0.08, 0):.3f}:d=0.08[a]")
    return (["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=44100:cl=stereo"], f"[{idx_in}:a]anull[a]")


def render_photo_scene(ff: str, item: Dict, ovl: Path, nar: Optional[Path], dur: float, out: Path, zoom_in: bool):
    base = out.with_suffix(".jpg")
    _photo_base(item["path"], base)
    frames = max(1, int(round(dur * FPS)))
    step = 0.07 / frames
    z = f"min(1+{step:.6f}*on,1.07)" if zoom_in else f"max(1.07-{step:.6f}*on,1)"
    a_in, a_filter = _scene_audio_args(nar, dur, 2)
    fc = (f"[0:v]scale=in_range=pc:out_range=tv,format=yuv420p,zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
          f"setsar=1[bg];[bg][1:v]overlay=0:0:shortest=1,format=yuv420p[v];{a_filter}")
    _run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(base), "-loop", "1", "-t", f"{dur:.3f}", "-i", str(ovl),
          *a_in, "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}", *_ENC, str(out)])
    base.unlink(missing_ok=True)


def render_video_scene(ff: str, item: Dict, ovl: Path, nar: Optional[Path], dur: float, out: Path):
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
        a = (f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,apad,atrim=0:{dur:.3f}[n]")
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


def _scene_seconds(item: Dict, caption: str, nar_sec: float) -> float:
    if nar_sec:
        d = nar_sec + 0.35
    else:
        d = 1.4 + 0.16 * len(re.sub(r"\s|==", "", caption))
    d = max(2.2, min(d, 7.0))
    if item["kind"] == "영상" and not nar_sec:
        vd = (item.get("info") or {}).get("duration") or 0
        if vd:
            d = max(2.2, min(vd, 6.0))
    return round(d, 3)


# ─────────────────────────────── 전체 만들기 ───────────────────────────────
def build_clip(api_key: str, script: Dict, items: List[Dict], voice: str = "female_bright", tone: str = "review",
               sponsor: str = "", progress: Optional[Callable[[str, int], None]] = None) -> Dict:
    """대본 → mp4. 반환 {path, seconds, scenes}."""
    def say(msg: str, pct: int):
        logger.info(f"🎬 {msg}")
        if progress:
            progress(msg, pct)

    script = clean_script(script, items)
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
                    nar_files[i], nar_secs[i] = p, sec
                    done += 1
                    say(f"나레이션 만드는 중... ({done}/{len(todo)})", 8 + int(30 * done / max(len(todo), 1)))
        # 2) 장면마다 영상 조각
        parts = []
        total = 0.0
        for i, s in enumerate(scenes):
            it = by_key[s["media"]]
            dur = _scene_seconds(it, s["caption"], nar_secs[i])
            ovl = work / f"ovl_{i:02d}.png"
            overlay_png(script["title_lines"], s["caption"], sponsor, ovl, fonts)
            part = work / f"part_{i:02d}.mp4"
            say(f"장면 합치는 중... ({i + 1}/{len(scenes)})", 40 + int(50 * i / len(scenes)))
            if it["kind"] == "사진":
                render_photo_scene(ff, it, ovl, nar_files[i], dur, part, zoom_in=(i % 2 == 0))
            else:
                render_video_scene(ff, it, ovl, nar_files[i], dur, part)
            parts.append(part)
            total += dur
        # 3) 이어 붙이기
        say("마무리하는 중...", 93)
        lst = work / "list.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        out = work / "clip.mp4"
        _run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
              "-c", "copy", "-movflags", "+faststart", str(out)])
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
