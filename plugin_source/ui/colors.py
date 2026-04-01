"""
AnkiCollab Unified Color System

Single source of truth for all colors used in the addon.
All dialogs and components should import colors from here.

Design principles:
- Minimal, clean aesthetic that blends with Anki's native look
- Colors used sparingly as accents only
- Prefer neutral grays and subtle contrasts
- Avoid visual noise
"""

from aqt.theme import theme_manager


def get_colors() -> dict:
    """
    Returns theme-aware color palette.
    
    Colors are intentionally muted and minimal.
    Primary accent is used sparingly for key interactive elements.
    """
    dark = theme_manager.night_mode
    
    return {
        # === Accent (use sparingly) ===
        # Subtle blue accent for primary actions and focus states
        'primary': '#6B9AC4' if dark else '#3B82F6',
        'primary_hover': '#7EAED4' if dark else '#2563EB',
        
        # === Semantic Colors (only where meaning is essential) ===
        'success': '#5A9E68' if dark else '#22C55E',
        'success_hover': '#6DB37A' if dark else '#16A34A',
        
        'warning': '#D4A04A' if dark else '#F59E0B',
        'warning_hover': '#E0B060' if dark else '#D97706',
        
        'danger': '#C4655A' if dark else '#EF4444',
        'danger_hover': '#D47A70' if dark else '#DC2626',
        
        # === Surfaces (match Anki closely) ===
        'background': '#1E1E1E' if dark else '#FFFFFF',
        'surface': '#2A2A2A' if dark else '#FAFAFA',
        'surface_elevated': '#333333' if dark else '#FFFFFF',
        'surface_hover': '#3A3A3A' if dark else '#F5F5F5',
        
        # === Text (high contrast, readable) ===
        'text_primary': '#E0E0E0' if dark else '#1A1A1A',
        'text_secondary': '#999999' if dark else '#666666',
        'text_muted': '#666666' if dark else '#999999',
        'text_on_accent': '#FFFFFF',
        
        # === Borders (subtle) ===
        'border': '#404040' if dark else '#E0E0E0',
        'border_strong': '#555555' if dark else '#CCCCCC',
        'border_focus': '#6B9AC4' if dark else '#3B82F6',
        
        # === Neutral Buttons ===
        'neutral_bg': '#404040' if dark else '#E5E5E5',
        'neutral_bg_hover': '#4A4A4A' if dark else '#D5D5D5',
        'neutral_text': '#E0E0E0' if dark else '#333333',
        
        # === Info/Help (subtle, not attention-grabbing) ===
        'info_bg': '#2A3038' if dark else '#F5F7FA',
        'info_border': '#3A4048' if dark else '#E0E5EB',
        'info_text': '#A0A8B0' if dark else '#505860',
        
        # === Legacy aliases for compatibility ===
        'accent': '#5A9E68' if dark else '#22C55E',  # Maps to success
        'accent_dark': '#4A8E58' if dark else '#16A34A',  # Maps to success_hover
    }


def get_color(name: str) -> str:
    """Get a single color by name."""
    return get_colors().get(name, '#888888')


def get_button_style(variant: str = 'primary', size: str = 'medium') -> str:
    """
    Get a button stylesheet. Keeps styling minimal and clean.
    
    Args:
        variant: 'primary', 'success', 'danger', 'neutral'
        size: 'small', 'medium', 'large'
    """
    colors = get_colors()
    
    # Minimal size differences
    sizes = {
        'small': {'padding': '5px 12px', 'font_size': '12px', 'radius': '4px'},
        'medium': {'padding': '7px 16px', 'font_size': '13px', 'radius': '4px'},
        'large': {'padding': '9px 20px', 'font_size': '14px', 'radius': '5px'},
    }
    
    # Variant colors - kept subdued
    variants = {
        'primary': {
            'bg': colors['primary'],
            'bg_hover': colors['primary_hover'],
            'text': colors['text_on_accent'],
        },
        'success': {
            'bg': colors['success'],
            'bg_hover': colors['success_hover'],
            'text': colors['text_on_accent'],
        },
        'danger': {
            'bg': colors['danger'],
            'bg_hover': colors['danger_hover'],
            'text': colors['text_on_accent'],
        },
        'neutral': {
            'bg': colors['neutral_bg'],
            'bg_hover': colors['neutral_bg_hover'],
            'text': colors['neutral_text'],
        },
    }
    
    s = sizes.get(size, sizes['medium'])
    v = variants.get(variant, variants['primary'])
    
    return f"""
        QPushButton {{
            background-color: {v['bg']};
            color: {v['text']};
            border: none;
            padding: {s['padding']};
            border-radius: {s['radius']};
            font-size: {s['font_size']};
            font-weight: 500;
        }}
        QPushButton:hover {{
            background-color: {v['bg_hover']};
        }}
        QPushButton:pressed {{
            background-color: {v['bg_hover']};
        }}
        QPushButton:disabled {{
            background-color: {colors['border']};
            color: {colors['text_muted']};
        }}
    """


