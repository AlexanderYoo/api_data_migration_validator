#!/usr/bin/env python3
"""Compare API responses between two CSV input files.

Each input CSV represents one system side (A and B). The script loads rows,
calls each API, compares response bodies, and writes a detailed report CSV.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from time import perf_counter
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


@dataclass
class ApiRecord:
    """Single API test row loaded from input CSV."""

    key: str
    method: str
    api_url: str
    request_header: Optional[str] = None
    request_body: Optional[str] = None


@dataclass
class HttpResult:
    """Captured request/response data (or error) for one API call."""

    request_header: Optional[str]
    request_body: Optional[str]
    response_header: Optional[str]
    response_body: Optional[str]
    start_time_utc: str
    end_time_utc: str
    response_time_ms: Optional[float]
    error: Optional[str]


@dataclass
class ComparisonRow:
    """Final report row for one key comparison between side A and side B."""

    test_index: int
    key: str
    method_a: Optional[str]
    method_b: Optional[str]
    api_url_a: Optional[str]
    api_url_b: Optional[str]
    request_header_a: Optional[str]
    request_header_b: Optional[str]
    request_body_a: Optional[str]
    request_body_b: Optional[str]
    response_header_a: Optional[str]
    response_header_b: Optional[str]
    response_body_a: Optional[str]
    response_body_b: Optional[str]
    start_time_utc_a: Optional[str]
    end_time_utc_a: Optional[str]
    start_time_utc_b: Optional[str]
    end_time_utc_b: Optional[str]
    response_time_ms_a: Optional[float]
    response_time_ms_b: Optional[float]
    response_time_diff_ms: Optional[float]
    status_result: str
    description: str


def parse_args() -> argparse.Namespace:
    """Parse and return command-line arguments."""

    parser = argparse.ArgumentParser(description="Compare APIs from two CSV files")

    parser.add_argument(
        "--csv-a",
        default=None,
        help="Input CSV file A",
    )
    parser.add_argument(
        "--csv-b",
        default=None,
        help="Input CSV file B",
    )

    parser.add_argument("--key-column-a", default="api_name", help="Key column in CSV A")
    parser.add_argument("--key-column-b", default="api_name", help="Key column in CSV B")
    parser.add_argument("--method-column-a", default="method", help="HTTP method column in CSV A")
    parser.add_argument("--method-column-b", default="method", help="HTTP method column in CSV B")
    parser.add_argument("--api-column-a", default="api_url", help="API URL column in CSV A")
    parser.add_argument("--api-column-b", default="api_url", help="API URL column in CSV B")
    parser.add_argument(
        "--request-header-column-a",
        default="request_header",
        help="Request header column in CSV A (JSON text)",
    )
    parser.add_argument(
        "--request-header-column-b",
        default="request_header",
        help="Request header column in CSV B (JSON text)",
    )
    parser.add_argument(
        "--request-body-column-a",
        default="request_body",
        help="Request body column in CSV A (JSON text or string)",
    )
    parser.add_argument(
        "--request-body-column-b",
        default="request_body",
        help="Request body column in CSV B (JSON text or string)",
    )

    parser.add_argument("--timeout-seconds", type=float, default=20.0, help="HTTP timeout")
    parser.add_argument("--limit", type=int, default=None, help="Optional max rows from each CSV")
    parser.add_argument("--report-csv", default="comparison_report.csv", help="Output report CSV path")

    return parser.parse_args()


def normalize_value(raw: Any) -> Any:
    """Normalize scalar/JSON-like input into a comparable Python value."""

    if raw is None:
        return None
    if isinstance(raw, (dict, list, int, float, bool)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")

    text_value = str(raw).strip()
    if text_value == "":
        return ""

    try:
        return json.loads(text_value)
    except json.JSONDecodeError:
        return text_value


def canonicalize(raw: Any) -> str:
    """Convert any value into deterministic JSON text for equality checks."""

    return json.dumps(
        normalize_value(raw),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def as_text(raw: Any) -> Optional[str]:
    """Render values as text for report output without request-specific formatting."""

    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return json.dumps(raw, ensure_ascii=False, sort_keys=True)
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def as_request_text(raw: Any) -> Optional[str]:
    """Render request values, using single quotes for object/list readability."""

    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return json.dumps(raw, ensure_ascii=False, sort_keys=True).replace('"', "'")
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")

    text = str(raw)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text

    if isinstance(parsed, (dict, list)):
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True).replace('"', "'")
    return text


def empty_to_none(value: Optional[str]) -> Optional[str]:
    """Treat empty/whitespace-only strings as None."""

    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def parse_header_text(raw: Optional[str]) -> Tuple[Dict[str, str], Optional[str]]:
    """Parse request-header text into a string dictionary with an error message on failure."""

    if raw is None:
        return {}, None

    try:
        parsed = parse_json_like(raw)
    except json.JSONDecodeError as exc:
        return {}, f"Invalid request header JSON: {exc}"

    if not isinstance(parsed, dict):
        return {}, "Invalid request header JSON: expected object"

    return {str(k): str(v) for k, v in parsed.items()}, None


def parse_body_text(raw: Optional[str]) -> Any:
    """Parse request-body text as JSON-like content; fallback to raw text."""

    if raw is None:
        return None
    try:
        return parse_json_like(raw)
    except json.JSONDecodeError:
        return raw


def parse_json_like(raw: str) -> Any:
    """Parse JSON while tolerating common CSV-escaping and malformed-key patterns."""

    text = raw.strip()
    if text == "":
        raise json.JSONDecodeError("Empty JSON text", text, 0)

    candidates: List[str] = [text]

    # Handle common CSV-escaped JSON such as {\"k\":\"v\"}
    candidates.append(text.replace('\\"', '"'))

    # Handle malformed header values like {Accept":"application/json"}"
    fixed_unquoted = re.sub(r'([{\[,]\s*)([A-Za-z0-9_.-]+)\s*:', r'\1"\2":', text)
    fixed_unquoted = re.sub(r'([{\[,]\s*)([A-Za-z0-9_.-]+)"\s*:', r'\1"\2":', fixed_unquoted)
    fixed_unquoted = fixed_unquoted.strip().strip('"').strip("'")
    candidates.append(fixed_unquoted)
    candidates.append(fixed_unquoted.replace('\\"', '"'))

    seen: set[str] = set()
    last_error: Optional[json.JSONDecodeError] = None
    for candidate in candidates:
        if candidate in seen or candidate == "":
            continue
        seen.add(candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise json.JSONDecodeError("Invalid JSON text", text, 0)


def require_columns(headers: List[str], required_columns: List[str], label: str) -> None:
    """Raise an error when required columns are missing from a CSV header row."""

    missing = [col for col in required_columns if col not in headers]
    if missing:
        raise RuntimeError(f"{label} missing required columns: {', '.join(missing)}")


def load_records_from_csv(
    path: str,
    label: str,
    key_column: str,
    method_column: str,
    api_column: str,
    request_header_column: str,
    request_body_column: str,
    limit: Optional[int],
) -> Dict[str, ApiRecord]:
    """Load API records from one CSV file into a key-indexed dictionary."""

    records: Dict[str, ApiRecord] = {}

    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise RuntimeError(f"{label} has no header row")

            require_columns(
                reader.fieldnames,
                [key_column, method_column, api_column],
                label,
            )

            for idx, row in enumerate(reader, start=1):
                if limit is not None and idx > limit:
                    break

                key = empty_to_none(row.get(key_column))
                method = empty_to_none(row.get(method_column))
                api_url = empty_to_none(row.get(api_column))

                if key is None:
                    continue
                if method is None or api_url is None:
                    continue

                records[str(key)] = ApiRecord(
                    key=str(key),
                    method=str(method).upper(),
                    api_url=str(api_url),
                    request_header=empty_to_none(row.get(request_header_column)),
                    request_body=empty_to_none(row.get(request_body_column)),
                )
    except OSError as exc:
        raise RuntimeError(f"Failed reading {label} file '{path}': {exc}") from exc

    return records


def call_api(
    session: requests.Session,
    record: ApiRecord,
    timeout_seconds: float,
) -> HttpResult:
    """Execute one API call and return structured request/response details."""

    request_header_text = record.request_header
    request_body_text = record.request_body

    headers, header_err = parse_header_text(request_header_text)
    if header_err:
        return HttpResult(
            request_header=as_request_text(request_header_text),
            request_body=as_request_text(request_body_text),
            response_header=None,
            response_body=None,
            error=header_err,
        )

    body = parse_body_text(request_body_text)
    started_at = datetime.now(timezone.utc).isoformat()
    started = perf_counter()
    try:
        # Send structured bodies via `json` and plain content via `data`.
        response = session.request(
            method=record.method,
            url=record.api_url,
            headers=headers,
            json=body if isinstance(body, (dict, list)) else None,
            data=None if isinstance(body, (dict, list)) else body,
            timeout=timeout_seconds,
        )
        elapsed_ms = (perf_counter() - started) * 1000
        ended_at = datetime.now(timezone.utc).isoformat()
        return HttpResult(
            request_header=as_request_text(headers),
            request_body=as_request_text(body),
            response_header=as_text(dict(response.headers)),
            response_body=response.text,
            start_time_utc=started_at,
            end_time_utc=ended_at,
            response_time_ms=elapsed_ms,
            error=None,
        )
    except requests.RequestException as exc:
        elapsed_ms = (perf_counter() - started) * 1000
        ended_at = datetime.now(timezone.utc).isoformat()
        return HttpResult(
            request_header=as_request_text(headers),
            request_body=as_request_text(body),
            response_header=None,
            response_body=None,
            start_time_utc=started_at,
            end_time_utc=ended_at,
            response_time_ms=elapsed_ms,
            error=str(exc),
        )


def compare_response_bodies(left: Optional[str], right: Optional[str]) -> bool:
    """Compare response bodies after canonical normalization."""

    return canonicalize(left) == canonicalize(right)


def _short_value(value: Any, max_len: int = 120) -> str:
    """Render a compact value preview for mismatch descriptions."""

    text = canonicalize(value)
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3]}..."


def describe_response_mismatch(left: Optional[str], right: Optional[str], max_items: int = 5) -> str:
    """Build a concise reason for body mismatch (key/index/value differences)."""

    left_value = normalize_value(left)
    right_value = normalize_value(right)
    details: List[str] = []

    def add_detail(message: str) -> None:
        if len(details) < max_items:
            details.append(message)

    def walk(path: str, a: Any, b: Any) -> None:
        if len(details) >= max_items:
            return

        if isinstance(a, dict) and isinstance(b, dict):
            keys_a = set(a.keys())
            keys_b = set(b.keys())

            for key in sorted(keys_a - keys_b):
                add_detail(f"{path}.{key} missing in B")
            for key in sorted(keys_b - keys_a):
                add_detail(f"{path}.{key} missing in A")

            for key in sorted(keys_a & keys_b):
                walk(f"{path}.{key}", a[key], b[key])
            return

        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                add_detail(f"{path} length A={len(a)} B={len(b)}")
            for i, (item_a, item_b) in enumerate(zip(a, b)):
                walk(f"{path}[{i}]", item_a, item_b)
            return

        if canonicalize(a) != canonicalize(b):
            add_detail(f"{path} A={_short_value(a)} B={_short_value(b)}")

    walk("$", left_value, right_value)
    if not details:
        details = [f"$ A={_short_value(left_value)} B={_short_value(right_value)}"]

    suffix = ""
    if len(details) >= max_items:
        suffix = " (showing first differences)"
    return f"Response body mismatch: {'; '.join(details)}{suffix}"


def compare_records(
    records_a: Dict[str, ApiRecord],
    records_b: Dict[str, ApiRecord],
    timeout_seconds: float,
) -> List[ComparisonRow]:
    """Compare all keys across both sources and build report rows."""

    rows: List[ComparisonRow] = []
    all_keys = sorted(set(records_a.keys()) | set(records_b.keys()))
    session = requests.Session()

    for test_index, key in enumerate(all_keys, start=1):
        rec_a = records_a.get(key)
        rec_b = records_b.get(key)

        if rec_a is None:
            rows.append(
                ComparisonRow(
                    test_index=test_index,
                    key=key,
                    method_a=None,
                    method_b=rec_b.method,
                    api_url_a=None,
                    api_url_b=rec_b.api_url,
                    request_header_a=None,
                    request_header_b=as_request_text(rec_b.request_header),
                    request_body_a=None,
                    request_body_b=as_request_text(rec_b.request_body),
                    response_header_a=None,
                    response_header_b=None,
                    response_body_a=None,
                    response_body_b=None,
                    start_time_utc_a=None,
                    end_time_utc_a=None,
                    start_time_utc_b=None,
                    end_time_utc_b=None,
                    response_time_ms_a=None,
                    response_time_ms_b=None,
                    response_time_diff_ms=None,
                    status_result="fail",
                    description="Key exists only in CSV B",
                )
            )
            continue

        if rec_b is None:
            rows.append(
                ComparisonRow(
                    test_index=test_index,
                    key=key,
                    method_a=rec_a.method,
                    method_b=None,
                    api_url_a=rec_a.api_url,
                    api_url_b=None,
                    request_header_a=as_request_text(rec_a.request_header),
                    request_header_b=None,
                    request_body_a=as_request_text(rec_a.request_body),
                    request_body_b=None,
                    response_header_a=None,
                    response_header_b=None,
                    response_body_a=None,
                    response_body_b=None,
                    start_time_utc_a=None,
                    end_time_utc_a=None,
                    start_time_utc_b=None,
                    end_time_utc_b=None,
                    response_time_ms_a=None,
                    response_time_ms_b=None,
                    response_time_diff_ms=None,
                    status_result="fail",
                    description="Key exists only in CSV A",
                )
            )
            continue

        result_a = call_api(session, rec_a, timeout_seconds)
        result_b = call_api(session, rec_b, timeout_seconds)
        time_diff_ms: Optional[float] = None
        if result_a.response_time_ms is not None and result_b.response_time_ms is not None:
            time_diff_ms = abs(result_a.response_time_ms - result_b.response_time_ms)

        if result_a.error or result_b.error:
            description = f"A error: {result_a.error or 'none'} | B error: {result_b.error or 'none'}"
            rows.append(
                ComparisonRow(
                    test_index=test_index,
                    key=key,
                    method_a=rec_a.method,
                    method_b=rec_b.method,
                    api_url_a=rec_a.api_url,
                    api_url_b=rec_b.api_url,
                    request_header_a=result_a.request_header,
                    request_header_b=result_b.request_header,
                    request_body_a=result_a.request_body,
                    request_body_b=result_b.request_body,
                    response_header_a=result_a.response_header,
                    response_header_b=result_b.response_header,
                    response_body_a=result_a.response_body,
                    response_body_b=result_b.response_body,
                    start_time_utc_a=result_a.start_time_utc,
                    end_time_utc_a=result_a.end_time_utc,
                    start_time_utc_b=result_b.start_time_utc,
                    end_time_utc_b=result_b.end_time_utc,
                    response_time_ms_a=result_a.response_time_ms,
                    response_time_ms_b=result_b.response_time_ms,
                    response_time_diff_ms=time_diff_ms,
                    status_result="fail",
                    description=description,
                )
            )
            continue

        same_body = compare_response_bodies(result_a.response_body, result_b.response_body)
        status = "success" if same_body else "fail"
        description = (
            "Response body match"
            if same_body
            else describe_response_mismatch(result_a.response_body, result_b.response_body)
        )

        rows.append(
            ComparisonRow(
                test_index=test_index,
                key=key,
                method_a=rec_a.method,
                method_b=rec_b.method,
                api_url_a=rec_a.api_url,
                api_url_b=rec_b.api_url,
                request_header_a=result_a.request_header,
                request_header_b=result_b.request_header,
                request_body_a=result_a.request_body,
                request_body_b=result_b.request_body,
                response_header_a=result_a.response_header,
                response_header_b=result_b.response_header,
                response_body_a=result_a.response_body,
                response_body_b=result_b.response_body,
                start_time_utc_a=result_a.start_time_utc,
                end_time_utc_a=result_a.end_time_utc,
                start_time_utc_b=result_b.start_time_utc,
                end_time_utc_b=result_b.end_time_utc,
                response_time_ms_a=result_a.response_time_ms,
                response_time_ms_b=result_b.response_time_ms,
                response_time_diff_ms=time_diff_ms,
                status_result=status,
                description=description,
            )
        )

    return rows


def write_report(rows: List[ComparisonRow], path: str) -> None:
    """Write comparison rows to CSV."""

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "test_index",
                "key",
                "method_a",
                "method_b",
                "api_url_a",
                "api_url_b",
                "request_header_a",
                "request_header_b",
                "request_body_a",
                "request_body_b",
                "response_header_a",
                "response_header_b",
                "response_body_a",
                "response_body_b",
                "start_time_utc_a",
                "end_time_utc_a",
                "start_time_utc_b",
                "end_time_utc_b",
                "response_time_ms_a",
                "response_time_ms_b",
                "response_time_diff_ms",
                "status_result",
                "description",
            ]
        )
        for item in rows:
            writer.writerow(
                [
                    item.test_index,
                    item.key,
                    item.method_a,
                    item.method_b,
                    item.api_url_a,
                    item.api_url_b,
                    item.request_header_a,
                    item.request_header_b,
                    item.request_body_a,
                    item.request_body_b,
                    item.response_header_a,
                    item.response_header_b,
                    item.response_body_a,
                    item.response_body_b,
                    item.start_time_utc_a,
                    item.end_time_utc_a,
                    item.start_time_utc_b,
                    item.end_time_utc_b,
                    item.response_time_ms_a,
                    item.response_time_ms_b,
                    item.response_time_diff_ms,
                    item.status_result,
                    item.description,
                ]
            )


def build_report_path(base_path: str) -> Path:
    """Build output path with a YYYYMMDD suffix."""

    input_path = Path(base_path)
    suffix = input_path.suffix or ".csv"
    dated_name = f"{input_path.stem}_{datetime.now().strftime('%Y%m%d')}{suffix}"
    return input_path.with_name(dated_name).resolve()


def print_summary(rows: List[ComparisonRow]) -> None:
    """Print aggregate pass/fail summary and failure lines."""

    total = len(rows)
    success = sum(1 for r in rows if r.status_result == "success")
    fail = total - success

    print("Comparison complete")
    print(f"Total tests: {total}")
    print(f"success: {success}")
    print(f"fail: {fail}")

    for row in rows:
        if row.status_result == "fail":
            print(f"[fail] test_index={row.test_index} key={row.key} -> {row.description}")


def main() -> int:
    """Program entry point."""

    args = parse_args()
    csv_a = args.csv_a or "sample_apis_a.csv"
    csv_b = args.csv_b or "sample_apis_b.csv"

    try:
        records_a = load_records_from_csv(
            path=csv_a,
            label="CSV A",
            key_column=args.key_column_a,
            method_column=args.method_column_a,
            api_column=args.api_column_a,
            request_header_column=args.request_header_column_a,
            request_body_column=args.request_body_column_a,
            limit=args.limit,
        )
        records_b = load_records_from_csv(
            path=csv_b,
            label="CSV B",
            key_column=args.key_column_b,
            method_column=args.method_column_b,
            api_column=args.api_column_b,
            request_header_column=args.request_header_column_b,
            request_body_column=args.request_body_column_b,
            limit=args.limit,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Comparison triggers live API calls for matching keys from both files.
    rows = compare_records(records_a=records_a, records_b=records_b, timeout_seconds=args.timeout_seconds)

    report_path = build_report_path(args.report_csv)
    write_report(rows, str(report_path))
    print_summary(rows)
    print(f"Report written to: {report_path}")
    print("Test is complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
