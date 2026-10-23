# Shared observation reads, legacy exports and storage diagnostics

## Exact request-scoped reads

`observation_state.observation_states(db, ids, through=None)` accepts 1–200 unique positive IDs and returns the complete states in caller order. It pins one SQLite read snapshot, loads the union of explicit observation/correction/source-event links, and builds one ephemeral `LedgerSnapshot`. Each observation keeps its own exact closure; matching amounts or descriptions never establish lineage. Physical ledger rows, all applicable allocation/classification revisions, basis adjustments, reviews and account scopes are loaded in bounded sets. Both accounting bases and observation-date cash lineage derive from the same packet.

The packet retains revision history through its maximum requested date, then selects the effective revision separately at each cutoff. Property filtering follows allocation replacement. Classification/allocation precedence, row ordering, virtual adjustments, dimensions, integer cents and full state digests match the frozen previous readers in `tests/reference_portfolio_readers.py`. Returned projections are copies. No persistent cache, financial guard, proposal dependency gate or write lock changes here.

The benchmark script takes no company/database path and creates synthetic books through the isolated test harness. The recorded [October 23 results](../benchmarks/portfolio-shared-reads-2026-10-23.json) use 300 observations and 6,000 classification revisions, five samples per variant, and assert complete output and digest equality with the frozen reader at 1/30/100/200 observations. It includes serialization, SQL execution time/query counts, JSON byte count and peak traced Python allocation. `ORM-evicted` clears the session identity map; `warm` omits that explicit eviction. Neither resets OS caches or guarantees resident ORM identities. SQL timings cover cursor execution, while elapsed time includes row decoding and Python work. These measurements do not establish production latency, connector transit, writer-lock duration, process RSS, or throughput under concurrent writes.


The ORM-evicted full-response measurements were:

| Observations | Baseline queries | Shared queries | Baseline p50 ms | Shared p50 ms | Full JSON bytes |
|---:|---:|---:|---:|---:|---:|
| 1 | 17–18 | 10 | 7.367 | 4.521 | 2,983 |
| 30 | 510 | 10 | 233.313 | 29.894 | 80,562 |
| 100 | 1700 | 10 | 794.175 | 105.352 | 268,381 |
| 200 | 3400 | 10 | 1663.968 | 245.442 | 538,381 |

At 200 observations, peak traced Python allocation increased from 2,978,858 to 8,531,091 bytes because the packet retains the selected revision history once. Compact output was 501,890 bytes; it reduces payload size without promising faster computation. The ten-query result describes this shallow synthetic graph; deeper ancestry adds bounded discovery rounds.

A separate WAL regression commits a writer change between the shared ledger load and bulk review read; the batch still returns one consistent earlier snapshot and a subsequent reader sees the new value. It tests consistency rather than writer latency. Full synthetic samples and p50/p95 measurements remain in the JSON artifact; p95 of five samples is the sample maximum, not a robust population estimate.

## Value-only XLS normalization

The existing import-preview path additionally accepts genuine OLE BIFF5/7/8 `.xls` workbooks through pinned `xlrd==2.0.2`. It validates BIFF record boundaries and rejects formula/cache, encrypted, malformed and unsupported-version workbooks before normalization. The preflight is necessary because [xlrd extracts formula caches rather than calculating formulas](https://xlrd.readthedocs.io/en/stable/). The reviewed [2.0.2 release](https://pypi.org/project/xlrd/2.0.2/) is pinned in production requirements and included in the macOS bundle. `xlwt==1.3.0` is a development-only synthetic fixture writer.

The header convention is the first worksheet row with unique named columns; callers supply explicit date/amount or debit/credit mappings and a destination bank/card account. Multi-sheet workbooks require an explicit sheet. XLS/XLSX/CSV equivalents retain exact normalized cents/dates, original file SHA-256, selected sheet, physical row, logical record index and equal-row occurrence counts. Both Excel date systems are handled; XLS error/boolean cells, time-of-day date cells, the nonexistent 1900-02-29 serial and subcent amounts fail closed. XLSX formulas even outside mapped columns and binary files mislabeled as CSV are rejected. Limits remain 25 MiB and 20,000 data rows.

Normalization reads original bytes directly, without an intermediate conversion or overwriting a source file. It does not create an attachment archive: retain/link the original export through the existing document/source-location workflow. The raw-byte SHA identifies that original file, not a generated CSV.

New worksheet row identities include the sheet as well as original SHA and logical row. Reimporting an older XLSX observation reuses its legacy identity only when the saved original sheet matches. Two equal rows on one sheet, or equivalent rows on separate sheets, remain distinct observations. Provider transaction IDs retain their existing account-scoped identity rule. Cross-file matches still generate possible-duplicate review instead of silent removal.

## Read-only storage diagnostics

`storage_diagnostics(db)` returns current page size/count, freelist bytes, journal mode, optional table/index B-tree footprints, exact stored JSON/text bytes for requests/proposals/events/reviews/source metadata, and separately sampled database/WAL file sizes. It returns byte counts, not source text or filesystem paths. [SQLite DBSTAT](https://www.sqlite.org/dbstat.html) excludes freelist, pointer-map and lock pages; availability and that scope are explicit. On SQLite builds without DBSTAT the overall and payload measurements remain available.

Filesystem sizes can change independently while a read snapshot is open; they are marked as separately sampled. A missing in-memory file has null sizes, not a fabricated zero. This endpoint performs no checkpoint, vacuum, journal-mode assignment, audit deletion or data migration. Its retention declaration preserves events, request replay, proposals, source identities and correction history. It reports current footprint; trend/growth needs comparing dated measurements. Backup/restore behavior is unchanged.
