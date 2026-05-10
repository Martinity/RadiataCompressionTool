from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal, QModelIndex, QSettings, QObject, QTimer, QEvent
from PyQt6.QtWidgets import (
    QMainWindow, QStackedWidget, QMessageBox, QWidget, QMenu, QVBoxLayout, QSplitter, 
    QFileDialog, QApplication, QLabel, QPushButton, QTreeView, QListView, QListWidget, 
    QHBoxLayout, QListWidgetItem, QProgressBar, QTextEdit, QHeaderView
)
from PyQt6.QtGui import QAction, QCloseEvent, QKeyEvent, QShortcut, QKeySequence
from pathlib import Path
from core.node import VfsNode, ModTracker
from core.dispatcher import Dispatcher
from core.registry import Registry, GLOBAL_ACTIONS
from core.contracts import BaseEditor
from core.workers import ActionStatus, ActionResult, ActionType, ActionDef
from plugins.logger import LoggingWindow
from ui.tree_model import TreeProxyModel, VfsCategoryModel, VfsTreeModel
from ui.style_sheet import DarkTheme, ThemeManager
from plugins.hex_editor import HexEditorWidget

import logging
logger = logging.getLogger(f'radiata.{__name__}')

_ACTION_TYPE_PRIORETY: dict[ActionType, int] = {
    ActionType.TREE_EXPAND: 0,
    ActionType.PROCESS:     1,
    ActionType.DIALOG:      2,
    ActionType.EXPORT:      3,
    ActionType.IMPORT:      4,
}

###---------------------------------------------- Main Window ----------------------------------------###

