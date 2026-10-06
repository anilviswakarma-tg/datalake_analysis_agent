"""Which store keeps chat history, favourites and Athena usage. The app
(chainlit_app.py) talks only to the store this returns, never to a backend
directly, so backends can be swapped by configuration.

CHAT_STORE picks one:
    sql       (default) chat_store.py + chainlit_data.py: SQLite, or Postgres
              such as RDS, by CHAT_DB_URL (CHAT_DB_URL=off disables history)
    dynamodb  chat_store_dynamo.py + chainlit_data_dynamo.py: one DynamoDB
              table (DYNAMODB_TABLE; DYNAMODB_ENDPOINT_URL for DynamoDB Local)
    off       no history

Both stores have the same methods (ChatStore). Settings that don't depend on
the backend - retention days, the favourites and chat caps, the saved-answer
format - stay in chat_store.py and apply to both."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, Set

import chat_store


class ChatStore(Protocol):
    name: str
    sweeps: bool          # True: the app runs purge_expired daily; False: the backend expires chats itself

    def data_layer(self) -> Any: ...
    async def ensure_schema(self) -> None: ...
    async def purge_expired(self) -> int: ...
    async def touch_chat(self, thread_id: str) -> None: ...
    async def set_favourite(self, thread_id: str, user: str, favourite: bool) -> None: ...
    async def favourite_thread_ids(self, user: str) -> Set[str]: ...
    async def favourite_chats(self, user: str) -> List[Dict[str, Any]]: ...
    async def record_usage(self, user: str, thread_id: Optional[str],
                           scans: List[Dict[str, Any]], now: Optional[datetime] = None) -> None: ...
    async def month_usage(self, user: str, now: Optional[datetime] = None) -> Dict[str, Any]: ...
    async def user_tier(self, user: str) -> str: ...
    async def set_user_tier(self, user: str, tier: str) -> None: ...


class SqlStore:
    """chat_store.py's SQL functions, bound to one database URL."""

    name = "sql"
    sweeps = True

    def __init__(self, url: str):
        # Imported here, while the app loads: Chainlit only has this folder on
        # the import path then, not later when it asks for the data layer.
        from chainlit_data import build_data_layer
        self.url = url
        self._build_layer = build_data_layer

    def data_layer(self):
        return self._build_layer(self.url)

    async def ensure_schema(self) -> None:
        await chat_store.ensure_schema(self.url)

    async def purge_expired(self) -> int:
        return await chat_store.purge_expired(self.url)

    async def touch_chat(self, thread_id: str) -> None:
        return None         # SQL retention reads each chat's newest message instead

    async def set_favourite(self, thread_id, user, favourite) -> None:
        await chat_store.set_favourite(self.url, thread_id, user, favourite)

    async def favourite_thread_ids(self, user):
        return await chat_store.favourite_thread_ids(self.url, user)

    async def favourite_chats(self, user):
        return await chat_store.favourite_chats(self.url, user)

    async def record_usage(self, user, thread_id, scans, now=None) -> None:
        await chat_store.record_usage(self.url, user, thread_id, scans, now=now)

    async def month_usage(self, user, now=None):
        return await chat_store.month_usage(self.url, user, now=now)

    async def user_tier(self, user):
        return await chat_store.user_tier(self.url, user)

    async def set_user_tier(self, user, tier) -> None:
        await chat_store.set_user_tier(self.url, user, tier)


def store_from_env() -> Optional[ChatStore]:
    """The configured store, or None when history is off."""
    kind = os.getenv("CHAT_STORE", "sql").strip().lower() or "sql"
    if kind == "off":
        return None
    if kind == "sql":
        url = chat_store.chat_db_url()
        return SqlStore(url) if url else None
    if kind in ("dynamodb", "dynamo"):
        from chat_store_dynamo import DynamoStore
        return DynamoStore()
    raise ValueError(f"CHAT_STORE must be sql, dynamodb or off, not {kind!r}")
