from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal, QModelIndex
from PyQt6.QtWidgets import QMainWindow, QStackedWidget, QMessageBox, QWidget, QMenu, QVBoxLayout, QSplitter, QFileDialog, QApplication, QLabel, QPushButton, QTreeView, QListView
from PyQt6.QtGui import QAction
from pathlib import Path
from core.node import VfsNode
from core.dispatcher import Dispatcher
from core.registry import Registry
from core.resolver import ActionResolver
from core.contracts import BaseEditor
from plugins.logger import LoggingWindow
from ui.tree_model import VfsCategoryProxyModel, VfsCategoryModel, VfsTreeModel
from plugins.hex_editor import HexEditorWidget

import logging
logger = logging.getLogger(f'radiata.{__name__}')


###---------------------------------------------- Main Window ----------------------------------------###

class MainWindow(QMainWindow):
    def __init__(self, dispatcher: Dispatcher) -> None:
        super().__init__(parent=None)
        self.dispatcher = dispatcher
        # Setup View
        self.stack = QStackedWidget()
        self.welcome_page = WelcomePage()
        self.workspace_page = WorkspaceWidget()
        self._setup_ui()
        # Controllers
        self.controller = WorkspaceController(self.workspace_page, self.dispatcher)
        self.menu_manager = MainMenuBar(self, self.workspace_page, self.dispatcher)
        self._connect_main_signals()

    def _setup_ui(self) -> None:
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self.welcome_page)
        self.stack.addWidget(self.workspace_page)
        self.setWindowTitle('Raidata Modding Tool 2.0 Alpha')
        self.resize(1200,800)

    def _connect_main_signals(self) -> None:
        '''Only for main window state signals'''
        self.welcome_page.request_open.connect(self.attempt_load_iso)

    def attempt_load_iso(self, path: Path) -> None:
        result = self.dispatcher.load_source(Path(path))
        if result:
            root_node = result[0] if isinstance(result, (list, tuple)) else result
            self.controller.init_workspace(root_node)
            self.stack.setCurrentIndex(1)
            logger.info(f'Successfully loaded: {root_node.name}')
        else:
            QMessageBox.critical(self, 'Load Error', 'Failed to initialize ISO.')

###------------------------------------------ Workspace UI -------------------------------------###

class WorkspaceWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._init_views()
        self._assemble_layout()

        self.active_editor: QWidget | None = None

    def _init_views(self) -> None:
        self.category_view = QListView()

        self.category_model = VfsCategoryModel()
        self.category_model.setStringList(self.category_model.categories)
        self.category_view.setModel(self.category_model)

        self.tree_view = QTreeView()
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.hex_editor = HexEditorWidget()

        self.log_console = LoggingWindow()

    def _assemble_layout(self) -> None:
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

    def set_center_widget(self, new_widget: QWidget) -> None:
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
    '''Handles all signals and logic for the workspace'''
    def __init__(self, workspace: WorkspaceWidget, dispatcher: Dispatcher) -> None:
        self.view = workspace
        self.dispatcher = dispatcher

        self.tree_model: VfsTreeModel | None = None
        self.category_proxy_model: VfsCategoryProxyModel | None = None

    def init_workspace(self, root_node: VfsNode) -> None:
        source_model = VfsTreeModel(root_node)
        proxy_model = VfsCategoryProxyModel()
        proxy_model.setSourceModel(source_model)

        self.view.tree_view.setModel(proxy_model)

        self.tree_model = source_model
        self.category_proxy_model = proxy_model

        self.view.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        # prevent duplicate signals
        try:
            self.view.category_view.clicked.disconnect()
            self.view.tree_view.customContextMenuRequested.disconnect()
        except TypeError:
            pass

        self.view.category_view.clicked.connect(self.handle_category_select)
        self.view.tree_view.customContextMenuRequested.connect(self.handle_context_menu)

        tree_selection = self.view.tree_view.selectionModel()
        if tree_selection:
            tree_selection.currentChanged.connect(self.handle_tree_select)

        self.view.tree_view.setColumnWidth(0,300)
        self.view.tree_view.expandToDepth(0)

    def handle_category_select(self, index: QModelIndex) -> None:
        selected_category = self.view.category_model.data(index, Qt.ItemDataRole.DisplayRole)
        if self.category_proxy_model:
            self.category_proxy_model.set_category(selected_category)
        self.view.tree_view.expandAll()
        logger.info(f'Filtering by: {selected_category}')

    def handle_tree_select(self, current: QModelIndex, previous: QModelIndex) -> None:
        if not current.isValid(): 
            return
        if not self.category_proxy_model:
            return
        
        source_index = self.category_proxy_model.mapToSource(current)
        node = source_index.data(Qt.ItemDataRole.UserRole)
        if node:
            logger.debug(f'Selected: {current.data()}')
            supported_profiles = Registry.get_editor(node)
            if supported_profiles:
                self.launch_editor(node, supported_profiles)

    def handle_context_menu(self, position) -> None:
        if not self.category_proxy_model:
            return
        
        proxy_index = self.view.tree_view.indexAt(position)
        if not proxy_index.isValid(): 
            return

        source_index = self.category_proxy_model.mapToSource(proxy_index)
        node = source_index.data(Qt.ItemDataRole.UserRole)
        if not node: 
            return

        menu = QMenu(self.view)
        editor_class = Registry.get_editor(node)

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

    def launch_editor(self, node: VfsNode, editor_class: type[BaseEditor]) -> None:
        '''Instantiate new editor and create view for it'''
        new_editor = editor_class()
        raw_bytes = self.dispatcher.get_node_data(node)
        new_editor.load_node(node, raw_bytes)

        self.view.set_center_widget(new_editor)
        logger.info(f'Opened "{node.name}" in {editor_class.__name__}')

    def route_action(self, node: VfsNode, action_name: str) -> None:
        '''Route action
        Type 1: For new tree nodes
        Type 2: For node editors/properties'''
        if action_name in ('Unpack', 'Decompress'): # Type 1
            logger.debug(f'User requested new node(s) with "{action_name}" on {node.name}')
            new_nodes = self.dispatcher.load_source(node)

            if new_nodes and self.tree_model and self.category_proxy_model:
                source_index = self.tree_model.index_for_node(node)
                self.tree_model.add_children_to_node(source_index, new_nodes)

                proxy_idx = self.category_proxy_model.mapFromSource(source_index)
                self.view.tree_view.expand(proxy_idx)
        else: # Type 2
            logger.debug(f'User requested node information with "{action_name}" on {node.name}')
            self.dispatcher.execute_node_action(node, action_name)

###-------------------------------------- Welcome Page --------------------------------------###

class WelcomePage(QWidget):
    request_open = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
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

    def open_file_dialog(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, 'Open ISO', '', 'ISO Files (*.iso);;All Files (*)')
        if file_path:
            self.request_open.emit(file_path)

###------------------------------------- Menu Bar ------------------------------------------###

class MainMenuBar:
    def __init__(self, main_window: QMainWindow, workspace_page: WorkspaceWidget, dispatcher: Dispatcher) -> None:
        self.window = main_window
        self.workspace = workspace_page
        self.dispatcher = dispatcher

        self.menu_bar = self.window.menuBar()
        self._build_file_menu()
        self._build_view_menu()

    def _build_file_menu(self) -> None:
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

    def _build_view_menu(self) -> None:
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
    def _handle_open(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self.window, "Open ISO", "", "ISO Files (*.iso);;All Files (*)")
        if file_path:
            self.window.attempt_load_iso(file_path)

    def _handle_close(self) -> None:
        if self.dispatcher.active_handler:
            self.dispatcher.active_handler.close()
            self.window.stack.setCurrentIndex(0)

    def _handle_exit(self) -> None:
        if self.dispatcher.active_handler:
            self.dispatcher.active_handler.close()
        QApplication.quit()