import sys
import os
from pathlib import Path

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