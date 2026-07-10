'''
Contains all logic for interfacing with metadata json"s
NodeMeta            - Translates JSON <-> Application
NodeMetadataStore   - Updates node metadata, updates metadata with new entries
DatacenterTargets   - Links files to datacenter headers
One possible improvement to search legibility is to remove the metadata that is filled for
removed game files.
'''
from __future__ import annotations

import json
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, Protocol, runtime_checkable
from PyQt6.QtCore import QTimer, QObject, pyqtSignal

from core.name_overrides import generate_name_overrides
if TYPE_CHECKING:
    from core.node import VfsNode

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###--------------------------------- Metadata --------------------------------------###

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

class NodeMetadataStore(QObject):
    '''Owns the metadata database.'''
    SAVE_DEBOUNCE_MS = 2000
    entry_registered = pyqtSignal(str)  # hid str
    entry_updated    = pyqtSignal(str)  # hid str
    bulk_updated     = pyqtSignal(int)  # updated count

    def __init__(
        self, 
        json_path: Path, 
        *, 
        auto_save: bool = False, 
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
            logger.info(f'metadata file not found at {self._path} - Starting anew')
            return 
        try:
            raw: dict[str, dict] = json.loads(self._path.read_text(encoding='utf-8'))
            parsed: dict[str, NodeMeta] = {}
            errors = 0
            for hid, entry in raw.items():
                try:
                    parsed[hid] = NodeMeta.from_dict(entry)
                except Exception as e:
                    logger.debug(f'Metadata parse error for "{hid}": {e}')
                    errors += 1
            with self._lock:
                self._db = parsed
            logger.info(
                f'Loaded {len(parsed)} metadata from {self._path.name}'
                + (f' ({errors} skipped)' if errors else '') 
            )
        except Exception as e:
            logger.error(f'Failed to load metadata database: {e}', exc_info=True)
        
    def enrich(self, node: VfsNode) -> None:
        '''Stamp metadata onto a node'''
        with self._lock: # snapshot the db entry
            meta = self._db.get(node.hierarchical_id_str)
        if meta is None:
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
    def _merge_entry(
        self,
        hid: str,
        *,
        title:       str | None = None,
        description: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
        target: tuple[int, ...] | None = None,
    ) -> None:
        existing = self._db.get(hid)
        if tags is not None:
            existing_tags = existing.tags if existing else()
            merged_tags = tuple(dict.fromkeys((*existing_tags, *tags)))
            if merged_tags != ('Unknown',) and 'Unknown' in merged_tags:
                merged_tags = tuple(t for t in merged_tags if t != 'Unknown')
            new_tags = merged_tags
        else:
            new_tags = existing.tags if existing else ('Unknown',)
        
        self._db[hid] = NodeMeta(
            title       = title       if title       is not None else (existing.title       if existing else ''),
            description = description if description is not None else (existing.description if existing else ''),
            tags        = new_tags,
            target_hid  = target      if target      is not None else (existing.target_hid  if existing else None)
        )

    def register(
            self, 
            hid: str, 
            *, 
            title:       str | None = None,
            description: str | None = None,
            tags:   list[str]| None = None,
            target: tuple[int, ...]| None = None,
        ) -> None:
        '''
        Create entries and update fields
        Update logic for fields:
        title and description   - overwrite 
        tags                    - merge
        target                  - overwrite
        '''
        with self._lock:
            is_new   = hid not in self._db
            self._merge_entry(hid, title=title, description=description, tags=tags, target=target)
            self._dirty = True
        # Update search model
        if is_new:
            self.entry_registered.emit(hid)
        else:
            self.entry_updated.emit(hid)
        # Trigger save debounce
        if self._auto_save:
            self._save_timer.start(self.SAVE_DEBOUNCE_MS)

    def register_many(self, entries: Iterator[tuple[str, dict[str, Any]]]) -> int:
        count = 0
        with self._lock:
            for hid, fields in entries:
                self._merge_entry(
                    hid,
                    title=fields.get('title'),
                    description=fields.get('description'),
                    tags=fields.get('tags'),
                    target=fields.get('target')
                )
                count += 1
            if count:
                self._dirty = True
        if count:
            self.bulk_updated.emit(count)
            if self._auto_save:
                self._save_timer.start(self.SAVE_DEBOUNCE_MS)
        return count

    ### Persistence for expansion
    def save(self) -> None:
        '''Current metadata back to disk'''
        self._save_timer.stop()
        self._save_to_disk()

    def _save_to_disk(self) -> None:
        logger.debug('Attempting to save updated metadata to disk...')
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
            logger.debug(f'Metadata updated - {len(snapshot)} total entries -> {self._path.name}')
        except Exception as e:
            logger. error(f'Failed to save metadata: {e}', exc_info=True)
            with self._lock:
                self._dirty = True

    def _sort_key(self, kv: tuple[str, Any]) -> tuple:
        parts  = kv[0].split('.')
        sorted = []
        for p in parts:
            try:
                sorted.append(int(p))
            except ValueError:
                logger.warning('Could not cast key to int')
        return tuple(sorted)

    def dump_metadata(self, output_path: Path) -> int:
        '''Write the full metadata json to disk'''
        target_path = output_path or self._path
        with self._lock:
            snapshot = {k: v.to_dict() for k, v in self._db.items()}
        sorted_snapshot = dict(sorted(snapshot.items(), key=self._sort_key))
        target_path.write_text(
            json.dumps(sorted_snapshot, indent=2, ensure_ascii=False),
            encoding='utf-8'
        )
        logger.info(f'Dumped {len(sorted_snapshot)} metadata entries -> {target_path}')
        return len(sorted_snapshot)

    ### Metadata building from scratch
    def ingest_datacenter_targets(self) -> int:
        '''Migrate all known datacenter targets and register them. Radiata Specific'''
        def _entries() -> Iterator[tuple[str, dict[str, Any]]]:
            for disk_idx_str, hid_tuple in DatacenterTargets.to_hid_str_map():
                yield disk_idx_str, {'target': hid_tuple}
        return self.register_many(_entries())

    def ingest_metadata(self) -> int:
        '''Run every source's iter_entries() through register_many()'''
        _SOURCES = [
            PhysicalFileCategories, PhysicalFileNames, EntityPackSections, MapSections, IOPModules,
            EventScripts, CharaPortraits, TextureBanks
        ]
        def _all_entries() -> Iterator[tuple[str, dict[str, Any]]]:
            for source in _SOURCES:
                if not hasattr(source, 'iter_entries'):
                    logger.warning(f'{source.__name__} has no iter_entries(). --Skipped--')
                    continue
                yield from source.iter_entries()
        count = self.register_many(_all_entries())
        logger.info(f'Ingested {count} metadata entries from {len(_SOURCES)} source(s).')
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
        '''Used to export and repopulate metadata with target_hids
        yields hid string representations mapped to flat integer tuples'''
        for disk_idx, hid in cls._TARGET_STATIC.items():
            yield str(disk_idx), hid
        for start, end, prefix, steps in cls._TARGET_MAP:
            for disk_idx in range(start, end):
                base = (disk_idx - start) * steps
                yield str(disk_idx), prefix + (base,)
                for child_idx in range(steps -1):
                    yield f'{disk_idx}.{child_idx}', prefix + (base + 1 + child_idx,)

###--------------------------------- Protocol ------------------------------------###

@runtime_checkable
class StaticMetadataSource(Protocol):
    '''Import protocol'''
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]: ...

