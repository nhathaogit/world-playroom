"""Local, bounded story retrieval. An offered plot beat is never an outcome.

No provider, save-file writes or clock-based forced events live in this module.
Only the normal tracker proposes timing; the backend verifies IDs and evidence.
"""
from pathlib import Path
import random
import re
import unicodedata

CLOSED = {'completed', 'superseded'}
PLAYED_STATUSES = CLOSED | {'active', 'planted'}
COOLDOWN = 10
TIER_ALIASES = {
    'small': 'small', 'nho': 'small', 'minor': 'small', 'slice': 'small',
    'medium': 'medium', 'vua': 'medium', 'moderate': 'medium', 'mid': 'medium',
    'large': 'large', 'lon': 'large', 'major': 'large', 'climax': 'large',
}
TIER_LABELS = {'small': 'Nhỏ', 'medium': 'Vừa', 'large': 'Lớn'}


def normalize_tier(value):
    key = _plain(value or 'small').strip()
    return TIER_ALIASES.get(key, 'small')


def _plain(text):
    return ''.join(c for c in unicodedata.normalize('NFD', str(text).casefold())
                   if not unicodedata.combining(c)).replace('đ', 'd')


def wants_next(directives):
    return any(re.search(r'\b(?:(?:chuyen (?:den|sang)|chay|mo) (?:dien bien|su kien|main event|arc)(?: chinh| lon| cot truyen)* tiep theo|tiep tuc cot truyen)\b',
                         _plain(d)) for d in directives)


def _match_tiered_directive(events, directives, state=None):
    """Resolve explicit director commands [[...]] against tiered story events or recent suggestions."""
    if not directives:
        return None
    director = (state or {}).get('story_director', {}) if isinstance(state, dict) else {}
    records = director.get('events', {}) if isinstance(director, dict) else {}
    played_ids = set(director.get('played_event_ids', [])) if isinstance(director, dict) else set()
    tiered = {
        e['id']: e for e in events
        if e.get('is_tiered_event') and e.get('selectable', True)
        and e['id'] not in played_ids
        and records.get(e['id'], {}).get('status') not in PLAYED_STATUSES
    }
    if not tiered:
        return None
    raw_directives = [str(d).strip() for d in directives if str(d).strip()]
    joined_plain = _plain(' '.join(raw_directives))
    if re.search(r'\b(?:khong|dung|chua)\s+(?:chuyen|bat dau|buoc vao|vao|mo|kich hoat)\b', joined_plain):
        return None

    # 1. Explicit ID tag or exact ID token inside directive
    explicit_tags = _explicit_id_tags(raw_directives)
    if explicit_tags:
        for match in explicit_tags:
            if match in tiered:
                return match
        return None
    for d in raw_directives:
        for ident in tiered:
            if re.search(rf'(?<![\w.-]){re.escape(ident)}(?![\w.-])', d):
                return ident

    # 2. Match against last model-generated suggestions stored on state
    last_suggestions = director.get('last_suggestions', []) if isinstance(director, dict) else []
    for d in raw_directives:
        d_plain = _plain(re.sub(r'^bat dau su kien[:\s-]*', '', _plain(d)).strip())
        if not d_plain:
            continue
        for item in last_suggestions:
            if not isinstance(item, dict):
                continue
            eid = item.get('event_id')
            if eid not in tiered:
                continue
            for field in (item.get('director_command', ''), item.get('title', '')):
                f_plain = _plain(re.sub(r'^bat dau su kien[:\s-]*', '', _plain(field)).strip())
                if f_plain and (d_plain == f_plain or (len(f_plain) >= 10 and (f_plain in d_plain or d_plain in f_plain))):
                    return eid

    # 3. Direct title / director_command / bigram overlap across eligible tiered events
    stop = {'chuyen','den','sang','ngay','buoi','mot','cua','cho','khi','vao','thi',
            'nhung','nhan','vat','dien','bien','su','kien','tiep','theo','chinh','arc',
            'drama','bat','dau','cao','trao','moi','toi','lai','duoc','hay','ve','tai',
            'trong','cung','voi','nho','vua','lon','lenh','dao','nha'}
    def terms(value):
        return [w for w in re.findall(r'\w+', _plain(value)) if len(w) >= 2 and w not in stop]

    query = terms(joined_plain)
    if not query:
        return None
    q_bigrams = set(zip(query, query[1:]))
    ranked = []
    for eid, event in tiered.items():
        title_plain = _plain(event.get('title', ''))
        cmd_plain = _plain(re.sub(r'^bat dau su kien[:\s-]*', '', _plain(event.get('director_command', ''))).strip())
        for d_plain in (_plain(x) for x in raw_directives):
            if title_plain and len(title_plain) >= 8 and title_plain in d_plain:
                return eid
            if cmd_plain and len(cmd_plain) >= 8 and cmd_plain in d_plain:
                return eid
        t_words = terms(event.get('title', '') + ' ' + event.get('director_command', ''))
        b_words = terms(event.get('summary', '') + ' ' + event.get('content', ''))
        all_words = t_words + b_words
        shared_title = set(query) & set(t_words)
        shared_all = set(query) & set(all_words)
        phrases = q_bigrams & set(zip(all_words, all_words[1:]))
        title_ratio = len(shared_title) / max(1, len(set(t_words)))
        if (len(shared_title) >= 2 and title_ratio >= 0.45) or (len(shared_all) >= 2 and phrases):
            score = 3 * len(shared_title) + len(shared_all) + 3 * len(phrases)
            ranked.append((score, eid))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    if not ranked or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
        return None
    return ranked[0][1]


