"""예약 발행 목록 (schedule_queue.py)

- 여러 건의 원고를 각자 예약 시각과 함께 목록에 추가 → 백그라운드에서 한 건씩 N사 예약 발행으로 등록
- 목록에서 상태 확인·취소 (등록 전: 목록에서 제외 / N사에 예약된 뒤: N사 예약 글 삭제)
- 예약마다 N사 계정·내 사진을 함께 저장 → 화면에서 계정을 바꿔도 각 예약은 원래 계정으로 등록
- 토큰: N사에 예약 등록될 때 100토큰 차감. 등록된 뒤 취소해도 돌려주지 않음 (등록 전 빼기는 차감 없음)
- 저장: data/schedules.json

- 키워드 자동 예약: 제목(주제)과 시각을 먼저 정해 항목(auto)을 넣고, 차례가 오면 원고를 써서 같은 흐름으로 등록

상태: queued(등록 대기) → writing(원고 작성 중, auto만) → registering(N사 등록 중) → scheduled(예약됨)
      → published(발행 시각 지남) / canceling(취소 중) → canceled(취소됨) / failed(실패, error)
"""
import json
import random
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from config import DATA_DIR, interval_conflict, load_history

SCHEDULE_FILE = DATA_DIR / "schedules.json"
ACTIVE = ("queued", "writing", "registering", "scheduled", "canceling")
WAITING = ("queued", "writing", "registering")    # 아직 N사에 등록되지 않은 상태
MIN_LEAD_MIN = 15       # 자동 예약 글 하나를 쓰고 등록하는 데 필요한 여유 (이보다 가까우면 시각을 옮긴다)
_lock = threading.RLock()
_paused = ""            # 사람이 해결해야 해서 멈춘 사유 (로그인 인증 등). 비어 있으면 진행


def _load() -> List[Dict]:
    try:
        return json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items: List[Dict]):
    SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCHEDULE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SCHEDULE_FILE)


