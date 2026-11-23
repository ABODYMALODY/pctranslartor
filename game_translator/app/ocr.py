"""Read English text from a cropped area of the game picture.

Backends, in order of preference:
  1. rapidocr (+ onnxruntime)  - deep-learning OCR, good on blurry / compressed video
  2. rapidocr_onnxruntime      - older package name of the same engine
  3. pytesseract               - fallback, weaker on low quality pictures
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .text_utils import letter_count

MAX_SIDE = 2400  # never feed the OCR a picture bigger than this


@dataclass
class OcrLine:
    text: str
    confidence: float
    box: tuple[int, int, int, int]  # x1, y1, x2, y2 in crop pixels
    # the pieces the OCR found on this line: (text, box, confidence) — gaps between them may hold icons
    pieces: list[tuple[str, tuple[int, int, int, int], float]] = field(default_factory=list)


@dataclass
class OcrResult:
    lines: list[OcrLine] = field(default_factory=list)

    @property
    def text(self) -> str:
        # Wrapped subtitles are one sentence, so lines are joined with a space.
        return " ".join(line.text for line in self.lines)

    @property
    def confidence(self) -> float:
        total = sum(len(line.text) for line in self.lines)
        if total == 0:
            return 0.0
        return sum(line.confidence * len(line.text) for line in self.lines) / total


@dataclass
class _Fragment:
    text: str
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float


def preprocess(img: np.ndarray) -> tuple[np.ndarray, float]:
    """Upscale small crops and sharpen them. Helps a lot with Remote Play compression."""
    h, w = img.shape[:2]
    if h < 250:
        scale = 2.5
    elif h < 500:
        scale = 2.0
    elif h < 800:
        scale = 1.5
    else:
        scale = 1.0
    scale = min(scale, MAX_SIDE / max(w, h))
    if abs(scale - 1.0) > 0.05:
        interpolation = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=interpolation)
    else:
        scale = 1.0
    blur = cv2.GaussianBlur(img, (0, 0), 1.2)
    sharp = cv2.addWeighted(img, 1.7, blur, -0.7, 0)
    return sharp, scale


def group_lines(
    fragments: list[_Fragment], scale: float = 1.0, max_gap_factor: float | None = None
) -> list[OcrLine]:
    """Merge word/phrase boxes that sit on the same row into lines, top to bottom.

    max_gap_factor: if set, a row is split where the gap between words is wider than
    this many line-heights (used for menus and phone screens).
    """
    rows: list[list[_Fragment]] = []
    for frag in sorted(fragments, key=lambda f: (f.y1 + f.y2) / 2):
        height = max(frag.y2 - frag.y1, 1)
        for row in rows:
            ry1, ry2 = min(f.y1 for f in row), max(f.y2 for f in row)
            overlap = min(frag.y2, ry2) - max(frag.y1, ry1)
            # same row when they share at least half of the smaller height (descenders like g/y don't matter)
            if overlap >= 0.5 * min(height, ry2 - ry1):
                row.append(frag)
                break
        else:
            rows.append([frag])

    # Optionally split a row where there is a big horizontal gap (menus: two columns, icons, etc.).
    segments: list[list[_Fragment]] = []
    for row in rows:
        row.sort(key=lambda f: f.x1)
        if max_gap_factor is None:
            segments.append(row)
            continue
        current = [row[0]]
        for frag in row[1:]:
            row_h = max(f.y2 - f.y1 for f in current + [frag])
            if frag.x1 - current[-1].x2 > max_gap_factor * row_h:
                segments.append(current)
                current = [frag]
            else:
                current.append(frag)
        segments.append(current)

    lines: list[OcrLine] = []
    for row in segments:
        text = " ".join(f.text.strip() for f in row if f.text.strip())
        if not text:
            continue
        weight = sum(len(f.text) for f in row) or 1
        conf = sum(f.confidence * len(f.text) for f in row) / weight
        box = (
            int(min(f.x1 for f in row) / scale),
            int(min(f.y1 for f in row) / scale),
            int(max(f.x2 for f in row) / scale),
            int(max(f.y2 for f in row) / scale),
        )
        pieces = [
            (f.text.strip(), (int(f.x1 / scale), int(f.y1 / scale), int(f.x2 / scale), int(f.y2 / scale)), f.confidence)
            for f in row if f.text.strip()
        ]
        lines.append(OcrLine(text, conf, box, pieces))
    lines.sort(key=lambda line: line.box[1])
    return lines


def _fragment(box, text, score) -> _Fragment:
    pts = np.asarray(box, dtype=np.float32).reshape(-1, 2)
    return _Fragment(
        str(text),
        float(score),
        float(pts[:, 0].min()),
        float(pts[:, 1].min()),
        float(pts[:, 0].max()),
        float(pts[:, 1].max()),
    )


class OcrEngine:
    def __init__(self, min_confidence: float = 0.45) -> None:
        self.min_confidence = min_confidence
        self.backend = ""
        self._engine = None
        try:
            from rapidocr import RapidOCR

            self._engine = RapidOCR()
            self.backend = "rapidocr"
        except ImportError:
            try:
                from rapidocr_onnxruntime import RapidOCR

                self._engine = RapidOCR()
                self.backend = "rapidocr_onnxruntime"
            except ImportError:
                import pytesseract

                self._engine = pytesseract
                self.backend = "tesseract"

    def read(self, img_bgr: np.ndarray) -> OcrResult:
        """Subtitles / instructions: all rows joined into one sentence."""
        return OcrResult([line for line in self._lines(img_bgr, None, sparse=False) if letter_count(line.text) >= 2])

    def read_blocks(self, img_bgr: np.ndarray) -> list[OcrLine]:
        """Menus / phone screens: every separate piece of text keeps its own box."""
        return [line for line in self._lines(img_bgr, 3.2, sparse=True) if letter_count(line.text) >= 1]

    def _lines(self, img_bgr: np.ndarray, max_gap_factor: float | None, sparse: bool) -> list[OcrLine]:
        prepared, scale = preprocess(img_bgr)
        fragments = [
            f for f in self._run(prepared, sparse)
            if f.confidence >= self.min_confidence and letter_count(f.text) >= 1
        ]
        return group_lines(fragments, scale, max_gap_factor)

    def _run(self, img: np.ndarray, sparse: bool = False) -> list[_Fragment]:
        if self.backend == "rapidocr":
            out = self._engine(img)
            if out is None or getattr(out, "txts", None) is None:
                return []
            return [_fragment(b, t, s) for b, t, s in zip(out.boxes, out.txts, out.scores)]
        if self.backend == "rapidocr_onnxruntime":
            result, _ = self._engine(img)
            if not result:
                return []
            return [_fragment(b, t, s) for b, t, s in result]
        return self._run_tesseract(img, sparse)

    def _run_tesseract(self, img: np.ndarray, sparse: bool) -> list[_Fragment]:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if not sparse:
            return self._tesseract_words(gray, "--psm 6", 0, 0, 1.0)
        # Whole game screens confuse Tesseract: find text-like regions first, read each one alone.
        found = []
        for x1, y1, x2, y2 in _text_regions(gray):
            crop = cv2.resize(gray[y1:y2, x1:x2], None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            found += [
                f for f in self._tesseract_words(crop, "--psm 7", x1, y1, 2.0)
                if f.confidence >= 0.6 and (len(f.text.strip()) > 2 or f.confidence >= 0.9)  # bits of pictures
            ]
        # Regions can overlap: keep one reading per word (the surest).
        kept: list[_Fragment] = []
        for frag in sorted(found, key=lambda f: -f.confidence):
            if all(_overlap(frag, k) < 0.4 for k in kept):
                kept.append(frag)
        return kept

    def _tesseract_words(self, gray: np.ndarray, config: str, ox: int, oy: int, scale: float) -> list[_Fragment]:
        from pytesseract import Output

        data = self._engine.image_to_data(gray, lang="eng", config=config, output_type=Output.DICT)
        fragments = []
        for i, word in enumerate(data["text"]):
            conf = float(data["conf"][i])
            if not word.strip() or conf < 0:
                continue
            x, y, w, h = (v / scale for v in (data["left"][i], data["top"][i], data["width"][i], data["height"][i]))
            fragments.append(_Fragment(word, conf / 100.0, ox + x, oy + y, ox + x + w, oy + y + h))
        return fragments


def _overlap(a: _Fragment, b: _Fragment) -> float:
    ix = max(0.0, min(a.x2, b.x2) - max(a.x1, b.x1))
    iy = max(0.0, min(a.y2, b.y2) - max(a.y1, b.y1))
    smaller = min((a.x2 - a.x1) * (a.y2 - a.y1), (b.x2 - b.x1) * (b.y2 - b.y1)) or 1.0
    return ix * iy / smaller


def _text_regions(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Classic text finder: strong edges joined horizontally into word/line blobs."""
    h, w = gray.shape
    grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    _, bw = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    joined = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, w // 90), 1)))
    contours, _ = cv2.findContours(joined, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in contours:
        x, y, bw_, bh = cv2.boundingRect(c)
        if 8 <= bh <= h // 8 and bw_ >= 1.2 * bh and cv2.countNonZero(bw[y:y + bh, x:x + bw_]) > 0.15 * bw_ * bh:
            pad = max(3, bh // 4)
            boxes.append((max(0, x - pad), max(0, y - pad), min(w, x + bw_ + pad), min(h, y + bh + pad)))
    return boxes
