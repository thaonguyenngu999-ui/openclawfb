"""
Scripts Page - Lịch đăng tự động / Schedule Automation
PySide6 version với real CDP automation
"""
import threading
import time
import random
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QScrollArea, QMessageBox, QSpinBox, QProgressBar, QTabWidget, QLineEdit,
    QTableWidget, QTableWidgetItem
)
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor

from config import COLORS
from widgets import (
    CyberButton, CyberInput, CyberComboBox, CyberCard,
    CyberTitle, CyberStatCard, CyberCheckBox
)
from db import (
    get_schedules, save_schedule, delete_schedule, update_schedule,
    get_groups_for_profiles, get_contents, get_categories,
    get_schedule_folders, save_schedule_folder, delete_schedule_folder,
    get_post_history, get_connection
)
from api_service import api

# Import automation modules với fallback
try:
    from automation.window_manager import acquire_window_slot, release_window_slot, get_window_bounds
    WINDOW_MANAGER_AVAILABLE = True
except ImportError:
    WINDOW_MANAGER_AVAILABLE = False
    def acquire_window_slot(): return 0
    def release_window_slot(slot_id): pass
    def get_window_bounds(slot_id): return (0, 0, 800, 600)

try:
    from automation import CDPHelper
    CDP_AVAILABLE = True
except ImportError:
    CDPHelper = None
    CDP_AVAILABLE = False


class ScriptsSignal(QObject):
    """Signal để thread-safe UI update"""
    folders_loaded = Signal(list)
    profiles_loaded = Signal(list)
    groups_loaded = Signal(list)
    contents_loaded = Signal(object)  # dict với categories và contents
    schedules_loaded = Signal(list)
    log_message = Signal(str, str)
    progress_update = Signal(str)
    schedule_status = Signal(int, str)  # schedule_id, new_status


