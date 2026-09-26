# devices, user_devices

DMS-sourced bronze tables. **Both refreshed daily.** See [README.md](README.md) for shared vocabulary and cross-table rules.

## devices

`tg-deltalake-bronze.devices`. PK `device_id`. Joins to `playactivity_v2.device_id`.

| Column | Meaning |
|---|---|
| `device_type_id` | Device category. |
| `os` | Operating system. |
| `carrier` | Mobile carrier, where applicable. |
| `first_app_version`, `last_app_version` | App version at first/last seen. |
| `group_id` | Store/client. |

## user_devices

`tg-deltalake-bronze.user_devices`. PK `id`. Join table: `user_id` → `users.user_id`, `device_id` → `devices.device_id`.

| Column | Meaning |
|---|---|
| `last_ip_address`, `last_location` | Last known location. |
| `is_obsolete` | Device no longer in active use. |

## Related docs

- [playactivity.md](playactivity.md), [users.md](users.md)
