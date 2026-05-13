"""
Unit tests for the storage service.

Uses a temporary directory so no real data is modified.
"""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from backend.domain.models import (
    ChatMessage,
    CritiqueReport,
    FailureType,
    GeometryStats,
    ModelMetadata,
    ProjectConfig,
)
from backend.storage.service import StorageService


def _make_storage() -> tuple[StorageService, Path]:
    tmp = tempfile.mkdtemp()
    svc = StorageService(Path(tmp))
    return svc, Path(tmp)


def _make_config(project_id="test-proj") -> ProjectConfig:
    return ProjectConfig(project_id=project_id, name="Test Project")


class TestProjectCRUD(unittest.TestCase):

    def setUp(self):
        self.storage, self.tmp = _make_storage()
        self.config = _make_config()
        self.storage.create_project(self.config)

    def test_create_and_get(self):
        loaded = self.storage.get_project("test-proj")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.project_id, "test-proj")
        self.assertEqual(loaded.name, "Test Project")

    def test_get_missing_returns_none(self):
        result = self.storage.get_project("nonexistent")
        self.assertIsNone(result)

    def test_list_projects(self):
        self.storage.create_project(_make_config("proj-2"))
        projects = self.storage.list_projects()
        ids = [p.project_id for p in projects]
        self.assertIn("test-proj", ids)
        self.assertIn("proj-2", ids)

    def test_update_project(self):
        self.config.name = "Updated Name"
        self.storage.update_project(self.config)
        loaded = self.storage.get_project("test-proj")
        self.assertEqual(loaded.name, "Updated Name")

    def test_delete_project(self):
        self.storage.delete_project("test-proj")
        loaded = self.storage.get_project("test-proj")
        self.assertIsNone(loaded)

    def test_project_dir_created(self):
        project_dir = self.storage.get_project_dir("test-proj")
        self.assertTrue(project_dir.exists())
        self.assertTrue((project_dir / "models").exists())


class TestModelCRUD(unittest.TestCase):

    def setUp(self):
        self.storage, self.tmp = _make_storage()
        self.config = _make_config()
        self.storage.create_project(self.config)
        self.project_id = "test-proj"

    def test_next_model_id_sequential(self):
        id1 = self.storage.next_model_id(self.project_id)
        self.assertEqual(id1, "model-001")
        self.storage.create_model_dir(self.project_id, id1)

        id2 = self.storage.next_model_id(self.project_id)
        self.assertEqual(id2, "model-002")

    def test_create_model_dir(self):
        model_dir = self.storage.create_model_dir(self.project_id, "model-001")
        self.assertTrue(model_dir.exists())

    def test_save_and_get_metadata(self):
        model_id = "model-001"
        self.storage.create_model_dir(self.project_id, model_id)

        metadata = ModelMetadata(
            model_id=model_id,
            prompt="test box",
            cad_source="import cadquery as cq\nresult = cq.Workplane().box(10,10,10)",
            has_step=True,
            has_stl=True,
            has_glb=True,
            iteration=1,
        )
        self.storage.save_model_metadata(self.project_id, metadata)

        loaded = self.storage.get_model_metadata(self.project_id, model_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.model_id, model_id)
        self.assertEqual(loaded.prompt, "test box")
        self.assertTrue(loaded.has_glb)

    def test_metadata_with_critique(self):
        model_id = "model-001"
        self.storage.create_model_dir(self.project_id, model_id)

        critique = CritiqueReport(
            overall_printability=0.85,
            matches_intent=True,
            suggested_repairs=[],
        )
        metadata = ModelMetadata(
            model_id=model_id,
            prompt="test",
            critique=critique,
            vision_score=0.85,
        )
        self.storage.save_model_metadata(self.project_id, metadata)
        loaded = self.storage.get_model_metadata(self.project_id, model_id)
        self.assertIsNotNone(loaded.critique)
        self.assertAlmostEqual(loaded.critique.overall_printability, 0.85)
        self.assertAlmostEqual(loaded.vision_score, 0.85)

    def test_metadata_with_geometry_stats(self):
        model_id = "model-001"
        self.storage.create_model_dir(self.project_id, model_id)

        geo = GeometryStats(
            bbox_x_mm=50.0, bbox_y_mm=30.0, bbox_z_mm=10.0,
            volume_mm3=15000.0, solid_count=1, is_closed=True,
        )
        metadata = ModelMetadata(model_id=model_id, prompt="test", geometry_stats=geo)
        self.storage.save_model_metadata(self.project_id, metadata)

        loaded = self.storage.get_model_metadata(self.project_id, model_id)
        self.assertAlmostEqual(loaded.geometry_stats.bbox_x_mm, 50.0)
        self.assertTrue(loaded.geometry_stats.is_closed)

    def test_get_metadata_missing_returns_none(self):
        result = self.storage.get_model_metadata(self.project_id, "model-999")
        self.assertIsNone(result)

    def test_list_models_ordered(self):
        for i in ["001", "002", "003"]:
            mid = f"model-{i}"
            self.storage.create_model_dir(self.project_id, mid)
            self.storage.save_model_metadata(
                self.project_id, ModelMetadata(model_id=mid, prompt=f"step {i}")
            )
        models = self.storage.list_models(self.project_id)
        ids = [m.model_id for m in models]
        self.assertEqual(ids, ["model-001", "model-002", "model-003"])

    def test_latest_successful_model(self):
        self.storage.create_model_dir(self.project_id, "model-001")
        self.storage.save_model_metadata(
            self.project_id,
            ModelMetadata(model_id="model-001", prompt="failed",
                          failure_type=FailureType.EXECUTION_ERROR),
        )
        self.storage.create_model_dir(self.project_id, "model-002")
        self.storage.save_model_metadata(
            self.project_id,
            ModelMetadata(model_id="model-002", prompt="success", has_glb=True),
        )
        latest = self.storage.latest_successful_model(self.project_id)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.model_id, "model-002")

    def test_save_and_get_model_source(self):
        model_id = "model-001"
        self.storage.create_model_dir(self.project_id, model_id)
        src = 'import cadquery as cq\nresult = cq.Workplane("XY").box(10,10,10)'
        self.storage.save_model_text(self.project_id, model_id, "source.py", src)
        loaded = self.storage.get_model_source_text(self.project_id, model_id)
        self.assertEqual(loaded.strip(), src.strip())

    def test_get_source_missing_returns_empty(self):
        result = self.storage.get_model_source_text(self.project_id, "model-999")
        self.assertEqual(result, "")

    def test_save_geometry_analysis(self):
        model_id = "model-001"
        self.storage.create_model_dir(self.project_id, model_id)
        analysis = {"bounding_box": "50 × 30 × 10 mm", "volume": "15000 mm³"}
        self.storage.save_geometry_analysis(self.project_id, model_id, analysis)

        loaded = self.storage.get_geometry_analysis(self.project_id, model_id)
        self.assertEqual(loaded["bounding_box"], "50 × 30 × 10 mm")

    def test_get_geometry_analysis_missing(self):
        result = self.storage.get_geometry_analysis(self.project_id, "model-999")
        self.assertEqual(result, {})

    def test_get_model_renders_dir_created(self):
        model_id = "model-001"
        self.storage.create_model_dir(self.project_id, model_id)
        renders_dir = self.storage.get_model_renders_dir(self.project_id, model_id)
        self.assertTrue(renders_dir.exists())
        self.assertEqual(renders_dir.name, "renders")


