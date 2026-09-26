# Data Lake Knowledge Files

This folder holds the **business knowledge** the agent uses to write SQL.

| File | Purpose | Edited by |
|---|---|---|
| `data-dictionary/` | **Authoritative** per-table reference: what each table and column *means* | Data team, in `reporting-deltalake` — **not here** |
| `dictionary_gaps.md` | Historical review queue from the `domain_rules.md` migration — see below | Humans, on review |
| `feedback.md` | Observations the agent recorded during real queries | Agent (append) → Human review |

## How the agent uses these files

On every user question the agent calls, in order:

1. `get_data_dictionary(tables)` — the per-table reference for every table the query will touch, plus the shared cross-table rules from the dictionary's own README.
2. `describe_table(db, table)` — only for tables the dictionary doesn't document, or to confirm a column exists.

**The data dictionary is the agent's ONLY curated source of data knowledge.**
The `domain_rules.md` playbook that used to sit alongside it was retired on
2026-09-24; everything it knew about the data was merged into the dictionary,
and everything it said about agent *behaviour* (entity-resolution policy,
reporting format, never fabricating a result) moved into the system prompt in
`prompt.py`. That split is the rule going forward:

- **What the data means → the dictionary.** Nothing else.
- **How the agent behaves → `prompt.py`.**

If a data fact isn't in the dictionary, it isn't established. Don't start a
second knowledge file — add it upstream, or let the agent verify against Glue.

**Precedence when sources disagree:**

- On what a column *means* or how tables relate → **the data dictionary wins**.
- On whether a column *exists* and its type → **Glue wins**.

When the agent learns something useful, it calls `capture_finding(...)`, which appends to `feedback.md` for human review.

## About `data-dictionary/`

A **vendored copy** of `reporting-deltalake/docs/data-dictionary` (currently commit `20b253f`, 2026-09-23), where it is maintained by the data team and scoped deliberately to data meaning — no ingestion detail, job names or code paths.

**Don't hand-edit these files here.** Edits belong upstream in `reporting-deltalake`; anything changed locally is lost on the next refresh and silently diverges in the meantime. To refresh:

```bash
cp ../../reporting-deltalake/docs/data-dictionary/*.md app/knowledge/data-dictionary/
```

The agent builds its table→doc index by parsing the dictionary README's `## Tables` section at call time, so a refreshed copy (including newly added tables) takes effect with no code change and no restart.

> ⚠️ **`knowledge/` is excluded from deploys.** [`remote_deploy.sh`](../../.github/deploy/remote_deploy.sh) extracts the release tarball with `--exclude='knowledge'`, and the folder is a Docker volume on the box. A refreshed dictionary must be copied to the EC2 instance directly — a deploy will not carry it.

## Reviewing `dictionary_gaps.md`

**Its line references point into a file that no longer exists**, so treat it as
history, not a live queue. It is still worth reading once: it records genuine
**CONFLICT** items where the retired playbook and the dictionary disagreed, and
some of those were never resolved — including a wrong partition-key claim and a
partition-pruning assertion that affects Athena spend. Anything still true and
unresolved belongs upstream in `reporting-deltalake`; then this file can go.

## Reviewing `feedback.md`

- Append-only — the agent never modifies existing entries.
- Read it periodically (weekly is reasonable).
- For each useful entry, promote it upstream into the `reporting-deltalake` data dictionary (or into `prompt.py` if it's agent behaviour, not data), then delete the entry from `feedback.md` (or move it to an `archive/` folder).
- Bad / wrong observations: just delete them. No harm done.

## Why this design

- **Knowledge is editable without redeploys** — markdown files in the repo.
- **Knowledge grows with usage** — feedback file captures real observations.
- **Bad inferences don't corrupt the source of truth** — humans review before promotion.
- **Glue stays authoritative for structure** — markdown describes meaning, not schema.

## A note on partial knowledge

The data lake has more tables than the dictionary documents. That's fine.
The agent will discover undocumented tables via Glue when needed, and treats
what it finds as provisional. Query patterns worth canonizing belong upstream
in the dictionary, not in a local file.
