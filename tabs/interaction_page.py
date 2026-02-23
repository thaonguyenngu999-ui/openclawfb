"""
FB Manager Pro - Interaction Page
🎯 Nuôi nick tự động: Cuộn Feed + Like + Comment bằng AI
Giám sát real-time với screenshot + log chi tiết

Flow mới (qua API server port 8899):
1. POST /open_browser → mở browser
2. POST /check_fb_status → check LIVE
3. POST /fb_read_feed → đọc feed, cuộn
4. POST /like → like bài
5. POST /fb_comment → comment bằng AI
6. POST /screenshot → chụp giám sát
7. POST /close → đóng browser
"""

import os
import random
import time
import json
import threading
import requests
import re
from typing import List, Dict, Optional, Tuple
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QScrollArea,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QTextEdit, QSpinBox, QCheckBox, QTabWidget, QComboBox, QSplitter,
    QGroupBox, QGridLayout, QLineEdit, QPushButton, QProgressBar,
    QPlainTextEdit
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer, QByteArray
from PySide6.QtGui import QColor, QPixmap, QImage

from config import COLORS
from widgets import CyberButton, CyberInput, CyberCard, CyberCheckBox
from db import get_profiles
from api_service import api

# API server
API_BASE = "http://127.0.0.1:8899"


class InteractionSignals(QObject):
    """Signals cho interaction page"""
    log_message = Signal(str, str)  # message, level
    progress_update = Signal(int, int)  # current, total
    profile_done = Signal(str, dict)  # profile_name, result_dict
    folders_loaded = Signal(list)
    profiles_loaded = Signal(list)
    screenshot_ready = Signal(str)  # screenshot file path or URL
    step_update = Signal(str, str, str)  # profile_name, step_desc, status


