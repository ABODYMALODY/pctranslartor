"""Translation memory: every translation is saved, so a line is never paid for twice."""
from __future__ import annotations

import csv
import sqlite3
import threading
import time
from pathlib import Path

from .text_utils import similar


class TranslationMemory:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS translations (
                    key        TEXT NOT NULL,
                    language   TEXT NOT NULL,
                    source     TEXT NOT NULL,
                    target     TEXT NOT NULL,
                    model      TEXT,
                    created_at REAL NOT NULL,
                    hits       INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (key, language)
                );
                CREATE TABLE IF NOT EXISTS history (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       REAL NOT NULL,
                    area     TEXT NOT NULL,
                    source   TEXT NOT NULL,
                    target   TEXT NOT NULL,
                    origin   TEXT NOT NULL
                );
                """
            )
            self._db.commit()

    def get(self, key: str, language: str, fuzzy: bool = True) -> str | None:
        """Exact match first, then a near match (OCR often reads the same line with 1-2 wrong letters).

        fuzzy=False is used for page screens, which ask for many lines per frame and need to be fast.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT key, target FROM translations WHERE key = ? AND language = ?", (key, language)
            ).fetchone()
            if row is None and fuzzy:
                margin = max(3, len(key) // 10)
                candidates = self._db.execute(
                    "SELECT key, target FROM translations WHERE language = ? AND length(key) BETWEEN ? AND ? "
                    "ORDER BY created_at DESC LIMIT 2000",
                    (language, len(key) - margin, len(key) + margin),
                ).fetchall()
                row = next((c for c in candidates if similar(key, c[0], 0.92)), None)
            if row:
                self._db.execute(
                    "UPDATE translations SET hits = hits + 1 WHERE key = ? AND language = ?", (row[0], language)
                )
                self._db.commit()
            return row[1] if row else None

    def put(self, key: str, source: str, target: str, language: str, model: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO translations (key, language, source, target, model, created_at, hits) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (key, language, source, target, model, time.time()),
            )
            self._db.commit()

    def log(self, area: str, source: str, target: str, origin: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO history (ts, area, source, target, origin) VALUES (?, ?, ?, ?, ?)",
                (time.time(), area, source, target, origin),
            )
            self._db.commit()

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM translations").fetchone()[0]

    def recent(self, limit: int = 200) -> list[tuple[str, str, int]]:
        with self._lock:
            return self._db.execute(
                "SELECT source, target, hits FROM translations ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def export_csv(self, path: Path) -> int:
        with self._lock:
            rows = self._db.execute(
                "SELECT source, target, language, model, hits FROM translations ORDER BY created_at"
            ).fetchall()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["english", "translation", "language", "model", "hits"])
            writer.writerows(rows)
        return len(rows)

    def close(self) -> None:
        with self._lock:
            self._db.close()
