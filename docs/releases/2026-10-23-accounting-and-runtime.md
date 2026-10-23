# Accounting lifecycle and recoverable Codex operations

Portfolio release `2026.10-evidence-workflows.2`; local Mac app `0.4.0`, build
`20261023.1`; upstream base `2.22.3`. Schema `bb0004` adds seven tables for card
identity/coverage/activity and durable execution/verification history. Upgrade
performs no financial backfill and closes no reviews.

## Accounting changes

- **Cards:** stable issuer identities and dated aliases; deduplication across
  aliases; typed company costs, maintained-entity reciprocal charges and real
  external-party receivables; component refunds; paired bank/issuer repayments,
  partial returns and linked retries. Actual posting dates are retained through
  a settlement bridge. Repayments never create a second purchase expense.
- **Issuer coverage:** source document hash, exact observation inventory, opening
  and ending liability and fresh state checks. A payment confirmation alone
  cannot certify the issuer balance. Late imports make prior coverage stale;
  reviewed append-only supersession retains the original statement evidence.
- **Owner distributions:** atomic classification of actual sender/holder bank
  observations using complete dated economic ownership and deterministic residual
  cents. Sender equity and holder custody remain distinct. Receipt-only coverage
  records a source gap without inventing sender cash or equity. Each external
  owner gets an individual obligation; partial/final onward payments apply only
  to that exact beneficiary and remaining amount.
- **Audit:** operation cancellation records the actual cancelling user rather
  than attributing it to the original submitter. Existing posted accounting,
  source history and append-only evidence remain protected.

These workflows accept supported source shapes and explicit evidence. They do
not infer ownership, certify a source document automatically, or resolve the
company's existing reviews merely because software was upgraded. Distribution
custody currently covers external owners with unrestricted general funds;
maintained-entity owners and restricted funds require different typed workflows.

## Codex and data handling

`submit_operation`, `operation_status`, `resume_operation` and `cancel_operation`
provide durable accepted/running/terminal stages for local SQLite commands and
atomic batches. Admission binds company, schema, build and original principal.
Explicit same-key resumption handles interruption; accounting and the committed
receipt/status share one transaction. Cancellation is available only before
claim. No automatic startup replay or partial child commit is performed.

`record_operation_verification` saves append-only reviewer attestations against
an exact committed effect digest and registered document hashes. Execution and
review status stay separate. A declaration is not independent external checking.
See the [runtime contract](../dev/portfolio-agent-runtime.md).

`observation_states` shares one ephemeral ledger packet and SQLite snapshot
across up to 200 IDs. The [synthetic benchmark](../benchmarks/portfolio-shared-reads-2026-10-23.json)
checks full outputs and digests against a frozen prior reader. At 200 observations,
ORM-evicted median time fell from **1663.968 ms to 245.442 ms**, with **3400 to 10 SQL
queries**. Peak Python memory increased. This does not establish production
latency, connector transit time or concurrent write throughput.

Legacy XLS imports now normalize through pinned `xlrd` after rejecting formula
and encrypted BIFF workbooks. Original file hashes, worksheet names, physical
rows and duplicate occurrences are retained. Sheet-specific retry identity avoids
dropping equal transactions from separate worksheets while recognizing old
same-sheet imports. Read-only `storage_diagnostics` reports stored payload sizes,
SQLite page usage and separately sampled file/WAL sizes; it performs no cleanup.
See [import/storage details](../dev/portfolio-shared-reads-and-imports.md).

## Handoff status after this pass

The [first release](2026-10-22-evidence-workflows.md) covers A/B/C/N/O/P and earlier
runtime safeguards. This pass closes substantial D/E/G/H/M implementation items.
It does not claim that every roadmap item is finished.

| Area | Now available | Remaining scope |
| --- | --- | --- |
| D | Typed purchase/refund/payment/return/retry lifecycle, aliases and issuer coverage | Additional historical source shapes, richer coverage UX |
| E | Paired and receipt-only external-owner distributions, individual custody applications | Maintained-entity owners, restricted distributions, broader recognition timing |
| F | Existing guarded typed correction routes | General mixed-component plans, independent recognition periods and broader unit support |
| G | Durable local admission/status/recovery, atomic batches, reviewer attestations | Automatic supervised recovery policies and non-SQLite background execution |
| H | Shared ledger/ancestry reads, exact parity and 1/30/100/200 benchmark | Large-company profiling, SQL aggregation where parity is proven |
| I | Existing connection binding, snapshots, compact/full results | Dependency-scoped revisions, shorter preview locks, persistent cursors |
| J | Registered evidence and scoped immutable attestations | General evidence assertions, clarification queue, cases and rules |
| K | Existing ownership/policy and new issuer coverage controls | Source groups and richer allocation/coverage configuration |
| L | Original-principal revalidation and company/build binding | Capability/entity restrictions, token expiry and managed rotation |
| M | Strict legacy XLS, sheet-safe retry identity, storage diagnostics | User-approved archival/compaction design; no destructive cleanup |

## Release validation and upgrade

Run the full suite and whole-tree Black/Ruff gates. Rehearse `bb0004` on a
consistent company backup and compare all existing table contents before
installing. Retain a separate pre-install database backup and prior app bundle.
Migration downgrade refuses to discard populated card or operation history.

Generate the build manifest from the clean published source commit, package with
`packaging/macos/BalancedBooksRE.spec`, and exercise native MCP against fresh
synthetic books. Confirm the installed executable hash, source commit, company
identity, review count and preserved financial/audit history. Read-only token-use
telemetry is accounted for separately. Existing long-lived Codex adapters may
need reconnection to discover new tools. The bundle is for local internal use;
it is not an Apple-notarized public distribution.