def _explicit_id_tags(directives):
    tags = []
    for d in directives or []:
        for match in re.findall(r'\[id:([a-zA-Z0-9_.-]+)\]', str(d)):
            if match not in tags:
                tags.append(match)
    return tags


def _match_arc_beat_directive(events, directives, state=None):
    """Resolve explicit [id:...] directives for ordered arc beats.

    Returns (matched_id, saw_explicit_arc_tag). When saw_explicit_arc_tag is True
    and matched_id is None, the requested arc beat is locked/already closed and must
    not fall back to fuzzy matching.
    """
    if not directives:
        return None, False
    raw_directives = [str(d).strip() for d in directives if str(d).strip()]
    joined_plain = _plain(' '.join(raw_directives))
    arc_ids = {e['id'] for e in events if not e.get('is_tiered_event')}
    explicit_arc_tags = [tag for tag in _explicit_id_tags(raw_directives) if tag in arc_ids]
    if not explicit_arc_tags:
        return None, False
    if re.search(r'\b(?:khong|dung|chua)\s+(?:chuyen|bat dau|buoc vao|vao|mo|kich hoat)\b', joined_plain):
        return None, True
    actionable = next_actionable_arc_beat_ids(events, state)
    for tag in explicit_arc_tags:
        if tag in actionable:
            return tag, True
    return None, True


def requested_event(events, candidates, directives, state=None):
    """Resolve a named director transition against tiered story_events, ordered arc beats, or the ready frontier.

    Ordinary speech, unrelated travel and vague 'drama' never select a beat.
    Retrieval does not complete prerequisites or fast-forward to a climax.
    """
    arc_match, saw_arc_tag = _match_arc_beat_directive(events, directives, state=state)
    if saw_arc_tag:
        return arc_match
    tiered_match = _match_tiered_directive(events, directives, state=state)
    if tiered_match:
        return tiered_match
    text = _plain(' '.join(str(d) for d in directives))
    if re.search(r'\b(?:khong|dung|chua)\s+(?:chuyen|bat dau|buoc vao|vao)\b', text):
        return None
    if not re.search(r'\b(?:chuyen (?:den|sang)|den (?:sang|ngay|buoi)|buoc vao|vao (?:dai |cao trao|arc)|bat dau)\b', text):
        return None
    stop = {'chuyen','den','sang','ngay','buoi','mot','cua','cho','khi','vao','thi',
            'nhung','nhan','vat','dien','bien','su','kien','tiep','theo','chinh','arc',
            'drama','bat','dau','cao','trao','moi','chuyen','toi','lai','duoc'}
    def terms(value):
        return [word for word in re.findall(r'\w+', _plain(value)) if word not in stop]
    query = terms(text)
    bigrams = set(zip(query, query[1:]))
    ranked = []
    known = {e['id']:e for e in events}
    for item in candidates:
        if not item.get('ready') or not item.get('selectable', True):
            continue
        event = known[item['id']]
        words = terms(event['title']+' '+event.get('content', ''))
        shared = set(query) & set(words)
        phrases = bigrams & set(zip(words, words[1:]))
        if len(shared) >= 2 and phrases:
            ranked.append((len(shared)+2*len(phrases), event['id']))
    ranked.sort(key=lambda pair:pair[0], reverse=True)
    # Ambiguous destinations should not silently choose a plot for the player.
    if not ranked or (len(ranked)>1 and ranked[0][0]==ranked[1][0]):
        return None
    return ranked[0][1]


def _brief(text, limit=170):
    text = re.sub(r'\s+', ' ', str(text)).strip()
    return text if len(text) <= limit else text[:limit].rsplit(' ', 1)[0] + '…'


