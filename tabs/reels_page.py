"""
Reels Page - Đăng Reels lên Fanpage
PySide6 version - BEAUTIFUL UI like ProfilesPage
CDP automation thật cho upload video
"""
import threading
import os
import re
import time
import random
from typing import List, Dict, Optional
from datetime import datetime
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QFileDialog, QMessageBox, QTableWidgetItem, QSpinBox,
    QTextEdit, QDateTimeEdit
)
from PySide6.QtCore import Qt, QTimer, QDateTime, Signal, QObject

from config import COLORS
from widgets import (
    CyberButton, CyberInput, CyberComboBox, CyberCard,
    CyberTitle, CyberStatCard, CyberTable, CyberCheckBox
)
from api_service import api
from db import (
    get_profiles, get_pages, get_pages_for_profiles,
    get_reel_schedules, save_reel_schedule, delete_reel_schedule,
    get_posted_reels, save_posted_reel, get_posted_reels_count
)

# CDP imports
import requests
import json as json_module

try:
    from automation.window_manager import acquire_window_slot, release_window_slot, get_window_bounds
    from automation.cdp_helper import CDPHelper
    CDP_AVAILABLE = True
except ImportError:
    CDP_AVAILABLE = False
    def acquire_window_slot(): return 0
    def release_window_slot(slot_id): pass
    CDPHelper = None


class ReelsSignal(QObject):
    """Signal để thread-safe UI update"""
    folders_loaded = Signal(list)
    profiles_loaded = Signal(list)
    log_message = Signal(str, str)


