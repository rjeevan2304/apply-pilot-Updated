"""Search rotation: a time budget never starves the queries at the end of the list."""

import pytest

pytest.importorskip("jobspy")
import applypilot.discovery.jobspy as js  # noqa: E402
from applypilot.discovery.companies import title_matches  # noqa: E402


def run(monkeypatch, tmp_path, cfg, stop_after):
    """Run a crawl where the time budget runs out after `stop_after` searches."""
    seen = []
    clock = {"t": 1000.0}

    def fake_search(s, *a, **k):
        seen.append(s["query"])
        if len(seen) >= stop_after:
            clock["t"] = 10_000.0          # past the deadline -> next iteration stops
        return {"new": 0, "existing": 0, "errors": 0}

    monkeypatch.setattr(js, "_run_one_search", fake_search)
    monkeypatch.setattr(js.time, "time", lambda: clock["t"])
    monkeypatch.setattr(js.time, "sleep", lambda s: None)
    monkeypatch.setattr(js.config, "APP_DIR", tmp_path)
    monkeypatch.setattr(js, "init_db", lambda: None)

    class C:
        def execute(self, *a):
            class R:
                def fetchone(self):
                    return [0]
            return R()

    monkeypatch.setattr(js, "get_connection", lambda: C())
    js._full_crawl(search_cfg=dict(cfg, deadline_epoch=5000), sites=["indeed"])
    return seen


def test_rotation_resumes_where_previous_run_stopped(monkeypatch, tmp_path):
    cfg = {"queries": [{"query": q, "tier": 1} for q in "ABCDE"],
           "locations": [{"location": "Hyderabad"}]}
    assert run(monkeypatch, tmp_path, cfg, 2) == ["A", "B"]
    assert run(monkeypatch, tmp_path, cfg, 2) == ["C", "D"]
    assert run(monkeypatch, tmp_path, cfg, 2) == ["E", "A"]   # wraps around


def test_rotation_is_per_region(monkeypatch, tmp_path):
    q = [{"query": x, "tier": 1} for x in "ABC"]
    india = {"queries": q, "locations": [{"location": "Hyderabad"}]}
    uae = {"queries": q, "locations": [{"location": "Dubai"}], "region": "UAE"}
    assert run(monkeypatch, tmp_path, india, 1) == ["A"]
    assert run(monkeypatch, tmp_path, uae, 1) == ["A"]
    assert run(monkeypatch, tmp_path, india, 1) == ["B"]


def test_company_boards_allow_program_and_project_managers():
    kw = ["program", "engineer"]
    assert title_matches("Technical Program Manager", kw)
    assert title_matches("Associate Project Manager", ["project"])
    assert not title_matches("Engineering Manager", kw)


def test_tier_one_searched_first(monkeypatch, tmp_path):
    cfg = {"queries": [{"query": "T2a", "tier": 2}, {"query": "T1", "tier": 1}, {"query": "T2b", "tier": 2}],
           "locations": [{"location": "Hyderabad"}]}
    assert run(monkeypatch, tmp_path, cfg, 3) == ["T1", "T2a", "T2b"]


def test_rotation_restarts_when_search_list_changes(monkeypatch, tmp_path):
    cfg = {"queries": [{"query": q, "tier": 1} for q in "ABCD"], "locations": [{"location": "X"}]}
    assert run(monkeypatch, tmp_path, cfg, 2) == ["A", "B"]
    cfg["queries"].append({"query": "E", "tier": 1})      # list changed -> start from the top
    assert run(monkeypatch, tmp_path, cfg, 2) == ["A", "B"]
    assert run(monkeypatch, tmp_path, cfg, 2) == ["C", "D"]
