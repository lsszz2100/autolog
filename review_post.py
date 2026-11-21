"""체험단·기자단 글 (review_post.py)

두 가지 방식:
  1) 가이드 원고: 업체·대행사가 준 가이드를 해석해 원고를 만든다 (본문 1,500자 이상, 가이드의 필수 문구·키워드·해시태그 반영)
  2) 그대로 올리기: 사용자가 준 원고를 고치지 않고 그대로 올린다

본문 표기 (원고 안에서 쓰는 약속, 두 방식 공통)
  **굵게**   __밑줄__   ==형광펜==   {{빨강:글자색}}  (빨강·주황·노랑·초록·파랑·남색·보라·분홍·회색)
  ## 소제목          > 인용구 한 줄
  [사진:1] [영상:1] [클립:1]   그 자리에 1번 사진·영상·클립
  [스티커] [구분선] [지도] [지도:장소명] [링크:https://...]
사진·영상·클립 번호는 올린 순서이며, 원고에도 1번부터 차례대로 들어간다.
"""
import logging
import re
from typing import Dict, List, Optional, Tuple

_PORTAL = "\ub124\uc774\ubc84"  # 포털 이름(한글), 소스에 상표명을 직접 쓰지 않음

logger = logging.getLogger("naver.review")

MIN_GUIDE_CHARS = 1500          # 가이드 원고 최소 분량 (공백 제외)
MAX_GUIDE = 20000
MAX_NOTES = 10000

# 글자색 이름 → 에디터 팔레트 색 (팔레트에 있는 값만 쓸 수 있다)
COLORS = {
    "빨강": "#ff0010", "빨간": "#ff0010", "주황": "#ff9300", "노랑": "#ffd300", "초록": "#00a84b",
    "파랑": "#0078cb", "파란": "#0078cb", "남색": "#004e82", "보라": "#aa1f91", "분홍": "#ff008c",
    "회색": "#777777", "검정": "#000000",
}
HIGHLIGHT = "#fff593"           # ==형광펜== 배경색

MEDIA_RE = re.compile(r"^\[(사진|영상|클립):\s*(\d{1,2})\]$")
LINK_RE = re.compile(r"^\[링크:\s*(https?://\S+)\]$")
STICKER_RE = re.compile(r"^\[스티커(?::\s*(인사|포인트|주의|감사))?\]$")
_TOKEN_RE = re.compile(r"(\*\*|__|==|\{\{[^:{}]{1,4}:|\}\})")


_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)


def norm_marker(line: str) -> str:
    """AI가 섞어 쓰는 전각 문자·보이지 않는 문자를 정리한 줄 (표기 판별용)."""
    s = (line or "").translate(_ZW)
    try:
        import unicodedata
        s = unicodedata.normalize("NFKC", s)
    except ImportError:
        pass
    return s.strip()


def media_marker(line: str) -> Optional[Tuple[str, int]]:
    m = MEDIA_RE.match(norm_marker(line))
    return (m.group(1), int(m.group(2))) if m else None


MAP_RE = re.compile(r"^\[지도(?::\s*([^\]]{1,60}))?\]$")


def map_marker(line: str) -> Optional[str]:
    """[지도] → '' (설정의 지도 검색어), [지도:장소명] → 장소명. 지도 줄이 아니면 None."""
    m = MAP_RE.match(norm_marker(line))
    return (m.group(1) or "").strip() if m else None


def parse_inline(line: str) -> List[Tuple[str, Dict]]:
    """한 줄의 강조 표기 → [(글자, {'bold','underline','highlight','color'})]. 짝이 안 맞는 표기는 글자 그대로."""
    parts = _TOKEN_RE.split(line)
    # 짝 맞는 표기만 서식으로 (홀수 개면 마지막 것은 글자로)
    counts = {t: parts.count(t) for t in ("**", "__", "==")}
    opens = sum(1 for p in parts if p.startswith("{{"))
    closes = parts.count("}}")
    state = {"bold": False, "underline": False, "highlight": False, "color": ""}
    seen = {"**": 0, "__": 0, "==": 0}
    color_depth = 0
    out: List[Tuple[str, Dict]] = []
    key = {"**": "bold", "__": "underline", "==": "highlight"}
    for p in parts:
        if not p:
            continue
        if p in key:
            seen[p] += 1
            if counts[p] % 2 == 1 and seen[p] == counts[p]:
                out.append((p, dict(state)))           # 짝 없는 마지막 표기
                continue
            state[key[p]] = not state[key[p]]
            continue
        if p.startswith("{{") and p.endswith(":"):
            name = p[2:-1].strip()
            if name in COLORS and closes > color_depth and opens:
                state["color"] = COLORS[name]
                color_depth += 1
                continue
            out.append((p, dict(state)))
            continue
        if p == "}}":
            if color_depth:
                color_depth -= 1
                state["color"] = ""
                continue
            out.append((p, dict(state)))
            continue
        out.append((p, dict(state)))
    # 같은 서식끼리 합치기
    merged: List[Tuple[str, Dict]] = []
    for t, s in out:
        if merged and merged[-1][1] == s:
            merged[-1] = (merged[-1][0] + t, s)
        else:
            merged.append((t, s))
    return merged


