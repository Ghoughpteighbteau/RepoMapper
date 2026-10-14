import argparse
import asyncio
import json
import os
import re
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any, Set
import dataclasses

from fastmcp import FastMCP, settings
from repomapper.repomap_class import RepoMap
from repomapper.utils import count_tokens, read_text, find_src_files, find_git_root
from repomapper.repomap_dir import init_repomap_dir
from repomapper.config import load_config
from repomapper.areas import load_areas, moved_tests_hint, stale_entries, test_file_checker
from repomapper.scm import get_scm_fname
from repomapper.importance import filter_important_files

# Configure logging - only show errors
root_logger = logging.getLogger()
root_logger.setLevel(logging.ERROR)

# Create console handler for errors only
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.ERROR)
console_formatter = logging.Formatter('%(levelname)-5s %(asctime)-15s %(name)s:%(funcName)s:%(lineno)d - %(message)s')
console_handler.setFormatter(console_formatter)
root_logger.addHandler(console_handler)

# Suppress FastMCP logs
fastmcp_logger = logging.getLogger('fastmcp')
fastmcp_logger.setLevel(logging.ERROR)
# Suppress server startup message
server_logger = logging.getLogger('fastmcp.server')
server_logger.setLevel(logging.ERROR)

log = logging.getLogger(__name__)

# Set global stateless_http setting
settings.stateless_http = True

# Create MCP server
mcp = FastMCP("RepoMapServer")

IGNORE_HINT = (
    "If results include vendored, minified, bundled or generated files that aren't useful, "
    "suggest the user add gitignore-style patterns for them to .repomap/ignore in the project root."
)

# Files init_project created at startup, reported in the first tool response and then cleared.
CREATED_FILES: List[str] = []


def init_project(root: str) -> None:
    global CREATED_FILES
    CREATED_FILES = init_repomap_dir(root)


# Whether a response already carried the generic IGNORE_HINT in this server process
HINT_SENT = False

# Paths that suggest vendored, minified, bundled or generated code
GENERATED_PATH = re.compile(
    r"\.min\.(?:js|css)\b|[.-]bundle\.|[.-]chunk\.|\.generated\.|_pb2\b|\.pb\.(?:go|ts|js)\b"
    r"|(?:^|[/\s])(?:dist|vendor|vendors|third_party|__generated__|generated)/"
)


def looks_generated(text: str) -> bool:
    """Whether paths in text (a map or a list of result files) look vendored, minified, bundled or generated."""
    return bool(GENERATED_PATH.search(text))


def ignore_hint(listing: str) -> Optional[str]:
    """The ignore-file hint for a response whose files (or map) are in listing, or None. The generic hint goes out with
    the first response of the server process and again whenever the listing looks generated (looks_generated); files
    created at startup are reported, with the hint, in the first response."""
    global CREATED_FILES, HINT_SENT
    if CREATED_FILES:
        created, CREATED_FILES = CREATED_FILES, []
        HINT_SENT = True
        return (
            f"Created {', '.join(created)} in the project root. .repomap/ignore lists default patterns and files "
            "detected as generated; areas.toml and config.toml are commented templates. Tell the user so they can "
            f"review them, remove false positives and commit .repomap/. {IGNORE_HINT}"
        )
    if HINT_SENT and not looks_generated(listing):
        return None
    HINT_SENT = True
    return IGNORE_HINT


MAX_ERROR_EXAMPLES = 5


def parse_error_summary(parse_errors: Dict[str, str], root: str) -> Optional[Dict[str, Any]]:
    """Count of files that failed to parse plus a few examples, or None if there were none."""
    if not parse_errors:
        return None
    examples = [
        f"{os.path.relpath(fname, root)}: {error}"
        for fname, error in sorted(parse_errors.items())[:MAX_ERROR_EXAMPLES]
    ]
    return {
        "failed_files": len(parse_errors),
        "examples": examples,
        "note": "These files were skipped. Tell the user if the failures look like a RepoMapper bug.",
    }


def resolve_project_root(root: Optional[str] = None) -> str:
    """The explicit --root if given, else the git root of the cwd, else the cwd."""
    if root:
        return str(Path(root).resolve())
    cwd = os.getcwd()
    return str(Path(find_git_root(cwd) or cwd).resolve())


# Resolved once at startup by main(); the project belongs to the session, not to each call.
PROJECT_ROOT = resolve_project_root()


