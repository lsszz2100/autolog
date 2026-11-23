"""쇼핑커넥트 자동화 (shopping_connect.py) — 로그인된 계정 브라우저(Playwright page)로 동작

- 크리에이터 여부 확인: blog.naver.com/api/blogs/{id}/shopping-connect/creator-status
- 상품 검색·발급 목록·링크 발급: 스마트에디터의 쇼핑커넥트 패널이 쓰는 API (에디터 인증 헤더 필요)
- 상품 페이지 캡처: 대표 이미지 갤러리를 넘기며 3장 이상 + 상품 정보 글자 수집
  (상품 페이지는 창 없는(headless) 브라우저를 막으므로 창을 띄운 브라우저로 연다)
- 주의: 발급된 짧은 링크(naver.me / brandconnect)는 열지 않는다 — 클릭 수가 집계될 수 있음. 캡처는 productUrl 로.
"""
import hashlib
import json
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("naver.shopping_connect")

API = "https://platform.editor.naver.com/api/blogpc001/v1/shopping-connect/affiliates"


class ShoppingConnectError(RuntimeError):
    pass


def creator_status(page, blog_id: str) -> Dict:
    """{'enabled': bool, 'space_id': int|None}. 블로그 페이지(blog.naver.com)에서 호출."""
    if not re.match(r"^https://blog\.naver\.com/", page.url or ""):
        page.goto(f"https://blog.naver.com/{blog_id}", wait_until="domcontentloaded")
    txt = page.evaluate("async (b) => (await fetch('/api/blogs/' + b + '/shopping-connect/creator-status')).text()", blog_id)
    try:
        data = json.loads(txt)
        r = data["result"] or {}
    except Exception:
        raise ShoppingConnectError(f"쇼핑커넥트 가입 상태를 확인하지 못했습니다: {str(txt)[:120]}")
    return {"enabled": bool(r.get("affiliateEnabled")), "space_id": r.get("creatorSpaceId")}


class ConnectApi:
    """에디터를 한 번 열어 쇼핑커넥트 패널 요청의 인증 헤더를 얻은 뒤 API를 직접 부른다."""

    def __init__(self, page, blog_id: str):
        self.page, self.blog_id = page, blog_id
        self.headers: Dict[str, str] = {}
        self.space_id = None

    def open(self):
        st = creator_status(self.page, self.blog_id)
        if not st["enabled"]:
            raise ShoppingConnectError(f"'{self.blog_id}' 계정은 쇼핑커넥트 크리에이터가 아닙니다. 쇼핑커넥트 가입 후 이용해 주세요.")
        self.space_id = st["space_id"]
        got = {}

        def on_req(req):
            if "/shopping-connect/affiliates/" in req.url and "se-authorization" in req.headers:
                got.update({k: v for k, v in req.headers.items() if k.lower() in ("se-authorization", "se-app-id")})
        self.page.on("request", on_req)
        try:
            from publisher import SmartEditorPublisher
            pub = SmartEditorPublisher(self.page, self.blog_id)
            try:
                self.page.set_viewport_size({"width": 1400, "height": 950})   # 좁으면 툴바 버튼이 '더보기'로 숨는다
            except Exception:
                pass
            self.page.goto(f"https://blog.naver.com/PostWriteForm.naver?blogId={self.blog_id}", wait_until="domcontentloaded")
            f = pub._resolve_editor_frame()
            pub.frame = f
            pub._dismiss_all_popups()
            btn = f.locator("button.se-shopping-connect-toolbar-button").first
            try:
                btn.click(timeout=15000)
            except Exception:
                pub._save_diagnostic("shopping_connect_button")
                raise ShoppingConnectError("글쓰기 화면에서 [쇼핑커넥트] 버튼을 찾지 못했습니다.")
            for _ in range(30):
                if got:
                    break
                self.page.wait_for_timeout(300)
            try:
                f.locator(".se-popup-shopping-connect button.se-popup-close-button").first.click(timeout=2000)
            except Exception:
                pass
        finally:
            self.page.remove_listener("request", on_req)
        if not got:
            raise ShoppingConnectError("쇼핑커넥트 패널 인증 정보를 받지 못했습니다.")
        self.headers = {**got, "Accept": "application/json",
                        "Referer": f"https://blog.naver.com/PostWriteForm.naver?blogId={self.blog_id}"}
        return self

    def _get(self, path: str, params: Dict) -> Dict:
        params = {**params, "creatorSpaceId": self.space_id}
        try:
            r = self.page.context.request.get(f"{API}/{path}", params=params, headers=self.headers, timeout=20000)
        except Exception as e:      # 브라우저 오류 글에는 요청 쿠키가 들어 있어 그대로 남기지 않는다
            raise ShoppingConnectError(f"쇼핑커넥트 서버에 연결하지 못했습니다 ({type(e).__name__}).") from None
        if r.status != 200:
            raise ShoppingConnectError(f"쇼핑커넥트 API 오류 ({r.status}): {r.text()[:200]}")
        return r.json()

    # 한 번에 최대 50개(API 제한). s = 페이지 번호(1부터), 응답의 total = 전체 상품 수
    def search_page(self, query: str, page_no: int = 1, count: int = 20) -> Tuple[List[Dict], int]:
        d = self._get("items", {"query": query, "s": max(1, int(page_no)), "count": max(1, min(50, count))})
        return d.get("items") or [], int(d.get("total") or 0)

    def issued_page(self, page_no: int = 1, count: int = 20) -> Tuple[List[Dict], int]:
        d = self._get("issued-items", {"s": max(1, int(page_no)), "count": max(1, min(50, count))})
        return d.get("items") or [], int(d.get("total") or 0)

    def search(self, query: str, page_no: int = 1, count: int = 20) -> List[Dict]:
        return self.search_page(query, page_no, count)[0]

    def issued(self, page_no: int = 1, count: int = 20) -> List[Dict]:
        return self.issued_page(page_no, count)[0]

    def issue(self, affiliate_product_id) -> Dict:
        """링크 발급 (이미 발급한 상품이면 기존 링크). {'shortenUrl', 'affiliateUrl', ...}"""
        try:
            return self._get("issue", {"affiliateProductId": affiliate_product_id})
        except ShoppingConnectError as e:
            if "(404)" in str(e):
                raise ShoppingConnectError("이 상품은 판매가 끝났거나 쇼핑커넥트 대상에서 빠져 링크를 발급할 수 없습니다. "
                                           "다른 상품을 골라 주세요.") from None
            raise


