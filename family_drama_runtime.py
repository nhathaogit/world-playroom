"""Room-scoped performance direction; no save migration or extra AI calls."""
import copy
import json

PROFILE = 'family_drama_v1'


def enabled(bible):
    return (bible.get('story_id') == 'muc-son-qua-hien'
            and bible.get('performance_profile') == PROFILE)


def performance_fields(performance):
    fields = {key: copy.deepcopy(performance[key])
              for key in ('personality', 'voice', 'desire', 'dramatic_engine')}
    fields['voice'] += '\nVí dụ khẩu khí, chưa phải lời đã diễn: ' + ' / '.join(performance.get('samples', []))
    return fields


def load(root, bible):
    if not enabled(bible):
        return bible
    direction = json.loads((root / 'performance_direction.json').read_text(encoding='utf-8'))
    if direction.get('profile') != PROFILE or not isinstance(direction.get('characters'), dict):
        raise ValueError('Invalid family drama performance direction')
    result = copy.deepcopy(bible)
    result['_performance_direction'] = direction
    result['_performance_prompt'] = (root / 'scene_writer.md').read_text(encoding='utf-8')
    for npc_id, performance in direction['characters'].items():
        profile = result.get('characters', {}).get(npc_id)
        if profile is not None:
            profile.update(performance_fields(performance))
    return result


def project_context(context, bible):
    if not enabled(bible):
        return context
    result = copy.deepcopy(context)
    performances = bible['_performance_direction']['characters']
    for item in result.get('characters', []):
        performance = performances.get(item['speaker_id'])
        if performance:
            item.setdefault('profile', {}).update(performance_fields(performance))
            item.get('presentation', {}).pop('personality', None)
            item.get('presentation', {}).pop('tone', None)
    for item in result.get('existing_temporary_profiles', []):
        performance = performances.get(item['id'])
        if performance:
            item.update(performance_fields(performance))
            item.pop('tone', None)
    return result


def scene_prompt(bible):
    return bible['_performance_prompt']


def reconcile_playthrough(room, bible, archives, events):
    """Room packs own their progress; a fresh framework has no historical repairs."""
    return


def protect_continuity(context, state):
    records = state.get('established_continuity')
    if records:
        context['established_continuity'] = copy.deepcopy(records)