###------------------------------------ Metadata Imports -----------------------------------------###
'''
Metadata importing classes are resposible for one type of data each
classes hold hid and metadata information
importing happens through iter_entries()
'''
class PhysicalFileCategories:
    _CATEGORIES = {
        range(8, 17):       ('FMV',),
        range(3, 4):        ('TAC', 'Audio',),
        range(42, 176):     ('TAC', 'Audio',),
        range(176, 181):    ('TAC', 'Audio',),
        range(188, 189):    ('TAC', 'Audio',),
        range(184, 188):    ('Script',),
        range(204, 205):    ('Script',),
        range(206, 207):    ('Script',),
        range(4, 5):        ('Texture',),
        range(26, 27):      ('Texture',),
        range(189, 204):    ('Texture',),
        range(205, 206):    ('Texture',),
        range(207, 1206):   ('Map',),
        range(1206, 1511):  ('Character',),
        range(1511, 1688):  ('Monster',),
        range(1688, 1939):  ('Prop',),
        range(1939, 2126):  ('Equipment',),
        range(2126, 2426):  ('VFX',),
        range(2426, 3426):  ('Scene Setup',),
        range(3426, 3629):  ('Animation',),
        range(3730, 4151):  ('Battle Animation',),

        range(0, 3):        ('System',), # boot
        range(5, 8):        ('System',), # datacenter/SO3
        range(18, 26):      ('System',), # stats
        range(27, 42):      ('System',), # core/debug
        range(182, 184):    ('System',), # game
    }
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        for idx_range, category in cls._CATEGORIES.items():
            for disk_idx in idx_range:
                yield f'{disk_idx}', {'tags': category}

class PhysicalFileNames:
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        _NAMES: dict[int, str] = generate_name_overrides()
        for entry in _NAMES.items():
            yield f'{entry[0]}', {'title': entry[1]}

class EntityPackSections:
    _SECTIONS = {
        0: ['Model Data', ('Model',)],
        1: ['Basic Animation Data', ('Animation',)],
        2: ['Secondary Basic Animation Data', ('Animation',)],
        3: ['Battle Data', ('Battle',)],
        4: ['Secondary Battle Data', ('Battle',)],
        5: ['Script Animation Data', ('Script', 'Animation')],
        6: ['Animation Data 6', ('Animation',)],
        7: ['Script Data', ('Script',)],
        8: ['NPC Battle Data', ('Battle',)]
    }
    _RANGE = range(1207, 2425)

    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        for disk_idx in cls._RANGE:
            for child_idx, values in cls._SECTIONS.items():
                yield f'{disk_idx}.{child_idx}', {'title': values[0], 'tags': values[1]}

class MapSections:
    '''Auto-fill map packs with section metadata'''
    _SECTIONS = {
        1: ['Object References', ('System', 'Map')],
        2: ['Script Data', ('Script', 'Map')],
        3: ['Model Data', ('Model', 'Map')],
        4: ['Animation Data', ('Animation', 'Map')],
        5: ['Terrain Data', ('Terrain', 'Map')],
        6: ['Message Data', ('Message', 'Map')],
        8: ['Scene Graph', ('Scene Graph', 'Map')],
        9: ['Music Data', ('Audio', 'Map')]
    }
    _RANGE = range(207, 1206)
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        for disk_idx in cls._RANGE:
            for child_idx, values in cls._SECTIONS.items():
                yield f'{disk_idx}.{child_idx}', {'title': values[0], 'tags': values[1]}

class IOPModules:
    _SECTIONS = {
        0: ['sio2man', 'Manager Interface for joypads, multitaps and memory cards.',],
        1: ['sio1d', 'Interface for joypads, multitaps and memory cards.',],
        2: ['dbcman', 'Device Control Library (used by libpad2 and libmc2)',],
        3: ['ds1o_d', '',],
        4: ['libsd', 'Sound Library',],
        5: ['csm', '',],
        6: ['csd', '',],
        7: ['csi', '',],
        8: ['hdd', 'Hard Disk Drive',],
        9: ['pfs', 'Playstation File System',],
        10: ['mcman', 'MCMAN is the memory card manager',],
        11: ['mcserv', 'MCSERV is the memory card server. This provides the RPC interface to MCMAN',]
    }
    _RANGE = range(1, 2)
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        for disk_idx in cls._RANGE:
            for child_idx, values in cls._SECTIONS.items():
                fields: dict[str, Any] = {'title': values[0]}
                if len(values) > 1 and values[1]:
                    fields['description'] = values[1]
                fields['tags'] = ('System', 'IOP')
                yield f'{disk_idx}.{child_idx}', fields