def product_from_item(it: Dict) -> Dict:
    """API 상품 항목 → 글쓰기용 상품 정보."""
    price = it.get("discountedSalePrice") or it.get("salePrice")
    out = {
        "affiliate_product_id": it.get("affiliateProductId"),
        "channel_product_no": it.get("channelProductNo"),
        "name": re.sub(r"\s+", " ", str(it.get("productName") or "")).strip()[:120],
        "store": str(it.get("storeName") or "")[:60],
        "price": f"{int(price):,}원" if price else "",
        "list_price": f"{int(it['salePrice']):,}원" if it.get("salePrice") else "",
        "discount_rate": it.get("discountedRate") or 0,
        "commission": it.get("commissionRate") or 0,
        "image": it.get("representativeProductImageUrl") or "",
        "product_url": it.get("productUrl") or "",
        "url": it.get("shortenAffiliateUrl") or it.get("shortenUrl") or "",
    }
    return out


_RAW_KEYS = ("affiliateProductId", "commissionRate", "promotionCommissionRate", "storeName", "brandStore", "channelProductNo",
             "productName", "salePrice", "discountedSalePrice", "discountedRate", "representativeProductImageUrl",
             "productUrl", "shortenAffiliateUrl", "shortenUrl")


def clean_raw_item(raw) -> Optional[Dict]:
    """화면에서 돌려받은 API 상품 항목 검사 (예약해 두었다가 나중에 글을 쓸 때 다시 검색하지 않으려고 보관).
    상품 번호가 숫자이고 상품 주소가 N사 쇼핑 주소일 때만 쓴다."""
    if isinstance(raw, dict) and raw.get("kind") == "travel":
        import travel_connect
        return travel_connect.clean_raw(raw)
    if not isinstance(raw, dict) or not str(raw.get("affiliateProductId") or "").isdigit():
        return None
    url = str(raw.get("productUrl") or "")
    if not re.match(r"^https://([a-z0-9-]+\.)*naver\.(com|me)/", url):
        return None
    out = {k: raw.get(k) for k in _RAW_KEYS if raw.get(k) not in (None, "")}
    out["affiliateProductId"] = int(raw["affiliateProductId"])
    for k in ("productName", "storeName"):
        out[k] = str(out.get(k) or "")[:200]
    for k in ("commissionRate", "promotionCommissionRate", "salePrice", "discountedSalePrice", "discountedRate"):
        try:
            out[k] = float(out[k]) if k in out else 0
        except (TypeError, ValueError):
            out[k] = 0
    return out


# ── 상품 페이지 캡처 ──
# 좌표로 자르지 않고 이미지 요소 자체를 찍는다 (페이지가 늦게 그려져 위치가 바뀌어도 정확).

