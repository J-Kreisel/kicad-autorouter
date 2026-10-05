#!/usr/bin/env python3
"""Build a platform-specific KiCad Plugin and Content Manager archive."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SOURCE = ROOT / "kicad_plugin"


def _platform_name() -> str:
    names = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}
    try:
        return names[platform.system()]
    except KeyError as error:
        raise RuntimeError(f"Unsupported platform: {platform.system()}") from error


def _architecture() -> str:
    machine = platform.machine().lower()
    return {
        "amd64": "x86_64",
        "x64": "x86_64",
        "aarch64": "arm64",
    }.get(machine, machine)


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as project_file:
        return tomllib.load(project_file)["project"]["version"]


def _build_wheel(output: Path) -> Path:
    subprocess.run(
        ["maturin", "build", "--release", "--out", str(output)],
        cwd=ROOT,
        check=True,
    )
    wheels = sorted(output.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected one wheel, found {len(wheels)}")
    return wheels[0]


def _copy_python_package(wheel: Path, plugins: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        members = [
            member
            for member in archive.infolist()
            if member.filename.startswith("kicad_autorouter/") and not member.is_dir()
        ]
        if not any("_native" in member.filename for member in members):
            raise RuntimeError("Built wheel does not contain the native router extension")
        for member in members:
            destination = plugins / member.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)


def _write_metadata(stage: Path, version: str, platform_name: str) -> None:
    metadata = json.loads((PLUGIN_SOURCE / "metadata.template.json").read_text())
    metadata["versions"] = [
        {
            "version": version,
            "status": "testing",
            "kicad_version": "9.0.5",
            "platforms": [platform_name],
            "runtime": "ipc",
        }
    ]
    (stage / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")


def _zip_tree(stage: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(stage).as_posix())


def build(destination: Path | None = None, wheel: Path | None = None) -> Path:
    version = _project_version()
    platform_name = _platform_name()
    filename = f"kicad-autorouter-{version}-kicad-pcm-{platform_name}-{_architecture()}.zip"
    destination = destination or ROOT / "dist" / filename
    with tempfile.TemporaryDirectory(prefix="kicad-autorouter-pcm-") as temporary:
        temporary_path = Path(temporary)
        wheel = wheel or _build_wheel(temporary_path / "wheels")
        if not wheel.is_file():
            raise FileNotFoundError(wheel)
        stage = temporary_path / "stage"
        plugins = stage / "plugins"
        shutil.copytree(PLUGIN_SOURCE / "icons", plugins / "icons")
        shutil.copytree(PLUGIN_SOURCE / "resources", stage / "resources")
        for name in ("plugin.json", "launch.py", "requirements.txt"):
            shutil.copy2(PLUGIN_SOURCE / name, plugins / name)
        shutil.copy2(ROOT / "LICENSE", plugins / "LICENSE")
        _copy_python_package(wheel, plugins)
        _write_metadata(stage, version, platform_name)
        _zip_tree(stage, destination)
    return destination


def _default_plugin_directory(kicad_version: str) -> Path:
    if sys.platform == "win32":
        documents = Path(os.environ.get("USERPROFILE", Path.home())) / "Documents"
        return documents / "KiCad" / kicad_version / "plugins"
    if sys.platform == "darwin":
        return Path.home() / "Documents" / "KiCad" / kicad_version / "plugins"
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / "kicad" / kicad_version / "plugins"


def install(archive: Path, plugin_root: Path) -> Path:
    destination = plugin_root / "kicad-autorouter"
    temporary = plugin_root / ".kicad-autorouter-installing"
    backup = plugin_root / ".kicad-autorouter-backup"
    if temporary.exists():
        shutil.rmtree(temporary)
    if backup.exists():
        shutil.rmtree(backup)
    temporary.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                if not member.filename.startswith("plugins/") or member.is_dir():
                    continue
                relative = Path(member.filename).relative_to("plugins")
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError(f"Unsafe package path: {member.filename}")
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        if destination.exists():
            destination.replace(backup)
        temporary.replace(destination)
        shutil.rmtree(backup, ignore_errors=True)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        if backup.exists() and not destination.exists():
            backup.replace(destination)
        raise
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="output ZIP path")
    parser.add_argument("--wheel", type=Path, help="use an existing built wheel")
    parser.add_argument("--install", action="store_true", help="install after building")
    parser.add_argument("--kicad-version", default="10.0", help="target KiCad user directory")
    parser.add_argument("--plugin-dir", type=Path, help="override KiCad plugin directory")
    arguments = parser.parse_args()
    archive = build(arguments.output, arguments.wheel)
    print(archive)
    if arguments.install:
        plugin_root = arguments.plugin_dir or _default_plugin_directory(arguments.kicad_version)
        installed = install(archive, plugin_root)
        print(f"Installed to {installed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
