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
from telegram import Update
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
    {"model": "openai",      "name": "GPT-5 Mini",        "timeout": 30},   # Best overall
    {"model": "deepseek",    "name": "DeepSeek V3.2",     "timeout": 40},   # Great reasoning
    {"model": "claude-fast", "name": "Claude Haiku 4.5",  "timeout": 30},   # Very accurate
    {"model": "mistral",     "name": "Mistral Small 3.2", "timeout": 30},   # Good & cheap
    {"model": "openai-fast", "name": "GPT-5 Nano",        "timeout": 25},   # Fast backup
]

# Track rotation state + cooldowns
import time as _time
_key_index = 0       # Start with best key (3 pollen)
_model_index = 0     # Start with best model
_key_cooldowns = {}   # key -> cooldown_until timestamp
_model_cooldowns = {} # model -> cooldown_until timestamp

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
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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
SYSTEM_PROMPT = """You are AI CUTE - a Vietnamese AI assistant for FB Manager Pro (Facebook profile manager).
Parse user message → return JSON action. NEVER return plain text.

CORE RULES:
1. ALWAYS return valid JSON: {"action": "...", "params": {...}, "reply": "..."}
2. Use conversation history to resolve "nó", "cái đó", "folder kia", etc.
3. When user wants action (xóa/check/mở...) → return action, NEVER just chat about it
4. Short follow-ups ("xóa đi", "làm luôn") → infer from history, execute
5. Never ask "chọn 1,2,3" — just DO the best action

ACTIONS (key ones):
- delete_die_all: Check ALL folders, delete DIE profiles {"concurrency": N}
- delete_die_profiles: Check+delete DIE in 1 folder {"folder_id": "fb3", "concurrency": N}
- batch_check_login: Check login status of folder {"folder_id": "fb1", "concurrency": N}
- list_folders: List folders + counts
- list_profiles: List profiles in folder {"folder_id": "fb1"}
- open_browser/close_browser: {"profile": "S10"}
- check_fb_status: Full check 1 profile {"profile": "S10"}
- leave_groups/debug_groups: {"profile": "S10"}
- watch_reels: {"profile": "S10", "count": 5, "comment": true}
- fb_nurture_batch: {"profiles": [...], "max_workers": 3}
- agent_execute: ANY browser task {"profile": "S10", "task": "description"}
- chat: Just talking, no action {"reply": "..."}

VIETNAMESE INTENT MAP:
- "check/kiểm tra toàn bộ" + "die xóa/live giữ" → delete_die_all
- "còn bao nhiêu/tổng" → list_folders
- "xóa hết/xóa luôn/xóa đi" (after check) → delete from context
- "nuôi/dưỡng" → fb_nurture_batch
- "luồng/thread" → concurrency param
- fb1/fb2/fb3 = folder names, S10/A200 = profile names

OUTPUT FORMAT: {"action": "NAME", "params": {}, "reply": "Việt namếse reply"}
For chat: {"action": "chat", "params": {}, "reply": "Friendly Vietnamese reply"}
"""


# ============================================================
# AI providers (Key rotation → Model fallback → JanAI)
# ============================================================
async def call_pollinations(messages: list, max_tokens: int = 300, temperature: float = 0.1) -> str:
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
                        content = data["choices"][0]["message"]["content"]
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


async def call_janai(messages: list, max_tokens: int = 500, temperature: float = 0.3) -> str:
    """Call Devstral-24B via JanAI (local fallback, FREE)."""
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": DEVSTRAL_MODEL,
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
                    content = data["choices"][0]["message"]["content"]
                    logger.info("JanAI fallback OK")
                    return content
                else:
                    error_text = await resp.text()
                    logger.error(f"JanAI {resp.status}: {error_text[:300]}")
                    return f'{{"action":"chat","params":{{}},"reply":"Lỗi AI: cả Pollinations lẫn JanAI đều fail"}}'
    except Exception as e:
        logger.error(f"JanAI error: {e}")
        return f'{{"action":"chat","params":{{}},"reply":"Lỗi kết nối AI: {e}"}}'


async def call_ai(messages: list, max_tokens: int = 300, temperature: float = 0.1) -> str:
    """Try Pollinations rotation (5 models), fallback to JanAI (local, free)."""
    result = await call_pollinations(messages, max_tokens, temperature)
    if result is not None:
        return result
    logger.info("All Pollinations models exhausted, falling back to JanAI...")
    return await call_janai(messages, max_tokens, temperature)


