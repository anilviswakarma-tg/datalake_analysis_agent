# music_streams_v3

**Start here for stream-level analysis.** One row per playback stream — the collapsed, business-rule-applied view of [playactivity.md](playactivity.md). Check here (and [music_fetches.md](music_fetches.md)) before dropping to raw `playactivity_v2`.

See [README.md](README.md) for shared vocabulary and cross-table rules.

**Table:** `tg-deltalake-silver.music_streams_v3`, partitioned by `dw_reported_date`, `group_id`. Built daily from `playactivity_v2`.

## What it is

Groups `playactivity_v2` rows by stream, derives play/skip/completion flags, attaches track/owner metadata from `mastermusic`. One row = one stream.

## Key columns

| Column | Meaning |
|---|---|
| `dw_reported_date` | Partition column, type **DATE**. Compare with date literals: `dw_reported_date BETWEEN DATE '2026-09-01' AND DATE '2026-09-30'`. A plain string (`>= '2026-09-01'`) fails with `TYPE_MISMATCH`. |
| `group_id` | Partition column (string): the store. Always filter on it. |
| `stream_id` | Grouping key (`guid`, falls back to `dw_log_id`). |
| `stock_code_id` | The track played: FK → `mastermusic.id` (`dw_stock_type = 'track'`). The way to anything about the content itself, including whether it is video (below). |
| `asset_type_id` | **Not the content type, and empty in practice:** `NULL` or the string `'NULL'` on every ETEG stream checked (2026-09-15). Never filter on it to find video. |
| `play_type` | `'Stream'` (played online) or `'Download'` (played offline from a downloaded file). Both are plays; count both unless the question says online or offline. |
| `owner_id`, `artist_id`, `album_id` | From `mastermusic` at build time — today's ownership, not stream-time (live-table caveat). |
| `is_stream` | `1` if a completion event occurred OR max seconds played > 29 — **use this, not raw `play_action`, to count streams.** |
| `is_play_start`, `is_full_stream`, `is_skip` | Derived from `play_action` codes present in the stream. |
| Most non-flag columns (`ip_address`, `country`, `device_id`, ...) | Come from the single raw row with the **highest `play_action_value`**, not an aggregate. |
| `stream_duration` | Seconds played, capped at track duration. |
| `station_id`/`playlist_id` | From `list_id`, only when `list_type` matches. |
| `sub_sku` | See [subscriptions.md](subscriptions.md) for name resolution. |
| `extras` | Passed through unmodified from `playactivity_v2.extras` — see [playactivity.md#extras-keys](playactivity.md#extras-keys) for known keys. **Pending as of 2026-09-03**: deployed, but hasn't run against real data yet — don't assume this column is actually populated until confirmed. |

## Streams by content type: video, audio, karaoke

This table has no usable content-type column. Join the track to
`mastermusic` and use its **`content_type`** (`audio`, `video`, `karaoke`,
`audiobook`; see [mastermusic.md](mastermusic.md)):

```sql
SELECT m.content_type, sum(s.is_stream) AS streams
FROM "tg-deltalake-silver"."music_streams_v3" s
JOIN "tg-deltalake-bronze"."mastermusic" m
  ON m.id = s.stock_code_id AND m.dw_stock_type = 'track'
WHERE s.group_id = '<store>'
  AND s.dw_reported_date BETWEEN DATE '2026-09-01' AND DATE '2026-09-30'
GROUP BY 1
LIMIT 100
```

For "video streams", filter `m.content_type = 'video'`. Karaoke is its own
`content_type`; say whether it is included. Measured for ETEG, September
2026: 23,449 video streams (20,924 `Stream`, 2,525 `Download`), 32 karaoke,
30.0M audio; 1.4 GB scanned.

## Already filtered out

Fetches, `is_dupe_ex_guid`, null/`0` `stock_code_id`, previews (except `FLUS`/`FLSP`), test users (unmatched `user_id` defaults to **kept** — opposite of `music_fetches`).

## Caveats

- Same 45-day late-arrival caveat as `playactivity_v2` (rebuilt from it daily). Practical effect: don't treat a recent date's stream count as final.

## Related docs

- [playactivity.md](playactivity.md), [music_fetches.md](music_fetches.md), [mastermusic.md](mastermusic.md)
