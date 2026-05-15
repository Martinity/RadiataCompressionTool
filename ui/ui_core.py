from __future__ import annotations

import json
from pathlib import Path
from enum import IntEnum
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal, QModelIndex, QSettings, QObject, QTimer, QEvent
from PyQt6.QtWidgets import (
    QMainWindow, QStackedWidget, QMessageBox, QWidget, QMenu, QVBoxLayout, QSplitter, 
    QFileDialog, QApplication, QLabel, QPushButton, QTreeView, QListView, QListWidget, 
    QHBoxLayout, QListWidgetItem, QProgressBar, QTextEdit, QHeaderView,
    QDialog, QScrollArea, QFrame, QGraphicsOpacityEffect
)
from PyQt6.QtGui import QAction, QCloseEvent, QKeyEvent, QMouseEvent, QShortcut, QKeySequence, QImage, QPixmap

from core.node import VfsNode, ModTracker
from core.dispatcher import Dispatcher
from core.registry import Registry, GLOBAL_ACTIONS
from core.contracts import BaseEditor
from core.workers import ActionStatus, ActionResult, ActionType, ActionDef
from ui.logger import LoggingWindow
from ui.tree_model import TreeProxyModel, VfsTreeModel, FlatSearchModel
from ui.theme_manager import ThemeManager
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

# Descriptor JSON loaded once
_DESCRIPTORS: dict[str, dict] = {}

def _load_descriptors(path: Path) -> None:
    global _DESCRIPTORS
    try:
        _DESCRIPTORS = json.loads(path.read_text(encoding='utf-8'))
        logger.info(f'Loaded {len(_DESCRIPTORS)} file descriptors.')
    except FileNotFoundError:
        logger.debug(f'No descriptor file at {path}')
    except json.JSONDecodeError as e:
        logger.warning(f'Descriptor JSON parse error: {e}')

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
        self.dispatcher    = dispatcher
        self.settings      = QSettings('RadiataModding', 'Tool')
        saved_theme = self.settings.value('theme_name', 'Dark')
        self.current_theme = saved_theme
        self._setup_zoom_shortcuts()

        # Setup View
        self.stack          = QStackedWidget()
        self.welcome_page   = WelcomePage()
        self.workspace_page = WorkspaceWidget()
        self.staging_page   = StagingPage(self.dispatcher.tracker)
        self.rebuild_page   = RebuildStatusPage()
        self.editor_page    = EditorPage()

        _load_descriptors(get_resource_path('ui/assets/descriptor.json'))
        self._setup_ui()

        # Controllers
        self.controller   = WorkspaceController(
            self.workspace_page, 
            self.editor_page,
            self.dispatcher, 
            self.dispatcher.tracker,
        )
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
        self.stack.addWidget(self.editor_page)
        self.adjust_zoom(0) # Initialize the style sheet via font, probably a scuffy way to do this
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
        self.staging_page.request_workspace.connect(lambda: self.stack.setCurrentIndex(AppPage.WORKSPACE))
        self.editor_page.back_requested.connect(lambda: self.stack.setCurrentIndex(AppPage.WORKSPACE))
        
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
        ThemeManager.apply_theme(self.current_theme, delta)
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
        if self.dispatcher:
            self.dispatcher.close()
        return super().closeEvent(event)

###------------------------------------------ Workspace UI -------------------------------------###

class WorkspaceWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._init_views()
        self._assemble_layout()

    def _init_views(self) -> None:
        self.tree_view = QTreeView()
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.setUniformRowHeights(True)
        self.tree_view.setAnimated(False)

        self.search_results_view = QListView()
        self.sidebar_stack = QStackedWidget()
        self.sidebar_stack.addWidget(self.tree_view)
        self.sidebar_stack.addWidget(self.search_results_view)

        self.descriptor_panel = FileDescriptorPanel()
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

###-------------------------------------------- Workspace Signals -------------------------###

