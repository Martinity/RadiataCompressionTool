from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal
from core.contracts import BaseHandler
from pathlib import Path
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.node import VfsNode
    from core.contracts import BaseHandler
import logging
logger = logging.getLogger(f'radiata.{__name__}')

class RebuildWorker(QThread):
    '''Runs the heavy ISO rebuild process off the main GUI thread.'''
    progress_updated = pyqtSignal(int)
    log_message = pyqtSignal(str)
    rebuild_finished = pyqtSignal(bool, str)

    def __init__(self, handler: BaseHandler, root_node: VfsNode, staged_nodes: list[VfsNode], output_path: Path):
        super().__init__()
        self.handler = handler
        self.root_node = root_node
        self.staged_nodes = staged_nodes
        self.output_path = output_path

    def run(self) -> None:
        self.log_message.emit(f"Initializing rebuild to {self.output_path.name}...")
        try:
            self.handler.rebuild_node(self.root_node, self.staged_nodes, self.output_path)
            
            self.progress_updated.emit(100)
            self.rebuild_finished.emit(True, "ISO Rebuild completed successfully.")
        except Exception as e:
            logger.error(f"Rebuild failed: {e}", exc_info=True)
            self.rebuild_finished.emit(False, f"Build Failed: {str(e)}")