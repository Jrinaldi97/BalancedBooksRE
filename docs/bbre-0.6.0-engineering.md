# BalancedBooksRE 0.6.0: evidence workflows and native agent operation

This release extends `fix/october-workflow-followup` (`28e779ba`) after reviewing
actual portfolio use and the remaining-limitations engineering handoff. The
implementation uses synthetic acceptance cases. Client documents, databases,
review IDs and source evidence are not included in this repository.

## Workflow changes

| Limitation | Implemented behavior | Regression coverage |
|---|---|---|
| An obligation can be supported by several corrections | `validate_existing_correction` accepts an explicit `supporting_corrective_event_ids` set. The original scalar anchor stays required and must belong to the set. Each material scope needs actual value-bearing support on both bases; metadata-only descendants, foreign families and unrelated scope cannot qualify. The immutable receipt maps scopes to supporting events and registered hashes. | `test_portfolio_family_support.py`, existing correction-validation tests |
| A check is one component of an aggregate deposit | `associate_transfer_component` records immutable, versioned metadata with exact remaining capacity. `reverse_transfer_component` appends a reversal. Whole-transfer claims and components cannot reuse capacity. No cash, purpose classification or closure is created. | `test_portfolio_transfer_components.py`, concurrent claims and immutable history |
| A mortgage payment is already posted to suspense | `correct_posted_mortgage_payment` requires current source/debt digests and registered actual-payment application evidence. Principal cannot exceed supported debt at the payment date or consume capacity required later. Effective allocation dates are respected. Partial interest/escrow/fee corrections retain the unexplained residual. | `test_portfolio_posted_mortgage.py`, native agent evidence tests |
| Card payment date differs from invoice/service dates | Card components explicitly choose `issuer_date`, `invoice_date` or `funded_deposit`. Completed invoices carry identity, supplier, service dates, gross amount, earned/forfeited discounts and a registered source. One atomic operation recognizes the invoice, creates/reuses its open item, applies the payment and supplies modified-cash adjustments. Partial invoices retain AP; mixed deposits and multiple invoices work. | `test_portfolio_card_invoices.py`, existing card refund/failed/retry/source-preservation tests |
| Missing debtor or source-of-funds evidence is difficult to act on | `evidence_plan` identifies exact obligation dimensions, fund beneficiaries, eligible own-fund targets, original movement, open questions and needed evidence. Eligibility, beneficiary and debtor are explicitly distinct. | `test_portfolio_agent_evidence.py`, existing funding and beneficiary tests |
| Coverage diagnostics hide missing trailing periods | `coverage_plan` names missing bank intervals and invalid current reconciliations. `card_state` exposes uncovered leading/internal/trailing periods, historical validity and current coverage separately. Zero liability and an empty internal-gap list do not certify completeness. | Agent evidence tests and existing card coverage/reconciliation tests |
| Conflicting card suffixes need evidence of replacement | `add_card_alias` optionally links a predecessor alias and registered issuer evidence, requires the same issuer account and an ordered effective start, and retains both identities. It does not turn a similar date/amount into a verified transaction match. | Alias replacement and existing alias/date tests |
| The registered original file is missing or moved | `document_state` reports availability separately from hash verification. `relocate_document` verifies exact bytes and appends a location; different exports must be registered separately. Hash reads are local, regular-file-only, bounded to 128 MiB and reject symlinks or changes while reading. | Missing/matching/different bytes, symlink and nonregular-file tests |
| A genuine restoration has legacy fund overlays | Original-receipt validation normalizes an explicit same-source, same-date correction family. Each correcting event must be zero-net cash and effective receipt components cannot borrow another fund. The original deficit episode, receipt amount and reconciliation proof still govern closure. | Positive legacy overlay plus changed-date/borrowing/unrelated-source regressions |

A daily accrual or prepaid policy is **not** silently inferred from a meter period.
The new card operation supports disclosed invoice-date recognition for completed
service, issuer-date recognition and actually funded deposit assets. Future service
uses the existing prepaid workflow. A lender's outstanding balance is not authority
to invent opening debt. Software support does not itself supply missing evidence or
replay any historical remediation batch.

## Agent contract and performance

- `command_schemas(operations=[...])` returns selected contracts (up to 20); the
  full contract is cached by its model set and retains its existing default.
- `review_register` pages short status rows (up to 200) with exact resolution hashes
  and proof references. Continuation requires an unchanged book digest.
- `proof_page` reconstructs full request/proposal/review proof, including Unicode,
  in digest-bound character pages. Compact responses keep full effect digests and
  full-result references. Large assessment/restoration/obligation proofs become
  small summaries in compact mode; stored evidence is not truncated.
- `run_manifest(keys=[...])` gives authoritative native receipt, proposal, review
  and scoped attestation states for up to 200 exact operation keys. Accepted,
  prepared, stale, cancelled, rejected and committed work are distinguishable.
  A committed receipt takes precedence over an old proposal. Attestation does not
  imply that the software independently inspected external documents.
- Existing durable submit/status/resume, same-key recovery, exact-body conflict
  checks, preview revision guards and provisional preview IDs remain in force.
- Batch observation lineage uses a union of original pointers, scalar references
  and explicit reference arrays, with indexes for correction/source joins. It
  avoids an observations-by-events JSON scan and preserves full-state digests.
