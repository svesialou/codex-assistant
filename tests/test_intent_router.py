from __future__ import annotations

import unittest

from codex_telegram_bot.services.intent_router import IntentContext, IntentRouter


class IntentRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.router = IntentRouter()

    def test_select_project_from_plain_text(self) -> None:
        result = self.router.route("Работаем с ботом")

        self.assertEqual(result.intent, "select_project")
        self.assertEqual(result.project_query, "бот")

    def test_create_task_from_plain_text(self) -> None:
        result = self.router.route("Добавь агентные роли и кнопку мозги")

        self.assertEqual(result.intent, "create_task")
        self.assertEqual(result.task_text, "Добавь агентные роли и кнопку мозги")

    def test_show_brain_uses_last_task_context(self) -> None:
        result = self.router.route("что думает команда?", IntentContext(last_task_id="task-1"))

        self.assertEqual(result.intent, "show_brain")
        self.assertEqual(result.task_id, "task-1")

    def test_confirm_execution_uses_last_planned_task(self) -> None:
        result = self.router.route("делай", IntentContext(last_planned_task_id="task-2"))

        self.assertEqual(result.intent, "confirm_execution")
        self.assertEqual(result.task_id, "task-2")

    def test_continue_task_with_query_is_detected(self) -> None:
        result = self.router.route("продолжи задачу про авторизацию")

        self.assertEqual(result.intent, "continue_task")
        self.assertEqual(result.task_text, "задачу про авторизацию")

    def test_remember_and_alias_intents_require_confirmation(self) -> None:
        remember = self.router.route("Запомни: source repo главный")
        alias = self.router.route("Называй codex-assistant просто бот")

        self.assertEqual(remember.intent, "remember")
        self.assertTrue(remember.requires_confirmation)
        self.assertEqual(remember.memory_text, "source repo главный")
        self.assertEqual(alias.intent, "add_alias")
        self.assertEqual(alias.project_query, "codex-assistant")
        self.assertEqual(alias.alias, "бот")


if __name__ == "__main__":
    unittest.main()
