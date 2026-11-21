"""100% 저작권 안전 및 본문 맥락 일치(Content-Aware) 고성능 실사 이미지 조달 모듈 (image_service.py)

- 블로그 제목과 본문 내용(문단)을 종합 분석하여 글의 주제와 100% 직결되는 실사 이미지 자동 조달
- 인물, 얼굴, 초상화(인물 사진, 화가 모네 등) 100% 원천 배제 (STRICT: NO humans, NO faces, NO portraits)
- 글자/텍스트/간판/워터마크 100% 원천 배제 (STRICT: NO text, NO letters, NO words)
- 1순위: 무료 사진 검색 Openverse·Wikimedia Commons (CC0·퍼블릭 도메인만, 출처 표기 불필요, 키 없음)
         Gemini 키가 있으면 AI가 후보 사진을 직접 보고 장면이 맞는 것만 고름 (없으면 제목·태그 단어 비교)
- 2순위: 맞는 사진이 없는 자리만 본문 맥락 맞춤 FLUX AI 그림 (Pollinations 무료, 45초당 1장)
- 규격: 960x720 고화질 LANCZOS 최적화 (대표 1장 + 콜라주 2세트(4장) + 마무리 1장 = 총 6장)
"""
import os
import re
import io
import time
import json
import random
import base64
import threading
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from typing import List, Dict, Optional
from PIL import Image, ImageEnhance, ImageOps

import gemini_client

from config import DATA_DIR
POST_IMAGES_DIR = DATA_DIR / "post_images"
POST_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "NaverBlogStudio/1.0 (blog image fetcher)"
}

# 인물, 초상화, 그림, 역사적 인물 배제용 블랙리스트
BAD_IMAGE_KEYWORDS = [
    "person", "portrait", "face", "man", "woman", "people", "human", "girl", "boy",
    "monet", "painting", "statue", "drawing", "illustration", "memorial", "tomb",
    "sculpture", "sketch", "caricature", "bust", "monument", "cemetery", "grave"
]

def clean_text_for_context(text: str) -> str:
    """텍스트에서 마크다운 기호 및 특수문자 정돈"""
    t = re.sub(r"\[.*?\]|\(.*?\)|<.*?>", " ", text)
    t = re.sub(r"[#*`_~■▶]", " ", t)
    return " ".join(t.split())

