"""Notifier abstraction: Telegram in production, JSONL file in the local harness / tests."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol

Buttons = list[list[tuple[str, str]]]  # rows of (label, callback_data)


class Notifier(Protocol):
    async def send(self, user_id: int, text: str, buttons: Buttons | None = None) -> None: ...
    async def send_photo(self, user_id: int, path: str, caption: str = "") -> None: ...
    async def send_document(self, user_id: int, path: str, caption: str = "") -> None: ...


class MemoryNotifier:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(self, user_id: int, text: str, buttons: Buttons | None = None) -> None:
        self.messages.append({"user_id": user_id, "text": text, "buttons": buttons or []})

    async def send_photo(self, user_id: int, path: str, caption: str = "") -> None:
        self.messages.append({"user_id": user_id, "photo": path, "text": caption, "buttons": []})

    async def send_document(self, user_id: int, path: str, caption: str = "") -> None:
        self.messages.append({"user_id": user_id, "document": path, "text": caption, "buttons": []})

    def texts(self) -> list[str]:
        return [m["text"] for m in self.messages]


class JsonlNotifier(MemoryNotifier):
    """Writes every outbound message to a JSONL file (Telegram send layer stub)."""

    def __init__(self, path: str):
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, rec: dict[str, Any]) -> None:
        rec = {"ts": int(time.time()), **rec}
        with self.path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    async def send(self, user_id: int, text: str, buttons: Buttons | None = None) -> None:
        await super().send(user_id, text, buttons)
        self._write({"type": "message", "user_id": user_id, "text": text, "buttons": buttons or []})

    async def send_photo(self, user_id: int, path: str, caption: str = "") -> None:
        await super().send_photo(user_id, path, caption)
        self._write({"type": "photo", "user_id": user_id, "path": path, "caption": caption})

    async def send_document(self, user_id: int, path: str, caption: str = "") -> None:
        await super().send_document(user_id, path, caption)
        self._write({"type": "document", "user_id": user_id, "path": path, "caption": caption})
