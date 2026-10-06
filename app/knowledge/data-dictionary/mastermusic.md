# mastermusic

The master track/album catalogue. Used in nearly every report. See [README.md](README.md) for shared vocabulary and cross-table rules.

**Size (measured 2026-09-24, `status = 1`):** 219.3M track rows + 50.9M album rows = 270.1M active rows. The often-quoted "~219M" is the *active track* count, not the table — an unfiltered `COUNT(*)` returns ~280M. Always say which of the three you mean.

**Mandatory filter: `status = 1`** for any "active" question — the table holds
deleted and inactive rows too. **This applies to `mastermusic` only.** It is
not a global catalogue rule, and it is never a reason to join this table into a
question about a store's catalogue; see [track_active.md](track_active.md).

**This is a central catalogue and belongs to no store.** Nothing in it says which store carries a track. Every store-scoped question must be joined to a table that identifies the store — normally [track_active.md](track_active.md). A query filtered only by `owner_id` answers "what exists in this label's catalogue", never "what does this store have".

**Table:** `tg-deltalake-bronze.mastermusic`, partitioned by `dw_stock_type`, `owner_id`, `salt`. **Refreshed daily** (CDC-based upsert).

## Schema (key columns)

| Column | Meaning |
|---|---|
| `id` | Primary key. FK target for `stock_code_id` in `music_streams_v3`/`playactivity_v2`, and for `track_id` in `track_active`/`disambiguation_v1`. |
| `stock_code` | **The product code users quote.** `{owner_id}_{UPC}_{ISRC}` on track rows (`1399_00199957799768_USUM71409728`), `{owner_id}_{UPC}` on album rows (`1399_00199957799768`). Match it exactly against this column — never split it into `id`, `pk` or `track_id` guesses. The same ISRC appears on many albums, so the UPC part matters: `USUM71409728` alone matched 20 tracks under one owner (measured 2026-09-30). See "Looking up products by stock code" below. |
| `pk` | `'Track#<id>'` / `'Album#<id>'` — a key string, never a stock code. |
| `dw_stock_type` | `'track'` or `'album'` — also a partition column. |
| `owner_id` | Owning label/catalogue. Partition column. **Live/mutable** — see caveat below. |
| `album_id` | Populated on track rows only — links a track to its parent album (`mastermusic.id` where `dw_stock_type='album'`). |
| `title` | Track or album title. **Always populated** (100% on both stock types). Free-text search: `LOWER(title) LIKE '%…%'` — no entity resolution needed. |
| `title2` / `title_original` | Alternate and original-script titles. **Sparse — do not rely on them**: `title2` 41% of tracks / 52% of albums, `title_original` 47% / 33%. A search that only checks these misses most of the catalogue; search `title` and treat these as supplementary. |
| `artist_name` | **The performing artist.** Always populated (100% on both stock types). This is the column for any question about artists — see "Artist vs label vs distributor" below, because picking the wrong name column is an easy and silent error. Search with `LOWER(artist_name) LIKE '%…%'`; no entity resolution needed. |
| `artist_name2` / `artist_name_original` | `artist_name_original` is always populated (100%); `artist_name2` is sparse (41% tracks / 51% albums). Prefer `artist_name`. |
| `artist_id` | Numeric artist FK, 99% populated. Group by this rather than `artist_name` when you need distinct artists — names are neither unique nor stable across rows, the same caution that applies to `owner_id` vs `owner_name`. |
| `isrc` | Track identifier. **Tracks only** — 100% of track rows, 0% of album rows (measured 2026-09-24). Because the fill is exactly stock-aligned, `isrc IS NOT NULL` is a de facto `dw_stock_type = 'track'` filter; prefer the explicit partition filter, which prunes. |
| `upc` | Album identifier. **Albums only** — 100% of album rows, 0% of track rows (measured 2026-09-24). Asking for a track's UPC returns nothing; get it from the parent album via `album_id`. |
| `duration_secs` | Track length in seconds (bigint). **Tracks only** — 100% of track rows, 0% of album rows. Any duration aggregate (`AVG`, `SUM`, "longest track") must filter `dw_stock_type = 'track'`, or album rows contribute nothing and silently skew what the number is *of*. |
| `language` / `language2` | Language of `title` / `title2` respectively (2-letter code, e.g. `EN`/`AM`) — paired per-title metadata, not a catalogue-wide attribute. |
| `content_language` | Language of the track's actual audio content. Uses 3-letter codes (e.g. `AMH`), a different scheme than `language`/`language2`'s 2-letter codes. Sparsely populated — only ~11% filled in a sampled owner_id — verify fill rate before relying on it as a primary filter. |
| `content_type` | **What the track is: `audio`, `video`, `karaoke` or `audiobook`.** Use this to tell video from audio, including for streams (join `music_streams_v3.stock_code_id` to `id`; see [music_streams_v3.md](music_streams_v3.md#streams-by-content-type-video-audio-karaoke)). Prefer it to the `is_*` flags below. |
| `is_video`, `is_karaoke`, `is_audiobook` | Boolean flags, but **nullable, not just true/false** — some rows have `NULL` instead of `false` (confirmed for `is_video` 2026-08-25, e.g. mastermusic ids `73375392`/`95894977` under owner `1216`; same nullability applies to `is_karaoke`/`is_audiobook`). A filter written as `WHERE is_video = false` silently drops these rows (`NULL = false` → `NULL`, not true). To mean "flag is not set," use `WHERE (is_video = false OR is_video IS NULL)` or `WHERE coalesce(is_video, false) = false` — same pattern for the other two flags. **`is_karaoke = true` implies `is_video = true`** (karaoke tracks are a video subtype) — when checking for either karaoke or video content, check `is_karaoke` first/separately rather than assuming `is_video = false` rules out karaoke. **Contradicted, 2026-10-06:** every karaoke track streamed by ETEG in September 2026 had `content_type = 'karaoke'` and `is_video = false`; `content_type` avoids the question. |
| `asset_type` | Audio bitrate/quality tier: `505` = low (48 kbps), `506` = high (96 kbps), `522` = HQ (256 kbps). Integer, compared without quotes. "Does client X have HQ audio for all its labels?" means checking each label has **at least one** `asset_type = 522` track — `COUNT(CASE WHEN asset_type = 522 THEN 1 END) > 0` per `owner_id`, reached via [track_active.md](track_active.md) for the client's labels. |
| `rights` | Map keyed by territory code (e.g. `WW` = worldwide, `US` = United States) — **the key's case varies by row** (`US` on some, `us` on others; compare `upper(key)`, see "Looking up products by stock code"), value struct includes `act` (1/0 active flag), `astr` (boolean, streaming allowed), `ssdt`/`sedt` (ISO-8601 rights start/end date strings). `ssdt` null = no lower bound (treat as min/-infinity time); `sedt` null = no upper bound (treat as max/+infinity time). "Currently active right" = `act = 1 AND astr = true AND (ssdt IS NULL OR ssdt <= now) AND (sedt IS NULL OR sedt >= now)` for at least one territory entry via `CROSS JOIN UNNEST(map_values(rights))`. **These are the rights available on the track itself** — catalogue-wide, with no store dimension. They are not any store's rights and must never be mixed with, or read as, store availability; for that see [track_active.md](track_active.md). Two further cautions: `sedt`/`sed` are unset on the overwhelming majority of entries, so the end-date test is effectively inert; and `WW` may be absent entirely, with worldwide grants appearing instead as many individual per-country entries — so `WW` alone is not a reliable test for worldwide rights. **Keys are ISO 3166 two-letter country codes** (after `upper(key)`; `CH` = Switzerland, `AE` = United Arab Emirates): all 249 are used, plus `WW` (worldwide), `AN` (Netherlands Antilles, a retired code) and `XK` (Kosovo). Two keys are free text, data-entry errors that a code lookup can't match: `only egypt` (7 active tracks) and `ksa and uae` (6) (all active tracks checked 2026-10-06). |

## Artist vs label vs distributor — four different name columns

The table carries several `*_name` columns that all look like plausible answers
to "who is this by". Choosing the wrong one produces a confident, wrong,
error-free answer, so pick deliberately:

| Question | Column |
|---|---|
| "top artists", "which artists", "tracks by artist X" | **`artist_name`** (or `artist_id` to group) |
| "which distributor/aggregator delivers this" | `owner_name` (with `owner_id` to group) |
| "which label/imprint is this on" | `label` — see the caution below |

**`owner_name` is the distributor, not the artist and not the label.** Its
values are aggregators — Fuga, CD Baby, Distrokid, One RPM, The Orchard,
United Masters. Grouping by `owner_name` for an "artists" question returns
distributor names that read like plausible artists (e.g. "Warner"), with
nothing to signal the substitution. Observed doing exactly this on
2026-09-24.

**Unresolved — `label` vs `sub_label_name`.** Measured evidence conflicts and
neither is safe to assume:

- Searching for the "Q&A" imprint found it in **`label`** (e.g. `label = 'Q & A
  MUSIC INC'`), while that row's `owner_name` *and* `sub_label_name` both held
  the distributor (`REBEAT Digital GmbH`).
- Yet sampled Warner tracks show `label = 'Warner Music Group'` with a
  *different* `sub_label_name` (`Hidden Square`).

So `sub_label_name` sometimes mirrors the distributor and sometimes holds a
distinct imprint. **When asked about a label or imprint by name, search `label`,
`sub_label_name` and `owner_name` and report which column matched** rather than
assuming one. Both `label` and `sub_label_name` are 100% populated, so a null
check tells you nothing.

## Looking up products by stock code

"Check these products / stock codes and tell me whether they are available
for streaming, and in which territories" is answered from this table: the
`rights` map, per territory. No store is named, so it is a catalogue question,
not a `track_active` one. (If a store *is* named, answer from
[track_active.md](track_active.md) instead.)

Put every code the user gave into a `VALUES` list and `LEFT JOIN`, so the
result has **one row per input code, including the ones that don't exist**
(`NOT FOUND` in the row, never dropped). Derive the partition filters from the
code itself: the first part is `owner_id`, and three parts means a track, two
an album. Without them the lookup scans the whole table. Use this query as it
stands, changing only the codes and the territory columns asked about:

```sql
WITH input(stock_code) AS (VALUES
  '1399_00199957799768_USUM71409728',
  '1399_00199957799768'),
products AS (
  SELECT i.stock_code, m.id, m.dw_stock_type, m.title, m.artist_name,
         -- territory codes are upper case on some rows and lower on others
         -- (owner 1399: 24% lower, never mixed in one row), so compare upper()
         transform(map_entries(m.rights), e -> CAST(ROW(upper(e[1]),
           e[2].act = 1 AND e[2].astr
           AND (e[2].ssdt IS NULL OR from_iso8601_timestamp(e[2].ssdt) <= current_timestamp)
           AND (e[2].sedt IS NULL OR from_iso8601_timestamp(e[2].sedt) >= current_timestamp))
           AS ROW(territory varchar, live boolean))) AS r
  FROM input i
  LEFT JOIN "tg-deltalake-bronze".mastermusic m
    ON m.stock_code = i.stock_code
   AND m.owner_id = split_part(i.stock_code, '_', 1)
   AND m.dw_stock_type = IF(cardinality(split(i.stock_code, '_')) = 3, 'track', 'album')
   AND m.status = 1)
SELECT stock_code,
       CASE WHEN id IS NULL THEN 'NOT FOUND' ELSE dw_stock_type END AS type,
       title, artist_name,
       CASE WHEN id IS NOT NULL THEN IF(any_match(r, x -> x.live), 'Yes', 'No') END AS streaming_anywhere,
       -- a territory's own entry decides; WW only when it has none
       CASE WHEN id IS NOT NULL THEN IF(
         IF(any_match(r, x -> x.territory = 'US'), any_match(r, x -> x.territory = 'US' AND x.live),
                                                   any_match(r, x -> x.territory = 'WW' AND x.live)),
         'Yes', 'No') END AS us_streaming,
       CASE WHEN id IS NOT NULL THEN IF(
         IF(any_match(r, x -> x.territory = 'CA'), any_match(r, x -> x.territory = 'CA' AND x.live),
                                                   any_match(r, x -> x.territory = 'WW' AND x.live)),
         'Yes', 'No') END AS ca_streaming,
       array_join(array_sort(transform(filter(r, x -> x.live), x -> x.territory)), ',') AS streaming_territories
FROM products
```

It returns text `Yes`/`No`, which reads correctly in the CSV/Excel download.
Checked 2026-09-30 against tracks keyed `US`, keyed `us`, keyed only `AU`, and
a code that does not exist.

- **Territory codes are not consistently upper case.** Owner `1399`: 857,569 of
  3.59M active tracks key `rights` in lower case (`us`, `gb`), the rest upper;
  never both in one row (measured 2026-09-30). `element_at(rights, 'US')`
  silently misses a lower-case row and reports "No" wrongly — always compare
  `upper(key)`, as above.
- **A right counts only while it is live**: `act = 1`, `astr = true`, and today
  between `ssdt` and `sedt` (either may be null). Start dates run up to
  2026-12-31, so future rights exist.
- **No entry for a territory means no rights there.** Owner `1399` lists every
  territory individually and never uses `WW`. Example:
  `1399_00199957799768_USUM71409728` has only `AU`, with `astr = false`, so it
  can't be streamed anywhere, US and CA included.
- **`WW` is a fallback, and that's unverified.** How a `WW` entry combines
  with a territory's own entry has not been measured, and no owner checked so
  far uses both. Say so if the answer depends on it.
- **`UM` is not the US** — it's US Minor Outlying Islands.
- **"Available" has two meanings.** Rights (this table) answer "may it be
  streamed in the US at all". Whether a given store actually carries it is
  `track_active` (`group_id`, `country`, `allow_stream`). If the user's
  wording could mean either, answer from rights and say that no store was
  named.
- **Don't chart this.** It's a lookup; the table and its downloads are the
  answer.

## Common join pattern

```sql
-- track metadata
join "tg-deltalake-bronze".mastermusic m on s.stock_code_id = m.id and m.dw_stock_type = 'track'
-- parent album
join "tg-deltalake-bronze".mastermusic a on a.id = m.album_id and a.dw_stock_type = 'album'
```

When relating streams/events to catalogue membership, always join on **both** `stock_code_id` and `owner_id` (not `stock_code_id` alone) — otherwise plays can leak to owners with no actual catalogue membership, since the same `id` can appear under multiple `owner_id` rows over time.

## Caveat

**A label's catalogue size tells you nothing about how much of it a store carries.** These two numbers are unrelated and are easily mistaken for each other when they happen to look similar — in one measured case a label's catalogue and a store's catalogue were both ~3.6M, yet their actual overlap was under 5%. Always join through [track_active.md](track_active.md) and count the intersection; never infer overlap from two totals.


**mastermusic is a live, mutable table with no point-in-time snapshot.** The same track `id` can move between `owner_id`s (reassignment). Re-running the same report period weeks apart can produce different per-owner totals purely from this drift, independent of any query change.

## Related docs

- [track_active.md](track_active.md) — which tracks a store currently carries; joins to mastermusic on `track_id` = `id`
- [disambiguation.md](disambiguation.md) — clusters duplicate mastermusic rows representing the same real recording