async def call_fb_api(endpoint: str, method: str = "POST", data: dict = None) -> dict:
    """Call FB Manager Pro API."""
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{FB_MANAGER_API}{endpoint}"
            if method == "POST":
                async with session.post(url, json=data or {}, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                    return await resp.json()
            else:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    return await resp.json()
    except Exception as e:
        logger.error(f"FB API error: {e}")
        return {"error": str(e)}


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
    # Try to find raw JSON object
    if not json_match and not text.startswith('['):
        json_match2 = re.search(r'(\{[^{}]*"action"[^{}]*\})', text, re.DOTALL)
        if json_match2:
            text = json_match2.group(1)
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
        folder_id = params.get("folder_id")
        concurrency = params.get("concurrency", 5)
        if not folder_id:
            return "❌ Thiếu folder. VD: 'check fb2 bao nhiêu live'"
        # Step 1: Get profiles in folder
        list_result = await call_fb_api("/list_profiles", data={"folder_id": folder_id, "page_size": 300})
        if "error" in list_result:
            return f"❌ Lỗi lấy profiles: {list_result['error']}"
        profiles_list = list_result.get("data", [])
        if isinstance(profiles_list, dict):
            profiles_list = profiles_list.get("content", [])
        if not profiles_list or not isinstance(profiles_list, list):
            return f"📱 Không tìm thấy profiles nào trong {folder_id}."
        
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
            return f"❌ Lỗi batch check: {batch_result['error']}"
        
        # Parse results
        results = batch_result.get("results", [])
        summary = batch_result.get("summary", {})
        workers = batch_result.get("workers", concurrency)
        
        live_list = []
        die_list = []
        locked_list = []
        error_list = []
        
        for r in results:
            name = r.get("name", r.get("profile_uuid", "?")[:20])
            status = r.get("status", "ERROR")
            detail = r.get("detail", "")
            if status == "LIVE":
                live_list.append(f"{name} ({detail})" if detail and detail != status else name)
            elif status in ("DIE", "NOT_LOGGED_IN"):
                die_list.append(name)
            elif status in ("LOCKED", "2FA"):
                locked_list.append(f"{name} ({status})")
            else:
                error_list.append(f"{name}: {status}:{detail}" if detail else f"{name}: {status}")
        
        text = f"📊 **Check Login - {folder_id}** ({total} profiles, {workers} luồng)\n\n"
        text += f"✅ Live: {len(live_list)}\n"
        text += f"❌ Die: {len(die_list)}\n"
        if locked_list:
            text += f"🔒 Locked/2FA: {len(locked_list)}\n"
        if error_list:
            text += f"⚠️ Lỗi: {len(error_list)}\n"
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
            for e in error_list[:10]:
                text += f"• `{e}`\n"
        
        # Suggest next action if die found
        if die_list:
            text += f"\n💡 Gõ **'xóa die {folder_id}'** để xóa {len(die_list)} profiles die"
        return text

    # ===== DELETE DIE PROFILES =====
    if act == "delete_die_all":
        # Delete die profiles across ALL folders
        concurrency = params.get("concurrency", 5)
        # Get all folders
        folders_result = await call_fb_api("/list_folders", data={})
        if "error" in folders_result:
            return f"❌ Lỗi: {folders_result['error']}"
        folders = folders_result.get("folders", [])
        if not folders:
            return "📁 Không có thư mục nào."

        total_text = f"🗑️ **Xóa DIE toàn bộ ({len(folders)} thư mục)**\n\n"
        grand_live = 0
        grand_die = 0
        grand_deleted = 0
        grand_locked = 0

        for fi, folder in enumerate(folders):
            fname = folder.get("name", "?")
            fid = folder.get("id", "")
            ftotal = folder.get("total_browser", 0)
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
            list_result = await call_fb_api("/list_profiles", data={"folder_id": str(fid), "page_size": 300})
            if "error" in list_result:
                # Retry once after short delay (SQLITE_ERROR can be transient)
                await asyncio.sleep(2)
                list_result = await call_fb_api("/list_profiles", data={"folder_id": str(fid), "page_size": 300})
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
        return total_text

    if act == "delete_die_profiles":
        folder_id = params.get("folder_id")
        if not folder_id:
            return "❌ Thiếu folder. VD: 'xóa die fb1'"
        concurrency = params.get("concurrency", 5)

        # Step 1: Get profiles
        list_result = await call_fb_api("/list_profiles", data={"folder_id": folder_id, "page_size": 300})
        if "error" in list_result:
            return f"❌ Lỗi: {list_result['error']}"
        profiles_list = list_result.get("data", [])
        if isinstance(profiles_list, dict):
            profiles_list = profiles_list.get("content", [])
        if not profiles_list:
            return f"📱 Không tìm thấy profiles nào trong {folder_id}."

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
            return f"❌ Lỗi check: {batch_result['error']}"

        results = batch_result.get("results", [])

        # Collect die profiles
        die_uuids = []
        die_names = []
        live_count = 0
        locked_count = 0
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

        if not die_uuids:
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

        text = f"🗑️ **Xóa DIE - {folder_id}**\n\n"
        text += f"📊 Tổng: {total} profiles\n"
        text += f"✅ Live: {live_count}\n"
        if locked_count:
            text += f"🔒 Locked: {locked_count}\n"
        text += f"❌ Die: {len(die_names)}\n\n"
        text += f"🗑️ **Đã xóa {deleted} profiles:**\n"
        for n in die_names[:20]:
            text += f"• `{n}`\n"
        if len(die_names) > 20:
            text += f"... +{len(die_names)-20} khác\n"
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
        result = await call_fb_api("/agent_execute", data=data)
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

    return reply


# ============================================================
# Telegram handlers
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 **FB Manager Pro Bot** (AI-powered)\n\n"
        "Nói chuyện tự nhiên, tui tự hiểu:\n"
        "• `s10 mở browser` / `đóng browser s10`\n"
        "• `s10 thoát hết nhóm` / `xem groups s10`\n"
        "• `check status s10` / `check fb1`\n"
        "• `s10 xem 5 reels` / `nuôi s10`\n"
        "• `đọc feed s10` / `chụp s10`\n"
        "• `vision click nút Like` trên s10\n"
        "• `liệt kê profiles fb1` / `thư mục`\n"
        "• `tags` / `scripts` / `campaigns`\n"
        "• `đang chạy` / `xóa die fb1`\n\n"
        "Gõ /help để xem đầy đủ.",
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
        "/open\\_browser S10 · /leave\\_groups S10\n"
        "/debug\\_groups S10 · /profiles",
        parse_mode="Markdown"
    )


