# VDF QA API Endpoints

All endpoints use the API base URL where the service is running. If `VDF_API_KEY` is configured, send it as the `X-API-Key` header on every request except `GET /healthz`.

Examples below use `curl.exe` and PowerShell syntax. Replace the base URL, file names, run ID, and flag code as needed. File paths in the comparison request are paths on the API server and must resolve inside `VDF_INPUT_DIR`.

Both submitted files are checked against the configured entry schema. The completed run summary reports invalid record and schema error counts separately for each file. Schema problem rows identify their source in `source_file`.

## Start a comparison

`POST /runs` - starts a comparison in the background and returns HTTP `202`.

Request body (exactly these three fields):

```json
{
  "title": "Monthly comparison",
  "previous_path": "previous.json",
  "current_path": "current.json"
}
```

Both file paths must resolve to files inside `VDF_INPUT_DIR`.

Response:

```json
{
  "run_id": "20261125T120000000000Z",
  "status": "queued"
}
```

```powershell
curl.exe -X POST "http://localhost:8080/runs" `
  -H "Content-Type: application/json" `
  -d '{"title":"Monthly comparison","previous_path":"previous.json","current_path":"current.json"}'
```

If API-key authentication is enabled, add `-H "X-API-Key: YOUR_API_KEY"`.

## Runs

| Method | Endpoint | Response |
|---|---|---|
| `GET` | `/runs` | Runs with their title, status, verdict, completion time, `progress_percent` (0-100), and `progress_stage`. |
| `GET` | `/runs/{run_id}` | Status, verdict, summary, and run metadata, including `progress_percent` and `progress_stage`. The completed summary includes `current_file_records` and `previous_file_records`. Poll until the status is `done` or `failed`. |

```powershell
curl.exe "http://localhost:8080/runs"
curl.exe "http://localhost:8080/runs/{run_id}"
```

`progress_percent` is the estimated overall completion percentage. `progress_stage` identifies the active step, such as `hashing_current_file`, `validating_previous_file`, `building_distributions`, or `comparing_products`. Completed runs report 100 percent and `completed`.

Example run item from `GET /runs` while processing:

```json
{
  "period": "20261125T120000000000Z",
  "title": "Monthly comparison",
  "status": "running",
  "verdict": null,
  "progress_percent": 63,
  "progress_stage": "loading_previous_snapshot"
}
```

## Flags

| Method | Endpoint | Response |
|---|---|---|
| `GET` | `/runs/{run_id}/flags` | Each flag's code, description, owner, priority, total count, and exactly five top records. |
| `GET` | `/runs/{run_id}/flags/{code}?offset=0&limit=200` | Paginated records after the top five for that flag. `offset` starts at zero; `limit` is 1-5000. Response includes `total_count`, `remaining_count`, `next_offset`, and `has_more`. |
| `GET` | `/runs/{run_id}/schema-errors?offset=0&limit=200` | Paginated schema, duplicate-SKU, and missing-SKU problems. Schema issues include `source_file` to identify whether the previous or current file failed. `offset` starts at zero; `limit` is 1-5000. |

```powershell
# Flag list includes each flag's total_count and its top five records.
curl.exe "http://localhost:8080/runs/{run_id}/flags"

# Fetch more records for one flag. Use next_offset from the previous response.
curl.exe "http://localhost:8080/runs/{run_id}/flags/domain_chg?offset=0&limit=200"

# Fetch the next page for that flag.
curl.exe "http://localhost:8080/runs/{run_id}/flags/domain_chg?offset=200&limit=200"

# Fetch schema and data-integrity problems.
curl.exe "http://localhost:8080/runs/{run_id}/schema-errors?offset=0&limit=200"
```

## Reports and health

| Method | Endpoint | Response |
|---|---|---|
| `GET` | `/runs/{run_id}/report.html` | Downloadable self-contained HTML report. |
| `GET` | `/runs/{run_id}/report.json?offset=0&limit=500` | Report summary, per-group totals, and a page of issue rows. `offset` starts at zero; `limit` is 1-5000. |
| `GET` | `/healthz` | Liveness status. This endpoint does not require the API key. |

```powershell
curl.exe "http://localhost:8080/runs/{run_id}/report.json?offset=0&limit=500"
curl.exe -o "vdf_qa_report.html" "http://localhost:8080/runs/{run_id}/report.html"
curl.exe "http://localhost:8080/healthz"
```
