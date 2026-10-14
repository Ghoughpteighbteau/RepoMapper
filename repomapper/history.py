"""
Git history signals: one cached `git log --name-only` pass gives per-file commit times.

Used for recency-weighted PageRank seeds, activity notes on outline lines, and the areas.toml starter hints
and staleness check. Without git (or with no commits) everything falls back to None.
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from repomapper.config import GitConfig
from repomapper.utils import REPOMAP_DIR

HISTORY_CACHE = "history.v1.json"
RECENT_DAYS = 90       # window for "N commits" on outline lines
IDLE_DAYS = 365        # no commits for this long reads as idle
DAY = 86400
COMMIT_MARK = "\x01"   # starts each commit's header line in our log format


@dataclass
class History:
    """Commits within the configured window, newest first; paths are relative to the project root."""
    commits: List[Tuple[int, List[str]]]   # (commit time, files touched)
    since: int                             # start of the window (unix time)
    shallow: bool = False
    bulk_files: int = 50
    now: int = field(default_factory=lambda: int(time.time()))

    def __post_init__(self):
        # Per path (files and every ancestor dir, "" for the root): last commit time and recent commit count.
        # Bulk commits (reformats, renames, bumps) are left out of activity but still count for staleness.
        self._last: Dict[str, int] = {}
        self._recent: Dict[str, int] = {}
        self._file_times: Dict[str, List[int]] = {}
        recent_since = self.now - RECENT_DAYS * DAY
        for when, files in self.commits:
            if len(files) > self.bulk_files:
                continue
            touched = set()
            for f in files:
                self._file_times.setdefault(f, []).append(when)
                parts = f.split("/")
                touched.update("/".join(parts[:i]) for i in range(len(parts) + 1))
            for path in touched:
                self._last.setdefault(path, when)   # newest first, so the first seen is the last commit
                if when >= recent_since:
                    self._recent[path] = self._recent.get(path, 0) + 1

    def last_commit(self, path: str) -> Optional[int]:
        """Time of the newest non-bulk commit touching path (a file or directory), within the window."""
        return self._last.get(path.strip("/"))

    def commits_since(self, path: str, since: int) -> int:
        """Commits (bulk ones included) touching path after since."""
        prefix = path.strip("/")
        return sum(1 for when, files in self.commits if when > since
                   and (not prefix or any(f == prefix or f.startswith(prefix + "/") for f in files)))

    def activity(self, rel_fname: str, half_life_days: float) -> float:
        """Recency-decayed commit count: each commit weighs 0.5 ** (age / half_life)."""
        half_life = max(half_life_days, 1) * DAY
        return sum(0.5 ** ((self.now - when) / half_life) for when in self._file_times.get(rel_fname, ()))

    def recent_commits(self, path: str) -> int:
        """Non-bulk commits touching path in the last RECENT_DAYS."""
        return self._recent.get(path.strip("/"), 0)

    def describe(self, path: str) -> Optional[str]:
        """Short activity note for an outline line: '12 commits/90d, last 3d', 'last 5mo' or 'idle 2y'."""
        last = self.last_commit(path)
        if last is None:
            return None if self.shallow else f"idle >{age((self.now - self.since) // DAY)}"
        days = (self.now - last) // DAY
        recent = self.recent_commits(path)
        if recent:
            return f"{recent} commit{'' if recent == 1 else 's'}/{RECENT_DAYS}d, last {age(days)}"
        return f"idle {age(days)}" if days >= IDLE_DAYS else f"last {age(days)}"

    def summary(self) -> str:
        start = time.strftime("%Y-%m-%d", time.gmtime(self.since))
        n = len(self.commits)
        note = f"{n} commit{'' if n == 1 else 's'} since {start}"
        return note + " (shallow clone: history is incomplete)" if self.shallow else note


def age(days: int) -> str:
    if days < 60:
        return f"{days}d" if days > 0 else "<1d"
    if days < 365 * 2:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def _git(root: str, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def parse_log(text: str) -> List[Tuple[int, List[str]]]:
    commits: List[Tuple[int, List[str]]] = []
    for line in text.splitlines():
        if line.startswith(COMMIT_MARK):
            commits.append((int(line[1:]), []))
        elif line and commits:
            commits[-1][1].append(line)
    return commits


def head_commit(root: str) -> Optional[str]:
    """The commit HEAD points at, None outside a git repo or without commits."""
    head = _git(root, "rev-parse", "HEAD")
    return head.strip() if head and head.strip() else None


def read_history(root: str, settings: GitConfig = GitConfig()) -> Optional[History]:
    """Read (or load from cache) the project's history; None outside a git repo or without commits."""
    head = _git(root, "rev-parse", "HEAD")
    if not head:
        return None
    loaded = _load_commits(os.path.realpath(root), head.strip(), settings.history_years)
    if loaded is None:
        return None
    since, commits, shallow = loaded
    return History(commits, since, shallow, settings.bulk_commit_files)


@lru_cache(maxsize=8)
def _load_commits(root: str, head: str, years: int) -> Optional[Tuple[int, List[Tuple[int, List[str]]], bool]]:
    """Commits since `years` ago, from .repomap/cache/ when HEAD hasn't moved, else from git log."""
    since = int(time.time()) - int(years * 365.25 * DAY)
    shallow = (_git(root, "rev-parse", "--is-shallow-repository") or "").strip() == "true"
    cache_file = os.path.join(root, REPOMAP_DIR, "cache", HISTORY_CACHE)
    key = {"head": head, "years": years}
    try:
        with open(cache_file, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("key") == key:
            return data["since"], [(when, files) for when, files in data["commits"]], shallow
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # --relative limits to the project root and makes paths relative to it, also when it's a subdir
    log = _git(root, "log", "--relative", "--no-renames", "--name-only", f"--format={COMMIT_MARK}%ct",
               f"--since=@{since}", "HEAD", "--", ".")
    if log is None:
        return None
    commits = parse_log(log)
    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"key": key, "since": since, "commits": commits}, f)
    except OSError:
        pass
    return since, commits, shallow


def activity_factors(history: Optional[History], nodes, settings: GitConfig) -> Dict[str, float]:
    """PageRank personalization multipliers: 1 + capped recency-decayed commit count per file."""
    if history is None or settings.max_activity_boost <= 0:
        return {}
    factors = {}
    for node in nodes:
        score = history.activity(node, settings.recency_half_life_days)
        if score:
            factors[node] = 1 + min(score, settings.max_activity_boost)
    return factors
