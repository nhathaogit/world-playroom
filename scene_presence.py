"""Apply explicit, validated presence deltas; reasoning text never changes a roster."""
from typing import Any
import re


def _unique_ascii_first_name(roster: dict, name: str) -> str:
    parts = str(name or '').strip().split()
    if len(parts) <= 1:
        return ''
    first = parts[0]
    if not (str(name).isascii() and first.isalpha() and len(first) >= 3):
        return ''
    first_cf = first.casefold()
    if sum(bool(str(value.get('name', '')).strip().split())
           and str(value.get('name', '')).strip().split()[0].casefold() == first_cf
           for value in roster.values()) != 1:
        return ''
    return first


def presence_name_aliases(roster: dict) -> dict:
    """Exact full/familiar names and unambiguous title + given-name addresses.

    Never add bare short names such as An: 'an khang' is not an NPC mention.
    """
    owners = {}
    given_owners = {}
    for npc_id, npc in roster.items():
        parts = str(npc.get('name', '')).strip().split()
        if parts:
            given_owners.setdefault(parts[-1].casefold(), set()).add(npc_id)
    for npc_id, npc in roster.items():
        name = str(npc.get('name', '')).strip()
        forms = {name, ' '.join(name.split()[-2:])} if name else set()
        first = _unique_ascii_first_name(roster, name)
        if first:
            forms.add(first)
        aliases = npc.get('aliases', [])
        if isinstance(aliases, list):
            forms.update(a.strip() for a in aliases if isinstance(a, str) and len(a.strip()) >= 3)
        parts = name.split()
        if len(parts) > 1 and len(given_owners.get(parts[-1].casefold(), set())) == 1:
            forms.update(f'{title} {parts[-1]}' for title in
                         ('anh','chị','em','cậu','cô','ông','bà','chú','bác','dì'))
        for form in forms:
            owners.setdefault(form.casefold(), set()).add(npc_id)
    return {name: next(iter(ids)) for name, ids in owners.items() if len(ids) == 1}


def is_scene_member(room: dict, npc: dict) -> bool:
    """One presence contract for the engine and knowledge projection.

    A current-scene manual ON is explicit placement, even in old saves where
    toggle failed to update scene_scope. It grants no retroactive knowledge.
    """
    if not npc.get('is_active'):
        return False
    setting = room.get('setting')
    manual_here = (npc.get('presence_source') == 'manual'
                   and npc.get('presence_lock_scene') == setting)
    return manual_here or npc.get('scene_scope', setting) == setting


def repair_manual_membership(room: dict) -> list:
    """Repair only explicit current-scene ON placement; no flags/history changes."""
    repaired = []
    for npc_id, npc in room.get('roster', {}).items():
        if (npc.get('is_active') and npc.get('presence_source') == 'manual'
                and npc.get('presence_lock_scene') == room.get('setting')
                and npc.get('scene_scope') != room.get('setting')):
            npc['scene_scope'] = room.get('setting', '')
            repaired.append(npc_id)
    return repaired


def set_manual_presence(room: dict, npc_id: str, active: bool) -> dict:
    npc = room['roster'][npc_id]
    npc.update(is_active=active, presence_source='manual',
               presence_lock_scene=room.get('setting',''),
               presence_reason='Người chơi bật/tắt thủ công trong cảnh này.')
    if active:
        npc['scene_scope'] = room.get('setting','')
    room['active_character_ids'] = [key for key, value in room['roster'].items() if value.get('is_active')]
    return npc


_NARRATED_EXCLUDED_RE = re.compile(
    r'\b(nếu|giả sử|ước gì|có lẽ|dường như|tưởng tượng|giấc mơ|bức tranh|bức ảnh|'
    r'qua màn hình|qua điện thoại|qua lacrima|nhắc tới|nhắc đến|kể về|nghe nói|'
    r'hồi tưởng|hồi ức|ký ức|hôm qua|ngày mai|ở xa|nơi xa|nơi khác|từ xa|'
    r'qua thư|lá thư|bức thư|không thấy|chưa thấy|không hề|chưa hề|'
    r'không có mặt|vắng mặt|chưa tới|chưa đến|đợi đến khi|chờ đến khi)\b',
    re.I,
)

