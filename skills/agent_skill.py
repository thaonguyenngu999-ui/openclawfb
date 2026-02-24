"""
AgentSkill - Autonomous AI agent for arbitrary browser tasks.

Flow per step:
  1. CDP screenshot (base64) + DOM extract (interactive elements with index)
  2. AI sees screenshot + DOM → returns JSON action
  3. Execute action: click_index / click_selector / click_vision / scroll / navigate / type / wait
  4. Repeat until "done" / "failed" / MAX_STEPS

Click methods (3 tiers):
  - click_index   → fastest: JS click element from DOM list by index
  - click_selector → CSS/aria selector via CDP click()
  - click_vision  → vision pipeline (AI detect + locator + click) — slowest but most flexible
"""

import json
import time
import random
import re
import base64
import traceback
from typing import Dict, Any, Optional, List

import requests as _requests

# DB for skill registry
import db as _db

# AI config — Pollinations FREE (primary, key rotation + model chain)
_POLL_API_URL = "https://gen.pollinations.ai/v1/chat/completions"
_POLL_KEYS = [
    "sk_cH1x3TFuKuaFAK1tHH2y8NLJNejEgxJH",  # Key-3P (3 pollen)
    "sk_3HRi9HUGLup7OKB6ykRds8YtnpcmLHD3",   # Key-1P (1 pollen)
]
# Model chain: best → fast → vision fallback (ALL FREE)
_POLL_MODELS = [
    {"model": "openai-large", "name": "GPT-5.2 Full",  "timeout": 60},  # BEST
    {"model": "openai",       "name": "GPT-5 Mini",    "timeout": 35},  # Good
    {"model": "kimi",         "name": "Kimi K2.5",     "timeout": 60},  # Vision + 256K
    {"model": "deepseek",     "name": "DeepSeek V3.2", "timeout": 45},  # Reasoning
    {"model": "claude-fast",  "name": "Claude Haiku",  "timeout": 35},  # Accurate
    {"model": "gemini-fast",  "name": "Gemini Flash",  "timeout": 30},  # Google
]
# Last resort — Grok (costs credit, use sparingly)
_GROK_API_URL = "https://api.x.ai/v1/chat/completions"
_GROK_API_KEY = "xai-JBnA5g8A9rAZr1KeU3lOpHZNgLtp2PfbfCi23BfOYvjRO0WNoOOWS0wLcwgReJWR91Fk3KVAGgwKya9W"
_GROK_MODEL = "grok-4-1-fast-reasoning"
_GROK_HEADERS = {
    "Authorization": f"Bearer {_GROK_API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "FBManagerPro/1.0",
}

_poll_key_idx = 0  # rotating key index


def _strip_images(msgs):
    """Strip image_url parts from messages for text-only models."""
    out = []
    for msg in msgs:
        if isinstance(msg.get("content"), list):
            parts = [p["text"] for p in msg["content"] if p.get("type") == "text"]
            out.append({"role": msg["role"], "content": " ".join(parts)})
        else:
            out.append(msg)
    return out


# ════════════════════════════════════════════════════════════
#  GROK — SUPERVISOR (planning, reflection, re-planning)
#  Grok là trợ lý thông minh, toàn quyền chỉ đạo chiến lược.
#  Dùng ÍT, chỉ cho quyết định quan trọng (tốn credit).
# ════════════════════════════════════════════════════════════

def _call_grok(messages, max_tokens=400, temperature=0.2, timeout=30):
    """Call Grok API — SUPERVISOR role only (planning, reflection, re-plan).
    Grok supports vision so send original messages."""
    try:
        resp = _requests.post(_GROK_API_URL, json={
            "model": _GROK_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }, headers=_GROK_HEADERS, timeout=timeout)
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"].strip()
            if content:
                print(f"[GROK-SUPERVISOR] OK ({len(content)} chars)")
                return {"ok": True, "content": content, "model": _GROK_MODEL}
        print(f"[GROK-SUPERVISOR] HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[GROK-SUPERVISOR] Error: {e}")

    # Fallback: try best Pollinations model if Grok fails
    print(f"[GROK-SUPERVISOR] Grok failed, falling back to Pollinations...")
    return _call_worker(messages, max_tokens=max_tokens,
                        temperature=temperature, timeout=timeout)


# ════════════════════════════════════════════════════════════
#  POLLINATIONS — WORKERS (step execution, content generation)
#  FREE models, dùng thoải mái cho mọi agent step.
#  Key rotation × model chain → reliable + zero cost.
# ════════════════════════════════════════════════════════════

def _call_worker(messages, max_tokens=500, temperature=0.2, timeout=45):
    """Call Pollinations FREE (key rotation × model chain) — WORKER role.
    Used for agent step decisions, content generation, etc."""
    global _poll_key_idx

    text_msgs = _strip_images(messages)

    for model_info in _POLL_MODELS:
        model_name = model_info["model"]
        model_timeout = min(model_info["timeout"], timeout)

        # Try each key for this model
        for attempt in range(len(_POLL_KEYS)):
            key = _POLL_KEYS[_poll_key_idx % len(_POLL_KEYS)]
            _poll_key_idx = (_poll_key_idx + 1) % len(_POLL_KEYS)

            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
            try:
                resp = _requests.post(_POLL_API_URL, json={
                    "model": model_name,
                    "messages": text_msgs,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }, headers=headers, timeout=model_timeout)
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"].strip()
                    if content:
                        print(f"[WORKER] OK: {model_info['name']} (free)")
                        return {"ok": True, "content": content, "model": model_name}
                print(f"[WORKER] {model_info['name']} HTTP {resp.status_code}")
            except Exception as e:
                print(f"[WORKER] {model_info['name']} error: {e}")

    print(f"[WORKER] All Pollinations models failed! Trying JanAI local...")

    # ── Fallback: JanAI local (auto-detect model) ──
    _JANAI_API = "http://127.0.0.1:1337/v1/chat/completions"
    try:
        # Get available models
        models_resp = _requests.get("http://127.0.0.1:1337/v1/models", timeout=3)
        if models_resp.status_code != 200:
            print(f"[WORKER] JanAI not running (health={models_resp.status_code})")
            return {"ok": False, "content": "", "model": "none"}

        models_data = models_resp.json().get("data", [])
        if not models_data:
            print(f"[WORKER] JanAI has no models loaded")
            return {"ok": False, "content": "", "model": "none"}

        janai_model = models_data[0]["id"]
        print(f"[WORKER] JanAI model: {janai_model}")

        resp = _requests.post(_JANAI_API, json={
            "model": janai_model,
            "messages": text_msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }, timeout=timeout + 30)  # JanAI local can be slower
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"].strip()
            if content:
                print(f"[WORKER] OK: JanAI {janai_model} (local)")
                return {"ok": True, "content": content, "model": f"janai-{janai_model}"}
        print(f"[WORKER] JanAI HTTP {resp.status_code}")
    except Exception as e:
        print(f"[WORKER] JanAI error: {e}")

    print(f"[WORKER] All providers failed (Pollinations + JanAI)!")
    return {"ok": False, "content": "", "model": "none"}


AGENT_SYSTEM = """AI agent điều khiển trình duyệt Facebook. Trả về 1 JSON object duy nhất.

ACTIONS:
navigate {url,reason} | click_index {index,reason} | scroll {direction,amount,reason}
type {index,text,reason} | wait {seconds,reason} | done {summary,reason} | failed {reason}
compose_post {topic,tone,reason} → TỰ ĐỘNG đăng bài (tìm ô+gõ+đăng). Navigate /me trước.
smart_comment {tone,reason} → TỰ ĐỘNG bình luận (đọc bài+viết+gõ+gửi)
upload_reel {video_folder,topic,tone,reason} → TỰ ĐỘNG upload reel
create_page {page_name,category,description,reason} → TỰ ĐỘNG tạo page
list_fb_pages {reason} → TỰ ĐỘNG xem/đếm page đã tạo
click_vision {target,reason} → AI vision click (chậm, dùng cuối cùng)

FB URLs: / (feed) | /me (profile) | /notifications | /friends/requests | /messages | /groups | /marketplace | /watch | /reel/ | /pages/?category=your_pages

QUY TẮC:
1. Sai trang → navigate URL đúng, wait 2s
2. Đăng bài → compose_post (KHÔNG click_index+type thủ công)
3. Comment → smart_comment (KHÔNG type thủ công)
4. Upload reel / Tạo page / Xem page → dùng action chuyên dụng
5. KHÔNG lặp action đã fail → thử cách khác hoặc failed
6. Action lặp 3 lần = BỊ KẸT → đổi chiến lược
7. Ưu tiên: navigate > action chuyên dụng > click_index > scroll > click_vision
8. Memory cho thấy fail → KHÔNG thử lại, dùng cách khác
"""

MAX_STEPS = 30


