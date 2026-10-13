import argparse
import asyncio
import json
import os
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any, Set
import dataclasses

from fastmcp import FastMCP, settings
from repomapper.repomap_class import RepoMap
from repomapper.utils import count_tokens, read_text, find_src_files, init_ignore_file, find_git_root
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
    "suggest the user add gitignore-style patterns for them to .repomapignore in the project root."
)

CREATED_HINT = (
    "Created .repomapignore in the project root with default patterns and any files detected as minified. "
    "Tell the user so they can review it and remove false positives."
)


def ignore_hint(created: bool) -> str:
    return f"{CREATED_HINT} {IGNORE_HINT}" if created else IGNORE_HINT


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
    token_limit: Any = 8192,  # Accept any type to handle empty strings
    exclude_unranked: bool = False,
    force_refresh: bool = False,
    mentioned_files: Optional[List[str]] = None,
    mentioned_idents: Optional[List[str]] = None,
    verbose: bool = False,
    max_context_window: Optional[int] = None,
) -> Dict[str, Any]:
    """Generate a repository map for the specified files, providing a list of function prototypes and variables for files as well as relevant related
    files. Provide filenames relative to the project root. In addition to the files provided, relevant related files will also be included with a
    very small ranking boost.

    Files matching .gitignore or .repomapignore (gitignore syntax, in the project root) are skipped. If the map is
    dominated by vendored, minified, bundled or generated files, suggest adding them to .repomapignore.

    :param chat_files: A list of file paths that are currently in the chat context. These files will receive the highest ranking.
    :param other_files: A list of other relevant file paths in the repository to consider for the map. They receive a lower ranking boost than mentioned_files and chat_files.
    :param token_limit: The maximum number of tokens the generated repository map should occupy. Defaults to 8192.
    :param exclude_unranked: If True, files with a PageRank of 0.0 will be excluded from the map. Defaults to False.
    :param force_refresh: If True, forces a refresh of the repository map cache. Defaults to False.
    :param mentioned_files: Optional list of file paths explicitly mentioned in the conversation and receive a mid-level ranking boost.
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
            - 'total_files_considered': total files processed
            - 'parse_errors' (only if any): count of files that failed to parse and a few example errors
        Or an 'error' key if an error occurred.
    """
    project_root = PROJECT_ROOT
    if not os.path.isdir(project_root):
        return {"error": f"Project root directory not found: {project_root}"}
    hint = ignore_hint(init_ignore_file(project_root))

    # 1. Handle and validate parameters
    # Convert token_limit to integer with fallback
    try:
        token_limit = int(token_limit) if token_limit else 8192
    except (TypeError, ValueError):
        token_limit = 8192
    
    # Ensure token_limit is positive
    if token_limit <= 0:
        token_limit = 8192
    
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
            max_context_window=max_context_window
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
            mentioned_idents=mentioned_idents_set,
            force_refresh=force_refresh
        )
        
        # Convert FileReport to dictionary for JSON serialization
        report_dict = {
            "excluded": file_report.excluded,
            "definition_matches": file_report.definition_matches,
            "reference_matches": file_report.reference_matches,
            "total_files_considered": file_report.total_files_considered
        }
        errors = parse_error_summary(file_report.parse_errors, project_root)
        if errors:
            report_dict["parse_errors"] = errors
        
        return {
            "map": map_content or "No repository map could be generated.",
            "report": report_dict,
            "hint": hint,
        }
    except Exception as e:
        log.exception(f"Error generating repository map for project '{project_root}': {e}")
        return {"error": f"Error generating repository map: {str(e)}"}
    
MAX_CONTEXT_LINE_LENGTH = 200


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
    """Search for identifiers in code files. Get back a list of matching identifiers with their file, line number, and context.
       When searching, just use the identifier name without any special characters, prefixes or suffixes. The search is 
       case-insensitive. Files matching .gitignore or .repomapignore are skipped; if hits come from vendored,
       minified or generated files, suggest adding them to .repomapignore.

    Args:
        query: Search query (identifier name)
        max_results: Maximum number of results to return
        context_lines: Number of lines of context to show
        include_definitions: Whether to include definition occurrences
        include_references: Whether to include reference occurrences
    
    Returns:
        Dictionary containing search results or error message, plus 'parse_errors' if any files failed to parse
    """
    project_root = PROJECT_ROOT
    if not os.path.isdir(project_root):
        return {"error": f"Project root directory not found: {project_root}"}
    hint = ignore_hint(init_ignore_file(project_root))

    try:
        # Initialize RepoMap with search-specific settings
        repo_map = RepoMap(
            root=project_root,
            token_counter_func=lambda text: count_tokens(text, "gpt-4"),
            file_reader_func=read_text,
            output_handler_funcs={'info': log.info, 'warning': log.warning, 'error': log.error},
            verbose=False,
            exclude_unranked=True
        )

        # Find all source files in the project
        all_files = find_src_files(project_root)
        
        # Get all tags (definitions and references) for all files
        all_tags = []
        for file_path in all_files:
            rel_path = str(Path(file_path).relative_to(project_root))
            tags = repo_map.get_tags(file_path, rel_path)
            all_tags.extend(tags)

        # Filter tags based on search query and options
        matching_tags = []
        query_lower = query.lower()
        
        for tag in all_tags:
            if query_lower in tag.name.lower():
                if (tag.kind == "def" and include_definitions) or \
                   (tag.kind == "ref" and include_references):
                    matching_tags.append(tag)

        # Sort by relevance (definitions first, then references)
        matching_tags.sort(key=lambda x: (x.kind != "def", x.name.lower().find(query_lower)))

        # Limit results
        matching_tags = matching_tags[:max_results]

        # Format results with context
        results = []
        for tag in matching_tags:
            file_path = str(Path(project_root) / tag.rel_fname)
            
            # Calculate context range based on context_lines parameter
            start_line = max(1, tag.line - context_lines)
            end_line = tag.line + context_lines
            context_range = list(range(start_line, end_line + 1))
            
            context = repo_map.render_tree(
                file_path,
                tag.rel_fname,
                context_range
            )
            
            if context:
                context = truncate_lines(context, MAX_CONTEXT_LINE_LENGTH)
                results.append({
                    "file": tag.rel_fname,
                    "line": tag.line,
                    "name": tag.name,
                    "kind": tag.kind,
                    "context": context
                })

        response = {"results": results, "hint": hint}
        errors = parse_error_summary(repo_map.parse_errors, project_root)
        if errors:
            response["parse_errors"] = errors
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

    # Run the MCP server
    log.debug("Starting FastMCP server...")
    mcp.run()

if __name__ == "__main__":
    main()
