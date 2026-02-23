"""
BaseSkill - Core singleton + CDP utilities + basic browser control methods.
All other skills inherit from this.
"""

import json
import threading
import base64
import os
import time
import random
import traceback
import requests as _requests
from typing import Optional, Dict, Any, List
from api_service import api as hidemium
from automation.cdp_client import CDPClient

# Pollinations AI for generating comments
_POLLINATIONS_API = "https://gen.pollinations.ai/v1/chat/completions"
_POLLINATIONS_KEY = "sk_3HRi9HUGLup7OKB6ykRds8YtnpcmLHD3"
_POLLINATIONS_MODEL = "gemini-fast"

# Fallback generic comments (used when AI fails)
_FALLBACK_COMMENTS = [
    "Hay quá 😂", "Đỉnh quá bạn ơi", "Quá hay luôn 👍",
    "Like mạnh 🔥", "Xuất sắc", "Quá cuốn", "Content hay ghê",
    "Hay lắm bạn ơi 👏", "Chất lượng quá", "Tuyệt vời 🫶"
]

_COMMENT_PROMPT = (
    "Bạn là người Việt đang lướt Facebook. Dựa vào nội dung bài viết/video bên dưới, "
    "hãy viết MỘT comment ngắn gọn (tối đa 15 từ), tích cực, tự nhiên như người thật. "
    "KHÔNG dùng hashtag. KHÔNG giải thích gì thêm. CHỈ trả về đúng 1 câu comment.\n\n"
    "Nội dung: {content}"
)

# Thư mục lưu screenshot
SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'screenshots')
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


