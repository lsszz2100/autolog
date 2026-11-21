"""AI + 검색 최적화(SEO) 기반 블로그 주제 추천 모듈 (topic_service.py)

키워드 하나를 넣으면:
1. N사 자동완성 + '함께 많이 찾는' 검색어로 실제 사람들이 찾는 세부 키워드(롱테일) 후보를 모읍니다.
2. 후보마다 N사 블로그 검색 상위 글을 보고 수치를 잽니다.
   - 검색 수요: 자동완성/함께 찾는 검색어 순위, '요즘 인기' 표시
   - 경쟁도: 상위 글 제목 중 그 키워드를 그대로 쓴 글 비율
   - 빈틈: 상위 글 중 3개월 넘게 지난 글 비율 (오래된 글이 많으면 새 글이 올라가기 쉬움)
3. Gemini가 수치와 상위 글 제목을 보고, 상위 글이 놓친 관점으로 주제·제목·글 구성 템플릿을 제안합니다.
   키가 없거나 실패하면 규칙 기반으로 추천합니다.
"""
import random
import re
import json
import html
import time
import logging
import datetime
import difflib
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Tuple

import requests

import gemini_client
from gemini_client import extract_json
from config import NO_SEARCH_DATA, banned_keywords, find_banned, PORTAL_KO

logger = logging.getLogger("naver.topic")

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
       "Accept-Language": "ko-KR,ko;q=0.9", "Referer": "https://search.naver.com/"}

TOPIC_MODELS = ["gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-flash-latest"]
STYLES = ("info", "review", "tips", "compare", "howto", "qna", "checklist", "issue")
STYLE_NAMES = {"info": "정보형 총정리", "review": "내돈내산 후기", "tips": "실전 꿀팁", "compare": "비교 분석형",
               "howto": "단계별 방법", "qna": "궁금증 Q&A", "checklist": "체크리스트", "issue": "최신 이슈·제도"}
MAX_CANDIDATES = 20       # 검색 화면까지 확인해 점수를 매기는 후보 수
POOL_LIMIT = 80            # 후보 풀(자동완성 확장 결과) 상한
DEFAULT_TOPIC_COUNT = 12

_cache: Dict[str, Tuple[float, dict]] = {}
_CACHE_SEC = 1800          # 같은 키워드는 30분 동안 다시 조회하지 않음
_req_lock = threading.Lock()
_next_slot = 0.0
_MIN_GAP = 0.7             # N사 검색 화면 요청 시작 사이 최소 간격(초). 몰아서 보내면 네트워크 검색이 잠시 막힘
_BLOCK_PAUSE = 600
_blocked_until = 0.0


