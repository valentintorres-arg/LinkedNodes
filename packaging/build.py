"""Builds LinkedNodes.exe, a windowed program that runs without Python installed.

    python packaging/build.py              build dist/LinkedNodes/
    python packaging/build.py --install    build, copy it to your user's Programs folder
                                           and add Desktop and Start Menu shortcuts
    python packaging/build.py --uninstall  remove that copy and its shortcuts

Needs PyInstaller and Pillow: pip install -e ".[app,build]". Windows only.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORK = ROOT / "build"
BUILT = ROOT / "dist" / "LinkedNodes"
# Imported lazily by NetworkX for things LinkedNodes never calls; left in, they would
# multiply the size of the program.
UNUSED = ["numpy", "scipy", "pandas", "matplotlib", "PIL", "tkinter", "pytest", "IPython", "setuptools"]


def draw_icon(path: Path) -> None:
    """A small graph, white on black with one lavender node, at every size Windows asks for."""
    from PIL import Image, ImageDraw

    size = 1024  # drawn large and scaled down, which smooths the edges
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    pen.rounded_rectangle((0, 0, size - 1, size - 1), radius=200, fill="#0b0c0e")
    nodes = {"a": (300, 330), "b": (730, 270), "c": (560, 620), "d": (270, 760), "e": (790, 760)}
    for start, end in ("ab", "ac", "bc", "cd", "ce"):
        pen.line((*nodes[start], *nodes[end]), fill="#ffffff", width=34)
    for name, (x, y) in nodes.items():
        radius = 120 if name == "c" else 82
        pen.ellipse((x - radius, y - radius, x + radius, y + radius), fill="#b9a8ff" if name == "c" else "#ffffff",
                    outline="#0b0c0e", width=26)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])


def build() -> None:
    icon = WORK / "linkednodes.ico"
    draw_icon(icon)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
               "--name", "LinkedNodes", "--icon", str(icon),
               "--distpath", str(BUILT.parent), "--workpath", str(WORK), "--specpath", str(WORK),
               "--collect-data", "linkednodes"]
    for module in UNUSED:
        command += ["--exclude-module", module]
    subprocess.run([*command, str(HERE / "launch.py")], check=True, cwd=ROOT)
    megabytes = sum(f.stat().st_size for f in BUILT.rglob("*") if f.is_file()) / 1_000_000
    print(f"Built {BUILT / 'LinkedNodes.exe'} ({megabytes:.0f} MB in the folder)")


def _places() -> tuple[Path, list[Path]]:
    """Where the program is installed for this user, and where its shortcuts go."""
    ask = "[Environment]::GetFolderPath('Desktop'); [Environment]::GetFolderPath('Programs')"
    found = subprocess.run(["powershell", "-NoProfile", "-Command", ask], capture_output=True, text=True, check=True)
    desktop, menu = (Path(line.strip()) for line in found.stdout.splitlines()[:2])
    target = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "LinkedNodes"
    return target, [desktop / "LinkedNodes.lnk", menu / "LinkedNodes.lnk"]


def install() -> None:
    target, shortcuts = _places()
    # The new copy is put beside the old one, and they change places only once it is whole.
    # Windows will not move a folder a running program was started from, so with LinkedNodes
    # open this stops there, and the copy that is installed is left as it was.
    fresh, old = target.with_name(target.name + ".new"), target.with_name(target.name + ".old")
    for leftover in (fresh, old):
        shutil.rmtree(leftover, ignore_errors=True)
    shutil.copytree(BUILT, fresh)
    if target.exists():
        try:
            target.rename(old)
        except OSError:
            shutil.rmtree(fresh, ignore_errors=True)
            raise SystemExit(f"error: LinkedNodes is open, so {target} cannot be replaced. Close its window and "
                             "run this again; the build in dist/ is kept.")
    fresh.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    program = target / "LinkedNodes.exe"
    for shortcut in shortcuts:
        script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{shortcut}'); "
                  f"$s.TargetPath = '{program}'; $s.WorkingDirectory = '{target}'; "
                  f"$s.IconLocation = '{program},0'; $s.Description = 'LinkedNodes'; $s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
        print(f"Shortcut: {shortcut}")
    print(f"Installed in {target}")


def uninstall() -> None:
    target, shortcuts = _places()
    for shortcut in shortcuts:
        shortcut.unlink(missing_ok=True)
    if target.exists():
        shutil.rmtree(target)
    print(f"Removed {target} and its shortcuts. Settings stay in {target.parent.parent / 'LinkedNodes'}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    options = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("error: this builds the Windows program; run it on Windows")
    if options.uninstall:
        uninstall()
    else:
        build()
        if options.install:
            install()
