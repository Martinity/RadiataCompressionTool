"""
This file contains the logic for the QMainWindow (the main application window)
as well as the simple stack pages: WelcomePage and RebuildPage.

Once FileBrowserPage's log_console is initialized all logger.* calls will print
to the log console even when on a different page.

Here is a visual breakdown of the widget hierarchy with ISO loading/rebuild as example:
=========================================================================================
  VISUAL HIERARCHY (Main GUI Thread)         |   CONCURRENCY & TASKS (Worker Threads)
=========================================================================================
                                             |
[MainWindow]                                 |   [Dispatcher] (Task Coordinator)
 ├── MainMenuBar                             |    │
 ├── QStatusBar                              |    │
 │                                           |    │
 └── QStackedWidget                          |    ├── Async Task: Load ISO
      │                                      |    │    └── Returns: TaskHandle
      ├── [0] WelcomePage                    |    │
      │                                      |    └── Async Task: Rebuild ISO
      ├── [1] FileBrowserPage                |         ├── emit rebuild_progress
      │    ├── [0] tree_view                 |         └── emit rebuild_log
      │    ├── [1] search_view               |
      │    ├── FileMetadataPanel             |
      │    ├── SearchOverlay                 |
      │    └── log_console (Toggleable)      |
      │                                      |
      ├── [2] StagingPage                    |
      │    └── HexDiffModel                  |
      │                                      |
      ├── [3] RebuildStatusPage              |
      │    ├── QProgressBar ◄────────────────┼─── receive rebuild_progress
      │    └── QTextEdit ◄───────────────────┼─── receive rebuild_log
      │                                      |
      └── [4] EditorPage                     |
           └── *Plugin-specific
"""

from __future__ import annotations

import logging
import threading
import platform
import json
import subprocess
import os
from enum import IntEnum
from pathlib import Path

from core.dispatcher import Dispatcher
from core.metadata_manager import NodeMetadataStore
from core.node import VfsNode
from core.version import __version__
from core.workers import TaskHandle
from PyQt6.QtCore import QSettings, Qt, QTimer, pyqtSignal, QPropertyAnimation, QEasingCurve, QPoint
from PyQt6.QtGui import QAction, QCloseEvent, QColor, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMenuBar,
    QMessageBox, QProgressBar, QPushButton, QStackedWidget, QStatusBar, QTextEdit,
    QTreeView, QVBoxLayout, QWidget, QTabWidget, QLineEdit, QComboBox, QSizePolicy
)
from ui.editor_page import EditorPage
from ui.file_browser_page import FileBrowserBehavior, FileBrowserPage
from ui.settings import AppSettings, Shortcuts, Shortcut
from ui.staging_page import StagingPage
from ui.theme_manager import ThemeManager
from utilities import get_resource_path, human_size

logger = logging.getLogger(f'radiata.{__name__}')


# Enums for page stack idx
class AppPage(IntEnum):
    WELCOME     = 0
    FILEBROWSER = 1
    STAGING     = 2
    REBUILD     = 3
    EDITOR      = 4


###---------------------------------------------- Main Window ----------------------------------------###


