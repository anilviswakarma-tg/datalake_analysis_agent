# music_fetches

**Start here for fetch activity** — a fetch is a client retrieving track data/file for streaming or playback, not a play event itself. One row per fetch event — deduped, metadata-enriched view of `playactivity.md`'s fetch-flagged rows. Check here (and [music_streams_v3.md](music_streams_v3.md) for playback) before dropping to raw `playactivity_v2`.

See [README.md](README.md) for shared vocabulary and cross-table rules.

**Table:** `tg-deltalake-silver.music_fetches`, partitioned by `dw_reported_date`, `group_id`. Built daily from `playactivity_v2` where `is_fetch = 'true'`.

## What it is

Straight dedup (one row per `fetch_id`, earliest wins) plus track/owner metadata — unlike `music_streams_v3`, no aggregated flags; a fetch just is or isn't present.

## Key columns

| Column | Meaning |
|---|---|
| `fetch_id` | Dedup key (`guid`, falls back to `dw_log_id`). |
| `owner_id`, `artist_id`, `album_id` | From `mastermusic` at build time — same live-table caveat as `music_streams_v3`. |
| `api_type` | Overridden for specific `(group_id, api_type)` pairs (e.g. `FLSP` 2→1) — check the override list before reconciling against raw source. |
| `longitude` | Spelled correctly here — `playactivity_v2`/`music_streams_v3` keep the source `longtitude` typo. |
| `sub_sku` | See [subscriptions.md](subscriptions.md) for name resolution. |

## Already filtered out

`is_fetch = 'true'`, `is_dupe_ex_guid = false`, null/`0` `stock_code_id`, test users (`lower(test_user) <> 'y'` — **unmatched `user_id` is excluded here**, opposite of `music_streams_v3`).

## Caveats

- Live-table drift on `owner_id`/`artist_id`/`album_id`, same as `mastermusic`.
- No `FLUS`/`FLSP` out-of-band correction job exists for this table (unlike `music_streams_v3`).
- Same 45-day late-arrival caveat as `playactivity_v2`.

## Related docs

- [playactivity.md](playactivity.md), [music_streams_v3.md](music_streams_v3.md), [mastermusic.md](mastermusic.md)
