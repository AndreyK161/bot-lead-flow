import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from apps.bot.backend import telegram_queue


class TelegramQueueTests(unittest.IsolatedAsyncioTestCase):
    def settings(self):
        return SimpleNamespace(
            redis_url='redis://example.invalid:6379/0',
            telegram_queue_name='telegram',
            telegram_queue_max_retries=3,
        )

    def test_enqueue_is_idempotent_for_existing_job(self):
        queue = Mock()
        queue.fetch_job.return_value = Mock()
        with patch.object(telegram_queue, '_queue', return_value=queue):
            first = telegram_queue._enqueue('text', '123', None, 'lead:1:admin:123')
            second = telegram_queue._enqueue('text', '123', None, 'lead:1:admin:123')

        self.assertEqual(first, second)
        queue.enqueue.assert_not_called()

    def test_enqueue_configures_retries_and_stable_job_id(self):
        queue = Mock()
        queue.fetch_job.return_value = None
        with patch.object(telegram_queue, '_queue', return_value=queue), patch.object(
            telegram_queue, 'get_settings', return_value=self.settings(),
        ):
            job_id = telegram_queue._enqueue('text', '123', {'inline_keyboard': []}, 'lead:1')

        self.assertTrue(job_id.startswith('telegram-'))
        kwargs = queue.enqueue.call_args.kwargs
        self.assertEqual(kwargs['job_id'], job_id)
        self.assertEqual(kwargs['retry'].max, 3)
        self.assertEqual(kwargs['retry'].intervals, [10, 30, 60])

    async def test_redis_failure_falls_back_to_direct_delivery(self):
        direct = AsyncMock(return_value={'message_id': 42})
        with patch.object(telegram_queue, '_enqueue', side_effect=RuntimeError('redis down')), patch.object(
            telegram_queue, 'send_telegram_message', new=direct,
        ):
            result = await telegram_queue.enqueue_telegram_message(
                'text', chat_id=123, idempotency_key='lead:1',
            )

        self.assertFalse(result['queued'])
        direct.assert_awaited_once_with('text', chat_id=123, reply_markup=None)

    def test_health_reports_queue_registries(self):
        connection = Mock()
        queue = Mock()
        queue.count = 4
        queue.failed_job_registry.count = 1
        queue.scheduled_job_registry.count = 2
        with patch.object(telegram_queue, '_connection', return_value=connection), patch.object(
            telegram_queue, '_queue', return_value=queue,
        ):
            result = telegram_queue.health()

        connection.ping.assert_called_once_with()
        self.assertEqual(result, {
            'status': 'ok', 'queued': 4, 'failed': 1, 'scheduled': 2,
        })


if __name__ == '__main__':
    unittest.main()