_LOCAL_EXCLUDED_RE = re.compile(
    r'\b(?:qua (?:màn hình|điện thoại|lacrima)|từ xa|ở xa|nơi xa|nơi khác|'
    r'hồi tưởng|hồi ức|ký ức|nghe nói|nhắc (?:tới|đến)|kể về|'
    r'nhớ (?:lại|tới|đến)|nghĩ (?:tới|đến|về)|trước (?:đây|kia)|ngày xưa|hồi đó|lúc trước|'
    r'giấc mơ|bức tranh|bức ảnh|qua thư|lá thư|bức thư|tưởng tượng|ước gì|có lẽ|dường như|'
    r'nếu|giả sử|chưa tới|chưa đến|không thấy|chưa thấy|không hề|chưa hề|'
    r'không có mặt|vắng mặt|không ở đây|chưa quay lại|chưa trở lại|đợi đến khi|chờ đến khi|'
    r'sẽ\s+(?:tới|đến|vào|ra|có mặt|hiện diện|ghé|quay lại|trở lại|bước)|'
    r'ở lại|tiễn|từ biệt|chia tay|rời khỏi)\b',
    re.I,
)


def _speaker_name_forms(roster: dict, npc_id: str) -> set:
    npc = roster.get(npc_id, {})
    full = str(npc.get('name', '')).strip()
    forms = {full} if full else set()
    aliases = npc.get('aliases', [])
    if isinstance(aliases, list):
        forms.update(a.strip() for a in aliases if isinstance(a, str) and a.strip())
    # Foreign given names are often used without a surname in their own blocks.
    # Keep Vietnamese compound names and ambiguous shared surnames intact.
    first = full.split()[0] if full else ''
    if first.isascii() and len(first) >= 3 and sum(
            str(value.get('name', '')).strip().split()[:1] == [first]
            for value in roster.values()) == 1:
        forms.add(first)
    for alias, owner in presence_name_aliases(roster).items():
        if owner == npc_id:
            forms.add(alias)
    return forms


def narrated_presence_updates(room: dict, narration: str) -> list:
    """Conservative recovery of explicit local presence, never mere name mentions.

    Structured plans remain the primary contract. This fallback handles older
    saves/outputs without metadata. Ambiguous language intentionally returns no delta.
    """
    text = re.sub(r'[“"][^”"]*[”"]', ' ', str(narration or ''))
    sentences = re.split(r'(?<=[.!?])\s+|\n+', text)
    result = {}
    enter = re.compile(
        r'^(?:\s*[,;:]?\s*)(?:(?:đang|vẫn|đã|vừa|cũng|khẽ|cùng|cùng nhau|cùng lúc|lần lượt|'
        r'đều|vội|vội vã|vội vàng|lặng lẽ|chậm rãi|bất ngờ|chợt|bỗng|liền|lập tức|ngay)\s+){0,3}(?:'
        r'(?:ngồi|đứng)\s+(?:ngay\s+|lại\s+)?(?:gần đó|gần đây|bên cạnh|trước mặt|trước cửa|bên cửa|cạnh cửa|bên bàn|quanh bàn|trong phòng|trong sảnh|ở đây|tại đây|đối diện)|'
        r'(?:ngồi\s+xuống|đứng\s+dậy|đứng\s+sững)|'
        r'(?:bước|tiến|đi|chạy|lao|xông|ghé|trở|quay|lách|len|trượt)\s+(?:vào|tới|đến|ra|lại|lại gần|ngang qua|về phía)|'
        r'(?:đẩy|mở|bật)\s+(?:cánh\s+)?cửa|'
        r'(?:thuấn di|dịch chuyển)\s+(?:vào|tới|đến|ra)|'
        r'(?:xuất hiện|hiện ra|tới nơi|đến nơi)|'
        r'(?:có mặt|hiện diện)\s+(?:ở đây|tại đây|trong phòng|trong sảnh|trước cửa|bên cạnh)|'
        r'cất tiếng|lên tiếng|cúi chào|ló\s+(?:đầu|mặt)|tựa\s+(?:lưng|vai))',
        re.I,
    )
    leave = re.compile(r'^(?:\s*[,;:]?\s*)(?:(?:đã|vừa|lặng lẽ)\s+){0,2}(?:rời khỏi phòng|rời phòng|bước ra khỏi phòng|rời khỏi cảnh)', re.I)
    aliases = presence_name_aliases(room.get('roster', {}))
    if not aliases:
        return []
    names = re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(n) for n in sorted(aliases, key=len, reverse=True)) + r')(?!\w)', re.I)
    for sentence in sentences:
        if _NARRATED_EXCLUDED_RE.search(sentence):
            continue
        matches = list(names.finditer(sentence))
        for index, match in enumerate(matches):
            end = match.end()
            # Share a predicate only across a contiguous named subject list.
            for following in matches[index+1:]:
                if not re.fullmatch(r'\s*(?:,|và|cùng với)\s*', sentence[end:following.start()], re.I):
                    break
                end = following.end()
            suffix = sentence[end:]
            if leave.search(suffix):
                action = 'leave'
            elif enter.search(suffix):
                action = 'enter'
            else:
                continue
            npc_id = aliases[match.group().casefold()]
            result[npc_id] = {'npc_id':npc_id, 'action':action,
                'reason':'Lời dẫn xác nhận sự hiện diện thực tế trong cảnh hiện tại.',
                'evidence':sentence.strip()}
    return list(result.values())


