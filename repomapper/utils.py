"""
Utility functions for RepoMap.
"""

import functools
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, List
from collections import defaultdict, namedtuple

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


REPOMAP_DIR = ".repomap"
IGNORE_FILE = f"{REPOMAP_DIR}/ignore"

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


def find_git_root(path: str) -> Optional[str]:
    """Return the git repository root containing path, or None if it isn't in a repo."""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=path, capture_output=True, check=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


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
    """Load root/.repomap/ignore, falling back to the default patterns if it doesn't exist."""
    try:
        with open(os.path.join(root, IGNORE_FILE), encoding="utf-8") as f:
            return pathspec.GitIgnoreSpec.from_lines([f"/{REPOMAP_DIR}/", *f])
    except FileNotFoundError:
        return pathspec.GitIgnoreSpec.from_lines(DEFAULT_IGNORE_PATTERNS)


def find_src_files(directory: str, ignore_root: Optional[str] = None) -> List[str]:
    """Find source files in a directory, skipping files ignored by git or .repomap/ignore.

    Patterns in .repomap/ignore are relative to ignore_root (defaults to directory).
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


@functools.lru_cache(maxsize=None)
def _lang_parseable(lang: Optional[str]) -> bool:
    from repomapper.scm import get_scm_fname
    return bool(lang and get_scm_fname(lang))


def is_parseable(path: str) -> bool:
    """Whether RepoMapper has a tags query for this file's language."""
    from grep_ast import filename_to_lang
    return _lang_parseable(filename_to_lang(path))


LARGE_FILE_LINES = 10_000

# Bundler fingerprints: rollup/esbuild rename clashing identifiers to name$1 / name$$0,
# webpack and esbuild emit their own runtime helpers.
BUNDLER_RENAME = re.compile(r"[A-Za-z_]\w*\$\$?\d+\b")
BUNDLER_RENAME_MIN = 20
BUNDLER_HELPERS = ("__webpack_require__", "__commonJS(", "__toESM(")

# JS/TS barrels: files made only of imports and re-exports (e.g. an index.ts of `export * from`).
# They define nothing, so they only add noise edges to the graph.
BARREL_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
JS_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
JS_MODULE_STATEMENT = re.compile(
    r"""(?:import|export)\b[^;'"]*?\bfrom\s*(['"])[^'"]*\1\s*;?"""   # import/export ... from '...'
    r"""|import\s*(['"])[^'"]*\2\s*;?"""                          # import '...'
    r"""|export\s+(?:type\s+)?\{[^}]*\}\s*;?"""                   # export { a, b }
)
REEXPORT = re.compile(r"\bexport\b[^;'\"]*?\bfrom\b")


def is_barrel(path: str, code: str) -> bool:
    """Whether a JS/TS file only imports and re-exports other modules."""
    if not path.endswith(BARREL_EXTENSIONS):
        return False
    code = JS_COMMENT.sub("", code)
    return bool(REEXPORT.search(code)) and not JS_MODULE_STATEMENT.sub("", code).strip()


# Minifiers that keep line breaks still rename to single letters; judged on definition names
MINIFIED_MIN_DEFS = 10
MINIFIED_SINGLE_LETTER_SHARE = 0.5
JS_DEFINITION = re.compile(r"\b(?:function\s*\*?|class|var|let|const)\s+([A-Za-z_$][\w$]*)")


def mostly_single_letters(names: List[str]) -> bool:
    """Whether most of a file's definition names are one character, as minifiers leave them."""
    return (len(names) >= MINIFIED_MIN_DEFS
            and sum(len(n) == 1 for n in names) / len(names) > MINIFIED_SINGLE_LETTER_SHARE)


DETECTED_SECTIONS = [
    ("minified", f"Detected as minified (average line length > {MINIFIED_AVG_LINE_LENGTH}, "
                 "or definitions mostly single letters)"),
    ("bundled", "Detected as bundled (bundler-renamed identifiers or runtime helpers)"),
    ("barrel", "Detected as JS/TS barrels (only imports and re-exports)"),
    ("large", f"Detected as very large (> {LARGE_FILE_LINES} lines), possibly bundled or generated"),
]


def classify_generated(path: str) -> Optional[str]:
    """Return 'minified', 'bundled', 'barrel' or 'large' if the file looks generated or uninformative, else None."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            code = f.read()
    except OSError:
        return None
    if not code:
        return None
    lines = code.count("\n") + 1
    if len(code) / lines > MINIFIED_AVG_LINE_LENGTH:
        return "minified"
    if path.endswith(BARREL_EXTENSIONS) and mostly_single_letters(JS_DEFINITION.findall(code)):
        return "minified"
    if any(h in code for h in BUNDLER_HELPERS) or len(BUNDLER_RENAME.findall(code)) >= BUNDLER_RENAME_MIN:
        return "bundled"
    if is_barrel(path, code):
        return "barrel"
    if lines > LARGE_FILE_LINES:
        return "large"
    return None


def ignore_file_content(root: str) -> str:
    """Default patterns plus files under root detected as minified, bundled, barrels or very large."""
    detected = defaultdict(list)
    for f in find_src_files(root):
        kind = is_parseable(f) and classify_generated(f)
        if kind:
            detected[kind].append(os.path.relpath(f, root).replace(os.sep, "/"))

    lines = [
        "# Files RepoMapper skips, in .gitignore syntax, relative to the project root.",
        "# Files ignored by git are skipped too.",
        "# Generated on first run; edit freely. Add vendored, bundled or generated files here.",
        "",
        *DEFAULT_IGNORE_PATTERNS,
    ]
    for kind, heading in DETECTED_SECTIONS:
        if detected[kind]:
            lines += ["", f"# {heading}; remove any false positives", *sorted(detected[kind])]
    return "\n".join(lines) + "\n"
