import os
import sys
import json
import re
import copy
import time
import httpx
import unicodedata
from speaker_identity import resolve_speaker, identity_key
from llm_json_budget import json_output_budget
from difflib import SequenceMatcher
import asyncio
from playroom_catalog import initialize_catalog, list_playrooms, room_directory, read_manifest, DEFAULT_PLAYROOM_ID
from scene_presence import (apply_presence_plan, narrated_presence_updates, presence_name_aliases,
                            close_scene_membership, finalize_presence_events,
                            is_scene_member, repair_manual_membership, set_manual_presence, director_destination_plan,
                            local_speaker_presence_update, narrated_speaker_presence_update)
from story_events import (load_events, event_index, select_event, apply_event_delta,
                          story_guidance, reconciliation_context,
                          eligible_tiered_events, sample_tiered_pool, pick_fallback_suggestions,
                          mark_event_started, requested_event, normalize_tier, TIER_LABELS,
                          ordered_arc_roadmap, advance_active_arc_if_completed)
import canon_runtime
import episodic_memory
import blueprint_runtime
import ensemble_runtime
import family_drama_runtime
import knowledge_scope
import companion_scene
import companion_cast
import roleplay_sessions
import knowledge_codex
import scene_illustrator
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
from typing import List, Optional, Dict, Any, Literal, Tuple
from fastapi import FastAPI, HTTPException, Request
import companion_creator
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from openai import OpenAI, APITimeoutError
import llm_config
from llm_errors import ProviderContentBlocked, response_block_reason
from token_meter import TokenMeter
from prompt_context import dump as compact_json, relevant_items, select_world, compact_history, fit_context, pack_scene_messages, pack_tracker_prompt, recent_turn_history

# Tải biến môi trường từ tệp .env
BASE_DIR = Path(__file__).resolve().parent
TOKEN_METER = TokenMeter(BASE_DIR / "token_usage_session.json", BASE_DIR / "token_usage.jsonl")
load_dotenv(dotenv_path=BASE_DIR / ".env")

app = FastAPI(
    title="Playroom AI Backend Engine",
    description="Backend engine điều phối phòng chơi nhập vai đa tác tử (Playroom AI Sandbox)",
    version="1.0.0"
)

# Cấu hình CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def rollback_failed_turn(request, call_next):
    """Không để một lượt /api/turn lỗi lưu state dở dang trong bộ nhớ."""
    global ROOM_STATE, LONG_TERM_MEMORY
    if request.method == "POST" and request.url.path == "/api/turn":
        room_snapshot = copy.deepcopy(ROOM_STATE)
        memory_snapshot = copy.deepcopy(LONG_TERM_MEMORY)
        try:
            return await call_next(request)
        except Exception:
            ROOM_STATE = room_snapshot
            LONG_TERM_MEMORY = memory_snapshot
            raise
    return await call_next(request)

# Đường dẫn tệp trạng thái và thư mục tĩnh
PLAYROOMS_DIR = initialize_catalog(BASE_DIR)
ACTIVE_PLAYROOM_ID = DEFAULT_PLAYROOM_ID
ACTIVE_PLAYROOM_DIR = room_directory(PLAYROOMS_DIR, ACTIVE_PLAYROOM_ID)
ROOM_REQUEST_LOCK = asyncio.Lock()
STATE_FILE_PATH = ACTIVE_PLAYROOM_DIR / "room_state.json"
STORY_BIBLE_PATH = ACTIVE_PLAYROOM_DIR / "story_bible.json"
AVATAR_DIR = BASE_DIR / "avatar"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
MEMORY_DIR = ACTIVE_PLAYROOM_DIR / "memory"
LONG_TERM_MEMORY_PATH = MEMORY_DIR / "long_term_memory.json"
HISTORY_ARCHIVE_DIR = MEMORY_DIR / "archives"
ACTIVE_HISTORY_LIMIT = 48
RECENT_HISTORY_TO_KEEP = 18
CHECKPOINT_MAX_OUTPUT_TOKENS = 2048
SUGGESTION_REMINDER_COOLDOWN_TURNS = 10
NARRATION_TOKEN_LIMIT = 450
DIALOGUE_TOKEN_LIMIT = 220
# Mục tiêu mở rộng giúp văn phong sinh động, giàu hình ảnh kì bí và chiều sâu kì ảo.
NARRATION_TARGET_TOKENS = 280
DIALOGUE_TARGET_TOKENS = 140
# Gemini 3 counts internal thinking against this ceiling. The prompt's visible
# token limits bound the prose; the API ceiling leaves generous room for
# thinking plus rich scene JSON.
RESPONSE_MAX_OUTPUT_TOKENS = 4096
RESPONSE_RETRY_OUTPUT_TOKENS = 10240
ROLEPLAY_REASONING_EFFORT = "low"

# Mount thư mục avatar nếu tồn tại
if AVATAR_DIR.exists():
    app.mount("/avatar", StaticFiles(directory=str(AVATAR_DIR)), name="avatar")

@app.get("/favicon.ico", include_in_schema=False)
def site_favicon():
    """Phục vụ logo làm biểu tượng tab/taskbar trình duyệt."""
    logo_path = BASE_DIR / "static" / "logo.png"
    if logo_path.is_file():
        return FileResponse(logo_path, media_type="image/png")
    raise HTTPException(status_code=404, detail="Favicon not found")

def get_gemini_api_key() -> str:
    """Compatibility name for the provider-scoped credential accessor."""
    model = globals().get('MODEL_ROLEPLAY')
    return llm_config.api_key(llm_config.provider_for_model(model) if model else None)

