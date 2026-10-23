# Stream only financial revision fields

`LedgerSnapshot` previously loaded complete allocation/classification ORM objects, including evidence text unused by its projections, then copied every decoded allocation component list. It now selects only line ID, effective date, event ID and cash-flow/components, fetching at most 256 result rows at a time. Scalar JSON selections decode independent values, so the packet retains those values without a redundant copy. Evidence remains unchanged in its audit tables.

Every applicable revision is still retained. Effective dates, same-day ID ordering, allocation/classification precedence, both accounting bases and output fields are unchanged. This is request-local loading, with no persistent cache or new transaction behavior. Tests also verify that mutating an already-loaded ORM JSON value or a returned projection cannot mutate the packet.

Run `python -m tests.benchmark_revision_reads` from the checkout to reproduce the [recorded synthetic measurement](../benchmarks/portfolio-revision-columns-2026-10-23.json). The script reads the prior loader from pinned Git commit `5da53e6`, isolates application paths through the test harness, and creates a disposable SQLite database. It accepts no database path.

The fixture contains 30 observations, 20 allocation and 20 classification revisions per source line: 1,200 revision rows and 10,752,000 stored bytes of evidence prose. Each sample loads the packet and serializes three dated projections on both bases. Complete output equality is asserted.

| Five ORM-evicted samples | Previous loader | Financial-column loader |
|---|---:|---:|
| Median elapsed milliseconds | 42.326 | 34.422 |
| Maximum sample milliseconds | 82.350 | 41.090 |
| Maximum traced Python allocation bytes | 7,526,480 | 1,470,025 |
| SQL queries per sample | 4 | 4 |
| Serialized output bytes | 214,065 | 214,065 |

These numbers measure this allocation-heavy synthetic packet, not full application or production latency. Serialization is included; OS caches are uncontrolled. Traced Python allocations are not process RSS. The five-sample p95 is only the sample maximum. No connector, writer-lock or concurrent-throughput result is claimed, and retained financial history still grows with the selected scope.
