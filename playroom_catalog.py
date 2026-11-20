"""Local catalog and non-destructive migration of the original playroom."""
import json
import re
import shutil
from pathlib import Path

DEFAULT_PLAYROOM_ID = "sandbox"
MARKET_CATEGORIES = {"playroom", "boyfriend", "girlfriend"}


def read_manifest(path: Path) -> dict:
    data = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    data["category"] = data.get("category", "playroom")
    if data["category"] not in MARKET_CATEGORIES:
        raise ValueError("Loại chợ không hợp lệ")
    return data


def initialize_catalog(base_dir: Path) -> Path:
    """Create local runtime once; subsequent starts preserve every player save."""
    rooms_dir = base_dir / 'playrooms'
    room = rooms_dir / DEFAULT_PLAYROOM_ID
    room.mkdir(parents=True, exist_ok=True)
    seed = base_dir / 'starter'
    for name in ('manifest.json', 'story_bible.json'):
        if not (room / name).exists():
            shutil.copy2(seed / name, room / name)
    if not (room / 'room_state.json').exists():
        shutil.copy2(seed / 'state.template.json', room / 'room_state.json')
    return rooms_dir


def room_directory(rooms_dir: Path, room_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", room_id):
        raise ValueError("Mã playroom không hợp lệ")
    path = rooms_dir / room_id
    if not all((path / name).is_file() for name in ("manifest.json", "story_bible.json", "room_state.json")):
        raise FileNotFoundError("Playroom chưa có sẵn")
    return path


def list_playrooms(rooms_dir: Path) -> list:
    rooms = []
    for manifest_path in sorted(rooms_dir.glob("*/manifest.json")):
        try:
            data = read_manifest(manifest_path.parent)
            room_id = manifest_path.parent.name
            room_directory(rooms_dir, room_id)
            state = json.loads((manifest_path.parent / "room_state.json").read_text(encoding="utf-8"))
            companion = data["category"] in {"boyfriend", "girlfriend"}
            bible = json.loads((manifest_path.parent / "story_bible.json").read_text(encoding="utf-8")) if companion else {}
            rooms.append({
                "id": room_id, "title": str(data.get("title", room_id)),
                "category": data["category"],
                "description": str(data.get("description", "")), "genre": data.get("genre", []),
                "rating": str(data.get("rating", "16+")), "thumbnail": str(data.get("thumbnail", "")),
                "has_progress": any(item.get("is_player") for item in state.get("history", []) if isinstance(item, dict)) or bool(state.get("narrative_state", {}).get("scene_pacing", {}).get("turn_number", 0)),
                "play_url": f"/character/{room_id}" if companion else (
                    f"/enter/{room_id}" if (manifest_path.parent/'roleplay_start.json').is_file() else f"/play/{room_id}"),
                "profile": bible.get("profile", {}) if companion else None
            })
        except (ValueError, OSError, TypeError):
            continue
    return rooms
