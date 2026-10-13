"""
Directory outline: the repo map shown when there's no context to zoom in on.

Directories are folded to one summary line and opened by share of PageRank, largest first, while the
result fits the token budget. Edges between the displayed nodes show which areas depend on which.
"""

import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from repomapper.areas import Areas, annotation
from repomapper.config import OutlineConfig

DEP_LINE_TOKENS = 20   # budget reserved per dependency line while choosing which directories to open

HEADER = (
    "Outline (no chat_files, mentioned_files or mentioned_idents given): directories folded by share of "
    "PageRank. Pass files or identifiers to see signatures."
)


@dataclass
class OutlineFile:
    rank: float                       # File PageRank
    defs: List[Tuple[float, str]]     # (rank, def name), highest first


@dataclass
class Dir:
    path: str                                          # "" for the root, else "a/b"
    dirs: Dict[str, "Dir"] = field(default_factory=dict)
    files: List[str] = field(default_factory=list)     # rel paths directly inside, highest rank first
    count: int = 0
    rank: float = 0.0
    langs: Counter = field(default_factory=Counter)
    defs: List[Tuple[float, str]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return (self.path.rsplit("/", 1)[-1] if self.path else ".") + "/"


def build_tree(files: Dict[str, OutlineFile]) -> Dir:
    root = Dir("")
    for rel in files:
        node = root
        parts = rel.split("/")
        for i, part in enumerate(parts[:-1]):
            node = node.dirs.setdefault(part, Dir("/".join(parts[:i + 1])))
        node.files.append(rel)

    def total(d: Dir):
        d.files.sort(key=lambda f: files[f].rank, reverse=True)
        for f in d.files:
            d.count += 1
            d.rank += files[f].rank
            d.langs[os.path.splitext(f)[1].lstrip(".") or "?"] += 1
            d.defs.extend(files[f].defs)
        for sub in d.dirs.values():
            total(sub)
            d.count += sub.count
            d.rank += sub.rank
            d.langs.update(sub.langs)
            d.defs.extend(sub.defs)
        d.defs.sort(reverse=True)

    total(root)
    return root


def top_names(defs: List[Tuple[float, str]], n: int) -> List[str]:
    names: List[str] = []
    for _rank, name in defs:
        if name not in names:
            names.append(name)
            if len(names) == n:
                break
    return names


def pct(rank: float) -> str:
    return f"{rank * 100:.1f}%"


def render_outline(
    files: Dict[str, OutlineFile],
    edges: Dict[Tuple[str, str], float],
    max_tokens: int,
    token_count: Callable[[str], int],
    docs: Callable[[str], List[str]] = lambda rel_dir: [],
    settings: OutlineConfig = OutlineConfig(),
    areas: Areas = Areas(),
) -> Optional[str]:
    """Render the outline, opening the highest-ranked directories while it fits max_tokens."""
    if not files:
        return None
    files_per_dir, max_edges = settings.files_per_dir, settings.max_edges
    root = build_tree(files)
    doc_cache: Dict[str, List[str]] = {}

    def doc_note(d: Dir) -> str:
        if d.path not in doc_cache:
            doc_cache[d.path] = docs(d.path)
        docs_note = f"  [{', '.join(doc_cache[d.path])}]" if doc_cache[d.path] else ""
        return docs_note + annotation(areas.entries.get(d.path))

    def summary(d: Dir) -> str:
        langs = ", ".join(f"{lang} {n}" for lang, n in d.langs.most_common(settings.max_langs))
        return f"{d.label}  {d.count} file{'' if d.count == 1 else 's'} ({langs}) {pct(d.rank)}"

    def render(open_dirs: Set[str], deps: List[Tuple[str, str]] = ()) -> str:
        lines = [HEADER, ""]

        def walk(d: Dir, depth: int):
            indent = "  " * depth
            if d.path not in open_dirs:
                names = top_names(d.defs, settings.defs_per_file)
                lines.append(f"{indent}{summary(d)}: {', '.join(names)}{doc_note(d)}" if names
                             else f"{indent}{summary(d)}{doc_note(d)}")
                return
            lines.append(f"{indent}{summary(d)}{doc_note(d)}")
            for sub in sorted(d.dirs.values(), key=lambda s: s.rank, reverse=True):
                walk(sub, depth + 1)
            for f in d.files[:files_per_dir]:
                names = ", ".join(top_names(files[f].defs, settings.defs_per_file))
                lines.append(f"{indent}  {f.rsplit('/', 1)[-1]} {pct(files[f].rank)}" + (f": {names}" if names else "")
                             + annotation(areas.entries.get(f)))
            if len(d.files) > files_per_dir:
                lines.append(f"{indent}  +{len(d.files) - files_per_dir} more files")

        walk(root, 0)
        if deps:
            lines += ["", "Dependencies (strongest references first):"] + [f"  {a} -> {b}" for a, b in deps]
        return "\n".join(lines)

    def node_of(rel: str, open_dirs: Set[str]) -> str:
        """The displayed node a file belongs to: itself when listed, else its innermost displayed directory."""
        d = root
        parts = rel.split("/")
        for part in parts[:-1]:
            if d.path not in open_dirs:
                return d.path + "/" if d.path else "./"
            d = d.dirs[part]
        if d.path not in open_dirs:
            return d.path + "/"
        if rel in d.files[:files_per_dir]:
            return rel
        return d.path + "/" if d.path else "./"

    def dependencies(open_dirs: Set[str]) -> List[Tuple[str, str]]:
        weights: Dict[Tuple[str, str], float] = defaultdict(float)
        for (src, dst), w in edges.items():
            a, b = node_of(src, open_dirs), node_of(dst, open_dirs)
            if a != b:
                weights[(a, b)] += w
        return [pair for pair, _w in sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:max_edges]]

    def opened(open_dirs: Set[str], d: Dir) -> Set[str]:
        # Open chains of single-subdirectory directories together; they'd only add a line each
        result = open_dirs | {d.path}
        while len(d.dirs) == 1 and not d.files:
            d = next(iter(d.dirs.values()))
            if areas.resolve(d.path).fold:
                break
            result.add(d.path)
        return result

    def with_dependencies(open_dirs: Set[str]) -> str:
        # Aggregating edges is costly on big repos, so it runs once for the chosen layout
        deps = dependencies(open_dirs)
        out = render(open_dirs, deps)
        while deps and token_count(out) > max_tokens:
            deps = deps[:-1]
            out = render(open_dirs, deps)
        return out

    tree_budget = max_tokens - min(max_edges * DEP_LINE_TOKENS, max_tokens // 5)
    open_dirs = opened(set(), root)
    failed: Set[str] = set()
    while True:
        def visible(d: Dir):
            for sub in d.dirs.values():
                if sub.path in open_dirs:
                    yield from visible(sub)
                elif sub.path not in failed and not areas.resolve(sub.path).fold:
                    yield sub
        for cand in sorted(visible(root), key=lambda s: s.rank, reverse=True):
            trial_dirs = opened(open_dirs, cand)
            if token_count(render(trial_dirs)) <= tree_budget:
                open_dirs = trial_dirs
                break
            failed.add(cand.path)
        else:
            return with_dependencies(open_dirs)
