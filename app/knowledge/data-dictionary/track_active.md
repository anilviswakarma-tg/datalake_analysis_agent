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
| `country` | Country the row applies to: an upper-case ISO 3166 two-letter code (`CH` = Switzerland, `DE` = Germany, `AE` = United Arab Emirates), or `WW` = worldwide; also `AN` (Netherlands Antilles, a retired code) and `XK` (Kosovo). Never null or lower case (all 1.24B rows checked 2026-10-06). **A worldwide track has only its `WW` row**, never per-country rows (true of every one of 28.9M `WW` tracks across 49 stores). So for a country question, **filter `country IN (<the countries>, 'WW')` and group by `country`**: each country's own tracks, and the worldwide ones as their own row. A worldwide row plus one country's row is that country's full catalogue; different countries' rows overlap, so never add them. A track has one row per country: count tracks with `COUNT(DISTINCT track_id)` (SPLH: 693,602 rows, 261,205 tracks). |
| `active` | Always `'Y'` — a track that isn't active for a store has no row. Checked 2026-09-26: all 1,244,031,758 rows across 61 groups. **Don't filter on it**; it removes nothing and only lengthens the query |
| `allow_stream` / `allow_sale` | Per-track permission flags for this store |
| `date_updated` | Last update timestamp |

**`track_active` has no `owner_id` column.** To get the owning label for a store's catalogue, you must join through `mastermusic`.

## Answering "what is store X's active catalogue size?"

Use this table **alone**. No join, no extra filtering:

```sql
SELECT COUNT(DISTINCT track_id) AS active_catalogue_size
FROM "tg-deltalake-bronze"."track_active"
WHERE group_id = '<store>'
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
  AND ta.allow_stream = 'Y'
```

Filter on `ta.country` when the question is territory-specific — a track can be active in one country and not another, so an unqualified count is the union across territories and will exceed any single country's figure. Include `'WW'` and group by `country`; see `country` above.

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

## Comparing a store's territories across releases of the same ISRC

*For a store with `WW` rows, a release carried worldwide shows as `WW`; treat it as every country rather than reporting the others as missing.*

"Rights on store" for a track means **the countries it is active in for that
store**: its `country` values here, as a set. It is **not**
`mastermusic.rights` (the track's own catalogue-wide rights, a nested map;
see [mastermusic.md](mastermusic.md)). One ISRC is often several track rows,
one per release, and a store can carry those releases in different
countries. To find ISRCs whose releases differ, build each track's sorted
country set, then compare the sets within each ISRC. Select only these few
columns: carrying `mastermusic.rights` along made one attempt's result 8 GB.
The UPC is on the parent album row (`album_id`), never on the track.

```sql
WITH store AS (
  SELECT track_id, array_sort(array_agg(DISTINCT upper(country))) AS t
  FROM "tg-deltalake-bronze"."track_active"
  WHERE group_id = '<store>' AND allow_stream = 'Y'
  GROUP BY track_id
), occurrences AS (
  SELECT mm.isrc, mm.title, mm.artist_name, al.upc, s.t
  FROM store s
  JOIN "tg-deltalake-bronze"."mastermusic" mm
    ON s.track_id = mm.id AND mm.dw_stock_type = 'track'
  LEFT JOIN "tg-deltalake-bronze"."mastermusic" al
    ON al.id = mm.album_id AND al.dw_stock_type = 'album'
), inconsistent AS (          -- ISRCs on 2+ releases whose country sets differ
  SELECT isrc, count(*) AS n
  FROM occurrences
  GROUP BY isrc
  HAVING count(*) > 1 AND count(DISTINCT array_join(t, '|')) > 1
), territory_counts AS (
  SELECT o.isrc, c, count(*) AS k
  FROM occurrences o
  JOIN inconsistent i ON o.isrc = i.isrc
  CROSS JOIN UNNEST(o.t) AS u(c)
  GROUP BY o.isrc, c
), differing AS (             -- countries on some of an ISRC's releases, not all
  SELECT tc.isrc, array_sort(array_agg(tc.c)) AS terrs
  FROM territory_counts tc
  JOIN inconsistent i ON tc.isrc = i.isrc
  WHERE tc.k < i.n
  GROUP BY tc.isrc
)
SELECT o.title, o.artist_name AS artist, o.upc, o.isrc,
       array_join(o.t, '|') AS rights_on_store,
       array_join(concat(
         transform(array_except(d.terrs, o.t), c -> c || ' missing'),
         transform(array_intersect(d.terrs, o.t), c -> c || ' present')), ', ') AS rights_difference
FROM occurrences o
JOIN differing d ON o.isrc = d.isrc
ORDER BY o.isrc, rights_on_store
```

Measured on SPLH (2026-10-05): 11,164 ISRCs across 36,225 rows, 26 GB
scanned. `GBAYE0702916` (2 Hearts) comes back as three `AT|CH|DE` rows and
one `AT|DE` row (`CH missing`). For summaries (how many ISRCs, which
countries differ), wrap this query in an aggregate rather than re-running
variants of it.

## Related docs

- [mastermusic.md](mastermusic.md) — the catalogue table `track_active` joins against
- [musicowners.md](musicowners.md) — the direct group→owner assignment table; prefer it over this one for "which owners does group X have"
- [disambiguation.md](disambiguation.md) — "Finding the disambiguation set for a store" uses this same `track_active` join pattern
