"""Configuration accessors, and the placeholder-key guard.

The API-key check exists because a placeholder value passes a plain
truthiness test and then fails much later as a raw 401 from inside the agent,
which reads like an application bug rather than a missing setting.
"""
import pytest

import config


@pytest.mark.parametrize("value,expected", [
    ("sk-REPLACE_ME", False),          # the placeholder in .env
    ("sk-...", False),                 # the placeholder in .env.example
    ("", False),
    ("   ", False),
    ("changeme", False),
    ("not-a-key", False),
    ("sk-proj-AbC123RealLookingKeyValue0099", True),
    ("sk-AbC123RealLookingKeyValue0099", True),
])
def test_openai_key_detection(monkeypatch, value, expected):
    monkeypatch.setenv("OPENAI_API_KEY", value)
    assert config._openai_key_looks_real() is expected


def test_openai_key_missing_entirely(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert config._openai_key_looks_real() is False


def test_knowledge_paths_exist():
    assert config.KNOWLEDGE_DIR.is_dir()
    assert config.DATA_DICT_DIR.is_dir()
    assert config.DATA_DICT_README.exists()
    # The dictionary is the agent's ONLY curated knowledge source; the old
    # domain_rules.md playbook was retired, so nothing may reintroduce it.
    assert not hasattr(config, "DOMAIN_RULES_FILE")
    assert not (config.KNOWLEDGE_DIR / "domain_rules.md").exists()


def test_known_databases_are_the_three_expected():
    dbs = config._known_databases()
    assert len(dbs) == 3
    assert config._bronze_db() in dbs
    assert config._silver_db() in dbs
    assert config._master_db() in dbs
