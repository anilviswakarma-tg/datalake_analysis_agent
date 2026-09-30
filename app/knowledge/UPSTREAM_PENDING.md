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

## `playactivity.md`

7. **New section: "Counting streams on bronze"** — the 30-second-or-completed
   rule as SQL, with a pointer to prefer `music_streams_v3.is_stream` on silver.
