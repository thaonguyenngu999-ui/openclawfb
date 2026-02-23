"""
FB Manager Pro - API Server (thin HTTP layer)
All business logic lives in skills/ package.
This file only contains the HTTP handler + routing + server startup.
"""

import json
import os
import threading
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse
from skills import FBManagerAPI, SCREENSHOT_DIR


# HTTP Handler
class APIHandler(BaseHTTPRequestHandler):
    fb_api = FBManagerAPI()

    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/health':
            self._send_json({"status": "ok", "service": "fb-manager-pro"})
        elif path == '/status':
            self._send_json({
                "current_profile": self.fb_api.current_profile,
                "active_connections": list(self.fb_api.connections.keys())
            })
        elif path.startswith('/screenshots/'):
            filename = path.split('/screenshots/')[-1]
            filename = os.path.basename(filename)
            filepath = os.path.join(SCREENSHOT_DIR, filename)

            if os.path.exists(filepath) and filepath.endswith('.png'):
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(os.path.getsize(filepath)))
                self.end_headers()
                with open(filepath, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self._send_json({"error": "Screenshot not found"}, 404)
        elif path == '/screenshots':
            files = sorted(os.listdir(SCREENSHOT_DIR), reverse=True)[:50]
            screenshots = []
            for f in files:
                if f.endswith('.png'):
                    screenshots.append({
                        "filename": f,
                        "url": f"http://127.0.0.1:8899/screenshots/{f}",
                        "size": os.path.getsize(os.path.join(SCREENSHOT_DIR, f))
                    })
            self._send_json({"screenshots": screenshots, "total": len(screenshots)})
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        raw_body = self.rfile.read(content_length) if content_length else b'{}'

        try:
            body = raw_body.decode('utf-8')
        except UnicodeDecodeError:
            body = raw_body.decode('latin-1')

        try:
            params = json.loads(body) if body else {}
        except:
            params = {}

        parsed = urlparse(self.path)
        path = parsed.path

        # Endpoints that manage their own concurrency
        no_lock_paths = {'/list_profiles', '/list_folders', '/wait', '/send_telegram_photo',
                         '/check_fb_batch', '/check_fb_status',
                         '/fb_nurture_batch', '/vision_skills', '/vision_clear_skills',
                         '/agent_execute', '/agent_skills', '/agent_task_history',
                         '/agent_skill_delete', '/delete_profiles',
                         '/list_tags', '/list_scripts', '/list_campaigns',
                         '/get_profile_detail', '/get_versions', '/get_running',
                         '/get_default_configs', '/get_schedules', '/get_user_info',
                         '/create_profile', '/update_profile_name', '/update_profile_note',
                         '/update_proxy', '/remove_proxy', '/change_fingerprint',
                         '/change_status', '/add_to_folder', '/sync_tags',
                         '/create_schedule'}

        try:
            if path in no_lock_paths:
                result = self._handle_post(path, params)
            else:
                profile_uuid = params.get('profile_uuid') or params.get('profile')
                lock = self.fb_api._get_profile_lock(profile_uuid)
                with lock:
                    result = self._handle_post(path, params)
        except Exception as e:
            traceback.print_exc()
            result = {"success": False, "error": f"Server error: {e}"}

        self._send_json(result)

    def _handle_post(self, path: str, params: dict) -> dict:
        # Auto-resolve profile name → UUID
        for key in ('profile_uuid', 'profile'):
            val = params.get(key)
            if val:
                params[key] = self.fb_api.resolve_profile(val)

        if path == '/open_browser':
            profile_uuid = params.get('profile_uuid') or params.get('profile')
            if not profile_uuid:
                return {"error": "profile_uuid required"}
            return self.fb_api.open_browser(profile_uuid)

        elif path == '/navigate':
            url = params.get('url')
            if not url:
                return {"error": "url required"}
            return self.fb_api.navigate(url, params.get('profile'))

        elif path == '/scroll':
            amount = params.get('amount', 500)
            return self.fb_api.scroll(amount, params.get('profile'))

        elif path == '/get_dom':
            selector = params.get('selector', 'body')
            return self.fb_api.get_dom(selector, params.get('profile'))

        elif path == '/execute_js':
            code = params.get('code')
            if not code:
                return {"error": "code required"}
            return self.fb_api.execute_js(code, params.get('profile'))

        elif path == '/like':
            return self.fb_api.like_post(params.get('profile'))

        elif path == '/check':
            selector = params.get('selector')
            if not selector:
                return {"error": "selector required"}
            return self.fb_api.check_result(selector, params.get('expected'), params.get('profile'))

        elif path == '/close':
            return self.fb_api.close_browser(params.get('profile'))

        elif path == '/delete_profiles':
            uuids = params.get('uuids') or params.get('profile_uuids', [])
            if not uuids:
                return {"error": "uuids required (list of profile UUIDs)"}
            if isinstance(uuids, str):
                uuids = [uuids]
            # Resolve names → UUIDs
            resolved = []
            for u in uuids:
                r = self.fb_api.resolve_profile(u)
                if r:
                    resolved.append(r)
            if not resolved:
                return {"error": "No valid profiles to delete"}
            # Delete from Hidemium
            from api_service import api as hidemium
            result = hidemium.delete_profiles(resolved, is_local=True)
            # Also delete from local DB
            import db as _db
            for uuid in resolved:
                _db.delete_profile(uuid)
            return {"success": True, "deleted": len(resolved), "uuids": resolved, "hidemium_result": result}

        elif path == '/check_login':
            return self.fb_api.check_login(params.get('profile'))

        # ===== GENERIC CDP CONTROL =====
        elif path == '/click_element':
            selector = params.get('selector')
            if not selector:
                return {"error": "selector required"}
            return self.fb_api.click_element(selector, params.get('profile'))

        elif path == '/type_text':
            selector = params.get('selector')
            text = params.get('text')
            if not selector or not text:
                return {"error": "selector and text required"}
            human_like = params.get('human_like', True)
            return self.fb_api.type_text_into(selector, text, human_like=human_like,
                                               profile_uuid=params.get('profile'))

        elif path == '/press_key':
            key = params.get('key')
            if not key:
                return {"error": "key required (Enter, Tab, Escape, Backspace, ArrowDown, ArrowUp, Space)"}
            return self.fb_api.press_key(key, params.get('profile'))

        elif path == '/get_page_text':
            return self.fb_api.get_page_text(params.get('profile'))

        elif path == '/wait':
            seconds = params.get('seconds', 2)
            return self.fb_api.wait_seconds(float(seconds))

        # ===== FACEBOOK BROWSE & COMMENT =====
        elif path == '/fb_read_feed':
            scroll_count = params.get('scroll_count', 3)
            return self.fb_api.fb_read_feed(params.get('profile'), scroll_count=int(scroll_count))

        elif path == '/fb_comment':
            post_index = params.get('post_index')
            comment_text = params.get('comment') or params.get('text')
            if post_index is None or not comment_text:
                return {"error": "post_index and comment (text) required"}
            return self.fb_api.fb_comment(int(post_index), comment_text, params.get('profile'))

        # ===== CHECK FB STATUS =====
        elif path == '/check_fb_status':
            profile_uuid = params.get('profile_uuid') or params.get('profile')
            if not profile_uuid:
                return {"error": "profile_uuid required"}
            close_after = params.get('close_after', True)
            return self.fb_api.check_fb_status(profile_uuid, close_after=close_after)

        elif path == '/check_fb_batch':
            profile_uuids = params.get('profile_uuids') or params.get('profiles', [])
            if not profile_uuids:
                return {"error": "profile_uuids required (list of uuids or names)"}
            # Auto-resolve names → UUIDs
            resolved = []
            name_map = {}  # uuid → original name
            for p in profile_uuids:
                uuid = self.fb_api.resolve_profile(p)
                resolved.append(uuid)
                name_map[uuid] = p
            close_after = params.get('close_after', True)
            max_workers = int(params.get('max_workers', 5))

            # Telegram progress callback
            tg_chat_id = params.get('telegram_chat_id')
            tg_msg_id = params.get('telegram_message_id')
            tg_token = params.get('telegram_token')
            progress_cb = None
            if tg_chat_id and tg_msg_id and tg_token:
                import requests as tg_req
                last_update = [0]
                def progress_cb(checked, total, summary, last_uuid, last_status):
                    import time as t
                    now = t.time()
                    # Throttle: update every 3 seconds
                    if now - last_update[0] < 3 and checked < total:
                        return
                    last_update[0] = now
                    name = name_map.get(last_uuid, last_uuid[:20])
                    live = summary.get('LIVE', 0)
                    die = summary.get('DIE', 0) + summary.get('NOT_LOGGED_IN', 0)
                    err = summary.get('ERROR', 0)
                    locked = summary.get('LOCKED', 0) + summary.get('2FA', 0)
                    status_icon = {'LIVE': '✅', 'DIE': '❌', 'ERROR': '⚠️',
                                   'LOCKED': '🔒', '2FA': '🔒', 'NOT_LOGGED_IN': '❌'}.get(last_status, '❓')
                    text = (f"🔍 Checking... {checked}/{total}\n"
                            f"{status_icon} {name} → {last_status}\n\n"
                            f"✅ Live: {live}  ❌ Die: {die}")
                    if locked:
                        text += f"  🔒 Lock: {locked}"
                    if err:
                        text += f"  ⚠️ Err: {err}"
                    try:
                        tg_req.post(
                            f"https://api.telegram.org/bot{tg_token}/editMessageText",
                            json={"chat_id": tg_chat_id, "message_id": tg_msg_id, "text": text},
                            timeout=5
                        )
                    except:
                        pass

            result = self.fb_api.check_fb_batch(resolved, close_after=close_after,
                                               max_workers=max_workers,
                                               progress_callback=progress_cb)
            # Add original names back to results
            if 'results' in result:
                for r in result['results']:
                    uuid = r.get('profile_uuid', '')
                    r['name'] = name_map.get(uuid, uuid)
            return result

        elif path == '/fb_nurture_batch':
            profile_uuids = params.get('profile_uuids') or params.get('profiles', [])
            if not profile_uuids:
                return {"error": "profile_uuids required (list of uuids)"}
            max_workers = int(params.get('max_workers', 3))
            scroll_count = int(params.get('scroll_count', 3))
            comments_per_profile = int(params.get('comments_per_profile', 2))
            comment_style = params.get('comment_style', 'positive')
            return self.fb_api.fb_nurture_batch(
                profile_uuids,
                max_workers=max_workers,
                scroll_count=scroll_count,
                comments_per_profile=comments_per_profile,
                comment_style=comment_style
            )

        elif path == '/leave_groups':
            profile_uuid = params.get('profile_uuid') or params.get('profile')
            max_groups = int(params.get('max_groups', 100))
            telegram_chat_id = params.get('telegram_chat_id')
            telegram_message_id = params.get('telegram_message_id')
            if telegram_message_id:
                telegram_message_id = int(telegram_message_id)
            return self.fb_api.leave_groups(profile_uuid, max_groups=max_groups,
                                            telegram_chat_id=telegram_chat_id,
                                            telegram_message_id=telegram_message_id)

        elif path == '/watch_reels':
            profile_uuid = params.get('profile_uuid') or params.get('profile')
            count = int(params.get('count', 5))
            do_comment = params.get('comment', True)
            comment_count = int(params.get('comment_count', 3))
            telegram_chat_id = params.get('telegram_chat_id')
            telegram_message_id = params.get('telegram_message_id')
            if telegram_message_id:
                telegram_message_id = int(telegram_message_id)
            return self.fb_api.watch_reels(
                profile_uuid, count=count, comment=do_comment,
                comment_count=comment_count,
                telegram_chat_id=telegram_chat_id,
                telegram_message_id=telegram_message_id)

        elif path == '/debug_groups':
            profile_uuid = params.get('profile_uuid') or params.get('profile')
            return self.fb_api.debug_groups(profile_uuid)

        elif path == '/list_folders':
            try:
                from api_service import api as hidemium
                folders = hidemium.get_folders(limit=100)
                return {"folders": folders, "total": len(folders)}
            except Exception as e:
                return {"error": str(e)}

        elif path == '/list_profiles':
            folder_id = params.get('folder_id')
            page = params.get('page', 1)
            page_size = params.get('page_size', 100)
            search = params.get('search', '')
            # Map string folder names (fb1, fb2...) to numeric Hidemium folder IDs
            FOLDER_NAME_MAP = {
                'fb1': 1, 'fb2': 2, 'fb5': 5, 'fb6': 6, 'fb7': 7,
                '1': 1, '2': 2, '5': 5, '6': 6, '7': 7,
            }
            if folder_id is not None and not isinstance(folder_id, list):
                folder_id = [folder_id]
            if folder_id is None:
                folder_id = []
            # Convert string names to numeric IDs
            folder_id = [FOLDER_NAME_MAP.get(str(f).lower().strip(), f) for f in folder_id]
            try:
                import requests as req
                resp = req.post(
                    f"http://127.0.0.1:2222/v1/browser/list?is_local=true",
                    headers={
                        "Authorization": "Bearer MkkmO8VZiI22dcJIS4qDiYwyNH6JIGC8G9L",
                        "Content-Type": "application/json"
                    },
                    json={"page": page, "limit": page_size, "folder_id": folder_id, "search": search}
                )
                raw = resp.json()
                # Normalize Hidemium nested response to flat {data: [...], total: N}
                profiles = raw
                if isinstance(profiles, dict) and 'data' in profiles:
                    inner = profiles['data']
                    if isinstance(inner, dict) and 'content' in inner:
                        content = inner['content']
                        if isinstance(content, list):
                            total = inner.get('meta', {}).get('total', len(content))
                            return {"data": content, "total": total}
                        else:
                            return {"data": [], "total": 0, "error": "Hidemium returned error: " + str(content)}
                return raw
            except Exception as e:
                return {"success": False, "error": str(e)}

        # ===== LOGIN FB =====
        elif path == '/login_fb':
            profile_uuid = params.get('profile_uuid') or params.get('profile')
            fb_id = params.get('fb_id') or params.get('uid')
            password = params.get('password') or params.get('pass')
            if not profile_uuid or not fb_id or not password:
                return {"error": "profile_uuid, fb_id, password required"}
            close_after = params.get('close_after', False)
            return self.fb_api.login_fb(profile_uuid, fb_id, password, close_after=close_after)

        elif path == '/login_fb_try':
            profile_uuid = params.get('profile_uuid') or params.get('profile')
            accounts = params.get('accounts', [])
            if not profile_uuid or not accounts:
                return {"error": "profile_uuid and accounts required. accounts=[{fb_id,password},...]"}
            close_after = params.get('close_after', False)
            return self.fb_api.login_fb_try_accounts(profile_uuid, accounts, close_after=close_after)

        elif path == '/screenshot':
            profile_uuid = params.get('profile_uuid') or params.get('profile')
            if not profile_uuid:
                return {"error": "profile_uuid required"}
            return self.fb_api.screenshot_profile(profile_uuid)

        elif path == '/send_telegram_photo':
            filepath = params.get('filepath') or params.get('screenshot')
            chat_id = params.get('chat_id')
            caption = params.get('caption', '')
            if not filepath or not chat_id:
                return {"error": "filepath and chat_id required"}
            return self.fb_api.send_telegram_photo(filepath, str(chat_id), caption)

        # ===== VISION PIPELINE =====
        elif path == '/vision_click':
            target = params.get('target') or params.get('target_desc')
            if not target:
                return {"error": "target (description) required, e.g. 'Create post button'"}
            skill_name = params.get('skill_name')
            return self.fb_api.vision_click_target(
                target, skill_name=skill_name,
                profile_uuid=params.get('profile'),
                max_retries=int(params.get('max_retries', 2))
            )

        elif path == '/vision_type':
            target = params.get('target') or params.get('target_desc')
            text = params.get('text')
            if not target or not text:
                return {"error": "target and text required"}
            skill_name = params.get('skill_name')
            return self.fb_api.vision_type_into(
                target, text, skill_name=skill_name,
                profile_uuid=params.get('profile')
            )

        elif path == '/vision_capture':
            return self.fb_api.vision_capture(params.get('profile'))

        elif path == '/vision_detect':
            image_b64 = params.get('image_b64')
            target = params.get('target') or params.get('target_desc')
            width = params.get('width')
            height = params.get('height')
            if not image_b64 or not target or not width or not height:
                return {"error": "image_b64, target, width, height required"}
            return self.fb_api.vision_detect(image_b64, target, int(width), int(height))

        elif path == '/vision_skills':
            return self.fb_api.vision_list_skills()

        elif path == '/vision_clear_skills':
            return self.fb_api.vision_clear_skills()

        elif path == '/agent_execute':
            profile_uuid = params.get('profile_uuid') or params.get('profile')
            task = params.get('task', '')
            telegram_chat_id = params.get('telegram_chat_id')
            telegram_message_id = params.get('telegram_message_id')
            if telegram_message_id:
                telegram_message_id = int(telegram_message_id)
            if not task:
                return {"error": "task is required"}
            return self.fb_api.agent_execute(
                task=task, profile_uuid=profile_uuid,
                telegram_chat_id=telegram_chat_id,
                telegram_message_id=telegram_message_id)

        elif path == '/agent_skills':
            import db as _db
            return {"skills": _db.get_agent_skills(limit=50)}

        elif path == '/agent_task_history':
            import db as _db
            profile_uuid = params.get('profile_uuid')
            limit = int(params.get('limit', 20))
            return {"history": _db.get_agent_task_history(
                limit=limit, profile_uuid=profile_uuid)}

        elif path == '/agent_skill_delete':
            import db as _db
            skill_id = params.get('skill_id')
            if not skill_id:
                return {"error": "skill_id required"}
            ok = _db.delete_agent_skill(int(skill_id))
            return {"success": ok}

        # ===== HIDEMIUM MANAGEMENT APIs =====
        elif path == '/list_tags':
            try:
                from api_service import api as hidemium
                result = hidemium.get_tags()
                return {"tags": result}
            except Exception as e:
                return {"error": str(e)}

        elif path == '/list_scripts':
            try:
                from api_service import api as hidemium
                scripts = hidemium.get_scripts(limit=100)
                return {"scripts": scripts, "total": len(scripts)}
            except Exception as e:
                return {"error": str(e)}

        elif path == '/list_campaigns':
            try:
                from api_service import api as hidemium
                search = params.get('search', '')
                limit = int(params.get('limit', 50))
                result = hidemium.get_campaigns(search=search, limit=limit)
                return result
            except Exception as e:
                return {"error": str(e)}

        elif path == '/get_profile_detail':
            try:
                from api_service import api as hidemium
                uuid = params.get('uuid') or params.get('profile_uuid')
                if not uuid:
                    return {"error": "uuid required"}
                return hidemium.get_profile_detail(uuid)
            except Exception as e:
                return {"error": str(e)}

        elif path == '/create_profile':
            try:
                from api_service import api as hidemium
                return hidemium.create_profile(params)
            except Exception as e:
                return {"error": str(e)}

        elif path == '/update_profile_name':
            try:
                from api_service import api as hidemium
                uuid = params.get('uuid') or params.get('profile_uuid')
                name = params.get('name')
                if not uuid or not name:
                    return {"error": "uuid and name required"}
                return hidemium.update_profile_name(uuid, name)
            except Exception as e:
                return {"error": str(e)}

        elif path == '/update_profile_note':
            try:
                from api_service import api as hidemium
                uuid = params.get('uuid') or params.get('profile_uuid')
                note = params.get('note', '')
                if not uuid:
                    return {"error": "uuid required"}
                return hidemium.update_profile_note(uuid, note)
            except Exception as e:
                return {"error": str(e)}

        elif path == '/update_proxy':
            try:
                from api_service import api as hidemium
                uuid = params.get('uuid') or params.get('profile_uuid')
                proxy_type = params.get('type', 'http')
                ip = params.get('ip') or params.get('host')
                port = params.get('port')
                user = params.get('user', '')
                password = params.get('pass', params.get('password', ''))
                if not uuid or not ip or not port:
                    return {"error": "uuid, ip, port required"}
                return hidemium.update_proxy(uuid, proxy_type, ip, str(port), user, password)
            except Exception as e:
                return {"error": str(e)}

        elif path == '/remove_proxy':
            try:
                from api_service import api as hidemium
                uuid = params.get('uuid') or params.get('profile_uuid')
                if not uuid:
                    return {"error": "uuid required"}
                return hidemium.remove_proxy(uuid)
            except Exception as e:
                return {"error": str(e)}

        elif path == '/change_fingerprint':
            try:
                from api_service import api as hidemium
                uuid = params.get('uuid') or params.get('profile_uuid')
                if not uuid:
                    return {"error": "uuid required"}
                return hidemium.change_fingerprint(uuid)
            except Exception as e:
                return {"error": str(e)}

        elif path == '/change_status':
            try:
                from api_service import api as hidemium
                uuid = params.get('uuid') or params.get('profile_uuid')
                status = params.get('status')
                if not uuid or not status:
                    return {"error": "uuid and status required"}
                return hidemium.change_status(uuid, status)
            except Exception as e:
                return {"error": str(e)}

        elif path == '/add_to_folder':
            try:
                from api_service import api as hidemium
                folder_uuid = params.get('folder_uuid') or params.get('folder_id')
                profile_uuids = params.get('profile_uuids', [])
                if not folder_uuid or not profile_uuids:
                    return {"error": "folder_uuid and profile_uuids required"}
                return hidemium.add_profiles_to_folder(str(folder_uuid), profile_uuids)
            except Exception as e:
                return {"error": str(e)}

        elif path == '/sync_tags':
            try:
                from api_service import api as hidemium
                uuid = params.get('uuid') or params.get('profile_uuid')
                tags = params.get('tags', [])
                if not uuid:
                    return {"error": "uuid required"}
                return hidemium.sync_tags(uuid, tags)
            except Exception as e:
                return {"error": str(e)}

        elif path == '/get_versions':
            try:
                from api_service import api as hidemium
                versions = hidemium.get_versions()
                return {"versions": versions, "total": len(versions)}
            except Exception as e:
                return {"error": str(e)}

        elif path == '/get_running':
            try:
                from api_service import api as hidemium
                running = hidemium.get_running_profiles()
                return {"running": running, "total": len(running)}
            except Exception as e:
                return {"error": str(e)}

        elif path == '/get_default_configs':
            try:
                from api_service import api as hidemium
                return hidemium.get_default_configs()
            except Exception as e:
                return {"error": str(e)}

        elif path == '/create_schedule':
            try:
                from api_service import api as hidemium
                name = params.get('name')
                campaign_id = params.get('campaign_id')
                start_time = params.get('start_time')
                if not name or not campaign_id or not start_time:
                    return {"error": "name, campaign_id, start_time required"}
                return hidemium.create_schedule(
                    name=name, campaign_id=int(campaign_id), start_time=start_time,
                    execution_frequency=int(params.get('execution_frequency', 1)),
                    is_running=params.get('is_running', False)
                )
            except Exception as e:
                return {"error": str(e)}

        elif path == '/get_schedules':
            try:
                from api_service import api as hidemium
                campaign_id = params.get('campaign_id')
                if not campaign_id:
                    return {"error": "campaign_id required"}
                return hidemium.get_schedules(int(campaign_id))
            except Exception as e:
                return {"error": str(e)}

        elif path == '/get_user_info':
            try:
                from api_service import api as hidemium
                return hidemium.get_user_uuid()
            except Exception as e:
                return {"error": str(e)}

        else:
            return {"error": "Unknown endpoint"}

    def log_message(self, format, *args):
        print(f"[API] {args[0]}")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Multi-threaded HTTP server to handle concurrent requests"""
    daemon_threads = True


def start_api_server(port: int = 8899):
    """Khởi động API server trong thread riêng"""
    server = ThreadingHTTPServer(('127.0.0.1', port), APIHandler)
    print(f"[API] FB Manager Pro API running on http://127.0.0.1:{port}")
    server.serve_forever()


def start_api_server_thread(port: int = 8899):
    """Khởi động API server trong background thread"""
    thread = threading.Thread(target=start_api_server, args=(port,), daemon=True)
    thread.start()
    return thread


if __name__ == '__main__':
    start_api_server(8899)
