import json
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "simple_unrouted"


def run_drc(board: Path, report: Path) -> dict:
    if shutil.which("kicad-cli") is None:
        pytest.skip("kicad-cli is not installed")
    subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            "--format",
            "json",
            "--output",
            str(report),
            str(board),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(report.read_text())


def test_reference_fixture_closes_ratsnest_without_drc_errors(tmp_path):
    before = run_drc(FIXTURES / "simple_unrouted.kicad_pcb", tmp_path / "before.json")
    after = run_drc(FIXTURES / "expected_routed.kicad_pcb", tmp_path / "after.json")

    assert len(before["unconnected_items"]) == 1
    assert after["unconnected_items"] == []
    assert not [item for item in after["violations"] if item["severity"] == "error"]
