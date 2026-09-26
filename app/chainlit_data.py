"""Chainlit's chat-history data layer, made safe on any SQLAlchemy backend.

Chainlit's SQLAlchemyDataLayer is written against Postgres and fails on
anything else in ways that are easy to miss, because it catches every
database error and logs a warning instead of raising. This subclass fixes
the three it trips over, so the same code runs on SQLite today and Postgres
later; which one is purely chat_store.chat_db_url()."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Union

from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from sqlalchemy import text

import chat_store

log = logging.getLogger(__name__)


class PortableSQLAlchemyDataLayer(SQLAlchemyDataLayer):

    async def execute_sql(self, query: str, parameters: dict
                          ) -> Union[List[Dict[str, Any]], int, None]:
        """As the parent, but a failed write is logged as an ERROR with its
        query. The parent logs a warning and carries on, which is how a
        missing column silently stopped history being saved."""
        async with self.async_session() as session:
            try:
                await session.begin()
                result = await session.execute(text(query), parameters)
                await session.commit()
                if result.returns_rows:
                    return self.clean_result([dict(r._mapping) for r in result.fetchall()])
                return result.rowcount
            except Exception:
                await session.rollback()
                log.exception("chat history SQL failed: %s", " ".join(query.split())[:300])
                return None

    async def update_thread(self, thread_id: str, name: Optional[str] = None,
                            user_id: Optional[str] = None,
                            metadata: Optional[Dict] = None,
                            tags: Optional[List[str]] = None):
        # Tags arrive as a Python list, which only Postgres arrays can bind.
        # They are unused (auto_tag_thread is off), so drop them rather than
        # require an array type on every backend.
        await super().update_thread(thread_id, name=name, user_id=user_id,
                                    metadata=metadata)

    async def create_step(self, step_dict):
        step_dict = dict(step_dict)
        step_dict.pop("tags", None)        # as update_thread
        # `modes` (the model picker selection on a user message) is a dict the
        # parent passes through raw; no driver binds a dict to a text column.
        if isinstance(step_dict.get("modes"), (dict, list)):
            step_dict["modes"] = json.dumps(step_dict["modes"])
        await super().create_step(step_dict)

    async def create_element(self, element):
        # Deliberately not persisted: there is no blob store, and none is
        # needed. Tables and charts are rebuilt on resume from the result
        # snapshot saved with the answer (chat_store.answer_metadata).
        return None


def build_data_layer(url: str) -> PortableSQLAlchemyDataLayer:
    return PortableSQLAlchemyDataLayer(conninfo=url, connect_args=chat_store.connect_args(url))
