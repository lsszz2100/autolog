"""여행커넥트 자동화 (travel_connect.py) — 로그인된 계정 브라우저(Playwright)로 동작

- 상품 검색·상세·링크 발급: 브랜드 커넥트 크리에이터 화면이 쓰는 API (계정 쿠키 + 스페이스 번호)
- 상품 페이지(패키지 여행)에서 사진과 일정·포함/불포함 같은 확인된 사실을 모은다
- 티켓/패스·골프투어는 링크 발급·실적 인정 대상이 아니므로 뺀다
- 주의: 발급된 짧은 링크는 열지 않는다 (클릭 수 집계 우려). 상품 페이지는 상품 주소로 연다.
"""
import base64
import hashlib
import json
import logging
import random
import re
from typing import Dict, List, Optional, Tuple

from shopping_connect import ShoppingConnectError, creator_status, _open_account_browser, _is_fatal, _thumb_data_url

logger = logging.getLogger("naver.travel_connect")

GW = "https://gw-brandconnect.naver.com/affiliate"
SERVICE = "TRAVEL_PACKAGE"
PAGE_SIZE = 20
PRODUCT_URL_RE = re.compile(r"^https://pkgtour\.naver\.com/products/")
# 키워드 없이 고를 때 살펴볼 여행지
REGIONS = ["일본", "오사카", "후쿠오카", "삿포로", "도쿄", "베트남", "다낭", "나트랑", "푸꾸옥", "하노이", "태국", "방콕", "치앙마이",
           "필리핀", "세부", "보홀", "대만", "홍콩", "싱가포르", "발리", "괌", "사이판", "호주", "유럽", "스페인", "이탈리아",
           "미국", "캐나다", "중국", "제주", "부산", "강릉"]


class TravelConnectError(ShoppingConnectError):
    pass


def numeric_id(product_id: str) -> str:
    """여행 상품 번호(영문 포함)를 화면·예약이 쓰는 숫자 번호로 바꾼다 (같은 상품 = 같은 번호)."""
    return str(int(hashlib.md5(str(product_id).encode("utf-8")).hexdigest()[:13], 16))


class TravelApi:
    def __init__(self, page, blog_id: str):
        self.page, self.blog_id = page, blog_id
        self.space_id = None
        self.headers: Dict[str, str] = {}

    def open(self):
        st = creator_status(self.page, self.blog_id)
        if not st["enabled"] or not st["space_id"]:
            raise TravelConnectError(f"'{self.blog_id}' 계정은 브랜드 커넥트 크리에이터가 아닙니다. 가입 후 이용해 주세요.")
        self.space_id = st["space_id"]
        self.headers = {"Accept": "application/json", "x-space-id": str(self.space_id),
                        "Origin": "https://brandconnect.naver.com",
                        "Referer": f"https://brandconnect.naver.com/{self.space_id}/travel-connect/products"}
        return self

    def _get(self, path: str, params: Dict) -> Dict:
        try:
            r = self.page.context.request.get(f"{GW}/query/{path}", params={"serviceType": SERVICE, **params},
                                              headers=self.headers, timeout=20000)
        except Exception as e:      # 브라우저 오류 글에는 요청 쿠키가 들어 있어 그대로 남기지 않는다
            raise TravelConnectError(f"여행커넥트 서버에 연결하지 못했습니다 ({type(e).__name__}).") from None
        if r.status != 200:
            raise TravelConnectError(f"여행커넥트 API 오류 ({r.status}): {r.text()[:160]}")
        return r.json()

    def search_page(self, query: str, page_no: int = 1) -> Tuple[List[Dict], int]:
        d = self._get("connect/products", {"query": query, "page": max(1, int(page_no)), "size": PAGE_SIZE})
        return d.get("data") or [], int((d.get("paging") or {}).get("total") or 0)

    def detail(self, url: str) -> Dict:
        return self._get("connect/products/detail", {"productUrl": url})

    def issue(self, url: str) -> str:
        """링크 발급. 발급된 링크 주소를 돌려준다."""
        try:
            r = self.page.context.request.post(f"{GW}/command/connect-urls", data=json.dumps({"url": url}),
                                               headers={**self.headers, "Content-Type": "application/json"}, timeout=20000)
        except Exception as e:
            raise TravelConnectError(f"여행커넥트 링크 발급 요청에 실패했습니다 ({type(e).__name__}).") from None
        if r.status not in (200, 201):
            raise TravelConnectError(f"여행커넥트 링크를 발급하지 못했습니다 ({r.status}): {r.text()[:160]}")
        link = str((r.json() or {}).get("url") or "")
        if not link:
            raise TravelConnectError("여행커넥트 링크 발급 응답에 주소가 없습니다.")
        return link