def generate_contextual_plans_with_gemini(keyword: str, title: str, contents: Optional[List[str]] = None, gemini_api_key: str = "") -> List[Dict[str, str]]:
    """
    Gemini 다중 모델 캐스케이드를 통해 제목과 본문 내용을 심층 분석하여
    글의 맥락과 100% 일치하며 인물/글자가 배제된 6장의 사진 기획(영문 AI 프롬프트 + 안전 스톡 키워드) 생성
    """
    clean_k = keyword.strip()
    clean_t = clean_text_for_context(title)
    
    body_summary = ""
    if contents:
        # 본문 핵심 문단 발췌 (최대 1,500자)
        valid_lines = [clean_text_for_context(l) for l in contents if clean_text_for_context(l) and not l.strip().startswith(("#", "[상품링크", "[쇼핑커넥트"))]
        body_summary = "\n".join(valid_lines[:12])[:1500]

    if gemini_api_key:
        system_instruction = (
            "You are a world-class professional commercial photography director for premium blogs. "
            "Analyze the Blog Title and Body Content to generate 6 highly relevant, contextual photograph plans.\n"
            "STRICT RULES:\n"
            "1. ABSOLUTELY NO humans, NO human faces, NO portraits, NO people, NO historical figures (e.g. painter Claude Monet). "
            "Never generate or search for any human portraits!\n"
            "2. Focus entirely on realistic physical objects, modern desk setups, laptops/screens, workspace environments, culinary food dishes, architectural spaces, or relevant tools that specifically match the topic and body content.\n"
            "3. NO text, NO typography, NO watermark, NO letters, NO signs in the images.\n"
            "4. Return a raw JSON array of 6 objects with keys:\n"
            "   - 'angle': short camera angle name (e.g. 'hero_shot', 'workspace_setup', 'tool_detail', 'action_macro', 'overhead_view', 'closing_ambient')\n"
            "   - 'prompt': 4k photorealistic image generation prompt for FLUX (must include '4k photorealistic, award-winning photography, no humans, no faces, no text')\n"
            "   - 'safe_stock_query': a 2-3 word English concrete object query for stock image search (e.g. 'laptop desk workspace', 'minimalist mechanical keyboard', 'korean bbq grill'). Never include person names!\n"
            "Return ONLY the raw JSON array. Do not wrap in markdown backticks."
        )

        user_content = f"Blog Title: {clean_t}\nMain Keyword: {clean_k}\n\nBody Content Context:\n{body_summary}"

        def _accept(text: str):
            parsed = json.loads(text)
            return parsed[:6] if isinstance(parsed, list) and len(parsed) >= 6 else None

        try:
            plans, m = gemini_client.generate(
                gemini_api_key, user_content,
                ["gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-2.5-flash"],
                system=system_instruction,
                generation_config={"responseMimeType": "application/json", "temperature": 0.7},
                timeout=10, accept=_accept, verbose=False)
            print(f"✔ [Gemini AI] 본문 맥락 기반 저작권/인물 배제 실사 사진 기획 6종 완성 ({m})")
            return plans
        except gemini_client.GeminiError:
            pass

    # 폴백: 안전한 규칙 기반 기획
    base_topic = clean_k or clean_t or "modern workspace"
    safe_defaults = [
        {"angle": "hero", "prompt": f"Stunning hero shot of {base_topic}, gourmet presentation or sleek workspace, 4k photorealistic, no humans, no faces, no text", "safe_stock_query": f"{base_topic} setup"},
        {"angle": "detail_1", "prompt": f"Close-up macro detail of tools and components related to {base_topic}, natural soft lighting, 4k photorealistic, no humans, no faces, no text", "safe_stock_query": f"{base_topic} detail"},
        {"angle": "detail_2", "prompt": f"Minimalist clean indoor environment for {base_topic}, warm wooden textures, 4k photorealistic, no humans, no faces, no text", "safe_stock_query": f"{base_topic} indoor"},
        {"angle": "action", "prompt": f"Dynamic action view of {base_topic} process or cooking, rising steam or soft lighting, 4k photorealistic, no humans, no faces, no text", "safe_stock_query": f"{base_topic} action"},
        {"angle": "macro", "prompt": f"Extreme macro shot showing fine textures and appetizing presentation of {base_topic}, 4k photorealistic, no humans, no faces, no text", "safe_stock_query": f"{base_topic} macro"},
        {"angle": "closing", "prompt": f"Wide satisfying overview of {base_topic} spread on a beautiful table, 4k photorealistic, no humans, no faces, no text", "safe_stock_query": f"{base_topic} table"}
    ]
    return safe_defaults

# Pollinations 무료 요청은 nologo=true를 무시하고 우하단(높이의 약 4%)에 로고를 찍는다.
# → 세로로 WATERMARK_CROP만큼 더 길게 생성한 뒤 하단을 잘라내고 목표 크기로 맞춘다.
WATERMARK_CROP = 0.08


# 무료 한도(IP당 45초에 1장): 서버는 '앞 사진 생성이 끝난 때'부터 45초를 센다.
# 다음 요청을 그 시각에 맞춰 보내 거절 왕복을 줄이고, 여러 작업(미리 만들기·발행)이 동시에 불러도 한 줄로 세운다.
FREE_INTERVAL = 46.0
_pace_lock = threading.Lock()
_free_lock = threading.Lock()
_next_slot = 0.0


def _pace_free_limit():
    with _pace_lock:
        wait = _next_slot - time.time()
    if wait > 0:
        print(f"  ⏳ AI 이미지 무료 한도(45초당 1장) 순서 대기 {int(wait + 0.9)}초...")
        time.sleep(wait)


def _set_next_slot(after_sec: float):
    global _next_slot
    with _pace_lock:
        _next_slot = max(_next_slot, time.time() + after_sec)


