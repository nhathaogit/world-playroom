"""Opt-in supporting cast for a companion. The principal remains the only lead.

All changes belong to execute_turn's existing snapshot/rollback transaction.
No provider calls, save writes or imports of another room's runtime.
"""
import copy
import re
import unicodedata
import companion_scene

_OFFSTAGE=re.compile(r'\b(?:nếu|giả sử|nhắc đến|nhắc tới|kể về|nghe nói|hồi tưởng|hôm qua|ngày mai|chưa tới|chưa đến|định gọi|sẽ gọi)\b',re.I)


def enabled(bible):
    return bible.get('session_mode')=='companion' and bible.get('supporting_cast',{}).get('enabled') is True


def principal_id(bible):
    return next(iter(bible.get('characters',{})),None)


def _key(value):
    return ' '.join(str(value or '').casefold().split())


def _slug(name):
    value=unicodedata.normalize('NFKD',name.lower().replace('đ','d'))
    return 'npc-'+re.sub(r'[^a-z0-9]+','-', ''.join(c for c in value if not unicodedata.combining(c))).strip('-')


def _narration(raw):
    return '\n'.join(s.get('text','') for r in raw.get('responses',[]) if isinstance(r,dict)
                     for s in r.get('segments',[]) if isinstance(s,dict) and s.get('type')=='narration' and isinstance(s.get('text'),str))


def _locked(room,npc):
    return npc.get('presence_source')=='manual' and npc.get('presence_lock_scene')==room.get('setting')


def _presence(room,ident,active,reason):
    npc=room['roster'][ident]
    if _locked(room,npc) and bool(npc.get('is_active'))!=active:
        return False
    changed=bool(npc.get('is_active'))!=active or (active and npc.get('scene_scope')!=room.get('setting'))
    npc['is_active']=active
    npc['presence_source']='auto' if not _locked(room,npc) else 'manual'
    npc['presence_reason']=str(reason)[:400]
    if active:
        npc['scene_scope']=room.get('setting','')
        npc['retired']=False
    if changed:
        event={'npc_id':ident,'name':npc['name'],'action':'enter' if active else 'leave',
               'source':'auto','reason':str(reason)[:400]}
        room.setdefault('history',[]).append({'sender':'System','is_player':False,'presence_event':event,
            'text':f"[HIỆN DIỆN]: {npc['name']} {'có mặt trong cảnh' if active else 'rời khỏi cảnh'}. {event['reason']}"})
    return True


def _make_npc(ident,name,values,quote,turn,channel='in_person'):
    return {'id':ident,'canon_id':'','name':name,
        **{k:str(values.get(k,''))[:1600] for k in ('role','personality','tone','scene_function','opening_knowledge')},
        'aliases':list(values.get('aliases',[])),
        'temporary':True,'supporting':True,'can_loveline':False,'loveline_hint':'',
        'relationship':'Nhân vật phụ; quan hệ theo những gì đã diễn ra.',
        'avatar':'','is_active':False,'introduced_turn':turn,'last_seen_turn':turn,
        'presence_channel':channel,'introduction_evidence':quote,'retire_after_turns':8}


def _forms(ident,profile):
    return [str(x) for x in [ident,profile.get('name'),*profile.get('aliases',[])] if isinstance(x,str) and x.strip()]


def bootstrap_present_roles(room,bible,directives=()):
    """Materialize authored role identities already physically established.

    Never create every family member on load, use thoughts, or revive someone
    from a scene before the last boundary/committed departure.
    """
    if not enabled(bible):return set()
    history=room.get('history',[])
    boundary=max((i+1 for i,e in enumerate(history) if e.get('scene_event')),default=0)
    turn=room.get('narrative_state',{}).get('scene_pacing',{}).get('turn_number',0)
    roster=room.setdefault('roster',{})
    registered=set()
    for ident,profile in bible.get('supporting_cast',{}).get('known_profiles',{}).items():
        if not profile.get('auto_register_when_present'):continue
        start=max(boundary,max((i+1 for i,e in enumerate(history)
            if e.get('presence_event',{}).get('npc_id')==ident and e['presence_event'].get('action')=='leave'),default=0))
        chunks=[s.get('text','') for e in history[start:] if not e.get('is_player')
                for s in e.get('segments',[]) if s.get('type')=='narration']
        latest=next((e for e in reversed(history) if e.get('is_player')), {})
        chunks += [s.get('text','') for s in latest.get('segments',[]) if s.get('type')=='action']
        chunks += list(directives)
        forms=_forms(ident,profile)
        quote=next((sentence for text in chunks for sentence in re.split(r'(?<=[.!?])\s+|\n+',text)
            if not _OFFSTAGE.search(sentence)
            and any(re.search(r'(?<!\w)'+re.escape(form)+r'(?!\w)',sentence,re.I) for form in forms)
            and re.search(r'\b(?:ngồi|đứng|nhấp|uống|đặt|nâng|lên tiếng|nói|hỏi|đáp|nhìn|liếc|nhướng|cười|dùng trà|bước vào|có mặt)\b',sentence,re.I)),None)
        if not quote:continue
        # Reuse an existing identity bearing this full name/role alias.
        existing=next((k for k,v in roster.items() if k==ident or _key(v.get('name'))==_key(profile.get('name'))),None)
        key=existing or ident
        if key not in roster:roster[key]=_make_npc(key,profile['name'],profile,quote,turn)
        if roster[key].get('supporting') and _presence(room,key,True,quote):registered.add(key)
    return registered


