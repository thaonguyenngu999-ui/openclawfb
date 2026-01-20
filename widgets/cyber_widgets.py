"""
FB Manager Pro - Cyberpunk Widgets
🐱 CUTE CAT Edition - BEAUTIFUL 🐱
"""

from PySide6.QtWidgets import (
    QWidget, QLabel, QFrame, QPushButton, QVBoxLayout, QHBoxLayout,
    QLineEdit, QTextEdit, QComboBox, QScrollArea, QGraphicsDropShadowEffect,
    QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox, QSizePolicy
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFont, QCursor, QPainter, QPen, QBrush, QLinearGradient, QPainterPath

import sys
sys.path.insert(0, str(__file__).rsplit('/', 2)[0])
from config import COLORS


class CyberButton(QPushButton):
    """Button đơn giản với màu neon"""

    def __init__(self, text: str, variant: str = "primary", icon: str = None, parent=None):
        display_text = f"{icon} {text}" if icon else text
        super().__init__(display_text, parent)

        self.variant = variant
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setFixedHeight(32)
        self.setMinimumWidth(60)

        colors = {
            "primary": (COLORS["neon_pink"], COLORS["neon_purple"]),
            "success": (COLORS["neon_mint"], COLORS["neon_cyan"]),
            "danger": (COLORS["neon_coral"], COLORS["neon_pink"]),
            "warning": (COLORS["neon_yellow"], COLORS["neon_coral"]),
            "purple": (COLORS["neon_purple"], COLORS["neon_blue"]),
            "cyan": (COLORS["neon_cyan"], COLORS["neon_mint"]),
            "ghost": (COLORS["text_muted"], COLORS["border"]),
        }

        self.color1, self.color2 = colors.get(variant, colors["primary"])
        self._apply_style()

    def _apply_style(self):
        hover_text = "#0a0a14"

        self.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                border: 2px solid {self.color1};
                border-radius: 10px;
                color: {self.color1};
                padding: 4px 14px;
                font-weight: bold;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background-color: {self.color1};
                color: {hover_text};
            }}
            QPushButton:pressed {{
                background-color: {self.color2};
                color: {hover_text};
            }}
        """)

    def setVariant(self, variant: str):
        """Thay đổi variant/màu của button sau khi khởi tạo"""
        colors = {
            "primary": (COLORS["neon_pink"], COLORS["neon_purple"]),
            "success": (COLORS["neon_mint"], COLORS["neon_cyan"]),
            "danger": (COLORS["neon_coral"], COLORS["neon_pink"]),
            "warning": (COLORS["neon_yellow"], COLORS["neon_coral"]),
            "purple": (COLORS["neon_purple"], COLORS["neon_blue"]),
            "cyan": (COLORS["neon_cyan"], COLORS["neon_mint"]),
            "ghost": (COLORS["text_muted"], COLORS["border"]),
        }
        self.variant = variant
        self.color1, self.color2 = colors.get(variant, colors["primary"])
        self._apply_style()


class ToggleButton(QPushButton):
    """Toggle button Start/Stop 🐱"""
    
    toggled_state = Signal(bool)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.is_running = False
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setFixedSize(80, 30)
        self.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self._update_style()
        self.clicked.connect(self._toggle)
    
    def _toggle(self):
        self.is_running = not self.is_running
        self._update_style()
        self.toggled_state.emit(self.is_running)
    
    def _update_style(self):
        if self.is_running:
            self.setText("⏹ Stop")
            self.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['neon_coral']};
                    border: none;
                    border-radius: 6px;
                    color: #0c0c18;
                    font-size: 11px;
                    font-weight: bold;
                    padding: 0px;
                }}
                QPushButton:hover {{
                    background: {COLORS['neon_pink']};
                }}
            """)
        else:
            self.setText("▶ Start")
            self.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['neon_mint']};
                    border: none;
                    border-radius: 6px;
                    color: #0c0c18;
                    font-size: 11px;
                    font-weight: bold;
                    padding: 0px;
                }}
                QPushButton:hover {{
                    background: {COLORS['neon_cyan']};
                }}
            """)
    
    def set_running(self, running: bool):
        self.is_running = running
        self._update_style()


class CyberInput(QLineEdit):
    """Input COMPACT 🐱"""
    
    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setFixedHeight(34)
        self.setStyleSheet(f"""
            QLineEdit {{
                background: {COLORS['bg_card']};
                border: 2px solid {COLORS['border']};
                border-radius: 10px;
                padding: 4px 14px;
                color: {COLORS['text_primary']};
                font-size: 13px;
            }}
            QLineEdit:focus {{
                border-color: {COLORS['neon_cyan']};
            }}
            QLineEdit::placeholder {{
                color: {COLORS['text_muted']};
            }}
        """)


class CyberComboBox(QComboBox):
    """Dropdown COMPACT 🐱"""
    
    def __init__(self, items: list = None, parent=None):
        super().__init__(parent)
        if items:
            self.addItems(items)
        self.setFixedHeight(34)
        
        self.setStyleSheet(f"""
            QComboBox {{
                background: {COLORS['bg_card']};
                border: 2px solid {COLORS['border']};
                border-radius: 10px;
                padding: 4px 14px;
                color: {COLORS['text_primary']};
                font-size: 12px;
                min-width: 140px;
            }}
            QComboBox:hover {{
                border-color: {COLORS['neon_purple']};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 30px;
            }}
            QComboBox::down-arrow {{
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 6px solid {COLORS['neon_purple']};
            }}
            QComboBox QAbstractItemView {{
                background: {COLORS['bg_card']};
                border: 2px solid {COLORS['border']};
                border-radius: 8px;
                selection-background-color: {COLORS['bg_hover']};
                font-size: 12px;
            }}
        """)


class CyberCard(QWidget):
    """Card cute 🐱"""
    
    def __init__(self, glow_color: str = None, parent=None):
        super().__init__(parent)
        self.glow_color = QColor(glow_color) if glow_color else QColor(COLORS['neon_pink'])
        self.border_radius = 16
        
        glow = QGraphicsDropShadowEffect(self)
        glow.setBlurRadius(20)
        glow.setColor(QColor(self.glow_color.red(), self.glow_color.green(), self.glow_color.blue(), 60))
        glow.setOffset(0, 2)
        self.setGraphicsEffect(glow)
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        path = QPainterPath()
        path.addRoundedRect(1, 1, self.width()-2, self.height()-2, self.border_radius, self.border_radius)
        
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0, QColor(COLORS['bg_card']))
        gradient.setColorAt(1, QColor(COLORS['bg_darker']))
        painter.fillPath(path, gradient)
        
        border_gradient = QLinearGradient(0, 0, self.width(), 0)
        border_gradient.setColorAt(0, self.glow_color)
        border_gradient.setColorAt(1, QColor(self.glow_color.red(), self.glow_color.green(), self.glow_color.blue(), 80))
        
        painter.setPen(QPen(QBrush(border_gradient), 2))
        painter.drawPath(path)


class CyberStatCard(QWidget):
    """Stat card COMPACT 🐱"""
    
    def __init__(self, label: str, value: str, icon: str = "😺", color: str = "pink", parent=None):
        super().__init__(parent)
        self.setFixedHeight(50)
        self.setMinimumWidth(150)
        
        color_map = {
            "pink": COLORS["neon_pink"],
            "cyan": COLORS["neon_cyan"],
            "purple": COLORS["neon_purple"],
            "mint": COLORS["neon_mint"],
            "green": COLORS["neon_mint"],
            "yellow": COLORS["neon_yellow"],
        }
        self.neon_color = QColor(color_map.get(color, COLORS["neon_pink"]))
        self.border_radius = 12
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        
        icon_lbl = QLabel(icon)
        icon_lbl.setStyleSheet("font-size: 16px;")
        icon_lbl.setFixedWidth(20)
        layout.addWidget(icon_lbl)
        
        self.value_label = QLabel(value)
        self.value_label.setStyleSheet(f"""
            color: {self.neon_color.name()}; 
            font-size: 20px; 
            font-weight: bold;
        """)
        layout.addWidget(self.value_label)
        
        lbl = QLabel(label)
        lbl.setStyleSheet(f"""
            color: {COLORS['text_muted']}; 
            font-size: 10px;
        """)
        layout.addWidget(lbl)
        layout.addStretch()
    
    def set_value(self, value: str):
        self.value_label.setText(value)
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        path = QPainterPath()
        path.addRoundedRect(1, 1, self.width()-2, self.height()-2, self.border_radius, self.border_radius)
        
        painter.fillPath(path, QColor(COLORS['bg_card']))
        painter.setPen(QPen(self.neon_color, 2))
        painter.drawPath(path)


class CyberTitle(QWidget):
    """Title COMPACT 🐱"""
    
    def __init__(self, title: str, subtitle: str = "", color: str = "pink", parent=None):
        super().__init__(parent)
        self.setFixedHeight(50)
        
        color_map = {
            "pink": COLORS["neon_pink"],
            "cyan": COLORS["neon_cyan"],
            "purple": COLORS["neon_purple"],
            "mint": COLORS["neon_mint"],
        }
        neon_color = color_map.get(color, COLORS["neon_pink"])
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        
        cat = QLabel("🐱")
        cat.setStyleSheet("font-size: 22px;")
        layout.addWidget(cat)
        
        from .cyber_effects import GlitchText
        title_label = GlitchText(title.upper(), neon_color, size=20)
        title_label.setFixedHeight(36)
        layout.addWidget(title_label)
        
        if subtitle:
            sub = QLabel(f"// {subtitle}")
            sub.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
            layout.addWidget(sub)
        
        layout.addStretch()


class NavItem(QPushButton):
    """Navigation item 🐱"""
    
    clicked_nav = Signal(str)
    
    def __init__(self, text: str, icon: str, tab_id: str, color: str = "pink", parent=None):
        super().__init__(f" {icon}  {text}", parent)
        
        self.tab_id = tab_id
        color_map = {
            "pink": COLORS["neon_pink"],
            "cyan": COLORS["neon_cyan"],
            "purple": COLORS["neon_purple"],
            "mint": COLORS["neon_mint"],
            "green": COLORS["neon_mint"],
            "yellow": COLORS["neon_yellow"],
            "coral": COLORS["neon_coral"],
            "blue": COLORS["neon_blue"],
        }
        self.neon_color = color_map.get(color, COLORS["neon_pink"])
        self.is_active = False
        
        self.setFixedHeight(40)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self._apply_style()
        
        self.clicked.connect(lambda: self.clicked_nav.emit(self.tab_id))
    
    def _apply_style(self):
        if self.is_active:
            self.setStyleSheet(f"""
                QPushButton {{
                    background: qlineargradient(x1:0, x2:1, stop:0 {self.neon_color}40, stop:1 transparent);
                    border: none;
                    border-left: 3px solid {self.neon_color};
                    border-radius: 0 10px 10px 0;
                    text-align: left;
                    padding-left: 14px;
                    color: {self.neon_color};
                    font-weight: bold;
                    font-size: 13px;
                }}
            """)
        else:
            self.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    border: none;
                    border-left: 3px solid transparent;
                    border-radius: 0 10px 10px 0;
                    text-align: left;
                    padding-left: 14px;
                    color: {COLORS['text_secondary']};
                    font-size: 13px;
                }}
                QPushButton:hover {{
                    background: {COLORS['bg_hover']};
                    color: {COLORS['text_primary']};
                }}
            """)
    
    def set_active(self, active: bool):
        self.is_active = active
        self._apply_style()


