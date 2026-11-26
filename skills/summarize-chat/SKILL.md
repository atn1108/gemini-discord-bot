---
name: summarize-chat
description: Tóm tắt nhanh các tin nhắn gần nhất trong kênh theo đầu mục rõ ràng khi thành viên nhờ tóm tắt.
always_on: false
enabled: true
---
# Skill: Tóm tắt hội thoại kênh Discord

Khi thành viên yêu cầu tóm tắt cuộc trò chuyện (ví dụ: "tóm tắt nãy giờ nói gì", "tldr kênh này", "tóm tắt 20 tin nhắn gần nhất"):
1. Gọi tool `read_channel` trên kênh hiện tại (hoặc kênh được chỉ định) với `limit` từ `10` đến `20`.
2. Đọc nội dung các tin nhắn vừa lấy (bao gồm cả tên người nói và phản hồi của bot khác nếu có).
3. Trình bày bản tóm tắt theo các gạch đầu dòng ngắn gọn:
   - **Chủ đề chính đang bàn**
   - **Các ý kiến / quyết định đáng chú ý** (gắn đúng tên thành viên)
   - **Việc cần làm tiếp** (nếu có)
4. Không bịa thêm chi tiết không có trong lịch sử kênh.
