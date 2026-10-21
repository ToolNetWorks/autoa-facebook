"""Bộ lập lịch thông minh cho toàn bộ Facebook của một Pipeline.

Module này chia công việc đăng bài của một Workspace thành bốn phần độc lập:

* :mod:`scheduler.time_windows` - khung giờ và cách rải bài trong ngày.
* :mod:`scheduler.account_limits` - hạn mức và khoảng cách của từng tài khoản.
* :mod:`scheduler.group_cooldowns` - khoảng nghỉ giữa hai lần đăng vào cùng group.
* :mod:`scheduler.job_allocator` - ghép tất cả lại thành lịch đăng.

Khoảng cách thời gian chỉ là một lớp kiểm soát để giảm rủi ro spam. Hệ thống
không cam kết tránh được hạn chế của Facebook và không dùng lịch ngẫu nhiên để
né hệ thống phát hiện.
"""
