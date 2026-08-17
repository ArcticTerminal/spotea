"""GET /feeds/import/{job_id}/status reading a progress dict a background
thread is still writing to (routers/feeds.py's get_bulk_import_status).

ProgressRegistry's lock (app/progress.py) only guards the registry's own
set/get/sweep bookkeeping — the dict it hands back via get() is the exact
same mutable object run_bulk_import's worker thread keeps appending to.
Iterating progress["results"] directly while a plain Python list is being
appended to picks up the newly appended item mid-loop (this is real,
observable list behavior, not a hypothetical) — the fix takes one snapshot
copy before building any response objects, immune to whatever the worker
does afterward.
"""

import app.routers.feeds as feeds_router
from app.progress import ProgressRegistry
from app.schemas import BulkImportResultOut


def test_a_result_appended_mid_read_does_not_appear_in_that_same_read(monkeypatch):
    """Simulates the actual race deterministically instead of relying on
    thread timing: constructing the first BulkImportResultOut appends a new
    raw entry to the *same* progress dict the endpoint is still reading from
    — standing in for run_bulk_import's worker thread doing exactly that
    between two entries of a poll that arrived mid-job."""
    progress = {
        "total": 5,
        "resolved": 2,
        "done": 2,
        "results": [
            {"url": "https://a.example", "status": "added", "channel_title": "A", "error": None},
        ],
    }
    registry: ProgressRegistry[str, dict] = ProgressRegistry()
    registry.set("job1", progress)
    monkeypatch.setattr(feeds_router, "import_progress", registry)

    real_init = BulkImportResultOut.__init__
    injected = {"done": False}

    def appending_init(self, **kwargs):
        if not injected["done"]:
            injected["done"] = True
            # The worker thread's next line of real work, landing squarely
            # inside this read.
            progress["results"].append(
                {"url": "https://late.example", "status": "added", "channel_title": "B", "error": None}
            )
        real_init(self, **kwargs)

    monkeypatch.setattr(BulkImportResultOut, "__init__", appending_init)

    result = feeds_router.get_bulk_import_status("job1")

    assert [r.url for r in result.results] == ["https://a.example"]
    # The live dict genuinely has two entries now — a later poll will see
    # both. This read's own response just can't have been built from a
    # half-appended-to list.
    assert len(progress["results"]) == 2
