"""
LoginSkill - Facebook login & status checking via CDP.
Includes FB_STATUS_JS, LOGIN_RESULT_JS, check_fb_status, check_fb_batch,
login_fb, login_fb_try_accounts, check_login.
"""

import json
import os
import re
import threading
import time
import traceback
from typing import Optional, Dict, Any, List
from concurrent.futures import ThreadPoolExecutor, as_completed
from api_service import api as hidemium
from skills.base_skill import SCREENSHOT_DIR


class LoginSkill:
    """Facebook login & status detection."""

    # ============================================================
    # JS blocks for detecting FB login status
    # ============================================================

    FB_STATUS_JS = '''
    (function() {
        try {
            let url = window.location.href;

            // Check for checkpoint/locked
            if (url.includes('checkpoint') || url.includes('locked') ||
                url.includes('recover') || url.includes('accountquality')) {
                let pageText = (document.body.innerText || '').toLowerCase();
                if (pageText.includes('xác nhận') || pageText.includes('confirm') ||
                    pageText.includes('verify')) return 'LOCKED:checkpoint_verify';
                if (pageText.includes('vô hiệu hóa') || pageText.includes('disabled'))
                    return 'DIE:account_disabled';
                return 'LOCKED:' + url.substring(0, 80);
            }

            // Check for 2FA
            if (url.includes('two_step') || url.includes('code_generator') ||
                document.querySelector('input[name="approvals_code"]'))
                return '2FA:code_required';

            // Check if on login page URL = NOT logged in
            if (url.includes('/login') || url.includes('login.php') ||
                url.includes('/login/') || url.includes('login/?')) {
                return 'NOT_LOGGED_IN:login_page';
            }

            // Check for login form (email + password fields) = NOT logged in
            let emailField = document.querySelector('#email, input[name="email"]');
            let passField = document.querySelector('#pass, input[name="pass"]');
            if (emailField && passField) return 'NOT_LOGGED_IN:login_form_present';

            // Check for "Create Account" / "Sign Up" button = login/signup page
            let createAcct = document.querySelector('[data-testid="open-registration-form-button"]') ||
                             document.querySelector('a[href*="r.php"]');
            if (createAcct && emailField) return 'NOT_LOGGED_IN:signup_page';

            // Check for disabled account page
            let bodyText = (document.body.innerText || '').toLowerCase();
            let diePhrases = ['tài khoản của bạn đã bị vô hiệu hóa',
                              'your account has been disabled',
                              'your account is disabled',
                              'this account has been suspended',
                              'account has been locked',
                              'tài khoản đã bị khóa',
                              'we suspended your account',
                              'tạm ngưng tài khoản'];
            for (let p of diePhrases) {
                if (bodyText.includes(p)) return 'DIE:disabled';
            }

            // Check for modal dialog blocking = usually locked
            let modal = document.querySelector('[role="dialog"]');
            if (modal) {
                let modalText = (modal.innerText || '').toLowerCase();
                if (modalText.includes('checkpoint') || modalText.includes('xác nhận') ||
                    modalText.includes('verify') || modalText.includes('identit'))
                    return 'LOCKED:modal_checkpoint';
                if (modalText.includes('disabled') || modalText.includes('vô hiệu hóa'))
                    return 'DIE:modal_disabled';
            }

            // === LIVE DETECTION ===
            // Key logic: if we're at facebook.com/ and there's NO login form,
            // the user IS logged in. FB ALWAYS shows login form immediately for
            // logged-out users. The SPA indicators (Messenger, etc.) take time to
            // render but the absence of login form = logged in.

            let hasComposer = document.querySelector('[aria-label*="bạn đang nghĩ gì"]') ||
                              document.querySelector('[aria-label*="What\\'s on your mind"]') ||
                              document.querySelector('[role="textbox"][contenteditable="true"]');
            let hasFeed = document.querySelectorAll('[role="article"], div[aria-posinset]').length > 0;
            let hasNav = document.querySelector('[aria-label*="Messenger"]') ||
                         document.querySelector('[aria-label*="Thông báo"]') ||
                         document.querySelector('[aria-label*="Notifications"]') ||
                         document.querySelector('[aria-label*="Tài khoản"]') ||
                         document.querySelector('[aria-label*="Account"]') ||
                         document.querySelector('[aria-label*="Menu"]');
            let hasAvatar = document.querySelector('svg image') ||
                            document.querySelector('image[*|href]');
            let hasSidebar = document.querySelector('[role="navigation"] a[href*="/groups/"]') ||
                             document.querySelector('[role="navigation"] a[href*="/friends/"]');

            // Strong LIVE: multiple indicators
            if ((hasComposer && hasFeed) || (hasNav && hasFeed) || (hasNav && hasComposer))
                return 'LIVE:full_indicators';
            if (hasNav && hasAvatar) return 'LIVE:nav_avatar';
            if (hasNav) return 'LIVE:nav_only';
            if (hasComposer) return 'LIVE:composer_only';
            if (hasSidebar && hasFeed) return 'LIVE:sidebar_feed';
            if (hasFeed && document.querySelectorAll('[role="article"]').length >= 2)
                return 'LIVE:feed_present';

            // KEY RULE: At facebook.com/ with NO login form = LIVE
            // FB always shows login form immediately for logged-out/dead accounts.
            // If no login form is found, the account is logged in even if SPA
            // indicators haven't rendered yet.
            let isFBHome = url.startsWith('https://www.facebook.com') ||
                           url.startsWith('https://m.facebook.com');
            if (isFBHome && !emailField && !passField) {
                let textLen = (document.body.innerText || '').trim().length;
                if (textLen < 20) return 'UNKNOWN:page_loading';
                // No login form + some content = LIVE (SPA not fully rendered yet)
                return 'LIVE:no_login_form';
            }

            return 'UNKNOWN:' + url.substring(0, 80);
        } catch(e) {
            return 'ERROR:js_exception_' + e.message.substring(0, 50);
        }
    })()
    '''

    LOGIN_RESULT_JS = '''
    (function() {
        let url = window.location.href;
        let pageText = (document.body.innerText || '').toLowerCase();

        // Check for checkpoint/locked
        if (url.includes('checkpoint') || url.includes('locked') ||
            url.includes('verify') || url.includes('confirm')) {
            return 'LOCKED';
        }

        // Check for 2FA
        if (url.includes('two_step') || url.includes('code_generator') ||
            document.querySelector('input[name="approvals_code"]')) {
            return '2FA';
        }

        // Check for wrong password
        let wrongPassPhrases = [
            'password that you', 'password you entered',
            'incorrect password', 'wrong password',
            'sai mật khẩu', 'mật khẩu không đúng',
            'mật khẩu bạn nhập', 'forgotten password'
        ];
        for (let phrase of wrongPassPhrases) {
            if (pageText.includes(phrase)) return 'WRONG_PASS';
        }

        // Check error elements
        let errorEl = document.querySelector('._9ay7, [data-testid="royal_login_form"] + div, .login_error_box');
        if (errorEl && errorEl.innerText.toLowerCase().includes('password')) {
            return 'WRONG_PASS';
        }

        // Check for disabled/banned
        let diePhrases = [
            'disabled', 'suspended', 'account has been disabled',
            'vô hiệu hóa', 'đã vô hiệu hóa', 'bị vô hiệu hóa',
            'đã bị khóa', 'tài khoản của bạn đã bị'
        ];
        for (let phrase of diePhrases) {
            if (pageText.includes(phrase)) return 'DIE';
        }

        // Check if logged in
        let isLoggedIn = document.querySelector('[aria-label*="Account"]') ||
                         document.querySelector('[aria-label*="Tài khoản"]') ||
                         document.querySelector('[aria-label*="Messenger"]') ||
                         document.querySelector('[aria-label*="Notifications"]') ||
                         document.querySelector('[aria-label*="Thông báo"]') ||
                         document.querySelector('div[role="navigation"]') ||
                         document.querySelector('[aria-label="Facebook"]');

        if (isLoggedIn || url === 'https://www.facebook.com/' ||
            url.includes('facebook.com/?sk=') || url.includes('facebook.com/home')) {
            return 'LIVE';
        }

        // Still on login page
        if (url.includes('/login') && document.querySelector('#pass')) {
            let hasError = document.querySelector('[role="alert"], .uiBoxRed, ._9ay7');
            if (hasError) return 'WRONG_PASS';
            return 'FAILED';
        }

        return 'UNKNOWN:' + url.substring(0, 80);
    })()
    '''

    # ============================================================
    # Methods
    # ============================================================

    def check_fb_status(self, profile_uuid: str, close_after: bool = True) -> Dict[str, Any]:
        """
        Check THỰC SỰ trạng thái FB account bằng CDP:
        1. Mở browser → 2. Navigate facebook.com → 3. Chạy JS detection → 4. Đóng
        Returns: {success, status, detail, profile_uuid}
        """
        import websocket as ws_lib

        ws = None
        remote_port = None
        short_id = profile_uuid[:20] if profile_uuid else '?'
        try:
            print(f"[CHECK] {short_id} → Opening browser...")
            result = hidemium.open_browser(profile_uuid, auto_resize=False)

            # Handle error / already open
            if result.get('type') == 'error':
                msg = result.get('message', '')
                if 'already' in msg.lower():
                    # Browser already open — try to get remote_port from data
                    data = result.get('data', {})
                    remote_port = data.get('remote_port')
                    print(f"[CHECK] {short_id} → Already open, port={remote_port}")
                else:
                    return {
                        "success": False, "status": "ERROR",
                        "detail": f"Không mở được browser: {msg}",
                        "profile_uuid": profile_uuid
                    }
            else:
                data = result.get('data', {})
                remote_port = data.get('remote_port')
                ws_url = data.get('web_socket', '')
                if not remote_port and ws_url:
                    m = re.search(r':(\d+)/', ws_url)
                    if m:
                        remote_port = int(m.group(1))

            if not remote_port:
                # Fallback: query Hidemium for running browser info
                try:
                    import requests as req
                    check = hidemium.check_profile(profile_uuid)
                    remote_port = check.get('data', {}).get('remote_port')
                    print(f"[CHECK] {short_id} → Fallback check_profile port={remote_port}")
                except:
                    pass

            if not remote_port:
                return {
                    "success": False, "status": "ERROR",
                    "detail": "Không lấy được remote_port",
                    "profile_uuid": profile_uuid
                }

            print(f"[CHECK] {short_id} → Browser ready, port={remote_port}")
            time.sleep(2)

            import requests as req
            resp = req.get(f"http://127.0.0.1:{remote_port}/json", timeout=10)
            tabs = resp.json()

            page_ws = None
            for tab in tabs:
                if tab.get('type') == 'page':
                    page_ws = tab.get('webSocketDebuggerUrl')
                    break

            if not page_ws:
                return {
                    "success": False, "status": "ERROR",
                    "detail": "Không tìm được page WebSocket",
                    "profile_uuid": profile_uuid
                }

            ws = ws_lib.create_connection(page_ws, timeout=30, suppress_origin=True)

            msg_id = [0]
            def send_cmd(method, params=None):
                msg_id[0] += 1
                msg = {"id": msg_id[0], "method": method}
                if params:
                    msg["params"] = params
                ws.send(json.dumps(msg))
                return json.loads(ws.recv())

            def evaluate(expression):
                r = send_cmd("Runtime.evaluate", {
                    "expression": expression,
                    "returnByValue": True
                })
                return r.get('result', {}).get('result', {}).get('value')

            print(f"[CHECK] {short_id} → Navigating to facebook.com...")
            send_cmd("Page.navigate", {"url": "https://www.facebook.com"})
            time.sleep(4)

            # Wait for page ready
            for _ in range(15):
                ready = evaluate('document.readyState')
                if ready == 'complete':
                    break
                time.sleep(1)

            # Extra wait for FB SPA to render content (JS-heavy)
            time.sleep(4)

            # Check if FB redirected to login page (means not logged in)
            current_url = evaluate('window.location.href') or ''
            print(f"[CHECK] {short_id} → Page loaded, URL: {current_url[:100]}")

            # If redirected to login, no need to run full JS
            if '/login' in current_url or 'login.php' in current_url:
                print(f"[CHECK] {short_id} → Redirected to login → NOT_LOGGED_IN")
                screenshot_path = self._capture_screenshot(send_cmd, profile_uuid, 'check_NOT_LOGGED_IN')
                result_data = {
                    "success": True, "status": "NOT_LOGGED_IN",
                    "detail": "redirected_to_login",
                    "profile_uuid": profile_uuid, "remote_port": remote_port
                }
                if screenshot_path:
                    result_data["screenshot"] = screenshot_path
                    result_data["screenshot_url"] = f"http://127.0.0.1:8899/screenshots/{os.path.basename(screenshot_path)}"
                return result_data

            status = 'UNKNOWN:no_result'
            for attempt in range(3):
                try:
                    raw = evaluate(self.FB_STATUS_JS)
                    status = raw if raw and isinstance(raw, str) else 'UNKNOWN:js_returned_none'
                    print(f"[CHECK] {short_id} → JS attempt {attempt+1}: {status}")
                except Exception as e:
                    status = f'ERROR:{e}'
                    print(f"[CHECK] {short_id} → JS attempt {attempt+1} exception: {e}")
                status_clean = status.split(':')[0] if ':' in str(status) else status
                if status_clean in ['LIVE', 'DIE', 'LOCKED', '2FA', 'NOT_LOGGED_IN']:
                    break
                if attempt < 2:
                    print(f"[CHECK] {short_id} → Retrying in 3s...")
                    time.sleep(3)

            status_clean = status.split(':')[0] if ':' in str(status) else status
            detail_part = status.split(':', 1)[1] if ':' in str(status) else ''

            print(f"[CHECK] {short_id} → Status: {status_clean} (detail: {detail_part or 'none'})")

            screenshot_path = self._capture_screenshot(send_cmd, profile_uuid, f'check_{status_clean}')

            result_data = {
                "success": True,
                "status": status_clean,
                "detail": detail_part if detail_part else status_clean,
                "profile_uuid": profile_uuid,
                "remote_port": remote_port
            }
            if screenshot_path:
                result_data["screenshot"] = screenshot_path
                result_data["screenshot_url"] = f"http://127.0.0.1:8899/screenshots/{os.path.basename(screenshot_path)}"

            return result_data

        except Exception as e:
            traceback.print_exc()
            print(f"[CHECK] {short_id} → EXCEPTION: {e}")
            return {
                "success": False, "status": "ERROR",
                "detail": str(e),
                "profile_uuid": profile_uuid
            }
        finally:
            if ws:
                try:
                    ws.close()
                except:
                    pass
            if close_after:
                try:
                    hidemium.close_browser(profile_uuid)
                    print(f"[CHECK] {short_id} → Browser closed")
                except:
                    pass

    def check_fb_batch(self, profile_uuids: List[str], close_after: bool = True,
                        max_workers: int = 5,
                        progress_callback=None) -> Dict[str, Any]:
        """
        Check nhiều profiles SONG SONG (parallel).
        progress_callback(checked, total, summary, last_name, last_status) called after each profile.
        Returns: {success, results, summary: {LIVE:n, DIE:n,...}}
        """
        results_map = {}
        summary = {}
        checked_count = [0]
        lock = threading.Lock()

        workers = min(max_workers, len(profile_uuids))
        total = len(profile_uuids)
        print(f"[API] check_fb_batch: {total} profiles, {workers} workers parallel")

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_uuid = {
                executor.submit(self.check_fb_status, uuid, close_after): uuid
                for uuid in profile_uuids
            }
            for future in as_completed(future_to_uuid):
                uuid = future_to_uuid[future]
                try:
                    result = future.result(timeout=120)
                except Exception as e:
                    result = {
                        "success": False, "status": "ERROR",
                        "detail": str(e), "profile_uuid": uuid
                    }
                results_map[uuid] = result
                s = result.get('status', 'ERROR')
                summary[s] = summary.get(s, 0) + 1
                with lock:
                    checked_count[0] += 1
                    count = checked_count[0]
                print(f"[API] check_fb_batch: [{count}/{total}] {uuid[:20]}... → {s}")
                # Call progress callback
                if progress_callback:
                    try:
                        progress_callback(count, total, dict(summary), uuid, s)
                    except Exception as e:
                        print(f"[API] progress_callback error: {e}")

        results = [results_map.get(uuid, {"status": "ERROR", "profile_uuid": uuid}) for uuid in profile_uuids]

        return {
            "success": True,
            "total": len(profile_uuids),
            "results": results,
            "summary": summary,
            "parallel": True,
            "workers": workers
        }

    def login_fb(self, profile_uuid: str, fb_id: str, password: str,
                 close_after: bool = False) -> Dict[str, Any]:
        """
        Đăng nhập Facebook trên profile:
        1. Mở browser → 2. Xóa cookies → 3. Navigate login → 4. Gõ email + pass → 5. Click Login → 6. Detect
        Returns: {success, status, detail, profile_uuid, fb_id}
        """
        import random
        import websocket as ws_lib

        ws = None
        remote_port = None
        try:
            result = hidemium.open_browser(profile_uuid)
            if result.get('type') == 'error':
                msg = result.get('message', 'Unknown error')
                if 'already' in msg.lower():
                    data = result.get('data', {})
                    remote_port = data.get('remote_port')
                else:
                    return {
                        "success": False, "status": "ERROR",
                        "detail": f"Không mở được browser: {msg}",
                        "profile_uuid": profile_uuid, "fb_id": fb_id
                    }

            data = result.get('data', {})
            if not remote_port:
                remote_port = data.get('remote_port')
            ws_url = data.get('web_socket', '')

            if not remote_port and ws_url:
                match = re.search(r':(\d+)/', ws_url)
                if match:
                    remote_port = int(match.group(1))

            if not remote_port:
                return {
                    "success": False, "status": "ERROR",
                    "detail": "Không lấy được remote_port",
                    "profile_uuid": profile_uuid, "fb_id": fb_id
                }

            time.sleep(2)

            import requests as req
            resp = req.get(f"http://127.0.0.1:{remote_port}/json", timeout=10)
            tabs = resp.json()

            page_ws = None
            for tab in tabs:
                if tab.get('type') == 'page':
                    page_ws = tab.get('webSocketDebuggerUrl')
                    break

            if not page_ws:
                return {
                    "success": False, "status": "ERROR",
                    "detail": "Không tìm được page WebSocket",
                    "profile_uuid": profile_uuid, "fb_id": fb_id
                }

            ws = ws_lib.create_connection(page_ws, timeout=30, suppress_origin=True)

            msg_id = [0]
            def send_cmd(method, params=None):
                msg_id[0] += 1
                msg = {"id": msg_id[0], "method": method}
                if params:
                    msg["params"] = params
                ws.send(json.dumps(msg))
                return json.loads(ws.recv())

            def evaluate(expression):
                r = send_cmd("Runtime.evaluate", {
                    "expression": expression,
                    "returnByValue": True
                })
                return r.get('result', {}).get('result', {}).get('value')

            def type_text(text, field_selector):
                """Gõ từng ký tự như người thật"""
                evaluate(f'''
                    (function() {{
                        let el = document.querySelector('{field_selector}');
                        if (el) {{ el.focus(); el.value = ''; }}
                    }})()
                ''')
                time.sleep(random.uniform(0.2, 0.5))
                for char in text:
                    send_cmd("Input.insertText", {"text": char})
                    time.sleep(random.uniform(0.10, 0.28))

            # Xóa cookies Facebook cũ
            for domain in [".facebook.com", "facebook.com", "www.facebook.com"]:
                send_cmd("Network.deleteCookies", {"domain": domain})
            for origin in ["https://www.facebook.com", "https://m.facebook.com"]:
                send_cmd("Storage.clearDataForOrigin", {"origin": origin, "storageTypes": "all"})

            send_cmd("Page.navigate", {"url": "https://www.facebook.com/login"})
            time.sleep(3)

            for _ in range(10):
                ready = evaluate('document.readyState')
                if ready == 'complete':
                    break
                time.sleep(1)

            # Xử lý "Không phải bạn?"
            not_you = evaluate('''
                (function() {
                    let els = document.querySelectorAll('a, div[role="button"], span, button');
                    for (let el of els) {
                        let text = (el.innerText || el.textContent || '').trim();
                        if (text.includes('Không phải bạn') || text.includes('Not you') ||
                            text.includes('Log in with another') || text.includes('Đăng nhập bằng tài khoản khác')) {
                            el.click();
                            return 'clicked';
                        }
                    }
                    return 'no_session';
                })()
            ''')
            if not_you and 'clicked' in str(not_you):
                time.sleep(2)

            form_check = evaluate('''
                (function() {
                    let email = document.querySelector('#email');
                    let pass = document.querySelector('#pass');
                    return email && pass ? 'OK' : 'NO_FORM';
                })()
            ''')

            if form_check != 'OK':
                return {
                    "success": False, "status": "ERROR",
                    "detail": "Không tìm thấy form đăng nhập",
                    "profile_uuid": profile_uuid, "fb_id": fb_id
                }

            type_text(fb_id, "#email")
            time.sleep(random.uniform(0.3, 0.7))
            type_text(password, "#pass")
            time.sleep(random.uniform(0.5, 1.0))

            login_click = evaluate('''
                (function() {
                    let loginBtn = document.querySelector('#loginbutton') ||
                                   document.querySelector('button[name="login"]') ||
                                   document.querySelector('button[type="submit"]');
                    if (loginBtn) { loginBtn.click(); return 'CLICKED'; }
                    let form = document.querySelector('#email')?.closest('form');
                    if (form) { form.submit(); return 'SUBMITTED'; }
                    return 'NO_BTN';
                })()
            ''')

            if login_click not in ['CLICKED', 'SUBMITTED']:
                return {
                    "success": False, "status": "ERROR",
                    "detail": "Không tìm được nút Login",
                    "profile_uuid": profile_uuid, "fb_id": fb_id
                }

            time.sleep(3)
            for _ in range(10):
                ready = evaluate('document.readyState')
                if ready == 'complete':
                    break
                time.sleep(1)
            time.sleep(2)

            status = 'UNKNOWN'
            for attempt in range(3):
                status = evaluate(self.LOGIN_RESULT_JS) or 'UNKNOWN'
                status_clean = status.split(':')[0] if ':' in str(status) else status
                if status_clean in ['LIVE', 'DIE', 'WRONG_PASS', 'LOCKED', '2FA']:
                    break
                if attempt < 2:
                    time.sleep(2)

            status_clean = status.split(':')[0] if ':' in str(status) else status
            detail_part = status.split(':', 1)[1] if ':' in str(status) else ''

            screenshot_path = self._capture_screenshot(send_cmd, profile_uuid, f'login_{status_clean}')

            result_data = {
                "success": True,
                "status": status_clean,
                "detail": detail_part if detail_part else status_clean,
                "profile_uuid": profile_uuid,
                "fb_id": fb_id,
                "remote_port": remote_port
            }
            if screenshot_path:
                result_data["screenshot"] = screenshot_path
                result_data["screenshot_url"] = f"http://127.0.0.1:8899/screenshots/{os.path.basename(screenshot_path)}"

            return result_data

        except Exception as e:
            traceback.print_exc()
            return {
                "success": False, "status": "ERROR",
                "detail": str(e),
                "profile_uuid": profile_uuid, "fb_id": fb_id
            }
        finally:
            if ws:
                try:
                    ws.close()
                except:
                    pass
            if close_after:
                try:
                    hidemium.close_browser(profile_uuid)
                except:
                    pass

    def login_fb_try_accounts(self, profile_uuid: str, accounts: List[Dict[str, str]],
                               close_after: bool = False) -> Dict[str, Any]:
        """
        Thử login lần lượt các account cho đến khi LIVE.
        accounts = [{"fb_id": "...", "password": "..."}, ...]
        """
        results = []
        for i, acc in enumerate(accounts):
            fb_id = acc.get('fb_id', '')
            password = acc.get('password', '')

            if not fb_id or not password:
                continue

            result = self.login_fb(profile_uuid, fb_id, password, close_after=False)
            results.append(result)
            status = result.get('status', 'ERROR')

            if status == 'LIVE':
                if close_after:
                    try: hidemium.close_browser(profile_uuid)
                    except: pass
                return {
                    "success": True,
                    "status": "LIVE",
                    "logged_in_with": fb_id,
                    "tried": i + 1,
                    "total_accounts": len(accounts),
                    "results": results,
                    "profile_uuid": profile_uuid
                }
            elif status in ['LOCKED', '2FA', 'WRONG_PASS', 'DIE']:
                try: hidemium.close_browser(profile_uuid)
                except: pass
                time.sleep(2)
                continue
            else:
                try: hidemium.close_browser(profile_uuid)
                except: pass
                time.sleep(2)
                continue

        if close_after:
            try: hidemium.close_browser(profile_uuid)
            except: pass

        return {
            "success": False,
            "status": "ALL_FAILED",
            "detail": f"Đã thử {len(results)} accounts, không có cái nào LIVE",
            "tried": len(results),
            "total_accounts": len(accounts),
            "results": results,
            "profile_uuid": profile_uuid
        }

    def check_login(self, profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """
        Quick login check trên trang HIỆN TẠI (không navigate).
        Returns: {logged_in: bool, status, detail, url}
        """
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "logged_in": False, "error": "No active connection. Call /open_browser first."}

        try:
            current_url = cdp._evaluate_js("window.location.href") or ""

            status = None
            for attempt in range(2):
                try:
                    status = cdp._evaluate_js(self.FB_STATUS_JS)
                except:
                    status = 'ERROR'
                if status and str(status).startswith(('LIVE', 'NOT_LOGGED_IN', 'DIE', 'LOCKED', '2FA')):
                    break
                time.sleep(1)

            status_str = str(status) if status else 'UNKNOWN'
            status_clean = status_str.split(':')[0]
            detail = status_str.split(':', 1)[1] if ':' in status_str else ''
            logged_in = status_clean == 'LIVE'

            return {
                "success": True,
                "logged_in": logged_in,
                "status": status_clean,
                "detail": detail,
                "url": str(current_url)[:100]
            }
        except Exception as e:
            return {"success": False, "logged_in": False, "error": str(e)}
