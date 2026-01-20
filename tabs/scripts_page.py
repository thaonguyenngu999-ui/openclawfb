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
    QScrollArea, QMessageBox, QSpinBox, QProgressBar, QTabWidget
)
from PySide6.QtCore import Qt, QTimer, Signal, QObject

from config import COLORS
from widgets import (
    CyberButton, CyberInput, CyberComboBox, CyberCard,
    CyberTitle, CyberStatCard, CyberCheckBox
)
from db import (
    get_schedules, save_schedule, delete_schedule, update_schedule,
    get_groups_for_profiles, get_contents
)
from api_service import api
from automation.window_manager import acquire_window_slot, release_window_slot, get_window_bounds
from automation import CDPHelper


class ScriptsSignal(QObject):
    """Signal để thread-safe UI update"""
    folders_loaded = Signal(list)
    profiles_loaded = Signal(list)
    groups_loaded = Signal(list)
    contents_loaded = Signal(list)
    schedules_loaded = Signal(list)
    log_message = Signal(str, str)
    progress_update = Signal(str)
    schedule_status = Signal(int, str)  # schedule_id, new_status


class ScriptsPage(QWidget):
    """Scripts Page - Lịch đăng tự động"""

    def __init__(self, log_func, parent=None):
        super().__init__(parent)
        self.log = log_func

        # Data
        self.folders: List[Dict] = []
        self.profiles: List[Dict] = []
        self.groups: List[Dict] = []
        self.contents: List[Dict] = []
        self.schedules: List[Dict] = []

        # Selection state
        self.selected_profile_uuids: List[str] = []
        self.selected_group_ids: List[int] = []
        self.profile_vars: Dict[str, bool] = {}  # uuid -> selected
        self.group_vars: Dict[int, bool] = {}     # id -> selected

        # Scheduler state
        self._scheduler_running = False
        self._scheduler_thread: Optional[threading.Thread] = None

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
        title = CyberTitle("Lịch đăng", "Đăng bài tự động theo lịch", "cyan")
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

        # Content selection
        content_label = QLabel("📝 Chọn nội dung:")
        content_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px; font-weight: bold;")
        right_layout.addWidget(content_label)

        self.content_combo = CyberComboBox(["-- Chọn nội dung --"])
        right_layout.addWidget(self.content_combo)

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

        self.btn_start = CyberButton("Bật scheduler", "primary", "▶️")
        self.btn_start.clicked.connect(self._toggle_scheduler)
        btn_row.addWidget(self.btn_start)

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

        btn_refresh = CyberButton("", "ghost", "🔄")
        btn_refresh.setFixedWidth(40)
        btn_refresh.clicked.connect(self._load_schedules)
        header.addWidget(btn_refresh)

        layout.addLayout(header)

        # Schedule list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"""
            QScrollArea {{
                background: {COLORS['bg_darker']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
        """)

        self.schedule_list = QWidget()
        self.schedule_list_layout = QVBoxLayout(self.schedule_list)
        self.schedule_list_layout.setContentsMargins(8, 8, 8, 8)
        self.schedule_list_layout.setSpacing(4)
        self.schedule_list_layout.addStretch()

        scroll.setWidget(self.schedule_list)
        layout.addWidget(scroll)

        return widget

    def _load_initial_data(self):
        """Load dữ liệu ban đầu"""
        self._load_folders()
        self._load_contents()
        self._load_schedules()

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
        self.profile_vars[uuid] = (state == Qt.Checked)
        self._update_profile_count()
        self._load_groups()

    def _toggle_all_profiles(self, state):
        """Toggle chọn tất cả profiles"""
        select_all = (state == Qt.Checked)
        for uuid in self.profile_vars:
            self.profile_vars[uuid] = select_all
        self._render_profiles()
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
            for group in self.groups:
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
        self.group_vars[gid] = (state == Qt.Checked)
        self._update_group_count()

    def _toggle_all_groups(self, state):
        """Toggle chọn tất cả groups"""
        select_all = (state == Qt.Checked)
        for gid in self.group_vars:
            self.group_vars[gid] = select_all
        self._render_groups()

    def _update_group_count(self):
        """Cập nhật số groups đã chọn"""
        count = sum(1 for v in self.group_vars.values() if v)
        self.group_count_label.setText(f"{count} nhóm")
        self.selected_group_ids = [i for i, v in self.group_vars.items() if v]

    def _load_contents(self):
        """Load nội dung từ Soạn tin"""
        def fetch():
            try:
                return get_contents()
            except Exception as e:
                print(f"[Scripts] Error loading contents: {e}")
                return []

        def run():
            result = fetch()
            self.signal.contents_loaded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_contents_loaded(self, contents):
        """Slot nhận contents"""
        self.contents = contents or []

        self.content_combo.clear()
        self.content_combo.addItem("-- Chọn nội dung --")
        for c in self.contents:
            title = c.get('title', 'Untitled')[:40]
            self.content_combo.addItem(f"📝 {title}")

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
        """Tạo lịch đăng mới"""
        # Validate
        if not self.selected_group_ids:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn ít nhất 1 nhóm!")
            return

        content_idx = self.content_combo.currentIndex()
        if content_idx <= 0:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn nội dung!")
            return

        selected_hours = [h for h, cb in self.time_vars.items() if cb.isChecked()]
        if not selected_hours:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn ít nhất 1 khung giờ!")
            return

        selected_days = [d for d, cb in self.day_vars.items() if cb.isChecked()]
        if not selected_days:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn ít nhất 1 ngày!")
            return

        # Get content
        content = self.contents[content_idx - 1]

        # Create schedule for each group
        for group_id in self.selected_group_ids:
            group = next((g for g in self.groups if g.get('id') == group_id), None)
            if not group:
                continue

            schedule_data = {
                'profile_uuid': group.get('profile_uuid'),
                'group_id': group_id,
                'group_name': group.get('group_name') or group.get('name', ''),
                'group_url': group.get('group_url') or group.get('url', ''),
                'content_id': content.get('id'),
                'content_title': content.get('title', ''),
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
            for schedule in self.schedules:
                row = self._create_schedule_row(schedule)
                self.schedule_list_layout.addWidget(row)

        self.schedule_list_layout.addStretch()

    def _create_schedule_row(self, schedule: Dict):
        """Tạo row cho schedule"""
        sid = schedule.get('id', 0)
        status = schedule.get('status', 'active')

        row = QWidget()
        bg_color = COLORS['bg_card']
        if status == 'paused':
            bg_color = "#3a3a2a"
        elif status == 'completed':
            bg_color = "#1a472a"
        row.setStyleSheet(f"background: {bg_color}; border-radius: 6px;")
        row.setFixedHeight(70)

        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(12, 8, 12, 8)

        # Status icon
        status_icons = {'active': '✅', 'paused': '⏸️', 'completed': '✓', 'error': '❌'}
        icon_label = QLabel(status_icons.get(status, '❓'))
        icon_label.setStyleSheet("font-size: 18px;")
        icon_label.setFixedWidth(30)
        row_layout.addWidget(icon_label)

        # Info
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)

        group_name = schedule.get('group_name', 'Unknown')[:40]
        name_label = QLabel(f"👥 {group_name}")
        name_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 12px; font-weight: bold;")
        info_layout.addWidget(name_label)

        content_title = schedule.get('content_title', '')[:30]
        content_label = QLabel(f"📝 {content_title}")
        content_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 11px;")
        info_layout.addWidget(content_label)

        time_slots = schedule.get('time_slots', '')
        time_label = QLabel(f"⏰ {time_slots}h")
        time_label.setStyleSheet(f"color: {COLORS['neon_cyan']}; font-size: 10px;")
        info_layout.addWidget(time_label)

        row_layout.addLayout(info_layout, 1)

        # Buttons
        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(4)

        if status == 'active':
            btn_pause = CyberButton("⏸️", "ghost")
            btn_pause.setFixedSize(32, 24)
            btn_pause.clicked.connect(lambda: self._pause_schedule(sid))
            btn_layout.addWidget(btn_pause)
        else:
            btn_resume = CyberButton("▶️", "ghost")
            btn_resume.setFixedSize(32, 24)
            btn_resume.clicked.connect(lambda: self._resume_schedule(sid))
            btn_layout.addWidget(btn_resume)

        btn_delete = CyberButton("🗑️", "ghost")
        btn_delete.setFixedSize(32, 24)
        btn_delete.clicked.connect(lambda: self._delete_schedule(sid))
        btn_layout.addWidget(btn_delete)

        row_layout.addLayout(btn_layout)

        return row

    def _pause_schedule(self, schedule_id):
        """Tạm dừng schedule"""
        update_schedule(schedule_id, {'status': 'paused'})
        self._load_schedules()

    def _resume_schedule(self, schedule_id):
        """Tiếp tục schedule"""
        update_schedule(schedule_id, {'status': 'active'})
        self._load_schedules()

    def _delete_schedule(self, schedule_id):
        """Xóa schedule"""
        reply = QMessageBox.question(
            self, "Xác nhận",
            "Bạn có chắc muốn xóa lịch này?",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            delete_schedule(schedule_id)
            self._load_schedules()

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
        self.btn_start.setText("Tắt scheduler")
        self.btn_start.setVariant("danger")
        self.log("Scheduler đã bật - kiểm tra mỗi phút", "success")

        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()

    def _stop_scheduler(self):
        """Dừng scheduler"""
        self._scheduler_running = False
        self.btn_start.setText("Bật scheduler")
        self.btn_start.setVariant("primary")
        self.log("Scheduler đã tắt", "info")

    def _scheduler_loop(self):
        """Vòng lặp scheduler - kiểm tra mỗi phút"""
        while self._scheduler_running:
            now = datetime.now()
            current_hour = now.hour
            current_day = now.weekday()  # 0 = Monday

            self.signal.progress_update.emit(f"Kiểm tra: {now.strftime('%H:%M:%S')}")

            # Refresh schedules từ DB
            try:
                active_schedules = get_schedules(status='active')
            except:
                active_schedules = []

            for schedule in active_schedules:
                # Check time slot
                time_slots = schedule.get('time_slots', '')
                if not time_slots:
                    continue

                hours = [int(h) for h in time_slots.split(',') if h.strip().isdigit()]
                if current_hour not in hours:
                    continue

                # Check day of week
                days_str = schedule.get('days_of_week', '')
                if days_str:
                    days = [int(d) for d in days_str.split(',') if d.strip().isdigit()]
                    if current_day not in days:
                        continue

                # Check last run - không chạy lại trong cùng 1 giờ
                last_run_str = schedule.get('last_run')
                if last_run_str:
                    try:
                        last_run = datetime.strptime(last_run_str, '%Y-%m-%d %H:%M:%S')
                        if last_run.hour == current_hour and last_run.date() == now.date():
                            continue  # Đã chạy trong giờ này rồi
                    except:
                        pass

                # Execute schedule
                self.signal.log_message.emit(
                    f"Thực thi lịch: {schedule.get('group_name', 'Unknown')}", "info"
                )
                self._execute_schedule(schedule)

            # Wait 60 seconds before next check
            for _ in range(60):
                if not self._scheduler_running:
                    break
                time.sleep(1)

    def _execute_schedule(self, schedule: Dict):
        """Thực thi một schedule - đăng bài vào nhóm"""
        schedule_id = schedule.get('id')
        profile_uuid = schedule.get('profile_uuid')
        group_url = schedule.get('group_url', '')
        content_id = schedule.get('content_id')

        if not profile_uuid or not group_url:
            self.signal.log_message.emit("Thiếu thông tin profile/nhóm", "error")
            return

        # Get content
        try:
            contents = get_contents()
            content = next((c for c in contents if c.get('id') == content_id), None)
            if not content:
                self.signal.log_message.emit("Không tìm thấy nội dung", "error")
                return
        except Exception as e:
            self.signal.log_message.emit(f"Lỗi lấy nội dung: {e}", "error")
            return

        content_text = content.get('content', '')
        if not content_text:
            self.signal.log_message.emit("Nội dung rỗng", "error")
            return

        # Execute posting
        slot_id = acquire_window_slot()
        helper = None

        try:
            # Open browser
            result = api.open_browser(profile_uuid)
            if result.get('type') == 'error':
                self.signal.log_message.emit(f"Lỗi mở browser: {result.get('message')}", "error")
                return

            data = result.get('data', {})
            remote_port = data.get('remote_port') or data.get('port')
            ws_url = data.get('web_socket', '')

            if not remote_port:
                remote_port = result.get('remote_port')

            if not remote_port:
                self.signal.log_message.emit("Không có port", "error")
                return

            time.sleep(2)

            # Connect CDP
            helper = CDPHelper()
            if not helper.connect(remote_port=remote_port, ws_url=ws_url):
                self.signal.log_message.emit("Không kết nối được CDP", "error")
                return

            # Navigate to group
            if not helper.navigate(group_url):
                self.signal.log_message.emit("Không navigate được", "error")
                return

            helper.wait_for_page_ready(timeout_ms=10000)
            time.sleep(2)

            # Click compose box
            js_click_compose = '''
            (function() {
                var selectors = [
                    'div[role="button"][aria-label*="Viết gì đó"]',
                    'div[role="button"][aria-label*="Write something"]',
                    'span[aria-label*="Viết gì đó"]',
                    'span[aria-label*="Write something"]'
                ];
                for (var i = 0; i < selectors.length; i++) {
                    var el = document.querySelector(selectors[i]);
                    if (el && el.offsetParent) {
                        el.click();
                        return 'clicked: ' + selectors[i];
                    }
                }
                return 'not_found';
            })();
            '''
            compose_result = helper.execute_js(js_click_compose)
            self.signal.log_message.emit(f"Compose: {compose_result}", "info")

            if 'not_found' in str(compose_result):
                return

            time.sleep(2)

            # Type content
            js_focus_editor = '''
            (function() {
                var editors = document.querySelectorAll('[contenteditable="true"]');
                for (var i = 0; i < editors.length; i++) {
                    var ed = editors[i];
                    if (ed.offsetParent && ed.offsetHeight > 50) {
                        ed.click();
                        ed.focus();
                        return 'focused';
                    }
                }
                return 'no_editor';
            })();
            '''
            helper.execute_js(js_focus_editor)
            time.sleep(0.5)

            helper.type_human_like(content_text)
            time.sleep(2)

            # Click post button
            js_click_post = '''
            (function() {
                var btns = document.querySelectorAll('div[role="button"], span[role="button"]');
                for (var i = 0; i < btns.length; i++) {
                    var text = (btns[i].innerText || '').trim();
                    if (text === 'Đăng' || text === 'Post') {
                        btns[i].click();
                        return 'clicked_post';
                    }
                }
                return 'no_post_btn';
            })();
            '''
            post_result = helper.execute_js(js_click_post)
            self.signal.log_message.emit(f"Post: {post_result}", "info")

            time.sleep(5)

            # Update schedule
            update_schedule(schedule_id, {
                'last_run': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            })

            self.signal.log_message.emit(
                f"Đã đăng vào: {schedule.get('group_name', 'Unknown')}", "success"
            )

        except Exception as e:
            self.signal.log_message.emit(f"Lỗi: {str(e)}", "error")

        finally:
            if helper:
                helper.close()
            try:
                api.close_browser(profile_uuid)
            except:
                pass
            release_window_slot(slot_id)

    def _run_now(self):
        """Chạy ngay các schedule đã chọn"""
        if not self.selected_group_ids:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn nhóm để đăng!")
            return

        content_idx = self.content_combo.currentIndex()
        if content_idx <= 0:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn nội dung!")
            return

        content = self.contents[content_idx - 1]
        self.log(f"Đang đăng vào {len(self.selected_group_ids)} nhóm...", "info")

        # Run in background
        threading.Thread(
            target=self._execute_run_now,
            args=(content,),
            daemon=True
        ).start()

    def _execute_run_now(self, content: Dict):
        """Thực thi đăng ngay"""
        content_text = content.get('content', '')
        total = len(self.selected_group_ids)
        delay = self.delay_minutes.value() * 60

        for idx, group_id in enumerate(self.selected_group_ids):
            group = next((g for g in self.groups if g.get('id') == group_id), None)
            if not group:
                continue

            self.signal.progress_update.emit(f"Đang đăng {idx+1}/{total}...")

            # Create temp schedule
            schedule = {
                'id': 0,
                'profile_uuid': group.get('profile_uuid'),
                'group_url': group.get('group_url') or group.get('url', ''),
                'group_name': group.get('group_name') or group.get('name', ''),
                'content_id': content.get('id')
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
