"""
Groups Page - Đăng nhóm Facebook
PySide6 version với 3 TABS: Quét nhóm, Đăng nhóm, Đẩy tin
HỖ TRỢ: Multi-profile posting, Parallel scanning với ThreadPoolExecutor
"""
import threading
import random
import time
import os
from typing import List, Dict, Optional
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QMessageBox, QProgressBar, QTableWidgetItem, QSpinBox,
    QTabWidget, QScrollArea, QTextEdit, QFileDialog, QCheckBox
)
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor

from config import COLORS
from widgets import (
    CyberButton, CyberInput, CyberComboBox, CyberCard,
    CyberTitle, CyberStatCard, CyberTable, CyberCheckBox
)
from api_service import api
from db import (
    get_profiles, get_groups, get_groups_for_profiles, save_group,
    delete_group, sync_groups, clear_groups, get_categories, get_contents,
    save_post_history, get_post_history, get_post_history_filtered, get_post_history_count
)

# Import automation modules
import requests
import websocket
import json as json_module
import re

try:
    from automation.window_manager import acquire_window_slot, release_window_slot, get_window_bounds
    WINDOW_MANAGER_AVAILABLE = True
except ImportError:
    WINDOW_MANAGER_AVAILABLE = False
    def acquire_window_slot(): return 0
    def release_window_slot(slot_id): pass
    def get_window_bounds(slot_id): return (0, 0, 800, 600)

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False


class GroupsSignal(QObject):
    """Signal để thread-safe UI update"""
    data_loaded = Signal(dict)  # folders, profiles, categories
    groups_loaded = Signal(list)
    scan_progress = Signal(int, int)  # current, total
    scan_complete = Signal()
    post_progress = Signal(int, int)
    post_complete = Signal()
    comment_progress = Signal(int, int)
    comment_complete = Signal()
    log_message = Signal(str, str)  # message, type


