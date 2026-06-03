'''LeafHandler for returning raw byte stream payloads
If someone really doesn"t want to use the handler background thread processing they __could__
just pass a raw buffer to the editor and have the editor process the data itself. **not recommended**'''
from __future__ import annotations

from typing import Any
from core.contracts import LeafHandler
from core.registry import Registry
from core.node import VfsNode

import logging 
logger = logging.getLogger(f'radiata.{__name__}')

@Registry.register(name='Generic Binary Handler', extensions=())
class GenericBinaryHandler(LeafHandler):
    '''Passthrough Generic Handler used to get raw bytes of node'''
    def __init__(self, source: bytes, parent_node):
        super().__init__(source)

    def prepare_editor_data(self, node: VfsNode, raw_bytes: bytes) -> Any:
        return raw_bytes
    
    def decode_editor_data(self, node: VfsNode, payload: Any, **kwargs) -> bytes:
        if not isinstance(payload, bytes):
            raise TypeError(
                f'GenericBinaryHanlder.decode_editor_data expected bytes, got {type(payload).__name__}'
                f'HexEditorWidget should always emit bytes via apply_requested.'
            )
        return payload