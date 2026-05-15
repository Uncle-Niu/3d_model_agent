"""
CAD Importer and Converter.
Handles STEP, STL, and GLB imports and conversions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
import uuid
import shutil
import cadquery as cq
import trimesh

from .engine import export_glb

def import_file(
    file_path: Path,
    project_dir: Path,
    name: Optional[str] = None
) -> dict[str, Any]:
    """
    Import a CAD file, convert to GLB for viewing, and return metadata.
    """
    ext = file_path.suffix.lower()
    if not name:
        name = file_path.stem
    
    import_id = str(uuid.uuid4())[:8]
    import_dir = project_dir / "imports" / import_id
    import_dir.mkdir(parents=True, exist_ok=True)
    
    # Copy original file
    dest_path = import_dir / file_path.name
    shutil.copy2(file_path, dest_path)
    
    result = {
        "import_id": import_id,
        "name": name,
        "filename": file_path.name,
        "extension": ext,
        "glb_path": None,
        "success": False,
        "message": ""
    }
    
    try:
        if ext == ".step" or ext == ".stp":
            shape = cq.importers.importStep(str(dest_path))
            glb_path = import_dir / "view.glb"
            export_glb(shape, glb_path, name=name)
            result["glb_path"] = str(glb_path)
            result["success"] = True
        elif ext == ".stl":
            # Use trimesh for STL to GLB conversion
            mesh = trimesh.load(str(dest_path))
            glb_path = import_dir / "view.glb"
            mesh.export(str(glb_path), file_type="glb")
            result["glb_path"] = str(glb_path)
            result["success"] = True
        elif ext == ".glb" or ext == ".gltf":
            # Already GLB, just use it
            result["glb_path"] = str(dest_path)
            result["success"] = True
        else:
            result["message"] = f"Unsupported file extension: {ext}"
            return result
            
        result["message"] = "Imported successfully"
    except Exception as e:
        result["success"] = False
        result["message"] = f"Import failed: {str(e)}"
        
    return result

