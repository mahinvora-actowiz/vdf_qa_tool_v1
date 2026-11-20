# VDF QA Tool

Monthly quality-assurance backend for vendor data files (VDF). It runs the client
schema and the VDF Review Flags against one data file, compares it with the previous
month SKU by SKU, stores the issues, and produces a dashboard API and an HTML report.

For each data file the tool:

1. Streams the file once (no full load). Each record is validated against the
   client schema, checked for duplicate and missing SKUs, and stored as a snapshot
   row keyed by `(period, sku)`.
2. Reuses the previous month's snapshot when its SHA-256 matches, otherwise ingests it.
3. Builds the rank statistics used by the flags (top 5, rare 5%, shortest/longest 5%,
   lowest/highest 5%) from the current file, in MongoDB, not RAM.
4. Compares every SKU present in both months and evaluates the flag catalogue only on
   fields whose value changed.
5. Stores issues with an owner (crawler or QA) and computes a verdict.
6. Serves the results over an API and writes a self-contained HTML report.

Storage is MongoDB. Snapshots, rank counters, runs, and issues are stored in the
database selected by `VDF_MONGODB_DATABASE`.

## Setup and run

Install dependencies with your system Python, set `VDF_MONGODB_URI` in `.env`,
then start the API:

```powershell
python -m pip install -r requirements.txt
python main.py
```

The API uses the host and port from `VDF_HOST` and `VDF_PORT` in `.env` (defaults:
`0.0.0.0:8080`). MongoDB must be reachable at `VDF_MONGODB_URI`.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `VDF_MONGODB_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `VDF_MONGODB_DATABASE` | `vdf_qa` | MongoDB database name |
| `VDF_INPUT_DIR` | `./inbox` | The API reads data files only from this folder |
| `VDF_SCHEMA_DIR` | `./schemas` | Client schema files (every `*.json` in the folder is loaded) |
| `VDF_ENTRY_SCHEMA` | `TransformVendorProduct.schema.json` | Schema each record is validated against |
| `VDF_REJECT_ON_SCHEMA_ERROR` | `1` | Schema-invalid records make the run Rejected |
| `VDF_MAX_FLAG_HITS` | unset | Optional cap on High-priority hits. Unset means no cap |
| `VDF_API_KEY` | unset | If set, every API request must send `X-API-Key` |

`schemas/` holds the client's `TransformVendorProduct.schema.json` and
`AssetIndex.schema.json`. The `ICS` scheme file that was shared earlier also works with
`--entry-schema` (see the notes below).

## CLI

```bash
# Run one month (the period is read from the file name when it follows data_file_YYYY_MM_DD)
python -m vdf_qa run --current <current.json> --previous <previous.json> \
    --period 2026-09-15 --previous-period 2026-08-31

# Write the HTML report for that run
python -m vdf_qa report --period 2026-09-15 --out report_2026_09_15.html

