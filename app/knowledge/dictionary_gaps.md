# Data dictionary — gap review

> ⚠️ **HISTORICAL — `domain_rules.md` was retired on 2026-09-24.**
> Every line reference below points into a file that no longer exists. Its data
> content was merged into `data-dictionary/`; its agent-behaviour content moved
> into `prompt.py`. Keep this only for the unresolved **CONFLICT** items — in
> particular the wrong partition-key claim and the partition-pruning assertion
> that affects Athena spend. Once those are fixed upstream in
> `reporting-deltalake`, delete this file.


**Status:** awaiting review. Nothing here has been promoted yet.

The agent now treats `knowledge/data-dictionary/` (vendored from
`reporting-deltalake/docs/data-dictionary`, commit `20b253f`, 2026-09-23) as the
authority on what tables and columns mean. This file lists everything
`domain_rules.md` knows that the dictionary does **not**, so it can be reviewed
and — where it's genuinely data meaning — promoted upstream into
`reporting-deltalake`.

Each item says what `domain_rules.md` asserts, where, and a suggested action.
Line references are into `knowledge/domain_rules.md`.

**Three buckets:**

- **PROMOTE** — data meaning the dictionary is missing. Belongs upstream.
- **CONFLICT** — the two sources disagree. Needs a decision before either is trusted.
- **KEEP LOCAL** — agent behaviour or SQL recipes. The dictionary deliberately
  excludes these (see its README: "describe data only"), so they stay in
  `domain_rules.md`. Listed so nobody promotes them by mistake.

---

## CONFLICT — resolve these first

### C1. `mastermusic` partition keys — RESOLVED, and the dictionary is wrong

**Settled against Glue (2026-09-23).** `mastermusic` is partitioned by:

    owner_id_salt, dw_stock_type

`owner_id` is an ordinary column, **not** a partition key.

| Source | Claim | Verdict |
|---|---|---|
| `domain_rules.md:99` | `owner_id_salt` + `dw_stock_type` | ✅ correct |
| `domain_rules.md:1565` | `dw_stock_type, owner_id, salt` | ❌ wrong |
| `data-dictionary/mastermusic.md:7` | `dw_stock_type, owner_id, salt` | ❌ **wrong — fix upstream** |
| `data-dictionary/mastermusic.md:15` | "`owner_id` ... Partition column" | ❌ **wrong — fix upstream** |

**This one matters for money, not just tidiness.** `domain_rules.md:316` tells
the agent to prefer `owner_id` because "you want partition pruning benefits".
That is false: filtering `owner_id` prunes nothing, because the partition is
`owner_id_salt`. Measured consequence — a real agent query from 2026-09-23
filtered on `stock_code` (also not a partition key) and scanned **15.78 GB** of
`mastermusic` despite containing a `LIMIT 1`.

Actions:
1. Correct `mastermusic.md` upstream in `reporting-deltalake` — this is the
   first confirmed case of the dictionary being wrong, and per the precedence
   rule (Glue wins on what exists), it must be fixed there.
2. Fix `domain_rules.md:1565` and remove the false pruning claim at line 316.
3. Document how to actually prune `mastermusic`, since `owner_id_salt` is
   derived — the agent currently has no way to know.

For contrast, `track_active` **is** partitioned by `group_id_salt`, `group_id`,
so the mandated `group_id` filter there does prune correctly.

### C2. `dw_stock_type` on `track_active` (line 614)

`domain_rules.md` has a worked example selecting and grouping by
`dw_stock_type` **from `track_active`**:

```sql
SELECT dw_stock_type, COUNT(*) AS total
FROM "tg-deltalake-bronze"."track_active"
WHERE group_id = 'GMTH'
GROUP BY dw_stock_type
```

`track_active.md` lists no such column — its schema is `id`, `group_id`,
`track_id`, `country`, `active`, `allow_stream`, `allow_sale`, `date_updated`.
Either the dictionary's schema is incomplete or this example is broken.

**Action:** verify with `describe_table`. If the column doesn't exist, delete the
example; if it does, add it to `track_active.md`.

### C3. `play_action = 2` (line 1027 area, column reference table)

`domain_rules.md` states `play_action`: `1 = start, 2 = progress, 3 = EOF, 4 = skip`.
`playactivity.md` says `1` = play start, `3` = full stream/complete, `4` = skip,
and explicitly: "Other codes exist but aren't confirmed."

The local file asserts `2 = progress` as fact. If that's been confirmed, the
dictionary should say so and drop its hedge.

**Action:** confirm, then promote `2 = progress` upstream.

### C4. `disambiguation_v1` match types — local copy is stale

