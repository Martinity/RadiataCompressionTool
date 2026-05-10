from pathlib import Path
from core.node import VfsNode
from core.contracts import LeafHandler
from core.registry import Registry

@Registry.register(name='File Import/Exporter',is_fallback=True,)
class IOHandler(LeafHandler):
    def __init__(self, raw_bytes: bytes, parent: VfsNode | None = None):
        super().__init__(raw_bytes, parent)

    def export_node(self, raw_node: bytes, output_path: Path, progress) -> str:
        '''Export raw node to file'''
        progress(10, f"Opening {output_path.name}...")
        with open(output_path, 'wb') as f:
            f.write(raw_node)
        progress(100, "Extraction Finished.")
        return f'Node exported to {output_path.name}'

    def import_node(self, import_path: Path, progress) -> bytes:
        '''return the raw bytes of the requested file'''
        progress(10, f'Opening {import_path.name}...')
        with open(import_path, 'rb') as new_node:
            new_data = new_node.read()
        progress(100, 'Import Finished.')
        return new_data

    def execute_action(self, node: VfsNode, action: str, progress_callback, log_callback, **kwargs) -> bytes | str | None:
        if action == 'Export':
            return self.export_node(kwargs['raw_node'], kwargs['file_path'], progress_callback)
        elif action == 'Import':
            return self.import_node(kwargs['file_path'], progress_callback)

    def get_file_tree(self) -> VfsNode:
        return VfsNode(name='io_export')

    def rebuild_node(self, node: VfsNode) -> bytes:
        return b''
    
    def get_raw_node(self, node: VfsNode) -> bytes:
        return b''