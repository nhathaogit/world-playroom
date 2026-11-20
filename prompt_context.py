"""Read-only prompt projections: never compact or mutate the actual save."""
import json
import re
import copy


def pack_tracker_prompt(core,state,public,responses,directives,byte_limit):
    """Bound old retrieval, not this turn's responses or committed truth ledgers."""
    payload={'state':copy.deepcopy(state),'player_public':public,'responses':responses,'directives':directives}
    def render():return core+'\n'+dump(payload)
    def wire_size():
        return len(dump([{'role':'system','content':render()}, {'role':'user',
            'content':'Ghi nhận delta trạng thái của lượt vừa hoàn tất dưới dạng JSON.'}]).encode('utf-8'))
    budget=byte_limit-800
    for key in ('facts','suggestion_ledger','temporary_npcs'):
        value=payload['state'].get(key)
        while wire_size()>budget and isinstance(value,list) and value:value.pop(0)
    if wire_size()>budget:payload['state'].pop('event_reconciliation_memory',None)
    if wire_size()>budget:
        raise ValueError('Required tracker context exceeds input budget; no request sent')
    return render()


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def relevant_items(items, query, limit=8, recent=3):
    if not isinstance(items, list):
        return []
    terms = set(re.findall(r'\w{3,}', query.casefold()))
    scored = sorted(enumerate(items), key=lambda pair: (
        len(terms & set(re.findall(r'\w{3,}', dump(pair[1]).casefold()))), pair[0]), reverse=True)
    selected = set(range(max(0, len(items)-recent), len(items)))
    for index, _ in scored:
        if len(selected) >= limit:
            break
        selected.add(index)
    return [items[index] for index in sorted(selected)]


def select_world(world, query, max_bytes=7500):
    """Retrieve complete rule blocks, never cut a rule halfway through."""
    blocks = []
    def walk(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + [key])
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + [str(index)])
        else:
            blocks.append({'path': '.'.join(path), 'value': value})
    walk(world, [])
    terms = set(re.findall(r'\w{3,}', query.casefold()))
    def rank(block):
        words = set(re.findall(r'\w{3,}', dump(block).casefold()))
        return len(words & terms) + (3 if any(word in block['path'].casefold()
                for word in ('constraint', 'limit', 'scope', 'era', 'culture')) else 0)
    selected, used = [], 0
    for block in sorted(blocks, key=rank, reverse=True):
        size = len(dump(block).encode('utf-8'))
        if used + size <= max_bytes:
            selected.append(block)
            used += size
    return selected


def compact_history(entries):
    result = []
    for entry in entries:
        segments = entry.get('segments')
        if entry.get('intent') == 'thought':
            segments = [{'type':'thought_hidden','text':'Player đang suy nghĩ; nội dung riêng tư bị ẩn.'}]
        elif entry.get('intent') == 'command':
            segments = [{'type':'event','text':'Player đưa chỉ thị đạo diễn (xem directives hiện tại).'}]
        elif segments:
            segments = [({'type':'thought_hidden','text':'Player đang suy nghĩ; nội dung riêng tư bị ẩn.'}
                         if item.get('type') == 'thought' else item)
                        for item in segments if isinstance(item, dict) and item.get('type') != 'command']
        if not segments:
            segments = [{'type': kind, 'text': entry.get(key)} for kind, key in
                        [('narration', 'narration'), ('dialogue', 'dialogue')] if entry.get(key)]
        result.append({'speaker_id': 'player' if entry.get('is_player') else entry.get('speaker_id', 'system'),
                       'sender': entry.get('sender', ''),
                       'segments': [{ 'type': item.get('type'), 'text': item.get('text') }
                                    for item in segments if isinstance(item, dict) and item.get('text')]
                                   if segments else [{'type': 'event', 'text': entry.get('text', '')}]})
    return result


def recent_turn_history(entries, turns=3, min_entries=0):
    """Select complete exchanges, never the arbitrary last N speaker blocks."""
    players = [i for i,e in enumerate(entries)
               if e.get('is_player') or e.get('speaker_id') == 'player']
    if not players and min_entries:
        return entries[-min_entries:]
    start = players[-turns] if len(players) >= turns else 0
    if min_entries:
        start = min(start, max(0, len(entries)-min_entries))
        start = next((i for i in reversed(players) if i <= start), 0)
    return entries[start:]


