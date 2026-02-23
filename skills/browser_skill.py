"""
BrowserSkill - Open/close browser, screenshot profile.
"""

import os
import re
import time
import json
import base64
import traceback
from typing import Optional, Dict, Any
from api_service import api as hidemium
from automation.cdp_client import CDPClient
from skills.base_skill import SCREENSHOT_DIR


class BrowserSkill:
    """Browser lifecycle management: open, close, screenshot."""

    def open_browser(self, profile_uuid: str) -> Dict[str, Any]:
        """Mở browser cho profile"""
        try:
            result = hidemium.open_browser(profile_uuid)
            if result.get('type') == 'error':
                return {"success": False, "error": result.get('message')}

            data = result.get('data', {})
            remote_port = data.get('remote_port')
            ws_url = data.get('web_socket', '')

            if not remote_port and ws_url:
                match = re.search(r':(\d+)/', ws_url)
                if match:
                    remote_port = int(match.group(1))

            if not remote_port:
                return {"success": False, "error": "No remote_port found"}

            # Kết nối CDP
            time.sleep(2)
            cdp = CDPClient(remote_port)
            connect_result = cdp.connect()

            if not connect_result.success:
                return {"success": False, "error": f"CDP connect failed: {connect_result.error}"}

            self.connections[profile_uuid] = cdp
            self.current_profile = profile_uuid

            return {
                "success": True,
                "remote_port": remote_port,
                "ws_url": ws_url,
                "profile": profile_uuid
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def close_browser(self, profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Đóng browser"""
        uuid = profile_uuid or self.current_profile
        if uuid and uuid in self.connections:
            try:
                self.connections[uuid].disconnect()
            except:
                pass
            try:
                del self.connections[uuid]
            except:
                pass

        if uuid:
            hidemium.close_browser(uuid)

        if self.current_profile == uuid:
            self.current_profile = None

        return {"success": True, "closed": uuid}

    def screenshot_profile(self, profile_uuid: str) -> Dict[str, Any]:
        """
        Chụp screenshot browser đang mở (dùng CDP trực tiếp).
        Profile phải đã được mở bằng Hidemium.
        Returns: {success, screenshot, screenshot_url}
        """
        import websocket as ws_lib
        import requests as req

        try:
            # Thử mở profile (nếu đã mở thì trả lại port)
            result = hidemium.open_browser(profile_uuid)
            data = result.get('data', {})
            remote_port = data.get('remote_port')
            ws_url = data.get('web_socket', '')

            if not remote_port and ws_url:
                match = re.search(r':(\d+)/', ws_url)
                if match:
                    remote_port = int(match.group(1))

            if not remote_port:
                return {"success": False, "error": "Không lấy được remote_port. Profile chưa mở?"}

            # Kết nối CDP
            time.sleep(1)
            resp = req.get(f"http://127.0.0.1:{remote_port}/json", timeout=10)
            tabs = resp.json()

            page_ws = None
            for tab in tabs:
                if tab.get('type') == 'page':
                    page_ws = tab.get('webSocketDebuggerUrl')
                    break

            if not page_ws:
                return {"success": False, "error": "Không tìm được page WebSocket"}

            ws = ws_lib.create_connection(page_ws, timeout=15, suppress_origin=True)

            msg_id = [0]
            def send_cmd(method, params=None):
                msg_id[0] += 1
                msg = {"id": msg_id[0], "method": method}
                if params:
                    msg["params"] = params
                ws.send(json.dumps(msg))
                return json.loads(ws.recv())

            # Chụp screenshot
            screenshot_path = self._capture_screenshot(send_cmd, profile_uuid, 'manual')

            # Lấy URL hiện tại
            current_url = None
            try:
                r = send_cmd("Runtime.evaluate", {
                    "expression": "window.location.href",
                    "returnByValue": True
                })
                current_url = r.get('result', {}).get('result', {}).get('value')
            except:
                pass

            ws.close()

            if screenshot_path:
                return {
                    "success": True,
                    "screenshot": screenshot_path,
                    "screenshot_url": f"http://127.0.0.1:8899/screenshots/{os.path.basename(screenshot_path)}",
                    "current_url": current_url,
                    "profile_uuid": profile_uuid
                }
            else:
                return {"success": False, "error": "Chụp screenshot thất bại"}

        except Exception as e:
            traceback.print_exc()
            return {"success": False, "error": str(e)}