@mcp.tool()
async def repo_map(
    chat_files: Optional[List[str]] = None,
    other_files: Optional[List[str]] = None,
    token_limit: Any = None,  # Accept any type to handle empty strings
    exclude_unranked: bool = False,
    mentioned_files: Optional[List[str]] = None,
    mentioned_idents: Optional[List[str]] = None,
    verbose: bool = False,
    max_context_window: Optional[int] = None,
) -> Dict[str, Any]:
    """Generate a repository map for the specified files, providing a list of function prototypes and variables for files as well as relevant related
    files. Provide filenames relative to the project root. In addition to the files provided, relevant related files will also be included with a
    very small ranking boost.

    With chat_files, mentioned_files or mentioned_idents the map is zoomed: a thin trunk (the path from the root to those files, sibling directories
    folded to one line, and which areas depend on which) with signatures of the seeded files' directories inside it. With none of them it is
    a directory outline of the whole project.

    Files matching .gitignore or .repomap/ignore (gitignore syntax, relative to the project root) are skipped. If the
    map is dominated by vendored, minified, bundled or generated files, suggest adding them to .repomap/ignore.
    The response's 'hint' repeats this advice only on the first call of the server process, when it reports files
    created in .repomap/, or when the map shows paths that look generated (*.min.js, *.bundle.js, dist/, vendor/);
    without a 'hint', the advice above still holds.

    :param chat_files: A list of file paths that are currently in the chat context. These files will receive the highest ranking.
    :param other_files: A list of other relevant file paths in the repository to consider for the map. They receive a lower ranking boost than mentioned_files and chat_files.
    :param token_limit: The maximum number of tokens the generated repository map should occupy. Defaults to map.token_limit in .repomap/config.toml (8192).
    :param exclude_unranked: If True, files with a PageRank of 0.0 will be excluded from the map. Defaults to False.
    :param mentioned_files: Optional list of file paths explicitly mentioned in the conversation and receive a mid-level ranking boost. A directory here is a focus: every file under it is seeded and the directory opens to signatures while the rest of the map stays folded.
    :param mentioned_idents: Optional list of identifiers explicitly mentioned in the conversation, to boost their ranking.
    :param verbose: If True, enables verbose logging for the RepoMap generation process. Defaults to False.
    :param max_context_window: Optional maximum context window size for token calculation, used to adjust map token limit when no chat files are provided.
    :returns: A dictionary containing:
        - 'map': the generated repository map string
        - 'report': a dictionary with file processing details including:
            - 'included': list of processed files
            - 'excluded': dictionary of excluded files with reasons
            - 'definition_matches': count of matched definitions
            - 'reference_matches': count of matched references
            - 'parseable_files': files in a supported language, which the map can show
            - 'total_files_considered': all files given, including docs, configs and lock files
            - 'git_history' (unseeded maps): commits the activity signals used, or why there were none
            - 'parse_errors' (only if any): count of files that failed to parse and a few example errors
        - 'hint' (only when it applies, see above): suggestion to add generated files to .repomap/ignore
        - 'config_warnings' (only if any): problems in .repomap/config.toml or areas.toml; tell the user
        Or an 'error' key if an error occurred.
    """
    project_root = PROJECT_ROOT
    if not os.path.isdir(project_root):
        return {"error": f"Project root directory not found: {project_root}"}
    config, areas = load_config(project_root), load_areas(project_root)
    warnings = (config.warnings + moved_tests_hint(config, areas) + areas.warnings
                + stale_entries(project_root, areas, config.git))

    # 1. Handle and validate parameters
    # Convert token_limit to integer, falling back to the configured default
    try:
        token_limit = int(token_limit) if token_limit else config.map.token_limit
    except (TypeError, ValueError):
        token_limit = config.map.token_limit
    if token_limit <= 0:
        token_limit = config.map.token_limit
    
    chat_files_list = chat_files or []
    mentioned_fnames_set = set(mentioned_files) if mentioned_files else None
    mentioned_idents_set = set(mentioned_idents) if mentioned_idents else None

    # 2. If a specific list of other_files isn't provided, scan the whole root directory.
    # This should happen regardless of whether chat_files are present.
    effective_other_files = []
    if other_files:
        effective_other_files = other_files
    else:
        log.info("No other_files provided, scanning root directory for context...")
        effective_other_files = find_src_files(project_root)

    # Add a print statement for debugging so you can see what the tool is working with.
    log.debug(f"Chat files: {chat_files_list}")
    log.debug(f"Effective other_files count: {len(effective_other_files)}")

    # If after all that we have no files, we can exit early.
    if not chat_files_list and not effective_other_files:
        log.info("No files to process.")
        return {"map": "No files found to generate a map."}

    # 3. Resolve paths relative to project root
    root_path = Path(project_root).resolve()
    abs_chat_files = [str(root_path / f) for f in chat_files_list]
    abs_other_files = [str(root_path / f) for f in effective_other_files]
    
    # Remove any chat files from the other_files list to avoid duplication
    abs_chat_files_set = set(abs_chat_files)
    abs_other_files = [f for f in abs_other_files if f not in abs_chat_files_set]

    # 4. Instantiate and run RepoMap
    try:
        repo_mapper = RepoMap(
            map_tokens=token_limit,
            root=str(root_path),
            token_counter_func=lambda text: count_tokens(text, "gpt-4"),
            file_reader_func=read_text,
            output_handler_funcs={'info': log.info, 'warning': log.warning, 'error': log.error},
            verbose=verbose,
            exclude_unranked=exclude_unranked,
            max_context_window=max_context_window,
            config=config,
            areas=areas,
        )
    except Exception as e:
        log.exception(f"Failed to initialize RepoMap for project '{project_root}': {e}")
        return {"error": f"Failed to initialize RepoMap: {str(e)}"}

    try:
        map_content, file_report = await asyncio.to_thread(
            repo_mapper.get_repo_map,
            chat_files=abs_chat_files,
            other_files=abs_other_files,
            mentioned_fnames=mentioned_fnames_set,
            mentioned_idents=mentioned_idents_set
        )
        
        # Convert FileReport to dictionary for JSON serialization
        report_dict = {
            "excluded": file_report.excluded,
            "definition_matches": file_report.definition_matches,
            "reference_matches": file_report.reference_matches,
            "parseable_files": file_report.parseable_files,
            "total_files_considered": file_report.total_files_considered,
        }
        if file_report.git_history:
            report_dict["git_history"] = file_report.git_history
        errors = parse_error_summary(file_report.parse_errors, project_root)
        if errors:
            report_dict["parse_errors"] = errors
        
        response = {
            "map": map_content or "No repository map could be generated.",
            "report": report_dict,
        }
        hint = ignore_hint(map_content or "")
        if hint:
            response["hint"] = hint
        if warnings:
            response["config_warnings"] = warnings
        return response
    except Exception as e:
        log.exception(f"Error generating repository map for project '{project_root}': {e}")
        return {"error": f"Error generating repository map: {str(e)}"}
    
