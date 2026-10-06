"""The DynamoDB chat store (chat_store_dynamo.py) and data layer
(chainlit_data_dynamo.py), and the adapter that picks a store (storage.py).

Runs against moto's in-process DynamoDB by default. Set TEST_DYNAMODB_ENDPOINT
(e.g. http://localhost:8002, DynamoDB Local from docker-compose) to run them
against DynamoDB Local instead. Each test gets its own throwaway table."""
import asyncio
import inspect
import json
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
import pandas as pd
import pytest
from chainlit.context import init_http_context
from chainlit.types import Pagination, ThreadFilter
from chainlit.user import User

import chat_store
import chat_store_dynamo as dyn
import chainlit_data_dynamo as layer_mod
import storage

DAY = 86400
USER = "a@tunedglobal.com"


@pytest.fixture
def store(monkeypatch):
    """A DynamoStore on a fresh table, never the app's own."""
    name = f"test-chat-{uuid.uuid4().hex[:10]}"
    assert name != dyn.table_name()
    monkeypatch.delenv("CHAT_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("FAVOURITES_MAX", raising=False)
    endpoint = os.getenv("TEST_DYNAMODB_ENDPOINT")
    if endpoint:
        monkeypatch.setenv("DYNAMODB_ENDPOINT_URL", endpoint)
        client = boto3.client("dynamodb", endpoint_url=endpoint, region_name="us-west-2",
                              aws_access_key_id="local", aws_secret_access_key="local")
        s = dyn.DynamoStore(name, client)
        asyncio.run(s.ensure_schema())
        yield s
        client.delete_table(TableName=name)
    else:
        from moto import mock_aws
        for var, value in (("AWS_ACCESS_KEY_ID", "test"), ("AWS_SECRET_ACCESS_KEY", "test"),
                           ("AWS_DEFAULT_REGION", "us-west-2")):
            monkeypatch.setenv(var, value)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.delenv("DYNAMODB_ENDPOINT_URL", raising=False)
        monkeypatch.setenv("DYNAMODB_CREATE_TABLE", "true")
        with mock_aws():
            s = dyn.DynamoStore(name, boto3.client("dynamodb", region_name="us-west-2"))
            asyncio.run(s.ensure_schema())
            yield s


def _item(store, pk, sk):
    return store.client.get_item(TableName=store.table,
                                 Key={"PK": {"S": pk}, "SK": {"S": sk}}).get("Item")


def _chat_items(store, thread_id):
    return store.client.query(
        TableName=store.table, KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": f"THREAD#{thread_id}"}})["Items"]


def _expiries(store, thread_id):
    return {i["SK"]["S"]: (int(i["expiresAt"]["N"]) if "expiresAt" in i else None)
            for i in _chat_items(store, thread_id)}


def _steps(thread_id):
    """A question, a tool step and an answer with a result snapshot, as
    Chainlit writes them (same shape as tests/test_chat_store.py)."""
    q, t, a = (str(uuid.uuid4()) for _ in range(3))
    df = pd.DataFrame({"letter": list("MSA"), "count": [63, 55, 43]})
    meta = chat_store.answer_metadata("how many?", "M: 63", df, "qid-1",
                                      {"type": "bar", "x": "letter", "y": "count"}, ["a notice"])
    base = {"threadId": thread_id, "streaming": False, "isError": False,
            "metadata": {}, "tags": None, "language": None}
    return [
        {**base, "id": q, "name": "user", "type": "user_message", "output": "how many?",
         "createdAt": "2026-09-26T00:00:01Z", "modes": {"model": "gpt4o-mini"}},
        {**base, "id": t, "parentId": q, "name": "sql_db_query", "type": "tool",
         "input": "{}", "output": "ok", "createdAt": "2026-09-26T00:00:02Z",
         "autoCollapse": False, "showInput": "json"},
        {**base, "id": a, "parentId": q, "name": "Assistant", "type": "assistant_message",
         "output": "M: 63", "metadata": meta, "createdAt": "2026-09-26T00:00:04Z"},
    ]


def _conversation(store, thread_id=None, user=USER):
    """A whole chat written through the data layer. Returns (layer, thread id)."""
    thread_id = thread_id or str(uuid.uuid4())
    layer = store.data_layer()

    async def go():
        init_http_context()          # the data layer's write decorator needs one
        u = await layer.create_user(User(identifier=user, metadata={}))
        await layer.update_thread(thread_id, name="Top letters", user_id=u.id,
                                  metadata={"chat_profile": "General",
                                            "history": [{"question": "q", "answer": "a"}]})
        for step in _steps(thread_id):
            await layer.create_step(step)
    asyncio.run(go())
    return layer, thread_id


# ── the adapter ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (None, "sql"), ("sql", "sql"), ("dynamodb", "dynamodb"), ("DynamoDB", "dynamodb"),
    ("off", None)])
