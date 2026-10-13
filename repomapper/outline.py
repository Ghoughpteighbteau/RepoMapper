"""
Directory outline: the repo map shown when there's no context to zoom in on.

The token budget is split top-down: every top-level area gets enough to open one level, the rest goes to
siblings by sqrt(files) times areas.toml importance, recursively; a directory whose share can't hold its
own lines stays folded to one summary line. A large directory (at least LARGE_DIR_MIN_FILES files and
LARGE_DIR_FRACTION of the repo's) always opens a level when its parent does: if its share can't hold its
files too, it lists just its biggest subdirectories folded, `+N more dirs` and `+M files`. Budget a share
couldn't use goes back to still-folded siblings heaviest first, but only to those with at least
LEFTOVER_MIN_WEIGHT of the siblings' weight, so small cheap directories don't open down to files.
Single-child chains (a directory with no files and one subdirectory) are one line: `java/com/acme/email/`. Share of PageRank is shown on each line. Edges between the
displayed nodes show which areas depend on which: `a <-> b` when both ways, and labels relative to the
deepest common directory (`lib/types/: task.ts -> user.ts`). A directory's edges to its own listed files
are left out.

Edge stopgaps (until import-based edges): a file's top-level area is its top-level directory, then down
through directories without files of their own (`source/`, `source/ui/`) to the first that has some
(`source/ui/src/`). An edge between two parts of one area is internal and dropped, unless no edge crosses
areas (then the internal ones show as before). Crossing edges end at the area instead of a folder inside it
(files directly in the area keep their name), so sibling-folder pairs merge into one line and free slots.
Edges touching files areas.toml marks legacy or deprecated are dropped (both directions, internal or not).

Files areas.toml marks with a kind (tests, configs) count toward their directory, shown as `(py 40; 38 test)`,
but are never listed as lines and give no sample names or edges, and a directory of nothing else stays folded. Sample names skip names defined in many directories (ngOnInit, value) and
minified files (definitions mostly single letters), and generic names (push, set, id) from config. The rest are
ordered by rank times ranking's identifier weight (names defined in many files down), a boost for classes,
interfaces and types, and a penalty for short names.

Lines below MIN_SHOWN_RANK of the PageRank show no percentage. Git activity shows on directory lines, and
on file lines only for hot files (HOT_FILE_COMMITS in the last 90 days).
"""

import math
import os
import posixpath
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from repomapper.areas import Areas, annotation
from repomapper.config import IdentifierWeightConfig, OutlineConfig, identifier_weight
from repomapper.utils import mostly_single_letters

IMPORTANCE_WEIGHTS = {"core": 4.0, "normal": 1.0, "low": 0.25}   # areas.toml importance -> share multiplier
LARGE_DIR_MIN_FILES = 100   # a "large" directory has at least this many files...
LARGE_DIR_FRACTION = 0.1    # ...and this share of the repo's files; it always opens a level (see docstring)
LEFTOVER_MIN_WEIGHT = 0.1   # share of the siblings' weight a folded directory needs to get leftover budget
MIN_SHOWN_RANK = 0.001     # lines with a smaller share of PageRank show no percentage
HOT_FILE_COMMITS = 5       # file lines show git activity only with this many commits in the last 90 days
DEP_LINE_TOKENS = 20   # budget reserved per dependency line while choosing which directories to open

HEADER = (
    "Outline (no chat_files, mentioned_files or mentioned_idents given): directories folded by size and "
    "importance, with share of PageRank (if >= 0.1%) and git activity (commits in 90 days, time since the "
    "last; on files only when hot). "
    "Pass files or identifiers to see signatures."
)


@dataclass
class OutlineFile:
    rank: float                       # File PageRank
    defs: List[Tuple[float, str]]     # (rank, def name), highest first; all of them, samples are picked here
    types: Set[str] = field(default_factory=set)   # def names that are classes, interfaces, types or enums