class MainWindow(QMainWindow):
    """
    Serves as the root container for the application stack, managing the QSettings,
    theme state, and cross-component signal routing.
    """

    def __init__(self, dispatcher: Dispatcher, is_test: bool = False) -> None:
        super().__init__(parent=None)
        self._main_thread_id = threading.get_ident()
        # Setup App
        self.dispatcher      = dispatcher
        self.app_settings    = AppSettings()
        self.settings        = QSettings('RadiataModding', 'Tool')
        self.current_theme   = self.app_settings.theme_name
        self._zoom_delta     = self.app_settings.zoom_delta
        # Setup metadata database
        self.metadata_store  = NodeMetadataStore(
            get_resource_path('ui/assets/radi_metadata.json'),
            auto_save=True,
            parent=self,
        )
        self.metadata_store.load()
        self.dispatcher.set_metadata_store(self.metadata_store)
        # Setup View
        self.stack              = QStackedWidget()
        self.welcome_page       = WelcomePage(self.app_settings)
        self.file_browser_page  = FileBrowserPage(self.metadata_store)
        self.staging_page       = StagingPage(self.dispatcher)
        self.rebuild_page       = RebuildStatusPage()
        self.editor_page        = EditorPage()
        self._setup_ui()

        # Behavior controllers
        self.controller = FileBrowserBehavior(
            self.file_browser_page,
            self.editor_page,
            self.dispatcher,
            self.metadata_store,
        )
        self.menu_manager = MainMenuBar(
            self,
            self.file_browser_page,
            self.dispatcher,
            self.metadata_store,
            self.app_settings,
        )
        self.toast = Toast(self)
        self._setup_statusbar()
        self._connect_signals()
        self._restore_layout()
        # Start Thread Pool but queue the result to the next event cycle to prevent mac segfault
        if not is_test:
            QTimer.singleShot(
                0,
                lambda: self.dispatcher.task_coordinator.start_task(lambda **kwargs: None),
            )

    def _setup_ui(self) -> None:
        """Initializes the central widget stack and default window properties."""
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self.welcome_page)
        self.stack.addWidget(self.file_browser_page)
        self.stack.addWidget(self.staging_page)
        self.stack.addWidget(self.rebuild_page)
        self.stack.addWidget(self.editor_page)

        self.setWindowTitle(f'Radiata Modding Tool {__version__}')
        self.resize(1400, 900)

    @property
    def status_bar(self) -> QStatusBar:
        bar = self.statusBar()
        assert bar is not None
        return bar

    def _setup_statusbar(self) -> None:
        self.status_bar.showMessage('Ready')

    def _on_worker_log(self, msg: str) -> None:
        """
        Bound slot for worker log_message signals.
        Enforces execution on the main thread to prevent race conditions.
        """
        if threading.get_ident() != self._main_thread_id:
            logger.error('_on_worker_log ran off the main thread!')

    def _connect_signals(self) -> None:
        """Routes main window state signals between UI pages or dispatcher"""
        self.welcome_page.request_open.connect(self.attempt_load_iso)
        self.file_browser_page.btn_review.clicked.connect(lambda: self.stack.setCurrentIndex(AppPage.STAGING))
        self.staging_page.request_file_browser.connect(lambda: self.stack.setCurrentIndex(AppPage.FILEBROWSER))
        self.editor_page.back_requested.connect(lambda: self.stack.setCurrentIndex(AppPage.FILEBROWSER))

        self.dispatcher.iso_loaded.connect(self._on_iso_loaded)
        self.dispatcher.rebuild_requested.connect(self.start_rebuild)
        self.dispatcher.rebuild_progress.connect(self.rebuild_page.update_progress)
        self.dispatcher.rebuild_log.connect(self.rebuild_page.append_log)
        self.dispatcher.rebuild_complete.connect(self.on_rebuild_complete)
        self.dispatcher.iso_verified.connect(lambda build: self.status_bar.showMessage(build))

        self.dispatcher.action_progress.connect(self.toast.show_progress)
        self.dispatcher.file_browser_log.connect(self.file_browser_page.append_log)

    ###------------------------------- Appearance ----------------------------------###
    def _restore_layout(self) -> None:
        """Restore App State to previously used parameters."""
        s = self.app_settings
        if s.geometry:
            self.restoreGeometry(s.geometry)
        if s.h_splitter:
            self.file_browser_page.h_splitter.restoreState(s.h_splitter)
        if s.v_splitter:
            self.file_browser_page.v_splitter.restoreState(s.v_splitter)
        self._apply_theme()
        self.file_browser_page.log_console.setVisible(s.show_log_console)
        logging.getLogger('radiata').setLevel(
            logging.DEBUG if self.app_settings.verbose_logging else logging.INFO
        )

    def _apply_theme(self) -> None:
        """Apply the current_theme without changing the zoom level."""
        ThemeManager.apply_theme(self.current_theme, self._zoom_delta)

    def adjust_zoom(self, delta: int):
        self._zoom_delta += delta
        ThemeManager.apply_theme(self.current_theme, self._zoom_delta)
        self.app_settings.zoom_delta = self._zoom_delta
        logger.debug(f'Zoom Adjusted (Font size set to: {ThemeManager.current_font_size})')

    def reset_zoom(self) -> None:
        """Resets the UI zoom scaling back to the default baseline."""
        DEFAULT = 0
        delta_to_default = DEFAULT - self._zoom_delta
        self.adjust_zoom(delta_to_default)
        self._zoom_delta = DEFAULT
        self.app_settings.zoom_delta = DEFAULT

    def set_theme(self, theme_name: str) -> None:
        self.current_theme = theme_name
        self._apply_theme()
        self.app_settings.theme_name = theme_name

    def resizeEvent(self, event: QResizeEvent) -> None: # type: ignore
        '''Repositions the toast progress bar when the window is resized.'''
        super().resizeEvent(event)
        if self.toast.progress.isVisible():
            self.toast._reposition()

    ###----------------------------------- ISO ----------------------------------###

    def attempt_load_iso(self, path: Path) -> None:
        """
        Prompt the user for an ISO and pass the path to the dispatcher.
        ISO processing happens on a background thread.
        If dispatcher returns without a handle the ISO failed to load and the UI resets.
        """
        self.app_settings.last_iso_dir = str(path)
        self.status_bar.showMessage(f'Loading {path.name}...')
        self.welcome_page.set_loading(True)
        task_handle = self.dispatcher.load_source(path)
        if not isinstance(task_handle, TaskHandle):
            QMessageBox.critical(self, 'Load Error', f'No handler for {path.name}')
            self.welcome_page.set_loading(False)
            return
        task_handle.log_message.connect(self._on_worker_log)

    def start_rebuild(self, staged_nodes: list[VfsNode]) -> None:
        """Transitions UI and asks for save location before kicking off background thread"""
        file_path, _ = QFileDialog.getSaveFileName(self, 'Save Modified ISO', '', 'ISO Files (*.iso)')

        if not file_path:  # User canceled the save dialog, stay on staging page
            return

        self.stack.setCurrentWidget(self.rebuild_page)
        self.rebuild_page.log_output.clear()
        self.rebuild_page.progress_bar.setValue(0)
        handle = self.dispatcher.start_iso_rebuild(Path(file_path))
        if handle:
            self.rebuild_page.set_task_handle(handle)

    def _on_iso_loaded(self, success: bool, result: VfsNode | str) -> None:
        self.welcome_page.set_loading(False)
        if not success:
            msg = f'Failed to load ISO:\n{result}'
            QMessageBox.critical(self, 'Load Error', msg)
            logger.error(msg)
            self.status_bar.clearMessage()
            return
        has_iso = isinstance(result, VfsNode)
        if not has_iso:
            return
        self.controller.init_file_tree(result)
        self.stack.setCurrentIndex(AppPage.FILEBROWSER)
        self.file_browser_page.setFocus()
        self.menu_manager.open_action.setEnabled(not has_iso)
        self.menu_manager.close_action.setEnabled(has_iso)
        self.menu_manager.verify_hash.setEnabled(has_iso)

    def on_rebuild_complete(self, success: bool, message: str) -> None:
        """Handles the completion signal from the background thread"""
        self.rebuild_page.on_rebuild_finished()
        if success:
            QMessageBox.information(self, 'Success', message)
        else:
            QMessageBox.critical(self, 'Build Failed', message)
        self.stack.setCurrentWidget(self.file_browser_page)

    ###------------------------------------- Lifecycle --------------------------------------###

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        """Saves current UI layout parameters before destroying the window."""
        s = self.app_settings
        s.geometry = self.saveGeometry()
        s.h_splitter = self.file_browser_page.h_splitter.saveState()
        s.v_splitter = self.file_browser_page.v_splitter.saveState()
        s.sync()
        self.dispatcher.close()
        return super().closeEvent(a0)


