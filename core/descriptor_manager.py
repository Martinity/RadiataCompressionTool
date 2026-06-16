'''Contains all logic for interfacing with descriptor json"s
NodeMeta            - Translates JSON <-> Application
NodeDescriptorStore - Updates node metadata, updates descriptor with new entries
DatacenterTargets   - Links files to datacenter headers'''
from __future__ import annotations

import json
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from PyQt6.QtCore import QTimer, QObject, pyqtSignal

if TYPE_CHECKING:
    from core.node import VfsNode, VfsManager

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###--------------------------------- Metadata/Descriptor --------------------------------------###

@dataclass(frozen=True)
class NodeMeta:
    title:       str
    description: str
    tags:        tuple[str, ...]
    target_hid:  tuple[int, ...] | None = None

    @staticmethod
    def from_dict(d: dict) -> NodeMeta:
        cat = d.get('category', ['Unknown'])
        if isinstance(cat, str):
            cat = [cat] if cat else ['Unknown']
        
        raw_target = d.get('target')
        target: tuple[int, ...] | None = None
        if raw_target:
            try:
                if isinstance(raw_target, list) and raw_target and isinstance(raw_target[0], list):
                    target = tuple(int(i) for i in raw_target[0])
                else:
                    target = tuple(int(i) for i in raw_target)        
            except (TypeError, ValueError) as e:
                logger.debug(f'NodeMeta.from_dict: invalid target {raw_target!r}: {e}')

        return NodeMeta(
            title=d.get('title', ''),
            description=d.get('description', ''),
            tags=tuple(d.get('tags', [])),
            target_hid=target
        )
    
    def to_dict(self) -> dict:
        d: dict = {
            'title':       self.title,
            'description': self.description,
            'tags':        list(self.tags),
        }
        if self.target_hid:
            d['target'] = list(self.target_hid)
        return d

###--------------------------------------- Store ----------------------------------------------###

