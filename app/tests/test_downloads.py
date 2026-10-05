"""Downloads and result size: a result too large to download gets none, a
large one gets CSV only, and the agent is told which, so it never promises
a download that isn't there.

From 2026-10-05: an 8 GB result was pulled into the app's memory to build
its downloads, and the answer never appeared."""
import asyncio
import inspect

import pandas as pd
import pytest

import aws
import results
import run_state
import tools

MB = 1024 ** 2


@pytest.mark.parametrize("size, formats", [
    (None, ("csv", "excel")),            # size unknown: built from the preview
    (2 * MB, ("csv", "excel")),
    (10 * MB, ("csv", "excel")),
    (10 * MB + 1, ("csv",)),
    (100 * MB, ("csv",)),
    (100 * MB + 1, ()),
    (8 * 1024 * MB, ()),
])
def test_what_a_result_of_each_size_gets(size, formats, monkeypatch):
    monkeypatch.delenv("DOWNLOAD_MAX_MB", raising=False)
    assert results.download_formats(size) == formats


def test_the_download_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("DOWNLOAD_MAX_MB", "500")
    assert results.download_formats(400 * MB) == ("csv",)


def test_a_huge_result_is_never_read_into_memory(monkeypatch):
    read = []

    class S3:
        def head_object(self, Bucket, Key):
            return {"ContentLength": 8 * 1024 * MB}

        def get_object(self, Bucket, Key):
            read.append(Key)
            return {"Body": None}

    monkeypatch.setattr(aws, "_result_location", lambda qid: ("b", f"{qid}.csv"))
    monkeypatch.setattr(aws, "_session", lambda: type("S", (), {"client": lambda self, n: S3()})())
    assert aws._fetch_s3_csv("q1") == b"" and read == []


def _summary(monkeypatch, size):
    df = pd.DataFrame({"isrc": [f"I{i}" for i in range(100)], "rights": ["AT|DE"] * 100})
    df.attrs.update(query_id="q1", truncated=True)
    monkeypatch.setattr(tools, "tracked_query", lambda *a, **k: df)
    monkeypatch.setattr(tools, "result_size_bytes", lambda qid: size)
    run_state.start_run().dictionary_loaded = True
    return tools.sql_db_query.func("SELECT isrc, rights FROM t")


def test_the_agent_is_told_a_huge_result_has_no_downloads(monkeypatch):
    msg = _summary(monkeypatch, 8 * 1024 * MB)
    assert "too large to download, so there are NO download buttons" in msg
    assert "(8.0 GB)" in msg
    assert "suggest narrowing the question" in msg


def test_the_agent_is_told_a_large_result_is_csv_only(monkeypatch):
    msg = _summary(monkeypatch, 50 * MB)
    assert "a CSV download button" in msg and "NO Excel download" in msg


def test_the_agent_is_told_a_small_result_gets_both(monkeypatch):
    msg = _summary(monkeypatch, 1 * MB)
    assert "CSV and Excel download buttons" in msg


@pytest.fixture
def cl_app(monkeypatch):
    monkeypatch.delenv("OAUTH_GOOGLE_CLIENT_ID", raising=False)
    import importlib
    import chainlit_app
    return importlib.reload(chainlit_app)


def _parts(cl_app, monkeypatch, size):
    fetched = []

    def fetch(qid):
        fetched.append(qid)
        return b"isrc\n" + b"x\n" * 150
    monkeypatch.setattr(cl_app, "result_size_bytes", lambda qid: size)
    monkeypatch.setattr(cl_app, "_fetch_s3_csv", fetch)
    import chainlit.element
    from types import SimpleNamespace
    monkeypatch.setattr(chainlit.element, "context",       # elements read the thread id
                        SimpleNamespace(session=SimpleNamespace(thread_id="t1")))
    df = pd.DataFrame({"isrc": ["x"] * 100, "n": range(100)})
    elements, captions = asyncio.run(cl_app._result_parts(df, "q1", None, dev=False))
    return [type(e).__name__ for e in elements], captions, fetched


def test_chainlit_offers_nothing_to_download_for_a_huge_result(cl_app, monkeypatch):
    kinds, captions, fetched = _parts(cl_app, monkeypatch, 8 * 1024 * MB)
    assert kinds == ["Dataframe"] and fetched == []
    assert "too large to download here" in captions[0]


def test_chainlit_offers_csv_only_for_a_large_result(cl_app, monkeypatch):
    kinds, captions, _ = _parts(cl_app, monkeypatch, 50 * MB)
    assert kinds == ["Dataframe", "File"]
    assert "too large for Excel" in captions[0]


def test_chainlit_offers_both_for_a_small_result(cl_app, monkeypatch):
    kinds, _, _ = _parts(cl_app, monkeypatch, 1 * MB)
    assert kinds == ["Dataframe", "File", "File"]


def test_a_failure_building_downloads_never_loses_the_answer(cl_app):
    """The answer streamed, then building its downloads stalled, and it was
    never finished or saved."""
    src = inspect.getsource(cl_app._answer)
    guarded = src.split("try:\n        elements, captions = await _result_parts(")[1]
    assert guarded.index("except Exception:") < guarded.index("await answer.send()")
    resume = inspect.getsource(cl_app.on_chat_resume)
    assert "except Exception:" in resume.split("_result_parts(")[1]


def test_streamlit_follows_the_same_limits():
    import ui
    src = inspect.getsource(ui._render_answer)
    assert "download_formats(size)" in src
    assert '_fetch_s3_csv(query_id) if download_formats(size) else b""' in src
    assert 'if "excel" not in formats' in src
    assert "_xl_key not in st.session_state" in src      # built once, not every rerun