def test_the_adapter_picks_the_store(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("CHAT_STORE", raising=False)
    else:
        monkeypatch.setenv("CHAT_STORE", value)
    monkeypatch.delenv("CHAT_DB_URL", raising=False)
    monkeypatch.setattr(dyn, "make_client", lambda: object())
    store = storage.store_from_env()
    assert (store.name if store else None) == expected


def test_sql_stays_switchable_off_by_its_own_url(monkeypatch):
    monkeypatch.setenv("CHAT_STORE", "sql")
    monkeypatch.setenv("CHAT_DB_URL", "off")
    assert storage.store_from_env() is None


def test_an_unknown_store_is_refused(monkeypatch):
    monkeypatch.setenv("CHAT_STORE", "mongo")
    with pytest.raises(ValueError, match="CHAT_STORE"):
        storage.store_from_env()


def test_both_stores_offer_the_same_methods():
    wanted = {n for n in dir(storage.ChatStore) if not n.startswith("_")}
    for cls in (storage.SqlStore, dyn.DynamoStore):
        assert wanted <= set(dir(cls)), cls.__name__


def test_the_app_only_talks_to_the_adapter():
    import chainlit_app
    src = inspect.getsource(chainlit_app)
    assert "STORE = storage.store_from_env()" in src
    for direct in ("chat_store.set_favourite(", "chat_store.record_usage(",
                   "chat_store.month_usage(", "build_data_layer(", "DynamoStore("):
        assert direct not in src, direct


# ── the table ───────────────────────────────────────────────────────────────

def test_on_aws_a_missing_table_is_reported_not_created(store, monkeypatch):
    monkeypatch.delenv("DYNAMODB_CREATE_TABLE", raising=False)
    monkeypatch.delenv("DYNAMODB_ENDPOINT_URL", raising=False)
    missing = dyn.DynamoStore("test-chat-missing", store.client)
    with pytest.raises(RuntimeError, match="does not exist"):
        asyncio.run(missing.ensure_schema())


def test_ensure_schema_is_idempotent(store):
    asyncio.run(store.ensure_schema())
    index = store.client.describe_table(TableName=store.table)["Table"]["GlobalSecondaryIndexes"]
    assert [i["IndexName"] for i in index] == ["UserThread"]


# ── a whole conversation through the data layer ─────────────────────────────

def test_a_conversation_saves_and_reads_back_whole(store):
    layer, thread_id = _conversation(store)
    thread = asyncio.run(layer.get_thread(thread_id))
    assert [s["type"] for s in thread["steps"]] == ["user_message", "tool", "assistant_message"]
    assert thread["name"] == "Top letters" and thread["userIdentifier"] == USER

    record = chat_store.read_answer_metadata(thread["steps"][-1]["metadata"])
    assert record["question"] == "how many?" and record["notices"] == ["a notice"]
    df = chat_store.preview_dataframe(record["result"])
    # Packed as text, so numbers stay integers (the parent reads every number back as a float)
    assert df.to_dict("list") == {"letter": ["M", "S", "A"], "count": [63, 55, 43]}


def test_the_snapshot_is_stored_packed(store):
    _, thread_id = _conversation(store)
    answer = [i for i in _chat_items(store, thread_id)
              if i.get("type", {}).get("S") == "assistant_message"][0]
    assert answer["metadata"]["M"]["datalake"]["S"].startswith("z:")


def test_the_saved_session_drops_the_growing_history(store):
    layer, thread_id = _conversation(store)
    metadata = asyncio.run(layer.get_thread(thread_id))["metadata"]
    assert metadata == {"chat_profile": "General"}


def test_a_rename_keeps_the_session_and_the_creation_date(store):
    """The parent overwrites createdAt on every update and wipes the session
    on a rename."""
    layer, thread_id = _conversation(store)
    before = asyncio.run(layer.get_thread(thread_id))
    time.sleep(0.01)
    asyncio.run(layer.update_thread(thread_id, name="Renamed"))
    after = asyncio.run(layer.get_thread(thread_id))
    assert after["name"] == "Renamed"
    assert after["metadata"] == {"chat_profile": "General"}
    assert after["createdAt"] == before["createdAt"]


def test_the_sidebar_lists_the_users_chats(store):
    layer, t1 = _conversation(store)
    _, other = _conversation(store, user="b@tunedglobal.com")
    page = asyncio.run(layer.list_threads(Pagination(first=20), ThreadFilter(userId=USER)))
    assert [t["id"] for t in page.data] == [t1]


def test_deleting_a_chat_deletes_all_of_it_and_its_favourite(store):
    layer, thread_id = _conversation(store)
    asyncio.run(store.set_favourite(thread_id, USER, True))
    asyncio.run(layer.delete_thread(thread_id))
    assert _chat_items(store, thread_id) == []
    assert _item(store, f"USER#{USER}", f"FAV#{thread_id}") is None


def test_attachments_are_not_stored(store):
    """As in the SQL layer: tables and charts are rebuilt from the snapshot."""
    assert asyncio.run(store.data_layer().create_element(object())) is None


# ── packing large answers ───────────────────────────────────────────────────

def test_a_huge_preview_is_trimmed_to_fit_the_item_limit(monkeypatch):
    monkeypatch.setattr(layer_mod, "SNAPSHOT_MAX_BYTES", 20_000)
    noise = [[os.urandom(48).hex() for _ in range(6)] for _ in range(100)]   # incompressible
    record = {"question": "q", "answer": "a", "notices": [],
              "result": {"preview": {"columns": list("abcdef"), "data": noise}}}
    packed = layer_mod.pack_snapshot(record)
    back = layer_mod.unpack_snapshot(packed)
    assert len(packed) <= 20_000
    assert 0 < len(back["result"]["preview"]["data"]) < 100
    assert back["result"]["preview_trimmed"] is True
    assert len(record["result"]["preview"]["data"]) == 100      # the caller's copy untouched


def test_a_normal_preview_is_kept_whole():
    df = pd.DataFrame({"i": range(100), "label": ["x"] * 100})
    record = chat_store.answer_metadata("q", "a", df, "qid", None, [])["datalake"]
    back = layer_mod.unpack_snapshot(layer_mod.pack_snapshot(record))
    assert back == record


# ── retention by TTL ────────────────────────────────────────────────────────

def test_every_item_of_a_new_chat_expires_after_the_retention_period(store):
    _, thread_id = _conversation(store)
    expiries = _expiries(store, thread_id)
    now = time.time()
    assert len(expiries) == 4                     # the record and three steps
    assert all(now + 59 * DAY < e <= now + 60 * DAY + 5 for e in expiries.values())


def test_using_a_chat_pushes_every_items_expiry_out(store):
    _, thread_id = _conversation(store)
    for item in _chat_items(store, thread_id):    # as if last used long ago
        store._set_expiry(item, int(time.time()) + DAY)
    asyncio.run(store.touch_chat(thread_id))
    assert all(e > time.time() + 59 * DAY for e in _expiries(store, thread_id).values())


def test_the_chat_record_is_refreshed_last(store):
    """A refresh that stops partway must leave the record expiring first, so
    the chat disappears whole rather than opening with messages missing."""
    _, thread_id = _conversation(store)
    order = []
    real = store._set_expiry
    store._set_expiry = lambda item, e: (order.append(item["SK"]["S"]), real(item, e))
    asyncio.run(store.touch_chat(thread_id))
    assert order[-1] == "THREAD" and len(order) == 4


def test_a_favourite_never_expires_and_unfavouriting_restores_expiry(store):
    layer, thread_id = _conversation(store)
    asyncio.run(store.set_favourite(thread_id, USER, True))
    assert set(_expiries(store, thread_id).values()) == {None}

    async def more():                             # a new message in a favourite
        init_http_context()
        await layer.create_step({**_steps(thread_id)[0], "id": str(uuid.uuid4())})
    asyncio.run(more())
    asyncio.run(store.touch_chat(thread_id))
    assert set(_expiries(store, thread_id).values()) == {None}

    asyncio.run(store.set_favourite(thread_id, USER, False))
    assert all(e and e > time.time() + 59 * DAY for e in _expiries(store, thread_id).values())


def test_favouriting_clears_the_record_first(store):
    """Favouriting removes expiry; record first, so a stop partway can't
    expire the chat the user asked to keep."""
    _, thread_id = _conversation(store)
    order = []
    real = store._set_expiry
    store._set_expiry = lambda item, e: (order.append(item["SK"]["S"]), real(item, e))
    asyncio.run(store.set_favourite(thread_id, USER, True))
    assert order[0] == "THREAD"


def test_an_expired_chat_is_gone_before_ttl_deletes_it(store):
    layer, thread_id = _conversation(store)
    _, live = _conversation(store)
    store._set_expiry(_item(store, f"THREAD#{thread_id}", "THREAD"), int(time.time()) - 10)
    assert asyncio.run(layer.get_thread(thread_id)) is None
    page = asyncio.run(layer.list_threads(Pagination(first=20), ThreadFilter(userId=USER)))
    assert [t["id"] for t in page.data] == [live]


def test_renaming_never_extends_a_chats_expiry(store):
    """Only touch_chat extends it, for every item at once; extending the
    record alone would let messages expire under a chat that still shows."""
    layer, thread_id = _conversation(store)
    record = _item(store, f"THREAD#{thread_id}", "THREAD")
    store._set_expiry(record, int(time.time()) + DAY)
    asyncio.run(layer.update_thread(thread_id, name="Renamed"))
    assert _expiries(store, thread_id)["THREAD"] < time.time() + 2 * DAY


def test_retention_off_means_nothing_expires(store, monkeypatch):
    monkeypatch.setenv("CHAT_RETENTION_DAYS", "0")
    _, thread_id = _conversation(store)
    assert set(_expiries(store, thread_id).values()) == {None}


def test_no_sweep_job_on_dynamodb(store):
    assert store.sweeps is False and asyncio.run(store.purge_expired()) == 0


# ── favourites ──────────────────────────────────────────────────────────────

def test_favourites_list_newest_first_with_names(store):
    _, t1 = _conversation(store)
    _, t2 = _conversation(store)
    asyncio.run(store.set_favourite(t1, USER, True))
    time.sleep(0.01)
    asyncio.run(store.set_favourite(t2, USER, True))
    chats = asyncio.run(store.favourite_chats(USER))
    assert [c["id"] for c in chats] == [t2, t1] and chats[0]["name"] == "Top letters"
    assert asyncio.run(store.favourite_thread_ids(USER)) == {t1, t2}


def test_favourites_are_capped_per_user(store, monkeypatch):
    monkeypatch.setenv("FAVOURITES_MAX", "1")
    _, t1 = _conversation(store)
    _, t2 = _conversation(store)
    asyncio.run(store.set_favourite(t1, USER, True))
    asyncio.run(store.set_favourite(t1, USER, True))          # again: still allowed
    with pytest.raises(chat_store.FavouriteLimitReached):
        asyncio.run(store.set_favourite(t2, USER, True))
    asyncio.run(store.set_favourite(t2, "b@tunedglobal.com", True))   # per user
    asyncio.run(store.set_favourite(t1, USER, False))
    asyncio.run(store.set_favourite(t2, USER, True))
    assert asyncio.run(store.favourite_thread_ids(USER)) == {t2}


# ── Athena usage and tiers ──────────────────────────────────────────────────

def _scan(n, status="succeeded", qid=None):
    return {"query_id": qid or f"q{uuid.uuid4().hex[:6]}", "bytes_scanned": n, "status": status,
            "tool": "sql_db_query", "workgroup": "datalake-agent"}


def test_usage_is_totalled_for_the_calendar_month(store):
    GB = 1024 ** 3
    sept = datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)
    octo = datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc)
    nov = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)

    async def go():
        await store.record_usage(USER, "t1", [_scan(100)], now=sept)
        await store.record_usage(USER, "t1", [_scan(5 * GB), _scan(7, "failed")], now=octo)
        await store.record_usage(USER, "t1", [_scan(1)], now=nov)      # next month's first instant
        await store.record_usage("b@tunedglobal.com", None, [_scan(999)], now=octo)
        return (await store.month_usage(USER, now=datetime(2026, 10, 5, tzinfo=timezone.utc)),
                await store.month_usage(USER, now=sept),
                await store.month_usage("c@tunedglobal.com"))

    oct_usage, sep_usage, nobody = asyncio.run(go())
    assert oct_usage == {"bytes": 5 * GB + 7, "queries": 2, "since": "2026-10-01T00:00:00Z"}
    assert sep_usage["bytes"] == 100 and sep_usage["queries"] == 1
    assert nobody["bytes"] == 0 and nobody["queries"] == 0