class MainWindow(QMainWindow):
    def __init__(self, dispatcher: Dispatcher) -> None:
        super().__init__(parent=None)
        self.dispatcher    = dispatcher
        self.settings      = QSettings('RadiataModding', 'Tool')
        self.current_theme = DarkTheme
        self._setup_zoom_shortcuts()

        # Setup View
        self.stack          = QStackedWidget()
        self.welcome_page   = WelcomePage()
        self.workspace_page = WorkspaceWidget()
        self.staging_page   = StagingPage(self.dispatcher.tracker)
        self.rebuild_page   = RebuildStatusPage()
        self._setup_ui()

        # Controllers
        self.controller   = WorkspaceController(self.workspace_page, self.dispatcher, self.dispatcher.tracker)
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
        self.adjust_zoom(0) # Initialize the style sheet via font, probably be a scuffy way to do this
        self.setWindowTitle('Radiata Modding Tool 2.0 Alpha')
        self.resize(1400, 900)
    
    def _setup_statusbar(self) -> None:
        self.statusBar().showMessage('Ready', 3000)

    def _setup_zoom_shortcuts(self):
        QShortcut(QKeySequence('Ctrl+='), self).activated.connect(lambda: self.adjust_zoom(1))
        QShortcut(QKeySequence('Ctrl++'), self).activated.connect(lambda: self.adjust_zoom(1))
        QShortcut(QKeySequence('Ctrl+-'), self).activated.connect(lambda: self.adjust_zoom(-1))

    def _connect_signals(self) -> None:
        '''Only for main window state signals'''
        self.welcome_page.request_open.connect(self.attempt_load_iso)
        self.workspace_page.btn_review.clicked.connect(lambda: self.stack.setCurrentWidget(self.staging_page))
        self.staging_page.request_workspace.connect(lambda: self.stack.setCurrentIndex(1))
        
        self.dispatcher.rebuild_requested.connect(self.start_rebuild)
        self.dispatcher.rebuild_progress.connect(self.rebuild_page.update_progress)
        self.dispatcher.rebuild_log.connect(self.rebuild_page.append_log)
        self.dispatcher.rebuild_complete.connect(self.on_rebuild_complete)
        self.dispatcher.iso_verified.connect(lambda build: self.statusBar().showMessage(f'Build: {build}', 0))
        self.dispatcher.io_progress.connect(lambda val, msg: self.statusBar().showMessage(msg))
        self.dispatcher.io_complete.connect(self._handle_io_completion)

    def _restore_layout(self) -> None:
        '''Restore window geometry'''
        geometry = self.settings.value('geometry')
        if geometry:
            self.restoreGeometry(geometry)
        if h_state := self.settings.value('h_splitter'):
            self.workspace_page.h_splitter.restoreState(h_state)
        if v_state := self.settings.value('v_splitter'):
            self.workspace_page.v_splitter.restoreState(v_state)

    def adjust_zoom(self, delta: int):
        new_css = ThemeManager.get_theme_with_zoom(self.current_theme, delta)
        self.setStyleSheet(new_css)
        if app := QApplication.instance():
            app.setStyleSheet(new_css)
        logger.debug(f'Zoom Adjusted (Font size set to: {ThemeManager.current_font_size})')
    
    def attempt_load_iso(self, path: Path) -> None:
        self.statusBar().showMessage(f'Loading {path.name}')
        result = self.dispatcher.load_source(path)
        if result:
            root_node = result[0] if isinstance(result, (list, tuple)) else result
            self.controller.init_workspace(root_node)
            self.stack.setCurrentIndex(1)
            self.statusBar().showMessage('ISO loaded successfully', 5000)
            logger.info(f'Successfully loaded: {root_node.name}')
        else:
            QMessageBox.critical(self, 'Load Error', 'Failed to initialize ISO.')
            self.statusBar().showMessage('Load failed', 5000)

    def start_rebuild(self, staged_nodes: list[VfsNode]) -> None:
        '''Transitions UI and asks for save location before kicking off background thread'''
        file_path, _ = QFileDialog.getSaveFileName(self, "Save Modified ISO", "", "ISO Files (*.iso)")
        
        if not file_path: # User canceled the save dialog, stay on staging page
            return 
            
        self.stack.setCurrentWidget(self.rebuild_page)
        self.rebuild_page.log_output.clear()
        self.rebuild_page.progress_bar.setValue(0)
        self.statusBar().showMessage('Rebuilding ISO...', 0)
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

    def _handle_io_completion(self, success: bool, msg: str):
        if success:
            self.statusBar().showMessage(msg, 5000)
        else:
            QMessageBox.warning(self, 'Task Error', msg)

    def closeEvent(self, event: QCloseEvent | None) -> None:
        self.settings.setValue('geometry', self.saveGeometry())
        self.settings.setValue('h_splitter', self.workspace_page.h_splitter.saveState())
        self.settings.setValue('v_splitter', self.workspace_page.v_splitter.saveState())
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
        self.category_model = VfsCategoryModel()
        self.category_model.setStringList(self.category_model.categories)
        self.category_view.setModel(self.category_model)

        self.tree_view = QTreeView()
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.setUniformRowHeights(True)
        self.tree_view.setAnimated(False)

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
        self.h_splitter.setStretchFactor(0, 1)
        self.h_splitter.setStretchFactor(1, 3)
        self.h_splitter.setStretchFactor(2, 3)

        # Vertical split: Main area | Log
        self.v_splitter = QSplitter(Qt.Orientation.Vertical)
        self.v_splitter.addWidget(self.h_splitter)
        self.v_splitter.addWidget(self.log_console)
        self.v_splitter.setStretchFactor(0, 4)
        self.v_splitter.setStretchFactor(1, 1)

        layout.addWidget(self.v_splitter)

        # Review bar
        self.review_bar = QWidget()
        self.review_bar.setObjectName('ReviewBar')
        bar_layout = QHBoxLayout(self.review_bar)
        bar_layout.setContentsMargins(12,8,12,8)

        self.status_label = QLabel('No pending ISO modifications')
        self.btn_review = QPushButton('Review & Rebuild ISO')
        bar_layout.addWidget(self.status_label)
        bar_layout.addStretch()
        bar_layout.addWidget(self.btn_review)

        layout.addWidget(self.review_bar)
        self.review_bar.setVisible(False)

    def set_center_widget(self, new_widget: QWidget) -> None:
        '''Currently used to change the right widget, aka hex editor...'''
        old_widget = self.h_splitter.widget(2)
        if old_widget is new_widget:
            return
        self.h_splitter.replaceWidget(2, new_widget)
        if old_widget:
            old_widget.deleteLater()
        self.active_editor = new_widget

    def update_review_bar(self, has_mods: bool, count: int) -> None:
        self.review_bar.setVisible(has_mods)
        self.status_label.setText(f'{count} file(s) modified and ready for review')

