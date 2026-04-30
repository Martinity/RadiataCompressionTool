from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal, QModelIndex, QSettings
from PyQt6.QtWidgets import QMainWindow, QStackedWidget, QMessageBox, QWidget, QMenu, QVBoxLayout, QSplitter, QFileDialog, QApplication, QLabel, QPushButton, QTreeView, QListView, QListWidget, QHBoxLayout, QListWidgetItem
from PyQt6.QtWidgets import QProgressBar, QTextEdit
from PyQt6.QtGui import QAction, QCloseEvent
from pathlib import Path
from core.node import VfsNode, ModTracker
from core.dispatcher import Dispatcher
from core.registry import Registry
from core.resolver import ActionResolver
from core.contracts import BaseEditor
from plugins.logger import LoggingWindow
from ui.tree_model import VfsCategoryProxyModel, VfsCategoryModel, VfsTreeModel
from ui.style_sheets import DARK_STYLESHEET
from plugins.hex_editor import HexEditorWidget

import logging
logger = logging.getLogger(f'radiata.{__name__}')

# DARK_STYLESHEET = """
#     QMainWindow, QWidget {
#         background-color: #1e1e1e;
#         color: #d4d4d4;
#     }
#     QTreeView, QListView, QTextEdit, QListWidget {
#         background-color: #252526;
#         border: 1px solid #3c3c3c;
#         selection-background-color: #094771;
#     }
#     QPushButton {
#         background-color: #2c3e50;
#         color: white;
#         border: none;
#         padding: 6px 12px;
#         border-radius: 4px;
#     }
#     QPushButton:hover { background-color: #34495e; }
#     QPushButton:pressed { background-color: #1f2a38; }
#     QSplitter::handle {
#         background-color: #3c3c3c;
#     }
#     QLabel { color: #d4d4d4; }
# """

###---------------------------------------------- Main Window ----------------------------------------###