def pack_scene_messages(prompt, context, byte_limit, invalid_draft=None):
    """Budget the serialized messages, including escaping and JSON retry overhead."""
    reserved = 800
    cap = byte_limit - reserved
    draft = invalid_draft
    def render(fitted):
        payload = fitted if draft is None else {'context': fitted, 'invalid_draft': draft}
        return [{'role':'system', 'content':prompt}, {'role':'user', 'content':dump(payload)}]
    def size(messages):return len(dump(messages).encode('utf-8'))
    while True:
        full = render(context)
        if size(full) <= cap:
            return full
        minimal = fit_context(context, 0, allow_overflow=True)
        best = render(minimal)
        if size(best) > cap:
            if draft is not None:
                # Regenerate from original context and the precise validation error
                # rather than dropping mandatory player/character information.
                draft = None
                continue
            parts = {'system':len(prompt.encode('utf-8')), **{
                key:len(dump(value).encode('utf-8')) for key,value in minimal.items()}}
            largest = ', '.join(f'{key}={value}' for key,value in sorted(parts.items(),key=lambda pair:pair[1],reverse=True)[:4])
            raise ValueError(f'Required scene context exceeds input budget ({size(best)}/{cap} bytes; {largest}); no request sent.')
        # Escaping overhead shrinks with optional retrieval. Subtracting the
        # original overflow once can overshoot below the required context even
        # though its actual wire payload fits. Search measured candidates.
        low, high = len(dump(minimal).encode('utf-8')), len(dump(context).encode('utf-8'))
        while low <= high:
            limit = (low + high) // 2
            fitted = fit_context(context, limit)
            candidate = render(fitted)
            if size(candidate) <= cap:
                best = candidate
                low = limit + 1
            else:
                high = limit - 1
        return best


def fit_context(context, byte_limit, allow_overflow=False):
    """Remove optional retrieval units; preserve persona, instructions and latest turn.

    Failing closed is preferable to silently dropping a character's defining rules.
    """
    import copy
    result = copy.deepcopy(context)
    protected_count = result.get('context_policy', {}).get('protected_history_count', 1)
    protected_count = protected_count if isinstance(protected_count,int) and protected_count>0 else 1
    # Protected working memory beats optional lore/old summaries, not vice versa.
    paths = [('world_rules',), ('memory', 'recent_suggestions'),
             ('memory','scoped_events'), ('memory', 'facts'),
             ('memory', 'recent_events'), ('memory', 'known_outcomes'),
             ('memory', 'open_threads'), ('arc_hint',),
             ('story_guidance','threads'), ('story_guidance','next_beats'),
             ('continuity_memory','records'), ('history',)]
    if 'continuity_memory' in result:
        paths.insert(2, ('memory','commitments'))
        paths.insert(3, ('memory','summary'))
    for path in paths:
        while len(dump(result).encode('utf-8')) > byte_limit:
            node = result
            for key in path[:-1]:
                node = node.get(key, {})
            value = node.get(path[-1])
            if path in {('story_guidance','threads'), ('story_guidance','next_beats')}:
                # A future roadmap is optional retrieval, not immutable truth.
                # Keep the active thread and one nearby beat; the full selected
                # story_event and the player's director command remain intact.
                if not isinstance(value,list) or len(value)<=1:
                    break
                protected = result.get('story_guidance',{}).get('current_thread') if path[-1]=='threads' else result.get('story_event',{}).get('id')
                removable = next((index for index in range(len(value)-1,-1,-1)
                                  if value[index].get('id')!=protected), None)
                if removable is None:
                    break
                value.pop(removable)
            elif isinstance(value, list) and len(value) > (protected_count if path == ('history',) else 0):
                value.pop(0 if path != ('world_rules',) else -1)
            elif path == ('arc_hint',) and value:
                node[path[-1]] = {}
            elif path == ('memory','summary') and isinstance(value,str) and value:
                node[path[-1]] = ''
            else:
                break
    if len(dump(result).encode('utf-8')) > byte_limit and not allow_overflow:
        raise ValueError('Required scene context exceeds input budget; shorten persona/character profiles or raise the configured budget.')
    return result
