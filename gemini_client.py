"""Google Gemini REST 호출 공용 모듈 (gemini_client.py)

- ai.py(원고), image_service.py(사진 기획), app.py(키 진단)가 함께 사용
- API 키는 URL 쿼리(?key=)가 아니라 x-goog-api-key 헤더로 전송 (예외 메시지·프록시 로그에 키가 남지 않게)
- 모델 캐스케이드: 앞 모델부터 시도, 503/429·오류·응답 검증 실패는 바로 다음 모델로 (마지막 모델만 503/429 1회 재시도)
"""
import json
import time
import logging
import urllib.error
import urllib.request
from typing import Any, Callable, Optional, Sequence, Tuple

logger = logging.getLogger("naver.gemini")

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiError(RuntimeError):
    def __init__(self, message: str, invalid_key: bool = False, daily_quota: bool = False):
        super().__init__(message)
        self.invalid_key = invalid_key
        self.daily_quota = daily_quota   # 하루 무료 사용량 소진 (구글 기준 태평양 자정 = 한국 16~17시 초기화)


DAILY_QUOTA_MSG = "오늘 Gemini 무료 사용량을 모두 썼습니다 (매일 오후 5시쯤 초기화)"


def is_daily_quota(err: BaseException) -> bool:
    """예외(또는 그 원인 사슬)가 하루 사용량 소진인지."""
    seen = 0
    while err is not None and seen < 5:
        if getattr(err, "daily_quota", False):
            return True
        err, seen = (err.__cause__ or err.__context__), seen + 1
    return False


def _post(model: str, api_key: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        ENDPOINT.format(model=model),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _with_thinking(body: dict, model: str) -> dict:
    """gemini-3.x 는 기본 '생각' 단계가 길어 maxOutputTokens 를 다 쓰고(MAX_TOKENS 잘림) 25초 제한도 넘긴다
    (2026-11-16 실측: 3.5-flash, 300토큰 중 285 생각·30.5초). 호출자가 thinkingConfig 를 정하지 않았으면 low 로."""
    if not (model.startswith("gemini-3") or model.endswith("-latest")):
        return body
    cfg = body.get("generationConfig") or {}
    if "thinkingConfig" in cfg:
        return body
    out = dict(body)
    out["generationConfig"] = {**cfg, "thinkingConfig": {"thinkingLevel": "low"}}
    return out


def generate(api_key: str, prompt: str, models: Sequence[str], *,
             system: Optional[str] = None,
             generation_config: Optional[dict] = None,
             timeout: float = 25,
             accept: Optional[Callable[[str], Any]] = None,
             verbose: bool = True,
             images: Sequence[Tuple[str, str]] = ()) -> Tuple[Any, str]:
    """models를 순서대로 시도해 (결과, 모델명)을 반환.

    accept(text)가 주어지면 그 반환값을 결과로 쓰고, None을 반환하거나 예외가 나면 다음 모델로 넘어간다.
    images: (mime 형식, base64 데이터) 목록 — 사진을 함께 보내 내용을 읽게 할 때.
    모든 모델 실패 시 GeminiError.
    """
    key = (api_key or "").strip()
    if not key:
        raise GeminiError("Gemini API 키가 비어 있습니다.")
    parts: list = [{"inline_data": {"mime_type": m, "data": d}} for m, d in images]
    body: dict = {"contents": [{"role": "user", "parts": parts + [{"text": prompt}]}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if generation_config:
        body["generationConfig"] = generation_config

    last_err = "시도한 모델 없음"
    tried = daily_hits = 0
    for model in models:
        tried += 1
        if verbose:
            logger.info(f"▶ Gemini AI 모델 시도: {model}...")
        body_m = _with_thinking(body, model)
        for attempt in range(1, 3):
            try:
                data = _post(model, key, body_m, timeout)
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                result = accept(text) if accept else text
                if result is None:
                    last_err = f"{model} (응답 형식 불일치)"
                    break
                return result, model
            except urllib.error.HTTPError as he:
                detail = he.read().decode("utf-8", errors="ignore")
                if he.code == 400 and "API_KEY_INVALID" in detail:
                    raise GeminiError("유효하지 않은 Gemini API 키입니다.", invalid_key=True)
                # 이 모델이 thinkingLevel 을 받지 않으면(400) 설정을 빼고 한 번 더
                if he.code == 400 and body_m is not body and "thinking" in detail.lower() and attempt < 2:
                    body_m = body
                    continue
                last_err = f"{model} (HTTP {he.code})"
                if he.code == 429 and "PerDay" in detail:
                    daily_hits += 1
                if verbose:
                    logger.warning(f"  ⚠️ {model} HTTP {he.code} (시도 {attempt}/2): {detail[:90]}")
                # 과부하(503)·한도(429)는 같은 모델을 다시 기다리지 않고 바로 다음 모델로 (마지막 모델만 1회 재시도)
                if he.code in (503, 429) and attempt < 2 and model == models[-1]:
                    time.sleep(1.0)
                    continue
                break
            except Exception as e:
                last_err = f"{model} ({type(e).__name__}: {e})"
                if verbose:
                    logger.warning(f"  ⚠️ {model} 호출 오류: {e}")
                break
    # 시도한 모델의 절반 이상이 '하루 한도' 429 → 오늘은 더 해도 안 됨
    if daily_hits and daily_hits * 2 >= tried:
        raise GeminiError(f"{DAILY_QUOTA_MSG} ({last_err})", daily_quota=True)
    raise GeminiError(f"모든 Gemini 모델 호출 실패 ({last_err})")


def extract_json(text: str, opener: str = "{", closer: str = "}") -> Any:
    """응답 텍스트에서 첫 opener ~ 마지막 closer 구간을 JSON으로 파싱. 실패 시 None."""
    start, end = text.find(opener), text.rfind(closer)
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None
