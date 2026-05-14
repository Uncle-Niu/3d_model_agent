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

IMAGE_SIZE = (768, 768)


# ---------------------------------------------------------------------------
# Trimesh-based renderer (lightweight, no Blender)
# ---------------------------------------------------------------------------

def _render_with_trimesh(
    stl_path: Path,
    output_dir: Path,
) -> dict[str, str]:
    """
    Render multi-angle PNGs from the STL.

    Renderer priority:
      1. VTK (proper Z-buffer, opaque solids, fast)
      2. pyrender (if installed)
      3. matplotlib 3D (fallback — depth ordering is approximate)
    """
    try:
        import trimesh  # type: ignore
        import numpy as np

        mesh = trimesh.load(str(stl_path), force="mesh")
        if mesh is None or (hasattr(mesh, "is_empty") and mesh.is_empty):
            raise ValueError("Trimesh returned empty mesh")

        # Capture real-world dimensions BEFORE normalizing so we can annotate scale
        real_extents = tuple(float(x) for x in mesh.extents)  # (Lx, Ly, Lz) in mm

        # Center mesh at origin in world coords (real-world units kept — VTK is happy with mm)
        mesh.apply_translation(-mesh.bounds.mean(axis=0))

        # 1. Try VTK first — best quality, works headless on Windows
        try:
            return _render_vtk(stl_path, output_dir, real_extents=real_extents)
        except Exception as e:
            # Fall through to next renderer
            pass

        # For pyrender/matplotlib we want unit-cube scaling so the camera is generic
        scale = max(mesh.extents)
        if scale > 0:
            mesh.apply_scale(2.0 / scale)

        # 2. pyrender
        try:
            return _render_pyrender(mesh, output_dir, real_extents=real_extents)
        except Exception:
            pass

        # 3. matplotlib fallback
        return _render_matplotlib(mesh, output_dir, real_extents=real_extents)

    except ImportError:
        raise RuntimeError(
            "trimesh is not installed. Run: pip install trimesh[easy]"
        )


# ---------------------------------------------------------------------------
# VTK-based offscreen renderer — proper Z-buffer, opaque solids
# ---------------------------------------------------------------------------

