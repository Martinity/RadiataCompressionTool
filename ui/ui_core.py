'''
Contains all the Qt window logic hierarchy looks like (only 1 of each tier is displayed at a time):
MainWindow
    WelcomePage
    WorkspacePage
        tree_view - QAbstractItemModel
        search_view - QAbstractListModel
    StagingPage
    RebuildPage
    EditorPage

MainWindow always contains log console
WorkspacePage always contains FileDescriptorPanel and SearchOverlay
'''
from __future__ import annotations

from pathlib import Path
from enum import IntEnum
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal, QModelIndex, QSettings, QObject, QTimer, QEvent
from PyQt6.QtWidgets import (
    QMainWindow, QStackedWidget, QMessageBox, QWidget, QMenu, QVBoxLayout, QSplitter, 
    QFileDialog, QApplication, QLabel, QPushButton, QTreeView, QListView, 
    QHBoxLayout, QProgressBar, QTextEdit, QHeaderView, QDialog, QTextBrowser,
    QScrollArea, QFrame, QGraphicsOpacityEffect, QAbstractItemView
)
from PyQt6.QtGui import QAction, QCloseEvent, QKeyEvent, QMouseEvent, QShortcut, QKeySequence

from core.node import VfsNode
from core.dispatcher import Dispatcher
from core.registry import Registry, GLOBAL_ACTIONS
from core.contracts import BaseEditor
from core.workers import ActionStatus, ActionResult, ActionType, ActionDef, EditorPayload
from core.descriptor_manager import NodeDescriptorStore, NodeMeta
from ui.editor_session import EditorSession
from ui.logger import LoggingWindow
from ui.tree_model import TreeProxyModel, VfsTreeModel, FlatSearchModel
from ui.theme_manager import ThemeManager
from ui.staging_page import StagingPage
from ui.settings import AppSettings
from utilities import human_size, get_resource_path

import logging
logger = logging.getLogger(f'radiata.{__name__}')

# Context menu priority by ActionType
_ACTION_TYPE_PRIORETY: dict[ActionType, int] = {
    ActionType.TREE_EXPAND: 0,
    ActionType.PROCESS:     1,
    ActionType.DIALOG:      2,
    ActionType.EXPORT:      3,
    ActionType.IMPORT:      4,
}

# Enums for page stack idx
class AppPage(IntEnum):
    WELCOME   = 0
    WORKSPACE = 1
    STAGING   = 2
    REBUILD   = 3
    EDITOR    = 4

###---------------------------------------------- Main Window ----------------------------------------###

class MainWindow(QMainWindow):
    def __init__(self, dispatcher: Dispatcher) -> None:
        super().__init__(parent=None)
        # Setup App
        self.dispatcher    = dispatcher
        self.app_settings  = AppSettings()
        self.settings      = QSettings('RadiataModding', 'Tool')
        self.current_theme = self.app_settings.theme_name
        self._zoom_delta = self.app_settings.zoom_delta
        # Setup descriptor database
        self.descriptor_store = NodeDescriptorStore(get_resource_path('ui/assets/descriptors.json'), auto_save=True, parent=self)
        self.descriptor_store.load()
        self.dispatcher.set_descriptor_store(self.descriptor_store)
        # Setup View
        self.stack          = QStackedWidget()
        self.welcome_page   = WelcomePage(self.app_settings)
        self.workspace_page = WorkspaceWidget(self.descriptor_store)
        self.staging_page   = StagingPage(self.dispatcher)
        self.rebuild_page   = RebuildStatusPage()
        self.editor_page    = EditorPage()
        self._setup_ui()

        # Controllers
        self.controller   = WorkspaceController(
            self.workspace_page, 
            self.editor_page,
            self.dispatcher, 
            self.descriptor_store,
        )
        self.menu_manager = MainMenuBar(self, self.workspace_page, self.dispatcher, self.descriptor_store, self.app_settings)
        self._setup_statusbar()
        self._connect_signals()
        self._restore_layout()

    def _setup_ui(self) -> None:
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self.welcome_page)
        self.stack.addWidget(self.workspace_page)
        self.stack.addWidget(self.staging_page)
        self.stack.addWidget(self.rebuild_page)
        self.stack.addWidget(self.editor_page)

        self.setWindowTitle('Radiata Modding Tool 2.0 Alpha')
        self.resize(1400, 900)
    
    def _setup_statusbar(self) -> None:
        self.statusBar().showMessage('Ready', 3000)

    def _connect_signals(self) -> None:
        '''Only for main window state signals'''
        self.welcome_page.request_open.connect(self.attempt_load_iso)
        self.workspace_page.btn_review.clicked.connect(lambda: self.stack.setCurrentIndex(AppPage.STAGING))
        self.staging_page.request_workspace.connect(lambda: self.stack.setCurrentIndex(AppPage.WORKSPACE))
        self.editor_page.back_requested.connect(lambda: self.stack.setCurrentIndex(AppPage.WORKSPACE))
        
        self.dispatcher.rebuild_requested.connect(self.start_rebuild)
        self.dispatcher.rebuild_progress.connect(self.rebuild_page.update_progress)
        self.dispatcher.rebuild_log.connect(self.rebuild_page.append_log)
        self.dispatcher.rebuild_complete.connect(self.on_rebuild_complete)
        self.dispatcher.iso_verified.connect(lambda build: self.statusBar().showMessage(f'Build: {build}', 0))
        self.dispatcher.io_progress.connect(lambda val, msg: self.statusBar().showMessage(msg))
        self.dispatcher.io_complete.connect(self._handle_io_completion)

        self.dispatcher.workspace_log.connect(self.workspace_page.append_log)

    ###------------------------------- Appearance ----------------------------------###
    def _restore_layout(self) -> None:
        '''Restore App State to previously used if any'''
        s = self.app_settings
        if s.geometry:
            self.restoreGeometry(s.geometry)
        if s.h_splitter:
            self.workspace_page.h_splitter.restoreState(s.h_splitter)
        if s.v_splitter:
            self.workspace_page.v_splitter.restoreState(s.v_splitter)
        self._apply_theme()
        self.workspace_page.log_console.setVisible(s.show_log_console)

    def _apply_theme(self) -> None:
        '''Apply the current_theme at current _zoom_delta'''
        ThemeManager.apply_theme(self.current_theme, self._zoom_delta)

    def adjust_zoom(self, delta: int):
        self._zoom_delta += delta
        ThemeManager.apply_theme(self.current_theme, delta)
        self.app_settings.zoom_delta = self._zoom_delta
        logger.debug(f'Zoom Adjusted (Font size set to: {ThemeManager.current_font_size})')

    def reset_zoom(self) -> None:
        DEFAULT = 0
        delta_to_default = DEFAULT - self._zoom_delta
        self.adjust_zoom(delta_to_default)
        self._zoom_delta = DEFAULT
        self.app_settings.zoom_delta = DEFAULT

    def set_theme(self, theme_name: str) -> None:
        self.current_theme = theme_name
        self._apply_theme()
        self.app_settings.theme_name = theme_name
    
    ###----------------------------------- ISO ----------------------------------###

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
        s = self.app_settings
        s.geometry = self.saveGeometry()
        s.h_splitter = self.workspace_page.h_splitter.saveState()
        s.v_splitter = self.workspace_page.v_splitter.saveState()
        s.sync()
        if self.dispatcher:
            self.dispatcher.close()
        return super().closeEvent(event)

