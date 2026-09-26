# musicowners / musicowners_groups

Owner/label reference tables — names behind `owner_id`, and which `owner_id`(s) a `group_id` carries. See [README.md](README.md) for shared vocabulary and cross-table rules.

**Tables:** `tg-master.musicowners`, `tg-master.musicowners_groups`. Full overwrite, no history. **Refreshed monthly, not daily** — runs ~1st of each month.

## musicowners

| Column | Meaning |
|---|---|
| `id` | Primary key = `mastermusic.owner_id`. |
| `name`, `display_name` | Display only — never join/group on these. |
| `active`, `is_aggregator`/`aggregator_id` | Owner status/aggregation flags. |

## musicowners_groups

The authoritative "which owners does this group carry" mapping — prefer this over deriving it from `track_active`/`mastermusic` (which only shows currently-active tracks, not the commercial relationship).

| Column | Meaning |
|---|---|
| `owner_id` | FK → `musicowners.id`. The persisted column name is `owner_id`, not `owner`. |
| `group_id` | Store/client. |
| `reporting_parent_id` | Parent-owner FK for label hierarchy — not fully documented, don't assume single-level. |

## Common query pattern — owners for a group

```sql
SELECT mog.owner_id, mo.name
FROM "tg-master".musicowners_groups mog
JOIN "tg-master".musicowners mo ON mo.id = mog.owner_id
WHERE mog.group_id = '<group_id>'
```

For a track's owner within a group's catalogue, join `mastermusic.owner_id` (cast to integer — type mismatch across tables) → `musicowners_groups.owner_id`.

## Caveats

- Full monthly overwrite — a mid-month owner/group change won't appear until the next refresh, and no history is kept.
- Use `LEFT JOIN` from `mastermusic`/event tables into `musicowners` — an old `owner_id` may not exist in today's dump; inner join silently drops those rows.
- Never join or `GROUP BY` on `name`/`display_name` — group by `owner_id`, resolve name last for display.

## Related docs

- [mastermusic.md](mastermusic.md), [track_active.md](track_active.md), [groups.md](groups.md)
