"""Hand-written hints from .repomap/areas.toml: importance, status and notes per path."""
import functools
import json
import os
import re
import subprocess
import tomllib
from collections import Counter
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from repomapper.config import Config, GitConfig
from repomapper.history import History, read_history
from repomapper.utils import REPOMAP_DIR

AREAS_FILE = f"{REPOMAP_DIR}/areas.toml"

IMPORTANCE = ("core", "normal", "low")
STATUS = ("active", "legacy", "deprecated")
KIND = ("test", "config")


@dataclass
class Area:
    """One entry; None means "not set here", so the key comes from the nearest ancestor that sets it."""
    importance: Optional[str] = None
    status: Optional[str] = None
    replaced_by: Optional[str] = None
    fold: Optional[bool] = None
    note: Optional[str] = None
    kind: Optional[str] = None     # "test" or "config": counted in the unseeded outline, but no names or edges


CHOICES = {"importance": IMPORTANCE, "status": STATUS, "kind": KIND}
TYPES = {"importance": str, "status": str, "replaced_by": str, "fold": bool, "note": str, "kind": str}
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


@functools.lru_cache(maxsize=None)
def glob_regex(pattern: str) -> re.Pattern:
    """Compile an areas.toml glob. Without a slash it matches the file or folder name anywhere; with one it
    matches the whole root-relative path, `*` within one segment and `**` across any number. A trailing `/**`
    also matches the folder itself."""
    pattern = normalize(pattern)
    out = "" if "/" in pattern else "(?:.*/)?"
    for part in re.split(r"(\*\*/|/\*\*$|\*\*|\*|\?)", pattern):
        out += {"**/": "(?:.*/)?", "/**": "(?:/.*)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(part, re.escape(part))
    return re.compile(out + r"\Z")


@dataclass
class Areas:
    entries: Dict[str, Area] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    globs: Dict[str, Area] = field(default_factory=dict)   # pattern -> entry, in file order
    _resolved: Dict[str, Area] = field(default_factory=dict, compare=False, repr=False)

    def chain(self, path: str) -> List[Area]:
        """Entries that apply to path, strongest first: for the path and then each folder above it, its exact
        entry followed by the globs matching it (in file order)."""
        out = []
        for p in ancestors(path):
            if p in self.entries:
                out.append(self.entries[p])
            if p:
                out += [entry for pattern, entry in self.globs.items() if glob_regex(pattern).match(p)]
        return out

    def resolve(self, path: str) -> Area:
        """Effective settings for path: per key, the strongest entry that sets it (see chain), else the default."""
        if path in self._resolved:
            return self._resolved[path]
        result = Area()
        for entry in self.chain(path):
            for f in fields(Area):
                if getattr(result, f.name) is None:
                    setattr(result, f.name, getattr(entry, f.name))
        for f in fields(Area):
            if getattr(result, f.name) is None:
                setattr(result, f.name, getattr(DEFAULTS, f.name))
        self._resolved[path] = result
        return result


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
        if key not in ("paths", "globs"):
            warn(f"unknown table '{key}'; entries go under [paths.\"some/dir\"] or [globs.\"*_test.*\"]")

    for section, target in (("paths", areas.entries), ("globs", areas.globs)):
        raw_entries = table.get(section, {})
        if not isinstance(raw_entries, dict):
            warn(f"'{section}' should be a table")
            continue
        for raw_path, values in raw_entries.items():
            name = f'{section}."{raw_path}"'
            if not isinstance(values, dict):
                warn(f"{name} should be a table")
                continue
            if section == "paths" and not os.path.exists(os.path.join(root, normalize(raw_path))):
                warn(f"{name} doesn't exist")
            entry = Area()
            for key, value in values.items():
                if key not in TYPES:
                    warn(f"{name}: unknown key '{key}'")
                elif not isinstance(value, TYPES[key]):
                    warn(f"{name}.{key} should be {TYPES[key].__name__}, got {value!r}")
                elif key in CHOICES and value not in CHOICES[key]:
                    warn(f"{name}.{key} should be one of {', '.join(CHOICES[key])}, got {value!r}")
                else:
                    setattr(entry, key, value)
            target[normalize(raw_path) if section == "paths" else raw_path] = entry
    return areas


# Starter test globs, written into new areas.toml files and suggested for old [tests] sections
TEST_GLOBS = ("*_test.*", "test_*.*", "*.spec.*", "*.test.*", "*_spec.*", "*-spec.*", "*Test.java",
              "*Tests.java", "*Test.kt", "**/tests/**", "**/src/test/**", "**/__tests__/**", "conftest.py")
TEST_GLOB_ENTRIES = "\n".join(f'[globs."{g}"]\nimportance = "low"\nkind = "test"\n' for g in TEST_GLOBS)


def moved_tests_hint(config: Config, areas: Areas) -> List[str]:
    """For a config.toml still holding the old [tests] section, while areas.toml has no kind = "test" globs:
    the starter entries to paste into areas.toml."""
    if not config.stale_tests or any(entry.kind == "test" for entry in areas.globs.values()):
        return []
    return [f"{AREAS_FILE}: no kind = \"test\" globs yet; starter entries to paste:\n\n{TEST_GLOB_ENTRIES}"]


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


# Sentences that address readers or agents, describe the docs themselves, or are template boilerplate: a
# starter note should say what the code is, so suggesting nothing beats these.
NOT_A_DESCRIPTION = re.compile(
    r"(?:guidance|guidelines|instructions|notes|names|conventions|rules|tips|steps)\s+(?:for|on|in|about|to)\b"
    r"|(?:this|the)\s+(?:file|document|doc|readme|guide|page|folder|directory)\b"
    r"|(?:this|the)\s+(?:repo|repository|project)\s+(?:was|is)\s+(?:bootstrapped|generated|created|a\s+template)"
    r"|(?:always|never|please|do\s+not|don't|make\s+sure|ensure|remember|note|see|read|use|run|install|follow|when|if|before|after|to)\b"
    r"|welcome\b|getting\s+started\b|table\s+of\s+contents\b|todo\b"
    r"|[\w\s]{1,40}\s[-–—:]\s",   # a title with a tagline: "Email Service - ..."
    re.I)


def readme_sentence(directory: str) -> Optional[str]:
    """First sentence of the opening prose in the directory's README or CLAUDE.md, or None if it has none.

    Headings (# or underlined), badges, html, lists, code and tables are skipped, as are title-ish lines and
    fragments (fewer than three words, or not ending in . ! ?: "Email Service -", "Type: `x`"). Prose under a
    heading that follows other content belongs to a section, not the intro, so the search stops at that
    heading ("## Glossary" / "Names in this codebase that..."). Sentences that give guidance or instructions,
    describe the docs, or are boilerplate (NOT_A_DESCRIPTION) are skipped. READMEs are read before CLAUDE.md,
    which is mostly written for agents."""
    try:
        names = sorted((n for n in os.listdir(directory) if n.upper().startswith("README") or n == "CLAUDE.md"),
                       key=lambda n: (n == "CLAUDE.md", n))
    except OSError:
        return None
    for name in names:
        try:
            with open(os.path.join(directory, name), encoding="utf-8", errors="ignore") as f:
                text = f.read(20_000)
        except OSError:
            continue
        seen_content = False
        for raw in re.split(r"\n\s*\n", text):
            lines = raw.strip().splitlines()
            if not lines:
                continue
            setext = len(lines) > 1 and re.fullmatch(r"\s*(=+|-+)\s*", lines[-1])
            if lines[0].startswith("#") or setext:
                if seen_content:
                    break
                continue
            para = " ".join(raw.split())
            if para[0] in "[!<":   # badges and html
                continue
            seen_content = True
            if para[0] in "-*|>`=" or para[0].isdigit():   # lists, code fences and tables
                continue
            sentence = re.split(r"(?<=[.!?])\s", para, maxsplit=1)[0]
            if len(sentence.split()) < 3 or not re.search(r"[.!?]$", sentence) or NOT_A_DESCRIPTION.match(sentence):
                continue
            return sentence if len(sentence) <= MAX_NOTE_LENGTH else sentence[:MAX_NOTE_LENGTH - 3] + "..."
    return None


def starter_entries(root: str, files: List[str], history: Optional[History] = None) -> str:
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

    if history is None:
        history = read_history(root)
    now = datetime.now(timezone.utc).date()
    blocks = ["", "# Areas found in this project, with derived hints. Uncomment and fill in what's useful."]
    for path in areas:
        langs = Counter(os.path.splitext(f)[1].lstrip(".") or "?" for f in tree[path])
        hint = f"{len(tree[path])} files ({', '.join(f'{l} {n}' for l, n in langs.most_common(3))})"
        when = history.last_commit(path) if history else None
        # Areas idle for longer than the history window (usually few) look up their last commit directly
        last = (datetime.fromtimestamp(when, timezone.utc).date().isoformat() if when
                else history and _git(root, "log", "-1", "--format=%cs", "--", path))
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


def stale_entries(root: str, areas: Areas, settings: GitConfig = GitConfig(),
                  history: Optional[History] = None) -> List[str]:
    """Entries with at least stale_area_commits commits under their path since the entry was last edited.

    Counts come from the history window, so an entry edited before it is judged on the window alone."""
    if not areas.entries:
        return []
    if history is None:
        history = read_history(root, settings)
    if history is None:
        return []
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
        count = history.commits_since(path, time)
        if count >= settings.stale_area_commits:
            when = datetime.fromtimestamp(time, timezone.utc).date().isoformat()
            stale.append(f'{AREAS_FILE}: paths."{path}" was last edited {when}, {count} commits under it since; '
                         "check it's still accurate")
    return stale



def test_file_checker(areas: Areas):
    """Predicate for test files: areas.toml kind = "test" when it sets any, else the starter test globs."""
    if any(a.kind == "test" for a in [*areas.entries.values(), *areas.globs.values()]):
        return lambda rel: areas.resolve(rel).kind == "test"
    regexes = [glob_regex(g) for g in TEST_GLOBS]
    return lambda rel: any(r.match(rel) for r in regexes)
