"""N사 스마트에디터 ONE 자동 포스팅 모듈 (publisher.py)

Mublo Content Studio 역공학 기반 상용급 스마트에디터 ONE 포스팅 엔진:
1. mainFrame 및 메인 페이지 동적 탐색 및 로딩 완결 대기 (_resolve_editor_frame)
2. '이어서 작성' 모달 및 도움말/가림 오버레이 소멸 (취소선 버튼 오클릭 원천 방어) (_dismiss_all_popups)
3. 툴바 토글 상태 및 인라인 서식(취소선, 볼드, 이탤릭) 원천 리셋 엔진 (_reset_inline_format)
4. 본문 취소선(strikethrough) TreeWalker 실측 기반 강제 해제 (_ensure_body_not_struck_through)
5. 제목 필드 정밀 포커스 및 타이핑 검증 (미반영 시 DOM 직접 주입 3중 보장) (_type_title)
6. 마크다운 기호(**, ##) 완전 소멸 및 단락 간격/줄바꿈 100% 보존 본문 입력 (_type_body)
7. 본문 최하단 N사 표준 해시태그 블록 자동 배치
8. N사 업로드 레이어 ('내 컴퓨터' .nvu_btn_local) 연동 및 개별사진 배치 카드뉴스 3장+ 첨부 (_attach_images)
9. 발행 패널 [data-testid=seOnePublishBtn] 클릭, 확인 모달 자동 수락 (_confirm_publish)
10. 실제 발행 완료 글 URL (blog.naver.com/{blogId}/{logNo}) 정밀 추출
"""
import os
import re
import json
import time
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from playwright.sync_api import Page, Frame

logger = logging.getLogger("naver.publisher")
logger.setLevel(logging.INFO)


class NeedUserAction(RuntimeError):
    """로그인 인증·보안문자처럼 사람이 직접 해결해야 하는 상황. 자동 재시도하지 않고 예약 작업을 멈춘다."""


class PublishUnconfirmed(RuntimeError):
    """[발행]을 누른 뒤 결과를 확인하지 못함. 이미 올라갔을 수 있어 중복을 막으려고 자동 재시도하지 않는다."""