###------------------------------------------ Workspace UI -------------------------------------###

class WorkspaceWidget(QWidget):
    def __init__(self, descriptor_store: NodeDescriptorStore, parent=None) -> None:
        super().__init__(parent)
        self.descriptor_store = descriptor_store
        self._init_views()
        self._assemble_layout()

    def _init_views(self) -> None:
        self.tree_view = QTreeView()
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.setUniformRowHeights(True)
        self.tree_view.setAnimated(False)

        self.search_results_view = QListView()
        self.search_results_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.search_results_view.setUniformItemSizes(True)
        self.sidebar_stack = QStackedWidget()
        self.sidebar_stack.addWidget(self.tree_view)
        self.sidebar_stack.addWidget(self.search_results_view)

        self.descriptor_panel = FileDescriptorPanel(self.descriptor_store)
        self.log_console = LoggingWindow()

    def _assemble_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)

        # Horizontal split:  Tree | Descriptor
        self.h_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.h_splitter.addWidget(self.sidebar_stack)
        self.h_splitter.addWidget(self.descriptor_panel)
        self.h_splitter.setStretchFactor(0, 3)
        self.h_splitter.setStretchFactor(1, 2)

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

    def update_review_bar(self, has_mods: bool, count: int) -> None:
        self.review_bar.setVisible(has_mods)
        self.status_label.setText(f'{count} file(s) modified and ready for review')

    def append_log(self, message: str) -> None:
        self.log_console.append_log(f'{message} -log_callback', 1)

###-------------------------------------------- Workspace Signals -------------------------###

