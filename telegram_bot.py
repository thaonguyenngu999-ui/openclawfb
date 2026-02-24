#!/usr/bin/env python3
"""
FB Manager Pro Telegram Bot — NLP-powered.
Primary: Pollinations.ai rotation (GPT-5 Mini → DeepSeek V3.2 → Claude Haiku 4.5 → Mistral 3.2 → GPT-5 Nano)
Fallback: JanAI Devstral-24B (local, FREE)
Understands natural language like "s10 thoát hết nhóm" and executes FB Manager actions.
"""

import asyncio
import json
import re
import sys
import aiohttp
import logging
from telegram import Update, constants
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from telegram.request import HTTPXRequest

# Fix UTF-8 for Windows
sys.stdout.reconfigure(encoding='utf-8')

# Configuration
TELEGRAM_BOT_TOKEN = "6891995669:AAHOlqrRiSoNzI1FcyqWemvpo5ZRxV4AKDI"
FB_MANAGER_API = "http://127.0.0.1:8899"

# Pollinations.ai — Key rotation + Model fallback
# Primary: rotate API keys on same best model
# Fallback: try other models if all keys exhausted
POLLINATIONS_API = "https://gen.pollinations.ai/v1"

# API Key pool: ordered by pollen balance (most first)
POLLINATIONS_KEYS = [
    {"key": "sk_cH1x3TFuKuaFAK1tHH2y8NLJNejEgxJH", "name": "Key-3P", "pollen": 3},   # 3 pollen
    {"key": "sk_3HRi9HUGLup7OKB6ykRds8YtnpcmLHD3",  "name": "Key-1P", "pollen": 1},   # 1 pollen
]

# Model pool: ordered by intelligence (best first)
POLLINATIONS_MODELS = [
    {"model": "openai-large", "name": "GPT-5.2 Full",      "timeout": 60},   # BEST: GPT-5.2 full, 128K context
    {"model": "openai",       "name": "GPT-5 Mini",        "timeout": 30},   # Good: GPT-5 Mini, 128K context
    {"model": "kimi",         "name": "Kimi K2.5",         "timeout": 60},   # 256K context! reasoning+agentic+vision, CHEAP
    {"model": "deepseek",     "name": "DeepSeek V3.2",     "timeout": 45},   # Great reasoning, 64K context
    {"model": "claude-fast",  "name": "Claude Haiku 4.5",  "timeout": 30},   # Very accurate, 200K context
    {"model": "qwen-coder",   "name": "Qwen3 Coder 30B",   "timeout": 40},   # Good at code, 32K context
    {"model": "mistral",      "name": "Mistral Small 3.2", "timeout": 30},   # Reliable, 32K context
    {"model": "nova-fast",    "name": "Amazon Nova Micro",  "timeout": 20},   # ULTRA CHEAP + ultra fast
    {"model": "perplexity-reasoning", "name": "Sonar Reasoning", "timeout": 45},  # Reasoning + web search
    {"model": "openai-fast",  "name": "GPT-5 Nano",        "timeout": 25},   # Fast backup
    {"model": "gemini-fast",  "name": "Gemini 2.5 Flash",  "timeout": 30},   # Google fallback
    {"model": "gemini-search","name": "Gemini + Search",   "timeout": 30},   # Gemini + Google Search
    {"model": "perplexity-fast", "name": "Sonar",          "timeout": 30},   # Perplexity fallback
]

# Track rotation state + cooldowns
import time as _time
_key_index = 0       # Start with best key (3 pollen)
_model_index = 0     # Start with best model
_key_cooldowns = {}   # key -> cooldown_until timestamp
_model_cooldowns = {} # model -> cooldown_until timestamp

# Global cancel event — set by stop_all to abort running batch operations
_cancel_event: asyncio.Event = None  # Lazy init (needs running loop)
_cancel_generation: int = 0  # Incremented on each stop, prevents stale clears

# Global task registry — tracks ALL running batch/agent tasks for hard cancel
_active_tasks: set = set()  # Set[asyncio.Task]

def _register_task(task: asyncio.Task):
    """Register a running task so stop_all can cancel it."""
    _active_tasks.add(task)
    task.add_done_callback(_active_tasks.discard)

def _cancel_all_tasks():
    """Hard-cancel ALL registered tasks."""
    count = 0
    for t in list(_active_tasks):
        if not t.done():
            t.cancel()
            count += 1
    logger.info(f"🛑 Cancelled {count} active tasks")
    return count

def _get_cancel_event() -> asyncio.Event:
    """Get or create the global cancel event (lazy, loop-safe)."""
    global _cancel_event
    if _cancel_event is None:
        _cancel_event = asyncio.Event()
    return _cancel_event

def _request_cancel():
    """Signal all running batch operations to stop."""
    global _cancel_generation
    evt = _get_cancel_event()
    evt.set()
    _cancel_generation += 1
    logger.info(f"🛑 Cancel event SET (gen={_cancel_generation}) — all batch ops will stop")

def _clear_cancel_safe(my_gen: int):
    """Clear cancel signal ONLY if no newer stop was issued (race-safe)."""
    if my_gen >= _cancel_generation:
        evt = _get_cancel_event()
        evt.clear()
    else:
        logger.info(f"Skipping cancel clear: my_gen={my_gen} < current={_cancel_generation}")

def _clear_cancel():
    """Clear cancel signal (legacy, use _clear_cancel_safe when possible)."""
    evt = _get_cancel_event()
    evt.clear()

def _is_cancelled() -> bool:
    """Check if cancel has been requested."""
    evt = _get_cancel_event()
    return evt.is_set()

# ---------------------------------------------------------------------------
# Batch operation guard — only ONE batch at a time, capped concurrency
# ---------------------------------------------------------------------------
MAX_BATCH_CONCURRENT = 15         # Global max profiles open at once
_batch_lock: asyncio.Lock = None  # Lazy init
_batch_name: str = None           # Name of currently running batch

def _get_batch_lock() -> asyncio.Lock:
    global _batch_lock
    if _batch_lock is None:
        _batch_lock = asyncio.Lock()
    return _batch_lock

async def _acquire_batch(name: str) -> str | None:
    """Try to acquire the batch lock.  Returns error message if busy, else None."""
    global _batch_name
    lock = _get_batch_lock()
    if lock.locked():
        return f"⚠️ Đang chạy batch khác ({_batch_name}). Hãy đợi hoặc /stop_all trước."
    await lock.acquire()
    _batch_name = name
    return None

def _release_batch():
    """Release the batch lock (safe to call even if not held)."""
    global _batch_name
    lock = _get_batch_lock()
    _batch_name = None
    try:
        lock.release()
    except RuntimeError:
        pass

def _classify_error(detail: str) -> str:
    """Classify an error detail string into a human-readable reason."""
    d = (detail or "").lower()
    if "không mở được browser" in d or "cannot open" in d or "open_browser" in d:
        return "Browser không mở được (Hidemium lỗi/quá tải)"
    if "remote_port" in d or "port" in d:
        return "Không lấy được port browser"
    if "websocket" in d or "ws" in d:
        return "WebSocket kết nối thất bại"
    if "timeout" in d or "timed out" in d:
        return "Timeout (browser/page quá chậm)"
    if "connection" in d and ("refused" in d or "reset" in d):
        return "Browser bị đóng/crash giữa chừng"
    if "navigate" in d or "page" in d:
        return "Không navigate được tới FB"
    if "unknown" in d or "no_result" in d or "js_returned_none" in d:
        return "FB page load lỗi (JS detection fail)"
    if "already" in d:
        return "Browser đang mở sẵn bởi task khác"
    return f"Lỗi khác: {detail[:60]}"

def _get_next_key() -> dict:
    """Get next available API key, skip ones on cooldown."""
    global _key_index
    now = _time.time()
    for _ in range(len(POLLINATIONS_KEYS)):
        k = POLLINATIONS_KEYS[_key_index % len(POLLINATIONS_KEYS)]
        if now >= _key_cooldowns.get(k["key"], 0):
            return k
        _key_index = (_key_index + 1) % len(POLLINATIONS_KEYS)
    # All on cooldown — clear and return first
    _key_cooldowns.clear()
    return POLLINATIONS_KEYS[0]

def _rotate_key(failed_key: str, cooldown_secs: int = 90):
    """Put failed key on cooldown and rotate to next."""
    global _key_index
    _key_cooldowns[failed_key] = _time.time() + cooldown_secs
    _key_index = (_key_index + 1) % len(POLLINATIONS_KEYS)
    next_k = _get_next_key()
    logger.warning(f"🔑 Key rotated → {next_k['name']} (cooldown {cooldown_secs}s)")

def _get_next_model() -> dict:
    """Get next available model, skip ones on cooldown."""
    global _model_index
    now = _time.time()
    for _ in range(len(POLLINATIONS_MODELS)):
        m = POLLINATIONS_MODELS[_model_index % len(POLLINATIONS_MODELS)]
        if now >= _model_cooldowns.get(m["model"], 0):
            return m
        _model_index = (_model_index + 1) % len(POLLINATIONS_MODELS)
    _model_cooldowns.clear()
    return POLLINATIONS_MODELS[0]

def _rotate_model(failed_model: str, cooldown_secs: int = 60):
    """Put failed model on cooldown and rotate to next."""
    global _model_index
    _model_cooldowns[failed_model] = _time.time() + cooldown_secs
    _model_index = (_model_index + 1) % len(POLLINATIONS_MODELS)
    next_m = _get_next_model()
    logger.warning(f"🔄 Model rotated → {next_m['name']} (cooldown {cooldown_secs}s)")

# JanAI local (last resort fallback) — Devstral-24B, FREE, no internet needed
JANAI_API = "http://127.0.0.1:1337/v1"
DEVSTRAL_MODEL = "Devstral-Small-2-24B-Instruct-2512-IQ4_XS"

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8', mode='a'),
    ]
)
logger = logging.getLogger(__name__)

# ============================================================
# Helpers: Markdown escape + safe edit
# ============================================================

def escape_md(text: str) -> str:
    """Escape special chars for Telegram legacy Markdown."""
    if not text:
        return text
    for ch in ('\\', '`', '*', '_', '[', ']'):
        text = text.replace(ch, f'\\{ch}')
    return text


async def safe_edit(msg, text: str, parse_mode: str = "Markdown"):
    """Edit message text with Markdown fallback → plain text fallback."""
    if not text:
        text = "✅ Done"
    try:
        await msg.edit_text(text, parse_mode=parse_mode)
    except Exception:
        try:
            await msg.edit_text(text)
        except Exception as e:
            logger.warning(f"safe_edit failed: {e}")


async def send_long_text(text: str, target_msg):
    """Send long text, split into multiple messages if > 4000 chars."""
    MAX_LEN = 4000
    if not text:
        text = "✅ Done"
    if len(text) <= MAX_LEN:
        await safe_edit(target_msg, text)
        return
    # Split into chunks
    chunks = []
    while text:
        if len(text) <= MAX_LEN:
            chunks.append(text)
            break
        split_pos = text.rfind('\n', 0, MAX_LEN)
        if split_pos < MAX_LEN // 2:
            split_pos = MAX_LEN
        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip('\n')
    # Edit first chunk into existing message
    await safe_edit(target_msg, chunks[0])
    # Send remaining chunks as new messages
    for chunk in chunks[1:]:
        try:
            await target_msg.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            try:
                await target_msg.reply_text(chunk)
            except Exception:
                pass


# ============================================================
# Conversation memory (per chat_id)
# ============================================================
from collections import deque

# chat_id -> deque of {role, content} (last N messages)
CONVERSATION_HISTORY = {}
MAX_HISTORY = 20  # Keep last 20 exchanges for better context

def get_history(chat_id: str) -> list:
    """Get conversation history for a chat."""
    return list(CONVERSATION_HISTORY.get(chat_id, []))

def add_to_history(chat_id: str, role: str, content: str):
    """Add a message to conversation history."""
    if chat_id not in CONVERSATION_HISTORY:
        CONVERSATION_HISTORY[chat_id] = deque(maxlen=MAX_HISTORY)
    CONVERSATION_HISTORY[chat_id].append({"role": role, "content": content})

# ============================================================
# SYSTEM PROMPT for NLP intent parsing
# ============================================================

# Grok (xAI) — Supervisor AI: analyzes, plans, validates, auto-fixes
GROK_API_KEY = "xai-JBnA5g8A9rAZr1KeU3lOpHZNgLtp2PfbfCi23BfOYvjRO0WNoOOWS0wLcwgReJWR91Fk3KVAGgwKya9W"
GROK_API_URL = "https://api.x.ai/v1/chat/completions"
GROK_MODEL = "grok-4-1-fast-reasoning"  # Cheaper than grok-3-mini ($0.20 vs $0.30/M input), 2M context, reasoning
GROK_ENABLED = True  # Set False to disable Grok supervisor and use GPT-only flow

# ============================================================
# GROK INTELLIGENCE: Error Memory + System Awareness
# ============================================================
_grok_error_memory = []  # List of {timestamp, action, error, fix_tried, resolved}
MAX_ERROR_MEMORY = 50

def _grok_remember_error(action: str, error: str, fix_tried: str = "", resolved: bool = False):
    """Save an error to Grok's memory for learning."""
    _grok_error_memory.append({
        "timestamp": _time.time(),
        "action": action,
        "error": error[:500],
        "fix_tried": fix_tried[:200],
        "resolved": resolved
    })
    while len(_grok_error_memory) > MAX_ERROR_MEMORY:
        _grok_error_memory.pop(0)

def _grok_get_past_errors(action: str = None, limit: int = 5) -> str:
    """Get recent error history relevant to an action."""
    relevant = _grok_error_memory
    if action:
        relevant = [e for e in relevant if e["action"] == action]
    recent = relevant[-limit:]
    if not recent:
        return "Không có lỗi nào trước đó."
    lines = []
    for e in recent:
        status = "Fixed" if e["resolved"] else "Unfixed"
        lines.append(f"- {e['action']}: {e['error'][:80]} | Fix: {e['fix_tried'][:60]} | {status}")
    return "\n".join(lines)

SYSTEM_PROMPT = """Bạn là CUTE AI - trợ lý điều khiển hệ thống quản lý Facebook profiles (Hidemium).
Xưng "em", gọi user "anh". Giọng điệu dễ thương, nhí nhảnh, gọn lẹ kiểu gái thông minh.

OUTPUT: CHỈ JSON. {"action": "...", "params": {...}, "reply": "1-2 câu tiếng Việt ngắn"}
reply PHẢI ngắn gọn, KHÔNG giải thích logic, KHÔNG lặp lại yêu cầu.

AGENT LOOP: Sau mỗi action, hệ thống gửi lại kết quả. Bạn trả:
- XONG → {"action": "done", "params": {}, "reply": "tóm tắt"}
- TIẾP → JSON action tiếp theo
Tối đa 5 bước. Mỗi bước phải có mục đích rõ.

ENTITY:
- fb1, fb2, fb3... = folder_id (truyền "fb1")
- S10, A20... = profile (truyền "S10")
- "5 luồng" = concurrency (mặc định 5)

═══ BẢNG QUYẾT ĐỊNH (đọc từ trên xuống, match đầu tiên thắng) ═══

User nói gì?                          → action
─────────────────────────────────────────────────────────
"stop/dừng/tắt hết/ngừng"            → stop_all
danh sách UID+PASS + folder + "login" → bulk_login {folder_id, accounts, count, concurrency, close_after}
"tạo/thêm" + số + profile + folder  → create_profiles {folder_id, count, name_prefix}
"trùng/duplicate/giống nhau" + folder → check_duplicates {folder_id}
"xóa trùng/giữ 1" + folder           → delete_duplicates {folder_id}
"xóa hết/sạch" + folder (KO nói die) → delete_all_profiles {folder_id}
"xóa die/lọc die" + folder           → delete_die_profiles {folder_id, concurrency}
"xóa die toàn bộ/tất cả"             → delete_die_all {concurrency}
"check/kiểm" + folder (KO nói xóa)   → batch_check_login {folder_id, concurrency}
"check/kiểm" + "xóa" + folder        → delete_die_profiles {folder_id, concurrency}
"còn bao nhiêu/sao rồi/xong chưa"    → verify_status {folder_id}
"có mấy folder/tổng"                  → list_folders {}
"liệt kê/list" + folder              → list_profiles {folder_id}
"mở" + profile                        → open_browser {profile}
"đóng/tắt" + profile                  → close_browser {profile}
"check" + 1 profile                   → check_fb_status {profile}
"thoát nhóm/group" + profile          → leave_groups {profile}
"lướt reels/xem reels" + profile       → watch_reels {profile, count}
"đăng reels/upload reel" + profile    → agent_execute {profile, task: "upload reel from [folder] chủ đề [topic]"}
"tạo page/tạo trang" + profile        → agent_execute {profile, task: "create_page tên [name] danh mục [category]"}
"bao nhiêu page/xem page" + profile   → agent_execute {profile, task: "list_fb_pages"}
"nuôi" + profiles                     → fb_nurture_batch {profiles}
1 profile + task cụ thể               → agent_execute {profile, task}
"tất cả/chạy hết" + folder + task     → batch_agent_execute {folder_id, task, concurrency}
"chụp/screenshot" + profile           → screenshot {profile}
"xem groups" + profile                → debug_groups {profile}
lỗi/bug/vấn đề kỹ thuật              → dev_diagnose {error}
"xem code/đọc file"                   → dev_read_code {file, search}
"check hệ thống/services"             → dev_check_health {}
"test/run command"                     → dev_run_cmd {command, type}
phát hiện bug cần sửa                 → dev_fix_code {file, old_code, new_code}
"lưu skill" (KHÔNG chạy)             → dev_create_skill {name, ...}
chào hỏi/nói chuyện                   → chat {}
"xem tất cả action có"              → dev_list_actions {}

═══ ACTIONS ẨN (không cần vào bảng, AI tự dùng khi cần) ═══
create_profile {name, folder_name, os, proxy}
update_profile_name {uuid, name}
update_profile_note {uuid, note}
update_proxy {uuid, proxy}
remove_proxy {uuid}
change_fingerprint {uuid}
change_status {uuid, status}
add_to_folder {folder_uuid, profile_uuids}
sync_tags {uuid, tags}
login_fb {profile, email, password}
navigate {profile, url}
fb_comment {profile, comment}
fb_read_feed {profile}
dev_restart_service {service}

═══ VÍ DỤ CHỌN ACTION ═══

"chạy hết fb1 xem trùng không"
→ Ý: check trùng lặp → check_duplicates {folder_id: "fb1"}
(KHÔNG dùng batch_agent_execute vì hệ thống ĐÃ CÓ action chuyên dụng)

"xóa cái trùng đi giữ 1 cái" + context fb1
→ delete_duplicates {folder_id: "fb1"}
(KHÔNG dùng batch_agent_execute — đã có action xóa trùng chuyên dụng)

"check fb1 xong xóa die"
→ Step 1: batch_check_login {folder_id: "fb1"} → Step 2: delete_die_profiles {folder_id: "fb1"}

"fb1 có profiles nào"
→ list_profiles {folder_id: "fb1"}

"stop" / "dừng lại" / "tắt hết"
→ stop_all {}

"tạo 5 profile fb1"
→ create_profiles {folder_id: "fb1", count: 5, name_prefix: "s"}
(name_prefix: "s" → s1,s2,s3... | "Profile" → Profile_1,Profile_2... | user có thể chỉ định)

User gửi danh sách "61555409704088  6nZSTibO8f2@\\n61555416063703  rLFy5wjah52@" + "login fb1" hoặc "đăng nhập fb1"
→ bulk_login {folder_id: "fb1", accounts: "61555409704088  6nZSTibO8f2@\\n61555416063703  rLFy5wjah52@", count: 0, concurrency: 2, close_after: true, auto_create: true, delete_die: true, name_prefix: "s"}
(accounts = TOÀN BỘ text uid+pass gốc, GIỮ NGUYÊN format, mỗi dòng = 1 account)
(count=0 nghĩa là dùng tất cả. Nếu user nói "5 cái đầu" → count: 5)
(auto_create=true: tự tạo profiles nếu folder rỗng/thiếu)
(delete_die=true: tự xóa profile die sau login)

QUAN TRỌNG VỀ bulk_login:
- Khi user gửi danh sách số dài + password → đó là UID/PASS Facebook, DÙNG bulk_login
- KHÔNG dùng batch_agent_execute cho việc login
- KHÔNG dùng batch_check_login cho việc login
- accounts phải là STRING chứa text gốc user gửi (mỗi dòng UID PASS)
- Nếu user nói "tạo profile + login" hoặc "tạo rồi login" → auto_create: true
- Nếu user nói "xóa die" hoặc "die thì xóa" → delete_die: true
- Nếu user nói "theo s1,s2" hoặc "đặt tên s" → name_prefix: "s"
- Folder user nói "FB" → folder_id: "FB" (PHÂN BIỆT HOA THƯỜNG)

═══ QUY TẮC VÀNG ═══
1. Ưu tiên action CHUYÊN DỤNG hơn action generic (check_duplicates > batch_agent_execute)
2. batch_agent_execute CHỈ khi user muốn chạy 1 task TÙY Ý trên mọi profile (VD: "tất cả fb1 vào fb.com/me")
3. KHÔNG dùng batch_agent_execute cho: check, trùng, xóa, liệt kê (đã có action riêng)
4. Phân tích ngữ cảnh từ history: "nó", "cái đó", "tiếp đi" → suy ra từ context
5. Đủ thông tin → làm luôn, ĐỪNG HỎI LẠI
6. KHÔNG CÓ action phù hợp → chat

═══ DEV MODE: TỰ SỬA CODE ═══
Khi phát hiện lỗi/bug, thực hiện Agent Loop 3 bước:
Step 1: dev_read_code {file, search: "keyword1|keyword2"} → đọc code liên quan  
Step 2: dev_diagnose {error} → phân tích root cause
Step 3: dev_fix_code {file, old_code: "đoạn code gốc CHÍNH XÁC", new_code: "code đã sửa", reason: "..."}
QUAN TRỌNG: old_code phải COPY CHÍNH XÁC từ kết quả dev_read_code (giữ nguyên indent, dấu cách).
dev_read_code search hỗ trợ OR: dùng | để tìm nhiều keyword (VD: "delete|remove|destroy")
"""


