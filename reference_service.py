"""상위 노출 최신 블로그 글 참고 자료 수집 모듈 (reference_service.py)

- N사 블로그 검색(관련도순·최근 1개월) + 다음 블로그 검색(티스토리, 정확도순·최근 1개월)에서 상위 글 수집
- 본문 분량·소제목 구성 기준으로 품질 상위 글만 골라 AI 원고 작성 시 '구성/정보 범위' 참고용으로 전달
- 생성 원고가 참고 글 문장을 그대로 베끼지 않았는지 n-gram 중복률로 검사
"""
import re
import html
import logging
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict

import requests

from text_style import strip_links

logger = logging.getLogger("naver.reference")

_UA_PC = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
          "Accept-Language": "ko-KR,ko;q=0.9"}
_UA_MO = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36",
          "Accept-Language": "ko-KR,ko;q=0.9"}

# 광고 고지·저작권 문구 등 참고 가치 없는 줄
_NOISE = re.compile(r"제휴|수수료|파트너스|협찬|소정의|원고료|무단\s*(전재|복제)|저작권|구독|공감.*댓글|댓글.*공감|이웃\s*추가")


def _clean(fragment: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment)).replace("\u200b", "").strip()
    # 외부 글의 링크·연락처는 프롬프트에 넘기지 않음
    return strip_links(re.sub(r"\s+", " ", text))


def _search_naver(keyword: str, exclude_blog_id: str, limit: int) -> List[str]:
    q = urllib.parse.quote(keyword)
    url = f"https://search.naver.com/search.naver?ssc=tab.blog.all&query={q}&nso={urllib.parse.quote('so:r,p:1m')}"
    r = requests.get(url, headers=_UA_PC, timeout=10)
    seen, out = set(), []
    for bid, no in re.findall(r"https://blog\.naver\.com/([A-Za-z0-9_\-]+)/(\d{9,})", r.text):
        # 같은 블로거 글은 1개만 (한 사람 문체에 쏠리지 않게)
        if bid == exclude_blog_id or bid in seen:
            continue
        seen.add(bid)
        out.append(f"https://m.blog.naver.com/PostView.naver?blogId={bid}&logNo={no}")
        if len(out) >= limit:
            break
    return out


def _search_tistory(keyword: str, limit: int) -> List[str]:
    q = urllib.parse.quote(keyword)
    url = f"https://search.daum.net/search?w=fusion&col=blog&q={q}&sort=accuracy&period=m"
    r = requests.get(url, headers=_UA_PC, timeout=10)
    seen, out = set(), []
    for link in re.findall(r'href="(https?://([a-z0-9\-]+)\.tistory\.com/(?:entry/)?[^"#?]+)"', r.text):
        if link[1] in seen:
            continue
        seen.add(link[1])
        out.append(link[0])
        if len(out) >= limit:
            break
    return out


def _parse_naver(page: str) -> Dict:
    title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    paras = []
    # se-fs-fs24 등 큰 글씨 문단은 소제목으로 간주
    for body in re.findall(r'<p class="se-text-paragraph[^"]*"[^>]*>(.*?)</p>', page, re.S):
        t = _clean(body)
        if t:
            paras.append(t)
    headings = [_clean(h) for h in re.findall(r'<span[^>]*class="[^"]*se-fs-fs(?:2[4-9]|3\d)[^"]*"[^>]*>(.*?)</span>', page, re.S)]
    return {"title": _clean(title.group(1)) if title else "", "paras": paras, "headings": [h for h in headings if h]}


def _parse_tistory(page: str) -> Dict:
    title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    m = re.search(r'<div[^>]+class="[^"]*(?:tt_article_useless_p_margin|entry-content|article-view|contents_style)[^"]*"[^>]*>(.*?)'
                  r'(?:<div[^>]+class="[^"]*(?:another_category|container_postbtn|tt-plugin)|$)', page, re.S)
    body = m.group(1) if m else ""
    paras = [_clean(p) for p in re.findall(r"<(?:p|h[2-4])[^>]*>(.*?)</(?:p|h[2-4])>", body, re.S)]
    headings = [_clean(h) for h in re.findall(r"<h[2-4][^>]*>(.*?)</h[2-4]>", body, re.S)]
    return {"title": _clean(title.group(1)) if title else "", "paras": [p for p in paras if p], "headings": [h for h in headings if h]}


