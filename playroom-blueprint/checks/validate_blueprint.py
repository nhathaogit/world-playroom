"""Read-only blueprint lint; NOT a backend, UI or live-model acceptance test.

Run: python playroom-blueprint/checks/validate_blueprint.py --self-test
Only standard library. No engine imports, network, save writes or server hooks.
"""
import argparse
import copy
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_scene(data):
    before = data['before']
    output = data['scene_output']
    expected = data['expected_after']
    require(set(('responses', 'scene_changed', 'new_setting', 'scene_evidence',
                 'presence_updates', 'npc_introductions')) <= set(output), 'Missing envelope fields')
    require(type(output['scene_changed']) is bool, 'scene_changed must be boolean')
    catalog = data.get('catalog', {})
    introduced = {p['id'] for p in output['npc_introductions']}
    identities = set(before['roster']) | set(catalog) | introduced
    for profile in output['npc_introductions']:
        if profile['id'] not in catalog and profile['id'] not in before['roster']:
            require(all(profile.get(k) for k in ('name', 'role', 'personality', 'tone',
                                                'scene_function', 'evidence')), 'Incomplete new NPC')
    narration = []
    for block in output['responses']:
        require(block['speaker_id'] in identities | {'__narrator__'}, 'Unknown fixture speaker')
        require(bool(block['segments']), 'Empty response')
        for segment in block['segments']:
            require(segment['type'] in {'narration', 'dialogue'}, 'Invalid segment kind')
            require(isinstance(segment['text'], str) and segment['text'], 'Empty segment')
            require(block['speaker_id'] != '__narrator__' or segment['type'] == 'narration',
                    'Narrator cannot speak dialogue')
            if segment['type'] == 'narration':
                narration.append(segment['text'])
    public = '\n'.join(p['text'] for p in data['player_segments']
                       if p['type'] in {'action', 'speech', 'command'})
    authority = '\n'.join(p['text'] for p in data['player_segments']
                          if p['type'] in {'action', 'command'})
    if output['scene_changed']:
        require(bool(output['new_setting']) and output['new_setting'] != before['setting'],
                'Transition needs a different setting')
        require(bool(output['scene_evidence']) and output['scene_evidence'] in authority,
                'Transition lacks public action/directive authority')
        require(expected.get('setting') == output['new_setting'], 'Expected banner differs')
        require(expected.get('scene_notification_count') == 1, 'Missing scene notification')
    source = public + '\n' + '\n'.join(narration)
    pairs = set()
    active = set() if output['scene_changed'] else {
        key for key, value in before['roster'].items() if value.get('is_active')}
    for update in output['presence_updates']:
        ident, action = update['npc_id'], update['action']
        require(ident in identities and action in {'enter', 'leave'}, 'Invalid presence identity/action')
        require(ident in before['roster'] or ident in introduced, 'Catalog identity not registered')
        require(bool(update['reason']) and bool(update['evidence']) and update['evidence'] in source,
                'Presence evidence missing from supplied public text/narration')
        require((ident, action) not in pairs, 'Duplicate presence notification')
        pairs.add((ident, action))
        if action == 'enter':
            active.add(ident)
        else:
            active.discard(ident)
    require(active == set(expected['active_character_ids']), 'Expected active cast differs')
    require(not active & set(expected['inactive_character_ids']), 'Expected inactive actor is ON')
    require(expected['presence_notification_count'] == len(pairs), 'Notification count differs')
    if 'presence_events' in expected:
        require(pairs == {(e['npc_id'], e['action']) for e in expected['presence_events']},
                'Expected event pairs differ')
    if data['case_id'] == 'SCN-07':
        require(expected.get('departure_after_prose') is True, 'Farewell ordering not specified')
        require(expected.get('next_turn_recovery_reactivates') is False, 'Recovery must not reopen departure')
        require(expected.get('identity_retained') in identities, 'Identity must survive leave')


