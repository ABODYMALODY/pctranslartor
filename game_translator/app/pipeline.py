"""The main loop.

Text areas (subtitles, instructions):
    capture -> crop -> OCR -> wait until stable -> memory / Claude -> overlay text
Page areas (phone, menus):
    capture -> crop -> OCR every block -> memory -> redraw the screen in Arabic -> overlay image
    (missing texts of a screen are sent to Claude in ONE call once the screen stops moving)
"""
from __future__ import annotations

import copy
import re
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal

from . import capture, pages, styles
from .config import MEMORY_PATH, Config
from .memory import TranslationMemory
from .ocr import OcrEngine, OcrResult
from .text_utils import normalize, similar
from .translator import ClaudeTranslator

UNCHANGED_PIXEL_DIFF = 1.5  # mean grey-level change below this = same picture, skip OCR
MAX_PAGE_BATCH = 40
RETRY_AFTER_ERROR_S = 5.0


@dataclass
class AreaState:
    thumb: np.ndarray | None = None
    last_ocr: OcrResult | None = None
    candidate: str = ""
    count: int = 0
    best: tuple[OcrResult, np.ndarray] | None = None
    shown: str = ""  # key of the text currently translated / being translated
    empty: int = 0


@dataclass
class PageState:
    thumb: np.ndarray | None = None
    crop: np.ndarray | None = None
    blocks: list[pages.PageBlock] | None = None
    active: bool = False
    cache: dict[str, str] = field(default_factory=dict)  # key -> Arabic (with colour tags)
    pending: list[pages.PageBlock] = field(default_factory=list)  # blocks still in English
    in_flight: bool = False
    dirty: bool = False  # new translations arrived: redraw without OCR
    retry_at: float = 0.0
    next_idle_check: float = 0.0


