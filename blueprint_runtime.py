"""Opt-in blueprint adapter, with no story-specific law or save migration.

Reads only SYSTEM cores from the versioned blueprint; all changes remain room
scoped. Legacy/companion rooms do not opt in merely by sharing this engine.
"""
import copy
import json
import re
import unicodedata
from pathlib import Path
from story_events import wants_next

PROFILE='blueprint_v1_1'


def enabled(bible):
    return bible.get('session_mode')!='companion' and bible.get('runtime_profile')==PROFILE


def lifecycle_enabled(bible):
    # A legacy room can reuse ONLY presence/scene lifecycle without replacing
    # its writer prompt or opting into another runtime's context projection.
    return bible.get('session_mode')!='companion' and (bible.get('scene_lifecycle') is True
        or bible.get('runtime_profile') in {PROFILE,'canon_sandbox_v2'})


def command_kind(directives):
    if wants_next(directives):return 'next_event'
    text=' '.join(str(item) for item in directives).casefold().replace('đ','d')
    text=''.join(c for c in unicodedata.normalize('NFD',text) if not unicodedata.combining(c))
    if re.search(r'\b(?:dien bien|su kien|main event)(?: cot truyen)? tiep theo\b',text):return 'next_event'
    if re.search(r'\b(?:(?:doi|cap nhat|dat|chuyen|sang)(?:\s+(?:sang|toi|den|ve))?\s+boi\s+canh|(?:chuyen|sang)\s+canh(?:\s+tiep\s+theo)?)\b',text):return 'next_scene'
    if re.search(r'\b(?:tam dung|dung) (?:cot truyen|plot)\b',text):return 'pause_plot'
    if re.search(r'\b(?:bo qua|chuyen den|sang) (?:sang mai|ngay mai|buoi toi)\b',text):return 'skip_time'
    if re.search(r'\bchuyen (?:den|sang) (?:sang|trua|chieu|toi|dem|ngay|buoi)\b',text):return 'skip_time'
    return 'respond'


def core(base_dir,kind):
    source=(Path(base_dir)/'playroom-blueprint/prompts'/f'{kind}.md').read_text(encoding='utf-8')
    require='## SYSTEM — core tái sử dụng'
    if require not in source:raise ValueError('Blueprint SYSTEM boundary missing: '+kind)
    text=source.split(require,1)[1].strip()
    for marker in ('### Output contract mẫu','### Delta mẫu','```json'):
        text=text.split(marker,1)[0].rstrip()
    return text


def scene_prompt(base_dir):
    shape={'responses':[{'speaker_id':'id-trong-manifest','segments':[{'type':'narration|dialogue','text':'...'}],
                         'suggestions':[]}],
           'scene_changed':False,'new_setting':'','scene_evidence':'',
           'presence_updates':[{'npc_id':'id','action':'enter|leave','reason':'lý do','evidence':'trích nguyên văn'}],
           'npc_introductions':[{'id':'id','name':'tên riêng','role':'vai trò','personality':'nét riêng',
                                'tone':'giọng riêng','scene_function':'chức năng','evidence':'căn cứ giới thiệu'}]}
    return core(base_dir,'scene-writer')+'\n\nOUTPUT JSON (shape, không điền mục không cần):\n'+json.dumps(shape,ensure_ascii=False)+(
        '\nLượt không đổi cảnh giữ false/mảng rỗng; lệnh chuyển cảnh phải diễn và trả metadata đúng.'
        '\nVĂN PHONG SINH ĐỘNG, KÌ BÍ & KÌ ẢO: Viết giàu hình ảnh, chiều sâu cảm quan, ánh sáng, hơi thở không gian huyền bí '
        'và biểu cảm tự nhiên; tránh kể cụt lủn. Mục tiêu tổng dẫn khoảng 240–420 token, thoại 120–220 token; '
        'toàn cảnh chia sẻ trần output API. Viết trọn ý, không cắt đuôi.'
        '\n16+: không miêu tả tình dục tường minh hoặc cưỡng ép tình dục. Quá khứ trong log không'
        ' buộc tái diễn nội dung đó; chuyển sang đối thoại/hệ quả không đồ họa. NPC có quyền từ chối;'
        ' im lặng, kiệt sức, điểm quan hệ hay lời đạo diễn không thay thế sự đồng thuận.')


