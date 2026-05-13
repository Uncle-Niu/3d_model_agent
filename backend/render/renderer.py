"""
Server-side rendering service.

Generates multi-angle PNG images from CadQuery shapes for vision critique.

Strategy:
  1. Primary: Use pythonOCC (OCC) offscreen rendering via matplotlib 3D
     - Export shape to STL, load with trimesh, render with trimesh's built-in renderer
  2. Fallback: pygfx offscreen renderer (if available)

Produces 4 views:
  - isometric (iso)
  - front
  - right
  - top

Each view is saved as a PNG file in the model's renders/ subdirectory.
"""

from __future__ import annotations

import io
import math
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cadquery as cq

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class RenderResult:
    success: bool
    message: str = ""
    renders: dict[str, str] = field(default_factory=dict)  # view_name → file_path


# ---------------------------------------------------------------------------
# Camera / view parameters
# ---------------------------------------------------------------------------

# Each view: (elevation_deg, azimuth_deg)
VIEWS: dict[str, tuple[float, float]] = {
    "iso":   (30.0,  45.0),
    "front": (0.0,   0.0),
    "right": (0.0,   90.0),
    "top":   (90.0,  0.0),
}

# Section views: (plane_name, elevation_deg, azimuth_deg)
SECTION_VIEWS: dict[str, tuple[str, float, float]] = {
    "section_x": ("X", 0.0, 90.0),
    "section_y": ("Y", 0.0, 0.0),
}

IMAGE_SIZE = (512, 512)


# ---------------------------------------------------------------------------
# Trimesh-based renderer (lightweight, no Blender)
# ---------------------------------------------------------------------------

def _render_with_trimesh(
    stl_path: Path,
    output_dir: Path,
) -> dict[str, str]:
    """
    Render using trimesh + pyrender (headless).

    Falls back to matplotlib 3D if pyrender is unavailable.
    """
    try:
        import trimesh  # type: ignore
        import numpy as np

        mesh = trimesh.load(str(stl_path), force="mesh")
        if mesh is None or (hasattr(mesh, "is_empty") and mesh.is_empty):
            raise ValueError("Trimesh returned empty mesh")

        # Normalize: center mesh at origin, scale to unit cube
        mesh.apply_translation(-mesh.bounds.mean(axis=0))
        scale = max(mesh.extents)
        if scale > 0:
            mesh.apply_scale(2.0 / scale)

        renders: dict[str, str] = {}

        # Try pyrender first (real 3D)
        try:
            renders = _render_pyrender(mesh, output_dir)
        except Exception:
            # Fall back to matplotlib 3D projection
            renders = _render_matplotlib(mesh, output_dir)

        return renders

    except ImportError:
        raise RuntimeError(
            "trimesh is not installed. Run: pip install trimesh[easy]"
        )