###-------------------------------------- Welcome Page --------------------------------------###


class WelcomePage(QWidget):
    """
    Initial landing page prompting the user to load a source ISO.
    """

    request_open = pyqtSignal(Path)

    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch()

        subtitle = QLabel('Select a Radiata Stories ISO')
        subtitle.setObjectName('TextSubtitle')
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        self.device_widget = DeviceOrIsoDialog(
            parent=self,
            last_dir=self.settings.last_iso_dir or ''
        )
        self.device_widget.source_selected.connect(self._on_source_selected)
        layout.addWidget(self.device_widget, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()

        self.set_loading(False)

    def _on_source_selected(self, path: Path) -> None:
        if path.is_file() or path.is_dir():
            self.settings.last_iso_dir = str(path)
        self.request_open.emit(path)

    def set_loading(self, is_loading: bool) -> None:
        if is_loading:
            self.device_widget.btn_confirm.setText('Loading...')
            self.device_widget.btn_confirm.setEnabled(False)
        else:
            self.device_widget.btn_confirm.setText('Open ISO')
            self.device_widget.btn_confirm.setEnabled(True)


class DeviceOrIsoDialog(QWidget):
    '''
    A custom file dialog widget that allows the user to select either an existing ISO file
    from the pc's file browser or a physical block device/optical drive.
    Cross-platform compatible.
    '''
    source_selected = pyqtSignal(Path)

    def __init__(self, parent=None, last_dir: str = '') -> None:
        super().__init__(parent)
        self.last_dir = last_dir
        self.selected_path: Path | None = None
        self.setFixedWidth(600)
        self.setFixedHeight(300)
        self.setObjectName('SurfaceTabPanel')
        layout = QVBoxLayout(self)
        # Tabs for selecting between the two types of media inputs
        self.tabs = QTabWidget()
        self.tabs.setObjectName('SurfaceTabPanel')
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)
        self.tabs.addTab(self._create_iso_tab(), 'ISO Image File')
        self.tabs.addTab(self._create_device_tab(), 'Physical Disk')
        layout.addWidget(self.tabs)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.btn_confirm = QPushButton()
        self.btn_confirm.setObjectName('BtnLarge')
        self.btn_confirm.clicked.connect(self._on_confirm)

        btn_layout.addWidget(self.btn_confirm)
        layout.addLayout(btn_layout)

    def _check_elevation(self) -> bool:
        if platform.system() != 'Windows' and self.selected_path:
            return os.access(self.selected_path, os.R_OK)
        return True

    def _create_iso_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(QLabel('Select a .iso file:'))
        h_layout = QHBoxLayout()
        self.iso_path_input = QLineEdit()
        self.iso_path_input.setPlaceholderText('Path to .iso file')
        if self.last_dir:
            self.iso_path_input.setText(self.last_dir)
        browse_btn = QPushButton('Browse...')
        browse_btn.clicked.connect(self._browse_iso)
        h_layout.addWidget(self.iso_path_input)
        h_layout.addWidget(browse_btn)
        layout.addLayout(h_layout)
        return widget

    def _create_device_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(QLabel('Select a physical optical drive or block device:'))
        self.device_combo = QComboBox()
        self._populate_device_combo()
        layout.addWidget(self.device_combo)
        refresh_btn = QPushButton('Refresh Device List')
        refresh_btn.clicked.connect(self._populate_device_combo)
        layout.addWidget(refresh_btn)
        return widget

    def _browse_iso(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            'Select ISO',
            self.last_dir,
            'ISO Files (*.iso);;All Files (*)'
        )
        if path:
            self.iso_path_input.setText(path)

    def _populate_device_combo(self) -> None:
        self.device_combo.clear()
        system = platform.system()
        try:
            if system == 'Windows':
                # bitmask of active logical drives
                import ctypes, string
                bitmask = ctypes.windll.kernel32.GetLogicalDrives()
                for i, letter in enumerate(string.ascii_uppercase):
                    if bitmask & (1 << i):
                        drive_root = f'{letter}:\\'
                        if ctypes.windll.kernel32.GetDriveTypeW(drive_root) == 5:
                            device_path = f'\\\\.\\{letter}:'
                            self.device_combo.addItem(f'{letter} Optical Drive ({device_path})', userData=device_path)
            elif system == 'Linux':
                result = subprocess.run(
                    ['lsblk', '-J', '-o', 'NAME,SIZE,TYPE,PATH,MOUNTPOINT'],
                    capture_output=True, text=True, check=True,
                )
                data = json.loads(result.stdout)
                for device in data.get('blockdevices', []):
                    self._parse_lsblk_node(device)
            elif system == 'Darwin':
                result = subprocess.run(
                    ['diskutil', 'list', '-plist'],
                    capture_output=True, check=True,
                )
                if result.stdout:
                    import plistlib
                    plist = plistlib.loads(result.stdout)
                    for device in plist.get('AllDisksAndPartitions', []):
                        self._parse_diskutil_node(device)
        except Exception as e:
            self.device_combo.addItem(f'Error enumerating devices: {e}', userData='')
        if self.device_combo.count() == 0:
            self.device_combo.addItem('No Optical Drive found.', userData='')

    def _parse_lsblk_node(self, device: dict) -> None:
        dtype = device.get('type', '')
        if dtype != 'rom':
            return
        path  = device.get('path') or f"/dev/{device.get('name')}"
        size  = device.get('size', '')
        mount = device.get('mountpoint' , '')
        label = f'{path} - {dtype.upper()} ({size})'
        if mount:
            label += f' [Mounted: {mount}]'
        if dtype == 'rom':
            label = f'[Optical Drive] {path} ({size})'
        self.device_combo.addItem(label, userData=path)

    def _parse_diskutil_node(self, device: dict) -> None:
        dev_id = device.get('DeviceIdentifier', '')
        if not dev_id:
            return
        # Filter out non-optical drive devices
        try:
            info_proc = subprocess.run(['diskutil', 'info', '-plist', dev_id], capture_output=True, text=True)
            import plistlib
            info_plist = plistlib.loads(info_proc.stdout)
            if not info_plist.get('OpticalDrive'):
                return
        except Exception:
            return

        # Construct raw disk path
        path = f'/dev/r{dev_id}'
        size_bytes = device.get('Size', 0)
        if size_bytes > 0:
            size = human_size(size_bytes)
        else:
            size = 'Unknown Size'
        # Plist structures
        volume_name = device.get('VolumeName', '')
        label = f'[Optical Drive] {path} ({size})'
        if volume_name:
            label += f' - {volume_name}'
        self.device_combo.addItem(label, userData=path)

    def _on_confirm(self) -> None:
        if not self._check_elevation():
            raise PermissionError('Insufficient permissions for optical drive access.')
        if self.tabs.currentIndex() == 0:
            path_str = self.iso_path_input.text().strip()
            if not path_str:
                QMessageBox.warning(self, 'Invalid Path', 'Please specify a valid ISO file.')
                return
            self.selected_path = Path(path_str)
        else:
            path_str = self.device_combo.currentData()
            if not path_str:
                QMessageBox.warning(self, 'Invalid Device', 'Please select a valid optical drive.')
                return
            self.selected_path = Path(path_str)
        self.source_selected.emit(self.selected_path)


