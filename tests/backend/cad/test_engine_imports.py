import pytest
from pathlib import Path
import tempfile
import cadquery as cq
from unittest.mock import MagicMock
from backend.cad.engine import execute_cadquery_code, process_cadquery_code
from backend.storage.service import StorageService

@pytest.fixture
def mock_storage():
    storage = MagicMock(spec=StorageService)
    return storage

def test_load_import_helper(mock_storage):
    # Setup mock storage
    project_id = "proj-123"
    import_id = "imp-456"
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        import_dir = tmp_path / "imports" / import_id
        import_dir.mkdir(parents=True)
        step_file = import_dir / "base.step"
        
        # Create a real STEP file for the mock to point to
        box = cq.Workplane("XY").box(10, 10, 10)
        cq.exporters.export(box, str(step_file), exportType="STEP")
        
        mock_storage.get_import.return_value = {
            "import_id": import_id,
            "name": "base_part",
            "filename": "base.step",
            "extension": ".step"
        }
        mock_storage.get_project_dir.return_value = tmp_path
        
        # Code that uses the helper
        code = """
import cadquery as cq
base = load_import("base_part")
result = base.faces(">Z").workplane().circle(2).extrude(5)
"""
        success, shape, msg = execute_cadquery_code(code, project_id=project_id, storage=mock_storage)
        
        assert success is True, msg
        assert shape is not None
        # Verify it's a CadQuery object
        assert hasattr(shape, "extrude")

def test_load_import_not_found(mock_storage):
    mock_storage.get_import.return_value = None
    mock_storage.list_imports.return_value = []
    
    code = 'result = load_import("missing")'
    success, shape, msg = execute_cadquery_code(code, project_id="p", storage=mock_storage)
    
    assert success is False
    assert "not found" in msg

def test_load_import_invalid_format(mock_storage):
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        import_dir = tmp_path / "imports" / "1"
        import_dir.mkdir(parents=True)
        f = import_dir / "f.txt"
        f.write_text("dummy")
        
        mock_storage.get_import.return_value = {
            "import_id": "1", "name": "n", "filename": "f.txt", "extension": ".txt"
        }
        mock_storage.get_project_dir.return_value = tmp_path
        
        code = 'result = load_import("n")'
        success, shape, msg = execute_cadquery_code(code, project_id="p", storage=mock_storage)
        
        assert success is False
        assert "cannot be loaded" in msg
