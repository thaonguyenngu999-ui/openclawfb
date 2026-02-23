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
from PySide6.QtCore import Qt, QTimer, Signal, QObject, QSettings
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
    posted_log = Signal(str)  # posted URL log entry


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

        # Pagination for boost tab
        self._boost_page = 0
        self._boost_page_size = 30
        self._boost_total_count = 0

        # CDP tracking
        self._cdp_id = 0

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
        self.signal.posted_log.connect(self._on_posted_log)

        # Settings file
        self._settings = QSettings("FBManagerPro", "GroupsPage")
        
        self._setup_ui()
        QTimer.singleShot(500, self._load_data)
        QTimer.singleShot(600, self._load_settings)  # Load settings after UI ready

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # ========== TOP BAR ==========
        top_bar = QHBoxLayout()
        top_bar.setSpacing(12)

        title = CyberTitle("ĐĂNG NHÓM", "", "coral")
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
        self.cat_combo.currentIndexChanged.connect(lambda: self._save_settings())
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

        # Image count row
        img_count_row = QHBoxLayout()
        img_num_label = QLabel("Số ảnh random:")
        img_num_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 11px;")
        img_count_row.addWidget(img_num_label)

        self.img_count_spin = QSpinBox()
        self.img_count_spin.setRange(1, 10)
        self.img_count_spin.setValue(5)
        self.img_count_spin.setFixedWidth(60)
        self.img_count_spin.setStyleSheet(f"""
            QSpinBox {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_primary']};
                border: 2px solid {COLORS['border']};
                border-radius: 6px;
                padding: 4px;
            }}
        """)
        img_count_row.addWidget(self.img_count_spin)
        img_count_row.addStretch()

        img_layout.addLayout(img_count_row)

        content_layout.addWidget(img_frame)

        # Connect checkbox và auto-save
        self.attach_img_cb.stateChanged.connect(
            lambda s: self.img_folder_input.setEnabled(s == Qt.CheckState.Checked or s == 2)
        )
        self.attach_img_cb.stateChanged.connect(lambda: self._save_settings())
        self.img_folder_input.textChanged.connect(lambda: self._save_settings())
        self.img_count_spin.valueChanged.connect(lambda: self._save_settings())

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

        # Auto Like row
        like_row = QHBoxLayout()
        self.auto_like_cb = CyberCheckBox()
        like_row.addWidget(self.auto_like_cb)

        like_label = QLabel("Tự động thích bài")
        like_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 11px;")
        like_row.addWidget(like_label)

        like_type_label = QLabel("Loại:")
        like_type_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px; margin-left: 15px;")
        like_row.addWidget(like_type_label)

        self.react_type_combo = CyberComboBox(["👍 Like", "❤️ Yêu thích", "😆 Haha", "😮 Wow", "😢 Buồn", "😡 Phẫn nộ"])
        self.react_type_combo.setFixedWidth(130)
        like_row.addWidget(self.react_type_combo)
        like_row.addStretch()

        options_layout.addLayout(like_row)

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

        # Connect auto-save cho options
        self.delay_spin.valueChanged.connect(lambda: self._save_settings())
        self.random_delay_cb.stateChanged.connect(lambda: self._save_settings())
        self.random_content_cb.stateChanged.connect(lambda: self._save_settings())

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

        # Pagination
        pagination_widget = QWidget()
        pagination_widget.setFixedHeight(40)
        pagination_widget.setStyleSheet(f"background: {COLORS['bg_darker']}; border-radius: 0 0 14px 14px;")
        pagination_layout = QHBoxLayout(pagination_widget)
        pagination_layout.setContentsMargins(12, 4, 12, 4)

        self.btn_prev_page = CyberButton("< Trước", "ghost")
        self.btn_prev_page.setFixedWidth(80)
        self.btn_prev_page.clicked.connect(self._prev_boost_page)
        pagination_layout.addWidget(self.btn_prev_page)

        pagination_layout.addStretch()

        self.page_label = QLabel("Trang 1/1")
        self.page_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        pagination_layout.addWidget(self.page_label)

        pagination_layout.addStretch()

        self.btn_next_page = CyberButton("Sau >", "ghost")
        self.btn_next_page.setFixedWidth(80)
        self.btn_next_page.clicked.connect(self._next_boost_page)
        pagination_layout.addWidget(self.btn_next_page)

        left_layout.addWidget(pagination_widget)

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
        text = text.lower().strip()
        for group in self.groups:
            group_id = group.get('id')
            # Dùng cả group_name và name để filter
            name = (group.get('group_name') or group.get('name', '')).lower()
            if group_id in self.post_group_checkboxes:
                cb = self.post_group_checkboxes[group_id]
                parent = cb.parent()
                if parent:
                    parent.setVisible(text in name or not text)

    def _toggle_select_all_post(self, state):
        checked = state == Qt.CheckState.Checked or state == 2
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
            self._posting_port = remote_port  # Lưu để dùng cho tab mới
            time.sleep(2)

            # QUAN TRỌNG: Đóng hết tab cũ, giữ lại 1 tab về about:blank
            self._close_old_tabs_and_prepare(cdp_base, profile_uuid)
            time.sleep(1)

            # Lấy WebSocket với retry
            page_ws = None
            for attempt in range(5):
                try:
                    resp = requests.get(f"{cdp_base}/json", timeout=10)
                    pages = resp.json()
                    for p in pages:
                        if p.get('type') == 'page':
                            page_ws = p.get('webSocketDebuggerUrl')
                            break
                    if page_ws:
                        break
                except Exception as e:
                    print(f"[WARN] CDP attempt {attempt + 1}/5 failed: {e}")
                    if attempt == 2:
                        # Browser có thể đã đóng, thử mở lại
                        print(f"[INFO] Browser có thể đã đóng, thử mở lại...")
                        result = api.open_browser(profile_uuid)
                        if result.get('type') != 'error':
                            data = result.get('data', {})
                            new_port = data.get('remote_port')
                            if new_port:
                                remote_port = new_port
                                cdp_base = f"http://127.0.0.1:{remote_port}"
                                self._posting_port = remote_port
                                print(f"[INFO] Đã mở lại browser, port: {remote_port}")
                    time.sleep(1)

            if not page_ws:
                raise Exception("Không tìm thấy WebSocket")

            # Kết nối WebSocket
            ws = None
            try:
                ws = websocket.create_connection(page_ws, timeout=30, suppress_origin=True)
            except:
                try:
                    ws = websocket.create_connection(page_ws, timeout=30, origin=f"http://127.0.0.1:{remote_port}")
                except:
                    try:
                        ws = websocket.create_connection(page_ws, timeout=30)
                    except Exception as e:
                        raise Exception(f"WebSocket error: {e}")

            if not ws:
                raise Exception("Không kết nối được WebSocket")

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

                # QUAN TRỌNG: Kiểm tra và reconnect WS nếu cần
                ws, ws_ok = self._get_or_create_ws(ws, cdp_base, "about:blank")
                if not ws_ok:
                    self.signal.log_message.emit(f"Lỗi WebSocket, bỏ qua nhóm này", "error")
                    continue

                # Lưu ID tab cũ để đóng sau
                old_target_id = None
                try:
                    resp = requests.get(f"{cdp_base}/json", timeout=10)
                    pages = resp.json()
                    for p in pages:
                        if p.get('type') == 'page':
                            old_target_id = p.get('id')
                            break
                except:
                    pass

                # Tạo tab MỚI với group URL (tránh leave site dialog)
                group_url = f"https://www.facebook.com/groups/{group_id}"
                target_id = None
                new_ws = None

                for attempt in range(3):
                    try:
                        # Thử tạo tab mới qua CDP
                        msg_id[0] += 1
                        ws.send(json_module.dumps({
                            "id": msg_id[0],
                            "method": "Target.createTarget",
                            "params": {"url": group_url}
                        }))
                        result = json_module.loads(ws.recv())
                        target_id = result.get('result', {}).get('targetId')

                        if target_id:
                            time.sleep(random.uniform(2, 3))

                            # Lấy WebSocket của tab mới
                            new_ws_url = None
                            resp = requests.get(f"{cdp_base}/json", timeout=10)
                            pages = resp.json()
                            for p in pages:
                                if p.get('id') == target_id:
                                    new_ws_url = p.get('webSocketDebuggerUrl')
                                    break
                            # Fallback: tìm theo URL
                            if not new_ws_url:
                                for p in pages:
                                    if p.get('type') == 'page' and group_id in p.get('url', '') and p.get('id') != old_target_id:
                                        new_ws_url = p.get('webSocketDebuggerUrl')
                                        target_id = p.get('id')
                                        break

                            if new_ws_url:
                                try:
                                    new_ws = websocket.create_connection(new_ws_url, timeout=30, suppress_origin=True)
                                except:
                                    new_ws = websocket.create_connection(new_ws_url, timeout=30)

                                if new_ws:
                                    print(f"[INFO] Tạo tab mới thành công (attempt {attempt + 1})")
                                    break
                    except Exception as e:
                        print(f"[WARN] Attempt {attempt + 1}/3 tạo tab thất bại: {e}")
                        time.sleep(1)

                # Nếu không tạo được tab mới, thử FALLBACK: navigate trên tab hiện tại
                if not new_ws:
                    print(f"[WARN] Không tạo được tab mới, thử navigate trên tab hiện tại...")
                    try:
                        send_cdp("Page.handleJavaScriptDialog", {"accept": True})
                    except:
                        pass
                    try:
                        send_cdp("Page.navigate", {"url": group_url})
                        time.sleep(random.uniform(3, 4))
                        new_ws = ws
                        target_id = old_target_id
                        old_target_id = None  # Không đóng tab vì đang dùng
                        print(f"[INFO] Fallback navigate thành công")
                    except Exception as e:
                        self.signal.log_message.emit(f"Lỗi navigate: {e}", "error")
                        continue

                # Đóng WebSocket cũ TRƯỚC (nếu đã tạo tab mới)
                if new_ws != ws:
                    try:
                        ws.close()
                    except:
                        pass

                # Đóng tab CŨ bằng ID đã lưu
                if old_target_id and old_target_id != target_id:
                    try:
                        requests.get(f"{cdp_base}/json/close/{old_target_id}", timeout=5)
                        print(f"[INFO] Đã đóng tab cũ")
                    except:
                        pass

                # Từ giờ dùng new_ws
                ws = new_ws
                time.sleep(random.uniform(1, 2))

                # Đợi page load xong với retry
                page_loaded = False
                for _ in range(15):
                    try:
                        msg_id[0] += 1
                        ws.send(json_module.dumps({
                            "id": msg_id[0],
                            "method": "Runtime.evaluate",
                            "params": {"expression": "document.readyState", "returnByValue": True}
                        }))
                        result = json_module.loads(ws.recv())
                        if result.get('result', {}).get('result', {}).get('value') == 'complete':
                            page_loaded = True
                            break
                    except:
                        ws, _ = self._get_or_create_ws(ws, cdp_base, group_url)
                    time.sleep(1)

                if not page_loaded:
                    print(f"[WARN] Page không load được, thử tiếp...")

                time.sleep(random.uniform(1, 2))

                # Cập nhật send_cdp để dùng ws mới
                def send_cdp(method, params=None):
                    msg_id[0] += 1
                    msg = {"id": msg_id[0], "method": method}
                    if params:
                        msg["params"] = params
                    ws.send(json_module.dumps(msg))
                    return json_module.loads(ws.recv())

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

                # ===== BƯỚC 2: Click vào "Bạn viết gì đi..." với mouse movement =====
                # Tìm vị trí composer button với RETRY - PHÂN BIỆT COMPOSER TẠO BÀI vs Ô COMMENT
                get_composer_pos_js = '''
                (function() {
                    const composerTexts = ['Bạn viết gì đi', 'Write something', 'bạn viết gì đi', 'write something'];
                    const mainComposerIndicators = [
                        'Bài viết ẩn danh', 'Anonymous post',
                        'Thăm dò ý kiến', 'Poll',
                        'Cảm xúc/hoạt động', 'Feeling/activity',
                        'Trang cá nhân', 'Ảnh/video', 'Photo/video'
                    ];
                    
                    // Loại trừ comment box
                    const commentKeywords = [
                        'viết bình luận', 'write a comment', 'viết phản hồi',
                        'write a reply', 'bình luận công khai', 'public comment'
                    ];

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
                    
                    function isCommentButton(el) {
                        let text = (el.innerText || '').toLowerCase();
                        let ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
                        let combined = text + ' ' + ariaLabel;
                        for (let kw of commentKeywords) {
                            if (combined.includes(kw)) return true;
                        }
                        return false;
                    }

                    function hasComposerText(text) {
                        if (!text) return false;
                        let lowerText = text.toLowerCase();
                        for (let kw of composerTexts) {
                            if (lowerText.includes(kw.toLowerCase())) return true;
                        }
                        return false;
                    }

                    function isMainComposer(el) {
                        let container = el;
                        for (let i = 0; i < 8; i++) {
                            if (!container.parentElement) break;
                            container = container.parentElement;
                            let text = container.innerText || '';
                            for (let indicator of mainComposerIndicators) {
                                if (text.includes(indicator)) {
                                    return true;
                                }
                            }
                        }
                        return false;
                    }

                    function getCoords(el) {
                        let rect = el.getBoundingClientRect();
                        if (rect.top > 0 && rect.top < 700 && rect.width > 50 && rect.height > 10) {
                            return {
                                x: rect.left + rect.width / 2 + (Math.random() * 20 - 10),
                                y: rect.top + rect.height / 2 + (Math.random() * 6 - 3),
                                top: rect.top
                            };
                        }
                        return null;
                    }

                    let candidates = [];

                    // Cách 1: Tìm [role="button"][tabindex="0"] với text composer
                    let btns = document.querySelectorAll('[role="button"][tabindex="0"]');
                    for (let btn of btns) {
                        let text = btn.innerText || '';
                        if (hasComposerText(text) && !isInsideArticle(btn) && !isCommentButton(btn)) {
                            if (isMainComposer(btn)) {
                                let coords = getCoords(btn);
                                if (coords) {
                                    candidates.push({...coords, method: 'role+tabindex', priority: 1});
                                }
                            }
                        }
                    }

                    // Cách 2: Tìm tất cả [role="button"]
                    let allBtns = document.querySelectorAll('[role="button"]');
                    for (let btn of allBtns) {
                        let text = btn.innerText || '';
                        if (hasComposerText(text) && !isInsideArticle(btn) && !isCommentButton(btn) && isMainComposer(btn)) {
                            let coords = getCoords(btn);
                            if (coords) {
                                let isDup = candidates.some(c => Math.abs(c.top - coords.top) < 10);
                                if (!isDup) {
                                    candidates.push({...coords, method: 'role=button', priority: 2});
                                }
                            }
                        }
                    }

                    // Cách 3: Tìm div[tabindex="0"]
                    let divs = document.querySelectorAll('div[tabindex="0"]');
                    for (let div of divs) {
                        let text = div.innerText || '';
                        if (hasComposerText(text) && !isInsideArticle(div) && !isCommentButton(div)) {
                            let coords = getCoords(div);
                            if (coords) {
                                let isDup = candidates.some(c => Math.abs(c.top - coords.top) < 10);
                                if (!isDup) {
                                    candidates.push({...coords, method: 'div+tabindex', priority: 3});
                                }
                            }
                        }
                    }

                    // Cách 4: Tìm span chứa text rồi lên parent clickable
                    let spans = document.querySelectorAll('span');
                    for (let span of spans) {
                        let text = span.innerText || '';
                        if (hasComposerText(text)) {
                            let parent = span.closest('[role="button"], [tabindex="0"]');
                            if (parent && !isInsideArticle(parent) && !isCommentButton(parent) && isMainComposer(parent)) {
                                let coords = getCoords(parent);
                                if (coords) {
                                    let isDup = candidates.some(c => Math.abs(c.top - coords.top) < 10);
                                    if (!isDup) {
                                        candidates.push({...coords, method: 'span->parent', priority: 4});
                                    }
                                }
                            }
                        }
                    }

                    // Cách 5: Tìm theo aria-label
                    let labeled = document.querySelectorAll('[aria-label*="Create a post"], [aria-label*="Tạo bài viết"], [aria-label*="Viết bài"]');
                    for (let el of labeled) {
                        if (!isInsideArticle(el) && !isCommentButton(el)) {
                            let coords = getCoords(el);
                            if (coords) {
                                let isDup = candidates.some(c => Math.abs(c.top - coords.top) < 10);
                                if (!isDup) {
                                    candidates.push({...coords, method: 'aria-label', priority: 5});
                                }
                            }
                        }
                    }

                    if (candidates.length === 0) {
                        return null;
                    }

                    // Sắp xếp theo priority và top
                    candidates.sort((a, b) => {
                        if (a.priority !== b.priority) return a.priority - b.priority;
                        return a.top - b.top;
                    });

                    let best = candidates[0];
                    return {x: best.x, y: best.y};
                })()
                '''

                composer_pos = None
                for attempt in range(3):
                    composer_pos = self._cdp_evaluate(ws, get_composer_pos_js)
                    if composer_pos:
                        break
                    print(f"[WARN] Không tìm thấy composer (attempt {attempt + 1}/3), thử scroll và reload...")
                    self._cdp_evaluate(ws, "window.scrollTo(0, 0)")
                    time.sleep(1)
                    if attempt == 1:
                        self._cdp_send(ws, "Page.reload", {})
                        time.sleep(random.uniform(3, 4))

                if not composer_pos:
                    print(f"[WARN] Không tìm thấy nút tạo bài trong group {group_id} sau 3 lần thử")
                    return (False, "", ws)

                # Di chuyển chuột đến composer và click
                self._move_mouse_human(ws, int(composer_pos['x']), int(composer_pos['y']))
                time.sleep(random.uniform(0.1, 0.3))

                # Click với mouse events
                self._cdp_send(ws, "Input.dispatchMouseEvent", {
                    "type": "mousePressed",
                    "x": int(composer_pos['x']),
                    "y": int(composer_pos['y']),
                    "button": "left",
                    "clickCount": 1
                })
                time.sleep(random.uniform(0.05, 0.12))
                self._cdp_send(ws, "Input.dispatchMouseEvent", {
                    "type": "mouseReleased",
                    "x": int(composer_pos['x']),
                    "y": int(composer_pos['y']),
                    "button": "left",
                    "clickCount": 1
                })

                time.sleep(random.uniform(2, 3))  # Đợi popup mở

                # ===== BƯỚC 3: Focus vào textarea với RETRY =====
                # Logic chặt chẽ: PHẢI là main composer, KHÔNG PHẢI comment box
                focus_textarea_js = '''
                (function() {
                    // Các từ khóa của COMMENT BOX (cần loại trừ)
                    const commentKeywords = [
                        'viết bình luận', 'write a comment', 'viết phản hồi', 
                        'write a reply', 'bình luận', 'comment', 'reply',
                        'trả lời', 'phản hồi'
                    ];
                    
                    // Các từ khóa của MAIN COMPOSER (cần tìm)
                    const composerKeywords = [
                        'nghĩ gì', 'on your mind', 'viết gì đi', 'write something',
                        'tạo bài viết', 'create a post', 'create post'
                    ];
                    
                    function isInsideArticle(el) {
                        let parent = el;
                        while (parent) {
                            if (parent.getAttribute && parent.getAttribute('role') === 'article') {
                                return true;
                            }
                            // Kiểm tra thêm: nếu nằm trong phần comments
                            if (parent.getAttribute && parent.getAttribute('aria-label') && 
                                parent.getAttribute('aria-label').toLowerCase().includes('comment')) {
                                return true;
                            }
                            parent = parent.parentElement;
                        }
                        return false;
                    }
                    
                    function isCommentBox(el) {
                        let ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
                        let placeholder = (el.getAttribute('placeholder') || '').toLowerCase();
                        let dataPlaceholder = (el.getAttribute('data-placeholder') || '').toLowerCase();
                        let textToCheck = ariaLabel + ' ' + placeholder + ' ' + dataPlaceholder;
                        
                        for (let kw of commentKeywords) {
                            if (textToCheck.includes(kw)) {
                                return true;
                            }
                        }
                        return false;
                    }
                    
                    function isMainComposer(el) {
                        let ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
                        let placeholder = (el.getAttribute('placeholder') || '').toLowerCase();
                        let dataPlaceholder = (el.getAttribute('data-placeholder') || '').toLowerCase();
                        let textToCheck = ariaLabel + ' ' + placeholder + ' ' + dataPlaceholder;
                        
                        // Nếu không có label => có thể là composer chính (popup)
                        if (!ariaLabel && !placeholder && !dataPlaceholder) {
                            return true;
                        }
                        
                        for (let kw of composerKeywords) {
                            if (textToCheck.includes(kw)) {
                                return true;
                            }
                        }
                        return false;
                    }
                    
                    let editors = document.querySelectorAll('[contenteditable="true"]');
                    let candidates = [];
                    
                    for (let i = 0; i < editors.length; i++) {
                        let ed = editors[i];
                        let rect = ed.getBoundingClientRect();
                        
                        // Bỏ qua nếu không visible hoặc quá nhỏ
                        if (rect.width < 100 || rect.height < 20) continue;
                        
                        // Bỏ qua nếu nằm trong article (bài viết cũ)
                        if (isInsideArticle(ed)) {
                            console.log('[SKIP] Editor inside article');
                            continue;
                        }
                        
                        // Bỏ qua nếu là comment box
                        if (isCommentBox(ed)) {
                            console.log('[SKIP] Editor is comment box');
                            continue;
                        }
                        
                        // Ưu tiên nếu là main composer
                        let priority = isMainComposer(ed) ? 1 : 2;
                        
                        candidates.push({
                            editor: ed,
                            priority: priority,
                            top: rect.top
                        });
                    }
                    
                    if (candidates.length === 0) {
                        console.log('[ERROR] No valid editor found');
                        return false;
                    }
                    
                    // Sắp xếp: priority thấp hơn (main composer) trước
                    candidates.sort((a, b) => {
                        if (a.priority !== b.priority) return a.priority - b.priority;
                        return a.top - b.top;
                    });
                    
                    let best = candidates[0];
                    console.log('[FOCUS] Focusing best editor, priority=' + best.priority);
                    best.editor.focus();
                    return true;
                })()
                '''

                focused = False
                for attempt in range(3):
                    focused = self._cdp_evaluate(ws, focus_textarea_js)
                    if focused:
                        break
                    print(f"[WARN] Không focus được textarea (attempt {attempt + 1}/3), thử click lại composer...")
                    if composer_pos:
                        self._cdp_send(ws, "Input.dispatchMouseEvent", {
                            "type": "mousePressed", "x": int(composer_pos['x']), "y": int(composer_pos['y']),
                            "button": "left", "clickCount": 1
                        })
                        self._cdp_send(ws, "Input.dispatchMouseEvent", {
                            "type": "mouseReleased", "x": int(composer_pos['x']), "y": int(composer_pos['y']),
                            "button": "left"
                        })
                        time.sleep(random.uniform(1, 2))

                if not focused:
                    print(f"[WARN] Không focus được textarea trong group {group_id} sau 3 lần thử")
                    return (False, "", ws)

                time.sleep(random.uniform(0.5, 1))

                # ===== BƯỚC 4: Gõ nội dung từng ký tự =====
                typing_success = self._type_like_human(ws, post_text, msg_id)
                if not typing_success:
                    print(f"[ERROR] WebSocket đóng trong khi gõ nội dung cho group {group_id}")
                    cdp_base = f"http://127.0.0.1:{self._thread_local.posting_port}"
                    ws, reconnected = self._get_or_create_ws(ws, cdp_base, group_url)
                    if not reconnected:
                        print(f"[ERROR] Không thể reconnect, browser có thể đã đóng")
                        return (False, "", None)
                    return (False, "", ws)
                time.sleep(random.uniform(1, 2))

                # ===== BƯỚC 5: Upload ảnh nếu có =====
                images = []
                
                # Ưu tiên ảnh từ schedule (nếu có)
                if hasattr(self, '_schedule_images') and self._schedule_images:
                    images = self._schedule_images
                    self._schedule_images = []  # Reset sau khi dùng
                    self.signal.log_message.emit(f"[IMG] Dùng {len(images)} ảnh từ schedule", "info")
                else:
                    # Lấy từ UI như bình thường
                    has_attach_cb = hasattr(self, 'attach_img_cb')
                    is_checked = self.attach_img_cb.isChecked() if has_attach_cb else False
                    img_folder = self.img_folder_input.text().strip() if hasattr(self, 'img_folder_input') else ""
                    self.signal.log_message.emit(f"[IMG] Check: cb={is_checked}, folder='{img_folder[:30] if img_folder else 'N/A'}'", "info")
                    
                    if is_checked and img_folder:
                        img_count = self.img_count_spin.value() if hasattr(self, 'img_count_spin') else 5
                        images = self._get_random_images(img_folder, img_count)
                        self.signal.log_message.emit(f"[IMG] Found {len(images)} images in folder", "info")

                if images:
                    self.signal.log_message.emit(f"Đang upload {len(images)} ảnh...", "info")
                    
                    # Dùng absolute path - giữ nguyên backslash cho Windows CDP
                    images_normalized = [os.path.abspath(img_path) for img_path in images]
                    
                    self.signal.log_message.emit(f"[IMG] Files: {[os.path.basename(p) for p in images_normalized]}", "info")
                    
                    # Bật chế độ intercept file chooser để ngăn dialog mở
                    self._cdp_send(ws, "Page.setInterceptFileChooserDialog", {"enabled": True})
                    
                    # Get document để tìm input[type="file"]
                    doc_result = self._cdp_send(ws, "DOM.getDocument", {})
                    root_id = doc_result.get('result', {}).get('root', {}).get('nodeId', 0)

                    uploaded = False
                    if root_id:
                        # Tìm tất cả input[type="file"]
                        query_result = self._cdp_send(ws, "DOM.querySelectorAll", {
                            "nodeId": root_id,
                            "selector": 'input[type="file"]'
                        })
                        node_ids = query_result.get('result', {}).get('nodeIds', [])
                        self.signal.log_message.emit(f"[IMG] Found {len(node_ids)} file inputs", "info")

                        # Thử từng input cho đến khi upload được
                        for node_id in node_ids:
                            if uploaded:
                                break
                            if not node_id or node_id == 0:
                                continue
                            try:
                                # Set tất cả files một lần - KHÔNG mở dialog
                                set_result = self._cdp_send(ws, "DOM.setFileInputFiles", {
                                    "nodeId": node_id,
                                    "files": images_normalized
                                })
                                error_msg = set_result.get('error', {}).get('message', '')
                                if error_msg:
                                    self.signal.log_message.emit(f"[IMG] Node {node_id} error: {error_msg}", "warning")
                                    continue
                                    
                                self.signal.log_message.emit(f"[IMG] Set files to node {node_id}", "info")
                                time.sleep(2)  # Đợi upload

                                # Kiểm tra xem có preview ảnh không
                                preview_count = self._cdp_evaluate(ws, '''
                                (function() {
                                    let imgs = document.querySelectorAll('img[src*="blob:"]');
                                    return imgs.length;
                                })()
                                ''')
                                self.signal.log_message.emit(f"[IMG] Preview images: {preview_count}", "info")
                                
                                if preview_count and preview_count > 0:
                                    uploaded = True
                                    self.signal.log_message.emit(f"✓ Đã upload {len(images)} ảnh thành công!", "success")
                            except Exception as e:
                                self.signal.log_message.emit(f"[IMG] Node {node_id} exception: {e}", "warning")
                                continue
                    
                    # Tắt intercept
                    self._cdp_send(ws, "Page.setInterceptFileChooserDialog", {"enabled": False})

                    if not uploaded:
                        self.signal.log_message.emit("⚠ Không upload được ảnh, tiếp tục đăng text", "warning")

                    # Đợi upload hoàn tất
                    if uploaded:
                        time.sleep(random.uniform(3, 5))  # Đợi ảnh upload xong
                    else:
                        time.sleep(random.uniform(1, 2))

                # ===== BƯỚC 6: Click nút Đăng với mouse movement và RETRY =====
                get_post_btn_pos_js = '''
                (function() {
                    const postTexts = ['Đăng', 'Post', 'Đăng bài', 'Submit'];
                    const postAriaLabels = ['Đăng', 'Post', 'Đăng bài', 'Submit post', 'Submit'];

                    function getCoords(btn) {
                        let rect = btn.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0 && rect.top > 0) {
                            return {
                                x: rect.left + rect.width / 2 + (Math.random() * 10 - 5),
                                y: rect.top + rect.height / 2 + (Math.random() * 4 - 2),
                                disabled: btn.disabled || btn.getAttribute('aria-disabled') === 'true'
                            };
                        }
                        return null;
                    }

                    // Tìm trong dialog/form tạo bài viết trước
                    let dialogs = document.querySelectorAll('[role="dialog"], [data-pagelet*="ComposerPage"], form[method="post"]');
                    for (let dialog of dialogs) {
                        // Tìm nút trong dialog
                        let btns = dialog.querySelectorAll('[role="button"]');
                        for (let btn of btns) {
                            let text = (btn.innerText || '').trim();
                            // Chỉ match text CHÍNH XÁC, không phải substring
                            if (postTexts.includes(text)) {
                                let coords = getCoords(btn);
                                if (coords) {
                                    console.log('[PostBtn] Found in dialog:', text);
                                    return coords;
                                }
                            }
                        }
                        
                        // Cách 2: Tìm theo aria-label trong dialog
                        for (let label of postAriaLabels) {
                            let btn = dialog.querySelector('[aria-label="' + label + '"]');
                            if (btn) {
                                let coords = getCoords(btn);
                                if (coords) {
                                    console.log('[PostBtn] Found by aria-label in dialog:', label);
                                    return coords;
                                }
                            }
                        }
                    }

                    // Fallback: Tìm ngoài dialog nhưng ưu tiên nút ở vị trí cao (trong form)
                    // KHÔNG lấy nút có vị trí thấp (có thể là comment button)
                    let candidates = [];
                    let btns = document.querySelectorAll('[role="button"]');
                    for (let btn of btns) {
                        let text = (btn.innerText || '').trim();
                        if (postTexts.includes(text)) {
                            let rect = btn.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 && rect.top > 0) {
                                // Bỏ qua nếu nút nằm trong comment section
                                let isInComment = btn.closest('[aria-label*="Write a comment"], [aria-label*="Viết bình luận"], [data-testid*="comment"]');
                                if (!isInComment) {
                                    candidates.push({btn: btn, top: rect.top});
                                }
                            }
                        }
                    }
                    
                    // Sắp xếp theo vị trí top (cao nhất = đầu tiên) để tránh lấy nút comment ở dưới
                    candidates.sort((a, b) => a.top - b.top);
                    
                    if (candidates.length > 0) {
                        let best = candidates[0];
                        let rect = best.btn.getBoundingClientRect();
                        console.log('[PostBtn] Found best candidate at top:', best.top);
                        return {
                            x: rect.left + rect.width / 2 + (Math.random() * 10 - 5),
                            y: rect.top + rect.height / 2 + (Math.random() * 4 - 2),
                            disabled: best.btn.disabled || best.btn.getAttribute('aria-disabled') === 'true'
                        };
                    }

                    // Cách cuối: Tìm button có text Đăng/Post trong span
                    let spans = document.querySelectorAll('span');
                    for (let span of spans) {
                        let text = (span.innerText || '').trim();
                        if (postTexts.includes(text)) {
                            let btn = span.closest('[role="button"]');
                            if (btn) {
                                // Kiểm tra không phải trong comment
                                let isInComment = btn.closest('[aria-label*="Write a comment"], [aria-label*="Viết bình luận"]');
                                if (!isInComment) {
                                    let coords = getCoords(btn);
                                    if (coords) return coords;
                                }
                            }
                        }
                    }

                    // Cách cuối cùng: Tìm form submit button
                    let submitBtns = document.querySelectorAll('button[type="submit"], input[type="submit"]');
                    for (let btn of submitBtns) {
                        let coords = getCoords(btn);
                        if (coords) return coords;
                    }

                    return null;
                })()
                '''

                post_btn_pos = None
                for attempt in range(3):
                    post_btn_pos = self._cdp_evaluate(ws, get_post_btn_pos_js)
                    if post_btn_pos:
                        break
                    print(f"[WARN] Không tìm thấy nút Đăng (attempt {attempt + 1}/3), đợi thêm...")
                    time.sleep(1)

                if not post_btn_pos:
                    print(f"[WARN] Không tìm thấy nút Đăng trong group {group_id} sau 3 lần thử")
                    return (False, "", ws)

                # *** LƯU DANH SÁCH POST IDs TRƯỚC KHI ĐĂNG (chỉ của group này) ***
                get_existing_ids_js = f'''
                (function() {{
                    let groupId = '{group_id}';
                    let ids = [];
                    
                    // Chỉ lấy links của group này
                    let links = document.querySelectorAll('a[href*="/groups/"][href*="/posts/"]');
                    for (let link of links) {{
                        if (link.href.includes(groupId) || link.href.includes('/groups/{group_id}/')) {{
                            let match = link.href.match(/\\/posts\\/(\\d+)/);
                            if (match && match[1]) {{
                                ids.push(match[1]);
                            }}
                        }}
                    }}
                    
                    // Thêm pcb IDs
                    let pcbLinks = document.querySelectorAll('a[href*="set=pcb."]');
                    for (let link of pcbLinks) {{
                        let match = link.href.match(/set=pcb\\.(\\d+)/);
                        if (match && match[1]) {{
                            ids.push(match[1]);
                        }}
                    }}
                    return JSON.stringify([...new Set(ids)]);
                }})()
                '''
                existing_ids_json = self._cdp_evaluate(ws, get_existing_ids_js)
                existing_ids = json_module.loads(existing_ids_json) if existing_ids_json else []
                print(f"[Groups] Post IDs TRƯỚC khi đăng: {len(existing_ids)} IDs")

                # Di chuyển chuột đến nút Đăng
                self._move_mouse_human(ws, int(post_btn_pos['x']), int(post_btn_pos['y']))
                time.sleep(random.uniform(0.15, 0.4))

                # Click với mouse events
                self._cdp_send(ws, "Input.dispatchMouseEvent", {
                    "type": "mousePressed",
                    "x": int(post_btn_pos['x']),
                    "y": int(post_btn_pos['y']),
                    "button": "left",
                    "clickCount": 1
                })
                time.sleep(random.uniform(0.05, 0.12))
                self._cdp_send(ws, "Input.dispatchMouseEvent", {
                    "type": "mouseReleased",
                    "x": int(post_btn_pos['x']),
                    "y": int(post_btn_pos['y']),
                    "button": "left",
                    "clickCount": 1
                })

                time.sleep(random.uniform(5, 8))  # Đợi đăng xong

                # ===== BƯỚC 7: Lấy URL bài đăng =====
                # Tìm POST ID MỚI (không có trong danh sách cũ)
                debug_count = self._cdp_evaluate(ws, "document.querySelectorAll('a[href*=\"/posts/\"]').length")
                print(f"[Groups] DEBUG: Số links /posts/ SAU khi đăng = {debug_count}")
                
                post_url = self._get_new_post_url(ws, group_id, existing_ids, msg_id)
                print(f"[Groups] Kết quả URL: '{post_url}'")
                
                if post_url and 'facebook.com' in post_url:
                    self.signal.log_message.emit(f"✓ Đã đăng vào {group_name[:25]} - URL: {post_url[:50]}...", "success")
                    self.signal.posted_log.emit(f"✅ {group_name} | {post_url}")
                else:
                    self.signal.log_message.emit(f"✓ Đã đăng vào {group_name[:25]} (không lấy được URL)", "success")
                    self.signal.posted_log.emit(f"✅ {group_name} | (không lấy được URL)")

                # Lưu lịch sử
                save_post_history({
                    'profile_uuid': profile_uuid,
                    'group_id': group_id,
                    'group_name': group_name,
                    'content': post_text[:200],
                    'post_url': post_url,
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

    def _on_posted_log(self, entry: str):
        """Ghi vào nhật ký đăng tường"""
        timestamp = time.strftime("%H:%M:%S")
        self.posted_log.append(f"[{timestamp}] {entry}")

    def _on_post_complete(self):
        self._is_posting = False
        self.btn_post.setEnabled(True)
        self.btn_stop_post.setEnabled(False)
        self.post_progress.setValue(100)
        self.log("Đăng tường hoàn tất!", "success")

    # ============ BOOST TAB ============

    def _load_boost_posts(self):
        """Load posted history for boost with pagination"""
        filter_idx = self.date_filter.currentIndex()

        if filter_idx == 0:  # Today
            days = 1
        elif filter_idx == 1:  # 7 days
            days = 7
        elif filter_idx == 2:  # 30 days
            days = 30
        else:  # All
            days = 9999

        # Get total count
        self._boost_total_count = get_post_history_count(days_back=days)

        # Get current page data
        offset = self._boost_page * self._boost_page_size
        self.boost_posts = get_post_history_filtered(
            days_back=days,
            limit=self._boost_page_size,
            offset=offset
        )
        self._render_boost_posts()
        self._update_pagination_ui()

    def _on_date_filter_change(self, idx):
        self._boost_page = 0  # Reset to first page
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
        checked = state == Qt.CheckState.Checked or state == 2
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
                                # Đọc cho đến khi nhận đúng response
                                for _ in range(50):
                                    try:
                                        new_ws.settimeout(30)
                                        resp = new_ws.recv()
                                        data = json_module.loads(resp)
                                        if data.get('id') == new_msg_id[0]:
                                            return data
                                    except:
                                        return {}
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

    # ==================== PAGINATION METHODS ====================
    
    def _prev_boost_page(self):
        """Go to previous page"""
        if self._boost_page > 0:
            self._boost_page -= 1
            self._load_boost_posts()

    def _next_boost_page(self):
        """Go to next page"""
        total_pages = max(1, (self._boost_total_count + self._boost_page_size - 1) // self._boost_page_size)
        if self._boost_page + 1 < total_pages:
            self._boost_page += 1
            self._load_boost_posts()

    def _update_pagination_ui(self):
        """Update pagination buttons and label"""
        total_pages = max(1, (self._boost_total_count + self._boost_page_size - 1) // self._boost_page_size)
        current_page = self._boost_page + 1

        self.page_label.setText(f"Trang {current_page}/{total_pages}")

        # Enable/disable buttons
        self.btn_prev_page.setEnabled(self._boost_page > 0)
        self.btn_next_page.setEnabled(current_page < total_pages)

    # ==================== HELPER METHODS ====================

    def _normalize_vietnamese(self, text: str) -> str:
        """Chuẩn hóa text tiếng Việt để tìm kiếm - bỏ dấu, lowercase"""
        if not text:
            return ""
        import unicodedata
        text = text.lower()
        text = unicodedata.normalize('NFD', text)
        text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
        text = unicodedata.normalize('NFC', text)
        text = text.replace('đ', 'd').replace('Đ', 'd')
        return text

    def _get_random_images(self, folder_path: str, count: int) -> List[str]:
        """Lấy random ảnh từ thư mục"""
        if not os.path.isdir(folder_path):
            return []
        img_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}
        images = []
        for f in os.listdir(folder_path):
            if os.path.splitext(f)[1].lower() in img_extensions:
                images.append(os.path.join(folder_path, f))
        if len(images) <= count:
            return images
        return random.sample(images, count)

    def _cdp_send(self, ws, method: str, params: Dict = None) -> Dict:
        """Gửi CDP command và nhận response"""
        if not ws:
            return {"error": "No WebSocket connection"}

        self._cdp_id += 1
        msg = {"id": self._cdp_id, "method": method, "params": params or {}}

        try:
            ws.send(json_module.dumps(msg))
        except Exception as e:
            return {"error": f"WebSocket send failed: {str(e)}", "ws_closed": True}

        while True:
            try:
                ws.settimeout(30)
                resp = ws.recv()
                data = json_module.loads(resp)
                if data.get('id') == self._cdp_id:
                    return data
            except Exception as e:
                return {"error": f"WebSocket recv failed: {str(e)}", "ws_closed": True}

    def _cdp_evaluate(self, ws, expression: str):
        """Evaluate JavaScript trong browser"""
        result = self._cdp_send(ws, "Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True
        })
        return result.get('result', {}).get('result', {}).get('value')

    def _is_ws_connected(self, ws) -> bool:
        """Kiểm tra WebSocket còn kết nối không"""
        if not ws:
            return False
        try:
            result = self._cdp_send(ws, "Runtime.evaluate", {
                "expression": "1+1",
                "returnByValue": True
            })
            if result.get('ws_closed'):
                return False
            return result.get('result', {}).get('result', {}).get('value') == 2
        except:
            return False

    def _is_browser_alive(self, cdp_base: str) -> bool:
        """Kiểm tra browser còn chạy không"""
        try:
            resp = requests.get(f"{cdp_base}/json/version", timeout=3)
            return resp.status_code == 200
        except:
            return False

    def _get_or_create_ws(self, ws, cdp_base: str, target_url: str = None):
        """
        Kiểm tra WS hiện tại, nếu không ok thì tạo tab mới.
        Returns: (ws, success)
        """
        # Check WS hiện tại
        if self._is_ws_connected(ws):
            return (ws, True)

        print(f"[WARN] WebSocket mất kết nối, đang reconnect...")

        # Kiểm tra browser còn sống không
        if not self._is_browser_alive(cdp_base):
            print(f"[ERROR] Browser đã đóng hoàn toàn, không thể reconnect")
            return (None, False)

        # Thử lấy WS từ tab hiện có
        try:
            resp = requests.get(f"{cdp_base}/json", timeout=10)
            pages = resp.json()
            for p in pages:
                if p.get('type') == 'page':
                    ws_url = p.get('webSocketDebuggerUrl')
                    if ws_url:
                        try:
                            new_ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
                            print(f"[INFO] Đã reconnect WS từ tab có sẵn")
                            return (new_ws, True)
                        except:
                            pass
        except:
            pass

        # Không có tab nào, tạo tab mới
        if target_url:
            try:
                resp = requests.get(f"{cdp_base}/json/new?{target_url}", timeout=10)
                new_page = resp.json()
                ws_url = new_page.get('webSocketDebuggerUrl')
                if ws_url:
                    new_ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
                    print(f"[INFO] Đã tạo tab mới và kết nối WS")
                    return (new_ws, True)
            except Exception as e:
                print(f"[ERROR] Không tạo được tab mới: {e}")

        return (ws, False)

    def _close_old_tabs_and_prepare(self, cdp_base: str, profile_uuid: str):
        """Đóng hết tab cũ, giữ lại 1 tab và navigate về about:blank"""
        try:
            resp = requests.get(f"{cdp_base}/json", timeout=10)
            all_pages = resp.json()
            page_targets = [p for p in all_pages if p.get('type') == 'page']

            if len(page_targets) > 0:
                # Navigate tab đầu tiên về about:blank TRƯỚC (giữ browser mở)
                first_tab_ws = page_targets[0].get('webSocketDebuggerUrl')
                if first_tab_ws:
                    try:
                        temp_ws = websocket.create_connection(first_tab_ws, timeout=10, suppress_origin=True)
                        temp_ws.send(json_module.dumps({
                            "id": 1,
                            "method": "Page.navigate",
                            "params": {"url": "about:blank"}
                        }))
                        temp_ws.recv()
                        temp_ws.close()
                        print(f"[INFO] Đã navigate tab chính về about:blank")
                    except Exception as e:
                        print(f"[WARN] Không navigate được tab chính: {e}")

                # SAU ĐÓ mới đóng các tab còn lại
                if len(page_targets) > 1:
                    for p in page_targets[1:]:
                        target_id = p.get('id')
                        if target_id:
                            requests.get(f"{cdp_base}/json/close/{target_id}", timeout=5)
                    time.sleep(1)
                    print(f"[INFO] Đã đóng {len(page_targets) - 1} tab cũ")
            else:
                # Không có tab nào, tạo tab mới
                print(f"[WARN] Không có tab nào, tạo tab mới...")
                requests.get(f"{cdp_base}/json/new?about:blank", timeout=10)
                time.sleep(1)
        except Exception as e:
            print(f"[WARN] Không đóng được tab cũ: {e}")

    def _move_mouse_human(self, ws, target_x: int, target_y: int, steps: int = 20):
        """Di chuyển chuột theo đường cong như người thật"""
        import math

        current_x = random.randint(100, 300)
        current_y = random.randint(100, 200)

        # Quadratic Bezier curve control point
        ctrl_x = (current_x + target_x) / 2 + random.randint(-100, 100)
        ctrl_y = (current_y + target_y) / 2 + random.randint(-50, 50)

        for i in range(steps + 1):
            t = i / steps
            x = int((1-t)**2 * current_x + 2*(1-t)*t * ctrl_x + t**2 * target_x)
            y = int((1-t)**2 * current_y + 2*(1-t)*t * ctrl_y + t**2 * target_y)

            self._cdp_send(ws, "Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": x,
                "y": y
            })
            time.sleep(random.uniform(0.005, 0.02))

    def _auto_react_post(self, ws, react_type: str = "👍 Like"):
        """Tự động react bài viết sau khi đăng"""
        try:
            # Mapping react type
            react_map = {
                "👍 Like": "Like",
                "❤️ Yêu thích": "Love",
                "😆 Haha": "Haha",
                "😮 Wow": "Wow",
                "😢 Buồn": "Sad",
                "😡 Phẫn nộ": "Angry"
            }
            react_name = react_map.get(react_type, "Like")

            # Tìm nút like
            js_find_like = '''
            (function() {
                var likeBtn = document.querySelector('[aria-label="Thích"]') || 
                             document.querySelector('[aria-label="Like"]') ||
                             document.querySelector('[data-testid="like_button"]');
                if (likeBtn) {
                    var rect = likeBtn.getBoundingClientRect();
                    return {x: rect.x + rect.width/2, y: rect.y + rect.height/2};
                }
                return null;
            })();
            '''
            result = self._cdp_evaluate(ws, js_find_like)

            if result and 'x' in result:
                # Di chuyển chuột human-like
                self._move_mouse_human(ws, int(result['x']), int(result['y']))
                time.sleep(0.3)

                # Hover để hiện menu react
                self._cdp_send(ws, "Input.dispatchMouseEvent", {
                    "type": "mousePressed",
                    "x": int(result['x']),
                    "y": int(result['y']),
                    "button": "left",
                    "clickCount": 1
                })
                self._cdp_send(ws, "Input.dispatchMouseEvent", {
                    "type": "mouseReleased",
                    "x": int(result['x']),
                    "y": int(result['y']),
                    "button": "left"
                })

                self.signal.log_message.emit(f"✓ Đã {react_type} bài viết", "success")
                return True

        except Exception as e:
            self.signal.log_message.emit(f"Lỗi auto react: {str(e)[:30]}", "warning")

        return False

    def _get_new_post_url(self, ws, group_id: str, existing_ids: list, msg_id: list) -> str:
        """Tìm URL bài viết MỚI (không có trong danh sách existing_ids)"""
        try:
            # Helper function để evaluate JS
            def cdp_eval(expr):
                msg_id[0] += 1
                ws.send(json_module.dumps({
                    "id": msg_id[0],
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expr,
                        "returnByValue": True,
                        "awaitPromise": True
                    }
                }))
                for _ in range(50):
                    try:
                        ws.settimeout(30)
                        resp = ws.recv()
                        data = json_module.loads(resp)
                        if data.get('id') == msg_id[0]:
                            return data.get('result', {}).get('result', {}).get('value')
                    except:
                        break
                return None

            # Chuyển existing_ids thành JSON string để truyền vào JS
            existing_ids_json = json_module.dumps(existing_ids)

            # JavaScript để tìm POST ID MỚI - CHỈ từ group hiện tại
            get_new_url_js = f'''
            (function() {{
                let groupId = "{group_id}";
                let existingIds = {existing_ids_json};
                let existingSet = new Set(existingIds);
                console.log('[GetURL] Group ID:', groupId);
                console.log('[GetURL] Existing IDs:', existingIds.length);

                // Tìm tất cả post IDs hiện tại - CHỈ của group này
                let currentIds = [];
                
                // Từ /posts/ links - CHỈ lấy của group này
                let postLinks = document.querySelectorAll('a[href*="/groups/"][href*="/posts/"]');
                for (let link of postLinks) {{
                    // Kiểm tra link thuộc về group này
                    if (link.href.includes('/groups/' + groupId + '/') || link.href.includes('/groups/' + groupId + '?')) {{
                        let match = link.href.match(/\\/posts\\/(\\d+)/);
                        if (match && match[1]) {{
                            currentIds.push({{id: match[1], url: link.href}});
                        }}
                    }}
                }}

                // Từ set=pcb. links - cũng filter theo group
                let pcbLinks = document.querySelectorAll('a[href*="set=pcb."]');
                for (let link of pcbLinks) {{
                    // Kiểm tra nếu link nằm trong context của group này
                    let parentGroup = link.closest('[data-pagelet*="Group"]') || link.closest('[role="article"]');
                    let match = link.href.match(/set=pcb\\.(\\d+)/);
                    if (match && match[1]) {{
                        let url = 'https://www.facebook.com/groups/{group_id}/posts/' + match[1] + '/';
                        currentIds.push({{id: match[1], url: url}});
                    }}
                }}

                console.log('[GetURL] Current IDs from this group:', currentIds.length);

                // Tìm ID MỚI (không có trong existingSet)
                let newIds = currentIds.filter(item => !existingSet.has(item.id));
                console.log('[GetURL] NEW IDs:', newIds.length);

                if (newIds.length > 0) {{
                    // Trả về ID mới đầu tiên (hoặc có thể sort theo ID lớn nhất nếu có nhiều)
                    console.log('[GetURL] ✓ NEW POST:', newIds[0].url);
                    return newIds[0].url;
                }}

                console.log('[GetURL] ✗ Không tìm thấy ID mới');
                return null;
            }})()
            '''

            post_url = cdp_eval(get_new_url_js)
            print(f"[Groups] URL bài MỚI: {post_url}")

            if post_url:
                # Clean URL
                if '?' in post_url:
                    post_url = post_url.split('?')[0]
                return post_url

            return ""

        except Exception as e:
            print(f"[Groups] Lỗi lấy URL mới: {e}")
            import traceback
            traceback.print_exc()
            return ""

    def _get_post_url_from_dom(self, ws, group_id: str, msg_id: list) -> str:
        """Lấy URL bài viết vừa đăng từ DOM"""
        try:
            # Helper function để evaluate JS
            def cdp_eval(expr):
                msg_id[0] += 1
                ws.send(json_module.dumps({
                    "id": msg_id[0],
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expr,
                        "returnByValue": True,
                        "awaitPromise": True
                    }
                }))
                for _ in range(50):
                    try:
                        ws.settimeout(30)
                        resp = ws.recv()
                        data = json_module.loads(resp)
                        if data.get('id') == msg_id[0]:
                            return data.get('result', {}).get('result', {}).get('value')
                    except:
                        break
                return None

            # Đơn giản: Tìm TẤT CẢ links có /posts/ và lấy ID lớn nhất
            get_url_js = '''
            (function() {
                console.log('[GetURL] Bắt đầu tìm URL...');
                
                // Lấy tất cả links có /posts/
                let allLinks = document.querySelectorAll('a[href*="/posts/"]');
                console.log('[GetURL] Tìm thấy', allLinks.length, 'links có /posts/');
                
                let postIds = [];
                for (let link of allLinks) {
                    let href = link.href;
                    // Match /posts/POST_ID
                    let match = href.match(/\\/posts\\/(\\d+)/);
                    if (match && match[1]) {
                        postIds.push({id: match[1], url: href});
                    }
                }

                console.log('[GetURL] Post IDs:', postIds.length);
                
                if (postIds.length > 0) {
                    // Sắp xếp theo ID giảm dần (số lớn = mới nhất)
                    postIds.sort((a, b) => {
                        if (a.id.length !== b.id.length) {
                            return b.id.length - a.id.length; // Số dài hơn = lớn hơn
                        }
                        return b.id.localeCompare(a.id); // So sánh string cho số lớn
                    });
                    
                    console.log('[GetURL] ✓ Post ID lớn nhất:', postIds[0].id);
                    console.log('[GetURL] ✓ URL:', postIds[0].url);
                    return postIds[0].url;
                }

                // Fallback: Tìm set=pcb.POST_ID
                let pcbLinks = document.querySelectorAll('a[href*="set=pcb."]');
                console.log('[GetURL] pcb links:', pcbLinks.length);
                
                let pcbIds = [];
                for (let link of pcbLinks) {
                    let match = link.href.match(/set=pcb\\.(\\d+)/);
                    if (match && match[1]) {
                        pcbIds.push(match[1]);
                    }
                }
                
                if (pcbIds.length > 0) {
                    pcbIds.sort((a, b) => {
                        if (a.length !== b.length) return b.length - a.length;
                        return b.localeCompare(a);
                    });
                    let url = 'https://www.facebook.com/groups/GROUP_ID/posts/' + pcbIds[0] + '/';
                    console.log('[GetURL] ✓ Từ pcb:', url);
                    return url;
                }

                // Fallback: Tìm pfbid
                let pfbLinks = document.querySelectorAll('a[href*="pfbid"]');
                console.log('[GetURL] pfbid links:', pfbLinks.length);
                if (pfbLinks.length > 0) {
                    console.log('[GetURL] ✓ pfbid:', pfbLinks[pfbLinks.length - 1].href);
                    return pfbLinks[pfbLinks.length - 1].href;
                }

                console.log('[GetURL] ✗ Không tìm thấy');
                return null;
            })()
            '''

            # Thử lấy URL
            post_url = cdp_eval(get_url_js)
            print(f"[Groups] URL từ DOM: {post_url}")

            if post_url:
                # Fix URL nếu cần (thay GROUP_ID bằng group_id thật)
                if 'GROUP_ID' in post_url:
                    post_url = post_url.replace('GROUP_ID', group_id)
                return post_url

            return ""

        except Exception as e:
            print(f"[Groups] Lỗi lấy URL từ DOM: {e}")
            import traceback
            traceback.print_exc()
            return ""

    def _get_post_url_after_reload(self, ws, group_id: str, msg_id: list) -> str:
        """Lấy URL bài viết vừa đăng sau khi reload page"""
        try:
            # Helper function để evaluate JS
            def cdp_eval(expr):
                msg_id[0] += 1
                ws.send(json_module.dumps({
                    "id": msg_id[0],
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expr,
                        "returnByValue": True,
                        "awaitPromise": True
                    }
                }))
                for _ in range(50):
                    try:
                        ws.settimeout(30)
                        resp = ws.recv()
                        data = json_module.loads(resp)
                        if data.get('id') == msg_id[0]:
                            return data.get('result', {}).get('result', {}).get('value')
                    except:
                        break
                return None

            # JavaScript để tìm URL bài vừa đăng
            # Sau khi reload, bài vừa đăng thường là bài ĐẦU TIÊN trong feed
            # (nếu không cần duyệt) hoặc có trạng thái "Đang chờ duyệt"
            
            get_url_js = f'''
            (function() {{
                let groupId = '{group_id}';
                console.log('[GetURL] Tìm bài vừa đăng sau reload, groupId:', groupId);

                // Các từ khóa thời gian mới đăng
                const recentKeywords = [
                    'vừa xong', 'just now', 'now',
                    '1 phút', '2 phút', '3 phút', '4 phút', '5 phút',
                    '1m', '2m', '3m', '4m', '5m', 
                    '1 min', '2 min', '3 min',
                    'một phút', 'vài giây', 'giây trước',
                    'seconds ago', 'minute ago', 'minutes ago'
                ];

                // Các từ khóa pending
                const pendingKeywords = [
                    'đang chờ', 'chờ duyệt', 'pending', 'awaiting', 
                    'submitted', 'đợi phê duyệt', 'review'
                ];

                function matchesKeywords(text, keywords) {{
                    if (!text) return false;
                    let lower = text.toLowerCase();
                    for (let kw of keywords) {{
                        if (lower.includes(kw.toLowerCase())) return true;
                    }}
                    return false;
                }}

                function getUrlFromArticle(article) {{
                    // 1. Tìm set=pcb.POST_ID (link ảnh)
                    let pcbLinks = article.querySelectorAll('a[href*="set=pcb."]');
                    for (let link of pcbLinks) {{
                        let match = link.href.match(/set=pcb\\.(\\d+)/);
                        if (match && match[1]) {{
                            return 'https://www.facebook.com/groups/' + groupId + '/posts/' + match[1] + '/';
                        }}
                    }}

                    // 2. Tìm /groups/.../posts/
                    let postLinks = article.querySelectorAll('a[href*="/groups/"][href*="/posts/"]');
                    for (let link of postLinks) {{
                        if (link.href.includes(groupId) && !link.href.includes('notif')) {{
                            return link.href;
                        }}
                    }}

                    // 3. Tìm pfbid
                    let pfbLinks = article.querySelectorAll('a[href*="pfbid"]');
                    for (let link of pfbLinks) {{
                        if (link.href.includes('/groups/')) return link.href;
                    }}

                    // 4. Tìm permalink
                    let permaLinks = article.querySelectorAll('a[href*="permalink"]');
                    for (let link of permaLinks) {{
                        if (link.href.includes('/groups/')) return link.href;
                    }}

                    return null;
                }}

                // Lấy tất cả articles
                let articles = document.querySelectorAll('[role="article"]');
                console.log('[GetURL] Tìm thấy', articles.length, 'bài viết');

                // Ưu tiên 1: Tìm bài có thời gian "vừa xong" hoặc pending
                for (let i = 0; i < articles.length; i++) {{
                    let article = articles[i];
                    let text = article.innerText || '';
                    let first500 = text.substring(0, 500);

                    let isRecent = matchesKeywords(first500, recentKeywords);
                    let isPending = matchesKeywords(first500, pendingKeywords);

                    if (isRecent || isPending) {{
                        let url = getUrlFromArticle(article);
                        if (url) {{
                            console.log('[GetURL] ✓ Tìm thấy bài', isRecent ? 'MỚI' : 'PENDING', 'tại index', i, ':', url);
                            return url;
                        }}
                    }}
                }}

                // Ưu tiên 2: Lấy bài ĐẦU TIÊN (sau reload thường là bài mới nhất)
                if (articles.length > 0) {{
                    let firstArticle = articles[0];
                    let url = getUrlFromArticle(firstArticle);
                    if (url) {{
                        console.log('[GetURL] ✓ Lấy bài ĐẦU TIÊN:', url);
                        return url;
                    }}
                }}

                // Ưu tiên 3: Tìm bất kỳ link posts nào
                let allPostLinks = document.querySelectorAll('a[href*="/groups/' + groupId + '/posts/"]');
                if (allPostLinks.length > 0) {{
                    // Lấy link đầu tiên
                    console.log('[GetURL] ✓ Lấy link đầu tiên trong page:', allPostLinks[0].href);
                    return allPostLinks[0].href;
                }}

                console.log('[GetURL] ✗ Không tìm thấy URL nào');
                return null;
            }})()
            '''

            # Thử lấy URL
            post_url = cdp_eval(get_url_js)
            print(f"[Groups] URL sau reload: {post_url}")

            if post_url and '/groups/' in post_url and ('/posts/' in post_url or 'pfbid' in post_url):
                return post_url

            return post_url or ""

        except Exception as e:
            print(f"[Groups] Lỗi lấy URL sau reload: {e}")
            import traceback
            traceback.print_exc()
            return ""

    def _get_post_url_current_tab(self, ws, group_id: str, msg_id: list) -> str:
        """Lấy URL bài viết vừa đăng trực tiếp trên tab hiện tại"""
        try:
            # Helper function để evaluate JS
            def cdp_eval(expr):
                msg_id[0] += 1
                ws.send(json_module.dumps({
                    "id": msg_id[0],
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expr,
                        "returnByValue": True,
                        "awaitPromise": True
                    }
                }))
                # Đọc cho đến khi nhận đúng response
                for _ in range(50):
                    try:
                        ws.settimeout(30)
                        resp = ws.recv()
                        data = json_module.loads(resp)
                        if data.get('id') == msg_id[0]:
                            return data.get('result', {}).get('result', {}).get('value')
                    except:
                        break
                return None

            # JavaScript để tìm URL bài vừa đăng
            # Sau khi đăng, Facebook thường hiển thị:
            # 1. URL trong thanh địa chỉ thay đổi (nếu không duyệt)
            # 2. Có notification/toast hiển thị "Đã đăng" với link
            # 3. Bài viết mới xuất hiện trong feed (có thể ở dưới hoặc pending)
            
            get_url_js = f'''
            (function() {{
                let groupId = '{group_id}';
                console.log('[GetURL] Tìm URL bài đăng, groupId:', groupId);

                // 1. Kiểm tra URL hiện tại - FB có thể đã redirect đến bài viết
                let currentUrl = window.location.href;
                console.log('[GetURL] Current URL:', currentUrl);
                if (currentUrl.includes('/posts/') || currentUrl.includes('pfbid')) {{
                    console.log('[GetURL] ✓ URL đã là bài viết!');
                    return currentUrl;
                }}

                // 2. Tìm toast/notification "Đã đăng bài" có chứa link
                let toasts = document.querySelectorAll('[role="alert"], [role="status"], [data-testid*="toast"]');
                for (let toast of toasts) {{
                    let links = toast.querySelectorAll('a[href*="/posts/"], a[href*="pfbid"]');
                    if (links.length > 0) {{
                        console.log('[GetURL] ✓ Tìm thấy link trong toast:', links[0].href);
                        return links[0].href;
                    }}
                }}

                // 3. Tìm bài có trạng thái "Đang chờ duyệt" / "Pending" / "Submitted"
                let articles = document.querySelectorAll('[role="article"]');
                console.log('[GetURL] Found', articles.length, 'articles');
                
                for (let article of articles) {{
                    let text = article.innerText || '';
                    // Kiểm tra có phải bài pending không
                    let isPending = text.includes('Đang chờ') || text.includes('Pending') || 
                                    text.includes('đợi duyệt') || text.includes('Submitted') ||
                                    text.includes('chờ phê duyệt') || text.includes('awaiting');
                    
                    // Kiểm tra bài vừa đăng (thời gian gần)
                    let isRecent = text.includes('vừa xong') || text.includes('just now') ||
                                   text.includes('1 phút') || text.includes('1m') ||
                                   text.includes('1 min') || text.includes('vài giây');

                    if (isPending || isRecent) {{
                        // Tìm URL trong article này
                        // Cách 1: set=pcb.POST_ID (link ảnh)
                        let pcbLinks = article.querySelectorAll('a[href*="set=pcb."]');
                        for (let link of pcbLinks) {{
                            let match = link.href.match(/set=pcb\\.(\\d+)/);
                            if (match && match[1]) {{
                                let postUrl = 'https://www.facebook.com/groups/' + groupId + '/posts/' + match[1] + '/';
                                console.log('[GetURL] ✓ Got from pcb (pending/recent):', postUrl);
                                return postUrl;
                            }}
                        }}
                        
                        // Cách 2: /groups/.../posts/
                        let postLinks = article.querySelectorAll('a[href*="/groups/"][href*="/posts/"]');
                        for (let link of postLinks) {{
                            if (link.href.includes(groupId)) {{
                                console.log('[GetURL] ✓ Got direct (pending/recent):', link.href);
                                return link.href;
                            }}
                        }}

                        // Cách 3: pfbid
                        let pfbLinks = article.querySelectorAll('a[href*="pfbid"]');
                        for (let link of pfbLinks) {{
                            if (link.href.includes('/groups/')) {{
                                console.log('[GetURL] ✓ Got pfbid (pending/recent):', link.href);
                                return link.href;
                            }}
                        }}
                    }}
                }}

                // 4. Fallback: Scroll xuống và tìm bài cuối cùng (bài mới thường ở dưới)
                window.scrollTo(0, document.body.scrollHeight);
                
                // Tìm bài cuối cùng trong feed
                let lastArticle = articles[articles.length - 1];
                if (lastArticle) {{
                    let pcbLinks = lastArticle.querySelectorAll('a[href*="set=pcb."]');
                    for (let link of pcbLinks) {{
                        let match = link.href.match(/set=pcb\\.(\\d+)/);
                        if (match && match[1]) {{
                            let postUrl = 'https://www.facebook.com/groups/' + groupId + '/posts/' + match[1] + '/';
                            console.log('[GetURL] ✓ Got from LAST article:', postUrl);
                            return postUrl;
                        }}
                    }}

                    let postLinks = lastArticle.querySelectorAll('a[href*="/posts/"]');
                    if (postLinks.length > 0) {{
                        console.log('[GetURL] ✓ Got from LAST article link:', postLinks[0].href);
                        return postLinks[0].href;
                    }}
                }}

                // 5. Fallback cuối: Tìm bất kỳ link posts nào trong page
                let allPostLinks = document.querySelectorAll('a[href*="/groups/' + groupId + '/posts/"]');
                if (allPostLinks.length > 0) {{
                    // Lấy link cuối cùng (có thể là bài mới nhất)
                    let lastLink = allPostLinks[allPostLinks.length - 1];
                    console.log('[GetURL] ✓ Got last post link:', lastLink.href);
                    return lastLink.href;
                }}

                console.log('[GetURL] ✗ Không tìm thấy URL');
                return null;
            }})()
            '''

            # Thử lấy URL với retry
            post_url = None
            for attempt in range(3):
                post_url = cdp_eval(get_url_js)
                print(f"[Groups] Attempt {attempt + 1}/3 - URL: {post_url}")

                if post_url and '/groups/' in post_url and ('/posts/' in post_url or 'pfbid' in post_url):
                    print(f"[Groups] ✓ Tìm thấy URL!")
                    return post_url

                # Scroll và thử lại
                if attempt < 2:
                    time.sleep(2)
                    cdp_eval("window.scrollBy(0, 500);")
                    time.sleep(1)

            return post_url or ""

        except Exception as e:
            print(f"[Groups] Lỗi lấy URL: {e}")
            import traceback
            traceback.print_exc()
            return ""

    def _get_post_url_new_tab(self, ws, cdp_base: str, group_url: str, group_id: str, msg_id: list) -> str:
        """Mở tab mới để lấy URL bài viết vừa đăng"""
        try:
            # Đợi FB cập nhật feed
            time.sleep(random.uniform(3, 5))

            # Tạo tab mới navigate đến group
            msg_id[0] += 1
            ws.send(json_module.dumps({
                "id": msg_id[0],
                "method": "Target.createTarget",
                "params": {"url": group_url}
            }))
            
            # Đọc cho đến khi nhận response có id khớp
            target_id = None
            for _ in range(50):  # Max 50 messages
                try:
                    ws.settimeout(30)
                    resp = ws.recv()
                    data = json_module.loads(resp)
                    if data.get('id') == msg_id[0]:
                        target_id = data.get('result', {}).get('targetId')
                        break
                except:
                    break

            if not target_id:
                print(f"[Groups] Không tạo được tab mới")
                return ""

            print(f"[Groups] Tạo tab mới OK, targetId: {target_id[:20]}...")
            time.sleep(random.uniform(3, 4))

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
                print(f"[Groups] Không lấy được WS URL tab mới")
                return ""

            # Kết nối WebSocket tab mới
            new_ws = None
            try:
                new_ws = websocket.create_connection(new_ws_url, timeout=30, suppress_origin=True)
            except:
                try:
                    new_ws = websocket.create_connection(new_ws_url, timeout=30)
                except:
                    print(f"[Groups] Không kết nối được WS tab mới")
                    return ""

            new_msg_id = [0]

            def send_new(method, params=None):
                new_msg_id[0] += 1
                msg = {"id": new_msg_id[0], "method": method, "params": params or {}}
                new_ws.send(json_module.dumps(msg))
                # Đọc cho đến khi nhận response có id khớp
                while True:
                    try:
                        new_ws.settimeout(30)
                        resp = new_ws.recv()
                        data = json_module.loads(resp)
                        if data.get('id') == new_msg_id[0]:
                            return data
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
            time.sleep(random.uniform(2, 3))
            for _ in range(10):
                ready = eval_new("document.readyState")
                if ready == 'complete':
                    break
                time.sleep(1)

            time.sleep(random.uniform(2, 3))

            # Tìm bài vừa đăng - logic cải tiến từ code tham khảo
            get_post_url_js = f'''
            (function() {{
                let groupId = '{group_id}';
                console.log('[GetURL] Group ID:', groupId);
                console.log('[GetURL] Current URL:', window.location.href);

                // Các từ khóa thời gian "vừa đăng"
                const recentKeywords = [
                    'vừa xong', 'vua xong', 'just now',
                    '1 phút', '2 phút', '3 phút', '4 phút', '5 phút',
                    '1m', '2m', '3m', '4m', '5m',
                    '1 min', '2 min', '3 min', '4 min', '5 min',
                    '1 minute', '2 minutes', '3 minutes', '4 minutes', '5 minutes',
                    'một phút', 'hai phút', 'vài giây', 'a few seconds',
                    '1 giây', '2 giây', '5 giây', '10 giây', '30 giây', '45 giây',
                    '1s', '2s', '5s', '10s', '30s', '45s',
                    'now', 'mới'
                ];

                function isRecentTime(text) {{
                    if (!text) return false;
                    let normalized = text.toLowerCase().replace(/\\s+/g, '');
                    for (let kw of recentKeywords) {{
                        if (normalized.includes(kw.toLowerCase().replace(/\\s+/g, ''))) {{
                            return true;
                        }}
                    }}
                    return false;
                }}

                function getPostUrlFromArticle(post) {{
                    // 1. Tìm pcb link (link ảnh có set=pcb.POST_ID)
                    let pcbLinks = post.querySelectorAll('a[href*="set=pcb."]');
                    for (let link of pcbLinks) {{
                        let match = link.href.match(/set=pcb\\.(\\d+)/);
                        if (match && match[1]) {{
                            let postId = match[1];
                            let postUrl = 'https://www.facebook.com/groups/' + groupId + '/posts/' + postId + '/';
                            console.log('[GetURL] Got URL from pcb:', postUrl);
                            return postUrl;
                        }}
                    }}

                    // 2. Tìm link /groups/.../posts/ trực tiếp
                    let postLinks = post.querySelectorAll('a[href*="/groups/"][href*="/posts/"]');
                    for (let link of postLinks) {{
                        if (link.href && !link.href.includes('notif_id') && link.href.includes(groupId)) {{
                            console.log('[GetURL] Got direct post URL:', link.href);
                            return link.href;
                        }}
                    }}

                    // 3. Tìm permalink hoặc pfbid
                    let permalinks = post.querySelectorAll('a[href*="pfbid"], a[href*="permalink"]');
                    for (let link of permalinks) {{
                        if (link.href && link.href.includes('/groups/')) {{
                            console.log('[GetURL] Got permalink:', link.href);
                            return link.href;
                        }}
                    }}

                    // 4. Tìm bất kỳ link nào có post id pattern
                    let allLinks = post.querySelectorAll('a[href*="/groups/"]');
                    for (let link of allLinks) {{
                        let href = link.href;
                        // Match patterns like /posts/123 or /permalink/123 or pfbid...
                        if (href.match(/\\/posts\\/\\d+/) || href.includes('pfbid') || href.includes('permalink')) {{
                            console.log('[GetURL] Got link pattern:', href);
                            return href;
                        }}
                    }}

                    return null;
                }}

                // Tìm trong các articles
                let articles = document.querySelectorAll('[role="article"]');
                console.log('[GetURL] Found', articles.length, 'articles');

                // *** QUAN TRỌNG: Lấy bài đầu tiên vì nó là bài vừa đăng ***
                if (articles.length > 0) {{
                    // Bài đầu tiên thường là bài mới nhất
                    let firstArticle = articles[0];
                    let url = getPostUrlFromArticle(firstArticle);
                    if (url) {{
                        console.log('[GetURL] ✓ Got URL from FIRST article:', url);
                        return url;
                    }}
                }}

                // Fallback: Tìm bài có thời gian mới
                for (let article of articles) {{
                    let hasRecentTime = false;

                    // Tìm trong abbr, time elements
                    let timeEls = article.querySelectorAll('abbr, time, [data-utime]');
                    for (let el of timeEls) {{
                        let text = el.innerText || el.getAttribute('title') || '';
                        if (isRecentTime(text)) {{
                            hasRecentTime = true;
                            break;
                        }}
                    }}

                    // Tìm trong aria-label của các link
                    if (!hasRecentTime) {{
                        let links = article.querySelectorAll('a[aria-label]');
                        for (let link of links) {{
                            let label = link.getAttribute('aria-label') || '';
                            if (isRecentTime(label)) {{
                                hasRecentTime = true;
                                break;
                            }}
                        }}
                    }}

                    // Tìm trong text đầu bài viết
                    if (!hasRecentTime) {{
                        let firstText = (article.innerText || '').substring(0, 500);
                        if (isRecentTime(firstText)) {{
                            hasRecentTime = true;
                        }}
                    }}

                    if (hasRecentTime) {{
                        let url = getPostUrlFromArticle(article);
                        if (url) {{
                            console.log('[GetURL] ✓ FOUND POST URL (recent time):', url);
                            return url;
                        }}
                    }}
                }}

                // Fallback: Tìm link trực tiếp trong page
                console.log('[GetURL] No article match, searching page directly...');
                let allPostLinks = document.querySelectorAll('a[href*="/groups/' + groupId + '/posts/"]');
                console.log('[GetURL] Found', allPostLinks.length, 'direct post links');
                if (allPostLinks.length > 0) {{
                    // Lấy link đầu tiên
                    let firstLink = allPostLinks[0];
                    if (firstLink.href && !firstLink.href.includes('notif_id')) {{
                        console.log('[GetURL] ✓ Direct link:', firstLink.href);
                        return firstLink.href;
                    }}
                }}

                // Fallback: Tìm pcb link trong toàn page
                let allPcbLinks = document.querySelectorAll('a[href*="set=pcb."]');
                if (allPcbLinks.length > 0) {{
                    let match = allPcbLinks[0].href.match(/set=pcb\\.(\\d+)/);
                    if (match && match[1]) {{
                        let postId = match[1];
                        let postUrl = 'https://www.facebook.com/groups/' + groupId + '/posts/' + postId + '/';
                        console.log('[GetURL] ✓ Built from pcb:', postUrl);
                        return postUrl;
                    }}
                }}

                // Fallback cuối: Tìm bất kỳ link có pfbid
                let pfbidLinks = document.querySelectorAll('a[href*="pfbid"]');
                for (let link of pfbidLinks) {{
                    if (link.href.includes('/groups/')) {{
                        console.log('[GetURL] ✓ pfbid link:', link.href);
                        return link.href;
                    }}
                }}

                console.log('[GetURL] ✗ No valid URL found');
                return null;
            }})()
            '''

            # Thử lấy URL với retry
            post_url = None
            for attempt in range(5):
                post_url = eval_new(get_post_url_js)
                print(f"[Groups] Attempt {attempt + 1}/5 - URL: {post_url}")

                # Chỉ chấp nhận URL hợp lệ
                if post_url and '/groups/' in post_url and ('/posts/' in post_url or 'pfbid' in post_url):
                    print(f"[Groups] ✓ Tìm thấy URL hợp lệ!")
                    break

                post_url = None
                if attempt < 4:
                    print(f"[Groups] Reload và thử lại...")
                    time.sleep(random.uniform(2, 3))
                    send_new("Page.reload", {})
                    time.sleep(random.uniform(3, 4))

            # Đóng tab mới
            try:
                new_ws.close()
                requests.get(f"{cdp_base}/json/close/{target_id}", timeout=5)
            except:
                pass

            if post_url:
                print(f"[Groups] Tìm thấy post URL: {post_url}")
                return post_url
            else:
                print(f"[Groups] Không tìm thấy URL bài đăng")
                return ""

        except Exception as e:
            print(f"[Groups] Lỗi lấy post URL: {e}")
            import traceback
            traceback.print_exc()
            return ""

    # ============ SETTINGS SAVE/LOAD ============

    def _save_settings(self):
        """Lưu settings vào file để khôi phục khi mở lại app"""
        try:
            # Lưu các cài đặt POST tab
            self._settings.setValue("post/attach_img_checked", self.attach_img_cb.isChecked())
            self._settings.setValue("post/img_folder", self.img_folder_input.text())
            self._settings.setValue("post/img_count", self.img_count_spin.value())
            self._settings.setValue("post/random_content_checked", self.random_content_cb.isChecked())
            self._settings.setValue("post/delay", self.delay_spin.value())
            self._settings.setValue("post/random_delay_checked", self.random_delay_cb.isChecked())
            
            # Lưu category đã chọn
            self._settings.setValue("post/category_index", self.cat_combo.currentIndex())
            
            # Lưu nội dung đã nhập
            self._settings.setValue("post/content_text", self.content_input.toPlainText())
            
            # Sync settings
            self._settings.sync()
            
        except Exception as e:
            try:
                print(f"[Settings] Error saving settings: {e}")
            except Exception:
                pass

    def _load_settings(self):
        """Load settings từ file khi mở app"""
        try:
            # Load các cài đặt POST tab
            if self._settings.contains("post/attach_img_checked"):
                checked = self._settings.value("post/attach_img_checked", False, type=bool)
                self.attach_img_cb.setChecked(checked)
            
            if self._settings.contains("post/img_folder"):
                folder = self._settings.value("post/img_folder", "")
                if folder and os.path.isdir(folder):
                    self.img_folder_input.setText(folder)
                    self.img_folder_input.setEnabled(self.attach_img_cb.isChecked())
                    # Count images
                    count = sum(1 for f in os.listdir(folder) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')))
                    self.img_count_label.setText(f"(Tổng: {count} ảnh)")
            
            if self._settings.contains("post/img_count"):
                self.img_count_spin.setValue(self._settings.value("post/img_count", 5, type=int))
            
            if self._settings.contains("post/random_content_checked"):
                self.random_content_cb.setChecked(self._settings.value("post/random_content_checked", False, type=bool))
            
            if self._settings.contains("post/delay"):
                self.delay_spin.setValue(self._settings.value("post/delay", 5, type=int))
            
            if self._settings.contains("post/random_delay_checked"):
                self.random_delay_cb.setChecked(self._settings.value("post/random_delay_checked", True, type=bool))
            
            # Load category (sau khi data đã load)
            QTimer.singleShot(1000, self._load_category_setting)
            
            # Load nội dung
            if self._settings.contains("post/content_text"):
                content = self._settings.value("post/content_text", "")
                if content:
                    self.content_input.setPlainText(content)
            
            self.log("✓ Đã khôi phục cài đặt", "success")
        except Exception as e:
            print(f"[Settings] Lỗi load settings: {e}")

    def _load_category_setting(self):
        """Load category setting sau khi categories đã load"""
        try:
            if self._settings.contains("post/category_index"):
                idx = self._settings.value("post/category_index", 0, type=int)
                if idx < self.cat_combo.count():
                    self.cat_combo.setCurrentIndex(idx)
        except:
            pass

    def closeEvent(self, event):
        """Lưu settings khi đóng widget"""
        self._save_settings()
        super().closeEvent(event)