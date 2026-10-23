# Portfolio agent runtime contract

The local MCP adapter keeps all calls on authenticated loopback HTTP. This change retains existing transaction locks, per-entity revisions, immutable posting rules and exact key/content idempotency. It does not post or remediate live company data.

## Recovery and execution state

`BALANCEDBOOKS_TIMEOUT_SECONDS` sets the adapter HTTP timeout, from 1 through 3600 seconds; the default is 600 seconds. A timeout, disconnect, server error (HTTP 5xx) or malformed response during a mutation raises a structured `unknown_outcome` tool error. The adapter does not automatically issue another mutation or log the raw exception, token or request body.

For a command, recover with `request_result(key)` (REST `GET /api/portfolio/requests?key=...`, preserving arbitrary retry-key characters); if absent, the request may still be running. Retry only the same key and body. For a commit, retry the same proposal ID and digest. A lost preview response may leave a saved preview, but no accounting was committed by preview execution. `proposal_detail(proposal_id)` recovers the full saved preview when its ID is known. Proposing an already-committed key returns `already_committed` with recovery guidance; it cannot relabel real accounting as prepared or create another proposal. Changed content under that key still returns `idempotency_conflict`.

API command/commit responses say `execution_state: committed` only after database commit. Saved previews say `prepared`; their generated IDs are explicitly provisional. A committed proposal read separates its historical prepared preview from its actual committed result. Request lookup exposes committed records only: `not_found` means `execution_state: unknown`, never proof that execution stopped. There is no durable accepted/running queue or cancellation contract.

`verification.external_evidence: not_recorded` states that this API does not record external source verification. Internal accounting safeguards have run, but that does not certify documentary completeness or a user's later verification work. No external verification attestation or evidence-case workflow is added here.

## Bounded reads and projections

`observation_states(ids, as_of=None, projection="full")` accepts 1–200 unique positive IDs. Every state in a response uses one SQLite read transaction, including both bases and all correction-chain queries. The response is complete or fails for a missing ID; it is never silently truncated. Its `snapshot_digest` binds ordered observation IDs to full state digests. It is not a cursor for later requests, and observations are still evaluated individually within the snapshot.

`full` remains the default. `compact` is available for observation states, event detail, command/commit responses, saved previews and request recovery. It removes repeated source or review prose, retains financial components, dimensions, residuals, event IDs and review transitions, and reports `omitted_fields` plus a recoverable `full_result_ref`. Full and compact command responses share an `effect_digest`; observation projections share the original `state_digest`. Prepared event references point to the saved proposal, never to provisional event IDs as though they were committed.

Tracked correction commands use a request-local ORM flush collector instead of reading every review before and after execution. The outer atomic batch reports final review effects across all its children, including metadata-only commands. Per-code counts distinguish preexisting closures, newly opened flags, reopened flags and net reduction. `replaces_review_ids` retains the older co-occurrence convention and is not proof of causal replacement.

Scoped ledgers restrict cash classification reads to applicable ledger line IDs while preserving date/ID precedence. Property filtering still occurs after effective allocation replacement, so an allocation to a different property remains visible. Event details join line dimensions/accounts and prefetch document locations in bounded queries.

## Connection binding

Read `context.connection.company_session_id`, the existing opaque company identity, before financial work. New command, proposal and commit envelopes accept optional `expected_connection` with `company_session_id`, `command_schema_digest`, `source_tree_sha256` and/or `executable_sha256`. A mismatch rejects before service execution. Saved proposals retain supplied preconditions and recheck them at commit, even if the commit request omits them. Financial idempotency hashes continue to cover operation/data only.

Build diagnostics report a process-instance ID, a startup snapshot hash of Python application source in source mode, or a SHA-256 of the executable in packaged mode. Optional `build_manifest.json` values are explicitly build-supplied metadata. These are diagnostics, not signatures: an executable hash alone does not certify every bundled resource. Restart after changing code; a disk edit does not change the identity already captured by a running process. A missing legacy company ID is reported as null; the read does not create a new identity.

These optional preconditions supplement existing authentication and RBAC. This change does not add token expiry, entity/account permissions, narrower write capabilities, dependency-scoped proposal revisions or stable cross-request pagination. Broad existing proposal stale-state gates and write locks remain intact.

## Measurements

Run `python scripts/benchmark_portfolio_runtime.py` with development dependencies. The script takes no database path, isolates application data via the test harness and creates a disposable synthetic SQLite database. It reports elapsed time, SQL execution time/query count, JSON bytes and peak traced Python memory. Samples evict ORM identities; they do not reset the OS cache or measure connector transit, production write-lock duration or concurrent writer throughput.

The recorded [October 22 synthetic results](../benchmarks/portfolio-runtime-2026-10-22.json) use 300 observations and 6,000 classification overrides, with five samples per case, measured on the runtime branch before other handoff features were integrated. They are not a final packaged-release benchmark. Reading the same effective classification using the full override scan had a median 50.871 ms; selecting its 20 scoped rows had a median 0.381 ms and identical output. This isolates that query, not total application speed.

State batches of 1/30/100/200 had full-response medians of 7.367/216.649/746.792/1505.611 ms. The 200-observation compact response retained every tested financial field and digest while decreasing JSON from 465,252 to 428,761 bytes. It took 1531.987 ms and still executed 3,200 queries, so this is a round-trip/payload improvement, not a claim of fewer state-computation queries. SQL time, peak memory and all samples remain in the JSON artifact. The WAL test separately verifies that a concurrent writer cannot mix new state into an in-progress batch read.