def _render_pyrender(mesh, output_dir: Path) -> dict[str, str]:
    """Render with pyrender headless renderer."""
    import pyrender  # type: ignore
    import trimesh
    import numpy as np

    renders: dict[str, str] = {}
    W, H = IMAGE_SIZE

    tri_mesh = pyrender.Mesh.from_trimesh(
        mesh,
        smooth=True,
        material=pyrender.MetallicRoughnessMaterial(
            baseColorFactor=[0.4, 0.6, 0.85, 1.0],
            metallicFactor=0.2,
            roughnessFactor=0.6,
        ),
    )

    for view_name, (elev_deg, azim_deg) in VIEWS.items():
        scene = pyrender.Scene(ambient_light=[0.3, 0.3, 0.3], bg_color=[0.12, 0.12, 0.14, 1.0])
        scene.add(tri_mesh)

        # Camera setup
        camera = pyrender.PerspectiveCamera(yfov=math.radians(35), aspectRatio=W / H)

        # Convert elevation/azimuth to camera pose matrix
        elev = math.radians(elev_deg)
        azim = math.radians(azim_deg)
        dist = 4.0

        eye = np.array([
            dist * math.cos(elev) * math.sin(azim),
            dist * math.sin(elev),
            dist * math.cos(elev) * math.cos(azim),
        ])
        up = np.array([0.0, 1.0, 0.0])
        if abs(elev_deg) >= 85:
            up = np.array([0.0, 0.0, 1.0])

        forward = -eye / np.linalg.norm(eye)
        right = np.cross(forward, up)
        norm = np.linalg.norm(right)
        if norm < 1e-6:
            right = np.array([1.0, 0.0, 0.0])
        else:
            right = right / norm
        up_actual = np.cross(right, forward)

        pose = np.eye(4)
        pose[:3, 0] = right
        pose[:3, 1] = up_actual
        pose[:3, 2] = -forward
        pose[:3, 3] = eye

        scene.add(camera, pose=pose)

        # Lights
        light = pyrender.DirectionalLight(color=np.ones(3), intensity=4.0)
        light_pose = np.eye(4)
        light_pose[:3, 3] = [2.0, 4.0, 3.0]
        scene.add(light, pose=light_pose)

        light2 = pyrender.DirectionalLight(color=np.ones(3), intensity=2.0)
        light2_pose = np.eye(4)
        light2_pose[:3, 3] = [-2.0, 1.0, -2.0]
        scene.add(light2, pose=light2_pose)

        r = pyrender.OffscreenRenderer(W, H)
        try:
            color, _depth = r.render(scene)
        finally:
            r.delete()

        # Save PNG
        out_path = output_dir / f"render_{view_name}.png"
        from PIL import Image  # type: ignore
        img = Image.fromarray(color)
        img.save(str(out_path))
        renders[view_name] = str(out_path)

    return renders


