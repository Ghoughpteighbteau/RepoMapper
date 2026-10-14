"""
Directory outline: the repo map shown when there's no context to zoom in on. With context (render_outline(focus=...))
it is the trunk of a zoomed map: the focus directories and the ones above them are forced open, lean, and signature
blocks go inside, indented under their directory (see render_outline).

The token budget is spent on a priority queue of expansions. Everything starts folded to one line. A candidate
is opening a folded directory (its own line, its first FIRST_LEVEL_DIRS subdirectories folded, its biggest
files) or listing FIRST_LEVEL_DIRS more subdirectories of an open one (up to dirs_per_dir), scored by areas.toml
importance x sqrt(files) x activity / (tokens it costs x display depth). The best candidate that fits is taken
until nothing does; opening a directory makes its subdirectories candidates, so the outline grows from the top
down, shallow before deep. Core areas come first: a core directory with no open core level above it, and the
directories above a core one, open before anything else (a directory that only passes through to one
subdirectory isn't a level). Activity is 1 for a directory with `active_commits` commits in the last 90 days,
down to `idle_activity` for an idle one, so idle normal and low directories stay folded, and a flat directory
of many files (`lib/repositories/src`) lists its top files. A directory that doesn't fit whole opens lean if it
can (at least MIN_LEAN_DIRS subdirectories folded, no file lines, `+N more dirs`). Subdirectories are ordered
by sqrt(files) x (PageRank relative to the biggest sibling + activity), so shared code everyone references
doesn't outrank where the work happens.
Single-child chains (a directory with no files and one subdirectory) are one line: `java/com/acme/email/`
(its areas.toml status, replacement and note are the deepest directory's that sets each). Doc files (`outline.doc_globs`) are listed in brackets. Share of PageRank is shown on each line. Edges between the
displayed nodes show which areas depend on which: `a <-> b` when both ways, and labels relative to the
deepest common directory (`lib/types/: task.ts -> user.ts`). A directory's edges to its own listed files
are left out.

Edge stopgaps (until import-based edges): a file's top-level area is its top-level directory, then down
through directories without files of their own (`source/`, `source/ui/`) to the first that has some
(`source/ui/src/`). An edge between two parts of one area is internal and dropped, unless no edge crosses
areas (then the internal ones show as before). Crossing edges end at the area instead of a folder inside it
(files directly in the area keep their name), so sibling-folder pairs merge into one line and free slots.
Edges touching files areas.toml marks legacy or deprecated are dropped (both directions, internal or not).

Chains also merge when a directory's biggest subdirectory holds `outline.merge_dir_share` of its files (`ui/src/app/  2110 files
... (+21 files outside)`): the files the merged-away directories hold elsewhere are counted, not listed, and a directory with a
focus directory outside the biggest subdirectory is not merged. Non-code sibling directories are merged away
too (counted in the outside figure) only when they hold at most PLAIN_OUTSIDE_SHARE of the biggest subdirectory's files.

Directory lines give the file count; the language breakdown `(ts 3, js 2)` is only on truncated directories (folded, or
open with `+N more dirs`/files) whose languages differ from their parent's (other set, or top share more than REPEAT_SHARE_GAP off). Test/config counts follow the same rule: only on truncated directories.

A file with no sample names (nothing defined, or only names skipped above: `x.api.ts`, `app-routing.ts`) gets no line: it is
counted in its directory's file count and in `+N more files`, and makes the directory count as truncated. A directory whose
files all lack names lists none (just `+N files` when open); costs are cached per directory as before.

Files areas.toml marks with a kind (tests, configs) count toward their directory, shown as `(py 40; 38 test)`,
but are never listed as lines and give no sample names or edges, and a directory of nothing else stays folded. Sample names skip names defined in many directories (ngOnInit, value) and
minified files (definitions mostly single letters), names defined in more than common_name_files files or by more than
sibling_name_files files of one directory (Change, Options in every file of a package), and generic names (push, set, id,
message; any case) from config. The rest are
ordered by rank times ranking's identifier weight (names defined in many files down), a boost for classes,
interfaces and types, and a penalty for short names.

Non-code directories: a directory with no parseable file anywhere below it (`source/flyway/` migrations, `helm/`, `utils/bruno/`)
is shown as one folded line, `source/flyway/  136 files (sql 136) — Flyway migrations`, with its areas.toml note. They
have no PageRank share, samples, file lines or edges, and are left out of their parents' counts, languages and
shares. Only the topmost such directory is a line (everything below it counts), unparseable files lying in a
directory that also holds code are ignored, as are hidden paths (and, upstream, anything git or `.repomap/ignore`
skips). To stay cheap, directories under PLAIN_DIR_MIN_FILES files are dropped, only the MAX_PLAIN_DIRS biggest
are kept, together they may use PLAIN_BUDGET_SHARE of the token budget (biggest first; the root's own lines can't be
trimmed later), and a parent lists them among its biggest subdirectories or under `+N more dirs` like any folded one.

Lines below MIN_SHOWN_RANK (1%) of the PageRank show no percentage. Git activity shows on directory lines, and
on file lines only for hot files (HOT_FILE_COMMITS in the last 90 days).
"""

