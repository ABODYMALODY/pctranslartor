"""Find a window (e.g. PS Remote Play) and grab its picture. Windows only."""
from __future__ import annotations

import ctypes

import mss
import numpy as np
import win32gui

OWN_WINDOW_TITLES = ("Game Translator",)


def list_windows() -> list[str]:
    titles: set[str] = set()

    def callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd).strip()
            if title and not title.startswith(OWN_WINDOW_TITLES):
                titles.add(title)

    win32gui.EnumWindows(callback, None)
    return sorted(titles, key=str.lower)


def find_window(title: str) -> int:
    """Exact title match first, then 'title contains'. Returns 0 if not found."""
    wanted = title.strip().lower()
    if not wanted:
        return 0
    exact, partial = 0, 0

    def callback(hwnd, _):
        nonlocal exact, partial
        if not win32gui.IsWindowVisible(hwnd):
            return
        current = win32gui.GetWindowText(hwnd).strip()
        if current.startswith(OWN_WINDOW_TITLES):
            return
        if current.lower() == wanted and not exact:
            exact = hwnd
        elif wanted in current.lower() and not partial:
            partial = hwnd

    win32gui.EnumWindows(callback, None)
    return exact or partial


def is_valid(hwnd: int) -> bool:
    return bool(hwnd) and bool(win32gui.IsWindow(hwnd))


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Client area (without title bar) in physical screen pixels: x, y, width, height."""
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    x, y = win32gui.ClientToScreen(hwnd, (left, top))
    return x, y, right - left, bottom - top


def exclude_from_capture(hwnd: int) -> bool:
    """Hide our overlay from screen captures so OCR never reads our own text.

    Needs Windows 10 version 2004 or newer.
    """
    WDA_EXCLUDEFROMCAPTURE = 0x11
    try:
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), WDA_EXCLUDEFROMCAPTURE))
    except (AttributeError, OSError):
        return False


class WindowCapturer:
    """Create it in the thread that uses it (mss is not thread-safe)."""

    def __init__(self) -> None:
        self._sct = mss.mss()

    def grab(self, hwnd: int) -> tuple[np.ndarray, tuple[int, int, int, int]] | None:
        """Returns (BGR image, window rect) or None if the window is minimized or too small."""
        if not is_valid(hwnd) or win32gui.IsIconic(hwnd):
            return None
        x, y, w, h = client_rect(hwnd)
        if w < 64 or h < 64:
            return None
        shot = self._sct.grab({"left": x, "top": y, "width": w, "height": h})
        frame = np.ascontiguousarray(np.asarray(shot)[:, :, :3])  # BGRA -> BGR
        return frame, (x, y, w, h)

    def close(self) -> None:
        self._sct.close()


def crop_relative(frame: np.ndarray, rel: list[float]) -> np.ndarray | None:
    h, w = frame.shape[:2]
    x1, y1 = int(max(0.0, rel[0]) * w), int(max(0.0, rel[1]) * h)
    x2, y2 = int(min(1.0, rel[2]) * w), int(min(1.0, rel[3]) * h)
    if x2 - x1 < 16 or y2 - y1 < 16:
        return None
    return frame[y1:y2, x1:x2]
