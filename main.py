"""
FB Manager Pro - Main Application
🐱 CUTE CAT Edition - WITH REAL HIDEMIUM API 🐱
"""

import sys
import os
import threading

# Fix Windows console encoding for Vietnamese
if sys.platform == 'win32':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QFrame, QLabel, QStackedWidget, QTableWidgetItem, QMessageBox
)
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QLinearGradient

from config import COLORS, MENU_ITEMS, CYBERPUNK_QSS
from widgets import (
    CyberButton, CyberInput, CyberComboBox, CyberCard, CyberStatCard,
    CyberTitle, NavItem, CyberTerminal, CyberTable, CyberCheckBox,
    ScanlineOverlay, NeonRain, CyberGrid, PulsingDot, GlitchText, NeonFlash
)

# Import API và Database
from api_service import api
from database import sync_profiles, get_profiles as db_get_profiles, update_profile_local

# Import Tab Pages
from tabs import LoginPage, PagesPage, ReelsPage, ContentPage, GroupsPage, ScriptsPage, PostsPage, InteractionPage


class Sidebar(QWidget):
    """Sidebar COMPACT 🐱"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(200)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Logo
        logo_section = QWidget()
        logo_section.setFixedHeight(70)
        logo_layout = QHBoxLayout(logo_section)
        logo_layout.setContentsMargins(14, 12, 14, 12)
        
        logo_box = QFrame()
        logo_box.setFixedSize(40, 40)
        logo_box.setStyleSheet(f"""
            background: qlineargradient(x1:0, x2:1, stop:0 {COLORS['neon_pink']}, stop:1 {COLORS['neon_purple']});
            border-radius: 12px;
        """)
        logo_box_layout = QVBoxLayout(logo_box)
        logo_box_layout.setAlignment(Qt.AlignCenter)
        logo_text = QLabel("🐱")
        logo_text.setStyleSheet("font-size: 20px;")
        logo_text.setAlignment(Qt.AlignCenter)
        logo_box_layout.addWidget(logo_text)
        logo_layout.addWidget(logo_box)
        
        title_widget = QWidget()
        title_layout = QVBoxLayout(title_widget)
        title_layout.setContentsMargins(10, 0, 0, 0)
        title_layout.setSpacing(0)
        
        main_title = QLabel("SonCuto")
        main_title.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 14px; font-weight: bold;")
        title_layout.addWidget(main_title)
        
        sub_title = QLabel("FB MANAGER")
        sub_title.setStyleSheet(f"color: {COLORS['neon_pink']}; font-size: 9px; letter-spacing: 1px;")
        title_layout.addWidget(sub_title)
        
        logo_layout.addWidget(title_widget)
        logo_layout.addStretch()
        layout.addWidget(logo_section)
        
        divider = QFrame()
        divider.setFixedHeight(2)
        divider.setStyleSheet(f"background: qlineargradient(x1:0, x2:1, stop:0 {COLORS['neon_pink']}, stop:1 {COLORS['neon_cyan']}); margin: 0 12px;")
        layout.addWidget(divider)
        
        self.nav_items = {}
        nav_container = QWidget()
        nav_layout = QVBoxLayout(nav_container)
        nav_layout.setContentsMargins(8, 12, 8, 0)
        nav_layout.setSpacing(4)
        
        for item in MENU_ITEMS:
            nav = NavItem(item["text"], item["icon"], item["id"], item["color"])
            nav_layout.addWidget(nav)
            self.nav_items[item["id"]] = nav
        
        layout.addWidget(nav_container)
        layout.addStretch()
        
        conn_frame = QWidget()
        conn_frame.setFixedHeight(36)
        conn_layout = QHBoxLayout(conn_frame)
        conn_layout.setContentsMargins(14, 0, 14, 12)
        
        self.conn_dot = PulsingDot(COLORS['text_muted'])
        conn_layout.addWidget(self.conn_dot)
        
        self.conn_text = QLabel("CHECKING...")
        self.conn_text.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px;")
        conn_layout.addWidget(self.conn_text)
        conn_layout.addStretch()
        
        layout.addWidget(conn_frame)
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLORS['bg_darker']))
        
        gradient = QLinearGradient(self.width() - 2, 0, self.width() - 2, self.height())
        gradient.setColorAt(0, QColor(COLORS['neon_pink']))
        gradient.setColorAt(1, QColor(COLORS['neon_cyan']))
        
        painter.setPen(QPen(QBrush(gradient), 2))
        painter.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
    
    def set_connection(self, connected: bool):
        if connected:
            self.conn_dot.color = QColor(COLORS['neon_mint'])
            self.conn_text.setText("CONNECTED 😸")
            self.conn_text.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 10px;")
        else:
            self.conn_dot.color = QColor(COLORS['neon_coral'])
            self.conn_text.setText("OFFLINE 😿")
            self.conn_text.setStyleSheet(f"color: {COLORS['neon_coral']}; font-size: 10px;")


class DataLoadedSignal(QObject):
    """Signal để truyền data từ thread về main thread"""
    data_ready = Signal(dict)


class ProfilesPage(QWidget):
    """Profiles - WITH REAL HIDEMIUM API 🐱"""
    
    def __init__(self, log_func, parent=None):
        super().__init__(parent)
        self.log = log_func
        self.profiles = []
        self.folders = []
        self.folder_map = {}
        self.running_uuids = set()
        self.toggle_buttons = {}
        
        # Signal để nhận data từ thread
        self.data_signal = DataLoadedSignal()
        self.data_signal.data_ready.connect(self._on_data_loaded)
        
        self._setup_ui()
        
        # Auto refresh timer
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._auto_refresh_status)
        self.refresh_timer.start(5000)
        
        # Initial load
        QTimer.singleShot(500, self._load_data)
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)
        
        # Top bar
        top_bar = QHBoxLayout()
        top_bar.setSpacing(12)
        
        title = CyberTitle("PROFILES", "", "pink")
        top_bar.addWidget(title)
        
        top_bar.addStretch()
        
        self.stat_total = CyberStatCard("TOTAL", "0", "😺", "pink")
        self.stat_total.setFixedWidth(160)
        top_bar.addWidget(self.stat_total)
        
        self.stat_running = CyberStatCard("RUNNING", "0", "🟢", "mint")
        self.stat_running.setFixedWidth(160)
        top_bar.addWidget(self.stat_running)
        
        self.stat_folders = CyberStatCard("FOLDERS", "0", "📁", "purple")
        self.stat_folders.setFixedWidth(160)
        top_bar.addWidget(self.stat_folders)
        
        layout.addLayout(top_bar)
        
        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        
        self.search_input = CyberInput("🔍 Tìm kiếm...")
        self.search_input.setFixedWidth(200)
        self.search_input.textChanged.connect(self._filter_profiles)
        toolbar.addWidget(self.search_input)
        
        self.folder_combo = CyberComboBox(["📁 Tất cả"])
        self.folder_combo.setFixedWidth(160)
        self.folder_combo.currentIndexChanged.connect(self._filter_profiles)
        toolbar.addWidget(self.folder_combo)
        
        toolbar.addStretch()
        
        btn_refresh = CyberButton("⟳", "ghost")
        btn_refresh.setFixedWidth(40)
        btn_refresh.clicked.connect(self._refresh_status)
        toolbar.addWidget(btn_refresh)
        
        btn_sync = CyberButton("SYNC", "cyan", "🔄")
        btn_sync.clicked.connect(self._sync_profiles)
        toolbar.addWidget(btn_sync)
        
        layout.addLayout(toolbar)
        
        # Table card
        table_card = CyberCard(COLORS['neon_pink'])
        table_layout = QVBoxLayout(table_card)
        table_layout.setContentsMargins(2, 2, 2, 2)
        
        # Header
        header = QWidget()
        header.setFixedHeight(44)
        header.setStyleSheet(f"background: {COLORS['bg_darker']}; border-radius: 14px 14px 0 0;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 0, 16, 0)
        header_layout.setSpacing(12)
        
        # Select All
        select_all_widget = QWidget()
        select_all_layout = QHBoxLayout(select_all_widget)
        select_all_layout.setContentsMargins(0, 0, 0, 0)
        select_all_layout.setSpacing(8)
        
        self.select_all_cb = CyberCheckBox()
        self.select_all_cb.stateChanged.connect(self._toggle_select_all)
        select_all_layout.addWidget(self.select_all_cb)
        
        select_all_label = QLabel("Chọn tất cả")
        select_all_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        select_all_label.setCursor(Qt.PointingHandCursor)
        select_all_label.mousePressEvent = lambda e: self.select_all_cb.setChecked(not self.select_all_cb.isChecked())
        select_all_layout.addWidget(select_all_label)
        
        header_layout.addWidget(select_all_widget)
        
        sep = QFrame()
        sep.setFixedWidth(2)
        sep.setFixedHeight(24)
        sep.setStyleSheet(f"background: {COLORS['border']};")
        header_layout.addWidget(sep)
        
        header_title = QLabel("😺 PROFILES")
        header_title.setStyleSheet(f"color: {COLORS['neon_pink']}; font-size: 12px; font-weight: bold; letter-spacing: 2px;")
        header_layout.addWidget(header_title)
        
        self.count_label = QLabel("[0]")
        self.count_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        header_layout.addWidget(self.count_label)
        
        header_layout.addStretch()
        
        self.selected_label = QLabel("")
        self.selected_label.setStyleSheet(f"color: {COLORS['neon_cyan']}; font-size: 11px;")
        header_layout.addWidget(self.selected_label)
        
        table_layout.addWidget(header)
        
        # Table
        self.table = CyberTable(["✓", "ID", "NAME", "FOLDER", "ACTION"])
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 300)
        self.table.setColumnWidth(3, 150)
        self.table.setColumnWidth(4, 120)
        
        table_layout.addWidget(self.table)
        layout.addWidget(table_card, 1)
    
    def _on_data_loaded(self, result):
        """Slot nhận data từ thread - chạy trên main thread"""
        print(f"[DEBUG] _on_data_loaded called with {len(result.get('profiles', []))} profiles")
        if "error" in result:
            self.log(f"Error: {result['error']}", "error")
            return
        
        self.folders = result.get("folders", [])
        self.folder_map = {f.get('id'): f.get('name', 'Unknown') for f in self.folders}
        print(f"[DEBUG] Folder map: {self.folder_map}")
        
        self.folder_combo.clear()
        self.folder_combo.addItem("📁 Tất cả")
        for folder in self.folders:
            self.folder_combo.addItem(f"📁 {folder.get('name', 'Unknown')}")
        
        self.profiles = result.get("profiles", [])
        self.running_uuids = set(result.get("running", []))
        print(f"[DEBUG] self.profiles has {len(self.profiles)} items")
        
        sync_profiles(self.profiles)
        self._update_table()
        self._update_stats()
        
        self.log(f"Loaded {len(self.profiles)} profiles, {len(self.folders)} folders", "success")
    
    def _load_data(self):
        """Load profiles và folders từ Hidemium"""
        self.log("Loading from Hidemium...", "info")
        
        def fetch():
            try:
                print("[DEBUG] Fetching folders...")
                folders = api.get_folders(limit=100)
                print(f"[DEBUG] Got {len(folders)} folders")
                
                print("[DEBUG] Fetching profiles...")
                profiles = api.get_profiles(limit=500)
                print(f"[DEBUG] Got {len(profiles)} profiles")
                
                running = api.get_running_profiles()
                print(f"[DEBUG] Got {len(running)} running")
                return {"folders": folders, "profiles": profiles, "running": running}
            except Exception as e:
                print(f"[DEBUG] Fetch error: {str(e)}")
                import traceback
                traceback.print_exc()
                return {"error": str(e)}
        
        def run_thread():
            result = fetch()
            print("[DEBUG] Emitting signal...")
            self.data_signal.data_ready.emit(result)
            print("[DEBUG] Signal emitted")
        
        threading.Thread(target=run_thread, daemon=True).start()
    
    def _update_table(self, filtered_profiles=None):
        """Update table với profiles"""
        from widgets.cyber_widgets import ToggleButton, CyberCheckBox
        
        profiles_to_show = filtered_profiles if filtered_profiles is not None else self.profiles
        
        self.table.setRowCount(len(profiles_to_show))
        self.toggle_buttons.clear()
        
        for row, profile in enumerate(profiles_to_show):
            uuid = profile.get('uuid', '')
            name = profile.get('name', 'Unknown')
            folder_id = profile.get('folder_id')
            folder_name = self.folder_map.get(folder_id, '-')
            is_running = uuid in self.running_uuids
            
            # Checkbox
            checkbox_widget = QWidget()
            checkbox_widget.setStyleSheet("background: transparent;")
            checkbox_layout = QHBoxLayout(checkbox_widget)
            checkbox_layout.setContentsMargins(0, 0, 0, 0)
            checkbox_layout.setAlignment(Qt.AlignCenter)
            checkbox = CyberCheckBox()
            checkbox_layout.addWidget(checkbox)
            self.table.setCellWidget(row, 0, checkbox_widget)
            
            # UUID
            self.table.setItem(row, 1, QTableWidgetItem(uuid[:8] if uuid else '-'))
            
            # Name
            self.table.setItem(row, 2, QTableWidgetItem(name))
            
            # Folder
            self.table.setItem(row, 3, QTableWidgetItem(folder_name))
            
            # Toggle button
            action_widget = QWidget()
            action_widget.setStyleSheet("background: transparent;")
            action_layout = QHBoxLayout(action_widget)
            action_layout.setContentsMargins(0, 0, 0, 0)
            action_layout.setAlignment(Qt.AlignCenter)
            
            toggle_btn = ToggleButton()
            toggle_btn.set_running(is_running)
            toggle_btn.toggled_state.connect(lambda running, u=uuid: self._on_toggle_profile(u, running))
            action_layout.addWidget(toggle_btn)
            
            self.table.setCellWidget(row, 4, action_widget)
            self.toggle_buttons[uuid] = toggle_btn
        
        self.count_label.setText(f"[{len(profiles_to_show)} profiles]")
    
    def _on_toggle_profile(self, uuid: str, should_run: bool):
        if should_run:
            self._start_profile(uuid)
        else:
            self._stop_profile(uuid)
    
    def _start_profile(self, uuid: str):
        self.log(f"Starting {uuid[:8]}...", "info")
        
        def do_start():
            try:
                return api.open_browser(uuid)
            except Exception as e:
                return {"error": str(e)}
        
        def on_complete(result):
            if "error" in result or result.get("type") == "error":
                self.log(f"Failed: {result.get('title', result.get('error', 'Unknown'))}", "error")
                if uuid in self.toggle_buttons:
                    self.toggle_buttons[uuid].set_running(False)
            else:
                self.log(f"Started {uuid[:8]} 😸", "success")
                self.running_uuids.add(uuid)
                self._update_stats()
        
        def run():
            result = do_start()
            QTimer.singleShot(0, lambda: on_complete(result))
        
        threading.Thread(target=run, daemon=True).start()
    
    def _stop_profile(self, uuid: str):
        self.log(f"Stopping {uuid[:8]}...", "info")
        
        def do_stop():
            try:
                return api.close_browser(uuid)
            except Exception as e:
                return {"error": str(e)}
        
        def on_complete(result):
            if "error" in result or result.get("type") == "error":
                self.log(f"Failed: {result.get('title', result.get('error', 'Unknown'))}", "error")
                if uuid in self.toggle_buttons:
                    self.toggle_buttons[uuid].set_running(True)
            else:
                self.log(f"Stopped {uuid[:8]}", "success")
                self.running_uuids.discard(uuid)
                self._update_stats()
        
        def run():
            result = do_stop()
            QTimer.singleShot(0, lambda: on_complete(result))
        
        threading.Thread(target=run, daemon=True).start()
    
    def _sync_profiles(self):
        self.log("Syncing...", "info")
        self._load_data()
    
    def _refresh_status(self):
        self.log("Refreshing...", "info")
        
        def fetch():
            try:
                return api.get_running_profiles()
            except:
                return []
        
        def on_complete(running):
            self.running_uuids = set(running)
            for uuid, btn in self.toggle_buttons.items():
                btn.set_running(uuid in self.running_uuids)
            self._update_stats()
            self.log(f"{len(running)} running", "success")
        
        def run():
            result = fetch()
            QTimer.singleShot(0, lambda: on_complete(result))
        
        threading.Thread(target=run, daemon=True).start()
    
    def _auto_refresh_status(self):
        def fetch():
            try:
                return api.get_running_profiles()
            except:
                return []
        
        def on_complete(running):
            old = self.running_uuids.copy()
            self.running_uuids = set(running)
            if old != self.running_uuids:
                for uuid, btn in self.toggle_buttons.items():
                    btn.set_running(uuid in self.running_uuids)
                self._update_stats()
        
        def run():
            result = fetch()
            QTimer.singleShot(0, lambda: on_complete(result))
        
        threading.Thread(target=run, daemon=True).start()
    
    def _filter_profiles(self):
        search = self.search_input.text().lower()
        folder_idx = self.folder_combo.currentIndex()
        
        filtered = []
        for profile in self.profiles:
            name = profile.get('name', '').lower()
            uuid = profile.get('uuid', '').lower()
            
            if search and search not in name and search not in uuid:
                continue
            
            if folder_idx > 0:
                folder_id = profile.get('folder_id')
                if folder_idx <= len(self.folders):
                    selected = self.folders[folder_idx - 1]
                    if folder_id != selected.get('id'):
                        continue
            
            filtered.append(profile)
        
        self._update_table(filtered)
    
    def _toggle_select_all(self, state):
        checked = state == Qt.Checked
        count = 0
        for row in range(self.table.rowCount()):
            widget = self.table.cellWidget(row, 0)
            if widget:
                cb = widget.findChild(CyberCheckBox)
                if cb:
                    cb.setChecked(checked)
                    if checked:
                        count += 1
        self.selected_label.setText(f"✓ {count} đã chọn" if checked else "")
    
    def _update_stats(self):
        self.stat_total.set_value(str(len(self.profiles)))
        self.stat_running.set_value(str(len(self.running_uuids)))
        self.stat_folders.set_value(str(len(self.folders)))


class LogPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(280)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        header = QWidget()
        header.setFixedHeight(40)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 0, 12, 0)
        
        title = QLabel("😸 LOG")
        title.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 11px; font-weight: bold; letter-spacing: 2px;")
        header_layout.addWidget(title)
        header_layout.addStretch()
        
        btn_clear = CyberButton("✕", "danger")
        btn_clear.setFixedSize(28, 28)
        btn_clear.clicked.connect(self._clear)
        header_layout.addWidget(btn_clear)
        
        layout.addWidget(header)
        
        self.terminal = CyberTerminal()
        layout.addWidget(self.terminal)
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLORS['bg_darker']))
        
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0, QColor(COLORS['neon_mint']))
        gradient.setColorAt(1, QColor(COLORS['neon_purple']))
        
        painter.setPen(QPen(QBrush(gradient), 2))
        painter.drawLine(0, 0, 0, self.height())
    
    def add_line(self, message: str, level: str = "info"):
        self.terminal.add_line(message, level)
    
    def _clear(self):
        self.terminal.clear()


class StatusBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(32)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        
        self.dot = PulsingDot(COLORS['neon_mint'])
        layout.addWidget(self.dot)
        
        self.status = QLabel("CHECKING...")
        self.status.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px; font-weight: bold;")
        layout.addWidget(self.status)
        
        layout.addStretch()
        
        version = QLabel("🐱 CUTE CAT v2.0.77")
        version.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px;")
        layout.addWidget(version)
    
    def set_online(self, online: bool):
        if online:
            self.dot.color = QColor(COLORS['neon_mint'])
            self.status.setText("HIDEMIUM ONLINE")
            self.status.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 10px; font-weight: bold;")
        else:
            self.dot.color = QColor(COLORS['neon_coral'])
            self.status.setText("HIDEMIUM OFFLINE")
            self.status.setStyleSheet(f"color: {COLORS['neon_coral']}; font-size: 10px; font-weight: bold;")
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLORS['bg_darker']))
        
        gradient = QLinearGradient(0, 0, self.width(), 0)
        gradient.setColorAt(0, QColor(COLORS['neon_pink']))
        gradient.setColorAt(0.5, QColor(COLORS['neon_purple']))
        gradient.setColorAt(1, QColor(COLORS['neon_cyan']))
        
        painter.setPen(QPen(QBrush(gradient), 2))
        painter.drawLine(0, 0, self.width(), 0)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        
        self.setWindowTitle("🐱 SonCuto FB Manager - Cute Cat Edition")
        self.setMinimumSize(1200, 700)
        self.resize(1500, 850)
        
        central = QWidget()
        self.setCentralWidget(central)
        
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        
        self.sidebar = Sidebar()
        content_layout.addWidget(self.sidebar)
        
        self.main_container = QWidget()
        self.main_container.setStyleSheet(f"background: {COLORS['bg_dark']};")
        main_container_layout = QVBoxLayout(self.main_container)
        main_container_layout.setContentsMargins(0, 0, 0, 0)
        main_container_layout.setSpacing(0)
        
        self.pages = QStackedWidget()
        self.log_panel = LogPanel()
        
        # Profiles page (built-in)
        self.profiles_page = ProfilesPage(self.log)
        self.pages.addWidget(self.profiles_page)

        # Login page
        self.login_page = LoginPage(self.log)
        self.pages.addWidget(self.login_page)

        # Pages page
        self.pages_page = PagesPage(self.log)
        self.pages.addWidget(self.pages_page)

        # Reels page
        self.reels_page = ReelsPage(self.log)
        self.pages.addWidget(self.reels_page)

        # Content page
        self.content_page = ContentPage(self.log)
        self.pages.addWidget(self.content_page)

        # Groups page
        self.groups_page = GroupsPage(self.log)
        self.pages.addWidget(self.groups_page)

        # Scripts page - truyền groups_page để dùng chung logic đăng bài
        self.scripts_page = ScriptsPage(self.log, self.groups_page)
        self.pages.addWidget(self.scripts_page)

        # Posts page
        self.posts_page = PostsPage(self.log)
        self.pages.addWidget(self.posts_page)
        
        # Interaction page
        self.interaction_page = InteractionPage(self.log)
        self.pages.addWidget(self.interaction_page)
        
        main_container_layout.addWidget(self.pages)
        
        content_layout.addWidget(self.main_container, 1)
        content_layout.addWidget(self.log_panel)
        
        main_layout.addWidget(content, 1)
        
        self.status_bar = StatusBar()
        main_layout.addWidget(self.status_bar)
        
        self._setup_effects()
        
        for tab_id, nav_item in self.sidebar.nav_items.items():
            nav_item.clicked_nav.connect(self._switch_tab)
        
        self._switch_tab("profiles")
        
        self.log("System ready", "success")
        self.log("Cute Cat Edition 🐱", "info")
        
        QTimer.singleShot(500, self._check_connection)
    
    def _setup_effects(self):
        self.grid = CyberGrid(self.main_container)
        self.grid.lower()
        
        self.rain = NeonRain(self.main_container)
        self.rain.lower()
        
        self.scanlines = ScanlineOverlay(self.main_container)
        self.scanlines.raise_()
    
    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = self.main_container.width()
        h = self.main_container.height()
        self.grid.setGeometry(0, 0, w, h)
        self.rain.setGeometry(0, 0, w, h)
        self.scanlines.setGeometry(0, 0, w, h)
    
    def _switch_tab(self, tab_id: str):
        for tid, nav in self.sidebar.nav_items.items():
            nav.set_active(tid == tab_id)
        
        tab_indices = {"profiles": 0, "login": 1, "pages": 2, "reels": 3, "content": 4, "groups": 5, "scripts": 6, "posts": 7, "interaction": 8}
        self.pages.setCurrentIndex(tab_indices.get(tab_id, 0))
        
        self.log(f"→ {tab_id.upper()}", "info")
    
    def _check_connection(self):
        def check():
            return api.check_connection()
        
        def on_complete(connected):
            self.sidebar.set_connection(connected)
            self.status_bar.set_online(connected)
            
            if connected:
                self.log("Hidemium connected! 😸", "success")
            else:
                self.log("Hidemium offline 😿", "warning")
        
        def run():
            result = check()
            QTimer.singleShot(0, lambda: on_complete(result))
        
        threading.Thread(target=run, daemon=True).start()
    
    def log(self, message: str, level: str = "info"):
        self.log_panel.add_line(message, level)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(CYBERPUNK_QSS)
    
    window = MainWindow()
    window.show()
    # Khởi động OpenClaw API Server
    from openclaw_api import start_api_server_thread
    start_api_server_thread(8899)
    print("[Main] OpenClaw API Server started on http://127.0.0.1:8899")
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
