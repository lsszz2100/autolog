"""원고 텍스트 스타일 공용 규칙 (text_style.py)

- 소제목 판별: 원고 생성(ai.py), 블록 분할·에디터 서식(rich_publisher.py)이 같은 규칙을 사용
- 이모지 절제: 기본은 본문 이모지 0개 (꾸밈은 스마트에디터 스티커로 대체)
- 스티커 배치: 도입 뒤·중간 소제목 앞·맺음말 앞에 "[스티커:분위기]" 표시 줄을 넣고, 퍼블리셔가 그 자리에 스티커 삽입
"""
import re
from typing import List, Tuple

# 이모지·픽토그램 + 변형 선택자/ZWJ/키캡
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U0001F1E6-\U0001F1FF"
    "←-⇿⌀-⏿■-◿⤴⤵〰〽㊗㊙️‍⃣]+"
)

# "1. 소제목", "TIP 2. 소제목", "3) 소제목"은 짧을 때만. 앞의 이모지는 제거 후 판별
_SUBHEAD_RE = re.compile(r"^(?:\d{1,2}\.\s|TIP\s*\d{1,2}[.:]\s?)", re.I)


def strip_emojis(text: str) -> str:
    text = EMOJI_RE.sub("", text).replace("()", "")
    return re.sub(r"\s{2,}", " ", text).strip()


# URL·도메인·전화번호: 참고 글(외부 작성자) 경유 프롬프트 인젝션으로 외부 링크가 내 글에 섞이는 것 방지
_LINK_RE = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|kr|co\.kr|io|me|ly|shop|xyz|link)(?:/\S*)?\b"
                      r"|\b0\d{1,2}-\d{3,4}-\d{4}\b", re.I)


def strip_links(text: str) -> str:
    return re.sub(r"\s{2,}", " ", _LINK_RE.sub("", text)).strip() if _LINK_RE.search(text) else text


def is_hashtag_line(text: str) -> bool:
    """본문에 섞인 해시태그 줄 판별 (해시태그는 퍼블리셔가 본문 최하단에 1회만 입력)."""
    t = text.strip()
    if not t:
        return False
    words = t.split()
    if all(w.startswith("#") for w in words):
        return True
    if len(re.findall(r"#[가-힣a-zA-Z0-9_]+", t)) >= 2:
        return True
    return t.startswith("#") and len(words) <= 4


def is_subheading(line: str) -> bool:
    t = strip_emojis(line)
    return bool(t) and len(t) <= 60 and bool(_SUBHEAD_RE.match(t)) and not t.endswith(("다.", "요.", "니다"))


# 문단 내용에 맞춰 보충할 이모지 (앞쪽 규칙 우선), 맞는 게 없으면 위치별 기본값
_EMOJI_HINTS = [
    (r"주의|조심|위험|금지|사고|중독", "⚠️"),
    (r"추천|만족|좋[은았]|최고|강추", "👍"),
    (r"팁|요령|방법|노하우|비결|알아두", "💡"),
    (r"맛|음식|요리|식사|커피|카페", "😋"),
    (r"가을|단풍|낙엽", "🍂"),
    (r"여행|캠핑|산책|나들이|야외", "🌿"),
]
_EMOJI_DEFAULTS = ["😊", "💡", "👍"]


def _pick_emoji(text: str, pos: int, used: set) -> str:
    for pat, emo in _EMOJI_HINTS:
        if emo not in used and re.search(pat, text):
            return emo
    for emo in _EMOJI_DEFAULTS[pos:] + _EMOJI_DEFAULTS[:pos]:
        if emo not in used:
            return emo
    return _EMOJI_DEFAULTS[0]