class TestChatThreads(unittest.TestCase):

    def setUp(self):
        self.storage, self.tmp = _make_storage()
        self.storage.create_project(_make_config())
        self.project_id = "test-proj"

    def test_create_thread(self):
        thread = self.storage.create_chat_thread(self.project_id, "My Thread")
        self.assertEqual(thread["title"], "My Thread")
        self.assertIn("thread_id", thread)

    def test_list_threads(self):
        self.storage.create_chat_thread(self.project_id, "Thread A")
        self.storage.create_chat_thread(self.project_id, "Thread B")
        threads = self.storage.list_chat_threads(self.project_id)
        titles = [t["title"] for t in threads]
        self.assertIn("Thread A", titles)
        self.assertIn("Thread B", titles)

    def test_append_and_get_messages(self):
        thread = self.storage.create_chat_thread(self.project_id, "t1")
        tid = thread["thread_id"]

        self.storage.append_chat_thread_message(
            self.project_id, tid, ChatMessage(role="user", content="Hello")
        )
        self.storage.append_chat_thread_message(
            self.project_id, tid, ChatMessage(role="assistant", content="Hi there")
        )

        messages = self.storage.get_chat_thread_messages(self.project_id, tid)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[1].content, "Hi there")

    def test_rename_thread(self):
        thread = self.storage.create_chat_thread(self.project_id, "Old Name")
        tid = thread["thread_id"]
        self.storage.rename_chat_thread(self.project_id, tid, "New Name")
        loaded = self.storage.get_chat_thread(self.project_id, tid)
        self.assertEqual(loaded["title"], "New Name")

    def test_delete_thread(self):
        thread = self.storage.create_chat_thread(self.project_id, "Delete Me")
        tid = thread["thread_id"]
        self.storage.delete_chat_thread(self.project_id, tid)
        loaded = self.storage.get_chat_thread(self.project_id, tid)
        self.assertIsNone(loaded)

    def test_thread_title_auto_from_first_message(self):
        thread = self.storage.create_chat_thread(self.project_id, "New chat")
        tid = thread["thread_id"]
        self.storage.append_chat_thread_message(
            self.project_id, tid, ChatMessage(role="user", content="Design a box for screws")
        )
        loaded = self.storage.get_chat_thread(self.project_id, tid)
        # Auto-title should use the first user message
        self.assertIn("Design", loaded["title"])

    def test_messages_empty_for_missing_thread(self):
        messages = self.storage.get_chat_thread_messages(self.project_id, "missing-id")
        self.assertEqual(messages, [])


class TestModelFilePaths(unittest.TestCase):

    def setUp(self):
        self.storage, self.tmp = _make_storage()
        self.storage.create_project(_make_config())
        self.project_id = "test-proj"

    def test_get_model_file_path(self):
        path = self.storage.get_model_file_path(self.project_id, "model-001", "model.glb")
        self.assertTrue(str(path).endswith("model.glb"))
        self.assertIn("model-001", str(path))

    def test_save_model_binary_file(self):
        model_id = "model-001"
        self.storage.create_model_dir(self.project_id, model_id)
        content = b"FAKE_BINARY_DATA"
        path = self.storage.save_model_file(self.project_id, model_id, "test.bin", content)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
