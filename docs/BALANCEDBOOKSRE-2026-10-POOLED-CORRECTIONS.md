# Posted pooled corrections — 0.2.1

This pass addresses the October 22 updated pooled-transaction handoff's five priorities. It does not merge additional upstream features or apply accounting corrections to the live company. No database migration or financial backfill is required.

## Changes

- A shared cash-lineage normalizer accepts an original bank line plus proven same-account, same-date, independently zero-net corrections. Every physical line and gross debit/credit remains intact. Allocation versions replace effective attribution. Other observations, unrelated ancestry, additional net movements, and later-dated corrections are rejected.
- Posted-pool planning removes only the selected observation's existing attribution when checking funding. An exact existing bank-to-custody mismatch can be repaired without demanding the withdrawal twice. Custody, restricted release, period, and funding checks remain active.
- Reallocation exposing insufficient source holdings remains blocked. Diagnostics distinguish existing cash reallocation from a new release, with affected funds, before/delta/after cents, provenance, and the missing dependency. Supported dependencies can use the existing atomic batch; no financing is inferred.
- `resolve_historical_shortage` requires a fresh dated assessment, explicit reason/evidence, continuous reconciled source coverage, and relevant correction or identified restoration. It checks the selected interval rather than the latest reconciliation. Precise failures distinguish missing/stale reconciliation, unrelated correction, historical deficit, and replacement-scope deficit. Later alerts remain unchanged. End-of-day evidence does not certify intraday availability. Assessments recognize both allocation versions and validated legacy zero-net attribution journals.
- Correction previews/results include gross preexisting closures, newly open reviews, net reduction, and linked transitions. Pooled results expose affected funds and before/after scoped suspense. Incoming member-loan correction can identify evidenced own-company funding. `resolve_loan_funding_source` documents an already classified receipt's lender/source without another journal, leaving cash attribution and unrelated deficits unchanged.
- Domain failures now produce MCP tool errors containing structured diagnostics, rather than successful outer MCP calls.

## Boundaries

No successful historical correction is replayed automatically. No bulk review closure, direct financial SQL update, invented financing, or state-law tracking is introduced. Source deficits still need supported dependencies. Later-dated/multi-account legacy chains remain outside the single-observation pooled allocation path. Other historical proposals remain as documented in the earlier maintenance release.

## Validation

Synthetic regressions cover legacy three-line history, both statement bases, retries, unrelated/date/amount rejection, already-funded withdrawals, atomic deficit rejection, historical resolution with separate later shortage, loan provenance, preview rollback/commit, and MCP errors.

On a fresh disposable Rinaldi Capital Group backup, Gibson observation 21276 passes the correction path. Hackensack 21285 correctly identifies an $89.99 source-fund deficit. Review 34568's dated assessment recognizes Boylston zero-net correction 29137. These rehearsals are rolled back; they are not live accounting corrections.

Final focused acceptance suite: 67 passed. The broader suite recorded 3,792 passes and 204 environmental skips, with five environment-related failures: four OCR probes cannot traverse the root-owned mode-0700 `/usr/local/bin`, and a launcher process test was sandbox-restricted. OCR integration collection has the same host permission limitation. No OCR code or system permissions were changed in this scoped pass. Rehearsal rollback preserved all 119 database tables exactly by row-content hashes.

The launcher regression suite passed outside the sandbox: 11 passed, 1 Windows-only skip. The remaining four broader-suite failures are the unchanged OCR host-permission limitation.
