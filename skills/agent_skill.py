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

# AI config
_API_URL = "https://gen.pollinations.ai/v1/chat/completions"
_API_KEY = "sk_3HRi9HUGLup7OKB6ykRds8YtnpcmLHD3"
_MODEL = "gemini-fast"

AGENT_SYSTEM = """Bạn là AI agent điều khiển trình duyệt Facebook.
Bạn nhận: ảnh screenshot, DOM summary (danh sách element có index), nhiệm vụ, và KẾ HOẠCH đã lập.

QUAN TRỌNG - CÁCH TIẾP CẬN:
1. LUÔN bắt đầu bằng navigate đến URL phù hợp nếu trang hiện tại không liên quan đến nhiệm vụ.
   - Trang chủ: https://www.facebook.com/
   - Thông báo: https://www.facebook.com/notifications
   - Bạn bè / Lời mời: https://www.facebook.com/friends/requests
   - Messenger: https://www.facebook.com/messages
   - Trang cá nhân mình: https://www.facebook.com/me
   - Marketplace: https://www.facebook.com/marketplace
   - Watch: https://www.facebook.com/watch
   - Nhóm: https://www.facebook.com/groups
2. Sau khi navigate → wait 2-3s để load.
3. Phân tích screenshot + DOM để tìm element cần tương tác.
4. Chỉ click vào element thực sự có trong DOM list (có index).
5. KHÔNG lặp lại cùng action nếu lần trước thất bại → thử cách khác.

CÁC ACTION:
- {"action": "navigate", "url": "https://...", "reason": "Mở trang..."}
- {"action": "click_index", "index": 5, "reason": "Click vào..."}
- {"action": "scroll", "direction": "down", "amount": 400, "reason": "Cuộn xuống..."}
- {"action": "type", "index": 3, "text": "nội dung", "reason": "Gõ vào..."}
- {"action": "compose_post", "topic": "chủ đề/yêu cầu bài viết", "tone": "giọng điệu (genz/formal/funny...)", "reason": "Đăng bài"} ← DÙNG ĐỂ ĐĂNG BÀI (HỆ THỐNG TỰ VIẾT NỘI DUNG + CLICK Ô + GÕ + ĐĂNG)
- {"action": "wait", "seconds": 3, "reason": "Đợi load"}
- {"action": "click_vision", "target": "mô tả element", "reason": "Click ..."}  (chậm, dùng cuối cùng)
- {"action": "done", "summary": "Tóm tắt kết quả", "reason": "Hoàn thành"}
- {"action": "failed", "reason": "Lý do thất bại"}

QUY TẮC:
- CHỈ trả về 1 JSON object, không text khác.
- Ưu tiên: navigate > compose_post (nếu đăng bài) > click_index > scroll > type > click_vision.
- KHÔNG click_selector (dễ fail). Dùng click_index với index từ DOM list.
- Nếu element không thấy trong DOM → scroll DOWN trước rồi thử lại.
- Nếu trang hiện tại không liên quan → navigate đến URL đúng.
- **ĐĂNG BÀI**: Khi cần đăng bài → navigate tới /me hoặc / → wait 3s → compose_post (NÓ TỰ TÌM + CUỘN + CLICK Ô VIẾT BÀI + GÕ + ĐĂNG). KHÔNG click_index vào "Bạn đang nghĩ gì?" — compose_post tự xử lý hết. KHÔNG dùng click_index + type riêng lẻ cho việc đăng bài.
- Nếu compose_post thất bại → KHÔNG thử lại compose_post liên tục. Thử navigate lại /me rồi dùng compose_post 1 lần nữa. Nếu vẫn fail → failed.
- Nếu cùng action lặp lại 3 lần → BẠN ĐANG BỊ KẸT. Hãy thử navigate hoặc cách tiếp cận khác.
- Khi xong → "done" với summary rõ ràng. Không thể tiếp → "failed".

VÍ DỤ FLOW - Đăng bài lên trang cá nhân:
  1. navigate → https://www.facebook.com/me
  2. wait 3s (chờ load xong profile)
  3. compose_post với topic="hết tết sắp đi làm" tone="genz"
  4. done (compose_post đã tự đăng xong rồi)

VÍ DỤ FLOW - Xem thông báo:
  1. navigate → https://www.facebook.com/notifications
  2. wait 2s  
  3. click_index vào 1 thông báo bất kỳ
  4. wait 2s
  5. done
"""

MAX_STEPS = 20


