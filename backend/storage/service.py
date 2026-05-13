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
from datetime import datetime, timezone
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
        (project_dir / "chat_threads").mkdir(exist_ok=True)

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

    def get_project_dir(self, project_id: str) -> Path:
        """Get the project directory path."""
        return self.projects_dir / project_id

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

    def latest_successful_model(self, project_id: str) -> Optional[ModelMetadata]:
        """Get the latest successful model checkpoint for a project."""
        models = [m for m in self.list_models(project_id) if m.has_glb and not m.failure_type]
        if not models:
            return None
        return models[-1]

    def get_model_source_text(self, project_id: str, model_id: str) -> str:
        """Load CadQuery source for a model checkpoint."""
        source_path = self.get_model_file_path(project_id, model_id, "source.py")
        if not source_path.exists():
            return ""
        return source_path.read_text(encoding="utf-8")

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

    def get_model_renders_dir(self, project_id: str, model_id: str) -> Path:
        """Get (and create) the renders sub-directory for a model."""
        renders_dir = self.projects_dir / project_id / "models" / model_id / "renders"
        renders_dir.mkdir(parents=True, exist_ok=True)
        return renders_dir

    def save_geometry_analysis(self, project_id: str, model_id: str, analysis: dict) -> None:
        """Persist geometry analysis data as analysis.json for the model."""
        model_dir = self.projects_dir / project_id / "models" / model_id
        model_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(model_dir / "analysis.json", analysis)

    def get_geometry_analysis(self, project_id: str, model_id: str) -> dict:
        """Load geometry analysis from disk, or return empty dict if not found."""
        path = self.projects_dir / project_id / "models" / model_id / "analysis.json"
        if not path.exists():
            return {}
        return self._read_json(path)

    def get_model_parameters(self, project_id: str, model_id: str) -> list[dict]:
        """Load editable parameters for a model."""
        path = self.projects_dir / project_id / "models" / model_id / "parameters.json"
        if not path.exists():
            return []
        return self._read_json(path)

    def get_model_features(self, project_id: str, model_id: str) -> list[dict]:
        """Load feature manifest for a model."""
        path = self.projects_dir / project_id / "models" / model_id / "features.json"
        if not path.exists():
            return []
        return self._read_json(path)

    def get_model_assembly(self, project_id: str, model_id: str) -> dict:
        """Load assembly manifest for a model."""
        path = self.projects_dir / project_id / "models" / model_id / "assembly_manifest.json"
        if not path.exists():
            return {}
        return self._read_json(path)


    # ------------------------------------------------------------------
    # Chat History
    # ------------------------------------------------------------------

    def create_chat_thread(self, project_id: str, title: str = "New chat") -> dict:
        """Create a chat thread for a project."""
        import uuid

        thread_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()
        thread = {
            "thread_id": thread_id,
            "title": title,
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }
        thread_dir = self.projects_dir / project_id / "chat_threads"
        thread_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(thread_dir / f"{thread_id}.json", thread)
        return thread

    def list_chat_threads(self, project_id: str) -> list[dict]:
        """List chat threads for a project, including legacy chat_history.json."""
        project_dir = self.projects_dir / project_id
        thread_dir = project_dir / "chat_threads"
        threads: list[dict] = []

        if thread_dir.exists():
            for path in sorted(thread_dir.glob("*.json")):
                data = self._read_json(path)
                messages = data.get("messages", [])
                threads.append({
                    "thread_id": data.get("thread_id", path.stem),
                    "title": data.get("title") or self._chat_thread_title(messages),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at") or data.get("created_at"),
                    "message_count": len(messages),
                    "last_message": messages[-1] if messages else None,
                })

        legacy_path = project_dir / "chat_history.json"
        if legacy_path.exists():
            messages = self._read_json(legacy_path)
            if messages:
                legacy_meta = self._read_legacy_chat_meta(project_id)
                threads.append({
                    "thread_id": "legacy",
                    "title": legacy_meta.get("title") or self._chat_thread_title(messages),
                    "created_at": messages[0].get("timestamp"),
                    "updated_at": legacy_meta.get("updated_at") or messages[-1].get("timestamp"),
                    "message_count": len(messages),
                    "last_message": messages[-1],
                })

        return sorted(
            threads,
            key=lambda t: t.get("updated_at") or t.get("created_at") or "",
            reverse=True,
        )

    def get_chat_thread(self, project_id: str, thread_id: str) -> dict | None:
        """Load a chat thread by ID."""
        if thread_id == "legacy":
            messages = self.get_chat_history(project_id)
            if not messages:
                return None
            data = [m.model_dump(mode="json") for m in messages]
            legacy_meta = self._read_legacy_chat_meta(project_id)
            return {
                "thread_id": "legacy",
                "title": legacy_meta.get("title") or self._chat_thread_title(data),
                "created_at": data[0].get("timestamp"),
                "updated_at": legacy_meta.get("updated_at") or data[-1].get("timestamp"),
                "messages": data,
            }

        path = self.projects_dir / project_id / "chat_threads" / f"{thread_id}.json"
        if not path.exists():
            return None
        return self._read_json(path)

    def get_chat_thread_messages(self, project_id: str, thread_id: str) -> list[ChatMessage]:
        """Load messages for a chat thread."""
        thread = self.get_chat_thread(project_id, thread_id)
        if not thread:
            return []
        return [ChatMessage(**m) for m in thread.get("messages", [])]

    def append_chat_thread_message(self, project_id: str, thread_id: str, message: ChatMessage) -> None:
        """Append a message to a chat thread."""
        if thread_id == "legacy":
            self.append_chat_message(project_id, message)
            return

        thread = self.get_chat_thread(project_id, thread_id)
        if not thread:
            now = datetime.now(timezone.utc).isoformat()
            thread = {
                "thread_id": thread_id,
                "title": "New chat",
                "created_at": now,
                "updated_at": now,
                "messages": [],
            }

        messages = thread.setdefault("messages", [])
        messages.append(message.model_dump(mode="json"))
        if thread.get("title") == "New chat" and message.role == "user":
            thread["title"] = self._chat_thread_title(messages)
        thread["updated_at"] = datetime.now(timezone.utc).isoformat()

        thread_dir = self.projects_dir / project_id / "chat_threads"
        thread_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(thread_dir / f"{thread_id}.json", thread)

    def rename_chat_thread(self, project_id: str, thread_id: str, title: str) -> dict | None:
        """Rename a chat thread."""
        thread = self.get_chat_thread(project_id, thread_id)
        if not thread:
            return None

        thread["title"] = title
        thread["updated_at"] = datetime.now(timezone.utc).isoformat()

        if thread_id == "legacy":
            meta_path = self.projects_dir / project_id / "legacy_chat_meta.json"
            self._write_json(meta_path, {"title": title, "updated_at": thread["updated_at"]})
        else:
            path = self.projects_dir / project_id / "chat_threads" / f"{thread_id}.json"
            self._write_json(path, thread)
        return thread

    def delete_chat_thread(self, project_id: str, thread_id: str) -> None:
        """Delete a chat thread."""
        if thread_id == "legacy":
            self._write_json(self.projects_dir / project_id / "chat_history.json", [])
            meta_path = self.projects_dir / project_id / "legacy_chat_meta.json"
            if meta_path.exists():
                meta_path.unlink()
            return

        path = self.projects_dir / project_id / "chat_threads" / f"{thread_id}.json"
        if path.exists():
            path.unlink()

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

    @staticmethod
    def _chat_thread_title(messages: list[dict]) -> str:
        for msg in messages:
            if msg.get("role") == "user" and msg.get("content"):
                title = " ".join(str(msg["content"]).split())
                return title[:48] + ("..." if len(title) > 48 else "")
        return "New chat"

    def _read_legacy_chat_meta(self, project_id: str) -> dict:
        meta_path = self.projects_dir / project_id / "legacy_chat_meta.json"
        if not meta_path.exists():
            return {}
        return self._read_json(meta_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_json(path: Path, data) -> None:
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    @staticmethod
    def _read_json(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))