`domain_rules.md` (lines ~636–726) lists four match types: `self`, `md5_96k`,
`md5_320k`, `isrc_metadata`, `metadata_only`. `disambiguation.md` documents a
fifth — `artist_variant` (confidence 95, artist spelling variants within
similarity ≥ 0.85 and ≤ 2 edits), plus normalisation rules (`&` vs `and`,
punctuation, case) and the rule that non-identifying titles (`intro`, `outro`,
`interlude`, `skit`) never match through that path.

The dictionary is newer here. No promotion needed — this is a case for trimming
the local copy so it can't go on contradicting.

**Action:** dictionary wins. Trim the duplicated section in `domain_rules.md`.

---

## PROMOTE — data meaning missing from the dictionary

### mastermusic

**P1. `status` column and the `status = 1` rule** (line 107)
> "**Always** `status = 1` (active records only)"

`mastermusic.md` never mentions `status` at all. This is the single most-used
filter in the catalogue domain — the dictionary is missing the column that
separates live catalogue from deleted/inactive rows. Highest-value gap in this file.

**P2. `asset_type` — audio quality tiers** (lines 220–228)

| `asset_type` | Quality | Bitrate |
|---|---|---|
| `505` | Low | 48 kbps |
| `506` | High | 96 kbps |
| `522` | HQ | 256 kbps |

Not in the dictionary. Pure column semantics — belongs there.