def line_html(line: str, heading_px: int = 24, body_px: int = 16, center: bool = False) -> str:
    """한 줄 → 에디터에 붙여넣을 HTML 문단 (굵게·밑줄·형광펜·글자색·소제목 크기·정렬)."""
    import html as _h
    s = (line or "").rstrip()
    align = ' style="text-align:center"' if center else ""
    if not s.strip():
        return f"<p{align}><br></p>"
    heading = is_heading(s)
    body = s.strip()[3:] if heading else s
    heading = heading or emoji_heading(s)
    out = []
    for text, st in parse_inline(body):
        if not text:
            continue
        t = _h.escape(text)
        t = re.sub(r"  +", lambda m: "&nbsp;" * len(m.group(0)), t)
        if st["underline"]:
            t = f"<u>{t}</u>"
        if st["bold"] or heading:
            t = f"<b>{t}</b>"
        style = []
        if st["color"]:
            style.append(f"color:{st['color']}")
        if st["highlight"]:
            style.append(f"background-color:{HIGHLIGHT}")
        if style:
            t = f'<span style="{";".join(style)}">{t}</span>'
        out.append(t)
    px = heading_px if heading else body_px
    return f'<p{align}><span style="font-size:{px}px">{"".join(out)}</span></p>'


_EMOJI_START = re.compile("^[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B55\u2705\u274C\u2728]")


def emoji_heading(line: str) -> bool:
    """'📍 매장 분위기' 처럼 이모지로 시작하는 짧은 한 줄 = 소제목 (기호 없는 원고의 소제목)."""
    s = (line or "").strip()
    return bool(_EMOJI_START.match(s)) and len(s) <= 30 and not re.search(r"[.?!]$|(다|요|죠)$", s)


def plain_text(line: str) -> str:
    """강조 표기를 뺀 글자만."""
    return "".join(t for t, _ in parse_inline(line))


def is_heading(line: str) -> bool:
    return (line or "").lstrip().startswith("## ")


QUOTE_RE = re.compile(r"^\[인용구?:\s*(.+?)\s*\]$")


def quote_text(line: str) -> Optional[str]:
    """인용구 줄이면 안쪽 글: "> 문장" 또는 "[인용:문장]"."""
    s = (line or "").lstrip()
    if s.startswith("> "):
        return s[2:]
    m = QUOTE_RE.match(norm_marker(s))
    return m.group(1) if m else None


def is_quote(line: str) -> bool:
    return quote_text(line) is not None


def is_special(line: str) -> bool:
    s = norm_marker(line)
    return bool(media_marker(s) or LINK_RE.match(s) or STICKER_RE.match(s) or s == "[구분선]" or map_marker(s) is not None)


def body_chars(lines: List[str]) -> int:
    """본문 글자 수 (공백·표기·사진 자리 제외)."""
    total = 0
    for l in lines:
        s = l.strip()
        if not s or is_special(s):
            continue
        if is_heading(s):
            s = s[3:]
        elif is_quote(s):
            s = quote_text(s)
        total += len(re.sub(r"\s+", "", plain_text(s)))
    return total


# ── 사진·영상·클립 순번 ──

def order_media(lines: List[str], counts: Dict[str, int]) -> List[str]:
    """[사진:N]·[영상:N]·[클립:N] 을 올린 순서대로 1, 2, 3… 으로 맞추고, 빠진 것도 모두 넣는다.
    사진은 AI가 쓴 번호를 위치 단서로 삼아 2~4장씩 묶어 글 전체에 고르게 나눈다 (남은 사진을 글 끝에 몰지 않음)."""
    n_photo = counts.get("사진", 0)
    out: List[str] = []
    nxt = {k: 1 for k in counts}
    photo_spots: List[Tuple[int, int]] = []           # (out 안 위치, AI가 쓴 사진 번호)
    for l in lines:
        mm = media_marker(l)
        if not mm:
            out.append(l)
            continue
        kind, num = mm
        if kind == "사진":
            if n_photo and 1 <= num <= 99:
                if photo_spots and photo_spots[-1][0] == len(out) - 1 and out[-1] == "\0사진":
                    photo_spots[-1] = (photo_spots[-1][0], max(photo_spots[-1][1], num))   # 연속 줄 = 한 묶음
                else:
                    out.append("\0사진")
                    photo_spots.append((len(out) - 1, num))
            continue
        if nxt.get(kind, 1) > counts.get(kind, 0):
            continue                                   # 올린 개수보다 많은 자리는 버림
        out.append(f"[{kind}:{nxt[kind]}]")
        nxt[kind] += 1
    out = _place_photos(out, photo_spots, n_photo)
    missing = [f"[{k}:{n}]" for k in ("영상", "클립") for n in range(nxt.get(k, 1), counts.get(k, 0) + 1)]
    if missing:
        out = spread_markers(out, missing)
    return out


