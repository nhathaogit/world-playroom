"""Real-time contextual scene illustration powered by gpt-image-2.5 on APIZ.

Triggers periodically (~every 10 turns) or on demand, builds a strict face-locked
character portrait reference sheet from local avatar assets + public visual
descriptions, and generates an atmospheric scene illustration matching the room's
current setting and events.
"""
from __future__ import annotations

import base64
import io
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    import httpx
except ImportError:  # Allow lightweight test/tooling environments without httpx
    httpx = None  # type: ignore[assignment]
from PIL import Image, ImageDraw, ImageFont, ImageOps

ILLUSTRATION_MODEL = "gpt-image-2.5"
ILLUSTRATION_INTERVAL_TURNS = 10


def _fold(text: str) -> str:
    nfd = unicodedata.normalize("NFD", (text or "").replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in nfd if not unicodedata.combining(c)).lower().strip()


def should_auto_illustrate(narrative_state: Dict[str, Any], has_apiz_key: bool, interval: int = ILLUSTRATION_INTERVAL_TURNS) -> bool:
    """Return True when enough turns elapsed since the last illustration and APIZ is ready."""
    if not has_apiz_key or not isinstance(narrative_state, dict):
        return False
    pacing = narrative_state.get("scene_pacing", {})
    if not isinstance(pacing, dict):
        return False
    turn_num = int(pacing.get("turn_number") or 0)
    last_turn = int(pacing.get("last_illustration_turn") or 0)
    if turn_num < interval:
        return False
    return (turn_num - last_turn) >= interval


def _load_font(size: int) -> ImageFont.ImageFont:
    for candidate in (
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
    ):
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _resolve_local_avatar_file(
    profile: Dict[str, Any],
    entity_id: str,
    avatar_dir: Path,
    resolve_avatar_url: Optional[Callable[[Optional[Dict[str, Any]], str], str]] = None,
) -> Optional[Path]:
    """Resolve a character's portrait image Path inside avatar_dir."""
    if resolve_avatar_url is not None:
        url = resolve_avatar_url(profile, entity_id) or ""
        clean = url.split("?")[0]
        if clean.startswith("/avatar/"):
            fname = clean[len("/avatar/"):]
            candidate = avatar_dir / fname
            if candidate.is_file():
                return candidate

    raw_av = str((profile or {}).get("avatar") or "").split("?")[0].strip()
    if raw_av:
        fname = raw_av[len("/avatar/"):] if raw_av.startswith("/avatar/") else raw_av
        candidate = avatar_dir / Path(fname).name
        if candidate.is_file():
            return candidate

    name = str((profile or {}).get("name") or "").strip()
    if name and avatar_dir.is_dir():
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            p = avatar_dir / f"{name}{ext}"
            if p.is_file():
                return p
            if entity_id:
                pid = avatar_dir / f"{entity_id}{ext}"
                if pid.is_file():
                    return pid
    return None


def _extract_visual_anchor(profile: Dict[str, Any], canon_profile: Dict[str, Any], catalog_profile: Dict[str, Any]) -> str:
    """Build spoiler-safe visual appearance anchor from public character fields."""
    parts: List[str] = []
    seen: set = set()
    for src in (canon_profile, catalog_profile, profile):
        if not isinstance(src, dict):
            continue
        for key in ("appearance", "public_face", "role", "guild"):
            val = str(src.get(key) or "").strip()
            if val and val not in seen:
                seen.add(val)
                parts.append(val)
    age = (canon_profile or profile or {}).get("age")
    if age:
        parts.insert(0, f"Age {age}")
    joined = "; ".join(parts)
    return joined[:360] if len(joined) > 360 else joined


