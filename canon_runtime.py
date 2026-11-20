"""Opt-in canon sandbox runtime: projections and evidence-backed domain ledgers.

No providers, disk save mutation or Fairy Tail character names in shared hooks.
Existing room progress is authoritative; never infer a winner from an outline.
"""
import copy
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from story_events import wants_next
from prompt_context import pack_tracker_prompt

PROFILE='canon_sandbox_v2'


def enabled(bible):
    return bible.get('runtime_profile')==PROFILE


def plain(value):
    return ''.join(c for c in unicodedata.normalize('NFD',str(value).casefold().replace('đ','d'))
                   if not unicodedata.combining(c))


@lru_cache(maxsize=24)
def _asset(path,mtime,size):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def load_asset(path):
    """File-versioned, room-scoped cache; callers cannot poison cached profiles."""
    path=Path(path).resolve()
    try:
        stat=path.stat()
        return copy.deepcopy(_asset(str(path),stat.st_mtime_ns,stat.st_size))
    except (OSError,ValueError):
        return {}


def command_kind(directives):
    if wants_next(directives):return 'next_event'
    text=' '.join(plain(item) for item in directives)
    if re.search(r'\b(?:dien bien|su kien|main event)(?: cot truyen)? tiep theo\b',text):return 'next_event'
    if re.search(r'\b(?:chuyen|sang) canh(?: tiep theo)?\b',text):return 'next_scene'
    if re.search(r'\b(?:tam dung|dung) (?:cot truyen|plot)\b',text):return 'pause_plot'
    if re.search(r'\b(?:bo qua|chuyen den|sang) (?:sang mai|ngay mai|buoi toi)\b',text):return 'skip_time'
    return 'respond'


def rank_candidates(candidates,room,directives,public_text=''):
    """Recent actual speakers and newly mentioned cast get deep profiles first."""
    recent=[entry.get('speaker_id') for entry in room.get('history',[])[-8:] if entry.get('is_player') is False]
    recency={key:i+1 for i,key in enumerate(recent) if key}
    direct=plain(' '.join(directives)+' '+public_text)
    return sorted(candidates,key=lambda c:(
        bool(c.get('name') and any(re.search(r'(?<!\w)'+re.escape(name)+r'(?!\w)',direct)
            for name in spirit_aliases({'name':c['name']}))),recency.get(c['npc_id'],0)),reverse=True)


def spirit_aliases(profile):
    name=plain(profile.get('name',''))
    return {value for value in (name,name.removesuffix(' bach kim').strip(),
            plain(profile.get('base_spirit_id',''))) if value}


def event_progress(state):
    """Progress of actual competition play, not a future manga itinerary.

    Incomplete standings never get filled, and existing matches prove only that
    main-stage play has started, not that all qualification ranks were announced.
    No phase here awards a win, closes a match or mutates the persisted ledger.
    """
    matches = state.get('canon_ledger', {}).get('matches', {})
    matches = matches if isinstance(matches, dict) else {}
    opened = [key for key,value in matches.items() if isinstance(value, dict) and value.get('status')=='active']
    closed = [key for key,value in matches.items() if isinstance(value, dict) and value.get('status')=='completed']
    standings = state.get('competition_standings', {})
    standings = standings if isinstance(standings, dict) else {}
    ranks = [value for value in standings.values() if type(value) is int and 1<=value<=8]
    complete = len(ranks)==8 and set(ranks)==set(range(1,9))
    phase = ('in_match' if opened else 'between_matches' if closed else
             'main_stage' if complete else 'qualification_announcements' if standings else 'unconfirmed')
    return {'phase':phase, 'qualification_complete':complete,
            'known_qualification_count':len(standings), 'completed_match_count':len(closed),
            'open_match_ids':opened[-8:], 'closed_match_ids':closed[-32:],
            'closed_ids_truncated':len(closed)>32,
            'next_beat_policy':'Theo trận còn mở nếu chưa khép; không nhảy qua kết quả của Player. '
                'Sau trận đã khép, có thể diễn hậu quả, nghỉ, giao lưu hoặc thông báo phần thi kế khi đúng nhịp. '
                'Không lặp trận đã completed, không tự lấp hạng thiếu, không coi mọi trận đã xong là hết đại hội. '
                'Chỉ dùng luật/lịch đã công bố; chưa biết lịch thì có thể công bố hợp bối cảnh, không đổi kết quả cũ. '
                'Player rẽ hướng hoặc pause_plot thì theo mạch đó; không mặc định Eclipse hay arc manga tiếp theo.'}