def _place_photos(out: List[str], spots: List[Tuple[int, int]], n: int) -> List[str]:
    """사진 자리 표시(\\0사진)를 실제 [사진:N] 묶음으로 바꾼다."""
    if not n:
        return [l for l in out if l != "\0사진"]
    if not spots:
        return spread_markers([l for l in out if l != "\0사진"], [f"[사진:{i}]" for i in range(1, n + 1)])
    # AI가 쓴 번호를 사진 전체 범위로 늘려 각 자리의 '마지막 사진 번호'로 (순서 유지, 자리마다 최소 1장)
    top = max(num for _, num in spots)
    ends: List[int] = []
    k = len(spots)
    for j, (_, num) in enumerate(spots):
        e = round(num * n / top) if top < n else min(num, n)
        e = max(e, (ends[-1] + 1) if ends else 1)
        e = min(e, n - (k - 1 - j))                    # 뒤 자리마다 1장은 남긴다
        ends.append(e)
    ends[-1] = n
    groups: List[List[int]] = []
    prev = 0
    for e in ends:
        groups.append(list(range(prev + 1, e + 1)))
        prev = e
    # 한 자리에 5장 이상이면 그 자리 ~ 다음 사진 자리 사이 문단으로 3장씩 나눠 보낸다
    pos = [p for p, _ in spots]
    ins: Dict[int, List[str]] = {}
    for j, g in enumerate(groups):
        if len(g) <= 4:
            ins.setdefault(pos[j], []).extend(f"[사진:{i}]" for i in g)
            continue
        nxt_pos = pos[j + 1] if j + 1 < len(pos) else len(out)
        paras = [i + 1 for i in range(pos[j] + 1, nxt_pos) if out[i].strip() and not is_special(out[i])
                 and not _TAG_LINE.fullmatch(out[i].strip())
                 and (i + 1 >= nxt_pos or not out[i + 1].strip())]
        chunks = [g[i:i + 3] for i in range(0, len(g), 3)]
        if not paras:
            ins.setdefault(pos[j], []).extend(f"[사진:{i}]" for i in g)
            continue
        ins.setdefault(pos[j], []).extend(f"[사진:{i}]" for i in chunks[0])
        rest = chunks[1:]
        for c_i, chunk in enumerate(rest):
            at = paras[min(len(paras) - 1, int(c_i * len(paras) / len(rest)))]
            ins.setdefault(at, []).extend(f"[사진:{i}]" for i in chunk)
    res: List[str] = []
    for i, l in enumerate(out):
        if i in ins and l != "\0사진":
            res.extend(ins[i])
        if l == "\0사진":
            res.extend(ins.get(i, []))
            continue
        res.append(l)
    if len(out) in ins:
        res.extend(ins[len(out)])
    return res


def spread_markers(lines: List[str], markers: List[str]) -> List[str]:
    """표시가 없는 사진 등을 문단 사이에 고르게 (앞에서부터 차례대로). 해시태그 줄 앞까지만."""
    if not markers:
        return lines
    end = len(lines)
    while end > 0 and (not lines[end - 1].strip() or lines[end - 1].strip().startswith("#") and not is_heading(lines[end - 1])):
        end -= 1
    # 문단 끝(다음 줄이 빈 줄이거나 소제목) 자리들
    spots = [i + 1 for i in range(end) if lines[i].strip() and not is_special(lines[i]) and not is_heading(lines[i])
             and (i + 1 >= end or not lines[i + 1].strip() or is_heading(lines[i + 1]) or is_special(lines[i + 1]))]
    # 이미 표시가 있는 자리보다 뒤쪽 문단에만 (번호 순서 유지). 그런 자리가 없으면 글 끝에
    last_marker = max([i for i, l in enumerate(lines[:end]) if media_marker(l)] or [-1])
    later = [s for s in spots if s > last_marker]
    if not later:
        return lines[:end] + markers + lines[end:]
    n = len(markers)
    picks = [later[min(len(later) - 1, int(j * len(later) / n))] for j in range(n)]
    ins: Dict[int, List[str]] = {}
    for at, mk in zip(picks, markers):
        ins.setdefault(at, []).append(mk)
    out: List[str] = []
    for i, l in enumerate(lines):
        if i in ins:
            out.extend(ins[i])
        out.append(l)
    if len(lines) in ins:
        out.extend(ins[len(lines)])
    return out


# ── 해시태그 ──

_TAG_LINE = re.compile(r"(#[^\s#]+\s*)+")