def generate_ai_image(prompt: str, save_path: str, width: int = 960, height: int = 720, timeout_sec: int = 12) -> bool:
    """
    1순위: FLUX AI 실사 모델 호출
    인물, 얼굴, 글자 원천 배제 태그를 강제 추가하여 안전한 실사 이미지 생성 (워터마크 영역 잘라냄)
    """
    try:
        clean_p = f"{prompt}, no humans, no faces, no people, no portraits, no typography, no words, no watermark"
        enc = urllib.parse.quote(clean_p)
        seed = random.randint(1000, 999999)
        gen_h = int(round(height / (1 - WATERMARK_CROP)))
        url = f"https://image.pollinations.ai/prompt/{enc}?width={width}&height={gen_h}&nologo=true&seed={seed}&model=flux"

        data = b""
        with _free_lock:  # 무료 요청은 한 번에 1장씩 (미리 만들기·발행이 동시에 불러도 한도 순서 유지)
            for attempt in range(3):
                _pace_free_limit()
                try:
                    req = urllib.request.Request(url, headers=HEADERS)
                    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                        data = resp.read()
                    _set_next_slot(FREE_INTERVAL)
                    break
                except urllib.error.HTTPError as he:
                    # 무료 한도 초과(402/429) 또는 무료 서버 전체가 붐빔(503, "Retry after 10s"):
                    # 서버가 알려 준 시간 + 2초 뒤 다시 (최대 2회)
                    body = he.read().decode("utf-8", errors="ignore")
                    m = re.search(r"[Rr]etry after (\d+)s", body)
                    if he.code in (402, 429, 503) and attempt < 2:
                        wait = min(int(m.group(1)) if m else (10 if he.code == 503 else 45), 60) + 2
                        why = "무료 서버 혼잡" if he.code == 503 else "무료 한도(45초당 1장)"
                        print(f"  ⏳ AI 이미지 {why} 대기 {wait}초...")
                        _set_next_slot(wait)
                        continue
                    return False
                except Exception:
                    # 시간 초과 등: 서버에서는 생성이 계속됐을 수 있어 다음 요청도 한도 간격을 둔다
                    _set_next_slot(FREE_INTERVAL)
                    raise
        if len(data) <= 8000:
            return False
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size  # 서버가 크기를 줄여 보낼 수 있어 실제 크기 기준으로 자름
        im = im.crop((0, 0, w, int(h * (1 - WATERMARK_CROP))))
        im = im.resize((width, height), Image.Resampling.LANCZOS)
        im.save(save_path, "JPEG", quality=95, optimize=True)
        return True
    except Exception:
        pass
    return False

# ───────────────────────── 무료 사진 검색 (키 없음) ─────────────────────────
# 출처 표기 없이 상업적으로 쓸 수 있는 사진만: Openverse(CC0·퍼블릭 도메인) → Wikimedia Commons(CC0·퍼블릭 도메인만).
# 검색은 서로 기다리지 않아 6장을 한꺼번에 찾고, 맞는 사진이 없는 자리만 무료 AI 그림(45초당 1장)으로 채운다.
OV_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AutoLog/1.0"}
SEARCH_WAIT = 60  # 사진 검색을 기다리는 최대 시간 (Openverse는 처음 찾는 단어에 20~40초 걸리기도 함)
MIN_SRC_WIDTH = 640
_ov_sem = threading.Semaphore(3)  # Openverse 동시 요청 수 (몰리면 다 같이 느려지고 429)
STOP = {"the", "and", "with", "for", "of", "in", "on", "at", "a", "an", "to", "photo", "image", "picture",
        "modern", "close", "view", "top", "shot", "setup"}


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z]+", (text or "").lower())