import fnmatch
import heapq
import math
import os
import posixpath
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from repomapper.areas import Area, Areas, annotation
from repomapper.config import IdentifierWeightConfig, OutlineConfig, identifier_weight
from repomapper.utils import mostly_single_letters

IMPORTANCE_WEIGHTS = {"core": 4.0, "normal": 1.0, "low": 0.25}   # areas.toml importance -> score multiplier
MIN_SHOWN_RANK = 0.01      # lines with a smaller share of PageRank show no percentage
PLAIN_OUTSIDE_SHARE = 0.1  # a chain merges past non-code sibling directories only when they hold at most this share of its files
REPEAT_SHARE_GAP = 0.1     # a directory's language breakdown is left out when it has its parent's languages, top share within this
HOT_FILE_COMMITS = 5       # file lines show git activity only with this many commits in the last 90 days
MAX_PLAIN_DIRS = 12        # at most this many non-code directory lines in the whole outline, biggest first
PLAIN_DIR_MIN_FILES = 3    # a non-code directory needs at least this many files to be shown
PLAIN_BUDGET_SHARE = 0.15  # share of max_tokens all non-code directory lines may use
FIRST_LEVEL_DIRS = 6       # an opened directory lists this many subdirectories at first; more are separate expansions
MIN_LEAN_DIRS = 3          # a directory that doesn't fit whole opens lean only with at least this many subdirectories listed
DEP_LINE_TOKENS = 20   # budget reserved per dependency line while choosing which directories to open