class AgentSkill:
    """Autonomous AI agent for executing arbitrary browser tasks."""

    # ── Main entry point ──

    def agent_execute(self, task: str, profile_uuid: str = None,
                      telegram_chat_id: str = None,
                      telegram_message_id: int = None,
                      cancel_event=None) -> Dict[str, Any]:
        """Execute an arbitrary task using AI agent loop."""
        browser_opened_here = False
        steps_log: List[Dict] = []

        try:
            profile_uuid = profile_uuid or self.current_profile
            if not profile_uuid:
                return {"success": False, "error": "No profile specified"}

            short_id = profile_uuid[:20]
            print(f"[AGENT] === START === {short_id} -> {task}")

            # 0a. Extract Windows paths from task text (AI often mangles these)
            import os as _os
            # Match Windows paths including Unicode folder names
            _extracted_paths = re.findall(r'[A-Za-z]:\\[^\s,;:!?\n]+', task)
            _valid_folder_path = ""
            for _p in _extracted_paths:
                _p_clean = _p.strip().rstrip('.,;:!?')
                if _os.path.isdir(_p_clean):
                    _valid_folder_path = _p_clean
                    print(f"[AGENT] Extracted valid folder from task: {_valid_folder_path}")
                    break
                # Try without last word (AI sometimes appends extra words)
                _parent = _os.path.dirname(_p_clean)
                if _os.path.isdir(_parent) and _parent != _p_clean:
                    # Check if basename is a real subfolder
                    if not _os.path.isdir(_p_clean):
                        _valid_folder_path = _parent
                        print(f"[AGENT] Extracted parent folder from task: {_valid_folder_path}")
                        break

            # 0. Ensure browser is open + CDP connected
            cdp = self._get_cdp(profile_uuid)
            if not cdp:
                print(f"[AGENT] No CDP, opening browser...")
                open_result = self.open_browser(profile_uuid)
                if not open_result.get('success'):
                    err = open_result.get('error', 'unknown')
                    print(f"[AGENT] Open browser FAILED: {err}")
                    return {"success": False, "error": f"Cannot open browser: {err}"}
                browser_opened_here = True
                time.sleep(3)
                cdp = self._get_cdp(profile_uuid)
                if not cdp:
                    print(f"[AGENT] Still no CDP after open_browser!")
                    return {"success": False, "error": "CDP connection failed after opening browser"}

            print(f"[AGENT] CDP OK, port={cdp.remote_port}")

            # ── DIRECT SKILL SHORTCUT: skip AI loop for structured skills ──
            # AI (JanAI/Pollinations) is too dumb to correctly use create_page/upload_reel.
            # For known skill patterns, bypass the AI loop entirely and call the skill directly.
            task_pattern = self._classify_task(task)

            if task_pattern == "create_page":
                print(f"[AGENT] ⚡ SHORTCUT: create_page detected, skipping AI loop")
                # Extract page name and category from task
                t_lower = task.lower()
                # Parse: "tạo page V Giải Trí danh mục Giải trí"
                # or "s3 tạo 1 page V giải Trí nhé. hạng mục giải trí"
                page_name = ""
                category = "Giải trí"
                description = ""

                # Extract page name: text between "page" and common delimiters
                name_match = re.search(
                    r'(?:page|trang|fanpage)\s+(.+?)(?:\s*(?:nhé|nha|đi|hạng mục|danh mục|category|mô tả|$))',
                    task, re.IGNORECASE)
                if name_match:
                    page_name = name_match.group(1).strip().rstrip('.,!?')
                    # Clean up: remove "1 " or numbers at start
                    page_name = re.sub(r'^\d+\s+', '', page_name).strip()

                # Extract category
                cat_match = re.search(
                    r'(?:hạng mục|danh mục|category)\s+(.+?)(?:\s*(?:nhé|nha|đi|mô tả|content|$))',
                    task, re.IGNORECASE)
                if cat_match:
                    category = cat_match.group(1).strip().rstrip('.,!?')

                if not page_name:
                    page_name = f"Page {profile_uuid[:8]}"

                print(f"[AGENT] create_page shortcut: name='{page_name}', category='{category}'")

                # Send progress
                if telegram_chat_id and telegram_message_id:
                    try:
                        self._telegram_edit_message(telegram_chat_id, telegram_message_id,
                            f"🤖 *Agent - {short_id}*\n📋 Tạo page '{page_name}'\n\n"
                            f"⏳ Đang tạo page trên Facebook...")
                    except Exception:
                        pass

                result = self._create_page(profile_uuid, page_name, category, description)

                # Send final screenshot
                desc = result.get("desc", "")
                ok = result.get("success", False)
                icon = "✅" if ok else "❌"
                self._send_agent_screenshot(profile_uuid, telegram_chat_id,
                    f"{icon} Create Page {'thành công' if ok else 'thất bại'}!\n"
                    f"📋 {page_name}\n📝 {desc}")

                return {"success": ok, "steps": [{"step": 1, "ok": ok, "desc": desc}],
                        "total_steps": 1, "profile_uuid": profile_uuid}

            if task_pattern == "upload_reel":
                print(f"[AGENT] ⚡ SHORTCUT: upload_reel detected, skipping AI loop")
                # Extract video folder and topic from task
                video_folder = ""
                topic = ""
                tone = ""

                # Try to find folder path in task
                folder_match = re.search(
                    r'(?:folder|thư mục|path|đường dẫn)\s*[=:]*\s*["\']?([^"\']+?)["\']?\s*(?:$|,|topic|chủ đề|nhé|nha)',
                    task, re.IGNORECASE)
                if folder_match:
                    video_folder = folder_match.group(1).strip()

                if not video_folder:
                    # Default video folder
                    video_folder = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                                 "..", "data", "reels")
                    if not _os.path.isdir(video_folder):
                        video_folder = _os.path.join(_os.path.expanduser("~"),
                                                     "Desktop", "reels")

                # Extract topic/caption hint
                topic_match = re.search(
                    r'(?:topic|chủ đề|caption|nội dung|về)\s*[=:]*\s*["\']?(.+?)(?:["\']?\s*(?:$|nhé|nha|đi))',
                    task, re.IGNORECASE)
                if topic_match:
                    topic = topic_match.group(1).strip().rstrip('.,!?')

                print(f"[AGENT] upload_reel shortcut: folder='{video_folder}', topic='{topic}'")

                # Send progress
                if telegram_chat_id and telegram_message_id:
                    try:
                        self._telegram_edit_message(telegram_chat_id, telegram_message_id,
                            f"🤖 *Agent - {short_id}*\n📋 Upload Reel\n\n"
                            f"⏳ Đang upload reel...")
                    except Exception:
                        pass

                result = self._upload_reel(profile_uuid, video_folder, topic, tone)

                desc = result.get("desc", "")
                ok = result.get("success", False)
                icon = "✅" if ok else "❌"
                self._send_agent_screenshot(profile_uuid, telegram_chat_id,
                    f"{icon} Upload Reel {'thành công' if ok else 'thất bại'}!\n📝 {desc}")

                return {"success": ok, "steps": [{"step": 1, "ok": ok, "desc": desc}],
                        "total_steps": 1, "profile_uuid": profile_uuid}

            if task_pattern == "list_fb_pages":
                print(f"[AGENT] ⚡ SHORTCUT: list_fb_pages detected, skipping AI loop")

                # Send progress
                if telegram_chat_id and telegram_message_id:
                    try:
                        self._telegram_edit_message(telegram_chat_id, telegram_message_id,
                            f"🤖 *Agent - {short_id}*\n📋 Xem danh sách Page\n\n"
                            f"⏳ Đang mở trang Pages...")
                    except Exception:
                        pass

                result = self._list_fb_pages(profile_uuid)

                desc = result.get("desc", "")
                ok = result.get("success", False)
                pages = result.get("pages", [])
                icon = "✅" if ok else "❌"
                page_text = ""
                if pages:
                    page_text = "\n".join(f"  • {p.get('name', '?')}" for p in pages[:20])
                    msg = f"{icon} Tìm thấy {len(pages)} page:\n{page_text}"
                else:
                    msg = f"{icon} {desc}"
                self._send_agent_screenshot(profile_uuid, telegram_chat_id, msg)

                return {"success": ok, "steps": [{"step": 1, "ok": ok, "desc": desc}],
                        "total_steps": 1, "profile_uuid": profile_uuid,
                        "pages": pages}

            def send_progress(step_num, action_desc):
                if not telegram_chat_id or not telegram_message_id:
                    return
                text = f"🤖 *Agent - {short_id}*\n📋 {task[:60]}\n\n"
                text += f"⏳ Step {step_num}/{MAX_STEPS}: {action_desc}\n\n"
                for s in steps_log[-5:]:
                    icon = "✅" if s.get("ok") else "❌"
                    text += f"{icon} {s.get('desc', '?')[:55]}\n"
                try:
                    self._telegram_edit_message(telegram_chat_id, telegram_message_id, text)
                except Exception:
                    pass

            # ── Pre-planning: ask AI for a plan ──
            plan = ""
            try:
                plan = self._agent_make_plan(task, profile_uuid)
                print(f"[AGENT] Plan: {plan[:200]}")
            except Exception as e:
                print(f"[AGENT] Plan failed: {e}")

            # ── Search for matching learned skills ──
            matched_skills = self._search_matching_skills(task)
            matched_skill_id = matched_skills[0]["id"] if matched_skills else None
            skill_hint = self._build_skill_hint(matched_skills)
            if skill_hint:
                plan = plan + skill_hint
                print(f"[AGENT] Injected {len(matched_skills)} skill hints into plan")
            elif not matched_skills:
                # No learned skill → inject builtin template
                builtin = self._get_builtin_template(task)
                if builtin:
                    plan = plan + builtin
                    print(f"[AGENT] No learned skill → injected builtin template")

            # ── Timing ──
            _start_time = time.time()

            # ── Loop detection state ──
            last_actions = []   # track recent action strings
            last_dom_hash = ""  # track DOM changes
            stuck_warning = ""  # injected into AI prompt when stuck

            # ── Step Memory (structured agent memory) ──
            step_memory = {
                "observations": [],      # Key observations each step
                "failed_actions": [],    # Actions that failed (never retry)
                "current_state": "",     # Current page/element state
                "reflection": "",        # Latest self-reflection insight
                "pages_visited": [],     # URLs navigated to
                "scroll_count": 0,       # Total scrolls done
            }

            # ── Load persistent memory from DB ──
            try:
                past_memories = _db.get_agent_memories(profile_uuid, limit=10)
                if past_memories:
                    persistent_lessons = []
                    for mem in past_memories:
                        persistent_lessons.append(
                            f"[{mem.get('memory_type', '?')}] {mem.get('content', '')[:80]}")
                    step_memory["persistent_lessons"] = persistent_lessons
                    print(f"[AGENT] Loaded {len(past_memories)} persistent memories")
            except Exception as e:
                print(f"[AGENT] Load memory error: {e}")

            # ── Agent loop ──
            for step_num in range(1, MAX_STEPS + 1):
                # ── Cancel check ──
                if cancel_event and cancel_event.is_set():
                    print(f"[AGENT] 🛑 CANCELLED at step {step_num}")
                    steps_log.append({"step": step_num, "ok": False,
                                      "desc": "Cancelled by user"})
                    break

                print(f"\n[AGENT] -- Step {step_num}/{MAX_STEPS} --")
                send_progress(step_num, "Đang phân tích...")

                # 1. Screenshot via CDP (fast, reliable)
                img_b64 = self._agent_screenshot_b64(profile_uuid)
                if not img_b64:
                    print(f"[AGENT] Screenshot FAILED")
                    steps_log.append({"step": step_num, "ok": False,
                                      "desc": "Screenshot failed"})
                    # Try to reconnect CDP
                    self._reconnect_cdp(profile_uuid)
                    time.sleep(2)
                    continue

                # 2. DOM summary with indexed elements
                dom_summary, dom_elements = self._get_indexed_dom(profile_uuid)
                dom_hash = str(len(dom_elements)) + "_" + str(len(dom_summary))
                print(f"[AGENT] DOM: {len(dom_elements)} elements, {len(dom_summary)} chars (hash={dom_hash})")

                # 3. Loop detection: check if stuck
                if len(last_actions) >= 3:
                    last3 = last_actions[-3:]
                    if last3[0] == last3[1] == last3[2]:
                        if dom_hash == last_dom_hash:
                            print(f"[AGENT] ⚠️ LOOP DETECTED: '{last3[0]}' repeated 3x, DOM unchanged!")
                            stuck_warning = (
                                f"\n\n⚠️ CẢNH BÁO: Bạn đang BỊ KẸT! Action '{last3[0]}' đã lặp 3 lần "
                                f"và trang KHÔNG thay đổi. PHẢI thử cách KHÁC ngay:\n"
                                f"- Nếu sai trang → navigate đến URL đúng\n"
                                f"- Nếu element không click được → scroll down rồi thử element khác\n"
                                f"- Nếu không có cách → failed\n"
                                f"TUYỆT ĐỐI KHÔNG lặp lại action '{last3[0]}'!"
                            )
                        else:
                            stuck_warning = ""
                    else:
                        stuck_warning = ""
                last_dom_hash = dom_hash

                # 4. Ask AI (Grok) for next action
                ai_action = self._ask_agent_ai(task, img_b64, dom_summary,
                                                steps_log, plan=plan,
                                                extra_warning=stuck_warning,
                                                memory=step_memory)

                # ── Cancel check after AI call (slow operation) ──
                if cancel_event and cancel_event.is_set():
                    print(f"[AGENT] 🛑 CANCELLED after AI call at step {step_num}")
                    steps_log.append({"step": step_num, "ok": False,
                                      "desc": "Cancelled by user"})
                    break

                if not ai_action:
                    print(f"[AGENT] AI returned NOTHING")
                    steps_log.append({"step": step_num, "ok": False,
                                      "desc": "AI returned nothing"})
                    time.sleep(2)
                    continue

                action_type = ai_action.get("action", "?")
                reason = ai_action.get("reason", "")
                # Build action fingerprint for loop detection
                action_fingerprint = action_type
                if action_type == "click_index":
                    action_fingerprint += f"_{ai_action.get('index', '?')}"
                elif action_type == "click_selector":
                    action_fingerprint += f"_{ai_action.get('selector', '?')}"
                elif action_type == "navigate":
                    action_fingerprint += f"_{ai_action.get('url', '?')}"
                last_actions.append(action_fingerprint)

                print(f"[AGENT] AI -> {action_type}: {reason}")
                send_progress(step_num, f"{action_type}: {reason[:45]}")

                # 5. Execute (pass extracted context for path resolution etc.)
                _task_ctx = {"extracted_folder": _valid_folder_path, "task": task}
                result = self._execute_agent_action(
                    ai_action, profile_uuid, dom_elements, step_num,
                    task_context=_task_ctx)
                result["action_type"] = action_type
                steps_log.append(result)

                # 5a. Auto-done after upload_reel/create_page/list_fb_pages succeeds (don't let AI loop)
                if action_type in ("upload_reel", "create_page", "list_fb_pages") and result.get("ok"):
                    desc = result.get("desc", f"{action_type} completed")
                    self._send_agent_screenshot(profile_uuid, telegram_chat_id,
                        f"✅ Agent hoàn thành!\n📋 {task[:60]}\n📝 {desc}")
                    print(f"[AGENT] === DONE (auto-done after {action_type}) === {desc}")
                    # Add a terminal "done" step
                    steps_log.append({"step": step_num, "ok": True,
                                      "desc": f"DONE: {desc}", "action_type": "done"})
                    break

                # 5b. Update step memory + consecutive fail tracking
                desc = result.get("desc", "")
                if result.get("ok"):
                    step_memory["observations"].append(f"Step {step_num}: {desc[:60]}")
                    if action_type == "navigate":
                        url = ai_action.get("url", "")
                        step_memory["pages_visited"].append(url)
                        step_memory["current_state"] = f"Navigated to {url}"
                    elif action_type == "scroll":
                        step_memory["scroll_count"] += 1
                    elif action_type in ("click_index", "click_vision"):
                        step_memory["current_state"] = f"Clicked: {desc[:40]}"
                    elif action_type == "type":
                        step_memory["current_state"] = f"Typed text: {desc[:40]}"
                    step_memory["reflection"] = ""  # Clear reflection on success
                else:
                    step_memory["failed_actions"].append(
                        f"{action_fingerprint}: {desc[:50]}")
                    step_memory["observations"].append(
                        f"FAIL Step {step_num}: {desc[:50]}")
                    # Self-reflect on failure (not for terminal states)
                    if action_type not in ("done", "failed"):
                        reflection = self._self_reflect(
                            task, result, dom_summary, steps_log, step_memory)
                        if reflection:
                            step_memory["reflection"] = reflection

                # 5c. Dynamic re-plan every 8 steps or after 2 consecutive fails
                recent_fails = sum(
                    1 for s in steps_log[-2:] if not s.get("ok"))
                if ((step_num % 8 == 0 or recent_fails >= 2)
                        and action_type not in ("done", "failed")):
                    print(f"[AGENT] 🔄 Re-planning (step={step_num})...")
                    plan = self._dynamic_replan(
                        task, steps_log, step_memory, plan)

                # 6. Check terminal states
                if action_type == "done":
                    self._send_agent_screenshot(profile_uuid, telegram_chat_id,
                        f"✅ Agent hoàn thành!\n📋 {task[:60]}\n📝 {result.get('desc', '')}")
                    print(f"[AGENT] === DONE === {result.get('desc', '')}")
                    break

                if action_type == "failed":
                    self._send_agent_screenshot(profile_uuid, telegram_chat_id,
                        f"❌ Agent thất bại\n📋 {task[:60]}\n💬 {reason}")
                    print(f"[AGENT] === FAILED === {reason}")
                    break

                # Small delay between steps
                time.sleep(1)
            else:
                # Max steps reached
                self._send_agent_screenshot(profile_uuid, telegram_chat_id,
                    f"⚠️ Agent đạt giới hạn {MAX_STEPS} bước\n📋 {task[:60]}")
                print(f"[AGENT] === MAX STEPS ===")

            # Final summary
            ok_count = sum(1 for s in steps_log if s.get("ok"))
            last = steps_log[-1] if steps_log else {}
            task_success = any(s.get("desc", "").startswith("DONE") for s in steps_log)
            duration = time.time() - _start_time

            # ── Auto-save skill & log task ──
            try:
                if task_success:
                    self._auto_save_skill(task, steps_log, duration, matched_skill_id)
                elif matched_skill_id:
                    _db.increment_skill_fail(matched_skill_id)

                _db.log_agent_task(
                    profile_uuid=profile_uuid,
                    task_text=task,
                    matched_skill_id=matched_skill_id,
                    steps_json=json.dumps(
                        [{"s": s.get("step"), "ok": s.get("ok"),
                          "d": s.get("desc", "")[:60]} for s in steps_log],
                        ensure_ascii=False),
                    success=task_success,
                    total_steps=len(steps_log),
                    duration=duration,
                    error_message=last.get("desc", "") if not task_success else None,
                )
                print(f"[AGENT] Task logged: success={task_success}, "
                      f"steps={len(steps_log)}, duration={duration:.1f}s")
            except Exception as e:
                print(f"[AGENT] Skill/log save error: {e}")

            # ── Save persistent memories (lessons learned) ──
            try:
                # Save failed actions as error patterns (avoid in future)
                if step_memory.get("failed_actions"):
                    failed_summary = "; ".join(step_memory["failed_actions"][-5:])
                    _db.save_agent_memory(
                        profile_uuid, "error_pattern",
                        f"Task '{task[:40]}': FAILED actions: {failed_summary[:200]}",
                        task_context=task[:100])

                # Save successful strategy as lesson
                if task_success and step_memory.get("observations"):
                    success_summary = "; ".join(step_memory["observations"][-5:])
                    _db.save_agent_memory(
                        profile_uuid, "lesson",
                        f"Task '{task[:40]}': SUCCESS strategy: {success_summary[:200]}",
                        task_context=task[:100])

                # Save reflection as strategy insight
                if step_memory.get("reflection"):
                    _db.save_agent_memory(
                        profile_uuid, "strategy",
                        step_memory["reflection"][:200],
                        task_context=task[:100])

                # Cleanup old memories (keep last 50)
                _db.clear_old_agent_memories(profile_uuid, keep_last=50)
                print(f"[AGENT] Persistent memory saved")
            except Exception as e:
                print(f"[AGENT] Memory save error: {e}")

            if telegram_chat_id and telegram_message_id:
                text = f"🤖 *Agent - {short_id}*\n📋 Task: {task[:60]}\n\n"
                if last.get("desc", "").startswith("DONE"):
                    text += f"✅ *Hoàn thành!*\n📝 {last.get('desc', '')[6:]}\n\n"
                elif last.get("desc", "").startswith("FAILED"):
                    text += f"❌ *Thất bại:* {last.get('desc', '')[8:]}\n\n"
                else:
                    text += f"⚠️ {len(steps_log)} bước đã thực hiện\n\n"
                skill_note = ""
                if matched_skill_id:
                    skill_note = f"🧠 Đã dùng skill đã học\n"
                text += f"{skill_note}📊 {ok_count}✅ / {len(steps_log)} steps • {duration:.0f}s"
                try:
                    self._telegram_edit_message(telegram_chat_id, telegram_message_id, text)
                except Exception:
                    pass

            return {
                "success": task_success,
                "profile_uuid": profile_uuid, "task": task,
                "steps": steps_log, "total_steps": len(steps_log),
                "duration": round(duration, 1),
                "used_skill": matched_skill_id,
            }

        except Exception as e:
            traceback.print_exc()
            print(f"[AGENT] === EXCEPTION === {e}")
            self._send_agent_screenshot(profile_uuid, telegram_chat_id,
                                         f"❌ Agent lỗi: {str(e)[:100]}")
            return {"success": False, "error": str(e), "steps": steps_log}

    # ── Skill Registry: search & save learned procedures ──

    def _extract_keywords(self, task: str) -> str:
        """Extract meaningful keywords from task text for DB matching."""
        stopwords = {
            'vào', 'trên', 'của', 'và', 'hoặc', 'với', 'cho', 'để',
            'từ', 'đến', 'các', 'một', 'những', 'là', 'có', 'không',
            'được', 'đã', 'sẽ', 'đang', 'cái', 'này', 'kia', 'nào',
            'thì', 'mà', 'nên', 'cần', 'tôi', 'mình', 'bạn', 'hãy',
            'the', 'a', 'an', 'in', 'on', 'at', 'to', 'for', 'of',
            'and', 'or', 'is', 'my', 'me', 'go', 'do', 'it', 'về',
        }
        words = re.findall(r'[\w]+', task.lower())
        keywords = [w for w in words if w not in stopwords and len(w) > 1]
        return ",".join(keywords[:15])

    def _classify_task(self, task: str) -> str:
        """Classify task into a pattern category for skill matching."""
        t = task.lower()
        if any(w in t for w in ['viết bài', 'đăng bài', 'compose', 'post']):
            return 'compose_post'
        if any(w in t for w in ['like', 'thích']):
            return 'like'
        if any(w in t for w in ['comment', 'bình luận']):
            return 'comment'
        if any(w in t for w in ['thông báo', 'notification']):
            return 'check_notifications'
        if any(w in t for w in ['tin nhắn', 'message', 'messenger']):
            return 'check_messages'
        if any(w in t for w in ['bạn bè', 'friend', 'lời mời']):
            return 'check_friends'
        if any(w in t for w in ['nhóm', 'group']):
            return 'interact_group'
        if any(w in t for w in ['reel', 'reels', 'video ngắn']):
            # Distinguish upload vs watch
            if any(w in t for w in ['đăng', 'upload', 'tải lên', 'post', 'tạo',
                                     'đăng reel', 'up reel', 'up video']):
                return 'upload_reel'
            return 'watch_reels'
        if any(w in t for w in ['xem', 'duyệt', 'feed', 'lướt']):
            return 'browse_feed'
        if any(w in t for w in ['tạo page', 'tạo trang', 'create page', 'fanpage', 'tạo fanpage']):
            return 'create_page'
        if any(w in t for w in ['bao nhiêu page', 'mấy page', 'list page', 'xem page',
                                 'danh sách page', 'các page', 'page đã tạo',
                                 'trang đã tạo', 'có page nào', 'liệt kê page',
                                 'bao nhiêu trang', 'mấy trang']):
            return 'list_fb_pages'
        if any(w in t for w in ['marketplace', 'mua', 'bán']):
            return 'marketplace'
        if any(w in t for w in ['trang cá nhân', 'profile', '/me']):
            return 'visit_profile'
        return 'general'

    def _search_matching_skills(self, task: str) -> List[Dict]:
        """Search DB for previously learned skills matching this task."""
        try:
            skills = _db.search_agent_skills(task, limit=3)
            if skills:
                names = [s.get('name', '?') for s in skills]
                print(f"[AGENT-SKILL] Found {len(skills)} matching skills: {names}")
            return skills
        except Exception as e:
            print(f"[AGENT-SKILL] Search error: {e}")
            return []

    def _build_skill_hint(self, matched_skills: List[Dict]) -> str:
        """Build a hint string from matched skills to inject into AI prompt."""
        if not matched_skills:
            return ""
        hints = ["\n\n📚 KINH NGHIỆM TỪ TASK TƯƠNG TỰ ĐÃ THÀNH CÔNG:"]
        for i, skill in enumerate(matched_skills[:2]):
            name = skill.get("name", "?")
            sc = skill.get("success_count", 0)
            steps_str = skill.get("steps_json", "[]")
            try:
                steps = json.loads(steps_str)
            except Exception:
                steps = []
            hints.append(f"\n--- Skill #{i+1}: '{name}' (đã thành công {sc} lần) ---")
            for j, step in enumerate(steps[:8]):
                desc = step.get("desc", "?")[:60]
                hints.append(f"  Bước {j+1}: {desc}")
            hints.append(f"  → Tổng {skill.get('total_steps', '?')} bước")
        hints.append(
            "\nBạn có thể LÀM THEO FLOW TƯƠNG TỰ nhưng KHÔNG bắt buộc. "
            "Hãy adapt linh hoạt theo trang hiện tại."
        )
        return "\n".join(hints)

    def _auto_save_skill(self, task: str, steps_log: List[Dict],
                         duration: float, matched_skill_id: int = None):
        """Auto-save successful procedure as a new skill or update existing."""
        try:
            # Only save if we have meaningful steps (not trivially short)
            ok_steps = [s for s in steps_log if s.get("ok")]
            if len(ok_steps) < 1:
                return

            task_pattern = self._classify_task(task)
            keywords = self._extract_keywords(task)
            name = f"{task_pattern}: {task[:50]}"

            # Simplify steps for storage (only keep desc and ok)
            simplified = []
            for s in steps_log:
                simplified.append({
                    "step": s.get("step", 0),
                    "ok": s.get("ok", False),
                    "desc": s.get("desc", "")[:80],
                })

            result = _db.save_agent_skill(
                name=name,
                task_pattern=task_pattern,
                keywords=keywords,
                steps_json=json.dumps(simplified, ensure_ascii=False),
                total_steps=len(steps_log),
                duration=duration,
            )

            if result.get("updated"):
                print(f"[AGENT-SKILL] Updated skill #{result['id']}: {name}")
            else:
                print(f"[AGENT-SKILL] Saved NEW skill #{result['id']}: {name}")

        except Exception as e:
            print(f"[AGENT-SKILL] Save error: {e}")

    # ── Screenshot via CDP (fast) ──

    def _agent_screenshot_b64(self, profile_uuid: str) -> Optional[str]:
        """Capture screenshot as base64 using CDP. Uses JPEG for smaller size."""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            print(f"[AGENT] _agent_screenshot_b64: no CDP")
            return None
        try:
            # Use JPEG format with quality 55 for smaller payload (saves tokens)
            result = cdp._send_command("Page.captureScreenshot", {
                "format": "jpeg", "quality": 55
            })
            img_data = result.get("result", {}).get("data")
            if img_data:
                size_kb = len(img_data) // 1000
                print(f"[AGENT] Screenshot OK ({size_kb}KB b64 jpeg)")
                return img_data
            print(f"[AGENT] Screenshot: no data in result: {str(result)[:200]}")
            return None
        except Exception as e:
            print(f"[AGENT] Screenshot error: {e}")
            return None

    def _reconnect_cdp(self, profile_uuid: str):
        """Try to reconnect CDP if connection was lost."""
        try:
            # Remove stale connection
            if profile_uuid in self.connections:
                try:
                    self.connections[profile_uuid].disconnect()
                except Exception:
                    pass
                del self.connections[profile_uuid]

            # Re-open via Hidemium
            from api_service import api as hidemium
            result = hidemium.open_browser(profile_uuid)
            data = result.get('data', {})
            remote_port = data.get('remote_port')
            ws_url = data.get('web_socket', '')
            if not remote_port and ws_url:
                m = re.search(r':(\d+)/', ws_url)
                if m:
                    remote_port = int(m.group(1))
            if remote_port:
                from automation.cdp_client import CDPClient
                cdp_new = CDPClient(remote_port)
                res = cdp_new.connect()
                if res.success:
                    self.connections[profile_uuid] = cdp_new
                    print(f"[AGENT] Reconnected CDP at port {remote_port}")
                else:
                    print(f"[AGENT] Reconnect failed: {res.error}")
        except Exception as e:
            print(f"[AGENT] Reconnect error: {e}")

    # ── DOM extraction with indexed elements ──

    def _get_indexed_dom(self, profile_uuid: str) -> tuple:
        """Extract DOM elements with indices for direct clicking.
        Returns (summary_text, elements_list).
        """
        dom_js = """
        (function() {
            var url = window.location.href;
            var title = document.title;
            var elements = [];
            var sels = 'a[href], button, [role="button"], [role="link"], [role="tab"], ' +
                       '[role="menuitem"], input, textarea, [role="textbox"], ' +
                       '[aria-label], [data-testid], [role="switch"], [role="checkbox"], ' +
                       '[role="dialog"], [role="menu"], [contenteditable="true"], ' +
                       '[role="option"], [role="listbox"], [role="combobox"], ' +
                       'select, [role="img"][aria-label], [role="article"], ' +
                       '[data-pagelet], form';
            var els = document.querySelectorAll(sels);
            var idx = 0;
            var seen = new Set();

            for (var i = 0; i < els.length && idx < 60; i++) {
                var el = els[i];
                var rect = el.getBoundingClientRect();
                if (rect.width < 5 || rect.height < 5) continue;
                if (rect.bottom < -100 || rect.top > window.innerHeight * 2.5) continue;

                // Deduplicate by position (same cx,cy within 3px = same element)
                var cx = Math.round(rect.left + rect.width / 2);
                var cy = Math.round(rect.top + rect.height / 2);
                var posKey = Math.round(cx/3) + '_' + Math.round(cy/3);
                if (seen.has(posKey)) continue;
                seen.add(posKey);

                var tag = el.tagName.toLowerCase();
                var text = (el.innerText || el.textContent || '').trim().substring(0, 60);
                var ariaLabel = el.getAttribute('aria-label') || '';
                var role = el.getAttribute('role') || '';
                var href = el.getAttribute('href') || '';
                var placeholder = el.getAttribute('placeholder') || '';
                var testid = el.getAttribute('data-testid') || '';
                var visible = rect.top >= 0 && rect.top < window.innerHeight;
                var editable = el.getAttribute('contenteditable') === 'true';
                var disabled = el.disabled || el.getAttribute('aria-disabled') === 'true';
                var checked = el.checked || el.getAttribute('aria-checked') === 'true';
                var expanded = el.getAttribute('aria-expanded');
                var inputType = el.getAttribute('type') || '';
                var inputValue = (tag === 'input' || tag === 'textarea') ? (el.value || '').substring(0, 30) : '';

                // Build a unique selector
                var selector = '';
                if (testid) {
                    selector = '[data-testid="' + testid + '"]';
                } else if (el.id) {
                    selector = '#' + el.id;
                } else if (ariaLabel) {
                    selector = '[aria-label="' + ariaLabel.replace(/"/g, '\\\\"') + '"]';
                } else if (role && text) {
                    selector = '[role="' + role + '"]';
                }

                // Build compact description (save tokens)\n                var desc = tag;\n                if (role) desc += '[' + role + ']';\n                if (ariaLabel) desc += ' aria=\"' + ariaLabel.substring(0, 30) + '\"';\n                if (testid) desc += ' tid=\"' + testid.substring(0, 25) + '\"';\n                if (placeholder) desc += ' ph=\"' + placeholder.substring(0, 25) + '\"';\n                if (editable) desc += ' [edit]';\n                if (disabled) desc += ' [DIS]';\n                if (inputType && inputType !== 'hidden') desc += ' t=' + inputType;\n                if (inputValue) desc += ' v=\"' + inputValue + '\"';\n                if (text && text.length > 1 && text.length < 40) desc += ' \"' + text.substring(0, 35) + '\"';\n                if (href && !href.startsWith('javascript') && !href.startsWith('#')) {\n                    desc += ' ->' + href.substring(0, 50);\n                }

                elements.push({
                    idx: idx,
                    desc: desc,
                    selector: selector,
                    visible: visible,
                    cx: cx,
                    cy: cy,
                    tag: tag
                });
                idx++;
            }

            // Detect page state (Facebook-specific)
            var pageState = [];
            if (document.querySelector('[role="dialog"]')) pageState.push('DIALOG_OPEN');
            if (document.querySelector('[role="menu"]')) pageState.push('MENU_OPEN');
            if (document.querySelector('[data-testid="Keycommand_wrapper_composer"]')) pageState.push('COMPOSER_OPEN');
            if (document.querySelector('[role="textbox"][contenteditable="true"]')) pageState.push('TEXTBOX_ACTIVE');
            var loadingSpinners = document.querySelectorAll('[role="progressbar"], .uiLoadingIndicator, svg[aria-label*="Loading"]');
            if (loadingSpinners.length > 0) pageState.push('LOADING');

            var bodyText = document.body ? document.body.innerText.substring(0, 300) : '';

            return JSON.stringify({
                url: url,
                title: title,
                elements: elements,
                page_text: bodyText.replace(/\\n+/g, ' ').substring(0, 200),
                viewport: {w: window.innerWidth, h: window.innerHeight},
                page_state: pageState
            });
        })()
        """
        try:
            result = self.execute_js(dom_js, profile_uuid)
            raw = result.get("result", "{}")
            if isinstance(raw, bool) or raw is None:
                return "DOM extraction: invalid result", []
            data = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(data, dict):
                return f"DOM extraction: unexpected type {type(data)}", []

            lines = [f"URL: {data.get('url', '?')}",
                     f"Title: {data.get('title', '?')}",
                     f"Viewport: {data.get('viewport', {})}"]

            # Page state indicators (Facebook-specific)
            page_state = data.get("page_state", [])
            if page_state:
                lines.append(f"⚡ Page State: {', '.join(page_state)}")

            elements = data.get("elements", [])
            visible = [e for e in elements if e.get("visible")]
            offscreen = [e for e in elements if not e.get("visible")]

            lines.append(f"\nVisible({len(visible)}):")
            for e in visible:
                lines.append(f"  [{e['idx']}] {e['desc']}")

            if offscreen:
                lines.append(f"Below({len(offscreen)}):")
                for e in offscreen[:8]:
                    lines.append(f"  [{e['idx']}] {e['desc']}")

            lines.append(f"\nText: {data.get('page_text', '')[:200]}")

            elem_list = []
            for e in elements:
                elem_list.append({
                    "index": e["idx"],
                    "selector": e.get("selector", ""),
                    "cx": e.get("cx", 0),
                    "cy": e.get("cy", 0),
                    "desc": e.get("desc", ""),
                    "tag": e.get("tag", ""),
                })

            return "\n".join(lines), elem_list

        except Exception as e:
            print(f"[AGENT] DOM extraction error: {e}")
            traceback.print_exc()
            return f"DOM extraction error: {e}", []

    # ── Pre-planning: ask AI for a high-level plan ──

    # ── Built-in Skill Templates (zero-shot knowledge) ──
    _BUILTIN_TEMPLATES = {
        "compose_post": {
            "name": "Đăng bài lên trang cá nhân",
            "steps": [
                "1. navigate → https://www.facebook.com/me",
                "2. wait 3s (chờ load profile)",
                "3. compose_post với topic + tone phù hợp",
                "4. done (compose_post tự xử lý hết)",
            ],
            "tips": "KHÔNG click_index vào ô 'Bạn đang nghĩ gì?' — compose_post tự làm hết.",
        },
        "like": {
            "name": "Like bài viết trên feed",
            "steps": [
                "1. navigate → https://www.facebook.com/",
                "2. wait 2s",
                "3. Tìm nút Like (aria-label chứa 'Thích'/'Like') → click_index",
                "4. done",
            ],
            "tips": "Nút Like thường có aria-label='Thích' hoặc 'Like'. Nếu không thấy → scroll down.",
        },
        "comment": {
            "name": "Bình luận bài viết",
            "steps": [
                "1. navigate → https://www.facebook.com/ (hoặc trang chứa bài cần comment)",
                "2. wait 2s",
                "3. scroll down nếu cần tìm bài viết",
                "4. smart_comment với tone phù hợp (tự đọc bài + viết + gõ + gửi)",
                "5. done",
            ],
            "tips": "LUÔN dùng smart_comment thay vì type thủ công. smart_comment tự đọc nội dung bài viết và viết comment PHÙ HỢP. KHÔNG dùng click_index + type để comment.",
        },
        "check_notifications": {
            "name": "Xem thông báo",
            "steps": [
                "1. navigate → https://www.facebook.com/notifications",
                "2. wait 2s",
                "3. Click vào thông báo đầu tiên/quan tâm → click_index",
                "4. wait 2s",
                "5. done",
            ],
            "tips": "Thông báo chưa đọc thường có nền xanh nhạt. data-testid thường là 'notification_item'.",
        },
        "check_friends": {
            "name": "Xem/Chấp nhận lời mời kết bạn",
            "steps": [
                "1. navigate → https://www.facebook.com/friends/requests",
                "2. wait 2s",
                "3. Tìm nút 'Xác nhận'/'Confirm' → click_index (chấp nhận)",
                "4. wait 1s",
                "5. done",
            ],
            "tips": "Nút xác nhận: aria-label chứa 'Confirm'/'Xác nhận'. Nút xóa: 'Delete'/'Xóa'.",
        },
        "check_messages": {
            "name": "Xem tin nhắn Messenger",
            "steps": [
                "1. navigate → https://www.facebook.com/messages",
                "2. wait 3s (Messenger load chậm)",
                "3. Click vào cuộc hội thoại → click_index",
                "4. wait 2s",
                "5. done hoặc type tin nhắn",
            ],
            "tips": "Messenger có DOM phức tạp. Cuộc hội thoại là div có role='row' hoặc link có href chứa /messages/t/.",
        },
        "interact_group": {
            "name": "Tương tác trong nhóm",
            "steps": [
                "1. navigate → https://www.facebook.com/groups",
                "2. wait 2s",
                "3. Click vào nhóm cần tương tác → click_index",
                "4. wait 2s",
                "5. Tương tác: like/comment/post trong nhóm",
                "6. done",
            ],
            "tips": "Trong nhóm, ô viết bài tương tự newsfeed. Các nhóm được list dưới dạng link.",
        },
        "browse_feed": {
            "name": "Lướt newsfeed",
            "steps": [
                "1. navigate → https://www.facebook.com/",
                "2. wait 2s",
                "3. scroll down 400-600px",
                "4. Xem bài viết, like/comment nếu cần",
                "5. Lặp scroll + tương tác",
                "6. done",
            ],
            "tips": "Feed là vùng cuộn chính. Mỗi bài viết thường có data-testid. Scroll lượng 300-500 là hợp lý.",
        },
        "watch_reels": {
            "name": "Xem Reels",
            "steps": [
                "1. navigate → https://www.facebook.com/reel/",
                "2. wait 3s",
                "3. Xem reel hiện tại",
                "4. scroll down để chuyển reel tiếp",
                "5. Lặp lại 3-4 và done",
            ],
            "tips": "Reels tự play. Scroll down = reel tiếp theo. Like reel: nút có aria-label='Like'/'Thích'.",
        },
        "visit_profile": {
            "name": "Xem trang cá nhân",
            "steps": [
                "1. navigate → https://www.facebook.com/me",
                "2. wait 2s",
                "3. Xem thông tin, scroll nếu cần",
                "4. done",
            ],
            "tips": "Profile có các section: Giới thiệu, Bạn bè, Ảnh, Bài viết. Scroll để load thêm.",
        },
        "marketplace": {
            "name": "Duyệt Marketplace",
            "steps": [
                "1. navigate → https://www.facebook.com/marketplace",
                "2. wait 2s",
                "3. Tìm kiếm sản phẩm hoặc browse danh mục",
                "4. Click vào sản phẩm → xem chi tiết",
                "5. done",
            ],
            "tips": "Marketplace có search bar ở trên. Sản phẩm hiển thị dạng grid với ảnh + giá.",
        },
        "upload_reel": {
            "name": "Đăng Reels lên Facebook",
            "steps": [
                "1. navigate → https://www.facebook.com/reels/create",
                "2. wait 3s — trang tạo Reels sẽ load",
                "3. Dùng upload_reel action: upload_reel với video_folder chứa đường dẫn thư mục video, topic mô tả nội dung, tone giọng điệu",
                "4. Hệ thống tự động: chọn video → upload → viết caption → đăng",
                "5. done",
            ],
            "tips": "DÙNG ACTION upload_reel ĐỂ ĐĂNG REEL. KHÔNG click thủ công. "
                    "Hệ thống tự xử lý file input, chờ upload, viết caption AI, click Đăng. "
                    "Nếu task có đường dẫn thư mục (VD: C:\\Users\\...\\mypham) → truyền vào video_folder. "
                    "Nếu không có đường dẫn → agent hỏi hoặc failed.",
        },
        "create_page": {
            "name": "Tạo Facebook Page (Fanpage)",
            "steps": [
                "1. Dùng create_page action: create_page với page_name tên page, category danh mục, description mô tả",
                "2. Hệ thống tự động: mở /pages/create → điền tên → gõ danh mục → chọn gợi ý → điền mô tả → click Tạo Trang",
                "3. done",
            ],
            "tips": "DÙNG ACTION create_page ĐỂ TẠO PAGE. KHÔNG navigate hay click thủ công. "
                    "KHÔNG dùng click_index hay type. create_page tự xử lý toàn bộ: "
                    "navigate + gõ tên + gõ category (trigger dropdown) + chọn + gõ bio + click Tạo Trang. "
                    "Chỉ cần truyền page_name, category, description.",
        },
        "list_fb_pages": {
            "name": "Xem danh sách Facebook Page đã tạo",
            "steps": [
                "1. Dùng list_fb_pages action",
                "2. Hệ thống tự động: mở /pages → đọc danh sách page → trả kết quả",
                "3. done",
            ],
            "tips": "DÙNG ACTION list_fb_pages. KHÔNG navigate thủ công. "
                    "Hệ thống tự mở trang Pages và đọc danh sách.",
        },
    }

    def _get_builtin_template(self, task: str) -> str:
        """Get built-in skill template for common tasks (zero-shot knowledge)."""
        pattern = self._classify_task(task)
        template = self._BUILTIN_TEMPLATES.get(pattern)
        if not template:
            return ""
        steps_text = "\n".join(template["steps"])
        return (
            f"\n\n🎯 TEMPLATE SẴN CÓ — '{template['name']}':\n"
            f"{steps_text}\n"
            f"💡 Tips: {template['tips']}\n"
            f"(Đây là template tham khảo, hãy adapt linh hoạt theo tình huống thực tế)"
        )

    def _agent_make_plan(self, task: str, profile_uuid: str) -> str:
        """Ask Grok SUPERVISOR to create a detailed plan.
        Enhanced with Facebook domain knowledge + builtin templates."""
        # Get current page info
        dom_summary, _ = self._get_indexed_dom(profile_uuid)
        current_url = ""
        for line in dom_summary.split("\n"):
            if line.startswith("URL:"):
                current_url = line[5:].strip()
                break

        # Get builtin template hint
        template_hint = self._get_builtin_template(task)

        # Get persistent memories for this profile
        memory_hint = ""
        try:
            memories = _db.get_agent_memories(profile_uuid, limit=5)
            if memories:
                memory_hint = "\n\n📚 KINH NGHIỆM TỪ SESSIONS TRƯỚC:\n"
                for m in memories[:5]:
                    memory_hint += f"  - [{m.get('memory_type', '?')}] {m.get('content', '')[:80]}\n"
        except Exception:
            pass

        plan_prompt = f"""Task: {task}
Current URL: {current_url}

FB URLs: / (feed), /me (profile), /friends/requests, /messages, /watch, /reel/, /groups, /marketplace, /search/top?q=KEYWORD, /pages
Actions: navigate,click_index,click_selector,type,scroll,wait,compose_post,upload_reel,create_page,list_fb_pages,done,failed
Rules: navigate first→wait 2-3s→interact. compose_post auto-handles posting. wait after navigate.
{template_hint}{memory_hint}
Plan 3-6 specific steps. List only."""

        try:
            result = _call_grok([
                {"role": "system", "content": "You are a browser automation planner. Output concise step list."},
                {"role": "user", "content": plan_prompt}
            ], max_tokens=300, temperature=0.25, timeout=25)
            if result["ok"]:
                return result["content"]
            return ""
        except Exception as e:
            print(f"[AGENT] Plan error: {e}")
            return ""

    # ── Ask AI for next action ──

    def _ask_agent_ai(self, task: str, image_b64: str,
                       dom_summary: str, steps_log: List[Dict],
                       plan: str = "", extra_warning: str = "",
                       memory: Dict = None) -> Optional[Dict]:
        """Send screenshot + DOM + memory to Grok, get next action JSON."""
        # Build step history (last 5 only to save tokens)
        history = ""
        if steps_log:
            history = "\nSTEPS:\n"
            for s in steps_log[-5:]:
                icon = "✅" if s.get("ok") else "❌"
                history += f"  {s.get('step','?')}: {icon} {s.get('desc', '?')[:50]}\n"

        # Build plan section (compressed)
        plan_section = ""
        if plan:
            plan_section = f"\nPLAN: {plan[:300]}\n"

        # Build memory section (compressed — only critical info)
        memory_section = ""
        if memory:
            parts = []
            if memory.get("failed_actions"):
                parts.append("FAIL: " + "; ".join(memory["failed_actions"][-3:]))
            if memory.get("current_state"):
                parts.append(f"State: {memory['current_state'][:60]}")
            if memory.get("reflection"):
                parts.append(f"Fix: {memory['reflection'][:80]}")
            if parts:
                memory_section = "\nMEM: " + " | ".join(parts) + "\n"

        user_msg = (
            f"TASK: {task}\n"
            f"{plan_section}{memory_section}"
            f"DOM:\n{dom_summary}\n"
            f"{history}{extra_warning}\n"
            f"→ JSON bước tiếp:"
        )

        messages = [{"role": "system", "content": AGENT_SYSTEM}]

        # Build user message with optional image
        if image_b64:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    {"type": "text", "text": user_msg}
                ]
            })
        else:
            messages.append({"role": "user", "content": user_msg})

        try:
            # Always use Grok for step decisions (accurate + fast)
            result = _call_grok(messages, max_tokens=250, temperature=0.1, timeout=30)

            if not result["ok"]:
                print(f"[AGENT-AI] All AI providers failed")
                return None

            content = result["content"]
            model_used = result["model"]
            print(f"[AGENT-AI] ({model_used}) Response: {content[:300]}")

            # Parse JSON from response
            clean = re.sub(r'```(?:json)?', '', content).strip()
            m = re.search(r'\{.*\}', clean, re.DOTALL)
            if m:
                parsed = json.loads(m.group())
                if "action" in parsed:
                    return parsed
                print(f"[AGENT-AI] No 'action' key in: {parsed}")

            print(f"[AGENT-AI] No valid JSON in response")
            return None

        except Exception as e:
            print(f"[AGENT-AI] Error: {e}")
            return None

    def _self_reflect(self, task: str, failed_action: Dict,
                       dom_summary: str, steps_log: List[Dict],
                       memory: Dict = None) -> str:
        """Self-reflection: analyze why an action failed and suggest alternative.
        Returns reflection text to inject into memory."""
        failed_desc = failed_action.get("desc", "unknown")
        action_type = failed_action.get("action_type", "?")

        # Build context
        recent_steps = ""
        for s in steps_log[-5:]:
            icon = "✅" if s.get("ok") else "❌"
            recent_steps += f"  {icon} {s.get('desc', '?')[:60]}\n"

        memory_ctx = ""
        if memory and memory.get("failed_actions"):
            memory_ctx = f"\nActions đã fail trước đó: {'; '.join(memory['failed_actions'][-3:])}\n"

        prompt = f"""Task: {task}
Failed: {action_type} — {failed_desc}
{memory_ctx}Recent:
{recent_steps}
DOM: {dom_summary[:300]}

Why did it fail? What to try instead? (2 sentences max)"""

        try:
            result = _call_grok([
                {"role": "system", "content": "Analyze browser action failure. 2 sentences max."},
                {"role": "user", "content": prompt}
            ], max_tokens=120, temperature=0.2, timeout=15)
            if result["ok"]:
                reflection = result["content"][:200]
                print(f"[AGENT-REFLECT] {reflection}")
                return reflection
        except Exception as e:
            print(f"[AGENT-REFLECT] Error: {e}")
        return ""

    def _dynamic_replan(self, task: str, steps_log: List[Dict],
                         memory: Dict, current_plan: str) -> str:
        """Re-evaluate and update the plan based on current progress."""
        ok_steps = [s for s in steps_log if s.get("ok")]
        fail_steps = [s for s in steps_log if not s.get("ok")]

        progress = ""
        for s in steps_log[-8:]:
            icon = "✅" if s.get("ok") else "❌"
            progress += f"  {icon} {s.get('desc', '?')[:60]}\n"

        failed_info = ""
        if memory.get("failed_actions"):
            failed_info = f"\nActions đã fail: {'; '.join(memory['failed_actions'][-5:])}\n"

        prompt = f"""Task: {task}
Old plan: {current_plan[:200]}
Progress ({len(ok_steps)} OK/{len(fail_steps)} FAIL):
{progress}{failed_info}
Update plan: 3-5 remaining steps. Skip done steps, avoid failed actions."""

        try:
            result = _call_grok([
                {"role": "system", "content": "Update automation plan. Concise step list only."},
                {"role": "user", "content": prompt}
            ], max_tokens=200, temperature=0.3, timeout=15)
            if result["ok"]:
                new_plan = result["content"]
                print(f"[AGENT-REPLAN] Updated plan: {new_plan[:150]}")
                return new_plan
        except Exception as e:
            print(f"[AGENT-REPLAN] Error: {e}")
        return current_plan

    # ── Execute a single agent action ──

    def _execute_agent_action(self, ai_action: Dict, profile_uuid: str,
                               dom_elements: List[Dict],
                               step_num: int,
                               task_context: Dict = None) -> Dict:
        """Execute one AI action and return step result."""
        action = ai_action.get("action", "?")
        reason = ai_action.get("reason", "")

        try:
            # ── DONE ──
            if action == "done":
                summary = ai_action.get("summary", "Hoàn thành")
                return {"step": step_num, "ok": True, "desc": f"DONE: {summary}"}

            # ── FAILED ──
            if action == "failed":
                return {"step": step_num, "ok": False, "desc": f"FAILED: {reason}"}

            # ── CLICK BY INDEX (fastest) ──
            if action == "click_index":
                idx = int(ai_action.get("index", -1))
                elem = next((e for e in dom_elements if e["index"] == idx), None)
                if not elem:
                    return {"step": step_num, "ok": False,
                            "desc": f"click_index [{idx}]: not found in DOM"}

                print(f"[AGENT] click_index [{idx}]: {elem['desc'][:50]}")

                # Step 0: scrollIntoView + get fresh coords
                fresh = self._scroll_and_get_coords(profile_uuid, idx)
                if fresh:
                    cx_f, cy_f = fresh
                    print(f"[AGENT] scrolled → fresh coords ({cx_f},{cy_f})")
                else:
                    cx_f = elem.get("cx", 0)
                    cy_f = elem.get("cy", 0)

                # Method 1: CDP mouse click at fresh center (most reliable)
                if cx_f > 0 and cy_f > 0:
                    ok = self._cdp_mouse_click(profile_uuid, cx_f, cy_f)
                    if ok:
                        time.sleep(1.5)
                        return {"step": step_num, "ok": True,
                                "desc": f"click [{idx}] mouse({cx_f},{cy_f}): {elem['desc'][:35]}"}

                # Method 2: JS click (with scrollIntoView)
                ok_js = self._js_click_by_index(profile_uuid, idx)
                if ok_js:
                    time.sleep(1.5)
                    return {"step": step_num, "ok": True,
                            "desc": f"click [{idx}] JS: {elem['desc'][:40]}"}

                # Method 3: CSS selector click
                if elem.get("selector"):
                    result = self.click_element(elem["selector"], profile_uuid)
                    if result.get("success"):
                        time.sleep(1.5)
                        return {"step": step_num, "ok": True,
                                "desc": f"click [{idx}] selector: {elem['desc'][:40]}"}
                    print(f"[AGENT] selector click failed: {result.get('error', '')[:50]}")

                return {"step": step_num, "ok": False,
                        "desc": f"click [{idx}]: all methods failed"}

            # ── CLICK BY SELECTOR ──
            if action == "click_selector":
                sel = ai_action.get("selector", "")
                if not sel:
                    return {"step": step_num, "ok": False,
                            "desc": "click_selector: empty selector"}

                result = self.click_element(sel, profile_uuid)
                if result.get("success"):
                    time.sleep(1.5)
                    return {"step": step_num, "ok": True,
                            "desc": f"click_selector: {sel[:50]}"}

                # Fallback: vision
                try:
                    result2 = self.vision_click_target(sel, profile_uuid=profile_uuid)
                    if result2.get("success"):
                        time.sleep(1.5)
                        return {"step": step_num, "ok": True,
                                "desc": f"click_sel->vision: {sel[:45]}"}
                except Exception:
                    pass

                return {"step": step_num, "ok": False,
                        "desc": f"click_selector FAIL: {sel[:40]}"}

            # ── CLICK BY VISION (slowest) ──
            if action in ("click_vision", "click"):
                target = ai_action.get("target", "")
                if not target:
                    return {"step": step_num, "ok": False,
                            "desc": "click_vision: empty target"}
                try:
                    result = self.vision_click_target(target, profile_uuid=profile_uuid)
                    ok = result.get("success", False)
                    time.sleep(1.5 if ok else 0.5)
                    detail = 'OK' if ok else result.get('error', 'fail')[:30]
                    return {"step": step_num, "ok": ok,
                            "desc": f"click_vision '{target[:30]}': {detail}"}
                except Exception as e:
                    return {"step": step_num, "ok": False,
                            "desc": f"click_vision error: {str(e)[:40]}"}

            # ── COMPOSE POST (reliable post creation) ──
            if action == "compose_post":
                topic = ai_action.get("topic", "") or ai_action.get("text", "")
                tone = ai_action.get("tone", "")
                if not topic:
                    return {"step": step_num, "ok": False,
                            "desc": "compose_post: empty topic"}
                # Generate quality content via dedicated AI call
                post_text = self._generate_post_content(topic, tone)
                if not post_text:
                    return {"step": step_num, "ok": False,
                            "desc": "compose_post: content generation failed"}
                result = self._compose_post(profile_uuid, post_text)
                return {"step": step_num, "ok": result.get("success", False),
                        "desc": result.get("desc", "compose_post: unknown")}

            # ── SMART COMMENT (context-aware commenting) ──
            if action == "smart_comment":
                tone = ai_action.get("tone", "")
                result = self._smart_comment(profile_uuid, tone, step_num)
                return result

            # ── UPLOAD REEL (automated reel creation) ──
            if action == "upload_reel":
                video_folder = ai_action.get("video_folder", "")
                topic = ai_action.get("topic", "") or ai_action.get("text", "")
                tone = ai_action.get("tone", "")

                # Smart path resolution: prefer extracted real path over AI-guessed path
                import os as _os
                extracted = (task_context or {}).get("extracted_folder", "")
                if video_folder and _os.path.isdir(video_folder.strip().strip('"').strip("'")):
                    video_folder = video_folder.strip().strip('"').strip("'")
                elif extracted and _os.path.isdir(extracted):
                    print(f"[AGENT] upload_reel: AI path '{video_folder}' invalid, using extracted: {extracted}")
                    video_folder = extracted
                elif video_folder:
                    # Try fixing: strip trailing words AI may have appended
                    vf = video_folder.strip().strip('"').strip("'")
                    while vf and not _os.path.isdir(vf):
                        parent = vf.rsplit(' ', 1)[0] if ' ' in vf else vf.rsplit('\\', 1)[0]
                        if parent == vf:
                            break
                        vf = parent
                    if _os.path.isdir(vf):
                        print(f"[AGENT] upload_reel: fixed AI path '{video_folder}' → '{vf}'")
                        video_folder = vf
                    elif extracted:
                        video_folder = extracted

                if not video_folder:
                    return {"step": step_num, "ok": False,
                            "desc": "upload_reel: empty video_folder"}
                result = self._upload_reel(profile_uuid, video_folder, topic, tone)
                return {"step": step_num, "ok": result.get("success", False),
                        "desc": result.get("desc", "upload_reel: unknown")}

            # ── CREATE PAGE (automated Facebook Page creation) ──
            if action == "create_page":
                page_name = ai_action.get("page_name", "") or ai_action.get("name", "")
                category = ai_action.get("category", "Giải trí")
                description = ai_action.get("description", "")
                if not page_name:
                    return {"step": step_num, "ok": False,
                            "desc": "create_page: empty page_name"}
                result = self._create_page(profile_uuid, page_name, category, description)
                return {"step": step_num, "ok": result.get("success", False),
                        "desc": result.get("desc", "create_page: unknown")}

            # ── LIST FB PAGES (read page list from Facebook) ──
            if action == "list_fb_pages":
                result = self._list_fb_pages(profile_uuid)
                desc = result.get("desc", "list_fb_pages: unknown")
                pages = result.get("pages", [])
                if pages:
                    desc += " | " + ", ".join(p.get('name', '?') for p in pages[:10])
                return {"step": step_num, "ok": result.get("success", False),
                        "desc": desc}

            # ── SCROLL ──
            if action == "scroll":
                direction = ai_action.get("direction", "down")
                amount = int(ai_action.get("amount", 400))
                if direction == "up":
                    amount = -amount
                try:
                    self.execute_js(f"window.scrollBy(0, {amount})", profile_uuid)
                    time.sleep(1)
                    return {"step": step_num, "ok": True,
                            "desc": f"scroll {direction} {abs(amount)}px"}
                except Exception as e:
                    return {"step": step_num, "ok": False,
                            "desc": f"scroll failed: {e}"}

            # ── NAVIGATE ──
            if action == "navigate":
                url = ai_action.get("url", "")
                if not url:
                    return {"step": step_num, "ok": False,
                            "desc": "navigate: empty URL"}
                result = self.navigate(url, profile_uuid)
                time.sleep(3)
                ok = result.get("success", False)
                return {"step": step_num, "ok": ok,
                        "desc": f"navigate {url[:45]}: {'OK' if ok else result.get('error', 'fail')[:25]}"}

            # ── TYPE ──
            if action == "type":
                text = ai_action.get("text", "")
                idx = ai_action.get("index")
                selector = ai_action.get("selector", "")
                target = ai_action.get("target", "")

                # Try to focus the input field
                click_ok = False
                if idx is not None:
                    # Scroll into view + fresh coords + mouse click
                    elem = next((e for e in dom_elements if e["index"] == int(idx)), None)
                    if elem:
                        fresh = self._scroll_and_get_coords(profile_uuid, int(idx))
                        if fresh:
                            click_ok = self._cdp_mouse_click(profile_uuid, fresh[0], fresh[1])
                        if not click_ok and elem.get("selector"):
                            r = self.click_element(elem["selector"], profile_uuid)
                            click_ok = r.get("success", False)
                        if not click_ok:
                            click_ok = self._cdp_mouse_click(
                                profile_uuid, elem.get("cx", 0), elem.get("cy", 0))
                elif selector:
                    r = self.click_element(selector, profile_uuid)
                    click_ok = r.get("success", False)
                elif target:
                    try:
                        r = self.vision_click_target(target, profile_uuid=profile_uuid)
                        click_ok = r.get("success", False)
                    except Exception:
                        pass

                if not click_ok:
                    # Last resort: try to focus any visible contenteditable
                    focus_js = """
                    (function(){
                        var el = document.querySelector('[contenteditable="true"][role="textbox"]:not([aria-hidden])');
                        if (!el) el = document.querySelector('[contenteditable="true"]:not([aria-hidden])');
                        if (!el) el = document.querySelector('textarea:not([aria-hidden])');
                        if (el) { el.focus(); return 'focused'; }
                        return 'not_found';
                    })()
                    """
                    try:
                        r = self.execute_js(focus_js, profile_uuid)
                        click_ok = r.get("result") == "focused"
                    except Exception:
                        pass

                if not click_ok:
                    return {"step": step_num, "ok": False,
                            "desc": f"type: cannot focus input"}

                time.sleep(0.5)
                cdp = self._get_cdp(profile_uuid)
                if cdp:
                    try:
                        # Use insertText for reliable Unicode/Vietnamese input
                        cdp._send_command('Input.insertText', {'text': text})
                        time.sleep(0.5)
                        return {"step": step_num, "ok": True,
                                "desc": f"type '{text[:25]}' OK"}
                    except Exception as e:
                        return {"step": step_num, "ok": False,
                                "desc": f"type CDP error: {e}"}

                return {"step": step_num, "ok": False, "desc": "type: no CDP"}

            # ── WAIT ──
            if action == "wait":
                seconds = min(int(ai_action.get("seconds", 2)), 10)
                time.sleep(seconds)
                return {"step": step_num, "ok": True, "desc": f"wait {seconds}s"}

            # Unknown action
            return {"step": step_num, "ok": False,
                    "desc": f"Unknown action: {action}"}

        except Exception as e:
            print(f"[AGENT] Execute error: {e}")
            traceback.print_exc()
            return {"step": step_num, "ok": False,
                    "desc": f"Exception: {str(e)[:60]}"}

    # ── Helper: scroll into view + fresh coordinates ──

    def _scroll_and_get_coords(self, profile_uuid: str, index: int):
        """Scroll element into view and return fresh (cx, cy) center coordinates.
        MUST use same selectors/filters/dedup as _get_indexed_dom!"""
        js = """
        (function() {
            var sels = 'a[href], button, [role="button"], [role="link"], [role="tab"], ' +
                       '[role="menuitem"], input, textarea, [role="textbox"], ' +
                       '[aria-label], [data-testid], [role="switch"], [role="checkbox"], ' +
                       '[role="dialog"], [role="menu"], [contenteditable="true"], ' +
                       '[role="option"], [role="listbox"], [role="combobox"], ' +
                       'select, [role="img"][aria-label], [role="article"], ' +
                       '[data-pagelet], form';
            var els = document.querySelectorAll(sels);
            var idx = 0;
            var seen = new Set();
            for (var i = 0; i < els.length && idx < 180; i++) {
                var el = els[i];
                var rect = el.getBoundingClientRect();
                if (rect.width < 5 || rect.height < 5) continue;
                if (rect.bottom < -100 || rect.top > window.innerHeight * 2.5) continue;
                var cx = Math.round(rect.left + rect.width / 2);
                var cy = Math.round(rect.top + rect.height / 2);
                var posKey = Math.round(cx/3) + '_' + Math.round(cy/3);
                if (seen.has(posKey)) continue;
                seen.add(posKey);
                if (idx === """ + str(index) + """) {
                    el.scrollIntoView({block: 'center', behavior: 'instant'});
                    var r2 = el.getBoundingClientRect();
                    var cx2 = Math.round(r2.left + r2.width / 2);
                    var cy2 = Math.round(r2.top + r2.height / 2);
                    return JSON.stringify({ok: true, cx: cx2, cy: cy2});
                }
                idx++;
            }
            return JSON.stringify({ok: false});
        })()
        """
        try:
            result = self.execute_js(js, profile_uuid)
            val = result.get("result", "")
            data = json.loads(val) if isinstance(val, str) else val
            if data.get("ok"):
                time.sleep(0.3)
                print(f"[AGENT] scroll_and_coords idx={index} → ({data['cx']},{data['cy']})")
                return (data["cx"], data["cy"])
        except Exception as e:
            print(f"[AGENT] scroll_and_coords error: {e}")
        return None

    # ── Helper: generate post content via dedicated AI call ──

    def _generate_post_content(self, topic: str, tone: str = "") -> Optional[str]:
        """Generate quality Facebook post content using a dedicated creative AI call."""
        tone_desc = tone if tone else "tự nhiên, chân thật"
        prompt = f"""Viết 1 bài đăng Facebook ngắn gọn (3-5 câu, tối đa 200 ký tự) về chủ đề: {topic}

Giọng điệu: {tone_desc}
Yêu cầu:
- Viết tự nhiên như người thật đăng Facebook, KHÔNG lặp lại câu
- Dùng emoji phù hợp (1-3 emoji, không quá nhiều)
- KHÔNG hashtag trừ khi được yêu cầu
- KHÔNG viết dài dòng, mỗi ý 1 câu ngắn
- Chỉ trả về NỘI DUNG bài đăng, không giải thích gì thêm"""

        try:
            result = _call_worker([
                {"role": "system", "content": "Bạn là người Việt trẻ, viết Facebook post tự nhiên và sáng tạo. Chỉ trả về nội dung bài đăng, không thêm gì khác."},
                {"role": "user", "content": prompt}
            ], max_tokens=300, temperature=0.8, timeout=20)

            if result["ok"]:
                content = result["content"]
                # Clean up: remove quotes, markdown, etc.
                content = content.strip('"\'')
                content = re.sub(r'^```.*?\n', '', content)
                content = content.strip('`').strip()
                # Remove any "Bài đăng:" or "Nội dung:" prefix
                content = re.sub(r'^(Bài đăng|Nội dung|Post|Content)\s*[:：]\s*', '', content, flags=re.IGNORECASE)
                content = content.strip()
                if len(content) > 20:
                    print(f"[AGENT] Generated post content ({len(content)} chars): {content[:80]}...")
                    return content
                print(f"[AGENT] Generated content too short: '{content}'")
                return None
            print(f"[AGENT] Content gen: AI call failed")
            return None
        except Exception as e:
            print(f"[AGENT] Content gen error: {e}")
            return None

    # ── Helper: compose_post (reliable Facebook post creation) ──

    def _compose_post(self, profile_uuid: str, text: str) -> dict:
        """Click 'Bạn đang nghĩ gì?' → type text → click Post button.
        Handles different viewport sizes by scrolling to find the compose trigger."""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "desc": "compose_post: no CDP connection"}

        try:
            # ── Step 0: Ensure we're on OWN profile page ──
            # Check current URL — if not /me or own profile, navigate there first
            url_check = self.execute_js("window.location.href", profile_uuid)
            current_url = str(url_check.get("result", ""))
            print(f"[AGENT] compose_post: current URL = {current_url}")
            
            # Navigate to /me if not already on own profile
            if '/me' not in current_url or 'profile.php' in current_url:
                print(f"[AGENT] compose_post: navigating to /me first...")
                self.execute_js("window.location.href = 'https://www.facebook.com/me'", profile_uuid)
                time.sleep(4)  # Wait for profile page to load
            # ── Step 1: Scroll progressively to find compose box ──
            # On profile pages it's below cover photo + header + tabs
            find_compose_js = """
            (function() {
                // Method 1: Find LEAF elements containing "nghĩ gì" / "What's on your mind"
                // Must be a LEAF span/div (not a parent container with lots of children)
                var candidates = document.querySelectorAll('span, div[role="button"], div[tabindex]');
                for (var el of candidates) {
                    // Skip elements with many children (containers)
                    if (el.children.length > 5) continue;
                    var t = (el.innerText || '').trim();
                    // Must contain the trigger text
                    if (!t.includes('nghĩ gì') && !t.includes("What's on your mind") &&
                        !t.includes('Viết gì đó') && !t.includes('Write something')) continue;
                    // Must NOT be too long (the actual trigger is short, not a paragraph)
                    if (t.length > 80) continue;
                    // Must NOT contain "Video trực tiếp" or "Ảnh/video" (that's the parent container)
                    if (t.includes('Video trực tiếp') || t.includes('Ảnh/video') ||
                        t.includes('Live video') || t.includes('Photo/video')) continue;
                    
                    var clickTarget = el;
                    // If it's a span, find closest clickable parent
                    if (el.tagName === 'SPAN') {
                        clickTarget = el.closest('[role="button"]') ||
                                      el.closest('div[tabindex="0"]') ||
                                      el.closest('a') || el;
                    }
                    clickTarget.scrollIntoView({block: 'center', behavior: 'instant'});
                    var r = clickTarget.getBoundingClientRect();
                    if (r.width > 30 && r.height > 10 && r.top > 0 && r.top < window.innerHeight) {
                        return JSON.stringify({
                            found: true, method: 'text_leaf',
                            cx: Math.round(r.left + r.width / 2),
                            cy: Math.round(r.top + r.height / 2),
                            text: t.substring(0, 40)
                        });
                    }
                }

                // Method 2: Find by aria-label
                var ariaSelectors = [
                    '[aria-label*="nghĩ gì"]',
                    '[aria-label*="on your mind"]',
                    '[aria-label*="Create a post"]',
                    '[aria-label*="Tạo bài viết"]'
                ];
                for (var sel of ariaSelectors) {
                    var el = document.querySelector(sel);
                    if (el) {
                        el.scrollIntoView({block: 'center', behavior: 'instant'});
                        var r = el.getBoundingClientRect();
                        if (r.width > 30 && r.height > 10 && r.top > 0 && r.top < window.innerHeight) {
                            return JSON.stringify({
                                found: true, method: 'aria:' + sel,
                                cx: Math.round(r.left + r.width / 2),
                                cy: Math.round(r.top + r.height / 2),
                                text: (el.getAttribute('aria-label') || '').substring(0, 40)
                            });
                        }
                    }
                }

                return JSON.stringify({found: false, debug: document.title});
            })()
            """

            compose_data = None
            # Try at different scroll positions to find the compose box
            for scroll_attempt in range(6):
                scroll_y = scroll_attempt * 350
                self.execute_js(f"window.scrollTo(0, {scroll_y})", profile_uuid)
                time.sleep(0.8)

                result = self.execute_js(find_compose_js, profile_uuid)
                raw = result.get("result", "{}")
                try:
                    data = json.loads(raw) if isinstance(raw, str) else raw
                except:
                    data = {}

                print(f"[AGENT] compose_post scroll={scroll_y}: {json.dumps(data, ensure_ascii=False)[:120]}")

                if data.get("found"):
                    compose_data = data
                    break

            if not compose_data:
                print(f"[AGENT] compose_post: FAILED to find compose box after 6 scroll attempts")
                return {"success": False,
                        "desc": "compose_post: cannot find 'Bạn đang nghĩ gì?' box after scrolling"}

            cx, cy = compose_data["cx"], compose_data["cy"]
            method = compose_data.get("method", "?")
            print(f"[AGENT] compose_post: found via {method} at ({cx},{cy}) text='{compose_data.get('text','')}'")
            time.sleep(0.5)

            # ── Step 2: Click with CDP mouse (real click to trigger FB modal) ──
            # Must click precisely on the text "Bạn đang nghĩ gì?" part, not the container
            self._cdp_mouse_click(profile_uuid, cx, cy)
            time.sleep(2.5)

            # ── Step 3: Check if dialog/modal opened, find textbox ONLY inside it ──
            # CRITICAL: Do NOT match comment boxes (which are form contenteditable outside dialog)
            focus_modal_js = """
            (function() {
                // ONLY look inside dialog/modal — never match comment boxes
                var dialog = document.querySelector('[role="dialog"]') ||
                             document.querySelector('[aria-modal="true"]');
                if (!dialog) {
                    return JSON.stringify({found: false, reason: 'no_dialog'});
                }
                // Verify this is a compose dialog (not photo viewer, etc.)
                var dialogText = (dialog.innerText || '').substring(0, 200).toLowerCase();
                var isCompose = dialogText.includes('tạo bài viết') || 
                                dialogText.includes('create post') ||
                                dialogText.includes('đăng') ||
                                dialogText.includes('post');
                if (!isCompose) {
                    // Check if there's a contenteditable anyway
                    var anyEditable = dialog.querySelector('[contenteditable="true"]');
                    if (!anyEditable) {
                        return JSON.stringify({found: false, reason: 'dialog_not_compose', 
                                              text: dialogText.substring(0, 60)});
                    }
                }
                // Find textbox inside dialog
                var selectors = [
                    '[contenteditable="true"][role="textbox"]',
                    '[contenteditable="true"][data-lexical-editor]',
                    '[contenteditable="true"]'
                ];
                for (var sel of selectors) {
                    var el = dialog.querySelector(sel);
                    if (el) {
                        el.focus();
                        el.click();
                        return JSON.stringify({found: true, sel: sel, inDialog: true});
                    }
                }
                return JSON.stringify({found: false, reason: 'no_textbox_in_dialog'});
            })()
            """

            # Try focus with retries — dialog may take time to appear
            focus_data = None
            for retry in range(4):
                focus_result = self.execute_js(focus_modal_js, profile_uuid)
                try:
                    fd = json.loads(focus_result.get("result", "{}"))
                except:
                    fd = {}
                print(f"[AGENT] compose_post focus attempt {retry+1}: {fd}")
                if fd.get("found"):
                    focus_data = fd
                    break

                reason = fd.get("reason", "")
                if reason == "no_dialog":
                    # Dialog didn't open — re-find and re-click compose trigger
                    print(f"[AGENT] compose_post: no dialog, re-clicking compose trigger...")
                    # Scroll back to compose area and find it again
                    result2 = self.execute_js(find_compose_js, profile_uuid)
                    try:
                        data2 = json.loads(result2.get("result", "{}"))
                    except:
                        data2 = {}
                    if data2.get("found"):
                        cx2, cy2 = data2["cx"], data2["cy"]
                        self._cdp_mouse_click(profile_uuid, cx2, cy2)
                    else:
                        self._cdp_mouse_click(profile_uuid, cx, cy)
                    time.sleep(2.5)
                else:
                    # Dialog exists but no textbox — wait more
                    time.sleep(1.5)

            if not focus_data:
                return {"success": False,
                        "desc": "compose_post: modal/textbox not found after clicking compose box"}

            print(f"[AGENT] compose_post: modal textbox found via {focus_data.get('sel')}")
            time.sleep(0.5)

            # ── Step 4: Type text word-by-word using insertText ──
            words = text.split(' ')
            for i, word in enumerate(words):
                chunk = word if i == 0 else ' ' + word
                cdp._send_command('Input.insertText', {'text': chunk})
                time.sleep(random.uniform(0.08, 0.25))

            print(f"[AGENT] compose_post: typed {len(text)} chars")
            time.sleep(1.5)

            # ── Step 5: Find and click the Post/Đăng button ──
            find_post_btn_js = """
            (function() {
                // Look for the Post/Đăng button in dialog
                var containers = document.querySelectorAll('[role="dialog"], [aria-modal="true"]');
                if (containers.length === 0) {
                    // No dialog found, search entire page
                    containers = [document];
                }
                for (var container of containers) {
                    var btns = container.querySelectorAll('[role="button"], button');
                    for (var b of btns) {
                        var txt = (b.innerText || b.textContent || '').trim();
                        var lower = txt.toLowerCase();
                        if (lower === 'đăng' || lower === 'post' || lower === 'share' ||
                            lower === 'đăng bài') {
                            if (b.getAttribute('aria-disabled') === 'true') continue;
                            b.scrollIntoView({block: 'center'});
                            var r = b.getBoundingClientRect();
                            return JSON.stringify({
                                found: true,
                                cx: Math.round(r.left + r.width / 2),
                                cy: Math.round(r.top + r.height / 2),
                                text: txt
                            });
                        }
                    }
                }
                return JSON.stringify({found: false});
            })()
            """
            btn_result = self.execute_js(find_post_btn_js, profile_uuid)
            try:
                btn_data = json.loads(btn_result.get("result", "{}"))
            except:
                btn_data = {}

            if not btn_data.get("found"):
                print(f"[AGENT] compose_post: Post/Đăng button not found!")
                return {"success": False, "desc": "compose_post: Post/Đăng button not found"}

            print(f"[AGENT] compose_post: clicking '{btn_data['text']}' at ({btn_data['cx']},{btn_data['cy']})")
            time.sleep(0.5)
            self._cdp_mouse_click(profile_uuid, btn_data["cx"], btn_data["cy"])
            time.sleep(3)

            return {"success": True,
                    "desc": f"compose_post: posted '{text[:30]}...' successfully"}

        except Exception as e:
            print(f"[AGENT] compose_post error: {e}")
            traceback.print_exc()
            return {"success": False, "desc": f"compose_post error: {str(e)[:50]}"}

    # ── Helper: create_page (automated Facebook Page creation) ──

    def _create_page(self, profile_uuid: str, page_name: str,
                     category: str = "Giải trí", description: str = "") -> dict:
        """Navigate to /pages/create → fill name → category → description → click Create.
        Uses CDP Input.insertText for typing (triggers React autocomplete properly)."""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "desc": "create_page: no CDP connection"}

        def _find_input(selectors: list, fallback_index: int = 0) -> dict:
            """Find an input field by multiple selectors. Returns {found, cx, cy} or {found: false}."""
            sels_json = json.dumps(selectors)
            js = f"""
            (function() {{
                var sels = {sels_json};
                for (var sel of sels) {{
                    var els = document.querySelectorAll(sel);
                    for (var el of els) {{
                        if (el.offsetParent !== null) {{
                            el.scrollIntoView({{block: 'center'}});
                            var r = el.getBoundingClientRect();
                            if (r.width > 20 && r.height > 10) {{
                                return JSON.stringify({{found: true, sel: sel,
                                    cx: Math.round(r.left + r.width/2),
                                    cy: Math.round(r.top + r.height/2),
                                    tag: el.tagName, aria: (el.getAttribute('aria-label')||'').substring(0,40)}});
                            }}
                        }}
                    }}
                }}
                // Fallback: nth visible input
                var inputs = document.querySelectorAll('input[type="text"], input:not([type])');
                var visible = [];
                for (var i of inputs) if (i.offsetParent !== null) visible.push(i);
                if (visible.length > {fallback_index}) {{
                    var el = visible[{fallback_index}];
                    el.scrollIntoView({{block: 'center'}});
                    var r = el.getBoundingClientRect();
                    return JSON.stringify({{found: true, sel: 'fallback_' + {fallback_index},
                        cx: Math.round(r.left + r.width/2), cy: Math.round(r.top + r.height/2),
                        tag: el.tagName, aria: (el.getAttribute('aria-label')||'').substring(0,40)}});
                }}
                return JSON.stringify({{found: false}});
            }})()
            """
            result = self.execute_js(js, profile_uuid)
            try:
                return json.loads(result.get("result", "{}"))
            except:
                return {"found": False}

        def _cdp_type_text(text: str, delay: float = 0.05):
            """Type text using CDP Input.insertText (triggers React events)."""
            for ch in text:
                cdp._send_command('Input.insertText', {'text': ch})
                time.sleep(delay + random.uniform(0, 0.03))

        def _cdp_clear_input():
            """Select all + delete to clear current input."""
            cdp._send_command("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "a", "code": "KeyA",
                "windowsVirtualKeyCode": 65, "modifiers": 2  # Ctrl
            })
            cdp._send_command("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "a", "code": "KeyA",
                "windowsVirtualKeyCode": 65, "modifiers": 2
            })
            time.sleep(0.1)
            cdp._send_command("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "Backspace", "code": "Backspace",
                "windowsVirtualKeyCode": 8
            })
            cdp._send_command("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "Backspace", "code": "Backspace",
                "windowsVirtualKeyCode": 8
            })
            time.sleep(0.1)

        def _cdp_key(key: str, code: str, keyCode: int):
            """Send a single key press via CDP."""
            cdp._send_command("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": key, "code": code,
                "windowsVirtualKeyCode": keyCode
            })
            cdp._send_command("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": key, "code": code,
                "windowsVirtualKeyCode": keyCode
            })

        try:
            # ── Step 1: Navigate to page creation URL ──
            print(f"[AGENT] create_page [STEP 1] navigating to /pages/create...")
            self.execute_js(
                "window.location.href = 'https://www.facebook.com/pages/create'",
                profile_uuid)
            time.sleep(6)

            # DOM snapshot for debugging
            snap_js = """
            (function() {
                var out = [];
                var els = document.querySelectorAll('input, textarea, [role="button"], button');
                for (var i = 0; i < Math.min(els.length, 30); i++) {
                    var e = els[i]; var r = e.getBoundingClientRect();
                    if (r.width < 5) continue;
                    var info = e.tagName + '[type=' + (e.type||'') + ']';
                    var aria = e.getAttribute('aria-label') || '';
                    if (aria) info += ' aria="' + aria.substring(0,30) + '"';
                    var txt = (e.innerText||'').substring(0,20);
                    if (txt) info += ' "' + txt + '"';
                    info += ' (' + Math.round(r.left) + ',' + Math.round(r.top) + ')';
                    out.push(info);
                }
                return out.join('\\n');
            })()
            """
            snap = self.execute_js(snap_js, profile_uuid)
            print(f"[AGENT] create_page [STEP 1] DOM:\n{snap.get('result', '')[:400]}")

            # ── Step 2: Fill page name via CDP typing ──
            print(f"[AGENT] create_page [STEP 2] filling page name: {page_name}")
            name_field = _find_input([
                'input[aria-label*="Tên"]',
                'input[aria-label*="name" i]',
                'input[aria-label*="Page name"]',
                'input.x1i10hfl[type="text"]'
            ], fallback_index=0)
            print(f"[AGENT] create_page [STEP 2] name field: {json.dumps(name_field, ensure_ascii=False)[:120]}")

            if not name_field.get("found"):
                return {"success": False, "desc": "create_page: cannot find page name input"}

            # Click to focus → clear → type
            self._cdp_mouse_click(profile_uuid, name_field["cx"], name_field["cy"])
            time.sleep(0.3)
            self._cdp_mouse_click(profile_uuid, name_field["cx"], name_field["cy"])
            time.sleep(0.3)
            _cdp_clear_input()
            _cdp_type_text(page_name, delay=0.04)
            print(f"[AGENT] create_page [STEP 2] typed name OK")
            time.sleep(1)

            # ── Step 3: Fill category via CDP typing (triggers React dropdown) ──
            print(f"[AGENT] create_page [STEP 3] filling category: {category}")
            cat_field = _find_input([
                'input[aria-label*="Hạng mục"]',
                'input[aria-label*="Category"]',
                'input[aria-label*="hạng mục"]'
            ], fallback_index=1)  # 2nd visible input = category
            print(f"[AGENT] create_page [STEP 3] category field: {json.dumps(cat_field, ensure_ascii=False)[:120]}")

            if cat_field.get("found"):
                # Click to focus → clear → type character by character
                self._cdp_mouse_click(profile_uuid, cat_field["cx"], cat_field["cy"])
                time.sleep(0.3)
                self._cdp_mouse_click(profile_uuid, cat_field["cx"], cat_field["cy"])
                time.sleep(0.3)
                _cdp_clear_input()
                _cdp_type_text(category, delay=0.06)
                print(f"[AGENT] create_page [STEP 3] typed category, waiting for dropdown...")
                time.sleep(2)

                # Check if dropdown appeared
                dropdown_js = """
                (function() {
                    var items = document.querySelectorAll('[role="option"], [role="listbox"] [role="option"], ul[role="listbox"] li');
                    if (items.length > 0) {
                        var first = items[0];
                        first.scrollIntoView({block: 'center'});
                        var r = first.getBoundingClientRect();
                        return JSON.stringify({found: true, count: items.length,
                            text: (first.innerText||'').substring(0,30),
                            cx: Math.round(r.left + r.width/2),
                            cy: Math.round(r.top + r.height/2)});
                    }
                    // Try generic dropdown items
                    var lists = document.querySelectorAll('[role="listbox"]');
                    if (lists.length > 0) {
                        var children = lists[0].children;
                        if (children.length > 0) {
                            var r = children[0].getBoundingClientRect();
                            return JSON.stringify({found: true, count: children.length,
                                text: (children[0].innerText||'').substring(0,30),
                                cx: Math.round(r.left + r.width/2),
                                cy: Math.round(r.top + r.height/2)});
                        }
                    }
                    return JSON.stringify({found: false});
                })()
                """
                dd_result = self.execute_js(dropdown_js, profile_uuid)
                try:
                    dd_data = json.loads(dd_result.get("result", "{}"))
                except:
                    dd_data = {}
                print(f"[AGENT] create_page [STEP 3] dropdown: {json.dumps(dd_data, ensure_ascii=False)[:120]}")

                if dd_data.get("found"):
                    # Click the first dropdown option
                    self._cdp_mouse_click(profile_uuid, dd_data["cx"], dd_data["cy"])
                    print(f"[AGENT] create_page [STEP 3] clicked dropdown option: {dd_data.get('text', '')}")
                    time.sleep(1)
                else:
                    # Fallback: ArrowDown + Enter
                    print(f"[AGENT] create_page [STEP 3] no dropdown visible, trying ArrowDown+Enter...")
                    _cdp_key("ArrowDown", "ArrowDown", 40)
                    time.sleep(0.5)
                    _cdp_key("Enter", "Enter", 13)
                    time.sleep(1)
            else:
                print(f"[AGENT] create_page [STEP 3] WARNING: no category input found")

            # ── Step 4: Fill bio/description ──
            bio_text = description
            if not bio_text:
                try:
                    desc_prompt = f"Viết mô tả ngắn (1-2 câu, max 80 ký tự) cho Facebook Page tên '{page_name}' danh mục '{category}'. Chỉ trả về mô tả, không giải thích."
                    desc_result = _call_worker([
                        {"role": "system", "content": "Bạn viết mô tả Facebook Page ngắn gọn."},
                        {"role": "user", "content": desc_prompt}
                    ], max_tokens=100, temperature=0.7, timeout=15)
                    if desc_result["ok"]:
                        bio_text = desc_result["content"].strip().strip('"\'\'`').strip()
                        print(f"[AGENT] create_page [STEP 4] auto-gen bio: {bio_text[:50]}")
                except Exception as e:
                    print(f"[AGENT] create_page auto-bio error (non-fatal): {e}")

            if bio_text:
                print(f"[AGENT] create_page [STEP 4] filling bio: {bio_text[:50]}...")
                bio_field = _find_input([], fallback_index=0)  # Won't match inputs
                # Find textarea specifically
                bio_js = """
                (function() {
                    var bio = document.querySelector('textarea.x1i10hfl');
                    if (!bio) {
                        var textareas = document.querySelectorAll('textarea');
                        for (var t of textareas) {
                            if (t.offsetParent !== null) { bio = t; break; }
                        }
                    }
                    if (bio && bio.offsetParent !== null) {
                        bio.scrollIntoView({block: 'center'});
                        var r = bio.getBoundingClientRect();
                        return JSON.stringify({found: true,
                            cx: Math.round(r.left + r.width/2),
                            cy: Math.round(r.top + r.height/2)});
                    }
                    return JSON.stringify({found: false});
                })()
                """
                bio_result = self.execute_js(bio_js, profile_uuid)
                try:
                    bio_data = json.loads(bio_result.get("result", "{}"))
                except:
                    bio_data = {}

                if bio_data.get("found"):
                    self._cdp_mouse_click(profile_uuid, bio_data["cx"], bio_data["cy"])
                    time.sleep(0.3)
                    _cdp_clear_input()
                    _cdp_type_text(bio_text, delay=0.03)
                    print(f"[AGENT] create_page [STEP 4] bio typed OK")
                    time.sleep(0.5)
                else:
                    print(f"[AGENT] create_page [STEP 4] no bio textarea found")

            # ── Step 5: Click "Tạo Trang" / "Create Page" button ──
            print(f"[AGENT] create_page [STEP 5] looking for Create Page button...")
            find_btn_js = """
            (function() {
                var btns = document.querySelectorAll('div[role="button"], button, span[role="button"]');
                for (var b of btns) {
                    var txt = (b.innerText || b.textContent || '').trim().toLowerCase();
                    var aria = (b.getAttribute('aria-label') || '').toLowerCase();
                    if (txt === 'create page' || txt === 'tạo trang' ||
                        aria.includes('create page') || aria.includes('tạo trang')) {
                        if (b.getAttribute('aria-disabled') === 'true') continue;
                        b.scrollIntoView({behavior: 'smooth', block: 'center'});
                        var r = b.getBoundingClientRect();
                        return JSON.stringify({found: true, text: txt.substring(0,20),
                            cx: Math.round(r.left + r.width/2),
                            cy: Math.round(r.top + r.height/2)});
                    }
                }
                return JSON.stringify({found: false});
            })()
            """
            btn_status = 'not_found'
            for click_try in range(3):
                btn_result = self.execute_js(find_btn_js, profile_uuid)
                try:
                    btn_data = json.loads(btn_result.get("result", "{}"))
                except:
                    btn_data = {}
                print(f"[AGENT] create_page [STEP 5] try {click_try+1}: {json.dumps(btn_data, ensure_ascii=False)[:100]}")

                if btn_data.get("found"):
                    self._cdp_mouse_click(profile_uuid, btn_data["cx"], btn_data["cy"])
                    btn_status = 'clicked'
                    print(f"[AGENT] create_page [STEP 5] clicked '{btn_data.get('text', '')}'")
                    break
                time.sleep(2)
                self.execute_js("window.scrollBy(0, 300)", profile_uuid)
                time.sleep(1)

            if btn_status != 'clicked':
                return {"success": False, "desc": "create_page: 'Tạo Trang' button not found"}

            # ── Step 6: Wait for page to be created (URL changes) ──
            print(f"[AGENT] create_page [STEP 6] waiting for page creation...")
            page_url = ""
            for wait in range(15):
                time.sleep(2)
                url_result = self.execute_js("window.location.href", profile_uuid)
                current_url = url_result.get("result", "")
                print(f"[AGENT] create_page [STEP 6] URL check {wait+1}: {current_url[:60]}")
                if '/pages/create' not in current_url and current_url:
                    page_url = current_url
                    print(f"[AGENT] create_page: SUCCESS! Page URL: {page_url}")
                    break

            if not page_url:
                # Still on /pages/create — check if there's an error message
                error_js = """
                (function() {
                    var page = document.body.innerText;
                    if (page.includes('lỗi') || page.includes('error') || page.includes('thất bại'))
                        return 'error_on_page';
                    // Check if form still visible (create button still there)
                    var btns = document.querySelectorAll('[role="button"]');
                    for (var b of btns) {
                        var txt = (b.innerText || '').trim().toLowerCase();
                        if (txt === 'tạo trang' || txt === 'create page') return 'still_on_form';
                    }
                    return 'unknown';
                })()
                """
                state = self.execute_js(error_js, profile_uuid).get("result", "")
                print(f"[AGENT] create_page [STEP 6] final state: {state}")
                if state == 'error_on_page':
                    return {"success": False,
                            "desc": f"create_page: Facebook showed error after clicking Create"}
                return {"success": True,
                        "desc": f"create_page: form submitted for '{page_name}', page may be creating..."}

            # ── Step 7: Verify page was actually created ──
            print(f"[AGENT] create_page [STEP 7] verifying page creation...")
            time.sleep(2)
            verify_js = """
            (function() {
                var url = window.location.href;
                var title = document.title;
                var page = document.body.innerText.substring(0, 500);
                var hasPageMgmt = page.includes('Quản lý') || page.includes('Manage') ||
                                  page.includes('Trang chủ') || page.includes('Dashboard');
                var hasPageName = page.includes('""" + page_name.replace("'", "\\'")[:30] + """');
                return JSON.stringify({url: url.substring(0,80), title: title.substring(0,50),
                    hasPageMgmt: hasPageMgmt, hasPageName: hasPageName});
            })()
            """
            verify_result = self.execute_js(verify_js, profile_uuid)
            try:
                verify_data = json.loads(verify_result.get("result", "{}"))
            except:
                verify_data = {}
            print(f"[AGENT] create_page [STEP 7] verify: {json.dumps(verify_data, ensure_ascii=False)[:200]}")

            return {"success": True,
                    "desc": f"DONE: Đã tạo page '{page_name}' ({category}). URL: {page_url[:60]}"}

        except Exception as e:
            print(f"[AGENT] create_page error: {e}")
            traceback.print_exc()
            return {"success": False, "desc": f"create_page error: {str(e)[:50]}"}

    # ── Helper: list_fb_pages (read pages owned by this account) ──

    def _list_fb_pages(self, profile_uuid: str) -> dict:
        """Navigate to Facebook Pages section and scrape the list of pages owned by this account."""
        try:
            cdp = self._get_cdp(profile_uuid)
            if not cdp:
                return {"success": False, "desc": "list_fb_pages: no CDP connection", "pages": []}

            # Step 1: Navigate to Pages management
            print(f"[AGENT] list_fb_pages [STEP 1] navigating to /pages/?category=your_pages")
            self.navigate("https://www.facebook.com/pages/?category=your_pages", profile_uuid)
            time.sleep(4)

            # Step 2: Extract page list from DOM
            print(f"[AGENT] list_fb_pages [STEP 2] extracting page list...")
            extract_js = """
            (function() {
                var pages = [];
                // Method 1: Look for page links in the content
                var links = document.querySelectorAll('a[href*="/pages/"][role="link"], a[href*="facebook.com/"][role="link"]');
                var seen = {};
                for (var link of links) {
                    var href = link.href || '';
                    var text = (link.innerText || '').trim();
                    // Skip navigation links, empty, or very long text
                    if (!text || text.length > 80 || text.length < 2) continue;
                    // Skip common FB navigation items
                    if (['Trang', 'Pages', 'Tạo Trang mới', 'Create new Page',
                         'Trang chủ', 'Home', 'Bảng tin', 'Xem thêm', 'See more',
                         'Meta Business Suite', 'Trung tâm quảng cáo'].includes(text)) continue;
                    // Must be a page link (not profile, group, etc.)
                    if (href.includes('/groups/') || href.includes('/profile.php') ||
                        href.includes('/friends') || href.includes('/notifications') ||
                        href.includes('/messages') || href.includes('/settings') ||
                        href.includes('/pages/create') || href.includes('/pages/?')) continue;
                    var key = text.toLowerCase();
                    if (seen[key]) continue;
                    seen[key] = true;
                    pages.push({name: text, url: href.substring(0, 100)});
                }

                // Method 2: If no links found, try looking for page cards/items
                if (pages.length === 0) {
                    var items = document.querySelectorAll('[role="listitem"], [data-testid]');
                    for (var item of items) {
                        var name = '';
                        var link = item.querySelector('a');
                        if (link) {
                            name = (link.innerText || '').trim();
                            var href = link.href || '';
                            if (name && name.length < 80 && name.length > 1 && !seen[name.toLowerCase()]) {
                                seen[name.toLowerCase()] = true;
                                pages.push({name: name, url: href.substring(0, 100)});
                            }
                        }
                    }
                }

                // Method 3: Look for spans/strong inside page sections
                if (pages.length === 0) {
                    var bodyText = document.body.innerText.substring(0, 3000);
                    return JSON.stringify({pages: [], bodyPreview: bodyText.substring(0, 500)});
                }

                return JSON.stringify({pages: pages});
            })()
            """
            result = self.execute_js(extract_js, profile_uuid)
            raw = result.get("result", "{}")
            try:
                data = json.loads(raw)
            except:
                data = {"pages": []}

            pages = data.get("pages", [])
            body_preview = data.get("bodyPreview", "")

            print(f"[AGENT] list_fb_pages [STEP 2] found {len(pages)} pages")

            if pages:
                page_names = ", ".join(p.get("name", "?") for p in pages[:20])
                return {
                    "success": True,
                    "desc": f"DONE: Tìm thấy {len(pages)} page: {page_names[:200]}",
                    "pages": pages,
                    "count": len(pages)
                }

            # Step 3: If no pages found, try scrolling and retry
            print(f"[AGENT] list_fb_pages [STEP 3] no pages found, scrolling + retry...")
            self.execute_js("window.scrollBy(0, 600)", profile_uuid)
            time.sleep(2)

            result2 = self.execute_js(extract_js, profile_uuid)
            raw2 = result2.get("result", "{}")
            try:
                data2 = json.loads(raw2)
            except:
                data2 = {"pages": []}
            pages2 = data2.get("pages", [])

            if pages2:
                page_names = ", ".join(p.get("name", "?") for p in pages2[:20])
                return {
                    "success": True,
                    "desc": f"DONE: Tìm thấy {len(pages2)} page: {page_names[:200]}",
                    "pages": pages2,
                    "count": len(pages2)
                }

            # No pages found at all
            if body_preview:
                # Check if page says "no pages" or similar
                bp_lower = body_preview.lower()
                if any(w in bp_lower for w in ['chưa có trang', 'no pages', 'bạn chưa', "you haven't"]):
                    return {
                        "success": True,
                        "desc": "DONE: Chưa có page nào được tạo (0 page)",
                        "pages": [],
                        "count": 0
                    }
            return {
                "success": True,
                "desc": f"DONE: Không tìm thấy page nào (có thể chưa tạo hoặc DOM thay đổi)",
                "pages": [],
                "count": 0
            }

        except Exception as e:
            print(f"[AGENT] list_fb_pages error: {e}")
            traceback.print_exc()
            return {"success": False, "desc": f"list_fb_pages error: {str(e)[:50]}", "pages": []}

    # ── Helper: upload_reel (automated Facebook Reels creation) ──

    def _upload_reel(self, profile_uuid: str, video_folder: str,
                     topic: str = "", tone: str = "") -> dict:
        """Navigate to Reels Create → pick a video file → upload → write caption → publish.
        Uses CDP DOM.setFileInputFiles for file selection.
        Every step analyzes DOM to find the right elements."""
        import os
        import glob

        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "desc": "upload_reel: no CDP connection"}

        def _dom_snapshot() -> str:
            """Take a DOM snapshot for logging/debugging."""
            js = """
            (function() {
                var out = [];
                var els = document.querySelectorAll(
                    '[role="button"], button, [contenteditable="true"], textarea, '
                    + 'input[type="text"], input[type="file"], [role="textbox"], '
                    + '[role="dialog"], [aria-label]');
                for (var i = 0; i < Math.min(els.length, 60); i++) {
                    var e = els[i];
                    var tag = e.tagName.toLowerCase();
                    var role = e.getAttribute('role') || '';
                    var txt = (e.innerText || '').substring(0, 50).replace(/\\n/g, ' ');
                    var aria = e.getAttribute('aria-label') || '';
                    var type = e.getAttribute('type') || '';
                    var disabled = e.getAttribute('aria-disabled') || '';
                    var accept = e.getAttribute('accept') || '';
                    var editable = e.getAttribute('contenteditable') || '';
                    var r = e.getBoundingClientRect();
                    if (r.width < 5 || r.height < 5) continue;
                    var info = i + ': ' + tag;
                    if (role) info += '[role=' + role + ']';
                    if (type) info += '[type=' + type + ']';
                    if (accept) info += '[accept=' + accept + ']';
                    if (editable) info += '[editable]';
                    if (disabled === 'true') info += '[DISABLED]';
                    if (txt) info += ' "' + txt + '"';
                    if (aria) info += ' aria="' + aria + '"';
                    info += ' (' + Math.round(r.left) + ',' + Math.round(r.top) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height) + ')';
                    out.push(info);
                }
                return out.join('\\n');
            })()
            """
            result = self.execute_js(js, profile_uuid)
            return result.get("result", "")

        def _find_button(keywords: list, exclude_disabled: bool = True) -> dict:
            """Find a button by text/aria-label keywords. Returns {found, cx, cy, text} or {found: false}."""
            kw_json = json.dumps(keywords)
            js = f"""
            (function() {{
                var keywords = {kw_json};
                var btns = document.querySelectorAll('[role="button"], button, a[role="link"]');
                for (var b of btns) {{
                    var txt = (b.innerText || '').trim().toLowerCase();
                    var aria = (b.getAttribute('aria-label') || '').toLowerCase();
                    var disabled = b.getAttribute('aria-disabled') === 'true';
                    if ({'disabled && ' if exclude_disabled else ''}false) continue;
                    {'' if not exclude_disabled else 'if (disabled) continue;'}
                    for (var kw of keywords) {{
                        if (txt === kw || aria === kw || txt.includes(kw) || aria.includes(kw)) {{
                            b.scrollIntoView({{block: 'center'}});
                            var r = b.getBoundingClientRect();
                            if (r.width > 5 && r.height > 5) {{
                                return JSON.stringify({{found: true, text: txt.substring(0,40),
                                    cx: Math.round(r.left + r.width/2),
                                    cy: Math.round(r.top + r.height/2),
                                    disabled: disabled}});
                            }}
                        }}
                    }}
                }}
                return JSON.stringify({{found: false}});
            }})()
            """
            result = self.execute_js(js, profile_uuid)
            try:
                return json.loads(result.get("result", "{}"))
            except:
                return {"found": False}

        try:
            # ── Step 0: Find video files in the folder ──
            video_folder = video_folder.strip().strip('"').strip("'")
            if not os.path.isdir(video_folder):
                return {"success": False,
                        "desc": f"upload_reel: folder not found: {video_folder}"}

            video_exts = ['*.mp4', '*.mov', '*.avi', '*.mkv', '*.webm', '*.m4v']
            video_files = []
            for ext in video_exts:
                video_files.extend(glob.glob(os.path.join(video_folder, ext)))
                video_files.extend(glob.glob(os.path.join(video_folder, ext.upper())))
            video_files = list(set(video_files))
            if not video_files:
                return {"success": False,
                        "desc": f"upload_reel: no video files in {video_folder}"}

            chosen_video = random.choice(video_files)
            chosen_video_abs = os.path.abspath(chosen_video)
            video_name = os.path.basename(chosen_video)
            print(f"[AGENT] upload_reel: chosen video = {video_name} ({len(video_files)} total)")

            # ── Step 0b: Dismiss any open native file dialog ──
            try:
                cdp._send_command("Input.dispatchKeyEvent", {
                    "type": "keyDown", "key": "Escape",
                    "code": "Escape", "windowsVirtualKeyCode": 27
                })
                cdp._send_command("Input.dispatchKeyEvent", {
                    "type": "keyUp", "key": "Escape",
                    "code": "Escape", "windowsVirtualKeyCode": 27
                })
                time.sleep(1)
                print(f"[AGENT] upload_reel: sent Escape to dismiss any file dialog")
            except Exception as e:
                print(f"[AGENT] upload_reel: Escape failed (non-fatal): {e}")

            # ── Step 1: Navigate to /reels/create ──
            print(f"[AGENT] upload_reel [STEP 1] navigating to /reels/create...")
            self.execute_js(
                "window.location.href = 'https://www.facebook.com/reels/create'",
                profile_uuid)
            time.sleep(5)

            # Analyze DOM after navigation
            dom = _dom_snapshot()
            print(f"[AGENT] upload_reel [STEP 1] DOM after navigate:\n{dom[:500]}")

            # ── Step 2: Find and set file via CDP ──
            print(f"[AGENT] upload_reel [STEP 2] finding file input...")
            file_input_data = None
            for attempt in range(5):
                find_js = """
                (function() {
                    var inputs = document.querySelectorAll('input[type="file"]');
                    for (var inp of inputs) {
                        var accept = (inp.getAttribute('accept') || '').toLowerCase();
                        if (accept.includes('video') || accept.includes('mp4') || 
                            accept === '' || accept === '*/*') {
                            return JSON.stringify({found: true, method: 'file_input', accept: accept});
                        }
                    }
                    if (inputs.length > 0) {
                        return JSON.stringify({found: true, method: 'any_file_input',
                                              accept: inputs[0].getAttribute('accept') || ''});
                    }
                    return JSON.stringify({found: false, inputs: inputs.length,
                                          url: window.location.href,
                                          title: document.title});
                })()
                """
                result = self.execute_js(find_js, profile_uuid)
                try:
                    data = json.loads(result.get("result", "{}"))
                except:
                    data = {}
                print(f"[AGENT] upload_reel [STEP 2] attempt {attempt+1}: {json.dumps(data, ensure_ascii=False)[:150]}")
                if data.get("found"):
                    file_input_data = data
                    break
                time.sleep(2)

            if not file_input_data:
                return {"success": False,
                        "desc": "upload_reel: no file input found on /reels/create"}

            # Make input visible + get objectId via CDP
            prep_js = """
            (function() {
                var inputs = document.querySelectorAll('input[type="file"]');
                for (var inp of inputs) {
                    var accept = (inp.getAttribute('accept') || '').toLowerCase();
                    if (accept.includes('video') || accept.includes('mp4') ||
                        accept === '' || accept === '*/*' || inputs.length === 1) {
                        inp.style.display = 'block';
                        inp.style.opacity = '1';
                        inp.style.position = 'fixed';
                        inp.style.zIndex = '99999';
                        return 'ready';
                    }
                }
                if (inputs.length > 0) { inputs[0].style.display = 'block'; return 'ready_first'; }
                return 'no_input';
            })()
            """
            self.execute_js(prep_js, profile_uuid)

            try:
                cdp._send_command("DOM.enable", {})
            except Exception:
                pass

            eval_resp = cdp._send_command("Runtime.evaluate", {
                "expression": """
                (function() {
                    var inputs = document.querySelectorAll('input[type="file"]');
                    for (var inp of inputs) {
                        var accept = (inp.getAttribute('accept') || '').toLowerCase();
                        if (accept.includes('video') || accept.includes('mp4') ||
                            accept === '' || accept === '*/*' || inputs.length === 1) {
                            return inp;
                        }
                    }
                    return inputs.length > 0 ? inputs[0] : null;
                })()
                """,
                "returnByValue": False
            })
            remote_obj = eval_resp.get("result", {}).get("result", {})
            object_id = remote_obj.get("objectId")
            if not object_id:
                print(f"[AGENT] upload_reel: no objectId: {eval_resp}")
                return {"success": False, "desc": "upload_reel: cannot get file input objectId"}

            print(f"[AGENT] upload_reel [STEP 2] objectId={object_id[:30]}...")
            set_result = cdp._send_command("DOM.setFileInputFiles", {
                "objectId": object_id,
                "files": [chosen_video_abs]
            })
            print(f"[AGENT] upload_reel [STEP 2] setFileInputFiles = {set_result}")
            if "error" in set_result:
                err = set_result.get("error", {}).get("message", str(set_result["error"]))
                return {"success": False, "desc": f"upload_reel: CDP error: {err[:50]}"}
            print(f"[AGENT] upload_reel [STEP 2] file set OK!")

            # ── Step 3: Wait for video processing ──
            print(f"[AGENT] upload_reel [STEP 3] waiting for video processing...")
            time.sleep(8)

            for wait_round in range(15):  # Up to ~75s
                state_js = """
                (function() {
                    var page = document.body.innerText.toLowerCase();
                    if (page.includes('đang xử lý') || page.includes('processing') ||
                        page.includes('uploading') || page.includes('đang tải')) return 'processing';
                    if (page.includes('lỗi tải') || page.includes('upload error') ||
                        page.includes('thất bại')) return 'error';
                    // Look for UI that means video is loaded (caption field, next button, etc)
                    var editable = document.querySelectorAll('[contenteditable="true"]');
                    var btns = document.querySelectorAll('[role="button"], button');
                    var hasNext = false, hasCaption = false;
                    for (var b of btns) {
                        var t = (b.innerText || '').trim().toLowerCase();
                        if (t === 'tiếp' || t === 'next' || t === 'tiếp theo') hasNext = true;
                        if (t === 'đăng' || t === 'publish' || t === 'share' ||
                            t === 'chia sẻ' || t === 'đăng reel') hasNext = true;
                    }
                    for (var e of editable) {
                        var r = e.getBoundingClientRect();
                        if (r.width > 50 && r.height > 15) hasCaption = true;
                    }
                    if (hasNext || hasCaption) return 'ready';
                    return 'waiting';
                })()
                """
                status = self.execute_js(state_js, profile_uuid)
                state = status.get("result", "waiting")
                print(f"[AGENT] upload_reel [STEP 3] check {wait_round+1}: {state}")
                if state == 'ready':
                    break
                if state == 'error':
                    return {"success": False, "desc": "upload_reel: FB error processing video"}
                time.sleep(5)

            # ── Step 4: Analyze DOM → find caption field → fill it ──
            dom = _dom_snapshot()
            print(f"[AGENT] upload_reel [STEP 4] DOM after upload:\n{dom[:600]}")

            # Generate caption first
            caption = ""
            if topic:
                tone_desc = tone if tone else "tự nhiên, chân thật"
                cap_prompt = f"""Viết caption ngắn cho Reels Facebook (2-3 câu, max 150 ký tự) về: {topic}
Giọng điệu: {tone_desc}
- Viết tự nhiên như người thật, dùng emoji phù hợp
- Thêm 2-3 hashtag liên quan
- Chỉ trả về caption, không giải thích"""
                try:
                    cap_result = _call_worker([
                        {"role": "system", "content": "Bạn viết caption Reels Facebook ngắn gọn, sáng tạo."},
                        {"role": "user", "content": cap_prompt}
                    ], max_tokens=200, temperature=0.8, timeout=20)
                    if cap_result["ok"]:
                        caption = cap_result["content"].strip().strip('"\'`').strip()
                        caption = re.sub(r'^(Caption|Nội dung)\s*[:：]\s*', '', caption, flags=re.IGNORECASE).strip()
                        print(f"[AGENT] upload_reel caption: {caption[:80]}...")
                except Exception as e:
                    print(f"[AGENT] upload_reel caption error: {e}")
            if not caption:
                caption = f"✨ {topic or 'New reel'} 🎬"

            # Find caption field by analyzing DOM
            find_caption_js = """
            (function() {
                // Priority order for caption fields
                var selectors = [
                    '[contenteditable="true"][role="textbox"]',
                    '[contenteditable="true"][data-lexical-editor]',
                    '[contenteditable="true"][aria-label]',
                    'textarea[aria-label]',
                    '[contenteditable="true"]',
                    'textarea'
                ];
                for (var sel of selectors) {
                    var els = document.querySelectorAll(sel);
                    for (var el of els) {
                        var r = el.getBoundingClientRect();
                        // Must be visible and reasonably sized
                        if (r.width > 40 && r.height > 10 && r.top > 0 && r.top < window.innerHeight * 1.5) {
                            var aria = el.getAttribute('aria-label') || '';
                            var placeholder = el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '';
                            el.scrollIntoView({block: 'center'});
                            // Re-get bounds after scroll
                            r = el.getBoundingClientRect();
                            return JSON.stringify({
                                found: true, sel: sel,
                                aria: aria.substring(0, 60),
                                placeholder: placeholder.substring(0, 60),
                                cx: Math.round(r.left + r.width/2),
                                cy: Math.round(r.top + r.height/2),
                                w: Math.round(r.width), h: Math.round(r.height)
                            });
                        }
                    }
                }
                return JSON.stringify({found: false, 
                    editables: document.querySelectorAll('[contenteditable="true"]').length,
                    textareas: document.querySelectorAll('textarea').length});
            })()
            """

            cap_data = {}
            for cap_try in range(3):
                cap_result2 = self.execute_js(find_caption_js, profile_uuid)
                try:
                    cap_data = json.loads(cap_result2.get("result", "{}"))
                except:
                    cap_data = {}
                print(f"[AGENT] upload_reel [STEP 4] caption field try {cap_try+1}: {json.dumps(cap_data, ensure_ascii=False)[:200]}")
                if cap_data.get("found"):
                    break
                # Maybe need to scroll or wait
                self.execute_js("window.scrollBy(0, 200)", profile_uuid)
                time.sleep(2)

            if cap_data.get("found"):
                time.sleep(0.5)
                # Click to focus the caption field
                self._cdp_mouse_click(profile_uuid, cap_data["cx"], cap_data["cy"])
                time.sleep(0.5)
                # Click again to ensure focus
                self._cdp_mouse_click(profile_uuid, cap_data["cx"], cap_data["cy"])
                time.sleep(0.3)
                # Type caption word by word
                for i, word in enumerate(caption.split(' ')):
                    chunk = word if i == 0 else ' ' + word
                    cdp._send_command('Input.insertText', {'text': chunk})
                    time.sleep(random.uniform(0.05, 0.15))
                print(f"[AGENT] upload_reel [STEP 4] typed caption ({len(caption)} chars)")
                time.sleep(1)
            else:
                print(f"[AGENT] upload_reel [STEP 4] WARNING: no caption field found")

            # ── Step 5: Click through pages (Tiếp/Next → Đăng/Publish) ──
            # Analyze DOM at each click to find the right button
            for click_round in range(6):
                time.sleep(2)
                dom = _dom_snapshot()
                print(f"[AGENT] upload_reel [STEP 5] round {click_round+1} DOM:\n{dom[:400]}")

                # Check: is there a "Đăng"/"Publish" button? (final step)
                publish_btn = _find_button(['đăng reel', 'đăng reels', 'publish reel',
                                            'share reel', 'chia sẻ reel'])
                if publish_btn.get("found"):
                    print(f"[AGENT] upload_reel [STEP 5] found PUBLISH: '{publish_btn['text']}' at ({publish_btn['cx']},{publish_btn['cy']})")
                    self._cdp_mouse_click(profile_uuid, publish_btn["cx"], publish_btn["cy"])
                    time.sleep(5)
                    print(f"[AGENT] upload_reel: PUBLISHED! video='{video_name}', caption='{caption[:40]}...'")
                    return {"success": True,
                            "desc": f"DONE: Đã đăng reel '{video_name}' với caption '{caption[:40]}...'"}

                # Check: "Đăng" alone (generic publish)
                pub_btn = _find_button(['đăng', 'publish', 'share', 'chia sẻ'])
                if pub_btn.get("found"):
                    txt = pub_btn.get("text", "")
                    # Make sure it's not "đăng nhập" (login) or navigation
                    if 'nhập' not in txt and 'xuất' not in txt and len(txt) < 20:
                        print(f"[AGENT] upload_reel [STEP 5] found PUBLISH: '{txt}' at ({pub_btn['cx']},{pub_btn['cy']})")
                        self._cdp_mouse_click(profile_uuid, pub_btn["cx"], pub_btn["cy"])
                        time.sleep(5)
                        print(f"[AGENT] upload_reel: PUBLISHED! video='{video_name}'")
                        return {"success": True,
                                "desc": f"DONE: Đã đăng reel '{video_name}' với caption '{caption[:40]}...'"}

                # Check: "Tiếp"/"Next" button (intermediate step)
                next_btn = _find_button(['tiếp', 'next', 'tiếp theo', 'tiếp tục'])
                if next_btn.get("found"):
                    print(f"[AGENT] upload_reel [STEP 5] clicking NEXT: '{next_btn['text']}' at ({next_btn['cx']},{next_btn['cy']})")
                    self._cdp_mouse_click(profile_uuid, next_btn["cx"], next_btn["cy"])
                    time.sleep(3)
                    # Re-analyze DOM after click
                    continue

                # Nothing found — try scrolling
                print(f"[AGENT] upload_reel [STEP 5] no button found, scrolling...")
                self.execute_js("window.scrollBy(0, 200)", profile_uuid)
                time.sleep(2)

            return {"success": False,
                    "desc": "upload_reel: could not find Publish/Đăng button after navigating"}

        except Exception as e:
            print(f"[AGENT] upload_reel error: {e}")
            traceback.print_exc()
            return {"success": False, "desc": f"upload_reel error: {str(e)[:50]}"}

    # ── Helper: smart_comment (context-aware commenting) ──

    def _smart_comment(self, profile_uuid: str, tone: str, step_num: int) -> dict:
        """Read post content → generate relevant comment → find comment box → type → submit.
        This is the SMART way to comment — reads what the post says first."""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"step": step_num, "ok": False, "desc": "smart_comment: no CDP"}

        try:
            # ── Step 1: Extract the visible post content from the page ──
            post_content = self._extract_post_content(profile_uuid)
            if not post_content:
                return {"step": step_num, "ok": False,
                        "desc": "smart_comment: cannot read post content from page"}

            print(f"[AGENT] smart_comment: post content = '{post_content[:80]}...'")

            # ── Step 2: Generate a relevant comment based on post content ──
            comment_text = self._generate_comment_content(post_content, tone)
            if not comment_text:
                return {"step": step_num, "ok": False,
                        "desc": "smart_comment: comment generation failed"}

            print(f"[AGENT] smart_comment: generated = '{comment_text[:60]}'")

            # ── Step 3: Find and click the Comment button/area ──
            find_comment_js = """
            (function() {
                // Strategy 1: Find "Bình luận" / "Comment" button on the FIRST visible post
                var posts = document.querySelectorAll('[role="article"], [data-testid*="post"], [data-pagelet*="FeedUnit"]');
                if (posts.length === 0) {
                    // Fallback: use entire page
                    posts = [document.body];
                }

                for (var post of posts) {
                    var r = post.getBoundingClientRect();
                    // Only consider posts that are at least partially visible
                    if (r.bottom < 0 || r.top > window.innerHeight * 1.5) continue;

                    // Find "Bình luận" / "Comment" button
                    var btns = post.querySelectorAll('[role="button"], button, a, span[role="button"]');
                    for (var btn of btns) {
                        var txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                        var aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                        if (txt === 'bình luận' || txt === 'comment' || txt === 'viết bình luận' ||
                            aria.includes('bình luận') || aria.includes('comment') ||
                            aria.includes('write a comment') || aria.includes('leave a comment')) {
                            var br = btn.getBoundingClientRect();
                            if (br.width > 10 && br.height > 10 && br.top > 0 && br.top < window.innerHeight) {
                                btn.scrollIntoView({block: 'center', behavior: 'instant'});
                                var br2 = btn.getBoundingClientRect();
                                return JSON.stringify({
                                    found: true, method: 'comment_button',
                                    cx: Math.round(br2.left + br2.width / 2),
                                    cy: Math.round(br2.top + br2.height / 2),
                                    text: (btn.innerText || '').trim().substring(0, 30)
                                });
                            }
                        }
                    }
                }

                // Strategy 2: Find comment input box directly (already open)
                var commentBoxes = document.querySelectorAll(
                    '[contenteditable="true"][role="textbox"], ' +
                    'textarea[placeholder*="bình luận"], textarea[placeholder*="comment"], ' +
                    '[aria-label*="bình luận"], [aria-label*="comment"]'
                );
                for (var cb of commentBoxes) {
                    // Skip if it's inside a dialog/compose (that's for posting, not commenting)
                    if (cb.closest('[role="dialog"]')) continue;
                    var cbr = cb.getBoundingClientRect();
                    if (cbr.width > 20 && cbr.height > 10 && cbr.top > 0) {
                        cb.scrollIntoView({block: 'center', behavior: 'instant'});
                        cb.focus();
                        cb.click();
                        var cbr2 = cb.getBoundingClientRect();
                        return JSON.stringify({
                            found: true, method: 'comment_box_direct',
                            cx: Math.round(cbr2.left + cbr2.width / 2),
                            cy: Math.round(cbr2.top + cbr2.height / 2),
                            focused: true
                        });
                    }
                }

                return JSON.stringify({found: false});
            })()
            """

            # Find comment area
            cmt_result = self.execute_js(find_comment_js, profile_uuid)
            try:
                cmt_data = json.loads(cmt_result.get("result", "{}"))
            except:
                cmt_data = {}

            if not cmt_data.get("found"):
                return {"step": step_num, "ok": False,
                        "desc": "smart_comment: cannot find comment button/box"}

            print(f"[AGENT] smart_comment: found via {cmt_data.get('method')} at ({cmt_data.get('cx')},{cmt_data.get('cy')})")

            # Click comment button (to open comment box if not already open)
            if not cmt_data.get("focused"):
                self._cdp_mouse_click(profile_uuid, cmt_data["cx"], cmt_data["cy"])
                time.sleep(1.5)

            # ── Step 4: Focus the comment textbox ──
            focus_comment_js = """
            (function() {
                // Find comment textbox (NOT inside dialog — that's compose post)
                var candidates = document.querySelectorAll('[contenteditable="true"][role="textbox"]');
                for (var el of candidates) {
                    if (el.closest('[role="dialog"]')) continue;
                    var r = el.getBoundingClientRect();
                    if (r.width > 20 && r.height > 10 && r.top > 0 && r.top < window.innerHeight) {
                        el.focus();
                        el.click();
                        return JSON.stringify({found: true});
                    }
                }
                // Fallback: any contenteditable not in dialog
                candidates = document.querySelectorAll('[contenteditable="true"]');
                for (var el of candidates) {
                    if (el.closest('[role="dialog"]')) continue;
                    var r = el.getBoundingClientRect();
                    if (r.width > 20 && r.height > 10 && r.top > 0 && r.top < window.innerHeight) {
                        el.focus();
                        el.click();
                        return JSON.stringify({found: true});
                    }
                }
                return JSON.stringify({found: false});
            })()
            """

            focus_ok = False
            for attempt in range(3):
                focus_result = self.execute_js(focus_comment_js, profile_uuid)
                try:
                    fd = json.loads(focus_result.get("result", "{}"))
                except:
                    fd = {}
                if fd.get("found"):
                    focus_ok = True
                    break
                time.sleep(1)

            if not focus_ok:
                return {"step": step_num, "ok": False,
                        "desc": "smart_comment: cannot focus comment textbox"}

            time.sleep(0.5)

            # ── Step 5: Type the comment ──
            words = comment_text.split(' ')
            for i, word in enumerate(words):
                chunk = word if i == 0 else ' ' + word
                cdp._send_command('Input.insertText', {'text': chunk})
                time.sleep(random.uniform(0.05, 0.15))

            print(f"[AGENT] smart_comment: typed '{comment_text[:40]}'")
            time.sleep(1)

            # ── Step 6: Submit (press Enter) ──
            cdp._send_command('Input.dispatchKeyEvent', {
                'type': 'keyDown', 'key': 'Enter',
                'code': 'Enter', 'windowsVirtualKeyCode': 13,
                'nativeVirtualKeyCode': 13
            })
            time.sleep(0.1)
            cdp._send_command('Input.dispatchKeyEvent', {
                'type': 'keyUp', 'key': 'Enter',
                'code': 'Enter', 'windowsVirtualKeyCode': 13,
                'nativeVirtualKeyCode': 13
            })
            time.sleep(2)

            return {"step": step_num, "ok": True,
                    "desc": f"smart_comment: '{comment_text[:35]}' on post about '{post_content[:25]}'"}

        except Exception as e:
            print(f"[AGENT] smart_comment error: {e}")
            traceback.print_exc()
            return {"step": step_num, "ok": False,
                    "desc": f"smart_comment error: {str(e)[:50]}"}

    def _extract_post_content(self, profile_uuid: str) -> Optional[str]:
        """Extract the text content of the first visible Facebook post on screen."""
        extract_js = """
        (function() {
            // Strategy 1: Find posts by role="article" (most common in FB)
            var articles = document.querySelectorAll('[role="article"]');
            for (var art of articles) {
                var r = art.getBoundingClientRect();
                // Must be visible on screen
                if (r.top > window.innerHeight || r.bottom < 0) continue;
                if (r.height < 50) continue;

                // Get text content, skip very short
                var text = '';
                // Look for the main text container (not buttons/labels)
                var textContainers = art.querySelectorAll(
                    '[data-ad-preview], [dir="auto"], [data-testid*="post_message"]'
                );
                if (textContainers.length > 0) {
                    for (var tc of textContainers) {
                        var t = (tc.innerText || '').trim();
                        if (t.length > text.length && t.length < 2000) {
                            text = t;
                        }
                    }
                }

                // Fallback: get all text in article, but filter out button/link text
                if (text.length < 20) {
                    var allText = [];
                    var walker = document.createTreeWalker(art, NodeFilter.SHOW_TEXT, null, false);
                    var node;
                    while (node = walker.nextNode()) {
                        var parent = node.parentElement;
                        if (!parent) continue;
                        var tag = parent.tagName.toLowerCase();
                        // Skip buttons, links, labels
                        if (['button', 'a'].includes(tag)) continue;
                        if (parent.getAttribute('role') === 'button') continue;
                        var t = node.textContent.trim();
                        if (t.length > 3 && t.length < 500) {
                            allText.push(t);
                        }
                    }
                    text = allText.join(' ');
                }

                // Also get any shared link title
                var linkTitle = '';
                var links = art.querySelectorAll('a[role="link"] span, a h2, a h3');
                for (var lk of links) {
                    var lt = (lk.innerText || '').trim();
                    if (lt.length > 10) { linkTitle = lt; break; }
                }

                // Get author name
                var author = '';
                var authorLink = art.querySelector('h2 a, h3 a, [data-testid*="story-subtitle"] a, strong a');
                if (authorLink) author = (authorLink.innerText || '').trim();

                if (text.length >= 10 || linkTitle.length >= 10) {
                    return JSON.stringify({
                        found: true,
                        text: text.substring(0, 500),
                        link_title: linkTitle.substring(0, 100),
                        author: author.substring(0, 50)
                    });
                }
            }

            // Strategy 2: Fallback — get visible text from feed area
            var feedArea = document.querySelector('[role="feed"]') || document.body;
            var bodyText = (feedArea.innerText || '').substring(0, 800);
            if (bodyText.length > 50) {
                return JSON.stringify({
                    found: true, text: bodyText.substring(0, 500),
                    link_title: '', author: '', method: 'fallback_feed'
                });
            }

            return JSON.stringify({found: false});
        })()
        """
        try:
            result = self.execute_js(extract_js, profile_uuid)
            raw = result.get("result", "{}")
            data = json.loads(raw) if isinstance(raw, str) else raw
            if not data.get("found"):
                return None

            parts = []
            author = data.get("author", "")
            text = data.get("text", "")
            link_title = data.get("link_title", "")

            if author:
                parts.append(f"Tác giả: {author}")
            if text:
                parts.append(f"Nội dung: {text}")
            if link_title:
                parts.append(f"Link chia sẻ: {link_title}")

            content = ". ".join(parts)
            return content if len(content) > 10 else None

        except Exception as e:
            print(f"[AGENT] extract_post_content error: {e}")
            return None

    def _generate_comment_content(self, post_content: str, tone: str = "") -> Optional[str]:
        """Generate a RELEVANT comment based on the actual post content."""
        tone_desc = tone if tone else "tự nhiên, chân thật, như bạn bè"

        prompt = f"""Đọc bài đăng Facebook sau và viết 1 BÌNH LUẬN phù hợp:

BÀI ĐĂNG:
{post_content[:400]}

Giọng điệu: {tone_desc}

YÊU CẦU TUYỆT ĐỐI:
- Bình luận PHẢI LIÊN QUAN đến nội dung bài viết
- Đọc kỹ nội dung → bình luận ĐÚNG chủ đề
- Viết như người thật, 1-2 câu ngắn gọn
- Có thể khen, hỏi, đồng cảm, chia sẻ ý kiến — TÙY NỘI DUNG BÀI  
- Dùng 0-2 emoji tự nhiên
- KHÔNG viết chung chung kiểu "hay quá", "thanks đã chia sẻ" — PHẢI cụ thể theo nội dung
- KHÔNG hashtag, KHÔNG quá dài
- CHỈ trả về nội dung bình luận, không giải thích

VÍ DỤ:
- Bài về đi du lịch Đà Lạt → "Đà Lạt giờ đẹp lắm luôn, hồi trc mình đi cũng mê 🌿"
- Bài về được tăng lương → "Chúc mừng nha, xứng đáng rồi! 🎉"
- Bài về nấu ăn → "Nhìn ngon quá trời, cho xin công thức đi 😋"
- Bài chia sẻ tin tức → "Đọc xong thấy lo thật, hy vọng mọi chuyện ổn thôi"
"""

        try:
            result = _call_worker([
                {"role": "system", "content": "Bạn là người Việt bình thường đang comment Facebook. Chỉ trả về NỘI DUNG bình luận, không giải thích. Bình luận phải CỤ THỂ theo nội dung bài."},
                {"role": "user", "content": prompt}
            ], max_tokens=150, temperature=0.85, timeout=20)

            if result["ok"]:
                comment = result["content"].strip()
                # Clean up
                comment = comment.strip('"\'')
                comment = re.sub(r'^```.*?\n', '', comment)
                comment = comment.strip('`').strip()
                # Remove "Bình luận:" prefix
                comment = re.sub(r'^(Bình luận|Comment|Nội dung)\s*[:：]\s*', '', comment, flags=re.IGNORECASE)
                comment = comment.strip()
                if len(comment) > 5 and len(comment) < 300:
                    print(f"[AGENT] Generated comment: '{comment[:60]}'")
                    return comment
                print(f"[AGENT] Comment too short/long: '{comment}'")
                return None
            print(f"[AGENT] Comment gen: AI call failed")
            return None
        except Exception as e:
            print(f"[AGENT] Comment gen error: {e}")
            return None

    # ── Helper: CDP mouse click ──

    def _cdp_mouse_click(self, profile_uuid: str, x: int, y: int) -> bool:
        """Click at (x, y) using CDP Input.dispatchMouseEvent."""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return False
        try:
            cdp._send_command('Input.dispatchMouseEvent', {
                'type': 'mouseMoved', 'x': x, 'y': y
            })
            time.sleep(0.1)
            cdp._send_command('Input.dispatchMouseEvent', {
                'type': 'mousePressed', 'x': x, 'y': y,
                'button': 'left', 'clickCount': 1
            })
            time.sleep(0.05)
            cdp._send_command('Input.dispatchMouseEvent', {
                'type': 'mouseReleased', 'x': x, 'y': y,
                'button': 'left', 'clickCount': 1
            })
            print(f"[AGENT] CDP mouse click ({x}, {y}) OK")
            return True
        except Exception as e:
            print(f"[AGENT] CDP mouse click error: {e}")
            return False

    # ── Helper: JS click by DOM index ──

    def _js_click_by_index(self, profile_uuid: str, index: int) -> bool:
        """Click element by its index in the DOM collection.
        MUST use same selectors/filters/dedup as _get_indexed_dom!"""
        js = """
        (function() {
            var sels = 'a[href], button, [role="button"], [role="link"], [role="tab"], ' +
                       '[role="menuitem"], input, textarea, [role="textbox"], ' +
                       '[aria-label], [data-testid], [role="switch"], [role="checkbox"], ' +
                       '[role="dialog"], [role="menu"], [contenteditable="true"], ' +
                       '[role="option"], [role="listbox"], [role="combobox"], ' +
                       'select, [role="img"][aria-label], [role="article"], ' +
                       '[data-pagelet], form';
            var els = document.querySelectorAll(sels);
            var idx = 0;
            var seen = new Set();
            for (var i = 0; i < els.length && idx < 180; i++) {
                var el = els[i];
                var rect = el.getBoundingClientRect();
                if (rect.width < 5 || rect.height < 5) continue;
                if (rect.bottom < -100 || rect.top > window.innerHeight * 2.5) continue;
                var cx = Math.round(rect.left + rect.width / 2);
                var cy = Math.round(rect.top + rect.height / 2);
                var posKey = Math.round(cx/3) + '_' + Math.round(cy/3);
                if (seen.has(posKey)) continue;
                seen.add(posKey);
                if (idx === """ + str(index) + """) {
                    el.scrollIntoView({block: 'center'});
                    el.click();
                    return 'clicked';
                }
                idx++;
            }
            return 'not_found';
        })()
        """
        try:
            result = self.execute_js(js, profile_uuid)
            val = result.get("result", "")
            ok = val == "clicked"
            print(f"[AGENT] JS click_index({index}): {val}")
            return ok
        except Exception as e:
            print(f"[AGENT] JS click error: {e}")
            return False

    # ── Helper: send screenshot to Telegram ──

    def _send_agent_screenshot(self, profile_uuid: str,
                                telegram_chat_id: str, caption: str):
        """Take screenshot and send to Telegram."""
        if not telegram_chat_id:
            return
        try:
            sc = self.screenshot_profile(profile_uuid)
            if sc.get("success"):
                self.send_telegram_photo(sc["screenshot"], telegram_chat_id, caption)
            else:
                print(f"[AGENT] screenshot_profile failed: {sc.get('error')}")
        except Exception as e:
            print(f"[AGENT] Screenshot send error: {e}")
