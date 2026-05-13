import sys
from pathlib import Path
# Add current dir to path
sys.path.insert(0, str(Path.cwd()))

import backend
print(f"Backend file: {backend.__file__}")

from backend.render.renderer import RenderService
import cadquery as cq

service = RenderService()
shape = cq.Workplane("XY").box(10, 10, 10)
out_dir = Path("temp_render_test")
out_dir.mkdir(exist_ok=True)

result = service.render_shape(shape, out_dir, include_sections=True)
print(f"Success: {result.success}")
print(f"Renders: {list(result.renders.keys())}")
