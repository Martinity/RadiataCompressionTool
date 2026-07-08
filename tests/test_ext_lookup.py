"""Equivalence test: lookup_extension() must return the same result as the
old linear next(...) scan for every signature in generate_ext_overrides()
and for a selection of headers that match nothing."""

import pytest
from core.extension_overrides import generate_ext_overrides, lookup_extension


def _old_scan(header: bytes, default: str = '.bin') -> str:
    """Reference implementation: the original linear scan."""
    d = generate_ext_overrides()
    return next((m for s, m in d.items() if header.startswith(s)), default)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def all_signatures():
    return list(generate_ext_overrides().keys())


# ---------------------------------------------------------------------------
# Parametric equivalence: every known signature
# ---------------------------------------------------------------------------

def _make_header(sig: bytes) -> bytes:
    """Pad signature to at least 8 bytes so both approaches have enough to slice."""
    return sig + b'\x00' * max(0, 8 - len(sig))


@pytest.mark.parametrize('sig', list(generate_ext_overrides().keys()))
def test_known_signature_matches_old_scan(sig):
    header = _make_header(sig)
    expected = _old_scan(header)
    result = lookup_extension(header)
    assert result == expected, (
        f"Mismatch for sig={sig!r}: old={expected!r} new={result!r}"
    )


# ---------------------------------------------------------------------------
# Headers that should return the default
# ---------------------------------------------------------------------------

NO_MATCH_HEADERS = [
    b'\x00\x00\x00\x00\x00\x00\x00\x00',   # all zeros
    b'UNKN\x00\x00\x00\x00',               # unknown magic
    b'\xDE\xAD\xBE\xEF\x00\x00\x00\x00',  # random bytes
    b'    \x00\x00\x00\x00',               # spaces
    b'',                                    # empty
]


@pytest.mark.parametrize('header', NO_MATCH_HEADERS)
def test_no_match_returns_default(header):
    assert lookup_extension(header) == '.bin'
    assert lookup_extension(header, '.unknown') == '.unknown'


# ---------------------------------------------------------------------------
# Verify custom default is forwarded correctly
# ---------------------------------------------------------------------------

def test_custom_default_on_match():
    """A match should never use the default, even if a custom one is supplied."""
    sig = b'SLZ'
    header = sig + b'\x00' * 4
    assert lookup_extension(header, '.CUSTOM') == '.slz'


def test_custom_default_on_no_match():
    """An unrecognised header with a custom default must return that default."""
    header = b'XXXXXXXX'
    assert lookup_extension(header, '.pk3') == '.pk3'
