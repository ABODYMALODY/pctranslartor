"""Dialog to draw the read areas on a snapshot of the game window."""
from __future__ import annotations

import cv2
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

AREA_COLORS = {
    "subtitles": QColor(255, 210, 0),
    "instructions": QColor(0, 220, 255),
    "phone": QColor(255, 90, 200),
    "menu": QColor(120, 255, 120),
}


def bgr_to_qimage(frame: np.ndarray) -> QImage:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


class _Canvas(QWidget):
    def __init__(self, image: QImage, areas: dict[str, list[float]]) -> None:
        super().__init__()
        self.image = image
        self.areas = {k: list(v) for k, v in areas.items()}
        self.active = next(iter(self.areas), "subtitles")
        self._drag_start: QPointF | None = None
        self.setMinimumSize(960, 540)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _image_rect(self) -> QRectF:
        iw, ih = self.image.width(), self.image.height()
        scale = min(self.width() / iw, self.height() / ih)
        w, h = iw * scale, ih * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _to_rel(self, pos: QPointF) -> tuple[float, float]:
        r = self._image_rect()
        x = min(1.0, max(0.0, (pos.x() - r.x()) / r.width()))
        y = min(1.0, max(0.0, (pos.y() - r.y()) / r.height()))
        return x, y

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(20, 20, 28))
        r = self._image_rect()
        p.drawImage(r, self.image)
        for name, rel in self.areas.items():
            color = AREA_COLORS.get(name, QColor(255, 255, 255))
            box = QRectF(
                r.x() + rel[0] * r.width(),
                r.y() + rel[1] * r.height(),
                (rel[2] - rel[0]) * r.width(),
                (rel[3] - rel[1]) * r.height(),
            )
            pen = QPen(color, 3 if name == self.active else 2)
            if name != self.active:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(box)
            p.drawText(box.topLeft() + QPointF(6, 18), name)
        p.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_start is None:
            return
        x1, y1 = self._to_rel(self._drag_start)
        x2, y2 = self._to_rel(event.position())
        self.areas[self.active] = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        self._drag_start = None


class AreaPickerDialog(QDialog):
    def __init__(self, frame: np.ndarray, areas: dict[str, list[float]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Game Translator — Read areas")
        self.canvas = _Canvas(bgr_to_qimage(frame), areas)

        choose = QHBoxLayout()
        choose.addWidget(QLabel("Area to draw:"))
        group = QButtonGroup(self)
        for i, name in enumerate(self.canvas.areas):
            button = QRadioButton(name)
            button.setChecked(i == 0)
            button.toggled.connect(lambda checked, n=name: checked and self._select(n))
            group.addButton(button)
            choose.addWidget(button)
        choose.addStretch()

        hint = QLabel("Drag a box around where the text appears. Leave some margin around it.")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(choose)
        layout.addWidget(hint)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(buttons)
        self.resize(1280, 820)

    def _select(self, name: str) -> None:
        self.canvas.active = name
        self.canvas.update()

    def areas(self) -> dict[str, list[float]]:
        return {k: [round(v, 4) for v in rel] for k, rel in self.canvas.areas.items()}