###-------------------------------------------- Workspace Signals -------------------------###

class WorkspaceController(QObject):
    '''Handles all signals and logic for the workspace'''
    def __init__(self, workspace: WorkspaceWidget, dispatcher: Dispatcher, tracker: ModTracker) -> None:
        super().__init__(parent=workspace)
        self.view       = workspace
        self.dispatcher = dispatcher
        self.tracker    = tracker
        self.tree_model: VfsTreeModel | None = None
        self.proxy_model: TreeProxyModel | None = None

        # Setup Invisible Search
        self.search_buffer = ''
        self.search_timer  = QTimer(self)
        self.search_timer.setInterval(1500)
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self.clear_search_buffer)

        # Connect tracker state
        self.dispatcher.tracking_update.connect(self.on_tracking_update)
        self.dispatcher.action_complete.connect(self.handle_action_result)


    def init_workspace(self, root_node: VfsNode) -> None:
        self.tree_model  = VfsTreeModel(self.dispatcher.vfs)
        self.proxy_model = TreeProxyModel()
        self.proxy_model.setSourceModel(self.tree_model)

        self.view.tree_view.setModel(self.proxy_model)
        self.view.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.tree_view.setSortingEnabled(True)
        self.view.tree_view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.tree_view.installEventFilter(self)

        # prevent duplicate signals, by disconnecting old connections
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

        header = self.view.tree_view.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.view.tree_view.setColumnWidth(0, 150)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.view.tree_view.setColumnWidth(1, 400)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.view.tree_view.setColumnWidth(2, 85)

        self.view.tree_view.expandToDepth(1)
        self.view.tree_view.setUniformRowHeights(True)
        self.view.update_review_bar(False, 0)
        
    def on_tracking_update(self, modified_count: int, staged_count: int):
        total = modified_count + staged_count
        self.view.review_bar.setVisible(total > 0)
        self.view.status_label.setText(f'{total} modification(s) pending.')

    ###----------------- Tree interactions-------------------###

    def handle_category_select(self, index: QModelIndex) -> None:
        selected_category = self.view.category_model.data(index, Qt.ItemDataRole.DisplayRole)
        if self.proxy_model:
            self.proxy_model.set_category(selected_category)
        self.view.tree_view.expandAll()

    def handle_tree_select(self, current: QModelIndex, previous: QModelIndex) -> None:
        if not current.isValid() and not self.proxy_model: 
            return
        node = self.proxy_model.mapToSource(current).data(Qt.ItemDataRole.UserRole)
        if node:
            logger.debug(f'Selected: {current.data()}')
            editor_class = Registry.get_editor(node)
            if editor_class:
                self.launch_editor(node, editor_class)

    ###---------------------- Context Menu -----------------------###

    def handle_context_menu(self, position) -> None:
        if not self.proxy_model:
            return
        proxy_index = self.view.tree_view.indexAt(position)
        if not proxy_index.isValid(): 
            return
        node = self.proxy_model.mapToSource(proxy_index).data(Qt.ItemDataRole.UserRole)
        if not node: 
            return
        menu = QMenu(self.view)
        editor_class = Registry.get_editor(node)
        if editor_class:
            open_action = menu.addAction(f'Open with {editor_class.__name__}')
            font = open_action.font()
            font.setBold(True)
            open_action.setFont(font)
            open_action.triggered.connect(lambda checked=False, e=editor_class, n=node: self.launch_editor(n, e))
        menu.addSeparator()

        # Get action defs
        action_defs: list[ActionDef] = []
        profile = Registry.get_profile(node)
        if profile:
            action_defs.extend(profile.actions.values())
        if not node.is_hidden and node.size > 0:
            action_defs.extend(GLOBAL_ACTIONS.values())
        # Sort by ActionType priority
        action_defs.sort(key=lambda a: _ACTION_TYPE_PRIORETY.get(a.action_type, 99))

        for action_def in action_defs:
            qt_action = menu.addAction(action_def.title)
            qt_action.triggered.connect(lambda checked=False, d=action_def, n=node: self.route_action(n, d))
        
        menu.exec(self.view.tree_view.viewport().mapToGlobal(position))

    ###------------------------- Routing ---------------------------###

    def route_action(self, node: VfsNode, action_def: ActionDef) -> None:
        '''Collect any UI required actions then pass to dispatcher.'''
        kwargs: dict = {}
        match action_def.action_type:
            case ActionType.EXPORT:
                path, _ = QFileDialog.getSaveFileName(self.view, action_def.title, '', 'All Files (*)')
                if not path:
                    return
                kwargs['file_path'] = Path(path)
            case ActionType.IMPORT:
                path, _ = QFileDialog.getOpenFileName(self.view, action_def.title, '', 'All Files (*)')
                if not path:
                    return
                kwargs['file_path'] = Path(path)
            case ActionType.TREE_EXPAND | ActionType.PROCESS | ActionType.DIALOG:
                pass
            case _:
                logger.warning(f'Unhandled ActionType {action_def.action_type} for {action_def.name}')
        self.dispatcher.execute_node_action(node, action_def.name, **kwargs)

    ###----------------------- Post Action ------------------------###

    def handle_action_result(self, result: ActionResult) -> None:
        '''Respond to a completed action.'''
        if result.status == ActionStatus.FAILURE:
            logger.warning(f'{result.action_name} failed: {result.message}')
            return
        
        action_def = Registry.get_action(result.node, result.action_name)
        if not action_def:
            logger.debug(f'No ActionDef for completed action "{result.action_name}"')
            return
        
        match action_def.action_type:
            case ActionType.DIALOG:
                QMessageBox.information(
                    self.view, action_def.title, str(result.payload or result.message)
                )
            case ActionType.TREE_EXPAND:
                if self.tree_model and self.proxy_model: # expand to see new children
                    source_index = self.tree_model.index_for_node(result.node)
                    if source_index.isValid():
                        proxy_idx = self.proxy_model.mapFromSource(source_index)
                        self.view.tree_view.expand(proxy_idx)
            case ActionType.PROCESS:
                if isinstance(result.payload, bytes) and result.payload:
                    editor_class = Registry.get_editor(result.node)
                    if editor_class:
                        self.launch_editor(result.node, editor_class)
            case ActionType.EXPORT:
                logger.info(f'Exported: {result.message}')
            case ActionType.IMPORT:
                # tree is refreshed via signal
                pass

    ###------------------- Editor --------------------###

    def launch_editor(self, node: VfsNode, editor_class: type[BaseEditor]) -> None:
        '''Instantiate new editor and create view for it'''
        new_editor = editor_class()
        raw_bytes = self.dispatcher.get_node_data(node)
        new_editor.load_node(node, raw_bytes)
        if hasattr(new_editor, 'apply_requested'):
            new_editor.apply_requested.connect(self.dispatcher.apply_edit)
        self.view.set_center_widget(new_editor)
        logger.info(f'Opened "{node.name}" in {editor_class.__name__}')

    ###-------------------- Search --------------------###

    def eventFilter(self, obj: QObject, event: QKeyEvent) -> bool:
        if obj is self.view.tree_view and event.type() == QEvent.Type.KeyPress:
            key_event: QKeyEvent = event
            if key_event.key() == Qt.Key.Key_Escape and self.proxy_model:
                self.search_buffer = ''
                self.proxy_model.set_search_query('')
                self.search_timer.stop()
                return True
            text = key_event.text()
            if text and text.isprintable() and self.proxy_model:
                self.search_buffer += text
                self.proxy_model.set_search_query(self.search_buffer)
                self.view.tree_view.expandAll()
                self.search_timer.start()
                return True
        return super().eventFilter(obj, event)
    
    def clear_search_buffer(self) -> None:
        '''Clears the search buffer but not the category proxy model. To reset proxy "Esc" in eventFilter'''
        self.search_buffer = ''

