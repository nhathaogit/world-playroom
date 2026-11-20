"""Isolated opt-in playthroughs. Shared author assets, private mutable state/memory."""
import copy
import json
import re
import shutil
import uuid
from speaker_identity import identity_key
from datetime import datetime, timezone
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def config(room):
    path = Path(room) / 'roleplay_start.json'
    return read(path) if path.is_file() else None


def directory(room, session_id='legacy'):
    if session_id == 'legacy':
        return Path(room)
    if not re.fullmatch(r'[a-f0-9]{32}', session_id):
        raise ValueError('Mã phiên không hợp lệ')
    path = Path(room) / 'sessions' / session_id
    if not (path / 'session.json').is_file() or not (path / 'room_state.json').is_file():
        raise FileNotFoundError('Không tìm thấy phiên chơi')
    return path


def player_id(state):
    return state.get('player', {}).get('character_id', '')


def guard(state, ident):
    if owns_reference(state, ident):
        raise ValueError('Nhân vật này đang do Player điều khiển; không thể dùng làm NPC')


def owns_reference(state, reference):
    ident = player_id(state)
    if not ident or not reference:
        return False
    profile = state['player'].get('canon_profile', {})
    name = profile.get('name', state['player'].get('name', ''))
    forms = [ident, name] + profile.get('aliases', [])
    # Familiar canonical given names (Lucy/Natsu) must not create a second body.
    first = name.split()[0] if name.split() else ''
    if len(first) >= 3 and not any(identity_key(n.get('name','').split()[0] if n.get('name') else '') == identity_key(first)
                                    for n in state.get('roster',{}).values()):
        forms.append(first)
    return identity_key(reference) in {identity_key(f) for f in forms if f}


def enforce(state):
    """Fail closed on duplicate identity even if a tracker/import restored it."""
    ident = player_id(state)
    if ident:
        state.get('roster', {}).pop(ident, None)
        state['active_character_ids'] = [key for key in state.get('active_character_ids', []) if key != ident]


