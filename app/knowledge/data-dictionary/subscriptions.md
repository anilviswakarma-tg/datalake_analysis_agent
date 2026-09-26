# subscriptions, package_cost_text, subscriptions_meta

Subscription records and their names. See [README.md](README.md) for shared vocabulary and cross-table rules.

**Always resolve subscription info via `subscriptions_meta` — don't query `package_cost`/`package_cost_text` directly.** `package_cost_text` has multiple rows per `package_cost_id` (one per language/field); a direct join fans out and silently duplicates rows. `subscriptions_meta` has already resolved that to one row per `(sub_id, group_id)`, plus carries `country`/`currency` from `package_cost`.

## subscriptions (bronze)

**Table:** `tg-deltalake-bronze.subscriptions`, partitioned by `group_id`. PK: `id`. Dual-sourced (DMS + DynamoDB CDC). **Refreshed daily**; `subscriptions_meta` rebuilds daily right after, same day's data available same day.

| Column | Meaning |
|---|---|
| `id`, `group_id`, `user_id` | Row key, store/client, FK → `users.user_id`. |
| `package_cost_id` | FK → `package_cost.cost_id`. **This is what `sub_sku` on event tables matches**, not `package_id`. |
| `status`, `enabled`, `start_date`, `end_date` | Subscription state/period. |

## package_cost_text (bronze)

Key-value table, one row per `(package_cost_id, field_name, language)` — `field_name` in `'NAME'`/`'INTERNALNAME'`. Not for direct querying — see rule above.

## subscriptions_meta (silver)

**Table:** `tg-deltalake-silver.subscriptions_meta`, built from `package_cost` + `package_cost_text`. One row per `(sub_id, group_id)` — the id→name lookup for `sub_sku`, same role [musicowners.md](musicowners.md) plays for `owner_id`.

| Column | Meaning |
|---|---|
| `sub_id` | = `package_cost.cost_id`, what `sub_sku` joins against. |
| `sub_name`, `sub_internal_name` | Display names (English preferred, falls back if none). |

```sql
SELECT s.sub_sku, sm.sub_name
FROM "tg-deltalake-silver".music_streams_v3 s
LEFT JOIN "tg-deltalake-silver".subscriptions_meta sm
  ON sm.sub_id = s.sub_sku AND sm.group_id = s.group_id
```

Use `LEFT JOIN` — an old `sub_sku` may not resolve in the current table.

## Caveats

- Never join or `GROUP BY` on `sub_name`/`sub_internal_name` — resolve last, for display only.
- `subscriptions` (who's subscribed) and `subscriptions_meta` (what the package is called) answer different questions — don't conflate them.

## Related docs

- [users.md](users.md), [musicowners.md](musicowners.md), [groups.md](groups.md)
