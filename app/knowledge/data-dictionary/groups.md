# groups

Resolves `group_id` (store/client identifier) to its business name — e.g. "Etisalat", "Gabb". See [README.md](README.md) for shared vocabulary and cross-table rules.

**Table:** `tg-master.groups`. Full overwrite, no history — same pattern as [musicowners.md](musicowners.md). PK is `id`; expect one row per `group_id` in practice. **Refreshed monthly, not daily** — runs ~1st of each month.

## Key columns

| Column | Meaning |
|---|---|
| `group_id` | The `group_id` used everywhere else in this doc set. |
| `name` | Business/brand name. Display only. |
| `country`, `hfa` | Group attributes. |

## Resolve business names before querying

Requests name clients by business name ("Etisalat", "Gabb"), but every table is keyed on `group_id`, never the name.

```sql
SELECT group_id, name FROM "tg-master".groups WHERE trim(lower(name)) = lower('Etisalat')
```

**If this doesn't resolve to exactly one row, stop — don't guess or fuzzy-match.** Ask for clarification instead. Same discipline applies to `owner_id` ([musicowners.md](musicowners.md)) and `sub_sku` ([subscriptions.md](subscriptions.md)).

Never join or `GROUP BY` on `name` — resolve to `group_id` first, use the name only for display.

## Related docs

- [musicowners.md](musicowners.md) — same name→id pattern for `owner_id`; also where to find which owners are assigned to a `group_id`