class CyberCheckBox(QCheckBox):
    """Checkbox cute 🐱"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QCheckBox {{
                spacing: 0px;
            }}
            QCheckBox::indicator {{
                width: 20px;
                height: 20px;
                border: 2px solid {COLORS['border']};
                border-radius: 6px;
                background: {COLORS['bg_darker']};
            }}
            QCheckBox::indicator:hover {{
                border-color: {COLORS['neon_pink']};
            }}
            QCheckBox::indicator:checked {{
                background: qlineargradient(x1:0, x2:1, stop:0 {COLORS['neon_pink']}, stop:1 {COLORS['neon_purple']});
                border-color: {COLORS['neon_pink']};
            }}
        """)


class CyberTerminal(QWidget):
    """Terminal COMPACT 🐱"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.border_radius = 12
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        self.text_area = QTextEdit()
        self.text_area.setReadOnly(True)
        self.text_area.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                border: none;
                color: {COLORS['text_primary']};
                font-family: 'Consolas', monospace;
                font-size: 11px;
            }}
        """)
        layout.addWidget(self.text_area)
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        path = QPainterPath()
        path.addRoundedRect(1, 1, self.width()-2, self.height()-2, self.border_radius, self.border_radius)
        
        painter.fillPath(path, QColor(COLORS['bg_darker']))
        
        gradient = QLinearGradient(0, 0, self.width(), 0)
        gradient.setColorAt(0, QColor(COLORS['neon_mint']))
        gradient.setColorAt(1, QColor(COLORS['neon_cyan']))
        
        painter.setPen(QPen(QBrush(gradient), 2))
        painter.drawPath(path)
    
    def add_line(self, message: str, level: str = "info"):
        from datetime import datetime
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        colors = {
            "info": COLORS["neon_cyan"],
            "success": COLORS["neon_mint"],
            "warning": COLORS["neon_yellow"],
            "error": COLORS["neon_coral"],
        }
        icons = {"info": "▸", "success": "✓", "warning": "⚠", "error": "✗"}
        
        color = colors.get(level, colors["info"])
        icon = icons.get(level, "▸")
        
        html = f'<div style="margin: 2px 0; font-size: 11px;">'
        html += f'<span style="color:{COLORS["text_muted"]}">{timestamp}</span> '
        html += f'<span style="color:{color}">{icon}</span> '
        html += f'<span style="color:{COLORS["text_primary"]}">{message}</span></div>'
        
        self.text_area.insertHtml(html)
        self.text_area.verticalScrollBar().setValue(self.text_area.verticalScrollBar().maximum())
    
    def clear(self):
        self.text_area.clear()


