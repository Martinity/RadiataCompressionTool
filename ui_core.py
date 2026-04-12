from PyQt6.QtCore import Qt, pyqtSignal, QModelIndex
from PyQt6.QtWidgets import QMainWindow, QStackedWidget, QMessageBox, QWidget, QMenu, QVBoxLayout, QSplitter, QMenuBar, QFileDialog, QApplication, QLabel, QPushButton, QTreeView, QListView
from PyQt6.QtGui import QAction
from pathlib import Path
from core.dispatcher import Dispatcher
from core.registry import Registry
from core.resolver import ActionResolver
from plugins.logger import LoggingWindow
from ui.tree_model import VfsCategoryProxyModel, VfsCategoryModel, VfsTreeModel
from plugins.hex_editor import HexEditorWidget

import logging
logger = logging.getLogger(f'radiata.{__name__}')


###---------------------------------------------- Main Window ----------------------------------------###

class MainWindow(QMainWindow):
    def __init__(self, dispatcher: Dispatcher):
        super().__init__(parent=None)
        self.dispatcher = dispatcher
        # Setup View
        self.stack = QStackedWidget()
        self.welcome_page = WelcomePage()
        self.workspace_page = WorkspaceWidget()
        self._setup_ui()
        # Setup Signals
        self.controller = WorkspaceController(self.workspace_page, self.dispatcher)
        self.menu_manager = MainMenuBar(self, self.workspace_page, self.dispatcher)
        self._connect_main_signals()

    def _setup_ui(self):
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self.welcome_page)
        self.stack.addWidget(self.workspace_page)
        self.setWindowTitle('Raidata Modding Tool 2.0 Alpha')
        self.resize(1200,800)

    def _connect_main_signals(self):
        '''Only for main window state signals'''
        self.welcome_page.request_open.connect(self.attempt_load_iso)

    def attempt_load_iso(self, path: Path):
        node, _id = self.dispatcher.load_source(Path(path))
        if node:
            self.controller.init_workspace(node)
            self.stack.setCurrentIndex(1)
            logger.info(f'Successfully loaded: {node.name}')
        else:
            QMessageBox.critical(self, 'Load Error', 'Failed to initialize ISO.')

###------------------------------------------ Workspace UI -------------------------------------###

class WorkspaceWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Setup View Components
        self._init_views()
        # Setup Window Layout
        self._assemble_layout()

    def _init_views(self):
        self.category_view = QListView()

        self.category_model = VfsCategoryModel()
        self.category_model.setStringList(self.category_model.categories)
        self.category_view.setModel(self.category_model)

        self.tree_view = QTreeView()
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.hex_editor = HexEditorWidget()

        self.log_console = LoggingWindow()

    def _assemble_layout(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)

        self.h_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.h_splitter.addWidget(self.category_view)
        self.h_splitter.addWidget(self.tree_view)
        self.h_splitter.addWidget(self.hex_editor)
        self.h_splitter.setSizes([125, 700, 1200])

        self.v_splitter = QSplitter(Qt.Orientation.Vertical)
        self.v_splitter.addWidget(self.h_splitter)
        self.v_splitter.addWidget(self.log_console)
        self.v_splitter.setSizes([600,200])

        layout.addWidget(self.v_splitter)

    def set_center_widget(self, new_widget: QWidget):
        old_widget = self.h_splitter.widget(2)

        if old_widget:
            self.h_splitter.replaceWidget(2, new_widget)
            old_widget.deleteLater()
        else:
            self.h_splitter.insertWidget(2, new_widget)
        new_widget.show()
        self.active_editor = new_widget

###-------------------------------------------- Workspace Signals -------------------------###