def truncate_lines(text: str, max_length: int) -> str:
    """Cap each line so long lines (data blobs, generated code) can't bloat results."""
    return "\n".join(
        line if len(line) <= max_length else line[:max_length - 3] + "..."
        for line in text.splitlines()
    )


@mcp.tool()
async def search_identifiers(
    query: str,
    max_results: int = 50,
    context_lines: int = 2,
    include_definitions: bool = True,
    include_references: bool = True
) -> Dict[str, Any]:
    """Search for identifiers in code files. Get back `results`: one entry per file, `{file, hits: [...]}`, each hit
       with its line number, name, `kind` (def/ref) and `context` (source lines around it, without the path).
       Within the ranking, exact matches come first, then definitions, then references by file and line with test
       files last; files are ordered by their best hit and hits keep that ranking inside a file. Hits on nearby
       lines share one hit (`line` is the first, `lines` lists all, `names` lists differing names). `exact`
       (true/false) is only present when some hits are substring matches (e.g. `get_tags_raw` for `get_tags`).
       `more_hits` is present only when `max_results` cut the result: that many further hits were not returned
       (raise `max_results` to see them; without it the result is complete).
       When searching, just use the identifier name without any special characters, prefixes or suffixes. The search is 
       case-insensitive. Files matching .gitignore or .repomap/ignore are skipped; if hits come from vendored,
       minified or generated files, suggest adding them to .repomap/ignore. A 'hint' key says so on the first call of
       the server process, when .repomap/ was just created, or when hit files look generated (*.min.js, dist/, vendor/);
       without it the advice still holds.

    Args:
        query: Search query (identifier name)
        max_results: Maximum number of hits to return (hits on nearby lines count separately before merging)
        context_lines: Number of lines of context to show
        include_definitions: Whether to include definition occurrences
        include_references: Whether to include reference occurrences
    
    Returns:
        Dictionary containing search results or error message, plus 'parse_errors' if any files failed to parse
        and 'config_warnings' if .repomap/config.toml or areas.toml has problems (tell the user)
    """
    project_root = PROJECT_ROOT
    if not os.path.isdir(project_root):
        return {"error": f"Project root directory not found: {project_root}"}
    config, areas = load_config(project_root), load_areas(project_root)
    warnings = (config.warnings + moved_tests_hint(config, areas) + areas.warnings
                + stale_entries(project_root, areas, config.git))

    try:
        # Initialize RepoMap with search-specific settings
        repo_map = RepoMap(
            root=project_root,
            token_counter_func=lambda text: count_tokens(text, "gpt-4"),
            file_reader_func=read_text,
            output_handler_funcs={'info': log.info, 'warning': log.warning, 'error': log.error},
            verbose=False,
            exclude_unranked=True,
            config=config,
            areas=areas,
        )

        # Find all source files in the project
        all_files = find_src_files(project_root)
        
        # Get all tags (definitions and references) for all files
        all_tags = []
        items = [(file_path, str(Path(file_path).relative_to(project_root))) for file_path in all_files]
        for _file_path, _rel_path, tags in repo_map.iter_tags(items):
            all_tags.extend(tags)

        # Filter tags based on search query and options. Pygments refs also fire on definition sites,
        # so a ref at the same name and line as a definition is dropped.
        query_lower = query.lower()
        defined = {(t.rel_fname, t.line, t.name) for t in all_tags if t.kind == "def"}
        matching_tags = [
            tag for tag in all_tags
            if query_lower in tag.name.lower()
            and ((tag.kind == "def" and include_definitions)
                 or (tag.kind == "ref" and include_references and (tag.rel_fname, tag.line, tag.name) not in defined))
        ]

        # Exact matches first, then definitions, then non-test files, then file and line
        is_test = test_file_checker(areas)
        matching_tags.sort(key=lambda t: (t.name.lower() != query_lower, t.kind != "def", is_test(t.rel_fname),
                                          t.rel_fname, t.line))
        more_hits = max(0, len(matching_tags) - max_results)
        matching_tags = matching_tags[:max_results]

        # Hits whose context windows touch share one block, placed where its best hit ranks
        blocks = []
        for tag in matching_tags:
            for block in blocks:
                if block[0].rel_fname == tag.rel_fname and any(
                        abs(tag.line - other.line) <= 2 * context_lines + 1 for other in block):
                    block.append(tag)
                    break
            else:
                blocks.append([tag])

        hit_blocks = []
        for block in blocks:
            block.sort(key=lambda t: t.line)
            first = block[0]
            lois = sorted({n for t in block
                           for n in range(max(1, t.line - context_lines), t.line + context_lines + 1)})
            context = repo_map.render_tree(str(Path(project_root) / first.rel_fname), first.rel_fname, lois)
            if context:
                # render_tree opens with "<path>:"; the file entry already names the path
                context = context.split("\n", 1)[1] if context.startswith(first.rel_fname + ":\n") else context
                result = {
                    "line": first.line,
                    "name": first.name,
                    "kind": "def" if any(t.kind == "def" for t in block) else "ref",
                    "exact": any(t.name.lower() == query_lower for t in block),
                    "context": truncate_lines(context, config.search.max_context_line_length),
                }
                if len(block) > 1:
                    result["lines"] = [t.line for t in block]
                if len({t.name for t in block}) > 1:
                    result["names"] = sorted({t.name for t in block})
                hit_blocks.append((first.rel_fname, result))
        if all(r["exact"] for _, r in hit_blocks):
            for _, r in hit_blocks:
                del r["exact"]

        # One entry per file, in the order of each file's best-ranked block; blocks keep rank order within it
        by_file: Dict[str, list] = {}
        for rel_fname, result in hit_blocks:
            by_file.setdefault(rel_fname, []).append(result)
        results = [{"file": f, "hits": hs} for f, hs in by_file.items()]

        response = {"results": results}
        hint = ignore_hint("\n".join(by_file))
        if hint:
            response["hint"] = hint
        if more_hits:
            response["more_hits"] = more_hits
        errors = parse_error_summary(repo_map.parse_errors, project_root)
        if errors:
            response["parse_errors"] = errors
        if warnings:
            response["config_warnings"] = warnings
        return response

    except Exception as e:
        log.exception(f"Error searching identifiers in project '{project_root}': {e}")
        return {"error": f"Error searching identifiers: {str(e)}"}    

# --- Main Entry Point ---
def main():
    global PROJECT_ROOT
    parser = argparse.ArgumentParser(description="RepoMapper MCP server (stdio).")
    parser.add_argument(
        "--root",
        help="Project root to map (default: git root of the current directory, else the current directory)",
    )
    args = parser.parse_args()
    PROJECT_ROOT = resolve_project_root(args.root)
    init_project(PROJECT_ROOT)

    # Run the MCP server
    log.debug("Starting FastMCP server...")
    mcp.run()

if __name__ == "__main__":
    main()
