# Dictionary changes pending upstream

`data-dictionary/` is a vendored copy of `reporting-deltalake/docs/data-dictionary`
(commit `20b253f`, 2026-09-23). The files below have been edited **here** and
not upstream. **A refresh from upstream deletes every change listed, silently.**
Until they land upstream, copy only the unaffected files, or re-apply these
edits after the refresh. The tests in `tests/test_wiring.py` catch the loss of
the store-catalogue, `active` and stock-code rules, but not the rest.

To see the exact current differences:

```bash
for f in app/knowledge/data-dictionary/*.md; do
  diff -u ../reporting-deltalake/docs/data-dictionary/$(basename $f) $f
done
```

Once a change is merged upstream and re-vendored, delete its entry. Delete this
file when it is empty.

## `README.md`

12. **New section: "The central catalogue and a store's data"** — central
    catalogue (`mastermusic`) vs a store's catalogue (`track_active`, per
    country); store = client = group = platform, always `group_id`; any table
    with a `group_id` column is store data, one without is central (checked
    against Glue for every documented table, 2026-10-06); a one-line pointer
    to `track_active.md` for country codes and `COUNT(DISTINCT track_id)`. In
    the shared part, so the agent receives it with every dictionary lookup.
    Not upstream at all. Also: the "Describe data only" rule moved up into a
    new "Editing this dictionary" section, above Shared vocabulary, so the
    agent no longer receives it (it is for editors).
13. **Country codes documented** (in `track_active.md` `country`, with the
    SPLH 693,602 rows / 261,205 tracks example, and `mastermusic.md`
    `rights`; the README only points there): upper-case ISO 3166 two-letter codes plus `WW`
    (worldwide), `AN` and `XK`; a worldwide track has only its `WW` row, so a
    country question is `country IN ('<code>', 'WW')` (ETEG: 7.1M worldwide
    tracks); two free-text `rights` keys flagged as data errors. Checked
    2026-10-06 against all 1.24B `track_active` rows and every active
    track's `rights`.

## `track_active.md`

1. **`active` is always `'Y'` — stop filtering on it.** Every row checked on
   2026-09-26 (1,244,031,758 rows across 61 groups) had `active = 'Y'`; a track
   that isn't active for a store has no row. Removed `active = 'Y'` from both
   example queries and rewrote the column note, which had said only ETEG was
   verified. Found when the agent copied the filter into every query.
   Upstream has it in the "what does store X carry from label Y?" example.
   *(Related, not changed: `allow_stream` is `'N'` on exactly 1 row of the 1.24B,
   so that filter is also close to a no-op.)*
2. **New section: "Answering 'what is store X's active catalogue size?'"** —
   use `track_active` alone; never join `mastermusic` to apply `status = 1`;
   join `mastermusic` only for metadata. Carries the SHUS measurement
   (4,206,570 vs 4,206,438; 0.03 GB vs 1.65 GB scanned). Written 2026-09-24
   after the agent gave a wrong SHUS catalogue size. Not upstream at all.
9. **New section: "Comparing a store's territories across releases of the
   same ISRC"** — a store's rights for a track are its `country` set here,
   not `mastermusic.rights`; a query that compares the sets per ISRC and names
   each row's missing/present countries, with the UPC from the parent album.
   Written 2026-10-05 after the agent compared whole `rights` maps for SPLH,
   produced an 8 GB result and never answered. Checked against four ISRCs
   the user gave as known cases. Not upstream at all.

## `mastermusic.md`

3. **`status = 1` scoped to this table** — explicitly not a global catalogue rule
   and not a reason to join into store questions (pairs with item 2).
4. **Size corrected** — "~219M rows" is the active *track* count; active tracks
   + albums is 270.1M, unfiltered ~280M (measured 2026-09-24).
5. **Column reference expanded** — `title`, `title2`/`title_original`,
   `artist_name`, `artist_name2`/`artist_name_original`, `artist_id`,
   `duration_secs`, `asset_type`, with measured fill rates; `isrc`/`upc` stock
   alignment made exact.
6. **New section: "Artist vs label vs distributor"** — `owner_name` is the
   distributor, not the artist or label (observed producing a wrong "top
   artists" answer on 2026-09-24); `label` vs `sub_label_name` flagged as
   unresolved.

8. **`stock_code` and `pk` documented, plus a new section "Looking up products
   by stock code"** — the `{owner_id}_{UPC}_{ISRC}` / `{owner_id}_{UPC}`
   format, a batch `VALUES` + `LEFT JOIN` lookup that keeps unmatched codes as
   `NOT FOUND` rows, and territory streaming from `rights` (owner `1399` has
   no `WW` entries; `WW`-vs-country precedence unverified). Also: `rights` keys are
   lower case on 24% of owner `1399`'s tracks (`us`, not `US`), so the query and
   the `rights` column note both compare `upper(key)`; rights are live only
   between `ssdt` and `sedt`, and start dates run into the future. Written
   2026-09-30 after the agent, with the column undocumented, guessed `pk` and
   `track_id` and reported an existing product as not found.
10. **`content_type` documented** as the way to tell audio, video, karaoke
    and audiobook apart, preferred to the `is_*` flags; and the flags' "karaoke
    implies `is_video`" claim marked contradicted (ETEG's September 2026
    karaoke tracks all have `is_video = false`). Written 2026-10-06 with item 11.

## `music_streams_v3.md`

11. **Columns documented: `dw_reported_date` (a DATE partition: compare with
    `DATE '...'` literals), `group_id`, `stock_code_id`, `asset_type_id` (not the
    content type, and empty on every ETEG stream checked), `play_type`; plus a
    new section "Streams by content type"** joining `stock_code_id` to
    `mastermusic.content_type`. Written 2026-10-06 after the agent filtered
    `asset_type_id = 'video'` for ETEG's September video streams, and retried a
    string-vs-date comparison 36 times. Checked: 23,449 video streams.

## `playactivity.md`

7. **New section: "Counting streams on bronze"** — the 30-second-or-completed
   rule as SQL, with a pointer to prefer `music_streams_v3.is_stream` on silver.
