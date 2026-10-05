import json
import zipfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

SPEC = spec_from_file_location("build_kicad_plugin", "tools/build_kicad_plugin.py")
assert SPEC is not None and SPEC.loader is not None
BUILD_PLUGIN = module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD_PLUGIN)
install = BUILD_PLUGIN.install

REPOSITORY_SPEC = spec_from_file_location("build_pcm_repository", "tools/build_pcm_repository.py")
assert REPOSITORY_SPEC is not None and REPOSITORY_SPEC.loader is not None
BUILD_REPOSITORY = module_from_spec(REPOSITORY_SPEC)
REPOSITORY_SPEC.loader.exec_module(BUILD_REPOSITORY)
build_repository = BUILD_REPOSITORY.build_repository


def test_kicad_plugin_and_pcm_identifiers_match():
    plugin = json.loads(Path("kicad_plugin/plugin.json").read_text())
    metadata = json.loads(Path("kicad_plugin/metadata.template.json").read_text())

    assert plugin["identifier"] == metadata["identifier"]
    assert plugin["runtime"]["type"] == "python"
    assert plugin["actions"][0]["scopes"] == ["pcb"]
    assert metadata["type"] == "plugin"


def test_launcher_does_not_start_gui_when_reimported_by_spawn(tmp_path, monkeypatch):
    import runpy

    monkeypatch.setenv("HOME", str(tmp_path))
    namespace = runpy.run_path("kicad_plugin/launch.py", run_name="__mp_main__")

    assert callable(namespace["main"])
    log = tmp_path / ".local" / "share" / "kicad" / "kicad-autorouter-launch.log"
    assert not log.exists()


def test_manual_installer_atomically_extracts_plugin_payload(tmp_path):
    archive = tmp_path / "plugin.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("metadata.json", "{}")
        package.writestr("plugins/plugin.json", '{"name": "new"}')
        package.writestr("plugins/package/module.py", "value = 1\n")
    destination = tmp_path / "plugins" / "kicad-autorouter"
    destination.mkdir(parents=True)
    (destination / "obsolete.py").write_text("old\n")

    installed = install(archive, tmp_path / "plugins")

    assert installed == destination
    assert json.loads((installed / "plugin.json").read_text()) == {"name": "new"}
    assert (installed / "package" / "module.py").read_text() == "value = 1\n"
    assert not (installed / "obsolete.py").exists()
    assert not (installed / "metadata.json").exists()


def test_manual_installer_rejects_paths_outside_plugin_directory(tmp_path):
    archive = tmp_path / "plugin.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("plugins/../../outside", "unsafe")

    with pytest.raises(ValueError, match="Unsafe package path"):
        install(archive, tmp_path / "plugins")

    assert not (tmp_path / "outside").exists()


def test_pcm_repository_contains_verified_linux_package(tmp_path):
    archives = tmp_path / "archives"
    archives.mkdir()
    archive = archives / "kicad-autorouter-0.1.0-kicad-pcm-linux-x86_64.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("metadata.json", "{}")
        package.writestr("plugins/launch.py", "pass\n")

    output = tmp_path / "repository"
    build_repository(archives, output, timestamp=1_700_000_000)

    repository = json.loads((output / "repository.json").read_text())
    packages = json.loads((output / "packages.json").read_text())
    version = packages["packages"][0]["versions"][0]
    assert repository["schema_version"] == 2
    assert repository["packages"]["update_timestamp"] == 1_700_000_000
    assert version["version"] == "0.1.0"
    assert version["platforms"] == ["linux"]
    assert len(version["download_sha256"]) == 64
    assert (output / archive.name).read_bytes() == archive.read_bytes()
