'''File Extension naming overrides'''

HEADERS = {
    # Core containers
    b'SLZ'  : '.slz',     # Compressed file                     | 100%
    b'SLE'  : '.sle',     # Encrypted compressed file           | 100%
    b'Kods' : '.kods',    # Custom archive (4 bytes)            | 70%
    b'1bcb' : '.bcb',     # Packed entity data                  | 5%
    b'VIB'  : '.vib',     # Vibration motor data                | 0%
    (0x464C457F).to_bytes(4, 'little'): ".elf", # Executables   | ---
    (0xD51556).to_bytes(3, 'little'): ".idx",   # TOC           | 100%
    # Audio
    b'SEQW' : '.seqw',    # Sound data (4 bytes)                | 0%
    b'VAGp' : '.VAG',     # PS2 Standard format                 | 100%
    (0x000020).to_bytes(3, 'little'): ".020", # looped audio    | 0%
    # Movie
    (0x225277).to_bytes(3, 'little'): ".fmv", # movies          | 0%
    # Mesh
    b'FPS'  : '.fps',     # 
    b'FSS'  : '.fss',     # 
    b'IDOM' : '.idom',    #
    b'LCTP' : '.lctp',    #
    # Event
    b'EVD'  : '.evd',     # Event vm dispatcher data            | 60%
    # Animation
    b'FAS'  : '.fas',     # 
    b'HFAS' : '.hfas',    #
    b'RMAC' : '.rmac',    #
    b'RTA'  : '.rta',     #
    b'PAF'  : '.paf',     #
    # Texture
    b'FIS0' : '.fis',     #
    b'FISP' : '.fisp',    #
    b'TIM2' : '.tim2',    # PS2 Standard format                 | 100%
    # Scene
    b'RBAD' : '.rbad',    # Radiata Background Animation Data   | 10%
    b'RLF'  : '.rlf',     # 
    b'RMF'  : '.rmf',     #
    b'NDNC' : '.ndnc',    #
    b'XBDC' : '.xbdc',    #
    b'DNAL' : '.dnal',    #
    b'TGIL' : '.tgil',    # Container for map animation data
    # Gameplay
    b'0MPA' : '.mpa',     # Sprite animation data
    b'0DTH' : '.dth',     #
    b'0CPA' : '.cpa',     #
    b'0IPA' : '.ipa',     #
    b'0FDC' : '.fdc',     #
    # Unknown / Descriptor
    b'RCP'  : '.rcp',     # Unknown Table of grouped IDs        | 30%
    b'RCAD' : '.rcad',    #
}

def generate_ext_overrides() -> dict[bytes, str]:
    """Build complete magic→extension map."""
    overrides = {}

    for idx, name in HEADERS.items():
        overrides[idx] = name

    return overrides
