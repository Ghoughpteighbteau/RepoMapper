"""
Utility functions for RepoMap.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, List
from collections import namedtuple

import pathspec

try:
    import tiktoken
except ImportError:
    print("Error: tiktoken is required. Install with: pip install tiktoken")
    sys.exit(1)

# Tag namedtuple for storing parsed code definitions and references
Tag = namedtuple("Tag", "rel_fname fname line name kind".split())


def count_tokens(text: str, model_name: str = "gpt-4") -> int:
    """Count tokens in text using tiktoken."""
    if not text:
        return 0
    
    try:
        encoding = tiktoken.encoding_for_model(model_name)
    except KeyError:
        # Fallback for unknown models
        encoding = tiktoken.get_encoding("cl100k_base")
    
    return len(encoding.encode(text))


def read_text(filename: str, encoding: str = "utf-8", silent: bool = False) -> Optional[str]:
    """Read text from file with error handling."""
    try:
        return Path(filename).read_text(encoding=encoding, errors='ignore')
    except FileNotFoundError:
        if not silent:
            print(f"Error: {filename} not found.")
        return None
    except IsADirectoryError:
        if not silent:
            print(f"Error: {filename} is a directory.")
        return None
    except OSError as e:
        if not silent:
            print(f"Error reading {filename}: {e}")
        return None
    except UnicodeError as e:
        if not silent:
            print(f"Error decoding {filename}: {e}")
        return None
    except Exception as e:
        if not silent:
            print(f"An unexpected error occurred while reading {filename}: {e}")
        return None


IGNORE_FILE = ".repomapignore"

DEFAULT_IGNORE_PATTERNS = [
    "# Hidden files and directories",
    ".*",
    "",
    "# Dependency, environment and vendored code",
    "node_modules/",
    "__pycache__/",
    "venv/",
    "env/",
    "vendor/",
]

MINIFIED_AVG_LINE_LENGTH = 300


def _git_files(directory: str) -> Optional[List[str]]:
    """List files under directory honoring .gitignore, or None if not in a git repo."""
    try:
        result = subprocess.run(
            ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
            cwd=directory, capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    files = []
    for rel in result.stdout.decode('utf-8', 'surrogateescape').split('\0'):
        full_path = os.path.join(directory, rel)
        if rel and os.path.isfile(full_path):
            files.append(full_path)
    return files


def load_ignore_spec(root: str) -> pathspec.GitIgnoreSpec:
    """Load root/.repomapignore, falling back to the default patterns if it doesn't exist."""
    try:
        with open(os.path.join(root, IGNORE_FILE), encoding="utf-8") as f:
            return pathspec.GitIgnoreSpec.from_lines([f"/{IGNORE_FILE}", *f])
    except FileNotFoundError:
        return pathspec.GitIgnoreSpec.from_lines(DEFAULT_IGNORE_PATTERNS)


def find_src_files(directory: str, ignore_root: Optional[str] = None) -> List[str]:
    """Find source files in a directory, skipping files ignored by git or .repomapignore.

    Patterns in .repomapignore are relative to ignore_root (defaults to directory).
    """
    if not os.path.isdir(directory):
        return [directory] if os.path.isfile(directory) else []

    src_files = _git_files(directory)
    if src_files is None:
        src_files = [os.path.join(root, f) for root, _dirs, files in os.walk(directory) for f in files]

    ignore_root = ignore_root or directory
    spec = load_ignore_spec(ignore_root)
    return [
        f for f in src_files
        if not spec.match_file(os.path.relpath(f, ignore_root).replace(os.sep, "/"))
    ]


def is_parseable(path: str) -> bool:
    """Whether RepoMapper has a tags query for this file's language."""
    from grep_ast import filename_to_lang
    from repomapper.scm import get_scm_fname
    lang = filename_to_lang(path)
    return bool(lang and get_scm_fname(lang))


def is_minified(path: str) -> bool:
    """Heuristic for minified/bundled files: very long average line length."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            code = f.read()
    except OSError:
        return False
    return bool(code) and len(code) / (code.count("\n") + 1) > MINIFIED_AVG_LINE_LENGTH


def init_ignore_file(root: str) -> bool:
    """Create root/.repomapignore with defaults and detected minified files, if missing.

    Returns True if the file was created.
    """
    path = os.path.join(root, IGNORE_FILE)
    if not os.path.isdir(root) or os.path.exists(path):
        return False

    minified = sorted(
        os.path.relpath(f, root).replace(os.sep, "/")
        for f in find_src_files(root) if is_parseable(f) and is_minified(f)
    )
    lines = [
        "# Files RepoMapper skips, in .gitignore syntax. Files ignored by git are skipped too.",
        "# Generated on first run; edit freely. Add vendored, bundled or generated files here.",
        "",
        *DEFAULT_IGNORE_PATTERNS,
    ]
    if minified:
        lines += ["", f"# Detected as minified (average line length > {MINIFIED_AVG_LINE_LENGTH}); remove any false positives", *minified]
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        return False
    return True
