"""Bounded, source-backed PUBLIC recall; not another LLM summarization call.

Archives stay untouched. Old episodes are historical evidence, not a permission
to activate NPCs, reveal private thoughts or overwrite current state.
"""
import copy
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path


def _dump(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def _terms(value):
    value=''.join(c for c in unicodedata.normalize('NFD',str(value).casefold().replace('đ','d'))
                  if not unicodedata.combining(c))
    stop=set('toi ta em anh co cau toi ban nguoi no ho minh cua va la thi ma mot nhung cac da dang se chua khong cho voi tu tren duoi trong ngoai khi luc do nay ay nhu ngay noi loi nhan vat player chung lai len xuong hay giup duoc ve de ra vao that cung rat con chi sao gi nao di roi duoc biet hay nhe nhe nay xem tiep'.split())
    return {word for word in re.findall(r'\w{3,}',value) if word not in stop}


def public_pieces(entry):
    """Never fall back to raw mixed text after removing hidden-only segments."""
    if not isinstance(entry,dict):return []
    player=entry.get('is_player') is True
    if player and entry.get('intent') in {'thought','command'}:return []
    segments=entry.get('segments')
    if isinstance(segments,list) and segments:
        kinds={'speech','action'} if player else {'narration','dialogue'}
        return [(s['type'],s['text']) for s in segments if isinstance(s,dict)
                and s.get('type') in kinds and isinstance(s.get('text'),str)]
    if player and entry.get('intent')=='mixed':return []
    if player:return [('speech',str(entry.get('text','')))]
    pieces=[(key,entry[key]) for key in ('narration','dialogue') if isinstance(entry.get(key),str) and entry[key]]
    return pieces or [('legacy_public_text',str(entry.get('text','')))]


def _records(history,source):
    records=[]
    for index,entry in enumerate(history if isinstance(history,list) else []):
        for kind,text in public_pieces(entry):
            # Complete sentences/chunks, never cut a recalled quotation mid-word.
            chunks=re.split(r'(?<=[.!?。！？])\s+|\n+',text)
            buffer=''
            for sentence in chunks+['']:
                if buffer and (not sentence or len((buffer+' '+sentence).encode())>1400):
                    records.append({'source':source,'entry':index,'speaker_id':entry.get('speaker_id',
                        'player' if entry.get('is_player') else 'unknown'),
                        'kind':kind,'excerpt':buffer})
                    buffer=''
                if sentence and len(sentence.encode())<=1400:
                    buffer=(buffer+' '+sentence).strip()
    return records


@lru_cache(maxsize=96)
def _archive_records(path,mtime,size):
    try:
        data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        return _records(data.get('history',[]),Path(path).name)
    except (OSError,ValueError,AttributeError):return []


def protect_recent_exchange(context):
    """Keep latest Player input AND preceding scene through budget fitting."""
    history=context.get('history',[])
    if not history:return
    players=[i for i,e in enumerate(history) if e.get('speaker_id')=='player']
    # Last two player turns plus intervening scene, bounded by supplied history.
    start=players[-2] if len(players)>=2 else 0
    context['context_policy']={'protected_history_count':len(history)-start}


def recall(room_dir,room,query,catalog,max_bytes=5000):
    """Small lexical/rarity-ranked recall with source IDs and explicit trust labels.

    No semantic assertion is inferred from a quotation. At most 256 archive files
    and 8 MB are scanned locally; output is bounded independently of save length.
    """
    state=room.get('narrative_state',{})
    records=[]
    directory=Path(room_dir)/'memory'/'archives'
    scanned=0;file_count=0;limited=False
    try:files=sorted(directory.glob('checkpoint-*.json'))
    except OSError:files=[]
    for path in files[-256:]:
        try:
            stat=path.stat()
            if stat.st_size>500000 or scanned+stat.st_size>8000000:
                limited=True;continue
            scanned+=stat.st_size;file_count+=1
            records.extend(_archive_records(str(path.resolve()),stat.st_mtime_ns,stat.st_size))
        except OSError:continue
    active=room.get('history',[])
    # The input currently being answered already has its own protected channel.
    records.extend(_records(active[:-1] if active and active[-1].get('is_player') else active,'active_history'))
    facts=state.get('facts',[])
    for field,values in [('facts',facts),('known_outcomes',state.get('checkpoint_memory',{}).get('known_outcomes',[]))]:
        for index,text in enumerate(values if isinstance(values,list) else []):
            if isinstance(text,str) and len(text.encode())<=1400:
                records.append({'source':field,'entry':index,'speaker_id':'not_assigned',
                    'kind':'stored_claim' if field=='facts' else 'summary_outcome','excerpt':text})
    tokens=[_terms(record['excerpt']) for record in records]
    df=Counter(term for words in tokens for term in words)
    query_terms=_terms(query)
    entity_terms=set()
    for profile in catalog.values():
        if not isinstance(profile,dict):continue
        names=[profile.get('name',''),profile.get('base_spirit_id','')]
        for name in names:
            terms=_terms(name)
            if terms and terms<=query_terms:entity_terms.update(terms)
    # On generic continuation, use the recent actual topic rather than arbitrary
    # newest archive. No Player private thought enters this topic expansion.
    if len(query_terms)<2:
        for entry in room.get('history',[])[-4:]:
            for _,text in public_pieces(entry):query_terms.update(_terms(text))
    ranked=[];total=max(1,len(records))
    for index,(record,words) in enumerate(zip(records,tokens)):
        overlap=words & query_terms
        if not overlap:continue
        if entity_terms and not words & entity_terms:continue
        score=sum(math.log(1+total/(1+df[t])) for t in overlap)/math.sqrt(max(1,len(words))/12)
        score+=8*len(words & entity_terms)
        if record['source']=='facts' and record['entry']>=len(facts)-3:score+=1
        ranked.append((score,index,record))
    result={'policy':'Trích lịch sử công khai có nguồn, KHÔNG phải tri thức chung của mọi NPC hoặc trạng thái hiện diện hiện tại. Lời nói là lời nhân vật; stored_claim/summary có thể sai. Đối chiếu chronology và ledger, không coi hồi ức là triệu hồi mới.',
            'records':[],'coverage':{'archive_files_scanned':file_count,'limited':limited or len(files)>256}}
    selected=[];seen=set()
    for score,index,record in sorted(ranked,key=lambda row:(row[0],row[1]),reverse=True):
        digest=hashlib.sha256(record['excerpt'].encode()).hexdigest()
        if digest in seen:continue
        candidate=copy.deepcopy(record)
        candidate['memory_id']=digest[:16]
        tentative={**result,'records':[item[1] for item in selected]+[candidate]}
        if len(_dump(tentative).encode())>max_bytes:continue
        selected.append((index,candidate));seen.add(digest)
        if len(selected)>=6:break
    result['records']=[item[1] for item in sorted(selected)]
    return result