def requested_speakers(room,bible,directives):
    required=set()
    for ident,npc in room.get('roster',{}).items():
        if not npc.get('supporting') or not npc.get('is_active'):continue
        forms=_forms(ident,npc)
        for directive in directives:
            if _OFFSTAGE.search(str(directive)):continue
            if any(re.search(r'(?<!\w)'+re.escape(form)+r'(?!\w)',str(directive),re.I) for form in forms) and re.search(r'lên tiếng|nói trước|trả lời|đáp lời|thoại',str(directive),re.I):
                required.add(ident)
    return required


def project_context(context,room,bible):
    """Registry references are profiles to reuse, never proof of attendance."""
    result=copy.deepcopy(context)
    known={**bible.get('supporting_cast',{}).get('known_profiles',{})}
    known.update({k:v for k,v in room.get('roster',{}).items() if v.get('supporting')})
    result['supporting_cast']={'enabled':True,'principal_id':principal_id(bible),
        'policy':'Nhân vật phụ có tên/tính cách/giọng/vai trò. Dùng lại ID và hồ sơ; chỉ biết điều họ chứng kiến hoặc được kể. Hồ sơ không có nghĩa đang hiện diện. Không chiếm tuyến chính hay mặc định loveline.',
        'known_profiles':[{'id':k,**{field:v.get(field,'') for field in
            ('name','role','personality','tone','opening_knowledge','aliases')}} for k,v in list(known.items())[-20:]],
        'requested_speaker_ids':sorted(requested_speakers(room,bible,result.get('directives',[])))}
    return result