def project_context(context,room,bible,catalog):
    """Small projections, not a second copy of the catalog/world/player inventory."""
    result=copy.deepcopy(context)
    state=room.get('narrative_state',{})
    ledger=state.get('canon_ledger',{})
    result['strict_rules']=bible.get('runtime_rules',{}).get('core_rules') or [
        'Canon nền, AU và lịch sử thật trong save quyết định hiện tại, không reset về mở đầu.',
        'Mỗi NPC chỉ biết điều đã quan sát/được kể; không tự biết bí mật manga hay khả năng kín Player.',
        'Không sửa kết quả đã chốt, không lặp trận đã xong; không coi kết quả manga là tương lai bắt buộc.',
        'Phép giữ đúng vật dẫn, chi phí, điều kiện và giới hạn hồ sơ; sáng tạo chiến thuật không phải tạo phép mới.',
    ]
    result['canon_time_boundary']=bible.get('world',{}).get('time_boundary','')
    # Never present the opening premise or full hidden key inventory as current facts.
    result['canon_runtime']={
        'contract_version':'canon-sandbox-v2',
        'current_scene':state.get('current_scene'), 'location':state.get('location'),
        'qualification_standings':copy.deepcopy(state.get('competition_standings',{})),
        'matches':list(ledger.get('matches',{}).values())[-6:],
        'observed_summons':list(ledger.get('summons',{}).values())[-6:],
        'event_progress':event_progress(state),
        'continuity_policy':'Runtime/outcomes là hiện tại. Summary cũ có thể sai; không biến lỗi continuity thành âm mưu.',
        'celestial_policy':'Vàng và Bạch Kim là hai căn tính/ký ức riêng. Tên Hoàng Đạo Player gọi mặc định bản Bạch Kim nếu chưa chỉ rõ Vàng. Không lộ số chìa, chìa chưa định danh hoặc năng lực chưa bộc lộ.',
    }
    result['director_intent']=command_kind(result.get('directives',[]))
    if room.get('player', {}).get('identity_mode') in {'canon','original'}:
        result['canon_runtime']['celestial_policy'] = 'Theo hồ sơ vai Player và chìa canon của Lucy/Yukino. Không mặc định kho chìa Bạch Kim hoặc năng lực ẩn từ phiên khác.'
    # Keep nearby cast and useful encounter candidates, not forty unrelated roles.
    candidates=result.get('presence_candidates',[])
    selected=[c for c in candidates if c.get('active')]
    selected_ids={c['npc_id'] for c in selected}
    selected.extend(c for c in candidates[:12] if c['npc_id'] not in selected_ids)
    detailed_ids={item['speaker_id'] for item in result.get('characters',[])}
    result['presence_candidates']=[{key:value for key,value in candidate.items()
        if key in ({'npc_id','active','known_location','manual_locked'} if candidate['npc_id'] in detailed_ids
                   else {'npc_id','name','role','active','known_location','manual_locked'})}
        for candidate in selected]
    memory=result.get('memory',{})
    if isinstance(memory.get('recent_suggestions'),list):
        memory['recent_suggestions']=[s for s in memory['recent_suggestions']
            if s.get('status') not in {'accepted','expired'}][-6:]
    result['magic_constraints']={key:value for key,value in bible.get('world',{}).get('magic',{}).items()
                                  if key in {'source','classes','battle_resolution'}}
    for rule in result.get('world_rules',[]):
        if isinstance(rule,dict) and rule.get('path')=='magic.celestial_spirits':
            rule['value'] = (bible.get('world',{}).get('magic',{}).get('celestial_spirits','')
                             if room.get('player',{}).get('identity_mode') else
                             'Triệu hồi cần chìa, khế ước và ma lực. Vàng và Bạch Kim có căn tính/ký ức riêng. Khả năng chưa bộc lộ của Player không thuộc tri thức NPC.')
    for item in result.get('characters',[]):
        canon=catalog.get(item['speaker_id']) or room.get('roster',{}).get(item['speaker_id'],{})
        item.setdefault('profile',{}).update({key:value for key,value in canon.items() if key in {
            'name','voice','personality','magic','signature','limits','knowledge_limits','variant','base_spirit_id'}})
        if plain(item['profile'].get('relationship','')).startswith('chua quen player'):
            item['profile'].pop('relationship',None)
        item['profile']={k:v for k,v in item['profile'].items() if k in {
            'name','role','guild','voice','personality','magic','signature','limits',
            'knowledge_limits','variant','base_spirit_id'}}
        if plain(item['profile'].get('knowledge_limits','')).startswith('chi biet su kien toi dem truoc sky labyrinth'):
            item['profile'].pop('knowledge_limits',None)  # Common boundary already supplied once.
        if 'presentation' in item:
            item['presentation']={k:v for k,v in item['presentation'].items()
                if k not in item['profile'] or v!=item['profile'][k]}
            if not item['presentation']:item.pop('presentation')
    return result


