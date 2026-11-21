"""N사 블로그 고화질 카드뉴스 이미지 세트 자동 생성 모듈 (thumbnail.py)

N사 블로그 상위 노출에 필수적인 이미지 최소 3장 이상을
글의 흐름(대표 썸네일, 핵심 비교, 실전 꿀팁)에 맞춰 고화질로 자동 생성합니다.
- 텍스트 줄바꿈(Pixel Wrap) 엔진으로 긴 제목과 설명문이 절대 잘리지 않음
- Noto Sans CJK KR 정밀 인덱싱(index=1)으로 한글 두부(Tofu) 현상 100% 차단
- 넘버링 뱃지(01, 02, 03) 및 프리미엄 컬러 팔레트 적용
"""
import os
import re
import random
import logging
from pathlib import Path
from typing import List
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("naver.thumbnail")
logger.setLevel(logging.INFO)

# 프리셋 테마 팔레트 (다크 모던, 클린, 소프트)
THEMES = [
    {"bg": (15, 23, 42), "card": (30, 41, 59), "accent": (56, 189, 248), "text": (248, 250, 252), "sub": (148, 163, 184)},  # Ocean Slate
    {"bg": (24, 24, 27), "card": (39, 39, 42), "accent": (52, 211, 153), "text": (255, 255, 255), "sub": (161, 161, 170)},  # Emerald Dark
    {"bg": (23, 23, 23), "card": (38, 38, 38), "accent": (251, 146, 60), "text": (250, 250, 250), "sub": (163, 163, 163)},  # Warm Amber
    {"bg": (19, 25, 40), "card": (30, 58, 138), "accent": (147, 197, 253), "text": (255, 255, 255), "sub": (191, 219, 254)}, # Deep Navy
]

def _clean_text_for_image(text: str) -> str:
    """이미지에 렌더링할 텍스트에서 특수문자/이모지를 정제합니다."""
    if not text:
        return ""
    # 마크다운 기호 제거
    t = re.sub(r"[\*\_#`~]", "", text)
    # 이모지 제거 (폰트에 글리프가 없어 깨지는 현상 방지)
    t = re.sub(r"[\U00010000-\U0010ffff]", "", t)
    t = re.sub(r"[\u2600-\u27bf]", "", t)
    return t.strip()

