"""Assembling the LangChain agent from a model, the tool set, and the prompt."""

from __future__ import annotations

from langchain.agents import create_agent

import models
from models import _build_llm
from prompt import _build_system_prompt, question_hints
from run_state import current_run
from tools import (capture_finding, describe_table,
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
    # Plus notes on what the question itself contains (e.g. stock codes)
    prompt = _build_system_prompt() + question_hints(current_run().question)
    return create_agent(model, tools, system_prompt=prompt)