###-------------------------------------- Welcome Page --------------------------------------###

class WelcomePage(QWidget):
    request_open = pyqtSignal(Path)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(50,50,50,50)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        subtitle = QLabel('Select a Radiata Stories ISO...')
        subtitle.setObjectName('WelcomeSubtitle')

        self.button = QPushButton('Open ISO', self)
        self.button.setObjectName('WelcomeButton')
        self.button.clicked.connect(self.open_file_dialog)

        layout.addWidget(subtitle)
        layout.addWidget(self.button)

    def open_file_dialog(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, 'Open ISO', '', 'ISO Files (*.iso);;All Files (*)')
        if file_path:
            self.request_open.emit(Path(file_path))

###------------------------------------- Staging Page --------------------------------------###

class StagingPage(QWidget):
    '''UI for managing the filesystem vs Staging Area'''
    request_workspace = pyqtSignal()

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
        unstage_label = QLabel('Unstage Changes')
        unstage_label.setObjectName('SectionHeader')
        unstaged_layout.addWidget(unstage_label)
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
        staged_label = QLabel('Staged Changes (Ready to Commit)')
        staged_label.setObjectName('SectionHeader')
        staged_layout.addWidget(staged_label)
        self.staged_list = QListWidget()
        staged_layout.addWidget(self.staged_list)

        # assemble
        lists_layout.addLayout(unstaged_layout)
        lists_layout.addLayout(button_layout)
        lists_layout.addLayout(staged_layout)

        # bottom button
        bottom_layout = QHBoxLayout()
        self.btn_back = QPushButton('Back')
        self.btn_back.setObjectName('FloatClearButton')
        bottom_layout.addWidget(self.btn_back)
        bottom_layout.addStretch()
        self.btn_confirm = QPushButton('Build New ISO')
        self.btn_confirm.setObjectName('ConfirmButton')
        bottom_layout.addWidget(self.btn_confirm)

        main_layout.addLayout(lists_layout)
        main_layout.addLayout(bottom_layout)
    
    def _connect_signals(self) -> None:
        self.btn_back.clicked.connect(self.request_workspace.emit)
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
            self.tracker.stage_node(item.data(Qt.ItemDataRole.UserRole))

    def _on_unstage(self) -> None:
        for item in self.staged_list.selectedItems():
            self.tracker.unstage_node(item.data(Qt.ItemDataRole.UserRole))

    def _on_revert(self) -> None:
        selected_items = self.unstaged_list.selectedItems() + self.staged_list.selectedItems()
        for item in selected_items:
            self.tracker.revert_node(item.data(Qt.ItemDataRole.UserRole))

