# disambiguation_v1

Plain-language reference for anyone (human or AI agent) querying `disambiguation_v1` alongside `mastermusic` and other tables. See [README.md](README.md) for shared vocabulary and cross-table rules.

## What it is

`mastermusic` (the master track/album catalogue, ~219M rows, prod) contains many duplicate rows for what is really the *same underlying recording* — the same song ingested multiple times under different `track_id`s (different owners, re-encodings, re-uploads, etc.).

`disambiguation_v1` is a daily pipeline output that groups those duplicate track rows into **clusters** representing one real-world recording, so downstream consumers can say "these N `track_id`s are actually the same recording" without re-running matching logic themselves.

**Table:** `tg-deltalake-silver.disambiguation_v1`, partitioned by `owner_id`.

## Schema

| Column | Type | Meaning |
|---|---|---|
| `track_id` | bigint | Row key — matches `mastermusic.id` |
| `owner_id` | bigint | Owning label/catalogue; also the partition column |
| `disambiguation_id` | bigint | Cluster ID = the **minimum `track_id`** among all tracks judged to be the same recording. An unmatched track's `disambiguation_id` equals its own `track_id`. |
| `confidence` | int | 100 = isolated/self, 99 = audio-hash match, 97 = ISRC+metadata match, 95 = metadata match (exact artist or artist spelling variant) |
| `match_type` | string | `self`, `md5_96k`, `md5_320k`, `isrc_metadata`, `metadata_only`, `artist_variant` |
| `content_type` | string | `audio`, `karaoke`, `video`, `audiobook` — clusters never cross content types |
| `isrc` | string | Track's ISRC code |
| `cluster_size` | int | Number of tracks in this track's cluster (1 for isolated tracks) — **can be stale, see caveats** |
| `dw_created_at` | timestamp | First time this track row was written to disambiguation_v1 |
| `dw_updated_at` | timestamp | Last write/update time for this row |

## Relationship to mastermusic

**Join key: `mastermusic.id` = `disambiguation_v1.track_id`.**

To find "all tracks that are the same recording as track X": look up X's `disambiguation_id` in `disambiguation_v1`, then find every row sharing that same `disambiguation_id` — those `track_id`s are the full cluster.

Built entirely from `tg-deltalake-bronze.mastermusic`, filtered to `dw_stock_type='track'` (albums excluded) with a valid nonzero duration and a non-empty ISRC. **Tracks with missing/zero duration or missing ISRC (~17K) are excluded entirely** — they never appear in `disambiguation_v1`, not even as isolated singletons.

`disambiguation_id` is computed fresh by the pipeline (graph connected-components over match "edges") and is **not** stored anywhere in mastermusic itself.

## How tracks get matched (priority order)

1. Identical file hash (96kbps or 320kbps) → confidence 99
2. Same ISRC + duration within 5s + similar title → confidence 97
3. Same normalized artist+title + duration within 5s, no ISRC → confidence 95
4. Same normalized title + duration within 5s, artist names differing only slightly (similarity ≥ 0.85 and at most 2 character edits, e.g. `moustafa amar` / `mostafa amar`) → `artist_variant`, confidence 95
5. No match → isolated, confidence 100, cluster of 1

Normalized artist ignores case, punctuation and `&` vs `and`, so `b.b. king` and `b b king` are the same artist.

Rules 3 and 4 both carry confidence 95 and differ only in `match_type` (`metadata_only` vs `artist_variant`).

Titles that do not identify a recording (e.g. `intro`, `outro`, `interlude`, `skit`) never match through 4; they still match through 1–3.

Content types (audio/karaoke/video/audiobook) never merge across each other, even with identical title/ISRC — this is intentional, not a gap.

## Finding the disambiguation set for a store

Stores/clients are identified by `group_id`. [track_active.md](track_active.md) holds the current `(track_id, group_id)` pairings — i.e. which tracks each store currently carries. Join it to `disambiguation_v1` on `track_id` to get each of a store's tracks along with its cluster:

```sql
SELECT ta.track_id, ta.group_id, d.disambiguation_id, d.confidence, d.content_type
FROM "tg-deltalake-bronze".track_active ta
JOIN "tg-deltalake-silver".disambiguation_v1 d ON ta.track_id = d.track_id
WHERE ta.group_id = <store's group_id>
```

`track_active` is large (1.1B+ rows) — always filter by `group_id` before joining, don't scan unfiltered. To get the store's distinct recordings (rather than every duplicate track row), group the result by `disambiguation_id`.

## Caveats to keep in mind when analyzing

- **`cluster_size` can be stale.** Incremental runs update `cluster_size` only on newly-changed rows within a cluster, not on older unchanged member rows — an old row may understate the true current cluster size.
- **`confidence` and `match_type` can be stale the same way.** A row is rewritten only when its cluster changes, so an unchanged row keeps the label from its last write.
- Disambiguation always reads current mastermusic, so re-running it for a past period is not reproducible, and cluster membership / `owner_id` attribution can shift between runs — see the mastermusic live-table rule in [README.md](README.md).

## Quick vocabulary

- **Cluster** — a set of `track_id`s judged to be the same recording; identified by their shared `disambiguation_id`.
- **Canonical track_id** — the lowest `track_id` in a cluster; used as the cluster's `disambiguation_id`.
- **Isolated track** — a track with no matches; its own cluster of size 1.