_JS_MARK_MAIN = """() => {
  // 페이지 위쪽·왼쪽 절반의 가장 큰 상품 이미지 = 대표 이미지 (화면에 실제로 보이는 것만)
  document.querySelectorAll('[data-nbs-main]').forEach(e => e.removeAttribute('data-nbs-main'));
  let best = null, area = 0;
  for (const i of document.images) {
    const r = i.getBoundingClientRect(), top = r.top + scrollY;
    if (r.width < 250 || r.height < 250 || top > 1500 || r.left > innerWidth * 0.5) continue;
    const st = getComputedStyle(i);
    if (st.visibility === 'hidden' || st.opacity === '0') continue;
    const cx = r.left + r.width / 2, cy = Math.min(r.top + r.height / 2, innerHeight - 5);
    const hit = document.elementFromPoint(cx, cy);
    if (!hit || !(hit === i || hit.contains(i) || i.parentElement.contains(hit))) continue;   // 넘김 갤러리의 가려진 슬라이드 제외
    if (r.width * r.height > area) { area = r.width * r.height; best = i; }
  }
  if (!best) return false;
  best.setAttribute('data-nbs-main', '1');
  return true;
}"""

_JS_MARK_THUMBS = """() => {
  const m = document.querySelector('[data-nbs-main]'); if (!m) return 0;
  const R = m.getBoundingClientRect();
  const th = [...document.images].filter(i => { const r = i.getBoundingClientRect();
    return r.width >= 30 && r.width <= 130 && r.height >= 30 && r.height <= 130
      && r.top > R.bottom - 10 && r.top < R.bottom + 220 && r.left > R.left - 80 && r.right < R.right + 80; });
  th.forEach((t, k) => t.setAttribute('data-nbs-thumb', String(k)));
  return th.length;
}"""

_JS_MARK_DETAIL = """(skip) => {
  // 상세 설명의 판매자 상품 이미지 (포털 배너·숏클립 제외: 판매자 이미지 서버 shop-phinf 만)
  const out = [];
  for (const i of document.images) {
    const r = i.getBoundingClientRect(), src = i.currentSrc || i.src || '';
    if (r.width >= 500 && r.height >= 300 && /shop-phinf\\.pstatic\\.net/.test(src) && r.top + scrollY > skip) {
      i.setAttribute('data-nbs-detail', String(out.length)); out.push(src);
    }
  }
  return out.length;
}"""


_JS_GALLERY_SRCS = """() => {
  // 대표 이미지 갤러리: 페이지 위쪽(1500px 안)의 판매자 상품 이미지들 — 큰 슬라이드(화면 밖 포함)와 썸네일.
  // 썸네일 주소는 같은 원본의 작은 판이므로 크기 옵션(?type=)을 떼면 원본을 받을 수 있다.
  const items = [];
  for (const i of document.images) {
    const r = i.getBoundingClientRect(), top = r.top + scrollY, src = i.currentSrc || i.src || '';
    if (top > 1500 || r.width < 36 || r.height < 36 || !/shop-phinf\\.pstatic\\.net/.test(src)) continue;
    const ratio = r.width / r.height;
    if (ratio > 1.8 || ratio < 0.55) continue;                 // 가로로 긴 로고·띠 배너 제외
    items.push({src: src.split('?')[0], big: r.width >= 300, x: r.left, y: top});
  }
  items.sort((a, b) => (b.big - a.big) || (a.y - b.y) || (a.x - b.x));
  return [...new Set(items.map(i => i.src))];
}"""


def _download_images(page, srcs: List[str], referer: str, limit: int) -> List[bytes]:
    out = []
    for src in srcs:
        if len(out) >= limit:
            break
        try:
            r = page.context.request.get(src, headers={"Referer": referer}, timeout=15000)
            if r.status == 200 and (r.headers.get("content-type", "").startswith("image/")):
                out.append(r.body())
        except Exception as e:
            logger.info(f"상품 이미지 받기 실패: {e}")
    return out


def _wait_loaded(page):
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(1500)