class MainWindow(QMainWindow):
    def __init__(self, dispatcher: Dispatcher) -> None:
        super().__init__(parent=None)
        self.dispatcher = dispatcher
        self.settings = QSettings('RadiataModding', 'Tool')

        # Setup View # TODO definde how the tracker is passed
        self.stack = QStackedWidget()
        self.welcome_page = WelcomePage()
        self.workspace_page = WorkspaceWidget()
        self.staging_page = StagingPage(self.dispatcher.tracker)
        self.rebuild_page = RebuildStatusPage()
        self._setup_ui()

        # Controllers
        self.controller = WorkspaceController(self.workspace_page, self.dispatcher, self.dispatcher.tracker)
        self.menu_manager = MainMenuBar(self, self.workspace_page, self.dispatcher)
        self._setup_statusbar()
        self._connect_signals()
        self._restore_layout()

    def _setup_ui(self) -> None:
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self.welcome_page)
        self.stack.addWidget(self.workspace_page)
        self.stack.addWidget(self.staging_page)
        self.stack.addWidget(self.rebuild_page)
        self.setStyleSheet(DARK_STYLESHEET)
        self.setWindowTitle('Radiata Modding Tool 2.0 Alpha')
        self.resize(1400, 900)
    
    def _setup_statusbar(self) -> None:
        self.statusBar().showMessage('Ready', 3000)

    def _connect_signals(self) -> None:
        '''Only for main window state signals'''
        self.welcome_page.request_open.connect(self.attempt_load_iso)
        self.workspace_page.btn_review.clicked.connect(lambda: self.stack.setCurrentWidget(self.staging_page))
        
        self.dispatcher.rebuild_requested.connect(self.start_rebuild)

        self.dispatcher.rebuild_progress.connect(self.rebuild_page.update_progress)
        self.dispatcher.rebuild_log.connect(self.rebuild_page.append_log)
        self.dispatcher.rebuild_complete.connect(self.on_rebuild_complete)

    def _restore_layout(self) -> None:
        '''Restore window geometry'''
        geometry = self.settings.value('geometry')
        if geometry:
            self.restoreGeometry(geometry)
    
    def attempt_load_iso(self, path: Path) -> None:
        self.statusBar().showMessage(f'Loading {Path(path).name}')
        result = self.dispatcher.load_source(Path(path))
        if result:
            root_node = result[0] if isinstance(result, (list, tuple)) else result
            self.controller.init_workspace(root_node)
            self.stack.setCurrentIndex(1)
            self.statusBar().showMessage('ISO loaded successfully', 5000)
            logger.info(f'Successfully loaded: {root_node.name}')
        else:
            QMessageBox.critical(self, 'Load Error', 'Failed to initialize ISO.')
            self.statusBar().showMessage('Load failed', 5000)

    def show_staging_page(self) -> None:
        self.stack.setCurrentWidget(self.staging_page)

    def show_rebuild_page(self) -> None:
        self.stack.setCurrentWidget(self.rebuild_page)

    def start_rebuild(self, staged_nodes: list[VfsNode]) -> None:
        '''Transitions UI and asks for save location before kicking off background thread'''
        # 1. Ask user where to save the new ISO (Never overwrite the original!)
        file_path, _ = QFileDialog.getSaveFileName(self, "Save Modified ISO", "", "ISO Files (*.iso)")
        
        if not file_path:
            # User canceled the save dialog, stay on staging page
            return 
            
        # 2. Reset and switch to the status page
        self.stack.setCurrentWidget(self.rebuild_page)
        self.rebuild_page.log_output.clear()
        self.rebuild_page.progress_bar.setValue(0)
        self.statusBar().showMessage('Rebuilding ISO...', 0)

        # 3. Tell the dispatcher to start the background thread
        self.dispatcher.start_iso_rebuild(Path(file_path))

    def on_rebuild_complete(self, success: bool, message: str) -> None:
        '''Handles the end of the background thread'''
        if success:
            self.statusBar().showMessage('Rebuild Complete!', 5000)
            QMessageBox.information(self, 'Success', message)
            self.stack.setCurrentWidget(self.workspace_page)
        else:
            self.statusBar().showMessage('Rebuild Failed', 5000)
            QMessageBox.critical(self, 'Build Failed', message)
            
    def closeEvent(self, event: QCloseEvent | None) -> None:
        self.settings.setValue('geometry', self.saveGeometry())
        return super().closeEvent(event)

###------------------------------------------ Workspace UI -------------------------------------###

class WorkspaceWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._init_views()
        self._assemble_layout()
        self.active_editor: QWidget | None = None

    def _init_views(self) -> None:
        self.category_view = QListView()
        self.category_view.setAlternatingRowColors(True)

        self.category_model = VfsCategoryModel()
        self.category_model.setStringList(self.category_model.categories)
        self.category_view.setModel(self.category_model)

        self.tree_view = QTreeView()
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.setAlternatingRowColors(True)
        self.tree_view.setUniformRowHeights(True)

        self.hex_editor = HexEditorWidget()

        self.log_console = LoggingWindow()

    def _assemble_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)

        # Horizontal split: Categories | Tree | Editor
        self.h_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.h_splitter.addWidget(self.category_view)
        self.h_splitter.addWidget(self.tree_view)
        self.h_splitter.addWidget(self.hex_editor)
        self.h_splitter.setSizes([180, 700, 650])
        self.h_splitter.setStretchFactor(1, 2)

        # Vertical split: Main area | Log
        self.v_splitter = QSplitter(Qt.Orientation.Vertical)
        self.v_splitter.addWidget(self.h_splitter)
        self.v_splitter.addWidget(self.log_console)
        self.v_splitter.setSizes([650,250])
        self.v_splitter.setStretchFactor(0, 1)

        layout.addWidget(self.v_splitter)

        # Review bar
        self.review_bar = QWidget()
        self.review_bar.setObjectName('ReviewBar')
        bar_layout = QHBoxLayout(self.review_bar)
        bar_layout.setContentsMargins(12,8,12,8)

        self.status_label = QLabel('No pending ISO modifications')
        self.btn_review = QPushButton('Review & Rebuild ISO')
        self.btn_review.setFixedHeight(32)

        bar_layout.addWidget(self.status_label)
        bar_layout.addStretch()
        bar_layout.addWidget(self.btn_review)

        layout.addWidget(self.review_bar)
        self.review_bar.setVisible(False)

    def set_center_widget(self, new_widget: QWidget) -> None:
        old_widget = self.h_splitter.widget(2)

        if old_widget and old_widget is not new_widget:
            self.h_splitter.replaceWidget(2, new_widget)
            old_widget.deleteLater()
        else:
            self.h_splitter.insertWidget(2, new_widget)
        self.active_editor = new_widget
        new_widget.show()

    def update_review_bar(self, has_mods: bool, count: int) -> None:
        self.review_bar.setVisible(has_mods)
        self.status_label.setText(f'{count} file(s) modified and ready for review')