class WorkspaceController(QObject):
    '''Handles all signals and logic for the workspace'''
    def __init__(
            self, 
            workspace:   WorkspaceWidget, 
            editor_page: EditorPage, 
            dispatcher:  Dispatcher, 
            tracker:     ModTracker,
    ) -> None:
        super().__init__(parent=workspace)
        self.view        = workspace
        self.editor_page = editor_page
        self.dispatcher  = dispatcher
        self.tracker     = tracker
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

        self._pending_editor: BaseEditor | None = None

    def init_workspace(self, root_node: VfsNode) -> None:
        ### Models
        self.tree_model  = VfsTreeModel(self.dispatcher.vfs)
        self.proxy_model = TreeProxyModel()
        self.proxy_model.setSourceModel(self.tree_model)
        self.proxy_model.set_descriptors(_DESCRIPTORS)

        ### Tree
        self.view.tree_view.setModel(self.proxy_model)
        self.view.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.tree_view.setSortingEnabled(True)
        self.view.tree_view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        
        ### Search / Filter
        self.search_results_view = QListView()
        self.search_model = FlatSearchModel(self.dispatcher.vfs, _DESCRIPTORS)
        self.search_results_view.setModel(self.search_model)

        self.search_results_view.clicked.connect(self.handle_tree_select)

        self.search_overlay = SearchOverlay(self.view.window())

        self.view.installEventFilter(self)
        self.view.tree_view.installEventFilter(self)
        self.view.descriptor_panel.tagClicked.connect(self.on_tag_clicked)

        try:
            self.view.tree_view.customContextMenuRequested.disconnect()
        except TypeError:
            pass

        self.view.tree_view.customContextMenuRequested.connect(self.handle_context_menu)

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
        self.view.tree_view.setUniformRowHeights(True)
        self.view.update_review_bar(False, 0)
        
    def on_tracking_update(self, modified_count: int, staged_count: int):
        '''Controls the apply modifications button visibility'''
        total = modified_count + staged_count
        self.view.review_bar.setVisible(total > 0)
        self.view.status_label.setText(f'{total} modification(s) pending.')

    ###----------------- Tree interactions-------------------###

    def handle_tree_select(self, current: QModelIndex, _previous: QModelIndex) -> None:
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

    def handle_tree_double_click(self, index: QModelIndex) -> None:
        if not self.proxy_model:
            return
        node: VfsNode | None = self.proxy_model.mapToSource(index).data(Qt.ItemDataRole.UserRole)
        if not node or node.children:
            return
        editor_classes = Registry.get_editors(node)
        if editor_classes:
            self.launch_editor(node, editor_classes[0])

    def on_tag_clicked(self, tag_name: str) -> None:
        self.search_buffer = tag_name
        self.proxy_model.set_search_query(tag_name)
        self.search_overlay.show_text(f'Tag: {tag_name}')
        self.view.tree_view.expandAll()

    def on_search_updated(self, query: str):
        if not query:
            self.sidebar_stack.setCurrentIndex(0)
            return
        self.sidebar_stack.setCurrentIndex(1)
        self.search_model.set_query(query)

    ###---------------------- Context Menu -----------------------###

    def handle_context_menu(self, position) -> None:
        if not self.proxy_model:
            return
        proxy_index = self.view.tree_view.indexAt(position)
        if not proxy_index.isValid(): 
            return
        node: VfsNode | None = self.proxy_model.mapToSource(proxy_index).data(Qt.ItemDataRole.UserRole)
        if not node: 
            return
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
        profile = Registry.get_profile(node)
        if profile:
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
        
        menu.exec(self.view.tree_view.viewport().mapToGlobal(position))

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
            logger.debug(f'No ActionDef for completed action "{result.action_name}"')
            return
        
        match action_def.action_type:
            case ActionType.DIALOG:
                if (result.action_name == 'Properties'):
                    if result.payload or result.message:
                        self.view.descriptor_panel.set_properties_text(str(result.payload or result.message))
                    else:
                        logger.warning('"Properties" action returned without payload...')
                elif isinstance(result.payload, QImage):
                    dlg = TexturePreviewDialog(result.payload, title=f'Preview: {result.node.name}', parent=self.view)
                    dlg.exec()
                else:
                    QMessageBox.information(
                        self.view, action_def.name, str(result.payload or result.message)
                    )
            case ActionType.TREE_EXPAND:
                if self.tree_model and self.proxy_model: # expand to see new children
                    source_index = self.tree_model.index_for_node(result.node)
                    if source_index.isValid():
                        self.view.tree_view.expand(self.proxy_model.mapFromSource(source_index))
            case ActionType.PROCESS:
                if isinstance(result.payload, bytes) and result.payload:
                    editor_classes = Registry.get_editors(result.node)
                    if editor_classes:
                        self.launch_editor(result.node, editor_classes[0])
            case ActionType.EXPORT:
                logger.info(f'Exported: {result.message}')
            case ActionType.IMPORT:
                pass # tree is refreshed via signal

    ###------------------- Editor --------------------###

    def launch_editor(self, node: VfsNode, editor_class: type[BaseEditor]) -> None:
        '''Instantiate new editor and create view for it'''
        if self._pending_editor is not None: # 
            self._pending_editor.show_load_error('Cancelled... another file was opened')
            self._pending_editor = None

        new_editor = editor_class()
        new_editor.begin_loading(node)
        new_editor.apply_requested.connect(self.dispatcher.apply_edit)
        self.editor_page.load_editor(new_editor, node)
        self._pending_editor = new_editor

        window = self.view.window()
        if isinstance(window, QMainWindow) and hasattr(window, 'stack'):
            window.stack.setCurrentIndex(AppPage.EDITOR)
        else: # Recursive main window fallback
            widget = self.view
            while widget and not isinstance(widget, QMainWindow):
                widget = widget.parent()
            if widget:
                widget.stack.setCurrentWidget(self.editor_page)

        signals = self.dispatcher.open_editor(node, new_editor)
        if not signals:
            raise ValueError('Navigator not initialized')
        signals.finished.connect(
            lambda succes, payload, e=new_editor: self._on_editor_data_ready(succes, payload, e)
        )
        plugin_name = getattr(editor_class, '_plugin_name', editor_class.__name__)
        logger.info(f'Opening "{node.name}" in {plugin_name}')

    def _on_editor_data_ready(self, success: bool, payload: Any, editor: BaseEditor) -> None:
        '''Verifies that the payload matches the editors expected type'''
        if editor is not self._pending_editor:
            logger.debug('Editor data arrived for a superseded editor... discarding')
            return
        
        from core.workers import EditorPayload
        if not success or not isinstance(payload, EditorPayload):
            editor.show_load_error(str(payload)) if not success else 'Unexpected payload type'
            self._pending_editor = None
            return
            error = str(payload) if not success else 'Unexpected payload type'
            logger.error(f'Editor data preparation failed: {error}')
            if hasattr(editor, 'info_label'):
                editor.info_label.setText(f'Load failed: {error}')
            return
        if payload.node is not editor.current_node:
            logger.debug('EditorPayload node mismatch')
            return
        
        editor.receive_data(payload.data, self.dispatcher.get_node_data)
        self._pending_editor = None
        logger.debug(f'Editor populated for {payload.node.name}')

    ###-------------------- Search --------------------###

    def eventFilter(self, obj: QObject, event: QKeyEvent) -> bool:    
        if event.type() == QEvent.Type.KeyPress:
            key_event: QKeyEvent = event
            # Ignore keyboard events when modifiers are held
            if key_event.modifiers() & (Qt.KeyboardModifier.ControlModifier | 
                                        Qt.KeyboardModifier.AltModifier | 
                                        Qt.KeyboardModifier.MetaModifier):
                return super().eventFilter(obj, event)

            if key_event.key() == Qt.Key.Key_Escape and self.proxy_model:
                self.search_buffer = ''
                self.proxy_model.set_search_query('')
                self.search_overlay.hide_overlay()
                self.search_timer.stop()
                return True
            
            if key_event.key() == Qt.Key.Key_Backspace and self.search_buffer and self.proxy_model:
                self.search_buffer = self.search_buffer[:-1]
                self.proxy_model.set_search_query(self.search_buffer)
                self.view.tree_view.expandAll()
                self.search_overlay.show_text(self.search_buffer)
                self.search_timer.start()
                return True

            text = key_event.text()
            if text and text.isprintable() and self.proxy_model:
                self.search_buffer += text
                self.proxy_model.set_search_query(self.search_buffer)
                self.view.tree_view.expandAll()
                self.search_overlay.show_text(self.search_buffer)
                self.search_timer.start()
                return True
        return super().eventFilter(obj, event)
    
    def clear_search_buffer(self) -> None:
        '''Clears the search buffer. To reset proxy "Esc" in eventFilter'''
        self.search_buffer = ''

