# users

DMS-sourced bronze table, `tg-deltalake-bronze.users`. PK `user_id`. Partitioned by `group_id`. **Refreshed daily.** See [README.md](README.md) for shared vocabulary and cross-table rules.

FK target from `playactivity_v2.user_id`, `playlists.user_id`, `radiostations.user_id`, `user_devices.user_id`.

| Column | Meaning |
|---|---|
| `member_id` | External identifier — not the same as `user_id`. |
| `test_user` | Filter `<> 'Y'` for analyst-facing metrics. |
| `last_track_id`, `last_playlist_id`, `last_radiostation_id` | IDs, safe to join on, but latest-value only (no history). |
| `primary_user_id` | Links a sub-profile to its parent account. |
| `group_id` | Store/client. |
| `home_country`, `language` | Locale fields. |
| `date_created`, `date_updated` | Lifecycle timestamps. |

## Related docs

- [playactivity.md](playactivity.md), [devices.md](devices.md), [musicowners.md](musicowners.md)