class ReelsPage(QWidget):
    """Reels Page - Dang Reels - BEAUTIFUL UI"""

    def __init__(self, log_func, parent=None):
        super().__init__(parent)
        self.log = log_func
        self.folders: List[Dict] = []
        self.profiles: List[Dict] = []
        self.pages: List[Dict] = []
        self.schedules: List[Dict] = []
        self.posted_reels: List[Dict] = []

        # Selected items
        self.selected_profile_uuid = None
        self.selected_page_id = None
        self.video_path = ""

        # Running state
        self._is_posting = False
        self._stop_requested = False

        # Signal để thread-safe UI update
        self.signal = ReelsSignal()
        self.signal.folders_loaded.connect(self._on_folders_loaded)
        self.signal.profiles_loaded.connect(self._on_profiles_loaded)
        self.signal.log_message.connect(lambda msg, t: self.log(msg, t))

        self._setup_ui()
        QTimer.singleShot(500, self._load_folders)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # ========== TOP BAR ==========
        top_bar = QHBoxLayout()
        top_bar.setSpacing(12)

        title = CyberTitle("Reels", "Đăng Reels lên Fanpage", "pink")
        top_bar.addWidget(title)

        top_bar.addStretch()

        self.stat_profiles = CyberStatCard("PROFILES", "0", "📁", "pink")
        self.stat_profiles.setFixedWidth(160)
        top_bar.addWidget(self.stat_profiles)

        self.stat_pages = CyberStatCard("PAGES", "0", "📄", "purple")
        self.stat_pages.setFixedWidth(160)
        top_bar.addWidget(self.stat_pages)

        self.stat_scheduled = CyberStatCard("ĐÃ HẸN", "0", "📅", "cyan")
        self.stat_scheduled.setFixedWidth(160)
        top_bar.addWidget(self.stat_scheduled)

        self.stat_posted = CyberStatCard("ĐÃ ĐĂNG", "0", "🎬", "mint")
        self.stat_posted.setFixedWidth(160)
        top_bar.addWidget(self.stat_posted)

        layout.addLayout(top_bar)

        # ========== TOOLBAR ==========
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        # Folder selection
        self.folder_combo = CyberComboBox(["📁 Chon folder"])
        self.folder_combo.setFixedWidth(180)
        self.folder_combo.currentIndexChanged.connect(self._on_folder_change)
        toolbar.addWidget(self.folder_combo)

        btn_load = CyberButton("TAI", "cyan", "📥")
        btn_load.clicked.connect(self._load_profiles)
        toolbar.addWidget(btn_load)

        toolbar.addStretch()

        btn_refresh = CyberButton("", "ghost", "🔄")
        btn_refresh.setFixedWidth(40)
        btn_refresh.clicked.connect(self._load_history)
        toolbar.addWidget(btn_refresh)

        layout.addLayout(toolbar)

        # ========== MAIN CONTENT ==========
        content = QHBoxLayout()
        content.setSpacing(12)

        # LEFT - Create Reel Form
        left_card = CyberCard(COLORS['neon_pink'])
        left_card.setFixedWidth(380)
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(16, 16, 16, 16)
        left_layout.setSpacing(12)

        # Header
        form_header = QWidget()
        form_header.setFixedHeight(40)
        form_header.setStyleSheet(f"background: {COLORS['bg_darker']}; border-radius: 10px;")
        form_header_layout = QHBoxLayout(form_header)
        form_header_layout.setContentsMargins(12, 0, 12, 0)

        form_title = QLabel("🎬 TẠO REEL MỚI")
        form_title.setStyleSheet(f"color: {COLORS['neon_pink']}; font-size: 12px; font-weight: bold; letter-spacing: 2px;")
        form_header_layout.addWidget(form_title)
        form_header_layout.addStretch()

        left_layout.addWidget(form_header)

        # Profile selection
        profile_row = QHBoxLayout()
        profile_label = QLabel("Profile:")
        profile_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        profile_label.setFixedWidth(80)
        profile_row.addWidget(profile_label)

        self.profile_combo = CyberComboBox(["-- Chọn Profile --"])
        self.profile_combo.currentIndexChanged.connect(self._on_profile_change)
        profile_row.addWidget(self.profile_combo, 1)

        left_layout.addLayout(profile_row)

        # Page selection
        page_row = QHBoxLayout()
        page_label = QLabel("Page:")
        page_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        page_label.setFixedWidth(80)
        page_row.addWidget(page_label)

        self.page_combo = CyberComboBox(["-- Chọn Page --"])
        page_row.addWidget(self.page_combo, 1)

        left_layout.addLayout(page_row)

        # Video selection
        video_row = QHBoxLayout()
        video_label = QLabel("Video:")
        video_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        video_label.setFixedWidth(80)
        video_row.addWidget(video_label)

        self.video_input = CyberInput("Chọn file video...")
        self.video_input.setReadOnly(True)
        video_row.addWidget(self.video_input, 1)

        btn_browse = CyberButton("CHON", "purple", "📂")
        btn_browse.setFixedWidth(80)
        btn_browse.clicked.connect(self._browse_video)
        video_row.addWidget(btn_browse)

        left_layout.addLayout(video_row)

        # Caption
        caption_title = QLabel("📝 CAPTION")
        caption_title.setStyleSheet(f"color: {COLORS['neon_cyan']}; font-size: 11px; font-weight: bold; letter-spacing: 1px;")
        left_layout.addWidget(caption_title)

        self.caption_text = QTextEdit()
        self.caption_text.setFixedHeight(100)
        self.caption_text.setPlaceholderText("Nhập caption cho Reel...")
        self.caption_text.setStyleSheet(f"""
            QTextEdit {{
                background: {COLORS['bg_card']};
                color: {COLORS['text_primary']};
                border: 2px solid {COLORS['border']};
                border-radius: 10px;
                padding: 10px;
                font-size: 13px;
            }}
            QTextEdit:focus {{
                border-color: {COLORS['neon_cyan']};
            }}
        """)
        left_layout.addWidget(self.caption_text)

        # Hashtags
        hashtag_row = QHBoxLayout()
        hashtag_label = QLabel("Hashtags:")
        hashtag_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        hashtag_label.setFixedWidth(80)
        hashtag_row.addWidget(hashtag_label)

        self.hashtag_input = CyberInput("#viral #trending #reels")
        hashtag_row.addWidget(self.hashtag_input, 1)

        left_layout.addLayout(hashtag_row)

        # Schedule section
        schedule_title = QLabel("📅 HẸN GIỜ")
        schedule_title.setStyleSheet(f"color: {COLORS['neon_purple']}; font-size: 11px; font-weight: bold; letter-spacing: 1px;")
        left_layout.addWidget(schedule_title)

        schedule_row = QHBoxLayout()
        self.schedule_cb = CyberCheckBox()
        schedule_row.addWidget(self.schedule_cb)

        schedule_text = QLabel("Hẹn giờ đăng")
        schedule_text.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        schedule_row.addWidget(schedule_text)

        self.schedule_datetime = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600))
        self.schedule_datetime.setDisplayFormat("dd/MM/yyyy HH:mm")
        self.schedule_datetime.setStyleSheet(f"""
            QDateTimeEdit {{
                background: {COLORS['bg_card']};
                color: {COLORS['text_primary']};
                border: 2px solid {COLORS['border']};
                border-radius: 8px;
                padding: 6px 12px;
                font-size: 13px;
            }}
            QDateTimeEdit:focus {{
                border-color: {COLORS['neon_purple']};
            }}
        """)
        self.schedule_datetime.setEnabled(False)
        self.schedule_cb.stateChanged.connect(
            lambda state: self.schedule_datetime.setEnabled(state == Qt.Checked)
        )
        schedule_row.addWidget(self.schedule_datetime, 1)

        left_layout.addLayout(schedule_row)

        # Delay settings
        delay_row = QHBoxLayout()
        delay_label = QLabel("Delay (s):")
        delay_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        delay_label.setFixedWidth(80)
        delay_row.addWidget(delay_label)

        self.delay_min = QSpinBox()
        self.delay_min.setRange(10, 300)
        self.delay_min.setValue(30)
        self.delay_min.setStyleSheet(f"""
            QSpinBox {{
                background: {COLORS['bg_card']};
                color: {COLORS['text_primary']};
                border: 2px solid {COLORS['border']};
                border-radius: 8px;
                padding: 6px 12px;
                font-size: 13px;
            }}
            QSpinBox:focus {{
                border-color: {COLORS['neon_cyan']};
            }}
        """)
        delay_row.addWidget(self.delay_min)

        delay_to = QLabel("-")
        delay_to.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 14px;")
        delay_row.addWidget(delay_to)

        self.delay_max = QSpinBox()
        self.delay_max.setRange(10, 300)
        self.delay_max.setValue(60)
        self.delay_max.setStyleSheet(f"""
            QSpinBox {{
                background: {COLORS['bg_card']};
                color: {COLORS['text_primary']};
                border: 2px solid {COLORS['border']};
                border-radius: 8px;
                padding: 6px 12px;
                font-size: 13px;
            }}
            QSpinBox:focus {{
                border-color: {COLORS['neon_cyan']};
            }}
        """)
        delay_row.addWidget(self.delay_max)
        delay_row.addStretch()

        left_layout.addLayout(delay_row)

        left_layout.addStretch()

        # Actions
        actions_title = QLabel("🚀 HÀNH ĐỘNG")
        actions_title.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 11px; font-weight: bold; letter-spacing: 1px;")
        left_layout.addWidget(actions_title)

        btn_post = CyberButton("ĐĂNG NGAY", "success", "🚀")
        btn_post.clicked.connect(self._post_reel_now)
        left_layout.addWidget(btn_post)

        btn_schedule = CyberButton("HẸN LỊCH", "cyan", "📅")
        btn_schedule.clicked.connect(self._schedule_reel)
        left_layout.addWidget(btn_schedule)

        # Progress
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"color: {COLORS['neon_pink']}; font-size: 11px;")
        self.progress_label.setAlignment(Qt.AlignCenter)
        left_layout.addWidget(self.progress_label)

        content.addWidget(left_card)

        # RIGHT - History Table
        right_card = CyberCard(COLORS['neon_purple'])
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(2, 2, 2, 2)

        # Header
        header = QWidget()
        header.setFixedHeight(50)
        header.setStyleSheet(f"background: {COLORS['bg_darker']}; border-radius: 14px 14px 0 0;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 0, 16, 0)
        header_layout.setSpacing(12)

        # Tabs
        self.btn_scheduled = CyberButton("ĐÃ HẸN", "primary", "📅")
        self.btn_scheduled.setFixedWidth(120)
        self.btn_scheduled.clicked.connect(lambda: self._show_tab("scheduled"))
        header_layout.addWidget(self.btn_scheduled)

        self.btn_posted = CyberButton("ĐÃ ĐĂNG", "secondary", "🎬")
        self.btn_posted.setFixedWidth(120)
        self.btn_posted.clicked.connect(lambda: self._show_tab("posted"))
        header_layout.addWidget(self.btn_posted)

        sep = QFrame()
        sep.setFixedWidth(2)
        sep.setFixedHeight(24)
        sep.setStyleSheet(f"background: {COLORS['border']};")
        header_layout.addWidget(sep)

        header_title = QLabel("🎬 LỊCH SỬ REELS")
        header_title.setStyleSheet(f"color: {COLORS['neon_purple']}; font-size: 12px; font-weight: bold; letter-spacing: 2px;")
        header_layout.addWidget(header_title)

        self.count_label = QLabel("[0]")
        self.count_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        header_layout.addWidget(self.count_label)

        header_layout.addStretch()

        right_layout.addWidget(header)

        # Table
        self.table = CyberTable(["PAGE", "CAPTION", "THOI GIAN", "TRANG THAI", ""])
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(3, 100)
        self.table.setColumnWidth(4, 60)

        right_layout.addWidget(self.table)
        content.addWidget(right_card, 1)

        layout.addLayout(content, 1)

    def _load_folders(self):
        """Load folders từ Hidemium"""
        self.log("Đang tải thư mục...", "info")

        def fetch():
            try:
                folders = api.get_folders(limit=100)
                print(f"[DEBUG] ReelsPage got {len(folders)} folders")
                return folders
            except Exception as e:
                print(f"[DEBUG] ReelsPage folder error: {e}")
                return []

        def run():
            result = fetch()
            self.signal.folders_loaded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_folders_loaded(self, folders):
        """Slot nhận folders từ thread - chạy trên main thread"""
        self.folders = folders or []

        print(f"[DEBUG] _on_folders_loaded: {len(self.folders)} folders")

        self.folder_combo.clear()
        self.folder_combo.addItem("📁 Chọn folder")
        for f in self.folders:
            name = f.get('name', 'Unknown')
            self.folder_combo.addItem(f"📁 {name}")

        self.log(f"Đã tải {len(self.folders)} thư mục", "success")
        self._load_history()

    def _on_folder_change(self, index):
        if index > 0:
            self._load_profiles()

    def _load_profiles(self):
        """Load profiles từ folder"""
        folder_idx = self.folder_combo.currentIndex()
        if folder_idx <= 0:
            return

        folder = self.folders[folder_idx - 1]
        folder_id = folder.get('id')

        self.log(f"Đang tải profiles từ {folder.get('name')}...", "info")

        def fetch():
            try:
                profiles = api.get_profiles(folder_id=[folder_id], limit=500)
                print(f"[DEBUG] ReelsPage got {len(profiles)} profiles")
                return profiles
            except Exception as e:
                print(f"[DEBUG] ReelsPage profiles error: {e}")
                return []

        def run():
            result = fetch()
            self.signal.profiles_loaded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_profiles_loaded(self, profiles):
        """Slot nhận profiles từ thread - chạy trên main thread"""
        self.profiles = profiles or []

        print(f"[DEBUG] _on_profiles_loaded: {len(self.profiles)} profiles")

        self.profile_combo.clear()
        self.profile_combo.addItem("-- Chọn Profile --")
        for p in self.profiles:
            name = p.get('name', 'Unknown')
            self.profile_combo.addItem(f"👤 {name}")

        self.stat_profiles.set_value(str(len(self.profiles)))
        self.log(f"Đã tải {len(self.profiles)} profiles", "success")

    def _on_profile_change(self, index):
        """Khi thay doi profile"""
        if index <= 0:
            self.page_combo.clear()
            self.page_combo.addItem("-- Chọn Page --")
            self.selected_profile_uuid = None
            self.stat_pages.set_value("0")
            return

        profile = self.profiles[index - 1]
        self.selected_profile_uuid = profile.get('uuid')

        # Load pages for this profile
        self.pages = get_pages(self.selected_profile_uuid)

        self.page_combo.clear()
        self.page_combo.addItem("-- Chọn Page --")
        for page in self.pages:
            name = page.get('page_name', 'Unknown')
            self.page_combo.addItem(f"📄 {name}")

        self.stat_pages.set_value(str(len(self.pages)))

    def _browse_video(self):
        """Chọn file video"""
        path, _ = QFileDialog.getOpenFileName(
            self, "Chọn video",
            "", "Video Files (*.mp4 *.mov *.avi *.mkv *.webm)"
        )

        if path:
            self.video_path = path
            self.video_input.setText(os.path.basename(path))
            self.log(f"Selected: {os.path.basename(path)}", "info")

    def _validate_inputs(self):
        """Validate inputs"""
        if not self.selected_profile_uuid:
            QMessageBox.warning(self, "Loi", "Chưa chọn profile!")
            return False

        if self.page_combo.currentIndex() <= 0:
            QMessageBox.warning(self, "Loi", "Chưa chọn page!")
            return False

        if not self.video_path:
            QMessageBox.warning(self, "Loi", "Chưa chọn video!")
            return False

        if not os.path.exists(self.video_path):
            QMessageBox.warning(self, "Loi", "File video không tồn tại!")
            return False

        return True

    def _post_reel_now(self):
        """Đăng Reel ngay - sử dụng CDP automation thật"""
        if not self._validate_inputs():
            return

        if self._is_posting:
            QMessageBox.warning(self, "Thông báo", "Đang trong quá trình đăng...")
            return

        if not CDP_AVAILABLE:
            QMessageBox.warning(self, "Lỗi", "Chưa có module CDP automation!")
            return

        self._is_posting = True
        self._stop_requested = False
        self.log("Bắt đầu đăng Reel qua CDP...", "info")
        self.progress_label.setText("Đang mở browser...")

        page_idx = self.page_combo.currentIndex()
        page = self.pages[page_idx - 1] if page_idx > 0 else {}

        caption = self.caption_text.toPlainText().strip()
        hashtags = self.hashtag_input.text().strip()

        def do_post():
            try:
                reel_url = self._post_reel_to_page(page, caption, hashtags)
                QTimer.singleShot(0, lambda: self._on_post_complete(True, reel_url))
            except Exception as e:
                print(f"[ReelsPage] Error: {e}")
                import traceback
                traceback.print_exc()
                self.signal.log_message.emit(f"Lỗi: {str(e)}", "error")
                QTimer.singleShot(0, lambda: self._on_post_complete(False))

        threading.Thread(target=do_post, daemon=True).start()

    def _open_browser_with_cdp(self, profile_uuid: str, max_retries: int = 2):
        """Mở browser và kết nối CDP với logic retry"""
        for attempt in range(max_retries):
            print(f"[ReelsPage] Opening browser (attempt {attempt + 1}/{max_retries})...")
            result = api.open_browser(profile_uuid)

            status = result.get('status') or result.get('type')
            if status not in ['successfully', 'success', True]:
                if 'already' not in str(result).lower() and 'running' not in str(result).lower():
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue
                    raise Exception(f"Không mở được browser: {result}")

            data = result.get('data', {})
            remote_port = data.get('remote_port')
            ws_url = data.get('web_socket', '')

            if not remote_port:
                match = re.search(r':(\d+)/', ws_url)
                if match:
                    remote_port = int(match.group(1))

            if not remote_port:
                raise Exception("Không lấy được remote debugging port")

            print(f"[ReelsPage] Browser opened, port: {remote_port}")
            time.sleep(3)

            # Thử kết nối CDP
            cdp_base = f"http://127.0.0.1:{remote_port}"
            cdp_connected = False
            tabs = None

            for cdp_attempt in range(5):
                try:
                    resp = requests.get(f"{cdp_base}/json", timeout=10)
                    tabs = resp.json()
                    cdp_connected = True
                    break
                except Exception as e:
                    print(f"[ReelsPage] CDP retry {cdp_attempt + 1}/5: {e}")
                    time.sleep(2)

            if cdp_connected and tabs:
                print(f"[ReelsPage] CDP connected!")
                return remote_port, tabs

            # CDP fail - đóng browser và thử lại
            if attempt < max_retries - 1:
                print(f"[ReelsPage] CDP failed, retrying...")
                try:
                    api.close_browser(profile_uuid)
                    time.sleep(3)
                except:
                    pass

        raise Exception("Không kết nối được CDP")

    def _post_reel_to_page(self, page: Dict, caption: str, hashtags: str) -> Optional[str]:
        """Đăng Reels lên một Page qua CDPHelper - CDP automation thật"""
        profile_uuid = page.get('profile_uuid') or self.selected_profile_uuid
        page_id = page.get('page_id', '')
        page_name = page.get('page_name', 'Unknown')
        page_url = page.get('page_url', f"https://www.facebook.com/{page_id}")

        print(f"[ReelsPage] Đang đăng Reels lên {page_name}...")
        print(f"[ReelsPage] Video: {self.video_path}")
        print(f"[ReelsPage] Caption: {caption[:50] if caption else 'N/A'}...")

        slot_id = acquire_window_slot()
        cdp = None

        try:
            # Bước 1: Mở browser và kết nối CDP
            remote_port, tabs = self._open_browser_with_cdp(profile_uuid)

            # Tìm WebSocket URL
            page_ws = None
            for tab in tabs:
                if tab.get('type') == 'page':
                    ws_url = tab.get('webSocketDebuggerUrl', '')
                    if ws_url:
                        page_ws = ws_url
                        break

            if not page_ws:
                raise Exception("Không tìm thấy tab Facebook")

            # Bước 2: Kết nối CDPHelper
            cdp = CDPHelper()
            if not cdp.connect(remote_port=remote_port, ws_url=page_ws):
                raise Exception("Không kết nối được CDPHelper")

            print(f"[ReelsPage] CDPHelper connected!")
            QTimer.singleShot(0, lambda: self.progress_label.setText("Đang vào trang..."))

            # Bước 3: Navigate đến page
            print(f"[ReelsPage] Navigating to page: {page_url}")
            cdp.navigate(page_url)
            cdp.wait_for_page_load()
            time.sleep(3)

            # Click "Chuyển ngay" nếu có
            js_click_switch = '''
            (function() {
                var buttons = document.querySelectorAll('div[role="button"], span[role="button"]');
                for (var i = 0; i < buttons.length; i++) {
                    var btn = buttons[i];
                    var ariaLabel = btn.getAttribute('aria-label') || '';
                    var text = (btn.innerText || '').trim();
                    if (ariaLabel === 'Chuyển ngay' || text === 'Chuyển ngay' ||
                        ariaLabel === 'Switch now' || text === 'Switch now') {
                        btn.click();
                        return 'clicked_switch';
                    }
                }
                return 'no_switch';
            })();
            '''
            switch_result = cdp.execute_js(js_click_switch)
            print(f"[ReelsPage] Switch result: {switch_result}")
            if 'clicked' in str(switch_result):
                time.sleep(3)

            # Bước 4: Navigate đến Reels creator
            QTimer.singleShot(0, lambda: self.progress_label.setText("Đang vào trang tạo Reel..."))
            reels_create_url = "https://www.facebook.com/reels/create"
            print(f"[ReelsPage] Navigating to Reels creator: {reels_create_url}")
            cdp.navigate(reels_create_url)
            cdp.wait_for_page_load(timeout_ms=20000)
            time.sleep(3)

            # Bước 5: Upload video
            QTimer.singleShot(0, lambda: self.progress_label.setText("Đang upload video..."))
            print(f"[ReelsPage] Preparing to upload video...")
            video_path = self.video_path.replace('\\', '/')

            # Click vào vùng upload
            js_click_upload = '''
            (function() {
                var uploadSelectors = [
                    '[aria-label*="video"]', '[aria-label*="Tải"]', '[aria-label*="Upload"]',
                    '[aria-label*="Thêm video"]', '[aria-label*="Add video"]'
                ];
                for (var i = 0; i < uploadSelectors.length; i++) {
                    try {
                        var el = document.querySelector(uploadSelectors[i]);
                        if (el) {
                            var btn = el.closest('[role="button"]') || el;
                            if (btn && btn.click) {
                                btn.click();
                                return 'clicked: ' + uploadSelectors[i];
                            }
                        }
                    } catch(e) {}
                }
                return 'no_upload_area';
            })();
            '''
            cdp.execute_js(js_click_upload)
            time.sleep(2)

            # Upload file sử dụng CDPHelper
            selectors_to_try = [
                'input[type="file"][accept*="video"]',
                'input[type="file"][accept*="mp4"]',
                'input[type="file"]'
            ]

            uploaded = False
            for selector in selectors_to_try:
                try:
                    if cdp.upload_file(selector, video_path):
                        print(f"[ReelsPage] Video uploaded via: {selector}")
                        uploaded = True
                        break
                except Exception as e:
                    print(f"[ReelsPage] Upload error with {selector}: {e}")
                    continue

            if not uploaded:
                raise Exception("Không upload được video")

            # Đợi video xử lý
            QTimer.singleShot(0, lambda: self.progress_label.setText("Đang xử lý video..."))
            print(f"[ReelsPage] Waiting for video processing...")
            time.sleep(15)

            # Bước 6: Click nút "Tiếp"
            js_click_next = '''
            (function() {
                var spans = document.querySelectorAll('span');
                for (var i = 0; i < spans.length; i++) {
                    var text = (spans[i].innerText || '').trim();
                    if (text === 'Tiếp' || text === 'Next') {
                        var clickable = spans[i].closest('div[role="none"]') ||
                                       spans[i].closest('div[role="button"]') ||
                                       spans[i].parentElement.parentElement;
                        if (clickable && clickable.offsetParent !== null) {
                            clickable.click();
                            return 'clicked: ' + text;
                        }
                    }
                }
                return 'no_next_button';
            })();
            '''

            print(f"[ReelsPage] Looking for 'Tiếp' button...")
            next_result = cdp.execute_js(js_click_next)
            print(f"[ReelsPage] First 'Tiếp': {next_result}")
            if 'clicked' in str(next_result):
                time.sleep(5)

            next_result2 = cdp.execute_js(js_click_next)
            print(f"[ReelsPage] Second 'Tiếp': {next_result2}")
            if 'clicked' in str(next_result2):
                time.sleep(5)

            # Bước 7: Nhập caption
            full_caption = f"{caption}\n\n{hashtags}" if hashtags else caption
            QTimer.singleShot(0, lambda: self.progress_label.setText("Đang nhập caption..."))

            if full_caption:
                print(f"[ReelsPage] Adding caption...")
                js_focus_caption = '''
                (function() {
                    var editors = document.querySelectorAll('[contenteditable="true"][data-lexical-editor="true"]');
                    for (var i = 0; i < editors.length; i++) {
                        var ed = editors[i];
                        var placeholder = (ed.getAttribute('aria-placeholder') || '').toLowerCase();
                        if (placeholder.includes('mô tả') || placeholder.includes('thước phim') || placeholder.includes('description')) {
                            ed.click();
                            ed.focus();
                            return 'focused';
                        }
                    }
                    // Fallback
                    var allEditors = document.querySelectorAll('[contenteditable="true"]');
                    for (var i = 0; i < allEditors.length; i++) {
                        var ed = allEditors[i];
                        var ariaLabel = (ed.getAttribute('aria-label') || '').toLowerCase();
                        if (!ariaLabel.includes('bình luận') && !ariaLabel.includes('comment')) {
                            ed.click();
                            ed.focus();
                            return 'fallback_editor';
                        }
                    }
                    return 'no_editor';
                })();
                '''
                cdp.execute_js(js_focus_caption)
                time.sleep(0.5)

                # Gõ caption
                cdp.type_human_like(full_caption)
                time.sleep(2)

            # Bước 8: Click nút đăng
            QTimer.singleShot(0, lambda: self.progress_label.setText("Đang đăng Reel..."))
            print(f"[ReelsPage] Looking for 'Đăng' button...")
            js_click_post = '''
            (function() {
                var spans = document.querySelectorAll('span');
                for (var i = 0; i < spans.length; i++) {
                    var text = (spans[i].innerText || '').trim();
                    if (text === 'Đăng' || text === 'Share' || text === 'Post') {
                        var clickable = spans[i].closest('div[role="none"]') ||
                                       spans[i].closest('div[role="button"]') ||
                                       spans[i].parentElement.parentElement;
                        if (clickable && clickable.offsetParent !== null) {
                            clickable.click();
                            return 'clicked: ' + text;
                        }
                    }
                }
                return 'no_post_button';
            })();
            '''
            click_result = cdp.execute_js(js_click_post)
            print(f"[ReelsPage] Post button: {click_result}")

            if 'no_post_button' in str(click_result):
                raise Exception("Không tìm thấy nút đăng")

            # Đợi đăng xong
            print(f"[ReelsPage] Waiting for Reel to be posted...")
            time.sleep(15)

            # Bước 9: Tìm URL của Reel vừa đăng
            js_get_reel_url = '''
            (function() {
                var links = document.querySelectorAll('a');
                for (var i = 0; i < links.length; i++) {
                    var href = links[i].href || '';
                    if (href.includes('/reel/')) {
                        var match = href.match(/\\/reel\\/(\\d{10,})/);
                        if (match) {
                            return 'https://www.facebook.com/reel/' + match[1];
                        }
                    }
                }
                return '';
            })();
            '''

            reel_url = None
            for attempt in range(10):
                result = cdp.execute_js(js_get_reel_url)
                if result and 'facebook.com/reel/' in str(result):
                    reel_url = result
                    break
                time.sleep(2)

            # Lưu vào database
            save_posted_reel({
                'profile_uuid': profile_uuid,
                'page_id': page_id,
                'page_name': page_name,
                'reel_url': reel_url or '',
                'caption': caption,
                'hashtags': hashtags,
                'video_path': self.video_path,
                'status': 'success'
            })

            print(f"[ReelsPage] SUCCESS - Đã đăng Reels lên {page_name}")
            if reel_url:
                print(f"[ReelsPage] REEL URL: {reel_url}")

            return reel_url

        except Exception as e:
            print(f"[ReelsPage] ERROR: {e}")
            import traceback
            traceback.print_exc()

            # Lưu lỗi vào DB
            try:
                save_posted_reel({
                    'profile_uuid': profile_uuid,
                    'page_id': page_id,
                    'page_name': page_name,
                    'reel_url': '',
                    'caption': caption,
                    'hashtags': hashtags,
                    'video_path': self.video_path,
                    'status': 'failed',
                    'error_message': str(e)
                })
            except:
                pass
            raise e

        finally:
            if cdp:
                cdp.close()
            release_window_slot(slot_id)

    def _on_post_complete(self, success, reel_url=None):
        self._is_posting = False

        if success:
            if reel_url:
                self.progress_label.setText(f"Đã đăng thành công!")
                self.log(f"Reel đã được đăng: {reel_url[:50]}...", "success")
            else:
                self.progress_label.setText("Đã đăng thành công!")
                self.log("Reel đã được đăng!", "success")
            self._load_history()

            # Clear form
            self.caption_text.clear()
            self.video_path = ""
            self.video_input.clear()
        else:
            self.progress_label.setText("Lỗi khi đăng!")
            self.log("Lỗi đăng Reel", "error")

    def _schedule_reel(self):
        """Hen lich dang Reel"""
        if not self._validate_inputs():
            return

        if not self.schedule_cb.isChecked():
            QMessageBox.warning(self, "Loi", "Chưa bật hẹn giờ!")
            return

        page_idx = self.page_combo.currentIndex()
        page = self.pages[page_idx - 1] if page_idx > 0 else {}

        schedule_time = self.schedule_datetime.dateTime().toPython()

        if schedule_time <= datetime.now():
            QMessageBox.warning(self, "Loi", "Thời gian hẹn phải lớn hơn hiện tại!")
            return

        save_reel_schedule({
            'profile_uuid': self.selected_profile_uuid,
            'page_id': page.get('id'),
            'page_name': page.get('page_name', ''),
            'video_path': self.video_path,
            'caption': self.caption_text.toPlainText(),
            'hashtags': self.hashtag_input.text(),
            'scheduled_time': schedule_time.isoformat(),
            'delay_min': self.delay_min.value(),
            'delay_max': self.delay_max.value()
        })

        self.log(f"Đã hẹn lịch đăng lúc {schedule_time.strftime('%d/%m/%Y %H:%M')}", "success")
        self.progress_label.setText(f"Hẹn lúc {schedule_time.strftime('%H:%M %d/%m')}")

        # Clear form
        self.caption_text.clear()
        self.video_path = ""
        self.video_input.clear()
        self.schedule_cb.setChecked(False)

        self._load_history()

    def _load_history(self):
        """Load lich su Reels"""
        self.schedules = get_reel_schedules(status='pending')
        self.posted_reels = get_posted_reels(limit=50)

        self.stat_scheduled.set_value(str(len(self.schedules)))
        self.stat_posted.set_value(str(len(self.posted_reels)))

        self._show_tab("scheduled")

    def _show_tab(self, tab: str):
        """Hien thi tab"""
        self.table.setRowCount(0)

        if tab == "scheduled":
            self.btn_scheduled.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['neon_purple']};
                    border: 2px solid {COLORS['neon_purple']};
                    color: {COLORS['bg_dark']};
                    border-radius: 8px;
                    padding: 8px 16px;
                    font-weight: bold;
                }}
            """)
            self.btn_posted.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    border: 2px solid {COLORS['border']};
                    color: {COLORS['text_secondary']};
                    border-radius: 8px;
                    padding: 8px 16px;
                }}
            """)
            items = self.schedules
            self.count_label.setText(f"[{len(items)} hen]")
        else:
            self.btn_scheduled.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    border: 2px solid {COLORS['border']};
                    color: {COLORS['text_secondary']};
                    border-radius: 8px;
                    padding: 8px 16px;
                }}
            """)
            self.btn_posted.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['neon_mint']};
                    border: 2px solid {COLORS['neon_mint']};
                    color: {COLORS['bg_dark']};
                    border-radius: 8px;
                    padding: 8px 16px;
                    font-weight: bold;
                }}
            """)
            items = self.posted_reels
            self.count_label.setText(f"[{len(items)} đã đăng]")

        self.table.setRowCount(len(items))

        for row, item in enumerate(items):
            # Page name
            page_name = item.get('page_name', 'Unknown')[:20]
            self.table.setItem(row, 0, QTableWidgetItem(f"📄 {page_name}"))

            # Caption
            caption = item.get('caption', '')[:30]
            if len(item.get('caption', '')) > 30:
                caption += "..."
            self.table.setItem(row, 1, QTableWidgetItem(caption))

            # Time
            if tab == "scheduled":
                time_str = item.get('scheduled_time', '')[:16].replace('T', ' ')
            else:
                time_str = item.get('posted_at', '')[:16].replace('T', ' ')
            self.table.setItem(row, 2, QTableWidgetItem(time_str))

            # Status
            if tab == "scheduled":
                status_text = "📅 Chờ đăng"
            else:
                status = item.get('status', '')
                if status == 'success':
                    status_text = "✅ Thành công"
                else:
                    status_text = "❌ Loi"
            self.table.setItem(row, 3, QTableWidgetItem(status_text))

            # Action button
            if tab == "scheduled":
                action_widget = QWidget()
                action_widget.setStyleSheet("background: transparent;")
                action_layout = QHBoxLayout(action_widget)
                action_layout.setContentsMargins(0, 0, 0, 0)
                action_layout.setAlignment(Qt.AlignCenter)

                btn_del = CyberButton("", "danger", "🗑️")
                btn_del.setFixedSize(32, 32)
                item_id = item.get('id')
                btn_del.clicked.connect(lambda checked, sid=item_id: self._delete_schedule(sid))
                action_layout.addWidget(btn_del)

                self.table.setCellWidget(row, 4, action_widget)
            else:
                self.table.setItem(row, 4, QTableWidgetItem(""))

    def _delete_schedule(self, schedule_id):
        """Xoa schedule"""
        if schedule_id:
            reply = QMessageBox.question(
                self, "Xác nhận",
                "Bạn có chắc muốn xóa lịch hẹn này?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )

            if reply == QMessageBox.Yes:
                delete_reel_schedule(schedule_id)
                self.log("Đã xóa lịch hẹn", "success")
                self._load_history()