# Start the API (requires fastapi + uvicorn)
python -m vdf_qa serve --host 127.0.0.1 --port 8000
```

Pass `--period` and `--previous-period` explicitly for files whose names do not follow
the `data_file_YYYY_MM_DD` pattern, or whose previous file is not a month-end. The ICS
samples are one example (`2026-08-31-v1`, `2026-09-15-v1`, `2026-10-22-v2`).

## API (when `serve` is running)

| Method | Path | Purpose |
|---|---|---|
| POST | `/runs` | Start a run in the background (202). Body: `current_path`, optional `previous_path`, `period`, `previous_period` (YYYY-MM-DD). Paths must be inside `VDF_INPUT_DIR`. |
| GET | `/runs` | All runs with verdicts |
| GET | `/runs/{period}` | Run status and summary |
| GET | `/runs/{period}/flags?top=5` | Per flag: count, owner, description, top N SKUs |
| GET | `/runs/{period}/flags/{code}?offset&limit` | Ranked SKUs for one flag (the list below the top 5) |
| GET | `/runs/{period}/schema-errors?offset&limit` | Schema, duplicate and missing-SKU problems |
| GET | `/runs/{period}/report.html` | Downloadable HTML report |
| GET | `/healthz` | Liveness |

One run executes at a time (process lock). A server restart stops a run in progress, so
POST it again. Completed runs stay in MongoDB.

## Verdict

The VDF Review Flags reference says that flags are review cues, not findings, and that
their High/Medium classification is not finding severity. The verdict therefore depends
only on hard data failures:

| Verdict | When |
|---|---|
| **Rejected** | Schema-invalid records (when `VDF_REJECT_ON_SCHEMA_ERROR=1`), duplicate SKUs, records without a SKU, or High hits above `VDF_MAX_FLAG_HITS` if that is set |
| **Review required** | No hard failure, but at least one flag was hit |
| **Accepted** | No hard failure and no flag hits |

The SOP may define other acceptance rules. Those would change the verdict block at
step 5 of `run_period` in `vdf_qa/pipeline.py`.

## Owner routing

The reference does not define owners. The routing used is:

| Issue | Owner |
|---|---|
| Schema, duplicate SKU, missing SKU | crawler |
| Flags with priority High or Medium | crawler |
| Gained / Lost review cues | QA |

This is a configurable assumption (`owner_for` in `vdf_qa/issues_store.py`). Confirm
it against the SOP.

## Report: feed-wide banner

When a flag fires on at least 50% of the compared rows (and at least 50 rows were
compared), the report shows a "Likely feed-wide changes" banner above the flag
sections. The rows are still listed. The banner only tells the reviewer that the cause
is probably the vendor feed format and not individual products.

## Schema notes

- `TransformVendorProduct` is the transform-stage schema. It requires `sha256` and
  `media_type` on `main_image` and on every downloaded asset. The client's
  `vendor-export-example.json` is scrape-stage data, so it fails this schema. That
  failure is expected and is covered by a test.
- The client's TransformVendorProduct HTML doc shows a `main_image` example with only
  `source`. The raw schema rejects that shape. Ask the client which one is correct.
- `parsed_value` is optional in the schema. The ICS samples carry only `raw_value`, so
  `no_parsed` fires on most rows. That is the reference's definition of the flag and
  the banner makes it visible.
- `AssetIndex.schema.json` describes a separate asset index file. The monthly run does
  not validate it.
- The uploaded `ICS` scheme differs from `TransformVendorProduct` only in how
  `main_image` and `assets` are described. Both accept the ICS samples.

## Flag codes

Codes match the VDF Review Flags reference: `domain_chg`, `path_chg`, `query_chg`, and
so on. The shared base filters (`gained`, `lost`, `new_value_present`, and so on) are
view filters in the reference. Here only `gained` and `lost` produce cues. The
"All prices numeric" base filter (`no_nonnum`) is not produced as a cue.

Priorities for all flags in the catalogue were checked against the reference: no
mismatches.

## Tests

```bash
pytest -q
```

| File | Covers |
|---|---|
| `test_core.py` | streaming reader, bounded Levenshtein, schema cross-file refs, flag predicates, ranking |
| `test_pipeline.py` | end-to-end run on synthetic data: duplicates, missing SKU, schema failure, reuse, HTML escaping |
| `test_real_schema.py` | the client schema against the ICS samples (first 50 rows) and the vendor example |
| `test_verdict_report.py` | verdict rules, optional cap, feed-wide banner |
| `test_api.py` | API auth, input-folder restriction, period validation, full run then flags and report. Needs FastAPI. |

Test status when this package was built: 37 tests pass. `test_api.py` was NOT
executed in the build environment because FastAPI was not installed there. Run
`pytest -q` on your machine with `pip install -r requirements.txt` to execute it.

## Performance (measured on synthetic data)

- 100,000 records, 1 in 5 changed: about 86 s for the full run, peak memory about 48 MB.
  Measured before the real schema was used.
- The real schema on the ICS samples: 4,000 records (two files) in about 2.5 s.
- Schema validation is the largest cost (about 60% in a profile). Expect roughly
  30 minutes for 2 million records on one core. Memory stays flat.
- If the window is tight, the next step is a compiled validator for the entry schema.

## Known gaps

- MongoDB stores snapshots, rank counters, runs, and issues.
- The API runs jobs in a thread. Jobs do not survive a restart.
- Lead-time flags that compare parsed values (`no_parsed`, `parsed1`, and related) need
  parsed values. The ICS data does not include them.
- Owner routing and acceptance rules come from assumptions, not the SOP.

## Layout

```
vdf_qa/
  jsonstream.py     streaming reader for a large top-level JSON array
  schema_check.py   Draft-7 validation, cross-file $ref through a referencing registry
  snapshot.py       monthly snapshots in MongoDB, keyed by (month, sku)
  distribution.py   disk-backed counters for the rank-based flags
  rules.py          feature extraction, flag catalogue, evaluator
  issues_store.py   runs and issues tables, ranking queries
  pipeline.py       run orchestration and verdict
  report.py         self-contained HTML report with the feed-wide banner
  api.py            FastAPI layer
  cli.py            command-line entry point
  config.py         environment settings
schemas/            client schemas (TransformVendorProduct, AssetIndex)
tests/              pytest suite, synthetic generator, real-data fixtures
```
