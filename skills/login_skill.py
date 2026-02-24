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
                cur_id = msg_id[0]
                msg = {"id": cur_id, "method": method}
                if params:
                    msg["params"] = params
                ws.send(json.dumps(msg))
                for _ in range(200):
                    raw = ws.recv()
                    resp = json.loads(raw)
                    if resp.get("id") == cur_id:
                        return resp
                return {}

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

            # Extract c_user cookie (FB UID) via CDP
            fb_uid = None
            try:
                cookies_resp = send_cmd("Network.getCookies", {"urls": ["https://www.facebook.com"]})
                cookies = cookies_resp.get('result', {}).get('cookies', [])
                for ck in cookies:
                    if ck.get('name') == 'c_user':
                        fb_uid = ck.get('value')
                        print(f"[CHECK] {short_id} → c_user (FB UID): {fb_uid}")
                        break
            except Exception as ce:
                print(f"[CHECK] {short_id} → Cookie extraction failed: {ce}")

            result_data = {
                "success": True,
                "status": status_clean,
                "detail": detail_part if detail_part else status_clean,
                "profile_uuid": profile_uuid,
                "remote_port": remote_port
            }
            if fb_uid:
                result_data["fb_uid"] = fb_uid
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
            # Graceful shutdown via CDP Browser.close() to flush cookies
            if ws and close_after:
                try:
                    msg_id[0] += 1
                    ws.send(json.dumps({"id": msg_id[0], "method": "Browser.close"}))
                    print(f"[CHECK] {short_id} → Sent Browser.close()")
                    time.sleep(3)
                except Exception:
                    pass
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
                        progress_callback=None,
                        cancel_event=None) -> Dict[str, Any]:
        """
        Check nhiều profiles SONG SONG (parallel).
        progress_callback(checked, total, summary, last_name, last_status) called after each profile.
        cancel_event: threading.Event — if set, stop processing and cancel remaining futures.
        Returns: {success, results, summary: {LIVE:n, DIE:n,...}}
        """
        results_map = {}
        summary = {}
        checked_count = [0]
        lock = threading.Lock()
        cancelled = False

        workers = min(max_workers, len(profile_uuids))
        total = len(profile_uuids)
        print(f"[API] check_fb_batch: {total} profiles, {workers} workers parallel")

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_uuid = {
                executor.submit(self.check_fb_status, uuid, close_after): uuid
                for uuid in profile_uuids
            }
            for future in as_completed(future_to_uuid):
                # Check cancel signal BEFORE processing result
                if cancel_event and cancel_event.is_set():
                    print(f"[API] check_fb_batch: CANCEL signal received — stopping")
                    cancelled = True
                    # Cancel remaining futures
                    for f in future_to_uuid:
                        if not f.done():
                            f.cancel()
                    break

                uuid = future_to_uuid[future]
                try:
                    result = future.result(timeout=180)
                except Exception as e:
                    result = {
                        "success": False, "status": "ERROR",
                        "detail": str(e), "profile_uuid": uuid
                    }
                results_map[uuid] = result
                s = result.get('status', 'ERROR')
                with lock:
                    summary[s] = summary.get(s, 0) + 1
                    checked_count[0] += 1
                    count = checked_count[0]
                print(f"[API] check_fb_batch: [{count}/{total}] {uuid[:20]}... → {s}")
                # Call progress callback with detail
                detail = result.get('detail', '')
                if progress_callback:
                    try:
                        progress_callback(count, total, dict(summary), uuid, s, detail)
                    except Exception as e:
                        print(f"[API] progress_callback error: {e}")

        results = [results_map.get(uuid, {"status": "ERROR", "profile_uuid": uuid}) for uuid in profile_uuids]

        return {
            "success": True,
            "total": len(profile_uuids),
            "checked": len(results_map),
            "cancelled": cancelled,
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
            print(f"[LOGIN] Opening browser for {profile_uuid[:20]}... fb_id={fb_id}")
            result = hidemium.open_browser(profile_uuid)
            print(f"[LOGIN] open_browser result: {result}")
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
                cur_id = msg_id[0]
                msg = {"id": cur_id, "method": method}
                if params:
                    msg["params"] = params
                ws.send(json.dumps(msg))
                for _ in range(200):
                    raw = ws.recv()
                    resp = json.loads(raw)
                    if resp.get("id") == cur_id:
                        return resp
                return {}

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

            # Xóa cookies + storage Facebook cũ trước khi login
            # Dùng Storage.clearDataForOrigin để xóa cookies theo origin (đúng spec CDP)
            for origin in ["https://www.facebook.com", "https://m.facebook.com"]:
                send_cmd("Storage.clearDataForOrigin", {"origin": origin, "storageTypes": "cookies,local_storage,session_storage,indexeddb"})

            print(f"[LOGIN] Navigating to facebook.com/login...")
            nav_result = send_cmd("Page.navigate", {"url": "https://www.facebook.com/login"})
            print(f"[LOGIN] Navigate result: {nav_result}")
            time.sleep(4)

            for i in range(15):
                ready = evaluate('document.readyState')
                print(f"[LOGIN] readyState attempt {i}: {ready}")
                if ready == 'complete':
                    break
                time.sleep(1)

            # ========== PHASE 1: CHECK DOM/HTML — tìm selector động ==========
            # Lấy HTML thực tế của trang để phân tích, không hardcode selector
            dom_info = None
            email_sel = None
            pass_sel = None
            btn_sel = None

            for _dom_retry in range(8):
                dom_info = evaluate(r'''
                    (function() {
                        var url = window.location.href || '';
                        var title = document.title || '';
                        var html = document.documentElement.outerHTML || '';

                        // Tìm tất cả input fields trên trang
                        var inputs = document.querySelectorAll('input');
                        var inputDetails = [];
                        for (var i = 0; i < inputs.length; i++) {
                            var inp = inputs[i];
                            var rect = inp.getBoundingClientRect();
                            inputDetails.push({
                                tag: 'input',
                                id: inp.id || '',
                                name: inp.name || '',
                                type: (inp.type || 'text').toLowerCase(),
                                placeholder: inp.placeholder || '',
                                ariaLabel: inp.getAttribute('aria-label') || '',
                                className: inp.className || '',
                                visible: rect.width > 0 && rect.height > 0,
                                dataTestid: inp.getAttribute('data-testid') || ''
                            });
                        }

                        // Tìm tất cả buttons
                        var btns = document.querySelectorAll('button, input[type="submit"], div[role="button"]');
                        var btnDetails = [];
                        for (var j = 0; j < btns.length; j++) {
                            var b = btns[j];
                            var brect = b.getBoundingClientRect();
                            btnDetails.push({
                                tag: b.tagName.toLowerCase(),
                                id: b.id || '',
                                name: b.name || '',
                                type: (b.type || '').toLowerCase(),
                                text: (b.innerText || b.textContent || b.value || '').trim().substring(0, 50),
                                ariaLabel: b.getAttribute('aria-label') || '',
                                dataTestid: b.getAttribute('data-testid') || '',
                                visible: brect.width > 0 && brect.height > 0
                            });
                        }

                        // Xử lý "Không phải bạn?" / "Not you?"
                        var notYou = false;
                        var links = document.querySelectorAll('a, div[role="button"], span, button');
                        for (var k = 0; k < links.length; k++) {
                            var txt = (links[k].innerText || links[k].textContent || '').trim();
                            if (txt.indexOf('Kh\u00f4ng ph\u1ea3i b\u1ea1n') >= 0 || txt.indexOf('Not you') >= 0 ||
                                txt.indexOf('Log in with another') >= 0 || txt.indexOf('\u0110\u0103ng nh\u1eadp b\u1eb1ng t\u00e0i kho\u1ea3n kh\u00e1c') >= 0) {
                                links[k].click();
                                notYou = true;
                                break;
                            }
                        }

                        return JSON.stringify({
                            url: url,
                            title: title,
                            htmlLen: html.length,
                            inputCount: inputs.length,
                            inputs: inputDetails,
                            btnCount: btns.length,
                            buttons: btnDetails,
                            notYouClicked: notYou
                        });
                    })()
                ''')
                print(f"[LOGIN] DOM check attempt {_dom_retry}: {str(dom_info)[:500]}")

                if not dom_info:
                    time.sleep(2)
                    continue

                try:
                    dom = json.loads(dom_info)
                except Exception:
                    time.sleep(2)
                    continue

                # Nếu click "Không phải bạn" thì đợi trang reload
                if dom.get('notYouClicked'):
                    print("[LOGIN] Clicked 'Not you' button, waiting for reload...")
                    time.sleep(3)
                    continue

                inputs_list = dom.get('inputs', [])
                buttons_list = dom.get('buttons', [])

                print(f"[LOGIN] URL: {dom.get('url')}, Title: {dom.get('title')}, "
                      f"Inputs: {len(inputs_list)}, Buttons: {len(buttons_list)}")

                # ---- Tìm email/uid field ----
                for inp in inputs_list:
                    if not inp.get('visible'):
                        continue
                    # Ưu tiên: id='email', name='email', type='email'
                    if inp.get('id') == 'email' or inp.get('name') == 'email':
                        email_sel = '#email' if inp.get('id') == 'email' else 'input[name="email"]'
                        break
                    if inp.get('type') == 'email':
                        if inp.get('id'):
                            email_sel = f"#{inp['id']}"
                        elif inp.get('name'):
                            email_sel = f"input[name=\"{inp['name']}\"]"
                        else:
                            email_sel = 'input[type="email"]'
                        break
                    # data-testid chứa 'email' hoặc 'username'
                    dt = inp.get('dataTestid', '').lower()
                    if 'email' in dt or 'username' in dt or 'login' in dt:
                        if inp.get('id'):
                            email_sel = f"#{inp['id']}"
                        else:
                            email_sel = f'input[data-testid="{inp["dataTestid"]}"]'
                        break
                    # placeholder / aria-label chứa email/phone/số điện thoại
                    ph = (inp.get('placeholder', '') + ' ' + inp.get('ariaLabel', '')).lower()
                    if any(kw in ph for kw in ['email', 'phone', 'mobile', 'username',
                                                'so dien thoai', 'dia chi email',
                                                'email address', 'số điện thoại']):
                        if inp.get('id'):
                            email_sel = f"#{inp['id']}"
                        elif inp.get('name'):
                            email_sel = f"input[name=\"{inp['name']}\"]"
                        else:
                            email_sel = f"input[placeholder=\"{inp.get('placeholder')}\"]"
                        break

                # Nếu chưa tìm được, fallback: input[type=text] visible đầu tiên
                if not email_sel:
                    for inp in inputs_list:
                        if inp.get('visible') and inp.get('type') in ('text', 'tel', ''):
                            if inp.get('id'):
                                email_sel = f"#{inp['id']}"
                            elif inp.get('name'):
                                email_sel = f"input[name=\"{inp['name']}\"]"
                            else:
                                email_sel = f"input[type=\"{inp.get('type', 'text')}\"]"
                            break

                # ---- Tìm password field ----
                for inp in inputs_list:
                    if not inp.get('visible'):
                        continue
                    if inp.get('id') == 'pass' or inp.get('name') == 'pass':
                        pass_sel = '#pass' if inp.get('id') == 'pass' else 'input[name="pass"]'
                        break
                    if inp.get('type') == 'password':
                        if inp.get('id'):
                            pass_sel = f"#{inp['id']}"
                        elif inp.get('name'):
                            pass_sel = f"input[name=\"{inp['name']}\"]"
                        else:
                            pass_sel = 'input[type="password"]'
                        break

                # ---- Tìm login button ----
                for btn in buttons_list:
                    if not btn.get('visible'):
                        continue
                    # id='loginbutton' hoặc name='login'
                    if btn.get('id') == 'loginbutton':
                        btn_sel = '#loginbutton'
                        break
                    if btn.get('name') == 'login':
                        btn_sel = 'button[name="login"]'
                        break
                    # data-testid chứa 'login'
                    dt = btn.get('dataTestid', '').lower()
                    if 'login' in dt or 'submit' in dt:
                        btn_sel = f'[data-testid="{btn["dataTestid"]}"]'
                        break
                    # text chứa 'Log in', 'Đăng nhập', 'Sign in'
                    txt = btn.get('text', '').lower()
                    if any(kw in txt for kw in ['log in', 'login', 'dang nhap',
                                                 'đăng nhập', 'sign in']):
                        if btn.get('id'):
                            btn_sel = f"#{btn['id']}"
                        elif btn.get('name'):
                            btn_sel = f"button[name=\"{btn['name']}\"]"
                        elif btn.get('dataTestid'):
                            btn_sel = f'[data-testid="{btn["dataTestid"]}"]'
                        else:
                            # Dùng text match
                            btn_sel = f'__TEXT_MATCH__:{txt}'
                        break

                # Nếu chưa tìm btn, fallback: button[type=submit]
                if not btn_sel:
                    for btn in buttons_list:
                        if btn.get('visible') and btn.get('type') == 'submit':
                            if btn.get('id'):
                                btn_sel = f"#{btn['id']}"
                            else:
                                btn_sel = 'button[type="submit"]'
                            break

                print(f"[LOGIN] Detected selectors: email={email_sel}, pass={pass_sel}, btn={btn_sel}")

                if email_sel and pass_sel:
                    break  # Tìm được cả 2 field → tiến hành login
                else:
                    # Reset cho retry tiếp
                    email_sel = None
                    pass_sel = None
                    btn_sel = None
                    time.sleep(2)

            # ========== PHASE 2: Kiểm tra kết quả DOM check ==========
            if not email_sel or not pass_sel:
                # Lấy thêm HTML snippet để debug
                html_snippet = evaluate('document.documentElement.outerHTML.substring(0, 3000)') or 'N/A'
                print(f"[LOGIN] FAILED - No form found. HTML snippet: {str(html_snippet)[:1000]}")
                return {
                    "success": False, "status": "ERROR",
                    "detail": f"Khong tim thay form dang nhap. URL={dom.get('url','?')}, "
                              f"inputs={dom.get('inputCount',0)}, title={dom.get('title','?')}",
                    "profile_uuid": profile_uuid, "fb_id": fb_id
                }

            # ========== PHASE 3: Nhập UID + Password dùng selector động ==========
            print(f"[LOGIN] Typing fb_id into {email_sel}...")
            type_text(fb_id, email_sel)
            time.sleep(random.uniform(0.3, 0.7))

            print(f"[LOGIN] Typing password into {pass_sel}...")
            type_text(password, pass_sel)
            time.sleep(random.uniform(0.5, 1.0))

            # ========== PHASE 4: Click login button ==========
            if btn_sel and btn_sel.startswith('__TEXT_MATCH__:'):
                # Text-based button match
                match_text = btn_sel.split(':', 1)[1]
                login_click = evaluate(f'''
                    (function() {{
                        var btns = document.querySelectorAll('button, div[role="button"], input[type="submit"]');
                        for (var i = 0; i < btns.length; i++) {{
                            var t = (btns[i].innerText || btns[i].textContent || btns[i].value || '').trim().toLowerCase();
                            if (t.indexOf("{match_text}") >= 0) {{
                                btns[i].click();
                                return 'CLICKED';
                            }}
                        }}
                        return 'NO_BTN';
                    }})()
                ''')
            elif btn_sel:
                login_click = evaluate(f'''
                    (function() {{
                        var btn = document.querySelector('{btn_sel}');
                        if (btn) {{ btn.click(); return 'CLICKED'; }}
                        return 'NO_BTN';
                    }})()
                ''')
            else:
                # Fallback: submit form chứa email field
                login_click = evaluate(f'''
                    (function() {{
                        var emailEl = document.querySelector('{email_sel}');
                        if (emailEl) {{
                            var form = emailEl.closest('form');
                            if (form) {{ form.submit(); return 'SUBMITTED'; }}
                        }}
                        // Last resort: press Enter
                        var passEl = document.querySelector('{pass_sel}');
                        if (passEl) {{
                            passEl.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter', code:'Enter', keyCode:13, bubbles:true}}));
                            passEl.dispatchEvent(new KeyboardEvent('keyup', {{key:'Enter', code:'Enter', keyCode:13, bubbles:true}}));
                            return 'ENTER_PRESSED';
                        }}
                        return 'NO_BTN';
                    }})()
                ''')

            print(f"[LOGIN] Login click result: {login_click}")

            if login_click not in ['CLICKED', 'SUBMITTED', 'ENTER_PRESSED']:
                return {
                    "success": False, "status": "ERROR",
                    "detail": "Khong tim duoc nut Login",
                    "profile_uuid": profile_uuid, "fb_id": fb_id
                }

            time.sleep(3)
            for _ in range(10):
                ready = evaluate('document.readyState')
                if ready == 'complete':
                    break
                time.sleep(1)
            # Extra wait for FB SPA to render after login redirect
            time.sleep(4)

            status = 'UNKNOWN'
            for attempt in range(4):
                status = evaluate(self.LOGIN_RESULT_JS) or 'UNKNOWN'
                status_clean = status.split(':')[0] if ':' in str(status) else status
                if status_clean in ['LIVE', 'DIE', 'WRONG_PASS', 'LOCKED', '2FA', 'FAILED']:
                    break
                # Fallback: check URL + no login form = LIVE (same logic as FB_STATUS_JS) 
                if status_clean == 'UNKNOWN':
                    url_check = evaluate('window.location.href') or ''
                    has_login_form = evaluate('!!(document.querySelector("#email") || document.querySelector("#pass"))') or False
                    if ('facebook.com' in url_check and '/login' not in url_check 
                        and 'login.php' not in url_check and not has_login_form):
                        status = 'LIVE'
                        status_clean = 'LIVE'
                        print(f"[LOGIN] Fallback LIVE detection: URL={url_check[:80]}, no login form")
                        break
                if attempt < 3:
                    time.sleep(3)

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

            # ===== VERIFY + PERSIST COOKIES =====
            if status_clean in ('LIVE', 'LOGGED_IN'):
                try:
                    # Lấy cookies hiện tại để verify
                    cookies_resp = send_cmd("Network.getCookies", {"urls": ["https://www.facebook.com"]})
                    cookies = cookies_resp.get('result', {}).get('cookies', [])
                    has_cuser = any(c.get('name') == 'c_user' for c in cookies)
                    print(f"[LOGIN] Cookies after login: {len(cookies)} total, c_user={'YES' if has_cuser else 'NO'}")
                    if not has_cuser:
                        print(f"[LOGIN] WARNING: No c_user cookie found after LIVE status!")
                    
                    # Re-set critical cookies via CDP with explicit persistence
                    # This forces Chrome to write them to its cookie store
                    critical_names = {'c_user', 'xs', 'fr', 'datr', 'sb', 'wd', 'presence'}
                    for ck in cookies:
                        if ck.get('name') in critical_names:
                            send_cmd("Network.setCookie", {
                                "name": ck['name'],
                                "value": ck['value'],
                                "domain": ck.get('domain', '.facebook.com'),
                                "path": ck.get('path', '/'),
                                "httpOnly": ck.get('httpOnly', False),
                                "secure": ck.get('secure', True),
                                "expires": ck.get('expires', time.time() + 86400 * 365),
                                "sameSite": ck.get('sameSite', 'None')
                            })
                    print(f"[LOGIN] Re-set critical cookies for persistence")
                except Exception as cookie_err:
                    print(f"[LOGIN] Cookie persist error: {cookie_err}")

            return result_data

        except Exception as e:
            traceback.print_exc()
            return {
                "success": False, "status": "ERROR",
                "detail": str(e),
                "profile_uuid": profile_uuid, "fb_id": fb_id
            }
        finally:
            # === GRACEFUL SHUTDOWN ===
            # CRITICAL: Hidemium's /closeProfile force-kills Chrome process,
            # preventing cookies from being flushed to disk.
            # Browser.close() tells Chrome to gracefully save ALL data first.
            if ws and close_after:
                try:
                    # Send Browser.close() - Chrome will flush cookies/storage
                    # to disk and then exit gracefully
                    msg_id[0] += 1
                    ws.send(json.dumps({"id": msg_id[0], "method": "Browser.close"}))
                    print(f"[LOGIN] Sent Browser.close() for graceful cookie flush")
                    time.sleep(5)  # Wait for Chrome to finish saving and exit
                except Exception as bc_err:
                    print(f"[LOGIN] Browser.close() note: {bc_err}")
            if ws:
                try:
                    ws.close()
                except:
                    pass
            if close_after:
                try:
                    # Cleanup: tell Hidemium to update internal state
                    # (Chrome already exited via Browser.close())
                    hidemium.close_browser(profile_uuid)
                    print(f"[LOGIN] Hidemium cleanup done for {profile_uuid[:20]}")
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
