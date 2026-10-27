import unittest
from unittest.mock import patch

import httpx

from app.bitrix_client import BitrixApiError, BitrixClient, BitrixTransportError


class BitrixTransportErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_status_error_never_contains_webhook_credential(self):
        secret = "super-secret-webhook-token"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, request=request, json={"error": "bad request"})

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http_client.aclose)
        with patch("app.bitrix_client._http_client", return_value=http_client):
            with self.assertRaises(BitrixApiError) as raised:
                await BitrixClient(f"https://portal.test/rest/24/{secret}/").get_lead("42")

        message = str(raised.exception)
        self.assertEqual(message, "crm.lead.get: Bitrix HTTP 400")
        self.assertNotIn(secret, message)
        self.assertNotIn("https://", message)

    async def test_network_error_is_retryable_and_redacted(self):
        secret = "another-secret-webhook-token"

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection failed", request=request)

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http_client.aclose)
        with patch("app.bitrix_client._http_client", return_value=http_client):
            with self.assertRaises(BitrixTransportError) as raised:
                await BitrixClient(f"https://portal.test/rest/24/{secret}/").get_lead("42")

        message = str(raised.exception)
        self.assertIn("Bitrix transport error", message)
        self.assertNotIn(secret, message)
        self.assertNotIn("https://", message)


if __name__ == "__main__":
    unittest.main()
