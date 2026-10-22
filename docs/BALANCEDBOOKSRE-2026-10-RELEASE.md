# BalancedBooksRE October maintenance release

Internal-use Mac release 0.2.0. Preserves the source-available license and attribution.

## Included
- Selected upstream fixes: c5b83ba (long reversal memos and safe error handling), a259c25 (audit deleted bill/invoice lines). Other upstream encryption, server, and QuickBooks migration changes were not merged wholesale.
- Immutable effective bank allocations; posted pooled receipt/payment corrections and partial component splits.
- Coordinated same-day posted internal pooled transfers; posted intercompany advances/repayments with unresolved funding retained.
- Posted recognized-item settlement and separate party receivable/payable items without duplicate bank cash.
- Constrained validation of complete existing corrections; complete scoped balances on both bases are required. Missing typed beneficiary associations remain held.
- Atomic command batches, retry-result recovery, expanded preview event effects, observation lineage, effective property filters, exact review filters, and company/schema identity.
- Read-only shortage assessments distinguish coverage uncertainty, possible allocation artifacts, and historical shortfalls followed by later positive balances. They never automatically close alerts.
- CSV/XLSX physical source rows, sheet identifiers, normalized indexes, and provider IDs are retained separately. Existing metadata is not rewritten.

## Validation
The pre-release suite ran 3,777 passing tests with 207 environmental skips and six failures. The six failures were repaired and their affected regression suite passed (87 passed, 3 skipped). Later focused checks passed (48 tests). Migration rehearsal preserved all 117 existing tables byte-for-byte at the row-content level. Seventy-two annual combined/entity financial-report comparisons on the populated backup matched the original code on both supported bases. SQLite allocation lookups were bounded after the populated-copy test found the parameter limit.

## Remaining limitations
This release does not implement every proposed enhancement in the evaluation handoffs. Legacy XLS input, a dedicated review-case/notes system, automatic validated shortage closure, and the historical 134-item evidence crosswalk remain future work. Historical postings and review flags are not automatically remediated.

The internal pooled-transfer correction currently requires both legs already posted, the same entity, and the same bank date. Cross-date transit and mixed posted/unposted pooled routing need further work. Intercompany and party correction variants require full remaining observation amounts. General composite service/prepaid/closing corrections, broader unit/timing combinations, and enrichment of legacy beneficiary associations remain limited. Unsupported or ambiguous cases are rejected or remain open rather than guessed.

Existing-correction validation currently requires the complete economic classification to match on both bases and a single original bank movement. It intentionally cannot certify every legacy multi-leg or differently timed case. Source evidence and confirmation of purpose remain required. An accounting control check is not proof of ownership, funding, complete source coverage, or GAAP-complete financial statements.
