"""
FB Manager Pro - Cyberpunk Effects (Simplified - No Animation)
🐱 CUTE CAT Edition - Pastel Neon 🐱
"""

from PySide6.QtWidgets import QWidget, QLabel, QFrame, QGraphicsOpacityEffect, QVBoxLayout
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, Property, QRect, QPoint
from PySide6.QtGui import QPainter, QColor, QPen, QLinearGradient, QFont, QBrush, QRadialGradient
import random
import math


class TearGlitchText(QWidget):
    """Text đơn giản - không animation"""
    
    def __init__(self, text: str, color: str = "#ff6b9d", size: int = 24, parent=None):
        super().__init__(parent)
        self.base_text = text
        self.base_color = QColor(color)
        self.font_size = size
        self.setMinimumHeight(size + 14)
        self.setMinimumWidth(len(text) * (size // 2 + 3) + 40)
    
    def paintEvent(self, event):
        try:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            
            font = QFont("Consolas", self.font_size, QFont.Bold)
            painter.setFont(font)
            painter.setPen(self.base_color)
            painter.drawText(self.rect(), Qt.AlignCenter, self.base_text)
            
            painter.end()
        except:
            pass


class GlitchText(QLabel):
    """Glitch text đơn giản"""
    
    def __init__(self, text: str, color: str = "#ff6b9d", size: int = 14, parent=None):
        super().__init__(text, parent)
        self.setStyleSheet(f"""
            color: {color};
            font-size: {size}px;
            font-weight: bold;
            font-family: 'Consolas', monospace;
        """)


class PulsingDot(QWidget):
    """Dot đơn giản - không pulse"""
    
    def __init__(self, color: str = "#6bffb8", size: int = 8, parent=None):
        super().__init__(parent)
        self.color = QColor(color)
        self.dot_size = size
        self.setFixedSize(size + 4, size + 4)
    
    def paintEvent(self, event):
        try:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setBrush(QBrush(self.color))
            painter.setPen(Qt.NoPen)
            
            x = (self.width() - self.dot_size) // 2
            y = (self.height() - self.dot_size) // 2
            painter.drawEllipse(x, y, self.dot_size, self.dot_size)
            
            painter.end()
        except:
            pass


class CyberGrid(QWidget):
    """Grid đơn giản - không animation"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
    
    def paintEvent(self, event):
        try:
            painter = QPainter(self)
            
            pen = QPen(QColor(255, 107, 157, 10))
            pen.setWidth(1)
            painter.setPen(pen)
            
            cell_size = 40
            for x in range(0, self.width(), cell_size):
                painter.drawLine(x, 0, x, self.height())
            for y in range(0, self.height(), cell_size):
                painter.drawLine(0, y, self.width(), y)
            
            painter.end()
        except:
            pass


class ScanlineOverlay(QWidget):
    """Scanline đơn giản - không animation"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
    
    def paintEvent(self, event):
        try:
            painter = QPainter(self)
            
            pen = QPen(QColor(255, 107, 157, 5))
            pen.setWidth(1)
            painter.setPen(pen)
            
            for y in range(0, self.height(), 4):
                painter.drawLine(0, y, self.width(), y)
            
            painter.end()
        except:
            pass


class NeonRain(QWidget):
    """Neon rain - disabled"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
    
    def paintEvent(self, event):
        pass  # Disabled


class NeonFlash(QWidget):
    """Neon flash đơn giản"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
    
    def flash(self, color="#ff6b9d", duration=200):
        pass  # Disabled
    
    def paintEvent(self, event):
        pass  # Disabled


class HologramEffect(QWidget):
    """Hologram đơn giản"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
    
    def paintEvent(self, event):
        try:
            painter = QPainter(self)
            
            glow_color = QColor(255, 107, 157, 20)
            glow_size = 50
            
            gradient = QRadialGradient(0, 0, glow_size)
            gradient.setColorAt(0, glow_color)
            gradient.setColorAt(1, QColor(0, 0, 0, 0))
            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(-glow_size//2, -glow_size//2, glow_size, glow_size)
            
            painter.end()
        except:
            pass


class DataStream(QWidget):
    """Data stream - disabled"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
    
    def paintEvent(self, event):
        pass  # Disabled