def split_hashtags(lines: List[str]) -> Tuple[List[str], List[str]]:
    """'#태그 #태그' 만 있는 줄을 떼어 태그 목록으로 (해시태그는 발행 때 글 맨 끝에 한 번 넣는다).
    '## 소제목' 은 해시태그가 아니다."""
    tags: List[str] = []
    body: List[str] = []
    for l in lines:
        s = l.strip()
        if s and not is_heading(s) and _TAG_LINE.fullmatch(s):
            tags += re.findall(r"#([^\s#]+)", s)
            continue
        body.append(l)
    while body and not body[-1].strip():
        body.pop()
    return body, tags


def merge_tags(*groups: List[str], limit: int = 30) -> List[str]:
    out: List[str] = []
    for g in groups:
        for t in g or []:
            t = re.sub(r"[\s#,]+", "", str(t))[:40]
            if t and t not in out:
                out.append(t)
    return out[:limit]


def parse_tags(text: str) -> List[str]:
    return merge_tags(re.split(r"[\s,#]+", text or ""))


# ── 꾸밈 기호 없애기 (가이드 원고는 기호 없이: **, ##, ==, __, >, {{색:}}) ──

_COLOR_RE = re.compile(r"\{\{[^:{}]{1,4}:([^{}]*)\}\}")


def strip_markup(line: str) -> str:
    s = line or ""
    if media_marker(s) or norm_marker(s).startswith("["):
        return s
    s = re.sub(r"^\s*#{2,6}\s+", "", s)                # ## 소제목 → 소제목
    m = re.match(r"^\s*>\s?(.+)$", s)
    if m:                                               # > 인용 → [인용:문장]
        return f"[인용:{strip_markup(m.group(1))}]"
    s = _COLOR_RE.sub(r"\1", s)
    s = s.replace("**", "").replace("__", "").replace("==", "").replace("~~", "").replace("`", "")
    s = re.sub(r"^\s*[*\-]\s+(?=\S)", "• ", s)          # 마크다운 목록 → 가운뎃점
    return s.rstrip()


# ── 가이드 필수 사항 (정규식으로 확실히 잡히는 것만) ──

def guide_requirements(guide: str) -> Dict:
    """가이드에서 해시태그·필수 키워드(횟수)·제목 키워드·필수 문구를 뽑는다."""
    req: Dict = {"hashtags": [], "keywords": {}, "title": [], "phrases": []}
    for l in (guide or "").splitlines():
        s = l.strip()
        if not s:
            continue
        req["hashtags"] += [t for t in re.findall(r"#([^\s#,]+)", s) if not re.match(r"\d", t)]
        m = re.match(r"^(.{0,30}?)[:：\]]\s*(.+)$", s)
        if not m:
            continue
        head, body = m.group(1), m.group(2).strip()
        if not body:
            continue
        if re.search(r"(태그|해시태그)", head):
            req["hashtags"] += [t for t in re.split(r"[\s,#/]+", body) if 1 < len(t) <= 30 and not re.search(r"\d+\s*개", t)]
            continue
        quoted = re.findall(r"[\'\"‘“「『]([^\'\"’”」』]{2,120})[\'\"’”」』]", body)
        if re.search(r"(문구|멘트|문장|고지)", head):
            req["phrases"] += quoted or ([re.sub(r"\s*\(.*?\)\s*$", "", body)] if len(body) >= 8 else [])
            continue
        if re.search(r"키워드", head):
            n = re.search(r"(\d+)\s*회", body)
            times = int(n.group(1)) if n else 1
            words = quoted or re.split(r"[,/·]|\s{2,}", re.sub(r"\([^)]*\)", "", body))
            for w in words:
                w = w.strip().strip(".")
                if 1 < len(w) <= 25:
                    if "제목" in head:
                        req["title"].append(w)
                    else:
                        req["keywords"][w] = max(req["keywords"].get(w, 0), times)
    req["hashtags"] = merge_tags(req["hashtags"])
    req["phrases"] = list(dict.fromkeys(p.strip() for p in req["phrases"] if p.strip()))[:10]
    return req


def _nospace(t: str) -> str:
    return re.sub(r"\s+", "", t or "")


def missing_requirements(title: str, lines: List[str], req: Dict) -> List[str]:
    body = _nospace("\n".join(plain_text(l) for l in lines))
    out = []
    for w in req.get("title") or []:
        if _nospace(w) not in _nospace(title):
            out.append(f"제목에 '{w}' 넣기")
    for w, n in (req.get("keywords") or {}).items():
        have = body.count(_nospace(w))
        if have < n:
            out.append(f"본문에 '{w}' {n}회 이상 (지금 {have}회)")
    for ph in req.get("phrases") or []:
        if _nospace(ph) not in body:
            out.append(f"문구 그대로 넣기: {ph}")
    return out


