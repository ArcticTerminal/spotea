"""Regression coverage for conftest.py's own setup, not the app itself."""

import subprocess
import sys
from pathlib import Path


def test_test_tmpdir_is_cleaned_up_when_the_test_process_exits():
    """conftest.py's _TEST_DIR (tempfile.mkdtemp) used to have no cleanup at
    all — 58 of them had piled up in $TMPDIR before this was noticed, one per
    test run ever since the file was written. mkdtemp itself never removes
    what it creates, and _TEST_DIR is built at import time (before any pytest
    fixture could run), so an atexit hook registered right next to its
    creation is the only thing that can clean it up regardless of how the
    run ends. Verified in a fresh subprocess rather than by inspecting
    conftest._TEST_DIR directly — this process already imported conftest
    once and won't exit until the whole suite is done."""
    script = "import sys; sys.path.insert(0, 'tests'); import conftest; print(conftest._TEST_DIR)"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=".")

    assert result.returncode == 0, result.stderr
    tmp_dir = Path(result.stdout.strip())
    assert not tmp_dir.exists(), f"{tmp_dir} was not cleaned up after the process that created it exited"