# ============================================================
# AI providers (Key rotation → Model fallback → JanAI)
# ============================================================
async def call_pollinations(messages: list, max_tokens: int = 4000, temperature: float = 0.1) -> str:
    """Call Pollinations.ai with KEY rotation + MODEL fallback.
    Strategy: Try all keys on best model → try all keys on next model → ... → JanAI.
    """
    tried_combos = set()  # (key, model) combos already tried
    max_attempts = len(POLLINATIONS_KEYS) * len(POLLINATIONS_MODELS)
    attempts = 0
    
    while attempts < max_attempts:
        key_info = _get_next_key()
        model_info = _get_next_model()
        combo = (key_info["key"], model_info["model"])
        
        if combo in tried_combos:
            # Try rotating key first, then model
            if attempts % len(POLLINATIONS_KEYS) == 0 and attempts > 0:
                _rotate_model(model_info["model"], cooldown_secs=30)
            else:
                _rotate_key(key_info["key"], cooldown_secs=30)
            attempts += 1
            continue
        tried_combos.add(combo)
        attempts += 1
        
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": model_info["model"],
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "stream": False
                }
                headers = {
                    "Authorization": f"Bearer {key_info['key']}",
                    "Content-Type": "application/json"
                }
                timeout = aiohttp.ClientTimeout(total=model_info.get("timeout", 30))
                async with session.post(
                    f"{POLLINATIONS_API}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=timeout
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        choices = data.get("choices", [])
                        if not choices:
                            logger.warning(f"⚠️ Empty choices [{key_info['name']}] [{model_info['name']}]")
                            _rotate_key(key_info["key"], cooldown_secs=30)
                            continue
                        content = choices[0].get("message", {}).get("content", "")
                        if not content or not content.strip():
                            # Empty response — try next key/model
                            logger.warning(f"⚠️ Empty response [{key_info['name']}] [{model_info['name']}]")
                            _rotate_key(key_info["key"], cooldown_secs=30)
                            continue
                        logger.info(f"✅ AI OK [{model_info['name']}] [{key_info['name']}]")
                        return content
                    elif resp.status == 429:
                        # Rate limited on this key — rotate key first
                        logger.warning(f"⚠️ 429 [{key_info['name']}] [{model_info['name']}]")
                        _rotate_key(key_info["key"], cooldown_secs=120)
                        continue
                    else:
                        error_text = await resp.text()
                        logger.warning(f"⚠️ {resp.status} [{key_info['name']}] [{model_info['name']}]: {error_text[:100]}")
                        _rotate_key(key_info["key"], cooldown_secs=60)
                        continue
        except asyncio.TimeoutError:
            logger.warning(f"⏰ Timeout [{key_info['name']}] [{model_info['name']}]")
            _rotate_model(model_info["model"], cooldown_secs=60)
            continue
        except Exception as e:
            logger.warning(f"❌ Error [{key_info['name']}] [{model_info['name']}]: {e}")
            _rotate_key(key_info["key"], cooldown_secs=60)
            continue
    
    logger.error("All Pollinations keys × models failed!")
    return None  # Signal to try JanAI fallback


async def call_janai(messages: list, max_tokens: int = 4000, temperature: float = 0.3) -> str:
    """Call JanAI local model (auto-detect model, FREE)."""
    try:
        async with aiohttp.ClientSession() as session:
            # Auto-detect model
            model_name = DEVSTRAL_MODEL
            try:
                async with session.get(f"{JANAI_API}/models", timeout=aiohttp.ClientTimeout(total=3)) as mresp:
                    if mresp.status == 200:
                        mdata = await mresp.json()
                        models = mdata.get("data", [])
                        if models:
                            model_name = models[0]["id"]
                            logger.info(f"JanAI auto-detected model: {model_name}")
            except Exception:
                pass

            payload = {
                "model": model_name,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False
            }
            async with session.post(
                f"{JANAI_API}/chat/completions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    choices = data.get("choices", [])
                    if not choices:
                        logger.warning("JanAI returned empty choices")
                        return json.dumps({"action": "chat", "params": {}, "reply": "AI không trả kết quả (empty choices)"})
                    content = choices[0].get("message", {}).get("content", "")
                    logger.info("JanAI fallback OK")
                    return content
                else:
                    error_text = await resp.text()
                    logger.error(f"JanAI {resp.status}: {error_text[:300]}")
                    return f'{{"action":"chat","params":{{}},"reply":"Lỗi AI: cả Pollinations lẫn JanAI đều fail"}}'
    except Exception as e:
        logger.error(f"JanAI error: {e}")
        return json.dumps({"action": "chat", "params": {}, "reply": f"Lỗi kết nối AI: {str(e)[:200]}"})


async def call_ai(messages: list, max_tokens: int = 4000, temperature: float = 0.1) -> str:
    """Try Pollinations rotation (5 models), fallback to JanAI (local, free)."""
    result = await call_pollinations(messages, max_tokens, temperature)
    if result is not None:
        return result
    logger.warning("All Pollinations models exhausted, trying JanAI fallback...")
    # Only try JanAI if it's actually running (avoid connection refused errors)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{JANAI_API}/models", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    logger.info("JanAI is running, using fallback")
                    return await call_janai(messages, max_tokens, temperature)
    except Exception:
        pass
    logger.error("JanAI not running — all AI providers down")
    return json.dumps({"action": "chat", "params": {}, "reply": "⚠️ AI tạm thời không khả dụng (Pollinations down + JanAI offline). Thử lại sau 1-2 phút."})


async def call_grok(messages: list, max_tokens: int = 4000, temperature: float = 0.3) -> str | None:
    """Call Grok (xAI) as supervisor AI. Returns None on failure."""
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": GROK_MODEL,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False
            }
            headers = {
                "Authorization": f"Bearer {GROK_API_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "FBManagerPro/1.0"
            }
            timeout = aiohttp.ClientTimeout(total=60)
            async with session.post(GROK_API_URL, json=payload, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.warning(f"Grok API error {resp.status}: {err[:200]}")
                    return None
                data = await resp.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                logger.info(f"Grok [{GROK_MODEL}] {usage.get('prompt_tokens',0)}in/{usage.get('completion_tokens',0)}out: {content[:200]}")
                return content
    except Exception as e:
        logger.error(f"Grok API call failed: {e}")
        return None


# ============================================================
# GROK SUPERVISOR: Plan → Execute → Validate → Fix → Retry
# ============================================================

GROK_SUPERVISOR_PROMPT = """Bạn là GROK — BỘ NÃO CHÍNH (SUPERVISOR AI) của hệ thống FB Manager Pro.
Bạn có TOÀN QUYỀN: giám sát, lên kế hoạch, quan sát, phân tích lỗi, TỰ SINH CODE, TỰ SỬA BUG.
Bạn KHÔNG BAO GIỜ bỏ qua lỗi. Mọi lỗi PHẢI được phân tích và xử lý.
Bạn là AI THÔNG MINH NHẤT trong hệ thống — GPT worker chỉ là công nhân, BẠN là giám đốc.

HỆ THỐNG:
- Hidemium browser manager quản lý hàng trăm Facebook profiles theo folder (fb1, fb2...)
- Mỗi profile = 1 trình duyệt riêng biệt với fingerprint riêng
- GPT worker parse user intent → JSON action, nhưng HAY SAI → bạn phải fix
- Bạn (Grok) là BỘ NÃO CHÍNH, quyết định cuối cùng
- Bạn CÓ THỂ đọc code, phân tích bug, viết code fix, và yêu cầu restart service
- Bạn NHỚ các lỗi đã gặp và học từ chúng

═══ BẢNG ACTION ĐẦY ĐỦ ═══
stop_all {}                                    → Dừng mọi thứ
list_folders {}                                → Xem tất cả folder + số profile
list_profiles {folder_id}                      → Liệt kê profiles trong folder
batch_check_login {folder_id?, concurrency}    → Check trạng thái login hàng loạt
delete_die_profiles {folder_id, concurrency}   → Xóa profiles die trong folder
delete_die_all {concurrency}                   → Xóa die toàn bộ hệ thống
check_duplicates {folder_id}                   → Kiểm tra trùng lặp
delete_duplicates {folder_id}                  → Xóa profiles trùng
delete_all_profiles {folder_id}                → Xóa sạch 1 folder
bulk_login {folder_id, accounts, concurrency, auto_create, delete_die, name_prefix}  → Đăng nhập hàng loạt bằng UID+PASS (auto_create=true tự tạo profiles, delete_die=true tự xóa die)
create_profiles {folder_id, count, name_prefix}→ Tạo profiles mới
open_browser {profile}                         → Mở trình duyệt
close_browser {profile}                        → Đóng trình duyệt
check_fb_status {profile}                      → Check 1 profile live/die
agent_execute {profile, task}                  → Chạy task tùy ý trên 1 profile
batch_agent_execute {folder_id, task, concurrency} → Chạy task trên tất cả profiles
leave_groups {profile}                         → Thoát groups
watch_reels {profile, count}                   → Lướt reels
fb_nurture_batch {profiles}                    → Nuôi nick tự động
screenshot {profile}                           → Chụp màn hình

═══ QUY TẮC SUY LUẬN ═══
- "fb live"/"sống"/"còn sống" → batch_check_login (check trạng thái)
- "trùng/duplicate" → check_duplicates hoặc delete_duplicates
- "xóa die" → delete_die_profiles (1 folder) hoặc delete_die_all (tất cả)
- "bao nhiêu" + folder = list_profiles, "bao nhiêu" + tất cả = list_folders
- "X luồng/thread" → concurrency=X
- Không nói folder cụ thể = tất cả folders (bỏ folder_id)
- "check xong xóa" = 2 steps: batch_check_login → delete_die_profiles
- Ưu tiên action CHUYÊN DỤNG > batch_agent_execute
- Hành động nguy hiểm (xóa, stop) → thêm risk warning

═══ XỬ LÝ LỖI (QUAN TRỌNG NHẤT) ═══
Bạn TUYỆT ĐỐI KHÔNG ĐƯỢC bỏ qua lỗi. Khi thấy lỗi:
1. PHÂN TÍCH nguyên nhân gốc (root cause)
2. ĐỀ XUẤT cách sửa cụ thể
3. NẾU retry được → trả status "retry" kèm fix action với params đã sửa
4. NẾU không retry được → giải thích RÕ RÀNG cho user biết lý do + gợi ý giải pháp
5. NẾU là lỗi CODE → phân tích file nào gây lỗi, keyword tìm kiếm, đề xuất fix

Các lỗi thường gặp & cách xử lý:
- "Không mở được browser" → Hidemium quá tải, giảm concurrency xuống 3-5, retry
- "remote_port" / "WebSocket" → Browser mở nhưng chưa sẵn sàng, tăng wait time, retry
- "Timeout" → Page load chậm, retry với ít profiles hơn hoặc concurrency thấp hơn
- "UNKNOWN/no_result" → JS detection fail (FB page chưa load xong), retry
- "connection refused/reset" → Browser crash, retry
- "ERROR" trong batch → MỖI profile lỗi phải được retry riêng
- Batch có errors → KHÔNG BAO GIỜ chấp nhận "ok", PHẢI retry hoặc giải thích
- Nếu đã retry 2 lần vẫn lỗi → ESCALATE: phân tích code, tìm bug, đề xuất fix

═══ TỰ SỬA CODE (ESCALATION) ═══
Khi lỗi retry params không giải quyết được, bạn được phép:
1. Xác định file nào gây lỗi (telegram_bot.py, openclaw_api.py, skills/login_skill.py, etc.)
2. Chỉ ra keyword tìm kiếm trong file để tìm code liên quan
3. Phân tích code và đề xuất fix (old_code → new_code)
4. Hệ thống sẽ tự áp dụng fix + restart nếu confidence >= 0.7

Khi được hỏi [CODE ANALYSIS], trả:
{"is_code_bug": true/false, "file_to_read": "filename", "search_keyword": "keyword|keyword2", "diagnosis": "phán đoán"}

Khi được hỏi [CODE FIX], trả:
{"old_code": "code gốc CHÍNH XÁC", "new_code": "code đã sửa", "reason": "lý do", "needs_restart": true/false, "confidence": 0.0-1.0}

═══ OUTPUT FORMAT ═══

Khi PLAN (phân tích yêu cầu user):
{"analysis": "...", "plan": [{"step": 1, "action": "...", "params": {...}}], "risk": "nếu có"}

Khi VALIDATE (đánh giá kết quả):
{"status": "ok|retry|error", "diagnosis": "phân tích chi tiết", "fix": {"action": "...", "params": {...}}, "user_message": "giải thích cho user nếu cần"}
- ok = hoàn thành tốt, KHÔNG có lỗi nào
- retry = CÓ lỗi, cần retry với params đã fix (giảm concurrency, thay đổi params)
- error = lỗi không thể retry, giải thích rõ cho user

Khi FIX GPT output:
{"action": "...", "params": {...}, "reply": "mô tả ngắn"}

Khi ERROR ANALYSIS (phân tích lỗi):
{"diagnosis": "nguyên nhân gốc", "solution": "cách khắc phục", "retry_action": {"action": "...", "params": {...}} hoặc null, "user_message": "giải thích ngắn cho user"}

QUAN TRỌNG: Thấy ⚠️ Err hoặc ❌ Lỗi → PHẢI phân tích, KHÔNG ĐƯỢC bỏ qua.
Luôn trả JSON. Tối đa 5 steps. Tiếng Việt.
"""


def _extract_json_from_text(text: str) -> dict | None:
    """Extract JSON from text that may contain markdown code blocks or extra text."""
    if not text:
        return None
    # Try direct parse first
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    # Try extracting from ```json ... ```
    m = re.search(r'```(?:json)?\s*\n?(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Try finding first { ... } block
    depth = 0
    start = -1
    for i, c in enumerate(text):
        if c == '{':
            if depth == 0:
                start = i
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start:i+1])
                except Exception:
                    start = -1
    return None


async def grok_plan(user_message: str, entity_hints: list, history: list) -> dict | None:
    """Grok analyzes user intent and creates execution plan."""
    if not GROK_ENABLED:
        return None
    
    messages = [{"role": "system", "content": GROK_SUPERVISOR_PROMPT}]
    # Add recent history for context (max 4 turns)
    for h in history[-4:]:
        messages.append(h)
    
    hint_str = ", ".join(entity_hints) if entity_hints else "none"
    messages.append({"role": "user", "content": (
        f"[PLAN] User nói: {user_message}\n"
        f"Entities phát hiện: {hint_str}\n"
        f"Phân tích ý user và tạo plan. Trả JSON với format PLAN."
    )})
    
    response = await call_grok(messages, max_tokens=1500, temperature=0.15)
    if not response:
        return None
    
    parsed = _extract_json_from_text(response)
    if parsed and (parsed.get("plan") or parsed.get("action")):
        return parsed
    return None


async def grok_validate(action: dict, result: str, original_request: str) -> dict | None:
    """Grok validates execution result and decides: ok, retry, or fix.
    NEVER skips when result contains errors."""
    if not GROK_ENABLED:
        return None
    
    act_name = action.get("action", "")
    result_str = str(result) if result else ""
    
    # Detect if result contains errors
    has_error = any(marker in result_str for marker in [
        "❌", "⚠️", "Lỗi", "ERROR", "thất bại", "failed", "timeout",
        "Err:", "không mở", "UNKNOWN", "crash"
    ])
    
    # Only skip validation for SUCCESSFUL simple actions (no errors)
    skip_actions = {"chat", "done", "list_folders", "list_profiles", "stop_all",
                    "open_browser", "close_browser", "screenshot"}
    if act_name in skip_actions and not has_error:
        return {"status": "ok", "diagnosis": "Simple action OK"}
    
    # If there's an error, ALWAYS call Grok regardless of action type
    error_context = ""
    if has_error:
        error_context = (
            "\n\n⚠️ KẾT QUẢ CÓ LỖI! Bạn PHẢI:\n"
            "1. Phân tích nguyên nhân GỐC\n"
            "2. Nếu retry được → status=retry + fix action (giảm concurrency, thay params)\n"
            "3. Nếu không retry → status=error + giải thích RÕ cho user\n"
            "4. KHÔNG ĐƯỢC trả status=ok khi có lỗi!"
        )
    
    messages = [{"role": "system", "content": GROK_SUPERVISOR_PROMPT}]
    messages.append({"role": "user", "content": (
        f"[VALIDATE] Đánh giá kết quả:\n"
        f"User yêu cầu: {original_request}\n"
        f"Action: {json.dumps(action, ensure_ascii=False)}\n"
        f"Kết quả:\n{result_str[:3000]}\n"
        f"{error_context}\n"
        f"Trả JSON VALIDATE format."
    )})
    
    response = await call_grok(messages, max_tokens=1200, temperature=0.15)
    if not response:
        # Grok API failed but we have errors → return error status ourselves
        if has_error:
            return {"status": "error", "diagnosis": "Grok API unreachable, manual review needed",
                    "user_message": "⚠️ Kết quả có lỗi nhưng Grok không phân tích được. Xem chi tiết ở trên."}
        return None
    
    parsed = _extract_json_from_text(response)
    if parsed and parsed.get("status"):
        # Safety: if result has errors but Grok says ok, override
        if has_error and parsed.get("status") == "ok":
            logger.warning("Grok said ok but result has errors — forcing retry/error")
            parsed["status"] = "error"
            if not parsed.get("diagnosis"):
                parsed["diagnosis"] = "Result contains errors"
        return parsed
    
    # Grok response not parseable
    if has_error:
        return {"status": "error", "diagnosis": "Grok parse failed, result has errors",
                "user_message": response[:500]}  # At least show Grok's text analysis
    return {"status": "ok", "diagnosis": "Grok response unclear, no errors detected"}


async def grok_handle_error(action: dict, error_result: str, original_request: str,
                            retry_count: int = 0, max_retry: int = 2) -> dict:
    """Grok analyzes ANY error and decides: retry with fix, or explain to user.
    Returns: {"retry": bool, "fix_action": {...} or None, "user_message": "...", "diagnosis": "..."}
    """
    if not GROK_ENABLED:
        return {"retry": False, "fix_action": None, "user_message": error_result, "diagnosis": "Grok disabled"}
    
    can_retry = retry_count < max_retry
    
    # Get past error context for learning
    act_name = action.get("action", "") if isinstance(action, dict) else str(action)
    past_errors = _grok_get_past_errors(act_name)
    
    messages = [{"role": "system", "content": GROK_SUPERVISOR_PROMPT}]
    messages.append({"role": "user", "content": (
        f"[ERROR ANALYSIS] Action thất bại, bạn PHẢI xử lý:\n\n"
        f"User yêu cầu: {original_request}\n"
        f"Action đã chạy: {json.dumps(action, ensure_ascii=False)}\n"
        f"Kết quả LỖI:\n{error_result[:3000]}\n\n"
        f"Retry count: {retry_count}/{max_retry} ({'CÓ THỂ retry' if can_retry else 'HẾT retry rồi'})\n\n"
        f"📝 LỖI TƯƠNG TỰ TRƯỚC ĐÂY (dùng để học hỏi, KHÔNG lặp cách fix đã thất bại):\n{past_errors}\n\n"
        f"BẠN PHẢI trả JSON:\n"
        f'{{"diagnosis": "nguyên nhân gốc", '
        f'"solution": "cách khắc phục", '
        f'"retry_action": {{"action": "...", "params": {{...}}}} hoặc null nếu không retry, '
        f'"user_message": "giải thích ngắn cho user biết đang làm gì"}}\n\n'
        f"Gợi ý fix thường dùng:\n"
        f"- Browser lỗi → giảm concurrency (vd: từ 10 xuống 5, từ 5 xuống 3)\n"
        f"- Timeout → tăng thời gian chờ, giảm batch size\n"
        f"- WebSocket/port → retry lại (lỗi tạm thời)\n"
        f"- Hết retry → giải thích lý do + gợi ý user thử lại sau"
    )})
    
    response = await call_grok(messages, max_tokens=1200, temperature=0.1)
    if not response:
        return {"retry": False, "fix_action": None, "user_message": error_result, "diagnosis": "Grok API failed"}
    
    parsed = _extract_json_from_text(response)
    if parsed:
        result = {
            "retry": bool(parsed.get("retry_action")) and can_retry,
            "fix_action": parsed.get("retry_action") if can_retry else None,
            "user_message": parsed.get("user_message", error_result),
            "diagnosis": parsed.get("diagnosis", "")
        }
        # Remember this error + fix attempt for future learning
        fix_desc = f"retry={result['retry']}, {parsed.get('solution', '')[:100]}"
        _grok_remember_error(act_name, error_result[:200], fix_desc, resolved=False)
        logger.info(f"Grok error analysis: retry={result['retry']}, diagnosis={result['diagnosis'][:100]}")
        return result
    
    # Grok returned text but not JSON → use as user message
    _grok_remember_error(act_name, error_result[:200], "Grok text response", resolved=False)
    return {"retry": False, "fix_action": None, "user_message": response[:500], "diagnosis": "Grok text response"}


async def grok_system_state() -> str:
    """Gather real-time system state info for Grok's awareness."""
    import os as _os
    state_lines = []
    
    # 1. API health
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{FB_MANAGER_API}/health", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    state_lines.append("API: Online")
                else:
                    state_lines.append(f"API: Error status {resp.status}")
    except Exception:
        state_lines.append("API: Offline/Unreachable")
    
    # 2. Recent bot.log errors
    try:
        log_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'bot.log')
        if _os.path.exists(log_path):
            with open(log_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()[-30:]
            errors = [l.strip() for l in lines if 'ERROR' in l or 'CRITICAL' in l]
            if errors:
                state_lines.append(f"Recent log errors ({len(errors)}):")
                for e in errors[-3:]:
                    state_lines.append(f"  {e[:150]}")
            else:
                state_lines.append("Logs: Clean")
    except Exception:
        state_lines.append("Logs: Unreadable")
    
    # 3. Error memory summary
    if _grok_error_memory:
        unresolved = [e for e in _grok_error_memory if not e["resolved"]]
        state_lines.append(f"Error memory: {len(_grok_error_memory)} total, {len(unresolved)} unresolved")
    
    # 4. Batch lock status
    try:
        lock = _get_batch_lock()
        if lock.locked():
            state_lines.append(f"Batch: Locked ({_batch_name})")
        else:
            state_lines.append("Batch: Free")
    except Exception:
        pass
    
    return "\n".join(state_lines)


async def grok_auto_fix(action: dict, error_result: str, original_request: str) -> dict:
    """Grok attempts to fix errors at the CODE level when param retries fail.
    Escalation: param retry → code analysis → code fix → syntax verify → restart.
    Returns: {"fixed": bool, "message": str, "needs_restart": bool}
    """
    if not GROK_ENABLED:
        return {"fixed": False, "message": "Grok disabled", "needs_restart": False}
    
    act_name = action.get("action", "")
    
    # Gather context
    system_state = await grok_system_state()
    past_errors = _grok_get_past_errors(act_name)
    
    # Step 1: Ask Grok if this is a code-level bug
    messages = [{"role": "system", "content": GROK_SUPERVISOR_PROMPT}]
    messages.append({"role": "user", "content": (
        f"[CODE ANALYSIS] Loi DA retry params 2 lan van that bai. Phan tich xem co phai loi CODE khong.\n\n"
        f"Action: {json.dumps(action, ensure_ascii=False)}\n"
        f"Loi: {error_result[:2000]}\n"
        f"System state:\n{system_state}\n"
        f"Lich su loi tuong tu:\n{past_errors}\n\n"
        f"Neu day la loi CODE (bug trong he thong), tra:\n"
        f'{{\"is_code_bug\": true, \"file_to_read\": \"ten file can doc\", \"search_keyword\": \"keyword tim trong file\", \"diagnosis\": \"phan doan ban dau\"}}\n\n'
        f"Neu KHONG PHAI loi code (external: Hidemium down, network, FB block), tra:\n"
        f'{{\"is_code_bug\": false, \"diagnosis\": \"nguyen nhan thuc te\", \"user_message\": \"giai thich cho user\"}}\n\n'
        f"Files trong he thong: telegram_bot.py, openclaw_api.py, skills/login_skill.py, skills/group_skill.py, skills/agent_skill.py"
    )})
    
    response = await call_grok(messages, max_tokens=1000, temperature=0.1)
    if not response:
        return {"fixed": False, "message": "Grok API khong phan hoi", "needs_restart": False}
    
    parsed = _extract_json_from_text(response)
    if not parsed:
        return {"fixed": False, "message": response[:500], "needs_restart": False}
    
    if not parsed.get("is_code_bug"):
        # External issue — just explain to user
        _grok_remember_error(act_name, error_result[:200], "external issue", resolved=False)
        return {
            "fixed": False,
            "message": parsed.get("user_message", parsed.get("diagnosis", "Loi ngoai he thong")),
            "needs_restart": False
        }
    
    # Step 2: Code bug detected — read the relevant file
    import os as _os
    file_to_read = parsed.get("file_to_read", "")
    search_keyword = parsed.get("search_keyword", "")
    
    if not file_to_read:
        return {"fixed": False, "message": "Grok khong xac dinh duoc file can fix", "needs_restart": False}
    
    proj_dir = _os.path.dirname(_os.path.abspath(__file__))
    filepath = _os.path.join(proj_dir, file_to_read)
    if not _os.path.exists(filepath):
        filepath = _os.path.join(proj_dir, "skills", file_to_read)
    if not _os.path.exists(filepath):
        return {"fixed": False, "message": f"File '{file_to_read}' khong tim thay", "needs_restart": False}
    
    # Read relevant code
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            code_content = f.read()
    except Exception as e:
        return {"fixed": False, "message": f"Khong doc duoc file: {e}", "needs_restart": False}
    
    # Find relevant sections
    relevant_code = ""
    if search_keyword:
        code_lines = code_content.split('\n')
        for i, line in enumerate(code_lines):
            keywords = search_keyword.split('|')
            if any(kw.lower() in line.lower() for kw in keywords):
                start = max(0, i - 15)
                end = min(len(code_lines), i + 16)
                section = '\n'.join(f"L{start+j+1}: {code_lines[start+j]}" for j in range(end - start))
                relevant_code += f"\n--- Match at line {i+1} ---\n{section}\n"
                if len(relevant_code) > 4000:
                    break
    if not relevant_code:
        code_lines = code_content.split('\n')[:200]
        relevant_code = '\n'.join(f"L{i+1}: {l}" for i, l in enumerate(code_lines))
    
    # Step 3: Ask Grok to generate the fix
    fix_messages = [{"role": "system", "content": GROK_SUPERVISOR_PROMPT}]
    fix_messages.append({"role": "user", "content": (
        f"[CODE FIX] Ban da phan tich loi va xac dinh day la bug code.\n\n"
        f"Diagnosis: {parsed.get('diagnosis', '')}\n"
        f"File: {file_to_read}\n"
        f"Code lien quan:\n```\n{relevant_code[:5000]}\n```\n\n"
        f"Loi gap phai: {error_result[:1500]}\n\n"
        f"HAY TRA JSON voi code fix:\n"
        f'{{\"old_code\": \"doan code goc CHINH XAC can thay (copy nguyen tu code tren, BO so dong L123:)\", '
        f'\"new_code\": \"code da sua\", '
        f'\"reason\": \"giai thich ngan\", '
        f'\"needs_restart\": true, '
        f'\"confidence\": 0.0-1.0}}\n\n'
        f"QUAN TRONG:\n"
        f"- old_code PHAI trich CHINH XAC tu code hien tai (giu nguyen indent)\n"
        f"- BO so dong (L123:) trong old_code va new_code\n"
        f"- Chi sua phan THUC SU gay loi\n"
        f"- confidence < 0.7 -> KHONG nen ap dung"
    )})
    
    fix_response = await call_grok(fix_messages, max_tokens=2500, temperature=0.1)
    if not fix_response:
        return {"fixed": False, "message": "Grok khong tra code fix", "needs_restart": False}
    
    fix_parsed = _extract_json_from_text(fix_response)
    if not fix_parsed or not fix_parsed.get("old_code") or not fix_parsed.get("new_code"):
        _grok_remember_error(act_name, error_result[:200], "No parseable fix", resolved=False)
        return {"fixed": False, "message": f"Grok phan tich: {fix_response[:500]}", "needs_restart": False}
    
    confidence = float(fix_parsed.get("confidence", 0))
    if confidence < 0.7:
        _grok_remember_error(act_name, error_result[:200], f"Low confidence ({confidence})", resolved=False)
        return {
            "fixed": False,
            "message": (
                f"🧠 **Grok tim thay bug nhung confidence thap ({confidence:.0%})**\n"
                f"📝 {fix_parsed.get('reason', '')}\n"
                f"⚠️ Can review manual."
            ),
            "needs_restart": False
        }
    
    # Step 4: Apply the fix
    old_code = fix_parsed["old_code"]
    new_code = fix_parsed["new_code"]
    
    # Strip line numbers if Grok included them (L123: ...)
    old_code = re.sub(r'^L\d+:\s?', '', old_code, flags=re.MULTILINE)
    new_code = re.sub(r'^L\d+:\s?', '', new_code, flags=re.MULTILINE)
    
    if old_code not in code_content:
        _grok_remember_error(act_name, error_result[:200], "Code match failed", resolved=False)
        return {
            "fixed": False,
            "message": (
                f"🧠 Grok tim thay fix nhung code goc khong match.\n"
                f"📝 Diagnosis: {fix_parsed.get('reason', '')}"
            ),
            "needs_restart": False
        }
    
    # Backup & apply
    try:
        backup_path = filepath + f".bak.{int(_time.time())}"
        with open(backup_path, 'w', encoding='utf-8') as f:
            f.write(code_content)
        
        new_content = code_content.replace(old_code, new_code, 1)
        
        # Verify syntax before saving
        import ast
        try:
            ast.parse(new_content)
        except SyntaxError as e:
            # Rollback!
            _grok_remember_error(act_name, error_result[:200], f"Syntax error in fix: {e}", resolved=False)
            return {
                "fixed": False,
                "message": f"🧠 Grok fix gay syntax error -> da rollback.\nError: {e}",
                "needs_restart": False
            }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        
        _grok_remember_error(act_name, error_result[:200], f"Code fix: {fix_parsed.get('reason', '')}", resolved=True)
        
        needs_restart = fix_parsed.get("needs_restart", False)
        msg_text = (
            f"🧠 **Grok Auto-Fix thanh cong!**\n\n"
            f"📄 File: `{file_to_read}`\n"
            f"🔍 Diagnosis: {fix_parsed.get('reason', '')}\n"
            f"💾 Backup: `{_os.path.basename(backup_path)}`\n"
            f"🔧 Confidence: {confidence:.0%}"
        )
        
        # Auto-restart if needed
        if needs_restart:
            try:
                import subprocess
                # Only restart openclaw_api.py (bot can't restart itself safely)
                if 'openclaw_api' in file_to_read or 'skill' in file_to_read:
                    # Kill existing API process and restart
                    subprocess.Popen(
                        ["python", "openclaw_api.py"],
                        cwd=proj_dir,
                        creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
                    )
                    await asyncio.sleep(3)
                    msg_text += "\n♻️ API server da duoc restart!"
                else:
                    msg_text += "\n⚠️ Can restart bot manual (telegram_bot.py da thay doi)"
            except Exception as e:
                msg_text += f"\n⚠️ Restart failed: {e}"
        else:
            msg_text += "\n✅ Fix ap dung ngay, khong can restart."
        
        return {"fixed": True, "message": msg_text, "needs_restart": needs_restart}
    except Exception as e:
        return {"fixed": False, "message": f"Loi apply fix: {e}", "needs_restart": False}


async def grok_fix_gpt_output(gpt_output: str, user_message: str, entity_hints: list) -> dict | None:
    """Grok fixes incorrect GPT output (wrong action, missing params, etc.)."""
    if not GROK_ENABLED:
        return None
    
    messages = [{"role": "system", "content": GROK_SUPERVISOR_PROMPT}]
    hint_str = ", ".join(entity_hints) if entity_hints else "none"
    messages.append({"role": "user", "content": (
        f"[FIX] GPT parse SAI. Bạn là supervisor, hãy trả JSON ĐÚNG.\n\n"
        f"User nói: {user_message}\n"
        f"Entities: {hint_str}\n"
        f"GPT trả sai: {gpt_output[:800]}\n\n"
        f"Trả JSON: {{\"action\": \"...\", \"params\": {{...}}, \"reply\": \"mô tả\"}}\n"
        f"Nhớ: action phải là 1 trong các action có sẵn. params phải đầy đủ."
    )})
    
    response = await call_grok(messages, max_tokens=800, temperature=0.1)
    if not response:
        return None
    
    parsed = _extract_json_from_text(response)
    if parsed and parsed.get("action") and parsed.get("action") != "chat":
        # Ensure reply exists
        if not parsed.get("reply"):
            parsed["reply"] = f"Grok: {parsed['action']}"
        return parsed
    return None


async def call_fb_api(endpoint: str, method: str = "POST", data: dict = None, max_retries: int = 3, timeout_seconds: int = 0) -> dict:
    """Call FB Manager Pro API with auto-retry on connection errors.
    timeout_seconds: 0 = auto (300s default, longer for batch ops)
    """
    if timeout_seconds <= 0:
        # Auto timeout based on endpoint
        if 'batch' in endpoint or 'nurture' in endpoint:
            # Batch operations: estimate based on profile count
            n_profiles = len((data or {}).get('profiles', (data or {}).get('profile_uuids', [])))
            workers = int((data or {}).get('max_workers', 5))
            # ~30s per profile, divided by workers, with 2x safety margin
            timeout_seconds = max(600, int((n_profiles / max(workers, 1)) * 30 * 2))
        else:
            timeout_seconds = 300
    last_error = None
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{FB_MANAGER_API}{endpoint}"
                if method == "POST":
                    async with session.post(url, json=data or {}, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as resp:
                        if resp.status >= 400:
                            err_text = await resp.text()
                            return {"error": f"HTTP {resp.status}: {err_text[:200]}"}
                        try:
                            return await resp.json()
                        except Exception:
                            text = await resp.text()
                            return {"error": f"Invalid JSON response: {text[:200]}"}
                else:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        if resp.status >= 400:
                            err_text = await resp.text()
                            return {"error": f"HTTP {resp.status}: {err_text[:200]}"}
                        try:
                            return await resp.json()
                        except Exception:
                            text = await resp.text()
                            return {"error": f"Invalid JSON response: {text[:200]}"}
        except (aiohttp.ClientConnectorError, aiohttp.ClientOSError, ConnectionRefusedError) as e:
            last_error = e
            wait = (attempt + 1) * 2  # 2s, 4s, 6s
            logger.warning(f"API connection failed (attempt {attempt+1}/{max_retries}): {e}. Retry in {wait}s...")
            await asyncio.sleep(wait)
        except asyncio.TimeoutError:
            last_error = "Timeout (300s)"
            logger.warning(f"API timeout (attempt {attempt+1}/{max_retries}): {endpoint}")
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"FB API error: {e}")
            return {"error": str(e)}
    # All retries failed — try auto-heal
    error_msg = str(last_error)
    if "Connect" in error_msg or "refused" in error_msg.lower():
        # Auto-heal: try to restart API server (check not already running first)
        logger.warning("🛠️ API down! Attempting auto-restart...")
        try:
            import subprocess, os as _os
            # Check if openclaw_api.py is already running to avoid duplicates
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=5
                )
                if "openclaw_api" in result.stdout.lower():
                    logger.warning("API process already exists, skipping auto-restart")
                    return {"error": f"API server không phản hồi (process exists nhưng không respond). Kiểm tra logs."}
            except Exception:
                pass
            proj_dir = _os.path.dirname(_os.path.abspath(__file__))
            subprocess.Popen(
                ["python", "openclaw_api.py"],
                cwd=proj_dir,
                creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
            )
            await asyncio.sleep(3)
            # Retry one more time after restart
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"{FB_MANAGER_API}{endpoint}"
                    if method == "POST":
                        async with session.post(url, json=data or {}, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                            logger.info("✅ Auto-restart API successful!")
                            return await resp.json()
                    else:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            logger.info("✅ Auto-restart API successful!")
                            return await resp.json()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Auto-restart failed: {e}")
        return {"error": f"API server không phản hồi. Đã thử restart nhưng thất bại. Kiểm tra Hidemium + openclaw_api.py."}
    return {"error": f"Lỗi sau {max_retries} lần retry: {error_msg}"}


def parse_ai_response(text: str) -> dict | list:
    """Extract JSON from AI response, handling markdown code blocks. Returns dict or list of dicts."""
    text = text.strip()
    # Try to find JSON in code blocks
    json_match = re.search(r'```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```', text, re.DOTALL)
    if json_match:
        text = json_match.group(1)
    # Try to find raw JSON array first
    if not json_match:
        array_match = re.search(r'(\[\s*\{.*?\}\s*\])', text, re.DOTALL)
        if array_match:
            text = array_match.group(1)
    # Try to find raw JSON object with brace-counting (handles nested params)
    if not json_match and not text.startswith('['):
        start = text.find('{')
        if start != -1:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                if depth == 0:
                    candidate = text[start:i+1]
                    if '"action"' in candidate:
                        text = candidate
                    break
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed  # Multiple actions
        return parsed  # Single action
    except json.JSONDecodeError:
        logger.warning(f"Failed to parse AI response as JSON: {text[:200]}")
        return {"action": "chat", "params": {}, "reply": text[:500]}


# ============================================================
# Action executors
# ============================================================
async def execute_action(action: dict, telegram_chat_id: str = None, telegram_message_id: int = None) -> str:
    """Execute the parsed action and return result text."""
    act = action.get("action", "chat")
    params = action.get("params", {})
    reply = action.get("reply", "Đang xử lý...")
    profile = params.get("profile", "")
    logger.info(f"🎯 execute_action: act={act} params={params}")

    if act == "chat":
        return reply

    if act == "open_browser":
        if not profile:
            return "❌ Thiếu tên profile. VD: 's10 mở browser'"
        result = await call_fb_api("/open_browser", data={"profile": profile})
        if "error" in result:
            return f"❌ Mở browser {profile} thất bại: {result['error']}"
        return f"✅ Đã mở browser cho `{profile}`"

    if act == "close_browser":
        if not profile:
            return "❌ Thiếu tên profile."
        result = await call_fb_api("/close", data={"profile": profile})
        if "error" in result:
            return f"❌ Đóng browser {profile} thất bại: {result['error']}"
        return f"✅ Đã đóng browser `{profile}`"

    if act == "stop_all":
        # === PHASE 1: Signal cancel + hard-cancel all tracked tasks ===
        _request_cancel()
        tasks_cancelled = _cancel_all_tasks()

        # Also signal API server to cancel any running check_fb_batch
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(f"{FB_MANAGER_API}/cancel_batch",
                             json={}, timeout=aiohttp.ClientTimeout(total=3))
        except Exception:
            pass

        # Wait briefly for tasks to actually finish/cancel
        await asyncio.sleep(2)

        # NOW release batch lock (after tasks are cancelled, not before)
        _release_batch()

        # === PHASE 2: Close ALL running browsers ===
        closed_count = 0
        errors = []
        hidemium_headers = {"Authorization": "Bearer MkkmO8VZiI22dcJIS4qDiYwyNH6JIGC8G9L"}
        running_list = []

        try:
            async with aiohttp.ClientSession() as session:
                # Try Hidemium directly (port 2222)
                try:
                    async with session.get(
                        "http://127.0.0.1:2222/v2/status-profile?is_local=true",
                        headers=hidemium_headers,
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        running = await resp.json()
                    running_list = running.get("content", []) if isinstance(running, dict) else []
                    if not running_list and isinstance(running, list):
                        running_list = running
                except Exception:
                    # Fallback: try our API server at port 8899
                    try:
                        async with session.get(
                            f"{FB_MANAGER_API}/get_running",
                            timeout=aiohttp.ClientTimeout(total=5)
                        ) as resp:
                            data = await resp.json()
                        uuids = data.get("running", [])
                        if uuids:
                            running_list = [{"uuid": u} for u in uuids]
                    except Exception as e2:
                        errors.append(f"Hidemium & API đều không thể kết nối: {str(e2)[:60]}")

                # Close each running profile (with retry)
                for p in running_list:
                    uuid = p.get("uuid", p) if isinstance(p, dict) else str(p)
                    if not uuid:
                        continue
                    for _retry in range(2):
                        try:
                            async with session.get(
                                f"http://127.0.0.1:2222/v1/closeProfile?uuid={uuid}",
                                headers=hidemium_headers,
                                timeout=aiohttp.ClientTimeout(total=10)
                            ) as resp:
                                await resp.read()
                            closed_count += 1
                            break
                        except Exception as e:
                            if _retry == 1:
                                errors.append(f"{uuid[:15]}: {str(e)[:40]}")
                            await asyncio.sleep(0.5)

                # === PHASE 3: Re-scan — catch browsers opened AFTER first scan ===
                await asyncio.sleep(1)
                try:
                    async with session.get(
                        "http://127.0.0.1:2222/v2/status-profile?is_local=true",
                        headers=hidemium_headers,
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        running2 = await resp.json()
                    list2 = running2.get("content", []) if isinstance(running2, dict) else []
                    if not list2 and isinstance(running2, list):
                        list2 = running2
                    already_closed = {(p.get("uuid", p) if isinstance(p, dict) else str(p)) for p in running_list}
                    for p2 in list2:
                        uuid2 = p2.get("uuid", p2) if isinstance(p2, dict) else str(p2)
                        if uuid2 and uuid2 not in already_closed:
                            try:
                                async with session.get(
                                    f"http://127.0.0.1:2222/v1/closeProfile?uuid={uuid2}",
                                    headers=hidemium_headers,
                                    timeout=aiohttp.ClientTimeout(total=10)
                                ) as resp:
                                    await resp.read()
                                closed_count += 1
                            except Exception:
                                pass
                except Exception:
                    pass

            # Build result
            text = f"🛑 **STOP ALL**\n\n"
            text += f"🚫 Đã huỷ **{tasks_cancelled}** tasks đang chạy\n"
            text += f"✅ Đã đóng **{closed_count}** browsers\n"
            if not running_list and not errors:
                text += "ℹ️ Không có browser nào đang chạy\n"
            if errors:
                text += f"\n⚠️ Lỗi ({len(errors)}):\n"
                for e in errors[:5]:
                    text += f"• {e}\n"
            return text
        except Exception as e:
            return f"❌ Stop all thất bại: {str(e)[:200]}"

    if act == "list_folders":
        result = await call_fb_api("/list_folders", data={})
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        folders = result.get("folders", [])
        if not folders:
            return "📁 Không có thư mục nào."
        grand_total = 0
        text = f"📁 **Thư mục ({len(folders)})**\n\n"
        for f in folders:
            name = f.get("name", "?")
            fid = f.get("id", "?")
            # Get REAL profile count by calling list_profiles (not cached total_browser)
            real_count = 0
            try:
                lp = await call_fb_api("/list_profiles", data={"folder_id": str(fid), "page_size": 1})
                if "total" in lp:
                    real_count = lp["total"]
                elif "data" in lp:
                    d = lp["data"]
                    if isinstance(d, dict):
                        real_count = d.get("meta", {}).get("total", len(d.get("content", [])))
                    elif isinstance(d, list):
                        real_count = len(d)
                else:
                    real_count = f.get("total_browser", 0)
            except Exception:
                real_count = f.get("total_browser", 0)
            grand_total += real_count if isinstance(real_count, int) else 0
            text += f"• **{name}** (id={fid}) — {real_count} profiles\n"
        text += f"\n📊 Tổng: **{grand_total}** profiles"
        return text

    if act == "list_tags":
        result = await call_fb_api("/list_tags", data={})
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        tags = result.get("tags", {})
        # tags could be dict or list
        if isinstance(tags, dict):
            items = tags.get("data", tags.get("content", []))
        else:
            items = tags
        if not items:
            return "🏷️ Không có tag nào."
        if isinstance(items, list):
            text = f"🏷️ **Tags ({len(items)})**\n\n"
            for t in items[:30]:
                if isinstance(t, dict):
                    text += f"• `{t.get('name', t.get('tag', '?'))}`\n"
                else:
                    text += f"• `{t}`\n"
            return text
        return f"🏷️ Tags: {items}"

    if act == "list_scripts":
        result = await call_fb_api("/list_scripts", data={})
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        scripts = result.get("scripts", [])
        if not scripts:
            return "📜 Không có script nào."
        text = f"📜 **Scripts ({len(scripts)})**\n\n"
        for s in scripts[:20]:
            name = s.get("name", "?")
            sid = s.get("id", s.get("key", "?"))
            text += f"• **{name}** (id={sid})\n"
        if len(scripts) > 20:
            text += f"... +{len(scripts)-20} scripts khác"
        return text

    if act == "list_campaigns":
        result = await call_fb_api("/list_campaigns", data={})
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        campaigns = result.get("data", result.get("campaigns", []))
        if isinstance(campaigns, dict):
            campaigns = campaigns.get("content", [])
        if not campaigns:
            return "📋 Không có campaign nào."
        text = f"📋 **Campaigns ({len(campaigns)})**\n\n"
        for c in campaigns[:20]:
            name = c.get("name", "?")
            cid = c.get("id", "?")
            text += f"• **{name}** (id={cid})\n"
        return text

    if act == "get_running":
        result = await call_fb_api("/get_running", data={})
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        running = result.get("running", [])
        total = result.get("total", len(running))
        if not running:
            return "🟢 Không có profile nào đang chạy."
        text = f"🟢 **Đang chạy ({total} profiles)**\n\n"
        for uuid in running[:20]:
            text += f"• `{uuid[:12]}...`\n"
        if len(running) > 20:
            text += f"... +{len(running)-20} profiles khác"
        return text

    if act == "get_versions":
        result = await call_fb_api("/get_versions", data={})
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        versions = result.get("versions", [])
        if not versions:
            return "🌐 Không có phiên bản nào."
        text = f"🌐 **Browser versions ({len(versions)})**\n\n"
        for v in versions[:15]:
            if isinstance(v, dict):
                text += f"• `{v.get('version', v.get('name', '?'))}`\n"
            else:
                text += f"• `{v}`\n"
        return text

    if act == "create_profile":
        result = await call_fb_api("/create_profile", data=params)
        if "error" in result:
            return f"❌ Tạo profile thất bại: {result['error']}"
        name = params.get("name", "New")
        return f"✅ Đã tạo profile **{name}**"

    if act == "update_profile_name":
        uuid = params.get("uuid") or params.get("profile_uuid")
        name = params.get("name")
        if not uuid or not name:
            return "❌ Cần uuid và name"
        result = await call_fb_api("/update_profile_name", data={"uuid": uuid, "name": name})
        if "error" in result:
            return f"❌ Đổi tên thất bại: {result['error']}"
        return f"✅ Đã đổi tên → **{name}**"

    if act == "update_profile_note":
        uuid = params.get("uuid") or params.get("profile_uuid")
        note = params.get("note", "")
        if not uuid:
            return "❌ Cần uuid"
        result = await call_fb_api("/update_profile_note", data={"uuid": uuid, "note": note})
        if "error" in result:
            return f"❌ Cập nhật note thất bại: {result['error']}"
        return f"✅ Đã cập nhật note"

    if act == "update_proxy":
        uuid = params.get("uuid") or params.get("profile_uuid")
        ip = params.get("ip") or params.get("host")
        port = params.get("port")
        if not uuid or not ip or not port:
            return "❌ Cần uuid, ip, port"
        result = await call_fb_api("/update_proxy", data={
            "uuid": uuid, "type": params.get("type", "http"),
            "ip": ip, "port": str(port),
            "user": params.get("user", ""), "pass": params.get("pass", "")
        })
        if "error" in result:
            return f"❌ Cập nhật proxy thất bại: {result['error']}"
        return f"✅ Đã cập nhật proxy → {ip}:{port}"

    if act == "remove_proxy":
        uuid = params.get("uuid") or params.get("profile_uuid")
        if not uuid:
            return "❌ Cần uuid"
        result = await call_fb_api("/remove_proxy", data={"uuid": uuid})
        if "error" in result:
            return f"❌ Xóa proxy thất bại: {result['error']}"
        return "✅ Đã xóa proxy"

    if act == "change_fingerprint":
        uuid = params.get("uuid") or params.get("profile_uuid")
        if not uuid:
            return "❌ Cần uuid"
        result = await call_fb_api("/change_fingerprint", data={"uuid": uuid})
        if "error" in result:
            return f"❌ Đổi fingerprint thất bại: {result['error']}"
        return "✅ Đã tạo lại fingerprint"

    if act == "change_status":
        uuid = params.get("uuid") or params.get("profile_uuid")
        status = params.get("status")
        if not uuid or not status:
            return "❌ Cần uuid và status"
        result = await call_fb_api("/change_status", data={"uuid": uuid, "status": status})
        if "error" in result:
            return f"❌ Đổi status thất bại: {result['error']}"
        return f"✅ Đã đổi status → **{status}**"

    if act == "add_to_folder":
        folder_uuid = params.get("folder_uuid") or params.get("folder_id")
        profile_uuids = params.get("profile_uuids", [])
        if not folder_uuid or not profile_uuids:
            return "❌ Cần folder_uuid và profile_uuids"
        result = await call_fb_api("/add_to_folder", data={
            "folder_uuid": folder_uuid, "profile_uuids": profile_uuids
        })
        if "error" in result:
            return f"❌ Thêm vào folder thất bại: {result['error']}"
        return f"✅ Đã thêm {len(profile_uuids)} profiles vào folder"

    if act == "sync_tags":
        uuid = params.get("uuid") or params.get("profile_uuid")
        tags = params.get("tags", [])
        if not uuid:
            return "❌ Cần uuid"
        result = await call_fb_api("/sync_tags", data={"uuid": uuid, "tags": tags})
        if "error" in result:
            return f"❌ Sync tags thất bại: {result['error']}"
        return f"✅ Đã sync tags: {', '.join(tags) if tags else '(xóa hết)'}"

    # ===== CREATE PROFILES: Create N empty profiles in a folder =====
    if act == "create_profiles":
        folder_id = params.get("folder_id", "")
        count = int(params.get("count", 1))
        name_prefix = params.get("name_prefix", "Profile")
        if not folder_id:
            return "❌ Thiếu folder. VD: 'tạo 5 profile fb1'"
        if count < 1:
            count = 1
        if count > 50:
            count = 50  # Safety limit

        # Resolve folder name to get actual folder name for API
        # fb1, fb2, fb3... are folder display names in Hidemium
        folder_name = folder_id  # Pass as-is, Hidemium maps folder_name

        created = []
        errors = []
        for i in range(count):
            # Smart naming: if prefix ends with letter, use prefix+num (s1, s2)
            # If prefix has explicit separator, use that (Profile_ → Profile_1)
            if name_prefix.endswith('_') or name_prefix.endswith('-'):
                profile_name = f"{name_prefix}{i+1}" if count > 1 else name_prefix.rstrip('_-')
            elif len(name_prefix) <= 3 and name_prefix[-1:].isalpha():
                # Short prefix like 's', 'fb' → s1, s2 (no separator)
                profile_name = f"{name_prefix}{i+1}" if count > 1 else name_prefix
            else:
                profile_name = f"{name_prefix}_{i+1}" if count > 1 else name_prefix
            try:
                result = await call_fb_api("/create_profile", data={
                    "name": profile_name,
                    "folder_name": folder_name,
                    "os": "win",
                    "browser": "chrome",
                })
                if "error" in result:
                    errors.append(f"{profile_name}: {result['error']}")
                else:
                    uuid = result.get("uuid", result.get("data", {}).get("uuid", "?"))
                    created.append((profile_name, uuid))
            except Exception as e:
                errors.append(f"{profile_name}: {e}")

            # Update progress
            if telegram_chat_id and telegram_message_id and (i + 1) % 3 == 0:
                try:
                    from telegram import Bot
                    _bot = Bot(token=TELEGRAM_BOT_TOKEN)
                    await _bot.edit_message_text(
                        chat_id=int(telegram_chat_id),
                        message_id=telegram_message_id,
                        text=f"➕ Tạo profile: {i+1}/{count}..."
                    )
                except Exception:
                    pass

        text = f"➕ **Tạo Profile - {folder_id.upper()}**\n\n"
        text += f"✅ Đã tạo: {len(created)}/{count}\n"
        if created:
            for pname, puuid in created:
                text += f"  • `{pname}` ({puuid[:8]}...)\n"
        if errors:
            text += f"\n❌ Lỗi ({len(errors)}):\n"
            for err in errors:
                text += f"  • {err}\n"
        return text

    if act == "list_profiles":
        folder_id = params.get("folder_id")
        # If no folder specified → show summary per folder with REAL counts
        if not folder_id:
            folders_result = await call_fb_api("/list_folders", data={})
            if "error" not in folders_result:
                folders = folders_result.get("folders", [])
                if folders:
                    grand_total = 0
                    text = "📊 **Tổng hợp profiles:**\n\n"
                    for f in folders:
                        name = f.get("name", "?")
                        fid = f.get("id", "?")
                        # Get REAL count
                        real_count = 0
                        try:
                            lp = await call_fb_api("/list_profiles", data={"folder_id": str(fid), "page_size": 1})
                            if "total" in lp:
                                real_count = lp["total"]
                            elif "data" in lp:
                                d = lp["data"]
                                if isinstance(d, dict):
                                    real_count = d.get("meta", {}).get("total", len(d.get("content", [])))
                                elif isinstance(d, list):
                                    real_count = len(d)
                            else:
                                real_count = f.get("total_browser", 0)
                        except Exception:
                            real_count = f.get("total_browser", 0)
                        grand_total += real_count if isinstance(real_count, int) else 0
                        text += f"📁 **{name}**: {real_count} profiles\n"
                    text += f"\n📊 **Tổng: {grand_total} profiles**"
                    text += "\n\n💡 Gõ `fb1`, `fb3`... để xem chi tiết từng folder"
                    return text
        data = {}
        if folder_id:
            data["folder_id"] = folder_id
        result = await call_fb_api("/list_profiles", data=data)
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        profiles = result.get("data", result.get("profiles", []))
        # Hidemium API returns nested: {data: {content: [...], ...}}
        if isinstance(profiles, dict):
            profiles = profiles.get("content", [])
        if not profiles:
            return f"📱 Folder {folder_id}: 0 profiles."
        folder_label = folder_id.upper() if folder_id else "ALL"
        text = f"📱 **{folder_label}** ({len(profiles)} profiles):\n"
        for p in profiles[:30]:
            name = p.get("name", "?")
            note = p.get("note", "")
            status_icon = ""
            if note:
                note_lower = note.lower()
                if "live" in note_lower: status_icon = "✅"
                elif "die" in note_lower: status_icon = "❌"
                elif "lock" in note_lower: status_icon = "🔒"
            text += f"• {status_icon}`{name}`"
            if note:
                text += f" — {note[:30]}"
            text += "\n"
        if len(profiles) > 30:
            text += f"... +{len(profiles)-30} profiles khác"
        return text

    if act == "check_login":
        if not profile:
            return "❌ Thiếu tên profile."
        result = await call_fb_api("/check_login", data={"profile": profile})
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        logged_in = result.get("logged_in", False)
        if logged_in:
            return f"✅ `{profile}` đã login Facebook"
        else:
            return f"❌ `{profile}` chưa login Facebook"

    if act == "debug_groups":
        if not profile:
            return "❌ Thiếu tên profile."
        result = await call_fb_api("/debug_groups", data={"profile": profile})
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        total = result.get("total_groups", 0)
        groups = result.get("groups", [])
        text = f"🔍 **Groups - {profile}**: {total} nhóm\n"
        for i, g in enumerate(groups[:15], 1):
            name = g.get("group_name", "N/A")
            text += f"{i}. `{name}`\n"
        if total > 15:
            text += f"... +{total-15} nhóm khác"
        return text

    if act == "leave_groups":
        if not profile:
            return "❌ Thiếu tên profile."
        data = {"profile": profile}
        # Pass telegram info for live progress updates
        if telegram_chat_id and telegram_message_id:
            data["telegram_chat_id"] = str(telegram_chat_id)
            data["telegram_message_id"] = telegram_message_id
        result = await call_fb_api("/leave_groups", data=data)
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        left = result.get("left", 0)
        failed = result.get("failed", 0)
        skipped = result.get("skipped", 0)
        total = result.get("total_groups", 0)
        text = f"✅ *Thoát nhóm - {profile}*\n"
        text += f"📊 Tổng: {total} | ✅ Thoát: {left} | ❌ Lỗi: {failed} | ⏭ Bỏ qua: {skipped}\n"
        if result.get("screenshot"):
            text += "📸 Screenshot đã gửi\n"
        errors = result.get("errors", [])
        if errors:
            for err in errors[:3]:
                text += f"• {err}\n"
        return text

    if act == "screenshot":
        if not profile:
            return "❌ Thiếu tên profile."
        result = await call_fb_api("/screenshot", data={"profile": profile})
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        return f"📸 Đã chụp `{profile}`: {result.get('filepath', 'OK')}"

    if act == "check_fb_status":
        if not profile:
            return "❌ Thiếu tên profile."
        result = await call_fb_api("/check_fb_status", data={"profile": profile})
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        status = result.get("status", "unknown")
        return f"📊 `{profile}` status: {status}"

    if act == "navigate":
        url = params.get("url", "")
        if not url:
            return "❌ Thiếu URL."
        result = await call_fb_api("/navigate", data={"url": url, "profile": profile})
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        return f"✅ Đã navigate đến {url}"

    if act == "watch_reels":
        if not profile:
            return "❌ Thiếu tên profile."
        count = int(params.get("count", 5))
        do_comment = params.get("comment", True)
        comment_count = int(params.get("comment_count", 3))
        data = {
            "profile": profile,
            "count": count,
            "comment": do_comment,
            "comment_count": comment_count
        }
        if telegram_chat_id and telegram_message_id:
            data["telegram_chat_id"] = str(telegram_chat_id)
            data["telegram_message_id"] = telegram_message_id
        result = await call_fb_api("/watch_reels", data=data)
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        watched = result.get("watched", 0)
        liked = result.get("liked", 0)
        commented = result.get("commented", 0)
        text = f"🎬 *Xem Reels - {profile}*\n\n"
        text += f"👁 Đã xem: {watched}/{count}\n"
        text += f"❤️ Like: {liked}\n"
        text += f"💬 Comment: {commented}\n"
        if result.get("screenshot"):
            text += "📸 Screenshot đã gửi"
        return text

    if act == "batch_check_login":
        busy = await _acquire_batch("batch_check_login")
        if busy:
            return busy
        folder_id = params.get("folder_id")
        concurrency = min(int(params.get("concurrency", 5)), MAX_BATCH_CONCURRENT)
        if not folder_id:
            _release_batch()
            return "❌ Thiếu folder. VD: 'check fb2 bao nhiêu live'"
        # Step 1: Get profiles in folder
        list_result = await call_fb_api("/list_profiles", data={"folder_id": folder_id, "page_size": 500})
        if "error" in list_result:
            _release_batch()
            return f"❌ Lỗi lấy profiles: {list_result['error']}"
        profiles_list = list_result.get("data", [])
        if isinstance(profiles_list, dict):
            profiles_list = profiles_list.get("content", [])
        if not profiles_list or not isinstance(profiles_list, list):
            _release_batch()
            return f"📱 Không tìm thấy profiles nào trong {folder_id}."
        
        # Sort by name for consistent ordering
        profiles_list.sort(key=lambda p: p.get("name", ""))
        total = len(profiles_list)
        profile_names = [p.get("name", "") for p in profiles_list if p.get("name")]
        
        # Step 2: Call /check_fb_batch — REAL parallel execution
        # This opens N browsers simultaneously, navigates to FB, checks status, closes
        if telegram_chat_id and telegram_message_id:
            try:
                async with aiohttp.ClientSession() as s:
                    await s.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                        json={"chat_id": telegram_chat_id, "message_id": telegram_message_id,
                              "text": f"🔍 Đang check {total} profiles ({concurrency} luồng song song)...\nMở browser → navigate FB → check status → đóng"}
                    )
            except Exception:
                pass
        
        # Call the batch API with Telegram progress info for live updates
        batch_result = await call_fb_api("/check_fb_batch", data={
            "profiles": profile_names,
            "max_workers": concurrency,
            "close_after": True,
            "telegram_chat_id": telegram_chat_id,
            "telegram_message_id": telegram_message_id,
            "telegram_token": TELEGRAM_BOT_TOKEN
        })
        
        if "error" in batch_result:
            _release_batch()
            err = batch_result['error']
            if 'timeout' in str(err).lower() or 'Timeout' in str(err):
                return f"❌ Batch check timeout! {total} profiles quá nhiều cho timeout hiện tại.\n💡 Thử giảm số profiles hoặc tăng concurrency."
            return f"❌ Lỗi batch check: {err}"
        
        # Parse results into categories
        results = batch_result.get("results", [])
        summary = batch_result.get("summary", {})
        workers = batch_result.get("workers", concurrency)
        
        # Merge function: parse results into live/die/locked/error lists
        def _parse_batch_results(results_list):
            _live, _die, _locked, _error, _error_names = [], [], [], [], []
            for r in results_list:
                name = r.get("name", r.get("profile_uuid", "?")[:20])
                status = r.get("status", "ERROR")
                detail = r.get("detail", "")
                if status == "LIVE":
                    _live.append(f"{name} ({detail})" if detail and detail != status else name)
                elif status in ("DIE", "NOT_LOGGED_IN"):
                    _die.append(name)
                elif status in ("LOCKED", "2FA"):
                    _locked.append(f"{name} ({status})")
                else:
                    _error.append(f"{name}: {detail}" if detail else f"{name}: {status}")
                    _error_names.append(name)
            return _live, _die, _locked, _error, _error_names
        
        live_list, die_list, locked_list, error_list, error_names = _parse_batch_results(results)
        
        # ===== AUTO-RETRY: Nếu có lỗi, tự động retry tối đa 2 lần =====
        MAX_RETRY = 2
        retry_round = 0
        while error_names and retry_round < MAX_RETRY and not _is_cancelled():
            retry_round += 1
            retry_count = len(error_names)
            logger.info(f"[BATCH-RETRY] Round {retry_round}: retrying {retry_count} error profiles")
            
            # Notify user about retry
            if telegram_chat_id and telegram_message_id:
                try:
                    async with aiohttp.ClientSession() as s:
                        await s.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                            json={"chat_id": telegram_chat_id, "message_id": telegram_message_id,
                                  "text": f"🔄 Auto-retry lần {retry_round}/{MAX_RETRY}: {retry_count} profiles lỗi...\n"
                                          f"✅ Live: {len(live_list)}  ❌ Die: {len(die_list)}  🔒 Lock: {len(locked_list)}\n"
                                          f"⏳ Đợi 5s rồi retry..."}
                        )
                except Exception:
                    pass
            
            # Wait a bit before retry (browser cleanup time)
            await asyncio.sleep(5)
            
            # Retry only the error profiles
            retry_result = await call_fb_api("/check_fb_batch", data={
                "profiles": error_names,
                "max_workers": min(concurrency, len(error_names)),
                "close_after": True,
                "telegram_chat_id": telegram_chat_id,
                "telegram_message_id": telegram_message_id,
                "telegram_token": TELEGRAM_BOT_TOKEN
            })
            
            if "error" in retry_result:
                logger.warning(f"[BATCH-RETRY] Round {retry_round} API error: {retry_result['error']}")
                break  # Stop retrying if API itself fails
            
            retry_results = retry_result.get("results", [])
            r_live, r_die, r_locked, r_error, r_error_names = _parse_batch_results(retry_results)
            
            # Merge retry successes into main lists
            live_list.extend(r_live)
            die_list.extend(r_die)
            locked_list.extend(r_locked)
            
            # Only keep still-failing as errors
            error_list = r_error
            error_names = r_error_names
            
            fixed = retry_count - len(r_error_names)
            logger.info(f"[BATCH-RETRY] Round {retry_round}: fixed {fixed}/{retry_count}, still error: {len(r_error_names)}")
        
        # Group remaining errors by reason
        error_reasons = {}
        for e in error_list:
            # e is "name: detail"
            parts = e.split(": ", 1)
            detail = parts[1] if len(parts) > 1 else parts[0]
            reason = _classify_error(detail)
            error_reasons[reason] = error_reasons.get(reason, 0) + 1
        
        # Check if not all profiles were processed (timeout/crash)
        checked = len(live_list) + len(die_list) + len(locked_list) + len(error_list)
        not_checked = total - checked
        
        # Build report text
        text = f"📊 **Check Login - {folder_id}** ({total} profiles, {workers} luồng)\n\n"
        text += f"✅ Live: {len(live_list)}\n"
        text += f"❌ Die: {len(die_list)}\n"
        if locked_list:
            text += f"🔒 Locked/2FA: {len(locked_list)}\n"
        if error_list:
            text += f"⚠️ Lỗi (sau {retry_round}x retry): {len(error_list)}\n"
        if not_checked > 0:
            text += f"🚫 Chưa check: {not_checked}\n"
        if retry_round > 0:
            text += f"🔄 Đã auto-retry {retry_round} lần\n"
        text += f"\n"
        if live_list:
            text += f"**✅ Live ({len(live_list)}):**\n"
            for n in live_list[:30]:
                text += f"• `{n}`\n"
            if len(live_list) > 30:
                text += f"... +{len(live_list)-30} khác\n"
        if die_list:
            text += f"\n**❌ Die ({len(die_list)}):**\n"
            for n in die_list[:30]:
                text += f"• `{n}`\n"
            if len(die_list) > 30:
                text += f"... +{len(die_list)-30} khác\n"
        if locked_list:
            text += f"\n**🔒 Locked/2FA ({len(locked_list)}):**\n"
            for n in locked_list[:10]:
                text += f"• `{n}`\n"
        if error_list:
            text += f"\n**⚠️ Lỗi ({len(error_list)}):**\n"
            # Show grouped error reasons first
            if error_reasons:
                for reason, count in sorted(error_reasons.items(), key=lambda x: -x[1]):
                    text += f"  → {reason}: {count}x\n"
                text += f"\n"
            for e in error_list[:15]:
                text += f"• `{e}`\n"
            if len(error_list) > 15:
                text += f"... +{len(error_list)-15} khác\n"
        
        # Suggest next action
        if die_list:
            text += f"\n💡 Gõ **'xóa die {folder_id}'** để xóa {len(die_list)} profiles die"
        if error_list:
            text += f"\n💡 Gõ **'check lại {folder_id}'** để retry {len(error_list)} profiles lỗi"
        _release_batch()
        return text

    # ===== DELETE DIE PROFILES =====
    if act == "delete_die_all":
        # Delete die profiles across ALL folders
        busy = await _acquire_batch("delete_die_all")
        if busy:
            return busy
        concurrency = min(int(params.get("concurrency", 5)), MAX_BATCH_CONCURRENT)
        # Get all folders
        folders_result = await call_fb_api("/list_folders", data={})
        if "error" in folders_result:
            _release_batch()
            return f"❌ Lỗi: {folders_result['error']}"
        folders = folders_result.get("folders", [])
        if not folders:
            _release_batch()
            return "📁 Không có thư mục nào."

        total_text = f"🗑️ **Xóa DIE toàn bộ ({len(folders)} thư mục)**\n\n"
        grand_live = 0
        grand_die = 0
        grand_deleted = 0
        grand_locked = 0

        for fi, folder in enumerate(folders):
            # Check cancel
            if _is_cancelled():
                total_text += f"\n🛑 **Đã dừng bởi STOP ALL** (sau {fi}/{len(folders)} folders)\n"
                break

            fname = folder.get("name", "?")
            fid = folder.get("id", "")

            # Always fetch real profile count (total_browser is often stale)
            quick_check = await call_fb_api("/list_profiles", data={"folder_id": str(fid), "page_size": 1})
            if "error" in quick_check:
                total_text += f"📁 **{fname}**: ⚠️ Lỗi: {quick_check['error'][:40]}\n"
                continue
            quick_data = quick_check.get("data", {})
            if isinstance(quick_data, dict):
                ftotal = quick_data.get("total", quick_data.get("totalElements", 0))
            elif isinstance(quick_data, list):
                ftotal = len(quick_data)
            else:
                ftotal = 0
            if ftotal == 0:
                total_text += f"📁 **{fname}**: 0 profiles (bỏ qua)\n"
                continue

            # Update progress
            if telegram_chat_id and telegram_message_id:
                try:
                    async with aiohttp.ClientSession() as s:
                        await s.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                            json={"chat_id": telegram_chat_id, "message_id": telegram_message_id,
                                  "text": f"🔍 [{fi+1}/{len(folders)}] Đang check {fname} ({ftotal} profiles)..."}
                        )
                except Exception:
                    pass

            # Get profiles in this folder (with retry on SQLITE_ERROR)
            list_result = await call_fb_api("/list_profiles", data={"folder_id": str(fid), "page_size": 500})
            if "error" in list_result:
                # Retry once after short delay (SQLITE_ERROR can be transient)
                await asyncio.sleep(2)
                list_result = await call_fb_api("/list_profiles", data={"folder_id": str(fid), "page_size": 500})
            if "error" in list_result:
                total_text += f"📁 **{fname}**: ⚠️ Lỗi: {list_result['error'][:60]}\n"
                continue
            profiles_list = list_result.get("data", [])
            if isinstance(profiles_list, dict):
                profiles_list = profiles_list.get("content", [])
            if not profiles_list:
                total_text += f"📁 **{fname}**: ⚠️ API trả về 0 (nhưng folder có {ftotal})\n"
                continue

            profile_names = [p.get("name", "") for p in profiles_list if p.get("name")]

            # Check status
            batch_result = await call_fb_api("/check_fb_batch", data={
                "profiles": profile_names,
                "max_workers": concurrency,
                "close_after": True,
                "telegram_chat_id": telegram_chat_id,
                "telegram_message_id": telegram_message_id,
                "telegram_token": TELEGRAM_BOT_TOKEN
            })

            if "error" in batch_result:
                total_text += f"📁 **{fname}**: ⚠️ Check lỗi: {batch_result['error'][:60]}\n"
                continue

            results = batch_result.get("results", [])
            die_uuids = []
            live = 0
            locked = 0
            for r in results:
                st = r.get("status", "")
                if st in ("DIE", "NOT_LOGGED_IN"):
                    die_uuids.append(r.get("profile_uuid", ""))
                elif st == "LIVE":
                    live += 1
                elif st in ("LOCKED", "2FA"):
                    locked += 1

            # Delete die
            deleted = 0
            if die_uuids:
                del_result = await call_fb_api("/delete_profiles", data={"uuids": die_uuids})
                deleted = del_result.get("deleted", len(die_uuids))

            grand_live += live
            grand_die += len(die_uuids)
            grand_deleted += deleted
            grand_locked += locked
            total_text += f"📁 **{fname}**: ✅{live} 🔒{locked} ❌{len(die_uuids)}"
            if deleted:
                total_text += f" 🗑️{deleted}"
            total_text += "\n"

        total_text += f"\n📊 **Tổng kết:**\n"
        total_text += f"✅ Live: {grand_live} | 🔒 Lock: {grand_locked}\n"
        total_text += f"❌ Die: {grand_die} | 🗑️ Đã xóa: {grand_deleted}"
        _release_batch()
        return total_text

    if act == "verify_status":
        # Smart contextual answer: actually check current state via API
        topic = params.get("topic")  # "delete", "check", "count", or None
        v_folder = params.get("folder_id")
        v_profile = params.get("profile")
        last_result = params.get("last_result", "")
        question = params.get("original_question", "")
        
        # Case 1: Question about a folder ("fb3 xóa hết chưa?")
        if v_folder:
            # Actually call API to check CURRENT state
            try:
                lp = await call_fb_api("/list_profiles", data={"folder_id": v_folder, "page_size": 1})
                current_count = 0
                if "total" in lp:
                    current_count = lp["total"]
                elif "data" in lp:
                    d = lp["data"]
                    if isinstance(d, dict):
                        current_count = d.get("meta", {}).get("total", len(d.get("content", [])))
                    elif isinstance(d, list):
                        current_count = len(d)
            except Exception:
                current_count = -1  # Error
            
            if topic == "delete":
                if current_count == 0:
                    return f"✅ Đã xóa sạch {v_folder} rồi bác! Hiện tại còn 0 profiles."
                elif current_count > 0:
                    auto_action = params.get("auto_action")
                    if auto_action == "delete_all":
                        # Auto-execute: delete ALL without asking
                        sub = {"action": "delete_all_profiles", "params": {"folder_id": v_folder}}
                        del_result = await execute_action(sub, telegram_chat_id=telegram_chat_id, telegram_message_id=telegram_message_id)
                        return f"⚠️ {v_folder} còn {current_count} profiles → Xóa hết!\n\n{del_result}"
                    elif auto_action == "delete_die":
                        # Auto-execute: delete DIE only
                        sub = {"action": "delete_die_profiles", "params": {"folder_id": v_folder, "concurrency": 10}}
                        del_result = await execute_action(sub, telegram_chat_id=telegram_chat_id, telegram_message_id=telegram_message_id)
                        return f"⚠️ {v_folder} còn {current_count} profiles → Xóa die!\n\n{del_result}"
                    else:
                        return f"⚠️ Chưa hết bác ơi, {v_folder} vẫn còn {current_count} profiles. Bác muốn xóa tiếp không?"
                else:
                    return f"❌ Không kiểm tra được {v_folder}. Thử lại sau bac nhé."
            elif topic == "check":
                if current_count >= 0:
                    return f"📊 {v_folder} hiện tại có {current_count} profiles."
                else:
                    return f"❌ Lỗi khi kiểm tra {v_folder}."
            else:
                # Generic question about folder
                if current_count >= 0:
                    return f"📊 {v_folder}: {current_count} profiles hiện tại."
                # Fallback to last result
                if last_result:
                    return f"Kết quả gần nhất {v_folder}:\n{last_result[:300]}"
                return f"Không check được {v_folder}. Bác thử gõ 'check {v_folder}' xem."
        
        # Case 2: Question about a profile ("S10 sao rồi?")
        if v_profile:
            if last_result:
                return f"Kết quả {v_profile} lần trước:\n{last_result[:300]}"
            return f"Chưa có thông tin về {v_profile}. Bác muốn check {v_profile} không?"
        
        # Case 3: No specific target — check all folders
        if topic == "delete" or topic == "count":
            try:
                folders_result = await call_fb_api("/list_folders", data={})
                folders = folders_result.get("folders", [])
                if folders:
                    text = "📊 Tình trạng hiện tại:\n\n"
                    grand = 0
                    for f in folders:
                        fname = f.get("name", "?")
                        fid = f.get("id", "?")
                        try:
                            lp2 = await call_fb_api("/list_profiles", data={"folder_id": str(fid), "page_size": 1})
                            cnt = 0
                            if "total" in lp2:
                                cnt = lp2["total"]
                            elif "data" in lp2:
                                d2 = lp2["data"]
                                if isinstance(d2, dict):
                                    cnt = d2.get("meta", {}).get("total", len(d2.get("content", [])))
                                elif isinstance(d2, list):
                                    cnt = len(d2)
                        except Exception:
                            cnt = f.get("total_browser", 0)
                        grand += cnt if isinstance(cnt, int) else 0
                        text += f"• **{fname}** — {cnt} profiles\n"
                    text += f"\n📊 Tổng: **{grand}** profiles"
                    return text
            except Exception:
                pass
        
        # Fallback: show last result from history
        if last_result:
            return f"Kết quả gần nhất:\n{last_result[:300]}"
        return "Chưa có kết quả nào trước đó bác ơi. Bác muốn check gì?"

    if act == "delete_all_profiles":
        folder_id = params.get("folder_id")
        if not folder_id:
            return "❌ Thiếu folder. VD: 'xóa hết profile fb3'"

        # Step 1: Get all profiles in folder
        list_result = await call_fb_api("/list_profiles", data={"folder_id": folder_id, "page_size": 500})
        if "error" in list_result:
            return f"❌ Lỗi: {list_result['error']}"
        profiles_list = list_result.get("data", [])
        if isinstance(profiles_list, dict):
            profiles_list = profiles_list.get("content", [])
        if not profiles_list:
            return f"📱 Không có profile nào trong {folder_id}."

        total = len(profiles_list)
        all_uuids = [p.get("uuid") or p.get("profile_uuid") for p in profiles_list if p.get("uuid") or p.get("profile_uuid")]
        all_names = [p.get("name", "?") for p in profiles_list]

        if not all_uuids:
            return f"❌ Không lấy được UUID từ profiles trong {folder_id}."

        # Step 2: Update progress
        if telegram_chat_id and telegram_message_id:
            try:
                async with aiohttp.ClientSession() as s:
                    await s.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                        json={"chat_id": telegram_chat_id, "message_id": telegram_message_id,
                              "text": f"🗑️ Đang xóa TẤT CẢ {total} profiles trong {folder_id}..."}
                    )
            except Exception:
                pass

        # Step 3: Delete all (batch, 50 at a time to avoid API limits)
        deleted_total = 0
        errors = []
        for i in range(0, len(all_uuids), 50):
            batch = all_uuids[i:i+50]
            try:
                del_result = await call_fb_api("/delete_profiles", data={"uuids": batch})
                if "error" in del_result:
                    errors.append(f"Batch {i//50+1}: {del_result['error']}")
                else:
                    deleted_total += del_result.get("deleted", len(batch))
            except Exception as e:
                errors.append(f"Batch {i//50+1}: {e}")

        # Wait for Hidemium to finalize delete (it's async internally)
        await asyncio.sleep(2)

        # Auto-verify: check remaining count after deletion (with retry)
        remaining = -1
        for _retry in range(3):
            try:
                verify = await call_fb_api("/list_profiles", data={"folder_id": folder_id, "page_size": 1})
                if "total" in verify:
                    remaining = verify["total"]
                elif "data" in verify:
                    d = verify["data"]
                    if isinstance(d, dict):
                        remaining = d.get("meta", {}).get("total", len(d.get("content", [])))
                    elif isinstance(d, list):
                        remaining = len(d)
                if remaining >= 0 and remaining < total:
                    break
                await asyncio.sleep(2)
            except Exception:
                pass

        text = f"🗑️ **Xóa TẤT CẢ profiles - {folder_id}**\n\n"
        text += f"📊 Trước: {total} profiles\n"
        text += f"🗑️ **Đã xóa: {deleted_total}**\n"
        if errors:
            text += f"\n⚠️ Lỗi:\n"
            for err in errors[:5]:
                text += f"• {err}\n"
        # Self-monitoring: report actual remaining
        if remaining == 0:
            text += f"\n✅ Xóa sạch {folder_id}! Còn 0 profiles."
        elif remaining > 0:
            text += f"\n⚠️ Vẫn còn {remaining} profiles trong {folder_id}."
        elif deleted_total > 0:
            text += f"\n✅ Xóa sạch {folder_id}!"
        return text

    if act == "delete_die_profiles":
        busy = await _acquire_batch("delete_die_profiles")
        if busy:
            return busy
        folder_id = params.get("folder_id")
        if not folder_id:
            _release_batch()
            return "❌ Thiếu folder. VD: 'xóa die fb1'"
        concurrency = min(int(params.get("concurrency", 5)), MAX_BATCH_CONCURRENT)

        # Step 1: Get profiles
        list_result = await call_fb_api("/list_profiles", data={"folder_id": folder_id, "page_size": 500})
        if "error" in list_result:
            _release_batch()
            return f"❌ Lỗi: {list_result['error']}"
        profiles_list = list_result.get("data", [])
        if isinstance(profiles_list, dict):
            profiles_list = profiles_list.get("content", [])
        if not profiles_list:
            _release_batch()
            return f"📱 Không tìm thấy profiles nào trong {folder_id}."

        # Sort by name for consistent ordering
        profiles_list.sort(key=lambda p: p.get("name", ""))
        total = len(profiles_list)
        profile_names = [p.get("name", "") for p in profiles_list if p.get("name")]

        # Step 2: Check login status
        if telegram_chat_id and telegram_message_id:
            try:
                async with aiohttp.ClientSession() as s:
                    await s.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                        json={"chat_id": telegram_chat_id, "message_id": telegram_message_id,
                              "text": f"🔍 Đang check {total} profiles trong {folder_id}...\nSau đó sẽ xóa profiles DIE"}
                    )
            except Exception:
                pass

        batch_result = await call_fb_api("/check_fb_batch", data={
            "profiles": profile_names,
            "max_workers": concurrency,
            "close_after": True,
            "telegram_chat_id": telegram_chat_id,
            "telegram_message_id": telegram_message_id,
            "telegram_token": TELEGRAM_BOT_TOKEN
        })

        if "error" in batch_result:
            _release_batch()
            return f"❌ Lỗi check: {batch_result['error']}"

        results = batch_result.get("results", [])

        # Collect die profiles + track errors for retry
        die_uuids = []
        die_names = []
        live_count = 0
        locked_count = 0
        error_names_dd = []
        for r in results:
            status = r.get("status", "")
            name = r.get("name", r.get("profile_uuid", "?")[:20])
            uuid = r.get("profile_uuid", "")
            if status in ("DIE", "NOT_LOGGED_IN"):
                die_uuids.append(uuid)
                die_names.append(name)
            elif status == "LIVE":
                live_count += 1
            elif status in ("LOCKED", "2FA"):
                locked_count += 1
            else:
                error_names_dd.append(name)
        
        # Auto-retry error profiles (max 2 rounds)
        retry_round_dd = 0
        while error_names_dd and retry_round_dd < 2 and not _is_cancelled():
            retry_round_dd += 1
            logger.info(f"[DELETE-DIE-RETRY] Round {retry_round_dd}: retrying {len(error_names_dd)} error profiles")
            if telegram_chat_id and telegram_message_id:
                try:
                    async with aiohttp.ClientSession() as s:
                        await s.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                            json={"chat_id": telegram_chat_id, "message_id": telegram_message_id,
                                  "text": f"🔄 Retry lần {retry_round_dd}: {len(error_names_dd)} profiles lỗi...\n⏳ Đợi 5s..."}
                        )
                except Exception:
                    pass
            await asyncio.sleep(5)
            retry_r = await call_fb_api("/check_fb_batch", data={
                "profiles": error_names_dd,
                "max_workers": min(concurrency, len(error_names_dd)),
                "close_after": True,
                "telegram_chat_id": telegram_chat_id,
                "telegram_message_id": telegram_message_id,
                "telegram_token": TELEGRAM_BOT_TOKEN
            })
            if "error" in retry_r:
                break
            new_errors = []
            for r in retry_r.get("results", []):
                status = r.get("status", "")
                name = r.get("name", r.get("profile_uuid", "?")[:20])
                uuid = r.get("profile_uuid", "")
                if status in ("DIE", "NOT_LOGGED_IN"):
                    die_uuids.append(uuid)
                    die_names.append(name)
                elif status == "LIVE":
                    live_count += 1
                elif status in ("LOCKED", "2FA"):
                    locked_count += 1
                else:
                    new_errors.append(name)
            error_names_dd = new_errors

        if not die_uuids:
            _release_batch()
            return (f"📊 {folder_id}: {total} profiles\n"
                    f"✅ Live: {live_count} | 🔒 Lock: {locked_count}\n"
                    f"Không có profile DIE nào để xóa!")

        # Step 3: Delete die profiles
        if telegram_chat_id and telegram_message_id:
            try:
                async with aiohttp.ClientSession() as s:
                    await s.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                        json={"chat_id": telegram_chat_id, "message_id": telegram_message_id,
                              "text": f"🗑️ Đang xóa {len(die_uuids)} profiles DIE..."}
                    )
            except Exception:
                pass

        del_result = await call_fb_api("/delete_profiles", data={"uuids": die_uuids})
        deleted = del_result.get("deleted", 0)

        # Wait for Hidemium to finalize delete (it's async internally)
        await asyncio.sleep(2)

        # Auto-verify: check remaining count after deletion (with retry)
        remaining = -1
        for _retry in range(3):
            try:
                verify = await call_fb_api("/list_profiles", data={"folder_id": folder_id, "page_size": 1})
                if "total" in verify:
                    remaining = verify["total"]
                elif "data" in verify:
                    d = verify["data"]
                    if isinstance(d, dict):
                        remaining = d.get("meta", {}).get("total", len(d.get("content", [])))
                    elif isinstance(d, list):
                        remaining = len(d)
                # If remaining decreased or is 0, trust it
                if remaining >= 0 and remaining < total:
                    break
                # Otherwise wait and retry (Hidemium may not have flushed yet)
                await asyncio.sleep(2)
            except Exception:
                pass

        text = f"🗑️ **Xóa DIE - {folder_id}**\n\n"
        text += f"📊 Trước: {total} profiles\n"
        text += f"✅ Live: {live_count}\n"
        if locked_count:
            text += f"🔒 Locked: {locked_count}\n"
        text += f"❌ Die: {len(die_names)}\n\n"
        text += f"🗑️ **Đã xóa {deleted} profiles:**\n"
        for n in die_names[:20]:
            text += f"• `{n}`\n"
        if len(die_names) > 20:
            text += f"... +{len(die_names)-20} khác\n"
        # Self-monitoring: report actual remaining
        if remaining >= 0:
            text += f"\n📊 **Còn lại: {remaining} profiles** trong {folder_id}"
        _release_batch()
        return text

    # ===== FEED =====
    if act == "fb_read_feed":
        if not profile:
            return "❌ Thiếu tên profile."
        scroll_count = int(params.get("scroll_count", 3))
        result = await call_fb_api("/fb_read_feed", data={"profile": profile, "scroll_count": scroll_count})
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        posts = result.get("posts", [])
        text = f"📰 *Feed - {profile}* ({len(posts)} bài)\n\n"
        for i, p in enumerate(posts[:10]):
            author = p.get("author", "?")
            content = p.get("content", "")[:80]
            text += f"{i}. *{author}*: {content}\n"
        if len(posts) > 10:
            text += f"... +{len(posts)-10} bài khác"
        return text

    if act == "fb_comment":
        if not profile:
            return "❌ Thiếu tên profile."
        post_index = params.get("post_index", 0)
        comment_text = params.get("comment", "")
        if not comment_text:
            return "❌ Thiếu nội dung comment."
        result = await call_fb_api("/fb_comment", data={
            "profile": profile, "post_index": int(post_index), "comment": comment_text
        })
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        return f"✅ Đã comment bài #{post_index}: \"{comment_text[:50]}\""

    if act == "fb_nurture_batch":
        profiles_list = params.get("profiles", [])
        max_workers = int(params.get("max_workers", 3))
        comments_per = int(params.get("comments_per_profile", 2))
        if not profiles_list:
            return "❌ Thiếu danh sách profiles. VD: ['S10', 'S11']"
        result = await call_fb_api("/fb_nurture_batch", data={
            "profiles": profiles_list,
            "max_workers": max_workers,
            "comments_per_profile": comments_per
        })
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        summary = result.get("summary", {})
        text = f"🌱 *Nuôi hàng loạt* ({len(profiles_list)} profiles)\n\n"
        text += f"✅ Thành công: {summary.get('success', 0)}\n"
        text += f"❌ Lỗi: {summary.get('failed', 0)}\n"
        text += f"💬 Tổng comment: {summary.get('total_comments', 0)}"
        return text

    # ===== LOGIN =====
    if act == "login_fb":
        if not profile:
            return "❌ Thiếu tên profile."
        fb_id = params.get("fb_id") or params.get("uid", "")
        password = params.get("password") or params.get("pass", "")
        if not fb_id or not password:
            return "❌ Thiếu uid hoặc password."
        result = await call_fb_api("/login_fb", data={
            "profile": profile, "fb_id": fb_id, "password": password, "close_after": False
        })
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        status = result.get("status", "unknown")
        return f"🔐 Login {profile}: {status}"

    # ===== BULK LOGIN: Map UID/pass list to profiles in folder =====
    if act == "bulk_login":
        busy = await _acquire_batch("bulk_login")
        if busy:
            return busy
        folder_id = params.get("folder_id", "")
        accounts_raw = params.get("accounts", "")  # "UID PASS\nUID PASS\n..." or list
        count = params.get("count", 0)  # 0 = all accounts
        concurrency = min(int(params.get("concurrency", 5)), MAX_BATCH_CONCURRENT)
        close_after = params.get("close_after", True)
        auto_create = params.get("auto_create", True)  # Auto-create profiles if folder empty
        delete_die = params.get("delete_die", False)  # Auto-delete die profiles after login
        name_prefix = params.get("name_prefix", "s")  # Profile naming: s1, s2, s3...

        if not folder_id:
            _release_batch()
            return "❌ Thiếu folder. VD: 'login fb1 với danh sách uid'"
        if not accounts_raw:
            _release_batch()
            return "❌ Thiếu danh sách UID/password. Gửi dạng:\nUID1 PASS1\nUID2 PASS2"

        # Parse accounts: handle both string and list input from AI
        accounts = []
        if isinstance(accounts_raw, list):
            # AI sent as list: [["uid","pwd"], ...] or [{"uid":..., "password":...}, ...] or ["uid pwd", ...]
            for item in accounts_raw:
                if isinstance(item, dict):
                    uid = str(item.get("uid", item.get("fb_id", ""))).strip()
                    pwd = str(item.get("password", item.get("pass", item.get("pwd", "")))).strip()
                    if uid and pwd:
                        accounts.append({"uid": uid, "password": pwd})
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    uid, pwd = str(item[0]).strip(), str(item[1]).strip()
                    if uid and pwd:
                        accounts.append({"uid": uid, "password": pwd})
                elif isinstance(item, str):
                    parts = item.strip().split(None, 1)
                    if len(parts) == 2:
                        accounts.append({"uid": parts[0].strip(), "password": parts[1].strip()})
        else:
            # String input: "UID PASS\nUID PASS\n..."
            raw_str = str(accounts_raw)
            for line in raw_str.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2:
                    uid, pwd = parts[0].strip(), parts[1].strip()
                    if uid and pwd:
                        accounts.append({"uid": uid, "password": pwd})

        if not accounts:
            _release_batch()
            return "❌ Không parse được account nào. Định dạng: UID PASSWORD (mỗi dòng 1 account)"

        # Limit count
        if count and count > 0:
            accounts = accounts[:int(count)]

        # Get profiles from folder
        list_result = await call_fb_api("/list_profiles", data={"folder_id": folder_id, "page_size": 500})
        if "error" in list_result:
            _release_batch()
            return f"❌ Lỗi lấy profiles: {list_result['error']}"
        profiles_list = list_result.get("data", [])
        if isinstance(profiles_list, dict):
            profiles_list = profiles_list.get("content", [])
        
        # AUTO-CREATE: If folder is empty or not enough profiles, create them
        if (not profiles_list or len(profiles_list) < len(accounts)) and auto_create:
            needed = len(accounts) - len(profiles_list)
            logger.info(f"bulk_login: Auto-creating {needed} profiles in {folder_id} (prefix={name_prefix})")
            if telegram_chat_id and telegram_message_id:
                try:
                    from telegram import Bot
                    _bot = Bot(token=TELEGRAM_BOT_TOKEN)
                    await _bot.edit_message_text(
                        chat_id=int(telegram_chat_id),
                        message_id=telegram_message_id,
                        text=f"➕ Đang tạo {needed} profiles trong {folder_id}..."
                    )
                except Exception:
                    pass
            start_idx = len(profiles_list) + 1
            for i in range(needed):
                pname = f"{name_prefix}{start_idx + i}"  # s1, s2, s3... (no underscore)
                try:
                    cr = await call_fb_api("/create_profile", data={
                        "name": pname,
                        "folder_name": folder_id,
                        "os": "win",
                        "browser": "chrome",
                    })
                    if "error" not in cr:
                        new_uuid = cr.get("uuid", cr.get("data", {}).get("uuid", ""))
                        profiles_list.append({"name": pname, "uuid": new_uuid})
                        logger.info(f"Auto-created {pname} (uuid={str(new_uuid)[:20]})")
                    else:
                        logger.warning(f"Auto-create {pname} failed: {cr['error']}")
                except Exception as e:
                    logger.warning(f"Auto-create {pname} error: {e}")
                # Progress update every 10
                if (i + 1) % 10 == 0 and telegram_chat_id and telegram_message_id:
                    try:
                        from telegram import Bot
                        _bot = Bot(token=TELEGRAM_BOT_TOKEN)
                        await _bot.edit_message_text(
                            chat_id=int(telegram_chat_id),
                            message_id=telegram_message_id,
                            text=f"➕ Tạo profiles: {i+1}/{needed}..."
                        )
                    except Exception:
                        pass
        
        if not profiles_list:
            _release_batch()
            return f"❌ Folder {folder_id} không có profile nào và tạo tự động thất bại."

        # Sort profiles by name (Hidemium returns newest-first, we need Profile_1 first)
        profiles_list.sort(key=lambda p: p.get("name", ""))

        # Map accounts to profiles by order (1st account → 1st profile, etc.)
        pairs = min(len(accounts), len(profiles_list))
        if pairs == 0:
            _release_batch()
            return "❌ Không có account hoặc profile nào để login."

        text_header = f"🔐 **Bulk Login - {folder_id.upper()}**\n"
        text_header += f"📊 {pairs} accounts → {pairs} profiles (concurrency={concurrency})\n\n"

        # Clear any previous cancel signal (race-safe)
        my_gen = _cancel_generation
        _clear_cancel_safe(my_gen)

        # Progress update helper
        async def _update_progress(done, total, results_map):
            if telegram_chat_id and telegram_message_id:
                try:
                    from telegram import Bot
                    _bot = Bot(token=TELEGRAM_BOT_TOKEN)
                    lines = [f"🔐 Bulk Login {folder_id.upper()}: {done}/{total}"]
                    for pn, (icon, desc) in list(results_map.items())[-8:]:
                        lines.append(f"{icon} {pn}: {desc[:60]}")
                    if done < total:
                        lines.append(f"\n⏳ Còn {total - done}...")
                    await _bot.edit_message_text(
                        chat_id=int(telegram_chat_id),
                        message_id=telegram_message_id,
                        text="\n".join(lines)
                    )
                except Exception:
                    pass

        # Run logins with concurrency control
        sem = asyncio.Semaphore(concurrency)
        results_map = {}

        async def login_one(idx):
            if _is_cancelled():
                return  # Aborted by stop_all
            async with sem:
                if _is_cancelled():
                    return
                acct = accounts[idx]
                prof = profiles_list[idx]
                pname = prof.get("name", f"Profile_{idx+1}")
                puuid = prof.get("uuid", "")
                uid = acct["uid"]
                pwd = acct["password"]
                logger.info(f"bulk_login [{idx+1}/{pairs}] {pname} (uuid={puuid[:20]}...) → UID={uid}")
                try:
                    result = await call_fb_api("/login_fb", data={
                        "profile": puuid or pname,
                        "fb_id": uid,
                        "password": pwd,
                        "close_after": close_after
                    })
                    logger.info(f"bulk_login [{idx+1}] {pname} result: {result.get('status', result.get('error', 'unknown'))}")
                    if "error" in result:
                        results_map[pname] = ("❌", f"UID {uid}: {result['error'][:60]}")
                    else:
                        status = result.get("status", "unknown")
                        if status in ("LIVE", "LOGGED_IN"):
                            results_map[pname] = ("✅", f"UID {uid}: {status}")
                        elif "DIE" in status or "DISABLED" in status:
                            results_map[pname] = ("💀", f"UID {uid}: {status}")
                        elif "LOCKED" in status or "CHECKPOINT" in status or "2FA" in status:
                            results_map[pname] = ("🔒", f"UID {uid}: {status}")
                        else:
                            results_map[pname] = ("⚠️", f"UID {uid}: {status}")
                except Exception as e:
                    results_map[pname] = ("❌", f"UID {uid}: {str(e)[:60]}")

                await _update_progress(len(results_map), pairs, results_map)

        # Run logins — track task for stop_all cancellation
        _gather_task = asyncio.ensure_future(
            asyncio.gather(*[login_one(i) for i in range(pairs)], return_exceptions=True)
        )
        _register_task(_gather_task)
        try:
            await _gather_task
        except asyncio.CancelledError:
            logger.info("bulk_login: gather CANCELLED by stop_all")

        cancelled = _is_cancelled()
        # Build final report
        live = sum(1 for _, (i, _) in results_map.items() if i == "✅")
        die = sum(1 for _, (i, _) in results_map.items() if i == "💀")
        locked = sum(1 for _, (i, _) in results_map.items() if i == "🔒")
        err = sum(1 for _, (i, _) in results_map.items() if i in ("❌", "⚠️"))

        text = text_header
        for idx in range(pairs):
            pname = profiles_list[idx].get("name", f"Profile_{idx+1}")
            if pname in results_map:
                icon, desc = results_map[pname]
                text += f"{icon} {escape_md(pname)}: {escape_md(desc)}\n"

        text += f"\n📊 **Kết quả:** {live}✅ Live | {die}💀 Die | {locked}🔒 Locked | {err}⚠️ Lỗi"
        if cancelled:
            skipped = pairs - len(results_map)
            text += f"\n🛑 **Đã huỷ bởi STOP ALL** — bỏ qua {skipped} profiles"
        if len(accounts) > pairs:
            text += f"\n⚠️ Còn {len(accounts) - pairs} accounts chưa dùng (hết profile)"
        
        # AUTO-DELETE DIE: Remove profiles that died during login
        if delete_die and die > 0:
            die_uuids = []
            for idx_d in range(pairs):
                pname_d = profiles_list[idx_d].get("name", "")
                puuid_d = profiles_list[idx_d].get("uuid", "")
                if pname_d in results_map and results_map[pname_d][0] == "💀" and puuid_d:
                    die_uuids.append(puuid_d)
            if die_uuids:
                try:
                    del_result = await call_fb_api("/delete_profiles", data={"uuids": die_uuids})
                    if "error" not in del_result:
                        text += f"\n🗑️ **Đã xóa {len(die_uuids)} profile die**"
                        logger.info(f"Auto-deleted {len(die_uuids)} die profiles")
                    else:
                        text += f"\n⚠️ Xóa die lỗi: {del_result['error'][:60]}"
                except Exception as e:
                    text += f"\n⚠️ Xóa die lỗi: {e}"
        
        _release_batch()
        return text

    # ===== VISION =====
    if act == "vision_capture":
        if not profile:
            return "❌ Thiếu tên profile."
        result = await call_fb_api("/vision_capture", data={"profile": profile})
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        w = result.get("width", "?")
        h = result.get("height", "?")
        return f"📸 Đã chụp vision {profile} ({w}x{h})"

    if act == "vision_click":
        if not profile:
            return "❌ Thiếu tên profile."
        target = params.get("target", "")
        if not target:
            return "❌ Thiếu mô tả element cần click."
        result = await call_fb_api("/vision_click", data={
            "profile": profile, "target": target, "max_retries": int(params.get("max_retries", 2))
        })
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        method = result.get("method", "?")
        return f"✅ Đã click '{target}' (method: {method})"

    if act == "vision_type":
        if not profile:
            return "❌ Thiếu tên profile."
        target = params.get("target", "")
        text_to_type = params.get("text", "")
        if not target or not text_to_type:
            return "❌ Thiếu target hoặc text."
        result = await call_fb_api("/vision_type", data={
            "profile": profile, "target": target, "text": text_to_type
        })
        if "error" in result:
            return f"❌ Lỗi: {result['error']}"
        return f"✅ Đã gõ '{text_to_type[:30]}' vào '{target}'"

    # ===== CHECK DUPLICATES: Find duplicate FB accounts across profiles in a folder =====
    if act == "check_duplicates":
        busy = await _acquire_batch("check_duplicates")
        if busy:
            return busy
        folder_id = params.get("folder_id", "")
        concurrency = min(int(params.get("concurrency", 3)), MAX_BATCH_CONCURRENT)
        if not folder_id:
            _release_batch()
            return "❌ Thiếu folder_id (fb1, fb2...)"
        
        # Step 1: Get all profiles in folder
        folder_result = await call_fb_api("/list_profiles", data={"folder_id": folder_id})
        profiles = folder_result.get("data", folder_result.get("profiles", []))
        if isinstance(profiles, dict):
            profiles = profiles.get("content", [])
        if not profiles:
            _release_batch()
            return f"❌ Folder {folder_id} không có profiles nào."
        profile_names = [p.get("name", p.get("uuid", "?")) for p in profiles]
        
        # Step 2: Update status
        if telegram_chat_id and telegram_message_id:
            try:
                from telegram import Bot
                _bot = Bot(token=TELEGRAM_BOT_TOKEN)
                await _bot.edit_message_text(
                    chat_id=int(telegram_chat_id),
                    message_id=telegram_message_id,
                    text=f"🔍 Check trùng lặp: {len(profile_names)} profiles\n📁 Folder: {folder_id}\n\n⏳ Đang mở từng profile lấy FB UID..."
                )
            except Exception:
                pass
        
        # Step 3: Call check_fb_batch (which now returns fb_uid from c_user cookie)
        batch_result = await call_fb_api("/check_fb_batch", data={
            "profiles": profile_names,
            "max_workers": concurrency,
            "close_after": True,
            "telegram_chat_id": str(telegram_chat_id) if telegram_chat_id else "",
            "telegram_message_id": telegram_message_id,
            "telegram_token": TELEGRAM_BOT_TOKEN
        })
        
        results = batch_result.get("results", [])
        
        # Step 4: Extract UIDs and find duplicates
        uid_map = {}  # fb_uid → [profile_names]
        no_uid = []   # profiles without UID (not logged in, die, etc.)
        status_map = {}  # profile → status
        
        for r in results:
            pname = r.get("name", r.get("profile_uuid", "?"))
            # Try to match back to profile name
            for p in profiles:
                if p.get("uuid") == r.get("profile_uuid") or p.get("name") == r.get("profile_uuid"):
                    pname = p.get("name", pname)
                    break
            
            status = r.get("status", "ERROR")
            fb_uid = r.get("fb_uid", "")
            status_map[pname] = status
            
            if fb_uid:
                if fb_uid not in uid_map:
                    uid_map[fb_uid] = []
                uid_map[fb_uid].append(pname)
            else:
                no_uid.append((pname, status))
        
        # Step 5: Build report
        text = f"🔍 **Check Trùng Lặp - {folder_id.upper()}**\n"
        text += f"📊 Tổng: {len(profile_names)} profiles\n\n"
        
        # Find actual duplicates
        duplicates = {uid: names for uid, names in uid_map.items() if len(names) > 1}
        
        if duplicates:
            text += f"🔴 **TRÙNG LẶP: {len(duplicates)} FB account dùng chung!**\n\n"
            for uid, names in duplicates.items():
                text += f"⚠️ **UID {uid}** → {', '.join(names)} ({len(names)} profiles)\n"
            text += "\n"
        else:
            text += "✅ **Không có trùng lặp!**\n\n"
        
        # Show all UIDs
        unique_uids = {uid: names for uid, names in uid_map.items() if len(names) == 1}
        if unique_uids:
            text += f"📋 **UIDs riêng biệt** ({len(unique_uids)}):\n"
            for uid, names in unique_uids.items():
                text += f"• {names[0]}: `{uid}`\n"
            text += "\n"
        
        # Show profiles without UID
        if no_uid:
            text += f"⚠️ **Không lấy được UID** ({len(no_uid)}):\n"
            for pname, status in no_uid:
                text += f"• {pname}: {status}\n"
        
        text += f"\n📊 **Tóm tắt:** {len(uid_map)} unique UIDs, {len(duplicates)} trùng, {len(no_uid)} không có UID"
        _release_batch()
        return text

    # ===== DELETE DUPLICATES: Remove duplicate profiles keeping one per UID =====
    if act == "delete_duplicates":
        busy = await _acquire_batch("delete_duplicates")
        if busy:
            return busy
        folder_id = params.get("folder_id", "")
        concurrency = min(int(params.get("concurrency", 3)), MAX_BATCH_CONCURRENT)
        keep = params.get("keep", "first")  # "first" = keep first profile per UID
        if not folder_id:
            _release_batch()
            return "❌ Thiếu folder_id (fb1, fb2...)"

        # Step 1: Get all profiles in folder
        folder_result = await call_fb_api("/list_profiles", data={"folder_id": folder_id})
        profiles = folder_result.get("data", folder_result.get("profiles", []))
        if isinstance(profiles, dict):
            profiles = profiles.get("content", [])
        if not profiles:
            _release_batch()
            return f"❌ Folder {folder_id} không có profiles nào."
        profile_names = [p.get("name", p.get("uuid", "?")) for p in profiles]

        # Step 2: Update status
        if telegram_chat_id and telegram_message_id:
            try:
                from telegram import Bot
                _bot = Bot(token=TELEGRAM_BOT_TOKEN)
                await _bot.edit_message_text(
                    chat_id=int(telegram_chat_id),
                    message_id=telegram_message_id,
                    text=f"🔍 Tìm trùng lặp: {len(profile_names)} profiles\n📁 Folder: {folder_id}\n\n⏳ Đang mở từng profile lấy FB UID..."
                )
            except Exception:
                pass

        # Step 3: Check all profiles to get FB UIDs
        batch_result = await call_fb_api("/check_fb_batch", data={
            "profiles": profile_names,
            "max_workers": concurrency,
            "close_after": True,
            "telegram_chat_id": str(telegram_chat_id) if telegram_chat_id else "",
            "telegram_message_id": telegram_message_id,
            "telegram_token": TELEGRAM_BOT_TOKEN
        })

        results = batch_result.get("results", [])

        # Step 4: Build UID → profiles map (preserving order)
        uid_map = {}  # fb_uid → [(profile_name, profile_uuid)]
        for r in results:
            profile_uuid = r.get("profile_uuid", "")
            fb_uid = r.get("fb_uid", "")
            # Find profile name
            pname = profile_uuid
            for p in profiles:
                if p.get("uuid") == profile_uuid or p.get("name") == profile_uuid:
                    pname = p.get("name", profile_uuid)
                    break
            if fb_uid:
                if fb_uid not in uid_map:
                    uid_map[fb_uid] = []
                uid_map[fb_uid].append((pname, profile_uuid))

        # Step 5: Identify duplicates to delete (keep first of each group)
        to_delete = []  # (profile_name, profile_uuid, fb_uid)
        to_keep = []    # (profile_name, fb_uid)
        for fb_uid, group in uid_map.items():
            if len(group) > 1:
                # Keep first, delete rest
                to_keep.append((group[0][0], fb_uid))
                for pname, puuid in group[1:]:
                    to_delete.append((pname, puuid, fb_uid))

        if not to_delete:
            _release_batch()
            return f"✅ **{folder_id}**: Không có profile trùng nào cần xóa!"

        # Step 6: Update status before deleting
        if telegram_chat_id and telegram_message_id:
            try:
                from telegram import Bot
                _bot = Bot(token=TELEGRAM_BOT_TOKEN)
                await _bot.edit_message_text(
                    chat_id=int(telegram_chat_id),
                    message_id=telegram_message_id,
                    text=f"🗑️ Tìm thấy {len(to_delete)} profiles trùng → Đang xóa..."
                )
            except Exception:
                pass

        # Step 7: Delete duplicate profiles (by UUID)
        delete_uuids = [d[1] for d in to_delete]
        del_result = await call_fb_api("/delete_profiles", data={"uuids": delete_uuids})
        deleted = del_result.get("deleted", 0)

        # Step 8: Build report
        text = f"🗑️ **Xóa Trùng Lặp - {folder_id.upper()}**\n\n"
        text += f"📊 Tổng profiles: {len(profile_names)}\n"
        text += f"🔴 Trùng: {len(to_delete)} profiles\n"
        text += f"🗑️ Đã xóa: {deleted}\n\n"

        # Show what was kept vs deleted
        for fb_uid, group in uid_map.items():
            if len(group) > 1:
                kept = group[0][0]
                removed = [g[0] for g in group[1:]]
                text += f"🆔 UID {fb_uid}:\n"
                text += f"  ✅ Giữ: {kept}\n"
                text += f"  🗑️ Xóa: {', '.join(removed)}\n\n"

        remaining = len(profile_names) - deleted
        text += f"📊 **Còn lại: {remaining} profiles (unique)**"
        _release_batch()
        return text

    # ===== BATCH AGENT: Run task on ALL profiles in a folder =====
    if act == "batch_agent_execute":
        busy = await _acquire_batch("batch_agent_execute")
        if busy:
            return busy
        folder_id = params.get("folder_id", "")
        task = params.get("task", "")
        concurrency = min(int(params.get("concurrency", 3)), MAX_BATCH_CONCURRENT)
        if not task:
            _release_batch()
            return "❌ Thiếu mô tả task."
        if not folder_id:
            _release_batch()
            return "❌ Thiếu folder_id (fb1, fb2...)"
        
        # Step 1: Get all profiles in folder
        folder_result = await call_fb_api("/list_profiles", data={"folder_id": folder_id})
        profiles = folder_result.get("data", folder_result.get("profiles", []))
        # Hidemium API returns nested: {data: {content: [...], ...}}
        if isinstance(profiles, dict):
            profiles = profiles.get("content", [])
        if not profiles:
            _release_batch()
            return f"❌ Folder {folder_id} không có profiles nào."
        profile_names = [p.get("name", p.get("uuid", "?")) for p in profiles]
        
        # Step 2: Update status message
        if telegram_chat_id and telegram_message_id:
            try:
                from telegram import Bot
                _bot = Bot(token=TELEGRAM_BOT_TOKEN)
                await _bot.edit_message_text(
                    chat_id=int(telegram_chat_id),
                    message_id=telegram_message_id,
                    text=f"🚀 Batch Agent: {len(profile_names)} profiles\n📋 Task: {task[:60]}\n\n⏳ Đang chạy (max {concurrency} luồng)..."
                )
            except Exception:
                pass
        
        # Step 3: Run agent on each profile with concurrency control
        my_gen = _cancel_generation
        _clear_cancel_safe(my_gen)
        sem = asyncio.Semaphore(concurrency)
        results_map = {}
        
        async def run_one(pname):
            if _is_cancelled():
                return
            async with sem:
                if _is_cancelled():
                    return
                try:
                    data = {"profile": pname, "task": task}
                    result = await call_fb_api("/agent_execute", data=data)
                    steps = result.get("steps", [])
                    last = steps[-1] if steps else {}
                    last_desc = last.get("desc", "")
                    if last_desc.startswith("DONE"):
                        results_map[pname] = ("✅", last_desc[6:].strip())
                    elif last_desc.startswith("FAILED"):
                        results_map[pname] = ("❌", last_desc[8:].strip())
                    else:
                        # Try to extract useful info from last step
                        results_map[pname] = ("⚠️", last_desc[:100] if last_desc else f"{len(steps)} steps")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    results_map[pname] = ("❌", str(e)[:80])
                
                # Progress update
                if telegram_chat_id and telegram_message_id:
                    done = len(results_map)
                    total = len(profile_names)
                    try:
                        progress_lines = [f"🚀 Batch Agent: {total} profiles | Task: {task[:40]}"]
                        progress_lines.append(f"📊 Tiến độ: {done}/{total}\n")
                        for pn, (icon, desc) in list(results_map.items())[-8:]:
                            progress_lines.append(f"{icon} {pn}: {desc[:60]}")
                        if done < total:
                            progress_lines.append(f"\n⏳ Còn {total - done} profiles...")
                        from telegram import Bot
                        _bot = Bot(token=TELEGRAM_BOT_TOKEN)
                        await _bot.edit_message_text(
                            chat_id=int(telegram_chat_id),
                            message_id=telegram_message_id,
                            text="\n".join(progress_lines)
                        )
                    except Exception:
                        pass
        
        # Track task for stop_all cancellation
        _gather_task = asyncio.ensure_future(
            asyncio.gather(*[run_one(p) for p in profile_names], return_exceptions=True)
        )
        _register_task(_gather_task)
        try:
            await _gather_task
        except asyncio.CancelledError:
            logger.info("batch_agent_execute: gather CANCELLED by stop_all")
        
        cancelled = _is_cancelled()
        # Step 4: Build final report
        text = f"🚀 **Batch Agent Done**\n📋 Task: {task[:60]}\n📁 Folder: {folder_id} ({len(profile_names)} profiles)\n\n"
        success_count = sum(1 for _, (icon, _) in results_map.items() if icon == "✅")
        fail_count = sum(1 for _, (icon, _) in results_map.items() if icon == "❌")
        
        for pname in profile_names:
            if pname in results_map:
                icon, desc = results_map[pname]
                text += f"{icon} **{pname}**: {desc[:80]}\n"
        
        text += f"\n📊 Tổng: {success_count}✅ {fail_count}❌ / {len(profile_names)} profiles"
        if cancelled:
            skipped = len(profile_names) - len(results_map)
            text += f"\n🛑 **Đã huỷ bởi STOP ALL** — bỏ qua {skipped} profiles"
        
        # Step 5: Check for duplicates if task involves checking IDs
        if any(kw in task.lower() for kw in ['id', 'trùng', 'duplicate', 'trung']):
            # Try to extract IDs from results and find duplicates
            id_map = {}
            for pname, (icon, desc) in results_map.items():
                # Try to find numeric ID in desc
                import re as _re
                id_match = _re.search(r'(\d{10,20})', desc)
                if id_match:
                    fb_id = id_match.group(1)
                    if fb_id not in id_map:
                        id_map[fb_id] = []
                    id_map[fb_id].append(pname)
            
            duplicates = {k: v for k, v in id_map.items() if len(v) > 1}
            if duplicates:
                text += f"\n\n⚠️ **TRÙNG LẶP ID:**\n"
                for fb_id, pnames in duplicates.items():
                    text += f"🔴 ID {fb_id}: {', '.join(pnames)}\n"
            elif id_map:
                text += f"\n\n✅ **Không có ID trùng lặp** ({len(id_map)} IDs khác nhau)"
            else:
                text += f"\n\n⚠️ Không trích xuất được ID từ kết quả"
        
        _release_batch()
        return text

    # ===== AGENT (catch-all for unknown tasks) =====
    if act == "agent_execute":
        if not profile:
            return "❌ Thiếu tên profile."
        task = params.get("task", "")
        if not task:
            return "❌ Thiếu mô tả task."
        data = {"profile": profile, "task": task}
        if telegram_chat_id and telegram_message_id:
            data["telegram_chat_id"] = str(telegram_chat_id)
            data["telegram_message_id"] = telegram_message_id
        # Agent can take 5+ minutes - long timeout, NO retry on timeout
        result = await call_fb_api("/agent_execute", data=data, max_retries=1, timeout_seconds=900)
        if "error" in result and not result.get("steps"):
            return f"❌ Lỗi: {result['error']}"
        steps = result.get("steps", [])
        total = len(steps)
        success = sum(1 for s in steps if s.get("ok"))
        last = steps[-1] if steps else {}
        last_desc = last.get("desc", "")
        text = f"🤖 *Agent - {profile}*\n"
        text += f"📋 Task: {task[:60]}\n\n"
        if last_desc.startswith("DONE"):
            text += f"✅ {last_desc[6:]}\n"
        elif last_desc.startswith("FAILED"):
            text += f"❌ {last_desc[8:]}\n"
        else:
            text += f"⚠️ {total} bước đã thực hiện\n"
        text += f"\n📊 {success}✅ / {total} steps"
        if result.get("used_skill"):
            text += f"\n🧠 Đã dùng skill đã học"
        if result.get("duration"):
            text += f" • {result['duration']}s"
        return text

    # ===== AGENT SKILLS LIST =====
    if act == "agent_skills":
        result = await call_fb_api("/agent_skills", data={})
        skills = result.get("skills", [])
        if not skills:
            return "🧠 Agent chưa học được skill nào."
        text = f"🧠 *Agent Skills ({len(skills)})*\n\n"
        for s in skills[:15]:
            name = s.get("name", "?")[:40]
            sc = s.get("success_count", 0)
            fc = s.get("fail_count", 0)
            steps = s.get("total_steps", 0)
            text += f"#{s.get('id', '?')} *{name}*\n"
            text += f"  ✅{sc} ❌{fc} • {steps} bước\n"
        return text

    # ===== AGENT TASK HISTORY =====
    if act == "agent_task_history":
        limit = params.get("limit", 10)
        result = await call_fb_api("/agent_task_history", data={"limit": limit})
        history = result.get("history", [])
        if not history:
            return "📜 Chưa có lịch sử agent."
        text = f"📜 *Lịch sử Agent ({len(history)})*\n\n"
        for h in history[:10]:
            icon = "✅" if h.get("success") else "❌"
            task = h.get("task_text", "")[:35]
            steps = h.get("total_steps", 0)
            dur = h.get("duration_seconds", 0)
            skill = h.get("skill_name", "")
            text += f"{icon} {task}\n"
            text += f"  {steps} bước • {dur:.0f}s"
            if skill:
                text += f" • 🧠 {skill[:20]}"
            text += "\n"
        return text

    # ===== DEV MODE: Developer AI agent =====
    if act == "dev_check_health":
        # Check all services: API server, Hidemium, Telegram connectivity
        health = []
        # Check API server
        try:
            api_check = await call_fb_api("/health", method="GET", max_retries=1)
            if api_check.get("status") == "ok":
                health.append("✅ API Server (port 8899): OK")
            else:
                health.append(f"❌ API Server: {api_check}")
        except Exception as e:
            health.append(f"❌ API Server: {e}")
        # Check Hidemium (non-blocking)
        try:
            async with aiohttp.ClientSession() as _hsession:
                async with _hsession.post(
                    "http://127.0.0.1:2222/v1/browser/list?is_local=true",
                    json={"page": 1, "limit": 1},
                    headers={"Authorization": "Bearer MkkmO8VZiI22dcJIS4qDiYwyNH6JIGC8G9L"},
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        health.append("✅ Hidemium (port 2222): OK")
                    else:
                        health.append(f"❌ Hidemium: HTTP {resp.status}")
        except Exception as e:
            health.append(f"❌ Hidemium: {e}")
        # Check folders exist
        try:
            folders = await call_fb_api("/list_folders", data={}, max_retries=1)
            total_folders = len(folders.get("folders", []))
            health.append(f"📁 Folders: {total_folders}")
        except Exception:
            health.append("⚠️ Folders: không kiểm tra được")
        # Check Pollinations AI
        try:
            test_msg = [{"role": "user", "content": "say OK"}]
            ai_test = await call_pollinations(test_msg, max_tokens=5, temperature=0)
            if ai_test:
                health.append(f"✅ AI (Pollinations): OK")
            else:
                health.append("❌ AI (Pollinations): không phản hồi")
        except Exception as e:
            health.append(f"❌ AI: {e}")
        return "🏥 **Health Check**\n\n" + "\n".join(health)
    
    if act == "dev_read_code":
        import os as _os
        target_file = params.get("file", "telegram_bot.py")
        search_term = params.get("search", "")
        proj_dir = _os.path.dirname(_os.path.abspath(__file__))
        filepath = _os.path.join(proj_dir, target_file)
        if not _os.path.exists(filepath):
            # Try in skills/
            filepath = _os.path.join(proj_dir, "skills", target_file)
        if not _os.path.exists(filepath):
            # List all available .py files including skills/
            all_files = [f for f in _os.listdir(proj_dir) if f.endswith('.py')]
            skills_dir = _os.path.join(proj_dir, "skills")
            if _os.path.isdir(skills_dir):
                all_files += [f"skills/{f}" for f in _os.listdir(skills_dir) if f.endswith('.py')]
            return f"❌ File '{target_file}' không tìm thấy. Các file có: " + ", ".join(all_files)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            total_lines = len(lines)
            if search_term:
                # Split search by | for OR matching, or use as single term
                search_parts = [s.strip().lower() for s in search_term.split('|') if s.strip()]
                if not search_parts:
                    search_parts = [search_term.lower()]
                # Find lines matching ANY search term
                matches = []
                for i, line in enumerate(lines, 1):
                    line_lower = line.lower()
                    if any(sp in line_lower for sp in search_parts):
                        # Show context: 3 lines before, 5 after
                        start = max(0, i - 4)
                        end = min(total_lines, i + 5)
                        snippet = "".join(lines[start:end])
                        matches.append(f"**Line {i}:**\n```python\n{snippet.rstrip()}\n```")
                        if len(matches) >= 8:
                            break
                if matches:
                    return f"📄 **{target_file}** ({total_lines} lines) | Tìm '{search_term}' ({len(matches)} kết quả):\n\n" + "\n\n".join(matches)
                return f"📄 **{target_file}** ({total_lines} lines) | Không tìm thấy '{search_term}'"
            else:
                # Show first 50 lines as overview
                preview = "".join(lines[:50])
                return f"📄 **{target_file}** ({total_lines} lines):\n```python\n{preview[:2000]}\n```"
        except Exception as e:
            return f"❌ Lỗi đọc file: {e}"
    
    if act == "dev_diagnose":
        import os as _os, traceback as _tb
        error_desc = params.get("error", "")
        target_file = params.get("file", "")
        proj_dir = _os.path.dirname(_os.path.abspath(__file__))
        
        # Step 1: Gather system info
        diag_info = []
        # Check services
        try:
            api_check = await call_fb_api("/health", method="GET", max_retries=1)
            diag_info.append(f"API Server: {'OK' if api_check.get('status') == 'ok' else 'FAIL'}")
        except Exception:
            diag_info.append("API Server: DOWN")
        
        # Step 2: If file mentioned, read relevant code
        code_context = ""
        if target_file:
            filepath = _os.path.join(proj_dir, target_file)
            if not _os.path.exists(filepath):
                filepath = _os.path.join(proj_dir, "skills", target_file)
            if _os.path.exists(filepath):
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()
                    # Extract error-related sections
                    if error_desc:
                        for keyword in error_desc.split()[:5]:
                            if len(keyword) > 3:
                                for i, line in enumerate(content.split('\n'), 1):
                                    if keyword.lower() in line.lower():
                                        start = max(0, i - 5)
                                        end = min(len(content.split('\n')), i + 5)
                                        code_context += f"\nFile {target_file} line {i}:\n" + "\n".join(content.split('\n')[start:end]) + "\n"
                                        break
                except Exception:
                    pass
        
        # Step 3: Ask AI to analyze with full context
        diag_messages = [{"role": "system", "content": (
            "Bạn là senior developer. Phân tích lỗi NGẮN GỌN:\n"
            "1. Root cause (1-2 câu)\n"
            "2. Cách sửa cụ thể (code snippet nếu cần)\n"
            "3. Phòng tránh (1 câu)\n"
            "TỐI ĐA 8-10 câu. Tiếng Việt. KHÔNG lặp lại error message. KHÔNG reasoning dài dòng."
        )}]
        diag_messages.append({"role": "user", "content": (
            f"Lỗi: {error_desc}\n"
            f"System: {'; '.join(diag_info)}\n"
            f"Code:\n{code_context[:1500]}\n"
            f"Project: FB Manager Pro (Hidemium port 2222, API port 8899)\n"
            f"Phân tích ngắn gọn."
        )})
        try:
            diagnosis = await call_ai(diag_messages, max_tokens=500, temperature=0.3)
            return f"🔍 **Chẩn đoán lỗi**\n\n🏥 Services: {' | '.join(diag_info)}\n\n🧠 **Phân tích:**\n{diagnosis}"
        except Exception as e:
            return f"🔍 Services: {' | '.join(diag_info)}\n❌ AI phân tích thất bại: {e}"
    
    if act == "dev_run_cmd":
        import subprocess, os as _os
        command = params.get("command", "")
        cmd_type = params.get("type", "shell")
        if not command:
            return "❌ Thiếu command"
        # Safety: block dangerous commands
        dangerous = ["rm -rf", "del /s", "format", "rmdir", "shutdown", "taskkill"]
        if any(d in command.lower() for d in dangerous):
            return "❌ Command này bị chặn vì lý do an toàn."
        try:
            if cmd_type == "python":
                result = subprocess.run(
                    ["python", "-c", command],
                    capture_output=True, text=True, timeout=30,
                    cwd=_os.path.dirname(_os.path.abspath(__file__))
                )
            else:
                result = subprocess.run(
                    command, shell=True,
                    capture_output=True, text=True, timeout=30,
                    cwd=_os.path.dirname(_os.path.abspath(__file__))
                )
            output = result.stdout + result.stderr
            output = output[:2000] if output else "(no output)"
            icon = "✅" if result.returncode == 0 else "❌"
            return f"{icon} **Command:** `{command[:100]}`\n\n```\n{output}\n```"
        except subprocess.TimeoutExpired:
            return f"⏰ Command timeout (30s): `{command[:100]}`"
        except Exception as e:
            return f"❌ Lỗi chạy command: {e}"
    
    if act == "dev_fix_code":
        import os as _os
        target_file = params.get("file", "")
        old_code = params.get("old_code", "")
        new_code = params.get("new_code", "")
        reason = params.get("reason", "AI auto-fix")
        if not target_file or not old_code or not new_code:
            return "❌ Cần file, old_code, new_code"
        proj_dir = _os.path.dirname(_os.path.abspath(__file__))
        filepath = _os.path.join(proj_dir, target_file)
        if not _os.path.exists(filepath):
            filepath = _os.path.join(proj_dir, "skills", target_file)
        if not _os.path.exists(filepath):
            return f"❌ File '{target_file}' không tìm thấy"
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            if old_code not in content:
                return f"❌ Không tìm thấy code cần sửa trong {target_file}"
            # Backup first
            backup_path = filepath + ".bak"
            with open(backup_path, 'w', encoding='utf-8') as f:
                f.write(content)
            # Apply fix
            new_content = content.replace(old_code, new_code, 1)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(new_content)
            return (f"✅ **Đã sửa code** trong `{target_file}`\n\n"
                    f"📝 Lý do: {reason}\n"
                    f"💾 Backup: `{_os.path.basename(backup_path)}`\n\n"
                    f"⚠️ Cần restart service để áp dụng!")
        except Exception as e:
            return f"❌ Lỗi sửa code: {e}"
    
    if act == "dev_create_skill":
        import db as _db
        name = params.get("name", "")
        task_pattern = params.get("task_pattern", "")
        keywords = params.get("keywords", "")
        steps = params.get("steps", [])
        # Ensure keywords is string (AI may send list)
        if isinstance(keywords, list):
            keywords = ",".join(str(k) for k in keywords)
        # Ensure steps is properly serialized
        if isinstance(steps, str):
            steps_json = steps
            try:
                steps_list = json.loads(steps)
                total = len(steps_list) if isinstance(steps_list, list) else 1
            except Exception:
                total = 1
        else:
            steps_json = json.dumps(steps, ensure_ascii=False) if steps else "[]"
            total = len(steps) if isinstance(steps, list) else 1
        if not name or not task_pattern:
            return "❌ Cần name và task_pattern"
        try:
            result = _db.save_agent_skill(
                name=name,
                task_pattern=task_pattern,
                keywords=str(keywords),
                steps_json=str(steps_json),
                total_steps=total,
                duration=0
            )
            sid = result.get("id", "?")
            updated = result.get("updated", False)
            action_word = "cập nhật" if updated else "tạo mới"
            return (f"🧠 **Skill {action_word}!**\n\n"
                    f"ID: #{sid}\n"
                    f"Tên: **{name}**\n"
                    f"Pattern: `{task_pattern}`\n"
                    f"Keywords: `{keywords}`\n"
                    f"Steps: {len(steps)} bước")
        except Exception as e:
            return f"❌ Lỗi tạo skill: {e}"

    if act == "dev_restart_service":
        service = params.get("service", "all")
        import subprocess
        try:
            subprocess.Popen(["python", "telegram_bot.py"])
            return "✅ Service restart triggered"
        except Exception as e:
            return f"❌ Restart fail: {e}"

    # ===== DEV LIST ACTIONS: Auto-discover all available actions =====
    if act == "dev_list_actions":
        import os as _os
        proj_dir = _os.path.dirname(_os.path.abspath(__file__))
        actions_found = []
        # Scan telegram_bot.py for all 'if act ==' handlers
        bot_path = _os.path.join(proj_dir, "telegram_bot.py")
        try:
            with open(bot_path, 'r', encoding='utf-8') as f:
                for i, line in enumerate(f, 1):
                    stripped = line.strip()
                    if stripped.startswith('if act == '):
                        act_name = stripped.split('"')[1] if '"' in stripped else stripped.split("'")[1] if "'" in stripped else "?"
                        actions_found.append(f"L{i}: `{act_name}`")
        except Exception:
            pass
        # Scan openclaw_api.py for all API endpoints
        api_endpoints = []
        api_path = _os.path.join(proj_dir, "openclaw_api.py")
        try:
            with open(api_path, 'r', encoding='utf-8') as f:
                for i, line in enumerate(f, 1):
                    if "path == '" in line or 'path == "' in line:
                        ep = line.split("'")[1] if "'" in line else line.split('"')[1]
                        api_endpoints.append(f"`{ep}`")
        except Exception:
            pass
        # Scan skills/ folder
        skills_list = []
        skills_dir = _os.path.join(proj_dir, "skills")
        if _os.path.isdir(skills_dir):
            for sf in sorted(_os.listdir(skills_dir)):
                if sf.endswith('.py') and not sf.startswith('__'):
                    skills_list.append(f"`{sf}`")
        text = f"📚 **Tất cả actions ({len(actions_found)})**\n"
        text += ", ".join(actions_found[:60])
        text += f"\n\n🔌 **API endpoints ({len(api_endpoints)})**\n"
        text += ", ".join(api_endpoints[:60])
        text += f"\n\n🧠 **Skills ({len(skills_list)})**\n"
        text += ", ".join(skills_list)
        return text

    return f"⚠️ Action '{act}' chưa được hỗ trợ. Dùng dev_list_actions để xem tất cả."


# ============================================================
# Telegram handlers
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "💖 **CUTE AI — FB Manager Pro**\n\n"
        "Chào anh~ Em là trợ lý AI, cứ nói tự nhiên em hiểu:\n"
        "• `tạo 5 profile fb1` · `xóa hết fb3`\n"
        "• `check fb1` · `xóa die fb2`\n"
        "• `check trùng fb1` · `xóa trùng fb1`\n"
        "• `s10 mở browser` · `đóng browser s10`\n"
        "• `s10 thoát hết nhóm` · `s10 xem 5 reels`\n"
        "• `chụp s10` · `nuôi s10`\n"
        "• `liệt kê profiles fb1` · `thư mục`\n"
        "• `stop` · `dừng hết`\n\n"
        "Gõ /help để xem đầy đủ nha~ 💕",
        parse_mode="Markdown"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📋 **Hướng dẫn**\n\n"
        "**🌐 Browser:**\n"
        "`mở browser s10` · `đóng browser s10`\n\n"
        "**🔍 Kiểm tra:**\n"
        "`check login s10` · `check status s10`\n"
        "`check fb1` (batch) · `liệt kê profiles fb1`\n\n"
        "**👥 Groups & Reels:**\n"
        "`s10 thoát hết nhóm` · `xem groups s10`\n"
        "`s10 xem 5 reels comment`\n\n"
        "**📰 Feed & Nuôi:**\n"
        "`đọc feed s10` · `nuôi s10`\n\n"
        "**📸 Screenshot & Vision:**\n"
        "`chụp s10` · `vision capture s10`\n"
        "`vision click nút Like trên s10`\n\n"
        "**📁 Quản lý Hidemium:**\n"
        "`thư mục` · `tags` · `scripts` · `campaigns`\n"
        "`đang chạy` · `phiên bản browser`\n"
        "`xóa die fb1` · `skills` · `lịch sử agent`\n\n"
        "**Lệnh nhanh:**\n"
        "`/open_browser S10` · `/leave_groups S10`\n"
        "`/debug_groups S10` · `/profiles`",
        parse_mode="Markdown"
    )


