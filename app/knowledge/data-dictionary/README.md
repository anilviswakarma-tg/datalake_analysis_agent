# Data Dictionary

**Version: 3**

Plain-language reference for anyone (human or AI agent) querying these tables. Scope is strictly the **meaning** of what's in each table/column — not how it's ingested or produced.

## Tables

| Table | Layer | Doc |
|---|---|---|
| `mastermusic` | Bronze | [mastermusic.md](mastermusic.md) |
| `track_active` | Bronze | [track_active.md](track_active.md) |
| `playactivity_v2` | Bronze | [playactivity.md](playactivity.md) |
| `users` | Bronze | [users.md](users.md) |
| `playlists`, `radiostations` | Bronze | [playlists-radiostations.md](playlists-radiostations.md) |
| `devices`, `user_devices` | Bronze | [devices.md](devices.md) |
| `musicowners`, `musicowners_groups` | Master | [musicowners.md](musicowners.md) |
| `subscriptions`, `package_cost`, `package_cost_text`, `subscriptions_meta` | Bronze/Silver | [subscriptions.md](subscriptions.md) — `package_cost`/`package_cost_text` aren't queried directly, use `subscriptions_meta` |
| `groups` | Master | [groups.md](groups.md) |
| `disambiguation_v1` | Silver | [disambiguation.md](disambiguation.md) |
| `music_streams_v3` | Silver | [music_streams_v3.md](music_streams_v3.md) — preferred over `playactivity_v2` for stream questions |
| `music_fetches` | Silver | [music_fetches.md](music_fetches.md) — preferred over `playactivity_v2` for fetch questions |

**Not started:** `tg-configs.services`, `tg-master.export_submit`/`export_delta`, `reportlabel_parentchild`.

**Out of scope:** `label_streams`, `daily_catalog_status_snapshot_v2` — single-purpose reporting-pipeline outputs, not general-purpose tables. Pipeline-only tables (`webhooklog`, `live_radio_cf_log`, `owners_groups`, etc.) are also out of scope.

## Editing this dictionary

Not sent to the agent: it receives this file from Shared vocabulary on.

**Describe data only — no ingestion job/class names, trigger/workflow names, or code paths.** Don't reference job/class names (e.g. `PlayActivityCsvInsert`), Glue trigger/workflow names (e.g. `on_montly_reports_start`), or source file paths (e.g. `adhoc/manual_playactivity_upsert/`) — implementation detail belongs in code/commit history, not here. If an implementation fact is the *evidence* for a data-behavior claim (e.g. a refresh cadence), keep the claim and drop the specific identifier that backs it.

## Shared vocabulary

| Concept | Definition |
|---|---|
| `group_id` | Store/client. Resolve business names ("Etisalat", "Gabb") via `groups.name` first — see [groups.md](groups.md). |
| `owner_id` | Owning label on `mastermusic`. Live/mutable. Resolve name via `musicowners.id`. |
| `stock_code_id` | FK from event tables → `mastermusic.id`. |
| `dw_stock_type` | `mastermusic` discriminator: `'track'`/`'album'`. |
| `disambiguation_id` | Cluster ID in `disambiguation_v1`. |
| `sub_sku` | = `package_cost.cost_id`. Resolve name via `subscriptions_meta`. |

## The central catalogue and a store's data

Two different things are both called "the catalogue":

| | Central catalogue | A store's catalogue |
|---|---|---|
| What | Everything Tuned Global has ingested from labels, whoever sells it | What one store actually carries |
| Table | `mastermusic` (one row per track or album) | `track_active` (one row per track **per country** per store) |
| Keys | `id`, `owner_id` (the label) | `group_id` (the store), `track_id`, `country` |
| "Rights" means | `mastermusic.rights`: what the label granted, catalogue-wide | The countries the store carries the track in: `track_active.country` |

- **Store = client = group = platform:** one of Tuned Global's customers,
  e.g. Etisalat (`ETEG`), Spafax Lufthansa (`SPLH`). Always identified by
  `group_id`; `groups` lists them.
- **Any table with a `group_id` column holds store (client) data:**
  `track_active`, `music_streams_v3`, `music_fetches`, `playactivity_v2`,
  `users`, `subscriptions`, `subscriptions_meta`, `playlists`,
  `radiostations`, `devices`, `user_devices`, `musicowners_groups`, and
  `groups` (the list of stores itself).
  **A table without one is central, shared by every store:** `mastermusic`,
  `musicowners`, `disambiguation_v1`. A question that names a store needs a
  table with `group_id`.
- **A store carries a track per country (ISO code) or worldwide (`WW`):** a
  worldwide track has only its `WW` row, so a country question groups by
  `country`, with `WW` as its own row. Count tracks with
  `COUNT(DISTINCT track_id)`. See [track_active.md](track_active.md).

## Rules that apply across every table here

**Resolve business names to IDs before querying — fail if unresolved.** No table is keyed on a business name. Look it up (`groups.name` → `group_id`, `musicowners.name` → `owner_id`, `subscriptions_meta.sub_name` → `sub_sku`); if it doesn't resolve to exactly one row, stop and ask — don't guess or fuzzy-match.

**Joins and aggregations: IDs only, never names.** Names aren't unique or stable (same owner can have multiple `owner_id`s with the same name; names get renamed/reused). A name-based join/`GROUP BY` silently merges or splits rows with no error. Join/group by ID, resolve to a name only at the very end, for display.

**Store-scoped questions must join to a store-identifying table.** Most catalogue tables — `mastermusic` above all — are central and carry no store dimension; filtering them by `owner_id`, rights or status answers "what exists", not "what does this store have". Route any "what can store X stream/carry/see" question through `track_active` (or another table holding `group_id`) and count the intersection. Two plausible-looking totals are not evidence of overlap — join and measure it.

**Use resolved silver lookup tables, not their raw key-value sources.** E.g. `subscriptions_meta`, not `package_cost_text` directly — the latter is multi-row per ID (one per language) and fans out silently on a naive join.
