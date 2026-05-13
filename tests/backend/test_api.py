"""
Integration tests for the REST API using FastAPI TestClient.

These tests exercise the HTTP layer end-to-end (no real Ollama needed).
CAD execution is mocked where the test only targets the API routing logic.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from backend.domain.models import ProjectConfig, HardConstraints, SoftConstraints


def _make_app(data_root: Path):
    """Create a fresh FastAPI app pointing at a temp directory."""
    import os
    os.environ["CAD_DATA_ROOT"] = str(data_root)

    from backend.storage.service import StorageService
    from backend.api.routes import router
    from backend.api.websocket import ws_router
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.include_router(router, prefix="/api")
    app.include_router(ws_router)

    storage = StorageService(data_root)
    app.state.storage = storage
    return app, storage


class TestProjectEndpoints(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app, self.storage = _make_app(self.tmp)
        self.client = TestClient(self.app)

    # --- Create ---
    def test_create_project_returns_201_data(self):
        resp = self.client.post("/api/projects", json={"name": "My Part"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["name"], "My Part")
        self.assertIn("project_id", data)

    def test_create_project_default_name(self):
        resp = self.client.post("/api/projects", json={})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("project_id", resp.json())

    def test_create_project_with_constraints(self):
        body = {
            "name": "Small Part",
            "hard_constraints": {"max_x_mm": 100, "max_y_mm": 80, "max_z_mm": 60},
        }
        resp = self.client.post("/api/projects", json=body)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["hard_constraints"]["max_x_mm"], 100)

    # --- List ---
    def test_list_projects_empty(self):
        resp = self.client.get("/api/projects")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_list_projects_after_create(self):
        self.client.post("/api/projects", json={"name": "P1"})
        self.client.post("/api/projects", json={"name": "P2"})
        resp = self.client.get("/api/projects")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 2)

    # --- Get ---
    def test_get_project_found(self):
        create_resp = self.client.post("/api/projects", json={"name": "Find Me"})
        pid = create_resp.json()["project_id"]
        resp = self.client.get(f"/api/projects/{pid}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Find Me")

    def test_get_project_not_found(self):
        resp = self.client.get("/api/projects/nonexistent-id")
        self.assertEqual(resp.status_code, 404)

    # --- Update ---
    def test_update_project_name(self):
        create_resp = self.client.post("/api/projects", json={"name": "Old"})
        pid = create_resp.json()["project_id"]
        resp = self.client.put(f"/api/projects/{pid}", json={"name": "New"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "New")

    def test_update_nonexistent_project(self):
        resp = self.client.put("/api/projects/fake-id", json={"name": "X"})
        self.assertEqual(resp.status_code, 404)

    # --- Delete ---
    def test_delete_project(self):
        create_resp = self.client.post("/api/projects", json={"name": "Del"})
        pid = create_resp.json()["project_id"]
        del_resp = self.client.delete(f"/api/projects/{pid}")
        self.assertEqual(del_resp.status_code, 200)
        get_resp = self.client.get(f"/api/projects/{pid}")
        self.assertEqual(get_resp.status_code, 404)

    # --- Constraints ---
    def test_update_constraints(self):
        create_resp = self.client.post("/api/projects", json={"name": "P"})
        pid = create_resp.json()["project_id"]
        resp = self.client.put(
            f"/api/projects/{pid}/constraints",
            json={"hard_constraints": {"max_x_mm": 150}},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["hard_constraints"]["max_x_mm"], 150)


class TestModelEndpoints(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app, self.storage = _make_app(self.tmp)
        self.client = TestClient(self.app)

        # Create a project and a successful model
        create_resp = self.client.post("/api/projects", json={"name": "Test"})
        self.pid = create_resp.json()["project_id"]
        self._setup_model()

    def _setup_model(self):
        """Create a fake model with files on disk."""
        from backend.domain.models import ModelMetadata
        mid = "model-001"
        model_dir = self.storage.create_model_dir(self.pid, mid)

        # Write dummy files
        (model_dir / "model.glb").write_bytes(b"FAKE_GLB")
        (model_dir / "model.step").write_bytes(b"FAKE_STEP")
        (model_dir / "model.stl").write_bytes(b"FAKE_STL")
        (model_dir / "source.py").write_text("result = None")

        meta = ModelMetadata(
            model_id=mid, prompt="test box",
            has_step=True, has_stl=True, has_glb=True, iteration=1,
        )
        self.storage.save_model_metadata(self.pid, meta)
        self.mid = mid

    def test_list_models(self):
        resp = self.client.get(f"/api/projects/{self.pid}/models")
        self.assertEqual(resp.status_code, 200)
        models = resp.json()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["model_id"], "model-001")

    def test_get_glb(self):
        resp = self.client.get(f"/api/projects/{self.pid}/models/{self.mid}/glb")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "model/gltf-binary")

    def test_get_step(self):
        resp = self.client.get(f"/api/projects/{self.pid}/models/{self.mid}/step")
        self.assertEqual(resp.status_code, 200)

    def test_get_stl(self):
        resp = self.client.get(f"/api/projects/{self.pid}/models/{self.mid}/stl")
        self.assertEqual(resp.status_code, 200)

    def test_get_source(self):
        resp = self.client.get(f"/api/projects/{self.pid}/models/{self.mid}/source")
        self.assertEqual(resp.status_code, 200)

    def test_get_glb_missing(self):
        resp = self.client.get(f"/api/projects/{self.pid}/models/model-999/glb")
        self.assertEqual(resp.status_code, 404)

    def test_get_analysis_empty(self):
        resp = self.client.get(f"/api/projects/{self.pid}/models/{self.mid}/analysis")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), dict)

    def test_get_analysis_with_data(self):
        self.storage.save_geometry_analysis(
            self.pid, self.mid, {"bounding_box": "50 × 30 × 10 mm"}
        )
        resp = self.client.get(f"/api/projects/{self.pid}/models/{self.mid}/analysis")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("bounding_box", resp.json())

    def test_get_metadata_endpoint(self):
        resp = self.client.get(f"/api/projects/{self.pid}/models/{self.mid}/metadata")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["model_id"], "model-001")
        self.assertEqual(data["prompt"], "test box")

    def test_get_render_invalid_view(self):
        resp = self.client.get(
            f"/api/projects/{self.pid}/models/{self.mid}/renders/invalid_view"
        )
        self.assertEqual(resp.status_code, 400)

    def test_get_render_valid_view_missing_file(self):
        resp = self.client.get(
            f"/api/projects/{self.pid}/models/{self.mid}/renders/iso"
        )
        self.assertEqual(resp.status_code, 404)

    def test_get_render_valid_view_exists(self):
        renders_dir = self.storage.get_model_renders_dir(self.pid, self.mid)
        (renders_dir / "render_iso.png").write_bytes(b"PNG_DATA")
        resp = self.client.get(
            f"/api/projects/{self.pid}/models/{self.mid}/renders/iso"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/png")


class TestExecuteSourceEndpoint(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app, self.storage = _make_app(self.tmp)
        self.client = TestClient(self.app)
        create_resp = self.client.post("/api/projects", json={"name": "P"})
        self.pid = create_resp.json()["project_id"]

    def test_execute_valid_source(self):
        resp = self.client.post(
            f"/api/projects/{self.pid}/models/execute_source",
            json={
                "source": 'import cadquery as cq\nresult = cq.Workplane("XY").box(10,10,10)',
                "prompt": "unit test box",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertIsNotNone(data["glb_url"])

    def test_execute_invalid_source_returns_failure(self):
        resp = self.client.post(
            f"/api/projects/{self.pid}/models/execute_source",
            json={"source": "this is not python ###", "prompt": "bad code"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertNotEqual(data["message"], "")

    def test_execute_empty_source_rejected(self):
        resp = self.client.post(
            f"/api/projects/{self.pid}/models/execute_source",
            json={"source": "   ", "prompt": "empty"},
        )
        self.assertEqual(resp.status_code, 400)


class TestHealthEndpoint(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app, _ = _make_app(self.tmp)
        self.client = TestClient(self.app)

    def test_health_returns_200(self):
        # Ollama likely not running in test env — health is still 200
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("status", data)
        self.assertIn("llm_model", data)

    def test_health_has_model_fields(self):
        resp = self.client.get("/api/health")
        data = resp.json()
        self.assertIn("ollama_connected", data)
        self.assertIn("available_models", data)
        self.assertIn("model_available", data)


class TestChatThreadEndpoints(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app, self.storage = _make_app(self.tmp)
        self.client = TestClient(self.app)
        create_resp = self.client.post("/api/projects", json={"name": "P"})
        self.pid = create_resp.json()["project_id"]

    def test_create_thread(self):
        resp = self.client.post(
            f"/api/projects/{self.pid}/chat_threads", json={"title": "Thread 1"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "Thread 1")

    def test_list_threads(self):
        self.client.post(f"/api/projects/{self.pid}/chat_threads", json={"title": "A"})
        self.client.post(f"/api/projects/{self.pid}/chat_threads", json={"title": "B"})
        resp = self.client.get(f"/api/projects/{self.pid}/chat_threads")
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.json()), 2)

    def test_get_thread(self):
        create_resp = self.client.post(
            f"/api/projects/{self.pid}/chat_threads", json={"title": "T"}
        )
        tid = create_resp.json()["thread_id"]
        resp = self.client.get(f"/api/projects/{self.pid}/chat_threads/{tid}")
        self.assertEqual(resp.status_code, 200)

    def test_rename_thread(self):
        create_resp = self.client.post(
            f"/api/projects/{self.pid}/chat_threads", json={"title": "Old"}
        )
        tid = create_resp.json()["thread_id"]
        resp = self.client.put(
            f"/api/projects/{self.pid}/chat_threads/{tid}", json={"title": "New"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "New")

    def test_rename_empty_title_rejected(self):
        create_resp = self.client.post(
            f"/api/projects/{self.pid}/chat_threads", json={"title": "T"}
        )
        tid = create_resp.json()["thread_id"]
        resp = self.client.put(
            f"/api/projects/{self.pid}/chat_threads/{tid}", json={"title": "  "}
        )
        self.assertEqual(resp.status_code, 400)

    def test_delete_thread(self):
        create_resp = self.client.post(
            f"/api/projects/{self.pid}/chat_threads", json={"title": "Del"}
        )
        tid = create_resp.json()["thread_id"]
        del_resp = self.client.delete(f"/api/projects/{self.pid}/chat_threads/{tid}")
        self.assertEqual(del_resp.status_code, 200)
        get_resp = self.client.get(f"/api/projects/{self.pid}/chat_threads/{tid}")
        self.assertEqual(get_resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
