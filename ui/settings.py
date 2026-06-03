'''Stores and Sets application states for new session ease of use'''
from __future__ import annotations

from PyQt6.QtCore import QSettings, QByteArray

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