class WorkspaceController(QObject):
    '''Handles all signals and logic for the workspace'''
    def __init__(
            self, 
            workspace:        WorkspaceWidget, 
            editor_page:      EditorPage, 
            dispatcher:       Dispatcher, 
            descriptor_store: NodeDescriptorStore,
    ) -> None:
        super().__init__(parent=workspace)
        self.view             = workspace
        self.editor_page      = editor_page
        self.dispatcher       = dispatcher
        self.descriptor_store = descriptor_store
        self.tree_model:  VfsTreeModel | None = None
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

        self.editor_page.save_requested.connect(self.request_save)
        self._current_session: EditorSession | None = None

    def init_workspace(self, root_node: VfsNode) -> None:
        if not self.dispatcher.vfs:
            raise TypeError('No filesystem currenlty loaded - cant initialize workspace')
        ### Tree Models
        self.tree_model  = VfsTreeModel(self.dispatcher.vfs)
        self.proxy_model = TreeProxyModel()
        self.proxy_model.setSourceModel(self.tree_model)

        ### State Memory
        main_window = self.view.window()
        if hasattr(main_window, 'app_settings'):
            self.proxy_model.set_show_hidden(main_window.app_settings.show_hidden_files)

        ### Tree View
        self.view.tree_view.setModel(self.proxy_model)
        self.view.tree_view.setSortingEnabled(True)
        self.view.tree_view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        
        ### Search / Filter
        self.search_model = FlatSearchModel(self.dispatcher.vfs, self.descriptor_store)
        self.view.search_results_view.setModel(self.search_model)
        self.view.search_results_view.clicked.connect(self._on_search_result_clicked)
        self.view.search_results_view.doubleClicked.connect(self._on_search_double_click)
        self.view.search_results_view.customContextMenuRequested.connect(self.on_search_context_menu)

        self.search_overlay = SearchOverlay(self.view.window())

        self.view.installEventFilter(self)
        self.view.tree_view.installEventFilter(self)
        self.view.search_results_view.installEventFilter(self)
        self.view.descriptor_panel.tagClicked.connect(self.on_tag_clicked)

        try:
            self.view.tree_view.customContextMenuRequested.disconnect()
        except TypeError:
            pass

        self.view.tree_view.customContextMenuRequested.connect(self.on_tree_context_menu)

        ### Tree Interactions
        tree_selection = self.view.tree_view.selectionModel()
        if tree_selection:
            tree_selection.currentChanged.connect(self.handle_tree_select)
        self.view.tree_view.doubleClicked.connect(self.handle_tree_double_click)

        ### Tree headers
        header = self.view.tree_view.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.view.tree_view.setColumnWidth(0, 150)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.view.tree_view.setColumnWidth(1, 400)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.view.tree_view.setColumnWidth(2, 85)

        self.view.tree_view.expandToDepth(1)
        self.view.update_review_bar(False, 0)
        
    def on_tracking_update(self, modified_count: int, staged_count: int) -> None:
        '''Controls the apply modifications button visibility'''
        total = modified_count + staged_count
        self.view.review_bar.setVisible(total > 0)
        self.view.status_label.setText(f'{total} modification(s) pending.')

    def _on_layout_ready(self, node: VfsNode) -> None:
        '''signaled when the layout has finished rendering the view swap'''
        if not self.tree_model or not self.proxy_model:
            return
        source_index = self.tree_model.index_for_node(node)
        if not source_index.isValid:
            return
        proxy_index = self.proxy_model.mapFromSource(source_index)
        if not proxy_index.isValid():
            logger.warning(f'Node {node.name} is not visible in current tree filter')
            return

        self.view.tree_view.expand(proxy_index)
        self.view.tree_view.scrollTo(proxy_index, QAbstractItemView.ScrollHint.PositionAtTop)
        self.view.tree_view.setCurrentIndex(proxy_index)

    ###----------------- Tree interactions-------------------###

    def handle_tree_select(self, current: QModelIndex, _previous: QModelIndex) -> None:
        '''Clicking mechanics for the tree view'''
        if not current.isValid() and not self.proxy_model: 
            return
        node: VfsNode | None = self.proxy_model.mapToSource(current).data(Qt.ItemDataRole.UserRole)
        if not node:
            return
        logger.debug(f'Selected: {node.name}')
        self.view.descriptor_panel.load_node(node)
        props_def = Registry.get_action(node, 'Properties')
        if props_def:
            self.dispatcher.execute_node_action(node, 'Properties')
        else:
            self.view.descriptor_panel.set_properties_text('-')

    def _on_search_result_clicked(self, index: QModelIndex) -> None:
        '''Clicking mechanics for the search results'''
        node = self.search_model.data(index, Qt.ItemDataRole.UserRole)
        if not isinstance(node, VfsNode) or not node:
            return
        self.view.descriptor_panel.load_node(node)
        prop_def = Registry.get_action(node, 'Properties')
        if prop_def:
            self.dispatcher.execute_node_action(node, 'Properties')
        else:
            self.view.descriptor_panel.set_properties_text('-')

    def _on_search_double_click(self, index: QModelIndex) -> None:
        if not self.search_model:
            return
        node: VfsNode | None = self.search_model.data(index, Qt.ItemDataRole.UserRole)
        if not node:
            return
        editor_classes = Registry.get_editors(node)
        if editor_classes:
            self.launch_editor(node, editor_classes[0])

    def handle_tree_double_click(self, index: QModelIndex) -> None:
        if not self.proxy_model:
            return
        node: VfsNode | None = self.proxy_model.mapToSource(index).data(Qt.ItemDataRole.UserRole)
        if not node:
            return
        editor_classes = Registry.get_editors(node)
        if editor_classes:
            self.launch_editor(node, editor_classes[0])

    def on_tag_clicked(self, tag_name: str) -> None:
        self.search_buffer = tag_name
        self.on_search_updated(tag_name)
        self.search_overlay.show_text(f'Tag: {tag_name}')

    def on_search_updated(self, query: str):
        if not query:
            self.view.sidebar_stack.setCurrentIndex(0)
            return
        self.view.sidebar_stack.setCurrentIndex(1)
        self.search_model.set_query(query)

    ###---------------------- Context Menu -----------------------###

    def on_search_context_menu(self, position) -> None:
        '''get the node for the list model and pass to _build_context_menu'''
        if not self.search_model:
            return
        index = self.view.search_results_view.indexAt(position)
        if not index.isValid():
            return
        node: VfsNode | None = self.search_model.data(index, Qt.ItemDataRole.UserRole)
        if not node:
            return
        
        self._build_context_menu(node, self.view.search_results_view.mapToGlobal(position))

    def on_tree_context_menu(self, position) -> None:
        '''get the node for the tree model and pass to _build_context_menu'''
        if not self.proxy_model:
            return
        proxy_index = self.view.tree_view.indexAt(position)
        if not proxy_index.isValid(): 
            return
        node: VfsNode | None = self.proxy_model.mapToSource(proxy_index).data(Qt.ItemDataRole.UserRole)
        if not node: 
            return
        
        self._build_context_menu(node, self.view.tree_view.viewport().mapToGlobal(position))

    def _build_context_menu(self, node: VfsNode, position) -> None:
        menu = QMenu(self.view)

        # Get Editor Classes
        editor_classes: list[type[BaseEditor]] = Registry.get_editors(node)
        for editor_class in editor_classes:
            plugin_name = getattr(editor_class, '_plugin_name', editor_class.__name__)
            open_action = menu.addAction(f'Open in {plugin_name}')
            if editor_class is editor_classes[0]:
                font = open_action.font()
                font.setBold(True)
                open_action.setFont(font)
            open_action.triggered.connect(lambda checked=False, e=editor_class, n=node: self.launch_editor(n, e))
        menu.addSeparator()

        # Get ActionDefs
        action_defs: list[ActionDef] = []
        profiles = Registry.get_handler_profiles(node)
        if profiles:
            for profile in profiles:
                action_defs.extend(profile.actions)
        if not node.is_hidden and node.size > 0:
            action_defs.extend(GLOBAL_ACTIONS)
        # Sort by ActionType priority
        action_defs.sort(key=lambda a: _ACTION_TYPE_PRIORETY.get(a.action_type, 99))

        for action_def in action_defs:
            if action_def.name == 'Properties': # Filter out properties from user
                continue
            qt_action = menu.addAction(action_def.name)
            qt_action.triggered.connect(lambda checked=False, d=action_def, n=node: self.route_action(n, d))
        
        if self.view.sidebar_stack.currentIndex() == 1: # Add go to in tree view in search view
            search_action = menu.addAction('Go to in Tree View')
            search_action.triggered.connect(lambda checked=False, n=node: self._handle_goto(n))

        menu.exec(position)

    def _handle_goto(self, node: VfsNode) -> None:
        '''Go to selected search node in tree view'''
        self.view.sidebar_stack.setCurrentIndex(0)
        QTimer.singleShot(1, lambda: self._on_layout_ready(node))


    ###------------------------- Routing ---------------------------###

    def route_action(self, node: VfsNode, action_def: ActionDef) -> None:
        '''Collect any UI required actions then pass to dispatcher.'''
        kwargs: dict = {}
        match action_def.action_type:
            case ActionType.EXPORT:
                path, _ = QFileDialog.getSaveFileName(self.view, action_def.name, '', 'All Files (*)')
                if not path:
                    return
                kwargs['file_path'] = Path(path)
            case ActionType.IMPORT:
                path, _ = QFileDialog.getOpenFileName(self.view, action_def.name, '', 'All Files (*)')
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
            logger.debug(f'No ActionDef for action "{result.action_name}"')
            return
        
        match action_def.action_type:
            case ActionType.DIALOG:
                if (result.action_name == 'Properties'):
                    if result.payload or result.message:
                        self.view.descriptor_panel.set_properties_text(str(result.payload or result.message))
                    else:
                        logger.warning('"Properties" action returned without payload...')
                else:
                    QMessageBox.information(
                        self.view, action_def.name, str(result.payload or result.message)
                    )
            case ActionType.TREE_EXPAND:
                self._on_expand_complete(result)
            case ActionType.PROCESS:
                if isinstance(result.payload, bytes) and result.payload:
                    editor_classes = Registry.get_editors(result.node)
                    if editor_classes:
                        self.launch_editor(result.node, editor_classes[0])
            case ActionType.EXPORT:
                logger.info(f'Exported: {result.message}')
            case ActionType.IMPORT:
                pass # tree is refreshed via signal

    def _on_expand_complete(self, result: ActionResult) -> None:
        if result.status != ActionStatus.SUCCESS or not self.tree_model or not self.proxy_model:
            return
        if result.action_name == 'Unpack' or hasattr(result, 'node'):
            orig_node = result.node
            source_parent_idx = self.tree_model.index_for_node(orig_node)
            proxy_parent_idx = self.proxy_model.mapFromSource(source_parent_idx)
            if proxy_parent_idx.isValid():
                self.view.tree_view.setExpanded(proxy_parent_idx, True)
                QTimer.singleShot(0, lambda: self._scroll_to(proxy_parent_idx))

    def _scroll_to(self, proxy_index: QModelIndex) -> None:
        '''Scroll to the selected proxy index'''
        if proxy_index.isValid():
            self.view.sidebar_stack.setCurrentIndex(0)
            self.view.tree_view.scrollTo(proxy_index, QTreeView.ScrollHint.PositionAtTop)
            self.view.tree_view.setCurrentIndex(proxy_index)

    ###------------------- Editor --------------------###

    def launch_editor(self, node: VfsNode, editor_class: type[BaseEditor]) -> None:
        '''Instantiate new editor and create view for it'''
        if self._current_session and not self._current_session.is_done():
            self._current_session.cancel()
        new_editor = editor_class()
        session = EditorSession(node=node, editor=new_editor)
        new_editor._session = session
        new_editor.begin_loading(node)
        self.editor_page.load_editor(session)
        self._current_session = session

        window = self.view.window()
        if isinstance(window, QMainWindow) and hasattr(window, 'stack'):
            window.stack.setCurrentIndex(AppPage.EDITOR)
        signals = self.dispatcher.open_editor(node, new_editor)
        if not signals:
            session.fail('Navigator not initialised.')
            raise ValueError('Navigator not initialized')
        signals.finished.connect(
            lambda succes, payload, s=session: self._on_editor_data_ready(s, succes, payload)
        )
        plugin_name = getattr(editor_class, '_plugin_name', editor_class.__name__)
        logger.info(f'Opening "{node.name}" in {plugin_name} [{session!r}]')

    def request_save(self) -> None:
        '''Called to save editor data'''
        if self._current_session:
            self._current_session.apply_changes(self.dispatcher.apply_edit)

    def _on_editor_data_ready(self, session: EditorSession, success: bool, payload: Any) -> None:
        '''Pass processed handler data to editor. Passes through 5 guards first.'''
        if not session.is_active(): # Session state
            logger.debug(f'{session} result discarded - state is {session.state!r}')
            session.editor.cleanup()
            return
        if session is not self._current_session: # Session currency
            logger.debug(f'{session} discarded - superseded by newer session')
            session.cancel()
            session.editor.cleanup()
            return
        if not success: # Task success
            session.fail(str(payload))
            return
        if not isinstance(payload, EditorPayload): # Payload type
            session.fail(f'Unexpected payload type: {type(payload).__name__} (expected EditorPayload)')
            return
        if payload.node is not session.node: # Node Identity
            session.fail(f'Payload node mismatch - received data for "{payload.node.name}", expected "{session.node.name}"')
            return
        
        session.complete(payload.data, self.dispatcher.get_node_data)
        logger.debug(f'{session} populated successfully.')

    ###-------------------- Search --------------------###

    def eventFilter(self, obj: QObject, event: QKeyEvent) -> bool:    
        if event.type() == QEvent.Type.KeyPress:
            key_event: QKeyEvent = event
            # Ignore keyboard events when modifiers are held
            if key_event.modifiers() & (Qt.KeyboardModifier.ControlModifier | 
                                        Qt.KeyboardModifier.AltModifier | 
                                        Qt.KeyboardModifier.MetaModifier):
                return super().eventFilter(obj, event)

            if key_event.key() == Qt.Key.Key_Escape: # Esc
                self.search_buffer = ''
                self.on_search_updated('')
                self.search_overlay.hide_overlay()
                self.search_timer.stop()
                return True
            
            if key_event.key() == Qt.Key.Key_Backspace and self.search_buffer: # Backspace
                self.search_buffer = self.search_buffer[:-1]
                if self.search_buffer == '':
                    self.on_search_updated('')
                    return True
                self.on_search_updated(self.search_buffer)
                self.search_overlay.show_text(self.search_buffer)
                self.search_timer.start()
                return True

            text = key_event.text()
            if text and text.isprintable(): # Printable
                self.search_buffer += text
                self.on_search_updated(self.search_buffer)
                self.search_overlay.show_text(self.search_buffer)
                self.search_timer.start()
                return True
        return super().eventFilter(obj, event)
    
    def clear_search_buffer(self) -> None:
        '''Clears the search buffer. To reset proxy "Esc" in eventFilter'''
        self.search_buffer = ''
        self.search_model._query = ''
        # self.on_search_updated('')
        self.search_overlay.hide_overlay()


