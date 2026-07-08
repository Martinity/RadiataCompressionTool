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
WorkspacePage always contains FileMetadataPanel and SearchOverlay
'''
from __future__ import annotations

from pathlib import Path
from enum import IntEnum
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal, QModelIndex, QSettings, QObject, QTimer, QEvent
from PyQt6.QtWidgets import (
    QMainWindow, QStackedWidget, QMessageBox, QWidget, QMenu, QVBoxLayout, QSplitter, 
    QFileDialog, QApplication, QLabel, QPushButton, QTreeView, QListView, QSizePolicy,
    QHBoxLayout, QProgressBar, QTextEdit, QHeaderView, QDialog, QStatusBar,
    QScrollArea, QFrame, QAbstractItemView, QLineEdit, QMenuBar
)
from PyQt6.QtGui import QAction, QCloseEvent, QKeyEvent, QMouseEvent, QStandardItem, QStandardItemModel, QColor
from PyQt6 import sip

from core.node import VfsNode
from core.dispatcher import Dispatcher
from core.registry import Registry, GLOBAL_ACTIONS
from core.contracts import BaseEditor
from core.workers import ActionStatus, ActionResult, ActionType, ActionDef, EditorPayload, TaskHandle
from core.metadata_manager import NodeMetadataStore
from core.version import __version__
from ui.logger import LoggingWindow
from ui.tree_model import TreeProxyModel, VfsTreeModel, FlatSearchModel
from ui.theme_manager import ThemeManager
from ui.staging_page import StagingPage
from ui.editor_page import EditorPage, EditorSession
from ui.settings import AppSettings
from utilities import human_size, get_resource_path, hline

import functools
import threading
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
        self._main_thread_id = threading.get_ident()
        # Setup App
        self.dispatcher    = dispatcher
        self.app_settings  = AppSettings()
        self.settings      = QSettings('RadiataModding', 'Tool')
        self.current_theme = self.app_settings.theme_name
        self._zoom_delta = self.app_settings.zoom_delta
        # Setup metadata database
        self.metadata_store = NodeMetadataStore(get_resource_path('ui/assets/radi_metadata.json'), auto_save=True, parent=self)
        self.metadata_store.load()
        self.dispatcher.set_metadata_store(self.metadata_store)
        # Setup View
        self.stack          = QStackedWidget()
        self.welcome_page   = WelcomePage(self.app_settings)
        self.workspace_page = WorkspaceWidget(self.metadata_store)
        self.staging_page   = StagingPage(self.dispatcher)
        self.rebuild_page   = RebuildStatusPage()
        self.editor_page    = EditorPage()
        self._setup_ui()

        # Controllers
        self.controller   = WorkspaceController(
            self.workspace_page, 
            self.editor_page,
            self.dispatcher, 
            self.metadata_store,
        )
        self.menu_manager = MainMenuBar(self, self.workspace_page, self.dispatcher, self.metadata_store, self.app_settings)
        self._setup_statusbar()
        self._connect_signals()
        self._restore_layout()
        # Start Thread Pool
        self.dispatcher.task_coordinator.start_task(lambda **kwargs: None)

    def _setup_ui(self) -> None:
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self.welcome_page)
        self.stack.addWidget(self.workspace_page)
        self.stack.addWidget(self.staging_page)
        self.stack.addWidget(self.rebuild_page)
        self.stack.addWidget(self.editor_page)

        self.setWindowTitle(f'Radiata Modding Tool 2.0 Alpha {__version__}')
        self.resize(1400, 900)

    @property
    def status_bar(self) -> QStatusBar:
        bar = self.statusBar()
        assert bar is not None
        return bar

    def _setup_statusbar(self) -> None:
        self.status_bar.showMessage('Ready', 3000)

    def _on_worker_log(self, msg: str) -> None:
        '''Bound slot for worker log_message signals — always runs on main thread.'''
        if threading.get_ident() != self._main_thread_id:
            logger.error("_on_worker_log ran off the main thread")
        self.status_bar.showMessage(msg, 0)

    def _connect_signals(self) -> None:
        '''Only for main window state signals'''
        self.welcome_page.request_open.connect(self.attempt_load_iso)
        self.workspace_page.btn_review.clicked.connect(lambda: self.stack.setCurrentIndex(AppPage.STAGING))
        self.staging_page.request_workspace.connect(lambda: self.stack.setCurrentIndex(AppPage.WORKSPACE))
        self.editor_page.back_requested.connect(lambda: self.stack.setCurrentIndex(AppPage.WORKSPACE))

        self.dispatcher.iso_loaded.connect(self._on_iso_loaded)
        self.dispatcher.rebuild_requested.connect(self.start_rebuild)
        self.dispatcher.rebuild_progress.connect(self.rebuild_page.update_progress)
        self.dispatcher.rebuild_log.connect(self.rebuild_page.append_log)
        self.dispatcher.rebuild_complete.connect(self.on_rebuild_complete)
        self.dispatcher.iso_verified.connect(lambda build: self.status_bar.showMessage(f'Build: {build}', 0))
        self.dispatcher.io_progress.connect(lambda val, msg: self.status_bar.showMessage(msg))
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
        logging.getLogger('radiata').setLevel(logging.DEBUG if self.app_settings.verbose_logging else logging.INFO)

    def _apply_theme(self) -> None:
        '''Apply the current_theme without changing the zoom'''
        ThemeManager.apply_theme(self.current_theme, self._zoom_delta)

    def adjust_zoom(self, delta: int):
        self._zoom_delta += delta
        ThemeManager.apply_theme(self.current_theme, self._zoom_delta)
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
        self.app_settings.last_iso_dir = str(path.parent)
        self.status_bar.showMessage(f'Loading {path.name}...')
        self.welcome_page.set_loading(True)
        task_handle = self.dispatcher.load_source(path)
        if not isinstance(task_handle, TaskHandle):
            QMessageBox.critical(self, 'Load Error', f'No handler for {path.name}')
            self.welcome_page.set_loading(False)
            return
        task_handle.log_message.connect(self._on_worker_log)

    def start_rebuild(self, staged_nodes: list[VfsNode]) -> None:
        '''Transitions UI and asks for save location before kicking off background thread'''
        file_path, _ = QFileDialog.getSaveFileName(self, "Save Modified ISO", "", "ISO Files (*.iso)")
        
        if not file_path: # User canceled the save dialog, stay on staging page
            return 
            
        self.stack.setCurrentWidget(self.rebuild_page)
        self.rebuild_page.log_output.clear()
        self.rebuild_page.progress_bar.setValue(0)
        handle = self.dispatcher.start_iso_rebuild(Path(file_path))
        if handle:
            self.rebuild_page.set_task_handle(handle)

    def _on_iso_loaded(self, nodes: list | None) -> None:
        self.welcome_page.set_loading(False)
        if nodes is None:
            QMessageBox.critical(self, 'Load Error', 'Invalid ISO.')
            return
        root_node = nodes[0]
        self.controller.init_workspace(root_node)
        self.stack.setCurrentIndex(AppPage.WORKSPACE)
        self.workspace_page.setFocus()
        self.status_bar.showMessage('ISO loaded - verifying build...')
        has_iso = bool(nodes)
        self.menu_manager.open_action.setEnabled(not has_iso)
        self.menu_manager.close_action.setEnabled(has_iso)

    def on_rebuild_complete(self, success: bool, message: str) -> None:
        '''Handles the end of the background thread'''
        self.rebuild_page.on_rebuild_finished()
        if success:
            QMessageBox.information(self, 'Success', message)
        else:
            QMessageBox.critical(self, 'Build Failed', message)
        self.stack.setCurrentWidget(self.workspace_page)

    def _handle_io_completion(self, success: bool, msg: str):
        if success:
            self.status_bar.showMessage(msg, 5000)
        else:
            QMessageBox.warning(self, 'Task Error', msg)

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        s = self.app_settings
        s.geometry = self.saveGeometry()
        s.h_splitter = self.workspace_page.h_splitter.saveState()
        s.v_splitter = self.workspace_page.v_splitter.saveState()
        s.sync()
        self.dispatcher.close()
        return super().closeEvent(a0)

###------------------------------------------ Workspace UI -------------------------------------###

class WorkspaceWidget(QWidget):
    def __init__(self, metadata_store: NodeMetadataStore, parent=None) -> None:
        super().__init__(parent)
        self.metadata_store = metadata_store
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

        self.metadata_panel = FileMetadataPanel(self.metadata_store)
        self.log_console = LoggingWindow()

    def _assemble_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)

        # Horizontal split:  Tree | Metadata
        self.h_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.h_splitter.addWidget(self.sidebar_stack)
        self.h_splitter.addWidget(self.metadata_panel)
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
        self.btn_review = QPushButton('Review Rebuild Queue')
        bar_layout.addWidget(self.status_label)
        bar_layout.addStretch()
        bar_layout.addWidget(self.btn_review)

        layout.addWidget(self.review_bar)
        self.review_bar.setVisible(False)

    def update_review_bar(self, has_mods: bool, count: int) -> None:
        self.review_bar.setVisible(has_mods)
        self.status_label.setText(f'{count} file(s) modified and ready for review')

    def append_log(self, message: str) -> None:
        self.log_console.append_log(f'{message}', 1)

###-------------------------------------------- Workspace Signals -------------------------###

class WorkspaceController(QObject):
    '''Handles all signals and logic for the workspace'''
    def __init__(
            self, 
            workspace:        WorkspaceWidget, 
            editor_page:      EditorPage, 
            dispatcher:       Dispatcher, 
            metadata_store: NodeMetadataStore,
    ) -> None:
        super().__init__(parent=workspace)
        self._main_thread_id = threading.get_ident()
        self.view             = workspace
        self.editor_page      = editor_page
        self.dispatcher       = dispatcher
        self.metadata_store   = metadata_store
        self.tree_model:     VfsTreeModel | None = None
        self.proxy_model:  TreeProxyModel | None = None
        self._last_selected_node: VfsNode | None = None

        # Setup Invisible Search
        self.search_buffer = ''
        self.search_timer  = QTimer(self)
        self.search_timer.setInterval(1500)
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self.clear_search_buffer)

        # Connect tracker state
        self.dispatcher.tracking_update.connect(self.on_tracking_update)
        self.dispatcher.action_complete.connect(self.handle_action_result)

        self._current_session: EditorSession | None = None

        self.view.metadata_panel.metadata_changed.connect(self._on_metadata_changed)

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
        self.search_model = FlatSearchModel(self.dispatcher.vfs, self.metadata_store)
        self.view.search_results_view.setModel(self.search_model)
        self.view.search_results_view.clicked.connect(self._on_search_result_clicked)
        self.view.search_results_view.doubleClicked.connect(self._on_search_double_click)
        self.view.search_results_view.customContextMenuRequested.connect(self.on_search_context_menu)

        self.search_overlay = SearchOverlay(self.view.window())

        self.view.installEventFilter(self)
        self.view.tree_view.installEventFilter(self)
        self.view.search_results_view.installEventFilter(self)
        self.view.metadata_panel.tagClicked.connect(self.on_tag_clicked)

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
        if header is not None:
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

    def _on_metadata_changed(self, node: VfsNode, title: str, description: str, tags: tuple[str, ...]) -> None:
        '''Update the metadata store and refresh the panel'''
        hids_str = node.hierarchical_id_str
        node.name = title
        node.category = tags
        self.metadata_store.register(
            hid=hids_str,
            title=title,
            description=description,
            tags=list(tags)
        )
        self.view.metadata_panel.load_node(node, title, description, tags)

    ###----------------- Tree interactions-------------------###

    def handle_tree_select(self, current: QModelIndex, _previous: QModelIndex) -> None:
        '''Clicking mechanics for the tree view'''
        if not current.isValid() or self.proxy_model is None: 
            return
        node: VfsNode | None = self.proxy_model.mapToSource(current).data(Qt.ItemDataRole.UserRole)
        if not node:
            return
        self._last_selected_node = node

        meta   = self.metadata_store.get(node.hierarchical_id_str)
        title = meta.title if meta and meta.title else node.name
        desc  = meta.description if meta else ''
        tags  = meta.tags if meta and meta.tags else node.category
        self.view.metadata_panel.load_node(node, title, desc, tags)
        props_def = Registry.get_action(node, 'Properties')
        if props_def:
            self.dispatcher.execute_node_action(node, 'Properties')
        else:
            self.view.metadata_panel.set_properties_text('-')

    def handle_tree_double_click(self, index: QModelIndex) -> None:
        if not self.proxy_model:
            return
        node: VfsNode | None = self.proxy_model.mapToSource(index).data(Qt.ItemDataRole.UserRole)
        if not node:
            return
        editor_classes = Registry.get_editors(node)
        if editor_classes:
            self.launch_editor(node, editor_classes[0])

    def _on_search_result_clicked(self, index: QModelIndex) -> None:
        '''Clicking mechanics for the search results'''
        entry = self.search_model.data(index, Qt.ItemDataRole.UserRole)
        if entry.node:
            self._handle_goto(entry.node)
        else:
            hid = tuple(map(int, entry.hid_str.split('.')))
            self.dispatcher.resolve_ghost_node(hid, self._handle_goto)

    def _on_search_double_click(self, index: QModelIndex) -> None:
        if not self.dispatcher.nav or not self.dispatcher.vfs:
            return
        entry = self.search_model.data(index, Qt.ItemDataRole.UserRole)
        node = entry.node
        if node is None:
            hid = tuple(map(int, entry.hid_str.split('.')))
            ancestor = self.dispatcher.vfs.find_nearest_ancestor(hid)
            if ancestor is not None:
                node = self.dispatcher.vfs.get_node_by_id(hid)
        if node is None:
            logger.warning(f'No node found for id:({entry.hid_str})')
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
            if self._last_selected_node:
                QTimer.singleShot(1, lambda: self._on_layout_ready(self._last_selected_node))
            return
        self.view.sidebar_stack.setCurrentIndex(1)
        self.search_model.set_query(query)

    ###---------------------- Context Menu -----------------------###

    def on_search_context_menu(self, position) -> None:
        '''get the node for the list model and pass to _build_context_menu'''
        if not self.search_model or not self.dispatcher.nav or not self.dispatcher.vfs:
            return
        index = self.view.search_results_view.indexAt(position)
        if not index.isValid():
            return
        entry = self.search_model.data(index, Qt.ItemDataRole.UserRole)
        node = entry.node
        hid = tuple(map(int, entry.hid_str.split('.')))
        if not node and self.dispatcher.vfs:
            self.dispatcher.resolve_ghost_node(hid, self._handle_goto)
            node = self.dispatcher.vfs.get_node_by_id(hid)
        if not node:
            logger.error(f'No node found for ({hid})')
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
        viewport = self.view.tree_view.viewport()
        if viewport:
            self._build_context_menu(node, viewport.mapToGlobal(position))

    def _build_context_menu(self, node: VfsNode, position) -> None:
        menu = QMenu(self.view)

        # Get Editor Classes
        editor_classes: list[type[BaseEditor]] = Registry.get_editors(node)
        for editor_class in editor_classes:
            plugin_name = getattr(editor_class, '_plugin_name', editor_class.__name__)
            open_action = menu.addAction(f'Open in {plugin_name}')
            if open_action is None:
                continue
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
            if qt_action is None:
                continue
            qt_action.triggered.connect(lambda checked=False, d=action_def, n=node: self.route_action(n, d))
        
        if self.view.sidebar_stack.currentIndex() == 1: # Add go to in tree view in search view
            search_action = menu.addAction('Go to in Tree View')
            if search_action is not None:
                search_action.triggered.connect(lambda checked=False, n=node: self._handle_goto(n))

        menu.exec(position)

    def _handle_goto(self, node: VfsNode) -> None:
        '''Go to selected search node in tree view'''
        self._last_selected_node = node
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
                        self.view.metadata_panel.set_properties_text(str(result.payload or result.message))
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
                QMessageBox.information(self.view, 'Export Action Result', result.message)
                logger.info(f'Exported: {result.message}')
            case ActionType.IMPORT:
                QMessageBox.information(self.view, 'Import Action Result', result.message)
                # tree is refreshed via signal

    def _on_expand_complete(self, result: ActionResult) -> None:
        if result.status != ActionStatus.SUCCESS or not self.tree_model or not self.proxy_model:
            return
        if result.action_name == 'Unpack' or hasattr(result, 'node'):
            orig_node = result.node
            source_parent_idx = self.tree_model.index_for_node(orig_node)
            proxy_parent_idx = self.proxy_model.mapFromSource(source_parent_idx)
            if proxy_parent_idx.isValid():
                self.view.tree_view.setExpanded(proxy_parent_idx, True)
                if self.view.sidebar_stack.currentIndex() == 1: # For search model actions scroll to
                    QTimer.singleShot(0, lambda: self._scroll_to(proxy_parent_idx))

    def _scroll_to(self, proxy_index: QModelIndex) -> None:
        '''Scroll to the selected proxy index'''
        if proxy_index.isValid():
            self.view.sidebar_stack.setCurrentIndex(0)
            self.view.tree_view.scrollTo(proxy_index, QTreeView.ScrollHint.PositionAtTop)
            self.view.tree_view.setCurrentIndex(proxy_index)

    ###------------------- Editor --------------------###

    def launch_editor(self, node: VfsNode, editor_class: type[BaseEditor]) -> None:
        '''Instantiate new editor and create view for it.
        Create the close session callback and task handle.'''
        if self._current_session and not self._current_session.is_done():
            self._current_session.cancel()
        new_editor = editor_class()
        # Build the dispatch funtion that will close confirm/reject callbacks
        def dispatch_fn(node: VfsNode, data: Any) -> None:
            self.dispatcher.apply_edit(
                node, data, new_editor,
                on_success=session.confirm_save,
                on_failure=session.reject_save,
            )
        session = EditorSession(node=node, editor=new_editor, dispatch_callback=dispatch_fn)
        self._current_session = session
        new_editor.begin_loading(node)
        self.editor_page.load_editor(session)

        window = self.view.window()
        if isinstance(window, QMainWindow) and hasattr(window, 'stack'):
            window.stack.setCurrentIndex(AppPage.EDITOR)
        task_handle = self.dispatcher.open_editor(node, new_editor)
        if not task_handle:
            session.fail('Navigato not initialised.')
            return
        session.set_active_task(task_handle)
        task_handle.finished.connect(functools.partial(self._on_editor_data_ready, session))

        plugin_name = getattr(editor_class, '_plugin_name', editor_class.__name__)
        logger.info(f'Opening "{node.name}" in {plugin_name}')

    def _on_editor_data_ready(self, session: EditorSession, success: bool, payload: Any) -> None:
        '''Pass processed handler data to editor. Passes through 5 guards first.'''
        if threading.get_ident() != self._main_thread_id:
            logger.error("_on_editor_data_ready ran off the main thread")
        if not session.is_active(): # Session state
            logger.debug(f'{session} result discarded - state is {session.state!r}')
            if sip.isdeleted(session.editor):
                return
            session.editor.cleanup()
            return
        if session is not self._current_session: # Session currency
            logger.debug(f'{session} discarded - superseded by newer session')
            session.cancel()
            if sip.isdeleted(session.editor):
                return
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
            if text and text.isprintable(): # Ensure Printable
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


###----------------------------------- Metadata Panel ------------------------------------###

class FileMetadataPanel(QWidget):
    '''Right panel of the workspace'''
    metadata_changed = pyqtSignal(object, str, str, tuple)  # (node, title, description, tags)
    tagClicked       = pyqtSignal(str)                      # tag str

    def __init__(self, metadata_store: NodeMetadataStore, parent: QWidget | None = None, controller = None) -> None:
        super().__init__(parent)
        self._store = metadata_store
        self._current_node: VfsNode | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        root.addWidget(hline())

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_view_page())
        self._stack.addWidget(self._build_edit_page())
        root.addWidget(self._stack)

    def _build_header(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName('SurfaceToolbar')
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 6, 8, 6)

        self._name_label = QLabel('No file selected')
        self._name_label.setObjectName('TextTitle')
        self._hid_label  = QLabel('')
        self._hid_label.setObjectName('TextHeader')
        
        name_col = QVBoxLayout()
        name_col.setSpacing(1)
        name_col.addWidget(self._name_label)
        name_col.addWidget(self._hid_label)

        self._edit_btn = QPushButton('✎')
        self._edit_btn.setObjectName('TextTitle')
        self._edit_btn.setFixedWidth(70)
        self._edit_btn.setEnabled(False)
        self._edit_btn.clicked.connect(self._enter_edit_mode)

        layout.addLayout(name_col, stretch=1)
        layout.addWidget(self._edit_btn)
        return bar

    def _build_view_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        
        content = QWidget()
        layout  = QVBoxLayout(content)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        ### Tags
        self._tags_row = QHBoxLayout()
        self._tags_row.setSpacing(4)
        self._tags_row.setContentsMargins(0, 0, 0, 0)
        tags_wrap = QWidget()
        tags_wrap.setLayout(self._tags_row)
        layout.addWidget(tags_wrap)
        ### Description
        self._desc_label = QLabel()
        self._desc_label.setWordWrap(True)
        self._desc_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._desc_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._desc_label)
        layout.addWidget(hline())
        ### Properties
        props_header = QLabel('Properties')
        props_header.setObjectName('TextHeader')
        self._props_label = QLabel('-')
        self._props_label.setWordWrap(True)
        self._props_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(props_header)
        layout.addWidget(self._props_label)
        layout.addWidget(hline())
        ### File Info
        info_header = QLabel('File Info')
        info_header.setObjectName('TextHeader')
        self._info_container = QWidget()
        self._info_layout    = QVBoxLayout(self._info_container)
        self._info_layout.setContentsMargins(0, 0, 0, 0)
        self._info_layout.setSpacing(2)
        layout.addWidget(info_header)
        layout.addWidget(self._info_container)
        layout.addStretch()

        scroll.setWidget(content)
        return scroll

    def _build_edit_page(self) -> QWidget:
        page   = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        layout.addWidget(QLabel('Title'))
        self._title_edit = QLineEdit()
        self._title_edit.setPlaceholderText('Display name...')
        layout.addWidget(self._title_edit)

        layout.addWidget(QLabel('Tags (comma-separated)'))
        self._tags_edit = QLineEdit()
        self._tags_edit.setPlaceholderText('ex. Character, Texture, System...')
        layout.addWidget(self._tags_edit)

        layout.addWidget(QLabel('Description'))
        self._desc_edit = QTextEdit()
        self._desc_edit.setPlaceholderText('Notes...')
        self._desc_edit.setFixedHeight(90)
        layout.addWidget(self._desc_edit)

        btn_row = QHBoxLayout()
        self._save_btn   = QPushButton('Save')
        self._cancel_btn = QPushButton('Cancel')
        self._save_btn.setObjectName('BtnImportant')
        self._save_btn.clicked.connect(self._on_save)
        self._cancel_btn.clicked.connect(self._exit_edit_mode)
        btn_row.addStretch()
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._save_btn)
        layout.addLayout(btn_row)
        layout.addStretch()

        return page

    def load_node(self, node: VfsNode, title: str, description: str, tags: tuple[str, ...]) -> None:
        '''Populate the panel'''
        self._current_node = node
        hid = node.hierarchical_id_str

        self._name_label.setText(title or node.name)
        self._hid_label.setText(hid)
        self._edit_btn.setEnabled(True)

        _clear_layout(self._tags_row)
        pill_source = tags if tags else node.category
        for tag in pill_source:
            if not tag:
                continue
            pill = _ClickableTag(tag)
            pill.tagClicked.connect(self.tagClicked.emit)
            self._tags_row.addWidget(pill)
        self._tags_row.addStretch()

        self._desc_label.setText(description or 'No description')
        self._props_label.setText('-')
        _clear_layout(self._info_layout)
        for label, value in [
            ('Size',          human_size(node.size)),
            ('Offset',        hex(node.offset) if node.offset else '-'),
            ('Physical',     'Yes' if node.is_physical else 'No'),
            ('Dependant HID', str(node.target) if node.target else '-')
        ]:
            self._info_layout.addWidget(_info_row(label, value))

        self._exit_edit_mode()

    def set_properties_text(self, text: str) -> None:
        '''Called by WorkspaceController when Properties action completes.'''
        self._props_label.setText(text or '-')

    def clear(self) -> None:
        self._current_node = None
        self._name_label.setText('No file selected')
        self._hid_label.setText('')
        self._desc_label.setText('')
        self._props_label.setText('-')
        self._edit_btn.setEnabled(False)
        _clear_layout(self._tags_row)
        _clear_layout(self._info_layout)
        self._exit_edit_mode()

    @property
    def current_node(self) -> VfsNode | None:
        return self._current_node
    
    def _enter_edit_mode(self) -> None:
        if not self._current_node:
            return
        self._title_edit.setText(self._name_label.text())
        pills: list[_ClickableTag] = [
            widget
            for i in range(self._tags_row.count())
            if (item := self._tags_row.itemAt(i)) is not None
            and isinstance(widget := item.widget(), _ClickableTag)
        ]
        self._tags_edit.setText(', '.join(p.text() for p in pills))
        self._desc_edit.setPlainText(
            self._desc_label.text()
            if self._desc_label.text() != 'No description.'
            else ''
        )
        self._edit_btn.setEnabled(False)
        self._stack.setCurrentIndex(1)

    def _exit_edit_mode(self) -> None:
        self._edit_btn.setEnabled(self._current_node is not None)
        self._stack.setCurrentIndex(0)

    def _on_save(self) -> None:
        if not self._current_node:
            return
        title = self._title_edit.text().strip()
        desc  = self._desc_edit.toPlainText().strip()
        tags  = tuple(
            t.strip()
            for t in self._tags_edit.text().split(',')
            if t.strip()
        )
        self.metadata_changed.emit(self._current_node, title, desc, tags)

class _ClickableTag(QLabel):
    tagClicked = pyqtSignal(str)

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName('BtnSurface')
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, ev: QMouseEvent | None) -> None:
        if ev and ev.button() == Qt.MouseButton.LeftButton:
            self.tagClicked.emit(self.text())

def _info_row(label: str, value: str) -> QWidget:
    row    = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    key = QLabel(label)
    key.setObjectName('TextHeader')
    val = QLabel(value)
    val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    layout.addWidget(key)
    layout.addStretch()
    layout.addWidget(val)
    return row

###-------------------------------------- Welcome Page --------------------------------------###

class WelcomePage(QWidget):
    request_open = pyqtSignal(Path)

    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        layout = QVBoxLayout(self)
        layout.setContentsMargins(50,50,50,50)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        subtitle = QLabel('Select a Radiata Stories ISO')
        subtitle.setObjectName('TextSubtitle')

        self.button = QPushButton()
        self.button.setObjectName('BtnLarge')
        self.set_loading(False)
        self.button.clicked.connect(self.open_file_dialog)

        layout.addWidget(subtitle)
        layout.addWidget(self.button)

    def open_file_dialog(self) -> None:
        start_dir = self.settings.last_iso_dir or ''
        path, _ = QFileDialog.getOpenFileName(self, 'Open ISO', start_dir, 'ISO Files (*.iso);;All Files (*)')
        if path:
            self.settings.last_iso_dir = str(Path(path).parent)
            self.request_open.emit(Path(path))

    def set_loading(self, is_loading: bool) -> None:
        if is_loading:
            self.button.setText('Loading...')
            self.button.setEnabled(False)
        else:
            self.button.setText('Open ISO')
            self.button.setEnabled(True)

###------------------------------------- Rebuilding Page -----------------------------------###

class RebuildStatusPage(QWidget):
    '''Displays logs and progress during the ISO rebuild process.'''
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(50, 50, 50, 50)
        
        header_bar = QHBoxLayout()
        self._cancel_btn = QPushButton('Cancel ISO Build')
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        header_bar.addWidget(self._cancel_btn)
        header_bar.addStretch()
        self.header = QLabel('Rebuilding ISO...')
        self.header.setObjectName('TextTitle')
        self.header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_bar.addWidget(self.header)
        header_bar.addStretch()
        right_spacer = QWidget()
        right_spacer.setFixedWidth(self._cancel_btn.sizeHint().width())
        header_bar.addWidget(right_spacer)
        layout.addLayout(header_bar)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setObjectName('TextMono')
        layout.addWidget(self.log_output)

        self._task_handle = None
        
    def append_log(self, message: str) -> None:
        self.log_output.append(message)
        
    def update_progress(self, percentage: int) -> None:
        self.progress_bar.setValue(percentage)

    def set_task_handle(self, handle: TaskHandle) -> None:
        self._task_handle = handle
        self._cancel_btn.setEnabled(True)
        self._cancel_btn.setText('Cancel ISO Build')

    def _on_cancel(self) -> None:
        if self._task_handle:
            self._task_handle.cancel()
            self._cancel_btn.setEnabled(False)
            self._cancel_btn.setText('Cancelling...')

    def on_rebuild_finished(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._task_handle = None

###------------------------------------- Menu Bar ------------------------------------------###

class MainMenuBar:
    def __init__(
            self, 
            main_window:      MainWindow, 
            workspace_page:   WorkspaceWidget, 
            dispatcher:       Dispatcher,
            metadata_store: NodeMetadataStore,
            app_settings:     AppSettings 
        ) -> None:
        self.window     = main_window
        self.workspace  = workspace_page
        self.dispatcher = dispatcher
        self._store     = metadata_store
        self.settings   = app_settings

        self._build_file_menu()
        self._build_view_menu()
        self._build_info_menu()

    @property
    def menu_bar(self) -> QMenuBar:
        menu_bar = self.window.menuBar()
        assert menu_bar is not None
        return menu_bar

    def _build_file_menu(self) -> None:
        file_menu = self.menu_bar.addMenu('&File')
        assert file_menu is not None

        self.dump_metadata = QAction('Dump metadata', self.window)
        self.dump_metadata.triggered.connect(self._handle_meta_dump)
        file_menu.addAction(self.dump_metadata)

        file_menu.addSeparator()

        self.open_action = QAction('Open ISO', self.window)
        self.open_action.setShortcut('Ctrl+O')
        self.open_action.triggered.connect(self._handle_open)
        file_menu.addAction(self.open_action)

        self.close_action = QAction('Close ISO', self.window)
        self.close_action.setShortcut('Ctrl+W')
        self.close_action.setEnabled(False)
        self.close_action.triggered.connect(self._handle_close)
        file_menu.addAction(self.close_action)

        file_menu.addSeparator()

        exit_action = QAction('Exit', self.window)
        exit_action.setShortcut('Ctrl+Q')
        exit_action.triggered.connect(self._handle_exit)
        file_menu.addAction(exit_action)

    def _build_view_menu(self) -> None:
        view_menu = self.menu_bar.addMenu('&View')
        assert view_menu is not None
        # Theme
        theme_menu = view_menu.addMenu('Theme')
        assert theme_menu is not None
        self._theme_actions: dict[str, QAction] = {}
        for name in ThemeManager.THEMES.keys():
            action = QAction(name, self.window)
            action.setCheckable(True)
            action.setChecked(name == self.settings.theme_name)
            action.triggered.connect(lambda checked, n=name: self._handle_theme_change(n))
            if action.isChecked():
                action.setEnabled(False)
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
        toggle_log.setToolTip('Hides the bottom log window in file browser.')
        toggle_log.triggered.connect(self._handle_toggle_log)
        view_menu.addAction(toggle_log)

        toggle_verbose_logging = QAction('Verbose Logging', self.window)
        toggle_verbose_logging.setCheckable(True)
        toggle_verbose_logging.setChecked(self.settings.verbose_logging)
        toggle_verbose_logging.setToolTip('Set logger to "debug" mode')
        toggle_verbose_logging.triggered.connect(self._handle_toggle_verbose)
        view_menu.addAction(toggle_verbose_logging)

        toggle_hidden = QAction('Show Hidden Files', self.window)
        toggle_hidden.setCheckable(True)
        toggle_hidden.setChecked(self.settings.show_hidden_files)
        toggle_hidden.setToolTip('Hides core File System nodes and Sentinels')
        toggle_hidden.triggered.connect(self._handle_toggle_hidden)
        view_menu.addAction(toggle_hidden)

    def _build_info_menu(self) -> None:
        info_menu = self.menu_bar.addMenu('Info')
        assert info_menu is not None

        legend_action = QAction('File Legend', self.window)
        legend_action.triggered.connect(self._handle_legend)
        info_menu.addAction(legend_action)

    #-------- Actions --------#
    def _handle_meta_dump(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self.window, 'Dump metadata', 'radi_metadata.json', 'JSON Files (*.json);; All Files (*)')
        if not path:
            return
        if not path.lower().endswith('.json'):
            path += '.json'
        self.window.metadata_store.dump_metadata(Path(path))

    def _handle_open(self) -> None:
        start_dir = self.settings.last_iso_dir or ''
        path, _ = QFileDialog.getOpenFileName(self.window, 'Open ISO', start_dir, 'ISO Files (*.iso);;All Files (*)')
        if path:
            self.settings.last_iso_dir = str(Path(path).parent)
            self.window.attempt_load_iso(Path(path))

    def _handle_close(self) -> None:
        self.dispatcher.close()
        self.open_action.setEnabled(True)
        self.close_action.setEnabled(False)
        self.window.welcome_page.set_loading(False)
        self.window.stack.setCurrentIndex(AppPage.WELCOME)

    def _handle_exit(self) -> None:
        QApplication.quit()

    def _handle_theme_change(self, theme_name: str) -> None:
        for name, action in self._theme_actions.items():
            action.setChecked(name == theme_name)
            if action.isChecked():
                action.setEnabled(False)
                continue
            action.setEnabled(True)
        self.window.set_theme(theme_name)

    def _handle_toggle_log(self, checked: bool) -> None:
        self.workspace.log_console.setVisible(checked)
        self.settings.show_log_console = checked

    def _handle_toggle_verbose(self, checked: bool) -> None:
        logging.getLogger('radiata').setLevel(logging.DEBUG if checked else logging.INFO)
        self.settings.verbose_logging = checked

    def _handle_toggle_hidden(self, checked: bool) -> None:
        '''Pass the toggle signal to the proxy model'''
        self.settings.show_hidden_files = checked
        if self.window.controller.proxy_model: # Prevent crashing when no proxy_model is live
            self.window.controller.proxy_model.set_show_hidden(checked)

    def _handle_legend(self) -> None:
        theme_name = self.window.current_theme
        LegendView(ThemeManager.THEMES.get(theme_name)).exec()

###------------------------------------------------- Search Overlay ---------------------------------------------------------###

class SearchOverlay(QLabel):
    '''Floating centered text overlay that fades when idle for searching'''
    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setObjectName('TextOverlay')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.opacity = 0.0

        self.fade_timer = QTimer(self)
        self.fade_timer.setInterval(50)
        self.fade_timer.timeout.connect(self._fade_step)

        self.idle_timer = QTimer(self)
        self.idle_timer.setInterval(300)
        self.idle_timer.setSingleShot(True)
        self.idle_timer.timeout.connect(self.fade_timer.start)

        self.hide()

    def _apply_opacity(self) -> None:
        '''Bake current opacity alpha into the widget stylesheet.

        Replaces the old QGraphicsOpacityEffect approach which forced offscreen
        rasterization and caused Windows repaint artifacts.  The background is
        kept fully transparent; only the text colour carries the alpha.
        '''
        c = QColor(ThemeManager.active_theme.TEXT)
        alpha = int(self.opacity * 255)
        self.setStyleSheet(
            f'#SearchOverlay {{'
            f'color: rgba({c.red()}, {c.green()}, {c.blue()}, {alpha});'
            f'background-color: rgba(0, 0, 0, 0);'
            f'font-weight: bold;'
            f'font-size: 42px;'
            f'}}'
        )

    def show_text(self, text: str) -> None:
        if not text:
            self.hide_overlay()
            return

        self.setText(text)
        self.opacity = .80
        self._apply_opacity()
        self.adjustSize()

        if self.parentWidget():
            parent = self.parentWidget()
            if parent:
                geo = parent.geometry()
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
        self._apply_opacity()

    def hide_overlay(self) -> None:
        self.opacity = 0
        self.fade_timer.stop()
        self.idle_timer.stop()
        self.hide()

###------------------------------------------- File Legend ------------------------------------------###

def build_legend_tree(theme) -> QStandardItemModel:
    model = QStandardItemModel()
    model.setHorizontalHeaderLabels(["Extension", "Description", "Support"])

    def add_category(name) -> QStandardItem:
        category = QStandardItem(name)
        category.setEditable(False)
        category.setBackground(QColor(theme.BORDER))
        empty1 = QStandardItem("")
        empty2 = QStandardItem("")
        empty1.setBackground(QColor(theme.BORDER))
        empty2.setBackground(QColor(theme.BORDER))
        model.appendRow([category, empty1, empty2])
        return category

    def add_item(parent, ext, desc, support="") -> None:
        ext_item = QStandardItem(ext)
        desc_item = QStandardItem(desc)
        support_item = QStandardItem(support)
        for item in (ext_item, desc_item, support_item):
            item.setEditable(False)
        parent.appendRow([ext_item, desc_item, support_item])

    ### File System
    fs = add_category("File System")
    add_item(fs, ".idx", "TOC", "Fully supported: 'Open ISO'")
    add_item(fs, ".slz", "Compressed file", "Fully supported: 'Decompress'")
    add_item(fs, ".sle", "Encrypted compressed file", "Fully supported: 'Decompress'")
    add_item(fs, ".kods", "Custom archive format", "Fully supported: 'Unpack'")
    add_item(fs, ".vib", "Vibration motor data")
    add_item(fs, ".elf", "Executables and IOP modules")

    ### Audio
    audio = add_category("Audio")
    add_item(audio, ".seqw", "Audio file container for ADPCM and PCM format streams", "---")
    add_item(audio, ".vag", "PS2 standard audio format", "---")
    add_item(audio, ".020", "Audio files. Mostly shorter instrumental SFX, occasional full song.", "Supported: TAC Audio Viewer, 'Export as WAV'. Missing: 'Import from WAV'")

    ### Movie
    movie = add_category("Movie")
    add_item(movie, ".fmv", "Movies", "---")

    ### Mesh
    mesh = add_category("Mesh")
    add_item(mesh, ".fps", "Mesh data head", "Experimentally supported: 'Deconstruct Chain'. \n'.fps-segment' also supports the experimental 'Extract FIS'")
    add_item(mesh, ".fss", "Mesh data terminal")
    add_item(mesh, ".idom", "Mesh data")
    add_item(mesh, ".lctp", "Mesh data", "Experimentally supported: 'Deconstruct Chain'")

    ### Event
    event = add_category("Event")
    add_item(event, ".evd", "Event VM dispatcher data", "---")

    ### Animation
    anim = add_category("Animation")
    add_item(anim, ".fas", "Animation data head", "Experimentally supported: 'Deconstruct Chain'")
    add_item(anim, ".hfas", "Animation data terminal")
    add_item(anim, ".rmac", "Animation data", "Experimentally supported: 'Deconstruct Chain'")
    add_item(anim, ".rta", "Animation data")
    add_item(anim, ".paf", "Animation data")

    ### Texture
    tex = add_category("Texture")
    add_item(tex, ".fis", "Texture data", "Supported: 'FIS Texture Editor', 'Export as PNG'. Missing: 'Import from PNG'")
    add_item(tex, ".fisp", "Texture data")
    add_item(tex, ".fisa", "Texture data")
    add_item(tex, ".tim2", "PS2 standard texture format", "---")

    ### Scene
    scene = add_category("Scene")
    add_item(scene, ".rbad", "Map object references")
    add_item(scene, ".rlf", "Scene data")
    add_item(scene, ".rmf", "Scene data")
    add_item(scene, ".ndnc", "Scene data")
    add_item(scene, ".xbdc", "Scene data")
    add_item(scene, ".pcdc", "Scene data")
    add_item(scene, ".dnal", "Scene data")
    add_item(scene, ".tgil", "Map model data", "Experimentally supported: 'Deconstruct Chain'")

    ### Gameplay
    game = add_category("Gameplay")
    add_item(game, ".dth", "Gameplay data")
    add_item(game, ".cpa", "Gameplay data")
    add_item(game, ".ipa", "Gameplay data")
    add_item(game, ".fdc", "Gameplay data")
    add_item(game, ".bcb", "Packed entity data, perhaps battle related")

    ### Unknown
    unk = add_category("Unknown / Descriptor")
    add_item(unk, ".mpa", "Unknown ")
    add_item(unk, ".rcp", "Grouped ID table", "---")
    add_item(unk, ".rcad", "Descriptor data")
    add_item(unk, ".png", "PNG image")

    note = add_category('Notes:')
    add_item(note, "....segment", '"Deconstruct Chain" unpacks segments with the extension '
                            'suffix \n"segment" to prevent unpacking previously unpacked files.')

    return model

class LegendView(QDialog):
    def __init__(self, theme, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Legend')
        self.resize(600,500)
        layout = QVBoxLayout(self)
        self.tree = LegendModel(theme)
        layout.addWidget(self.tree)

class LegendModel(QTreeView):
    def __init__(self, theme):
        super().__init__()
        self.setModel(build_legend_tree(theme))

        self.setHeaderHidden(False)
        self.expandAll()
        self.setRootIsDecorated(True)
        self.setIndentation(12)
        self.resizeColumnToContents(0)
        self.resizeColumnToContents(1)
        self.resizeColumnToContents(2)

###------------------------------------------- Utility ------------------------------------------###

def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        if item.layout():
            _clear_layout(item.layout())

