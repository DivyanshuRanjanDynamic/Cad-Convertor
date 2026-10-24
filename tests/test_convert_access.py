"""Signed upload tickets, per-user quotas and early rejection for /convert.

    python -m unittest discover -s tests -v
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import convert_access  # noqa: E402
from convert_access import ConvertQuota, authorize_convert, sign_ticket, verify_ticket  # noqa: E402

try:
    import main
except ImportError:  # pragma: no cover - local dev without OCCT
    main = None

SECRET = "test-service-token-1234567890"


def ticket(sub="uid_1", scope="convert", ttl=600, secret=SECRET, now=None):
    now = time.time() if now is None else now
    return sign_ticket({"sub": sub, "scope": scope, "exp": int(now + ttl)}, secret)


class TicketTest(unittest.TestCase):
    def test_valid_ticket_yields_the_user(self):
        self.assertEqual(verify_ticket(ticket(), SECRET, "convert"), "uid_1")

    def test_rejects_forged_expired_rescoped_and_long_lived_tickets(self):
        now = time.time()
        bad = {
            "wrong secret": ticket(secret="someone-elses-secret"),
            "expired": ticket(ttl=-1),
            "wrong scope": ticket(scope="analyze"),
            "ttl too long": ticket(ttl=convert_access.MAX_TICKET_TTL_S + 60),
            "empty sub": ticket(sub=""),
            "garbage": "v1.not-base64.%%%",
            "wrong version": ticket().replace("v1.", "v0.", 1),
        }
        # Tamper with the payload but keep the old signature
        version, body, sig = ticket(sub="uid_1").split(".")
        forged_body = ticket(sub="admin").split(".")[1]
        bad["swapped payload"] = f"{version}.{forged_body}.{sig}"

        for label, value in bad.items():
            with self.assertRaises(HTTPException, msg=label) as ctx:
                verify_ticket(value, SECRET, "convert", now=now)
            self.assertEqual(ctx.exception.status_code, 401, label)


class SharedVectorTest(unittest.TestCase):
    # Same vector as studio/src/lib/__tests__/shop-pricing-and-convert-ticket.test.ts —
    # proves the studio (TypeScript) signer and this verifier agree byte for byte.
    VECTOR = (
        "v1.eyJzdWIiOiJ1aWRfdmVjdG9yIiwic2NvcGUiOiJjb252ZXJ0IiwiZXhwIjoxODAwMDAwNjAwfQ"
        ".whemFXEaecHD18MvPx66V0uf_-BV8djqumevpm-2-NI"
    )

    def test_verifies_the_studio_minted_ticket(self):
        self.assertEqual(verify_ticket(self.VECTOR, "vector-secret", "convert", now=1800000000), "uid_vector")


class AuthorizeConvertTest(unittest.TestCase):
    def test_service_token_ticket_and_missing_credentials(self):
        with mock.patch.dict(os.environ, {"CONVERTER_SERVICE_TOKEN": SECRET}):
            self.assertEqual(authorize_convert(f"Bearer {SECRET}"), "service")
            self.assertEqual(authorize_convert(f"Bearer {ticket()}"), "user:uid_1")
            for header in ("", "Bearer ", f"Basic {SECRET}", "Bearer nope"):
                with self.assertRaises(HTTPException, msg=header) as ctx:
                    authorize_convert(header)
                self.assertEqual(ctx.exception.status_code, 401)

    def test_fails_closed_without_a_configured_secret(self):
        with mock.patch.dict(os.environ, {"CONVERTER_SERVICE_TOKEN": ""}):
            with self.assertRaises(HTTPException) as ctx:
                authorize_convert(f"Bearer {ticket()}")
        self.assertEqual(ctx.exception.status_code, 503)


class ConvertQuotaTest(unittest.TestCase):
    def test_hourly_limit_per_user(self):
        quota = ConvertQuota(per_hour=2, per_caller_active=5, total_active=10)
        t0 = 1_000_000.0
        for _ in range(2):
            quota.acquire("user:a", now=t0)
            quota.release("user:a")
        with self.assertRaises(HTTPException) as ctx:
            quota.acquire("user:a", now=t0 + 10)
        self.assertEqual(ctx.exception.status_code, 429)
        quota.acquire("user:b", now=t0 + 10)  # other users unaffected
        quota.acquire("user:a", now=t0 + 3601)  # window slides

    def test_concurrency_caps_and_release(self):
        quota = ConvertQuota(per_hour=100, per_caller_active=1, total_active=2)
        quota.acquire("user:a")
        with self.assertRaises(HTTPException):
            quota.acquire("user:a")
        quota.acquire("user:b")
        with self.assertRaises(HTTPException) as ctx:
            quota.acquire("user:c")
        self.assertEqual(ctx.exception.status_code, 503)
        quota.release("user:a")
        quota.acquire("user:c")

    def test_service_caller_has_no_hourly_limit(self):
        quota = ConvertQuota(per_hour=1, per_caller_active=5, total_active=10)
        for _ in range(3):
            quota.acquire("service")
            quota.release("service")


@unittest.skipIf(main is None, "CadQuery / OCP not installed")
class ConvertEndpointTest(unittest.TestCase):
    FILES = {"file": ("part.step", b"ISO-10303-21;", "application/octet-stream")}

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"CONVERTER_SERVICE_TOKEN": SECRET})
        self.env.start()
        self.quota = ConvertQuota(per_hour=30, per_caller_active=1, total_active=6)
        self.quota_patch = mock.patch.object(main, "convert_quota", self.quota)
        self.quota_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.quota_patch.stop()
        self.env.stop()

    def test_rejects_anonymous_uploads(self):
        with mock.patch.object(main, "_process_conversion_job_bg") as job:
            res = self.client.post("/convert", files=self.FILES)
            sync = self.client.post("/convert?sync=true", files=self.FILES)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(sync.status_code, 401)
        job.assert_not_called()

    def test_ticket_holder_gets_a_job_and_the_slot_is_released(self):
        auth = {"Authorization": f"Bearer {ticket()}"}
        with mock.patch.object(main, "_process_conversion_job_bg") as job:
            first = self.client.post("/convert", files=self.FILES, headers=auth)
            # TestClient runs background tasks before returning, so the slot is free again
            second = self.client.post("/convert", files=self.FILES, headers=auth)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(job.call_count, 2)
        self.assertEqual(self.quota._active, {})

    def test_sync_mode_is_limited_too(self):
        auth = {"Authorization": f"Bearer {ticket()}"}
        self.quota.acquire("user:uid_1")  # an upload already in flight
        with mock.patch.object(main, "_convert_step_to_stl") as convert:
            res = self.client.post("/convert?sync=true", files=self.FILES, headers=auth)
        self.assertEqual(res.status_code, 429)
        convert.assert_not_called()

    def test_cors_preflight_still_works(self):
        res = self.client.options(
            "/convert",
            headers={
                "Origin": "http://localhost:9002",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("authorization", res.headers.get("access-control-allow-headers", "").lower())

    def test_rejections_carry_cors_headers(self):
        res = self.client.post("/convert", files=self.FILES, headers={"Origin": "http://localhost:9002"})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.headers.get("access-control-allow-origin"), "http://localhost:9002")

    def test_analysis_endpoints_reject_before_reading_uploads(self):
        res = self.client.post("/analyze-cnc", files=self.FILES)
        self.assertEqual(res.status_code, 401)


@unittest.skipIf(main is None, "CadQuery / OCP not installed")
class JobStatusTest(unittest.TestCase):
    """/status is bound to the caller that created the job, and never leaks S3 links."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"CONVERTER_SERVICE_TOKEN": SECRET})
        self.env.start()
        self.store = main.ConversionJobStore()
        self.store._db = None  # in-memory for the test
        self.store_patch = mock.patch.object(main, "job_store", self.store)
        self.store_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.store_patch.stop()
        self.env.stop()

    def status(self, job_id, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.get(f"/status/{job_id}", headers=headers)

    def test_only_the_creator_can_read_a_job(self):
        self.store.create_job("job_a", "part.step", 10, owner="user:uid_1")
        self.store.update_job("job_a", {"status": "done", "result": {"stl": "AAAA"}})

        self.assertEqual(self.status("job_a").status_code, 401)  # anonymous
        self.assertEqual(self.status("job_a", ticket(sub="uid_2")).status_code, 404)  # someone else
        self.assertEqual(self.status("job_a", ticket(sub="uid_1", secret="wrong")).status_code, 401)

        mine = self.status("job_a", ticket(sub="uid_1"))  # a fresh ticket for the same user works
        self.assertEqual(mine.status_code, 200)
        self.assertEqual(mine.json()["result"], {"stl": "AAAA"})
        self.assertNotIn("owner", mine.json())

        self.assertEqual(self.status("job_a", SECRET).status_code, 200)  # the web app's service token

    def test_old_jobs_never_return_s3_links(self):
        # A job written before this change: no owner, S3 URLs stored
        self.store.create_job("job_old", "part.step", 10, owner="user:uid_1")
        self.store.update_job("job_old", {"owner": None, "fileUrl": "https://bucket/cad_uploads/x.step", "resultUrl": "https://bucket/x.stl"})
        self.assertEqual(self.status("job_old", ticket(sub="uid_1")).status_code, 404)
        body = self.status("job_old", SECRET).json()
        self.assertNotIn("fileUrl", body)
        self.assertNotIn("resultUrl", body)

    def test_conversion_job_stores_no_copy_of_the_file(self):
        self.assertFalse(hasattr(main, "_upload_to_s3"))
        self.store.create_job("job_b", "part.step", 10, owner="user:uid_1")
        with mock.patch.object(main, "_convert_step_to_stl", return_value={"stl": "AAAA"}):
            main._process_conversion_job_bg("job_b", b"ISO-10303-21;", "part.step")
        job = self.store.get_job("job_b")
        self.assertEqual(job["status"], "done")
        self.assertNotIn("fileUrl", job)
        self.assertNotIn("resultUrl", job)


if __name__ == "__main__":
    unittest.main()
