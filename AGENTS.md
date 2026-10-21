AGENTS.md — TOOLNET PUBLISHER ENGINEERING CONSTITUTION

> **Phạm vi:** Toàn bộ source code, cấu hình, database, frontend, backend, worker, script, migration, bảo mật, trải nghiệm người dùng và triển khai của project `/root/facebook-pipeline-manager`.
> 
> **Tính chất:** Quy tắc thường trực, áp dụng ở **mọi phiên làm việc**, không phải một task hay prompt dùng một lần. Mọi AI/coding agent phải đọc tài liệu này **trước khi phân tích, sửa code, chạy script hoặc deploy**.
> 
> **Nguyên tắc ưu tiên:** Giữ đúng nghiệp vụ và cách ly dữ liệu > bảo mật và an toàn > tính toàn vẹn dữ liệu > tương thích > khả năng bảo trì > hiệu năng > tốc độ triển khai. Không được viện lý do hoàn thành nhanh để bỏ qua các quy tắc này. Tuân thủ các chỉ dẫn hệ thống và an toàn có mức ưu tiên cao hơn tài liệu project.

NGUYÊN TẮC HẠT NHÂN — KHÔNG THƯƠNG LƯỢNG

• Đây là quy chế kỹ thuật dài hạn, không phải prompt cho một task. Mọi thay đổi của coding agent phải tuân theo. Không tạo tính năng “chạy tạm” hoặc mô phỏng mà trình bày như production.
• 1 Pipeline = 1 CTV = 1 Workspace. Một Pipeline chỉ có tối đa một CTV đang được gán; Admin quản lý nhiều Pipeline. Mọi tài nguyên nghiệp vụ riêng tư theo pipeline_id.
• Không bao giờ nhồi nhiều tính năng không liên quan vào một file. Duy trì ranh giới module, tầng logic và contract; mở rộng bằng module đúng chỗ thay vì liên tục phình app.py, index.html, app.js, một file CSS hoặc TSX.
• Giao diện phải đủ đẹp để bàn giao khách hàng: ít chữ, ít bước, rõ tác vụ, không icon rác/emoji trang trí, không phô lỗi nội bộ, có loading, trạng thái và hiệu ứng nhẹ.
• Không được mất dữ liệu, phiên Facebook, cookie, quyền truy cập, lịch đăng hoặc cấu hình khách hàng. Có kiểm thử, backup, kiểm soát lỗi và rollback.
• Không được giả lập thành công: kết quả API, metrics, trạng thái POSTED, progress, phân tích video và kiểm tra chính sách phải dựa trên chứng cứ.
• Không phá quy tắc an toàn để tự động hóa bằng mọi giá. Khi điều kiện đăng chưa được xác minh hoặc nền tảng không hỗ trợ, dừng/review thay vì vượt qua kiểm soát.

R01 — PHẠM VI PROJECT VÀ GIỚI HẠN THAO TÁC

1. Thư mục project duy nhất: /root/facebook-pipeline-manager. Không tự chuyển sang /home/ubuntu hoặc repository khác.
2. Chỉ sửa các file liên quan trực tiếp đến yêu cầu; không tiện tay refactor hoặc thay đổi project khác.
3. Trước khi sửa: xác minh working directory, Git status, cấu trúc source, luồng dữ liệu, API hiện có và những thay đổi chưa commit. Không ghi đè công việc đang dở của người khác.
4. Không tự ý xóa/reset database, user, Pipeline, Pipeline Key, cookie, browser profile, Group, Album, lịch sử, queue, job, tệp người dùng, AI profile hoặc dữ liệu phân tích.
5. Không tự ý đổi Cloudflare Tunnel, domain, server, port, AWS Region, AI Provider, credentials, kế hoạch hạ tầng hoặc framework. Việc thay đổi có tác động cần được đánh giá và được người dùng chấp thuận nếu vượt phạm vi task.
6. Bảo vệ .env, secret, Access Key, Facebook cookie, token và session; không đưa các giá trị này vào Git, test fixture, log, ảnh chụp hoặc báo cáo.

R02 — NGHIỆP VỤ BẤT BIẾN: 1 PIPELINE = 1 CTV = 1 WORKSPACE

1. Một Pipeline có đúng một CTV đang được gán; một CTV chỉ thuộc một Pipeline; Workspace chính là Pipeline đó. Không xây dựng mô hình nhiều CTV dùng chung một Pipeline.
2. Super Admin có thể tạo, xem, quản trị nhiều Pipeline; tạo username, mật khẩu và Pipeline Access Key; thu hồi key, khóa CTV, chuyển giao chủ sở hữu bằng quy trình có kiểm soát.
3. CTV đăng nhập bằng username + password + Pipeline Key, sau đó chỉ vào Workspace thuộc Pipeline được cấp.
4. Mỗi Pipeline chứa tài nguyên riêng tư: nhiều Facebook Accounts, Facebook Groups, Album/Media, AI Instructions/Knowledge Base, Auto Group Posting, Auto Comment, Reels, Video Pre-Check, scheduler, jobs, lịch sử và analytics.
5. Mặc định không chia sẻ bất cứ tài nguyên nào giữa các Pipeline. Không hiển thị, tham chiếu, tải xuống hoặc sử dụng cookie, ảnh, AI profile, group hoặc job của Pipeline khác.
6. Cấu hình Auto Posting/Auto Comment là cấu hình của Pipeline. CTV tự thiết lập ngay từ lần đăng nhập đầu; Admin không phải tạo cấu hình thay. Khi chưa có bản ghi, UI phải hiện form mặc định OFF, không phải trang trống.
7. Không tạo tầng owner_ctv_id thứ hai một cách vô nghĩa để cô lập dữ liệu nghiệp vụ: pipeline_id là tenant boundary. created_by_user_id chỉ dành cho audit nếu cần.
8. Enforce quan hệ 1:1 ở backend và bằng ràng buộc DB thích hợp; không chỉ kiểm tra trên giao diện. Nếu dữ liệu cũ mâu thuẫn với 1:1, báo cáo và migration an toàn, không xóa hoặc tự chuyển chủ sở hữu.

