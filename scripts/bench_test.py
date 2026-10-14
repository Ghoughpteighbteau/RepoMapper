import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("bench", Path(__file__).with_name("bench.py"))
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def test_summarize_and_names():
    s = bench.summarize([3.0, 1.0, 2.0])
    assert s["median"] == 2.0 and s["min"] == 1.0 and s["runs"] == [3.0, 1.0, 2.0]
    assert bench.result_name("2026-10-14", "abc1234", True) == "2026-10-14-abc1234-dirty.json"
    assert bench.result_name("2026-10-14", "abc1234", False, ["services"]) == "2026-10-14-abc1234-services.json"


def test_compare_table_flags_regressions_and_missing():
    a = {"meta": {}, "this": {"time.x": {"median": 1.0}, "size.m@4096": {"tokens": 100, "lines": 10}, "time.gone": {"median": 1}}}
    b = {"meta": {}, "this": {"time.x": {"median": 1.5}, "size.m@4096": {"tokens": 90, "lines": 10}, "time.new": {"median": 1}}}
    table = bench.compare_table(a, b)
    assert "+50.0%" in table and "REGRESSION" in table
    assert "-10.0%" in table and table.count("REGRESSION") == 1
    assert "only in A" in table and "only in B" in table


def test_smoke_run_on_tmp_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def alpha():\n    return beta()\n")
    (repo / "b.py").write_text("def beta():\n    return 1\n")
    monkeypatch.setattr(bench, "REPO_ROOT", repo)
    monkeypatch.setitem(bench.CASES, "this", {"chat": "a.py", "dir": "nodir", "idents": ["beta"], "search": "beta"})
    out = tmp_path / "out.json"
    assert bench.main(["--repo", "this", "--runs", "1", "--out", str(out)]) == 0
    data = json.loads(out.read_text())
    assert "time.seeded_chat" in data["this"] and "time.seeded_dir" not in data["this"]
    assert data["this"]["size.search"]["more_hits"] == 0
    assert bench.main(["--compare", str(out), str(out)]) == 0
