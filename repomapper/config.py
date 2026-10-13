"""Settings from .repomap/config.toml. Every key is optional; bad or unknown entries are reported, not fatal."""
import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from typing import Any, List, Set

from repomapper.utils import REPOMAP_DIR

CONFIG_FILE = f"{REPOMAP_DIR}/config.toml"


@dataclass
class MapConfig:
    token_limit: int = 8192


@dataclass
class LanguagesConfig:
    families: List[List[str]] = field(default_factory=lambda: [
        ["javascript", "typescript", "tsx"], ["c", "cpp", "objc"], ["java", "kotlin", "scala", "groovy"],
    ])


@dataclass
class IdentifierWeightConfig:
    mentioned: float = 10.0
    specific: float = 5.0
    specific_min_length: int = 8
    private: float = 0.1
    common: float = 0.1
    common_definers: int = 5


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


@dataclass
class DefinitionBoostConfig:
    mentioned_ident: float = 10.0
    mentioned_file: float = 5.0
    chat_file: float = 20.0


@dataclass
class RankingConfig:
    pagerank_alpha: float = 0.85
    chat_file_personalization: float = 100.0
    mentioned_file_personalization: float = 10.0
    chat_file_ref_weight: float = 50.0
    core_importance: float = 4.0
    low_importance: float = 0.25
    identifier_weight: IdentifierWeightConfig = field(default_factory=IdentifierWeightConfig)
    definition_boost: DefinitionBoostConfig = field(default_factory=DefinitionBoostConfig)


@dataclass
class OutlineConfig:
    defs_per_file: int = 4
    files_per_dir: int = 10
    dirs_per_dir: int = 12
    max_langs: int = 3
    max_edges: int = 12
    common_name_dirs: int = 5
    type_boost: float = 3.0           # sample score multiplier for classes, interfaces, types and enums
    short_name_length: int = 6        # names shorter than this get short_name_penalty as samples
    short_name_penalty: float = 0.3
    # Builtin and everyday method names: defined in few places but called everywhere, so they rank high
    generic_names: List[str] = field(default_factory=lambda: ["all", "add", "array", "describe", "error", "get", "has", "id", "key", "keys", "length", "log", "main", "name", "object", "pop", "push", "remove", "run", "set", "toString", "type", "update", "value", "values", "version"])


@dataclass
class SearchConfig:
    max_context_line_length: int = 200


@dataclass
class GitConfig:
    history_years: int = 2
    bulk_commit_files: int = 50
    recency_half_life_days: int = 90
    max_activity_boost: float = 3.0
    stale_area_commits: int = 100


@dataclass
class Config:
    map: MapConfig = field(default_factory=MapConfig)
    languages: LanguagesConfig = field(default_factory=LanguagesConfig)
    ranking: RankingConfig = field(default_factory=RankingConfig)
    outline: OutlineConfig = field(default_factory=OutlineConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    git: GitConfig = field(default_factory=GitConfig)
    # Problems found while loading, for the report
    warnings: List[str] = field(default_factory=list, compare=False)
    # The file still has the old [tests] section (test globs now live in areas.toml)
    stale_tests: bool = field(default=False, compare=False)


def _matches(default: Any, value: Any) -> bool:
    if isinstance(default, bool) or isinstance(value, bool):
        return type(default) is type(value)
    if isinstance(default, float):
        return isinstance(value, (int, float))
    if isinstance(default, list):
        return isinstance(value, list)
    return isinstance(value, type(default))


def _apply(target: Any, table: dict, prefix: str, warnings: List[str]) -> None:
    """Copy table's values onto the target dataclass, keeping defaults for bad entries."""
    fields = {f.name for f in dataclasses.fields(target)} - {"warnings", "stale_tests"}
    for key, value in table.items():
        name = f"{prefix}{key}"
        if key not in fields:
            warnings.append(f"{CONFIG_FILE}: unknown key '{name}'")
            continue
        default = getattr(target, key)
        if dataclasses.is_dataclass(default):
            if isinstance(value, dict):
                _apply(default, value, f"{name}.", warnings)
            else:
                warnings.append(f"{CONFIG_FILE}: '{name}' should be a table")
        elif _matches(default, value):
            setattr(target, key, float(value) if isinstance(default, float) else value)
        else:
            warnings.append(f"{CONFIG_FILE}: '{name}' should be {type(default).__name__}, got {value!r}; using {default!r}")


def load_config(root: str) -> Config:
    """Load root/.repomap/config.toml over the defaults; a missing file gives the defaults."""
    config = Config()
    try:
        with open(os.path.join(root, CONFIG_FILE), "rb") as f:
            table = tomllib.load(f)
    except FileNotFoundError:
        return config
    except (OSError, tomllib.TOMLDecodeError) as e:
        config.warnings.append(f"{CONFIG_FILE}: {e}; using defaults")
        return config
    if "tests" in table:
        table = {k: v for k, v in table.items() if k != "tests"}
        config.stale_tests = True
        config.warnings.append(f"{CONFIG_FILE}: unknown key 'tests'; test globs moved to "
                               '.repomap/areas.toml as [globs."..."] entries with kind = "test". Remove it')
    _apply(config, table, "", config.warnings)
    return config