R03 — AUTHENTICATION, RBAC VÀ CÁCH LY TENANT

1. Mọi request đọc/ghi phải xác thực danh tính từ session/token phía server và suy ra Pipeline được phép. Không tin pipeline_id, ctv_id, role do frontend tự khai.
2. Mọi repository query, API, file/media access, worker và scheduler phải scope theo pipeline_id được xác thực. Tránh IDOR, cross-tenant reads/writes, rò rỉ cache, nhầm dữ liệu trong background jobs.
3. Super Admin được xem, quản trị các Pipeline theo chức năng quản lý; hành động nhạy cảm cần audit log. CTV không được phép dùng API Admin kể cả khi đoán URL.
4. Tài khoản CTV, mật khẩu chỉ lưu dạng hash Argon2id, Pipeline Key lưu dưới dạng verifier/hash an toàn; key ngẫu nhiên đủ mạnh. Không lưu plaintext vào DB hay log; chỉ hiển thị key gốc một lần khi tạo.
5. Thực thi revoke/disable thật sự ở backend và phiên đăng nhập. Rate limit đăng nhập; cookie web bảo mật (HttpOnly/Secure/SameSite thích hợp); không lưu secret vào localStorage.
6. Facebook cookie/session là secret, phải mã hóa/lưu ở vùng an toàn và tách theo Pipeline; trình duyệt tự động hóa không tái sử dụng profile/cookie giữa các Pipeline.
7. Luôn test hai Pipeline độc lập; cố tình thay URL/ID để xác minh không truy cập chéo.

R04 — KIẾN TRÚC MODULAR MONOLITH VÀ QUẢN LÝ FILE

1. Modular Monolith là kiến trúc mặc định. Không tự tách microservices, đổi framework hoặc di chuyển hàng loạt thư mục vì sở thích. Chỉ thay kiến trúc khi có vấn đề đo đạc được và đã đánh giá chi phí migration.
2. Tổ chức theo feature-first + layer-within-feature. Ranh giới tầng: router/controller nhận request; schema validate dữ liệu; service điều phối nghiệp vụ; repository truy cập DB; policy/guard thực thi quyền & an toàn; worker chạy task dài; scheduler tạo lịch; integration/adapter nói chuyện với bên ngoài.
3. app.py chỉ dành cho bootstrap (app factory, DI, middleware, router registration, lifecycle). Không thêm endpoint nghiệp vụ, SQL, FFmpeg, AWS, AI, Facebook hoặc UI vào đây. main.py cũng theo nguyên tắc tương tự.
4. Frontend chia theo features, dùng component/tokens chung. Không dồn toàn bộ route, screen, CSS, API calls, state và validation vào một index.html, app.js, App.tsx hoặc styles.css.
5. Một file một trách nhiệm rõ ràng. Nếu file chứa nhiều nghiệp vụ, nhiều loại dữ liệu, nhiều side-effect hoặc có những thay đổi không liên quan cùng sửa vào một file, phải tách hợp lý. Không lạm dụng chia một hàm nhỏ thành một file rỗng; độ gắn kết (cohesion) quan trọng hơn số dòng cứng.
6. Dùng giới hạn cảnh báo để audit, không phải quota máy móc: từ khoảng 300 dòng/component hoặc 500 dòng/module cần chủ động đánh giá; không được kéo dài file vượt mức chỉ vì sửa nhanh. Nếu chưa tách, ghi rõ lý do và kế hoạch refactor. Không tách file tự động nếu làm hỏng tính nhất quán.
7. Chỉ import theo hướng rõ ràng: UI → API client → backend route → service → repository/integration; tầng domain/service không phụ thuộc framework UI. Không circular imports hoặc import chéo business code giữa hai feature không qua interface rõ ràng.
8. Logic dùng chung đặt ở core hoặc shared chỉ khi thực sự có ít nhất hai nơi cần dùng; không tạo utils.py, helpers.py, common.js như thùng chứa mọi chức năng. Tên module phải mô tả được trách nhiệm.
9. Tách schema (request/response) khỏi ORM/DB model và khỏi view-model khi cần; tránh trả thẳng bản ghi DB chứa bí mật ra API.
10. Mọi function nhận input rõ, trả output có kiểu, sử dụng Guard Clauses/Early Return, validate dữ liệu tại boundary, exception phân lớp theo nguyên nhân. Logging có ngữ cảnh, không lộ secret.
11. Không copy/paste logic. Không hardcode Pipeline ID, Facebook ID, Region, tên model, credential hoặc port trong business logic; tham số hạ tầng ở config/adapters.
12. Tính năng mới phải được đặt trong module sở hữu nghiệp vụ, bao gồm routing, schema, service/repository, UI, tests và tài liệu. Không thêm if/else ở hàng chục module để xử lý feature mới.

