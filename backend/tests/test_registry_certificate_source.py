from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.registry_certificate_source import (
    CERTIFICATE_IMAGE_MAX_BYTES,
    CERTIFICATE_ENDPOINT,
    fetch_certificate_image,
    fetch_certificate_rows,
    iter_certificate_pages,
    normalize_certificate_image_ref,
    normalize_certificate_page,
)
from services.visit_source import VisitSourceError


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "code": 200,
            "data": {
                "records": [
                    {"pcsname": "滨湖新城派出所", "sssq": "长板社区", "dz": "测试路1号", "czrxm": "甲"},
                    {"pcsname": "其他派出所", "sssq": "其他社区", "dz": "测试路2号", "czrxm": "乙"},
                ]
            },
        }


class _Client:
    last_request = None

    def __init__(self, *args, headers=None, **kwargs):
        self.headers = dict(headers or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, endpoint, params=None):
        type(self).last_request = (endpoint, params)
        return _Response()


class RegistryCertificateSourceTests(unittest.IsolatedAsyncioTestCase):
    def test_image_reference_is_restricted_to_source_relative_jpeg_or_png(self):
        self.assertEqual(
            normalize_certificate_image_ref("2026-08-19/signature_001.JPG"),
            "2026-08-19/signature_001.JPG",
        )
        for value in (
            "", "../secret.jpg", "https://example.invalid/a.jpg",
            "2026/08/a.jpg", "2026-08-19/a.gif",
        ):
            with self.assertRaises(Exception):
                normalize_certificate_image_ref(value)

    async def test_fetches_all_dates_and_rejects_other_police_stations(self):
        with (
            patch("services.registry_certificate_source.httpx.AsyncClient", _Client),
            patch("services.registry_certificate_source.settings.VISIT_SOURCE_BASE_URL", "http://source.invalid"),
            patch("services.registry_certificate_source.settings.VISIT_SOURCE_AUTHORIZATION", "token"),
            patch("services.registry_certificate_source.settings.VISIT_SOURCE_POLICE_CODE", "320584710000"),
            patch("services.registry_certificate_source.settings.VISIT_SOURCE_POLICE_NAME", "滨湖新城派出所"),
        ):
            result = await fetch_certificate_rows()
        self.assertEqual(result["valid_count"], 1)
        self.assertEqual(result["issue_count"], 1)
        self.assertEqual(result["rows"][0]["source_row"], 1)
        endpoint, params = _Client.last_request
        self.assertEqual(endpoint, CERTIFICATE_ENDPOINT)
        self.assertNotIn("startTime", params)
        self.assertNotIn("endTime", params)
        self.assertEqual(params["deptCode"], "320584710000")

    async def test_page_iterator_can_resume_from_a_saved_page(self):
        class PagedResponse(_Response):
            def __init__(self, page):
                self.page = page

            def json(self):
                return {
                    "code": 200,
                    "data": {
                        "records": [{
                            "pcsname": "滨湖新城派出所",
                            "sssq": "长板社区",
                            "dz": f"测试路{self.page}号",
                        }],
                    },
                }

        class PagedClient(_Client):
            async def get(self, endpoint, params=None):
                type(self).last_request = (endpoint, params)
                return PagedResponse(params["pageNum"])

        with (
            patch("services.registry_certificate_source.httpx.AsyncClient", PagedClient),
            patch("services.registry_certificate_source.settings.VISIT_SOURCE_BASE_URL", "http://source.invalid"),
            patch("services.registry_certificate_source.settings.VISIT_SOURCE_AUTHORIZATION", "token"),
        ):
            pages = [page async for page in iter_certificate_pages(start_page=7)]
        self.assertEqual([7], [page["page"] for page in pages])
        self.assertEqual("测试路7号", pages[0]["rows"][0]["address"])
        self.assertTrue(pages[0]["is_last"])

    def test_page_normalization_reports_scope_and_required_field_rejections(self):
        rows, rejected = normalize_certificate_page([
            {"pcsname": "滨湖新城派出所", "sssq": "长板社区", "dz": "测试路1号"},
            {"pcsname": "其他派出所", "sssq": "长板社区", "dz": "测试路2号"},
            {"pcsname": "滨湖新城派出所", "sssq": "长板社区", "dz": ""},
        ])
        self.assertEqual(1, len(rows))
        self.assertEqual(2, rejected)

    async def test_fetches_image_with_bounded_relative_path_and_checks_magic(self):
        class StreamResponse:
            status_code = 200
            headers = {"content-length": "8", "content-type": "image/jpeg"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            async def aiter_bytes(self):
                yield b"\xff\xd8\xff\xe0test"

        class ImageClient:
            requested_url = ""

            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, method, url):
                type(self).requested_url = f"{method} {url}"
                return StreamResponse()

        with (
            patch("services.registry_certificate_source.httpx.AsyncClient", ImageClient),
            patch(
                "services.registry_certificate_source.settings.CERTIFICATE_IMAGE_BASE_URL",
                "http://source.invalid/attachment/signatures",
            ),
        ):
            content, media_type, extension = await fetch_certificate_image(
                "2026-08-19/signature_001.JPG"
            )
        self.assertTrue(content.startswith(b"\xff\xd8\xff"))
        self.assertEqual(media_type, "image/jpeg")
        self.assertEqual(extension, "jpg")
        self.assertEqual(
            ImageClient.requested_url,
            "GET http://source.invalid/attachment/signatures/2026-08-19/signature_001.JPG",
        )

    async def test_rejects_oversized_or_non_image_source_content(self):
        class StreamResponse:
            status_code = 200
            headers = {"content-length": "0"}
            content = b"not-an-image"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            async def aiter_bytes(self):
                yield self.content

        class ImageClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, _method, _url):
                return StreamResponse()

        with (
            patch("services.registry_certificate_source.httpx.AsyncClient", ImageClient),
            patch(
                "services.registry_certificate_source.settings.CERTIFICATE_IMAGE_BASE_URL",
                "http://source.invalid/attachment/signatures",
            ),
        ):
            with self.assertRaisesRegex(VisitSourceError, "图片内容格式无效"):
                await fetch_certificate_image("2026-08-19/signature_001.jpg")

            StreamResponse.headers = {
                "content-length": str(CERTIFICATE_IMAGE_MAX_BYTES + 1),
            }
            with self.assertRaisesRegex(VisitSourceError, "超过大小限制"):
                await fetch_certificate_image("2026-08-19/signature_001.jpg")


if __name__ == "__main__":
    unittest.main()
