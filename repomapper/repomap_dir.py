"""The per-project .repomap/ folder: ignore patterns, area hints, settings and caches."""
import os
from typing import List

from repomapper.utils import REPOMAP_DIR, IGNORE_FILE, ignore_file_content

CACHE_DIR = f"{REPOMAP_DIR}/cache"

GITIGNORE = """\
# Caches are local; everything else in .repomap/ is meant to be committed and shared.
cache/
"""

AREAS_TEMPLATE = """\
# Hints about parts of this project that RepoMapper can't derive from the code.
# Keys are paths relative to the project root. The nearest entry wins, per key: a child entry
# overrides only the keys it sets, the rest come from the nearest ancestor that sets them.
#
#   importance  = "core" | "normal" | "low"          (default "normal") share of the outline and ranking
#   status      = "active" | "legacy" | "deprecated"  (default "active") shown on the outline and seeded maps
#   replaced_by = "path"   where to look instead of legacy or deprecated code
#   fold        = true     always one line in the outline (seeded maps still open it)
#   note        = "..."    one line shown next to the path
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
"""

CONFIG_TEMPLATE = """\
# RepoMapper settings. Every key is optional; the commented values are the defaults.

[map]
# token_limit = 8192

[tests]
# Test files are demoted in unseeded outlines (counted, but no sample names or edges).
# globs = ["*_test.*", "test_*.*", "*.spec.*", "*.test.*", "tests/**", "**/__tests__/**"]

[languages]
# Files only link through shared names within a language family.
# families = [["javascript", "typescript", "tsx"], ["c", "cpp"]]

[ranking]
# pagerank_alpha = 0.85
# chat_file_personalization = 100.0     # PageRank seed for files in the chat
# mentioned_file_personalization = 10.0 # PageRank seed for mentioned files
# chat_file_ref_weight = 50.0           # edge multiplier for references made from chat files

[ranking.identifier_weight]
# Edge weight multipliers per identifier.
# mentioned = 10.0                # identifier mentioned in the conversation
# specific = 10.0                 # snake_case or camelCase name at least specific_min_length long
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
# max_langs = 3
# max_edges = 12

[search]
# max_context_line_length = 200

[git]
# history_years = 2             # how far back git history signals look
# bulk_commit_files = 50        # commits touching more files are skipped (reformats, renames, bumps)
# recency_half_life_days = 90
"""


def init_repomap_dir(root: str) -> List[str]:
    """Create root/.repomap/ and whichever of its files are missing; never overwrite existing ones.

    Returns the created paths, relative to root.
    """
    if not os.path.isdir(root):
        return []
    files = [
        (IGNORE_FILE, lambda: ignore_file_content(root)),
        (f"{REPOMAP_DIR}/areas.toml", lambda: AREAS_TEMPLATE),
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
