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
| `is_video`, `is_karaoke`, `is_audiobook` | Boolean flags, but **nullable, not just true/false** — some rows have `NULL` instead of `false` (confirmed for `is_video` 2026-08-25, e.g. mastermusic ids `73375392`/`95894977` under owner `1216`; same nullability applies to `is_karaoke`/`is_audiobook`). A filter written as `WHERE is_video = false` silently drops these rows (`NULL = false` → `NULL`, not true). To mean "flag is not set," use `WHERE (is_video = false OR is_video IS NULL)` or `WHERE coalesce(is_video, false) = false` — same pattern for the other two flags. **`is_karaoke = true` implies `is_video = true`** (karaoke tracks are a video subtype) — when checking for either karaoke or video content, check `is_karaoke` first/separately rather than assuming `is_video = false` rules out karaoke. |
| `asset_type` | Audio bitrate/quality tier: `505` = low (48 kbps), `506` = high (96 kbps), `522` = HQ (256 kbps). Integer, compared without quotes. "Does client X have HQ audio for all its labels?" means checking each label has **at least one** `asset_type = 522` track — `COUNT(CASE WHEN asset_type = 522 THEN 1 END) > 0` per `owner_id`, reached via [track_active.md](track_active.md) for the client's labels. |
| `rights` | Map keyed by territory code (e.g. `WW` = worldwide, `US` = United States), value struct includes `act` (1/0 active flag), `astr` (boolean, streaming allowed), `ssdt`/`sedt` (ISO-8601 rights start/end date strings). `ssdt` null = no lower bound (treat as min/-infinity time); `sedt` null = no upper bound (treat as max/+infinity time). "Currently active right" = `act = 1 AND astr = true AND (ssdt IS NULL OR ssdt <= now) AND (sedt IS NULL OR sedt >= now)` for at least one territory entry via `CROSS JOIN UNNEST(map_values(rights))`. **These are the rights available on the track itself** — catalogue-wide, with no store dimension. They are not any store's rights and must never be mixed with, or read as, store availability; for that see [track_active.md](track_active.md). Two further cautions: `sedt`/`sed` are unset on the overwhelming majority of entries, so the end-date test is effectively inert; and `WW` may be absent entirely, with worldwide grants appearing instead as many individual per-country entries — so `WW` alone is not a reliable test for worldwide rights. |

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
