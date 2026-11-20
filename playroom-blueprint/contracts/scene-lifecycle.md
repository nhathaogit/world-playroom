# Hợp đồng cảnh, hiện diện và giao diện — v1.1

Đây là yêu cầu triển khai/kiểm thử cho room sau, không phải cờ cấu hình tự kích
hoạt. Không chép toàn tài liệu vào prompt. Phần SYSTEM của scene-writer diễn đạt
nghĩa của trường; backend chịu trách nhiệm thứ tự, kiểm tra và commit.

## 1. Phân công một nguồn cho mỗi trách nhiệm

| Trách nhiệm | Chủ thể |
|---|---|
| Diễn cảnh, đề xuất bối cảnh và ai tới/rời | Scene writer, trong cùng request cảnh |
| Cho phép chuyển cảnh, ID, manual lock, áp scope/flags | Backend |
| Facts, quan hệ, kết quả bền vững | Tracker đề nghị delta; backend kiểm và áp |
| Thông báo System, active IDs và setting trả UI | Backend từ thay đổi đã commit |
| Hiển thị banner, công tắc, thông báo | Frontend từ state/API, không parse văn kể |
| Canon/AU, luật triệu hồi, quyền hạn theo văn hóa | Content pack và domain adapter |

Không thêm request điều phối presence mặc định sau mỗi NPC. Hai request là
đường thành công thường: scene + tracker; repair/checkpoint vẫn có thể phát sinh.
Không áp luật triệu hồi/giải đấu của một truyện vào engine hoặc prompt chung.

## 2. Envelope đầu ra của scene writer

Các trường ổn định: `responses`, `scene_changed`, `new_setting`, `scene_evidence`,
`presence_updates`, `npc_introductions`. Response có `speaker_id` và `segments`
gồm narration/dialogue. Không nhân bản nội dung vào trường text cho model viết.

Một presence update có `npc_id`, `action` enter/leave, `reason`, `evidence`.
Evidence phải trích nguyên văn từ hành động công khai/chỉ thị hợp lệ hoặc lời
dẫn **của chính draft này** xác lập việc tới/rời; chỉ có tên không đủ về ngữ nghĩa.
Đây là grounding, không phải bằng chứng code hiểu được mọi câu văn tiếng Việt.
Lời thoại, lời đồn, tương lai, ý định, thought hoặc archive không tự bật nhân vật.

Chuyển nơi/thời gian của Player phải có quyền từ hành động hoặc chỉ thị công
khai. `scene_evidence` đối chiếu đúng nguồn quyền đó; không dùng narration do AI
tự viết để tự cấp quyền di chuyển Player. Kể nơi khác hoặc nói “muốn tới” chưa
phải di chuyển. Với lệnh tiến cảnh rõ ràng, không trả thành công chỉ vì có câu
“đã tới” nhưng bỏ metadata. Repair giới hạn một lần nếu thiếu metadata; nếu
không thể chuyển hợp lệ thì thông báo rõ thay vì giả vờ cập nhật hay teleport.

`new_setting` là mô tả hiện tại cho UI, không phải opening của pack. Kiến trúc
đích tách `scene_id`, `location_id`, `scene_revision` khỏi mô tả; backend hiện
còn so scope bằng chuỗi. Không được gọi thay câu mô tả thời tiết là đã triển khai
ID cảnh ổn định hoặc dùng nó để tùy tiện hết hạn manual lock.

## 3. Trình tự transaction bắt buộc

1. Chụp snapshot/revision và vị trí history trước lượt; giữ input để trả lại khi lỗi.
2. Đóng gói context bằng **một** ngân sách request cuối: core, JSON escaping,
   framing, repair, dự phòng. Bảo vệ Player mới và đối đáp trước; chọn lore/recall
   hữu hạn. Không kiểm 25 KB nội bộ trước một request cuối hợp lệ 42 KB.
3. Scene writer trả envelope; kiểm schema và nguồn quyền chuyển cảnh.
4. Resolve người đã có bằng ID/alias/catalog, import hồ sơ nếu cần; không tạo bản
   giả cùng tên. NPC phụ thật sự mới cần tên/role/personality/tone/scene_function
   và evidence. **Đăng ký danh tính trước thoại đầu**, rồi mới kiểm hiện diện.
5. Áp setting/scope và arrivals vào trạng thái tạm; nếu narration xác nhận người
   đi cùng thì ghi cả người không nói. Người bật ở cảnh cũ không mặc nhiên có mặt
   ở cảnh mới. Dữ kiện narration mới phải dùng được, không chỉ lời Player đầu vào.
6. Kiểm responses với roster tạm; người đã có mặt được chào trước lúc rời. Không
   cho người vắng tự nói chỉ vì có profile. Scope cũ, locks và người chết vẫn là
   điều kiện cần kiểm; domain adapter xử lý các ngoại lệ riêng có căn cứ.
