"""숏폼 클립을 N사 클립 크리에이터(clipcreators)에 올린다: 영상·설명·카테고리·장소 태그·공개 설정.

화면 구조는 2026-11-25에 확인했다(clipcreators.naver.com /web/upload → /web/draft/{id}).
영상 파일을 넣는 순간 임시글이 생기므로, 실패하면 그 임시글이 남을 수 있다(결과에 draft 주소를 알려 준다).
"""
import logging
import re
import time
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("naver.clip_upload")

UPLOAD_URL = "https://clipcreators.naver.com/web/upload"
DESC_MAX = 3000
# 1차 카테고리 (화면의 목록 그대로, 2026-11-25)
CATEGORIES = ["플레이스", "여행", "푸드", "일상기록", "엔터", "음악", "뷰티", "패션", "프로스포츠", "아웃도어, 운동",
              "미술", "경제", "과학", "리빙, 홈", "동물", "라이프스타일", "테크", "문학", "인문, 교양", "이슈",
              "건강, 의학", "게임", "유머", "교육", "어학, 외국어", "자동차", "쇼핑", "법", "전쟁", "범죄", "재난",
              "종교", "정치", "커리어", "커뮤니케이션", "생산성", "지도, 네비게이션", "라이프 이벤트", "시상식, 행사", "장르"]

F = "[class*=ClipDetailForm_"


from clip_maker import ClipError


class ClipUploadError(ClipError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"[\s·・,.\-()]+", "", (s or "")).lower()


def pick_place(cards: List[Dict], query: str) -> int:
    """검색 결과 [{name, address}] 중 검색어에 가장 맞는 것의 번호. 결과가 없으면 -1."""
    if not cards:
        return -1
    q = _norm(query)
    toks = [_norm(t) for t in re.split(r"\s+", query or "") if _norm(t)]
    best, best_score = 0, -1.0
    for i, c in enumerate(cards):
        name, addr = _norm(c.get("name", "")), _norm(c.get("address", ""))
        if name and name == q:
            return i
        score = sum(1.0 for t in toks if t in name) + sum(0.5 for t in toks if t not in name and t in addr)
        if q and (q in name or name in q):
            score += 2
        if score > best_score:
            best, best_score = i, score
    return best


def _toggle(page, label: str, on: bool):
    sw = page.locator(f"button[role=switch][aria-label='{label}']").first
    sw.scroll_into_view_if_needed(timeout=5000)
    if (sw.get_attribute("aria-checked") == "true") != on:
        sw.click()
        page.wait_for_timeout(500)


def _choose_category(page, cat1: str, cat2: str = ""):
    opt = "button[class*=ClipDetailForm_dropdownOption]"
    page.locator(f"button{F}dropdownPrimary]").first.click()
    page.wait_for_timeout(600)
    page.locator(opt).filter(has_text=re.compile(rf"^\s*{re.escape(cat1)}\s*$")).first.click()
    page.wait_for_timeout(800)
    sec = page.locator(f"button{F}dropdownSecondary]").first
    if sec.count() and sec.is_enabled():
        sec.click()
        page.wait_for_timeout(600)
        opts = page.locator(opt)
        target = opts.filter(has_text=re.compile(rf"^\s*{re.escape(cat2)}\s*$")) if cat2 else opts
        (target if target.count() else opts).first.click()
        page.wait_for_timeout(500)


def _tag_place(page, query: str) -> Optional[Dict]:
    """장소 태그. 고른 장소 {name, address} 또는 결과가 없으면 None."""
    page.locator(f"button{F}tagBtn]", has_text="장소").first.click()
    box = page.locator("input[class*=TagSearchBar_searchInput]").first
    box.wait_for(timeout=10000)
    box.fill(query)
    box.press("Enter")
    card = "[class*=InfoTagPopupView_card]"
    try:
        page.locator(card).first.wait_for(timeout=10000)
    except Exception:
        page.locator("[class*=Modal_closeBtn]").first.click()
        return None
    cards = page.evaluate(f"""() => [...document.querySelectorAll('{card}')].map(c => ({{
        name: (c.querySelector('[class*=placeName]') || {{}}).innerText || '',
        address: (c.querySelector('[class*=placeAddress]') || {{}}).innerText || ''}}))""")
    i = pick_place(cards, query)
    page.locator(card).nth(i).locator("button[class*=InfoTagPopupView_selectBtn]").click()
    page.locator(f"{F}selectedPlaceName]").first.wait_for(timeout=8000)
    return cards[i]