###------------------------------------- Rebuilding Page -----------------------------------###


class RebuildStatusPage(QWidget):
    """Displays logs and progress during an active ISO rebuild process."""

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
        """
        Prompts an active task handle to stop which will return an action complete signal.
        Action complete will trigger on_rebuild_finished
        """
        if self._task_handle:
            self._task_handle.cancel()
            self._cancel_btn.setEnabled(False)
            self._cancel_btn.setText('Cancelling...')

    def on_rebuild_finished(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._task_handle = None


###------------------------------------- Menu Bar ------------------------------------------###


class MainMenuBar:
    """Contructs and manages the application-wide top menu bar."""

    def __init__(
        self,
        main_window:       MainWindow,
        file_browser_page: FileBrowserPage,
        dispatcher:        Dispatcher,
        metadata_store:    NodeMetadataStore,
        app_settings:      AppSettings,
    ) -> None:
        self.window        = main_window
        self.file_browser  = file_browser_page
        self.dispatcher    = dispatcher
        self._store        = metadata_store
        self.settings      = app_settings

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

        self.verify_hash = QAction('Verify hash', self.window)
        self.verify_hash.triggered.connect(self._handle_verify_hash)
        self.verify_hash.setEnabled(False)
        file_menu.addAction(self.verify_hash)

        file_menu.addSeparator()

        self.open_action = QAction('Open ISO', self.window)
        self.open_action.setShortcut(Shortcuts.sequence(Shortcut.OPEN))
        self.open_action.triggered.connect(self._handle_open)
        file_menu.addAction(self.open_action)

        self.close_action = QAction('Close ISO', self.window)
        self.close_action.setShortcut(Shortcuts.sequence(Shortcut.CLOSE))
        self.close_action.setEnabled(False)
        self.close_action.triggered.connect(self._handle_close)
        file_menu.addAction(self.close_action)

        file_menu.addSeparator()

        exit_action = QAction('Exit', self.window)
        exit_action.setShortcut(Shortcuts.sequence(Shortcut.QUIT))
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
        zoom_in  = QAction('Zoom In', self.window)
        zoom_out = QAction('Zoom out', self.window)
        zoom_rst = QAction('Reset Zoom', self.window)
        zoom_in.setShortcut(Shortcuts.sequence(Shortcut.ZOOM_IN))
        zoom_out.setShortcut(Shortcuts.sequence(Shortcut.ZOOM_OUT))
        zoom_rst.setShortcut(Shortcuts.sequence(Shortcut.ZOOM_RESET))
        zoom_in.triggered.connect(lambda: self.window.adjust_zoom(+1))
        zoom_out.triggered.connect(lambda: self.window.adjust_zoom(-1))
        zoom_rst.triggered.connect(lambda: self.window.reset_zoom())
        for act in (zoom_in, zoom_out, zoom_rst):
            view_menu.addAction(act)

        # Toggles
        view_menu.addSeparator()
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

    # -------- Actions --------#
    def _handle_meta_dump(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self.window,
            'Dump metadata',
            'radi_metadata.json',
            'JSON Files (*.json);; All Files (*)',
        )
        if not path:
            return
        if not path.lower().endswith('.json'):
            path += '.json'
        self.window.metadata_store.dump_metadata(Path(path))

    def _handle_verify_hash(self) -> None:
        self.window.dispatcher._handle_verify_hash()

    def _handle_open(self) -> None:
        start_dir = self.settings.last_iso_dir or ''
        path, _ = QFileDialog.getOpenFileName(
            self.window, 'Open ISO', start_dir, 'ISO Files (*.iso);;All Files (*)'
        )
        if path:
            self.settings.last_iso_dir = str(Path(path).parent)
            self.window.attempt_load_iso(Path(path))

    def _handle_close(self) -> None:
        self.dispatcher.close()
        self.open_action.setEnabled(True)
        self.close_action.setEnabled(False)
        self.verify_hash.setEnabled(False)
        self.window.welcome_page.set_loading(False)
        self.window.stack.setCurrentIndex(AppPage.WELCOME)
        self.window.status_bar.clearMessage()

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
        self.file_browser.log_console.setVisible(checked)
        self.settings.show_log_console = checked

    def _handle_toggle_verbose(self, checked: bool) -> None:
        logging.getLogger('radiata').setLevel(logging.DEBUG if checked else logging.INFO)
        self.settings.verbose_logging = checked

    def _handle_toggle_hidden(self, checked: bool) -> None:
        """
        Pass the toggle signal to the proxy model.
        Hidden nodes are nodes that are sentinels or file system requirements
        """
        self.settings.show_hidden_files = checked
        if self.window.controller.proxy_model:  # Prevent crashing when no proxy_model is live
            self.window.controller.proxy_model.set_show_hidden(checked)

    def _handle_legend(self) -> None:
        theme_name = self.window.current_theme
        LegendView(ThemeManager.THEMES.get(theme_name), self.window).exec()


###------------------------------------------- Toasts ------------------------------------------###


class Toast(QWidget):
    '''
    All toast notifications popus for non-blocking user feedback.
    Handles temporary text messages and action progress bars.
    Animates upwards upon creation and recedes downwards automatically or manually.
    '''
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.SubWindow | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setObjectName('Popus')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.label)
        layout.addWidget(self.progress)

        self._is_active = False
        self.dismiss_timer = QTimer(self)
        self.dismiss_timer.setSingleShot(True)
        self.dismiss_timer.timeout.connect(self.dismiss)
        self.hide()

    def show_message(self, message: str, duration_ms: int = 3000) -> None:
        '''Display a temporary text-only notification.'''
        self.label.setText(message)
        self.progress.hide()
        self._slide_up(duration_ms)

    def show_progress(self, value: int, title: str = '') -> None:
        '''
        Display or update a persistent progress bar.
        Automatically fades out if value reaches 100.
        '''
        if title:
            self.label.setText(title)
        self.progress.show()
        self.progress.setValue(value)
        if not self._is_active:
            self._slide_up(display_duration=0)
        if value >= 100:
            self.dismiss_timer.start(2500)
        else:
            self.dismiss_timer.stop()

    def dismiss(self) -> None:
        '''Force the toast to slide down and hide'''
        if not self._is_active:
            return

        self.dismiss_timer.stop()
        self.anim = QPropertyAnimation(self, b'pos')
        self.anim.setDuration(300)
        self.anim.setStartValue(self.pos())
        self.anim.setEndValue(self.hidden_pos)
        self.anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self.anim.finished.connect(self.hide)
        self.anim.finished.connect(lambda: setattr(self, '_is_active', False))
        self.anim.start()

    def _calculate_position(self) -> tuple[QPoint, QPoint]:
        '''Determines the display anchor and off-screen anchor point from window geometry.'''
        parent = self.parentWidget()
        if not parent:
            return QPoint(0, 0), QPoint(0, 0)

        parent_rect   = parent.rect()
        padding_x  = 30
        padding_y = 40
        self.adjustSize()
        x = parent_rect.right() - self.width() - padding_x
        y = parent_rect.bottom() - self.height() - padding_y
        target_pos = QPoint(x, y)
        hidden_pos = QPoint(x, parent_rect.height())
        return target_pos, hidden_pos

    def _reposition(self) -> None:
        if not self.parentWidget():
            return
        self.target_pos, self.hidden_pos = self._calculate_position()
        if self._is_active:
            self.move(self.target_pos)

    def _slide_up(self, display_duration: int) -> None:
        '''Slide upward animation. Start auto-dismiss timer if duration > 0.'''
        self.target_pos, self.hidden_pos = self._calculate_position()
        if self._is_active:
            self.move(self.target_pos)
        else:
            self.move(self.hidden_pos)
            self.show()
            self.raise_()
            self.anim = QPropertyAnimation(self, b'pos')
            self.anim.setDuration(350)
            self.anim.setStartValue(self.hidden_pos)
            self.anim.setEndValue(self.target_pos)
            self.anim.setEasingCurve(QEasingCurve.Type.InCubic)
            self.anim.start()
            self._is_active = True
        if display_duration > 0:
            self.dismiss_timer.start(display_duration)