###----------------------------------- Descriptor Panel ------------------------------------###

class FileDescriptorPanel(QWidget):
    '''Right panel of the workspace'''
    tagClicked = pyqtSignal(str)

    def __init__(self, descriptor_store: NodeDescriptorStore, parent: QWidget | None = None, controller = None) -> None:
        super().__init__(parent)
        self._store = descriptor_store
        self._current_node: VfsNode | None = None
        self._setup_ui()
        self.clear()

    def _setup_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(12, 12, 12, 12)
        self._content_layout.setSpacing(10)

        ### Header ###
        header_layout = QHBoxLayout()
        self._name_label = QLabel()
        self._name_label.setObjectName('SectionHeader')
        self._hid_label = QLabel()
        self._hid_label.setObjectName('SectionHeader')

        header_layout.addWidget(self._hid_label)
        header_layout.addWidget(self._name_label)
        header_layout.addStretch()
        self._content_layout.addLayout(header_layout)
        self._content_layout.addWidget(_divider())

        ### Tags ###
        self._tags_row = QHBoxLayout()
        self._tags_container = QWidget()
        self._tags_container.setLayout(self._tags_row)
        self._content_layout.addWidget(self._tags_container)

        ### Description ###
        desc_header = QLabel('Description')
        desc_header.setObjectName('SectionHeader')
        self._description = QLabel()
        self._description.setWordWrap(True)
        self._description.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._content_layout.addWidget(desc_header)
        self._content_layout.addWidget(self._description)
        self._content_layout.addWidget(_divider())

        ### Properties (async) ###
        self._props_header = QLabel('Properties')
        self._props_header.setObjectName('SectionHeader')
        self._props_label = QLabel('-')
        self._props_label.setWordWrap(True)
        self._props_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._props_label.setObjectName('GenericText')
        self._content_layout.addWidget(self._props_header)
        self._content_layout.addWidget(self._props_label)
        self._content_layout.addWidget(_divider())

        ### File info ###
        info_header = QLabel('File Info')
        info_header.setObjectName('SectionHeader')
        self._info_grid = QWidget()
        self._info_layout = QVBoxLayout(self._info_grid)
        self._info_layout.setContentsMargins(0, 0, 0, 0)
        self._info_layout.setSpacing(2)
        self._content_layout.addWidget(info_header)
        self._content_layout.addWidget(self._info_grid)
        self._content_layout.addStretch()

        scroll.setWidget(content)
        root_layout.addWidget(scroll)

    ### -------------------- Public ----------------------###

    def load_node(self, node: VfsNode) -> None:
        '''Populate the panel for the selected node.'''
        self._current_node = node
        hid = node.hierarchical_id_str
        meta: NodeMeta | None = self._store.get(hid)

        ### Header
        self._name_label.setText(node.name)
        self._hid_label.setText(hid)

        ### Tags
        _clear_layout(self._tags_row)
        for tag in (node.category if isinstance(node.category, tuple) else [node.category]):
            tag_clickable = ClickableTag(tag)
            tag_clickable.tagClicked.connect(self.tagClicked.emit)
            self._tags_row.addWidget(tag_clickable)
        self._tags_row.addStretch()
        self._tags_container.setVisible(bool(node.category))

        ### Description
        self._description.setText(meta.description if (meta and meta.description) else 'No description for node.')

        ### Properties
        self._props_label.setText('Loading...')
        self._props_header.setVisible(True)

        ### File info
        _clear_layout(self._info_layout)
        for label, value in [
            ('Name', node.name),
            ('Size', human_size(node.size)),
            ('Offset', hex(node.offset) if node.offset else '-'),
            ('Physical', str(node.is_physical)),
            ('Datacenter header HID', node.target)
        ]:
            row = QHBoxLayout()
            key_label = QLabel(f'{label}:')
            val_label = QLabel(str(value))
            val_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(key_label)
            row.addStretch()
            row.addWidget(val_label)
            self._info_layout.addLayout(row)
        
    def set_properties_text(self, text: str) -> None:
        '''Called by WorkspaceController when Properties action completes.'''
        self._props_label.setText(text or '-')

    def clear(self) -> None:
        self._current_node = None
        self._name_label.setText('No file selected')
        self._hid_label.setText('')
        self._description.setText('')
        self._props_label.setText('-')
        _clear_layout(self._tags_row)
        _clear_layout(self._info_layout)

    @property
    def current_node(self) -> VfsNode | None:
        return self._current_node
    