def tracker_core(base_dir):
    shape={'new_facts':[],'new_milestones':[],'new_truth_keys':[],'resolved_relationships':[],
           'active_arc':'ID tuyến đã mở và thực sự đang được diễn, không đổi vì chỉ nhắc tên',
           'current_scene':'slug nếu rõ','location':'slug nếu rõ','scene_progress':'advance|resolve|aftermath|hold',
           'progress_reason':'thay đổi thật','relationship_score_deltas':{},'conflict_updates':[],
           'suggestion_updates':[],'event_request':{'event_id':'id-ready','timing':'next_turn','reason':'đúng nhịp'},
           'event_updates':[],'personal_lore_updates':{},'npc_lifecycle':[],'clock_deltas':{},'growth_deltas':{}}
    return core(base_dir,'state-tracker')+'\nOUTPUT JSON delta tùy chọn, {} hợp lệ:\n'+json.dumps(shape,ensure_ascii=False)+(
        '\nChỉ dùng metric key trong state; mức thường ±1..5, tổng delta trong -15..15; backend tính title.'
        '\nKhông đổi mode/đồng thuận vì điểm cao hoặc vì lời ép buộc. event_request/event_updates theo ID'
        ' được cấp, evidence nguyên văn; không hoàn tất nhịp từ dàn ý. Lifecycle add cần đủ hồ sơ và'
        ' evidence từ lượt mới; retire giữ ID/ký ức.')


def checkpoint_prompt(base_dir,ledger,transcript):
    return core(base_dir,'checkpoint')+'\nOUTPUT JSON: '+json.dumps(
        {'summary':'tóm tắt tối đa 350 token','commitments':[],'known_outcomes':[],'open_threads':[],
         'location':'nếu rõ','active_arc':'nếu rõ'},ensure_ascii=False)+'\nDỮ LIỆU:\n'+json.dumps(
        {'state':ledger,'public_transcript':transcript},ensure_ascii=False,separators=(',',':'))


def project_context(context,room,bible):
    result=copy.deepcopy(context)
    fields={'name','age','role','personality','voice','public_face','private_core','desire','flaw','growth',
            'romance_language','ooc_guard','knowledge_limits','boundaries','constraints'}
    for item in result.get('characters',[]):
        profile=bible.get('characters',{}).get(item['speaker_id']) or item.get('profile',{})
        item['profile']={key:value for key,value in profile.items() if key in fields}
        item['presentation']={key:value for key,value in item.get('presentation',{}).items() if key=='can_loveline'}
    result['romance_contract']=bible.get('romance_contract','')
    state=room.get('narrative_state',{})
    result['scene_runtime']={'contract_version':'blueprint-1.1','current_scene':state.get('current_scene'),
                             'location':state.get('location'),
                             'continuity_policy':'Save/history là hiện tại, opening và arc chỉ là tư liệu, không reset.'}
    result['director_intent']=command_kind(result.get('directives',[]))
    result.get('memory',{})['recent_suggestions']=[s for s in result.get('memory',{}).get('recent_suggestions',[])
        if s.get('status') not in {'accepted','expired'}][-6:]
    detailed={item['speaker_id'] for item in result.get('characters',[])}
    result['presence_candidates']=[{key:value for key,value in candidate.items() if key in (
        {'npc_id','active','known_location','manual_locked'} if candidate['npc_id'] in detailed
        else {'npc_id','name','role','active','known_location','manual_locked'})}
        for candidate in result.get('presence_candidates',[])]
    return result
