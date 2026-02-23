"""
CDP Client - Chrome DevTools Protocol wrapper with condition-based waits
No more sleep() - wait for conditions instead
Human-like behavior built-in
"""
import json
import time
import re
import random
import requests
import websocket
from dataclasses import dataclass
from typing import Optional, Callable, Any, Dict, List
from enum import Enum
from datetime import datetime

from .human_behavior import HumanBehavior, AntiDetection, WaitStrategy


class ConditionType(Enum):
    """Types of wait conditions"""
    ELEMENT_VISIBLE = "element_visible"
    ELEMENT_CLICKABLE = "element_clickable"
    ELEMENT_EXISTS = "element_exists"
    TEXT_PRESENT = "text_present"
    URL_CONTAINS = "url_contains"
    URL_EQUALS = "url_equals"
    PAGE_LOADED = "page_loaded"
    NETWORK_IDLE = "network_idle"
    CUSTOM = "custom"


@dataclass
class Condition:
    """A wait condition"""
    type: ConditionType
    selector: Optional[str] = None
    text: Optional[str] = None
    url: Optional[str] = None
    custom_fn: Optional[Callable] = None
    description: str = ""


@dataclass
class WaitResult:
    """Result from waiting for a condition"""
    success: bool
    elapsed_ms: int
    error: Optional[str] = None
    data: Any = None


@dataclass
class ActionResult:
    """Result from executing an action"""
    success: bool
    error: Optional[str] = None
    data: Any = None


