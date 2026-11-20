"""Experimental room-scoped perception ledger. No AI calls or save-file writes.

Statements are evidence of what was said, not proof that their content is true.
Unattributed legacy summaries never become universal NPC knowledge.
"""
import copy
import re
from uuid import uuid4
from episodic_memory import public_pieces
from prompt_context import dump, relevant_items, recent_turn_history
from scene_presence import is_scene_member, presence_name_aliases

VERSION = 'scoped_perception_v1'


def enabled(bible):
    return (bible.get('story_id') == 'muc-son-qua-hien'
            and bible.get('knowledge_isolation') == VERSION)


def present_ids(room):
    return {key for key, npc in room.get('roster', {}).items() if isinstance(npc, dict)
            and is_scene_member(room,npc)}


def _segments(entry):
    """Retain original indexes even when thought/command segments are omitted."""
    segments = entry.get('segments')
    if isinstance(segments, list) and segments:
        kinds = {'speech','action'} if entry.get('is_player') else {'dialogue','narration'}
        return [(i, s['type'], s['text']) for i, s in enumerate(segments)
                if isinstance(s, dict) and s.get('type') in kinds and isinstance(s.get('text'), str)]
    return [(i, kind, text) for i, (kind, text) in enumerate(public_pieces(entry))]


def _whisper(text):
    return bool(re.search(r'\b(thì thầm|ghé tai|nói nhỏ|nói riêng)\b', text, re.I))


def _ledger(room):
    return room.get('narrative_state', {}).get('knowledge_ledger', {})


def _event(room, scene, speaker, kind, text, observers):
    return dict(id='perception-' + uuid4().hex, scene_id=scene,
                turn=room.get('narrative_state', {}).get('scene_pacing', {}).get('turn_number', 0),
                source_id=speaker, kind='heard_statement' if kind in {'speech','dialogue'} else 'observed_action',
                # A claim is not promoted to omniscient truth, including lies/rumors.
                epistemic_status='statement_not_verified' if kind in {'speech','dialogue'} else 'observation',
                text=text, known_by=sorted(set(observers) | {'player'}))