###----------------------------------- Descriptor Panel ------------------------------------###

class FileDescriptorPanel(QWidget):
    '''Right panel of the workspace'''
    tagClicked = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None, controller = None) -> None:
        super().__init__(parent)
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
        self._current_node = node
        hid = node.hierarchical_id_str
        descriptor = _DESCRIPTORS.get(hid, {})

        ### Header
        title = descriptor.get('title', node.name)
        self._name_label.setText(title)
        self._hid_label.setText(hid)

        ### Tags
        _clear_layout(self._tags_row)
        for tag in descriptor.get('tags', []):
            tag_clickable = ClickableTag(tag)
            tag_clickable.tagClicked.connect(self.tagClicked.emit)
            self._tags_row.addWidget(tag_clickable)
        self._tags_row.addStretch()
        self._tags_container.setVisible(bool(descriptor.get('tags')))

        ### Description
        self._description.setText(descriptor.get('description', 'No description available'))

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
        if ev.button() == Qt.MouseButton.LeftButton:
            self.tagClicked.emit(self.text())

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

###---------------------------------- Editor Page -------------------------------------------###

class EditorPage(QWidget):
    '''UX is not final. Especially for this...'''
    back_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._current_editor: BaseEditor | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QWidget()
        toolbar.setObjectName('EditorToolbar')
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(10, 5, 10, 5)

        self._back_btn = QPushButton('Back')
        self._back_shortcut = QShortcut(QKeySequence('Esc'), self)
        self._back_btn.setObjectName('FloatClearButton')
        self._back_shortcut.activated.connect(self._back_btn.click)
        self._back_btn.clicked.connect(self._on_back)

        self._editor_title = QLabel('Editor')
        self._editor_title.setObjectName('SectionHeader')

        bar.addWidget(self._back_btn)
        bar.addWidget(self._editor_title)
        bar.addStretch()

        layout.addWidget(toolbar)

        self._editor_area = QStackedWidget()
        layout.addWidget(self._editor_area)

    def load_editor(self, editor: BaseEditor, node: VfsNode) -> None:
        if self._current_editor:
            self._current_editor.cleanup()
            self._editor_area.removeWidget(self._current_editor)
            self._current_editor.deleteLater()

        self._current_editor = editor
        self._editor_area.addWidget(editor)
        self._editor_area.setCurrentWidget(editor)
        plugin_name = getattr(editor.__class__, '_plugin_name', editor.__class__.__name__)
        self._editor_title.setText(f'{plugin_name} / {node.name}')

    def _on_back(self) -> None:
        if self._current_editor and self._current_editor.is_dirty():
            if self._current_editor.is_mutable:
                reply = QMessageBox.question(
                    self,
                    'Unsaved Changes', 'You have unsaved changes. Apply before closing?',
                    QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
                    QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Save,
                )
                if reply == QMessageBox.StandardButton.Cancel:
                    return
                if reply == QMessageBox.StandardButton.Save:
                    self._current_editor.apply_changes()
                else:
                    self._current_editor.discard_changes()
        self.back_requested.emit()

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

        for name in ThemeManager.THEMES.keys():
            action = QAction(name, self.window)
            action.setCheckable(True)
            action.setChecked(name == self.window.current_theme)
            action.triggered.connect(lambda checked, n=name: self._handle_theme_change(n))
            view_menu.addAction(action)

        toggle_log = QAction('Show Log Console', self.window)
        toggle_log.setCheckable(True)
        toggle_log.setChecked(True)
        toggle_log.triggered.connect(self.workspace.log_console.setVisible)
        view_menu.addAction(toggle_log)

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
        self.window.stack.setCurrentIndex(AppPage.WELCOME)

    def _handle_exit(self) -> None:
        QApplication.quit()

    def _handle_theme_change(self, theme_name: str) -> None:
        self.window.current_theme = theme_name
        self.window.adjust_zoom(0)

    def _handle_toggle_hidden(self, checked: bool) -> None:
        '''Pass the toggle signal to the proxy model'''
        if self.window.controller.proxy_model:
            self.window.controller.proxy_model.set_show_hidden(checked)


class TexturePreviewDialog(QDialog):
    def __init__(self, image: QImage, title="Texture Preview", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(600, 500)
        
        layout = QVBoxLayout(self)
        
        # Scroll area in case the texture is larger than the window
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # Convert QImage to QPixmap for display
        pixmap = QPixmap.fromImage(image)
        self.image_label.setPixmap(pixmap)
        
        # Add a dark background to see transparency/alpha better
        self.image_label.setStyleSheet("background-color: #1a1a1a;")
        
        scroll.setWidget(self.image_label)
        layout.addWidget(scroll)

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