class CDPClient:
    """
    CDP Client with condition-based waiting

    Key principles:
    - Never use blind sleep()
    - Wait for explicit conditions
    - Separate action from verification
    - Track all operations for debugging
    """

    def __init__(self, remote_port: int, timeout_ms: int = 30000):
        self.remote_port = remote_port
        self.base_url = f"http://127.0.0.1:{remote_port}"
        self.default_timeout = timeout_ms
        self.ws: Optional[websocket.WebSocket] = None
        self.page_ws_url: Optional[str] = None
        self._msg_id = 0
        self._operation_log: List[Dict] = []

    def _log_operation(self, operation: str, success: bool,
                       duration_ms: int, details: Dict = None):
        """Log operation for debugging"""
        self._operation_log.append({
            'operation': operation,
            'success': success,
            'duration_ms': duration_ms,
            'timestamp': datetime.now().isoformat(),
            'details': details or {}
        })

    def get_operation_log(self) -> List[Dict]:
        """Get operation log for debugging"""
        return self._operation_log.copy()

    def connect(self) -> ActionResult:
        """Connect to browser via CDP"""
        start = datetime.now()
        try:
            # Get page list
            resp = requests.get(f"{self.base_url}/json", timeout=10)
            pages = resp.json()

            # Find main page (not devtools, extension, etc)
            page = None
            for p in pages:
                url = p.get('url', '')
                ptype = p.get('type', '')
                if ptype == 'page' and not url.startswith('devtools://'):
                    page = p
                    break

            if not page:
                page = pages[0] if pages else None

            if not page:
                return ActionResult(False, "No page found")

            self.page_ws_url = page.get('webSocketDebuggerUrl', '')
            if not self.page_ws_url:
                return ActionResult(False, "No WebSocket URL")

            # Connect WebSocket with retries
            for attempt in range(3):
                try:
                    self.ws = websocket.create_connection(
                        self.page_ws_url,
                        timeout=30,
                        suppress_origin=True
                    )
                    break
                except Exception as e:
                    if attempt == 2:
                        return ActionResult(False, f"WebSocket connect failed: {e}")
                    time.sleep(1)

            duration = int((datetime.now() - start).total_seconds() * 1000)
            self._log_operation('connect', True, duration)
            return ActionResult(True, data={'ws_url': self.page_ws_url})

        except Exception as e:
            duration = int((datetime.now() - start).total_seconds() * 1000)
            self._log_operation('connect', False, duration, {'error': str(e)})
            return ActionResult(False, str(e))

    def disconnect(self):
        """Disconnect from browser"""
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
            self.ws = None

    def _send_command(self, method: str, params: Dict = None) -> Dict:
        """Send CDP command and get response"""
        if not self.ws:
            raise Exception("Not connected")

        self._msg_id += 1
        msg = {
            'id': self._msg_id,
            'method': method,
            'params': params or {}
        }

        try:
            self.ws.send(json.dumps(msg))
        except Exception as e:
            raise Exception(f"WebSocket send failed for {method}: {e}")

        # Wait for response with matching id (max 60s)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                resp = json.loads(self.ws.recv())
                if resp.get('id') == self._msg_id:
                    return resp
            except websocket.WebSocketTimeoutException:
                raise Exception(f"Timeout waiting for {method}")
            except (websocket.WebSocketConnectionClosedException, ConnectionError, OSError) as e:
                raise Exception(f"WebSocket closed during {method}: {e}")
            except Exception as e:
                raise Exception(f"WebSocket error during {method}: {e}")
        raise Exception(f"Deadline exceeded waiting for {method}")

    def _evaluate_js(self, expression: str) -> Any:
        """Evaluate JavaScript and return result"""
        resp = self._send_command('Runtime.evaluate', {
            'expression': expression,
            'returnByValue': True,
            'awaitPromise': True
        })

        if 'error' in resp:
            raise Exception(resp['error'].get('message', 'Unknown error'))

        result = resp.get('result', {}).get('result', {})
        if result.get('type') == 'undefined':
            return None

        return result.get('value')

    # ==================== CONDITION-BASED WAITS ====================

    def wait_for(self, condition: Condition, timeout_ms: int = None,
                debounce_ms: int = 0) -> WaitResult:
        """
        Wait for a condition to be true
        This is the core - no blind sleep!

        Args:
            condition: The condition to wait for
            timeout_ms: Maximum time to wait
            debounce_ms: Condition must be true for this long (anti-flake)
        """
        timeout = timeout_ms or self.default_timeout
        start = datetime.now()
        stable_start = None

        while True:
            elapsed = int((datetime.now() - start).total_seconds() * 1000)
            if elapsed > timeout:
                self._log_operation(
                    f'wait_{condition.type.value}', False, elapsed,
                    {'condition': condition.description, 'timeout': True}
                )
                return WaitResult(
                    success=False,
                    elapsed_ms=elapsed,
                    error=f"Timeout waiting for: {condition.description or condition.type.value}"
                )

            try:
                result = self._check_condition(condition)
                if result:
                    if debounce_ms > 0:
                        # Debounce: condition must stay true
                        if stable_start is None:
                            stable_start = datetime.now()
                        elif (datetime.now() - stable_start).total_seconds() * 1000 >= debounce_ms:
                            self._log_operation(
                                f'wait_{condition.type.value}', True, elapsed,
                                {'condition': condition.description, 'debounced': True}
                            )
                            return WaitResult(success=True, elapsed_ms=elapsed, data=result)
                    else:
                        self._log_operation(
                            f'wait_{condition.type.value}', True, elapsed,
                            {'condition': condition.description}
                        )
                        return WaitResult(success=True, elapsed_ms=elapsed, data=result)
                else:
                    stable_start = None  # Reset debounce
            except Exception as e:
                stable_start = None  # Reset debounce on error
                pass

            # Poll with jitter to avoid pattern detection
            poll_interval = HumanBehavior.add_jitter(0.1, 0.3)
            time.sleep(poll_interval)

    def _check_condition(self, condition: Condition) -> Any:
        """Check if a condition is met"""
        if condition.type == ConditionType.ELEMENT_EXISTS:
            js = f'''
                (function() {{
                    let el = document.querySelector('{condition.selector}');
                    return el !== null;
                }})()
            '''
            return self._evaluate_js(js)

        elif condition.type == ConditionType.ELEMENT_VISIBLE:
            js = f'''
                (function() {{
                    let el = document.querySelector('{condition.selector}');
                    if (!el) return false;
                    let rect = el.getBoundingClientRect();
                    let style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                           && style.visibility !== 'hidden'
                           && style.display !== 'none';
                }})()
            '''
            return self._evaluate_js(js)

        elif condition.type == ConditionType.ELEMENT_CLICKABLE:
            js = f'''
                (function() {{
                    let el = document.querySelector('{condition.selector}');
                    if (!el) return false;
                    let rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) return false;
                    // Check if in viewport
                    if (rect.top < 0 || rect.top > window.innerHeight) return false;
                    // Check if not disabled
                    if (el.disabled) return false;
                    return true;
                }})()
            '''
            return self._evaluate_js(js)

        elif condition.type == ConditionType.TEXT_PRESENT:
            js = f'''
                (function() {{
                    let el = document.querySelector('{condition.selector}');
                    if (!el) return false;
                    return el.textContent.includes('{condition.text}');
                }})()
            '''
            return self._evaluate_js(js)

        elif condition.type == ConditionType.URL_CONTAINS:
            js = f"window.location.href.includes('{condition.url}')"
            return self._evaluate_js(js)

        elif condition.type == ConditionType.URL_EQUALS:
            js = f"window.location.href === '{condition.url}'"
            return self._evaluate_js(js)

        elif condition.type == ConditionType.PAGE_LOADED:
            js = "document.readyState === 'complete'"
            return self._evaluate_js(js)

        elif condition.type == ConditionType.NETWORK_IDLE:
            # Check if no pending requests (simplified)
            js = '''
                (function() {
                    return document.readyState === 'complete';
                })()
            '''
            return self._evaluate_js(js)

        elif condition.type == ConditionType.CUSTOM:
            if condition.custom_fn:
                return condition.custom_fn(self)
            return False

        return False

    # ==================== ACTIONS ====================

    def navigate(self, url: str, wait_load: bool = True) -> ActionResult:
        """Navigate to URL"""
        start = datetime.now()
        try:
            self._send_command('Page.navigate', {'url': url})

            if wait_load:
                result = self.wait_for(
                    Condition(ConditionType.PAGE_LOADED, description=f"Page load: {url}"),
                    timeout_ms=30000
                )
                if not result.success:
                    return ActionResult(False, result.error)

            duration = int((datetime.now() - start).total_seconds() * 1000)
            self._log_operation('navigate', True, duration, {'url': url})
            return ActionResult(True)

        except Exception as e:
            duration = int((datetime.now() - start).total_seconds() * 1000)
            self._log_operation('navigate', False, duration, {'url': url, 'error': str(e)})
            return ActionResult(False, str(e))

    def click(self, selector: str, human_like: bool = True) -> ActionResult:
        """Click element with human-like behavior"""
        start = datetime.now()
        try:
            # First wait for element to be clickable
            wait_result = self.wait_for(
                Condition(
                    ConditionType.ELEMENT_CLICKABLE,
                    selector=selector,
                    description=f"Clickable: {selector}"
                ),
                timeout_ms=10000
            )

            if not wait_result.success:
                return ActionResult(False, wait_result.error)

            if human_like:
                # Human-like: scroll, hover, then click
                result = AntiDetection.natural_click(self, selector)
            else:
                # Direct click
                js = f'''
                    (function() {{
                        let el = document.querySelector('{selector}');
                        if (el) {{
                            el.click();
                            return true;
                        }}
                        return false;
                    }})()
                '''
                result = self._evaluate_js(js)

            # Add human-like delay after click
            if human_like:
                HumanBehavior.random_delay(0.3, 0.8)

            duration = int((datetime.now() - start).total_seconds() * 1000)
            self._log_operation('click', result, duration, {'selector': selector})
            return ActionResult(result, error=None if result else "Click failed")

        except Exception as e:
            duration = int((datetime.now() - start).total_seconds() * 1000)
            self._log_operation('click', False, duration, {'selector': selector, 'error': str(e)})
            return ActionResult(False, str(e))

    def type_text(self, selector: str, text: str, clear: bool = True,
                  human_like: bool = True) -> ActionResult:
        """Type text with optional human-like character-by-character input"""
        start = datetime.now()
        try:
            # Wait for element
            wait_result = self.wait_for(
                Condition(
                    ConditionType.ELEMENT_VISIBLE,
                    selector=selector,
                    description=f"Input visible: {selector}"
                ),
                timeout_ms=10000
            )

            if not wait_result.success:
                return ActionResult(False, wait_result.error)

            if clear:
                # Clear existing content
                clear_js = f'''
                    (function() {{
                        let el = document.querySelector('{selector}');
                        if (!el) return false;
                        el.focus();
                        if (el.contentEditable === 'true') {{
                            el.innerHTML = '';
                        }} else {{
                            el.value = '';
                        }}
                        return true;
                    }})()
                '''
                self._evaluate_js(clear_js)
                HumanBehavior.random_delay(0.1, 0.3)

            if human_like and len(text) < 500:
                # Type character by character for shorter texts
                result = AntiDetection.gradual_type(self, selector, text)
            else:
                # Paste for longer texts - dùng CDP Input.insertText cho Lexical editor
                escaped_text = text.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n')

                # Focus element trước
                focus_js = f'''
                    (function() {{
                        let el = document.querySelector('{selector}');
                        if (!el) return {{found: false}};
                        el.focus();
                        let isContentEditable = el.contentEditable === 'true';
                        if (!isContentEditable) {{
                            el.value = '{escaped_text}';
                            el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        }}
                        return {{found: true, isContentEditable: isContentEditable}};
                    }})()
                '''
                focus_result = self._evaluate_js(focus_js)

                if focus_result and focus_result.get('isContentEditable'):
                    # Gõ từng ký tự cho contenteditable (hoạt động với Lexical editor)
                    try:
                        for char in text:
                            self._send_command('Input.insertText', {'text': char})
                            # Delay ngắn để trông tự nhiên hơn
                            if char in ' .,!?;:\n':
                                time.sleep(random.uniform(0.03, 0.08))
                            else:
                                time.sleep(random.uniform(0.015, 0.04))
                        result = True
                    except Exception as e:
                        print(f"[CDPClient] Input.insertText error: {e}")
                        result = False
                else:
                    result = focus_result.get('found', False) if focus_result else False

            duration = int((datetime.now() - start).total_seconds() * 1000)
            self._log_operation('type_text', result, duration, {
                'selector': selector,
                'text_length': len(text),
                'human_like': human_like
            })
            return ActionResult(True if result else False)

        except Exception as e:
            duration = int((datetime.now() - start).total_seconds() * 1000)
            self._log_operation('type_text', False, duration, {'error': str(e)})
            return ActionResult(False, str(e))

    def scroll_to(self, selector: str = None, y: int = None) -> ActionResult:
        """Scroll to element or position"""
        start = datetime.now()
        try:
            if selector:
                js = f'''
                    (function() {{
                        let el = document.querySelector('{selector}');
                        if (el) {{
                            el.scrollIntoView({{behavior: 'smooth', block: 'center'}});
                            return true;
                        }}
                        return false;
                    }})()
                '''
            else:
                js = f'window.scrollTo(0, {y or 0}); true'

            result = self._evaluate_js(js)
            time.sleep(0.3)  # Brief wait for scroll animation

            duration = int((datetime.now() - start).total_seconds() * 1000)
            self._log_operation('scroll', result, duration)
            return ActionResult(result)

        except Exception as e:
            duration = int((datetime.now() - start).total_seconds() * 1000)
            self._log_operation('scroll', False, duration, {'error': str(e)})
            return ActionResult(False, str(e))

    def execute_js(self, js_code: str) -> ActionResult:
        """Execute arbitrary JavaScript"""
        start = datetime.now()
        try:
            result = self._evaluate_js(js_code)
            duration = int((datetime.now() - start).total_seconds() * 1000)
            self._log_operation('execute_js', True, duration)
            return ActionResult(True, data=result)
        except Exception as e:
            duration = int((datetime.now() - start).total_seconds() * 1000)
            self._log_operation('execute_js', False, duration, {'error': str(e)})
            return ActionResult(False, str(e))

    def get_element_text(self, selector: str) -> ActionResult:
        """Get text content of element"""
        try:
            js = f'''
                (function() {{
                    let el = document.querySelector('{selector}');
                    return el ? el.textContent : null;
                }})()
            '''
            text = self._evaluate_js(js)
            return ActionResult(True, data=text)
        except Exception as e:
            return ActionResult(False, str(e))

    def get_current_url(self) -> ActionResult:
        """Get current page URL"""
        try:
            url = self._evaluate_js('window.location.href')
            return ActionResult(True, data=url)
        except Exception as e:
            return ActionResult(False, str(e))

    def take_screenshot(self) -> ActionResult:
        """Take screenshot and return base64 data"""
        start = datetime.now()
        try:
            resp = self._send_command('Page.captureScreenshot', {'format': 'png'})
            data = resp.get('result', {}).get('data', '')
            duration = int((datetime.now() - start).total_seconds() * 1000)
            self._log_operation('screenshot', True, duration)
            return ActionResult(True, data=data)
        except Exception as e:
            duration = int((datetime.now() - start).total_seconds() * 1000)
            self._log_operation('screenshot', False, duration, {'error': str(e)})
            return ActionResult(False, str(e))

    # ==================== VERIFICATION ====================

    def verify_element_exists(self, selector: str) -> bool:
        """Verify element exists"""
        result = self.wait_for(
            Condition(ConditionType.ELEMENT_EXISTS, selector=selector),
            timeout_ms=5000
        )
        return result.success

    def verify_text_present(self, selector: str, text: str) -> bool:
        """Verify text is present in element"""
        result = self.wait_for(
            Condition(ConditionType.TEXT_PRESENT, selector=selector, text=text),
            timeout_ms=5000
        )
        return result.success

    def verify_url_contains(self, url_part: str) -> bool:
        """Verify URL contains string"""
        result = self.wait_for(
            Condition(ConditionType.URL_CONTAINS, url=url_part),
            timeout_ms=5000
        )
        return result.success
