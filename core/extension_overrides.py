'''File Extension naming overrides'''

HEADERS = {
    # Core containers       | Purpose                             | In tool support
    b'SLZ'    : '.slz',     # Compressed file                     | 100%
    b'SLE'    : '.sle',     # Encrypted compressed file           | 100%
    b'Kods'   : '.kods',    # Custom archive (4 bytes)            | 100%
    b'1bcb'   : '.bcb',     # Packed entity data                  | 100%
    b'VIB'    : '.vib',     # Vibration motor data                | 0%
    (0x464C457F).to_bytes(4, 'little'): ".elf", # Executables     | ---
    (0xD51556).to_bytes(3, 'little'): ".idx",   # TOC             | 100%
    # Audio
    b'SEQW'   : '.seqw',    # Sound data (4 bytes)                | 0%
    b'VAGp'   : '.VAG',     # PS2 Standard format                 | 0%
    (0x000020).to_bytes(3, 'little'): ".020", # looped audio      | 0%
    # Movie
    (0x225277).to_bytes(3, 'little'): ".fmv", # movies            | 0%
    # Mesh
    b'FPS'    : '.fps',     # 
    b'FSS'    : '.fss',     # 
    b'IDOM'   : '.idom',    #
    b'LCTP'   : '.lctp',    #
    # Event
    b'EVD'    : '.evd',     # Event vm dispatcher data            | 0%
    # Animation
    b'FAS'    : '.fas',     # 
    b'HFAS'   : '.hfas',    #
    b'RMAC'   : '.rmac',    #
    b'RTA'    : '.rta',     #
    b'PAF'    : '.paf',     #
    # Texture
    b'FIS\00' : '.fis',   #
    b'FISP'   : '.fisp',    #
    b'FISA'   : ',fisa',
    b'TIM2'   : '.tim2',    # PS2 Standard format                 | 0%
    # Scene
    b'RBAD'   : '.rbad',    # Radiata Background Animation Data   | 0%
    b'RLF'    : '.rlf',     # 
    b'RMF'    : '.rmf',     #
    b'NDNC'   : '.ndnc',    #
    b'XBDC'   : '.xbdc',    #
    b'DNAL'   : '.dnal',    #
    b'TGIL'   : '.tgil',    # Container for map animation data
    # Gameplay
    b'0MPA'   : '.mpa',     # Sprite animation data
    b'0DTH'   : '.dth',     #
    b'0CPA'   : '.cpa',     #
    b'0IPA'   : '.ipa',     #
    b'0FDC'   : '.fdc',     #
    # Unknown / Descriptor
    b'RCP'    : '.rcp',     # Unknown Table of grouped IDs        | 0%
    b'RCAD'   : '.rcad',    #
    (0x89504E47).to_bytes(4, 'big'): ".png", # image
}

def generate_ext_overrides() -> dict[bytes, str]:
    """Build complete magic→extension map."""
    overrides = {}

    for idx, name in HEADERS.items():
        overrides[idx] = name

    return overrides