def tone_down_emojis(title: str, lines: List[str], min_body_emojis: int = 2, max_body_emojis: int = 3) -> Tuple[str, List[str]]:
    """제목·소제목 이모지 제거, 본문은 줄당 1개·글 전체 min~max개.
    모자라면 도입·중간·마무리 쪽 본문 문단 끝에 내용에 어울리는 이모지를 보충한다."""
    out, used = [], 0
    for line in lines:
        if is_subheading(line):
            out.append(strip_emojis(line))
            continue
        found = EMOJI_RE.findall(line)
        if not found:
            out.append(line)
            continue
        if used < max_body_emojis:
            # 문장 끝 이모지 1개만 유지
            body = strip_emojis(line)
            if body:
                # 연속 이모지 묶음(😊✨)은 첫 글자 하나만 (변형 선택자 등 결합 문자 제외)
                out.append(f"{body} {found[-1][0]}")
                used += 1
                continue
        out.append(strip_emojis(line))

    if used < min_body_emojis:
        body_idx = [i for i, l in enumerate(out) if l.strip() and not is_subheading(l) and not EMOJI_RE.search(l)]
        if body_idx:
            n = len(body_idx)
            # 도입(첫 문단) → 중간 → 마무리(끝에서 두 번째, 맺음 인사 직전) 순
            spots = [body_idx[0], body_idx[n // 2], body_idx[max(0, n - 2)]]
            chosen = set()
            for pos, i in enumerate(dict.fromkeys(spots)):
                if used >= min_body_emojis + 1 or used >= max_body_emojis:
                    break
                emo = _pick_emoji(out[i], pos, chosen)
                chosen.add(emo)
                out[i] = f"{out[i].rstrip()} {emo}"
                used += 1
    return strip_emojis(title), out


# ── 스마트에디터 스티커 ──
# 원고에는 "[스티커:인사]" 같은 표시 줄로 넣어 사용자가 입력창에서 옮기거나 지울 수 있게 하고,
# 퍼블리셔가 해당 위치에 스티커 컴포넌트를 삽입한다 (글과 붙이지 않고 단독 줄).
STICKER_RE = re.compile(r"^\[스티커(?::\s*([가-힣a-zA-Z]+))?\]$")
STICKER_MOODS = ("인사", "포인트", "주의", "감사")
_CAUTION_RE = re.compile(r"주의|조심|위험|금지|사고|부작용|피해야|실수")


def sticker_mood(line: str):
    """스티커 표시 줄이면 분위기(인사/포인트/주의/감사)를, 아니면 None."""
    m = STICKER_RE.match(line.strip())
    if not m:
        return None
    mood = m.group(1) or "포인트"
    return mood if mood in STICKER_MOODS else "포인트"


def image_heading_slots(n_heads: int, n_images: int = 6) -> dict:
    """사진 포함 발행에서 사진이 들어가는 소제목 경계. {k: 종류} = k번째(0부터) 소제목 바로 앞.
    rich_publisher.build_auto_rich_blocks 와 place_sticker_markers 가 같은 규칙을 공유한다.
      - 도입 ↔ 1번: 콜라주(사진 2·3), 2번 ↔ 3번: 콜라주(사진 4·5), 마지막 번호 앞: 마무리 사진"""
    slots = {}
    if n_heads == 0:
        return slots
    if n_images >= 3:
        slots[0] = "collage1"
    if n_images >= 5 and n_heads >= 4:
        slots[2] = "collage2"
    if n_heads >= 2 and (n_images >= 6 or n_images in (2, 4)) and (n_heads - 1) not in slots:
        slots[n_heads - 1] = "closing"
    return slots


def place_sticker_markers(lines: List[str], count: int = 3) -> List[str]:
    """스티커 표시 줄을 '번호와 번호 사이'(예: 3번 끝나고 4번 전)에 최대 count개 넣는다.
    사진이 들어가는 경계(image_heading_slots)는 피해서 스티커와 사진이 붙지 않게 하고,
    고를 수 있는 경계가 많으면 앞·가운데·뒤로 고르게 나눈다. 이미 표시 줄이 있으면 그대로 둔다."""
    out = list(lines)
    if any(sticker_mood(l) for l in out):
        return out
    heads = [i for i, l in enumerate(out) if is_subheading(l)]
    n = len(heads)
    if n < 2:
        return out
    taken = image_heading_slots(n)
    cand = [k for k in range(1, n) if k not in taken]
    if len(cand) > count:
        cand = sorted({cand[0], cand[len(cand) // 2], cand[-1]})[:count]
    if not cand:
        return out
    moods = {1: ["포인트"], 2: ["인사", "감사"], 3: ["인사", "포인트", "감사"]}[len(cand)]
    picks = []
    for k, mood in zip(cand, moods):
        if mood == "포인트":
            # 직전 번호 섹션이 주의 내용이면 '주의' 스티커
            section = " ".join(out[heads[k - 1]:heads[k]])
            mood = "주의" if _CAUTION_RE.search(section) else "포인트"
        picks.append((heads[k], mood))
    # 번호 사이 자리가 1개뿐이면(소제목 4개 이하) 맺음말 문단 앞(마지막 섹션의 문단 사이)에 감사 스티커를 더해 최소 2개
    if len(picks) < 2:
        tail = [i for i in range(heads[-1] + 1, len(out)) if out[i].strip() and not is_subheading(out[i])]
        if len(tail) >= 2:
            picks = [(i, "인사" if m == "포인트" else m) for i, m in picks] + [(tail[-1], "감사")]
    for idx, mood in sorted(picks, reverse=True):
        block = [f"[스티커:{mood}]", ""]
        if out[idx - 1].strip():
            block.insert(0, "")
        out[idx:idx] = block
    return out