###-------------------------------------------- Workspace Signals -------------------------###

class WorkspaceController:
    '''Handles all signals and logic for the workspace'''
    def __init__(self, workspace: WorkspaceWidget, dispatcher: Dispatcher, tracker: ModTracker) -> None:
        self.view = workspace
        self.dispatcher = dispatcher
        self.tracker = tracker

        # Connect tracker state
        self.dispatcher.tracking_update.connect(self.on_tracking_update)

    def init_workspace(self, root_node: VfsNode) -> None:
        source_model = VfsTreeModel(self.dispatcher.vfs)
        self.category_proxy_model = VfsCategoryProxyModel()
        self.category_proxy_model.setSourceModel(source_model)

        self.view.tree_view.setModel(self.category_proxy_model)

        self.tree_model = source_model

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

        self.view.tree_view.setColumnWidth(0,350)
        self.view.tree_view.expandToDepth(1)

        self.view.update_review_bar(False, 0)
        
    def on_tracking_update(self, modified_count: int, staged_count: int):
        total = modified_count + staged_count
        self.view.review_bar.setVisible(total > 0)
        self.view.status_label.setText(f'{total} modification(s) pending.')

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

        if hasattr(new_editor, 'apply_requested'):
            new_editor.apply_requested.connect(self.dispatcher.apply_edit)

        self.view.set_center_widget(new_editor)
        logger.info(f'Opened "{node.name}" in {editor_class.__name__}')

    def route_action(self, node: VfsNode, action_name: str) -> None:
        '''Route action to the dispatcher'''
        logger.debug(f'User requested new node(s) with "{action_name}" on {node.name} (Datacenter={getattr(node, 'target', None)})')
        if action_name in ('Unpack', 'Decompress'): # Type 1: produce new nodes
            new_nodes = self.dispatcher.load_source(node)

            if new_nodes and self.tree_model and self.category_proxy_model:
                source_index = self.tree_model.index_for_node(node)
                proxy_idx = self.category_proxy_model.mapFromSource(source_index)
                self.view.tree_view.expand(proxy_idx)
        else: # Type 2: info / editor actions
            self.dispatcher.execute_node_action(node, action_name)

    def update_review_bar_visibility(self) -> None:
        '''Toggle the bar if there are modified nodes'''
        has_modifications = (len(self.tracker.modified_nodes) > 0 or 
                             len(self.tracker.rebuild_queue) > 0)
        self.view.review_bar.setVisible(has_modifications)

        count = len(self.tracker.modified_nodes) + len(self.tracker.rebuild_queue)
        self.view.status_label.setText(f'{count} file(s) modified in current session')

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

###------------------------------------- Staging Page --------------------------------------###