class CyberTable(QTableWidget):
    """Table với checkbox và toggle button 🐱"""
    
    def __init__(self, columns: list, parent=None):
        super().__init__(parent)
        
        self.setColumnCount(len(columns))
        self.setHorizontalHeaderLabels([c.upper() for c in columns])
        
        self.setStyleSheet(f"""
            QTableWidget {{
                background: transparent;
                border: none;
                gridline-color: {COLORS['border']};
                font-size: 13px;
            }}
            QTableWidget::item {{
                padding: 8px;
                border-bottom: 1px solid {COLORS['border']};
                color: {COLORS['text_primary']};
            }}
            QTableWidget::item:selected {{
                background: rgba(255, 107, 157, 0.15);
            }}
            QTableWidget::item:hover {{
                background: {COLORS['bg_hover']};
            }}
            QHeaderView::section {{
                background: {COLORS['bg_darker']};
                color: {COLORS['neon_cyan']};
                padding: 10px 8px;
                border: none;
                border-bottom: 2px solid {COLORS['neon_cyan']};
                font-weight: bold;
                font-size: 11px;
                letter-spacing: 1px;
            }}
        """)
        
        header = self.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Stretch)  # NAME column stretches
        header.setMinimumSectionSize(50)
        
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(45)
        self.setShowGrid(False)
        self.setSelectionBehavior(QTableWidget.SelectRows)
    
    def add_row_with_widgets(self, data: list, row: int):
        """Add row với checkbox và toggle button - CENTERED"""
        # Checkbox - centered
        checkbox_widget = QWidget()
        checkbox_widget.setStyleSheet("background: transparent;")
        checkbox_layout = QHBoxLayout(checkbox_widget)
        checkbox_layout.setContentsMargins(0, 0, 0, 0)
        checkbox_layout.setAlignment(Qt.AlignCenter)
        checkbox = CyberCheckBox()
        checkbox_layout.addWidget(checkbox)
        self.setCellWidget(row, 0, checkbox_widget)
        
        # Data columns
        for col, value in enumerate(data):
            item = QTableWidgetItem(str(value))
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.setItem(row, col + 1, item)
        
        # Toggle button - centered
        action_widget = QWidget()
        action_widget.setStyleSheet("background: transparent;")
        action_layout = QHBoxLayout(action_widget)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setAlignment(Qt.AlignCenter)
        
        toggle_btn = ToggleButton()
        action_layout.addWidget(toggle_btn)
        
        self.setCellWidget(row, self.columnCount() - 1, action_widget)
