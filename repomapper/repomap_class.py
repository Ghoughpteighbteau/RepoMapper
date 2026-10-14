"""
RepoMap class for generating repository maps.
"""

import os
import posixpath
import sys
import time
from pathlib import Path
from collections import Counter, namedtuple, defaultdict
from typing import List, Dict, Set, Optional, Tuple, Callable, Any, Union
import bisect
import concurrent.futures
import dataclasses
import functools
import hashlib
import importlib.metadata
import inspect
import math
import multiprocessing
import shutil
import sqlite3
from repomapper.utils import Tag
from dataclasses import dataclass, field
import diskcache
import numpy as np
from grep_ast import TreeContext
from pygments.lexers import guess_lexer_for_filename
from pygments.token import Token
from pygments.util import ClassNotFound
from repomapper.utils import count_tokens, read_text, is_parseable, Tag
from repomapper.repomap_dir import CACHE_DIR
from repomapper.scm import get_scm_fname
from repomapper.importance import filter_important_files
from repomapper.outline import OutlineFile, find_docs, render_outline
from repomapper.config import Config, IdentifierWeightConfig, identifier_weight, load_config
from repomapper.areas import Area, Areas, annotation, load_areas, test_file_checker
from repomapper.imports import IMPORT_RE, ImportResolver, extract_imports, load_packages
from repomapper.graph import BaseGraph, OutlinePairs, PairSums, RefGraph, ident_ranks as ident_rank_sums, pagerank
from repomapper.history import History, activity_factors, head_commit, read_history
from repomapper import ranking_cache


@dataclass
class FileReport:
    excluded: Dict[str, str]        # File -> exclusion reason with status
    definition_matches: int         # Total definition tags
    reference_matches: int          # Total reference tags
    total_files_considered: int     # Total files provided as input, including unparseable ones (docs, queries)
    parse_errors: Dict[str, str] = field(default_factory=dict)  # File -> error, for files that failed to parse
    git_history: Optional[str] = None  # What history signals used, or why there were none (unseeded maps only)
    parseable_files: int = 0        # Files of those with a tree-sitter language, which can hold definitions


@dataclass
class Ranking:
    graph: Optional[RefGraph]                        # File reference graph; None when nothing was ranked
    file_ranks: Dict[str, float]                     # rel_fname -> PageRank
    ident_ranks: Dict[Tuple[str, str], float]        # (rel_fname, def name) -> rank
    included: List[str]                              # Absolute paths that were parsed
    definitions: Dict[str, Set[str]]                 # rel_fname -> def names
    report: FileReport
    mentioned_fnames: Set[str]
    mentioned_idents: Set[str]
    chat_rel_fnames: Set[str]
    nested: Dict[str, Set[str]] = field(default_factory=dict)  # rel_fname -> names only defined inside functions
    types: Dict[str, Set[str]] = field(default_factory=dict)   # rel_fname -> names of classes, interfaces, types, enums
    # The graph's edges as the outline reads them. A ranking loaded from disk has only these (its graph is a stub
    # holding the node list); a fresh one gets them when it is stored, or from outline_inputs.
    pair_edges: Optional[OutlinePairs] = None
    # The seed independent graph this ranking was made from (shared, not to be changed); None for one loaded from
    # ranking.v1, which has only the numbers.
    base: Optional[BaseGraph] = None



# Constants
CACHE_VERSION = 1

TAGS_CACHE_DIR_DEFAULT = f"{CACHE_DIR}/tags.v{CACHE_VERSION}"
TAGS_CACHE_DIR = TAGS_CACHE_DIR_DEFAULT  # tests point this elsewhere

EXTRACTOR_PACKAGES = ("grep-ast", "tree-sitter", "tree-sitter-language-pack", "pygments")


@functools.lru_cache(maxsize=None)
def extractor_fingerprint() -> str:
    """Hash of everything that determines a file's tags: the query files, the extraction code and parser
    package versions. Cached tags from a different fingerprint are re-parsed, so editing any of these
    invalidates the cache without bumping CACHE_VERSION."""
    h = hashlib.sha256()
    for scm in sorted((Path(__file__).parent / "queries").rglob("*.scm")):
        h.update(str(scm.relative_to(Path(__file__).parent)).encode())
        h.update(scm.read_bytes())
    for fn in (RepoMap.get_tags_raw, RepoMap.get_pygments_refs, get_scm_fname, extract_imports):
        h.update(inspect.getsource(fn).encode())
    h.update(IMPORT_RE.pattern.encode())
    for pkg in EXTRACTOR_PACKAGES:
        try:
            h.update(f"{pkg}={importlib.metadata.version(pkg)}".encode())
        except importlib.metadata.PackageNotFoundError:
            h.update(f"{pkg}=missing".encode())
    return h.hexdigest()


# Personalization weights: chat files seed PageRank strongly, mentioned files less so.
SQLITE_ERRORS = (sqlite3.OperationalError, sqlite3.DatabaseError)

# Tag namedtuple for storing parsed code definitions and references
Tag = namedtuple("Tag", "rel_fname fname line name kind".split())

# Nested definitions also get a tag of kind "nested", so the outline can skip local helpers as sample names
# while ranking still links to them (code wrapped in an IIFE or a factory function is all nested).
# Class-like definitions (class, interface, type, enum...) also get a tag of kind "type", so the outline prefers them
# as sample names over methods and fields.
TYPE_DEFINITION_KINDS = ("class", "type", "interface", "enum", "union", "object", "mixin", "alias")
# JS/TS files also get a tag of kind "import" per module specifier (name = the specifier), for import edges.
IMPORT_LANGUAGES = ("javascript", "typescript", "tsx")
FUNCTION_NODE_WORDS = ("function", "method", "lambda", "closure")


def is_nested(name_node) -> bool:
    """Whether a definition's name sits inside a function body, above the definition's own node."""
    node = name_node.parent.parent if name_node.parent else None
    while node is not None:
        if any(word in node.type for word in FUNCTION_NODE_WORDS):
            return True
        node = node.parent
    return False


def language_families(families: List[List[str]]) -> Callable[[str], str]:
    """fname -> its language family: the first language of its group in families, else its own language."""
    from grep_ast import filename_to_lang
    group = {lang: langs[0] for langs in families for lang in langs}

    def family(fname: str) -> str:
        lang = filename_to_lang(fname) or ""
        return group.get(lang, lang)
    return family


