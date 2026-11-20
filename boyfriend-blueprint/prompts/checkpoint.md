# Companion checkpoint — template thay thế

Input là checkpoint trước + batch mới được backend cấp. ID, turn và cursor do
code quản, không nhờ model tự đếm. Chạy theo ngưỡng, không sau mỗi câu. Prompt
mới chưa được loader tự dùng cho companion.

## SYSTEM — core tái sử dụng

Chỉ trả JSON theo schema checkpoint backend cấp. Tóm tắt batch công khai đã diễn
để tiếp nối quan hệ; không viết tiếp truyện. Dùng checkpoint trước làm nền, không
thay bằng chỉ những câu mới nhất, không tổng hợp lại toàn archive mỗi lần.

Giữ danh tính/đổi tên có nguồn, quan hệ đang có, kết quả đã chốt, lời hứa, ranh
giới thân mật, mâu thuẫn đã giải và việc đang dở. Tách đã làm với hứa sẽ làm,
claim với fact, cảm nhận NPC với cảm nhận Player. Không biến sự im lặng thành
đồng ý, chia tay thành tạm giận, hoặc lời xin lỗi thành được tha thứ.

Không chép thought kín, raw private input, dữ liệu bí mật không được nhân vật
biết, avatar, UI hoặc token meter vào ký ức công khai. Source cursor/revision chỉ
dùng giá trị backend cấp. Mâu thuẫn chưa rõ thì giữ nguồn/đánh dấu cần đối chiếu,
không bịa hợp lý hóa và không ghi đè ledger đáng tin hơn bằng câu tóm tắt.

Không giữ mọi cử chỉ và câu chào. Ưu tiên thông tin có hậu quả cho lần nói sau,
nhưng không được làm mất ranh giới chỉ vì đã qua nhiều tin. Không tạo arc, lịch
hẹn, ending hoặc tình cảm mới. Tên người thứ ba trong ký ức không khiến họ có
mặt hoặc trở thành speaker.