class InteractionPage(QWidget):
    """
    Tab Tương tác - Nuôi nick tự động

    Gọi API server (port 8899) thay vì tự làm CDP.
    FeedSkill đã có sẵn: like_post, fb_comment, fb_read_feed.
    Có live screenshot giám sát từng bước.
    """

    def __init__(self, log_func=None):
        QWidget.__init__(self)
        self.log = log_func or print
        self.signal = InteractionSignals()
        self.signal.log_message.connect(self._handle_log)
        self.signal.profile_done.connect(self._on_profile_done)
        self.signal.folders_loaded.connect(self._on_folders_loaded)
        self.signal.profiles_loaded.connect(self._on_profiles_loaded)
        self.signal.screenshot_ready.connect(self._display_screenshot)
        self.signal.step_update.connect(self._on_step_update)

        # Data
        self.folders = []
        self.profiles = []
        self.is_running = False
        self.stop_requested = False
        self._worker_thread = None

        # Stats
        self.total_likes = 0
        self.total_comments = 0
        self.profiles_done = 0
        self.profiles_failed = 0

        self._setup_ui()
        QTimer.singleShot(500, self._load_folders)

    # ══════════════════════════════════════
    #  UI SETUP
    # ══════════════════════════════════════

    def _handle_log(self, msg: str, level: str):
        self.log(msg, level)
        self._append_log(msg, level)

    def _append_log(self, msg: str, level: str = "info"):
        color_map = {
            "info": COLORS['neon_cyan'],
            "success": COLORS['neon_mint'],
            "error": COLORS['neon_pink'],
            "warning": COLORS['neon_yellow'],
        }
        color = color_map.get(level, COLORS['text_secondary'])
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_terminal.append(
            f'<span style="color:{COLORS["text_muted"]}">[{timestamp}]</span> '
            f'<span style="color:{color}">{msg}</span>'
        )

    def _on_profile_done(self, name: str, result: dict):
        self.profiles_done += 1
        likes = result.get('likes', 0)
        comments = result.get('comments_success', 0)
        self.total_likes += likes
        self.total_comments += comments
        self._update_stat(self.stat_done, self.profiles_done)
        self._update_stat(self.stat_likes, self.total_likes)
        self._update_stat(self.stat_comments, self.total_comments)

    def _on_step_update(self, name: str, step: str, status: str):
        """Update current step label"""
        icon = {"ok": "✅", "fail": "❌", "working": "⏳"}.get(status, "❓")
        self.current_step_label.setText(f"{icon} [{name}] {step}")

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # ── Header ──
        header = QHBoxLayout()
        title = QLabel("🎯 NUÔI NICK")
        title.setStyleSheet(f"font-size: 24px; font-weight: bold; color: {COLORS['neon_coral']};")
        header.addWidget(title)

        subtitle = QLabel("Feed + Like + Comment | Giám sát AI")
        subtitle.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px;")
        header.addWidget(subtitle)
        header.addStretch()

        # Stats
        self.stat_profiles = self._create_stat_badge("0", "PROFILES", COLORS['neon_cyan'])
        self.stat_done = self._create_stat_badge("0", "XONG", COLORS['neon_mint'])
        self.stat_likes = self._create_stat_badge("0", "LIKES", COLORS['neon_pink'])
        self.stat_comments = self._create_stat_badge("0", "CMTS", COLORS['neon_purple'])
        header.addWidget(self.stat_profiles)
        header.addWidget(self.stat_done)
        header.addWidget(self.stat_likes)
        header.addWidget(self.stat_comments)

        layout.addLayout(header)

        # ── Main Splitter ──
        splitter = QSplitter(Qt.Horizontal)

        # LEFT: Profile Selection
        left_panel = self._create_profile_panel()
        splitter.addWidget(left_panel)

        # RIGHT: Settings + Monitor + Log
        right_panel = self._create_right_panel()
        splitter.addWidget(right_panel)

        splitter.setSizes([350, 700])
        layout.addWidget(splitter)

    def _create_profile_panel(self) -> QFrame:
        panel = QFrame()
        panel.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['bg_card']};
                border: 1px solid {COLORS['border']};
                border-radius: 12px;
            }}
        """)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(15, 15, 15, 15)

        # Header
        header = QHBoxLayout()
        title = QLabel("📋 Chọn Profiles")
        title.setStyleSheet(f"color: {COLORS['neon_cyan']}; font-size: 14px; font-weight: bold;")
        header.addWidget(title)

        btn_select_all = CyberButton("Chọn tất cả", variant="cyan")
        btn_select_all.clicked.connect(self._select_all_profiles)
        btn_deselect = CyberButton("Bỏ chọn", variant="danger")
        btn_deselect.clicked.connect(self._deselect_all_profiles)
        header.addWidget(btn_select_all)
        header.addWidget(btn_deselect)
        layout.addLayout(header)

        # Folder
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("📁 Thư mục:"))
        self.folder_combo = QComboBox()
        self.folder_combo.addItem("-- Chọn thư mục --")
        self.folder_combo.setStyleSheet(f"""
            QComboBox {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_primary']};
                border: 1px solid {COLORS['border']};
                border-radius: 6px;
                padding: 8px 12px;
                min-width: 200px;
            }}
            QComboBox::drop-down {{ border: none; }}
        """)
        self.folder_combo.currentIndexChanged.connect(self._on_folder_change)
        folder_row.addWidget(self.folder_combo)
        folder_row.addStretch()
        layout.addLayout(folder_row)

        # Filter
        filter_row = QHBoxLayout()
        self.filter_input = CyberInput(placeholder="🔍 Tìm profile...")
        self.filter_input.textChanged.connect(self._filter_profiles)
        filter_row.addWidget(self.filter_input)

        self.filter_status = QComboBox()
        self.filter_status.addItems(["Tất cả", "Đã login", "Chưa login"])
        self.filter_status.setStyleSheet(f"""
            QComboBox {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_primary']};
                border: 1px solid {COLORS['border']};
                border-radius: 6px;
                padding: 6px 12px;
                min-width: 100px;
            }}
        """)
        self.filter_status.currentIndexChanged.connect(self._filter_profiles)
        filter_row.addWidget(self.filter_status)
        layout.addLayout(filter_row)

        # Profile table
        self.profile_table = QTableWidget()
        self.profile_table.setColumnCount(4)
        self.profile_table.setHorizontalHeaderLabels(["✓", "Tên", "UID", "Status"])
        self.profile_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.profile_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.profile_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.profile_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.profile_table.setColumnWidth(0, 40)
        self.profile_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.profile_table.verticalHeader().setVisible(False)
        self.profile_table.setStyleSheet(f"""
            QTableWidget {{
                background: {COLORS['bg_darker']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                gridline-color: {COLORS['border']};
            }}
            QTableWidget::item {{
                padding: 8px;
                color: {COLORS['text_primary']};
            }}
            QHeaderView::section {{
                background: {COLORS['bg_card']};
                color: {COLORS['neon_cyan']};
                padding: 8px;
                border: none;
                font-weight: bold;
            }}
        """)
        layout.addWidget(self.profile_table)

        return panel

    def _create_right_panel(self) -> QFrame:
        panel = QFrame()
        panel.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['bg_card']};
                border: 1px solid {COLORS['border']};
                border-radius: 12px;
            }}
        """)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(15, 15, 15, 15)

        # ── Settings row ──
        settings_row = QHBoxLayout()

        # Scroll settings
        scroll_group = QGroupBox("📜 Cuộn")
        scroll_group.setStyleSheet(self._group_box_style())
        sg = QGridLayout(scroll_group)
        sg.addWidget(QLabel("Số lần:"), 0, 0)
        self.scroll_count = QSpinBox()
        self.scroll_count.setRange(1, 20)
        self.scroll_count.setValue(5)
        self.scroll_count.setStyleSheet(self._spinbox_style())
        sg.addWidget(self.scroll_count, 0, 1)
        settings_row.addWidget(scroll_group)

        # Like settings
        like_group = QGroupBox("👍 Like")
        like_group.setStyleSheet(self._group_box_style())
        lg = QGridLayout(like_group)
        lg.addWidget(QLabel("Số like:"), 0, 0)
        self.max_likes = QSpinBox()
        self.max_likes.setRange(0, 20)
        self.max_likes.setValue(3)
        self.max_likes.setStyleSheet(self._spinbox_style())
        lg.addWidget(self.max_likes, 0, 1)
        settings_row.addWidget(like_group)

        # Comment settings
        cmt_group = QGroupBox("💬 Comment")
        cmt_group.setStyleSheet(self._group_box_style())
        cg = QGridLayout(cmt_group)
        cg.addWidget(QLabel("Số cmt:"), 0, 0)
        self.max_comments = QSpinBox()
        self.max_comments.setRange(0, 10)
        self.max_comments.setValue(2)
        self.max_comments.setStyleSheet(self._spinbox_style())
        cg.addWidget(self.max_comments, 0, 1)
        settings_row.addWidget(cmt_group)

        # Delay settings
        delay_group = QGroupBox("⏱ Delay")
        delay_group.setStyleSheet(self._group_box_style())
        dg = QGridLayout(delay_group)
        dg.addWidget(QLabel("Giữa profiles:"), 0, 0)
        self.profile_delay = QSpinBox()
        self.profile_delay.setRange(3, 60)
        self.profile_delay.setValue(10)
        self.profile_delay.setSuffix("s")
        self.profile_delay.setStyleSheet(self._spinbox_style())
        dg.addWidget(self.profile_delay, 0, 1)
        settings_row.addWidget(delay_group)

        layout.addLayout(settings_row)

        # ── Options row ──
        opts_row = QHBoxLayout()
        self.chk_close_after = QCheckBox("Đóng browser sau khi xong")
        self.chk_close_after.setChecked(True)
        self.chk_close_after.setStyleSheet(f"color: {COLORS['text_primary']};")
        opts_row.addWidget(self.chk_close_after)

        self.chk_skip_not_live = QCheckBox("Bỏ qua profile không LIVE")
        self.chk_skip_not_live.setChecked(True)
        self.chk_skip_not_live.setStyleSheet(f"color: {COLORS['text_primary']};")
        opts_row.addWidget(self.chk_skip_not_live)

        opts_row.addStretch()
        layout.addLayout(opts_row)

        # ── Control Buttons ──
        btn_row = QHBoxLayout()
        self.btn_start = CyberButton("🚀 BẮT ĐẦU NUÔI NICK", variant="success")
        self.btn_start.clicked.connect(self._start_interaction)
        self.btn_stop = CyberButton("⏹️ DỪNG", variant="danger")
        self.btn_stop.clicked.connect(self._stop_interaction)
        self.btn_stop.setEnabled(False)
        self.btn_test_api = CyberButton("🔌 Test API", variant="purple")
        self.btn_test_api.clicked.connect(self._test_api)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addWidget(self.btn_test_api)
        layout.addLayout(btn_row)

        # ── Progress ──
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background: {COLORS['bg_darker']};
                border: 1px solid {COLORS['border']};
                border-radius: 6px;
                height: 20px;
                text-align: center;
                color: {COLORS['text_primary']};
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, x2:1,
                    stop:0 {COLORS['neon_coral']}, stop:1 {COLORS['neon_pink']});
                border-radius: 5px;
            }}
        """)
        layout.addWidget(self.progress_bar)

        # ── Current step indicator ──
        self.current_step_label = QLabel("⏸ Chờ bắt đầu...")
        self.current_step_label.setStyleSheet(
            f"color: {COLORS['neon_yellow']}; font-size: 12px; padding: 5px;")
        layout.addWidget(self.current_step_label)

        # ── Bottom: Screenshot + Log side-by-side ──
        monitor_splitter = QSplitter(Qt.Horizontal)

        # Screenshot preview
        screenshot_frame = QFrame()
        screenshot_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['bg_darker']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
        """)
        sc_layout = QVBoxLayout(screenshot_frame)
        sc_layout.setContentsMargins(8, 8, 8, 8)

        sc_header = QLabel("📸 Live Monitor")
        sc_header.setStyleSheet(
            f"color: {COLORS['neon_pink']}; font-weight: bold; font-size: 12px;")
        sc_layout.addWidget(sc_header)

        self.screenshot_label = QLabel("Chưa có screenshot")
        self.screenshot_label.setAlignment(Qt.AlignCenter)
        self.screenshot_label.setMinimumSize(320, 200)
        self.screenshot_label.setStyleSheet(f"""
            background: {COLORS['bg_darker']};
            color: {COLORS['text_muted']};
            border: 1px dashed {COLORS['border']};
            border-radius: 6px;
            padding: 10px;
        """)
        self.screenshot_label.setScaledContents(False)
        sc_layout.addWidget(self.screenshot_label)

        monitor_splitter.addWidget(screenshot_frame)

        # Log terminal
        log_frame = QFrame()
        log_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['bg_darker']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
        """)
        log_layout = QVBoxLayout(log_frame)
        log_layout.setContentsMargins(8, 8, 8, 8)

        log_header = QLabel("📜 Activity Log")
        log_header.setStyleSheet(
            f"color: {COLORS['neon_yellow']}; font-weight: bold; font-size: 12px;")
        log_layout.addWidget(log_header)

        self.log_terminal = QTextEdit()
        self.log_terminal.setReadOnly(True)
        self.log_terminal.setStyleSheet(f"""
            QTextEdit {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_secondary']};
                border: none;
                font-family: 'Consolas', monospace;
                font-size: 11px;
            }}
        """)
        log_layout.addWidget(self.log_terminal)

        monitor_splitter.addWidget(log_frame)
        monitor_splitter.setSizes([350, 350])
        layout.addWidget(monitor_splitter, stretch=1)

        return panel

    # ══════════════════════════════════════
    #  HELPERS
    # ══════════════════════════════════════

    def _create_stat_badge(self, value: str, label: str, color: str) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['bg_card']};
                border: 1px solid {color};
                border-radius: 8px;
                padding: 5px 15px;
            }}
        """)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(8)

        val_label = QLabel(value)
        val_label.setStyleSheet(f"color: {color}; font-size: 18px; font-weight: bold;")
        val_label.setObjectName("value")
        layout.addWidget(val_label)

        text_label = QLabel(label)
        text_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px;")
        layout.addWidget(text_label)

        return frame

    def _update_stat(self, frame: QFrame, value: int):
        val_label = frame.findChild(QLabel, "value")
        if val_label:
            val_label.setText(str(value))

    def _group_box_style(self) -> str:
        return f"""
            QGroupBox {{
                font-weight: bold;
                color: {COLORS['neon_cyan']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 10px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }}
        """

    def _spinbox_style(self) -> str:
        return f"""
            QSpinBox {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_primary']};
                border: 1px solid {COLORS['border']};
                border-radius: 4px;
                padding: 4px 8px;
                min-width: 60px;
            }}
        """

    # ══════════════════════════════════════
    #  API HELPERS
    # ══════════════════════════════════════

    def _api_post(self, endpoint: str, data: dict = None, timeout: int = 60) -> dict:
        """POST to API server, return JSON."""
        try:
            resp = requests.post(f"{API_BASE}{endpoint}",
                                 json=data or {}, timeout=timeout)
            return resp.json()
        except requests.exceptions.ConnectionError:
            return {"success": False, "error": "API server không chạy! Hãy start openclaw_api.py"}
        except requests.exceptions.Timeout:
            return {"success": False, "error": f"Timeout sau {timeout}s"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _api_get(self, endpoint: str, timeout: int = 10) -> dict:
        """GET from API server."""
        try:
            resp = requests.get(f"{API_BASE}{endpoint}", timeout=timeout)
            return resp.json()
        except Exception:
            return {"success": False, "error": "API không phản hồi"}

    def _take_screenshot(self, uuid: str) -> Optional[str]:
        """Take screenshot qua API, emit signal để hiển thị. Returns filepath."""
        result = self._api_post("/screenshot", {"profile": uuid}, timeout=20)
        if result.get("success"):
            path = result.get("screenshot", "")
            url = result.get("screenshot_url", "")
            if path and os.path.exists(path):
                self.signal.screenshot_ready.emit(path)
                return path
            elif url:
                self.signal.screenshot_ready.emit(url)
                return url
        return None

    def _display_screenshot(self, path_or_url: str):
        """Display screenshot in the preview label."""
        try:
            if path_or_url.startswith("http"):
                resp = requests.get(path_or_url, timeout=10)
                if resp.status_code == 200:
                    pixmap = QPixmap()
                    pixmap.loadFromData(resp.content)
                else:
                    return
            else:
                pixmap = QPixmap(path_or_url)

            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    self.screenshot_label.width() - 10,
                    self.screenshot_label.height() - 10,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                self.screenshot_label.setPixmap(scaled)
        except Exception as e:
            print(f"[InteractionPage] Screenshot display error: {e}")

    # ══════════════════════════════════════
    #  DATA LOADING
    # ══════════════════════════════════════

    def _load_folders(self):
        def worker():
            try:
                folders = api.get_folders()
            except Exception:
                folders = []
            self.signal.folders_loaded.emit(folders)
        threading.Thread(target=worker, daemon=True).start()

    def _on_folders_loaded(self, folders: List[Dict]):
        self.folders = folders or []
        self.folder_combo.clear()
        self.folder_combo.addItem("-- Chọn thư mục --")
        for folder in self.folders:
            name = folder.get('name', 'Unknown')
            self.folder_combo.addItem(f"📁 {name}")
        self.signal.log_message.emit(f"Đã tải {len(self.folders)} thư mục", "info")

    def _on_folder_change(self, index: int):
        if index > 0:
            self._load_profiles()

    def _load_profiles(self):
        folder_idx = self.folder_combo.currentIndex()
        if folder_idx <= 0:
            return
        folder = self.folders[folder_idx - 1]
        folder_id = folder.get('id')
        self.signal.log_message.emit(
            f"Đang tải profiles từ {folder.get('name')}...", "info")

        def worker():
            try:
                profiles = api.get_profiles(folder_id=[folder_id], limit=500)
            except Exception:
                profiles = []
            self.signal.profiles_loaded.emit(profiles)
        threading.Thread(target=worker, daemon=True).start()

    def _on_profiles_loaded(self, profiles: List[Dict]):
        self.profiles = profiles or []
        self._render_profiles(self.profiles)
        self._update_stat(self.stat_profiles, len(self.profiles))
        self.signal.log_message.emit(f"Đã tải {len(self.profiles)} profiles", "success")

    def _render_profiles(self, profiles: List[Dict]):
        self.profile_table.setRowCount(len(profiles))
        for i, profile in enumerate(profiles):
            cb = QCheckBox()
            cb.setStyleSheet("margin-left: 10px;")
            self.profile_table.setCellWidget(i, 0, cb)

            name = profile.get('name', profile.get('profile_name', 'Unknown'))
            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.UserRole, profile.get('uuid'))
            self.profile_table.setItem(i, 1, name_item)

            uid = profile.get('uid', profile.get('facebook_uid', ''))
            uid_item = QTableWidgetItem(str(uid) if uid else '-')
            self.profile_table.setItem(i, 2, uid_item)

            status = "✅" if uid else "❌"
            status_item = QTableWidgetItem(status)
            status_item.setTextAlignment(Qt.AlignCenter)
            self.profile_table.setItem(i, 3, status_item)

    def _filter_profiles(self):
        search_text = self.filter_input.text().lower()
        status_filter = self.filter_status.currentIndex()
        for row in range(self.profile_table.rowCount()):
            name_item = self.profile_table.item(row, 1)
            status_item = self.profile_table.item(row, 3)
            if not name_item:
                continue
            name = name_item.text().lower()
            is_logged = status_item.text() == "✅" if status_item else False
            match_search = search_text in name if search_text else True
            match_status = True
            if status_filter == 1:
                match_status = is_logged
            elif status_filter == 2:
                match_status = not is_logged
            self.profile_table.setRowHidden(row, not (match_search and match_status))

    def _select_all_profiles(self):
        for row in range(self.profile_table.rowCount()):
            if not self.profile_table.isRowHidden(row):
                cb = self.profile_table.cellWidget(row, 0)
                if cb:
                    cb.setChecked(True)

    def _deselect_all_profiles(self):
        for row in range(self.profile_table.rowCount()):
            cb = self.profile_table.cellWidget(row, 0)
            if cb:
                cb.setChecked(False)

    def _get_selected_profiles(self) -> List[Dict]:
        selected = []
        for row in range(self.profile_table.rowCount()):
            cb = self.profile_table.cellWidget(row, 0)
            if cb and cb.isChecked():
                name_item = self.profile_table.item(row, 1)
                if name_item:
                    uuid = name_item.data(Qt.UserRole)
                    name = name_item.text()
                    selected.append({"uuid": uuid, "name": name})
        return selected

    # ══════════════════════════════════════
    #  TEST API
    # ══════════════════════════════════════

    def _test_api(self):
        self.signal.log_message.emit("🔄 Test kết nối API server...", "info")

        def test():
            result = self._api_get("/status")
            if "error" in result and "không" in result.get("error", ""):
                self.signal.log_message.emit(
                    "❌ API server không chạy! Hãy chạy: python openclaw_api.py", "error")
            else:
                n = len(result.get('active_connections', []))
                self.signal.log_message.emit(
                    f"✅ API OK! Active connections: {n}", "success")
        threading.Thread(target=test, daemon=True).start()

    # ══════════════════════════════════════
    #  MAIN WORKER
    # ══════════════════════════════════════

    def _start_interaction(self):
        selected = self._get_selected_profiles()
        if not selected:
            self.signal.log_message.emit("❌ Chưa chọn profile nào!", "error")
            return

        # Check API server
        try:
            resp = requests.get(f"{API_BASE}/status", timeout=5)
            if resp.status_code != 200:
                self.signal.log_message.emit(
                    "❌ API server lỗi! Hãy start openclaw_api.py", "error")
                return
        except Exception:
            self.signal.log_message.emit(
                "❌ API server không chạy! Hãy chạy: python openclaw_api.py", "error")
            return

        self.is_running = True
        self.stop_requested = False
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

        # Reset stats
        self.total_likes = 0
        self.total_comments = 0
        self.profiles_done = 0
        self.profiles_failed = 0
        self._update_stat(self.stat_done, 0)
        self._update_stat(self.stat_likes, 0)
        self._update_stat(self.stat_comments, 0)
        self.progress_bar.setValue(0)

        self.signal.log_message.emit(
            f"🚀 Bắt đầu nuôi {len(selected)} profiles | "
            f"Like: {self.max_likes.value()}, Comment: {self.max_comments.value()}", "info")

        self._worker_thread = threading.Thread(
            target=self._interaction_worker,
            args=(selected,),
            daemon=True
        )
        self._worker_thread.start()

    def _stop_interaction(self):
        self.stop_requested = True
        self.signal.log_message.emit("⏹️ Đang dừng...", "warning")

    def _interaction_worker(self, profiles: List[Dict]):
        """Worker chính - xử lý từng profile qua API server."""
        total = len(profiles)

        for i, profile in enumerate(profiles):
            if self.stop_requested:
                break

            uuid = profile['uuid']
            name = profile['name']

            self.signal.log_message.emit(f"\n{'═'*40}", "info")
            self.signal.log_message.emit(f"👤 [{i+1}/{total}] {name}", "info")
            self.progress_bar.setValue(int((i / total) * 100))

            try:
                result = self._process_single_profile(uuid, name)
                self.signal.profile_done.emit(name, result)

                if result.get('success'):
                    likes = result.get('likes', 0)
                    cmts = result.get('comments_success', 0)
                    self.signal.log_message.emit(
                        f"✅ {name}: {likes} likes, {cmts} comments", "success")
                else:
                    self.profiles_failed += 1
                    self.signal.log_message.emit(
                        f"❌ {name}: {result.get('error', 'Unknown')}", "error")
            except Exception as e:
                self.profiles_failed += 1
                self.signal.log_message.emit(
                    f"❌ {name}: Exception - {e}", "error")

            # Delay giữa profiles
            if not self.stop_requested and i < total - 1:
                delay = self.profile_delay.value() + random.uniform(-2, 2)
                delay = max(3, delay)
                self.signal.log_message.emit(
                    f"⏳ Chờ {delay:.0f}s trước profile tiếp...", "info")
                for _ in range(int(delay)):
                    if self.stop_requested:
                        break
                    time.sleep(1)

        # Done
        self.is_running = False
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_bar.setValue(100)
        self.signal.log_message.emit(
            f"\n🏁 Hoàn thành! {self.profiles_done} xong, "
            f"{self.profiles_failed} lỗi | "
            f"{self.total_likes} likes, {self.total_comments} comments", "success")
        self.signal.step_update.emit("", "Hoàn thành!", "ok")

    # ══════════════════════════════════════
    #  PROCESS SINGLE PROFILE (via API)
    # ══════════════════════════════════════

    def _process_single_profile(self, uuid: str, name: str) -> dict:
        """
        Xử lý 1 profile qua API server:
        mở → check login → cuộn feed → like → comment → screenshot → đóng.
        """
        likes_done = 0
        comments_done = 0
        comments_verified = 0

        def step(desc: str, status: str = "working"):
            self.signal.step_update.emit(name, desc, status)
            self.signal.log_message.emit(f"   {desc}", "info")

        try:
            # ── 1. Mở browser ──
            step("🌐 Mở browser...")
            result = self._api_post("/open_browser", {"profile": uuid}, timeout=30)
            if result.get("type") == "error" or (
                    "error" in result and "success" not in result):
                err = result.get("message") or result.get("error", "Unknown")
                step(f"❌ Mở browser thất bại: {err}", "fail")
                return {"success": False, "error": f"Open browser: {err}"}
            step("✅ Browser đã mở", "ok")
            time.sleep(3)

            # ── 2. Screenshot ban đầu ──
            step("📸 Chụp screenshot...")
            self._take_screenshot(uuid)

            # ── 3. Check FB status ──
            if self.chk_skip_not_live.isChecked():
                step("🔍 Kiểm tra trạng thái login...")
                # Navigate to FB
                self._api_post("/navigate", {
                    "url": "https://www.facebook.com/", "profile": uuid
                }, timeout=20)
                time.sleep(4)

                status_result = self._api_post("/check_fb_status", {
                    "profile": uuid, "close_after": False
                }, timeout=30)
                fb_status = status_result.get("fb_status",
                                              status_result.get("status", "UNKNOWN"))
                status_clean = str(fb_status).split(':')[0] if fb_status else "UNKNOWN"

                if status_clean != "LIVE":
                    step(f"⚠️ Profile không LIVE: {fb_status}", "fail")
                    self._take_screenshot(uuid)
                    if self.chk_close_after.isChecked():
                        self._api_post("/close", {"profile": uuid}, timeout=15)
                    return {"success": False, "error": f"Not LIVE: {fb_status}",
                            "fb_status": str(fb_status)}
                step(f"✅ Status: {status_clean}", "ok")
                self._take_screenshot(uuid)

            # ── 4. Đọc Feed ──
            scroll_n = self.scroll_count.value()
            step(f"📜 Đọc feed (cuộn {scroll_n} lần)...")
            feed_result = self._api_post("/fb_read_feed", {
                "profile": uuid, "scroll_count": scroll_n
            }, timeout=60)
            posts = feed_result.get("posts", [])
            total_posts = len(posts)
            step(f"📰 Tìm thấy {total_posts} bài viết", "ok")
            self._take_screenshot(uuid)

            if self.stop_requested:
                return {"success": False, "error": "Stopped by user"}

            # ── 5. Like bài ──
            target_likes = self.max_likes.value()
            if target_likes > 0:
                step(f"👍 Bắt đầu like ({target_likes} bài)...")
                for li in range(target_likes):
                    if self.stop_requested:
                        break
                    like_result = self._api_post("/like",
                                                 {"profile": uuid}, timeout=20)
                    if like_result.get("success"):
                        likes_done += 1
                        step(f"👍 Like #{likes_done} OK", "ok")
                    else:
                        err = like_result.get('error', '?')[:40]
                        step(f"⚠️ Like thất bại: {err}", "fail")

                    # Screenshot sau mỗi 2 likes
                    if likes_done > 0 and likes_done % 2 == 0:
                        self._take_screenshot(uuid)

                    # Delay ngẫu nhiên giữa likes
                    time.sleep(random.uniform(2, 5))
                self._take_screenshot(uuid)

            if self.stop_requested:
                return {"success": False, "error": "Stopped by user",
                        "likes": likes_done}

            # ── 6. Comment bài ──
            target_comments = self.max_comments.value()
            if target_comments > 0 and total_posts > 0:
                step(f"💬 Bắt đầu comment ({target_comments} bài)...")
                n_comments = min(target_comments, total_posts)
                post_indices = random.sample(range(total_posts), n_comments)

                for idx in post_indices:
                    if self.stop_requested:
                        break

                    post_info = posts[idx] if idx < len(posts) else {}
                    post_content = (post_info.get('content', '')
                                    or post_info.get('author', ''))
                    author = post_info.get('author', 'Unknown')

                    step(f"💬 Comment bài #{idx} ({author[:20]})...")

                    # Generate AI comment
                    comment_text = self._generate_comment(post_content)
                    if not comment_text:
                        step(f"⚠️ Không tạo được comment", "fail")
                        continue

                    step(f"💬 \"{comment_text[:40]}\"")
                    cmt_result = self._api_post("/fb_comment", {
                        "profile": uuid,
                        "post_index": idx,
                        "comment": comment_text
                    }, timeout=30)

                    if cmt_result.get("success"):
                        comments_done += 1
                        verified = cmt_result.get("verified", False)
                        if verified:
                            comments_verified += 1
                        v_text = " (✓ verified)" if verified else ""
                        step(f"💬 Comment #{comments_done} OK{v_text}", "ok")
                    else:
                        err = cmt_result.get('error', '?')[:40]
                        step(f"⚠️ Comment thất bại: {err}", "fail")

                    self._take_screenshot(uuid)
                    time.sleep(random.uniform(3, 7))

            # ── 7. Screenshot cuối ──
            step("📸 Screenshot cuối...")
            self._take_screenshot(uuid)

            # ── 8. Đóng browser ──
            if self.chk_close_after.isChecked():
                step("🔚 Đóng browser...")
                self._api_post("/close", {"profile": uuid}, timeout=15)
                step("✅ Đã đóng browser", "ok")

            return {
                "success": True,
                "likes": likes_done,
                "comments_success": comments_done,
                "comments_verified": comments_verified,
                "total_posts": total_posts,
            }

        except Exception as e:
            self.signal.log_message.emit(f"   ❌ Exception: {e}", "error")
            if self.chk_close_after.isChecked():
                try:
                    self._api_post("/close", {"profile": uuid}, timeout=10)
                except Exception:
                    pass
            return {"success": False, "error": str(e),
                    "likes": likes_done,
                    "comments_success": comments_done}

    # ══════════════════════════════════════
    #  AI COMMENT GENERATION
    # ══════════════════════════════════════

    def _generate_comment(self, post_content: str) -> Optional[str]:
        """Generate AI comment using Pollinations.ai (same as feed_skill)."""
        fallbacks = [
            "Hay quá!", "👍 Nice!", "Đỉnh thật 🔥", "Chuẩn luôn!",
            "Quá đỉnh 😍", "Tuyệt vời!", "Like mạnh 💪",
            "Xịn quá!", "🙌 Chất lượng!", "👏👏"
        ]

        if not post_content or len(post_content.strip()) < 5:
            return random.choice(fallbacks)

        try:
            prompt = (
                f"Hãy viết 1 comment ngắn (5-15 từ) bằng tiếng Việt cho bài Facebook. "
                f"Giọng tích cực, tự nhiên. KHÔNG hashtag, KHÔNG tag ai.\n\n"
                f"Bài: {post_content[:200]}\n\nComment:"
            )

            resp = requests.post(
                "https://gen.pollinations.ai/v1/chat/completions",
                json={
                    "model": "gemini-fast",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 50,
                    "temperature": 0.8
                },
                headers={
                    "Authorization": "Bearer sk_3HRi9HUGLup7OKB6ykRds8YtnpcmLHD3",
                    "Content-Type": "application/json"
                },
                timeout=15
            )

            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"].strip()
                content = content.strip('"').strip("'")
                if len(content) > 100:
                    content = content[:100]
                if len(content) > 3:
                    return content
        except Exception as e:
            print(f"[InteractionPage] Comment gen error: {e}")

        return random.choice(fallbacks)