Cấu trúc tham khảo — điều chỉnh theo source thật

/root/facebook-pipeline-manager/
├── AGENTS.md
├── backend/
│   ├── app/                 # bootstrap, dependency injection, router registry
│   ├── core/                # config, auth primitives, exceptions, database
│   ├── modules/
│   │   ├── auth/
│   │   ├── pipelines/
│   │   ├── ctv/
│   │   ├── facebook_accounts/
│   │   ├── groups/
│   │   ├── media/
│   │   ├── ai_profiles/
│   │   ├── auto_posting/
│   │   ├── auto_comment/
│   │   ├── reels/
│   │   ├── video_precheck/
│   │   ├── safety/
│   │   └── analytics/
│   ├── integrations/        # AWS, Turso, Facebook browser, AI providers
│   ├── jobs/                # durable queue, workers, schedulers, locks
│   ├── migrations/
│   └── tests/
├── frontend/
│   └── src/
│       ├── app/             # router, providers, auth/tenant layout
│       ├── features/        # mỗi tính năng có page, hooks, API, types, UI riêng
│       ├── components/ui/   # input, button, modal, toast, skeleton...
│       ├── lib/             # typed client, format, error mapping
│       └── styles/          # tokens, resets, shared primitives
├── scripts/                # vận hành, backup, migrations, smoke tests
└── docs/                   # architecture, API contracts, runbook

• Đây không phải lệnh bắt buộc chuyển thư mục. Nếu source hiện tại ở cấu trúc khác, refactor tăng dần với adapter và regression tests. Không tạo hàng loạt file/folder trống.
• Mỗi module có thể dùng router.py, schemas.py, service.py, repository.py, policies.py chỉ khi chúng có chức năng thật.
• File cấu hình môi trường, migration và asset riêng phải được bố trí đúng lớp; không để .env, DB file, cookie, video upload hoặc cache bị lộ dưới static/public.

R05 — DATABASE, TURSO VÀ MIGRATION

1. Turso là nguồn dữ liệu bền vững ưu tiên cho CTV, password hashes, Pipeline, Pipeline Key, RBAC, AI settings và Auto Posting/Comment settings khi đã được project cấu hình làm nguồn chính. Không để các đối tượng này chỉ tồn tại trong frontend state hoặc SQLite local rồi báo lưu thành công.
2. Audit kỹ dữ liệu hiện tại trước khi quyết định mô hình nguồn sự thật. Nếu SQLite/Turso cùng tồn tại, phải ghi rõ trách nhiệm của từng DB, không âm thầm tạo hai nguồn auth độc lập.
3. Bắt buộc: migration có version, foreign keys, unique constraints, composite indexes theo pipeline_id, transaction, idempotency key và audit fields hợp lý.
4. Mọi thay đổi schema phải backup trước, có kế hoạch rollback, tương thích dữ liệu cũ; tránh destructive migration. Không drop column/table khi chưa xác minh dữ liệu và đường đọc cũ.
5. Chỉ báo lưu thành công sau khi DB COMMIT. Nếu ghi thất bại thì trả lỗi rõ ràng, không cập nhật UI như đã lưu.
6. Tác vụ có thể chạy đồng thời phải dùng transaction/locking/unique keys để ngăn tạo trùng, race condition, vượt quota hoặc đăng chéo account.
7. Dữ liệu file/media lưu trong private local storage hoặc R2 được cấu hình; DB lưu metadata, storage key, ownership, hash, trạng thái và thời gian. Không mở public bucket/folder cho dữ liệu riêng tư.

R06 — FACEBOOK, GROUP, ALBUM VÀ NGUỒN DỮ LIỆU

1. Một Pipeline có thể thêm nhiều Facebook qua phương thức đăng nhập hoặc Cookie JSON mà chức năng hiện có hỗ trợ. Xác minh trạng thái thực, tránh lấy nhầm đoạn text của Facebook làm tên tài khoản.
2. Group được thêm bằng URL, chuẩn hóa ID/link, loại trùng và liên kết đúng tài khoản. Theo dõi trạng thái DIRECT, REVIEW, UNKNOWN, RESTRICTED theo bằng chứng có thật, không suy đoán.
3. Không gửi bài test để dò nhóm; không tự đổi chế độ Ẩn danh/Biệt danh sang tên thật nếu tính năng đó không khả dụng.
4. Album riêng từng Pipeline, CTV tự upload; mỗi bài Auto Group Posting dùng đúng 1 ảnh + 1 caption; chỉ chọn ảnh hợp lệ, đã kiểm tra và phù hợp chủ đề.
5. Media: validate chữ ký file/MIME/kích thước, chống path traversal, hash + perceptual hash, thumbnail, quyền truy cập, dọn tệp tạm; không xóa media đang được job tham chiếu.
6. Mọi dữ liệu lấy từ Facebook đều có thể thiếu, thay đổi hoặc bị hạn chế. Không tự tuyên bố đã đăng, đã duyệt, có reach/views hoặc group được đăng trực tiếp khi không có bằng chứng xác nhận.
7. Chỉ sử dụng những phương thức truy cập/xuất bản được Facebook cho phép; không vượt checkpoint/CAPTCHA, né giới hạn, hoặc tạo tương tác không xác thực.

