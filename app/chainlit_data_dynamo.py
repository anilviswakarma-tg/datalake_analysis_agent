"""Chainlit's DynamoDB data layer, adapted to this app. The SQL equivalent
is chainlit_data.py; storage.py picks one.

What this changes in Chainlit's DynamoDBDataLayer (2.12; re-check after an
upgrade, tests/test_chat_store_dynamo.py pins each):
- Answer snapshots are stored compressed. A 100-row preview can pass
  DynamoDB's 400 KB item limit uncompressed (3.9 MB seen; 156 KB packed);
  one that still doesn't fit keeps fewer preview rows. As a string it also
  dodges the parent reading every number back as a float.
- New items carry the chat's expiry (chat_store_dynamo: TTL retention),
  except in a favourite; an expired chat counts as gone before TTL gets to
  deleting it, in the sidebar and when opened.
- update_thread keeps the chat's creation date (the parent overwrites it on
  every update), only replaces the saved session when given one (the
  parent wipes it on a rename), never extends the chat's expiry (only
  touch_chat does, for every item at once), and leaves out the running
  question-and-answer history, which grows with every question and is
  rebuilt from the messages on resume anyway.
- Deleting a chat deletes every item and the owner's favourite entry.
- Attachments aren't stored, as in the SQL layer."""

from __future__ import annotations

import base64
import gzip
import json
import logging
import time
from typing import Any, Dict, List, Optional

from chainlit.data.dynamodb import DynamoDBDataLayer
from chainlit.types import PageInfo, PaginatedResponse, ThreadDict

import chat_store
import chat_store_dynamo as dyn

log = logging.getLogger(__name__)

PACKED_PREFIX = "z:"
SNAPSHOT_MAX_BYTES = 300_000     # packed; leaves room in the 400 KB item


