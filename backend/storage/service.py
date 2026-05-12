"""
Storage service — filesystem-based, per-project storage.

Layout:
    data/projects/{project_id}/
        project.json
        chat_history.json
        models/
            model-001/
                source.py
                model.step
                model.stl
                model.glb
                render.png
                metadata.json
            model-002/
                ...
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Optional

from ..domain.models import ChatMessage, ModelMetadata, ProjectConfig


# Default data root — relative to where the server runs
DATA_ROOT = Path(os.environ.get("CAD_DATA_ROOT", "data"))


class StorageService:
    """Filesystem-based storage for projects and models."""

    def __init__(self, data_root: Path | None = None):
        self.root = data_root or DATA_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self.projects_dir = self.root / "projects"
        self.projects_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def create_project(self, config: ProjectConfig) -> Path:
        """Create a new project directory and save its config."""
        project_dir = self.projects_dir / config.project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "models").mkdir(exist_ok=True)

        self._write_json(project_dir / "project.json", config.model_dump(mode="json"))
        self._write_json(project_dir / "chat_history.json", [])
        return project_dir

    def get_project(self, project_id: str) -> Optional[ProjectConfig]:
        """Load a project config from disk."""
        config_path = self.projects_dir / project_id / "project.json"
        if not config_path.exists():
            return None
        data = self._read_json(config_path)
        return ProjectConfig(**data)

    def list_projects(self) -> list[ProjectConfig]:
        """List all projects."""
        projects = []
        if not self.projects_dir.exists():
            return projects
        for d in sorted(self.projects_dir.iterdir()):
            if d.is_dir():
                config = self.get_project(d.name)
                if config:
                    projects.append(config)
        return projects

    def update_project(self, config: ProjectConfig) -> None:
        """Update a project config on disk."""
        project_dir = self.projects_dir / config.project_id
        if not project_dir.exists():
            raise FileNotFoundError(f"Project {config.project_id} not found")
        self._write_json(project_dir / "project.json", config.model_dump(mode="json"))

    def delete_project(self, project_id: str) -> None:
        """Delete a project directory."""
        project_dir = self.projects_dir / project_id
        if project_dir.exists():
            shutil.rmtree(project_dir)

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------

    def next_model_id(self, project_id: str) -> str:
        """Generate the next sequential model ID for a project."""
        models_dir = self.projects_dir / project_id / "models"
        models_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(d.name for d in models_dir.iterdir() if d.is_dir())
        if not existing:
            return "model-001"
        last_num = int(existing[-1].split("-")[1])
        return f"model-{last_num + 1:03d}"

    def create_model_dir(self, project_id: str, model_id: str) -> Path:
        """Create a model directory and return its path."""
        model_dir = self.projects_dir / project_id / "models" / model_id
        model_dir.mkdir(parents=True, exist_ok=True)
        return model_dir

    def save_model_metadata(self, project_id: str, metadata: ModelMetadata) -> None:
        """Save model metadata to disk."""
        model_dir = self.projects_dir / project_id / "models" / metadata.model_id
        model_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(model_dir / "metadata.json", metadata.model_dump(mode="json"))

    def get_model_metadata(self, project_id: str, model_id: str) -> Optional[ModelMetadata]:
        """Load model metadata from disk."""
        meta_path = self.projects_dir / project_id / "models" / model_id / "metadata.json"
        if not meta_path.exists():
            return None
        data = self._read_json(meta_path)
        return ModelMetadata(**data)

    def list_models(self, project_id: str) -> list[ModelMetadata]:
        """List all models for a project, ordered by creation."""
        models_dir = self.projects_dir / project_id / "models"
        if not models_dir.exists():
            return []
        result = []
        for d in sorted(models_dir.iterdir()):
            if d.is_dir():
                meta = self.get_model_metadata(project_id, d.name)
                if meta:
                    result.append(meta)
        return result

    def get_model_file_path(self, project_id: str, model_id: str, filename: str) -> Path:
        """Get the full path to a model file."""
        return self.projects_dir / project_id / "models" / model_id / filename

    def save_model_file(self, project_id: str, model_id: str, filename: str, content: bytes) -> Path:
        """Save a binary file to the model directory."""
        model_dir = self.projects_dir / project_id / "models" / model_id
        model_dir.mkdir(parents=True, exist_ok=True)
        file_path = model_dir / filename
        file_path.write_bytes(content)
        return file_path

    def save_model_text(self, project_id: str, model_id: str, filename: str, content: str) -> Path:
        """Save a text file to the model directory."""
        model_dir = self.projects_dir / project_id / "models" / model_id
        model_dir.mkdir(parents=True, exist_ok=True)
        file_path = model_dir / filename
        file_path.write_text(content, encoding="utf-8")
        return file_path

    # ------------------------------------------------------------------
    # Chat History
    # ------------------------------------------------------------------

    def get_chat_history(self, project_id: str) -> list[ChatMessage]:
        """Load chat history for a project."""
        chat_path = self.projects_dir / project_id / "chat_history.json"
        if not chat_path.exists():
            return []
        data = self._read_json(chat_path)
        return [ChatMessage(**m) for m in data]

    def append_chat_message(self, project_id: str, message: ChatMessage) -> None:
        """Append a message to the chat history."""
        chat_path = self.projects_dir / project_id / "chat_history.json"
        history = []
        if chat_path.exists():
            history = self._read_json(chat_path)
        history.append(message.model_dump(mode="json"))
        self._write_json(chat_path, history)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_json(path: Path, data) -> None:
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    @staticmethod
    def _read_json(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))
