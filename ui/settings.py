'''Session persistence + cross-platform shortcuts'''
from __future__ import annotations

from enum import Enum, auto

from PyQt6.QtCore import QSettings, QByteArray, QOperatingSystemVersion
from PyQt6.QtGui import QKeySequence

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###----------------------------------------- App Settings ----------------------------------------------###

class AppSettings:
    _ORG = 'RadiataModding'
    _APP = 'Tool'

    def __init__(self) -> None:
        self._q = QSettings(self._ORG, self._APP)

    ### Geometry
    @property
    def geometry(self) -> QByteArray | None:
        v = self._q.value('geometry')
        return v if isinstance(v, QByteArray) else None

    @geometry.setter
    def geometry(self, value: QByteArray) -> None:
        self._q.setValue('geometry', value)

    ### Splitters
    @property
    def h_splitter(self) -> QByteArray | None:
        v = self._q.value('h_splitter')
        return v if isinstance(v, QByteArray) else None

    @h_splitter.setter
    def h_splitter(self, value: QByteArray) -> None:
        self._q.setValue('h_splitter', value)

    @property
    def v_splitter(self) -> QByteArray | None:
        v = self._q.value('v_splitter')
        return v if isinstance(v, QByteArray) else None

    @v_splitter.setter
    def v_splitter(self, value: QByteArray) -> None:
        self._q.setValue('v_splitter', value)

    ### Theme
    @property
    def theme_name(self) -> str:
        return str(self._q.value('theme_name', 'Dark'))

    @theme_name.setter
    def theme_name(self, value: str) -> None:
        self._q.setValue('theme_name', value)

    ### Zoom
    @property
    def zoom_delta(self) -> int:
        try:
            return int(self._q.value('zoom_delta', 0))
        except (TypeError, ValueError):
            return 0

    @zoom_delta.setter
    def zoom_delta(self, value: int) -> None:
        self._q.setValue('zoom_delta', value)

    ### MenuBar Toggles
    @property
    def show_log_console(self) -> bool:
        return self._q.value('show_log_console', True, type=bool)

    @show_log_console.setter
    def show_log_console(self, value: bool) -> None:
        self._q.setValue('show_log_console', value)

    @property
    def verbose_logging(self) -> bool:
        return self._q.value('verbose_logging', False, type=bool)

    @verbose_logging.setter
    def verbose_logging(self, value: bool) -> None:
        self._q.setValue('verbose_logging', value)

    @property
    def show_hidden_files(self) -> bool:
        return self._q.value('show_hidden_files', False, type=bool)

    @show_hidden_files.setter
    def show_hidden_files(self, value: bool) -> None:
        self._q.setValue('show_hidden_files', value)

    ### Last used paths
    @property
    def last_iso_dir(self) -> str:
        return str(self._q.value('last_iso_dir', ''))

    @last_iso_dir.setter
    def last_iso_dir(self, value: str) -> None:
        self._q.setValue('last_iso_dir', value)

    ### Sync
    def sync(self) -> None:
        self._q.sync()

###------------------------------ Cross-platform Shortcuts ---------------------------------------------###


class Shortcut(Enum):
    '''Hold all the shortcuts for the application so that users don't need to deal with the odd Qt defaults'''
    # File Operations
    OPEN = auto()
    CLOSE = auto()
    SAVE = auto()
    QUIT = auto()
    BACK = auto()

    # Edit Operations
    UNDO = auto()
    REDO = auto()
    CUT = auto()
    COPY = auto()
    PASTE = auto()
    DELETE = auto()
    SELECT_ALL = auto()

    # View / Nav Operations
    FIND = auto()
    ZOOM_IN = auto()
    ZOOM_OUT = auto()
    ZOOM_RESET = auto()

class Shortcuts:
    '''Provide platform-specific shortcuts for the application. Overrides the Qt defaults.'''
    _WINDOWS_LINUX = {
        Shortcut.OPEN:       'Ctrl+O',
        Shortcut.CLOSE:      'Ctrl+W',
        Shortcut.SAVE:       'Ctrl+S',
        Shortcut.QUIT:       'Ctrl+Q',
        Shortcut.BACK:       'Esc',

        Shortcut.UNDO:       'Ctrl+Z',
        Shortcut.REDO:       'Ctrl+Y',
        Shortcut.CUT:        'Ctrl+X',
        Shortcut.COPY:       'Ctrl+C',
        Shortcut.PASTE:      'Ctrl+V',
        Shortcut.DELETE:     'Del',
        Shortcut.SELECT_ALL: 'Ctrl+A',

        Shortcut.FIND:       'Ctrl+F',

        Shortcut.ZOOM_IN:    'Ctrl+=',
        Shortcut.ZOOM_OUT:   'Ctrl+-',
        Shortcut.ZOOM_RESET: 'Ctrl+0',
    }
    _MACOS = {
        Shortcut.OPEN:       'Cmd+O',
        Shortcut.CLOSE:      'Cmd+W',
        Shortcut.SAVE:       'Cmd+S',
        Shortcut.QUIT:       'Cmd+Q',
        Shortcut.BACK:       'Esc',

        Shortcut.UNDO:       'Cmd+Z',
        Shortcut.REDO:       'Cmd+Shift+Z',
        Shortcut.CUT:        'Cmd+X',
        Shortcut.COPY:       'Cmd+C',
        Shortcut.PASTE:      'Cmd+V',
        Shortcut.DELETE:     'Backspace',
        Shortcut.SELECT_ALL: 'Cmd+A',

        Shortcut.FIND:       'Cmd+F',

        Shortcut.ZOOM_IN:    'Cmd+=',
        Shortcut.ZOOM_OUT:   'Cmd+-',
        Shortcut.ZOOM_RESET: 'Cmd+0',
    }

    @classmethod
    def sequence(cls, shortcut: Shortcut) -> QKeySequence:
        '''Return the QKeySequence for the given shortcut.'''
        if QOperatingSystemVersion.currentType() == QOperatingSystemVersion.OSType.MacOS:
            return QKeySequence(cls._MACOS[shortcut])
        return QKeySequence(cls._WINDOWS_LINUX[shortcut])

    @classmethod
    def text(cls, shortcut: Shortcut) -> str:
        '''Return the text for the given shortcut.'''
        return cls.sequence(shortcut).toString(
            QKeySequence.SequenceFormat.NativeText
        )
