"""
Content Page - Quản lý nội dung đăng bài
PySide6 version - BEAUTIFUL UI
Có hỗ trợ Import/Export và macro processing
"""
import threading
import random
from typing import List, Dict
from datetime import datetime
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QScrollArea, QMessageBox, QTextEdit, QFileDialog,
    QTableWidgetItem, QInputDialog
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor

from config import COLORS
from widgets import (
    CyberButton, CyberInput, CyberComboBox, CyberCard,
    CyberTitle, CyberStatCard, CyberTable, CyberCheckBox
)
from db import (
    get_categories, save_category, delete_category,
    get_contents, save_content, delete_content, get_contents_count
)


class ContentPage(QWidget):
    """Content Page - Quản lý nội dung"""

    def __init__(self, log_func, parent=None):
        super().__init__(parent)
        self.log = log_func
        self.categories: List[Dict] = []
        self.contents: List[Dict] = []
        self.current_category_id = None
        self.editing_content = None
        self.content_checkboxes: Dict[int, CyberCheckBox] = {}

        self._setup_ui()
        QTimer.singleShot(500, self._load_data)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # ========== TOP BAR ==========
        top_bar = QHBoxLayout()
        top_bar.setSpacing(12)

        title = CyberTitle("Soạn Tin", "Quản lý nội dung đăng bài", "yellow")
        top_bar.addWidget(title)

        top_bar.addStretch()

        self.stat_categories = CyberStatCard("DANH MỤC", "0", "📁", "yellow")
        self.stat_categories.setFixedWidth(160)
        top_bar.addWidget(self.stat_categories)

        self.stat_contents = CyberStatCard("NỘI DUNG", "0", "📝", "cyan")
        self.stat_contents.setFixedWidth(160)
        top_bar.addWidget(self.stat_contents)

        self.stat_selected = CyberStatCard("ĐÃ CHỌN", "0", "✓", "mint")
        self.stat_selected.setFixedWidth(160)
        top_bar.addWidget(self.stat_selected)

        # Import/Export buttons
        btn_import = CyberButton("📥 Nạp", "secondary")
        btn_import.setFixedWidth(80)
        btn_import.clicked.connect(self._import_contents)
        top_bar.addWidget(btn_import)

        btn_export = CyberButton("📤 Xuất", "secondary")
        btn_export.setFixedWidth(80)
        btn_export.clicked.connect(self._export_contents)
        top_bar.addWidget(btn_export)

        layout.addLayout(top_bar)

        # ========== MAIN CONTENT - 3 PANELS ==========
        content = QHBoxLayout()
        content.setSpacing(12)

        # ========== LEFT PANEL - Categories ==========
        left_card = CyberCard(COLORS['neon_yellow'])
        left_card.setFixedWidth(240)
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(2, 2, 2, 2)

        # Header
        cat_header = QWidget()
        cat_header.setFixedHeight(44)
        cat_header.setStyleSheet(f"background: {COLORS['bg_darker']}; border-radius: 14px 14px 0 0;")
        cat_header_layout = QHBoxLayout(cat_header)
        cat_header_layout.setContentsMargins(12, 0, 12, 0)
        cat_header_layout.setSpacing(6)

        cat_title = QLabel("📁 DANH MỤC")
        cat_title.setStyleSheet(f"color: {COLORS['neon_yellow']}; font-size: 11px; font-weight: bold;")
        cat_header_layout.addWidget(cat_title)

        self.cat_count_label = QLabel("[0]")
        self.cat_count_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px;")
        cat_header_layout.addWidget(self.cat_count_label)

        cat_header_layout.addStretch()

        left_layout.addWidget(cat_header)

        # Add category input row (inline)
        add_cat_widget = QWidget()
        add_cat_widget.setStyleSheet(f"background: {COLORS['bg_darker']};")
        add_cat_layout = QHBoxLayout(add_cat_widget)
        add_cat_layout.setContentsMargins(8, 6, 8, 6)
        add_cat_layout.setSpacing(6)

        self.new_cat_input = CyberInput("Tên danh mục mới...")
        self.new_cat_input.returnPressed.connect(self._add_category_quick)
        add_cat_layout.addWidget(self.new_cat_input)

        btn_add_cat = CyberButton("+", "success")
        btn_add_cat.setFixedSize(32, 32)
        btn_add_cat.clicked.connect(self._add_category_quick)
        add_cat_layout.addWidget(btn_add_cat)

        left_layout.addWidget(add_cat_widget)

        # Category list scroll
        scroll_cat = QScrollArea()
        scroll_cat.setWidgetResizable(True)
        scroll_cat.setStyleSheet(f"""
            QScrollArea {{
                background: {COLORS['bg_darker']};
                border: none;
                border-radius: 0 0 14px 14px;
            }}
        """)

        self.cat_list_widget = QWidget()
        self.cat_list_widget.setStyleSheet(f"background: {COLORS['bg_darker']};")
        self.cat_list_layout = QVBoxLayout(self.cat_list_widget)
        self.cat_list_layout.setContentsMargins(8, 8, 8, 8)
        self.cat_list_layout.setSpacing(4)
        self.cat_list_layout.addStretch()

        scroll_cat.setWidget(self.cat_list_widget)
        left_layout.addWidget(scroll_cat, 1)

        content.addWidget(left_card)

        # ========== MIDDLE PANEL - Content Table ==========
        middle_card = CyberCard(COLORS['neon_cyan'])
        middle_layout = QVBoxLayout(middle_card)
        middle_layout.setContentsMargins(2, 2, 2, 2)

        # Header
        content_header = QWidget()
        content_header.setFixedHeight(44)
        content_header.setStyleSheet(f"background: {COLORS['bg_darker']}; border-radius: 14px 14px 0 0;")
        content_header_layout = QHBoxLayout(content_header)
        content_header_layout.setContentsMargins(16, 0, 16, 0)
        content_header_layout.setSpacing(12)

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

        content_header_layout.addWidget(select_widget)

        sep = QFrame()
        sep.setFixedWidth(2)
        sep.setFixedHeight(24)
        sep.setStyleSheet(f"background: {COLORS['border']};")
        content_header_layout.addWidget(sep)

        content_title = QLabel("📝 NỘI DUNG")
        content_title.setStyleSheet(f"color: {COLORS['neon_cyan']}; font-size: 12px; font-weight: bold; letter-spacing: 2px;")
        content_header_layout.addWidget(content_title)

        self.content_count_label = QLabel("[0]")
        self.content_count_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        content_header_layout.addWidget(self.content_count_label)

        content_header_layout.addStretch()

        self.selected_label = QLabel("")
        self.selected_label.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 11px;")
        content_header_layout.addWidget(self.selected_label)

        # Search
        self.search_input = CyberInput("🔍 Tìm kiếm...")
        self.search_input.setFixedWidth(180)
        self.search_input.textChanged.connect(self._filter_contents)
        content_header_layout.addWidget(self.search_input)

        btn_add_content = CyberButton("+", "success")
        btn_add_content.setFixedSize(32, 32)
        btn_add_content.clicked.connect(self._add_content)
        content_header_layout.addWidget(btn_add_content)

        middle_layout.addWidget(content_header)

        # Table
        self.table = CyberTable(["✓", "TIÊU ĐỀ", "NỘI DUNG", "HÌNH"])
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(2, 300)
        self.table.setColumnWidth(3, 80)
        self.table.cellClicked.connect(self._on_table_click)

        middle_layout.addWidget(self.table)
        content.addWidget(middle_card, 1)

        # ========== RIGHT PANEL - Editor ==========
        right_card = CyberCard(COLORS['neon_mint'])
        right_card.setFixedWidth(350)
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(2, 2, 2, 2)

        # Header
        editor_header = QWidget()
        editor_header.setFixedHeight(44)
        editor_header.setStyleSheet(f"background: {COLORS['bg_darker']}; border-radius: 14px 14px 0 0;")
        editor_header_layout = QHBoxLayout(editor_header)
        editor_header_layout.setContentsMargins(16, 0, 16, 0)

        editor_title = QLabel("✏️ CHINH SUA")
        editor_title.setStyleSheet(f"color: {COLORS['neon_mint']}; font-size: 12px; font-weight: bold; letter-spacing: 2px;")
        editor_header_layout.addWidget(editor_title)

        editor_header_layout.addStretch()

        self.editing_label = QLabel("")
        self.editing_label.setStyleSheet(f"color: {COLORS['neon_pink']}; font-size: 11px;")
        editor_header_layout.addWidget(self.editing_label)

        right_layout.addWidget(editor_header)

        # Editor form
        form_widget = QWidget()
        form_widget.setStyleSheet(f"background: {COLORS['bg_darker']}; border-radius: 0 0 14px 14px;")
        form_layout = QVBoxLayout(form_widget)
        form_layout.setContentsMargins(16, 16, 16, 16)
        form_layout.setSpacing(12)

        # Category selection
        cat_row = QHBoxLayout()
        cat_label = QLabel("Danh muc:")
        cat_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        cat_label.setFixedWidth(80)
        cat_row.addWidget(cat_label)

        self.cat_combo = CyberComboBox()
        cat_row.addWidget(self.cat_combo)
        form_layout.addLayout(cat_row)

        # Title
        title_row = QHBoxLayout()
        title_label = QLabel("Tiêu đề:")
        title_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        title_label.setFixedWidth(80)
        title_row.addWidget(title_label)

        self.title_input = CyberInput("Nhập tiêu đề bài viết...")
        title_row.addWidget(self.title_input)
        form_layout.addLayout(title_row)

        # Content
        content_label = QLabel("Nội dung:")
        content_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        form_layout.addWidget(content_label)

        self.content_text = QTextEdit()
        self.content_text.setPlaceholderText("Nhập nội dung bài viết...\n\nSử dụng {name}, {time}, {date} để thay thế tự động...")
        self.content_text.setStyleSheet(f"""
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
        form_layout.addWidget(self.content_text, 1)

        # Image
        image_label = QLabel("Hình ảnh:")
        image_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        form_layout.addWidget(image_label)

        image_row = QHBoxLayout()

        self.image_input = CyberInput("Chọn hình ảnh...")
        self.image_input.setReadOnly(True)
        image_row.addWidget(self.image_input, 1)

        btn_browse = CyberButton("📂", "purple")
        btn_browse.setFixedWidth(40)
        btn_browse.clicked.connect(self._browse_image)
        image_row.addWidget(btn_browse)

        btn_clear_img = CyberButton("✕", "danger")
        btn_clear_img.setFixedWidth(40)
        btn_clear_img.clicked.connect(lambda: self.image_input.clear())
        image_row.addWidget(btn_clear_img)

        form_layout.addLayout(image_row)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_new = CyberButton("TẠO MỚI", "primary", "➕")
        self.btn_new.clicked.connect(self._add_content)
        btn_row.addWidget(self.btn_new)

        self.btn_save = CyberButton("LƯU", "success", "💾")
        self.btn_save.clicked.connect(self._save_content)
        btn_row.addWidget(self.btn_save)

        self.btn_cancel = CyberButton("HỦY", "ghost")
        self.btn_cancel.clicked.connect(self._clear_editor)
        btn_row.addWidget(self.btn_cancel)

        self.btn_delete = CyberButton("XÓA", "danger", "🗑️")
        self.btn_delete.clicked.connect(self._delete_content)
        btn_row.addWidget(self.btn_delete)

        form_layout.addLayout(btn_row)

        right_layout.addWidget(form_widget, 1)

        content.addWidget(right_card)
        layout.addLayout(content, 1)

    def _load_data(self):
        """Load categories va contents"""
        self.categories = get_categories()
        self._render_categories()
        self._update_category_combo()
        self._update_stats()
        self.log(f"Loaded {len(self.categories)} categories", "success")

    def _render_categories(self):
        """Render danh sach categories"""
        # Clear old items
        while self.cat_list_layout.count() > 0:
            item = self.cat_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for cat in self.categories:
            cat_id = cat.get('id')
            name = cat.get('name', 'Unknown')
            count = get_contents_count(cat_id)

            row = QWidget()
            is_selected = cat_id == self.current_category_id
            row.setFixedHeight(44)
            row.setCursor(Qt.PointingHandCursor)

            if is_selected:
                row.setStyleSheet(f"""
                    QWidget {{
                        background: qlineargradient(x1:0, x2:1, stop:0 {COLORS['neon_yellow']}40, stop:1 transparent);
                        border: none;
                        border-left: 3px solid {COLORS['neon_yellow']};
                        border-radius: 0 8px 8px 0;
                    }}
                """)
            else:
                row.setStyleSheet(f"""
                    QWidget {{
                        background: transparent;
                        border-radius: 8px;
                    }}
                    QWidget:hover {{
                        background: {COLORS['bg_hover']};
                    }}
                """)

            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(12, 4, 8, 4)
            row_layout.setSpacing(8)

            # Icon
            icon_label = QLabel("📁")
            icon_label.setFixedWidth(20)
            row_layout.addWidget(icon_label)

            # Name
            name_label = QLabel(name[:18] + "..." if len(name) > 18 else name)
            name_color = COLORS['neon_yellow'] if is_selected else COLORS['text_primary']
            name_label.setStyleSheet(f"color: {name_color}; font-size: 12px; font-weight: {'bold' if is_selected else 'normal'};")
            row_layout.addWidget(name_label, 1)

            # Count badge
            count_badge = QLabel(str(count))
            count_badge.setFixedSize(24, 20)
            count_badge.setAlignment(Qt.AlignCenter)
            count_badge.setStyleSheet(f"""
                background: {COLORS['bg_card']};
                color: {COLORS['text_muted']};
                border-radius: 10px;
                font-size: 10px;
            """)
            row_layout.addWidget(count_badge)

            # Delete button (not for default)
            if cat_id != 1:
                btn_del = CyberButton("×", "danger")
                btn_del.setFixedSize(24, 24)
                btn_del.clicked.connect(lambda _, cid=cat_id: self._delete_category(cid))
                row_layout.addWidget(btn_del)

            # Click handler
            row.mousePressEvent = lambda e, c=cat_id: self._select_category(c)

            self.cat_list_layout.addWidget(row)

        self.cat_list_layout.addStretch()
        self.cat_count_label.setText(f"[{len(self.categories)}]")

    def _update_category_combo(self):
        """Update category combo in editor"""
        self.cat_combo.clear()
        for cat in self.categories:
            self.cat_combo.addItem(f"📁 {cat.get('name', 'Unknown')}")

    def _select_category(self, cat_id):
        """Chon category"""
        self.current_category_id = cat_id
        self._render_categories()
        self._load_contents()
        self._clear_editor()

        # Update combo selection
        for i, cat in enumerate(self.categories):
            if cat.get('id') == cat_id:
                self.cat_combo.setCurrentIndex(i)
                break

    def _load_contents(self):
        """Load contents theo category"""
        if not self.current_category_id:
            self.contents = []
        else:
            self.contents = get_contents(self.current_category_id)
        self._render_contents()

    def _render_contents(self, search_text=None):
        """Render contents vao table"""
        contents_to_show = self.contents
        if search_text:
            search_lower = search_text.lower()
            contents_to_show = [c for c in self.contents
                               if search_lower in c.get('title', '').lower()
                               or search_lower in c.get('content', '').lower()]

        self.table.setRowCount(len(contents_to_show))
        self.content_checkboxes.clear()

        for row, content in enumerate(contents_to_show):
            content_id = content.get('id')
            title = content.get('title', 'Không có tiêu đề')
            body = content.get('content', '')
            image = content.get('image_path', '')

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
            self.content_checkboxes[content_id] = checkbox

            # Title
            title_item = QTableWidgetItem(title[:30] + "..." if len(title) > 30 else title)
            self.table.setItem(row, 1, title_item)

            # Content preview
            preview = body[:40] + "..." if len(body) > 40 else body
            preview = preview.replace('\n', ' ')
            content_item = QTableWidgetItem(preview)
            content_item.setForeground(QColor(COLORS['text_muted']))
            self.table.setItem(row, 2, content_item)

            # Image indicator
            has_image = "🖼️" if image else "—"
            image_item = QTableWidgetItem(has_image)
            image_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 3, image_item)

        self.content_count_label.setText(f"[{len(contents_to_show)} nội dung]")
        self._update_stats()

    def _on_table_click(self, row, col):
        """Handle table click - load content to editor"""
        if col == 0:  # Checkbox column, skip
            return

        if row < len(self.contents):
            content = self.contents[row]
            self._edit_content(content)

    def _filter_contents(self, text):
        self._render_contents(text)

    def _add_category_quick(self):
        """Thêm category nhanh từ input"""
        name = self.new_cat_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Thông báo", "Vui lòng nhập tên danh mục!")
            return

        cat = save_category({'name': name})
        self.new_cat_input.clear()
        self.current_category_id = cat.get('id')
        self.log(f"Đã thêm danh mục: {name}", "success")
        self._load_data()
        self._load_contents()

    def _add_category(self):
        """Thêm category mới (dialog - backup)"""
        name, ok = QInputDialog.getText(self, "Thêm danh mục", "Tên danh mục mới:")
        if ok and name.strip():
            save_category({'name': name.strip()})
            self.log(f"Đã thêm danh mục: {name}", "success")
            self._load_data()

    def _delete_category(self, cat_id):
        """Xóa category"""
        reply = QMessageBox.question(
            self, "Xác nhận xóa",
            "Bạn có chắc muốn xóa danh mục này?\nTất cả nội dung trong danh mục sẽ bị xóa!",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            delete_category(cat_id)
            self.log("Đã xóa danh mục", "success")
            if self.current_category_id == cat_id:
                self.current_category_id = None
                self.contents = []
                self._render_contents()
            self._load_data()

    def _add_content(self):
        """Thêm content mới"""
        if not self.current_category_id:
            QMessageBox.warning(self, "Thông báo", "Vui lòng chọn danh mục trước!")
            return

        self.editing_content = None
        self._clear_editor()
        self.editing_label.setText("✨ TẠO MỚI")
        self.title_input.setFocus()
        self.log("Đang tạo nội dung mới...", "info")

    def _edit_content(self, content: Dict):
        """Edit content"""
        self.editing_content = content
        self.editing_label.setText(f"SUA #{content.get('id')}")

        # Set category
        cat_id = content.get('category_id')
        for i, cat in enumerate(self.categories):
            if cat.get('id') == cat_id:
                self.cat_combo.setCurrentIndex(i)
                break

        self.title_input.setText(content.get('title', ''))
        self.content_text.setText(content.get('content', ''))
        self.image_input.setText(content.get('image_path', ''))

    def _save_content(self):
        """Luu content"""
        # Get category from combo
        cat_idx = self.cat_combo.currentIndex()
        if cat_idx < 0 or cat_idx >= len(self.categories):
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn danh mục!")
            return

        category_id = self.categories[cat_idx].get('id')
        title = self.title_input.text().strip()
        content_text = self.content_text.toPlainText().strip()

        if not title:
            QMessageBox.warning(self, "Lỗi", "Vui lòng nhập tiêu đề!")
            return

        data = {
            'category_id': category_id,
            'title': title,
            'content': content_text,
            'image_path': self.image_input.text()
        }

        if self.editing_content:
            data['id'] = self.editing_content.get('id')

        save_content(data)
        self.log(f"Đã lưu: {title}", "success")

        # Refresh
        self.current_category_id = category_id
        self._load_data()
        self._load_contents()
        self._clear_editor()

    def _delete_content(self):
        """Xoa content"""
        if not self.editing_content:
            QMessageBox.warning(self, "Thông báo", "Chưa chọn nội dung để xóa!")
            return

        reply = QMessageBox.question(
            self, "Xác nhận xóa",
            f"Bạn có chắc muốn xóa '{self.editing_content.get('title')}'?",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            delete_content(self.editing_content.get('id'))
            self.log("Đã xóa nội dung", "success")
            self._clear_editor()
            self._load_contents()
            self._render_categories()

    def _clear_editor(self):
        """Clear editor"""
        self.editing_content = None
        self.editing_label.setText("")
        self.title_input.clear()
        self.content_text.clear()
        self.image_input.clear()

    def _browse_image(self):
        """Chọn hình ảnh"""
        path, _ = QFileDialog.getOpenFileName(
            self, "Chọn hình ảnh",
            "", "Images (*.png *.jpg *.jpeg *.gif *.webp)"
        )

        if path:
            self.image_input.setText(path)

    def _toggle_select_all(self, state):
        """Toggle select all contents"""
        checked = state == Qt.Checked
        count = 0
        for content_id, cb in self.content_checkboxes.items():
            cb.setChecked(checked)
            if checked:
                count += 1
        self.selected_label.setText(f"✓ {count} đã chọn" if checked else "")
        self.stat_selected.set_value(str(count))

    def _update_selection_count(self):
        """Update selection count"""
        count = sum(1 for cb in self.content_checkboxes.values() if cb.isChecked())
        self.selected_label.setText(f"✓ {count} đã chọn" if count > 0 else "")
        self.stat_selected.set_value(str(count))

    def _update_stats(self):
        """Update stats cards"""
        self.stat_categories.set_value(str(len(self.categories)))
        self.stat_contents.set_value(str(len(self.contents)))
        selected = sum(1 for cb in self.content_checkboxes.values() if cb.isChecked())
        self.stat_selected.set_value(str(selected))

    def get_selected_contents(self) -> List[Dict]:
        """Get list of selected contents"""
        selected = []
        for content in self.contents:
            content_id = content.get('id')
            if content_id in self.content_checkboxes:
                if self.content_checkboxes[content_id].isChecked():
                    selected.append(content)
        return selected

    def _import_contents(self):
        """Import contents từ file TXT"""
        path, _ = QFileDialog.getOpenFileName(
            self, "Nạp nội dung",
            "", "Text files (*.txt);;All files (*.*)"
        )

        if not path:
            return

        if not self.current_category_id:
            QMessageBox.warning(self, "Thông báo", "Vui lòng chọn danh mục trước!")
            return

        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            count = 0
            for line in lines:
                line = line.strip()
                if line:
                    save_content({
                        'title': f"Imported {count + 1}",
                        'content': line,
                        'category_id': self.current_category_id
                    })
                    count += 1

            self._load_contents()
            self._render_categories()
            self.log(f"Đã nạp {count} nội dung từ file", "success")
            QMessageBox.information(self, "Thành công", f"Đã nạp {count} nội dung!")

        except Exception as e:
            self.log(f"Lỗi nạp file: {e}", "error")
            QMessageBox.warning(self, "Lỗi", f"Không thể nạp file: {e}")

    def _export_contents(self):
        """Export contents ra file TXT"""
        if not self.contents:
            QMessageBox.warning(self, "Thông báo", "Không có nội dung để xuất!")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Xuất nội dung",
            "contents.txt", "Text files (*.txt)"
        )

        if not path:
            return

        try:
            with open(path, 'w', encoding='utf-8') as f:
                for content in self.contents:
                    title = content.get('title', '')
                    body = content.get('content', '')
                    f.write(f"=== {title} ===\n")
                    f.write(f"{body}\n")
                    f.write("-" * 50 + "\n\n")

            self.log(f"Đã xuất {len(self.contents)} nội dung", "success")
            QMessageBox.information(self, "Thành công", f"Đã xuất {len(self.contents)} nội dung ra {path}")

        except Exception as e:
            self.log(f"Lỗi xuất file: {e}", "error")
            QMessageBox.warning(self, "Lỗi", f"Không thể xuất file: {e}")

    def process_macros(self, content: str) -> str:
        """Xử lý macro trong nội dung
        {r} - Random một dòng từ danh sách
        {rrr} - Shuffle và nối tất cả các dòng
        {time} - Thời gian hiện tại
        {date} - Ngày hiện tại
        """
        if not content:
            return content

        # {r} - Random một dòng
        if content.startswith('{r}'):
            lines = content[3:].strip().split('\n')
            lines = [l.strip() for l in lines if l.strip()]
            if lines:
                return random.choice(lines)

        # {rrr} - Shuffle và nối
        if content.startswith('{rrr}'):
            lines = content[5:].strip().split('\n')
            lines = [l.strip() for l in lines if l.strip()]
            random.shuffle(lines)
            return '\n'.join(lines)

        # Thay thế macros đơn giản
        content = content.replace('{time}', datetime.now().strftime('%H:%M:%S'))
        content = content.replace('{date}', datetime.now().strftime('%d/%m/%Y'))
        content = content.replace('{datetime}', datetime.now().strftime('%d/%m/%Y %H:%M'))

        return content
