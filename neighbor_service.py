"""오토로그 (AutoLog) 자동 이웃 순회 엔진 (neighbor_service.py)

N사 블로그 이웃 생태계와의 자연스럽고 지능적인 상호작용(소통 품앗이 및 서이추 발굴)을 완전 자동화:
1. 타깃 발굴:
   - 내 이웃 새글 피드 (m.blog.naver.com/FeedList.naver) 실시간 순회
   - 키워드 검색 탐색 (SectionSearch.naver)을 통한 잠재 서이추 타깃 발굴
   - 추천/탐색 피드 (Recommendation.naver) 순회
2. 자동 액션:
   - 공감(좋아요) 확률형 자동 클릭 (aria-pressed 상태 검사 및 중복 방지)
   - Google Gemini AI 연동 포스팅 본문 분석 기반 맞춤형 고품질 댓글 생성 (템플릿 치환 폴백)
   - 서로이웃(BothBuddy) 맞춤 멘트 자동 신청
3. 안티 어뷰징 & 계정 보호:
   - 일일 쿼터 제한 (공감 최대 100건, 서이추 최대 100건 등 N사 권장량 엄수)
   - 액션 간 랜덤 지연 시간 (기본 15초~35초) 난수 부여
   - 중복 방문 및 블랙리스트/스팸 블로그 원천 차단
   - 실시간 백그라운드 스레드 제어 (시작/정지/일시정지/라이브 로그 스트리밍)
"""
import json
import logging
import os
import random
import re
import threading
import time
import urllib.parse
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from browser import launch_naver_context, first_page
import gemini_client
import ai

logger = logging.getLogger("naver.neighbor")

CONFIG_FILE = config.DATA_DIR / "neighbor_config.json"
HISTORY_FILE = config.DATA_DIR / "neighbor_history.json"
BLACKLIST_FILE = config.DATA_DIR / "neighbor_blacklist.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "account_id": "",           # 순회 대상 계정 ID (공백 시 현재 기본 활성 계정)
    "enabled_actions": {
        "sympathy": True,
        "comment": True,
        "buddy_request": True,
    },
    "probabilities": {
        "sympathy": 0.90,       # 90% 확률로 공감 클릭
        "comment": 0.40,        # 40% 확률로 댓글 작성
        "buddy_request": 0.35,  # 35% 확률로 서이추 신청 (미이웃 시)
    },
    "delays": {
        "min_seconds": 45,
        "max_seconds": 90,
    },
    "stall_minutes": 10,        # 이 시간 동안 공감·댓글·이웃 신청이 한 건도 안 되면 순회를 멈추고 안내 (0 = 끔)
    "daily_limits": {
        "sympathy": 80,         # 보호조치 예방 권장치
        "comment": 30,
        "buddy_request": 30,
    },
    "account_limits": {},       # 계정별 일일 한도 {계정ID: {sympathy, comment, buddy_request}} — 없으면 daily_limits 사용
    "discovery_mode": "feed",   # "feed" | "keyword" | "recommend" | "target"
    "keywords": ["일상", "맛집", "소통", "서이추", "취미", "리뷰", "육아", "여행", "재테크"],
    "target_blogs": [],         # 🎯 타깃 블로그 ID / URL 목록 (경쟁사/인플루언서 벤치마킹)
    "comment_mode": "ai",       # "ai" (Gemini LLM) | "template" | "mixed" (AI우선 실패시 템플릿)
    "comment_templates": [
        "{닉네임}님, 유익하고 정성스러운 포스팅 잘 읽고 갑니다! 오늘도 행복한 하루 보내세요 :)",
        "{닉네임}님 피드 보고 반가워서 흔적 남겨요. 유용한 정보 감사드리고 자주 소통해요!",
        "{닉네임}님 글 깔끔하게 정리되어 있어서 몰입해서 읽었네요. 앞으로도 좋은 글 기대할게요!",
        "{닉네임}님 포스팅 오늘도 잘 보고 갑니다. 편안하고 즐거운 시간 되세요~",
        "{닉네임}님 글에 완전 공감하고 갑니다! 좋은 하루 보내시길 바랄게요 :)"
    ],
    "buddy_mode": "ai",         # "ai" (Gemini LLM 본문 분석) | "mixed" | "templates" | "custom"
    "buddy_templates": [
        "안녕하세요 {닉네임}님! 정성스러운 포스팅 잘 보고 갑니다. 관심사가 비슷해 자주 소통하고 싶어 신청드려요 :)",
        "{닉네임}님 올려주신 유익한 포스팅 재미있게 읽었습니다. 앞으로 좋은 이웃으로 친하게 지내요~",
        "{닉네임}님 피드 글들이 알차서 인상 깊네요! 함께 진솔하게 소통하는 이웃이 되고 싶어 신청합니다 ^^",
        "안녕하세요 {닉네임}님, 좋은 글 공유 감사드려요. 서로 긍정적인 자극 주고받는 찐이웃 되고 싶어요!",
        "{닉네임}님 글 꼼꼼히 읽고 공감 누르고 갑니다! 앞으로도 자주 왕래하며 따뜻하게 소통해요 :)",
        "포스팅 분위기가 너무 좋아서 한참 머물다 갑니다 {닉네임}님! 서로이웃 맺고 일상 함께 나누고 싶어요~",
        "안녕하세요 {닉네임}님! 알찬 꿀팁 가득한 글 잘 보았습니다. 서로 응원하며 소통하는 서로이웃 부탁드려요!",
        "{닉네임}님 반가워요! 진정성 있는 포스팅에 반해 이웃 신청드립니다. 자주 찾아뵙고 소통할게요 :)"
    ],
    "buddy_message_template": "안녕하세요 {닉네임}님! 좋은 글 잘 보고 소통하고 싶어 서로이웃 신청드립니다. 자주 왕래하며 소통해요 :)",
    "ai_comment_prompt": (
        f"당신은 {config.PORTAL_KO} 블로그 이웃과 진정성 있게 소통하는 따뜻한 이웃 블로거입니다. "
        "주어진 블로그 글의 제목과 본문 내용을 읽고, 글을 실제로 꼼꼼히 읽었음이 자연스럽게 드러나는 "
        "1~3문장의 따뜻하고 친근한 댓글을 작성하세요.\n\n"
        "작성 원칙:\n"
        "1. 기계적이거나 상투적인 스팸 문구('좋은 글 잘 보고 갑니다', '제 블로그도 놀러오세요' 등)는 절대 금지합니다.\n"
        "2. 본문에서 언급된 구체적인 포인트(장소, 음식, 경험, 감정, 사진 내용 등) 1가지를 자연스럽게 짚으며 칭찬하거나 공감하세요.\n"
        "3. 부드러운 한국어 구어체 존댓말(~해요, ~네요, ~더라고요 등)을 사용하세요.\n"
        "4. 이모지는 1~2개 이내로 과하지 않게 사용하세요.\n"
        "5. 오직 작성할 댓글 내용 텍스트만 출력하세요 (따옴표나 설명 제외)."
    ),
    "ai_buddy_prompt": (
        f"당신은 {config.PORTAL_KO} 블로그에서 진정성 있는 이웃과 지속적으로 교류하고 싶어하는 블로거입니다. "
        "주어진 블로그 글의 작성자 닉네임, 글 제목, 본문 내용을 참고하여 "
        "상대방의 포스팅과 관심사에 진심으로 공감하며 서로이웃(서이추)을 정중하고 친근하게 신청하는 멘트를 작성하세요.\n\n"
        "작성 원칙:\n"
        "1. 글자 수 엄수: 공백 포함 반드시 40자 이상 85자 이하로 작성하세요 (100자 초과 시 N사에서 거절됨).\n"
        "2. 본문이나 제목에서 다룬 관심사/주제/포인트(예: 맛집, 일상, 여행, 취미 등)를 1단어 자연스럽게 언급하세요.\n"
        "3. 기계적이고 뻔한 문구('기본 멘트', '제 블로그도 놀러오세요' 등)는 절대 금지합니다.\n"
        "4. 예의 바르고 따뜻한 한국어 구어체 존댓말을 사용하세요.\n"
        "5. 이모지는 1개 이내로 과하지 않게 사용하세요.\n"
        "6. 오직 신청 메시지 텍스트만 출력하세요 (따옴표나 설명 제외)."
    ),
    "auto_headless": True,
}


# ── 보호조치(계정 일시 제한) 예방: 설정값과 상관없이 지키는 최소 안전선 ──
SAFE_MIN_DELAY = 40          # 글과 글 사이 최소 대기(초). 설정이 더 짧아도 이 값으로 올린다
SAFE_MIN_SPAN = 30           # 최대 대기는 최소 대기보다 이만큼은 길게 (간격이 일정하면 기계처럼 보임)
HOURLY_CAPS = {"sympathy": 40, "comment": 12, "buddy_request": 10}   # 최근 1시간 동안 동작별 최대 횟수
REST_EVERY = (8, 14)         # 글을 이만큼 방문할 때마다
REST_SECONDS = (240, 540)    # 4~9분 쉰다
BLOCK_URL_HINTS = ("nid.naver.com/user2/help", "nid.naver.com/nidlogin", "/protect", "captcha")
BLOCK_TEXT_HINTS = ("보호조치", "보호 조치", "비정상적인 활동", "비정상적인 접근", "자동입력 방지", "자동 입력 방지",
                    "이용이 제한", "이용 제한", "일시적으로 제한", "사용이 제한")


def safe_delays(cfg: Dict[str, Any]) -> Tuple[float, float]:
    d = cfg.get("delays") or {}
    lo = max(SAFE_MIN_DELAY, float(d.get("min_seconds") or 0))
    hi = max(lo + SAFE_MIN_SPAN, float(d.get("max_seconds") or 0))
    return lo, hi


def load_neighbor_config() -> Dict[str, Any]:
    """이웃 순회 설정 로드"""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            merged = dict(DEFAULT_CONFIG)
            merged.update(data)
            for k in ("enabled_actions", "probabilities", "delays", "daily_limits"):
                if k in data and isinstance(data[k], dict):
                    nested = dict(DEFAULT_CONFIG.get(k, {}))
                    nested.update(data[k])
                    merged[k] = nested
            if not merged.get("buddy_templates"):
                merged["buddy_templates"] = list(DEFAULT_CONFIG["buddy_templates"])
            return merged
        except Exception as e:
            logger.warning(f"이웃 순회 설정 로드 실패 (기본값 사용): {e}")
    return dict(DEFAULT_CONFIG)


def limits_for(cfg: Dict[str, Any], account: str) -> Dict[str, int]:
    """계정별 일일 한도 (계정 전용 값이 없으면 공통 daily_limits)"""
    base = dict(DEFAULT_CONFIG["daily_limits"])
    base.update(cfg.get("daily_limits") or {})
    own = (cfg.get("account_limits") or {}).get(account or "")
    if isinstance(own, dict):
        for k in base:
            try:
                if k in own and own[k] not in (None, ""):
                    base[k] = max(0, int(own[k]))
            except (TypeError, ValueError):
                pass
    return base


