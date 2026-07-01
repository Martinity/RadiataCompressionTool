from __future__ import annotations

import threading
from typing import Any, Callable, TYPE_CHECKING
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QStackedWidget, QMessageBox
from PyQt6.QtGui import QShortcut, QKeySequence
from PyQt6 import sip

from core.workers import TaskHandle
if TYPE_CHECKING:
    from core.node import VfsNode
    from core.contracts import BaseEditor

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###------------------------------------------ Editor Session -------------------------------------------------###

class EditorSession:
    '''Manages the lifecycle of an editor'''
    _VALID_TRANSITIONS: dict[str, set[str]] = {
        'loading': {'ready', 'error', 'cancelled'},
        'ready':   {'saving', 'cancelled'},
        'saving':  {'ready', 'error', 'cancelled'},
        'error':   {'cancelled', 'ready'},
        'cancelled': set()
    }
    _counter_lock = threading.Lock()
    _next_id      = 0

    def __init__(self, node: VfsNode, editor: BaseEditor, dispatch_callback: Callable) -> None:
        with EditorSession._counter_lock:
            self.session_id = EditorSession._next_id
            EditorSession._next_id += 1
        self.node   = node
        self.editor = editor
        self._state = 'loading'
        self._active_task: TaskHandle | None = None
        self._dispatch_cb = dispatch_callback
        self.state_changed_callback: Callable[[str], None] | None = None
        self._post_save_failure:     Callable              | None = None
        self._post_save_success:     Callable              | None = None
        logger.debug(f'EditorSession #{self.session_id} created for "{node.name}"')

    def set_active_task(self, task_handle: TaskHandle) -> None:
        '''Links backgroundd worker handle to the session'''
        self._active_task = task_handle

    def apply_changes(self) -> None:
        '''Handles save state transition'''
        if self._state != 'ready' or not self.editor.is_dirty():
            return
        self._transition('saving')
        self.editor.snapshot()
        try:
            self._dispatch_cb(
                self.node,
                self.editor._pending_data,
            )
        except Exception:
            logger.exception(f'EditorSession #{self.session_id}: dispatch raised; reverting to ready')
            self._transition('ready')
            raise

    def save_then(self, on_success: Callable | None, on_failure: Callable | None) -> None:
        '''Apply changes and fire callback on the next successful save'''
        self._post_save_success = on_success
        self._post_save_failure = on_failure
        self.apply_changes()

    def confirm_save(self) -> None:
        '''Dispatcher calls this when save complete'''
        if self._state == 'cancelled':
            return
        if sip.isdeleted(self.editor):
            logger.debug(f'Session {self.session_id} commit aborted: C++ Editor object is dead.')
            return
        self.editor.confirm_changes_applied()
        self._transition('ready')
        cb, self._post_save_success = self._post_save_success, None
        if cb:
            cb()

    def reject_save(self, reason: str) -> None:
        '''Dispatcher calls this when save fails'''
        if self._state == 'cancelled':
            return
        if sip.isdeleted(self.editor):
            logger.debug(f'Session {self.session_id} reject aborted: C++ Editor object is dead.')
            return
        self.editor.reject_changes_applied(reason)
        self._transition('ready')
        cb, self._post_save_failure = self._post_save_failure, None
        if cb:
            cb(reason)

    @property
    def state(self) -> str:
        return self._state
    
    def is_active(self) -> bool:
        '''True until _populate_ui aka background thread finishes'''
        return self._state == 'loading'
    
    def is_done(self) -> bool:
        '''True after the session has reached a final state'''
        return self._state in ('ready', 'error', 'cancelled')
    
    def _transition(self, target: str) -> None:
        valid = self._VALID_TRANSITIONS.get(self._state, set())
        if target not in valid:
            raise ValueError(f'EditorSession #{self.session_id}: invalid transition {self._state!r}->{target!r}')
        logger.debug(f'EditorSession #{self.session_id} ("{self.node.name}"): {self._state}->{target}')
        self._state = target
        if self.state_changed_callback:
            self.state_changed_callback(target)

    def complete(self, data: Any, data_resolver: Callable | None = None) -> None:
        '''Populate the editor with processed data'''
        self._transition('ready')
        self.editor.receive_data(data, data_resolver)

    def fail(self, reason: str) -> None:
        '''Show load error in editor'''
        self._transition('error')
        self.editor.show_error(reason)
        logger.error(f'EditorSession #{self.session_id} ("{self.node.name}") failed: {reason}')

    def cancel(self) -> None:
        '''Silently discard any pending data'''
        if self._state == 'cancelled':
            return
        if self._active_task:
            logger.debug(f'Session {self.session_id} canceling worker thread.')
            self._active_task.cancel()
            try:
                self._active_task.finished.disconnect()
                self._active_task.progress.disconnect()
                self._active_task.log_message.disconnect()
            except TypeError:
                pass # Ignore signals that were never connected
            self._active_task = None
        prev = self._state
        self._state = 'cancelled'
        logger.debug(f'EditorSession #{self.session_id} ("{self.node.name}"): {prev}->cancelled')

    def __repr__(self) -> str:
        return f'<EditorSession #{self.session_id} node={self.node.name} state={self._state!r}>'

