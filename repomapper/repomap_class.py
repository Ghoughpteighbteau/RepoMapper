"""
RepoMap class for generating repository maps.
"""

import os
import sys
from pathlib import Path
from collections import Counter, namedtuple, defaultdict
from typing import List, Dict, Set, Optional, Tuple, Callable, Any, Union
import bisect
import math
import shutil
import sqlite3
from repomapper.utils import Tag
from dataclasses import dataclass, field
import diskcache
import networkx as nx
from grep_ast import TreeContext
from pygments.lexers import guess_lexer_for_filename
from pygments.token import Token
from pygments.util import ClassNotFound
from repomapper.utils import count_tokens, read_text, is_parseable, Tag
from repomapper.repomap_dir import CACHE_DIR
from repomapper.scm import get_scm_fname
from repomapper.importance import filter_important_files
from repomapper.outline import OutlineFile, render_outline
from repomapper.config import Config, IdentifierWeightConfig, load_config
from repomapper.areas import Areas, annotation, load_areas


@dataclass
class FileReport:
    excluded: Dict[str, str]        # File -> exclusion reason with status
    definition_matches: int         # Total definition tags
    reference_matches: int          # Total reference tags
    total_files_considered: int     # Total files provided as input
    parse_errors: Dict[str, str] = field(default_factory=dict)  # File -> error, for files that failed to parse


@dataclass
class Ranking:
    graph: Optional[nx.MultiDiGraph]                 # File reference graph; None when nothing was ranked
    file_ranks: Dict[str, float]                     # rel_fname -> PageRank
    ident_ranks: Dict[Tuple[str, str], float]        # (rel_fname, def name) -> rank
    included: List[str]                              # Absolute paths that were parsed
    definitions: Dict[str, Set[str]]                 # rel_fname -> def names
    report: FileReport
    mentioned_fnames: Set[str]
    mentioned_idents: Set[str]
    chat_rel_fnames: Set[str]



# Constants
CACHE_VERSION = 1

TAGS_CACHE_DIR_DEFAULT = f"{CACHE_DIR}/tags.v{CACHE_VERSION}"
TAGS_CACHE_DIR = TAGS_CACHE_DIR_DEFAULT  # tests point this elsewhere

# Personalization weights: chat files seed PageRank strongly, mentioned files less so.
SQLITE_ERRORS = (sqlite3.OperationalError, sqlite3.DatabaseError)

# Tag namedtuple for storing parsed code definitions and references
Tag = namedtuple("Tag", "rel_fname fname line name kind".split())