class WorkspaceController:
    '''For workspace signals'''
    def __init__(self, workspace: WorkspaceWidget, dispatcher: Dispatcher):
        self.view = workspace
        self.dispatcher = dispatcher

    def init_workspace(self, root_node):
        source_model = VfsTreeModel(root_node)
        proxy_model = VfsCategoryProxyModel()
        proxy_model.setSourceModel(source_model)

        self.view.tree_view.setModel(proxy_model)

        self.view.tree_model = source_model
        self.view.category_proxy_model = proxy_model

        self.view.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        try:
            self.view.category_view.clicked.disconnect()
            self.view.tree_view.customContextMenuRequested.disconnect()
        except TypeError:
            pass

        # self.view.category_view.blockSignals(True)
        self.view.category_view.clicked.connect(self.handle_category_select)
        # self.view.category_view.blockSignals(False)
        self.view.tree_view.customContextMenuRequested.connect(self.handle_context_menu)

        tree_selection = self.view.tree_view.selectionModel()
        tree_selection.currentChanged.connect(self.handle_tree_select)

        self.view.tree_view.setColumnWidth(0,300)
        self.view.tree_view.expandToDepth(0)

    def _wire_signals(self):
        self.view.category_view.clicked.connect(self.handle_category_select)
        self.view.tree_view.customContextMenuRequested.connect(self.handle_context_menu)
        if self.view.tree_view.selectionModel():
            self.view.tree_view.selectionModel().currentChanged.connect(self.handle_tree_select)

    def handle_category_select(self, index: QModelIndex):
        selected_category = self.view.category_model.data(index, Qt.ItemDataRole.DisplayRole)
        self.view.category_proxy_model.set_category(selected_category)
        self.view.tree_view.expandAll()
        logger.info(f'Filtering by: {selected_category}')

    def handle_tree_select(self, current: QModelIndex, previous: QModelIndex):
        if not current.isValid(): return
        source_index = self.view.category_proxy_model.mapToSource(current)
        node = source_index.data(Qt.ItemDataRole.UserRole)
        if node:
            logger.debug(f'Selected: {current.data()}')
            supported_profiles = Registry.get_editor_for(node)
            if supported_profiles:
                self.launch_editor(node, supported_profiles)

    def handle_context_menu(self, position):
        proxy_index = self.view.tree_view.indexAt(position)
        if not proxy_index.isValid(): 
            return

        source_index = self.view.category_proxy_model.mapToSource(proxy_index)
        node = source_index.data(Qt.ItemDataRole.UserRole)
        if not node: 
            return

        menu = QMenu(self.view)
        editor_class = Registry.get_editor_for(node)

        if editor_class:
            action_text = f'Open with {editor_class.__name__}'
            action = menu.addAction(action_text)
            font = action.font()
            font.setBold(True)
            action.setFont(font)
            action.triggered.connect(lambda checked=False, e=editor_class, n=node: self.launch_editor(n, e))

        menu.addSeparator()

        supported_actions = ActionResolver.get_supported_actions(node)
        for action_name in supported_actions:
            action = menu.addAction(action_name)
            action.triggered.connect(lambda checked=False, a=action_name, n=node: self.route_action(n, a))
        
        global_pos = self.view.tree_view.viewport().mapToGlobal(position)
        menu.exec(global_pos)

    def launch_editor(self, node, editor_class):

        new_editor = editor_class()

        raw_bytes = self.dispatcher.get_node_data(node) # enforced in contract
        new_editor.load_node(node, raw_bytes) # enforced in contract

        self.view.set_center_widget(new_editor)
        logger.info(f'Opened "{node.name}" in {editor_class.__name__}')

    def route_action(self, node, action_name):
        logger.debug(f'User requested "{action_name}" on {node.name}')
        if action_name == 'Hex View':
            editor_class = Registry.get_editor_for(node)
            self.launch_editor(node, editor_class)
        elif action_name == 'Properties':
            '''TODO add get_properties for handlers'''
        elif action_name == 'Unpack node':
            self.dispatcher.load_source(node)
            self.view.tree_view.expandAll()
        else:
            self.dispatcher.execute_node_action(node, action_name)

###-------------------------------------- Welcome Page --------------------------------------###

class WelcomePage(QWidget):
    request_open = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(50,50,50,50)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        subtitle = QLabel('Select a Radiata Stories ISO...')
        subtitle.setStyleSheet('font-size: 14px; color: #888; margin-bottom: 40px;')

        self.button = QPushButton('Open ISO', self)
        self.button.setFixedSize(220,60)
        self.button.setStyleSheet('font-size: 16px; background-color: #2c3e50; color: white; border-radius: 8px;')
        self.button.clicked.connect(self.open_file_dialog)

        layout.addWidget(subtitle)
        layout.addWidget(self.button)

    def open_file_dialog(self):
        file_path, _ = QFileDialog.getOpenFileName(self, 'Open ISO', '', 'ISO Files (*.iso);;All Files (*)')
        if file_path:
            self.request_open.emit(file_path)

###------------------------------------- Menu Bar ------------------------------------------###

class MainMenuBar:
    def __init__(self, main_window, workspace_page, dispatcher):
        self.window = main_window
        self.workspace = workspace_page
        self.dispatcher = dispatcher

        self. menu_bar = self.window.menuBar()
        self._build_file_menu()
        self._build_view_menu()

    def _build_file_menu(self):
        file_menu = self.menu_bar.addMenu('&File')

        open_action = QAction('Open ISO', self.window)
        open_action.setShortcut('Ctrl+O')
        open_action.triggered.connect(self._handle_open)
        file_menu.addAction(open_action)


        close_action = QAction('Close ISO', self.window)
        close_action.setShortcut('Ctrl+W')
        close_action.triggered.connect(self._handle_close)
        file_menu.addAction(close_action)

        file_menu.addSeparator()

        exit_action = QAction('Exit ISO', self.window)
        exit_action.setShortcut('Ctrl+Q')
        exit_action.triggered.connect(self._handle_exit)
        file_menu.addAction(exit_action)

    def _build_view_menu(self):
        view_menu = self.menu_bar.addMenu('&View')

        toggle_categories = QAction('Show Categories', self.window)
        toggle_categories.setCheckable(True)
        toggle_categories.setChecked(True)
        toggle_categories.triggered.connect(self.workspace.category_view.setVisible)
        view_menu.addAction(toggle_categories)

        toggle_log = QAction('Show Log Console', self.window)
        toggle_log.setCheckable(True)
        toggle_log.setChecked(True)
        toggle_log.triggered.connect(self.workspace.log_console.setVisible)
        view_menu.addAction(toggle_log)

        toggle_hex = QAction('Show Hex Editor', self.window)
        toggle_hex.setCheckable(True)
        toggle_hex.setChecked(True)
        toggle_hex.triggered.connect(self.workspace.hex_editor.setVisible)

    #-------- Actions --------#
    def _handle_open(self):
        file_path, _ = QFileDialog.getOpenFileName(self.window, "Open ISO", "", "ISO Files (*.iso);;All Files (*)")
        if file_path:
            self.window.attempt_load_iso(file_path)

    def _handle_close(self):
        if self.dispatcher.active_handler:
            self.dispatcher.active_handler.close()
            self.window.stack.setCurrentIndex(0)

    def _handle_exit(self):
        if self.dispatcher.active_handler:
            self.dispatcher.active_handler.close()
        QApplication.quit()