def _repair_prompt(guide: str, title: str, lines: List[str], missing: List[str]) -> str:
    body = "\n".join(lines)
    todo = "\n".join("- " + m for m in missing)
    return f"""아래 블로그 원고가 가이드의 필수 사항 일부를 지키지 못했습니다. 빠진 것을 모두 채워 고쳐 주세요.
[빠진 필수 사항]
{todo}

- 나머지 문장·[사진:N] 같은 표기·순서는 그대로 두고, 필요한 곳에 자연스럽게 넣거나 문장을 조금 고치세요.
- 제목을 고쳐야 하면 title도 고치세요. **, ##, ==, __, > 같은 꾸밈 기호는 쓰지 마세요.

<가이드>
{guide[:6000]}
</가이드>

<원고 제목>{title}</원고 제목>
<원고>
{body}
</원고>

JSON으로만 답하세요: {{"title": "제목", "contents": ["..."]}}"""


def _ai_lines(items) -> List[str]:
    """AI가 준 contents → 줄 목록. 한 항목에 여러 줄(사진 표기 포함)을 몰아 넣는 경우가 있어 줄마다 나눈다."""
    out: List[str] = []
    for it in items or []:
        if isinstance(it, (str, int, float)):
            out += [strip_markup(x) for x in str(it).replace("\r\n", "\n").split("\n")]
    return out


# ── 가이드 원고 생성 ──