class GroupsPage(QWidget):
    """Groups Page - Đăng nhóm với 3 TABS"""

    def __init__(self, log_func, parent=None):
        super().__init__(parent)
        self.log = log_func
        self.profiles: List[Dict] = []
        self.groups: List[Dict] = []
        self.folders: List[Dict] = []
        self.contents: List[Dict] = []
        self.categories: List[Dict] = []

        # Selection
        self.selected_profile_uuids: List[str] = []
        self.profile_checkboxes: Dict[str, CyberCheckBox] = {}
        self.scan_group_checkboxes: Dict[int, CyberCheckBox] = {}
        self.post_group_checkboxes: Dict[int, CyberCheckBox] = {}
        self.boost_checkboxes: Dict[int, CyberCheckBox] = {}

        # Multi-profile support
        self.profile_groups: Dict[str, List[Dict]] = {}  # profile_uuid -> groups
        self._scan_completed_count = 0
        self._multi_profile_mode = False

        # State
        self._is_scanning = False
        self._is_posting = False
        self._is_commenting = False
        self._stop_requested = False

        # Posted history
        self.posted_history: List[Dict] = []
        self.boost_posts: List[Dict] = []

        # Signal
        self.signal = GroupsSignal()
        self.signal.data_loaded.connect(self._on_data_loaded)
        self.signal.groups_loaded.connect(self._on_groups_loaded)
        self.signal.scan_progress.connect(self._on_scan_progress)
        self.signal.scan_complete.connect(self._on_scan_complete)
        self.signal.post_progress.connect(self._on_post_progress)
        self.signal.post_complete.connect(self._on_post_complete)
        self.signal.comment_progress.connect(self._on_comment_progress)
        self.signal.comment_complete.connect(self._on_comment_complete)
        self.signal.log_message.connect(lambda msg, t: self.log(msg, t))

        self._setup_ui()
        QTimer.singleShot(500, self._load_data)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # ========== TOP BAR ==========
        top_bar = QHBoxLayout()
        top_bar.setSpacing(12)

        title = CyberTitle("Đăng Nhóm", "Quét, đăng bài và đẩy tin vào nhóm", "coral")
        top_bar.addWidget(title)

        top_bar.addStretch()

        self.stat_profiles = CyberStatCard("PROFILES", "0", "📁", "coral")
        self.stat_profiles.setFixedWidth(140)
        top_bar.addWidget(self.stat_profiles)

        self.stat_groups = CyberStatCard("NHÓM", "0", "👥", "purple")
        self.stat_groups.setFixedWidth(140)
        top_bar.addWidget(self.stat_groups)

        self.stat_selected = CyberStatCard("ĐÃ CHỌN", "0", "✓", "mint")
        self.stat_selected.setFixedWidth(140)
        top_bar.addWidget(self.stat_selected)

        layout.addLayout(top_bar)

        # ========== PROFILE SELECTOR ==========
        profile_bar = QHBoxLayout()
        profile_bar.setSpacing(8)

        folder_label = QLabel("📁 Thư mục:")
        folder_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 12px;")
        profile_bar.addWidget(folder_label)

        self.folder_combo = CyberComboBox(["📁 Tất cả"])
        self.folder_combo.setFixedWidth(180)
        self.folder_combo.currentIndexChanged.connect(self._on_folder_change)
        profile_bar.addWidget(self.folder_combo)

        profile_label = QLabel("📱 Profile:")
        profile_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 12px;")
        profile_bar.addWidget(profile_label)

        self.profile_combo = CyberComboBox(["-- Chọn profile --"])
        self.profile_combo.setFixedWidth(220)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_change)
        profile_bar.addWidget(self.profile_combo)

        btn_refresh = CyberButton("🔄 Làm mới", "ghost")
        btn_refresh.clicked.connect(self._load_data)
        profile_bar.addWidget(btn_refresh)

        profile_bar.addStretch()

        self.profile_status = QLabel("")
        self.profile_status.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        profile_bar.addWidget(self.profile_status)

        layout.addLayout(profile_bar)

        # ========== TAB WIDGET - 3 TABS ==========
        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet(f"""
            QTabWidget::pane {{
                background: {COLORS['bg_card']};
                border: 2px solid {COLORS['border']};
                border-radius: 12px;
            }}
            QTabBar::tab {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_muted']};
                padding: 10px 24px;
                margin-right: 4px;
                border-radius: 8px 8px 0 0;
                font-weight: bold;
                font-size: 12px;
            }}
            QTabBar::tab:selected {{
                background: {COLORS['bg_card']};
                color: {COLORS['neon_cyan']};
                border-bottom: 3px solid {COLORS['neon_cyan']};
            }}
            QTabBar::tab:hover:!selected {{
                background: {COLORS['bg_hover']};
                color: {COLORS['text_primary']};
            }}
        """)

        # Tab 1: Quét nhóm
        self.tab_scan = QWidget()
        self._create_scan_tab()
        self.tab_widget.addTab(self.tab_scan, "🔍 Quét nhóm")

        # Tab 2: Đăng nhóm
        self.tab_post = QWidget()
        self._create_post_tab()
        self.tab_widget.addTab(self.tab_post, "📤 Đăng nhóm")

        # Tab 3: Đẩy tin
        self.tab_boost = QWidget()
        self._create_boost_tab()
        self.tab_widget.addTab(self.tab_boost, "💬 Đẩy tin")

        layout.addWidget(self.tab_widget, 1)

    def _create_scan_tab(self):
        """Tab Quét nhóm"""
        layout = QVBoxLayout(self.tab_scan)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Action bar
        action_bar = QHBoxLayout()
        action_bar.setSpacing(8)

        self.btn_scan = CyberButton("🔍 Quét nhóm", "success")
        self.btn_scan.clicked.connect(self._scan_groups)
        action_bar.addWidget(self.btn_scan)

        self.btn_stop_scan = CyberButton("⏹️ Dừng", "danger")
        self.btn_stop_scan.clicked.connect(self._stop_scan)
        self.btn_stop_scan.setEnabled(False)
        action_bar.addWidget(self.btn_stop_scan)

        btn_clear = CyberButton("🗑️ Xóa tất cả", "ghost")
        btn_clear.clicked.connect(self._clear_all_groups)
        action_bar.addWidget(btn_clear)

        action_bar.addStretch()

        self.scan_stats = QLabel("Tổng: 0 nhóm")
        self.scan_stats.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px;")
        action_bar.addWidget(self.scan_stats)

        layout.addLayout(action_bar)

        # Progress
        self.scan_progress = QProgressBar()
        self.scan_progress.setFixedHeight(8)
        self.scan_progress.setStyleSheet(f"""
            QProgressBar {{
                background: {COLORS['bg_darker']};
                border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background: {COLORS['neon_cyan']};
                border-radius: 4px;
            }}
        """)
        layout.addWidget(self.scan_progress)

        # Groups table
        self.scan_table = CyberTable(["✓", "ID", "Tên nhóm", "Group ID", "Thành viên", "Ngày quét"])
        self.scan_table.setColumnWidth(0, 50)
        self.scan_table.setColumnWidth(1, 50)
        self.scan_table.setColumnWidth(2, 280)
        self.scan_table.setColumnWidth(3, 180)
        self.scan_table.setColumnWidth(4, 100)
        self.scan_table.setColumnWidth(5, 120)
        layout.addWidget(self.scan_table, 1)

    def _create_post_tab(self):
        """Tab Đăng nhóm"""
        layout = QHBoxLayout(self.tab_post)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # ========== LEFT PANEL - Groups List ==========
        left_card = CyberCard(COLORS['neon_purple'])
        left_card.setFixedWidth(360)
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(2, 2, 2, 2)

        # Header
        header = QWidget()
        header.setFixedHeight(44)
        header.setStyleSheet(f"background: {COLORS['bg_darker']}; border-radius: 14px 14px 0 0;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 0, 16, 0)

        header_title = QLabel("📋 Danh sách nhóm")
        header_title.setStyleSheet(f"color: {COLORS['neon_purple']}; font-size: 13px; font-weight: bold;")
        header_layout.addWidget(header_title)

        header_layout.addStretch()

        self.post_select_all = CyberCheckBox()
        self.post_select_all.stateChanged.connect(self._toggle_select_all_post)
        header_layout.addWidget(self.post_select_all)

        select_label = QLabel("Tất cả")
        select_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        header_layout.addWidget(select_label)

        left_layout.addWidget(header)

        # Stats
        self.post_stats = QLabel("Đã chọn: 0 / 0")
        self.post_stats.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 11px; padding: 8px 16px;")
        left_layout.addWidget(self.post_stats)

        # Filter
        filter_widget = QWidget()
        filter_widget.setStyleSheet(f"background: {COLORS['bg_darker']};")
        filter_layout = QHBoxLayout(filter_widget)
        filter_layout.setContentsMargins(12, 8, 12, 8)

        self.group_filter = CyberInput("🔍 Lọc nhóm...")
        self.group_filter.textChanged.connect(self._filter_post_groups)
        filter_layout.addWidget(self.group_filter)

        left_layout.addWidget(filter_widget)

        # Groups list scroll
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"""
            QScrollArea {{
                background: {COLORS['bg_darker']};
                border: none;
                border-radius: 0 0 14px 14px;
            }}
        """)

        self.post_groups_widget = QWidget()
        self.post_groups_widget.setStyleSheet(f"background: {COLORS['bg_darker']};")
        self.post_groups_layout = QVBoxLayout(self.post_groups_widget)
        self.post_groups_layout.setContentsMargins(8, 8, 8, 8)
        self.post_groups_layout.setSpacing(4)
        self.post_groups_layout.addStretch()

        scroll.setWidget(self.post_groups_widget)
        left_layout.addWidget(scroll, 1)

        layout.addWidget(left_card)

        # ========== RIGHT PANEL - Post Content ==========
        right_card = CyberCard(COLORS['neon_cyan'])
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(2, 2, 2, 2)

        # Header
        right_header = QWidget()
        right_header.setFixedHeight(44)
        right_header.setStyleSheet(f"background: {COLORS['bg_darker']}; border-radius: 14px 14px 0 0;")
        right_header_layout = QHBoxLayout(right_header)
        right_header_layout.setContentsMargins(16, 0, 16, 0)

        right_title = QLabel("📝 Nội dung đăng")
        right_title.setStyleSheet(f"color: {COLORS['neon_cyan']}; font-size: 13px; font-weight: bold;")
        right_header_layout.addWidget(right_title)

        right_layout.addWidget(right_header)

        # Content area (scrollable)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setStyleSheet(f"""
            QScrollArea {{
                background: {COLORS['bg_darker']};
                border: none;
            }}
        """)

        right_content = QWidget()
        right_content.setStyleSheet(f"background: {COLORS['bg_darker']};")
        content_layout = QVBoxLayout(right_content)
        content_layout.setContentsMargins(16, 16, 16, 16)
        content_layout.setSpacing(12)

        # Category selector
        cat_row = QHBoxLayout()
        cat_label = QLabel("Danh mục:")
        cat_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        cat_label.setFixedWidth(80)
        cat_row.addWidget(cat_label)

        self.cat_combo = CyberComboBox(["Mặc định"])
        self.cat_combo.currentIndexChanged.connect(self._on_category_change)
        cat_row.addWidget(self.cat_combo)

        self.random_content_cb = CyberCheckBox()
        self.random_content_cb.setChecked(True)
        cat_row.addWidget(self.random_content_cb)

        random_label = QLabel("Random nội dung")
        random_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        cat_row.addWidget(random_label)

        content_layout.addLayout(cat_row)

        # Content selector
        content_row = QHBoxLayout()
        content_label = QLabel("Tin đăng:")
        content_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        content_label.setFixedWidth(80)
        content_row.addWidget(content_label)

        self.content_combo = CyberComboBox(["-- Random từ danh mục --"])
        content_row.addWidget(self.content_combo)

        content_layout.addLayout(content_row)

        # Content preview
        preview_label = QLabel("Xem trước nội dung:")
        preview_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        content_layout.addWidget(preview_label)

        self.content_preview = QTextEdit()
        self.content_preview.setReadOnly(True)
        self.content_preview.setFixedHeight(100)
        self.content_preview.setStyleSheet(f"""
            QTextEdit {{
                background: {COLORS['bg_card']};
                color: {COLORS['text_muted']};
                border: 2px solid {COLORS['border']};
                border-radius: 8px;
                padding: 8px;
                font-size: 11px;
            }}
        """)
        content_layout.addWidget(self.content_preview)

        # Image section
        img_frame = QFrame()
        img_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['bg_card']};
                border: 2px solid {COLORS['border']};
                border-radius: 10px;
            }}
        """)
        img_layout = QVBoxLayout(img_frame)
        img_layout.setContentsMargins(12, 12, 12, 12)
        img_layout.setSpacing(8)

        img_header = QHBoxLayout()
        self.attach_img_cb = CyberCheckBox()
        img_header.addWidget(self.attach_img_cb)

        img_title = QLabel("Kèm hình ảnh")
        img_title.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 12px;")
        img_header.addWidget(img_title)
        img_header.addStretch()

        img_layout.addLayout(img_header)

        img_path_row = QHBoxLayout()
        self.img_folder_input = CyberInput("Đường dẫn thư mục ảnh...")
        self.img_folder_input.setEnabled(False)
        img_path_row.addWidget(self.img_folder_input)

        btn_browse_img = CyberButton("📂", "purple")
        btn_browse_img.setFixedWidth(40)
        btn_browse_img.clicked.connect(self._browse_image_folder)
        img_path_row.addWidget(btn_browse_img)

        img_layout.addLayout(img_path_row)

        self.img_count_label = QLabel("(Tổng: 0 ảnh)")
        self.img_count_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px;")
        img_layout.addWidget(self.img_count_label)

        content_layout.addWidget(img_frame)

        # Connect checkbox
        self.attach_img_cb.stateChanged.connect(
            lambda s: self.img_folder_input.setEnabled(s == Qt.Checked)
        )

        # Options
        options_frame = QFrame()
        options_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['bg_card']};
                border: 2px solid {COLORS['border']};
                border-radius: 10px;
            }}
        """)
        options_layout = QVBoxLayout(options_frame)
        options_layout.setContentsMargins(12, 12, 12, 12)
        options_layout.setSpacing(8)

        options_title = QLabel("⚙️ Tùy chọn đăng")
        options_title.setStyleSheet(f"color: {COLORS['neon_yellow']}; font-size: 12px; font-weight: bold;")
        options_layout.addWidget(options_title)

        delay_row = QHBoxLayout()
        delay_label = QLabel("Delay (giây):")
        delay_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 11px;")
        delay_label.setFixedWidth(80)
        delay_row.addWidget(delay_label)

        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(1, 60)
        self.delay_spin.setValue(5)
        self.delay_spin.setFixedWidth(60)
        self.delay_spin.setStyleSheet(f"""
            QSpinBox {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_primary']};
                border: 2px solid {COLORS['border']};
                border-radius: 6px;
                padding: 4px;
            }}
        """)
        delay_row.addWidget(self.delay_spin)

        self.random_delay_cb = CyberCheckBox()
        self.random_delay_cb.setChecked(True)
        delay_row.addWidget(self.random_delay_cb)

        random_delay_label = QLabel("Random (1-10s)")
        random_delay_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px;")
        delay_row.addWidget(random_delay_label)

        delay_row.addStretch()
        options_layout.addLayout(delay_row)

        content_layout.addWidget(options_frame)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_post = CyberButton("📤 Đăng tường", "success")
        self.btn_post.clicked.connect(self._start_posting)
        btn_row.addWidget(self.btn_post)

        self.btn_stop_post = CyberButton("⏹️ Dừng", "danger")
        self.btn_stop_post.clicked.connect(self._stop_posting)
        self.btn_stop_post.setEnabled(False)
        btn_row.addWidget(self.btn_stop_post)

        btn_row.addStretch()
        content_layout.addLayout(btn_row)

        # Progress
        self.post_progress = QProgressBar()
        self.post_progress.setFixedHeight(8)
        self.post_progress.setStyleSheet(f"""
            QProgressBar {{
                background: {COLORS['bg_card']};
                border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background: {COLORS['neon_mint']};
                border-radius: 4px;
            }}
        """)
        content_layout.addWidget(self.post_progress)

        self.post_status = QLabel("Tiến trình: 0 / 0")
        self.post_status.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        content_layout.addWidget(self.post_status)

        # Posted log
        log_label = QLabel("📜 Nhật ký đăng tường:")
        log_label.setStyleSheet(f"color: {COLORS['neon_yellow']}; font-size: 12px; font-weight: bold;")
        content_layout.addWidget(log_label)

        self.posted_log = QTextEdit()
        self.posted_log.setReadOnly(True)
        self.posted_log.setFixedHeight(120)
        self.posted_log.setStyleSheet(f"""
            QTextEdit {{
                background: {COLORS['bg_card']};
                color: {COLORS['text_muted']};
                border: 2px solid {COLORS['border']};
                border-radius: 8px;
                padding: 8px;
                font-size: 10px;
            }}
        """)
        content_layout.addWidget(self.posted_log)

        content_layout.addStretch()

        right_scroll.setWidget(right_content)
        right_layout.addWidget(right_scroll, 1)

        layout.addWidget(right_card, 1)

    def _create_boost_tab(self):
        """Tab Đẩy tin (bình luận)"""
        layout = QHBoxLayout(self.tab_boost)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # ========== LEFT PANEL - Posted URLs ==========
        left_card = CyberCard(COLORS['neon_coral'])
        left_card.setFixedWidth(400)
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(2, 2, 2, 2)

        # Header
        header = QWidget()
        header.setFixedHeight(44)
        header.setStyleSheet(f"background: {COLORS['bg_darker']}; border-radius: 14px 14px 0 0;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 0, 16, 0)

        header_title = QLabel("📋 Bài đã đăng")
        header_title.setStyleSheet(f"color: {COLORS['neon_coral']}; font-size: 13px; font-weight: bold;")
        header_layout.addWidget(header_title)

        header_layout.addStretch()

        btn_refresh_boost = CyberButton("🔄", "ghost")
        btn_refresh_boost.setFixedWidth(36)
        btn_refresh_boost.clicked.connect(self._load_boost_posts)
        header_layout.addWidget(btn_refresh_boost)

        left_layout.addWidget(header)

        # Filter row
        filter_widget = QWidget()
        filter_widget.setStyleSheet(f"background: {COLORS['bg_darker']};")
        filter_layout = QHBoxLayout(filter_widget)
        filter_layout.setContentsMargins(12, 8, 12, 8)

        filter_label = QLabel("Lọc:")
        filter_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        filter_layout.addWidget(filter_label)

        self.date_filter = CyberComboBox(["Hôm nay", "7 ngày", "30 ngày", "Tất cả"])
        self.date_filter.setFixedWidth(100)
        self.date_filter.currentIndexChanged.connect(self._on_date_filter_change)
        filter_layout.addWidget(self.date_filter)

        filter_layout.addStretch()

        self.boost_count = QLabel("0 bài")
        self.boost_count.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 11px;")
        filter_layout.addWidget(self.boost_count)

        left_layout.addWidget(filter_widget)

        # Select all
        select_row = QWidget()
        select_row.setStyleSheet(f"background: {COLORS['bg_darker']};")
        select_layout = QHBoxLayout(select_row)
        select_layout.setContentsMargins(12, 4, 12, 4)

        self.boost_select_all = CyberCheckBox()
        self.boost_select_all.stateChanged.connect(self._toggle_select_all_boost)
        select_layout.addWidget(self.boost_select_all)

        select_label = QLabel("Chọn tất cả")
        select_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        select_layout.addWidget(select_label)

        select_layout.addStretch()

        left_layout.addWidget(select_row)

        # Posts list scroll
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"""
            QScrollArea {{
                background: {COLORS['bg_darker']};
                border: none;
                border-radius: 0 0 14px 14px;
            }}
        """)

        self.boost_list_widget = QWidget()
        self.boost_list_widget.setStyleSheet(f"background: {COLORS['bg_darker']};")
        self.boost_list_layout = QVBoxLayout(self.boost_list_widget)
        self.boost_list_layout.setContentsMargins(8, 8, 8, 8)
        self.boost_list_layout.setSpacing(4)
        self.boost_list_layout.addStretch()

        scroll.setWidget(self.boost_list_widget)
        left_layout.addWidget(scroll, 1)

        layout.addWidget(left_card)

        # ========== RIGHT PANEL - Comment Content ==========
        right_card = CyberCard(COLORS['neon_mint'])
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(2, 2, 2, 2)

        # Header
        right_header = QWidget()
        right_header.setFixedHeight(44)
        right_header.setStyleSheet(f"background: {COLORS['bg_darker']}; border-radius: 14px 14px 0 0;")
        right_header_layout = QHBoxLayout(right_header)
        right_header_layout.setContentsMargins(16, 0, 16, 0)

        right_title = QLabel("💬 Nội dung bình luận")
        right_title.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 13px; font-weight: bold;")
        right_header_layout.addWidget(right_title)

        right_layout.addWidget(right_header)

        # Content
        right_content = QWidget()
        right_content.setStyleSheet(f"background: {COLORS['bg_darker']}; border-radius: 0 0 14px 14px;")
        content_layout = QVBoxLayout(right_content)
        content_layout.setContentsMargins(16, 16, 16, 16)
        content_layout.setSpacing(12)

        comment_label = QLabel("Nội dung comment (mỗi dòng 1 comment, sẽ random):")
        comment_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        content_layout.addWidget(comment_label)

        self.comment_text = QTextEdit()
        self.comment_text.setPlaceholderText("Hay quá!\nCảm ơn bạn!\nThông tin hữu ích!\nĐã lưu lại!")
        self.comment_text.setText("Hay quá!\nCảm ơn bạn!\nThông tin hữu ích!\nĐã lưu lại!")
        self.comment_text.setStyleSheet(f"""
            QTextEdit {{
                background: {COLORS['bg_card']};
                color: {COLORS['text_primary']};
                border: 2px solid {COLORS['border']};
                border-radius: 10px;
                padding: 12px;
                font-size: 12px;
            }}
            QTextEdit:focus {{
                border-color: {COLORS['neon_mint']};
            }}
        """)
        content_layout.addWidget(self.comment_text, 1)

        # Options
        options_row = QHBoxLayout()

        delay_label = QLabel("Delay (giây):")
        delay_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 11px;")
        options_row.addWidget(delay_label)

        self.comment_delay = QSpinBox()
        self.comment_delay.setRange(1, 30)
        self.comment_delay.setValue(3)
        self.comment_delay.setFixedWidth(60)
        self.comment_delay.setStyleSheet(f"""
            QSpinBox {{
                background: {COLORS['bg_card']};
                color: {COLORS['text_primary']};
                border: 2px solid {COLORS['border']};
                border-radius: 6px;
                padding: 4px;
            }}
        """)
        options_row.addWidget(self.comment_delay)

        self.random_comment_delay = CyberCheckBox()
        self.random_comment_delay.setChecked(True)
        options_row.addWidget(self.random_comment_delay)

        random_label = QLabel("Random (1-5s)")
        random_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px;")
        options_row.addWidget(random_label)

        options_row.addStretch()
        content_layout.addLayout(options_row)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_comment = CyberButton("💬 Bình luận", "success")
        self.btn_comment.clicked.connect(self._start_commenting)
        btn_row.addWidget(self.btn_comment)

        self.btn_stop_comment = CyberButton("⏹️ Dừng", "danger")
        self.btn_stop_comment.clicked.connect(self._stop_commenting)
        self.btn_stop_comment.setEnabled(False)
        btn_row.addWidget(self.btn_stop_comment)

        btn_row.addStretch()
        content_layout.addLayout(btn_row)

        # Progress
        self.comment_progress = QProgressBar()
        self.comment_progress.setFixedHeight(8)
        self.comment_progress.setStyleSheet(f"""
            QProgressBar {{
                background: {COLORS['bg_card']};
                border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background: {COLORS['neon_cyan']};
                border-radius: 4px;
            }}
        """)
        content_layout.addWidget(self.comment_progress)

        self.comment_status = QLabel("Tiến trình: 0 / 0")
        self.comment_status.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        content_layout.addWidget(self.comment_status)

        # Comment log
        log_label = QLabel("📜 Nhật ký bình luận:")
        log_label.setStyleSheet(f"color: {COLORS['neon_yellow']}; font-size: 12px; font-weight: bold;")
        content_layout.addWidget(log_label)

        self.comment_log = QTextEdit()
        self.comment_log.setReadOnly(True)
        self.comment_log.setStyleSheet(f"""
            QTextEdit {{
                background: {COLORS['bg_card']};
                color: {COLORS['text_muted']};
                border: 2px solid {COLORS['border']};
                border-radius: 8px;
                padding: 8px;
                font-size: 10px;
            }}
        """)
        content_layout.addWidget(self.comment_log, 1)

        right_layout.addWidget(right_content, 1)

        layout.addWidget(right_card, 1)

    # ============ DATA LOADING ============

    def _load_data(self):
        """Load profiles, folders, categories"""
        self.log("Đang tải dữ liệu...", "info")

        def fetch():
            try:
                folders = api.get_folders(limit=100)
                profiles = api.get_profiles(limit=500)
                categories = get_categories()
                print(f"[DEBUG] GroupsPage got {len(folders)} folders, {len(profiles)} profiles")
                return {"folders": folders, "profiles": profiles, "categories": categories}
            except Exception as e:
                print(f"[DEBUG] GroupsPage load error: {e}")
                return {"error": str(e)}

        def run():
            result = fetch()
            self.signal.data_loaded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_data_loaded(self, result):
        """Slot nhận data từ thread - chạy trên main thread"""
        if "error" in result:
            self.log(f"Lỗi: {result['error']}", "error")
            return

        self.folders = result.get("folders", [])
        self.profiles = result.get("profiles", [])
        self.categories = result.get("categories", [])

        print(f"[DEBUG] _on_data_loaded: {len(self.folders)} folders, {len(self.profiles)} profiles")

        # Update folder combo
        self.folder_combo.clear()
        self.folder_combo.addItem("📁 Tất cả")
        for f in self.folders:
            self.folder_combo.addItem(f"📁 {f.get('name', 'Unknown')}")

        # Update profile combo
        self._update_profile_combo()

        # Update category combo
        self.cat_combo.clear()
        for cat in self.categories:
            self.cat_combo.addItem(f"📁 {cat.get('name', 'Mặc định')}")

        # Update stats
        self.stat_profiles.set_value(str(len(self.profiles)))

        self.log(f"Đã tải {len(self.profiles)} profiles, {len(self.folders)} folders", "success")

    def _update_profile_combo(self):
        """Update profile combo based on folder filter"""
        self.profile_combo.clear()
        self.profile_combo.addItem("-- Chọn profile --")

        folder_idx = self.folder_combo.currentIndex()
        profiles_to_show = self.profiles

        if folder_idx > 0 and folder_idx - 1 < len(self.folders):
            folder_id = self.folders[folder_idx - 1].get('id')
            profiles_to_show = [p for p in self.profiles if p.get('folder_id') == folder_id]

        for p in profiles_to_show:
            name = p.get('name', 'Unknown')
            self.profile_combo.addItem(f"📱 {name[:30]}")

        self.profile_status.setText(f"{len(profiles_to_show)} profiles")

    def _on_folder_change(self, idx):
        self._update_profile_combo()

    def _on_profile_change(self, idx):
        """When profile selected, load groups"""
        if idx <= 0:
            return

        folder_idx = self.folder_combo.currentIndex()
        profiles_to_show = self.profiles

        if folder_idx > 0 and folder_idx - 1 < len(self.folders):
            folder_id = self.folders[folder_idx - 1].get('id')
            profiles_to_show = [p for p in self.profiles if p.get('folder_id') == folder_id]

        if idx - 1 < len(profiles_to_show):
            profile = profiles_to_show[idx - 1]
            uuid = profile.get('uuid')
            self.selected_profile_uuids = [uuid]
            self._load_groups_for_profile(uuid)

    def _load_groups_for_profile(self, uuid: str):
        """Load groups for selected profile from DB"""
        self.groups = get_groups_for_profiles([uuid])
        self._render_scan_groups()
        self._render_post_groups()
        self.stat_groups.set_value(str(len(self.groups)))
        self.scan_stats.setText(f"Tổng: {len(self.groups)} nhóm")

    def _on_groups_loaded(self, groups):
        self.groups = groups
        self._render_scan_groups()
        self._render_post_groups()

    # ============ SCAN TAB ============

    def _render_scan_groups(self):
        """Render groups in scan table"""
        self.scan_table.setRowCount(len(self.groups))
        self.scan_group_checkboxes.clear()

        for row, group in enumerate(self.groups):
            group_id = group.get('id', row)

            # Checkbox
            cb_widget = QWidget()
            cb_widget.setStyleSheet("background: transparent;")
            cb_layout = QHBoxLayout(cb_widget)
            cb_layout.setContentsMargins(0, 0, 0, 0)
            cb_layout.setAlignment(Qt.AlignCenter)
            checkbox = CyberCheckBox()
            cb_layout.addWidget(checkbox)
            self.scan_table.setCellWidget(row, 0, cb_widget)
            self.scan_group_checkboxes[group_id] = checkbox

            # ID
            self.scan_table.setItem(row, 1, QTableWidgetItem(str(row + 1)))

            # Name - hỗ trợ cả 'name' và 'group_name'
            name = group.get('group_name') or group.get('name', 'Unknown')
            self.scan_table.setItem(row, 2, QTableWidgetItem(name[:40]))

            # Group ID
            fb_id = group.get('group_id', '')
            self.scan_table.setItem(row, 3, QTableWidgetItem(str(fb_id)))

            # Members
            members = group.get('member_count') or group.get('members', 0)
            self.scan_table.setItem(row, 4, QTableWidgetItem(str(members)))

            # Date
            scan_date = group.get('scan_date', '') or group.get('created_at', '')
            if scan_date:
                scan_date = scan_date[:10]
            self.scan_table.setItem(row, 5, QTableWidgetItem(scan_date))

    def _scan_groups(self):
        """Start scanning groups - PARALLEL SCANNING với ThreadPoolExecutor"""
        if not self.selected_profile_uuids:
            QMessageBox.warning(self, "Thông báo", "Vui lòng chọn profile trước!")
            return

        if not BS4_AVAILABLE:
            QMessageBox.warning(self, "Lỗi", "Cần cài BeautifulSoup4: pip install beautifulsoup4")
            return

        self._is_scanning = True
        self._stop_requested = False
        self._scan_completed_count = 0
        self.btn_scan.setEnabled(False)
        self.btn_stop_scan.setEnabled(True)
        self.log("Bắt đầu quét nhóm song song...", "info")

        profiles_to_scan = list(self.selected_profile_uuids)

        def do_parallel_scan():
            """Quét song song nhiều profiles với ThreadPoolExecutor"""
            try:
                all_groups = []
                total = len(profiles_to_scan)

                def scan_single_profile(profile_uuid: str) -> List[Dict]:
                    """Quét 1 profile"""
                    try:
                        return self._execute_group_scan_for_profile(profile_uuid)
                    except Exception as e:
                        print(f"[ERROR] Scan profile {profile_uuid}: {e}")
                        return []

                # Chạy song song (max 3 browser cùng lúc để tránh quá tải)
                max_workers = min(total, 3)
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_uuid = {
                        executor.submit(scan_single_profile, uuid): uuid
                        for uuid in profiles_to_scan
                    }

                    for future in as_completed(future_to_uuid):
                        if self._stop_requested:
                            break

                        uuid = future_to_uuid[future]
                        try:
                            result = future.result()
                            all_groups.extend(result)

                            # Lưu vào profile_groups
                            self.profile_groups[uuid] = result

                            self._scan_completed_count += 1
                            self.signal.scan_progress.emit(self._scan_completed_count, total)

                            profile_name = next((p.get('name', 'Unknown')[:20] for p in self.profiles if p.get('uuid') == uuid), 'Unknown')
                            self.signal.log_message.emit(
                                f"[{self._scan_completed_count}/{total}] {profile_name}: {len(result)} nhóm",
                                "success"
                            )
                        except Exception as e:
                            print(f"[ERROR] Scan {uuid}: {e}")

                self.signal.scan_complete.emit()
                self.signal.log_message.emit(f"Quét xong! Tìm thấy {len(all_groups)} nhóm từ {total} profiles", "success")

            except Exception as e:
                import traceback
                traceback.print_exc()
                self.signal.log_message.emit(f"Lỗi quét: {str(e)}", "error")
                self.signal.scan_complete.emit()

        threading.Thread(target=do_parallel_scan, daemon=True).start()

    def _execute_group_scan_for_profile(self, profile_uuid: str):
        """Quét nhóm cho 1 profile - GIỐNG CODE GỐC"""
        groups_found = []
        slot_id = acquire_window_slot()

        try:
            # Bước 1: Mở browser
            self.signal.log_message.emit(f"Mở browser {profile_uuid[:8]}...", "info")
            result = api.open_browser(profile_uuid)
            print(f"[DEBUG] open_browser: {result}")

            status = result.get('status') or result.get('type')
            if status not in ['successfully', 'success', True]:
                if 'already' not in str(result).lower() and 'running' not in str(result).lower():
                    self.signal.log_message.emit(f"Không mở được browser", "error")
                    release_window_slot(slot_id)
                    return []

            # Lấy CDP port
            data = result.get('data', {})
            remote_port = data.get('remote_port')
            ws_url = data.get('web_socket', '')

            if not remote_port:
                match = re.search(r':(\d+)/', ws_url)
                if match:
                    remote_port = int(match.group(1))

            if not remote_port:
                self.signal.log_message.emit("Không lấy được CDP port", "error")
                release_window_slot(slot_id)
                return []

            cdp_base = f"http://127.0.0.1:{remote_port}"
            self.signal.log_message.emit(f"CDP port: {remote_port}", "info")
            time.sleep(2)

            # Bước 2: Lấy WebSocket
            try:
                resp = requests.get(f"{cdp_base}/json", timeout=10)
                tabs = resp.json()
            except Exception as e:
                self.signal.log_message.emit(f"Lỗi CDP: {e}", "error")
                release_window_slot(slot_id)
                return []

            page_ws = None
            for tab in tabs:
                if tab.get('type') == 'page':
                    page_ws = tab.get('webSocketDebuggerUrl')
                    break

            if not page_ws:
                release_window_slot(slot_id)
                return []

            # Bước 3: Kết nối WebSocket
            try:
                ws = websocket.create_connection(page_ws, timeout=30, suppress_origin=True)
            except:
                release_window_slot(slot_id)
                return []

            # Navigate đến trang nhóm
            groups_url = "https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added"
            self.signal.log_message.emit("Đang vào trang nhóm Facebook...", "info")

            ws.send(json_module.dumps({
                "id": 1,
                "method": "Page.navigate",
                "params": {"url": groups_url}
            }))
            ws.recv()
            time.sleep(8)

            # Bước 4: CUỘN TRANG - GIỐNG CODE GỐC (đơn giản)
            self.signal.log_message.emit("Đang cuộn trang...", "info")
            for i in range(10):
                ws.send(json_module.dumps({
                    "id": 100 + i,
                    "method": "Runtime.evaluate",
                    "params": {"expression": "window.scrollTo(0, document.body.scrollHeight);"}
                }))
                ws.recv()
                time.sleep(2)

            # Bước 5: Lấy HTML
            self.signal.log_message.emit("Đang quét danh sách nhóm...", "info")
            ws.send(json_module.dumps({
                "id": 200,
                "method": "Runtime.evaluate",
                "params": {"expression": "document.documentElement.outerHTML"}
            }))
            result = json_module.loads(ws.recv())
            html = result.get('result', {}).get('result', {}).get('value', '')

            ws.close()
            print(f"[Groups] Got HTML, length={len(html) if html else 0}")

            if not html:
                release_window_slot(slot_id)
                return []

            # Parse HTML - GIỐNG CODE GỐC
            soup = BeautifulSoup(html, 'html.parser')

            # Thử nhiều cách tìm links nhóm
            links = soup.find_all('a', {'aria-label': 'Xem nhóm'})
            print(f"[Groups] Found {len(links)} links with aria-label='Xem nhóm'")

            if not links:
                links = soup.find_all('a', {'aria-label': 'Visit group'})
                print(f"[Groups] Found {len(links)} links with aria-label='Visit group'")

            if not links:
                # Fallback: Tìm tất cả links có /groups/ trong href
                links = soup.find_all('a', href=re.compile(r'/groups/[^/]+/?$'))
                print(f"[Groups] Found {len(links)} links matching /groups/xxx pattern")

            for link in links:
                href = link.get('href', '')
                if '/groups/' in href:
                    match = re.search(r'/groups/([^/?]+)', href)
                    if match:
                        group_id = match.group(1)

                        if group_id in ['joins', 'feed', 'discover', 'create', 'settings']:
                            continue

                        group_name = group_id

                        # Tìm tên nhóm trong parent elements
                        parent = link
                        for _ in range(10):
                            parent = parent.find_parent()
                            if parent is None:
                                break
                            spans = parent.find_all(['span', 'div'], recursive=False)
                            for span in spans:
                                text = span.get_text(strip=True)
                                skip_texts = ['Xem nhóm', 'Visit group', 'View group', 'Tham gia', 'Join']
                                if text and len(text) > 3 and text not in skip_texts and not text.startswith('http'):
                                    if len(text) < 150:
                                        group_name = text
                                        break
                            if group_name != group_id:
                                break

                        group_url = f"https://www.facebook.com/groups/{group_id}/"

                        if not any(g['group_id'] == group_id for g in groups_found):
                            groups_found.append({
                                'group_id': group_id,
                                'group_name': group_name,
                                'group_url': group_url,
                                'member_count': 0,
                                'profile_uuid': profile_uuid
                            })

            # Lưu vào database - DÙNG sync_groups GIỐNG CODE GỐC
            print(f"[Groups] Profile {profile_uuid[:8]} found {len(groups_found)} groups")
            if groups_found:
                sync_groups(profile_uuid, groups_found)

            self.signal.log_message.emit(f"Tìm thấy {len(groups_found)} nhóm", "success")

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.signal.log_message.emit(f"Lỗi: {str(e)}", "error")
        finally:
            release_window_slot(slot_id)

        return groups_found

    def _stop_scan(self):
        self._stop_requested = True
        self.log("Đang dừng quét...", "warning")

    def _on_scan_progress(self, current, total):
        self.scan_progress.setValue(int(current / total * 100))

    def _on_scan_complete(self):
        self._is_scanning = False
        self.btn_scan.setEnabled(True)
        self.btn_stop_scan.setEnabled(False)
        self.scan_progress.setValue(100)

        # QUAN TRỌNG: Reload groups từ DB và render lại UI
        if self.selected_profile_uuids:
            self.groups = get_groups_for_profiles(self.selected_profile_uuids)
            self._render_scan_groups()
            self._render_post_groups()
            self.stat_groups.set_value(str(len(self.groups)))
            self.scan_stats.setText(f"Tổng: {len(self.groups)} nhóm")

        self.log("Quét nhóm hoàn tất!", "success")

    def _clear_all_groups(self):
        """Clear all groups"""
        reply = QMessageBox.question(
            self, "Xác nhận",
            "Xóa tất cả nhóm đã quét?",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            clear_groups()
            self.groups = []
            self._render_scan_groups()
            self._render_post_groups()
            self.stat_groups.set_value("0")
            self.log("Đã xóa tất cả nhóm", "success")

    # ============ POST TAB ============

    def _render_post_groups(self):
        """Render groups list in post tab"""
        # Clear old items
        while self.post_groups_layout.count() > 0:
            item = self.post_groups_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.post_group_checkboxes.clear()

        if not self.groups:
            empty_label = QLabel("Chưa có nhóm\nQuét nhóm trước")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px; padding: 40px;")
            self.post_groups_layout.addWidget(empty_label)
            self.post_groups_layout.addStretch()
            return

        for group in self.groups:
            group_id = group.get('id')
            name = group.get('group_name') or group.get('name', 'Unknown')

            row = QWidget()
            row.setFixedHeight(36)
            row.setStyleSheet(f"""
                QWidget {{
                    background: {COLORS['bg_card']};
                    border-radius: 6px;
                }}
                QWidget:hover {{
                    background: {COLORS['bg_hover']};
                }}
            """)

            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 4, 8, 4)
            row_layout.setSpacing(8)

            checkbox = CyberCheckBox()
            checkbox.stateChanged.connect(self._update_post_stats)
            row_layout.addWidget(checkbox)
            self.post_group_checkboxes[group_id] = checkbox

            name_label = QLabel(name[:35] + "..." if len(name) > 35 else name)
            name_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 11px;")
            row_layout.addWidget(name_label, 1)

            self.post_groups_layout.addWidget(row)

        self.post_groups_layout.addStretch()
        self._update_post_stats()

    def _filter_post_groups(self, text):
        """Filter post groups by text"""
        text = text.lower()
        for group in self.groups:
            group_id = group.get('id')
            name = group.get('name', '').lower()
            if group_id in self.post_group_checkboxes:
                cb = self.post_group_checkboxes[group_id]
                parent = cb.parent()
                if parent:
                    parent.setVisible(text in name or not text)

    def _toggle_select_all_post(self, state):
        checked = state == Qt.Checked
        for cb in self.post_group_checkboxes.values():
            cb.setChecked(checked)
        self._update_post_stats()

    def _update_post_stats(self):
        selected = sum(1 for cb in self.post_group_checkboxes.values() if cb.isChecked())
        total = len(self.post_group_checkboxes)
        self.post_stats.setText(f"Đã chọn: {selected} / {total}")
        self.stat_selected.set_value(str(selected))

    def _on_category_change(self, idx):
        """Load contents for selected category"""
        if idx < 0 or idx >= len(self.categories):
            return

        cat_id = self.categories[idx].get('id')
        self.contents = get_contents(cat_id)

        self.content_combo.clear()
        self.content_combo.addItem("-- Random từ danh mục --")
        for content in self.contents:
            title = content.get('title', 'Không tiêu đề')[:30]
            self.content_combo.addItem(f"📝 {title}")

    def _browse_image_folder(self):
        """Browse for image folder"""
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục ảnh")
        if folder:
            self.img_folder_input.setText(folder)
            # Count images
            count = 0
            for f in os.listdir(folder):
                if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                    count += 1
            self.img_count_label.setText(f"(Tổng: {count} ảnh)")

    def _start_posting(self):
        """Start posting to groups - HỖ TRỢ MULTI-PROFILE"""
        selected_groups = [g for g in self.groups if g.get('id') in self.post_group_checkboxes
                          and self.post_group_checkboxes[g.get('id')].isChecked()]

        if not selected_groups:
            QMessageBox.warning(self, "Thông báo", "Vui lòng chọn ít nhất 1 nhóm!")
            return

        if not self.selected_profile_uuids:
            QMessageBox.warning(self, "Thông báo", "Vui lòng chọn profile trước!")
            return

        # Lấy nội dung để đăng
        content_to_post = self._get_content_to_post()
        if not content_to_post:
            QMessageBox.warning(self, "Thông báo", "Vui lòng chọn nội dung để đăng!")
            return

        # Xây dựng posting tasks - hỗ trợ multi-profile
        posting_tasks = []
        selected_group_ids = set(g.get('id') for g in selected_groups)

        if self._multi_profile_mode and self.profile_groups:
            # Multi-profile mode: mỗi profile đăng vào groups của nó
            for profile_uuid in self.selected_profile_uuids:
                profile_all_groups = self.profile_groups.get(profile_uuid, [])
                profile_selected_groups = [g for g in profile_all_groups if g.get('id') in selected_group_ids]
                if profile_selected_groups:
                    posting_tasks.append((profile_uuid, profile_selected_groups))
        else:
            # Single profile mode
            posting_tasks.append((self.selected_profile_uuids[0], selected_groups))

        if not posting_tasks:
            QMessageBox.warning(self, "Thông báo", "Không có nhóm nào để đăng!")
            return

        total_groups = sum(len(groups) for _, groups in posting_tasks)
        self._is_posting = True
        self._stop_requested = False
        self.btn_post.setEnabled(False)
        self.btn_stop_post.setEnabled(True)
        self.log(f"Bắt đầu đăng vào {total_groups} nhóm từ {len(posting_tasks)} profile...", "info")

        def execute_all_posting():
            """Thực hiện posting cho tất cả profiles"""
            completed_count = 0

            for profile_uuid, groups in posting_tasks:
                if self._stop_requested:
                    break

                profile_name = next((p.get('name', 'Unknown')[:20] for p in self.profiles if p.get('uuid') == profile_uuid), 'Unknown')
                self.signal.log_message.emit(f"Đang đăng với profile: {profile_name}", "info")

                try:
                    self._execute_posting_for_profile(profile_uuid, groups, content_to_post, completed_count, total_groups)
                    completed_count += len(groups)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    self.signal.log_message.emit(f"Lỗi với profile {profile_name}: {e}", "error")

            self.signal.post_complete.emit()

        threading.Thread(target=execute_all_posting, daemon=True).start()

    def _type_like_human(self, ws, text: str, msg_id: list) -> bool:
        """Gõ từng ký tự như người thật với typo và pause. Returns True nếu thành công."""
        # Các ký tự hay bị gõ nhầm (adjacent keys)
        typo_map = {
            'a': ['s', 'q', 'z'], 'b': ['v', 'n', 'g'], 'c': ['x', 'v', 'd'],
            'd': ['s', 'f', 'e'], 'e': ['w', 'r', 'd'], 'f': ['d', 'g', 'r'],
            'g': ['f', 'h', 't'], 'h': ['g', 'j', 'y'], 'i': ['u', 'o', 'k'],
            'j': ['h', 'k', 'u'], 'k': ['j', 'l', 'i'], 'l': ['k', 'o', 'p'],
            'm': ['n', 'k'], 'n': ['b', 'm', 'h'], 'o': ['i', 'p', 'l'],
            'p': ['o', 'l'], 'q': ['w', 'a'], 'r': ['e', 't', 'f'],
            's': ['a', 'd', 'w'], 't': ['r', 'y', 'g'], 'u': ['y', 'i', 'j'],
            'v': ['c', 'b', 'f'], 'w': ['q', 'e', 's'], 'x': ['z', 'c', 's'],
            'y': ['t', 'u', 'h'], 'z': ['x', 'a']
        }

        def send_cdp(method, params=None):
            msg_id[0] += 1
            msg = {"id": msg_id[0], "method": method}
            if params:
                msg["params"] = params
            try:
                ws.send(json_module.dumps(msg))
                return json_module.loads(ws.recv())
            except:
                return {"ws_closed": True}

        # Chia text thành các đoạn (theo dòng hoặc câu)
        paragraphs = text.split('\n')

        for p_idx, paragraph in enumerate(paragraphs):
            if not paragraph.strip():
                # Gõ newline
                result = send_cdp("Input.insertText", {"text": "\n"})
                if result.get('ws_closed'):
                    return False
                time.sleep(random.uniform(0.3, 0.8))
                continue

            # Chia paragraph thành các câu
            sentences = paragraph.replace('. ', '.|').replace('! ', '!|').replace('? ', '?|').split('|')

            for s_idx, sentence in enumerate(sentences):
                for i, char in enumerate(sentence):
                    # Random typo (3% chance cho chữ thường)
                    if char.lower() in typo_map and random.random() < 0.03:
                        # Gõ sai
                        wrong_char = random.choice(typo_map[char.lower()])
                        result = send_cdp("Input.insertText", {"text": wrong_char})
                        if result.get('ws_closed'):
                            return False
                        time.sleep(random.uniform(0.05, 0.15))

                        # Nhận ra sai, dừng lại
                        time.sleep(random.uniform(0.2, 0.5))

                        # Xóa (Backspace)
                        result = send_cdp("Input.dispatchKeyEvent", {
                            "type": "keyDown",
                            "key": "Backspace",
                            "code": "Backspace"
                        })
                        if result.get('ws_closed'):
                            return False
                        result = send_cdp("Input.dispatchKeyEvent", {
                            "type": "keyUp",
                            "key": "Backspace",
                            "code": "Backspace"
                        })
                        if result.get('ws_closed'):
                            return False
                        time.sleep(random.uniform(0.1, 0.2))

                    # Gõ ký tự đúng
                    result = send_cdp("Input.insertText", {"text": char})
                    if result.get('ws_closed'):
                        return False

                    # Delay khác nhau tùy ký tự
                    if char in ' .,!?':
                        # Sau dấu câu chậm hơn
                        time.sleep(random.uniform(0.08, 0.2))
                    elif char.isupper():
                        # Chữ hoa chậm hơn (phải giữ Shift)
                        time.sleep(random.uniform(0.06, 0.15))
                    else:
                        # Chữ thường nhanh hơn
                        time.sleep(random.uniform(0.03, 0.1))

                # Pause giữa các câu
                if s_idx < len(sentences) - 1:
                    time.sleep(random.uniform(0.3, 0.8))

            # Gõ newline giữa các paragraph
            if p_idx < len(paragraphs) - 1:
                result = send_cdp("Input.insertText", {"text": "\n"})
                if result.get('ws_closed'):
                    return False
                # Pause lâu hơn giữa các đoạn
                time.sleep(random.uniform(0.5, 1.2))

        return True

    def _scroll_page(self, ws, direction: str, amount: int, msg_id: list):
        """Cuộn trang như người thật"""
        scroll_script = f"window.scrollBy(0, {amount if direction == 'down' else -amount});"
        msg_id[0] += 1
        ws.send(json_module.dumps({
            "id": msg_id[0],
            "method": "Runtime.evaluate",
            "params": {"expression": scroll_script}
        }))
        try:
            ws.recv()
        except:
            pass

    def _execute_posting_for_profile(self, profile_uuid: str, groups: List[Dict], content_to_post: str, completed_offset: int, total_groups: int):
        """Thực hiện posting cho 1 profile"""
        slot_id = acquire_window_slot()

        try:
            # Mở browser
            self.signal.log_message.emit(f"Mở browser...", "info")
            result = api.open_browser(profile_uuid)

            status = result.get('status') or result.get('type')
            if status not in ['successfully', 'success', True]:
                if 'already' not in str(result).lower():
                    raise Exception(f"Không mở được browser: {result}")

            # Lấy CDP port
            data = result.get('data', {})
            remote_port = data.get('remote_port')
            ws_url = data.get('web_socket', '')

            if not remote_port:
                match = re.search(r':(\d+)/', ws_url)
                if match:
                    remote_port = int(match.group(1))

            if not remote_port:
                raise Exception("Không lấy được CDP port")

            cdp_base = f"http://127.0.0.1:{remote_port}"
            time.sleep(2)

            # Lấy WebSocket
            resp = requests.get(f"{cdp_base}/json", timeout=10)
            tabs = resp.json()

            page_ws = None
            for tab in tabs:
                if tab.get('type') == 'page':
                    page_ws = tab.get('webSocketDebuggerUrl')
                    break

            if not page_ws:
                raise Exception("Không tìm thấy WebSocket")

            ws = websocket.create_connection(page_ws, timeout=30, suppress_origin=True)
            msg_id = [10]

            def send_cdp(method, params=None):
                msg_id[0] += 1
                msg = {"id": msg_id[0], "method": method}
                if params:
                    msg["params"] = params
                ws.send(json_module.dumps(msg))
                return json_module.loads(ws.recv())

            total = len(groups)

            for i, group in enumerate(groups):
                if self._stop_requested:
                    break

                group_name = group.get('group_name') or group.get('name', 'Unknown')
                group_id = group.get('group_id', '')
                self.signal.log_message.emit(f"[{completed_offset + i + 1}/{total_groups}] Đăng vào: {group_name[:30]}", "info")

                # Navigate đến nhóm
                group_url = f"https://www.facebook.com/groups/{group_id}"
                send_cdp("Page.navigate", {"url": group_url})
                time.sleep(random.uniform(4, 6))

                # Đợi page load
                for _ in range(10):
                    result = send_cdp("Runtime.evaluate", {"expression": "document.readyState"})
                    if result.get('result', {}).get('result', {}).get('value') == 'complete':
                        break
                    time.sleep(1)
                time.sleep(random.uniform(1, 2))

                # Scroll xuống một chút như người thật đọc trang
                if random.random() < 0.7:
                    self._scroll_page(ws, "down", random.randint(100, 300), msg_id)
                    time.sleep(random.uniform(0.5, 1.5))
                    self._scroll_page(ws, "up", random.randint(50, 150), msg_id)
                    time.sleep(random.uniform(0.3, 0.8))

                # Lấy nội dung random hoặc cố định
                if self.random_content_cb.isChecked() and self.contents:
                    content = random.choice(self.contents)
                    post_text = content.get('content', content_to_post)
                else:
                    post_text = content_to_post

                # Click vào ô "Bạn viết gì đi..." - PHÂN BIỆT COMPOSER TẠO BÀI vs Ô COMMENT
                click_composer_script = '''
                (function() {
                    const composerTexts = ['Bạn viết gì đi', 'Write something', 'bạn viết gì đi', 'write something'];

                    function hasComposerText(text) {
                        if (!text) return false;
                        let lowerText = text.toLowerCase();
                        for (let kw of composerTexts) {
                            if (lowerText.includes(kw.toLowerCase())) return true;
                        }
                        return false;
                    }

                    function isInsideArticle(el) {
                        let parent = el;
                        while (parent) {
                            if (parent.getAttribute && parent.getAttribute('role') === 'article') {
                                return true;
                            }
                            parent = parent.parentElement;
                        }
                        return false;
                    }

                    // Cách 1: Tìm [role="button"][tabindex="0"] với text composer
                    let btns = document.querySelectorAll('[role="button"][tabindex="0"]');
                    for (let btn of btns) {
                        let text = btn.innerText || '';
                        if (hasComposerText(text) && !isInsideArticle(btn)) {
                            let rect = btn.getBoundingClientRect();
                            if (rect.top > 0 && rect.top < 700 && rect.width > 50) {
                                btn.click();
                                return {x: rect.left + rect.width/2, y: rect.top + rect.height/2, clicked: true};
                            }
                        }
                    }

                    // Cách 2: Tìm tất cả [role="button"]
                    let allBtns = document.querySelectorAll('[role="button"]');
                    for (let btn of allBtns) {
                        let text = btn.innerText || '';
                        if (hasComposerText(text) && !isInsideArticle(btn)) {
                            let rect = btn.getBoundingClientRect();
                            if (rect.top > 0 && rect.top < 700) {
                                btn.click();
                                return {x: rect.left + rect.width/2, y: rect.top + rect.height/2, clicked: true};
                            }
                        }
                    }

                    // Cách 3: Tìm div[tabindex="0"]
                    let divs = document.querySelectorAll('div[tabindex="0"]');
                    for (let div of divs) {
                        let text = div.innerText || '';
                        if (hasComposerText(text) && !isInsideArticle(div)) {
                            let rect = div.getBoundingClientRect();
                            if (rect.top > 0 && rect.top < 700) {
                                div.click();
                                return {x: rect.left + rect.width/2, y: rect.top + rect.height/2, clicked: true};
                            }
                        }
                    }

                    // Cách 4: Tìm theo aria-label
                    let labeled = document.querySelectorAll('[aria-label*="Create a post"], [aria-label*="Tạo bài viết"], [aria-label*="Viết bài"]');
                    for (let el of labeled) {
                        if (!isInsideArticle(el)) {
                            let rect = el.getBoundingClientRect();
                            if (rect.top > 0 && rect.top < 700) {
                                el.click();
                                return {x: rect.left + rect.width/2, y: rect.top + rect.height/2, clicked: true};
                            }
                        }
                    }

                    return {clicked: false};
                })()
                '''
                result = send_cdp("Runtime.evaluate", {"expression": click_composer_script, "returnByValue": True})
                time.sleep(random.uniform(2, 3))

                # Đợi editor xuất hiện
                for _ in range(5):
                    check_result = send_cdp("Runtime.evaluate", {
                        "expression": "document.querySelector('[contenteditable=\"true\"][role=\"textbox\"]') !== null",
                        "returnByValue": True
                    })
                    if check_result.get('result', {}).get('result', {}).get('value'):
                        break
                    time.sleep(1)

                # Focus vào editor
                send_cdp("Runtime.evaluate", {
                    "expression": """
                    (function() {
                        var editor = document.querySelector('[contenteditable="true"][role="textbox"]');
                        if (editor) {
                            editor.focus();
                            editor.click();
                            return true;
                        }
                        return false;
                    })()
                    """
                })
                time.sleep(0.5)

                # Gõ nội dung như người thật
                use_human_typing = random.random() < 0.6  # 60% dùng human typing
                if use_human_typing and len(post_text) < 500:
                    self.signal.log_message.emit("Đang gõ nội dung...", "info")
                    self._type_like_human(ws, post_text, msg_id)
                else:
                    # Dùng insertText nhanh hơn cho text dài
                    type_script = f"""
                    (function() {{
                        var editor = document.querySelector('[contenteditable="true"][role="textbox"]');
                        if (editor) {{
                            editor.focus();
                            document.execCommand('insertText', false, {json_module.dumps(post_text)});
                            return 'typed';
                        }}
                        return 'no_editor';
                    }})()
                    """
                    send_cdp("Runtime.evaluate", {"expression": type_script})
                time.sleep(random.uniform(1, 2))

                # Click nút Đăng
                post_script = """
                (function() {
                    // Tìm nút Đăng trong dialog/popup
                    var btns = document.querySelectorAll('[aria-label*="Đăng"], [aria-label*="Post"], button, [role="button"]');
                    for (var btn of btns) {
                        var text = btn.textContent || btn.getAttribute('aria-label') || '';
                        var rect = btn.getBoundingClientRect();
                        // Nút Đăng thường ở góc dưới/phải của popup
                        if ((text.trim() === 'Đăng' || text.trim() === 'Post') && rect.width > 30) {
                            btn.click();
                            return 'posted';
                        }
                    }
                    // Fallback: tìm nút có text chứa Đăng/Post
                    for (var btn of btns) {
                        var text = btn.textContent || '';
                        if (text.includes('Đăng') || text.includes('Post')) {
                            btn.click();
                            return 'posted_fallback';
                        }
                    }
                    return 'no_button';
                })()
                """
                send_cdp("Runtime.evaluate", {"expression": post_script})
                time.sleep(random.uniform(2, 4))  # Đợi đăng xong
                self.signal.log_message.emit(f"✓ Đã đăng vào {group_name[:25]}", "success")

                # Lưu lịch sử
                save_post_history({
                    'profile_uuid': profile_uuid,
                    'group_id': group_id,
                    'group_name': group_name,
                    'content': post_text[:200],
                    'status': 'success',
                    'posted_at': time.strftime('%Y-%m-%d %H:%M:%S')
                })

                self.signal.post_progress.emit(completed_offset + i + 1, total_groups)

                # Delay giữa các nhóm
                if i < total - 1:
                    delay = random.randint(5, 15) if self.random_delay_cb.isChecked() else self.delay_spin.value()
                    self.signal.log_message.emit(f"Đợi {delay}s...", "info")
                    time.sleep(delay)

            ws.close()

        finally:
            release_window_slot(slot_id)

    def _get_content_to_post(self):
        """Lấy nội dung để đăng từ category/content đã chọn"""
        # Nếu có chọn content cụ thể
        content_idx = self.content_combo.currentIndex()
        if content_idx > 0 and content_idx - 1 < len(self.contents):
            return self.contents[content_idx - 1].get('content', '')

        # Nếu random từ danh mục
        if self.contents:
            return random.choice(self.contents).get('content', '')

        # Preview text
        preview_text = self.content_preview.toPlainText().strip()
        if preview_text:
            return preview_text

        return None

    def _stop_posting(self):
        self._stop_requested = True
        self.log("Đang dừng đăng...", "warning")

    def _on_post_progress(self, current, total):
        self.post_progress.setValue(int(current / total * 100))
        self.post_status.setText(f"Tiến trình: {current} / {total}")

    def _on_post_complete(self):
        self._is_posting = False
        self.btn_post.setEnabled(True)
        self.btn_stop_post.setEnabled(False)
        self.post_progress.setValue(100)
        self.log("Đăng tường hoàn tất!", "success")

    # ============ BOOST TAB ============

    def _load_boost_posts(self):
        """Load posted history for boost"""
        filter_idx = self.date_filter.currentIndex()

        if filter_idx == 0:  # Today
            days = 1
        elif filter_idx == 1:  # 7 days
            days = 7
        elif filter_idx == 2:  # 30 days
            days = 30
        else:  # All
            days = 9999

        self.boost_posts = get_post_history_filtered(days_back=days)
        self._render_boost_posts()

    def _on_date_filter_change(self, idx):
        self._load_boost_posts()

    def _render_boost_posts(self):
        """Render boost posts list"""
        # Clear old items
        while self.boost_list_layout.count() > 0:
            item = self.boost_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.boost_checkboxes.clear()

        if not self.boost_posts:
            empty_label = QLabel("Chưa có bài đăng nào\nĐăng bài ở tab Đăng nhóm trước")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px; padding: 40px;")
            self.boost_list_layout.addWidget(empty_label)
            self.boost_list_layout.addStretch()
            self.boost_count.setText("0 bài")
            return

        for post in self.boost_posts:
            post_id = post.get('id')
            group_name = post.get('group_name', 'Unknown')[:25]
            post_url = post.get('post_url', '')[:30]

            row = QWidget()
            row.setFixedHeight(44)
            row.setStyleSheet(f"""
                QWidget {{
                    background: {COLORS['bg_card']};
                    border-radius: 6px;
                }}
                QWidget:hover {{
                    background: {COLORS['bg_hover']};
                }}
            """)

            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 4, 8, 4)
            row_layout.setSpacing(8)

            checkbox = CyberCheckBox()
            row_layout.addWidget(checkbox)
            self.boost_checkboxes[post_id] = checkbox

            info_widget = QWidget()
            info_layout = QVBoxLayout(info_widget)
            info_layout.setContentsMargins(0, 0, 0, 0)
            info_layout.setSpacing(2)

            name_label = QLabel(group_name)
            name_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 11px;")
            info_layout.addWidget(name_label)

            url_label = QLabel(post_url + "..." if post_url else "—")
            url_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 9px;")
            info_layout.addWidget(url_label)

            row_layout.addWidget(info_widget, 1)

            self.boost_list_layout.addWidget(row)

        self.boost_list_layout.addStretch()
        self.boost_count.setText(f"{len(self.boost_posts)} bài")

    def _toggle_select_all_boost(self, state):
        checked = state == Qt.Checked
        for cb in self.boost_checkboxes.values():
            cb.setChecked(checked)

    def _start_commenting(self):
        """Start commenting on posts - MỞ BROWSER VÀ BÌNH LUẬN THẬT"""
        selected_posts = [p for p in self.boost_posts if p.get('id') in self.boost_checkboxes
                         and self.boost_checkboxes[p.get('id')].isChecked()]

        if not selected_posts:
            QMessageBox.warning(self, "Thông báo", "Vui lòng chọn ít nhất 1 bài!")
            return

        comments = self.comment_text.toPlainText().strip().split('\n')
        comments = [c.strip() for c in comments if c.strip()]

        if not comments:
            QMessageBox.warning(self, "Thông báo", "Vui lòng nhập nội dung bình luận!")
            return

        if not self.selected_profile_uuids:
            QMessageBox.warning(self, "Thông báo", "Vui lòng chọn profile trước!")
            return

        self._is_commenting = True
        self._stop_requested = False
        self.btn_comment.setEnabled(False)
        self.btn_stop_comment.setEnabled(True)
        self.log(f"Bắt đầu bình luận {len(selected_posts)} bài...", "info")

        def comment():
            try:
                profile_uuid = self.selected_profile_uuids[0]
                slot_id = acquire_window_slot()

                # Mở browser
                self.signal.log_message.emit("Mở browser...", "info")
                result = api.open_browser(profile_uuid)

                status = result.get('status') or result.get('type')
                if status not in ['successfully', 'success', True]:
                    if 'already' not in str(result).lower():
                        self.signal.log_message.emit("Không mở được browser", "error")
                        release_window_slot(slot_id)
                        self.signal.comment_complete.emit()
                        return

                # Lấy CDP port
                data = result.get('data', {})
                remote_port = data.get('remote_port')
                ws_url = data.get('web_socket', '')

                if not remote_port:
                    match = re.search(r':(\d+)/', ws_url)
                    if match:
                        remote_port = int(match.group(1))

                if not remote_port:
                    release_window_slot(slot_id)
                    self.signal.comment_complete.emit()
                    return

                cdp_base = f"http://127.0.0.1:{remote_port}"
                time.sleep(2)

                # Lấy WebSocket
                try:
                    resp = requests.get(f"{cdp_base}/json", timeout=10)
                    tabs = resp.json()
                except:
                    release_window_slot(slot_id)
                    self.signal.comment_complete.emit()
                    return

                page_ws = None
                for tab in tabs:
                    if tab.get('type') == 'page':
                        page_ws = tab.get('webSocketDebuggerUrl')
                        break

                if not page_ws:
                    release_window_slot(slot_id)
                    self.signal.comment_complete.emit()
                    return

                try:
                    ws = websocket.create_connection(page_ws, timeout=30, suppress_origin=True)
                except:
                    release_window_slot(slot_id)
                    self.signal.comment_complete.emit()
                    return

                total = len(selected_posts)
                msg_id = [10]

                def send_cdp(method, params=None):
                    msg_id[0] += 1
                    msg = {"id": msg_id[0], "method": method}
                    if params:
                        msg["params"] = params
                    ws.send(json_module.dumps(msg))
                    return json_module.loads(ws.recv())

                for i, post in enumerate(selected_posts):
                    if self._stop_requested:
                        break

                    group_name = post.get('group_name', 'Unknown')
                    post_url = post.get('post_url', '')
                    comment_text = random.choice(comments)

                    self.signal.log_message.emit(f"[{i+1}/{total}] Comment: {group_name}", "info")

                    if post_url and 'facebook.com' in post_url:
                        # TẠO TAB MỚI để tránh Leave site dialog
                        try:
                            result = send_cdp("Target.createTarget", {"url": post_url})
                            target_id = result.get('result', {}).get('targetId')

                            if not target_id:
                                self.signal.log_message.emit(f"Không tạo được tab mới", "warning")
                                continue

                            time.sleep(random.uniform(3, 5))

                            # Lấy WebSocket của tab mới
                            new_ws_url = None
                            try:
                                resp = requests.get(f"{cdp_base}/json", timeout=10)
                                pages = resp.json()
                                for p in pages:
                                    if p.get('id') == target_id:
                                        new_ws_url = p.get('webSocketDebuggerUrl')
                                        break
                            except:
                                pass

                            if not new_ws_url:
                                send_cdp("Target.closeTarget", {"targetId": target_id})
                                continue

                            # Kết nối WebSocket tab mới
                            new_ws = None
                            try:
                                new_ws = websocket.create_connection(new_ws_url, timeout=30, suppress_origin=True)
                            except:
                                try:
                                    new_ws = websocket.create_connection(new_ws_url, timeout=30)
                                except:
                                    send_cdp("Target.closeTarget", {"targetId": target_id})
                                    continue

                            new_msg_id = [10]

                            def send_new(method, params=None):
                                new_msg_id[0] += 1
                                msg = {"id": new_msg_id[0], "method": method, "params": params or {}}
                                new_ws.send(json_module.dumps(msg))
                                try:
                                    new_ws.settimeout(30)
                                    resp = new_ws.recv()
                                    return json_module.loads(resp)
                                except:
                                    return {}

                            def eval_new(expr):
                                result = send_new("Runtime.evaluate", {
                                    "expression": expr,
                                    "returnByValue": True,
                                    "awaitPromise": True
                                })
                                return result.get('result', {}).get('result', {}).get('value')

                            # Đợi page load
                            for _ in range(10):
                                ready = eval_new("document.readyState")
                                if ready == 'complete':
                                    break
                                time.sleep(1)

                            time.sleep(random.uniform(1, 2))

                            # Scroll xuống một chút để thấy comment box
                            eval_new("window.scrollBy(0, 300);")
                            time.sleep(random.uniform(0.5, 1))

                            # Tìm và click vào ô comment
                            click_comment_js = '''
                            (function() {
                                // Tìm ô "Viết bình luận..." hoặc "Write a comment..."
                                let placeholders = document.querySelectorAll('[contenteditable="true"]');
                                for (let el of placeholders) {
                                    let placeholder = el.getAttribute('aria-placeholder') || el.getAttribute('placeholder') || '';
                                    if (placeholder.includes('bình luận') || placeholder.includes('comment') ||
                                        placeholder.includes('Viết') || placeholder.includes('Write')) {
                                        el.focus();
                                        el.click();
                                        return true;
                                    }
                                }

                                // Fallback: tìm theo aria-label
                                let commentBox = document.querySelector('[aria-label*="bình luận"]');
                                if (!commentBox) commentBox = document.querySelector('[aria-label*="comment"]');
                                if (!commentBox) commentBox = document.querySelector('[aria-label*="Viết"]');
                                if (commentBox) {
                                    commentBox.focus();
                                    commentBox.click();
                                    return true;
                                }

                                // Fallback 2: click vào text "Viết bình luận"
                                let spans = document.querySelectorAll('span');
                                for (let span of spans) {
                                    if (span.innerText && (span.innerText.includes('Viết bình luận') ||
                                        span.innerText.includes('Write a comment'))) {
                                        span.click();
                                        return true;
                                    }
                                }
                                return false;
                            })()
                            '''
                            clicked = eval_new(click_comment_js)
                            if not clicked:
                                self.signal.log_message.emit(f"Không tìm thấy ô comment", "warning")
                                try:
                                    new_ws.close()
                                except:
                                    pass
                                send_cdp("Target.closeTarget", {"targetId": target_id})
                                continue

                            time.sleep(random.uniform(1, 2))

                            # Gõ comment từng ký tự như người thật
                            for char in comment_text:
                                # Random typo (3% chance)
                                if random.random() < 0.03:
                                    wrong_char = random.choice('abcdefghijklmnopqrstuvwxyz')
                                    send_new("Input.insertText", {"text": wrong_char})
                                    time.sleep(random.uniform(0.05, 0.15))
                                    send_new("Input.dispatchKeyEvent", {
                                        "type": "keyDown",
                                        "key": "Backspace",
                                        "code": "Backspace"
                                    })
                                    send_new("Input.dispatchKeyEvent", {
                                        "type": "keyUp",
                                        "key": "Backspace",
                                        "code": "Backspace"
                                    })
                                    time.sleep(random.uniform(0.1, 0.2))

                                send_new("Input.insertText", {"text": char})
                                time.sleep(random.uniform(0.03, 0.12))

                            time.sleep(random.uniform(0.5, 1))

                            # Nhấn Enter để gửi comment
                            send_new("Input.dispatchKeyEvent", {
                                "type": "keyDown",
                                "key": "Enter",
                                "code": "Enter"
                            })
                            time.sleep(0.1)
                            send_new("Input.dispatchKeyEvent", {
                                "type": "keyUp",
                                "key": "Enter",
                                "code": "Enter"
                            })

                            time.sleep(random.uniform(2, 3))

                            # Đóng tab mới
                            try:
                                new_ws.close()
                            except:
                                pass
                            send_cdp("Target.closeTarget", {"targetId": target_id})

                            self.signal.log_message.emit(f"✓ Đã comment: {comment_text[:30]}...", "success")

                        except Exception as e:
                            self.signal.log_message.emit(f"Lỗi comment: {str(e)[:30]}", "error")
                    else:
                        self.signal.log_message.emit(f"Bỏ qua: không có URL", "warning")

                    self.signal.comment_progress.emit(i + 1, total)

                    # Delay
                    if i < total - 1:
                        delay = random.randint(3, 8) if self.random_comment_delay.isChecked() else self.comment_delay.value()
                        time.sleep(delay)

                ws.close()
                release_window_slot(slot_id)
                self.signal.comment_complete.emit()

            except Exception as e:
                import traceback
                traceback.print_exc()
                self.signal.log_message.emit(f"Lỗi: {str(e)}", "error")
                self.signal.comment_complete.emit()

        threading.Thread(target=comment, daemon=True).start()

    def _stop_commenting(self):
        self._stop_requested = True
        self.log("Đang dừng bình luận...", "warning")

    def _on_comment_progress(self, current, total):
        self.comment_progress.setValue(int(current / total * 100))
        self.comment_status.setText(f"Tiến trình: {current} / {total}")

    def _on_comment_complete(self):
        self._is_commenting = False
        self.btn_comment.setEnabled(True)
        self.btn_stop_comment.setEnabled(False)
        self.comment_progress.setValue(100)
        self.log("Bình luận hoàn tất!", "success")
