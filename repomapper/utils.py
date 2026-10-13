"""
Utility functions for RepoMap.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, List
from collections import namedtuple

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


SKIP_DIRS = {'node_modules', '__pycache__', 'venv', 'env', 'vendor'}


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
        if not rel:
            continue
        parts = Path(rel).parts
        if any(p.startswith('.') or p in SKIP_DIRS for p in parts):
            continue
        full_path = os.path.join(directory, rel)
        if os.path.isfile(full_path):
            files.append(full_path)
    return files


def find_src_files(directory: str) -> List[str]:
    """Find source files in a directory, skipping files ignored by git."""
    if not os.path.isdir(directory):
        return [directory] if os.path.isfile(directory) else []

    git_files = _git_files(directory)
    if git_files is not None:
        return git_files

    src_files = []
    for root, dirs, files in os.walk(directory):
        # Skip hidden directories and common non-source directories
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in SKIP_DIRS]
        for file in files:
            if not file.startswith('.'):
                src_files.append(os.path.join(root, file))
    return src_files
