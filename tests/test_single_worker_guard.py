"""app.main._assert_single_worker — refuses to start under more than one
uvicorn worker.

Download/backfill/import progress (app/progress.py's ProgressRegistry) and
the recommendations build lock are in-process, module-level state; a second
worker is a second OS process with its own copy of each, silently breaking
progress polling and the build lock's serialization with no error anywhere.
This is the one signal that can actually be checked from inside the process
— see the function's own docstring for what it can't catch.
"""

import pytest

from app.main import _assert_single_worker


def test_no_web_concurrency_set_is_fine(monkeypatch):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    _assert_single_worker()  # must not raise


def test_web_concurrency_of_one_is_fine(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    _assert_single_worker()  # must not raise


@pytest.mark.parametrize("value", ["2", "4", "0"])
def test_any_other_web_concurrency_refuses_to_start(monkeypatch, value):
    monkeypatch.setenv("WEB_CONCURRENCY", value)
    with pytest.raises(RuntimeError, match="WEB_CONCURRENCY"):
        _assert_single_worker()