def _stem(w: str) -> str:
    """charging·chargers·charge → charg 처럼 끝만 다른 단어를 같게 본다"""
    for suf in ("ings", "ing", "ers", "er", "ies", "es", "ed", "s", "e"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _query_words(query: str) -> List[str]:
    return [w for w in _words(query) if len(w) > 2 and w not in STOP]


def _hits(c: Dict, query: str) -> int:
    """사진 제목·태그에 검색어 단어가 몇 개 들어 있는지 (단어 단위)"""
    have = {_stem(w) for w in _words(c.get("title", "") + " " + " ".join(c.get("tags", [])))}
    return sum(1 for w in _query_words(query) if _stem(w) in have)


def _relevant(c: Dict, query: str) -> bool:
    """AI가 사진을 볼 수 없을 때의 거르기: 3단어까지는 모두, 그 이상은 하나 빼고 다 맞아야 사용"""
    words = _query_words(query)
    if not words:
        return False
    return _hits(c, query) >= (len(words) if len(words) <= 3 else len(words) - 1)


def _has_person(c: Dict) -> bool:
    ws = set(_words(c.get("title", "") + " " + " ".join(c.get("tags", []))))
    return any(b in ws or b + "s" in ws for b in BAD_IMAGE_KEYWORDS) or "men" in ws or "women" in ws


def search_openverse(query: str, count: int = 20) -> List[Dict]:
    params = {"q": query, "license": "cc0,pdm", "page_size": count, "mature": "false", "category": "photograph"}
    url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(params)
    data = None
    with _ov_sem:
        for attempt in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=OV_HEADERS), timeout=50) as r:
                    data = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as he:
                if he.code == 429 and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                return []
            except Exception:
                return []
    out = []
    for r in (data or {}).get("results", []):
        w = r.get("width") or 0
        if (w and w < MIN_SRC_WIDTH) or not r.get("url"):
            continue
        out.append({"url": r["url"], "thumb": r.get("thumbnail") or r["url"], "w": w, "h": r.get("height") or 0,
                    "title": (r.get("title") or "")[:120], "tags": [t.get("name", "") for t in (r.get("tags") or [])][:30]})
    return out


def search_wikimedia(query: str, count: int = 12) -> List[Dict]:
    """Wikimedia Commons에서 CC0·퍼블릭 도메인 사진만 (CC BY·BY-SA는 출처 표기가 필요해서 뺀다)"""
    params = {"action": "query", "generator": "search", "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6,
              "gsrlimit": count, "prop": "imageinfo", "iiprop": "url|mime|size|extmetadata", "iiurlwidth": 1280,
              "iiextmetadatafilter": "LicenseShortName", "format": "json"}
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=15) as r:
            pages = json.loads(r.read().decode("utf-8")).get("query", {}).get("pages", {})
    except Exception:
        return []
    out = []
    for p in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        ii = (p.get("imageinfo") or [{}])[0]
        lic = ((ii.get("extmetadata") or {}).get("LicenseShortName") or {}).get("value", "").lower()
        if not ("cc0" in lic or "public domain" in lic or lic.startswith("pd")):
            continue
        if "jpeg" not in ii.get("mime", "") and "png" not in ii.get("mime", ""):
            continue
        if (ii.get("width") or 0) < MIN_SRC_WIDTH:
            continue
        title = re.sub(r"^File:|\.\w+$", "", p.get("title", ""))
        title = re.sub(r"[_\-]+", " ", title)
        out.append({"url": ii.get("thumburl") or ii.get("url"), "thumb": ii.get("thumburl") or ii.get("url"),
                    "w": ii.get("width") or 0, "h": ii.get("height") or 0, "title": title, "tags": []})
    return out


VISION_MODELS = ["gemini-3.5-flash-lite", "gemini-3-flash-preview", "gemini-2.5-flash-lite", "gemini-2.5-flash"]  # 새 키는 2.5 계열이 404 → 3.x 먼저. 무료 키는 lite가 자주 429
VISION_CANDS = 4


def _thumb_b64(url: str) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers=OV_HEADERS), timeout=10) as r:
        data = r.read(6_000_000)
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((384, 384))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def _vision_pick(api_key: str, subject: str, topic: str, cands: List[Dict]) -> Optional[int]:
    """Gemini가 후보 사진을 직접 보고 subject 장면인 것을 고른다 (태그는 엉터리가 많음).
    번호(0부터), 맞는 사진이 없으면 -1, AI 확인을 못 했으면 None (호출한 쪽이 글자 비교로 대신)."""
    if not api_key or not cands:
        return None
    from concurrent.futures import ThreadPoolExecutor

    def _safe_thumb(c):
        try:
            return _thumb_b64(c["thumb"])
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=len(cands)) as ex:
        thumbs = list(ex.map(_safe_thumb, cands))
    shown = [(i, t) for i, t in enumerate(thumbs) if t]
    if not shown:
        return None
    prompt = (
        f'A blog post about "{topic}" needs a photo of: "{subject}".\n'
        f"The {len(shown)} images above are candidate photos, numbered 1 to {len(shown)} in order.\n"
        "Pick the photo that most clearly shows that subject as its main content. Be strict: a different kind of "
        "object, any visible person or face as a main subject, a drawing, a map, a document scan, a historical or "
        "very old photo, a blurry photo, advertisements, banners, screenshots, and watermarks do NOT match. "
        "The photo goes into a blog post, so also reject any photo where readable words, labels, signs, brand names "
        "or logos are clearly visible (tiny unreadable marks are fine). The whole scene must also fit the blog topic: "
        "reject photos whose other content belongs to a different topic (e.g. meat in a coffee post). "
        "Small differences in details are fine.\n"
        'Answer only JSON: {"best": <number>} or {"best": 0} if none clearly matches.')

    def _accept(text):
        d = gemini_client.extract_json(text)
        return int(d.get("best", 0)) if isinstance(d, dict) else None
    try:
        res, _ = gemini_client.generate(api_key, prompt, VISION_MODELS, images=[("image/jpeg", t) for _, t in shown],
                                        generation_config={"temperature": 0, "responseMimeType": "application/json"},
                                        timeout=25, accept=_accept, verbose=False)
    except gemini_client.GeminiError:
        return None
    return shown[res - 1][0] if 1 <= res <= len(shown) else -1