class ClickableTag(QLabel):
    tagClicked = pyqtSignal(str)

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName('DescriptorTag')
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, ev: QMouseEvent | None) -> None:
        if ev and ev.button() == Qt.MouseButton.LeftButton:
            self.tagClicked.emit(self.text())

###-------------------------------------- Welcome Page --------------------------------------###

class WelcomePage(QWidget):
    request_open = pyqtSignal(Path)

    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
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
        start_dir = self.settings.last_iso_dir or ''
        path, _ = QFileDialog.getOpenFileName(self, 'Open ISO', start_dir, 'ISO Files (*.iso);;All Files (*)')
        if path:
            self.settings.last_iso_dir = str(Path(path).parent)
            self.request_open.emit(Path(path))
            
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

###---------------------------------- Editor Page -------------------------------------------###

class EditorPage(QWidget):
    '''UX is not final. Especially for this...'''
    back_requested = pyqtSignal()
    save_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._current_session: EditorSession | None = None
        self._waiting_to_close: bool = False
        self._setup_ui()
        self._setup_shortcuts()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QWidget()
        toolbar.setObjectName('EditorToolbar')
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(10, 5, 10, 5)

        self._back_btn = QPushButton('Back')
        self._back_btn.setObjectName('FloatClearButton')
        self._back_btn.clicked.connect(self._on_back)

        self._editor_title = QLabel('Editor')
        self._editor_title.setObjectName('SectionHeader')

        self.btn_undo   = QPushButton('Undo')
        self.btn_redo   = QPushButton('Redo')
        self.btn_revert = QPushButton('Revert')
        self.btn_save   = QPushButton('Save')

        self.btn_undo.setToolTip('Ctrl+Z')
        self.btn_redo.setToolTip('Ctrl+Y')
        self.btn_revert.setToolTip('Ctrl+R')
        self.btn_save.setToolTip('Ctrl+S')

        self.btn_undo.clicked.connect(self._on_undo)
        self.btn_redo.clicked.connect(self._on_redo)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_revert.clicked.connect(self._on_revert)

        bar.addWidget(self._back_btn)
        bar.addWidget(self._editor_title)
        bar.addStretch()
        bar.addWidget(self.btn_undo)
        bar.addWidget(self.btn_redo)
        bar.addSpacing(15)
        bar.addWidget(self.btn_revert)
        bar.addWidget(self.btn_save)

        layout.addWidget(toolbar)
        self._editor_area = QStackedWidget()
        layout.addWidget(self._editor_area)

        self._set_toolbar_enabled(False)

    def _setup_shortcuts(self) -> None:
        self._back_shortcut = QShortcut(QKeySequence('Esc'), self)
        self._back_shortcut.activated.connect(self._back_btn.click)

        self.save_shortcut = QShortcut(QKeySequence('Ctrl+S'), self)
        self.save_shortcut.activated.connect(self._on_save)

        self.revert_shortcut = QShortcut(QKeySequence('Ctrl+R'), self)
        self.revert_shortcut.activated.connect(self._on_revert)

        self.undo_shortcut = QShortcut(QKeySequence('Ctrl+Z'), self)
        self.undo_shortcut.activated.connect(self._on_undo)
        
        self.redo_shortcut = QShortcut(QKeySequence('Ctrl+Y'), self)
        self.redo_shortcut.activated.connect(self._on_redo)

    def load_editor(self, session: EditorSession) -> None:
        if self._current_session:
            self._deconstruct_old_session()

        self._current_session = session
        self._editor_area.addWidget(session.editor)
        self._editor_area.setCurrentWidget(session.editor)

        is_mutable = getattr(session.editor, 'is_mutable', True)
        self.btn_save.setVisible(is_mutable)
        self.btn_revert.setVisible(is_mutable)
        has_history = hasattr(session.editor, 'undo') and hasattr(session.editor, 'redo')
        self.btn_undo.setVisible(has_history)
        self.btn_redo.setVisible(has_history)

        if is_mutable:
            session.editor.dataChanged.connect(self._on_editor_state_changed)
            session.state_changed_callback = self._on_session_state_changed
            if hasattr(session.editor, 'history'):
                session.editor.history. can_undo_changed.connect(self.btn_undo.setEnabled)
                session.editor.history.can_redo_changed.connect(self.btn_redo.setEnabled)
        
        self._update_title(is_dirty=False)
        self._set_toolbar_enabled(False)

    def _deconstruct_old_session(self) -> None:
        old_editor = self._current_session.editor if self._current_session else None
        if not old_editor:
            return
        self._editor_area.removeWidget(old_editor)
        old_editor.cleanup()
        old_editor.deleteLater()
        self._current_session = None

    def finalize_load(self) -> None:
        self._set_toolbar_enabled(True)
        if self._current_session:
            self._on_editor_state_changed(self._current_session.editor.is_dirty())

    def _on_session_state_changed(self, state: str) -> None:
        ''''''
        if not self._current_session:
            return
        self._on_editor_state_changed(self._current_session.editor.is_dirty())
        if self._waiting_to_close:
            if state == 'ready':
                self._waiting_to_close = False
                self.back_requested.emit()
            elif state == 'error':
                self._waiting_to_close = False
                QMessageBox.warning(self, 'Save Failed', 'Could not save changes. Error message in console...')

    def _on_editor_state_changed(self, is_dirty: bool) -> None:
        is_ready = bool(self._current_session and self._current_session.state == 'ready')
        self.btn_save.setEnabled(is_dirty and is_ready)
        self.btn_revert.setEnabled(is_dirty and is_ready)
        self._update_title(is_dirty)

    def _update_title(self, is_dirty: bool) -> None:
        if not self._current_session:
            self._editor_title.setText('Editor')
            return
        plugin_name = getattr(
            self._current_session.editor.__class__,
            '_plugin_name',
            self._current_session.editor.__class__.__name__
        )
        node_name = self._current_session.node.name
        asterisk = ' *' if is_dirty else ''

        self._editor_title.setText(f'{plugin_name} / {node_name}{asterisk}')

    def _set_toolbar_enabled(self, enabled: bool) -> None:
        self.btn_save.setEnabled(enabled)
        self.btn_revert.setEnabled(enabled)
        self.btn_undo.setEnabled(enabled)
        self.btn_redo.setEnabled(enabled)

    ###--------------------------------------- Triggers ----------------------------------###

    def _on_back(self) -> None:
        if not self._current_session:
            self.back_requested.emit()
            return
        session = self._current_session
        if session.state == 'loading':
            reply = QMessageBox.question(
                self, 'Loading in Progress', 'Data is still loading. Cancel and go back?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                session.cancel()
                session.editor.cleanup()
                self.back_requested.emit()
            return
        if session.state == ('ready', 'error'):
            editor = session.editor
            if editor.is_mutable and editor.is_dirty():
                reply = QMessageBox.question(
                    self, 'Unsaved Changes', 'Apply changes before closing?',
                    QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
                    QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Save,
                )
                if reply == QMessageBox.StandardButton.Cancel:
                    return
                if reply == QMessageBox.StandardButton.Save:
                    self._waiting_to_close = True
                    self.save_requested.emit()
                    return
                else:
                    editor.discard_changes()
        self.back_requested.emit()

    # def _on_apply_confirmed(self, dirty: bool) -> None:
    #     '''Insure that the editor instance gets the data during the closing state'''
    #     if not dirty:
    #         if self._current_session:
    #             try:
    #                 self._current_session.editor.dataChanged.disconnect(self._on_apply_confirmed)
    #             except TypeError:
    #                 pass
    #         self.back_requested.emit()
    
    def _on_save(self) -> None:
        if not self._current_session:
            return
        editor = self._current_session.editor if self._current_session else None
        if editor and editor.is_mutable and editor.is_dirty():
            logger.info(f'Staging modifications for {self._current_session.node.name}...')
            self.save_requested.emit()

    def _on_revert(self) -> None:
        if self._current_session and self._current_session.editor.is_mutable:
            self._current_session.editor.discard_changes()

    def _on_undo(self) -> None:
        if self._current_session and hasattr(self._current_session.editor, 'undo'):
            self._current_session.editor.undo()

    def _on_redo(self) -> None:
        if self._current_session and hasattr(self._current_session.editor, 'redo'):
            self._current_session.editor.redo()

###------------------------------------- Menu Bar ------------------------------------------###

class MainMenuBar:
    def __init__(
            self, 
            main_window:      QMainWindow, 
            workspace_page:   WorkspaceWidget, 
            dispatcher:       Dispatcher,
            descriptor_store: NodeDescriptorStore,
            app_settings:     AppSettings 
        ) -> None:
        self.window     = main_window
        self.workspace  = workspace_page
        self.dispatcher = dispatcher
        self._store     = descriptor_store
        self.settings   = app_settings

        self.menu_bar = self.window.menuBar()
        self._build_file_menu()
        self._build_view_menu()
        self._build_descriptor_menu()
        self._build_info_menu()

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
        # Theme
        theme_menu = view_menu.addMenu('Theme')
        self._theme_actions: dict[str, QAction] = {}
        for name in ThemeManager.THEMES.keys():
            action = QAction(name, self.window)
            action.setCheckable(True)
            action.setChecked(name == self.settings.theme_name)
            action.triggered.connect(lambda checked, n=name: self._handle_theme_change(n))
            theme_menu.addAction(action)
            self._theme_actions[name] = action
        # Zoom
        view_menu.addSeparator()
        zoom_in = QAction('Zoom In', self.window)
        zoom_out = QAction('Zoom out', self.window)
        zoom_rst = QAction('Reset Zoom', self.window)
        zoom_in.setShortcut('Ctrl+=')
        zoom_out.setShortcut('Ctrl+-')
        zoom_rst.setShortcut('Ctrl+0')
        zoom_in.triggered.connect(lambda: self.window.adjust_zoom(+1))
        zoom_out.triggered.connect(lambda: self.window.adjust_zoom(-1))
        zoom_rst.triggered.connect(lambda: self.window.reset_zoom())
        for act in (zoom_in, zoom_out, zoom_rst):
            view_menu.addAction(act)

        # Toggles
        toggle_log = QAction('Show Log Console', self.window)
        toggle_log.setCheckable(True)
        toggle_log.setChecked(self.settings.show_log_console)
        toggle_log.triggered.connect(self._handle_toggle_log)
        view_menu.addAction(toggle_log)

        toggle_hidden = QAction('Show Hidden Files', self.window)
        toggle_hidden.setCheckable(True)
        toggle_hidden.setChecked(self.settings.show_hidden_files)
        toggle_hidden.triggered.connect(self._handle_toggle_hidden)
        view_menu.addAction(toggle_hidden)

    def _build_descriptor_menu(self) -> None:
        descriptor_menu = self.menu_bar.addMenu('Descriptors')

        build_action = QAction('Export new JSON', self.window)
        build_action.triggered.connect(self._handle_export_template)
        descriptor_menu.addAction(build_action)

        save_action = QAction('Save Now', self.window)
        save_action.triggered.connect(self._store.save)
        descriptor_menu.addAction(save_action)

    def _build_info_menu(self) -> None:
        info_menu = self.menu_bar.addMenu('Info')

        legend_action = QAction('File Legend', self.window)
        legend_action.triggered.connect(self._handle_legend)
        info_menu.addAction(legend_action)

    #-------- Actions --------#
    def _handle_open(self) -> None:
        start_dir = self.settings.last_iso_dir or ''
        path, _ = QFileDialog.getOpenFileName(self.window, 'Open ISO', start_dir, 'ISO Files (*.iso);;All Files (*)')
        if path:
            self.settings.last_iso_dir = str(Path(path).parent)
            self.window.attempt_load_iso(Path(path))

    def _handle_close(self) -> None:
        self.dispatcher.close()
        self.window.stack.setCurrentIndex(AppPage.WELCOME)

    def _handle_exit(self) -> None:
        QApplication.quit()

    def _handle_theme_change(self, theme_name: str) -> None:
        for name, action in self._theme_actions.items():
            action.setChecked(name == theme_name)
        self.window.set_theme(theme_name)

    def _handle_toggle_log(self, checked: bool) -> None:
        self.workspace.log_console.setVisible(checked)
        self.settings.show_log_console = checked

    def _handle_toggle_hidden(self, checked: bool) -> None:
        '''Pass the toggle signal to the proxy model'''
        self.settings.show_hidden_files = checked
        if self.window.controller.proxy_model: # Prevent crashing when no proxy_model is live
            self.window.controller.proxy_model.set_show_hidden(checked)

    def _handle_export_template(self) -> None:
        if not self.dispatcher.vfs:
            return
        path, _ = QFileDialog.getSaveFileName(self.window, 'Export Descriptor JSON', 'descriptors.json', 'JSON Files (*.json)')
        if not path:
            return
        output = Path(path)
        count = self._store.export_template(self.dispatcher.vfs, output)
        QMessageBox.information(self.window, 'Template Exported', f'{count} new stub(s) added.\nSave to {output.name}')

    def _handle_legend(self) -> None:
        LegendViewer(self.window).exec()

###------------------------------------------------- Search Overlay ---------------------------------------------------------###

class SearchOverlay(QLabel):
    '''Floating centered text overlay that fades when idle for searching'''
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName('SearchOverlay')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity_effect)
        self.opacity = 0.0
        self.opacity_effect.setOpacity(self.opacity)

        self.fade_timer = QTimer(self)
        self.fade_timer.setInterval(50)
        self.fade_timer.timeout.connect(self._fade_step)

        self.idle_timer = QTimer(self)
        self.idle_timer.setInterval(300)
        self.idle_timer.setSingleShot(True)
        self.idle_timer.timeout.connect(self.fade_timer.start)

        self.hide()

    def show_text(self, text: str) -> None:
        if not text:
            self.hide_overlay()
            return
        
        self.setText(text)
        self.opacity = .80
        self.opacity_effect.setOpacity(self.opacity)
        self.adjustSize()

        if self.parentWidget():
            geo = self.parentWidget().geometry()
            self.move(
                (geo.width() - self.width()) // 2,
                (geo.height() - self.height()) //2
            )
        self.show()
        self.fade_timer.stop()
        self.idle_timer.start()

    def _fade_step(self) -> None:
        self.opacity -= 0.05
        if self.opacity <= 0:
            self.opacity = 0
            self.fade_timer.stop()
            self.hide()
        self.opacity_effect.setOpacity(self.opacity)

    def hide_overlay(self) -> None:
        self.opacity = 0
        self.opacity_effect.setOpacity(0)
        self.fade_timer.stop()
        self.idle_timer.stop()
        self.hide()

