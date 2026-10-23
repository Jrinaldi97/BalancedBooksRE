# Local evidence-workflow release

Portfolio release: `2026.10-evidence-workflows.1`. Local Mac app: `0.3.0`, build
`20261022.3`. Upstream base remains `2.22.3`; schema advances from `bb0002` to
`bb0003` with one additive reconciliation-revision table. No financial backfill
or automatic review closure runs during upgrade.

## Accounting behavior

Existing accounting can now be validated using the recognized historical
beneficiary contract without another journal. Conflicting identities, unsupported
source shapes, stale state and dimensional suspense still reject. The expanded
contract supports complete multi-entity economic vectors and explicit single-bank,
paired-bank or card-liability movement manifests. Every original observation must
match its actual account, date and amount; totals alone do not establish lineage.
Legacy requests retain their prior balance-dimension semantics. Explicit expanded
requests check all declared dimensions, including cash-flow classification.

`outgoing_funding_assessment` and `resolve_outgoing_funding_source` separate a
withdrawal's funding source from its established party receivable or intercompany
advance. The command records source evidence and, where needed, an append-only
attribution replacement. It preserves principal and aggregate physical bank cash,
checks both reporting bases and relevant date boundaries, rejects client/restricted
fund financing, and retains an explicit unsupported remainder. A balance alone is
not source evidence. Ambiguous partial allocations across dimensions, later
attribution revisions and contradictory bank basis adjustments require review.

Historical shortage proofs now distinguish an original restoration receipt from
its later matched-transfer classification, record shared deficit episodes, and
accept explicitly assessed causal allocation sets. A current positive balance does
not independently close a historical alert. Replacement scopes, explicit lineage,
reconciliation coverage and the selected proof digest remain enforced.

`reconciliation_revision_state` and `revise_reconciliation` allow a reviewed,
append-only replacement of an obsolete statement snapshot. The operation requires
the exact current observation inventory, supported opening and ending balances,
registered source hashes and a fresh digest. Original records stay inspectable;
competing revisions, gaps and unsupported mid-statement cutoffs reject.

## Codex interaction

- The local adapter timeout defaults to 600 seconds and is configurable from
  1–3600 seconds with `BALANCEDBOOKS_TIMEOUT_SECONDS`.
- Transport failures describe unknown write outcomes and same-key recovery.
  A missing request record does not prove that execution stopped.
- `observation_states` reads 1–200 IDs in one database snapshot. Compact/full
  projections share state/effect digests and provide recoverable full references.
- Preview IDs are provisional. Prepared accounting, committed accounting and
  externally verified evidence are separate states. This release does not add
  a durable asynchronous running queue or external-verification attestations.
- Context exposes the existing opaque company session identity plus startup
  build diagnostics. Optional company/schema/build expectations reject a stale
  connection before executing accounting.
- Request-scoped review effects replace whole-book review scans and include
  batch/per-code totals. Classification overrides are selected by relevant lines;
  event detail prefetches accounts and document locations.

See [agent API details](../dev/portfolio-agent-runtime.md), the reproducible
[benchmark](../benchmarks/portfolio-runtime-2026-10-22.json), and
`scripts/benchmark_portfolio_runtime.py`. The benchmark demonstrates scoped-query
and payload savings; it does not establish live-company latency or a faster
underlying computation for batched observation states. Outgoing funding assessment
performance on a large account remains unbenchmarked.

## Handoff coverage

This is the first staged release from the engineering handoff, not a declaration
that its entire roadmap or the company's accounting review is complete.

| Handoff area | Included here | Remaining work |
| --- | --- | --- |
| A — historical beneficiaries | Recognized versioned decoder and negative tests | Additional historical shapes require explicit evidence and tests |
| B — complete corrections | Multi-entity vectors and exact movement manifests | Additional movement families need typed contracts |
| C — outgoing funding | Evidence and attribution for typed receivables/advances, partial holds | Broader historical attribution variants |
| D — cards | Typed source visibility and existing-correction validation | Purchase/refund/repayment/return lifecycle, account aliases and issuer coverage |
| E — owner distributions | Existing workflows remain available | Atomic sender-to-holder distribution and onward beneficiary applications |
| F — general correction plans | Existing typed routes remain available | General components, units and independent recognition periods |
| G — durable status | Truthful boundary states, preview/request recovery, longer timeout | Durable accepted/running operations and separate verification records |
| H — performance | Scoped override/detail reads, review-effect collector, bounded batch reads, whole-account assessment fast path | Shared ledger inputs, exact SQL aggregation and broader scale benchmarks |
| I — concurrency/contracts | Compact/full results, batch lifecycle summaries, snapshot reads | Dependency-scoped revisions, shorter preview locks and persistent cursor snapshots |
| J — evidence/cases | Existing documents/policies reused; typed proof records for new workflows | General assertions, clarification queue, cases and scoped rules |
| K — policies/coverage | Existing ownership/policy and reconciliation safeguards preserved | Source groups, allocation conventions and account/issuer coverage UX |
| L — agent hardening | Optional company/schema/build binding; existing local access controls retained | Capability/entity restrictions, expiry and managed token rotation |
| M — imports/storage | Optional OCR installation probing tolerates inaccessible paths | Legacy XLS normalization and read-only storage diagnostics |
| N — restoration lineage | Original/matched receipts, deficit episodes and metadata-only resolution | Other legacy ancestry variants require explicit support |
| O — combined allocation | Causal correction sets, active revisions and affected-scope checks | Broader historical correction shapes |
| P — reconciliation revision | Audited immutable supersession with source/inventory checks | No unproved intra-statement cutoff shortcut |

## Local build and upgrade

Run the test suite on synthetic data. Rehearse `bb0003` on a consistent SQLite
backup before upgrading a real company, checking that existing table contents and
financial records remain unchanged. Retain both the database backup and prior app
bundle. Downgrading a company with reviewed reconciliation revisions is refused;
restore a verified backup rather than discarding audit history.

From a clean source commit, generate build identity before invoking PyInstaller:

```sh
python scripts/bbre_build_manifest.py
pyinstaller --noconfirm packaging/macos/BalancedBooksRE.spec
```

The generated manifest is excluded from Git and packaged with the app. Verify its
source commit and the independently computed executable hash through native MCP
context. Existing long-lived adapters may need reconnecting to discover the new
tool names. The bundle is for local internal use; it is not an Apple-notarized
public distribution.

Software tests and installation do not resolve existing financial reviews. Any
subsequent financial remediation requires fresh source evidence, current digests,
the native guarded workflow and scoped actual verification.