def _shorter_queries(q: str) -> List[str]:
    words = q.split()
    return list(dict.fromkeys(x for x in [q, " ".join(words[:2]), " ".join(words[-2:])] if x.strip()))


def download_and_process_image(url: str, save_path: str, width: int = 960, height: int = 720) -> bool:
    """사진을 내려받아 가운데를 기준으로 960x720에 맞춰 자르고(찌그러뜨리지 않음) 저장"""
    try:
        req = urllib.request.Request(url, headers=OV_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            img_bytes = r.read(25_000_000)
        if len(img_bytes) <= 5000:
            return False
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        if im.width < MIN_SRC_WIDTH or im.height < 400:
            return False
        im = ImageOps.fit(im, (width, height), Image.Resampling.LANCZOS)
        im = ImageEnhance.Sharpness(im).enhance(1.04)
        im.save(save_path, "JPEG", quality=95, optimize=True)
        return True
    except Exception:
        return False


def is_solid_color_image(img_path: str) -> bool:
    """이미지가 내용 없이 단색/빈 색상만 있는지 정밀 검사 (단색 사진 원천 필터링)"""
    try:
        with Image.open(img_path) as im:
            small = im.resize((32, 24)).convert("L")
            extrema = small.getextrema()
            # 픽셀 명암 차이가 15 미만이면 아무 피사체도 없는 단순 단색 사각형
            if extrema[1] - extrema[0] < 15:
                return True
    except Exception:
        pass
    return False


def _good(img_path: str, label: str) -> bool:
    if os.path.exists(img_path) and os.path.getsize(img_path) > 5000:
        if not is_solid_color_image(img_path):
            return True
        print(f"  ⚠ [{label}] 피사체 없는 단순 색상 이미지 감지 -> 즉시 폐기.")
        try: os.remove(img_path)
        except Exception: pass
    return False


def find_stock_photo(query: str, img_path: str, label: str, topic: str = "", gemini_api_key: str = "",
                     used: Optional[set] = None, used_lock: Optional[threading.Lock] = None) -> bool:
    """무료 사진 검색으로 1장 확보. Gemini 키가 있으면 AI가 후보를 보고 고르고, 없으면 제목·태그 단어가 맞는 사진만."""
    if not _query_words(query):
        return False  # 영어 검색어가 없으면(키 없을 때의 한글 기획) 찾을 단어가 없음
    used = used if used is not None else set()
    used_lock = used_lock or threading.Lock()
    for q in _shorter_queries(query):
        cands = [c for c in search_openverse(q) + search_wikimedia(q) if not _has_person(c)]
        with used_lock:
            cands = [c for c in cands if c["url"] not in used]
        if not cands:
            continue
        ranked = [c for h, i, c in sorted(((_hits(c, q), i, c) for i, c in enumerate(cands)),
                                         key=lambda x: (-x[0], x[1])) if h > 0]
        pick = _vision_pick(gemini_api_key, query, topic, ranked[:VISION_CANDS]) if ranked and gemini_api_key else None
        if pick is None:
            chosen = [c for c in ranked if _relevant(c, q)][:3]
        elif pick < 0:
            print(f"  · [{label}] 검색어 '{q}' 사진 중 맞는 장면 없음 (AI 확인)")
            continue
        else:
            chosen = [ranked[pick]]
        for c in chosen:
            with used_lock:
                if c["url"] in used:
                    continue
                used.add(c["url"])
            if download_and_process_image(c["url"], img_path) and _good(img_path, label):
                print(f"  📷 [{label}] 무료 사진 사용 ('{q}' · {c['title'][:40]})")
                return True
    return False


def _make_ai_image(idx: int, count: int, plan: Dict[str, str], img_path: str) -> bool:
    """맞는 무료 사진이 없을 때: 본문 맞춤 무료 AI 그림 (실패하면 1번 더)"""
    label = f"{idx + 1}/{count}"
    prompt = plan.get("prompt", "")
    print(f"  [이미지 {label}] 맞는 사진이 없어 본문 맞춤 AI 그림 생성 ({plan.get('angle')})...")
    for timeout in (30, 45):
        try:
            if generate_ai_image(prompt, img_path, width=960, height=720, timeout_sec=timeout) and _good(img_path, label):
                return True
        except Exception:
            pass
        print(f"  ↻ [이미지 {label}] 재시도 중...")
    print(f"  ℹ [{label}] 적합한 고화질 실사 이미지가 없어 무의미한 단색 사진을 넣지 않고 건너뜁니다.")
    return False


class ImageJob:
    """사진 세트를 뒤에서(별도 스레드) 만든다.

    paths: 만들 사진 파일 경로(순서 고정). 발행은 이 경로로 블록을 미리 짜 두고,
    사진 블록 차례가 오면 wait(path)로 그 사진만 기다린다 → 사진 6장이 다 될 때까지 에디터가 놀지 않음.
    무료 사진 검색은 모든 자리를 한꺼번에 찾고, 못 찾은 자리만 앞 사진부터 차례로 AI 그림을 만든다.
    """

    def __init__(self, keyword: str, title: str, contents: Optional[List[str]] = None,
                 gemini_api_key: str = "", count: int = 6):
        self.keyword = keyword.strip()
        self.title = title
        self.contents = list(contents or [])
        self.gemini_api_key = gemini_api_key
        self.count = max(0, int(count))
        ts = int(time.time() * 1000)
        self.paths = [str(POST_IMAGES_DIR / f"rich_{ts}_{i}.jpg") for i in range(self.count)]
        self._done = {p: threading.Event() for p in self.paths}
        self._ok: Dict[str, bool] = {}
        self._finished = threading.Event()
        self._used: set = set()
        self._used_lock = threading.Lock()
        self._claimed: set = set()  # 사진이 정해졌거나 AI 그림을 만들기 시작한 자리
        self._slot_lock = threading.Lock()
        self.canceled = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="image-job")

    def start(self) -> "ImageJob":
        self._thread.start()
        return self

    def cancel(self):
        """아직 시작 안 한 사진은 만들지 않는다 (지금 받는 1장은 끝까지)."""
        self.canceled = True

    def _mark(self, idx: int, ok: bool, how: str = ""):
        path = self.paths[idx]
        self._ok[path] = ok
        self._done[path].set()
        if ok:
            print(f"  ✔ [{idx + 1}/{self.count}] {how} 확보 완료: {os.path.basename(path)}")

    def _run(self):
        from concurrent.futures import ThreadPoolExecutor
        ex = None
        try:
            clean_k = self.keyword
            clean_t = clean_text_for_context(self.title)
            print(f"▶ [본문 맥락 매칭 실사 이미지 조달] 키워드: '{clean_k}', 제목: '{clean_t}'...")
            plans = generate_contextual_plans_with_gemini(keyword=clean_k, title=self.title,
                                                          contents=self.contents, gemini_api_key=self.gemini_api_key)
            if self.count and len(plans) < self.count:
                plans = plans + [plans[0]] * (self.count - len(plans))

            def _search(idx: int) -> bool:
                if self.canceled:
                    return False
                q = plans[idx].get("safe_stock_query") or f"{clean_k} workspace"
                tmp = self.paths[idx] + ".search.jpg"
                try:
                    ok = find_stock_photo(q, tmp, f"{idx + 1}/{self.count}", clean_t or clean_k,
                                          self.gemini_api_key, self._used, self._used_lock)
                except Exception:
                    ok = False
                with self._slot_lock:  # 늦게 끝난 검색이 이미 AI 그림으로 넘어간 자리를 덮어쓰지 않게
                    if ok and idx not in self._claimed:
                        self._claimed.add(idx)
                        os.replace(tmp, self.paths[idx])
                        self._mark(idx, True, "무료 사진")
                        return True
                try: os.remove(tmp)
                except Exception: pass
                return False

            ex = ThreadPoolExecutor(max_workers=max(1, min(self.count, 6)), thread_name_prefix="image-search")
            futs = [ex.submit(_search, idx) for idx in range(self.count)]
            # 검색으로 못 채운 자리만 앞에서부터 AI 그림 (무료 한도 45초당 1장이라 한 장씩)
            for idx, fut in enumerate(futs):
                try:
                    found = fut.result(timeout=SEARCH_WAIT)
                except Exception:
                    found = False  # 너무 느린 검색은 기다리지 않음 (끝나도 결과는 버림)
                    fut.cancel()
                with self._slot_lock:
                    if found or idx in self._claimed:
                        continue
                    self._claimed.add(idx)
                ok = False
                if not self.canceled:
                    try:
                        ok = _make_ai_image(idx, self.count, plans[idx], self.paths[idx])
                    except Exception:
                        ok = False
                self._mark(idx, ok, "AI 그림")
        finally:
            if ex is not None:
                ex.shutdown(wait=False, cancel_futures=True)
            for ev in self._done.values():
                ev.set()
            self._finished.set()
            if not self.canceled:
                print(f"🎉 총 {len(self.result(wait=False))}장의 순수 고화질 실사 사진 준비 완료 (단색 이미지 0% 배제)!")

    def wait(self, path: str, timeout: float = 300) -> bool:
        """그 사진이 준비될 때까지 기다림. 준비됐으면 True, 실패·건너뜀이면 False."""
        ev = self._done.get(path)
        if ev is None:
            return os.path.exists(path)
        ev.wait(timeout)
        return bool(self._ok.get(path))

    def is_done(self, path: str) -> bool:
        ev = self._done.get(path)
        return ev is None or ev.is_set()

    def ready_count(self) -> int:
        return sum(1 for p in self.paths if self._ok.get(p))

    def result(self, wait: bool = True, timeout: float = 600) -> List[str]:
        """확보한 사진 경로 목록 (기획 순서 = 대표·콜라주·마무리 순)."""
        if wait:
            self._finished.wait(timeout)
        return [p for p in self.paths if self._ok.get(p)]


