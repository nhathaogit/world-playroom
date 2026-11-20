"""Scene/location commits for one-character rooms, independent of cast toggles."""
import re
import logging
import copy

_DIRECT = re.compile(r'^\s*(?:(?:cập nhật|đổi|đặt)\s+bối cảnh\s*(?:là|thành|sang|:)|chuyển\s+cảnh\s+(?:đến|sang|tới))\s*(.+?)\s*$', re.I)
_TIME_SCENE = re.compile(
    r'^\s*(?:tối đến|sáng hôm sau|chiều hôm sau|tối hôm sau|ngày hôm sau|'
    r'mười năm sau|\d+ năm sau)\s*[,.:—-]\s*.+', re.I)

_VOICE_EXAMPLES = {}
_HYPOTHETICAL = re.compile(r'\b(?:nếu|giả sử|ước gì|dự định|muốn tới|muốn đến|rủ|đề nghị|hẹn gặp|(?:định|sẽ|sắp|chưa|không)\s+(?:đến|tới|vào|rời|trở về))\b',re.I)


def _asserted_evidence(anchor):
    # A present scene may be described without a movement keyword, e.g.
    # "Trong phòng ngủ, đèn đã tắt." Check clauses rather than vetoing a whole
    # arrival paragraph because a later, unrelated sentence contains "sẽ".
    clauses=re.split(r'(?<=[.!?])\s+|[\n;]+',anchor)
    return any(clause.strip() and not _HYPOTHETICAL.search(clause) for clause in clauses)


def _skip(reason):
    logging.getLogger(__name__).warning('Ignored optional companion scene metadata: %s',reason)
    return False


def explicit_setting(directives):
    """Only an explicit director assignment, never dialogue/hidden thoughts."""
    for text in reversed(directives):
        match=_DIRECT.fullmatch(str(text))
        if match:
            target=match[1].strip().rstrip('.').strip()
            if 0<len(target)<=1200:
                return target,str(text).strip()
        # A completed director time jump establishes the scene before writing.
        # Ordinary dialogue and future/hypothetical travel never enter this path.
        if _TIME_SCENE.fullmatch(str(text)) and not _HYPOTHETICAL.search(str(text)):
            target = str(text).strip().rstrip('.').strip()
            if len(target) <= 1200:
                return target, str(text).strip()
    return None


def project_context(context, room):
    """Project current companion identity without changing the authored profile."""
    result = copy.deepcopy(context)
    continuing = (sum(bool(e.get('is_player')) for e in room.get('history', [])
                      if isinstance(e, dict)) > 1
                  or room.get('narrative_state', {}).get('checkpoint_memory', {}).get('checkpoint_count', 0) > 0)
    if continuing:
        result.pop('background_before_chat', None)
    current_characters = []
    for item in result.get('characters', []):
        npc = room.get('roster', {}).get(item['speaker_id'], {})
        profile = item.get('profile', {})
        role = str(npc.get('role') or profile.get('role', ''))
        if continuing and role and role != profile.get('role', ''):
            # Runtime role can change after years of play. Opening age, student
            # authority and opening-specific biography must not reset it.
            for field in ('age', 'backstory', 'authority', 'initiative', 'everyday_texture'):
                profile.pop(field, None)
            profile['role'] = role
        if continuing:
            profile.pop('opening_knowledge', None)
        examples = _VOICE_EXAMPLES.get(item['speaker_id'])
        if examples:
            profile['dialogue_examples'] = copy.deepcopy(examples)
            profile['dialogue_example_policy'] = (
                'Ví dụ khẩu khí và nhịp phản ứng, chưa xảy ra trong truyện. '
                'Viết câu mới theo tình huống; không chép câu mẫu hoặc tự thêm đạo cụ trong ví dụ. '
                'Giữ lời thoại của người đang sống cùng Player, tránh giọng xác nhận yêu cầu và giải thích dụng ý.')
        current_characters.append({'speaker_id': item['speaker_id'], 'role': role,
                                   'relationship': item.get('relationship', {})})
    result['current_companion_state'] = {
        'setting': room.get('setting', ''),
        'current_directives': result.get('directives', []),
        'characters': current_characters,
        'policy': 'Trạng thái hiện tại và chỉ thị mới nhất ưu tiên hơn cảnh trong history cũ. '
                  'Hồ sơ mô tả tính cách, không đặt lại tuổi, nghề hoặc quan hệ đã thay đổi. '
                  'Tiếp diễn phản ứng ngay lúc này; không tự tua tới sáng mai hay kết thúc cả cuộc gặp.'}
    result['current_companion_state']['writing_policy'] = (
        'Lượt cũ là bằng chứng sự kiện, không là mẫu văn phong phải bắt chước. '
        'Tiếp tục hành động đang diễn bằng lời đáp có tính cách và hành động quan sát được. '
        'Đừng giải thích mỗi cử chỉ chứng tỏ tôn trọng, chủ động, kiềm chế hay lựa chọn gì. '
        'Nhân vật không cần thông báo rằng họ đã hiểu yêu cầu trước khi thực hiện một việc bình thường.')
    return result


def commit(room, plan, directives=(), actions=(), narration=''):
    """Commit validated scene metadata or an explicit current director setting.

    Do not activate/deactivate the principal, import NPCs or call another model.
    Caller owns the existing whole-turn snapshot/rollback transaction.
    """
    assignment=explicit_setting(directives)
    source='director'
    if assignment:
        target,anchor=assignment
    else:
        if not isinstance(plan,dict) or plan.get('scene_changed') is not True:
            return False
        target=plan.get('new_setting')
        anchor=str(plan.get('scene_evidence') or '').strip()
        if not isinstance(target,str) or not 0<len(target.strip())<=1200 or len(anchor)<6:
            return _skip('missing_setting_or_evidence')
        sources=[str(x) for x in (*directives,*actions)] + [narration]
        if not any(anchor.casefold() in text.casefold() for text in sources):
            return _skip('evidence_not_in_current_action_or_narration')
        if not _asserted_evidence(anchor):
            return _skip('only_hypothetical_or_pending_transition')
        source='auto'
    target=target.strip()
    previous=room.get('setting','')
    if target==previous:
        return False
    event={'setting':target,'previous_setting':previous,'source':source,'evidence':anchor}
    room['setting']=target
    state=room.setdefault('narrative_state',{})
    state['scene_context']=event
    state['location']=target
    state['current_scene']=target
    room.setdefault('history',[]).append({'sender':'System','is_player':False,
        'scene_event':event,'text':'[CHUYỂN CẢNH]: '+target})
    # Principal remains the same identity and active state. Only its physical
    # scope follows the confirmed room, rather than lingering at the opening.
    for npc in room.get('roster',{}).values():
        if npc.get('is_active') and not npc.get('supporting'):
            npc['scene_scope']=target
        npc.pop('presence_lock_scene',None)
    return True
