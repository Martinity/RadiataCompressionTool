'''Tracks all editor states and transitions to make EditorPage easier to manage/debug'''
from __future__ import annotations

import threading
from typing import Any, Callable, TYPE_CHECKING
if TYPE_CHECKING:
    from core.node import VfsNode
    from core.contracts import BaseEditor

import logging
logger = logging.getLogger(f'radiata.{__name__}')

class EditorSession:
    '''Manages the lifecycle of an editor'''
    _VALID_TRANSITIONS: dict[str, set[str]] = {
        'loading': {'ready', 'error', 'cancelled'},
        'ready':   {'cancelled'},
        'error':   {'cancelled'},
        'cancelled': set()
    }
    _counter_lock = threading.Lock()
    _next_id      = 0

    def __init__(self, node: VfsNode, editor: BaseEditor) -> None:
        with EditorSession._counter_lock:
            self.session_id = EditorSession._next_id
            EditorSession._next_id += 1
        self.node = node
        self.editor = editor
        self._state = 'loading'
        logger.debug(f'EditorSession #{self.session_id} created for "{node.name}"')

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

    def complete(self, data: Any, data_resolver: Callable | None = None) -> None:
        '''Populate the editor with processed data'''
        self._transition('ready')
        self.editor.receive_data(data, data_resolver)

    def fail(self, reason: str) -> None:
        '''Show load error in editor'''
        self._transition('error')
        self.editor.show_load_error(reason)
        logger.error(f'EditorSession #{self.session_id} ("{self.node.name}") failed: {reason}')

    def cancel(self) -> None:
        '''Silently discard any pending data'''
        if self._state == 'cancelled':
            return
        prev = self._state
        self._state = 'cancelled'
        logger.debug(f'EditorSession #{self.session_id} ("{self.node.name}"): {prev}->cancelled')

    def __repr__(self) -> str:
        return f'<EditorSession #{self.session_id} node={self.node.name} state={self._state!r}>'