# Cấu hình LLM Client
MODEL_CHOICES = [
    {"id": "gpt-6-luna", "label": "Luna · GPT-6", "tier": 1},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5", "tier": 2},
    {"id": "gpt-5.6-terra", "label": "Terra · GPT-5.6", "tier": 3},
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5", "tier": 4},
    {"id": "gpt-6.1-sol", "label": "Sol 6.1", "tier": 5},
    {"id": "gpt-6.2-sol", "label": "Sol 6.2 · GPT-6.2", "tier": 6},
    {"id": "gpt-6.2-astra", "label": "Astra · GPT-6.2", "tier": 7},
    {"id": "gemini-4-argon", "label": "Argon · Gemini 4", "tier": None, "family": "Gemini"},
    {"id": "grok-4.3", "label": "Grok 4.3", "tier": None, "family": "Grok"},
    {"id": "grok-4.6", "label": "Grok 4.6", "tier": None, "family": "Grok"},
    {"id": "grok-4.7", "label": "Grok 4.7", "tier": None, "family": "Grok"},
    {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro", "tier": None, "family": "DeepSeek"},
    {"id": "deepseek-v4.1-flash", "label": "DeepSeek V4.1 Flash", "tier": None, "family": "DeepSeek"},
]
GEMINI_MODEL_CHOICES = [
    {"id":"gemini-3.1-flash-lite", "label":"Gemini 3.1 Flash-Lite", "tier":1, "family":"Gemini",
     "description":"Tiết kiệm nhất trong danh sách; nên thử với đối thoại đơn giản, kiểm tra kỹ cảnh nhiều nhân vật."},
    {"id":"gemini-3.5-flash-lite", "label":"Gemini 3.5 Flash-Lite", "tier":2, "family":"Gemini",
     "description":"Lựa chọn thử nghiệm cho playroom tiết kiệm; chưa kiểm chứng chất lượng nhập vai trên provider thật."},
    {"id":"gemini-3.8-flash", "label":"Gemini 3.8 Flash", "tier":3, "family":"Gemini",
     "description":"Mặc định; ưu tiên cho cảnh có nhiều quan hệ, ràng buộc và diễn biến phức tạp."},
    {"id":"gemini-4-argon", "label":"Gemini 4 Argon", "tier":4, "family":"Gemini",
     "description":"Lựa chọn reasoning cao hơn; không tự chuyển sang khi model khác lỗi."},
]
MODEL_SETTINGS_PATH = BASE_DIR / "llm_settings.json"
APIZ_GEMINI_MODEL_CHOICES = [
    {"id": "apiz:gemini-3.8-flash", "label": "Gemini 3.8 Flash · APIZ", "family": "Gemini", "tier": None,
     "description": "Qua APIZ, dùng APIZ_API_KEY; reasoning Medium, không tự chuyển sang Google."},
    {"id": "apiz:gemini-3.1-pro", "label": "Gemini 3.1 Pro (Chính thức) · APIZ", "family": "Gemini", "tier": None,
     "description": "Qua APIZ, mã gemini-3.1-pro chính thức; reasoning High, không tự chuyển sang Google."},
    {"id": "apiz:gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview · APIZ", "family": "Gemini", "tier": None,
     "description": "Qua APIZ, mã gemini-3.1-pro-preview; reasoning High, không tự chuyển sang Google."},
    {"id": "apiz:gemini-4-argon", "label": "Gemini 4 Argon · APIZ", "family": "Gemini", "tier": None,
     "description": "Qua APIZ, mã gemini-4-argon; reasoning Low, tối ưu chi phí & tốc độ."},
    {"id": "apiz:gemini-4-hydrogen", "label": "Gemini 4 Hydrogen · APIZ", "family": "Gemini", "tier": None,
     "description": "Qua APIZ, mã gemini-4-hydrogen; reasoning Medium, phản hồi nhanh."},
    {"id": "apiz:gemini-4-carbon", "label": "Gemini 4 Carbon · APIZ", "family": "Gemini", "tier": None,
     "description": "Qua APIZ, mã gemini-4-carbon; reasoning High, suy luận sâu cao cấp."},
]


def provider_model_choices(provider=None):
    # No provider filter in the UI. Explicit filters are only for diagnostics.
    choices = [dict(item, provider='gemini', group_label='Gemini · Google AI Studio')
               for item in GEMINI_MODEL_CHOICES]
    choices += [dict(item, provider='apiz', group_label='Gemini · APIZ',
                     api_model=llm_config.upstream_model(item['id']))
                for item in APIZ_GEMINI_MODEL_CHOICES]
    choices += [dict(item, provider='apiz', group_label=
                    f"{item.get('family', 'GPT / Claude')} · APIZ")
                for item in MODEL_CHOICES if item['id'] not in llm_config.GEMINI_REASONING_EFFORTS]
    return [item for item in choices if provider is None or item['provider'] == provider]


def read_saved_model(path, provider=None):
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        model = saved.get("model") if isinstance(saved, dict) else None
        saved_provider = saved.get('provider') if isinstance(saved,dict) else None
        if model not in {item['id'] for item in provider_model_choices(provider)}:
            return None
        route = llm_config.provider_for_model(model)
        # Preserve verified provider identity, including old settings files.
        # An old gateway Gemini selection must not silently become Google usage.
        if saved_provider == route or (route == 'apiz' and saved_provider == 'shopaikey') or (
                saved_provider is None and route == 'apiz' and llm_config.provider_name() == 'apiz'):
            return model
    except (OSError, ValueError):
        pass
    return None


_available_models = {item['id'] for item in provider_model_choices()}
_default_model = 'gemini-3.8-flash' if llm_config.provider_name()=='gemini' else 'gpt-6-luna'
MODEL_ARBITER = os.getenv('MODEL_ARBITER',_default_model).strip()
MODEL_ROLEPLAY = os.getenv('MODEL_ROLEPLAY',_default_model).strip()
if MODEL_ARBITER not in _available_models:MODEL_ARBITER = _default_model
if MODEL_ROLEPLAY not in _available_models:MODEL_ROLEPLAY = _default_model
saved_model = read_saved_model(MODEL_SETTINGS_PATH)
if saved_model:
    MODEL_ARBITER = MODEL_ROLEPLAY = saved_model
ENABLE_OOC_GUARD = os.getenv("ENABLE_OOC_GUARD", "true").strip().lower() not in {"0", "false", "no"}
ENABLE_STATE_TRACKER = os.getenv("ENABLE_STATE_TRACKER", "true").strip().lower() not in {"0", "false", "no"}
SCENE_MODE = os.getenv("PLAYROOM_SCENE_MODE", "true").strip().lower() not in {"0", "false", "no"}
# Byte ceilings are deterministic local guards, not claims of exact provider tokenization.
INPUT_BYTE_LIMITS = {'scene': 100000, 'scene repair': 100000, 'state tracker': 24000,
                     'checkpoint': 30000, 'default': 42000}
IMPROVISATION_CONTRACT = """ỨNG TÁC CÓ NHẤT QUÁN:
Đây là sáng tác tương tác, không phải tra cứu hồ sơ. Chỗ trống chưa được mô tả KHÔNG phải điều cấm.
Chủ động bổ sung chi tiết nhỏ có ích, hợp văn hóa, hoàn cảnh, vai vế và tính cách: gia cảnh thường dân
của NPC phụ, việc học, công việc cũ, sở thích, thói quen, ứng xử, quan sát tại chỗ. NPC biết đời mình;
khi được hỏi, hãy trả lời cụ thể ngay, không xin phép khai rồi lại né, không nói 'chưa thiết lập',
'chưa có căn cứ', 'không dám bịa' chỉ vì hồ sơ chưa viết. Nếu đã né ở lượt trước, đi thẳng vào câu trả lời.
Không đổi dữ kiện đã chốt, gia phả chính, sự kiện lớn, kết quả giải đấu, giới hạn năng lực, bí mật hoặc
quyết định của người khác/Player. Quá khứ ĐÃ XÁC LẬP là cố định; phần tiểu sử đời thường còn trống được bổ sung.
Với người khác: phân biệt điều vừa quan sát, lời kể và suy đoán; không gán tội hay động cơ bí mật như sự thật.
Chi tiết cá nhân mới phải nhất quán với personal_lore và ký ức về sau. Lời tự thuật chưa là chứng cứ khách quan.
Thể hiện nét riêng bằng một lựa chọn/cử chỉ/lời đáp có ý nghĩa, không lặp cúi đầu–giữ lễ–chờ lệnh.
Khi đang đối thoại, thường 2–4 câu trọn ý hoặc vài nhịp dẫn–thoại; giới hạn độ dài là trần, không phải
lệnh trả lời tối thiểu. Khi người chơi kết thúc cảnh thì đáp gọn, không kéo họ lại bằng nhiệm vụ mới.
QUYỀN CHỦ ĐỘNG CỦA NHÂN VẬT:
NPC có nhu cầu, lòng tự trọng, ham muốn, sở thích và dự định riêng, không phải giao diện chờ lệnh.
Được tự mở chuyện hợp cảnh, hỏi ngược, trêu, bộc lộ cảm xúc, bất đồng, thương lượng, từ chối hoặc đặt
ranh giới đúng tính cách; không phải luôn chiều Player hay luôn phản bác. Được hiểu nhầm hợp lý nhưng
cũng biết nhận ra và sửa hiểu nhầm. Người trầm tính vẫn có ý riêng, không đồng nghĩa im lặng vô vị.
Được làm việc nhỏ trong quyền hạn mà không xin phép từng bước: thư đồng chuẩn bị bút sách, người
thân chủ động quan tâm, đồng bạn góp ý hoặc nối lời nhau. Có thể tạo cơ hội gần gũi hay căng thẳng nhỏ,
không tự quyết cảm xúc/đồng thuận của Player và không biến mọi cuộc nói chuyện thành nhiệm vụ.
Gia nhân được chủ động góp thông tin hữu ích, hỏi để làm đúng việc, giữ ranh giới cá nhân hoặc báo
nguy cơ cụ thể; lễ phép thể hiện qua thời điểm và cách nói, không phải luật cấm mở miệng tuyệt đối.
Không buộc mọi lượt phải có sáng kiến: nghe, lặng im hoặc rời đi cũng có thể là lựa chọn có ý nghĩa.
Không tự hoàn thành việc lớn, ép chuyển cảnh, bỏ qua thời gian dài hoặc làm sống lại xung đột đã khép.
"""

# One facade, separate SDK connections and credentials per destination.
client = llm_config.ModelRouter(OpenAI)

# Khởi tạo trạng thái phòng mặc định
DEFAULT_ROOM_STATE: Dict[str, Any] = {
    "room_id": "the_gioi_co_dai",
    "setting": "",
    "player": {
        "name": "",
        "role": "",
        "avatar": ""
    },
    "roster": {},
    "narrative_state": {},
    "history": []
}

def load_story_bible() -> Dict[str, Any]:
    """Tải canon bất biến tách khỏi trạng thái phiên chơi."""
    try:
        with open(STORY_BIBLE_PATH, "r", encoding="utf-8") as f:
            bible = json.load(f)
        policy_path = STORY_BIBLE_PATH.parent / "interaction_policy.json"
        if policy_path.exists():
            try:
                policy = json.loads(policy_path.read_text(encoding="utf-8"))
                if isinstance(policy,dict):
                    bible["interaction_policy"] = policy
            except (OSError,ValueError):
                print("[Warning] Không đọc được interaction_policy của phòng")
        if canon_runtime.enabled(bible):
            bible['runtime_rules'] = canon_runtime.load_asset(STORY_BIBLE_PATH.parent / 'runtime_rules.json')
            bible['celestial_registry'] = canon_runtime.load_asset(STORY_BIBLE_PATH.parent / 'celestial_key_registry.json')
        try:
            return family_drama_runtime.load(STORY_BIBLE_PATH.parent, bible)
        except (OSError, ValueError, KeyError, TypeError) as error:
            # A missing optional direction file must not erase a room's canon.
            print(f'[Warning] Không tải được chỉ dẫn diễn xuất: {type(error).__name__}; giữ canon gốc')
            bible.pop('performance_profile', None)
            return bible
    except Exception as e:
        print(f"[Error] Lỗi khi đọc {STORY_BIBLE_PATH}: {e}")
        return {"strict_rules": [], "characters": {}, "arcs": {}, "world": {}}

def load_room_state() -> Dict[str, Any]:
    """Tải trạng thái phòng từ tệp room_state.json nếu tồn tại."""
    if STATE_FILE_PATH.exists():
        try:
            with open(STATE_FILE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data
        except Exception as e:
            print(f"[Error] Lỗi khi đọc {STATE_FILE_PATH}: {e}")
    return DEFAULT_ROOM_STATE.copy()

def save_room_state(state: Dict[str, Any]):
    """Ghi trạng thái phòng xuống tệp room_state.json."""
    roleplay_sessions.enforce(state)
    try:
        temp_path = STATE_FILE_PATH.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        temp_path.replace(STATE_FILE_PATH)
    except Exception as e:
        print(f"[Error] Lỗi khi ghi {STATE_FILE_PATH}: {e}")

def load_long_term_memory() -> Dict[str, Any]:
    """Đọc bản tóm tắt bền vững của các phiên chơi trước."""
    try:
        if LONG_TERM_MEMORY_PATH.exists():
            with open(LONG_TERM_MEMORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        print(f"[Warning] Không thể đọc bộ nhớ dài hạn: {e}")
    return {
        "schema_version": 1,
        "story_id": "",
        "saved_at": None,
        "session_count": 0,
        "summary": "Chưa có ký ức từ phiên trước.",
        "facts": [],
        "milestones": [],
        "relationships": {},
        "unresolved_threads": [],
        "recent_events": []
    }

ROOM_STATE = load_room_state()
STORY_BIBLE = load_story_bible()
LONG_TERM_MEMORY = load_long_term_memory()
SESSION_STARTED_AT = datetime.now(timezone.utc).isoformat()

@app.middleware("http")
async def select_playroom_context(request, call_next):
    """Bind stateful APIs to a room; serialize the legacy engine globals."""
    global ACTIVE_PLAYROOM_ID, ACTIVE_PLAYROOM_DIR, STATE_FILE_PATH, STORY_BIBLE_PATH
    global MEMORY_DIR, LONG_TERM_MEMORY_PATH, HISTORY_ARCHIVE_DIR
    global ROOM_STATE, STORY_BIBLE, LONG_TERM_MEMORY
    if not request.url.path.startswith("/api/") or request.url.path.startswith("/api/illustrations/") or request.url.path in {"/api/playrooms", "/api/markets", "/api/companions", "/api/token-usage"}:
        response = await call_next(request)
        if request.url.path.startswith("/avatar/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response
    room_id = request.headers.get("X-Playroom-ID", DEFAULT_PLAYROOM_ID)
    try:
        selected_dir = room_directory(PLAYROOMS_DIR, room_id)
    except (ValueError, FileNotFoundError) as error:
        return JSONResponse(status_code=404, content={"detail": str(error)})
    async with ROOM_REQUEST_LOCK:
        try:
            save_dir = roleplay_sessions.directory(selected_dir, request.headers.get('X-Playroom-Session', 'legacy'))
        except (ValueError, FileNotFoundError) as error:
            return JSONResponse(status_code=404, content={'detail':str(error)})
        ACTIVE_PLAYROOM_ID, ACTIVE_PLAYROOM_DIR = room_id, selected_dir
        STATE_FILE_PATH = save_dir / "room_state.json"
        STORY_BIBLE_PATH = selected_dir / "story_bible.json"
        MEMORY_DIR = save_dir / "memory"
        LONG_TERM_MEMORY_PATH = MEMORY_DIR / "long_term_memory.json"
        HISTORY_ARCHIVE_DIR = MEMORY_DIR / "archives"
        ROOM_STATE, STORY_BIBLE, LONG_TERM_MEMORY = load_room_state(), load_story_bible(), load_long_term_memory()
        roleplay_sessions.enforce(ROOM_STATE)
        STORY_BIBLE = roleplay_sessions.overlay(STORY_BIBLE, ROOM_STATE)
        response = await call_next(request)
        response.headers["X-Playroom-ID"] = room_id
        response.headers['X-Playroom-Session'] = request.headers.get('X-Playroom-Session', 'legacy')
        return response

@app.get("/api/playrooms")
def get_playroom_catalog():
    return {"playrooms": list_playrooms(PLAYROOMS_DIR)}


class RoleplaySessionRequest(BaseModel):
    mode: Literal['original', 'canon']
    name: str = ''
    persona: str = ''
    character_id: str = ''
    gender: Literal['male','female','other','unspecified'] = 'unspecified'


@app.get('/api/play-sessions')
def get_play_sessions():
    cfg = roleplay_sessions.config(ACTIVE_PLAYROOM_DIR)
    catalog = load_character_library() or STORY_BIBLE.get('characters', {})
    catalog = {key:value for key,value in catalog.items() if value.get('variant') != 'platinum_twin' and not key.startswith('silver_')}
    return {'enabled':bool(cfg), 'sessions':roleplay_sessions.sessions(ACTIVE_PLAYROOM_DIR),
            'characters':[{'id':k, **{key:v for key,v in profile.items() if key in
                         {'name','guild','role','personality','magic','limits'}}}
                          for k,profile in catalog.items()] if cfg else []}


@app.post('/api/play-sessions', status_code=201)
def create_play_session(payload: RoleplaySessionRequest):
    try:
        meta = roleplay_sessions.create(ACTIVE_PLAYROOM_DIR, payload.mode,
            load_character_library() or STORY_BIBLE.get('characters', {}), payload.name,
            payload.persona, payload.character_id, payload.gender)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return {**meta, 'play_url':f'/play/{ACTIVE_PLAYROOM_ID}?session={meta["id"]}'}


def guard_player_identity(ident):
    try:
        roleplay_sessions.guard(ROOM_STATE, ident)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error

@app.get("/api/markets")
def get_markets():
    entries = list_playrooms(PLAYROOMS_DIR)
    return {category: [entry for entry in entries if entry["category"] == category]
            for category in ("playroom", "boyfriend", "girlfriend")}


@app.post("/api/companions", status_code=201)
async def create_boyfriend(request: Request):
    """Create an isolated companion pack without binding or changing the active room."""
    return await companion_creator.create_from_request(request, PLAYROOMS_DIR, BASE_DIR)

def _plain_history_text(item: Dict[str, Any]) -> str:
    if blueprint_runtime.enabled(STORY_BIBLE):
        return ' '.join(text for _,text in episodic_memory.public_pieces(item))
    if item.get("intent") == "thought":
        player_name = ROOM_STATE.get("player", {}).get("name") or "Người chơi"
        return f"[{player_name} im lặng và có vẻ đang suy nghĩ; nội dung suy nghĩ không được lưu vào ký ức NPC]"
    if item.get("is_player") and item.get("intent") == "mixed":
        return visible_mixed_turn({"segments": item.get("segments", []), "mention_ids": item.get("mention_ids", [])})
    if item.get("is_player"):
        return str(item.get("text", "")).strip()
    narration = str(item.get("narration", "")).strip()
    dialogue = str(item.get("dialogue", "")).strip()
    if narration or dialogue:
        return " ".join(part for part in [narration, f'“{dialogue}”' if dialogue else ""] if part)
    return str(item.get("text", "")).strip()

def get_checkpoint_context(state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    state = state or get_narrative_state()
    checkpoint = state.get("checkpoint_memory", {})
    if not isinstance(checkpoint, dict):
        return {}
    return {
        "summary": truncate_to_token_budget(checkpoint.get("summary", ""), 380),
        "commitments": checkpoint.get("commitments", [])[-10:],
        "known_outcomes": checkpoint.get("known_outcomes", [])[-12:],
        "open_threads": checkpoint.get("open_threads", [])[-10:],
        "checkpoint_count": checkpoint.get("checkpoint_count", 0)
    }

def _checkpoint_fallback(chunk: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    """Summary không dùng API khi offline; chỉ lấy dữ kiện công khai, không lộ thought."""
    visible = [
        f"{item.get('sender', 'Unknown')}: {_plain_history_text(item)[:300]}"
        for item in chunk[-16:]
    ]
    return {
        "summary": truncate_to_token_budget("Diễn biến đã lưu: " + " ".join(visible), 350),
        "commitments": [],
        "known_outcomes": [],
        "open_threads": [item.get("summary", "") for item in get_conflict_context(state)["active"]],
        "location": state.get("location", ""),
        "active_arc": state.get("active_arc", "")
    }

def build_checkpoint_summary(chunk: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    """Nén transcript cũ thành ký ức ngắn, có cấu trúc và không lộ độc thoại Player."""
    # Summarize unusually large archives in bounded chronological batches. Preserve
    # the accumulated checkpoint as input to the next batch, never slice transcripts.
    batches, batch, used = [], [], 0
    for item in chunk:
        size = len(compact_json({'sender':item.get('sender', ''), 'text':_plain_history_text(item)}).encode('utf-8'))
        if batch and used + size > 16000:
            batches.append(batch)
            batch, used = [], 0
        batch.append(item)
        used += size
    if batch:
        batches.append(batch)
    if len(batches) > 1:
        working = copy.deepcopy(state)
        for batch in batches:
            summary = build_checkpoint_summary(batch, working)
            working['checkpoint_memory'] = {**working.get('checkpoint_memory', {}), **summary}
        return summary
    public_transcript = [
        {"sender": item.get("sender", "Unknown"), "text": _plain_history_text(item)}
        for item in chunk
    ]
    current_key = get_gemini_api_key()
    if not client or not current_key or current_key == "YOUR_API_KEY":
        return _checkpoint_fallback(chunk, state)
    prompt = f"""
Bạn là Memory Curator cho game nhập vai. Tóm tắt CHÍNH XÁC transcript cũ để giảm token input, không sáng tác.
Không ghi nội dung suy nghĩ riêng của Player; chỉ ghi điều NPC có thể quan sát hoặc sự kiện công khai.

TRẠNG THÁI CẤU TRÚC HIỆN TẠI:
{compact_json({"previous_checkpoint": get_checkpoint_context(state), "conflicts": get_conflict_context(state), "location": state.get("location"), "active_arc": state.get("active_arc"), "established_facts": state.get("facts", [])[-8:]})}

TRANSCRIPT CẦN NÉN:
{compact_json(public_transcript)}

Trả JSON ngắn gọn:
{{
  "summary": "tối đa 350 token, theo thứ tự nhân quả; chỉ điều đã xảy ra",
  "commitments": ["lời hứa/nợ/đề nghị còn hiệu lực, tối đa 8"],
  "known_outcomes": ["kết quả đã chốt, gồm minh oan/khép án nếu có, tối đa 10"],
  "open_threads": ["việc chưa giải quyết, chỉ khi có căn cứ, tối đa 8"],
  "location": "địa điểm cuối transcript nếu rõ",
  "active_arc": "arc phù hợp nếu rõ"
}}
"""
    if blueprint_runtime.enabled(STORY_BIBLE):
        prompt=blueprint_runtime.checkpoint_prompt(BASE_DIR,{
            'previous_checkpoint':get_checkpoint_context(state),'conflicts':get_conflict_context(state),
            'location':state.get('location'),'active_arc':state.get('active_arc'),
            'established_facts':state.get('facts',[])[-8:]},public_transcript)
    try:
        summary = request_llm_json(
            model=MODEL_ARBITER,
            output_budget=CHECKPOINT_MAX_OUTPUT_TOKENS,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Tạo checkpoint JSON từ transcript."}
            ],
            purpose="checkpoint"
        )
        return {
            "summary": truncate_to_token_budget(summary.get("summary", ""), 350),
            "commitments": [str(x) for x in summary.get("commitments", [])[:8] if isinstance(x, str)],
            "known_outcomes": [str(x) for x in summary.get("known_outcomes", [])[:10] if isinstance(x, str)],
            "open_threads": [str(x) for x in summary.get("open_threads", [])[:8] if isinstance(x, str)],
            "location": str(summary.get("location", "")).strip(),
            "active_arc": str(summary.get("active_arc", "")).strip()
        }
    except Exception as e:
        print(f"[Warning] Checkpoint summary fallback do lỗi: {e}")
        return _checkpoint_fallback(chunk, state)

def checkpoint_history_if_needed() -> bool:
    """Archive transcript cũ rồi giữ cửa sổ gần nhất cho state hoạt động."""
    history = ROOM_STATE.get("history", [])
    if len(history) <= ACTIVE_HISTORY_LIMIT:
        return False
    state = get_narrative_state()
    retained = recent_turn_history(history, turns=5, min_entries=RECENT_HISTORY_TO_KEEP)
    chunk = history[:len(history)-len(retained)]
    if not chunk:
        return False
    checkpoint = build_checkpoint_summary(chunk, state)
    if knowledge_scope.enabled(STORY_BIBLE):
        checkpoint['knowledge_scope'] = knowledge_scope.checkpoint_metadata(chunk)
    previous = state.get("checkpoint_memory", {}) if isinstance(state.get("checkpoint_memory"), dict) else {}
    checkpoint_count = int(previous.get("checkpoint_count", 0)) + 1
    checkpoint["checkpoint_count"] = checkpoint_count
    checkpoint["created_at"] = datetime.now(timezone.utc).isoformat()
    checkpoint["archived_entries"] = len(chunk)
    # Summary cũ có thể chứa tình trạng đã khép; facts/milestones giữ ký ức bền vững.
    for field, max_items in (("commitments", 10), ("known_outcomes", 14)):
        checkpoint[field] = list(dict.fromkeys([
            *previous.get(field, []), *checkpoint.get(field, [])
        ]))[-max_items:]
    checkpoint["open_threads"] = list(dict.fromkeys(checkpoint.get("open_threads", [])))[-10:]
    state["checkpoint_memory"] = checkpoint

    HISTORY_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = HISTORY_ARCHIVE_DIR / f"checkpoint-{checkpoint_count:04d}.json"
    with open(archive_path, "w", encoding="utf-8") as f:
        json.dump({"checkpoint": checkpoint, "history": chunk}, f, ensure_ascii=False, indent=2)
    ROOM_STATE["history"] = retained
    return True

def persist_long_term_memory(reason: str = "manual") -> Dict[str, Any]:
    """Tạo snapshot tóm tắt và ghi nguyên tử để dùng lại ở phiên sau."""
    global LONG_TERM_MEMORY
    state = get_narrative_state()
    history = ROOM_STATE.get("history", [])
    recent_events = [
        {"sender": item.get("sender", "Unknown"), "text": _plain_history_text(item)[:600]}
        for item in history[-12:]
    ]
    facts = list(dict.fromkeys([
        *LONG_TERM_MEMORY.get("facts", []),
        *state.get("facts", [])
    ]))[-40:]
    milestones = list(dict.fromkeys([
        *LONG_TERM_MEMORY.get("milestones", []),
        *state.get("milestones", [])
    ]))[-40:]
    arc = state.get("active_arc", "xuan_ve_de_kinh")
    location = state.get("location", "chưa rõ")
    summary_parts = [f"Arc hiện tại: {arc}.", f"Địa điểm: {location}."]
    if facts:
        summary_parts.append("Sự kiện đã xác lập: " + "; ".join(map(str, facts[-8:])) + ".")
    if milestones:
        summary_parts.append("Mốc đã đạt: " + "; ".join(map(str, milestones[-8:])) + ".")
    LONG_TERM_MEMORY = {
        "schema_version": 1,
        "story_id": ROOM_STATE.get("story_id") or STORY_BIBLE.get("story_id", ""),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "session_started_at": SESSION_STARTED_AT,
        "save_reason": reason,
        "session_count": int(LONG_TERM_MEMORY.get("session_count", 0)) + (1 if reason == "shutdown" else 0),
        "summary": " ".join(summary_parts),
        "facts": facts,
        "milestones": milestones,
        "relationships": state.get("relationships", {}),
        "unresolved_threads": state.get("unresolved_threads", []),
        "recent_events": recent_events
    }
    if knowledge_scope.enabled(STORY_BIBLE):
        ledger = state.get('knowledge_ledger', {})
        LONG_TERM_MEMORY['knowledge_scope'] = {
            'version':1, 'event_count':len(ledger.get('events', [])),
            'current_scene_id':ledger.get('current_scene_id'),
            'source':'room_state.narrative_state.knowledge_ledger',
            'summary_is_director_only':True}
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = LONG_TERM_MEMORY_PATH.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(LONG_TERM_MEMORY, f, ensure_ascii=False, indent=2)
    temp_path.replace(LONG_TERM_MEMORY_PATH)
    return LONG_TERM_MEMORY

@app.on_event("shutdown")
def save_memory_on_shutdown() -> None:
    try:
        save_room_state(ROOM_STATE)
        persist_long_term_memory("shutdown")
        print(f"[Memory] Đã lưu bộ nhớ dài hạn: {LONG_TERM_MEMORY_PATH}")
    except Exception as e:
        print(f"[Warning] Không thể lưu bộ nhớ khi dừng server: {e}")

# --- Pydantic Data Contracts ---

class TurnRequest(BaseModel):
    content: str
    intent: str = "mixed"  # UI mới dùng mixed; các intent cũ vẫn tương thích API
    target_id: Optional[str] = None

class SceneUpdateRequest(BaseModel):
    setting: str

class ToggleNPCRequest(BaseModel):
    npc_id: str
    is_active: bool

class CharacterUpsertRequest(BaseModel):
    id: str
    name: str
    role: str
    personality: str
    tone: str
    is_active: bool = True
    avatar: Optional[str] = ""
    relationship: Optional[str] = ""
    can_loveline: Optional[bool] = False
    loveline_hint: Optional[str] = ""

class PlayerUpdateRequest(BaseModel):
    name: str
    role: str
    avatar: Optional[str] = ""
    persona: Optional[str] = None
    gender: Optional[Literal["male", "female", "other", "unspecified"]] = None

class PlayerPersonaRequest(BaseModel):
    persona: str = ""

# --- Helper Logic Functions ---

def get_avatar_url(entity_dict: Optional[dict] = None, entity_id: str = "") -> str:
    """Xác định đường dẫn ảnh đại diện avatar trong thư mục avatar cho NPC hoặc Player."""
    # Room-local art mapping: no rewrite of a live save, and custom choices win.
    try:
        mapping = json.loads((ACTIVE_PLAYROOM_DIR / 'avatar_overrides.json').read_text(encoding='utf-8'))
        entry = mapping.get(entity_id, {})
        current = str((entity_dict or {}).get('avatar') or '').removeprefix('/avatar/')
        image = entry.get('file', '')
        if (entry.get('name') == (entity_dict or {}).get('name')
                and current in entry.get('replace', ['']) + [image]
                and Path(image).name == image and (AVATAR_DIR / image).is_file()):
            return '/avatar/' + image
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    if entity_dict:
        avatar_val = str(entity_dict.get("avatar", "") or "").strip()
        if avatar_val:
            if avatar_val.startswith("http://") or avatar_val.startswith("https://") or avatar_val.startswith("/avatar/") or avatar_val.startswith("/static/"):
                return avatar_val
            return f"/avatar/{avatar_val}"

    # Tự động khớp theo tên hoặc ID không dấu với file trong avatar/
    check_names = [entity_id]
    if entity_dict and entity_dict.get("name"):
        check_names.append(entity_dict["name"])
    elif entity_id == "__narrator__":
        check_names.append("Người kể chuyện")

    if AVATAR_DIR.exists():
        import unicodedata, re
        def to_slug(text: str) -> str:
            text = unicodedata.normalize('NFKD', text).encode('ASCII', 'ignore').decode('utf-8')
            text = re.sub(r'[^\w\s-]', '', text).strip().lower()
            return re.sub(r'[-\s]+', '_', text)

        for name in check_names:
            if not name:
                continue
            for ext in [".png", ".jpg", ".jpeg", ".webp"]:
                exact_target = AVATAR_DIR / f"{name}{ext}"
                if exact_target.is_file():
                    return f"/avatar/{name}{ext}"
            slug = to_slug(name)
            for ext in [".png", ".jpg", ".jpeg", ".webp"]:
                target = AVATAR_DIR / f"{slug}{ext}"
                if target.exists():
                    return f"/avatar/{slug}{ext}"

    return ""


def get_avatar_versions() -> Dict[str, int]:
    """Trả map {tên_file: mtime_seconds} để frontend gắn query ?v= phá cache trình duyệt khi thay ảnh."""
    if not AVATAR_DIR.exists():
        return {}
    versions: Dict[str, int] = {}
    for entry in AVATAR_DIR.iterdir():
        try:
            if entry.is_file():
                versions[entry.name] = int(entry.stat().st_mtime)
        except OSError:
            continue
    return versions

def get_active_npcs() -> Dict[str, dict]:
    """Lấy danh sách các NPC đang kích hoạt (is_active = True)."""
    roleplay_sessions.enforce(ROOM_STATE)
    if STORY_BIBLE.get("session_mode") == "companion" and not companion_cast.enabled(STORY_BIBLE):
        principal = next(iter(STORY_BIBLE.get("characters", {})), None)
        return {principal: ROOM_STATE["roster"][principal]} if principal in ROOM_STATE.get("roster", {}) else {}
    return {
        k: v for k, v in ROOM_STATE.get("roster", {}).items()
        if isinstance(v, dict) and v.get("is_active", False)
    }

def get_canon_character(npc_id: str) -> Dict[str, Any]:
    """Lấy hồ sơ canon; UI chỉ được sửa phần trình bày, không ghi đè canon."""
    npc = ROOM_STATE.get("roster", {}).get(npc_id, {})
    canon_id = npc.get("canon_id") or npc_id
    canon = STORY_BIBLE.get("characters", {}).get(canon_id)
    if canon is not None:
        return canon
    return load_character_library().get(canon_id, {})

def load_character_library() -> Dict[str, Dict[str, Any]]:
    """Read a room's opt-in canon library without putting the whole cast in prompts."""
    path = ACTIVE_PLAYROOM_DIR / "character_catalog.json"
    if not path.is_file():
        return {}
    try:
        data = canon_runtime.load_asset(path)
        if ROOM_STATE.get('player', {}).get('identity_mode') in {'canon','original'}:
            data = {key:value for key,value in data.items() if value.get('variant') != 'platinum_twin' and not key.startswith('silver_')}
        if ROOM_STATE.get('player', {}).get('identity_mode') == 'canon':
            for profile in data.values():
                profile['relationship'] = 'Nhận Player theo danh tính canon và quan hệ trước Sky Labyrinth; không mặc định người lạ, không biết biến cố tương lai.'
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as error:
        print(f"[Warning] Không đọc được character library: {error}")
        return {}

def library_roster_entry(npc_id: str, canon: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": npc_id, "canon_id": npc_id, "name": canon["name"],
        "role": canon.get("role", canon.get("guild", "Pháp sư")),
        "personality": canon.get("personality", ""),
        "tone": canon.get("voice", ""),
        "relationship": canon.get("relationship", "Chưa quen Player"),
        "can_loveline": bool(canon.get("can_loveline", False)),
        "loveline_hint": "Chỉ phát triển từ tương tác thực tế và sự đồng thuận.",
        "is_active": False, "avatar": get_avatar_url({"name": canon["name"]}, npc_id)
    }

def get_relationship_profile(npc_id: str, mode: Optional[str] = None) -> Dict[str, Any]:
    """Lấy bộ chỉ số riêng theo nhân vật và loại quan hệ hiện tại."""
    profiles = STORY_BIBLE.get("relationship_profiles", {})
    character_profile = profiles.get(npc_id) or profiles.get("temporary_default", {})
    selected_mode = mode or character_profile.get("default_mode", "social")
    profile = character_profile.get(selected_mode)
    if not isinstance(profile, dict):
        selected_mode = character_profile.get("default_mode", "social")
        profile = character_profile.get(selected_mode, {})
    return {"mode": selected_mode, **profile}

def relationship_title(npc_id: str, profile: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Tính danh phận hiện tại và mốc kế tiếp từ chỉ số trung tâm."""
    title_metric = str(profile.get("title_metric", ""))
    score = clamp_int(metrics.get(title_metric, 0), 0, 100)
    current_title = "Chưa định danh"
    next_title = None
    next_at = None
    for milestone in profile.get("titles", []):
        if not isinstance(milestone, list) or len(milestone) != 2:
            continue
        threshold = clamp_int(milestone[0], 0, 100)
        label = str(milestone[1]).strip()
        if score >= threshold:
            current_title = label or current_title
        elif next_title is None:
            next_title, next_at = label, threshold
    return {
        "title": current_title,
        "title_description": STORY_BIBLE.get("relationship_title_meanings", {}).get(npc_id, {}).get(current_title, "Mối quan hệ đã bước sang một trạng thái mới, nhưng ý nghĩa cụ thể vẫn tùy cách hai người tiếp tục đối đáp."),
        "title_metric": title_metric,
        "title_score": score,
        "next_title": next_title,
        "next_title_at": next_at
    }

def normalize_relationship_entry(npc_id: str, entry: Any) -> Dict[str, Any]:
    """Migration tương thích: chuyển điểm cũ sang bộ chỉ số động mà không mất state."""
    entry = entry if isinstance(entry, dict) else {}
    mode = str(entry.get("mode", "")).strip() or get_relationship_profile(npc_id).get("mode", "social")
    profile = get_relationship_profile(npc_id, mode)
    mode = profile.get("mode", mode)
    metric_labels = profile.get("metrics", {}) if isinstance(profile.get("metrics"), dict) else {}
    metrics = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else {}

    # Dữ liệu cũ dùng thang 0-100 nhưng các state đầu tiên thường chỉ có 0-5.
    legacy_affection = clamp_int(entry.get("affection", 0), 0, 100)
    legacy_trust = clamp_int(entry.get("trust", 0), 0, 100)
    legacy_tension = clamp_int(entry.get("tension", 0), 0, 100)
    if legacy_affection <= 10:
        legacy_affection *= 5
    if legacy_trust <= 10:
        legacy_trust *= 5
    if legacy_tension <= 10:
        legacy_tension *= 5
    primary = str(profile.get("title_metric", ""))
    for key in metric_labels:
        if key not in metrics:
            if key == primary:
                metrics[key] = legacy_affection
            elif key in {"tin_cay", "tin_tuong", "ton_trong", "an_tam"}:
                metrics[key] = legacy_trust
            elif key in {"kiem_soat", "chiem_huu", "de_phong", "doi_dau"}:
                metrics[key] = legacy_tension
            else:
                metrics[key] = 0
        metrics[key] = clamp_int(metrics[key], 0, 100)

    title_state = relationship_title(npc_id, profile, metrics)
    return {
        **{k: v for k, v in entry.items() if k not in {"affection", "trust", "tension", "debt", "stage"}},
        "mode": mode,
        "mode_label": {"potential_romance": "Loveline tiềm năng", "friendship": "Friendzone / tình bạn", "rivalry": "Đối thủ", "social": "Quan hệ xã hội"}.get(mode, mode),
        "metrics": metrics,
        "metric_labels": metric_labels,
        "title": title_state["title"],
        "title_description": title_state["title_description"],
        "title_metric": title_state["title_metric"],
        "title_score": title_state["title_score"],
        "next_title": title_state["next_title"],
        "next_title_at": title_state["next_title_at"],
        "boundary": entry.get("boundary", "")
    }

def get_narrative_state() -> Dict[str, Any]:
    state = ROOM_STATE.setdefault("narrative_state", {})
    ensure_narrative_mechanics(state)
    return state

def ensure_narrative_mechanics(state: Dict[str, Any]) -> None:
    """Migration nhẹ: bổ sung cơ chế trạng thái, không tự sửa nội dung truyện cũ."""
    owned_ids = STORY_BIBLE.get("player_owned_character_ids", [])
    if isinstance(owned_ids, list) and owned_ids:
        library = load_character_library()
        roster = ROOM_STATE.setdefault("roster", {})
        for npc_id in owned_ids:
            if isinstance(npc_id, str) and npc_id in library and npc_id not in roster:
                roster[npc_id] = library_roster_entry(npc_id, library[npc_id])
    if not isinstance(state.get("conflicts"), dict):
        state["conflicts"] = {}
    pacing = state.setdefault("scene_pacing", {})
    pacing.setdefault("turn_number", 0)
    pacing.setdefault("turns_without_progress", 0)
    pacing.setdefault("last_progress_turn", 0)
    pacing.setdefault("recent_hooks", [])
    checkpoint = state.setdefault("checkpoint_memory", {})
    checkpoint.setdefault("checkpoint_count", 0)
    checkpoint.setdefault("summary", "")
    checkpoint.setdefault("commitments", [])
    checkpoint.setdefault("known_outcomes", [])
    checkpoint.setdefault("open_threads", [])
    normalize_suggestion_ledger(state)
    state.setdefault("suggestion_next_id", 1)
    relationships = state.setdefault("relationships", {})
    for npc_id in ROOM_STATE.get("roster", {}):
        relationships[npc_id] = normalize_relationship_entry(npc_id, relationships.get(npc_id, {}))

def normalize_suggestion_ledger(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Migrate ledger cũ (`items` + metadata) sang map chỉ chứa suggestion record."""
    raw = state.get("suggestion_ledger", {})
    if not isinstance(raw, dict):
        raw = {}
    nested_items = raw.get("items", {}) if isinstance(raw.get("items"), dict) else {}
    cooldown = raw.get("cooldown_turns", state.get("suggestion_cooldown_turns", SUGGESTION_REMINDER_COOLDOWN_TURNS))
    state["suggestion_cooldown_turns"] = clamp_int(cooldown, 1, 100)
    clean: Dict[str, Dict[str, Any]] = {}
    for source in (nested_items, raw):
        for suggestion_id, suggestion in source.items():
            if not isinstance(suggestion, dict):
                continue
            if not str(suggestion_id).startswith("s"):
                continue
            clean[str(suggestion_id)] = suggestion
    state["suggestion_ledger"] = clean
    return clean

def get_suggestion_context(state: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Các gợi ý gần đây mà NPC phải nhớ để không lặp như một kịch bản cố định."""
    state = state or get_narrative_state()
    result = []
    current_turn = state.get("scene_pacing", {}).get("turn_number", 0)
    for suggestion_id, suggestion in normalize_suggestion_ledger(state).items():
        if not isinstance(suggestion, dict):
            continue
        age = current_turn - int(suggestion.get("created_turn", current_turn))
        if age > SUGGESTION_REMINDER_COOLDOWN_TURNS + 8:
            continue
        result.append({
            "id": suggestion_id,
            "speaker_id": suggestion.get("speaker_id", ""),
            "text": suggestion.get("text", ""),
            "status": suggestion.get("status", "pending"),
            "age_turns": age,
            "cooldown_remaining": max(0, SUGGESTION_REMINDER_COOLDOWN_TURNS - int(suggestion.get("player_turns_since_offer", 0)))
        })
    return result[-16:]

def mark_suggestions_seen_by_player(state: Dict[str, Any]) -> None:
    """Player đã có lượt đáp lại: không được lặp gợi ý ngay cả khi chưa phân loại đồng ý/từ chối."""
    for suggestion in normalize_suggestion_ledger(state).values():
        if not isinstance(suggestion, dict) or suggestion.get("status") not in {"pending", "deferred"}:
            continue
        suggestion["player_turns_since_offer"] = int(suggestion.get("player_turns_since_offer", 0)) + 1
        if suggestion["player_turns_since_offer"] >= 1:
            suggestion["status"] = "deferred"

def register_npc_suggestions(state: Dict[str, Any], npc_id: str, suggestions: Any) -> None:
    """Chỉ lưu đề nghị trực tiếp, không lưu mọi câu hỏi hay lời dẫn như một gợi ý."""
    if not isinstance(suggestions, list):
        return
    ledger = normalize_suggestion_ledger(state)
    turn = state.get("scene_pacing", {}).get("turn_number", 0)
    for text in suggestions[:2]:
        text = str(text).strip()
        if not text or len(text) > 360:
            continue
        normalized = re.sub(r"\W+", "", text.lower())
        if any(re.sub(r"\W+", "", str(item.get("text", "")).lower()) == normalized for item in ledger.values() if isinstance(item, dict)):
            continue
        suggestion_id = f"s{int(state.get('suggestion_next_id', 1)):04d}"
        state["suggestion_next_id"] = int(state.get("suggestion_next_id", 1)) + 1
        ledger[suggestion_id] = {
            "speaker_id": npc_id,
            "text": text,
            "status": "pending",
            "created_turn": turn,
            "player_turns_since_offer": 0
        }
    # Chỉ giữ ledger gần đây; summary/checkpoint giữ các cam kết thật sự quan trọng.
    if len(ledger) > 40:
        oldest = sorted(
            (key for key, value in ledger.items() if isinstance(value, dict)),
            key=lambda key: int(ledger[key].get("created_turn", 0))
        )[:-40]
        for key in oldest:
            ledger.pop(key, None)

def apply_suggestion_updates(state: Dict[str, Any], updates: Any) -> None:
    if not isinstance(updates, list):
        return
    allowed = {"accepted", "declined", "deferred", "expired"}
    ledger = normalize_suggestion_ledger(state)
    for update in updates[:4]:
        if not isinstance(update, dict):
            continue
        suggestion_id = str(update.get("id", ""))
        status = str(update.get("status", "")).lower()
        if suggestion_id in ledger and isinstance(ledger[suggestion_id], dict) and status in allowed:
            ledger[suggestion_id]["status"] = status
            ledger[suggestion_id]["status_reason"] = str(update.get("reason", ""))[:240]

def get_conflict_context(state: Optional[Dict[str, Any]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Chỉ đưa các xung đột có trạng thái rõ vào prompt để tránh tái diễn mơ hồ."""
    state = state or get_narrative_state()
    active, closed = [], []
    for conflict_id, conflict in state.get("conflicts", {}).items():
        if not isinstance(conflict, dict):
            continue
        safe = {
            "id": conflict_id,
            "status": conflict.get("status", "active"),
            "summary": conflict.get("summary", ""),
            "target": conflict.get("target", ""),
            "evidence": conflict.get("evidence", ""),
            "change_reason": conflict.get("change_reason", "")
        }
        if safe["status"] == "active":
            active.append(safe)
        elif safe["status"] in {"resolved", "superseded", "dormant"}:
            closed.append(safe)
    return {"active": active[-8:], "closed": closed[-12:]}

def apply_conflict_updates(state: Dict[str, Any], updates: Any) -> bool:
    """Áp dụng chuyển pha xung đột; trạng thái đã đóng không được tự mở lại."""
    if not isinstance(updates, list):
        return False
    changed = False
    conflicts = state.setdefault("conflicts", {})
    allowed_statuses = {"active", "resolved", "superseded", "dormant"}
    for update in updates[:2]:
        if not isinstance(update, dict):
            continue
        conflict_id = str(update.get("id", "")).strip().lower()
        status = str(update.get("status", "")).strip().lower()
        summary = str(update.get("summary", "")).strip()
        reason = str(update.get("change_reason", "")).strip()
        if not re.fullmatch(r"[a-z0-9_]{3,48}", conflict_id) or status not in allowed_statuses:
            continue
        existing = conflicts.get(conflict_id)
        if existing and existing.get("status") in {"resolved", "superseded"} and status == "active":
            # Muốn mở lại cần một vụ việc mới có ID khác; tránh lặp lại cùng cáo buộc.
            continue
        if not existing and status != "active":
            continue
        if not existing and (not summary or not reason):
            continue
        record = dict(existing or {})
        record.update({
            "status": status,
            "summary": summary or record.get("summary", ""),
            "target": str(update.get("target", "")).strip() or record.get("target", ""),
            "evidence": str(update.get("evidence", "")).strip() or record.get("evidence", ""),
            "change_reason": reason or record.get("change_reason", ""),
            "last_updated_turn": state["scene_pacing"].get("turn_number", 0)
        })
        conflicts[conflict_id] = record
        changed = True
    return changed

def recalculate_arc_unlocks() -> None:
    """Mở tuyến theo độ chín quan hệ, không biến arc thành đường ray bắt buộc."""
    state = get_narrative_state()
    if STORY_BIBLE.get("session_mode") == "companion":
        state["active_arc"] = ""
        state["unlocked_arcs"] = []
        return
    if STORY_BIBLE.get("arc_policy", {}).get("mode") == "open_suggestions":
        # Availability is not completion, and switching threads has no quest gate.
        state["unlocked_arcs"] = sorted(STORY_BIBLE.get("arcs", {}))
        return
    completed = set(state.get("completed_arcs", []))
    resolved = set(state.get("resolved_relationships", []))
    milestones = set(state.get("milestones", []))

    # Các tuyến gặp gỡ luôn sẵn sàng để Player tự do chọn, bỏ qua hoặc đan xen.
    arcs = STORY_BIBLE.get("arcs", {})
    unlocked = {arc_id for arc_id, arc in arcs.items()
                if isinstance(arc, dict) and arc.get("kind") not in {"ensemble", "finale"}}

    # Arc quần thể chỉ mở sau khi đã có trải nghiệm quan hệ đủ để tạo ý nghĩa.
    relationship_progress = len(completed.intersection(unlocked - {"xuan_ve_de_kinh"}))
    if relationship_progress >= 2 or "xuan_yen_ready" in milestones:
        unlocked.update(arc_id for arc_id, arc in arcs.items() if isinstance(arc, dict) and arc.get("kind") == "ensemble")

    # Finale mở bằng độ chín chứ không bằng việc thu thập đủ manh mối chính trị.
    if relationship_progress >= 3 or resolved or "ready_to_choose" in milestones:
        unlocked.update(arc_id for arc_id, arc in arcs.items() if isinstance(arc, dict) and arc.get("kind") == "finale")

    state["unlocked_arcs"] = sorted(unlocked)

def apply_temporary_npc_lifecycle(state: Dict[str, Any], updates: Any) -> bool:
    """Thêm/rút NPC phụ có danh tính mà không làm roster phình vô hạn."""
    if STORY_BIBLE.get("session_mode") == "companion" or not isinstance(updates, list):
        return False
    if STORY_BIBLE.get("canon_licensed") and not canon_runtime.enabled(STORY_BIBLE):
        return False
    roster = ROOM_STATE.setdefault("roster", {})
    knowledge = state.setdefault("knowledge", {})
    relationships = state.setdefault("relationships", {})
    turn_number = state.get("scene_pacing", {}).get("turn_number", 0)
    changed = False

    for update in updates[:16]:
        if not isinstance(update, dict):
            continue
        action = str(update.get("action", "")).strip().lower()
        npc_id = str(update.get("id", "")).strip().lower()
        if not npc_id or npc_id in STORY_BIBLE.get("characters", {}) or (
                canon_runtime.enabled(STORY_BIBLE) and npc_id in load_character_library()):
            continue

        if action == "add":
            if npc_id in roster:
                # Adding an existing ID must not overwrite a main character or reset
                # an established temporary character's personality/knowledge.
                continue
            name = str(update.get("name", "")).strip()
            role = str(update.get("role", "")).strip()
            personality = str(update.get("personality", "")).strip()
            tone = str(update.get("tone", "")).strip()
            scene_function = str(update.get("scene_function", "")).strip()
            # Cấm NPC vô danh hoặc hồ sơ rỗng kiểu "một người hầu".
            if not all((name, role, personality, tone, scene_function)) or len(name) > 40:
                continue
            existing = roster.get(npc_id, {})
            roster[npc_id] = {
                "id": npc_id,
                "canon_id": "",
                "name": name,
                "role": role,
                "personality": personality,
                "tone": tone,
                "relationship": str(update.get("relationship", "Nhân vật phụ mới gặp trong cảnh hiện tại")).strip(),
                "can_loveline": False,
                "loveline_hint": "",
                "is_active": True,
                "avatar": existing.get("avatar", ""),
                "temporary": True,
                "scene_scope": ROOM_STATE.get('setting', ''),
                "speech_protocol": 'deferential' if STORY_BIBLE.get('interaction_policy', {}).get('deferential_ids')
                    and re.search(r'thị nữ|gia nhân|người hầu|thằng hầu|thư đồng|cận vệ', role, re.I) else '',
                "scene_function": scene_function,
                "introduced_turn": existing.get("introduced_turn", turn_number),
                "last_seen_turn": turn_number,
                "retire_after_turns": 8
            }
            knowledge.setdefault(npc_id, [])
            relationships.setdefault(npc_id, {"stage": "nhan_vat_phu", "trust": 0, "affection": 0, "tension": 0})
            changed = True
        elif action in {"deactivate", "retire"}:
            npc = roster.get(npc_id)
            if isinstance(npc, dict) and npc.get("temporary"):
                npc["is_active"] = False
                npc["last_seen_turn"] = turn_number
                npc["retired_reason"] = str(update.get("reason", "Đã hoàn thành vai trò trong cảnh")).strip()
                changed = True

    # Chỉ xóa NPC phụ đã rời cảnh đủ lâu và không còn giữ thread/xung đột mở.
    open_text = json.dumps({
        "conflicts": get_conflict_context(state),
        "facts": state.get("facts", [])[-40:],
        "suggestions": get_suggestion_context(state)
    }, ensure_ascii=False).lower()
    for npc_id, npc in list(roster.items()):
        if not isinstance(npc, dict) or not npc.get("temporary") or npc.get("is_active"):
            continue
        last_seen = clamp_int(npc.get("last_seen_turn", turn_number), 0, turn_number)
        ttl = clamp_int(npc.get("retire_after_turns", 8), 4, 30)
        if turn_number - last_seen >= ttl and npc_id.lower() not in open_text and str(npc.get("name", "")).lower() not in open_text:
            if blueprint_runtime.lifecycle_enabled(STORY_BIBLE):
                # Registry identity survives retirement; only its scene role ends.
                npc['retired'] = True
                continue
            del roster[npc_id]
            knowledge.pop(npc_id, None)
            relationships.pop(npc_id, None)
            changed = True
    return changed

def apply_relationship_score_deltas(state: Dict[str, Any], updates: Any, responding_ids: set) -> Dict[str, Dict[str, Any]]:
    """Áp điểm sau từng cuộc đối đáp và trả dữ liệu note cho UI."""
    if not isinstance(updates, dict):
        return {}
    relationships = state.setdefault("relationships", {})
    events: Dict[str, Dict[str, Any]] = {}
    for npc_id, payload in updates.items():
        if npc_id not in responding_ids or npc_id not in ROOM_STATE.get("roster", {}) or not isinstance(payload, dict):
            continue
        entry = normalize_relationship_entry(npc_id, relationships.get(npc_id, {}))
        old_title = entry.get("title", "")
        requested_mode = str(payload.get("mode", "")).strip()
        # Chỉ đổi mode khi Player đã nói/đặt ranh giới rõ ràng; tracker phải cung cấp lý do.
        mode_reason = str(payload.get("mode_reason", "")).strip()
        if requested_mode and mode_reason:
            available = STORY_BIBLE.get("relationship_profiles", {}).get(npc_id, {})
            if requested_mode in available:
                archived = entry.setdefault("archived_metrics", {})
                archived[entry.get("mode", "unknown")] = dict(entry.get("metrics", {}))
                entry["mode"] = requested_mode
                new_profile = get_relationship_profile(npc_id, requested_mode)
                entry["metrics"] = dict(archived.get(requested_mode, {}))
                entry = normalize_relationship_entry(npc_id, entry)

        profile = get_relationship_profile(npc_id, entry.get("mode"))
        allowed_metrics = profile.get("metrics", {})
        metric_deltas = payload.get("metrics", {})
        applied = []
        if isinstance(metric_deltas, dict):
            for metric_key, raw_delta in metric_deltas.items():
                if metric_key not in allowed_metrics:
                    continue
                delta = clamp_int(raw_delta, -15, 15)
                if delta == 0:
                    continue
                old_value = clamp_int(entry["metrics"].get(metric_key, 0), 0, 100)
                new_value = clamp_int(old_value + delta, 0, 100)
                actual_delta = new_value - old_value
                if actual_delta:
                    entry["metrics"][metric_key] = new_value
                    applied.append({"key": metric_key, "label": allowed_metrics[metric_key], "delta": actual_delta, "value": new_value})

        title_state = relationship_title(npc_id, profile, entry.get("metrics", {}))
        entry.update(title_state)
        entry["metric_labels"] = allowed_metrics
        entry["mode_label"] = {"potential_romance": "Loveline tiềm năng", "friendship": "Friendzone / tình bạn", "rivalry": "Đối thủ", "social": "Quan hệ xã hội"}.get(entry.get("mode"), entry.get("mode"))
        relationships[npc_id] = entry
        if applied or old_title != entry.get("title") or (requested_mode and mode_reason):
            events[npc_id] = {
                "changes": applied,
                "reason": str(payload.get("reason", "")).strip(),
                "mode": entry.get("mode"),
                "mode_label": entry.get("mode_label"),
                "title": entry.get("title"),
                "title_description": entry.get("title_description"),
                "previous_title": old_title,
                "title_changed": old_title != entry.get("title"),
                "next_title": entry.get("next_title"),
                "next_title_at": entry.get("next_title_at")
            }
    return events

def get_story_context(npc_id: Optional[str] = None) -> Dict[str, Any]:
    """Tạo context tối thiểu, có giới hạn tri thức cho từng NPC."""
    state = get_narrative_state()
    active_arc_id = state.get("active_arc", "xuan_ve_de_kinh")
    context: Dict[str, Any] = {
        "session_mode": STORY_BIBLE.get("session_mode", "playroom"),
        "player_profile": ROOM_STATE.get("player", {}),
        "background_before_chat": STORY_BIBLE.get("background_before_chat", []),
        "starting_situation": STORY_BIBLE.get("starting_situation", ""),
        "interaction_contract": STORY_BIBLE.get("interaction_contract", ""),
        "premise": STORY_BIBLE.get("premise", ""),
        "tone": STORY_BIBLE.get("tone", ""),
        "strict_rules": STORY_BIBLE.get("strict_rules", []),
        "active_arc": STORY_BIBLE.get("arcs", {}).get(active_arc_id, {}),
        "runtime": {
            "act": state.get("act"),
            "active_arc": active_arc_id,
            "current_scene": state.get("current_scene"),
            "location": state.get("location"),
            "truth_keys": state.get("truth_keys", []),
            "milestones": state.get("milestones", []),
            "world_clocks": state.get("world_clocks", {}),
            "facts": state.get("facts", [])[-16:],
            "conflicts": get_conflict_context(state),
            "scene_pacing": state.get("scene_pacing", {}),
            "checkpoint_memory": get_checkpoint_context(state),
            "recent_suggestions": get_suggestion_context(state)
        },
        "long_term_memory": {
            "summary": LONG_TERM_MEMORY.get("summary", ""),
            "facts": LONG_TERM_MEMORY.get("facts", [])[-12:],
            "milestones": LONG_TERM_MEMORY.get("milestones", [])[-12:],
            "recent_events": LONG_TERM_MEMORY.get("recent_events", [])[-6:]
        }
    }
    if npc_id:
        context["character_canon"] = get_canon_character(npc_id)
        context["character_knowledge"] = state.get("knowledge", {}).get(npc_id, [])
        context["relationship_state"] = state.get("relationships", {}).get(npc_id, {})
    context["public_character_identities"] = public_character_identities()
    if STORY_BIBLE.get("world"):
        context["world_rules"] = STORY_BIBLE.get("world", {})
    if STORY_BIBLE.get("arc_policy"):
        context["arc_policy"] = STORY_BIBLE["arc_policy"]
    if STORY_BIBLE.get("canon_licensed"):
        context["canon_time_boundary"] = STORY_BIBLE.get("canon_licensed")
    return context


def public_character_identities():
    """Public identity only: cross-character role checks without revealing secrets."""
    if len(ROOM_STATE.get("roster",{})) > 40:
        visible = json.dumps(get_visible_history(8), ensure_ascii=False)
        selected = {item["npc_id"] for item in presence_candidates(visible)}
    else:
        selected = set(ROOM_STATE.get("roster",{}))
    return [{"npc_id":npc_id, "name":canon.get("name", npc.get("name",npc_id)),
             "role":str(canon.get("role",npc.get("role","")))[:180],
             "active":bool(npc.get("is_active"))}
            for npc_id,npc in ROOM_STATE.get("roster",{}).items()
            if npc_id in selected
            for canon in [get_canon_character(npc_id)]]

def clamp_int(value: Any, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return low

def parse_json_object(raw: Any) -> Dict[str, Any]:
    """Đọc JSON đầu tiên kể cả khi model bọc code fence hoặc nối thêm văn bản."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        parsed, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(parsed, dict):
        raise ValueError("LLM response is not a JSON object")
    return parsed


def request_llm_json(model: str, messages: List[Dict[str, str]],
                     output_budget: int = RESPONSE_MAX_OUTPUT_TOKENS,
                     purpose: str = "JSON") -> Dict[str, Any]:
    """One provider-aware JSON contract for every AI subsystem.

    Gemini 3.8: low/medium/high thinking only; omit deprecated sampling knobs.
    Retry incomplete/empty JSON once, never patch partial prose or replay a turn.
    The API ceiling includes thinking; visible prose limits remain in prompts.
    """
    if client is None:
        raise ValueError("AI client is not configured")
    options = None
    budget = output_budget
    for attempt in range(2):
        request_messages = copy.deepcopy(messages)
        # JSON mode gateways require an explicit JSON instruction in message content,
        # not merely response_format. Enforce centrally for every subsystem/model.
        if not any('json' in str(item.get('content', '')).casefold() for item in request_messages):
            instruction = 'Return only a valid JSON object matching the requested schema.'
            system = next((item for item in request_messages if item.get('role') == 'system'), None)
            if system is not None:
                system['content'] = str(system.get('content', '')) + '\n' + instruction
            else:
                request_messages.insert(0, {'role':'system', 'content':instruction})
        # Some gateways map system messages to Responses.instructions; their JSON
        # validator checks only Responses.input. The current user input must carry
        # the requirement too, even when the system prompt already mentions JSON.
        user_message = next((item for item in reversed(request_messages) if item.get('role') == 'user'), None)
        if user_message is None:
            request_messages.append({'role':'user', 'content':'Return only a valid JSON object.'})
        elif 'json' not in str(user_message.get('content', '')).casefold():
            user_message['content'] = str(user_message.get('content', '')) + '\nReturn only a valid JSON object.'
        if attempt:
            request_messages[-1]["content"] += (
                "\nLần trước không có JSON hoàn chỉnh. Chỉ trả một JSON object trọn vẹn, "
                "đóng đủ chuỗi và dấu ngoặc; không thêm giải thích bên ngoài. "
                "Nếu có thoại: 2–5 segments sinh động, dẫn khoảng 220 token, thoại khoảng 110 token."
            ) if not (purpose in {'scene','scene repair'} and budget > RESPONSE_MAX_OUTPUT_TOKENS) else (
                '\nLần trước không có JSON hoàn chỉnh. Trả một JSON object đúng schema, '
                'đóng đủ chuỗi/dấu ngoặc, không giải thích ngoài JSON. Giữ nhịp và độ đầy đặn '
                'của cảnh theo yêu cầu; hoàn thành trong output budget.'
            )
        if attempt:
            budget = max(budget, RESPONSE_RETRY_OUTPUT_TOKENS)
        if model.startswith("gpt-"):
            budget = max(budget, 4096)
        token_parameter = "max_completion_tokens" if model.startswith("gpt-") else "max_tokens"
        input_bytes = len(compact_json(request_messages).encode('utf-8'))
        ceiling = INPUT_BYTE_LIMITS.get(purpose, INPUT_BYTE_LIMITS['default'])
        if input_bytes > ceiling:
            raise ValueError(f'{purpose}: input budget exceeded ({input_bytes}/{ceiling} bytes); request not sent')
        if options is None:
            options = llm_config.request_options(model)
            budget = json_output_budget(model, purpose, budget, options)
        timeout = llm_config.request_timeout()
        accounting_id = TOKEN_METER.begin(purpose=purpose, model=model, input_bytes=input_bytes,
                                          provider=llm_config.provider_for_model(model))
        started = time.monotonic()
        try:
            completion = client.chat.completions.create(
                model=model,
                response_format={"type": "json_object"},
                messages=request_messages,
                timeout=timeout,
                **options,
                **{token_parameter: budget},
            )
        except APITimeoutError as error:
            elapsed = time.monotonic() - started
            cause = error.__cause__
            phase = next((label for cls,label in (
                (httpx.ReadTimeout,'read'), (httpx.ConnectTimeout,'connect'),
                (httpx.WriteTimeout,'write'), (httpx.PoolTimeout,'pool'))
                if isinstance(cause, cls)), 'unknown')
            TOKEN_METER.finish(accounting_id, elapsed, 'timeout', type(error).__name__, phase)
            print(f'[Error] LLM timeout: purpose={purpose}, model={model}, phase={phase}, elapsed={elapsed:.2f}s, input_bytes={input_bytes}')
            raise RuntimeError(
                f'{purpose} / {model}: timeout ({phase}) sau {elapsed:.1f}s; '
                'không tự gửi lại. Chưa nhận được usage nên chưa xác định token/chi phí upstream.'
            ) from error
        except Exception as error:
            TOKEN_METER.finish(accounting_id, time.monotonic()-started, 'error', type(error).__name__)
            raise
        TOKEN_METER.finish(accounting_id, time.monotonic()-started, 'response_received')
        TOKEN_METER.record(getattr(completion, "usage", None), accounting_id)
        choices = getattr(completion, "choices", None)
        choice = choices[0] if choices else None
        finish = getattr(choice, "finish_reason", None)
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        block_reason = response_block_reason(completion)
        if block_reason:
            TOKEN_METER.finish(accounting_id, time.monotonic()-started, 'blocked', 'ProviderContentBlocked')
            print(f'[Warning] {purpose}: provider blocked response (model={model}, reason={block_reason}); no retry')
            raise ProviderContentBlocked(purpose, model, block_reason)
        try:
            if choice is None or not isinstance(content, str) or not content.strip():
                raise ValueError("AI returned no JSON text")
            if finish == "length":
                raise ValueError("AI exhausted output budget before completing response")
            return parse_json_object(content)
        except (json.JSONDecodeError, ValueError) as error:
            usage = getattr(completion, "usage", None)
            details = getattr(usage, "completion_tokens_details", None)
            print(f"[Warning] {purpose}: incomplete JSON (attempt={attempt + 1}, "
                  f"model={model}, finish_reason={finish}, "
                  f"output_tokens={getattr(usage, 'completion_tokens', None)}, "
                  f"reasoning_tokens={getattr(details, 'reasoning_tokens', None)}): {error}")
            if attempt:
                raise ValueError(
                    f"{purpose}: AI did not return complete JSON after two attempts "
                    f"(model={model}, finish_reason={finish}, output_budget={budget}, "
                    f"output_tokens={getattr(usage, 'completion_tokens', None)}, "
                    f"reasoning_tokens={getattr(details, 'reasoning_tokens', None)})"
                ) from error
            budget = json_output_budget(model, purpose, budget, options, exhausted=finish == "length")
    raise AssertionError("unreachable")

def truncate_to_token_budget(text: Any, limit: int) -> str:
    """Giữ các câu hoàn chỉnh trong ngân sách; tuyệt đối không cắt rồi thêm dấu ba chấm."""
    clean = str(text or "").strip()
    pieces = re.findall(r"\w+|[^\w\s]", clean, flags=re.UNICODE)
    if len(pieces) <= limit:
        return clean
    sentences = re.split(r"(?<=[.!?。！？])\s+", clean)
    kept: List[str] = []
    used = 0
    for sentence in sentences:
        count = len(re.findall(r"\w+|[^\w\s]", sentence, flags=re.UNICODE))
        if count == 0:
            continue
        if used + count > limit:
            break
        kept.append(sentence.strip())
        used += count
    if kept:
        return " ".join(kept).strip()
    # Một câu đơn quá dài: ưu tiên giữ nguyên ý thay vì tạo câu cụt có “...”.
    return clean

def compose_response_text(narration: str, dialogue: str, segments: Optional[List[Dict[str, str]]] = None) -> str:
    if segments:
        return "\n\n".join(
            f"“{segment['text']}”" if segment.get("type") == "dialogue" else segment.get("text", "")
            for segment in segments if segment.get("text")
        )
    return "\n\n".join(part for part in [narration.strip(), f"“{dialogue.strip()}”" if dialogue.strip() else ""] if part)

def normalize_npc_response(value: Any) -> Dict[str, Any]:
    """Normalize structure without cutting prose. Generation owns length control.

    Word/punctuation regex counts are NOT provider tokens, especially in Vietnamese.
    Never discard a complete response or trim its sentences using that proxy.
    """
    raw_segments = value.get("segments", []) if isinstance(value, dict) else []
    raw_suggestions = value.get("suggestions", []) if isinstance(value, dict) else []
    segments: List[Dict[str, str]] = []
    if isinstance(raw_segments, list):
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type", "")).lower()
            if kind not in {"narration", "dialogue"}:
                continue
            text = str(item.get("text", "")).strip().strip('"“”')
            if text:
                segments.append({"type": kind, "text": text})

    if segments:
        narration = " ".join(s["text"] for s in segments if s["type"] == "narration")
        dialogue = " ".join(s["text"] for s in segments if s["type"] == "dialogue")
    elif isinstance(value, dict):
        narration = str(value.get("narration", "")).strip()
        dialogue = str(value.get("dialogue", "")).strip().strip('"“”')
    else:
        raw = str(value or "").strip()
        matches = re.findall(r'[“"]([^”"]+)[”"]', raw, flags=re.DOTALL)
        dialogue = " ".join(m.strip() for m in matches if m.strip())
        narration = re.sub(r'[“"][^”"]+[”"]', " ", raw, flags=re.DOTALL)
        narration = re.sub(r"\s+", " ", narration).strip()
        if not dialogue:
            dialogue, narration = raw, ""
    if not segments:
        segments = [
            {"type": kind, "text": text}
            for kind, text in (("narration", narration), ("dialogue", dialogue)) if text
        ]
    suggestions = [str(item).strip() for item in raw_suggestions[:2] if isinstance(item, str) and str(item).strip()]
    return {
        "narration": narration,
        "dialogue": dialogue,
        "segments": segments,
        "suggestions": suggestions,
        "presence_updates": value.get("presence_updates", [])[:8] if isinstance(value,dict) and isinstance(value.get("presence_updates"),list) else [],
        "text": compose_response_text(narration, dialogue, segments)
    }

def latest_player_speech():
    entry = next((item for item in reversed(ROOM_STATE.get("history",[])) if item.get("is_player")),{})
    if entry.get("segments"):
        return [str(s.get("text","")) for s in entry["segments"] if isinstance(s,dict) and s.get("type") == "speech"]
    return [str(entry.get("text","")).split(":",1)[-1]] if entry.get("intent") == "speech" else []


def action_only_dialogue(text):
    # Named imperatives and questions are genuine speech, not stage directions.
    if re.search(r'(?:\b(?:đi|nhé|nhỉ|nào|ạ)\s*[.!]?\s*$|\?\s*$)', text.strip(), re.I):
        return False
    names = [str(npc.get("name","")) for npc in ROOM_STATE.get("roster",{}).values()]
    names.append(str(ROOM_STATE.get("player",{}).get("name","")))
    aliases = {name for name in names if name}
    aliases.update(" ".join(name.split()[-2:]) for name in names if len(name.split()) > 2)
    # Only clear named third-person actions, not ambiguous first/second-person speech.
    return any(re.match(re.escape(name)+r'\s+(?:(?:đã|vừa|khẽ|đang)\s+)?(?:nhấc|đặt|bước|ngồi|đứng|cười|gật|quay|rót|nhìn|cúi|tiến)\b',text.strip(),re.I)
               for name in aliases)


def dialogue_contract_issues(npc_id, reply):
    issues=[]
    spoken = latest_player_speech()
    for segment in reply.get("segments",[]):
        if segment.get("type") != "dialogue":
            continue
        words = re.findall(r'\w+', str(segment.get("text","")).casefold())
        if action_only_dialogue(segment.get("text","")):
            issues.append("action_mislabeled_as_dialogue")
        if len(words) >= 12:
            for speech in spoken:
                source = re.findall(r'\w+',speech.casefold())
                match = SequenceMatcher(None,source,words,autojunk=False)
                if len(source) >= 12 and (match.ratio() >= .86 or match.find_longest_match().size / len(words) >= .9):
                    issues.append("player_speech_echo")
                    break
    return list(dict.fromkeys(issues))


def enforce_dialogue_contract(npc_id, reply):
    repaired = copy.deepcopy(reply)
    for segment in repaired.get("segments",[]):
        if segment.get("type") == "dialogue" and action_only_dialogue(segment.get("text","")):
            segment["type"] = "narration"
    repaired = normalize_npc_response(repaired)
    if "player_speech_echo" in dialogue_contract_issues(npc_id,repaired):
        raise HTTPException(status_code=503,detail="AI nhầm lời Player thành lời NPC; lượt chưa được lưu. Hãy thử lại hoặc chọn model khác.")
    return repaired


def player_addressed_group_ids():
    """Public speaking-act attribution resolves a group, independent of quote wording."""
    entry = next((item for item in reversed(ROOM_STATE.get('history', [])) if item.get('is_player')), {})
    actions = [str(s.get('text', '')) for s in entry.get('segments', [])
               if isinstance(s, dict) and s.get('type') == 'action']
    group = r'(?:các ngươi|các em|mọi người|các người|(?:bọn|đám|các|nhóm)\s+(?:nô gia|gia nhân|người hầu|thị nữ|thư đồng))'
    staff_group = False
    found = False
    for text in latest_player_speech():
        match = re.search(r'(?:^|[.!?\n])\s*(' + group + r')\b', text, re.I)
        if match:
            found = True
            staff_group = bool(re.search(r'nô gia|gia nhân|người hầu|thị nữ|thư đồng', match[1], re.I))
            break
    if not found:
        pattern = (r'\b(?:tôi|ta|mình|player)\s+(?:nghiêm giọng\s+)?'
                   r'(?:hỏi|gọi|bảo|nhờ|nói với|nói cùng|yêu cầu)\s+(?:lại\s+)?(' + group + r')\b')
        for text in actions:
            for match in re.finditer(pattern, text, re.I):
                prefix = re.split(r'[.!?;,]', text[:match.start()])[-1]
                if re.search(r'\b(?:nếu|giả sử|ước gì|định|sẽ|muốn)\b', prefix, re.I):
                    continue
                found = True
                staff_group = bool(re.search(r'nô gia|gia nhân|người hầu|thị nữ|thư đồng', match[1], re.I))
                break
            if found:
                break
    if not found:
        return set()
    staff_ids = STORY_BIBLE.get('interaction_policy', {}).get('deferential_ids', [])
    result = set()
    for npc_id,npc in ROOM_STATE.get('roster', {}).items():
        if not npc.get('is_active') or npc.get('scene_scope', ROOM_STATE.get('setting')) != ROOM_STATE.get('setting'):
            continue
        staff = npc_id in staff_ids or npc.get('speech_protocol') == 'deferential' or bool(
            re.search(r'thị nữ|gia nhân|người hầu|thư đồng|nô gia', str(npc.get('role', '')), re.I))
        if (staff_group and staff) or (not staff_group and (staff or npc.get('temporary'))):
            result.add(npc_id)
    return result


def player_addressed_ids():
    """Resolve public vocatives and explicit player attribution, never mere mentions.

    This permission comes from Player's text, not the model's speech_reason claim.
    Thoughts and director descriptions cannot grant an NPC an invitation to speak.
    """
    entry = next((item for item in reversed(ROOM_STATE.get('history', [])) if item.get('is_player')), {})
    segments = entry.get('segments', [])
    actions = [str(item.get('text', '')) for item in segments
               if isinstance(item, dict) and item.get('type') == 'action']
    speech = latest_player_speech()
    aliases_by_id = STORY_BIBLE.get('interaction_policy', {}).get('address_aliases', {})
    addressed = player_addressed_group_ids()
    for npc_id, npc in ROOM_STATE.get('roster', {}).items():
        aliases = [str(npc.get('name', '')).strip()] + aliases_by_id.get(npc_id, [])
        for alias in aliases:
            if not alias:
                continue
            name = re.escape(alias)
            if any(re.search(r'(?:^|[\n:])\s*' + name + r'\s*(?:[,!]|ơi\b)', text, re.I) for text in speech):
                addressed.add(npc_id)
            if any(re.search(r'\b(?:cảm\s*ơn|nhờ|mời|phiền|xin hỏi)\s+' + name + r'(?!\w)', text, re.I)
                   for text in speech):
                addressed.add(npc_id)
            # Require a completed observable speaking act; "nhắc đến", "định hỏi",
            # "không hỏi" and quoted/imagined actions deliberately do not match.
            pattern = (r'\b(?:tôi|ta|mình|player)\s+(?:(?:quay sang|quay lại)\s+)?'
                       r'(?:hỏi|gọi|bảo|nhờ|nói với|nói cùng|đáp lời|yêu cầu)\s+' + name + r'(?!\w)')
            if any(re.search(pattern, text, re.I) for text in actions):
                addressed.add(npc_id)
    return addressed


def speaking_allowed(npc_id, decision, target_id, public_text):
    policy = STORY_BIBLE.get("interaction_policy",{})
    npc = ROOM_STATE.get('roster', {}).get(npc_id, {})
    # Etiquette is a writing constraint, not authorization to access an API.
    # In contextual mode, presence/identity remain hard checks in the scene validator;
    # model-supplied speech_reason/evidence is optional and must not abort the scene.
    if policy.get('speech_enforcement') == 'contextual':
        return bool(npc.get('is_active'))
    if npc_id not in policy.get("deferential_ids",[]) and npc.get('speech_protocol') != 'deferential':
        return True
    if target_id == npc_id:
        return True
    if npc_id in player_addressed_ids():
        return True
    if (policy.get('allow_relevant_initiative') is True
            and decision.get('speech_reason') == 'role_initiative'
            and isinstance(decision.get('initiative_reason'), str)
            and decision['initiative_reason'].strip()
            and is_current_scene_member(npc)):
        return True
    aliases = [ROOM_STATE.get("roster",{}).get(npc_id,{}).get("name","")]+policy.get("address_aliases",{}).get(npc_id,[])
    if any(re.search(r'(?:^|[\n:])\s*'+re.escape(alias)+r'\s*[,!]',speech,re.I)
           for alias in aliases if alias for speech in latest_player_speech()):
        return True
    reason = decision.get("speech_reason")
    evidence = str(decision.get("speech_evidence","")).strip().casefold()
    if not evidence or evidence not in public_text.casefold():
        return False
    markers = policy.get("urgent_markers",[]) if reason == "urgent_warning" else policy.get("service_requests",[]) if reason == "material_service_need" else []
    return any(str(marker).casefold() in evidence for marker in markers)


def parse_mixed_player_input(text: str) -> Dict[str, Any]:
    """Tách một ô nhập thành mô tả, lời nói, suy nghĩ và @mention."""
    segments: List[Dict[str, str]] = []
    buffer: List[str] = []
    mode = "action"
    quote_end = ""

    def flush() -> None:
        content = "".join(buffer).strip()
        buffer.clear()
        if content:
            segments.append({"type": mode, "text": content})

    pairs = {'"': '"', '“': '”', "'": "'", '‘': '’'}
    source = str(text or "")
    index = 0
    while index < len(source):
        char = source[index]
        if mode != "command" and source.startswith("[[", index):
            flush()
            mode, quote_end = "command", "]]"
            index += 2
            continue
        if mode == "command" and source.startswith("]]", index):
            flush()
            mode, quote_end = "action", ""
            index += 2
            while index < len(source) and source[index] in pairs and not source[index + 1:].strip():
                index += 1
            continue
        if mode == "action" and char in pairs:
            flush()
            mode = "speech" if char in {'"', '“'} else "thought"
            quote_end = pairs[char]
        elif mode in {"speech", "thought"} and char == quote_end:
            flush()
            mode, quote_end = "action", ""
        else:
            buffer.append(char)
        index += 1
    flush()

    # Nếu thiếu dấu đóng, vẫn giữ loại đoạn mà người chơi đã mở.
    active = ROOM_STATE.get("roster", {})
    mention_ids: List[str] = []
    searchable = sorted(
        ((npc_id, str(npc.get("name") or npc_id)) for npc_id, npc in active.items()),
        key=lambda pair: len(pair[1]), reverse=True
    )
    raw_lower = " ".join(segment.get("text", "") for segment in segments
                         if segment.get("type") != "thought").lower()
    for npc_id, name in searchable:
        mention_pattern = rf"(?<!\w)@(?:{re.escape(name)}|{re.escape(npc_id)})(?!\w)"
        if re.search(mention_pattern, raw_lower, flags=re.IGNORECASE):
            mention_ids.append(npc_id)
            for segment in segments:
                if segment.get("type") == "thought":
                    continue
                segment["text"] = re.sub(mention_pattern, "", segment["text"], flags=re.IGNORECASE).strip()
    # Dọn các nhãn tự nhiên đứng ngay trước dấu nháy để UI không hiện “nói:”/“nghĩ trong lòng:” như hành động riêng.
    for index, segment in enumerate(segments):
        if index == 0 or segment.get("type") not in {"speech", "thought"}:
            continue
        previous = segments[index - 1]
        if previous.get("type") != "action":
            continue
        suffix = r"(?:nói|lên tiếng)\s*:\s*$" if segment["type"] == "speech" else r"(?:nghĩ(?:\s+trong\s+lòng)?|thầm\s+nghĩ)\s*:\s*$"
        previous["text"] = re.sub(suffix, "", previous.get("text", ""), flags=re.IGNORECASE).strip()
    segments = [
        segment for segment in segments
        if segment.get("text") and (segment.get("type") != "action" or re.search(r"\w", segment["text"], flags=re.UNICODE))
    ]
    return {"segments": segments, "mention_ids": mention_ids}

def format_mixed_turn(parsed: Dict[str, Any]) -> str:
    player_name = ROOM_STATE.get("player", {}).get("name") or "Người chơi"
    labels = {"action": "HÀNH ĐỘNG/MÔ TẢ", "speech": "LỜI NÓI", "thought": "SUY NGHĨ RIÊNG", "thought_hidden": "BIỂU HIỆN QUAN SÁT ĐƯỢC", "command": "CHỈ THỊ ĐẠO DIỄN ƯU TIÊN"}
    parts = [f"[{labels.get(seg['type'], seg['type'])}]: {seg['text']}" for seg in parsed.get("segments", [])]
    if parsed.get("mention_ids"):
        names = [ROOM_STATE.get("roster", {}).get(npc_id, {}).get("name", npc_id) for npc_id in parsed["mention_ids"]]
        parts.append("[NHẮC ĐẾN]: " + ", ".join(names))
    return f"{player_name}: " + " ".join(parts)

def visible_mixed_turn(parsed: Dict[str, Any]) -> str:
    safe = []
    for segment in parsed.get("segments", []):
        if segment.get("type") == "thought":
            safe.append({"type": "thought_hidden", "text": "Player thoáng trầm ngâm; NPC không biết nội dung suy nghĩ."})
        else:
            safe.append(segment)
    return format_mixed_turn({"segments": safe, "mention_ids": parsed.get("mention_ids", [])})

def current_turn_directives() -> List[str]:
    """Only the latest Player turn can direct the current scene."""
    for item in reversed(ROOM_STATE.get("history", [])):
        if not item.get("is_player"):
            continue
        if item.get("intent") == "command":
            return [str(item.get("raw_content") or item.get("text", "")).strip()]
        if item.get("intent") == "mixed":
            return [str(segment.get("text", "")).strip() for segment in item.get("segments", [])
                    if segment.get("type") == "command" and str(segment.get("text", "")).strip()]
        return []
    return []

def format_turn_payload(intent: str, text: str, target_id: Optional[str]) -> str:
    """Bao gói tin nhắn tuân theo nguyên tắc Information Boundary Control."""
    player_name = ROOM_STATE.get("player", {}).get("name") or "Người chơi"
    active_roster = get_active_npcs()

    if intent == "speech":
        return f'{player_name} (Nói): "{text}"'
    elif intent == "thought":
        return f'[{player_name} ĐANG NGHĨ THẦM TRONG ĐẦU - CÁC NPC KHÔNG NGHE THẤY NỘI DUNG NÀY]: ({text})'
    elif intent == "command":
        return f'[CHỈ THỊ ĐẠO DIỄN ƯU TIÊN]: {text}'
    elif intent == "mention":
        target_name = active_roster.get(target_id, {}).get("name") if target_id else None
        target_display = target_name if target_name else (target_id or "ai đó")
        return f'{player_name} (Quay sang nói đích danh với {target_display}): "{text}"'
    return f'{player_name}: {text}'

def visible_message_for_npcs(intent: str, formatted_text: str) -> str:
    """Che nội dung độc thoại nội tâm khỏi mọi model đang đóng vai NPC."""
    if intent == "thought":
        player_name = ROOM_STATE.get("player", {}).get("name") or "Người chơi"
        return f"[{player_name} im lặng và có vẻ đang suy nghĩ; NỘI DUNG SUY NGHĨ BỊ ẨN]"
    return formatted_text

def get_visible_history(limit: int = 8) -> List[Dict[str, Any]]:
    visible: List[Dict[str, Any]] = []
    source = [
        item for item in ROOM_STATE.get("history", [])
        if isinstance(item, dict) and item.get("speaker_id") != "__illustrator__" and item.get("type") != "illustration"
    ]
    for item in source[-limit:]:
        safe = dict(item)
        if safe.get("intent") == "thought":
            safe["text"] = visible_message_for_npcs("thought", safe.get("text", ""))
        elif safe.get("intent") == "mixed" and isinstance(safe.get("segments"), list):
            safe_segments = []
            for segment in safe["segments"]:
                if segment.get("type") == "thought":
                    safe_segments.append({"type": "thought_hidden", "text": "Player có vẻ đang suy nghĩ; nội dung bị ẩn."})
                else:
                    safe_segments.append(segment)
            safe["segments"] = safe_segments
            safe["text"] = visible_mixed_turn({"segments": safe_segments, "mention_ids": safe.get("mention_ids", [])})
        visible.append(safe)
    return visible

def room_character_role(npc_id, npc):
    """Room-authored role corrections are a projection, never a save rewrite."""
    role = STORY_BIBLE.get('role_overrides', {}).get(npc_id)
    return role if isinstance(role, str) and role.strip() else npc.get('role', '')


def presence_candidates(public_text: str, story_event=None) -> List[Dict[str, Any]]:
    """Small candidate list, without importing a room's entire character library."""
    text = public_text.casefold()
    candidates = []
    aliases = presence_name_aliases(ROOM_STATE.get('roster', {}))
    addressed = {npc_id for name, npc_id in aliases.items()
                 if re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text)}
    if canon_runtime.enabled(STORY_BIBLE):
        library=load_character_library()
        for npc_id in STORY_BIBLE.get('player_owned_character_ids', []):
            if any(re.search(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',canon_runtime.plain(text))
                   for alias in canon_runtime.spirit_aliases(library.get(npc_id, {}))):
                addressed.add(npc_id)
    siblings = bool(re.search(r'\b(?:hai anh em|anh chị em|các anh em|các chị em)\b', text))
    # Source cast is eligibility for retrieval, NEVER evidence of presence.
    event_cast = set((story_event or {}).get('cast_ids', []))
    event_text = str((story_event or {}).get('content', '')).casefold()
    given_names = {}
    for ident, entry in ROOM_STATE.get('roster', {}).items():
        tail = str(entry.get('name', '')).casefold().split()[-1:]
        if tail:
            given_names.setdefault(tail[0], set()).add(ident)
    for npc_id, npc in ROOM_STATE.get("roster", {}).items():
        canon = get_canon_character(npc_id)
        if canon.get("status") == "deceased" or npc.get("status") == "deceased":
            continue
        name = str(npc.get("name", npc_id))
        score = 100 if npc_id in addressed or npc_id.casefold() in text else 0
        role = str(room_character_role(npc_id, npc)).casefold()
        # Relationship references select profiles, NOT presence. Arrival still
        # requires public evidence, and cousins are not inferred as siblings.
        if siblings and re.search(r'\b(?:anh|chị|em)\b', role) and re.search(r'cùng cha|cùng mẹ|ruột', role) and not re.search(r'\bhọ\b', role):
            score += 60
        score += 20 if npc.get("is_active") else 0
        event_relevant = False
        if npc_id in event_cast:
            forms = [name.casefold(), ' '.join(name.casefold().split()[-2:])]
            given = name.casefold().split()[-1:]
            if given and len(given[0]) >= 3 and len(given_names.get(given[0], set())) == 1:
                forms.append(given[0])
            mentions = sum(bool(re.search(r'(?<!\w)'+re.escape(form)+r'(?!\w)', event_text))
                           for form in set(forms) if form)
            event_relevant = mentions > 0
            score += (80 + 10*mentions) if event_relevant else 5
        candidates.append((score, {"npc_id":npc_id, "name":name, "role":room_character_role(npc_id, npc),
            "event_relevant":event_relevant,
            "context_relevant":score >= 60,
            "active":bool(npc.get("is_active")), "guild":canon.get("guild", ""),
            "known_location":npc.get("location", canon.get("location", "")),
            "manual_locked":"presence_lock_scene" in npc and npc.get("presence_lock_scene") == ROOM_STATE.get("setting")}))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [item for _,item in candidates[:40]]


_DIRECTOR_SETTING_RE = re.compile(
    r'^\s*(?:(?:cập nhật|đổi|đặt|chuyển|sang)\s+(?:sang|tới|đến|về\s+)?bối cảnh\s*(?:là|thành|sang|tới|đến|:)?|'
    r'chuyển\s+cảnh\s+(?:đến|sang|tới)\s*(?:là|thành|:)?)\s*(.+?)\s*$',
    re.I,
)
_GENERIC_SCENE_TARGETS = {
    'tiếp theo', 'mới', 'kế tiếp', 'sau', 'khác', 'cảnh mới', 'cảnh tiếp theo',
    'tiếp', 'lượt tiếp', 'diễn biến tiếp theo',
}
_VAGUE_EVENT_LOCATIONS = {'linh hoạt', 'tùy ý', 'tự do', 'bất kỳ', 'không cố định', 'nhiều nơi'}


def explicit_director_setting(directives: List[str]) -> Optional[tuple]:
    """Extract a concrete target setting from an explicit director scene directive."""
    for item in reversed(directives or []):
        text = str(item or '').strip()
        if not text:
            continue
        match = _DIRECTOR_SETTING_RE.fullmatch(text)
        if not match:
            continue
        target = match.group(1).strip().rstrip('.').strip()
        if not target or len(target) > 1200 or target.casefold() in _GENERIC_SCENE_TARGETS:
            continue
        return target, text
    return None


def concrete_event_location(selected_event: Optional[Dict[str, Any]], current_setting: str) -> str:
    """Return a clean event location when an explicitly triggered event moves the scene."""
    if not isinstance(selected_event, dict):
        return ''
    loc = str(selected_event.get('location') or '').strip()
    if not loc or loc.casefold() in _VAGUE_EVENT_LOCATIONS:
        return ''
    current_cf = str(current_setting or '').casefold()
    if current_cf and (loc.casefold() in current_cf or current_cf in loc.casefold()):
        return ''
    return loc


def commit_presence_plan(plan: Dict[str, Any], candidates: List[Dict[str, Any]],
                         evidence: str, scene_changed: bool = False,
                         defer_departures: bool = False) -> List[Dict[str, Any]]:
    if STORY_BIBLE.get("session_mode") == "companion":
        return []
    plan = dict(plan)
    if canon_runtime.enabled(STORY_BIBLE):
        plan = canon_runtime.filter_presence(plan, ROOM_STATE, load_character_library(), evidence)
    if plan.get("scene_changed") and not scene_changed:
        latest = next((item for item in reversed(ROOM_STATE.get("history", []))
                       if item.get("is_player")), {})
        actions = [str(segment.get("text", "")) for segment in latest.get("segments", [])
                   if segment.get("type") in {"action", "command"}]
        if latest.get("intent") == "command":
            actions.append(str(latest.get("raw_content", "")))
        has_director_command = bool(
            latest.get("intent") == "command"
            or any(
                segment.get("type") == "command" and str(segment.get("text", "")).strip()
                for segment in latest.get("segments", [])
            )
        )
        anchor_raw = str(plan.get("scene_evidence", "")).strip()
        anchor = anchor_raw.casefold()
        in_actions = bool(anchor) and any(anchor in action.casefold() for action in actions)
        in_narrated_evidence = (
            has_director_command
            and bool(anchor)
            and anchor in evidence.casefold()
            and companion_scene._asserted_evidence(anchor_raw)
        )
        if not in_actions and not in_narrated_evidence:
            plan["scene_changed"] = False
    dead = {npc_id for npc_id,canon in STORY_BIBLE.get("characters", {}).items()
            if canon.get("status") == "deceased"}
    if canon_runtime.enabled(STORY_BIBLE):
        dead.update(npc_id for npc_id in ROOM_STATE.get('roster', {})
                    if get_canon_character(npc_id).get('status')=='deceased')
    return apply_presence_plan(ROOM_STATE, plan, {item["npc_id"] for item in candidates},
                               evidence, explicit_scene_change=scene_changed, dead_ids=dead,
                               arrival_limit=None if blueprint_runtime.lifecycle_enabled(STORY_BIBLE) else 3,
                               close_previous_scene=blueprint_runtime.lifecycle_enabled(STORY_BIBLE),
                               defer_departures=defer_departures)


def narration_from_entry(entry):
    segments = entry.get("segments")
    if isinstance(segments,list) and segments:
        return "\n".join(str(s.get("text", "")) for s in segments
                         if isinstance(s,dict) and s.get("type") == "narration")
    if isinstance(entry.get("narration"),str):
        return entry["narration"]
    # Legacy NPC text: ignore quoted dialogue; never read player thought here.
    return re.sub(r'[“"][^”"]*[”"]', ' ', str(entry.get("text", "")))


def reconcile_narrated_presence(reply):
    if STORY_BIBLE.get("session_mode") == "companion":
        return []
    narration = narration_from_entry(reply)
    verified = narrated_presence_updates(ROOM_STATE, narration)
    if canon_runtime.enabled(STORY_BIBLE):
        verified.extend(canon_runtime.spirit_presence_updates(ROOM_STATE, load_character_library(), narration))
    candidates = [{"npc_id":npc_id} for npc_id in ROOM_STATE.get("roster",{})]
    # Metadata cannot activate anyone just mentioned in speech or absent from prose.
    events = commit_presence_plan({"presence_updates":verified}, candidates, narration)
    audit = ROOM_STATE.setdefault("narrative_state",{}).setdefault("presence",{})
    expected = {(u["npc_id"],u["action"]) for u in verified}
    for proposal in reply.get("presence_updates",[])[:8] if isinstance(reply.get("presence_updates"),list) else []:
        if isinstance(proposal,dict) and (str(proposal.get("npc_id")),str(proposal.get("action"))) not in expected:
            audit.setdefault("last_validation",{}).setdefault("rejected",[]).append({
                "npc_id":str(proposal.get("npc_id", ""))[:80], "reason":"narration_does_not_confirm_local_presence"})
    return events


def current_scene_npc_entries():
    history = list(ROOM_STATE.get("history",[]))
    boundary = max((i+1 for i,item in enumerate(history)
                    if str(item.get("text","")).startswith("[CHUYỂN CẢNH]:")), default=0)
    return [entry for entry in history[max(boundary,len(history)-12):] if entry.get("is_player") is False]


def reconcile_recent_scene_presence():
    events = []
    for entry in current_scene_npc_entries():
        if blueprint_runtime.lifecycle_enabled(STORY_BIBLE):
            # Recovery cannot undo a later committed departure just because its
            # older arrival prose remains in the sliding history window.
            history=ROOM_STATE.get('history',[])
            position=next((i for i,item in enumerate(history) if item is entry),len(history))
            superseded={item.get('presence_event',{}).get('npc_id') for item in history[position+1:]
                if isinstance(item.get('presence_event'),dict) and item['presence_event'].get('action')=='leave'}
            narration=narration_from_entry(entry)
            verified=narrated_presence_updates(ROOM_STATE,narration)
            if canon_runtime.enabled(STORY_BIBLE):
                verified+=canon_runtime.spirit_presence_updates(ROOM_STATE,load_character_library(),narration)
            candidates=[{'npc_id':key} for key in ROOM_STATE.get('roster',{})]
            events.extend(commit_presence_plan({'presence_updates':[u for u in verified
                if not (u['action']=='enter' and u['npc_id'] in superseded)]},candidates,narration))
        else:
            events.extend(reconcile_narrated_presence(entry))
    return events


def sync_scene_presence(setting: str, previous_setting: str) -> List[Dict[str, Any]]:
    if setting == previous_setting or STORY_BIBLE.get("session_mode") == "companion":
        return []
    # An explicit scene change expires manual overrides from the previous scene.
    for npc in ROOM_STATE.get("roster", {}).values():
        npc.pop("presence_lock_scene", None)
    if not client or not get_gemini_api_key():
        return []
    candidates = presence_candidates(setting)
    prompt = (
        "Bạn điều phối sự hiện diện thực tế cho playroom, không viết cốt truyện. "
        "Chỉ dùng NPC trong candidates. Chọn tối đa 3 người có lý do ở cảnh mới, "
        "và cho người ở cảnh cũ rời đi khi họ không cùng di chuyển. Không tự triệu hồi "
        "tinh linh, hồi sinh, dịch chuyển người ở xa, nhập NPC ngoài roster hoặc ép loveline. "
        "Nếu không có căn cứ thì giữ nguyên, không cần thêm người mỗi lần. "
        "Chỉ được dùng thông tin địa điểm/vai trò và sự kiện công khai. "
        "reason là lý do cụ thể; evidence phải trích nguyên cụm từ trong bối cảnh mới. "
        "Trả JSON {presence_updates:[{npc_id,action:enter|leave,reason,evidence}]}."
    )
    try:
        plan = request_llm_json(MODEL_ARBITER, [
            {"role":"system", "content":prompt},
            {"role":"user", "content":json.dumps({"previous_setting":previous_setting,
                "new_setting":setting, "candidates":candidates}, ensure_ascii=False)}
        ], purpose="scene presence")
        # The scene is already set by the API, not by the AI's prose.
        plan.pop("new_setting", None)
        return commit_presence_plan(plan, candidates, setting, scene_changed=True)
    except Exception as error:
        print(f"[Warning] Presence không thay roster do lỗi: {error}")
        return []


def _arbitrate_speakers(intent: str, target_id: Optional[str], wrapped_message: str) -> List[Dict[str, Any]]:
    """
    Phân xử lượt nói: Model quyết định:
    1. Những nhân vật nào trong Active Roster sẽ lên tiếng
    2. Thứ tự phát biểu ra sao (order: 1, 2)
    3. Thái độ/tâm trạng (attitude) thế nào dựa vào tính cách, mối quan hệ và khả năng loveline
    """
    active_roster = get_active_npcs()
    if not ROOM_STATE.get("roster"):
        return []

    # Fast-path fallback nếu chưa cấu hình API Key
    current_key = get_gemini_api_key()
    if not client or not current_key or current_key == "YOUR_API_KEY":
        if not active_roster:
            return []
        available_ids = list(active_roster.keys())
        first_id = target_id if (target_id in active_roster) else available_ids[0]
        first_npc = active_roster[first_id]
        loveline_txt = "rung động ngầm, quan tâm dịu dàng" if first_npc.get("can_loveline") else "điềm tĩnh, đúng mực"
        decisions = [
            {
                "speaker_id": first_id,
                "order": 1,
                "attitude": f"Tự nhiên, {loveline_txt}",
                "reasoning": "Mặc định lượt nói đầu tiên."
            }
        ]
        return decisions

    npc_context_list = "\n".join([
        f"- ID '{k}': {v.get('name', k)} ({v.get('role', 'Chưa rõ vai trò')})\n"
        f"  + Tính cách hiển thị: {v.get('personality', 'Tự nhiên')}\n"
        f"  + Canon bất biến: {', '.join(get_canon_character(k).get('immutable_traits', []))}\n"
        f"  + Mối quan hệ: {v.get('relationship', 'Chưa thiết lập')}\n"
        f"  + Trạng thái quan hệ: {json.dumps(get_narrative_state().get('relationships', {}).get(k, {}), ensure_ascii=False)}\n"
        f"  + Loveline: {'có thể phát triển' if v.get('can_loveline') else 'không phải tuyến lãng mạn'}"
        for k, v in active_roster.items()
    ])

    player_info = f"{ROOM_STATE.get('player', {}).get('name', 'Người chơi')} ({ROOM_STATE.get('player', {}).get('role', 'Nhân vật chính')})"
    setting_info = ROOM_STATE.get('setting') or "Không gian mở, chưa có bối cảnh chi tiết."
    narrative_info = json.dumps(get_story_context(), ensure_ascii=False)
    recent_public_narration = "\n".join(narration_from_entry(item) for item in current_scene_npc_entries())
    presence_evidence = setting_info + "\n" + recent_public_narration + "\n" + wrapped_message
    candidates = presence_candidates(presence_evidence)

    system_prompt = f"""
Bạn là Playroom Arbiter (Bộ phân xử lượt nói và điều phối tâm lý cho game AI Interactive Sandbox).
Bối cảnh phòng: {setting_info}
Người chơi: {player_info}

CANON VÀ TRẠNG THÁI CỐT TRUYỆN:
{narrative_info}

DANH SÁCH NHÂN VẬT HIỆN DIỆN TRONG PHÒNG:
{npc_context_list}

ỨNG VIÊN CÓ THỂ VÀO/RỜI CẢNH (chưa hiện diện không có nghĩa đã ở đây):
{json.dumps(candidates, ensure_ascii=False)}
ĐIỀU PHỐI HIỆN DIỆN: Khi có chuyển cảnh thực sự hoặc nhân vật xuất hiện/rời đi hợp lý,
trả presence_updates rõ ràng. Backend chỉ chấp nhận delta có npc_id, action enter|leave,
reason cụ thể và evidence trích nguyên cụm từ trong tin công khai hoặc bối cảnh.
Không chỉ ghi việc bật nhân vật trong reasoning. Chỉ chọn người nói sau khi áp dụng delta.
Hiện diện và lượt nói là hai việc khác nhau: người đã được lời dẫn xác nhận ở ngay trong
cảnh phải có delta enter dù chưa tới lượt nói. Dùng public_character_identities để kiểm
tra chức phận, không gán nghề của cha cho con. Lời kể NPC gần nhất là bằng chứng công khai,
nhưng người được nhắc tới, ở cảnh khác hoặc trong giả định không được coi là đã có mặt.
Không bật chỉ vì được @nhắc hoặc được nói tới trong suy nghĩ; không tự dịch chuyển người ở xa,
triệu hồi tinh linh, hồi sinh, nhập thư viện hay cho đối thủ xuất hiện để gây chuyện.
Tối đa 3 người vào; không cần đổi roster mỗi lượt. Người đã bật/tắt thủ công được giữ trong
cảnh hiện tại. Companion không tự đổi roster. Nếu nghi ngờ thì giữ nguyên.
Chỉ scene_changed=true khi Player đã chuyển nơi qua hành động công khai/chỉ thị [[...]],
không coi mong muốn, giả định, hỏi đường hoặc lời thoại là hành động đã thực hiện.
new_setting chỉ mô tả cảnh mới; scene_evidence phải trích nguyên cụm từ ở lượt hiện tại.

LỊCH SỬ GẦN NHẤT (thought đã được che):
{json.dumps(get_visible_history(4), ensure_ascii=False)}

Tin nhắn vừa gửi vào phòng (Loại tin: {intent}):
"{wrapped_message}"

NHIỆM VỤ ĐIỀU PHỐI (QUAN TRỌNG):
Dựa vào tình huống vừa xảy ra, tính cách, mối quan hệ và khả năng phát triển loveline của từng nhân vật, bạn hãy quyết định:
1. NHÂN VẬT NÀO NÓI: Chọn những nhân vật có lý do phù hợp từ danh sách hiện diện, không giới hạn số người nói.
2. THỨ TỰ NÓI (order: 1, 2, ...): Sắp xếp thứ tự tự nhiên, người bị tác động trực tiếp đáp trước rồi người có điều cần góp nối lời.
3. NHỊP DIỄN CỤ THỂ (attitude): Nêu một phản ứng dễ thấy bằng hành động/cách nói của đúng nhân vật này, không chỉ dán nhãn cảm xúc. Ví dụ người hay giễu cợt có thể cắt ngang bằng một câu châm biếm; người ít nói có thể đưa ra quyết định cụ thể thay vì im lặng chung chung. Không gán cùng một nhịp 'trầm, lạnh, quan sát' cho mọi người.

QUY TẮC PHÂN XỬ:
- CHỈ CHỌN ID đã hiện diện hoặc có delta enter hợp lệ; người có delta leave không được nói.
- Nếu có target_id hoặc intent == 'mention'/'mixed' chứa @mention: Nhân vật được nhắc đích danh PHẢI là người nói (thường ở order 1).
- Với intent == 'mixed': đọc hành động và lời nói công khai, nhưng tuyệt đối không được biết nội dung đoạn thought/suy nghĩ đã bị che.
- Nếu intent == 'thought': NPC không nghe thấy nội dung suy nghĩ, chỉ thấy người chơi trầm ngâm im lặng. Chọn NPC có sự chú ý đặc biệt hoặc có loveline/quan tâm người chơi nhất để hỏi han.
- Nếu lượt có [[...]]: xem đó là chỉ thị đạo diễn ưu tiên về nhịp cảnh, nhân vật cần hiện diện và điều cần diễn ra; chọn người chịu ảnh hưởng trực tiếp, không bỏ qua vì arc cũ. Chỉ từ chối phần bất khả thi hoặc xung đột với dữ kiện đã xác lập, ranh giới đồng thuận và quyền tự quyết của Player.
- Không chọn NPC chỉ vì họ có loveline. Ưu tiên người có mặt, có quyền biết và bị sự kiện tác động trực tiếp.
Không bắt buộc mọi người hiện diện đều nói. Người phục vụ có thể thao tác im lặng trong narration,
không chen câu xã giao vô thưởng vô phạt khi chủ nhân tiếp khách. Tuân thủ policy của phòng:
{json.dumps(STORY_BIBLE.get('interaction_policy',{}),ensure_ascii=False)}
Với người thuộc deferential_ids, chỉ chọn thoại khi được gọi trực tiếp hoặc có speech_reason
urgent_warning|material_service_need cùng speech_evidence trích nguyên yêu cầu/sự kiện công khai.
- Arc là các mạch truyện gợi ý, không phải đường ray. Player có thể rẽ sang địa điểm, mục tiêu hoặc quan hệ mới; hãy chọn NPC phản ứng với điều Player vừa làm thay vì cố kéo họ về checkpoint/arc quen thuộc.
- Không kích hoạt payoff lớn hoặc tiết lộ bí mật khi chưa có căn cứ; nhưng được phép để cảnh và trạng thái phát triển tự do ngoài các arc có sẵn.

Trả về kết quả dưới dạng JSON theo đúng cấu trúc:
{{
  "scene_changed": false,
  "new_setting": "chỉ khi Player thực sự chuyển cảnh",
  "scene_evidence": "cụm từ nguyên văn trong hành động/chỉ thị chuyển cảnh",
  "presence_updates": [{{"npc_id":"id", "action":"enter|leave", "reason":"lý do có mặt/rời đi", "evidence":"cụm từ nguyên văn công khai"}}],
  "decisions": [
    {{
      "speaker_id": "id_1",
      "order": 1,
      "attitude": "Một chỉ dẫn diễn xuất ngắn gồm sắc thái và cách biểu hiện riêng",
      "reasoning": "Lý do chọn nhân vật này và thái độ này"
      ,"speech_reason": "direct_address|substantive_response|urgent_warning|material_service_need",
      "speech_evidence": "cụm từ trong tin Player chứng minh lý do chen lời nếu cần"
    }}
  ]
}}
"""
    try:
        parsed = request_llm_json(
            model=MODEL_ARBITER,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Xác định danh sách decisions (gồm speaker_id, order, attitude, reasoning) dưới dạng JSON."}
            ],
            purpose="arbiter"
        )
        commit_presence_plan(parsed, candidates, presence_evidence)
        presence_audit = get_narrative_state().setdefault("presence",{})
        presence_audit["arbiter_validation"] = copy.deepcopy(presence_audit.get("last_validation",{}))
        presence_audit["arbiter_proposed"] = [
            {"npc_id":str(item.get("npc_id",""))[:80], "action":str(item.get("action",""))[:12]}
            for item in parsed.get("presence_updates",[])[:40] if isinstance(item,dict)
        ] if isinstance(parsed.get("presence_updates"),list) else []
        active_roster = get_active_npcs()
        raw_decisions = parsed.get("decisions", [])

        # Nếu model trả về dạng speakers list cũ thì chuẩn hóa
        if not raw_decisions and "speakers" in parsed:
            raw_decisions = [
                {"speaker_id": spk, "order": idx + 1, "attitude": "Tự nhiên theo tính cách", "reasoning": parsed.get("reasoning", "")}
                for idx, spk in enumerate(parsed["speakers"])
            ]

        valid_decisions = []
        for idx, dec in enumerate(raw_decisions):
            spk_id = dec.get("speaker_id")
            if spk_id in active_roster and spk_id not in [d["speaker_id"] for d in valid_decisions]:
                valid_decisions.append({
                    "speaker_id": spk_id,
                    "order": dec.get("order", idx + 1),
                    "attitude": dec.get("attitude", "Tự nhiên"),
                    "reasoning": dec.get("reasoning", "")
                    ,"speech_reason": dec.get("speech_reason", ""),
                    "speech_evidence": dec.get("speech_evidence", "")
                })

        # Nếu không chọn được ai, fallback chọn nhân vật đầu tiên
        if not valid_decisions:
            if not active_roster:
                return []
            first_id = target_id if target_id in active_roster else list(active_roster.keys())[0]
            valid_decisions = [{
                "speaker_id": first_id,
                "order": 1,
                "attitude": "Phản ứng rõ nét theo cá tính",
                "reasoning": "Fallback do không có đề xuất phù hợp."
            }]

        return valid_decisions
    except Exception as e:
        print(f"[Error] Arbiter exception: {e}")
        active_roster = get_active_npcs()
        if not active_roster:
            return []
        first_id = target_id if target_id in active_roster else list(active_roster.keys())[0]
        return [{
            "speaker_id": first_id,
            "order": 1,
            "attitude": "Điềm đạm",
            "reasoning": f"Lỗi phân xử: {e}"
        }]

def arbitrate_speakers(intent, target_id, wrapped_message):
    proposals = _arbitrate_speakers(intent,target_id,wrapped_message)
    allowed = [d for d in proposals if speaking_allowed(d.get("speaker_id"),d,target_id,wrapped_message)]
    get_narrative_state().setdefault("presence",{})["speaking_rejected"] = [
        {"npc_id":d.get("speaker_id"),"reason":"world_speech_protocol"} for d in proposals if d not in allowed]
    if allowed:
        return allowed
    for npc_id in get_active_npcs():
        if speaking_allowed(npc_id,{},target_id,wrapped_message):
            return [{"speaker_id":npc_id,"order":1,"attitude":"Đáp việc chính, không lặp lời Player."}]
    return [{"speaker_id":next(iter(get_active_npcs())),"order":1,"narration_only":True,
             "attitude":"Chỉ phục vụ bằng hành động im lặng; không có lời thoại."}] if get_active_npcs() else []


def is_current_scene_member(npc):
    return is_scene_member(ROOM_STATE,npc)


def scene_extra_profile_ids():
    """One roster projection for prompt AND validation, including old edited saves."""
    established = {npc_id for group in scene_personnel_requirements()
                   for npc_id in personnel_profile_ids(group['role'])}
    return [npc_id for npc_id,npc in ROOM_STATE.get('roster', {}).items()
            if (is_current_scene_member(npc) or npc_id in established) and (npc.get('temporary') or
                (not npc.get('canon_id') and npc_id not in STORY_BIBLE.get('characters', {})))]


def scene_personnel_source():
    return '\n'.join(narration_from_entry(entry) for entry in current_scene_npc_entries()) + '\n' + '\n'.join(current_turn_directives())


def scene_personnel_requirements():
    if STORY_BIBLE.get('session_mode') == 'companion' or STORY_BIBLE.get('canon_licensed'):
        return []
    counts = {'một':1, 'hai':2, 'ba':3, 'bốn':4, 'năm':5, 'sáu':6, 'bảy':7, 'tám':8, 'chín':9, 'mười':10}
    groups = {}
    pattern = r'\b(\d{1,2}|một|hai|ba|bốn|năm|sáu|bảy|tám|chín|mười)\s+(thị nữ|(?:người|thằng) hầu(?: làm việc nặng)?|thư đồng)\b'
    for match in re.finditer(pattern, scene_personnel_source(), re.I):
        count = int(match[1]) if match[1].isdigit() else counts[match[1].casefold()]
        role = match[2].casefold().replace('thằng hầu', 'người hầu')
        if 0 < count <= 12:
            groups[role] = max(groups.get(role, 0), count)
    return [{'role':role, 'count':count} for role,count in groups.items()]


def personnel_role(role):
    """Stable category despite job qualifiers such as 'làm việc nặng'."""
    role = str(role).casefold()
    if re.search(r'\b(?:người|thằng) hầu\b', role):
        return 'người hầu'
    return next((key for key in ('thị nữ', 'thư đồng') if key in role), role)


def personnel_profile_ids(role):
    """Count established identities, not current attendance.

    Known offstage members of the same established cohort need no new profiles.
    Shared introduction evidence or an exact place anchor links them; unrelated
    staff elsewhere cannot fill a missing group. This NEVER activates anyone.
    """
    roster = ROOM_STATE.get('roster', {})
    matches = {key:npc for key,npc in roster.items()
               if personnel_role(npc.get('role', '')) == personnel_role(role)}
    def place(text):
        return str(text or '').split(',', 1)[0].split(';', 1)[0].strip().casefold()
    current_place = place(ROOM_STATE.get('setting'))
    anchors = {key for key,npc in matches.items() if npc.get('is_active') or (
        current_place and place(npc.get('scene_scope')) == current_place)}
    evidence = {matches[key].get('introduction_evidence') for key in anchors
                if matches[key].get('introduction_evidence')}
    scopes = {matches[key].get('scene_scope') for key in anchors if matches[key].get('scene_scope')}
    return [key for key,npc in matches.items() if key in anchors or (
        npc.get('introduction_evidence') and npc['introduction_evidence'] in evidence) or (
        npc.get('scene_scope') and npc['scene_scope'] in scopes)]


def register_scene_introductions(raw, public_text):
    if companion_cast.enabled(STORY_BIBLE):
        return companion_cast.prepare(ROOM_STATE,STORY_BIBLE,raw,public_text,current_turn_directives())
    profiles = raw.get('npc_introductions', [])
    if not isinstance(profiles, list) or len(profiles) > 12:
        raise ValueError('Invalid npc_introductions; maximum 12 new profiles per scene')
    evidence_source = public_text + '\n' + scene_personnel_source()
    if blueprint_runtime.lifecycle_enabled(STORY_BIBLE):
        evidence_source += '\n' + '\n'.join(narration_from_entry(item) for item in raw.get('responses',[]) if isinstance(item,dict))
    registered = set()
    references = {}
    requirements = scene_personnel_requirements()
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError('Invalid new NPC profile')
        name = str(profile.get('name', '')).strip()
        role = str(profile.get('role', '')).strip()
        original_id = str(profile.get('id', '')).strip()
        npc_id = original_id.lower()
        if not name or len(name) > 40:
            raise ValueError(f'NPC introduction has invalid name: {name!r}')
        if blueprint_runtime.lifecycle_enabled(STORY_BIBLE):
            library = load_character_library()
            if canon_runtime.enabled(STORY_BIBLE):
                canonical_id = canon_runtime.resolve_catalog_profile(profile, library,
                    STORY_BIBLE.get('player_owned_character_ids', []))
            else:
                library={**STORY_BIBLE.get('characters',{}),**library}
                exact=[key for key,value in library.items() if value.get('name','').casefold()==name.casefold()]
                canonical_id=npc_id if npc_id in library else exact[0] if len(exact)==1 else None
            if canonical_id:
                if canonical_id not in ROOM_STATE.get('roster', {}):
                    ROOM_STATE.setdefault('roster', {})[canonical_id] = library_roster_entry(canonical_id, library[canonical_id])
                registered.add(canonical_id)
                references[name] = canonical_id
                if original_id:
                    references[original_id] = canonical_id
                continue
            silver_id = canon_runtime.resolve_silver_profile(profile, STORY_BIBLE.get('celestial_registry', {}))
            if silver_id:
                npc_id = silver_id
        # Resolve identities BEFORE checking new-person evidence or group capacity.
        # A repeated introduction is a reference, not a request to replace a profile.
        matches = [(key, npc) for key,npc in ROOM_STATE.get('roster', {}).items()
                   if str(npc.get('name', '')).strip().casefold() == name.casefold()]
        if matches:
            exact = next((pair for pair in matches if pair[0] == npc_id), None)
            if exact is None and len(matches) != 1:
                raise ValueError(f'NPC {name}: ambiguous existing identity; use its roster ID')
            existing_id, existing_npc = exact or matches[0]
            registered.add(existing_id)
            references[name] = existing_id
            if original_id:
                references[original_id] = existing_id
            continue
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', npc_id):
            normalized = unicodedata.normalize('NFKD', name.casefold().replace('đ', 'd'))
            base = 'npc-' + re.sub(r'[^a-z0-9]+', '-', ''.join(c for c in normalized if not unicodedata.combining(c))).strip('-')
            npc_id = base
            suffix = 2
            while npc_id in ROOM_STATE.get('roster', {}) and ROOM_STATE['roster'][npc_id].get('name', '').casefold() != name.casefold():
                npc_id = f'{base}-{suffix}'
                suffix += 1
        for existing_id,npc in ROOM_STATE.get('roster', {}).items():
            if npc.get('temporary') and npc.get('name', '').casefold() == name.casefold():
                npc_id = existing_id
                break
        evidence = str(profile.get('evidence', '')).strip()
        if not evidence or evidence.casefold() not in evidence_source.casefold():
            group = next((g for g in requirements if personnel_role(g['role']) == personnel_role(role)), None)
            occupied = len(personnel_profile_ids(group['role'])) if group else 0
            existing_member = ROOM_STATE.get('roster', {}).get(npc_id, {}).get('temporary')
            if not group or (occupied >= group['count'] and not existing_member):
                raise ValueError(f'NPC {name}: no verified introduction or available established group slot (role={role})')
            role = group['role']
            evidence = scene_personnel_source()
        if npc_id in registered:
            raise ValueError('Duplicate NPC introduction ID')
        existing = ROOM_STATE.get('roster', {}).get(npc_id)
        if existing and not existing.get('temporary'):
            raise ValueError('Cannot overwrite an existing main character')
        if any(key != npc_id and npc.get('name', '').casefold() == name.casefold()
               for key,npc in ROOM_STATE.get('roster', {}).items()):
            raise ValueError('NPC already exists under another ID; reuse that ID')
        apply_temporary_npc_lifecycle(get_narrative_state(), [{**profile, 'id':npc_id, 'role':role, 'action':'add'}])
        if npc_id not in ROOM_STATE.get('roster', {}):
            raise ValueError('New NPC profile missing required name/role/personality/tone/scene_function or not allowed in this room')
        registered.add(npc_id)
        ROOM_STATE['roster'][npc_id].setdefault('introduction_evidence', evidence)
        if canon_runtime.enabled(STORY_BIBLE) and npc_id.startswith('silver_'):
            ROOM_STATE['roster'][npc_id].update(owner='Player',spirit_key_id=npc_id)
        references[name] = npc_id
        if original_id:
            references[original_id] = npc_id
    for response in raw.get('responses', []):
        if isinstance(response, dict) and response.get('speaker_id') in references:
            response['speaker_id'] = references[response['speaker_id']]
    for requirement in scene_personnel_requirements():
        actual = len(personnel_profile_ids(requirement['role']))
        if actual < requirement['count']:
            raise ValueError(f"Missing scene personnel profiles: {requirement['role']} {actual}/{requirement['count']}; register all explicitly introduced people")
    return registered


def compact_turn_context(public_text, candidates):
    state = get_narrative_state()
    ids = [item['npc_id'] for item in candidates]
    checkpoint = get_checkpoint_context(state)
    missing_personnel = []
    for group in scene_personnel_requirements():
        existing = personnel_profile_ids(group['role'])
        if len(existing) < group['count']:
            missing_personnel.append({**group, 'count':group['count']-len(existing),
                                      'total':group['count'], 'existing_ids':existing})
    visible_source = [
        item for item in ROOM_STATE.get('history', [])
        if isinstance(item, dict) and item.get('speaker_id') != '__illustrator__' and item.get('type') != 'illustration'
    ]
    visible_history = get_visible_history(len(recent_turn_history(visible_source, turns=10)))
    compacted_history = compact_history(visible_history)
    context = {
        'mode': STORY_BIBLE.get('session_mode', 'playroom'),
        'player': {key: ROOM_STATE.get('player', {}).get(key, '') for key in ('name', 'role', 'gender', 'persona')},
        'setting': ROOM_STATE.get('setting', ''),
        'interaction_contract': STORY_BIBLE.get('interaction_contract', ''),
        'strict_rules': STORY_BIBLE.get('strict_rules', []),
        'speech_policy': STORY_BIBLE.get('interaction_policy', {}),
        'canon_time_boundary': STORY_BIBLE.get('canon_licensed', ''),
        'background_before_chat': STORY_BIBLE.get('background_before_chat', [])
                                  if STORY_BIBLE.get('session_mode') == 'companion' else [],
        'directives': current_turn_directives(),
        'history': compacted_history,
        'memory': {
            'summary': checkpoint.get('summary') or LONG_TERM_MEMORY.get('summary', ''),
            'commitments': checkpoint.get('commitments', []),
            'known_outcomes': relevant_items(checkpoint.get('known_outcomes', []), public_text, 8),
            'open_threads': relevant_items(checkpoint.get('open_threads', []), public_text, 6),
            'facts': relevant_items(state.get('facts', []), public_text, 8, 3),
            'conflicts': get_conflict_context(state),
            'recent_suggestions': get_suggestion_context(state),
        },
        'characters': [{
            'speaker_id': npc_id,
            'profile': {key: value for key, value in get_canon_character(npc_id).items()
                        if key not in {'avatar', 'avatar_options'}},
            'presentation': {key: ROOM_STATE['roster'][npc_id].get(key, '')
                             for key in ('personality', 'tone', 'can_loveline')
                             if not get_canon_character(npc_id) or key == 'can_loveline'},
            'knowledge': relevant_items(state.get('knowledge', {}).get(npc_id, []), public_text, 8),
            'personal_lore': ROOM_STATE['roster'][npc_id].get('personal_lore', {}),
            'relationship': {key:value for key,value in state.get('relationships', {}).get(npc_id, {}).items()
                             if key in {'mode', 'metrics', 'boundary', 'title'}},
        } for npc_id in ids[:4]],
        'presence_candidates': candidates,
        'personnel_to_register': missing_personnel,
        'recent_personnel_evidence': scene_personnel_source()[-5000:] if missing_personnel else '',
        'existing_temporary_profiles': [{ 'id':npc_id, 'name':npc.get('name'), 'role':npc.get('role'),
            'is_active':bool(npc.get('is_active')),
            'personality':npc.get('personality'), 'tone':npc.get('tone'), 'personal_lore':npc.get('personal_lore', {}) }
            for npc_id,npc in ROOM_STATE.get('roster', {}).items()
            if npc_id in scene_extra_profile_ids()],
        'arc_hint': {key: value for key, value in STORY_BIBLE.get('arcs', {}).get(state.get('active_arc'), {}).items()
                     if key in {'title', 'summary', 'purpose', 'theme', 'hook'}},
    }
    if compacted_history:
        context['memory']['summary_chronology_note'] = (
            'summary và known_outcomes chỉ tóm tắt các lượt CŨ trước cửa sổ history hiện tại; '
            'KHÔNG dùng chúng để lặp lại phản ứng cho sự kiện đã được xử lý trong history.'
        )
        if compacted_history[-1].get('speaker_id') == 'player':
            context['current_player_turn'] = {
                'speaker_id': 'player',
                'sender': compacted_history[-1].get('sender', ''),
                'segments': compacted_history[-1].get('segments', []),
                'directives': current_turn_directives(),
                'chronology_rule': (
                    'Đây là lượt MỚI NHẤT của Player đang chờ phản hồi NGAY BÂY GIỜ; '
                    'mọi mục đứng trước trong history đều đã diễn ra xong và các nhân vật PHẢI nhớ đủ.'
                ),
            }
    # Four detailed profiles are a retrieval budget, not a speaking quota.
    # Other present canonical characters still need identity and voice guidance;
    # temporary characters already have their compact profiles above.
    represented = {item['speaker_id'] for item in context['characters']} | {
        item['id'] for item in context['existing_temporary_profiles']}
    event_ids = {item['npc_id'] for item in candidates if item.get('event_relevant')}
    for npc_id, npc in ROOM_STATE.get('roster', {}).items():
        if npc_id in represented or not (is_current_scene_member(npc) or npc_id in event_ids):
            continue
        canon = get_canon_character(npc_id)
        context['characters'].append({
            'speaker_id': npc_id,
            'profile': {key:value for key,value in canon.items() if key in {
                'name','role','personality','tone','speech_style','speaking_style',
                'voice','desire','authority','ooc_guard',
                'constraints','boundaries','knowledge_limits','strict_rules'}},
            'presentation': {key:(room_character_role(npc_id,npc) if key=='role' else npc.get(key,''))
                             for key in ('name','role','personality','tone')},
            'knowledge': relevant_items(state.get('knowledge', {}).get(npc_id, []), public_text, 4),
            'personal_lore': npc.get('personal_lore', {}),
            'relationship': {key:value for key,value in state.get('relationships', {}).get(npc_id, {}).items()
                             if key in {'mode','metrics','boundary','title'}},
        })
    # Common OOC rules apply once to all profiles, not once per participant.
    # Preserve every unique character constraint and every original full rule.
    guards = [item.get('profile', {}).get('ooc_guard', []) for item in context['characters']]
    guards = [value for value in guards if isinstance(value, list)]
    shared = list(dict.fromkeys(rule for values in guards for rule in values
                  if isinstance(rule, str) and sum(rule in group for group in guards) > 1))
    if shared:
        context['shared_character_rules'] = shared
        for item in context['characters']:
            profile = item.get('profile', {})
            if isinstance(profile.get('ooc_guard'), list):
                profile['ooc_guard'] = [rule for rule in profile['ooc_guard'] if rule not in shared]
    context['world_rules'] = select_world(STORY_BIBLE.get('world', {}),
        public_text + ' ' + str(ROOM_STATE.get('setting', '')) + ' ' + compact_json(context['characters']), 6500)
    context['speaker_manifest'] = [
        {'speaker_id':npc_id, 'name':ROOM_STATE['roster'][npc_id].get('name', npc_id),
         'aliases':STORY_BIBLE.get('interaction_policy', {}).get('address_aliases', {}).get(npc_id, []),
         'present':is_scene_member(ROOM_STATE, ROOM_STATE['roster'][npc_id]),
         'manual_locked':(not ROOM_STATE['roster'][npc_id].get('is_active') and
                          ROOM_STATE['roster'][npc_id].get('presence_lock_scene') == ROOM_STATE.get('setting'))}
        for npc_id in dict.fromkeys([item['speaker_id'] for item in context['characters']] +
                                    [item['id'] for item in context['existing_temporary_profiles']])]
    context['narrator_id'] = '__narrator__'
    if canon_runtime.enabled(STORY_BIBLE):
        context = canon_runtime.project_context(context, ROOM_STATE, STORY_BIBLE, load_character_library())
    elif blueprint_runtime.enabled(STORY_BIBLE):
        context=blueprint_runtime.project_context(context,ROOM_STATE,STORY_BIBLE)
    elif family_drama_runtime.enabled(STORY_BIBLE):
        context=family_drama_runtime.project_context(context,STORY_BIBLE)
    if blueprint_runtime.enabled(STORY_BIBLE) or canon_runtime.enabled(STORY_BIBLE):
        # Persistent source recall and the preceding exchange must not disappear
        # simply because optional world/profile retrieval filled the input cap.
        context['continuity_memory'] = episodic_memory.recall(STATE_FILE_PATH.parent, ROOM_STATE,
            public_text, {**STORY_BIBLE.get('characters',{}),**load_character_library()}, max_bytes=4200)
    episodic_memory.protect_recent_exchange(context)
    if companion_cast.enabled(STORY_BIBLE):
        context=companion_cast.project_context(context,ROOM_STATE,STORY_BIBLE)
    if STORY_BIBLE.get('session_mode') == 'companion':
        context = companion_scene.project_context(context, ROOM_STATE)
    # One final wire budget for every room, including system instructions,
    # JSON escaping and repair overhead. No independent 35 KB pre-pruning:
    # it would discard relevant context even when the 100 KB request fits.
    return roleplay_sessions.project(context, ROOM_STATE)


def validate_scene_draft(raw, candidates, target_id, public_text, profiled_ids=None, selected_event=None):
    perception_before = copy.deepcopy(ROOM_STATE) if knowledge_scope.enabled(STORY_BIBLE) else None
    if not isinstance(raw, dict) or not isinstance(raw.get('responses'), list):
        raise ValueError('Scene must contain responses array')
    controlled = roleplay_sessions.player_id(ROOM_STATE)
    if controlled:
        for item in raw['responses'] + raw.get('npc_introductions', []):
            if isinstance(item, dict):
                refs = [item.get(k, '') for k in ('speaker_id', 'id', 'name', 'canon_id')]
                if any(roleplay_sessions.owns_reference(ROOM_STATE, ref) for ref in refs):
                    raise ValueError('Player owns this character; do not generate its dialogue or NPC duplicate')
        for update in raw.get('presence_updates', []):
            if isinstance(update,dict) and update.get('npc_id') == controlled:
                raise ValueError('Player identity cannot enter as an NPC')
    allowed_ids = {item['npc_id'] for item in candidates[:4]}
    allowed_ids.update(scene_extra_profile_ids())
    if profiled_ids is not None:
        allowed_ids = set(profiled_ids)
    # Apply only evidence-backed presence changes, then validate the actual speaking roster.
    turn_directives = current_turn_directives()
    presence_evidence = public_text + '\n' + '\n'.join(turn_directives)
    narration = '\n'.join(narration_from_entry(item) for item in raw['responses'] if isinstance(item, dict))
    verified_presence = narrated_presence_updates(ROOM_STATE, narration)
    lifecycle_mode = blueprint_runtime.lifecycle_enabled(STORY_BIBLE)
    if lifecycle_mode:
        if (blueprint_runtime.command_kind(turn_directives) in {'next_scene','skip_time'}
                and (raw.get('scene_changed') is not True or not str(raw.get('new_setting') or '').strip()
                     or not str(raw.get('scene_evidence') or '').strip())):
            explicit_setting = explicit_director_setting(turn_directives)
            if not explicit_setting:
                raise ValueError('Director scene transition requires scene_changed, new_setting and scene_evidence; prose alone does not update the room')
        if canon_runtime.enabled(STORY_BIBLE):
            verified_presence.extend(canon_runtime.spirit_presence_updates(ROOM_STATE, load_character_library(), narration))
        # Metadata may quote newly WRITTEN narration, not only the player's text.
        # Scene-change authority still comes from public actions/directives in
        # commit_presence_plan; dialogue/thought cannot teleport the player.
        presence_evidence += '\n' + narration
    # Enter before speaking; leave after the final response so a farewell can be spoken.
    initial_plan = dict(raw)
    non_empty_directives = [str(d).strip() for d in turn_directives if str(d or '').strip()]
    if non_empty_directives and STORY_BIBLE.get('session_mode') != 'companion':
        anchor_raw = str(initial_plan.get('scene_evidence') or '').strip()
        if (initial_plan.get('scene_changed') is True
                and str(initial_plan.get('new_setting') or '').strip()):
            in_evidence = bool(anchor_raw) and anchor_raw.casefold() in presence_evidence.casefold()
            if not in_evidence and (not anchor_raw or companion_scene._asserted_evidence(anchor_raw)):
                initial_plan['scene_evidence'] = non_empty_directives[-1]
        elif initial_plan.get('scene_changed') is not True:
            explicit_setting = explicit_director_setting(non_empty_directives)
            if explicit_setting:
                initial_plan['scene_changed'] = True
                initial_plan['new_setting'] = explicit_setting[0]
                initial_plan['scene_evidence'] = explicit_setting[1]
            elif selected_event and selected_event.get('id'):
                candidate_index = [{'id': selected_event['id'], 'ready': True, 'selectable': True}]
                matched_id = requested_event([selected_event], candidate_index, non_empty_directives, state=get_narrative_state())
                if not matched_id and any(re.search(rf'\[id:{re.escape(selected_event["id"])}\]', d) for d in non_empty_directives):
                    matched_id = selected_event['id']
                event_loc = concrete_event_location(selected_event, ROOM_STATE.get('setting', '')) if matched_id == selected_event['id'] else ''
                if event_loc:
                    initial_plan['scene_changed'] = True
                    initial_plan['new_setting'] = event_loc
                    initial_plan['scene_evidence'] = non_empty_directives[-1]
    updates = raw.get('presence_updates', [])
    updates = updates if isinstance(updates, list) else []
    initial_plan['presence_updates'] = [u for u in updates if isinstance(u,dict) and u.get('action') != 'leave']
    if lifecycle_mode:
        initial_plan['presence_updates'].extend(u for u in verified_presence if u['action']=='enter')
    plan_candidates = ([{'npc_id':npc_id} for npc_id in ROOM_STATE.get('roster',{})]
                       if lifecycle_mode else candidates)
    previous_setting=ROOM_STATE.get('setting','')
    previous_active={key for key,npc in ROOM_STATE.get('roster',{}).items() if npc.get('is_active')}
    events = commit_presence_plan(initial_plan, plan_candidates, presence_evidence,defer_departures=lifecycle_mode)
    if (lifecycle_mode and blueprint_runtime.command_kind(turn_directives) in {'next_scene','skip_time'}
            and previous_setting == ROOM_STATE.get('setting','')
            and not (explicit_director_setting(turn_directives) and explicit_director_setting(turn_directives)[0] == previous_setting)):
        raise ValueError('Director scene transition was not committed; use a new setting and quote the current public action/directive exactly')
    registered_ids=register_scene_introductions(raw, public_text)
    allowed_ids.update(registered_ids)
    if lifecycle_mode and registered_ids:
        # An existing catalog identity may only now have entered the roster.
        # Re-evaluate against its real ID/profile before checking its first line.
        verified_presence=narrated_presence_updates(ROOM_STATE,narration)
        if canon_runtime.enabled(STORY_BIBLE):
            verified_presence+=canon_runtime.spirit_presence_updates(ROOM_STATE,load_character_library(),narration)
    narrative_candidates = [{'npc_id':npc_id} for npc_id in ROOM_STATE.get('roster', {})]
    if lifecycle_mode:
        destination = director_destination_plan(ROOM_STATE, current_turn_directives(),
            {item.get('speaker_id') for item in raw['responses'] if isinstance(item, dict)})
        if destination:
            if previous_setting != ROOM_STATE.get('setting'):
                destination['new_setting'] = ROOM_STATE['setting']
            events.extend(commit_presence_plan(destination, narrative_candidates, presence_evidence,
                                               defer_departures=True))
    post_registration_updates=[u for u in verified_presence if u['action']=='enter']
    if lifecycle_mode:
        post_registration_updates=initial_plan['presence_updates']+post_registration_updates
    events.extend(commit_presence_plan({'presence_updates':post_registration_updates},
                                      narrative_candidates, presence_evidence if lifecycle_mode else narration))
    validated = []
    for item in raw['responses']:
        if not isinstance(item, dict):
            raise ValueError('Scene response block must be an object')
        segments = item.get('segments')
        if not isinstance(segments, list) or not segments or any(
                not isinstance(s, dict) or s.get('type') not in {'narration', 'dialogue'}
                or not isinstance(s.get('text'), str) for s in segments):
            raise ValueError('Invalid scene segment contract')
        reference = item.get('speaker_id', item.get('speaker_name'))
        narration_only = all(s['type'] == 'narration' for s in segments)
        if narration_only and (reference is None or identity_key(reference) in {'narrator','nguoikechuyen','danchuyen','narration','system'}):
            npc_id = '__narrator__'
        else:
            if canon_runtime.enabled(STORY_BIBLE) and isinstance(reference,str) and reference not in ROOM_STATE.get('roster', {}):
                preferred=canon_runtime.resolve_catalog_profile({'name':reference},load_character_library(),
                    STORY_BIBLE.get('player_owned_character_ids', []))
                if preferred in allowed_ids:
                    reference=preferred
            npc_id = resolve_speaker(reference, ROOM_STATE.get('roster', {}), allowed_ids,
                STORY_BIBLE.get('interaction_policy', {}).get('address_aliases', {}))
            npc_entry = ROOM_STATE.get('roster', {}).get(npc_id, {})
            departing = any(u.get('npc_id') == npc_id and u.get('action') == 'leave'
                for u in updates + verified_presence if isinstance(u, dict))
            if not npc_entry.get('is_active') and not departing:
                canon_profile = load_character_library().get(npc_id, {}) if canon_runtime.enabled(STORY_BIBLE) else {}
                is_spirit = bool(
                    canon_profile.get('variant') == 'platinum_twin'
                    or canon_profile.get('zodiac')
                    or (isinstance(npc_id, str) and npc_id.startswith('silver_'))
                )
                if not (canon_runtime.enabled(STORY_BIBLE) and is_spirit):
                    local_narration = narration_from_entry(item)
                    confirmation = local_speaker_presence_update(
                        ROOM_STATE, npc_id, local_narration, allow_inactive=True)
                    ev_source = local_narration
                    if not confirmation:
                        confirmation = narrated_speaker_presence_update(ROOM_STATE, npc_id, narration)
                        ev_source = narration
                    if confirmation:
                        events.extend(commit_presence_plan({'presence_updates': [confirmation]},
                            narrative_candidates, ev_source, defer_departures=True))
            if not ROOM_STATE.get('roster', {}).get(npc_id, {}).get('is_active'):
                raise ValueError(f'Speaker {npc_id!r} is not present in scene')
            if lifecycle_mode and not is_scene_member(ROOM_STATE, ROOM_STATE['roster'][npc_id]):
                # is_active may still belong to the previous scene while its
                # departures are deferred for farewell dialogue. A local actor
                # must acquire the NEW scope before cleanup, not speak and then
                # silently disappear because its old scope was left untouched.
                if not departing:
                    local_narration = narration_from_entry(item)
                    confirmation = local_speaker_presence_update(ROOM_STATE, npc_id, local_narration)
                    if confirmation:
                        events.extend(commit_presence_plan({'presence_updates': [confirmation]},
                            narrative_candidates, local_narration, defer_departures=True))
                    if not is_scene_member(ROOM_STATE, ROOM_STATE['roster'][npc_id]):
                        raise ValueError(f'Speaker {npc_id!r} is not established in the current scene; '
                                         'confirm local presence before dialogue')
        item['speaker_id'] = npc_id
        # Visible length is a generation target, not a post-hoc tokenization test.
        # API max_tokens limits spend; usage reports actual provider tokens.
        draft = enforce_dialogue_contract(npc_id, normalize_npc_response(item))
        if draft['dialogue']:
            if not speaking_allowed(npc_id, item, target_id, public_text):
                raise ValueError(f"World speech protocol forbids this speaker: {npc_id} ({ROOM_STATE['roster'][npc_id].get('name', npc_id)}); no verified player invitation/service/urgent evidence")
        if not draft['segments']:
            raise ValueError('Empty scene block')
        validated.append({'speaker_id': npc_id, 'order': len(validated)+1, 'draft': draft})
    if not validated:
        raise ValueError('Scene response is empty')
    if companion_cast.enabled(STORY_BIBLE):
        required=companion_cast.requested_speakers(ROOM_STATE,STORY_BIBLE,current_turn_directives())
        spoken={d['speaker_id'] for d in validated if d['draft'].get('dialogue')}
        if required-spoken:
            raise ValueError('Director-requested NPC dialogue must use its own speaker_id: '+', '.join(sorted(required-spoken)))
    if STORY_BIBLE.get('session_mode') == 'companion':
        latest=next((e for e in reversed(ROOM_STATE.get('history',[])) if e.get('is_player')), {})
        actions=[s.get('text','') for s in latest.get('segments',[]) if s.get('type')=='action']
        companion_scene.commit(ROOM_STATE,raw,current_turn_directives(),actions,narration)
    if canon_runtime.enabled(STORY_BIBLE):
        canon_runtime.validate_scene(raw, get_narrative_state(),
            STORY_BIBLE.get('runtime_rules', {}).get('team_aliases', {}),
            STORY_BIBLE.get('runtime_rules', {}).get('announcer_ids', []))
    events.extend(commit_presence_plan({'presence_updates':[u for u in updates if isinstance(u,dict) and u.get('action')=='leave']},
                                      plan_candidates, presence_evidence))
    events.extend(commit_presence_plan({'presence_updates':[u for u in verified_presence if u['action']=='leave']},
                                      narrative_candidates, narration))
    if lifecycle_mode and previous_setting != ROOM_STATE.get('setting',''):
        events.extend(close_scene_membership(ROOM_STATE,previous_active))
    if perception_before is not None:
        knowledge_scope.commit_scene(perception_before, ROOM_STATE, raw, validated,
                                     public_text, current_turn_directives())
    return validated, events


def record_scene_failure(error, raw, profiled_ids, phase):
    """Diagnostic metadata only: no dialogue, private thought, prompt or credentials."""
    record = {'at':datetime.now(timezone.utc).isoformat(), 'phase':phase,
              'room_id':ACTIVE_PLAYROOM_ID, 'model':MODEL_ROLEPLAY, 'reason':str(error),
              'profiled_ids':sorted(profiled_ids),
              'returned_speakers':[{'reference':str(item.get('speaker_id', item.get('speaker_name')))[:120],
                                    'segment_types':[s.get('type') for s in item.get('segments', []) if isinstance(s,dict)]}
                                   for item in raw.get('responses', [])[:12] if isinstance(item,dict)]
                                   if isinstance(raw,dict) and isinstance(raw.get('responses'),list) else []}
    try:
        path = BASE_DIR / 'scene_diagnostics.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    except OSError as failure:
        print(f'[Warning] Cannot save scene diagnostics: {failure}')


def companion_freeform_enabled():
    return (STORY_BIBLE.get('session_mode') == 'companion'
            and STORY_BIBLE.get('response_style', {}).get('mode') == 'freeform')


def scene_output_budget():
    is_rich_mode = (
        companion_freeform_enabled()
        or ensemble_runtime.enabled(STORY_BIBLE)
        or blueprint_runtime.enabled(STORY_BIBLE)
        or canon_runtime.enabled(STORY_BIBLE)
    )
    if not is_rich_mode:
        return RESPONSE_MAX_OUTPUT_TOKENS
    default_budget = 8192 if companion_freeform_enabled() else 10240
    value = STORY_BIBLE.get('response_style', {}).get('max_output_tokens', default_budget)
    if type(value) is not int:
        return default_budget
    return max(RESPONSE_MAX_OUTPUT_TOKENS, min(value, 16384))


def generate_scene_response(target_id, public_text):
    if STORY_BIBLE.get('session_mode') == 'companion':
        # Use a direct scene assignment before writing so the writer itself
        # sees the right setting. execute_turn rolls this back on any failure.
        companion_scene.commit(ROOM_STATE,{},current_turn_directives())
    if companion_cast.enabled(STORY_BIBLE):
        ROOM_STATE.setdefault('narrative_state',{}).setdefault('supporting_cast',{})['presence_history_start']=len(ROOM_STATE.get('history',[]))
        companion_cast.bootstrap_present_roles(ROOM_STATE,STORY_BIBLE,current_turn_directives())
    # Retrieve first so absent but relevant participants can receive profiles.
    # No roster flags change until the scene's existing presence validator runs.
    events = load_events(ACTIVE_PLAYROOM_DIR, STORY_BIBLE)
    family_drama_runtime.reconcile_playthrough(ROOM_STATE, STORY_BIBLE, HISTORY_ARCHIVE_DIR, events)
    selected_event = select_event(events, get_narrative_state(), ROOM_STATE, current_turn_directives(), public_text)
    candidates = presence_candidates(public_text + ' ' + ' '.join(current_turn_directives())
                                     + ' ' + str(ROOM_STATE.get('setting', '')), selected_event)
    if canon_runtime.enabled(STORY_BIBLE):
        candidates = canon_runtime.rank_candidates(candidates, ROOM_STATE, current_turn_directives(), public_text)
    # Full profiles for a bounded cast; targeted NPC gets first priority.
    # Stable sort retains relationship/name relevance from presence_candidates.
    candidates.sort(key=lambda item: item['npc_id'] == target_id, reverse=True)
    context = compact_turn_context(public_text, candidates[:12])
    guidance = story_guidance(events, STORY_BIBLE, get_narrative_state())
    if guidance:
        # Old saves may not have any event records yet. Let the first scene see
        # matching established outcomes too, before the tracker reconciles them;
        # otherwise an explicit next-event command could replay the opening.
        guidance['established_evidence'] = reconciliation_context(events, get_narrative_state(), [
            get_checkpoint_context(get_narrative_state()).get('known_outcomes', []),
            LONG_TERM_MEMORY.get('facts', []), LONG_TERM_MEMORY.get('milestones', [])],
            max_bytes=1200 if STORY_BIBLE.get('arc_policy',{}).get('event_flow')=='parallel' else 2200)
        context['story_guidance'] = guidance
    if selected_event:
        context['story_event'] = selected_event
        # Retrieval may cross into the next unlocked thread. Keep its guidance
        # consistent without prematurely committing an arc change to the save.
        context['arc_hint'] = {key: value for key, value in
            STORY_BIBLE.get('arcs', {}).get(selected_event['arc_id'], {}).items()
            if key in {'title', 'summary', 'purpose', 'theme', 'hook'}}
    prompt = f"""Bạn viết trọn một cảnh tương tác, không phải người giao nhiệm vụ.
Player tự quyết định lời nói, hành động, suy nghĩ và đồng thuận. Dữ kiện đã chốt là nền tảng, không phải giới hạn sức sáng tạo.
{IMPROVISATION_CONTRACT}
Ưu tiên an toàn/quyền Player/sự kiện đã chốt > directives [[...]] > luật và nhân vật > gợi ý arc.
Thực hiện chỉ thị đạo diễn và chuyển thời gian ngay khi hợp lệ. Không đóng khung Player vào arc.
Chọn những người CÓ LÝ DO đáp lời, không giới hạn gò bó số người nói trong lượt; có mặt không đồng nghĩa phải nói.
VĂN PHONG SINH ĐỘNG, KÌ BÍ VÀ KÌ ẢO: Viết giàu hình ảnh, chiều sâu và không khí huyền bí — khắc họa ánh sáng, bóng tối, dư chấn ma lực, cổ ngữ, âm thanh tinh tế, nhịp thở và nội tâm hành động để cảnh truyện cuốn hút như tiểu thuyết giả tưởng; tránh kể cụt lủn hoặc liệt kê khô khan. Giữ tổng cảnh trọn vẹn trong ngân sách output, không chia đều lượt vô thưởng vô phạt.
Tuân speech_policy: gia nhân nói đúng thời điểm và chức phận; ngoài đáp lời được chủ động góp ý hữu ích
khi allow_relevant_initiative bật. Với sáng kiến đó dùng speech_reason=role_initiative và initiative_reason
nêu rõ ích lợi, liên hệ với việc đang diễn ra và vai trò; không dùng để chào hỏi thừa hay cướp lượt chủ nhân.
Nếu speech_enforcement=contextual, lễ phép là yêu cầu diễn xuất, không phải cấm nhân vật nói khi chưa
được gọi; speech_reason, speech_evidence, initiative_reason là ghi chú tùy chọn, không phải giấy phép.
Giữ cá tính, từ vựng, nhịp câu, xưng hô và mục tiêu riêng từng người; phản ứng rồi phát triển tự nhiên.
Mọi nhân vật PHẢI ghi nhớ đủ toàn bộ diễn biến theo thứ tự trong history và phản hồi trực tiếp `current_player_turn` (lượt mới nhất của Player); không lặp lại phản ứng ngỡ ngàng hay câu hỏi cho sự kiện cũ ở đầu history hoặc trong memory.summary đã được xử lý xong.
Không nhại nguyên lời Player, không đóng vai Player, không đọc suy nghĩ ẩn hoặc tri thức của NPC khác.
Giới hạn knowledge của từng NPC; memory facts là sự thật không mặc nhiên mọi người biết.
Đừng lặp gợi ý đã bị bỏ qua, không kết mỗi lượt bằng lựa chọn A/B. Tình cảm phát triển theo đối đáp.
Lời dẫn ngôi ba nằm trong narration; CHỈ lời nói trực tiếp nằm trong dialogue, không kèm động tác.
Viết xen kẽ dẫn–thoại–dẫn–thoại, tự hoàn thành ý trong ngân sách ngay khi viết.
Mục tiêu khoảng {NARRATION_TARGET_TOKENS} token dẫn/cảnh và {DIALOGUE_TARGET_TOKENS} token thoại/mỗi NPC;
cố gắng không vượt {NARRATION_TOKEN_LIMIT}/{DIALOGUE_TOKEN_LIMIT} tương ứng, nhưng ưu tiên câu trọn ý.
Đây là hướng dẫn độ dài, không phải yêu cầu liệt kê/đếm từng token. Không bù dài bằng lời lặp.
Background là quá khứ, không ép tương lai. Arc chỉ gợi ý. Với thế giới canon không bẻ giới hạn sức mạnh.
story_guidance cho biết đại arc, chặng hiện tại và nhịp kế tiếp, không phải sự kiện đã xảy ra.
Không chỉ đáp lời thụ động: khi có điểm nghỉ, được chủ động gieo một dấu hiệu/tiếp xúc hợp cảnh hướng
về đại arc. Nếu ký ức cho thấy nhịp mở đã qua thì tiếp tục từ hệ quả, không diễn lại để khớp dàn ý.
story_event nếu có là tư liệu được backend lấy theo nhịp, KHÔNG phải sự kiện đã xảy ra.
Đọc lượt Player mới nhất trước khi dùng: nếu họ đổi hướng, bận một cuộc đối thoại, từ chối hoặc ký ức
cho thấy nhịp này đã diễn ra, hãy hoãn/bỏ nhịp, không lặp lại hay ép họ. Chỉ thị chuyển tiếp rõ ràng
cho phép mở diễn biến phù hợp ngay, không cho phép quyết định phản ứng/kết quả của Player.
Nội dung kịch bản có thể chứa dự tính/bí mật; chỉ biểu hiện qua nguyên nhân, nhân vật và chứng cứ hợp cảnh.
Không tự coi climax/resolution dự kiến là kết quả chắc chắn; không cho NPC biết hết tư liệu đạo diễn.
Chỉ dùng speaker_id có hồ sơ trong characters, existing_temporary_profiles hoặc npc_introductions của lượt này.
speaker_manifest là danh sách ID chung được backend kiểm tra; sao chép đúng speaker_id từ đó.
Đoạn dẫn không thuộc ai có thể dùng speaker_id="__narrator__", CHỈ chứa narration, không chứa dialogue.
Khi Player/lời dẫn xác lập người mới trong cảnh, tạo hồ sơ trước khi viết thoại qua npc_introductions.
personnel_to_register chỉ liệt kê số hồ sơ còn thiếu sau khi backend đối chiếu nhóm đã có.
Nếu có nhóm còn thiếu, đăng ký đủ số thiếu (không chỉ người đang nói); dùng lại existing_ids,
không tạo bản sao người đã có. Hồ sơ có sẵn không có nghĩa đang hiện diện: người vắng phải có
lời dẫn tới và presence_updates hợp lệ trước khi nói, không tự bật chỉ vì cùng nhóm.
Giữ nguyên tên Player đã đặt; người chưa có tên thì đặt tên ngắn phù hợp văn hóa, tính cách và giọng riêng.
Không biến NPC phụ thành nhân vật chính hay tự gán bí mật/năng lực lớn; được bổ sung tiểu sử đời thường còn trống.
Dùng lại ID người đã có, không tạo bản sao.
Lời gọi tập thể của Player cho phép người liên quan đáp đúng vai; không ép cả nhóm lên tiếng.
Không triệu hồi/hồi sinh/teleport NPC.
Khi chuyển cảnh, dựng người liên quan trong speaker_manifest theo vai trò và địa điểm mới;
event_relevant/cast_ids chỉ là ứng viên, không phải toàn bộ đều đang ở đó hoặc phải nói.
Nếu mở xung đột, thể hiện người gây tác động và người có quyền phản ứng bằng hành động/thoại cụ thể,
không giao toàn cảnh cho một người đứng ngoài hỏi lại điều vừa được kể.
Người từ cảnh cũ chỉ đi cùng khi có lời dẫn xác nhận; không giữ bật chỉ vì trước đó đang active.
Nếu người mới tới phải có lời dẫn xác nhận và presence_updates; evidence trích lời Player cho thay đổi cảnh,
hoặc để lời dẫn rõ ràng xác lập việc tới. Không gọi người bị khóa tắt thủ công.
Trả JSON {{"responses":[{{"speaker_id":"id", "speech_reason":"direct_response|material_service_need|urgent_warning|role_initiative",
"initiative_reason":"chỉ điền khi có sáng kiến hữu ích đúng vai trò",
"speech_evidence":"trích nguyên văn nếu cần", "segments":[{{"type":"narration|dialogue","text":"..."}}],
"suggestions":[]}}], "presence_updates":[{{"npc_id":"id","action":"enter|leave","reason":"...","evidence":"..."}}],
"scene_changed":false,"new_setting":"","scene_evidence":"",
"npc_introductions":[{{"id":"slug","name":"tên riêng","role":"chức phận","personality":"nét riêng ngắn",
"tone":"giọng nói","scene_function":"vai trò hiện tại","evidence":"trích từ Player/directives/recent_personnel_evidence"}}]}}.
ID và evidence có thể bỏ trống cho thành viên personnel_to_register: backend tự tạo ID và gắn căn cứ nhóm.
Khi bỏ ID, dùng tên nhân vật mới trong speaker_id; backend đối chiếu lại. Không thêm người vượt số lượng nhóm.
Có thể lặp speaker_id để các nhân vật nối lời nhau; số blocks tùy nhịp cảnh trong ngân sách output.
Không sinh trường text/narration/dialogue trùng segments.
"""
    if ensemble_runtime.enabled(STORY_BIBLE):
        prompt = ensemble_runtime.scene_prompt(BASE_DIR, STORY_BIBLE)
    elif companion_cast.enabled(STORY_BIBLE):
        prompt = (BASE_DIR / 'runtime_prompts/companion_supporting_cast.md').read_text(encoding='utf-8')
    elif companion_freeform_enabled():
        # Replace the legacy 130/70-token writer, never append a conflicting core.
        prompt = (BASE_DIR / 'runtime_prompts/companion_freeform.md').read_text(encoding='utf-8')
    elif canon_runtime.enabled(STORY_BIBLE):
        prompt = (BASE_DIR / 'runtime_prompts' / 'canon_scene.md').read_text(encoding='utf-8')
    elif blueprint_runtime.enabled(STORY_BIBLE):
        prompt=blueprint_runtime.scene_prompt(BASE_DIR)
    elif family_drama_runtime.enabled(STORY_BIBLE):
        prompt=family_drama_runtime.scene_prompt(STORY_BIBLE)
    if STORY_BIBLE.get('session_mode') == 'companion' and not companion_freeform_enabled():
        prompt += ('\nCompanion vẫn có chuyển cảnh: khi hành động/chỉ thị hiện tại hoặc lời dẫn '
            'xác lập đã tới địa điểm hay thời điểm mới, trả scene_changed=true, new_setting ngắn gọn '
            'và scene_evidence trích nguyên câu xác lập. Lời mời/dự định không là việc đã tới. '
            'Chỉ cập nhật cảnh, không thêm NPC hoặc đổi principal.')
    if knowledge_scope.enabled(STORY_BIBLE):
        context=knowledge_scope.project_context(context,ROOM_STATE,public_text,target_id)
    family_drama_runtime.protect_continuity(context, get_narrative_state())
    if 'established_continuity' in context:
        prompt += ('\nestablished_continuity chốt các mốc đã xảy ra; nếu transcript cũ hoặc dàn ý '
                   'mâu thuẫn, chỉ tiếp tục hành động và đối thoại còn phù hợp với các mốc đó.')
    # Privacy projection can change history indexes. Protect the actual visible
    # preceding exchange after projection, including Mực Son and legacy rooms.
    episodic_memory.protect_recent_exchange(context)
    if ensemble_runtime.enabled(STORY_BIBLE):
        context = ensemble_runtime.project_context(context)
    profiled_ids = {item['speaker_id'] for item in context['speaker_manifest']}
    messages = pack_scene_messages(prompt, context, INPUT_BYTE_LIMITS['scene'])
    snapshot = copy.deepcopy(ROOM_STATE)
    raw = request_llm_json(MODEL_ROLEPLAY, messages, output_budget=scene_output_budget(), purpose='scene')
    try:
        return validate_scene_draft(raw, candidates[:12], target_id, public_text, profiled_ids, selected_event=selected_event)
    except (ValueError, HTTPException) as error:
        record_scene_failure(error, raw, profiled_ids, 'scene')
        ROOM_STATE.clear()
        ROOM_STATE.update(copy.deepcopy(snapshot))
        # One repair only after a concrete validation failure, never a guard call per NPC.
        print(f'[Warning] Scene validation failed: {error}')
        repair_context = copy.deepcopy(context)
        repair_context['scene_repair'] = {
            'validation_error': str(error),
            'present_speaker_ids': sorted(key for key in profiled_ids
                if is_scene_member(ROOM_STATE, ROOM_STATE.get('roster', {}).get(key, {}))),
            'instruction': ('Write a replacement scene from the original context. A profile or a name in history '
                'does not establish presence. Use the appropriate present speakers; an absent character '
                'needs a justified narrated arrival and presence_updates before dialogue. '
                'Do not preserve an invalid speaker merely because it appeared in invalid_draft.'),
        }
        repair = pack_scene_messages(prompt + '\nSửa lỗi kiểm tra: ' + str(error), repair_context,
                                     INPUT_BYTE_LIMITS['scene repair'], invalid_draft=raw)
        repaired = request_llm_json(MODEL_ROLEPLAY, repair, output_budget=scene_output_budget(), purpose='scene repair')
        try:
            return validate_scene_draft(repaired, candidates[:12], target_id, public_text, profiled_ids, selected_event=selected_event)
        except (ValueError, HTTPException) as final_error:
            record_scene_failure(final_error, repaired, profiled_ids, 'scene repair')
            ROOM_STATE.clear()
            ROOM_STATE.update(snapshot)
            raise


def generate_npc_response(npc_id: str, attitude: str = "") -> Dict[str, str]:
    """Sinh lời thoại nhập vai cho NPC dựa trên persona, mối quan hệ, loveline và thái độ được chỉ định."""
    npc = ROOM_STATE.get("roster", {}).get(npc_id)
    if not npc:
        return normalize_npc_response({"narration": f"Nhân vật {npc_id} không tồn tại.", "dialogue": ""})

    npc_name = npc.get("name") or npc_id
    npc_role = room_character_role(npc_id, npc) or "Chưa rõ vai trò"
    npc_personality = npc.get("personality") or "Tự nhiên, bình thường"
    npc_tone = npc.get("tone") or "Thân thiện, xưng hô lịch sự"
    npc_relationship = npc.get("relationship") or "Chưa thiết lập mối quan hệ rõ ràng"
    canon = get_canon_character(npc_id)
    story_context = get_story_context(npc_id)
    relationship_state = get_narrative_state().get("relationships", {}).get(npc_id, {})
    relationship_mode = relationship_state.get("mode")
    can_loveline = bool(npc.get("can_loveline", False) and relationship_mode not in {"friendship", "rivalry", "social"})
    loveline_hint = npc.get("loveline_hint", "")
    setting_info = ROOM_STATE.get("setting") or "Không gian mở"
    player_name = ROOM_STATE.get("player", {}).get("name") or "Người chơi"

    recent_history = "\n".join([
        f"{item.get('sender', 'Unknown')}: {item.get('text', '')}"
        for item in get_visible_history(8)
    ])

    current_key = get_gemini_api_key()
    if not client or not current_key or current_key == "YOUR_API_KEY":
        raise HTTPException(status_code=503, detail="Chưa kết nối dịch vụ AI; lượt này chưa được lưu.")

    directives = current_turn_directives()
    system_prompt = f"""
Bạn là {npc_name}, nhân vật trong một playroom tương tác. Chỉ viết lời và hành động của nhân vật này; Player tự quyết định lời nói, cảm xúc và hành động của họ.

Thứ tự ưu tiên: giới hạn an toàn, quyền tự quyết của Player và sự kiện đã xác lập > chỉ thị [[...]] trong lượt hiện tại > luật thế giới/hồ sơ nhân vật > mạch nói chuyện > gợi ý arc. Chỉ thị [[...]] là quyền đạo diễn của Player, không phải lời thoại NPC nghe thấy: hãy thực hiện thay đổi cảnh, nhịp, sắc thái, sự kiện hoặc hướng tương tác được yêu cầu NGAY trong lượt nếu có thể. Không tự làm yếu chỉ thị thành một câu hỏi xin phép hay lời hứa cho tương lai. Nếu một phần bất khả thi hoặc mâu thuẫn dữ kiện đã chốt, chỉ điều chỉnh phần đó đến phương án gần nhất hợp lý; giữ phần còn lại. Không dùng chỉ thị để viết thay lời nói, cảm xúc hay đồng thuận của Player.
Chỉ thị đang có hiệu lực trong lượt này: {json.dumps(directives, ensure_ascii=False)}
{IMPROVISATION_CONTRACT}
Không tự coi lời suy đoán, lời đồn hay tuyên bố chưa kiểm chứng là sự thật. Không sửa kết quả, thứ hạng hay giới hạn phép thuật đã chốt; không quyết định thay người khác. Được bổ sung chi tiết đời thường theo quy tắc ứng tác ở trên.
Nhận diện điều Player đang thật sự nói hoặc làm. Nếu họ hỏi một thông tin nhân vật biết, trả lời ngay và rõ. Nếu chưa biết, nói ngắn gọn vì sao, rồi tiếp tục phản ứng theo tính cách; không câu giờ nhiều lượt. Nếu đã có kết quả hoặc xung đột đã khép, chuyển sang hệ quả mới, không tái diễn cùng nút thắt. Chỉ mở nhịp mới khi hợp cảnh; không ép Player theo arc, loveline hay lựa chọn A/B.
Giữ giọng riêng của {npc_name}; chủ động hợp lý nhưng không đọc suy nghĩ thầm, không tự nắm thông tin ngoài tầm, không bẻ giới hạn ma thuật hay tính cách để chiều Player. Tính cách phải HIỆN RA trong hành động và lời nói, không chỉ nằm ở hồ sơ: nhân vật được phép trêu, phản bác, đổi nhịp, thú nhận một điều nhỏ, đòi hỏi, tự chọn việc làm hoặc bộc lộ một thói quen riêng đúng hoàn cảnh. Dùng từ vựng, nhịp câu, cách xưng hô và thứ họ chú ý khác biệt; tránh lời đáp trung tính kiểu tư vấn viên, câu khuôn 'ta hiểu', nhìn sâu/im lặng/lạnh lùng lặp lại. Cảm xúc có biên độ nhưng không đồng nghĩa mọi nhân vật đều nóng nảy; sự bình tĩnh cũng có thể sắc, hài, dịu hoặc kiêu. Cho NPC có ý kiến và mục tiêu của chính họ, kể cả khi không thuận Player. Mỗi lượt cần một lựa chọn biểu đạt cụ thể gắn với tính cách, không phải diễn giải tính cách bằng văn dẫn.
Player được quyền bộc lộ năng lực mới của chính mình trong lượt hiện tại: phản ứng với biểu hiện quan sát được và hệ quả hợp lý, không tự biết năng lực ấy từ trước, không bác bỏ chỉ vì chưa ghi trong hồ sơ. Với companion, background_before_chat chỉ là quá khứ, không phải lịch trình tương lai.

Phòng: {setting_info}
Chỉ tạo lời thoại CỦA {npc_name}. Không chép lời Player vào dialogue, không kể lại toàn bộ
lượt Player thay cho phản ứng NPC. Động tác/bối cảnh phải ở narration, không ở dialogue.
Nếu Player đã chuyển chủ đề hoặc cho thời gian trôi đến cuối tiệc/chiều muộn, phản ứng
ngắn vào lời nói rồi chuyển tới mốc đó; không giữ họ ở câu hỏi/chén trà đã bỏ qua.
Player: {player_name}
Nhân vật: {json.dumps({"name": npc_name, "role": npc_role, "personality": npc_personality, "tone": npc_tone, "relationship": npc_relationship, "romance_enabled": can_loveline, "romance_hint": loveline_hint if can_loveline else "", "attitude": attitude}, ensure_ascii=False)}
Hồ sơ canon: {json.dumps(canon, ensure_ascii=False)}
Chi tiết cá nhân đã kể: {compact_json(npc.get('personal_lore', {}))}
Trạng thái truyện: {json.dumps(story_context, ensure_ascii=False)}

Trả JSON với segments gồm 2–6 đoạn narration/dialogue xen kẽ giàu nhịp điệu, sinh động và thấm đẫm sắc thái kì bí, kì ảo (tả ánh sáng, ma lực, không gian, biểu cảm tinh tế). Narration ngôi thứ ba, chỉ kể điều quan sát được; dialogue chỉ có lời nói, không kèm dấu ngoặc kép. Tổng narration mục tiêu {NARRATION_TARGET_TOKENS}, tối đa {NARRATION_TOKEN_LIMIT} token; tổng dialogue mục tiêu {DIALOGUE_TARGET_TOKENS}, tối đa {DIALOGUE_TOKEN_LIMIT} token. Viết câu trọn vẹn, không cắt bằng dấu ba chấm. suggestions là mảng rỗng trừ khi NPC thật sự đưa đề nghị mới, cụ thể, đúng nhịp cảnh.
Khi đưa một người vào/rời cảnh, xác nhận hành động ngay trong narration bằng tên riêng,
ví dụ 'Tên nhân vật bước vào phòng', và trả presence_updates có npc_id, action, reason,
evidence trích nguyên câu đó. Không tạo hiện diện qua nhắc tên, thoại, hồi ức hoặc người
ở cảnh khác. Tra public_character_identities cho mọi người được giới thiệu; không đổi
chức phận, quan hệ cha-con để thuận lời kể. Nhân vật có mặt không bắt buộc nói ngay.
Schema: {{"segments": [{{"type": "narration|dialogue", "text": "..."}}], "suggestions": [], "presence_updates": [{{"npc_id":"id", "action":"enter|leave", "reason":"...", "evidence":"câu narration"}}]}}
"""
    user_prompt = f"Diễn biến phòng chat gần đây:\n{recent_history}\n\nĐến lượt {npc_name} lên tiếng (thể hiện thái độ: {attitude}):"

    try:
        parsed = request_llm_json(
            model=MODEL_ROLEPLAY,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}],
            purpose=f"NPC {npc_name}"
        )
        return normalize_npc_response(parsed)
    except ProviderContentBlocked as error:
        raise HTTPException(status_code=422, detail=error.public_message,
                            headers={'X-Playroom-Error': error.code}) from error
    except Exception as e:
        print(f"[Error] Lỗi khi gọi LLM cho NPC {npc_name}: {e}")
        raise HTTPException(status_code=503, detail="Dịch vụ AI chưa trả lời được; lượt này chưa được lưu. Vui lòng thử lại.") from e

def guard_npc_response(npc_id: str, draft: Dict[str, str]) -> Dict[str, str]:
    """Kiểm tra và sửa vi phạm OOC/agency/knowledge trước khi lưu lịch sử."""
    current_key = get_gemini_api_key()
    if not ENABLE_OOC_GUARD or not client or not current_key or current_key == "YOUR_API_KEY":
        return draft

    npc = ROOM_STATE.get("roster", {}).get(npc_id, {})
    canon = get_canon_character(npc_id)
    state = get_narrative_state()
    recent_history = get_visible_history(8)
    local_issues = dialogue_contract_issues(npc_id,draft)
    prompt = f"""
Bạn là Canon Guard. Kiểm tra bản nháp thoại của {npc.get('name', npc_id)}.

CANON NHÂN VẬT:
{json.dumps(canon, ensure_ascii=False)}
ĐỊNH DANH CÔNG KHAI CỦA CÁC NHÂN VẬT KHÁC (kiểm tra cả người được giới thiệu):
{json.dumps(public_character_identities(), ensure_ascii=False)}

LUẬT THẾ GIỚI VÀ GIỚI HẠN THỜI ĐIỂM:
{json.dumps(STORY_BIBLE.get('world', {}), ensure_ascii=False)}
{STORY_BIBLE.get('canon_licensed', '')}

LUẬT TOÀN CỤC:
{json.dumps(STORY_BIBLE.get('strict_rules', []), ensure_ascii=False)}

SỰ KIỆN ĐÃ XÁC LẬP (ưu tiên hơn suy đoán hoặc câu sai trong lịch sử):
{json.dumps(state.get('facts', [])[-20:], ensure_ascii=False)}

{IMPROVISATION_CONTRACT}
NPC được bổ sung tiểu sử cá nhân đời thường còn trống, không coi đó là vi phạm canon. Không biến suy đoán về người khác thành sự thật. Không phủ nhận kết quả đã xác lập hoặc hồi sinh xung đột đã khép.

NHỮNG GÌ NHÂN VẬT ĐƯỢC BIẾT:
{json.dumps(state.get('knowledge', {}).get(npc_id, []), ensure_ascii=False)}

LỊCH SỬ GẦN:
{json.dumps(recent_history, ensure_ascii=False)}

BẢN NHÁP:
{json.dumps(draft, ensure_ascii=False)}
LỖI ĐÃ ĐƯỢC CODE PHÁT HIỆN (phải sửa dù bản nháp JSON hợp lệ):
{json.dumps(local_issues)}
Kiểm tra ai sở hữu từng lời thoại. Không để Player nói qua nhãn NPC. Câu tả động tác
không phải lời nói; chuyển nó sang narration. Không chỉ xác nhận valid rồi giữ nguyên.

CHỈ THỊ [[...]] CỦA LƯỢT NÀY:
{json.dumps(current_turn_directives(), ensure_ascii=False)}

Sửa tối thiểu nếu bản nháp: viết thay Player; đọc suy nghĩ; lộ bí mật chưa mở; vi phạm giọng nói/tính cách; yêu/ghen/tỏ tình quá nhanh; đổi phe vô lý; bỏ qua chỉ thị khả thi. Chỉ thị đạo diễn phải được thể hiện ngay trong cảnh khi khả thi, không hạ xuống thành lời hứa hoặc hỏi lại. Không cho nó viết thay sự đồng thuận của Player hay phủ nhận sự kiện đã chốt. Bảo vệ quyền tự do Player: không ép Player về arc, tình cảm hay lựa chọn A/B. Kiểm tra continuity: không lặp xung đột đã khép. Giữ cá tính, hài hước, góc cạnh và nhịp câu của bản nháp; đừng 'sửa an toàn' thành giọng chung chung. Narration ngôi thứ ba, tối đa {NARRATION_TOKEN_LIMIT} token; dialogue tối đa {DIALOGUE_TOKEN_LIMIT} token. Giữ nguyên nếu hợp lệ.
Trả JSON: {{"valid": true|false, "segments": [{{"type": "narration|dialogue", "text": "..."}}], "suggestions": ["chỉ đề nghị mới, cụ thể; mảng rỗng nếu không có"], "violations": ["..."]}}.
"""
    try:
        parsed = request_llm_json(
            model=MODEL_ROLEPLAY,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Kiểm tra bản nháp và trả JSON theo schema đã yêu cầu."}
            ],
            purpose="canon guard"
        )
        if parsed.get("valid") is True and not local_issues:
            return draft
        if parsed.get("segments"):
            parsed["suggestions"] = parsed.get("suggestions", draft.get("suggestions", []))
            return normalize_npc_response(parsed)
        return draft
    except Exception as e:
        print(f"[Warning] Canon Guard bỏ qua do lỗi: {e}")
        return draft

def apply_personal_lore(updates, npc_responses):
    """Optional self-report memory. Invalid/missing metadata never blocks a turn."""
    if not isinstance(updates, dict):
        return
    speakers = {item.get('speaker_id') for item in npc_responses if item.get('dialogue') or
                any(s.get('type') == 'dialogue' for s in item.get('segments', []) if isinstance(s,dict))}
    allowed = {'family', 'education', 'past_occupation', 'preferences', 'habits', 'skills'}
    for npc_id, details in list(updates.items())[:4]:
        npc = ROOM_STATE.get('roster', {}).get(npc_id)
        if npc_id not in speakers or not npc or not isinstance(details, dict):
            continue
        canon = get_canon_character(npc_id)
        lore = npc.setdefault('personal_lore', {})
        for key,value in list(details.items())[:6]:
            if key not in allowed or key in canon or key in lore or not isinstance(value, str) or not 0 < len(value.strip()) <= 400:
                continue
            if key == 'family' and (npc_id in STORY_BIBLE.get('characters', {}) or STORY_BIBLE.get('canon_licensed')):
                continue  # Main/canon genealogy is not generated by this optional memory path.
            lore[key] = value.strip()


def update_narrative_state(player_message: str, npc_responses: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Cập nhật state bằng delta nhỏ; gate arc vẫn được tính lại bằng code."""
    tracker_snapshot = copy.deepcopy(ROOM_STATE) if blueprint_runtime.lifecycle_enabled(STORY_BIBLE) else None
    state = get_narrative_state()
    pacing = state["scene_pacing"]
    pacing["turn_number"] += 1
    # Tracker chỉ cần sổ cái xung đột và facts gần; không nhồi transcript cũ vào ngữ cảnh.
    responding_ids = {item.get('speaker_id') for item in npc_responses if item.get('speaker_id')}
    tracker_state = {key: state.get(key) for key in
        ('active_arc', 'unlocked_arcs', 'completed_arcs', 'current_scene', 'location', 'world_clocks', 'player_growth')}
    tracker_state.update(
        facts=relevant_items(state.get('facts', []), player_message, 10, 4),
        conflicts=get_conflict_context(state),
        suggestion_ledger=get_suggestion_context(state),
        relationships={npc_id: {key:value for key,value in state.get('relationships', {}).get(npc_id, {}).items()
                               if key in {'mode','metrics','metric_labels','boundary','title'}} for npc_id in responding_ids},
        temporary_npcs=[{'id':npc_id, 'name':npc.get('name'), 'role':npc.get('role')}
                        for npc_id,npc in ROOM_STATE.get('roster', {}).items() if npc.get('temporary')],
        personal_lore={npc_id:ROOM_STATE.get('roster', {}).get(npc_id, {}).get('personal_lore', {}) for npc_id in responding_ids},
    )
    if canon_runtime.enabled(STORY_BIBLE):
        tracker_state['canon_runtime'] = canon_runtime.project_context({}, ROOM_STATE, STORY_BIBLE,
            load_character_library())['canon_runtime']
    if blueprint_runtime.lifecycle_enabled(STORY_BIBLE):
        tracker_state['setting'] = ROOM_STATE.get('setting','')
        tracker_state['present_ids'] = list(get_active_npcs())
    story_events = load_events(ACTIVE_PLAYROOM_DIR, STORY_BIBLE)
    tracker_state['event_index'] = event_index(story_events, state)
    if ROOM_STATE.get('player', {}).get('identity_mode'):
        tracker_state['player_identity'] = roleplay_sessions.project({}, ROOM_STATE)['player']
    guidance = story_guidance(story_events, STORY_BIBLE, state)
    if guidance:
        tracker_state['story_plan'] = {key:guidance[key] for key in ('macro_arc','current_thread','threads')}
    family_drama_runtime.protect_continuity(tracker_state, state)
    offered_id = state.get('story_director', {}).get('offered_event_id')
    tracker_state['offered_event'] = next((event for event in story_events if event['id']==offered_id), None)
    # Bounded public evidence lets old saves reconcile already-played beats.
    recent_public = get_visible_history(12)
    boundary = max((i for i, item in enumerate(recent_public) if item.get('is_player')), default=0)
    prior_slice = recent_public[:boundary][-6:]
    if prior_slice:
        tracker_state['recent_context_before_turn'] = compact_history(prior_slice)
    checkpoint = get_checkpoint_context(state)
    past_evidence = reconciliation_context(story_events, state, [
        checkpoint.get('known_outcomes', []), checkpoint.get('summary', ''),
        LONG_TERM_MEMORY.get('known_outcomes', []), LONG_TERM_MEMORY.get('facts', []),
        LONG_TERM_MEMORY.get('milestones', []),
        compact_json(compact_history(recent_public[:boundary][-4:]))], max_bytes=6000)
    tracker_state['event_reconciliation_memory'] = past_evidence if story_events else ''
    compact_responses = compact_history(npc_responses)
    # Keep every current response: dropping a block would lose relationship evidence.
    tracker_payload = {'state': tracker_state, 'history': compact_responses}
    current_key = get_gemini_api_key()
    if not ENABLE_STATE_TRACKER or not client or not current_key or current_key == "YOUR_API_KEY":
        recalculate_arc_unlocks()
        return {}
    relationship_events: Dict[str, Dict[str, Any]] = {}

    tracker_prompt = f"""
Bạn là Narrative State Tracker, không phải người kể chuyện.
CHẾ ĐỘ: {STORY_BIBLE.get('session_mode', 'playroom')}
HỢP ĐỒNG TƯƠNG TÁC: {STORY_BIBLE.get('interaction_contract', '')}
Trong companion, không tạo arc, clock, nhiệm vụ hoặc tương lai; chỉ ghi lại sự kiện đã xảy ra. Persona: {json.dumps(ROOM_STATE.get('player', {}).get('persona', ''), ensure_ascii=False)}
Chỉ ghi nhận điều THỰC SỰ xảy ra trong lượt này; không tự tạo twist, bí mật, tình cảm hay milestone. Giữ sự kiện đã xác lập; nếu lời thoại mới mâu thuẫn, không biến nó thành fact. Tách điều nhân vật tin với sự thật đã kiểm chứng.
NPC được ứng tác chi tiết cá nhân hợp vai chưa có trong canon. Nếu NPC đã thật sự kể gia cảnh, học vấn,
nghề cũ, sở thích, thói quen hay kỹ năng thường, lưu vào personal_lore_updates dưới dạng tóm tắt lời tự thuật.
Không sáng tác thêm khi lưu; không lưu lời né tránh, lời hứa sẽ khai, suy đoán về người khác hay suy nghĩ riêng Player.
Không biến tiểu sử tự thuật thành bằng chứng khách quan, thân phận bí mật hoặc milestone.
Chỉ thị [[...]] của lượt này: {json.dumps(current_turn_directives(), ensure_ascii=False)}. Ghi thay đổi đã thật sự được thể hiện trong phản hồi NPC, kể cả do chỉ thị đạo diễn gây ra; đừng chỉ lưu lời hứa sẽ thực hiện. Không ghi chỉ thị chưa được diễn thành sự kiện đã hoàn tất.

ARC CANON:
{compact_json({key: {'title':value.get('title', ''), 'gates':value.get('gates', {})}
    for key,value in STORY_BIBLE.get('arcs', {}).items() if key in state.get('unlocked_arcs', [])})}
STATE HIỆN TẠI:
{compact_json(tracker_payload['state'])}
TIN NHẮN PLAYER:
{player_message}
PHẢN HỒI NPC:
{compact_json(tracker_payload['history'])}

Trả JSON với các trường tùy chọn:
{{
  "personal_lore_updates": {{"npc_id_vừa_nói": {{"family": "chi tiết gia cảnh nếu đã kể", "education": "việc học nếu đã kể"}}}},
  "event_request": {{"event_id":"ID ready trong event_index", "timing":"next_turn", "reason":"vì sao đúng nhịp"}},
  "event_updates": [{{"event_id":"ID trong event_index/offered_event", "status":"planted|active|completed|superseded|deferred", "evidence":"trích nguyên văn hành động/lời nói công khai", "reconcile_existing":false}}],
  "active_arc": "chỉ một arc đã mở",
  "current_scene": "slug ngắn",
  "location": "slug ngắn",
  "new_facts": ["tối đa 2 thay đổi bền vững, đã được xác nhận"],
  "new_milestones": ["chỉ milestone rõ ràng đã đạt"],
  "new_truth_keys": ["chỉ key đã được chứng minh"],
  "resolved_relationships": ["npc_id chỉ khi quan hệ đã được nói rõ"],
  "clock_deltas": {{"clock": -1|0|1}},
  "growth_deltas": {{"trait": 0|1}},
  "relationship_score_deltas": {{"npc_id": {{"mode": "chỉ đổi khi Player xác lập rõ potential_romance|friendship|rivalry", "mode_reason": "bằng chứng trực tiếp nếu đổi mode", "metrics": {{"metric_key_hợp_lệ": -15..15}}, "reason": "một câu nêu chi tiết đối đáp gây thay đổi"}}}},
  "scene_progress": "advance|resolve|aftermath|hold",
  "progress_reason": "thay đổi thực sự đã xảy ra, không phải chỉ lời đe dọa hay tư thế",
  "conflict_updates": [
    {{"id": "slug_da_co_hoac_moi", "status": "active|resolved|superseded|dormant", "summary": "một câu cụ thể", "target": "ai/bộ phận liên quan", "evidence": "căn cứ mới nếu có", "change_reason": "hành động hoặc sự kiện khiến trạng thái đổi"}}
  ],
  "suggestion_updates": [
    {{"id": "số id trong suggestion_ledger", "status": "accepted|declined|deferred|expired", "reason": "Player đã phản hồi thế nào"}}
  ],
  "npc_lifecycle": [
    {{"action": "add", "id": "slug_khong_trung_nhan_vat_chinh", "name": "tên riêng bắt buộc", "role": "chức phận cụ thể", "personality": "một nét riêng dễ nhận", "tone": "nhịp nói và cách xưng hô", "scene_function": "lý do nhân vật cần xuất hiện", "relationship": "liên hệ ngắn với người trong cảnh"}},
    {{"action": "deactivate|retire", "id": "npc phụ đã có", "reason": "vai trò trong cảnh đã kết thúc"}}
  ]
}}
QUY TẮC XUNG ĐỘT: Một nghi vấn phải được quản lý bằng conflict_updates. Khi đã có bằng chứng minh oan/khép án/đạt thỏa thuận, đánh dấu resolved hoặc superseded. Xung đột resolved/superseded không được mở lại với cùng ID; nếu có chứng cứ mới thì tạo xung đột mới, nêu rõ chứng cứ đó. Không coi lời nói đơn lẻ là tình yêu, sự thật hoặc hoàn thành arc.
ĐIỀU PHỐI SỰ KIỆN: event_request chỉ là yêu cầu lấy kịch bản cho lượt SAU, không phải fact.
Chỉ yêu cầu khi cuộc thoại có điểm nghỉ/chuyển cảnh tự nhiên hoặc Player muốn tiến truyện; không mở drama
chỉ vì đủ lượt hoặc vì Player nói ngắn. Khi chưa đúng lúc, bỏ event_request. Không lặp sự kiện bị bỏ qua.
Mỗi lượt đối chiếu event_index/review_context với event_reconciliation_memory để cập nhật nhịp cũ
đã thật sự diễn; không bỏ qua việc đối chiếu chỉ vì lượt mới đang là sinh hoạt thường ngày.
Khi khép nhịp bằng evidence hợp lệ, được event_request nhịp kế tiếp nếu requires sẽ đủ sau cập nhật.
offered_event là tư liệu dự kiến đã cấp cho cảnh, không phải kết quả. Chỉ cập nhật planted/active/completed
khi phản hồi CÔNG KHAI đã thể hiện đúng nội dung và có evidence nguyên văn; lời hứa chưa phải hoàn thành.
Đánh dấu deferred nếu Player không theo nhịp; superseded chỉ khi hành động thực tế làm nhịp không còn
phù hợp hoặc lịch sử đã xác nhận nó qua rồi. Không tự bỏ các bước để ép mở bước sau. Nếu tiến độ save cũ
chưa rõ, giữ thận trọng, không diễn lại cảnh mở đã có trong ký ức. Companion không dùng các trường sự kiện.
Với save cũ, có thể đánh dấu nhịp đã qua bằng reconcile_existing=true và evidence trích NGUYÊN VĂN
event_reconciliation_memory; phải thật sự khớp nội dung nhịp, không suy từ tên arc hay từ dàn ý.
CHỐNG VÒNG LẶP: Không thêm fact cho các hành động lặp, lời thách thức, vòng vây hay áp lực đã tồn tại nếu không có thay đổi bền vững. Khi một xung đột đã đóng, hãy ghi aftermath thay vì lặp lại lời buộc tội.
GỢI Ý: Xem suggestion_ledger. Nếu Player đã đổi chủ đề/hành động theo hướng khác mà không nhận đề nghị cũ, đánh dấu deferred; nếu từ chối rõ, đánh dấu declined; nếu đã làm theo, đánh dấu accepted. Không tạo trạng thái chỉ vì Player chưa trả lời.
NPC PHỤ: Chỉ thêm khi cảnh thật sự cần người hầu, cận vệ, quan viên, chủ quán, sứ giả... Mỗi NPC phải có TÊN RIÊNG, chức phận cụ thể, một nét tính cách phân biệt và giọng nói riêng; không dùng nhãn vô danh như “người hầu”. Không cho NPC phụ loveline hoặc bí mật cấp cốt truyện. Khi chức năng cảnh kết thúc, trả deactivate/retire. Không xóa hoặc cho nghỉ NPC đang giữ bằng chứng, lời hứa hay xung đột mở.
CHẤM ĐIỂM QUAN HỆ:
PERSONAL LORE: Chỉ gửi mục thật sự vừa được NPC kể; không điền đủ cho đẹp mẫu. Key hợp lệ: family, education, past_occupation, preferences, habits, skills. Tối đa 400 ký tự/mục; không sửa mục đã lưu hoặc gia phả canon.
- Chỉ chấm cho NPC thực sự vừa đối đáp trong PHẢN HỒI NPC. Dùng đúng metric key hiện có trong STATE của NPC đó; mỗi nhân vật có bộ chỉ số riêng.
- Mức thường là ±1..5; ±6..10 cho khoảnh khắc quan trọng; ±11..15 chỉ cho bước ngoặt hiếm. Không cộng điểm chỉ vì Player gửi tin hoặc vì NPC vốn có loveline.
- Cả chỉ số tích cực và nhạy cảm đều phản ánh tâm lý: `kiem_soat`/`chiem_huu` tăng không đồng nghĩa quan hệ tốt. Trừ điểm khi đối đáp làm suy giảm thuộc tính tương ứng.
- `reason` phải chỉ ra câu nói/hành động cụ thể. Nếu không có thay đổi có ý nghĩa, bỏ NPC khỏi relationship_score_deltas.
- Không tự chuyển sang friendship/loveline/rivalry. Chỉ đổi mode khi Player xác lập ranh giới hoặc tình cảm rõ ràng trong chính lượt này và phải nêu mode_reason.
"""
    try:
        if canon_runtime.enabled(STORY_BIBLE):
            tracker_prompt = canon_runtime.tracker_prompt(
                (BASE_DIR / 'runtime_prompts' / 'canon_tracker.md').read_text(encoding='utf-8'),
                tracker_state, player_message, compact_responses, current_turn_directives(), INPUT_BYTE_LIMITS['state tracker'])
        elif blueprint_runtime.enabled(STORY_BIBLE):
            tracker_prompt=pack_tracker_prompt(blueprint_runtime.tracker_core(BASE_DIR),tracker_state,
                player_message,compact_responses,current_turn_directives(),INPUT_BYTE_LIMITS['state tracker'])
        else:
            # Legacy rooms use the same bounded payload contract; never repeat
            # state/responses inside both prose and JSON to fit old-save recall.
            core = tracker_prompt.split('ARC CANON:', 1)[0] + 'Trả JSON với các trường tùy chọn:' + tracker_prompt.split(
                'Trả JSON với các trường tùy chọn:', 1)[1]
            tracker_state['arc_catalog'] = {key:{'title':value.get('title',''), 'gates':value.get('gates',{})}
                for key,value in STORY_BIBLE.get('arcs',{}).items() if key in state.get('unlocked_arcs',[])}
            tracker_prompt=pack_tracker_prompt(core,tracker_state,player_message,compact_responses,
                current_turn_directives(),INPUT_BYTE_LIMITS['state tracker'])
        if blueprint_runtime.lifecycle_enabled(STORY_BIBLE):
            state['tracker_sync'] = {'status':'pending','turn':pacing['turn_number']}
        delta = request_llm_json(
            model=MODEL_ARBITER,
            messages=[
                {"role": "system", "content": tracker_prompt},
                {"role": "user", "content": "Ghi nhận delta trạng thái của lượt vừa hoàn tất dưới dạng JSON."}
            ],
            purpose="state tracker"
        )
        # Apply against the same index used to ask the tracker, before arc changes.
        # Private thoughts/directives are not event-completion evidence.
        public_responses = '\n'.join(s.get('text', '') for item in npc_responses
            for s in item.get('segments', []) if s.get('type') in {'narration','dialogue'})
        latest_player = next((item for item in reversed(ROOM_STATE.get('history', [])) if item.get('is_player')), {})
        player_evidence = '\n'.join(s.get('text', '') for s in latest_player.get('segments', [])
            if s.get('type') in {'action','speech'})
        apply_event_delta(story_events, state, delta, public_responses + '\n' + player_evidence, ROOM_STATE, past_evidence)
        if canon_runtime.enabled(STORY_BIBLE):
            issues = canon_runtime.apply_updates(state, delta.get('domain_updates', {}), ROOM_STATE,
                load_character_library(), public_responses + '\n' + player_evidence,
                allowed_teams=set(STORY_BIBLE.get('runtime_rules', {}).get('team_aliases', {})))
            state['domain_validation'] = {'rejected':issues[:16], 'turn':pacing['turn_number']}
        unlocked = set(state.get("unlocked_arcs", []))
        apply_personal_lore(delta.get('personal_lore_updates', {}), npc_responses)
        proposed_arc = delta.get("active_arc")
        if proposed_arc in unlocked and proposed_arc not in state.get('completed_arcs', []):
            state["active_arc"] = proposed_arc
        if state.get('established_continuity', {}).get('anniversary_closed'):
            # The writer's validated scene transition owns location. A tracker
            # summary cannot move the story back to an earlier ceremony.
            state['current_scene'] = state['location'] = ROOM_STATE.get('setting', '')
        else:
            for field in ("current_scene", "location"):
                if isinstance(delta.get(field), str) and 0 < len(delta[field]) <= 80:
                    state[field] = delta[field]

        for key, target_field in (("new_facts", "facts"), ("new_milestones", "milestones"),
                                  ("new_truth_keys", "truth_keys"), ("resolved_relationships", "resolved_relationships")):
            existing = state.setdefault(target_field, [])
            for item in delta.get(key, [])[:2]:
                if isinstance(item, str) and item not in existing:
                    if key == "new_truth_keys" and item not in STORY_BIBLE.get("world", {}).get("truth_keys", []):
                        continue
                    if key == "resolved_relationships" and item not in ROOM_STATE.get("roster", {}):
                        continue
                    existing.append(item)

        # Facts là nhật ký bền vững, không phải transcript vô hạn. Bộ nhớ dài hạn giữ bối cảnh cũ.
        state["facts"] = state.get("facts", [])[-160:]
        conflict_changed = apply_conflict_updates(state, delta.get("conflict_updates", []))
        apply_suggestion_updates(state, delta.get("suggestion_updates", []))
        npc_lifecycle_changed = apply_temporary_npc_lifecycle(state, delta.get("npc_lifecycle", []))
        progress = str(delta.get("scene_progress", "hold")).lower()
        if progress in {"advance", "resolve", "aftermath"} or conflict_changed or npc_lifecycle_changed:
            pacing["turns_without_progress"] = 0
            pacing["last_progress_turn"] = pacing["turn_number"]
        else:
            pacing["turns_without_progress"] += 1
        reason = str(delta.get("progress_reason", "")).strip()
        if reason and progress != "hold":
            hooks = pacing.setdefault("recent_hooks", [])
            if reason not in hooks:
                hooks.append(reason)
            pacing["recent_hooks"] = hooks[-8:]

        clocks = state.setdefault("world_clocks", {})
        for key, change in delta.get("clock_deltas", {}).items():
            if key in clocks:
                clocks[key] = clamp_int(clocks[key] + clamp_int(change, -1, 1), 0, 8)

        growth = state.setdefault("player_growth", {})
        for key, change in delta.get("growth_deltas", {}).items():
            if key in growth:
                growth[key] = clamp_int(growth[key] + clamp_int(change, 0, 1), 0, 5)

        responding_ids = {str(item.get("speaker_id", "")) for item in npc_responses if item.get("speaker_id")}
        relationship_events = apply_relationship_score_deltas(
            state,
            delta.get("relationship_score_deltas", {}),
            responding_ids
        )
        if blueprint_runtime.lifecycle_enabled(STORY_BIBLE):
            state['tracker_sync'] = {'status':'completed','turn':pacing['turn_number']}
    except Exception as e:
        if blueprint_runtime.lifecycle_enabled(STORY_BIBLE):
            # Roll back ALL partial tracker changes, not the validated scene/history.
            ROOM_STATE.clear()
            ROOM_STATE.update(tracker_snapshot)
            state = get_narrative_state()
            pacing = state['scene_pacing']
            pacing['turn_number'] += 1
            state['tracker_sync'] = {'status':'pending','turn':pacing['turn_number'],'error_type':type(e).__name__}
        print(f"[Warning] State Tracker bỏ qua do lỗi: {e}")
    finally:
        recalculate_arc_unlocks()
    return relationship_events

# --- API Endpoints ---

@app.get("/api/avatars")
def list_available_avatars():
    """Lấy danh sách các tệp ảnh đại diện có trong thư mục avatar."""
    if not AVATAR_DIR.exists():
        return []
    allowed_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    return [
        f.name for f in sorted(AVATAR_DIR.iterdir())
        if f.suffix.lower() in allowed_exts and f.name.lower() != "logo.png"
    ]

@app.get("/api/character-library")
def get_character_library():
    library = load_character_library()
    roster = ROOM_STATE.get("roster", {})
    return {"characters": [
        {"id": npc_id, "name": canon.get("name", npc_id),
         "role": canon.get("role", canon.get("guild", "")),
         "guild": canon.get("guild", ""), "magic": canon.get("magic", ""),
         "personality": canon.get("personality", ""),
         "imported": npc_id in roster, "active": bool(roster.get(npc_id, {}).get("is_active"))}
        for npc_id, canon in library.items()
    ]}

@app.post("/api/character-library/{npc_id}/import")
def import_character_from_library(npc_id: str):
    guard_player_identity(npc_id)
    library = load_character_library()
    if npc_id not in library:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhân vật trong thư viện phòng")
    roster = ROOM_STATE.setdefault("roster", {})
    if npc_id not in roster:
        roster[npc_id] = library_roster_entry(npc_id, library[npc_id])
        save_room_state(ROOM_STATE)
    return {"character": roster[npc_id]}

@app.get("/api/state")
def get_state():
    """Lấy toàn bộ trạng thái hiện tại của phòng chơi kèm avatar được phân giải."""
    ensure_narrative_mechanics(ROOM_STATE.setdefault("narrative_state", {}))
    # Resolve newly supplied art as well as empty avatars; preserve custom choices.
    for k, v in ROOM_STATE.get("roster", {}).items():
        if isinstance(v, dict):
            v["avatar"] = get_avatar_url(v, k)
    if "player" in ROOM_STATE and isinstance(ROOM_STATE["player"], dict) and not ROOM_STATE["player"].get("avatar"):
        ROOM_STATE["player"]["avatar"] = get_avatar_url(ROOM_STATE["player"], "player")
    narrator_avatar = get_avatar_url({"name": "Người kể chuyện"}, "__narrator__")
    if narrator_avatar:
        for item in ROOM_STATE.get("history", []):
            if isinstance(item, dict) and item.get("speaker_id") == "__narrator__":
                item["avatar"] = narrator_avatar
    # Correct displayed roles from authored canon without editing the live save.
    roster = {key:{**npc, 'role':room_character_role(key,npc)}
              for key,npc in ROOM_STATE.get('roster',{}).items()}
    events = load_events(ACTIVE_PLAYROOM_DIR, STORY_BIBLE)
    narrative_state = ROOM_STATE.get("narrative_state", {})
    pool = eligible_tiered_events(events, narrative_state)
    eligible_ids = {e["id"] for e in pool}
    director = narrative_state.get("story_director", {}) if isinstance(narrative_state, dict) else {}
    cached_suggestions = [
        item for item in director.get("last_suggestions", [])
        if isinstance(item, dict) and item.get("event_id") in eligible_ids
    ] if isinstance(director, dict) else []
    remaining_counts = {t: sum(1 for e in pool if normalize_tier(e.get("tier")) == t) for t in ("small", "medium", "large")}
    pacing = narrative_state.get("scene_pacing", {}) if isinstance(narrative_state, dict) else {}
    return {
        **ROOM_STATE,
        "roster": roster,
        "narrator_avatar": narrator_avatar,
        "avatar_versions": get_avatar_versions(),
        "illustration_status": {
            "turn_number": int(pacing.get("turn_number") or 0),
            "last_illustration_turn": int(pacing.get("last_illustration_turn") or 0),
            "interval": scene_illustrator.ILLUSTRATION_INTERVAL_TURNS,
            "has_apiz_key": bool(llm_config.api_key("apiz")),
        },
        "token_usage": TOKEN_METER.snapshot(),
        "story_event_suggestions": {
            "suggestions": cached_suggestions,
            "remaining_counts": remaining_counts,
            "total_remaining": len(pool),
            "used_model": bool(director.get("last_suggestions_used_model", False)) if isinstance(director, dict) else False,
            "exhausted": bool(STORY_BIBLE.get("story_events")) and len(pool) == 0,
        },
        "arc_suggestions": ordered_arc_roadmap(events, STORY_BIBLE, narrative_state),
    }


@app.get("/api/token-usage")
def get_token_usage():
    return TOKEN_METER.snapshot()

@app.get("/api/story")
def get_story_bible_public():
    """Trả canon để UI/debug hiển thị; secrets vẫn chỉ dùng nội bộ prompt."""
    return {
        "story_id": STORY_BIBLE.get("story_id"),
        "title": STORY_BIBLE.get("title"),
        "genre": STORY_BIBLE.get("genre", []),
        "rating": STORY_BIBLE.get("rating"),
        "premise": STORY_BIBLE.get("premise"),
        "tone": STORY_BIBLE.get("tone"),
        "world": STORY_BIBLE.get("world", {}),
        "arcs": STORY_BIBLE.get("arcs", {}),
        "finale": STORY_BIBLE.get("finale", {}),
        "category": read_manifest(ACTIVE_PLAYROOM_DIR).get("category", "playroom"),
        "session_mode": STORY_BIBLE.get("session_mode", "playroom"),
        "profile": STORY_BIBLE.get("profile", {}),
        "player_persona_guidance": STORY_BIBLE.get("player_persona_guidance", {}),
        "background_before_chat": STORY_BIBLE.get("background_before_chat", []),
        "starting_situation": STORY_BIBLE.get("starting_situation", "")
    }


_CODEX_ENGINE_CACHE: Dict[Tuple[Any, ...], knowledge_codex.CodexSearchEngine] = {}


@app.get("/api/codex")
def get_codex():
    """Trả về toàn bộ bảng tra cứu tri thức (0-cost, đã loại bỏ secrets) để UI index tức thời."""
    return knowledge_codex.get_room_codex(ACTIVE_PLAYROOM_DIR, STORY_BIBLE)


@app.get("/api/codex/search")
def search_codex(q: str = "", categories: str = "", limit: int = 80):
    """Tra cứu nhanh theo inverted-index + BM25F + prefix/fuzzy (không tốn token LLM)."""
    codex = knowledge_codex.get_room_codex(ACTIVE_PLAYROOM_DIR, STORY_BIBLE)
    fp = knowledge_codex._compute_dir_fingerprint(ACTIVE_PLAYROOM_DIR)
    cache_key = (str(ACTIVE_PLAYROOM_DIR.resolve()), fp, codex.get("story_id", ""), codex.get("total_entries", 0))
    engine = _CODEX_ENGINE_CACHE.get(cache_key)
    if engine is None:
        if len(_CODEX_ENGINE_CACHE) >= 16:
            _CODEX_ENGINE_CACHE.clear()
        engine = knowledge_codex.CodexSearchEngine(codex["entries"])
        _CODEX_ENGINE_CACHE[cache_key] = engine
    cat_list = [c.strip() for c in categories.split(",") if c.strip()] if categories else None
    safe_limit = max(1, min(int(limit or 80), 300))
    payload = engine.search(query=q, categories=cat_list, limit=safe_limit)
    payload["categories"] = codex["categories"]
    payload["total_entries"] = codex["total_entries"]
    return payload

@app.get("/api/narrative")
def get_narrative():
    """Trạng thái tiến triển dùng để kiểm tra arc, clock và quan hệ."""
    return get_narrative_state()

@app.get("/api/memory")
def get_memory():
    """Xem bộ nhớ tóm tắt dài hạn đang được dùng làm ngữ cảnh."""
    return LONG_TERM_MEMORY

@app.post("/api/memory/save")
def save_memory_now():
    """Cho phép lưu snapshot thủ công trước khi đóng tab hoặc dừng server."""
    return {"status": "success", "memory": persist_long_term_memory("manual")}

@app.post("/api/state")
def set_full_state(new_state: Dict[str, Any]):
    """Cập nhật state runtime nhưng không cho UI xóa khóa canon/agency."""
    global ROOM_STATE
    if not isinstance(new_state.get("roster"), dict) or not isinstance(new_state.get("player"), dict):
        raise HTTPException(status_code=400, detail="State phải có player và roster hợp lệ")
    if ROOM_STATE.get('session_id'):
        new_state['session_id'] = ROOM_STATE['session_id']
        for key in ('identity_mode','character_id','canon_profile'):
            if key in ROOM_STATE.get('player', {}):
                new_state['player'][key] = copy.deepcopy(ROOM_STATE['player'][key])
            else:
                new_state['player'].pop(key, None)
        if roleplay_sessions.player_id(ROOM_STATE):
            for key in ('name','role'):
                new_state['player'][key] = ROOM_STATE['player'][key]
    new_state["story_id"] = STORY_BIBLE.get("story_id", "xuan-y-kinh-hoa")
    new_state["player"]["agency_locked"] = True
    new_state.setdefault("history", [])
    new_state.setdefault("narrative_state", ROOM_STATE.get("narrative_state", {}))
    for npc_id, npc in new_state["roster"].items():
        if isinstance(npc, dict) and npc_id in STORY_BIBLE.get("characters", {}):
            npc["canon_id"] = npc_id
    ROOM_STATE = new_state
    ensure_narrative_mechanics(ROOM_STATE["narrative_state"])
    recalculate_arc_unlocks()
    save_room_state(ROOM_STATE)
    return {"status": "success", "state": ROOM_STATE}

@app.post("/api/scene")
def update_scene(payload: SceneUpdateRequest):
    """Cập nhật bối cảnh phòng và thêm thông báo sự kiện vào lịch sử."""
    previous_setting = ROOM_STATE.get("setting", "")
    ROOM_STATE["setting"] = payload.setting
    event_entry = {
        "sender": "System",
        "text": f"[BỐI CẢNH THAY ĐỔI]: {payload.setting}"
    }
    ROOM_STATE["history"].append(event_entry)
    presence_events = sync_scene_presence(payload.setting, previous_setting)
    save_room_state(ROOM_STATE)
    return {"status": "success", "event": event_entry, "presence_events": presence_events,
            "active_character_ids": list(get_active_npcs())}

@app.post("/api/roster/toggle")
def toggle_roster(payload: ToggleNPCRequest):
    guard_player_identity(payload.npc_id)
    """Bật/tắt trạng thái hiện diện của nhân vật trong phòng."""
    roster = ROOM_STATE.get("roster", {})
    if payload.npc_id not in roster:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy NPC với ID: {payload.npc_id}")

    npc = roster[payload.npc_id]
    if payload.is_active and get_canon_character(payload.npc_id).get("status") == "deceased":
        raise HTTPException(status_code=400, detail="Nhân vật đã mất không thể có mặt trong cảnh hiện tại")
    set_manual_presence(ROOM_STATE,payload.npc_id,payload.is_active)

    action = "đã bước vào phòng" if payload.is_active else "đã rời khỏi phòng"
    display_name = npc.get("name") or payload.npc_id
    role_info = f" ({npc.get('role')})" if npc.get("role") else ""
    event_entry = {
        "sender": "System",
        "text": f"[THÔNG BÁO]: {display_name}{role_info} {action}."
    }
    ROOM_STATE["history"].append(event_entry)
    save_room_state(ROOM_STATE)
    return {"status": "success", "npc": npc, "event": event_entry}

@app.post("/api/roster/character")
def upsert_character(char: CharacterUpsertRequest):
    guard_player_identity(char.id)
    if roleplay_sessions.owns_reference(ROOM_STATE, char.name):
        raise HTTPException(409, 'Không thể tạo bản sao nhân vật đang do Player điều khiển')
    """Thêm mới hoặc chỉnh sửa thông tin chi tiết một nhân vật NPC."""
    if (STORY_BIBLE.get("session_mode") == "companion" and char.id not in STORY_BIBLE.get("characters", {})
            and not (companion_cast.enabled(STORY_BIBLE) and ROOM_STATE.get('roster',{}).get(char.id,{}).get('supporting'))):
        raise HTTPException(status_code=400, detail="Companion chỉ có một nhân vật chat chính")
    if char.is_active and get_canon_character(char.id).get('status') == 'deceased':
        raise HTTPException(status_code=400, detail='Nhân vật đã mất không thể có mặt trong cảnh hiện tại')
    if "roster" not in ROOM_STATE:
        ROOM_STATE["roster"] = {}

    avatar_val = char.avatar.strip() if char.avatar else ""
    if not avatar_val:
        avatar_val = get_avatar_url({"name": char.name}, char.id)

    existing = ROOM_STATE["roster"].get(char.id, {})
    ROOM_STATE["roster"][char.id] = {
        **existing,
        "id": char.id,
        "canon_id": existing.get("canon_id", char.id if char.id in STORY_BIBLE.get("characters", {}) else ""),
        "name": char.name,
        "role": char.role,
        "personality": char.personality,
        "tone": char.tone,
        "relationship": char.relationship or "",
        "can_loveline": bool(char.can_loveline),
        "loveline_hint": char.loveline_hint or "",
        "is_active": char.is_active,
        "avatar": avatar_val
    }
    if not existing or bool(existing.get('is_active')) != char.is_active:
        set_manual_presence(ROOM_STATE,char.id,char.is_active)
    save_room_state(ROOM_STATE)
    return {"status": "success", "character": ROOM_STATE["roster"][char.id]}

@app.post("/api/player")
def update_player(payload: PlayerUpdateRequest):
    current = ROOM_STATE.get('player', {})
    if current.get('identity_mode') == 'canon' and (payload.name != current.get('name') or payload.role != current.get('role')):
        raise HTTPException(409, 'Vai canon được chọn khi tạo phiên; hãy mở phiên mới để đổi danh tính')
    """Cập nhật thông tin nhân vật của người chơi."""
    if "player" not in ROOM_STATE:
        ROOM_STATE["player"] = {}
    ROOM_STATE["player"]["name"] = payload.name
    ROOM_STATE["player"]["role"] = payload.role
    if payload.persona is not None:
        ROOM_STATE["player"]["persona"] = payload.persona
    if payload.gender is not None:
        ROOM_STATE["player"]["gender"] = payload.gender
    avatar_val = payload.avatar.strip() if payload.avatar else ""
    if not avatar_val:
        avatar_val = get_avatar_url(ROOM_STATE["player"], "player")
    ROOM_STATE["player"]["avatar"] = avatar_val
    save_room_state(ROOM_STATE)
    return {"status": "success", "player": ROOM_STATE["player"]}

@app.post("/api/persona")
def update_persona(payload: PlayerPersonaRequest):
    ROOM_STATE.setdefault("player", {})["persona"] = payload.persona
    save_room_state(ROOM_STATE)
    return {"player": ROOM_STATE["player"]}

@app.post("/api/companion/start")
def start_companion(payload: PlayerPersonaRequest):
    if STORY_BIBLE.get("session_mode") != "companion":
        raise HTTPException(status_code=400, detail="Phòng này không phải companion")
    ROOM_STATE.setdefault("player", {})["persona"] = payload.persona
    if not ROOM_STATE.get("session_started"):
        opening = copy.deepcopy(STORY_BIBLE.get("opening_message", {}))
        if not opening.get("speaker_id") or not opening.get("segments"):
            raise HTTPException(status_code=400, detail="Nhân vật chưa có tình huống mở đầu")
        ROOM_STATE.setdefault("history", []).append(opening)
        ROOM_STATE["session_started"] = True
    save_room_state(ROOM_STATE)
    return {"play_url": f"/play/{ACTIVE_PLAYROOM_ID}", "session_started": True}

@app.post("/api/turn")
def execute_turn(req: TurnRequest):
    """Xử lý một lượt gửi tin nhắn từ người chơi (Speech, Thought, Command, Mention)."""
    if STORY_BIBLE.get("session_mode") == "companion" and not ROOM_STATE.get("session_started"):
        raise HTTPException(status_code=409, detail="Mở hồ sơ nhân vật và bắt đầu trò chuyện trước")
    if not client or not get_gemini_api_key():
        raise HTTPException(status_code=503, detail="Chưa cấu hình API key cho provider hiện tại. Mở /settings để xem hướng dẫn; tin nhắn chưa được lưu.")
    if STORY_BIBLE.get("canon_licensed"):
        key = get_gemini_api_key()
        if not client or not key or key == "YOUR_API_KEY":
            raise HTTPException(status_code=503, detail="Chưa cấu hình kết nối AI; tin nhắn chưa được lưu")
    turn_snapshot = copy.deepcopy(ROOM_STATE)
    repair_manual_membership(ROOM_STATE)
    if STORY_BIBLE.get("session_mode") == "companion":
        key = get_gemini_api_key()
        if not client or not key or key == "YOUR_API_KEY":
            raise HTTPException(status_code=503, detail="Chưa cấu hình kết nối AI; tin nhắn chưa được lưu")
    scene_mode = SCENE_MODE or blueprint_runtime.lifecycle_enabled(STORY_BIBLE) or knowledge_scope.enabled(STORY_BIBLE)
    # 1. Định dạng tin nhắn người chơi theo Intent
    parsed_mixed = parse_mixed_player_input(req.content) if req.intent == "mixed" else None
    formatted_msg = format_mixed_turn(parsed_mixed) if parsed_mixed else format_turn_payload(req.intent, req.content, req.target_id)
    effective_target = req.target_id
    if parsed_mixed and parsed_mixed.get("mention_ids"):
        effective_target = parsed_mixed["mention_ids"][0]
    player_name = ROOM_STATE.get("player", {}).get("name") or "Người chơi"
    player_avatar = get_avatar_url(ROOM_STATE.get("player"), "player")

    player_entry = {
        "sender": player_name,
        "text": formatted_msg,
        "raw_content": req.content if req.intent == "command" else "",
        "intent": req.intent,
        "segments": parsed_mixed.get("segments", []) if parsed_mixed else [],
        "mention_ids": parsed_mixed.get("mention_ids", []) if parsed_mixed else [],
        "is_player": True,
        "avatar": player_avatar
    }
    if knowledge_scope.enabled(STORY_BIBLE):
        player_entry['_perception_start'] = {
            'observers':sorted(knowledge_scope.present_ids(turn_snapshot)),
            'setting':turn_snapshot.get('setting'), 'target':effective_target}
    ROOM_STATE["history"].append(player_entry)
    if not effective_target:
        addressed = player_addressed_ids()
        if len(addressed) == 1:
            effective_target = next(iter(addressed))
    if '_perception_start' in player_entry:
        player_entry['_perception_start']['target'] = effective_target
    turn_presence_events = reconcile_recent_scene_presence()
    get_narrative_state().setdefault("presence",{})["last_events"] = []
    # Bất kỳ lượt mới nào của Player đều khóa việc lặp lại ngay gợi ý trước đó.
    mark_suggestions_seen_by_player(get_narrative_state())

    # Scene mode writes the entire exchange in one call. Legacy path remains opt-in.
    public_text = visible_mixed_turn(parsed_mixed) if parsed_mixed else visible_message_for_npcs(req.intent, formatted_msg)
    scene_event_start = len(ROOM_STATE['history'])
    try:
        if scene_mode:
            decisions, scene_events = generate_scene_response(effective_target, public_text)
            turn_presence_events.extend(scene_events)
        else:
            decisions = arbitrate_speakers(req.intent, effective_target, public_text)
    except Exception as error:
        ROOM_STATE.clear()
        ROOM_STATE.update(turn_snapshot)
        if isinstance(error, ProviderContentBlocked):
            raise HTTPException(status_code=422, detail=error.public_message,
                                headers={'X-Playroom-Error': error.code}) from error
        raise HTTPException(status_code=503, detail=f'Không hoàn tất cảnh; lượt chưa được lưu: {error}') from error
    sorted_decisions = sorted(decisions, key=lambda d: d.get("order", 1))
    deferred_departures = []
    if blueprint_runtime.lifecycle_enabled(STORY_BIBLE):
        # Validation has committed presence already. Persist departures AFTER
        # the farewell/prose so next-turn recovery cannot reopen an older arrival.
        tail=ROOM_STATE['history'][scene_event_start:]
        deferred_departures=[entry for entry in tail if entry.get('presence_event',{}).get('action')=='leave']
        ROOM_STATE['history'][scene_event_start:]=[entry for entry in tail
            if entry.get('presence_event',{}).get('action')!='leave']
    turn_presence_events.extend(get_narrative_state().get("presence",{}).get("last_events",[]))

    responses = []
    # 3. Tuần tự gọi từng NPC phát biểu
    for dec in sorted_decisions:
        spk_id = dec.get("speaker_id")
        if not spk_id:
            continue
        attitude = dec.get("attitude", "")
        try:
            reply = dec['draft'] if scene_mode else generate_npc_response(spk_id, attitude=attitude)
            if not scene_mode and ENABLE_OOC_GUARD and dialogue_contract_issues(spk_id, reply):
                reply = guard_npc_response(spk_id, reply)
            if dec.get("narration_only"):
                reply = normalize_npc_response({
                    "segments": [s for s in reply.get("segments", []) if s.get("type") == "narration"],
                    "narration": reply.get("narration", ""), "dialogue": "",
                    "presence_updates": reply.get("presence_updates", []), "suggestions": []
                })
            reply = enforce_dialogue_contract(spk_id, reply)
        except HTTPException:
            ROOM_STATE.clear()
            ROOM_STATE.update(turn_snapshot)
            raise
        npc = ROOM_STATE.get("roster", {}).get(spk_id, {})
        if npc.get("temporary"):
            npc["last_seen_turn"] = get_narrative_state().get("scene_pacing", {}).get("turn_number", 0)
        name = npc.get("name") or ('Người kể chuyện' if spk_id == '__narrator__' else spk_id)
        avatar = get_avatar_url(npc, spk_id)

        # Ghi ngay vào history để NPC kế tiếp (nếu có) kế thừa ngữ cảnh
        history_item = {
            "sender": name,
            "text": reply["text"],
            "narration": reply["narration"],
            "dialogue": reply["dialogue"],
            "segments": reply["segments"],
            "suggestions": reply["suggestions"],
            "speaker_id": spk_id,
            "order": dec.get("order", 1),
            "attitude": attitude,
            "can_loveline": npc.get("can_loveline", False),
            "is_player": False,
            "avatar": avatar
        }
        if 'perception' in dec:
            history_item['perception'] = copy.deepcopy(dec['perception'])
        ROOM_STATE["history"].append(history_item)
        if not blueprint_runtime.lifecycle_enabled(STORY_BIBLE):
            # Canon scene validation already reconciled the COMPLETE scene once,
            # including departures. Replaying each earlier block reactivates NPCs.
            turn_presence_events.extend(reconcile_narrated_presence(reply))
        responses.append({
            "speaker_id": spk_id,
            "sender": name,
            "text": reply["text"],
            "narration": reply["narration"],
            "dialogue": reply["dialogue"],
            "segments": reply["segments"],
            "suggestions": reply["suggestions"],
            "order": dec.get("order", 1),
            "attitude": attitude,
            "can_loveline": npc.get("can_loveline", False),
            "avatar": avatar
        })

        register_npc_suggestions(get_narrative_state(), spk_id, reply["suggestions"])

    ROOM_STATE['history'].extend(deferred_departures)
    # 4. Cập nhật arc/quan hệ bằng delta nhỏ, sau khi toàn bộ lượt đã hoàn tất.
    relationship_events = update_narrative_state(public_text, responses)
    if companion_cast.enabled(STORY_BIBLE):
        turn_presence_events.extend(companion_cast.finalize(ROOM_STATE,STORY_BIBLE,responses))
    if relationship_events:
        for response in responses:
            event = relationship_events.get(response.get("speaker_id"))
            if event:
                response["relationship_change"] = event
        attached = set()
        for history_item in reversed(ROOM_STATE.get("history", [])):
            speaker_id = history_item.get("speaker_id")
            if speaker_id in relationship_events and speaker_id not in attached:
                history_item["relationship_change"] = relationship_events[speaker_id]
                attached.add(speaker_id)
            if len(attached) == len(relationship_events):
                break

    if blueprint_runtime.lifecycle_enabled(STORY_BIBLE):
        turn_presence_events=finalize_presence_events(ROOM_STATE,turn_snapshot.get('roster',{}),
                                                      len(turn_snapshot.get('history',[])))

    # Nếu Player đã dùng lệnh đạo diễn [[...]] kích hoạt một sự kiện từ story_events
    # hoặc một chặng Arc có [id:...], đánh dấu bước đó đã diễn ra ngay sau khi lượt thành công.
    triggered_event_meta = None
    turn_directives = current_turn_directives()
    if turn_directives:
        all_events = load_events(ACTIVE_PLAYROOM_DIR, STORY_BIBLE)
        narrative_state = get_narrative_state()
        offered_id = narrative_state.get("story_director", {}).get("offered_event_id")
        directed_id = requested_event(all_events, event_index(all_events, narrative_state), turn_directives, state=narrative_state)
        target_eid = directed_id or offered_id
        matched_event = next((e for e in all_events if e["id"] == target_eid), None)
        explicit_id_tag = bool(target_eid) and any(
            re.search(rf'\[id:{re.escape(target_eid)}\]', str(d)) for d in turn_directives
        )
        if matched_event and (matched_event.get("is_tiered_event") or explicit_id_tag):
            evidence_sample = "\n".join(r.get("text", "") for r in responses if r.get("text")) or public_text
            mark_event_started(narrative_state, matched_event, evidence=evidence_sample,
                               turn=narrative_state.get("scene_pacing", {}).get("turn_number", 0),
                               events=all_events, bible=STORY_BIBLE)
            triggered_event_meta = {
                "event_id": matched_event["id"],
                "source": "story" if matched_event.get("is_tiered_event") else "arc",
                "tier": matched_event.get("tier", "small") if matched_event.get("is_tiered_event") else "arc",
                "tier_label": matched_event.get("tier_label", "Nhỏ") if matched_event.get("is_tiered_event") else "Arc",
                "title": matched_event["title"],
            }

    # Checkpoint transcript cũ trước khi state phình to; archive vẫn giữ lịch sử đầy đủ local.
    checkpoint_history_if_needed()

    get_narrative_state().setdefault("presence",{})["last_events"] = turn_presence_events
    save_room_state(ROOM_STATE)

    # Snapshot nhẹ sau mỗi lượt giúp phục hồi ngay cả khi tiến trình bị đóng cưỡng bức.
    persist_long_term_memory("autosave")
    illustration_pending = scene_illustrator.should_auto_illustrate(
        get_narrative_state(),
        bool(llm_config.api_key("apiz")),
    )

    return {
        "setting": ROOM_STATE.get('setting',''),
        "user_message": formatted_msg,
        "user_entry": player_entry,
        "decisions": [{key:value for key,value in item.items() if key != 'draft'} for item in sorted_decisions],
        "responses": responses,
        "active_character_ids": list(get_active_npcs()),
        "presence_events": get_narrative_state().get("presence", {}).get("last_events", []),
        "triggered_event": triggered_event_meta,
        "illustration_pending": illustration_pending,
    }


@app.post("/api/illustrate")
def create_scene_illustration(request: Request, auto: bool = False):
    """Sinh ảnh minh họa bối cảnh qua gpt-image-2.5 trên APIZ với khóa khuôn mặt nhân vật."""
    apiz_key = llm_config.api_key("apiz")
    if not apiz_key:
        raise HTTPException(status_code=503, detail="Thiếu APIZ_API_KEY để gọi gpt-image-2.5 tạo ảnh minh họa.")
    narrative_state = get_narrative_state()
    if auto and not scene_illustrator.should_auto_illustrate(narrative_state, True):
        return {"status": "skipped", "reason": "interval_not_reached"}

    session_id = request.headers.get("X-Playroom-Session", "legacy") or "legacy"
    save_dir = STATE_FILE_PATH.parent
    llm_plan_fn = None
    if client is not None and get_gemini_api_key():
        llm_plan_fn = lambda msgs: request_llm_json(
            MODEL_ARBITER, msgs, output_budget=2048, purpose="illustration brief"
        )
    try:
        entry = scene_illustrator.generate_room_illustration(
            room_id=ACTIVE_PLAYROOM_ID,
            session_id=session_id,
            room_state=ROOM_STATE,
            story_bible=STORY_BIBLE,
            playroom_dir=ACTIVE_PLAYROOM_DIR,
            save_dir=save_dir,
            avatar_dir=AVATAR_DIR,
            apiz_api_key=apiz_key,
            apiz_base_url=llm_config.endpoint("apiz"),
            resolve_avatar_url=get_avatar_url,
            llm_plan_fn=llm_plan_fn,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    except Exception as err:
        print(f"[Error] Scene illustration failed: {err}")
        raise HTTPException(status_code=502, detail=f"Không tạo được ảnh minh họa trên APIZ: {err}") from err

    ROOM_STATE.setdefault("history", []).append(entry)
    pacing = narrative_state.setdefault("scene_pacing", {})
    pacing["last_illustration_turn"] = int(entry.get("turn_number") or pacing.get("turn_number") or 0)
    save_room_state(ROOM_STATE)
    return {"status": "ok", "illustration": entry}


@app.get("/api/illustrations/{room_id}/{session_id}/{filename}")
def serve_scene_illustration(room_id: str, session_id: str, filename: str):
    """Phục vụ file PNG minh họa bối cảnh đã lưu an toàn theo playroom/session."""
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename or not safe_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        raise HTTPException(status_code=400, detail="Tên tệp minh họa không hợp lệ.")
    try:
        selected_dir = room_directory(PLAYROOMS_DIR, room_id)
        save_dir = roleplay_sessions.directory(selected_dir, session_id)
    except (ValueError, FileNotFoundError) as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    target = save_dir / "illustrations" / safe_name
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy ảnh minh họa.")
    return FileResponse(str(target), media_type="image/png")


class StoryEventSuggestRequest(BaseModel):
    count: int = 10


def suggest_next_story_events(count: int = 10) -> Dict[str, Any]:
    """Dùng model (hoặc fallback cục bộ) gợi ý ~10 sự kiện chưa diễn ra đủ 3 mức Nhỏ/Vừa/Lớn."""
    target_count = max(3, min(15, int(count or 10)))
    events = load_events(ACTIVE_PLAYROOM_DIR, STORY_BIBLE)
    state = get_narrative_state()
    pool = eligible_tiered_events(events, state)
    remaining_counts = {t: sum(1 for e in pool if normalize_tier(e.get("tier")) == t) for t in ("small", "medium", "large")}
    arc_roadmap = ordered_arc_roadmap(events, STORY_BIBLE, state)
    if not pool:
        director = state.setdefault("story_director", {"version": 1, "events": {}, "pending": None})
        director["last_suggestions"] = []
        save_room_state(ROOM_STATE)
        return {
            "suggestions": [],
            "remaining_counts": remaining_counts,
            "total_remaining": 0,
            "used_model": False,
            "exhausted": bool(STORY_BIBLE.get("story_events")),
            "arc_suggestions": arc_roadmap,
        }

    eligible_by_id = {e["id"]: e for e in pool}
    sampled = sample_tiered_pool(events, state, per_tier=8)
    candidate_payload = [{
        "event_id": e["id"],
        "tier": normalize_tier(e.get("tier")),
        "tier_label": TIER_LABELS.get(normalize_tier(e.get("tier")), "Nhỏ"),
        "title": e["title"],
        "summary": e["summary"],
        "location": e.get("location", ""),
        "cast_ids": e.get("cast_ids", [])[:6],
    } for e in sampled]

    recent_tail = [{
        "sender": item.get("sender", ""),
        "text": str(item.get("text", ""))[:260],
    } for item in ROOM_STATE.get("history", [])[-4:]]
    active_names = [
        ROOM_STATE.get("roster", {}).get(nid, {}).get("name", nid)
        for nid in get_active_npcs()
    ]

    picked = []
    used_model = False
    key = get_gemini_api_key()
    if client is not None and key and key != "YOUR_API_KEY":
        prompt_payload = {
            "count": min(target_count, len(pool)),
            "setting": ROOM_STATE.get("setting", ""),
            "active_characters": active_names[:12],
            "recent_history": recent_tail,
            "candidates": candidate_payload,
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "Bạn là trợ lý Đạo diễn Cốt truyện cho trò chơi nhập vai tương tác. "
                    "Nhiệm vụ: dựa vào bối cảnh hiện tại và danh sách sự kiện `candidates` lấy từ `story_events`, "
                    "hãy chọn ra khoảng 10 gợi ý bất kỳ nhưng đa dạng, hấp dẫn, phù hợp hoặc tạo chuyển biến hay cho cảnh hiện tại.\n"
                    "QUY TẮC BẮT BUỘC:\n"
                    "1. CHỈ chọn các `event_id` có trong `candidates` (không tự bịa ID, không lặp ID).\n"
                    "2. Phải bao gồm ĐỦ CẢ 3 LOẠI mức độ: `small` (Nhỏ - đời thường/hài hước/nhẹ), `medium` (Vừa - nhiệm vụ/đấu tập/điều tra), và `large` (Lớn - biến cố/cao trào toàn trường), lý tưởng khoảng 4 small + 4 medium + 2 large nếu danh sách còn đủ.\n"
                    "3. Với mỗi gợi ý, viết `teaser` (1 câu ngắn gọn khơi gợi nối từ bối cảnh/nhân vật đang có mặt) và `director_command` bắt đầu bằng `Bắt đầu sự kiện: <tên sự kiện rõ ràng>`.\n"
                    "4. Chỉ trả về đúng một JSON object có khóa `suggestions` là mảng các object `{event_id, teaser, director_command}`."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt_payload, ensure_ascii=False),
            },
        ]
        try:
            raw = request_llm_json(
                model=MODEL_ARBITER,
                messages=messages,
                output_budget=2200,
                purpose="story event suggest",
            )
            items = raw.get("suggestions", []) if isinstance(raw, dict) else []
            seen_ids = set()
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                eid = str(item.get("event_id") or "").strip()
                if eid not in eligible_by_id or eid in seen_ids:
                    continue
                base = eligible_by_id[eid]
                tier = normalize_tier(base.get("tier"))
                cmd = str(item.get("director_command") or "").strip() or base.get("director_command") or f"Bắt đầu sự kiện: {base['title']}"
                if not cmd.lower().startswith("bắt đầu sự kiện"):
                    cmd = f"Bắt đầu sự kiện: {base['title']}"
                teaser = str(item.get("teaser") or base["summary"]).strip()[:240]
                picked.append({
                    "event_id": eid,
                    "tier": tier,
                    "tier_label": TIER_LABELS.get(tier, "Nhỏ"),
                    "title": base["title"],
                    "teaser": teaser,
                    "director_command": cmd,
                    "location": base.get("location", ""),
                    "cast_ids": list(base.get("cast_ids", [])),
                })
                seen_ids.add(eid)
                if len(picked) >= target_count:
                    break
            used_model = bool(picked)
        except Exception as err:
            print(f"[story-events/suggest fallback] {err}", file=sys.stderr)
            picked = []

    # Bảo đảm đủ ~target_count và có đủ cả 3 mức (small, medium, large) nếu trong pool còn
    fallback_items = pick_fallback_suggestions(events, state, count=max(target_count, len(pool)))
    picked_ids = {item["event_id"] for item in picked}
    present_tiers = {item["tier"] for item in picked}
    for required_tier in ("small", "medium", "large"):
        if remaining_counts.get(required_tier, 0) > 0 and required_tier not in present_tiers:
            candidate = next((f for f in fallback_items if f["tier"] == required_tier and f["event_id"] not in picked_ids), None)
            if candidate:
                if len(picked) >= target_count:
                    # Thay thế phần tử thuộc tier đang thừa nhiều nhất
                    tier_counts = {t: sum(1 for p in picked if p["tier"] == t) for t in ("small", "medium", "large")}
                    excess_tier = max(tier_counts, key=tier_counts.get)
                    drop_idx = next((idx for idx, p in enumerate(picked) if p["tier"] == excess_tier), len(picked) - 1)
                    picked_ids.discard(picked[drop_idx]["event_id"])
                    picked[drop_idx] = candidate
                else:
                    picked.append(candidate)
                picked_ids.add(candidate["event_id"])
                present_tiers.add(required_tier)
    for item in fallback_items:
        if len(picked) >= min(target_count, len(pool)):
            break
        if item["event_id"] not in picked_ids:
            picked.append(item)
            picked_ids.add(item["event_id"])

    director = state.setdefault("story_director", {"version": 1, "events": {}, "pending": None})
    director["last_suggestions"] = picked
    director["last_suggestions_used_model"] = used_model
    save_room_state(ROOM_STATE)
    return {
        "suggestions": picked,
        "remaining_counts": remaining_counts,
        "total_remaining": len(pool),
        "used_model": used_model,
        "exhausted": False,
        "arc_suggestions": arc_roadmap,
    }


@app.post("/api/story-events/suggest")
def api_suggest_story_events(req: Optional[StoryEventSuggestRequest] = None):
    """Gợi ý ~10 sự kiện (Nhỏ / Vừa / Lớn) chưa diễn ra từ story_events dựa trên trạng thái hiện tại."""
    count = req.count if req is not None else 10
    return suggest_next_story_events(count=count)

@app.post("/api/reset")
def reset_history():
    """Xóa lịch sử hội thoại của phòng chơi."""
    ROOM_STATE["history"] = []
    save_room_state(ROOM_STATE)
    return {"status": "success", "message": "Đã xóa sạch lịch sử phòng chơi."}

# Mount thư mục Static Web UI
STATIC_DIR = BASE_DIR / "static"

@app.get("/api/llm/config")
def public_llm_config():
    """Read-only configuration status; never return credentials or call a provider."""
    provider = llm_config.provider_for_model(MODEL_ROLEPLAY)
    return {
        "provider": provider,
        "model_arbiter": MODEL_ARBITER,
        "model_roleplay": MODEL_ROLEPLAY,
        "api_model": llm_config.upstream_model(MODEL_ROLEPLAY),
        "reasoning_effort": llm_config.request_options(MODEL_ROLEPLAY).get("reasoning_effort", "provider_default"),
        "model_choices": provider_model_choices(),
        "model_selection_enabled": True,
        "provider_label": 'Google AI Studio · Gemini API' if provider=='gemini' else 'APIZ',
        "api_base_url": llm_config.endpoint(provider),
        "model_routing": "by_model",
        "providers": {name: {"key_configured": bool(llm_config.api_key(name)),
                             "api_base_url": llm_config.endpoint(name)}
                      for name in llm_config.DEFAULT_ENDPOINTS},
        "model_selection_scope": "project",
        "scene_contract_version": "2026-10-15.contextual-speech-v3",
        "room_runtime_profile": STORY_BIBLE.get('runtime_profile','shared'),
        "room_scene_contract_version": 'canon-sandbox-v2' if canon_runtime.enabled(STORY_BIBLE) else
            'blueprint-1.1' if blueprint_runtime.enabled(STORY_BIBLE) else 'contextual-speech-v3',
        "scene_mode": SCENE_MODE or blueprint_runtime.lifecycle_enabled(STORY_BIBLE),
        "input_byte_limits": INPUT_BYTE_LIMITS,
        "visible_length_policy": "generation_target_preserve_complete_response",
        "key_configured": bool(get_gemini_api_key() and client),
        "key_validated": False,
        "automatic_provider_fallback": False,
        "ooc_guard": ENABLE_OOC_GUARD,
        "state_tracker": ENABLE_STATE_TRACKER,
        "key_variable": "APIZ_API_KEY" if provider == "apiz" else "GEMINI_API_KEY",
    }


# The UI catalog and API/OpenAPI validation must have exactly the same IDs.
ProjectModelId = Literal[tuple(item['id'] for item in provider_model_choices())]


class ModelSelectionRequest(BaseModel):
    model: ProjectModelId


@app.put("/api/llm/model")
def select_project_model(payload: ModelSelectionRequest):
    """Turn middleware serializes selection; persist without touching room history."""
    global MODEL_ARBITER, MODEL_ROLEPLAY
    provider = llm_config.provider_for_model(payload.model)
    if not llm_config.api_key(provider):
        variable = 'GEMINI_API_KEY' if provider == 'gemini' else 'APIZ_API_KEY'
        raise HTTPException(status_code=409, detail=f"Chưa có {variable}. Điền key một lần trong .env rồi khởi động lại server; model cũ được giữ nguyên.")
    temporary = MODEL_SETTINGS_PATH.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps({"provider":provider,"model": payload.model}, indent=2), encoding="utf-8")
        temporary.replace(MODEL_SETTINGS_PATH)
    except OSError:
        raise HTTPException(status_code=503, detail="Không lưu được lựa chọn model; cấu hình cũ được giữ nguyên")
    MODEL_ARBITER = MODEL_ROLEPLAY = payload.model
    return public_llm_config()


@app.get("/settings")
def configuration_page():
    return FileResponse(str(STATIC_DIR / "settings.html"))

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def serve_frontend():
        return FileResponse(str(STATIC_DIR / "market.html"))

    @app.get("/boyfriend")
    @app.get("/girlfriend")
    def serve_companion_market():
        return FileResponse(str(STATIC_DIR / "market.html"))

    @app.get("/boyfriend/new")
    def serve_boyfriend_creator():
        return FileResponse(str(STATIC_DIR / "companion-create.html"))

    @app.get("/character/{room_id}")
    def serve_companion_profile(room_id: str):
        try:
            selected = room_directory(PLAYROOMS_DIR, room_id)
            if read_manifest(selected)["category"] not in {"boyfriend", "girlfriend"}:
                raise ValueError("Đây không phải hồ sơ companion")
        except (ValueError, FileNotFoundError) as error:
            raise HTTPException(status_code=404, detail=str(error))
        return FileResponse(str(STATIC_DIR / "character.html"))

    @app.get("/play/{room_id}")
    def serve_playroom(room_id: str):
        try:
            selected = room_directory(PLAYROOMS_DIR, room_id)
            if read_manifest(selected)["category"] in {"boyfriend", "girlfriend"}:
                state = json.loads((selected / "room_state.json").read_text(encoding="utf-8"))
                if not state.get("session_started"):
                    return RedirectResponse(f"/character/{room_id}", status_code=303)
        except (ValueError, FileNotFoundError) as error:
            raise HTTPException(status_code=404, detail=str(error))
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get('/enter/{room_id}')
    def serve_role_selection(room_id: str):
        try:
            selected = room_directory(PLAYROOMS_DIR, room_id)
            if not roleplay_sessions.config(selected):
                return RedirectResponse(f'/play/{room_id}', status_code=303)
        except (ValueError, FileNotFoundError) as error:
            raise HTTPException(404, str(error)) from error
        return FileResponse(str(STATIC_DIR/'play-sessions.html'))

if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8000))
    display_host = "localhost" if host == "0.0.0.0" else host
    print("=" * 60)
    print(f"🚀 Playroom AI Server đang chạy!")
    print(f"👉 Nhấp vào đây để mở trình duyệt: http://{display_host}:{port}")
    print("=" * 60)
    uvicorn.run(app, host=host, port=port)