def test_usage_outlives_the_chat_it_came_from(store):
    layer, thread_id = _conversation(store)
    asyncio.run(store.record_usage(USER, thread_id, [_scan(42)]))
    asyncio.run(layer.delete_thread(thread_id))
    assert asyncio.run(store.month_usage(USER))["bytes"] == 42


def test_usage_is_kept_unless_a_retention_is_set(store, monkeypatch):
    asyncio.run(store.record_usage(USER, None, [_scan(1, qid="keep")]))
    monkeypatch.setenv("USAGE_RETENTION_DAYS", "400")
    asyncio.run(store.record_usage(USER, None, [_scan(1, qid="expire")]))
    items = store.client.query(
        TableName=store.table, KeyConditionExpression="PK = :pk AND begins_with(SK, :u)",
        ExpressionAttributeValues={":pk": {"S": f"USER#{USER}"}, ":u": {"S": "USAGE#"}})["Items"]
    by_query = {i["queryId"]["S"]: i for i in items}
    assert "expiresAt" not in by_query["keep"]
    assert int(by_query["expire"]["expiresAt"]["N"]) > time.time() + 399 * DAY


def test_users_are_capped_unless_marked_uncapped(store):
    async def go():
        before = await store.user_tier(USER)
        await store.set_user_tier(USER, chat_store.UNCAPPED)
        after = await store.user_tier(USER)
        with pytest.raises(ValueError):
            await store.set_user_tier(USER, "gold")
        return before, after
    assert asyncio.run(go()) == ("capped", "uncapped")


