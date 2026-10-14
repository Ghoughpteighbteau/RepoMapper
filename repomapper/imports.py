"""Import edges for JS/TS: which module specifiers a file imports, and which repo files they point at.

Specifiers come from a conservative regex (static `import`/`export ... from`, side-effect `import '...'`, `require()` and
dynamic `import()` with a string literal; only statements starting a line, so strings and trailing comments don't match).
Resolution needs no tsconfig: relative specifiers try the usual extensions and index files; workspace package names
(`@opsready/types`) go to the directory of the package.json with that name, via its types/main entry or `src/index`.
A package entry points at build output (`dist/index.js`, usually not in the repo), so a first directory like dist/ or
lib/ is also tried as `src/`. An import of a file that is not in the repo map because it is a barrel (only re-exports) resolves to the files the
barrel re-exports, followed through nested barrels.
"""
import json
import posixpath
import re
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from repomapper.utils import is_barrel

EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
BUILD_DIRS = ("dist", "build", "lib", "out")
ENTRY_FIELDS = ("types", "typings", "module", "main")
MAX_BARREL_DEPTH = 6

IMPORT_RE = re.compile(
    r"""(?:^[ \t]*(?:import|export)\b(?:(?!\b(?:import|export)\b)[^'"`;{}]|\{[^{}]*\})*?\bfrom\s*"""
    r"""|^[ \t]*import\s*"""
    r"""|\brequire\s*\(\s*"""
    r"""|\bimport\s*\(\s*)(['"])([^'"\n]+)\1""",
    re.M,
)


def extract_imports(code: str) -> List[Tuple[str, int]]:
    """(module specifier, line) of every import, re-export, require and dynamic import in code."""
    out = []
    for m in IMPORT_RE.finditer(code):
        start = m.start() + len(m.group(0)) - len(m.group(0).lstrip())
        out.append((m.group(2), code.count("\n", 0, start) + 1))
    return out


def load_packages(root: str, rel_files: Iterable[str]) -> Dict[str, Tuple[str, Dict[str, str]]]:
    """package name -> (its directory relative to root, its entry fields), from the package.json files among rel_files."""
    packages = {}
    for rel in rel_files:
        if posixpath.basename(rel) != "package.json":
            continue
        try:
            with open(posixpath.join(root, rel), encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict) or not isinstance(manifest.get("name"), str) or not manifest["name"]:
            continue
        entries = {k: manifest[k] for k in ENTRY_FIELDS if isinstance(manifest.get(k), str)}
        packages[manifest["name"]] = (posixpath.dirname(rel), entries)
    return packages


class ImportResolver:
    """Resolves import specifiers to files of the repo map. `read(rel)` gives the text of a file that exists on disk
    (None if missing), for barrels the map leaves out."""

    def __init__(self, files: Set[str], read: Callable[[str], Optional[str]],
                 packages: Dict[str, Tuple[str, Dict[str, str]]]):
        self.files = files
        self.read = read
        self.packages = packages
        self.cache: Dict[Tuple[str, str], List[str]] = {}

    def resolve(self, importer: str, spec: str) -> List[str]:
        """Files `spec` points at when imported from importer (both root-relative); [] for external modules."""
        relative = spec.startswith(".")
        key = (posixpath.dirname(importer) if relative else "", spec)
        if key not in self.cache:
            self.cache[key] = self.resolve_relative(key[0], spec) if relative else self.resolve_package(spec)
        return self.cache[key]

    def resolve_relative(self, directory: str, spec: str) -> List[str]:
        base = posixpath.normpath(posixpath.join(directory, spec))
        return [] if base.startswith("..") else self.resolve_path(base, set())

    def resolve_path(self, base: str, seen: Set[str]) -> List[str]:
        """base as a file (as written, with an extension, or as a directory index), expanding barrels."""
        candidates = [base]
        candidates += [base + ext for ext in EXTENSIONS]
        candidates += [posixpath.join(base, "index" + ext) for ext in EXTENSIONS]
        stem, ext = posixpath.splitext(base)
        if ext in (".js", ".jsx", ".mjs", ".cjs"):   # ESM-style TypeScript imports name the compiled file
            candidates += [stem + e for e in (".ts", ".tsx", ".mts", ".cts")]
        for path in candidates:
            if path in self.files:
                return [path]
        for path in candidates:
            if path in seen or not path.endswith(EXTENSIONS):
                continue
            code = self.read(path)
            if code is not None and is_barrel(path, code):
                return self.expand_barrel(path, code, seen)
        return []

    def expand_barrel(self, path: str, code: str, seen: Set[str]) -> List[str]:
        seen = seen | {path}
        out: List[str] = []
        if len(seen) > MAX_BARREL_DEPTH:
            return out
        for spec, _line in extract_imports(code):
            if spec.startswith("."):
                base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
                found = [] if base.startswith("..") else self.resolve_path(base, seen)
            else:
                found = self.resolve_package(spec, seen)
            out += [f for f in found if f not in out]
        return out

    def resolve_package(self, spec: str, seen: Set[str] = frozenset()) -> List[str]:
        parts = spec.split("/")
        name = "/".join(parts[:2] if spec.startswith("@") else parts[:1])
        if name not in self.packages:
            return []
        directory, entries = self.packages[name]
        rest = spec[len(name) + 1:]
        if rest:
            bases = [posixpath.join(directory, "src", rest), posixpath.join(directory, rest)]
        else:
            bases = []
            for field in ENTRY_FIELDS:
                if field not in entries:
                    continue
                entry = re.sub(r"\.d\.[cm]?ts$|\.[cm]?[jt]sx?$", "", posixpath.normpath(entries[field]))
                head, _, tail = entry.partition("/")
                if tail and head in BUILD_DIRS:
                    bases.append(posixpath.join(directory, "src", tail))
                bases.append(posixpath.join(directory, entry))
            bases += [posixpath.join(directory, "src", "index"), posixpath.join(directory, "index")]
        for base in bases:
            found = self.resolve_path(base, set(seen))
            if found:
                return found
        return []