R07 — AUTO GROUP POSTING VÀ LỊCH TỰ ĐỘNG

1. CTV cấu hình một lần cho toàn Pipeline, áp dụng cho tất cả Facebook được phép hoặc tập con đã chọn; không cấu hình lại cho từng Facebook trừ override được phép.
2. Mỗi Facebook có hạn mức bài/ngày, cooldown, lịch sử, trạng thái, lock riêng; có thể có giới hạn Pipeline và Group cao hơn. Không tạo job vượt giới hạn hiệu lực nhỏ nhất.
3. Scheduler hỗ trợ timezone (mặc định Asia/Ho_Chi_Minh khi chưa được cấu hình khác), ngày hoạt động, khung giờ, giãn cách bài, pause/resume, nhiều account, lịch qua nửa đêm.
4. Không có “số phút chắc chắn không bị spam”. Khoảng cách thời gian chỉ là biện pháp giảm tần suất, không phải kỹ thuật né bộ lọc. Khi Facebook cảnh báo/hạn chế: tạm dừng và yêu cầu xử lý, không đổi IP hoặc xoay account để tiếp tục.
5. Anti-duplicate phải kiểm tra media, caption, nội dung gần giống, cặp account/group, job đã xếp lịch và lịch sử. Idempotent khi retry/restart/nhấn nút nhiều lần.
6. Mọi job phải đi qua Safety Engine, quyền đăng và kiểm tra anonymous/nickname; unknown/restricted group không được tự đăng khi chưa xác minh điều kiện.
7. Phân biệt QUEUED, PENDING_APPROVAL, SUBMITTED_UNVERIFIED, POSTED, FAILED, REMOVED, BLOCKED. Không retry mù khi chưa xác minh bài đã gửi hay chưa.

R08 — AUTO COMMENT: HAI CHẾ ĐỘ, KHÔNG SPAM

1. Hỗ trợ chế độ Group và News Feed trong phạm vi có quyền truy cập và phương thức xuất bản được Meta hỗ trợ. Nếu không có quyền/API phù hợp, chỉ hỗ trợ đọc/xem trước/bản nháp/phê duyệt thủ công thay vì tự động hóa vượt rào cản.
2. CTV cấu hình riêng theo Pipeline: nguồn được phép, chủ đề, giới hạn mỗi Facebook/ngày, khung giờ, cooldown, pause, duyệt bình luận.
3. Một nội dung bình luận sau chuẩn hóa được dùng tối đa 3 lần trên toàn Pipeline, tính cả job đã đăng và đang chờ; lần thứ 4 phải bị chặn. Không dùng biến thể ký tự/từ đồng nghĩa để né anti-duplicate.
4. Không bình luận nhiều lần vào cùng bài, không tạo bình luận không liên quan, tương tác giả, spam quảng cáo, giả vờ đã trải nghiệm sản phẩm hoặc lan truyền thông tin sai.
5. Khi có cảnh báo, checkpoint, từ chối hoặc trạng thái gửi không rõ, dừng/review; không gửi lại hoặc đổi account để vượt hạn chế.

R09 — AI RIÊNG CHO MỖI PIPELINE

1. Mỗi Pipeline có AI Profile riêng: System Prompt, phong cách caption/bình luận, chủ đề, quy định thương hiệu, ví dụ, Knowledge Base và phiên bản cấu hình.
2. Job AI phải nạp đúng pipeline_id; không đưa nội dung/tệp/tài liệu của Pipeline khác vào prompt hoặc cache. Cache AI phải tenant-scoped.
3. Phân biệt lưu instructions/Knowledge Base với training/fine-tuning model thực sự; không tuyên bố đã huấn luyện khi chỉ thay prompt.
4. AI được sửa caption tự nhiên, đúng nghĩa, giảm nguy cơ vi phạm dựa trên sửa đổi thực chất. Không dùng cách né kiểm duyệt bằng ký tự lạ, nói lái hoặc biến thể giả.
5. AI Provider timeout, 403/404, sai model capability phải báo đúng lỗi; không giả kết quả thành công hay bỏ qua kiểm tra bắt buộc.
6. Chính sách an toàn cấp hệ thống không bị ghi đè bởi prompt của CTV.

R10 — VIDEO PRE-CHECK: AWS, OCR, KHÔNG ASR

