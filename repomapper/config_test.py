import re

from repomapper.config import Config, load_config
from repomapper.repomap_dir import CONFIG_TEMPLATE


def write_config(root, text):
    (root / ".repomap").mkdir(exist_ok=True)
    (root / ".repomap" / "config.toml").write_text(text)


def test_missing_file_gives_defaults(tmp_path):
    assert load_config(str(tmp_path)) == Config()


def test_template_lists_every_key_with_its_default(tmp_path):
    # Uncommenting each "# key = value" line must load cleanly and change nothing
    uncommented = re.sub(r"^# (\w+ = )", r"\1", CONFIG_TEMPLATE, flags=re.M)
    write_config(tmp_path, uncommented)

    config = load_config(str(tmp_path))

    assert config.warnings == []
    assert config == Config()


def test_values_override_defaults(tmp_path):
    write_config(tmp_path, "[map]\ntoken_limit = 2048\n[ranking.identifier_weight]\ncommon = 0\n")

    config = load_config(str(tmp_path))

    assert config.map.token_limit == 2048
    assert config.ranking.identifier_weight.common == 0.0
    assert config.ranking.identifier_weight.private == 0.1
    assert config.warnings == []


def test_unknown_keys_and_bad_types_are_reported_not_fatal(tmp_path):
    write_config(tmp_path, '[map]\ntoken_limit = "big"\n[outline]\nmax_edge = 3\n[nope]\n')

    config = load_config(str(tmp_path))

    assert config.map.token_limit == 8192
    assert len(config.warnings) == 3
    assert any("outline.max_edge" in w for w in config.warnings)


def test_invalid_toml_falls_back_to_defaults(tmp_path):
    write_config(tmp_path, "[map\n")

    config = load_config(str(tmp_path))

    assert config.map == Config().map
    assert len(config.warnings) == 1