def _prompt(guide: str, notes: str, brand: str, photo_descs: List[str], n_videos: int, clip_titles: List[str],
            min_chars: int) -> str:
    photos = "\n".join(f"- 사진 {i}: {d or '(설명 없음)'}" for i, d in enumerate(photo_descs, 1))
    clips = "\n".join(f"- 클립 {i}: {t}" for i, t in enumerate(clip_titles, 1))
    media = ""
    if photo_descs:
        media += f"\n[올린 사진 {len(photo_descs)}장 - 올린 순서]\n{photos}\n"
    if n_videos:
        media += f"\n[올린 영상 {n_videos}개]\n"
    if clip_titles:
        media += f"\n[넣을 클립 {len(clip_titles)}개]\n{clips}\n"
    return f"""당신은 {_PORTAL} 블로그 체험단·기자단 원고를 쓰는 전문 작가입니다.
아래 [가이드]는 업체·대행사가 준 작성 가이드입니다. 가이드를 꼼꼼히 해석해서, 가이드의 모든 요구를 지킨 블로그 원고를 쓰세요.

[가이드]
<가이드>
{guide}
</가이드>
{f'''
[업체·제품명] {brand}
''' if brand else ''}{f'''
[글쓴이 메모 - 실제로 겪은 내용, 가이드보다 우선하는 사실]
<메모>
{notes}
</메모>
''' if notes else ''}{media}
[가이드 해석·반영 규칙 - 가장 중요]
0. 무조건 가이드대로 쓰세요. 가이드 지시와 아래 규칙이 부딪히면 가이드가 우선입니다(단, 꾸밈 기호 금지는 지킬 것).
1. 가이드에 있는 필수 사항을 하나도 빠뜨리지 마세요: 제목에 넣을 키워드, 본문 필수 키워드(횟수 지정이 있으면 그 횟수), 꼭 넣을 문구(대가 표시·협찬 문구·체험단 문구 등은 가이드 문장 그대로), 소개할 특징·장점, 가격·위치·영업시간 같은 정보, 링크·지도 삽입 요청, 사진·영상 위치 요청, 글 구성 순서.
2. 가이드의 금지 사항(쓰면 안 되는 표현·비교·과장)은 절대 쓰지 마세요.
3. 가이드에 해시태그·태그가 있으면 하나도 빼지 말고 전부 그대로 tags에 넣고(가이드 태그가 먼저), 모자라면 내용에 맞는 태그를 더해 10~15개를 채우세요. 가이드에 태그가 없어도 10~15개를 꼭 만드세요.
4. 대가(협찬·제품 제공·원고료) 표시 문구가 가이드에 있으면 그 문장 그대로, 없으면 "업체로부터 제품(서비스)을 제공받아 작성한 솔직한 후기입니다." 같은 문장을 본문 맨 첫 줄에 넣으세요. 가이드가 끝에도 넣으라고 하면 끝에도 넣으세요.
5. 가이드·메모·사진에 없는 사실을 지어내지 마세요: 수치·기간·효과·가격·주소·전화번호, 직원·사장님의 응대, 다른 손님 모습, 날씨, 함께 간 사람, 재방문 여부 같은 겪은 일. 확인되지 않은 효능을 단정하지 마세요. 체험 소감은 가이드·메모·사진에 근거한 범위에서 1인칭으로 자연스럽게 쓰고, 분량은 가이드에 있는 특징을 더 자세히 풀어 설명해서 채우세요.
6. 본문 분량은 반드시 공백 제외 {min_chars:,}자 이상입니다. 가이드가 더 길게 요구하면 그 분량을 따르세요.
7. 말투·문체는 가이드가 정하면 그대로, 정하지 않으면 친근한 블로그 존댓말로 쓰세요.

[꾸밈 표기 - 가이드가 원하는 꾸밈을 아래 표기로 넣으세요]
- 기호 금지: **, ##, ###, ==, __, >, *, ~~, 백틱, {{{{색:글자}}}} 같은 마크다운·꾸밈 기호를 절대 쓰지 마세요. 강조하고 싶은 말도 기호 없이 문장으로만.
- 이모지: 가이드가 금지하지 않으면 문단·소제목에 어울리게 적당히 쓰세요(문단마다 0~2개). 가이드가 금지하면 쓰지 마세요.
- 소제목: 기호 없이 짧은 한 줄(앞에 어울리는 이모지 하나를 붙여도 됨, 예: "📍 매장 분위기"). 4~6개, 소제목 줄 앞뒤는 빈 줄.
- 인용구: "[인용:핵심 한 줄]" 한 줄로 2~3개 (도입부 한 줄 요약, 가장 좋았던 점, 마무리 한 줄 등). 인용구 안 글은 40자 이내. 가이드가 인용구를 정하면 그대로.
- 글은 가운데 정렬로 올라갑니다. 문단은 1~3문장으로 짧게 끊고 문단 사이에 빈 줄을 넣어 모바일에서 읽기 좋게 쓰세요.
- 그 밖에 줄 하나로: [구분선], [스티커], [지도:가게·장소 이름](가이드가 지도·위치 삽입을 원하면, 가이드에 나온 상호명 그대로), [링크:https://주소](가이드에 링크가 있으면 그 주소 그대로).
- 지도 줄 아래에 주소·전화번호를 따로 쓰지 마세요 (가이드에 주소가 있을 때만 가이드 그대로).
- 모든 표기는 반각 문자로 정확히 쓰세요 (예: [사진:1]). 전각 숫자·괄호를 쓰지 마세요.
- 사진 자리: "[사진:번호]" 한 줄에 하나. 올린 사진을 1번부터 마지막 번호까지 올린 순서 그대로 빠짐없이 한 번씩 넣으세요(번호를 건너뛰거나 순서를 바꾸지 말 것).
- 사진이 많으면(8장 이상) 내용이 같은 사진끼리 2~4장을 연달아 묶어 한 자리에 넣고(예: "[사진:3]" "[사진:4]" "[사진:5]" 를 연속 줄로), 그 묶음 바로 앞뒤 문단은 그 사진들의 내용과 맞게 쓰세요. 묶음은 글 처음부터 끝까지 고르게 나누고, 앞쪽에 1장씩 넣다가 남은 사진을 글 끝에 몰아넣지 마세요. 사진 설명 문장을 그대로 옮기지는 마세요.
- 영상 자리: "[영상:번호]", 클립 자리: "[클립:번호]" 한 줄씩. 1번부터 차례대로 모두 한 번씩, 가이드가 위치를 정하면 그 위치에.
- 위 표기([사진:N], [인용:...] 등 대괄호 표기) 말고 다른 표기는 쓰지 마세요.
- 본문 마지막 줄은 해시태그 한 줄(예: "#태그1 #태그2 ...")로 끝내세요. tags와 같은 태그.

[제목] 가이드의 제목 규칙(필수 키워드·글자 수·형식)을 지키고, 없으면 검색에 잘 걸리는 자연스러운 제목.

반드시 아래 JSON으로만 답하세요 (코드블록 금지):
{{
  "title": "제목",
  "contents": ["본문 첫 줄(대가 표시 문구)", "", "[인용:한 줄 요약]", "", "도입 문단", "", "[사진:1]", "[사진:2]", "", "📍 소제목", "", "문단", "...", "#태그1 #태그2"],
  "tags": ["태그1", "태그2"],
  "checklist": ["가이드 필수 사항을 어떻게 반영했는지 짧게"]
}}"""


def _expand_prompt(guide: str, title: str, lines: List[str], need: int) -> str:
    body = "\n".join(lines)
    return f"""아래 블로그 원고는 공백 제외 {need:,}자보다 짧습니다. 가이드를 지키면서 공백 제외 {need:,}자 이상이 되도록 늘려 주세요.
- 기존 문장·표기([사진:N], [영상:N], [클립:N] 등)와 순서는 그대로 두고, 문단을 보충하거나 소제목 아래 내용을 더 쓰세요.
- **, ##, ==, __, > 같은 꾸밈 기호는 쓰지 마세요. 이모지는 적당히 써도 됩니다.
- 가이드·원고에 없는 수치·효과·가격을 지어내지 마세요.

<가이드>
{guide[:6000]}
</가이드>

<원고 제목>{title}</원고 제목>
<원고>
{body}
</원고>

JSON으로만 답하세요: {{"contents": ["..."]}}"""