7. Sau validation, áp departures và đóng membership cũ chưa được xác nhận ở
   cảnh mới. Leave không xóa identity/knowledge, không đồng nghĩa chết hoặc hủy
   khế ước. Không đóng cảnh cũ nếu chuyển cảnh bị từ chối hoặc chỉ nhắc nơi khác.
8. Không chạy lại presence từng block sau khi đã xử lý trọn cảnh. Lưu departure
   sau lời chào/lời dẫn; recovery lượt sau không được dùng arrival cũ đảo leave
   mới hơn. Có arrival mới thật về sau thì được vào lại bằng cùng ID.
9. Tracker chỉ nhận lượt mới, setting/present_ids đã commit trong giao dịch và
   ledger cần thiết. Nếu tracker lỗi: rollback delta dở dang, giữ cảnh hợp lệ và
   đánh dấu pending theo khả năng runtime; không giả vờ đã hoàn tất tracker.
10. Đối chiếu flags cuối với đầu lượt và event đã ghi; bổ sung thông báo còn thiếu
    cho import/intro/lifecycle. Không nhân đôi thông báo cùng (npc_id, action).
    Một người thật sự tới rồi đi có thể có hai action khác nhau, không gộp sai.
11. Commit save/history/notifications nhất quán, rồi trả API. Nếu cảnh không hợp
    lệ sau repair: rollback cả setting, scope, flags và thông báo tạm; usage đã
    tiêu không rollback. Checkpoint phải giữ nguồn và không làm mất sự kiện mới.

Đây là invariants/acceptance cho implementation. Thứ tự helper trong code có
thể khác nếu vẫn giữ các invariants; không dùng checklist như bằng chứng đã chạy.

## 4. Hợp đồng backend–frontend

Kết quả lượt thành công cần có setting hiện tại, active_character_ids,
presence_events, responses và trạng thái tracker khi được hỗ trợ. Full state
giữ roster với is_active/scope và history có System entries.

- Scene event lưu setting mới, setting cũ, source và evidence theo schema version.
- Presence event lưu npc_id, name, action, source, reason. Backend sinh thông
  báo từ event, không yêu cầu narrator nói “đã bật nhân vật”.
- Frontend cập nhật banner/header và công tắc từ state đã commit; render System
  entries riêng với ô thoại. Reload không làm mất thông báo đã lưu.
- Có thể fetch `/api/state` sau POST thành công như UI hiện tại. Nếu chỉ dùng
  delta, phải đối chiếu revision và cập nhật tất cả thành phần trên, không chỉ chat.
- Khi POST lỗi, không hiển thị chuyển cảnh/công tắc provisional như đã lưu.
  Giữ hoặc trả lại tin Player để sửa/gửi lại, không tự retry tính tiền.
- Không dùng trường tracker `location`/`current_scene` làm bằng chứng banner
  đã đổi; hai biểu diễn phải được đối chiếu với setting/scene context có nguồn.

## 5. Manual lock và companion

Manual lock giữ trong cùng cảnh; hết hạn theo chuyển cảnh thật được backend
chấp nhận, không theo mention hay một câu nghĩ thầm. Không tự bật nhân vật bị
khóa trong cảnh chỉ vì model muốn họ đáp. Chuyển scene phải phân biệt người đi
cùng, người ở lại và người đang tới, không reset cả roster thành active.

Companion một nhân vật không bị đổi thành room đông NPC hoặc tự hoán đổi người
chính bởi cơ chế presence chung. Nếu pack muốn mở rộng cast phải có cấu hình
và hợp đồng riêng, không ngầm áp quy tắc playroom vào boyfriend/girlfriend.

## 6. Capability gate và tình trạng thực tế

| Khả năng | Baseline đã thấy trong engine | Yêu cầu tích hợp room sau |
|---|---|---|
| Banner/state/notifications qua shared UI | Có đường đi chung | Test đúng POST → GET → render |
| Một scene request và tracker delta | Có trong scene mode | Không bật legacy per-NPC nhầm |
| Recall archive và bảo vệ đối đáp gần | Profile canon đã dùng | Tích hợp độc lập khỏi luật truyện |
| Evidence narration, catalog import, đóng membership cũ | Đã kiểm ở Fairy Tail | Không suy mọi profile có sẵn |
| Không replay presence; thông báo intro/lifecycle | Đã kiểm ở Fairy Tail | Test trên profile đích |
| Giữ cảnh khi tracker lỗi, pending | Profile canon có, chưa có worker hoàn tất | Công bố giới hạn; test rollback |
| Scene/location IDs, revision, idempotent turn jobs | Thiết kế đích chưa hoàn thiện | Không báo đã triển khai |

Không đặt runtime_profile của Fairy Tail cho room mới để lách capability gate.
Trích phần orchestration dùng lại qua adapter/capability rõ ràng khi triển khai
thật; giữ luật thế giới và domain invariants trong pack. Blueprint không tự sửa
save cũ, suy lại người đi cùng hoặc chuyển máy chủ sang một engine mới.