def _render_matplotlib(mesh, output_dir: Path) -> dict[str, str]:
    """Render a simple 3D wireframe/surface plot using matplotlib."""
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")  # headless
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # type: ignore

    renders: dict[str, str] = {}

    for view_name, (elev_deg, azim_deg) in VIEWS.items():
        fig = plt.figure(figsize=(5, 5), dpi=100, facecolor="#1e1e22")
        ax = fig.add_subplot(111, projection="3d", facecolor="#1e1e22")

        verts = mesh.vertices[mesh.faces]
        poly = Poly3DCollection(
            verts,
            alpha=0.9,
            facecolor="#6699cc",
            edgecolor="#334455",
            linewidth=0.2,
        )
        ax.add_collection3d(poly)

        # Set axis limits based on mesh bounds
        bounds = mesh.bounds
        center = bounds.mean(axis=0)
        half_range = (bounds[1] - bounds[0]).max() / 2 * 1.2

        ax.set_xlim(center[0] - half_range, center[0] + half_range)
        ax.set_ylim(center[1] - half_range, center[1] + half_range)
        ax.set_zlim(center[2] - half_range, center[2] + half_range)

        ax.view_init(elev=elev_deg, azim=azim_deg)
        ax.set_axis_off()

        # Dark theme tick/pane colors
        ax.xaxis.pane.fill = False
        ax.yaxis.pane.fill = False
        ax.zaxis.pane.fill = False

        # Add label
        ax.text2D(0.05, 0.95, view_name.upper(), transform=ax.transAxes,
                  color="white", fontsize=10, fontweight="bold",
                  bbox=dict(facecolor="#00000080", edgecolor="none", boxstyle="round,pad=0.3"))

        out_path = output_dir / f"render_{view_name}.png"
        plt.savefig(str(out_path), dpi=100, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)

        renders[view_name] = str(out_path)

    return renders


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class RenderService:
    """Service that generates multi-angle renders from CadQuery shapes."""

    def __init__(self, output_base_dir: Optional[Path] = None):
        self.output_base_dir = output_base_dir

    def render_shape(
        self,
        shape: cq.Workplane,
        output_dir: Path,
        model_name: str = "part",
        include_sections: bool = True,
    ) -> RenderResult:
        """
        Render a CadQuery shape from multiple angles.

        Steps:
        1. Export shape to temporary STL
        2. Load with trimesh
        3. Render 4 views
        4. Save PNGs to output_dir/renders/

        Returns RenderResult with paths to all generated PNG files.
        """
        renders_dir = Path(output_dir) / "renders"
        renders_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: Export to STL in renders dir (used as intermediate)
        stl_path = renders_dir / "_render_source.stl"
        try:
            cq.exporters.export(shape, str(stl_path), exportType="STL", tolerance=0.02)
        except Exception as e:
            return RenderResult(
                success=False,
                message=f"Failed to export STL for rendering: {e}",
            )

        if not stl_path.exists() or stl_path.stat().st_size == 0:
            return RenderResult(
                success=False,
                message="Exported STL is empty — shape may be degenerate",
            )

        # Step 2: Render main views
        try:
            renders = _render_with_trimesh(stl_path, renders_dir)
        except RuntimeError as e:
            # trimesh not installed
            return RenderResult(success=False, message=str(e))
        except Exception as e:
            return RenderResult(
                success=False,
                message=f"Rendering failed: {traceback.format_exc()}",
            )

        # Step 3: Render sections (optional)
        if include_sections:
            try:
                self._render_sections(shape, renders_dir, renders)
            except Exception as e:
                # Log warning but continue
                print(f"Warning: Section rendering failed: {e}")

        if not renders:
            return RenderResult(success=False, message="No render images produced")

        return RenderResult(
            success=True,
            message=f"Rendered {len(renders)} views",
            renders=renders,
        )

    def _render_sections(self, shape: cq.Workplane, output_dir: Path, renders: dict[str, str]):
        """Generate section cut renders."""
        try:
            bb = shape.val().BoundingBox()
            center = [(bb.xmin + bb.xmax) / 2, (bb.ymin + bb.ymax) / 2, (bb.zmin + bb.zmax) / 2]
        except Exception:
            center = [0, 0, 0]

        for name, (plane, elev, azim) in SECTION_VIEWS.items():
            try:
                # Create sectioned shape
                if plane == "X":
                    sectioned = cq.Workplane("YZ", origin=(center[0], 0, 0)).add(shape.val()).split(keepTop=True)
                elif plane == "Y":
                    sectioned = cq.Workplane("XZ", origin=(0, center[1], 0)).add(shape.val()).split(keepTop=True)
                else:
                    continue

                # Export sectioned to temp STL
                section_stl = output_dir / f"_section_{name}.stl"
                cq.exporters.export(sectioned, str(section_stl), exportType="STL", tolerance=0.02)

                if not section_stl.exists() or section_stl.stat().st_size == 0:
                    continue

                # Render this specific view
                import trimesh
                mesh = trimesh.load(str(section_stl), force="mesh")
                if mesh is None or (hasattr(mesh, "is_empty") and mesh.is_empty):
                    continue

                # Normalize for consistent viewing
                mesh.apply_translation(-mesh.bounds.mean(axis=0))
                scale = max(mesh.extents)
                if scale > 0:
                    mesh.apply_scale(2.0 / scale)

                # Use matplotlib for sections as it's more robust in diverse environments
                # We only want ONE view for this specific section mesh
                # Temporary override VIEWS for _render_matplotlib call
                global VIEWS
                original_views = VIEWS
                VIEWS = {name: (elev, azim)}
                try:
                    section_renders = _render_matplotlib(mesh, output_dir)
                    renders.update(section_renders)
                finally:
                    VIEWS = original_views

            except Exception as e:
                print(f"Failed to render section {name}: {e}")


def render_shape_multiangle(
    shape: cq.Workplane,
    output_dir: Path,
    model_name: str = "part",
) -> RenderResult:
    """Convenience function to render a shape from multiple angles."""
    service = RenderService()
    return service.render_shape(shape, output_dir, model_name)
