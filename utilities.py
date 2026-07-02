'''Anything that is repeated frequently I have been placing here as a global import utility.
get_resource_path is not repeated frequently but may help for app building in the future so I included it here as a global utility'''
from __future__ import annotations

import sys
from pathlib import Path
from PyQt6.QtWidgets import QFrame

def get_resource_path(relative_path: str | Path) -> Path:
    '''
    Setup for building in the future I have no clue if this will work until the tool grows
    Until then in place just so that the system doesn't need to change
    '''
    if hasattr(sys, '_MEIPASS'): # For future building
        base_path = Path(sys._MEIPASS)
    else: # For running from source
        base_path = Path(__file__).resolve().parent 
    return base_path / relative_path


def human_size(n: int) -> str:
    '''Converts bytes into a human-readable string'''
    if n < 0:
        return 'Invalid Size'
    if n == 0:
        return '0 B'
    value = float(n)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024:
            return f'{value:.1f} {unit}' if unit != 'B' else f'{value} B'
        value /= 1024
    return f'{value:.1f} TB'

def hline() -> QFrame:
    f = QFrame()
    f.setObjectName('HLine')
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f

def vline() -> QFrame:
    f = QFrame()
    f.setObjectName('VLine')
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f

