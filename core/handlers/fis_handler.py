'''FIS texture handler and viewer for Radiata Stories.

Decodes PS2 PSMT8 / PSMT4 textures directly to QImage — no PIL dependency.
Registers both a handler (for VFS parsing) and an editor widget (for display),
so the workspace automatically opens the viewer when a FIS node is selected.
'''
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Callable

from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtGui import QImage, QPixmap, QColor
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QScrollArea,
    QWidget, QPushButton, QFileDialog, QFrame, QSizePolicy,
    QMessageBox,
)

from core.contracts import LeafHandler, BaseEditor
from core.registry import Registry
from core.workers import ActionDef, ActionType
from core.node import VfsNode

import logging
logger = logging.getLogger(f'radiata.{__name__}')

# ---------------------------------------------------------------------------
# FIS format constants
# ---------------------------------------------------------------------------

FIS_MAGIC             = b'FIS\x00'
FIS_FLAG_SPECIAL      = 0x2000

_PAL_8BPP = {0x13, 0x1B}
_PAL_4BPP = {0x14, 0x24, 0x2C}

_PSM_NAMES = {
    0x00: 'PSMCT32', 0x01: 'PSMCT24', 0x02: 'PSMCT16',
    0x13: 'PSMT8',   0x14: 'PSMT4',   0x1B: 'PSMT8H',
    0x24: 'PSMT4HL', 0x2C: 'PSMT4HH',
}

# ---------------------------------------------------------------------------
# FIS metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FISInfo:
    '''All parsed metadata for a single FIS texture.'''
    name:                bytes
    flags:               int
    psm:                 int
    psm_name:            str
    bpp:                 int
    width:               int
    height:              int
    raw_width:           int
    raw_height:          int
    dimension_mode:      str
    swizzled:            bool
    padded_4bpp_clut:    bool
    palette_offset:      int | None
    palette_storage_size: int
    image_offset:        int
    image_size:          int

    @property
    def special_layout(self) -> bool:
        return bool(self.flags & FIS_FLAG_SPECIAL)

# ---------------------------------------------------------------------------
# Binary helpers (no PIL)
# ---------------------------------------------------------------------------

def _u16(data: bytes, off: int) -> int:
    return struct.unpack_from('<H', data, off)[0]

def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from('<I', data, off)[0]

def _ps2_alpha(a: int) -> int:
    '''PS2 alpha 0..0x80 → 0..255.'''
    return min(255, a * 2)

def _read_palette(data: bytes, offset: int, count: int) -> list[tuple[int,int,int,int]]:
    out = []
    for i in range(count):
        r, g, b, a = struct.unpack_from('4B', data, offset + i * 4)
        out.append((r, g, b, _ps2_alpha(a)))
    return out

def _read_4bpp_palette(data: bytes, offset: int, *, padded: bool) -> list[tuple[int,int,int,int]]:
    out = []
    for i in range(16):
        pos = offset + i * 4
        if padded and i >= 8:
            pos += 0x20
        r, g, b, a = struct.unpack_from('4B', data, pos)
        out.append((r, g, b, _ps2_alpha(a)))
    return out

def _clut_interleave(pal: list) -> list:
    '''PS2 PSMT8 CLUT interleave — symmetric (encode == decode).'''
    out = list(pal)
    for base in range(0, 256, 32):
        for i in range(8):
            out[base + 8 + i], out[base + 16 + i] = pal[base + 16 + i], pal[base + 8 + i]
    return out

def _unswizzle_psmt8(src: bytes, width: int, height: int) -> bytearray:
    dst = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            bl = (y & ~0xF) * width + (x & ~0xF) * 2
            ss = (((y + 2) >> 2) & 1) * 4
            py = (((y & ~3) >> 1) + (y & 1)) & 7
            cl = py * width * 2 + ((x + ss) & 7) * 4
            bn = ((y >> 1) & 1) + ((x >> 2) & 2)
            si = bl + cl + bn
            li = y * width + x
            if si < len(src) and li < len(dst):
                dst[li] = src[si]
    return dst

def _unpack_4bpp(data: bytes, width: int, height: int) -> list[int]:
    expected = (width * height) // 2
    out = []
    for byte in data[:expected]:
        out.append(byte & 0x0F)
        out.append((byte >> 4) & 0x0F)
    return out

def _auto_padded_4bpp(data: bytes, pal_off: int, pal_size: int) -> bool:
    if pal_size < 0x60 or len(data) < pal_off + 0x60:
        return False
    def mostly_zero(chunk: bytes) -> bool:
        return chunk.count(0) >= len(chunk) * 0.75
    return (
        not mostly_zero(data[pal_off:pal_off + 0x20])
        and mostly_zero(data[pal_off + 0x20:pal_off + 0x40])
        and not mostly_zero(data[pal_off + 0x40:pal_off + 0x60])
    )