def eligible(it: Dict) -> bool:
    """링크 발급·실적 인정 대상인 패키지/가이드투어만."""
    extra = it.get("extra") or {}
    return bool(PRODUCT_URL_RE.match(str(it.get("url") or ""))) and it.get("affiliateEnabled") is not False \
        and not extra.get("tourTicket") and "골프" not in str(it.get("name") or "")


def raw_from_item(it: Dict) -> Dict:
    extra = it.get("extra") or {}
    return {"kind": "travel", "affiliateProductId": numeric_id(it.get("productId")), "productId": str(it.get("productId") or "")[:80],
            "productName": re.sub(r"\s+", " ", str(it.get("name") or "")).strip()[:200], "storeName": str(it.get("storeName") or "")[:60],
            "productUrl": str(it.get("url") or "")[:400], "image": str(it.get("representativeProductImageUrl") or "")[:500],
            "salePrice": it.get("salePrice") or 0, "discountedSalePrice": it.get("discountedSalePrice") or 0,
            "discountedRate": it.get("discountedRate") or 0, "commissionRate": it.get("commissionRate") or 0,
            "shortenUrl": str(it.get("shortenUrl") or "")[:200], "startDate": str(extra.get("startDate") or "")[:10],
            "duration": str(extra.get("duration") or "")[:20], "productType": str(extra.get("productType") or "")[:20],
            "countries": [str(x)[:20] for x in (extra.get("countryNames") or [])][:6],
            "cities": [str(x)[:20] for x in (extra.get("cityNames") or [])][:8]}


def clean_raw(raw) -> Optional[Dict]:
    """화면에서 돌려받은 여행 상품 검사 (예약해 두었다가 글 쓸 때 다시 쓴다)."""
    if not isinstance(raw, dict) or raw.get("kind") != "travel" or not PRODUCT_URL_RE.match(str(raw.get("productUrl") or "")):
        return None
    if numeric_id(raw.get("productId")) != str(raw.get("affiliateProductId")):
        return None
    out = {k: raw.get(k) for k in ("kind", "affiliateProductId", "productId", "productName", "storeName", "productUrl", "image",
                                   "shortenUrl", "startDate", "duration", "productType")}
    for k in ("productId", "productName", "storeName", "productUrl", "image", "shortenUrl", "startDate", "duration", "productType"):
        out[k] = str(out.get(k) or "")[:400]
    for k in ("salePrice", "discountedSalePrice", "discountedRate", "commissionRate"):
        try:
            out[k] = float(raw.get(k) or 0)
        except (TypeError, ValueError):
            out[k] = 0
    for k in ("countries", "cities"):
        out[k] = [str(x)[:20] for x in raw.get(k) or []][:8] if isinstance(raw.get(k), list) else []
    return out


