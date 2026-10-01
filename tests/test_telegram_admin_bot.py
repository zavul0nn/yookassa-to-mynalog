import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import telegram_admin_bot as bot_module
from state_store import StateStore


class TelegramAdminBotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bot = bot_module.TelegramAdminBot.__new__(
            bot_module.TelegramAdminBot
        )
        self.bot.admin_chat_id = "-1001"
        self.bot.admin_user_id = "42"
        self.bot.token = "bot-token-secret"
        self.bot.send = AsyncMock()
        self.bot.store = StateStore(self.root / "sync_state.db")
        self.bot.store.save({
            "pending_payments": [],
            "pending_refunds": [],
            "notification_preferences": {
                "receipt_success": True,
                "receipt_errors": True,
            },
            "receipt_reports": {
                "enabled": True,
                "chat_id": "-1001",
                "thread_id": None,
            },
        })

    def tearDown(self):
        self.temporary.cleanup()

    def test_command_from_another_group_member_is_rejected(self):
        update = {
            "message": {
                "chat": {"id": -1001},
                "from": {"id": 99},
                "text": "/status",
            }
        }

        asyncio.run(self.bot.handle(update))

        self.bot.send.assert_not_awaited()

    def test_notification_preference_is_saved(self):
        asyncio.run(self.bot.command_notifications("errors off"))

        preferences = self.bot.store.load()["notification_preferences"]
        self.assertFalse(preferences["receipt_errors"])
        self.bot.send.assert_awaited_once()

    def test_main_button_opens_status_without_command(self):
        self.bot.command_status = AsyncMock()
        update = {
            "message": {
                "chat": {"id": -1001},
                "from": {"id": 42},
                "text": "📊 Статус",
            }
        }

        asyncio.run(self.bot.handle(update))

        self.bot.command_status.assert_awaited_once()

    def test_inline_button_toggles_notification(self):
        self.bot._api = AsyncMock(return_value=True)
        update = {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 42},
                "message": {"chat": {"id": -1001}},
                "data": "notify:errors:toggle",
            }
        }

        asyncio.run(self.bot.handle(update))

        preferences = self.bot.store.load()["notification_preferences"]
        self.assertFalse(preferences["receipt_errors"])
        self.bot._api.assert_awaited_once_with(
            "answerCallbackQuery", callback_query_id="callback-1"
        )
        self.bot.send.assert_awaited_once()

    def test_logs_are_limited_and_token_is_redacted(self):
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "sync.log").write_text(
            "first\nsecret bot-token-secret\nlast\n",
            encoding="utf-8",
        )

        with patch.object(bot_module, "LOG_DIR", logs):
            asyncio.run(self.bot.command_logs("2"))

        message = self.bot.send.await_args.args[0]
        self.assertNotIn("first", message)
        self.assertNotIn("bot-token-secret", message)
        self.assertIn("***", message)

    def test_current_group_topic_can_be_saved_for_receipt_reports(self):
        self.bot._api = AsyncMock(return_value=True)
        update = {"callback_query": {
            "id": "callback-report",
            "from": {"id": 42},
            "message": {"chat": {"id": -100777}, "message_thread_id": 321},
            "data": "report:here",
        }}

        asyncio.run(self.bot.handle(update))

        report = self.bot.store.load()["receipt_reports"]
        self.assertEqual("-100777", report["chat_id"])
        self.assertEqual(321, report["thread_id"])

    def test_replacement_retry_resumes_after_cancellation_not_from_start(self):
        self.bot.store.save({
            "pending_payments": [],
            "pending_refunds": [{
                "refund_id": "refund-1",
                "receipt_uuid": "old-receipt",
                "status": "replacement_unknown",
            }],
        })

        asyncio.run(self.bot.retry_queue_item("r", "refund-1"))

        item = self.bot.store.load()["pending_refunds"][0]
        self.assertEqual("cancelled", item["status"])

    def test_unknown_payment_is_shown_as_automatic_fns_reconciliation(self):
        self.bot.store.save({
            "pending_payments": [{
                "payment_id": "payment-unknown",
                "amount": "139.00",
                "created_at": "2026-10-01T11:41:53Z",
                "status": "unknown",
                "queue_attempts": 0,
                "verification_attempts": 3,
                "unknown_negative_checks": 2,
                "last_verification_at": "2026-10-01T14:00:00Z",
                "next_unknown_verification_at": "2026-10-01T14:30:00Z",
                "error": "ФНС не ответила вовремя",
            }],
            "pending_refunds": [],
        })

        with patch.object(
            bot_module.config, "FNS_RETRY_SCHEDULE", "*/5 * * * *"
        ):
            asyncio.run(self.bot.show_queue_item("p", "payment-unknown"))

        message = self.bot.send.await_args.args[0]
        self.assertIn("автоматическая сверка в ФНС", message)
        self.assertIn("Проверок в ФНС: 3", message)
        self.assertIn("Успешных проверок без чека: 2 из 5", message)
        self.assertIn("2026-10-01T14:30:00Z", message)
        self.assertIn("*/5 * * * *", message)
        self.assertNotIn("нужна ручная проверка", message)

    def test_status_counts_unknown_payment_as_automatic(self):
        self.bot.store.save({
            "pending_payments": [{"status": "unknown"}],
            "pending_refunds": [],
            "watched_payments": [],
            "receipt_deliveries": [],
            "notification_preferences": {},
            "receipt_reports": {},
        })

        asyncio.run(self.bot.command_status())

        message = self.bot.send.await_args.args[0]
        self.assertIn("автоматическая обработка: <b>1</b>", message)
        self.assertIn("ручная проверка: <b>0</b>", message)

    def test_start_cancels_report_id_input(self):
        self.bot.pending_input = "report_chat_id"
        self.bot.pending_input_deadline = None
        self.bot.show_main_menu = AsyncMock()
        update = {"message": {
            "chat": {"id": -1001},
            "from": {"id": 42},
            "text": "/start",
        }}

        asyncio.run(self.bot.handle(update))

        self.assertIsNone(self.bot.pending_input)
        self.bot.show_main_menu.assert_awaited_once()
        self.bot.send.assert_not_awaited()

    def test_menu_button_cancels_report_id_input_and_runs_action(self):
        self.bot.pending_input = "report_chat_id"
        self.bot.pending_input_deadline = None
        self.bot.command_status = AsyncMock()
        update = {"message": {
            "chat": {"id": -1001},
            "from": {"id": 42},
            "text": "📊 Статус",
        }}

        asyncio.run(self.bot.handle(update))

        self.assertIsNone(self.bot.pending_input)
        self.bot.command_status.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
