# OpenClaw FB Manager Pro

Hệ thống quản lý & tự động hóa Facebook đa tài khoản với AI Agent, Telegram Bot và giao diện Desktop.

## Tính năng

- **Quản lý đa tài khoản** — Mở, quản lý nhiều profile Hidemium cùng lúc
- **AI Agent** — Agent tự động điều khiển trình duyệt thực hiện task bất kỳ (đăng bài, tương tác, duyệt feed...)
- **Telegram Bot** — Điều khiển từ xa qua Telegram (chat AI, ra lệnh agent, nuôi nick...)
- **Nuôi nick tự động** — Duyệt feed, like, comment tự động với AI sinh nội dung
- **Đăng bài hàng loạt** — Đăng bài lên nhiều tài khoản, page, group
- **Giao diện Desktop** — PySide6 UI với live monitoring

## Yêu cầu

- **Python** >= 3.9
- **Hidemium Browser** đang chạy trên port `2222`
- **Windows** 10/11

## Cài đặt

```bash
# Clone repo
git clone https://github.com/thaonguyenngu999-ui/openclawfb.git
cd openclawfb

# Tạo virtual environment
python -m venv .venv
.venv\Scripts\activate

# Cài dependencies
pip install -r requirements.txt
```

## Cấu hình

Sửa các API key trong file tương ứng:

| Key | File | Mô tả |
|-----|------|--------|
| `HIDEMIUM_TOKEN` | `config.py` | API key Hidemium (lấy từ app Hidemium) |
| `TELEGRAM_BOT_TOKEN` | `telegram_bot.py` | Token bot Telegram (tạo qua @BotFather) |
| `POLLINATIONS_KEY` | `telegram_bot.py` | API key Pollinations.ai |

## Chạy

### Giao diện Desktop

```bash
python main.py
```

### API Server + Telegram Bot (headless)

```bash
# Terminal 1 — API Server (port 8899)
python openclaw_api.py

# Terminal 2 — Telegram Bot
python telegram_bot.py
```

## Telegram Bot

| Lệnh | Ví dụ | Mô tả |
|-------|-------|--------|
| `s{id} {task}` | `s09 vào trang cá nhân viết bài về tết` | Agent thực hiện task trên profile |
| `nuôi nick` | `nuôi 5 nick` | Nuôi nick hàng loạt |
| `list` | `list` | Xem danh sách profile |
| Chat tự do | `hôm nay thời tiết thế nào?` | Chat AI |

## API Endpoints (port 8899)

| Method | Endpoint | Mô tả |
|--------|----------|--------|
| GET | `/status` | Trạng thái server |
| POST | `/open_browser` | Mở browser profile |
| POST | `/close` | Đóng browser |
| POST | `/screenshot` | Chụp màn hình |
| POST | `/agent_execute` | Chạy AI agent |
| POST | `/like` | Like bài viết |
| POST | `/fb_comment` | Comment bài |
| POST | `/fb_read_feed` | Đọc feed |
| POST | `/fb_nurture_batch` | Nuôi nick hàng loạt |
| POST | `/list_profiles` | Danh sách profile |

## Cấu trúc

```
├── main.py                 # GUI Desktop (PySide6)
├── openclaw_api.py         # HTTP API Server
├── telegram_bot.py         # Telegram Bot
├── config.py               # Cấu hình Hidemium
├── db.py / database.py     # Database
│
├── skills/                 # Business logic
│   ├── base_skill.py       # CDP, JS, screenshot, AI
│   ├── browser_skill.py    # Mở/đóng browser
│   ├── login_skill.py      # Đăng nhập Facebook
│   ├── feed_skill.py       # Like, comment, feed, nuôi nick
│   ├── groups_skill.py     # Quản lý group
│   ├── reels_skill.py      # Xem reels
│   ├── agent_skill.py      # AI Agent (compose_post, navigate, click...)
│   ├── vision_skill.py     # Vision click
│   └── telegram_mixin.py   # Telegram helpers
│
├── tabs/                   # GUI tabs
│   ├── interaction_page.py # Nuôi nick + live monitoring
│   ├── login_page.py       # Đăng nhập
│   ├── posts_page.py       # Đăng bài
│   ├── groups_page.py      # Groups
│   ├── pages_page.py       # Pages
│   ├── reels_page.py       # Reels
│   ├── content_page.py     # Nội dung
│   └── scripts_page.py     # Scripts
│
├── widgets/                # UI components
├── automation/             # CDP client & window manager
└── data/                   # JSON data & SQLite DB
```

## Tech Stack

- **UI**: PySide6
- **Browser**: CDP (Chrome DevTools Protocol) via WebSocket
- **AI**: Pollinations.ai (gemini-fast)
- **Antidetect**: Hidemium Browser
- **Bot**: python-telegram-bot
- **Server**: Python ThreadingHTTPServer