def _hist_account(item: Dict[str, Any], legacy: str) -> str:
    return item.get("account") or legacy


def save_neighbor_config(cfg: Dict[str, Any]):
    """이웃 순회 설정 저장"""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def load_neighbor_history() -> List[Dict[str, Any]]:
    """이웃 순회 이력 로드"""
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_neighbor_history(items: List[Dict[str, Any]]):
    """이웃 순회 이력 저장 (최근 2,000건 유지)"""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if len(items) > 2000:
        items = items[-2000:]
    HISTORY_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def load_neighbor_blacklist() -> List[str]:
    """블랙리스트 ID 목록 로드"""
    if BLACKLIST_FILE.exists():
        try:
            return json.loads(BLACKLIST_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_neighbor_blacklist(items: List[str]):
    """블랙리스트 ID 목록 저장"""
    BLACKLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    clean = sorted(list(set(b.strip().lower() for b in items if b.strip())))
    BLACKLIST_FILE.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


class NeighborBot:
    """N사 자동 이웃 순회 및 소통 봇"""

    _instance: Optional["NeighborBot"] = None
    _lock = threading.RLock()

    @classmethod
    def get_instance(cls) -> "NeighborBot":
        with cls._lock:
            if cls._instance is None:
                cls._instance = NeighborBot()
            return cls._instance

    def __init__(self):
        self.state = "idle"  # idle | running | paused | stopping
        self.current_account: str = ""
        self.current_profile_dir: Optional[Path] = None
        self.worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # set = 진행 중, clear = 일시정지
        self.stop_reason = ""         # 자동으로 멈춘 이유(화면에 안내)
        self.stop_reason_at = 0.0
        self.buddy_count = None       # 시작할 때 읽은 내 이웃 수
        self._skip_buddy = False      # 5,000명 한도에 닿아 이번 순회에서 이웃 신청을 건너뜀
        self._last_progress = 0.0
        self._recent_actions: Dict[str, List[float]] = {}   # 동작별 최근 실행 시각 (시간당 상한용)

        self.current_target: Optional[Dict[str, Any]] = None
        self.recent_logs: List[Dict[str, str]] = []
        self._log_lock = threading.Lock()

        # 당일 액션 통계
        self.today_counts = {
            "date": str(date.today()),
            "sympathy": 0,
            "comment": 0,
            "buddy_request": 0,
            "total_visited": 0,
        }
        self._refresh_today_counts("")

    def _log(self, level: str, message: str):
        now_str = datetime.now().strftime("%H:%M:%S")
        entry = {"time": now_str, "level": level, "msg": message}
        with self._log_lock:
            self.recent_logs.append(entry)
            if len(self.recent_logs) > 100:
                self.recent_logs = self.recent_logs[-100:]
        if level == "error":
            logger.error(f"[이웃순회] {message}")
        elif level == "warning":
            logger.warning(f"[이웃순회] {message}")
        else:
            logger.info(f"[이웃순회] {message}")

    def _resolve_account(self, cfg: Optional[Dict[str, Any]] = None) -> str:
        if self.current_account:
            return self.current_account
        cfg = cfg or load_neighbor_config()
        acc = (cfg.get("account_id") or "").strip()
        if not acc:
            acc = config.load_config().get("active_account") or ""
        return acc

    def _refresh_today_counts(self, account: Optional[str] = None):
        """오늘(자정 기준) 카운트 — 계정별로 따로 센다. 날짜가 바뀌면 자동으로 0부터 다시 시작"""
        today_s = str(date.today())
        if account is None:
            account = self._resolve_account()
        try:
            app_cfg = config.load_config()
            accs = app_cfg.get("accounts") or []
            legacy = app_cfg.get("legacy_account") or (accs[0].get("naver_id") if accs else "") or account
        except Exception:
            legacy = account
        history = load_neighbor_history()
        today_hist = [h for h in history if h.get("date") == today_s and _hist_account(h, legacy) == account]
        self.today_counts = {
            "date": today_s,
            "sympathy": sum(1 for h in today_hist if "sympathy" in h.get("actions", [])),
            "comment": sum(1 for h in today_hist if "comment" in h.get("actions", [])),
            "buddy_request": sum(1 for h in today_hist if "buddy_request" in h.get("actions", [])),
            "total_visited": len(today_hist),
        }

    def get_status(self) -> Dict[str, Any]:
        """현재 봇 상태 및 통계 반환"""
        cfg = load_neighbor_config()
        acc = self._resolve_account(cfg)
        self._refresh_today_counts(acc)
        with self._log_lock:
            logs = list(self.recent_logs)
        return {
            "state": self.state,
            "current_account": self.current_account,
            "target_account": cfg.get("account_id", ""),
            "today_counts": self.today_counts,
            "daily_limits": limits_for(cfg, acc),
            "status_account": acc,
            "current_target": self.current_target,
            "recent_logs": logs,
            "discovery_mode": cfg.get("discovery_mode", "feed"),
            "probabilities": cfg.get("probabilities", {}),
            "buddy_mode": cfg.get("buddy_mode", "ai"),
            "stop_reason": self.stop_reason,
            "stop_reason_at": self.stop_reason_at,
            "buddy_count": self.buddy_count,
        }

    def start(self, override_options: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """순회 시작"""
        with self._lock:
            if self.state in ("running", "paused"):
                return False, "이미 이웃 순회가 실행 중입니다."

            if override_options:
                cfg = load_neighbor_config()
                cfg.update(override_options)
                save_neighbor_config(cfg)

            self.state = "running"
            self._stop_event.clear()
            self._pause_event.set()
            self._log("info", "이웃 순회 작업을 시작합니다.")

            self.worker_thread = threading.Thread(
                target=self._run_loop, name="NeighborBotWorker", daemon=True
            )
            self.worker_thread.start()
            return True, "이웃 순회가 시작되었습니다."

    def stop(self) -> Tuple[bool, str]:
        """순회 정지"""
        with self._lock:
            if self.state == "idle":
                return True, "이미 대기 상태입니다."
            self.state = "stopping"
            self._stop_event.set()
            self._pause_event.set()
            self._log("info", "순회 중지 신호를 보냈습니다. 현재 작업 완료 후 안전하게 종료됩니다.")
            return True, "순회가 정지됩니다."

    def pause(self) -> Tuple[bool, str]:
        """일시정지 토글"""
        with self._lock:
            if self.state == "running":
                self.state = "paused"
                self._pause_event.clear()
                self._log("warning", "이웃 순회가 일시 정지되었습니다.")
                return True, "일시 정지되었습니다."
            elif self.state == "paused":
                self.state = "running"
                self._last_progress = time.time()   # 쉬는 동안은 '성과 없음' 시간에 넣지 않는다
                self._pause_event.set()
                self._log("info", "이웃 순회를 재개합니다.")
                return True, "재개되었습니다."
            return False, "실행 중인 작업이 없습니다."

    def _all_done_today(self, cfg: Dict[str, Any], account: str) -> bool:
        """켜 둔 동작이 모두 오늘 한도에 닿았는지. 꺼 둔 동작·확률 0%·한도 0인 동작은 따지지 않는다."""
        limits = limits_for(cfg, account)
        enabled = cfg.get("enabled_actions", {})
        probs = cfg.get("probabilities", {})
        active = [k for k in ("sympathy", "comment", "buddy_request")
                  if enabled.get(k, True) and float(probs.get(k, 1) or 0) > 0 and limits.get(k, 0) > 0
                  and not (k == "buddy_request" and self._skip_buddy)]
        return all(self.today_counts.get(k, 0) >= limits.get(k, 0) for k in active)

    def _stalled(self, cfg: Dict[str, Any]) -> bool:
        """정해 둔 시간 동안 아무 성과가 없으면 멈추고 다른 방법을 안내한다."""
        mins = int(cfg.get("stall_minutes", 10) or 0)
        if mins > 0:
            mins = max(mins, 10)   # 대기 간격이 길어져 5분은 너무 짧다
        if mins <= 0 or time.time() - self._last_progress < mins * 60:
            return False
        names = {"feed": "추천 탐색 피드", "keyword": "키워드 검색 발굴", "target": "타깃 블로그 발굴", "recommend": "추천 블로그"}
        cur = cfg.get("discovery_mode", "feed")
        others = " · ".join(v for k, v in names.items() if k != cur and k != "recommend")
        reason = (f"{mins}분 동안 공감·댓글·이웃 신청이 한 건도 되지 않아 순회를 멈췄습니다. "
                  f"N사에서 잠시 막혔을 수 있으니 잠시 후 다시 시도하거나, 발굴 방식을 [{others}]로 바꿔 시도해 주세요.")
        if self._skip_buddy:
            reason += " (이웃 수가 5,000명 한도에 닿아 이웃 신청은 건너뛰는 중입니다)"
        self._log("warning", reason)
        self.stop_reason = reason
        self.stop_reason_at = time.time()
        self._stop_event.set()
        return True

    def _license_stopped(self) -> bool:
        try:
            import licensing
            if licensing.is_usable():
                return False
        except Exception:
            return False
        self._log("warning", "라이선스를 사용할 수 없어 이웃 순회를 멈춥니다. 라이선스 키를 확인해 주세요.")
        self.stop_reason = "라이선스를 사용할 수 없어 순회를 멈췄습니다."
        self.stop_reason_at = time.time()
        self._stop_event.set()
        return True

    def _finish_all_done(self):
        c = self.today_counts
        self._log("info", f"오늘 할 일을 모두 마쳤습니다 (공감 {c.get('sympathy', 0)} · 댓글 {c.get('comment', 0)} · "
                          f"이웃 신청 {c.get('buddy_request', 0)}). 더 방문하지 않고 순회를 자동으로 멈춥니다.")
        self.stop_reason = "오늘 정한 공감·댓글·이웃 신청을 모두 마쳐 순회를 멈췄습니다."
        self.stop_reason_at = time.time()
        self._stop_event.set()

    def _hourly_ok(self, action: str) -> bool:
        """최근 1시간 동안 action 이 HOURLY_CAPS 미만이면 True (몰아서 하면 보호조치가 걸리기 쉬움)."""
        now = time.time()
        lst = [t for t in self._recent_actions.get(action, []) if now - t < 3600]
        self._recent_actions[action] = lst
        return len(lst) < HOURLY_CAPS.get(action, 10 ** 6)

    def _mark_action(self, action: str):
        self._recent_actions.setdefault(action, []).append(time.time())

    def _wait_hourly(self, cfg: Dict[str, Any], account: str) -> bool:
        """켜 둔 동작이 모두 시간당 상한에 닿았으면 하나가 풀릴 때까지 쉰다. 중지되면 True."""
        limits = limits_for(cfg, account)
        enabled = cfg.get("enabled_actions", {})
        probs = cfg.get("probabilities", {})
        active = [k for k in ("sympathy", "comment", "buddy_request")
                  if enabled.get(k, True) and float(probs.get(k, 1) or 0) > 0
                  and self.today_counts.get(k, 0) < limits.get(k, 0)
                  and not (k == "buddy_request" and self._skip_buddy)]
        if not active or any(self._hourly_ok(k) for k in active):
            return False
        free_at = min(min(self._recent_actions[k]) + 3600 for k in active)
        wait = max(60.0, free_at - time.time()) + random.uniform(30, 120)
        self._log("info", f"보호조치 예방: 최근 1시간 동작 수가 상한에 닿아 {int(wait // 60)}분 쉬었다가 이어갑니다.")
        self._last_progress = time.time() + wait
        return self._wait_delay(wait, wait)

    def _check_blocked(self, page) -> bool:
        """보호조치·자동입력 방지·이용 제한 화면이면 순회를 즉시 멈추고 True."""
        try:
            url = (page.url or "").lower()
            # 정상 글 화면(본문 영역 있음)이면 글 내용에 '보호조치' 같은 단어가 있어도 무시한다
            text = page.evaluate("""() => document.querySelector('.se-main-container, #postViewArea, .post_ct, .u_cbox')
                ? '' : (document.body ? document.body.innerText : '').slice(0, 4000)""") or ""
        except Exception:
            return False
        hit = next((h for h in BLOCK_URL_HINTS if h in url), None) or next((h for h in BLOCK_TEXT_HINTS if h in text), None)
        if not hit:
            return False
        self._log("error", f"계정 보호조치·이용 제한 화면이 감지되어 순회를 즉시 멈춥니다 (감지: {hit}). "
                           "N사 블로그에 직접 로그인해 보호조치를 풀고, 하루 정도 쉰 뒤 한도를 낮춰 다시 시작하세요.")
        self.stop_reason = "보호조치(이용 제한) 화면이 떠서 순회를 멈췄습니다. 직접 로그인해 해제한 뒤 하루 쉬고 다시 시작하세요."
        self.stop_reason_at = time.time()
        self._stop_event.set()
        return True

    def _wait_delay(self, min_sec: int, max_sec: int) -> bool:
        """지연 시간 대기 (중지 이벤트 감지 시 즉시 True 반환)"""
        delay = round(random.uniform(min_sec, max_sec), 1)
        self._log("info", f"다음 작업까지 {delay}초간 대기합니다...")
        end_time = time.time() + delay
        while time.time() < end_time:
            if self._stop_event.is_set():
                return True
            time.sleep(0.5)
        return False

    def _run_loop(self):
        """메인 백그라운드 순회 루프"""
        app_cfg = config.load_config()
        cfg = load_neighbor_config()
        blacklist = set(load_neighbor_blacklist())
        headless = cfg.get("auto_headless", True)

        # 1. 대상 계정 및 독립 프로필 확인
        target_account = (cfg.get("account_id") or "").strip()
        if not target_account:
            target_account = app_cfg.get("active_account") or ""
        self.current_account = target_account

        try:
            profile_dir = config.account_profile_dir(app_cfg, target_account)
            acc = config.find_account(app_cfg, target_account)
            slot = (acc or {}).get("slot", 1)

            # Windows 프로필 우선 폴백 (개발/WSL/크로스플랫폼 지원)
            win_name = "naver_profile_win" if slot == 1 else f"naver_profile_win_{slot}"
            win_profile = config.DATA_DIR / win_name
            if win_profile.exists() and (win_profile / "Default" / "Network" / "Cookies").exists():
                profile_dir = win_profile

            self.current_profile_dir = profile_dir
            acc_label = f"[{slot}번 계정: {target_account}]" if target_account else "[기본 활성 계정]"
            self._log("info", f"브라우저 환경 준비 중 (순회 실행 계정: {acc_label}, 프로필: {Path(profile_dir).name})...")

            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                ctx = launch_naver_context(pw, headless=headless, profile_dir=profile_dir)
                page = first_page(ctx)

                # 내 이웃 수 확인: N사는 이웃을 5,000명까지만 둘 수 있어 그 근처면 이웃 신청이 실패한다
                self._skip_buddy = False
                self.stop_reason = ""
                if cfg.get("enabled_actions", {}).get("buddy_request", True):
                    my_blog = _admin_target(target_account)[0]
                    cnt = buddy_count(page, my_blog) if my_blog else None
                    self.buddy_count = cnt
                    if cnt is not None:
                        if cnt >= BUDDY_LIMIT - 10:
                            self._skip_buddy = True
                            self._log("warning", f"내 이웃이 {cnt:,}명으로 5,000명 한도에 닿아 이번 순회에서는 이웃 신청을 건너뜁니다. "
                                                 "[유령 이웃 정리]·[무응답 신청 취소]로 자리를 비우면 다시 신청할 수 있습니다.")
                        elif cnt >= 4800:
                            self._log("warning", f"내 이웃이 {cnt:,}명입니다. 5,000명을 넘으면 이웃 신청이 되지 않으니 유령 이웃 정리를 권합니다.")
                        else:
                            self._log("info", f"내 이웃 {cnt:,}명 (5,000명까지 {BUDDY_LIMIT - cnt:,}명 남음)")

                # 타깃 수집 및 순회 루프
                visited_in_session = set()
                self._last_progress = time.time()
                visits, next_rest = 0, random.randint(*REST_EVERY)

                while not self._stop_event.is_set():
                    self._pause_event.wait()
                    if self._license_stopped():
                        break
                    self._refresh_today_counts(target_account)

                    # 켜 둔 동작(공감·댓글·이웃 신청)을 오늘 한도까지 모두 마쳤으면 더 방문하지 않고 순회를 끝낸다
                    if self._all_done_today(cfg, target_account):
                        self._finish_all_done()
                        break

                    # 타깃 포스팅 목록 수집
                    targets = self._fetch_targets(page, cfg)
                    if not targets:
                        if self._stalled(cfg):
                            break
                        self._log("warning", "수집된 새로운 타깃 포스팅이 없습니다. 30초 후 재시도합니다.")
                        if self._wait_delay(20, 35):
                            break
                        continue

                    self._log("info", f"새로운 타깃 포스팅 {len(targets)}개를 발굴하였습니다.")

                    for target in targets:
                        if self._stop_event.is_set():
                            break
                        self._pause_event.wait()
                        # 라이선스가 정지·만료·로그아웃되면 순회를 바로 멈춘다 (켜 둔 채로 계속 쓰는 것 방지)
                        if self._license_stopped():
                            break
                        # 글 하나를 마칠 때마다 확인: 다 채웠으면 남은 글은 방문하지 않는다
                        self._refresh_today_counts(target_account)
                        if self._all_done_today(cfg, target_account):
                            self._finish_all_done()
                            break

                        if self._wait_hourly(cfg, target_account):
                            break
                        post_url = target["url"]
                        blog_id = target.get("blog_id", "")
                        log_no = target.get("log_no", "")

                        # 중복 및 블랙리스트 검사
                        target_key = f"{blog_id}_{log_no}"
                        if target_key in visited_in_session:
                            continue
                        if blog_id.lower() in blacklist:
                            self._log("info", f"블랙리스트 블로그 스킵: {blog_id}")
                            continue

                        # 과거 히스토리 검사 (오늘 이미 방문한 경우 스킵)
                        history = load_neighbor_history()
                        today_s = str(date.today())
                        already_done = any(
                            h.get("blog_id") == blog_id and h.get("log_no") == log_no and h.get("date") == today_s
                            and (h.get("account") or target_account) == target_account
                            for h in history
                        )
                        if already_done:
                            continue

                        visited_in_session.add(target_key)
                        self.current_target = target

                        # 포스팅 방문 및 액션 실행
                        before = sum(v for v in self.today_counts.values() if isinstance(v, int))
                        self._process_post(page, target, cfg)
                        self._refresh_today_counts(target_account)
                        if sum(v for v in self.today_counts.values() if isinstance(v, int)) > before:
                            self._last_progress = time.time()
                        elif self._stalled(cfg):
                            break

                        if self._stop_event.is_set():   # 보호조치 감지 등
                            break
                        # 지연 시간 부여 (보호조치 예방 최소선 적용) + 몇 글마다 길게 쉬기
                        visits += 1
                        if visits >= next_rest:
                            visits, next_rest = 0, random.randint(*REST_EVERY)
                            rest = random.uniform(*REST_SECONDS)
                            self._log("info", f"사람처럼 보이도록 잠시 쉽니다 ({int(rest // 60)}분 {int(rest % 60)}초).")
                            self._last_progress = time.time() + rest   # 쉬는 시간은 '성과 없음'으로 세지 않는다
                            if self._wait_delay(rest, rest):
                                break
                            continue
                        lo, hi = safe_delays(cfg)
                        if self._wait_delay(lo, hi):
                            break

                ctx.close()

        except Exception as e:
            self._log("error", f"순회 실행 중 치명적 예외 발생: {e}")
        finally:
            self.state = "idle"
            self.current_account = ""
            self.current_profile_dir = None
            self.current_target = None
            self._log("info", "이웃 순회 작업이 종료되었습니다.")

    def _fetch_targets(self, page, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        """모드에 따라 타깃 포스팅 목록 크롤링"""
        mode = cfg.get("discovery_mode", "feed")
        targets: List[Dict[str, Any]] = []

        try:
            if mode == "feed":
                # 1. 내 이웃 새글 피드
                self._log("info", "내 이웃 새글 피드(FeedList.naver) 탐색 중...")
                page.goto("https://m.blog.naver.com/FeedList.naver", wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(3000)

                # 스크롤하여 더 많은 피드 로드
                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                page.wait_for_timeout(1500)

                # 링크 파싱
                links = page.evaluate("""() => {
                    const anchors = Array.from(document.querySelectorAll('a'));
                    const result = [];
                    for (const a of anchors) {
                        const href = a.href || '';
                        const m = href.match(/m\\.blog\\.naver\\.com\\/([a-zA-Z0-9_\\-]+)\\/([0-9]+)/);
                        if (m && !href.includes('Recommendation') && !href.includes('FeedList')) {
                            const title = a.innerText ? a.innerText.trim().replace(/\\n/g, ' ') : '';
                            result.push({
                                url: href,
                                blog_id: m[1],
                                log_no: m[2],
                                title: title.substring(0, 100)
                            });
                        }
                    }
                    return result;
                }""")
                targets.extend(links)

            elif mode == "keyword":
                # 2. 키워드 검색 발굴
                keywords = cfg.get("keywords", ["일상", "맛집", "소통"])
                kw = random.choice(keywords)
                self._log("info", f"키워드 [{kw}] 검색 기반 새 이웃 발굴 중...")
                search_url = f"https://m.blog.naver.com/SectionSearch.naver?searchValue={urllib.parse.quote(kw)}"
                page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(3000)

                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                page.wait_for_timeout(1500)

                links = page.evaluate("""() => {
                    const anchors = Array.from(document.querySelectorAll('a'));
                    const result = [];
                    for (const a of anchors) {
                        const href = a.href || '';
                        const m = href.match(/m\\.blog\\.naver\\.com\\/([a-zA-Z0-9_\\-]+)\\/([0-9]+)/);
                        if (m && !href.includes('SectionSearch')) {
                            const title = a.innerText ? a.innerText.trim().replace(/\\n/g, ' ') : '';
                            result.push({
                                url: href,
                                blog_id: m[1],
                                log_no: m[2],
                                title: title.substring(0, 100)
                            });
                        }
                    }
                    return result;
                }""")
                targets.extend(links)

                # Fallback: PC 블로그 검색 탭에서 모바일 URL로 변환 수집
                if len(targets) < 5:
                    pc_search_url = f"https://search.naver.com/search.naver?ssc=tab.blog.all&query={urllib.parse.quote(kw)}"
                    try:
                        page.goto(pc_search_url, wait_until="domcontentloaded", timeout=20000)
                        page.wait_for_timeout(2000)
                        pc_links = page.evaluate("""() => {
                            const anchors = Array.from(document.querySelectorAll('a'));
                            const res = [];
                            for (const a of anchors) {
                                const h = a.href || '';
                                const m = h.match(/blog\\.naver\\.com\\/([a-zA-Z0-9_\\-]+)\\/([0-9]+)/);
                                if (m) {
                                    const m_url = `https://m.blog.naver.com/${m[1]}/${m[2]}`;
                                    res.push({
                                        url: m_url,
                                        blog_id: m[1],
                                        log_no: m[2],
                                        title: (a.innerText || '').trim().replace(/\\n/g, ' ').substring(0, 100)
                                    });
                                }
                            }
                            return res;
                        }""")
                        targets.extend(pc_links)
                    except Exception as pe:
                        logger.debug(f"PC 검색 보완 오류: {pe}")

            elif mode == "target":
                # 3. 🎯 타깃 블로그 기반 활성 이웃 & 댓글 작성자 크롤링
                target_blogs = cfg.get("target_blogs", [])
                if not target_blogs:
                    self._log("warning", "타깃 블로그 목록이 비어 있습니다. 상단 설정에서 벤치마킹할 블로그 아이디를 입력해주세요.")
                else:
                    raw_target = random.choice(target_blogs).strip()
                    m_tid = re.search(r'blog\.naver\.com/([a-zA-Z0-9_\-]+)', raw_target)
                    t_id = m_tid.group(1) if m_tid else raw_target.strip("/ ")
                    
                    self._log("info", f"🎯 타깃 블로그 [{t_id}] 기반 찐 소통 유저 발굴 중...")
                    
                    # (1) 타깃 블로그 최근 포스팅 수집
                    page.goto(f"https://m.blog.naver.com/{t_id}", wait_until="domcontentloaded", timeout=25000)
                    page.wait_for_timeout(2500)
                    
                    post_links = page.evaluate("""() => {
                        const anchors = Array.from(document.querySelectorAll('a'));
                        const res = [];
                        for (const a of anchors) {
                            const h = a.href || '';
                            const m = h.match(/m\\.blog\\.naver\\.com\\/([a-zA-Z0-9_\\-]+)\\/([0-9]+)/);
                            if (m && !res.some(r => r.url === h)) {
                                res.push({ url: h, blog_id: m[1], log_no: m[2] });
                            }
                        }
                        return res;
                    }""")
                    
                    extracted_users = []
                    # (2) 최근 포스팅 댓글창에서 실제 댓글을 남긴 활성 소통 유저 추출
                    for pl in post_links[:2]:
                        c_url = f"https://m.blog.naver.com/CommentList.naver?blogId={pl['blog_id']}&logNo={pl['log_no']}"
                        try:
                            page.goto(c_url, wait_until="domcontentloaded", timeout=15000)
                            page.wait_for_timeout(2000)
                            c_users = page.evaluate("""() => {
                                const anchors = Array.from(document.querySelectorAll('a'));
                                const u_set = [];
                                for (const a of anchors) {
                                    const h = a.href || '';
                                    const m = h.match(/m\\.blog\\.naver\\.com\\/([a-zA-Z0-9_\\-]+)(?:\\?|$)/);
                                    if (m && m[1] !== 'PostView' && m[1] !== 'CommentList') {
                                        if (!u_set.includes(m[1])) u_set.push(m[1]);
                                    }
                                }
                                return u_set;
                            }""")
                            for u in c_users:
                                if u != t_id and u not in extracted_users:
                                    extracted_users.append(u)
                        except Exception:
                            pass
                    
                    # (3) 공개 이웃 목록에서도 활성 이웃 추출
                    if len(extracted_users) < 10:
                        try:
                            page.goto(f"https://m.blog.naver.com/BuddyList.naver?blogId={t_id}", wait_until="domcontentloaded", timeout=15000)
                            page.wait_for_timeout(2000)
                            b_users = page.evaluate("""() => {
                                const anchors = Array.from(document.querySelectorAll('a'));
                                const u_set = [];
                                for (const a of anchors) {
                                    const h = a.href || '';
                                    const m = h.match(/m\\.blog\\.naver\\.com\\/([a-zA-Z0-9_\\-]+)(?:\\?|$)/);
                                    if (m) {
                                        if (!u_set.includes(m[1])) u_set.push(m[1]);
                                    }
                                }
                                return u_set;
                            }""")
                            for u in b_users:
                                if u != t_id and u not in extracted_users:
                                    extracted_users.append(u)
                        except Exception:
                            pass
                    
                    self._log("info", f"🎯 타깃 블로그 [{t_id}]에서 활성 유저 {len(extracted_users)}명 발굴 완료! 포스팅 탐색 중...")
                    
                    # (4) 발굴된 활성 유저의 최신 포스팅을 순회 타깃으로 변환
                    for cu in extracted_users[:12]:
                        try:
                            page.goto(f"https://m.blog.naver.com/{cu}", wait_until="domcontentloaded", timeout=15000)
                            page.wait_for_timeout(1500)
                            cu_post = page.evaluate("""() => {
                                const anchors = Array.from(document.querySelectorAll('a'));
                                for (const a of anchors) {
                                    const h = a.href || '';
                                    const m = h.match(/m\\.blog\\.naver\\.com\\/([a-zA-Z0-9_\\-]+)\\/([0-9]+)/);
                                    if (m) {
                                        return {
                                            url: h,
                                            blog_id: m[1],
                                            log_no: m[2],
                                            title: (a.innerText || '').trim().replace(/\\n/g, ' ').substring(0, 100)
                                        };
                                    }
                                }
                                return null;
                            }""")
                            if cu_post:
                                targets.append(cu_post)
                        except Exception:
                            pass

            else:
                # 4. 추천/탐색 피드
                self._log("info", "추천 탐색 피드(Recommendation.naver) 탐색 중...")
                page.goto("https://m.blog.naver.com/Recommendation.naver", wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(3000)

                links = page.evaluate("""() => {
                    const anchors = Array.from(document.querySelectorAll('a'));
                    const result = [];
                    for (const a of anchors) {
                        const href = a.href || '';
                        const m = href.match(/m\\.blog\\.naver\\.com\\/([a-zA-Z0-9_\\-]+)\\/([0-9]+)/);
                        if (m) {
                            result.push({
                                url: href,
                                blog_id: m[1],
                                log_no: m[2],
                                title: (a.innerText || '').trim().replace(/\\n/g, ' ').substring(0, 100)
                            });
                        }
                    }
                    return result;
                }""")
                targets.extend(links)

        except Exception as e:
            self._log("warning", f"타깃 크롤링 중 오류: {e}")

        # 중복 URL 제거
        seen = set()
        clean_targets = []
        for t in targets:
            key = f"{t['blog_id']}_{t['log_no']}"
            if key not in seen:
                seen.add(key)
                clean_targets.append(t)
        return clean_targets

    def _process_post(self, page, target: Dict[str, Any], cfg: Dict[str, Any]):
        """단일 포스팅 방문 및 인터랙션 실행"""
        url = target["url"]
        # PC URL(blog.naver.com)인 경우 프레임셋 없는 모바일 URL(m.blog.naver.com)로 자동 정규화
        if "m.blog.naver.com" not in url:
            url = re.sub(r'https?://(?:www\.)?blog\.naver\.com/', 'https://m.blog.naver.com/', url)
            target["url"] = url

        blog_id = target.get("blog_id", "")
        log_no = target.get("log_no", "")
        self._log("info", f"포스팅 방문: {blog_id}/{log_no}")

        executed_actions = []

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(random.randint(2500, 5000))
            if self._check_blocked(page):
                return
            # 글을 읽는 것처럼 조금 내려 본다
            try:
                for _ in range(random.randint(2, 4)):
                    page.mouse.wheel(0, random.randint(300, 700))
                    page.wait_for_timeout(random.randint(800, 1800))
            except Exception:
                pass

            # 포스팅 본문 및 제목, 진짜 작성자 닉네임 정밀 추출
            post_data = page.evaluate("""() => {
                const titleEl = document.querySelector('.se-title-text, .tit_h3, h3.tit');
                const title = titleEl ? titleEl.innerText.trim() : document.title;
                
                const bodyEl = document.querySelector('.se-main-container, #postViewArea, .post_ct');
                const body = bodyEl ? bodyEl.innerText.trim().substring(0, 600) : '';
                
                let nick = '';
                const nickSelectors = [
                    '.blog_author .nick',
                    '.author .nick',
                    '.writer .nick',
                    '[class*="ProfileBlock__name"]',
                    '.nick_area .nick',
                    '.blogger',
                    'span.nick',
                    'em.nick',
                    'a.nick'
                ];
                for (const sel of nickSelectors) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText.trim()) {
                        const txt = el.innerText.trim();
                        if (!txt.includes('신고') && !txt.includes('댓글') && !txt.includes('공감') && txt.length < 30) {
                            nick = txt;
                            break;
                        }
                    }
                }
                return { title, body, nick };
            }""")

            title = post_data.get("title") or target.get("title", "")
            body = post_data.get("body", "")
            raw_nick = post_data.get("nick") or blog_id
            nickname = blog_id if raw_nick in ("신고하기", "댓글", "공감", "이웃추가", "") else raw_nick

            probs = cfg.get("probabilities", {"sympathy": 0.9, "comment": 0.4, "buddy_request": 0.35})
            limits = limits_for(cfg, self.current_account or self._resolve_account(cfg))
            enabled = cfg.get("enabled_actions", {"sympathy": True, "comment": True, "buddy_request": True})

            # ----------------------------------------------------
            # 1. 공감(좋아요) 액션
            # ----------------------------------------------------
            if enabled.get("sympathy") and self.today_counts["sympathy"] < limits.get("sympathy", 100) and self._hourly_ok("sympathy"):
                if random.random() <= probs.get("sympathy", 0.9):
                    liked = self._action_sympathy(page)
                    if liked:
                        self._mark_action("sympathy")
                        executed_actions.append("sympathy")
                        self.today_counts["sympathy"] += 1
                        self._log("info", f"[{nickname}] 포스팅 공감(좋아요) 완료 (오늘 {self.today_counts['sympathy']}회)")
                    page.wait_for_timeout(random.randint(1500, 2500))

            # ----------------------------------------------------
            # 2. 댓글 작성 액션
            # ----------------------------------------------------
            if enabled.get("comment") and self.today_counts["comment"] < limits.get("comment", 50) and self._hourly_ok("comment"):
                if random.random() <= probs.get("comment", 0.4):
                    comment_text = self._build_comment_text(title, body, nickname, cfg)
                    commented, reason = self._action_comment(page, comment_text)
                    if self._check_blocked(page):
                        return
                    if commented:
                        self._mark_action("comment")
                        executed_actions.append("comment")
                        self.today_counts["comment"] += 1
                        self._log("info", f"[{nickname}] 맞춤 댓글 등록 완료: '{comment_text[:40]}...' (오늘 {self.today_counts['comment']}회)")
                    else:
                        if reason == "not_logged_in":
                            self._log("warning", f"[{nickname}] 댓글 작성 스킵: 블로그 계정 로그인이 필요하거나 세션이 만료되었습니다.")
                        elif reason == "disallowed":
                            self._log("info", f"[{nickname}] 댓글 등록 스킵 (게시글 작성자가 댓글 작성을 비허용한 포스팅)")
                        else:
                            self._log("info", f"[{nickname}] 댓글 등록 스킵 ({reason})")
                    page.wait_for_timeout(random.randint(1500, 2500))

            # ----------------------------------------------------
            # 3. 서로이웃 신청 액션
            # ----------------------------------------------------
            if enabled.get("buddy_request") and not self._skip_buddy and self.today_counts["buddy_request"] < limits.get("buddy_request", 50) \
                    and self._hourly_ok("buddy_request"):
                if random.random() <= probs.get("buddy_request", 0.35):
                    buddy_msg = self._build_buddy_message(title, body, nickname, cfg)
                    status_reason, requested = self._action_buddy_request(page, buddy_msg)
                    if self._check_blocked(page):
                        return
                    if requested:
                        self._mark_action("buddy_request")
                        executed_actions.append("buddy_request")
                        self.today_counts["buddy_request"] += 1
                        self._log("info", f"[{nickname}] 맞춤 서로이웃 신청 완료: '{buddy_msg[:35]}...' (오늘 {self.today_counts['buddy_request']}회)")
                    else:
                        if status_reason == "already_buddy":
                            self._log("info", f"[{nickname}] 이미 이웃이거나 서로이웃 상태입니다. (신청 스킵)")
                        elif status_reason == "no_button":
                            self._log("info", f"[{nickname}] 이웃추가 버튼을 찾을 수 없습니다. (스킵)")
                        else:
                            self._log("info", f"[{nickname}] 서로이웃 신청 스킵 ({status_reason})")

            # 히스토리 기록
            history_item = {
                "date": str(date.today()),
                "time": datetime.now().strftime("%H:%M:%S"),
                "account": self.current_account or self._resolve_account(cfg),
                "blog_id": blog_id,
                "log_no": log_no,
                "author": nickname,
                "title": title,
                "url": url,
                "actions": executed_actions,
            }
            hist = load_neighbor_history()
            hist.append(history_item)
            save_neighbor_history(hist)

        except Exception as e:
            self._log("warning", f"포스팅 처리 중 오류 ({blog_id}/{log_no}): {e}")

    def _action_sympathy(self, page) -> bool:
        """공감 버튼 클릭 처리"""
        try:
            # 좋아요 버튼 로케이터 탐색
            like_btn = page.locator("a.u_likeit_button._face, button.u_likeit_button").first
            if like_btn.count() == 0:
                # 대체 셀렉터
                like_btn = page.locator("a[class*='likeit_button'], button[class*='likeit_button']").first

            if like_btn.count() > 0:
                pressed = like_btn.get_attribute("aria-pressed")
                if pressed == "true":
                    self._log("info", "이미 공감한 포스팅입니다. 스킵합니다.")
                    return False

                like_btn.scroll_into_view_if_needed()
                page.wait_for_timeout(500)
                like_btn.click()
                page.wait_for_timeout(1000)
                return True
        except Exception as e:
            logger.debug(f"공감 클릭 실패: {e}")
        return False

    def _build_comment_text(self, title: str, body: str, nickname: str, cfg: Dict[str, Any]) -> str:
        """댓글 문구 생성 (Gemini LLM 또는 템플릿 치환)"""
        mode = cfg.get("comment_mode", "ai")
        app_cfg = config.load_config()
        gemini_key = app_cfg.get("gemini_api_key", "").strip()

        if mode in ("ai", "mixed") and gemini_key:
            try:
                system_prompt = cfg.get("ai_comment_prompt", DEFAULT_CONFIG["ai_comment_prompt"])
                user_prompt = f"블로그 작성자: {nickname}\n글 제목: {title}\n본문 발췌:\n{body[:500]}\n\n작성자 닉네임을 적절히 부르며 본문 내용에 부합하는 정성스러운 댓글을 작성해주세요."

                # Gemini 캐스케이드 호출 (ai.WRITER_MODELS와 동기화)
                models = list(ai.WRITER_MODELS)
                comment, _ = gemini_client.generate(
                    api_key=gemini_key,
                    prompt=user_prompt,
                    models=models,
                    system=system_prompt,
                    timeout=15.0,
                    verbose=False,
                )
                clean_comment = comment.strip().strip('"').strip("'")
                if len(clean_comment) > 5:
                    return clean_comment
            except Exception as e:
                logger.warning(f"Gemini AI 댓글 생성 실패 (템플릿 폴백): {e}")

        # 템플릿 폴백
        templates = cfg.get("comment_templates", DEFAULT_CONFIG["comment_templates"])
        tmpl = random.choice(templates)
        return tmpl.replace("{닉네임}", nickname)

    def _build_buddy_message(self, title: str, body: str, nickname: str, cfg: Dict[str, Any]) -> str:
        """서로이웃 신청 문구 생성 (Gemini AI 본문 분석 또는 다채로운 템플릿 풀)"""
        mode = cfg.get("buddy_mode", "ai")
        app_cfg = config.load_config()
        gemini_key = app_cfg.get("gemini_api_key", "").strip()

        if mode in ("ai", "mixed") and gemini_key:
            try:
                system_prompt = cfg.get("ai_buddy_prompt", DEFAULT_CONFIG["ai_buddy_prompt"])
                user_prompt = (
                    f"블로그 작성자 닉네임: {nickname}\n"
                    f"포스팅 제목: {title}\n"
                    f"본문 내용 발췌:\n{body[:500]}\n\n"
                    "위 포스팅의 관심사나 주제를 자연스럽게 1단어 언급하며, "
                    "상대방이 기분 좋게 수락할 수 있는 정중하고 따뜻한 서로이웃 신청 멘트를 40자 이상 85자 이하로 작성해주세요."
                )

                models = list(ai.WRITER_MODELS)
                reply, _ = gemini_client.generate(
                    api_key=gemini_key,
                    prompt=user_prompt,
                    models=models,
                    system=system_prompt,
                    timeout=15.0,
                    verbose=False,
                )
                clean_msg = reply.strip().strip('"').strip("'").strip()
                if 20 <= len(clean_msg) <= 95:
                    return clean_msg
                elif len(clean_msg) > 95:
                    return clean_msg[:92] + " :)"
            except Exception as e:
                logger.warning(f"Gemini AI 서로이웃 신청 문구 생성 실패 (템플릿 폴백): {e}")

        if mode == "custom":
            custom_tmpl = cfg.get("buddy_message_template") or DEFAULT_CONFIG["buddy_message_template"]
            return custom_tmpl.replace("{닉네임}", nickname).strip()[:95]

        # templates 모드 또는 AI 실패 시 템플릿 풀에서 무작위 선택
        templates = cfg.get("buddy_templates") or DEFAULT_CONFIG["buddy_templates"]
        if not templates:
            templates = DEFAULT_CONFIG["buddy_templates"]

        tmpl = random.choice(templates)
        res = tmpl.replace("{닉네임}", nickname).strip()
        return res[:95]

    def _dump_comment_diag(self, page, reason: str):
        """댓글 실패 시 화면·후보 요소를 data/diagnostics 에 저장 (실제 화면 구조 확인용, 최근 12건만 유지)"""
        try:
            import json as _json, time as _time
            from config import DATA_DIR
            d = DATA_DIR / "diagnostics"
            d.mkdir(parents=True, exist_ok=True)
            info = page.evaluate("""() => ({
              url: location.href, vw: innerWidth, vh: innerHeight,
              btns: Array.from(document.querySelectorAll('button, a')).filter(b => /comment|댓글/i.test((b.className||'')+(b.innerText||''))).slice(0,20).map(b => {const r=b.getBoundingClientRect(); return {tag:b.tagName, cls:(b.className||'').toString().slice(0,80), txt:(b.innerText||'').trim().slice(0,20), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height)}}),
              cbox: Array.from(document.querySelectorAll('[class*=u_cbox], #naverComment, [class*=bottom_sheet], [class*=Comment]')).slice(0,15).map(e => e.tagName + '.' + (e.className||'').toString().slice(0,60)),
              iframes: Array.from(document.querySelectorAll('iframe')).map(f => f.src).slice(0,5)
            })""")
            info["reason"] = reason
            stamp = int(_time.time())
            (d / f"diag_comment_{stamp}.json").write_text(_json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
            page.screenshot(path=str(d / f"diag_comment_{stamp}.png"))
            old = sorted(d.glob("diag_comment_*"))
            for f in old[:-24]:
                f.unlink(missing_ok=True)
        except Exception:
            pass

    def _action_comment(self, page, comment_text: str) -> Tuple[bool, str]:
        """댓글 등록 처리 (모바일 바텀시트 및 공통 u_cbox 완벽 대응)"""
        try:
            # 1. 댓글 버튼 탐색 및 클릭 (숨겨진 템플릿 링크를 배제하고 뷰포트 내 상호작용 가능한 버튼 클릭)
            click_status = page.evaluate("""() => {
                // 이미 댓글 레이어/바텀시트가 열려있는지 확인
                const openSheet = document.querySelector('[class*="bottom_sheet_layer"], [class*="CommentBottomSheet"]');
                if (openSheet && (openSheet.classList.contains('is_show') || openSheet.classList.contains('CustomModal__is_show--Ldwjt'))) {
                    return 'ALREADY_OPEN';
                }

                const btns = Array.from(document.querySelectorAll('button, a'));

                // 우선순위 1: 화면 뷰포트 내에 고정/떠 있는 인터랙션 댓글 버튼
                for (const b of btns) {
                    const c = (b.className || '').toString();
                    if (c.includes('Interact__comment_btn')) {
                        const r = b.getBoundingClientRect();
                        if (r.height > 0 && r.y >= 0 && r.y <= window.innerHeight) {
                            b.click();
                            return 'CLICKED_VIEWPORT';
                        }
                    }
                }

                // 우선순위 2: 본문 하단 인라인 댓글 버튼
                for (const b of btns) {
                    const c = (b.className || '').toString();
                    const t = (b.innerText || '').trim();
                    if (c.includes('Interact__comment_btn') || c.includes('btn_comment') || (t.startsWith('댓글') && t.length < 15)) {
                        const r = b.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                            b.scrollIntoView({ block: 'center' });
                            b.click();
                            return 'CLICKED_INLINE';
                        }
                    }
                }
                return 'NO_BTN';
            }""")

            if click_status == "NO_BTN":
                # 하단 댓글 영역은 스크롤해야 렌더되는 경우가 있어 끝까지 내린 뒤 한 번 더 찾는다
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1500)
                    has_cmt = page.evaluate(
                        "() => !!document.querySelector('[class*=comment_btn], [class*=btn_comment], #naverComment, .u_cbox')")
                except Exception:
                    has_cmt = False
                if has_cmt:
                    click_status = "SCROLLED"
                else:
                    # 그래도 없으면 댓글 전용 페이지로 직접 이동 (글 화면 구조와 무관하게 작성 폼이 있음)
                    import re as _re
                    m = _re.search(r"m\.blog\.naver\.com/([^/?#]+)/(\d+)", page.url)
                    if m:
                        c_url = f"https://m.blog.naver.com/CommentList.naver?blogId={m.group(1)}&logNo={m.group(2)}"
                        try:
                            page.goto(c_url, wait_until="domcontentloaded", timeout=20000)
                            page.wait_for_timeout(1500)
                            click_status = "COMMENT_PAGE"
                        except Exception:
                            pass
                if click_status == "NO_BTN":
                    self._dump_comment_diag(page, "no_comment_button")
                    return (False, "no_comment_button")

            # 2. 비동기 댓글 작성 폼(u_cbox) 마운트 대기 (동적 렌더링에 최대 8초 대기)
            try:
                page.wait_for_selector(
                    ".u_cbox_write_wrap, .u_cbox_text, #naverComment__write_textarea, .u_cbox_write_box",
                    state="attached",
                    timeout=8000
                )
            except Exception:
                # 바텀시트는 열렸는데 댓글 위젯 자체가 로딩 실패(u_cbox_loading_fail)한 경우:
                # 일시 오류일 수 있어 새로고침 → 댓글 전용 페이지 순으로 재시도한다
                form_sel = ".u_cbox_write_wrap, .u_cbox_text, #naverComment__write_textarea, .u_cbox_write_box"
                load_failed = False
                try:
                    load_failed = page.evaluate("() => !!document.querySelector('.u_cbox_loading_fail')")
                except Exception:
                    pass
                recovered = False
                if load_failed:
                    import re as _re2
                    m2 = _re2.search(r"blogId=([^&]+)&logNo=(\d+)", page.url) or _re2.search(
                        r"m\.blog\.naver\.com/([^/?#]+)/(\d+)", page.url)
                    retry_urls = [None]
                    if m2:
                        retry_urls.append(
                            f"https://m.blog.naver.com/CommentList.naver?blogId={m2.group(1)}&logNo={m2.group(2)}")
                    for ru in retry_urls:
                        try:
                            if ru is None:
                                page.reload(wait_until="domcontentloaded", timeout=20000)
                                page.wait_for_timeout(2500)
                                page.evaluate("""() => {
                                    const b = document.querySelector('[class*="Interact__comment_btn"], .btn_floating_commentwrite');
                                    if (b) b.click();
                                }""")
                            else:
                                page.goto(ru, wait_until="domcontentloaded", timeout=20000)
                            page.wait_for_selector(form_sel, state="attached", timeout=8000)
                            recovered = True
                            break
                        except Exception:
                            continue
                if not recovered:
                    reason = "comment_widget_load_failed" if load_failed else "form_timeout"
                    self._dump_comment_diag(page, reason)
                    return (False, reason)

            page.wait_for_timeout(800)

            # 3. 로그인 상태 및 댓글 허용 여부 정밀 검사
            status_check = page.evaluate("""() => {
                const root = document.querySelector('#naverComment, [class*="bottom_sheet_layer"], body');
                const loggedOut = root.querySelector('.u_cbox_type_logged_out');
                const guide = root.querySelector('.u_cbox_guide');
                const guideText = guide ? (guide.innerText || '').trim() : '';

                if (loggedOut || guideText.includes('로그인')) {
                    return { ok: false, reason: 'not_logged_in' };
                }

                const input = root.querySelector('#naverComment__write_textarea, .u_cbox_text, textarea.u_cbox_text, [contenteditable="true"].u_cbox_text, textarea');
                if (!input) {
                    return { ok: false, reason: 'disallowed' };
                }
                return { ok: true, reason: 'ready' };
            }""")

            if not status_check.get("ok"):
                self._dump_comment_diag(page, status_check.get("reason", "disallowed"))
                return (False, status_check.get("reason", "disallowed"))

            # 4. 댓글 입력창 포커스 및 텍스트 주입 (DOM 이벤트 + 키보드 이벤트 결합)
            type_success = page.evaluate("""(text) => {
                const root = document.querySelector('#naverComment, [class*="bottom_sheet_layer"], body');
                const input = root.querySelector('#naverComment__write_textarea, .u_cbox_text, textarea.u_cbox_text, [contenteditable="true"].u_cbox_text, textarea');
                if (!input) return false;

                // 가이드 플레이스홀더 숨김 및 포커스 트리거
                const guide = root.querySelector('.u_cbox_guide');
                if (guide) guide.style.display = 'none';

                const inbox = root.querySelector('.u_cbox_inbox, .u_cbox_write_area, .u_cbox_write');
                if (inbox) inbox.click();

                input.focus();
                if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {
                    input.value = text;
                } else {
                    input.innerText = text;
                }

                // 댓글창(u_cbox) 글자수 카운터 및 등록 버튼 활성화를 위한 표준 이벤트 발화
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
                input.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
                return true;
            }""", comment_text)

            if not type_success:
                self._dump_comment_diag(page, "type_failed")
                return (False, "type_failed")

            page.wait_for_timeout(500)

            # 자연스러운 키 입력 이벤트 보강 (스마트 안티봇 감지 우회)
            try:
                page.keyboard.press("Space")
                page.wait_for_timeout(80)
                page.keyboard.press("Backspace")
                page.wait_for_timeout(300)
            except Exception:
                pass

            # 5. 등록 버튼 클릭
            uploaded = page.evaluate("""() => {
                const root = document.querySelector('#naverComment, [class*="bottom_sheet_layer"], body');
                const btn = root.querySelector('.u_cbox_btn_upload, button[class*="writeButton"], button[class*="u_cbox_btn_upload"]');
                if (btn && !btn.disabled) {
                    btn.click();
                    return true;
                }
                return false;
            }""")

            if not uploaded:
                # 폴백: Playwright 로케이터 강제 클릭
                upload_btn = page.locator(".u_cbox_btn_upload, button[class*='writeButton'], button:has-text('등록')").first
                if upload_btn.count() > 0:
                    upload_btn.click(force=True)
                    uploaded = True

            if uploaded:
                page.wait_for_timeout(2500)
                return (True, "ok")
            else:
                self._dump_comment_diag(page, "upload_button_not_clickable")
                return (False, "upload_button_not_clickable")

        except Exception as e:
            logger.debug(f"댓글 등록 예외: {e}")
            return (False, f"error_{e}")

    def _action_buddy_request(self, page, message: str) -> Tuple[str, bool]:
        """서로이웃 신청 처리: (상태사유, 성공여부)"""
        try:
            # 이웃추가 버튼 탐색
            buddy_btn = page.locator(
                "button[class*='ProfileBuddyButton'], a.btn_buddyadd, button:has-text('이웃추가'), "
                "a:has-text('이웃추가'), [class*='buddyadd']"
            ).first
            if buddy_btn.count() == 0:
                return ("no_button", False)

            btn_text = buddy_btn.inner_text().strip()
            # 이미 이웃이거나 서로이웃인 경우 스킵
            if "이웃추가" not in btn_text:
                return ("already_buddy", False)

            buddy_btn.scroll_into_view_if_needed()
            buddy_btn.click()
            page.wait_for_timeout(2000)

            # 서로이웃 라디오 버튼 선택 (#bothBuddy, label[for='bothBuddy'])
            both_radio = page.locator("input#bothBuddy, label[for='bothBuddy'], input[value='bothBuddy']").first
            if both_radio.count() > 0:
                both_radio.click()
                page.wait_for_timeout(500)

            # 다음 또는 신청 메시지 입력
            next_btn = page.locator("button:has-text('다음'), a:has-text('다음')").first
            if next_btn.count() > 0:
                next_btn.click()
                page.wait_for_timeout(1000)

            # 메시지 입력창
            msg_box = page.locator("textarea#message, textarea[name='message'], textarea[placeholder*='서로이웃']").first
            if msg_box.count() > 0:
                msg_box.click()
                msg_box.fill("")
                page.wait_for_timeout(200)
                msg_box.fill(message[:95])
                page.wait_for_timeout(500)

            # 최종 확인/신청 버튼
            submit_btn = page.locator("button:has-text('확인'), a:has-text('확인'), button:has-text('신청')").first
            if submit_btn.count() > 0:
                submit_btn.click()
                page.wait_for_timeout(1500)
                return ("ok", True)
            return ("no_submit_btn", False)
        except Exception as e:
            logger.debug(f"서로이웃 신청 실패: {e}")
            return (f"error_{e}", False)


# ═══════════════════════ 스마트 이웃 관리 (블로그 관리 페이지) ═══════════════════════
# 2026-11 기준 관리 페이지 (예전 BuddyManage / BuddyAddManage 주소는 없어졌다)
#   내가 추가한 이웃: BuddyListManage.naver   (체크박스 name=buddyBlogNo, 칸: 그룹|이웃구분|이름|새글소식|최근 글|이웃추가일, 50명씩)
#   받은 서로이웃 신청: BuddyInviteReceivedManage.naver (name=targetBlogId, 일괄 수락 _acceptMultiBuddy / 거절 _denyMultiBuddy)
#   보낸 서로이웃 신청: BuddyInviteSentManage.naver     (name=targetBlogId, 일괄 신청취소 _cancleMultiInvite)
ADMIN = "https://admin.blog.naver.com"
BUDDY_LIMIT = 5000

_ROWS_JS = r"""(kind) => {
  const out = [];
  const name = kind === 'list' ? 'buddyBlogNo' : 'targetBlogId';
  document.querySelectorAll(`tbody tr input[type=checkbox][name=${name}]`).forEach(cb => {
    const tr = cb.closest('tr'); const tds = [...tr.querySelectorAll('td')].map(t => (t.innerText || '').trim().replace(/\s+/g, ' '));
    const a = tr.querySelector('a[href*="blog.naver.com"]'); let bid = '';
    if (a) { const h = a.getAttribute('href') || ''; const m = h.match(/userId=([\w-]+)/) || h.match(/blog\.naver\.com\/([\w-]+)/); if (m) bid = m[1]; }
    if (kind === 'list') out.push({value: cb.value, blog_id: bid, type: tds[2] || '', name: tds[3] || '', last_post: tds[5] || '', added: tds[6] || ''});
    else out.push({value: cb.value, blog_id: cb.value, name: tds[1] || '', msg: (tr.querySelector('td.msg') || {}).innerText || '', date: (tr.querySelector('td.date') || {}).innerText || ''});
  });
  return out;
}"""
_PAGES_JS = r"""() => { let mx = 1; document.querySelectorAll('.paginate a[href*="goPage"], .paginate strong').forEach(a => {
  const m = ((a.getAttribute && a.getAttribute('href')) || '').match(/goPage\((\d+)\)/); const n = m ? +m[1] : parseInt(a.textContent, 10); if (n > mx) mx = n; }); return mx; }"""


def _ymd(s: str):
    """'26.11.14.' / '2026.11.14' / '2026-11-14' → date (없으면 None)"""
    s = (s or "").strip()
    m = re.search(r"(\d{2,4})[.\-/](\d{1,2})[.\-/](\d{1,2})", s)
    if not m:
        return None
    y = int(m.group(1))
    y = y + 2000 if y < 100 else y
    try:
        return date(y, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _admin_target(account_id: str):
    app_cfg = config.load_config()
    target = (account_id or "").strip() or app_cfg.get("active_account") or ""
    acc = config.find_account(app_cfg, target)
    slot = (acc or {}).get("slot", 1)
    blog_id = (acc or {}).get("blog_id") or (acc or {}).get("naver_id") or app_cfg.get("blog_id") or app_cfg.get("naver_id") or ""
    profile_dir = config.account_profile_dir(app_cfg, target)
    win_profile = config.DATA_DIR / ("naver_profile_win" if slot == 1 else f"naver_profile_win_{slot}")
    if win_profile.exists() and (win_profile / "Default" / "Network" / "Cookies").exists():
        profile_dir = win_profile
    return blog_id, profile_dir


def _open_admin(page, kind: str, blog_id: str, page_no: int = 1) -> bool:
    """관리 페이지를 연다. 로그인이 풀렸으면 False."""
    name = {"list": "BuddyListManage", "received": "BuddyInviteReceivedManage", "sent": "BuddyInviteSentManage"}[kind]
    page.goto(f"{ADMIN}/{name}.naver?blogId={blog_id}", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1800)
    if "nidlogin" in page.url or "nid.naver.com" in page.url:
        return False
    if page_no > 1:
        try:
            with page.expect_navigation(timeout=15000):
                page.evaluate(f"goPage({int(page_no)})")
        except Exception:
            page.evaluate(f"goPage({int(page_no)})")
        page.wait_for_timeout(1500)
    return True


def _bulk_click(page, kind: str, values: list, button_text: str) -> int:
    """values 의 체크박스를 고르고 일괄 버튼을 누른 뒤, 화면을 다시 읽어 실제로 사라진 수를 돌려준다."""
    name = "buddyBlogNo" if kind == "list" else "targetBlogId"
    picked = 0
    for v in values:
        cb = page.locator(f"tbody input[type=checkbox][name={name}][value=\"{v}\"]").first
        if cb.count():
            cb.check()
            picked += 1
    if not picked:
        return 0
    btn = page.locator("button").filter(has_text=re.compile(f"^\\s*{button_text}\\s*$")).first
    if not btn.count():
        return 0
    def _ok(d):
        try:
            d.accept()
        except Exception:
            pass
    page.on("dialog", _ok)
    try:
        btn.click()
        page.wait_for_timeout(1500)
    finally:
        page.remove_listener("dialog", _ok)
    if kind == "list" and button_text == "삭제":
        # 이웃 삭제는 툴팁 레이어(#tpl_layer_del)의 이미지 확인 버튼 → oEventForm.submit('del').
        # 레이어가 안 보이면 같은 동작을 직접 실행한다(체크된 buddyBlogNo 는 frmProcess 안에 있음).
        try:
            ok = page.locator("input[type=image][alt='확인']:visible").first
            if ok.count():
                ok.click()
            else:
                page.evaluate("""() => {
                    const d = document.getElementById('delete_all'); if (d) d.checked = true;
                    oEventForm.submit('del');
                }""")
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(2500)
        return picked
    # 레이어로 뜨는 확인 창이 있으면 '확인'
    for t in ("확인", "삭제", "예"):
        ok = page.locator(".layer_popup button, .ly_pop button, [class*=layer] button, [class*=popup] button").filter(
            has_text=re.compile(f"^\\s*{t}\\s*$")).first
        try:
            if ok.count() and ok.is_visible():
                ok.click()
                page.wait_for_timeout(1500)
                break
        except Exception:
            pass
    page.wait_for_timeout(1500)
    return picked


def _accept_via_popup(page, values: list, full_groups: set, reopen=None, max_tries: int = 15) -> None:
    """받은 신청 일괄 수락. '수락'을 누르면 새 창(BothBuddyMultiAcceptForm)이 떠서 그룹을 고르고 '확인'을 눌러야 한다.
    그룹 인원이 꽉 차면 '그룹이웃수 제한초과'가 나오므로, 그 그룹을 full_groups 에 기록하고 다음 그룹으로 다시 시도한다."""
    for _ in range(max_tries):
        picked = 0
        for v in values:
            cb = page.locator(f"tbody input[type=checkbox][name=targetBlogId][value=\"{v}\"]").first
            if cb.count():
                cb.check()
                picked += 1
        if not picked:
            return
        btn = page.locator("button").filter(has_text=re.compile(r"^\s*수락\s*$")).first
        if not btn.count():
            return
        def _ok(d):
            try:
                d.accept()
            except Exception:
                pass
        page.on("dialog", _ok)   # '수락하시겠습니까?' 확인창 → 그 뒤에 새 창이 뜬다
        try:
            with page.context.expect_page(timeout=10000) as pi:
                btn.click()
            pop = pi.value
        except Exception as e:
            logger.warning(f"[받은신청관리] 수락 창이 열리지 않았습니다: {type(e).__name__}: {str(e)[:200]}")
            page.remove_listener("dialog", _ok)
            return
        page.remove_listener("dialog", _ok)
        try:
            pop.on("dialog", lambda d: d.accept())
            pop.wait_for_url(re.compile(r"BothBuddyMultiAcceptForm"), timeout=15000)
            pop.wait_for_load_state("domcontentloaded")
            pop.wait_for_selector("select#groupIdSelector, select[name=groupIdSelector]", timeout=10000)
            sel = pop.locator("select#groupIdSelector, select[name=groupIdSelector]").first
            opts = sel.evaluate("s=>[...s.options].map(o=>({v:o.value,t:o.text.trim(),s:o.selected}))") if sel.count() else []
            cur = next((o for o in opts if o["s"]), opts[0] if opts else None)
            if cur and cur["v"] in full_groups:
                # 꽉 찬 그룹은 건너뛰고, 최근에 만든(목록 뒤쪽) 그룹부터 고른다
                cur = next((o for o in reversed(opts) if o["v"] not in full_groups and o["v"]), None)
                if not cur:
                    logger.warning("[받은신청관리] 빈 자리가 있는 이웃 그룹이 없습니다.")
                    pop.close()
                    return
                sel.select_option(cur["v"])
            ok = pop.locator("input[alt='확인'], input[value='확인']").first
            if not ok.count():
                ok = pop.get_by_text("확인", exact=True).first
            ok.click()
            pop.wait_for_url(re.compile(r"BothBuddyMultiAccept\.naver"), timeout=15000)
            pop.wait_for_timeout(800)
            txt = pop.inner_text("body") if not pop.is_closed() else ""
        except Exception as e:
            logger.warning(f"[받은신청관리] 수락 창 처리 실패: {e}")
            txt = ""
        finally:
            try:
                if not pop.is_closed():
                    pop.close()
            except Exception:
                pass
        m = re.search(r"(\d+)\s*명과\s*서로이웃이", txt)
        logger.info(f"[받은신청관리] 그룹 '{cur['t'] if cur else '?'}'로 수락 결과: {m.group(1) if m else '?'}명 / {re.sub(r'\s+', ' ', txt)[:120]}")
        if "제한초과" in txt and cur:
            full_groups.add(cur["v"])
            page.wait_for_timeout(1500)
            if reopen:
                reopen()
            else:
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
            left = {r["value"] for r in page.evaluate(_ROWS_JS, "received")}
            values = [v for v in values if v in left]
            if not values:
                logger.info("[받은신청관리] 다시 읽어 보니 남은 신청이 없습니다.")
                return
            logger.info(f"[받은신청관리] 그룹이 꽉 차 다른 그룹으로 다시 시도합니다(꽉 찬 그룹 {len(full_groups)}개).")
            continue
        page.wait_for_timeout(1000)
        return


def buddy_count(page, blog_id: str):
    """내가 추가한 이웃 수 (못 읽으면 None)."""
    try:
        if not _open_admin(page, "list", blog_id):
            return None
        txt = page.inner_text("body")
        m = re.search(r"이웃\s*([\d,]+)\s*명", txt)
        return int(m.group(1).replace(",", "")) if m else None
    except Exception:
        return None


def _scan(page, kind: str, blog_id: str, max_pages: int):
    """모든 페이지의 행을 읽는다 → (페이지별 행 목록, 마지막 페이지 번호)"""
    if not _open_admin(page, kind, blog_id):
        return None, 0
    last = min(page.evaluate(_PAGES_JS) or 1, max_pages)
    # 페이지 번호가 10개씩만 보이므로 '맨뒤' 페이지 번호도 읽었다(_PAGES_JS). 그 이상이면 차례로 넘기며 확인
    pages = {}
    n = 1
    while n <= last:
        if n > 1 and not _open_admin(page, kind, blog_id, n):
            break
        pages[n] = page.evaluate(_ROWS_JS, kind)
        if not pages[n]:
            break
        last = max(last, min(page.evaluate(_PAGES_JS) or n, max_pages))
        n += 1
    return pages, n - 1


def _run_admin(account_id: str, work):
    from playwright.sync_api import sync_playwright
    import browser
    blog_id, profile_dir = _admin_target(account_id)
    if not blog_id:
        return {"ok": False, "message": "등록된 N사 계정 정보가 없습니다."}
    try:
        with sync_playwright() as pw:
            ctx = browser.launch_naver_context(pw, headless=True, profile_dir=profile_dir)
            try:
                return work(browser.first_page(ctx), blog_id)
            finally:
                ctx.close()
    except Exception as e:
        logger.error(f"[이웃관리] 오류: {e}")
        return {"ok": False, "message": f"처리 중 오류가 발생했습니다: {e}"}


def _mask(bid: str) -> str:
    return (bid[:2] + "***") if bid else "?"


def cancel_unanswered_buddy_requests(account_id: str = "", older_than_days: int = 7, dry_run: bool = False,
                                     max_pages: int = 60, limit: int = 0) -> Dict[str, Any]:
    """보낸 서로이웃 신청 중 older_than_days 일 이상 응답이 없는 것을 취소 (5,000명 한도 확보)."""
    def work(page, blog_id):
        pages, last = _scan(page, "sent", blog_id, max_pages)
        if pages is None:
            return {"ok": False, "message": f"[{blog_id}] 로그인이 풀렸습니다. 계정 탭에서 다시 로그인해 주세요."}
        today = date.today()
        targets = {n: [r["value"] for r in rows if (d := _ymd(r["date"])) and (today - d).days >= older_than_days]
                   for n, rows in pages.items()}
        if limit:   # 가장 오래된 신청부터 limit 건만
            dated = sorted(((_ymd(r["date"]), n, r["value"]) for n, rows in pages.items() for r in rows if r["value"] in targets[n]),
                           key=lambda x: x[0])[:limit]
            keep = {(n, v) for _, n, v in dated}
            targets = {n: [v for v in vs if (n, v) in keep] for n, vs in targets.items()}
        total = sum(len(v) for v in targets.values())
        all_rows = sum(len(v) for v in pages.values())
        if dry_run or not total:
            return {"ok": True, "dry_run": dry_run, "target_count": total, "scanned": all_rows,
                    "message": f"보낸 신청 {all_rows}건 중 {older_than_days}일 이상 응답 없는 신청 {total}건" + (" (미리 보기)" if dry_run else " — 취소할 신청이 없습니다.")}
        done = 0
        for n in sorted(targets, reverse=True):   # 뒤 페이지부터: 앞 페이지 목록이 밀리지 않게
            if not targets[n] or not _open_admin(page, "sent", blog_id, n):
                continue
            before = {r["value"] for r in page.evaluate(_ROWS_JS, "sent")}
            want = [v for v in targets[n] if v in before]
            _bulk_click(page, "sent", want, "신청취소")
            _open_admin(page, "sent", blog_id, n)
            after = {r["value"] for r in page.evaluate(_ROWS_JS, "sent")}
            done += sum(1 for v in want if v not in after)
        return {"ok": True, "canceled_count": done, "target_count": total,
                "message": f"{older_than_days}일 이상 응답 없는 보낸 신청 {done}건을 취소했습니다." + (f" (대상 {total}건 중 {total - done}건은 처리되지 않음)" if done < total else "")}
    logger.info(f"[보낸신청취소] 기준 {older_than_days}일, 미리보기={dry_run}")
    return _run_admin(account_id, work)


def clean_ghost_buddies(account_id: str = "", inactive_months: int = 6, whitelist: List[str] = None,
                        max_pages: int = 120, dry_run: bool = False, keep_mutual: bool = False, limit: int = 0) -> Dict[str, Any]:
    """'최근 글'이 inactive_months 개월보다 오래된(또는 글이 없고 이웃이 된 지도 그만큼 지난) 이웃을 삭제."""
    wl = {w.strip().lower() for w in (whitelist or []) if w.strip()}
    months = max(1, int(inactive_months or 6))   # 0개월(=전체 삭제)은 허용하지 않는다

    def is_ghost(r):
        if r["blog_id"].lower() in wl:
            return False
        if keep_mutual and "서로" in r["type"]:
            return False
        limit = months * 30
        last = _ymd(r["last_post"])
        if last:
            return (date.today() - last).days >= limit
        added = _ymd(r["added"])   # 글이 하나도 없으면, 이웃이 된 지 기준보다 오래됐을 때만
        return bool(added and (date.today() - added).days >= limit)

    def work(page, blog_id):
        pages, last = _scan(page, "list", blog_id, max_pages)
        if pages is None:
            return {"ok": False, "message": f"[{blog_id}] 로그인이 풀렸습니다. 계정 탭에서 다시 로그인해 주세요."}
        targets = {n: [r for r in rows if is_ghost(r)] for n, rows in pages.items()}
        if limit:   # 새 글이 가장 오래 없는 이웃부터 limit 명만
            flat = sorted(((_ymd(r["last_post"]) or _ymd(r["added"]) or date.min, n, r["value"]) for n, v in targets.items() for r in v),
                          key=lambda x: x[0])[:limit]
            keep = {(n, v) for _, n, v in flat}
            targets = {n: [r for r in v if (n, r["value"]) in keep] for n, v in targets.items()}
        total = sum(len(v) for v in targets.values())
        scanned = sum(len(v) for v in pages.values())
        sample = [f"{r['name'][:12]}({_mask(r['blog_id'])}, 최근 글 {r['last_post'] or '없음'})" for v in targets.values() for r in v][:8]
        if dry_run or not total:
            return {"ok": True, "dry_run": dry_run, "target_count": total, "scanned": scanned, "sample": sample,
                    "message": f"이웃 {scanned}명 중 {months}개월 넘게 새 글이 없는 이웃 {total}명" + (" (미리 보기)" if dry_run else " — 정리할 이웃이 없습니다.")}
        done = 0
        for n in sorted(targets, reverse=True):
            if not targets[n] or not _open_admin(page, "list", blog_id, n):
                continue
            now_rows = {r["value"]: r for r in page.evaluate(_ROWS_JS, "list")}
            want = [r["value"] for r in targets[n] if r["value"] in now_rows and is_ghost(now_rows[r["value"]])]
            _bulk_click(page, "list", want, "삭제")
            _open_admin(page, "list", blog_id, n)
            after = {r["value"] for r in page.evaluate(_ROWS_JS, "list")}
            done += sum(1 for v in want if v not in after)
        return {"ok": True, "deleted_count": done, "target_count": total,
                "message": f"{months}개월 넘게 새 글이 없는 이웃 {done}명을 정리했습니다." + (f" (대상 {total}명 중 {total - done}명은 처리되지 않음)" if done < total else "")}
    logger.info(f"[유령이웃정리] 기준 {months}개월, 보호 {len(wl)}명, 미리보기={dry_run}")
    return _run_admin(account_id, work)


def manage_received_buddy_requests(account_id: str = "", action_type: str = "accept_filtered", spam_keywords: List[str] = None,
                                   reject_default_message: bool = True, max_pages: int = 20, dry_run: bool = False,
                                   limit: int = 0) -> Dict[str, Any]:
    """받은 서로이웃 신청: 스팸(키워드·기본 멘트)은 거절, 나머지는 수락(모드에 따라). 수락은 5,000명 한도 안에서만."""
    keywords = [k.strip().lower() for k in (spam_keywords or [
        "대출", "부업", "재택", "카지노", "토토", "코인", "투자", "수익", "마케팅", "원고료", "상위노출", "광고", "체험단"
    ]) if k.strip()]
    default_greetings = ["우리서로이웃해요", "서로이웃을신청합니다", "서로이웃신청합니다", "서로이웃해요"]

    def is_spam(r):
        text = f"{r['name']} {r['msg']}".lower()
        if any(k in text for k in keywords):
            return True
        compact = re.sub(r"\s+", "", r["msg"])
        return reject_default_message and (not compact or any(compact.startswith(g) and len(compact) <= len(g) + 4 for g in default_greetings))

    def work(page, blog_id):
        cnt = buddy_count(page, blog_id)
        room = (BUDDY_LIMIT - cnt) if cnt is not None else None
        pages, last = _scan(page, "received", blog_id, max_pages)
        if pages is None:
            return {"ok": False, "message": f"[{blog_id}] 로그인이 풀렸습니다. 계정 탭에서 다시 로그인해 주세요."}
        rows = [r for v in pages.values() for r in v]
        spam = [r for r in rows if is_spam(r)]
        good = [r for r in rows if not is_spam(r)]
        acc_n = 0 if action_type == "reject_spam_only" else len(rows if action_type == "accept_all" else good)
        rej_n = 0 if action_type == "accept_all" else len(spam)
        note = ""
        if room is not None and acc_n > room:
            note = f" 이웃이 {cnt:,}명이라 5,000명 한도까지 {max(room, 0)}명만 수락할 수 있습니다."
        if dry_run or not rows:
            return {"ok": True, "dry_run": dry_run, "accept_target": acc_n, "reject_target": rej_n, "scanned": len(rows),
                    "buddy_count": cnt, "message": (f"받은 신청 {len(rows)}건: 수락 대상 {acc_n}건, 스팸 거절 대상 {rej_n}건" if rows else "받은 신청이 없습니다.") + note + (" (미리 보기)" if dry_run and rows else "")}
        accepted = rejected = 0
        budget = room if room is not None else acc_n
        if limit:
            budget = min(budget, limit)
        rej_budget = limit or 10 ** 6
        full_groups = set()
        for n in sorted(pages, reverse=True):
            if not _open_admin(page, "received", blog_id, n):
                continue
            now = page.evaluate(_ROWS_JS, "received")
            if action_type != "accept_all":
                rej = [r["value"] for r in now if is_spam(r)][:rej_budget - rejected]
                if rej:
                    _bulk_click(page, "received", rej, "거절")
                    _open_admin(page, "received", blog_id, n)
                    after = {r["value"] for r in page.evaluate(_ROWS_JS, "received")}
                    rejected += sum(1 for v in rej if v not in after)
                    now = page.evaluate(_ROWS_JS, "received")   # 거절 뒤 화면을 새로 읽고 나서 수락(예전 체크박스 재사용 금지)
            if action_type != "reject_spam_only" and budget > 0:
                acc = [r["value"] for r in now if action_type == "accept_all" or not is_spam(r)][:budget]
                if acc:
                    _accept_via_popup(page, acc, full_groups, reopen=lambda: _open_admin(page, "received", blog_id, n))
                    _open_admin(page, "received", blog_id, n)
                    after = {r["value"] for r in page.evaluate(_ROWS_JS, "received")}
                    k = sum(1 for v in acc if v not in after)
                    accepted += k
                    budget -= k
        return {"ok": True, "accepted_count": accepted, "rejected_count": rejected,
                "message": f"수락 {accepted}건, 스팸 거절 {rejected}건을 처리했습니다." + note}
    logger.info(f"[받은신청관리] 모드 {action_type}, 미리보기={dry_run}")
    return _run_admin(account_id, work)
