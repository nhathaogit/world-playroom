# Nghiệm thu blueprint v1.1 khi tích hợp một room

Đây là test plan cho room/runtime đích, không phải danh sách đã pass. Dùng save
bản sao, chặn provider thật trong offline tests. Các fixtures ở examples là dữ
liệu trung lập cho test adapter, không import vào room đang chơi.

## Mức bằng chứng

1. **Tĩnh:** JSON/link/schema/prompt không mâu thuẫn. Không chứng minh backend chạy.
2. **Unit:** identity, privacy, evidence, budget và thứ tự events bằng code.
3. **HTTP:** production loader + policy + POST turn + save tạm + GET state, mock LLM.
4. **UI:** kiểm banner, công tắc và thông báo sau POST/reload, không chỉ response JSON.
5. **Live có phép:** cùng provider/model/payload; đo chi phí/độ trễ và đọc chất lượng
   diễn xuất. Mock không chứng minh model tự nhiên hay không OOC.

## Các ca bắt buộc

| ID | Tình huống tái hiện | Kết quả phải quan sát |
|---|---|---|
| MEM-01 | NPC vừa chứng kiến việc rồi Player hỏi tiếp | Câu quan sát còn trong **messages sau pack**, không chỉ raw context |
| MEM-02 | Chuyện cũ đã nằm ngoài summary/history ngắn | Recall có trích đoạn/source; không gửi toàn archive hoặc thêm AI mỗi NPC |
| MEM-03 | Quote archive nhắc người từng tới | Không tự bật họ ở cảnh hiện tại |
| MEM-04 | Summary/fact mới phủ nhận sai nguồn trước | Không ghi đè ledger; giữ nguồn, phân biệt claim và fact |
| MEM-05 | Archive có thought/command và mixed chỉ có phần kín | Không lọt nội dung kín qua fallback raw text |
| BUD-01 | Required projection 27 KB, overhead 7 KB, ceiling 42 KB | Không bị trần nội bộ 25 KB chặn; số là byte minh họa, đo payload thật |
| BUD-02 | World/facts lớn nhưng lượt gần nhỏ | Bớt lore tùy chọn trước, giữ đối đáp gần và luật nhân vật bắt buộc |
| BUD-03 | Dữ kiện bắt buộc thực sự vượt ceiling | Dừng trước mạng, báo thành phần/byte; không cắt câu hoặc tự tăng vô hạn |
| BUD-04 | Draft repair quá lớn | Giới hạn repair cùng ceiling; có thể bỏ draft, giữ lỗi + context cần thiết |
| SCN-01 | [Fixture chuyển cảnh](examples/scene-transition.json) | Setting, scope, flags, active IDs, save và System events cùng kết quả |
| SCN-02 | Lệnh chuyển cảnh nhưng writer chỉ kể, thiếu metadata | Repair giới hạn hoặc báo rõ, không thành công giả với banner cũ |
| SCN-03 | “Muốn tới”, lời nói, thought hay mention địa điểm | Không tự chuyển nơi Player, không tắt cast cũ |
| SCN-04 | Cast cũ có người active nhưng chưa có scope | Không mặc nhiên đi theo mọi cảnh mới; người theo cần xác nhận |
| SCN-05 | Người đi cùng không nói, người khác ở lại | Người im lặng vẫn ON đúng cảnh; người ở lại OFF, giữ ID/ký ức |
| SCN-06 | Người cũ chào rồi chuyển cảnh | Được chào trước leave; trạng thái cuối không giữ họ ON |
| SCN-07 | [Fixture departure](examples/narrated-departure.json) | OFF sau commit; recovery lượt sau không replay arrival cũ |
| SCN-08 | Tới lại sau departure có căn cứ mới | ON bằng cùng ID, không tạo bản sao và không cấm quay lại vĩnh viễn |
| SCN-09 | Manual OFF trong cùng cảnh | Model không phá lock; chỉ hết hạn khi chuyển cảnh thật theo policy |
| SCN-10 | Import người canon có sẵn rồi thoại đầu | Resolve/import → presence → validation thoại; không tạo NPC giả cùng tên |
| SCN-11 | Tạo NPC phụ, rồi tracker cho nghỉ vai | Mỗi flag delta có notification; không mất hồ sơ hoặc lặp add |
| SCN-12 | Cảnh hỏng sau khi tạm đổi setting/flags | Save và GET state giữ snapshot cũ, không có thông báo ma |
| SCN-13 | Tracker timeout hoặc lỗi giữa lúc áp delta | Không giữ nửa delta, không sinh lại cảnh hợp lệ; pending được công bố |
| UI-01 | Cảnh mới đã commit | Banner/header, switches, System events cùng revision/kết quả |
| UI-02 | Reload sau tới/rời | State và thông báo vẫn đúng, không phụ thuộc toast chưa lưu |
| UI-03 | POST thất bại | Không hiển thị trạng thái tạm như đã commit; giữ tin Player |
| DIA-01 | Đối thoại bình thường | Có thể kết bằng câu trần thuật/cử chỉ, không câu hỏi giao lượt bắt buộc |
| DIA-02 | Nhân vật hỏi thăm vết thương thật | Câu hỏi tự nhiên được giữ, không xóa dấu hỏi bằng regex |
| DIA-03 | History có nhiều câu kết “chọn đi” | Core không yêu cầu bắt chước; live review kiểm xem có bớt lặp không |
| DIA-04 | Player bỏ qua gợi ý, đã hết cooldown | Không tự nhắc theo lịch; cần đúng mạch/ích lợi mới |
| ISO-01 | Hai room hoặc hai profile | Save/recall/cache/policy không lẫn, không đem luật canon truyện này sang truyện kia |
| ISO-02 | Companion một người | Không tự bật ensemble/NPC chính khác bởi cơ chế chung |
| EVT-01 | Arc chỉ có hook/set_pieces/completion, không có source Markdown | Nạp cả cơ hội phát triển và tiêu chí khép tuyến; không chỉ câu mở đầu |
| EVT-02 | Chỉ mới xong hook | Không thêm completed_arcs hoặc tự mở finale |
| EVT-03 | Đối đáp thực tế đạt completion goal, bỏ qua vài cảnh gợi ý | Lưu goal completed bằng evidence, cập nhật completed_arcs và giữ sau reload |
| EVT-04 | Tuyến song song, Player muốn gặp người khác | Đề xuất nhịp ready thuộc tuyến đã mở; không bắt làm xong tuyến hiện tại |
| EVT-05 | Goal có selectable=false hoặc arc chưa mở | Không request một kết quả định sẵn hoặc vượt gate |
| EVT-06 | Save cũ không có sổ event nhưng có facts/outcomes | Recall đúng nhịp, trích evidence; không suy từ số lượt hoặc diễn lại opening |
| EVT-07 | Sandbox có ledger trận đang diễn/trận đã xong, thiếu vài thứ hạng | Dùng tiến độ thật, không reset trận/vòng loại, không lấp hạng từ canon |
| EVT-08 | Request gần trần, retrieval chứa nhiều quote/newline | Đo wire payload; không báo vượt giả do ước lượng escaping cũ |
| EVT-09 | Companion dùng cùng engine | Không cấp kế hoạch sự kiện tương lai hoặc arc_completion |
| EVT-10 | Chỉ thị tạo một cuộc gặp cụ thể, bản đồ sự kiện tương lai dài | Thu gọn roadmap xa trước khi bỏ đối đáp gần; giữ directive, người được yêu cầu, đại arc hiện tại và story_event đã chọn |

## Cách ghi kết quả

Mỗi lần nghiệm thu ghi: test ID, runtime/profile/version, fixture/command chính
xác, input bytes sau pack, số request theo purpose, kết quả và artifact kiểm.
Không ghi “pass” cho UI nếu mới đọc JSON; không ghi “model tốt” nếu provider là mock.
Test schema/fixture hỏng thì sửa template hoặc adapter ở đúng tầng, không sửa
save thật cho vừa test hoặc nới guard để che dữ liệu bất nhất.

Các test tham khảo của engine nằm ngoài bộ này: test_canon_memory.py,
test_canon_runtime_pipeline.py, test_scene_budget.py, test_scene_presence.py và
test_narrated_presence.py. Chúng kiểm baseline cụ thể, không thay test room mới.
