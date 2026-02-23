"""
FB Manager Pro - Interaction Page
🎯 Nuôi nick tự động: Cuộn News Feed + Like bằng AI phân tích DOM
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
from PySide6.QtCore import Qt, Signal, QObject, QTimer
from PySide6.QtGui import QColor

from config import COLORS
from widgets import CyberButton, CyberInput, CyberCard, CyberCheckBox
from db import get_profiles
from api_service import api
from automation.cdp_client import CDPClient


class InteractionSignals(QObject):
    """Signals cho interaction page"""
    log_message = Signal(str, str)  # message, level
    progress_update = Signal(int, int)  # current, total
    profile_done = Signal(str, int, int)  # profile_name, likes, scrolls
    folders_loaded = Signal(list)
    profiles_loaded = Signal(list)


class InteractionPage(QWidget):
    """
    Tab Tương tác - Nuôi nick tự động
    
    Flow:
    1. Mở browser profile
    2. Vào facebook.com (News Feed)
    3. Cuộn trang
    4. Lấy DOM → Gửi AI (LLM-Mux) phân tích tìm nút Like
    5. CDP click vào nút Like
    6. Lặp lại
    """
    
    def __init__(self, log_func=None):
        QWidget.__init__(self)
        self.log = log_func or print
        self.signal = InteractionSignals()
        self.signal.log_message.connect(self._handle_log)
        self.signal.profile_done.connect(self._on_profile_done)
        self.signal.folders_loaded.connect(self._on_folders_loaded)
        self.signal.profiles_loaded.connect(self._on_profiles_loaded)
        
        # Data
        self.folders = []
        self.profiles = []
        self.is_running = False
        self.stop_requested = False
        self._worker_thread = None
        
        # Stats
        self.total_likes = 0
        self.total_scrolls = 0
        self.profiles_done = 0
        
        # LLM Config - mặc định LLM-Mux
        self.llm_endpoint = "http://127.0.0.1:8317/v1/chat/completions"
        self.llm_model = "gemini-2.5-flash"
        
        self._setup_ui()
        QTimer.singleShot(500, self._load_folders)
    
    def _handle_log(self, msg: str, level: str):
        """Handle log từ signal"""
        self.log(msg, level)
        self._append_log(msg, level)
    
    def _append_log(self, msg: str, level: str = "info"):
        """Append log to terminal"""
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
    
    def _on_profile_done(self, name: str, likes: int, scrolls: int):
        """Callback khi xong 1 profile"""
        self.profiles_done += 1
        self.total_likes += likes
        self.total_scrolls += scrolls
        self._update_stat(self.stat_done, self.profiles_done)
        self._update_stat(self.stat_likes, self.total_likes)
    
    def _setup_ui(self):
        """Setup UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        # Header
        header = QHBoxLayout()
        title = QLabel("🎯 NUÔI NICK")
        title.setStyleSheet(f"""
            font-size: 24px;
            font-weight: bold;
            color: {COLORS['neon_coral']};
        """)
        header.addWidget(title)
        
        subtitle = QLabel("Cuộn Feed + Like bằng AI")
        subtitle.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px;")
        header.addWidget(subtitle)
        header.addStretch()
        
        # Stats
        self.stat_profiles = self._create_stat_badge("0", "PROFILES", COLORS['neon_cyan'])
        self.stat_done = self._create_stat_badge("0", "XONG", COLORS['neon_mint'])
        self.stat_likes = self._create_stat_badge("0", "LIKES", COLORS['neon_pink'])
        header.addWidget(self.stat_profiles)
        header.addWidget(self.stat_done)
        header.addWidget(self.stat_likes)
        
        layout.addLayout(header)
        
        # Main content - Splitter
        splitter = QSplitter(Qt.Horizontal)
        
        # ===== LEFT: Profile Selection =====
        left_panel = self._create_profile_panel()
        splitter.addWidget(left_panel)
        
        # ===== RIGHT: Settings & Log =====
        right_panel = self._create_settings_panel()
        splitter.addWidget(right_panel)
        
        splitter.setSizes([400, 600])
        layout.addWidget(splitter)
    
    def _create_profile_panel(self) -> QFrame:
        """Panel chọn profiles"""
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
        
        # Folder selection
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
            QComboBox::drop-down {{
                border: none;
            }}
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
    
    def _create_settings_panel(self) -> QFrame:
        """Panel cài đặt"""
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
        
        # ===== Cài đặt cuộn =====
        scroll_group = QGroupBox("📜 Cài đặt cuộn")
        scroll_group.setStyleSheet(self._group_box_style())
        scroll_layout = QGridLayout(scroll_group)
        
        scroll_layout.addWidget(QLabel("Số lần cuộn:"), 0, 0)
        self.scroll_count = QSpinBox()
        self.scroll_count.setRange(3, 50)
        self.scroll_count.setValue(10)
        self.scroll_count.setStyleSheet(self._spinbox_style())
        scroll_layout.addWidget(self.scroll_count, 0, 1)
        
        scroll_layout.addWidget(QLabel("Delay cuộn (s):"), 0, 2)
        self.scroll_delay = QSpinBox()
        self.scroll_delay.setRange(1, 10)
        self.scroll_delay.setValue(2)
        self.scroll_delay.setStyleSheet(self._spinbox_style())
        scroll_layout.addWidget(self.scroll_delay, 0, 3)
        
        layout.addWidget(scroll_group)
        
        # ===== Cài đặt Like =====
        like_group = QGroupBox("👍 Cài đặt Like")
        like_group.setStyleSheet(self._group_box_style())
        like_layout = QGridLayout(like_group)
        
        like_layout.addWidget(QLabel("Số like tối đa:"), 0, 0)
        self.max_likes = QSpinBox()
        self.max_likes.setRange(1, 30)
        self.max_likes.setValue(5)
        self.max_likes.setStyleSheet(self._spinbox_style())
        like_layout.addWidget(self.max_likes, 0, 1)
        
        like_layout.addWidget(QLabel("% chance like:"), 0, 2)
        self.like_chance = QSpinBox()
        self.like_chance.setRange(10, 100)
        self.like_chance.setValue(50)
        self.like_chance.setSuffix("%")
        self.like_chance.setStyleSheet(self._spinbox_style())
        like_layout.addWidget(self.like_chance, 0, 3)
        
        like_layout.addWidget(QLabel("Delay sau like (s):"), 1, 0)
        self.like_delay_min = QSpinBox()
        self.like_delay_min.setRange(1, 30)
        self.like_delay_min.setValue(2)
        self.like_delay_min.setStyleSheet(self._spinbox_style())
        like_layout.addWidget(self.like_delay_min, 1, 1)
        
        like_layout.addWidget(QLabel("-"), 1, 2)
        self.like_delay_max = QSpinBox()
        self.like_delay_max.setRange(2, 60)
        self.like_delay_max.setValue(5)
        self.like_delay_max.setStyleSheet(self._spinbox_style())
        like_layout.addWidget(self.like_delay_max, 1, 3)
        
        layout.addWidget(like_group)
        
        # ===== AI Config =====
        ai_group = QGroupBox("🤖 LLM-Mux (AI phân tích DOM)")
        ai_group.setStyleSheet(self._group_box_style())
        ai_layout = QVBoxLayout(ai_group)
        
        endpoint_row = QHBoxLayout()
        endpoint_row.addWidget(QLabel("Endpoint:"))
        self.llm_endpoint_input = CyberInput(placeholder="http://127.0.0.1:8317/v1/chat/completions")
        self.llm_endpoint_input.setText(self.llm_endpoint)
        endpoint_row.addWidget(self.llm_endpoint_input)
        ai_layout.addLayout(endpoint_row)
        
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Model:"))
        self.llm_model_combo = QComboBox()
        self.llm_model_combo.addItems([
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-3-flash",
            "claude-3-5-haiku-20241022",
            "claude-sonnet-4-5",
        ])
        self.llm_model_combo.setStyleSheet(f"""
            QComboBox {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_primary']};
                border: 1px solid {COLORS['border']};
                border-radius: 6px;
                padding: 6px 12px;
            }}
        """)
        model_row.addWidget(self.llm_model_combo)
        
        btn_test_ai = CyberButton("🧪 Test", variant="purple")
        btn_test_ai.clicked.connect(self._test_llm_connection)
        model_row.addWidget(btn_test_ai)
        ai_layout.addLayout(model_row)
        
        layout.addWidget(ai_group)
        
        # ===== Progress =====
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
                background: qlineargradient(x1:0, x2:1, stop:0 {COLORS['neon_coral']}, stop:1 {COLORS['neon_pink']});
                border-radius: 5px;
            }}
        """)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        
        # ===== Control buttons =====
        btn_row = QHBoxLayout()
        self.btn_start = CyberButton("🚀 BẮT ĐẦU NUÔI NICK", variant="success")
        self.btn_start.clicked.connect(self._start_interaction)
        self.btn_stop = CyberButton("⏹️ DỪNG", variant="danger")
        self.btn_stop.clicked.connect(self._stop_interaction)
        self.btn_stop.setEnabled(False)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        layout.addLayout(btn_row)
        
        # ===== Log =====
        log_label = QLabel("📜 Log")
        log_label.setStyleSheet(f"color: {COLORS['neon_yellow']}; font-weight: bold;")
        layout.addWidget(log_label)
        
        self.log_terminal = QTextEdit()
        self.log_terminal.setReadOnly(True)
        self.log_terminal.setStyleSheet(f"""
            QTextEdit {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_secondary']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                padding: 10px;
                font-family: 'Consolas', monospace;
                font-size: 11px;
            }}
        """)
        layout.addWidget(self.log_terminal)
        
        return panel
    
    def _create_stat_badge(self, value: str, label: str, color: str) -> QFrame:
        """Tạo stat badge"""
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
        """Update stat value"""
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
    
    def _load_folders(self):
        """Load danh sách folders từ API"""
        def fetch():
            try:
                return api.get_folders()
            except:
                return []
        
        def worker():
            folders = fetch()
            self.signal.folders_loaded.emit(folders)
        
        threading.Thread(target=worker, daemon=True).start()
    
    def _on_folders_loaded(self, folders: List[Dict]):
        """Callback khi load folders xong"""
        self.folders = folders or []
        self.folder_combo.clear()
        self.folder_combo.addItem("-- Chọn thư mục --")
        for folder in self.folders:
            name = folder.get('name', 'Unknown')
            self.folder_combo.addItem(f"📁 {name}")
        self.signal.log_message.emit(f"Đã tải {len(self.folders)} thư mục", "info")
    
    def _on_folder_change(self, index: int):
        """Khi chọn folder → load profiles"""
        if index > 0:
            self._load_profiles()
    
    def _load_profiles(self):
        """Load profiles từ folder đã chọn"""
        folder_idx = self.folder_combo.currentIndex()
        if folder_idx <= 0:
            return
        
        folder = self.folders[folder_idx - 1]
        folder_id = folder.get('id')
        
        self.signal.log_message.emit(f"Đang tải profiles từ {folder.get('name')}...", "info")
        
        def fetch():
            try:
                return api.get_profiles(folder_id=[folder_id], limit=500)
            except:
                return []
        
        def worker():
            profiles = fetch()
            self.signal.profiles_loaded.emit(profiles)
        
        threading.Thread(target=worker, daemon=True).start()
    
    def _on_profiles_loaded(self, profiles: List[Dict]):
        """Callback khi load profiles xong"""
        self.profiles = profiles or []
        self._render_profiles(self.profiles)
        self._update_stat(self.stat_profiles, len(self.profiles))
        self.signal.log_message.emit(f"Đã tải {len(self.profiles)} profiles", "success")
    
    def _render_profiles(self, profiles: List[Dict]):
        """Render profiles vào table"""
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
        """Filter profiles"""
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
        """Lấy profiles đã chọn"""
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
    
    def _test_llm_connection(self):
        """Test kết nối LLM-Mux"""
        self.signal.log_message.emit(f"🔄 Đang test kết nối LLM...", "info")
        
        def test():
            try:
                endpoint = self.llm_endpoint_input.text().strip()
                model = self.llm_model_combo.currentText()
                
                print(f"[Test] Endpoint: {endpoint}")
                print(f"[Test] Model: {model}")
                
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": "Hi, respond with just 'OK'"}],
                    "max_tokens": 10
                }
                
                print(f"[Test] Sending request...")
                resp = requests.post(endpoint, json=payload, timeout=10)
                print(f"[Test] Response status: {resp.status_code}")
                print(f"[Test] Response: {resp.text[:200]}")
                
                if resp.status_code == 200:
                    content = resp.json().get('choices', [{}])[0].get('message', {}).get('content', '')
                    self.signal.log_message.emit(f"✅ LLM-Mux OK! Response: {content}", "success")
                else:
                    self.signal.log_message.emit(f"❌ LLM-Mux lỗi: {resp.status_code} - {resp.text[:100]}", "error")
            except requests.exceptions.ConnectTimeout:
                self.signal.log_message.emit(f"❌ Không kết nối được - Server không chạy?", "error")
            except requests.exceptions.ReadTimeout:
                self.signal.log_message.emit(f"❌ Timeout - Server quá chậm hoặc bị treo", "error")
            except requests.exceptions.ConnectionError as e:
                self.signal.log_message.emit(f"❌ Connection Error - Server không chạy!", "error")
                print(f"[Test] ConnectionError: {e}")
            except Exception as e:
                self.signal.log_message.emit(f"❌ LLM-Mux lỗi: {e}", "error")
                print(f"[Test] Exception: {e}")
        
        threading.Thread(target=test, daemon=True).start()
    
    def _start_interaction(self):
        """Bắt đầu nuôi nick"""
        selected = self._get_selected_profiles()
        if not selected:
            self.signal.log_message.emit("❌ Chưa chọn profile nào!", "error")
            return
        
        self.is_running = True
        self.stop_requested = False
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        
        # Reset stats
        self.total_likes = 0
        self.total_scrolls = 0
        self.profiles_done = 0
        self._update_stat(self.stat_done, 0)
        self._update_stat(self.stat_likes, 0)
        self.progress_bar.setValue(0)
        
        self.signal.log_message.emit(f"🚀 Bắt đầu nuôi {len(selected)} profiles...", "info")
        
        self._worker_thread = threading.Thread(
            target=self._interaction_worker,
            args=(selected,),
            daemon=True
        )
        self._worker_thread.start()
    
    def _stop_interaction(self):
        """Dừng"""
        self.stop_requested = True
        self.signal.log_message.emit("⏹️ Đang dừng...", "warning")
    
    def _interaction_worker(self, profiles: List[Dict]):
        """Worker thread chính"""
        total = len(profiles)
        
        for i, profile in enumerate(profiles):
            if self.stop_requested:
                break
            
            uuid = profile['uuid']
            name = profile['name']
            
            self.signal.log_message.emit(f"👤 [{i+1}/{total}] {name}", "info")
            self.progress_bar.setValue(int((i / total) * 100))
            
            try:
                likes, scrolls = self._process_profile(uuid, name)
                self.signal.profile_done.emit(name, likes, scrolls)
                self.signal.log_message.emit(f"✅ {name}: {likes} likes, {scrolls} scrolls", "success")
            except Exception as e:
                self.signal.log_message.emit(f"❌ {name}: {e}", "error")
            
            # Delay giữa profiles
            if not self.stop_requested and i < total - 1:
                delay = random.uniform(5, 10)
                self.signal.log_message.emit(f"⏳ Chờ {delay:.1f}s...", "info")
                time.sleep(delay)
        
        # Done
        self.is_running = False
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_bar.setValue(100)
        self.signal.log_message.emit(f"🏁 Hoàn thành! {self.total_likes} likes từ {self.profiles_done} profiles", "success")
    
    def _process_profile(self, uuid: str, name: str) -> Tuple[int, int]:
        """Xử lý 1 profile: mở browser, cuộn, like"""
        likes = 0
        scrolls = 0
        cdp = None
        
        try:
            # 1. Mở browser
            print(f"[Interaction] Opening browser for {name}...")
            result = api.open_browser(uuid)
            print(f"[Interaction] open_browser result: {result.get('type', 'OK')}")
            
            if result.get('type') == 'error':
                raise Exception(f"Không mở được browser: {result.get('message', 'Unknown error')}")
            
            time.sleep(2)
            
            # 2. Lấy remote_port và tạo CDPClient
            data = result.get('data', {})
            remote_port = data.get('remote_port')
            ws_url = data.get('web_socket', '')
            
            # Nếu không có remote_port, lấy từ ws_url
            if not remote_port and ws_url:
                match = re.search(r':(\d+)/', ws_url)
                if match:
                    remote_port = int(match.group(1))
            
            print(f"[Interaction] Remote port: {remote_port}")
            
            if not remote_port:
                raise Exception(f"Không có remote_port. Data: {data}")
            
            # 3. Kết nối CDP
            print(f"[Interaction] Creating CDPClient...")
            cdp = CDPClient(remote_port)
            print(f"[Interaction] Calling cdp.connect()...")
            try:
                connect_result = cdp.connect()
                print(f"[Interaction] cdp.connect() returned: {connect_result.success}, {connect_result.error}")
            except Exception as cdp_err:
                print(f"[Interaction] cdp.connect() EXCEPTION: {cdp_err}")
                raise
            
            if not connect_result.success:
                raise Exception(f"CDP connect failed: {connect_result.error}")
            
            self.signal.log_message.emit(f"   ✓ Đã kết nối CDP", "info")
            print(f"[Interaction] CDP connected!")
            
            # 4. Navigate đến Facebook
            self.signal.log_message.emit(f"   🌐 Đang vào facebook.com...", "info")
            print(f"[Interaction] Navigating to facebook.com...")
            
            nav_result = cdp.navigate("https://www.facebook.com/", wait_load=True)
            if not nav_result.success:
                raise Exception(f"Navigate failed: {nav_result.error}")
            
            time.sleep(3)
            print(f"[Interaction] Page loaded, starting scroll/like loop...")
            
            self.signal.log_message.emit(f"   📜 Bắt đầu cuộn và like...", "info")
            
            # 5. Cuộn và like - Logic mới:
            # - Cuộn 3-5 lần ngẫu nhiên (như người thật)
            # - Gửi DOM → AI tìm nút Like → Click
            # - Delay 3-5s
            # - Lặp lại
            max_likes = self.max_likes.value()
            
            while likes < max_likes:
                if self.stop_requested:
                    break
                
                # 1. Cuộn 3-5 lần ngẫu nhiên (như người thật)
                scroll_times = random.randint(3, 5)
                for _ in range(scroll_times):
                    if self.stop_requested:
                        break
                    scroll_amount = random.randint(300, 600)
                    cdp._evaluate_js(f"window.scrollBy(0, {scroll_amount})")
                    scrolls += 1
                    self.signal.log_message.emit(f"   📜 Cuộn #{scrolls}", "info")
                    # Delay giữa các lần cuộn: 0.5-1.5s
                    time.sleep(random.uniform(0.5, 1.5))
                
                # 2. Tìm nút Like qua AI và click
                try:
                    self.signal.log_message.emit(f"   🔍 Tìm bài viết và nút Like...", "info")
                    result = self._find_like_button_with_ai(cdp)
                    if result:
                        likes += 1
                        self.signal.log_message.emit(f"   👍 Like #{likes}", "success")
                        time.sleep(random.uniform(1, 2))
                    else:
                        self.signal.log_message.emit(f"   ⚠️ Không tìm thấy bài/nút Like", "warning")
                except Exception as e:
                    self.signal.log_message.emit(f"   ⚠️ Lỗi like: {e}", "warning")
                
                # 3. Delay 3-5s trước vòng tiếp
                if likes < max_likes and not self.stop_requested:
                    delay = random.uniform(3, 5)
                    self.signal.log_message.emit(f"   ⏳ Chờ {delay:.1f}s...", "info")
                    time.sleep(delay)
            
        finally:
            if cdp:
                try:
                    cdp.disconnect()
                except:
                    pass
        
        return likes, scrolls
    
    def _find_like_button_with_ai(self, cdp: CDPClient) -> Optional[str]:
        """Gửi DOM lên AI để tìm nút Like và click"""
        
        # Bước 1: Lấy HTML của các bài viết trong viewport
        get_posts_html_js = '''
        (function() {
            // Tìm các bài viết
            let posts = document.querySelectorAll('[data-ad-rendering-role="story_message"]');
            if (posts.length === 0) {
                posts = document.querySelectorAll('[role="article"]');
            }
            
            const results = [];
            let idx = 0;
            
            for (let post of posts) {
                const rect = post.getBoundingClientRect();
                
                // Chỉ lấy bài trong viewport
                if (rect.bottom < 0 || rect.top > window.innerHeight) continue;
                
                idx++;
                
                // Đánh dấu bài viết để click sau
                post.setAttribute('data-ai-post-idx', idx.toString());
                
                // Lấy HTML rút gọn (chỉ lấy phần action bar có nút Like)
                // Tìm các nút trong bài
                const buttons = post.querySelectorAll('div[role="button"]');
                const buttonInfos = [];
                
                let btnIdx = 0;
                for (let btn of buttons) {
                    btnIdx++;
                    
                    // Đánh dấu nút
                    btn.setAttribute('data-ai-btn-idx', `${idx}-${btnIdx}`);
                    
                    const label = btn.getAttribute('aria-label') || '';
                    const pressed = btn.getAttribute('aria-pressed') || '';
                    const spans = btn.querySelectorAll('span');
                    const texts = [];
                    for (let s of spans) {
                        const t = (s.innerText || '').trim();
                        if (t) texts.push(t);
                    }
                    
                    buttonInfos.push({
                        btnIdx: btnIdx,
                        ariaLabel: label,
                        ariaPressed: pressed,
                        spanTexts: texts.slice(0, 3) // Chỉ lấy 3 span đầu
                    });
                }
                
                results.push({
                    postIdx: idx,
                    top: Math.round(rect.top),
                    buttons: buttonInfos.slice(0, 20) // Giới hạn 20 nút mỗi bài
                });
                
                if (results.length >= 5) break; // Chỉ lấy 5 bài
            }
            
            return JSON.stringify({
                totalPosts: posts.length,
                viewportPosts: results
            });
        })()
        '''
        
        print(f"[Interaction] Extracting posts DOM...")
        dom_result = cdp._evaluate_js(get_posts_html_js)
        print(f"[Interaction] DOM result: {dom_result[:800] if dom_result else 'None'}")
        
        if not dom_result:
            return None
        
        try:
            dom_data = json.loads(dom_result)
            viewport_posts = dom_data.get('viewportPosts', [])
            print(f"[Interaction] Found {len(viewport_posts)} posts in viewport")
        except Exception as e:
            print(f"[Interaction] JSON parse error: {e}")
            return None
        
        if not viewport_posts:
            print(f"[Interaction] No posts in viewport!")
            return None
        
        # Bước 2: Gửi lên AI để chọn nút Like
        endpoint = self.llm_endpoint_input.text().strip()
        model = self.llm_model_combo.currentText()
        
        prompt = f"""Đây là danh sách các bài viết Facebook và các nút trong mỗi bài.
