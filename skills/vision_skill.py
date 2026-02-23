"""
VisionSkill - Universal vision-based automation pipeline.

Architecture (8-step pipeline):
  1. capture_viewport()     → 1:1 CSS pixel screenshot via CDP + Playwright
  2. vision_detect_point()  → Pollinations gemini-fast detects target (x,y)
  3. normalize_point()      → Adjust for DPR/zoom (usually 1:1 with override)
  4. resolve_element()      → elementFromPoint → element attributes
  5. derive_locator()       → Generate CSS/XPath/role locator candidates
  6. verify_target()        → Check locator uniqueness, visibility
  7. click_target()         → Click via Playwright mouse or locator
  8. cache_skill()          → Save successful locator to learned/ for reuse

3-tier hybrid:
  Tier 1: Cached skill (learned/) → try first, fast
  Tier 2: Vision pipeline → full 8-step flow
  Tier 3: Pixel click fallback → mouse.click(x,y) if locator fails

Threading: Playwright sync API is greenlet-bound. All Playwright operations
run on a dedicated worker thread (_PlaywrightWorker) for thread-safety
with the multi-threaded HTTP server.
"""

import base64
import json
import os
import queue
import re
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests as http_requests

# Directory for learned skills cache
SKILLS_DIR = os.path.dirname(os.path.abspath(__file__))
LEARNED_DIR = os.path.join(SKILLS_DIR, 'learned')
os.makedirs(LEARNED_DIR, exist_ok=True)

SCREENSHOT_DIR = os.path.join(os.path.dirname(SKILLS_DIR), 'screenshots')

# Vision API config
VISION_API_URL = "https://gen.pollinations.ai/v1/chat/completions"
VISION_API_KEY = "sk_3HRi9HUGLup7OKB6ykRds8YtnpcmLHD3"
VISION_MODEL = "gemini-fast"


# ================================================================
# PlaywrightWorker: dedicated thread for all Playwright operations
# ================================================================

class _PlaywrightWorker:
    """Single-thread Playwright execution context. Thread-safe task queue."""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="pw-worker")
        self._pw = None
        self._connections: Dict[int, tuple] = {}  # port -> (browser, page, cdp_session)
        self._started = threading.Event()
        self._thread.start()
        self._started.wait(timeout=15)

    def _run(self):
        """Worker loop: start Playwright once, process tasks forever."""
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            self._pw = pw
            self._started.set()
            print("[Vision] Playwright worker thread started")
            while True:
                try:
                    item = self._queue.get(timeout=300)
                    if item is None:
                        break
                    func, args, kwargs, result_q = item
                    try:
                        result = func(*args, **kwargs)
                        result_q.put(('ok', result))
                    except Exception as e:
                        traceback.print_exc()
                        result_q.put(('error', e))
                except queue.Empty:
                    continue
                except Exception:
                    pass

    def execute(self, func: Callable, *args, timeout: float = 60, **kwargs) -> Any:
        """Submit a task to the worker thread and wait for result."""
        result_q: queue.Queue = queue.Queue()
        self._queue.put((func, args, kwargs, result_q))
        try:
            status, result = result_q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"Playwright operation timed out ({timeout}s)")
        if status == 'error':
            raise result
        return result

    def get_page(self, remote_port: int):
        """Get or create browser connection. Must run ON worker thread."""
        cached = self._connections.get(remote_port)
        if cached:
            browser, page, cdp_session = cached
            try:
                page.evaluate('1+1')
                return page, cdp_session
            except Exception:
                try:
                    browser.close()
                except Exception:
                    pass
                del self._connections[remote_port]

        browser = self._pw.chromium.connect_over_cdp(f'http://127.0.0.1:{remote_port}')
        if not browser.contexts or not browser.contexts[0].pages:
            return None, None

        page = browser.contexts[0].pages[0]
        cdp_session = page.context.new_cdp_session(page)
        self._connections[remote_port] = (browser, page, cdp_session)
        print(f"[Vision] Connected to port {remote_port}, page: {page.url[:80]}")
        return page, cdp_session


# Singleton worker
_pw_worker: Optional[_PlaywrightWorker] = None
_pw_worker_lock = threading.Lock()


def _get_worker() -> _PlaywrightWorker:
    global _pw_worker
    if _pw_worker is None or not _pw_worker._thread.is_alive():
        with _pw_worker_lock:
            if _pw_worker is None or not _pw_worker._thread.is_alive():
                _pw_worker = _PlaywrightWorker()
    return _pw_worker


