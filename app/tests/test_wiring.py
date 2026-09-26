"""Structural guards on how the modules fit together.

These are cheap and catch the class of mistake a refactor introduces:
a broken cross-module reference, a dependency cycle, a tool that silently
stops being registered, or a prompt that no longer names the tool it relies on.
"""
import importlib

import pytest

# Dependency order: each module may only import ones listed before it.
MODULE_ORDER = [
    "config", "aws", "results", "run_state", "knowledge", "entities",
    "models", "prompt", "tools", "agent", "ui", "auth", "app",
]


@pytest.mark.parametrize("name", MODULE_ORDER)
def test_module_imports_cleanly(name):
    importlib.import_module(name)


def test_no_import_cycles():
    """Importing in dependency order must not require anything defined later."""
    for name in MODULE_ORDER:
        importlib.import_module(name)


EXPECTED_TOOLS = {
    "get_data_dictionary", "capture_finding",
    "list_databases", "list_tables", "describe_table", "count_rows",
    "resolve_label", "resolve_client", "sql_db_query_checker", "sql_db_query",
    "visualize_results", "note_default_applied",
}


def test_every_tool_is_registered_on_the_agent():
    """A tool that exists but isn't passed to create_agent is invisible."""
    import agent
    import inspect
    src = inspect.getsource(agent.build_agent)
    missing = {t for t in EXPECTED_TOOLS if t not in src}
    assert not missing, f"tools defined but not registered: {sorted(missing)}"


def test_sql_checker_uses_the_selected_model():
    """The checker builds its own LLM; it must follow the sidebar choice, not
    default to OpenAI. This broke once when the modules were split."""
    import models
    import tools as tools_mod
    import inspect
    # @tool wraps the function in a StructuredTool; .func is the original.
    src = inspect.getsource(tools_mod.sql_db_query_checker.func)
    assert "models.build_active_llm()" in src
    import run_state
    run_state.start_run(model_choice="deepseek")
    assert models.active_model_choice() == "deepseek"
    run_state.start_run(model_choice="glm")


def test_prompt_puts_the_dictionary_first():
    import prompt
    text = prompt._build_system_prompt()
    assert "get_data_dictionary(tables)" in text
    assert "PRECEDENCE, when sources disagree" in text
    assert "Call get_data_dictionary(tables) naming every table" in text
    assert "THE SINGLE AUTHORITY" in text
    # the playbook and its tool were retired — nothing may reference them
    assert "get_domain_knowledge" not in text
    assert "domain_rules" not in text


def test_model_registry_default_is_selectable():
    import models
    assert "glm" in models._MODEL_REGISTRY
    for key, cfg in models._MODEL_REGISTRY.items():
        assert "label" in cfg and "provider" in cfg, f"{key} is malformed"


def test_unknown_model_choice_does_not_fall_back_to_openai(monkeypatch):
    """Regression: _build_llm() used to default an unrecognised choice to the
    OpenAI entry. app.py only key-checks choices whose provider is "openai",
    so a stale or misspelled selection skipped that guard AND got an OpenAI
    client — surfacing as "401 Incorrect API key provided: sk-REPLACE_ME" on a
    run that never intended to touch OpenAI. It must fail loudly instead."""
    import models
    with pytest.raises(ValueError, match="Unknown model choice"):
        models._build_llm("not-a-real-model")


def test_vendor_entries_declare_what_they_need():
    """Each provider must carry the fields _build_llm reads for it, or it fails
    at call time rather than at import."""
    import models
    required = {
        "openai_compatible": ("model_id", "base_url", "api_key_env"),
        "google_genai": ("model_id", "api_key_env"),
        "bedrock_mantle": ("model_id",),
    }
    for key, cfg in models._MODEL_REGISTRY.items():
        for field in required.get(cfg["provider"], ()):
            assert cfg.get(field), f"{key} is missing {field}"


def test_gemini_uses_the_native_sdk_not_the_openai_shim():
    """Regression: Gemini 3.x attaches a thought_signature to every function
    call and requires it back on the next turn. Google's OpenAI-compatible
    endpoint has no field for it, so routing Gemini through ChatOpenAI breaks
    on the SECOND tool call with "Function call is missing a thought_signature"
    — an agent that plans a query then runs it fails every time. Verified that
    reasoning_effort="none" does not avoid it; only the native client works."""
    import models
    cfg = models._MODEL_REGISTRY["gemini"]
    assert cfg["provider"] == "google_genai", (
        "Gemini must not be routed through the OpenAI-compatible shim"
    )
    assert "base_url" not in cfg, "native client takes no base_url"