**P3. `sub_label_name` / `sub_label_id` — imprint vs distributor** (lines 496–533, gotcha #11 line ~1610)

`owner_name` is the distributor/aggregator ("Believe SAS", "CD Baby",
"FreshTunes"); `sub_label_name` is the actual label imprint ("Koala Music",
"Zvonko Digital"). Searching `owner_name` for an imprint misses it entirely.
Neither column appears in `mastermusic.md`.

**P4. `owner_name` exists on `mastermusic`** (lines 290–348)

The dictionary documents `owner_id` but never says `owner_name` is denormalised
onto the table. Worth stating along with the warning that multiple distinct
`owner_id`s can share one `owner_name`, so it is display-only — consistent with
the README's existing "join on IDs, never names" rule.

**P5. Date columns are STRINGS** (lines 350–364)
- `release_date` — STRING. `LIKE '2024-%'` for year, or `date_parse(release_date, '%Y-%m-%d')`.
- `datetime_added` — STRING in ISO 8601. Needs `from_iso8601_timestamp(...)`.

Not in the dictionary. Type traps of exactly the kind it exists to record.

**P6. `artist_name`, `title` columns**

Used throughout `domain_rules.md` for artist/title search; absent from
`mastermusic.md`'s column list.

### playactivity_v2 / music_streams_v3

**P7. `api_type` values** (lines 963, 1031)
`1` = aAPI (Advanced API), `2` = CDS. The dictionary mentions `api_type` only in
`music_fetches.md`, and only to note the per-group override — never what the
values mean.

**P8. `play_action_value` semantics** (line ~1030)
Seconds elapsed; `0` at start, `5`/`30` at progress, full duration at end. This
is what the 30-second stream rule is built on, so it should be documented
alongside `is_stream`.

**P9. `play_type` value set** (line ~1029)
`'Stream'` / `'Download'` / `'Preview'`. `playactivity.md` references
`play_type = 'preview'` exclusion but never lists the values — note also the
case difference (`'Preview'` locally vs `'preview'` in the dictionary), which is
worth pinning down since Trino string comparison is case-sensitive.

**P10. `report_datetime` vs `play_datetime`** (line ~1039)
When reported to the API vs when the play actually happened. A real
two-timestamp distinction and not recorded anywhere in the dictionary.

**P11. `user_id` / `device_id` = `0` means anonymous** (lines ~1034–1035)
Not documented. A `COUNT(DISTINCT user_id)` that doesn't exclude `0` silently
counts "anonymous" as one user.

**P12. `is_play` flag on `music_streams_v3`** (line 785)
`music_streams_v3.md` documents `is_stream`, `is_play_start`, `is_full_stream`,
`is_skip` — but not `is_play` ("1 if any play action"), which the skip-rate
pattern depends on.

**P13. `audio_quality` column on `music_streams_v3`** (line 797)
Listed locally as a key column; absent from the dictionary.

**P14. `is_offline` column** (line ~1028)
Offline download flag on `playactivity_v2`. Not documented.

### store domain — the largest cluster of gaps

**P15. `'Y'` / `'N'` string flags, not booleans** (line 1264)

| Table | Column | Active | Inactive |
|---|---|---|---|
| `subscriptions` | `enabled` | `'Y'` | `'N'` |
| `playlists` | `enabled` | `'Y'` | `'N'` |
| `playlists` | `deleted` | `'Y'` (is deleted) | `'N'` |
| `users` | `active` | `'Y'` | `'N'` |
| `users` | `test_user` | `'Y'` (is test) | `'N'` |

The dictionary only ever mentions `test_user <> 'Y'`. It never states that these
are `'Y'`/`'N'` strings across the board — and `users.active`, `playlists.enabled`,
`playlists.deleted` and `devices.enabled` aren't documented as columns at all
(`users.md`, `playlists-radiostations.md`, `devices.md`). Writing
`WHERE enabled = true` against any of them fails or returns nothing.

**P16. `playlists.type` values** (line 1290)
`User` / `System` / `SystemDailyDiscovery`. "How many user playlists" is a
common question and needs `type = 'User'`; `playlists-radiostations.md` doesn't
list the column.

**P17. `subscriptions.status` is a trap** (lines 1304–1312)
> "`status` — exists, but do **not** filter `status = 'active'` — use
> `enabled = 'Y'` and `end_date >= current_date` instead"

`subscriptions.md` lists `status` as an ordinary column with no such warning.

**P18. `date_created` on store tables** (lines 1274–1284)
`playlists`, `subscriptions`, `users`, `devices`, `user_devices` all carry
`date_created`. Only `users.md` documents it. It's the column every
"added/created/new in period" question depends on.

**P19. "Current state" vs "added in period" is a data distinction** (lines 1251–1262)

A row's status flags describe **now**, not the period asked about. A
subscription created last week that has since expired is invisible to
`enabled = 'Y'`, so "how many were added last week" must drop the status
filters and filter `date_created` instead.

The SQL recipe is local business, but the underlying fact — *these tables hold
current state with no history* — is data meaning and is worth a line in the
dictionary next to each table's refresh cadence.

**P20. `devices.enabled`** (line 1280)
`devices.md` lists `device_type_id`, `os`, `carrier`, app versions, `group_id` —
no `enabled`, which `domain_rules.md` requires for current-device counts.

---

## KEEP LOCAL — do not promote

These are agent behaviour or SQL recipes. The dictionary's README explicitly
scopes it to data meaning only, so these stay in `domain_rules.md`:

- **Domain classification** (catalogue / playlog / store) — an agent routing concept.
- **Default date range** = month-to-date + the `note_default_applied(...)` call (lines 757–770).
- **Reporting rules** — always `SELECT` the name beside the id, sort by the
  measure `DESC`, `LIMIT 100` on aggregations, put the human-readable column
  first so auto-charting picks it up (lines 535–568). These exist to serve the
  Streamlit UI.
- **`resolve_label` / `resolve_client` behaviour** — ambiguity handling, the
  15-minute cache, the "never resolve an artist name" rule (lines 1503–1556).
  Tool contract, not data.
- **All worked SQL example blocks.**
- **Gotcha #10 — never fabricate numbers when a query returns 0 rows** (line ~1600).
  Pure agent-safety instruction; arguably the most important line in the file.
- **Gotcha #9 — `webhooklog` does not exist** (line ~1598). The dictionary
  already lists `webhooklog` as out of scope; the local version is an
  anti-hallucination guard and should stay.
- **`EXECUTE_LIVE` / dry-run, LIMIT policy, chart guidance.**

---

## Worth knowing: where the dictionary is already ahead

Not gaps — reasons the swap is worth making. `domain_rules.md` lacks all of these:

1. **`track_active` is the answer, not an input.** Availability rules are
   already resolved into its rows; layering `mastermusic.rights` on top is
   wrong and changes results by a fraction of a percent while implying
   precision that isn't there.
2. **A label's catalogue size says nothing about what a store carries.** One
   measured case: both ~3.6M, actual overlap under 5%.
3. **`mastermusic` is live and mutable with no point-in-time snapshot** — the
   same `track_id` can move between owners, so re-running a past report drifts.
4. **`rights`: `WW` is not a reliable worldwide test** (worldwide grants often
   appear as many per-country entries), and `sedt` is unset on the overwhelming
   majority of rows, making the end-date test effectively inert.
5. **`music_streams_v3` non-flag columns** (`ip_address`, `country`,
   `device_id`, …) come from the single raw row with the highest
   `play_action_value` — not an aggregate.
6. **`music_fetches` has no `FLUS`/`FLSP` correction job**, unlike
   `music_streams_v3`.
7. **`musicowners_groups.reporting_parent_id`** — don't assume single-level hierarchy.
8. **Lookup tables refresh monthly with no history** — always `LEFT JOIN` into
   them so retired IDs don't silently drop rows.

---

## Suggested order of work

1. Settle **C1–C4**. Conflicts make both files untrustworthy until resolved.
2. Promote **P1** (`status = 1`) and **P15** (`'Y'`/`'N'` flags). Between them
   they cover most everyday queries, and both cause silently wrong counts
   rather than errors.
3. Promote the rest of the store gaps (**P16–P20**) — that table set is the
   thinnest part of the dictionary.
4. Promote the mastermusic column gaps (**P2–P6**).
5. Promote the playactivity gaps (**P7–P14**).
6. Once a section is upstream, delete the duplicate from `domain_rules.md` and
   leave a pointer, so it can't drift again.
