'''
Asset id lookup tables.

The game addresses its content by numeric id: character 1 is Jack, item 320 is
Sharkskin, location 754 is Jack's Place. Those tables are not specific to any
one file format -- a script references a character id, a save patch references
the same id, a model tool references the same location -- so they live here
rather than inside whichever handler happened to need them first.

Each category is a separate asset file under `ui/assets/symbols/`, loaded and
cached on first use. A handler that needs one category pays for one category:

    from core.asset_symbols import names, name_for

    name_for(CHARACTER, 1)      # 'Jack'
    names(ITEM)                 # the whole item table

Names are advisory. They are for display and for completion; nothing in a file
format is decided by them, so a missing or stale table costs readability only.
'''
from __future__ import annotations

import json
import re
from functools import lru_cache

from utilities import get_resource_path

import logging
logger = logging.getLogger(f'radiata.{__name__}')

CHARACTER = 'character'
ITEM      = 'item'
LOCATION  = 'location'
BGM       = 'bgm'
SKILL     = 'skill'
EVENT     = 'event'
FLAG      = 'flag'

CATEGORIES: tuple[str, ...] = (CHARACTER, ITEM, LOCATION, BGM, SKILL, EVENT, FLAG)

_ASSET_DIR = 'ui/assets/symbols'
_WHITESPACE = re.compile(r'\s+')
_loaded: list[str] = []
_MAX_NAME = 60


def _clean(name: str) -> str:
    '''Collapse a spreadsheet cell into something safe to append to a line.

    These tables are compiled from community spreadsheets, so a cell can carry
    newlines and quotes. Callers paste the result into comments and tooltips.
    '''
    text = _WHITESPACE.sub(' ', name.replace('"', "'").replace(';', ',')).strip()
    return text[:_MAX_NAME - 3].rstrip() + '...' if len(text) > _MAX_NAME else text


@lru_cache(maxsize=None)
def names(category: str) -> dict[int, str]:
    '''The id-to-name table for one category, or empty when it will not load.

    Cached, so repeated lookups cost one parse. A category that is missing or
    malformed is logged and returns empty rather than raising: a lookup table
    is a convenience, and no caller should fail because a label is unavailable.
    '''
    path = get_resource_path(f'{_ASSET_DIR}/{category}.json')
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f'Could not load the {category} id table from {path}: {e}')
        _loaded.append(category)
        return {}

    table: dict[int, str] = {}
    for key, value in raw.items():
        try:
            number = int(key, 16)
        except (TypeError, ValueError):
            logger.warning(f'{category}.json: skipping non-hex id {key!r}')
            continue
        cleaned = _clean(value)
        if cleaned:
            table[number] = cleaned
    _loaded.append(category)
    return table


def name_for(category: str, value: int) -> str | None:
    '''The name of one id, or None when it has no label.'''
    return names(category).get(value)


def search(category: str, text: str) -> list[tuple[int, str]]:
    '''Ids in `category` whose name contains `text`, case-insensitively.'''
    needle = text.strip().lower()
    if not needle:
        return []
    return sorted((value, name) for value, name in names(category).items()
                  if needle in name.lower())


def all_names() -> dict[str, dict[int, str]]:
    '''Every category at once, for a caller that genuinely needs all of them.

    Loading one category at a time is the point of this module; reach for this
    only when the alternative is calling `names()` for all of them anyway.
    '''
    return {category: names(category) for category in CATEGORIES}


def loaded_categories() -> tuple[str, ...]:
    '''Which categories have actually been read, in load order.

    Exposed so a caller can confirm it is not dragging in tables it never asked
    for; the lazy loading is the whole point of splitting these files up.
    '''
    return tuple(_loaded)