1. AWS Rekognition Image là Vision Engine chính, dùng DetectModerationLabels và DetectLabels trên keyframes do FFmpeg trích xuất. AWS Region đang được cấu hình là ap-southeast-2; không tự sửa sang Region khác.
2. Giới hạn số frame/request, concurrency, retry backoff, timeout và cost guard. Cache theo video hash + phiên bản xử lý. Không tự bật dịch vụ Rekognition Video/S3 trả phí ngoài phạm vi được phép.
3. OCR tự host nhẹ trên CPU; ưu tiên Tesseract Native CLI với language packs vie+eng theo lựa chọn gần nhất, không thêm PaddleOCR/ASR hoặc OCR cloud khi chưa được yêu cầu. OCR trả text, confidence, timestamp, phân biệt NO_TEXT với CHECK_FAILED.
4. ASR/Speech-to-Text đã bị loại hoàn toàn. Không cài lại, không gọi transcription API, không đưa bước nhận diện lời thoại vào worker/UI. Nếu có kiểm tra audio thì chỉ giới hạn ở thông số/loudness/silence, không tuyên bố đã hiểu lời nói.
5. CTV upload video → FFprobe/FFmpeg → AWS Vision → OCR → caption → Community Standards → báo cáo. Quy trình chạy background worker, frontend có progress thực tế.
6. Các trạng thái PASS, NEEDS_REVIEW, BLOCKED, CHECK_FAILED phải có lý do/evidence/timestamp và checked scope. Không lấy kết quả nhãn AWS rỗng làm bằng chứng video an toàn tuyệt đối.
7. Không gắn copyright PASS chỉ vì không trùng media trong hệ thống: phải dùng UNVERIFIED nếu chưa xác minh quyền sử dụng.
8. Khi kiểm tra bắt buộc lỗi, không được tự xuất bản. Test cuối phải dùng AWS API thật và video thật, không chỉ mock hoặc HTTP 200.

R11 — ANALYTICS VÀ TÍNH ĐÚNG ĐẮN DỮ LIỆU

1. Admin Dashboard theo dõi từng Pipeline/CTV: số bài đã xác nhận đăng, jobs, lỗi, lượt tương tác, lượt xem Reels và chỉ số có quyền truy cập thực tế.
2. Không tự bịa views/reach/reactions/comments/shares. Không biến metric chưa có dữ liệu thành 0 giả.
3. Engagement snapshots phải có source, timestamp, trạng thái xác minh; khi tổng hợp dùng snapshot gần nhất hợp lệ mỗi post/metric, không cộng tất cả snapshot của cùng một bài.
4. Báo cáo CTV được xác định thông qua Pipeline 1:1; không cộng trùng nhiều account, job hoặc bản ghi.
5. UI Tổng quan chỉ hiển thị KPI ngắn gọn; chi tiết nằm trong màn hình Analytics riêng.

R12 — UI/UX THƯƠNG MẠI, TỐI GIẢN VÀ CHÍNH XÁC

1. Less UI, Better UX: một màn hình có một mục tiêu chính; hiện thông tin cần quyết định, ẩn thông tin phụ trong menu ngữ cảnh/Advanced Settings. Không xây trang form dài nhiều màn hình khi có thể dùng các bước tự nhiên.
2. Thiết kế mobile-first cho Safari iPhone, sau đó iPad/desktop. Trang Admin là dashboard ngắn gọn; click card Pipeline mới mở Workspace. CTV login vào đúng Workspace, không thấy Admin Dashboard hoặc Pipeline khác.
3. Navigation nên gom những feature liên quan (Tổng quan, Đăng bài, Thư viện, Facebook, Hoạt động, Cài đặt). Mobile dùng drawer; desktop sidebar. Không hiển thị 15–20 tab ngang hoặc lặp menu ở trang Tổng quan.
4. Không icon rác: tuyệt đối không dùng emoji, sticker, icon 3D/đồ họa AI vô nghĩa, biểu tượng ngẫu nhiên, nhiều bộ icon hay icon nhiều màu. Icon chỉ dùng khi giúp định vị/chỉ hành động; dùng một hệ thống (ví dụ Lucide), tiết chế, cùng size/stroke. Với hành động quan trọng, luôn có nhãn dễ hiểu.
5. Không phơi logic/lỗi nội bộ: CTV/khách hàng không được thấy traceback, ProviderError, AccessDenied, raw JSON, SQL, tên bảng, đường dẫn server, .env, cookie/token/key, stack trace, selector Playwright, response upstream hoặc debug nội bộ. Frontend chỉ hiển thị câu ngắn, lịch sự, có thể hành động và request/support ID nếu cần. Ví dụ “Chưa thể kiểm tra video. Vui lòng thử lại.” Admin Diagnostics mới có log kỹ thuật, phải kiểm soát quyền và ẩn dữ liệu nhạy cảm.
6. Không giấu cảnh báo quan trọng: tình trạng cần duyệt, tài khoản bị hạn chế, nội dung rủi ro, phiên hết hạn, job không chắc đã gửi, thất bại thanh toán hoặc vi phạm chính sách phải được thể hiện trung thực bằng câu chữ dễ hiểu; không tắt cảnh báo chỉ để giao diện sạch. Phân biệt lỗi kỹ thuật, lỗi nhập liệu và cảnh báo an toàn.
7. Mọi API/page async phải có trạng thái idle/loading/success/empty/error/disabled rõ; skeleton tương ứng layout khi load dữ liệu; spinner nhỏ trong nút khi xử lý; chặn submit trùng; toast thành công/thất bại ngắn, không spam notification.
8. Upload ảnh/video hiển thị tiến trình thực nếu backend hỗ trợ; video AI job có stage + progress thực hoặc indeterminate. Tuyệt đối không hiển thị % tưởng tượng hoặc “đã đăng” khi chưa xác nhận.
9. Transition/micro-interaction nhẹ, có chủ đích: hover, focus, drawer, modal, toast, card; thời gian tham khảo 150–250ms, không animation nặng, hỗ trợ prefers-reduced-motion và thiết bị cấu hình thấp.
10. Form: nhãn rõ, field đúng kích cỡ, validation tại chỗ, helper text ngắn chỉ khi cần; advanced options mặc định thu gọn; giữ dữ liệu người dùng đang nhập khi request lỗi; có confirmation cho xóa/thu hồi key/các hành động khó đảo ngược.
11. Tổ chức bảng dài bằng search/filter/pagination; media dùng thumbnail/lazy loading. Không nhồi KPI card, nhiều khung trong khung, border/shadow/gradient vô nghĩa; màu trung tính + một accent, không màu lòe loẹt.
12. Không mock data trong production; không lấy đoạn văn Facebook ngẫu nhiên làm tên tài khoản; không hiện 0 thay cho dữ liệu chưa thu được. Trạng thái EMPTY chỉ khi API thật xác nhận dữ liệu trống, không dùng khi 403/422/500.
13. Responsive bắt buộc ở width 390, 402, 430, 440, 768, 834, 1280, 1440. Không horizontal overflow, chữ ép thành cột, checkbox/label lệch, table/card bóp méo, menu che nội dung, modal tràn, nút quá nhỏ.
14. Accessibility: focus, keyboard operability, contrast, label association, error text, semantic controls, loading announcement phù hợp; tương tác vẫn hoạt động khi giảm animation.
15. Không thay UI của chức năng khác chỉ vì style tiện tay. Mọi shared-component/CSS thay đổi phải có visual regression cho các trang sử dụng chung.

