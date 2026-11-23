"""쇼핑커넥트 글 지원 (shopping_service.py)

- 상품 링크 확인 + 상품명·가격·설명 자동 읽기 (스토어가 막으면 사용자가 직접 입력한 값만 사용)
- 원고에 넣을 상품 정보 프롬프트
- 본문에 수수료 안내 문구(맨 위)와 상품 링크 표시 줄 2개(도입 뒤·맺음말 앞) 배치
  → 발행 때 '[상품링크:주소]' 줄은 스마트에디터 링크 카드로 바뀐다 (publisher._insert_link_card)
"""
import html
import json
import logging
import re
import urllib.request
from typing import Dict, List, Optional

from text_style import is_subheading, sticker_mood

_PORTAL = "\ub124\uc774\ubc84"  # 포털 이름(한글), 소스에 상표명을 직접 쓰지 않음

logger = logging.getLogger("naver.shopping")

# 공정위 표시 문구 (사용자 지정 문구 그대로). 발행 때 본문 맨 아래 해시태그 바로 앞에 연한 회색 작은 글씨로 넣는다
DISCLOSURE = f"이 포스팅은 {_PORTAL} 쇼핑 커넥트 활동의 일환으로, 판매 발생 시 수수료를 제공받습니다."
TRAVEL_DISCLOSURE = f"이 포스팅은 {_PORTAL} 여행 커넥트 활동의 일환으로, 예약 발생 시 수수료를 제공받습니다."
_OLD_DISCLOSURES = (DISCLOSURE, TRAVEL_DISCLOSURE, "이 포스팅은 쇼핑커넥트 활동의 일환으로, 판매 발생 시 수수료를 제공받습니다.")


def is_disclosure(line: str) -> bool:
    return (line or "").strip() in _OLD_DISCLOSURES


def is_shopping_post(lines: List[str]) -> bool:
    return any(link_from_marker(l) or is_disclosure(l) for l in lines)


def disclosure_for(lines: List[str]) -> str:
    """글에 맞는 수수료 안내 문구 (여행 링크가 있으면 여행 커넥트 문구)."""
    travel = any((l or "").strip().startswith("[여행링크:") or (l or "").strip() == TRAVEL_DISCLOSURE for l in lines)
    return TRAVEL_DISCLOSURE if travel else DISCLOSURE


LINK_MARKER_RE = re.compile(r"^\[(?:상품|여행)링크:(https?://\S+)\]$")
# 공식 쇼핑커넥트 상품 카드 자리: [쇼핑커넥트:상품ID|링크|상품명] (카드를 못 넣으면 링크 카드로 대신)
CONNECT_MARKER_RE = re.compile(r"^\[쇼핑커넥트:(\d+)\|(https?://[^|\s]+)\|(.+)\]$")
MAX_PRODUCTS = 24

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/131.0.0.0 Safari/537.36")


def clean_url(url: str) -> str:
    """http(s) 주소만 허용, 앞뒤 공백·꺾쇠 제거. 아니면 빈 문자열."""
    u = str(url or "").strip().strip("<>").strip()
    return u[:500] if re.match(r"^https?://[^\s/]+\.[^\s]+$", u) else ""


def link_marker(url: str, product: Optional[dict] = None) -> str:
    if (product or {}).get("kind") == "travel":
        return f"[여행링크:{url}]"      # 여행 상품은 공식 카드가 없어 링크 카드로 넣는다
    pid = str((product or {}).get("affiliate_product_id") or "")
    if pid.isdigit():
        name = re.sub(r"[|\]\n]", " ", (product or {}).get("name", ""))[:120]
        return f"[쇼핑커넥트:{pid}|{url}|{name}]"
    return f"[상품링크:{url}]"


def connect_from_marker(line: str) -> Optional[Dict[str, str]]:
    m = CONNECT_MARKER_RE.match((line or "").strip())
    return {"id": m.group(1), "url": m.group(2), "name": m.group(3).strip()} if m else None


def link_from_marker(line: str) -> str:
    """상품 링크 자리 줄이면 링크 주소 (일반 링크 카드·공식 카드 둘 다)."""
    c = connect_from_marker(line)
    if c:
        return c["url"]
    m = LINK_MARKER_RE.match((line or "").strip())
    return m.group(1) if m else ""