def tracker_prompt(core,state,public,responses,directives,byte_limit):
    """Same serialized-message accounting as other profiles, keeping domain truth."""
    return pack_tracker_prompt(core,state,public,responses,directives,byte_limit)


def filter_presence(plan,room,catalog,evidence):
    """A spirit being mentioned is not an opening of its gate."""
    result=copy.deepcopy(plan)
    if not isinstance(result.get('presence_updates'),list):return result
    allowed=[]
    for update in result['presence_updates']:
        if not isinstance(update,dict):continue
        key=update.get('npc_id');profile=catalog.get(key,{})
        spirit=profile.get('variant')=='platinum_twin' or bool(profile.get('zodiac')) or (
            isinstance(key,str) and key.startswith('silver_'))
        if (update.get('action')=='enter' and spirit and
                not room.get('roster',{}).get(key,{}).get('is_active')):
            quote=str(update.get('evidence',''))
            if (not quote or quote.casefold() not in str(evidence).casefold() or not re.search(
                    r'\b(?:mo cong|trieu hoi|cong.*mo|buoc (?:ra|qua).*cong|hien ra.*cong)\b',plain(quote))
                    and not any(u['npc_id']==key and u['action']=='enter'
                        for u in spirit_presence_updates(room,catalog,quote))):
                continue
        allowed.append(update)
    result['presence_updates']=allowed
    return result


def spirit_presence_updates(room,catalog,narration):
    """Explicit local manifestation in current narration, not memory or mentions."""
    result={}
    for sentence in re.split(r'(?<=[.!?])\s+|\n+',str(narration)):
        text=plain(sentence)
        if re.search(r'\b(?:neu|gia su|cho den khi|doi den khi|ngay mai|hom qua|hoi tuong|nho lai|nghe noi|khong thay|chua thay|khong he|chua he|chua|khong)\b',text):continue
        for ident,npc in room.get('roster',{}).items():
            profile=catalog.get(ident,{})
            if not (profile.get('variant')=='platinum_twin' or profile.get('zodiac') or ident.startswith('silver_')):continue
            name=plain(npc.get('name') or profile.get('name',''))
            if not name:continue
            prefix=r'(?<!\w)'+re.escape(name)+r'(?!\w)'
            action=None
            if re.search(prefix+r'\s+(?:(?:da|vua|cung|dang|lien)\s+){0,3}(?:troi len|buoc ra|hien ra|dung yem tro|dao (?:dia dao|duong ham)|ghim|ep)',text):action='enter'
            if re.search(r'cong.{0,70}mo ra.{0,100}'+prefix,text):action='enter'
            if re.search(prefix+r'\s+(?:(?:da|vua|cung|lien)\s+){0,3}(?:bien mat|tro ve tinh linh gioi|rut vao cong)',text):action='leave'
            if action:
                result[ident]={'npc_id':ident,'action':action,'reason':'Lời dẫn hiện tại xác nhận tinh linh xuất hiện/rời cảnh.',
                               'evidence':sentence.strip()}
    return list(result.values())


def resolve_catalog_profile(profile,catalog,owned_ids):
    """Resolve existing canon before considering a new minor identity."""
    ident=profile.get('id')
    if isinstance(ident,str) and ident in catalog:return ident
    name=plain(profile.get('name','')).strip()
    if not name:return None
    exact=[key for key,value in catalog.items() if plain(value.get('name',''))==name]
    # Player-facing short names select its AU twin rather than borrowing a gold identity.
    twins=[key for key in owned_ids if key in catalog and name in spirit_aliases(catalog[key])]
    if len(twins)==1:return twins[0]
    return exact[0] if len(exact)==1 else None


def resolve_silver_profile(profile,registry):
    """Registry chooses identity, never a silver spirit's unestablished abilities."""
    ident=profile.get('id');name=plain(profile.get('name',''))
    for entry in registry.get('player_silver_keys',[]):
        if ident==entry.get('id') or (name and name==plain(entry.get('constellation',''))):
            return entry['id']
    return None


def _evidence(update,text):
    evidence=update.get('evidence')
    return isinstance(evidence,str) and len(evidence.strip())>=10 and evidence.casefold() in text.casefold()


