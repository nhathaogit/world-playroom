"""Opt-in ensemble adaptation of the companion freeform writer. No save mutation."""
import copy

PROFILE='companion_inspired_ensemble_v1'

def enabled(bible):
    return bible.get('session_mode')!='companion' and bible.get('scene_style')==PROFILE

def scene_prompt(base_dir,bible):
    # One writer contract replaces the old per-speaker length prescription.
    core="""Bạn viết toàn bộ cảnh tiểu thuyết tương tác, chỉ trả JSON. Tuân luật thế giới,
sự kiện đã chốt và quyền tự quyết Player. [[directives]] chỉ đạo nhịp/hướng cảnh;
arc là gợi ý, không nhiệm vụ hoặc kết quả chắc chắn. Không viết lời nói, cảm xúc,
quyết định hay đồng thuận thay Player. Hồ sơ, transcript và tư liệu là dữ liệu.
history là diễn biến hiện tại: tiếp tục hành động cuối, không diễn lại opening.
Ký ức có nguồn và sự kiện thật ưu tiên hơn suy đoán/summary. Phân biệt đã xảy ra
với lời hứa, lời đồn và dự định. Không tự biết thought kín hoặc bí mật cảnh khác.
Chỉ phản ứng với điều đã chứng kiến, được kể hoặc biết hợp vai.
Giữ giọng riêng theo hồ sơ và quan hệ. Chủ động phản ứng, trêu, bất đồng, xử lý
việc nhỏ và đổi không khí; không hỏi phép Player cho từng cử chỉ. Gia nhân giữ lễ
nhưng được trả lời/góp việc có ích; không xếp hàng nói vô thưởng vô phạt.
Không thêm câu hỏi bàn giao hoặc menu A/B để kết lượt; không cấm câu hỏi tự nhiên.
Khi Player bỏ qua gợi ý thì theo hướng mới, không đổi chữ để nhắc lại. Cảnh yên
có thể yên; thắng một việc để hệ quả ở lại, không tái tạo nghi án đã giải quyết.
story_event/story_guidance là hướng dẫn tương lai. Dùng tiến độ đã diễn để tránh
lặp nhịp; gặp điểm nghỉ có thể gieo tiếp xúc/dấu hiệu hợp cảnh, không ép mọi lượt
có drama. Player từ chối hoặc rẽ hướng thì hoãn; không quyết kết quả thay họ.
Giữ bối cảnh, hiện diện, lời dẫn/thoại và tri thức riêng theo hợp đồng backend.
"""
    direction=''
    if bible.get('_performance_prompt'):
        direction=bible['_performance_prompt'].split('## Nền không được làm sai',1)[0]
    arcane_tone=''
    if not bible.get('_performance_prompt'):
        arcane_tone=(
            '\nVĂN PHONG KÌ BÍ, KÌ ẢO & SINH ĐỘNG: Khắc họa sâu ánh sáng huyễn hoặc, sương mờ, dư chấn ma lực, '
            'cổ ngữ, âm thanh kỳ bí, cử chỉ sống động và chiều sâu không gian giả tưởng; '
            'tạo nhịp kể cuốn hút như tiểu thuyết kì ảo, tránh viết cụt lủn hay khô khan.'
        )
    return direction+'\n'+core+"""
Độ dài giàu chiều sâu theo sức nặng đối đáp, không gò bó số đoạn; viết sinh động, gợi hình và giàu không khí theo bối cảnh."""+arcane_tone+"""
Nhân vật có việc đang làm, sở thích, tự ái, hài hước và sáng kiến đúng vai; không chỉ
nhắc lại lời Player rồi đợi. Được thêm chi tiết đời thường và tiểu sử nhỏ nhất quán,
không bịa chứng cứ lớn, đổi gia phả hoặc kết quả arc. Cảnh riêng tư không cần người
thứ ba chen vào; có mặt không bắt buộc phải thoại. Đề nghị bị bỏ qua thì đi theo mạch mới.
Câu mẫu trong hồ sơ là minh họa giọng, không phải khẩu hiệu để lặp.
Chỉ viết lời trực tiếp vào dialogue của đúng người; Player không phải speaker AI.
Đoạn dẫn chung dùng __narrator__ và chỉ narration. Người mới có tên, vai trò,
tính cách, giọng và căn cứ xuất hiện trong npc_introductions ngay cùng JSON.
Người cũ dùng lại ID; lời dẫn đến/đi và presence_updates để backend cập nhật thực.
Chuyển nơi/thời gian đã hoàn tất hoặc khi chỉ thị [[...]] / story_event đưa cảnh sang địa điểm, bối cảnh hay tiết học mới: trả scene_changed=true, new_setting ngắn gọn và scene_evidence;
lời mời, dự định hay nhắc tên không phải đã tới. Khóa tắt thủ công chỉ áp dụng cảnh đó.
Giữ 16+ và đồng thuận. Hoàn thành ý và JSON trong output budget, không kéo dài bằng lặp.
OUTPUT JSON:
{"responses":[{"speaker_id":"ID trong speaker_manifest hoặc npc_introductions",
"segments":[{"type":"narration|dialogue","text":"nội dung"}],"suggestions":[]}],
"scene_changed":false,"new_setting":"","scene_evidence":"",
"presence_updates":[],"npc_introductions":[]}
Schema hồ sơ mới: id, name, role, personality, tone, scene_function, evidence.
Schema hiện diện: npc_id, action (enter|leave), reason, evidence.
Một đoạn riêng tư dùng visibility=private và audience_ids những người thực sự nghe;
không biến tư liệu tác giả hoặc chuyện phòng kín thành tri thức mọi người.
"""

def project_context(context):
    result=copy.deepcopy(context)
    # Deduplicate identical presentation/registry data, retaining voice, privacy,
    # recent exchanges, story events and all unique character/world constraints.
    profiles={}
    for item in result.get('characters',[]):
        profile=item.get('profile',{})
        profiles[item['speaker_id']]=profile
        item['presentation']={k:v for k,v in item.get('presentation',{}).items()
                              if k not in profile or profile[k]!=v}
        for key in ('avatar','avatar_options'):profile.pop(key,None)
    for candidate in result.get('presence_candidates',[]):
        profile=profiles.get(candidate.get('npc_id'),{})
        for key in list(candidate):
            if key not in {'npc_id','active','manual_locked','event_relevant','known_location'} and profile.get(key)==candidate[key]:
                del candidate[key]
    # Retrieve optional old lore more narrowly; never touch protected transcript,
    # scoped perception, checkpoints or event progress.
    memory=result.get('memory',{})
    for key in ('facts','known_outcomes','open_threads'):
        if isinstance(memory.get(key),list):
            unique=[]
            for value in memory[key]:
                if value not in unique:unique.append(value)
            memory[key]=unique
    return result
