"""Hand-written hints from .repomap/areas.toml: importance, status and notes per path."""
import os
import tomllib
from dataclasses import dataclass, field, fields
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