# ================================================================
# Helper functions that run ON the Playwright worker thread
# ================================================================

def _pw_capture(worker: _PlaywrightWorker, port: int) -> Dict:
    """Capture 1:1 CSS pixel screenshot."""
    page, cdp = worker.get_page(port)
    if not page:
        raise RuntimeError("Could not connect to browser")

    vp_w = page.evaluate('window.innerWidth')
    vp_h = page.evaluate('window.innerHeight')
    dpr = page.evaluate('window.devicePixelRatio')

    cdp.send('Emulation.setDeviceMetricsOverride', {
        'width': vp_w, 'height': vp_h,
        'deviceScaleFactor': 1, 'mobile': False,
    })
    try:
        result = cdp.send('Page.captureScreenshot', {'format': 'png'})
    finally:
        cdp.send('Emulation.clearDeviceMetricsOverride')

    img_b64 = result['data']
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    filepath = os.path.join(SCREENSHOT_DIR, f'vision_{ts}.png')
    with open(filepath, 'wb') as f:
        f.write(base64.b64decode(img_b64))

    return {'image_b64': img_b64, 'width': vp_w, 'height': vp_h, 'dpr': dpr, 'filepath': filepath}


def _pw_element_at(worker: _PlaywrightWorker, port: int, x: float, y: float) -> Dict:
    """elementFromPoint with parent walk-up."""
    page, _ = worker.get_page(port)
    if not page:
        raise RuntimeError("Could not connect to browser")

    return page.evaluate('''(coords) => {
        let el = document.elementFromPoint(coords.x, coords.y);
        if (!el) return { found: false };

        const meaningful = (e) => {
            if (!e) return false;
            const tag = e.tagName?.toLowerCase();
            if (['a', 'button', 'input', 'textarea', 'select', 'label'].includes(tag)) return true;
            if (e.getAttribute('role')) return true;
            if (e.getAttribute('aria-label')) return true;
            if (e.getAttribute('data-testid')) return true;
            if (e.textContent?.trim() && e.children.length === 0) return true;
            const cs = window.getComputedStyle(e);
            if (cs.cursor === 'pointer') return true;
            return false;
        };

        let target = el, depth = 0;
        while (target && !meaningful(target) && depth < 5) {
            target = target.parentElement;
            depth++;
        }
        if (!target) target = el;

        const rect = target.getBoundingClientRect();
        const cs = window.getComputedStyle(target);

        const getSelector = (e) => {
            if (e.id) return '#' + CSS.escape(e.id);
            let sel = e.tagName.toLowerCase();
            if (e.getAttribute('data-testid'))
                return sel + '[data-testid="' + e.getAttribute('data-testid') + '"]';
            if (e.getAttribute('role'))
                sel += '[role="' + e.getAttribute('role') + '"]';
            if (e.getAttribute('aria-label'))
                sel += '[aria-label="' + e.getAttribute('aria-label') + '"]';
            return sel;
        };

        const pathParts = [];
        let curr = target;
        for (let i = 0; i < 4 && curr && curr !== document.body; i++) {
            pathParts.unshift(getSelector(curr));
            curr = curr.parentElement;
        }

        return {
            found: true,
            tag: target.tagName,
            id: target.id || null,
            className: (target.className?.substring?.(0, 200)) || '',
            text: (target.textContent || '').substring(0, 150).trim(),
            role: target.getAttribute('role'),
            ariaLabel: target.getAttribute('aria-label'),
            placeholder: target.getAttribute('placeholder'),
            dataTestId: target.getAttribute('data-testid'),
            href: target.getAttribute('href'),
            type: target.getAttribute('type'),
            name: target.getAttribute('name'),
            cursor: cs.cursor,
            rect: { x: Math.round(rect.x), y: Math.round(rect.y),
                    width: Math.round(rect.width), height: Math.round(rect.height) },
            selectorPath: pathParts.join(' > '),
            depth: depth,
        };
    }''', {'x': x, 'y': y})


