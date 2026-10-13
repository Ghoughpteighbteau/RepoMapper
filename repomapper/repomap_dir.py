"""The per-project .repomap/ folder: ignore patterns, area hints, settings and caches."""
import os
from typing import List

from repomapper.utils import REPOMAP_DIR, IGNORE_FILE, ignore_file_content, find_src_files
from repomapper.areas import TEST_GLOB_ENTRIES, starter_entries

CACHE_DIR = f"{REPOMAP_DIR}/cache"

GITIGNORE = """\
# Caches are local; everything else in .repomap/ is meant to be committed and shared.
cache/
"""

AREAS_TEMPLATE = """\
# Hints about parts of this project that RepoMapper can't derive from the code.
# [paths."..."] keys are paths relative to the project root; [globs."..."] keys are patterns. A glob without
# a slash matches file or folder names anywhere ("*_test.*"); with one it matches the whole path from the
# root, `*` within a folder name and `**` across folders ("tests/**" is tests/ and everything below it).
#
# Each key is resolved on its own, from the strongest entry that sets it. Strongest first: the path's own
# [paths] entry, then globs matching the path (in file order), then the same for its folder, its parent and
# so on up. So a [paths] entry beats a glob, and a glob matching a file beats an entry for its folder:
# "*_test.*" below keeps tests low inside a "core" folder, and the folder's note still applies to them.
#
#   importance  = "core" | "normal" | "low"          (default "normal") share of the outline and ranking
#   status      = "active" | "legacy" | "deprecated"  (default "active") shown on the outline and seeded maps
#   replaced_by = "path"   where to look instead of legacy or deprecated code
#   fold        = true     always one line in the outline (seeded maps still open it)
#   note        = "..."    one line shown next to the path
#   kind        = "test" | "config"   counted in the unseeded outline, but no sample names or dependency edges
#
# Examples:
#
# [paths."src/api"]
# importance = "core"
# note = "REST backend; all DB access goes through lib/repositories"
#
# [paths."maps"]
# importance = "low"
# status = "legacy"
# replaced_by = "src/api"
# note = "GIS service; legacy but still developed"
#
# [paths."src/old-reports"]
# status = "deprecated"
# fold = true

# Tests and tool configs. Remove or override what doesn't fit this project.

""" + TEST_GLOB_ENTRIES + """
[globs."karma*.conf.js"]
kind = "config"

[globs."eslint.config.*"]
kind = "config"

[globs.".eslintrc.*"]
kind = "config"

[globs."jest.config.*"]
kind = "config"

[globs."vite.config.*"]
kind = "config"

[globs."webpack.config.*"]
kind = "config"

[globs."babel.config.*"]
kind = "config"

[globs."tailwind.config.*"]
kind = "config"

[globs."postcss.config.*"]
kind = "config"

[globs."prettier.config.*"]
kind = "config"

[globs."docker-bake.*"]
kind = "config"

[globs."setup.py"]
kind = "config"

[globs."noxfile.py"]
kind = "config"
"""

CONFIG_TEMPLATE = """\
# RepoMapper settings. Every key is optional; the commented values are the defaults.

[map]
# token_limit = 8192

[languages]
# Files only link through shared names within a language family (a language in none is its own family).
# Test and config files are marked with kind in areas.toml.
# families = [["javascript", "typescript", "tsx"], ["c", "cpp", "objc"], ["java", "kotlin", "scala", "groovy"]]

[ranking]
# pagerank_alpha = 0.85
# chat_file_personalization = 100.0     # PageRank seed for files in the chat
# mentioned_file_personalization = 10.0 # PageRank seed for mentioned files
# chat_file_ref_weight = 50.0           # edge multiplier for references made from chat files
# core_importance = 4.0                 # rank multiplier for files in core areas, after PageRank, when nothing is seeded (normal is 1)
# low_importance = 0.25                 # rank multiplier for files in low areas, after PageRank, when nothing is seeded

[ranking.identifier_weight]
# Edge weight multipliers per identifier.
# mentioned = 10.0                # identifier mentioned in the conversation
# specific = 5.0                  # snake_case or camelCase name at least specific_min_length long
# specific_min_length = 8
# private = 0.1                   # name starts with "_"
# common = 0.1                    # defined in more than common_definers files
# common_definers = 5

[ranking.definition_boost]
# Multipliers on a definition's rank in the signature map.
# mentioned_ident = 10.0
# mentioned_file = 5.0
# chat_file = 20.0

[outline]
# defs_per_file = 4
# files_per_dir = 10
# dirs_per_dir = 12            # an open directory lists its biggest subdirectories, then "+N more dirs"
# max_langs = 3
# max_edges = 12
# common_name_dirs = 5         # names defined in more directories than this (ngOnInit, value) aren't sample names
# Builtin and everyday method names (push, set, id, log) are never sample names:
# generic_names = ["all", "add", "array", "describe", "error", "get", "has", "id", "key", "keys", "length", "log", "main", "name", "object", "pop", "push", "remove", "run", "set", "toString", "type", "update", "value", "values", "version"]

[search]
# max_context_line_length = 200

[git]
# history_years = 2             # how far back git history signals look
# bulk_commit_files = 50        # commits touching more files are skipped (reformats, renames, bumps)
# recency_half_life_days = 90   # recent commits tilt unseeded ranking; a commit's weight halves over this
# max_activity_boost = 3.0      # cap on that tilt (seed x 1..1+this); 0 turns it off
# stale_area_commits = 100      # report areas.toml entries with this many commits under them since last edited
"""


def init_repomap_dir(root: str) -> List[str]:
    """Create root/.repomap/ and whichever of its files are missing; never overwrite existing ones.

    Returns the created paths, relative to root.
    """
    if not os.path.isdir(root):
        return []
    files = [
        (IGNORE_FILE, lambda: ignore_file_content(root)),
        (f"{REPOMAP_DIR}/areas.toml", lambda: AREAS_TEMPLATE + starter_entries(root, find_src_files(root))),
        (f"{REPOMAP_DIR}/config.toml", lambda: CONFIG_TEMPLATE),
        (f"{REPOMAP_DIR}/.gitignore", lambda: GITIGNORE),
    ]
    created = []
    try:
        os.makedirs(os.path.join(root, CACHE_DIR), exist_ok=True)
        for rel, content in files:
            path = os.path.join(root, rel)
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content())
                created.append(rel)
    except OSError:
        pass
    return created
