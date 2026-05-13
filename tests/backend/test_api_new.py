"""
Integration tests for new API endpoints (parameters, features).
"""

import pytest
from fastapi.testclient import TestClient
from backend.app import app


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def test_get_parameters_and_features(client):
    # 1. Create a project
    resp = client.post("/api/projects", json={"name": "Test Project"})
    assert resp.status_code == 200
    project_id = resp.json()["project_id"]

    # 2. Execute some source to create a model
    source = """
length = 100
width = 50
result = cq.Workplane().box(length, width, 10)
"""
    resp = client.post(f"/api/projects/{project_id}/models/execute_source", json={
        "source": source,
        "prompt": "Test model"
    })
    assert resp.status_code == 200
    model_id = resp.json()["model"]["model_id"]

    # 3. Test GET parameters
    resp = client.get(f"/api/projects/{project_id}/models/{model_id}/parameters")
    assert resp.status_code == 200
    params = resp.json()
    assert len(params) == 2
    names = {p["name"] for p in params}
    assert "length" in names
    assert "width" in names

    # 4. Test GET features
    resp = client.get(f"/api/projects/{project_id}/models/{model_id}/features")
    assert resp.status_code == 200
    features = resp.json()
    assert len(features) >= 1
    assert features[0]["name"] == "part"


def test_update_parameters(client):
    # 1. Create a project
    resp = client.post("/api/projects", json={"name": "Update Param Project"})
    project_id = resp.json()["project_id"]

    # 2. Execute source
    source = "length = 100\nresult = cq.Workplane().box(length, 10, 10)"
    resp = client.post(f"/api/projects/{project_id}/models/execute_source", json={"source": source})
    model_id = resp.json()["model"]["model_id"]

    # 3. Update parameters
    resp = client.post(
        f"/api/projects/{project_id}/models/{model_id}/update_parameters",
        json={"parameters": {"length": 200}}
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["success"] == True
    new_model_id = result["model"]["model_id"]
    assert new_model_id != model_id

    # 4. Verify new source
    resp = client.get(f"/api/projects/{project_id}/models/{new_model_id}/source")
    assert resp.status_code == 200
    new_source = resp.text
    assert "length = 200" in new_source