def pack_snapshot(record: Dict[str, Any]) -> str:
    """The answer snapshot as gzip + base64 text. If it is still too big,
    halve the preview rows until it fits (the full result stays downloadable
    from S3 while Athena keeps it)."""
    record = json.loads(json.dumps(record))
    while True:
        raw = json.dumps(record, separators=(",", ":")).encode("utf-8")
        packed = PACKED_PREFIX + base64.b64encode(gzip.compress(raw, 6)).decode("ascii")
        preview = ((record.get("result") or {}).get("preview") or {})
        rows = preview.get("data") or []
        if len(packed) <= SNAPSHOT_MAX_BYTES or not rows:
            return packed
        preview["data"] = rows[: len(rows) // 2]
        record["result"]["preview_trimmed"] = True


def unpack_snapshot(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(PACKED_PREFIX):
        return json.loads(gzip.decompress(base64.b64decode(value[len(PACKED_PREFIX):])))
    return value


def _pack_step(step_dict: Dict[str, Any]) -> Dict[str, Any]:
    step_dict = dict(step_dict)
    metadata = step_dict.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get(chat_store.SNAPSHOT_KEY), dict):
        step_dict["metadata"] = {**metadata,
                                 chat_store.SNAPSHOT_KEY: pack_snapshot(metadata[chat_store.SNAPSHOT_KEY])}
    return step_dict


class ChatDynamoDBDataLayer(DynamoDBDataLayer):

    def __init__(self, store: "dyn.DynamoStore"):
        super().__init__(table_name=store.table, client=store.client)
        self.store = store

    # ── messages ──────────────────────────────────────────────────────────

    async def create_step(self, step_dict):
        step_dict = _pack_step(step_dict)
        expires = dyn.chat_expiry()
        thread_id = step_dict.get("threadId")
        if expires and thread_id and not self.store.is_favourite(thread_id):
            step_dict[dyn.TTL_ATTRIBUTE] = expires
        await super().create_step(step_dict)

    async def update_step(self, step_dict):
        await super().update_step(_pack_step(step_dict))

    async def create_element(self, element):
        # As chainlit_data.py: tables and charts are rebuilt from the answer
        # snapshot on resume, so there is nothing to store.
        return None

    # ── chats ─────────────────────────────────────────────────────────────

    async def get_thread(self, thread_id: str) -> Optional[ThreadDict]:
        thread = await super().get_thread(thread_id)
        if not thread or dyn.is_expired(thread):
            return None
        for step in thread.get("steps", []):
            metadata = step.get("metadata")
            if isinstance(metadata, dict) and chat_store.SNAPSHOT_KEY in metadata:
                metadata[chat_store.SNAPSHOT_KEY] = unpack_snapshot(metadata[chat_store.SNAPSHOT_KEY])
        return thread

    async def update_thread(self, thread_id: str, name: Optional[str] = None,
                            user_id: Optional[str] = None,
                            metadata: Optional[Dict] = None,
                            tags: Optional[List[str]] = None):
        now = self._get_current_timestamp()
        sets = ["UserThreadSK = :sk", "id = :id", "createdAt = if_not_exists(createdAt, :now)"]
        values: Dict[str, Any] = {":sk": f"TS#{now}", ":id": thread_id, ":now": now}
        names: Dict[str, str] = {}
        if name is not None:
            sets.append("#name = :name"); names["#name"] = "name"; values[":name"] = name
        if user_id:
            sets += ["userId = :uid", "userIdentifier = :uid", "UserThreadPK = :upk"]
            values[":uid"] = user_id
            values[":upk"] = f"USER#{user_id}"
        if tags is not None:
            sets.append("tags = :tags"); values[":tags"] = tags
        if metadata is not None:
            sets.append("metadata = :meta")
            values[":meta"] = {k: v for k, v in metadata.items() if k != "history"}
        expires = dyn.chat_expiry()
        if expires and not self.store.is_favourite(thread_id):
            # Only a new chat gets its first expiry here; extending it is
            # touch_chat's job, for all of the chat's items together.
            sets.append("expiresAt = if_not_exists(expiresAt, :exp)")
            values[":exp"] = expires
        self.client.update_item(
            TableName=self.table_name,
            Key={"PK": {"S": f"THREAD#{thread_id}"}, "SK": {"S": "THREAD"}},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeValues=self._serialize_item(values),
            **({"ExpressionAttributeNames": names} if names else {}))

    async def delete_thread(self, thread_id: str):
        await self.store.delete_chat(thread_id)

    async def list_threads(self, pagination, filters) -> PaginatedResponse[ThreadDict]:
        """As the parent, leaving out chats that have expired but that TTL
        hasn't deleted yet."""
        names = {"#pk": "UserThreadPK", "#exp": dyn.TTL_ATTRIBUTE}
        values: Dict[str, Any] = {":pk": {"S": f"USER#{filters.userId}"},
                                  ":now": {"N": str(int(time.time()))}}
        conditions = ["(attribute_not_exists(#exp) OR #exp > :now)"]
        if filters.search:
            conditions.append("contains(#name, :search)")
            names["#name"] = "name"
            values[":search"] = {"S": filters.search}
        query: Dict[str, Any] = {
            "TableName": self.table_name, "IndexName": dyn.USER_THREAD_INDEX,
            "ScanIndexForward": False, "Limit": self.user_thread_limit,
            "KeyConditionExpression": "#pk = :pk",
            "FilterExpression": " AND ".join(conditions),
            "ExpressionAttributeNames": names, "ExpressionAttributeValues": values}
        if pagination.cursor:
            query["ExclusiveStartKey"] = json.loads(pagination.cursor)
        response = self.client.query(**query)
        page = PaginatedResponse(data=[], pageInfo=PageInfo(
            hasNextPage="LastEvaluatedKey" in response, startCursor=pagination.cursor,
            endCursor=json.dumps(response["LastEvaluatedKey"]) if "LastEvaluatedKey" in response else None))
        for raw in response.get("Items", []):
            item = self._deserialize_item(raw)
            page.data.append(ThreadDict(  # type: ignore[typeddict-item]
                id=item["PK"][len("THREAD#"):],
                createdAt=item["UserThreadSK"][len("TS#"):],
                name=item.get("name")))
        return page