def load_events(room_dir, bible):
    """Index actual source sections, not chapter numbers or assumed outcomes.

    Only known source documents below the room's source/ folder can be read.
    Sections too large to fit a beat fall back to the hook, never clipped prose.
    """
    if bible.get('session_mode') == 'companion':
        return []
    root = (Path(room_dir) / 'source').resolve()
    events = []
    labels = {'Setup':'setup', 'Rising Action':'rising', 'Climax':'climax', 'Resolution':'resolution'}
    for arc_id, arc in bible.get('arcs', {}).items():
        if not isinstance(arc, dict) or arc.get('kind') == 'suggested_macro':
            continue
        hook = str(arc.get('hook') or arc.get('summary') or '').strip()
        source = str(arc.get('source_document') or '')
        sections = []
        cast_ids = list(arc.get('characters', [])) if isinstance(arc.get('characters'), list) else []
        try:
            path = (Path(room_dir) / source).resolve()
            if source and path.is_relative_to(root) and path.is_file() and path.stat().st_size <= 160000:
                text = path.read_text(encoding='utf-8-sig')
                front = re.match(r'\A---\s*\n(.*?)\n---', text, re.S)
                if front:
                    cast = re.search(r'^characters:\s*\n((?:[ \t]+-[^\n]+\n?)+)', front[1], re.M)
                    if cast:
                        cast_ids.extend(re.findall(r'^\s*-\s*([\w-]+)\s*$', cast[1], re.M))
                for match in re.finditer(r'^## (Setup|Rising Action|Climax|Resolution)\s*\n(.*?)(?=^## |\Z)', text, re.M | re.S):
                    kind, body = labels[match[1]], match[2].strip()
                    parts = re.split(r'^\d+\.\s+', body, flags=re.M) if kind == 'rising' else [body]
                    for part in parts:
                        if part.strip():
                            sections.append((kind, part.strip()))
        except (OSError, ValueError):
            pass
        structured = not sections and bool(arc.get('set_pieces') or arc.get('completion'))
        if not sections and hook:
            sections = [('setup', hook)]
        if structured:
            sections.extend(('rising', str(piece)) for piece in arc.get('set_pieces', [])
                            if isinstance(piece, str) and piece.strip())
            if arc.get('completion'):
                sections.append(('resolution', 'Điều kiện khép tuyến, chỉ ghi nhận nếu đã diễn: '+str(arc['completion'])))
        counts, previous = {}, []
        for kind, body in sections[:20]:
            counts[kind] = counts.get(kind, 0) + 1
            suffix = f'{kind}{counts[kind]:02}' if kind == 'rising' or counts[kind] > 1 else kind
            event_id = f'{arc_id}.{suffix}'
            if len(body.encode('utf-8')) > 4200:
                body = hook if len(hook.encode('utf-8')) <= 4200 else ''
            if not body:
                continue
            event = {'id':event_id, 'arc_id':arc_id,
                'title':f"{arc.get('title', arc_id)} — {suffix}", 'summary':_brief(body),
                'content':body, 'requires':previous[-1:], 'source':source}
            if cast_ids:
                event['cast_ids'] = list(dict.fromkeys(cast_ids))[:24]
            if bible.get('arc_policy', {}).get('event_flow') == 'parallel':
                event['event_flow'] = 'parallel'
            if structured:
                event.update(selectable=kind!='resolution', arc_completion=kind=='resolution',
                             optional=kind=='rising')
                # Set pieces are alternatives, not a checklist for affection.
                # A real agreement may resolve a route without playing them all.
                event['requires'] = [f'{arc_id}.setup'] if kind=='rising' and hook else []
                event['content'] += '\nMục tiêu cảm xúc: '+str(arc.get('emotional_goal', ''))
                if kind != 'resolution' and arc.get('pressure'):
                    event['content'] += '\nSức ép gợi ý, không ép kết quả: '+str(arc['pressure'])
            events.append(event)
            previous.append(event_id)

    raw_pool = bible.get('story_events')
    if isinstance(raw_pool, (list, dict)):
        raw_items = []
        if isinstance(raw_pool, dict):
            for tier_key, bucket in raw_pool.items():
                if isinstance(bucket, list):
                    for entry in bucket:
                        if isinstance(entry, dict):
                            merged = dict(entry)
                            merged.setdefault('tier', tier_key)
                            raw_items.append(merged)
        else:
            raw_items = [item for item in raw_pool if isinstance(item, dict)]
        for idx, item in enumerate(raw_items, 1):
            title = str(item.get('title') or '').strip()
            body = str(item.get('content') or item.get('summary') or title).strip()
            if not title or not body:
                continue
            tier = normalize_tier(item.get('tier', 'small'))
            eid = str(item.get('id') or f'tiered.{tier}.{idx:02}').strip()
            director_cmd = str(item.get('director_command') or f'Bắt đầu sự kiện: {title}').strip()
            ev = {
                'id': eid,
                'arc_id': str(item.get('arc_id') or 'tiered_pool'),
                'tier': tier,
                'tier_label': TIER_LABELS.get(tier, 'Nhỏ'),
                'title': title,
                'summary': _brief(item.get('summary') or body, 180),
                'content': body,
                'director_command': director_cmd,
                'location': str(item.get('location') or '').strip(),
                'requires': list(item.get('requires', [])) if isinstance(item.get('requires'), list) else [],
                'tags': [str(t) for t in item.get('tags', []) if isinstance(t, str)] if isinstance(item.get('tags'), list) else [],
                'selectable': bool(item.get('selectable', True)),
                'is_tiered_event': True,
            }
            if isinstance(item.get('cast_ids'), list):
                ev['cast_ids'] = [str(c) for c in item['cast_ids'] if isinstance(c, str)][:24]
            events.append(ev)
    return events


def is_event_played(event_id, state):
    director = (state or {}).get('story_director', {}) if isinstance(state, dict) else {}
    if not isinstance(director, dict):
        return False
    if event_id in director.get('played_event_ids', []):
        return True
    status = director.get('events', {}).get(event_id, {}).get('status')
    return status in PLAYED_STATUSES