class AgentSkill:
    """Autonomous AI agent for executing arbitrary browser tasks."""

    # ── Main entry point ──

    def agent_execute(self, task: str, profile_uuid: str = None,
                      telegram_chat_id: str = None,
                      telegram_message_id: int = None) -> Dict[str, Any]:
        """Execute an arbitrary task using AI agent loop."""
        browser_opened_here = False
        steps_log: List[Dict] = []

        try:
            profile_uuid = profile_uuid or self.current_profile
            if not profile_uuid:
                return {"success": False, "error": "No profile specified"}

            short_id = profile_uuid[:20]
            print(f"[AGENT] === START === {short_id} -> {task}")

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

            # ── Loop detection state ──
            last_actions = []   # track recent action strings
            last_dom_hash = ""  # track DOM changes
            stuck_warning = ""  # injected into AI prompt when stuck

            # ── Agent loop ──
            for step_num in range(1, MAX_STEPS + 1):
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

                # 4. Ask AI (with plan and stuck warning)
                ai_action = self._ask_agent_ai(task, img_b64, dom_summary,
                                                steps_log, plan=plan,
                                                extra_warning=stuck_warning)
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

                # 5. Execute
                result = self._execute_agent_action(
                    ai_action, profile_uuid, dom_elements, step_num)
                steps_log.append(result)

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

            if telegram_chat_id and telegram_message_id:
                text = f"🤖 *Agent - {short_id}*\n📋 Task: {task[:60]}\n\n"
                if last.get("desc", "").startswith("DONE"):
                    text += f"✅ *Hoàn thành!*\n📝 {last.get('desc', '')[6:]}\n\n"
                elif last.get("desc", "").startswith("FAILED"):
                    text += f"❌ *Thất bại:* {last.get('desc', '')[8:]}\n\n"
                else:
                    text += f"⚠️ {len(steps_log)} bước đã thực hiện\n\n"
                text += f"📊 {ok_count}✅ / {len(steps_log)} steps"
                try:
                    self._telegram_edit_message(telegram_chat_id, telegram_message_id, text)
                except Exception:
                    pass

            return {
                "success": any(s.get("desc", "").startswith("DONE") for s in steps_log),
                "profile_uuid": profile_uuid, "task": task,
                "steps": steps_log, "total_steps": len(steps_log),
            }

        except Exception as e:
            traceback.print_exc()
            print(f"[AGENT] === EXCEPTION === {e}")
            self._send_agent_screenshot(profile_uuid, telegram_chat_id,
                                         f"❌ Agent lỗi: {str(e)[:100]}")
            return {"success": False, "error": str(e), "steps": steps_log}

    # ── Screenshot via CDP (fast) ──

    def _agent_screenshot_b64(self, profile_uuid: str) -> Optional[str]:
        """Capture screenshot as base64 using CDP directly. Fast and reliable."""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            print(f"[AGENT] _agent_screenshot_b64: no CDP")
            return None
        try:
            result = cdp._send_command("Page.captureScreenshot", {"format": "png"})
            img_data = result.get("result", {}).get("data")
            if img_data:
                print(f"[AGENT] Screenshot OK ({len(img_data)//1000}KB b64)")
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
                       '[aria-label], [data-testid], [role="switch"], [role="checkbox"]';
            var els = document.querySelectorAll(sels);
            var idx = 0;

            for (var i = 0; i < els.length && idx < 80; i++) {
                var el = els[i];
                var rect = el.getBoundingClientRect();
                if (rect.width < 5 || rect.height < 5) continue;
                if (rect.bottom < 0 || rect.top > window.innerHeight * 2) continue;

                var tag = el.tagName.toLowerCase();
                var text = (el.innerText || el.textContent || '').trim().substring(0, 60);
                var ariaLabel = el.getAttribute('aria-label') || '';
                var role = el.getAttribute('role') || '';
                var href = el.getAttribute('href') || '';
                var placeholder = el.getAttribute('placeholder') || '';
                var testid = el.getAttribute('data-testid') || '';
                var visible = rect.top >= 0 && rect.top < window.innerHeight;
                var cx = Math.round(rect.left + rect.width / 2);
                var cy = Math.round(rect.top + rect.height / 2);

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

                // Build description
                var desc = tag;
                if (role) desc += '[role="' + role + '"]';
                if (ariaLabel) desc += '[aria-label="' + ariaLabel.substring(0, 40) + '"]';
                if (testid) desc += '[data-testid="' + testid.substring(0, 30) + '"]';
                if (placeholder) desc += '[placeholder="' + placeholder.substring(0, 30) + '"]';
                if (text && text.length > 1 && text.length < 50) desc += ' "' + text + '"';
                if (href && !href.startsWith('javascript')) desc += ' -> ' + href.substring(0, 50);

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

            var bodyText = document.body ? document.body.innerText.substring(0, 500) : '';

            return JSON.stringify({
                url: url,
                title: title,
                elements: elements,
                page_text: bodyText.replace(/\\n+/g, ' ').substring(0, 300),
                viewport: {w: window.innerWidth, h: window.innerHeight}
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

            elements = data.get("elements", [])
            visible = [e for e in elements if e.get("visible")]
            offscreen = [e for e in elements if not e.get("visible")]

            lines.append(f"\n=== Visible ({len(visible)}) ===")
            for e in visible:
                lines.append(f"  [{e['idx']}] {e['desc']}")

            if offscreen:
                lines.append(f"\n=== Below viewport ({len(offscreen)}) ===")
                for e in offscreen[:15]:
                    lines.append(f"  [{e['idx']}] {e['desc']}")

            lines.append(f"\nPage text: {data.get('page_text', '')[:200]}")

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

    def _agent_make_plan(self, task: str, profile_uuid: str) -> str:
        """Ask AI to create a high-level plan before executing steps."""
        # Get current page info
        dom_summary, _ = self._get_indexed_dom(profile_uuid)
        current_url = ""
        for line in dom_summary.split("\n"):
            if line.startswith("URL:"):
                current_url = line[5:].strip()
                break

        plan_prompt = f"""Bạn là AI agent điều khiển trình duyệt Facebook.
Nhiệm vụ: {task}
Trang hiện tại: {current_url}

Hãy lập KẾ HOẠCH ngắn gọn (3-7 bước) để hoàn thành nhiệm vụ.
Bắt đầu bằng navigate đến URL phù hợp nếu trang hiện tại không đúng.

URL Facebook chính:
- Trang chủ: https://www.facebook.com/
- Trang cá nhân: https://www.facebook.com/me
- Thông báo: https://www.facebook.com/notifications
- Bạn bè: https://www.facebook.com/friends/requests
- Messenger: https://www.facebook.com/messages
- Watch: https://www.facebook.com/watch
- Groups: https://www.facebook.com/groups
- Marketplace: https://www.facebook.com/marketplace

Trả lời chỉ danh sách bước, ngắn gọn. Ví dụ:
1. Navigate đến /me
2. Click "Bạn đang nghĩ gì?"
3. Gõ nội dung bài viết
4. Click "Đăng"
"""
        try:
            resp = _requests.post(_API_URL, json={
                "model": _MODEL,
                "messages": [
                    {"role": "system", "content": "Bạn là AI planner. Trả lời ngắn gọn danh sách bước."},
                    {"role": "user", "content": plan_prompt}
                ],
                "max_tokens": 250,
                "temperature": 0.3
            }, headers={
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type": "application/json"
            }, timeout=20)
            if resp.status_code == 200:
                plan = resp.json()["choices"][0]["message"]["content"].strip()
                return plan
            return ""
        except Exception as e:
            print(f"[AGENT] Plan error: {e}")
            return ""

    # ── Ask AI for next action ──

    def _ask_agent_ai(self, task: str, image_b64: str,
                       dom_summary: str, steps_log: List[Dict],
                       plan: str = "", extra_warning: str = "") -> Optional[Dict]:
        """Send screenshot + DOM to AI, get next action JSON."""
        history = ""
        if steps_log:
            history = "\n\nBuoc da lam:\n"
            for s in steps_log[-8:]:
                icon = "OK" if s.get("ok") else "FAIL"
                history += f"  Step {s.get('step', '?')}: {icon} {s.get('desc', '?')[:60]}\n"

        plan_section = ""
        if plan:
            plan_section = f"\n\nKE HOACH:\n{plan}\n"

        user_msg = (
            f"NHIEM VU: {task}\n"
            f"{plan_section}\n"
            f"DOM HIEN TAI:\n{dom_summary}\n"
            f"{history}"
            f"{extra_warning}\n\n"
            f"Tra ve JSON cho BUOC TIEP THEO."
        )

        messages = [
            {"role": "system", "content": AGENT_SYSTEM},
        ]

        # Try with image
        if image_b64:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    {"type": "text", "text": user_msg}
                ]
            })
        else:
            messages.append({"role": "user", "content": user_msg})

        try:
            resp = _requests.post(_API_URL, json={
                "model": _MODEL,
                "messages": messages,
                "max_tokens": 300,
                "temperature": 0.2
            }, headers={
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type": "application/json"
            }, timeout=45)

            if resp.status_code != 200:
                print(f"[AGENT-AI] HTTP {resp.status_code}: {resp.text[:300]}")
                # Fallback: try without image
                if image_b64:
                    print(f"[AGENT-AI] Retrying without image...")
                    return self._ask_agent_ai_text_only(task, dom_summary, steps_log)
                return None

            content = resp.json()["choices"][0]["message"]["content"].strip()
            print(f"[AGENT-AI] Response: {content[:250]}")

            # Parse JSON
            clean = re.sub(r'```(?:json)?', '', content).strip()
            m = re.search(r'\{.*\}', clean, re.DOTALL)
            if m:
                parsed = json.loads(m.group())
                if "action" in parsed:
                    return parsed
                print(f"[AGENT-AI] No 'action' key in: {parsed}")

            print(f"[AGENT-AI] No valid JSON in response")
            # Fallback text-only
            if image_b64:
                return self._ask_agent_ai_text_only(task, dom_summary, steps_log)
            return None

        except Exception as e:
            print(f"[AGENT-AI] Error: {e}")
            if image_b64:
                return self._ask_agent_ai_text_only(task, dom_summary, steps_log)
            return None

    def _ask_agent_ai_text_only(self, task: str, dom_summary: str,
                                 steps_log: List[Dict]) -> Optional[Dict]:
        """Fallback: ask AI without screenshot (text-only)."""
        history = ""
        if steps_log:
            history = "\nBuoc da lam:\n"
            for s in steps_log[-6:]:
                icon = "OK" if s.get("ok") else "FAIL"
                history += f"  {icon} {s.get('desc', '?')[:60]}\n"

        user_msg = (
            f"NHIEM VU: {task}\n\nDOM:\n{dom_summary}\n{history}\n"
            f"(Khong co anh screenshot)\nTra ve JSON buoc tiep theo."
        )

        try:
            resp = _requests.post(_API_URL, json={
                "model": _MODEL,
                "messages": [
                    {"role": "system", "content": AGENT_SYSTEM},
                    {"role": "user", "content": user_msg}
                ],
                "max_tokens": 250,
                "temperature": 0.2
            }, headers={
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type": "application/json"
            }, timeout=30)

            if resp.status_code != 200:
                print(f"[AGENT-AI-text] HTTP {resp.status_code}")
                return None

            content = resp.json()["choices"][0]["message"]["content"].strip()
            print(f"[AGENT-AI-text] {content[:200]}")
            clean = re.sub(r'```(?:json)?', '', content).strip()
            m = re.search(r'\{.*\}', clean, re.DOTALL)
            if m:
                parsed = json.loads(m.group())
                if "action" in parsed:
                    return parsed
            return None
        except Exception as e:
            print(f"[AGENT-AI-text] Error: {e}")
            return None

    # ── Execute a single agent action ──

    def _execute_agent_action(self, ai_action: Dict, profile_uuid: str,
                               dom_elements: List[Dict],
                               step_num: int) -> Dict:
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
        """Scroll element into view and return fresh (cx, cy) center coordinates."""
        js = """
        (function() {
            var sels = 'a[href], button, [role="button"], [role="link"], [role="tab"], ' +
                       '[role="menuitem"], input, textarea, [role="textbox"], ' +
                       '[aria-label], [data-testid], [role="switch"], [role="checkbox"]';
            var els = document.querySelectorAll(sels);
            var idx = 0;
            for (var i = 0; i < els.length; i++) {
                var el = els[i];
                var rect = el.getBoundingClientRect();
                if (rect.width < 5 || rect.height < 5) continue;
                if (rect.bottom < 0 || rect.top > window.innerHeight * 2) continue;
                if (idx === """ + str(index) + """) {
                    el.scrollIntoView({block: 'center', behavior: 'instant'});
                    // Re-get rect after scroll
                    var r2 = el.getBoundingClientRect();
                    var cx = Math.round(r2.left + r2.width / 2);
                    var cy = Math.round(r2.top + r2.height / 2);
                    return JSON.stringify({ok: true, cx: cx, cy: cy});
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
            resp = _requests.post(_API_URL, json={
                "model": _MODEL,
                "messages": [
                    {"role": "system", "content": "Bạn là người Việt trẻ, viết Facebook post tự nhiên và sáng tạo. Chỉ trả về nội dung bài đăng, không thêm gì khác."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 300,
                "temperature": 0.8
            }, headers={
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type": "application/json"
            }, timeout=20)

            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"].strip()
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
            print(f"[AGENT] Content gen HTTP {resp.status_code}")
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
        """Click element by its index in the DOM collection."""
        js = """
        (function() {
            var sels = 'a[href], button, [role="button"], [role="link"], [role="tab"], ' +
                       '[role="menuitem"], input, textarea, [role="textbox"], ' +
                       '[aria-label], [data-testid], [role="switch"], [role="checkbox"]';
            var els = document.querySelectorAll(sels);
            var idx = 0;
            for (var i = 0; i < els.length; i++) {
                var el = els[i];
                var rect = el.getBoundingClientRect();
                if (rect.width < 5 || rect.height < 5) continue;
                if (rect.bottom < 0 || rect.top > window.innerHeight * 2) continue;
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
