# ToolNet Publisher — Tài liệu bàn giao

## Giới thiệu

ToolNet Publisher là nền tảng tự động hoá đăng bài Facebook theo dõu pipeline, thiết kế cho khách hàng thương mại.

Trang chủ: https://nevertheless-quantum-boulder-emphasis.trycloudflare.com

---

## Tính năng đã hỗ trợ

| Tính năng | Trạng thái |
|-----------|-----------|
| Quản lý Pipeline (Workspace) | Hoạt động |
| Xác thực CTV (username/password/Pipeline Key) | Hoạt động |
| Quản lý tài khoản Facebook (login/cookie) | Hoạt động |
| Thêm và quản lý nhóm Facebook | Hoạt động |
| Đăng bài group (ảnh ẩn danh/biệt danh) | Hoạt động |
| Lên lịch đăng group tự động | Hoạt động |
| Tự động bình luận group/newsfeed | Hoạt động |
| Album ảnh (upload, quản lý) | Hoạt động |
| Reels (lên lịch) | Hoạt động |
| Video Pre-Check (AWS Rekognition + OCR) | Hoạt động |
| AI Caption (kiểm tra ngôn ngữ) | Hoạt động |
| Safety Center (xem xét, yêu cầu phê duyệt) | Hoạt động |
| Lịch đăng & hàng đợi | Hoạt động |
| Lịch sử bài đăng | Hoạt động |
| Phân tích & báo cáo KPI | Hoạt động |
| Quản lý CTV & phân quyền | Dành cho Admin |
| Pipeline Access Key | Dành cho Admin |

---

## Cách sử dụng

### Đăng nhập
- Nhập **username**, **mật khẩu**, và **Pipeline Access Key** (chỉ cho CTV).
- Admin đăng nhập bằng username và mật khẩu.

### Dashboard Admin
- Xem danh sách tất cả Pipeline.
- Bấm vào thẻ Pipeline để mở Workspace.
- Bấm "Tạo pipeline" để tạo Workspace mới.

### CTV Workspace
- Mở trang Tổng quan để xem số liệu nhanh.
- Kết nối Facebook trước khi dùng bất kỳ tính năng nào.
- Thêm group và kiểm tra quyền đăng.
- Tải ảnh lên Album để chuẩn bị nội dung.
- Cấu hình tự động đăng group (chọn Facebook, group, lịch).

---

## Trạng thái nghiệp vụ

| Trạng thái | Nghĩa |
|-----------|-------|
| Chờ đăng | Đã lên lịch, đang chờ xuất lên Facebook |
| Chờ duyệt | Cần Admin phê duyệt trước khi đăng |
| Đã gửi, chưa xác minh | Đã gửi nhưng chưa xác nhận Facebook đã đăng thực sự |
| Đã đăng | Đã đăng thành công (cần xác minh thủ công trên Facebook) |
| Thất bại | Không đăng được |
| Cần xem lại | Nội dung cần kiểm tra lại |
| Chờ xác minh | Group chưa xác nhận quyền đăng |
| Đã khóa | Tài khoản bị khóa |
| Đang xử lý | Hệ thống đang thực hiện |

---

## Giới hạn và lưu ý quan trọng

1. **Trạng thái không phải là bằng chứng đăng thành công**: Trạng thái "Đã đăng" chỉ là kết quả từ lịch sử đăng. Luôn kiểm tra trực tiếp trên Facebook.

2. **Bài gửi không xác minh**: Bài ở trạng thái "Đã gửi, chưa xác minh" **không được coi là đã đăng thành công**.

3. **Video Pre-Check**: Kết quả kiểm tra là gợi ý an toàn nội dung, không bảo đảm Facebook sẽ duyệt.

4. **AI Caption**: Đánh giá của công cụ không bảo đảm Facebook sẽ chấp nhận.

5. **Thời gian thực**: Một số trạng thái cập nhật theo thời gian thực từ worker nền.

---

## Trách nhiệm Admin

- Tạo và quản lý Pipeline.
- Tạo tài khoản CTV và cấp Pipeline Key.
- Cấu hình chính sách đăng (giới hạn, thời gian).
- Theo dõi báo cáo an toàn và KPI.
- Hỗ trợ CTV khi có lỗi.

---

## Hỗ trợ kỹ thuật

Liên hệ đội ngũ phát triển để được hỗ trợ về:
- Lỗi kết nối Facebook
- Lỗi kiểm tra video
- Vấn đề lịch đăng
- Cấu hình hệ thống

---

## Cập nhật

- Phiên bản hiện tại phản ánh trạng thái thực tế của hệ thống.
- Các tính năng đang phát triển sẽ được thông báo qua giao diện.
- Không sử dụng các tính năng chưa chính thức ra mắt.