def generate_from_guide(guide: str, api_key: str, notes: str = "", brand: str = "",
                        photo_descs: Optional[List[str]] = None, n_videos: int = 0,
                        clip_titles: Optional[List[str]] = None, min_chars: int = MIN_GUIDE_CHARS
                        ) -> Tuple[str, List[str], List[str], List[str]]:
    """가이드 → (제목, 본문 줄, 해시태그, 가이드 반영 체크리스트)."""
    import gemini_client
    from ai import WRITER_MODELS
    guide = (guide or "").strip()[:MAX_GUIDE]
    if not guide:
        raise ValueError("가이드 내용을 넣어 주세요.")
    if not (api_key or "").strip():
        raise ValueError("Gemini API 키가 필요합니다. [설정]에서 키를 넣어 주세요.")
    photo_descs = list(photo_descs or [])
    clip_titles = list(clip_titles or [])

    def accept(text: str):
        d = gemini_client.extract_json(text)
        return d if isinstance(d, dict) and d.get("contents") else None

    data, model = gemini_client.generate(
        api_key.strip(), _prompt(guide, (notes or "").strip()[:MAX_NOTES], (brand or "").strip()[:100],
                                 photo_descs, n_videos, clip_titles, min_chars),
        WRITER_MODELS, generation_config={"temperature": 0.7, "maxOutputTokens": 12000}, timeout=40, accept=accept)
    logger.info(f"📝 체험단 가이드 원고 생성 ({model})")
    title = re.sub(r"\s+", " ", str(data.get("title") or "")).strip()[:100]
    lines = _ai_lines(data.get("contents"))
    first = next((i for i, l in enumerate(lines) if l.strip()), None)
    if first is not None and re.sub(r"\s+", "", lines[first]) == re.sub(r"\s+", "", title):
        del lines[first]                                  # 본문 첫 줄에 제목을 또 쓴 경우
    lines, body_tags = split_hashtags(lines)
    req = guide_requirements(guide)
    tags = merge_tags(req["hashtags"], [str(t) for t in data.get("tags") or []], body_tags)
    checklist = [str(c)[:200] for c in data.get("checklist") or []][:20]

    for _ in range(3):
        if body_chars(lines) >= min_chars:
            break
        logger.info(f"📝 분량 보충: {body_chars(lines):,}자 → {min_chars:,}자 이상")
        try:
            more, _m = gemini_client.generate(api_key.strip(), _expand_prompt(guide, title, lines, min_chars),
                                              WRITER_MODELS, generation_config={"temperature": 0.6, "maxOutputTokens": 12000},
                                              timeout=40, accept=accept)
            new = _ai_lines(more.get("contents"))
            new, more_tags = split_hashtags(new)
            if body_chars(new) > body_chars(lines):
                lines = new
                tags = merge_tags(tags, more_tags)
        except Exception as e:
            logger.warning(f"분량 보충 실패: {e}")
            break
    missing = missing_requirements(title, lines, req)
    if missing:                       # 가이드 필수 사항이 빠졌으면 한 번 더 고쳐 쓰게 한다
        logger.info(f"📝 가이드 필수 사항 보완: {missing}")
        try:
            fixed, _m = gemini_client.generate(api_key.strip(), _repair_prompt(guide, title, lines, missing),
                                               WRITER_MODELS, generation_config={"temperature": 0.4, "maxOutputTokens": 12000},
                                               timeout=40, accept=accept)
            new = _ai_lines(fixed.get("contents"))
            new, more_tags = split_hashtags(new)
            if len(missing_requirements(str(fixed.get("title") or title), new, req)) < len(missing) \
                    and body_chars(new) >= min(body_chars(lines), min_chars):
                lines, title = new, re.sub(r"\s+", " ", str(fixed.get("title") or title)).strip()[:100]
                tags = merge_tags(tags, more_tags)
        except Exception as e:
            logger.warning(f"가이드 필수 사항 보완 실패: {e}")
    for m in missing_requirements(title, lines, req):
        checklist.append("⚠️ 확인 필요: " + m)
    if not tags:                      # 태그는 꼭 넣는다 (가이드에 없고 AI도 안 줬으면 제목 단어로)
        tags = merge_tags([w for w in re.split(r"[\s,·]+", re.sub(r"[^\w\s가-힣]", " ", title)) if len(w) >= 2])[:10]
    lines = order_media(lines, {"사진": len(photo_descs), "영상": n_videos, "클립": len(clip_titles)})
    lines = _tidy(lines)
    if tags:                          # 원고 맨 끝에 태그 줄 (발행 때도 글 맨 끝에 들어간다)
        lines += ["", " ".join("#" + t for t in tags)]
    return title, lines, tags, checklist


def _tidy(lines: List[str]) -> List[str]:
    """빈 줄 연속 정리, 앞뒤 빈 줄 제거."""
    out: List[str] = []
    for l in lines:
        if not l.strip() and (not out or not out[-1].strip()):
            continue
        out.append(l)
    while out and not out[-1].strip():
        out.pop()
    return out


