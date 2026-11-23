"""Transparent, click-through window drawn on top of the game window,
plus the same drawing as pictures for the TV (render_text_layer, page_rect)."""
from __future__ import annotations

import cv2
import numpy as np
from PySide6.QtCore import QRect, QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QTextDocument, QTextOption
from PySide6.QtWidgets import QWidget

from . import styles
from .capture import exclude_from_capture

PADDING = 14  # at 1080p
PAGE_GAP = 0.012  # gap between the original phone and the Arabic one, as a share of the width
WHITE = (255, 255, 255)
Rel = tuple[float, float, float, float]  # x1, y1, x2, y2 as fractions of the game picture


# ====================================================================== shared drawing

def page_rect(rel: Rel, placement: str) -> Rel:
    """Where a page (phone) is drawn: over the original, or beside it (left, else right)."""
    x1, y1, x2, y2 = rel
    if placement != "beside":
        return rel
    w = x2 - x1
    left = x1 - PAGE_GAP - w
    if left < 0:
        left = x2 + PAGE_GAP
    return left, y1, left + w, y2


def text_box(area: str, rel: Rel, text: str, looks: dict, font_px: int,
             canvas_w: float, canvas_h: float) -> tuple[QTextDocument, QRectF, float]:
    """Lay out a translation box for a canvas of this size. Returns (document, box, padding)."""
    scale = canvas_h / 1080
    pad = PADDING * scale
    ax1, ay1, ax2, ay2 = rel[0] * canvas_w, rel[1] * canvas_h, rel[2] * canvas_w, rel[3] * canvas_h

    font = QFont("Segoe UI")
    font.setWeight(QFont.Weight.DemiBold)
    font.setPixelSize(max(12, round(font_px * scale)))

    doc = QTextDocument()
    doc.setDefaultFont(font)
    doc.setDocumentMargin(0)
    option = QTextOption(Qt.AlignmentFlag.AlignHCenter if area == "subtitles" else Qt.AlignmentFlag.AlignRight)
    option.setTextDirection(Qt.LayoutDirection.RightToLeft)
    option.setWrapMode(QTextOption.WrapMode.WordWrap)
    doc.setDefaultTextOption(option)
    icons = looks.get("icons", {})
    for token, picture in icons.items():
        doc.addResource(QTextDocument.ResourceType.ImageResource, QUrl(f"icon:{token}"), _qimage(picture))
    doc.setHtml(styles.to_html(text, looks.get("colors", {}), WHITE, font.pixelSize(), icons.keys()))
    max_w = max(ax2 - ax1, canvas_w * 0.35) - 2 * pad
    doc.setTextWidth(max_w)
    text_w = min(doc.idealWidth(), max_w)
    doc.setTextWidth(text_w)

    box_w, box_h = text_w + 2 * pad, doc.size().height() + 2 * pad
    if area == "subtitles":  # centred, sitting on the bottom of the area
        box = QRectF((ax1 + ax2) / 2 - box_w / 2, ay2 - box_h, box_w, box_h)
    else:  # top-left corner of the area
        box = QRectF(ax1, ay1, box_w, box_h)
    return doc, box, pad


def paint_text_box(painter: QPainter, doc: QTextDocument, box: QRectF, pad: float) -> None:
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 190))
    painter.drawRoundedRect(box, 10 * pad / PADDING, 10 * pad / PADDING)
    painter.save()
    painter.translate(box.left() + pad, box.top() + pad)
    doc.drawContents(painter)
    painter.restore()


def render_text_layer(area: str, rel: Rel, text: str, looks: dict, font_px: int,
                      canvas: tuple[int, int] = (1920, 1080)) -> tuple[QImage, Rel]:
    """The translation box as a transparent picture + its place (x, y, w, h fractions) — for the TV."""
    cw, ch = canvas
    doc, box, pad = text_box(area, rel, text, looks, font_px, cw, ch)
    image = QImage(max(1, int(box.width()) + 2), max(1, int(box.height()) + 2), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    paint_text_box(painter, doc, QRectF(0, 0, box.width(), box.height()), pad)
    painter.end()
    return image, (box.left() / cw, box.top() / ch, image.width() / cw, image.height() / ch)


def _qimage(bgr: np.ndarray) -> QImage:
    rgb = np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


# ====================================================================== PC overlay window

class OverlayWindow(QWidget):
    def __init__(self, areas: dict[str, list[float]], font_px: int) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setWindowTitle("Game Translator Overlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.areas = {k: list(v) for k, v in areas.items()}
        self.font_px = font_px
        self.page_placement: dict[str, str] = {}  # page area -> "beside" | "over"
        self.enabled = True
        self.excluded_from_capture = False
        self._texts: dict[str, tuple[str, dict]] = {}  # area -> (translation with tags, {"colors","icons"})
        self._pages: dict[str, tuple[QImage, list | None]] = {}  # area -> (whole page, patches or None)

    # ---------------------------------------------------------------- public

    def set_target(self, x: int, y: int, w: int, h: int) -> None:
        """Follow the game window. Coordinates are physical pixels from Win32."""
        if w <= 0 or h <= 0 or not self.enabled:
            self.hide()
            return
        dpr = self._device_pixel_ratio(x, y)
        self.setGeometry(QRect(round(x / dpr), round(y / dpr), round(w / dpr), round(h / dpr)))
        if not self.isVisible():
            self.show()

    def set_text(self, area: str, text: str, looks: dict | None = None) -> None:
        self._texts[area] = (text, looks or {"colors": {}, "icons": {}})
        self.update()

    def set_page(self, area: str, image: QImage, patches: list | None = None) -> None:
        self._pages[area] = (image, patches)
        self.update()

    def clear(self, area: str) -> None:
        self._texts.pop(area, None)
        self._pages.pop(area, None)
        self.update()

    def clear_all(self) -> None:
        self._texts.clear()
        self._pages.clear()
        self.update()

    # ---------------------------------------------------------------- Qt

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self.excluded_from_capture:
            self.excluded_from_capture = exclude_from_capture(int(self.winId()))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        for area, (image, patches) in self._pages.items():
            if patches:  # menus: only the pieces that changed, the live game stays visible around them
                for (x1, y1, x2, y2), patch in patches:
                    painter.drawImage(QRectF(x1 * w, y1 * h, (x2 - x1) * w, (y2 - y1) * h), patch)
            elif area in self.areas:
                x1, y1, x2, y2 = page_rect(tuple(self.areas[area]), self.page_placement.get(area, "over"))
                painter.drawImage(QRectF(x1 * w, y1 * h, (x2 - x1) * w, (y2 - y1) * h), image)
        for area, (text, looks) in self._texts.items():
            if area in self.areas and text:
                doc, box, pad = text_box(area, tuple(self.areas[area]), text, looks, self.font_px, w, h)
                paint_text_box(painter, doc, box, pad)
        painter.end()

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _device_pixel_ratio(x: int, y: int) -> float:
        """Find the screen that holds this physical point (handles 125% / 150% Windows scaling)."""
        for screen in QGuiApplication.screens():
            geo = screen.geometry()
            dpr = screen.devicePixelRatio()
            left, top = geo.x(), geo.y()
            if left <= x < left + geo.width() * dpr and top <= y < top + geo.height() * dpr:
                return dpr
        primary = QGuiApplication.primaryScreen()
        return primary.devicePixelRatio() if primary else 1.0
