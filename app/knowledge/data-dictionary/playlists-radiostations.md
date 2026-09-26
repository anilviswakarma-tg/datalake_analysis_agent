# playlists, radiostations

DMS-sourced bronze tables. See [README.md](README.md) for shared vocabulary and cross-table rules.

## playlists

`tg-deltalake-bronze.playlists`. PK `playlist_id` — this is `playactivity_v2.list_id` where `list_type = 'P'`. **Refreshed hourly.**

| Column | Meaning |
|---|---|
| `user_id` | Creator — FK → `users.user_id`. |
| `station_id` | Associated station, if any — FK → `radiostations.station_id`. |
| `is_public`, `is_video`, `is_draft`, `is_explicit` | Content flags. |
| `content_language`, `content_tier` | Classification. |

No `name`/`title` column is ingested here.

## radiostations

`tg-deltalake-bronze.radiostations`. PK `station_id` — this is `playactivity_v2.list_id` where `list_type = 'R'`. **Refreshed daily.**

| Column | Meaning |
|---|---|
| `user_id` | Creator, where applicable. |
| `station_name` | Display only — never join/group on it, use `station_id`. |
| `station_type`, `country`, `content_tier` | Classification. |
| `shared`, `enabled`, `deleted` | Status flags. |

## Related docs

- [playactivity.md](playactivity.md), [users.md](users.md)
