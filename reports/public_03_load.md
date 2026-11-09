# Load test — public_03_load (12 workflows, full per-day rate)

Machine: developer laptop (Apple Silicon, 8 cores), single controller process, SQLite WAL. Simulator and controller on the same machine.

- simulator world + history generation: 16 s (simulator cost, not ours)
- history bootstrap (C3): 1,337,471 events ingested in 16.6 s → 80,728 events/s

| sim day | events | controller ingest s | controller decide s | simulator advance s | wall-clock s | ingest events/s | decisions |
|---|---|---|---|---|---|---|---|
| D0 | 840,062 | 13.7 | 5.4 | 11.4 | 30 | 61,456 | 131 |
| D1 | 840,052 | 13.9 | 7.0 | 12.7 | 34 | 60,340 | 121 |
| D2 | 0 | 0.0 | 0.1 | 0.0 | 0 | 0 | 4 |

- decision step per tick (all 12 workflows): p50 0.05 s, p95 0.31 s, max 1.00 s
- ingest lag at decision time: max 0 events (we always drain every chunk before deciding); decision freshness is therefore bounded by the 15-minute tick
- peak controller RSS: 458 MB
- controller SQLite store: 118 MB; simulator event files: 1.4 GB
- total controller run: 81 s

