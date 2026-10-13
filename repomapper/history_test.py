import os
import subprocess
from datetime import datetime, timedelta, timezone

from repomapper.config import GitConfig
from repomapper.history import DAY, History, activity_factors, parse_log, read_history

NOW = 1_800_000_000


def commits(*entries):
    """(days ago, files) -> History commits, newest first."""
    return sorted(((NOW - days * DAY, files) for days, files in entries), reverse=True)


def test_parse_log_reads_commit_times_and_files():
    text = "\x01200\nsrc/a.py\nsrc/b.py\n\n\x01100\n\nREADME.md\n"
    assert parse_log(text) == [(200, ["src/a.py", "src/b.py"]), (100, ["README.md"])]


def test_describe_dirs_by_recent_commits_and_last_change():
    h = History(commits((3, ["src/a.py"]), (10, ["src/b.py", "docs/x.md"]), (400, ["old/c.py"])),
                since=NOW - 730 * DAY, now=NOW)
    assert h.describe("src") == "2 commits/90d, last 3d"
    assert h.describe("") == "2 commits/90d, last 3d"
    assert h.describe("old") == "idle 13mo"
    assert h.describe("gone") == "idle >2y"
    assert h.describe("src/a.py") == "1 commit/90d, last 3d"
    assert (h.recent_commits("src"), h.recent_commits("src/a.py"), h.recent_commits("old")) == (2, 1, 0)


def test_bulk_commits_skip_activity_but_count_for_staleness():
    bulk = [f"src/f{i}.py" for i in range(5)]
    h = History(commits((1, bulk), (200, ["src/f0.py"])), since=NOW - 730 * DAY, bulk_files=4, now=NOW)
    assert h.describe("src") == "last 6mo"
    assert h.commits_since("src", NOW - 300 * DAY) == 2
    assert h.commits_since("src/f1.py", NOW - 300 * DAY) == 1
    assert h.commits_since("other", 0) == 0


def test_activity_decays_with_age_and_factors_are_capped():
    h = History(commits((0, ["a.py"]), (90, ["a.py"]), *[(1, ["hot.py"])] * 20), since=0, now=NOW)
    assert abs(h.activity("a.py", 90) - 1.5) < 0.01
    factors = activity_factors(h, ["a.py", "hot.py", "cold.py"], GitConfig(max_activity_boost=3))
    assert abs(factors["a.py"] - 2.5) < 0.01
    assert factors["hot.py"] == 4
    assert "cold.py" not in factors
    assert activity_factors(h, ["a.py"], GitConfig(max_activity_boost=0)) == {}


def git(root, *args, date=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=env)


def test_read_history_is_relative_to_a_subdir_root_and_cached(tmp_path):
    git(tmp_path, "init", "-q")
    old = (datetime.now(timezone.utc) - timedelta(days=1000)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    for rel, date in (("app/src/a.py", old), ("app/src/b.py", None), ("other/c.py", None)):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x = 1\n")
        git(tmp_path, "add", rel)
        git(tmp_path, "commit", "-q", "-m", rel, date=date)

    h = read_history(str(tmp_path / "app"))
    assert [files for _when, files in h.commits] == [["src/b.py"]]   # a.py is outside the 2-year window
    assert (tmp_path / "app" / ".repomap" / "cache" / "history.v1.json").exists()
    assert read_history(str(tmp_path / "app"), GitConfig(history_years=5)).commits[-1][1] == ["src/a.py"]


def test_read_history_without_git_is_none(tmp_path):
    assert read_history(str(tmp_path)) is None
