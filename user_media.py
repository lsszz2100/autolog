"""사용자 자료(직접 올린 사진·글) 모듈 (user_media.py)

- 사진 저장: 화면에서 줄인 사진(base64) → data/user_uploads/<id>.jpg
  (다시 저장해 촬영 위치(GPS) 같은 숨은 정보는 지움)
- 사진 설명: Gemini가 사진을 보고 내용을 한두 문장으로 정리 → <id>.json
  원고 작성(사진 내용 반영)과 사진 자리 정하기에 씀
- 사진 자리: 본문의 "[사진:N]" 줄(N = 올린 순서). 표시가 없는 사진은 사진 설명과 문단 내용을 맞춰 자동 배치
    · 맨 위(본문 시작 전) = 대표 사진
    · 소제목 바로 아래 = 그 소제목 내용과 맞는 사진 (한 자리에 2장이면 콜라주)
"""
import base64
import io
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import gemini_client
from config import DATA_DIR
from text_style import is_subheading, is_hashtag_line, sticker_mood

logger = logging.getLogger("naver.user_media")

UPLOAD_DIR = DATA_DIR / "user_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_PHOTOS = 10
MAX_SIDE = 1600
PER_SPOT = 2                      # 한 자리에 최대 2장 (2장이면 콜라주)
VISION_MODELS = ["gemini-3.5-flash-lite", "gemini-3-flash-preview", "gemini-3.5-flash"]
PLACE_MODELS = ["gemini-3.5-flash-lite", "gemini-3-flash-preview", "gemini-3.5-flash"]

PHOTO_RE = re.compile(r"^\[사진\s*:?\s*(\d{1,2})\]$")
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


# ── 저장 ──
def save_photo(data_b64: str, name: str = "") -> Dict:
    """base64(또는 data URL) 사진을 검증해 JPEG로 저장. 사진이 아니면 ValueError."""
    from PIL import Image, ImageOps
    if "," in data_b64[:100]:
        data_b64 = data_b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(data_b64, validate=False)
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise ValueError("사진 파일을 읽을 수 없습니다. JPG·PNG·WEBP 사진을 올려 주세요.")
    img = ImageOps.exif_transpose(img)            # 세로 사진 방향 유지
    if img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    if min(img.size) < 200:
        raise ValueError("사진이 너무 작습니다 (가로·세로 200픽셀 이상).")
    pid = uuid.uuid4().hex
    img.save(UPLOAD_DIR / f"{pid}.jpg", "JPEG", quality=90)   # exif 미포함 → 위치 정보 제거
    meta = {"id": pid, "name": (name or "")[:80], "w": img.size[0], "h": img.size[1], "desc": "", "at": time.time()}
    _write_meta(pid, meta)
    return meta


def photo_path(pid: str) -> Optional[str]:
    if not isinstance(pid, str) or not _ID_RE.match(pid):
        return None
    p = UPLOAD_DIR / f"{pid}.jpg"
    return str(p) if p.exists() else None


def _meta_file(pid: str) -> Path:
    return UPLOAD_DIR / f"{pid}.json"


def _write_meta(pid: str, meta: Dict):
    _meta_file(pid).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def load_meta(pid: str) -> Dict:
    try:
        return json.loads(_meta_file(pid).read_text(encoding="utf-8"))
    except Exception:
        return {"id": pid, "desc": ""}


def delete_photo(pid: str):
    if photo_path(pid):
        for f in (UPLOAD_DIR / f"{pid}.jpg", _meta_file(pid)):
            try:
                f.unlink()
            except OSError:
                pass


def cleanup_old(days: int = 14, keep: set = frozenset()):
    """오래된 업로드 사진 정리 (발행이 끝난 사진이 쌓이지 않게). keep: 예약 대기 중인 글의 사진 id."""
    cutoff = time.time() - days * 86400
    for f in UPLOAD_DIR.glob("*"):
        try:
            if f.stem not in keep and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# ── 동영상 (체험단·기자단 글) ──
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".avi", ".wmv", ".mkv", ".webm", ".mpg", ".mpeg", ".3gp")
MAX_VIDEO_BYTES = 4 * 1024 ** 3          # 4GB (N사 한도는 8GB)


