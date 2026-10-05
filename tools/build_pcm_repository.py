#!/usr/bin/env python3
"""Build a KiCad PCM repository feed from release archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SOURCE = ROOT / "kicad_plugin"
RAW_BASE = "https://raw.githubusercontent.com/J-Kreisel/kicad-autorouter/pcm"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _install_size(archive: Path) -> int:
    with zipfile.ZipFile(archive) as package:
        return sum(member.file_size for member in package.infolist())


def build_repository(archives: Path, output: Path, timestamp: int | None = None) -> None:
    linux_archives = sorted(archives.glob("*-kicad-pcm-linux-x86_64.zip"))
    if len(linux_archives) != 1:
        raise RuntimeError(f"Expected one Linux x86-64 PCM archive, found {len(linux_archives)}")

    archive = linux_archives[0]
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, output / archive.name)

    metadata = json.loads((PLUGIN_SOURCE / "metadata.template.json").read_text())
    version = archive.name.split("-kicad-pcm-", 1)[0].removeprefix("kicad-autorouter-")
    metadata["versions"] = [
        {
            "version": version,
            "status": "testing",
            "kicad_version": "9.0.5",
            "platforms": ["linux"],
            "runtime": "ipc",
            "download_url": f"{RAW_BASE}/{archive.name}",
            "download_sha256": _sha256(archive),
            "download_size": archive.stat().st_size,
            "install_size": _install_size(archive),
        }
    ]
    packages_path = output / "packages.json"
    packages_path.write_text(json.dumps({"packages": [metadata]}, indent=2) + "\n")

    updated = int(time.time()) if timestamp is None else timestamp
    repository = {
        "$schema": "https://go.kicad.org/pcm/schemas/v2",
        "name": "J-Kreisel KiCad Plugins",
        "schema_version": 2,
        "packages": {
            "url": f"{RAW_BASE}/packages.json",
            "sha256": _sha256(packages_path),
            "update_timestamp": updated,
            "update_time_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(updated)),
        },
        "maintainer": {
            "name": "Johannes",
            "contact": {"web": "https://github.com/J-Kreisel"},
        },
    }
    (output / "repository.json").write_text(json.dumps(repository, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives", type=Path, default=ROOT / "dist")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "repository")
    arguments = parser.parse_args()
    build_repository(arguments.archives, arguments.output)
    print(arguments.output / "repository.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