def local_speaker_presence_update(room: dict, npc_id: str, narration: str, allow_inactive: bool = False) -> dict:
    """Bind a speaker's physical scene block to this location.

    Scene narration is local by default. Do not infer travel from dialogue,
    remote communication, memories, farewells, dead profiles or manual OFF locks.
    """
    roster = room.get('roster', {})
    npc = roster.get(npc_id, {})
    if not isinstance(npc, dict) or npc.get('status') == 'deceased':
        return {}
    if not allow_inactive and not npc.get('is_active'):
        return {}
    if 'presence_lock_scene' in npc and npc.get('presence_lock_scene') == room.get('setting') and not npc.get('is_active'):
        return {}
    text = re.sub(r'[“"][^”"]*[”"]', ' ', str(narration or '')).strip()
    if not text or _LOCAL_EXCLUDED_RE.search(text):
        return {}
    forms = _speaker_name_forms(roster, npc_id)
    if not any(re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text, re.I)
               for name in forms):
        return {}
    evidence = text
    for raw_sentence in re.split(r'(?<=[.!?])\s+|\n+', str(narration or '')):
        clean_sentence = re.sub(r'[“"][^”"]*[”"]', ' ', raw_sentence).strip()
        if clean_sentence and any(re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', clean_sentence, re.I)
                                  for name in forms):
            evidence = raw_sentence.strip()
            break
    return {'npc_id': npc_id, 'action': 'enter',
            'reason': 'Lời dẫn của nhân vật xác nhận đang trực tiếp hoạt động trong cảnh hiện tại.',
            'evidence': evidence}


