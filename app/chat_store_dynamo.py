"""Chat history storage on DynamoDB: one table, Chainlit's key layout plus
our own items. The SQL equivalent is chat_store.py; storage.py picks one.

Layout (PK / SK), all in one table:
    USER#<email>     USER                       Chainlit: the user
    USER#<email>     FAV#<thread id>            ours: a favourite chat
    USER#<email>     USAGE#<time>#<query id>    ours: one Athena query
    USER#<email>     TIER                       ours: capped / uncapped
    THREAD#<id>      THREAD                     Chainlit: the chat record (+ our favouritedAt)
    THREAD#<id>      STEP#<id>                  Chainlit: each message and step
One index, UserThread (UserThreadPK / UserThreadSK), which Chainlit needs
for the sidebar. Usage sits under the user, so deleting a chat never
deletes its usage.

Retention is DynamoDB TTL on `expiresAt` (epoch seconds), not a sweep:
every item of a chat expires CHAT_RETENTION_DAYS after the chat was last
asked or opened (touch_chat), and a favourite's items carry no expiry. When
a chat's expiry is rewritten, the chat record goes last, so a refresh that
stops partway leaves the record expiring first: the chat then disappears
whole rather than opening with messages missing.

boto3 is synchronous; the store's methods run it in a worker thread."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from botocore.exceptions import ClientError

import chat_store
from chat_store import CAPPED, TIERS, FavouriteLimitReached

DEFAULT_TABLE = "datalake-agent-chat"
USER_THREAD_INDEX = "UserThread"       # Chainlit's name; its layer queries it
TTL_ATTRIBUTE = "expiresAt"


def table_name() -> str:
    return os.getenv("DYNAMODB_TABLE", "").strip() or DEFAULT_TABLE


def endpoint_url() -> Optional[str]:
    """DynamoDB Local in development (DYNAMODB_ENDPOINT_URL); None = AWS."""
    return os.getenv("DYNAMODB_ENDPOINT_URL", "").strip() or None


def usage_retention_days() -> int:
    """How long Athena usage rows are kept (USAGE_RETENTION_DAYS, default 0 =
    forever). They are small: about 70 MB a year even at heavy use."""
    try:
        return max(0, int(os.getenv("USAGE_RETENTION_DAYS", "0")))
    except ValueError:
        return 0


def make_client():
    import aws                       # the app's session: profile or instance role
    return aws._session().client("dynamodb", endpoint_url=endpoint_url())


def table_definition(name: str) -> Dict[str, Any]:
    """The table as create_table wants it. On AWS, whoever owns the account
    creates it from this (see DEPLOY.md); locally the app does."""
    return {
        "TableName": name,
        "BillingMode": "PAY_PER_REQUEST",
        "AttributeDefinitions": [
            {"AttributeName": a, "AttributeType": "S"}
            for a in ("PK", "SK", "UserThreadPK", "UserThreadSK")],
        "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"},
                      {"AttributeName": "SK", "KeyType": "RANGE"}],
        "GlobalSecondaryIndexes": [{
            "IndexName": USER_THREAD_INDEX,
            "KeySchema": [{"AttributeName": "UserThreadPK", "KeyType": "HASH"},
                          {"AttributeName": "UserThreadSK", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
    }


def user_pk(user: str) -> str:
    return f"USER#{user}"


def thread_pk(thread_id: str) -> str:
    return f"THREAD#{thread_id}"


def chat_expiry(now: Optional[float] = None) -> Optional[int]:
    """Epoch seconds a chat used now expires at, or None when retention is off."""
    days = chat_store.retention_days()
    if not days:
        return None
    return int((now if now is not None else time.time()) + days * 86400)


def is_expired(item: Dict[str, Any], now: Optional[float] = None) -> bool:
    """TTL deletes within about 48 hours of expiry and returns the item until
    then; treat it as gone from the moment it expires."""
    expires = item.get(TTL_ATTRIBUTE)
    return expires is not None and float(expires) <= (now if now is not None else time.time())


class DynamoStore:
    """The chat store on DynamoDB. Same methods as storage.SqlStore."""

    name = "dynamodb"
    sweeps = False                  # TTL deletes expired chats; no sweep job

    def __init__(self, table: Optional[str] = None, client: Any = None):
        # Imported here, while the app loads: Chainlit only has this folder on
        # the import path then, not later when it asks for the data layer.
        from chainlit_data_dynamo import ChatDynamoDBDataLayer
        self.table = table or table_name()
        self.client = client or make_client()
        self._layer_class = ChatDynamoDBDataLayer

    def data_layer(self):
        return self._layer_class(self)

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    # ── schema ─────────────────────────────────────────────────────────────

    async def ensure_schema(self) -> None:
        await self._run(self._ensure_table)

    def _ensure_table(self) -> None:
        """Create the table against DynamoDB Local; on AWS only check it
        exists, so the server needs no CreateTable permission."""
        try:
            self.client.describe_table(TableName=self.table)
            return
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        if not endpoint_url() and os.getenv("DYNAMODB_CREATE_TABLE", "").lower() != "true":
            raise RuntimeError(
                f"DynamoDB table {self.table!r} does not exist. Create it as in "
                "DEPLOY.md (or set DYNAMODB_CREATE_TABLE=true to let the app do it).")
        self.client.create_table(**table_definition(self.table))
        self.client.get_waiter("table_exists").wait(TableName=self.table)
        try:
            self.client.update_time_to_live(TableName=self.table, TimeToLiveSpecification={
                "Enabled": True, "AttributeName": TTL_ATTRIBUTE})
        except ClientError:
            pass                    # DynamoDB Local may not support TTL settings

    # ── low-level helpers ─────────────────────────────────────────────────

    def _query_all(self, **kw) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        while True:
            response = self.client.query(TableName=self.table, **kw)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                return items
            kw["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def _chat_items(self, thread_id: str) -> List[Dict[str, Any]]:
        """Keys of every item of a chat (plus favouritedAt and userId on the
        record)."""
        return self._query_all(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": thread_pk(thread_id)}},
            ProjectionExpression="PK, SK, favouritedAt, userId")

    def _record(self, thread_id: str) -> Optional[Dict[str, Any]]:
        response = self.client.get_item(
            TableName=self.table,
            Key={"PK": {"S": thread_pk(thread_id)}, "SK": {"S": "THREAD"}},
            ProjectionExpression="favouritedAt, userId, expiresAt")
        return response.get("Item")

    def is_favourite(self, thread_id: str) -> bool:
        record = self._record(thread_id)
        return bool(record and "favouritedAt" in record)

    def _set_expiry(self, key: Dict[str, Any], expires: Optional[int]) -> None:
        """Set or remove one item's expiry. Only on items that exist: an
        update would otherwise create a stub item from the key alone."""
        if expires is None:
            expression, values = "REMOVE expiresAt", {}
        else:
            expression, values = "SET expiresAt = :e", {":e": {"N": str(expires)}}
        try:
            self.client.update_item(
                TableName=self.table, Key={"PK": key["PK"], "SK": key["SK"]},
                UpdateExpression=expression, ConditionExpression="attribute_exists(PK)",
                **({"ExpressionAttributeValues": values} if values else {}))
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def _apply_expiry(self, thread_id: str, expires: Optional[int], record_first: bool) -> None:
        items = self._chat_items(thread_id)
        record = [i for i in items if i["SK"]["S"] == "THREAD"]
        others = [i for i in items if i["SK"]["S"] != "THREAD"]
        for item in (record + others) if record_first else (others + record):
            self._set_expiry(item, expires)

    # ── retention ─────────────────────────────────────────────────────────

    async def touch_chat(self, thread_id: str) -> None:
        """The chat was asked or opened: push every item's expiry out by the
        retention period (or clear it on a favourite), the record last."""
        await self._run(self._touch_chat, thread_id)

    def _touch_chat(self, thread_id: str) -> None:
        record = self._record(thread_id)
        if not record:
            return
        if "favouritedAt" in record:
            self._apply_expiry(thread_id, None, record_first=True)
        else:
            self._apply_expiry(thread_id, chat_expiry(), record_first=False)

    async def purge_expired(self) -> int:
        return 0                    # DynamoDB TTL deletes expired chats

    async def delete_chat(self, thread_id: str) -> None:
        await self._run(self._delete_chat, thread_id)

    def _delete_chat(self, thread_id: str) -> None:
        """Every item of the chat, and the owner's favourite entry for it."""
        items = self._chat_items(thread_id)
        owners = {i["userId"]["S"] for i in items if "userId" in i}
        keys = [{"PK": i["PK"], "SK": i["SK"]} for i in items]
        keys += [{"PK": {"S": user_pk(o)}, "SK": {"S": f"FAV#{thread_id}"}} for o in owners]
        self._batch_write([{"DeleteRequest": {"Key": k}} for k in keys])

    def _batch_write(self, requests: List[Dict[str, Any]]) -> None:
        for start in range(0, len(requests), 25):
            pending = {self.table: requests[start:start + 25]}
            for attempt in range(8):
                response = self.client.batch_write_item(RequestItems=pending)
                pending = response.get("UnprocessedItems") or {}
                if not pending:
                    break
                time.sleep(min(2 ** attempt * 0.1, 5))
            if pending:
                raise RuntimeError(f"DynamoDB batch write left {len(pending[self.table])} items")

    # ── favourites ────────────────────────────────────────────────────────

    async def set_favourite(self, thread_id: str, user: str, favourite: bool) -> None:
        """Raises chat_store.FavouriteLimitReached past chat_store.favourites_max().
        Re-favouriting a chat that already is one is fine."""
        await self._run(self._set_favourite, thread_id, user, favourite)

    def _set_favourite(self, thread_id: str, user: str, favourite: bool) -> None:
        fav_key = {"PK": {"S": user_pk(user)}, "SK": {"S": f"FAV#{thread_id}"}}
        record_key = {"PK": {"S": thread_pk(thread_id)}, "SK": {"S": "THREAD"}}
        if favourite:
            limit = chat_store.favourites_max()
            if limit:
                others = self._favourite_items(user)
                if len([f for f in others if f["SK"]["S"] != f"FAV#{thread_id}"]) >= limit:
                    raise FavouriteLimitReached(limit)
            at = chat_store._iso(datetime.now(timezone.utc))
            self.client.put_item(TableName=self.table, Item={
                **fav_key, "favouritedAt": {"S": at}})
            self._update_if_exists(record_key, "SET favouritedAt = :f", {":f": {"S": at}})
            # Record first: if this stops partway the chat is already safe,
            # and its next use clears the rest (touch_chat).
            self._apply_expiry(thread_id, None, record_first=True)
        else:
            self.client.delete_item(TableName=self.table, Key=fav_key)
            self._update_if_exists(record_key, "REMOVE favouritedAt", {})
            self._apply_expiry(thread_id, chat_expiry(), record_first=False)

    def _update_if_exists(self, key, expression: str, values: Dict[str, Any]) -> None:
        try:
            self.client.update_item(
                TableName=self.table, Key=key, UpdateExpression=expression,
                ConditionExpression="attribute_exists(PK)",
                **({"ExpressionAttributeValues": values} if values else {}))
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def _favourite_items(self, user: str) -> List[Dict[str, Any]]:
        return self._query_all(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :fav)",
            ExpressionAttributeValues={":pk": {"S": user_pk(user)}, ":fav": {"S": "FAV#"}})

    async def favourite_thread_ids(self, user: str) -> Set[str]:
        items = await self._run(self._favourite_items, user)
        return {i["SK"]["S"][len("FAV#"):] for i in items}

    async def favourite_chats(self, user: str) -> List[Dict[str, Any]]:
        """The user's favourite chats with their names, newest favourite first."""
        return await self._run(self._favourite_chats, user)

    def _favourite_chats(self, user: str) -> List[Dict[str, Any]]:
        favs = sorted(self._favourite_items(user),
                      key=lambda i: i.get("favouritedAt", {}).get("S", ""), reverse=True)
        ids = [f["SK"]["S"][len("FAV#"):] for f in favs]
        names: Dict[str, str] = {}
        for start in range(0, len(ids), 100):
            keys = [{"PK": {"S": thread_pk(t)}, "SK": {"S": "THREAD"}} for t in ids[start:start + 100]]
            request = {self.table: {"Keys": keys, "ProjectionExpression": "PK, #n",
                                    "ExpressionAttributeNames": {"#n": "name"}}}
            while request:
                response = self.client.batch_get_item(RequestItems=request)
                for item in response.get("Responses", {}).get(self.table, []):
                    names[item["PK"]["S"][len("THREAD#"):]] = item.get("name", {}).get("S")
                request = response.get("UnprocessedKeys") or {}
        return [{"id": t, "name": names[t]} for t in ids if t in names]

    # ── Athena usage and tiers ────────────────────────────────────────────

    async def record_usage(self, user: str, thread_id: Optional[str],
                           scans: List[Dict[str, Any]], now: Optional[datetime] = None) -> None:
        """Store a question's Athena scans (run_state.RunContext.scans)."""
        if not scans or not user:
            return
        await self._run(self._record_usage, user, thread_id, scans, now)

    def _record_usage(self, user, thread_id, scans, now) -> None:
        when = now or datetime.now(timezone.utc)
        at = chat_store._iso(when)
        keep = usage_retention_days()
        requests = []
        for sc in scans:
            item: Dict[str, Any] = {
                "PK": {"S": user_pk(user)},
                "SK": {"S": f"USAGE#{at}#{sc.get('query_id') or uuid.uuid4()}"},
                "bytesScanned": {"N": str(int(sc.get("bytes_scanned") or 0))},
                "createdAt": {"S": at},
            }
            for attr, value in (("threadId", thread_id), ("queryId", sc.get("query_id")),
                                ("workgroup", sc.get("workgroup")), ("tool", sc.get("tool")),
                                ("status", sc.get("status"))):
                if value:
                    item[attr] = {"S": str(value)}
            if keep:
                item[TTL_ATTRIBUTE] = {"N": str(int(when.timestamp() + keep * 86400))}
            requests.append({"PutRequest": {"Item": item}})
        self._batch_write(requests)

    async def month_usage(self, user: str, now: Optional[datetime] = None) -> Dict[str, Any]:
        """This calendar month's (UTC) bytes scanned and query count for a user."""
        return await self._run(self._month_usage, user, now)

    def _month_usage(self, user, now) -> Dict[str, Any]:
        since = chat_store.month_start(now)
        until = chat_store.month_start(since + timedelta(days=32))
        # "USAGE#<until>" sorts before any "USAGE#<until>#<query id>", so the
        # first instant of next month is excluded, as in SQL.
        items = self._query_all(
            KeyConditionExpression="PK = :pk AND SK BETWEEN :a AND :b",
            ExpressionAttributeValues={
                ":pk": {"S": user_pk(user)},
                ":a": {"S": f"USAGE#{chat_store._iso(since)}"},
                ":b": {"S": f"USAGE#{chat_store._iso(until)}"}},
            ProjectionExpression="bytesScanned")
        total = sum(int(Decimal(i["bytesScanned"]["N"])) for i in items)
        return {"bytes": total, "queries": len(items), "since": chat_store._iso(since)}

    async def user_tier(self, user: str) -> str:
        """'capped' unless the user has been marked 'uncapped'."""
        response = await self._run(lambda: self.client.get_item(
            TableName=self.table, Key={"PK": {"S": user_pk(user)}, "SK": {"S": "TIER"}}))
        tier = response.get("Item", {}).get("tier", {}).get("S")
        return tier if tier in TIERS else CAPPED

    async def set_user_tier(self, user: str, tier: str) -> None:
        if tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS}")
        await self._run(lambda: self.client.put_item(TableName=self.table, Item={
            "PK": {"S": user_pk(user)}, "SK": {"S": "TIER"}, "tier": {"S": tier},
            "updatedAt": {"S": chat_store._iso(datetime.now(timezone.utc))}}))