Hợp đồng UX bắt buộc đối với các trang chính

• Admin Dashboard: card Pipeline + KPI ngắn gọn; xem chi tiết analytics khi click; không hiển thị debug hoặc nhiều biểu đồ trên trang đầu.
• CTV Workspace lần đầu: hiển thị các bước cấu hình có thể làm ngay (kết nối Facebook, thêm Group, tải Album, cấu hình AI), không hiện “Chưa có cấu hình” như lỗi.
• Auto Group Posting: cấu hình một lần cho mọi Facebook trong Pipeline, chọn nguồn, nhóm, thời gian, giới hạn; chế độ nâng cao thu gọn.
• Facebook Accounts: danh sách gọn với tên chính xác, trạng thái, menu thao tác; upload cookie JSON/login chỉ mở khi người dùng cần.
• Album: ảnh là nội dung chính, upload/preview/selection mượt; không card thống kê dư thừa.
• Video Pre-Check: upload → tiến trình → kết quả ngắn gọn + chi tiết có thể mở; thông báo nội bộ được ánh xạ an toàn.
• Group/Comment/Queue: dòng gọn, trạng thái rõ, thao tác phụ vào menu ba chấm; chỉ hiện các quyền thực tế của CTV.

R13 — WORKER, QUEUE VÀ HIỆU NĂNG

1. HTTP handlers không xử lý FFmpeg, OCR, AWS, Playwright hoặc job dài trực tiếp. Dùng background jobs có lifecycle và trạng thái lưu DB.
2. Scheduler/worker khởi động lại phải khôi phục job mà không chạy trùng. Có idempotency key, transactional claim, concurrency limit, lock theo Facebook, timeout và graceful shutdown.
3. Retry chỉ dành cho lỗi tạm thời và phải phù hợp idempotency; không retry mù lệnh đăng bài/bình luận khi kết quả gửi chưa xác minh.
4. Tránh tạo ồ ạt browser sessions; không dùng polling dày, vòng sleep dài trong request, hoặc load AI model nhiều lần theo frame.
5. Theo dõi memory/CPU/disk, số job, retry, tỷ lệ lỗi, độ trễ, AWS calls/cost ước tính; log có request/job ID nhưng không chứa secrets.
6. Tối ưu có benchmark, không thay đổi logic nghiệp vụ để đổi lấy hiệu năng chưa được chứng minh.

R14 — QUY TRÌNH BẮT BUỘC TRƯỚC MỖI TASK