def eligible_tiered_events(events, state):
    """Return unplayed tiered events whose prerequisites are satisfied."""
    director = _director(state)
    records = director.get('events', {})
    turn = _turn(state)
    return [
        e for e in events
        if e.get('is_tiered_event') and e.get('selectable', True)
        and not is_event_played(e['id'], state)
        and _ready(e, records, turn, explicit=True)
    ]


def sample_tiered_pool(events, state, per_tier=8, seed=None):
    """Sample randomized unplayed events from small/medium/large tiers for model prompt."""
    rng = random.Random(seed) if seed is not None else random
    pool = eligible_tiered_events(events, state)
    by_tier = {t: [] for t in ('small', 'medium', 'large')}
    for e in pool:
        by_tier.setdefault(normalize_tier(e.get('tier')), []).append(e)
    sampled = []
    for t in ('small', 'medium', 'large'):
        bucket = list(by_tier[t])
        rng.shuffle(bucket)
        sampled.extend(bucket[:per_tier])
    return sampled


def pick_fallback_suggestions(events, state, count=10, seed=None):
    """Pick ~count randomized unplayed events ensuring small, medium, and large coverage."""
    rng = random.Random(seed) if seed is not None else random
    pool = eligible_tiered_events(events, state)
    if not pool:
        return []
    by_tier = {'small': [], 'medium': [], 'large': []}
    for e in pool:
        by_tier[normalize_tier(e.get('tier'))].append(e)
    for t in by_tier:
        rng.shuffle(by_tier[t])
    quotas = {'small': 4, 'medium': 4, 'large': 2} if count >= 10 else {'small': 3, 'medium': 3, 'large': 2}
    chosen = []
    seen_ids = set()
    for t in ('small', 'medium', 'large'):
        for e in by_tier[t][:quotas.get(t, 1)]:
            if e['id'] not in seen_ids:
                chosen.append(e)
                seen_ids.add(e['id'])
    leftovers = [e for e in pool if e['id'] not in seen_ids]
    rng.shuffle(leftovers)
    while len(chosen) < count and leftovers:
        e = leftovers.pop()
        chosen.append(e)
        seen_ids.add(e['id'])
    rng.shuffle(chosen)
    return [{
        'event_id': e['id'],
        'tier': normalize_tier(e.get('tier')),
        'tier_label': TIER_LABELS.get(normalize_tier(e.get('tier')), 'Nhỏ'),
        'title': e['title'],
        'teaser': e['summary'],
        'director_command': e.get('director_command') or f"Bắt đầu sự kiện: {e['title']}",
        'location': e.get('location', ''),
        'cast_ids': list(e.get('cast_ids', [])),
    } for e in chosen]


def _clean_arc_beat_body(event):
    body = str(event.get('content') or event.get('summary') or event.get('title') or '').strip()
    body = re.split(r'\n(?:Mục tiêu cảm xúc|Sức ép gợi ý, không ép kết quả):', body)[0].strip()
    body = re.sub(r'^Điều kiện khép tuyến, chỉ ghi nhận nếu đã diễn:\s*', '', body).strip()
    return body


def _arc_beat_label(event):
    eid = str(event.get('id') or '')
    suffix = eid.split('.', 1)[1] if '.' in eid else ''
    if event.get('arc_completion') or suffix == 'resolution':
        return 'resolution', 'Khép Arc'
    if suffix.startswith('setup'):
        return 'setup', 'Mở màn'
    if suffix.startswith('climax'):
        return 'climax', 'Cao trào'
    match = re.match(r'^rising(\d+)$', suffix)
    if match:
        return 'rising', f'Chặng {int(match.group(1))}'
    return 'rising', 'Diễn biến'


def _is_sequential_arc_chain(arc_order, arc_events):
    if not arc_order:
        return False
    if not any(e.get('event_flow') == 'parallel' for e in arc_events):
        return True
    return all(re.match(r'^\d+[_-]', aid) for aid in arc_order)