def capture_product(page, product_url: str, min_shots: int = 3, max_shots: int = 5) -> Tuple[List[bytes], Dict[str, str]]:
    """상품 페이지에서 대표 이미지 갤러리를 썸네일로 넘기며 캡처 (최소 min_shots장, 모자라면 상세 설명 이미지로 채움).
    반환: (PNG 바이트 목록, 확인된 사실 dict)."""
    if not re.match(r"^https://(smartstore|brand|m\.smartstore)\.naver\.com/", product_url or ""):
        raise ShoppingConnectError(f"캡처할 수 없는 상품 주소입니다: {product_url}")
    try:
        page.set_viewport_size({"width": 1400, "height": 950})
    except Exception:
        pass
    page.goto(product_url, wait_until="domcontentloaded", timeout=40000, referer="https://blog.naver.com/")
    _wait_loaded(page)
    body = page.evaluate("() => document.body.innerText.slice(0, 600)")
    if re.search(r"접속이 불가|접속이 일시적으로 제한|에러페이지", page.title() + body):
        raise ShoppingConnectError("상품 페이지가 열리지 않았습니다 (접속 제한).")
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    facts = _page_facts(page)
    shots, seen = [], set()

    def add(png: bytes):
        h = hashlib.md5(png).hexdigest()
        if h not in seen and len(png) > 15000:      # 같은 사진·빈 사진 제외
            seen.add(h)
            shots.append(png)

    def shoot(sel: str):
        try:
            el = page.locator(sel).first
            el.scroll_into_view_if_needed(timeout=3000)
            page.wait_for_timeout(500)
            add(el.screenshot(timeout=5000, animations="disabled"))
        except Exception as e:
            logger.info(f"캡처 건너뜀 ({sel}): {e}")

    # 1) 대표 이미지 갤러리 슬라이드 원본 (가장 정확·고화질)
    for png in _download_images(page, page.evaluate(_JS_GALLERY_SRCS), product_url, max_shots):
        add(png)
    # 2) 모자라면 화면에 보이는 대표 이미지를 썸네일로 넘기며 캡처
    if len(shots) < min_shots and page.evaluate(_JS_MARK_MAIN):
        shoot("[data-nbs-main='1']")
        n = page.evaluate(_JS_MARK_THUMBS)
        for k in range(1, n):
            if len(shots) >= max_shots:
                break
            try:
                page.locator(f"[data-nbs-thumb='{k}']").first.click(timeout=2000)
            except Exception:
                continue
            page.wait_for_timeout(1000)
            if page.evaluate(_JS_MARK_MAIN):
                shoot("[data-nbs-main='1']")
    # 모자라면 상세 설명 이미지 (판매자 상품 이미지)
    if len(shots) < min_shots:
        try:
            page.get_by_text("상세정보", exact=True).first.click(timeout=3000)
        except Exception:
            page.mouse.wheel(0, 1500)
        page.wait_for_timeout(1500)
        for _ in range(8):                       # 지연 로딩 이미지를 불러오도록 조금씩 내림
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(500)
        n = page.evaluate(_JS_MARK_DETAIL, 1200)
        for k in range(n):
            if len(shots) >= min_shots + 1:
                break
            try:
                el = page.locator(f"[data-nbs-detail='{k}']").first
                el.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(600)
                box = el.bounding_box()
                vp = page.viewport_size
                if not box:
                    continue
                # 아주 긴 상세 이미지는 화면에 보이는 위쪽 부분만
                y = max(box["y"], 0)
                h = min(box["height"], vp["height"] - y, box["width"] * 1.3)
                if h >= 300:
                    add(page.screenshot(clip={"x": box["x"], "y": y, "width": box["width"], "height": h}))
            except Exception:
                continue
    logger.info(f"📸 상품 페이지 캡처 {len(shots)}장" + (f" (평점 {facts['rating']}, 리뷰 {facts.get('reviews', '?')}건)" if facts.get("rating") else ""))
    return shots, facts


def _page_facts(page) -> Dict[str, str]:
    """상품 페이지 글자에서 평점·리뷰 수·배송 같은 확인된 사실만 뽑는다."""
    text = page.evaluate("() => document.body.innerText.slice(0, 20000)")
    facts = {}
    m = re.search(r"평점\s*\n?\s*([0-5]\.\d{1,2})", text)
    if m:
        facts["rating"] = m.group(1)
    m = re.search(r"([\d,]+)\s*건\s*리뷰", text)
    if m:
        facts["reviews"] = m.group(1)
    if "무료배송" in text:
        facts["delivery"] = "무료배송"
    # 상품정보 제공고시 등 표 형태 정보 (항목: 값)
    rows = page.evaluate("""() => [...document.querySelectorAll('table tr')].map(tr => [...tr.children].map(c => c.innerText.trim()))
        .filter(c => c.length >= 2 && c[0].length < 30 && c[1] && c[1].length < 120).slice(0, 30)""")
    info = []
    for cells in rows:
        for i in range(0, len(cells) - 1, 2):
            k, v = cells[i], cells[i + 1]
            if k and v and not re.search(r"상세페이지 참조|판매자|사업자|통신판매|고객센터|전화|주소|이메일", k + v):
                info.append(f"{k}: {v}")
    if info:
        facts["spec"] = "\n".join(dict.fromkeys(info))[:1500]
    return facts


# ── 계정 브라우저로 상품 준비 (선택 → 링크 발급 → 캡처 → 사진 저장) ──

def _used_file():
    from config import DATA_DIR
    return DATA_DIR / "shopping_used.json"


def used_ids(blog_id: str) -> set:
    try:
        return set(json.loads(_used_file().read_text(encoding="utf-8")).get(blog_id, []))
    except Exception:
        return set()