HEADER = (
    "Outline (no chat_files, mentioned_files or mentioned_idents given): directories folded by size and "
    "importance, with share of PageRank (if >= 1%) and git activity (commits in 90 days, time since the "
    "last; on files only when hot). "
    "Pass files or identifiers to see signatures."
)
HEADER_SEEDED = (
    "Zoomed map: the path from the root to the given files with sibling directories folded (share of PageRank, git "
    "activity), their signatures indented under their directory, Dependencies between areas (the focus area's first), "
    "then other ranked files."
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
    files: List[str] = field(default_factory=list)     # rel paths directly inside with sample names, highest rank first; no kind files
    bare: List[str] = field(default_factory=list)      # files directly inside without sample names: counted, never listed
    hidden: List[str] = field(default_factory=list)    # test/config files directly inside: counted, never listed
    count: int = 0
    rank: float = 0.0
    langs: Counter = field(default_factory=Counter)
    kinds: Counter = field(default_factory=Counter)    # areas.toml kind -> files ("test": 38)
    defs: List[Tuple[float, str]] = field(default_factory=list)
    plain: bool = False                                # nothing parseable below: one folded line, not in its parents' totals
    outside: int = 0                                   # files of the chain's upper dirs that are not under this line's dir

    @property
    def own(self) -> bool:
        """Holds files of its own (listed or not)."""
        return bool(self.files or self.bare or self.hidden)

    @property
    def label(self) -> str:
        return (self.name or self.path.rsplit("/", 1)[-1] if self.path else ".") + "/"


def plain_dirs(files: Dict[str, OutlineFile], plain_files: List[str]) -> Dict[str, Counter]:
    """Topmost directories holding unparseable files and nothing parseable below them -> files per extension.
    Hidden paths, files directly in a directory with code, and directories with fewer than PLAIN_DIR_MIN_FILES
    files are left out; only the MAX_PLAIN_DIRS biggest remain."""
    code_dirs = {""}
    for rel in files:
        parts = rel.split("/")[:-1]
        code_dirs.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    found: Dict[str, Counter] = defaultdict(Counter)
    for rel in plain_files:
        parts = rel.split("/")
        if any(part.startswith(".") for part in parts):
            continue
        top = next(("/".join(parts[:i]) for i in range(1, len(parts)) if "/".join(parts[:i]) not in code_dirs), None)
        if top is not None:
            found[top][os.path.splitext(rel)[1].lstrip(".") or "?"] += 1
    big = [(sum(langs.values()), path) for path, langs in found.items() if sum(langs.values()) >= PLAIN_DIR_MIN_FILES]
    return {path: found[path] for _n, path in sorted(big, key=lambda b: (-b[0], b[1]))[:MAX_PLAIN_DIRS]}


def build_tree(files: Dict[str, OutlineFile], kinds: Dict[str, str] = {}, plain: Dict[str, Counter] = {},
               merge_share: float = 1.1, focus: Set[str] = frozenset()) -> Dir:
    """Directory tree with totals (kind files are counted but not listed); each file's defs should already be just its sample names.
    plain directories are leaves with their own count and languages, kept out of their parents' totals.
    Chains collapse: a directory with no files and one subdirectory merges with it, and so does one whose biggest
    subdirectory holds at least merge_share of its code files (everything else, non-code directories included, is counted
    in `outside`), unless a focus directory lies outside that subdirectory."""
    root = Dir("")
    for rel in files:
        node = root
        parts = rel.split("/")
        for i, part in enumerate(parts[:-1]):
            node = node.dirs.setdefault(part, Dir("/".join(parts[:i + 1])))
        (node.hidden if rel in kinds else node.files if files[rel].defs else node.bare).append(rel)
    for path, langs in plain.items():
        node = root
        parts = path.split("/")
        for i, part in enumerate(parts[:-1]):
            node = node.dirs.setdefault(part, Dir("/".join(parts[:i + 1])))
        node.dirs[parts[-1]] = Dir(path, plain=True, count=sum(langs.values()), langs=Counter(langs))

    def total(d: Dir):
        if d.plain:
            return
        d.files.sort(key=lambda f: files[f].rank, reverse=True)
        for f in d.files + d.bare + d.hidden:
            d.count += 1
            d.rank += files[f].rank
            d.langs[os.path.splitext(f)[1].lstrip(".") or "?"] += 1
            if f in kinds:
                d.kinds[kinds[f]] += 1
            d.defs.extend(files[f].defs)
        for sub in d.dirs.values():
            total(sub)
            if sub.plain:
                continue
            d.count += sub.count
            d.rank += sub.rank
            d.langs.update(sub.langs)
            d.kinds.update(sub.kinds)
            d.defs.extend(sub.defs)
        d.defs.sort(reverse=True)

    def merge_target(sub: Dir) -> Optional[Tuple[str, Dir]]:
        """The (name, subdirectory) sub merges into its line with, if any."""
        if not sub.own and len(sub.dirs) == 1:
            return next(iter(sub.dirs.items()))
        code = {name: s for name, s in sub.dirs.items() if not s.plain}
        if merge_share > 1 or not code:
            return None
        name, big = max(code.items(), key=lambda item: (item[1].count, item[0]))
        plain_outside = sum(s.count for s in sub.dirs.values() if s.plain)
        if big.count < merge_share * sub.count or plain_outside > PLAIN_OUTSIDE_SHARE * big.count or any(
                f.startswith(sub.path + "/") and not (f == big.path or f.startswith(big.path + "/")) for f in focus):
            return None
        return name, big

    def collapse(d: Dir):
        """Merge near-single-child chains below d (see build_tree)."""
        merged = {}
        for name, sub in d.dirs.items():
            sub.name = sub.name or name
            while (target := merge_target(sub)) is not None:
                child_name, child = target
                child.name = f"{sub.name}/{child.name or child_name}"
                child.chain = sub.chain + [sub.path]
                child.outside += (sub.outside + sub.count - child.count
                                  + sum(s.count for s in sub.dirs.values() if s.plain and s is not child))
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
    minified files, and no names defined in more than common_name_dirs directories or common_name_files files, by more
    than sibling_name_files files of one directory, or listed in generic_names (any case)."""
    dirs_of: Dict[str, Set[str]] = defaultdict(set)
    files_of: Counter = Counter()
    siblings: Counter = Counter()   # (directory, name) -> files of that directory defining it
    for rel, f in files.items():
        if rel not in kinds:
            for name in {name for _rank, name in f.defs}:
                dirs_of[name].add(posixpath.dirname(rel))
                files_of[name] += 1
                siblings[posixpath.dirname(rel), name] += 1
    generic = {name.lower() for name in settings.generic_names}
    out = {}
    for rel, f in files.items():
        names = [name for _rank, name in f.defs]
        keep = [] if rel in kinds or mostly_single_letters(names) else sorted(
            ((sample_score(rank, name, name in f.types, files_of[name], settings, weights), name) for rank, name in f.defs
             if len(dirs_of[name]) <= settings.common_name_dirs and files_of[name] <= settings.common_name_files
             and siblings[posixpath.dirname(rel), name] <= settings.sibling_name_files and name.lower() not in generic),
            key=lambda pair: -pair[0])   # stable: ties keep rank order
        out[rel] = OutlineFile(f.rank, keep[:settings.defs_per_file], f.types)
    return out


def find_docs(names: List[str], globs: List[str]) -> List[str]:
    """File names matching any of the doc globs (outline.doc_globs), case-insensitively, sorted."""
    patterns = [g.lower() for g in globs]
    return sorted(n for n in names if any(fnmatch.fnmatchcase(n.lower(), g) for g in patterns))


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
    plain_files: List[str] = (),
    import_edges: Dict[Tuple[str, str], float] = {},
    focus: Optional[Set[str]] = None,
    leaves: Optional[Callable[[int], Dict[str, str]]] = None,
) -> Optional[str]:
    """Render the outline, splitting max_tokens top-down among directories by size and importance.

    Seeded (focus is not None): the outline is the trunk, rendered with settings.seeded_trunk_share of max_tokens, and
    the directories in `focus` (a seeded file's directory, or a directory to zoom in on, root-relative, "" for the
    root) and everything above them are forced open (listing subdirectories folded, no file lines). leaves(budget)
    returns signature blocks per file (rank order, at most budget tokens); blocks of files in a focus directory
    (or below a focused one) go indented under it, the rest follow the trunk. The result never exceeds max_tokens."""
    if not files:
        return None
    seeded = focus is not None
    focus = {a.strip("/") for a in focus or ()}
    total_tokens = max_tokens
    if seeded:
        max_tokens = int(max_tokens * settings.seeded_trunk_share)
    header = HEADER_SEEDED if seeded else HEADER
    files_per_dir, max_edges = settings.files_per_dir, settings.max_edges
    kinds = {rel: kind for rel in files if (kind := areas.resolve(rel).kind)}
    files = sample_defs(files, kinds, settings, weights)
    candidates = plain_dirs(files, plain_files)
    root = build_tree(files, kinds, candidates)   # chains merge below, once the non-code lines are chosen
    doc_cache: Dict[str, List[str]] = {}
    parents: Dict[str, Dir] = {}

    def index_parents(d: Dir):
        for sub in d.dirs.values():
            parents[sub.path] = d
            index_parents(sub)

    index_parents(root)

    def doc_note(d: Dir) -> str:
        """Docs and areas.toml notes of d, and of the directories collapsed into its line (docs with their
        path from the line's start: `svc/README.md` on `svc/src/main/`)."""
        if d.path not in doc_cache:
            start = len(d.path) - len(d.name)
            doc_cache[d.path] = [f"{p[start:]}/{doc}" for p in d.chain for doc in docs(p)] + docs(d.path)
        docs_note = f"  [{', '.join(doc_cache[d.path])}]" if doc_cache[d.path] else ""
        # Nearest entry wins per key, as in areas.py: the deepest directory's status, replacement and note
        merged = Area()
        for p in reversed(d.chain + [d.path]):
            entry = areas.entries.get(p)
            for key in ("status", "replaced_by", "note"):
                if entry and getattr(merged, key) is None:
                    setattr(merged, key, getattr(entry, key))
        return docs_note + annotation(merged)

    def lone_dir(d: Dir) -> bool:
        """The root holding nothing but one directory: its line would repeat that directory's."""
        return not d.path and not d.own and len(d.dirs) == 1

    def repeats_parent(d: Dir) -> bool:
        """d's languages (top max_langs) are its parent's, with the top one's share within REPEAT_SHARE_GAP of it. The root
        is no parent when its line is not printed (it holds nothing but this directory)."""
        parent = parents.get(d.path)
        if parent is None or lone_dir(parent) or not d.count or not parent.count:
            return False
        top, up = d.langs.most_common(settings.max_langs), parent.langs.most_common(settings.max_langs)
        return ({lang for lang, _n in top} == {lang for lang, _n in up}
                and abs(top[0][1] / d.count - up[0][1] / parent.count) <= REPEAT_SHARE_GAP)

    def summary(d: Dir, detail: bool = True) -> str:
        """Dir line; the language breakdown and test/config counts only where the dir is truncated (folded, or listing less than it holds) and
        differs from its parent's."""
        parts = []
        if detail:   # truncated dirs only: languages unless they repeat the parent's, and the unlisted test/config counts
            if not repeats_parent(d):
                parts.append(", ".join(f"{lang} {n}" for lang, n in d.langs.most_common(settings.max_langs)))
            if d.kinds:
                parts.append(", ".join(f"{n} {kind}" for kind, n in sorted(d.kinds.items())))
        counts = f" ({'; '.join(parts)})" if parts else ""
        outside = f" (+{d.outside} file{'' if d.outside == 1 else 's'} outside)" if d.outside else ""
        line = f"{d.label}  {d.count} file{'' if d.count == 1 else 's'}{counts}{outside}{pct(d.rank)}"
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

    def plain_in(d: Dir) -> List[Dir]:
        return [sub for s in d.dirs.values() for sub in ([s] if s.plain else plain_in(s))]

    if candidates:
        # Non-code lines get PLAIN_BUDGET_SHARE of max_tokens between them, biggest first, so they can't crowd out the rest
        left, kept = max_tokens * PLAIN_BUDGET_SHARE, {}
        for d in sorted(plain_in(root), key=lambda d: (-d.count, d.path)):
            left -= token_count(folded_line(d, "  ")) + 1
            if left >= 0:
                kept[d.path] = candidates[d.path]
        candidates = kept
    if candidates or settings.merge_dir_share <= 1:
        root = build_tree(files, kinds, candidates, settings.merge_dir_share, focus)
        doc_cache.clear()
        parents.clear()
        index_parents(root)

    forced: Set[str] = set()          # seeded: directories on the way to a focus directory, and those (their paths)
    hosts: Dict[str, Dir] = {}        # seeded: focus directory -> the displayed directory whose line hosts its blocks

    def find_forced(d: Dir):
        paths = d.chain + [d.path]
        if not d.plain and (not d.path or any(a == p or a.startswith(p + "/") for a in focus for p in paths)):
            forced.add(d.path)
            for a in focus:
                if a in paths:
                    hosts[a] = d
            for sub in d.dirs.values():
                find_forced(sub)

    if seeded:
        find_forced(root)

    ranked_cache: Dict[str, List[Dir]] = {}
    shown_limit: Dict[str, int] = {}   # subdirectories listed per open directory; grows with the budget, up to dirs_per_dir

    def activity_factor(d: Dir) -> float:
        """1 for a directory with settings.active_commits commits in 90 days, settings.idle_activity for an idle one."""
        recent = min((recent_commits(d.path) or 0) / max(settings.active_commits, 1), 1.0)
        return settings.idle_activity + (1 - settings.idle_activity) * recent

    def shown(d: Dir) -> List[Dir]:
        """Subdirectories listed when d is open, best first; the rest become a '+N more dirs' line. Ordered by
        sqrt(files) x (PageRank relative to the biggest sibling + activity): shared code everyone references
        (lib/types) doesn't crowd out where work happens."""
        if d.path not in ranked_cache:
            top_rank = max((s.rank for s in d.dirs.values()), default=0.0) or 1.0
            ranked_cache[d.path] = sorted(d.dirs.values(),
                                          key=lambda s: (s.path not in forced,
                                                         -math.sqrt(s.count) * (s.rank / top_rank + activity_factor(s)), s.path))
        ranked = ranked_cache[d.path]
        # The way to a focus directory is always listed, whatever the limit
        return ranked[:max(shown_limit.get(d.path, settings.dirs_per_dir), sum(s.path in forced for s in ranked))]

    lean: Set[str] = set()   # large directories opened without their file lines

    def file_limit(d: Dir) -> int:
        return 0 if d.path in lean else files_per_dir

    def file_lines(d: Dir, indent: str, limit: int) -> List[str]:
        lines = [file_line(f, indent + "  ") for f in d.files[:limit]]
        more = max(len(d.files) - limit, 0) + len(d.bare)
        if more:
            listed = bool(lines)
            lines.append(f"{indent}  +{more} more files" if listed else f"{indent}  +{more} file{'' if more == 1 else 's'}")
        return lines

    def truncated(d: Dir, limit: int) -> bool:
        """An open dir that hides some of its subdirectories or files."""
        return len(d.dirs) > len(shown(d)) or len(d.files) > limit or bool(d.bare)

    def open_lines(d: Dir, depth: int, limit: int) -> List[str]:
        """d's own lines when open, with its subdirectories folded and up to limit files."""
        indent = "  " * depth
        lines = [f"{indent}{summary(d, truncated(d, limit))}{doc_note(d)}"]
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

    def indented(block: str, indent: str) -> List[str]:
        return [indent + line if line.strip() else "" for line in block.splitlines()]

    def render(open_dirs: Set[str], deps: List[Tuple[str, str, bool]] = (), blocks: Dict[str, str] = {}) -> str:
        lines = [header, ""]
        # Blocks go under the line of the nearest focus directory above their file; the rest follow the trunk
        hosted: Dict[str, List[str]] = defaultdict(list)
        loose: List[str] = []
        for rel, block in blocks.items():
            near = max((a for a in hosts if posixpath.dirname(rel) == a or (a and posixpath.dirname(rel).startswith(a + "/"))),
                       key=len, default=None)
            (hosted[hosts[near].path] if near is not None else loose).append(block)

        def walk(d: Dir, depth: int):
            indent = "  " * depth
            if d.path not in open_dirs:
                lines.append(folded_line(d, indent))
                return
            if lone_dir(d):
                walk(shown(d)[0], depth)
                return
            lines.append(f"{indent}{summary(d, truncated(d, 0 if d.path in hosted else file_limit(d)))}{doc_note(d)}")
            for sub in shown(d):
                walk(sub, depth + 1)
            if len(d.dirs) > len(shown(d)):
                lines.append(f"{indent}  +{len(d.dirs) - len(shown(d))} more dirs")
            if d.path not in hosted:   # a host's files come as signature blocks
                lines.extend(file_lines(d, indent, file_limit(d)))
            for block in hosted.pop(d.path, ()):
                lines.extend([""] + indented(block, indent + "  "))

        walk(root, 0)
        loose += [block for group in hosted.values() for block in group]   # hosts that were not displayed
        if deps:
            lines += ["", "Dependencies (strongest references first):"] + [edge_line(*dep) for dep in deps]
        if loose:
            lines += ["", "Other ranked files:"] + [line for block in loose for line in ("", *block.splitlines())]
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
            while d is not None and not d.own and child(d):
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
        internal: dropped unless nothing crosses areas. Crossing edges end at the area, not at a folder inside it.
        A pair with import edges takes its direction from them alone (`a -> b` when only a imports b); the shared-name
        edges still add to its strength, and decide direction for pairs with no imports."""
        nodes: Dict[str, str] = {}

        def aggregate(source: Dict[Tuple[str, str], float]):
            crossing: Dict[Tuple[str, str], float] = defaultdict(float)
            internal: Dict[Tuple[str, str], float] = defaultdict(float)
            for (src, dst), w in source.items():
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
                    crossing[(a, b)] += w
            return crossing, internal

        named, named_internal = aggregate(edges)
        imported, imported_internal = aggregate(import_edges)
        if named or imported:
            named, imported = dict(named), dict(imported)
        else:
            named, imported = dict(named_internal), dict(imported_internal)
        merged: Dict[frozenset, Tuple[float, str, str, bool]] = {}
        for pair in {*named, *imported}:
            key = frozenset(pair)
            if key in merged:
                continue
            a, b = pair
            both_ways = [(a, b), (b, a)]
            total = sum(named.get(p, 0.0) + imported.get(p, 0.0) for p in both_ways)
            direction = imported if any(p in imported for p in both_ways) else named
            present = [p for p in both_ways if p in direction]
            first = max(present, key=lambda p: (direction[p], p == pair))
            merged[key] = (total, *first, len(present) == 2)
        def near_focus(node: str) -> bool:
            path = node.rstrip("/")
            return node != "./" and any(a and (a == path or a.startswith(path + "/") or path.startswith(a + "/")) for a in focus)

        # Seeded: the focus area's edges first (who uses it, what it uses), then the strongest of the rest
        ranked = sorted(merged.values(), key=lambda m: (near_focus(m[1]) or near_focus(m[2]), m[0]), reverse=True)[:max_edges]
        return [(a, b, both) for _w, a, b, both in ranked]

    def with_dependencies(open_dirs: Set[str]) -> Tuple[str, List[Tuple[str, str, bool]]]:
        # Aggregating edges is costly on big repos, so it runs once for the chosen layout
        deps = dependencies(open_dirs)
        out = render(open_dirs, deps)
        limit = max_tokens
        if seeded:
            # The way to the focus is forced open whatever it costs; the dependencies still get their reserve
            limit = max(limit, token_count(render(open_dirs)) + min(max_edges * DEP_LINE_TOKENS, total_tokens // 5))
        while deps and token_count(out) > limit:
            deps = deps[:-1]
            out = render(open_dirs, deps)
        return out, deps

    def with_leaves(open_dirs: Set[str], trunk: str, deps: List[Tuple[str, str, bool]]) -> str:
        """The trunk with signature blocks added: they get what the trunk left of the whole budget, less what indenting
        and separating them costs (the blocks are asked for again with a smaller budget until the whole fits)."""
        budget = total_tokens - token_count(trunk)
        while budget > 0:
            blocks = leaves(budget)
            if not blocks:
                break
            out = render(open_dirs, deps, blocks)
            over = token_count(out) - total_tokens
            if over <= 0:
                return out
            budget -= over + 4
        while deps and token_count(trunk) > total_tokens:
            deps = deps[:-1]
            trunk = render(open_dirs, deps)
        lines = trunk.splitlines()
        while lines and token_count("\n".join(lines)) > total_tokens:
            lines.pop()
        return "\n".join(lines)

    def folds(d: Dir) -> bool:
        """Kept to one line: a non-code directory, fold = true in areas.toml, or nothing but tests and configs inside."""
        return bool(d.path) and (d.plain or areas.resolve(d.path).fold or sum(d.kinds.values()) == d.count)

    depth_cache: Dict[str, int] = {"": 0}

    def depth_of(d: Dir) -> int:
        """Display depth (collapsed chains are one level), filled in as directories are costed."""
        return depth_cache[d.path]

    cost_cache: Dict[Tuple[str, str], int] = {}

    def cost(d: Dir, kind: str) -> int:
        """Tokens of d's lines: "folded" (one line), "open" (own lines, subdirectories folded) or "lean" (open
        without file lines). Lines are counted separately, +1 each for the newline."""
        key = (d.path, kind)
        if key not in cost_cache:
            for sub in d.dirs.values():
                depth_cache.setdefault(sub.path, depth_of(d) + 1)
            lines = ([folded_line(d, "  " * depth_of(d))] if kind == "folded"
                     else open_lines(d, depth_of(d), files_per_dir if kind == "open" else 0))
            cost_cache[key] = sum(token_count(line) + 1 for line in lines)
        return cost_cache[key]

    def fit_lean(d: Dir, budget: float) -> bool:
        """Show as many of d's best subdirectories (folded, no file lines) as budget holds, at least
        MIN_LEAN_DIRS (or all of them if it has fewer)."""
        for k in range(len(shown(d)), min(MIN_LEAN_DIRS, len(shown(d))) - 1, -1):
            if k < len(shown(d)):
                shown_limit[d.path] = k
                for kind in ("open", "lean"):
                    cost_cache.pop((d.path, kind), None)
            if cost(d, "lean") <= budget:
                return True
        return False

    core_paths = {path for path in areas.entries if path and areas.resolve(path).importance == "core"}
    toward_core = {"/".join(parts[:i]) for path in core_paths for parts in [path.split("/")] for i in range(1, len(parts))}

    def is_core(d: Dir) -> bool:
        return bool(d.path) and areas.resolve(d.path).importance == "core"

    def first_in_line(d: Dir, level: int) -> bool:
        """Tier 0: a core directory with no open core level above it, or one on the way to a core area."""
        return (is_core(d) and level == 0) or d.path in toward_core or any(p in toward_core for p in d.chain)

    open_dirs: Set[str] = set()

    def allocate(budget: float):
        """Open the root, then repeatedly the best-scoring candidate that fits what's left, until nothing does.
        Candidates: opening a folded subdirectory of an open directory (its first FIRST_LEVEL_DIRS subdirectories
        folded, plus its files), and listing more subdirectories of an open one. A candidate's score is importance
        x sqrt(files) x activity per token. Core areas come first: tier 0 is a core directory with no open core
        level above it (pass-throughs to a single subdirectory aren't a level), and the directories above one."""
        open_dirs.add(root.path)
        if seeded:
            lean.add(root.path)
        left = budget - cost(root, "lean" if seeded else "open")
        queue: List[Tuple[int, float, int, str, Dir, int]] = []   # tier, -score, order, "open" or "more", dir, core level
        order = 0

        def push(tier: int, score: float, kind: str, d: Dir, level: int):
            nonlocal order
            order += 1
            heapq.heappush(queue, (tier, -score, order, kind, d, level))

        def offer(d: Dir, level: int):
            if folds(d) or not (d.dirs or d.files):
                return
            shown_limit[d.path] = min(len(d.dirs), FIRST_LEVEL_DIRS, settings.dirs_per_dir)
            extra = max(cost(d, "open") - cost(d, "folded"), 1)
            score = (importance[areas.resolve(d.path).importance] * math.sqrt(d.count) * activity_factor(d)
                     / (extra * depth_of(d)))
            push(0 if first_in_line(d, level) else 1, score, "open", d, level)

        def offer_more(d: Dir, level: int):
            """Another FIRST_LEVEL_DIRS of d's subdirectories, while it has any left under dirs_per_dir."""
            have = shown_limit[d.path]
            ranked = ranked_cache[d.path][have:min(have + FIRST_LEVEL_DIRS, settings.dirs_per_dir)]
            if ranked:
                extra = max(sum(cost(sub, "folded") for sub in ranked), 1)
                score = (importance[areas.resolve(d.path).importance] * math.sqrt(sum(sub.count for sub in ranked))
                         * activity_factor(d) / (extra * max(depth_of(d), 1)))
                push(1, score, "more", d, level)

        def offer_children(d: Dir, subs: List[Dir], level: int):
            if is_core(d) and (d.own or len(d.dirs) != 1):
                level += 1    # a level of core code is open below this directory
            for sub in subs:
                offer(sub, level if is_core(sub) else 0)

        if seeded:
            # The way to the focus directories opens first, lean (the focused files come as signature blocks), whatever it costs
            stack = [root]
            while stack:
                d = stack.pop(0)
                path_subs = [sub for sub in shown(d) if sub.path in forced]
                if d.path != root.path:
                    shown_limit[d.path] = max(min(len(d.dirs), FIRST_LEVEL_DIRS, settings.dirs_per_dir), len(path_subs))
                    lean.add(d.path)
                    open_dirs.add(d.path)
                    left -= cost(d, "lean") - cost(d, "folded")
                    for sub in shown(d):
                        depth_cache.setdefault(sub.path, depth_of(d) + 1)
                if d.path == root.path:
                    shown_limit[d.path] = max(shown_limit.get(d.path, min(len(d.dirs), FIRST_LEVEL_DIRS, settings.dirs_per_dir)), len(path_subs))
                offer_children(d, [sub for sub in shown(d) if sub.path not in forced], 0)
                offer_more(d, 0)
                stack.extend(path_subs)
        else:
            offer_children(root, shown(root), 0)
        while queue:
            _tier, _score, _n, kind, d, level = heapq.heappop(queue)
            if kind == "more":
                have = shown_limit[d.path]
                ranked = ranked_cache[d.path][have:min(have + FIRST_LEVEL_DIRS, settings.dirs_per_dir)]
                while ranked and sum(cost(sub, "folded") for sub in ranked) > left:
                    ranked = ranked[:-1]
                if ranked:
                    left -= sum(cost(sub, "folded") for sub in ranked)
                    shown_limit[d.path] = have + len(ranked)
                    offer_children(d, ranked, level)
                    offer_more(d, level)
                continue
            extra = cost(d, "open") - cost(d, "folded")
            if extra > left:
                if not (d.dirs and fit_lean(d, cost(d, "folded") + left)):
                    continue
                lean.add(d.path)
                extra = cost(d, "lean") - cost(d, "folded")
            open_dirs.add(d.path)
            left -= extra
            offer_children(d, shown(d), level)
            if d.path not in lean:
                offer_more(d, level)

    tree_budget = max_tokens - min(max_edges * DEP_LINE_TOKENS, max_tokens // 5) - token_count(header) - 2
    allocate(tree_budget)
    trunk, deps = with_dependencies(open_dirs)
    return with_leaves(open_dirs, trunk, deps) if seeded and leaves else trunk