def clean_markdown_text(text: str) -> str:
    """N사 스마트에디터 본문 및 제목에 노출되는 마크다운 기호(**, ## 등)를 완전히 정제합니다."""
    if not text:
        return ""
    t = text.strip()
    # 1. 볼드 **텍스트** 또는 __텍스트__
    t = re.sub(r"\*\*(.*?)\*\*", r"\1", t)
    t = re.sub(r"__(.*?)__", r"\1", t)
    # 2. 이탤릭 *텍스트* 또는 _텍스트_
    t = re.sub(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", r"\1", t)
    t = re.sub(r"(?<!_)_(?!_)(.*?)(?<!_)_(?!_)", r"\1", t)
    # 3. 헤딩 기호 #, ##, ###, #### 맨 앞 제거
    t = re.sub(r"^#{1,6}\s*", "", t)
    # 4. 취소선 ~~텍스트~~
    t = re.sub(r"~~(.*?)~~", r"\1", t)
    # 5. 백틱 `코드`
    t = re.sub(r"`(.*?)`", r"\1", t)
    # 6. 마크다운 링크 [라벨](url)
    t = re.sub(r"\[(.*?)\]\([^)]+\)", r"\1", t)
    # 7. 단독/잔여 마크다운 특수기호 정리
    t = t.replace("**", "").replace("##", "")
    return t.strip()

def normalize_naver_tags(raw_tags: List[str]) -> List[str]:
    """
    N사 블로그 태그 표준화:
    - 태그 중간에 공백이 들어간 경우(예: '정보 보호') 공백 단위로 분리하여 ['정보', '보호']로 각각 처리
    - 각 태그 내부 특수문자 제거 (# 등)
    - 중복 제거 및 유효 태그 목록 반환
    """
    normalized = []
    seen = set()
    for item in raw_tags:
        if not item:
            continue
        cleaned = item.strip().lstrip("#").strip()
        words = cleaned.split()
        for w in words:
            w_clean = re.sub(r"[^\w가-힣]", "", w)
            if w_clean and w_clean not in seen:
                seen.add(w_clean)
                normalized.append(w_clean)
    return normalized

def build_hashtag_line(tags: List[str]) -> str:
    """
    본문 최하단 N사 표준 해시태그 라인 생성:
    #정보 #보호 #청라맛집 (단어 내부 공백 없음, 태그 간격 정확히 1칸 공백)
    """
    norm_tags = normalize_naver_tags(tags)
    return " ".join([f"#{t}" for t in norm_tags])

class SmartEditorPublisher:
    def __init__(self, page: Page, blog_id: str):
        self.page = page
        self.blog_id = blog_id
        self.frame: Frame = page.main_frame
        self._seen_popups = set()
        self._install_guards()

    def _install_guards(self):
        """브라우저 확인창·새 창을 자동 처리.
        Playwright는 듣는 곳이 없으면 확인창을 취소하므로, 글 작성 중 뜬 확인창이 발행이나 화면 이동을 막을 수 있다."""
        def on_dialog(dialog):
            try:
                if dialog.message:
                    logger.info(f"🧹 브라우저 확인창 자동 승인: {dialog.message[:80]}")
                dialog.accept()
            except Exception:
                pass

        def on_new_page(new_page):
            try:
                logger.info(f"🧹 새로 열린 창 자동 닫기: {new_page.url[:80]}")
                new_page.close()
            except Exception:
                pass

        try:
            self.page.on("dialog", on_dialog)
            self.page.context.on("page", on_new_page)
        except Exception as e:
            logger.debug(f"자동 처리 등록 실패: {e}")

    # 페이지 안에서 팝업을 지켜본다: '작성 중인 글' 창은 입력을 시작한 뒤 늦게 뜰 때가 있고,
    # 발행 거부 안내(토스트)는 1~2초 만에 사라져서 나중에 읽으면 놓친다.
    _JS_WATCH = r"""
    () => {
      if (window.__alWatch) return;
      window.__alWatch = true;
      window.__alToasts = [];
      const check = () => {
        const p = document.querySelector('.se-popup');
        if (p && /작성\s*중|이어서/.test(p.innerText || '')) {
          const c = p.querySelector('.se-popup-button-cancel');
          if (c) c.click();
        }
      };
      new MutationObserver((ms) => {
        for (const m of ms) for (const n of m.addedNodes) {
          if (n.nodeType !== 1) continue;
          const t = n.matches('.se-toast-popup') ? n : n.querySelector('.se-toast-popup');
          if (t) window.__alToasts.push({
            msg: (t.innerText || '').trim().slice(0, 120),
            warn: !!t.querySelector('.se-toast-popup-content-warning'),
          });
        }
        check();
      }).observe(document.documentElement, {childList: true, subtree: true});
      check();
    }
    """

    def _start_watch(self):
        try:
            self.frame.evaluate(self._JS_WATCH)
        except Exception as e:
            logger.debug(f"팝업 감시 시작 실패: {e}")

    def _take_toasts(self) -> List[str]:
        """지금까지 뜬 경고 안내를 꺼내 온다 (꺼내면 비워짐)."""
        try:
            rows = self.frame.evaluate("() => (window.__alToasts || []).splice(0)")
            return [r["msg"] for r in rows if r.get("warn") and r.get("msg")]
        except Exception:
            return []

    # 처음 보는 팝업도 처리: 대화상자처럼 표시된 층이나 화면 가운데를 덮은 고정 층을 찾아 안전한 버튼을 누른다.
    # 에디터 자체의 패널(발행 설정·스티커 등)은 건드리지 않는다.
    _JS_BLOCKER = r"""
    (preferConfirm) => {
      const vis = (e) => {
        const r = e.getBoundingClientRect(), s = getComputedStyle(e);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
      };
      const own = '.se-toolbar, .se-sidebar, .se-help-panel, .se-floating-material-container, .se-utils, [class*="layer_publish"], [class*="layer_popup"]';
      let root = [...document.querySelectorAll('[role=dialog], [role=alertdialog], .se-popup, [class*=modal], [class*=Modal], [class*=dimmed]')]
        .find((e) => vis(e) && !e.closest(own));
      if (!root) {
        let e = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
        for (; e && e !== document.body; e = e.parentElement) {
          const r = e.getBoundingClientRect();
          if (getComputedStyle(e).position === 'fixed' && r.width > 200 && r.height > 100 && !e.closest(own)) { root = e; break; }
        }
      }
      if (!root) return null;
      const text = (root.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 100);
      if (/자동입력|보안문자|captcha|캡차/i.test(text + ' ' + root.className)) return {text, captcha: true};
      const buttons = [...root.querySelectorAll('button, a[role=button], [role=button]')].filter(vis);
      const label = (b) => (b.innerText || b.getAttribute('aria-label') || b.title || '').trim();
      const order = preferConfirm
        ? [/^(확인|예|발행|계속)/, /닫기|close/i, /^취소/]
        : [/보지\s*않/, /닫기|close|^[x×]$/i, /^취소/, /^(나중에|다음에|건너뛰기)/, /^확인/];
      for (const re of order) {
        const b = buttons.find((x) => re.test(label(x)));
        if (b) { const name = label(b).slice(0, 20); b.click(); return {text, clicked: name}; }
      }
      return {text, clicked: ''};
    }
    """

    def _clear_blockers(self, prefer_confirm: bool = False) -> bool:
        """화면을 막은 팝업을 닫는다. 닫았으면 True. 보안문자는 사람이 풀어야 하므로 NeedUserAction."""
        cleared = False
        contexts = [self.frame, self.page.main_frame] if self.frame != self.page.main_frame else [self.page.main_frame]
        for ctx in contexts:
            for _ in range(4):
                try:
                    found = ctx.evaluate(self._JS_BLOCKER, prefer_confirm)
                except Exception:
                    break
                if not found:
                    break
                text = found.get("text") or "(내용 없음)"
                if found.get("captcha"):
                    self._save_diagnostic("captcha")
                    raise NeedUserAction("블로그가 보안문자 입력을 요구합니다. 브라우저에서 직접 입력한 뒤 다시 시작해 주세요.")
                if text not in self._seen_popups:
                    self._seen_popups.add(text)
                    logger.info(f"🧹 팝업 자동 처리: '{text[:60]}' → [{found.get('clicked') or 'Esc'}]")
                    self._save_diagnostic("popup")
                if not found.get("clicked"):
                    self.page.keyboard.press("Escape")
                cleared = True
                self.page.wait_for_timeout(350)
        return cleared

    def _save_diagnostic(self, label: str = "error"):
        """오류 분석용 전체 스크린샷과 HTML, 진단 정보를 data/diagnostics 폴더에 자동 저장합니다."""
        try:
            from config import DATA_DIR
            diag_dir = str(DATA_DIR / "diagnostics")
            os.makedirs(diag_dir, exist_ok=True)
            ts = int(time.time())
            shot_path = os.path.join(diag_dir, f"diag_{label}_{ts}.png")
            html_path = os.path.join(diag_dir, f"diag_{label}_{ts}.html")
            json_path = os.path.join(diag_dir, f"diag_{label}_{ts}.json")

            self.page.screenshot(path=shot_path, full_page=True)

            content = ""
            try:
                content = self.frame.content()
            except Exception:
                content = self.page.content()

            with open(html_path, "w", encoding="utf-8", errors="ignore") as f:
                f.write(content[:150000])

            frames_info = [{"name": f.name, "url": f.url} for f in self.page.frames]
            info = {
                "label": label,
                "timestamp": ts,
                "page_url": self.page.url,
                "frames": frames_info
            }
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)

            logger.info(f"📸 진단 스냅샷 저장 완료: {shot_path}")
            # 오래된 진단 파일 정리 (최근 90개 = 약 30회분만 보관)
            files = sorted((os.path.join(diag_dir, n) for n in os.listdir(diag_dir)), key=os.path.getmtime)
            for old in files[:-90]:
                os.remove(old)
        except Exception as e:
            logger.debug(f"진단 스냅샷 저장 실패: {e}")

    def _reset_inline_format(self, frame: Frame):
        """
        Mublo 역공학 핵심: 스마트에디터 ONE의 활성화된 인라인 서식(취소선, 볼드, 이탤릭, 밑줄)을 원천 리셋합니다.
        (strikethrough 토글 버튼이 on 상태인 경우 즉시 클릭하여 해제)
        """
        js = """
        () => {
          try {
            document.execCommand('foreColor', false, '#000000');
          } catch (e) {}
          const CMDS = {
            bold: 'bold', italic: 'italic', underline: 'underline',
            strikethrough: 'strikeThrough',
          };
          const isPressed = (btn) => btn.getAttribute('aria-pressed') === 'true'
            || btn.classList.contains('se-is-on');
          const cmdState = (name) => {
            const cmd = CMDS[name];
            if (!cmd) return null;
            try { return document.queryCommandState(cmd) === true; } catch (e) { return null; }
          };
          const report = [];
          const buttons = document.querySelectorAll(
            'button[data-group="propertyToolbar"][data-type="toggle"]'
          );
          for (const btn of buttons) {
            const name = btn.getAttribute('data-name') || '';
            const pressedBefore = isPressed(btn);
            const stateBefore = cmdState(name);
            if (!pressedBefore && stateBefore !== true) continue;
            btn.click();
            report.push({
              name,
              pressed_before: pressedBefore,
              state_before: stateBefore,
            });
          }
          return report;
        }
        """
        for _ in range(2):
            try:
                rep = frame.evaluate(js)
                if not rep:
                    break
                self.page.wait_for_timeout(300)
            except Exception:
                pass

    def _measure_struck_chars(self, frame: Frame) -> Tuple[int, int]:
        """본문 취소선 글자수 실측 (Mublo 역공학 computed style 기반)"""
        try:
            res = frame.evaluate("""
                () => {
                  const root = document.querySelector('.se-main-container') || document.body;
                  if (!root) return {struck: 0, total: 0};
                  let struck = 0, total = 0;
                  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                  while (walker.nextNode()) {
                    const text = (walker.currentNode.textContent || '').replace(/\\s+/g, '');
                    if (!text.length) continue;
                    const el = walker.currentNode.parentElement;
                    if (!el) continue;
                    total += text.length;
                    const deco = (window.getComputedStyle(el).textDecorationLine || '');
                    if (deco.includes('line-through')) struck += text.length;
                  }
                  return {struck, total};
                }
            """)
            return res.get("struck", 0), res.get("total", 0)
        except Exception:
            return 0, 0

    def _ensure_body_not_struck_through(self, frame: Frame):
        """본문에 취소선이 적용된 경우 자동으로 선택하여 해제합니다 (Mublo 역공학 자동 복구 루프)"""
        self._reset_inline_format(frame)
        struck, total = self._measure_struck_chars(frame)
        if struck <= 0:
            return

        logger.warning(f"본문 취소선 감지 ({struck}/{total}자) -> 자동 해제 시도")
        for attempt in range(1, 4):
            try:
                frame.evaluate("""
                    () => {
                      const root = document.querySelector('.se-main-container');
                      if (!root) return;
                      const sel = window.getSelection();
                      const range = document.createRange();
                      range.selectNodeContents(root);
                      sel.removeAllRanges();
                      sel.addRange(range);
                      const btn = document.querySelector(
                        'button[data-group="propertyToolbar"][data-type="toggle"][data-name="strikethrough"]'
                      ) || document.querySelector('button.se-strikethrough-toolbar-button');
                      if (btn) btn.click();
                    }
                """)
                self.page.wait_for_timeout(400)
                frame.evaluate("() => { const s = window.getSelection(); if (s) s.removeAllRanges(); }")
                struck, _ = self._measure_struck_chars(frame)
                if struck <= 0:
                    logger.info(f"✔ 본문 취소선 완벽 해제 완료 (시도 {attempt})")
                    break
            except Exception:
                pass

    def _dismiss_all_popups(self):
        """
        '이어서 작성하시겠습니까?' 모달, 도움말 툴팁, 딤(dim) 오버레이를 제거합니다.
        (툴바의 '취소선' 버튼을 절대 건드리지 않도록 se-popup-button-cancel 등 정밀 타겟팅)
        """
        contexts = [self.frame, self.page.main_frame] if self.frame != self.page.main_frame else [self.page.main_frame]

        # 1. '이어서 작성하시겠습니까?' 모달 [취소] 버튼 직접 클릭 (툴바 취소선과 완전 분리)
        for ctx in contexts:
            try:
                cancel_btn = ctx.locator("button.se-popup-button-cancel").first
                if cancel_btn.count() > 0 and cancel_btn.is_visible(timeout=500):
                    cancel_btn.click(timeout=1000)
                    self.page.wait_for_timeout(300)
                    logger.info("✔ '작성 중인 글' 팝업 [취소] 클릭 완료.")
            except Exception:
                pass

        # 2. 도움말 및 팝업 닫기 버튼
        for ctx in contexts:
            try:
                close_btn = ctx.locator("button.se-help-panel-close-button, .se-popup-close-button").first
                if close_btn.count() > 0 and close_btn.is_visible(timeout=500):
                    close_btn.click(timeout=1000)
                    self.page.wait_for_timeout(200)
            except Exception:
                pass

        # 3. DOM 클린업 (오버레이 및 툴팁)
        for ctx in contexts:
            try:
                ctx.evaluate("""
                    () => {
                        const removeSelectors = [
                            '.se-help-header',
                            '.container_O3PGu',
                            '.se-help-panel',
                            '.se-tooltip',
                            '[class*="help-header"]'
                        ];
                        for (const s of removeSelectors) {
                            document.querySelectorAll(s).forEach(el => {
                                try { el.remove(); } catch(e) {}
                            });
                        }
                    }
                """)
            except Exception:
                pass

        # 4. 위 목록에 없는 팝업 (공지·안내 등)
        self._clear_blockers()

    def _resolve_editor_frame(self, timeout_sec: float = 20.0) -> Frame:
        """스마트에디터 ONE의 실제 에디팅 프레임을 동적으로 탐색하고 렌더링될 때까지 대기합니다."""
        deadline = time.time() + timeout_sec
        logger.info("스마트에디터 에디팅 프레임 탐색 및 로딩 대기 중...")

        editor_markers = [
            '.se-section-documentTitle',
            '.se-documentTitle',
            '.se-title-text',
            '.se-section-documentContent',
            '.se-component.se-text',
            '[contenteditable="true"]',
            'button.se-publish-btn'
        ]

        while time.time() < deadline:
            current_url = self.page.url.lower()
            if "nidlogin" in current_url or "nid.naver.com" in current_url:
                raise RuntimeError("N사 로그인 세션이 만료되었습니다. 웹 UI의 [최초 1회 로그인] 버튼을 눌러 다시 로그인해주세요.")

            self._dismiss_all_popups()

            # 1. 모든 자식 프레임 검사 (iframe#mainFrame 우선)
            for f in self.page.frames:
                for marker in editor_markers:
                    try:
                        if f.locator(marker).count() > 0:
                            logger.info(f"✔ 스마트에디터 프레임 감지 완료: name='{f.name}', marker='{marker}'")
                            self.frame = f
                            return f
                    except Exception:
                        continue

            # 2. 메인 페이지 검사
            for marker in editor_markers:
                try:
                    if self.page.locator(marker).count() > 0:
                        logger.info(f"✔ 스마트에디터 메인 페이지 감지 완료: marker='{marker}'")
                        self.frame = self.page.main_frame
                        return self.page.main_frame
                except Exception:
                    continue

            # iframe#mainFrame 객체 갱신
            try:
                mf = self.page.frame(name="mainFrame")
                if mf:
                    self.frame = mf
            except Exception:
                pass

            self.page.wait_for_timeout(400)

        logger.warning("에디터 마커 탐색 타임아웃, 현재 지정된 프레임으로 진행합니다.")
        mf = self.page.frame(name="mainFrame")
        self.frame = mf if mf else self.page.main_frame
        return self.frame

    def _type_title(self, title: str):
        """
        제목 입력란을 찾아 타이핑하고 실제 입력 여부를 철저히 검증합니다.
        (서식 리셋 + Playwright 로케이터 클릭 + JS DOM 직접 주입 3중 보장)
        """
        clean_title = clean_markdown_text(title)
        logger.info(f"제목 입력 영역 포커스 시도: '{clean_title}'")
        self._dismiss_all_popups()
        self._reset_inline_format(self.frame)
        self._ensure_body_not_struck_through(self.frame)

        contexts = [self.frame, self.page.main_frame] if self.frame != self.page.main_frame else [self.page.main_frame]
        focused = False

        title_selectors = [
            '.se-section-documentTitle',
            '.se-component.se-documentTitle',
            '.se-module.se-title-text',
            '.se-title-text',
            '.se-placeholder__title',
            '[placeholder*="제목"]',
            '.se-documentTitle'
        ]

        for ctx in contexts:
            for sel in title_selectors:
                try:
                    loc = ctx.locator(sel).first
                    if loc.count() > 0 and loc.is_visible(timeout=300):
                        loc.click(force=True, timeout=1500)
                        focused = True
                        logger.info(f"✔ 제목 요소 클릭 성공 ({sel})")
                        break
                except Exception:
                    continue
            if focused:
                break

        # JS Caret 포커스 폴백
        if not focused:
            for ctx in contexts:
                try:
                    focused = ctx.evaluate("""
                        () => {
                            const el = document.querySelector('.se-documentTitle, .se-section-documentTitle, .se-title-text, .se-placeholder__title');
                            if (el) {
                                const target = el.querySelector('[contenteditable="true"]') || el.querySelector('.se-text-paragraph') || el;
                                target.focus();
                                const sel = window.getSelection();
                                const range = document.createRange();
                                range.selectNodeContents(target);
                                range.collapse(false);
                                sel.removeAllRanges();
                                sel.addRange(range);
                                try { target.click(); } catch(e) {}
                                return true;
                            }
                            return false;
                        }
                    """)
                    if focused:
                        logger.info("✔ JS DOM API로 제목 영역 직접 포커스 성공")
                        break
                except Exception:
                    continue

        if not focused:
            self._save_diagnostic("title_not_found")
            raise RuntimeError("스마트에디터 제목 입력 영역을 찾지 못했습니다.")

        self.page.wait_for_timeout(200)
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Delete")
        self.page.wait_for_timeout(200)

        # 제목 타이핑 (한글 씹힘 방지 15ms 딜레이)
        self.page.keyboard.type(clean_title, delay=15)
        self.page.wait_for_timeout(400)

        # 입력 중 늦게 뜬 팝업이 글자를 가로챘으면 다시 입력
        self._ensure_title(clean_title)

        # 제목 입력 검증 및 JS 직접 주입 폴백 (빈 제목 원천 차단)
        for ctx in contexts:
            try:
                check_text = ctx.evaluate("""
                    () => {
                        const el = document.querySelector('.se-documentTitle, .se-section-documentTitle, .se-title-text');
                        return el ? (el.innerText || el.textContent || '').trim() : '';
                    }
                """)
                if not check_text or check_text == "제목":
                    logger.info("제목 키보드 타이핑 미반영 감지 -> JS 직접 주입 폴백 실행")
                    ctx.evaluate("""
                        (t) => {
                            const el = document.querySelector('.se-documentTitle, .se-section-documentTitle, .se-title-text');
                            if (el) {
                                const target = el.querySelector('[contenteditable="true"]') || el.querySelector('.se-text-paragraph') || el;
                                target.focus();
                                target.innerText = t;
                                target.dispatchEvent(new Event('input', { bubbles: true }));
                                target.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                        }
                    """, clean_title)
            except Exception:
                pass

        logger.info("✔ 제목 입력 및 검증 완료.")

    def _read_title(self) -> str:
        try:
            return self.frame.evaluate("""
                () => {
                    const el = document.querySelector('.se-documentTitle, .se-section-documentTitle, .se-title-text');
                    return el ? (el.innerText || el.textContent || '').trim() : '';
                }
            """) or ""
        except Exception:
            return ""

    def _ensure_title(self, title: str):
        """제목 칸이 입력한 값과 같은지 확인하고, 다르면 다시 입력한다."""
        want = re.sub(r"\s+", "", title)
        for _ in range(2):
            if re.sub(r"\s+", "", self._read_title()) == want:
                return
            logger.info("제목이 입력값과 달라 다시 입력합니다.")
            self._dismiss_all_popups()
            try:
                self.frame.locator(".se-section-documentTitle").first.click(force=True, timeout=1500)
                self.page.keyboard.press("Control+A")
                self.page.keyboard.press("Delete")
                self.page.keyboard.type(title, delay=15)
                self.page.wait_for_timeout(400)
            except Exception as e:
                logger.warning(f"제목 다시 입력 실패: {e}")

    def _check_body_written(self, expected_chars: int):
        """본문이 입력되지 않은 채 발행되는 일을 막는다 (입력 중 포커스를 빼앗긴 경우)."""
        _, total = self._measure_struck_chars(self.frame)
        if expected_chars > 200 and total < expected_chars * 0.6:
            self._save_diagnostic("body_missing")
            raise RuntimeError(f"본문이 제대로 입력되지 않았습니다 ({total}/{expected_chars}자). 다시 시도합니다.")

    def _focus_body_text_field(self) -> bool:
        """본문 입력 영역의 마지막 contenteditable 요소를 찾아 포커스합니다 (표 및 이미지 캡션 제외)."""
        contexts = [self.frame, self.page.main_frame] if self.frame != self.page.main_frame else [self.page.main_frame]
        for ctx in contexts:
            try:
                ok = ctx.evaluate("""
                    () => {
                        const visible = (el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                        };
                        const selectors = [
                            '.se-section-documentContent [contenteditable="true"]',
                            '.se-component.se-text [contenteditable="true"]',
                            '.se-text-paragraph [contenteditable="true"]',
                            '[class*="documentContent"] [contenteditable="true"]'
                        ];
                        let candidates = [];
                        for (const selector of selectors) {
                            candidates = candidates.concat(Array.from(document.querySelectorAll(selector)));
                        }
                        candidates = candidates
                            .filter((el, idx, arr) => arr.indexOf(el) === idx)
                            .filter(visible)
                            .filter((el) => !el.closest('.se-section-documentTitle, .se-documentTitle, [class*="title"]'))
                            .filter((el) => !el.closest('.se-component.se-table, td.se-cell'))
                            .filter((el) => !el.closest('.se-component.se-image, .se-image-container, .se-image-caption, .se-caption'));
                        if (!candidates.length) return false;
                        const target = candidates[candidates.length - 1];
                        target.focus();
                        const selection = window.getSelection();
                        const range = document.createRange();
                        range.selectNodeContents(target);
                        range.collapse(false);
                        selection.removeAllRanges();
                        selection.addRange(range);
                        return true;
                    }
                """)
                if ok:
                    return True
            except Exception:
                continue
        return False

    # ── 본문 서식: 소제목 굵게 24 / 본문 16 / 검은 글씨 ──
    HEADING_FONT_SIZE = 24
    BODY_FONT_SIZE = 16

    # 툴바 버튼 → 펼쳐진 목록의 항목을 페이지 안에서 한 번에 누른다 (자동화 왕복을 줄여 줄마다 1~2초 절약).
    _JS_PICK = """
        async ([btnSel, optSel]) => {
            const btn = document.querySelector(btnSel);
            if (!btn) return false;
            btn.click();
            for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 25));
                const opt = document.querySelector(optSel);
                if (opt && opt.offsetParent !== null) { opt.click(); return true; }
            }
            btn.click();   // 목록이 안 열렸으면 닫아 둔다
            return false;
        }
    """

    def _fast_pick(self, btn_sel: str, opt_sel: str) -> bool:
        try:
            if self.frame.evaluate(self._JS_PICK, [btn_sel, opt_sel]):
                self.page.wait_for_timeout(60)
                return True
        except Exception:
            pass
        return False

    def _set_font_size(self, size: int) -> bool:
        """툴바 글자 크기 드롭다운에서 size(px) 선택. 이후 입력하는 글자에 적용된다."""
        if self._fast_pick("button.se-font-size-code-toolbar-button",
                           f"button.se-toolbar-option-font-size-code-fs{size}-button"):
            return True
        f = self.frame
        try:
            f.locator("button.se-font-size-code-toolbar-button").first.click(timeout=3000)
            opt = f.locator(f"button.se-toolbar-option-font-size-code-fs{size}-button").first
            opt.wait_for(state="visible", timeout=3000)
            opt.click(timeout=3000)
            self.page.wait_for_timeout(150)
            return True
        except Exception as e:
            logger.warning(f"글자 크기 {size} 적용 실패: {e}")
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    def _set_bold(self, on: bool) -> bool:
        f = self.frame
        try:
            # 눌림 상태 확인과 클릭을 페이지 안에서 한 번에
            r = f.evaluate("""(on) => {
                const b = document.querySelector('button.se-bold-toolbar-button');
                if (!b) return null;
                const pressed = b.classList.contains('se-is-selected') || b.classList.contains('se-is-on') || b.getAttribute('aria-pressed') === 'true';
                if (pressed !== on) { b.click(); return 'clicked'; }
                return 'ok';
            }""", on)
            if r:
                if r == "clicked":
                    self.page.wait_for_timeout(60)
                return True
            btn = f.locator("button.se-bold-toolbar-button").first
            pressed = btn.evaluate("b => b.classList.contains('se-is-selected') || b.classList.contains('se-is-on') || b.getAttribute('aria-pressed') === 'true'", timeout=3000)
            if pressed != on:
                btn.click(timeout=3000)
                self.page.wait_for_timeout(120)
            return True
        except Exception as e:
            logger.warning(f"굵게 {'적용' if on else '해제'} 실패: {e}")
            return False

    def _set_font_color_black(self) -> bool:
        return self._set_font_color("#000000")

    def _type_disclosure(self, text: str):
        """수수료 안내 같은 작은 안내 문구: 13px 연한 회색 한 줄 → 이후 입력은 다시 본문 서식(16px 검정)."""
        try:
            self._focus_body_text_field()
            self.page.keyboard.press("Enter")
            self._set_bold(False)
            self._set_font_size(13)
            self._set_font_color("#999999")
            self._type_line_text(text)
            self.page.keyboard.press("Enter")
            self._set_font_size(16)
            self._set_font_color("#000000")
            logger.info(f"✔ 안내 문구 입력(연한 회색): {text}")
        except Exception as e:
            logger.warning(f"안내 문구 입력 실패: {e}")

    def _set_font_color(self, color: str) -> bool:
        if self._fast_pick("button.se-font-color-toolbar-button", f"button.se-color-palette[data-color='{color}']"):
            return True
        f = self.frame
        try:
            f.locator("button.se-font-color-toolbar-button").first.click(timeout=3000)
            sw = f.locator(f"button.se-color-palette[data-color='{color}']").first
            sw.wait_for(state="visible", timeout=3000)
            sw.click(timeout=3000)
            self.page.wait_for_timeout(150)
            return True
        except Exception as e:
            logger.warning(f"글자색 {color} 적용 실패: {e}")
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    # True: 본문 줄을 한 글자씩 치지 않고 줄 단위로 한 번에 입력 (발행 속도 향상, 서식은 줄마다 동일하게 적용)
    FAST_INPUT = True

    _AUTO_LIST_PREFIX = re.compile(r"^(\d{1,2}[.)]\s|[-*]\s)")

    def _type_line_text(self, text: str, delay: int = 12):
        """한 줄 입력. "1. ", "- " 로 시작하면 에디터가 번호/글머리 목록으로 자동 변환하므로
        접두부는 키 입력 이벤트 없이 insert_text로 넣고 나머지만 타이핑한다."""
        if self.FAST_INPUT:
            # 한 줄을 한 번에 입력 (붙여넣기와 같은 방식). 키 입력이 없어 목록 자동 변환도 일어나지 않는다
            if text:
                self.page.keyboard.insert_text(text)
            return
        m = self._AUTO_LIST_PREFIX.match(text)
        if m:
            self.page.keyboard.insert_text(m.group(1))
            text = text[m.end():]
        if text:
            self.page.keyboard.type(text, delay=delay)

    # 기본 스티커 묶음(motion2d_01)에서 분위기별 스티커 번호 (패널의 se-blind 이름 'motion2d_01-N')
    STICKER_PACK = "motion2d_01"
    STICKER_BY_MOOD = {"인사": 1, "포인트": 2, "주의": 3, "감사": 6}

    def _insert_sticker(self, mood: str) -> bool:
        """커서 위치(빈 줄)에 스티커 1개를 단독 컴포넌트로 삽입. 삽입 후 커서는 스티커 아래 새 문단."""
        f = self.frame
        name = f"{self.STICKER_PACK}-{self.STICKER_BY_MOOD.get(mood, 2)}"
        try:
            # 툴바 버튼은 패널을 토글하므로 이미 열려 있으면 누르지 않음
            if not f.locator(".se-sidebar-element-sticker").first.is_visible():
                f.locator("button.se-sticker-toolbar-button").first.click(timeout=3000)
            f.locator(".se-sidebar-element-sticker").first.wait_for(state="visible", timeout=5000)
            # 기본 묶음 탭 선택 (최근 사용/다른 묶음이 열려 있을 수 있음)
            target = f.locator(".se-sidebar-element-sticker", has=f.locator(".se-blind", has_text=re.compile(rf"^{re.escape(name)}$")))
            if not target.count():
                tabs = f.locator(".se-sidebar .se-tab-button:not(.se-tab-button-history):not(.se-tab-button-gif)")
                for i in range(min(tabs.count(), 10)):
                    tabs.nth(i).click(timeout=2000)
                    self.page.wait_for_timeout(500)
                    if target.count():
                        break
            if not target.count():
                logger.warning(f"스티커 '{name}'을(를) 찾지 못해 건너뜁니다.")
                return False
            before = f.locator(".se-component.se-sticker").count()
            target.first.click(timeout=3000)
            self.page.wait_for_timeout(800)
            ok = f.locator(".se-component.se-sticker").count() > before
            logger.info(f"{'✔' if ok else '⚠️'} 스티커 삽입 ({mood}: {name})")
            return ok
        except Exception as e:
            logger.warning(f"스티커 삽입 실패 ({mood}): {e}")
            return False

    MAP_MARKER = "[지도]"
    map_query = ""   # 설정의 지도 장소 검색어 (app.run_publish_task 가 지정)

    @property
    def placed_maps(self) -> list:
        """이번 글에 실제로 넣은 지도 (가게 이름·주소) — 완료 안내에 보여 준다."""
        if "_placed_maps" not in self.__dict__:
            self._placed_maps = []
        return self._placed_maps

    def _insert_place_map(self, query: str, prefer: str = "") -> bool:
        """[장소] 패널에서 query 검색 → 첫 결과 [추가] → [확인]. 지도는 커서 위치에 단독 컴포넌트로 들어가고
        커서는 지도 아래 새 문단으로 이동한다."""
        f = self.frame
        query = (query or "").strip()
        if not query:
            return False
        try:
            before = f.locator(".se-component.se-placesMap").count()
            map_btn = f.locator("button.se-map-toolbar-button").first
            try:
                map_btn.click(timeout=3000)
            except Exception:                  # 사진 도구 등이 버튼을 가리면 닫고 직접 누른다
                self._dismiss_all_popups()
                self.page.wait_for_timeout(400)
                map_btn.evaluate("b => b.click()")
            # 장소 패널 검색칸 (placeholder '장소명을 입력하세요.' — 하단 '글감 검색' 칸과 구분)
            box = f.locator("input[placeholder*='장소명'], input.react-autosuggest__input").first
            box.wait_for(state="visible", timeout=5000)
            self.page.wait_for_timeout(800)  # 패널(React) 초기화 대기
            # React 입력칸이라 fill() 대신 실제 타이핑으로 입력해야 검색이 동작
            # 주의: Ctrl+A 등 단축키 금지 — 포커스가 검색칸에 잡히기 전이면 본문 전체가 선택돼 덮어써짐
            box.click()
            box.fill("")
            box.type(query, delay=40)
            self.page.wait_for_timeout(300)
            box.press("Enter")
            items = f.locator("li.se-place-map-search-result-item")
            try:
                items.first.wait_for(state="attached", timeout=6000)
            except Exception:
                f.locator("button.se-place-search-button").first.click(timeout=2000)
                items.first.wait_for(state="attached", timeout=6000)
            # 이름에 찾는 가게가 들어간 결과 우선, 없으면 검색 첫 결과
            key = re.sub(r"\s+", "", prefer or "")
            names = [items.nth(i).locator(".se-place-map-search-result-title").inner_text(timeout=2000)
                     for i in range(min(items.count(), 7))]
            idx = next((i for i, n in enumerate(names) if key and key in re.sub(r"\s+", "", n)), 0)
            item = items.nth(idx)
            name = names[idx]
            try:
                addr = item.locator(".se-place-map-search-result-address").inner_text(timeout=1500)
            except Exception:
                addr = ""
            item.hover()
            item.locator("button.se-place-add-button").click(force=True, timeout=3000)
            self.page.wait_for_timeout(600)
            f.locator("button.se-popup-button-confirm").first.click(timeout=3000)
            self.page.wait_for_timeout(1500)
            ok = f.locator(".se-component.se-placesMap").count() > before
            logger.info(f"{'✔ 지도 삽입: ' + name + ' ' + addr if ok else '⚠️ 지도 삽입 확인 실패'} (검색어: {query})")
            if ok:
                self.placed_maps.append(f"{name} ({addr})" if addr else name)
            return ok
        except Exception as e:
            logger.warning(f"지도 삽입 실패 (검색어: {query}): {e}")
            try:
                f.locator("button.se-popup-close-button").first.click(timeout=1500)
            except Exception:
                pass
            return False

    def _insert_link_card(self, url: str) -> bool:
        """[링크] 버튼 → 주소 입력 → 미리보기 카드 확인 → [확인]. 카드는 커서 위치에 단독 컴포넌트로 들어간다.
        카드를 만들지 못하면 주소를 글자로 입력해 링크만이라도 남긴다."""
        f = self.frame
        try:
            before = f.locator(".se-component.se-oglink").count()
            f.locator("button.se-oglink-toolbar-button").first.click(timeout=3000)
            box = f.locator("input.se-popup-oglink-input").first
            box.wait_for(state="visible", timeout=5000)
            box.click()
            box.fill("")
            box.type(url, delay=15)
            box.press("Enter")
            f.locator(".se-popup-oglink-preview .se-section-oglink").first.wait_for(state="visible", timeout=12000)
            self.page.wait_for_timeout(500)
            f.locator(".se-popup-oglink button.se-popup-button-confirm").first.click(timeout=3000)
            self.page.wait_for_timeout(1500)
            ok = f.locator(".se-component.se-oglink").count() > before
            logger.info(f"{'✔ 상품 링크 카드 삽입' if ok else '⚠️ 상품 링크 카드 삽입 확인 실패'}: {url}")
            if ok:
                return True
        except Exception as e:
            logger.warning(f"상품 링크 카드 삽입 실패 ({url}): {e}")
            try:
                f.locator(".se-popup-oglink button.se-popup-close-button").first.click(timeout=1500)
            except Exception:
                pass
        # 대신 주소를 글자로 (에디터가 주소를 링크로 바꿔 준다)
        try:
            self._focus_body_text_field()
            self._apply_line_style(False)
            self._type_line_text(f"▶ 상품 자세히 보기: {url}")
            self.page.keyboard.press("Enter")
            logger.info(f"상품 링크를 글자로 입력했습니다: {url}")
        except Exception as e:
            logger.warning(f"상품 링크 글자 입력도 실패: {e}")
        return False

    def _insert_connect_card(self, product_id: str, name: str) -> bool:
        """[쇼핑커넥트] 버튼 → 상품명으로 검색 → 같은 상품(이름 일치)의 추가 버튼 → [추가하기].
        포털 공식 쇼핑커넥트 상품 카드가 커서 위치에 들어간다 (링크는 이미 발급된 것을 그대로 씀)."""
        f = self.frame
        try:
            before = f.locator(".se-component.se-shoppingConnect").count()
            f.locator("button.se-shopping-connect-toolbar-button").first.click(timeout=3000)
            box = f.locator(".se-popup-shopping-connect input.se-popup-search-input").first
            box.wait_for(state="visible", timeout=6000)
            box.click()
            box.fill("")
            box.type(name[:60], delay=15)
            box.press("Enter")
            self.page.wait_for_timeout(2500)
            want = re.sub(r"\s+", "", name)
            items = f.locator(".se-popup-shopping-connect li.se-shopping-connect-item")
            target = None
            for i in range(min(items.count(), 20)):
                it = items.nth(i)
                nm = re.sub(r"\s+", "", it.locator(".se-shopping-connect-item-name").inner_text(timeout=2000))
                if nm == want:
                    target = it
                    break
            if target is None:
                raise RuntimeError("검색 결과에서 같은 상품을 찾지 못함")
            target.hover()
            target.locator("button.se-shopping-connect-item-add-button").click(timeout=3000)
            f.locator("button.se-popup-button-confirm:has-text('추가하기')").first.click(timeout=5000)
            self.page.wait_for_timeout(2500)
            ok = f.locator(".se-component.se-shoppingConnect").count() > before
            logger.info(f"{'✔ 쇼핑커넥트 상품 카드 삽입' if ok else '⚠️ 쇼핑커넥트 카드 삽입 확인 실패'}: {name[:40]}")
            return ok
        except Exception as e:
            logger.warning(f"쇼핑커넥트 카드 삽입 실패 ({name[:40]}): {e}")
            for sel in (".se-popup-button-cancel", ".se-popup-shopping-connect button.se-popup-close-button"):
                try:
                    f.locator(sel).first.click(timeout=1500)
                except Exception:
                    pass
            return False

    def _center_all_stickers(self) -> int:
        """본문 입력이 모두 끝난 뒤 스티커·지도를 하나씩 선택해 가운데 정렬. (입력 중에 선택하면
        에디터 커서가 컴포넌트에 머물러 이후 타이핑이 사라지므로 마지막에 일괄 처리)"""
        f = self.frame
        stickers = f.locator(".se-component.se-sticker, .se-component.se-placesMap")
        done = 0
        for i in range(stickers.count()):
            try:
                st = stickers.nth(i)
                st.scroll_into_view_if_needed(timeout=3000)
                st.click(timeout=3000)
                self.page.wait_for_timeout(300)
                f.locator("button.se-property-toolbar-group-toggle-button.se-align-center-toolbar-button").first.click(timeout=3000)
                self.page.wait_for_timeout(300)
                done += 1
            except Exception as e:
                logger.warning(f"스티커 {i + 1} 가운데 정렬 실패: {e}")
        if done:
            logger.info(f"✔ 스티커·지도 {done}개 가운데 정렬")
        return done

    def _type_styled_lines(self, lines: List[str], delay: int = 12, plain: bool = False, keep_focus: bool = False):
        """본문 줄들을 서식과 함께 입력: 소제목(1. …) 굵게 24px, 본문 16px, 전체 검정.
        이미지 등 다른 컴포넌트 뒤 새 텍스트 블록은 기본 서식(15px)으로 돌아가므로 블록마다 상태를 새로 잡는다."""
        from text_style import is_subheading, sticker_mood
        from shopping_service import link_from_marker, connect_from_marker
        self._ensure_text_toolbar()
        self._set_font_color_black()
        if not keep_focus:
            self._focus_body_text_field()
        current = None  # None: 아직 미설정, True: 소제목 서식, False: 본문 서식
        for line in lines:
            line_str = line.rstrip()
            if not line_str.strip():
                self.page.keyboard.press("Enter")
                self.page.wait_for_timeout(80)
                continue
            if line_str.strip() == self.MAP_MARKER:
                if self._insert_place_map(self.map_query):
                    current = None
                    self._set_font_color_black()
                continue
            link = link_from_marker(line_str)
            if link:
                conn = connect_from_marker(line_str)
                if not (conn and self._insert_connect_card(conn["id"], conn["name"])):
                    self._insert_link_card(link)
                current = None
                self._set_font_color_black()
                continue
            mood = None if plain else sticker_mood(line_str)
            if mood:
                # 스티커는 글과 붙이지 않고 단독 줄로. 삽입 후 서식이 기본(15px)으로 돌아가므로 다시 지정
                if self._insert_sticker(mood):
                    current = None
                    self._set_font_color_black()
                continue
            text = clean_markdown_text(line_str)
            if not text:
                continue
            # 고정 문구(plain)는 사용자가 쓴 그대로 본문 서식 (예: '1. 주차 가능' 이 소제목으로 바뀌지 않게)
            heading = False if plain else is_subheading(text)
            if heading != current:
                self._apply_line_style(heading)
                current = heading
            self._type_line_text(text, delay=delay)
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(100)
        # 블록 뒤 이어지는 입력(해시태그 등)은 본문 서식으로
        if current is not False:
            self._apply_line_style(False)

    def _ensure_text_toolbar(self):
        """사진이 선택된 채로 남으면 글자 서식 버튼이 사라져 서식이 적용되지 않는다 → 사진 아래 새 문단으로 이동."""
        size_btn = self.frame.locator("button.se-font-size-code-toolbar-button").first
        try:
            if size_btn.count():
                return
            self._focus_body_text_field()
            self.page.wait_for_timeout(200)
            if size_btn.count():
                return
            logger.info("글자 서식 버튼이 보이지 않아 사진 아래로 커서를 옮깁니다.")
            self.frame.locator(".se-component-edge-button-bottom").last.click(force=True, timeout=2000)
            self.page.wait_for_timeout(300)
        except Exception as e:
            logger.debug(f"글자 서식 버튼 확보 실패: {e}")

    def _apply_line_style(self, heading: bool):
        """다음에 입력할 줄의 서식 설정: 소제목이면 굵게+24, 아니면 보통+16."""
        if heading:
            self._set_font_size(self.HEADING_FONT_SIZE)
            self._set_bold(True)
        else:
            self._set_font_size(self.BODY_FONT_SIZE)
            self._set_bold(False)

    def _attach_images(self, image_paths: List[str]):
        """Mublo 역공학 기반: N사 스마트에디터 ONE 이미지 세트 첨부 및 개별사진 배치 자동 선택"""
        if not image_paths:
            return

        valid_images = [os.path.abspath(p) for p in image_paths if os.path.exists(p)]
        if not valid_images:
            logger.warning("첨부할 유효한 이미지 파일이 없습니다.")
            return

        logger.info(f"본문에 이미지 세트 첨부 시도 ({len(valid_images)}개)...")
        self._dismiss_all_popups()
        contexts = [self.frame, self.page.main_frame] if self.frame != self.page.main_frame else [self.page.main_frame]

        uploaded = False

        # 1. 툴바 사진 버튼 클릭 & expect_file_chooser 주입 (N사 스마트에디터 ONE 표준)
        photo_selectors = [
            "button.se-image-toolbar-button",
            "button[data-name='image']",
            "button[data-log='dot.img']",
            "li.se-toolbar-item-image > button",
            "button[aria-label*='사진']",
            "button[aria-label*='이미지']",
            "button:has-text('사진')"
        ]

        for ctx in contexts:
            for sel in photo_selectors:
                try:
                    btn = ctx.locator(sel).first
                    if btn.count() > 0 and btn.is_visible(timeout=500):
                        try:
                            with self.page.expect_file_chooser(timeout=5000) as fc_info:
                                btn.click(force=True)
                            fc_info.value.set_files(valid_images)
                            uploaded = True
                            logger.info(f"✔ 툴바 사진 버튼 파일 선택자를 통한 이미지 {len(valid_images)}장 업로드 성공!")
                            self.page.wait_for_timeout(4000)
                            break
                        except Exception:
                            pass
                except Exception:
                    continue
            if uploaded:
                break

        # 2. 직접 input[type=file] 주입 시도 (폴백)
        if not uploaded:
            for sel in ("input[type='file'][accept*='image']", "input[type='file']", "input.se-image-upload-input", "input[name='image']"):
                for ctx in contexts:
                    try:
                        file_input = ctx.locator(sel).first
                        if file_input.count() > 0:
                            file_input.set_input_files(valid_images)
                            self.page.wait_for_timeout(4000)
                            uploaded = True
                            logger.info(f"✔ input[type=file] 직접 주입을 통한 이미지 업로드 성공 ({sel})")
                            break
                    except Exception:
                        pass
                if uploaded:
                    break

        # 3. [사진 첨부 방식] 모달이 뜬 경우 [개별사진] 라디오/레이블 자동 선택
        self.page.wait_for_timeout(1000)
        single_selectors = [
            "label[for='image-type-list']",
            "#image-type-list",
            ".se-image-type-option-list",
            "li[data-log='limgatt.ind']",
            "button:has-text('개별사진')",
            "label:has-text('개별사진')"
        ]
        for ctx in contexts:
            for s_sel in single_selectors:
                try:
                    single_target = ctx.locator(s_sel).first
                    if single_target.count() > 0 and single_target.is_visible(timeout=1000):
                        single_target.click(force=True)
                        logger.info(f"✔ 이미지 배치 모달 -> [개별사진] 선택 완료 ({s_sel})")
                        self.page.wait_for_timeout(1500)
                        break
                except Exception:
                    continue

        if not uploaded:
            logger.warning("사진 첨부 버튼을 찾지 못하여 본문 텍스트로만 포스팅을 진행합니다.")

    def _type_body(self, contents: List[str]):
        """본문 영역을 찾아 단락별로 줄바꿈 및 문단 간격을 살려 정밀 타이핑합니다."""
        logger.info(f"본문 입력 시작 ({len(contents)}개 라인)...")
        self._dismiss_all_popups()
        self._reset_inline_format(self.frame)
        self._ensure_body_not_struck_through(self.frame)

        # 본문 진입
        ok = self._focus_body_text_field()
        if not ok:
            logger.info("본문 JS 포커스 대기 중 -> Tab 키로 본문 진입")
            self.page.keyboard.press("Tab")
            self.page.wait_for_timeout(300)

        # 각 단락 및 빈 줄 간격 입력 (소제목 굵게 24 / 본문 16 / 검정)
        self._type_styled_lines(contents, delay=15)

        # 본문 타이핑 후 서식 리셋 및 취소선 최종 점검
        self._reset_inline_format(self.frame)
        self._ensure_body_not_struck_through(self.frame)
        logger.info("✔ 본문 입력 완료 (줄바꿈 및 문단 간격 보존, 취소선 차단 완료).")

    def _publish_panel_open(self) -> bool:
        try:
            return self.frame.locator("button[data-testid='seOnePublishBtn']:visible").count() > 0
        except Exception:
            return False

    def _open_publish_panel(self):
        """우측 상단의 [발행] 토글 버튼을 클릭하여 설정 패널을 엽니다."""
        self._dismiss_all_popups()
        if self._publish_panel_open():
            return
        logger.info("우측 상단 [발행] 설정 패널 열기 버튼 클릭...")
        contexts = [self.frame, self.page.main_frame] if self.frame != self.page.main_frame else [self.page.main_frame]

        clicked = False
        for ctx in contexts:
            try:
                clicked = ctx.evaluate("""
                    () => {
                        const candidates = Array.from(document.querySelectorAll('button, [role="button"], a'));
                        const visible = (el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                        };
                        for (const el of candidates) {
                            const area = el.getAttribute('data-click-area') || '';
                            const text = (el.innerText || el.textContent || '').trim();
                            if (!visible(el)) continue;
                            if (area === 'tpb.publish' || (text === '발행' && area.includes('publish'))) {
                                el.click();
                                return true;
                            }
                        }
                        for (const el of candidates) {
                            const text = (el.innerText || el.textContent || '').trim();
                            if (visible(el) && text === '발행') {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                if clicked:
                    break
            except Exception:
                continue

        if not clicked:
            btn = self.page.locator("button:has-text('발행'), [data-click-area='tpb.publish']")
            if btn.count() > 0 and btn.first.is_visible(timeout=3000):
                btn.first.click(force=True)
            else:
                self._save_diagnostic("publish_btn_not_found")
                raise RuntimeError("우측 상단 [발행] 버튼을 찾을 수 없습니다.")

        self.page.wait_for_timeout(1500)
        self._dismiss_all_popups()

    def _select_category(self, category_name: str):
        """
        발행 패널의 카테고리 드롭다운(button[data-click-area='tpb*i.category'])을 열고
        input[data-testid='categoryBtn_{no}'] 라디오의 label을 클릭한 뒤, 버튼 표시값으로 선택을 검증합니다.
        항목 id 형식은 '{categoryNo}_{categoryName}' 이며, category_name 으로 이 id 전체(웹 UI 값, 동명 하위
        카테고리 구분용) 또는 이름만(CLI/기존 설정값)을 받습니다.
        선택 검증에 실패하면 엉뚱한 카테고리로 발행되지 않도록 예외를 발생시킵니다.
        """
        category_name = (category_name or "").strip()
        if not category_name:
            return

        logger.info(f"카테고리 선택 시도: '{category_name}'")
        contexts = [self.frame, self.page.main_frame] if self.frame != self.page.main_frame else [self.page.main_frame]
        dropdown_sel = "button[data-click-area='tpb*i.category']"

        for ctx in contexts:
            dropdown = ctx.locator(dropdown_sel)
            if dropdown.count() == 0:
                continue

            # 1. 드롭다운 열기 (항목은 열려 있을 때만 DOM에 렌더링됨)
            if dropdown.first.get_attribute("aria-expanded") != "true":
                dropdown.first.click()
            try:
                ctx.wait_for_selector("input[data-testid^='categoryBtn_']", state="attached", timeout=5000)
            except Exception:
                self._save_diagnostic("category_list_not_open")
                raise RuntimeError("카테고리 드롭다운 목록이 열리지 않았습니다.")

            # 2. 카테고리 번호 확정 ('{no}_{name}' id 전체 일치 우선, 없으면 이름만 일치)
            target_no = ctx.evaluate("""
                (catName) => {
                    const inputs = Array.from(document.querySelectorAll("input[data-testid^='categoryBtn_']"));
                    const hit = inputs.find(el => el.id === catName)
                             || inputs.find(el => (el.id || '').replace(/^\\d+_/, '') === catName);
                    return hit ? hit.getAttribute('data-testid').replace('categoryBtn_', '') : null;
                }
            """, category_name)
            if not target_no:
                self._save_diagnostic("category_not_found")
                raise RuntimeError(f"카테고리 '{category_name}'를 발행 패널 목록에서 찾을 수 없습니다.")

            # 3. 해당 항목 label 클릭
            ctx.locator(f"input[data-testid='categoryBtn_{target_no}'] + label").first.click()
            self.page.wait_for_timeout(500)

            # 4. 드롭다운 버튼 표시값으로 검증
            shown = ctx.locator(f"{dropdown_sel} [data-testid='categoryItemText_{target_no}']")
            if shown.count() == 0:
                self._save_diagnostic("category_verify_failure")
                raise RuntimeError(f"카테고리 '{category_name}' 선택 후 검증에 실패했습니다.")
            logger.info(f"✔ 카테고리 '{category_name}' (no={target_no}) 선택 및 검증 완료.")
            return

        self._save_diagnostic("category_dropdown_not_found")
        raise RuntimeError("발행 패널에서 카테고리 드롭다운을 찾을 수 없습니다.")

    def _set_publish_options(self, public_open: bool = True):
        """
        공개 설정(전체공개 / 비공개)과 댓글·공감·검색 허용 체크박스를 설정하고 실제 적용 여부를 검증합니다.
        발행 패널 실측 id: 라디오 #open_public / #open_private, 체크박스 #publish-option-comment / -sympathy / -search.
        (input은 스타일상 숨겨져 있으므로 label[for=...] 을 클릭)
        """
        open_id = "open_public" if public_open else "open_private"
        checkbox_ids = ["publish-option-comment", "publish-option-sympathy", "publish-option-search"]
        contexts = [self.frame, self.page.main_frame] if self.frame != self.page.main_frame else [self.page.main_frame]

        for ctx in contexts:
            radio = ctx.locator(f"input#{open_id}")
            if radio.count() == 0:
                continue

            if not radio.is_checked():
                ctx.locator(f"label[for='{open_id}']").click()
                self.page.wait_for_timeout(300)
            if not radio.is_checked():
                self._save_diagnostic("open_type_failure")
                raise RuntimeError(f"공개 설정({'전체공개' if public_open else '비공개'}) 적용에 실패했습니다.")

            for cb_id in checkbox_ids:
                cb = ctx.locator(f"input#{cb_id}")
                if cb.count() == 0 or cb.is_disabled():
                    continue  # 비공개 시 공감허용 등은 비활성화됨
                if not cb.is_checked():
                    try:
                        ctx.locator(f"label[for='{cb_id}']").click(timeout=3000)
                        self.page.wait_for_timeout(200)
                    except Exception:
                        pass
                if not cb.is_checked():
                    logger.warning(f"⚠ 발행 옵션 '{cb_id}' 체크 실패 (발행은 계속 진행)")

            logger.info(f"✔ 공개 설정 '{'전체공개' if public_open else '비공개'}' 적용 및 검증 완료.")
            return

        self._save_diagnostic("open_type_not_found")
        raise RuntimeError("발행 패널에서 공개 설정 항목을 찾을 수 없습니다.")

    def _ensure_tags(self, tags: List[str]):
        """본문 하단 해시태그(#태그)는 에디터가 발행 태그로 자동 등록하므로 태그 입력란에 다시 쓰지 않는다.
        자동 등록된 태그가 하나도 없을 때만 예비로 직접 입력."""
        try:
            n = self.frame.locator("[id^='tag-item-']").count()
        except Exception:
            n = 0
        if n:
            logger.info(f"✔ 본문 해시태그 {n}개가 발행 태그로 자동 등록됨 (태그 입력란 중복 입력 생략)")
            return
        logger.warning("본문 해시태그가 발행 태그로 등록되지 않아 태그 입력란에 직접 입력합니다.")
        self._enter_tags(tags)

    def _enter_tags(self, tags: List[str]):
        """발행 패널의 태그 입력란에 태그들을 입력합니다."""
        if not tags:
            return

        logger.info(f"태그 입력 진행 ({len(tags)}개)...")
        contexts = [self.frame, self.page.main_frame] if self.frame != self.page.main_frame else [self.page.main_frame]

        tag_selectors = [
            "input[placeholder*='태그']",
            "input[class*='tag']",
            ".se-tag-input",
            "input[data-log*='tag']"
        ]

        for ctx in contexts:
            try:
                tag_input = None
                for sel in tag_selectors:
                    loc = ctx.locator(sel).first
                    if loc.count() > 0 and loc.is_visible(timeout=500):
                        tag_input = loc
                        break

                if tag_input:
                    tag_input.click(timeout=1000)
                    entered_count = 0
                    norm_tags = normalize_naver_tags(tags)
                    for clean_tag in norm_tags[:15]:
                        try:
                            tag_input.fill(clean_tag)
                            self.page.wait_for_timeout(150)
                            self.page.keyboard.press("Enter")
                            self.page.wait_for_timeout(200)
                            entered_count += 1
                        except Exception:
                            continue

                    # 실제 등록된 태그 칩(span#tag-item-{태그}) 개수로 검증
                    registered = ctx.locator("[id^='tag-item-']").count()
                    expected = len(norm_tags[:15])
                    if registered < expected:
                        logger.warning(f"⚠ 태그 {expected}개 중 {registered}개만 등록되었습니다.")
                    else:
                        logger.info(f"✔ 태그 {registered}개 등록 및 검증 완료.")
                    return
            except Exception:
                continue

        self._save_diagnostic("tag_input_not_found")
        logger.warning("⚠ 발행 패널 태그 입력란을 찾지 못해 태그 없이 발행합니다. (본문 하단 해시태그는 유지)")

    @staticmethod
    def normalize_schedule_time(schedule_at: datetime) -> datetime:
        """N사 예약 발행은 10분 단위만 지원하므로 다음 10분 단위로 올림합니다."""
        dt = schedule_at.replace(second=0, microsecond=0)
        if dt.minute % 10:
            dt += timedelta(minutes=10 - dt.minute % 10)
        return dt

    def _set_schedule(self, schedule_at: datetime):
        """
        발행 패널 [발행 시간]을 '예약'으로 바꾸고 날짜(jQuery UI datepicker)·시·분을 설정한 뒤 검증합니다.
        실측 DOM: #radio_time2(예약), input.input_date__*(읽기전용 'YYYY. MM. DD'),
        select[title='예약 발행 시간 선택'] / select[title='예약 발행 분 선택'](10분 단위).
        """
        dt = self.normalize_schedule_time(schedule_at)
        if dt <= datetime.now():
            raise RuntimeError(f"예약 시각({dt:%Y-%m-%d %H:%M})이 현재보다 이전입니다.")
        logger.info(f"예약 발행 시각 설정: {dt:%Y-%m-%d %H:%M}")

        f = self.frame
        f.locator("label[for='radio_time2']").click()
        f.wait_for_selector("[class^='option_time'] input[class^='input_date']", timeout=5000)

        # 1. 날짜: 달력을 열고 목표 연/월까지 '다음달' 이동 후 일자 클릭
        f.locator("[class^='option_time'] input[class^='input_date']").click()
        f.wait_for_selector(".ui-datepicker", timeout=5000)
        for _ in range(24):
            year = int(f.locator(".ui-datepicker-year").inner_text().strip())
            month = int(f.locator(".ui-datepicker-month").inner_text().strip().replace("월", ""))
            if (year, month) == (dt.year, dt.month):
                break
            f.locator(".ui-datepicker-next").click()
            self.page.wait_for_timeout(200)
        f.locator(".ui-datepicker td:not(.ui-state-disabled) button").filter(
            has_text=re.compile(rf"^{dt.day}$")).first.click()
        self.page.wait_for_timeout(300)

        # 2. 시/분
        f.locator("select[title='예약 발행 시간 선택']").select_option(f"{dt.hour:02d}")
        f.locator("select[title='예약 발행 분 선택']").select_option(f"{dt.minute:02d}")
        self.page.wait_for_timeout(300)

        # 3. 검증
        date_val = f.locator("[class^='option_time'] input[class^='input_date']").input_value().strip()
        hour_val = f.locator("select[title='예약 발행 시간 선택']").input_value()
        min_val = f.locator("select[title='예약 발행 분 선택']").input_value()
        expected = (f"{dt.year}. {dt.month:02d}. {dt.day:02d}", f"{dt.hour:02d}", f"{dt.minute:02d}")
        if (date_val, hour_val, min_val) != expected or not f.locator("#radio_time2").is_checked():
            self._save_diagnostic("schedule_verify_failure")
            raise RuntimeError(f"예약 시각 설정 검증 실패: 화면값 {date_val} {hour_val}:{min_val}, 기대값 {expected}")
        logger.info(f"✔ 예약 발행 시각 {dt:%Y-%m-%d %H:%M} 설정 및 검증 완료.")
        return dt

    def _open_schedule_list(self) -> bool:
        """글쓰기 화면의 [예약 발행 n건] 목록 열기. 예약 글이 없으면(0건·버튼 숨김) False."""
        btn = self.frame.locator("button[class^='reserve_btn'], button[class*=' reserve_btn']").first
        if not btn.count():
            return False
        try:
            txt = btn.inner_text(timeout=2000)
        except Exception:
            txt = ""
        if re.search(r"(?<!\d)0\s*건", txt) or not btn.is_visible():
            return False
        btn.click(timeout=5000)
        self.page.wait_for_timeout(1500)
        return True

    def cancel_scheduled(self, title: str, when: datetime) -> bool:
        """N사 예약 글 취소: 글쓰기 화면의 [예약 발행 n건] 목록에서 제목·예약 시각이 같은 항목을 삭제.
        이미 없으면(발행됐거나 직접 지운 경우) True."""
        editor_url = f"https://blog.naver.com/PostWriteForm.naver?blogId={self.blog_id}"
        self.page.goto(editor_url, wait_until="domcontentloaded", timeout=30000)
        self.page.wait_for_timeout(2500)
        self._resolve_editor_frame(timeout_sec=20.0)
        self._dismiss_all_popups()
        f = self.frame
        date_text = when.strftime("%Y.%m.%d %H:%M")
        if not self._open_schedule_list():
            logger.info("예약 글이 없습니다 (이미 발행·삭제됨).")
            return True
        items = f.locator("li:has(button[data-click-area='tpb*t.scheduledel'])")
        target = None
        for i in range(items.count()):
            it = items.nth(i)
            t = it.locator("strong").first.inner_text(timeout=2000).strip()
            d = it.locator("span[class^='date']").first.inner_text(timeout=2000).strip()
            if t == title.strip() and d == date_text:
                target = it
                break
        if target is None:
            logger.info(f"예약 목록에 '{title}' ({date_text}) 항목이 없습니다 (이미 발행·삭제됨).")
            return True
        # 삭제 버튼은 항목에 마우스를 올려야 나타남 → 올린 뒤 클릭, 안 되면 강제 클릭.
        # 클릭 중 오류가 나도 아래에서 목록을 다시 열어 실제로 지워졌는지로 판단
        del_btn = target.locator("button[data-click-area='tpb*t.scheduledel']").first
        try:
            target.scroll_into_view_if_needed(timeout=2000)
            target.hover(timeout=2000)
            self.page.wait_for_timeout(300)
            del_btn.click(timeout=3000)
        except Exception:
            try:
                del_btn.click(timeout=3000, force=True)
            except Exception:
                try:
                    del_btn.evaluate("b => b.click()")
                except Exception as e:
                    logger.warning(f"예약 삭제 버튼 클릭 오류 (삭제 여부를 다시 확인합니다): {e}")
        self.page.wait_for_timeout(1200)
        # 에디터 자체 확인 레이어 (확인/삭제 버튼)
        for sel in ("button:has-text('삭제'):visible", "button:has-text('확인'):visible"):
            try:
                b = f.locator(sel).last
                if b.count() and b.is_visible():
                    b.click(timeout=2000)
                    self.page.wait_for_timeout(1200)
                    break
            except Exception:
                pass
        # 검증: 목록을 다시 열어 같은 항목이 없어야 성공
        self.page.goto(editor_url, wait_until="domcontentloaded", timeout=30000)
        self.page.wait_for_timeout(2500)
        self._resolve_editor_frame(timeout_sec=20.0)
        self._dismiss_all_popups()
        f = self.frame
        if not self._open_schedule_list():
            logger.info(f"✔ N사 예약 취소 완료: '{title}' ({date_text})")
            return True
        rows = f.locator("li:has(button[data-click-area='tpb*t.scheduledel'])")
        for i in range(rows.count()):
            it = rows.nth(i)
            if (it.locator("strong").first.inner_text(timeout=2000).strip() == title.strip()
                    and it.locator("span[class^='date']").first.inner_text(timeout=2000).strip() == date_text):
                logger.warning(f"예약 삭제 확인 실패: '{title}' ({date_text})")
                return False
        logger.info(f"✔ N사 예약 취소 완료: '{title}' ({date_text})")
        return True

    def _schedule_exists(self, title: str, when: datetime) -> bool:
        """글쓰기 화면의 [예약 발행 n건] 목록에 이 글이 있는지 확인."""
        self.page.goto(f"https://blog.naver.com/PostWriteForm.naver?blogId={self.blog_id}",
                       wait_until="domcontentloaded", timeout=30000)
        self.page.wait_for_timeout(2500)
        self._resolve_editor_frame(timeout_sec=20.0)
        self._dismiss_all_popups()
        if not self._open_schedule_list():
            return False
        want, date_text = re.sub(r"\s+", "", clean_markdown_text(title)), when.strftime("%Y.%m.%d %H:%M")
        rows = self.frame.locator("li:has(button[data-click-area='tpb*t.scheduledel'])")
        for i in range(rows.count()):
            row = rows.nth(i)
            t = row.locator("strong").first.inner_text(timeout=2000)
            d = row.locator("span[class^='date']").first.inner_text(timeout=2000).strip()
            if re.sub(r"\s+", "", t) == want and d == date_text:
                return True
        return False

    def _find_recent_post(self, title: str) -> Tuple[Optional[str], bool]:
        """최근 공개 글에서 같은 제목 찾기 → (글 주소, 목록 확인 성공 여부)."""
        from urllib.parse import unquote_plus
        want = re.sub(r"\s+", "", clean_markdown_text(title))
        try:
            res = self.page.request.get(
                f"https://blog.naver.com/PostTitleListAsync.naver?blogId={self.blog_id}&viewdate=&currentPage=1"
                "&categoryNo=0&parentCategoryNo=0&countPerPage=10", timeout=15000)
            for post in json.loads(res.text().replace("\\'", "'")).get("postList", []):
                if re.sub(r"\s+", "", unquote_plus(post.get("title", ""))) == want:
                    return f"https://blog.naver.com/{self.blog_id}/{post['logNo']}", True
            return None, True
        except Exception as e:
            logger.warning(f"최근 글 목록 확인 실패: {e}")
            return None, False

    def _in_editor(self) -> bool:
        url = self.page.url
        return "PostWriteForm" in url or "Redirect=Write" in url

    def _wait_publish_result(self, timeout: float = 40.0) -> Tuple[str, str]:
        """[발행]을 누른 뒤 결과 대기 → ('left', '') 화면 이동 = 등록됨 | ('rejected', 사유) | ('timeout', '').
        거부되면 N사는 경고 안내만 잠깐 띄우고 패널을 닫은 채 에디터에 머문다."""
        deadline, warned_at, reason = time.time() + timeout, 0.0, ""
        while time.time() < deadline:
            if not self._in_editor():
                return "left", ""
            self._clear_blockers(prefer_confirm=True)
            toasts = self._take_toasts()
            if toasts:
                reason, warned_at = toasts[-1], time.time()
            # 경고 뒤에도 잠시 기다린다: 실제로 처리 중인 글을 거부로 오판하면 중복 발행이 된다
            if warned_at and time.time() - warned_at > 6 and not self._publish_panel_open():
                return "rejected", reason
            self.page.wait_for_timeout(400)
        return "timeout", ""

    def _submit_publish(self, title: str, tags: Optional[List[str]], category_name: str, public_open: bool,
                        schedule_at: Optional[datetime]) -> Tuple[Optional[datetime], str]:
        """발행 패널 설정 → 최종 [발행] → 결과 확인. 거부되면 원인을 처리하고 같은 화면에서 다시 시도.
        (예약 시각, 확인된 글 주소) 반환. 재시도해도 안전한 실패는 RuntimeError, 안전하지 않으면 PublishUnconfirmed."""
        when, state, reason = None, "", ""
        for attempt in range(1, 4):
            self._open_publish_panel()
            if category_name:
                self._select_category(category_name)
            self._set_publish_options(public_open=public_open)
            if tags:
                self._ensure_tags(tags)
            if schedule_at:
                when = self._set_schedule(schedule_at)
            self._take_toasts()
            if not self._confirm_publish():
                self._save_diagnostic("confirm_publish_failure")
                raise RuntimeError("최종 [발행] 확인 버튼 클릭에 실패했습니다.")
            state, reason = self._wait_publish_result()
            if state != "rejected":
                break
            logger.warning(f"⚠️ 발행이 거부되어 다시 시도합니다 ({attempt}/3): {reason}")
            self._save_diagnostic("publish_rejected")
            self._ensure_title(clean_markdown_text(title))
            self.page.wait_for_timeout(3000)
        if state == "left":
            return when, ""
        if state == "rejected":
            raise RuntimeError(f"블로그가 발행을 받아주지 않았습니다: {reason}")

        # 화면이 그대로 → 실제로 올라갔는지 확인한 뒤에만 판단 (모르고 다시 발행하면 중복)
        self._save_diagnostic("publish_timeout")
        logger.warning("발행 결과 화면으로 넘어가지 않아 실제 등록 여부를 확인합니다...")
        if when:
            try:
                if self._schedule_exists(title, when):
                    logger.info("✔ 예약 목록에서 글을 확인했습니다.")
                    return when, ""
            except NeedUserAction:
                raise
            except Exception as e:
                raise PublishUnconfirmed(f"예약 등록 여부를 확인하지 못했습니다. 블로그의 예약 목록을 직접 확인해 주세요. ({e})")
            raise RuntimeError("예약 목록에 글이 없습니다 (등록되지 않음).")
        url, checked = self._find_recent_post(title)
        if url:
            logger.info(f"✔ 최근 글 목록에서 발행을 확인했습니다: {url}")
            return None, url
        if checked and public_open:
            raise RuntimeError("최근 글 목록에 글이 없습니다 (발행되지 않음).")
        raise PublishUnconfirmed("발행 여부를 확인하지 못했습니다. 블로그를 직접 확인한 뒤 다시 시도해 주세요.")

    def _like_own_post(self, url: str) -> str:
        """발행을 마친 글에 공감 하나 누르기. 실패해도 발행 결과에는 영향 없음. 받은 url 그대로 돌려준다."""
        like_post(self.page, url)
        return url

    def _finish_scheduled(self, dt: datetime) -> str:
        """예약 발행은 공개 URL이 아직 없으므로 'scheduled:{시각}|{예약 글 URL}' 을 반환합니다."""
        m = re.search(r"logNo=(\d{6,})", self.page.url)
        post_url = f"https://blog.naver.com/{self.blog_id}/{m.group(1)}" if m else ""
        logger.info(f"🎉 예약 발행 등록 완료: {dt:%Y-%m-%d %H:%M} (예약 글: {post_url or '번호 미확인'})")
        return f"scheduled:{dt:%Y-%m-%d %H:%M}|{post_url}"

    def _accept_publish_modals(self):
        """최종 [발행] 뒤에 뜬 확인 창의 [확인]을 누른다. 대화상자 안의 버튼만 누른다
        (화면 전체에서 찾으면 상단 [발행] 버튼이 잡혀 설정 패널을 다시 열어 버린다)."""
        self.page.wait_for_timeout(800)
        self._clear_blockers(prefer_confirm=True)

    def _confirm_publish(self) -> bool:
        """발행 패널 하단의 최종 [발행] 확인 버튼을 클릭하고 후속 확인 모달을 수락합니다."""
        logger.info("최종 [발행] 확인 버튼 클릭...")
        contexts = [self.frame, self.page.main_frame] if self.frame != self.page.main_frame else [self.page.main_frame]

        confirm_selectors = [
            "button[data-testid='seOnePublishBtn']",
            "button.confirm_btn__byZZW",
            "button[class*='confirm_btn__']",
            "button[data-click-area='tpb*i.publish']"
        ]

        clicked = False
        for ctx in contexts:
            for sel in confirm_selectors:
                try:
                    loc = ctx.locator(sel).last
                    if loc.count() > 0 and loc.is_visible(timeout=1000):
                        loc.click(timeout=3000)
                        self.page.wait_for_timeout(1500)
                        logger.info(f"✔ 최종 [발행] 버튼 클릭 성공 (셀렉터: {sel})")
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                break

        if not clicked:
            # 마우스 좌표 클릭 폴백 (Mublo 역공학)
            for ctx in contexts:
                try:
                    pt = ctx.evaluate("""
                        () => {
                            const candidates = Array.from(document.querySelectorAll('button, [role="button"], a'));
                            const visible = (el) => {
                                const rect = el.getBoundingClientRect();
                                const style = window.getComputedStyle(el);
                                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                            };
                            const matches = candidates
                                .filter(visible)
                                .filter((el) => {
                                    const area = el.getAttribute('data-click-area') || '';
                                    const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                                    return area === 'tpb*i.publish' || text === '발행';
                                })
                                .map((el) => {
                                    const rect = el.getBoundingClientRect();
                                    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
                                });
                            return matches.length ? matches[matches.length - 1] : null;
                        }
                    """)
                    if pt:
                        self.page.mouse.click(pt["x"], pt["y"])
                        self.page.wait_for_timeout(1500)
                        logger.info(f"✔ 최종 [발행] 좌표 마우스 클릭 성공 (x={pt['x']}, y={pt['y']})")
                        clicked = True
                        break
                except Exception:
                    continue

        if clicked:
            self._accept_publish_modals()
            return True
        return False

    def publish(self, title: str, contents: List[str], tags: Optional[List[str]] = None,
                category_name: str = "", image_paths: Optional[List[str]] = None, public_open: bool = True,
                schedule_at: Optional[datetime] = None) -> str:
        """스마트에디터 ONE으로 글을 작성하고 최종 발행하여 실제 발행 글 URL을 반환합니다."""
        editor_url = f"https://blog.naver.com/PostWriteForm.naver?blogId={self.blog_id}"
        logger.info(f"스마트에디터 접속 중: {editor_url}")
        self.page.goto(editor_url, wait_until="domcontentloaded", timeout=30000)
        self.page.wait_for_timeout(2500)

        # 1. 에디터 프레임 동적 탐색 및 로딩 완결 대기
        self._resolve_editor_frame(timeout_sec=20.0)
        self._start_watch()

        # 2. '이어서 작성하시겠습니까?' 모달 및 도움말 팝업 제거
        self._dismiss_all_popups()

        # 3. 인라인 서식(취소선 등) 사전 리셋 및 해제
        self._reset_inline_format(self.frame)
        self._ensure_body_not_struck_through(self.frame)

        # 4. 제목 입력 (가시성 검사, 타이핑, 검증)
        try:
            self._type_title(title)
        except Exception as te:
            self._save_diagnostic("title_failure")
            raise te

        # 5. 본문 이미지 세트 첨부 (최소 3장 이상, 개별사진 배치)
        if image_paths:
            self._attach_images(image_paths)

        # 6. 본문 내용 가공 (태그 블록 자동 추가 보장)
        body_lines = list(contents)
        if tags:
            tag_line = build_hashtag_line(tags)
            if tag_line and not any(tag_line in l for l in body_lines):
                if body_lines and body_lines[-1].strip():
                    body_lines.append("")
                body_lines.append(tag_line)

        # 7. 본문 입력 (문단 간격 및 줄바꿈 보존, 마크다운 기호 제거)
        try:
            self._type_body(body_lines)
        except Exception as be:
            self._save_diagnostic("body_failure")
            raise be
        self._center_all_stickers()

        # 8~12. 발행 패널 설정 → 최종 발행 → 결과 확인
        self._check_body_written(sum(len(re.sub(r"\s+", "", l)) for l in contents))
        when, confirmed_url = self._submit_publish(title, tags, category_name, public_open, schedule_at)
        if when:
            return self._finish_scheduled(when)
        if confirmed_url:
            return self._like_own_post(confirmed_url)

        # 13. 실제 발행 완료 URL 대기 및 정밀 추출
        logger.info("발행 완료 대기 중... (PostView 및 logNo 감시)")
        published_url = None
        try:
            self.page.wait_for_url(
                re.compile(r"PostView|postView|blog\.naver\.com/[^/]+/\d+"),
                timeout=35000
            )
            curr = self.page.url
            m = re.search(r"(?:logNo=|/)(\d{6,})", curr)
            if m:
                published_url = f"https://blog.naver.com/{self.blog_id}/{m.group(1)}"
                logger.info(f"🎉 N사 블로그 글 발행 성공! 정규 URL: {published_url}")
                return self._like_own_post(published_url)
        except Exception:
            pass

        # 대기 타임아웃 시 iframe 내부 또는 현재 페이지에서 PostView logNo 정밀 검사
        try:
            for f in self.page.frames:
                f_url = f.url or ""
                m = re.search(r"(?:logNo=|/)(\d{6,})", f_url)
                if m:
                    published_url = f"https://blog.naver.com/{self.blog_id}/{m.group(1)}"
                    logger.info(f"✔ 프레임 URL에서 실제 발행 글 URL 추출 성공: {published_url}")
                    return self._like_own_post(published_url)
        except Exception:
            pass

        # 블로그 홈에서 최근 발행 글 logNo 정밀 추출
        try:
            logger.info("블로그 홈에서 최신 발행 글 URL 추출을 시도합니다...")
            home_url = f"https://blog.naver.com/{self.blog_id}"
            self.page.goto(home_url, wait_until="domcontentloaded", timeout=15000)
            self.page.wait_for_timeout(2500)
            extracted = self.page.evaluate("""
                (bId) => {
                    const links = Array.from(document.querySelectorAll('a[href*="blog.naver.com"], a[href*="logNo="], a[href*="PostView"]'));
                    for (const a of links) {
                        const href = a.href || '';
                        if (href.includes('PostWriteForm')) continue;
                        const m = href.match(/blog\\.naver\\.com\\/[^\\/]+\\/(\\d{6,})/) || href.match(/logNo=(\\d{6,})/);
                        if (m) return `https://blog.naver.com/${bId}/${m[1]}`;
                    }
                    return null;
                }
            """, self.blog_id)
            if extracted:
                published_url = extracted
                logger.info(f"✔ 블로그 홈에서 실제 발행 글 URL 추출 성공: {published_url}")
                return self._like_own_post(published_url)
        except Exception as e:
            logger.warning(f"블로그 홈 URL 추출 실패: {e}")

        # 포스팅이 실제로 등록되지 않은 상태에서 블로그 홈 주소를 성공 URL인 것처럼 반환하는 행위 원천 차단
        self._save_diagnostic("publish_failed_no_postview")
        raise PublishUnconfirmed("스마트에디터 ONE 글 발행 후 실제 포스팅 URL(PostView)로 전환되지 않았습니다. 발행 버튼 클릭 상태를 확인해주세요.")


def like_post(page, url: str) -> str:
    """글(blog.naver.com/아이디/번호)에 공감 하나. 결과: liked · already · nobtn · fail · bad_url. 예외를 내지 않는다."""
    m = re.search(r"blog\.naver\.com/([^/?#]+)/(\d{6,})", url or "")
    if not m:
        return "bad_url"
    try:
        page.goto(f"https://m.blog.naver.com/{m.group(1)}/{m.group(2)}", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2500)
        btn = page.locator("a.u_likeit_button._face, button.u_likeit_button, "
                           "a[class*='likeit_button'], button[class*='likeit_button']").first
        if btn.count() == 0:
            logger.info(f"공감 단추를 찾지 못해 공감은 건너뜁니다: {url}")
            return "nobtn"
        if (btn.get_attribute("aria-pressed") or "") == "true" or "on" in (btn.get_attribute("class") or "").split():
            logger.info(f"✔ 이미 공감한 글입니다: {url}")
            return "already"
        btn.scroll_into_view_if_needed()
        page.wait_for_timeout(600)
        btn.click()
        page.wait_for_timeout(1200)
        logger.info(f"✔ 발행한 글에 공감 1개를 눌렀습니다: {url}")
        return "liked"
    except Exception as e:
        logger.info(f"공감을 누르지 못했습니다 (발행은 정상): {e}")
        return "fail"