def mark_used(blog_id: str, product_id):
    try:
        data = json.loads(_used_file().read_text(encoding="utf-8"))
    except Exception:
        data = {}
    ids = data.setdefault(blog_id, [])
    if product_id not in ids:
        ids.append(product_id)
    data[blog_id] = ids[-2000:]
    _used_file().write_text(json.dumps(data), encoding="utf-8")


# 키워드 없이 고를 때 살펴볼 쇼핑 분야 (쇼핑커넥트 검색은 검색어가 꼭 있어야 해서 넓은 분야 이름으로 찾는다)
BROAD_QUERIES = ["주방용품", "생활용품", "청소용품", "수납정리", "욕실용품", "침구", "인테리어소품", "조명", "주방가전", "생활가전",
                 "계절가전", "음향기기", "휴대폰 액세서리", "컴퓨터 주변기기", "스킨케어", "헤어케어", "바디케어", "건강식품", "영양제",
                 "다이어트식품", "간편식", "커피", "차", "간식", "캠핑용품", "등산용품", "여행용품", "운동용품", "홈트레이닝", "자전거용품",
                 "유아용품", "육아용품", "완구", "반려동물용품", "문구", "가방", "지갑", "신발", "모자", "차량용품", "공구", "원예용품"]


def _usable(items: List[Dict], exclude: set) -> List[Dict]:
    from config import banned_keywords, find_banned
    words = banned_keywords()
    ex = {str(x) for x in exclude}
    return [i for i in items if str(i.get("affiliateProductId")) not in ex and i.get("productUrl")
            and not find_banned(i.get("productName") or "", words)]


