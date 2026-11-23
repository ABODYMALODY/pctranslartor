"""Main window: Source / Read areas / Translator / Show on / Memory tabs + live log."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QInputDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import capture, styles
from .area_picker import AreaPickerDialog
from .config import DEFAULT_AREAS, MEMORY_PATH, Config
from .memory import TranslationMemory
from . import tv_link
from .overlay import OverlayWindow, page_rect, render_text_layer
from .pipeline import TranslationWorker

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
MAX_LOG_ROWS = 300


def _png(image) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


class MainWindow(QMainWindow):
    test_finished = Signal(str)
    tv_status = Signal(str, bool)
    tv_paired = Signal(object)  # dict, or an error message (str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Game Translator")
        self.resize(820, 720)
        self.cfg = Config.load()
        self.worker: TranslationWorker | None = None
        self._warned_capture = False
        self.overlay = OverlayWindow(self.cfg.areas, self.cfg.overlay_font_px)
        self.test_finished.connect(self._on_test_finished)
        self.tv_status.connect(self._on_tv_status)
        self.tv_paired.connect(self._on_tv_paired)
        self.tv_online = False
        self.tv = tv_link.TvLink(lambda text, online: self.tv_status.emit(text, online))

        # top bar
        self.start_button = QPushButton("▶  Start")
        self.start_button.setMinimumHeight(36)
        self.start_button.clicked.connect(self.toggle_running)
        self.status_label = QLabel("Stopped")
        top = QHBoxLayout()
        top.addWidget(self.start_button)
        top.addWidget(self.status_label, 1)

        # tabs
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_source_tab(), "Source")
        self.tabs.addTab(self._build_areas_tab(), "Read areas")
        self.tabs.addTab(self._build_translator_tab(), "Translator")
        self.tabs.addTab(self._build_show_on_tab(), "Show on")
        self.tabs.addTab(self._build_memory_tab(), "Memory")

        # live log
        self.log_list = QListWidget()
        self.log_list.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addLayout(top)
        layout.addWidget(self.tabs, 3)
        layout.addWidget(QLabel("Live translations"))
        layout.addWidget(self.log_list, 2)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        self.refresh_windows()
        self.refresh_memory()
        if self.cfg.tv.get("token"):
            self.tv.start(self.cfg.tv)

    # ================================================================ tabs

    def _build_source_tab(self) -> QWidget:
        self.window_combo = QComboBox()
        self.window_combo.setEditable(True)
        refresh = QPushButton("⟳")
        refresh.setFixedWidth(40)
        refresh.clicked.connect(self.refresh_windows)
        row = QHBoxLayout()
        row.addWidget(self.window_combo, 1)
        row.addWidget(refresh)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("Window to read the game picture from:"))
        layout.addLayout(row)
        layout.addWidget(QLabel(
            "Tip: in PS Remote Play set Video Quality to 1080p and High frame rate.\n"
            "Keep the window open and big. Do not minimize it."
        ))
        layout.addStretch()
        return tab

    def _build_areas_tab(self) -> QWidget:
        self.areas_label = QLabel()
        self._update_areas_label()
        draw = QPushButton("Take snapshot and draw areas")
        draw.clicked.connect(self.draw_areas)
        reset = QPushButton("Reset to GTA VI defaults")
        reset.clicked.connect(self.reset_areas)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("Only these parts of the picture are read. Smaller areas = faster and fewer mistakes."))
        layout.addWidget(self.areas_label)
        layout.addWidget(draw)
        layout.addWidget(reset)
        layout.addStretch()
        return tab

    def _build_translator_tab(self) -> QWidget:
        self.api_key_edit = QLineEdit(self.cfg.api_key)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-ant-…  (empty = ANTHROPIC_API_KEY environment variable)")

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems(MODELS)
        self.model_combo.setCurrentText(self.cfg.model)

        self.language_edit = QLineEdit(self.cfg.target_language)

        self.page_model_combo = QComboBox()
        self.page_model_combo.setEditable(True)
        self.page_model_combo.addItems(MODELS)
        self.page_model_combo.setCurrentText(self.cfg.page_model)

        self.names_check = QCheckBox("Write names in Arabic letters (Lucia → لوسيا)")
        self.names_check.setChecked(self.cfg.transliterate_names)

        self.vision_check = QCheckBox("Send the picture to Claude when OCR is unsure")
        self.vision_check.setChecked(self.cfg.vision_fallback)
        self.vision_threshold = QDoubleSpinBox()
        self.vision_threshold.setRange(0.0, 1.0)
        self.vision_threshold.setSingleStep(0.05)
        self.vision_threshold.setValue(self.cfg.vision_confidence_threshold)

        self.min_conf = QDoubleSpinBox()
        self.min_conf.setRange(0.0, 1.0)
        self.min_conf.setSingleStep(0.05)
        self.min_conf.setValue(self.cfg.min_ocr_confidence)

        test = QPushButton("Test translation")
        test.clicked.connect(self.test_translation)
        self.test_label = QLabel("")
        self.test_label.setWordWrap(True)

        tab = QWidget()
        form = QFormLayout(tab)
        form.addRow("Claude API key", self.api_key_edit)
        form.addRow("Model for subtitles", self.model_combo)
        form.addRow("Model for phone and menus", self.page_model_combo)
        form.addRow("Translate to", self.language_edit)
        form.addRow("", self.names_check)
        form.addRow("", self.vision_check)
        form.addRow("…when OCR confidence below", self.vision_threshold)
        form.addRow("Ignore OCR words below", self.min_conf)
        form.addRow(test, self.test_label)
        return tab

    def _build_show_on_tab(self) -> QWidget:
        self.overlay_check = QCheckBox("Overlay on this PC (drawn over the game window)")
        self.overlay_check.setChecked(self.cfg.overlay_enabled)
        self.font_spin = QSpinBox()
        self.font_spin.setRange(14, 80)
        self.font_spin.setValue(self.cfg.overlay_font_px)
        self.font_spin.valueChanged.connect(self._on_font_changed)

        phone = self.cfg.pages.get("phone", {})
        self.mirror_check = QCheckBox("Flip the phone right-to-left, like an Arabic app")
        self.mirror_check.setChecked(bool(phone.get("mirror", True)))
        self.placement_combo = QComboBox()
        self.placement_combo.addItems(["over", "beside"])
        self.placement_combo.setCurrentText(phone.get("placement", "over"))
        self.menu_check = QCheckBox("Translate menus, options and button prompts (whole screen)")
        self.menu_check.setChecked("menu" in self.cfg.areas)

        tv_box = QGroupBox("Show on a TV")
        tv_layout = QVBoxLayout(tv_box)
        tv_layout.addWidget(QLabel(
            "Translations over the console picture on an Android TV / Google TV with the Game Translator app.\n"
            "Open the app on the TV, then connect it here with the 6-digit code it shows."
        ))
        self.tv_label = QLabel(self._tv_text())
        connect = QPushButton("Connect a TV")
        connect.clicked.connect(self.connect_tv)
        test_tv = QPushButton("Send a test")
        test_tv.clicked.connect(self.test_tv)
        remove = QPushButton("Remove")
        remove.clicked.connect(self.remove_tv)
        row = QHBoxLayout()
        row.addWidget(connect)
        row.addWidget(test_tv)
        row.addWidget(remove)
        tv_layout.addWidget(self.tv_label)
        tv_layout.addLayout(row)

        tab = QWidget()
        form = QFormLayout(tab)
        form.addRow(self.overlay_check)
        form.addRow("Text size (at 1080p)", self.font_spin)
        form.addRow(self.mirror_check)
        form.addRow("Arabic phone goes", self.placement_combo)
        form.addRow(self.menu_check)
        form.addRow(tv_box)
        return tab

    def _build_memory_tab(self) -> QWidget:
        self.memory_label = QLabel()
        self.memory_table = QTableWidget(0, 3)
        self.memory_table.setHorizontalHeaderLabels(["English", "Translation", "Reused"])
        self.memory_table.horizontalHeader().setStretchLastSection(False)
        self.memory_table.setColumnWidth(0, 330)
        self.memory_table.setColumnWidth(1, 330)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_memory)
        export = QPushButton("Export CSV…")
        export.clicked.connect(self.export_memory)
        buttons = QHBoxLayout()
        buttons.addWidget(self.memory_label, 1)
        buttons.addWidget(refresh)
        buttons.addWidget(export)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addLayout(buttons)
        layout.addWidget(self.memory_table)
        return tab

    # ================================================================ actions

    def refresh_windows(self) -> None:
        current = self.window_combo.currentText() or self.cfg.window_title
        self.window_combo.clear()
        self.window_combo.addItems(capture.list_windows())
        self.window_combo.setCurrentText(current)

    def draw_areas(self) -> None:
        hwnd = capture.find_window(self.window_combo.currentText())
        if not hwnd:
            QMessageBox.warning(self, "Read areas", "Window not found. Open it and pick it in the Source tab.")
            return
        capturer = capture.WindowCapturer()
        try:
            grabbed = capturer.grab(hwnd)
        finally:
            capturer.close()
        if grabbed is None:
            QMessageBox.warning(self, "Read areas", "The window is minimized or too small.")
            return
        dialog = AreaPickerDialog(grabbed[0], self.cfg.areas, self)
        if dialog.exec():
            self._apply_areas(dialog.areas())

    def reset_areas(self) -> None:
        self._apply_areas({k: list(v) for k, v in DEFAULT_AREAS.items()})

    def _apply_areas(self, areas: dict[str, list[float]]) -> None:
        self.cfg.areas = areas
        self.overlay.areas = {k: list(v) for k, v in areas.items()}
        self.overlay.update()
        self._update_areas_label()
        self.cfg.save()
        if self.worker:
            self.add_log("Areas changed — press Stop and Start to use them.")

    def _update_areas_label(self) -> None:
        lines = [f"{name}: {[round(v, 3) for v in rel]}" for name, rel in self.cfg.areas.items()]
        self.areas_label.setText("\n".join(lines))

    def _on_font_changed(self, value: int) -> None:
        self.overlay.font_px = value
        self.overlay.update()

    def _collect_settings(self) -> None:
        self.cfg.window_title = self.window_combo.currentText().strip()
        self.cfg.api_key = self.api_key_edit.text().strip()
        self.cfg.model = self.model_combo.currentText().strip()
        self.cfg.target_language = self.language_edit.text().strip() or "Arabic"
        self.cfg.vision_fallback = self.vision_check.isChecked()
        self.cfg.vision_confidence_threshold = self.vision_threshold.value()
        self.cfg.min_ocr_confidence = self.min_conf.value()
        self.cfg.overlay_enabled = self.overlay_check.isChecked()
        self.cfg.overlay_font_px = self.font_spin.value()
        self.cfg.page_model = self.page_model_combo.currentText().strip()
        self.cfg.transliterate_names = self.names_check.isChecked()
        if "phone" in self.cfg.pages:
            self.cfg.pages["phone"]["mirror"] = self.mirror_check.isChecked()
            self.cfg.pages["phone"]["placement"] = self.placement_combo.currentText()
        if self.menu_check.isChecked():
            self.cfg.areas.setdefault("menu", list(DEFAULT_AREAS["menu"]))
        else:
            self.cfg.areas.pop("menu", None)
        self.cfg.save()

    def _has_api_key(self) -> bool:
        return bool(self.cfg.api_key or os.environ.get("ANTHROPIC_API_KEY"))

    # ---------------------------------------------------------------- start / stop

    def toggle_running(self) -> None:
        if self.worker:
            self.stop_worker()
        else:
            self.start_worker()

    def start_worker(self) -> None:
        self._collect_settings()
        if not self._has_api_key():
            QMessageBox.warning(self, "Translator", "Add your Claude API key in the Translator tab.")
            self.tabs.setCurrentIndex(2)
            return
        self.overlay.enabled = self.cfg.overlay_enabled
        self.overlay.areas = {k: list(v) for k, v in self.cfg.areas.items()}
        self.overlay.font_px = self.cfg.overlay_font_px
        self.overlay.page_placement = {k: v.get("placement", "over") for k, v in self.cfg.pages.items()}

        worker = TranslationWorker(self.cfg)
        worker.status_changed.connect(self.status_label.setText)
        worker.log_message.connect(self.add_log)
        worker.translation_ready.connect(self._on_translation)
        worker.area_cleared.connect(self._on_cleared)
        worker.page_ready.connect(self._on_page)
        worker.page_cleared.connect(self._on_cleared)
        worker.window_geometry.connect(self.overlay.set_target)
        worker.finished.connect(self._on_worker_finished)
        self.worker = worker
        worker.start()
        self.start_button.setText("■  Stop")

    def stop_worker(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker.wait(3000)

    def _on_worker_finished(self) -> None:
        self.worker = None
        self.overlay.clear_all()
        if self.tv_online:
            self.tv.clear_all()
        self.overlay.hide()
        self.start_button.setText("▶  Start")
        self.refresh_memory()

    def _on_translation(self, area: str, english: str, translated: str, info: str, looks: dict) -> None:
        self.overlay.set_text(area, translated, looks)
        if self.tv_online and area in self.cfg.areas:
            image, place = render_text_layer(area, tuple(self.cfg.areas[area]), translated, looks, self.cfg.overlay_font_px)
            self.tv.send_group(area, [(place, _png(image))])
        self.add_log(f"[{time.strftime('%H:%M:%S')}] {area} · {info}\n{english}\n{styles.strip_tags(translated)}")
        if self.overlay.isVisible() and not self.overlay.excluded_from_capture and not self._warned_capture:
            self._warned_capture = True
            self.add_log("Warning: Windows could not hide the overlay from capture (needs Windows 10 2004+).")

    def _on_page(self, area: str, image, patches) -> None:
        self.overlay.set_page(area, image, patches)
        if not self.tv_online or area not in self.cfg.areas:
            return
        if patches:
            layers = [((x1, y1, x2 - x1, y2 - y1), _png(piece)) for (x1, y1, x2, y2), piece in patches]
        else:
            placement = self.cfg.pages.get(area, {}).get("placement", "over")
            x1, y1, x2, y2 = page_rect(tuple(self.cfg.areas[area]), placement)
            layers = [((x1, y1, x2 - x1, y2 - y1), _png(image))]
        self.tv.send_group(area, layers)

    def _on_cleared(self, area: str) -> None:
        self.overlay.clear(area)
        if self.tv_online:
            self.tv.clear(area)

    # ---------------------------------------------------------------- TV

    def _tv_text(self) -> str:
        tv = self.cfg.tv
        return f"Connected TVs: {tv.get('name', 'TV')} ({tv.get('host')})" if tv.get("token") else "No TV connected"

    def connect_tv(self) -> None:
        code, ok = QInputDialog.getText(self, "Connect a TV", "Code shown on the TV (6 digits):")
        code = code.strip()
        if not ok:
            return
        if not (code.isdigit() and len(code) == 6):
            QMessageBox.warning(self, "Connect a TV", "Enter the 6 digits shown on the TV.")
            return
        self.tv_label.setText("Looking for the TV…")

        def job() -> None:
            try:
                self.tv_paired.emit(tv_link.pair(code))
            except Exception as exc:
                self.tv_paired.emit(str(exc))

        threading.Thread(target=job, daemon=True).start()

    def _on_tv_paired(self, result) -> None:
        if isinstance(result, str):
            self.tv_label.setText(self._tv_text())
            QMessageBox.warning(self, "Connect a TV", result)
            return
        self.cfg.tv = result
        self.cfg.save()
        self.tv_label.setText(self._tv_text())
        self.tv.start(result)
        self.add_log(f"TV paired: {result.get('name')} ({result.get('host')})")

    def _on_tv_status(self, text: str, online: bool) -> None:
        self.tv_online = online
        if self.cfg.tv.get("token"):
            self.tv_label.setText(f"{self._tv_text()} · {text}")

    def remove_tv(self) -> None:
        self.tv.stop()
        self.tv_online = False
        self.cfg.tv = {}
        self.cfg.save()
        self.tv_label.setText(self._tv_text())

    def test_tv(self) -> None:
        if not self.tv_online:
            QMessageBox.information(self, "Show on a TV", "No TV is online. Connect one first.")
            return
        image, place = render_text_layer(
            "subtitles", (0.12, 0.76, 0.88, 0.96), "Game Translator متصل — الترجمة تظهر هنا", {}, self.cfg.overlay_font_px
        )
        self.tv.send_group("test", [(place, _png(image))])
        threading.Timer(4.0, lambda: self.tv.clear("test")).start()

    def add_log(self, text: str) -> None:
        self.log_list.insertItem(0, text)
        while self.log_list.count() > MAX_LOG_ROWS:
            self.log_list.takeItem(self.log_list.count() - 1)

    # ---------------------------------------------------------------- test

    def test_translation(self) -> None:
        self._collect_settings()
        if not self._has_api_key():
            self.test_label.setText("No API key.")
            return
        self.test_label.setText("Translating…")
        cfg = self.cfg

        def job() -> None:
            try:
                from .translator import ClaudeTranslator

                started = time.perf_counter()
                translator = ClaudeTranslator(cfg.api_key, cfg.model, cfg.target_language)
                result = translator.translate_text("Lucia: Get in the car, we gotta go before the cops show up!", "subtitles", [])
                ms = (time.perf_counter() - started) * 1000
                self.test_finished.emit(f"{result}   ({ms:.0f} ms)")
            except Exception as exc:
                self.test_finished.emit(f"Error: {exc}")

        threading.Thread(target=job, daemon=True).start()

    def _on_test_finished(self, text: str) -> None:
        self.test_label.setText(text)

    # ---------------------------------------------------------------- memory

    def refresh_memory(self) -> None:
        memory = TranslationMemory(MEMORY_PATH)
        try:
            count = memory.count()
            rows = memory.recent(200)
        finally:
            memory.close()
        self.memory_label.setText(f"{count} saved translations")
        self.memory_table.setRowCount(len(rows))
        for i, (source, target, hits) in enumerate(rows):
            self.memory_table.setItem(i, 0, QTableWidgetItem(source))
            target_item = QTableWidgetItem(styles.strip_tags(target))
            target_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.memory_table.setItem(i, 1, target_item)
            self.memory_table.setItem(i, 2, QTableWidgetItem(str(hits)))

    def export_memory(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export translations", "translations.csv", "CSV (*.csv)")
        if not path:
            return
        memory = TranslationMemory(MEMORY_PATH)
        try:
            count = memory.export_csv(Path(path))
        finally:
            memory.close()
        QMessageBox.information(self, "Export", f"Saved {count} translations.")

    # ---------------------------------------------------------------- close

    def closeEvent(self, event) -> None:
        self._collect_settings()
        self.stop_worker()
        self.tv.stop()
        self.overlay.close()
        super().closeEvent(event)
