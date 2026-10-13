"""
Directory outline: the repo map shown when there's no context to zoom in on.

The token budget is split top-down: every top-level area gets enough to open one level, the rest goes to
siblings by log(files) times areas.toml importance, recursively; a directory whose share can't hold its
own lines stays folded to one summary line. Share of PageRank is shown on each line. Edges between the
displayed nodes show which areas depend on which: `a <-> b` when both ways, and labels relative to the
deepest common directory (`lib/types/: task.ts -> user.ts`). A directory's edges to its own listed files
are left out.

Files areas.toml marks with a kind (tests, configs) count toward their directory, shown as `(py 40; 38 test)`,
but give no sample names or edges, and a directory of nothing else stays folded. Sample names skip names defined in many directories (ngOnInit, value) and
minified files (definitions mostly single letters).
"""

import math
import os
import posixpath
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from repomapper.areas import Areas, annotation
from repomapper.config import OutlineConfig
from repomapper.utils import mostly_single_letters

IMPORTANCE_WEIGHTS = {"core": 4.0, "normal": 1.0, "low": 0.25}   # areas.toml importance -> share multiplier
DEP_LINE_TOKENS = 20   # budget reserved per dependency line while choosing which directories to open

HEADER = (
    "Outline (no chat_files, mentioned_files or mentioned_idents given): directories folded by size and "
    "importance, with share of PageRank and git activity (commits in the last 90 days, time since the last). "
    "Pass files or identifiers to see signatures."
)


@dataclass
class OutlineFile:
    rank: float                       # File PageRank
    defs: List[Tuple[float, str]]     # (rank, def name), highest first; all of them, samples are picked here


