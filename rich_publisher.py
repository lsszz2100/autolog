import os
import re
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
from publisher import SmartEditorPublisher, PublishUnconfirmed, clean_markdown_text, build_hashtag_line
from config import DATA_DIR
from text_style import is_subheading, is_hashtag_line, image_heading_slots

logger = logging.getLogger("naver.rich_publisher")

class RichSmartEditorPublisher(SmartEditorPublisher):
    """
    상용 블로그 형태의
    인용구 + 단독 사진 + 콜라주 사진들 + 서식 텍스트 인터리브드(Interleaved) 레이아웃 포스팅 엔진
    """

    def _type_quotation(self, quote_lines: List[str]):
        """스마트에디터 ONE 상단 따옴표 인용구 삽입 및 1줄 헤드라인 타이핑"""
        # 여러 줄이 들어오더라도 인용구는 반복 없이 정확히 단 1줄의 핵심 헤드라인만 입력
        clean_line = ""
        for l in quote_lines:
            c = clean_markdown_text(l)
            if c:
                clean_line = c
                break
        if not clean_line:
            return

        logger.info(f"인용구 1줄 삽입 시도: '{clean_line}'")
        self._dismiss_all_popups()
        self._reset_inline_format(self.frame)

        # 1. 툴바 따옴표 인용구 버튼 찾기
        q_selectors = [
            'button.se-insert-quotation-default-toolbar-button',
            'button[data-name="quotation"][data-value="default"]',
            'button[data-name="quotation"]'
        ]
        q_clicked = False
        contexts = [self.frame, self.page.main_frame] if self.frame != self.page.main_frame else [self.page.main_frame]

        for ctx in contexts:
            for sel in q_selectors:
                try:
                    btn = ctx.locator(sel).first
                    if btn.count() > 0 and btn.is_visible(timeout=500):
                        btn.click(force=True)
                        q_clicked = True
                        logger.info(f"✔ 인용구 툴바 버튼 클릭 성공: {sel}")
                        self.page.wait_for_timeout(1000)
                        break
                except Exception:
                    continue
            if q_clicked:
                break

        if not q_clicked:
            logger.warning("인용구 버튼을 찾지 못해 일반 텍스트로 대체합니다.")
            return

        # 2. 인용구 내부에 정확히 1줄 헤드라인만 타이핑
        self.page.keyboard.type(clean_line, delay=15)
        self.page.wait_for_timeout(400)

        # 3. 인용구 밖(아래)으로 커서 이동
        escaped = False
        for ctx in contexts:
            try:
                edge_btn = ctx.locator('.se-quotation .se-component-edge-button-bottom').first
                if edge_btn.count() > 0:
                    edge_btn.click(force=True)
                    escaped = True
                    logger.info("✔ 인용구 하단 엣지 버튼 클릭으로 본문 탈출")
                    self.page.wait_for_timeout(300)
                    break
            except Exception:
                continue

        if not escaped:
            self.page.keyboard.press("ArrowDown")
            self.page.keyboard.press("ArrowDown")
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(300)

        logger.info("✔ 1줄 인용구 블록 작성 완료.")

    def _attach_single_image(self, img_path: str):
        """단독 사진 1장 첨부"""
        logger.info(f"단독 사진 1장 첨부 시도: {os.path.basename(img_path)}")
        self._dismiss_all_popups()
        contexts = [self.frame, self.page.main_frame] if self.frame != self.page.main_frame else [self.page.main_frame]

        photo_selectors = [
            "button.se-image-toolbar-button",
            "button[data-name='image']",
            "li.se-toolbar-item-image > button",
            "button[aria-label*='사진']"
        ]

        uploaded = False
        for ctx in contexts:
            for sel in photo_selectors:
                try:
                    btn = ctx.locator(sel).first
                    if btn.count() > 0 and btn.is_visible(timeout=500):
                        with self.page.expect_file_chooser(timeout=5000) as fc_info:
                            btn.click(force=True)
                        fc_info.value.set_files([os.path.abspath(img_path)])
                        uploaded = True
                        logger.info(f"✔ 단독 이미지 업로드 성공: {img_path}")
                        self.page.wait_for_timeout(3000)
                        break
                except Exception:
                    continue
            if uploaded:
                break

        self._focus_body_text_field()
        self.page.wait_for_timeout(300)

    def _attach_collage_images(self, img_paths: List[str]):
        """사진 2장 이상 첨부 후 [콜라주] 레이아웃 선택"""
        logger.info(f"콜라주 사진 첨부 시도 ({len(img_paths)}장)...")
        self._dismiss_all_popups()
        contexts = [self.frame, self.page.main_frame] if self.frame != self.page.main_frame else [self.page.main_frame]

        photo_selectors = [
            "button.se-image-toolbar-button",
            "button[data-name='image']",
            "li.se-toolbar-item-image > button",
            "button[aria-label*='사진']"
        ]

        uploaded = False
        for ctx in contexts:
            for sel in photo_selectors:
                try:
                    btn = ctx.locator(sel).first
                    if btn.count() > 0 and btn.is_visible(timeout=500):
                        with self.page.expect_file_chooser(timeout=5000) as fc_info:
                            btn.click(force=True)
                        fc_info.value.set_files([os.path.abspath(p) for p in img_paths])
                        uploaded = True
                        logger.info(f"✔ 이미지 {len(img_paths)}장 업로드 완료 -> 콜라주 모달 대기")
                        self.page.wait_for_timeout(2000)
                        break
                except Exception:
                    continue
            if uploaded:
                break

        # [사진 첨부 방식] 모달에서 [콜라주] 선택
        collage_selectors = [
            "label[for='image-type-collage']",
            "#image-type-collage",
            ".se-image-type-option-collage",
            "button:has-text('콜라주')",
            "label:has-text('콜라주')",
            "li.se-image-type-item:has-text('콜라주')"
        ]

        selected_collage = False
        for _ in range(4):
            for ctx in contexts:
                for c_sel in collage_selectors:
                    try:
                        c_target = ctx.locator(c_sel).first
                        if c_target.count() > 0 and c_target.is_visible(timeout=1000):
                            c_target.click(force=True)
                            selected_collage = True
                            logger.info(f"✔ [콜라주] 레이아웃 선택 완료 ({c_sel})")
                            self.page.wait_for_timeout(2500)
                            break
                    except Exception:
                        continue
                if selected_collage:
                    break
            if selected_collage:
                break
            self.page.wait_for_timeout(500)

        # 본문 다음 커서 위치 확보
        self._focus_body_text_field()
        self.page.wait_for_timeout(300)

    def _type_text_block(self, lines: List[str]):
        """본문 단락 텍스트 블록 정밀 타이핑 (소제목 굵게 24 / 본문 16 / 검정)"""
        self._focus_body_text_field()
        self._type_styled_lines(lines, delay=12)
        self.page.wait_for_timeout(200)

    # 뒤에서 사진을 만드는 작업(image_service.ImageJob). 없으면 파일이 이미 있는 것으로 본다.
    image_job = None

    def _image_ready(self, path: str) -> bool:
        if self.image_job is not None and path in self.image_job.paths:
            if not self.image_job.is_done(path):
                logger.info(f"⏳ 사진 준비 대기 중: {os.path.basename(path)} "
                            f"({self.image_job.ready_count()}/{self.image_job.count}장 완료)")
            return self.image_job.wait(path, timeout=600)
        return os.path.exists(path)

    def publish_rich_layout(self, title: str, blocks: List[Dict[str, Any]], tags: Optional[List[str]] = None,
                            category_name: str = "", public_open: bool = True,
                            schedule_at: Optional[datetime] = None,
                            header_lines: Optional[List[str]] = None, footer_lines: Optional[List[str]] = None) -> str:
        """
        인터리브드 리치 블록 목록을 스마트에디터 ONE에 순차 렌더링하고 글을 발행합니다.
        
        Block 구조:
        - {"type": "quotation", "lines": [...]}
        - {"type": "single_image", "path": "..."}
        - {"type": "collage_images", "paths": ["...", "..."]}
        - {"type": "text", "lines": [...]}
        """
        editor_url = f"https://blog.naver.com/PostWriteForm.naver?blogId={self.blog_id}"
        logger.info(f"스마트에디터 접속 중: {editor_url}")
        self.page.goto(editor_url, wait_until="domcontentloaded", timeout=30000)
        self.page.wait_for_timeout(2500)
        if "PostWriteForm" not in self.page.url and "Redirect=Write" not in self.page.url:
            # 다른 사람 블로그이거나 블로그 주소가 틀리면 N사가 블로그 홈으로 보냄
            raise RuntimeError(f"blog.naver.com/{self.blog_id} 의 글쓰기 화면을 열 수 없습니다. "
                               f"이 계정의 블로그 주소(블로그 ID)가 맞는지 확인해 주세요.")

        # 1. 에디터 프레임 탐색
        self._resolve_editor_frame(timeout_sec=20.0)
        self._start_watch()
        self._dismiss_all_popups()
        self._reset_inline_format(self.frame)
        self._ensure_body_not_struck_through(self.frame)

        # 2. 제목 입력
        self._type_title(title)

        # 2-1. 상단 고정 영역 (상호명·연락처 등 + 지도): 맨 위 인용구 바로 아래 = 본문 시작
        #      (제목 직후엔 본문 입력칸이 아직 잡히지 않아 인용구 다음에 입력)
        # 쇼핑커넥트 글: 본문의 수수료 안내 줄은 빼고, 본문 첫 부분(맨 위 인용구 바로 아래)과 해시태그 바로 앞에 한 번씩 넣는다
        from shopping_service import is_shopping_post, is_disclosure, disclosure_for
        shopping = any(is_shopping_post(b.get("lines", [])) for b in blocks if b.get("type") == "text")
        disclosure = disclosure_for([l for b in blocks if b.get("type") == "text" for l in b.get("lines", [])])
        if shopping:
            blocks = [{**b, "lines": [l for l in b.get("lines", []) if not is_disclosure(l)]} if b.get("type") == "text" else b
                      for b in blocks]
        top_lines = ([disclosure, ""] if shopping else []) + (list(header_lines) + [""] if header_lines else [])
        if top_lines:
            # 쇼핑커넥트 수수료 안내는 본문 첫 부분에도 본문 글씨(16px 검정)로 (공정위 지침: 제목·첫 부분에 표시)
            pos = 1 if blocks and blocks[0].get("type") == "quotation" else 0
            blocks = blocks[:pos] + [{"type": "fixed", "lines": top_lines}] + blocks[pos:]

        # 3. 블록 순차 렌더링
        logger.info(f"총 {len(blocks)}개의 리치 블록 렌더링 시작...")
        for b_idx, block in enumerate(blocks):
            b_type = block.get("type")
            logger.info(f"--- [블록 {b_idx + 1}/{len(blocks)}] 타입: {b_type} ---")

            if b_type in ("single_image", "collage_images"):
                # 사진은 뒤에서 만드는 중일 수 있음 → 이 블록 사진만 기다리고, 끝내 못 만든 사진은 빼고 넣는다
                paths = [block.get("path", "")] if b_type == "single_image" else list(block.get("paths", []))
                paths = [p for p in paths if p and self._image_ready(p)]
                if len(paths) >= 2:
                    b_type, block = "collage_images", {"type": "collage_images", "paths": paths}
                elif len(paths) == 1:
                    b_type, block = "single_image", {"type": "single_image", "path": paths[0], "rep": block.get("rep")}
                else:
                    logger.warning("이 자리에 넣을 사진을 확보하지 못해 사진 없이 진행합니다.")
                    continue

            if b_type == "quotation":
                self._type_quotation(block.get("lines", []))
            elif b_type == "single_image":
                self._attach_single_image(block.get("path", ""))
                if block.get("rep") and not self._set_rep_image():
                    raise RuntimeError("가이드의 대표사진을 지정하지 못했습니다.")
            elif b_type == "collage_images":
                self._attach_collage_images(block.get("paths", []))
            elif b_type == "text":
                self._type_text_block(block.get("lines", []))
            elif b_type == "styled":
                self._type_review_lines(block.get("lines", []), pad_after=int(block.get("pad_after") or 0))
            elif b_type == "video":
                self._attach_video(block.get("path", ""), block.get("title", ""))
            elif b_type == "clip":
                self._insert_my_clip(str(block.get("id", "")), block.get("title", ""))
            elif b_type == "oglink":            # 클립 크리에이터에 올린 클립 (naver.me 주소 → 클립 카드)
                if not self._insert_link_card(str(block.get("url", ""))):
                    logger.warning(f"⚠️ 클립 카드 삽입 실패: {block.get('url', '')}")
            elif b_type == "fixed":
                logger.info(f"고정 문구 입력 ({len([l for l in block.get('lines', []) if l])}줄)")
                if block.get("review"):
                    self._ensure_text_toolbar()
                    self._focus_review_body()
                    if self.review_center:
                        self._set_align("center")
                    self._type_styled_lines(block.get("lines", []), plain=True, keep_focus=True)
                else:
                    self._focus_body_text_field()
                    self._type_styled_lines(block.get("lines", []), plain=True)
            else:
                logger.warning(f"알 수 없는 블록 타입: {b_type}")

            self.page.wait_for_timeout(300)

        # 3-1. 하단 고정 영역 (해시태그 위)
        if footer_lines:
            logger.info(f"하단 고정 문구 입력 ({len(footer_lines)}줄)")
            self._focus_body_text_field()
            self._type_styled_lines([""] + list(footer_lines), plain=True)

        # 3-2. 쇼핑커넥트 수수료 안내 (해시태그 바로 앞, 연한 회색)
        if shopping:
            self._type_disclosure(disclosure)

        # 4. 하단 해시태그 본문 블록 추가 (유지)
        if tags:
            tag_line = build_hashtag_line(tags)
            logger.info(f"본문 최하단 표준 해시태그 블록 입력: {tag_line}")
            self.page.keyboard.press("Enter")
            self.page.keyboard.type(tag_line, delay=12)
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(300)

        # 스티커 가운데 정렬 (입력이 모두 끝난 뒤 일괄)
        self._center_all_stickers()

        # 서식 리셋 및 점검
        self._reset_inline_format(self.frame)
        self._ensure_body_not_struck_through(self.frame)

        # 5. 스크린샷 캡처 (발행 직전 에디터 상태 기록)
        try:
            self.page.screenshot(path=str(DATA_DIR / "scratch_rich_before_publish.png"), full_page=True)
            logger.info("📸 발행 직전 리치 에디터 스크린샷 저장 완료: data/scratch_rich_before_publish.png")
        except Exception:
            pass

        # 6~10. 발행 패널 설정 → 최종 발행 → 결과 확인
        expected = sum(len(re.sub(r"\s+", "", l)) for block in blocks if block.get("type") == "text"
                       for l in block.get("lines", []) if not re.match(r"^\s*\[(사진|스티커|지도)", l))
        if any(b.get("type") == "styled" for b in blocks):
            import review_post
            expected += sum(review_post.body_chars(b.get("lines", [])) for b in blocks if b.get("type") == "styled")
        self._check_body_written(expected)
        when, confirmed_url = self._submit_publish(title, tags, category_name, public_open, schedule_at)
        if when:
            return self._finish_scheduled(when)
        if confirmed_url:
            return self._like_own_post(confirmed_url)

        # 11. 실제 발행 글 URL 추출
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

        # 블로그 홈에서 추출
        try:
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
                return self._like_own_post(extracted)
        except Exception:
            pass

        self._save_diagnostic("publish_no_url")
        raise PublishUnconfirmed("글이 올라갔는지 확인하지 못했습니다. 블로그를 직접 확인한 뒤 다시 시도해 주세요.")

    # ── 체험단·기자단 글 (review_post 표기 그대로 입력) ──
    review_center = False      # True면 글 전체 가운데 정렬

    def _set_toggle(self, cls: str, on: bool) -> bool:
        """굵게·밑줄처럼 눌림 상태가 있는 툴바 버튼을 원하는 상태로."""
        try:
            r = self.frame.evaluate("""([cls, on]) => {
                const b = document.querySelector('button.' + cls);
                if (!b) return null;
                const pressed = b.classList.contains('se-is-selected') || b.classList.contains('se-is-on') || b.getAttribute('aria-pressed') === 'true';
                if (pressed !== on) { b.click(); return 'clicked'; }
                return 'ok';
            }""", [cls, on])
            if r == "clicked":
                self.page.wait_for_timeout(60)
            return bool(r)
        except Exception as e:
            logger.warning(f"{cls} {'켜기' if on else '끄기'} 실패: {e}")
            return False

    def _set_bg_color(self, color: str) -> bool:
        """글자 배경색(형광펜). color='' 이면 배경색 없음."""
        opt = f"button.se-color-palette[data-color='{color}']"
        if self._fast_pick("button.se-background-color-toolbar-button", opt):
            return True
        f = self.frame
        try:
            f.locator("button.se-background-color-toolbar-button").first.click(timeout=3000)
            sw = f.locator(opt).first
            sw.wait_for(state="visible", timeout=3000)
            sw.click(timeout=3000)
            self.page.wait_for_timeout(150)
            return True
        except Exception as e:
            logger.warning(f"배경색 {color or '없음'} 적용 실패: {e}")
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    def _set_align(self, where: str = "center") -> bool:
        label = {"center": "가운데 정렬", "left": "왼쪽 정렬", "right": "오른쪽 정렬"}.get(where, "가운데 정렬")
        f = self.frame
        try:
            f.locator("button.se-property-toolbar-drop-down-button[class*='se-align-']").first.click(timeout=3000)
            f.locator(f"button.se-toolbar-option-icon-button:has-text('{label}')").first.click(timeout=3000)
            self.page.wait_for_timeout(100)
            return True
        except Exception as e:
            logger.warning(f"{label} 적용 실패: {e}")
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    def _insert_divider(self) -> bool:
        try:
            self.frame.locator("button.se-document-toolbar-icon-select-button:has-text('구분선')").first.click(timeout=3000)
            self.page.wait_for_timeout(500)
            logger.info("✔ 구분선 삽입")
            return True
        except Exception as e:
            logger.warning(f"구분선 삽입 실패: {e}")
            return False

    def _attach_video(self, path: str, title: str = "") -> bool:
        """동영상 업로드: [동영상] → [동영상 추가] 파일 선택 → 업로드 완료 대기 → 제목(필수) → [완료]."""
        import os
        f = self.frame
        if not path or not os.path.exists(path):
            logger.warning(f"동영상 파일이 없습니다: {path}")
            return False
        name = os.path.basename(path)
        try:
            before = f.locator(".se-component.se-video").count()
            f.locator("button.se-video-toolbar-button").first.click(timeout=5000)
            self.page.wait_for_timeout(1200)
            with self.page.expect_file_chooser(timeout=10000) as fc:
                f.get_by_text("동영상 추가").last.click(timeout=5000)
            fc.value.set_files(path)
            size_mb = os.path.getsize(path) / 1048576
            logger.info(f"🎬 동영상 업로드 중: {name} ({size_mb:.0f}MB)")
            # 큰 파일은 오래 걸린다: 1MB당 3초, 최소 2분·최대 40분
            limit = min(2400, max(120, int(size_mb * 3)))
            box = f.locator("input[placeholder*='제목을 입력']").first
            done = False
            for _ in range(limit // 3):
                self.page.wait_for_timeout(3000)
                try:
                    if f.get_by_text("업로드 완료").count() and box.is_visible():
                        done = True
                        break
                    if f.get_by_text("업로드 실패").count():
                        break
                except Exception:
                    pass
            if not done:
                raise RuntimeError("동영상 업로드가 끝나지 않았습니다")
            box.fill((title or os.path.splitext(name)[0])[:40])
            f.locator("button:has-text('완료')").last.click(timeout=5000)
            self.page.wait_for_timeout(2500)
            ok = f.locator(".se-component.se-video").count() > before
            logger.info(f"{'✔ 동영상 삽입' if ok else '⚠️ 동영상 삽입 확인 실패'}: {name}")
            return ok
        except Exception as e:
            logger.warning(f"동영상 삽입 실패 ({name}): {e}")
            for sel in (".se-popup-close-button", "button.se-popup-button-cancel"):
                try:
                    f.locator(sel).first.click(timeout=1500)
                except Exception:
                    pass
            return False

    def _insert_my_clip(self, clip_id: str, title: str = "") -> bool:
        """[내 클립] 패널에서 클립 번호(clip/123)가 같은 것을 골라 넣는다."""
        f = self.frame
        try:
            before = f.locator(".se-component.se-oglink").count()
            f.locator("button.se-moment-toolbar-button").first.click(timeout=5000)
            self.page.wait_for_timeout(2500)
            target = None
            for _ in range(15):          # 목록은 내리면서 더 불러온다
                el = f.locator(f".se-sidebar-element-moment:has(a[href*='/clip/{clip_id}?']), "
                               f".se-sidebar-element-moment:has(a[href$='/clip/{clip_id}'])").first
                if el.count():
                    target = el
                    break
                f.evaluate("() => { const l = document.querySelector('.se-sidebar-list-moment'); if (l) l.scrollTop += 600; }")
                self.page.wait_for_timeout(900)
            if target is None:
                raise RuntimeError("내 클립 목록에서 찾지 못함")
            target.scroll_into_view_if_needed(timeout=3000)
            target.hover()
            target.locator(".se-sidebar-moment-button").click(timeout=5000)
            self.page.wait_for_timeout(2500)
            ok = f.locator(".se-component.se-oglink").count() > before
            logger.info(f"{'✔ 클립 삽입' if ok else '⚠️ 클립 삽입 확인 실패'}: {title[:40] or clip_id}")
            return ok
        except Exception as e:
            logger.warning(f"클립 삽입 실패 ({title[:40] or clip_id}): {e}")
            return False
        finally:
            try:   # 패널 닫기
                if f.locator(".se-sidebar-container-moment").count():
                    f.locator("button.se-moment-toolbar-button").first.click(timeout=2000)
            except Exception:
                pass

    def _set_rep_image(self) -> bool:
        """방금 넣은(맨 아래) 사진을 대표 사진(목록 썸네일)으로 지정: 사진을 누르면 뜨는 [대표] 버튼."""
        try:
            imgs = self.frame.locator(".se-component.se-image")
            n = imgs.count()
            if not n:
                logger.warning("대표 사진 지정: 사진 칸을 찾지 못했습니다.")
                return False
            comp = imgs.nth(n - 1)
            comp.scroll_into_view_if_needed(timeout=3000)
            comp.locator("img").first.click(timeout=3000)
            self.page.wait_for_timeout(500)
            for sel in (".se-set-rep-image-button", "button:has-text('대표')"):
                btn = comp.locator(sel).first
                if btn.count():
                    if "se-is-selected" not in (btn.get_attribute("class") or ""):
                        btn.click(force=True, timeout=3000)
                        self.page.wait_for_timeout(400)
                    ok = "se-is-selected" in (btn.get_attribute("class") or "")
                    logger.info(f"✔ 대표 사진 지정 {'완료' if ok else '실패 (선택 표시 없음)'}")
                    return ok
            logger.warning("대표 사진 지정: [대표] 버튼을 찾지 못했습니다 (첫 사진이 대표가 됩니다).")
            return False
        except Exception as e:
            logger.warning(f"대표 사진 지정 실패: {e}")
            return False
        finally:
            self._focus_body_text_field()

    def _review_base_style(self):
        """본문 기본 서식 (16px·검정·굵게 해제), 가운데 정렬 설정이면 정렬까지."""
        self._set_font_size(self.BODY_FONT_SIZE)
        self._set_bold(False)
        self._set_font_color_black()
        if self.review_center:
            self._set_align("center")

    def _focus_review_body(self):
        """체험단 글의 다음 입력 자리로 커서를 옮긴다."""
        if not self._review_started and not self.frame.locator(".se-component:not(.se-documentTitle):not(.se-text)").count():
            # 제목 바로 다음은 본문 칸이 아직 잡히지 않아 본문 첫 줄을 직접 눌러 커서를 둔다
            try:
                self.frame.locator(".se-component.se-text .se-text-paragraph").first.click(timeout=5000)
                self.page.wait_for_timeout(300)
            except Exception:
                self._focus_body_text_field()
        else:
            self._focus_body_text_field()
        self._review_started = True

    def _type_review_lines(self, lines: List[str], pad_after: int = 0):
        """review_post 표기(**굵게** ==형광펜== __밑줄__ {{색:글}} ## 소제목 [스티커] [구분선] [지도] [링크:주소])를 서식으로 입력.
        글은 서식을 담은 HTML로 붙여넣는다 (한 글자씩 서식을 켜고 끄며 치면 에디터가 글자를 겹쳐 넣는다)."""
        import review_post as rp
        self._ensure_text_toolbar()
        self._focus_review_body()
        pending: List[str] = []

        def flush():
            if not pending:
                return
            html = "".join(rp.line_html(l, self.HEADING_FONT_SIZE, self.BODY_FONT_SIZE, self.review_center) for l in pending)
            if not self._paste_html(html):
                logger.warning("서식 붙여넣기가 되지 않아 글자만 입력합니다.")
                for l in pending:
                    t = rp.plain_text(l[3:] if rp.is_heading(l) else l).strip()
                    if t:
                        self._type_line_text(t)
                    self.page.keyboard.press("Enter")
            else:
                self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(150)
            self._review_base_style()           # 다음 입력(해시태그·다음 글)은 본문 서식으로
            pending.clear()

        def component(ok: bool):
            if ok:      # 스티커·지도 등을 넣으면 커서가 그 아래로 간다
                self._focus_body_text_field()

        for raw in lines:
            s = raw.strip()
            s = rp.norm_marker(s) if s.startswith(("[", "［")) else s
            m = rp.STICKER_RE.match(s)
            if m:
                flush(); component(self._insert_sticker(m.group(1) or "포인트"))
                continue
            if s == "[구분선]":
                flush(); component(self._insert_divider())
                continue
            q = rp.map_marker(s)
            if q is not None:
                flush()
                tries = [x.strip() for x in (q or self.map_query).split("|") if x.strip()]
                store = tries[-1] if tries else ""
                if not any(self._insert_place_map(x, prefer=store) for x in tries):
                    if rp.map_optional(s):      # 원고에서 찾아 넣은 지도는 못 찾으면 빼고 발행
                        logger.info(f"ℹ️ 원고에서 찾은 장소로 지도를 찾지 못해 지도 없이 발행합니다 (검색어: {', '.join(tries)})")
                        continue
                    raise RuntimeError(                 # 가이드·원고가 요구한 지도가 빠진 채 발행 금지
                        f"지도를 넣지 못했습니다 (검색어: {', '.join(tries) or '없음'}). "
                        f"본문의 [지도:…] 줄에 정확한 가게 이름을 넣어 주세요.")
                component(True)
                continue
            m = rp.LINK_RE.match(s)
            if m:
                flush(); self._insert_link_card(m.group(1)); component(True)
                continue
            pending.append(raw.rstrip())
        # 블록 끝 빈 줄은 정해진 수(pad_after)만큼만 (다음 사진·인용구와의 간격)
        while pending and not pending[-1].strip():
            pending.pop()
        pending.extend([""] * pad_after)
        flush()

    _review_started = False

    def _paste_html(self, html: str) -> bool:
        """에디터의 입력 창(input_buffer)에 붙여넣기 이벤트로 서식 있는 HTML을 넣는다."""
        buf = next((fr for fr in self.page.frames if fr.name.startswith("input_buffer")), None)
        if buf is None:
            return False
        try:
            ok = buf.evaluate("""(html) => {
                const dt = new DataTransfer();
                dt.setData('text/html', html);
                const tmp = document.createElement('div'); tmp.innerHTML = html;
                dt.setData('text/plain', [...tmp.querySelectorAll('p')].map(p => p.innerText).join('\\n'));
                const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
                (document.activeElement || document.body).dispatchEvent(ev);
                return ev.defaultPrevented;
            }""", html)
            self.page.wait_for_timeout(600)
            return bool(ok)
        except Exception as e:
            logger.warning(f"서식 붙여넣기 실패: {e}")
            return False

def _quote_block(title: str) -> Dict[str, Any]:
    # 제목 앞단 대괄호([강남역 맛집], (솔직후기) 등)를 정돈하여 핵심 1줄 카피 추출
    clean_quote = re.sub(r"^[\[\(].*?[\]\)]\s*", "", title).strip() or title
    return {"type": "quotation", "lines": [clean_markdown_text(clean_quote)]}


def _blocks_by_headings(title: str, lines: List[str], heads: List[int], image_paths: List[str]) -> List[Dict[str, Any]]:
    """인용구 → 대표 사진 → 도입 → [사진] 1번 … 순서로, 사진은 image_heading_slots 경계에만 넣는다."""
    imgs = image_paths
    slots = image_heading_slots(len(heads), len(imgs))
    blocks: List[Dict[str, Any]] = [_quote_block(title)]
    if imgs:
        blocks.append({"type": "single_image", "path": imgs[0]})

    def add_text(seg: List[str]):
        if any(x.strip() for x in seg):
            blocks.append({"type": "text", "lines": seg})

    cursor = 0
    for k, h in enumerate(heads):
        kind = slots.get(k)
        if not kind:
            continue
        add_text(lines[cursor:h])
        if kind == "collage1":
            blocks.append({"type": "collage_images", "paths": [imgs[1], imgs[2]]})
        elif kind == "collage2":
            blocks.append({"type": "collage_images", "paths": [imgs[3], imgs[4]]})
        elif kind == "closing":
            blocks.append({"type": "single_image", "path": imgs[5] if len(imgs) >= 6 else imgs[-1]})
        cursor = h
    add_text(lines[cursor:])
    return blocks


def _blocks_with_user_photos(title: str, lines: List[str], user_photos: Dict[int, str],
                             ai_paths: List[str]) -> List[Dict[str, Any]]:
    """사용자 사진 배치: 본문의 '[사진:N]' 줄 자리에 N번 사진. 붙어 있는 사진 2장은 콜라주.
    AI 사진(모자란 장수만큼)은 대표 자리(비어 있으면)와 사진 없는 소제목 아래에 고르게."""
    from user_media import photo_marker
    ai = list(ai_paths)
    lines = [l for l in lines if (n := photo_marker(l)) is None or n in user_photos]
    first_text = next((i for i, l in enumerate(lines) if l.strip() and photo_marker(l) is None), len(lines))
    has_top = any(photo_marker(l) is not None for l in lines[:first_text])

    def photo_follows(i: int) -> bool:
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        return j < len(lines) and photo_marker(lines[j]) is not None

    heads = [i for i, l in enumerate(lines) if is_subheading(l)]
    free = [h for h in heads if not photo_follows(h)]
    seq: List[tuple] = []
    if not has_top and ai:
        seq.append(("img", ai.pop(0)))
    ai_heads = {}
    if ai and free:
        m = min(len(ai), len(free))
        for j in range(m):
            ai_heads[free[int((j + 0.5) * len(free) / m)]] = ai.pop(0)
    for i, l in enumerate(lines):
        n = photo_marker(l)
        if n is not None:
            seq.append(("img", user_photos[n]))
            continue
        if is_hashtag_line(l):
            continue
        seq.append(("text", l.strip()))
        if i in ai_heads:
            seq.append(("img", ai_heads[i]))

    blocks: List[Dict[str, Any]] = [_quote_block(title)]
    text: List[str] = []
    imgs: List[str] = []

    def flush_imgs():
        for k in range(0, len(imgs), 2):
            pair = imgs[k:k + 2]
            blocks.append({"type": "collage_images", "paths": pair} if len(pair) == 2
                          else {"type": "single_image", "path": pair[0]})
        imgs.clear()

    for kind, v in seq:
        if kind == "img":
            if any(x.strip() for x in text):
                blocks.append({"type": "text", "lines": text[:]})
            text.clear()
            imgs.append(v)
        else:
            if imgs and not v:
                continue            # 사진 사이·바로 뒤 빈 줄은 건너뜀 (붙은 사진 = 콜라주)
            flush_imgs()
            text.append(v)
    flush_imgs()
    if any(x.strip() for x in text):
        blocks.append({"type": "text", "lines": text})
    return blocks


def build_auto_rich_blocks(title: str, lines: List[str], image_paths: List[str],
                           user_photos: Optional[Dict[int, str]] = None) -> List[Dict[str, Any]]:
    """
    * user_photos({번호: 경로})가 있으면 본문 '[사진:N]' 자리에 사용자 사진을 우선 배치하고
      image_paths(AI 사진)는 빈자리만 채운다 (_blocks_with_user_photos).

    일반 텍스트 라인들과 이미지 목록(6장 권장)을 분석하여
    타겟 블로그 스타일의 구조화된 인터리브드 리치 블록 리스트를 자동 조립합니다.
    - 블록 1: 상단 따옴표 인용구 (제목 1줄 헤드라인으로 깔끔하게 축소)
    - 블록 2: 대표 단독 썸네일 1장
    - 블록 3: 섹션 1 (도입부)
    - 블록 4: 2장 콜라주 결합 (이미지 1, 2)
    - 블록 5: 섹션 2 (상세 정보/본문)
    - 블록 6: 2장 콜라주 결합 (이미지 3, 4)
    - 블록 7: 섹션 3 (디테일/후기)
    - 블록 8: 마무리 단독 사진 1장 (이미지 5)
    - 블록 9: 총평 및 마무리 텍스트 (마무리 사진 뒤에 배치)
    * 해시태그는 본문 텍스트에서 완전히 걸러내어, 퍼블리셔가 최하단에 단 1회만 단독 입력합니다.
    """
    if user_photos:
        return _blocks_with_user_photos(title, lines, user_photos, image_paths)
    # 사용자 사진이 없을 때 남은 표시 줄은 지움
    from user_media import photo_marker
    lines = [l for l in lines if photo_marker(l) is None]

    # 본문 텍스트 내에 포함된 해시태그 라인 완벽 제거 (중복 및 사진 앞뒤 분할 노출 원천 차단)
    clean_lines = [l.strip() for l in lines if not is_hashtag_line(l)]

    # 번호 소제목이 2개 이상이면: 사진을 정해진 '번호 사이'(text_style.image_heading_slots)에 배치.
    # 스티커 표시 줄은 같은 규칙으로 사진 없는 번호 사이에 들어가 있어 서로 붙지 않는다.
    heads = [i for i, l in enumerate(clean_lines) if is_subheading(l)]
    if len(heads) >= 2:
        return _blocks_by_headings(title, clean_lines, heads, image_paths)
    
    # 텍스트를 4개의 섹션으로 분할 (도입부, 상세1, 상세2, 총평)
    sections: List[List[str]] = []
    current_sec: List[str] = []
    
    for l in clean_lines:
        # 소제목 마커 감지 (📌, 💡, ✔️, 1., 2., [ 등)
        if is_subheading(l) and len(current_sec) > 3:
            sections.append([x for x in current_sec if not is_hashtag_line(x)])
            current_sec = [l]
        else:
            current_sec.append(l)
            
    if current_sec:
        sections.append([x for x in current_sec if not is_hashtag_line(x)])
        
    # 만약 소제목 기준 분할이 4개 미만이면 균등 분할
    if len(sections) < 4:
        n = max(1, len(clean_lines) // 4)
        sections = [
            clean_lines[0:n],
            clean_lines[n:2*n],
            clean_lines[2*n:3*n],
            clean_lines[3*n:]
        ]
        sections = [[x for x in s if not is_hashtag_line(x)] for s in sections if s]

    blocks = []
    
    # 1. 상단 인용구 (단 1줄 깔끔한 헤드라인으로 축소)
    # 제목 앞단 대괄호([강남역 맛집], (솔직후기) 등)를 정돈하여 핵심 1줄 카피 추출
    clean_quote = re.sub(r"^[\[\(].*?[\]\)]\s*", "", title).strip() or title
    clean_quote = clean_markdown_text(clean_quote)
    blocks.append({
        "type": "quotation",
        "lines": [clean_quote]
    })
    
    # 2. 대표 단독 사진 (img 0)
    if len(image_paths) >= 1:
        blocks.append({"type": "single_image", "path": image_paths[0]})
        
    # 3. 섹션 1 (도입부)
    if len(sections) >= 1:
        blocks.append({"type": "text", "lines": sections[0]})
        
    # 4. 콜라주 1세트 2장 (img 1, 2)
    if len(image_paths) >= 3:
        blocks.append({"type": "collage_images", "paths": [image_paths[1], image_paths[2]]})
        
    # 5. 섹션 2
    if len(sections) >= 2:
        blocks.append({"type": "text", "lines": sections[1]})
        
    # 6. 콜라주 2세트 2장 (img 3, 4)
    if len(image_paths) >= 5:
        blocks.append({"type": "collage_images", "paths": [image_paths[3], image_paths[4]]})
        
    # 7. 섹션 3
    if len(sections) >= 3:
        blocks.append({"type": "text", "lines": sections[2]})
        
    # 8. 마무리 단독 사진 (img 5) - 총평 텍스트 바로 앞에 배치
    if len(image_paths) >= 6:
        blocks.append({"type": "single_image", "path": image_paths[5]})
    elif len(image_paths) in (2, 4):
        blocks.append({"type": "single_image", "path": image_paths[-1]})
        
    # 9. 섹션 4 및 마무리 총평 (마무리 단독 사진 바로 아래)
    if len(sections) >= 4:
        closing = []
        for s in sections[3:]:
            for l in s:
                if not is_hashtag_line(l):
                    closing.append(l)
            closing.append("")
        if any(l.strip() for l in closing):
            blocks.append({"type": "text", "lines": closing})
        
    return blocks

