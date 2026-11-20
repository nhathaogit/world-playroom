# Tạo boyfriend bằng giao diện

Vào chợ **Boyfriend → ＋ Tạo boyfriend**, hoặc mở `/boyfriend/new`.
Không cần JSON, API key hoặc gọi model để tạo.

1. Điền tên, tuổi trưởng thành, sơ yếu, ảnh và mô tả hiển thị trên chợ.
2. Viết cá tính, giọng nói, mong muốn; mở phần nâng cao nếu cần khóa tri thức,
   quyền hạn, tính chủ động, điều kiện thay đổi hoặc hành vi OOC cần tránh.
3. Chỉ viết quá khứ và tình huống gặp mặt. Thêm từng đoạn dẫn/thoại cho opening;
   không viết thay hành động hoặc cảm xúc của Player, không định trước tương lai.
4. Chọn loại quan hệ, đặt 1–8 thuộc tính và điểm đầu 0–100, chọn thuộc tính xét
   title, sửa các mốc cùng lời giải thích. Mốc đầu 0, tăng dần, không trùng.
5. Xem trước rồi **Tạo & đưa lên chợ**. Mở hồ sơ mới để tự điền persona và bắt đầu.

Ảnh PNG/JPEG/WebP tối đa 2 MB và 16 triệu pixel; backend giải mã, bỏ metadata,
thu về tối đa 768 × 1024 và lưu PNG. Không tải URL ngoài hoặc tự sinh chân dung.
Để trống dùng silhouette chung; có nút bỏ ảnh. Nội dung chữ có bản nháp trong
localStorage của trình duyệt hiện tại, không có ảnh; tạo thành công xóa bản nháp.
Người khác dùng cùng browser profile có thể đọc nháp. Tạo trên điện thoại vẫn
lưu nhân vật ở máy chạy server, không ở điện thoại.

## Hợp đồng thực thi

- `GET /boyfriend/new`: trang form, CSS/JS riêng, nhãn và control native, không
  thêm framework hoặc gọi AI để điền tự động.
- `POST /api/companions`: JSON form có `X-Companion-Editor: 1`, kiểm Origin cùng
  website nếu có. Giới hạn body 3.1 MB; phần hồ sơ không gồm ảnh tối đa 28 KB.
- Backend không nhận raw bible/save, đường dẫn ghi hoặc category tùy ý. Chỉ tạo
  `boyfriend` mới, `session_mode=companion`, một speaker, không arc/finale.
- `companion_creator.py` xây pack từ luật chung của template rồi dùng chính
  `checks/validate_blueprint.py` kiểm. Các SYSTEM core trong `prompts/` vẫn không
  tự nạp vào engine. Ngữ nghĩa của văn người viết vẫn cần tự review/chat thử;
  lint không chứng minh không OOC, không thay thế kiểm duyệt nội dung.
- Mã trùng trả 409, không cập nhật hoặc ghi đè. Tạo trong staging rồi rename;
  chỉ ba JSON hoàn chỉnh mới xuất hiện trên chợ. Upload mang tên ngẫu nhiên,
  lỗi trước publish thì bỏ riêng ảnh mới. Process crash có thể để lại ảnh mồ côi
  hoặc lock staging; không tự quét/xóa dữ liệu cũ khi server khởi động.
- Không bind room context, không đổi `ROOM_STATE` đang chơi; save mới có history
  rỗng, session chưa bắt đầu, persona rỗng, điểm đầu do người viết nhập.
- Dùng cho máy local/LAN tin cậy như app hiện tại. Origin check không phải đăng
  nhập: ai truy cập được server vẫn có thể tạo. Không mở server ra Internet để
  dùng như dịch vụ nhiều người khi chưa thêm authentication/authorization/quota.

## Kiểm thử và triển khai

```powershell
python -m pip install -r requirements.txt
python -m unittest test_companion_creator test_boyfriend_blueprint test_boyfriend_profiles test_marketplace -v
node --check static/companion-create.js
```

Cần restart server sau khi nâng cấp code để có API mới; sau đó tạo thêm nhân vật
không cần restart. Pillow là dependency mới để xác minh ảnh thật.

Có thể chạy `python tools/serve_companion_editor_test.py --port 8766` để thử UI
với dữ liệu tạm, không vào chợ thật, không gọi AI. Mở
`http://127.0.0.1:8766/boyfriend/new`; Ctrl+C chỉ đóng helper và dọn dữ liệu thử.
Helper chặn các API ghi khác (bao gồm chat, model/settings) và tắt client AI;
không đổi cấu hình provider của project.