def round_up_10(dt: datetime) -> datetime:
    """N사 예약은 10분 단위 → 올림."""
    base = dt.replace(second=0, microsecond=0)
    if dt.second or dt.microsecond or base.minute % 10:
        base = base.replace(minute=(base.minute // 10) * 10) + timedelta(minutes=10)
    return base


def list_items() -> List[Dict]:
    """발행 시각이 지난 예약은 '발행 완료'로 표시해 반환 (예약 시각순, 내용 본문은 제외)."""
    with _lock:
        items = _load()
        now = datetime.now()
        changed = False
        for it in items:
            if it["status"] == "scheduled" and datetime.fromisoformat(it["schedule_at"]) <= now:
                it["status"] = "published"
                changed = True
        if changed:
            _save(items)
        out = []
        for it in sorted(items, key=lambda x: x["schedule_at"]):
            row = {k: v for k, v in it.items() if k not in ("content", "tags", "auto")}
            row["is_auto"] = bool(it.get("auto"))
            row["chars"] = len((it.get("content") or "").replace(" ", "").replace("\n", ""))
            out.append(row)
        return out


def _busy_times(cfg: dict, items: List[Dict], ignore_id: str = "") -> List[datetime]:
    """같은 계정의 예약·발행 시각 (목록 예약 + 실제 발행 이력)."""
    me, legacy = cfg.get("naver_id", ""), cfg.get("legacy_account", "")
    times = []
    for it in items:
        # 이미 N사에 등록된 예약은 발행 이력에도 있으므로 목록에서는 대기·등록 중만 셈
        if it["id"] != ignore_id and it["status"] in WAITING and it.get("account", me) == me:
            times.append(datetime.fromisoformat(it["schedule_at"]))
    for h in load_history():
        if me and (h.get("account") or legacy) != me:
            continue
        t = h.get("scheduled_at") or h.get("time")
        try:
            times.append(datetime.fromisoformat(str(t).replace(" ", "T")[:16]))
        except Exception:
            pass
    return times


def _conflict(when: datetime, cfg: dict, items: List[Dict], ignore_id: str = "") -> str:
    """발행 간격 검사 (간격 안에는 1건까지 허용, 하루 건수 제한 없음)."""
    return interval_conflict(when, _busy_times(cfg, items, ignore_id), int(cfg.get("min_interval_min") or 0))


def add(draft: Dict, schedule_at: str, cfg: dict) -> Dict:
    """원고를 예약 목록에 추가. 문제가 있으면 ValueError(사용자용 메시지)."""
    try:
        when = round_up_10(datetime.fromisoformat(schedule_at))
    except ValueError:
        raise ValueError("예약 시각 형식이 올바르지 않습니다.")
    if when < datetime.now() + timedelta(minutes=10):
        raise ValueError("예약 시각은 지금부터 10분 이후로 선택해 주세요.")
    if not (draft.get("title") or "").strip() or not (draft.get("content") or "").strip():
        raise ValueError("제목과 본문을 입력해 주세요.")
    with _lock:
        items = _load()
        msg = _conflict(when, cfg, items)
        if msg:
            raise ValueError(msg)
        item = {
            "id": uuid.uuid4().hex[:12],
            "title": draft["title"].strip()[:300],
            "content": draft["content"][:60000],
            "tags": (draft.get("tags") or "")[:2000],
            "category": draft.get("category") or "",
            "auto_thumb": bool(draft.get("auto_thumb", True)),
            "public_open": bool(draft.get("public_open", True)),
            "account": cfg.get("naver_id", ""),
            "photo_ids": [p for p in (draft.get("photo_ids") or []) if isinstance(p, str)][:10],
            "schedule_at": when.isoformat(timespec="minutes"),
            "status": "queued",
            "url": "",
            "error": "",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        items.append(item)
        _save(items)
        return item


def plan_slots(day: date, start_hour: int, end_hour: int, count: int, gap_min: int,
               busy: List[datetime], earliest: datetime) -> List[datetime]:
    """day의 start_hour~end_hour 안에서 서로 gap_min분 이상 떨어진 무작위 시각 count개 (10분 단위).
    다른 발행(busy)과도 gap_min분 이상 띄운다. 들어갈 자리가 모자라면 ValueError."""
    base = datetime.combine(day, datetime.min.time())
    lo = max(base + timedelta(hours=start_hour), round_up_10(earliest))
    hi = base + timedelta(hours=end_hour) - timedelta(minutes=10 if end_hour == 24 else 0)
    gap = timedelta(minutes=gap_min)
    free = []
    t = lo
    while t <= hi:
        if all(abs(t - b) >= gap for b in busy):
            free.append(t)
        t += timedelta(minutes=10)
    # 각 칸에서 시작하면 뒤로 몇 건까지 들어가는지 (뒤에서부터 계산)
    next_idx, fits = [len(free)] * len(free), [0] * (len(free) + 1)
    j = len(free)
    for i in range(len(free) - 1, -1, -1):
        while j > 0 and free[j - 1] - free[i] >= gap:
            j -= 1
        next_idx[i] = j
        fits[i] = 1 + fits[j]
    capacity = fits[0] if free else 0
    if capacity < count:
        raise ValueError(f"{day:%m월 %d일} {start_hour}시~{end_hour}시에는 {gap_min}분 간격으로 "
                         f"최대 {capacity}건까지 넣을 수 있습니다. 시간대를 늘리거나 날짜를 바꿔 주세요.")
    # 필수 간격을 뺀 여유 시간을 무작위로 나눈다 (그냥 뽑아 밀어내면 간격이 정확히 1시간씩 붙어 규칙적으로 보임).
    # 다른 발행을 피하느라 칸이 비어 있을 때도, 남은 글이 뒤에 다 들어갈 범위 안에서만 고른다
    step = gap_min // 10
    slack = max(0, len(free) - 1 - (count - 1) * step)
    targets = [r + k * step for k, r in enumerate(sorted(random.randint(0, slack) for _ in range(count)))]
    out, pos = [], 0
    for k, target in enumerate(targets):
        remain = count - k
        last = max(i for i in range(pos, len(free)) if fits[i] >= remain)
        i = min(max(target, pos), last)
        out.append(free[i])
        pos = next_idx[i]
    return out


def preview_slots(day: date, start_hour: int, end_hour: int, count: int, gap_min: int, cfg: dict) -> List[datetime]:
    with _lock:
        busy = _busy_times(cfg, _load())
    return plan_slots(day, start_hour, end_hour, count, gap_min, busy,
                      datetime.now() + timedelta(minutes=MIN_LEAD_MIN + 5))


def pick_keywords(keywords: List[str], count: int) -> List[str]:
    """글 count개에 쓸 키워드. 고르게 돌아가며 순서만 무작위."""
    picks: List[str] = []
    while len(picks) < count:
        round_ = keywords[:]
        random.shuffle(round_)
        picks += round_
    return picks[:count]


def add_batch(picks: List[str], topics: List[Dict], slots: List[datetime], gap_min: int, window: tuple,
              options: Dict, cfg: dict) -> List[Dict]:
    """키워드 자동 예약 항목 추가. picks·topics·slots는 같은 순서로 글 하나씩."""
    batch_id = uuid.uuid4().hex[:8]
    now = datetime.now().isoformat(timespec="seconds")
    new = [{
        "id": uuid.uuid4().hex[:12],
        "title": topic["title"].strip()[:300],
        "content": "",
        "tags": "",
        "category": options.get("category") or "",
        "auto_thumb": bool(options.get("auto_thumb", True)),
        "public_open": bool(options.get("public_open", True)),
        "account": cfg.get("naver_id", ""),
        "photo_ids": [],
        "schedule_at": when.isoformat(timespec="minutes"),
        "status": "queued",
        "url": "",
        "error": "",
        "created_at": now,
        "auto": {"keyword": kw, "topic": topic, "batch": batch_id, "gap": gap_min, "window": list(window), "tries": 0},
    } for kw, topic, when in zip(picks, topics, sorted(slots))]
    with _lock:
        items = _load()
        items.extend(new)
        _save(items)
    return new


def batch_status() -> Optional[Dict]:
    """가장 최근 자동 예약 묶음의 진행 상황 (사용자가 뺀 글은 total에서 제외하고 canceled로 따로 센다)."""
    with _lock:
        autos = [it for it in _load() if it.get("auto")]
    if not autos:
        return None
    last = max(autos, key=lambda it: it["created_at"])["auto"]["batch"]
    batch = [it for it in autos if it["auto"]["batch"] == last]
    rows = [it for it in batch if it["status"] != "canceled"]
    if not rows:
        return None
    done = sum(1 for it in rows if it["status"] in ("scheduled", "published"))
    failed = sum(1 for it in rows if it["status"] == "failed")
    waiting = sum(1 for it in rows if it["status"] in WAITING)
    return {"id": last, "total": len(rows), "done": done, "failed": failed, "waiting": waiting,
            "canceled": len(batch) - len(rows)}


def pause(reason: str):
    global _paused
    _paused = reason


def resume():
    global _paused
    _paused = ""


def paused() -> str:
    return _paused


def _move_auto(it: Dict, items: List[Dict], cfg: dict) -> bool:
    """시각이 너무 가까워진 자동 예약(프로그램이 꺼져 있었거나 재시도로 늦어진 경우)을 뒤의 빈 시각으로 옮긴다."""
    auto = it["auto"]
    start, end = auto["window"]
    busy = _busy_times(cfg, items, it["id"])
    earliest = datetime.now() + timedelta(minutes=MIN_LEAD_MIN + 5)
    day = max(earliest.date(), datetime.fromisoformat(it["schedule_at"]).date())
    for offset in range(14):
        try:
            slot = plan_slots(day + timedelta(days=offset), start, end, 1, auto["gap"], busy, earliest)[0]
        except ValueError:
            continue
        old = it["schedule_at"]
        it["schedule_at"] = slot.isoformat(timespec="minutes")
        it["error"] = f"시각이 지나 {old.replace('T', ' ')[5:]} → {it['schedule_at'].replace('T', ' ')[5:]} 로 옮겼습니다."
        return True
    return False


def _update(item_id: str, **kw) -> Optional[Dict]:
    with _lock:
        items = _load()
        for it in items:
            if it["id"] == item_id:
                it.update(kw)
                _save(items)
                return it
    return None


def get(item_id: str) -> Optional[Dict]:
    with _lock:
        return next((it for it in _load() if it["id"] == item_id), None)


def next_queued(cfg_for=None) -> Optional[Dict]:
    """등록할 다음 항목 (예약 시각이 빠른 순). 시각이 10분 이내로 다가와 등록할 수 없게 된 건은 실패 처리.
    자동 예약 항목은 실패시키지 않고 뒤의 빈 시각으로 옮긴다 (cfg_for: 계정 → 설정)."""
    with _lock:
        items = _load()
        changed = False
        pick = None
        now = datetime.now()
        for it in sorted(items, key=lambda x: x["schedule_at"]):
            if it["status"] != "queued":
                continue
            auto = it.get("auto")
            if auto and auto.get("not_before", "") > now.isoformat():
                continue
            if auto and datetime.fromisoformat(it["schedule_at"]) < now + timedelta(minutes=MIN_LEAD_MIN) and cfg_for:
                if _move_auto(it, items, cfg_for(it.get("account", ""))):
                    changed = True
                    pick = it
                    break
            if datetime.fromisoformat(it["schedule_at"]) < now + timedelta(minutes=5):
                it["status"], it["error"] = "failed", "예약 시각이 너무 가까워져 등록하지 못했습니다. 다시 추가해 주세요."
                changed = True
                continue
            pick = it
            break
        if changed:
            _save(items)
        return dict(pick) if pick else None


def mark_registering(item_id): return _update(item_id, status="registering", error="")
def mark_writing(item_id): return _update(item_id, status="writing", error="")
def mark_queued(item_id, error=""): return _update(item_id, status="queued", error=str(error)[:300])


def save_written(item_id: str, title: str, content: str, tags: str, topic_title: str = "") -> Optional[Dict]:
    """자동 예약 원고가 완성됨 → 등록 차례로."""
    with _lock:
        items = _load()
        for it in items:
            if it["id"] == item_id:
                it.update(title=title.strip()[:300], content=content[:60000], tags=tags[:2000], status="queued", error="")
                it["auto"]["topic_title"] = topic_title
                _save(items)
                return it
    return None


def write_failed(item_id: str, err, max_tries: int = 3, wait_until: Optional[datetime] = None) -> bool:
    """원고 작성 실패: 5분 뒤 다시 시도. 횟수를 다 쓰면 실패 처리. 다시 시도하면 True.
    wait_until: AI 하루 사용량 소진처럼 그때까지 기다리면 되는 실패 — 시도 횟수를 쓰지 않고 그 시각 이후 이어서."""
    with _lock:
        items = _load()
        for it in items:
            if it["id"] != item_id:
                continue
            if wait_until:
                it["status"], it["error"] = "queued", str(err)[:300]
                it["auto"]["not_before"] = wait_until.isoformat()
                _save(items)
                return True
            it["auto"]["tries"] = it["auto"].get("tries", 0) + 1
            retry = it["auto"]["tries"] < max_tries
            it["status"], it["error"] = ("queued" if retry else "failed"), str(err)[:300]
            it["auto"]["not_before"] = (datetime.now() + timedelta(minutes=5)).isoformat() if retry else ""
            _save(items)
            return retry
    return False


def retry(item_id: str) -> str:
    """실패한 항목 다시 시도. 되면 빈 문자열, 안 되면 사유."""
    with _lock:
        items = _load()
        for it in items:
            if it["id"] != item_id:
                continue
            if it["status"] != "failed":
                return "실패한 항목만 다시 시도할 수 있습니다."
            if it.get("unconfirmed"):
                return "이미 올라갔을 수 있어 자동으로 다시 시도하지 않습니다. 블로그를 확인해 주세요."
            if not it.get("auto") and datetime.fromisoformat(it["schedule_at"]) < datetime.now() + timedelta(minutes=10):
                return "예약 시각이 지났습니다. 다시 추가해 주세요."
            it["status"], it["error"] = "queued", ""
            if it.get("auto"):
                it["auto"].update(tries=0, not_before="")
            _save(items)
            return ""
    return "항목을 찾을 수 없습니다."


def all_titles() -> List[str]:
    """예약 목록에 있는 모든 글 제목 (주제 추천에서 겹치지 않게)."""
    with _lock:
        return [(it.get("auto") or {}).get("topic_title") or it.get("title", "") for it in _load()]


def used_titles(batch: str) -> List[str]:
    """같은 묶음에서 이미 쓴 주제 제목."""
    with _lock:
        return [it["auto"].get("topic_title") or it["title"] for it in _load()
                if (it.get("auto") or {}).get("batch") == batch and it.get("content")]


def same_keyword_waiting(batch: str, keyword: str) -> int:
    """같은 묶음에서 이 키워드로 아직 글을 쓰지 않은 항목 수."""
    with _lock:
        return sum(1 for it in _load() if (it.get("auto") or {}).get("batch") == batch
                   and it["auto"]["keyword"] == keyword and not it.get("content") and it["status"] in WAITING)


def mark_scheduled(item_id, url, title="", when=""):
    kw = {"status": "scheduled", "url": url or "", "error": "", "token_spent": True}
    if title:
        kw["posted_title"] = title          # N사에 들어간 실제 제목 (예약 취소 때 찾는 기준)
    if when:
        kw["schedule_at"] = when.replace(" ", "T")[:16]
    return _update(item_id, **kw)
def mark_failed(item_id, err): return _update(item_id, status="failed", error=str(err)[:300])
def mark_canceled(item_id): return _update(item_id, status="canceled", error="")
def mark_canceling(item_id): return _update(item_id, status="canceling", error="")


def request_cancel(item_id: str) -> str:
    """취소 요청 → 'removed'(등록 전이라 바로 취소) | 'remote'(N사 예약 삭제 필요) | 오류 메시지."""
    with _lock:
        it = get(item_id)
        if not it:
            return "항목을 찾을 수 없습니다."
        if it["status"] in ("queued", "failed"):
            mark_canceled(item_id)
            return "removed"
        if it["status"] in ("registering", "writing"):
            return "지금 처리하는 중입니다. 끝난 뒤 취소해 주세요."
        if it["status"] == "scheduled":
            if datetime.fromisoformat(it["schedule_at"]) <= datetime.now():
                return "이미 발행 시각이 지났습니다."
            mark_canceling(item_id)
            return "remote"
        return "취소할 수 없는 상태입니다."


def remove(item_id: str) -> bool:
    """끝난 항목(발행 완료·취소·실패)을 목록에서 지움."""
    with _lock:
        items = _load()
        keep = [it for it in items if not (it["id"] == item_id and it["status"] in ("published", "canceled", "failed"))]
        if len(keep) != len(items):
            _save(keep)
            return True
        return False


def remove_published() -> int:
    """이미 발행된 항목(발행 시각이 지난 예약 포함)을 목록에서 모두 지움. 지운 개수를 돌려줌."""
    with _lock:
        now = datetime.now()
        items = _load()
        def done(it):
            if it["status"] == "published":
                return True
            try:
                return it["status"] == "scheduled" and datetime.fromisoformat(it["schedule_at"]) <= now
            except (KeyError, ValueError):
                return False
        keep = [it for it in items if not done(it)]
        if len(keep) != len(items):
            _save(keep)
        return len(items) - len(keep)


def pending_count() -> int:
    """아직 처리할 일(등록 대기·등록 중·취소 중)이 남은 예약 수."""
    with _lock:
        return sum(1 for it in _load() if it["status"] in ("queued", "writing", "registering", "canceling"))


def next_cancel() -> Optional[Dict]:
    with _lock:
        it = next((x for x in _load() if x["status"] == "canceling"), None)
        return dict(it) if it else None


def photo_ids_in_use() -> set:
    with _lock:
        return {p for it in _load() if it["status"] in ("queued", "registering") for p in it.get("photo_ids", [])}


def recover_after_restart():
    """프로그램이 등록·취소 도중 종료됐던 항목 복구 (등록 중 → 대기로 되돌려 다시 등록, 취소 중 → 예약됨)."""
    with _lock:
        items = _load()
        changed = False
        for it in items:
            if it["status"] in ("registering", "writing"):
                it["status"], it["error"] = "queued", "프로그램이 종료되어 다시 진행합니다."
                changed = True
            elif it["status"] == "canceling":
                it["status"] = "scheduled"
                changed = True
        if changed:
            _save(items)