def _render_vtk(
    stl_path: Path,
    output_dir: Path,
    real_extents: Optional[tuple[float, float, float]] = None,
) -> dict[str, str]:
    """Render with VTK's offscreen render window.

    Uses an STL reader, two directional lights, and a matte plastic material so
    the resulting images look like a Bambu Studio preview — which is what
    multimodal LLMs have seen most of.
    """
    import vtk  # type: ignore

    W, H = IMAGE_SIZE

    reader = vtk.vtkSTLReader()
    reader.SetFileName(str(stl_path))
    reader.Update()

    # Compute polydata center so we can orbit a camera around it
    bounds = reader.GetOutput().GetBounds()  # (xmin, xmax, ymin, ymax, zmin, zmax)
    cx = (bounds[0] + bounds[1]) / 2.0
    cy = (bounds[2] + bounds[3]) / 2.0
    cz = (bounds[4] + bounds[5]) / 2.0
    dx = bounds[1] - bounds[0]
    dy = bounds[3] - bounds[2]
    dz = bounds[5] - bounds[4]
    diag = math.sqrt(dx * dx + dy * dy + dz * dz)
    if diag <= 0:
        diag = 1.0
    cam_dist = diag * 1.8

    # Add a feature-edge overlay so corners and hole rims are crisp. The mesh
    # arrives as triangles, so feature angle ~15° catches genuine edges (box
    # corners, hole boundaries) while still skipping fine tessellation noise.
    feature_edges = vtk.vtkFeatureEdges()
    feature_edges.SetInputConnection(reader.GetOutputPort())
    feature_edges.BoundaryEdgesOn()         # include open-mesh boundaries (rare on watertight CAD)
    feature_edges.FeatureEdgesOn()
    feature_edges.SetFeatureAngle(15.0)
    feature_edges.NonManifoldEdgesOff()
    feature_edges.ManifoldEdgesOff()
    feature_edges.ColoringOff()

    renders: dict[str, str] = {}

    for view_name, (elev_deg, azim_deg) in VIEWS.items():
        renderer = vtk.vtkRenderer()
        renderer.SetBackground(0.95, 0.95, 0.96)

        # Main mesh actor
        mesh_mapper = vtk.vtkPolyDataMapper()
        mesh_mapper.SetInputConnection(reader.GetOutputPort())
        mesh_mapper.ScalarVisibilityOff()
        mesh_actor = vtk.vtkActor()
        mesh_actor.SetMapper(mesh_mapper)
        prop = mesh_actor.GetProperty()
        prop.SetColor(0.80, 0.81, 0.85)
        prop.SetAmbient(0.30)
        prop.SetDiffuse(0.75)
        prop.SetSpecular(0.10)
        prop.SetSpecularPower(20)
        prop.SetInterpolationToGouraud()
        renderer.AddActor(mesh_actor)

        # Feature-edge actor
        edge_mapper = vtk.vtkPolyDataMapper()
        edge_mapper.SetInputConnection(feature_edges.GetOutputPort())
        edge_mapper.ScalarVisibilityOff()
        edge_actor = vtk.vtkActor()
        edge_actor.SetMapper(edge_mapper)
        eprop = edge_actor.GetProperty()
        eprop.SetColor(0.10, 0.12, 0.16)
        eprop.SetLineWidth(1.2)
        eprop.SetLighting(False)
        renderer.AddActor(edge_actor)

        # Camera: orbit around the center using elev/azim convention
        elev = math.radians(elev_deg)
        azim = math.radians(azim_deg)
        eye_x = cx + cam_dist * math.cos(elev) * math.sin(azim)
        eye_y = cy - cam_dist * math.cos(elev) * math.cos(azim)
        eye_z = cz + cam_dist * math.sin(elev)
        camera = renderer.GetActiveCamera()
        camera.SetPosition(eye_x, eye_y, eye_z)
        camera.SetFocalPoint(cx, cy, cz)
        # Z-up is the CAD convention; top view needs a different up vector
        if abs(elev_deg) >= 85:
            camera.SetViewUp(0.0, 1.0, 0.0)
        else:
            camera.SetViewUp(0.0, 0.0, 1.0)
        camera.SetViewAngle(28.0)

        # Let VTK compute clipping and dolly the camera so the model nicely fills
        # the frame. ResetCamera uses the renderer bounds, so we call it after
        # adding actors and lights.
        renderer.ResetCamera()
        renderer.GetActiveCamera().Zoom(1.15)   # slight tighten — keeps small features readable
        renderer.ResetCameraClippingRange()

        # Lights — one key, one fill, one rim, all positional in camera space
        renderer.RemoveAllLights()
        for (lx, ly, lz, intensity) in [
            (eye_x + dx, eye_y + dy, eye_z + dz, 0.85),
            (eye_x - dx * 0.5, eye_y - dy * 0.5, eye_z + dz, 0.35),
            (cx, cy, cz + diag * 2.0, 0.30),
        ]:
            light = vtk.vtkLight()
            light.SetLightTypeToSceneLight()
            light.SetPosition(lx, ly, lz)
            light.SetFocalPoint(cx, cy, cz)
            light.SetIntensity(intensity)
            renderer.AddLight(light)

        render_window = vtk.vtkRenderWindow()
        render_window.SetOffScreenRendering(1)
        render_window.SetSize(W, H)
        render_window.AddRenderer(renderer)
        render_window.SetMultiSamples(4)
        render_window.Render()

        wif = vtk.vtkWindowToImageFilter()
        wif.SetInput(render_window)
        wif.SetInputBufferTypeToRGB()
        wif.ReadFrontBufferOff()
        wif.Update()

        writer = vtk.vtkPNGWriter()
        out_path = output_dir / f"render_{view_name}.png"
        writer.SetFileName(str(out_path))
        writer.SetInputConnection(wif.GetOutputPort())
        writer.Write()

        _annotate_render(out_path, view_name, real_extents)
        renders[view_name] = str(out_path)

        # Explicitly release the render window so the GL/D3D context doesn't leak
        render_window.Finalize()

    if not renders:
        raise RuntimeError("VTK renderer produced no images")
    return renders


# ---------------------------------------------------------------------------
# Post-processing: annotate renders so a VLM can read view + scale at a glance
# ---------------------------------------------------------------------------

