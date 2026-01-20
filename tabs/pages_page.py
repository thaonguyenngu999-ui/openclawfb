"""
Pages Page - Quan ly cac Fanpage Facebook
PySide6 version - BEAUTIFUL UI like ProfilesPage
Có hỗ trợ TẠO PAGE MỚI qua CDP automation
"""
import threading
import random
import uuid as uuid_module
from typing import List, Dict
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QMessageBox, QTableWidgetItem, QProgressBar, QDialog,
    QTextEdit, QSpinBox
)
from PySide6.QtCore import Qt, QTimer, Signal, QObject

from config import COLORS
from widgets import (
    CyberButton, CyberInput, CyberComboBox, CyberCard,
    CyberTitle, CyberStatCard, CyberTable, CyberCheckBox
)
from api_service import api
from db import (
    get_profiles, get_pages, get_pages_for_profiles, save_page,
    delete_page, delete_pages_bulk, sync_pages, clear_pages, get_pages_count
)

# Import automation modules
import requests
import websocket
import json as json_module
import re
import time

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


class PagesSignal(QObject):
    """Signal để thread-safe UI update"""
    data_loaded = Signal(dict)  # folders, profiles, pages
    profiles_loaded = Signal(list)
    log_message = Signal(str, str)
    create_progress = Signal(int, int)  # current, total
    create_complete = Signal(int)  # count created