def _compact(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


# ── 1. 후보 키워드 수집 ──
def _autocomplete(query: str) -> List[str]:
    url = ("https://ac.search.naver.com/nx/ac?q=" + urllib.parse.quote(query) +
           "&con=1&frm=nv&ans=2&r_format=json&r_enc=UTF-8&r_unicode=0&t_koreng=1&run=2&rev=4&q_enc=UTF-8&st=100")
    try:
        data = requests.get(url, headers=_UA, timeout=6).json()
        return [s[0].strip() for s in (data.get("items") or [[]])[0] if s and s[0].strip()]
    except Exception as e:
        logger.debug(f"자동완성 조회 실패 {query}: {e}")
        return []


def _related(query: str) -> List[Tuple[str, str]]:
    """'함께 많이 찾는' 검색어 [(검색어, 배지)]. 검색 첫 화면이 비동기로 불러오는 주소를 그대로 호출."""
    try:
        s = requests.Session()   # 이 주소는 검색 화면이 준 쿠키(page_uid 등)가 없으면 403
        page = _search_page("https://search.naver.com/search.naver?query=" + urllib.parse.quote(query), s)
        m = re.search(r'"apiURL":"(https://s\.search\.naver\.com/p/qra/[^"]+)"', page)
        if not m:
            return []
        data = s.get(m.group(1).replace("\\u0026", "&"), headers=_UA, timeout=6).json()
        out = []
        for c in (data.get("result") or {}).get("contents") or []:
            q = str(c.get("query", "")).strip()
            if q:
                out.append((q, str((c.get("badge") or {}).get("text", ""))))
        return out
    except Exception as e:
        logger.debug(f"함께 찾는 검색어 조회 실패 {query}: {e}")
        return []


# ── 2. 상위 글 분석 ──
def _age_days(s: str, today: datetime.date) -> int:
    s = s.strip()
    m = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", s)
    if m:
        try:
            return (today - datetime.date(int(m[1]), int(m[2]), int(m[3]))).days
        except ValueError:
            return -1
    m = re.match(r"(\d+)\s*(분|시간|일|주|개월|달)\s*전", s)
    if m:
        n = int(m[1])
        return {"분": 0, "시간": 0, "일": n, "주": n * 7, "개월": n * 30, "달": n * 30}[m[2]]
    if s.startswith("어제"):
        return 1
    return -1


def _search_page(url: str, session=None) -> str:
    """N사 검색 화면 요청. 짧은 시간에 몰아서 보내면 네트워크 전체 검색이 잠시 막히므로
    모든 요청을 한 줄로 세워 최소 간격을 두고, 막힘 화면이 보이면 한동안 더 보내지 않는다.
    session을 주면 그 세션으로 요청해 검색 화면이 준 쿠키를 이어 쓸 수 있다."""
    global _blocked_until
    if time.time() < _blocked_until:
        return ""
    global _next_slot
    with _req_lock:   # 요청 '시작' 시각을 최소 간격만큼 벌려 둠 (동시에 최대 2개)
        now = time.time()
        start = max(now, _next_slot)
        _next_slot = start + _MIN_GAP
    if start > now:
        time.sleep(start - now)
    try:
        page = (session or requests).get(url, headers=_UA, timeout=8).text
    except Exception as e:
        logger.debug(f"검색 화면 요청 실패: {e}")
        page = ""
    if "검색 서비스 이용이 제한" in page:
        _blocked_until = time.time() + _BLOCK_PAUSE
        logger.warning("N사 검색이 잠시 제한되어 남은 키워드 분석을 건너뜁니다. (10분 뒤 다시 시도 가능)")
        return ""
    return page


def _serp(query: str) -> Dict:
    """N사 블로그 검색(관련도순) 첫 화면의 상위 글 제목·작성일."""
    page = _search_page("https://search.naver.com/search.naver?ssc=tab.blog.all&query=" + urllib.parse.quote(query))
    titles = [html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
              for t in re.findall(r'sds-comps-text-type-headline1[^"]*">(.*?)</span>', page, re.S)]
    titles = [t for t in titles if t][:15]
    today = datetime.date.today()
    ages = [a for a in (_age_days(d, today) for d in re.findall(r'profile-info-subtext">([^<]+)<', page)) if a >= 0][:15]
    return {"titles": titles, "ages": ages}


def _metrics(cand: Dict, serp: Dict) -> Dict:
    titles, ages = serp["titles"], serp["ages"]
    kw = _compact(cand["keyword"])
    words = [w for w in cand["keyword"].split() if len(w) > 1]
    exact = sum(1 for t in titles if kw in _compact(t))
    # 단어가 모두 들어간 제목은 반쯤 겨냥한 글로 셈
    loose = sum(1 for t in titles if kw not in _compact(t) and words and all(w.lower() in t.lower() for w in words))
    raw = (exact + 0.5 * loose) / len(titles) if titles else 0.75
    # 검색 결과는 원래 그 키워드가 든 제목이 대부분이라 0.3~0.95 구간을 0~1로 펼침
    competition = min(1.0, max(0.0, (raw - 0.3) / 0.65))
    stale = sum(1 for a in ages if a > 90) / len(ages) if ages else 0.3
    fresh = sum(1 for a in ages if a <= 30) / len(ages) if ages else 0.3
    demand = cand["demand"]
    score = 100 * (0.45 * demand + 0.35 * (1 - competition) + 0.20 * stale)
    return {"competition": round(competition, 2), "known": bool(titles), "stale": round(stale, 2), "fresh": round(fresh, 2),
            "demand": round(demand, 2), "seo_score": int(round(score)), "top_titles": titles[:8],
            "result_count": len(titles)}


def _level(v: float, hi: float, lo: float) -> str:
    return "높음" if v >= hi else ("낮음" if v <= lo else "보통")


# 한 단어만 넣어도 '실제로 검색하는 조합'을 찾기 위해 붙여 보는 말들 (자동완성은 이어 쓴 말을 돌려줌)
_MODIFIERS = ["추천", "후기", "방법", "가격", "비교", "순위", "장단점", "효과", "종류", "고르는 법",
              "주의사항", "꿀팁", "가성비", "초보", "2026", "인기", "사용법", "부작용", "차이", "하는법"]
_MOD_BONUS = {"추천": 0.08, "후기": 0.06, "가격": 0.05, "순위": 0.05, "비교": 0.04, "방법": 0.04}


def _expand_autocomplete(keyword: str) -> List[Tuple[str, int, str]]:
    """자동완성 확장: [(검색어, 순위, 어떤 말을 붙여 찾았는지)]. 입력어 + 붙임말, 그리고 1차 결과를 한 번 더 확장."""
    queries = [(keyword, "")] + [(f"{keyword} {m}", m) for m in _MODIFIERS]
    with ThreadPoolExecutor(max_workers=6) as ex:
        first = list(ex.map(lambda q: _autocomplete(q[0]), queries))
    out: List[Tuple[str, int, str]] = []
    for (q, mod), res in zip(queries, first):
        for i, kw in enumerate(res[:10]):
            out.append((kw, i, mod))
    # 2차: 입력어 자동완성 상위 4개를 다시 자동완성 (더 긴 롱테일)
    seconds = [kw for kw, i, mod in out if not mod][:4]
    with ThreadPoolExecutor(max_workers=4) as ex:
        second = list(ex.map(_autocomplete, seconds))
    for res in second:
        for i, kw in enumerate(res[:8]):
            out.append((kw, i + 2, "2차"))
    return out


def collect_candidates(keyword: str) -> List[Dict]:
    """후보 키워드 목록 [{keyword, source, demand, badge}]. demand는 0~1 (높을수록 많이 찾음)."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_ac, f_rel = ex.submit(_expand_autocomplete, keyword), ex.submit(_related, keyword)
        ac, rel = f_ac.result(), f_rel.result()
    cands: Dict[str, Dict] = {}

    def add(kw: str, source: str, demand: float, badge: str = ""):
        kw = re.sub(r"\s+", " ", kw).strip()
        key = _compact(kw)
        if not key or len(kw) > 40:
            return
        if key in cands:
            c = cands[key]
            c["demand"] = min(1.0, max(c["demand"], demand) + 0.08)   # 여러 곳에서 나오면 가산
            if source not in c["source"]:
                c["source"] += "·" + source
            c["badge"] = c["badge"] or badge
            return
        cands[key] = {"keyword": kw, "source": source, "demand": demand, "badge": badge}

    add(keyword, "입력", 0.55)
    for kw, i, mod in ac:
        base = max(0.3, 0.95 - i * 0.06)
        if mod == "2차":
            base -= 0.1
        elif mod:
            base = base - 0.1 + _MOD_BONUS.get(mod, 0.02)
        add(kw, "자동완성", min(1.0, max(0.25, base)))
    for i, (kw, badge) in enumerate(rel[:15]):
        add(kw, "함께 찾는", min(1.0, max(0.3, 0.85 - i * 0.05) + (0.1 if badge else 0)), badge)

    out = sorted(cands.values(), key=lambda c: c["demand"], reverse=True)[:POOL_LIMIT]
    first = [c for c in out if _compact(c["keyword"]) == _compact(keyword)]
    rest = [c for c in out if c not in first]
    # 비슷한 키워드(예: '에어프라이어 추천', '에어프라이어추천')만 몰리지 않게, 붙임 말이 다른 것이 고루 들어가도록 선별
    picked, used_tail = [], {}
    for c in rest:
        tail = _compact(c["keyword"]).replace(_compact(keyword), "")[:3]
        if used_tail.get(tail, 0) >= 2:
            continue
        used_tail[tail] = used_tail.get(tail, 0) + 1
        picked.append(c)
    picked += [c for c in rest if c not in picked]   # 모자라면 나머지로 채움
    return (first + picked)[:MAX_CANDIDATES]


def analyze_keyword(keyword: str) -> List[Dict]:
    cands = collect_candidates(keyword)
    with ThreadPoolExecutor(max_workers=2) as ex:   # 검색 제한 방지: 동시 2개 + 시작 간격 0.7초
        serps = list(ex.map(lambda c: _serp(c["keyword"]), cands))
    rows = []
    for c, s in zip(cands, serps):
        m = _metrics(c, s)
        rows.append({**c, **m,
                     "demand_level": _level(c["demand"], 0.7, 0.45),
                     "competition_level": _level(m["competition"], 0.7, 0.4) if m["known"] else "확인 못 함",
                     "gap_level": _level(m["stale"], 0.5, 0.2) if m["known"] else "확인 못 함"})
    rows.sort(key=lambda r: r["seo_score"], reverse=True)
    for r in rows:
        logger.info(f"  · {r['keyword']}: SEO {r['seo_score']}점 (수요 {r['demand_level']}, 경쟁 {r['competition_level']}, 빈틈 {r['gap_level']})")
    return rows


# ── 3. 주제 추천 ──
_STYLE_HINTS = [
    ("compare", r"비교|차이|vs|VS|대신|어떤게|뭐가"),
    ("howto", r"방법|하는\s*법|만들기|만드는|굽기|굽는|설정|사용법|순서|세척|청소|레시피|시간(?!대)|온도"),
    ("issue", r"신청|지원금|대상|일정|조건|자격|발표|개정|2\d{3}년|최신|접수"),
    ("checklist", r"주의|체크|확인|준비물|고르는|선택\s*기준|구매\s*전"),
    ("qna", r"\?|왜|이유|몇|될까|되나|가능|괜찮"),
    ("review", r"후기|추천|내돈내산|리뷰|순위|가성비|브랜드"),
    ("tips", r"꿀팁|팁|노하우|절약|활용"),
]
_TITLE_FMT = {
    "info": "{kw} 총정리, 꼭 알아야 할 핵심 정보", "review": "{kw} 장단점, 고르기 전에 알아둘 점",
    "tips": "{kw} 꿀팁 5가지, 모르면 손해", "compare": "{kw} 비교, 차이점과 선택 기준 정리",
    "howto": "{kw} 방법, 처음이라도 쉽게 따라 하는 순서", "qna": "{kw} 자주 묻는 질문 총정리",
    "checklist": "{kw} 전 꼭 확인할 체크리스트", "issue": "{kw} 최신 정리, 대상·일정·방법",
}


def _guess_style(kw: str) -> str:
    for style, pat in _STYLE_HINTS:
        if re.search(pat, kw):
            return style
    return "info"


def _reason(r: Dict) -> str:
    bits = []
    if r["demand_level"] == "높음":
        bits.append("검색하는 사람이 많은 키워드" + (f"('{r['badge']}')" if r.get("badge") else ""))
    if r["competition_level"] == "낮음":
        bits.append("이 키워드를 제목에 그대로 쓴 상위 글이 적음")
    elif r["competition_level"] == "높음":
        bits.append("경쟁 글이 많아 차별화된 관점이 필요함")
    if r["gap_level"] == "높음":
        bits.append("상위 글 상당수가 3개월 넘게 지난 글이라 최신 글이 올라가기 좋음")
    return ", ".join(bits) or "검색 수요와 경쟁이 고르게 맞는 키워드"


# 키워드에 이미 템플릿 핵심어가 들어 있을 때 쓰는 제목 ('절약 방법 방법' 같은 겹침 방지)
_TITLE_CORE = {
    "info": r"총정리|정리", "review": r"장단점|후기", "tips": r"꿀팁|팁", "compare": r"비교|차이",
    "howto": r"방법|하는\s*법|순서", "qna": r"질문|Q&A", "checklist": r"체크리스트|체크", "issue": r"최신|정리",
}
_TITLE_FMT_ALT = {
    "info": "{kw}, 꼭 알아야 할 핵심 정보", "review": "{kw}, 고르기 전에 알아둘 점",
    "tips": "{kw} 5가지, 모르면 손해", "compare": "{kw}, 차이점과 선택 기준 정리",
    "howto": "{kw}, 처음이라도 쉽게 따라 하는 순서", "qna": "{kw}, 자주 묻는 질문 총정리",
    "checklist": "{kw}, 빠뜨리기 쉬운 항목 총정리", "issue": "{kw}, 대상·일정·방법 최신 정리",
}


def _rule_title(kw: str, style: str) -> str:
    fmt = _TITLE_FMT_ALT if re.search(_TITLE_CORE[style], kw, re.I) else _TITLE_FMT
    return fmt[style].format(kw=kw)


def _rule_topics(rows: List[Dict], count: int) -> List[Dict]:
    out = []
    for r in rows[:count]:
        style = _guess_style(r["keyword"])
        out.append({"title": _rule_title(r["keyword"], style), "main_keyword": r["keyword"],
                    "sub_keywords": [x["keyword"] for x in rows if x is not r][:3], "style": style,
                    "angle": "", "intent": "", "reason": _reason(r), "score": r["seo_score"]})
    return out


def _avoid_rule(avoid) -> str:
    """이미 발행·예약한 제목과 같은 주제를 다시 추천하지 않게 하는 프롬프트 규칙."""
    titles = [t for t in (avoid or []) if t and str(t).strip()][:40]
    words = banned_keywords()
    ban = ("\n- 다음 금지 키워드와 그것이 가리키는 대상은 제목·main_keyword·sub_keywords 어디에도 넣지 말고, 그에 대한 주제도 추천하지 마세요: "
           + ", ".join(words[:100])) if words else ""
    if not titles:
        return ban
    return ban + ("\n- 아래는 이미 발행했거나 예약한 글 제목입니다. 이 글들과 같은 주제·같은 관점은 절대 추천하지 말고, "
            "전혀 다른 주제·다른 관점으로만 고르세요:\n" + "\n".join(f"  · {t}" for t in titles))


def _banned_topic(topic: Dict) -> str:
    """주제의 제목·키워드에 들어 있는 금지 키워드 ('' = 없음)."""
    words = banned_keywords()
    return find_banned(" ".join([str(topic.get("title", "")), str(topic.get("main_keyword", ""))]
                                + [str(k) for k in (topic.get("sub_keywords") or [])]), words)


def _is_used(topic: Dict, keyword: str, used_c: List[str]) -> bool:
    """이미 쓴 글과 같은 주제인지 (제목이 같거나 매우 비슷하거나, 이미 쓴 글이 다룬 세부 키워드)."""
    c = _compact(topic.get("title", ""))
    mk = _compact(topic.get("main_keyword", ""))
    if _banned_topic(topic):
        return True
    for u in used_c:
        if not u:
            continue
        if c == u or difflib.SequenceMatcher(None, c, u).ratio() >= 0.6:
            return True
        if mk and mk != _compact(keyword) and len(mk) >= 4 and mk in u:
            return True
    return False


def _ai_topics(keyword: str, rows: List[Dict], api_key: str, count: int, avoid: List[str] = None) -> List[Dict]:
    avoid_rule = _avoid_rule(avoid)
    table = []
    for i, r in enumerate(rows, 1):
        tops = " / ".join(r["top_titles"][:6]) or "(상위 글 정보 없음)"
        table.append(f"{i}. 키워드 '{r['keyword']}' — SEO점수 {r['seo_score']}, 검색 수요 {r['demand_level']}"
                     f"{' (' + r['badge'] + ')' if r.get('badge') else ''}, 경쟁도 {r['competition_level']}, "
                     f"오래된 상위 글 비율 {int(r['stale'] * 100)}%\n   현재 상위 글 제목: {tops}")
    styles = "\n".join(f"- {k}: {v}" for k, v in STYLE_NAMES.items())
    today = datetime.date.today().strftime("%Y년 %m월 %d일")
    prompt = f"""당신은 {PORTAL_KO} 블로그 상위 노출(C-Rank, D.I.A+) 전문 콘텐츠 기획자입니다. 오늘은 {today}입니다.
사용자가 '{keyword}' 키워드로 블로그 글을 쓰려고 합니다. 아래는 실제 검색 데이터로 잰 세부 키워드 후보입니다.
SEO점수는 검색 수요가 높고, 경쟁 글이 적고, 상위 글이 오래될수록 높습니다.
오래된 상위 글 비율이 높으면 새 글이 올라가기 쉽고, 0%에 가까우면 최근 글이 상위를 차지하고 있어 경쟁이 활발하다는 뜻입니다(좋은 신호가 아님).

[세부 키워드 후보와 현재 상위 글]
{chr(10).join(table)}

[할 일]
위 데이터를 근거로, 지금 쓰면 상위 노출과 클릭 가능성이 가장 높은 블로그 주제 {count}개를 추천하세요.
- main_keyword는 반드시 위 후보 키워드 중 하나를 글자 그대로 쓰세요. 같은 main_keyword는 한 번만 쓰세요.
- 상위 글 제목들을 보고 이미 많이 다룬 관점은 피하고, 검색하는 사람이 원하지만 상위 글이 놓친 관점(angle)을 잡으세요.
- title은 main_keyword로 시작하는 30자 안팎의 클릭하고 싶은 제목. 과장·낚시·이모지 금지. 직접 체험한 척하는 표현('직접 써보니' 등) 금지.
- style은 아래 글 구성 템플릿 코드 중 주제에 가장 맞는 하나:
{styles}
- sub_keywords는 본문 소제목·태그에 함께 넣을 황금키워드 2~4개. main_keyword와 검색 의도가 이어지는 후보 키워드를 조합해 한 글에서 여러 검색어를 동시에 노릴 수 있게 고르세요.
- intent는 이 키워드를 검색하는 사람이 알고 싶은 것 한 문장.
- reason은 데이터(수요·경쟁·오래된 글)를 근거로 왜 이 주제가 유리한지 한 문장.
- score는 0~100의 추천 점수 (SEO점수를 기본으로 하되 차별화 가능성과 클릭 매력을 반영).
- 시기를 타는 주제(계절·제도·행사)는 오늘 날짜에 맞는 것을 우선하세요.{avoid_rule}

반드시 아래 JSON 배열로만 답하세요 (코드블록 금지):
[{{"title": "...", "main_keyword": "...", "sub_keywords": ["..."], "style": "info", "angle": "상위 글과 다른 관점 한 문장", "intent": "...", "reason": "...", "score": 87}}]"""

    valid = {_compact(r["keyword"]): r for r in rows}

    def _accept(text: str):
        data = extract_json(text, "[", "]")
        if not isinstance(data, list):
            return None
        out, seen = [], set()
        for t in data:
            if not isinstance(t, dict):
                continue
            mk = str(t.get("main_keyword", "")).strip()
            key = _compact(mk)
            title = re.sub(r"\s+", " ", str(t.get("title", ""))).strip()[:60]
            if key not in valid or key in seen or not title:
                continue
            seen.add(key)
            style = t.get("style") if t.get("style") in STYLES else _guess_style(mk)
            try:
                score = max(0, min(100, int(t.get("score", valid[key]["seo_score"]))))
            except (TypeError, ValueError):
                score = valid[key]["seo_score"]
            subs = [str(s).strip()[:30] for s in (t.get("sub_keywords") or []) if str(s).strip()][:4]
            out.append({"title": title, "main_keyword": valid[key]["keyword"], "sub_keywords": subs, "style": style,
                        "angle": str(t.get("angle", "")).strip()[:150], "intent": str(t.get("intent", "")).strip()[:150],
                        "reason": str(t.get("reason", "")).strip()[:150], "score": score})
        return out if len(out) >= min(2, count) else None

    topics, model = gemini_client.generate(api_key, prompt, TOPIC_MODELS,
                                           generation_config={"temperature": 0.6, "maxOutputTokens": 8192,
                                                              "thinkingConfig": {"thinkingBudget": 1024}},  # 생각 단계를 줄여 응답 속도 향상
                                           timeout=40, accept=_accept)
    logger.info(f"🎯 Gemini ({model}) 주제 {len(topics)}개 추천")
    return topics


def _ai_only_topics(keyword: str, api_key: str, count: int, avoid: List[str] = None) -> List[Dict]:
    """검색 데이터 없이 AI가 키워드만 보고 주제를 고른다 (크몽 판매판, 자동 예약에서 추천 주제가 모자랄 때).
    avoid: 이미 쓴 제목 (겹치지 않게)."""
    avoid_rule = _avoid_rule(avoid)
    styles = "\n".join(f"- {k}: {v}" for k, v in STYLE_NAMES.items())
    today = datetime.date.today().strftime("%Y년 %m월 %d일")
    prompt = f"""당신은 블로그 콘텐츠 기획자입니다. 오늘은 {today}입니다.
사용자가 '{keyword}' 키워드로 블로그 글을 쓰려고 합니다.

[할 일]
이 키워드로 독자가 실제로 궁금해하고 끝까지 읽을 만한 블로그 주제 {count}개를 추천하세요.
- main_keyword는 '{keyword}'이거나 '{keyword}'를 포함한 구체적인 세부 키워드. 같은 main_keyword는 한 번만 쓰세요.
- 주제끼리 관점이 겹치지 않게 하고, 흔한 총정리보다 독자에게 실제로 도움이 되는 구체적인 관점(angle)을 잡으세요.
- title은 main_keyword로 시작하는 30자 안팎의 읽고 싶은 제목. 과장·낚시·이모지 금지. 직접 체험한 척하는 표현('직접 써보니' 등) 금지.
- style은 아래 글 구성 템플릿 코드 중 주제에 가장 맞는 하나:
{styles}
- sub_keywords는 본문 소제목에 넣을 세부 키워드 2~4개.
- intent는 이 주제를 찾는 사람이 알고 싶은 것 한 문장.
- reason은 왜 이 주제가 독자에게 유용한지 한 문장 (검색량·순위 같은 수치를 지어내지 마세요).
- score는 0~100의 추천 점수 (독자 관심도·유용성·시기 적합성).
- 시기를 타는 주제(계절·제도·행사)는 오늘 날짜에 맞는 것을 우선하세요.{avoid_rule}

반드시 아래 JSON 배열로만 답하세요 (코드블록 금지):
[{{"title": "...", "main_keyword": "...", "sub_keywords": ["..."], "style": "info", "angle": "이 글만의 관점 한 문장", "intent": "...", "reason": "...", "score": 87}}]"""

    def _accept(text: str):
        data = extract_json(text, "[", "]")
        if not isinstance(data, list):
            return None
        out, seen = [], set()
        for t in data:
            if not isinstance(t, dict):
                continue
            mk = re.sub(r"\s+", " ", str(t.get("main_keyword", ""))).strip()[:40] or keyword
            title = re.sub(r"\s+", " ", str(t.get("title", ""))).strip()[:60]
            key = _compact(title)
            if not title or key in seen:
                continue
            seen.add(key)
            style = t.get("style") if t.get("style") in STYLES else _guess_style(mk)
            try:
                score = max(0, min(100, int(t.get("score", 70))))
            except (TypeError, ValueError):
                score = 70
            subs = [str(x).strip()[:30] for x in (t.get("sub_keywords") or []) if str(x).strip()][:4]
            out.append({"title": title, "main_keyword": mk, "sub_keywords": subs, "style": style,
                        "angle": str(t.get("angle", "")).strip()[:150], "intent": str(t.get("intent", "")).strip()[:150],
                        "reason": str(t.get("reason", "")).strip()[:150], "score": score})
        return out if len(out) >= min(2, count) else None

    topics, model = gemini_client.generate(api_key, prompt, TOPIC_MODELS,
                                           generation_config={"temperature": 0.7, "maxOutputTokens": 8192,
                                                              "thinkingConfig": {"thinkingBudget": 1024}},
                                           timeout=40, accept=_accept)
    logger.info(f"🎯 Gemini ({model}) 주제 {len(topics)}개 추천")
    return topics


def _recommend_without_search(keyword: str, api_key: str, count: int, avoid: List[str] = None) -> Dict:
    logger.info(f"🎯 '{keyword}' 주제 추천: AI가 주제를 고르는 중...")
    topics, used_ai = [], False
    if api_key and len(api_key.strip()) > 10:
        try:
            topics, used_ai = _ai_only_topics(keyword, api_key.strip(), count, avoid), True
        except Exception as e:
            logger.warning(f"AI 주제 추천 실패 ({e}) — 기본 글 구성으로 추천합니다.")
    if not topics:  # 키가 없거나 실패: 글 구성 템플릿별 기본 제목
        topics = [{"title": _rule_title(keyword, st), "main_keyword": keyword, "sub_keywords": [], "style": st,
                   "angle": "", "intent": "", "reason": f"{STYLE_NAMES[st]} 구성으로 쓰는 기본 주제입니다.", "score": 60}
                  for st in STYLES[:count]]
    for t in topics:
        t["style_name"] = STYLE_NAMES.get(t["style"], "")
        t["metrics"] = {}
    topics.sort(key=lambda t: t["score"], reverse=True)
    return {"keyword": keyword, "ai": used_ai, "topics": topics[:count], "keywords": []}


def topics_for_batch(keyword: str, api_key: str, need: int, used_titles: List[str]) -> List[Dict]:
    """자동 예약용 주제 need개: 검색 데이터 추천을 먼저 쓰고, 모자라면 AI가 겹치지 않는 주제를 더 만든다."""
    used = {_compact(t) for t in used_titles}
    fresh = [t for t in recommend_topics(keyword, api_key, max(6, min(need, 8)), used_titles)["topics"] if _compact(t["title"]) not in used]
    for _ in range(3):      # AI가 요청보다 적게 주거나 겹치는 제목을 주면 모자란 만큼 다시 받는다
        if len(fresh) >= need:
            break
        try:
            more = _ai_only_topics(keyword, api_key, need - len(fresh) + 2, list(used_titles) + [t["title"] for t in fresh])
            taken = used | {_compact(t["title"]) for t in fresh}
            for t in more:
                if _compact(t["title"]) not in taken and not _banned_topic(t):
                    taken.add(_compact(t["title"]))
                    fresh.append(t)
        except Exception as e:
            logger.warning(f"추가 주제 만들기 실패: {e}")
    # 점수가 높은 주제부터 필요한 만큼 고르고, 올라가는 순서만 섞는다
    best = sorted(fresh, key=lambda t: t.get("score", 0), reverse=True)[:need]
    random.shuffle(best)
    return best


def recommend_topics(keyword: str, api_key: str = "", count: int = DEFAULT_TOPIC_COUNT,
                     used_titles: List[str] = None) -> Dict:
    """{'keyword', 'ai': bool, 'topics': [...], 'keywords': [키워드별 수치]}
    used_titles: 이미 발행·예약한 글 제목 — 같은 주제는 추천 목록에서 뺀다."""
    keyword = re.sub(r"\s+", " ", keyword or "").strip()[:50]
    if not keyword:
        raise ValueError("키워드를 입력해주세요.")
    hit = find_banned(keyword, banned_keywords())
    if hit:
        raise ValueError(f"'{keyword}'에 금지 키워드 '{hit}'이(가) 들어 있어 주제를 추천하지 않습니다.")
    used = [t for t in (used_titles or []) if t and str(t).strip()]
    used_c = [_compact(t) for t in used]
    ck = f"{keyword}|{bool(api_key)}|{hash(tuple(sorted(used_c)))}|{hash(tuple(banned_keywords()))}"
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < _CACHE_SEC:
        return hit[1]
    recent = list(used)[:40]       # AI에게는 최근 것부터 알려 준다 (목록은 최신순)
    rows = []
    topics, used_ai = [], False
    if NO_SEARCH_DATA:
        r = _recommend_without_search(keyword, api_key, count, recent)
        topics, used_ai = r["topics"], r["ai"]
    else:
        logger.info(f"🎯 '{keyword}' 주제 추천: 검색 데이터 분석 중...")
        rows = analyze_keyword(keyword)
        if api_key and len(api_key.strip()) > 10:
            try:
                topics, used_ai = _ai_topics(keyword, rows, api_key.strip(), count, recent), True
            except Exception as e:
                logger.warning(f"AI 주제 추천 실패 ({e}) — 검색 데이터 규칙으로 추천합니다.")
        by_kw = {_compact(r["keyword"]): r for r in rows}
        for t in topics:
            r = by_kw.get(_compact(t["main_keyword"]), {})
            t["style_name"] = STYLE_NAMES.get(t["style"], "")
            t["metrics"] = {k: r.get(k) for k in ("seo_score", "demand_level", "competition_level", "gap_level", "badge")}
    topics = [t for t in topics if not _is_used(t, keyword, used_c)]
    if not NO_SEARCH_DATA and len(topics) < count:      # 부족하면 아직 안 쓴 검색 키워드로 채움
        taken = {_compact(t["main_keyword"]) for t in topics}
        extra = _rule_topics([r for r in rows if _compact(r["keyword"]) not in taken], len(rows))
        by_kw = {_compact(r["keyword"]): r for r in rows}
        for t in extra:
            if len(topics) >= count:
                break
            if _is_used(t, keyword, used_c):
                continue
            r = by_kw.get(_compact(t["main_keyword"]), {})
            t["style_name"] = STYLE_NAMES.get(t["style"], "")
            t["metrics"] = {k: r.get(k) for k in ("seo_score", "demand_level", "competition_level", "gap_level", "badge")}
            topics.append(t)
    if api_key and len(api_key.strip()) > 10:           # 그래도 모자라면 AI가 겹치지 않는 주제를 더 만든다
        for _ in range(3):
            if len(topics) >= count:
                break
            try:
                more = _ai_only_topics(keyword, api_key.strip(), count - len(topics) + 2,
                                       recent + [t["title"] for t in topics])
            except Exception as e:
                logger.warning(f"추가 주제 만들기 실패: {e}")
                break
            for t in more:
                if len(topics) < count and not _is_used(t, keyword, used_c + [_compact(x["title"]) for x in topics]):
                    t["style_name"] = STYLE_NAMES.get(t["style"], "")
                    t["metrics"] = {}
                    topics.append(t)
    topics.sort(key=lambda t: t["score"], reverse=True)
    result = {"keyword": keyword, "ai": used_ai, "topics": topics[:count],
              "excluded_used": bool(used),
              "keywords": [{k: r[k] for k in ("keyword", "source", "badge", "seo_score", "demand_level",
                                              "competition_level", "gap_level")} for r in rows]}
    _cache[ck] = (time.time(), result)
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(json.dumps(recommend_topics(sys.argv[1] if len(sys.argv) > 1 else "에어프라이어", sys.argv[2] if len(sys.argv) > 2 else ""),
                     ensure_ascii=False, indent=1))