def pick_random(api, keyword: str, exclude: set) -> Optional[Dict]:
    """아직 쓰지 않은 상품 중 무작위로 하나. keyword(쉼표로 여러 개 가능)가 있으면 그 분야에서,
    없으면 링크 발급한 상품 + 넓은 쇼핑 분야에서 고른다. 분야·쪽도 무작위로 바꿔 가며 최대 8번 찾는다."""
    import random
    queries = [q.strip() for q in re.split(r"[,\n]", keyword or "") if q.strip()] or BROAD_QUERIES
    pool: List[Dict] = []
    if not keyword:
        try:
            pool += _usable(api.issued_page(1, 50)[0], exclude)
        except Exception as e:
            logger.warning(f"쇼핑커넥트 발급 목록 읽기 실패: {e}")
    tried = set()
    for _ in range(8):
        q = random.choice(queries)
        first, total = api.search_page(q, 1, 50)
        pages = max(1, min(10, -(-total // 50)))       # 앞 500개 안에서 (뒤쪽은 판매가 적은 상품이 많다)
        pg = random.randint(1, pages)
        if (q, pg) in tried:
            continue
        tried.add((q, pg))
        batch = first if pg == 1 else api.search_page(q, pg, 50)[0]
        pool += _usable(batch, exclude)
        if pool:
            break
    if not pool:
        return None
    pick = random.choice(pool)
    logger.info(f"🎲 쇼핑커넥트 무작위 상품 선택: '{q}' 분야 등 후보 {len(pool)}개 중 1개")
    return pick


def pick_product(items: List[Dict], exclude: set) -> Optional[Dict]:
    """아직 쓰지 않은 상품 중 수수료가 높은 것 → 같으면 할인율이 높은 것. 이름에 금지 키워드가 든 상품은 뺀다."""
    from config import banned_keywords, find_banned
    words = banned_keywords()
    cands = [i for i in items if i.get("affiliateProductId") not in exclude and i.get("productUrl")
             and not find_banned(i.get("productName") or "", words)]
    cands.sort(key=lambda i: (-(i.get("commissionRate") or 0), -(i.get("discountedRate") or 0)))
    return cands[0] if cands else None


def _open_account_browser(pw, profile, blog_id, headless=False):
    from browser import launch_naver_context, first_page
    from auth import is_logged_in, check_login_status
    # 상품 페이지는 창 없는 브라우저를 막으므로 사진 수집 때는 창을 띄운다 (검색만 할 때는 headless)
    ctx = launch_naver_context(pw, headless=headless, profile_dir=profile)
    page = first_page(ctx)
    if not is_logged_in(ctx) or not check_login_status(page):
        ctx.close()
        raise ShoppingConnectError(f"'{blog_id}' 계정 로그인이 필요합니다. 화면의 [로그인] 버튼으로 다시 로그인해 주세요.")
    return ctx, page


PAGE_SIZE = 20


def _is_fatal(msg: str) -> bool:
    """창을 띄워 다시 해 봐도 안 되는 실패 (가입 안 됨·로그인 풀림)."""
    return any(w in msg for w in ("크리에이터가 아닙니다", "로그인이 필요합니다", "세션이 만료", "다시 로그인"))


def search_products(profile, blog_id: str, keyword: str, page: int = 1) -> Dict:
    """계정의 쇼핑커넥트 상품 검색 (키워드 없으면 링크 발급한 상품 목록). 한 쪽에 20개씩.
    반환 {'items': [화면 목록용 상품 정보], 'total': 전체 수, 'page': 쪽, 'pages': 전체 쪽 수, 'mode': 'search'|'issued'}"""
    from playwright.sync_api import sync_playwright
    page = max(1, int(page or 1))

    def run(pw, headless):
        ctx, pg = _open_account_browser(pw, profile, blog_id, headless=headless)
        try:
            api = ConnectApi(pg, blog_id).open()
            return api.search_page(keyword, page, PAGE_SIZE) if keyword else api.issued_page(page, PAGE_SIZE)
        finally:
            ctx.close()

    with sync_playwright() as pw:
        try:
            items, total = run(pw, True)              # 검색은 창 없이 (글쓰기 화면이 보이지 않게)
        except Exception as e:
            msg = str(e)
            if "세션이 만료" in msg or "다시 로그인" in msg:
                raise ShoppingConnectError(f"'{blog_id}' 계정의 로그인이 풀렸습니다. 이 계정을 고른 상태에서 [로그인] 버튼으로 다시 로그인한 뒤 상품을 찾아 주세요.")
            if _is_fatal(msg):
                raise ShoppingConnectError(msg) if not isinstance(e, ShoppingConnectError) else e
            logger.warning("쇼핑커넥트 검색 창 없이 실패, 창을 띄워 재시도: %s", type(e).__name__)
            items, total = run(pw, False)
    from config import banned_keywords, find_banned
    words = banned_keywords()
    used = used_ids(blog_id)
    out = []
    for it in items:
        p = product_from_item(it)
        if find_banned(p.get("name", ""), words):      # 금지 키워드가 든 상품은 목록에서도 뺀다
            continue
        p["used"] = it.get("affiliateProductId") in used
        p["raw"] = clean_raw_item(it)                   # 고른 상품을 다시 검색하지 않고 바로 쓰려고 함께 돌려준다
        p["affiliate_product_id"] = str(p["affiliate_product_id"])   # JS 숫자 정밀도(16자리) 손실 방지
        if p["raw"]:
            p["raw"]["affiliateProductId"] = p["affiliate_product_id"]
        out.append(p)
    total = max(total, len(items) + (page - 1) * PAGE_SIZE)
    pages = max(1, -(-total // PAGE_SIZE))
    logger.info(f"🛒 쇼핑커넥트 {'검색' if keyword else '발급 목록'}: '{keyword}' {page}/{pages}쪽, 전체 {total:,}개 중 {len(out)}개 표시")
    return {"items": out, "total": total, "page": page, "pages": pages, "mode": "search" if keyword else "issued"}


def issue_links(profile, blog_id: str, product_ids: List[str]) -> Dict[str, str]:
    """화면 목록에서 고른 상품들의 쇼핑커넥트 링크 발급 (이미 발급한 상품은 기존 링크). 반환 {상품 번호: 링크}"""
    from playwright.sync_api import sync_playwright
    ids = [str(i) for i in product_ids if str(i).isdigit()][:24]
    if not ids:
        raise ShoppingConnectError("링크를 발급할 상품을 골라 주세요.")

    def run(pw, headless):
        ctx, pg = _open_account_browser(pw, profile, blog_id, headless=headless)
        try:
            api = ConnectApi(pg, blog_id).open()
            out = {}
            for pid in ids:
                try:
                    d = api.issue(int(pid))
                except ShoppingConnectError as e:
                    if "발급할 수 없습니다" not in str(e):
                        raise
                    logger.warning(f"쇼핑커넥트 링크 발급 불가 상품 {pid}: {e}")
                    continue
                url = d.get("shortenUrl") or d.get("shortenAffiliateUrl") or ""
                if url:
                    out[pid] = url
                pg.wait_for_timeout(400)          # 너무 빠르게 연달아 요청하지 않게
            return out
        finally:
            ctx.close()

    with sync_playwright() as pw:
        try:
            got = run(pw, True)
        except Exception as e:
            msg = str(e)
            if "세션이 만료" in msg or "다시 로그인" in msg:
                raise ShoppingConnectError(f"'{blog_id}' 계정의 로그인이 풀렸습니다. 이 계정을 고른 상태에서 [로그인] 버튼으로 다시 로그인한 뒤 해 주세요.")
            if _is_fatal(msg):
                raise ShoppingConnectError(msg) if not isinstance(e, ShoppingConnectError) else e
            logger.warning("쇼핑커넥트 링크 발급 창 없이 실패, 창을 띄워 재시도: %s", type(e).__name__)
            got = run(pw, False)
    logger.info(f"🔗 쇼핑커넥트 링크 발급: {len(got)}/{len(ids)}개 " + ", ".join(got.values()))
    if not got:
        raise ShoppingConnectError("링크를 발급받지 못했습니다. 판매가 끝났거나 쇼핑커넥트 대상에서 빠진 상품일 수 있습니다. 다른 상품을 골라 주세요.")
    return got


CONNECT_LINK_RE = re.compile(r"^https?://(naver\.me|([a-z0-9-]+\.)*brandconnect\.naver\.com)/", re.I)


def is_connect_link(url: str) -> bool:
    """쇼핑커넥트로 발급한 짧은 링크인지 (이런 링크는 프로그램이 열지 않는다 — 클릭 수가 집계될 수 있음)."""
    return bool(CONNECT_LINK_RE.match(str(url or "").strip()))


def _norm_link(url: str) -> str:
    u = re.sub(r"^https?://", "", str(url or "").strip().lower()).split("?")[0].split("#")[0]
    return u.rstrip("/")


def lookup_links(profile, blog_id: str, urls: List[str]) -> Dict[str, Dict]:
    """발급한 쇼핑커넥트 링크 → 상품 정보. 링크를 열지 않고 이 계정의 '발급한 상품 목록'에서 같은 링크를 찾는다.
    반환 {입력 링크: 화면용 상품 정보(raw 포함)} — 못 찾은 링크는 빠진다."""
    from playwright.sync_api import sync_playwright
    want = {_norm_link(u): u for u in urls if u}
    found: Dict[str, Dict] = {}
    if not want:
        return found

    def run(pw, headless):
        ctx, pg = _open_account_browser(pw, profile, blog_id, headless=headless)
        try:
            api = ConnectApi(pg, blog_id).open()
            for n in range(1, 41):                 # 50개씩 최대 2,000개
                batch, total = api.issued_page(n, 50)
                for it in batch:
                    for k in ("shortenAffiliateUrl", "shortenUrl", "affiliateUrl"):
                        key = _norm_link(it.get(k))
                        if key in want and want[key] not in found:
                            found[want[key]] = it
                if len(found) == len(want) or not batch or n * 50 >= total:
                    break
        finally:
            ctx.close()

    with sync_playwright() as pw:
        try:
            run(pw, True)
        except Exception as e:
            msg = str(e)
            if "세션이 만료" in msg or "다시 로그인" in msg:
                raise ShoppingConnectError(f"'{blog_id}' 계정의 로그인이 풀렸습니다. [로그인] 버튼으로 다시 로그인해 주세요.")
            if _is_fatal(msg):
                raise
            logger.warning("쇼핑커넥트 링크 찾기 창 없이 실패, 창을 띄워 재시도: %s", type(e).__name__)
            run(pw, False)
    out = {}
    for u, it in found.items():
        p = product_from_item(it)
        p["affiliate_product_id"] = str(p["affiliate_product_id"])
        p["url"] = u
        p["raw"] = clean_raw_item(it)
        if p["raw"]:
            p["raw"]["affiliateProductId"] = p["affiliate_product_id"]
        out[u] = p
    logger.info(f"🔗 쇼핑커넥트 링크 {len(want)}개 중 {len(out)}개를 발급 목록에서 찾음")
    return out


def image_facts(api_key: str, paths: List[str]) -> str:
    """상품 이미지들에 적힌 문구·사양만 뽑는다 (장면 묘사 없이). 원고의 사실 자료로 쓴다. 실패하면 빈 문자열."""
    import base64
    import gemini_client
    from user_media import VISION_MODELS
    imgs = [("image/jpeg", base64.b64encode(open(p, "rb").read()).decode("ascii")) for p in paths if p][:6]
    if not imgs or len((api_key or "").strip()) < 10:
        return ""
    prompt = ("다음은 한 상품의 판매 페이지 이미지들입니다. 이미지 안에 글자로 적혀 있는 상품 정보(제품명·용량·크기·성능 수치·소재·구성품·기능·특징 문구)만 "
              "한국어로 '- 항목' 줄 목록으로 정리하세요. 이미지에 글자로 적혀 있지 않은 것은 쓰지 마세요. 배경·분위기·사람·색감 같은 장면 묘사, 추측, 광고 수식어는 빼세요. "
              "글자로 된 정보가 하나도 없으면 '없음'이라고만 답하세요.")
    try:
        text, _ = gemini_client.generate(api_key, prompt, VISION_MODELS, images=imgs,
                                         generation_config={"temperature": 0.1, "maxOutputTokens": 800},
                                         timeout=60, accept=lambda t: t.strip() or None, verbose=False)
    except Exception as e:
        logger.warning(f"상품 이미지 문구 읽기 실패: {e}")
        return ""
    lines = [l.strip() for l in text.splitlines() if l.strip().startswith("-")]
    return "\n".join(lines)[:1500]


def short_keyword(name: str, hint: str = "") -> str:
    """제목·소제목·태그에 쓸 짧은 메인 키워드. 검색 키워드(hint)가 짧으면 '브랜드 + 검색 키워드',
    아니면 상품명 앞 2~3단어 (20자 이내)."""
    words = re.sub(r"[\[\]()/,]+", " ", name or "").split()
    brand = words[0] if words else ""
    hint = (hint or "").strip()
    if hint and len(hint) <= 15:
        return hint if (not brand or brand in hint or len(brand) > 10) else f"{brand} {hint}"
    out = ""
    for w in words[:3]:
        if len(out) + len(w) + 1 > 20:
            break
        out = f"{out} {w}".strip()
    return out or (name or "")[:20]


def _thumb_data_url(png: bytes) -> str:
    import base64
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    im.thumbnail((240, 240))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _find_item(api, product_id, keyword: str = "") -> Optional[Dict]:
    same = lambda i: str(i.get("affiliateProductId")) == str(product_id)
    for pg in range(1, 5):
        batch, total = api.issued_page(pg, 50)
        hit = next((i for i in batch if same(i)), None)
        if hit or not batch or pg * 50 >= total:
            break
    if not hit and keyword:
        for pg in range(1, 5):
            batch, total = api.search_page(keyword, pg, 50)
            hit = next((i for i in batch if same(i)), None)
            if hit or not batch or pg * 50 >= total:
                break
    return hit


def prepare_product(profile, blog_id: str, api_key: str, keyword: str = "", product_id=None,
                    min_shots: int = 3, item: Optional[Dict] = None) -> Tuple[Dict, List[Dict]]:
    """상품 하나를 준비한다.
    - product_id 가 있으면 그 상품, 없으면 keyword 로 검색해 아직 안 쓴 상품 중 수수료 높은 것
    - 링크 발급(이미 발급한 상품은 기존 링크) → 상품 페이지 캡처(최소 min_shots장) → 사진 저장·설명
    반환: (글쓰기용 상품 정보, 사진 [{id, desc, thumb}])"""
    import base64
    import user_media
    from playwright.sync_api import sync_playwright
    exclude = used_ids(blog_id)
    with sync_playwright() as pw:
        ctx, page = _open_account_browser(pw, profile, blog_id)
        try:
            api = ConnectApi(page, blog_id).open()
            item = clean_raw_item(item) if item else None
            if item and product_id and str(item["affiliateProductId"]) != str(product_id):
                item = None
            if item:
                item["affiliateProductId"] = int(item["affiliateProductId"])
            elif product_id:
                # 화면 목록의 상품 정보가 없을 때만: 검색 결과·발급 목록에서 같은 상품 찾기
                item = _find_item(api, product_id, keyword)
                if not item:
                    raise ShoppingConnectError("고른 상품을 쇼핑커넥트에서 다시 찾지 못했습니다. 상품을 다시 검색해 주세요.")
            else:
                # 자동: 아직 쓰지 않은 상품 중 무작위 (키워드가 있으면 그 분야, 없으면 넓은 분야)
                item = pick_random(api, keyword, exclude)
                if not item:
                    raise ShoppingConnectError((f"'{keyword}'(으)로 " if keyword else "") + "아직 쓰지 않은 쇼핑커넥트 상품을 찾지 못했습니다.")
            prod = product_from_item(item)
            issued = api.issue(prod["affiliate_product_id"])
            prod["url"] = issued.get("shortenUrl") or prod["url"]
            if not prod["url"]:
                raise ShoppingConnectError("쇼핑커넥트 링크를 발급받지 못했습니다.")
            logger.info(f"🛒 쇼핑커넥트 상품: {prod['name'][:40]} (수수료 {prod['commission']}%, 링크 {prod['url']})")
            shots, facts = capture_product(page, prod["product_url"], min_shots=min_shots)
        finally:
            ctx.close()
    if len(shots) < min_shots:
        raise ShoppingConnectError(f"상품 페이지 캡처가 {len(shots)}장뿐입니다 (최소 {min_shots}장 필요).")
    prod.update(facts)
    photos = []
    for i, png in enumerate(shots, 1):
        meta = user_media.save_photo(base64.b64encode(png).decode("ascii"), f"{prod['name'][:40]} {i}")
        photos.append({"id": meta["id"], "desc": user_media.describe_photo(meta["id"], api_key), "thumb": _thumb_data_url(png)})
    prod["image_facts"] = image_facts(api_key, [user_media.photo_path(p["id"]) for p in photos])
    mark_used(blog_id, prod["affiliate_product_id"])
    return prod, photos
