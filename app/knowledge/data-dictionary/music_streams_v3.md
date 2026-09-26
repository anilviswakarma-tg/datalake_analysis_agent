# music_streams_v3

**Start here for stream-level analysis.** One row per playback stream — the collapsed, business-rule-applied view of [playactivity.md](playactivity.md). Check here (and [music_fetches.md](music_fetches.md)) before dropping to raw `playactivity_v2`.

See [README.md](README.md) for shared vocabulary and cross-table rules.

**Table:** `tg-deltalake-silver.music_streams_v3`, partitioned by `dw_reported_date`, `group_id`. Built daily from `playactivity_v2`.

## What it is

Groups `playactivity_v2` rows by stream, derives play/skip/completion flags, attaches track/owner metadata from `mastermusic`. One row = one stream.

## Key columns

| Column | Meaning |
|---|---|
| `stream_id` | Grouping key (`guid`, falls back to `dw_log_id`). |
| `owner_id`, `artist_id`, `album_id` | From `mastermusic` at build time — today's ownership, not stream-time (live-table caveat). |
| `is_stream` | `1` if a completion event occurred OR max seconds played > 29 — **use this, not raw `play_action`, to count streams.** |
| `is_play_start`, `is_full_stream`, `is_skip` | Derived from `play_action` codes present in the stream. |
| Most non-flag columns (`ip_address`, `country`, `device_id`, ...) | Come from the single raw row with the **highest `play_action_value`**, not an aggregate. |
| `stream_duration` | Seconds played, capped at track duration. |
| `station_id`/`playlist_id` | From `list_id`, only when `list_type` matches. |
| `sub_sku` | See [subscriptions.md](subscriptions.md) for name resolution. |
| `extras` | Passed through unmodified from `playactivity_v2.extras` — see [playactivity.md#extras-keys](playactivity.md#extras-keys) for known keys. **Pending as of 2026-09-03**: deployed, but hasn't run against real data yet — don't assume this column is actually populated until confirmed. |

## Already filtered out

Fetches, `is_dupe_ex_guid`, null/`0` `stock_code_id`, previews (except `FLUS`/`FLSP`), test users (unmatched `user_id` defaults to **kept** — opposite of `music_fetches`).

## Caveats

- Same 45-day late-arrival caveat as `playactivity_v2` (rebuilt from it daily). Practical effect: don't treat a recent date's stream count as final.

## Related docs

- [playactivity.md](playactivity.md), [music_fetches.md](music_fetches.md), [mastermusic.md](mastermusic.md)