1. Read: đọc AGENTS.md trước; xác nhận đúng thư mục và tình trạng Git; xác định các module ảnh hưởng và tài nguyên cần bảo vệ.
2. Understand: kiểm tra luồng thực tế frontend → API → service → repository → DB → worker, không dựa trên phỏng đoán hoặc screenshot để khẳng định backend lỗi.
3. Plan: giới hạn phạm vi, các file sẽ sửa, contract/schema thay đổi, test cần chạy, rollback; hỏi khi một quyết định có thể phá dữ liệu hoặc thay đổi nghiệp vụ.
4. Backup: trước migration, thay đổi auth, cookie, session, scheduler hoặc production deployment; tuyệt đối không backup secrets lên repository công khai.
5. Implement: thay đổi tối thiểu, module đúng trách nhiệm, giữ API tương thích khi có thể; không phá chức năng đang hoạt động.
6. Validate: formatter/lint/typecheck/tests; kiểm tra lỗi/edge cases, RBAC isolation, restart, retry, migration và UI thực tế.
7. Deploy: chỉ sau khi test đạt; giữ khả năng rollback; kiểm tra fpm.service, /health và luồng nghiệp vụ bị ảnh hưởng. Không đổi Tunnel trừ khi task yêu cầu.
8. Report: ghi lại nguyên nhân, file sửa, migration, tests PASS/FAIL, bằng chứng E2E thật, blocker, rủi ro còn lại và trạng thái production. Không báo PASS cho chức năng chưa được kiểm chứng thực tế.

R15 — TIÊU CHUẨN NGHIỆM THU / DEFINITION OF DONE

Một task chỉ được đánh dấu DONE khi thỏa mãn tất cả điều kiện liên quan:

• Đúng nghiệp vụ 1 Pipeline = 1 CTV = 1 Workspace.
• Dữ liệu và secret cô lập theo Pipeline, API/worker kiểm tra quyền tại backend.
• Không mất dữ liệu; migration và rollback được đánh giá.
• Không làm hỏng chức năng hiện có; regression tests chạy đạt.
• Module tách hợp lý; không nhồi logic vào file khổng lồ.
• Không lỗi cú pháp, typecheck, tests, API contract hoặc lỗi nghiêm trọng trong console.
• Giao diện mobile/desktop có loading, error states; không vỡ layout.
• Với AI/AWS/OCR/Facebook: báo đúng phạm vi test và giới hạn thực tế; không giả dữ liệu, trạng thái hoặc chỉ số.
• fpm.service hoạt động sau deploy và endpoint /health trả trạng thái tốt; các E2E liên quan được xác minh.
• Báo cáo kết quả ngắn gọn, trung thực, đủ để người quản lý quyết định bước tiếp.

R16 — CẤM TUYỆT ĐỐI

• Tự tạo mô hình nhiều CTV chung một Pipeline.
• Xây tầng quyền sở hữu thứ hai làm lệch mô hình 1:1.
• Xóa/reset dữ liệu hoặc cookie để “sửa nhanh”.
• Tự ý ghi đè code đang dở hoặc sửa project không liên quan.
• Hardcode credentials, in .env/token ra log hoặc commit Git.
• Đưa dữ liệu Pipeline khác vào UI, API, AI prompt, cache hoặc worker.
• Bỏ qua kiểm duyệt bắt buộc; giả PASS, POSTED, APPROVED, metrics hoặc progress.
• Tự đăng lại bài/bình luận chưa xác minh kết quả, đã bị từ chối hoặc đã bị gỡ.
• Dùng xoay account/IP/cookie, giả mạo danh tính, CAPTCHA bypass hay biến thể nội dung để né chống spam/kiểm duyệt.
• Tái đưa ASR vào Video Pre-Check.
• Xây UI dài dòng, icon rác, quá nhiều card/form hoặc thông báo debug phơi ra khách hàng.
• Báo “hoàn thành” khi chỉ build/HTTP 200 mà tính năng E2E chưa hoạt động.

R17 — HỢP ĐỒNG API, TRẠNG THÁI VÀ XỬ LÝ LỖI

1. Định nghĩa kiểu request/response/error nhất quán, có validation và mã lỗi ổn định. Không bắt frontend phân tích chuỗi str(exception) hoặc Pydantic traceback để hiểu lỗi.
2. Backend trả HTTP status đúng nghĩa: 400/422 input không hợp lệ, 401 chưa đăng nhập, 403 không có quyền, 404 tài nguyên không tồn tại/không được phép thấy, 409 xung đột, 429 quá giới hạn, 5xx lỗi hệ thống; luôn có cấu trúc lỗi nhất quán.
3. UI chuyển các mã lỗi thành thông báo tiếng Việt ngắn, hướng giải quyết rõ. Không đưa exception message nội bộ lên màn hình khách hàng. Mã tham chiếu phục vụ hỗ trợ có thể hiển thị; thông tin chuyên sâu để ở Admin Diagnostics và log bảo vệ.
4. loading, empty, forbidden, validation_error, retryable_error, hard_error, needs_review là các trạng thái khác nhau; không được dùng EMPTY để che 403, 422 hoặc 500.
5. Khi dependency lỗi, không giả dữ liệu và không báo thành công. Có timeout, circuit breaker/backoff khi thích hợp; fail-safe đối với nghiệp vụ có rủi ro.
6. Mọi cập nhật dữ liệu nhiều bước phải có transaction hoặc idempotent recovery. Client và API đều chống double-submit, không chỉ disable button ở frontend.

R18 — QUẢN TRỊ CHẤT LƯỢNG SOURCE VÀ CODE REVIEW