class ScriptsPage(QWidget):
    """Scripts Page - Lịch đăng tự động"""

    def __init__(self, log_func, groups_page=None, parent=None):
        super().__init__(parent)
        self.log = log_func
        self.groups_page = groups_page  # Reference đến GroupsPage để dùng chung logic đăng bài

        # Data
        self.folders: List[Dict] = []
        self.profiles: List[Dict] = []
        self.groups: List[Dict] = []
        self.contents: List[Dict] = []
        self.schedules: List[Dict] = []
        self.categories: List[Dict] = []
        self.schedule_folders: List[Dict] = []
        self.schedule_checkboxes: Dict[int, CyberCheckBox] = {}  # schedule_id -> checkbox

        # Selection state
        self.selected_profile_uuids: List[str] = []
        self.selected_group_ids: List[int] = []
        self.profile_vars: Dict[str, bool] = {}  # uuid -> selected
        self.group_vars: Dict[int, bool] = {}     # id -> selected

        # Scheduler state
        self._scheduler_running = False
        self._scheduler_thread: Optional[threading.Thread] = None
        self._last_check_hour = -1  # Track giờ đã check

        # Signal để thread-safe UI update
        self.signal = ScriptsSignal()
        self.signal.folders_loaded.connect(self._on_folders_loaded)
        self.signal.profiles_loaded.connect(self._on_profiles_loaded)
        self.signal.groups_loaded.connect(self._on_groups_loaded)
        self.signal.contents_loaded.connect(self._on_contents_loaded)
        self.signal.schedules_loaded.connect(self._on_schedules_loaded)
        self.signal.log_message.connect(lambda msg, t: self.log(msg, t))
        self.signal.progress_update.connect(self._on_progress_update)
        self.signal.schedule_status.connect(self._on_schedule_status)

        self._setup_ui()
        QTimer.singleShot(500, self._load_initial_data)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # Top bar
        top_bar = QHBoxLayout()
        title = CyberTitle("LỊCH ĐĂNG", "", "cyan")
        top_bar.addWidget(title)
        top_bar.addStretch()

        self.stat_schedules = CyberStatCard("LỊCH", "0", "📅", "cyan")
        self.stat_schedules.setFixedWidth(140)
        top_bar.addWidget(self.stat_schedules)

        self.stat_groups = CyberStatCard("NHÓM", "0", "👥", "mint")
        self.stat_groups.setFixedWidth(140)
        top_bar.addWidget(self.stat_groups)

        self.stat_profiles = CyberStatCard("PROFILES", "0", "👤", "purple")
        self.stat_profiles.setFixedWidth(140)
        top_bar.addWidget(self.stat_profiles)

        layout.addLayout(top_bar)

        # Tab widget
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
        self.tab_widget.addTab(self._create_schedule_tab(), "Tạo lịch")
        self.tab_widget.addTab(self._create_list_tab(), "Danh sách lịch")
        self.tab_widget.addTab(self._create_history_tab(), "Lịch sử đăng")
        layout.addWidget(self.tab_widget, 1)

    def _create_schedule_tab(self):
        """Tab tạo lịch đăng"""
        widget = QWidget()
        main_layout = QHBoxLayout(widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(12)

        # ===== CỘT TRÁI: Chọn Profile & Nhóm =====
        left_col = CyberCard(COLORS['neon_cyan'])
        left_col.setFixedWidth(350)
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(12, 12, 12, 12)

        # Folder selection
        folder_row = QHBoxLayout()
        folder_label = QLabel("📁 Thư mục:")
        folder_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        folder_row.addWidget(folder_label)

        self.folder_combo = CyberComboBox(["-- Tất cả --"])
        self.folder_combo.currentIndexChanged.connect(self._on_folder_change)
        folder_row.addWidget(self.folder_combo, 1)

        btn_load = CyberButton("Tải", "cyan", "📥")
        btn_load.clicked.connect(self._load_profiles)
        folder_row.addWidget(btn_load)

        left_layout.addLayout(folder_row)

        # Profile list
        profile_header = QHBoxLayout()
        profile_label = QLabel("👤 Chọn Profiles:")
        profile_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px; font-weight: bold;")
        profile_header.addWidget(profile_label)

        self.select_all_profiles = CyberCheckBox()
        self.select_all_profiles.stateChanged.connect(self._toggle_all_profiles)
        profile_header.addWidget(self.select_all_profiles)

        profile_header.addStretch()

        self.profile_count_label = QLabel("0 profile")
        self.profile_count_label.setStyleSheet(f"color: {COLORS['neon_cyan']}; font-size: 11px;")
        profile_header.addWidget(self.profile_count_label)

        left_layout.addLayout(profile_header)

        # Profile scroll
        profile_scroll = QScrollArea()
        profile_scroll.setWidgetResizable(True)
        profile_scroll.setFixedHeight(150)
        profile_scroll.setStyleSheet(f"""
            QScrollArea {{
                background: {COLORS['bg_darker']};
                border: 1px solid {COLORS['border']};
                border-radius: 6px;
            }}
        """)

        self.profile_list = QWidget()
        self.profile_list_layout = QVBoxLayout(self.profile_list)
        self.profile_list_layout.setContentsMargins(4, 4, 4, 4)
        self.profile_list_layout.setSpacing(2)
        self.profile_list_layout.addStretch()

        profile_scroll.setWidget(self.profile_list)
        left_layout.addWidget(profile_scroll)

        # Group list
        group_header = QHBoxLayout()
        group_label = QLabel("👥 Chọn Nhóm:")
        group_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px; font-weight: bold;")
        group_header.addWidget(group_label)

        self.select_all_groups = CyberCheckBox()
        self.select_all_groups.stateChanged.connect(self._toggle_all_groups)
        group_header.addWidget(self.select_all_groups)

        group_header.addStretch()

        self.group_count_label = QLabel("0 nhóm")
        self.group_count_label.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 11px;")
        group_header.addWidget(self.group_count_label)

        left_layout.addLayout(group_header)
        
        # Group filter input
        self.group_filter_input = QLineEdit()
        self.group_filter_input.setPlaceholderText("🔍 Lọc nhóm...")
        self.group_filter_input.setStyleSheet(f"""
            QLineEdit {{
                background: {COLORS['bg_darker']};
                border: 1px solid {COLORS['border']};
                border-radius: 4px;
                padding: 6px 10px;
                color: {COLORS['text_primary']};
                font-size: 12px;
            }}
            QLineEdit:focus {{
                border-color: {COLORS['neon_pink']};
            }}
        """)
        self.group_filter_input.textChanged.connect(self._filter_groups)
        left_layout.addWidget(self.group_filter_input)

        # Group scroll
        group_scroll = QScrollArea()
        group_scroll.setWidgetResizable(True)
        group_scroll.setStyleSheet(f"""
            QScrollArea {{
                background: {COLORS['bg_darker']};
                border: 1px solid {COLORS['border']};
                border-radius: 6px;
            }}
        """)

        self.group_list = QWidget()
        self.group_list_layout = QVBoxLayout(self.group_list)
        self.group_list_layout.setContentsMargins(4, 4, 4, 4)
        self.group_list_layout.setSpacing(2)
        self.group_list_layout.addStretch()

        group_scroll.setWidget(self.group_list)
        left_layout.addWidget(group_scroll, 1)

        main_layout.addWidget(left_col)

        # ===== CỘT PHẢI: Cài đặt lịch =====
        right_col = CyberCard(COLORS['neon_mint'])
        right_layout = QVBoxLayout(right_col)
        right_layout.setContentsMargins(12, 12, 12, 12)

        # ===== THƯ MỤC LỊCH ĐĂNG =====
        schedule_folder_label = QLabel("📂 Thư mục lịch đăng:")
        schedule_folder_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px; font-weight: bold;")
        right_layout.addWidget(schedule_folder_label)
        
        schedule_folder_row = QHBoxLayout()
        self.schedule_folder_combo = CyberComboBox(["-- Chọn hoặc tạo mới --"])
        self.schedule_folder_combo.setMinimumWidth(200)
        schedule_folder_row.addWidget(self.schedule_folder_combo, 1)
        
        btn_new_folder = CyberButton("+ Tạo", "cyan")
        btn_new_folder.clicked.connect(self._create_schedule_folder)
        schedule_folder_row.addWidget(btn_new_folder)
        right_layout.addLayout(schedule_folder_row)

        # ===== CHUYÊN MỤC (Category) =====
        category_label = QLabel("📁 Chọn chuyên mục (random nội dung):")
        category_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px; font-weight: bold; margin-top: 8px;")
        right_layout.addWidget(category_label)

        self.category_combo = CyberComboBox(["-- Chọn chuyên mục --"])
        self.category_combo.currentIndexChanged.connect(self._on_category_change)
        right_layout.addWidget(self.category_combo)
        
        # Content selection (hiển thị số nội dung trong category)
        self.content_info_label = QLabel("📝 Nội dung: 0 bài (sẽ random)")
        self.content_info_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        right_layout.addWidget(self.content_info_label)

        # ===== FOLDER ẢNH RANDOM =====
        img_label = QLabel("🖼️ Folder ảnh random:")
        img_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px; font-weight: bold; margin-top: 8px;")
        right_layout.addWidget(img_label)
        
        img_row = QHBoxLayout()
        self.image_folder_input = CyberInput("C:/path/to/images")
        img_row.addWidget(self.image_folder_input, 1)
        
        btn_browse_img = CyberButton("...", "ghost")
        btn_browse_img.setFixedWidth(40)
        btn_browse_img.clicked.connect(self._browse_image_folder)
        img_row.addWidget(btn_browse_img)
        right_layout.addLayout(img_row)
        
        # Số lượng ảnh random
        img_count_row = QHBoxLayout()
        img_count_label = QLabel("Số ảnh random:")
        img_count_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        img_count_row.addWidget(img_count_label)
        
        self.min_images = QSpinBox()
        self.min_images.setRange(0, 20)
        self.min_images.setValue(3)
        self.min_images.setStyleSheet(f"""
            QSpinBox {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_primary']};
                border: 1px solid {COLORS['border']};
                border-radius: 4px;
                padding: 4px;
                min-width: 50px;
            }}
        """)
        img_count_row.addWidget(self.min_images)
        
        dash_label = QLabel("-")
        dash_label.setStyleSheet(f"color: {COLORS['text_muted']};")
        img_count_row.addWidget(dash_label)
        
        self.max_images = QSpinBox()
        self.max_images.setRange(0, 20)
        self.max_images.setValue(8)
        self.max_images.setStyleSheet(f"""
            QSpinBox {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_primary']};
                border: 1px solid {COLORS['border']};
                border-radius: 4px;
                padding: 4px;
                min-width: 50px;
            }}
        """)
        img_count_row.addWidget(self.max_images)
        
        img_count_row.addStretch()
        right_layout.addLayout(img_count_row)

        # Time slots
        time_label = QLabel("⏰ Khung giờ đăng:")
        time_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px; font-weight: bold; margin-top: 10px;")
        right_layout.addWidget(time_label)

        # Time slot grid (6h - 23h)
        time_frame = QFrame()
        time_frame.setStyleSheet(f"background: {COLORS['bg_card']}; border-radius: 6px; padding: 8px;")
        time_grid_layout = QVBoxLayout(time_frame)
        time_grid_layout.setContentsMargins(8, 8, 8, 8)

        self.time_vars = {}  # hour -> checkbox

        # Row 1: 6-14
        row1 = QHBoxLayout()
        for hour in range(6, 15):
            cb = CyberCheckBox()
            self.time_vars[hour] = cb
            hour_widget = QWidget()
            hour_layout = QVBoxLayout(hour_widget)
            hour_layout.setContentsMargins(0, 0, 0, 0)
            hour_layout.setSpacing(0)
            hour_layout.addWidget(cb, alignment=Qt.AlignCenter)
            lbl = QLabel(f"{hour}h")
            lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px;")
            hour_layout.addWidget(lbl, alignment=Qt.AlignCenter)
            row1.addWidget(hour_widget)
        time_grid_layout.addLayout(row1)

        # Row 2: 15-23
        row2 = QHBoxLayout()
        for hour in range(15, 24):
            cb = CyberCheckBox()
            self.time_vars[hour] = cb
            hour_widget = QWidget()
            hour_layout = QVBoxLayout(hour_widget)
            hour_layout.setContentsMargins(0, 0, 0, 0)
            hour_layout.setSpacing(0)
            hour_layout.addWidget(cb, alignment=Qt.AlignCenter)
            lbl = QLabel(f"{hour}h")
            lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px;")
            hour_layout.addWidget(lbl, alignment=Qt.AlignCenter)
            row2.addWidget(hour_widget)
        time_grid_layout.addLayout(row2)

        # Quick select
        quick_row = QHBoxLayout()

        btn_morning = CyberButton("Sáng (6-12)", "ghost")
        btn_morning.clicked.connect(lambda: self._select_time_range(6, 12))
        quick_row.addWidget(btn_morning)

        btn_afternoon = CyberButton("Chiều (12-18)", "ghost")
        btn_afternoon.clicked.connect(lambda: self._select_time_range(12, 18))
        quick_row.addWidget(btn_afternoon)

        btn_evening = CyberButton("Tối (18-23)", "ghost")
        btn_evening.clicked.connect(lambda: self._select_time_range(18, 24))
        quick_row.addWidget(btn_evening)

        btn_clear_time = CyberButton("Xóa", "ghost")
        btn_clear_time.clicked.connect(self._clear_time_selection)
        quick_row.addWidget(btn_clear_time)

        time_grid_layout.addLayout(quick_row)

        right_layout.addWidget(time_frame)

        # Days of week
        days_label = QLabel("📆 Ngày trong tuần:")
        days_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px; font-weight: bold; margin-top: 10px;")
        right_layout.addWidget(days_label)

        days_row = QHBoxLayout()
        self.day_vars = {}
        day_names = [("T2", 0), ("T3", 1), ("T4", 2), ("T5", 3), ("T6", 4), ("T7", 5), ("CN", 6)]

        for name, idx in day_names:
            cb = CyberCheckBox()
            cb.setChecked(True)  # Mặc định chọn tất cả
            self.day_vars[idx] = cb
            day_widget = QWidget()
            day_layout = QVBoxLayout(day_widget)
            day_layout.setContentsMargins(0, 0, 0, 0)
            day_layout.setSpacing(0)
            day_layout.addWidget(cb, alignment=Qt.AlignCenter)
            lbl = QLabel(name)
            lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
            day_layout.addWidget(lbl, alignment=Qt.AlignCenter)
            days_row.addWidget(day_widget)

        right_layout.addLayout(days_row)

        # Options
        options_label = QLabel("⚙️ Tùy chọn:")
        options_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px; font-weight: bold; margin-top: 10px;")
        right_layout.addWidget(options_label)

        options_row = QHBoxLayout()

        posts_label = QLabel("Số bài/nhóm:")
        posts_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        options_row.addWidget(posts_label)

        self.posts_per_group = QSpinBox()
        self.posts_per_group.setRange(1, 10)
        self.posts_per_group.setValue(1)
        self.posts_per_group.setStyleSheet(f"""
            QSpinBox {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_primary']};
                border: 1px solid {COLORS['border']};
                border-radius: 4px;
                padding: 4px;
            }}
        """)
        options_row.addWidget(self.posts_per_group)

        delay_label = QLabel("Delay (phút):")
        delay_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        options_row.addWidget(delay_label)

        self.delay_minutes = QSpinBox()
        self.delay_minutes.setRange(1, 60)
        self.delay_minutes.setValue(5)
        self.delay_minutes.setStyleSheet(f"""
            QSpinBox {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_primary']};
                border: 1px solid {COLORS['border']};
                border-radius: 4px;
                padding: 4px;
            }}
        """)
        options_row.addWidget(self.delay_minutes)

        options_row.addStretch()
        right_layout.addLayout(options_row)

        # Progress
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 11px;")
        right_layout.addWidget(self.progress_label)

        # Buttons
        right_layout.addStretch()
        btn_row = QHBoxLayout()

        self.btn_create = CyberButton("Tạo lịch", "success", "➕")
        self.btn_create.clicked.connect(self._create_schedule)
        btn_row.addWidget(self.btn_create)

        self.btn_run_now = CyberButton("Chạy ngay", "cyan", "⚡")
        self.btn_run_now.clicked.connect(self._run_now)
        btn_row.addWidget(self.btn_run_now)

        right_layout.addLayout(btn_row)

        main_layout.addWidget(right_col, 1)

        return widget

    def _create_list_tab(self):
        """Tab danh sách lịch"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        # Header
        header = QHBoxLayout()
        header_label = QLabel("📋 Danh sách lịch đăng")
        header_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 14px; font-weight: bold;")
        header.addWidget(header_label)

        header.addStretch()
        
        # Nút chọn tất cả
        self.select_all_schedules = CyberCheckBox()
        self.select_all_schedules.stateChanged.connect(self._toggle_all_schedules)
        header.addWidget(self.select_all_schedules)
        
        # Nút xóa đã chọn
        btn_delete_selected = CyberButton("🗑️ Xóa", "ghost")
        btn_delete_selected.clicked.connect(self._delete_selected_schedules)
        header.addWidget(btn_delete_selected)

        btn_refresh = CyberButton("🔄", "ghost")
        btn_refresh.setFixedWidth(35)
        btn_refresh.clicked.connect(self._load_schedules)
        header.addWidget(btn_refresh)

        layout.addLayout(header)

        # Schedule list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"""
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
        """)

        self.schedule_list = QWidget()
        self.schedule_list_layout = QVBoxLayout(self.schedule_list)
        self.schedule_list_layout.setContentsMargins(8, 8, 8, 8)
        self.schedule_list_layout.setSpacing(12)
        self.schedule_list_layout.addStretch()

        scroll.setWidget(self.schedule_list)
        layout.addWidget(scroll)

        return widget
    
    def _create_history_tab(self):
        """Tab lịch sử đăng bài"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        # Header với filter
        header = QHBoxLayout()
        header_label = QLabel("📜 Lịch sử đăng bài")
        header_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 14px; font-weight: bold;")
        header.addWidget(header_label)
        header.addStretch()

        # Filter status
        filter_label = QLabel("Lọc:")
        filter_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        header.addWidget(filter_label)

        self.history_filter_combo = CyberComboBox(["Tất cả", "Thành công", "Thất bại"])
        self.history_filter_combo.setFixedWidth(120)
        self.history_filter_combo.currentIndexChanged.connect(self._load_history)
        header.addWidget(self.history_filter_combo)

        btn_refresh_history = CyberButton("🔄", "ghost")
        btn_refresh_history.setFixedWidth(35)
        btn_refresh_history.clicked.connect(self._load_history)
        header.addWidget(btn_refresh_history)

        btn_clear_history = CyberButton("🗑️ Xóa tất cả", "ghost")
        btn_clear_history.clicked.connect(self._clear_history)
        header.addWidget(btn_clear_history)

        layout.addLayout(header)

        # Statistics row
        stats_row = QHBoxLayout()
        
        self.stat_total_label = QLabel("📊 Tổng: 0")
        self.stat_total_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px; padding: 4px 8px; background: {COLORS['bg_card']}; border-radius: 4px;")
        stats_row.addWidget(self.stat_total_label)

        self.stat_success_label = QLabel("✅ Thành công: 0")
        self.stat_success_label.setStyleSheet(f"color: {COLORS['neon_green']}; font-size: 12px; padding: 4px 8px; background: {COLORS['bg_card']}; border-radius: 4px;")
        stats_row.addWidget(self.stat_success_label)

        self.stat_failed_label = QLabel("❌ Thất bại: 0")
        self.stat_failed_label.setStyleSheet(f"color: {COLORS['neon_pink']}; font-size: 12px; padding: 4px 8px; background: {COLORS['bg_card']}; border-radius: 4px;")
        stats_row.addWidget(self.stat_failed_label)

        stats_row.addStretch()
        layout.addLayout(stats_row)

        # History list - sử dụng QTableWidget
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(6)
        self.history_table.setHorizontalHeaderLabels(["Thời gian", "Profile", "Nhóm", "Nội dung", "Trạng thái", "Link bài"])
        self.history_table.setStyleSheet(f"""
            QTableWidget {{
                background: {COLORS['bg_card']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                gridline-color: {COLORS['border']};
                color: {COLORS['text_primary']};
                font-size: 12px;
            }}
            QTableWidget::item {{
                padding: 8px;
                border-bottom: 1px solid {COLORS['border']};
            }}
            QTableWidget::item:selected {{
                background: {COLORS['bg_hover']};
            }}
            QHeaderView::section {{
                background: {COLORS['bg_dark']};
                color: {COLORS['neon_cyan']};
                padding: 8px;
                border: none;
                border-bottom: 2px solid {COLORS['neon_cyan']};
                font-weight: bold;
            }}
            QScrollBar:vertical {{
                background: {COLORS['bg_dark']};
                width: 10px;
                border-radius: 5px;
            }}
            QScrollBar::handle:vertical {{
                background: {COLORS['border']};
                border-radius: 5px;
                min-height: 20px;
            }}
        """)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        self.history_table.setColumnWidth(0, 140)
        self.history_table.setColumnWidth(1, 100)
        self.history_table.setColumnWidth(2, 200)
        self.history_table.setColumnWidth(3, 200)
        self.history_table.setColumnWidth(4, 90)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.cellDoubleClicked.connect(self._open_post_url)

        layout.addWidget(self.history_table)

        return widget

    def _load_history(self):
        """Load lịch sử đăng bài từ database"""
        try:
            filter_index = self.history_filter_combo.currentIndex()
            status_filter = None
            if filter_index == 1:
                status_filter = 'success'
            elif filter_index == 2:
                status_filter = 'failed'

            history = get_post_history(limit=500)
            
            # Filter theo status nếu có
            if status_filter:
                history = [h for h in history if h.get('status') == status_filter]

            # Update stats
            all_history = get_post_history(limit=10000)
            total = len(all_history)
            success = len([h for h in all_history if h.get('status') == 'success'])
            failed = len([h for h in all_history if h.get('status') == 'failed'])

            self.stat_total_label.setText(f"📊 Tổng: {total}")
            self.stat_success_label.setText(f"✅ Thành công: {success}")
            self.stat_failed_label.setText(f"❌ Thất bại: {failed}")

            # Clear và populate table
            self.history_table.setRowCount(0)
            
            for row_idx, h in enumerate(history):
                self.history_table.insertRow(row_idx)

                # Thời gian
                created_at = h.get('created_at', '')
                if created_at:
                    try:
                        dt = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                        created_at = dt.strftime('%d/%m %H:%M')
                    except:
                        pass
                time_item = QTableWidgetItem(created_at)
                self.history_table.setItem(row_idx, 0, time_item)

                # Profile
                profile = h.get('profile_uuid', '')[:8] if h.get('profile_uuid') else 'N/A'
                profile_item = QTableWidgetItem(profile)
                self.history_table.setItem(row_idx, 1, profile_item)

                # Nhóm
                group_name = h.get('group_name', 'N/A')
                if len(group_name) > 30:
                    group_name = group_name[:30] + '...'
                group_item = QTableWidgetItem(group_name)
                self.history_table.setItem(row_idx, 2, group_item)

                # Nội dung (content preview)
                content_preview = h.get('content_preview', '')[:40] + '...' if h.get('content_preview') else 'N/A'
                content_item = QTableWidgetItem(content_preview)
                self.history_table.setItem(row_idx, 3, content_item)

                # Trạng thái
                status = h.get('status', 'unknown')
                if status == 'success':
                    status_text = "✅ OK"
                    status_color = COLORS['neon_green']
                else:
                    status_text = "❌ Lỗi"
                    status_color = COLORS['neon_pink']
                status_item = QTableWidgetItem(status_text)
                status_item.setForeground(QColor(status_color))
                self.history_table.setItem(row_idx, 4, status_item)

                # Link bài
                post_url = h.get('post_url', '')
                if post_url:
                    url_item = QTableWidgetItem("🔗 Xem bài")
                    url_item.setData(Qt.UserRole, post_url)
                    url_item.setForeground(QColor(COLORS['neon_cyan']))
                else:
                    url_item = QTableWidgetItem("-")
                self.history_table.setItem(row_idx, 5, url_item)

        except Exception as e:
            print(f"[Scripts] Error loading history: {e}")
            import traceback
            traceback.print_exc()

    def _open_post_url(self, row, col):
        """Mở link bài đăng khi double click"""
        if col == 5:  # Cột Link bài
            item = self.history_table.item(row, col)
            if item:
                post_url = item.data(Qt.UserRole)
                if post_url:
                    import webbrowser
                    webbrowser.open(post_url)

    def _clear_history(self):
        """Xóa toàn bộ lịch sử"""
        reply = QMessageBox.question(
            self, "Xác nhận",
            "Bạn có chắc muốn xóa toàn bộ lịch sử đăng bài?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            try:
                with get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM post_history")
                self.log("Đã xóa toàn bộ lịch sử đăng bài", "success")
                self._load_history()
            except Exception as e:
                self.log(f"Lỗi xóa lịch sử: {e}", "error")
    
    def _toggle_all_schedules(self, state):
        """Toggle chọn tất cả schedules"""
        checked = (state == Qt.CheckState.Checked or state == 2)
        for cb in self.schedule_checkboxes.values():
            cb.setChecked(checked)
    
    def _delete_selected_schedules(self):
        """Xóa các schedules đã chọn"""
        selected_ids = [sid for sid, cb in self.schedule_checkboxes.items() if cb.isChecked()]
        if not selected_ids:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn lịch cần xóa!")
            return
        
        reply = QMessageBox.question(
            self, "Xác nhận",
            f"Xóa {len(selected_ids)} lịch đã chọn?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            for sid in selected_ids:
                delete_schedule(sid)
            self.log(f"Đã xóa {len(selected_ids)} lịch", "success")
            self._load_schedules()

    def _load_initial_data(self):
        """Load dữ liệu ban đầu"""
        self._load_folders()
        self._load_contents()
        self._load_schedules()
        self._load_schedule_folders()
        self._load_history()

    def _load_folders(self):
        """Load folders từ Hidemium"""
        def fetch():
            try:
                return api.get_folders(limit=100)
            except Exception as e:
                print(f"[Scripts] Error loading folders: {e}")
                return []

        def run():
            result = fetch()
            self.signal.folders_loaded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_folders_loaded(self, folders):
        """Slot nhận folders"""
        self.folders = folders or []

        self.folder_combo.clear()
        self.folder_combo.addItem("-- Tất cả --")
        for f in self.folders:
            name = f.get('name', 'Unknown')
            self.folder_combo.addItem(f"📁 {name}")

        self.log(f"Đã tải {len(self.folders)} thư mục", "success")
        self._load_profiles()

    def _on_folder_change(self, index):
        if index >= 0:
            self._load_profiles()

    def _load_profiles(self):
        """Load profiles từ folder"""
        folder_idx = self.folder_combo.currentIndex()

        def fetch():
            try:
                if folder_idx <= 0:
                    profiles = api.get_profiles(limit=500)
                else:
                    folder = self.folders[folder_idx - 1]
                    folder_id = folder.get('id')
                    profiles = api.get_profiles(folder_id=[folder_id], limit=500)
                return profiles
            except Exception as e:
                print(f"[Scripts] Error loading profiles: {e}")
                return []

        def run():
            result = fetch()
            self.signal.profiles_loaded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_profiles_loaded(self, profiles):
        """Slot nhận profiles"""
        self.profiles = profiles or []
        self.profile_vars = {p.get('uuid'): False for p in self.profiles}
        self._render_profiles()
        self.stat_profiles.set_value(str(len(self.profiles)))
        self.log(f"Đã tải {len(self.profiles)} profiles", "success")

    def _render_profiles(self):
        """Render danh sách profiles"""
        while self.profile_list_layout.count() > 0:
            item = self.profile_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for profile in self.profiles:
            uuid = profile.get('uuid', '')
            name = profile.get('name', profile.get('uuid', '')[:8])

            row = QWidget()
            row.setFixedHeight(28)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 0, 4, 0)
            row_layout.setSpacing(4)

            cb = CyberCheckBox()
            cb.setChecked(self.profile_vars.get(uuid, False))
            cb.stateChanged.connect(lambda s, u=uuid: self._on_profile_select(u, s))
            row_layout.addWidget(cb)

            lbl = QLabel(name[:30])
            lbl.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 11px;")
            row_layout.addWidget(lbl, 1)

            self.profile_list_layout.addWidget(row)

        self.profile_list_layout.addStretch()
        self._update_profile_count()

    def _on_profile_select(self, uuid, state):
        """Khi chọn profile"""
        self.profile_vars[uuid] = (state == Qt.CheckState.Checked or state == 2)
        self._update_profile_count()
        self._load_groups()

    def _toggle_all_profiles(self, state):
        """Toggle chọn tất cả profiles"""
        select_all = (state == Qt.CheckState.Checked or state == 2)
        for uuid in self.profile_vars:
            self.profile_vars[uuid] = select_all
        self._render_profiles()
        self._update_profile_count()
        self._load_groups()

    def _update_profile_count(self):
        """Cập nhật số profiles đã chọn"""
        count = sum(1 for v in self.profile_vars.values() if v)
        self.profile_count_label.setText(f"{count} profile")
        self.selected_profile_uuids = [u for u, v in self.profile_vars.items() if v]

    def _load_groups(self):
        """Load groups cho profiles đã chọn"""
        if not self.selected_profile_uuids:
            self.groups = []
            self._render_groups()
            return

        def fetch():
            try:
                return get_groups_for_profiles(self.selected_profile_uuids)
            except Exception as e:
                print(f"[Scripts] Error loading groups: {e}")
                return []

        def run():
            result = fetch()
            self.signal.groups_loaded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_groups_loaded(self, groups):
        """Slot nhận groups"""
        self.groups = groups or []
        self.group_vars = {g.get('id'): False for g in self.groups}
        self._render_groups()
        self.stat_groups.set_value(str(len(self.groups)))

    def _render_groups(self):
        """Render danh sách groups"""
        while self.group_list_layout.count() > 0:
            item = self.group_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self.groups:
            empty = QLabel("Chọn profile để xem nhóm")
            empty.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
            empty.setAlignment(Qt.AlignCenter)
            self.group_list_layout.addWidget(empty)
        else:
            # Lọc theo filter text
            filter_text = self.group_filter_input.text().strip().lower() if hasattr(self, 'group_filter_input') else ''
            
            filtered_groups = []
            for group in self.groups:
                name = (group.get('group_name') or group.get('name', '')).lower()
                if not filter_text or filter_text in name:
                    filtered_groups.append(group)
            
            if not filtered_groups:
                empty = QLabel(f"Không tìm thấy nhóm '{filter_text}'")
                empty.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
                empty.setAlignment(Qt.AlignCenter)
                self.group_list_layout.addWidget(empty)
            else:
                for group in filtered_groups:
                    gid = group.get('id', 0)
                    name = group.get('group_name') or group.get('name', 'Unknown')

                    row = QWidget()
                    row.setFixedHeight(28)
                    row_layout = QHBoxLayout(row)
                    row_layout.setContentsMargins(4, 0, 4, 0)
                    row_layout.setSpacing(4)

                    cb = CyberCheckBox()
                    cb.setChecked(self.group_vars.get(gid, False))
                    cb.stateChanged.connect(lambda s, i=gid: self._on_group_select(i, s))
                    row_layout.addWidget(cb)

                    lbl = QLabel(name[:30])
                    lbl.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 11px;")
                    row_layout.addWidget(lbl, 1)

                    self.group_list_layout.addWidget(row)

        self.group_list_layout.addStretch()
        self._update_group_count()

    def _on_group_select(self, gid, state):
        """Khi chọn group"""
        self.group_vars[gid] = (state == Qt.CheckState.Checked or state == 2)
        self._update_group_count()

    def _toggle_all_groups(self, state):
        """Toggle chọn tất cả groups (chỉ groups đang hiển thị)"""
        select_all = (state == Qt.CheckState.Checked or state == 2)
        filter_text = self.group_filter_input.text().strip().lower()
        
        for group in self.groups:
            gid = group.get('id')
            name = (group.get('group_name') or group.get('name', '')).lower()
            # Chỉ toggle những group đang hiển thị (match filter)
            if not filter_text or filter_text in name:
                self.group_vars[gid] = select_all
        self._render_groups()

    def _filter_groups(self, text):
        """Lọc danh sách groups theo từ khóa"""
        self._render_groups()

    def _update_group_count(self):
        """Cập nhật số groups đã chọn"""
        count = sum(1 for v in self.group_vars.values() if v)
        self.group_count_label.setText(f"{count} nhóm")
        self.selected_group_ids = [i for i, v in self.group_vars.items() if v]

    def _load_contents(self):
        """Load categories và contents từ DB"""
        def fetch():
            try:
                categories = get_categories()
                contents = get_contents()
                return {'categories': categories, 'contents': contents}
            except Exception as e:
                print(f"[Scripts] Error loading contents: {e}")
                return {'categories': [], 'contents': []}

        def run():
            result = fetch()
            self.signal.contents_loaded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_contents_loaded(self, data):
        """Slot nhận categories và contents"""
        if isinstance(data, dict):
            self.categories = data.get('categories', [])
            self.contents = data.get('contents', [])
        else:
            # Backward compatibility
            self.categories = []
            self.contents = data or []

        # Load categories vào combo
        self.category_combo.clear()
        self.category_combo.addItem("-- Chọn chuyên mục --")
        for cat in self.categories:
            name = cat.get('name', 'Untitled')
            cat_id = cat.get('id')
            # Đếm số nội dung trong category
            count = len([c for c in self.contents if c.get('category_id') == cat_id])
            self.category_combo.addItem(f"📁 {name} ({count} bài)")
    
    def _on_category_change(self, index):
        """Khi chọn category"""
        if index <= 0:
            self.content_info_label.setText("📝 Nội dung: 0 bài")
            self.selected_category_id = None
            return
        
        category = self.categories[index - 1]
        self.selected_category_id = category.get('id')
        
        # Đếm số nội dung trong category này
        contents_in_cat = [c for c in self.contents if c.get('category_id') == self.selected_category_id]
        self.content_info_label.setText(f"📝 Nội dung: {len(contents_in_cat)} bài (sẽ random)")
    
    def _browse_image_folder(self):
        """Chọn folder ảnh"""
        from PySide6.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(self, "Chọn folder ảnh")
        if folder:
            self.image_folder_input.setText(folder)

    def _load_schedule_folders(self):
        """Load danh sách thư mục lịch đăng"""
        def run():
            try:
                folders = get_schedule_folders()
                self.schedule_folders = folders
                # Emit để update UI
                self.signal.log_message.emit(f"Loaded {len(folders)} schedule folders", "info")
            except Exception as e:
                print(f"[Scripts] Error loading schedule folders: {e}")
                self.schedule_folders = []
        
        threading.Thread(target=run, daemon=True).start()
        
        # Update combo ngay
        try:
            self.schedule_folders = get_schedule_folders()
            self.schedule_folder_combo.clear()
            self.schedule_folder_combo.addItem("-- Chọn hoặc tạo mới --")
            for f in self.schedule_folders:
                self.schedule_folder_combo.addItem(f"📂 {f.get('name', 'Unknown')}")
        except:
            pass
    
    def _create_schedule_folder(self):
        """Tạo thư mục lịch đăng mới"""
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Tạo thư mục lịch đăng", "Nhập tên thư mục:")
        if ok and name.strip():
            try:
                folder_id = save_schedule_folder(name.strip())
                self.log(f"Đã tạo thư mục: {name}", "success")
                self._load_schedule_folders()
                # Chọn thư mục vừa tạo
                for i in range(self.schedule_folder_combo.count()):
                    if name.strip() in self.schedule_folder_combo.itemText(i):
                        self.schedule_folder_combo.setCurrentIndex(i)
                        break
            except Exception as e:
                QMessageBox.warning(self, "Lỗi", f"Không thể tạo thư mục: {e}")

    def _select_time_range(self, start, end):
        """Chọn khoảng thời gian"""
        for hour, cb in self.time_vars.items():
            if start <= hour < end:
                cb.setChecked(True)
            else:
                cb.setChecked(False)

    def _clear_time_selection(self):
        """Xóa chọn thời gian"""
        for cb in self.time_vars.values():
            cb.setChecked(False)

    def _create_schedule(self):
        """Tạo lịch đăng mới - sử dụng category để random nội dung"""
        # Validate
        if not self.selected_group_ids:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn ít nhất 1 nhóm!")
            return

        category_idx = self.category_combo.currentIndex()
        if category_idx <= 0:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn chuyên mục!")
            return

        selected_hours = [h for h, cb in self.time_vars.items() if cb.isChecked()]
        if not selected_hours:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn ít nhất 1 khung giờ!")
            return

        selected_days = [d for d, cb in self.day_vars.items() if cb.isChecked()]
        if not selected_days:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn ít nhất 1 ngày!")
            return

        # Get schedule folder
        folder_idx = self.schedule_folder_combo.currentIndex()
        folder_id = None
        folder_name = None
        if folder_idx > 0 and hasattr(self, 'schedule_folders') and self.schedule_folders:
            folder = self.schedule_folders[folder_idx - 1]
            folder_id = folder.get('id')
            folder_name = folder.get('name')

        # Get category
        category = self.categories[category_idx - 1]
        category_id = category.get('id')
        
        # Get image folder settings
        image_folder = self.image_folder_input.text().strip()
        min_images = self.min_images.value()
        max_images = self.max_images.value()

        # Create schedule for each group
        for group_id in self.selected_group_ids:
            group = next((g for g in self.groups if g.get('id') == group_id), None)
            if not group:
                continue

            schedule_data = {
                'folder_id': str(folder_id) if folder_id else None,
                'folder_name': folder_name,
                'profile_uuid': group.get('profile_uuid'),
                'group_id': group_id,
                'group_name': group.get('group_name') or group.get('name', ''),
                'group_url': group.get('group_url') or group.get('url', ''),
                'category_id': category_id,  # Lưu category thay vì content cụ thể
                'content_id': None,  # Sẽ random khi chạy
                'image_folder': image_folder,
                'min_images': min_images,
                'max_images': max_images,
                'time_slots': ','.join(map(str, sorted(selected_hours))),
                'days_of_week': ','.join(map(str, sorted(selected_days))),
                'posts_per_run': self.posts_per_group.value(),
                'delay_minutes': self.delay_minutes.value(),
                'status': 'active',
                'last_run': None,
                'next_run': None
            }

            save_schedule(schedule_data)

        self.log(f"Đã tạo {len(self.selected_group_ids)} lịch đăng", "success")
        self._load_schedules()

    def _load_schedules(self):
        """Load danh sách lịch"""
        def fetch():
            try:
                return get_schedules()
            except Exception as e:
                print(f"[Scripts] Error loading schedules: {e}")
                return []

        def run():
            result = fetch()
            self.signal.schedules_loaded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_schedules_loaded(self, schedules):
        """Slot nhận schedules"""
        self.schedules = schedules or []
        self._render_schedules()
        self.stat_schedules.set_value(str(len(self.schedules)))

    def _render_schedules(self):
        """Render danh sách schedules"""
        # Clear checkboxes
        self.schedule_checkboxes.clear()
        
        while self.schedule_list_layout.count() > 0:
            item = self.schedule_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self.schedules:
            empty = QLabel("Chưa có lịch đăng nào")
            empty.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px;")
            empty.setAlignment(Qt.AlignCenter)
            self.schedule_list_layout.addWidget(empty)
        else:
            # Gom nhóm theo profile
            profile_groups = {}
            for schedule in self.schedules:
                profile_uuid = schedule.get('profile_uuid', 'unknown')
                if profile_uuid not in profile_groups:
                    profile_groups[profile_uuid] = []
                profile_groups[profile_uuid].append(schedule)
            
            # Render theo profile
            for profile_uuid, schedules in profile_groups.items():
                row = self._create_profile_schedule_row(profile_uuid, schedules)
                self.schedule_list_layout.addWidget(row)

        self.schedule_list_layout.addStretch()

    def _create_profile_schedule_row(self, profile_uuid: str, schedules: List[Dict]):
        """Tạo row đẹp cho 1 profile với nhiều schedules"""
        # Lấy thông tin chung
        first = schedules[0]
        folder_name = first.get('folder_name') or 'Chưa phân loại'
        time_slots = first.get('time_slots', '')
        
        # Đếm active/paused
        active_count = sum(1 for s in schedules if s.get('status') == 'active')
        total_count = len(schedules)
        
        # Lấy tên profile
        profile_name = profile_uuid[:8] if profile_uuid else 'Unknown'
        for p in getattr(self, 'profiles', []):
            if p.get('uuid') == profile_uuid:
                profile_name = p.get('name', profile_uuid[:8])
                break

        # Card chính
        card = QWidget()
        all_active = active_count == total_count
        
        if all_active:
            border_color = COLORS['neon_mint']
        elif active_count > 0:
            border_color = "#ffaa00"
        else:
            border_color = COLORS['text_muted']
        
        card.setStyleSheet(f"""
            QWidget {{
                background-color: {COLORS['bg_card']};
                border: 2px solid {border_color};
                border-radius: 10px;
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
        """)
        card.setFixedHeight(70)

        main_layout = QHBoxLayout(card)
        main_layout.setContentsMargins(15, 10, 15, 10)
        main_layout.setSpacing(20)

        # Checkbox
        cb = CyberCheckBox()
        schedule_ids = [s.get('id') for s in schedules]
        for sid in schedule_ids:
            self.schedule_checkboxes[sid] = cb
        main_layout.addWidget(cb)

        # Profile name
        name_label = QLabel(f"👤 {profile_name}")
        name_label.setStyleSheet(f"color: {COLORS['neon_cyan']}; font-size: 15px; font-weight: bold;")
        name_label.setFixedWidth(120)
        main_layout.addWidget(name_label)
        
        # Số nhóm - với tooltip danh sách nhóm
        group_names = [s.get('group_name', 'Unknown')[:40] for s in schedules]
        tooltip_text = "Danh sách nhóm:\n" + "\n".join([f"• {name}" for name in group_names])
        
        count_label = QLabel(f"📊 {total_count} nhóm")
        count_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px;")
        count_label.setFixedWidth(80)
        count_label.setToolTip(tooltip_text)
        count_label.setCursor(Qt.PointingHandCursor)
        main_layout.addWidget(count_label)
        
        # Thời gian
        time_display = time_slots.replace(',', ',') + 'h' if time_slots else '-'
        time_label = QLabel(f"⏰ {time_display}")
        time_label.setStyleSheet(f"color: {COLORS['neon_pink']}; font-size: 13px; font-weight: bold;")
        main_layout.addWidget(time_label, 1)
        
        # Thư mục
        folder_label = QLabel(f"📁 {folder_name}")
        folder_label.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 12px;")
        folder_label.setFixedWidth(120)
        main_layout.addWidget(folder_label)

        # Toggle button
        btn_text = "⏸ Tạm dừng" if active_count > 0 else "▶ Kích hoạt"
        btn_variant = "warning" if active_count > 0 else "primary"
        toggle_btn = CyberButton(btn_text, btn_variant)
        toggle_btn.setFixedSize(110, 35)
        # Fix lambda capture
        ids_copy = list(schedule_ids)
        toggle_btn.clicked.connect(lambda checked=False, ids=ids_copy: self._toggle_profile_schedules(ids))
        main_layout.addWidget(toggle_btn)

        return card
    
    def _toggle_profile_schedules(self, schedule_ids: List[int]):
        """Toggle trạng thái tất cả schedules của 1 profile"""
        print(f"[Schedule] Toggle schedules: {schedule_ids}")
        # Check current status
        schedules = [s for s in self.schedules if s.get('id') in schedule_ids]
        has_active = any(s.get('status') == 'active' for s in schedules)
        new_status = 'paused' if has_active else 'active'
        print(f"[Schedule] New status: {new_status}")
        
        for sid in schedule_ids:
            update_schedule(sid, {'status': new_status})
        self._load_schedules()
        self.log(f"Đã {'tạm dừng' if new_status == 'paused' else 'kích hoạt'} {len(schedule_ids)} lịch", "success")
        
        # Tự động bật scheduler nếu có lịch active
        if new_status == 'active' and not self._scheduler_running:
            self._start_scheduler()
            self.log("Scheduler đã được tự động bật", "info")

    def _toggle_scheduler(self):
        """Bật/tắt scheduler"""
        if self._scheduler_running:
            self._stop_scheduler()
        else:
            self._start_scheduler()

    def _start_scheduler(self):
        """Bắt đầu scheduler background"""
        if self._scheduler_running:
            return

        self._scheduler_running = True
        # Set = giờ hiện tại để KHÔNG chạy ngay, đợi đến giờ tiếp theo
        self._last_check_hour = datetime.now().hour
        self.log(f"Scheduler đã bật - sẽ chạy vào giờ {(self._last_check_hour + 1) % 24}h", "success")

        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()

    def _stop_scheduler(self):
        """Dừng scheduler"""
        self._scheduler_running = False
        self.log("Scheduler đã tắt", "info")

    def _scheduler_loop(self):
        """Vòng lặp scheduler - kiểm tra mỗi phút"""
        
        while self._scheduler_running:
            now = datetime.now()
            current_hour = now.hour
            current_minute = now.minute
            current_day = now.weekday()  # 0 = Monday

            self.signal.progress_update.emit(f"Kiểm tra: {now.strftime('%H:%M:%S')}")
            
            # Chỉ check 1 lần mỗi giờ (tránh chạy lại)
            if current_hour == self._last_check_hour:
                # Đợi đến phút tiếp theo
                for _ in range(60):
                    if not self._scheduler_running:
                        break
                    time.sleep(1)
                continue
            
            self._last_check_hour = current_hour
            self.signal.log_message.emit(f"Đang check lịch đăng cho giờ {current_hour}h...", "info")

            # Refresh schedules từ DB
            try:
                active_schedules = get_schedules(status='active')
            except:
                active_schedules = []
            
            self.signal.log_message.emit(f"Có {len(active_schedules)} lịch active", "info")

            for schedule in active_schedules:
                sched_name = schedule.get('name', 'Unknown')
                
                # Check time slot
                time_slots = schedule.get('time_slots', '')
                if not time_slots:
                    self.signal.log_message.emit(f"Bỏ qua lịch {sched_name} - không có time_slots", "info")
                    continue

                hours = [int(h) for h in time_slots.split(',') if h.strip().isdigit()]
                if current_hour not in hours:
                    self.signal.log_message.emit(f"Bỏ qua lịch {sched_name} - giờ {current_hour} không trong {hours[:5]}...", "info")
                    continue

                # Check day of week
                days_str = schedule.get('days_of_week', '')
                if days_str:
                    days = [int(d) for d in days_str.split(',') if d.strip().isdigit()]
                    if current_day not in days:
                        self.signal.log_message.emit(f"Bỏ qua lịch {schedule.get('name', 'Unknown')} - không đúng ngày", "info")
                        continue

                # Check last run - không chạy lại trong cùng 1 giờ
                last_run_str = schedule.get('last_run')
                if last_run_str:
                    try:
                        last_run = datetime.strptime(last_run_str, '%Y-%m-%d %H:%M:%S')
                        if last_run.hour == current_hour and last_run.date() == now.date():
                            self.signal.log_message.emit(f"Bỏ qua lịch {schedule.get('name', 'Unknown')} - đã chạy giờ này", "info")
                            continue  # Đã chạy trong giờ này rồi
                    except:
                        pass

                # Execute schedule
                self.signal.log_message.emit(
                    f"🚀 Thực thi lịch: {schedule.get('name', schedule.get('group_name', 'Unknown'))}", "success"
                )
                self._execute_schedule(schedule)

            # Wait 60 seconds before next check
            for _ in range(60):
                if not self._scheduler_running:
                    break
                time.sleep(1)

    def _execute_schedule(self, schedule: Dict):
        """Thực thi schedule - gọi logic đăng bài từ groups_page"""
        import os
        import glob
        import re as regex_module
        
        schedule_id = schedule.get('id')
        profile_uuid = schedule.get('profile_uuid')
        group_url = schedule.get('group_url', '')
        group_name = schedule.get('group_name', 'Unknown')
        content_id = schedule.get('content_id')
        category_id = schedule.get('content_category_id') or schedule.get('category_id')
        image_folder = schedule.get('image_folder', '')
        random_images = schedule.get('random_images', [])

        if not profile_uuid or not group_url:
            self.signal.log_message.emit("Thiếu thông tin profile/nhóm", "error")
            return

        # Extract Facebook group ID từ URL
        fb_group_id = ''
        match = regex_module.search(r'/groups/(\d+)', group_url)
        if match:
            fb_group_id = match.group(1)
        else:
            # Thử lấy slug
            match = regex_module.search(r'/groups/([^/\?]+)', group_url)
            if match:
                fb_group_id = match.group(1)
        
        self.signal.log_message.emit(f"Group ID: {fb_group_id}, URL: {group_url[:50]}", "info")

        # 1. Lấy content random từ category
        try:
            contents = get_contents()
            content = None
            
            if content_id:
                content = next((c for c in contents if c.get('id') == content_id), None)
            elif category_id:
                contents_in_cat = [c for c in contents if c.get('category_id') == category_id]
                if contents_in_cat:
                    content = random.choice(contents_in_cat)
                    self.signal.log_message.emit(f"Random nội dung: {content.get('title', 'Untitled')[:30]}", "info")
            
            if not content:
                self.signal.log_message.emit("Không tìm thấy nội dung", "error")
                return
        except Exception as e:
            self.signal.log_message.emit(f"Lỗi lấy nội dung: {e}", "error")
            return

        content_text = content.get('content', '')
        
        # 2. Lấy ảnh random từ folder
        images = []
        if random_images:
            images = random_images
        elif image_folder and os.path.isdir(image_folder):
            min_imgs = schedule.get('min_images', 0)
            max_imgs = schedule.get('max_images', 0)
            
            available_images = []
            for ext in ['*.jpg', '*.jpeg', '*.png', '*.gif', '*.webp']:
                available_images.extend(glob.glob(os.path.join(image_folder, ext)))
                available_images.extend(glob.glob(os.path.join(image_folder, ext.upper())))
            
            if available_images and max_imgs > 0:
                num_to_pick = random.randint(min_imgs, min(max_imgs, len(available_images)))
                images = random.sample(available_images, num_to_pick) if num_to_pick > 0 else []
                self.signal.log_message.emit(f"Random {len(images)} ảnh từ folder", "info")
        
        if not content_text and not images:
            self.signal.log_message.emit("Nội dung rỗng", "error")
            return

        # 3. Gọi logic đăng bài từ groups_page
        if self.groups_page:
            try:
                # Tạo group dict giống format của groups_page
                group_data = {
                    'group_id': fb_group_id,  # Facebook group ID (từ URL)
                    'group_name': group_name,
                    'group_url': group_url,
                    'profile_uuid': profile_uuid
                }
                
                self.signal.log_message.emit(f"group_data: {group_data}", "info")
                
                # Set thông tin cần thiết cho groups_page
                # Tạm thời set content và images
                self.groups_page._schedule_content = content_text
                self.groups_page._schedule_images = images
                self.groups_page._schedule_profile = profile_uuid
                
                # Gọi _execute_posting_for_profile với 1 group
                self.signal.log_message.emit(f"Đang đăng vào {group_name[:30]}...", "info")
                
                # Chạy trong thread riêng
                self.groups_page._execute_posting_for_profile(
                    profile_uuid=profile_uuid,
                    groups=[group_data],
                    content_to_post=content_text,
                    completed_offset=0,
                    total_groups=1
                )
                
                self.signal.log_message.emit(f"✅ Đã đăng vào: {group_name}", "success")
                
            except Exception as e:
                self.signal.log_message.emit(f"❌ Lỗi gọi groups_page: {e}", "error")
                import traceback
                traceback.print_exc()
        else:
            self.signal.log_message.emit("Không có groups_page reference!", "error")
            return

        # 4. Update schedule last_run
        if schedule_id:
            update_schedule(schedule_id, {
                'last_run': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            })

    def _run_now(self):
        """Chạy ngay - random nội dung từ category và ảnh từ folder"""
        if not self.selected_group_ids:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn nhóm để đăng!")
            return

        category_idx = self.category_combo.currentIndex()
        if category_idx <= 0:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn chuyên mục!")
            return

        category = self.categories[category_idx - 1]
        category_id = category.get('id')
        
        # Lấy danh sách nội dung trong category
        contents_in_cat = [c for c in self.contents if c.get('category_id') == category_id]
        if not contents_in_cat:
            QMessageBox.warning(self, "Lỗi", "Không có nội dung trong chuyên mục này!")
            return
        
        # Lấy cài đặt ảnh
        image_folder = self.image_folder_input.text().strip()
        min_images = self.min_images.value()
        max_images = self.max_images.value()
        
        self.log(f"Đang đăng vào {len(self.selected_group_ids)} nhóm...", "info")

        # Run in background
        threading.Thread(
            target=self._execute_run_now,
            args=(contents_in_cat, image_folder, min_images, max_images),
            daemon=True
        ).start()

    def _execute_run_now(self, contents_list: List[Dict], image_folder: str, min_images: int, max_images: int):
        """Thực thi đăng ngay - random nội dung và ảnh"""
        import os
        import glob
        
        total = len(self.selected_group_ids)
        delay = self.delay_minutes.value() * 60
        
        # Lấy danh sách ảnh từ folder
        available_images = []
        if image_folder and os.path.isdir(image_folder):
            for ext in ['*.jpg', '*.jpeg', '*.png', '*.gif', '*.webp']:
                available_images.extend(glob.glob(os.path.join(image_folder, ext)))
                available_images.extend(glob.glob(os.path.join(image_folder, ext.upper())))
        
        print(f"[Schedule] Found {len(available_images)} images in folder")

        for idx, group_id in enumerate(self.selected_group_ids):
            group = next((g for g in self.groups if g.get('id') == group_id), None)
            if not group:
                continue

            self.signal.progress_update.emit(f"Đang đăng {idx+1}/{total}...")
            
            # Random chọn 1 nội dung từ danh sách
            content = random.choice(contents_list)
            content_text = content.get('content', '')
            
            # Random số lượng ảnh
            num_images = random.randint(min_images, max_images)
            random_images = []
            if available_images and num_images > 0:
                num_to_pick = min(num_images, len(available_images))
                random_images = random.sample(available_images, num_to_pick)
            
            print(f"[Schedule] Using content: {content.get('title', 'Untitled')[:30]}")
            print(f"[Schedule] Random {len(random_images)} images")

            # Create temp schedule với random content và images
            schedule = {
                'id': 0,
                'profile_uuid': group.get('profile_uuid'),
                'group_url': group.get('group_url') or group.get('url', ''),
                'group_name': group.get('group_name') or group.get('name', ''),
                'content_id': content.get('id'),
                'random_images': random_images  # Ảnh random
            }

            self._execute_schedule(schedule)

            # Delay between posts
            if idx < total - 1:
                self.signal.progress_update.emit(f"Đợi {self.delay_minutes.value()} phút...")
                time.sleep(delay)

        self.signal.progress_update.emit("Hoàn tất!")
        self.signal.log_message.emit(f"Đã đăng xong {total} nhóm", "success")

    def _on_progress_update(self, message):
        """Slot cập nhật progress"""
        self.progress_label.setText(message)

    def _on_schedule_status(self, schedule_id, status):
        """Slot cập nhật status của schedule"""
        self._load_schedules()

    # ============ CDP HELPER METHODS ============

    def _cdp_send(self, ws, method: str, params: dict = None) -> dict:
        """Gửi CDP command và nhận response"""
        import json as json_module
        if not ws:
            return {"error": "No WebSocket connection", "ws_closed": True}

        if not hasattr(self, '_cdp_id'):
            self._cdp_id = 0
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
        import requests
        try:
            resp = requests.get(f"{cdp_base}/json/version", timeout=3)
            return resp.status_code == 200
        except:
            return False

    def _get_or_create_ws(self, ws, cdp_base: str, target_url: str = None):
        """Kiểm tra WS hiện tại, nếu không ok thì tạo tab mới"""
        import websocket
        import requests
        
        if self._is_ws_connected(ws):
            return (ws, True)

        if not self._is_browser_alive(cdp_base):
            return (None, False)

        try:
            resp = requests.get(f"{cdp_base}/json", timeout=10)
            pages = resp.json()
            for p in pages:
                if p.get('type') == 'page':
                    ws_url = p.get('webSocketDebuggerUrl')
                    if ws_url:
                        try:
                            new_ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
                            return (new_ws, True)
                        except:
                            pass
        except:
            pass

        return (ws, False)

    def _close_old_tabs(self, cdp_base: str):
        """Đóng hết tab cũ, giữ lại 1 tab"""
        import websocket
        import requests
        import json as json_module
        try:
            resp = requests.get(f"{cdp_base}/json", timeout=10)
            all_pages = resp.json()
            page_targets = [p for p in all_pages if p.get('type') == 'page']

            if len(page_targets) > 0:
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
                    except:
                        pass

                if len(page_targets) > 1:
                    for p in page_targets[1:]:
                        target_id = p.get('id')
                        if target_id:
                            requests.get(f"{cdp_base}/json/close/{target_id}", timeout=5)
        except:
            pass

    def _type_like_human(self, ws, text: str) -> bool:
        """Gõ từng ký tự như người thật"""
        for char in text:
            result = self._cdp_send(ws, "Input.insertText", {"text": char})
            if result.get('ws_closed'):
                return False
            time.sleep(random.uniform(0.05, 0.15))
        return True

    def _scroll_page(self, ws, direction: str = "down", amount: int = None):
        """Scroll trang như người thật"""
        if amount is None:
            amount = random.randint(200, 500)
        if direction == "up":
            amount = -amount
        self._cdp_evaluate(ws, f"window.scrollBy(0, {amount})")