def _pw_verify(worker: _PlaywrightWorker, port: int, strategy: str, locator_str: str) -> Dict:
    """Verify locator uniqueness and visibility."""
    page, _ = worker.get_page(port)
    if not page:
        raise RuntimeError("Could not connect to browser")

    if strategy == 'role':
        m = re.match(r'(\w+)\[name="(.+)"\]', locator_str)
        loc = page.get_by_role(m.group(1), name=m.group(2)) if m else page.get_by_role(locator_str)
    elif strategy == 'text':
        m = re.match(r'\w+:has-text\("(.+)"\)', locator_str)
        loc = page.get_by_text(m.group(1)) if m else page.locator(locator_str)
    else:
        loc = page.locator(locator_str)

    count = loc.count()
    state = {}
    if count > 0:
        first = loc.first
        try:
            state = {'visible': first.is_visible(), 'enabled': first.is_enabled()}
            bbox = first.bounding_box()
            if bbox:
                state['bbox'] = {k: round(v) for k, v in bbox.items()}
        except Exception:
            pass

    return {'match_count': count, 'unique': count == 1, 'state': state}


def _pw_click_loc(worker: _PlaywrightWorker, port: int, strategy: str, locator_str: str) -> Dict:
    """Click via Playwright locator."""
    page, _ = worker.get_page(port)
    if not page:
        raise RuntimeError("Could not connect to browser")

    if strategy == 'role':
        m = re.match(r'(\w+)\[name="(.+)"\]', locator_str)
        loc = page.get_by_role(m.group(1), name=m.group(2)) if m else page.get_by_role(locator_str)
    elif strategy == 'text':
        m = re.match(r'\w+:has-text\("(.+)"\)', locator_str)
        loc = page.get_by_text(m.group(1)) if m else page.locator(locator_str)
    else:
        loc = page.locator(locator_str)

    if loc.count() == 1:
        loc.click(timeout=5000)
        return {'success': True, 'method': f'locator ({strategy})'}
    raise RuntimeError(f"Locator matched {loc.count()} elements, expected 1")


def _pw_click_xy(worker: _PlaywrightWorker, port: int, x: float, y: float) -> Dict:
    """Click at pixel coordinates."""
    page, _ = worker.get_page(port)
    if not page:
        raise RuntimeError("Could not connect to browser")
    page.mouse.click(x, y)
    return {'success': True}


def _pw_type(worker: _PlaywrightWorker, port: int, text: str, delay: int = 50) -> Dict:
    """Type text on focused element."""
    page, _ = worker.get_page(port)
    if not page:
        raise RuntimeError("Could not connect to browser")
    page.keyboard.type(text, delay=delay)
    return {'success': True}


def _pw_scroll(worker: _PlaywrightWorker, port: int, dy: int = 300) -> Dict:
    """Scroll page."""
    page, _ = worker.get_page(port)
    if not page:
        raise RuntimeError("Could not connect to browser")
    page.mouse.wheel(0, dy)
    return {'success': True}


def _pw_url(worker: _PlaywrightWorker, port: int) -> str:
    """Get current URL."""
    page, _ = worker.get_page(port)
    return page.url if page else ''


# ================================================================
# VisionSkill mixin
# ================================================================