Nhiệm vụ: Tìm nút LIKE của BÀI VIẾT (không phải comment).

Dữ liệu:
{json.dumps(viewport_posts, indent=2, ensure_ascii=False)}

Hướng dẫn:
- Nút Like có spanTexts chứa "Thích" hoặc "Like"
- Nút đã like có ariaPressed="true" hoặc spanTexts chứa "Đã thích"/"Liked" -> BỎ QUA
- Chọn nút Like CHƯA ĐƯỢC NHẤN

Trả lời ĐÚNG FORMAT sau (không giải thích):
postIdx,btnIdx

Ví dụ: 1,3 (nghĩa là bài 1, nút 3)
Nếu không tìm thấy nút Like phù hợp, trả lời: 0,0
"""
        
        print(f"[Interaction] Sending to AI: {endpoint}, model: {model}")
        
        try:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 50,
                "temperature": 0
            }
            
            print(f"[Interaction] Calling LLM API...")
            resp = requests.post(endpoint, json=payload, timeout=30)
            print(f"[Interaction] LLM response status: {resp.status_code}")
            
            if resp.status_code != 200:
                print(f"[Interaction] LLM error: {resp.text[:200]}")
                return None
            
            content = resp.json().get('choices', [{}])[0].get('message', {}).get('content', '')
            content = content.strip()
            print(f"[Interaction] AI response: {content}")
            
            # Parse response: postIdx,btnIdx
            match = re.search(r'(\d+)\s*,\s*(\d+)', content)
            if not match:
                print(f"[Interaction] Could not parse AI response")
                return None
            
            post_idx = int(match.group(1))
            btn_idx = int(match.group(2))
            
            if post_idx == 0 or btn_idx == 0:
                print(f"[Interaction] AI said no Like button found")
                return None
            
            print(f"[Interaction] AI chose: post {post_idx}, button {btn_idx}")
            
            # Bước 3: Click nút được AI chọn
            click_js = f'''
            (function() {{
                const btn = document.querySelector('[data-ai-btn-idx="{post_idx}-{btn_idx}"]');
                if (!btn) return JSON.stringify({{success: false, error: "Button not found"}});
                
                // Scroll vào view và click
                btn.scrollIntoView({{block: 'center', behavior: 'smooth'}});
                
                setTimeout(() => btn.click(), 300);
                
                return JSON.stringify({{
                    success: true,
                    clicked: "{post_idx}-{btn_idx}"
                }});
            }})()
            '''
            
            click_result = cdp._evaluate_js(click_js)
            print(f"[Interaction] Click result: {click_result}")
            
            if click_result:
                try:
                    click_data = json.loads(click_result)
                    if click_data.get('success'):
                        return "clicked"
                except:
                    pass
            
        except Exception as e:
            print(f"[Interaction] AI error: {e}")
            self.signal.log_message.emit(f"   ⚠️ AI error: {e}", "warning")
        
        return None