def narrated_speaker_presence_update(room: dict, npc_id: str, narration: str) -> dict:
    """Recover local physical presence of an active speaker from scene narration.

    Requires at least one non-excluded narrated sentence naming the speaker,
    so dialogue-only appearances, memories, remote channels and manual OFF locks
    never activate an absent character.
    """
    roster = room.get('roster', {})
    npc = roster.get(npc_id, {})
    if not isinstance(npc, dict) or npc.get('status') == 'deceased':
        return {}
    if 'presence_lock_scene' in npc and npc.get('presence_lock_scene') == room.get('setting') and not npc.get('is_active'):
        return {}
    forms = _speaker_name_forms(roster, npc_id)
    if not forms:
        return {}
    for raw_sentence in re.split(r'(?<=[.!?])\s+|\n+', str(narration or '')):
        sentence = raw_sentence.strip()
        clean = re.sub(r'[“"][^”"]*[”"]', ' ', sentence).strip()
        if not clean or _LOCAL_EXCLUDED_RE.search(clean):
            continue
        if any(re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', clean, re.I) for name in forms):
            return {'npc_id': npc_id, 'action': 'enter',
                    'reason': 'Lời dẫn cảnh hiện tại xác nhận nhân vật đang trực tiếp có mặt.',
                    'evidence': sentence}
    return {}


def director_destination_plan(room: dict, directives: list, speaking_ids: set) -> dict:
    """Recover a declared encounter, not a name mention or an NPC teleport.

    Run after identity registration. The player travels to the named NPC's
    location; the NPC is not required to walk into the player's previous scene.
    """
    roster = room.get('roster', {})
    for command in directives:
        text = str(command).strip()
        if re.search(r'\b(?:muốn|sẽ|định|nếu|giả sử|chưa|không|ngày mai|hồi tưởng)\b', text, re.I):
            continue
        match = re.search(r'\b(?:đến|tới)\s+(?:vị trí của|chỗ|bên cạnh|gặp)\s+([^.!?;,\n]+)', text, re.I)
        if not match:
            continue
        actor = text[:match.start()].casefold()
        if any(re.search(r'(?<!\w)' + re.escape(alias) + r'(?!\w)', actor)
               for npc in roster.values()
               for alias in [str(npc.get('name', '')).casefold()]
               if alias):
            continue
        name = match.group(1).strip().casefold()
        matches = []
        for key, npc in roster.items():
            full = str(npc.get('name', '')).strip().casefold()
            configured = npc.get('aliases', [])
            aliases = [full] + [str(a).casefold() for a in configured if isinstance(a, str)] if isinstance(configured, list) else [full]
            first = full.split()[0] if full else ''
            # Short western given names (Mira/Mirajane) must resolve uniquely
            # across the whole roster, never by whichever NPC happens to speak.
            shortened = (len(name) >= 4 and name.isascii() and ' ' not in name
                         and first.startswith(name))
            if name in aliases or shortened:
                matches.append(key)
        if len(matches) != 1 or matches[0] not in speaking_ids:
            continue
        key = matches[0]
        npc = roster[key]
        if not npc.get('is_active') and npc.get('presence_lock_scene') == room.get('setting'):
            continue
        location = npc.get('scene_scope')
        if not location or location == room.get('setting'):
            # An already-present NPC needs no recovery or synthetic transition.
            if is_scene_member(room, npc):
                continue
            location = 'Khu vực hiện tại của ' + npc.get('name', key)
        return {'scene_changed': True, 'new_setting': location,
                'scene_evidence': text, 'presence_updates': [
                    {'npc_id': key, 'action': 'enter',
                     'reason': 'Player đã chuyển tới vị trí của nhân vật theo chỉ thị đạo diễn.',
                     'evidence': text}]}
    return {}


def close_scene_membership(room: dict, previous_active: set[str]) -> list:
    """Close old-scene membership after farewell blocks have been validated."""
    events=[]
    for npc_id in sorted(previous_active):
        npc=room.get('roster',{}).get(npc_id,{})
        if not npc.get('is_active') or is_scene_member(room,npc):
            continue
        reason='Không có xác nhận đi cùng hoặc hiện diện trong cảnh mới.'
        npc.update(is_active=False,presence_source='auto',presence_reason=reason)
        event={'npc_id':npc_id,'name':npc.get('name',npc_id),'action':'leave','source':'auto','reason':reason}
        events.append(event)
        room.setdefault('history', []).append({'sender':'System','presence_event':event,
            'text':f"[HIỆN DIỆN]: {event['name']} rời khỏi cảnh. {reason}"})
    room['active_character_ids']=[key for key,npc in room.get('roster',{}).items() if npc.get('is_active')]
    return events


def finalize_presence_events(room: dict, previous_roster: dict, history_start: int) -> list:
    """Notify actual committed flags, including introductions/tracker lifecycle.

    Does not activate anyone. Keeps a single event per (identity, action) in the
    turn result and backfills notifications only for a real unreported flag delta.
    """
    history=room.setdefault('history',[])
    events=[];seen=set()
    for entry in history[history_start:]:
        event=entry.get('presence_event')
        if not isinstance(event,dict):continue
        key=(event.get('npc_id'),event.get('action'))
        if all(isinstance(v,str) for v in key) and key not in seen:
            events.append(event);seen.add(key)
    roster=room.get('roster',{})
    for npc_id in sorted(set(previous_roster)|set(roster)):
        before=bool(previous_roster.get(npc_id,{}).get('is_active'))
        npc=roster.get(npc_id,{})
        active=bool(npc.get('is_active'))
        action='enter' if active else 'leave'
        if before==active or (npc_id,action) in seen:continue
        reason=str(npc.get('presence_reason') or (npc.get('scene_function') if active
                   else npc.get('retired_reason')) or 'Cập nhật theo diễn biến đã lưu.')[:300]
        event={'npc_id':npc_id,'name':npc.get('name') or previous_roster.get(npc_id,{}).get('name',npc_id),
               'action':action,'source':'auto','reason':reason}
        verb='có mặt trong cảnh' if active else 'rời khỏi cảnh'
        history.append({'sender':'System','presence_event':event,
                        'text':f"[HIỆN DIỆN]: {event['name']} {verb}. {reason}"})
        events.append(event);seen.add((npc_id,action))
    room['active_character_ids']=[key for key,npc in roster.items() if npc.get('is_active')]
    return events


def apply_presence_plan(room: dict, plan: Any, candidates: set[str], evidence_text: str,
                        explicit_scene_change: bool = False, dead_ids: set[str] | None = None,
                        arrival_limit: int | None = 3, close_previous_scene: bool = False,
                        defer_departures: bool = False) -> list:
    if not isinstance(plan, dict):
        return []
    roster = room.get('roster', {})
    dead_ids = dead_ids or set()
    new_setting = plan.get('new_setting')
    anchor = str(plan.get('scene_evidence') or '').strip()
    previous_setting = room.get('setting', '')
    previous_active = {key for key,npc in roster.items() if npc.get('is_active')}
    transition = False
    changed_scene = bool(explicit_scene_change)
    if (plan.get('scene_changed') is True and isinstance(new_setting, str)
            and 0 < len(new_setting) <= 1200 and anchor and anchor.casefold() in evidence_text.casefold()):
        transition = new_setting != previous_setting
        changed_scene = changed_scene or transition
        if transition and close_previous_scene:
            # Unscoped legacy flags belong to the OLD scene, not every future one.
            for key in previous_active:
                roster[key].setdefault('scene_scope', previous_setting)
        room['setting'] = new_setting
        if transition or not close_previous_scene:
            event={'setting':new_setting,'previous_setting':previous_setting,'source':'auto','evidence':anchor}
            room.setdefault('history', []).append({'sender':'System','scene_event':event,
                'text':f'[CHUYỂN CẢNH]: {new_setting}'})
            if close_previous_scene:
                room.setdefault('narrative_state', {})['scene_context'] = event
    if changed_scene:
        for npc in roster.values():
            npc.pop('presence_lock_scene', None)
    updates = plan.get('presence_updates', [])
    if not isinstance(updates, list):
        if not close_previous_scene:
            return []
        updates = []
    def _norm_evidence(value: str) -> str:
        norm = str(value or '').replace('“', '"').replace('”', '"').replace("’", "'").replace("‘", "'")
        return re.sub(r'\s+', ' ', norm).strip().strip('.…').strip().casefold()

    evidence_norm = _norm_evidence(evidence_text)
    events, seen, rejected = [], set(), []
    for update in updates[:40]:
        if not isinstance(update, dict):
            continue
        npc_id = update.get('npc_id')
        if not isinstance(npc_id, str):
            continue
        action = update.get('action')
        reason = str(update.get('reason') or '').strip()
        evidence = str(update.get('evidence') or '').strip()
        ev_norm = _norm_evidence(evidence)
        npc = roster.get(npc_id)
        if (npc_id not in candidates or npc_id in seen or not isinstance(npc, dict)
                or npc_id in dead_ids or npc.get('status') == 'deceased'
                or not isinstance(action, str) or action not in {'enter','leave'} or not reason or not evidence
                or (evidence.casefold() not in evidence_text.casefold() and (not ev_norm or ev_norm not in evidence_norm))):
            rejected.append({'npc_id':npc_id, 'reason':'invalid_id_action_or_evidence'})
            continue
        seen.add(npc_id)
        active = action == 'enter'
        if 'presence_lock_scene' in npc and npc['presence_lock_scene'] == room.get('setting'):
            if bool(npc.get('is_active')) == active:
                # Reaffirming ON may repair its scope; it must not become an
                # auto-arrival or remove the player's manual lock.
                if active:
                    npc['scene_scope'] = room.get('setting', '')
                continue
            rejected.append({'npc_id':npc_id, 'reason':'manual_scene_lock'})
            continue
        entering_new_scope = (close_previous_scene and active
                              and npc.get('scene_scope',room.get('setting')) != room.get('setting'))
        if bool(npc.get('is_active')) == active and not entering_new_scope:
            npc.update(presence_source='auto', presence_reason=reason[:300])
            if active:
                npc['scene_scope'] = room.get('setting', '')
            continue
        # Bound arrivals, not departures, so a transition can close a crowded scene.
        if active and arrival_limit is not None and sum(e['action']=='enter' for e in events) >= arrival_limit:
            rejected.append({'npc_id':npc_id, 'reason':'arrival_limit'})
            continue
        npc.update(is_active=active, presence_source='auto', presence_reason=reason[:300])
        if active:
            npc['scene_scope'] = room.get('setting', '')
        event={'npc_id':npc_id, 'name':npc.get('name', npc_id), 'action':action,
               'source':'auto', 'reason':reason[:300]}
        events.append(event)
        verb = 'có mặt trong cảnh' if active else 'rời khỏi cảnh'
        room.setdefault('history', []).append({'sender':'System', 'presence_event':event,
            'text':f"[HIỆN DIỆN]: {event['name']} {verb}. {event['reason']}"})
    if transition and close_previous_scene and not defer_departures:
        # A scene switch closes old membership, not identity, memories or gates
        # elsewhere. Only cast explicitly confirmed in the new scene stays ON.
        events.extend(close_scene_membership(room,previous_active))
    room['active_character_ids'] = [key for key,npc in roster.items() if npc.get('is_active')]
    room.setdefault('narrative_state', {}).setdefault('presence', {})['last_events'] = events
    room['narrative_state']['presence']['last_validation'] = {'accepted':len(events), 'rejected':rejected[:20]}
    return events
