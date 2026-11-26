# Bé Yêu Skill System

Bot hỗ trợ 2 cách cài skill:

## 1. Cài trực tiếp từ Discord (lưu vào Postgres, chạy ngay không cần deploy lại)
- Dùng slash command `/skill_install` (yêu cầu quyền admin).
- Hoặc gửi/đính kèm file `SKILL.md` vào chat và tag bot bảo **"bé yêu cài skill này vào"**.
- Quản lý: `/skills`, `/skill_toggle`, `/skill_remove`.

## 2. Cài qua thư mục `skills/` trong repo (hỗ trợ cả hướng dẫn Markdown lẫn Python tool)
Tạo thư mục `skills/<ten-skill>/` gồm:
- `SKILL.md` (bắt buộc): frontmatter YAML (`name`, `description`, tùy chọn `always_on: true/false`, `enabled: true/false`) và phần hướng dẫn Markdown cho model.
- `tool.py` (tùy chọn): nếu skill cần hàm Python thực thi riêng, export:
  - `TOOL_SCHEMA`: dict chuẩn OpenAI function schema (`{"type": "function", "function": {...}}`)
  - `ADMIN_ONLY`: `True` hoặc `False` (mặc định `False`)
  - `async def run(args: dict, ctx: dict) -> str` (hoặc hàm đồng bộ)