###------------------------------------------- File Legend ------------------------------------------###


def build_legend_tree(theme) -> QStandardItemModel:
    """
    Contructs the QStandardItemModel for the file type legend window.
    To be updated as the app evolves.

    Currently manually sets the colors from the theme. Honestly not very well developed...
    """
    model = QStandardItemModel()
    model.setHorizontalHeaderLabels(['Extension', 'Description', 'Support'])

    def add_category(name) -> QStandardItem:
        category = QStandardItem(name)
        category.setEditable(False)
        category.setBackground(QColor(theme.BG_WINDOW))
        empty1 = QStandardItem('')
        empty2 = QStandardItem('')
        empty1.setBackground(QColor(theme.BG_WINDOW))
        empty2.setBackground(QColor(theme.BG_WINDOW))
        model.appendRow([category, empty1, empty2])
        return category

    def add_item(parent, ext, desc, support='') -> None:
        ext_item = QStandardItem(ext)
        desc_item = QStandardItem(desc)
        support_item = QStandardItem(support)
        for item in (ext_item, desc_item, support_item):
            item.setEditable(False)
        parent.appendRow([ext_item, desc_item, support_item])

    ### File System
    fs = add_category('File System')
    add_item(fs, '.idx', 'TOC', "Fully supported: 'Open ISO'")
    add_item(fs, '.slz', 'Compressed file', "Fully supported: 'Decompress'")
    add_item(fs, '.sle', 'Encrypted compressed file', "Fully supported: 'Decompress'")
    add_item(fs, '.kods', 'Custom archive format', "Fully supported: 'Unpack'")
    add_item(fs, '.vib', 'Vibration motor data')
    add_item(fs, '.elf', 'Executables and IOP modules')

    ### Audio
    audio = add_category('Audio')
    add_item(audio, '.seqw', 'Audio file container for ADPCM and PCM format streams', '---')
    add_item(audio, '.vag', 'PS2 standard audio format', '---')
    add_item(
        audio,
        '.020',
        'Audio files. Mostly shorter instrumental SFX, occasional full song.',
        "Supported: TAC Audio Viewer, 'Export as WAV'. Missing: 'Import from WAV'",
    )

    ### Movie
    movie = add_category('Movie')
    add_item(movie, '.fmv', 'Movies', '---')

    ### Mesh
    mesh = add_category('Mesh')
    add_item(
        mesh,
        '.fps',
        'Mesh data head',
        "Experimentally supported: 'Deconstruct Chain'. \n'.fps-segment' also supports the experimental 'Extract FIS'",
    )
    add_item(mesh, '.fss', 'Mesh data terminal')
    add_item(mesh, '.idom', 'Mesh data')
    add_item(mesh, '.lctp', 'Mesh data', "Experimentally supported: 'Deconstruct Chain'")

    ### Event
    event = add_category('Event')
    add_item(event, '.evd', 'Event VM dispatcher data', '---')

    ### Animation
    anim = add_category('Animation')
    add_item(
        anim,
        '.fas',
        'Animation data head',
        "Experimentally supported: 'Deconstruct Chain'",
    )
    add_item(anim, '.hfas', 'Animation data terminal')
    add_item(anim, '.rmac', 'Animation data', "Experimentally supported: 'Deconstruct Chain'")
    add_item(anim, '.rta', 'Animation data')
    add_item(anim, '.paf', 'Animation data')

    ### Texture
    tex = add_category('Texture')
    add_item(
        tex,
        '.fis',
        'Texture data',
        "Supported: 'FIS Texture Editor', 'Export as PNG'. Missing: 'Import from PNG'",
    )
    add_item(tex, '.fisp', 'Texture data')
    add_item(tex, '.fisa', 'Texture data')
    add_item(tex, '.tim2', 'PS2 standard texture format', '---')

    ### Scene
    scene = add_category('Scene')
    add_item(scene, '.rbad', 'Map object references')
    add_item(scene, '.rlf', 'Scene data')
    add_item(scene, '.rmf', 'Scene data')
    add_item(scene, '.ndnc', 'Scene data')
    add_item(scene, '.xbdc', 'Scene data')
    add_item(scene, '.pcdc', 'Scene data')
    add_item(scene, '.dnal', 'Scene data')
    add_item(
        scene,
        '.tgil',
        'Map model data',
        "Experimentally supported: 'Deconstruct Chain'",
    )

    ### Gameplay
    game = add_category('Gameplay')
    add_item(game, '.dth', 'Gameplay data')
    add_item(game, '.cpa', 'Gameplay data')
    add_item(game, '.ipa', 'Gameplay data')
    add_item(game, '.fdc', 'Gameplay data')
    add_item(game, '.bcb', 'Packed entity data, perhaps battle related')

    ### Unknown
    unk = add_category('Unknown / Descriptor')
    add_item(unk, '.mpa', 'Unknown ')
    add_item(unk, '.rcp', 'Grouped ID table', '---')
    add_item(unk, '.rcad', 'Descriptor data')
    add_item(unk, '.png', 'PNG image')

    note = add_category('Notes:')
    add_item(
        note,
        '....segment',
        '"Deconstruct Chain" unpacks segments with the extension '
        'suffix \n"segment" to prevent unpacking previously unpacked files.',
    )

    return model


class LegendView(QDialog):
    def __init__(self, theme, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Legend')
        if parent:
            self.resize(parent.size())
        else:
            self.resize(600, 500)
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
