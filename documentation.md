
---

## Current TODO list:

- write a user guide for plugins
- Check stylesheet when there are more elements. Consider implementing generic objects rather than specific -> `/ui/style_sheet.py`
- Improve stylesheet naming and fix hovering/selection coloring
- File type legend... what to do? 

## Future TODO list:

- FPS complexe texture data is falsly decoded as regular fis texture data and when repacked will result in bugs at runtime
- FIS editor decoding CLUT shifts 7F to 80, does it matter? -> `/core/handlers/fis_leaf.py`
- Hex editor toggle for bottom values to display in hex or dec -> `/ui/editors/hex_editor.py`
- Staging page diff... This could be greatly improved but I don't want to spend a ton of time on anything beyond the basics to allow better analysis of custom format building -> `/ui/ui_core.py`
- 0FDC unpacking, seems to be an archive of slz format -> `/core/handlers/fdc_handler.py`
- Icons? -> `/ui/...`
- seqw handler -> `/core/handler/seqw_handler.py`
- Improve the efficiency of loading HDD users are struggling currently