# ── what Chainlit's DynamoDB layer does, which the subclass relies on ───────

def test_chainlit_dynamodb_layer_still_needs_the_fixes():
    """If one of these fails, Chainlit changed the layer: re-check the
    matching override in chainlit_data_dynamo.py."""
    from chainlit.data.dynamodb import DynamoDBDataLayer as Parent
    update = inspect.getsource(Parent.update_thread)
    assert '"createdAt": ts' in update                 # overwrites the creation date
    assert "metadata = {}" in update                   # wipes the session on a rename
    assert '"IndexName": "UserThread"' in inspect.getsource(Parent.list_threads)
    assert "storage_provider" in inspect.getsource(Parent.create_element)
    assert "convert_decimals" in inspect.getsource(Parent._deserialize_item)  # numbers back as floats


def test_stores_import_their_data_layers_while_the_app_loads():
    """Chainlit has the app folder on the import path only while loading the
    app; an import made later, when it asks for the data layer, fails."""
    for cls in (storage.SqlStore, dyn.DynamoStore):
        assert "import" not in inspect.getsource(cls.data_layer), cls.__name__


@pytest.mark.parametrize("in_container,explicit,expected", [
    (True, None, "datalake-agent-chat"),
    (False, None, "test-datalake-agent-chat"),
    (False, "datalake-agent-chat", "datalake-agent-chat"),     # on purpose, by name
    (True, "other-table", "other-table"),
])
def test_only_the_server_defaults_to_the_production_table(monkeypatch, in_container, explicit, expected):
    """A local run with real AWS credentials and no DYNAMODB_ENDPOINT_URL
    must not write into production unless it names the table."""
    monkeypatch.setattr(dyn.access, "in_container", lambda: in_container)
    if explicit:
        monkeypatch.setenv("DYNAMODB_TABLE", explicit)
    else:
        monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
    assert dyn.table_name() == expected
