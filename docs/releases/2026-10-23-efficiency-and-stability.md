# Efficiency and stability

Portfolio release `2026.10-evidence-workflows.3`; local Mac app `0.4.1`, build
`20261023.2`; upstream base `2.22.3`. Schema remains `bb0004`. This release makes
no financial backfill and changes no existing reviews.

## Changes

- **Card refund history:** a later effective allocation could move unrelated
  historical receivables into the refunded item and conceal an earlier settled
  balance. Refund headroom now checks each allocation-effective interval, for
  both counterparties and both accounting bases. Unsupported backdated refunds
  fail atomically instead of overstating the original obligation. One scoped
  ledger packet per counterparty supplies every dated check. A synthetic case
  with 26 effective-date intervals required 12 queries instead of 368; query
  count stays constant as intervals increase, while projection work still grows
  with the financial history being checked.
- **Ledger memory:** revision reads select and stream only financial fields,
  excluding unused evidence prose and complete ORM objects. Every applicable
  revision remains available; projections and state digests are unchanged.
  The [reproducible benchmark](../dev/portfolio-revision-column-reads.md) showed
  peak traced Python allocation falling from 7.53 to 1.47 MB and median elapsed
  time from 42.33 to 34.42 ms on 1,200 evidence-heavy synthetic revisions.
  These measurements do not establish production latency or process RSS.
- **Compact polling:** operation status loads only the recent history window
  plus still-current reviewer attestations. Exact counts, full responses, saved
  results and supersession semantics remain intact. Counts and current-scope
  lookup still inspect database indexes; historical proof JSON no longer has to
  be decoded for every poll. All fields come from the same read snapshot.
- **Read availability:** expired token-use telemetry previously made an
  authenticated SQLite read wait for a writer, then fail if the lock remained.
  Telemetry now skips only SQLite BUSY/LOCKED contention, retries on a later
  request, and preserves the normal timestamp audit event when successful.
  Active-token authentication, revocation, role enforcement and other database
  failures retain their existing behavior. The temporary zero busy timeout is
  restored before its connection returns to the pool.
- **Native setup recovery:** cooperating launches serialize settings, migration,
  company identity and credential setup with a private POSIX file lock. Fresh
  settings and credentials are published as complete, fsynced `0600` files
  without overwriting existing names. A private pending issuance record allows
  recovery before or after token commit, including lost commit acknowledgment.
  Existing secrets stay byte-identical; revoked, mismatched or orphaned database
  tokens are never silently replaced or reactivated. This setup lock does not
  coordinate listener ownership after setup or run for the server's lifetime.

## Verification and upgrade

Regression coverage includes allocation date boundaries and atomic rollback,
exact projection parity and independent JSON ownership, compact histories with
older current attestations, concurrent snapshot consistency, and token reads
under a real WAL writer lock. Token tests also check audit recovery, throttling,
revocation, non-lock error propagation and connection-pool timeout restoration.
Native setup tests cover concurrent threads/processes, private file permissions,
interrupted publication/commit/cleanup, existing-file preservation, and rejection
of symlink or nonregular setup-lock files.

Release gates are the complete pytest suite, whole-tree Black/Ruff checks,
native MCP execution against fresh synthetic books, and a clean published-source
build manifest. Installation retains the previous bundle and a consistent
database backup, then checks every existing table, company identity, review
count, executable hash and audit history. Only ordinary token-use telemetry is
allowed to differ. No live financial command is used for this verification.

The [remaining handoff scope](2026-10-23-accounting-and-runtime.md#handoff-status-after-this-pass)
continues to apply. This patch does not claim to complete all roadmap items.