def _wait_encoding(page, say, timeout_sec: int = 600):
    end = time.time() + timeout_sec
    while time.time() < end:
        txt = page.locator("body").inner_text(timeout=5000)
        m = re.search(r"인코딩 진행 중\s*(\d+)%", txt)
        if not m and "인코딩 중" not in txt:
            return
        say(f"영상 처리 기다리는 중... {m.group(1) + '%' if m else ''}", 55)
        page.wait_for_timeout(3000)
    raise ClipUploadError("N사 쪽 영상 처리가 10분 넘게 끝나지 않았습니다. 클립 크리에이터의 임시저장 목록에서 확인해 주세요.")


def upload(page, video_path: str, description: str, *, category: str = "일상기록", category2: str = "",
           place: str = "", public: bool = True, ai_used: bool = True, sponsored: bool = False,
           progress: Optional[Callable[[str, int], None]] = None) -> Dict:
    """로그인된 page로 클립을 올린다. 반환 {url, draft, place}."""
    say = progress or (lambda m, p: None)
    description = (description or "").strip()[:DESC_MAX]
    if not description:
        raise ClipUploadError("클립 설명이 비어 있습니다.")
    if category not in CATEGORIES:
        category = "일상기록"

    say("클립 크리에이터 여는 중...", 10)
    page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30000)
    if "nid.naver.com" in page.url:
        raise ClipUploadError("로그인이 풀려 있습니다. [로그인] 버튼으로 다시 로그인해 주세요.")
    btn = page.get_by_role("button", name="파일 선택").first
    try:
        btn.wait_for(state="visible", timeout=20000)
    except Exception:
        raise ClipUploadError("클립 업로드 화면을 열지 못했습니다. 이 계정이 클립 크리에이터를 쓸 수 있는지 확인해 주세요.")
    page.wait_for_timeout(1500)     # 화면 준비(숨은 파일 칸에 바로 넣으면 반응이 없음)
    say("영상 올리는 중...", 20)
    with page.expect_file_chooser(timeout=15000) as fc:
        btn.click()
    fc.value.set_files(video_path)
    page.wait_for_url(re.compile(r"/web/draft/\d+"), timeout=180000)
    draft = page.url
    page.locator(f"textarea{F}textarea]").first.wait_for(timeout=30000)
    logger.info(f"🎞 클립 임시글 생성: {draft}")

    say("설명·카테고리 넣는 중...", 35)
    page.locator(f"textarea{F}textarea]").first.fill(description)
    _choose_category(page, category, category2)
    _toggle(page, "AI 활용 설정", ai_used)
    _toggle(page, "광고・협찬 설정", sponsored)

    chosen = None
    if place.strip():
        say(f"장소 태그 '{place.strip()}' 찾는 중...", 45)
        chosen = _tag_place(page, place.strip()[:60])
        if chosen:
            logger.info(f"📍 클립 장소 태그: {chosen['name']} ({chosen['address']})")
        else:
            logger.warning(f"📍 '{place}' 장소 검색 결과가 없어 장소 태그 없이 올립니다.")
    _toggle(page, "전체 공개", public)

    _wait_encoding(page, say)
    say("등록 누르는 중...", 85)
    page.locator(f"button{F}submitBtn]").first.click()
    end = time.time() + 90
    while time.time() < end and "/web/draft/" in page.url:
        dlg = page.locator("[role=dialog] button, [class*=Modal_] button").filter(has_text=re.compile(r"^\s*(확인|등록)\s*$"))
        if dlg.count():
            dlg.first.click()
        page.wait_for_timeout(1500)
    if "/web/draft/" in page.url:
        msg = page.locator("[role=dialog], [class*=Toast], [class*=error]").all_inner_texts()
        raise ClipUploadError("등록이 끝나지 않았습니다" + (f": {' '.join(msg)[:120]}" if msg else "")
                              + f" — 임시글({draft})을 확인해 주세요.")
    say("등록 완료", 100)
    logger.info(f"✅ 클립 등록 완료 ({'전체 공개' if public else '나만 보기'}): {page.url}")
    return {"url": page.url, "draft": draft, "place": chosen}