def test_missing_key_is_caught_before_the_run(monkeypatch):
    """A missing vendor key must be reported up front, not as a mid-run 401.
    Bedrock choices authenticate via SigV4 and must never be blocked here."""
    import models
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert "GEMINI_API_KEY" in models.missing_key_reason("gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "REPLACE_ME")
    assert models.missing_key_reason("gemini") is not None
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-looks-real-enough")
    assert models.missing_key_reason("gemini") is None
    # SigV4 / IAM — no key involved, never blocked
    assert models.missing_key_reason("glm") is None


def test_artist_questions_are_routed_to_artist_name():
    """Regression: asked for the top 5 artists in SHUS, the agent grouped by
    owner_name and returned distributors ("Warner", "Universal Fitness") as
    artists — a confident wrong answer with nothing to signal the swap."""
    import prompt
    text = prompt._build_system_prompt()
    assert "ARTIST QUESTIONS USE artist_name" in text
    assert "NEVER answer an artist\n  question with owner_name" in text


def test_metadata_columns_are_documented_with_their_stock_type():
    """artist_name was documented nowhere in the dictionary, which is why the
    agent reached for the name column that *was* documented. Stock-type
    scoping matters too: a duration average that forgets it silently averages
    over track rows only."""
    from config import DATA_DICT_DIR
    text = (DATA_DICT_DIR / "mastermusic.md").read_text(encoding="utf-8")
    for col in ("artist_name", "artist_id", "title", "duration_secs",
                "isrc", "upc"):
        assert f"`{col}`" in text, f"{col} is undocumented"
    assert "Artist vs label vs distributor" in text
    # The track/album split is the part that silently skews aggregates.
    assert "Tracks only" in text and "Albums only" in text


def test_store_catalogue_questions_do_not_join_mastermusic():
    """Regression for a real wrong answer: asked for SHUS's active catalogue
    size, the agent joined mastermusic to apply `status = 1`. A row only lands
    in track_active AFTER every availability check has been applied upstream,
    so the join cannot add correctness — it dropped 132 tracks (mastermusic
    refreshes daily, track_active hourly) and scanned 55x more data."""
    import prompt
    text = prompt._build_system_prompt()
    assert "STORE CATALOGUE" in text
    assert "track_active ALONE" in text
    assert "NEVER join mastermusic" in text


def test_status_filter_rule_is_scoped_to_its_table():
    """"Always status = 1" used to sit in the retired playbook under
    `# Domain: catalogue`, reading as a global rule — which is why the agent
    dragged mastermusic into a track_active question. In the dictionary it
    must name the table it applies to."""
    from config import DATA_DICT_DIR
    text = (DATA_DICT_DIR / "mastermusic.md").read_text(encoding="utf-8")
    i = text.index("**Mandatory filter: `status = 1`**")
    scope = text[i:i + 300]
    assert "mastermusic` only" in scope, "the rule must name the table it applies to"
    assert "track_active" in scope, "and must disclaim the store-catalogue case"


def test_store_catalogue_rule_lives_in_the_dictionary():
    """The playbook is gone, so the no-join rule must be in track_active.md
    or the agent loses it entirely."""
    from config import DATA_DICT_DIR
    text = (DATA_DICT_DIR / "track_active.md").read_text(encoding="utf-8")
    assert "active catalogue size" in text
    assert "Never join `mastermusic` to apply `status = 1`" in text
    assert "METADATA, never filtering" in text


def test_quota_and_auth_failures_get_actionable_messages(monkeypatch):
    """"Please try again" is wrong advice for a quota error (retrying cannot
    help until the window resets) and for an auth error (it never will)."""
    import models
    import run_state
    run_state.start_run(model_choice="gemini")

    quota = models.explain_failure(Exception(
        "429 RESOURCE_EXHAUSTED ... Quota exceeded ... limit: 20, model: "
        "gemini-3.6-flash ... GenerateRequestsPerDayPerProjectPerModel-FreeTier"))
    assert quota and "quota" in quota.lower()
    assert "20" in quota, "should surface the actual cap"
    assert "tomorrow" in quota, "a per-day quota does not reset in a minute"
    assert "try again" not in quota.lower()

    auth = models.explain_failure(Exception("Error code: 401 - invalid_api_key"))
    assert auth and "GEMINI_API_KEY" in auth

    denied = models.explain_failure(Exception(
        "403 User: arn:aws:iam::1:user/x is not authorized to perform: "
        "bedrock-mantle:CreateInference"))
    assert denied and "authoris" in denied.lower()

    # Anything unrecognised must fall through, not invent a remedy.
    assert models.explain_failure(ValueError("some unrelated bug")) is None


def test_app_explains_known_failures_to_end_users():
    """The handler must consult explain_failure() and not bury it behind
    dev_mode — end users are exactly who needs the remedy."""
    import inspect
    import app as app_mod
    src = inspect.getsource(app_mod)
    assert "explained = explain_failure(e)" in src
    assert src.index("explained = explain_failure(e)") < src.index("if dev_mode:\n                        st.error(f\"Agent failed")


def test_sql_checker_flattens_content_before_string_ops():
    """Regression: the checker did `resp.content.strip()`. On Gemini content is
    a list of blocks, so that raised AttributeError: 'list' object has no
    attribute 'strip' and killed the whole agent run mid-query."""
    import inspect
    import tools as tools_mod
    src = inspect.getsource(tools_mod.sql_db_query_checker.func)
    # Comment lines are stripped: the fix's own comment quotes the bad pattern
    # to explain it, which would otherwise match and fail this test.
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#"))
    assert "resp.content.strip()" not in code
    assert "message_text(resp.content)" in code


def test_normalizer_lives_where_every_consumer_can_import_it():
    """It started in ui.py, which tools.py may not import (ui comes later in
    MODULE_ORDER). It belongs with the provider definitions in models.py."""
    import models
    import ui
    assert hasattr(models, "message_text")
    assert MODULE_ORDER.index("models") < MODULE_ORDER.index("tools")
    # ui re-exports the same objects, so display code needn't know where it lives
    assert ui.message_text is models.message_text
    assert ui.GeminiNormalizer is models.GeminiNormalizer


def test_normalizer_base_handles_the_shapes_common_to_every_provider():
    """to_text() is the shared template; only _block_text differs per provider."""
    import ui
    n = ui.GeminiNormalizer()
    assert n.to_text("plain") == "plain"
    assert n.to_text(None) == ""
    assert n.to_text([]) == ""
    assert n.to_text(42) == "42"


def test_gemini_normalizer_flattens_typed_blocks():
    """The native Google client sends a list of typed blocks; rendering it
    directly would print a raw Python repr into the answer pane."""
    import ui
    n = ui.GeminiNormalizer()
    assert n.to_text([{"type": "text", "text": "Fuga "},
                      {"type": "text", "text": "leads."}]) == "Fuga leads."
    # Non-text blocks carry nothing to show and must be dropped, not repr'd.
    assert n.to_text([{"type": "thinking", "thinking": "hmm"},
                      {"type": "text", "text": "answer"}]) == "answer"
    assert n.to_text([{"type": "text"}]) == ""


def test_passthrough_normalizer_is_a_no_op_for_plain_strings():
    """Providers that already return a string need no normalisation."""
    import ui
    n = ui.PassthroughNormalizer()
    text = "Fuga leads with 60 tracks."
    assert n.to_text(text) is text  # identical object, not merely equal
    assert n.to_text(["a", "b"]) == "ab"


def test_normalizer_is_chosen_by_provider():
    """Gemini gets the typed-block reader; everything else gets the default."""
    import ui
    assert isinstance(ui.normalizer_for("google_genai"), ui.GeminiNormalizer)
    for provider in ("openai", "bedrock_mantle", "bedrock_converse", None, ""):
        assert isinstance(ui.normalizer_for(provider), ui.PassthroughNormalizer)


def test_active_normalizer_follows_the_sidebar_selection(monkeypatch):
    """message_text() must pick rules from the model actually in use, or a
    Gemini answer renders as a repr while Gemini is selected."""
    import models
    import ui
    import run_state
    run_state.start_run(model_choice="gemini")
    assert isinstance(ui.active_normalizer(), ui.GeminiNormalizer)
    assert ui.message_text([{"type": "text", "text": "ok"}]) == "ok"
    run_state.start_run(model_choice="glm")
    assert isinstance(ui.active_normalizer(), ui.PassthroughNormalizer)
    assert ui.message_text("ok") == "ok"


def test_every_registry_provider_has_a_normalizer_decision():
    """A new provider must be a deliberate choice: either it needs its own
    normaliser or it is knowingly fine with the default."""
    import models
    import ui
    for key, cfg in models._MODEL_REGISTRY.items():
        n = ui.normalizer_for(cfg["provider"])
        assert isinstance(n, ui.ContentNormalizer), key


def test_ui_reads_message_content_through_the_flattener():
    """Every display path must go through message_text(); a bare `.content`
    would regress Gemini rendering and silently kill live streaming."""
    import inspect
    import app as app_mod
    src = inspect.getsource(app_mod)
    assert "final_answer = message_text(" in src
    assert "chunk_content = message_text(" in src
    assert "str(content)[:2000]" not in src, "dev trace must flatten too"


def test_free_models_are_labelled_free_in_the_dropdown():
    """The FREE prefix is derived from the registry flag, not typed into each
    label, so a new free entry can't be added without it showing up."""
    import models
    assert models.model_label("gemini").startswith(models.FREE_PREFIX)
    # Billed models must not claim to be free: Bedrock inference bills to the
    # AWS account, OpenAI bills per token.
    for key in ("glm", "deepseek", "grok", "gpt4o-mini"):
        assert not models.model_label(key).startswith(models.FREE_PREFIX), key
    # Every flagged entry gets the prefix, whatever gets added later.
    for key, cfg in models._MODEL_REGISTRY.items():
        if cfg.get("free"):
            assert models.model_label(key).startswith(models.FREE_PREFIX), key


def test_dropdown_uses_the_derived_label():
    """app.py must render options through model_label(), or the FREE prefix
    silently stops appearing."""
    import inspect
    import app as app_mod
    assert "format_func=model_label" in inspect.getsource(app_mod)


def test_app_fails_fast_on_a_missing_vendor_key():
    """app.py must consult missing_key_reason() before building the agent."""
    import inspect
    import app as app_mod
    src = inspect.getsource(app_mod)
    assert "missing_key_reason(model_choice)" in src
    assert src.index("missing_key_reason(model_choice)") < src.index("build_agent(")
