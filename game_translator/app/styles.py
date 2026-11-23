"""Read how a piece of text looks, so the Arabic can look the same.

For one OCR line it finds:
  - background and text colour, bold or not, flat background or game scene behind it
  - words in another colour   -> tagged:  "Go to the <c1>safehouse</c1>."
  - controller / key icons    -> tokens:  "Press <i1/> to eat"   (the icon picture is kept)
Claude keeps the tags and tokens in place; the renderer paints the colours and puts the icons back.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import numpy as np

from .ocr import OcrLine

INK_DISTANCE = 60  # how far (in RGB) a pixel must be from the background to count as text
WORD_COLOR_DISTANCE = 90  # how different a word must be from the main text colour to be "coloured"
FLAT_BG_STD = 20  # border pixels this close to the background colour = flat UI panel (phone, menu box)

TAG_RE = re.compile(r"<(c\d+)>(.*?)</\1>", re.DOTALL)
ICON_RE = re.compile(r"<(i\d+)\s*/>")
ANY_MARK_RE = re.compile(r"</?c\d+>|<i\d+\s*/>")

Box = tuple[int, int, int, int]


@dataclass
class TextStyle:
    fg: tuple[int, int, int]  # BGR
    bg: tuple[int, int, int]  # BGR
    bold: bool
    height: int
    flat_bg: bool = True
    tagged_text: str = ""  # text with <cN> colour tags and <iN/> icon tokens
    colors: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    icons: dict[str, np.ndarray] = field(default_factory=dict)  # token -> icon picture (BGR)
    full_box: Box = (0, 0, 0, 0)  # text + icons: the part to erase


def _distance(pixels: np.ndarray, color: np.ndarray) -> np.ndarray:
    return np.linalg.norm(pixels.astype(np.int32) - np.asarray(color).astype(np.int32), axis=-1)


def _ink(pixels: np.ndarray, bg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    dist = _distance(pixels, bg)
    mask = dist > INK_DISTANCE
    return pixels[mask], dist[mask]


def _clip(img: np.ndarray, box: Box) -> Box:
    h, w = img.shape[:2]
    x1, y1, x2, y2 = box
    x1, y1 = max(0, min(w - 1, x1)), max(0, min(h - 1, y1))
    return x1, y1, max(x1 + 1, min(w, x2)), max(y1 + 1, min(h, y2))


def analyze(img: np.ndarray, line: OcrLine) -> TextStyle:
    x1, y1, x2, y2 = _clip(img, line.box)
    h = img.shape[0]
    pad = 3
    region = img[max(0, y1 - pad):min(h, y2 + pad), max(0, x1 - pad):x2 + pad]
    border = np.concatenate([region[0], region[-1], region[:, 0], region[:, -1]])
    bg = np.median(border, axis=0)
    # flat panel if most of the border is the same colour (an icon or a letter touching it doesn't count)
    flat = float(np.percentile(_distance(border, bg), 75)) < FLAT_BG_STD

    inner = img[y1:y2, x1:x2].reshape(-1, 3)
    ink, dist = _ink(inner, bg)
    if len(ink) < 8:
        fg = np.array([20, 20, 20]) if bg.mean() > 128 else np.array([240, 240, 240])
        ink_ratio = 0.0
    else:
        fg = np.median(ink[dist >= np.percentile(dist, 60)], axis=0)  # skip anti-aliased edges
        ink_ratio = len(ink) / max(len(inner), 1)

    style = TextStyle(
        fg=tuple(int(v) for v in fg), bg=tuple(int(v) for v in bg),
        bold=ink_ratio > 0.30, height=y2 - y1, flat_bg=flat,
    )
    items = _find_items(img, line, bg)
    style.tagged_text, style.colors, style.icons, style.full_box = _build_tagged(img, line, items, bg, fg)
    return style


# ---------------------------------------------------------------- icons

def _looks_like_icon(text: str, box: Box, conf: float) -> bool:
    """OCR often 'reads' a button icon as X, ®, (x), @ … — a short square-ish blob."""
    t = text.strip()
    w, h = box[2] - box[0], box[3] - box[1]
    if len(t) > 3 or h <= 0 or not 0.6 <= w / h <= 1.7:
        return False
    return conf < 0.8 or not t.isalpha() or t in {"X", "x", "O", "o"}


def _icon_in(img: np.ndarray, window: Box, bg: np.ndarray, line_h: int, grow: int = 2) -> Box | None:
    """Is there a separate icon-sized blob of ink in this window? Returns its tight box.

    If the blob is cut by the window's left/right side, the window grows so the whole icon is kept.
    """
    box = _icon_in_once(img, window, bg, line_h)
    if box and grow > 0:
        wx1, wy1, wx2, wy2 = _clip(img, window)
        if box[0] <= wx1 or box[2] >= wx2:
            bigger = (wx1 - line_h if box[0] <= wx1 else wx1, wy1, wx2 + line_h if box[2] >= wx2 else wx2, wy2)
            return _icon_in(img, bigger, bg, line_h, grow - 1) or box
    return box


def _icon_in_once(img: np.ndarray, window: Box, bg: np.ndarray, line_h: int) -> Box | None:
    x1, y1, x2, y2 = _clip(img, window)
    if x2 - x1 < 3:
        return None
    patch = img[y1:y2, x1:x2]
    mask = _distance(patch, bg) > INK_DISTANCE
    if mask.mean() < 0.08:
        return None
    ys, xs = np.nonzero(mask)
    bx1, by1, bx2, by2 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    # Something bigger than the line (avatar, picture) touches the window edge: not an icon.
    if by1 == 0 or by2 == patch.shape[0]:
        return None
    bw, bh = bx2 - bx1, by2 - by1
    if not (0.6 * line_h <= bh <= 1.9 * line_h and bw <= 2.2 * line_h):
        return None
    # A button prompt sits on the text line; a row icon (phone list arrow) is centred elsewhere.
    line_center = (window[1] + window[3]) / 2
    if abs((y1 + (by1 + by2) / 2) - line_center) > 0.35 * line_h:
        return None
    return x1 + bx1, y1 + by1, x1 + bx2, y1 + by2


def _find_items(img: np.ndarray, line: OcrLine, bg: np.ndarray) -> list[tuple]:
    """Turn the line into ('text', str, box) and ('icon', box) items, left to right."""
    pieces = line.pieces or [(line.text, line.box, line.confidence)]
    h = max(1, line.box[3] - line.box[1])
    items: list[tuple] = []
    for text, box, conf in pieces:
        if _looks_like_icon(text, box, conf) and len(pieces) > 1:
            items.append(("icon", box))
        else:
            items.append(("text", text, box))

    band_y1, band_y2 = line.box[1] - int(0.45 * h), line.box[3] + int(0.45 * h)
    out: list[tuple] = []
    first_x = items[0][-1][0]
    left = _icon_in(img, (first_x - int(2.4 * h), band_y1, first_x - 1, band_y2), bg, h)
    if left:
        out.append(("icon", left))
    for i, item in enumerate(items):
        out.append(item)
        if i + 1 < len(items):
            gap_x1, gap_x2 = item[-1][2] + 1, items[i + 1][-1][0] - 1
            if gap_x2 - gap_x1 > 0.5 * h and item[0] == "text" and items[i + 1][0] == "text":
                icon = _icon_in(img, (gap_x1, band_y1, gap_x2, band_y2), bg, h)
                if icon:
                    out.append(("icon", icon))
    last_x = items[-1][-1][2]
    right = _icon_in(img, (last_x + 1, band_y1, last_x + int(2.4 * h), band_y2), bg, h)
    if right:
        out.append(("icon", right))
    return out


# ---------------------------------------------------------------- coloured words + tokens

def _word_color(img, box: Box, bg, fg) -> np.ndarray | None:
    x1, y1, x2, y2 = _clip(img, box)
    ink, dist = _ink(img[y1:y2, x1:x2].reshape(-1, 3), bg)
    if len(ink) < 6:
        return None
    color = np.median(ink[dist >= np.percentile(dist, 60)], axis=0)
    colorful = color.max() - color.min() > 60  # a real colour, not grey/white
    far = _distance(color[None], fg)[0] > WORD_COLOR_DISTANCE
    return color if (colorful and far) else None


def _build_tagged(img, line: OcrLine, items, bg, fg):
    words: list[tuple[str, np.ndarray | None]] = []  # (word or icon token, colour)
    icons: dict[str, np.ndarray] = {}
    for item in items:
        if item[0] == "icon":
            token = f"i{len(icons) + 1}"
            x1, y1, x2, y2 = _clip(img, item[1])
            icons[token] = img[y1:y2, x1:x2].copy()
            words.append((f"<{token}/>", None))
            continue
        _, text, (x1, y1, x2, y2) = item
        total, pos = max(len(text), 1), 0
        for word in text.split():
            start = text.index(word, pos)
            pos = start + len(word)
            wx1 = x1 + int((x2 - x1) * start / total)
            wx2 = x1 + int((x2 - x1) * pos / total)
            words.append((word, _word_color(img, (wx1, y1, max(wx2, wx1 + 1), y2), bg, fg)))

    out, colors, i = [], {}, 0
    while i < len(words):
        word, color = words[i]
        if color is None:
            out.append(word)
            i += 1
            continue
        j = i
        while j + 1 < len(words) and words[j + 1][1] is not None and _distance(words[j + 1][1][None], color)[0] < 50:
            j += 1
        tag = f"c{len(colors) + 1}"
        colors[tag] = tuple(int(v) for v in color)
        out.append(f"<{tag}>{' '.join(w for w, _ in words[i:j + 1])}</{tag}>")
        i = j + 1

    boxes = [item[-1] for item in items]
    full = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    return " ".join(out), colors, icons, full


# ---------------------------------------------------------------- helpers for drawing

def split_runs(text: str, colors: dict[str, tuple[int, int, int]], default: tuple[int, int, int]):
    """'a <c1>b</c1> c' -> [('a ', default), ('b', colors['c1']), (' c', default)]. Icon tokens stay in the text."""
    runs, last = [], 0
    for match in TAG_RE.finditer(text):
        if match.start() > last:
            runs.append((text[last:match.start()], default))
        runs.append((match.group(2), colors.get(match.group(1), default)))
        last = match.end()
    if last < len(text):
        runs.append((text[last:], default))
    return [(t, c) for t, c in runs if t]


def to_html(text: str, colors: dict, default_bgr: tuple[int, int, int], icon_px: int, known_icons=()) -> str:
    """Translation -> right-to-left HTML: coloured spans, and <img src="icon:iN"> for icon tokens."""
    parts = []
    for run, (b, g, r) in split_runs(text, colors, default_bgr):
        pieces, last = [], 0
        for m in ICON_RE.finditer(run):
            pieces.append(html.escape(run[last:m.start()]))
            if m.group(1) in known_icons:
                pieces.append(f'<img src="icon:{m.group(1)}" height="{icon_px}" style="vertical-align: middle">')
            last = m.end()
        pieces.append(html.escape(run[last:]))
        parts.append(f'<span style="color:#{r:02x}{g:02x}{b:02x}">{"".join(pieces)}</span>')
    return f'<div dir="rtl">{"".join(parts)}</div>'


def ensure_icons(text: str, tokens) -> str:
    """If the translation lost an icon token, put it back at the end so no button prompt disappears."""
    for token in tokens:
        if f"<{token}/>" not in text.replace(" />", "/>"):
            text = f"{text} <{token}/>"
    return text


def strip_tags(text: str) -> str:
    """Plain text: colour tags removed, icon tokens removed."""
    return re.sub(r"\s{2,}", " ", ANY_MARK_RE.sub("", text)).strip()


def strip_colors(text: str) -> str:
    """Keep icon tokens, drop colour tags (used when a long text has to be cut)."""
    return re.sub(r"</?c\d+>", "", text)
