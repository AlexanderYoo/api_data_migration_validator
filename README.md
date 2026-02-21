# API Data Migration Validator

Python script for API comparison from two CSV files.

## Input CSVs
Each CSV should contain one row per API test and include:
- API name (key used to match rows between A and B)
- REST method (`GET`, `POST`, `PUT`, `DELETE`, etc.)
- API URL
- Request header (JSON object text)
- Request body (JSON text or plain text)

## What the script does
- Loads CSV A and CSV B.
- Matches records using API name/key.
- Calls APIs from both sides using each row's method, URL, headers, and body.
- Captures response header and response body for both sides.
- Compares response body only.
- Writes a report CSV with test result and failure reason.

## Request parsing and formatting behavior
- Request header parsing is tolerant of common CSV/JSON formatting issues:
  - Escaped quotes such as `{\"Accept\":\"application/json\"}`
  - Unquoted keys such as `{Accept:"application/json"}`
  - Malformed key pattern such as `{Accept":"application/json"}"`
- Request body parsing is also tolerant for JSON-like values; non-JSON content is sent as plain text.
- In the report CSV, request header/body values that are objects/lists are written using single quotes (example: `{'Accept': 'application/json'}`) for readability.
- Response header/body fields remain standard JSON/text output.

## Install
```powershell
pip install -r requirements.txt
```

## Usage
```powershell
python compare_api_data.py `
  --csv-a "apis_a.csv" `
  --csv-b "apis_b.csv" `
  --key-column-a "api_name" `
  --key-column-b "api_name" `
  --method-column-a "method" `
  --method-column-b "method" `
  --api-column-a "api_url" `
  --api-column-b "api_url" `
  --request-header-column-a "request_header" `
  --request-header-column-b "request_header" `
  --request-body-column-a "request_body" `
  --request-body-column-b "request_body" `
  --timeout-seconds 20 `
  --limit 100 `
  --report-csv "comparison_report.csv"
```

## Required columns
Per file, these columns are required (names are configurable with args):
- key (`api_name` by default)
- method (`method` by default)
- api url (`api_url` by default)

Request header/body columns are optional but recommended.

## Report CSV columns
- `test_index`
- `key`
- `method_a`, `method_b`
- `api_url_a`, `api_url_b`
- `request_header_a`, `request_header_b`
- `request_body_a`, `request_body_b`
- `response_header_a`, `response_header_b`
- `response_body_a`, `response_body_b`
- `status_result` (`success` or `fail`)
- `description`

## Notes
- `status_result=success` only when response bodies are equal after JSON normalization.
- Invalid request-header JSON or HTTP request failures are written as `fail` with reason in `description`.
- Output file name is date-suffixed automatically (example: `comparison_report_20260221.csv`).
