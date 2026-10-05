"""KiCad IPC plugin entrypoint."""

from __future__ import annotations

import datetime
import os
import re
import sys
import traceback
from pathlib import Path

_LOG = Path.home() / ".local" / "share" / "kicad" / "kicad-autorouter-launch.log"


def _log(message: str) -> None:
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now(datetime.UTC).isoformat()
        with _LOG.open("a") as stream:
            stream.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def _kicad_library_paths() -> list[str]:
    """Find shared libraries already loaded by the parent KiCad process."""
    socket = os.environ.get("KICAD_API_SOCKET", "")
    match = re.search(r"api-(\d+)\.sock", socket)
    process_ids = [int(match.group(1))] if match else [os.getppid()]
    directories: set[str] = set()
    for process_id in process_ids:
        try:
            lines = Path(f"/proc/{process_id}/maps").read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            path = line.rsplit(maxsplit=1)[-1]
            if path.startswith("/") and ".so" in path:
                directories.add(str(Path(path).parent))
    for pattern in ("*libxcb-*/lib", "*xcb-util-*/lib"):
        directories.update(str(path) for path in Path("/nix/store").glob(pattern))
    return sorted(directories)


def _enable_linux_compatibility() -> None:
    """Force X11 and, on NixOS, re-exec once with KiCad's loaded libraries."""
    if not sys.platform.startswith("linux"):
        return
    # KiCad launches plugins with QT_QPA_PLATFORM="wayland;xcb"; Qt's Wayland
    # platform plugin segfaults under PySide6 on some compositors, so force X11.
    os.environ["QT_QPA_PLATFORM"] = "xcb"

    if not Path("/etc/NIXOS").exists():
        return
    marker = "KICAD_AUTOROUTER_NIXOS_COMPAT"
    if os.environ.get(marker) == "1":
        return
    environment = os.environ.copy()
    paths = _kicad_library_paths()
    if environment.get("LD_LIBRARY_PATH"):
        paths.append(environment["LD_LIBRARY_PATH"])
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(paths)
    environment[marker] = "1"
    script = str(Path(__file__).resolve())
    os.execve(sys.executable, [sys.executable, script], environment)


def main() -> int:
    _enable_linux_compatibility()
    _log(f"SCRIPT STARTED argv={sys.argv}")
    try:
        from kicad_autorouter.gui import main as run_gui

        _log("import OK, calling main()")
        return run_gui()
    except SystemExit:
        raise
    except BaseException as error:
        _log(f"CRASH: {type(error).__name__}: {error}")
        try:
            with _LOG.open("a") as stream:
                traceback.print_exception(type(error), error, error.__traceback__, file=stream)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
