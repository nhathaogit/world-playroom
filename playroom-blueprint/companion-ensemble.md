# Diễn đa nhân vật theo kinh nghiệm companion runtime

Áp dụng 05/11/2026 cho Mực Son Qua Hiên và Xuân Ý Kinh Hoa tại source local.
Opt-in: story_bible.scene_style = companion_inspired_ensemble_v1.

- Một writer viết cả cảnh, một tracker ghi delta; không thêm request AI mặc định.
- Thay prompt writer cũ, không nối chồng những chỉ dẫn độ dài trái nhau.
- Độ dài theo đối đáp; 6144 là trần output, không độ dài tối thiểu hoặc quota từng NPC.
- Giữ vai, giọng, sáng kiến nhỏ và quyền tự quyết Player; không công thức A/B, không kéo dài bằng lặp.
- Chỉ bỏ thông tin presentation/candidate giống hệt hồ sơ đã gửi; không rút mất luật riêng, nguồn ký ức, đoạn vừa nói hoặc sự kiện đang chọn.
- Tiếp tục dùng lifecycle, metadata chuyển cảnh, registry NPC, khóa thủ công theo cảnh, privacy projection, event retrieval và tracker của playroom.
- Không chuyển playroom thành companion: giữ nhiều tuyến, dàn nhân vật và hệ thống thế giới riêng; không sao ABO, principal hay luật không arc.
- Prompt Mực Son giữ phần chỉ đạo diễn xuất gia đấu riêng. Mọi thay đổi này không ghi lại save/history.

Runtime: ensemble_runtime.py; kiểm thử: test_ensemble_runtime.py và các bộ integration hiện diện/tri thức/tiến độ arc.