###---------------------------------- Editor Page -------------------------------------------###

class EditorPage(QWidget):
    '''UX is not final. Especially for this...'''
    back_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._current_session: EditorSession | None = None
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

        self._wire_editor(session)

    def _wire_editor(self, session: EditorSession) -> None:
        editor = session.editor
        session.state_changed_callback = self._on_session_state_changed
        is_mutable = getattr(editor, 'is_mutable', True)
        self.btn_save.setVisible(is_mutable)
        self.btn_revert.setVisible(is_mutable)
        self.btn_undo.setVisible(False)
        self.btn_redo.setVisible(False)
        session.editor.undo_state_changed.connect(self._on_undo_state_changed)
        if is_mutable:
            session.editor.dataChanged.connect(self._on_editor_state_changed)
        self._update_title(is_dirty=False)
        self._set_toolbar_enabled(False)

    def _on_undo_state_changed(self, can_undo: bool, can_redo: bool) -> None:
        self.btn_undo.setVisible(can_undo or can_redo)
        self.btn_redo.setVisible(can_redo or can_undo)
        self.btn_undo.setEnabled(can_undo)
        self.btn_redo.setEnabled(can_redo)

    def _deconstruct_old_session(self) -> None:
        if self._current_session:
            self._current_session.state_changed_callback = None
            self._current_session.cancel()
        old_editor = self._current_session.editor if self._current_session else None
        if not old_editor:
            return
        self._editor_area.removeWidget(old_editor)
        old_editor.cleanup()
        old_editor.deleteLater()
        self._current_session = None

    def _refresh_toolbar(self) -> None:
        session = self._current_session
        is_ready  = bool(session and session.state == 'ready')
        # is_saving = bool(session and session.state == 'saving')
        is_dirty  = bool(session and session.editor.is_dirty())

        self.btn_save.setEnabled(is_dirty and is_ready)
        self.btn_revert.setEnabled(is_dirty and is_ready)
        self._update_title(is_dirty)

    def _on_session_state_changed(self, state: str) -> None:
        self._refresh_toolbar()
        if state == 'error' and self._current_session and not self._current_session.editor.is_dirty():
            QMessageBox.warning(self, 'Save Failed', 'Could not save.')

    def _on_editor_state_changed(self) -> None:
        self._refresh_toolbar()

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
                self._deconstruct_old_session()
                self.back_requested.emit()
            return

        if session.state in ('ready', 'error'):
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
                    def _on_save_success():
                        self._deconstruct_old_session()
                        self.back_requested.emit()
                    session.save_then(
                        on_success=_on_save_success,
                        on_failure=lambda reason: QMessageBox.warning(self, 'Save Failed', reason)
                    )
                    return
                else:
                    editor.discard_changes()

        if session.state == 'saving':
            return
        self._deconstruct_old_session()
        self.back_requested.emit()
    
    def _on_save(self) -> None:
        if self._current_session:
            self._current_session.apply_changes()

    def _on_revert(self) -> None:
        if self._current_session:
            self._current_session.editor.discard_changes()

    def _on_undo(self) -> None:
        if self._current_session:
            self._current_session.editor.undo()

    def _on_redo(self) -> None:
        if self._current_session:
            self._current_session.editor.redo()