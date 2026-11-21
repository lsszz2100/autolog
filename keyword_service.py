"""N사 실시간 황금키워드 및 연관검색어 자동 추출 모듈 (keyword_service.py)

N사 검색창의 실시간 자동완성 API(ac.search.naver.com)를 호출하여
실제 N사 검색 사용자들이 가장 많이 찾는 실시간 롱테일 '황금키워드' 목록을 수집합니다.
"""
import json
import logging
import urllib.request
import urllib.parse
from typing import List

logger = logging.getLogger("naver.keyword")

def get_naver_golden_keywords(query: str, max_count: int = 10) -> List[str]:
    """
    사용자가 입력한 검색어의 N사 실시간 자동완성 및 연관 황금키워드 목록을 반환합니다.
    """
    if not query:
        return []
    
    encoded = urllib.parse.quote(query.strip())
    url = f"https://ac.search.naver.com/nx/ac?q={encoded}&con=1&frm=nv&ans=2&r_format=json&r_enc=UTF-8&r_unicode=0&t_koreng=1&run=2&rev=4&q_enc=UTF-8&st=100"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "Referer": "https://www.naver.com/"
    }
    
    results = [query.strip()]
    # 붙임말 확장: 한 단어만 넣어도 실제로 많이 검색되는 조합(추천·후기·가격 등)을 함께 모음
    try:
        from topic_service import _expand_autocomplete
        seen = {query.strip().replace(" ", "")}
        for kw, _i, _m in sorted(_expand_autocomplete(query.strip()), key=lambda x: x[1]):
            key = kw.replace(" ", "")
            if key not in seen and len(kw) <= 40:
                seen.add(key)
                results.append(kw)
    except Exception as e:
        logger.debug(f"자동완성 확장 실패: {e}")
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("items", [])
            if items and len(items) > 0:
                for sublist in items[0]:
                    if sublist and len(sublist) > 0:
                        kw = sublist[0].strip()
                        if kw and kw not in results:
                            results.append(kw)
    except Exception as e:
        logger.warning(f"N사 자동완성 API 조회 실패 ({e}), 템플릿 황금키워드로 폴백합니다.")
    
    # 롱테일 폴백 생성
    fallbacks = [
        f"{query} 추천",
        f"{query} 비교",
        f"{query} 가격",
        f"{query} 솔직후기",
        f"{query} 장단점",
        f"{query} 사용법",
        f"{query} 꿀팁",
        f"{query} 주의점"
    ]
    for fb in fallbacks:
        if fb not in results:
            results.append(fb)
            
    logger.info(f"'{query}' 관련 N사 황금키워드 {len(results[:max_count])}개 추출 완료: {results[:max_count]}")
    return results[:max_count]
