"""Data dictionary lookup: index parsing, name normalisation, tool output.

The table -> doc index is parsed out of a README maintained in a *different*
repo (reporting-deltalake), so a formatting change upstream could silently
break the lookup. These tests are the guard against that.
"""
import pytest

import knowledge
import tools
from config import DATA_DICT_DIR

CORE_TABLES = [
    "mastermusic", "track_active", "disambiguation_v1", "music_streams_v3",
    "music_fetches", "playactivity_v2", "users", "playlists", "subscriptions",
    "groups", "musicowners", "devices",
]


@pytest.fixture(scope="module")
def index():
    return knowledge._data_dict_index()


def test_index_is_populated(index):
    assert len(index) >= len(CORE_TABLES)


@pytest.mark.parametrize("table", CORE_TABLES)
def test_core_table_is_indexed(index, table):
    assert table in index, f"{table} missing from the data dictionary index"


def test_every_indexed_doc_exists(index):
    missing = sorted({d for d in index.values() if not (DATA_DICT_DIR / d).exists()})
    assert not missing, f"index points at missing docs: {missing}"


def test_every_doc_is_reachable(index):
    docs = {f.name for f in DATA_DICT_DIR.glob("*.md") if f.name.lower() != "readme.md"}
    assert not (docs - set(index.values())), "docs exist that the index cannot reach"


@pytest.mark.parametrize("raw,expected", [
    ('"tg-deltalake-bronze"."mastermusic"', "mastermusic"),
    ("tg-master.groups", "groups"),
    ("  Music_Streams_V3 ", "music_streams_v3"),
    ("`users`", "users"),
    ("mastermusic", "mastermusic"),
])
def test_table_name_normalisation(raw, expected):
    assert knowledge._normalise_table_name(raw) == expected


def test_preamble_has_shared_rules_but_not_the_table_index():
    pre = knowledge._data_dict_preamble()
    assert "Shared vocabulary" in pre
    assert "Rules that apply" in pre
    # The agent already has the docs it asked for; the index would be dead weight.
    assert "| Table | Layer | Doc |" not in pre


def test_tool_returns_requested_docs_with_preamble():
    out = tools.get_data_dictionary.invoke({"tables": "mastermusic, track_active"})
    assert "# mastermusic" in out
    assert "# track_active" in out
    assert "Shared vocabulary" in out
    assert 5_000 < len(out) < 30_000, f"unexpected payload size: {len(out)}"


def test_tool_accepts_database_qualified_names():
    out = tools.get_data_dictionary.invoke(
        {"tables": '"tg-deltalake-bronze"."mastermusic"'})
    assert "# mastermusic" in out


def test_tables_sharing_a_doc_are_not_duplicated():
    out = tools.get_data_dictionary.invoke({"tables": "playlists, radiostations"})
    assert out.count("# playlists, radiostations") == 1


def test_unknown_table_is_reported_not_dropped():
    out = tools.get_data_dictionary.invoke({"tables": "music_streams_v3, webhooklog"})
    assert "# music_streams_v3" in out          # the known one still comes back
    assert "NOT DOCUMENTED" in out and "webhooklog" in out


def test_all_unknown_lists_what_is_documented():
    out = tools.get_data_dictionary.invoke({"tables": "webhooklog"})
    assert "No data dictionary entry" in out
    assert "mastermusic" in out


def test_empty_input_is_handled():
    out = tools.get_data_dictionary.invoke({"tables": "  ,  "})
    assert "Name at least one table" in out


def test_playbook_reader_is_gone():
    """domain_rules.md was retired — the dictionary is the single source of
    data knowledge. Its reader and section-extractor must not come back."""
    assert not hasattr(knowledge, "_read_domain_rules")
    assert not hasattr(knowledge, "_extract_domain_section")
