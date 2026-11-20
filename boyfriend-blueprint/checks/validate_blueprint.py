"""Read-only companion pack/template lint; no model, engine import or save writes.

Run: python boyfriend-blueprint/checks/validate_blueprint.py --self-test
Or: --pack path/to/staged-pack [--assets-root path/to/engine]
Does not prove prose quality, safety semantics, UI behavior or runtime integration.
"""
import argparse
import copy
import json
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def require(condition,message):
    if not condition:raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def text(value):
    return isinstance(value,str) and bool(value.strip())


def validate_pack(manifest,bible,state,assets_root=None):
    require(all(isinstance(x,dict) for x in (manifest,bible,state)),'Pack files must be objects')
    ident=manifest.get('id')
    require(isinstance(ident,str) and re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',ident),'Invalid room ID')
    require(manifest.get('category')=='boyfriend','Category must be boyfriend')
    require(bible.get('story_id')==ident==state.get('story_id')==state.get('room_id'),'Room IDs differ')
    require(bible.get('session_mode')=='companion','session_mode must be companion')
    require(not bible.get('runtime_profile') and not bible.get('scene_lifecycle'),
            'Do not opt companion into a multi-character runtime profile')
    require(bible.get('arcs')=={} and not bible.get('finale') and not bible.get('future_events'),
            'Companion must not contain a future arc/finale/event schedule')
    chars=bible.get('characters',{})
    require(isinstance(chars,dict) and len(chars)==1,'Exactly one principal character required')
    principal=next(iter(chars))
    char=chars[principal]
    require(isinstance(char,dict),'Principal profile must be object')
    profile=bible.get('profile',{})
    require(isinstance(profile,dict),'Public profile must be object')
    require(text(char.get('name')) and char['name']==profile.get('name')==manifest.get('title'),
            'Public profile/name/title differ')
    for value in (char.get('age'),profile.get('age')):
        require(type(value) in (int,float) and value>=18,'Character must have explicit adult age')
    require(char['age']==profile['age'],'Public/private ages differ')
    for key in ('height_cm','weight_kg'):
        require(key in profile,'Profile needs height_cm and weight_kg, null if unknown')
        value=profile[key]
        require(value is None or (type(value) in (int,float) and value>0),'Invalid profile measurement')
    for key in ('origin','occupation','appearance','relationship_with_player'):
        require(text(profile.get(key)),'Missing public profile field: '+key)
    for key in ('role','personality','voice','desire','backstory','opening_knowledge','authority'):
        require(text(char.get(key)),'Missing character field: '+key)
    require(isinstance(char.get('ooc_guard'),list) and bool(char['ooc_guard']),'Missing character boundaries')
    require(text(bible.get('interaction_contract')) and text(bible.get('starting_situation')),'Missing interaction/opening context')
    background=bible.get('background_before_chat')
    require(isinstance(background,list) and bool(background) and all(text(x) for x in background),
            'Background must contain nonempty pre-chat facts')
    require(isinstance(bible.get('strict_rules'),list) and bool(bible['strict_rules']),'Missing strict rules')
    opening=bible.get('opening_message',{})
    require(isinstance(opening,dict) and opening.get('speaker_id')==principal,'Opening speaker differs')
    require(opening.get('is_player') is False,'Opening must not be a player turn')
    validate_segments(opening.get('segments'))
    require(not any(k in opening for k in ('text','narration','dialogue')),'Opening duplicates segments as text')
    require(set(state.get('roster',{}))=={principal},'Save roster must contain only principal')
    require(state['roster'][principal].get('canon_id')==principal,'Roster canon_id differs')
    require(state['roster'][principal].get('name')==char['name'],'Roster/public name differs')
    require(state.get('session_started') is False and state.get('history')==[],
            'New pack must not contain an already-started session/history')
    player=state.get('player',{})
    require(isinstance(player,dict) and player.get('persona')=='' and player.get('agency_locked') is True,
            'New persona must be blank and player agency retained')
    narrative=state.get('narrative_state',{})
    require(isinstance(narrative,dict),'narrative_state must be object')
    require(not any(narrative.get(k) for k in ('facts','knowledge','milestones','completed_arcs','story_director')),
            'Do not transplant another save memory/progress')
    profiles=bible.get('relationship_profiles',{})
    require(set(profiles)=={principal},'Relationship profile IDs differ')
    rel=profiles[principal]; default=rel.get('default_mode')
    require(default in rel and isinstance(rel[default],dict),'Invalid default relationship mode')
    meanings=bible.get('relationship_title_meanings',{}).get(principal,{})
    for mode,config in rel.items():
        if mode=='default_mode':continue
        require(isinstance(config,dict),'Relationship mode must be object')
        metrics=config.get('metrics',{})
        require(isinstance(metrics,dict) and bool(metrics) and config.get('title_metric') in metrics,'Unknown title metric')
        titles=config.get('titles',[])
        require(isinstance(titles,list) and bool(titles),'Missing relationship thresholds')
        require(all(isinstance(row,list) and len(row)==2 and type(row[0]) is int and text(row[1]) for row in titles),
                'Invalid relationship threshold shape')
        limits=[row[0] for row in titles]
        require(limits==sorted(set(limits)) and limits[0]==0 and limits[-1]<=100,'Thresholds must ascend from 0 to <=100')
        require(all(text(meanings.get(row[1])) for row in titles),'Title explanation missing')
    thumbnail=manifest.get('thumbnail')
    require(text(thumbnail),'Missing portrait path')
    if assets_root:
        require(thumbnail.startswith(('/static/','/avatar/')),'Asset verification requires local static/avatar URL')
        asset_root=Path(assets_root).resolve(); path=(asset_root/thumbnail.lstrip('/')).resolve()
        require(path.is_relative_to(asset_root) and path.is_file(),'Portrait file missing or escapes asset root')
    return principal


def validate_segments(segments):
    require(isinstance(segments,list) and bool(segments),'Missing segments')
    for part in segments:
        require(isinstance(part,dict) and part.get('type') in {'narration','dialogue'} and text(part.get('text')),
                'Invalid narration/dialogue segment')


def validate_fixture(data):
    principal=data['principal_id']; scene=data['scene_output']
    require(isinstance(scene.get('responses'),list) and bool(scene['responses']),'Missing fixture responses')
    require(not scene.get('presence_updates') and not scene.get('npc_introductions'),'Companion fixture cannot add NPCs')
    require(scene.get('scene_changed') is False,'Baseline fixture does not claim unsupported scene transition')
    for block in scene.get('responses',[]):
        require(block.get('speaker_id') in {principal,'__narrator__'},'Unknown companion fixture speaker')
        validate_segments(block.get('segments'))
        require(block['speaker_id']!='__narrator__' or all(x['type']=='narration' for x in block['segments']),
                'Narrator cannot speak dialogue')
    public='\n'.join(p['text'] for p in data['player_segments'] if p['type'] in {'speech','action'})
    require(all(x in public for x in data['public_expected']),'Missing expected public text')
    require(all(x not in public for x in data['public_excluded']),'Fixture public/private boundary broken')


def validate_docs(root=ROOT):
    count=0
    for file in root.rglob('*.md'):
        body=file.read_text(encoding='utf-8')
        for target in re.findall(r'\]\(([^)]+)\)',body):
            if target.startswith(('https:','http:','#')):continue
            require((file.parent/target.split('#')[0]).is_file(),'Broken document link: '+str(file.relative_to(root)))
        if file.parent.name=='prompts':
            require(body.count('## SYSTEM — core tái sử dụng')==1,'Each prompt needs exactly one SYSTEM core')
            require('JSON' in body.split('## SYSTEM — core tái sử dụng')[1],'JSON mode instruction missing')
            count+=1
    require(count==3,'Expected scene/tracker/checkpoint prompt cores')


def self_test(manifest,bible,state):
    changes=[lambda m,b,s:b.update(arcs={'forced-ending':{}}),
             lambda m,b,s:b['profile'].update(age=17),
             lambda m,b,s:b.update(runtime_profile='canon_sandbox_v2'),
             lambda m,b,s:b['characters'].update(extra=dict(next(iter(b['characters'].values())))),
             lambda m,b,s:s.update(session_started=True),
             lambda m,b,s:s['player'].update(persona='copied persona'),
             lambda m,b,s:b['relationship_profiles'][next(iter(b['characters']))]['potential_romance'].update(title_metric='unknown')]
    for change in changes:
        m,b,s=copy.deepcopy((manifest,bible,state));change(m,b,s)
        try:validate_pack(m,b,s)
        except ValueError:continue
        raise AssertionError('Bad pack was accepted')
    return len(changes)


def run():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pack',type=Path,default=ROOT/'templates')
    parser.add_argument('--assets-root',type=Path)
    parser.add_argument('--self-test',action='store_true')
    args=parser.parse_args()
    try:
        manifest,bible,state=[read(args.pack/name) for name in ('manifest.json','story_bible.json','room_state.json')]
        principal=validate_pack(manifest,bible,state,args.assets_root)
        validate_docs();validate_fixture(read(ROOT/'examples/turn-contract.json'))
        cases=self_test(manifest,bible,state) if args.self_test else 0
    except (OSError,ValueError,KeyError,TypeError,AssertionError) as error:
        print('Blueprint lint FAILED:',error);return 1
    print(f'Blueprint lint OK: one principal ({principal}), three prompt cores; {cases} negative cases.')
    print('Static checks only; portrait assets not verified.' if not args.assets_root else 'Local portrait file exists; image content not judged.')
    return 0


if __name__=='__main__':raise SystemExit(run())
