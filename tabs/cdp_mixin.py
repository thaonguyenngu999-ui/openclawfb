"""
CDP Mixin - Các phương thức CDP chung cho tất cả các tab
Cung cấp các helper methods để làm việc với Chrome DevTools Protocol
"""
import json as json_module
import random
import time
import re
import threading
from typing import Dict, Any, Optional, Tuple, List

try:
    import websocket
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


class CDPMixin:
    """
    Mixin class cung cấp các phương thức CDP chung.
    Kế thừa class này để có các helper methods cho CDP automation.
    """
    
    def __init__(self):
        # CDP state - thread-local để tránh race condition
        self._cdp_thread_local = threading.local()
    
    @property
    def _cdp_id(self) -> int:
        """Get thread-local CDP message ID"""
        if not hasattr(self._cdp_thread_local, 'cdp_id'):
            self._cdp_thread_local.cdp_id = 0
        return self._cdp_thread_local.cdp_id
    
    @_cdp_id.setter
    def _cdp_id(self, value: int):
        self._cdp_thread_local.cdp_id = value
    
    @property
    def _posting_port(self) -> int:
        """Get thread-local posting port"""
        if not hasattr(self._cdp_thread_local, 'posting_port'):
            self._cdp_thread_local.posting_port = 0
        return self._cdp_thread_local.posting_port
    
    @_posting_port.setter
    def _posting_port(self, value: int):
        self._cdp_thread_local.posting_port = value
    
    # ============ CDP CORE METHODS ============
    
    def cdp_send(self, ws, method: str, params: Dict = None, timeout: int = 30) -> Dict:
        """
        Gửi CDP command và nhận response.
        
        Args:
            ws: WebSocket connection
            method: CDP method name (e.g., "Page.navigate", "Runtime.evaluate")
            params: Parameters for the method
            timeout: Response timeout in seconds
            
        Returns:
            Dict with response data or error info
        """
        if not ws:
            return {"error": "No WebSocket connection", "ws_closed": True}

        self._cdp_id += 1
        msg = {"id": self._cdp_id, "method": method, "params": params or {}}

        try:
            ws.send(json_module.dumps(msg))
        except Exception as e:
            return {"error": f"WebSocket send failed: {str(e)}", "ws_closed": True}

        # Đợi response với đúng ID
        while True:
            try:
                ws.settimeout(timeout)
                resp = ws.recv()
                data = json_module.loads(resp)
                if data.get('id') == self._cdp_id:
                    return data
            except Exception as e:
                return {"error": f"WebSocket recv failed: {str(e)}", "ws_closed": True}

    def cdp_evaluate(self, ws, expression: str, await_promise: bool = True) -> Any:
        """
        Evaluate JavaScript trong browser.
        
        Args:
            ws: WebSocket connection
            expression: JavaScript code to evaluate
            await_promise: Whether to await Promise results
            
        Returns:
            The result value from the evaluation
        """
        result = self.cdp_send(ws, "Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise
        })
        return result.get('result', {}).get('result', {}).get('value')

    def is_ws_connected(self, ws) -> bool:
        """
        Kiểm tra WebSocket còn kết nối không.
        
        Args:
            ws: WebSocket connection to check
            
        Returns:
            True if connected and responsive
        """
        if not ws:
            return False
        try:
            result = self.cdp_send(ws, "Runtime.evaluate", {
                "expression": "1+1",
                "returnByValue": True
            }, timeout=5)
            if result.get('ws_closed'):
                return False
            return result.get('result', {}).get('result', {}).get('value') == 2
        except:
            return False

    def is_browser_alive(self, cdp_base: str) -> bool:
        """
        Kiểm tra browser còn chạy không bằng cách ping CDP port.
        
        Args:
            cdp_base: CDP base URL (e.g., "http://127.0.0.1:9222")
            
        Returns:
            True if browser is alive
        """
        if not REQUESTS_AVAILABLE:
            return False
        try:
            resp = requests.get(f"{cdp_base}/json/version", timeout=3)
            return resp.status_code == 200
        except:
            return False

    def get_or_create_ws(self, ws, cdp_base: str, target_url: str = None) -> Tuple[Any, bool]:
        """
        Kiểm tra WS hiện tại, nếu không ok thì tạo tab mới.
        
        Args:
            ws: Current WebSocket connection
            cdp_base: CDP base URL
            target_url: URL to navigate to if creating new tab
            
        Returns:
            Tuple of (ws, success_bool)
        """
        if not WEBSOCKET_AVAILABLE:
            return (None, False)
            
        # Check WS hiện tại
        if self.is_ws_connected(ws):
            return (ws, True)

        print(f"[CDP] WebSocket mất kết nối, đang reconnect...")

        # Kiểm tra browser còn sống không
        if not self.is_browser_alive(cdp_base):
            print(f"[CDP] Browser đã đóng hoàn toàn, không thể reconnect")
            return (None, False)

        # Thử lấy WS từ tab hiện có
        try:
            resp = requests.get(f"{cdp_base}/json", timeout=10)
            pages = resp.json()
            for p in pages:
                if p.get('type') == 'page':
                    ws_url = p.get('webSocketDebuggerUrl')
                    if ws_url:
                        try:
                            new_ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
                            print(f"[CDP] Đã reconnect WS từ tab có sẵn")
                            return (new_ws, True)
                        except:
                            pass
        except:
            pass

        # Không có tab nào, tạo tab mới
        if target_url:
            try:
                resp = requests.get(f"{cdp_base}/json/new?{target_url}", timeout=10)
                new_page = resp.json()
                ws_url = new_page.get('webSocketDebuggerUrl')
                if ws_url:
                    new_ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
                    print(f"[CDP] Đã tạo tab mới và kết nối WS")
                    return (new_ws, True)
            except Exception as e:
                print(f"[CDP] Không tạo được tab mới: {e}")

        return (ws, False)

    def close_old_tabs(self, cdp_base: str, keep_blank: bool = True) -> bool:
        """
        Đóng hết tab cũ, tùy chọn giữ lại 1 tab về about:blank.
        
        Args:
            cdp_base: CDP base URL
            keep_blank: Whether to keep one blank tab open
            
        Returns:
            True if successful
        """
        if not WEBSOCKET_AVAILABLE or not REQUESTS_AVAILABLE:
            return False
            
        try:
            resp = requests.get(f"{cdp_base}/json", timeout=10)
            all_pages = resp.json()
            page_targets = [p for p in all_pages if p.get('type') == 'page']

            if len(page_targets) > 0:
                if keep_blank:
                    # Navigate tab đầu tiên về about:blank TRƯỚC
                    first_tab_ws = page_targets[0].get('webSocketDebuggerUrl')
                    if first_tab_ws:
                        try:
                            temp_ws = websocket.create_connection(first_tab_ws, timeout=10, suppress_origin=True)
                            temp_ws.send(json_module.dumps({
                                "id": 1,
                                "method": "Page.navigate",
                                "params": {"url": "about:blank"}
                            }))
                            temp_ws.recv()
                            temp_ws.close()
                            print(f"[CDP] Đã navigate tab chính về about:blank")
                        except Exception as e:
                            print(f"[CDP] Không navigate được tab chính: {e}")

                    # Đóng các tab còn lại
                    if len(page_targets) > 1:
                        for p in page_targets[1:]:
                            target_id = p.get('id')
                            if target_id:
                                requests.get(f"{cdp_base}/json/close/{target_id}", timeout=5)
                        time.sleep(1)
                        print(f"[CDP] Đã đóng {len(page_targets) - 1} tab cũ")
                else:
                    # Đóng tất cả tab
                    for p in page_targets:
                        target_id = p.get('id')
                        if target_id:
                            requests.get(f"{cdp_base}/json/close/{target_id}", timeout=5)
            else:
                # Không có tab nào, tạo tab mới
                print(f"[CDP] Không có tab nào, tạo tab mới...")
                requests.get(f"{cdp_base}/json/new?about:blank", timeout=10)
                time.sleep(1)
                
            return True
        except Exception as e:
            print(f"[CDP] Lỗi đóng tab cũ: {e}")
            return False

    def create_new_tab(self, ws, cdp_base: str, url: str, close_old: bool = True) -> Tuple[Any, str, bool]:
        """
        Tạo tab mới với URL, đóng tab cũ nếu cần.
        
        Args:
            ws: Current WebSocket connection
            cdp_base: CDP base URL
            url: URL to open in new tab
            close_old: Whether to close the old tab
            
        Returns:
            Tuple of (new_ws, target_id, success_bool)
        """
        if not WEBSOCKET_AVAILABLE or not REQUESTS_AVAILABLE:
            return (None, "", False)
            
        old_target_id = None
        
        # Lưu ID tab cũ
        try:
            resp = requests.get(f"{cdp_base}/json", timeout=10)
            pages = resp.json()
            for p in pages:
                if p.get('type') == 'page':
                    old_target_id = p.get('id')
                    break
        except:
            pass

        # Tạo tab mới với RETRY
        target_id = None
        new_ws = None
        
        for attempt in range(3):
            try:
                # Thử tạo tab mới qua CDP
                result = self.cdp_send(ws, "Target.createTarget", {"url": url})
                target_id = result.get('result', {}).get('targetId')

                if target_id:
                    time.sleep(random.uniform(2, 3))

                    # Lấy WebSocket của tab mới
                    new_ws_url = None
                    resp = requests.get(f"{cdp_base}/json", timeout=10)
                    pages = resp.json()
                    
                    for p in pages:
                        if p.get('id') == target_id:
                            new_ws_url = p.get('webSocketDebuggerUrl')
                            break
                            
                    # Fallback: tìm theo URL
                    if not new_ws_url:
                        for p in pages:
                            url_part = url.split('/')[-1] if '/' in url else url
                            if p.get('type') == 'page' and url_part in p.get('url', '') and p.get('id') != old_target_id:
                                new_ws_url = p.get('webSocketDebuggerUrl')
                                target_id = p.get('id')
                                break

                    if new_ws_url:
                        try:
                            new_ws = websocket.create_connection(new_ws_url, timeout=30, suppress_origin=True)
                        except:
                            try:
                                new_ws = websocket.create_connection(new_ws_url, timeout=30)
                            except:
                                pass

                        if new_ws:
                            print(f"[CDP] Tạo tab mới thành công (attempt {attempt + 1})")
                            break
            except Exception as e:
                print(f"[CDP] Attempt {attempt + 1}/3 tạo tab thất bại: {e}")
                time.sleep(1)

        # Nếu không tạo được tab mới, fallback navigate trên tab hiện tại
        if not new_ws:
            print(f"[CDP] Không tạo được tab mới, thử navigate trên tab hiện tại...")
            try:
                self.cdp_send(ws, "Page.handleJavaScriptDialog", {"accept": True})
            except:
                pass
            try:
                self.cdp_send(ws, "Page.navigate", {"url": url})
                time.sleep(random.uniform(3, 4))
                return (ws, old_target_id or "", True)
            except Exception as e:
                print(f"[CDP] Fallback navigate cũng thất bại: {e}")
                return (None, "", False)

        # Đóng WebSocket cũ
        if new_ws != ws:
            try:
                ws.close()
            except:
                pass

        # Đóng tab cũ nếu cần
        if close_old and old_target_id and old_target_id != target_id:
            try:
                requests.get(f"{cdp_base}/json/close/{old_target_id}", timeout=5)
                print(f"[CDP] Đã đóng tab cũ")
            except:
                pass

        return (new_ws, target_id, True)

    def wait_for_page_load(self, ws, cdp_base: str = None, target_url: str = None, 
                           max_wait: int = 15, retry_ws: bool = True) -> bool:
        """
        Đợi page load xong với retry.
        
        Args:
            ws: WebSocket connection
            cdp_base: CDP base URL (required if retry_ws=True)
            target_url: URL for reconnection (required if retry_ws=True)
            max_wait: Maximum wait time in seconds
            retry_ws: Whether to retry WebSocket connection on failure
            
        Returns:
            True if page loaded successfully
        """
        for i in range(max_wait):
            try:
                ready = self.cdp_evaluate(ws, "document.readyState")
                if ready == 'complete':
                    return True
            except:
                if retry_ws and cdp_base:
                    ws, _ = self.get_or_create_ws(ws, cdp_base, target_url)
            time.sleep(1)
        return False

    # ============ HUMAN-LIKE BEHAVIOR METHODS ============

    def move_mouse_human(self, ws, target_x: int, target_y: int, steps: int = 20):
        """
        Di chuyển chuột theo đường cong Bezier như người thật.
        
        Args:
            ws: WebSocket connection
            target_x: Target X coordinate
            target_y: Target Y coordinate
            steps: Number of interpolation steps
        """
        # Vị trí bắt đầu ngẫu nhiên
        current_x = random.randint(100, 300)
        current_y = random.randint(100, 200)

        # Control point cho Bezier curve
        ctrl_x = (current_x + target_x) / 2 + random.randint(-100, 100)
        ctrl_y = (current_y + target_y) / 2 + random.randint(-50, 50)

        for i in range(steps + 1):
            t = i / steps
            # Quadratic Bezier curve
            x = int((1-t)**2 * current_x + 2*(1-t)*t * ctrl_x + t**2 * target_x)
            y = int((1-t)**2 * current_y + 2*(1-t)*t * ctrl_y + t**2 * target_y)

            self.cdp_send(ws, "Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": x,
                "y": y
            })
            time.sleep(random.uniform(0.005, 0.02))

    def click_at_position(self, ws, x: int, y: int, human_move: bool = True):
        """
        Click tại vị trí với mouse movement trước.
        
        Args:
            ws: WebSocket connection
            x: X coordinate to click
            y: Y coordinate to click
            human_move: Whether to use human-like mouse movement
        """
        if human_move:
            self.move_mouse_human(ws, x, y)
            time.sleep(random.uniform(0.1, 0.3))

        # Mouse down
        self.cdp_send(ws, "Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": x,
            "y": y,
            "button": "left",
            "clickCount": 1
        })
        time.sleep(random.uniform(0.05, 0.15))
        
        # Mouse up
        self.cdp_send(ws, "Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": x,
            "y": y,
            "button": "left",
            "clickCount": 1
        })

    def scroll_page(self, ws, direction: str = "down", amount: int = None, 
                    steps: int = None, human_like: bool = True):
        """
        Scroll trang như người thật.
        
        Args:
            ws: WebSocket connection
            direction: "down" or "up"
            amount: Scroll amount in pixels (random if None)
            steps: Number of scroll steps (random if None)
            human_like: Whether to use human-like scrolling
        """
        if amount is None:
            amount = random.randint(200, 500)
        if steps is None:
            steps = random.randint(3, 6) if human_like else 1

        if direction == "up":
            amount = -amount

        if human_like:
            step_amount = amount // steps
            for _ in range(steps):
                self.cdp_evaluate(ws, f"window.scrollBy(0, {step_amount})")
                time.sleep(random.uniform(0.05, 0.15))
            time.sleep(random.uniform(0.3, 0.7))
        else:
            self.cdp_evaluate(ws, f"window.scrollBy(0, {amount})")

    def type_like_human(self, ws, text: str, typo_chance: float = 0.03) -> bool:
        """
        Gõ từng ký tự như người thật với typo và pause.
        
        Args:
            ws: WebSocket connection
            text: Text to type
            typo_chance: Probability of making a typo (0-1)
            
        Returns:
            True if successful, False if WebSocket closed
        """
        # Adjacent keys for typo simulation
        typo_map = {
            'a': ['s', 'q', 'z'], 'b': ['v', 'n', 'g'], 'c': ['x', 'v', 'd'],
            'd': ['s', 'f', 'e'], 'e': ['w', 'r', 'd'], 'f': ['d', 'g', 'r'],
            'g': ['f', 'h', 't'], 'h': ['g', 'j', 'y'], 'i': ['u', 'o', 'k'],
            'j': ['h', 'k', 'u'], 'k': ['j', 'l', 'i'], 'l': ['k', 'o', 'p'],
            'm': ['n', 'k'], 'n': ['b', 'm', 'h'], 'o': ['i', 'p', 'l'],
            'p': ['o', 'l'], 'q': ['w', 'a'], 'r': ['e', 't', 'f'],
            's': ['a', 'd', 'w'], 't': ['r', 'y', 'g'], 'u': ['y', 'i', 'j'],
            'v': ['c', 'b', 'f'], 'w': ['q', 'e', 's'], 'x': ['z', 'c', 's'],
            'y': ['t', 'u', 'h'], 'z': ['x', 'a']
        }

        paragraphs = text.split('\n')

        for p_idx, paragraph in enumerate(paragraphs):
            if not paragraph.strip():
                result = self.cdp_send(ws, "Input.insertText", {"text": "\n"})
                if result.get('ws_closed'):
                    return False
                time.sleep(random.uniform(0.3, 0.8))
                continue

            sentences = paragraph.replace('. ', '.|').replace('! ', '!|').replace('? ', '?|').split('|')

            for s_idx, sentence in enumerate(sentences):
                for char in sentence:
                    # Random typo
                    if char.lower() in typo_map and random.random() < typo_chance:
                        wrong_char = random.choice(typo_map[char.lower()])
                        result = self.cdp_send(ws, "Input.insertText", {"text": wrong_char})
                        if result.get('ws_closed'):
                            return False
                        time.sleep(random.uniform(0.05, 0.15))
                        time.sleep(random.uniform(0.2, 0.5))
                        
                        # Backspace
                        self.cdp_send(ws, "Input.dispatchKeyEvent", {
                            "type": "keyDown", "key": "Backspace", "code": "Backspace"
                        })
                        self.cdp_send(ws, "Input.dispatchKeyEvent", {
                            "type": "keyUp", "key": "Backspace", "code": "Backspace"
                        })
                        time.sleep(random.uniform(0.1, 0.2))

                    # Type correct char
                    result = self.cdp_send(ws, "Input.insertText", {"text": char})
                    if result.get('ws_closed'):
                        return False

                    # Variable delay
                    if char in ' .,!?':
                        time.sleep(random.uniform(0.08, 0.2))
                    elif char.isupper():
                        time.sleep(random.uniform(0.06, 0.15))
                    else:
                        time.sleep(random.uniform(0.03, 0.1))

                if s_idx < len(sentences) - 1:
                    time.sleep(random.uniform(0.3, 0.8))

            if p_idx < len(paragraphs) - 1:
                result = self.cdp_send(ws, "Input.insertText", {"text": "\n"})
                if result.get('ws_closed'):
                    return False
                time.sleep(random.uniform(0.5, 1.2))

        return True

    # ============ BROWSER CONNECTION HELPERS ============

    def get_cdp_connection(self, remote_port: int, retry: int = 5) -> Tuple[Any, str]:
        """
        Lấy WebSocket connection từ CDP port.
        
        Args:
            remote_port: CDP remote debugging port
            retry: Number of retry attempts
            
        Returns:
            Tuple of (ws, cdp_base) or (None, None) if failed
        """
        if not WEBSOCKET_AVAILABLE or not REQUESTS_AVAILABLE:
            return (None, None)
            
        cdp_base = f"http://127.0.0.1:{remote_port}"
        page_ws = None

        for attempt in range(retry):
            try:
                resp = requests.get(f"{cdp_base}/json", timeout=10)
                pages = resp.json()
                for p in pages:
                    if p.get('type') == 'page':
                        page_ws = p.get('webSocketDebuggerUrl')
                        break
                if page_ws:
                    break
            except Exception as e:
                print(f"[CDP] Attempt {attempt + 1}/{retry} failed: {e}")
                time.sleep(1)

        if not page_ws:
            return (None, None)

        # Kết nối WebSocket với nhiều fallback
        ws = None
        try:
            ws = websocket.create_connection(page_ws, timeout=30, suppress_origin=True)
        except:
            try:
                ws = websocket.create_connection(page_ws, timeout=30, origin=f"http://127.0.0.1:{remote_port}")
            except:
                try:
                    ws = websocket.create_connection(page_ws, timeout=30)
                except:
                    pass

        return (ws, cdp_base)

    def extract_port_from_result(self, result: Dict) -> Optional[int]:
        """
        Trích xuất remote_port từ kết quả api.open_browser().
        
        Args:
            result: Result dict from api.open_browser()
            
        Returns:
            Remote port number or None
        """
        data = result.get('data', {})
        remote_port = data.get('remote_port')
        
        if not remote_port:
            ws_url = data.get('web_socket', '')
            match = re.search(r':(\d+)/', ws_url)
            if match:
                remote_port = int(match.group(1))
                
        return remote_port

    def click_at_element(self, ws, selector: str, human_move: bool = True) -> bool:
        """
        Click vào element theo CSS selector với mouse movement.
        
        Args:
            ws: WebSocket connection
            selector: CSS selector
            human_move: Whether to use human-like mouse movement
            
        Returns:
            True if click succeeded
        """
        # Lấy vị trí element
        get_pos_js = f'''
        (function() {{
            let el = document.querySelector('{selector}');
            if (!el) return null;
            let rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) return null;
            return {{
                x: rect.left + rect.width / 2 + (Math.random() * 10 - 5),
                y: rect.top + rect.height / 2 + (Math.random() * 6 - 3)
            }};
        }})()
        '''
        pos = self.cdp_evaluate(ws, get_pos_js)
        if not pos or 'x' not in pos:
            return False

        # Click
        self.click_at_position(ws, int(pos['x']), int(pos['y']), human_move)
        return True

    def upload_files(self, ws, files: List[str]) -> bool:
        """
        Upload files qua input[type="file"] sử dụng DOM.setFileInputFiles.
        
        Args:
            ws: WebSocket connection
            files: List of absolute file paths
            
        Returns:
            True if upload succeeded
        """
        if not files:
            return False

        try:
            # Get document root
            doc_result = self.cdp_send(ws, "DOM.getDocument", {})
            root_id = doc_result.get('result', {}).get('root', {}).get('nodeId', 0)
            if not root_id:
                return False

            # Find all file inputs
            query_result = self.cdp_send(ws, "DOM.querySelectorAll", {
                "nodeId": root_id,
                "selector": 'input[type="file"]'
            })
            node_ids = query_result.get('result', {}).get('nodeIds', [])

            # Try each file input
            for node_id in node_ids:
                try:
                    result = self.cdp_send(ws, "DOM.setFileInputFiles", {
                        "nodeId": node_id,
                        "files": files
                    })
                    if not result.get('error'):
                        time.sleep(1)
                        # Check for preview
                        has_preview = self.cdp_evaluate(ws, 
                            "(function() { return document.querySelectorAll('img[src*=\"blob:\"]').length > 0; })()")
                        if has_preview:
                            return True
                except:
                    continue

            return False
        except Exception as e:
            print(f"[CDP] Upload files error: {e}")
            return False

    def find_and_click_button(self, ws, button_texts: List[str], human_move: bool = True) -> bool:
        """
        Tìm và click button theo text.
        
        Args:
            ws: WebSocket connection
            button_texts: List of button text to search for
            human_move: Whether to use human-like mouse movement
            
        Returns:
            True if click succeeded
        """
        texts_json = json_module.dumps(button_texts)
        get_btn_pos_js = f'''
        (function() {{
            const buttonTexts = {texts_json};
            
            function getCoords(btn) {{
                let rect = btn.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0 && rect.top > 0) {{
                    return {{
                        x: rect.left + rect.width / 2 + (Math.random() * 10 - 5),
                        y: rect.top + rect.height / 2 + (Math.random() * 4 - 2)
                    }};
                }}
                return null;
            }}

            // Find by role="button" and text
            let btns = document.querySelectorAll('[role="button"]');
            for (let btn of btns) {{
                let text = (btn.innerText || '').trim();
                if (buttonTexts.includes(text)) {{
                    let coords = getCoords(btn);
                    if (coords) return coords;
                }}
            }}

            // Find button with text in span
            let spans = document.querySelectorAll('span');
            for (let span of spans) {{
                let text = (span.innerText || '').trim();
                if (buttonTexts.includes(text)) {{
                    let btn = span.closest('[role="button"]');
                    if (btn) {{
                        let coords = getCoords(btn);
                        if (coords) return coords;
                    }}
                }}
            }}

            return null;
        }})()
        '''

        pos = self.cdp_evaluate(ws, get_btn_pos_js)
        if not pos or 'x' not in pos:
            return False

        self.click_at_position(ws, int(pos['x']), int(pos['y']), human_move)
        return True