def collect_scene_cast(
    room_state: Dict[str, Any],
    story_bible: Dict[str, Any],
    avatar_dir: Path,
    playroom_dir: Path,
    resolve_avatar_url: Optional[Callable[[Optional[Dict[str, Any]], str], str]] = None,
    max_chars: int = 4,
) -> List[Dict[str, Any]]:
    """Collect the most relevant visible characters in the current scene with face portraits."""
    history = [
        h for h in room_state.get("history", [])
        if isinstance(h, dict) and h.get("speaker_id") != "__illustrator__"
    ]
    recent = history[-8:]
    recent_text_folded = _fold(
        " ".join(
            f"{m.get('sender', '')} {m.get('text', '')} {m.get('narration', '')} {m.get('dialogue', '')}"
            for m in recent[-5:]
        )
    )
    last_turn_speakers = {
        m.get("speaker_id") for m in recent[-4:] if m.get("speaker_id") and m.get("speaker_id") != "__narrator__"
    }

    catalog_data: Dict[str, Any] = {}
    cat_path = playroom_dir / "character_catalog.json"
    if cat_path.is_file():
        try:
            catalog_data = json.loads(cat_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            catalog_data = {}

    canon_chars = story_bible.get("characters", {}) if isinstance(story_bible.get("characters"), dict) else {}
    roster = room_state.get("roster", {}) if isinstance(room_state.get("roster"), dict) else {}

    scored: List[Tuple[float, Dict[str, Any]]] = []
    candidate_ids = set(roster.keys()) | {
        m.get("speaker_id") for m in recent if m.get("speaker_id") and m.get("speaker_id") not in {"__narrator__", "player", ""}
    }

    for cid in candidate_ids:
        r_prof = roster.get(cid, {}) if isinstance(roster.get(cid), dict) else {}
        c_prof = canon_chars.get(cid, {}) if isinstance(canon_chars.get(cid), dict) else {}
        k_prof = catalog_data.get(cid, {}) if isinstance(catalog_data.get(cid), dict) else {}
        name = str(r_prof.get("name") or c_prof.get("name") or k_prof.get("name") or cid).strip()
        if not name or name.lower() == "system":
            continue

        score = 0.0
        if cid in last_turn_speakers:
            score += 5.0
        if r_prof.get("is_active"):
            score += 3.0
        folded_name = _fold(name)
        first_token = folded_name.split()[0] if folded_name else ""
        if folded_name and folded_name in recent_text_folded:
            score += 4.5
        elif len(first_token) >= 3 and re.search(rf"\b{re.escape(first_token)}\b", recent_text_folded):
            score += 3.5

        if score <= 0:
            continue

        merged_prof = {**c_prof, **r_prof, "name": name}
        avatar_path = _resolve_local_avatar_file(merged_prof, cid, avatar_dir, resolve_avatar_url)
        visual_anchor = _extract_visual_anchor(r_prof, c_prof, k_prof)
        scored.append((score, {
            "id": cid,
            "name": name,
            "visual_anchor": visual_anchor,
            "avatar_path": avatar_path,
            "is_player": False,
        }))

    # Also consider Player if referenced or has a portrait
    player = room_state.get("player", {}) if isinstance(room_state.get("player"), dict) else {}
    p_name = str(player.get("name") or "").strip()
    if p_name:
        p_avatar = _resolve_local_avatar_file(player, "player", avatar_dir, resolve_avatar_url)
        p_persona = str(player.get("persona") or player.get("role") or "").strip()[:260]
        p_folded = _fold(p_name)
        p_in_scene = (
            any(m.get("is_player") for m in recent[-3:])
            or (p_folded and p_folded in recent_text_folded)
            or p_avatar is not None
        )
        if p_in_scene:
            scored.append((4.2 if p_avatar else 2.5, {
                "id": "player",
                "name": p_name,
                "visual_anchor": p_persona or str(player.get("role") or "Main protagonist"),
                "avatar_path": p_avatar,
                "is_player": True,
            }))

    scored.sort(key=lambda item: (-item[0], 0 if item[1].get("avatar_path") else 1, item[1]["name"]))
    return [item[1] for item in scored[:max_chars]]


def build_reference_portrait_sheet(selected_chars: Sequence[Dict[str, Any]], output_path: Path) -> Optional[Path]:
    """Compose a labeled side-by-side portrait sheet PNG for strict face-locking."""
    with_images = [
        c for c in selected_chars
        if c.get("avatar_path") and Path(c["avatar_path"]).is_file()
    ][:4]
    if not with_images:
        return None

    tile = 384
    label_h = 46
    pad = 16
    n = len(with_images)
    width = pad + n * (tile + pad)
    height = pad + tile + label_h + pad

    canvas = Image.new("RGB", (width, height), (14, 18, 28))
    draw = ImageDraw.Draw(canvas)
    font = _load_font(20)

    for idx, char in enumerate(with_images):
        x0 = pad + idx * (tile + pad)
        y0 = pad
        ref_tag = f"REF {idx + 1}: {char['name']}"
        char["ref_label"] = f"REF {idx + 1}"

        with Image.open(char["avatar_path"]) as raw_im:
            im = ImageOps.exif_transpose(raw_im).convert("RGB")
            # Upper-center crop keeps face and hair prominently framed
            crop_im = ImageOps.fit(im, (tile, tile), method=Image.Resampling.LANCZOS, centering=(0.5, 0.22))
        canvas.paste(crop_im, (x0, y0))

        draw.rectangle([x0, y0, x0 + tile, y0 + tile], outline=(180, 145, 80), width=2)
        draw.rectangle([x0, y0 + tile, x0 + tile, y0 + tile + label_h], fill=(22, 28, 44))
        text = ref_tag[:34]
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text((x0 + max(4, (tile - tw) // 2), y0 + tile + max(4, (label_h - th) // 2)), text, fill=(248, 232, 188), font=font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)
    return output_path


FOCUS_MODE_GUIDE: Dict[str, Dict[str, Any]] = {
    "landscape_environment": {
        "max_chars": 0,
        "use_face_ref": False,
        "camera_rule": (
            "WIDE ESTABLISHING LANDSCAPE / ARCHITECTURE SHOT: Focus 100% on the environment, architecture, sky, "
            "weather, lighting, and world atmosphere. Do NOT place foreground characters posing; if any people "
            "exist in the distance, render only tiny unidentifiable silhouettes for scale."
        ),
    },
    "creature_spotlight": {
        "max_chars": 1,
        "use_face_ref": False,
        "camera_rule": (
            "CREATURE-FOCUSED COMPOSITION: The mythical creature, beast, spirit animal, or pet is the unmistakable "
            "centerpiece filling the visual focus in its natural habitat. Keep any human observer tiny, out of focus, "
            "or omitted — NEVER frame a human face portrait beside or in front of the creature."
        ),
    },
    "object_or_phenomenon": {
        "max_chars": 0,
        "use_face_ref": False,
        "camera_rule": (
            "ARTIFACT / MAGIC PHENOMENON CLOSE-OR-MEDIUM FOCUS: Center the frame on the physical object, bubbling cauldron, "
            "glowing rune circle, spell impact on the environment, ice crystals, ancient book, or room still-life. "
            "Do NOT fill the frame with posing faces or full-torso portraits; at most a gloved hand or distant silhouette."
        ),
    },
    "wide_action_scale": {
        "max_chars": 2,
        "use_face_ref": False,
        "camera_rule": (
            "CINEMATIC WIDE ACTION SCALE: Vast environment (arena, hall, sky, fjord, battlefield) occupies over 75% of the frame "
            "(high wide-angle or sweeping panorama). Any characters appear small-to-medium integrated naturally in motion — "
            "seen from dynamic angles or from behind, NEVER lined up chest-up facing the camera like an anime poster."
        ),
    },
    "character_interaction": {
        "max_chars": 2,
        "use_face_ref": True,
        "camera_rule": (
            "CANDID STORYTELLING INTERACTION: Natural off-angle, three-quarter, or over-the-shoulder framing inside the room "
            "while 1-2 characters are actively interacting with their surroundings (at least 40% physical setting visible; "
            "NO symmetrical low-angle hero lineup or chest-up staring at the viewer)."
        ),
    },
}


def _has_any_phrase(text_folded: str, phrases: Sequence[str]) -> bool:
    return any(re.search(rf"\b{re.escape(p)}\b", text_folded) for p in phrases)


def _strip_dialogue_quotes(text: str) -> str:
    return re.sub(r'[“"][^”"]*[”"]', ' ', str(text or '')).strip()


def _infer_focus_mode_from_text(setting: str, text_folded: str) -> str:
    """Heuristically choose landscape, creature, object/magic, wide action, or character interaction."""
    narration_only = _strip_dialogue_quotes(text_folded)
    combined = f"{_fold(setting)} {narration_only}".strip()
    creature_kw = (
        "con rong", "toc rong", "rong vinh", "bach loc", "ky lan", "con qua", "qua tuyet", "qua bac",
        "cu tuyet", "meo rung", "nhim suong mu", "con coc", "tuan loc", "thuy ma", "cao chin duoi",
        "sinh vat", "quai thu", "quai vat", "thu huyen bi", "linh thu", "ma thu", "linh vat",
        "draugr", "garmr", "lindworm", "wyvern", "nidhoggr", "kelpie",
        "dragon", "unicorn", "stag", "raven", "owl", "beast", "creature", "chim ung", "soi bac",
    )
    wide_action_kw = (
        "hexenwings", "hexwings", "san bay", "san dau", "khan dai", "vach xuat phat", "giai dau",
        "chien truong", "bay luon", "truy duoi", "cuoc dua", "vong loai", "giao tranh", "bao tuyet",
        "toan canh", "khong chien", "vinh suong bac", "stadium", "arena", "match", "battle", "duel",
    )
    object_kw = (
        "vac doc duoc", "chiec vac", "no vac", "doc duoc", "binh thuoc", "binh doc", "lo thuoc",
        "co ngu", "vong tron ma thuat", "thao duoc", "chiec nhan", "sach co", "cuon sach", "cuon so",
        "la thu", "luyen kim", "than vuc", "lanh dia", "ket gioi", "tinh the", "re den", "bang gia",
        "niflheim", "muspelheim", "alfheim", "vanaheim", "asgard", "midgard", "nidavellir", "helheim",
        "cauldron", "potion", "rune", "artifact", "herb", "grimoire", "crystals", "roots",
    )
    landscape_kw = (
        "lau dai", "bau troi", "cuc quang", "thung lung", "hanh lang", "phong ap mai", "phong ngu",
        "rung cam", "rung thi tham", "nha kinh", "thu vien", "ham ngam", "dai sanh", "ngon thap",
        "ho nuoc", "ho bang", "san truong", "quang truong", "canh quan", "binh minh", "hoang hon",
        "suong mu", "vinh", "castle", "corridor", "forest", "aurora", "hall", "courtyard", "sky",
        "tower", "attic", "greenhouse", "library", "fjord",
    )

    if _has_any_phrase(combined, creature_kw):
        return "creature_spotlight"
    if _has_any_phrase(combined, wide_action_kw):
        return "wide_action_scale"
    if _has_any_phrase(combined, object_kw):
        return "object_or_phenomenon"
    if _has_any_phrase(combined, landscape_kw):
        return "landscape_environment"
    return "character_interaction"


def _fallback_scene_plan(
    room_state: Dict[str, Any],
    story_bible: Dict[str, Any],
    cast: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    setting = str(room_state.get("setting") or story_bible.get("world", {}).get("setting") or "").strip()
    history = [
        h for h in room_state.get("history", [])
        if isinstance(h, dict) and h.get("speaker_id") != "__illustrator__"
    ]
    recent_lines = []
    for item in history[-4:]:
        raw_snippet = str(item.get("narration") or item.get("text") or "").strip()
        snippet = _strip_dialogue_quotes(raw_snippet).replace("\n", " ") or raw_snippet.replace("\n", " ")
        if snippet:
            recent_lines.append(snippet[:220])
    context_summary = " | ".join(recent_lines)[-500:]
    raw_recent = " ".join(
        f"{m.get('narration', '')} {m.get('text', '')}" for m in history[-3:]
    )
    focus_mode = _infer_focus_mode_from_text(setting, _fold(raw_recent))
    mode_cfg = FOCUS_MODE_GUIDE[focus_mode]
    present = [c["id"] for c in cast[:mode_cfg["max_chars"]]] if mode_cfg["use_face_ref"] else []
    scene_en = (
        f"Visual scene set in: {setting or 'arcane fantasy academy'}. "
        f"Focus ({focus_mode.replace('_', ' ')}): {context_summary}. "
        f"{mode_cfg['camera_rule']}"
    )
    caption_vi = f"Minh họa bối cảnh tại {setting}" if setting else "Minh họa khoảnh khắc hiện tại"
    return {
        "focus_mode": focus_mode,
        "character_ids": present,
        "composition_layers": {},
        "scene_prompt_en": scene_en,
        "caption_vi": caption_vi[:160],
    }


def plan_scene_illustration(
    room_state: Dict[str, Any],
    story_bible: Dict[str, Any],
    cast: Sequence[Dict[str, Any]],
    llm_plan_fn: Optional[Callable[[List[Dict[str, str]]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Use a fast JSON LLM call (or deterministic fallback) to choose focus mode, visual prompt & cast."""
    if llm_plan_fn is None:
        return _fallback_scene_plan(room_state, story_bible, cast)

    all_history = [h for h in room_state.get("history", []) if isinstance(h, dict)]
    recent_focus_modes = [
        str(h.get("focus_mode") or "character_interaction")
        for h in all_history
        if h.get("speaker_id") == "__illustrator__" or h.get("type") == "illustration"
    ][-3:]
    history = [
        h for h in all_history
        if h.get("speaker_id") != "__illustrator__" and h.get("type") != "illustration"
    ]
    recent_excerpt = []
    for m in history[-6:]:
        nar = str(m.get("narration") or "").strip()
        dlg = str(m.get("dialogue") or "").strip()
        txt = str(m.get("text") or "").strip()
        recent_excerpt.append({
            "sender": m.get("sender", ""),
            "speaker_id": m.get("speaker_id", ""),
            "narration": nar[:320] if nar else _strip_dialogue_quotes(txt)[:320],
            "dialogue": dlg[:180],
        })
    cast_payload = [
        {"id": c["id"], "name": c["name"], "visual": c.get("visual_anchor", "")}
        for c in cast
    ]
    world = story_bible.get("world", {}) if isinstance(story_bible.get("world"), dict) else {}
    world_summary = str(story_bible.get("premise") or "")[:320]
    if world.get("magic"):
        world_summary += f" | Magic/theme: {str(world.get('magic'))[:200]}"

    system_msg = (
        "You are a versatile cinematic art director choosing BOTH what the visual subject should be AND how to frame it for gpt-image-2.5. "
        "CRITICAL ANTI-PORTRAIT RULE: Roleplay turns always have character names and dialogue, so do NOT default to character portraits, anime boyband lineups, "
        "or chest-up hero poses staring at the viewer! Choose the subject that truly captures the atmosphere, scale, creature, object, or environment:\n"
        "- `landscape_environment`: Place, architecture, hall, sky, weather, aurora, forest, fjord, quiet room mood -> `character_ids` MUST be `[]` (people omitted or tiny silhouettes).\n"
        "- `creature_spotlight`: A dragon, owl, raven, stag, unicorn, monster, kelpie, or animal is in the scene -> frame the creature in its habitat; `character_ids` MUST be `[]` (at most tiny blurred/distant human silhouette in text).\n"
        "- `object_or_phenomenon`: Spell eruption (ice crystals, glowing roots, domain aura), potion cauldron, rune diagram, artifact, parchment, or room table detail -> focus on the object/phenomenon reshaping the space around it; `character_ids` MUST be `[]`.\n"
        "- `wide_action_scale`: Arena, stadium match (e.g. Hexwings), sweeping clash, storm, large hall commotion -> environment fills 75-85% of the frame via extreme wide/aerial/high-angle camera; any people appear small in motion (`character_ids` MUST be `[]` so faces are not zoomed in).\n"
        "- `character_interaction`: Use ONLY when a quiet 1-on-1 or 2-person emotional exchange genuinely depends on facial expressions, framed candidly/off-angle with >=40% physical setting visible -> `character_ids` has 1 or at most 2 ids.\n\n"
        "Return ONLY valid JSON: "
        '{"focus_mode": "landscape_environment|creature_spotlight|object_or_phenomenon|wide_action_scale|character_interaction", '
        '"character_ids": ["1-2 ids ONLY when focus_mode is character_interaction; MUST be [] for all other modes"], '
        '"composition_layers": {"foreground": "what sits near the lens", "midground": "main subject/space", "background": "architecture/sky/landscape depth"}, '
        '"scene_prompt_en": "Detailed English visual brief specifying wide/macro/candid camera distance, lighting, physical setting details, creatures/objects, and natural action.", '
        '"caption_vi": "1 short vivid Vietnamese caption (10-20 words)"}'
    )
    user_msg = json.dumps(
        {
            "setting": room_state.get("setting", ""),
            "recent_illustration_modes": recent_focus_modes,
            "world": world_summary,
            "cast_available_if_needed": cast_payload,
            "recent_turns": recent_excerpt,
        },
        ensure_ascii=False,
    )
    try:
        planned = llm_plan_fn([
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ])
        focus_mode = str(planned.get("focus_mode") or "").strip()
        if focus_mode not in FOCUS_MODE_GUIDE:
            focus_mode = _infer_focus_mode_from_text(
                str(room_state.get("setting") or ""),
                _fold(str(planned.get("scene_prompt_en") or "")),
            )
        valid_ids = {c["id"] for c in cast}
        mode_cfg = FOCUS_MODE_GUIDE[focus_mode]
        raw_ids = planned.get("character_ids")
        if not mode_cfg["use_face_ref"]:
            chosen: List[str] = []
        else:
            chosen = [cid for cid in (raw_ids or []) if cid in valid_ids][:mode_cfg["max_chars"]]
        raw_layers = planned.get("composition_layers")
        layers = {
            k: str(v).strip()
            for k, v in (raw_layers.items() if isinstance(raw_layers, dict) else [])
            if k in {"foreground", "midground", "background"} and str(v or "").strip()
        }
        scene_prompt = str(planned.get("scene_prompt_en") or "").strip()
        caption = str(planned.get("caption_vi") or "").strip()
        if not scene_prompt:
            return _fallback_scene_plan(room_state, story_bible, cast)
        return {
            "focus_mode": focus_mode,
            "character_ids": chosen,
            "composition_layers": layers,
            "scene_prompt_en": scene_prompt,
            "caption_vi": caption or _fallback_scene_plan(room_state, story_bible, cast)["caption_vi"],
        }
    except Exception:
        return _fallback_scene_plan(room_state, story_bible, cast)


def compose_locked_image_prompt(
    scene_prompt_en: str,
    present_chars: Sequence[Dict[str, Any]],
    setting: str,
    world_title: str = "",
    has_ref_sheet: bool = False,
    focus_mode: str = "character_interaction",
    composition_layers: Optional[Dict[str, str]] = None,
) -> str:
    """Assemble final prompt honoring scene composition scale first and identity lock second."""
    mode_cfg = FOCUS_MODE_GUIDE.get(focus_mode, FOCUS_MODE_GUIDE["character_interaction"])
    sections: List[str] = [
        f"COMPOSITION & CAMERA PRIORITY ({focus_mode.replace('_', ' ').upper()}): {mode_cfg['camera_rule']}",
        f"SCENE TO ILLUSTRATE: {scene_prompt_en}",
    ]
    if isinstance(composition_layers, dict) and composition_layers:
        layer_parts = [
            f"{k.upper()}: {composition_layers[k]}"
            for k in ("foreground", "midground", "background")
            if composition_layers.get(k)
        ]
        if layer_parts:
            sections.append("SPATIAL DEPTH LAYERS:\n- " + "\n- ".join(layer_parts))
    if setting:
        sections.append(f"CURRENT LOCATION & ATMOSPHERE ({world_title or 'World'}): {setting}")

    if mode_cfg["use_face_ref"] and has_ref_sheet and any(c.get("ref_label") for c in present_chars):
        sections.append(
            "CHARACTER IDENTITY REFERENCE (IDENTITY ONLY — DO NOT OVERRIDE COMPOSITION): "
            "Use the attached labeled portrait sheet ONLY to match the exact face, eyes, hair color/style, and age of the character(s) "
            "below while keeping them naturally scaled and integrated into the physical environment above "
            "(at least 40% scenery visible; NEVER turn into a symmetrical front-facing poster lineup or hero chest-up glare):"
        )
        for char in present_chars:
            if char.get("ref_label"):
                anchor = char.get("visual_anchor") or "faithful to reference portrait"
                sections.append(f"- {char['name']} (face/hair from {char['ref_label']}): {anchor}")
    elif not mode_cfg["use_face_ref"]:
        sections.append(
            "NON-PORTRAIT GUARDRAIL: Keep people absent, tiny distant silhouettes, or strictly secondary within the vast scenery/subject. "
            "Do NOT generate close-up faces, chest-up hero poses, symmetrical trio/group lineups, or characters staring at the camera."
        )
    elif present_chars:
        sections.append("CHARACTER VISUAL DETAILS (keep natural scale in scene):")
        for char in present_chars:
            anchor = char.get("visual_anchor") or char["name"]
            sections.append(f"- {char['name']}: {anchor}")

    sections.append(
        "ART DIRECTION: High-craft fantasy / dark academia digital painting, rich environmental storytelling, natural depth of field, "
        "cinematic lighting, authentic architecture/nature. NO speech bubbles, NO UI borders, NO overlaid text, NO watermarks."
    )
    return "\n".join(sections)


def _decode_image_response(payload: Dict[str, Any], client_http: httpx.Client) -> bytes:
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError(f"APIZ image API returned unexpected payload: {str(payload)[:240]}")
    first = items[0] if isinstance(items[0], dict) else {}
    b64 = first.get("b64_json")
    if isinstance(b64, str) and b64.strip():
        return base64.b64decode(b64)
    img_url = first.get("url")
    if isinstance(img_url, str) and img_url.strip():
        dl = client_http.get(img_url.strip(), follow_redirects=True)
        dl.raise_for_status()
        return dl.content
    raise ValueError("APIZ image API response contained neither b64_json nor url")


def call_apiz_image_api(
    prompt: str,
    api_key: str,
    base_url: str = "https://api.apiz.vn/v1",
    ref_sheet_path: Optional[Path] = None,
    timeout_seconds: float = 120.0,
) -> Tuple[bytes, bool]:
    """Call gpt-image-2.5 on APIZ (/images/edits with face reference sheet or /images/generations).

    Returns (png_bytes, used_reference_sheet).
    """
    if not api_key:
        raise ValueError("Thiếu APIZ_API_KEY để gọi gpt-image-2.5 trên APIZ.")
    endpoint_root = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}

    with httpx.Client(timeout=timeout_seconds) as http_client:
        if ref_sheet_path is not None and ref_sheet_path.is_file():
            ref_bytes = ref_sheet_path.read_bytes()
            for size in ("1536x1024", "1024x1024"):
                files = {"image": (ref_sheet_path.name, ref_bytes, "image/png")}
                data = {
                    "model": ILLUSTRATION_MODEL,
                    "prompt": prompt,
                    "n": "1",
                    "size": size,
                }
                resp = http_client.post(f"{endpoint_root}/images/edits", headers=headers, data=data, files=files)
                if resp.status_code == 200:
                    return _decode_image_response(resp.json(), http_client), True
                if resp.status_code in {401, 403}:
                    raise RuntimeError(f"APIZ xác thực thất bại ({resp.status_code}): {resp.text[:240]}")
                if resp.status_code in {404, 405, 501}:
                    break
                if resp.status_code == 400 and "size" in resp.text.lower() and size != "1024x1024":
                    continue
                print(f"[Warning] APIZ /images/edits returned {resp.status_code}, fallback to /images/generations: {resp.text[:200]}")
                break

        # Pure generation (no portraits needed, or gateway fallback)
        json_headers = {**headers, "Content-Type": "application/json"}
        last_err = ""
        for size in ("1536x1024", "1024x1024"):
            body = {
                "model": ILLUSTRATION_MODEL,
                "prompt": prompt,
                "n": 1,
                "size": size,
            }
            resp = http_client.post(f"{endpoint_root}/images/generations", headers=json_headers, json=body)
            if resp.status_code == 200:
                return _decode_image_response(resp.json(), http_client), False
            last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
            if resp.status_code == 400 and "size" in resp.text.lower() and size != "1024x1024":
                continue
            break
        raise RuntimeError(f"APIZ gpt-image-2.5 thất bại ({last_err or 'unknown error'})")


def generate_room_illustration(
    room_id: str,
    session_id: str,
    room_state: Dict[str, Any],
    story_bible: Dict[str, Any],
    playroom_dir: Path,
    save_dir: Path,
    avatar_dir: Path,
    apiz_api_key: str,
    apiz_base_url: str = "https://api.apiz.vn/v1",
    resolve_avatar_url: Optional[Callable[[Optional[Dict[str, Any]], str], str]] = None,
    llm_plan_fn: Optional[Callable[[List[Dict[str, str]]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Orchestrate cast resolution, face-locking sheet, gpt-image-2.5 generation, and local PNG save."""
    turn_number = int(room_state.get("narrative_state", {}).get("scene_pacing", {}).get("turn_number") or 0)
    cast = collect_scene_cast(room_state, story_bible, avatar_dir, playroom_dir, resolve_avatar_url=resolve_avatar_url)
    plan = plan_scene_illustration(room_state, story_bible, cast, llm_plan_fn=llm_plan_fn)

    focus_mode = str(plan.get("focus_mode") or "character_interaction")
    if focus_mode not in FOCUS_MODE_GUIDE:
        focus_mode = "character_interaction"
    mode_cfg = FOCUS_MODE_GUIDE[focus_mode]
    cap = mode_cfg["max_chars"]

    if not mode_cfg["use_face_ref"] or cap == 0:
        present_chars = []
    else:
        chosen_ids = list(dict.fromkeys(plan.get("character_ids") or []))
        chosen_set = set(chosen_ids[:cap])
        present_chars = [c for c in cast if c["id"] in chosen_set][:cap]

    ill_dir = save_dir / "illustrations"
    ill_dir.mkdir(parents=True, exist_ok=True)
    ref_tmp = ill_dir / f"_ref_tmp_{int(time.time() * 1000)}.png"
    try:
        ref_sheet = build_reference_portrait_sheet(present_chars, ref_tmp) if (mode_cfg["use_face_ref"] and present_chars) else None
        final_prompt = compose_locked_image_prompt(
            scene_prompt_en=plan["scene_prompt_en"],
            present_chars=present_chars,
            setting=str(room_state.get("setting") or ""),
            world_title=str(story_bible.get("title") or room_id),
            has_ref_sheet=ref_sheet is not None,
            focus_mode=focus_mode,
            composition_layers=plan.get("composition_layers"),
        )
        img_bytes, used_ref = call_apiz_image_api(
            prompt=final_prompt,
            api_key=apiz_api_key,
            base_url=apiz_base_url,
            ref_sheet_path=ref_sheet,
        )
    finally:
        if ref_tmp.exists():
            try:
                ref_tmp.unlink()
            except OSError:
                pass

    filename = f"scene_t{turn_number:04d}_{int(time.time())}.png"
    out_path = ill_dir / filename
    out_path.write_bytes(img_bytes)

    return {
        "sender": "Họa sĩ Ký ức",
        "speaker_id": "__illustrator__",
        "type": "illustration",
        "focus_mode": focus_mode,
        "image_url": f"/api/illustrations/{room_id}/{session_id}/{filename}",
        "caption": plan["caption_vi"],
        "prompt_preview": plan["scene_prompt_en"][:280],
        "characters": [{"id": c["id"], "name": c["name"], "face_locked": bool(c.get("ref_label"))} for c in present_chars],
        "turn_number": turn_number,
        "model": ILLUSTRATION_MODEL,
        "used_reference_sheet": used_ref,
        "is_player": False,
        "text": f"[Minh họa bối cảnh lượt {turn_number}]: {plan['caption_vi']}",
    }
