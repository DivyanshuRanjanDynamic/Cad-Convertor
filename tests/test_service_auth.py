"""Service-token protection and presigned-URL fetching for the costing endpoints.

    python -m unittest discover -s tests -v
"""

import io
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import Depends, FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import service_auth  # noqa: E402

try:
    import main
except ImportError:  # pragma: no cover - local dev without OCCT
    main = None

TOKEN = "test-service-token-1234567890"
S3_URL = "https://mechhub-cad.s3.eu-north-1.amazonaws.com/cnc-analysis/u1/abc/part.step?X-Amz-Signature=x"


def _guarded_app() -> FastAPI:
    app = FastAPI()

    @app.get("/guarded", dependencies=[Depends(service_auth.require_service_token)])
    def guarded():
        return {"ok": True}

    return app


class RequireServiceTokenTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(_guarded_app())

    def test_fails_closed_without_configured_token(self):
        with mock.patch.dict(os.environ, {"CONVERTER_SERVICE_TOKEN": ""}):
            res = self.client.get("/guarded", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(res.status_code, 503)

    def test_rejects_missing_wrong_and_non_bearer_credentials(self):
        with mock.patch.dict(os.environ, {"CONVERTER_SERVICE_TOKEN": TOKEN}):
            for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": f"Basic {TOKEN}"}):
                self.assertEqual(self.client.get("/guarded", headers=headers).status_code, 401, headers)

    def test_accepts_the_shared_token(self):
        with mock.patch.dict(os.environ, {"CONVERTER_SERVICE_TOKEN": TOKEN}):
            res = self.client.get("/guarded", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(res.status_code, 200)


class ValidateFileUrlTest(unittest.TestCase):
    def test_allows_https_s3_hosts(self):
        service_auth.validate_file_url(S3_URL)

    def test_rejects_other_schemes_and_hosts(self):
        for url in (
            "http://mechhub-cad.s3.amazonaws.com/x.step",
            "https://169.254.169.254/latest/meta-data",
            "https://evil.example.com/x.step",
            "https://amazonaws.com.evil.example.com/x.step",
            "file:///etc/passwd",
        ):
            with self.assertRaises(HTTPException, msg=url):
                service_auth.validate_file_url(url)

    def test_host_list_is_configurable(self):
        with mock.patch.dict(os.environ, {"FILE_URL_ALLOWED_HOSTS": "storage.example.com"}):
            service_auth.validate_file_url("https://storage.example.com/a.step")
            with self.assertRaises(HTTPException):
                service_auth.validate_file_url(S3_URL)


class _FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FetchFileTest(unittest.TestCase):
    def test_reads_the_body(self):
        with mock.patch.object(service_auth._opener, "open", return_value=_FakeResponse(b"ISO-10303")):
            self.assertEqual(service_auth.fetch_file(S3_URL, 100), b"ISO-10303")

    def test_caps_size_even_without_content_length(self):
        with mock.patch.object(service_auth._opener, "open", return_value=_FakeResponse(b"x" * 101)):
            with self.assertRaises(HTTPException) as ctx:
                service_auth.fetch_file(S3_URL, 100)
        self.assertEqual(ctx.exception.status_code, 413)

    def test_maps_storage_errors(self):
        denied = urllib.error.HTTPError(S3_URL, 403, "Forbidden", {}, None)
        with mock.patch.object(service_auth._opener, "open", side_effect=denied):
            with self.assertRaises(HTTPException) as ctx:
                service_auth.fetch_file(S3_URL, 100)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_does_not_follow_redirects(self):
        handler = service_auth._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example.com"))


@unittest.skipIf(main is None, "CadQuery / OCP not installed")
class CncEndpointsTest(unittest.TestCase):
    ANALYSIS = {"version": "cnc-1", "process": {"recommended": "cnc_machining_3axis"},
                "setups": {"count": 1}, "analysisMs": 5}

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"CONVERTER_SERVICE_TOKEN": TOKEN})
        self.env.start()
        self.analyze = mock.patch.object(main, "analyze_step_cnc", return_value=self.ANALYSIS)
        self.analyze.start()
        self.client = TestClient(main.app)
        self.auth = {"Authorization": f"Bearer {TOKEN}"}

    def tearDown(self):
        self.analyze.stop()
        self.env.stop()

    def test_multipart_endpoint_requires_the_token(self):
        files = {"file": ("part.step", b"ISO-10303-21;", "application/octet-stream")}
        self.assertEqual(self.client.post("/analyze-cnc", files=files).status_code, 401)
        res = self.client.post("/analyze-cnc", files=files, headers=self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["version"], "cnc-1")

    def test_internal_endpoint_fetches_the_presigned_url(self):
        body = {"fileUrl": S3_URL, "fileName": "part.step"}
        self.assertEqual(self.client.post("/internal/analyze-cnc", json=body).status_code, 401)

        with mock.patch.object(main, "fetch_file", return_value=b"ISO-10303-21;") as fetch:
            res = self.client.post("/internal/analyze-cnc", json=body, headers=self.auth)
        self.assertEqual(res.status_code, 200)
        fetch.assert_called_once_with(S3_URL, main.MAX_FILE_SIZE)

    def test_internal_endpoint_rejects_bad_names_and_hosts_before_fetching(self):
        with mock.patch.object(main, "fetch_file") as fetch:
            bad_ext = self.client.post(
                "/internal/analyze-cnc", json={"fileUrl": S3_URL, "fileName": "part.dxf"}, headers=self.auth
            )
            bad_host = self.client.post(
                "/internal/analyze-cnc",
                json={"fileUrl": "https://evil.example.com/p.step", "fileName": "p.step"},
                headers=self.auth,
            )
        self.assertEqual(bad_ext.status_code, 400)
        self.assertEqual(bad_host.status_code, 400)
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