def _expected_size(w: int, h: int, bpp: int) -> int | None:
    return {8: w * h, 4: (w * h) // 2, 16: w * h * 2, 32: w * h * 4}.get(bpp)

def _bpp(psm: int) -> int:
    if psm in _PAL_8BPP: return 8
    if psm in _PAL_4BPP: return 4
    if psm == 0x02:      return 16
    return 32

# ---------------------------------------------------------------------------
# FIS parsing
# ---------------------------------------------------------------------------

def parse_fis(data: bytes, *, swizzled: bool | None = None, padded_4bpp: bool | None = None) -> FISInfo:
    if len(data) < 0x30:
        raise ValueError('Buffer too small for FIS header')
    if data[:4] != FIS_MAGIC:
        raise ValueError(f'Not a FIS texture — magic {data[:4]!r}')

    flags               = _u16(data, 0x10)
    clut_entries_field  = _u16(data, 0x12)
    name                = data[0x14:0x18]
    pre_image_size      = _u32(data, 0x1C)
    pal_storage_size    = _u32(data, 0x20)
    img_hdr_off         = pre_image_size + 0x10

    if img_hdr_off + 0x10 > len(data):
        raise ValueError(f'Image header offset {hex(img_hdr_off)} outside buffer')

    raw_w      = _u16(data, img_hdr_off + 0x00)
    raw_h      = _u16(data, img_hdr_off + 0x02)
    image_size = _u32(data, img_hdr_off + 0x04)
    setup_off  = _u32(data, img_hdr_off + 0x08)
    psm        = _u16(data, img_hdr_off + 0x0C)
    tw         = data[img_hdr_off + 0x0E]
    th         = data[img_hdr_off + 0x0F]
    bpp_val    = _bpp(psm)

    def matches(w: int, h: int) -> bool:
        exp = _expected_size(w, h, bpp_val)
        return exp is not None and exp == image_size

    # Decode dimensions
    if flags & FIS_FLAG_SPECIAL:
        if psm in _PAL_8BPP:
            w, h, dim_mode = raw_w * 2, raw_h * 2, 'special_8bpp_x2'
        elif psm in _PAL_4BPP:
            w, h, dim_mode = raw_w * 2, raw_h * 4, 'special_4bpp_x2x4'
        else:
            w, h, dim_mode = raw_w, raw_h, 'special_unknown'
    else:
        w, h, dim_mode = raw_w, raw_h, 'stored'
        if not matches(w, h) and 0 <= tw <= 15 and 0 <= th <= 15:
            tw_w, tw_h = 1 << tw, 1 << th
            if matches(tw_w, tw_h):
                w, h, dim_mode = tw_w, tw_h, 'tw_th'

    # Fallback tw/th
    if not matches(w, h) and 0 <= tw <= 15 and 0 <= th <= 15:
        tw_w, tw_h = 1 << tw, 1 << th
        if matches(tw_w, tw_h):
            w, h, dim_mode = tw_w, tw_h, f'{dim_mode}_fallback_tw_th'

    # Palette location
    pal_off: int | None = None
    if pal_storage_size:
        computed = img_hdr_off - pal_storage_size
        if computed >= 0:
            pal_off = computed

    if swizzled is None:
        swizzled = bool(flags & FIS_FLAG_SPECIAL)
    if padded_4bpp is None:
        padded_4bpp = (
            bpp_val == 4 and pal_off is not None
            and _auto_padded_4bpp(data, pal_off, pal_storage_size)
        )

    return FISInfo(
        name=name, flags=flags, psm=psm,
        psm_name=_PSM_NAMES.get(psm, f'UNKNOWN_{psm:#04x}'),
        bpp=bpp_val, width=w, height=h, raw_width=raw_w, raw_height=raw_h,
        dimension_mode=dim_mode, swizzled=bool(swizzled),
        padded_4bpp_clut=bool(padded_4bpp),
        palette_offset=pal_off, palette_storage_size=pal_storage_size,
        image_offset=setup_off + 0x90, image_size=image_size,
    )

# ---------------------------------------------------------------------------
# FIS → QImage decode (no PIL)
# ---------------------------------------------------------------------------

def decode_fis_to_qimage(
    data: bytes,
    *,
    swizzled: bool | None = None,
    padded_4bpp: bool | None = None,
) -> tuple[QImage, FISInfo]:
    '''
    Decode a FIS texture into a QImage (Format_RGBA8888).
    Returns (image, info) so callers have access to metadata.
    Raises ValueError / NotImplementedError on unsupported formats.
    '''
    info = parse_fis(data, swizzled=swizzled, padded_4bpp=padded_4bpp)

    end = info.image_offset + info.image_size
    if end > len(data):
        raise ValueError(f'Truncated FIS: need 0x{end:X} bytes, have 0x{len(data):X}')

    w, h = info.width, info.height

    if info.bpp == 8:
        if info.palette_offset is None or len(data) < info.palette_offset + 0x400:
            raise ValueError('Insufficient 8bpp CLUT data')
        palette    = _clut_interleave(_read_palette(data, info.palette_offset, 256))
        pixel_data = bytearray(data[info.image_offset: info.image_offset + info.image_size])
        if info.swizzled:
            pixel_data = _unswizzle_psmt8(pixel_data, w, h)
        rgba = _indices_to_rgba(pixel_data, palette, w, h)

    elif info.bpp == 4:
        footprint = 0x60 if info.padded_4bpp_clut else 0x40
        if info.palette_offset is None or len(data) < info.palette_offset + footprint:
            raise ValueError('Insufficient 4bpp CLUT data')
        palette    = _read_4bpp_palette(data, info.palette_offset, padded=info.padded_4bpp_clut)
        pixel_data = bytearray(data[info.image_offset: info.image_offset + info.image_size])
        if info.swizzled:
            pixel_data = _unswizzle_psmt8(pixel_data, w // 2, h)
        indices = _unpack_4bpp(pixel_data, w, h)
        rgba = _indices_to_rgba(indices, palette, w, h)

    else:
        raise NotImplementedError(
            f'FIS decode only supports PSMT8/PSMT4; got {info.psm_name}'
        )

    # QImage takes ownership of a copy — pass bytes to avoid lifetime issues
    img = QImage(bytes(rgba), w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
    return img, info


def _indices_to_rgba(
    indices: bytes | bytearray | list[int],
    palette: list[tuple[int,int,int,int]],
    width: int,
    height: int,
) -> bytearray:
    '''Map palette indices to a flat RGBA8888 bytearray.'''
    buf = bytearray(width * height * 4)
    for i, idx in enumerate(indices[:width * height]):
        r, g, b, a = palette[idx]
        off = i * 4
        buf[off], buf[off+1], buf[off+2], buf[off+3] = r, g, b, a
    return buf

# ---------------------------------------------------------------------------
# Handler — registered for VFS integration
# ---------------------------------------------------------------------------

@Registry.register(
    name='FIS Texture Handler',
    extensions=('.fis',),
    supported_actions={
        'Properties': ActionDef('Properties', ActionType.DIALOG, 'Texture Properties'),
    },
)
class FisHandler(LeafHandler):
    '''Leaf handler for FIS textures. The editor widget owns decoding + display.'''

    def __init__(self, source: bytes, parent: VfsNode | None = None) -> None:
        super().__init__(source)
        self.handler_parent = parent
        self._raw = source if isinstance(source, bytes) else bytes(source)

    def execute_action(
        self,
        node: VfsNode,
        action_name: str,
        progress_callback: Callable,
        log_callback: Callable,
        **kwargs,
    ) -> Any:
        if action_name == 'Properties':
            try:
                info = parse_fis(self._raw)
                return (
                    f'Name:      {info.name!r}\n'
                    f'PSM:       {info.psm_name} ({info.bpp}bpp)\n'
                    f'Size:      {info.width} × {info.height}\n'
                    f'Swizzled:  {info.swizzled}\n'
                    f'Dim mode:  {info.dimension_mode}\n'
                    f'Padded CLUT: {info.padded_4bpp_clut}'
                )
            except Exception as e:
                return f'Parse error: {e}'
        return None

    def get_identity(self) -> str:
        return 'FIS Texture'

# ---------------------------------------------------------------------------
# Editor widget — registered as the viewer for .fis nodes
# ---------------------------------------------------------------------------

@Registry.register(name='FIS Texture Viewer', extensions=('.fis',))
class FisEditorWidget(BaseEditor):
    '''
    Displays a decoded FIS texture with metadata.
    Registered via the Registry so the workspace opens it automatically
    when a .fis node is selected.
    '''

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._raw:  bytes   | None = None
        self._info: FISInfo | None = None
        self._setup_ui()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_toolbar())

        # Central: scrollable image on the left, info panel on the right
        body = QHBoxLayout()
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(8)
        body.addWidget(self._build_image_area(), stretch=3)
        body.addWidget(self._build_info_panel(), stretch=1)
        root.addLayout(body)

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName('EditorToolbar')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 5, 10, 5)

        self._title_label = QLabel('FIS Texture Viewer')
        self._btn_export  = QPushButton('Export PNG')
        self._btn_export.setEnabled(False)
        self._btn_export.clicked.connect(self._export_png)

        lay.addWidget(self._title_label)
        lay.addStretch()
        lay.addWidget(self._btn_export)
        return bar

    def _build_image_area(self) -> QScrollArea:
        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._image_label.setMinimumSize(64, 64)
        self._image_label.setText('No texture loaded')

        area = QScrollArea()
        area.setWidget(self._image_label)
        area.setWidgetResizable(True)
        area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll_area = area
        return area

    def _build_info_panel(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setFixedWidth(220)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)

        self._info_rows: dict[str, QLabel] = {}
        for key in ('Name', 'PSM', 'BPP', 'Width', 'Height',
                    'Dim mode', 'Swizzled', 'Padded CLUT',
                    'Pal offset', 'Image offset', 'Image size'):
            row = QHBoxLayout()
            row.addWidget(QLabel(f'{key}:'))
            val = QLabel('—')
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val.setWordWrap(True)
            row.addWidget(val, stretch=1)
            lay.addLayout(row)
            self._info_rows[key] = val

        lay.addStretch()
        return frame

    # ------------------------------------------------------------------
    # BaseEditor implementation
    # ------------------------------------------------------------------

    def load_node(self, node: VfsNode, data: bytes) -> None:
        super().load_node(node, data)
        self._raw = data

        try:
            img, info = decode_fis_to_qimage(data)
        except NotImplementedError as e:
            self._show_error(str(e))
            logger.warning(f'FIS: unsupported format in {node.name}: {e}')
            return
        except Exception as e:
            self._show_error(f'Decode failed:\n{e}')
            logger.error(f'FIS decode error for {node.name}: {e}', exc_info=True)
            return

        self._info = info
        self._display_image(img)
        self._populate_info(info)
        self._title_label.setText(
            f'{node.name}  —  {info.width}×{info.height}  {info.psm_name}'
        )
        self._btn_export.setEnabled(True)
        logger.debug(f'FIS: loaded {node.name} ({info.width}×{info.height} {info.psm_name})')

    def get_modified_data(self) -> bytes:
        # FIS is currently read-only; return the original bytes unchanged
        return self._raw or b''

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def _display_image(self, img: QImage) -> None:
        '''Show the decoded image, scaled to fit without distortion.'''
        pixmap = QPixmap.fromImage(img)
        # Scale to fit the scroll area while preserving aspect ratio
        available = self._scroll_area.size() - QSize(4, 4)
        if img.width() > available.width() or img.height() > available.height():
            pixmap = pixmap.scaled(
                available,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        self._image_label.setPixmap(pixmap)
        self._image_label.resize(pixmap.size())

    def _show_error(self, message: str) -> None:
        self._image_label.setPixmap(QPixmap())
        self._image_label.setText(message)
        self._btn_export.setEnabled(False)
        for lbl in self._info_rows.values():
            lbl.setText('—')

    def _populate_info(self, info: FISInfo) -> None:
        def fmt_hex(v: int | None) -> str:
            return '—' if v is None else hex(v)

        self._info_rows['Name'].setText(repr(info.name))
        self._info_rows['PSM'].setText(info.psm_name)
        self._info_rows['BPP'].setText(str(info.bpp))
        self._info_rows['Width'].setText(str(info.width))
        self._info_rows['Height'].setText(str(info.height))
        self._info_rows['Dim mode'].setText(info.dimension_mode)
        self._info_rows['Swizzled'].setText(str(info.swizzled))
        self._info_rows['Padded CLUT'].setText(str(info.padded_4bpp_clut))
        self._info_rows['Pal offset'].setText(fmt_hex(info.palette_offset))
        self._info_rows['Image offset'].setText(fmt_hex(info.image_offset))
        self._info_rows['Image size'].setText(fmt_hex(info.image_size))

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _export_png(self) -> None:
        if not self._raw or not self._info:
            return
        node_name = self.current_node.name if self.current_node else 'texture'
        path, _ = QFileDialog.getSaveFileName(
            self, 'Export PNG', f'{node_name}.png', 'PNG Images (*.png)'
        )
        if not path:
            return
        try:
            img, _ = decode_fis_to_qimage(self._raw)
            if not img.save(path, 'PNG'):
                raise RuntimeError('QImage.save() returned False')
            logger.info(f'FIS: exported {node_name} → {path}')
        except Exception as e:
            QMessageBox.critical(self, 'Export failed', str(e))
            logger.error(f'FIS export error: {e}', exc_info=True)
