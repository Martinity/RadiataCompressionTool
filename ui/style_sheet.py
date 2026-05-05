
class DarkTheme:
    BG_MAIN           = '#202020'
    BG_SECONDARY      = '#161616'
    BG_TERTIARY       = '#1a1a1a'
    BORDER            = '#333333'
    TEXT              = '#dcddde'
    ACCENT            = '#7f6df2'
    ACCENT_HOVER      = '#8875ff'
    SUCCESS           = '#197300'
    INTERACTIVE       = '#2a2a2a'
    INTERACTIVE_HOVER = '#303030'
    SCROLL_BG         = 'rgba(255, 255, 255, 0.05)'
    SCROLL_HOVER      = 'rgba(255, 255, 255, 0.2)'
    FONT_SIZE         = '14px'
    FONT_SIZE_SMALL   = '12px'
    FONT_SIZE_LARGE   = '19px'
    BTN_PADDING       = '7px 14px'
    BTN_PADDING_LARGE = '14px 28px'
    BTN_MIN_WIDTH     = '140px'


STYLESHEET = """
QMainWindow, QWidget {{
    background-color: {BG_MAIN};
    color: {TEXT};
    font-size: {FONT_SIZE};
}}

/* General item views */
QTreeView, QListView, QTableView, QListWidget, QTreeWidget {{
    background-color: {BG_SECONDARY};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
    gridline-color: {BORDER};
}}

QTreeView::item:hover, QListView::item:hover, QTableView::item:hover, QListWidget::item:hover, QTreeWidget::item:hover{{
    background-color: {BG_TERTIARY};
}}

/* Remove focus outline */
QTreeView:focus, QListView:focus, QTableView:focus {{
    outline: none;
}}

/* Headers */
QHeaderView::section {{
    background-color: {BG_SECONDARY};
    color: {TEXT};
    font-size: {FONT_SIZE};
    font-weight: bold;
    padding: 4px;
    border: none;
}}

QHeaderView::section:hover {{
    background-color: {BG_TERTIARY}
}}

/* Scrollbars */
QScrollBar:vertical, QScrollBar::horizontal {{
    background: {BG_SECONDARY};
    width: 12px;
    margin: 0px;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {INTERACTIVE};
    min-height: 20px;
    border-radius: 5px;
}}
QScrollBar::handle:hover {{
    background: {SCROLL_HOVER};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0; 
    width: 0;
}}

/* Menus & Menu Bar */
QMenuBar {{
    background-color: {BG_MAIN};
    color: {TEXT};
}}
QMenuBar::item:selected {{
    background-color: {BORDER};
}}
QMenu {{
    background-color: {BG_SECONDARY};
    color: {TEXT};
    border: 1px solid {BORDER};
}}
QMenu::item:selected {{
    background-color: {ACCENT};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 0px;
}}

/* Buttons */
QPushButton {{
    background-color: {INTERACTIVE};
    color: {TEXT};
    border: none;
    padding: {BTN_PADDING};
    border-radius: 4px;
    min-width: {BTN_MIN_WIDTH};
}}
QPushButton:hover {{
    background-color: {INTERACTIVE_HOVER};
}}

QPushButton:pressed {{
    background-color: {ACCENT};
}}

/* Inputs & Consoles */
QLineEdit, QTextEdit, QPlainTextEdit {{
    background-color: {BG_SECONDARY};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px;
}}

/* Splitter */
QSplitter::handle {{
    background-color: {BORDER};
}}

/* Progress Bar */
QProgressBar {{
    border: 1px solid {BORDER};
    background-color: {BG_SECONDARY};
    text-align: center;
    border-radius: 4px;
    color: {TEXT};
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 3px;
}}

/* Labels */
QLabel {{
    color: {TEXT};
}}

/* ComboBox */
QComboBox {{
    background-color: {BG_SECONDARY};
    border: 1px solid {BORDER};
    padding: 4px;
}}
QComboBox::drop-down {{
    border: none;
}}

/* ------------------ Custom UI objects ------------------- */
#WelcomeSubtitle {{
    font-size: {FONT_SIZE};
    color: #888888;
    margin-bottom: 4px;
}}

#WelcomeButton {{
    font-size: {FONT_SIZE_LARGE};
    border-radius: 8px;
    padding: {BTN_PADDING_LARGE};
}}

#ConfirmButton {{
    font-weight: bold;
    background-color: {ACCENT};
    padding: {BTN_PADDING};
    min-width: {BTN_MIN_WIDTH};
}}

#ConfirmButton:hover {{
    background-color: {ACCENT_HOVER};
}}

#ConfirmButton:disabled {{
    background-color: {BG_MAIN};
    color: {BG_MAIN};
}}

#SectionHeader {{
    font-weight: bold;
    font-size: {FONT_SIZE};
}}

#PageTitle {{
    font-size: {FONT_SIZE_LARGE};
    font-weight: bold;
}}

#LogOutput {{
    font-family: Consolas, "Courier New", monospace;
}}

/* Plugin-Specific */
#EditorToolbar {{
    background-color: {BG_MAIN};
    border-bottom: 1px solid {BORDER};
}}

#HexView {{
    background-color: {BG_SECONDARY};
    gridline-color: {BORDER};
    border: none;
}}

#HexView::item:hover {{
    background-color: {BG_TERTIARY};
}}

#HexView::item:selected {{
    background-color: {INTERACTIVE_HOVER};
}}

#LogView {{
    background-color: {BG_SECONDARY};
    border: none;
}}

#LogView {{
    font-size: {FONT_SIZE_SMALL};
}}

#LogView QScrollBar:vertical {{
    background: {BG_SECONDARY};
}}

#FloatClearButton {{
    background-color: {INTERACTIVE};
    border: none;
    margin: {BTN_PADDING};
    padding: {BTN_PADDING};
    font-size: {FONT_SIZE_SMALL};
    min-width: {BTN_MIN_WIDTH};
}}

#FloatClearButton:hover {{
    background-color: {INTERACTIVE_HOVER};
    border: none;
}}

#FloatClearButton:pressed {{
    background-color: {ACCENT};
}}
"""