def ordered_arc_roadmap(events, bible=None, state=None):
    """Build an ordered Arc roadmap where beats inside each arc unlock step-by-step."""
    if isinstance(bible, dict) and bible.get('session_mode') == 'companion':
        return {'macro_arc': None, 'sequential_arcs': False, 'active_arc': '', 'completed_count': 0, 'total_count': 0, 'arcs': []}
    arc_events = [e for e in (events or []) if not e.get('is_tiered_event')]
    if not arc_events:
        return {'macro_arc': None, 'sequential_arcs': False, 'active_arc': '', 'completed_count': 0, 'total_count': 0, 'arcs': []}

    arcs_dict = bible.get('arcs', {}) if isinstance(bible, dict) else {}
    macro_arc = None
    for aid, arc in arcs_dict.items():
        if isinstance(arc, dict) and arc.get('kind') == 'suggested_macro':
            macro_arc = {'id': aid, 'title': arc.get('title', aid), 'hook': _brief(arc.get('hook', ''), 240)}
            break

    arc_order = []
    if arcs_dict:
        for aid, arc in arcs_dict.items():
            if isinstance(arc, dict) and arc.get('kind') != 'suggested_macro' and any(e['arc_id'] == aid for e in arc_events):
                arc_order.append(aid)
    for e in arc_events:
        if e['arc_id'] not in arc_order:
            arc_order.append(e['arc_id'])

    sequential_arcs = _is_sequential_arc_chain(arc_order, arc_events)
    state_dict = state if isinstance(state, dict) else {}
    records = state_dict.get('story_director', {}).get('events', {}) if isinstance(state_dict.get('story_director'), dict) else {}
    completed_arcs = set(state_dict.get('completed_arcs', []) or [])
    unlocked_list = state_dict.get('unlocked_arcs', []) or []
    unlocked_set = set(unlocked_list) if isinstance(unlocked_list, list) else set()
    active_arc = str(state_dict.get('active_arc') or '')

    arcs_out = []
    for idx, aid in enumerate(arc_order):
        beats = [e for e in arc_events if e['arc_id'] == aid]
        arc_meta = arcs_dict.get(aid, {}) if isinstance(arcs_dict.get(aid), dict) else {}
        all_beats_closed = bool(beats) and all(records.get(b['id'], {}).get('status') in CLOSED for b in beats)
        is_completed = (aid in completed_arcs) or all_beats_closed
        in_unlocked = (not unlocked_set) or (aid in unlocked_set)

        if sequential_arcs and idx > 0:
            prev_completed = arcs_out[idx - 1]['is_completed']
            is_unlocked = in_unlocked and (prev_completed or aid == active_arc)
            arc_lock_reason = '' if is_unlocked else (
                f'Hoàn thành Arc {idx} để mở' if in_unlocked and not prev_completed else 'Tuyến truyện chưa mở khóa'
            )
        else:
            is_unlocked = in_unlocked
            arc_lock_reason = '' if is_unlocked else 'Tuyến truyện chưa mở khóa'

        steps = []
        prev_steps_closed = True
        completed_steps = 0
        next_step_id = None

        for step_idx, beat in enumerate(beats):
            bid = beat['id']
            rec_status = records.get(bid, {}).get('status', 'unseen')
            kind, step_label = _arc_beat_label(beat)
            clean_body = _clean_arc_beat_body(beat)
            short_title = _brief(clean_body, 96)
            step_summary = _brief(clean_body, 220)

            if is_completed or rec_status in CLOSED:
                step_status = 'completed'
                actionable = False
                step_lock_reason = ''
                completed_steps += 1
            elif rec_status in {'active', 'planted'}:
                step_status = 'active'
                actionable = True
                step_lock_reason = ''
                if next_step_id is None:
                    next_step_id = bid
                prev_steps_closed = False
            elif is_unlocked and prev_steps_closed:
                step_status = 'next'
                actionable = True
                step_lock_reason = ''
                next_step_id = bid
                prev_steps_closed = False
            else:
                step_status = 'locked'
                actionable = False
                step_lock_reason = arc_lock_reason if not is_unlocked else f'Hoàn thành Bước {step_idx} để mở'
                prev_steps_closed = False

            steps.append({
                'event_id': bid,
                'arc_id': aid,
                'step_number': step_idx + 1,
                'kind': kind,
                'step_label': step_label,
                'title': short_title,
                'summary': step_summary,
                'status': step_status,
                'actionable': actionable,
                'lock_reason': step_lock_reason,
                'director_command': f'Bắt đầu chặng Arc [id:{bid}]: {_brief(clean_body, 75)}',
                'cast_ids': list(beat.get('cast_ids', [])),
            })

        arc_status = 'completed' if is_completed else ('active' if aid == active_arc and is_unlocked else ('unlocked' if is_unlocked else 'locked'))
        arcs_out.append({
            'arc_id': aid,
            'order': idx + 1,
            'title': str(arc_meta.get('title') or aid),
            'summary': _brief(arc_meta.get('summary') or arc_meta.get('hook') or '', 220),
            'status': arc_status,
            'is_active': aid == active_arc,
            'is_unlocked': is_unlocked,
            'is_completed': is_completed,
            'lock_reason': arc_lock_reason,
            'completed_steps': completed_steps,
            'total_steps': len(steps),
            'next_step_id': next_step_id,
            'steps': steps,
        })

    if active_arc and any(a['arc_id'] == active_arc and a['is_completed'] for a in arcs_out):
        first_open = next((a for a in arcs_out if a['is_unlocked'] and not a['is_completed']), None)
        if first_open:
            active_arc = first_open['arc_id']
            for a in arcs_out:
                a['is_active'] = (a['arc_id'] == active_arc)
                if a['is_active'] and a['status'] == 'unlocked':
                    a['status'] = 'active'

    return {
        'macro_arc': macro_arc,
        'sequential_arcs': sequential_arcs,
        'active_arc': active_arc,
        'completed_count': sum(1 for a in arcs_out if a['is_completed']),
        'total_count': len(arcs_out),
        'arcs': arcs_out,
    }


def next_actionable_arc_beat_ids(events, state=None):
    roadmap = ordered_arc_roadmap(events, None, state)
    return {
        step['event_id']
        for arc in roadmap['arcs']
        for step in arc['steps']
        if step.get('actionable')
    }