@dataclass
class Dir:
    path: str                                          # "" for the root, else "a/b"
    dirs: Dict[str, "Dir"] = field(default_factory=dict)
    files: List[str] = field(default_factory=list)     # rel paths directly inside, highest rank first
    count: int = 0
    rank: float = 0.0
    langs: Counter = field(default_factory=Counter)
    kinds: Counter = field(default_factory=Counter)    # areas.toml kind -> files ("test": 38)
    defs: List[Tuple[float, str]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return (self.path.rsplit("/", 1)[-1] if self.path else ".") + "/"


def build_tree(files: Dict[str, OutlineFile], kinds: Dict[str, str] = {}) -> Dir:
    """Directory tree with totals; each file's defs should already be just its sample names."""
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
            if f in kinds:
                d.kinds[kinds[f]] += 1
            d.defs.extend(files[f].defs)
        for sub in d.dirs.values():
            total(sub)
            d.count += sub.count
            d.rank += sub.rank
            d.langs.update(sub.langs)
            d.kinds.update(sub.kinds)
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


def sample_defs(files: Dict[str, OutlineFile], kinds: Dict[str, str], settings: OutlineConfig) -> Dict[str, OutlineFile]:
    """files with defs cut to their sample names: none for test/config kinds and minified files, and no
    names defined in more than common_name_dirs directories."""
    dirs_of: Dict[str, Set[str]] = defaultdict(set)
    for rel, f in files.items():
        if rel not in kinds:
            for _rank, name in f.defs:
                dirs_of[name].add(posixpath.dirname(rel))
    out = {}
    for rel, f in files.items():
        names = [name for _rank, name in f.defs]
        keep = [] if rel in kinds or mostly_single_letters(names) else [
            (rank, name) for rank, name in f.defs if len(dirs_of[name]) <= settings.common_name_dirs]
        out[rel] = OutlineFile(f.rank, keep[:settings.defs_per_file])
    return out


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
    activity: Callable[[str], Optional[str]] = lambda path: None,
    importance: Dict[str, float] = IMPORTANCE_WEIGHTS,
) -> Optional[str]:
    """Render the outline, splitting max_tokens top-down among directories by size and importance."""
    if not files:
        return None
    files_per_dir, max_edges = settings.files_per_dir, settings.max_edges
    kinds = {rel: kind for rel in files if (kind := areas.resolve(rel).kind)}
    files = sample_defs(files, kinds, settings)
    root = build_tree(files, kinds)
    doc_cache: Dict[str, List[str]] = {}

    def doc_note(d: Dir) -> str:
        if d.path not in doc_cache:
            doc_cache[d.path] = docs(d.path)
        docs_note = f"  [{', '.join(doc_cache[d.path])}]" if doc_cache[d.path] else ""
        return docs_note + annotation(areas.entries.get(d.path))

    def summary(d: Dir) -> str:
        langs = ", ".join(f"{lang} {n}" for lang, n in d.langs.most_common(settings.max_langs))
        if d.kinds:
            langs += "; " + ", ".join(f"{n} {kind}" for kind, n in sorted(d.kinds.items()))
        line = f"{d.label}  {d.count} file{'' if d.count == 1 else 's'} ({langs}) {pct(d.rank)}"
        note = activity(d.path)
        return f"{line}, {note}" if note else line

    def folded_line(d: Dir, indent: str) -> str:
        names = top_names(d.defs, settings.defs_per_file)
        return (f"{indent}{summary(d)}: {', '.join(names)}{doc_note(d)}" if names
                else f"{indent}{summary(d)}{doc_note(d)}")

    def file_line(f: str, indent: str) -> str:
        names = ", ".join(top_names(files[f].defs, settings.defs_per_file))
        return f"{indent}{f.rsplit('/', 1)[-1]} {pct(files[f].rank)}" + (f": {names}" if names else "") \
            + annotation(areas.entries.get(f))

    shown_cache: Dict[str, List[Dir]] = {}

    def shown(d: Dir) -> List[Dir]:
        """Subdirectories listed when d is open, biggest first; the rest become a '+N more dirs' line."""
        if d.path not in shown_cache:
            subs = sorted(d.dirs.values(), key=lambda s: (s.count, s.rank), reverse=True)
            shown_cache[d.path] = subs[:settings.dirs_per_dir]
        return shown_cache[d.path]

    def open_lines(d: Dir, depth: int) -> List[str]:
        """d's own lines when open, with its subdirectories folded."""
        indent = "  " * depth
        lines = [f"{indent}{summary(d)}{doc_note(d)}"]
        lines += [folded_line(sub, indent + "  ") for sub in shown(d)]
        if len(d.dirs) > len(shown(d)):
            lines.append(f"{indent}  +{len(d.dirs) - len(shown(d))} more dirs")
        lines += [file_line(f, indent + "  ") for f in d.files[:files_per_dir]]
        if len(d.files) > files_per_dir:
            lines.append(f"{indent}  +{len(d.files) - files_per_dir} more files")
        return lines

    def edge_line(a: str, b: str, both: bool) -> str:
        """`a -> b` (or `<->`), with the deepest common directory printed once as a prefix."""
        common = os.path.commonpath([a, b]) if a != "./" and b != "./" else ""
        arrow = "<->" if both else "->"
        if not common:
            return f"  {a} {arrow} {b}"
        n = len(common) + 1
        return f"  {common}/: {a[n:]} {arrow} {b[n:]}"

    def lone_dir(d: Dir) -> bool:
        """The root holding nothing but one directory: its line would repeat that directory's."""
        return not d.path and not d.files and len(d.dirs) == 1

    def render(open_dirs: Set[str], deps: List[Tuple[str, str, bool]] = ()) -> str:
        lines = [HEADER, ""]

        def walk(d: Dir, depth: int):
            indent = "  " * depth
            if d.path not in open_dirs:
                lines.append(folded_line(d, indent))
                return
            if lone_dir(d):
                walk(shown(d)[0], depth)
                return
            lines.append(f"{indent}{summary(d)}{doc_note(d)}")
            for sub in shown(d):
                walk(sub, depth + 1)
            if len(d.dirs) > len(shown(d)):
                lines.append(f"{indent}  +{len(d.dirs) - len(shown(d))} more dirs")
            for f in d.files[:files_per_dir]:
                lines.append(file_line(f, indent + "  "))
            if len(d.files) > files_per_dir:
                lines.append(f"{indent}  +{len(d.files) - files_per_dir} more files")

        walk(root, 0)
        if deps:
            lines += ["", "Dependencies (strongest references first):"] + [edge_line(*dep) for dep in deps]
        return "\n".join(lines)

    def node_of(rel: str, open_dirs: Set[str]) -> str:
        """The displayed node a file belongs to: itself when listed, else its innermost displayed directory."""
        d = root
        parts = rel.split("/")
        for part in parts[:-1]:
            if d.path not in open_dirs or d.dirs[part] not in shown(d):
                return d.path + "/" if d.path else "./"
            d = d.dirs[part]
        if d.path not in open_dirs:
            return d.path + "/"
        if rel in d.files[:files_per_dir]:
            return rel
        return d.path + "/" if d.path else "./"

    def contains(node: str, other: str) -> bool:
        return node == "./" or (node.endswith("/") and other.startswith(node))

    def dependencies(open_dirs: Set[str]) -> List[Tuple[str, str, bool]]:
        """(a, b, both ways) for the strongest edges; a reverse pair is one entry, ordered by its stronger
        direction, and a directory's edges with nodes inside it (files directly in it vs a listed one) are dropped."""
        weights: Dict[Tuple[str, str], float] = defaultdict(float)
        for (src, dst), w in edges.items():
            if src in kinds or dst in kinds:
                continue
            a, b = node_of(src, open_dirs), node_of(dst, open_dirs)
            if a != b and not contains(a, b) and not contains(b, a):
                weights[(a, b)] += w
        merged: Dict[frozenset, Tuple[float, str, str, bool]] = {}
        for (a, b), w in weights.items():
            key = frozenset((a, b))
            if key in merged:
                total, a0, b0, _both = merged[key]
                first = (a0, b0) if weights[(a0, b0)] >= w else (a, b)
                merged[key] = (total + w, *first, True)
            else:
                merged[key] = (w, a, b, False)
        ranked = sorted(merged.values(), key=lambda m: m[0], reverse=True)[:max_edges]
        return [(a, b, both) for _w, a, b, both in ranked]

    def with_dependencies(open_dirs: Set[str]) -> str:
        # Aggregating edges is costly on big repos, so it runs once for the chosen layout
        deps = dependencies(open_dirs)
        out = render(open_dirs, deps)
        while deps and token_count(out) > max_tokens:
            deps = deps[:-1]
            out = render(open_dirs, deps)
        return out

    def folds(d: Dir) -> bool:
        """Kept to one line: fold = true in areas.toml, or nothing but tests and configs inside."""
        return bool(d.path) and (areas.resolve(d.path).fold or sum(d.kinds.values()) == d.count)

    def depth_of(d: Dir) -> int:
        return d.path.count("/") + 1 if d.path else 0

    cost_cache: Dict[Tuple[str, str], int] = {}

    def cost(d: Dir, kind: str) -> int:
        """Tokens of d's lines: "folded" (one line), "open" (own lines, subdirectories folded) or "full"
        (everything below it open). Lines are counted separately, +1 each for the newline."""
        key = (d.path, kind)
        if key not in cost_cache:
            if kind == "full" and folds(d):
                cost_cache[key] = cost(d, "folded")
            elif kind == "full":
                cost_cache[key] = cost(d, "open") + sum(cost(sub, "full") - cost(sub, "folded") for sub in shown(d))
            else:
                lines = open_lines(d, depth_of(d)) if kind == "open" else [folded_line(d, "  " * depth_of(d))]
                cost_cache[key] = sum(token_count(line) + 1 for line in lines)
        return cost_cache[key]

    open_dirs: Set[str] = set()

    def allocate(d: Dir, budget: float, top: bool = False) -> int:
        """Open d if its lines fit budget, then split what's left among its subdirectories by
        log(files) x importance, recursively. Returns the tokens used. With top, d's subdirectories (or those
        of a single-subdirectory chain below it) are the top-level areas: each gets enough to open one level
        before the weighted split."""
        if folds(d) or (d.path and cost(d, "open") > budget):
            return cost(d, "folded")
        open_dirs.add(d.path)
        subs = shown(d)
        pool = budget - cost(d, "open")
        if top and len(subs) == 1 and not d.files:
            return cost(d, "open") - cost(subs[0], "folded") + allocate(subs[0], cost(subs[0], "folded") + pool, True)

        mins = {sub.path: cost(sub, "open") - cost(sub, "folded") if top and not folds(sub) else 0
                for sub in subs}
        reserved = sum(mins.values())
        if reserved > pool:   # Not enough to open every top-level area; split by weight alone
            mins, reserved = {path: 0 for path in mins}, 0
        weights = {sub.path: math.log1p(sub.count) * importance[areas.resolve(sub.path).importance] for sub in subs}
        # Weighted split, capped at what each subdirectory can use fully open; the excess goes to the others
        shares = dict(mins)
        uncapped = [sub for sub in subs if cost(sub, "full") - cost(sub, "folded") > mins[sub.path]]
        rest = pool - reserved
        while uncapped and rest > 0:
            total_weight = sum(weights[sub.path] for sub in uncapped) or 1
            capped = [sub for sub in uncapped if shares[sub.path] + rest * weights[sub.path] / total_weight
                      >= cost(sub, "full") - cost(sub, "folded")]
            if not capped:
                for sub in uncapped:
                    shares[sub.path] += rest * weights[sub.path] / total_weight
                break
            for sub in capped:
                rest -= cost(sub, "full") - cost(sub, "folded") - shares[sub.path]
                shares[sub.path] = cost(sub, "full") - cost(sub, "folded")
                uncapped.remove(sub)
        for sub in subs:
            pool -= allocate(sub, cost(sub, "folded") + shares[sub.path]) - cost(sub, "folded")
        # A share too small to open its directory is left over: offer it to those still folded, heaviest first
        for sub in sorted(subs, key=lambda s: weights[s.path], reverse=True):
            if sub.path not in open_dirs and pool >= 1:
                pool -= allocate(sub, cost(sub, "folded") + pool) - cost(sub, "folded")
        return round(budget - pool)

    tree_budget = max_tokens - min(max_edges * DEP_LINE_TOKENS, max_tokens // 5) - token_count(HEADER) - 2
    allocate(root, tree_budget, top=True)
    return with_dependencies(open_dirs)