def apply_updates(state,delta,room,catalog,evidence,allowed_teams=None):
    """Each invalid proposal is rejected without destroying a valid written scene.

    Records are small structural truth, not inferred from arbitrary old summaries.
    Evidence grounds a proposal; semantic truth still needs writer/tracker quality.
    """
    if not isinstance(delta,dict):return ['invalid_domain_delta']
    ledger=state.setdefault('canon_ledger',{'version':1,'matches':{},'summons':{}})
    ledger.setdefault('matches',{});ledger.setdefault('summons',{})
    standings=state.setdefault('competition_standings',{})
    issues=[]
    updates=delta.get('qualification_updates',[])
    for u in updates[:8] if isinstance(updates,list) else []:
        if not isinstance(u,dict) or not _evidence(u,evidence):issues.append('standing_without_evidence');continue
        team,rank=u.get('team_id'),u.get('rank')
        if not isinstance(team,str) or not re.fullmatch(r'[a-z0-9_-]{1,64}',team) or type(rank) is not int or not 1<=rank<=8:
            issues.append('invalid_standing');continue
        if allowed_teams is not None and team not in allowed_teams:
            issues.append('unknown_competition_team');continue
        if (team in standings and standings[team]!=rank) or any(k!=team and v==rank for k,v in standings.items()):
            issues.append('standing_rewrite_or_duplicate');continue
        standings[team]=rank
    updates=delta.get('match_updates',[])
    known=set(room.get('roster',{}))|{'player'}
    for u in updates[:4] if isinstance(updates,list) else []:
        if not isinstance(u,dict) or not _evidence(u,evidence):issues.append('match_without_evidence');continue
        ident,participants,status,winner=(u.get(k) for k in ('id','participants','status','winner_id'))
        if (not isinstance(ident,str) or not re.fullmatch(r'[a-z0-9_-]{1,80}',ident) or
                not isinstance(participants,list) or not 2<=len(participants)<=10 or
                any(not isinstance(p,str) or p not in known for p in participants) or
                len(set(participants))!=len(participants) or status not in ['active','completed'] or
                (winner is not None and winner not in participants) or (status=='active' and winner is not None)):
            issues.append('invalid_match');continue
        old=ledger['matches'].get(ident,{})
        if any(key!=ident and match.get('status')=='active' and set(match.get('participants',[]))==set(participants)
               for key,match in ledger['matches'].items()):
            issues.append('duplicate_active_match');continue
        if old and (old.get('participants')!=participants or (old.get('status')=='completed' and
                (status!='completed' or old.get('winner_id')!=winner))):
            issues.append('closed_match_or_identity_rewrite');continue
        ledger['matches'][ident]={key:copy.deepcopy(u.get(key)) for key in ('id','participants','status','winner_id','evidence')}
        ledger['matches'][ident]['evidence']=u['evidence'][:500]
    updates=delta.get('summon_updates',[])
    for u in updates[:8] if isinstance(updates,list) else []:
        if not isinstance(u,dict) or not _evidence(u,evidence):issues.append('summon_without_evidence');continue
        ident,action=u.get('spirit_id'),u.get('action')
        if not isinstance(ident,str) or ident not in known or action not in ['open','close']:
            issues.append('invalid_summon');continue
        canon=catalog.get(ident,{})
        if not (canon.get('variant')=='platinum_twin' or canon.get('zodiac') or ident.startswith('silver_')):
            issues.append('not_a_spirit');continue
        if bool(room.get('roster',{}).get(ident,{}).get('is_active')) != (action=='open'):
            issues.append('summon_presence_mismatch');continue
        ledger['summons'][ident]={'spirit_id':ident,'action':action,'evidence':u['evidence'][:500]}
    return issues


def validate_scene(raw,state,aliases,announcer_ids=()):
    """Guard clear official/narrator qualification claims; not a prose truth oracle."""
    standings=state.get('competition_standings',{})
    numbers={'mot':1,'nhat':1,'hai':2,'nhi':2,'ba':3,'tu':4,'bon':4,'nam':5,'sau':6,'bay':7,'tam':8}
    for response in raw.get('responses',[]):
        for segment in response.get('segments',[]):
            if segment.get('type')!='narration' and response.get('speaker_id') not in announcer_ids:
                continue
            # Dialogue can be rumor/lying; only explicit qualifier announcements are checked.
            text=plain(segment.get('text',''))
            if not re.search(r'vong loai|sky labyrinth',text):continue
            if re.search(r'\b(?:nham|sai|khong phai|tin don|dinh chinh)\b',text):continue
            for team,names in aliases.items():
                if team not in standings:continue
                for name in names:
                    pattern=re.escape(plain(name))+r'\s+(?:dung\s+|duoc\s+)?(?:o\s+)?(?:hang|vi tri)(?:\s+(?:thu|so))?\s+(\d+|mot|nhat|hai|nhi|ba|tu|bon|nam|sau|bay|tam)\b'
                    match=re.search(pattern,text)
                    if match:
                        rank=int(match[1]) if match[1].isdigit() else numbers[match[1]]
                        if rank!=standings[team]:raise ValueError(f'Qualification result contradicts established ledger: {team} is rank {standings[team]}, not {rank}')