class BaseSkill:
    """Core singleton class with CDP connection management and basic browser control."""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.connections = {}
            cls._instance.current_profile = None
            cls._instance._profile_locks = {}
            cls._instance._lock_lock = threading.Lock()
        return cls._instance

    def generate_smart_comment(self, content: str) -> str:
        """Generate a relevant Vietnamese comment based on post/reel content using AI.
        Falls back to random generic comment if AI fails."""
        if not content or len(content.strip()) < 5:
            return random.choice(_FALLBACK_COMMENTS)

        # Truncate content to save tokens
        content_short = content[:300].strip()
        prompt = _COMMENT_PROMPT.format(content=content_short)

        try:
            resp = _requests.post(
                _POLLINATIONS_API,
                json={
                    "model": _POLLINATIONS_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 60,
                    "temperature": 0.8
                },
                headers={
                    "Authorization": f"Bearer {_POLLINATIONS_KEY}",
                    "Content-Type": "application/json"
                },
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                comment = data["choices"][0]["message"]["content"].strip()
                # Clean up: remove quotes, leading/trailing junk
                comment = comment.strip('"\' \n\t')
                # Ensure reasonable length
                if 2 < len(comment) < 150:
                    print(f"[AI-CMT] Generated: {comment}")
                    return comment
                else:
                    print(f"[AI-CMT] Bad length ({len(comment)}), using fallback")
            else:
                print(f"[AI-CMT] API {resp.status_code}, using fallback")
        except Exception as e:
            print(f"[AI-CMT] Error: {e}, using fallback")

        return random.choice(_FALLBACK_COMMENTS)

    def _get_profile_lock(self, profile_uuid: Optional[str] = None) -> threading.Lock:
        """Get or create a per-profile lock for serializing operations."""
        key = profile_uuid or '__global__'
        with self._lock_lock:
            if key not in self._profile_locks:
                self._profile_locks[key] = threading.Lock()
            return self._profile_locks[key]

    def resolve_profile(self, name_or_uuid: str) -> str:
        """
        Nếu input là UUID (có dấu -) → trả về nguyên.
        Nếu input là tên (vd 's10') → search Hidemium API → trả UUID.
        """
        import requests as req

        if not name_or_uuid:
            return name_or_uuid
        # Đã là UUID rồi
        if '-' in name_or_uuid and len(name_or_uuid) > 20:
            return name_or_uuid
        # Search theo tên
        try:
            resp = req.post(
                "http://127.0.0.1:2222/v1/browser/list?is_local=true",
                headers={
                    "Authorization": "Bearer MkkmO8VZiI22dcJIS4qDiYwyNH6JIGC8G9L",
                    "Content-Type": "application/json"
                },
                json={"page": 1, "limit": 100, "search": name_or_uuid}
            )
            data = resp.json()
            items = data.get('data', data.get('items', []))
            # Handle nested structure: data.data.content
            if isinstance(items, dict):
                items = items.get('content', items.get('items', items.get('data', [])))
            if isinstance(items, list):
                for item in items:
                    item_name = item.get('name', '')
                    item_uuid = item.get('uuid', '')
                    if item_name.lower() == name_or_uuid.lower():
                        print(f"[API] Resolved '{name_or_uuid}' → {item_uuid}")
                        return item_uuid
                # Nếu không match chính xác, lấy cái đầu tiên
                if items:
                    first_uuid = items[0].get('uuid', name_or_uuid)
                    print(f"[API] Resolved '{name_or_uuid}' → {first_uuid} (first match)")
                    return first_uuid
        except Exception as e:
            print(f"[API] resolve_profile error: {e}")
        return name_or_uuid

    def _get_cdp(self, profile_uuid: Optional[str] = None) -> Optional[CDPClient]:
        """Get active CDP connection for profile. Returns None if no working connection."""
        uuid = profile_uuid or self.current_profile
        if not uuid:
            return None
        cdp = self.connections.get(uuid)
        if not cdp:
            return None
        # Check if websocket is still alive
        if not cdp.ws:
            try:
                del self.connections[uuid]
            except:
                pass
            return None
        # Verify ws is actually connected by trying a simple command
        try:
            cdp.ws.ping()
        except:
            # Websocket is dead, clean up
            try:
                cdp.disconnect()
            except:
                pass
            try:
                del self.connections[uuid]
            except:
                pass
            return None
        return cdp

    @staticmethod
    def _capture_screenshot(send_cmd_func, profile_uuid: str, label: str = '') -> Optional[str]:
        """Capture screenshot via CDP send_cmd function. Returns file path or None."""
        from datetime import datetime
        try:
            ss_result = send_cmd_func("Page.captureScreenshot", {"format": "png"})
            img_data = ss_result.get('result', {}).get('data')
            if not img_data:
                return None
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            safe_uuid = profile_uuid[:20].replace('-', '')
            filename = f"{safe_uuid}_{label}_{ts}.png"
            filepath = os.path.join(SCREENSHOT_DIR, filename)
            with open(filepath, 'wb') as f:
                f.write(base64.b64decode(img_data))
            return filepath
        except Exception as e:
            print(f"[API] Screenshot error: {e}")
            return None

    # ============================================================
    # BASIC BROWSER CONTROL (navigate, scroll, DOM, JS, click, type)
    # ============================================================

    def navigate(self, url: str, profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Điều hướng đến URL"""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "error": "No active connection. Call /open_browser first."}
        try:
            result = cdp.navigate(url, wait_load=True)
            return {
                "success": result.success,
                "error": result.error if not result.success else None,
                "url": url
            }
        except Exception as e:
            return {"success": False, "error": f"Navigate failed: {e}"}

    def scroll(self, amount: int = 500, profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Cuộn trang"""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "error": "No active connection. Call /open_browser first."}
        try:
            result = cdp._evaluate_js(f"window.scrollBy(0, {amount}); window.scrollY")
            return {"success": True, "scrolled": amount, "current_scroll": result}
        except Exception as e:
            return {"success": False, "error": f"Scroll failed: {e}"}

    def get_dom(self, selector: str = "body", profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Lấy DOM elements"""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "error": "No active connection. Call /open_browser first."}

        js_code = f'''
        (function() {{
            const elements = document.querySelectorAll('{selector}');
            const results = [];
            for (let i = 0; i < Math.min(elements.length, 10); i++) {{
                const el = elements[i];
                const rect = el.getBoundingClientRect();
                results.push({{
                    index: i,
                    tagName: el.tagName,
                    className: el.className,
                    id: el.id,
                    innerText: (el.innerText || '').substring(0, 100),
                    top: Math.round(rect.top),
                    height: Math.round(rect.height),
                    attributes: Array.from(el.attributes).slice(0, 10).map(a => ({{name: a.name, value: a.value.substring(0, 100)}}))
                }});
            }}
            return JSON.stringify({{
                count: elements.length,
                elements: results
            }});
        }})()
        '''
        try:
            result = cdp._evaluate_js(js_code)
            data = json.loads(result)
            return {"success": True, **data}
        except json.JSONDecodeError:
            return {"success": False, "error": "Failed to parse DOM", "raw": str(result)[:500]}
        except Exception as e:
            return {"success": False, "error": f"DOM query failed: {e}"}

    def execute_js(self, code: str, profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Chạy JavaScript tùy ý"""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "error": "No active connection. Call /open_browser first."}
        try:
            result = cdp._evaluate_js(code)
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": f"JS execution failed: {e}"}

    def click_element(self, selector: str, profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Click element by CSS selector (human-like with scroll + hover)"""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "error": "No active connection. Call /open_browser first."}
        try:
            result = cdp.click(selector)
            return {
                "success": result.success,
                "error": result.error if not result.success else None,
                "selector": selector
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def type_text_into(self, selector: str, text: str, human_like: bool = True,
                       profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Type text into element by CSS selector (human-like by default)"""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "error": "No active connection. Call /open_browser first."}
        try:
            result = cdp.type_text(selector, text, human_like=human_like)
            return {
                "success": result.success,
                "error": result.error if not result.success else None,
                "typed_length": len(text)
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def press_key(self, key: str, profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Press keyboard key (Enter, Tab, Escape, Backspace, ArrowDown, ArrowUp, Space)"""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "error": "No active connection. Call /open_browser first."}

        key_map = {
            'Enter': {'key': 'Enter', 'code': 'Enter', 'keyCode': 13},
            'Tab': {'key': 'Tab', 'code': 'Tab', 'keyCode': 9},
            'Escape': {'key': 'Escape', 'code': 'Escape', 'keyCode': 27},
            'Backspace': {'key': 'Backspace', 'code': 'Backspace', 'keyCode': 8},
            'ArrowDown': {'key': 'ArrowDown', 'code': 'ArrowDown', 'keyCode': 40},
            'ArrowUp': {'key': 'ArrowUp', 'code': 'ArrowUp', 'keyCode': 38},
            'Space': {'key': ' ', 'code': 'Space', 'keyCode': 32},
        }
        key_info = key_map.get(key, {'key': key, 'code': f'Key{key.upper()}', 'keyCode': ord(key[0]) if key else 0})

        try:
            cdp._send_command('Input.dispatchKeyEvent', {
                'type': 'keyDown',
                'key': key_info['key'],
                'code': key_info['code'],
                'windowsVirtualKeyCode': key_info['keyCode'],
                'nativeVirtualKeyCode': key_info['keyCode']
            })
            time.sleep(0.05)
            cdp._send_command('Input.dispatchKeyEvent', {
                'type': 'keyUp',
                'key': key_info['key'],
                'code': key_info['code'],
                'windowsVirtualKeyCode': key_info['keyCode'],
                'nativeVirtualKeyCode': key_info['keyCode']
            })
            return {"success": True, "key": key}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_page_text(self, profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Get visible text content of the current page (viewport area)"""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "error": "No active connection. Call /open_browser first."}

        js = '''
        (function() {
            const url = window.location.href;
            const title = document.title;
            const scrollY = window.scrollY;
            const viewHeight = window.innerHeight;
            const body = document.body;

            const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
            const texts = [];
            let totalLen = 0;

            while (walker.nextNode() && totalLen < 2000) {
                const node = walker.currentNode;
                const text = node.textContent.trim();
                if (!text || text.length < 2) continue;

                const el = node.parentElement;
                if (!el) continue;

                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;

                const rect = el.getBoundingClientRect();
                if (rect.bottom < -100 || rect.top > viewHeight + 500) continue;

                texts.push(text);
                totalLen += text.length;
            }

            return JSON.stringify({
                url: url,
                title: title,
                scroll_y: Math.round(scrollY),
                text: texts.join('\\n')
            });
        })()
        '''
        result = cdp._evaluate_js(js)
        try:
            data = json.loads(result)
            return {"success": True, **data}
        except:
            return {"success": True, "text": str(result)[:5000]}

    def wait_seconds(self, seconds: float) -> Dict[str, Any]:
        """Wait/sleep for specified seconds (max 30)"""
        seconds = min(max(seconds, 0.1), 30)
        time.sleep(seconds)
        return {"success": True, "waited": seconds}