def get_input_style() -> str:
    """Get a styled input field stylesheet."""
    colors = get_colors()
    
    return f"""
        QLineEdit, QTextEdit, QPlainTextEdit {{
            padding: 8px 10px;
            border: 1px solid {colors['border']};
            border-radius: 4px;
            font-size: 13px;
            background-color: {colors['surface']};
            color: {colors['text_primary']};
        }}
        QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
            border-color: {colors['border_focus']};
        }}
        QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover {{
            background-color: {colors['surface_hover']};
        }}
    """


def get_dialog_style() -> str:
    """Get base dialog stylesheet."""
    colors = get_colors()
    
    return f"""
        QDialog {{
            background-color: {colors['background']};
            color: {colors['text_primary']};
        }}
    """


def get_groupbox_style() -> str:
    """Get styled group box - minimal, blends with Anki."""
    colors = get_colors()
    
    return f"""
        QGroupBox {{
            font-weight: 500;
            font-size: 13px;
            color: {colors['text_primary']};
            border: 1px solid {colors['border']};
            border-radius: 4px;
            margin-top: 10px;
            padding-top: 10px;
            background-color: transparent;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
            color: {colors['text_secondary']};
        }}
    """


def get_info_box_style() -> str:
    """Get styled info/notice box - subtle, not attention-grabbing."""
    colors = get_colors()
    
    return f"""
        background-color: {colors['info_bg']};
        border: 1px solid {colors['info_border']};
        border-radius: 4px;
        padding: 10px;
        color: {colors['info_text']};
    """


def get_table_style() -> str:
    """Get styled table widget - clean and minimal."""
    colors = get_colors()
    dark = theme_manager.night_mode
    
    alt_row = '#252525' if dark else '#FAFAFA'
    selection = '#3A3A3A' if dark else '#E8E8E8'
    
    return f"""
        QTableWidget {{
            background-color: {colors['surface']};
            alternate-background-color: {alt_row};
            border: 1px solid {colors['border']};
            border-radius: 4px;
            gridline-color: {colors['border']};
            color: {colors['text_primary']};
        }}
        QTableWidget::item {{
            padding: 6px;
        }}
        QTableWidget::item:selected {{
            background-color: {selection};
            color: {colors['text_primary']};
        }}
        QHeaderView::section {{
            background-color: {colors['surface']};
            color: {colors['text_secondary']};
            padding: 8px;
            border: none;
            border-bottom: 1px solid {colors['border']};
            font-weight: 500;
        }}
    """


def get_combobox_style() -> str:
    """Get styled combo box - matches input fields."""
    colors = get_colors()
    
    return f"""
        QComboBox {{
            padding: 8px 10px;
            border: 1px solid {colors['border']};
            border-radius: 4px;
            font-size: 13px;
            background-color: {colors['surface']};
            color: {colors['text_primary']};
            min-height: 18px;
        }}
        QComboBox:focus {{
            border-color: {colors['border_focus']};
        }}
        QComboBox:hover {{
            background-color: {colors['surface_hover']};
        }}
        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 24px;
            border-left: 1px solid {colors['border']};
            border-top-right-radius: 4px;
            border-bottom-right-radius: 4px;
            background: {colors['surface_hover']};
        }}
        QComboBox::down-arrow {{
            width: 0;
            height: 0;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 5px solid {colors['text_muted']};
            margin: 0 6px;
        }}
        QComboBox QAbstractItemView {{
            border: 1px solid {colors['border']};
            background-color: {colors['surface']};
            color: {colors['text_primary']};
            selection-background-color: {colors['surface_hover']};
        }}
    """


def get_checkbox_style() -> str:
    """Get styled checkbox - subtle checkmark styling."""
    colors = get_colors()
    
    return f"""
        QCheckBox {{
            color: {colors['text_primary']};
            spacing: 8px;
        }}
        QCheckBox::indicator {{
            width: 16px;
            height: 16px;
        }}
        QCheckBox::indicator:unchecked {{
            border: 1px solid {colors['border_strong']};
            border-radius: 3px;
            background: {colors['surface']};
        }}
        QCheckBox::indicator:checked {{
            border: 1px solid {colors['primary']};
            border-radius: 3px;
            background: {colors['primary']};
        }}
    """


def get_scrollarea_style() -> str:
    """Get styled scroll area - minimal scrollbar."""
    colors = get_colors()
    
    return f"""
        QScrollArea {{
            border: 1px solid {colors['border']};
            border-radius: 4px;
            background-color: {colors['surface']};
        }}
        QScrollBar:vertical {{
            background-color: transparent;
            width: 8px;
        }}
        QScrollBar::handle:vertical {{
            background-color: {colors['border_strong']};
            border-radius: 4px;
            min-height: 20px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0;
        }}
    """