class ThemeManager:
    current_font_size = 14

    @classmethod
    def get_theme_with_zoom(cls, theme_class, delta: int):
        '''Adjust font size and return stylesheet'''
        cls.current_font_size = max(8, min(32, cls.current_font_size + delta))
        fs = cls.current_font_size
        theme_class.FONT_SIZE       = f'{fs}px'
        theme_class.FONT_SIZE_SMALL = f'{fs - 2}px'
        theme_class.FONT_SIZE_LARGE = f'{fs + 5}px'

        theme_class.BTN_PADDING       = f'{fs // 4}px {fs // 2}px'
        theme_class.BTN_PADDING_LARGE = f'{fs}px {fs * 2}px'
        theme_class.BTN_MIN_WIDTH     = f'{fs * 2}px'

        theme_class.CONTENT_MARGIN = max(20, fs * 3)
        return get_stylesheet(theme_class)

def get_stylesheet(theme_class) -> str:
    '''Maps class attributes to STYLESHEET placeholders'''
    return STYLESHEET.format(
        BG_MAIN           = theme_class.BG_MAIN,
        BG_SECONDARY      = theme_class.BG_SECONDARY,
        BG_TERTIARY       = theme_class.BG_TERTIARY,
        BORDER            = theme_class.BORDER,
        TEXT              = theme_class.TEXT,
        ACCENT            = theme_class.ACCENT,
        ACCENT_HOVER      = theme_class.ACCENT_HOVER,
        SUCCESS           = theme_class.SUCCESS, 
        INTERACTIVE       = theme_class.INTERACTIVE, 
        INTERACTIVE_HOVER = theme_class.INTERACTIVE_HOVER,
        SCROLL_BG         = theme_class.SCROLL_BG,
        SCROLL_HOVER      = theme_class.SCROLL_HOVER,
        FONT_SIZE         = theme_class.FONT_SIZE,
        FONT_SIZE_SMALL   = theme_class.FONT_SIZE_SMALL,
        FONT_SIZE_LARGE   = theme_class.FONT_SIZE_LARGE,
        BTN_PADDING       = theme_class.BTN_PADDING,
        BTN_PADDING_LARGE = theme_class.BTN_PADDING_LARGE,
        BTN_MIN_WIDTH     = theme_class.BTN_MIN_WIDTH,
    )