async def open_browser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("VD: `/open_browser S10`", parse_mode="Markdown")
        return
    profile = " ".join(context.args)
    msg = await update.message.reply_text(f"🌐 Đang mở browser {profile}...")
    result = await execute_action({"action": "open_browser", "params": {"profile": profile}})
    await msg.edit_text(result, parse_mode="Markdown")


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
    try:
        await msg.edit_text(result, parse_mode="Markdown")
    except Exception:
        pass  # Message may already be updated by API


async def debug_groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("VD: `/debug_groups S10`", parse_mode="Markdown")
        return
    profile = " ".join(context.args)
    msg = await update.message.reply_text(f"🔍 Đang kiểm tra groups {profile}...")
    result = await execute_action({"action": "debug_groups", "params": {"profile": profile}})
    await msg.edit_text(result, parse_mode="Markdown")


async def profiles_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("📱 Đang lấy danh sách...")
    folder_id = context.args[0] if context.args else None
    result = await execute_action({"action": "list_profiles", "params": {"folder_id": folder_id}})
    await msg.edit_text(result, parse_mode="Markdown")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """NLP handler: parse user intent via Devstral, then execute."""
    try:
        await _message_handler_inner(update, context)
    except Exception as e:
        logger.error(f"message_handler error: {e}")
        try:
            await update.message.reply_text(f"⚠️ Lỗi: {str(e)[:200]}")
        except Exception:
            pass