def save_video_stream(rfile, length: int, name: str = "") -> Dict:
    """요청 본문(동영상 파일 그대로)을 조금씩 읽어 저장. 동영상이 아니면 ValueError."""
    import os
    ext = os.path.splitext(name or "")[1].lower()
    if ext not in VIDEO_EXTS:
        raise ValueError("동영상 파일(MP4·MOV·AVI·WMV·MKV 등)을 올려 주세요.")
    if length <= 0 or length > MAX_VIDEO_BYTES:
        raise ValueError("동영상은 4GB 이하만 올릴 수 있습니다.")
    vid = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"{vid}{ext}"
    left = length
    try:
        with open(dest, "wb") as out:
            while left > 0:
                chunk = rfile.read(min(1 << 20, left))
                if not chunk:
                    raise ValueError("동영상을 끝까지 받지 못했습니다. 다시 올려 주세요.")
                out.write(chunk)
                left -= len(chunk)
    except Exception:
        try:
            dest.unlink()
        except OSError:
            pass
        raise
    meta = {"id": vid, "kind": "video", "name": (name or "")[:120], "ext": ext, "size": length, "at": time.time()}
    _write_meta(vid, meta)
    return meta


def save_video_file(src: str, name: str = "") -> Dict:
    """이미 만들어진 동영상 파일(클립 만들기 결과)을 올린 영상처럼 등록 (옮겨 저장)."""
    import os
    import shutil
    ext = os.path.splitext(src)[1].lower()
    if ext not in VIDEO_EXTS:
        raise ValueError("동영상 파일이 아닙니다.")
    vid = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"{vid}{ext}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dest)
    meta = {"id": vid, "kind": "video", "name": (name or dest.name)[:120], "ext": ext, "size": dest.stat().st_size,
            "at": time.time(), "made": "clip"}
    _write_meta(vid, meta)
    return meta


def video_path(vid: str) -> Optional[str]:
    if not isinstance(vid, str) or not _ID_RE.match(vid):
        return None
    meta = load_meta(vid)
    ext = meta.get("ext") if meta.get("kind") == "video" else None
    if ext not in VIDEO_EXTS:
        return None
    p = UPLOAD_DIR / f"{vid}{ext}"
    return str(p) if p.exists() else None


def delete_video(vid: str):
    p = video_path(vid)
    if p:
        for f in (Path(p), _meta_file(vid)):
            try:
                f.unlink()
            except OSError:
                pass


# ── 사진 설명 ──
def describe_photo(pid: str, api_key: str) -> str:
    """Gemini가 사진을 보고 내용을 정리 (한 번 만든 설명은 저장해 재사용). 키가 없거나 실패하면 빈 문자열."""
    meta = load_meta(pid)
    if meta.get("desc"):
        return meta["desc"]
    path = photo_path(pid)
    if not path or not (api_key or "").strip():
        return ""
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    prompt = ("이 사진은 블로그 글에 넣을 사진입니다. 사진에 보이는 것을 한국어 1~2문장(80자 이내)으로 설명하세요. "
              "무엇이 찍혔는지, 장소·음식·제품·상황의 특징, 사진 속에 읽히는 글자(상호명·메뉴·제품명 등)가 있으면 그대로 포함하세요. "
              "보이지 않는 것은 추측하지 말고, 사람이 있으면 생김새는 묘사하지 마세요. 설명 문장만 답하세요.")
    try:
        desc, _ = gemini_client.generate(api_key, prompt, VISION_MODELS, images=[("image/jpeg", data)],
                                         generation_config={"temperature": 0.2, "maxOutputTokens": 300},
                                         timeout=30, accept=lambda t: t.strip().strip('"')[:200] or None,
                                         verbose=False)
    except gemini_client.GeminiError as e:
        logger.warning(f"사진 내용 확인 실패 ({meta.get('name') or pid[:8]}): {e}")
        return ""
    meta["desc"] = desc
    _write_meta(pid, meta)
    return desc


