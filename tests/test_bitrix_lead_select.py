import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from apps.bot.backend.bitrix_client import BitrixClient


class BitrixLeadSelectTests(unittest.IsolatedAsyncioTestCase):
    async def test_bulk_lead_lookup_includes_handoff_field(self):
        settings = SimpleNamespace(
            bitrix_webhook_url="https://example.test/rest/1/secret",
            lead_failure_reason_field="UF_FAILURE",
            lead_handoff_at_field="UF_HANDOFF",
            deal_refusal_reason_field="UF_DEAL_FAILURE",
        )
        with patch("apps.bot.backend.bitrix_client.get_settings", return_value=settings):
            client = BitrixClient()
        client._get_by_ids = AsyncMock(return_value=[])

        await client.get_leads_by_ids(["1"])

        select = client._get_by_ids.await_args.args[2]
        self.assertIn("UF_HANDOFF", select)


if __name__ == "__main__":
    unittest.main()
