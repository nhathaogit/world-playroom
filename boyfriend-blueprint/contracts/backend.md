# Hợp đồng backend companion — một người chính

## Phạm vi và version

Blueprint version `boyfriend-blueprint/1.0`; runtime hiện vẫn là engine chung.
Đây không phải một giá trị `runtime_profile` được loader công nhận. Không ghi
profile này vào pack rồi kỳ vọng tự thay prompt. Girlfriend có thể tái dùng các
hợp đồng trung lập nhưng cần đổi category/nội dung và kiểm thử riêng.

## Một nguồn cho mỗi loại sự thật

- Manifest: category, ID, ảnh và giới thiệu chợ.
- story_bible: profile, quá khứ, điều NPC biết, giới hạn, opening, metric/title.
- room_state: persona, history, tiến trình và tương tác đã xảy ra; không là bản
  sao toàn bộ kế hoạch. `session_started=false` và history rỗng cho save mới.
- memory: checkpoint/archive/facts có nguồn. Canon quá khứ không tự là tri thức
  mọi người. Lời nói của NPC có thể sai; ghi claim thay vì vô tình sửa canon.
- Cập nhật canon cần đối chiếu các vai trò/bản sao profile, nhưng giữ nguyên
  history và lựa chọn Player; không sửa save đang chạy để đồng bộ nhãn hiển thị.

## API hiện có phải kiểm theo production loader

| Endpoint | Hợp đồng |
|---|---|
| GET /api/playrooms (catalog) | Category boyfriend dẫn tới trang hồ sơ, không thẳng vào save đã chơi của người khác |
| GET /api/story | Profile, background_before_chat, starting_situation để UI đọc |
| POST /api/companion/start | Lưu persona; chỉ chèn opening lần đầu; gọi lại không reset history |
| POST /api/persona | Sửa persona không sinh lượt hoặc reset điểm |
| POST /api/turn | Mixed input → scene đã validate → delta; trả lỗi có thể hiểu nếu chưa start |
| GET /api/state | Trạng thái/ảnh/token meter dùng cho render; không tự tạo kết quả quan hệ |
| GET /api/token-usage | Usage provider theo purpose; token thiếu phải unknown, không giả zero |
| POST /api/memory/save | Snapshot local thủ công trước khi đóng phiên |

Tên route catalog kiểm lại khi triển khai; phần lớn endpoint dùng room middleware
để chọn `playrooms/<id>`, không tự tạo một server boyfriend riêng.

## Input scene: dùng shape engine đang có

`mode=companion`, player, setting, directives, history public, characters,
speaker_manifest, narrator_id, memory, background_before_chat. Thêm recall có
nguồn chỉ sau adapter đã triển khai; tên trường không tự tạo khả năng.

Không truyền toàn profile card, mọi summary, mọi arc và bản sao roster vào nhiều
vùng cùng lúc. Xem serialized messages cuối cùng trong test, không chỉ context
trước khi pack. Input quá lớn phải báo components/bytes trước network.

## Output scene

```json
{
  "responses": [
    {"speaker_id": "principal-id", "segments": [
      {"type": "narration", "text": "Anh đặt cốc xuống."},
      {"type": "dialogue", "text": "Tôi nhớ chuyện ấy."}
    ], "suggestions": []}
  ],
  "scene_changed": false,
  "new_setting": "",
  "scene_evidence": "",
  "presence_updates": [],
  "npc_introductions": []
}
```

Cho phép lặp principal-id nhiều block, narrator chỉ có narration. Không có text,
dialogue, narration bản sao cạnh segments. Identity allowlist phải ở code khi
tích hợp. Một người có nhiều khối không cần gọi AI từng khối. Không thêm NPC phụ
trong companion; nhắc một người không tự chứng minh họ hiện diện hoặc biết chuyện.

`scene_changed` cùng bằng chứng phải được adapter áp dụng nếu muốn chuyển banner;
runtime companion hiện bỏ qua metadata đó. Không trả thành công UI khi chỉ prose
chuyển mà state cũ. Không biến đi xa thành teleport: `[[Sáng hôm sau]]` có authority
thời gian, một câu “mai gặp nhé” chỉ là lời hứa. Việc xa nhau không được tự giả
là đang mặt đối mặt; kênh điện thoại/tin nhắn là yêu cầu adapter kế tiếp.

## Capability matrix — thực trạng khi soạn

| Khả năng | Trạng thái trong engine | Bằng chứng cần trước publish |
|---|---|---|
| Persona tự do, start một lần | Có endpoint | HTTP + reload trên save tạm |
| Một scene + tracker, repair theo lỗi | Có luồng dùng chung | purpose/request count/rollback |
| Lọc thought trong mixed/history | Có parser/projection | sentinel không lọt payload writer/tracker/checkpoint |
| Score nhiều metric, title | Có normalize/tính ngưỡng | metric đúng vai, reason có đối đáp |
| Snapshot local/autosave/checkpoint | Có dùng chung | restart, archive và checkpoint cursor |
| Nội dung archive có nguồn quay lại writer | Chưa tự gắn vào companion | MEM-02 thực thi adapter |
| Cảnh đổi banner/save từ metadata | Companion đang skip presence commit | SCN-01 phải fail trước khi tích hợp |
| Một speaker được code allowlist | Prompt/roster một người chưa là bảo đảm toàn bộ | SPK-01 gửi narrator, principal, outsider |
| Không tương lai có sẵn | load_events companion trả rỗng | Không event/arc/finale trong payload |
| Turn idempotent sau timeout/mất mạng | Chưa coi là hoàn thiện | Không lưu trùng, phân biệt pending/committed |
| OOC/nhịp thoại/logic tâm lý | Không thể bảo đảm bằng schema | Đọc hội thoại dài và live có phép |

## Transaction và lỗi

Từ chối 400/402 không được retry như lỗi văn học. Timeout có thể đã tính tiền;
không retry vô hạn hoặc đổi provider ngầm. JSON mode cần từ `JSON` trong input,
schema/parameters phải theo adapter model đã chọn. Một repair có giới hạn, không
nhân thành guard × số câu. Usage vẫn ghi cả lượt lỗi, không rollback chi phí.

Cảnh lỗi trước commit không được lưu Player một mình. Tracker lỗi không được
bịa score để lấp. Muốn cảnh đã lưu vẫn hiển thị khi tracker lỗi phải có hợp đồng
commit/retry riêng; không giả gọi tóm tắt là đã bảo đảm transaction bền vững.

Shutdown hook chỉ là bổ sung: force-kill, sleep, hibernate hoặc mất điện không
đảm bảo chạy hook. Autosave/snapshot sau lượt mới là nền; không tuyên bố đóng
trình duyệt bằng dừng localhost. Mở LAN phải xử lý auth và key an toàn riêng.