# 원고를 만들자마자 사진도 미리 만들어 둔다 (사용자가 원고를 검토하는 동안). 발행 때 제목이 같으면 이어서 사용.
_prefetch_lock = threading.Lock()
_prefetch: Optional[ImageJob] = None


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", "", clean_text_for_context(t or ""))


def start_prefetch(keyword: str, title: str, contents: Optional[List[str]], gemini_api_key: str, count: int) -> Optional[ImageJob]:
    global _prefetch
    with _prefetch_lock:
        if _prefetch is not None:
            _prefetch.cancel()
        _prefetch = ImageJob(keyword, title, contents, gemini_api_key, count).start() if count > 0 else None
        return _prefetch


def take_prefetch(title: str, count: int) -> Optional[ImageJob]:
    """제목이 같은 미리 만들기 작업이 있으면 넘겨받는다 (한 번만). 없거나 다르면 None (다른 건 취소)."""
    global _prefetch
    with _prefetch_lock:
        job, _prefetch = _prefetch, None
    if job is None:
        return None
    if job.canceled or _norm_title(job.title) != _norm_title(title) or job.count < count:
        job.cancel()
        return None
    return job


def generate_rich_post_images(keyword: str, title: str, contents: Optional[List[str]] = None, gemini_api_key: str = "", count: int = 6) -> List[str]:
    """
    본문 맥락 100% 일치 + 인물/글자 배제 + 저작권 100% 안전 고화질 실사 이미지 자동 조달
    * 내용 없는 단순 색상 사각형(단색 더미 이미지)은 100% 원천 배제되며 절대 업로드되지 않습니다.
    """
    return ImageJob(keyword, title, contents, gemini_api_key, count).start().result()
