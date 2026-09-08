"""The asset id tables: names for the numbers the game files address content by.

These are shared data, not one format's private lookup, so the two things worth
pinning are that a caller pays only for the category it asks for, and that a
table which will not load costs readability rather than raising.
"""

import json

import pytest

from core import asset_symbols
from utilities import get_resource_path


@pytest.fixture(autouse=True)
def clean_cache():
    """Each test starts with nothing loaded, so load tracking means something."""
    asset_symbols.names.cache_clear()
    asset_symbols._loaded.clear()
    yield
    asset_symbols.names.cache_clear()
    asset_symbols._loaded.clear()


###------------------------------------------- Loading -------------------------------------------###

def test_every_category_has_an_asset_file():
    for category in asset_symbols.CATEGORIES:
        assert asset_symbols.names(category), f'{category} loaded nothing'


def test_asking_for_one_category_loads_only_that_category():
    """The whole point of splitting the file: a handler that names an item id
    must not pay to parse 886 location names it will never look at."""
    asset_symbols.names(asset_symbols.ITEM)
    assert asset_symbols.loaded_categories() == (asset_symbols.ITEM,)


def test_a_repeat_lookup_does_not_reread_the_file():
    asset_symbols.names(asset_symbols.BGM)
    asset_symbols.names(asset_symbols.BGM)
    assert asset_symbols.names.cache_info().misses == 1
    assert asset_symbols.loaded_categories() == (asset_symbols.BGM,)


def test_a_missing_category_is_empty_rather_than_fatal():
    """Names are advisory. Nothing in a file format is decided by them, so a
    caller must never fail because a label is unavailable."""
    assert asset_symbols.names('not_a_category') == {}
    assert asset_symbols.name_for('not_a_category', 1) is None


def test_a_malformed_table_is_empty_rather_than_fatal(tmp_path, monkeypatch):
    broken = tmp_path / 'character.json'
    broken.write_text('{ this is not json', encoding='utf-8')
    monkeypatch.setattr(asset_symbols, 'get_resource_path', lambda rel: broken)
    assert asset_symbols.names(asset_symbols.CHARACTER) == {}


###------------------------------------------- Contents -------------------------------------------###

def test_ids_are_parsed_as_hex():
    """The files key by hex string, matching how the operands are written."""
    raw = json.loads(get_resource_path('ui/assets/symbols/character.json').read_text('utf-8'))
    assert '0x1' in raw or any(k.startswith('0x') for k in raw)
    assert asset_symbols.name_for(asset_symbols.CHARACTER, 1) == 'Jack'


def test_names_are_safe_to_append_to_a_line():
    """Cells come from a spreadsheet and can carry newlines, quotes and
    semicolons; callers paste the result into comments."""
    for category in asset_symbols.CATEGORIES:
        for value, name in asset_symbols.names(category).items():
            assert '\n' not in name and '"' not in name and ';' not in name, (category, value)
            assert len(name) <= 60, (category, value, name)
            assert name == name.strip() and name


def test_search_finds_by_substring_case_insensitively():
    found = dict(asset_symbols.search(asset_symbols.ITEM, 'ORB'))
    assert found[2] == 'Blue Orb'
    assert asset_symbols.search(asset_symbols.ITEM, '   ') == []


def test_all_names_covers_every_category():
    every = asset_symbols.all_names()
    assert set(every) == set(asset_symbols.CATEGORIES)
    assert every[asset_symbols.ITEM] == asset_symbols.names(asset_symbols.ITEM)


###--------------------------------------- The EVD consumer ---------------------------------------###

def test_the_evd_handler_merges_every_category_on_load():
    """EVD is the one caller that legitimately wants all of them: a script can
    reference a character, an item, a location, a BGM track and a flag."""
    from core.handlers import evd_leaf

    assert set(evd_leaf.SYMBOLS.DOMAINS) == set(asset_symbols.CATEGORIES)
    assert evd_leaf.SYMBOLS.tables == asset_symbols.all_names()
    assert evd_leaf.SYMBOLS.lookup('character', 1) == 'Jack'