class CreatePageDialog(QDialog):
    """Dialog tạo Page mới - PySide6 version"""

    def __init__(self, parent, profile_uuids: List[str], profiles: List[Dict], log_func):
        super().__init__(parent)
        self.profile_uuids = profile_uuids
        self.profiles = profiles
        self.log = log_func
        self._is_creating = False

        self.setWindowTitle("Tạo Fanpage mới")
        self.setFixedSize(520, 450)
        self.setStyleSheet(f"background: {COLORS['bg_dark']};")

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # Header
        header = QLabel("➕ Tạo Fanpage mới")
        header.setStyleSheet(f"color: {COLORS['neon_purple']}; font-size: 18px; font-weight: bold;")
        layout.addWidget(header)

        # Profile info
        info = QLabel(f"Sẽ tạo Page cho {len(self.profile_uuids)} profile đã chọn")
        info.setStyleSheet(f"color: {COLORS['neon_cyan']}; font-size: 12px;")
        layout.addWidget(info)

        # Form card
        form_card = CyberCard(COLORS['neon_purple'])
        form_layout = QVBoxLayout(form_card)
        form_layout.setContentsMargins(16, 16, 16, 16)
        form_layout.setSpacing(12)

        # Page name
        name_row = QHBoxLayout()
        name_label = QLabel("Tên Page:")
        name_label.setFixedWidth(100)
        name_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        name_row.addWidget(name_label)
        self.name_input = CyberInput("Nhập tên Page...")
        name_row.addWidget(self.name_input)
        form_layout.addLayout(name_row)

        # Category
        cat_row = QHBoxLayout()
        cat_label = QLabel("Danh mục:")
        cat_label.setFixedWidth(100)
        cat_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        cat_row.addWidget(cat_label)
        self.category_combo = CyberComboBox([
            "Doanh nghiệp địa phương",
            "Công ty",
            "Thương hiệu hoặc sản phẩm",
            "Nghệ sĩ, ban nhạc hoặc nhân vật công chúng",
            "Giải trí",
            "Cộng đồng hoặc trang web"
        ])
        cat_row.addWidget(self.category_combo)
        form_layout.addLayout(cat_row)

        # Description
        desc_label = QLabel("Mô tả (Tiểu sử):")
        desc_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        form_layout.addWidget(desc_label)

        self.desc_text = QTextEdit()
        self.desc_text.setPlaceholderText("Nhập mô tả cho Page...")
        self.desc_text.setFixedHeight(60)
        self.desc_text.setStyleSheet(f"""
            QTextEdit {{
                background: {COLORS['bg_card']};
                color: {COLORS['text_primary']};
                border: 2px solid {COLORS['border']};
                border-radius: 8px;
                padding: 8px;
                font-size: 12px;
            }}
            QTextEdit:focus {{
                border-color: {COLORS['neon_purple']};
            }}
        """)
        form_layout.addWidget(self.desc_text)

        # Delay
        delay_row = QHBoxLayout()
        delay_label = QLabel("Delay giữa các lần (giây):")
        delay_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        delay_row.addWidget(delay_label)
        delay_row.addStretch()
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(3, 60)
        self.delay_spin.setValue(5)
        self.delay_spin.setFixedWidth(80)
        self.delay_spin.setStyleSheet(f"""
            QSpinBox {{
                background: {COLORS['bg_card']};
                color: {COLORS['text_primary']};
                border: 2px solid {COLORS['border']};
                border-radius: 6px;
                padding: 4px;
            }}
        """)
        delay_row.addWidget(self.delay_spin)
        form_layout.addLayout(delay_row)

        layout.addWidget(form_card)

        # Progress
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 11px;")
        layout.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background: {COLORS['bg_darker']};
                border: none;
                border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, x2:1, stop:0 {COLORS['neon_purple']}, stop:1 {COLORS['neon_mint']});
                border-radius: 3px;
            }}
        """)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)

        self.btn_create = CyberButton("TẠO PAGE", "success", "➕")
        self.btn_create.clicked.connect(self._create_pages)
        btn_row.addWidget(self.btn_create)

        btn_close = CyberButton("ĐÓNG", "ghost")
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)

        btn_row.addStretch()
        layout.addLayout(btn_row)

    def _create_pages(self):
        """Tạo pages cho các profiles đã chọn"""
        if self._is_creating:
            return

        page_name = self.name_input.text().strip()
        if not page_name:
            QMessageBox.warning(self, "Lỗi", "Vui lòng nhập tên Page!")
            return

        self._is_creating = True
        self.btn_create.setEnabled(False)
        category = self.category_combo.currentText()
        description = self.desc_text.toPlainText().strip()
        delay = self.delay_spin.value()

        self.progress_bar.setMaximum(len(self.profile_uuids))
        self.progress_bar.setValue(0)

        def create():
            try:
                total = len(self.profile_uuids)
                created_count = 0

                for i, uuid in enumerate(self.profile_uuids):
                    profile = next((p for p in self.profiles if p['uuid'] == uuid), None)
                    if not profile:
                        continue

                    profile_name = profile.get('name', 'Unknown')[:20]
                    QTimer.singleShot(0, lambda n=profile_name, idx=i+1, t=total:
                        self.progress_label.setText(f"Đang tạo Page cho {n} ({idx}/{t})..."))

                    # Tạo page
                    success = self._create_page_for_profile(uuid, page_name, category, description)
                    if success:
                        created_count += 1

                    QTimer.singleShot(0, lambda v=i+1: self.progress_bar.setValue(v))

                    # Delay
                    if i < total - 1:
                        time.sleep(delay + random.uniform(0, 2))

                QTimer.singleShot(0, lambda c=created_count: self._on_create_complete(c))

            except Exception as e:
                import traceback
                traceback.print_exc()
                QTimer.singleShot(0, lambda: self.progress_label.setText(f"Lỗi: {e}"))
            finally:
                self._is_creating = False
                QTimer.singleShot(0, lambda: self.btn_create.setEnabled(True))

        threading.Thread(target=create, daemon=True).start()

    def _create_page_for_profile(self, profile_uuid: str, name: str, category: str, description: str) -> bool:
        """Tạo page cho 1 profile sử dụng CDP automation"""
        slot_id = acquire_window_slot()
        created_page_id = None

        try:
            # Mở browser
            result = api.open_browser(profile_uuid)
            print(f"[CreatePage] open_browser {profile_uuid[:8]}: {result.get('status', result.get('type', 'unknown'))}")

            status = result.get('status') or result.get('type')
            if status not in ['successfully', 'success', True]:
                if 'already' not in str(result).lower() and 'running' not in str(result).lower():
                    release_window_slot(slot_id)
                    return False

            # Lấy thông tin CDP
            data = result.get('data', {})
            remote_port = data.get('remote_port')
            ws_url = data.get('web_socket', '')

            if not remote_port:
                match = re.search(r':(\d+)/', ws_url)
                if match:
                    remote_port = int(match.group(1))

            if not remote_port:
                release_window_slot(slot_id)
                return False

            cdp_base = f"http://127.0.0.1:{remote_port}"
            time.sleep(2)

            # Lấy WebSocket
            try:
                resp = requests.get(f"{cdp_base}/json", timeout=10)
                tabs = resp.json()
            except Exception as e:
                print(f"[CreatePage] CDP error: {e}")
                return False

            page_ws = None
            for tab in tabs:
                if tab.get('type') == 'page':
                    page_ws = tab.get('webSocketDebuggerUrl')
                    break

            if not page_ws:
                return False

            # Kết nối WebSocket
            ws = None
            try:
                ws = websocket.create_connection(page_ws, timeout=30, suppress_origin=True)
            except:
                try:
                    ws = websocket.create_connection(page_ws, timeout=30)
                except:
                    return False

            if not ws:
                return False

            # Navigate đến trang tạo Page
            create_url = "https://www.facebook.com/pages/create"
            ws.send(json_module.dumps({
                "id": 1,
                "method": "Page.navigate",
                "params": {"url": create_url}
            }))
            ws.recv()
            time.sleep(6)

            # Nhập tên Page
            js_fill_name = f'''
            (function() {{
                var pageName = "{name}";
                function setValueAndTrigger(input, value) {{
                    input.focus();
                    input.click();
                    if (input.tagName === 'INPUT') {{
                        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        nativeInputValueSetter.call(input, value);
                    }} else {{
                        input.innerText = value;
                    }}
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
                var confirmedInput = document.querySelector('input.x1i10hfl[type="text"]');
                if (confirmedInput && confirmedInput.offsetParent !== null) {{
                    setValueAndTrigger(confirmedInput, pageName);
                    return 'filled_name';
                }}
                var inputs = document.querySelectorAll('input[type="text"], input:not([type])');
                for (var i = 0; i < inputs.length; i++) {{
                    if (inputs[i].offsetParent !== null && !inputs[i].value) {{
                        setValueAndTrigger(inputs[i], pageName);
                        return 'filled_first_input';
                    }}
                }}
                return 'no_name_input';
            }})();
            '''
            ws.send(json_module.dumps({"id": 10, "method": "Runtime.evaluate", "params": {"expression": js_fill_name}}))
            ws.recv()
            time.sleep(1.5)

            # Nhập Category
            js_fill_category = f'''
            (function() {{
                var categoryText = "{category}";
                function setValueAndTrigger(input, value) {{
                    input.focus();
                    input.click();
                    var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(input, value);
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
                var categoryInput = document.querySelector('input[aria-label="Hạng mục (Bắt buộc)"]');
                if (categoryInput && categoryInput.offsetParent !== null) {{
                    setValueAndTrigger(categoryInput, categoryText);
                    return 'filled_category';
                }}
                var searchInputs = document.querySelectorAll('input[type="search"]');
                for (var i = 0; i < searchInputs.length; i++) {{
                    var ariaLabel = searchInputs[i].getAttribute('aria-label') || '';
                    if (!ariaLabel.includes('Tìm kiếm') && searchInputs[i].offsetParent !== null) {{
                        setValueAndTrigger(searchInputs[i], categoryText);
                        return 'filled_search_input';
                    }}
                }}
                return 'no_category_input';
            }})();
            '''
            ws.send(json_module.dumps({"id": 11, "method": "Runtime.evaluate", "params": {"expression": js_fill_category}}))
            ws.recv()
            time.sleep(2)

            # ArrowDown + Enter để chọn suggestion
            js_select = '''
            (function() {
                var input = document.querySelector('input[aria-label="Hạng mục (Bắt buộc)"]') || document.activeElement;
                if (input && input.tagName === 'INPUT') {
                    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', code: 'ArrowDown', keyCode: 40, bubbles: true}));
                    return 'arrow_down';
                }
                return 'no_input';
            })();
            '''
            ws.send(json_module.dumps({"id": 12, "method": "Runtime.evaluate", "params": {"expression": js_select}}))
            ws.recv()
            time.sleep(0.5)

            js_enter = '''
            (function() {
                var input = document.querySelector('input[aria-label="Hạng mục (Bắt buộc)"]') || document.activeElement;
                if (input) {
                    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
                    return 'enter';
                }
                return 'no_input';
            })();
            '''
            ws.send(json_module.dumps({"id": 13, "method": "Runtime.evaluate", "params": {"expression": js_enter}}))
            ws.recv()
            time.sleep(1.5)

            # Điền Bio nếu có
            if description:
                escaped_desc = description.replace('"', '\\"').replace('\n', '\\n')
                js_fill_bio = f'''
                (function() {{
                    var bioText = "{escaped_desc}";
                    function setTextareaValue(textarea, value) {{
                        textarea.focus();
                        textarea.click();
                        var setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        setter.call(textarea, value);
                        textarea.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        textarea.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                    var bioTextarea = document.querySelector('textarea.x1i10hfl');
                    if (bioTextarea && bioTextarea.offsetParent !== null) {{
                        setTextareaValue(bioTextarea, bioText);
                        return 'filled_bio';
                    }}
                    return 'no_bio_textarea';
                }})();
                '''
                ws.send(json_module.dumps({"id": 15, "method": "Runtime.evaluate", "params": {"expression": js_fill_bio}}))
                ws.recv()
                time.sleep(1)

            # Click nút Tạo Trang
            js_click_create = '''
            (function() {
                var buttons = document.querySelectorAll('div[role="button"], button, span[role="button"]');
                for (var i = 0; i < buttons.length; i++) {
                    var btn = buttons[i];
                    var text = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                    var ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                    if (text === 'create page' || text === 'tạo trang' ||
                        ariaLabel.includes('create page') || ariaLabel.includes('tạo trang')) {
                        btn.scrollIntoView({behavior: 'smooth', block: 'center'});
                        setTimeout(function() { btn.click(); }, 300);
                        return 'clicked: ' + text;
                    }
                }
                return 'no_create_button';
            })();
            '''
            ws.send(json_module.dumps({"id": 16, "method": "Runtime.evaluate", "params": {"expression": js_click_create}}))
            ws.recv()

            # Đợi page được tạo
            page_url = ""
            for wait_attempt in range(15):
                time.sleep(2)
                ws.send(json_module.dumps({
                    "id": 20 + wait_attempt,
                    "method": "Runtime.evaluate",
                    "params": {"expression": "window.location.href"}
                }))
                result = json_module.loads(ws.recv())
                current_url = result.get('result', {}).get('result', {}).get('value', '')
                print(f"[CreatePage] URL check {wait_attempt + 1}: {current_url}")

                if '/pages/create' not in current_url:
                    page_url = current_url
                    print(f"[CreatePage] Page created! URL: {page_url}")
                    break

            # Lấy page ID từ URL
            if page_url:
                match = re.search(r'id=(\d+)', page_url)
                if not match:
                    match = re.search(r'facebook\.com/(\d{10,})', page_url)
                if not match:
                    match = re.search(r'facebook\.com/([^/?]+)', page_url)
                if match:
                    created_page_id = match.group(1)

            ws.close()

            # Lưu vào database
            if created_page_id and page_url:
                page_data = {
                    'profile_uuid': profile_uuid,
                    'page_id': created_page_id,
                    'page_name': name,
                    'page_url': page_url,
                    'category': category,
                    'follower_count': 0,
                    'role': 'admin',
                    'note': description
                }
                save_page(page_data)
                print(f"[CreatePage] SUCCESS! Page ID: {created_page_id}")
                return True
            else:
                # Lưu với ID tạm
                page_data = {
                    'profile_uuid': profile_uuid,
                    'page_id': f"temp_{str(uuid_module.uuid4())[:8]}",
                    'page_name': name,
                    'page_url': '',
                    'category': category,
                    'follower_count': 0,
                    'role': 'admin',
                    'note': f"{description}\n[Cần scan lại để lấy Page ID thực]"
                }
                save_page(page_data)
                return True

        except Exception as e:
            import traceback
            print(f"[CreatePage] ERROR: {traceback.format_exc()}")
            return False
        finally:
            release_window_slot(slot_id)

    def _on_create_complete(self, count: int):
        """Khi tạo xong"""
        self.progress_label.setText(f"Đã tạo {count} pages!")
        self.progress_label.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 11px;")
        self.progress_bar.setValue(self.progress_bar.maximum())
        self.log(f"Đã tạo {count} Fanpages mới", "success")


class PagesPage(QWidget):
    """Pages Page - Quan ly Fanpage - BEAUTIFUL UI"""

    def __init__(self, log_func, parent=None):
        super().__init__(parent)
        self.log = log_func
        self.profiles: List[Dict] = []
        self.pages: List[Dict] = []
        self.folders: List[Dict] = []
        self.folder_map: Dict[str, str] = {}

        # Selection tracking
        self.page_checkboxes: Dict[int, CyberCheckBox] = {}

        # State
        self._is_scanning = False

        # Signal để thread-safe UI update
        self.signal = PagesSignal()
        self.signal.data_loaded.connect(self._on_data_loaded)
        self.signal.profiles_loaded.connect(self._on_profiles_loaded)
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

        title = CyberTitle("Pages", "Quản lý Fanpage Facebook", "purple")
        top_bar.addWidget(title)

        top_bar.addStretch()

        self.stat_total = CyberStatCard("TỔNG PAGE", "0", "📄", "purple")
        self.stat_total.setFixedWidth(160)
        top_bar.addWidget(self.stat_total)

        self.stat_selected = CyberStatCard("ĐÃ CHỌN", "0", "✓", "cyan")
        self.stat_selected.setFixedWidth(160)
        top_bar.addWidget(self.stat_selected)

        self.stat_profiles = CyberStatCard("PROFILES", "0", "📁", "pink")
        self.stat_profiles.setFixedWidth(160)
        top_bar.addWidget(self.stat_profiles)

        layout.addLayout(top_bar)

        # ========== TOOLBAR ==========
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        # Folder filter
        self.folder_combo = CyberComboBox(["📁 Tat ca folder"])
        self.folder_combo.setFixedWidth(180)
        self.folder_combo.currentIndexChanged.connect(self._on_folder_change)
        toolbar.addWidget(self.folder_combo)

        # Profile filter
        self.profile_combo = CyberComboBox(["👤 Tat ca profile"])
        self.profile_combo.setFixedWidth(200)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_change)
        toolbar.addWidget(self.profile_combo)

        # Search
        self.search_input = CyberInput("🔍 Tìm kiếm Page...")
        self.search_input.setFixedWidth(200)
        self.search_input.textChanged.connect(self._filter_pages)
        toolbar.addWidget(self.search_input)

        toolbar.addStretch()

        # Action buttons
        btn_refresh = CyberButton("", "ghost", "🔄")
        btn_refresh.setFixedWidth(40)
        btn_refresh.clicked.connect(self._load_data)
        toolbar.addWidget(btn_refresh)

        btn_scan = CyberButton("SCAN", "cyan", "🔍")
        btn_scan.clicked.connect(self._scan_pages)
        toolbar.addWidget(btn_scan)

        btn_create = CyberButton("TẠO PAGE", "success", "➕")
        btn_create.clicked.connect(self._open_create_dialog)
        toolbar.addWidget(btn_create)

        btn_delete = CyberButton("XÓA", "danger", "🗑️")
        btn_delete.clicked.connect(self._delete_selected_pages)
        toolbar.addWidget(btn_delete)

        layout.addLayout(toolbar)

        # ========== PROGRESS BAR ==========
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background: {COLORS['bg_darker']};
                border: none;
                border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, x2:1, stop:0 {COLORS['neon_purple']}, stop:1 {COLORS['neon_pink']});
                border-radius: 3px;
            }}
        """)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # ========== MAIN CONTENT - TABLE CARD ==========
        table_card = CyberCard(COLORS['neon_purple'])
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
        select_widget = QWidget()
        select_layout = QHBoxLayout(select_widget)
        select_layout.setContentsMargins(0, 0, 0, 0)
        select_layout.setSpacing(8)

        self.select_all_cb = CyberCheckBox()
        self.select_all_cb.stateChanged.connect(self._toggle_select_all)
        select_layout.addWidget(self.select_all_cb)

        select_label = QLabel("Chọn tất cả")
        select_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        select_label.setCursor(Qt.PointingHandCursor)
        select_label.mousePressEvent = lambda e: self.select_all_cb.setChecked(not self.select_all_cb.isChecked())
        select_layout.addWidget(select_label)

        header_layout.addWidget(select_widget)

        # Separator
        sep = QFrame()
        sep.setFixedWidth(2)
        sep.setFixedHeight(24)
        sep.setStyleSheet(f"background: {COLORS['border']};")
        header_layout.addWidget(sep)

        # Title
        header_title = QLabel("📄 FANPAGES")
        header_title.setStyleSheet(f"color: {COLORS['neon_purple']}; font-size: 12px; font-weight: bold; letter-spacing: 2px;")
        header_layout.addWidget(header_title)

        self.count_label = QLabel("[0]")
        self.count_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        header_layout.addWidget(self.count_label)

        header_layout.addStretch()

        # Selected count
        self.selected_label = QLabel("")
        self.selected_label.setStyleSheet(f"color: {COLORS['neon_cyan']}; font-size: 11px;")
        header_layout.addWidget(self.selected_label)

        # Progress text
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"color: {COLORS['neon_pink']}; font-size: 11px;")
        header_layout.addWidget(self.progress_label)

        table_layout.addWidget(header)

        # Table
        self.table = CyberTable(["✓", "TEN PAGE", "FOLLOWERS", "PROFILE", "ROLE", "CATEGORY"])
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(1, 250)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 150)
        self.table.setColumnWidth(4, 80)
        self.table.setColumnWidth(5, 120)

        table_layout.addWidget(self.table)
        layout.addWidget(table_card, 1)

    def _load_data(self):
        """Load folders, profiles va pages tu Hidemium va DB"""
        self.log("Đang tải dữ liệu...", "info")

        def fetch():
            try:
                folders = api.get_folders(limit=100)
                profiles = api.get_profiles(limit=500)
                pages = get_pages()
                print(f"[DEBUG] PagesPage got {len(folders)} folders, {len(profiles)} profiles")
                return {"folders": folders or [], "profiles": profiles or [], "pages": pages or []}
            except Exception as e:
                print(f"[DEBUG] PagesPage load error: {e}")
                return {"folders": [], "profiles": get_profiles(), "pages": get_pages(), "error": str(e)}

        def run():
            result = fetch()
            self.signal.data_loaded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_data_loaded(self, result):
        """Slot nhận data từ thread - chạy trên main thread"""
        if "error" in result:
            self.log(f"Lỗi API: {result['error']}", "warning")

        self.folders = result.get("folders", [])
        self.profiles = result.get("profiles", [])
        self.pages = result.get("pages", [])

        print(f"[DEBUG] _on_data_loaded: {len(self.folders)} folders, {len(self.profiles)} profiles")

        # Build folder map
        self.folder_map = {f.get('id'): f.get('name', 'Unknown') for f in self.folders}

        # Update folder combo
        self.folder_combo.clear()
        self.folder_combo.addItem("📁 Tất cả folder")
        for folder in self.folders:
            self.folder_combo.addItem(f"📁 {folder.get('name', 'Unknown')}")

        # Update profile combo
        self.profile_combo.clear()
        self.profile_combo.addItem("👤 Tất cả profile")
        for profile in self.profiles:
            name = profile.get('name', 'Unknown')
            if len(name) > 25:
                name = name[:25] + "..."
            self.profile_combo.addItem(f"👤 {name}")

        self._update_table()
        self._update_stats()
        self.log(f"Đã tải {len(self.profiles)} profiles, {len(self.pages)} pages", "success")

    def _on_folder_change(self, index):
        """Khi thay doi folder filter"""
        if index <= 0:
            # All folders - load all profiles
            self._load_profiles_for_folder(None)
        else:
            folder = self.folders[index - 1]
            folder_id = folder.get('id')
            self._load_profiles_for_folder(folder_id)

    def _load_profiles_for_folder(self, folder_id):
        """Load profiles theo folder"""
        self._current_folder_id = folder_id  # Store for use in thread

        def fetch():
            try:
                if folder_id:
                    profiles = api.get_profiles(folder_id=[folder_id], limit=500)
                else:
                    profiles = api.get_profiles(limit=500)
                print(f"[DEBUG] PagesPage load profiles got {len(profiles or [])} profiles")
                return profiles or []
            except Exception as e:
                print(f"[DEBUG] PagesPage load profiles error: {e}")
                return get_profiles()

        def run():
            result = fetch()
            self.signal.profiles_loaded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_profiles_loaded(self, profiles):
        """Slot nhận profiles từ thread - chạy trên main thread"""
        self.profiles = profiles or []

        print(f"[DEBUG] _on_profiles_loaded: {len(self.profiles)} profiles")

        # Update profile combo
        self.profile_combo.clear()
        self.profile_combo.addItem("👤 Tất cả profile")
        for profile in self.profiles:
            name = profile.get('name', 'Unknown')
            if len(name) > 25:
                name = name[:25] + "..."
            self.profile_combo.addItem(f"👤 {name}")

        self._filter_pages_by_profile()
        self._update_stats()

    def _on_profile_change(self, index):
        """Khi thay doi profile filter"""
        self._filter_pages_by_profile()

    def _filter_pages_by_profile(self):
        """Filter pages theo profile đã chọn"""
        profile_idx = self.profile_combo.currentIndex()

        if profile_idx <= 0:
            # All profiles - get pages for all current profiles
            profile_uuids = [p.get('uuid') for p in self.profiles]
            if profile_uuids:
                self.pages = get_pages_for_profiles(profile_uuids)
            else:
                self.pages = get_pages()
        else:
            profile = self.profiles[profile_idx - 1]
            uuid = profile.get('uuid', '')
            self.pages = get_pages(uuid)

        self._update_table()
        self._update_stats()

    def _filter_pages(self, search_text):
        """Filter pages theo search text"""
        self._update_table(search_text)

    def _update_table(self, search_text=None):
        """Update table voi pages"""
        # Filter by search
        pages_to_show = self.pages
        if search_text:
            search_lower = search_text.lower()
            pages_to_show = [
                p for p in self.pages
                if search_lower in p.get('page_name', '').lower()
                or search_lower in p.get('category', '').lower()
            ]

        # Build profile name map
        profile_map = {p.get('uuid'): p.get('name', 'Unknown') for p in self.profiles}

        self.table.setRowCount(len(pages_to_show))
        self.page_checkboxes.clear()

        for row, page in enumerate(pages_to_show):
            page_id = page.get('id')
            page_name = page.get('page_name', 'Unknown')
            followers = page.get('follower_count', 0)
            profile_uuid = page.get('profile_uuid', '')
            profile_name = profile_map.get(profile_uuid, 'Unknown')
            role = page.get('role', 'admin')
            category = page.get('category', '-')

            # Checkbox
            cb_widget = QWidget()
            cb_widget.setStyleSheet("background: transparent;")
            cb_layout = QHBoxLayout(cb_widget)
            cb_layout.setContentsMargins(0, 0, 0, 0)
            cb_layout.setAlignment(Qt.AlignCenter)
            checkbox = CyberCheckBox()
            checkbox.stateChanged.connect(self._update_selection_count)
            cb_layout.addWidget(checkbox)
            self.table.setCellWidget(row, 0, cb_widget)
            self.page_checkboxes[page_id] = checkbox

            # Page name
            name_item = QTableWidgetItem(page_name[:35] + "..." if len(page_name) > 35 else page_name)
            self.table.setItem(row, 1, name_item)

            # Followers
            followers_item = QTableWidgetItem(self._format_number(followers))
            followers_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 2, followers_item)

            # Profile name
            profile_display = profile_name[:20] + "..." if len(profile_name) > 20 else profile_name
            self.table.setItem(row, 3, QTableWidgetItem(profile_display))

            # Role
            role_item = QTableWidgetItem(role)
            role_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 4, role_item)

            # Category
            self.table.setItem(row, 5, QTableWidgetItem(category or '-'))

        self.count_label.setText(f"[{len(pages_to_show)} pages]")

    def _format_number(self, num):
        """Format so de hien thi"""
        if num >= 1000000:
            return f"{num/1000000:.1f}M"
        elif num >= 1000:
            return f"{num/1000:.1f}K"
        return str(num)

    def _toggle_select_all(self, state):
        """Toggle chon tat ca pages"""
        checked = state == Qt.Checked
        count = 0
        for page_id, cb in self.page_checkboxes.items():
            cb.setChecked(checked)
            if checked:
                count += 1
        self.selected_label.setText(f"✓ {count} đã chọn" if checked else "")
        self._update_stats()

    def _update_selection_count(self):
        """Cap nhat so luong đã chọn"""
        count = sum(1 for cb in self.page_checkboxes.values() if cb.isChecked())
        self.selected_label.setText(f"✓ {count} đã chọn" if count > 0 else "")
        self.stat_selected.set_value(str(count))

    def _update_stats(self):
        """Cap nhat stats"""
        self.stat_total.set_value(str(len(self.pages)))
        selected = sum(1 for cb in self.page_checkboxes.values() if cb.isChecked())
        self.stat_selected.set_value(str(selected))
        self.stat_profiles.set_value(str(len(self.profiles)))

    def _scan_pages(self):
        """Scan pages từ profiles - MỞ BROWSER VÀ QUÉT THẬT"""
        if self._is_scanning:
            QMessageBox.warning(self, "Thông báo", "Đang scan, vui lòng đợi...")
            return

        if not BS4_AVAILABLE:
            QMessageBox.warning(self, "Lỗi", "Cần cài BeautifulSoup4: pip install beautifulsoup4")
            return

        # Get selected profile or all profiles
        profile_idx = self.profile_combo.currentIndex()
        if profile_idx <= 0:
            profiles_to_scan = self.profiles
        else:
            profiles_to_scan = [self.profiles[profile_idx - 1]]

        if not profiles_to_scan:
            QMessageBox.warning(self, "Lỗi", "Không có profile nào để scan!")
            return

        reply = QMessageBox.question(
            self, "Xác nhận",
            f"Scan pages từ {len(profiles_to_scan)} profiles?\n\nSẽ mở browser và quét danh sách Fanpages.",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            return

        self._is_scanning = True
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(profiles_to_scan))
        self.progress_bar.setValue(0)
        self.log(f"Bắt đầu scan {len(profiles_to_scan)} profiles...", "info")

        def do_scan():
            total_pages_found = 0

            for i, profile in enumerate(profiles_to_scan):
                uuid = profile.get('uuid', '')
                name = profile.get('name', uuid[:8])

                QTimer.singleShot(0, lambda v=i+1: self.progress_bar.setValue(v))
                QTimer.singleShot(0, lambda m=f"[{i+1}/{len(profiles_to_scan)}] {name}": self.progress_label.setText(m))

                # Scan pages cho profile này
                pages_found = self._execute_page_scan_for_profile(uuid, name)
                total_pages_found += len(pages_found)

                # Lưu vào DB
                if pages_found:
                    sync_pages(uuid, pages_found)

            self._is_scanning = False
            QTimer.singleShot(0, lambda: self.progress_bar.setVisible(False))
            QTimer.singleShot(0, lambda: self.progress_label.setText(""))
            QTimer.singleShot(0, lambda: self.log(f"Scan hoàn thành! Tìm thấy {total_pages_found} pages", "success"))
            QTimer.singleShot(0, self._filter_pages_by_profile)

        threading.Thread(target=do_scan, daemon=True).start()

    def _execute_page_scan_for_profile(self, profile_uuid: str, profile_name: str):
        """Quét pages cho 1 profile - MỞ BROWSER VÀ QUÉT THẬT"""
        pages_found = []
        slot_id = acquire_window_slot()

        try:
            # Mở browser
            self.log(f"Mở browser {profile_name}...", "info")
            result = api.open_browser(profile_uuid)

            status = result.get('status') or result.get('type')
            if status not in ['successfully', 'success', True]:
                if 'already' not in str(result).lower():
                    self.log(f"Không mở được browser: {result}", "error")
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
                self.log("Không lấy được CDP port", "error")
                release_window_slot(slot_id)
                return []

            cdp_base = f"http://127.0.0.1:{remote_port}"
            time.sleep(2)

            # Lấy WebSocket
            try:
                resp = requests.get(f"{cdp_base}/json", timeout=10)
                tabs = resp.json()
            except Exception as e:
                self.log(f"Lỗi CDP: {e}", "error")
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

            try:
                ws = websocket.create_connection(page_ws, timeout=30, suppress_origin=True)
            except:
                release_window_slot(slot_id)
                return []

            # Navigate đến trang pages
            pages_url = "https://www.facebook.com/pages/?category=your_pages"
            self.log("Đang vào trang Fanpages...", "info")

            ws.send(json_module.dumps({
                "id": 1,
                "method": "Page.navigate",
                "params": {"url": pages_url}
            }))
            ws.recv()
            time.sleep(8)

            # Lấy HTML
            ws.send(json_module.dumps({
                "id": 2,
                "method": "Runtime.evaluate",
                "params": {"expression": "document.documentElement.outerHTML"}
            }))
            html_result = json_module.loads(ws.recv())
            html = html_result.get('result', {}).get('result', {}).get('value', '')

            ws.close()

            # Parse HTML
            if html and BS4_AVAILABLE:
                soup = BeautifulSoup(html, 'html.parser')

                # Tìm các page links
                page_links = soup.find_all('a', href=re.compile(r'facebook\.com/[^/]+/?$|/pages/[^/]+'))

                seen_ids = set()
                for link in page_links:
                    href = link.get('href', '')

                    # Lấy page ID hoặc username
                    page_id = None
                    if '/pages/' in href:
                        match = re.search(r'/pages/([^/?]+)', href)
                        if match:
                            page_id = match.group(1)
                    else:
                        match = re.search(r'facebook\.com/([^/?]+)', href)
                        if match:
                            page_id = match.group(1)

                    if page_id and page_id not in seen_ids and page_id not in ['groups', 'events', 'marketplace', 'watch', 'gaming']:
                        seen_ids.add(page_id)

                        # Lấy tên page
                        page_name = link.get_text(strip=True) or f"Page {page_id}"

                        pages_found.append({
                            'page_id': page_id,
                            'page_name': page_name[:100],
                            'profile_uuid': profile_uuid,
                            'follower_count': 0,
                            'role': 'admin',
                            'category': ''
                        })

                self.log(f"Tìm thấy {len(pages_found)} pages cho {profile_name}", "success")

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.log(f"Lỗi: {str(e)}", "error")
        finally:
            release_window_slot(slot_id)

        return pages_found

    def _open_create_dialog(self):
        """Mở dialog tạo Page mới"""
        # Lấy danh sách profile để tạo page
        profile_idx = self.profile_combo.currentIndex()

        if profile_idx <= 0:
            # Chọn tất cả profiles
            if not self.profiles:
                QMessageBox.warning(self, "Lỗi", "Không có profile nào!")
                return
            profile_uuids = [p.get('uuid') for p in self.profiles]
        else:
            # Chỉ profile đã chọn
            profile = self.profiles[profile_idx - 1]
            profile_uuids = [profile.get('uuid')]

        if not profile_uuids:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn profile trước!")
            return

        # Mở dialog
        dialog = CreatePageDialog(self, profile_uuids, self.profiles, self.log)
        dialog.exec()

        # Refresh sau khi đóng dialog
        self._filter_pages_by_profile()

    def _delete_selected_pages(self):
        """Xoa cac pages đã chọn"""
        selected_ids = [pid for pid, cb in self.page_checkboxes.items() if cb.isChecked()]

        if not selected_ids:
            QMessageBox.warning(self, "Lỗi", "Chưa chọn page nào!")
            return

        reply = QMessageBox.question(
            self, "Xác nhận",
            f"Bạn có chắc muốn xóa {len(selected_ids)} pages?",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            deleted = delete_pages_bulk(selected_ids)
            self.log(f"Đã xóa {deleted} pages", "success")
            self._filter_pages_by_profile()
