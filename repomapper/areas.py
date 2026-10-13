"""Hand-written hints from .repomap/areas.toml: importance, status and notes per path."""
import json
import os
import re
import subprocess
import tomllib
from collections import Counter
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional

from repomapper.utils import REPOMAP_DIR

AREAS_FILE = f"{REPOMAP_DIR}/areas.toml"

IMPORTANCE = ("core", "normal", "low")
STATUS = ("active", "legacy", "deprecated")


@dataclass
class Area:
    """One entry; None means "not set here", so the key comes from the nearest ancestor that sets it."""
    importance: Optional[str] = None
    status: Optional[str] = None
    replaced_by: Optional[str] = None
    fold: Optional[bool] = None
    note: Optional[str] = None


CHOICES = {"importance": IMPORTANCE, "status": STATUS}
TYPES = {"importance": str, "status": str, "replaced_by": str, "fold": bool, "note": str}
DEFAULTS = Area(importance="normal", status="active", fold=False)


def normalize(path: str) -> str:
    """Root-relative path with forward slashes and no leading './' or trailing '/'; "" is the root."""
    path = path.replace(os.sep, "/").strip("/")
    while path.startswith("./"):
        path = path[2:]
    return "" if path == "." else path


def ancestors(path: str) -> List[str]:
    """path and every directory above it, nearest first, ending with the root ""."""
    parts = normalize(path).split("/") if normalize(path) else []
    return ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]


@dataclass
class Areas:
    entries: Dict[str, Area] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def resolve(self, path: str) -> Area:
        """Effective settings for path: per key, the nearest entry that sets it, else the default."""
        result = Area()
        for p in ancestors(path):
            entry = self.entries.get(p)
            if entry:
                for f in fields(Area):
                    if getattr(result, f.name) is None:
                        setattr(result, f.name, getattr(entry, f.name))
        for f in fields(Area):
            if getattr(result, f.name) is None:
                setattr(result, f.name, getattr(DEFAULTS, f.name))
        return result

    def covering(self, paths: Iterable[str]) -> List[str]:
        """Entry paths that are paths or ancestors of them and carry a status or note, sorted."""
        found = set()
        for path in paths:
            for p in ancestors(path):
                entry = self.entries.get(p)
                if entry and (entry.status or entry.note or entry.replaced_by):
                    found.add(p)
        return sorted(found)


def annotation(entry: Optional[Area]) -> str:
    """' [legacy, replaced by x] — note' for an entry's own status and note; '' if it sets neither."""
    if not entry:
        return ""
    tags = [entry.status] if entry.status and entry.status != "active" else []
    if entry.replaced_by:
        tags.append(f"replaced by {entry.replaced_by}")
    out = f" [{', '.join(tags)}]" if tags else ""
    if entry.note:
        out += f" — {entry.note}"
    return out


def load_areas(root: str) -> Areas:
    """Load root/.repomap/areas.toml; a missing file means no entries. Bad entries are reported and skipped."""
    areas = Areas()
    try:
        with open(os.path.join(root, AREAS_FILE), "rb") as f:
            table = tomllib.load(f)
    except FileNotFoundError:
        return areas
    except (OSError, tomllib.TOMLDecodeError) as e:
        areas.warnings.append(f"{AREAS_FILE}: {e}; ignoring it")
        return areas

    def warn(message: str):
        areas.warnings.append(f"{AREAS_FILE}: {message}")

    for key in table:
        if key != "paths":
            warn(f"unknown table '{key}'; entries go under [paths.\"some/dir\"]")
    paths = table.get("paths", {})
    if not isinstance(paths, dict):
        warn("'paths' should be a table")
        return areas

    for raw_path, values in paths.items():
        path = normalize(raw_path)
        if not isinstance(values, dict):
            warn(f"paths.\"{raw_path}\" should be a table")
            continue
        if not os.path.exists(os.path.join(root, path)):
            warn(f"paths.\"{raw_path}\" doesn't exist")
        entry = Area()
        for key, value in values.items():
            if key not in TYPES:
                warn(f"paths.\"{raw_path}\": unknown key '{key}'")
            elif not isinstance(value, TYPES[key]):
                warn(f"paths.\"{raw_path}\".{key} should be {TYPES[key].__name__}, got {value!r}")
            elif key in CHOICES and value not in CHOICES[key]:
                warn(f"paths.\"{raw_path}\".{key} should be one of {', '.join(CHOICES[key])}, got {value!r}")
            else:
                setattr(entry, key, value)
        areas.entries[path] = entry
    return areas