class EventScripts:
    _EVENTS = {
        '400':    ['Goblin Trio @ Earth Valley'],
        '400.1':  ['Goblin Trio at the Pub'],
        '401':    ['Clive @ Theatre Vancoor'],
        '401.1':  ["Introduction to friends list and Clive's recruitment"],
        '402':    ['Fayt Armor'],
        '402.1':  ["Enter Ridley's room after becoming a knight again"],
        '403':    ['Goblin Cemetary Mission (Theatre Vancoor)'],
        '403.1':  ['Goblin Encyclopedia @ Vareth'],
        '403.2':  ['Before boss battle'],
        '403.3':  ['Arrive at Goblin Cemetary Entrance'],
        '403.4':  ['After boss battle'],
        '403.5':  ['After obtaining Recruitment Suit'],
        '404':   ['Algandars Castle (Theatre Vancoor - Human Path)'],
        '404.1': ['First meeting at Algandars Castle entrance'],
        '404.2': ['Before boss battle'],
        '404.3': ['Meeting after boss battle at entrance'],
        '404.4': ['After boss battle'],
        '405':   ['Creatures of the Sewer'],
        '405.1': ['Pre battle'],
        '405.2': ['Entering Sewer'],
        '405.3': ['Entering room prior to boss'],
        '405.4': ['Post Battle'],
        '406':   ['Chains of Fate (Unused Version)'],
        '406.1': ['Talking to Nocturne'],
        '406.2': ['Talking to Gerald'],
        '407':   ['Fireworks'],
        '407.1': ['Getting the letter'],
        '407.2': ['Fireworks event'],
        '408':   ['Stone of Miracles'],
        '408.1': ['Pre Battle'],
        '408.2': ['Post Battle'],
        '408.3': ['Kain telling Jack where to find'],
        '408.4': ['Bringing Stone to Kain'],
        '409':   ['Please Stop Lord Star'],
        '409.1': ['Pre Battle'],
        '409.2': ['Post Battle'],
        '410':   ['The Ultimate Battle'],
        '410.1': ['Pre Battle'],
        '410.2': ['Post Battle'],
        '410.3': ['Elwen telling Jack to do mission'],
        '411':   ['The Real Ultimate Battle'],
        '411.1': ['Pre Battle'],
        '411.2': ['Post Battle'],
        '412':   ["Gonovitch's Dilemma"],
        '412.1': ['Pre Battle'],
        '412.2': ['Post Battle'],
        '412.3': ['Gonovitch Pre'],
        '412.4': ['Gonovitch Post'],
        '413':   ['Earth Dragon Encounter at Dwarf Tunnel (Unused)'],
        '413.1': ['First time entering room'],
        '413.2': ['Pre Battle'],
        '413.3': ['Post Battle'],
        '414':   ["Hecton Squad's Lunch Plans"],
        '414.1': ['Start'],
        '414.2': ['Win Battle'],
        '414.3': ['Lose Battle'],
        '415':   ['Encounter with Leona at Vareth'],
        '416':   ['Post-Game Dungeon'],
        '416.1': ['First time entering DLC'],
        '416.2': ['Pre Battle - Baade (Earth Dragon)'],
        '416.3': ['Post Battle - Baade'],
        '416.4': ['Pre Battle - Kelvin (Water Dragon)'],
        '416.5': ['Post Battle - Kelvin'],
        '416.6': ['Pre Battle - Parsec (Fire Dragon)'],
        '416.7': ['Post Battle - Parsec'],
        '416.8': ['Pre Battle - Cepheid (Wind Dragon)'],
        '416.9': ['Post Battle - Cepheid'],
        '416.10': ['Unlock Radian'],
        '416.11': ['Pre Battle - Radian'],
        '416.12': ['Post Battle - Radian'],
        '416.13': ['First time entering Distortion Corridor / unlocking scene'],
        '416.14': ['Pre Battle - Cairn'],
        '416.15': ['Post Battle - Cairn'],
        '416.16': ['Pre Battle - Valkyrie'],
        '416.17': ['Post Battle - Valkyrie'],
        '416.18': ['Pre Battle - Quasar'],
        '416.19': ['Post Battle - Quasar'],
        '416.20': ['Wall Text'],
        '416.21': ['Pre Battle - Lezard'],
        '416.22': ['Post Battle - Lezard'],
        '416.23': ['Pre Battle - Gabriel Celesta'],
        '416.24': ['Post Battle - Gabriel Celesta'],
        '416.25': ['Pre Battle - Ethereal Queen'],
        '416.26': ['Post Battle - Ethereal Queen'],
        '416.27': ['Wind Dragon - Phase 2'],
        '416.28': ['Teleport from DLC to Distortion'],
        '417':   ['Gawain gives Jack the Arbitrator'],
        '417.1': ['Pre Battle'],
        '417.2': ['Post Battle'],
        '418':   ['Elwen gives Jack the Arbitrator (Unused)'],
        '419':   ["Leonard's Letter (Knights)"],
        '420':   ['Goblin Cemetery Mission (Non-Human Path)'],
        '421':   ['The Best Liquor (Liquor Fetch Quest - Non-Human Path)'],
        '422':   ['Letter of Defiance'],
        '423':   ['A Masterpiece of Fantasy'],
        '424':   ['Algandars Castle (Non-Human)'],
        '425':   ['Beasts by the Bridge'],
        '426': ["Practicing Business (Keane and Marsha's Shop Event)"],
        '427': ['Build that Body'],
        '428': ['Top Secret Mission'],
        '429': ['Shining Ore'],
        '430': ['J.J. and Galvados Event'],
        '431': ["Elven Wine for Ganz"],
        '432': ['Chains of Fate'],
        '433': ['The Strongest Elf (Franz & Gil)'],
        '434': ['Journey Pig Introduction'],
        '435':   ['Jack vs Hecton Squad (Non-Human)'],
        '435.1': ['Start'],
        '435.2': ['Win Battle'],
        '435.3': ['Lose Battle'],
        '436':   ['Jack vs Elwen (Non-Human)'],
        '436.1': ['Start'],
        '436.2': ['Win Battle'],
        '436.3': ['Lose Battle'],
        '437':   ['Jack vs Gerald (Non-Human)'],
        '437.1': ['Start'],
        '437.2': ['Win Battle'],
        '437.3': ['Lose Battle'],
        '500':   ['Start of game > Departure for first mission'],
        '500.1': ['Starting FMV / Coliseum Waiting Room'],
        '500.2': ['Pre Ridley Battle'],
        '500.3': ['Post Ridley Battle'],
        '500.4': ["Al showing Jack his room"],
        '500.5': ['Jack gets Trainee Wear'],
        '500.6': ['Knocking on Door to Meeting Room'],
        '500.7': ['Rose Cochon Inauguration Ceremony'],
        '500.8': ['Rose Cochon leaves for First Mission'],
        '500.9': ['Jasne talking to Natalie'],
        '500.10': ['Rose Cochon meets Clive'],
        '500.11': ['Movement Restriction Message'],
        '500.12': ['Movement Restriction Message'],
        '500.13': ['Adele and Jack - First cutscene'],
        '500.14': ['Adele and Jack Training'],
        '500.15': ['Adele gives Jack the Arbitrator'],
        '500.16': ['Jack walking to Radiata'],
        '500.17': ['Jack at Radiata Castle'],
        '500.18': ['Save Flag Tutorial'],
        '501':   ['First Mission (Outside Lupus Gate) > End of First Mission'],
        '501.1': ['Ganz: "Now then. Here are the details of our mission."'],
        '501.2': ['Jack: "Far out! Dwarves live in a crazy place like this?"'],
        '501.3': ['Jack: "Open up!"'],
        '501.4': ['Gonovitch: "So you are here instead of the Violet Chevre."'],
        '501.5': ['Ganz: "It seems that the goods are ready. Let\'s head to the top of the cliff."'],
        '501.6': ['Goblin Trio - Pre Battle'],
        '501.7': ['Goblin Trio - Post Battle'],
        '501.8': ['Ganz: "Ah, at last. Radiata Castle."'],
        '501.9': ['Ganz: "Brigade, halt!"'],
        '501.10': ['Larks: "I\'m glad you\'ve returned safely."'],
        '501.11': ['Movement Restriction Message'],
        '501.12': ['Movement Restriction Message'],
        '501.13': ['Movement Restriction Message'],
        '501.14': ['Movement Restriction Message'],
        '501.15': ['Movement Restriction Message'],
        '501.16': ['Ganz: "We are the Rose Cochon brigade. We are here to escort the trade goods in place of the Violet Chevre.'],
        '501.17': ['Movement Restriction Message'],
        '501.18': ['Movement Restriction Message'],
        '501.19': ['Movement Restriction Message'],
        '501.20': ['Movement Restriction Message'],
        '501.21': ['Gonovitch: "Come in! I\'m on the second floor."'],
        '501.22': ['Jack: "So, what do we do now that we\'ve finished the mission?"'],
        '501.23': ['Ganz: "Captain Ganz Rothschild and the Rose Cochon brigade reporting, sir!"'],
        '501.24': ['Ganz: "As Lord Larks said, it is very important that knights rest in preparation for their next mission."'],
        '502':   ['Second Mission (Knights)'],
        '502.1': ['Al tells Jack of emergency summons'],
        '502.2': ['Jack at meeting room'],
        '502.3': ['Fort Helencia'],
        '502.4': ['Natalie and Leonard outside Fort'],
        '502.5': ['Meeting Genius'],
        '502.6': ['Nogueira kills Blood Orc'],
        '502.7': ['First Arrival at City of Flowers'],
        '502.8': ['Jack praises Ganz'],
        '502.9': ['Nowem Region Appreciation'],
        '502.10': ['Arrival at Forest Metropolis'],
        '502.11': ['Meeting Lord Nogueira'],
        '502.12': ['Movement Restriction'],
        '502.13': ['Finding out about Blood Orc'],
        '502.14': ['Blood Orc'],
        '502.15': ['Transpiritation'],
        '502.16': ['Rose Cochon arrives at Castle Gate (Unused)'],
        '502.17': ['Rose Cochon and Jasne'],
        '502.18': ['Movement Restriction'],
        '502.19': ['Movement Restriction'],
        '502.20': ['Movement Restriction'],
        '502.21': ['Movement Restriction'],
        '502.22': ['Movement Restriction'],
        '502.23': ['Movement Restriction'],
        '502.24': ['Movement Restriction'],
        '502.25': ['Movement Restriction'],
        '502.26': ['Movement Restriction'],
        '502.27': ['Movement Restriction'],
        '502.28': ['Meeting Rocky (1) (Unused)'],
        '502.29': ['Meeting Rocky (2) (Unused)'],
        '502.30': ['After first Blood Orc Battle'],
        '503':   ['Radiata Castle Dungeon Event'],
        '504':   ["Lucian's Scheme (Unused)"],
        '505':   ['Rose Cochon Discharged'],
        '506':   ["Carl's Pub > Jack becoming Corporal"],
        '507':   ['Vancoor Square (Sheila Event)'],
        '508':   ['Crocogator Mission'],
        '509':   ['Smilodon Fang'],
        '510':   ['Vexatious Vermin'],
        '511':   ['Parsec at Vancoor Square (Unused)'],
        '512':   ["Ridley's Birthday, Graveyard of the Elves"],
        '513':   ["Second Hecton Squad Mission > Jack becomes a Sergeant"],
        '514':   ["Knight's discussing Dwarves' demands > Cross' Invasion of Earth Valley"],
        '515':   ['Donovitch appears > Earth Dragon'],
        '516':   ["Path Split (Ridley visits Jack)"],
        '516.1': ['Updates Characters with new Items (Shops)'],
        '554':   ["Lucian and Jasne get Rose Cochon Discharged"],
        '561':   ['Parsec at Vancoor Square'],
        '600':   ['Meeting at the Castle'],
        '601':   ['Wind Valley (Wind Dragon) > Gawain'],
        '602':   ['Parsec > Fire Mountain'],
        '603':   ["Lucian's Paintings"],
        '604':   ['Ridley visits Jack'],
        '605':   ["Ganz's Letter > Castle Jailbreak"],
        '606':   ['Gold Dragon at Lupus Gate > Zane'],
        '650':   ['Castle Meeting'],
        '651':   ['Wind Valley (Wind Dragon)'],
        '652':   ['Gawain at Fort Helencia'],
        '653':   ['Dynas makes Jack a Knight Captain'],
        '654':   ['Fire Dragon at Faucon Gate > Fire Mountain'],
        '655':   ['Secrets of the Sewers'],
        '656':   ["Ganz's Letter"],
        '657':   ['Ridley visits Jack'],
        '658':   ['Battle at Lupus Gate'],
        '659':   ['VS Gawain'],
        '660':   ['Gold Dragon Castle'],
        '700':   ['Jack and Ridley leave for City of Flowers'],
        '701':   ['Taking over Fort Helencia'],
        '702':   ['Parsec'],
        '703':   ['Goblin Haven'],
        '704':   ['Parsec (Fire Mountain)'],
        '705':   ["Lucian's Paintings"],
        '706':   ['Cross Attacks Fort Helencia'],
        '707':   ["Ridley's Mind"],
        '708':   ['Ridley Becomes the Gold Dragon'],
        '750':   ['Jack and Ridley go to the City of Flowers'],
        '751':   ['Capturing Fort Helencia'],
        '752':   ['Meeting with Parsec'],
        '753':   ['Goblin Haven'],
        '754':   ["Ridley's Illness"],
        '755':   ['Parsec (Fire Mountain)'],
        '756':   ['Ganz rescues Adele'],
        '757':   ['Cross Attacks the Fort'],
        '758':   ['Ressan Tree'],
        '759':   ['Jack heads to the End of the World'],
        '760':   ['Gold Dragon Castle'],
        '800':   ['Misc'],
        '800.1': ['Midnight Transition Animation'],
        '800.2': ['Starting Data for Characters (Run on New Game start)'],
        '800.3': ['Training Dummy'],
        '800.4': ['Journey Pig Statue'],
        '800.5': ['Save Flag'],
        '800.6': ['Add Missions (Debug Room)'],
        '800.7': ['Run after a training dummy mission (updates reward if won)'],
        '801':   ['Knights Invade Earth Valley FMV (Game Engine)'],
        '802':   ['Debug Room'],
        '803':   ['Transpiritation FMV (Game Engine)'],
        '804':   ['Earth Dragon FMV (Game Engine)'],
        '805':   ['Post-Battle Script'],
        '805.1': ['This is usually run after a kick battle'],
        '806':   ['Silver Dragon FMV (Game Engine)'],
        '807':   ['Movement Restriction Messages'],
        '808':   ['Completed Save Data'],
        '808.1': ['Prompt to save after beating the game'],
        '809':   ['Movement Restriction Messages'],
        '810':   ['Movement Restriction Messages'],
        '811':   ['Tokyo Game Show 2004 / E3 2005 Demo'],
    }
    _RANGE = range(186, 187)
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        for disk_idx in cls._RANGE:
            for sub_idx, values in cls._EVENTS.items():
                yield f'{disk_idx}.{sub_idx}', {'title': values[0], 'tags': ('Script',)}