# ── 본문의 사진 표시 줄 ──
def photo_marker(line: str) -> Optional[int]:
    """'[사진:3]' 줄이면 3, 아니면 None."""
    m = PHOTO_RE.match((line or "").strip())
    return int(m.group(1)) if m else None


def _spots(lines: List[str]) -> List[Dict]:
    """사진을 넣을 수 있는 자리 목록: 맨 위(대표) + 도입 첫 문단 뒤 + 각 소제목 바로 아래.
    소제목이 없으면 문단 몇 개마다 한 자리."""
    heads = [i for i, l in enumerate(lines) if is_subheading(l)]
    body = lambda a, b: " ".join(l.strip() for l in lines[a:b]
                                  if l.strip() and photo_marker(l) is None and not sticker_mood(l))
    first_text = next((i for i, l in enumerate(lines)
                       if l.strip() and photo_marker(l) is None and not sticker_mood(l)), 0)
    spots = [{"key": "top", "at": first_text, "ctx": body(0, len(lines))[:200]}]
    if heads:
        intro_end = heads[0]
        paras = [i for i in range(first_text, intro_end) if lines[i].strip() and photo_marker(lines[i]) is None
                 and not sticker_mood(lines[i])]
        if len(paras) >= 2:
            spots.append({"key": "0", "at": paras[0] + 1, "ctx": body(first_text, intro_end)[:600]})
        for k, h in enumerate(heads):
            end = heads[k + 1] if k + 1 < len(heads) else len(lines)
            spots.append({"key": str(k + 1), "at": h + 1, "ctx": body(h, end)[:600]})
    else:
        paras = [i for i, l in enumerate(lines) if l.strip() and photo_marker(l) is None
                 and not sticker_mood(l) and not is_hashtag_line(l)]
        step = max(2, len(paras) // 5)
        for n, j in enumerate(range(step - 1, len(paras) - 1, step)):
            i = paras[j]
            spots.append({"key": str(n), "at": i + 1, "ctx": body(paras[max(0, j - step + 1)], i + 1)[:300]})
    return spots


def _used_at(lines: List[str]) -> Dict[int, int]:
    """이미 본문에 있는 사진 표시 → {사진 번호: 줄 위치}"""
    return {n: i for i, l in enumerate(lines) if (n := photo_marker(l)) is not None}


def _bigrams(t: str) -> set:
    t = re.sub(r"[^가-힣a-zA-Z0-9]", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _score(desc: str, ctx: str) -> float:
    a, b = _bigrams(desc), _bigrams(ctx)
    return len(a & b) / (len(a) or 1)


def _assign_by_gemini(spots: List[Dict], todo: List[int], descs: Dict[int, str], api_key: str,
                      top_free: bool) -> Optional[Dict[int, str]]:
    spot_txt = "\n".join(f"- 자리 {s['key']}: {s['ctx'][:600]}" for s in spots if s["key"] != "top")
    photo_txt = "\n".join(f"- 사진 {n}: {descs.get(n) or '(설명 없음)'}" for n in todo)
    prompt = f"""블로그 글에 사진을 배치하려 합니다. 각 사진을 내용이 가장 잘 맞는 자리에 배정하세요.
[글의 자리]
{spot_txt}
[사진]
{photo_txt}

규칙:
- 사진마다 자리 하나. 한 자리에 최대 {PER_SPOT}장.
- {"글 전체를 가장 잘 보여 주는 사진 1장은 'top'(대표 사진)으로 정하세요." if top_free else "'top'은 쓰지 마세요."}
- 내용이 비슷한 자리가 여러 곳이면 사진이 글 전체에 고르게 퍼지게 하세요.
JSON으로만 답하세요: {{"사진 번호": "자리"}} 예) {{"1": "top", "2": "3"}}"""
    keys = {s["key"] for s in spots}

    def _accept(text: str):
        data = gemini_client.extract_json(text)
        if not isinstance(data, dict):
            return None
        out = {}
        for k, v in data.items():
            try:
                n = int(k)
            except ValueError:
                continue
            if n in todo and str(v) in keys:
                out[n] = str(v)
        return out or None

    try:
        res, _ = gemini_client.generate(api_key, prompt, PLACE_MODELS,
                                        generation_config={"temperature": 0.1, "maxOutputTokens": 400},
                                        timeout=20, accept=_accept, verbose=False)
        return res
    except gemini_client.GeminiError as e:
        logger.warning(f"사진 자리 자동 판단 실패 → 내용 비교 방식으로 배치: {e}")
        return None


def auto_place_photos(lines: List[str], count: int, descs: Optional[Dict[int, str]] = None,
                      api_key: str = "") -> List[str]:
    """본문에 표시가 없는 사진(1~count번)에 '[사진:N]' 줄을 알맞은 자리에 넣는다.
    이미 있는 표시는 그대로 두고, 없는 번호의 표시 줄은 지운다."""
    descs = descs or {}
    seen = set()
    kept = []
    for l in lines:
        n = photo_marker(l)
        if n is not None:
            if not 1 <= n <= count or n in seen:   # 없는 번호·같은 번호 두 번째 표시는 지움
                continue
            seen.add(n)
            l = f"[사진:{n}]"
        kept.append(l)
    lines = kept
    used = _used_at(lines)
    todo = [n for n in range(1, count + 1) if n not in used]
    if not todo:
        return lines
    spots = _spots(lines)
    if not spots:
        return lines
    at_of = {s["key"]: s["at"] for s in spots}
    # 자리별 이미 있는 사진 수 (표시 줄이 그 자리 바로 뒤에 붙어 있으면 같은 자리)
    load = {s["key"]: 0 for s in spots}
    for i in used.values():
        if i < at_of["top"]:
            load["top"] += 1
            continue
        for s in spots:
            if s["key"] != "top" and s["at"] <= i and all(
                    not l.strip() or photo_marker(l) is not None for l in lines[s["at"]:i]):
                load[s["key"]] += 1
                break
    top_free = load["top"] == 0

    assign = _assign_by_gemini(spots, todo, descs, api_key, top_free) if api_key else None
    final: Dict[int, str] = {}
    for n, key in (assign or {}).items():
        cap = 1 if key == "top" else PER_SPOT
        if load[key] < cap:
            final[n] = key
            load[key] += 1
    # 남은 사진: 내용이 가장 비슷한 빈자리 → 없으면 사진이 적은 자리 (대표 자리는 비어 있으면 첫 사진)
    rest = [n for n in todo if n not in final]
    if rest and load["top"] == 0:
        final[rest.pop(0)] = "top"
        load["top"] = 1
    body_spots = [s for s in spots if s["key"] != "top"] or spots
    m = len(rest)
    for j, n in enumerate(rest):
        cands = [s for s in body_spots if load[s["key"]] < PER_SPOT] or body_spots
        # 설명과 문단 내용이 확실히 겹치면 그 자리, 아니면 글 전체에 고르게 (빈자리 우선)
        best = max(cands, key=lambda s: _score(descs.get(n, ""), s["ctx"]))
        if _score(descs.get(n, ""), best["ctx"]) < 0.2:
            target = int((j + 0.5) * len(body_spots) / m)
            empty = [s for s in cands if load[s["key"]] == 0] or cands
            best = min(empty, key=lambda s: abs(body_spots.index(s) - target))
        final[n] = best["key"]
        load[best["key"]] += 1

    # 뒤쪽 자리부터 넣어 앞 위치가 밀리지 않게
    by_at: Dict[int, List[int]] = {}
    for n, key in final.items():
        by_at.setdefault(at_of[key], []).append(n)
    for at in sorted(by_at, reverse=True):
        lines[at:at] = [f"[사진:{n}]" for n in sorted(by_at[at])]
    return lines


def photo_brief(descs: Sequence[str]) -> str:
    """원고 작성 프롬프트용 사진 설명 목록."""
    return "\n".join(f"- 사진 {i}: {d}" for i, d in enumerate(descs, 1) if d)