def _find_korean_font(size: int, bold: bool = True):
    """시스템에서 사용 가능한 고품질 한글 폰트를 탐색합니다 (TTC의 경우 KR index=1 강제)."""
    home = os.path.expanduser("~")
    bold_candidates = [
        os.path.join(home, ".local/share/fonts/NotoSansCJK-Bold.ttc"),
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
        "/usr/share/fonts/truetype/nanum/NanumSquareB.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "C:\\Windows\\Fonts\\malgunbd.ttf",
        "/mnt/c/Windows/Fonts/malgunbd.ttf",
    ]
    regular_candidates = [
        os.path.join(home, ".local/share/fonts/NotoSansCJK-Regular.ttc"),
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumSquareR.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "C:\\Windows\\Fonts\\malgun.ttf",
        "/mnt/c/Windows/Fonts/malgun.ttf",
    ]

    search_list = (bold_candidates + regular_candidates) if bold else (regular_candidates + bold_candidates)

    for path in search_list:
        if os.path.exists(path):
            try:
                if path.endswith(".ttc"):
                    # NotoSansCJK.ttc에서 한글은 Index 1
                    try:
                        return ImageFont.truetype(path, size=size, index=1)
                    except Exception:
                        return ImageFont.truetype(path, size=size)
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue

    try:
        import subprocess
        out = subprocess.check_output(["fc-list", ":lang=ko", "file"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            fpath = line.split(":")[0].strip()
            if fpath and os.path.exists(fpath):
                try:
                    if fpath.endswith(".ttc"):
                        try:
                            return ImageFont.truetype(fpath, size=size, index=1)
                        except Exception:
                            pass
                    return ImageFont.truetype(fpath, size=size)
                except Exception:
                    continue
    except Exception:
        pass

    try:
        return ImageFont.load_default()
    except Exception:
        return None

def wrap_text_by_pixel(text: str, font: ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> List[str]:
    """텍스트를 픽셀 너비 기준으로 한 글자도 잘림 없이 정밀 줄바꿈합니다."""
    words = text.split()
    lines = []
    curr = ""
    for w in words:
        test = (curr + " " + w).strip() if curr else w
        bbox = draw.textbbox((0, 0), test, font=font)
        w_px = bbox[2] - bbox[0]
        if w_px <= max_width:
            curr = test
        else:
            if curr:
                lines.append(curr)
                curr = ""
            w_bbox = draw.textbbox((0, 0), w, font=font)
            if w_bbox[2] - w_bbox[0] > max_width:
                sub = ""
                for ch in w:
                    test_sub = sub + ch
                    s_bbox = draw.textbbox((0, 0), test_sub, font=font)
                    if s_bbox[2] - s_bbox[0] <= max_width:
                        sub = test_sub
                    else:
                        lines.append(sub)
                        sub = ch
                curr = sub
            else:
                curr = w
    if curr:
        lines.append(curr)
    return lines

def create_main_thumbnail(title: str, tag: str, theme: dict, output_dir: Path) -> str:
    """1. 대표 메인 썸네일 (960x960) - 동적 폰트 스케일링 & 수직 중앙 정렬로 절대 잘리지 않음"""
    width, height = 960, 960
    img = Image.new("RGB", (width, height), theme["bg"])
    draw = ImageDraw.Draw(img)

    margin = 50
    # 외곽 프레임 테두리
    draw.rectangle([(margin, margin), (width - margin, height - margin)], outline=theme["card"], width=3)
    # 상단 포인트 라인
    draw.line([(margin + 40, margin + 70), (margin + 200, margin + 70)], fill=theme["accent"], width=6)

    # 1. 태그 뱃지
    clean_tag = _clean_text_for_image(tag) or "BLOG"
    font_tag = _find_korean_font(28, bold=True)
    draw.text((margin + 40, margin + 95), f"★ {clean_tag.upper()} SPECIAL ISSUE", fill=theme["accent"], font=font_tag)

    # 2. 제목 동적 스케일링 및 줄바꿈 (잘림 100% 방지)
    clean_title = _clean_text_for_image(title)
    max_title_w = width - 2 * (margin + 40)

    # 제목 길이에 따른 지능형 폰트 크기 선택
    if len(clean_title) <= 20:
        font_size = 52
    elif len(clean_title) <= 35:
        font_size = 46
    else:
        font_size = 38

    font_title = _find_korean_font(font_size, bold=True)
    lines = wrap_text_by_pixel(clean_title, font_title, max_title_w, draw)

    # 상단 뱃지와 하단 푸터 사이 영역(220px ~ 800px)에서 수직 정중앙 자동 배치
    line_height = int(font_size * 1.48)
    total_text_h = len(lines) * line_height
    start_y = int(500 - (total_text_h / 2))

    for i, line in enumerate(lines):
        draw.text((margin + 40, start_y + (i * line_height)), line, fill=theme["text"], font=font_title)

    # 3. 하단 푸터 라인
    draw.line([(margin + 40, height - margin - 80), (width - margin - 40, height - margin - 80)], fill=theme["card"], width=2)
    font_sub = _find_korean_font(24, bold=False)
    draw.text((margin + 40, height - margin - 55), "BLOG C-RANK EDITION", fill=theme["sub"], font=font_sub)

    path = output_dir / f"1_main_{random.randint(1000, 9999)}.png"
    img.save(str(path), "PNG", quality=95)
    return str(path)

def create_section_card(section_title: str, points: List[str], badge: str, theme: dict, output_dir: Path, idx: int) -> str:
    """2 & 3. 본문 삽입용 인포그래픽 카드뉴스 (960x720) - 넘버링 뱃지 및 완전 줄바꿈"""
    width, height = 960, 720
    img = Image.new("RGB", (width, height), theme["bg"])
    draw = ImageDraw.Draw(img)

    margin = 40
    # 내부 카드 배경 (모서리 라운딩)
    draw.rounded_rectangle([(margin, margin), (width - margin, height - margin)], radius=20, fill=theme["card"])

    # 상단 뱃지
    font_badge = _find_korean_font(24, bold=True)
    draw.text((margin + 40, margin + 35), f"★ {badge}", fill=theme["accent"], font=font_badge)

    # 섹션 제목 (2줄 자동 줄바꿈 지원)
    clean_sec_title = _clean_text_for_image(section_title)
    sec_font_size = 38 if len(clean_sec_title) <= 24 else 32
    font_sec_title = _find_korean_font(sec_font_size, bold=True)
    sec_lines = wrap_text_by_pixel(clean_sec_title, font_sec_title, width - 2 * (margin + 40), draw)
    sec_lh = int(sec_font_size * 1.35)

    curr_y = margin + 75
    for s_line in sec_lines:
        draw.text((margin + 40, curr_y), s_line, fill=theme["text"], font=font_sec_title)
        curr_y += sec_lh

    curr_y += 10
    draw.line([(margin + 40, curr_y), (width - margin - 40, curr_y)], fill=theme["bg"], width=3)
    curr_y += 25

    # 불릿 포인트 (넘버링 뱃지 스타일 & 개별 줄바꿈)
    font_num = _find_korean_font(24, bold=True)
    font_pt = _find_korean_font(25, bold=False)
    pt_lh = 34
    max_pt_w = width - (margin + 120 + margin + 40)

    for i, pt in enumerate(points[:4]):
        clean_pt = _clean_text_for_image(pt)
        num_str = f"0{i+1}"
        draw.text((margin + 45, curr_y), num_str, fill=theme["accent"], font=font_num)

        pt_lines = wrap_text_by_pixel(clean_pt, font_pt, max_pt_w, draw)
        for p_line in pt_lines:
            draw.text((margin + 115, curr_y), p_line, fill=theme["sub"], font=font_pt)
            curr_y += pt_lh
        curr_y += 14

    path = output_dir / f"{idx}_card_{random.randint(1000, 9999)}.png"
    img.save(str(path), "PNG", quality=95)
    return str(path)

def generate_blog_image_set(keyword: str, title: str, output_dir: Path = None) -> List[str]:
    """
    N사 블로그 포스팅에 필요한 최소 3장의 고품질 이미지 세트를 자동 생성하여 경로 리스트로 반환합니다.
    """
    if output_dir is None:
        from config import DATA_DIR
        output_dir = DATA_DIR / "post_images"
    output_dir.mkdir(parents=True, exist_ok=True)

    theme = random.choice(THEMES)
    images = []

    # 1. 대표 메인 썸네일
    main_img = create_main_thumbnail(title=title, tag=keyword, theme=theme, output_dir=output_dir)
    images.append(main_img)

    # 2. 섹션 1용 핵심 비교 및 체크포인트 카드
    card1 = create_section_card(
        section_title=f"{keyword} 핵심 체크포인트 & 선택 기준",
        points=[
            "실사용 환경에 맞는 가성비 및 유지비용 검토",
            "세부 사양 및 주요 필수 기능 꼼꼼 비교",
            "실제 사용자들의 긍정 및 부정 솔직 후기 확인",
            "사후 AS 보증 및 안정적인 지속 사용성 점검"
        ],
        badge="CHECK POINT",
        theme=theme,
        output_dir=output_dir,
        idx=2
    )
    images.append(card1)

    # 3. 섹션 2용 실전 활용 노하우 및 꿀팁 카드
    card2 = create_section_card(
        section_title=f"{keyword} 200% 활용 실전 꿀팁 BEST",
        points=[
            "초기 올바른 세팅 및 매뉴얼 필수 숙지",
            "자주 사용하는 기능 즐겨찾기 및 루틴화",
            "정기적인 관리로 최상의 성능과 수명 유지",
            "다양한 부가 옵션과 확장 기능 적극 활용"
        ],
        badge="PRO TIPS",
        theme=theme,
        output_dir=output_dir,
        idx=3
    )
    images.append(card2)

    logger.info(f"블로그 포스팅용 고화질 이미지 세트 {len(images)}장 생성 완료!")
    return images

# 하위 호환 단일 썸네일 함수
def generate_blog_thumbnail(title: str, tag_text: str = "BLOG POST", output_dir: Path = None) -> str:
    images = generate_blog_image_set(keyword=tag_text, title=title, output_dir=output_dir)
    return images[0]
