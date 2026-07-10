'''LeafHandler for the global actions "Export as Raw Bytes" & "Import and Replace"'''
from __future__ import annotations

from pathlib import Path
from core.node import VfsNode
from core.contracts import LeafHandler
from core.registry import Registry

@Registry.register(name='File Import/Exporter', is_fallback=True,)
class IOHandler(LeafHandler):
    def __init__(self, raw_bytes: bytes, parent: VfsNode | None = None):
        super().__init__(raw_bytes, parent)

    def export_node(self, raw_node: bytes, output_path: Path) -> str:
        '''Export raw node to file'''
        with open(output_path, 'wb') as f:
            f.write(raw_node)
        return f'Node exported to {output_path.name}'

    def import_node(self, import_path: Path) -> bytes:
        '''return the raw bytes of the requested file'''
        with open(import_path, 'rb') as new_node:
            new_data = new_node.read()
        return new_data

    def execute_action(self, node: VfsNode, action_name: str, **kwargs) -> bytes | str | None:
        if action_name == 'Export as Raw Bytes':
            return self.export_node(kwargs['raw_node'], kwargs['file_path'])
        elif action_name == 'Import and Replace':
            return self.import_node(kwargs['file_path'])