async def open_browser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("VD: `/open_browser S10`", parse_mode="Markdown")
        return
    profile = " ".join(context.args)
    msg = await update.message.reply_text(f"🌐 Đang mở browser {profile}...")
    result = await execute_action({"action": "open_browser", "params": {"profile": profile}})
    await send_long_text(result, msg)


async def leave_groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("VD: `/leave_groups S10`", parse_mode="Markdown")
        return
    profile = " ".join(context.args)
    msg = await update.message.reply_text(f"⏳ Đang thoát nhóm cho {profile}...")
    chat_id = update.effective_chat.id
    result = await execute_action(
        {"action": "leave_groups", "params": {"profile": profile}},
        telegram_chat_id=str(chat_id), telegram_message_id=msg.message_id
    )
    await send_long_text(result, msg)


async def debug_groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("VD: `/debug_groups S10`", parse_mode="Markdown")
        return
    profile = " ".join(context.args)
    msg = await update.message.reply_text(f"🔍 Đang kiểm tra groups {profile}...")
    result = await execute_action({"action": "debug_groups", "params": {"profile": profile}})
    await send_long_text(result, msg)


async def profiles_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("📱 Đang lấy danh sách...")
    folder_id = context.args[0] if context.args else None
    result = await execute_action({"action": "list_profiles", "params": {"folder_id": folder_id}})
    await send_long_text(result, msg)


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """NLP handler: parse user intent via Devstral, then execute."""
    try:
        await _message_handler_inner(update, context)
    except Exception as e:
        logger.error(f"message_handler error: {e}", exc_info=True)
        # ===== Route crash through Grok before showing to user =====
        grok_diagnosis = ""
        if GROK_ENABLED:
            try:
                import traceback as _tb
                _tb_text = _tb.format_exc()
                _user_text = ""
                try:
                    _user_text = update.message.text[:500] if update.message and update.message.text else "unknown"
                except Exception:
                    _user_text = "unknown"
                _grok_crash = await grok_handle_error(
                    {"action": "message_handler_crash", "params": {}},
                    f"CRASH: {type(e).__name__}: {e}\n{_tb_text[-800:]}",
                    _user_text,
                    retry_count=0, max_retry=0  # No retry for top-level crash
                )
                grok_diagnosis = _grok_crash.get("user_message", "")
            except Exception:
                pass
        try:
            error_text = f"⚠️ Lỗi: {str(e)[:200]}"
            if grok_diagnosis and grok_diagnosis != str(e)[:200]:
                error_text += f"\n\n🧠 Grok phân tích: {grok_diagnosis[:300]}"
            await update.message.reply_text(error_text)
        except Exception:
            pass