def commit_scene(before, room, raw, decisions, public_text, directives=()):
    """Called only AFTER ordinary scene validation; atomic with the existing turn.

    Presence evidence is applied at its narrated position, not retroactively to
    all text in the turn. Optional audience_ids can narrow, never widen hearing.
    """
    ledger = room.setdefault('narrative_state', {}).setdefault('knowledge_ledger',
        {'version':1, 'current_scene_id':'scene-' + uuid4().hex, 'events':[]})
    scene = ledger['current_scene_id']
    player = next((item for item in reversed(room.get('history', [])) if item.get('is_player')), None)
    baseline = (player or {}).get('_perception_start', {})
    start_setting = baseline.get('setting', before.get('setting'))
    if ledger.get('setting', start_setting) != start_setting:
        # Also handle the existing manual /api/scene control, not only AI transitions.
        scene = 'scene-' + uuid4().hex
    observers = set(baseline.get('observers', present_ids(before)))
    roster_ids = set(room.get('roster', {}))
    observers &= roster_ids
    updates = [u for u in (raw.get('presence_updates') or []) if isinstance(u, dict)
               and u.get('npc_id') in roster_ids and u.get('action') in {'enter','leave'}
               and isinstance(u.get('evidence'), str) and u['evidence'].strip()]
    evidence = str(raw.get('scene_evidence') or '').strip()
    changed = raw.get('scene_changed') is True
    switched = False
    # If the boundary isn't locatable in prose, fail conservatively rather than
    # assuming everybody in the old scene traveled together.
    all_prose = '\n'.join(text for d in decisions for _, kind, text in _segments(d['draft']) if kind=='narration')
    boundary_in_prose = changed and evidence and evidence in all_prose
    if changed and not boundary_in_prose:
        observers.clear()
        scene = 'scene-' + uuid4().hex
        switched = True

    def transition(text, allowed):
        nonlocal scene, switched
        if changed and not switched and evidence and evidence in text:
            observers.clear()
            scene = 'scene-' + uuid4().hex
            switched = True
        for update in updates:
            marker = update['evidence']
            if marker in text and update['npc_id'] in allowed:
                if update['action']=='enter': observers.add(update['npc_id'])
                else: observers.discard(update['npc_id'])

    def record(entry, speaker, audience=None, private=False):
        scope = []
        for index, kind, text in _segments(entry):
            if not text.strip(): continue
            if kind in {'action','narration'}:
                # A new arrival does not witness preceding narration. The change
                # takes effect for subsequent speech, including in this block.
                seen = set(observers)
                if audience is not None: seen &= set(audience)
                if private or _whisper(text): seen &= set(audience or [])
                event = _event(room, scene, speaker, kind, text, seen | ({speaker} if speaker in roster_ids else set()))
                scope.append({'segment_index':index, 'event_id':event['id']})
                ledger['events'].append(event)
                transition(text, roster_ids)
                continue
            # A validated NPC speaking here is now physically present, but its
            # first line never grants access to earlier lines retroactively.
            if speaker in roster_ids: observers.add(speaker)
            heard = set(observers)
            if audience is not None: heard &= set(audience)
            if private or _whisper(text):
                heard &= set(audience or [])
            if speaker in roster_ids: heard.add(speaker)
            event = _event(room, scene, speaker, kind, text, heard)
            scope.append({'segment_index':index, 'event_id':event['id']})
            ledger['events'].append(event)
        return {'version':1, 'segments':scope}

    if player is not None:
        # Explicit public actions can establish entrances before the response.
        # Director instructions establish a scene, not a whispered conversation.
        for directive in directives:
            transition(str(directive), roster_ids)
        private = _whisper(' '.join(text for _, _, text in _segments(player)))
        target = baseline.get('target')
        audience = [target] if private and target in observers else None
        player['perception'] = record(player, 'player', audience=audience, private=private)
        player.pop('_perception_start', None)
    for index, decision in enumerate(decisions):
        source = raw.get('responses', [])[index] if index < len(raw.get('responses', [])) else {}
        audience = source.get('audience_ids') if isinstance(source, dict) else None
        audience = [i for i in audience if isinstance(i,str) and i in roster_ids] if isinstance(audience,list) else None
        private = bool(isinstance(source,dict) and source.get('visibility')=='private')
        # Whispering established by narration also narrows its following dialogue.
        private = private or _whisper(' '.join(t for _, k, t in _segments(decision['draft']) if k=='narration'))
        decision['perception'] = record(decision['draft'], decision['speaker_id'], audience, private)
    ledger['current_scene_id'] = scene
    ledger['setting'] = room.get('setting')
    # Only tiny source IDs enter the checkpoint; originals remain in this ledger/archive.
    return ledger


def _shared(event, recipients):
    return bool(recipients) and recipients <= set(event.get('known_by', []))