def advance_active_arc_if_completed(events, bible, state):
    """Advance active_arc to the next unlocked uncompleted arc once the current one finishes."""
    if not isinstance(state, dict):
        return
    roadmap = ordered_arc_roadmap(events, bible, state)
    current = state.get('active_arc')
    current_entry = next((a for a in roadmap['arcs'] if a['arc_id'] == current), None)
    if current_entry and not current_entry['is_completed']:
        return
    next_open = next((a for a in roadmap['arcs'] if a['is_unlocked'] and not a['is_completed']), None)
    if next_open:
        state['active_arc'] = next_open['arc_id']


def mark_event_started(state, event, evidence='', turn=None, events=None, bible=None):
    """Persist that an explicit story event or ordered arc beat has started/occurred."""
    if not isinstance(state, dict) or not isinstance(event, dict):
        return False
    eid = event.get('id')
    if not eid:
        return False
    director = _director(state)
    turn_num = _turn(state) if turn is None else int(turn)
    played = director.setdefault('played_event_ids', [])
    if eid not in played:
        played.append(eid)
    note = str(evidence or event.get('summary') or event.get('title') or eid).strip()[:500]
    existing = director['events'].get(eid, {})
    if existing.get('status') != 'completed':
        director['events'][eid] = {
            'status': 'completed',
            'tier': normalize_tier(event.get('tier')) if event.get('is_tiered_event') else 'arc',
            'evidence': note,
            'started_turn': turn_num,
            'updated_turn': turn_num,
            'source': 'director_command',
        }
    if not event.get('is_tiered_event') and event.get('arc_id'):
        arc_id = event['arc_id']
        if event.get('arc_completion'):
            completed = state.setdefault('completed_arcs', [])
            if arc_id not in completed:
                completed.append(arc_id)
            if events is not None:
                advance_active_arc_if_completed(events, bible, state)
        else:
            unlocked = state.get('unlocked_arcs')
            if not unlocked or arc_id in unlocked:
                state['active_arc'] = arc_id
    if isinstance(director.get('last_suggestions'), list):
        director['last_suggestions'] = [
            item for item in director['last_suggestions']
            if not (isinstance(item, dict) and item.get('event_id') == eid)
        ]
    return True


def _director(state):
    value = state.get('story_director')
    if not isinstance(value, dict):
        value = {'version':1, 'events':{}, 'pending':None}
        state['story_director'] = value
    if not isinstance(value.get('events'), dict):
        value['events'] = {}
    return value


def _turn(state):
    return int(state.get('scene_pacing', {}).get('turn_number', 0))


def _ready(event, records, turn, explicit=False):
    record = records.get(event['id'], {})
    if record.get('status') in CLOSED:
        return False
    if record.get('status') == 'deferred' and not explicit and turn < record.get('retry_after', 0):
        return False
    return all(records.get(key, {}).get('status') in CLOSED for key in event.get('requires', []))


def event_index(events, state):
    """Bounded director catalogue with review context, not the full source corpus."""
    director = state.get('story_director', {})
    records = director.get('events', {})
    active = state.get('active_arc')
    events = [e for e in events if not e.get('is_tiered_event') and e['arc_id'] not in state.get('completed_arcs', [])]
    arcs = list(dict.fromkeys(e['arc_id'] for e in events))
    parallel = any(e.get('event_flow') == 'parallel' for e in events)
    if parallel:
        unlocked = state.get('unlocked_arcs', arcs)
        eligible = [e for e in events if e['arc_id'] in unlocked
                    and records.get(e['id'], {}).get('status') not in CLOSED]
        current = [e for e in eligible if e['arc_id'] == active]
        if not current and eligible:
            active = eligible[0]['arc_id']
            current = [e for e in eligible if e['arc_id'] == active]
        # Review the current goal AND an actionable beat; show other unlocked
        # routes without forcing the player to finish this one first.
        action = next((e for e in current if e.get('selectable', True) and _ready(e, records, _turn(state))), None)
        current = ([action] if action else []) + [e for e in current if e.get('arc_completion')]
        for arc_id in dict.fromkeys(e['arc_id'] for e in eligible if e['arc_id'] != active):
            alternative = next((e for e in eligible if e['arc_id']==arc_id and e.get('selectable', True)
                                and _ready(e, records, _turn(state))), None)
            if alternative:
                current.append(alternative)
        remaining = current[:10]
        return [_index_entry(e, records, state, include_review=e['arc_id']==active) for e in remaining]
    if active not in arcs:
        # A macro is a container, not another replay of its child threads.
        unlocked = state.get('unlocked_arcs', arcs)
        active = next((key for key in arcs if key in unlocked and any(
            records.get(e['id'], {}).get('status') not in CLOSED for e in events if e['arc_id']==key)), None)
    current = [e for e in events if e['arc_id'] == active]
    if current and all(records.get(e['id'], {}).get('status') in CLOSED for e in current):
        later = arcs[arcs.index(active)+1:]
        unlocked = state.get('unlocked_arcs', arcs)
        next_arc = next((key for key in later if key in unlocked and any(
            records.get(e['id'], {}).get('status') not in CLOSED for e in events if e['arc_id']==key)), None)
        current = [e for e in events if e['arc_id'] == next_arc]
    remaining = [e for e in current if records.get(e['id'], {}).get('status') not in CLOSED]
    return [_index_entry(e, records, state) for e in remaining[:4]]