class TranslationWorker(QThread):
    status_changed = Signal(str)
    log_message = Signal(str)
    translation_ready = Signal(str, str, str, str, object)  # area, english, translation, info, {"colors","icons"}
    area_cleared = Signal(str)
    page_ready = Signal(str, object, object)  # area, QImage, patches [((x1, y1, x2, y2) of the window, QImage)] or None
    page_cleared = Signal(str)
    window_geometry = Signal(int, int, int, int)  # physical pixels; width 0 = hide overlay

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.cfg = copy.deepcopy(config)
        self._stop = threading.Event()
        self._status = ""
        self._context: deque[str] = deque(maxlen=6)
        self._states: dict[str, AreaState] = {}
        self._pages: dict[str, PageState] = {}
        self._looks: dict[str, dict] = {}  # area -> colours and icons of the line being translated
        self._memory: TranslationMemory | None = None
        self._translator: ClaudeTranslator | None = None
        self._pool: ThreadPoolExecutor | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # show the error instead of dying silently
            self.log_message.emit(f"Fatal error: {exc!r}")
            self.log_message.emit(traceback.format_exc())
        finally:
            if self._pool:
                self._pool.shutdown(wait=False, cancel_futures=True)
            if self._memory:
                self._memory.close()
            self.window_geometry.emit(0, 0, 0, 0)
            self._set_status("Stopped")

    # ------------------------------------------------------------------ loop

    def _run(self) -> None:
        cfg = self.cfg
        self._set_status("Starting…")
        capturer = capture.WindowCapturer()
        ocr = OcrEngine(cfg.min_ocr_confidence)
        self.log_message.emit(f"OCR engine: {ocr.backend}")
        self._translator = ClaudeTranslator(cfg.api_key, cfg.model, cfg.target_language, cfg.transliterate_names)
        self._memory = TranslationMemory(MEMORY_PATH)
        self._pool = ThreadPoolExecutor(max_workers=3)
        page_names = [n for n in cfg.pages if n in cfg.areas]
        text_names = [n for n in cfg.areas if n not in cfg.pages]
        self._states = {name: AreaState() for name in text_names}
        self._pages = {name: PageState() for name in page_names}
        self.log_message.emit(f"Translator: {cfg.model} (text), {cfg.page_model} (pages) → {cfg.target_language}")

        hwnd = 0
        last_geometry = None
        interval = cfg.capture_interval_ms / 1000.0

        while not self._stop.is_set():
            started = time.perf_counter()

            if not capture.is_valid(hwnd):
                hwnd = capture.find_window(cfg.window_title)
                if not hwnd:
                    self._set_status(f"Waiting for window “{cfg.window_title}”")
                    if last_geometry is not None:
                        self.window_geometry.emit(0, 0, 0, 0)
                        last_geometry = None
                    self._stop.wait(1.0)
                    continue

            grabbed = capturer.grab(hwnd)
            if grabbed is None:
                self._set_status("Window is minimized — restore it")
                if last_geometry is not None:
                    self.window_geometry.emit(0, 0, 0, 0)
                    last_geometry = None
                self._stop.wait(0.5)
                continue

            frame, geometry = grabbed
            if geometry != last_geometry:
                self.window_geometry.emit(*geometry)
                last_geometry = geometry
            self._set_status(f"Capturing “{cfg.window_title}”")

            # Pages first (phone, then menus): an open page hides its part of the picture
            # from the next pages and from the subtitles, so nothing is read twice.
            page_frame = frame
            for name in page_names:
                crop = capture.crop_relative(page_frame, cfg.areas[name])
                if crop is not None:
                    self._process_page(name, crop, ocr)
                page_frame = self._hide_active_pages(frame, upto=name)
            text_frame = self._hide_active_pages(frame)
            for name in text_names:
                crop = capture.crop_relative(text_frame, cfg.areas[name])
                if crop is not None:
                    self._process_area(name, crop, ocr)

            elapsed = time.perf_counter() - started
            self._stop.wait(max(0.01, interval - elapsed))

        capturer.close()

    def _hide_active_pages(self, frame: np.ndarray, upto: str | None = None) -> np.ndarray:
        names = list(self._pages)
        if upto is not None:
            names = names[:names.index(upto) + 1]
        active = [name for name in names if self._pages[name].active]
        if not active:
            return frame
        frame = frame.copy()
        h, w = frame.shape[:2]
        for name in active:
            x1, y1, x2, y2 = self.cfg.areas[name]
            frame[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)] = 0
        return frame

    @staticmethod
    def _thumb(crop: np.ndarray, size=(128, 32)) -> np.ndarray:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        return cv2.resize(gray, size, interpolation=cv2.INTER_AREA).astype(np.int16)

    # ------------------------------------------------------------------ text areas

    def _process_area(self, name: str, crop: np.ndarray, ocr: OcrEngine) -> None:
        st = self._states[name]
        thumb = self._thumb(crop)
        if st.thumb is not None and st.last_ocr is not None and np.abs(thumb - st.thumb).mean() < UNCHANGED_PIXEL_DIFF:
            result = st.last_ocr  # picture did not change: reuse the last reading
        else:
            result = ocr.read(crop)
            st.last_ocr = result
            st.thumb = thumb
        self._observe(name, st, result, crop)

    def _observe(self, name: str, st: AreaState, result: OcrResult, crop: np.ndarray) -> None:
        cfg = self.cfg
        key = normalize(result.text)

        if len(key) < 3:
            st.candidate, st.count, st.best = "", 0, None
            st.empty += 1
            if st.shown and st.empty >= cfg.clear_after_empty_frames:
                st.shown = ""
                self.area_cleared.emit(name)
            return
        st.empty = 0

        if st.candidate and similar(key, st.candidate):
            st.count += 1
            if st.best is None or result.confidence >= st.best[0].confidence:
                st.best = (result, crop)
        else:
            st.candidate, st.count, st.best = key, 1, (result, crop)

        # Translate exactly once, when the text has been stable for enough frames.
        if st.count != cfg.stable_frames:
            return
        if st.shown and similar(key, st.shown):
            return
        best_result, best_crop = st.best
        st.shown = normalize(best_result.text)
        self._commit(name, st.shown, best_result, best_crop)

    @staticmethod
    def _tag_result(crop: np.ndarray, result: OcrResult) -> tuple[str, dict]:
        """Mark coloured words and button icons of every line, numbering tags across lines."""
        parts, colors, icons = [], {}, {}
        for line in result.lines:
            style = styles.analyze(crop, line)
            mapping = {}
            for tag, color in style.colors.items():
                mapping[tag] = f"c{len(colors) + 1}"
                colors[mapping[tag]] = color
            for token, picture in style.icons.items():
                mapping[token] = f"i{len(icons) + 1}"
                icons[mapping[token]] = picture
            parts.append(re.sub(r"<(/?)([ci]\d+)(\s*/?)>",
                                lambda m: f"<{m.group(1)}{mapping.get(m.group(2), m.group(2))}{m.group(3)}>",
                                style.tagged_text))
        return " ".join(parts), {"colors": colors, "icons": icons}

    def _commit(self, area: str, key: str, result: OcrResult, crop: np.ndarray) -> None:
        cfg = self.cfg
        english = result.text
        tagged, looks = self._tag_result(crop, result)
        self._looks[area] = looks
        cached = self._memory.get(key, cfg.target_language)
        if cached:
            self._emit_translation(area, english, cached, "memory", 0.0)
            return
        use_vision = cfg.vision_fallback and result.confidence < cfg.vision_confidence_threshold
        self._pool.submit(
            self._translate_job, area, key, english, tagged, result.confidence,
            crop.copy() if use_vision else None, list(self._context),
        )

    def _translate_job(self, area, key, english, tagged, confidence, crop, context) -> None:
        cfg = self.cfg
        started = time.perf_counter()
        try:
            if crop is not None:
                translated = self._translator.translate_image(crop, area, tagged, context)
                origin = f"Claude + image (OCR {confidence:.0%})"
            else:
                translated = self._translator.translate_text(tagged, area, context)
                origin = f"Claude (OCR {confidence:.0%})"
        except Exception as exc:
            self.log_message.emit(f"Translation failed: {exc}")
            return
        ms = (time.perf_counter() - started) * 1000
        if translated is None:
            self.log_message.emit(f"[{area}] skipped as noise: {english}")
            return
        self._memory.put(key, english, translated, cfg.target_language, cfg.model)
        if self._states[area].shown != key:
            self.log_message.emit(f"[{area}] arrived late, not shown: {english}")
            return
        self._emit_translation(area, english, translated, origin, ms)

    def _emit_translation(self, area: str, english: str, translated: str, origin: str, ms: float) -> None:
        if area == "subtitles":
            self._context.append(english)
        self._memory.log(area, english, styles.strip_tags(translated), origin)
        info = origin if origin == "memory" else f"{origin} · {ms:.0f} ms"
        looks = self._looks.get(area, {"colors": {}, "icons": {}})
        translated = styles.ensure_icons(translated, looks["icons"].keys())
        self.translation_ready.emit(area, english, translated, info, looks)

    # ------------------------------------------------------------------ pages

    def _process_page(self, name: str, crop: np.ndarray, ocr: OcrEngine) -> None:
        cfg = self.cfg
        page_cfg = cfg.pages[name]
        st = self._pages[name]
        if not st.active and time.time() < st.next_idle_check:
            return  # page closed: during gameplay only look for it now and then
        st.next_idle_check = time.time() + float(page_cfg.get("idle_check_s", 0.5))
        thumb = self._thumb(crop, (64, 128))
        unchanged = st.thumb is not None and np.abs(thumb - st.thumb).mean() < UNCHANGED_PIXEL_DIFF

        if unchanged and not st.dirty:
            # The screen stopped moving: now it is worth paying for the missing texts.
            if st.active and st.pending and not st.in_flight and time.time() >= st.retry_at:
                self._request_page(name, st)
            return

        if not unchanged:
            st.thumb = thumb
            st.crop = crop.copy()
            blocks = pages.analyze_page(crop, ocr)
            if len(blocks) < int(page_cfg.get("min_blocks", 3)):
                st.blocks = None
                if st.active:
                    st.active = False
                    self.page_cleared.emit(name)
                return
            st.blocks = blocks
        if st.blocks is None:
            st.dirty = False
            return

        st.dirty = False
        st.active = True
        translations, missing = {}, []
        for block in st.blocks:
            text = self._from_cache(st, block.key)
            if text is None:
                text = self._memory.get(block.key, cfg.target_language, fuzzy=False)
                if text:
                    st.cache[block.key] = text
            if text:
                translations[block.key] = text
            else:
                missing.append(block)
        st.pending = missing

        mirror = bool(page_cfg.get("mirror", False))
        canvas, targets = pages.prepare_canvas(st.crop, st.blocks, mirror)
        image = pages.render_page(canvas, targets, translations)
        # A mirrored page (phone) is shown whole. A menu keeps the live game around its texts,
        # so only the rectangles around each text are sent.
        patches = None if mirror else self._patches(cfg.areas[name], image, targets)
        self.page_ready.emit(name, image, patches)

    @staticmethod
    def _patches(rel, image, targets) -> list:
        from PySide6.QtCore import QRect

        ax1, ay1, ax2, ay2 = rel
        iw, ih = image.width(), image.height()
        out = []
        for t in targets:
            pad = max(4, (t.y2 - t.y1) // 3)
            x1, y1 = max(0, min(t.left, t.block.box[0]) - pad), max(0, t.y1 - pad)
            x2, y2 = min(iw, max(t.right, t.block.box[2]) + pad), min(ih, t.y2 + pad)
            if x2 <= x1 or y2 <= y1:
                continue
            piece = image.copy(QRect(x1, y1, x2 - x1, y2 - y1))
            out.append(((ax1 + x1 / iw * (ax2 - ax1), ay1 + y1 / ih * (ay2 - ay1),
                         ax1 + x2 / iw * (ax2 - ax1), ay1 + y2 / ih * (ay2 - ay1)), piece))
        return out

    @staticmethod
    def _from_cache(st: PageState, key: str) -> str | None:
        """Exact match, else the same line read with a letter or two different."""
        text = st.cache.get(key)
        if text is not None:
            return text
        margin = max(2, len(key) // 10)
        for cached_key, cached in st.cache.items():
            if abs(len(cached_key) - len(key)) <= margin and similar(key, cached_key, 0.92):
                st.cache[key] = cached
                return cached
        return None

    def _request_page(self, name: str, st: PageState) -> None:
        items: dict[str, pages.PageBlock] = {}
        seen = set()
        for block in st.pending:
            if block.key not in seen and len(items) < MAX_PAGE_BATCH:
                seen.add(block.key)
                items[str(len(items) + 1)] = block
        if not items:
            return
        st.in_flight = True
        self._pool.submit(self._page_job, name, items)

    def _page_job(self, name: str, items: dict[str, pages.PageBlock]) -> None:
        cfg = self.cfg
        st = self._pages[name]
        started = time.perf_counter()
        try:
            result = self._translator.translate_page(
                {i: b.tagged for i, b in items.items()}, cfg.pages[name].get("hint", "a game menu"), cfg.page_model
            )
        except Exception as exc:
            self.log_message.emit(f"[{name}] translation failed: {exc}")
            st.retry_at = time.time() + RETRY_AFTER_ERROR_S
            st.in_flight = False
            return
        for i, block in items.items():
            if i in result:
                st.cache[block.key] = result[i]
                self._memory.put(block.key, block.text, result[i], cfg.target_language, cfg.page_model)
        if len(result) < len(items):
            st.retry_at = time.time() + RETRY_AFTER_ERROR_S
        st.pending = [b for b in st.pending if b.key not in st.cache]
        st.dirty = True
        st.in_flight = False
        ms = (time.perf_counter() - started) * 1000
        self.log_message.emit(f"[{name}] {len(result)} texts translated · {cfg.page_model} · {ms:.0f} ms")

    def _set_status(self, text: str) -> None:
        if text != self._status:
            self._status = text
            self.status_changed.emit(text)
