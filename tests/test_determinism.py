"""Output must not depend on hash ordering or the platform's line endings."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(seed: str, out: Path) -> dict[str, bytes]:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    for args in (
        ["map", "--output", str(out / "map")],
        ["evaluate", "ablation", "--output", str(out / "ablation")],
    ):
        subprocess.run(
            [sys.executable, "-m", "assay.cli", *args],
            cwd=ROOT, env=env, check=True, capture_output=True,
        )
    return {
        str(path.relative_to(out)): path.read_bytes()
        for path in sorted(out.rglob("*"))
        if path.is_file()
    }


def test_reports_are_identical_under_different_hash_seeds(tmp_path):
    first = _run("1", tmp_path / "a")
    second = _run("12345", tmp_path / "b")

    assert first and first == second


def test_written_reports_use_unix_line_endings(tmp_path):
    files = _run("0", tmp_path)

    assert files
    assert all(b"\r" not in content for content in files.values())
