"""
Posts Page - Quản lý bài đăng và buff tương tác (Like)
PySide6 version với CDP automation thực
"""
import threading
import time
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict
from datetime import datetime, date, timedelta
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QScrollArea, QMessageBox, QSpinBox, QProgressBar
)
from PySide6.QtCore import Qt, QTimer, Signal, QObject

from config import COLORS
from widgets import (
    CyberButton, CyberInput, CyberComboBox, CyberCard,
    CyberTitle, CyberStatCard, CyberCheckBox
)
from db import get_posts, save_post, delete_post, update_post_stats, get_post_history
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


class PostsSignal(QObject):
    """Signal để thread-safe UI update"""
    folders_loaded = Signal(list)
    profiles_loaded = Signal(list)
    posts_loaded = Signal(list)
    log_message = Signal(str, str)
    progress_update = Signal(int, int, str)
    post_status_update = Signal(int, dict)


class PostsPage(QWidget):
    """Posts Page - Quản lý bài đăng và buff like"""

    def __init__(self, log_func, parent=None):
        super().__init__(parent)
        self.log = log_func
        self.posts: List[Dict] = []
        self.profiles: List[Dict] = []
        self.folders: List[Dict] = []
        self.editing_post = None

        # State
        self._is_running = False
        self._stop_requested = False

        # Post status tracking
        self.post_status = {}  # {post_id: {target, liked, completed, error}}
        self.post_vars = {}  # {post_id: selected}

        # Signal để thread-safe UI update
        self.signal = PostsSignal()
        self.signal.folders_loaded.connect(self._on_folders_loaded)
        self.signal.profiles_loaded.connect(self._on_profiles_loaded)
        self.signal.posts_loaded.connect(self._on_posts_loaded)
        self.signal.log_message.connect(lambda msg, t: self.log(msg, t))
        self.signal.progress_update.connect(self._on_progress_update)
        self.signal.post_status_update.connect(self._on_post_status_update)

        self._setup_ui()
        QTimer.singleShot(500, self._load_folders)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # Top bar
        top_bar = QHBoxLayout()
        title = CyberTitle("BÀI ĐĂNG", "", "mint")
        top_bar.addWidget(title)
        top_bar.addStretch()

        self.stat_total = CyberStatCard("BÀI", "0", "📝", "mint")
        self.stat_total.setFixedWidth(140)
        top_bar.addWidget(self.stat_total)

        self.stat_likes = CyberStatCard("LIKES", "0", "❤️", "pink")
        self.stat_likes.setFixedWidth(140)
        top_bar.addWidget(self.stat_likes)

        self.stat_profiles = CyberStatCard("PROFILES", "0", "👤", "cyan")
        self.stat_profiles.setFixedWidth(140)
        top_bar.addWidget(self.stat_profiles)

        layout.addLayout(top_bar)

        # Toolbar - folder and date filter
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        # Folder selection
        folder_label = QLabel("Thư mục:")
        folder_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        toolbar.addWidget(folder_label)

        self.folder_combo = CyberComboBox(["-- Tất cả --"])
        self.folder_combo.setFixedWidth(180)
        self.folder_combo.currentIndexChanged.connect(self._on_folder_change)
        toolbar.addWidget(self.folder_combo)

        # Date filter
        date_label = QLabel("Ngày:")
        date_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        toolbar.addWidget(date_label)

        self.date_combo = CyberComboBox(["Hôm nay", "Hôm qua", "7 ngày", "30 ngày", "Tất cả"])
        self.date_combo.setFixedWidth(120)
        self.date_combo.currentIndexChanged.connect(self._load_posts)
        toolbar.addWidget(self.date_combo)

        btn_load = CyberButton("TẢI", "cyan", "📥")
        btn_load.clicked.connect(self._load_profiles)
        toolbar.addWidget(btn_load)

        toolbar.addStretch()

        btn_refresh = CyberButton("", "ghost", "🔄")
        btn_refresh.setFixedWidth(40)
        btn_refresh.clicked.connect(self._load_posts)
        toolbar.addWidget(btn_refresh)

        layout.addLayout(toolbar)

        # Main content - 2 columns
        content = QHBoxLayout()
        content.setSpacing(12)

        # Left panel - Settings
        left_card = CyberCard(COLORS['neon_mint'])
        left_card.setFixedWidth(380)
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(12, 12, 12, 12)

        # Header - Add post
        add_label = QLabel("➕ Thêm bài đăng")
        add_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 14px; font-weight: bold;")
        left_layout.addWidget(add_label)

        # URL
        url_label = QLabel("URL bài viết:")
        url_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        left_layout.addWidget(url_label)

        self.url_input = CyberInput("https://facebook.com/...")
        left_layout.addWidget(self.url_input)

        # Title
        title_label = QLabel("Tiêu đề (tùy chọn):")
        title_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        left_layout.addWidget(title_label)

        self.title_input = CyberInput("Mô tả bài viết...")
        left_layout.addWidget(self.title_input)

        # Target likes
        likes_row = QHBoxLayout()
        likes_label = QLabel("Số like/bài:")
        likes_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        likes_label.setFixedWidth(100)
        likes_row.addWidget(likes_label)

        self.target_likes = QSpinBox()
        self.target_likes.setRange(1, 100)
        self.target_likes.setValue(5)
        self.target_likes.setStyleSheet(f"""
            QSpinBox {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_primary']};
                border: 1px solid {COLORS['border']};
                border-radius: 4px;
                padding: 6px;
            }}
        """)
        likes_row.addWidget(self.target_likes, 1)

        left_layout.addLayout(likes_row)

        # Add button
        btn_row = QHBoxLayout()

        self.btn_add = CyberButton("Thêm bài", "success", "+")
        self.btn_add.clicked.connect(self._add_post)
        btn_row.addWidget(self.btn_add)

        self.btn_clear = CyberButton("Xóa form", "secondary")
        self.btn_clear.clicked.connect(self._clear_form)
        btn_row.addWidget(self.btn_clear)

        left_layout.addLayout(btn_row)

        # Divider
        divider = QFrame()
        divider.setFixedHeight(2)
        divider.setStyleSheet(f"background: {COLORS['border']}; margin: 10px 0;")
        left_layout.addWidget(divider)

        # Buff settings
        settings_label = QLabel("⚙️ Cài đặt Like")
        settings_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 14px; font-weight: bold;")
        left_layout.addWidget(settings_label)

        # Threads
        threads_row = QHBoxLayout()
        threads_label = QLabel("Số luồng:")
        threads_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        threads_label.setFixedWidth(80)
        threads_row.addWidget(threads_label)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 10)
        self.threads_spin.setValue(3)
        self.threads_spin.setStyleSheet(f"""
            QSpinBox {{
                background: {COLORS['bg_darker']};
                color: {COLORS['text_primary']};
                border: 1px solid {COLORS['border']};
                border-radius: 4px;
                padding: 6px;
            }}
        """)
        threads_row.addWidget(self.threads_spin)
        threads_row.addStretch()

        left_layout.addLayout(threads_row)

        # Action buttons
        action_row = QHBoxLayout()

        self.btn_start = CyberButton("Bắt đầu Like", "success", "👍")
        self.btn_start.clicked.connect(self._start_liking)
        action_row.addWidget(self.btn_start)

        self.btn_stop = CyberButton("Dừng", "danger", "⏹️")
        self.btn_stop.clicked.connect(self._stop_liking)
        self.btn_stop.setEnabled(False)
        action_row.addWidget(self.btn_stop)

        left_layout.addLayout(action_row)

        # Progress
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 11px;")
        left_layout.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background: {COLORS['bg_darker']};
                border: 1px solid {COLORS['border']};
                border-radius: 4px;
                height: 12px;
            }}
            QProgressBar::chunk {{
                background: {COLORS['neon_mint']};
                border-radius: 3px;
            }}
        """)
        left_layout.addWidget(self.progress_bar)

        left_layout.addStretch()
        content.addWidget(left_card)

        # Right panel - Post list
        right_card = CyberCard(COLORS['neon_pink'])
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(12, 12, 12, 12)

        # Header
        header_row = QHBoxLayout()

        # Select all
        self.select_all_cb = CyberCheckBox()
        self.select_all_cb.stateChanged.connect(self._toggle_select_all)
        header_row.addWidget(self.select_all_cb)

        posts_label = QLabel("📋 Danh sách bài đăng")
        posts_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 14px; font-weight: bold;")
        header_row.addWidget(posts_label)

        header_row.addStretch()

        self.post_count_label = QLabel("0 bài")
        self.post_count_label.setStyleSheet(f"color: {COLORS['neon_pink']}; font-size: 12px; font-weight: bold;")
        header_row.addWidget(self.post_count_label)

        right_layout.addLayout(header_row)

        # Search
        self.search_input = CyberInput("🔍 Tìm kiếm...")
        self.search_input.textChanged.connect(self._filter_posts)
        right_layout.addWidget(self.search_input)

        # Table header
        table_header = QFrame()
        table_header.setFixedHeight(32)
        table_header.setStyleSheet(f"background: {COLORS['bg_card']}; border-radius: 4px;")
        table_header_layout = QHBoxLayout(table_header)
        table_header_layout.setContentsMargins(8, 0, 8, 0)

        headers = [("", 30), ("Link bài viết", 200), ("Mục tiêu", 60), ("Đã like", 60), ("Trạng thái", 80)]
        for text, width in headers:
            lbl = QLabel(text)
            lbl.setFixedWidth(width)
            lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px; font-weight: bold;")
            table_header_layout.addWidget(lbl)

        table_header_layout.addStretch()
        right_layout.addWidget(table_header)

        # Post list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"""
            QScrollArea {{
                background: {COLORS['bg_darker']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
        """)

        self.post_list_widget = QWidget()
        self.post_list_layout = QVBoxLayout(self.post_list_widget)
        self.post_list_layout.setContentsMargins(8, 8, 8, 8)
        self.post_list_layout.setSpacing(4)

        empty = QLabel("Chưa có bài đăng nào")
        empty.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px;")
        empty.setAlignment(Qt.AlignCenter)
        self.post_list_layout.addWidget(empty)
        self.post_list_layout.addStretch()

        scroll.setWidget(self.post_list_widget)
        right_layout.addWidget(scroll, 1)

        content.addWidget(right_card, 1)
        layout.addLayout(content, 1)

    def _load_folders(self):
        """Load folders từ Hidemium"""
        def fetch():
            try:
                folders = api.get_folders(limit=100)
                return folders
            except Exception as e:
                print(f"[PostsPage] Error loading folders: {e}")
                return []

        def run():
            result = fetch()
            self.signal.folders_loaded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_folders_loaded(self, folders):
        """Slot nhận folders từ thread"""
        self.folders = folders or []

        self.folder_combo.clear()
        self.folder_combo.addItem("-- Tất cả --")
        for f in self.folders:
            name = f.get('name', 'Unknown')
            self.folder_combo.addItem(f"📁 {name}")

        self.log(f"Đã tải {len(self.folders)} thư mục", "success")
        self._load_posts()

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
                print(f"[PostsPage] Error loading profiles: {e}")
                return []

        def run():
            result = fetch()
            self.signal.profiles_loaded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_profiles_loaded(self, profiles):
        """Slot nhận profiles từ thread"""
        self.profiles = profiles or []
        self.stat_profiles.set_value(str(len(self.profiles)))
        self.log(f"Đã tải {len(self.profiles)} profiles", "success")

    def _load_posts(self):
        """Load posts theo filter"""
        def fetch():
            filter_value = self.date_combo.currentText()
            today = date.today()

            if filter_value == "Hôm nay":
                start_date = today
            elif filter_value == "Hôm qua":
                start_date = today - timedelta(days=1)
            elif filter_value == "7 ngày":
                start_date = today - timedelta(days=7)
            elif filter_value == "30 ngày":
                start_date = today - timedelta(days=30)
            else:
                start_date = None

            # Get posts from history
            try:
                all_posts = get_post_history(limit=500)
            except:
                all_posts = get_posts()

            if start_date:
                filtered = []
                for p in all_posts:
                    post_date_str = p.get('created_at', '') or p.get('posted_at', '')
                    if post_date_str:
                        try:
                            post_date = datetime.strptime(post_date_str[:10], '%Y-%m-%d').date()
                            if filter_value == "Hôm qua":
                                if post_date == start_date:
                                    filtered.append(p)
                            else:
                                if post_date >= start_date:
                                    filtered.append(p)
                        except:
                            pass
                return filtered
            return all_posts

        def run():
            result = fetch()
            self.signal.posts_loaded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_posts_loaded(self, posts):
        """Slot nhận posts từ thread"""
        self.posts = posts or []
        target = self.target_likes.value()

        # Initialize status
        for post in self.posts:
            post_id = post.get('id', 0)
            if post_id not in self.post_status:
                self.post_status[post_id] = {
                    'target': target,
                    'liked': 0,
                    'completed': False,
                    'error': False
                }
            self.post_vars[post_id] = False

        self._render_posts()
        self._update_stats()
        self.post_count_label.setText(f"{len(self.posts)} bài")
        self.log(f"Đã tải {len(self.posts)} bài đăng", "success")

    def _render_posts(self, search_text=None):
        """Render danh sách posts"""
        while self.post_list_layout.count() > 0:
            item = self.post_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        posts_to_show = self.posts
        if search_text:
            search_lower = search_text.lower()
            posts_to_show = [p for p in self.posts
                           if search_lower in p.get('url', '').lower()
                           or search_lower in p.get('post_url', '').lower()
                           or search_lower in p.get('title', '').lower()]

        if not posts_to_show:
            empty = QLabel("Chưa có bài đăng nào")
            empty.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px;")
            empty.setAlignment(Qt.AlignCenter)
            self.post_list_layout.addWidget(empty)
        else:
            target = self.target_likes.value()
            for post in posts_to_show:
                row = self._create_post_row(post, target)
                self.post_list_layout.addWidget(row)

        self.post_list_layout.addStretch()

    def _create_post_row(self, post: Dict, target: int):
        """Tạo row cho post"""
        post_id = post.get('id', 0)
        status = self.post_status.get(post_id, {})

        row = QWidget()
        bg_color = "#1a472a" if status.get('completed') else COLORS['bg_card']
        row.setStyleSheet(f"background: {bg_color}; border-radius: 4px;")
        row.setFixedHeight(45)
        row.setProperty('post_id', post_id)

        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(8, 4, 8, 4)
        row_layout.setSpacing(8)

        # Checkbox
        cb = CyberCheckBox()
        cb.setChecked(self.post_vars.get(post_id, False))
        cb.stateChanged.connect(lambda state, pid=post_id: self._on_post_select(pid, state))
        row_layout.addWidget(cb)

        # URL
        url = post.get('post_url') or post.get('url', '')
        url_display = url[:40] + "..." if len(url) > 40 else url
        url_label = QLabel(url_display)
        url_label.setFixedWidth(200)
        url_label.setStyleSheet(f"color: {COLORS['neon_cyan']}; font-size: 11px;")
        url_label.setCursor(Qt.PointingHandCursor)
        url_label.mousePressEvent = lambda e, u=url: self._open_url(u)
        row_layout.addWidget(url_label)

        # Target
        target_label = QLabel(str(status.get('target', target)))
        target_label.setFixedWidth(60)
        target_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 11px;")
        target_label.setProperty('type', 'target')
        row_layout.addWidget(target_label)

        # Liked count
        liked_label = QLabel(str(status.get('liked', 0)))
        liked_label.setFixedWidth(60)
        liked_label.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 11px;")
        liked_label.setProperty('type', 'liked')
        row_layout.addWidget(liked_label)

        # Status
        if status.get('completed'):
            status_text = "✅ Xong"
            status_color = COLORS['neon_mint']
        elif status.get('error'):
            status_text = "❌ Lỗi"
            status_color = COLORS['neon_coral']
        else:
            status_text = "⏳ Chờ"
            status_color = COLORS['neon_yellow']

        status_label = QLabel(status_text)
        status_label.setFixedWidth(80)
        status_label.setStyleSheet(f"color: {status_color}; font-size: 10px; font-weight: bold;")
        status_label.setProperty('type', 'status')
        row_layout.addWidget(status_label)

        row_layout.addStretch()
        return row

    def _on_post_select(self, post_id, state):
        """Khi chọn/bỏ chọn post"""
        self.post_vars[post_id] = (state == Qt.CheckState.Checked or state == 2)

    def _toggle_select_all(self, state):
        """Toggle chọn tất cả"""
        select_all = (state == Qt.CheckState.Checked or state == 2)
        for post_id in self.post_vars:
            self.post_vars[post_id] = select_all
        self._render_posts()

    def _filter_posts(self, text):
        self._render_posts(text)

    def _add_post(self):
        """Thêm bài đăng mới"""
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Lỗi", "Chưa nhập URL!")
            return

        if not url.startswith('http'):
            QMessageBox.warning(self, "Lỗi", "URL không hợp lệ!")
            return

        save_post({
            'url': url,
            'post_url': url,
            'title': self.title_input.text(),
            'target_likes': self.target_likes.value(),
            'status': 'pending'
        })

        self.log(f"Đã thêm bài đăng: {url[:40]}...", "success")
        self._clear_form()
        self._load_posts()

    def _delete_post(self, post_id):
        """Xóa bài đăng"""
        reply = QMessageBox.question(
            self, "Xác nhận",
            "Bạn có chắc muốn xóa bài đăng này?",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            delete_post(post_id)
            self.log("Đã xóa bài đăng", "success")
            self._load_posts()

    def _clear_form(self):
        """Clear form"""
        self.url_input.clear()
        self.title_input.clear()
        self.target_likes.setValue(5)

    def _open_url(self, url):
        """Mở URL trong browser"""
        import webbrowser
        if url:
            webbrowser.open(url)

    def _start_liking(self):
        """Bắt đầu like bài viết"""
        if self._is_running:
            return

        # Get selected posts
        selected_posts = []
        for post in self.posts:
            post_id = post.get('id', 0)
            if self.post_vars.get(post_id, False):
                selected_posts.append(post)

        if not selected_posts:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn ít nhất 1 bài để like!")
            return

        if not self.profiles:
            QMessageBox.warning(self, "Lỗi", "Không có profile nào. Vui lòng tải profiles trước!")
            return

        # Get settings
        thread_count = self.threads_spin.value()
        like_count = self.target_likes.value()

        # Reset status for selected posts
        for post in selected_posts:
            post_id = post.get('id', 0)
            self.post_status[post_id] = {
                'target': like_count,
                'liked': 0,
                'completed': False,
                'error': False
            }

        self._is_running = True
        self._stop_requested = False
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setMaximum(len(selected_posts))
        self.progress_bar.setValue(0)

        self.log(f"Bắt đầu like {len(selected_posts)} bài với {thread_count} luồng, {like_count} like/bài", "info")

        # Start in background
        threading.Thread(
            target=self._execute_liking,
            args=(selected_posts, thread_count, like_count),
            daemon=True
        ).start()

    def _execute_liking(self, posts: List[Dict], thread_count: int, like_count: int):
        """Thực hiện like với multiple threads"""
        total_posts = len(posts)
        completed_posts = 0

        # Sắp xếp profiles theo tên
        available_profiles = sorted(self.profiles, key=lambda p: p.get('name', ''))

        self.signal.log_message.emit(f"Có {len(available_profiles)} profiles sẵn sàng", "info")

        for post in posts:
            if self._stop_requested:
                break

            post_id = post.get('id', 0)
            post_url = post.get('post_url') or post.get('url', '')

            if not post_url:
                self.signal.post_status_update.emit(post_id, {'error': True})
                completed_posts += 1
                continue

            self.signal.log_message.emit(f"Đang like: {post_url[:50]}...", "info")

            # Get profiles for this post
            profiles_to_use = available_profiles[:like_count]
            if len(profiles_to_use) < like_count:
                profiles_to_use = (available_profiles * ((like_count // len(available_profiles)) + 1))[:like_count]

            liked_count = 0
            error_occurred = False

            def like_with_profile(profile):
                return self._like_post_with_profile(profile, post_url)

            # Execute with thread pool
            with ThreadPoolExecutor(max_workers=min(thread_count, len(profiles_to_use))) as executor:
                futures = {executor.submit(like_with_profile, p): p for p in profiles_to_use}

                for future in as_completed(futures):
                    if self._stop_requested:
                        break

                    try:
                        success = future.result()
                        if success:
                            liked_count += 1
                            self.signal.post_status_update.emit(post_id, {'liked': liked_count})
                    except Exception as e:
                        error_occurred = True
                        self.signal.log_message.emit(f"Lỗi: {e}", "error")

            # Update final status
            is_completed = liked_count >= like_count
            self.signal.post_status_update.emit(post_id, {
                'completed': is_completed,
                'error': error_occurred if not is_completed else False
            })

            completed_posts += 1
            self.signal.progress_update.emit(completed_posts, total_posts, f"Đang chạy... {completed_posts}/{total_posts}")

            # Delay between posts
            if not self._stop_requested and completed_posts < total_posts:
                time.sleep(random.uniform(2, 5))

        self._is_running = False
        self.signal.progress_update.emit(total_posts, total_posts, "Hoàn tất!")
        self.signal.log_message.emit(f"Hoàn tất like {completed_posts}/{total_posts} bài", "success")

        # Re-enable buttons on main thread
        QTimer.singleShot(0, lambda: self.btn_start.setEnabled(True))
        QTimer.singleShot(0, lambda: self.btn_stop.setEnabled(False))

    def _like_post_with_profile(self, profile: Dict, post_url: str) -> bool:
        """Like bài viết với 1 profile qua CDP"""
        import requests
        import websocket
        import json as json_module

        profile_uuid = profile.get('uuid', '')
        profile_name = profile.get('name', 'Unknown')

        if not profile_uuid:
            self.signal.log_message.emit(f"[{profile_name}] Không có UUID", "error")
            return False

        slot_id = acquire_window_slot()
        helper = None

        try:
            self.signal.log_message.emit(f"[{profile_name}] Đang mở browser...", "info")

            # Mở browser
            result = api.open_browser(profile_uuid)

            # Kiểm tra lỗi
            if result.get('type') == 'error':
                err_msg = result.get('message') or result.get('title', 'Unknown error')
                self.signal.log_message.emit(f"[{profile_name}] Lỗi: {err_msg}", "error")
                return False

            # Lấy remote port
            data = result.get('data', {})
            if not isinstance(data, dict):
                data = {}

            remote_port = data.get('remote_port') or data.get('port')
            ws_url = data.get('web_socket', '') or data.get('webSocketDebuggerUrl', '')

            if not remote_port:
                remote_port = result.get('remote_port') or result.get('port')
            if not ws_url:
                ws_url = result.get('web_socket', '') or result.get('webSocketDebuggerUrl', '')

            if not remote_port and ws_url:
                match = re.search(r':(\d+)/', ws_url)
                if match:
                    remote_port = int(match.group(1))

            if not remote_port:
                self.signal.log_message.emit(f"[{profile_name}] Không có port", "error")
                return False

            self.signal.log_message.emit(f"[{profile_name}] Đã mở, port: {remote_port}", "info")

            # Set window bounds
            try:
                time.sleep(0.5)
                resp = requests.get(f"http://127.0.0.1:{remote_port}/json", timeout=5)
                tabs = resp.json()
                page_ws = None
                for tab in tabs:
                    if tab.get('type') == 'page':
                        page_ws = tab.get('webSocketDebuggerUrl')
                        break
                if page_ws:
                    ws_tmp = websocket.create_connection(page_ws, timeout=5, suppress_origin=True)
                    x, y, w, h = get_window_bounds(slot_id)
                    ws_tmp.send(json_module.dumps({"id": 1, "method": "Browser.getWindowForTarget", "params": {}}))
                    win_res = json_module.loads(ws_tmp.recv())
                    if win_res and 'result' in win_res and 'windowId' in win_res['result']:
                        window_id = win_res['result']['windowId']
                        ws_tmp.send(json_module.dumps({
                            "id": 2, "method": "Browser.setWindowBounds",
                            "params": {"windowId": window_id, "bounds": {"left": x, "top": y, "width": w, "height": h, "windowState": "normal"}}
                        }))
                        ws_tmp.recv()
                    ws_tmp.close()
            except Exception as e:
                print(f"[Posts] Window bounds error: {e}")

            time.sleep(1.0)

            # Kết nối CDP
            helper = CDPHelper()
            if not helper.connect(remote_port=remote_port, ws_url=ws_url):
                self.signal.log_message.emit(f"[{profile_name}] Không kết nối được CDP", "error")
                return False

            # Navigate đến bài viết
            if not helper.navigate(post_url):
                self.signal.log_message.emit(f"[{profile_name}] Không navigate được", "error")
                return False

            helper.wait_for_page_ready(timeout_ms=8000)

            # Click Like button
            like_result = helper.click_like_button()

            if like_result:
                self.signal.log_message.emit(f"[{profile_name}] ✓ Đã like thành công", "success")
            else:
                self.signal.log_message.emit(f"[{profile_name}] ✗ Không tìm thấy nút Like", "error")

            time.sleep(random.uniform(0.3, 0.8))
            return like_result

        except Exception as e:
            self.signal.log_message.emit(f"[{profile_name}] Lỗi: {str(e)}", "error")
            return False

        finally:
            if helper:
                helper.close()
            try:
                api.close_browser(profile_uuid)
            except:
                pass
            release_window_slot(slot_id)

    def _on_progress_update(self, current, total, message):
        """Slot cập nhật progress"""
        self.progress_bar.setValue(current)
        self.progress_label.setText(message)

    def _on_post_status_update(self, post_id, status_dict):
        """Slot cập nhật status của post"""
        if post_id in self.post_status:
            self.post_status[post_id].update(status_dict)
        self._render_posts()

    def _stop_liking(self):
        """Dừng quá trình like"""
        if self._is_running:
            self._stop_requested = True
            self.log("Đang dừng...", "info")
            self.progress_label.setText("Đang dừng...")

    def _update_stats(self):
        total = len(self.posts)
        total_likes = sum(self.post_status.get(p.get('id', 0), {}).get('liked', 0) for p in self.posts)

        self.stat_total.set_value(str(total))
        self.stat_likes.set_value(str(total_likes))

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