###------------------------------------- Rebuilding Page -----------------------------------###

class RebuildStatusPage(QWidget):
    '''Displays logs and progress during the ISO rebuild process.'''
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(50, 50, 50, 50)
        
        self.header = QLabel('Rebuilding ISO...')
        self.header.setObjectName('PageTitle')
        self.header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.header)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setObjectName('LogOutput')
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

        exit_action = QAction('Exit', self.window)
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
        view_menu.addAction(toggle_hex)

        toggle_hidden = QAction('Show Hidden Files', self.window)
        toggle_hidden.setCheckable(True)
        toggle_hidden.setChecked(False)
        toggle_hidden.triggered.connect(self._handle_toggle_hidden)
        view_menu.addAction(toggle_hidden)

    #-------- Actions --------#
    def _handle_open(self) -> None:
        if hasattr(self.window, 'welcome_page'):
            self.window.welcome_page.open_file_dialog()
        else: # fallback 
            logger.warning('No welcome_page exists for MainWindow, falling back...')
            file_path, _ = QFileDialog.getOpenFileName(self.window, "Open ISO", "", "ISO Files (*.iso);;All Files (*)")
            if file_path:
                self.window.attempt_load_iso(Path(file_path))

    def _handle_close(self) -> None:
        self.dispatcher.close()
        self.window.stack.setCurrentIndex(0)

    def _handle_exit(self) -> None:
        if self.dispatcher.active_handler:
            self.dispatcher.active_handler.close()
        QApplication.quit()

    def _handle_toggle_hidden(self, checked: bool) -> None:
        '''Pass the toggle signal to the proxy model'''
        if self.window.controller.proxy_model:
            self.window.controller.proxy_model.set_show_hidden(checked)
