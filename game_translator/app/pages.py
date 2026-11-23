"""Game pages: in-game phone, menus, map, pause screen — screens full of separate texts.

Steps for one screen:
  1. analyze_page  : OCR every text block, keep its box, colours, boldness and button icons
  2. prepare_canvas: copy the screen (mirrored for a right-to-left phone), erase the English
  3. render_page   : draw the Arabic in the same places, same colours, same weight, icons put back
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from . import styles
from .ocr import OcrEngine, OcrLine
from .text_utils import letter_count, normalize


@dataclass
class PageBlock:
    key: str
    text: str  # plain English
    tagged: str  # English with <cN> colour tags and <iN/> icon tokens
    colors: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    icons: dict[str, np.ndarray] = field(default_factory=dict)
    box: tuple[int, int, int, int] = (0, 0, 0, 0)  # text + icons
    fg: tuple[int, int, int] = (0, 0, 0)
    bg: tuple[int, int, int] = (255, 255, 255)
    bold: bool = False
    flat_bg: bool = True
    font_px: int = 12  # Arabic text size for this block


@dataclass
class Target:
    block: PageBlock
    y1: int
    y2: int
    left: int  # the Arabic may use the space from left ...
    right: int  # ... to right
    align: str  # "right" (mirrored page) or "left" (page kept as it is)


def _stroke_width(img: np.ndarray, box, fg, bg) -> float:
    x1, y1, x2, y2 = box
    region = img[y1:y2, x1:x2]
    if region.size == 0:
        return 0.0
    up = cv2.resize(region, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC).astype(np.int32)
    contrast = np.linalg.norm(np.array(fg) - np.array(bg))
    dist = np.linalg.norm(up - np.array(bg), axis=-1)
    mask = (dist > 0.5 * contrast).astype(np.uint8)
    if not mask.any():
        return 0.0
    dt = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
    return float(2 * dt[mask > 0].mean())


def _merge_across_icons(img: np.ndarray, lines: list[OcrLine]) -> list[OcrLine]:
    """'Press' [X] 'to eat' are one sentence: join pieces of a row that have a button icon between them."""
    lines = sorted(lines, key=lambda l: (l.box[1], l.box[0]))
    merged: list[OcrLine] = []
    for line in lines:
        for i, other in enumerate(merged):
            prev, right = (other, line) if other.box[0] <= line.box[0] else (line, other)
            h = max(prev.box[3] - prev.box[1], right.box[3] - right.box[1], 1)
            overlap = min(prev.box[3], right.box[3]) - max(prev.box[1], right.box[1])
            gap = right.box[0] - prev.box[2]
            if overlap < 0.5 * h or not 0 < gap <= 5 * h:
                continue
            gx1, gy1, gx2, gy2 = prev.box[2], min(prev.box[1], right.box[1]), right.box[0], max(prev.box[3], right.box[3])
            ring = np.concatenate([img[max(0, gy1 - 3), gx1:gx2], img[min(img.shape[0] - 1, gy2 + 3), gx1:gx2]])
            if ring.size == 0:
                continue
            bg = np.median(ring, axis=0)
            if styles._icon_in(img, (gx1 + 1, gy1 - int(0.45 * h), gx2 - 1, gy2 + int(0.45 * h)), bg, h):
                box = (prev.box[0], min(prev.box[1], right.box[1]), right.box[2], max(prev.box[3], right.box[3]))
                conf = (prev.confidence * len(prev.text) + right.confidence * len(right.text)) / max(len(prev.text) + len(right.text), 1)
                merged[i] = OcrLine(f"{prev.text} {right.text}", conf, box, prev.pieces + right.pieces)
                break
        else:
            merged.append(line)
    return merged


def analyze_page(img: np.ndarray, ocr: OcrEngine) -> list[PageBlock]:
    lines = _merge_across_icons(img, ocr.read_blocks(img))
    # Drop icon noise: very short readings the OCR is not sure about.
    lines = [
        l for l in lines
        if (l.confidence >= 0.6 and letter_count(l.text) >= 3)
        or (l.confidence >= 0.9 and (l.box[2] - l.box[0]) > 1.2 * (l.box[3] - l.box[1]))
    ]
    if not lines:
        return []
    heights = sorted(l.box[3] - l.box[1] for l in lines)
    median_h = heights[len(heights) // 2]
    lines = [
        l for l in lines
        if (l.box[3] - l.box[1]) <= (2.5 if l.confidence >= 0.9 else 1.8) * median_h
    ]
    if not lines:
        return []

    found = []
    for line in lines:
        style = styles.analyze(img, line)
        found.append((line, style, _stroke_width(img, line.box, style.fg, style.bg)))

    # Stroke thickness against the page's usual letter height: >= ~10% looks bold, ~9% looks regular.
    heights = sorted(l.box[3] - l.box[1] for l in lines)
    median_h = max(heights[len(heights) // 2], 1)

    blocks = []
    for line, style, stroke in found:
        height = line.box[3] - line.box[1]
        blocks.append(PageBlock(
            key=normalize(line.text),
            text=line.text,
            tagged=style.tagged_text,
            colors=style.colors,
            icons=style.icons,
            box=style.full_box,
            fg=style.fg,
            bg=style.bg,
            bold=(stroke / 3) / median_h >= 0.098,  # stroke was measured on a 3x picture
            flat_bg=style.flat_bg,
            # OCR boxes hug the letters (no descenders), so Arabic needs ~1.3x the box height.
            font_px=round(1.3 * min(max(height, 0.85 * median_h), 1.15 * median_h)),
        ))
    return blocks


def prepare_canvas(img: np.ndarray, blocks: list[PageBlock], mirror: bool) -> tuple[np.ndarray, list[Target]]:
    """Copy the screen, mirror it if asked, and erase every English text (and its icons)."""
    h, w = img.shape[:2]
    canvas = cv2.flip(img, 1) if mirror else img.copy()
    targets = []
    for block in blocks:
        x1, y1, x2, y2 = block.box
        if mirror:
            x1, x2 = w - x2, w - x1
        pad = 2
        ex1, ey1, ex2, ey2 = max(0, x1 - pad), max(0, y1 - pad), min(w, x2 + pad), min(h, y2 + pad)
        if block.flat_bg:
            canvas[ey1:ey2, ex1:ex2] = block.bg  # flat panel: just paint the background colour
        else:
            # text over the game scene: rebuild what was behind the letters
            region = canvas[ey1:ey2, ex1:ex2]
            mask = (np.linalg.norm(region.astype(np.int32) - np.array(block.bg), axis=-1) > 40).astype(np.uint8) * 255
            mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=2)
            canvas[ey1:ey2, ex1:ex2] = cv2.inpaint(region, mask, 3, cv2.INPAINT_TELEA)
        grow = int(0.3 * (x2 - x1))
        if mirror:  # Arabic app: text starts at the right and may grow to the left
            targets.append(Target(block, y1, y2, max(0, x1 - grow), x2, "right"))
        else:  # same layout as the game: keep the left edge so menu columns stay lined up
            targets.append(Target(block, y1, y2, x1, min(w, x2 + grow), "left"))
    return canvas, targets


def render_page(canvas_bgr: np.ndarray, targets: list[Target], translations: dict[str, str]):
    """Draw the translations with Qt (needs a QApplication). Returns a QImage."""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QFont, QFontMetricsF, QImage, QPainter

    rgb = cv2.cvtColor(canvas_bgr, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    image = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    for target in targets:
        block = target.block
        text = styles.ensure_icons(translations.get(block.key) or block.tagged, block.icons.keys())
        max_w = target.right - target.left
        font = QFont("Segoe UI")
        font.setBold(block.bold)
        font.setPixelSize(max(8, block.font_px))

        icon_w = sum(img.shape[1] for img in block.icons.values())
        metrics = QFontMetricsF(font)
        plain = styles.strip_tags(text)
        if metrics.horizontalAdvance(plain) + icon_w > max_w:  # too long: shrink a little, then cut with …
            font.setPixelSize(max(8, round(font.pixelSize() * 0.88)))
            metrics = QFontMetricsF(font)
        if metrics.horizontalAdvance(plain) + icon_w > max_w:
            words = styles.strip_colors(text).split()
            while words and metrics.horizontalAdvance(styles.strip_tags(" ".join(words)) + "…") + icon_w > max_w:
                words.pop()
            text = " ".join(words) + "…"

        doc = _document(text, block, font, max_w, target.align, block.fg)
        top = target.y1 + ((target.y2 - target.y1) - doc.size().height()) / 2
        painter.save()
        painter.translate(target.left, top)
        if not block.flat_bg:  # over the game scene: a soft dark shadow keeps it readable
            shadow = _document(text, block, font, max_w, target.align, (0, 0, 0), with_icons=False)
            painter.translate(1, 1)
            shadow.drawContents(painter, QRectF(0, 0, max_w, doc.size().height()))
            painter.translate(-1, -1)
        doc.drawContents(painter, QRectF(0, 0, max_w, doc.size().height()))
        painter.restore()

    painter.end()
    return image


def _document(text: str, block: PageBlock, font, width: float, align: str, color_bgr, with_icons: bool = True):
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtGui import QImage, QTextDocument, QTextOption

    doc = QTextDocument()
    doc.setDefaultFont(font)
    doc.setDocumentMargin(0)
    option = QTextOption(Qt.AlignmentFlag.AlignRight if align == "right" else Qt.AlignmentFlag.AlignLeft)
    option.setTextDirection(Qt.LayoutDirection.RightToLeft)
    option.setWrapMode(QTextOption.WrapMode.NoWrap)
    doc.setDefaultTextOption(option)
    for token, icon in block.icons.items():
        rgb = np.ascontiguousarray(cv2.cvtColor(icon, cv2.COLOR_BGR2RGB))
        ih, iw = rgb.shape[:2]
        if with_icons:
            qimg = QImage(rgb.data, iw, ih, 3 * iw, QImage.Format.Format_RGB888).copy()
        else:  # same size, invisible: the shadow keeps exactly the same layout
            qimg = QImage(iw, ih, QImage.Format.Format_ARGB32)
            qimg.fill(Qt.GlobalColor.transparent)
        doc.addResource(QTextDocument.ResourceType.ImageResource, QUrl(f"icon:{token}"), qimg)
    colors = block.colors if color_bgr == block.fg else {k: color_bgr for k in block.colors}
    doc.setHtml(styles.to_html(text, colors, color_bgr, max(8, font.pixelSize()), block.icons.keys()))
    doc.setTextWidth(width)
    return doc