async def _keep_typing(chat_id_int: int, bot, stop_event: asyncio.Event):
    """Keep sending typing action every 4s until stop_event is set."""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id_int, action=constants.ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            break
        except asyncio.TimeoutError:
            continue


async def _message_handler_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inner handler with actual logic."""
    user_msg = update.message.text
    logger.info(f"User: {user_msg}")
    chat_id = str(update.effective_chat.id)
    chat_id_int = update.effective_chat.id
    bot = context.bot

    # Quick follow-up handler (ONLY for trivial: ok/retry/xong chưa)
    quick = try_quick_parse(user_msg, chat_id=chat_id)
    if quick:
        logger.info(f"Quick follow-up: {quick}")
        add_to_history(chat_id, "user", user_msg)
        msg = await update.message.reply_text(quick.get("reply", "⏳ Đang xử lý..."))
        # Start typing indicator during execution
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(chat_id_int, bot, stop_typing))
        try:
            result = await execute_action(quick, telegram_chat_id=chat_id, telegram_message_id=msg.message_id)
        finally:
            stop_typing.set()
            await typing_task
        add_to_history(chat_id, "assistant", result[:300] if result else "Done")
        await send_long_text(result, msg)
        return

    # ===== PRE-AI INTERCEPT: Hard-coded routing for critical intents =====
    # that AI brain (especially small JanAI model) keeps getting wrong
    _intercept = _try_intercept(user_msg)
    if _intercept:
        logger.info(f"Pre-AI intercept: {_intercept}")
        add_to_history(chat_id, "user", user_msg)
        msg = await update.message.reply_text(_intercept.get("reply", "⏳ Đang xử lý..."))
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(chat_id_int, bot, stop_typing))
        try:
            result = await execute_action(_intercept, telegram_chat_id=chat_id, telegram_message_id=msg.message_id)
        finally:
            stop_typing.set()
            await typing_task
        add_to_history(chat_id, "assistant", result[:300] if result else "Done")
        await send_long_text(result, msg)
        return

    # ===== AI-FIRST: GPT-5 is the BRAIN — analyzes, plans, decides =====
    # Start typing indicator immediately so user sees bot is alive
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(chat_id_int, bot, stop_typing))
    msg = await update.message.reply_text("🧠 Đang phân tích...")
    
    # Extract entities to help AI (pre-parsed hints)
    entity_hints = []
    _folder_m = re.search(r'fb\s*(\d+)', user_msg.lower())
    if _folder_m:
        entity_hints.append(f"folder_id=fb{_folder_m.group(1)}")
    else:
        _named = re.search(r'\b(fb\s*ok|fbok|đông\s*hưng)\b', user_msg.lower())
        if _named:
            _n = _named.group(1).strip().lower()
            _fmap = {'fb ok': 'fb5', 'fbok': 'fb5', 'đông hưng': 'fb7'}
            entity_hints.append(f"folder_id={_fmap.get(_n, _n)}")
    _prof_m = re.search(r'\b[sS]\s*(\d+)\b', user_msg)
    if _prof_m:
        entity_hints.append(f"profile=S{_prof_m.group(1)}")
    elif re.search(r'\b[aA]\s*(\d+)\b', user_msg):
        _am = re.search(r'\b[aA]\s*(\d+)\b', user_msg)
        entity_hints.append(f"profile=A{_am.group(1)}")
    _conc = _extract_concurrency(user_msg.lower())
    if _conc != 5:
        entity_hints.append(f"concurrency={_conc}")

    # Detect UID/PASSWORD lines (e.g. "61555409704088  6nZSTibO8f2@")
    _uid_pass_lines = re.findall(r'^\s*(\d{10,20})\s+(\S+)\s*$', user_msg, re.MULTILINE)
    if len(_uid_pass_lines) >= 2:
        entity_hints.append(f"uid_pass_accounts={len(_uid_pass_lines)}")
        entity_hints.append("ACTION_HINT=bulk_login (user gửi danh sách UID+PASS, dùng bulk_login)")
        # Extract the raw account lines to pass directly
        _acct_lines = []
        for uid, pwd in _uid_pass_lines:
            _acct_lines.append(f"{uid}  {pwd}")
        entity_hints.append(f"accounts_text={chr(10).join(_acct_lines)}")

    # Detect count hint from text (e.g. "lấy 5 profile đầu" → count=5)
    _count_m = re.search(r'(\d+)\s*(?:profile|cái|con|nick|account|tài khoản)\s*(?:đầu|đầu tiên|first)', user_msg.lower())
    if _count_m:
        entity_hints.append(f"count={_count_m.group(1)}")
    
    # Build messages with history for context
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    history = get_history(chat_id)
    messages.extend(history)
    # Add entity hints so AI knows what entities are in the message
    user_content = user_msg
    if entity_hints:
        user_content += f"\n[Detected: {', '.join(entity_hints)}]"
    messages.append({"role": "user", "content": user_content})
    # Save original user message to history (without hints)
    add_to_history(chat_id, "user", user_msg)
    
    ai_response = await call_ai(messages, max_tokens=4000, temperature=0.1)
    logger.info(f"AI raw: {ai_response[:300]}")
    
    # AI done thinking — update status
    try:
        await msg.edit_text("⚡ AI đã phân tích xong, đang xử lý...")
    except Exception:
        pass
    
    parsed = parse_ai_response(ai_response)
    
    # ===== GROK SUPERVISOR: Validate & Override GPT =====
    grok_plan_data = None
    if GROK_ENABLED:
        try:
            grok_plan_data = await grok_plan(user_msg, entity_hints, list(history))
            if grok_plan_data:
                logger.info(f"Grok plan: {json.dumps(grok_plan_data, ensure_ascii=False)[:300]}")
                grok_action = None
                
                # Extract first action from Grok plan
                if grok_plan_data.get("plan") and isinstance(grok_plan_data["plan"], list):
                    for step in grok_plan_data["plan"]:
                        if step.get("action") and step["action"] not in ("chat", "done"):
                            grok_action = step
                            break
                elif grok_plan_data.get("action") and grok_plan_data["action"] not in ("chat", "done"):
                    grok_action = grok_plan_data
                
                if grok_action:
                    gpt_action = parsed.get("action") if isinstance(parsed, dict) else None
                    
                    # Case 1: GPT returned chat but Grok found a real action → override
                    if gpt_action == "chat":
                        parsed = {
                            "action": grok_action["action"],
                            "params": grok_action.get("params", {}),
                            "reply": grok_plan_data.get("analysis", f"Grok: {grok_action['action']}")
                        }
                        logger.info(f"Grok override: GPT chat → {parsed['action']}")
                    
                    # Case 2: GPT chose wrong action → Grok corrects
                    elif gpt_action and gpt_action != grok_action["action"]:
                        # Grok's action takes priority (it has reasoning)
                        logger.info(f"Grok correction: GPT {gpt_action} → Grok {grok_action['action']}")
                        parsed = {
                            "action": grok_action["action"],
                            "params": grok_action.get("params", {}),
                            "reply": grok_plan_data.get("analysis", parsed.get("reply", ""))
                        }
                    
                    # Case 3: Same action but Grok has better params → merge
                    elif gpt_action == grok_action["action"] and isinstance(parsed, dict):
                        grok_params = grok_action.get("params", {})
                        gpt_params = parsed.get("params", {})
                        # Grok fills missing params
                        for k, v in grok_params.items():
                            if k not in gpt_params or gpt_params[k] is None:
                                gpt_params[k] = v
                        parsed["params"] = gpt_params
                    
                    # Show risk warning if any
                    risk = grok_plan_data.get("risk")
                    if risk:
                        try:
                            await msg.edit_text(f"⚠️ Grok: {risk[:150]}\n⚡ Đang xử lý...")
                        except Exception:
                            pass
                    else:
                        try:
                            analysis = grok_plan_data.get('analysis', 'Đang xử lý...')
                            await msg.edit_text(f"🧠 {analysis[:150]}")
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"Grok plan failed (non-fatal): {e}")
    
    # === JSON RETRY: If AI returned plain text (chat fallback) but user wanted an action ===
    if isinstance(parsed, dict) and parsed.get("action") == "chat":
        # Check if the original message seems like an action request
        action_keywords = r'\b(?:check|xóa|mở|đóng|xem|list|kiểm tra|nuôi|lướt|agent|thoát|delete|scan|bật|chạy|open|close|folder|stop|dừng|trùng|duplicate|login|đăng nhập)\b|\bfb\d|\bs\d'
        if re.search(action_keywords, user_msg.lower()):
            # AI returned chat but user wanted action → retry with stronger JSON instruction
            logger.warning("AI returned chat for action-like message, retrying with JSON nudge...")
            retry_messages = messages.copy()
            retry_messages.append({"role": "assistant", "content": ai_response})
            retry_messages.append({"role": "user", "content": "RESPOND WITH JSON ONLY. Format: {\"action\": \"...\", \"params\": {...}, \"reply\": \"...\"}. NO plain text."})
            retry_response = await call_ai(retry_messages, max_tokens=4000, temperature=0.1)
            retry_parsed = parse_ai_response(retry_response)
            if isinstance(retry_parsed, dict) and retry_parsed.get("action") != "chat":
                parsed = retry_parsed
                logger.info(f"JSON retry success: {parsed.get('action')}")
    
    # ===== GROK FIX: If GPT still wrong after retry, Grok takes over =====
    if GROK_ENABLED and isinstance(parsed, dict) and parsed.get("action") == "chat":
        action_keywords = r'\b(?:check|xóa|mở|đóng|xem|list|kiểm tra|nuôi|lướt|agent|thoát|delete|scan|bật|chạy|open|close|folder|stop|dừng|trùng|duplicate|login|đăng nhập)\b|\bfb\d|\bs\d'
        if re.search(action_keywords, user_msg.lower()):
            logger.warning("GPT failed twice, asking Grok to fix...")
            try:
                grok_fixed = await grok_fix_gpt_output(ai_response, user_msg, entity_hints)
                if grok_fixed and grok_fixed.get("action") != "chat":
                    parsed = grok_fixed
                    logger.info(f"Grok fixed GPT → {parsed['action']}")
                    try:
                        await msg.edit_text("🧠 Grok đã sửa lệnh, đang xử lý...")
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"Grok fix failed: {e}")
    
    # Handle multi-action (list of actions)
    if isinstance(parsed, list) and len(parsed) > 1:
        logger.info(f"Multi-action: {len(parsed)} actions")
        actions = parsed
        # Send initial status
        status_lines = []
        for i, a in enumerate(actions):
            profile = a.get("params", {}).get("profile", "?")
            act = a.get("action", "?")
            status_lines.append(f"⏳ {profile}: {a.get('reply', act)}")
        try:
            await msg.edit_text("\n".join(status_lines))
        except Exception:
            pass
        
        # Create separate messages for each action
        action_msgs = []
        for i, a in enumerate(actions):
            if i == 0:
                action_msgs.append(msg)  # Reuse first message
            else:
                new_msg = await update.message.reply_text(a.get("reply", "⏳ Đang xử lý..."))
                action_msgs.append(new_msg)
        
        # Run all actions concurrently — track for stop_all
        async def run_single_action(action, action_msg):
            if _is_cancelled():
                return "🛑 Cancelled by stop_all"
            try:
                result = await execute_action(action, telegram_chat_id=chat_id, telegram_message_id=action_msg.message_id)
                await send_long_text(result, action_msg)
                return result
            except asyncio.CancelledError:
                return "🛑 Cancelled by stop_all"
            except Exception as e:
                error_text = f"❌ Lỗi: {e}"
                await safe_edit(action_msg, error_text)
                return error_text
        
        _gather_task = asyncio.ensure_future(
            asyncio.gather(*[
                run_single_action(actions[i], action_msgs[i]) 
                for i in range(len(actions))
            ], return_exceptions=True)
        )
        _register_task(_gather_task)
        try:
            results = await _gather_task
        except asyncio.CancelledError:
            results = ["🛑 Cancelled by stop_all"] * len(actions)
        
        # ===== GROK: Check multi-action errors → route through Grok =====
        _error_markers_ma = ["❌", "⚠️ Lỗi", "ERROR", "thất bại", "failed", "CRASH"]
        results = list(results)  # make mutable
        for _i, (_res, _act, _amsg) in enumerate(zip(results, actions, action_msgs)):
            _res_str = str(_res) if _res else ""
            _has_err = any(m in _res_str for m in _error_markers_ma)
            if _has_err and GROK_ENABLED:
                try:
                    _grok_err = await grok_handle_error(
                        _act, _res_str, user_msg, retry_count=0, max_retry=1
                    )
                    if _grok_err["retry"] and _grok_err.get("fix_action"):
                        _fix = _grok_err["fix_action"]
                        if _fix.get("action"):
                            try:
                                await safe_edit(_amsg, f"🧠 Grok retry: {_grok_err.get('diagnosis', '')[:100]}...")
                            except Exception:
                                pass
                            _retry_res = await execute_action(_fix, telegram_chat_id=chat_id, telegram_message_id=_amsg.message_id)
                            await send_long_text(_retry_res, _amsg)
                            results[_i] = _retry_res
                    elif _grok_err.get("user_message") and _grok_err["user_message"] != _res_str:
                        _diag_text = f"{_res_str}\n\n🧠 Grok: {_grok_err['user_message']}"
                        await send_long_text(_diag_text, _amsg)
                except Exception as _ge:
                    logger.warning(f"Grok multi-action error handling failed: {_ge}")
        
        # Save summary to history
        summary = " | ".join([r[:100] if r else "Done" for r in results])
        add_to_history(chat_id, "assistant", summary[:300])
        stop_typing.set()
        await typing_task
        return

    # Single action (original flow) → NOW: AGENT LOOP
    action = parsed if isinstance(parsed, dict) else parsed[0] if isinstance(parsed, list) else {"action": "chat", "params": {}, "reply": str(parsed)}
    logger.info(f"Parsed action: {action.get('action')} params={action.get('params')}")
    
    # If just chat, return AI reply directly
    if action.get("action") == "chat":
        stop_typing.set()
        await typing_task
        reply_text = action.get('reply', ai_response)
        if not reply_text or len(reply_text.strip()) < 3:
            reply_text = "Anh nói rõ hơn em nghe với 😄"
        add_to_history(chat_id, "assistant", reply_text)
        await send_long_text(reply_text, msg)
        return
    
    # ===== AGENT LOOP: Execute → Feed result to AI → AI decides next → Repeat =====
    MAX_LOOP = 5  # Max iterations to prevent infinite loops
    loop_history = messages.copy()  # Clone conversation for loop context
    loop_history.append({"role": "assistant", "content": ai_response})
    all_results = []  # Collect results from all steps
    current_action = action
    result = None  # Initialize to avoid UnboundLocalError on early break
    
    for loop_i in range(MAX_LOOP):
        # === Cancel check: break immediately if stop was requested ===
        if _is_cancelled():
            all_results.append("[CANCELLED] Đã dừng bởi STOP ALL")
            break

        act_name = current_action.get('action', 'unknown')
        act_reply = current_action.get('reply', '⏳ Đang thực hiện...')
        step_label = f"[{loop_i+1}/{MAX_LOOP}]" if MAX_LOOP > 1 else ""
        
        # Update status message
        try:
            safe_act = escape_md(act_name)
            safe_reply = escape_md(act_reply)
            progress = f"⚡ {step_label} {safe_reply}\n⏳ _Đang thực hiện {safe_act}..._"
            if all_results:
                # Show previous results briefly
                prev_summary = "\n".join([f"✅ Step {i+1}: {escape_md(r[:80])}" for i, r in enumerate(all_results[-3:])])
                progress = f"{prev_summary}\n\n{progress}"
            await msg.edit_text(progress, parse_mode="Markdown")
        except Exception:
            try:
                await msg.edit_text(f"⚡ {step_label} {act_reply}\n⏳ Đang thực hiện {act_name}...")
            except Exception:
                pass
        
        # Execute action (wrapped — exceptions become error strings for Grok to see)
        # Dynamic timeout: batch actions get 1 hour, single actions 10 min
        _batch_actions = {"bulk_login", "batch_check_login", "batch_agent_execute",
                          "delete_die_all", "delete_die_profiles", "delete_die"}
        _action_timeout = 3600 if act_name in _batch_actions else 600
        try:
            result = await asyncio.wait_for(
                execute_action(current_action, telegram_chat_id=chat_id, telegram_message_id=msg.message_id),
                timeout=_action_timeout
            )
        except asyncio.CancelledError:
            all_results.append(f"[{act_name}] CANCELLED by stop_all")
            break
        except asyncio.TimeoutError:
            result = f"❌ TIMEOUT: Action '{act_name}' vượt quá {_action_timeout}s → tự động hủy"
            logger.error(f"execute_action TIMEOUT after {_action_timeout}s: {act_name}")
        except Exception as exec_err:
            import traceback as _tb
            tb_short = _tb.format_exc().strip().split('\n')[-3:]
            result = f"❌ CRASH in execute_action: {type(exec_err).__name__}: {exec_err}\n{''.join(tb_short)}"
            logger.error(f"execute_action CRASH (caught for Grok): {exec_err}", exc_info=True)
        all_results.append(f"[{act_name}] {result[:600] if result else 'Done'}")
        logger.info(f"Loop {loop_i+1} result: {act_name} → {result[:300] if result else 'None'}")

        # === Cancel check after execution ===
        if _is_cancelled():
            all_results.append("[CANCELLED] Đã dừng bởi STOP ALL")
            break
        result_str = str(result) if result else ""
        is_error_result = any(marker in result_str for marker in [
            "❌", "⚠️ Lỗi", "⚠️ Err", "ERROR", "thất bại", "failed",
            "timeout", "Timeout", "crash", "CRASH"
        ])
        
        # ===== GROK ERROR HANDLER: Mọi lỗi phải qua Grok =====
        grok_retry_count = 0
        while is_error_result and GROK_ENABLED and grok_retry_count < 2:
            grok_retry_count += 1
            logger.info(f"Error detected in result, calling Grok (attempt {grok_retry_count})...")
            try:
                grok_err = await grok_handle_error(
                    current_action, result_str, user_msg,
                    retry_count=grok_retry_count - 1, max_retry=2
                )
                diagnosis = grok_err.get('diagnosis', '')
                user_message = grok_err.get('user_message', '')
                logger.info(f"Grok error analysis: retry={grok_err['retry']}, diagnosis={diagnosis[:150]}")
                
                # ===== Alert if Grok API is offline =====
                if diagnosis == "Grok API failed":
                    logger.warning("⚠️ Grok supervisor API offline — error handling degraded")
                    try:
                        await msg.edit_text(f"{result_str[:500]}\n\n⚠️ Grok supervisor đang offline — không thể tự phân tích lỗi")
                    except Exception:
                        pass
                    break
                
                if grok_err["retry"] and grok_err.get("fix_action"):
                    # Grok says retry with fixed params
                    fix_action = grok_err["fix_action"]
                    if fix_action.get("action"):
                        try:
                            await msg.edit_text(
                                f"🧠 Grok phát hiện lỗi → {diagnosis[:100]}\n"
                                f"🔄 Đang retry với params đã sửa..."
                            )
                        except Exception:
                            pass
                        logger.info(f"Grok retry with: {fix_action}")
                        current_action = fix_action
                        # Re-execute with Grok's fixed action
                        result = await execute_action(current_action, telegram_chat_id=chat_id, telegram_message_id=msg.message_id)
                        result_str = str(result) if result else ""
                        # Update results list
                        all_results[-1] = f"[{fix_action.get('action', act_name)}-grok-retry] {result[:600] if result else 'Done'}"
                        # Check if still error
                        is_error_result = any(m in result_str for m in [
                            "❌", "⚠️ Lỗi", "⚠️ Err", "ERROR", "thất bại", "failed",
                            "timeout", "Timeout", "crash"
                        ])
                        if not is_error_result:
                            logger.info("Grok retry SUCCEEDED!")
                            # Mark last error as resolved in memory
                            for e in reversed(_grok_error_memory):
                                if e["action"] == act_name and not e["resolved"]:
                                    e["resolved"] = True
                                    break
                            try:
                                await msg.edit_text(f"✅ Grok retry thành công!")
                            except Exception:
                                pass
                        continue  # Check again if still error
                    else:
                        break  # No valid fix action
                else:
                    # Grok says no retry (no more attempts or unfixable)
                    # Append Grok's diagnosis to the result
                    if user_message and user_message != result_str:
                        result = f"{result_str}\n\n🧠 **Grok phân tích:** {user_message}"
                        result_str = result
                    break
            except Exception as e:
                logger.warning(f"Grok error handling failed: {e}")
                break
        
        # ===== GROK ESCALATION: Param retries exhausted → code-level fix =====
        if is_error_result and GROK_ENABLED and grok_retry_count >= 2:
            logger.info("Param retries exhausted, escalating to Grok code analysis...")
            try:
                await msg.edit_text("🧠 Grok đang phân tích code để tìm nguyên nhân gốc...")
            except Exception:
                pass
            try:
                auto_fix = await grok_auto_fix(current_action, result_str, user_msg)
                if auto_fix["fixed"]:
                    # Mark previous errors as resolved
                    for e in _grok_error_memory:
                        if not e["resolved"] and e["action"] == act_name:
                            e["resolved"] = True
                    result = f"{result_str}\n\n{auto_fix['message']}"
                    is_error_result = False  # Fixed!
                    if auto_fix.get("needs_restart"):
                        try:
                            import subprocess as _subprocess
                            proj_dir = os.path.dirname(os.path.abspath(__file__))
                            _subprocess.Popen(
                                [sys.executable, os.path.join(proj_dir, "openclaw_api.py")],
                                cwd=proj_dir,
                                creationflags=getattr(_subprocess, 'CREATE_NEW_CONSOLE', 0)
                            )
                            await asyncio.sleep(3)
                            result += "\n♻️ Service đã được auto-restart!"
                        except Exception as restart_err:
                            result += f"\n⚠️ Cần restart manual: {restart_err}"
                else:
                    result = f"{result_str}\n\n{auto_fix['message']}"
            except Exception as e:
                logger.warning(f"Grok auto-fix failed: {e}")
        
        # ===== GROK VALIDATE: For non-error results, still validate =====
        grok_verdict = None
        if GROK_ENABLED and result and not is_error_result:
            try:
                grok_verdict = await grok_validate(current_action, result_str, user_msg)
                if grok_verdict:
                    v_status = grok_verdict.get("status", "ok")
                    logger.info(f"Grok verdict: {v_status} — {grok_verdict.get('diagnosis', '')[:100]}")
                    
                    if v_status == "retry" and grok_verdict.get("fix"):
                        fix_action = grok_verdict["fix"]
                        if fix_action.get("action"):
                            logger.info(f"Grok validate retry: {fix_action['action']}")
                            current_action = fix_action
                            try:
                                await msg.edit_text(f"🧠 Grok phát hiện vấn đề, retry...")
                            except Exception:
                                pass
                            continue
                    
                    if v_status == "error" and grok_verdict.get("user_message"):
                        result = f"{result_str}\n\n🧠 **Grok:** {grok_verdict['user_message']}"
            except Exception as e:
                logger.warning(f"Grok validate failed (non-fatal): {e}")
        
        # Check if this is the last possible iteration
        if loop_i >= MAX_LOOP - 1:
            logger.info("Agent loop reached max iterations")
            break
        
        # Feed result back to AI — let AI decide: done or next step?
        loop_history.append({"role": "user", "content": (
            f"[SYSTEM] Action '{act_name}' đã thực hiện xong.\n"
            f"Kết quả:\n{result[:3000] if result else 'Done'}\n\n"
            f"Dựa trên kết quả, bạn cần làm gì tiếp?\n"
            f"- Nếu XONG → trả {'{'}\"action\": \"done\", \"reply\": \"tóm tắt kết quả cho user\"{'}'}\n"
            f"- Nếu CẦN LÀM TIẾP → trả JSON action tiếp theo như bình thường\n"
            f"- Nếu CÓ LỖI → phân tích và gợi ý action sửa, hoặc \"done\" + giải thích lỗi"
        )})
        
        try:
            next_response = await call_ai(loop_history, max_tokens=4000, temperature=0.2)
            logger.info(f"Loop AI response: {next_response[:200]}")
            loop_history.append({"role": "assistant", "content": next_response})
        except Exception as e:
            logger.error(f"Loop AI call failed: {e}")
            break
        
        next_parsed = parse_ai_response(next_response)
        if not isinstance(next_parsed, dict):
            next_parsed = next_parsed[0] if isinstance(next_parsed, list) and next_parsed else {"action": "done", "reply": next_response}
        
        next_act = next_parsed.get("action", "done")
        
        # AI says done → exit loop
        if next_act in ("done", "chat"):
            result = next_parsed.get("reply", result)
            break
        
        # GUARD: Block destructive/creative actions in agent loop unless user explicitly asked
        # This prevents AI from auto-creating/deleting when it sees empty results
        BLOCKED_IN_LOOP = {"create_profiles", "delete_all_profiles", "delete_duplicates"}
        original_act = action.get("action", "")
        if next_act in BLOCKED_IN_LOOP and next_act != original_act:
            logger.warning(f"Agent loop BLOCKED auto-{next_act} (original was {original_act})")
            result = next_parsed.get("reply", result) or result
            break
        
        # AI wants another action → continue loop
        current_action = next_parsed
        logger.info(f"Agent loop continuing: step {loop_i+2} → {next_act}")
    
    # Stop typing
    stop_typing.set()
    await typing_task
    
    # ===== Post-loop: Build final result =====
    # If we had multiple steps, add a summary
    if len(all_results) > 1:
        result = f"🔄 **Agent Loop** ({len(all_results)} bước)\n\n"
        for i, r in enumerate(all_results):
            result += f"**Step {i+1}:** {r[:500]}\n\n"
        # Add AI's final summary if it exists
        final_reply = current_action.get("reply", "") if isinstance(current_action, dict) else ""
        if final_reply and final_reply != result:
            result += f"📋 **Kết luận:** {final_reply}"
    
    # ===== AI SELF-DIAGNOSE: If result has errors, read actual code + analyze =====
    if result and ('❌' in result or '⚠️ Lỗi' in result):
        act_name = action.get('action', 'unknown')
        logger.info(f"Error in {act_name}, AI deep-diagnosing...")
        
        # Read relevant source code for context
        code_context = ""
        try:
            import os as _os
            proj_dir = _os.path.dirname(_os.path.abspath(__file__))
            # Find error keywords in code
            error_words = [w for w in result.split() if len(w) > 5 and not w.startswith('❌')][:3]
            for src_file in ['telegram_bot.py', 'openclaw_api.py']:
                fpath = _os.path.join(proj_dir, src_file)
                if _os.path.exists(fpath):
                    with open(fpath, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                    for ew in error_words:
                        for i, line in enumerate(lines):
                            if ew.lower() in line.lower() and 'def ' in line:
                                start = max(0, i - 2)
                                end = min(len(lines), i + 8)
                                code_context += f"\n[{src_file}:{i+1}] " + "".join(lines[start:end])
                                break
                        if code_context:
                            break
        except Exception:
            pass
        
        diag_messages = [{"role": "system", "content": (
            "Bạn là senior developer. Phân tích lỗi NGẮN GỌN:\n"
            "1) Root cause (1 câu)\n2) Cách sửa (1-2 câu)\n3) Action tiếp (1 câu)\n"
            "TỐI ĐA 4 CÂU. Tiếng Việt. KHÔNG lặp lại error message. KHÔNG viết dài."
        )}]
        diag_messages.append({"role": "user", "content": (
            f"Action: {act_name}\nParams: {action.get('params', {})}\n"
            f"Error: {result[:300]}\n"
            f"Code: {code_context[:500]}\n"
            f"Phân tích ngắn gọn."
        )})
        try:
            diagnosis = await call_ai(diag_messages, max_tokens=1000, temperature=0.3)
            if diagnosis and len(diagnosis.strip()) > 10:
                result += f"\n\n🧠 **AI phân tích:** {diagnosis.strip()}"
        except Exception:
            pass
    
    # Save structured result to history for smarter context tracking
    act_name = action.get('action', 'unknown')
    act_params = action.get('params', {})
    if act_name == 'agent_execute':
        add_to_history(chat_id, "assistant", f"[Agent done: {act_params.get('task','')}]")
    elif act_name in ('delete_all_profiles', 'delete_die_profiles', 'delete_die_all', 'delete_duplicates', 'check_duplicates'):
        # Save with action metadata so follow-up questions know what was done
        folder = act_params.get('folder_id', 'all')
        add_to_history(chat_id, "assistant", f"[{act_name}:{folder}] {result[:250] if result else 'Done'}")
    else:
        add_to_history(chat_id, "assistant", result[:300] if result else "Done")
    # ===== Send result — use module-level send_long_text =====
    await send_long_text(result, msg)


def _get_last_context(chat_id: str) -> dict:
    """Extract last folder/profile/action from conversation history."""
    ctx = {"folder": None, "profile": None, "action": None, "last_result": None}
    if not chat_id:
        return ctx
    history = get_history(chat_id)
    for msg in reversed(history):
        content = msg.get("content", "")
        c_lower = content.lower()
        # Extract folder
        if not ctx["folder"]:
            fm = re.search(r'fb\s*(\d+)', c_lower)
            if fm:
                ctx["folder"] = f"fb{fm.group(1)}"
            else:
                folder_map = {'fb1': 'fb1', 'fb2': 'fb2', 'fb3': 'fb3', 'fb ok': 'fb5', 'đông hưng': 'fb7'}
                for fname, fid in folder_map.items():
                    if fname in c_lower:
                        ctx["folder"] = fid
                        break
        # Extract profile
        if not ctx["profile"]:
            pm = re.search(r'\b[sS]\s*(\d+)\b', content)
            if pm:
                ctx["profile"] = f"S{pm.group(1)}"
            else:
                am = re.search(r'\b[aA]\s*(\d+)\b', content)
                if am:
                    ctx["profile"] = f"A{am.group(1)}"
        # Extract last action (structured metadata first, then regex fallback)
        if not ctx["action"]:
            # Structured format: [delete_all_profiles:fb3] ...
            meta_match = re.match(r'\[(delete_all_profiles|delete_die_profiles|delete_die_all|delete_duplicates|check_duplicates|batch_check_login|list_folders):([^\]]*)\]', content)
            if meta_match:
                action_map = {
                    'delete_all_profiles': 'delete_all',
                    'delete_die_profiles': 'delete_die',
                    'delete_die_all': 'delete_die',
                    'batch_check_login': 'batch_check',
                    'list_folders': 'list',
                }
                ctx["action"] = action_map.get(meta_match.group(1), meta_match.group(1))
                if not ctx["folder"] and meta_match.group(2) != 'all':
                    ctx["folder"] = meta_match.group(2)
            else:
                action_patterns = [
                    (r'xóa tất cả|xóa hết|xóa sạch|delete_all', 'delete_all'),
                    (r'xóa die|delete_die|đã xóa.*die', 'delete_die'),
                    (r'batch_check|check.*luồng|\d+ profiles', 'batch_check'),
                    (r'check|kiểm tra', 'check'),
                    (r'xóa|xoá|delete|dẹp', 'xóa'),
                    (r'list|danh sách|liệt kê', 'list'),
                ]
                for pattern, action_name in action_patterns:
                    if re.search(pattern, c_lower):
                        ctx["action"] = action_name
                        break
        # Extract last result summary
        if not ctx["last_result"] and msg.get("role") == "assistant":
            ctx["last_result"] = content[:300]
        if ctx["folder"] and ctx["profile"] and ctx["action"]:
            break
    return ctx


def _extract_concurrency(text_lower: str, default: int = 5) -> int:
    """Extract concurrency/thread count from text. Matches: '20 luồng', 'bật 20', 'mở 20 thread', etc."""
    # Pattern 1: "N luồng/thread/worker"
    m = re.search(r'(\d+)\s*(?:luồng|thread|worker|threads|workers)', text_lower)
    if m:
        return int(m.group(1))
    # Pattern 2: "bật/mở N" (before luồng concept)
    m = re.search(r'(?:bật|mở|chạy|dùng|set)\s+(\d+)', text_lower)
    if m:
        val = int(m.group(1))
        if 2 <= val <= 100:  # sanity check: not a profile number
            return val
    return default


# ============================================================
# CONCEPT-BASED INTENT SCORING ENGINE
# Instead of regex matching exact words, we detect CONCEPTS
# (semantic groups of synonyms) and score each intent.
# ============================================================

# Concept → list of Vietnamese/English synonyms
CONCEPTS = {
    # --- Actions ---
    "REMOVE":    ["xóa", "xoá", "delete", "dọn", "bỏ", "hủy", "loại", "loại bỏ", "dẹp", "xử lý",
                  "clear", "clean", "sạch", "remove", "bỏ đi", "vứt", "thanh lọc", "lọc", "trừ", "kick"],
    "CHECK":     ["check", "kiểm tra", "xem", "scan", "quét", "kiểm", "coi", "thử", "test", "verify",
                  "dò", "rà", "rà soát", "soát", "tra", "xét"],
    "OPEN":      ["mở", "open", "bật", "khởi động", "start", "launch", "run", "chạy"],
    "CLOSE":     ["đóng", "close", "tắt", "kill", "stop", "dừng", "ngưng", "shut"],
    "CREATE":    ["tạo", "thêm", "tạo mới", "create", "add", "new", "mới"],
    "LOGIN":     ["login", "đăng nhập", "nhập", "đăng nhập vào", "log in", "signin", "sign in"],
    "LIST":      ["list", "liệt kê", "danh sách", "show", "hiển thị", "có gì", "có những gì"],
    "COUNT":     ["bao nhiêu", "mấy", "tổng", "count", "đếm", "còn", "tồn tại", "số lượng", "total"],
    "NURTURE":   ["nuôi", "nurture", "dưỡng", "warm", "chăm", "chăm sóc", "tương tác", "farming", "farm"],
    "LEAVE":     ["thoát", "leave", "rời", "out", "bỏ", "hủy", "unfollow"],
    "WATCH":     ["xem", "watch", "lướt", "coi", "mở"],
    "UPLOAD":    ["đăng", "upload", "tải lên", "up", "đăng lên", "đăng reel", "đăng video"],
    
    # --- Targets ---
    "DIE":       ["die", "chết", "hỏng", "lỗi", "không login", "not log", "dead", "fail", "bay",
                  "mất", "không vào", "bị khóa", "locked", "disabled", "restrict", "checkpoint",
                  "không được", "hết hạn", "expired", "sập", "tạch", "toang", "tèo", "over"],
    "LIVE":      ["live", "sống", "ok", "hoạt động", "active", "tốt", "ngon", "fine", "good",
                  "còn sống", "đang sống", "vẫn sống", "healthy"],
    "KEEP":      ["giữ", "keep", "để lại", "giữ lại", "bảo toàn", "không xóa", "giữ nguyên", "preserved"],
    "ALL":       ["toàn bộ", "tất cả", "all", "hết", "mọi", "every", "toàn", "tất", "toàn thể",
                  "toàn diện", "mọi thứ", "tất tần tật", "toàn bọn", "hết thảy", "cả"],
    "FOLDER":    ["folder", "thư mục", "fb", "nhóm folder"],
    "PROFILE":   ["profile", "profiles", "acc", "account", "tài khoản", "nick", "con"],
    "GROUP":     ["group", "nhóm", "groups", "nh"],
    "REEL":      ["reel", "reels", "video ngắn", "short"],
    "PAGE":      ["page", "trang", "fanpage", "fan page"],
    "FEED":      ["feed", "bảng tin", "newsfeed", "trang chủ", "home"],
    "BROWSER":   ["browser", "trình duyệt", "chrome", "web"],
    "TAG":       ["tag", "tags", "nhãn", "label"],
    "SCRIPT":    ["script", "scripts", "kịch bản", "automation"],
    "CAMPAIGN":  ["campaign", "campaigns", "chiến dịch"],
    "RUNNING":   ["đang chạy", "running", "đang mở", "đang hoạt động"],
    "VERSION":   ["version", "phiên bản", "versions"],
    "SKILL":     ["skill", "skills", "kỹ năng", "đã học"],
    "HISTORY":   ["lịch sử", "history", "log", "nhật ký"],
    
    # --- Modifiers ---
    "COMMENT":   ["comment", "cmt", "bình luận", "phản hồi"],
    "LIKE":      ["like", "thích"],
    
    # --- Agent targets ---
    "NOTIFICATION": ["thông báo", "notification", "notif"],
    "FRIEND":    ["bạn bè", "friend", "friends", "kết bạn"],
    "MESSAGE":   ["tin nhắn", "message", "messenger", "nhắn tin", "chat"],
    "POST":      ["đăng bài", "post", "bài viết", "viết bài", "status"],
    "SEARCH":    ["tìm kiếm", "search", "tìm"],
    "FOLLOW":    ["theo dõi", "follow"],
    "DUPLICATE": ["trùng", "duplicate", "giống nhau", "trùng lặp", "giữ 1", "giữ một", "bản trùng"],
    "SCREENSHOT": ["chụp", "screenshot", "cap", "snap", "màn hình"],
    "VISION":    ["vision", "nhìn", "phân tích", "analyze"],
    "CLICK":     ["click", "bấm", "nhấn", "ấn", "chọn"],
    
    # --- Follow-up / confirmation ---
    "CONFIRM":   ["ok", "ừ", "ư", "ờ", "vâng", "yes", "y", "đi", "luôn", "ngay", "tiếp",
                  "tiếp tục", "làm đi", "chạy đi", "go", "ok luôn", "làm luôn", "chạy luôn",
                  "được", "thôi"],
    "RETRY":     ["lại", "retry", "rerun", "thử lại", "làm lại", "check lại", "chạy lại",
                  "lần nữa", "nữa"],
    "RESULT":    ["kết quả", "rồi sao", "xong chưa", "sao rồi", "thế nào", "ok chưa",
                  "xong", "ra sao", "kết quả sao"],
    "QUESTION":  ["chưa", "chưa?", "rồi chưa", "đã chưa", "xong chưa", "hết chưa",
                  "chưa vậy", "chưa bác", "đâu rồi", "sao rồi", "được chưa"],
}

# Pre-compile concept patterns for speed
_CONCEPT_PATTERNS = {}
for concept, words in CONCEPTS.items():
    # Sort by length (longest first) to match longer phrases before shorter ones
    sorted_words = sorted(words, key=len, reverse=True)
    # Escape regex special chars in each word
    escaped = [re.escape(w) for w in sorted_words]
    _CONCEPT_PATTERNS[concept] = re.compile(r'(?:' + '|'.join(escaped) + r')', re.IGNORECASE)


def _detect_concepts(text_lower: str) -> set:
    """Detect which semantic concepts appear in the text."""
    found = set()
    for concept, pattern in _CONCEPT_PATTERNS.items():
        if pattern.search(text_lower):
            found.add(concept)
    return found


# Intent scoring rules: (required_concepts, optional_boost_concepts, base_score)
# Higher score wins. Required = must have ALL. Optional = bonus points.
INTENT_RULES = {
    # --- Delete die (folder or all) ---
    # --- Delete ALL profiles (total wipe, no check needed) ---
    "delete_all_profiles": {
        "formulas": [
            # "xóa hết profile ở fb3" = REMOVE + ALL + PROFILE + FOLDER (no DIE)
            ({"REMOVE", "ALL", "PROFILE"}, {"FOLDER"}, 15),
            # "xóa hết fb3"
            ({"REMOVE", "ALL"}, {"PROFILE", "FOLDER"}, 11),
            # "xóa sạch fb3"
            ({"REMOVE"}, {"ALL", "PROFILE"}, 8),
        ],
        "needs_folder": True,
        "needs_no": {"DIE", "LIVE", "KEEP"},  # If mentions die/live → delete_die instead
    },
    "delete_die_all": {
        "formulas": [
            # "check toàn bộ die xóa live giữ"
            ({"REMOVE", "DIE"}, {"ALL", "CHECK", "KEEP", "LIVE"}, 10),
            # "dẹp hết mấy thằng chết"
            ({"REMOVE", "DIE", "ALL"}, {"CHECK"}, 12),
            # "check toàn bộ" alone (implicit = check + delete die)
            ({"CHECK", "ALL"}, {"REMOVE", "DIE"}, 8),
            # "loại bỏ toàn bộ die"
            ({"REMOVE", "ALL"}, {"DIE"}, 7),
            # "thanh lọc hết đi"  
            ({"REMOVE", "ALL"}, set(), 5),
        ],
        "needs_no_folder": True,  # If folder specified → delete_die_profiles instead
    },
    "delete_die_profiles": {
        "formulas": [
            ({"REMOVE", "DIE"}, {"CHECK", "KEEP", "LIVE"}, 10),
            ({"CHECK", "REMOVE"}, {"DIE"}, 8),
            ({"REMOVE"}, {"DIE", "CHECK"}, 5),
        ],
        "needs_folder": True,
    },
    # --- Create profiles ---
    "create_profiles": {
        "formulas": [
            ({"CREATE", "PROFILE"}, {"FOLDER"}, 15),
            ({"CREATE"}, {"PROFILE", "FOLDER"}, 10),
        ],
        "needs_folder": True,
    },
    # --- Delete duplicates (keep one per UID) ---
    "delete_duplicates": {
        "formulas": [
            ({"REMOVE", "DUPLICATE"}, {"KEEP", "FOLDER"}, 18),
            ({"DUPLICATE", "KEEP"}, {"REMOVE"}, 15),
            ({"DUPLICATE", "REMOVE"}, set(), 14),
        ],
        "needs_folder": True,
    },
    "check_duplicates": {
        "formulas": [
            ({"CHECK", "DUPLICATE"}, {"FOLDER"}, 12),
            ({"DUPLICATE"}, {"CHECK", "FOLDER"}, 8),
        ],
        "needs_folder": True,
        "needs_no": {"REMOVE", "KEEP"},  # If wants to remove → delete_duplicates
    },
    "batch_check_login": {
        "formulas": [
            ({"CHECK"}, {"PROFILE", "LIVE", "DIE", "WATCH"}, 8),
        ],
        "needs_folder": True,
        "needs_no": {"REMOVE"},  # Don't match if user also wants to delete
    },
    "check_fb_status": {
        "formulas": [
            ({"CHECK"}, {"LIVE", "DIE"}, 6),
        ],
        "needs_profile": True,
        "needs_no": {"REMOVE"},
    },
    "list_folders": {
        "formulas": [
            ({"COUNT", "PROFILE"}, set(), 8),
            ({"COUNT"}, {"FOLDER", "ALL"}, 7),
            ({"LIST", "FOLDER"}, set(), 8),
            ({"COUNT"}, set(), 5),
        ],
        "needs_no_profile": True,
        "needs_no_folder": True,  # If user specifies a folder, they want to CHECK it, not list all
    },
    "list_profiles": {
        "formulas": [
            ({"LIST", "PROFILE"}, {"FOLDER"}, 8),
        ],
    },
    "leave_groups": {
        "formulas": [
            ({"LEAVE", "GROUP"}, set(), 10),
            ({"REMOVE", "GROUP"}, set(), 9),
        ],
        "needs_profile": True,
    },
    "debug_groups": {
        "formulas": [
            ({"LIST", "GROUP"}, set(), 9),
            ({"CHECK", "GROUP"}, set(), 9),
            ({"COUNT", "GROUP"}, set(), 8),
        ],
        "needs_profile": True,
    },
    "watch_reels": {
        "formulas": [
            ({"WATCH", "REEL"}, {"COMMENT", "LIKE"}, 10),
        ],
        "needs_profile": True,
    },
    "upload_reel": {
        "formulas": [
            ({"UPLOAD", "REEL"}, set(), 15),  # Higher score than watch_reels
        ],
        "needs_profile": True,
        "needs_no": {"WATCH"},  # If also says "xem" → watch_reels instead
    },
    "fb_nurture_batch": {
        "formulas": [
            ({"NURTURE"}, {"PROFILE", "COMMENT", "LIKE", "FEED"}, 10),
            ({"FEED", "COMMENT"}, {"LIKE"}, 8),
            ({"FEED", "LIKE"}, set(), 7),
        ],
    },
    "fb_read_feed": {
        "formulas": [
            ({"WATCH", "FEED"}, set(), 8),
            ({"LIST", "FEED"}, set(), 7),
        ],
        "needs_profile": True,
    },
    "open_browser": {
        "formulas": [
            ({"OPEN", "BROWSER"}, set(), 10),
            ({"OPEN"}, {"BROWSER"}, 5),
        ],
        "needs_profile": True,
    },
    "close_browser": {
        "formulas": [
            ({"CLOSE", "BROWSER"}, set(), 10),
            ({"CLOSE"}, {"BROWSER"}, 5),
        ],
        "needs_profile": True,
    },
    "screenshot": {
        "formulas": [
            ({"SCREENSHOT"}, set(), 8),
        ],
        "needs_profile": True,
    },
    "vision_click": {
        "formulas": [
            ({"VISION", "CLICK"}, set(), 10),
            ({"CLICK"}, {"VISION"}, 6),
        ],
        "needs_profile": True,
    },
    "vision_capture": {
        "formulas": [
            ({"VISION", "SCREENSHOT"}, set(), 10),
            ({"VISION"}, set(), 6),
        ],
        "needs_profile": True,
    },
    "list_tags": {
        "formulas": [({"LIST", "TAG"}, set(), 8), ({"TAG"}, set(), 5)],
        "needs_no_profile": True,
    },
    "list_scripts": {
        "formulas": [({"LIST", "SCRIPT"}, set(), 8), ({"SCRIPT"}, set(), 5)],
        "needs_no_profile": True,
    },
    "list_campaigns": {
        "formulas": [({"LIST", "CAMPAIGN"}, set(), 8), ({"CAMPAIGN"}, set(), 5)],
        "needs_no_profile": True,
    },
    "get_running": {
        "formulas": [({"RUNNING"}, set(), 8)],
        "needs_no_profile": True,
    },
    "get_versions": {
        "formulas": [({"VERSION"}, set(), 8)],
        "needs_no_profile": True,
    },
    "agent_skills": {
        "formulas": [({"SKILL"}, set(), 8)],
    },
    "agent_task_history": {
        "formulas": [({"HISTORY"}, {"SKILL"}, 7)],
    },
}

# Agent catch-all concepts (used by AI prompt, kept for reference)
AGENT_CONCEPTS = {"NOTIFICATION", "FRIEND", "MESSAGE", "POST", "SEARCH", "FOLLOW"}


def _try_intercept(text: str) -> dict | None:
    """Hard-coded pre-AI intercept for intents that AI brain keeps getting wrong.
    Returns action dict if matched, None otherwise."""
    text_lower = text.lower().strip()
    
    # ===== UPLOAD REEL: "đăng reels", "upload reel", "đăng video ngắn" =====
    upload_words = ["đăng reel", "upload reel", "đăng video ngắn", "up reel", "đăng lên reel"]
    reel_words = ["reel", "reels"]
    dang_words = ["đăng", "upload", "tải lên", "up "]
    
    is_upload_reel = False
    # Direct phrase match
    if any(w in text_lower for w in upload_words):
        is_upload_reel = True
    # Combo: đăng/upload + reel (but NOT "đăng bài" which is compose_post)
    elif any(d in text_lower for d in dang_words) and any(r in text_lower for r in reel_words):
        if "đăng bài" not in text_lower:  # Avoid false positive with compose_post
            is_upload_reel = True
    
    if is_upload_reel:
        # Extract profile
        prof_match = re.search(r'\b[sS]\s*(\d+)\b', text)
        if not prof_match:
            prof_match = re.search(r'\b[aA]\s*(\d+)\b', text)
        profile = f"S{prof_match.group(1)}" if prof_match else None
        if not profile:
            return None  # Need a profile, let AI handle
        
        # Build task description preserving the full user text
        task = text  # Pass the full original text as task, agent will parse it
        return {
            "action": "agent_execute",
            "params": {"profile": profile, "task": task},
            "reply": f"🎬 Đang upload reel cho {profile}..."
        }
    
    # ===== CREATE PAGE: "tạo page", "tạo trang", "create page" =====
    create_page_words = ["tạo page", "tạo trang", "create page", "tạo fanpage"]
    is_create_page = any(w in text_lower for w in create_page_words)
    
    if is_create_page:
        prof_match = re.search(r'\b[sS]\s*(\d+)\b', text)
        if not prof_match:
            prof_match = re.search(r'\b[aA]\s*(\d+)\b', text)
        profile = f"S{prof_match.group(1)}" if prof_match else None
        if not profile:
            return None  # Need a profile, let AI handle
        
        task = text
        return {
            "action": "agent_execute",
            "params": {"profile": profile, "task": task},
            "reply": f"📄 Đang tạo Page cho {profile}..."
        }
    
    # ===== LIST FB PAGES: "bao nhiêu page", "mấy page", "xem page", "page đã tạo" =====
    list_page_words = ["bao nhiêu page", "mấy page", "xem page", "list page",
                       "page đã tạo", "trang đã tạo", "có page nào", "các page",
                       "danh sách page", "liệt kê page", "bao nhiêu trang", "mấy trang"]
    is_list_pages = any(w in text_lower for w in list_page_words)
    
    if is_list_pages:
        prof_match = re.search(r'\b[sS]\s*(\d+)\b', text)
        if not prof_match:
            prof_match = re.search(r'\b[aA]\s*(\d+)\b', text)
        profile = f"S{prof_match.group(1)}" if prof_match else None
        if not profile:
            return None  # Need a profile, let AI handle
        
        task = text
        return {
            "action": "agent_execute",
            "params": {"profile": profile, "task": task},
            "reply": f"📋 Đang xem danh sách Page của {profile}..."
        }
    
    return None


def try_quick_parse(text: str, chat_id: str = None) -> dict | None:
    """Handle ONLY trivial follow-ups (confirm/retry/question).
    Everything else → AI brain (GPT-5) decides."""
    text_lower = text.lower().strip()
    if not text_lower or len(text_lower) > 80:
        return None  # Long messages always → AI
    
    concepts = _detect_concepts(text_lower)
    
    # 0. STOP command — instant, no AI needed (word boundary matching to avoid false positives)
    stop_phrases = ['stop all', 'tắt hết', 'dừng lại', 'ngừng lại', 'tắt tất cả', 'dừng hết', 'ngừng hết']
    stop_words_exact = ['stop', 'dừng', 'ngừng', 'cancel']
    if any(sp in text_lower for sp in stop_phrases) or \
       any(re.search(r'(?:^|\s)' + re.escape(sw) + r'(?:$|\s)', text_lower) for sw in stop_words_exact):
        return {"action": "stop_all", "params": {}, "reply": "🛑 Đang tắt hết..."}
    
    # Substantive action concepts → AI handles (not a simple follow-up)
    action_concepts = concepts - {"CONFIRM", "RETRY", "RESULT", "QUESTION", "ALL", "KEEP", "LIVE", "DIE"}
    
    ctx = _get_last_context(chat_id) if chat_id else {}
    last_folder = ctx.get("folder")
    
    # 1. Pure QUESTION/RESULT only (no action verbs): "xong chưa?", "sao rồi?"
    if ("QUESTION" in concepts or "RESULT" in concepts) and not action_concepts and len(text_lower) < 50:
        folder_match = re.search(r'fb\s*(\d+)', text_lower)
        verify_folder = f"fb{folder_match.group(1)}" if folder_match else last_folder
        verify_profile = None
        pm = re.search(r'\b[sS]\s*(\d+)\b', text_lower)
        if pm:
            verify_profile = f"S{pm.group(1)}"
        elif ctx.get("profile"):
            verify_profile = ctx["profile"]
        
        question_topic = None
        if ctx.get("action") in ("xóa", "delete_die", "delete_all"):
            question_topic = "delete"
        elif ctx.get("action") in ("batch_check", "check"):
            question_topic = "check"
        elif ctx.get("action") == "list":
            question_topic = "count"
        
        return {"action": "verify_status", "params": {
            "topic": question_topic,
            "folder_id": verify_folder,
            "profile": verify_profile,
            "original_question": text_lower,
            "last_result": ctx.get("last_result", ""),
        }, "reply": "🔍 Đang kiểm tra..."}
    
    # 2. Pure CONFIRM (< 15 chars, no action): "ok", "ừ", "làm đi"
    if "CONFIRM" in concepts and len(text_lower) < 15 and not action_concepts:
        if last_folder:
            ctx_action = ctx.get("action")
            if ctx_action == "delete_all":
                return {"action": "delete_all_profiles", "params": {"folder_id": last_folder},
                        "reply": f"✅ OK, đang xóa hết {last_folder}..."}
            elif ctx_action in ("delete_die", "delete_die_profiles"):
                return {"action": "delete_die_profiles", "params": {"folder_id": last_folder, "concurrency": 10},
                        "reply": f"✅ OK, đang xóa die {last_folder}..."}
            # For other actions (list, check, etc.) — let AI handle, don't auto-delete
        return None  # No context or non-delete context → AI handles
    
    # 3. Pure RETRY (< 20 chars): "làm lại", "thử lại" — re-run last action, not auto-delete
    if "RETRY" in concepts and len(text_lower) < 20 and not action_concepts:
        if last_folder and ctx.get("action"):
            ctx_action = ctx["action"]
            if ctx_action in ("delete_die", "delete_die_profiles", "delete_die_all"):
                return {"action": "delete_die_profiles", "params": {"folder_id": last_folder, "concurrency": 10}, "reply": f"🔄 Chạy lại xóa die {last_folder}..."}
            elif ctx_action in ("batch_check", "check", "batch_check_login"):
                return {"action": "batch_check_login", "params": {"folder_id": last_folder, "concurrency": 10}, "reply": f"🔄 Check lại {last_folder}..."}
            elif ctx_action in ("list", "list_profiles"):
                return {"action": "list_profiles", "params": {"folder_id": last_folder}, "reply": f"🔄 Xem lại {last_folder}..."}
        return None
    
    # EVERYTHING ELSE → AI brain handles
    return None


def _build_action(intent: str, concepts: set, profile: str, folder_id: str,
                   concurrency: int, text_lower: str, raw: str) -> dict:
    """Build the action dict for the winning intent."""
    
    if intent == "delete_all_profiles":
        return {"action": "delete_all_profiles", "params": {"folder_id": folder_id},
                "reply": f"🗑️ Đang xóa TẤT CẢ profiles trong {folder_id}..."}
    
    if intent == "delete_die_all":
        return {"action": "delete_die_all", "params": {"concurrency": concurrency},
                "reply": f"🗑️ Check TOÀN BỘ ({concurrency} luồng), die xóa live giữ..."}
    
    if intent == "delete_die_profiles":
        return {"action": "delete_die_profiles", "params": {"folder_id": folder_id, "concurrency": concurrency},
                "reply": f"🗑️ Check {folder_id} ({concurrency} luồng), die xóa live giữ..."}
    
    if intent == "batch_check_login":
        return {"action": "batch_check_login", "params": {"folder_id": folder_id, "concurrency": concurrency},
                "reply": f"🔍 Đang check {folder_id} ({concurrency} luồng)..."}

    if intent == "delete_duplicates":
        return {"action": "delete_duplicates", "params": {"folder_id": folder_id, "concurrency": concurrency},
                "reply": f"🗑️ Đang tìm và xóa profile trùng trong {folder_id}, giữ 1 bản..."}

    if intent == "check_duplicates":
        return {"action": "check_duplicates", "params": {"folder_id": folder_id, "concurrency": concurrency},
                "reply": f"🔍 Đang kiểm tra trùng lặp trong {folder_id}..."}

    if intent == "create_profiles":
        # Extract count from text
        count_match = re.search(r'(\d+)\s*(?:profile|cái|con|acc)', text_lower)
        if not count_match:
            count_match = re.search(r'(\d+)', text_lower)
        count = int(count_match.group(1)) if count_match else 1
        return {"action": "create_profiles", "params": {"folder_id": folder_id, "count": count, "name_prefix": "Profile"},
                "reply": f"✨ Đang tạo {count} profile trong {folder_id}..."}

    if intent == "check_fb_status":
        return {"action": "check_fb_status", "params": {"profile": profile},
                "reply": f"🔍 Đang check {profile}..."}
    
    if intent == "list_folders":
        return {"action": "list_folders", "params": {}, "reply": "📁 Đang lấy danh sách thư mục..."}
    
    if intent == "list_profiles":
        return {"action": "list_profiles", "params": {"folder_id": folder_id},
                "reply": "📱 Đang lấy danh sách profiles..."}
    
    if intent == "leave_groups":
        return {"action": "leave_groups", "params": {"profile": profile},
                "reply": f"⏳ Đang thoát hết nhóm cho {profile}..."}
    
    if intent == "debug_groups":
        return {"action": "debug_groups", "params": {"profile": profile},
                "reply": f"🔍 Đang xem groups {profile}..."}
    
    if intent == "watch_reels":
        do_comment = "COMMENT" in concepts
        count_match = re.search(r'(\d+)\s*(?:cái|reel|video)', text_lower)
        count = int(count_match.group(1)) if count_match else 5
        return {"action": "watch_reels", "params": {"profile": profile, "count": count,
                "comment": do_comment, "comment_count": 3},
                "reply": f"🎬 Đang xem {count} reels cho {profile}..."}
    
    if intent == "fb_nurture_batch":
        profiles_list = [profile] if profile else []
        return {"action": "fb_nurture_batch", "params": {"profiles": profiles_list, "max_workers": 1,
                "comments_per_profile": 2},
                "reply": f"🌱 Đang nuôi {profile or 'profiles'}..."}
    
    if intent == "fb_read_feed":
        return {"action": "fb_read_feed", "params": {"profile": profile, "scroll_count": 3},
                "reply": f"📰 Đang đọc feed {profile}..."}
    
    if intent == "open_browser":
        return {"action": "open_browser", "params": {"profile": profile},
                "reply": f"🌐 Đang mở browser {profile}..."}
    
    if intent == "close_browser":
        return {"action": "close_browser", "params": {"profile": profile},
                "reply": f"🔒 Đang đóng browser {profile}..."}
    
    if intent == "screenshot":
        return {"action": "screenshot", "params": {"profile": profile},
                "reply": f"📸 Đang chụp {profile}..."}
    
    if intent == "vision_click":
        target_match = re.search(r'(?:click|bấm|nhấn|tìm|ấn|chọn)\s+(?:vào\s+)?(?:nút\s+)?(.+?)$', text_lower)
        target = target_match.group(1).strip() if target_match else ""
        if not target:
            return None  # Let AI extract
        return {"action": "vision_click", "params": {"profile": profile, "target": target},
                "reply": f"🎯 Đang tìm và click '{target}'..."}
    
    if intent == "vision_capture":
        return {"action": "vision_capture", "params": {"profile": profile},
                "reply": f"📸 Đang phân tích giao diện {profile}..."}
    
    if intent == "list_tags":
        return {"action": "list_tags", "params": {}, "reply": "🏷️ Đang lấy danh sách tags..."}
    if intent == "list_scripts":
        return {"action": "list_scripts", "params": {}, "reply": "📜 Đang lấy danh sách scripts..."}
    if intent == "list_campaigns":
        return {"action": "list_campaigns", "params": {}, "reply": "📋 Đang lấy danh sách campaigns..."}
    if intent == "get_running":
        return {"action": "get_running", "params": {}, "reply": "🟢 Đang xem profiles đang chạy..."}
    if intent == "get_versions":
        return {"action": "get_versions", "params": {}, "reply": "🌐 Đang xem phiên bản browser..."}
    if intent == "agent_skills":
        return {"action": "agent_skills", "params": {}, "reply": "🧠 Đang xem skills..."}
    if intent == "agent_task_history":
        return {"action": "agent_task_history", "params": {"limit": 10}, "reply": "📜 Đang xem lịch sử..."}
    
    return None


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors and notify user."""
    logger.error(f"Exception: {context.error}")
    if update and hasattr(update, 'effective_chat'):
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"⚠️ Lỗi hệ thống, thử lại nhé: {str(context.error)[:100]}"
            )
        except Exception:
            pass


def main():
    """Start the bot."""
    # Increase timeouts + connection pool for stability
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=30.0,
        pool_timeout=15.0,
        connection_pool_size=20,
    )
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .concurrent_updates(True)
        .build()
    )
    
    # Error handler
    app.add_error_handler(error_handler)
    
    # Commands (still work)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("open_browser", open_browser_cmd))
    app.add_handler(CommandHandler("profiles", profiles_cmd))
    app.add_handler(CommandHandler("debug_groups", debug_groups_cmd))
    app.add_handler(CommandHandler("leave_groups", leave_groups_cmd))
    
    # NLP text handler (catches everything else)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    logger.info("🚀 Bot started (Pollinations 5-model rotation + JanAI fallback + FB Manager Pro)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
