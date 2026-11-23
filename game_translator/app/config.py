"""Settings that are saved to config.json next to main.py."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Next to GameTranslator.exe when built as a program, else next to main.py.
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_DIR / "config.json"
DATA_DIR = APP_DIR / "data"
MEMORY_PATH = DATA_DIR / "translations.db"

# Read areas as fractions of the game window: [left, top, right, bottom].
# Defaults fit GTA VI: dialogue at the bottom centre, instructions at the top left,
# and the in-game phone on the right.
DEFAULT_AREAS: dict[str, list[float]] = {
    "subtitles": [0.12, 0.76, 0.88, 0.96],
    "instructions": [0.01, 0.02, 0.42, 0.22],
    "phone": [0.565, 0.17, 0.795, 0.86],
    "menu": [0.0, 0.0, 1.0, 1.0],
}
# Areas that are whole screens instead of one line of text, checked in this order.
#   mirror      : flip right-to-left like an Arabic app (phone) or keep the game's layout (menus)
#   placement   : "beside" the original or "over" it (replaces the English in place)
#   min_blocks  : fewer texts than this = the screen is not open
#   idle_check_s: while closed, look for it only this often (saves CPU during gameplay)
DEFAULT_PAGES: dict[str, dict] = {
    "phone": {"mirror": True, "placement": "over", "min_blocks": 3, "idle_check_s": 0.5,
              "hint": "the in-game smartphone: contacts, messages, apps"},
    "menu": {"mirror": False, "placement": "over", "min_blocks": 6, "idle_check_s": 1.0,
             "hint": "a game menu, pause screen, map or shop, with options and controller button prompts"},
}


@dataclass
class Config:
    # Source
    window_title: str = "PS Remote Play"

    # Translator (Claude API)
    api_key: str = ""  # empty = use the ANTHROPIC_API_KEY environment variable
    model: str = "claude-opus-5-5"  # subtitles and instructions
    page_model: str = "claude-opus-5-5"  # phone screens and menus
    target_language: str = "Arabic"
    # When OCR is unsure, send the cropped picture to Claude so it reads the text itself.
    vision_fallback: bool = True
    vision_confidence_threshold: float = 0.80
    transliterate_names: bool = True  # Lucia -> لوسيا

    # Reading
    capture_interval_ms: int = 250
    min_ocr_confidence: float = 0.45
    stable_frames: int = 2  # same text must be seen this many times before translating
    clear_after_empty_frames: int = 6  # hide a translation after this many empty frames

    # Show on
    overlay_enabled: bool = True
    overlay_font_px: int = 34  # text size at a 1080p window; scales with the window

    # Android TV app (filled in by "Connect a TV"): host, port, name, token
    tv: dict = field(default_factory=dict)

    # Game pages (phone, menus) — settings per page, see DEFAULT_PAGES
    pages: dict[str, dict] = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_PAGES.items()})

    areas: dict[str, list[float]] = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_AREAS.items()})

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return cfg
            for key, value in data.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
        for name, defaults in DEFAULT_PAGES.items():  # new page settings appear in old config files
            cfg.pages[name] = {**defaults, **cfg.pages.get(name, {})}
            cfg.areas.setdefault(name, list(DEFAULT_AREAS[name]))
        return cfg

    def save(self) -> None:
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
