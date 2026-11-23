# Development History Mask Area backfill

Use this offline Windows checkout tool to enrich legacy ViMo boxes in the development app's entry-relative `data/history.sqlite3`. The read-only reference is the original AREA-STUDY History package. The PNG directory and supplied ViMo model must match that study. The writable database must be a separate file. Normal application startup and portable review do not invoke this tool.

## Preview and apply

Run the following from the checkout containing `app.py`. Close the development application and other History owners before the initial plan and before applying. The defaults are `D:\Ly\Images`, `C:\Users\Sesame\Projects\Ly\model261116.vimosln`, and `outputs/dev-history-area-cleanup-1/full/`. Use `--images`, `--model` and `--work` for other verified copies or a fresh checkout output directory.

```powershell
uv run --no-sync python -m scripts.rebuild_history_areas plan --group 4769
uv run --no-sync python -m scripts.rebuild_history_areas plan
uv run --no-sync python -m scripts.rebuild_history_areas verify
uv run --no-sync python -m scripts.rebuild_history_areas apply
uv run --no-sync python -m scripts.rebuild_history_areas verify
```

`plan` takes a byte-verified snapshot of the starting development database under the application's maintenance lease. It accepts a prior enrichment when protected fields and historical camera/channel/view/label associations agree with the original package. Keep `before-history.sqlite3` with its checksum-bound plan. Review `summary.json` for coverage and `comparison.jsonl` for each camera's original and proposed rows, receipt origin and unresolved evidence. A group selector uses the stored first Cycle. Planning checks every linked camera exposure in scope, including those without recorded findings.

`apply` takes the maintenance lease, checks the linked PNG digest for each replay-based change and commits up to 32 Cycles per transaction. Use `--batch-size 1..128` to change the transaction size or `--limit N` to bound a planning or application pass. Repeat a command with the same work directory to resume. Use `plan --retry-unavailable` after restoring a missing source. A successful retry can replace a previously published display-only fallback with measured evidence.

`verify` reports applied, pending and unchanged Cycles. It checks SQLite integrity, foreign keys, all recorded business fields, group membership, links, captured configuration and reporting totals against the original snapshot. It also checks that any changed finding rows agree with their per-camera comparison. An unexpected change to development records stops publication. To recover the pre-rebuild evidence, retain the verified backup and its original SHA-256. Restoring a complete database would also revert any later development records.

## Evidence rules

The AREA-STUDY pilot, archive replay cache (`outputs/area-explorer-ux-2/cache`) and previous development rebuild cache can supply SDK measurements when the Run, group slot, Cycle, camera, PNG digest, dimensions, model, module and inference settings agree. A conflicting PNG path or digest from a verified pilot or previous rebuild stays unresolved. A cache miss for an original photograph uses the existing serial ViMo worker on the full-resolution BGR image. Frozen Recipe policy, channel geometry and Occupancy determine replay eligibility.

Within an exposure, a recorded effective finding establishes its channel, optical view and exact label. Verified replay supplies the largest eligible SDK Mask Area and its original rectangle for that combination regardless of rectangle overlap or region count. SDK order settles equal areas. A rectangle centered on the frozen divider belongs to the lower view. A shared region retains only the channels for which it wins and has recorded support.

When a historical label lacks an eligible replay winner, the tool retains one recorded representative. An existing measurement takes priority. Among unmeasured boxes the largest rectangle wins, with stored order settling ties. Its caption remains label-only. The comparison lists historical-only and replay-only associations separately. Unavailable photographs or receipts can use the same display fallback when the frozen channel geometry is trustworthy. If that geometry is also unavailable, the stored rows remain intact. The operation changes only effective finding rows in retained Cycle detail.

Portable review uses a separate copy of the enriched History with its corresponding archive layout. The original portable input remains the comparison reference. [History storage and portable review](configuration.md#history-storage)
