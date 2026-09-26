# playactivity_v2

Raw play/fetch event log — one row per client-reported playback or fetch (data/file retrieval for streaming or playback) action. Bronze source for `music_streams_v3`/`music_fetches`/`label_streams`.

**Check [music_streams_v3.md](music_streams_v3.md) (streams) and [music_fetches.md](music_fetches.md) (fetches) first** — they've already collapsed rows and applied business-rule filtering. Come here only for raw event-level fields those tables don't carry.

See [README.md](README.md) for shared vocabulary and cross-table rules.

**Table:** `tg-deltalake-bronze.playactivity_v2`, partitioned by `dw_reported_date`, `group_id`. PK: `dw_log_id`.

## What it is

Unions two Firehose streams (`PlayLog`, `PlayDownload`, tagged via `dw_data_source`). One logical stream/fetch can produce **multiple rows** here (e.g. start + complete events sharing a `guid`) — don't treat row count as play count.

## Key columns

| Column | Meaning |
|---|---|
| `dw_log_id` | PK, hash-derived from event fields. |
| `stock_code_id` | FK → `mastermusic.id`. Filter out null/`0`. |
| `guid` | Ties rows of one stream/fetch together. Can be missing (null/`''`/`'NULL'`) — downstream falls back to `dw_log_id` as the grouping key. |
| `play_action` | `1` = play start, `3` = full stream/complete, `4` = skip. Other codes exist but aren't confirmed. |
| `list_id`/`list_type` | Source of playback — see code table below. |
| `is_fetch` | `'true'` when `list_type = 'F'` — client retrieving track data/file for streaming or playback, not a play event itself. |
| `is_dupe_ex_guid` | Near-duplicate flag (same event, different guid) — always filter `= false` when counting. |
| `dw_reported_date` | Partition column. |
| `extras` | Raw JSON text carrying source/telemetry key-value pairs not otherwise modeled as columns — see [extras keys](#extras-keys) below. Plain `string`, not a map: `extras['key']` fails (`TYPE_MISMATCH`); use `json_extract_scalar(extras, '$.key')` (Athena/Presto) or `get_json_object(extras, '$.key')` (Spark SQL). Added 2026-09-03 — `NULL` on all rows ingested before then, since Delta doesn't backfill new columns onto existing files. |

## extras keys

| Key | Meaning | Populated by |
|---|---|---|
| `dts` | Device timestamp (epoch seconds) | Real-time Firehose `PlayLog` ingestion — also duplicated as the `device_ts` column (candidate for cleanup) |
| `tts` | True timestamp (epoch seconds) | Real-time Firehose `PlayLog` ingestion — also duplicated as `true_ts` |
| `bn` | Build number | Real-time Firehose `PlayLog` ingestion — also duplicated as `build_number` |
| `pbt` | Playback type (e.g. `TunedAutomix`) | Real-time Firehose `PlayLog` ingestion — also duplicated as `playback_type` |
| `cd` | Crossfade duration | Real-time Firehose `PlayLog` ingestion — also duplicated as `crossfade_duration` |
| `src` | Marks a row as coming from a source other than the standard Firehose `PlayLog`/`PlayDownload` pipeline, and identifies which one. Known value: `"contest"` — SongPicks (`FLSP`) contest usage, an ongoing data source (not a one-off historical load). | Non-Firehose sources generally; `contest` specifically = SongPicks |
| `abtg` | AB testing group. | Gabb (`GAUS`) app. |
| `es` | Extra source. Known values: `daily_discovery`, `iq_recommended_tracks`, `iq_recommended_artists`, `iq_continuous_play`. | Gabb (`GAUS`) app. |

New `src` values should be added to this table whenever a new non-Firehose source starts feeding this table.

## list_type codes

`R`=station, `P`=playlist, `Q`=queue, `A`=artist, `M`=album, `C`=podcast, `W`=preview, `F`=fetch, `L`=log, `E`=extradio, `T`=continuous, `S`=system, `B`=audiobook, `V`=voice

## Counting streams on bronze

A "stream" is a play of at least 30 seconds, or a completed play:

```sql
SUM(CASE WHEN play_action_value >= 30 OR play_action = 3 THEN 1 ELSE 0 END)
```

**Prefer silver.** [music_streams_v3.md](music_streams_v3.md)'s `is_stream` column
already encodes this rule — use `SUM(is_stream)` there instead of recomputing it.
Only reach for the formula above when the question needs an event-level column
that silver doesn't carry.

## Caveats

- Append-only — events don't get removed once written.
- **Late-arriving data**: a delayed-delivery job upserts up to 45 days back — don't treat recent days' totals as final.
- `play_type = 'preview'` rows are excluded from stream counting except for groups `FLUS`/`FLSP` (intentional).
- Joining `mastermusic`: always add `dw_stock_type = 'track'`; owner attribution reflects today's ownership, not event-time ownership (see [mastermusic.md](mastermusic.md)).

## Related docs

- [mastermusic.md](mastermusic.md), [users.md](users.md), [music_streams_v3.md](music_streams_v3.md), [music_fetches.md](music_fetches.md)