def product_from_raw(raw: Dict) -> Dict:
    price = raw.get("discountedSalePrice") or raw.get("salePrice")
    place = " ".join(raw.get("cities") or raw.get("countries") or [])
    return {"kind": "travel", "affiliate_product_id": str(raw["affiliateProductId"]), "name": raw.get("productName", "")[:120],
            "store": raw.get("storeName", ""), "price": f"{int(price):,}원" if price else "",
            "list_price": f"{int(raw['salePrice']):,}원" if raw.get("salePrice") else "",
            "discount_rate": raw.get("discountedRate") or 0, "commission": raw.get("commissionRate") or 0,
            "image": raw.get("image", ""), "product_url": raw.get("productUrl", ""), "url": raw.get("shortenUrl", ""),
            "start_date": raw.get("startDate", ""), "duration": raw.get("duration", ""), "place": place}


# ── 이미 쓴 상품 ──

def _used_file():
    from config import DATA_DIR
    return DATA_DIR / "travel_used.json"


def used_ids(blog_id: str) -> set:
    try:
        return set(json.loads(_used_file().read_text(encoding="utf-8")).get(blog_id, []))
    except Exception:
        return set()


def mark_used(blog_id: str, product_id: str):
    try:
        data = json.loads(_used_file().read_text(encoding="utf-8"))
    except Exception:
        data = {}
    ids = data.setdefault(blog_id, [])
    if product_id not in ids:
        ids.append(product_id)
    data[blog_id] = ids[-2000:]
    _used_file().write_text(json.dumps(data), encoding="utf-8")


# ── 검색·발급 ──

def _with_browser(profile, blog_id: str, what: str, fn):
    from playwright.sync_api import sync_playwright

    def run(pw, headless):
        ctx, pg = _open_account_browser(pw, profile, blog_id, headless=headless)
        try:
            return fn(TravelApi(pg, blog_id).open())
        finally:
            ctx.close()

    # 브랜드 커넥트 서버는 창 없는 브라우저 요청에 응답하지 않아 처음부터 창을 띄운다
    with sync_playwright() as pw:
        try:
            return run(pw, False)
        except TravelConnectError:
            raise
        except Exception as e:
            msg = str(e)
            if "세션이 만료" in msg or "다시 로그인" in msg:
                raise TravelConnectError(f"'{blog_id}' 계정의 로그인이 풀렸습니다. [로그인] 버튼으로 다시 로그인해 주세요.")
            if _is_fatal(msg):
                raise
            logger.warning("여행커넥트 %s 실패: %s", what, type(e).__name__)
            raise TravelConnectError(f"여행커넥트 {what}에 실패했습니다. 잠시 후 다시 시도해 주세요.") from None


