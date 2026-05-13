import pytest
from pathlib import Path
import tempfile
import shutil
import cadquery as cq
from backend.cad.importer import import_file
from backend.storage.service import StorageService

@pytest.fixture
def temp_project_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)

def test_import_step(temp_project_dir):
    # Create a dummy STEP file using CadQuery
    step_path = temp_project_dir / "test.step"
    box = cq.Workplane("XY").box(10, 10, 10)
    cq.exporters.export(box, str(step_path), exportType="STEP")
    
    result = import_file(step_path, temp_project_dir)
    
    assert result["success"] is True
    assert result["extension"] == ".step"
    assert Path(result["glb_path"]).exists()
    assert (temp_project_dir / "imports" / result["import_id"] / "test.step").exists()

def test_import_stl(temp_project_dir):
    # Create a dummy STL file
    stl_path = temp_project_dir / "test.stl"
    box = cq.Workplane("XY").box(10, 10, 10)
    cq.exporters.export(box, str(stl_path), exportType="STL")
    
    result = import_file(stl_path, temp_project_dir)
    
    assert result["success"] is True, result["message"]
    assert result["extension"] == ".stl"
    assert Path(result["glb_path"]).exists()

def test_import_glb(temp_project_dir):
    # Create a dummy GLB file
    glb_path = temp_project_dir / "test.glb"
    glb_path.write_text("dummy glb")
    
    result = import_file(glb_path, temp_project_dir)
    
    assert result["success"] is True
    assert result["extension"] == ".glb"
    assert Path(result["glb_path"]).exists()
    assert Path(result["glb_path"]).name == "test.glb"

def test_import_unsupported(temp_project_dir):
    txt_path = temp_project_dir / "test.txt"
    txt_path.write_text("hello")
    
    result = import_file(txt_path, temp_project_dir)
    
    assert result["success"] is False
    assert "Unsupported" in result["message"]
