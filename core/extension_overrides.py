'''File Extension naming overrides'''

HEADERS = {
    # Core containers
    b'SLZ'    : '.slz',
    b'SLE'    : '.sle',
    b'Kods'   : '.kods',
    b'1bcb'   : '.bcb',
    b'VIB'    : '.vib',
    (0x464C457F).to_bytes(4, 'little'): ".elf",
    (0xD51556).to_bytes(3, 'little'): ".idx",   # TOC 
    # Audio
    b'SEQW'   : '.seqw',
    b'VAGp'   : '.VAG',
    (0x000020).to_bytes(3, 'little'): ".020",
    # Movie
    (0x225277).to_bytes(3, 'little'): ".fmv",
    # Mesh
    b'FPS'    : '.fps',
    b'FSS'    : '.fss',
    b'IDOM'   : '.idom',
    b'LCTP'   : '.lctp',
    # Event
    b'EVD'    : '.evd',
    # Animation
    b'FAS'    : '.fas',
    b'HFAS'   : '.hfas',
    b'RMAC'   : '.rmac',
    b'RTA'    : '.rta',
    b'PAF'    : '.paf',
    # Texture
    b'FIS\00' : '.fis',
    b'FISP'   : '.fisp',
    b'FISA'   : ',fisa',
    b'TIM2'   : '.tim2',
    # Scene
    b'RBAD'   : '.rbad',
    b'RLF'    : '.rlf',
    b'RMF'    : '.rmf',
    b'NDNC'   : '.ndnc',
    b'XBDC'   : '.xbdc',
    b'PCDC'   : '.pcdc',
    b'DNAL'   : '.dnal',
    b'TGIL'   : '.tgil',
    # Gameplay
    b'0MPA'   : '.mpa',
    b'0DTH'   : '.dth',
    b'0CPA'   : '.cpa',
    b'0IPA'   : '.ipa',
    b'0FDC'   : '.fdc',
    # Unknown / Descriptor
    b'RCP'    : '.rcp',
    b'RCAD'   : '.rcad',
    (0x89504E47).to_bytes(4, 'big'): ".png",
}

def generate_ext_overrides() -> dict[bytes, str]:
    """Build complete magic→extension map."""
    overrides = {}

    for idx, name in HEADERS.items():
        overrides[idx] = name

    return overrides