class CharaPortraits:
    '''Sequential list of all the names for portraits
    Loops twice first time adds the "compressed" suffix second time is basic'''
    _ICONS = [
        'Placeholder Icon', 'Jack Icon', 'Ganz Icon', 'Ridley Icon','Rynka Icon', 'Flau Icon',
        'Star', 'Sebastian', 'Genius', 'Rocky', 'Gawain', 'Heavy Guardsman', 'Elwen', 'Gerald',
        'Caesar', 'Alicia', 'Dennis', 'Gareth', 'Gregory', 'Walter', 'Jarvis', 'Light Guardsman',
        'Aldo', 'Gordon', 'Bruce', 'David', 'Conrad', 'Rolec', 'Daniel', 'Carlos', 'Gene', 
        'Light Guardsman', 'Thanos', 'Curtis', 'Cecil', 'Morgan', 'Felix', 'Jill', 'Ursula', 
        'Derek', 'Christoph', 'Claudia', 'Ardoph', 'Dimitri', 'Aidan', 'Cornelia', 'Faraus', 
        'Marietta', 'Ernest', 'Franklin', 'Johan', 'Roche', 'Light Guardsman', 'Kain', 'Fernando', 
        'Anastasia', 'Dwight', 'Godwin', 'Achilles', 'Flora', 'Elena', 'Alvin', 'Vitas', 'Cosmo', 
        'Grant', 'Adina', 'Miranda', 'Edgar', 'Clive', 'Lulu', 'Eugene', 'Nyx', 'Ortoroz', 'Sonata', 
        'Iris', 'Nocturne', 'Herz', 'Alba', 'Lily', 'Jared', 'Pinky', 'Interlude', 'Solo', 'Joaquel', 
        'Eon', 'Elmo', 'Jiorus', 'Sarasenia', 'Belflower', 'Jasne', 'Larks', 'Sakurazaki', 'Junzaburo', 
        'Natalie', 'Nina', 'Charlie', 'Leonard', 'Light Guardsman', 'Heavy Guardsman', 'Raymond', 'Al',
        'Margaret', 'Zion', 'Paul', 'Toma', 'Torenia', 'Testa', 'Nuse', 'Jorn', 'Barbena', 'Giske', 
        'Yuri', 'Warc', 'Robin', 'Sheila', 'Jasmine', 'Camuse', 'Lantana', 'Lyle', 'Rose', 'Josef', 
        'Virginia', 'Morfinn', 'Bligh', 'Freija', 'Nask', 'Cherie', 'Zeke', 'Dan', 'Servia', 'Lunbar', 
        'Sonia', 'Startis', 'Brood', 'Garbella', 'Silvia', 'Thyme', 'Elef', 'Ryan', 'Hip', 'Nick', 'Kira',
        'Rabi', 'Golye', 'Butch', 'Sarval', 'Sunset', 'Sora', 'Keaton', 'Tarkin', 'Gonber', 'Leban', 'Mook', 
        'Wal', 'Wyze', 'Zeranium', '', 'Pommelie', 'Saron', 'Cepheid', 'Baade', 'Quasar', 'Aphelion', 
        'Gonovitch', 'Albert', 'Vladimir', 'Yevgeni', 'Oleg', 'Grigory', 'Brockle', 'Dyvad', 'Gehrmann', 
        'Sergei', 'Naom', 'Aegenhart', 'Marke', 'Donovitch', 'Zane', 'Hap', 'Gil', 'Shin', 'Fan', 'Row', 
        'Pitt', 'Few', 'Alan', 'Keane', 'Nogueira', 'Clarence', 'Serva', 'Hyann', 'Chatt', 'Zida', 'Franz', 
        'Romaria', 'Marsha', 'Lufa', 'Coco', 'Martinez', 'Santos', 'Rika', 'Mikey', 'Gob', 'Lin', 'Brie', 
        'Gonn', 'Golly', 'Gobrey', 'Den', 'Ben', 'Aesop', 'Monki', 'Gabe', 'Mason', 'Goo', 'Donkey', 
        'Ricky', 'Drew', 'Gruel', 'Doppio', 'Pietro', 'Jan', 'Marco', 'Niko', 'Danny', 'Dominic', 'Bosso', 
        'Georgio', 'Luka', 'Sonny', 'Giovanni', 'Polpo', 'Jj', 'Leona', 'Leann', 'Ray C Ross', 'Pinta', 
        'Buta', 'Valkyrie', 'Lezard', 'Radian', 'Ethereal Queen', 'Cairn', 'Kelvin', 'Gabriel Celesta', 
        '', '', 'Galvados', '', '', '', '', '', 'Drago', 'Bull', '', '', '', 
        '', 'Library', 'Phonograph', 'Jack Bookshelf', 'Cross', 'Stein', 'Blackjack', 'Event Watcher', 
        'Parsec', 'Light Guardsman', 'Light Guardsman', 'Light Guardsman', 'Heavy Guardsman', 
        'Heavy Guardsman', 'Heavy Guardsman', 'Heavy Guardsman', 'Heavy Guardsman', 'Heavy Guardsman', 
        'Heavy Guardsman', 'Heavy Guardsman', 'Heavy Guardsman', 'Cody', 'Adele', 'Howard', 'Ravil',
        'Astor', 'Maddock', 'Synelia', 'Tony', 'Patrick', 'Putt', 'Reynos', 'Gobblehope Ix', 'Nalshay', 
        'Sayna', 'Bran', 'Stefan', 'Mint', 'Daria', 'Yack', 'Lauren', 'Theresa', 'Garcia', 'Dynas', 'Epoch',
        'Roy', 'Louis',
    ]
    _THOUSANDS = [
        'Jack Handmade Tunic', 'Jack Trainee\'s Wear', 'Jack Leather Armor', 'Jack Sharkskin',
        'Jack Iron Breastplate', 'Jack Wooden Breastplate', 'Jack Wind Garb', 'Jack Divine Coat',
        'Jack Alfestrain', 'Jack Scale Armor', 'Jack Dragon Scale', 'Jack Iron Plate', 'Jack Plate Armour', 
        'Jack Ore Armour', 'Jack Valiant Mail', 'Jack Demon Mail', 'Jack Samurai Armour', 
        'Jack Absolute Guard', 'Jack Fayt Armour', 'Jack Robot Suit', 'Jack Recruitment Suit', 
        'Ganz Second', 'Ridely Second', 'Ridely Third', 'Adele Second', 'Ridely Fourth'
    ]
    _RANGE_ICONS = 205
    _RANGE_BANK03 = 192
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        # Fill 205 with metadata
        disk_idx = cls._RANGE_ICONS
        for child_idx, name in enumerate(cls._ICONS):
            yield f'{disk_idx}.{child_idx}', {'title': f'{name} Icon (Compressed)', 'tags': ('Texture',)}
        for child_idx, name in enumerate(cls._ICONS):
            yield f'{disk_idx}.{child_idx}.0', {'title': f'{name} Icon', 'tags': ('Texture', 'FIS')}
        for child_idx, name in enumerate(cls._THOUSANDS):
            yield f'{disk_idx}.{child_idx+1000}', {'title': f'{name} Icon (Compressed)', 'tags': ('Texture',)}
        for child_idx, name in enumerate(cls._THOUSANDS):
            yield f'{disk_idx}.{child_idx+1000}.0', {'title': f'{name} Icon', 'tags': ('Texture', 'FIS')}
        # Fill Texture Bank 03 with metadata
        disk_idx = cls._RANGE_BANK03
        for child_idx, name in enumerate(cls._ICONS):
            yield f'{disk_idx}.{child_idx}', {'title': f'{name} Portrait (Compressed)', 'tags': ('Texture',)}
        for child_idx, name in enumerate(cls._ICONS):
            yield f'{disk_idx}.{child_idx}.0', {
                'title': f'{name} Portrait', 
                'tags': ('Texture', 'FIS'), 
                'description': f'Friends book portrait for {name}.'
            }