def _fetch(url: str) -> Dict:
    try:
        is_naver = "blog.naver.com" in url
        r = requests.get(url, headers=_UA_MO if is_naver else _UA_PC, timeout=10)
        if r.status_code != 200:
            return {}
        doc = _parse_naver(r.text) if is_naver else _parse_tistory(r.text)
        doc["paras"] = [p for p in doc["paras"] if not _NOISE.search(p)]
        doc["url"] = url
        doc["source"] = "N사 블로그" if is_naver else "티스토리"
        doc["chars"] = sum(len(p.replace(" ", "")) for p in doc["paras"])
        return doc
    except Exception as e:
        logger.debug(f"참고 글 수집 실패 {url}: {e}")
        return {}


def _quality(doc: Dict) -> float:
    # 공백 제외 분량(2,500자까지만 가산) + 소제목 구성 + 문단 수. 지나치게 짧은 글은 제외
    if doc.get("chars", 0) < 800:
        return -1
    return min(doc["chars"], 2500) / 100 + min(len(doc["headings"]), 8) * 2 + min(len(doc["paras"]), 40) * 0.3


def collect_reference_posts(keyword: str, exclude_blog_id: str = "", max_posts: int = 3) -> List[Dict]:
    """키워드 관련 최신 상위 블로그 글 중 품질 상위 max_posts개를 반환. 실패 시 빈 리스트."""
    try:
        urls = []
        try:
            urls += _search_naver(keyword, exclude_blog_id, limit=6)
        except Exception as e:
            logger.warning(f"N사 블로그 검색 실패: {e}")
        try:
            urls += _search_tistory(keyword, limit=3)
        except Exception as e:
            logger.warning(f"다음(티스토리) 블로그 검색 실패: {e}")
        if not urls:
            return []
        with ThreadPoolExecutor(max_workers=6) as ex:
            docs = [d for d in ex.map(_fetch, urls) if d]
        ranked = sorted((d for d in docs if _quality(d) > 0), key=_quality, reverse=True)
        # N사 블로그 위주(최대 max_posts-1개) + 다른 블로그 1개, 부족하면 남은 상위 글로 채움
        naver = [d for d in ranked if d["source"] == "N사 블로그"][:max(1, max_posts - 1)]
        other = [d for d in ranked if d["source"] != "N사 블로그"][:1]
        picked = naver + other
        picked += [d for d in ranked if d not in picked][:max_posts - len(picked)]
        for d in picked:
            logger.info(f"📚 참고 글: [{d['source']}] {d['title'][:40]} ({d['chars']}자) {d['url']}")
        return picked
    except Exception as e:
        logger.warning(f"참고 글 수집 중 오류: {e}")
        return []


def build_reference_brief(docs: List[Dict], per_post_chars: int = 1500) -> str:
    """프롬프트에 넣을 참고 자료 요약 블록 (제목·소제목 구성·본문 발췌)."""
    parts = []
    for i, d in enumerate(docs, 1):
        excerpt, size = [], 0
        for p in d["paras"]:
            if size >= per_post_chars:
                break
            excerpt.append(p)
            size += len(p)
        heads = " / ".join(d["headings"][:10]) or "(소제목 없음)"
        parts.append(f"[참고 글 {i} · {d['source']}]\n제목: {d['title']}\n소제목 구성: {heads}\n본문 발췌:\n" + "\n".join(excerpt))
    return "\n\n".join(parts)


def _ngrams(text: str, n: int = 12) -> set:
    t = re.sub(r"\s+", "", text)
    return {t[i:i + n] for i in range(0, max(0, len(t) - n + 1))}


def copy_ratio(generated_lines: List[str], docs: List[Dict], n: int = 12) -> float:
    """생성 원고의 공백 제거 12글자 연속 구간 중 참고 글과 그대로 겹치는 비율 (0~1)."""
    gen = _ngrams(" ".join(generated_lines), n)
    if not gen or not docs:
        return 0.0
    ref = set()
    for d in docs:
        ref |= _ngrams(" ".join(d["paras"]), n)
    return len(gen & ref) / len(gen)