def prepare_direct(content: str, counts: Dict[str, int]) -> Tuple[List[str], List[str]]:
    """그대로 올리기: 원고는 고치지 않고, 끝의 해시태그 줄만 떼고, 사진 표시가 없으면 문단 사이에 차례로 넣는다."""
    lines = [l.rstrip() for l in (content or "").replace("\r\n", "\n").split("\n")]
    lines, tags = split_hashtags(lines)
    return order_media(lines, counts), tags


# ── 발행 블록 ──

def build_blocks(lines: List[str], photos: Dict[int, str], videos: Dict[int, Dict], clips: Dict[int, Dict]) -> List[Dict]:
    """본문 줄 → 에디터 블록. 글은 표기 그대로 'styled' 블록으로, 사진·영상·클립은 각자 블록으로."""
    blocks: List[Dict] = []
    text: List[str] = []

    def flush():
        if any(t.strip() for t in text):
            blocks.append({"type": "styled", "lines": text[:]})
        text.clear()

    for l in lines:
        mm = media_marker(l)
        if mm:
            kind, n = mm
            item = {"사진": photos, "영상": videos, "클립": clips}[kind].get(n)
            if not item:
                continue
            flush()
            if kind == "사진":
                blocks.append({"type": "single_image", "path": item})
            elif kind == "영상":
                blocks.append({"type": "video", "path": item["path"], "title": item.get("title", "")})
            else:
                blocks.append({"type": "clip", "id": item["id"], "title": item.get("title", "")})
            continue
        if is_quote(l):
            flush()
            blocks.append({"type": "quotation", "lines": [plain_text(quote_text(l))]})
            continue
        if text == [] and not l.strip():
            continue                                   # 블록 맨 앞 빈 줄은 버림 (사진 뒤 빈 줄)
        text.append(l)
    flush()
    return blocks


def check_guide_text(text: str) -> str:
    """가이드가 너무 짧거나 비면 사유."""
    t = (text or "").strip()
    if len(t) < 20:
        return "가이드 내용을 20자 이상 넣어 주세요."
    return ""


# ── 내 클립 목록 ──

def list_my_clips(profile, blog_id: str, limit: int = 60) -> List[Dict]:
    """글쓰기 화면의 [내 클립] 패널에서 내 클립 목록 [{id, title, date, thumb}] (최신순)."""
    from playwright.sync_api import sync_playwright
    from browser import launch_naver_context, first_page
    from auth import is_logged_in, check_login_status
    from rich_publisher import RichSmartEditorPublisher
    with sync_playwright() as pw:
        ctx = launch_naver_context(pw, headless=True, profile_dir=profile)
        try:
            page = first_page(ctx)
            if not is_logged_in(ctx) or not check_login_status(page):
                raise RuntimeError(f"'{blog_id}' 계정 로그인이 필요합니다. [로그인] 버튼으로 다시 로그인해 주세요.")
            page.set_viewport_size({"width": 1400, "height": 950})
            pub = RichSmartEditorPublisher(page, blog_id=blog_id)
            page.goto(f"https://blog.naver.com/PostWriteForm.naver?blogId={blog_id}", wait_until="domcontentloaded",
                      timeout=30000)
            page.wait_for_timeout(3000)
            pub._resolve_editor_frame(timeout_sec=20.0)
            pub._dismiss_all_popups()
            f = pub.frame
            f.locator("button.se-moment-toolbar-button").first.click(timeout=8000)
            page.wait_for_timeout(3000)
            js = """() => [...document.querySelectorAll('.se-sidebar-element-moment')].map(e => {
                const a = e.querySelector('a.se-sidebar-moment-reference-link');
                const m = a && (a.getAttribute('href') || '').match(/\\/clip\\/(\\d+)/);
                const img = e.querySelector('img');
                return { id: m ? m[1] : '', title: (e.querySelector('.se-sidebar-moment-info-title') || {}).innerText || '',
                         date: (e.querySelector('.se-sidebar-moment-info-date') || {}).innerText || '',
                         thumb: img ? img.src : '' };
            })"""
            seen: Dict[str, Dict] = {}
            for _ in range(12):
                for c in f.evaluate(js):
                    if c["id"] and c["id"] not in seen:
                        seen[c["id"]] = {k: str(v)[:300] for k, v in c.items()}
                if len(seen) >= limit:
                    break
                grew = f.evaluate("() => { const l = document.querySelector('.se-sidebar-list-moment'); if (!l) return false;"
                                  " const t = l.scrollTop; l.scrollTop += 800; return l.scrollTop > t; }")
                page.wait_for_timeout(1000)
                if not grew:
                    break
            out = list(seen.values())[:limit]
            logger.info(f"🎞 내 클립 {len(out)}개 불러옴 ({blog_id})")
            return out
        finally:
            ctx.close()