def check_privacy(data):
    expected = data['expected_recall']
    public = []
    for entry in data['archive']:
        allowed = {'speech', 'action'} if entry.get('is_player') else {'narration', 'dialogue'}
        # Hidden-only mixed entries must not fall back to raw text.
        public.extend(s['text'] for s in entry.get('segments', []) if s['type'] in allowed)
    text = '\n'.join(public)
    require(all(value in text for value in expected['contains']), 'Missing public fixture evidence')
    require(all(value not in text for value in expected['excludes']), 'Private fixture data leaked')
    require(expected.get('requires_source') is True, 'Recall must retain sources')
    require(expected.get('changes_presence') is False, 'Archive is not present-scene authority')
    require(expected.get('universal_npc_knowledge') is False, 'Archive is not universal knowledge')


def check_fixture(data):
    require(data.get('fixture_version') == '1.1', 'Wrong fixture version')
    if data['case_id'] == 'MEM-05':
        check_privacy(data)
    else:
        check_scene(data)


def check_documents():
    paths = sorted(ROOT.rglob('*.md'))
    json_blocks = 0
    for path in paths:
        text = path.read_text(encoding='utf-8')
        require(text.count('```') % 2 == 0, f'Unclosed fence: {path.name}')
        for match in re.finditer(r'\[[^\]]+\]\(([^)]+)\)', text):
            target = match[1].split('#', 1)[0].strip('<>')
            if not target or re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', target):
                continue
            resolved = (path.parent / target).resolve()
            require(resolved.is_relative_to(ROOT) and resolved.is_file(),
                    f'Broken/out-of-bundle link: {path.name} -> {target}')
        for block in re.findall(r'```json\s*\n(.*?)```', text, re.S):
            json.loads(block)
            json_blocks += 1
    for path in (ROOT / 'prompts').glob('*.md'):
        text = path.read_text(encoding='utf-8')
        require(not any(name in text for name in ('Fairy Tail', 'Raven Tail', 'Virgo', 'Tạ Cảnh Hoài')),
                f'Room-specific rule in common core: {path.name}')
    return len(paths), json_blocks


def self_test(fixtures):
    cases = []
    data = copy.deepcopy(fixtures['SCN-01']);data['scene_output'].pop('scene_evidence');cases.append(data)
    data = copy.deepcopy(fixtures['SCN-01']);data['player_segments'][0]['type'] = 'thought';cases.append(data)
    data = copy.deepcopy(fixtures['SCN-01']);data['scene_output']['responses'][1]['speaker_id'] = 'unknown';cases.append(data)
    data = copy.deepcopy(fixtures['SCN-01']);data['expected_after']['presence_notification_count'] = 0;cases.append(data)
    data = copy.deepcopy(fixtures['SCN-07']);data['expected_after']['next_turn_recovery_reactivates'] = True;cases.append(data)
    data = copy.deepcopy(fixtures['MEM-05']);data['expected_recall']['universal_npc_knowledge'] = True;cases.append(data)
    for data in cases:
        try:
            check_fixture(data)
        except (ValueError, KeyError):
            continue
        raise ValueError('Validator accepted a deliberately broken fixture')
    return len(cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    documents, blocks = check_documents()
    fixtures = {}
    for path in sorted((ROOT / 'examples').glob('*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        check_fixture(data)
        require(data['case_id'] not in fixtures, 'Duplicate case ID')
        fixtures[data['case_id']] = data
    require(set(fixtures) == {'SCN-01', 'SCN-07', 'MEM-05'}, 'Required fixtures missing')
    negative = self_test(fixtures) if args.self_test else 0
    print(f'Blueprint v1.1 static checks OK: {documents} Markdown files, {blocks} JSON blocks, '
          f'{len(fixtures)} fixtures, {negative} negative self-tests.')
    print('Read-only: no API/server/save access. This is NOT runtime/UI/model acceptance.')


if __name__ == '__main__':
    main()