class TextureBanks:
    _BANK00 = {
        0: {'title': 'Thought Bubbles', 'description': 'Channel packed.'},
        3: {'title': 'Clock'},
        9: {'title': 'Bear Icons'},
        10: {'title': 'Demo Finished Screen 1', 'description': 'First part of the "Hope you enjoyed playing" demo screen.'},
        11: {'title': 'Demo Finished Screen 2', 'description': 'Second part of the "Hope you enjoyed playing" demo screen.'},
        12: {'title': 'Demo Finished Screen 3', 'description': 'Third part of the "Hope you enjoyed playing" demo screen.'},
        20: {'title': 'Horizontal Menu Background 1', 'description': 'The background page-like texture underlay for menus.'},
        21: {'title': 'Vertical Menu Background 1', 'description': 'The background page-like texture underlay for menus.'},
        22: {'title': 'Horizonztal Menu Background 2', 'description': 'The background page-like texture underlay for menus.'},
        23: {'title': 'Vertical Menu Background 2', 'description': 'The background page-like texture underlay for menus.'},
        24: {'title': 'Save Menu & Cursor', 'description': 'Save menu elements and cursor.'},
        25: {'title': 'Totem Icon', 'description': 'Totem Icon as well as unknown japanese text.'},
        26: {'title': 'Unknown Japanese prompt', 'description': 'Contains an unknown japanese menu elements.'},
        27: {'title': 'Jack, Ganz, Ridely Cute Icons', 'description': 'Pack of cute icons for Jack, Ganz, and Ridely.'},
        30: {'title': 'Square Menu Background', 'description': 'The background page-like texture underlay for menus.'},
        31: {'title': 'Menu Texture 1',},
        32: {'title': 'Menu Texture 2',},
        33: {'title': 'Menu Texture 3',},
        34: {'title': 'Menu Texture 4',},
        35: {'title': 'Texture Elements', 'description': 'Icons that get displayed with "texture".'},
        36: {'title': 'Icon Pack 1', 'description': 'Icons for things like markers, arrow, shops.'},
        37: {'title': 'Icon Pack 2', 'description': 'Only two icons: knight and boot.'},
        40: {'title': 'Lootery Menu', 'description': 'Menu elements for the lootery.'},
        41: {'title': 'Training Dummy Icon', 'description': 'Training dummy, checkmark, and unknown dots.'},
        42: {'title': 'Frame Outlines', 'description': 'Frame outlines and some background texture data.'},
        43: {'title': 'Friend Book Background 1', 'description': 'First part of the friend book background.'},
        44: {'title': 'Friend Book Background 2', 'description': 'Second part of the friend book background.'},
        45: {'title': 'Friend Book Background 3', 'description': 'Third part of the friend book background.'},
        46: {'title': 'Friend Book Background Overlay', 'description': 'Overlay Texture for the friend book background.'},
        47: {'title': 'Friend Book Menu Elements', 'description': 'Contains icons, characters and menu elements.'},
        50: {'title': 'Complete Map Menu 1', 'description': 'First part of the map menu in a fully-unlocked state.'},
        51: {'title': 'Complete Map Menu 2', 'description': 'Second part of the map menu in a fully-unlocked state. Also contains Icons.'},
        52: {'title': 'Complete Map Menu 3', 'description': 'Third part of the map menu in a fully-unlocked state. Also contains Icons.'},
        53: {'title': 'Empty Map Menu 1', 'description': 'First part of the map menu in a non-unlocked state.'},
        54: {'title': 'Empty Map Menu 2', 'description': 'Second part of the map menu in a non-unlocked state. Also contains Icons.'},
        55: {'title': 'Empty Map Menu 3', 'description': 'Third part of the map menu in a non-unlocked state. Also contains Icons.'},
        56: {'title': 'Radiata Castle Map 1', 'description': 'Textures for the radiata castle floor layout.'},
        57: {'title': 'Radiata Castle Map 2', 'description': 'Textures for the radiata castle floor layout.'},
        58: {'title': 'Floor Layout Icons', 'description': 'Icons for floor layout menus.'}
    }
    _RANGE00 = 189
    _BANK01 = {
        1: {'title': 'Overlay', 'description': 'Overlay icons, and frame textures.'},
    }
    _RANGE01 = 190
    _BANK02 = [
        "Not Implemented Placeholder", "Not Implemented Attack", "Not Implemented Volty", "Not Implemented Message", "Not Implemented File",
        "Music Disk", "Herb Extract", "Moon Stone", "Cure Needle", "Eye Drops", "Bell Amulet", "Heating Tablet", 
        "Mint Drop", "Recovery Pills", "Toadstool Powder", "Book of _", "Strength Berry", "Not Implemented Apple", 
        "Defense Berry", "Evasion Berry", "Luck Berry", "Life Berry", "Mystery Berry", "Growth Stone", 
        "Not Implemented Bug", "Not Implemented Bread", "Not Implemented Flower", "Not Implemented Flask", 
        "Not Implemented Bottle", "Not Implemented Meat", "Not Implemented Fish", 'Not Implemented Bowl',
        "Not Implemented Cutlery", "Not Implemented Silhouette", "Not Implemented Mushroom 1", "Not Implemented Mushroom 2", 
        "Not Implemented Mineral", "Not Implemented Cards", "Not Implemented Book", "Not Implemented Scarf", 
        "Not Implemented Gem", "Not Implemented Tooth", "Not Implemented Feather", "Not Implemented Stone", 
        "Not Implemented Egg", "Not Implemented Crystal", "Not Implemented Bone", "Not Implemented Root", 
        "Sage", "Not Implemented Pollen", "Power Bangle", "Warrior Bangle", "Not Implemented Accessory 1", 
        "Not Implemented Bangle 2", "Protect Shell", "Monk Bangle", "Skill Upper", "Thief Bangle", 
        "Luck Bracelet", "Lucky Charm", "Toughness Bangle", "Life Bangle", "Not Implemented Accessory 3", 
        'Not Implemented Accessory 4', "Not Implemented Accessory 5", "Not Implemented Accessory 6", 
        "Not Implemented Accessory 7", "Not Implemented Accessory 8", "Not Implemented Accessory 9", 
        "Not Implemented Accessory 10", "Not Implemented Accessory 11", "Not Implemented Accessory 12", 
        "Not Implemented Accessory 13", "Not Implemented Accessory 14", "Not Implemented Accessory 15", 
        "Not Implemented Accessory 16", "Not Implemented Accessory 17", "Not Implemented Accessory 18", 
        "Not Implemented Accessory 19", "Not Implemented Accessory 20", "Not Implemented Accessory 21", 
        "Not Implemented Accessory 22", "Not Implemented Accessory 23", "Not Implemented Accessory 24", 
        "Not Implemented Accessory 25", "Not Implemented Accessory 26", "Not Implemented Accessory 27", 
        "Not Implemented Accessory 28", "Not Implemented Accessory 29", "Eagle Crest", "Lion Crest", "Elephant Crest", "Serpent Crest", 
        "Not Implemented Accessory 30", "Feather Earring", "Not Implemented Accessory 31", "Not Implemented Accessory 32", 
        "Divine Earring", "Hermit's Trophy", "Saint's Trophy", "Pluto's Trophy", "Beckoning Cat", "Not Implemented Accessory 33", 
        "Not Implemented Accessory 34", "Not Implemented Accessory 35", "Not Implemented Accessory 36", 
        "Power Stone", "Not Implemented Accessory 37", "Not Implemented Accessory 38", "Not Implemented Accessory 39", 
        "Not Implemented Accessory 40", "Not Implemented Accessory 41", "Not Implemented Accessory 42", 
        "Not Implemented Accessory 43", "Not Implemented Accessory 44", "Training Device", 'VIP Badge',
        'Not Implemented Accessory 45', 'Not Implemented Accessory 46', 'Not Implemented Accessory 47',
        'Leprechaun', 'Magic Mirror', 'Not Implemented Accessory 48', 'Magic Boost', 'Unknown Cross Trinket',
        'Unknown Trophy', "Iron Edge", "Steel Blade", "Knight Edge", "Glory Edge", "Avcoor*", "Jinn", 
        "Murasame*", "Kotetsu", "Basilisktos", "Evil Blade", "Hatred Edge", "Phantom Edge", "Spark Edge*", 
        "Flame Blade", "Aqua Blade", "Icicle Edge*", "Air Blade", "Breeze Edge*", "Lightning Edge*", 
        "Storm Bringer", "Iron Sword", "Steel Saber", "Knight Saber", "Glory Sword", "Holy Sword*", 
        "Falvern", "Efreet", "Muramasa", "Bizenosafune", "Rune Saber", "Curse Sword*", "Brain Breaker*", 
        "Bind Saber", "Heat Saber", "Flame Sword*", "Lævateinn*", "Blaze Saber", "Grand Saber", "Venom Sword", 
        "Cyclone Sword*", "Fake Gram", "Iron Axe", "Steel Axe", "Knight Axe", "Glory Axe", "Ancient Axe", 
        "Behemoth", "Death Scythe*", "Hard Chopper*", "Bind Smasher*", "Confuse Axe*", "Fall Smasher", 
        "Mist Axe*", "Spark Chopper", "Flame Axe*", "Aqua Chopper", "Icicle Axe", "Mad Axe*", "Rock Axe", 
        "Grand Smasher", "Earth Chopper", "Iron Spear", "Steel Pike", "Knight Spear", "Paradigm", "Leviathan", 
        "Gungnir*", "Medusa Spear", "Curse Lance", "Brain Shooter*", "Binding Spear", "Duster Pike*", 
        "Mad Spear*", "Grand Pike", "Water Pike", "Aqua Spear", "Deep Lance", "Unknown Spear", "Wind Spear*", 
        "Brionac","Oratorio", "Requiem", "Sylph Edge", 
        "Psycho Edge", "Floating Sword", "Vettea", "Dunvera", "Vaise", "Arabum", "President Blade", 
        "Toadstool Blade", "Ganz Sword", 
        "Bloody Grip", "Fathmil", "Damascus Blade", "E. Toadstool Sword", "Love Me True", "Blaze Axe", "Heavy Rain", "Bear Smasher", "Toadstool Axe", 
        "Titan Pike", "Storm Spear", "Cracked Spear", 
        "Toadstool Lance", "Abyss", "Ares Salute", "Adventia", "Entier", "Aldore", "Curozide", "Windmill", 
        "Arshaja", "Wellness", "Atmis", "Vipole", "Naruth", "Vatirork", "Asteka", 
        "Vathao", "Agroth", "Villhe", "Anviteo", "Suolo", "Wanchu", "Gigantic Hammer", "Flying Foot", 
        "Mythril Hammer", "Ore Hammer", "Bloody Hammer", "Iron Hammer", "Aron", "Esthia", "Raven Claw", 
        "Answerer", "Steel Dagger", "Butterfly Knife", "Iron Knife", "Kogitsunemaru", "Heat Dagger", 
        "Morningstar", "Head Basher", "Earth Crusher", "Bronze Crusher", "Symphonia", "Whip", "Predator Claw", 
        "Shovel Claw", "Chupa Claw", "Farmer's Hoe", "Spade", "Crossbow", "Truncheon", "Halberd", 
        "Oak Club", "Ladle", "Spatula", "Justice Ruling", "Winner Ruling", "Tobacco Pipe", "Bottle", 
        "Guiron Tree", "Walking Stick", "Metal Pipe", "Fly Swatter", "Toadstool Bazooka", 
        "Slingshot", "Tamtam Slingshot", "Frying Pan", "Bokken", "Zengen", 'Ancient Magic Book', 'Iron Gauntlet',
        'Vagabond\'s Guitar', 'Knight Axe', 'Handmade Tunic', "Leather Armor", "Sharkskin", "Iron Breastplate", 
        "Wind Garb", "Wooden Breastplate", "Iron Plate", "Scale Armor", "Divine Coat", "Plate Armor", 
        "Dragon Scale", "Demon Mail", "Ore Armor", "Alfestrain", "Samurai Armor", "Absolute Guard", 
        "Valiant Mail", "Fayt Armor", "Robot Suit", "Recruitment Suit", "Glory Armor", "Ganz's Armor", 
        "Ridley's Clothes", "Trainee's Wear", "Valiant Mail", "Steel Guard", 
        "Leather Tunic", "Legendary Armor", "Metal Body", "Enchanted Robe", "Bushin Armor", "Red Lion Armor", 
        "Ancient Mail", "Resist Coat", "Samurai Armor", "Wing Garb", "Crocogator Skin", "Plate Armor", 
        "Plate Armor", "Plate Armor", "Axe Head", "Plate Armor", "Plate Armor", "Plate Armor", "Crocogator Skin", 
        "Crocogator Skin", "Resist Coat", "Plate Armor", "Plate Armor", "Crocogator Skin", "Normal Clothes", 
        "Mage Armor", "Great Mage Robe", "Magical Dress", "Mage's Robe", "Witch Cloak", "Vareth Uniform", 
        "High Priest's Gown", "Dual Cloak", "Peacock Garb", "Dry Cloak", "Master's Garment", "Robe of Order", 
        "Monk's Robe", "Robe of Order", "Nun's Robe",
        "Monster Cloak", "Scouts Suit", "Hades Robe", "Black Dress", "Leather Clothes", "Disguise", 
        "Hoodlum's Clothes", "Assassin Suit", "Scouts Suit", "Chrome Clothes", "Chrome Clothes", "Scouts Suit", 
        "Not Implemented Armor 1", "Chrome Clothes", "Chrome Clothes", "Knight Armor", "Knight Armor", "Normal Clothes", 
        "Not Implemented Armor 2", "Sacred Blue Gown", 
        "Children's Clothes", "Herdsman's Clothes", "Children's Clothes", 
        "Farming Clothes", "Farming Clothes", "Cook's Apron","Farming Clothes", "Linen Cuirass", "Cloth Apron", 
        "Green Robe", "Grass Clothes", 
        "Grass Clothes", "Grass Clothes", "Grass Clothes", "Autumn Leaf Cloak", "Leaf Clothes", "Leaf Clothes", "Leaf Clothes", 
        "Goblin Suit", 
        "Goblin Suit", "Goblin Suit", "Goblin Suit", "Goblin Suit", "Goblin Suit", "Goblin Suit", "Goblin Suit", 
        "Goblin Suit", "Goblin Suit", "Goblin Suit", "Goblin Suit", "Goblin Suit", "Big Toadstool Suit", 
        "Toadstool Suit", "Toadstool Suit", "Toadstool Suit", 
        "Shoulder Pads", "Vareth Uniform", "Shabby Mail", "Shabby Mail", "Glory Armor", 
        "Normal Clothes", "Nurse Uniform", "Smelly Old Clothes", 
        "Children's Clothes", "Valiant Mail", "Not Implemented Armor 3", "Glory Armor", "Trainee's Wear", 
        "Umbrella", "Herb Extract S", "Herb Extract DX", "Herb Extract MAX", "Revival Stone", "Cleansing Stone",
        "Moon Stone Chip", "Revival Stone Chip", "Cure Drop", "Cooling Spray", "Holy Water", "Flexibility Lotion",
        "Invincibility Med", "Mud Powder", "Mustard Powder", "Startle Powder", "Snow Powder", "Magma Powder", 
        "Panic Powder", "Mass of Enmity", "Cement Powder", "Tsuchinoko Dumpling", "Flee Ball", "Analysis Ball", 
        "Celestial Nectar", "Holy Sword Gram", "Seraphic Garb", "Evening Bloom", "David's Letter", 
        "Carlos's Contact Lens", "Faraus's Med/Voynich Book", "Key to Repository", "Man's Picture", "Church Bulletin", "Worn Belt", 
        "Lulu's Cat", "Matango Larva", "Gobpakken Seed", "Pointura's Thread", "Blood Orc's Horn" "Collection Bag",
        "Bridge Blueprints", "Piglet", "King's Toadstool", "Polpo's Soup", "Tria Milk", "Bligh's Pipe",
        "Deathclover Larva", "Nightstone", "Blue Orb", "Green Orb", "Red Orb", "Purple Orb", "Orb",
        "Smilodon's Fang", "Crocogator's Skin", "Arbitrator", "Boundary Crest", "Recruitment Flyer",
        "Bundle of Dagol", "Funny Money", "Really Funny Money", "Written Request", "Royal Knight Charter",
        "Dwarf Liquor", "Elven Wine", "Parsec's Match", "Shiny Ore", "Dwarf's Parcel", "Grass Clothes",
        "Grass Clothes", "Leaf Clothes", "Leaf Clothes", "Leaf Clothes", "Magical Dress"
    ]
    _RANGE02 = 191
    _BANK03 = {
        1000: {'title': 'Friends Book Extra 1', 'description': 'First part of the "extra" friend book entry.'},
        1001: {'title': 'Friends Book Extra 1', 'description': 'Second part of the "extra" friend book entry.'},
        1002: {'title': 'Friends Book Extra 1', 'description': 'Third part of the "extra" friend book entry.'},
        1003: {'title': 'Friends Book In-Progress', 'description': 'Friend book in-progress texture.'},
        1004: {'title': 'Friends Book Complete', 'description': 'Friend book complete texture.'},
    }
    _RANGE03 = 192
    _BANK06 = {
        1: {'title': 'Icons', 'description': 'Pack of icons'}
    }
    _RANGE06 = 195
    _BANK10 = {
        1: {'title': 'Poster 1', 'description': 'First part of a post texture.'},
        2: {'title': 'Poster 2', 'description': 'Second part of a post texture.'},
        3: {'title': 'Poster 3', 'description': 'Third part of a post texture.'},
        4: {'title': '1000 Bill 1', 'description': 'First part of the 1000 dollar bill texture.'},
        5: {'title': '1000 Bill 2', 'description': 'Second part of the 1000 dollar bill texture.'},
        6: {'title': '1000 Bill 3', 'description': 'Third part of the 1000 dollar bill texture.'},
        7: {'title': '5000 Bill 1', 'description': 'First part of the 5000 dollar bill texture.'},
        8: {'title': '5000 Bill 2', 'description': 'Second part of the 5000 dollar bill texture.'},
        9: {'title': '5000 Bill 3', 'description': 'Third part of the 5000 dollar bill texture.'},
        10: {'title': 'Legend 1 1', 'description': 'First part of the first legend texture.'},
        11: {'title': 'Legend 1 2', 'description': 'Second part of the first legend texture.'},
        12: {'title': 'Legend 1 3', 'description': 'Third part of the first legend texture.'},
        13: {'title': 'Legend 2 1', 'description': 'First part of the second legend texture.'},
        14: {'title': 'Legend 2 2', 'description': 'Second part of the second legend texture.'},
        15: {'title': 'Legend 2 3', 'description': 'Third part of the second legend texture.'},
        16: {'title': 'Legend 3 1', 'description': 'First part of the third legend texture.'},
        17: {'title': 'Legend 3 2', 'description': 'Second part of the third legend texture.'},
        18: {'title': 'Legend 3 3', 'description': 'Third part of the third legend texture.'},
        19: {'title': 'Flashback Dwarf Meeting 1', 'description': 'First part of the Dwarf Meeting flashback texture'},
        20: {'title': 'Flashback Dwarf Meeting 2', 'description': 'Second part of the Dwarf Meeting flashback texture'},
        21: {'title': 'Flashback Dwarf Meeting 3', 'description': 'Third part of the Dwarf Meeting flashback texture'},
        22: {'title': 'Flashback Ridley City of Flowers 1', 'description': 'First part of the Ridley City of Flowers flashback texture'},
        23: {'title': 'Flashback Ridley City of Flowers 2', 'description': 'Second part of the Ridley City of Flowers flashback texture'},
        24: {'title': 'Flashback Ridley City of Flowers 3', 'description': 'Third part of the Ridley City of Flowers flashback texture'},
        25: {'title': 'Flashback Cairn and Gawain 1', 'description': 'First part of the Cairn and Gawain flashback texture'},
        26: {'title': 'Flashback Cairn and Gawain 2', 'description': 'Second part of the Cairn and Gawain flashback texture'},
        27: {'title': 'Flashback Cairn and Gawain 3', 'description': 'Third part of the Cairn and Gawain flashback texture'},
        28: {'title': 'Flashback Hydra fight 1 1', 'description': 'First part of the Hydra fight 1 flashback texture'},
        29: {'title': 'Flashback Hydra fight 1 2', 'description': 'Second part of the Hydra fight 1 flashback texture'},
        30: {'title': 'Flashback Hydra fight 1 3', 'description': 'Third part of the Hydra fight 1 flashback texture'},
        31: {'title': 'Flashback Hydra fight 2 1', 'description': 'First part of the Hydra fight 2 flashback texture'},
        32: {'title': 'Flashback Hydra fight 2 2', 'description': 'Second part of the Hydra fight 2 flashback texture'},
        33: {'title': 'Flashback Hydra fight 2 3', 'description': 'Third part of the Hydra fight 2 flashback texture'},
        34: {'title': 'Flashback Hydra fight 3 1', 'description': 'First part of the Hydra fight 3flashback texture'},
        35: {'title': 'Flashback Hydra fight 32', 'description': 'Second part of the Hydra fight 3flashback texture'},
        36: {'title': 'Flashback Hydra fight 33', 'description': 'Third part of the Hydra fight 3flashback texture'},
        37: {'title': 'Flashback Cairn Diseased 1', 'description': 'First part of the Cairn Diseased flashback texture'},
        38: {'title': 'Flashback Cairn Diseased 2', 'description': 'Second part of the Cairn Diseased flashback texture'},
        39: {'title': 'Flashback Cairn Diseased 3', 'description': 'Third part of the Cairn Diseased flashback texture'},
        40: {'title': 'Fancy Flashback Hydra fight 3 1', 'description': 'First part of the fancy Hydra fight 3 flashback texture'},
        41: {'title': 'Fancy Flashback Hydra fight 3 2', 'description': 'Second part of the fancy Hydra fight 3 flashback texture'},
        42: {'title': 'Fancy Flashback Hydra fight 3 3', 'description': 'Third part of the fancy Hydra fight 3 flashback texture'},
    }
    _RANGE10 = 199

    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        dict_banks = [
            (cls._RANGE00, cls._BANK00),
            (cls._RANGE01, cls._BANK01),
            (cls._RANGE03, cls._BANK03),
            (cls._RANGE06, cls._BANK06),
        ]
        # Metadata for Texture Bank 00, 01, 03, 06
        for disk_idx, bank in dict_banks:
            for child_idx, data in bank.items():
                name = data.get('title', 'Unknown')
                desc = data.get('description', '')
                # Base (Compressed) entry
                base_meta: dict[str, Any] = {
                    'title': f'{name} (Compressed)', 
                    'tags': ('Texture',)
                }
                if desc:
                    base_meta['description'] = desc
                yield f'{disk_idx}.{child_idx}', base_meta
        
                # FIS entry
                fis_meta: dict[str, Any] = {
                    'title': name, 
                    'tags': ('Texture', 'FIS')
                }
                if desc:
                    fis_meta['description'] = desc
                yield f'{disk_idx}.{child_idx}.0', fis_meta

        # Metadata for Texture Bank 02
        disk_idx = cls._RANGE02
        for child_idx, name in enumerate(cls._BANK02):
            yield f'{disk_idx}.{child_idx + 1}', {
                'title': f'{name} Icon (Compressed)', 
                'tags': ('Texture',)
            }
            yield f'{disk_idx}.{child_idx + 1}.0', {
                'title': f'{name} Icon', 
                'tags': ('Texture', 'FIS')
            }
        # Metadata for Texture Bank 10
        (cls._RANGE10, cls._BANK10)
        disk_idx = cls._RANGE10
        for child_idx, data in cls._BANK10.items():
            yield f'{disk_idx}.{child_idx}', {
                'title': data.get('title'), 
                'description': data.get('description'), 
                'tags': ('Texture', 'FIS')}