def search_products(profile, blog_id: str, keyword: str, page: int = 1) -> Dict:
    """여행지로 상품 검색. 쇼핑커넥트 검색과 같은 모양으로 돌려준다."""
    from config import banned_keywords, find_banned
    page = max(1, int(page or 1))
    keyword = (keyword or "").strip() or REGIONS[0]
    items, total = _with_browser(profile, blog_id, "검색", lambda api: api.search_page(keyword, page))
    words, used, out = banned_keywords(), used_ids(blog_id), []
    for it in items:
        if not eligible(it) or find_banned(str(it.get("name") or ""), words):
            continue
        raw = raw_from_item(it)
        p = product_from_raw(raw)
        p["used"] = raw["affiliateProductId"] in used
        p["raw"] = raw
        out.append(p)
    total = max(total, len(items) + (page - 1) * PAGE_SIZE)
    pages = max(1, -(-total // PAGE_SIZE))
    logger.info(f"✈️ 여행커넥트 검색: '{keyword}' {page}/{pages}쪽, 전체 {total:,}개 중 {len(out)}개 표시")
    return {"items": out, "total": total, "page": page, "pages": pages, "mode": "search", "kind": "travel"}


def issue_links(profile, blog_id: str, urls) -> Dict[str, str]:
    """{상품 번호: 상품 주소} → {상품 번호: 발급 링크}."""
    want = {}
    for k, v in dict(urls or {}).items():
        if PRODUCT_URL_RE.match(str(v)):
            want[str(k)] = str(v)
    want = dict(list(want.items())[:24])
    if not want:
        raise TravelConnectError("링크를 발급할 여행 상품을 골라 주세요.")

    def work(api):
        out = {}
        for pid, url in want.items():
            try:
                d = api.detail(url)
                out[pid] = d.get("shortenUrl") or api.issue(url)
            except TravelConnectError as e:
                logger.warning(f"여행커넥트 링크 발급 불가 {pid}: {e}")
            api.page.wait_for_timeout(400)
        return out

    got = _with_browser(profile, blog_id, "링크 발급", work)
    logger.info(f"🔗 여행커넥트 링크 발급: {len(got)}/{len(want)}개")
    if not got:
        raise TravelConnectError("링크를 발급받지 못했습니다. 판매가 끝났거나 여행커넥트 대상이 아닌 상품일 수 있습니다.")
    return got


def _pick(api, keyword: str, exclude: set) -> Optional[Dict]:
    from config import banned_keywords, find_banned
    words = banned_keywords()
    queries = [q.strip() for q in re.split(r"[,\n]", keyword or "") if q.strip()] or REGIONS
    for _ in range(8):
        q = random.choice(queries)
        first, total = api.search_page(q, 1)
        pages = max(1, min(5, -(-total // PAGE_SIZE)))
        pg = random.randint(1, pages)
        batch = first if pg == 1 else api.search_page(q, pg)[0]
        pool = [i for i in batch if eligible(i) and numeric_id(i.get("productId")) not in exclude
                and not find_banned(str(i.get("name") or ""), words)]
        if pool:
            logger.info(f"🎲 여행커넥트 무작위 상품 선택: '{q}' 후보 {len(pool)}개 중 1개")
            return random.choice(pool)
    return None


# ── 상품 페이지 ──

_JS_IMAGES = """() => {
  const out = [];
  for (const i of document.images) {
    const r = i.getBoundingClientRect(), s = i.currentSrc || i.src || '';
    if (r.width >= 400 && r.height >= 220 && /pkgtour-phinf\\.pstatic\\.net/.test(s)) out.push(s.split('?')[0]);
  }
  return [...new Set(out)];
}"""


def page_facts(text: str) -> str:
    """상품 페이지 글자에서 요약·소개·포함/불포함·항공·쇼핑 같은 확인된 내용만 잘라 낸다."""
    t = re.sub(r"[ \t]+", " ", text or "")
    parts = []
    m = re.search(r"상품 요약 정보(.{0,200}?)출발일 변경", t, re.S)
    if m:
        parts.append("상품 요약: " + " / ".join(x.strip() for x in m.group(1).splitlines() if x.strip()))
    start = t.find("상세정보 더보기")
    start = t.find("상품 금액") if start < 0 else start + len("상세정보 더보기")
    if start >= 0:
        end = t.find("예약 및 결제 규정", start)
        body = t[start:end if end > 0 else start + 5000]
        body = re.sub(r"판매자 직거래 요구 주의 안내.*?문의해 주세요\.", "", body, flags=re.S)
        parts.append("\n".join(x.strip() for x in body.splitlines() if x.strip()))
    return "\n".join(parts)[:4500]


def capture_product(page, product_url: str, image_url: str = "", min_shots: int = 3, max_shots: int = 5) -> Tuple[List[bytes], str]:
    if not PRODUCT_URL_RE.match(product_url or ""):
        raise TravelConnectError(f"열 수 없는 여행 상품 주소입니다: {product_url}")
    try:
        page.set_viewport_size({"width": 1400, "height": 950})
    except Exception:
        pass
    page.goto(product_url, wait_until="domcontentloaded", timeout=40000, referer="https://brandconnect.naver.com/")
    page.wait_for_timeout(4000)
    for _ in range(7):                 # 지연 로딩 사진·소개 글을 불러오도록 내림
        page.mouse.wheel(0, 1300)
        page.wait_for_timeout(450)
    facts = page_facts(page.evaluate("() => document.body.innerText.slice(0, 30000)"))
    srcs = ([image_url.split("?")[0]] if image_url else []) + page.evaluate(_JS_IMAGES)
    shots, seen = [], set()
    for src in dict.fromkeys(srcs):
        if len(shots) >= max_shots:
            break
        try:
            r = page.context.request.get(src, headers={"Referer": "https://pkgtour.naver.com/"}, timeout=15000)
            if r.status != 200 or not r.headers.get("content-type", "").startswith("image/"):
                continue
            b = r.body()
            h = hashlib.md5(b).hexdigest()
            if h not in seen and len(b) > 15000:
                seen.add(h)
                shots.append(b)
        except Exception as e:
            logger.info(f"여행 상품 사진 받기 실패: {e}")
    logger.info(f"📸 여행 상품 사진 {len(shots)}장, 상품 정보 {len(facts)}자")
    return shots, facts


def prepare_product(profile, blog_id: str, api_key: str, keyword: str = "", product_id=None,
                    min_shots: int = 3, item: Optional[Dict] = None) -> Tuple[Dict, List[Dict]]:
    """여행 상품 하나를 준비한다: 선택 → 링크 발급 → 상품 페이지 사진·정보 → 사진 저장·설명."""
    import user_media
    from playwright.sync_api import sync_playwright
    exclude = used_ids(blog_id)
    with sync_playwright() as pw:
        ctx, page = _open_account_browser(pw, profile, blog_id)     # 상품 페이지는 창을 띄워 연다
        try:
            api = TravelApi(page, blog_id).open()
            raw = clean_raw(item) if item else None
            if not raw:
                if product_id:
                    raise TravelConnectError("고른 여행 상품 정보를 읽지 못했습니다. 상품을 다시 찾아 주세요.")
                it = _pick(api, keyword, exclude)
                if not it:
                    raise TravelConnectError((f"'{keyword}'(으)로 " if keyword else "") + "아직 쓰지 않은 여행 상품을 찾지 못했습니다.")
                raw = raw_from_item(it)
            d = api.detail(raw["productUrl"])
            if not eligible(d):
                raise TravelConnectError("이 상품은 판매가 끝났거나 여행커넥트 대상이 아니어서 링크를 발급할 수 없습니다. 다른 상품을 골라 주세요.")
            raw = raw_from_item(d)
            prod = product_from_raw(raw)
            prod["url"] = d.get("shortenUrl") or api.issue(raw["productUrl"])
            logger.info(f"✈️ 여행커넥트 상품: {prod['name'][:40]} (수수료 {prod['commission']}%, 링크 {prod['url']})")
            shots, facts = capture_product(page, prod["product_url"], prod["image"], min_shots=min_shots)
        finally:
            ctx.close()
    if len(shots) < min_shots:
        raise TravelConnectError(f"여행 상품 사진이 {len(shots)}장뿐입니다 (최소 {min_shots}장 필요).")
    prod["facts"] = facts
    photos = []
    for i, img in enumerate(shots, 1):
        meta = user_media.save_photo(base64.b64encode(img).decode("ascii"), f"{prod['name'][:40]} {i}")
        photos.append({"id": meta["id"], "desc": user_media.describe_photo(meta["id"], api_key), "thumb": _thumb_data_url(img)})
    mark_used(blog_id, prod["affiliate_product_id"])
    return prod, photos


def short_keyword(prod_or_name, hint: str = "") -> str:
    """제목·태그용 짧은 키워드: 검색 여행지 또는 '도시 + 패키지'."""
    hint = (hint or "").strip()
    if hint and len(hint) <= 15:
        return hint if "패키지" in hint or "여행" in hint else f"{hint} 패키지 여행"
    place = prod_or_name.get("place", "") if isinstance(prod_or_name, dict) else ""
    first = place.split()[0] if place else ""
    return f"{first} 패키지 여행" if first else "패키지 여행"