def _index_entry(e, records, state, include_review=True):
    return {'id':e['id'], 'arc_id':e['arc_id'], 'title':_brief(e['title'], 100),
             'summary':e['summary'], 'status':records.get(e['id'], {}).get('status', 'unseen'),
             'review_context':_brief(e.get('content', e['summary']), 800) if include_review else e['summary'],
             'selectable':e.get('selectable', True), 'arc_completion':e.get('arc_completion', False),
             'ready':_ready(e, records, _turn(state)), 'requires':e.get('requires', [])}


def story_guidance(events, bible, state):
    """Project the parent arc and nearby threads without treating plans as facts.

    Macro arcs remain containers, not replayable beats. Ordering follows the
    authored arc map; only the current macro's children enter the small roadmap.
    """
    if not events or bible.get('session_mode') == 'companion':
        return {}
    index = event_index(events, state)
    current = index[0]['arc_id'] if index else state.get('active_arc')
    parent, families = None, {}
    arcs = bible.get('arcs', {})
    for ident, arc in arcs.items():
        if not isinstance(arc, dict):
            continue
        if arc.get('kind') == 'suggested_macro':
            parent = ident
        else:
            families[ident] = arc.get('parent_arc') or parent
    macro_id = families.get(current)
    macro = arcs.get(macro_id, {})
    if macro_id:
        children = [key for key in families if families[key] == macro_id]
    elif any(e.get('event_flow')=='parallel' for e in events):
        unlocked = state.get('unlocked_arcs', list(arcs))
        children = [current] + [key for key in families if key != current and key in unlocked]
    else:
        children = [current]
    records = state.get('story_director', {}).get('events', {})
    def status(key):
        if key in state.get('completed_arcs', []):
            return 'closed'
        beats = [e for e in events if e['arc_id'] == key and not e.get('is_tiered_event')]
        if beats and all(records.get(e['id'], {}).get('status') in CLOSED for e in beats):
            return 'closed'
        return 'current' if key == current else 'unresolved_plan'
    return {'macro_arc':{'id':macro_id, 'title':macro.get('title', ''),
                         'premise':_brief(macro.get('hook', ''), 550)},
            'current_thread':current,
            'threads':[{'id':key, 'title':arcs.get(key, {}).get('title', key), 'status':status(key)}
                       for key in children[:8]],
            # The writer needs route IDs and opportunities, not a duplicate of
            # tracker review text, dependency chains and roadmap titles.
            'next_beats':[{key:value for key,value in item.items() if key in {
                'id','arc_id','summary','status','ready','selectable','arc_completion'}} for item in index],
            'policy':'Đây là kế hoạch gợi ý, không phải lịch sử. Đối chiếu ký ức để nhận diện nhịp đã qua; '
                     'không reset mở truyện hoặc tự coi nhịp bị bỏ qua là hoàn tất. Chủ động gieo diễn biến '
                     'liên quan khi có điểm nghỉ hoặc Player muốn tiến truyện; đang đối thoại thì theo mạch '
                     'đó. Không ép Player điều tra hay kết mỗi lượt bằng câu hỏi chọn lựa.'}


def reconciliation_context(events, state, sources, max_bytes=6000):
    """Retrieve past public outcomes by beat, not merely the latest chat tail.

    This only supplies evidence to the existing tracker. It never changes event
    status itself and never reads raw/private transcripts or planned story text.
    """
    pool = []
    def collect(value):
        if isinstance(value, str) and value.strip() and value not in pool:
            pool.append(value)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    for key in ('facts', 'milestones', 'known_outcomes'):
        collect(state.get(key, []))
    for source in sources:
        collect(source)
    index = event_index(events, state)
    queries = [set(re.findall(r'\w{3,}', _plain(item.get('review_context', '')+' '+item['summary'])))
               for item in index]
    selected, used = [], 0
    # Round robin avoids one old beat monopolizing reconciliation memory.
    rankings = [sorted(pool, key=lambda text: len(terms & set(re.findall(r'\w{3,}', _plain(text)))),
                       reverse=True)[:4] for terms in queries]
    for rank in range(4):
        for candidates in rankings:
            if rank >= len(candidates):
                continue
            text = candidates[rank]
            size = len(text.encode('utf-8')) + 1
            if text not in selected and used + size <= max_bytes:
                selected.append(text)
                used += size
    return '\n'.join(selected)


