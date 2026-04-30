DARK_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #1e1e1e;
    color: #d4d4d4;
}

/* General item views */
QTreeView, QListView, QTableView, QListWidget, QTreeWidget {
    background-color: #252526;
    alternate-background-color: #2a2d2e;
    border: 1px solid #3c3c3c;
    selection-background-color: #094771;
    selection-color: #ffffff;
    gridline-color: #3c3c3c;
}

/* Remove focus outline */
QTreeView:focus, QListView:focus, QTableView:focus {
    outline: none;
}

/* Headers */
QHeaderView::section {
    background-color: #2d2d30;
    color: #d4d4d4;
    padding: 4px;
    border: 1px solid #3c3c3c;
}

/* Scrollbars */
QScrollBar:vertical {
    background: #2a2a2a;
    width: 12px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #3c3c3c;
    min-height: 20px;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover {
    background: #505050;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
}

/* Buttons */
QPushButton {
    background-color: #2c3e50;
    color: white;
    border: none;
    padding: 6px 12px;
    border-radius: 4px;
}
QPushButton:hover {
    background-color: #34495e;
}
QPushButton:pressed {
    background-color: #1f2a38;
}

/* Inputs */
QLineEdit, QTextEdit, QPlainTextEdit {
    background-color: #252526;
    color: #d4d4d4;
    border: 1px solid #3c3c3c;
    padding: 4px;
}

/* ComboBox */
QComboBox {
    background-color: #252526;
    border: 1px solid #3c3c3c;
    padding: 4px;
}
QComboBox::drop-down {
    border: none;
}

/* Splitter */
QSplitter::handle {
    background-color: #3c3c3c;
}

/* Labels */
QLabel {
    color: #d4d4d4;
}
"""