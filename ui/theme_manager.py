from __future__ import annotations
from pathlib import Path
from PyQt6.QtGui import QFontDatabase
from utilities import get_resource_path

import logging
logger = logging.getLogger(f'radiata.{__name__}')

class DarkTheme:
    BG_MAIN           = '#202020'
    BG_SECONDARY      = '#161616'
    BG_TERTIARY       = '#1a1a1a'
    BORDER            = '#333333'
    TEXT              = '#dcddde'
    ACCENT            = '#7f6df2'
    ACCENT_HOVER      = '#8875ff'
    SUCCESS           = '#a8a9ad'
    INTERACTIVE       = '#2a2a2a'
    INTERACTIVE_HOVER = '#303030'
    SCROLL_BG         = 'rgba(255, 255, 255, 0.05)'
    SCROLL_HOVER      = 'rgba(255, 255, 255, 0.2)'
    FONT_SANS         = '"Segoe UI", "Open Sans", sans-serif'
    FONT_MONO         = '"Courier New", monospace'
    BASE_FONT_WEIGHT  = 'normal'

class RadiataTheme:
    BG_MAIN           = '#94825a'
    BG_SECONDARY      = '#847441'
    BG_TERTIARY       = '#ab9c65'
    BORDER            = '#5b5851'
    TEXT              = '#302217'
    ACCENT            = '#f1c562'
    ACCENT_HOVER      = '#ddcaac'
    SUCCESS           = '#f1c562'
    INTERACTIVE       = '#e5dabe'
    INTERACTIVE_HOVER = '#efe5aa'
    SCROLL_BG         = 'rgba(255, 255, 255, 0.05)'
    SCROLL_HOVER      = 'rgba(255, 255, 255, 0.2)'
    FONT_SANS         = '"Segoe UI", "Open Sans", sans-serif'
    FONT_MONO         = '"Courier New", monospace'
    BASE_FONT_WEIGHT  = 'bold'

class ThemeManager:
    current_font_size = 14
    active_theme: type[DarkTheme] | type[RadiataTheme] = DarkTheme
    _raw_template = None
    _app = None

    THEMES = {
        'Radiata': RadiataTheme,
        'Dark':    DarkTheme
    }

    @classmethod
    def initialize(cls, app):
        '''Called once at startup'''
        cls._app = app
        cls.apply_theme('Dark', delta=0)

    @classmethod
    def apply_theme(cls, theme_name: str = 'Dark', delta: int = 0) -> None:
        if not cls._app:
            logger.warning('No application initialized. Make sure that a window exists to apply a theme to.')
            return
        if theme_name in cls.THEMES:
            cls.active_theme = cls.THEMES[theme_name]

        cls.current_font_size = max(8, min(32, cls.current_font_size + delta))
        fs = cls.current_font_size
        theme = cls.active_theme

        # Cache the raw string templates so we don't hit the disk constantly
        if cls._raw_template is None:
            qss_path = get_resource_path('ui/assets/static_sheet.qss')
            font_path = get_resource_path('ui/assets/dynamic_sheet.qss')

            base_qss = qss_path.read_text(encoding='utf-8') if qss_path.exists() else ''
            font_qss = font_path.read_text(encoding='utf-8') if font_path.exists() else ''
            cls._raw_template = base_qss + '\n' + font_qss

        # The mapping dictionary
        replacements = {
            '{BG_MAIN}':           theme.BG_MAIN,
            '{BG_SECONDARY}':      theme.BG_SECONDARY,
            '{BG_TERTIARY}':       theme.BG_TERTIARY,
            '{BORDER}':            theme.BORDER,
            '{TEXT}':              theme.TEXT,
            '{ACCENT}':            theme.ACCENT,
            '{ACCENT_HOVER}':      theme.ACCENT_HOVER,
            '{SUCCESS}':           theme.SUCCESS,
            '{INTERACTIVE}':       theme.INTERACTIVE,
            '{INTERACTIVE_HOVER}': theme.INTERACTIVE_HOVER,
            '{SCROLL_BG}':         theme.SCROLL_BG,
            '{SCROLL_HOVER}':      theme.SCROLL_HOVER,
            '{FONT_SANS}':         theme.FONT_SANS,
            '{FONT_MONO}':         theme.FONT_MONO,
            '{BASE_FONT_WEIGHT}':  theme.BASE_FONT_WEIGHT,
            
            '{FONT_SIZE}':         f'{fs}px',
            '{FONT_SIZE_SMALL}':   f'{fs - 2}px',
            '{FONT_SIZE_LARGE}':   f'{fs + 5}px',
            '{BTN_PADDING}':       f'{fs // 4}px {fs // 2}px',
            '{BTN_PADDING_LARGE}': f'{fs}px {fs * 2}px',
            '{BTN_MIN_WIDTH}':     f'{fs * 2}px',
        }
        full_sheet = cls._raw_template
        for key, value in replacements.items():
            full_sheet = full_sheet.replace(key, value)

        cls._app.setStyleSheet(full_sheet)

        logger.debug(f'Stylesheet {theme_name} initialised.')

    @staticmethod
    def load_custom_font(font_path: Path):
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id == -1:
            print(f'Failed to load font: {font_path}')
            return None
        return QFontDatabase.applicationFontFamilies(font_id)[0]