def prepare(room,bible,raw,public_text,directives):
    """Register named introductions and stage entrances/departures before speech."""
    roster=room.setdefault('roster',{})
    principal=principal_id(bible)
    library=bible.get('supporting_cast',{}).get('known_profiles',{})
    narration=_narration(raw)
    latest=next((e for e in reversed(room.get('history',[])) if e.get('is_player')), {})
    actions=[s.get('text','') for s in latest.get('segments',[]) if s.get('type')=='action']
    companion_scene.commit(room,raw,directives,actions,narration)
    runtime=room.setdefault('narrative_state',{}).setdefault('supporting_cast',{})
    runtime.setdefault('presence_history_start',len(room.get('history',[])))
    source='\n'.join([public_text,narration,*directives])
    turn=room.get('narrative_state',{}).get('scene_pacing',{}).get('turn_number',0)
    pending={k for k,v in roster.items() if v.get('supporting') and v.get('is_active')
             and v.get('scene_scope')!=room.get('setting')}
    registered=set(); references={}

    def resolve(reference,name=''):
        if reference in roster or reference in library:return reference
        candidates=[k for k,v in {**library,**roster}.items()
                    if any(_key(form) in {_key(reference),_key(name)} for form in _forms(k,v))]
        return candidates[0] if len(candidates)==1 else None

    def evidence(profile,name):
        quote=str(profile.get('evidence') or '').strip()
        if quote and quote.casefold() in source.casefold() and not _OFFSTAGE.search(quote):
            return quote
        # Known characters may return without re-authoring an unchanged profile.
        identity=next((v for v in [*roster.values(),*library.values()] if v.get('name')==name),{})
        forms=[name,*identity.get('aliases',[])]
        for sentence in re.split(r'(?<=[.!?])\s+|\n+', '\n'.join([narration,*directives,*actions])):
            if any(form and re.search(r'(?<!\w)'+re.escape(form)+r'(?!\w)',sentence,re.I) for form in forms) and not _OFFSTAGE.search(sentence):
                return sentence
        return ''

    def register(profile):
        name=str(profile.get('name') or '').strip()
        reference=str(profile.get('id') or '').strip()
        ident=resolve(reference,name)
        if ident==principal:
            return principal
        if ident in roster and not roster[ident].get('supporting'):
            return None
        canonical=library.get(ident,{})
        name=canonical.get('name') or (roster.get(ident,{}) if ident else {}).get('name') or name
        quote=evidence(profile,name)
        if not quote:return None
        channel=profile.get('channel')
        if channel not in {'in_person','phone','message'}:
            channel=('phone' if re.search(r'điện thoại|đầu dây|nhận máy|nghe máy|loa ngoài|cuộc gọi',quote,re.I)
                     else 'message' if re.search(r'tin nhắn|vừa nhắn|nhắn lại',quote,re.I) else 'in_person')
        if ident not in roster:
            values={**profile,**canonical}
            if not name or len(name)>100 or not all(isinstance(values.get(k),str) and values[k].strip()
                    for k in ('role','personality','tone','scene_function')):
                return None
            if not ident:
                ident=reference.lower() if re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}',reference.lower()) else _slug(name)
                if ident in roster:return None
            roster[ident]=_make_npc(ident,name,values,quote,turn,channel)
        references[name]=ident
        roster[ident]['presence_channel']=channel
        if reference:references[reference]=ident
        registered.add(ident)
        if _presence(room,ident,True,quote):pending.discard(ident)
        return ident

    profiles=raw.get('npc_introductions',[])
    if not isinstance(profiles,list):raise ValueError('npc_introductions must be an array')
    for profile in profiles[:12]:
        if isinstance(profile,dict):register(profile)
    for response in raw.get('responses',[]):
        if not isinstance(response,dict):continue
        reference=response.get('speaker_id')
        ident=references.get(reference) or resolve(reference)
        if ident in library and ident not in roster:
            register({'id':ident,'name':library[ident].get('name')})
        elif ident in roster and roster[ident].get('supporting') and (
                not roster[ident].get('is_active') or ident in pending):
            register({'id':ident,'name':roster[ident]['name']})
        if ident:response['speaker_id']=ident
        elif reference in references:response['speaker_id']=references[reference]
    updates=raw.get('presence_updates',[])
    for update in updates if isinstance(updates,list) else []:
        if not isinstance(update,dict):continue
        ident=references.get(update.get('npc_id')) or resolve(update.get('npc_id'))
        if ident not in roster or not roster[ident].get('supporting'):continue
        quote=str(update.get('evidence') or '').strip()
        if not quote or quote.casefold() not in source.casefold() or _OFFSTAGE.search(quote):continue
        if update.get('action')=='leave':pending.add(ident)
        elif update.get('action')=='enter' and _presence(room,ident,True,quote):
            pending.discard(ident);registered.add(ident)
    room.setdefault('narrative_state',{}).setdefault('supporting_cast',{})['pending_departures']=sorted(pending)
    return registered


def finalize(room,bible,responses):
    """Apply exits after farewell dialogue. Keep retired identities for reuse."""
    state=room.setdefault('narrative_state',{})
    runtime=state.setdefault('supporting_cast',{})
    turn=state.get('scene_pacing',{}).get('turn_number',0)
    for ident in runtime.pop('pending_departures',[]):
        if room.get('roster',{}).get(ident,{}).get('supporting'):
            _presence(room,ident,False,'Đã rời cảnh hoặc hoàn thành vai trò theo diễn biến.')
            room['roster'][ident]['last_seen_turn']=turn
    for response in responses:
        npc=room.get('roster',{}).get(response.get('speaker_id'),{})
        if npc.get('supporting'):npc['last_seen_turn']=turn
    for npc in room.get('roster',{}).values():
        if npc.get('supporting') and not npc.get('is_active') and turn-npc.get('last_seen_turn',turn)>=8:
            npc['retired']=True
    room['active_character_ids']=[k for k,v in room.get('roster',{}).items() if v.get('is_active')]
    start=runtime.pop('presence_history_start',len(room.get('history',[])))
    return [e['presence_event'] for e in room.get('history',[])[start:] if isinstance(e.get('presence_event'),dict)]