def _meta(page: str, *names: str) -> str:
    for n in names:
        m = re.search(r'<meta[^>]+(?:property|name)=["\']' + re.escape(n) + r'["\'][^>]*content=["\']([^"\']*)', page, re.I) \
            or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']' + re.escape(n) + r'["\']', page, re.I)
        if m and m.group(1).strip():
            return html.unescape(m.group(1)).strip()
    return ""


def _jsonld_price(page: str) -> str:
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        for d in (data if isinstance(data, list) else [data]):
            offers = d.get("offers") if isinstance(d, dict) else None
            offers = offers[0] if isinstance(offers, list) and offers else offers
            if isinstance(offers, dict) and offers.get("price"):
                return str(offers["price"])
    return ""


def _clean_name(name: str) -> str:
    # 'XXX : 브랜드스토어' / 'XXX - 스마트스토어' 같은 꼬리 제거
    name = re.sub(r"\s*[:|\-]\s*(\ub124\uc774\ubc84\s*)?(스마트스토어|브랜드스토어|쇼핑|N\s*쇼핑|N\x41VER).*$", "", name or "").strip()
    return re.sub(r"\s+", " ", name)[:120]


def fetch_product(url: str, timeout: float = 8) -> Dict[str, str]:
    """링크 주소에서 상품명·가격·설명·대표 이미지를 읽는다 (실패하면 빈 값). 짧은 주소는 따라가서 최종 주소도 돌려준다."""
    out = {"final_url": "", "name": "", "price": "", "description": "", "image": ""}
    url = clean_url(url)
    if not url:
        return out
    if re.match(r"^https?://(naver\.me|([a-z0-9-]+\.)*brandconnect\.naver\.com)/", url, re.I):
        # 쇼핑커넥트 발급 링크는 열지 않는다 (클릭 수 집계 우려, 열어도 상품이 아닌 이동 페이지만 나옴)
        return out
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "ko-KR,ko;q=0.9"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out["final_url"] = r.geturl()
            page = r.read(800_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
    except Exception as e:
        logger.info(f"상품 정보 자동 읽기 실패 ({url[:60]}): {e}")
        return out
    name = _meta(page, "og:title", "twitter:title")
    if not name:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        name = html.unescape(m.group(1)).strip() if m else ""
    out["name"] = _clean_name(name)
    out["description"] = re.sub(r"\s+", " ", _meta(page, "og:description", "description"))[:400]
    out["image"] = _meta(page, "og:image")
    price = _meta(page, "product:price:amount", "og:price:amount") or _jsonld_price(page)
    if price and re.sub(r"[^\d]", "", price):
        out["price"] = f"{int(float(re.sub(r'[^0-9.]', '', price))):,}원"
    # 로그인·차단·오류 페이지 이름은 버린다
    if re.search(r"로그인|접근|차단|오류|error|captcha|denied|not found", out["name"], re.I):
        out["name"] = ""
    return out


def normalize_product(raw: dict, fetch: bool = True) -> Dict[str, str]:
    """화면에서 받은 상품 {url, name, memo} 정리 + 비어 있는 상품명은 링크에서 자동으로 채움.
    링크가 올바르지 않으면 ValueError."""
    raw = raw if isinstance(raw, dict) else {}
    url = clean_url(raw.get("url", ""))
    if not url:
        raise ValueError("쇼핑커넥트 상품 링크(https://...)를 입력해 주세요.")
    p = {"url": url,
         "name": re.sub(r"\s+", " ", str(raw.get("name", "") or "")).strip()[:120],
         "memo": str(raw.get("memo", "") or "").strip()[:3000],
         "price": re.sub(r"\s+", " ", str(raw.get("price", "") or "")).strip()[:40],
         "description": ""}
    if fetch:
        info = fetch_product(url)
        p["name"] = p["name"] or info["name"]
        p["price"] = p["price"] or info["price"]
        p["description"] = info["description"]
        if info["name"]:
            logger.info(f"🛒 상품 정보 자동 읽기: {info['name']}" + (f" / {info['price']}" if info["price"] else ""))
    if not p["name"]:
        raise ValueError("상품명을 자동으로 읽지 못했습니다. 상품명을 직접 입력해 주세요.")
    return p


def parse_product_lines(text: str) -> List[Dict[str, str]]:
    """자동 예약용 상품 목록: 한 줄에 '링크 | 상품명 | 메모' (상품명·메모는 생략 가능)."""
    items = []
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        if not line.strip():
            continue
        parts = [x.strip() for x in line.split("|")]
        url = clean_url(parts[0])
        if not url:
            raise ValueError(f"링크로 시작하지 않는 줄이 있습니다: {line.strip()[:40]}")
        items.append({"url": url, "name": parts[1] if len(parts) > 1 else "",
                      "memo": " | ".join(parts[2:]) if len(parts) > 2 else ""})
    if len(items) > MAX_PRODUCTS:
        raise ValueError(f"상품은 한 번에 {MAX_PRODUCTS}개까지 넣을 수 있습니다.")
    return items


def product_section(p: Dict[str, str]) -> str:
    """프롬프트 맨 앞에 넣는 '소개할 상품' 구역."""
    if p.get("kind") == "travel":
        return travel_section(p)
    out = "\n[소개할 상품 - 쇼핑커넥트 글]\n"
    out += f"- 상품명: {p.get('name', '')}\n"
    if p.get("store"):
        out += f"- 판매 스토어: {p['store']}\n"
    if p.get("price"):
        out += f"- 판매가(글 쓰는 시점에 확인한 값, 바뀔 수 있음): {p['price']}"
        if p.get("list_price") and p.get("discount_rate"):
            out += f" (정가 {p['list_price']}에서 {int(float(p['discount_rate']))}% 할인)"
        out += "\n"
    if p.get("rating"):
        out += f"- 구매자 평점: {p['rating']}점" + (f" (리뷰 {p['reviews']}건)" if p.get("reviews") else "") + "\n"
    if p.get("delivery"):
        out += f"- 배송: {p['delivery']}\n"
    if p.get("spec"):
        out += f"<상품 정보 표>\n{p['spec']}\n</상품 정보 표>\n"
    if p.get("image_facts"):
        out += f"<상품 이미지에 적힌 정보>\n{p['image_facts']}\n</상품 이미지에 적힌 정보>\n"
    if p.get("description"):
        out += f"- 판매 페이지 소개: {p['description']}\n"
    if p.get("memo"):
        out += f"<사용자가 적은 상품 특징>\n{p['memo']}\n</사용자가 적은 상품 특징>\n"
    out += """[상품 소개 글 규칙 - 가장 중요]
- 이 글은 위 상품 하나를 소개하는 글입니다. 제목·소제목·태그에는 짧은 메인 키워드를 쓰고, 긴 전체 상품명(모델명 포함)은 도입부에서 한 번만 정확히 쓰세요. 소제목마다 전체 상품명을 반복하지 마세요. 본문 전체가 이 상품을 중심으로 흘러가게 쓰세요.
- 상품 정보는 위에 적힌 것(상품 정보 표·상품 이미지에 적힌 정보 포함)과 상품명에서 확실히 알 수 있는 일반 사실만 쓰세요. 이미지에 적힌 특징(용량·성능·구조 등)은 본문의 핵심 특징으로 적극 활용하세요.
- 숫자(용량·크기·무게·전압·시간·개수 등)는 위 자료에 그대로 있는 것만 쓰세요. 자료에 없는 숫자는 절대 만들어 쓰지 마세요. 확인되지 않은 사양은 '구매 전 상세 페이지에서 확인'하라고 쓰세요.
- 사진 속 장면(배경·분위기·연기·색감·사람의 동작)을 묘사하거나 '~하는 모습처럼' 같은 비유를 쓰지 마세요. 장점이나 단점을 추측해서 만들지 마세요. 없는 사양·성분·수치·인증·수상 내역을 지어내지 마세요.
- 사용자가 적은 상품 특징이 있으면 그것을 중심 내용으로 쓰세요. 글쓴이가 직접 써 본 것처럼 말하는 체험담은 사용자 메모에 경험이 적혀 있을 때만 그 범위에서 쓰세요.
- 가격은 위에 적힌 값만 '글 쓰는 시점 기준'이라고 밝혀 쓰고, 할인율·최저가·품절 여부는 단정하지 마세요. 가격이 없으면 가격을 쓰지 마세요.
- 링크 주소, '아래 링크', '구매 링크' 같은 말은 본문에 쓰지 마세요. 링크는 프로그램이 알맞은 자리에 따로 넣습니다.
- 과장 광고 표현('무조건', '최고', '완벽', '100% 효과')과 의학적 효능 단정은 쓰지 마세요.
"""
    return out


def travel_section(p: Dict[str, str]) -> str:
    """여행 패키지 글의 상품 구역."""
    out = "\n[소개할 여행 상품 - 여행커넥트 글]\n"
    out += f"- 상품명: {p.get('name', '')}\n"
    for label, key in (("여행사", "store"), ("여행지", "place"), ("기간", "duration"), ("확인한 출발일", "start_date")):
        if p.get(key):
            out += f"- {label}: {p[key]}\n"
    if p.get("price"):
        out += f"- 1인 가격(글 쓰는 시점, 위 출발일 기준이며 바뀔 수 있음): {p['price']}\n"
    if p.get("facts"):
        out += f"<상품 페이지에 적힌 정보>\n{p['facts']}\n</상품 페이지에 적힌 정보>\n"
    if p.get("memo"):
        out += f"<사용자가 적은 상품 특징>\n{p['memo']}\n</사용자가 적은 상품 특징>\n"
    out += """[여행 상품 소개 글 규칙 - 가장 중요]
- 이 글은 위 여행 상품 하나를 소개하는 글입니다. 제목·소제목·태그에는 짧은 여행지 키워드를 쓰고, 긴 전체 상품명은 도입부에서 한 번만 정확히 쓰세요.
- 상품 하나만 소개하므로 제목·소제목에 '비교', '순위', 'TOP', '총정리', '여행사별' 같은 말을 쓰지 마세요.
- 일정·호텔·항공·식사·관광지·가격은 위 자료에 적힌 것만 쓰세요. 자료에 없는 숫자·시설·혜택은 절대 만들지 마세요.
- 자료에 있으면 불포함 비용(가이드·기사 경비, 개인 경비), 쇼핑 횟수, 선택 관광, 최소 출발 조건을 빠뜨리지 말고 '예약 전 확인할 점'에 분명히 쓰세요.
- 글쓴이가 직접 다녀온 것처럼 쓰지 마세요. 체험담은 사용자 메모에 경험이 적혀 있을 때만 그 범위에서 쓰세요.
- 가격과 출발일은 '글 쓰는 시점 기준'이라고 밝히고, 출발일마다 달라질 수 있으니 예약 화면에서 확인하라고 쓰세요. 최저가·마감 임박은 단정하지 마세요.
- 링크 주소, '아래 링크', '예약 링크' 같은 말은 본문에 쓰지 마세요. 링크는 프로그램이 알맞은 자리에 따로 넣습니다.
- 과장 표현('무조건', '최고', '완벽')과 안전·날씨 보장 표현은 쓰지 마세요.
"""
    return out


def _closing_index(lines: List[str]) -> int:
    """맺음말 시작 위치: 마지막 소제목 섹션의 마지막 문단 (그 앞 스티커 줄이 있으면 스티커 앞)."""
    heads = [i for i, l in enumerate(lines) if is_subheading(l)]
    start = heads[-1] + 1 if heads else 0
    paras = [i for i in range(start, len(lines)) if lines[i].strip() and not sticker_mood(lines[i])]
    if not paras:
        return len(lines)
    idx = paras[-1]
    if idx > 0 and sticker_mood(lines[idx - 1]):
        idx -= 1
    return idx


def place_links(lines: List[str], url: str, product: Optional[dict] = None) -> List[str]:
    """본문에 상품 링크 2곳(도입 뒤, 맺음말 앞). 이미 표시 줄이 있으면 그대로 둔다.
    수수료 안내 문구는 본문에 넣지 않고 발행 때 해시태그 바로 앞에 넣는다 (rich_publisher)."""
    url = clean_url(url)
    out = [l for l in lines if not is_disclosure(l)]
    if not url:
        return out
    if not any(link_from_marker(l) for l in out):
        heads = [i for i, l in enumerate(out) if is_subheading(l)]
        closing = _closing_index(out)
        before = [] if closing and not out[closing - 1].strip() else [""]
        out = out[:closing] + before + [link_marker(url, product), ""] + out[closing:]
        if heads and heads[0] > 0:
            first = heads[0]
            out = out[:first] + [link_marker(url, product), ""] + out[first:]
    while out and not out[0].strip():
        out.pop(0)
    return out