class RepoMap:
    """Main class for generating repository maps."""
    
    def __init__(
        self,
        map_tokens: int = 1024,
        root: str = None,
        token_counter_func: Callable[[str], int] = count_tokens,
        file_reader_func: Callable[[str], Optional[str]] = read_text,
        output_handler_funcs: Dict[str, Callable] = None,
        repo_content_prefix: Optional[str] = None,
        verbose: bool = False,
        max_context_window: Optional[int] = None,
        map_mul_no_files: int = 8,
        refresh: str = "auto",
        exclude_unranked: bool = False,
        config: Optional[Config] = None,
        areas: Optional[Areas] = None,
    ):
        """Initialize RepoMap instance. config and areas default to root/.repomap/config.toml and areas.toml."""
        self.map_tokens = map_tokens
        self.max_map_tokens = map_tokens
        self.root = Path(root or os.getcwd()).resolve()
        self.config = config or load_config(str(self.root))
        self.areas = areas or load_areas(str(self.root))
        self.token_count_func_internal = token_counter_func
        self.read_text_func_internal = file_reader_func
        self.repo_content_prefix = repo_content_prefix
        self.verbose = verbose
        self.max_context_window = max_context_window
        self.map_mul_no_files = map_mul_no_files
        self.refresh = refresh
        self.exclude_unranked = exclude_unranked
        
        # Set up output handlers
        if output_handler_funcs is None:
            output_handler_funcs = {
                'info': print,
                'warning': print,
                'error': print
            }
        self.output_handlers = output_handler_funcs
        
        # Initialize caches
        self.tree_cache = {}
        self.tree_context_cache = {}
        self.tree_render_cache = {}
        self.map_cache = {}
        # Files that failed to parse in this run (fname -> error), surfaced in reports.
        self.parse_errors: Dict[str, str] = {}
        
        # Load persistent tags cache
        self.load_tags_cache()
    
    def history(self) -> Optional[History]:
        """Git history of the project, read once per instance (and cached on disk per HEAD)."""
        if not hasattr(self, "_history"):
            self._history = read_history(str(self.root), self.config.git)
        return self._history

    def tags_cache_dir(self) -> Path:
        """The tags cache lives in the project's .repomap/cache/."""
        return Path(self.root) / TAGS_CACHE_DIR

    def load_tags_cache(self):
        """Load the persistent tags cache."""
        cache_dir = self.tags_cache_dir()
        try:
            self.TAGS_CACHE = diskcache.Cache(str(cache_dir))
        except Exception as e:
            self.output_handlers['warning'](f"Failed to load tags cache: {e}")
            self.TAGS_CACHE = {}
    
    def save_tags_cache(self):
        """Save the tags cache (no-op as diskcache handles persistence)."""
        pass
    
    def tags_cache_error(self):
        """Handle tags cache errors."""
        try:
            cache_dir = self.tags_cache_dir()
            if cache_dir.exists():
                shutil.rmtree(cache_dir)
            self.load_tags_cache()
        except Exception:
            self.output_handlers['warning']("Failed to recreate tags cache, using in-memory cache")
            self.TAGS_CACHE = {}
    
    def token_count(self, text: str) -> int:
        """Count tokens in text with sampling optimization for long texts."""
        if not text:
            return 0
        
        len_text = len(text)
        if len_text < 200:
            return self.token_count_func_internal(text)
        
        # Sample for longer texts
        lines = text.splitlines(keepends=True)
        num_lines = len(lines)
        
        step = max(1, num_lines // 100)
        sampled_lines = lines[::step]
        sample_text = "".join(sampled_lines)
        
        if not sample_text:
            return self.token_count_func_internal(text)
        
        sample_tokens = self.token_count_func_internal(sample_text)
        
        if len(sample_text) == 0:
            return self.token_count_func_internal(text)
        
        est_tokens = (sample_tokens / len(sample_text)) * len_text
        return int(est_tokens)
    
    def get_rel_fname(self, fname: str) -> str:
        """Get relative filename from absolute path."""
        prefix = str(self.root) + os.sep
        if fname.startswith(prefix) and "/./" not in fname and "//" not in fname and ".." not in fname:
            return fname[len(prefix):]   # fast path: pathlib's relative_to dominated large repos
        try:
            return str(Path(fname).relative_to(self.root))
        except ValueError:
            return fname
    
    def read_disk_text(self, rel_fname: str) -> Optional[str]:
        """Text of a file under the root (also ones the map leaves out, like barrels), None if it can't be read."""
        try:
            return (self.root / rel_fname).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def get_file_stamp(self, fname: str) -> Optional[Tuple[int, int]]:
        """(mtime_ns, size) of a file; a change in either means its cached tags are stale."""
        try:
            st = os.stat(fname)
        except FileNotFoundError:
            self.output_handlers['warning'](f"File not found: {fname}")
            return None
        return (st.st_mtime_ns, st.st_size)

    def cached_tags(self, fname: str, stamp: Tuple[int, int]) -> Optional[List[Tag]]:
        """The cached tags of a file, None unless an entry matches both the stamp and the extractor fingerprint."""
        try:
            cached_entry = self.TAGS_CACHE.get(fname)
            if (cached_entry and cached_entry.get("stamp") == stamp
                    and cached_entry.get("extractor") == extractor_fingerprint()):
                return cached_entry["data"]
        except SQLITE_ERRORS:
            self.tags_cache_error()
        return None

    def store_tags(self, fname: str, stamp: Tuple[int, int], tags: List[Tag]):
        try:
            self.TAGS_CACHE[fname] = {"stamp": stamp, "extractor": extractor_fingerprint(), "data": tags}
        except SQLITE_ERRORS:
            self.tags_cache_error()

    def get_tags(self, fname: str, rel_fname: str, force_refresh: bool = False) -> List[Tag]:
        """Get tags for a file, using cache when possible. force_refresh re-parses and overwrites the cache.

        A cached entry is only used if both the file's stamp and the extractor fingerprint still match,
        so edits and changes to tag extraction invalidate it without a manual refresh.
        """
        stamp = self.get_file_stamp(fname)
        if stamp is None:
            return []

        if not force_refresh:
            tags = self.cached_tags(fname, stamp)
            if tags is not None:
                return tags

        # Cache miss, file changed, extractor changed, or forced refresh
        tags = self.get_tags_raw(fname, rel_fname)
        if fname in self.parse_errors:
            # Don't cache failures, so they're retried and reported on every run.
            return tags
        self.store_tags(fname, stamp, tags)
        return tags

    def iter_tags(self, items: List[Tuple[str, str]], force_refresh: bool = False):
        """Yield (fname, rel_fname, tags) for every (fname, rel_fname) item, as get_tags would return them.

        Cache hits come first, in order; the misses follow, in order, parsed in a process pool when there are
        enough of them (see parse_misses). So callers must not depend on the order of the files."""
        misses: List[Tuple[str, str, Tuple[int, int]]] = []
        for fname, rel_fname in items:
            stamp = self.get_file_stamp(fname)
            if stamp is None:
                yield fname, rel_fname, []
                continue
            tags = None if force_refresh else self.cached_tags(fname, stamp)
            if tags is None:
                misses.append((fname, rel_fname, stamp))
            else:
                yield fname, rel_fname, tags
        parsed = self.parse_misses(misses)
        for fname, rel_fname, _stamp in misses:
            yield fname, rel_fname, parsed[fname]

    def parse_worker_count(self, misses: int) -> int:
        """Worker processes for this many cache misses; 0 means parse in this process."""
        setting = self.config.map.parse_workers
        if setting == 0 or misses < PARALLEL_MIN_MISSES or self.read_text_func_internal is not read_text:
            return 0
        if setting < 0:
            setting = min(os.process_cpu_count() or 1, MAX_AUTO_PARSE_WORKERS)
        workers = min(setting, math.ceil(misses / MIN_FILES_PER_WORKER))
        return workers if workers > 1 else 0

    def parse_misses(self, misses: List[Tuple[str, str, Tuple[int, int]]]) -> Dict[str, List[Tag]]:
        """Parse files whose tags are not cached and cache the results (not the failures); fname -> tags.

        Many misses go to worker processes; the cache is only ever written from this process."""
        parsed: Dict[str, List[Tag]] = {}
        stamps = {fname: stamp for fname, _rel, stamp in misses}

        def finish(fname: str, tags: List[Tag], error: Optional[str]):
            if error:
                self.record_parse_error(fname, error)
            else:
                self.store_tags(fname, stamps[fname], tags)
            parsed[fname] = tags

        workers = self.parse_worker_count(len(misses))
        if workers:
            try:
                results = self.parse_in_pool([(fname, rel) for fname, rel, _stamp in misses], workers, finish)
                for fname, (tags, error) in results.items():
                    if fname not in parsed:
                        finish(fname, tags, error)
            except Exception as e:   # e.g. no way to start processes here: parse the rest serially
                self.output_handlers['warning'](f"Parallel parsing failed, parsing serially: {e}")
        for fname, rel_fname, stamp in misses:
            if fname not in parsed:
                tags = self.get_tags_raw(fname, rel_fname)
                if fname not in self.parse_errors:   # the failure was reported by get_tags_raw already
                    self.store_tags(fname, stamp, tags)
                parsed[fname] = tags
        return parsed

    def parse_in_pool(self, items: List[Tuple[str, str]], workers: int,
                      on_result: Optional[Callable[[str, List[Tag], Optional[str]], None]] = None,
                      ) -> Dict[str, Tuple[List[Tag], Optional[str]]]:
        """Parse (fname, rel_fname) items in worker processes; fname -> (tags, error message or None).
        on_result, if given, is called with each file's result as soon as it arrives, so the caller can cache it
        while the workers go on."""
        size = max(1, min(PARSE_CHUNK_FILES, len(items) // (workers * 4)))
        chunks = [items[i:i + size] for i in range(0, len(items), size)]
        results: Dict[str, Tuple[List[Tag], Optional[str]]] = {}
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=workers, mp_context=parse_context(), initializer=init_parse_worker) as pool:
            for chunk_results in pool.map(parse_chunk, chunks):
                for fname, tags, error in chunk_results:
                    results[fname] = (tags, error)
                    if on_result:
                        on_result(fname, tags, error)
        return results

    def get_pygments_refs(self, fname: str, rel_fname: str, code: str) -> List[Tag]:
        """Extract identifier references from a file using pygments tokens."""
        try:
            lexer = guess_lexer_for_filename(fname, code)
        except ClassNotFound:
            return []
        line_starts = [0] + [i + 1 for i, ch in enumerate(code) if ch == "\n"]
        return [
            Tag(rel_fname=rel_fname, fname=fname, line=bisect.bisect_right(line_starts, index),
                name=text, kind="ref")
            for index, token_type, text in lexer.get_tokens_unprocessed(code)
            if token_type in Token.Name
        ]

    def get_tags_raw(self, fname: str, rel_fname: str) -> List[Tag]:
        """Parse file to extract tags using Tree-sitter."""
        try:
            from grep_ast import filename_to_lang
            from grep_ast.tsl import get_language, get_parser
            from tree_sitter import Query, QueryCursor
        except ImportError:
            print("Error: grep-ast is required. Install with: pip install grep-ast")
            sys.exit(1)
            
        lang = filename_to_lang(fname)
        if not lang:
            return []
        
        try:
            language = get_language(lang)
            parser = get_parser(lang)
        except Exception as err:
            self.record_parse_error(fname, f"Skipping file: {err}")
            return []
        
        scm_fname = get_scm_fname(lang)
        if not scm_fname:
            return []
        
        code = self.read_text_func_internal(fname)
        if not code:
            return []
        
        try:
            tree = parser.parse(bytes(code, "utf-8"))
            
            # Load query from SCM file
            query_text = read_text(scm_fname, silent=True)
            if not query_text:
                return []
            
            query = Query(language, query_text)
            cursor = QueryCursor(query)
            captures = cursor.captures(tree.root_node)
            
            tags = []
            # Process captures as a dictionary
            for capture_name, nodes in captures.items():
                for node in nodes:
                    if "name.definition" in capture_name:
                        kind = "def"
                        is_type = capture_name.rsplit(".", 1)[-1] in TYPE_DEFINITION_KINDS
                    elif "name.reference" in capture_name:
                        kind = "ref"
                    else:
                        # Skip other capture types like 'reference.call' if not needed for tagging
                        continue 
                    
                    line_num = node.start_point[0] + 1
                    # Handle potential None value
                    name = node.text.decode('utf-8') if node.text else ""
                    
                    tags.append(Tag(
                        rel_fname=rel_fname,
                        fname=fname,
                        line=line_num,
                        name=name,
                        kind=kind
                    ))
                    if kind == "def" and is_nested(node):
                        tags.append(Tag(rel_fname=rel_fname, fname=fname, line=line_num, name=name, kind="nested"))
                    if kind == "def" and is_type:
                        tags.append(Tag(rel_fname=rel_fname, fname=fname, line=line_num, name=name, kind="type"))

            # Many query files capture few or no references (e.g. TypeScript only
            # captures type annotations, not calls). Merge in pygments name tokens
            # so ranking and search see every use of an identifier.
            seen = {(tag.name, tag.line) for tag in tags if tag.kind == "ref"}
            tags.extend(
                tag for tag in self.get_pygments_refs(fname, rel_fname, code)
                if (tag.name, tag.line) not in seen
            )

            if lang in IMPORT_LANGUAGES:
                tags.extend(Tag(rel_fname, fname, line, spec, "import") for spec, line in extract_imports(code))

            return tags
            
        except Exception as e:
            self.record_parse_error(fname, f"Error parsing: {e}")
            return []

    def record_parse_error(self, fname: str, message: str):
        """Log a parse failure and remember it so reports can show it to the caller."""
        self.parse_errors[fname] = message
        self.output_handlers['error'](f"{fname}: {message}")

    def mentioned_dirs(self, mentioned_fnames: Set[str]) -> Set[str]:
        """Root-relative paths of the directories among the mentioned names (absolute or relative, trailing slash ok)."""
        rels = {self.get_rel_fname(str((self.root / f).resolve())) for f in mentioned_fnames if (self.root / f).is_dir()}
        return {"" if rel == "." else rel for rel in rels}

    def normalize_mentioned_fnames(self, mentioned_fnames: Set[str]) -> Set[str]:
        """Mentioned files may be absolute or relative to the root; return root-relative names."""
        return {self.get_rel_fname(str((self.root / f).resolve())) for f in mentioned_fnames}
    
    def get_ranked_tags(
        self,
        chat_fnames: List[str],
        other_fnames: List[str],
        mentioned_fnames: Optional[Set[str]] = None,
        mentioned_idents: Optional[Set[str]] = None,
        force_refresh: bool = False
    ) -> Tuple[List[Tuple[float, Tag]], FileReport]:
        """Get ranked tags using PageRank algorithm with file report."""
        ranking = self.build_ranking(chat_fnames, other_fnames, mentioned_fnames, mentioned_idents, force_refresh)
        return self.ranked_tags_of(ranking), ranking.report

    def ranked_tags_of(self, ranking: "Ranking") -> List[Tuple[float, Tag]]:
        """Definition tags of a ranking, best first, with the boosts for mentioned and chat files and identifiers."""
        if ranking.graph is None:
            return []
        if ranking.base is None:   # loaded from ranking.v1, which keeps no definition lines: read the files' tags
            return self.ranked_tags_from_files(ranking)
        base = ranking.base
        ranks, ident_ranks = ranking.file_ranks, ranking.ident_ranks
        mentioned_fnames, mentioned_idents = ranking.mentioned_fnames, ranking.mentioned_idents
        chat_rel_fnames = ranking.chat_rel_fnames
        def_boost = self.config.ranking.definition_boost
        bounds = np.searchsorted(base.def_file, np.arange(len(base.included) + 1)).tolist()
        lines, name_ids, def_names = base.def_line.tolist(), base.def_name.tolist(), base.def_names

        ranked_tags = []
        for pos, fname in enumerate(base.included):
            lo, hi = bounds[pos], bounds[pos + 1]
            if lo == hi:
                continue
            rel_fname = self.get_rel_fname(fname)
            file_rank = ranks.get(rel_fname, 0.0)

            # Exclude files with low Page Rank if exclude_unranked is True
            if self.exclude_unranked and file_rank <= 0.0001:  # Use a small threshold to exclude near-zero ranks
                continue

            # Boosts multiply in the same order as in ranked_tags_from_files, so the ranks are the same floats
            plain = ident = 1.0
            ident *= def_boost.mentioned_ident
            if rel_fname in mentioned_fnames:
                plain *= def_boost.mentioned_file
                ident *= def_boost.mentioned_file
            if rel_fname in chat_rel_fnames:
                plain *= def_boost.chat_file
                ident *= def_boost.chat_file
            tie = file_rank * 1e-3   # file rank breaks ties between unreferenced definitions
            for i in range(lo, hi):
                name = def_names[name_ids[i]]
                boost = ident if name in mentioned_idents else plain
                ranked_tags.append(((ident_ranks.get((rel_fname, name), 0.0) + tie) * boost,
                                    Tag(rel_fname, fname, lines[i], name, "def")))

        # Sort by rank (descending)
        ranked_tags.sort(key=lambda x: x[0], reverse=True)
        return ranked_tags

    def ranked_tags_from_files(self, ranking: "Ranking") -> List[Tuple[float, Tag]]:
        """ranked_tags_of for a ranking that has no base graph: reads every file's tags."""
        ranks, ident_ranks = ranking.file_ranks, ranking.ident_ranks
        mentioned_fnames, mentioned_idents = ranking.mentioned_fnames, ranking.mentioned_idents
        chat_rel_fnames = ranking.chat_rel_fnames
        def_boost = self.config.ranking.definition_boost

        # Collect and rank tags
        ranked_tags = []

        for fname in ranking.included:
            rel_fname = self.get_rel_fname(fname)
            file_rank = ranks.get(rel_fname, 0.0)

            # Exclude files with low Page Rank if exclude_unranked is True
            if self.exclude_unranked and file_rank <= 0.0001:  # Use a small threshold to exclude near-zero ranks
                continue

            tags = self.get_tags(fname, rel_fname)
            for tag in tags:
                if tag.kind == "def":
                    # Boost for mentioned identifiers
                    boost = 1.0
                    if tag.name in mentioned_idents:
                        boost *= def_boost.mentioned_ident
                    if rel_fname in mentioned_fnames:
                        boost *= def_boost.mentioned_file
                    if rel_fname in chat_rel_fnames:
                        boost *= def_boost.chat_file

                    # File rank breaks ties between unreferenced definitions
                    final_rank = (ident_ranks.get((rel_fname, tag.name), 0.0) + file_rank * 1e-3) * boost
                    ranked_tags.append((final_rank, tag))

        # Sort by rank (descending)
        ranked_tags.sort(key=lambda x: x[0], reverse=True)

        return ranked_tags

    def build_ranking(
        self,
        chat_fnames: List[str],
        other_fnames: List[str],
        mentioned_fnames: Optional[Set[str]] = None,
        mentioned_idents: Optional[Set[str]] = None,
        force_refresh: bool = False
    ) -> "Ranking":
        """Parse files, build the reference graph and run PageRank over it.

        An unseeded ranking (nothing in the chat or mentioned) is reused from the in-process memo or from
        .repomap/cache/ranking.v1 while nothing it depends on has changed (see ranking_cache)."""
        mentioned_fnames = self.normalize_mentioned_fnames(mentioned_fnames or set())
        if mentioned_idents is None:
            mentioned_idents = set()
        # Return an empty ranking if no files
        if not chat_fnames and not other_fnames:
            return Ranking(None, {}, {}, [], {}, FileReport({}, 0, 0, 0), mentioned_fnames, mentioned_idents, set())

        # Normalize paths to absolute
        real_dirs: Dict[str, str] = {}

        def normalize_path(path):
            # Path.resolve costs a stat per path component; resolve each directory once, and a file only if it is a link
            directory, base = os.path.split(path)
            if not os.path.isabs(path) or not base or base in (".", "..") or os.path.islink(path):
                return str(Path(path).resolve())
            if directory not in real_dirs:
                real_dirs[directory] = os.path.realpath(directory)
            return os.path.join(real_dirs[directory], base)

        chat_fnames = [normalize_path(f) for f in chat_fnames]
        other_fnames = [normalize_path(f) for f in other_fnames]

        unseeded = not (chat_fnames or mentioned_fnames or mentioned_idents)
        if not (unseeded and self.config.map.ranking_cache):
            return self.compute_ranking(chat_fnames, other_fnames, mentioned_fnames, mentioned_idents, force_refresh)

        root = str(self.root)
        stamps = [(fname, ranking_cache.stamp_of(fname)) for fname in set(other_fnames)]
        if any(stamp is None for _fname, stamp in stamps):
            return self.compute_ranking(chat_fnames, other_fnames, mentioned_fnames, mentioned_idents, force_refresh)
        key = self.ranking_key(stamps)
        if not force_refresh:
            hit = ranking_cache.memo_get(root, key)
            from_memo = hit is not None
            if not from_memo:
                hit = ranking_cache.load(ranking_cache.cache_path(root, CACHE_DIR), key)
            if hit is not None and ranking_cache.reads_valid(root, hit[1]):
                ranking, reads = hit
                if not from_memo:
                    ranking_cache.memo_put(root, key, ranking, reads)
                return self.private_copy(ranking)

        reads: Dict[str, ranking_cache.Stamp] = {}
        ranking = self.compute_ranking(chat_fnames, other_fnames, mentioned_fnames, mentioned_idents, force_refresh,
                                       reads)
        report = ranking.report
        # Parse errors and missing files must be reported by every run, so those rankings are not kept
        if ranking.graph is not None and not report.excluded and not report.parse_errors:
            ranking.pair_edges = self.outline_pairs(ranking)
            reads_list = sorted(reads.items())
            ranking_cache.memo_put(root, key, ranking, reads_list)
            ranking_cache.store(ranking_cache.cache_path(root, CACHE_DIR), key, ranking_cache.pack(ranking, reads_list))
            return self.private_copy(ranking)
        return ranking

    @staticmethod
    def private_copy(ranking: "Ranking") -> "Ranking":
        """The memo hands out copies, so a caller changing the report doesn't change what the next call gets."""
        return dataclasses.replace(ranking, report=dataclasses.replace(
            ranking.report, excluded=dict(ranking.report.excluded), parse_errors=dict(ranking.report.parse_errors)))

    def ranking_key(self, stamps: List[Tuple[str, ranking_cache.Stamp]]) -> str:
        """Key of everything an unseeded build_ranking reads besides the listed files' tags: the extractor, the
        settings and areas it uses, git HEAD and the day (history activity), and the files' stamps."""
        head = head_commit(str(self.root))
        day = int(time.time() // ranking_cache.ACTIVITY_BUCKET_SECONDS) if head else None
        return ranking_cache.make_key(
            [extractor_fingerprint(), str(self.root), self.config.ranking, self.config.languages.families,
             self.config.git, self.config.outline.shared_name_edge_weight, self.areas.entries, self.areas.globs,
             head, day, FileReport.__dataclass_fields__.keys()],
            stamps)

    def compute_ranking(
        self,
        chat_fnames: List[str],
        other_fnames: List[str],
        mentioned_fnames: Set[str],
        mentioned_idents: Set[str],
        force_refresh: bool = False,
        reads: Optional[Dict[str, ranking_cache.Stamp]] = None,
    ) -> "Ranking":
        """build_ranking without the unseeded-ranking cache; paths are absolute and normalized. reads, if given,
        collects the (root-relative path -> stamp) of every unlisted file the import resolution looked at.

        The seed independent part (tags, reference graph, imports) comes from the base graph cache when it can."""
        all_fnames = list(set(chat_fnames + other_fnames))
        base = self.base_graph(all_fnames, force_refresh, reads)
        return self.rank_base(base, chat_fnames, all_fnames, mentioned_fnames, mentioned_idents)

    def graph_key(self, stamps: List[Tuple[str, ranking_cache.Stamp]]) -> str:
        """Key of everything the base graph reads besides the listed files' tags: the extractor, the settings and
        areas of the ranking, and the files' stamps. Not HEAD or the day (history only enters the seeded part), nor
        the outline and git settings."""
        return ranking_cache.make_key(
            ["graph", ranking_cache.GRAPH_CACHE_VERSION, extractor_fingerprint(), str(self.root), self.config.ranking,
             self.config.languages.families, self.areas.entries, self.areas.globs,
             tuple(BaseGraph.__dataclass_fields__)],
            stamps)

    def base_graph(self, all_fnames: List[str], force_refresh: bool = False,
                   reads: Optional[Dict[str, ranking_cache.Stamp]] = None) -> BaseGraph:
        """The seed independent graph of the files: from the in-process memo or .repomap/cache/graph.v1 while nothing
        it depends on has changed, else built (and stored)."""
        if not self.config.map.ranking_cache:
            return self.build_base_graph(all_fnames, force_refresh, reads)
        root = str(self.root)
        stamps = [(fname, ranking_cache.stamp_of(fname)) for fname in all_fnames]
        if any(stamp is None for _fname, stamp in stamps):
            return self.build_base_graph(all_fnames, force_refresh, reads)
        key = self.graph_key(stamps)
        if not force_refresh:
            hit = ranking_cache.graph_memo.get(root, key)
            base = hit[0] if hit else None
            from_memo = base is not None
            if base is None:
                base = ranking_cache.load_graph(ranking_cache.graph_cache_path(root, CACHE_DIR), key)
            if base is not None and ranking_cache.reads_valid(root, base.reads):
                if not from_memo:
                    ranking_cache.graph_memo.put(root, key, base, [])
                if reads is not None:
                    reads.update(base.reads)
                return base
        own_reads: Dict[str, ranking_cache.Stamp] = {}
        base = self.build_base_graph(all_fnames, force_refresh, own_reads)
        if reads is not None:
            reads.update(own_reads)
        # Parse errors and missing files must be reported by every run, so those graphs are not kept
        if not base.excluded and not self.parse_errors:
            ranking_cache.graph_memo.put(root, key, base, [])
            ranking_cache.store_graph(ranking_cache.graph_cache_path(root, CACHE_DIR), key, base)
        return base

    def build_base_graph(self, all_fnames: List[str], force_refresh: bool = False,
                         reads: Optional[Dict[str, ranking_cache.Stamp]] = None) -> BaseGraph:
        """Read every file's tags and build the reference and import edges, with nothing seed specific applied."""
        ranking_config = self.config.ranking
        included: List[str] = []
        excluded: Dict[str, str] = {}
        total_definitions = 0
        total_references = 0

        defines = defaultdict(set)
        references = defaultdict(Counter)
        definitions = defaultdict(set)
        nested: Dict[str, Counter] = defaultdict(Counter)
        types: Dict[str, Set[str]] = defaultdict(set)
        def_counts: Counter = Counter()
        imports: Dict[str, List[str]] = defaultdict(list)   # rel_fname -> module specifiers
        def_rows: List[Tuple[str, int, str]] = []            # (rel_fname, line, name) of every definition tag

        parseable_files = sum(1 for fname in all_fnames if is_parseable(fname))

        def absorb(rel_fname: str, tags: List[Tag]):
            nonlocal total_definitions, total_references
            for tag in tags:
                if tag.kind == "def":
                    defines[tag.name].add(rel_fname)
                    definitions[rel_fname].add(tag.name)
                    def_counts[(rel_fname, tag.name)] += 1
                    def_rows.append((rel_fname, tag.line, tag.name))
                    total_definitions += 1
                elif tag.kind == "ref":
                    references[tag.name][rel_fname] += 1
                    total_references += 1
                elif tag.kind == "nested":
                    nested[rel_fname][tag.name] += 1
                elif tag.kind == "type":
                    types[rel_fname].add(tag.name)
                elif tag.kind == "import":
                    imports[rel_fname].append(tag.name)

        included_rel: Set[str] = set()
        to_read: List[Tuple[str, str]] = []
        for fname in all_fnames:
            rel_fname = self.get_rel_fname(fname)
            if not os.path.exists(fname):
                reason = "File not found"
                excluded[fname] = reason
                self.output_handlers['warning'](f"Repo-map can't include {fname}: {reason}")
                continue
            included.append(fname)
            included_rel.add(rel_fname)
            to_read.append((fname, rel_fname))

        # Cache hits are absorbed first, then the files that had to be parsed (possibly in worker processes)
        for _fname, rel_fname, tags in self.iter_tags(to_read, force_refresh):
            absorb(rel_fname, tags)

        # Graph: nodes are files, edges numpy arrays (parallel edges allowed, summed by PageRank)
        nodes = list(dict.fromkeys(self.get_rel_fname(fname) for fname in all_fnames))
        node_index = {rel: i for i, rel in enumerate(nodes)}

        # Edges based on references, only between files of the same language family
        family = language_families(self.config.languages.families)
        family_ids: Dict[str, int] = {}
        file_family = np.zeros(len(nodes), dtype=np.int64)
        for fname in all_fnames:
            file_family[node_index[self.get_rel_fname(fname)]] = family_ids.setdefault(family(fname), len(family_ids))
        edge_src: List[int] = []
        edge_dst: List[int] = []
        edge_count: List[int] = []
        edge_name: List[int] = []
        names: List[str] = []
        name_definers: List[int] = []
        name_mul: List[float] = []
        for name, ref_counts in references.items():
            def_fnames = defines.get(name, set())
            if not def_fnames:
                continue
            name_id = len(names)
            names.append(name)
            name_definers.append(len(def_fnames))
            name_mul.append(identifier_weight(name, len(def_fnames), set(), ranking_config.identifier_weight))
            ref_idx = [node_index[rel] for rel in ref_counts]
            counts = list(ref_counts.values())
            for def_fname in def_fnames:
                edge_src += ref_idx
                edge_dst += [node_index[def_fname]] * len(ref_idx)
                edge_count += counts
                edge_name += [name_id] * len(ref_idx)
        src = np.array(edge_src, dtype=np.int64)
        dst = np.array(edge_dst, dtype=np.int64)
        keep = (src != dst) & (file_family[src] == file_family[dst])

        # Import edges, importer -> imported file(s); a barrel import shares one import's weight among its files
        imp_src: List[int] = []
        imp_dst: List[int] = []
        imp_weight: List[float] = []
        if imports and ranking_config.import_weight > 0:
            rel_files = included_rel
            read = self.read_disk_text
            if reads is not None:
                def read(rel_fname: str) -> Optional[str]:
                    reads[rel_fname] = ranking_cache.stamp_of(str(self.root / rel_fname))
                    return self.read_disk_text(rel_fname)
            resolver = ImportResolver(rel_files, read, load_packages(str(self.root), rel_files))
            for rel_fname, specs in imports.items():
                targets: Dict[str, int] = Counter()
                for spec in specs:
                    found = [t for t in resolver.resolve(rel_fname, spec) if t != rel_fname and t in rel_files]
                    for target in found:
                        targets[target] += 1
                    # a barrel's files split the import's weight; a repeated import counts once
                    if len(found) > 1:
                        for target in found:
                            targets[target] -= 1 - 1 / len(found)
                for target, share in targets.items():
                    imp_src.append(node_index[rel_fname])
                    imp_dst.append(node_index[target])
                    imp_weight.append(ranking_config.import_weight * min(share, 1.0))

        # Definition tags ordered by file position, then source order, as every included file's tags would be read
        position = {self.get_rel_fname(fname): i for i, fname in enumerate(included)}
        def_names: Dict[str, int] = {}
        def_file = np.array([position[rel] for rel, _line, _name in def_rows], dtype=np.int64)
        order = np.argsort(def_file, kind="stable")
        def_line = np.array([line for _rel, line, _name in def_rows], dtype=np.int64)[order]
        def_name = np.array([def_names.setdefault(name, len(def_names)) for _rel, _line, name in def_rows],
                            dtype=np.int64)[order]

        # A name also defined outside functions in the same file is no local helper
        nested = {rel: {name for name, n in counts.items() if n == def_counts[(rel, name)]}
                  for rel, counts in nested.items()}
        i32 = np.int32
        return BaseGraph(
            nodes, included, names, np.array(name_definers, dtype=i32), np.array(name_mul, dtype=float),
            src[keep].astype(i32), dst[keep].astype(i32), np.array(edge_name, dtype=i32)[keep],
            np.array(edge_count, dtype=i32)[keep],
            np.array(imp_src, dtype=i32), np.array(imp_dst, dtype=i32), np.array(imp_weight, dtype=float),
            definitions, nested, dict(types), def_file[order].astype(i32), def_line.astype(i32), def_name.astype(i32),
            list(def_names), excluded, total_definitions, total_references, parseable_files,
            sorted(reads.items()) if reads is not None else [])

    def rank_base(self, base: BaseGraph, chat_fnames: List[str], all_fnames: List[str],
                  mentioned_fnames: Set[str], mentioned_idents: Set[str]) -> "Ranking":
        """The seed specific part of a ranking: weights of mentioned identifiers and chat-file edges, personalization,
        PageRank and ident ranks."""
        ranking_config = self.config.ranking
        nodes, included = base.nodes, base.included
        excluded = dict(base.excluded)
        node_index = {rel: i for i, rel in enumerate(nodes)}
        chat_rel_fnames = set(self.get_rel_fname(f) for f in chat_fnames)

        # Seed PageRank from chat files and, more weakly, mentioned files
        personalization = {}
        chat_set = set(chat_fnames)
        for fname in included:
            rel_fname = self.get_rel_fname(fname)
            if fname in chat_set:
                personalization[rel_fname] = ranking_config.chat_file_personalization
            elif rel_fname in mentioned_fnames:
                personalization[rel_fname] = ranking_config.mentioned_file_personalization

        chat_nodes = np.zeros(len(nodes), dtype=bool)
        for rel in chat_rel_fnames:
            if rel in node_index:
                chat_nodes[node_index[rel]] = True
        src, dst = base.src.astype(np.int64), base.dst.astype(np.int64)
        name_mul = base.name_mul
        if mentioned_idents:
            name_mul = name_mul.copy()
            name_index = {name: i for i, name in enumerate(base.names)}
            for ident in mentioned_idents:
                i = name_index.get(ident)
                if i is not None:
                    name_mul[i] = identifier_weight(ident, int(base.name_definers[i]), mentioned_idents,
                                                    ranking_config.identifier_weight)
        weight = name_mul[base.name_id] * np.sqrt(base.counts.astype(float))
        weight = np.where(chat_nodes[src], weight * ranking_config.chat_file_ref_weight, weight)
        imp_src, imp_dst = base.import_src.astype(np.int64), base.import_dst.astype(np.int64)
        imp_weight = np.where(chat_nodes[imp_src], base.import_weight * ranking_config.chat_file_ref_weight,
                              base.import_weight)
        graph = RefGraph(
            nodes,
            np.concatenate([src, imp_src]),
            np.concatenate([dst, imp_dst]),
            np.concatenate([weight, imp_weight]),
            np.concatenate([base.name_id.astype(np.int64), np.full(len(imp_src), -1, dtype=np.int64)]),
            base.names,
        )

        if not nodes:
            report = FileReport(excluded, base.definition_matches, base.reference_matches, len(all_fnames),
                                dict(self.parse_errors), parseable_files=base.parseable_files)
            return Ranking(None, {}, {}, included, base.definitions, report, mentioned_fnames, mentioned_idents,
                           chat_rel_fnames)

        # Without seeds, recent commits tilt PageRank toward live code (a seed: stable hubs rarely change but
        # PageRank still finds them). areas.toml importance is applied to the result below instead, since
        # link mass swamped it as a seed.
        git_history = None
        importance_mul: Optional[np.ndarray] = None
        if not personalization:
            history = self.history()
            git_history = history.summary() if history else "unavailable (not a git repository, or no commits)"
            factors = activity_factors(history, nodes, self.config.git)
            if factors:
                personalization = {node: factors.get(node, 1.0) for node in nodes}
            if any(e.importance for e in [*self.areas.entries.values(), *self.areas.globs.values()]):
                seed = {"core": ranking_config.core_importance, "normal": 1.0, "low": ranking_config.low_importance}
                importance_mul = np.array([seed[self.areas.resolve(node).importance] for node in nodes])

        # Run PageRank; without seeds, plain PageRank still ranks files by how much the repo references them
        try:
            seeds = None
            if personalization:
                seeds = np.array([personalization.get(node, 0) for node in nodes], dtype=float)
            rank_array = pagerank(len(nodes), graph.src, graph.dst, graph.weight,
                                  ranking_config.pagerank_alpha, seeds)
        except ZeroDivisionError:
            # Fallback to uniform ranking
            rank_array = np.ones(len(nodes))

        # Split each file's rank across its outgoing edges, so a definition ranks by how much it's
        # referenced rather than inheriting its file's rank (which let one popular file fill the map)
        # Unseeded: scale each file's rank and its definitions' by its areas.toml importance, renormalized
        if importance_mul is not None:
            scaled = rank_array * importance_mul
            total = scaled.sum() or 1.0
            target_scale = importance_mul / total
            ident_ranks = ident_rank_sums(graph, rank_array, target_scale)
            rank_array = scaled / total
        else:
            ident_ranks = ident_rank_sums(graph, rank_array)
        ranks = dict(zip(nodes, rank_array.tolist()))

        # Update excluded dictionary with status information
        included_set = set(included)
        for fname in all_fnames:
            if fname in excluded:
                # Add status prefix to existing exclusion reason
                excluded[fname] = f"[EXCLUDED] {excluded[fname]}"
            elif fname not in included_set:
                excluded[fname] = "[NOT PROCESSED] File not included in final processing"

        file_report = FileReport(
            excluded=excluded,
            definition_matches=base.definition_matches,
            reference_matches=base.reference_matches,
            total_files_considered=len(all_fnames),
            parse_errors=dict(self.parse_errors),
            git_history=git_history,
            parseable_files=base.parseable_files,
        )
        return Ranking(graph, ranks, ident_ranks, included, base.definitions, file_report, mentioned_fnames,
                       mentioned_idents, chat_rel_fnames, base.nested, base.types, base=base)

    def get_outline(self, other_fnames: List[str], max_tokens: int,
                    force_refresh: bool = False) -> Tuple[Optional[str], FileReport]:
        """Directory outline folded to fit max_tokens, for maps with no context to zoom in on."""
        ranking = self.build_ranking([], other_fnames, force_refresh=force_refresh)
        if ranking.graph is None:
            return None, ranking.report
        return render_outline(*self.outline_inputs(ranking, other_fnames, max_tokens)), ranking.report

    def get_seeded_outline(self, chat_fnames: List[str], other_fnames: List[str], max_tokens: int,
                           mentioned_fnames: Optional[Set[str]] = None, mentioned_idents: Optional[Set[str]] = None,
                           focus_dirs: Set[str] = frozenset(),
                           force_refresh: bool = False) -> Tuple[Optional[str], FileReport]:
        """The outline as a trunk to the seeded files (chat files, mentioned files, files under focus_dirs, files defining
        mentioned identifiers) with signature blocks of their directories' files inside it. focus_dirs are root-relative."""
        ranking = self.build_ranking(chat_fnames, other_fnames, mentioned_fnames, mentioned_idents, force_refresh)
        if ranking.graph is None:
            return None, ranking.report
        tags = self.ranked_tags_of(ranking)
        focus = {posixpath.dirname(rel) for rel in ranking.chat_rel_fnames | ranking.mentioned_fnames
                 if rel in ranking.file_ranks and os.path.isfile(self.root / rel)} | set(focus_dirs)
        # Identifiers seed the files that define them (a few, best ranked first)
        defining = sorted({rel for rel, names in ranking.definitions.items() if names & ranking.mentioned_idents},
                          key=lambda rel: -ranking.file_ranks.get(rel, 0.0))[:5]
        focus |= {posixpath.dirname(rel) for rel in defining}

        focused: Dict[str, bool] = {}   # per directory: a map has a few hundred directories and ~100k tags

        def in_focus(rel: str) -> bool:
            directory = posixpath.dirname(rel)
            if directory not in focused:
                focused[directory] = any(directory == a or (a and directory.startswith(a + "/")) for a in focus)
            return focused[directory]

        # Outside the focus only definitions something refers to qualify; focused directories show everything
        tags = [(rank, tag) for rank, tag in tags
                if in_focus(tag.rel_fname) or ranking.ident_ranks.get((tag.rel_fname, tag.name), 0.0) > 0]

        # Other ranked files end at the first file ranked below a share of the best one's
        other: Dict[str, float] = {}
        for rank, tag in tags:
            if not in_focus(tag.rel_fname):
                other[tag.rel_fname] = max(other.get(tag.rel_fname, 0.0), rank)
        cutoff = max(other.values(), default=0.0) * self.config.outline.other_files_rank_share
        tags = [(rank, tag) for rank, tag in tags if in_focus(tag.rel_fname) or other[tag.rel_fname] >= cutoff]

        tags, summaries, hidden, overloads = self.limit_definitions(tags)

        def leaves(budget: int) -> Dict[str, str]:
            """Signature blocks per file, as many of the best ranked tags as fit budget (as in the old signature map)."""
            best: Dict[str, str] = {}
            left, right = 0, len(tags)
            while left <= right:
                mid = (left + right) // 2
                parts = self.tree_parts(tags[:mid], ranking.chat_rel_fnames, summaries, hidden, overloads) if mid else []
                if sum(self.token_count(text) + 2 for _rel, text in parts) <= budget:
                    best = dict(parts)
                    left = mid + 1
                else:
                    right = mid - 1
            return best

        return render_outline(*self.outline_inputs(ranking, other_fnames, max_tokens),
                              focus=focus, leaves=leaves), ranking.report

    def limit_definitions(self, tags: List[Tuple[float, Tag]]) -> Tuple[List[Tuple[float, Tag]], Dict[str, str], Dict[str, int], Dict[Tuple[str, int], int]]:
        """Seeded signature blocks without the bulk: each file keeps its best ranked seeded_defs_per_file definitions
        (hidden: file -> how many were dropped) and a test file keeps only its best one, which stands for the file's
        one-line summary (summaries: file -> text after the colon), e.g. `12 tests (test_a, test_b, test_c, +9 more)`, or `9 tests, 3 other defs (...)` when it has helpers."""
        cap, names_shown = self.config.outline.seeded_defs_per_file, self.config.outline.seeded_test_names
        is_test = test_file_checker(self.areas)
        by_file: Dict[str, List[Tuple[float, Tag]]] = {}
        for rank, tag in tags:                       # best ranked first, so each list is too
            by_file.setdefault(tag.rel_fname, []).append((rank, tag))
        kept, summaries, hidden, overloads = [], {}, {}, {}
        for rel, file_tags in by_file.items():
            if is_test(rel):
                names = [tag.name for _rank, tag in file_tags]
                tests = sum(n.lower().startswith("test") for n in names)
                word = (f"{tests} tests" if tests == len(names) else f"{len(names)} defs" if not tests
                        else f"{tests} tests, {len(names) - tests} other defs")
                shown = names[:names_shown]
                more = len(names) - len(shown)
                listed = ", ".join(shown + ([f"+{more} more"] if more and shown else []))
                summaries[rel] = word + (f" ({listed})" if shown else "")
                file_tags = file_tags[:1]
            else:
                file_tags = self.fold_overloads(rel, file_tags, overloads)
                if cap and len(file_tags) > cap:
                    hidden[rel] = len(file_tags) - cap
                    file_tags = file_tags[:cap]
            kept += file_tags
        kept.sort(key=lambda pair: pair[0], reverse=True)
        return kept, summaries, hidden, overloads

    @staticmethod
    def fold_overloads(rel: str, file_tags: List[Tuple[float, Tag]],
                       overloads: Dict[Tuple[str, int], int]) -> List[Tuple[float, Tag]]:
        """file_tags without the second and later of consecutive definitions (by line) with one name; the first one
        takes their best rank and its (file, line) goes into overloads with the count folded."""
        by_line = sorted(file_tags, key=lambda pair: pair[1].line)
        folded: Dict[int, Tuple[float, Tag]] = {}
        first = None
        for rank, tag in by_line:
            if first is not None and tag.name == first.name:
                best, head = folded[first.line]
                folded[first.line] = (max(best, rank), head)
                overloads[(rel, first.line)] = overloads.get((rel, first.line), 0) + 1
            else:
                first = tag
                folded[tag.line] = (rank, tag)
        return sorted(folded.values(), key=lambda pair: pair[0], reverse=True)

    def outline_pairs(self, ranking: "Ranking") -> OutlinePairs:
        """The ranking's edges between files the outline lists, summed per file pair."""
        from grep_ast import filename_to_lang
        # Shared names between JS/TS files are mostly coincidence (separate apps share `Account`, `get`); imports decide
        name_scale = self.config.outline.shared_name_edge_weight
        graph = ranking.graph
        n = len(graph.nodes)
        listed = {self.get_rel_fname(fname) for fname in ranking.included if is_parseable(fname)}
        shown = np.array([node in listed for node in graph.nodes], dtype=bool)
        js = np.array([node in listed and filename_to_lang(node) in IMPORT_LANGUAGES for node in graph.nodes], dtype=bool)
        is_import = graph.name_id < 0
        scale = np.where(js[graph.src] & js[graph.dst], name_scale, 1.0)
        weight = np.where(is_import, graph.weight, graph.weight * scale)
        use = shown[graph.src] & shown[graph.dst] & (is_import | (scale > 0))

        def pair_sums(mask: np.ndarray) -> PairSums:
            return PairSums.of(n, graph.src[mask], graph.dst[mask], weight[mask])

        return OutlinePairs(graph.nodes, pair_sums(use & ~is_import), pair_sums(use & is_import))

    def outline_inputs(self, ranking: "Ranking", other_fnames: List[str], max_tokens: int) -> tuple:
        """Arguments for render_outline: files with shares of PageRank, edges, budget and the callbacks."""
        files = {}
        plain_files = [self.get_rel_fname(fname) for fname in other_fnames
                       if not is_parseable(fname) and os.path.isfile(fname)]
        for fname in ranking.included:
            rel_fname = self.get_rel_fname(fname)
            if not is_parseable(fname):
                continue
            nested = ranking.nested.get(rel_fname, set())
            defs = sorted(
                ((ranking.ident_ranks.get((rel_fname, name), 0.0), name)
                 for name in ranking.definitions.get(rel_fname, ()) if name not in nested),
                reverse=True,
            )
            files[rel_fname] = OutlineFile(ranking.file_ranks.get(rel_fname, 0.0), defs,
                                           ranking.types.get(rel_fname, set()))

        # Shares of the shown files: unparseable nodes (queries, docs) would otherwise hold most of the rank
        total_rank = sum(f.rank for f in files.values()) or 1.0
        for f in files.values():
            f.rank /= total_rank

        pairs = ranking.pair_edges or self.outline_pairs(ranking)
        edges = pairs.named.as_dict(pairs.nodes)
        import_edges = pairs.imports.as_dict(pairs.nodes)

        def docs(rel_dir: str) -> List[str]:
            try:
                names = os.listdir(self.root / rel_dir)
            except OSError:
                return []
            return find_docs(names, self.config.outline.doc_globs)

        history = self.history()
        activity = history.describe if history else (lambda path: None)
        recent_commits = history.recent_commits if history else (lambda path: 0)
        ranking_config = self.config.ranking
        importance = {"core": ranking_config.core_importance, "normal": 1.0, "low": ranking_config.low_importance}
        return (files, edges, max_tokens, self.token_count, docs, self.config.outline, self.areas,
                activity, importance, recent_commits, ranking_config.identifier_weight, plain_files, import_edges)

    def render_tree(self, abs_fname: str, rel_fname: str, lois: List[int]) -> str:
        """Render a code snippet with specific lines of interest."""
        key = (abs_fname, tuple(lois))
        if key not in self.tree_render_cache:
            self.tree_render_cache[key] = self.render_tree_uncached(abs_fname, rel_fname, lois)
        return self.tree_render_cache[key]

    def render_tree_uncached(self, abs_fname: str, rel_fname: str, lois: List[int]) -> str:
        code = self.read_text_func_internal(abs_fname)
        if not code:
            return ""
        
        # Use TreeContext for rendering
        try:
            if rel_fname not in self.tree_context_cache:
                self.tree_context_cache[rel_fname] = TreeContext(
                    rel_fname,
                    code,
                    color=False
                )
            
            tree_context = self.tree_context_cache[rel_fname]
            return tree_context.format(lois)
        except Exception:
            # Fallback to simple line extraction
            lines = code.splitlines()
            result_lines = [f"{rel_fname}:"]
            
            for loi in sorted(set(lois)):
                if 1 <= loi <= len(lines):
                    result_lines.append(f"{loi:4d}: {lines[loi-1]}")
            
            return "\n".join(result_lines)
    
    def shorten_signature(self, rel_fname: str, line: str, overloads: Optional[Dict[Tuple[str, int], int]]) -> str:
        """A rendered signature line cut to seeded_max_line_length, then marked with its folded overloads."""
        limit = self.config.outline.seeded_max_line_length
        if limit and len(line.strip()) > limit:
            line = line.rstrip()[:len(line) - len(line.lstrip()) + limit - 3] + "..."
        number = line.split(":", 1)[0].strip()
        if overloads and number.isdigit() and (rel_fname, int(number)) in overloads:
            line += f" (+{overloads[(rel_fname, int(number))]} overloads)"
        return line

    def to_tree(self, tags: List[Tuple[float, Tag]], chat_rel_fnames: Set[str]) -> str:
        """Convert ranked tags to formatted tree output."""
        return "\n\n".join(text for _rel, text in self.tree_parts(tags, chat_rel_fnames))

    def tree_parts(self, tags: List[Tuple[float, Tag]], chat_rel_fnames: Set[str],
                   summaries: Optional[Dict[str, str]] = None,
                   hidden: Optional[Dict[str, int]] = None,
                   overloads: Optional[Dict[Tuple[str, int], int]] = None) -> List[Tuple[str, str]]:
        """(file, rendered signatures) per file of the ranked tags, most important file first. A file in summaries
        is one line (`file: summary`); a file in hidden ends with `+N more defs`; a line in overloads gets `(+N overloads)`.
        Lines longer than seeded_max_line_length are cut with "..." (in seeded maps, where the limit applies)."""
        if not tags:
            return []
        
        # Group tags by file
        file_tags = defaultdict(list)
        for rank, tag in tags:
            file_tags[tag.rel_fname].append((rank, tag))
        
        # Sort files by importance (max rank of their tags)
        sorted_files = sorted(
            file_tags.items(),
            key=lambda x: max(rank for rank, tag in x[1]),
            reverse=True
        )
        
        parts = []
        
        for rel_fname, file_tag_list in sorted_files:
            # Get lines of interest
            lois = [tag.line for rank, tag in file_tag_list]
            
            # Find absolute filename
            abs_fname = str(self.root / rel_fname)
            
            # Get the max rank for the file
            max_rank = max(rank for rank, tag in file_tag_list)
            
            # Render the tree for this file
            rendered = f"{rel_fname}:" if summaries and rel_fname in summaries else self.render_tree(abs_fname, rel_fname, lois)
            if rendered:
                # Add rank value to the output
                rendered_lines = rendered.splitlines()
                first_line = rendered_lines[0]
                code_lines = rendered_lines[1:]
                
                # Status inherits from folders; notes only from the file's own entry (folder notes stay in the outline)
                resolved, own = self.areas.resolve(rel_fname), self.areas.entries.get(rel_fname)
                note = annotation(Area(status=resolved.status, replaced_by=resolved.replaced_by,
                                       note=own.note if own else None))
                if note.startswith(" — "):
                    note = " " + note[3:]  # "file.py: note", the colon already separates it
                if summaries and rel_fname in summaries:
                    parts.append((rel_fname, f"{first_line} {summaries[rel_fname]}{note}"))
                    continue
                code_lines = [self.shorten_signature(rel_fname, line, overloads) for line in code_lines] if summaries is not None else code_lines
                if hidden and rel_fname in hidden:
                    code_lines.append(f"    +{hidden[rel_fname]} more defs")
                parts.append((rel_fname, f"{first_line}{note}\n" + "\n".join(code_lines)))
        
        return parts
    
    def get_ranked_tags_map(
        self,
        chat_fnames: List[str],
        other_fnames: List[str],
        max_map_tokens: int,
        mentioned_fnames: Optional[Set[str]] = None,
        mentioned_idents: Optional[Set[str]] = None,
        force_refresh: bool = False
    ) -> Optional[str]:
        """Get the ranked tags map with caching."""
        cache_key = (
            tuple(sorted(chat_fnames)),
            tuple(sorted(other_fnames)),
            max_map_tokens,
            tuple(sorted(mentioned_fnames or [])),
            tuple(sorted(mentioned_idents or [])),
        )
        
        if not force_refresh and cache_key in self.map_cache:
            return self.map_cache[cache_key]
        
        result = self.get_ranked_tags_map_uncached(
            chat_fnames, other_fnames, max_map_tokens,
            mentioned_fnames, mentioned_idents, force_refresh
        )
        
        self.map_cache[cache_key] = result
        return result
    
    def get_ranked_tags_map_uncached(
        self,
        chat_fnames: List[str],
        other_fnames: List[str],
        max_map_tokens: int,
        mentioned_fnames: Optional[Set[str]] = None,
        mentioned_idents: Optional[Set[str]] = None,
        force_refresh: bool = False
    ) -> Tuple[Optional[str], FileReport]:
        """Generate the ranked tags map without caching."""
        ranked_tags, file_report = self.get_ranked_tags(
            chat_fnames, other_fnames, mentioned_fnames, mentioned_idents, force_refresh
        )
        
        if not ranked_tags:
            return None, file_report
        
        # Filter important files
        important_files = filter_important_files(
            [self.get_rel_fname(f) for f in other_fnames]
        )
        
        # Binary search to find the right number of tags
        chat_rel_fnames = set(self.get_rel_fname(f) for f in chat_fnames)
        
        def try_tags(num_tags: int) -> Tuple[Optional[str], int]:
            if num_tags <= 0:
                return None, 0
            
            selected_tags = ranked_tags[:num_tags]
            tree_output = self.to_tree(selected_tags, chat_rel_fnames)
            
            if not tree_output:
                return None, 0
            
            tokens = self.token_count(tree_output)
            return tree_output, tokens
        
        # Binary search for optimal number of tags
        left, right = 0, len(ranked_tags)
        best_tree = None
        
        while left <= right:
            mid = (left + right) // 2
            tree_output, tokens = try_tags(mid)
            
            if tree_output and tokens <= max_map_tokens:
                best_tree = tree_output
                left = mid + 1
            else:
                right = mid - 1
        
        return best_tree, file_report
    
    def get_repo_map(
        self,
        chat_files: List[str] = None,
        other_files: List[str] = None,
        mentioned_fnames: Optional[Set[str]] = None,
        mentioned_idents: Optional[Set[str]] = None,
        force_refresh: bool = False
    ) -> Tuple[Optional[str], FileReport]:
        """Generate the repository map with file report."""
        if chat_files is None:
            chat_files = []
        if other_files is None:
            other_files = []
            
        # Create empty report for error cases
        empty_report = FileReport({}, 0, 0, 0)
        
        if self.max_map_tokens <= 0 or not other_files:
            return None, empty_report
        
        # Adjust max_map_tokens if no chat files
        max_map_tokens = self.max_map_tokens
        if not chat_files and self.max_context_window:
            padding = 1024
            available = self.max_context_window - padding
            max_map_tokens = min(
                max_map_tokens * self.map_mul_no_files,
                available
            )
        
        try:
            if not chat_files and not mentioned_fnames and not mentioned_idents:
                # Nothing to zoom in on: show how the repo is organized rather than its most popular utilities
                map_string, file_report = self.get_outline(other_files, max_map_tokens, force_refresh)
            else:
                # Zoom in: a trunk to the seeded files with their signatures inside it. A mentioned directory seeds
                # every file under it and opens to signatures.
                focus_dirs = self.mentioned_dirs(mentioned_fnames or set())
                seeds = {f for f in (mentioned_fnames or set()) if not (self.root / f).is_dir()}
                for rel_dir in focus_dirs:
                    prefix = rel_dir + "/" if rel_dir else ""
                    seeds |= {self.get_rel_fname(f) for f in other_files + chat_files if self.get_rel_fname(f).startswith(prefix)}
                map_string, file_report = self.get_seeded_outline(
                    chat_files, other_files, max_map_tokens, seeds, mentioned_idents, focus_dirs, force_refresh
                )
        except RecursionError:
            self.output_handlers['error']("Disabling repo map, git repo too large?")
            self.max_map_tokens = 0
            return None, FileReport({}, 0, 0, 0)  # Ensure consistent return type
        
        if map_string is None:
            return None, file_report
        
        if self.verbose:
            tokens = self.token_count(map_string)
            self.output_handlers['info'](f"Repo-map: {tokens / 1024:.1f} k-tokens")
        
        # Format final output
        other = "other " if chat_files else ""
        
        if self.repo_content_prefix:
            repo_content = self.repo_content_prefix.format(other=other)
        else:
            repo_content = ""
        
        repo_content += map_string
        
        return repo_content, file_report


# Parsing cache misses in worker processes: not worth it below this many misses or this many files per worker
PARALLEL_MIN_MISSES = 100
MIN_FILES_PER_WORKER = 8
MAX_AUTO_PARSE_WORKERS = 16
PARSE_CHUNK_FILES = 32


def parse_context():
    """Start method for the parse workers: never plain fork (the MCP server has threads)."""
    method = "forkserver" if "forkserver" in multiprocessing.get_all_start_methods() else "spawn"
    context = multiprocessing.get_context(method)
    if method == "forkserver":
        context.set_forkserver_preload(["repomapper.repomap_class"])
    return context


class ParseWorker(RepoMap):
    """Just enough of a RepoMap to run get_tags_raw in a worker process, reporting errors instead of printing."""

    def __init__(self):   # deliberately not RepoMap.__init__: no config, cache or output handlers here
        self.read_text_func_internal = read_text
        self.parse_errors: Dict[str, str] = {}

    def record_parse_error(self, fname: str, message: str):
        self.parse_errors[fname] = message


PARSE_WORKER: Optional[ParseWorker] = None


def init_parse_worker():
    """Per worker process, once: keep stdout clean for protocols that use it, and reuse parsers and compiled queries
    across files (get_tags_raw builds them per file, which is a third of its time) without touching its source."""
    global PARSE_WORKER
    import grep_ast.tsl
    import tree_sitter
    sys.stdout = sys.stderr
    parsers: Dict[str, Any] = {}
    queries: Dict[Tuple[int, str], Tuple[Any, Any]] = {}
    get_parser = grep_ast.tsl.get_parser
    query_class = tree_sitter.Query

    def cached_parser(lang):
        if lang not in parsers:
            parsers[lang] = get_parser(lang)
        return parsers[lang]

    def cached_query(language, text):
        key = (id(language), text)
        if key not in queries:
            queries[key] = (language, query_class(language, text))   # holds the language so its id stays unique
        return queries[key][1]

    grep_ast.tsl.get_parser = cached_parser
    tree_sitter.Query = cached_query
    PARSE_WORKER = ParseWorker()


def parse_chunk(items: List[Tuple[str, str]]) -> List[Tuple[str, List[Tag], Optional[str]]]:
    """Worker side: (fname, tags, error) for each (fname, rel_fname)."""
    if PARSE_WORKER is None:
        init_parse_worker()
    results = []
    for fname, rel_fname in items:
        PARSE_WORKER.parse_errors.clear()
        tags = PARSE_WORKER.get_tags_raw(fname, rel_fname)
        results.append((fname, tags, PARSE_WORKER.parse_errors.get(fname)))
    return results
