'''
EVD event-script support.

`evd_tool` is vendored from `rs_elf/tools/evd_tool.py`, the decoder/encoder that
round-trips all 7,462 EVD files the game ships. Upstream is a command-line
program; this copy is the part of it `api` reaches and nothing else -- no CLI,
no SLZ container handling (the compression handler unwraps that long before an
EVD gets here), no handler disassembly or reference generation.

The reduction is mechanical, not hand-edited, so re-syncing stays cheap:

    python scripts/vendor_evd_tool.py --check    # is this copy current?
    python scripts/vendor_evd_tool.py            # re-vendor from upstream

Nothing in this project should edit `evd_tool.py` -- the next re-vendor would
discard it. Everything the app needs is exposed through `api`, which is the only
module the handler and editor import.
'''
from core.evd import api

__all__ = ['api']
