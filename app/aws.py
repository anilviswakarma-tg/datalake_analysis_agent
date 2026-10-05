"""AWS access: boto3 sessions and clients, credential retry, and the Athena
query executor. The only module that talks to AWS directly."""

from __future__ import annotations

import functools
import os
import time
from typing import Any, Dict, List, Optional, Tuple

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


QUERY_TIMEOUT_SECONDS = 900


def _fetch_s3_csv(query_id: str) -> bytes:
    """Read the complete Athena result CSV straight from S3.

    Athena writes the full result file there automatically once a query
    succeeds, so this gives us all rows with a single S3 GetObject call —
    much faster than paging through get_query_results for large result sets.
    Returns empty bytes on any error (caller falls back to in-memory df).
    """
    if not query_id or query_id == "n/a":
        return b""
    try:
        bucket, key = _result_location(query_id)
        return _session().client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:
        return b""


def _result_location(query_id: str) -> Tuple[str, str]:
    """(bucket, key) of a query's result CSV, as Athena reports it. Not
    rebuilt from ATHENA_OUTPUT_S3: a workgroup that enforces its own output
    location writes elsewhere, and a guessed path would silently cut every
    download down to the in-app preview."""
    try:
        execution = _athena().get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
        uri = execution["ResultConfiguration"]["OutputLocation"]
    except Exception:
        uri = f"{_output_s3().rstrip('/')}/{query_id}.csv"     # best guess
    path = uri[5:] if uri.startswith("s3://") else uri
    bucket, _, key = path.partition("/")
    return bucket, key


# ═══════════════════════════════════════════════════════════════════════════
# 2. ATHENA QUERY EXECUTOR
# ═══════════════════════════════════════════════════════════════════════════

class AthenaQueryError(RuntimeError):
    """A query Athena failed or cancelled. Carries what it still cost: a
    query cancelled by the workgroup's per-query scan cutoff has scanned up
    to the cutoff, and that is billed."""

    def __init__(self, reason: str, query_id: str, bytes_scanned: int, state: str):
        super().__init__(reason)
        self.query_id = query_id
        self.bytes_scanned = bytes_scanned
        self.state = state

    @property
    def scan_cutoff(self) -> bool:
        """Stopped by the workgroup's BytesScannedCutoffPerQuery. Matched on
        Athena's wording ("Bytes scanned limit was exceeded"); unverified
        against a live cutoff until the agent's workgroup exists."""
        return "scanned limit" in str(self).lower()


def _bytes_scanned(execution: Dict[str, Any]) -> int:
    return int((execution.get("Statistics") or {}).get("DataScannedInBytes") or 0)


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
    deadline = time.time() + QUERY_TIMEOUT_SECONDS
    while True:
        if time.time() > deadline:
            # Stop it, or Athena keeps scanning (and billing) after we've
            # given up on the result.
            try:
                athena.stop_query_execution(QueryExecutionId=qid)
            except Exception:
                pass
            raise TimeoutError(f"Athena query {qid} timed out after "
                               f"{QUERY_TIMEOUT_SECONDS}s and was stopped")
        execution = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        stt = execution["Status"]
        state = stt["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            raise AthenaQueryError(stt.get("StateChangeReason", "unknown Athena error"),
                                   qid, _bytes_scanned(execution), state)
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
    df.attrs["bytes_scanned"] = _bytes_scanned(execution)
    df.attrs["workgroup"] = start_kwargs["WorkGroup"]
    for col in df.columns:
        try:
            df[col] = pd.to_numeric(df[col])
        except (ValueError, TypeError):
            pass
    return df