1. Mỗi task phải ghi rõ module sở hữu thay đổi trước khi sửa. Không tiện tay sửa 5–10 module không liên quan; nếu cần refactor rộng thì tách thành phase riêng có test.
2. Sau mỗi lần thay đổi, tự audit diff: file nào đột ngột phình, có duplicated logic, deep nesting, import vòng, magic strings/numbers, side effects trong UI, SQL ở router hay API call rải rác không.
3. Không gộp nhiều business domain vào một file dưới danh nghĩa service.py tổng, utils.py, handlers.py, App.tsx, script.js, style.css. Mỗi feature sở hữu repository/service/schema rõ.
4. Có ranh giới public API giữa feature: thay đổi API contract phải cập nhật bên gọi, test và version/migration thích hợp; không gọi xuyên vào state/private helper của module khác.
5. Giữ thay đổi Git nhỏ, có thể review và rollback. Không commit secret, cache, compiled model, media người dùng, dữ liệu thật, DB snapshot hoặc file nhạy cảm; đảm bảo .gitignore đầy đủ.
6. Unit/integration tests phải kiểm tra logic và lỗi biên, không chỉ happy path; nếu bug đã xảy ra thì phải thêm regression test chứng minh trước/sau.
7. Không thay code đang hoạt động để làm “đẹp” trừ khi tác động được hiểu rõ và đã chạy kiểm thử. Không phá compatibility hoặc dữ liệu vì đổi framework.

R19 — QUY TẮC PHÂN PHỐI, BÀN GIAO VÀ VẬN HÀNH CHO KHÁCH HÀNG

1. Giao diện khách hàng chỉ có tính năng thực sự hoạt động; tính năng chưa sẵn sàng phải được gắn trạng thái phù hợp, không đặt button giả, không để dead ends.
2. Không hiển thị các nhãn kỹ thuật như MVP, mock, experimental worker, ProviderError hoặc tên infrastructure như VPS/Turso/AWS trừ nơi quản trị kỹ thuật có quyền; tuy nhiên không được che rủi ro hoặc trạng thái chưa xác minh của tính năng.
3. Hướng dẫn thao tác cơ bản trong UI phải ngắn, đủ hiểu, không đẩy tài liệu kỹ thuật dài ra trang CTV. Việc vận hành/khắc phục lỗi có runbook cho Admin/Support ở docs/.
4. Service khởi động lại không mất job, session, cấu hình CTV; có health/readiness, log rotation, backup/restore được kiểm chứng, giới hạn resource và cảnh báo khi disk/memory gần đầy.
5. Có quy trình phát hành theo version và changelog ngắn; staging/test, backup, smoke tests, health, rollback. Nếu release lỗi, phải khôi phục đúng dữ liệu và xác nhận khả năng phục hồi.
6. Quyền Admin cao nhất không có nghĩa được vô tình tiết lộ token/cookie của CTV ra dashboard. Hành động xem/đổi quyền, reset credentials, xóa tài khoản, can thiệp dữ liệu khách hàng phải được audit.
7. Một chức năng là production-ready khi được kiểm chứng ở luồng thật, có lỗi/empty/loading states, bảo mật, test và hướng dẫn vận hành. Không coi số lượng test PASS là bằng chứng tính năng third-party đã hoạt động.

R20 — CHECKLIST TỰ ĐÁNH GIÁ TRƯỚC KHI TRẢ LỜI / KẾT THÚC TASK

AI phải tự trả lời YES/NO cho các câu hỏi sau, không được tự nhận YES khi chưa kiểm chứng:

• Đúng 1 Pipeline = 1 CTV = 1 Workspace và không trộn dữ liệu tenant?
• Đã đọc code liên quan, kiểm tra thư mục project và tình trạng Git trước khi sửa?
• Đã tách code theo module và đúng tầng; không tạo thêm god-file hoặc logic rải rác?
• Đã kiểm tra API contract, RBAC, database migration và các tác vụ async liên quan?
• Có thể rollback; không mất cookie/session/Album/Group/lịch/số liệu?
• UI gọn, không icon rác, không cảnh báo/log nội bộ, có loading và thông báo lỗi dễ hiểu?
• Đã test responsive và các hành động người dùng thực tế nếu sửa frontend?
• Đã xử lý tình huống lỗi upstream, timeout, thiếu quyền, trùng job, chưa xác minh kết quả?
• Đã test chức năng bằng dữ liệu/nguồn thật khi báo E2E PASS, không dùng mock thay thế?
• Đã báo cáo ngắn gọn file sửa, phạm vi, test PASS/FAIL, blocker và trạng thái deploy thực tế?

Nếu có câu trả lời NO, phải ghi rõ là chưa hoàn thành điều kiện đó, chỉ rõ bước tiếp theo; không ngụy tạo kết quả.

────────

Nhắc lại bắt buộc cho mọi AI agent: Trước mỗi lần đụng vào project này, hãy đọc tài liệu, tuân thủ mô hình 1:1, bảo vệ dữ liệu Pipeline và thực thi công việc theo module. Nếu yêu cầu mới mâu thuẫn với quy tắc kiến trúc hoặc có nguy cơ phá dữ liệu, nêu xung đột và xin xác nhận trước khi thay đổi.