def create(room, mode, catalog, name='', persona='', character_id='', gender='unspecified'):
    cfg = config(room)
    if not cfg:
        raise ValueError('Playroom chưa có cấu hình khởi đầu cho phiên mới')
    if mode not in {'original', 'canon'}:
        raise ValueError('Chọn nhân vật tự tạo hoặc nhân vật có sẵn')
    if len(persona) > 12000 or len(name) > 100:
        raise ValueError('Hồ sơ quá dài')
    if mode == 'canon' and (character_id not in catalog or
            catalog[character_id].get('variant') == 'platinum_twin' or character_id.startswith('silver_')):
        raise ValueError('Không tìm thấy vai trong thư viện của room')
    if mode == 'original' and not name.strip():
        raise ValueError('Hãy đặt tên cho nhân vật của bạn')
    state = copy.deepcopy(cfg['initial_state'])
    state['history'] = []
    state['player'] = {'name': name.strip(), 'role': cfg['original_role'], 'persona': persona,
                       'gender': gender, 'avatar': '', 'identity_mode': mode}
    if mode == 'canon':
        profile = copy.deepcopy(catalog[character_id])
        profile.pop('relationship', None)
        profile.update(copy.deepcopy(cfg.get('identity_overrides', {}).get(character_id, {})))
        state['player'].update(name=profile['name'], role=profile.get('role', ''),
                               character_id=character_id, canon_profile=profile,
                               avatar=profile.get('avatar', ''))
        state['setting'] = cfg['canon_setting']
        state['narrative_state']['identity_background'] = cfg['canon_background']
        state['narrative_state']['identity_relationships'] = cfg.get('relationships', [])
        state['narrative_state']['identity_team'] = next((team for team, ids in cfg.get('teams', {}).items()
                                                        if character_id in ids), None)
        # A selected familiar face is not a newly joined AU celestial wizard.
        state['narrative_state']['facts'] = [cfg['canon_background']]
        for key in list(state['roster']):
            if catalog.get(key, {}).get('variant') == 'platinum_twin':
                del state['roster'][key]
        for entry in state['roster'].values():
            entry['is_active'] = False
            entry.pop('scene_scope', None)
            entry['relationship'] = 'Quan hệ canon với ' + profile['name'] + ' tại mốc bắt đầu; không mặc định người lạ.'
        # Nearby companions only; other guilds start without teleported NPCs.
        for key in cfg.get('canon_nearby', {}).get(profile.get('guild', ''), []):
            if key in state['roster'] and key != character_id:
                state['roster'][key].update(is_active=True, scene_scope=state['setting'])
    else:
        state['narrative_state']['facts'] = [cfg['original_background']]
    enforce(state)
    state['active_character_ids'] = [k for k,v in state['roster'].items() if v.get('is_active')]
    ident = uuid.uuid4().hex
    state['session_id'] = ident
    meta = {'id':ident, 'mode':mode, 'name':state['player']['name'],
            'character_id':character_id if mode == 'canon' else '',
            'created_at':datetime.now(timezone.utc).isoformat()}
    root = Path(room) / 'sessions'
    root.mkdir(exist_ok=True)
    staging = root / ('.creating-' + ident)
    staging.mkdir()
    try:
        for filename, value in [('room_state.json', state), ('session.json', meta)]:
            (staging / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        staging.rename(root / ident)
    except Exception:
        shutil.rmtree(staging)
        raise
    return meta


def sessions(room):
    result = [{'id':'legacy', 'mode':'legacy', 'name':'Phiên hiện tại (giữ nguyên tiến trình)'}]
    for path in sorted((Path(room)/'sessions').glob('*/session.json')):
        if not re.fullmatch(r'[a-f0-9]{32}', path.parent.name):
            continue
        try:
            data = read(path)
            if data.get('id') == path.parent.name and (path.parent/'room_state.json').is_file():
                result.append(data)
        except (OSError, ValueError):
            continue
    return result


def project(context, state):
    """Identity travels inside protected player context, not an optional lore unit."""
    identity = state.get('player', {})
    if not identity.get('identity_mode'):
        return context
    context['player'] = copy.deepcopy(identity)
    background = state.get('narrative_state', {})
    if identity.get('identity_mode') == 'canon':
        context['player']['identity_contract'] = {
            'control':'Player chính là nhân vật này. Không điều khiển lời nói, ý nghĩ, hành động hoặc lựa chọn của Player. Tính cách gốc là nền tham khảo, không ép diễn đúng manga.',
            'recognition':'NPC nhận đúng người theo quan hệ và kiến thức có trước mốc bắt đầu; người chưa từng gặp vẫn chưa quen. Không coi Player là tân binh hay người giả dạng.',
            'background':background.get('identity_background'),
            'recognition_limits':identity.get('canon_profile', {}).get('recognition_limits', ''),
            'team':background.get('identity_team'),
            'relationships':background.get('identity_relationships', []),
            'inventory':'Chỉ có năng lực/vật dẫn trong hồ sơ vai đã chọn; không thừa hưởng Bạch Kim, kho chìa hoặc sức mạnh AU của nhân vật tự tạo.',
        }
        selected = identity['character_id']
        for key, id_key in [('characters','speaker_id'), ('speaker_manifest','speaker_id'),
                            ('presence_candidates','npc_id'), ('existing_temporary_profiles','id')]:
            context[key] = [item for item in context.get(key, []) if item.get(id_key) != selected]
    return context


def overlay(bible, state):
    """Session overrides never write back to the shared author bible."""
    mode = state.get('player', {}).get('identity_mode')
    if mode not in {'canon', 'original'}:
        return bible
    result = copy.deepcopy(bible)
    result['premise'] = state.get('narrative_state', {}).get('identity_background', '')
    result['player_owned_character_ids'] = []
    result.pop('celestial_registry', None)
    if mode == 'original':
        result['premise'] = 'Player là nhân vật mới tự tạo, gia nhập Fairy Tail và tham gia vòng loại với đội C. Persona quyết định xuất thân và ma pháp; không mặc định kho chìa Bạch Kim.'
        result['runtime_rules']['core_rules'] = [r for r in result.get('runtime_rules', {}).get('core_rules', []) if 'Bạch Kim' not in r]
        result['runtime_rules']['core_rules'].append(result['premise'])
        result['strict_rules'] = [r for r in result.get('strict_rules', []) if 'Bạch Kim' not in r]
        result['world']['magic']['celestial_spirits'] = 'Lucy/Yukino giữ các chìa canon. Player tự khai báo ma pháp và vật dẫn trong persona; không tự cấp kho chìa AU.'
        return result
    rules = result.get('runtime_rules', {})
    rules['core_rules'] = [r for r in rules.get('core_rules', [])
                           if not any(word in r for word in ('Team C', 'Bạch Kim'))]
    rules['core_rules'].append('Player là nhân vật canon được chọn tại mốc X791 trước Sky Labyrinth; đội A/B và quan hệ cũ giữ đúng mốc. Không có đội C hoặc kho chìa AU của Player trong phiên nhập vai canon.')
    result['runtime_rules'] = rules
    result['strict_rules'] = [r for r in result.get('strict_rules', []) if 'Bạch Kim' not in r and 'Team C' not in r]
    magic = result.get('world', {}).get('magic', {})
    if 'celestial_spirits' in magic:
        magic['celestial_spirits'] = 'Chìa Vàng và khế ước giữ theo canon tại X791: Lucy giữ 10 chìa; Yukino giữ Libra, Pisces và Ophiuchus. Không có kho chìa Bạch Kim AU của Player trong phiên này.'
    return result
