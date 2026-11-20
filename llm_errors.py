"""Classify provider refusals before JSON parsing, without inspecting room content."""
import re


class ProviderContentBlocked(ValueError):
    code = 'llm_content_blocked'

    def __init__(self, purpose, model, reason):
        self.purpose, self.model, self.reason = purpose, model, reason
        # Stable English diagnostic for callers; no prompt or refusal prose in logs.
        super().__init__(f'{purpose}: AI response blocked/refused (reason={reason})')

    @property
    def public_message(self):
        return ('Provider AI đã chặn phản hồi theo chính sách nội dung '
                f'({self.reason}). Đây không phải lỗi JSON hoặc hết token. '
                'Backend đã dừng, không tự gửi lại hay gọi sửa cảnh. '
                'Lượt chưa được lưu; hãy xem lại yêu cầu/bối cảnh để phù hợp chính sách của provider.')


def field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def blocked_code(value):
    if not isinstance(value, str):
        return None
    tokens = set(re.findall(r'[A-Z][A-Z_]*', value.upper().replace('-', '_')))
    # Handle exact enums AND compatibility strings such as
    # "content_filter: PROHIBITED_CONTENT", including case/spacing variants.
    for code in ('PROHIBITED_CONTENT', 'IMAGE_PROHIBITED_CONTENT', 'CONTENT_FILTER',
                 'SAFETY', 'IMAGE_SAFETY', 'BLOCKLIST', 'SPII', 'RECITATION', 'BLOCKED', 'REFUSAL'):
        if code in tokens:
            return code
    return None


def response_block_reason(completion):
    choices = field(completion, 'choices')
    for choice in choices if isinstance(choices, (list, tuple)) else []:
        for key in ('finish_reason', 'finishReason'):
            if reason := blocked_code(field(choice, key)):
                return reason
        refusal = field(field(choice, 'message'), 'refusal')
        if isinstance(refusal, str) and refusal.strip():
            return 'REFUSAL'
    # Some gateways expose a prompt block without any choices/content.
    for feedback_key in ('prompt_feedback', 'promptFeedback'):
        feedback = field(completion, feedback_key)
        for key in ('block_reason', 'blockReason'):
            value = field(feedback, key)
            if reason := blocked_code(value):
                return reason
            if isinstance(value, str) and value.strip().upper() not in {
                    '', 'NONE', '0', 'BLOCK_REASON_UNSPECIFIED', 'UNSPECIFIED'}:
                return 'PROMPT_BLOCKED'
    return None