def _annotate_render(
    image_path: Path,
    view_name: str,
    real_extents: Optional[tuple[float, float, float]],
) -> None:
    """Draw view label, axis triad, and scale text onto a saved PNG.

    The renderer above produces images of an isotropically-scaled mesh. We add
    overlays so the VLM has:
      - an unambiguous view name (otherwise iso/front/right look similar)
      - the real-world bounding box so it can reason about proportions
      - a tiny axis triad reminding it which way is +Z (print direction)
    """
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except ImportError:
        return

    try:
        img = Image.open(image_path).convert("RGB")
    except Exception:
        return

    draw = ImageDraw.Draw(img)
    W, H = img.size

    # Pick a font that exists on Windows; fall back to default bitmap font.
    font_label = None
    font_small = None
    for font_name in ("arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "segoeui.ttf"):
        try:
            font_label = ImageFont.truetype(font_name, max(18, W // 40))
            font_small = ImageFont.truetype(font_name, max(14, W // 55))
            break
        except (OSError, IOError):
            continue
    if font_label is None:
        font_label = ImageFont.load_default()
        font_small = ImageFont.load_default()

    # 1. View label (top-left, with pill background)
    label = view_name.upper()
    bbox = draw.textbbox((0, 0), label, font=font_label)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 8
    draw.rectangle(
        [pad, pad, pad + tw + 2 * pad, pad + th + 2 * pad],
        fill=(0, 0, 0, 220),
    )
    draw.text((pad * 2, pad * 2 - 2), label, font=font_label, fill=(255, 255, 255))

    # 2. Real-world dimensions (top-right)
    if real_extents:
        dx, dy, dz = real_extents
        dim_text = f"BBox: {dx:.1f} × {dy:.1f} × {dz:.1f} mm"
        bbox2 = draw.textbbox((0, 0), dim_text, font=font_small)
        tw2, th2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]
        draw.rectangle(
            [W - tw2 - 3 * pad, pad, W - pad, pad + th2 + 2 * pad],
            fill=(0, 0, 0, 220),
        )
        draw.text((W - tw2 - 2 * pad, pad * 2 - 2), dim_text, font=font_small, fill=(255, 255, 255))

    # 3. Axis triad (bottom-left). Shows +X red, +Y green, +Z blue.
    # NOTE: image Y axis grows downward, so a positive screen-up direction is dy<0.
    cx, cy = 60, H - 60
    L = 40
    axis_specs = {
        # (dx, dy) = direction of the world axis as it appears on screen
        "iso":   ((+0.9, +0.45), (-0.9, +0.45), (0, -1)),  # standard iso: Z up, X right-front, Y left-front
        "front": ((+1, 0), (0, 0), (0, -1)),               # looking down -Y: X right, Z up
        "right": ((0, 0), (+1, 0), (0, -1)),               # looking down -X: Y right, Z up
        "top":   ((+1, 0), (0, -1), (0, 0)),               # looking down -Z: X right, Y up
        "section_x": ((0, 0), (+1, 0), (0, -1)),
        "section_y": ((+1, 0), (0, 0), (0, -1)),
    }
    axes = axis_specs.get(view_name, axis_specs["iso"])
    colors = [(220, 50, 50), (50, 180, 50), (60, 110, 230)]
    labels = ["+X", "+Y", "+Z"]
    for (dx_a, dy_a), col, lab in zip(axes, colors, labels):
        if dx_a == 0 and dy_a == 0:
            continue
        ex, ey = cx + dx_a * L, cy + dy_a * L
        draw.line([(cx, cy), (ex, ey)], fill=col, width=3)
        # Place label slightly past the tip so it doesn't overlap the line
        draw.text((ex + 4, ey - 10), lab, font=font_small, fill=col)

    # 4. 10mm scale bar (bottom-right) — only meaningful with real_extents
    if real_extents:
        max_real = max(real_extents)
        if max_real > 0:
            # mesh was scaled to fit in [-1,+1] (2 units), so:
            # pixels per unit ≈ W / 2 * 0.45 (camera at dist=4 with yfov=35°);
            # easier path: use a fixed visible fraction of width per 10mm.
            scale_real_mm = 10.0
            unit_per_mm = 2.0 / max_real  # mesh units per real mm
            pixels_per_unit_estimate = W / 3.2  # approximate from camera
            bar_px = max(20, int(pixels_per_unit_estimate * unit_per_mm * scale_real_mm))
            bar_px = min(bar_px, W // 3)
            bx2 = W - 30
            bx1 = bx2 - bar_px
            by = H - 30
            draw.line([(bx1, by), (bx2, by)], fill=(255, 255, 255), width=4)
            draw.line([(bx1, by - 5), (bx1, by + 5)], fill=(255, 255, 255), width=2)
            draw.line([(bx2, by - 5), (bx2, by + 5)], fill=(255, 255, 255), width=2)
            draw.text((bx1, by + 6), f"~{scale_real_mm:.0f}mm", font=font_small, fill=(255, 255, 255))

    img.save(image_path)


def _render_pyrender(mesh, output_dir: Path, real_extents: Optional[tuple[float, float, float]] = None) -> dict[str, str]:
    """Render with pyrender headless renderer."""
    import pyrender  # type: ignore
    import trimesh
    import numpy as np

    renders: dict[str, str] = {}
    W, H = IMAGE_SIZE

    # Matte off-white plastic — gives VLMs cleaner shape silhouettes than a saturated blue.
    tri_mesh = pyrender.Mesh.from_trimesh(
        mesh,
        smooth=True,
        material=pyrender.MetallicRoughnessMaterial(
            baseColorFactor=[0.82, 0.82, 0.86, 1.0],
            metallicFactor=0.05,
            roughnessFactor=0.55,
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
        _annotate_render(out_path, view_name, real_extents)
        renders[view_name] = str(out_path)

    return renders


def _compute_face_shading(mesh, light_dir=(0.5, -0.3, 0.85)) -> "np.ndarray":
    """Per-face brightness from a single directional light (range 0..1).

    Used to produce flat-shaded faces that read clearly for a VLM without the
    triangulation-edge noise matplotlib's default Poly3DCollection gives us.
    """
    import numpy as np
    n = mesh.face_normals
    L = np.array(light_dir, dtype=float)
    L /= max(np.linalg.norm(L), 1e-9)
    intensity = np.clip(n @ L, 0.0, 1.0)
    # Add ambient term so shadow side is still visible
    return 0.35 + 0.65 * intensity


def _extract_feature_edges(mesh, angle_deg: float = 25.0):
    """Return edges (as Nx2 vertex-index pairs) where neighboring faces meet at a
    sharp angle. Skipping smooth-surface diagonals keeps the wireframe overlay
    informative (corners, hole boundaries) instead of visually noisy."""
    import numpy as np
    try:
        adjacency = mesh.face_adjacency                  # (M, 2) face index pairs
        adj_angles = mesh.face_adjacency_angles           # (M,) radians
        adj_edges = mesh.face_adjacency_edges             # (M, 2) vertex index pairs
        threshold = np.deg2rad(angle_deg)
        keep = adj_angles > threshold
        return adj_edges[keep]
    except Exception:
        return np.empty((0, 2), dtype=int)


def _render_matplotlib(mesh, output_dir: Path, real_extents: Optional[tuple[float, float, float]] = None) -> dict[str, str]:
    """Render flat-shaded views via matplotlib for VLM consumption.

    Improvements vs the previous version:
    - Per-face shading from a single directional light (no per-triangle edges).
    - Only draws feature edges (corners, hole rims) — not every triangulation
      diagonal — so the result reads like a real CAD picture, not a mesh.
    - Light background, off-white plastic material — closer to a Bambu Studio
      preview, which the vision model has seen plenty of.
    """
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")  # headless
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection, Line3DCollection  # type: ignore

    renders: dict[str, str] = {}
    bg = "#f3f3f5"

    # Precompute shading (same across views — light is fixed in world coords)
    shading = _compute_face_shading(mesh)
    base_rgb = np.array([0.78, 0.79, 0.83])  # warm off-white plastic
    face_colors = np.clip(np.outer(shading, base_rgb), 0.0, 1.0)

    # Feature-edge wireframe (drawn once, reused across views)
    feature_edges = _extract_feature_edges(mesh, angle_deg=20.0)

    for view_name, (elev_deg, azim_deg) in VIEWS.items():
        fig = plt.figure(figsize=(IMAGE_SIZE[0] / 100, IMAGE_SIZE[1] / 100), dpi=100, facecolor=bg)
        ax = fig.add_subplot(111, projection="3d", facecolor=bg)

        verts = mesh.vertices[mesh.faces]
        poly = Poly3DCollection(
            verts,
            # Full opacity — matplotlib's depth sort is approximate; alpha < 1.0
            # makes back faces bleed through, which confuses the VLM.
            facecolors=face_colors,
            edgecolors=face_colors,  # match face color → no triangulation lines
            linewidth=0.0,
        )
        # Disable matplotlib's edge antialiasing so face-color edges don't bleed.
        try:
            poly.set_alpha(1.0)
            poly.set_zsort("max")  # 'max' gives better depth ordering for CAD meshes
        except Exception:
            pass
        ax.add_collection3d(poly)

        if len(feature_edges) > 0:
            edge_segments = mesh.vertices[feature_edges]
            line_coll = Line3DCollection(
                edge_segments,
                colors=(0.10, 0.12, 0.16, 1.0),
                linewidths=1.0,
            )
            ax.add_collection3d(line_coll)

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

        # Don't draw the matplotlib in-axes label — the post-process step writes a
        # clearer overlay (view name, axis triad, scale bar).
        out_path = output_dir / f"render_{view_name}.png"
        plt.savefig(str(out_path), dpi=100, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)

        _annotate_render(out_path, view_name, real_extents)
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