# --- Starter entries and staleness, both derived from the tree and git history ---

BIG_AREA_FILES = 100      # top-level dirs with more files also get entries for their subdirectories
IDLE_DAYS = 365           # dirs without commits for this long get a commented "legacy" suggestion
MAX_NOTE_LENGTH = 120


def _git(root: str, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def readme_sentence(directory: str) -> Optional[str]:
    """First sentence of the first prose paragraph in the directory's README or CLAUDE.md."""
    try:
        names = sorted(n for n in os.listdir(directory) if n.upper().startswith("README") or n == "CLAUDE.md")
    except OSError:
        return None
    for name in names:
        try:
            with open(os.path.join(directory, name), encoding="utf-8", errors="ignore") as f:
                text = f.read(20_000)
        except OSError:
            continue
        for para in re.split(r"\n\s*\n", text):
            para = " ".join(para.split())
            # Skip headings, badges, html, lists, code fences and tables
            if not para or para[0] in "#[!<-*|>`=" or para[0].isdigit():
                continue
            sentence = re.split(r"(?<=[.!?])\s", para, maxsplit=1)[0]
            return sentence if len(sentence) <= MAX_NOTE_LENGTH else sentence[:MAX_NOTE_LENGTH - 3] + "..."
    return None


def starter_entries(root: str, files: List[str]) -> str:
    """Commented-out entries for the project's main areas, with hints derived from files and git."""
    tree: Dict[str, List[str]] = {}
    for f in files:
        parts = normalize(os.path.relpath(f, root)).split("/")
        if len(parts) > 1:
            tree.setdefault(parts[0], []).append(f)
            if len(parts) > 2:
                tree.setdefault("/".join(parts[:2]), []).append(f)
    areas = sorted(p for p in tree if "/" not in p or len(tree[p.split("/")[0]]) > BIG_AREA_FILES)
    if not areas:
        return ""

    now = datetime.now(timezone.utc).date()
    blocks = ["", "# Areas found in this project, with derived hints. Uncomment and fill in what's useful."]
    for path in areas:
        langs = Counter(os.path.splitext(f)[1].lstrip(".") or "?" for f in tree[path])
        hint = f"{len(tree[path])} files ({', '.join(f'{l} {n}' for l, n in langs.most_common(3))})"
        last = _git(root, "log", "-1", "--format=%cs", "--", path)
        if last:
            hint += f", last commit {last}"
        lines = ["", f"# {path}: {hint}", f'# [paths."{path}"]', '# importance = "normal"']
        if last and (now - date.fromisoformat(last)).days > IDLE_DAYS:
            lines.append(f'# status = "legacy"  # suggestion only: no commits since {last}')
        note = readme_sentence(os.path.join(root, path))
        lines.append(f"# note = {json.dumps(note, ensure_ascii=False)}" if note else '# note = ""')
        blocks += lines
    return "\n".join(blocks) + "\n"


ENTRY_HEADER = re.compile(r'^\s*\[\s*paths\.(?:"([^"]+)"|\'([^\']+)\'|([\w.-]+))\s*\]')


def stale_entries(root: str, areas: Areas, min_commits: int) -> List[str]:
    """Entries with at least min_commits commits under their path since the entry was last edited."""
    blame = _git(root, "blame", "--line-porcelain", "--", AREAS_FILE)
    if not blame or not areas.entries:
        return []
    times = [int(line.split()[1]) for line in blame.splitlines() if line.startswith("committer-time ")]
    try:
        with open(os.path.join(root, AREAS_FILE), encoding="utf-8") as f:
            text_lines = f.read().splitlines()
    except OSError:
        return []

    # Last edit of each entry: newest blame time among its lines, up to the next table header
    edited: Dict[str, int] = {}
    current = None
    for line, time in zip(text_lines, times):
        if line.lstrip().startswith("["):
            m = ENTRY_HEADER.match(line)
            current = normalize(next(g for g in m.groups() if g)) if m else None
        if current is not None and line.strip() and not line.lstrip().startswith("#"):
            edited[current] = max(edited.get(current, 0), time)

    stale = []
    for path, time in sorted(edited.items()):
        count = _git(root, "rev-list", "--count", f"--since=@{time}", "HEAD", "--", path or ".")
        if count and int(count) >= min_commits:
            when = datetime.fromtimestamp(time, timezone.utc).date().isoformat()
            stale.append(f'{AREAS_FILE}: paths."{path}" was last edited {when}, {count} commits under it since; '
                         "check it's still accurate")
    return stale
