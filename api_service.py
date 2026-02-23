"""
Hidemium API Service
Kết nối với Hidemium Browser API
"""
import requests
from typing import Optional, Dict, List, Any
from config import HIDEMIUM_BASE_URL, HIDEMIUM_TOKEN


class HidemiumAPI:
    def __init__(self, base_url: str = HIDEMIUM_BASE_URL, token: str = HIDEMIUM_TOKEN):
        self.base_url = base_url
        self.token = token
        # Headers với Authorization Bearer token
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Bearer {token}"
        }

    def _request(self, method: str, endpoint: str, params: Dict = None, data: Dict = None) -> Dict:
        """Thực hiện request đến API với Bearer token"""
        url = f"{self.base_url}{endpoint}"

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=self.headers,
                params=params,
                json=data,
                timeout=30
            )
            return response.json()
        except requests.exceptions.ConnectionError:
            return {"type": "error", "title": "Không thể kết nối đến Hidemium", "content": None}
        except Exception as e:
            return {"type": "error", "title": str(e), "content": None}

    def _get(self, endpoint: str, params: Dict = None) -> Dict:
        """GET request với Bearer token"""
        url = f"{self.base_url}{endpoint}"

        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            return response.json()
        except requests.exceptions.ConnectionError:
            return {"type": "error", "title": "Không thể kết nối đến Hidemium", "content": None}
        except Exception as e:
            return {"type": "error", "title": str(e), "content": None}

    # ============ CONNECTION CHECK ============

    def check_connection(self) -> bool:
        """Kiểm tra kết nối Hidemium với Bearer token"""
        try:
            response = requests.get(
                f"{self.base_url}/v2/tag",
                headers=self.headers,
                timeout=5
            )
            return response.status_code == 200
        except requests.exceptions.RequestException:
            # Handle all request-related errors (connection, timeout, etc.)
            return False
    
    # ============ PROFILE MANAGEMENT ============
    
    def get_profiles(self, limit: int = 100, page: int = 1, is_local: bool = True,
                     search: str = "", folder_id: List = None, status: str = "") -> List:
        """Lấy danh sách profiles (POST method với body JSON)"""
        body = {
            "orderName": 0,
            "orderLastOpen": 0,
            "page": page,
            "limit": limit,
            "search": search,
            "status": status,
            "date_range": ["", ""],
            "folder_id": folder_id or []
        }
        result = self._request(
            "POST",
            "/v1/browser/list",
            params={"is_local": str(is_local).lower()},
            data=body
        )
        # Parse response theo cấu trúc thực tế
        if result and 'data' in result:
            data = result['data']
            if isinstance(data, dict) and 'content' in data:
                content = data['content']
                # Nếu content là list -> trả về list
                if isinstance(content, list):
                    return content
                # Nếu content là dict - kiểm tra có phải error không
                elif isinstance(content, dict):
                    # Kiểm tra error response
                    if 'code' in content or 'error' in content:
                        return []
                    # Nếu là profile hợp lệ (có uuid) -> wrap trong list
                    if 'uuid' in content:
                        return [content]
                    return []
            elif isinstance(data, list):
                return data
        return []
    
    def get_profile_detail(self, uuid: str, is_local: bool = False) -> Dict:
        """Lấy chi tiết profile"""
        return self._request(
            "GET",
            f"/v1/browser/{uuid}",
            params={"is_local": str(is_local).lower()}
        )
    
    def create_profile_default(self, default_config_id: int, is_local: bool = True) -> Dict:
        """Tạo profile từ config mặc định"""
        return self._request(
            "POST",
            "/create-profile-by-default",
            params={"is_local": str(is_local).lower()},
            data={"defaultConfigId": default_config_id}
        )
    
    def create_profile_custom(self, config: Dict, is_local: bool = True) -> Dict:
        """Tạo profile tùy chỉnh - POST /create-profile-custom"""
        return self._request(
            "POST",
            "/create-profile-custom",
            params={"is_local": str(is_local).lower()},
            data=config
        )
    
    def create_profile(self, profile_data: Dict) -> Dict:
        """Tạo profile mới với đầy đủ options"""
        os_type = profile_data.get("os", "win")
        
        # Build config theo format API yêu cầu
        config = {
            "os": os_type,
            "osVersion": str(profile_data.get("osVersion", "11")),
            "browser": profile_data.get("browser", "chrome"),
            "version": str(profile_data.get("browserVersion", "143")),
            "name": profile_data.get("name", "New Profile"),
            "resolution": profile_data.get("resolution", "1920x1080"),
            "language": profile_data.get("language", "vi-VN"),
            "canvas": profile_data.get("canvas", True),
            "webGLImage": profile_data.get("webGLImage", "false"),
            "audioContext": profile_data.get("audioContext", "false"),
            "webGLMetadata": profile_data.get("webGLMetadata", "false"),
            "clientRectsEnable": profile_data.get("clientRectsEnable", "false"),
            "noiseFont": profile_data.get("noiseFont", "false"),
        }
        
        # Thêm device_type cho mobile
        if os_type in ["ios", "android"]:
            config["device_type"] = profile_data.get("device_type", "phone")
        
        # Debug log
        print(f"Creating profile with config: {config}")
        
        # Optional fields
        if profile_data.get("userAgent"):
            config["userAgent"] = profile_data["userAgent"]
        
        if profile_data.get("deviceMemory"):
            config["deviceMemory"] = profile_data["deviceMemory"]
        
        if profile_data.get("hardwareConcurrency"):
            config["hardwareConcurrency"] = profile_data["hardwareConcurrency"]
        
        if profile_data.get("StartURL"):
            config["StartURL"] = profile_data["StartURL"]
        
        if profile_data.get("command"):
            config["command"] = profile_data["command"]
        
        if profile_data.get("folder_name"):
            config["folder_name"] = profile_data["folder_name"]
        
        # Proxy format: TYPE|host|port|user|password
        if profile_data.get("proxy"):
            config["proxy"] = profile_data["proxy"]
        
        return self.create_profile_custom(config, is_local=True)
    
    def delete_profiles(self, uuids: List[str], is_local: bool = True) -> Dict:
        """Xóa profiles"""
        return self._request(
            "DELETE",
            "/v1/browser/destroy",
            params={"is_local": str(is_local).lower()},
            data={"uuid_browser": uuids}
        )
    
    def update_profile_name(self, uuid: str, name: str) -> Dict:
        """Cập nhật tên profile"""
        return self._request(
            "PUT",
            "/v1/browser/name/update",
            data={"uuid": uuid, "name": name}
        )
    
    def update_profile_note(self, uuid: str, note: str) -> Dict:
        """Cập nhật ghi chú"""
        return self._request(
            "PUT",
            "/v1/browser/note/update",
            data={"uuid": uuid, "note": note}
        )
    
    # ============ BROWSER CONTROL ============
    
    def open_browser(self, uuid: str, command: str = "", proxy: str = "",
                     auto_resize: bool = True) -> Dict:
        """
        Mở browser/profile - GET /openProfile
        """
        params = {"uuid": uuid}

        # Thêm --force-device-scale-factor để scale browser
        from automation.window_manager import WindowManager
        scale_factor = WindowManager.SCALE_FACTOR
        scale_flag = f"--force-device-scale-factor={scale_factor}"
        
        # Thêm flag cho CDP WebSocket access
        cdp_flag = "--remote-allow-origins=*"

        if command:
            # Append flags to existing command
            params["command"] = f"{command} {scale_flag} {cdp_flag}"
        else:
            params["command"] = f"{scale_flag} {cdp_flag}"

        if proxy:
            params["proxy"] = proxy
        result = self._get("/openProfile", params=params)

        # Auto resize window position if successful
        if auto_resize and result.get('status') == 'successfully':
            self._auto_resize_browser_window(result)

        return result

    def _auto_resize_browser_window(self, open_result: Dict):
        """
        Tự động sắp xếp vị trí cửa sổ browser theo grid
        (Scale được xử lý qua --force-device-scale-factor khi mở browser)
        """
        import time
        try:
            from automation.window_manager import acquire_window_slot, release_window_slot, get_window_bounds

            data = open_result.get('data', {})
            remote_port = data.get('remote_port')

            if not remote_port:
                print(f"[API] No remote_port for window resize")
                return

            # Acquire slot
            slot_id = acquire_window_slot()

            # Wait for browser to start
            time.sleep(0.5)

            # Get page websocket
            import requests
            import websocket
            import json as json_module

            resp = requests.get(f"http://127.0.0.1:{remote_port}/json", timeout=5)
            tabs = resp.json()

            page_ws = None
            for tab in tabs:
                if tab.get('type') == 'page':
                    page_ws = tab.get('webSocketDebuggerUrl')
                    break

            if not page_ws:
                print(f"[API] No page websocket found")
                release_window_slot(slot_id)
                return

            # Connect WebSocket
            ws = websocket.create_connection(page_ws, timeout=5, suppress_origin=True)
            msg_id = [0]

            def send_cmd(method, params=None):
                msg_id[0] += 1
                msg = {"id": msg_id[0], "method": method}
                if params:
                    msg["params"] = params
                ws.send(json_module.dumps(msg))
                return json_module.loads(ws.recv())

            # Get window bounds from slot
            x, y, w, h = get_window_bounds(slot_id)
            print(f"[API] Target window: x={x}, y={y}, w={w}, h={h}")

            # Set window position and size
            win_result = send_cmd("Browser.getWindowForTarget", {})
            print(f"[API] getWindowForTarget: {win_result}")

            if win_result and 'result' in win_result and 'windowId' in win_result['result']:
                window_id = win_result['result']['windowId']

                # Set window bounds (position only - scaling done via --force-device-scale-factor)
                bounds_result = send_cmd("Browser.setWindowBounds", {
                    "windowId": window_id,
                    "bounds": {"left": x, "top": y, "width": w, "height": h, "windowState": "normal"}
                })
                print(f"[API] setWindowBounds: {bounds_result}")

                if 'error' not in bounds_result:
                    print(f"[API] ✓ Window positioned at ({x}, {y})")
                else:
                    print(f"[API] ERROR: {bounds_result.get('error')}")

            ws.close()

            # Store slot_id in result for later release
            open_result['_window_slot_id'] = slot_id

        except Exception as e:
            import traceback
            print(f"[API] Window resize error: {e}")
            traceback.print_exc()
    
    def close_browser(self, uuid: str) -> Dict:
        """Đóng browser/profile - GET /closeProfile"""
        return self._get("/closeProfile", params={"uuid": uuid})
    
    def check_profile(self, uuid: str) -> Dict:
        """Kiểm tra trạng thái profile - GET /authorize"""
        return self._get("/authorize", params={"uuid": uuid})
    
    # ============ PROXY MANAGEMENT ============
    
    def update_proxy(self, browser_uuid: str, proxy_type: str, ip: str, port: str, 
                     user: str = "", password: str = "", is_local: bool = True) -> Dict:
        """Cập nhật proxy nhanh"""
        return self._request(
            "PUT",
            "/v2/proxy/quick-edit",
            params={"is_local": str(is_local).lower()},
            data={
                "browser_uuid": browser_uuid,
                "type": proxy_type,
                "ip": ip,
                "port": port,
                "user": user,
                "pass": password
            }
        )
    
    def remove_proxy(self, uuid: str) -> Dict:
        """Xóa proxy khỏi profile"""
        return self._request(
            "PUT",
            "/v1/browser/proxy/remove",
            data={"uuid": uuid}
        )
    
    # ============ FOLDER MANAGEMENT ============
    
    def get_folders(self, limit: int = 100, page: int = 1, is_local: bool = True) -> List:
        """Lấy danh sách folders"""
        result = self._request(
            "GET",
            "/v1/folder/list",
            params={"limit": limit, "page": page, "is_local": str(is_local).lower()}
        )
        # Parse response
        if result and 'data' in result:
            data = result['data']
            if isinstance(data, dict) and 'content' in data:
                content = data['content']
                if isinstance(content, list):
                    return content
                elif isinstance(content, dict):
                    return [content]
            elif isinstance(data, list):
                return data
        return []
    
    def add_profiles_to_folder(self, folder_uuid: str, profile_uuids: List[str], is_local: bool = True) -> Dict:
        """Thêm profiles vào folder"""
        return self._request(
            "POST",
            f"/v1/folder/{folder_uuid}/add-browser",
            params={"is_local": str(is_local).lower()},
            data={"uuid_browser": profile_uuids}
        )
    
    # ============ TAGS ============
    
    def get_tags(self) -> Dict:
        """Lấy danh sách tags"""
        return self._request("GET", "/v2/tag")
    
    def sync_tags(self, uuid: str, tags: List[str]) -> Dict:
        """Đồng bộ tags cho profile"""
        return self._request(
            "POST",
            "/v1/browser/tags/sync",
            data={"uuid": uuid, "tags": tags}
        )
    
    # ============ STATUS ============
    
    def get_status_list(self, is_local: bool = True) -> Dict:
        """Lấy danh sách status có thể có"""
        return self._request(
            "GET", 
            "/v2/status-profile",
            params={"is_local": str(is_local).lower()}
        )
    
    def get_running_profiles(self, is_local: bool = True) -> List[str]:
        """Lấy danh sách UUIDs của profiles đang thực sự running"""
        result = self._request(
            "GET", 
            "/v2/status-profile",
            params={"is_local": str(is_local).lower()}
        )
        # Response: {"content": [{"uuid": "...", ...}, ...]} hoặc {"content": []}
        if result and 'content' in result:
            content = result['content']
            if isinstance(content, list):
                return [p.get('uuid') for p in content if p.get('uuid')]
        return []
    
    # ============ DEFAULT CONFIG ============
    
    def get_default_configs(self, page: int = 1, limit: int = 10) -> Dict:
        """Lấy danh sách config mặc định"""
        return self._request(
            "GET",
            "/v2/default-config",
            params={"page": page, "limit": limit}
        )
    
    # ============ AUTOMATION / SCRIPTS ============
    
    def get_scripts(self, page: int = 1, limit: int = 50) -> List:
        """Lấy danh sách scripts/flows từ Hidemium"""
        result = self._request(
            "GET",
            "/v2/automation/script",
            params={"page": page, "limit": limit}
        )
        if result and 'data' in result:
            data = result['data']
            if isinstance(data, dict) and 'content' in data:
                return data['content']
        return []
    
    def get_campaigns(self, search: str = "", page: int = 1, limit: int = 10) -> Dict:
        """Lấy danh sách campaigns"""
        return self._request(
            "GET",
            "/automation/campaign",
            params={"search": search, "page": page, "limit": limit}
        )
    
    def create_campaign(self, name: str, input_vars: Dict = None) -> Dict:
        """Tạo campaign mới"""
        return self._request(
            "POST",
            "/automation/campaign",
            data={"name": name, "input_vars": input_vars or {}}
        )
    
    def update_campaign_variables(self, campaign_id: int, variables: List[Dict]) -> Dict:
        """Cập nhật biến cho campaign"""
        return self._request(
            "POST",
            "/automation/campaign/update-variables",
            data={
                "campaign_id": campaign_id,
                "variables": variables
            }
        )
    
    def delete_all_campaign_profiles(self, campaign_id: str) -> Dict:
        """Xóa tất cả profiles trong campaign"""
        return self._request(
            "DELETE",
            "/automation/campaign/delete-all-campaign-profile",
            data={"campaignId": campaign_id}
        )
    
    def run_script(self, script_key: int, profile_uuid: str, variables: Dict = None) -> Dict:
        """Chạy script cho profile (inferred endpoint)"""
        return self._request(
            "POST",
            "/automation/run",
            data={
                "script_key": script_key,
                "uuid": profile_uuid,
                "variables": variables or {}
            }
        )


# Singleton instance
api = HidemiumAPI()