class VisionSkill:
    """Universal vision-based automation pipeline with skill caching."""

    def _get_remote_port(self, profile_uuid: str) -> Optional[int]:
        """Get remote debugging port. Caches result per profile."""
        # Check cache first
        if not hasattr(self, '_port_cache'):
            self._port_cache = {}
        cached_port = self._port_cache.get(profile_uuid)
        if cached_port:
            # Verify port is still active
            try:
                import urllib.request
                urllib.request.urlopen(f'http://127.0.0.1:{cached_port}/json/version', timeout=2)
                return cached_port
            except Exception:
                del self._port_cache[profile_uuid]

        # Try from existing CDP connection
        cdp = self._get_cdp(profile_uuid)
        if cdp and hasattr(cdp, 'port'):
            self._port_cache[profile_uuid] = cdp.port
            return cdp.port

        # Open browser via Hidemium (also positions window)
        try:
            from api_service import api as hidemium
            result = hidemium.open_browser(profile_uuid)
            data = result.get('data', {})
            port = data.get('remote_port')
            if not port:
                ws_url = data.get('web_socket', '')
                m = re.search(r':(\d+)/', ws_url)
                if m:
                    port = int(m.group(1))
            if port:
                self._port_cache[profile_uuid] = port
            return port
        except Exception as e:
            print(f"[Vision] Get remote port error: {e}")
            return None

    def _require_port(self, profile_uuid: Optional[str] = None) -> int:
        """Get port or raise."""
        uuid = profile_uuid or self.current_profile
        if not uuid:
            raise RuntimeError("No profile specified")
        port = self._get_remote_port(uuid)
        if not port:
            raise RuntimeError(f"Could not get port for {uuid}")
        return port

    # ── Step 1: Capture ──

    def vision_capture(self, profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Capture viewport at 1:1 CSS pixels."""
        try:
            port = self._require_port(profile_uuid)
            w = _get_worker()
            r = w.execute(_pw_capture, w, port, timeout=15)
            return {"success": True, **r, "viewport": {"width": r['width'], "height": r['height']}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Step 2: Vision detect ──

    def vision_detect(self, image_b64: str, target_desc: str,
                      img_width: int, img_height: int) -> Dict[str, Any]:
        """Ask AI to find target coordinates in screenshot."""
        prompt = (
            f'Find the "{target_desc}" on this webpage screenshot. '
            f'The image is {img_width}x{img_height} pixels. '
            f'Return ONLY JSON: {{"points": [{{"x": <px>, "y": <py>, "confidence": <0-1>, "label": "<what>"}}]}}. '
            f'Coordinates MUST be within 0-{img_width} (x) and 0-{img_height} (y). '
            f'If not visible: {{"points": [], "error": "not found"}}. JSON only.'
        )
        try:
            resp = http_requests.post(VISION_API_URL, json={
                'model': VISION_MODEL,
                'messages': [{'role': 'user', 'content': [
                    {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{image_b64}'}},
                    {'type': 'text', 'text': prompt}
                ]}]
            }, headers={'Authorization': f'Bearer {VISION_API_KEY}'}, timeout=30)

            if resp.status_code != 200:
                return {"success": False, "error": f"API {resp.status_code}"}

            content = resp.json()['choices'][0]['message']['content']
            clean = re.sub(r'```(?:json)?', '', content).strip()
            m = re.search(r'\{.*\}', clean, re.DOTALL)
            if not m:
                return {"success": False, "error": f"No JSON: {content[:200]}"}

            data = json.loads(m.group())
            pts = data.get('points', [])
            valid = [p for p in pts if 0 <= p.get('x', -1) <= img_width and 0 <= p.get('y', -1) <= img_height]

            # Try rescaling out-of-bounds points
            if not valid and pts:
                for p in pts:
                    x, y = p.get('x', 0), p.get('y', 0)
                    if x > img_width or y > img_height:
                        sx, sy = img_width / max(x, 1), img_height / max(y, 1)
                        if 0.5 < sx < 2 and 0.5 < sy < 2:
                            valid.append({**p, 'x': int(x * sx), 'y': int(y * sy), 'scaled': True})

            return {"success": len(valid) > 0, "points": valid, "raw": content[:300]}
        except http_requests.Timeout:
            return {"success": False, "error": "Vision API timeout"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Step 4-5: Resolve element ──

    def vision_resolve_element(self, x: float, y: float,
                               profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """elementFromPoint with parent walk-up."""
        try:
            port = self._require_port(profile_uuid)
            w = _get_worker()
            elem = w.execute(_pw_element_at, w, port, x, y)
            return {"success": True, "element": elem}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Step 6: Derive locators ──

    @staticmethod
    def _derive_locators(elem: Dict) -> List[Dict]:
        """Generate locator candidates ordered by stability."""
        c = []
        if elem.get('dataTestId'):
            c.append({'strategy': 'css', 'stability': 0.95,
                      'locator': f'[data-testid="{elem["dataTestId"]}"]', 'reason': 'data-testid'})
        if elem.get('id'):
            c.append({'strategy': 'css', 'stability': 0.9,
                      'locator': f'#{elem["id"]}', 'reason': 'ID'})
        if elem.get('role') and elem.get('ariaLabel'):
            c.append({'strategy': 'role', 'stability': 0.85,
                      'locator': f'{elem["role"]}[name="{elem["ariaLabel"]}"]', 'reason': 'ARIA'})
        elif elem.get('ariaLabel'):
            c.append({'strategy': 'css', 'stability': 0.8,
                      'locator': f'[aria-label="{elem["ariaLabel"]}"]', 'reason': 'aria-label'})
        if elem.get('placeholder'):
            c.append({'strategy': 'css', 'stability': 0.75,
                      'locator': f'[placeholder="{elem["placeholder"]}"]', 'reason': 'placeholder'})
        if elem.get('name'):
            tag = elem.get('tag', '').lower()
            c.append({'strategy': 'css', 'stability': 0.75,
                      'locator': f'{tag}[name="{elem["name"]}"]', 'reason': 'name'})
        if elem.get('role') and not elem.get('ariaLabel'):
            c.append({'strategy': 'css', 'stability': 0.5,
                      'locator': f'[role="{elem["role"]}"]', 'reason': 'role only'})
        if elem.get('selectorPath'):
            c.append({'strategy': 'css', 'stability': 0.4,
                      'locator': elem['selectorPath'], 'reason': 'path'})
        text = elem.get('text', '').strip()
        if text and len(text) < 80:
            tag = elem.get('tag', 'div').lower()
            c.append({'strategy': 'text', 'stability': 0.3,
                      'locator': f'{tag}:has-text("{text[:50]}")', 'reason': 'text'})
        return c

    # ── Step 7: Verify ──

    def vision_verify_locator(self, locator_info: Dict,
                              profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Check locator uniqueness + visibility."""
        try:
            port = self._require_port(profile_uuid)
            w = _get_worker()
            r = w.execute(_pw_verify, w, port,
                          locator_info.get('strategy', 'css'), locator_info.get('locator', ''))
            return {"success": True, **r, "locator": locator_info}
        except Exception as e:
            return {"success": False, "error": str(e), "locator": locator_info}

    # ── Step 8: Click ──

    def vision_click(self, x: float, y: float, locator_info: Optional[Dict] = None,
                     profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Click via locator or pixel coordinates."""
        try:
            port = self._require_port(profile_uuid)
            w = _get_worker()

            if locator_info:
                try:
                    r = w.execute(_pw_click_loc, w, port,
                                 locator_info.get('strategy', 'css'), locator_info.get('locator', ''))
                    return {"success": True, "method": r['method'], "locator": locator_info.get('locator')}
                except Exception as e:
                    print(f"[Vision] Locator click failed: {e}")

            w.execute(_pw_click_xy, w, port, x, y)
            return {"success": True, "method": "pixel_click", "clicked_at": {"x": x, "y": y}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Skill cache ──

    def _get_cache_path(self, name: str) -> str:
        return os.path.join(LEARNED_DIR, re.sub(r'[^\w\-]', '_', name.lower()) + '.json')

    def _load_cached_skill(self, name: str) -> Optional[Dict]:
        path = self._get_cache_path(name)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                d = json.load(f)
            if d.get('cached_at') and (datetime.now() - datetime.fromisoformat(d['cached_at'])).days > 7:
                return None
            return d
        except Exception:
            return None

    def _save_cached_skill(self, name: str, loc: Dict, desc: str, url_pat: str = ''):
        existing = self._load_cached_skill(name)
        data = {
            'skill_name': name, 'target_desc': desc, 'locator': loc,
            'url_pattern': url_pat, 'cached_at': datetime.now().isoformat(),
            'success_count': (existing.get('success_count', 0) + 1) if existing else 1,
            'fail_count': existing.get('fail_count', 0) if existing else 0,
        }
        try:
            with open(self._get_cache_path(name), 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"[Vision] Cached: {name} → {loc.get('locator', '?')}")
        except Exception as e:
            print(f"[Vision] Cache save error: {e}")

    def _invalidate_cached_skill(self, name: str):
        cached = self._load_cached_skill(name)
        if not cached:
            return
        cached['fail_count'] = cached.get('fail_count', 0) + 1
        path = self._get_cache_path(name)
        if cached['fail_count'] >= 3:
            try:
                os.remove(path)
            except Exception:
                pass
        else:
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(cached, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

    # ── Main entry: 3-tier click ──

    def vision_click_target(self, target_desc: str,
                            skill_name: Optional[str] = None,
                            profile_uuid: Optional[str] = None,
                            max_retries: int = 2) -> Dict[str, Any]:
        """
        Universal click via natural language description.
        Tier 1: cached skill → Tier 2: vision pipeline → Tier 3: pixel click.
        """
        key = skill_name or target_desc
        uuid = profile_uuid or self.current_profile
        log = {"target": target_desc, "tiers_tried": []}

        # Tier 1: Cached
        cached = self._load_cached_skill(key)
        if cached:
            loc = cached.get('locator', {})
            try:
                v = self.vision_verify_locator(loc, uuid)
                if v.get('unique') and v.get('state', {}).get('visible'):
                    cr = self.vision_click(0, 0, loc, uuid)
                    if cr.get('success'):
                        self._save_cached_skill(key, loc, target_desc)
                        return {"success": True, "tier": 1, "method": cr.get('method'),
                                "locator": loc.get('locator'), "details": "Cached skill"}
            except Exception:
                pass
            self._invalidate_cached_skill(key)
            log["tiers_tried"].append({"tier": 1, "status": "miss"})

        # Tier 2 & 3: Vision
        for attempt in range(max_retries):
            cap = self.vision_capture(uuid)
            if not cap.get('success'):
                log["tiers_tried"].append({"tier": 2, "attempt": attempt,
                                           "status": "capture_fail", "error": cap.get('error')})
                continue

            det = self.vision_detect(cap['image_b64'], target_desc, cap['width'], cap['height'])
            if not det.get('success') or not det.get('points'):
                log["tiers_tried"].append({"tier": 2, "attempt": attempt,
                                           "status": "detect_fail", "error": det.get('error', 'no points')})
                try:
                    port = self._require_port(uuid)
                    _get_worker().execute(_pw_scroll, _get_worker(), port, 300)
                    time.sleep(1)
                except Exception:
                    pass
                continue

            pts = sorted(det['points'], key=lambda p: p.get('confidence', 0), reverse=True)
            x, y = pts[0]['x'], pts[0]['y']

            res = self.vision_resolve_element(x, y, uuid)
            if res.get('success') and res.get('element', {}).get('found'):
                elem = res['element']
                locs = self._derive_locators(elem)

                best = None
                for loc in locs:
                    try:
                        v = self.vision_verify_locator(loc, uuid)
                        if v.get('unique') and v.get('state', {}).get('visible'):
                            best = loc
                            break
                    except Exception:
                        continue

                if best:
                    cr = self.vision_click(x, y, best, uuid)
                    if cr.get('success'):
                        try:
                            port = self._require_port(uuid)
                            url = _get_worker().execute(_pw_url, _get_worker(), port)
                            url_pat = urlparse(url).path if url else ''
                        except Exception:
                            url_pat = ''
                        self._save_cached_skill(key, best, target_desc, url_pat)
                        return {
                            "success": True, "tier": 2, "method": cr.get('method'),
                            "locator": best.get('locator'),
                            "element": {"tag": elem.get('tag'), "text": elem.get('text', '')[:80]},
                            "details": f"Vision@({x},{y}) → {best.get('locator')}",
                        }

            # Tier 3: Pixel click
            cr = self.vision_click(x, y, None, uuid)
            if cr.get('success'):
                return {"success": True, "tier": 3, "method": "pixel_click",
                        "clicked_at": {"x": x, "y": y}, "details": f"Pixel click ({x},{y})"}

        return {"success": False, "error": f"Failed '{target_desc}' after {max_retries} attempts", "log": log}

    # ── Vision type ──

    def vision_type_into(self, target_desc: str, text: str,
                         skill_name: Optional[str] = None,
                         profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Find input, click it, type text."""
        cr = self.vision_click_target(target_desc, skill_name, profile_uuid)
        if not cr.get('success'):
            return {"success": False, "error": f"Input not found: {cr.get('error')}"}
        time.sleep(0.3)
        try:
            port = self._require_port(profile_uuid)
            _get_worker().execute(_pw_type, _get_worker(), port, text)
            return {"success": True, "tier": cr.get('tier'), "method": cr.get('method'),
                    "typed_text": text, "locator": cr.get('locator')}
        except Exception as e:
            return {"success": False, "error": f"Type failed: {e}"}

    # ── List / clear skills ──

    def vision_list_skills(self) -> Dict[str, Any]:
        skills = []
        for fn in os.listdir(LEARNED_DIR):
            if fn.endswith('.json'):
                try:
                    with open(os.path.join(LEARNED_DIR, fn), 'r', encoding='utf-8') as f:
                        d = json.load(f)
                    skills.append({
                        'name': d.get('skill_name', fn), 'locator': d.get('locator', {}).get('locator', ''),
                        'strategy': d.get('locator', {}).get('strategy', ''),
                        'target': d.get('target_desc', ''),
                        'success_count': d.get('success_count', 0), 'fail_count': d.get('fail_count', 0),
                        'cached_at': d.get('cached_at', ''),
                    })
                except Exception:
                    pass
        return {"success": True, "skills": skills, "total": len(skills)}

    def vision_clear_skills(self) -> Dict[str, Any]:
        n = 0
        for fn in os.listdir(LEARNED_DIR):
            if fn.endswith('.json'):
                try:
                    os.remove(os.path.join(LEARNED_DIR, fn))
                    n += 1
                except Exception:
                    pass
        return {"success": True, "cleared": n}
