"""Compile the native libs into native_build/ for bundling. Mirrors the flags in
core/native/compressor_loader.py and core/handlers/tac_leaf.py."""
import shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "native_build"

def lib_name(stem: str) -> str:
    if sys.platform.startswith("win"):
        return f"{stem}.dll"
    if sys.platform == "darwin":
        return f"lib{stem}.dylib"
    return f"lib{stem}.so"

def cc() -> str:
    if sys.platform.startswith("win"):
        c = shutil.which("gcc")
    else:
        c = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not c:
        raise SystemExit("No C compiler found on PATH")
    return c

def build(src: Path, out: Path, extra: list[str]) -> None:
    plat = ["-static-libgcc"] if sys.platform.startswith("win") else ["-fPIC"]
    cmd = [cc(), "-shared", "-O2", "-std=c99", "-Wall", "-Wextra", *plat,
           "-o", str(out), str(src), *extra]
    print("BUILD:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=src.parent)

def main() -> None:
    OUT.mkdir(exist_ok=True)
    build(ROOT / "core/native/radiata_compressor.c", OUT / lib_name("radiata_compressor"), [])
    build(ROOT / "core/native/tac_lib.c", OUT / lib_name("tac_codec"), ["-lm"])
    build(ROOT / "core/native/block_device.c", OUT / lib_name("block_device"), [])
    print("native_build contents:", [p.name for p in OUT.iterdir()])

if __name__ == "__main__":
    main()