@dataclass
class Dir:
    path: str                                          # "" for the root, else "a/b"
    name: str = ""                                     # label below its parent; "b/c/d" for a collapsed chain
    chain: List[str] = field(default_factory=list)     # paths of the dirs collapsed into this line ("a/b", "a/b/c")
    dirs: Dict[str, "Dir"] = field(default_factory=dict)
    files: List[str] = field(default_factory=list)     # rel paths directly inside, highest rank first; no kind files
    hidden: List[str] = field(default_factory=list)    # test/config files directly inside: counted, never listed
    count: int = 0
    rank: float = 0.0
    langs: Counter = field(default_factory=Counter)
    kinds: Counter = field(default_factory=Counter)    # areas.toml kind -> files ("test": 38)
    defs: List[Tuple[float, str]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return (self.name or self.path.rsplit("/", 1)[-1] if self.path else ".") + "/"


def build_tree(files: Dict[str, OutlineFile], kinds: Dict[str, str] = {}) -> Dir:
    """Directory tree with totals (kind files are counted but not listed); each file's defs should already be just its sample names."""
    root = Dir("")
    for rel in files:
        node = root
        parts = rel.split("/")
        for i, part in enumerate(parts[:-1]):
            node = node.dirs.setdefault(part, Dir("/".join(parts[:i + 1])))
        (node.hidden if rel in kinds else node.files).append(rel)

    def total(d: Dir):
        d.files.sort(key=lambda f: files[f].rank, reverse=True)
        for f in d.files + d.hidden:
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

    def collapse(d: Dir):
        """Merge single-child chains below d: a subdirectory with no files and one subdirectory of its own."""
        merged = {}
        for name, sub in d.dirs.items():
            sub.name = sub.name or name
            while not sub.files and not sub.hidden and len(sub.dirs) == 1:
                (child_name, child), = sub.dirs.items()
                child.name = f"{sub.name}/{child.name or child_name}"
                child.chain = sub.chain + [sub.path]
                sub = child
            collapse(sub)
            merged[sub.name] = sub
        d.dirs = merged

    total(root)
    collapse(root)
    return root


def top_names(defs: List[Tuple[float, str]], n: int) -> List[str]:
    names: List[str] = []
    for _rank, name in defs:
        if name not in names:
            names.append(name)
            if len(names) == n:
                break
    return names


def sample_score(rank: float, name: str, is_type: bool, definers: int, settings: OutlineConfig,
                 weights: IdentifierWeightConfig) -> float:
    """A definition's rank adjusted for sampling: ranking's identifier weight (specific names up, names defined
    in many files down), classes and types up, short names down."""
    score = rank * identifier_weight(name, definers, set(), weights)
    if is_type:
        score *= settings.type_boost
    if len(name) < settings.short_name_length:
        score *= settings.short_name_penalty
    return score


def sample_defs(files: Dict[str, OutlineFile], kinds: Dict[str, str], settings: OutlineConfig,
                weights: IdentifierWeightConfig = IdentifierWeightConfig()) -> Dict[str, OutlineFile]:
    """files with defs cut to their sample names, best first by sample_score: none for test/config kinds and
    minified files, and no names defined in more than common_name_dirs directories or listed in generic_names."""
    dirs_of: Dict[str, Set[str]] = defaultdict(set)
    files_of: Counter = Counter()
    for rel, f in files.items():
        if rel not in kinds:
            for _rank, name in f.defs:
                dirs_of[name].add(posixpath.dirname(rel))
                files_of[name] += 1
    generic = set(settings.generic_names)
    out = {}
    for rel, f in files.items():
        names = [name for _rank, name in f.defs]
        keep = [] if rel in kinds or mostly_single_letters(names) else sorted(
            ((sample_score(rank, name, name in f.types, files_of[name], settings, weights), name) for rank, name in f.defs
             if len(dirs_of[name]) <= settings.common_name_dirs and name not in generic),
            key=lambda pair: -pair[0])   # stable: ties keep rank order
        out[rel] = OutlineFile(f.rank, keep[:settings.defs_per_file], f.types)
    return out


def pct(rank: float) -> str:
    """' 12.3%' for a line's share of PageRank; '' below MIN_SHOWN_RANK."""
    return f" {rank * 100:.1f}%" if rank >= MIN_SHOWN_RANK else ""


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
    recent_commits: Callable[[str], Optional[int]] = lambda path: 0,
    weights: IdentifierWeightConfig = IdentifierWeightConfig(),
) -> Optional[str]:
    """Render the outline, splitting max_tokens top-down among directories by size and importance."""
    if not files:
        return None
    files_per_dir, max_edges = settings.files_per_dir, settings.max_edges
    kinds = {rel: kind for rel in files if (kind := areas.resolve(rel).kind)}
    files = sample_defs(files, kinds, settings, weights)
    root = build_tree(files, kinds)
    doc_cache: Dict[str, List[str]] = {}

    def doc_note(d: Dir) -> str:
        """Docs and areas.toml notes of d, and of the directories collapsed into its line (docs with their
        path from the line's start: `svc/README.md` on `svc/src/main/`)."""
        if d.path not in doc_cache:
            start = len(d.path) - len(d.name)
            doc_cache[d.path] = [f"{p[start:]}/{doc}" for p in d.chain for doc in docs(p)] + docs(d.path)
        docs_note = f"  [{', '.join(doc_cache[d.path])}]" if doc_cache[d.path] else ""
        return docs_note + "".join(annotation(areas.entries.get(p)) for p in d.chain + [d.path])

    def summary(d: Dir) -> str:
        langs = ", ".join(f"{lang} {n}" for lang, n in d.langs.most_common(settings.max_langs))
        if d.kinds:
            langs += "; " + ", ".join(f"{n} {kind}" for kind, n in sorted(d.kinds.items()))
        line = f"{d.label}  {d.count} file{'' if d.count == 1 else 's'} ({langs}){pct(d.rank)}"
        note = activity(d.path)
        return f"{line}, {note}" if note else line

    def folded_line(d: Dir, indent: str) -> str:
        names = top_names(d.defs, settings.defs_per_file)
        return (f"{indent}{summary(d)}: {', '.join(names)}{doc_note(d)}" if names
                else f"{indent}{summary(d)}{doc_note(d)}")

    def file_line(f: str, indent: str) -> str:
        names = ", ".join(top_names(files[f].defs, settings.defs_per_file))
        note = activity(f) if (recent_commits(f) or 0) >= HOT_FILE_COMMITS else None
        return (f"{indent}{f.rsplit('/', 1)[-1]}{pct(files[f].rank)}" + (f", {note}" if note else "")
                + (f": {names}" if names else "") + annotation(areas.entries.get(f)))

    shown_cache: Dict[str, List[Dir]] = {}
    shown_limit: Dict[str, int] = {}   # fewer than dirs_per_dir for a large directory with a small budget

    def shown(d: Dir) -> List[Dir]:
        """Subdirectories listed when d is open, biggest first; the rest become a '+N more dirs' line."""
        if d.path not in shown_cache:
            subs = sorted(d.dirs.values(), key=lambda s: (s.count, s.rank), reverse=True)
            shown_cache[d.path] = subs[:shown_limit.get(d.path, settings.dirs_per_dir)]
        return shown_cache[d.path]

    lean: Set[str] = set()   # large directories opened without their file lines

    def file_limit(d: Dir) -> int:
        return 0 if d.path in lean else files_per_dir

    def file_lines(d: Dir, indent: str, limit: int) -> List[str]:
        lines = [file_line(f, indent + "  ") for f in d.files[:limit]]
        if len(d.files) > limit:
            more = len(d.files) - limit
            lines.append(f"{indent}  +{more} more files" if limit else f"{indent}  +{more} file{'' if more == 1 else 's'}")
        return lines

    def open_lines(d: Dir, depth: int, limit: int) -> List[str]:
        """d's own lines when open, with its subdirectories folded and up to limit files."""
        indent = "  " * depth
        lines = [f"{indent}{summary(d)}{doc_note(d)}"]
        lines += [folded_line(sub, indent + "  ") for sub in shown(d)]
        if len(d.dirs) > len(shown(d)):
            lines.append(f"{indent}  +{len(d.dirs) - len(shown(d))} more dirs")
        return lines + file_lines(d, indent, limit)

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
        return not d.path and not d.files and not d.hidden and len(d.dirs) == 1

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
            lines.extend(file_lines(d, indent, file_limit(d)))

        walk(root, 0)
        if deps:
            lines += ["", "Dependencies (strongest references first):"] + [edge_line(*dep) for dep in deps]
        return "\n".join(lines)

    def node_of(rel: str, open_dirs: Set[str]) -> str:
        """The displayed node a file belongs to: itself when listed, else its innermost displayed directory."""
        d = root
        while d.path in open_dirs:
            sub = next((s for s in shown(d) if rel.startswith(s.path + "/")), None)
            if sub is None:
                return rel if rel in d.files[:file_limit(d)] else (d.path + "/" if d.path else "./")
            d = sub
        return d.path + "/"

    def contains(node: str, other: str) -> bool:
        return node == "./" or (node.endswith("/") and other.startswith(node))

    area_cache: Dict[str, str] = {}

    def area_of(rel: str) -> str:
        """The top-level area of a file: its top-level directory, then down through directories holding no
        files of their own (`source/`, `source/ui/`) to the first that does. "" for a file in the root."""
        if rel not in area_cache:
            def child(d: Dir) -> Optional[Dir]:
                return next((s for s in d.dirs.values() if rel.startswith(s.path + "/")), None)

            d = child(root)
            while d is not None and not d.files and child(d):
                d = child(d)
            area_cache[rel] = d.path if d is not None else ""
        return area_cache[rel]

    def in_area(node: str, area: str) -> str:
        """node, or its area when node is a folder or file below a subfolder of that area."""
        if not area or node == area + "/" or posixpath.dirname(node) == area:
            return node
        return area + "/"

    def dependencies(open_dirs: Set[str]) -> List[Tuple[str, str, bool]]:
        """(a, b, both ways) for the strongest edges; a reverse pair is one entry, ordered by its stronger
        direction, and a directory's edges with nodes inside it (files directly in it vs a listed one) are dropped.
        Edges touching legacy or deprecated files are dropped. An edge between two parts of one top-level area is
        internal: dropped unless nothing crosses areas. Crossing edges end at the area, not at a folder inside it."""
        weights: Dict[Tuple[str, str], float] = defaultdict(float)
        internal: Dict[Tuple[str, str], float] = defaultdict(float)
        nodes: Dict[str, str] = {}
        for (src, dst), w in edges.items():
            if src in kinds or dst in kinds:
                continue
            if any(areas.resolve(rel).status in ("legacy", "deprecated") for rel in (src, dst)):
                continue
            for rel in (src, dst):
                if rel not in nodes:
                    nodes[rel] = node_of(rel, open_dirs)
            a, b = nodes[src], nodes[dst]
            if a == b or contains(a, b) or contains(b, a):
                continue
            area_a, area_b = area_of(src), area_of(dst)
            if area_a and area_a == area_b:
                internal[(a, b)] += w
                continue
            a, b = in_area(a, area_a), in_area(b, area_b)
            if a != b:
                weights[(a, b)] += w
        weights = weights or internal
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

    depth_cache: Dict[str, int] = {"": 0}

    def depth_of(d: Dir) -> int:
        """Display depth (collapsed chains are one level), filled in as allocate walks down."""
        return depth_cache[d.path]

    def large(d: Dir) -> bool:
        return (bool(d.dirs) and not folds(d)
                and d.count >= max(LARGE_DIR_MIN_FILES, LARGE_DIR_FRACTION * root.count))

    cost_cache: Dict[Tuple[str, str], int] = {}

    def cost(d: Dir, kind: str) -> int:
        """Tokens of d's lines: "folded" (one line), "open" (own lines, subdirectories folded), "lean" (open
        without file lines) or "full" (everything below it open). Lines are counted separately, +1 each for the newline."""
        key = (d.path, kind)
        if key not in cost_cache:
            if kind == "full" and folds(d):
                cost_cache[key] = cost(d, "folded")
            elif kind == "full":
                cost_cache[key] = cost(d, "open") + sum(cost(sub, "full") - cost(sub, "folded") for sub in shown(d))
            else:
                for sub in d.dirs.values():
                    depth_cache.setdefault(sub.path, depth_of(d) + 1)
                lines = ([folded_line(d, "  " * depth_of(d))] if kind == "folded"
                         else open_lines(d, depth_of(d), files_per_dir if kind == "open" else 0))
                cost_cache[key] = sum(token_count(line) + 1 for line in lines)
        return cost_cache[key]

    must_cache: Dict[str, int] = {}

    def must(d: Dir) -> int:
        """Tokens over its folded line that a large d needs to open a level, with its large subdirectories."""
        if d.path not in must_cache:
            must_cache[d.path] = (cost(d, "lean") - cost(d, "folded")
                                  + sum(must(sub) for sub in shown(d) if large(sub)))
        return must_cache[d.path]

    def fit_lean(d: Dir, budget: float) -> bool:
        """Show as many of d's biggest subdirectories (folded, no file lines) as budget holds, at least one."""
        for k in range(len(shown(d)), 0, -1):
            if k < len(shown(d)):
                shown_limit[d.path] = k
                shown_cache.pop(d.path)
                for kind in ("open", "lean", "full"):
                    cost_cache.pop((d.path, kind), None)
            if cost(d, "lean") <= budget:
                return True
        return False

    open_dirs: Set[str] = set()

    def allocate(d: Dir, budget: float, top: bool = False) -> int:
        """Open d if its lines fit budget, then split what's left among its subdirectories by
        log(files) x importance, recursively. Returns the tokens used. With top, d's subdirectories (or those
        of a single-subdirectory chain below it) are the top-level areas: each gets enough to open one level
        before the weighted split."""
        if folds(d):
            return cost(d, "folded")
        own = cost(d, "open")
        if d.path and own > budget:
            if not (large(d) and fit_lean(d, budget)):
                return cost(d, "folded")
            lean.add(d.path)
            own = cost(d, "lean")
        open_dirs.add(d.path)
        subs = shown(d)
        pool = budget - own
        if top and len(subs) == 1 and not d.files:
            return own - cost(subs[0], "folded") + allocate(subs[0], cost(subs[0], "folded") + pool, True)

        # Large subdirectories are guaranteed a level; with top, every top-level area too, when they all fit
        needs = {sub.path: must(sub) if large(sub) else 0 for sub in subs}
        mins = {sub.path: max(needs[sub.path], cost(sub, "open") - cost(sub, "folded"))
                if top and not folds(sub) else needs[sub.path] for sub in subs}
        reserved = sum(mins.values())
        if reserved > pool:
            mins, reserved = needs, sum(needs.values())
        if reserved > pool:   # Not even the large ones fit: they share the pool in proportion to their needs
            scale = max(pool, 0) / reserved if reserved else 0
            mins, reserved = {path: need * scale for path, need in needs.items()}, max(pool, 0)
        weights = {sub.path: math.sqrt(sub.count) * importance[areas.resolve(sub.path).importance] for sub in subs}
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
        # A share too small to open its directory is left over: offer it to those still folded, heaviest first,
        # skipping light ones (a cheap small directory would otherwise open fully on a heavy sibling's leftovers)
        total_weight = sum(weights.values()) or 1
        for sub in sorted(subs, key=lambda s: weights[s.path], reverse=True):
            if sub.path not in open_dirs and pool >= 1 and weights[sub.path] >= LEFTOVER_MIN_WEIGHT * total_weight:
                pool -= allocate(sub, cost(sub, "folded") + pool) - cost(sub, "folded")
        return round(budget - pool)

    tree_budget = max_tokens - min(max_edges * DEP_LINE_TOKENS, max_tokens // 5) - token_count(HEADER) - 2
    allocate(root, tree_budget, top=True)
    return with_dependencies(open_dirs)
