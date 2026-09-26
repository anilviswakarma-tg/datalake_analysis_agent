"""Assembling the LangChain agent from a model, the tool set, and the prompt."""

from __future__ import annotations

from langchain.agents import create_agent

import models
from models import _build_llm
from prompt import _build_system_prompt
from tools import (capture_finding, count_rows, describe_table,
                   get_data_dictionary, list_databases,
                   list_tables, note_default_applied, resolve_client,
                   resolve_label, sql_db_query, sql_db_query_checker,
                   visualize_results)



def build_agent():
    """Build the agent for the current run's selected model. The model comes
    from the run context (run_state.start_run), the same place the SQL checker
    reads it, so the two can never disagree."""
    model = _build_llm(models.active_model_choice())
    tools = [
        # Knowledge
        get_data_dictionary,
        capture_finding,
        # Discovery
        list_databases,
        list_tables,
        describe_table,
        count_rows,
        # Entity resolution
        resolve_label,
        resolve_client,
        # SQL
        sql_db_query_checker,
        sql_db_query,
        # UX
        visualize_results,
        note_default_applied,
    ]
    return create_agent(model, tools, system_prompt=_build_system_prompt())