def select_event(events, state, room, directives, public_text):
    """Consume an AI proposal or explicit director command, not ordinary speech.

    Returning content does not mark it played. The writer must still re-check the
    NEW player input and may decline/defer this optional beat rather than railroad.
    """
    if not events:
        return None
    director = _director(state)
    director.pop('offered_event_id', None)
    pending = director.pop('pending', None)
    candidates = event_index(events, state)
    named = requested_event(events, candidates, directives, state=state)
    explicit_arc_match, _ = _match_arc_beat_directive(events, directives, state=state)
    explicit = wants_next(directives) or named is not None
    if named:
        selected_id = named
    elif explicit:
        selected_id = next((e['id'] for e in candidates if e.get('selectable', True) and _ready(
            next(item for item in events if item['id']==e['id']), director['events'], _turn(state), True)), None)
        if not selected_id:
            tiered_pool = eligible_tiered_events(events, state)
            if tiered_pool:
                selected_id = random.choice(tiered_pool)['id']
    else:
        if directives or not isinstance(pending, dict) or pending.get('setting') != room.get('setting'):
            return None
        if _turn(state) - pending.get('created_turn', -100) > 2:
            return None
        selected_id = pending.get('event_id')
    event = next((e for e in events if e['id']==selected_id), None)
    if not event:
        return None
    explicit_ordered_arc = bool(explicit_arc_match and selected_id == explicit_arc_match)
    if not explicit_ordered_arc and not event.get('selectable', True):
        return None
    if event.get('is_tiered_event'):
        if is_event_played(selected_id, state) or not _ready(event, director['events'], _turn(state), explicit):
            return None
    elif explicit_ordered_arc:
        if selected_id not in next_actionable_arc_beat_ids(events, state):
            return None
    elif selected_id not in {e['id'] for e in candidates} or not _ready(
            event, director['events'], _turn(state), explicit):
        return None
    director['offered_event_id'] = selected_id
    instruction = (
        'Người chơi ĐÃ ĐẠO DIỄN kích hoạt sự kiện này trong lượt hiện tại. '
        'Hãy mở màn sự kiện ngay một cách tự nhiên, sống động trong bối cảnh hiện tại '
        'và để các NPC phản ứng đúng tính cách; không áp đặt lựa chọn, hành động hay lời nói của Player.'
        if (event.get('is_tiered_event') or explicit_ordered_arc) and explicit else
        'Gợi ý chưa xảy ra. Đối chiếu ký ức và lời Player mới nhất; bỏ qua/đổi nhịp nếu đã xảy ra hoặc không còn hợp cảnh. Không áp đặt lựa chọn, kết quả hay tri thức bí mật lên Player/NPC.'
    )
    return {**event, 'explicit_player_request':explicit, 'instruction':instruction}


def apply_event_delta(events, state, delta, evidence_text, room, past_evidence=''):
    """Accept bounded tracker proposals; outcome changes require public evidence."""
    if not events or not isinstance(delta, dict):
        return
    director = _director(state)
    records = director['events']
    known = {e['id']:e for e in events}
    offered = director.get('offered_event_id')
    visible_ids = {e['id'] for e in event_index(events, state)}
    updates = delta.get('event_updates', [])
    for update in updates[:4] if isinstance(updates, list) else []:
        if not isinstance(update, dict):
            continue
        ident, status = update.get('event_id'), update.get('status')
        evidence = update.get('evidence')
        if not isinstance(ident, str) or ident not in known or ident not in visible_ids | {offered}:
            continue
        if not isinstance(status, str) or status not in {'planted','active','completed','superseded','deferred'}:
            continue
        if records.get(ident, {}).get('status') in CLOSED:
            continue
        reconciliation = update.get('reconcile_existing') is True
        evidence_source = past_evidence if reconciliation else evidence_text
        if not isinstance(evidence, str) or len(evidence.strip()) < 12 or evidence.casefold() not in evidence_source.casefold():
            continue
        # A sandbox can reach a beat organically without first requesting it.
        # Still require a ready frontier beat and actual public evidence; merely
        # retrieving/mentioning a plan never completes it.
        if (status in {'planted','active','completed'} and ident != offered and not reconciliation
                and not _ready(known[ident], records, _turn(state))):
            continue
        # Skipping a beat requires an actual refusal/contradiction in public play;
        # the tracker is instructed never to skip simply to unlock the next beat.
        records[ident] = {'status':status, 'evidence':evidence[:500], 'updated_turn':_turn(state)}
        if status == 'deferred':
            records[ident]['retry_after'] = _turn(state) + COOLDOWN
    # Only an evidence-backed actual goal resolves a structured arc. Opening a
    # scene, changing routes or superseding a proposal is never arc completion.
    for event in events:
        if (event.get('arc_completion') and records.get(event['id'], {}).get('status')=='completed'
                and records[event['id']].get('evidence')):
            completed = state.setdefault('completed_arcs', [])
            if event['arc_id'] not in completed:
                completed.append(event['arc_id'])
    director['pending'] = None
    request = delta.get('event_request')
    if not isinstance(request, dict) or request.get('timing') != 'next_turn':
        return
    ident = request.get('event_id')
    if not isinstance(ident, str):
        return
    event = known.get(ident)
    reason = request.get('reason')
    if (event and event.get('selectable', True) and ident in {e['id'] for e in event_index(events, state)}
            and _ready(event, records, _turn(state)) and isinstance(reason, str) and reason.strip()):
        director['pending'] = {'event_id':ident, 'reason':reason[:240],
                               'setting':room.get('setting'), 'created_turn':_turn(state)}