- Entity revision counters use an atomic bulk increment. Financial events, lines,
  actor attribution and immutable request receipts retain their audit trails;
  revision-only updates no longer create one full entity audit row per command
  per entity. Existing audit history is retained.
- Native `storage_diagnostics` defaults to compact metadata. Request `full` for
  the more expensive B-tree and stored-payload breakdown. Neither mode vacuums,
  checkpoints, deletes or rewrites history.

## SlowBooks Pro comparison

Reviewed upstream `main` through `197271ca` (2.31.1), relative to the fork's 2.22.3
base. This is a selective integration, not a declaration that BBRE now has every
upstream feature or migration. The upstream base version remains separately
reported; the BBRE contract release is `2026.10-evidence-workflows.6`.

| Upstream change | Integration decision |
|---|---|
| `9fa7e15e`, `a4a7fa83`: atomic account balances, ordered deltas and targeted cache expiration | Applied to posting and invoice reversal while preserving the portfolio posting barrier. Pending updates flush before SQL arithmetic. |
| `1fe4cf69`: audited line deletion without stale identity-map entries | Applied to invoice/bill edits, QBO invoice replacement and estimate lines. |
| `6559704a`, `882a0cf4`: audit secret redaction | Adapted writer and historical API reader redaction. Password/token/settings secrets are concealed; evidence/source hashes remain available. Historical rows are not rewritten. |
| `070c5334`, `0afd3b7b`: complete input matches and safe backup-name validation | Applied to current username/account/preference/AI/company/backup validators; extension parsing avoids regex backtracking. |
| `dd23070e`: finite scalars, NUL/date bounds and explicit-null updates | Applied to current routes and shared models, including native portfolio inputs. |
| `2eca3f4b`: middleware order | CORS and security headers wrap the authentication gate. Native token authorization is retained. |
| `a58969fc`: static upload protection | Applied and strengthened with case folding for case-insensitive Mac filesystems; encoded, traversal, duplicate-separator and uppercase paths are tested. |
| `ea77dae7`: revocation that sticks | Revoked tokens cannot be reactivated. BBRE's existing explicit native role/close-period contract is retained; no new token privilege is granted. |
| `c5954a81`: revocable browser sessions | User deactivation, role changes and password changes revoke existing sessions. Roles are reread from the user row. Version increments are atomic; pre-upgrade cookies cannot adopt a post-reset version. The auth-status response follows revocation, and native API tokens keep their separate contract. |
| `3e1e2b26`: typed opening equity lookup | Reuse only equity-typed accounts, including imported names; reject a wrong-typed reserved name. No automatic opening is posted. |
| `ac3fe8d7`: current Stripe SDK objects | Convert actual SDK payloads to plain nested mappings before webhook/poll reads; no newer clearing-account feature is assumed. |
| `c27055b9`, `c70a19f8`, `e40986f1`: Mac release resilience | Bounded staple/validate and busy-disk-image retries, with fail-closed tests. Apple signing/notarization credentials are not bundled or required for the internal-use build. |
| Accountant's Package / adjusting-entry traceability (`4c557a95`, `a3909e7e`) | Adapted the useful review/continuation function to native compact registers, recoverable proofs and scoped run manifests. The upstream GUI package format and its unrelated data model were not copied. |
| New upstream cash-basis engine (`3cf4df8d` and follow-ups) | Retain BBRE's established accrual/modified-cash projection and immutable scope controls. Native supplier invoice timing adds the useful payment-recognition behavior without introducing a conflicting second basis engine. |
| Server multi-company hosting, encrypted company-file format, QuickBooks replacement migration, construction job/pay-application suite, parent-customer billing, inventory expansion, payroll/UI features | Not imported wholesale. These require independent data-model/platform migration work, do not fit the reviewed native portfolio workflow, or duplicate existing portfolio/entity/property functions. Retain current import compatibility and local company credentials. |
| Reclassify balance repair and later fiscal-year Trial Balance fixes | The affected newer reclassify/fiscal-year engine is absent from this fork. No automatic historical balance rebuild or unrelated reporting migration is introduced. |

The full regression pass also exposed an inherited long-lived QBO-session issue:
a cached invoice could hide a payment committed in another request. The import
now flushes its pending changes and locks/reloads the invoice before both document
and ledger paid-total guards. Its inactive-account test source is fully mocked.

## Validation and release boundaries

Required checks: `black --check app tests`, `ruff check app tests`, and the complete
`python -m pytest tests -q` suite. Focused tests cover exact retries, stale state,
wrong-key conflicts, rollback, concurrent component claims, immutable/superseded
history, both bases, refund/return behavior and effective allocation boundaries.

Migration `bb0005` is additive: transfer associations, optional issuer-replacement
fields and read indexes. It is repeatable; downgrade refuses populated new evidence.
Migration `bb0006` adds a per-user browser-session version with a zero default.
A consistent private database copy is used for migration, row-hash comparisons,
SQLite integrity/foreign-key checks and read-result parity. No test runs against
live books. Compaction is evaluated only on a copy and does not justify deleting
historical audit, proposals or proofs.

Build an exact clean commit with `scripts/bbre_build_manifest.py` and
`packaging/macos/BalancedBooksRE.spec`. The internal-use Mac bundle is ad-hoc signed.
A local build/smoke check is separate from Developer ID notarization, installation,
production migration and a fresh authorized accounting-remediation run.
