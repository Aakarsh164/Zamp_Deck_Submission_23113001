"""Verify the deployed app's authenticated demo paths with only the Python standard library."""
import argparse
import json
import os
import runpy
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
API_KEYS = ("GEMINI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY")


class SmokeTestError(RuntimeError):
    pass


def admin_headers(headers=None):
    token = os.getenv("ADMIN_TOKEN", "")
    if not token:
        raise SmokeTestError("Set ADMIN_TOKEN in this process to authenticate smoke-test requests")
    return {**(headers or {}), "X-Admin-Token": token}


def request(base_url, path, data=None, headers=None, timeout=180):
    req = Request(base_url.rstrip("/") + path, data=data, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise SmokeTestError(f"{path} returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise SmokeTestError(f"{path} could not be reached: {exc.reason}") from exc


def read_events(body, path):
    try:
        return [json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeTestError(f"{path} did not return valid newline-delimited JSON") from exc


def require(condition, message):
    if not condition:
        raise SmokeTestError(message)


def check_public_paths(base_url):
    status, body = request(base_url, "/healthz")
    require(status == 200 and json.loads(body)["status"] == "ok", "Health check failed")
    print("PASS /healthz")

    status, body = request(base_url, "/")
    require(status == 200 and b"Forward-to-Decide" in body, "Home page check failed")
    print("PASS home page")

    status, body = request(base_url, "/api/samples", headers=admin_headers())
    samples = json.loads(body)
    require(status == 200 and any(sample.get("key") == "clean" for sample in samples),
            "Samples endpoint did not include the clean sample")
    print("PASS samples")

    status, body = request(base_url, "/api/gauntlet", data=b"", headers={
        "Content-Type": "application/json",
        **admin_headers(),
    })
    result = json.loads(body)
    require(status == 200 and result.get("total", 0) > 0
            and result.get("correct", 0) <= result["total"],
            "Gauntlet response was invalid")
    print(f"PASS Gauntlet ({result['correct']}/{result['total']})")

    status, body = request(base_url, "/api/sample/clean", data=b"", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        **admin_headers(),
    })
    events = read_events(body, "/api/sample/clean")
    decision = next((event for event in events if event.get("type") == "decision"), None)
    require(status == 200 and decision is not None, "Clean sample run did not produce a decision")
    print("PASS clean sample run")


def check_real_pdf_upload(base_url):
    fixture_dir = ROOT / "test_invoices" / "smoke-test"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TEST_INVOICE_DIR"] = str(fixture_dir)
    runpy.run_path(str(ROOT / "make_test_pdfs.py"))
    pdf_path = fixture_dir / "1_clean_northwind.pdf"
    boundary = "----forward-to-decide-" + uuid.uuid4().hex
    pdf = pdf_path.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{pdf_path.name}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + pdf + f"\r\n--{boundary}--\r\n".encode()
    status, response_body = request(
        base_url,
        "/api/run",
        data=body,
        headers=admin_headers({
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }),
    )
    events = read_events(response_body, "/api/run")
    decision = next((event for event in events if event.get("type") == "decision"), None)
    extract_stage = next(
        (event for event in events
         if event.get("type") == "stage" and event.get("name") == "Extract"
         and event.get("status") == "done"),
        None,
    )
    require(status == 200 and decision is not None, "PDF upload did not produce a decision")
    require(extract_stage and "Extractor:" in extract_stage.get("summary", ""),
            "PDF upload did not report the successful extractor")
    print("PASS real PDF upload")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="Base URL of the running deployment")
    args = parser.parse_args()
    try:
        check_public_paths(args.base_url)
        if any(os.getenv(key) for key in API_KEYS):
            check_real_pdf_upload(args.base_url)
        else:
            print("SKIP real PDF upload (no extractor API key configured in this environment)")
    except (SmokeTestError, KeyError, ValueError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
