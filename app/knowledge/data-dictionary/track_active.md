# track_active

Records which tracks are currently active for which store — `(track_id, group_id)` pairs. See [README.md](README.md) for shared vocabulary and cross-table rules.

**This table is the answer, not an input.** It holds the *result* of the platform's availability rules after they have been applied — rights grants, territory, activation and the rest are already resolved into these rows. For any question of the form "what can store X stream / carry / see", this table is authoritative, and no further rights filtering is needed or correct.

**Table:** `tg-deltalake-bronze.track_active`. Large — 1.1B+ rows. Always filter by `group_id` before joining; don't scan unfiltered. **Refreshed hourly** (CDC-based upsert).

## Schema

| Column | Meaning |
|---|---|
| `id` | Row key |
| `group_id` | Identifies the store/client |
| `track_id` | FK → `mastermusic.id` |
| `country` | Country the row applies to |
| `active` | Whether the track is currently active for this store. String type, not boolean — for ETEG, only value observed is `'Y'` (no `'N'`/inactive rows seen); verify before assuming this holds for other groups or before comparing with `=`/`CASE WHEN` as a boolean |
| `allow_stream` / `allow_sale` | Per-track permission flags for this store |
| `date_updated` | Last update timestamp |

**`track_active` has no `owner_id` column.** To get the owning label for a store's catalogue, you must join through `mastermusic`.

## Answering "what is store X's active catalogue size?"

Use this table **alone**. No join, no extra filtering:

```sql
SELECT COUNT(DISTINCT track_id) AS active_catalogue_size
FROM "tg-deltalake-bronze"."track_active"
WHERE group_id = '<store>' AND active = 'Y'
```

**Never join `mastermusic` to apply `status = 1`.** That rule is scoped to
`mastermusic` itself and is not a reason to bring the table in. Measured on
SHUS (2026-09-24): this query returns 4,206,570; adding
`JOIN mastermusic ... AND mm.status = 1` returns 4,206,438. The 132 missing
tracks are an artifact, not a correction — `track_active` refreshes **hourly**
and `mastermusic` **daily**, so an inner join silently drops tracks activated
since the last `mastermusic` refresh. The join also scanned 1.65 GB against
0.03 GB — 55x the cost for a worse answer.

**The only reason to join `mastermusic` is METADATA, never filtering.** Join it
when the question needs something that lives there and nowhere else — artist
name, title, the owning label — i.e. when you need to *describe* the tracks,
not to decide which ones count. Even then, do not add `mm.status = 1` to the
WHERE clause: it re-filters what this table already decided and drops the same
rows.

## Answering "what does store X carry from label Y?"

```sql
SELECT count(DISTINCT ta.track_id)
FROM "tg-deltalake-bronze".track_active ta
JOIN "tg-deltalake-bronze".mastermusic mm
  ON ta.track_id = mm.id AND mm.dw_stock_type = 'track'
WHERE ta.group_id = '<group>'
  AND mm.owner_id = '<owner>'
  AND ta.active = 'Y' AND ta.allow_stream = 'Y'
```

Add `ta.country` when the question is territory-specific — a track can be active in one country and not another, so an unqualified count is the union across territories and will exceed any single country's figure.

**Do not additionally filter on `mastermusic.rights`.** That map holds the rights available on the track itself, catalogue-wide — a different thing from what a store carries, already accounted for in these rows. Layering it on top changes results by a fraction of a percent and can only mislead. See [mastermusic.md](mastermusic.md).

## Getting a group's catalogue content, by owner

```sql
SELECT ta.group_id, mm.owner_id, ta.track_id
FROM "tg-deltalake-bronze".track_active ta
JOIN "tg-deltalake-bronze".mastermusic mm ON ta.track_id = mm.id
WHERE ta.group_id = <group_id>
```

**Always join and `GROUP BY` on `owner_id`** — pulled in via this join, since it isn't present on `track_active` directly — **never on owner/label name.** Names aren't unique or stable across rows for the same underlying owner.

If the question is just "which owners are assigned to this group" (not track-level detail), use `musicowners_groups` directly instead — see [musicowners.md](musicowners.md).

## Related docs

- [mastermusic.md](mastermusic.md) — the catalogue table `track_active` joins against
- [musicowners.md](musicowners.md) — the direct group→owner assignment table; prefer it over this one for "which owners does group X have"
- [disambiguation.md](disambiguation.md) — "Finding the disambiguation set for a store" uses this same `track_active` join pattern