def project_context(context, room, query, target_id=None):
    """The scene writer gets common knowledge, not the union of private memories.

    Potential speakers included in a manifest also count: they may enter later
    in the same completion. This deliberately favors privacy over recall breadth.
    """
    result = copy.deepcopy(context)
    if 'presence_candidates' in result:
        # Retrieval includes unrelated OFF profiles to prepare possible scenes.
        # They are NOT listeners and must not suppress all private recall.
        # Ranking includes the setting and historic evidence. A name matching
        # those channels (Trúc / viện Trúc Viễn) is not a newly invited listener.
        # Only the current Player input/directives may promote a context match.
        current_input = ' '.join([str(query), *(str(d) for d in result.get('directives', []))])
        aliases = presence_name_aliases(room.get('roster', {}))
        mentioned = {npc_id for alias, npc_id in aliases.items()
                     if re.search(r'(?<!\w)' + re.escape(alias) + r'(?!\w)', current_input, re.I)}
        eligible = present_ids(room) | {c['npc_id'] for c in result['presence_candidates']
                    if c.get('event_relevant') or (c.get('context_relevant') and c['npc_id'] in mentioned)}
        if target_id: eligible.add(target_id)
        result['characters'] = [c for c in result.get('characters', []) if c['speaker_id'] in eligible]
        result['existing_temporary_profiles'] = [c for c in result.get('existing_temporary_profiles', []) if c['id'] in eligible]
        result['speaker_manifest'] = [c for c in result.get('speaker_manifest', []) if c['speaker_id'] in eligible]
    recipients = {item['speaker_id'] for item in result.get('speaker_manifest', [])
                  if item.get('speaker_id') and item['speaker_id'] != '__narrator__'}
    events = _ledger(room).get('events', [])
    by_id = {e['id']:e for e in events if isinstance(e,dict) and 'id' in e}
    shared = [e for e in events if _shared(e, recipients)]
    result.pop('continuity_memory', None)
    projected_history = []
    history = recent_turn_history(room.get('history', []))
    history_event_ids = set()
    for position, entry in enumerate(history):
        pieces = []
        perception = {item.get('segment_index'):item.get('event_id')
                      for item in entry.get('perception', {}).get('segments', []) if isinstance(item,dict)}
        current = position == len(history)-1 and entry.get('is_player')
        for i, kind, text in _segments(entry):
            event = by_id.get(perception.get(i))
            # Legacy NPC statements are known to their own speaker only. Never
            # reconstruct listeners from a current ON switch or proximity guess.
            legacy_self = (not perception and not entry.get('is_player')
                           and recipients == {entry.get('speaker_id')})
            if current or (event and _shared(event, recipients)) or legacy_self:
                pieces.append({'type':kind, 'text':text})
                if event: history_event_ids.add(event['id'])
        if pieces:
            projected_history.append({'speaker_id':'player' if entry.get('is_player') else entry.get('speaker_id'),
                                      'segments':pieces})
    result['history'] = projected_history
    # Current working history is authoritative for scene position. Recall only
    # older source events, avoiding duplicate copies competing with that history.
    recalled = relevant_items([e for e in shared if e['id'] not in history_event_ids], query, 8, 3)
    bounded = []
    for event in reversed(recalled):
        if len(dump(bounded + [event]).encode('utf-8')) <= 7000: bounded.insert(0, event)
    result['memory'] = {'scoped_events':bounded}
    # These are global/unattributed old channels, not proof of NPC access.
    result['recent_personnel_evidence'] = '\n'.join(s['text'] for e in projected_history for s in e['segments'])[-5000:]
    for item in result.get('characters', []):
        actor = item['speaker_id']
        item['knowledge'] = [e['id'] for e in bounded if actor in e.get('known_by', [])]
        item['private_memory_withheld'] = sum(actor in e.get('known_by', []) and not _shared(e,recipients) for e in events)
        item.pop('personal_lore', None)
    for item in result.get('existing_temporary_profiles', []):
        item.pop('personal_lore', None)
    if isinstance(result.get('story_guidance'), dict):
        result['story_guidance'].pop('established_evidence', None)
    # Author-only old-family secrets must not be presented as public world law.
    result['world_rules'] = [block for block in result.get('world_rules', [])
                             if not any(k in str(block.get('path','')) for k in ('family_past','truth_keys','secret'))]
    result['knowledge_boundary'] = {
        'version':1, 'recipients':sorted(recipients),
        'policy':'scoped_events là lời được nghe/hành động được thấy, không tự là sự thật. ID không có trong đó không phải quyền biết. Bí mật riêng không gửi vào lượt viết chung; không bịa người báo tin để lấp chỗ trống. Story plan/profile là chỉ dẫn tác giả, không tự thành tri thức NPC.',
        'legacy_policy':'Không phát tán summary/facts cũ chưa có người biết. Việc hiện tại vẫn được phản ứng theo lời Player mới nhất.'}
    return result


def checkpoint_metadata(chunk):
    ids = list(dict.fromkeys(item.get('event_id') for entry in chunk
        for item in entry.get('perception', {}).get('segments', [])
        if isinstance(item,dict) and item.get('event_id')))
    return {'version':1, 'event_ids':ids, 'policy':'summary_is_director_only_not_shared_knowledge'}
