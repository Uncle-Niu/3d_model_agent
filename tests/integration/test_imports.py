import pytest
from pathlib import Path
import tempfile
import io
import cadquery as cq
from fastapi.testclient import TestClient
from backend.storage.service import StorageService

@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)

@pytest.fixture
def app_and_storage(temp_dir):
    import os
    os.environ["CAD_DATA_ROOT"] = str(temp_dir)
    
    from fastapi import FastAPI
    from backend.api.routes import router
    
    app = FastAPI()
    app.include_router(router, prefix="/api")
    
    storage = StorageService(temp_dir)
    app.state.storage = storage
    return app, storage

@pytest.fixture
def client(app_and_storage):
    app, _ = app_and_storage
    return TestClient(app)

def test_import_step_api(client, temp_dir):
    # 1. Create project
    resp = client.post("/api/projects", json={"name": "Test Import"})
    pid = resp.json()["project_id"]
    
    # 2. Create dummy STEP
    step_path = temp_dir / "test.step"
    box = cq.Workplane("XY").box(10, 10, 10)
    cq.exporters.export(box, str(step_path), exportType="STEP")
    
    # 3. Import via API
    with open(step_path, "rb") as f:
        resp = client.post(
            f"/api/projects/{pid}/imports",
            files={"file": ("test.step", f, "application/step")}
        )
    
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["import_data"]["extension"] == ".step"
    assert "glb_url" in data["import_data"]
    
    # 4. Check list
    resp = client.get(f"/api/projects/{pid}/imports")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    
    # 5. Check GLB serving
    import_id = data["import_data"]["import_id"]
    resp = client.get(f"/api/projects/{pid}/imports/{import_id}/glb")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "model/gltf-binary"

def test_import_stl_api(client, temp_dir):
    resp = client.post("/api/projects", json={"name": "Test STL"})
    pid = resp.json()["project_id"]
    
    stl_path = temp_dir / "test.stl"
    box = cq.Workplane("XY").box(10, 10, 10)
    cq.exporters.export(box, str(stl_path), exportType="STL")
    
    with open(stl_path, "rb") as f:
        resp = client.post(
            f"/api/projects/{pid}/imports",
            files={"file": ("test.stl", f, "application/sla")}
        )
    
    assert resp.status_code == 200
    assert resp.json()["success"] is True

def test_import_invalid_project(client, temp_dir):
    resp = client.post(
        "/api/projects/fake-pid/imports",
        files={"file": ("test.step", b"fake content", "application/step")}
    )
    assert resp.status_code == 404
