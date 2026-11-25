"""Pure parts of birddog/book_batches.py (no network, no database): request ids,
estimates at batch prices, budget planning, result handling with fake results."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from birddog import book_batches as bb
from birddog import books


def test_custom_id_is_valid_and_unique():
    ids = {bb.custom_id(d, 2581, p, ph) for d in bb.DAGS for p in ([1, 2, 3, 4], [5]) for ph in bb.PHASES}
    assert len(ids) == 8
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", i) for i in ids)
    assert bb.custom_id("read_books", 2581, [3, 4], "reread") == "r-2581-p3-4-reread"


def test_chunk_pages():
    assert bb.chunk_pages([1, 2, 3, 4, 5, 9], 4) == [[1, 2, 3, 4], [5], [9]]
    assert bb.chunk_pages([7, 3, 4], 2) == [[3, 4], [7]]
    assert bb.chunk_pages([], 4) == []


def test_estimates_at_batch_prices():
    # places: the calibrated $0.013 a page (Sonnet + expected re-reads), halved
    per_page = bb.est_book_cost("read_books", 100) / 100
    assert per_page == pytest.approx(books.estimate(100, 4175)[1] / 100 / 2, rel=0.05)
    # transcription: Sonnet $0.0256/page measured, halved
    assert bb.est_cost("transcribe_books", "first", 1) == pytest.approx(0.0256 / 2, rel=0.1)
    # Opus names re-read: $0.1256/page measured, halved
    assert bb.est_cost("transcribe_books", "reread", 1) == pytest.approx(0.1256 / 2, rel=0.1)
    assert bb.est_cost("read_books", "first", 4, 6000) > bb.est_cost("read_books", "first", 4, 3000)


def test_plan_requests_respects_budget():
    work = [(1, "File:A.pdf", list(range(1, 9)), 4175), (2, "File:B.pdf", [1, 2, 3], None)]
    one = bb.est_cost("read_books", "first", 4, 4175)
    picked, total, left = bb.plan_requests("read_books", "first", work, remaining_usd=one * 2.5)
    assert [(d, p) for d, _, p, _ in picked] == [(1, [1, 2, 3, 4]), (1, [5, 6, 7, 8])]
    assert left == 1 and total <= one * 2.5
    picked, _, left = bb.plan_requests("read_books", "first", work, remaining_usd=0)
    assert picked == [] and left == 3


def _message(text, stop="end_turn", tokens=(1000, 200)):
    return SimpleNamespace(model=books.READ_MODEL, stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]),
                           content=[SimpleNamespace(type="text", text=text)])


def _result(kind, message=None, error_type=None):
    error = SimpleNamespace(error=SimpleNamespace(type=error_type, message="bad")) if error_type else None
    return SimpleNamespace(custom_id="x", result=SimpleNamespace(type=kind, message=message, error=error))


def test_handle_success_saves_at_batch_price(monkeypatch):
    saved = []
    monkeypatch.setattr(bb, "_save", lambda *a: saved.append(a))
    body = json.dumps({"pages": [{"page": 3, "illegible": False, "places": [], "notes": ""}]})
    status, error, cost = bb._handle("read_books", "first", "run", (7, [3, 4], books.READ_MODEL),
                                     _result("succeeded", _message(body)))
    assert status == "succeeded" and error is None
    assert cost[2] == pytest.approx(books.cost_usd(books.READ_MODEL, 1000, 200) / 2)
    by_page = saved[0][4]
    assert set(by_page) == {3, 4} and by_page[4]["illegible"] is True     # page 4 not returned


def test_handle_refusal_retries_synchronously(monkeypatch):
    saved = []
    monkeypatch.setattr(bb, "_save", lambda *a: saved.append(a))
    monkeypatch.setattr(bb, "_sync_retry", lambda *a: ({5: {"page": 5}}, 900, 100, 0.01))
    status, error, cost = bb._handle("transcribe_books", "reread", "run", (7, [5], books.REREAD_MODEL),
                                     _result("succeeded", _message("", stop="refusal")))
    assert status == "refused" and saved and cost[0] == 1000 + 900


def test_handle_cut_off_fails_book(monkeypatch):
    failed = []
    monkeypatch.setattr(bb, "_fail_book", lambda *a: failed.append(a))
    status, _, cost = bb._handle("read_books", "first", "run", (7, [1], books.READ_MODEL),
                                 _result("succeeded", _message("{", stop="max_tokens")))
    assert status == "failed" and failed and cost[2] > 0


def test_handle_errors(monkeypatch):
    failed = []
    monkeypatch.setattr(bb, "_fail_book", lambda *a: failed.append(a))
    assert bb._handle("read_books", "first", "run", (7, [1], "m"), _result("expired"))[0] == "expired"
    assert not failed                                       # pages simply go again next run
    assert bb._handle("read_books", "first", "run", (7, [1], "m"),
                      _result("errored", error_type="invalid_request_error"))[0] == "errored"
    assert failed                                           # a bad request would fail every run


def test_batch_params_have_no_fallbacks(monkeypatch):
    monkeypatch.setattr(books, "page_image", lambda title, p: b"\xff\xd8jpeg")
    params = books.read_params("File:A.pdf", [1, 2], books.READ_MODEL)
    assert "fallbacks" not in params and "betas" not in params
    assert params["output_config"]["effort"] == "medium"
    from birddog import book_transcripts as bt
    params = bt.transcribe_params("File:A.pdf", [1], books.REREAD_MODEL, names=True)
    assert "BEST reading" in params["messages"][0]["content"][-1]["text"]
    assert params["max_tokens"] == bt.TRANSCRIBE_MAX_TOKENS


def test_dags_have_batch_shape():
    spec = yaml.safe_load((Path(__file__).resolve().parent.parent / "dags/yaml/books.yml").read_text())
    from birddog import dag_tasks_books
    for dag_id, first in (("read_books", "read"), ("transcribe_books", "transcribe")):
        dag = spec[dag_id]
        assert dag["params"]["use_batch"] is True
        tasks = {t["task_id"]: t for t in dag["tasks"]}
        for tid in (f"{first}_wait", "reread_wait"):
            assert tasks[tid]["decorator"] == "airflow.providers.standard.decorators.sensor.sensor_task"
            assert tasks[tid]["mode"] == "reschedule" and tasks[tid]["timeout"] >= 24 * 3600
        assert tasks["reread_submit"]["dependencies"] == [f"{first}_collect"]
        for t in tasks.values():
            assert callable(getattr(dag_tasks_books, t["python_callable"].rsplit(".", 1)[1]))


def test_sync_mode_tasks_do_nothing_but_the_first_submit(monkeypatch):
    from birddog import dag_tasks_books as tasks
    monkeypatch.setattr(tasks, "_require_key", lambda: None)
    assert tasks.batch_wait(pipeline="read_books", use_batch="False") is True
    assert tasks.batch_collect(pipeline="read_books", phase="first", use_batch="false") == {"skipped": "synchronous run"}
    assert tasks.batch_submit(pipeline="transcribe_books", phase="reread", use_batch="false") == {"skipped": "synchronous run"}
    with pytest.raises(ValueError):
        tasks.batch_wait(pipeline="other", use_batch="true")