class NodeDescriptorStore(QObject):
    '''Owns the descriptor database.'''
    SAVE_DEBOUNCE_MS = 2000
    entry_registered = pyqtSignal(str)  # hid str
    entry_updated    = pyqtSignal(str)  # hid str

    def __init__(
        self, 
        json_path: Path, 
        *, 
        auto_save: bool = True, 
        parent:    QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._path      = json_path
        self._auto_save = auto_save
        self._db: dict[str, NodeMeta] = {}
        self._lock = threading.RLock()
        self._dirty = False

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(self.SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._save_to_disk)

    def load(self) -> None:
        '''Parse json into a flat dict[str, NodeMeta]'''
        if not self._path.exists():
            logger.info(f'Descriptor file not found at {self._path} - Starting anew')
            return 
        try:
            raw: dict[str, dict] = json.loads(self._path.read_text(encoding='utf-8'))
            parsed: dict[str, NodeMeta] = {}
            errors = 0
            for hid, entry in raw.items():
                try:
                    parsed[hid] = NodeMeta.from_dict(entry)
                except Exception as e:
                    logger.debug(f'Descriptor parse error for "{hid}": {e}')
                    errors += 1
            with self._lock:
                self._db = parsed
            logger.info(
                f'Loaded {len(parsed)} descriptors from {self._path.name}'
                + (f' ({errors} skipped)' if errors else '') 
            )
        except Exception as e:
            logger.error(f'Failed to load descriptor database: {e}', exc_info=True)
        
    def enrich(self, node: VfsNode) -> None:
        '''Stamp metadata onto a node'''
        with self._lock: # snapshot the db entry
            meta = self._db.get(node.hierarchical_id_str)
        if meta is None: # no entry under id in db. register the id
            self.register(node.hierarchical_id_str)
            return
        if meta.title:
            node.name = meta.title
        if meta.tags and meta.tags != ('Unknown',):
            node.category = meta.tags
        if meta.target_hid:
            node.target = meta.target_hid
            node.extension = '.kods'

    ### Lookup 
    def get(self, hid: str) -> NodeMeta | None:
        with self._lock:
            return self._db.get(hid)
        
    def __contains__(self, hid: str) -> bool:
        with self._lock:
            return hid in self._db
    
    def __len__(self) -> int:
        with self._lock:
            return len(self._db)
        
    ### runtime registration
    def register(
            self, 
            hid: str, 
            *, 
            title:       str | None = None,
            description: str | None = None,
            tags:   list[str]| None = None,
            target: tuple[int, ...]| None = None,
        ) -> None:
        '''Create or update store entries'''
        with self._lock:
            is_new   = hid not in self._db
            existing = self._db.get(hid)
            self._db[hid] = NodeMeta(
                title       = title       if title       else (existing.title       if existing else ''),
                description = description if description else (existing.description if existing else ''),
                tags        = tuple(tags) if tags        else (existing.tags        if existing else ('Unknown',)),
                target_hid  = target      if target      else (existing.target_hid  if existing else None)
            )
            self._dirty = True
        # Update search model
        if is_new:
            logger.debug(f'Descriptor registered: {hid}')
            self.entry_registered.emit(hid)
        else:
            logger.debug(f'Descriptor updated: {hid}')
            self.entry_updated.emit(hid)
        # Trigger save debounce
        if self._auto_save:
            self._save_timer.start(self.SAVE_DEBOUNCE_MS)

    ### Persistence for expansion
    def save(self) -> None:
        '''Current descriptor back to disk'''
        self._save_timer.stop()
        self._save_to_disk()

    def _sort_key(self, kv: tuple[str, Any]) -> tuple:
        parts  = kv[0].split('.')
        sorted = []
        for p in parts:
            try:
                sorted.append(int(p))
            except ValueError:
                logger.warning('Could not cast key to int')
        return tuple(sorted)

    def _save_to_disk(self) -> None:
        logger.debug('Attempting to save updated Descriptors to disk...')
        with self._lock:
            if not self._dirty:
                logger.debug('No new entries.')
                return
            snapshot = {k: v.to_dict() for k, v in self._db.items()}
            self._dirty = False
        try:
            sorted_ss = dict(sorted(snapshot.items(), key=self._sort_key))
            self._path.write_text(
                json.dumps(sorted_ss, indent=2, ensure_ascii=False),
                encoding='utf-8'
            )
            logger.info(f'Descriptor updated - {len(snapshot)} total entries -> {self._path.name}')
        except Exception as e:
            logger. error(f'Failed to save descriptor: {e}', exc_info=True)
            with self._lock:
                self._dirty = True

    def export_template(self, vfs: VfsManager, output_path: Path) -> int:
        with self._lock:
            known = set(self._db.keys())
            snapshot = {k: v.to_dict() for k, v in self._db.items()}
        stubs_added = 0
        for hid, node in vfs.nodes_by_id.items():
            key = node.hierarchical_id_str
            if key in known or node.is_hidden:
                continue
            stub: dict = {
                'title':       node.name,
                'description': '',
                'tags':        list(node.category),
            }
            if node.target:
                stub['target'] = list(node.target)
                snapshot[key] = stub
            stubs_added += 1
        try:
            sorted_ss = dict(sorted(snapshot.items(), key=self._sort_key))
            output_path.write_text(
                json.dumps(sorted_ss, indent=2, ensure_ascii=False),
                encoding='utf-8'
            )
            logger.info(f'Template exprted. {stubs_added} new stub(s), {len(snapshot)} total -> {output_path.name}')
        except Exception as e:
            logger.error(f'Template export failed: {e}', exc_info=True)
        
        return stubs_added

    def migrate_datacenter_targets(self) -> int:
        '''Migrate all known datacenter targets and register them, called once on new target mappings (kept incase tri-ace games)'''
        count = 0

        for disk_idx_str, hid_tuple in DatacenterTargets.to_hid_str_map():
            if disk_idx_str not in self._db:
                self.register(disk_idx_str, target=hid_tuple)
                count += 1
        logger.info(f'Migrate {count} datacenter target entries into descriptor store.')
        return count

###--------------------------------------------- Radiata Datacenter targets --------------------------------------------------------###

class DatacenterTargets:
    '''Kods datacenter targets'''
    _TARGET_STATIC: dict[int, tuple[int, ...]] = { # format: [disk index]:[Datacenter header HIDs]
        186: (5, 0),  204: (5, 1),
        185: (5, 7),  187: (5, 9),
        203: (5, 10), 189: (5, 11), 190: (5, 12),
        191: (5, 13), 192: (5, 14), 193: (5, 15),
        194: (5, 16), 195: (5, 17), 196: (5, 18),
        197: (5, 19), 198: (5, 20), 199: (5, 21),
        200: (5, 22), 201: (5, 23), 206: (5, 25),
        179: (5, 26), 178: (5, 27), 177: (5, 28),
        176: (5, 29), 180: (5, 30), 188: (5, 31)
    }
    _TARGET_MAP: list[tuple[int, int, tuple[int, ...], int]] = [ # format: [Start Idx, End Idx, HID prefix, Number of Header Idxs]
        (1206, 1511, (5, 2, 0), 10),
        (1511, 1682, (5, 3, 0), 10),
        (1688, 1939, (5, 4, 0), 10),
        (1939, 2126, (5, 5, 0), 10),
        (2126, 2426, (5, 6, 0), 10),
    ]
    @classmethod
    def get_target(
        cls, 
        disk_index: int,
        child_index: int | None = None,
        ) -> list[tuple[int,...]] | None:
        '''Return the datacenter header HID(s) for a node HID where,
        disk index is the top level HID value and child_index is 0-8 for entity sections'''
        logger.debug(f'Searching for target for {disk_index}...')
        # Single datacenter header
        if disk_index in cls._TARGET_STATIC:
            if child_index is not None:
                return None
            return [cls._TARGET_STATIC[disk_index]]

        # Entity Pack datacenter headers
        for start, end, prefix, steps in cls._TARGET_MAP:
            if start <= disk_index < end:
                base_val = (disk_index - start) * steps
                if child_index is None:
                    return [prefix + (base_val,)]
                if 0 <= child_index < steps - 1:
                    return [prefix + (base_val + 1 + child_index,)]
                return None
        return None

    @classmethod
    def to_hid_str_map(cls):
        '''Used to export and repopulate descriptor with target_hids
        yields hid string representations mapped to flat integer tuples'''
        for disk_idx, hid in cls._TARGET_STATIC.items():
            yield str(disk_idx), hid
        for start, end, prefix, steps in cls._TARGET_MAP:
            for disk_idx in range(start, end):
                base = (disk_idx - start) * steps
                yield str(disk_idx), prefix + (base,)
                for child_idx in range(steps -1):
                    yield f'{disk_idx}.{child_idx}', prefix + (base + 1 + child_idx,)
