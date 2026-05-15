import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from backend.api.websocket import ActiveChatRun, ChatRunManager
from backend.domain.models import ChatMessage
from backend.storage.service import StorageService


class TestChatRunManager(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.storage = StorageService(self.tmp)
        self.manager = ChatRunManager(self.storage)
        self.project_id = "p1"
        self.thread_id = "t1"

    def _run(self) -> ActiveChatRun:
        task = asyncio.create_task(asyncio.sleep(60))
        orchestrator = MagicMock()
        orchestrator.current_steps = []
        return ActiveChatRun(
            project_id=self.project_id,
            thread_id=self.thread_id,
            task=task,
            orchestrator=orchestrator,
        )

    async def asyncTearDown(self):
        for run in list(self.manager._runs.values()):
            run.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run.task

    async def test_cancel_marks_run_as_user_requested(self):
        run = self._run()
        self.manager._runs[(self.project_id, self.thread_id)] = run

        self.assertTrue(self.manager.cancel(self.project_id, self.thread_id))

        self.assertTrue(run.cancel_requested)
        self.assertIsNotNone(run.cancel_requested_at)
        self.assertTrue(run.task.cancelled() or run.task.cancelling())

    async def test_persist_failed_writes_chat_response_not_stopped_by_user(self):
        run = self._run()
        self.storage.append_chat_thread_message(
            self.project_id,
            self.thread_id,
            ChatMessage(role="user", content="make a phone holder"),
        )
        self.storage.append_chat_thread_message(
            self.project_id,
            self.thread_id,
            ChatMessage(role="assistant", content="Generating model..."),
        )

        await self.manager._persist_failed(
            run,
            "CancelledError: upstream dependency cancelled the request",
            failure_type="unexpected_cancel",
            exc=asyncio.CancelledError(),
        )

        messages = self.storage.get_chat_thread_messages(self.project_id, self.thread_id)
        self.assertEqual(messages[-1].role, "assistant")
        self.assertIn("Generation failed:", messages[-1].content)
        self.assertIn("upstream dependency cancelled", messages[-1].content)
        self.assertNotIn("Stopped by user", messages[-1].content)
        self.assertEqual(run.events[-1]["type"], "chat_response")
        self.assertEqual(run.events[-1]["content"], messages[-1].content)


if __name__ == "__main__":
    unittest.main()