class StagingPage(QWidget):
    '''UI for managing the filesystem vs Staging Area'''
    def __init__(self, mod_track: ModTracker, parent=None) -> None:
        super().__init__(parent)
        self.tracker = mod_track
        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self) -> None:
        main_layout = QVBoxLayout(self)

        lists_layout = QHBoxLayout()

        # left side
        unstaged_layout = QVBoxLayout()
        unstaged_layout.addWidget(QLabel('<b>Unstage Changes</b>'))
        self.unstaged_list = QListWidget()
        unstaged_layout.addWidget(self.unstaged_list)

        # middle acitons
        button_layout = QVBoxLayout()
        button_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.btn_stage = QPushButton('Stage >>')
        self.btn_unstage = QPushButton('<< Unstage')
        self.btn_revert = QPushButton('Revert Change')
        button_layout.addWidget(self.btn_stage)
        button_layout.addWidget(self.btn_unstage)
        button_layout.addStretch()
        button_layout.addWidget(self.btn_revert)

        # right side
        staged_layout = QVBoxLayout()
        staged_layout.addWidget(QLabel('<b>Staged Changes (Ready to Commit)</b>'))
        self.staged_list = QListWidget()
        staged_layout.addWidget(self.staged_list)

        # assemble
        lists_layout.addLayout(unstaged_layout)
        lists_layout.addLayout(button_layout)
        lists_layout.addLayout(staged_layout)

        # confirm button
        confirm_layout = QHBoxLayout()
        confirm_layout.addStretch()
        self.btn_confirm = QPushButton('Confirm Changes & Rebuild')
        self.btn_confirm.setFixedSize(250, 40)
        self.btn_confirm.setStyleSheet('font-weight: bold; background-color: #27ae60; color: white;')
        confirm_layout.addWidget(self.btn_confirm)

        main_layout.addLayout(lists_layout)
        main_layout.addLayout(confirm_layout)
    
    def _connect_signals(self) -> None:
        self.btn_stage.clicked.connect(self._on_stage)
        self.btn_unstage.clicked.connect(self._on_unstage)
        self.btn_revert.clicked.connect(self._on_revert)


        self.tracker.state_changed.connect(self.refresh_lists)
        self.btn_confirm.clicked.connect(self.tracker.confirm_and_rebuild)
        
    def refresh_lists(self) -> None:
        '''Modifies the list of modified nodes'''
        self.unstaged_list.clear()
        self.staged_list.clear()

        for node in self.tracker.modified_nodes:
            item = QListWidgetItem(f'{node.name} (ID: {node.hierarchical_id_str})')
            item.setData(Qt.ItemDataRole.UserRole, node)
            self.unstaged_list.addItem(item)

        for node in self.tracker.rebuild_queue:
            item = QListWidgetItem(f'{node.name} (ID: {node.hierarchical_id_str})')
            item.setData(Qt.ItemDataRole.UserRole, node)
            self.staged_list.addItem(item)

        self.btn_confirm.setEnabled(len(self.tracker.rebuild_queue) > 0)

    def _on_stage(self) -> None:
        for item in self.unstaged_list.selectedItems():
            node = item.data(Qt.ItemDataRole.UserRole)
            self.tracker.stage_node(node)

    def _on_unstage(self) -> None:
        for item in self.staged_list.selectedItems():
            node = item.data(Qt.ItemDataRole.UserRole)
            self.tracker.unstage_node(node)

    def _on_revert(self) -> None:
        selected_items = self.unstaged_list.selectedItems() + self.staged_list.selectedItems()
        for item in selected_items:
            node = item.data(Qt.ItemDataRole.UserRole)
            self.tracker.revert_node(node)

###------------------------------------- Rebuilding Page -----------------------------------###

class RebuildStatusPage(QWidget):
    '''Displays logs and progress during the ISO rebuild process.'''
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(50, 50, 50, 50)
        
        self.header = QLabel("<h2>Rebuilding ISO...</h2>")
        self.header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.header)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4; font-family: monospace;")
        layout.addWidget(self.log_output)
        
    def append_log(self, message: str) -> None:
        self.log_output.append(message)
        
    def update_progress(self, percentage: int) -> None:
        self.progress_bar.setValue(percentage)

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
        self.dispatcher.close()
        self.window.stack.setCurrentIndex(0)

    def _handle_exit(self) -> None:
        if self.dispatcher.active_handler:
            self.dispatcher.active_handler.close()
        QApplication.quit()