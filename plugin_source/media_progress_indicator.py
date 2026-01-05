"""
Beautiful, non-intrusive media download progress indicator for AnkiCollab.
Shows download progress in the toolbar without stealing focus or interrupting user workflow.
"""

import time
from typing import Optional

from aqt.qt import (
    QWidget, QHBoxLayout, QLabel, QProgressBar, 
    QFrame, QGraphicsOpacityEffect, QApplication,
    QTimer, QPropertyAnimation, QEasingCurve,
    QFont, QPalette
)

from aqt import mw

class MediaProgressIndicator(QWidget):
    """
    Progress indicator that appears in the main window's toolbar area.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent or mw)
        self.setup_ui()
        self.setup_animations()
        self.hide()  # Initially hidden
        
        # State tracking
        self._is_active = False
        self._current_files = 0
        self._total_files = 0
        self._operation_type = "download"
        
    def setup_ui(self):
        """Create the UI elements"""
        self.setFixedHeight(28)
        self.setMinimumWidth(300)
        
        # Main layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(8)
        
        # Icon label
        self.icon_label = QLabel("🔄")
        self.icon_label.setFont(QFont("Segoe UI Emoji", 10))
        
        # Status text
        self.status_label = QLabel("Preparing...")
        font = QFont()
        font.setPointSize(9)
        font.setWeight(QFont.Weight.Medium)
        self.status_label.setFont(font)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: none;
                border-radius: 3px;
                background-color: rgba(0, 0, 0, 0.1);
            }
            QProgressBar::chunk {
                border-radius: 3px;
                background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #4CAF50, stop: 1 #45a049);
            }
        """)
        
        # Count label
        self.count_label = QLabel("0/0")
        count_font = QFont()
        count_font.setPointSize(8)
        count_font.setWeight(QFont.Weight.Normal)
        self.count_label.setFont(count_font)
        
        # Add widgets to layout
        layout.addWidget(self.icon_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar, 1)  # Stretch factor 1
        layout.addWidget(self.count_label)
        
        # Frame styling
        self.setStyleSheet("""
            MediaProgressIndicator {
                background-color: rgba(255, 255, 255, 0.95);
                border: 1px solid rgba(0, 0, 0, 0.1);
                border-radius: 8px;
                color: #333;
            }
        """)
        
        # Position in toolbar area
        self.position_in_toolbar()
        
    def setup_animations(self):
        """Setup smooth fade in/out animations"""
        # Opacity effect for smooth animations
        self.opacity_effect = QGraphicsOpacityEffect()
        self.setGraphicsEffect(self.opacity_effect)
        
        # Fade animation
        self.fade_animation = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.fade_animation.setDuration(300)
        self.fade_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        
        # Icon rotation timer for activity indicator
        self.rotation_timer = QTimer()
        self.rotation_timer.timeout.connect(self.rotate_icon)
        self._rotation_angle = 0
        
    def position_in_toolbar(self):
        """Position the indicator in the toolbar area"""
        if not mw:
            return
            
        # Position in the top-right area of the main window
        parent_rect = mw.rect()
        self.move(parent_rect.width() - self.width() - 20, 50)
        
    def start_progress(self, operation_type: str = "download", total_files: int = 0):
        """Start showing progress with smooth fade-in"""
        self._is_active = True
        self._operation_type = operation_type
        self._total_files = total_files
        self._current_files = 0
        
        # Update initial state
        self.icon_label.setText("⏳" if operation_type == "download" else "⬆️")
        self.status_label.setText(f"Media {operation_type} starting...")
        self.progress_bar.setMaximum(total_files if total_files > 0 else 100)
        self.progress_bar.setValue(0)
        self.count_label.setText(f"0/{total_files:,}")
        
        # Reposition and show with fade-in
        self.position_in_toolbar()
        self.show()
        self.raise_()
        
        # Animate fade-in
        self.fade_animation.setStartValue(0.0)
        self.fade_animation.setEndValue(1.0)
        self.fade_animation.start()
        
        # Start rotation animation
        self.rotation_timer.start(1000)
        
    def update_progress(self, progress_ratio: float, current_files: Optional[int] = None):
        """Update progress with smooth transitions"""
        if not self._is_active:
            return
            
        # Update current files count
        if current_files is not None:
            self._current_files = current_files
        else:
            self._current_files = int(progress_ratio * self._total_files)
            
        # Update progress bar
        if self._total_files > 0:
            self.progress_bar.setValue(self._current_files)
        else:
            self.progress_bar.setValue(int(progress_ratio * 100))
            
        # Update labels
        action = "Downloading" if self._operation_type == "download" else "Uploading"
        self.status_label.setText(f"{action} media files...")
        self.count_label.setText(f"{self._current_files:,}/{self._total_files:,}")
        
        # Ensure the widget stays visible and positioned correctly
        self.position_in_toolbar()
        
    def complete_progress(self, success: bool = True, message: Optional[str] = None):
        """Complete progress with fade-out"""
        if not self._is_active:
            return
            
        self._is_active = False
        self.rotation_timer.stop()
        
        # Show completion state briefly
        if success:
            self.icon_label.setText("✅")
            completion_msg = message or f"Completed! {self._current_files:,} files processed"
        else:
            self.icon_label.setText("❌")
            completion_msg = message or f"Failed after {self._current_files:,} files"
            
        self.status_label.setText(completion_msg)
        self.progress_bar.setValue(self.progress_bar.maximum())
        
        # Brief pause to show completion, then fade out
        QTimer.singleShot(2000, self.fade_out)
        
    def fade_out(self):
        """Fade out and hide the indicator"""
        self.fade_animation.setStartValue(1.0)
        self.fade_animation.setEndValue(0.0)
        self.fade_animation.finished.connect(self.hide)
        self.fade_animation.start()
        
    def rotate_icon(self):
        """Rotate the activity icon for visual feedback"""
        if not self._is_active:
            return
            
        icons = ["⏳", "⌛"] if self._operation_type == "download" else ["⬆️", "⬇️"]
        self._rotation_angle = (self._rotation_angle + 1) % len(icons)
        self.icon_label.setText(icons[self._rotation_angle])


# Global instance for easy access
_media_progress_indicator: Optional[MediaProgressIndicator] = None

def get_media_progress_indicator() -> MediaProgressIndicator:
    """Get or create the global media progress indicator"""
    global _media_progress_indicator
    if _media_progress_indicator is None:
        _media_progress_indicator = MediaProgressIndicator()
    return _media_progress_indicator

def show_media_progress(operation_type: str = "download", total_files: int = 0):
    """Start showing media progress"""
    indicator = get_media_progress_indicator()
    indicator.start_progress(operation_type, total_files)

def update_media_progress(progress_ratio: float, current_files: Optional[int] = None):
    """Update media progress"""
    indicator = get_media_progress_indicator()
    indicator.update_progress(progress_ratio, current_files)

def complete_media_progress(success: bool = True, message: Optional[str] = None):
    """Complete media progress"""
    indicator = get_media_progress_indicator()
    indicator.complete_progress(success, message)
