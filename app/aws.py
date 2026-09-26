"""AWS access: boto3 sessions and clients, credential retry, and the Athena
query executor. The only module that talks to AWS directly."""

from __future__ import annotations

import functools
import os
import time
from typing import Any, Dict, List, Optional

import boto3
import pandas as pd

from config import _output_s3, _workgroup



# ═══════════════════════════════════════════════════════════════════════════
# 1. AWS HELPERS
# ═══════════════════════════════════════════════════════════════════════════

@functools.lru_cache(maxsize=8)
def _cached_session(profile: str, region: str) -> boto3.Session:
    # The Session itself is cheap and safe to cache (it does NOT resolve
    # credentials at construction time, so caching it can't "lock in" a
    # transient failure). We deliberately do NOT cache the boto3 *client*
    # built from it (see _glue/_athena below) — a client resolves and
    # binds credentials once at creation time, and if that happens to hit
    # a brief EC2 IMDS hiccup, a cached client would be permanently broken
    # for the life of the process. Creating a fresh client per call (from
    # this shared session) lets a failed lookup retry cleanly next time.
    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


def _session() -> boto3.Session:
    profile = os.getenv("AWS_PROFILE", "")
    region = os.getenv("AWS_REGION", "us-west-2")
    return _cached_session(profile, region)


def _glue():
    return _session().client("glue")


def _athena():
    return _session().client("athena")


def _with_credential_retry(fn, *args, retries: int = 3, delay: float = 1.5, **kwargs):
    """Run an AWS call, retrying a couple of times if it fails with
    NoCredentialsError. EC2 instance-role credentials come from IMDS over
    the network, and a brand-new container's network path to IMDS can
    have a brief hiccup right after startup (or under rare transient
    conditions later). Each retry re-enters `fn`, which should create a
    fresh client (via _glue()/_athena()) rather than reusing one, so the
    credential lookup actually gets retried instead of reusing a client
    that already failed to bind credentials."""
    from botocore.exceptions import NoCredentialsError
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except NoCredentialsError as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(delay)
    raise last_exc


def _fetch_s3_csv(query_id: str) -> bytes:
    """Read the complete Athena result CSV straight from S3.

    Athena writes the full result file there automatically once a query
    succeeds, so this gives us all rows with a single S3 GetObject call —
    much faster than paging through get_query_results for large result sets.
    Returns empty bytes on any error (caller falls back to in-memory df).
    """
    s3_uri = _output_s3().rstrip("/")
    if not s3_uri or not query_id or query_id == "n/a":
        return b""
    path = s3_uri[5:] if s3_uri.startswith("s3://") else s3_uri
    bucket, _, prefix = path.partition("/")
    key = f"{prefix.rstrip('/')}/{query_id}.csv" if prefix else f"{query_id}.csv"
    try:
        return _session().client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:
        return b""


# ═══════════════════════════════════════════════════════════════════════════
# 2. ATHENA QUERY EXECUTOR
# ═══════════════════════════════════════════════════════════════════════════

def _run_athena_query(
    sql: str,
    database: Optional[str] = None,
    limit_rows: int = 1000,
) -> pd.DataFrame:
    start_kwargs: Dict[str, Any] = {
        "QueryString": sql,
        "WorkGroup": _workgroup(),
    }
    if database:
        start_kwargs["QueryExecutionContext"] = {"Database": database}
    if _output_s3():
        start_kwargs["ResultConfiguration"] = {"OutputLocation": _output_s3()}

    # First call uses a fresh client each retry attempt (see
    # _with_credential_retry) in case of a transient IMDS hiccup. Once it
    # succeeds, the shared Session's credentials are resolved/cached, so
    # later calls in this function are reliable without needing retry.
    start = _with_credential_retry(lambda: _athena().start_query_execution(**start_kwargs))
    qid = start["QueryExecutionId"]

    athena = _athena()
    deadline = time.time() + 900
    while True:
        if time.time() > deadline:
            raise TimeoutError(f"Athena query {qid} timed out after 300s")
        status = athena.get_query_execution(QueryExecutionId=qid)
        stt = status["QueryExecution"]["Status"]
        state = stt["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            raise RuntimeError(stt.get("StateChangeReason", "unknown Athena error"))
        time.sleep(1.5)

    paginator = athena.get_paginator("get_query_results")
    rows: List[List[Any]] = []
    columns: List[str] = []
    fetched = 0
    for i, page in enumerate(paginator.paginate(QueryExecutionId=qid)):
        rs = page["ResultSet"]
        if i == 0:
            columns = [c["Label"] for c in rs["ResultSetMetadata"]["ColumnInfo"]]
            data_rows = rs["Rows"][1:]
        else:
            data_rows = rs["Rows"]
        for r in data_rows:
            if fetched >= limit_rows:
                break
            rows.append([cell.get("VarCharValue") for cell in r["Data"]])
            fetched += 1
        if fetched >= limit_rows:
            break

    df = pd.DataFrame(rows, columns=columns)
    df.attrs["query_id"] = qid
    for col in df.columns:
        try:
            df[col] = pd.to_numeric(df[col])
        except (ValueError, TypeError):
            pass
    return df