###------------------------------------------- File Legend ------------------------------------------###

class LegendViewer(QDialog):
    '''Creates a paging dialog for known file magics and their supposed use as well as current support'''
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.pages = [Legend1, Legend2]
        self.page = 0
        self.setWindowTitle('File Legend for Radiata Stories')
        layout = QVBoxLayout(self)
        self.resize(500, 750)
        self.browser = QTextBrowser()
        self.browser.setHtml(self.pages[0])
        self.next_btn = QPushButton('Next')
        self.next_btn.setObjectName('FloatClearButton')
        self.next_btn.clicked.connect(self.next_page)
        layout.addWidget(self.browser)
        layout.addWidget(self.next_btn)

    def next_page(self) -> None:
        self.page += 1

        if self.page >= len(self.pages):
            self.accept()
            return
        self.browser.setHtml(self.pages[self.page])
        if self.page == len(self.pages) - 1:
            self.next_btn.setText('Close')

###------------------------------------------- Utility ------------------------------------------###

def _divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line

def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        if item.layout():
            _clear_layout(item.layout())

Legend1 = '''
<html><body>
<h3>File Legend (1/2)</h3>
<table border="1" cellspacing="0" cellpadding="4">    <tr>
        <th>Extension</th>
        <th>Description</th>
        <th>Support</th>
    </tr>

    <tr><th colspan="3">File System</th></tr>
    <tr><td>.slz</td><td>Compressed file</td><td>100%</td></tr>
    <tr><td>.sle</td><td>Encrypted compressed file</td><td>100%</td></tr>
    <tr><td>.kods</td><td>Custom archive</td><td>100%</td></tr>
    <tr><td>.bcb</td><td>Packed entity data</td><td>100%</td></tr>
    <tr><td>.vib</td><td>Vibration motor data</td><td>0%</td></tr>
    <tr><td>.elf</td><td>Executables</td><td>---</td></tr>
    <tr><td>.idx</td><td>TOC</td><td>100%</td></tr>

    <tr><th colspan="3">Audio</th></tr>
    <tr><td>.seqw</td><td>Sound data</td><td>0%</td></tr>
    <tr><td>.VAG</td><td>PS2 standard audio format</td><td>0%</td></tr>
    <tr><td>.020</td><td>TAC audio</td><td>Viewer / Export</td></tr>

    <tr><th colspan="3">Movie</th></tr>
    <tr><td>.fmv</td><td>Movies</td><td>0%</td></tr>

    <tr><th colspan="3">Mesh</th></tr>
    <tr><td>.fps</td><td>Mesh data</td><td></td></tr>
    <tr><td>.fss</td><td>Mesh data</td><td></td></tr>
    <tr><td>.idom</td><td>Mesh data</td><td></td></tr>
    <tr><td>.lctp</td><td>Mesh data</td><td></td></tr>

    <tr><th colspan="3">Event</th></tr>
    <tr><td>.evd</td><td>Event VM dispatcher data</td><td>0%</td></tr>
</table>
</body></html>
'''
Legend2 = '''
<html><body>
<h3>Supported Formats (2/2)</h3>
<table border="1" cellspacing="0" cellpadding="4">
    <tr><th colspan="3">Animation</th></tr>
    <tr><td>.fas</td><td>Animation data</td><td></td></tr>
    <tr><td>.hfas</td><td>Animation data</td><td></td></tr>
    <tr><td>.rmac</td><td>Animation data</td><td></td></tr>
    <tr><td>.rta</td><td>Animation data</td><td></td></tr>
    <tr><td>.paf</td><td>Animation data</td><td></td></tr>

    <tr><th colspan="3">Texture</th></tr>
    <tr><td>.fis</td><td>Texture data</td><td></td></tr>
    <tr><td>.fisp</td><td>Texture data</td><td></td></tr>
    <tr><td>.fisa</td><td>Texture data</td><td></td></tr>
    <tr><td>.tim2</td><td>PS2 standard texture format</td><td>0%</td></tr>

    <tr><th colspan="3">Scene</th></tr>
    <tr><td>.rbad</td><td>Radiata Background Animation Data</td><td>0%</td></tr>
    <tr><td>.rlf</td><td>Scene data</td><td></td></tr>
    <tr><td>.rmf</td><td>Scene data</td><td></td></tr>
    <tr><td>.ndnc</td><td>Scene data</td><td></td></tr>
    <tr><td>.xbdc</td><td>Scene data</td><td></td></tr>
    <tr><td>.pcdc</td><td>Scene data</td><td></td></tr>
    <tr><td>.dnal</td><td>Scene data</td><td></td></tr>
    <tr><td>.tgil</td><td>Container for map animation data</td><td></td></tr>

    <tr><th colspan="3">Gameplay</th></tr>
    <tr><td>.mpa</td><td>Sprite animation data</td><td></td></tr>
    <tr><td>.dth</td><td>Gameplay data</td><td></td></tr>
    <tr><td>.cpa</td><td>Gameplay data</td><td></td></tr>
    <tr><td>.ipa</td><td>Gameplay data</td><td></td></tr>
    <tr><td>.fdc</td><td>Gameplay data</td><td></td></tr>

    <tr><th colspan="3">Unknown / Descriptor</th></tr>
    <tr><td>.rcp</td><td>Unknown table of grouped IDs</td><td>0%</td></tr>
    <tr><td>.rcad</td><td>Descriptor data</td><td></td></tr>
    <tr><td>.png</td><td>PNG image</td><td></td></tr>
</table>
</body></html>
'''