def identifier_weight(name: str, num_definers: int, mentioned_idents: Set[str],
                      weights: IdentifierWeightConfig = IdentifierWeightConfig()) -> float:
    """Edge weight multiplier for an identifier, following aider's repo-map heuristics."""
    mul = 1.0
    is_snake = "_" in name and any(c.isalpha() for c in name)
    is_camel = any(c.isupper() for c in name) and any(c.islower() for c in name)
    if name in mentioned_idents:
        mul *= weights.mentioned
    if (is_snake or is_camel) and len(name) >= weights.specific_min_length:
        mul *= weights.specific
    if name.startswith("_"):
        mul *= weights.private
    if num_definers > weights.common_definers:
        mul *= weights.common
    return mul


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
        self.map_cache = {}
        # Files that failed to parse in this run (fname -> error), surfaced in reports.
        self.parse_errors: Dict[str, str] = {}
        
        # Load persistent tags cache
        self.load_tags_cache()
    
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
        try:
            return str(Path(fname).relative_to(self.root))
        except ValueError:
            return fname
    
    def get_mtime(self, fname: str) -> Optional[float]:
        """Get file modification time."""
        try:
            return os.path.getmtime(fname)
        except FileNotFoundError:
            self.output_handlers['warning'](f"File not found: {fname}")
            return None
    
    def get_tags(self, fname: str, rel_fname: str, force_refresh: bool = False) -> List[Tag]:
        """Get tags for a file, using cache when possible. force_refresh re-parses and overwrites the cache."""
        file_mtime = self.get_mtime(fname)
        if file_mtime is None:
            return []
        
        if not force_refresh:
            try:
                cached_entry = self.TAGS_CACHE.get(fname)
                if cached_entry and cached_entry.get("mtime") == file_mtime:
                    return cached_entry["data"]
            except SQLITE_ERRORS:
                self.tags_cache_error()

        # Cache miss, file changed, or forced refresh
        tags = self.get_tags_raw(fname, rel_fname)
        if fname in self.parse_errors:
            # Don't cache failures, so they're retried and reported on every run.
            return tags

        try:
            self.TAGS_CACHE[fname] = {"mtime": file_mtime, "data": tags}
        except SQLITE_ERRORS:
            self.tags_cache_error()
        
        return tags
    
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

            # Many query files capture few or no references (e.g. TypeScript only
            # captures type annotations, not calls). Merge in pygments name tokens
            # so ranking and search see every use of an identifier.
            seen = {(tag.name, tag.line) for tag in tags if tag.kind == "ref"}
            tags.extend(
                tag for tag in self.get_pygments_refs(fname, rel_fname, code)
                if (tag.name, tag.line) not in seen
            )

            return tags
            
        except Exception as e:
            self.record_parse_error(fname, f"Error parsing: {e}")
            return []

    def record_parse_error(self, fname: str, message: str):
        """Log a parse failure and remember it so reports can show it to the caller."""
        self.parse_errors[fname] = message
        self.output_handlers['error'](f"{fname}: {message}")

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
        if ranking.graph is None:
            return [], ranking.report
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

        return ranked_tags, ranking.report

    def build_ranking(
        self,
        chat_fnames: List[str],
        other_fnames: List[str],
        mentioned_fnames: Optional[Set[str]] = None,
        mentioned_idents: Optional[Set[str]] = None,
        force_refresh: bool = False
    ) -> "Ranking":
        """Parse files, build the reference graph and run PageRank over it."""
        ranking_config = self.config.ranking
        mentioned_fnames = self.normalize_mentioned_fnames(mentioned_fnames or set())
        if mentioned_idents is None:
            mentioned_idents = set()
        # Return an empty ranking if no files
        if not chat_fnames and not other_fnames:
            return Ranking(None, {}, {}, [], {}, FileReport({}, 0, 0, 0), mentioned_fnames, mentioned_idents, set())

        # Normalize paths to absolute
        def normalize_path(path):
            return str(Path(path).resolve())
        
        chat_fnames = [normalize_path(f) for f in chat_fnames]
        other_fnames = [normalize_path(f) for f in other_fnames]
        
        # Initialize file report
        included: List[str] = []
        excluded: Dict[str, str] = {}
        input_files: Dict[str, Dict] = {}
        total_definitions = 0
        total_references = 0
        
        # Collect all tags
        defines = defaultdict(set)
        references = defaultdict(Counter)
        definitions = defaultdict(set)
        
        personalization = {}
        chat_rel_fnames = set(self.get_rel_fname(f) for f in chat_fnames)
        
        all_fnames = list(set(chat_fnames + other_fnames))
        
        for fname in all_fnames:
            rel_fname = self.get_rel_fname(fname)
            
            if not os.path.exists(fname):
                reason = "File not found"
                excluded[fname] = reason
                self.output_handlers['warning'](f"Repo-map can't include {fname}: {reason}")
                continue
                
            included.append(fname)
            
            tags = self.get_tags(fname, rel_fname, force_refresh)
            
            for tag in tags:
                if tag.kind == "def":
                    defines[tag.name].add(rel_fname)
                    definitions[rel_fname].add(tag.name)
                    total_definitions += 1
                elif tag.kind == "ref":
                    references[tag.name][rel_fname] += 1
                    total_references += 1
            
            # Seed PageRank from chat files and, more weakly, mentioned files
            if fname in chat_fnames:
                personalization[rel_fname] = ranking_config.chat_file_personalization
            elif rel_fname in mentioned_fnames:
                personalization[rel_fname] = ranking_config.mentioned_file_personalization
        
        # Build graph
        G = nx.MultiDiGraph()
        
        # Add nodes
        for fname in all_fnames:
            rel_fname = self.get_rel_fname(fname)
            G.add_node(rel_fname)
        
        # Add edges based on references
        for name, ref_counts in references.items():
            def_fnames = defines.get(name, set())
            if not def_fnames:
                continue
            mul = identifier_weight(name, len(def_fnames), mentioned_idents, ranking_config.identifier_weight)
            for ref_fname, count in ref_counts.items():
                weight = mul * math.sqrt(count)
                if ref_fname in chat_rel_fnames:
                    weight *= ranking_config.chat_file_ref_weight
                for def_fname in def_fnames:
                    if ref_fname != def_fname:
                        G.add_edge(ref_fname, def_fname, weight=weight, name=name)
        
        if not G.nodes():
            report = FileReport(excluded, total_definitions, total_references, len(all_fnames),
                                dict(self.parse_errors))
            return Ranking(None, {}, {}, included, definitions, report, mentioned_fnames, mentioned_idents,
                           chat_rel_fnames)
        
        # Without seeds, areas.toml importance tilts PageRank toward core areas and away from low ones
        if not personalization and any(e.importance for e in self.areas.entries.values()):
            seed = {"core": ranking_config.core_importance, "normal": 1.0, "low": ranking_config.low_importance}
            personalization = {node: seed[self.areas.resolve(node).importance] for node in G.nodes()}

        # Run PageRank
        try:
            # Without seeds, plain PageRank still ranks files by how much the repo references them
            ranks = nx.pagerank(G, personalization=personalization or None,
                                 alpha=ranking_config.pagerank_alpha)
        except ZeroDivisionError:
            # Fallback to uniform ranking
            ranks = {node: 1.0 for node in G.nodes()}
        
        # Update excluded dictionary with status information
        for fname in set(chat_fnames + other_fnames):
            if fname in excluded:
                # Add status prefix to existing exclusion reason
                excluded[fname] = f"[EXCLUDED] {excluded[fname]}"
            elif fname not in included:
                excluded[fname] = "[NOT PROCESSED] File not included in final processing"
        
        # Create file report
        file_report = FileReport(
            excluded=excluded,
            definition_matches=total_definitions,
            reference_matches=total_references,
            total_files_considered=len(all_fnames),
            parse_errors=dict(self.parse_errors),
        )
        
        # Split each file's rank across its outgoing edges, so a definition ranks by how much it's
        # referenced rather than inheriting its file's rank (which let one popular file fill the map)
        ident_ranks: Dict[Tuple[str, str], float] = defaultdict(float)
        for src in G.nodes():
            out_edges = G.out_edges(src, data=True)
            total_weight = sum(data["weight"] for _, _, data in out_edges)
            for _, dst, data in out_edges:
                ident_ranks[(dst, data["name"])] += ranks.get(src, 0.0) * data["weight"] / total_weight

        return Ranking(G, ranks, ident_ranks, included, definitions, file_report, mentioned_fnames,
                       mentioned_idents, chat_rel_fnames)

    def get_outline(self, other_fnames: List[str], max_tokens: int,
                    force_refresh: bool = False) -> Tuple[Optional[str], FileReport]:
        """Directory outline folded to fit max_tokens, for maps with no context to zoom in on."""
        ranking = self.build_ranking([], other_fnames, force_refresh=force_refresh)
        if ranking.graph is None:
            return None, ranking.report

        files = {}
        for fname in ranking.included:
            rel_fname = self.get_rel_fname(fname)
            if not is_parseable(fname):
                continue
            defs = sorted(
                ((ranking.ident_ranks.get((rel_fname, name), 0.0), name)
                 for name in ranking.definitions.get(rel_fname, ())),
                reverse=True,
            )
            files[rel_fname] = OutlineFile(ranking.file_ranks.get(rel_fname, 0.0), defs[:self.config.outline.defs_per_file])

        # Shares of the shown files: unparseable nodes (queries, docs) would otherwise hold most of the rank
        total_rank = sum(f.rank for f in files.values()) or 1.0
        for f in files.values():
            f.rank /= total_rank

        edges: Dict[Tuple[str, str], float] = defaultdict(float)
        for src, dst, data in ranking.graph.edges(data=True):
            if src in files and dst in files:
                edges[(src, dst)] += data["weight"]

        def docs(rel_dir: str) -> List[str]:
            try:
                names = os.listdir(self.root / rel_dir)
            except OSError:
                return []
            return sorted(n for n in names if n.upper().startswith("README") or n == "CLAUDE.md")

        return render_outline(files, edges, max_tokens, self.token_count, docs, self.config.outline, self.areas), ranking.report

    def render_tree(self, abs_fname: str, rel_fname: str, lois: List[int]) -> str:
        """Render a code snippet with specific lines of interest."""
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
    
    def to_tree(self, tags: List[Tuple[float, Tag]], chat_rel_fnames: Set[str]) -> str:
        """Convert ranked tags to formatted tree output."""
        if not tags:
            return ""
        
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
        
        tree_parts = []
        
        for rel_fname, file_tag_list in sorted_files:
            # Get lines of interest
            lois = [tag.line for rank, tag in file_tag_list]
            
            # Find absolute filename
            abs_fname = str(self.root / rel_fname)
            
            # Get the max rank for the file
            max_rank = max(rank for rank, tag in file_tag_list)
            
            # Render the tree for this file
            rendered = self.render_tree(abs_fname, rel_fname, lois)
            if rendered:
                # Add rank value to the output
                rendered_lines = rendered.splitlines()
                first_line = rendered_lines[0]
                code_lines = rendered_lines[1:]
                
                tree_parts.append(
                    f"{first_line}\n"
                    f"(Rank value: {max_rank:.4f})\n\n" # Added an extra newline here
                    + "\n".join(code_lines)
                )
        
        return "\n\n".join(tree_parts)
    
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
            shown = {tag.rel_fname for _rank, tag in selected_tags} | chat_rel_fnames
            tree_output = self.area_notes(shown) + tree_output
            
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
    
    def area_notes(self, rel_fnames: Set[str]) -> str:
        """Header listing areas.toml status and notes for areas the shown files belong to."""
        paths = self.areas.covering(rel_fnames)
        if not paths:
            return ""
        lines = ["Area notes from .repomap/areas.toml:"]
        lines += [f"  {p or '.'}{'' if p in rel_fnames else '/'}{annotation(self.areas.entries[p])}" for p in paths]
        if any(self.areas.entries[p].status == "deprecated" for p in paths):
            lines.append("  Don't copy patterns from deprecated areas; look where they're replaced instead.")
        return "\n".join(lines) + "\n\n"

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
                # get_ranked_tags_map returns (map_string, file_report)
                map_string, file_report = self.get_ranked_tags_map(
                    chat_files, other_files, max_map_tokens,
                    mentioned_fnames, mentioned_idents, force_refresh
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
