# RepoMap - Command-Line Tool and MCP Server

RepoMap is a powerful tool designed to help, primarily LLMs, understand and navigate complex codebases. It functions both as a command-line application for on-demand analysis and as an MCP (Model Context Protocol) server, providing continuous repository mapping capabilities to other applications. By generating a "map" of the software repository, RepoMap highlights important files, code definitions, and their relationships. It leverages Tree-sitter for accurate code parsing and the PageRank algorithm to rank code elements by importance, ensuring that the most relevant information is always prioritized.


## Table of Contents
- [Aider](#aider)
- [Example Output](#example-output)
- [Features](#features)
- [Installation](#installation)
- [Usage](#usage)
  - [Basic Usage](#basic-usage)
  - [Advanced Options](#advanced-options)
- [How It Works](#how-it-works)
- [Output Format](#output-format)
- [Dependencies](#dependencies)
- [Caching](#caching)
- [Supported Languages](#supported-languages)
- [License](#license)
- [Running as an MCP Server](#running-as-an-mcp-server)
  - [Setup](#setup)
  - [Usage](#usage-1)
- [Changelog](#changelog)
----------

## Aider

RepoMap is 100% based on Aider's Repo map functionality, but I don't believe it shares any code with it. Allow me to explain.

My original effort was to take the RepoMap class from Aider, remove all the aider-specific dependencies, and then make it into a command-line tool. Python isn't my native language and I really struggled to get it to work.

So a few hours ago, I had a different idea. I took the RepoMap and some of its related code from aider and I fed it to an LLM (Either Claude or Gemini 2.5 Pro, can't remember) and had it create specifications for this, basically, from aider's implementation. So it generated a very detailed specification for this application (minus the MCP bits) and then I fed that to, well, Aider with Claude 3.7, and it built the command-line version of this.

I then used a combination of Aider w/Claude 3.7, Cline w/Gemini 2.5 Pro Preview & Gemini 2.5 Flash Preview, and Phind.com, and Gemini.com and Claude.com and ChatGPT.com and after a few hours, I finally got the MCP server sorted out. Again, keeping in mind, Python isn't really my native tongue.

----------

## Example Output

```
> repomap . --chat-files repomap_class.py
Chat files: ['/mnt/programming/RepoMapper/repomap_class.py']
repomap_class.py:
(Rank value: 10.8111)

  36: CACHE_VERSION = 1
  39: TAGS_CACHE_DIR = os.path.join(os.getcwd(), f".repomap.tags.cache.v{CACHE_VERSION}")
  40: SQLITE_ERRORS = (sqlite3.OperationalError, sqlite3.DatabaseError)
  43: Tag = namedtuple("Tag", "rel_fname fname line name kind".split())
  46: class RepoMap:
  49:     def __init__(
  93:     def load_tags_cache(self):
 102:     def save_tags_cache(self):
 459:     def get_ranked_tags_map_uncached(
 483:         def try_tags(num_tags: int) -> Tuple[Optional[str], int]:
 512:     def get_repo_map(

utils.py:
(Rank value: 0.2297)

  18: Tag = namedtuple("Tag", "rel_fname fname line name kind".split())
  21: def count_tokens(text: str, model_name: str = "gpt-4") -> int:
  35: def read_text(filename: str, encoding: str = "utf-8", silent: bool = False) -> Optional[str]:

importance.py:
(Rank value: 0.1149)

   8: IMPORTANT_FILENAMES = {
  27: IMPORTANT_DIR_PATTERNS = {
  34: def is_important(rel_file_path: str) -> bool:
  56: def filter_important_files(file_paths: List[str]) -> List[str]:

    ...
    ...
    ...
```

----------

## Features

-   **Smart Code Analysis**: Uses Tree-sitter to parse source code and extract function/class definitions
-   **Relevance Ranking**: Employs PageRank algorithm to rank code elements by importance
-   **Token-Aware**: Respects token limits to fit within LLM context windows
-   **Caching**: Persistent caching for fast subsequent runs
-   **Multi-Language**: Supports Python, JavaScript, TypeScript, Java, C/C++, Go, Rust, and more
-   **Important File Detection**: Automatically identifies and prioritizes important files (README, requirements.txt, etc.)

----------

## Installation

With [uv](https://docs.astral.sh/uv/) (works on Linux, macOS and Windows; no Python install needed):

```bash
uv tool install git+https://github.com/Ghoughpteighbteau/RepoMapper
```

This installs the `repomap` CLI and the `repomap-mcp` server. Alternatively, `pip install .` from a checkout.

----------

## Usage

### Basic Usage

```bash
# Map current directory
repomap .

# Map specific directory with custom token limit
repomap src/ --map-tokens 2048

# Map specific files
repomap file1.py file2.py

# Specify chat files (higher priority) vs other files
repomap --chat-files main.py --other-files src/

# Specify mentioned files and identifiers
repomap --mentioned-files config.py --mentioned-idents "main_function"

# Enable verbose output
repomap . --verbose

# Force refresh of caches
repomap . --force-refresh

# Specify model for token counting
repomap . --model gpt-3.5-turbo

# Set maximum context window
repomap . --max-context-window 8192

# Exclude files with Page Rank 0
repomap . --exclude-unranked
```

The tool prioritizes files in the following order:

1.  `--chat-files`: These files are given the highest priority, as they're assumed to be the files you're currently working on.
2.  `--mentioned-files`: These files are given a high priority, as they're explicitly mentioned in the current context.
3.  `--other-files`: These files are given the lowest priority and are used to provide additional context.

### Advanced Options

```bash
# Enable verbose output
repomap . --verbose

# Force refresh of caches
repomap . --force-refresh

# Specify model for token counting
repomap . --model gpt-3.5-turbo

# Set maximum context window
repomap . --max-context-window 8192

# Exclude files with Page Rank 0
repomap . --exclude-unranked

# Mention specific files or identifiers for higher priority
repomap . --mentioned-files config.py --mentioned-idents "main_function"
```

----------

## How It Works

1.  **File Discovery**: Scans the repository for source files
2.  **Code Parsing**: Uses Tree-sitter to parse code and extract definitions/references
3.  **Graph Building**: Creates a graph where files are nodes and symbol references are edges
4.  **Ranking**: Applies PageRank algorithm to rank files and symbols by importance
5.  **Token Optimization**: Uses binary search to fit the most important content within token limits
6.  **Output Generation**: Formats the results as a readable code map

----------

## Output Format

The tool generates a structured view of your codebase showing:

-   File paths and important code sections
-   Function and class definitions
-   Key relationships between code elements
-   Prioritized based on actual usage and references

----------

## Dependencies

-   `tiktoken`: Token counting for various LLM models
-   `networkx`: Graph algorithms (PageRank)
-   `diskcache`: Persistent caching
-   `grep-ast`: Tree-sitter integration for code parsing
-   `tree-sitter`: Code parsing framework
-   `pygments`: Syntax highlighting and lexical analysis

----------

## Caching

The tool uses persistent caching to speed up subsequent runs:

-   Cache directory: `.repomap/cache/` (git-ignored by `.repomap/.gitignore`)
-   Automatically invalidated when files change
-   Can be cleared with `--force-refresh`

----------

## Supported Languages

Currently supports languages with Tree-sitter grammars:

-   arduino
-   chatito
-   commonlisp
-   cpp
-   csharp
-   c
-   dart
-   d
-   elisp
-   elixir
-   elm
-   gleam
-   go
-   javascript
-   java
-   lua
-   ocaml_interface
-   ocaml
-   pony
-   properties
-   python
-   racket
-   r
-   ruby
-   rust
-   solidity
-   swift
-   udev
-   c_sharp
-   hcl
-   kotlin
-   php
-   ql
-   scala

----------

## License

This implementation is based on the RepoMap design from the Aider project.

----------

## The `.repomap/` folder

When `repomap` or the MCP server starts, it creates `.repomap/` in the project root with whichever of these
files are missing. Existing files are never overwritten, so your edits stick; delete a file to regenerate it.

```
.repomap/
  ignore        # files to skip, gitignore syntax
  areas.toml    # importance, status and notes per path (commented template)
  config.toml   # settings (commented template listing the defaults)
  cache/        # tag cache, local only
  .gitignore    # ignores cache/
```

Commit everything except `cache/` so the team shares the same hints and exclusions.

`config.toml` is read on every run, so edits apply without restarting the MCP server. Unknown keys and values
of the wrong type are reported (`config_warnings` in MCP responses, warnings in the CLI) and the default is used.

### Area hints

`areas.toml` holds what RepoMapper can't derive from the code, keyed by path relative to the project root:

```toml
[paths."src/api"]
importance = "core"            # core | normal | low: tilts ranking when no files are given
note = "REST backend; all DB access goes through lib/repositories"

[paths."maps"]
status = "legacy"              # active | legacy | deprecated
replaced_by = "src/api"
fold = true                    # always one line in the outline
```

Status, `replaced_by` and notes show on outline lines, and at the top of signature maps that include files in
that area. The nearest entry wins per key, so a child entry only overrides the keys it sets. Unknown keys or
values and paths that don't exist are reported like config problems.

When it's first created, `areas.toml` also lists the project's top-level directories (and the subdirectories of
very large ones) as commented-out entries with hints: file count, languages, last commit, the first sentence of
the directory's README, and a `legacy` suggestion for directories idle for over a year. Uncomment what's useful.
Entries with many commits under their path since they were last edited (`git.stale_area_commits`, 100 by
default) are reported so notes don't silently go out of date.

### Ignoring files

Files ignored by git are skipped automatically. Everything else RepoMapper skips is listed in `.repomap/ignore`,
which uses `.gitignore` syntax with paths relative to the project root.

It starts with default patterns (hidden files, `node_modules/`, `__pycache__/`, `venv/`, `env/`, `vendor/`)
plus any files that look minified, bundled, like JS/TS barrels, or very large, each under its own heading.
Review it, remove false positives, and add anything else that isn't useful in a map:

```gitignore
# rollup bundle of the MCP server
utils/mcp/server.js
```

The MCP tools remind agents of this file, so they can suggest entries when results look noisy.

## Running as an MCP Server

RepoMap can also be run as an MCP (Model Context Protocol) server, allowing other applications to access its repository mapping capabilities.

### Setup

1. The RepoMap MCP server uses STDIO (standard input/output) for communication. No additional configuration is required for the transport layer.
2. Install it (requires [uv](https://docs.astral.sh/uv/)), which puts `repomap` and `repomap-mcp` on your PATH:

```bash
uv tool install git+https://github.com/Ghoughpteighbteau/RepoMapper
```

3. Register it with your MCP client. The client launches the server itself; there is no port.

Claude Code:

```bash
claude mcp add repomap -- repomap-mcp
```

JSON-configured clients (Cline, Roo, Claude Desktop, Cursor, ...):

```json
{
  "mcpServers": {
    "RepoMapper": {
      "type": "stdio",
      "command": "repomap-mcp"
    }
  }
}
```

### Usage

1. Running `repomap-mcp` directly starts the server and waits for a client on STDIO:

```bash
repomap-mcp                     # maps the git root of the current directory (or the directory itself)
repomap-mcp --root /path/to/project
```

2. The server will start and listen for requests via STDIO.
3. Other applications can then use the `repo_map` and `search_identifiers` tools. The project root is fixed when
   the server starts, so tool calls don't pass it, and file paths in tool arguments are relative to it. Clients
   that launch servers from the project directory (like Claude Code) need no configuration. Global clients
   (like desktop apps) should pass `--root` in the server's `args`, one server per project.


## Changelog

7/13/2025 - Removed the project.json dependency. Fixed the MCP server to be a little easier for the LLM to work with in terms of filenames.