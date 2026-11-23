"""Offline ASGI checks; never import main/settings/db or connect to services."""
import argparse
import contextlib
import datetime
import io
import json
import logging
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import patch
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dictapp.diagnostics import RequestDiagnosticsMiddleware


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.add_middleware(RequestDiagnosticsMiddleware)

        @self.app.get("/api/health")
        async def health():
            return {"status": "ok"}

        @self.app.post("/api/entry/{entry_id}")
        async def entry(entry_id: int, request: Request):
            await request.body()
            raise HTTPException(429, detail="Handled error", headers={"Retry-After": "3"})

        @self.app.get("/broken/{value}")
        async def broken(value: str):
            raise RuntimeError("PRIVATE_EXCEPTION_TEXT")

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url="http://test.invalid")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_liveness_and_unique_server_ids(self):
        with self.assertLogs("uvicorn.error", level=logging.INFO) as captured:
            responses = [await self.client.get("/api/health", headers={"X-Request-ID": "PRIVATE_CALLER_VALUE"})
                         for _ in range(2)]
        ids = [r.headers["x-request-id"] for r in responses]
        self.assertNotEqual(*ids)
        for r, request_id, record in zip(responses, ids, captured.records):
            self.assertEqual(r.json(), {"status": "ok"})
            self.assertEqual(UUID(request_id).version, 4)
            data = json.loads(record.getMessage())
            self.assertEqual(set(data), {"event", "request_id", "method", "route", "status", "duration_ms"})
            self.assertEqual((data["request_id"], data["route"], data["status"]), (request_id, "/api/health", 200))
            self.assertGreaterEqual(data["duration_ms"], 0)
        self.assertNotIn("PRIVATE_CALLER_VALUE", "\n".join(captured.output))

    async def test_handled_error_preserves_headers_and_omits_sensitive_fields(self):
        with self.assertLogs("uvicorn.error", level=logging.INFO) as captured:
            response = await self.client.post("/api/entry/987654321?q=PRIVATE_QUERY", content=b"PRIVATE_BODY",
                                              headers={"Authorization": "Bearer PRIVATE_AUTH", "X-Request-ID": "PRIVATE_ID"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "3")
        self.assertEqual(response.json(), {"detail": "Handled error"})
        log = json.loads(captured.records[0].getMessage())
        self.assertEqual(log["route"], "/api/entry/{entry_id}")
        self.assertEqual(log["request_id"], response.headers["x-request-id"])
        for secret in ["987654321", "PRIVATE_QUERY", "PRIVATE_BODY", "PRIVATE_AUTH", "PRIVATE_ID", "127.0.0.1"]:
            self.assertNotIn(secret, "\n".join(captured.output))

    async def test_validation_unmatched_and_unhandled_failure(self):
        for path, status, route in [("/api/entry/PRIVATE_BAD_ID", 422, "/api/entry/{entry_id}"),
                                    ("/PRIVATE_UNMATCHED_PATH", 404, "unmatched"),
                                    ("/broken/PRIVATE_PATH_VALUE", 500, "/broken/{value}")]:
            with self.subTest(status=status), self.assertLogs("uvicorn.error", level=logging.INFO) as captured:
                response = await self.client.request("POST" if status == 422 else "GET", path)
            self.assertEqual(response.status_code, status)
            log = json.loads(captured.records[0].getMessage())
            self.assertEqual((log["status"], log["route"]), (status, route))
            self.assertNotIn("PRIVATE", captured.records[0].getMessage())
            if status != 500:
                self.assertEqual(response.headers["x-request-id"], log["request_id"])

    async def test_non_http_scopes_are_untouched(self):
        seen = []
        async def downstream(scope, receive, send):
            seen.append(scope)
        scope = {"type": "lifespan"}
        with patch("dictapp.diagnostics.logger.info") as info:
            await RequestDiagnosticsMiddleware(downstream)(scope, None, None)
        self.assertIs(seen[0], scope)
        self.assertNotIn("state", scope)
        info.assert_not_called()


class ReadonlyReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/diagnose_server_readonly.py"))

    def test_access_log_allowlist_drops_query_headers_ip_and_body(self):
        record = {"ts": 1795382400, "status": 200, "duration": 1.25, "size": 234,
                  "request": {"uri": "/api/search?q=PRIVATE_QUERY", "remote_ip": "PRIVATE_IP",
                              "headers": {"Authorization": ["PRIVATE_KEY"]}, "body": "PRIVATE_BODY"},
                  "msg": "PRIVATE_MESSAGE"}
        clean = self.script["sanitize_access_record"](record)
        self.assertEqual(clean["route"], "/api/search")
        self.assertEqual(clean["duration_seconds"], 1.25)
        self.assertEqual(clean["response_bytes"], 234)
        self.assertNotIn("PRIVATE", json.dumps(clean))

    def test_malformed_and_unknown_fields_do_not_echo_arbitrary_values(self):
        for change in [{"status": "PRIVATE_STATUS"}, {"ts": float("nan")},
                       {"request": {"uri": "/PRIVATE_PATH"}}, {"request": []}]:
            record = {"ts": 1795382400, "status": 200, "request": {"uri": "/api/health"}}
            record.update(change)
            self.assertIsNone(self.script["sanitize_access_record"](record))
        summary = self.script["summarize_journal"]("PRIVATE_NOT_JSON\n" + json.dumps({
            "logger": "tls.PRIVATE", "level": "error", "msg": "PRIVATE_SECRET"}), 100)
        self.assertEqual(summary["tls_warning_error_count"], 1)
        self.assertNotIn("PRIVATE", json.dumps(summary))

    def backend_record(self, **changes):
        record = {"event": "http_request", "request_id": "00000000-0000-4000-8000-000000000001",
                  "method": "GET", "route": "/api/search", "status": 200, "duration_ms": 12.75}
        return dict(record, **changes)

    def test_backend_log_allowlist_with_uvicorn_prefix_and_hostile_fields(self):
        record = self.backend_record(body="PRIVATE_BODY", query="PRIVATE_QUERY", ip="PRIVATE_IP",
                                     headers={"Authorization": "PRIVATE_KEY"}, exception="PRIVATE_EXCEPTION")
        logs = "\n".join([
            "PRIVATE_RAW_EXCEPTION", json.dumps({"event": "PRIVATE_EVENT", "body": "PRIVATE_BODY"}),
            "2026-11-23T00:19:00.123456789Z INFO:     " + json.dumps(record),
            "INFO: " + json.dumps(self.backend_record(route="/api/search?q=PRIVATE_QUERY")),
            "INFO: " + json.dumps(self.backend_record(route="/api/entry/PRIVATE_PATH")),
            "INFO: " + json.dumps(self.backend_record(request_id="PRIVATE_ID")),
            "INFO: " + json.dumps(self.backend_record(method="PRIVATE_METHOD")),
            "PRIVATE_OVERSIZED" + "x" * 1_000_000,
        ])
        summary = self.script["summarize_backend_logs"](logs)
        self.assertEqual(summary["request_diagnostics_records"], 1)
        self.assertFalse(summary["no_request_diagnostics_found"])
        self.assertEqual(summary["oversized_lines_skipped"], 1)
        recent = summary["recent_requests"][0]
        self.assertEqual(set(recent), {"request_id", "method", "route", "status", "duration_ms", "utc"})
        self.assertEqual(recent["utc"], "2026-11-23T00:19:00.123456+00:00")
        self.assertEqual(recent["duration_ms"], 12.75)
        self.assertNotIn("PRIVATE", json.dumps(summary))

    def test_backend_invalid_types_and_nested_records_are_ignored(self):
        for change in [{"event": "other"}, {"status": True}, {"status": 600}, {"status": "PRIVATE"},
                       {"duration_ms": True}, {"duration_ms": float("nan")}, {"duration_ms": -1},
                       {"route": []}, {"method": {}}, {"request_id": []},
                       {"request_id": "00000000-0000-1000-8000-000000000001"}]:
            with self.subTest(change=change):
                self.assertIsNone(self.script["sanitize_backend_record"](self.backend_record(**change)))
        logs = "\n".join(["INFO: " + json.dumps({"message": self.backend_record()}),
                           "INFO: {PRIVATE_INVALID_JSON", "PRIVATE_ERROR_NAME"])
        summary = self.script["summarize_backend_logs"](logs)
        self.assertTrue(summary["no_request_diagnostics_found"])
        self.assertEqual(summary["recent_requests"], [])
        self.assertNotIn("PRIVATE", json.dumps(summary))

    def test_backend_windows_use_explicit_container_and_bounded_docker_logs(self):
        calls = []
        def fake_command(args, **kwargs):
            calls.append((args, kwargs))
            return ((0, "INFO: " + json.dumps(self.backend_record())) if len(calls) == 1
                    else (1, "PRIVATE_DOCKER_ERROR"))
        collect = self.script["collect_backend_windows"]
        now = datetime.datetime(2026, 11, 23, 1, 0, tzinfo=datetime.timezone.utc)
        with patch.dict(collect.__globals__, {"command": fake_command}):
            windows = collect("explicit_backend", now)
        self.assertEqual(len(calls), 2)
        for args, kwargs in calls:
            self.assertEqual(args[:3], ["docker", "logs", "--timestamps"])
            self.assertEqual(args[-3:], ["--tail", "3000", "explicit_backend"])
            self.assertIn("--since", args)
            self.assertIn("--until", args)
            self.assertEqual(kwargs, {"combine_output": True})
        self.assertEqual(windows[1]["since"], "2026-11-23T00:30:00Z")
        self.assertEqual(windows[1]["until"], "2026-11-23T01:00:00Z")
        self.assertFalse(windows[1]["readable"])
        self.assertTrue(windows[1]["no_request_diagnostics_found"])
        self.assertNotIn("PRIVATE", json.dumps(windows))

    def test_default_report_never_reads_docker_logs_and_container_name_cannot_be_an_option(self):
        for invalid in ["--all", "name; PRIVATE", "name/PRIVATE", "name\nPRIVATE"]:
            with self.assertRaises(argparse.ArgumentTypeError):
                self.script["backend_container_name"](invalid)
        calls = []
        def fake_command(args, **kwargs):
            calls.append(args)
            return 0, ""
        main = self.script["main"]
        output = io.StringIO()
        with patch.dict(main.__globals__, {"command": fake_command,
                                          "probe": lambda *args: {}, "resource_summary": lambda: {}}), \
                contextlib.redirect_stdout(output):
            main([])
        report = json.loads(output.getvalue())
        self.assertFalse(report["backend_logs_requested"])
        self.assertEqual(report["backend_windows"], [])
        self.assertFalse(any(args[0] == "docker" for args in calls))


if __name__ == "__main__":
    unittest.main()