async def _message_handler_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inner handler with actual logic."""
    user_msg = update.message.text
    logger.info(f"User: {user_msg}")
    chat_id = str(update.effective_chat.id)

    # Quick pattern matching for common commands (no AI needed)
    quick = try_quick_parse(user_msg, chat_id=chat_id)
    if quick:
        logger.info(f"Quick parse: {quick}")
        # Save to history so AI has context
        add_to_history(chat_id, "user", user_msg)
        msg = await update.message.reply_text(quick.get("reply", "⏳ Đang xử lý..."))
        result = await execute_action(quick, telegram_chat_id=chat_id, telegram_message_id=msg.message_id)
        add_to_history(chat_id, "assistant", result[:300] if result else "Done")
        try:
            await msg.edit_text(result, parse_mode="Markdown")
        except Exception:
            try:
                await msg.edit_text(result)
            except Exception:
                pass
        return

    # Send to AI for NLP parsing (with conversation history)
    msg = await update.message.reply_text("🤔 Đang suy nghĩ...")
    
    # Build messages with history for context
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    # Add conversation history (last N exchanges)
    history = get_history(chat_id)
    messages.extend(history)
    messages.append({"role": "user", "content": user_msg})
    # Save user message to history
    add_to_history(chat_id, "user", user_msg)
    
    ai_response = await call_ai(messages, max_tokens=800, temperature=0.4)
    logger.info(f"AI raw: {ai_response[:300]}")
    
    parsed = parse_ai_response(ai_response)
    
    # === JSON RETRY: If AI returned plain text (chat fallback) but user wanted an action ===
    if isinstance(parsed, dict) and parsed.get("action") == "chat":
        # Check if the original message seems like an action request
        action_keywords = r'check|xóa|mở|đóng|xem|list|kiểm|nuôi|lướt|agent|thoát|delete|scan|bật|chạy|open|close'
        if re.search(action_keywords, user_msg.lower()):
            # AI returned chat but user wanted action → retry with stronger JSON instruction
            logger.warning("AI returned chat for action-like message, retrying with JSON nudge...")
            retry_messages = messages.copy()
            retry_messages.append({"role": "assistant", "content": ai_response})
            retry_messages.append({"role": "user", "content": "RESPOND WITH JSON ONLY. Format: {\"action\": \"...\", \"params\": {...}, \"reply\": \"...\"}. NO plain text."})
            retry_response = await call_ai(retry_messages, max_tokens=500, temperature=0.1)
            retry_parsed = parse_ai_response(retry_response)
            if isinstance(retry_parsed, dict) and retry_parsed.get("action") != "chat":
                parsed = retry_parsed
                logger.info(f"JSON retry success: {parsed.get('action')}")
    
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
        await msg.edit_text("\n".join(status_lines))
        
        # Create separate messages for each action
        action_msgs = []
        for i, a in enumerate(actions):
            if i == 0:
                action_msgs.append(msg)  # Reuse first message
            else:
                new_msg = await update.message.reply_text(a.get("reply", "⏳ Đang xử lý..."))
                action_msgs.append(new_msg)
        
        # Run all actions concurrently
        async def run_single_action(action, action_msg):
            try:
                result = await execute_action(action, telegram_chat_id=chat_id, telegram_message_id=action_msg.message_id)
                try:
                    await action_msg.edit_text(result, parse_mode="Markdown")
                except Exception:
                    try:
                        await action_msg.edit_text(result)
                    except Exception:
                        pass
                return result
            except Exception as e:
                error_text = f"❌ Lỗi: {e}"
                try:
                    await action_msg.edit_text(error_text)
                except Exception:
                    pass
                return error_text
        
        results = await asyncio.gather(*[
            run_single_action(actions[i], action_msgs[i]) 
            for i in range(len(actions))
        ])
        
        # Save summary to history
        summary = " | ".join([r[:100] if r else "Done" for r in results])
        add_to_history(chat_id, "assistant", summary[:300])
        return

    # Single action (original flow)
    action = parsed if isinstance(parsed, dict) else parsed[0] if isinstance(parsed, list) else {"action": "chat", "params": {}, "reply": str(parsed)}
    logger.info(f"Parsed action: {action.get('action')} params={action.get('params')}")
    
    # If just chat, return AI reply directly
    if action.get("action") == "chat":
        reply_text = action.get('reply', ai_response)
        # Don't prefix with robot emoji - feel more natural
        if not reply_text or len(reply_text.strip()) < 3:
            reply_text = "Bác nói rõ hơn tui nghe với 😄"
        add_to_history(chat_id, "assistant", reply_text)
        await msg.edit_text(reply_text)
        return
    
    # Execute the action
    try:
        await msg.edit_text(action.get("reply", "⏳ Đang thực hiện..."))
    except Exception as e:
        logger.warning(f"edit_text reply failed: {e}")
    result = await execute_action(action, telegram_chat_id=chat_id, telegram_message_id=msg.message_id)
    # Save result summary to history — but keep it short to avoid AI copying raw output
    act_name = action.get('action', 'unknown')
    if act_name == 'agent_execute':
        # Don't pollute history with agent raw output (AI copies it)
        add_to_history(chat_id, "assistant", f"[Agent done: {action.get('params',{}).get('task','')}]")
    else:
        add_to_history(chat_id, "assistant", result[:300] if result else "Done")
    try:
        await msg.edit_text(result, parse_mode="Markdown")
    except Exception:
        # Retry without Markdown if parse fails
        try:
            await msg.edit_text(result)
        except Exception as e2:
            logger.warning(f"edit_text result failed: {e2}")


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
        # Extract last action
        if not ctx["action"]:
            for kw in ["delete_die", "batch_check", "check", "xóa", "list"]:
                if kw in c_lower:
                    ctx["action"] = kw
                    break
        # Extract last result summary
        if not ctx["last_result"] and msg.get("role") == "assistant":
            ctx["last_result"] = content[:200]
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
    "LIST":      ["list", "liệt kê", "danh sách", "xem", "show", "hiển thị", "có gì", "có những gì"],
    "COUNT":     ["bao nhiêu", "mấy", "tổng", "count", "đếm", "còn", "tồn tại", "số lượng", "total"],
    "NURTURE":   ["nuôi", "nurture", "dưỡng", "warm", "chăm", "chăm sóc", "tương tác", "farming", "farm"],
    "LEAVE":     ["thoát", "leave", "rời", "out", "bỏ", "hủy", "unfollow"],
    "WATCH":     ["xem", "watch", "lướt", "coi", "mở"],
    
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
    "batch_check_login": {
        "formulas": [
            ({"CHECK"}, {"PROFILE", "LIVE", "DIE"}, 6),
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

# Agent catch-all concepts — if profile + any of these, → agent_execute
AGENT_CONCEPTS = {"NOTIFICATION", "FRIEND", "MESSAGE", "POST", "SEARCH", "FOLLOW"}


def try_quick_parse(text: str, chat_id: str = None) -> dict | None:
    """Smart Vietnamese intent detection using concept scoring."""
    text_lower = text.lower().strip()
    raw = text.strip()
    
    if not text_lower:
        return None
    
    # ===== DETECT CONCEPTS =====
    concepts = _detect_concepts(text_lower)
    
    # ===== EXTRACT ENTITIES =====
    profile_matches = re.findall(r'\b[sS]\s*(\d+)\b', raw)
    a_matches = re.findall(r'\b[aA]\s*(\d+)\b', raw)
    
    if len(profile_matches) + len(a_matches) > 1:
        return None  # Multi-profile → let AI handle
    
    profile = None
    if profile_matches:
        profile = f"S{profile_matches[0]}"
    elif a_matches:
        profile = f"A{a_matches[0]}"
    
    folder_match = re.search(r'fb\s*(\d+)', text_lower)
    folder_id = f"fb{folder_match.group(1)}" if folder_match else None
    if not folder_id:
        named = re.search(r'\b(fb\s*ok|fbok|đông\s*hưng)\b', text_lower)
        if named:
            n = named.group(1).strip().lower()
            folder_map = {'fb ok': 'fb5', 'fbok': 'fb5', 'đông hưng': 'fb7'}
            folder_id = folder_map.get(n, n)
    
    concurrency = _extract_concurrency(text_lower)
    
    # ===== FOLLOW-UP / SHORT COMMANDS (use history context) =====
    ctx = _get_last_context(chat_id) if chat_id else {}
    last_folder = ctx.get("folder")
    last_profile = ctx.get("profile")
    
    # Pure follow-up: only CONFIRM/RETRY/RESULT concepts, nothing else substantive
    action_concepts = concepts - {"CONFIRM", "RETRY", "RESULT", "ALL", "KEEP", "LIVE"}
    
    if len(text_lower) < 40 and not action_concepts:
        # "ok", "làm đi", "tiếp", "chạy luôn"
        if "CONFIRM" in concepts:
            if last_folder:
                return {"action": "delete_die_profiles", "params": {"folder_id": last_folder, "concurrency": 10},
                        "reply": f"✅ OK, đang thực hiện cho {last_folder}..."}
            return None
        
        # "làm lại", "thử lại", "check lại"
        if "RETRY" in concepts:
            if last_folder:
                return {"action": "delete_die_all", "params": {"concurrency": 10}, "reply": "🔄 Chạy lại..."}
            return None
        
        # "rồi sao?", "xong chưa?", "kết quả?"
        if "RESULT" in concepts or text_lower.strip('?!. ') == '':
            last_result = ctx.get("last_result", "")
            if last_result:
                return {"action": "chat", "params": {}, "reply": f"Kết quả lần trước: {last_result[:300]}"}
            return {"action": "chat", "params": {}, "reply": "Chưa có kết quả nào trước đó bác ơi. Bác muốn check gì?"}
    
    # Short "xóa/remove" without explicit target → from context
    if len(text_lower) < 30 and "REMOVE" in concepts and not folder_id and not profile:
        if last_folder:
            return {"action": "delete_die_profiles", "params": {"folder_id": last_folder, "concurrency": 10},
                    "reply": f"🗑️ Xóa die {last_folder}..."}
        return {"action": "delete_die_all", "params": {"concurrency": 10}, "reply": "🗑️ Xóa die toàn bộ..."}
    
    # ===== AGENT PRIORITY: If profile + specific agent targets → agent_execute =====
    # These override generic "check/xem" intents
    if profile and concepts & AGENT_CONCEPTS:
        task = re.sub(r'\b[sS]\s*\d+\b', '', raw).strip()
        task = re.sub(r'\b[aA]\s*\d+\b', '', task).strip()
        if task:
            return {"action": "agent_execute", "params": {"profile": profile, "task": task},
                    "reply": f"🤖 Agent đang thực hiện cho {profile}..."}
    
    # ===== SCORE ALL INTENTS =====
    scores = {}
    for intent, rule in INTENT_RULES.items():
        # Check entity constraints
        if rule.get("needs_profile") and not profile:
            continue
        if rule.get("needs_folder") and not folder_id:
            continue
        if rule.get("needs_no_profile") and profile:
            continue
        if rule.get("needs_no_folder") and folder_id:
            # Special: delete_die_all needs no folder, but if folder → use delete_die_profiles
            continue
        if rule.get("needs_no"):
            if concepts & rule["needs_no"]:
                continue
        
        best_score = 0
        for required, optional, base in rule["formulas"]:
            if required.issubset(concepts):
                score = base + len(concepts & optional) * 2
                best_score = max(best_score, score)
        
        if best_score > 0:
            scores[intent] = best_score
    
    if not scores:
        # Check agent catch-all
        if profile and concepts & AGENT_CONCEPTS:
            task = re.sub(r'\b[sS]\s*\d+\b', '', raw).strip()
            task = re.sub(r'\b[aA]\s*\d+\b', '', task).strip()
            if task:
                return {"action": "agent_execute", "params": {"profile": profile, "task": task},
                        "reply": f"🤖 Agent đang thực hiện cho {profile}..."}
        return None  # Let AI handle
    
    # Pick highest score
    best_intent = max(scores, key=scores.get)
    logger.info(f"Intent scores: {scores} → winner: {best_intent} (score={scores[best_intent]})")
    
    # ===== BUILD RESPONSE =====
    return _build_action(best_intent, concepts, profile, folder_id, concurrency, text_lower, raw)


def _build_action(intent: str, concepts: set, profile: str, folder_id: str,
                   concurrency: int, text_lower: str, raw: str) -> dict:
    """Build the action dict for the winning intent."""
    
    if intent == "delete_die_all":
        return {"action": "delete_die_all", "params": {"concurrency": concurrency},
                "reply": f"🗑️ Check TOÀN BỘ ({concurrency} luồng), die xóa live giữ..."}
    
    if intent == "delete_die_profiles":
        return {"action": "delete_die_profiles", "params": {"folder_id": folder_id, "concurrency": concurrency},
                "reply": f"🗑️ Check {folder_id} ({concurrency} luồng), die xóa live giữ..."}
    
    if intent == "batch_check_login":
        return {"action": "batch_check_login", "params": {"folder_id": folder_id, "concurrency": concurrency},
                "reply": f"🔍 Đang check {folder_id} ({concurrency} luồng)..."}
    
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
