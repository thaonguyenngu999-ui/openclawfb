"""
ReelsSkill - Watch Facebook Reels and comment randomly.
"""

import json
import time
import random
import traceback
from typing import Optional, Dict, Any




# URLs to try for reels, in order
REELS_URLS = [
    "https://www.facebook.com/reel/",
    "https://www.facebook.com/reels/",
    "https://www.facebook.com/watch/reels/",
    "https://www.facebook.com/watch",
]


class ReelsSkill:
    """Facebook Reels: watch, like, comment."""

    def _check_reels_page(self, profile_uuid: str) -> dict:
        """Check if the current page actually loaded reels content."""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"ok": False, "reason": "no_cdp"}

        check_js = """
        (function() {
            const url = window.location.href;
            const body = document.body ? document.body.innerText : '';

            // Check for error page indicators
            const errorTexts = [
                'không xem được', 'không khả dụng', 'nội dung này',
                'không tìm thấy', 'đã bị xóa', 'không tồn tại',
                "can't be found", "isn't available", "content isn't available",
                "Page Not Found", "Sorry, this content"
            ];
            for (const et of errorTexts) {
                if (body.includes(et)) return JSON.stringify({ok: false, reason: 'error_page', text: et});
            }

            // Check for login page
            const loginForm = document.querySelector('#email') || document.querySelector('#pass');
            if (loginForm) return JSON.stringify({ok: false, reason: 'not_logged_in'});
            if (url.includes('/login')) return JSON.stringify({ok: false, reason: 'not_logged_in'});

            // Check for video elements (reels should have videos)
            const videos = document.querySelectorAll('video');
            const visibleVideos = [];
            for (const v of videos) {
                const rect = v.getBoundingClientRect();
                if (rect.width > 100 && rect.height > 100) visibleVideos.push(1);
            }

            if (visibleVideos.length > 0) {
                return JSON.stringify({ok: true, videos: visibleVideos.length, url: url});
            }

            // Check for reel-like containers
            const reelContainers = document.querySelectorAll(
                '[data-pagelet*="Reel"], [data-pagelet*="reel"], ' +
                '[aria-label*="Reel"], [aria-label*="reel"], ' +
                '[role="main"] video'
            );
            if (reelContainers.length > 0) {
                return JSON.stringify({ok: true, containers: reelContainers.length, url: url});
            }

            return JSON.stringify({ok: false, reason: 'no_video_content', url: url});
        })()
        """
        try:
            result = cdp._evaluate_js(check_js)
            return json.loads(str(result))
        except:
            return {"ok": False, "reason": "js_error"}

    def _send_verify_screenshot(self, profile_uuid: str, telegram_chat_id: str, caption: str):
        """Take screenshot and send to Telegram for verification."""
        if not telegram_chat_id:
            return
        try:
            sc = self.screenshot_profile(profile_uuid)
            if sc.get("success"):
                self.send_telegram_photo(sc["screenshot"], telegram_chat_id, caption)
        except Exception as e:
            print(f"[REELS] Screenshot error: {e}")

    def watch_reels(self, profile_uuid: str = None, count: int = 5,
                    comment: bool = True, comment_count: int = 3,
                    telegram_chat_id: str = None,
                    telegram_message_id: int = None) -> Dict[str, Any]:
        """
        Watch Facebook Reels with full verification at each step.
        """
        browser_opened_here = False
        try:
            profile_uuid = profile_uuid or self.current_profile
            if not profile_uuid:
                return {"success": False, "error": "No profile specified"}

            short_id = profile_uuid[:20]
            print(f"[REELS] {short_id} → Starting watch_reels (count={count}, comment={comment})")

            # 0. Auto-open browser if needed
            cdp = self._get_cdp(profile_uuid)
            if not cdp:
                print(f"[REELS] {short_id} → Opening browser...")
                open_result = self.open_browser(profile_uuid)
                if not open_result.get('success'):
                    return {"success": False, "error": f"Cannot open browser: {open_result.get('error')}"}
                browser_opened_here = True
                time.sleep(3)

            # 1. Try multiple URLs to find working reels page
            reels_loaded = False
            used_url = None

            for url in REELS_URLS:
                print(f"[REELS] {short_id} → Trying {url}...")
                nav_result = self.navigate(url, profile_uuid)
                if not nav_result.get('success'):
                    print(f"[REELS] {short_id} → Navigate failed for {url}")
                    continue

                time.sleep(5)

                # Verify page actually loaded reels
                page_check = self._check_reels_page(profile_uuid)
                print(f"[REELS] {short_id} → Page check: {page_check}")

                if page_check.get("ok"):
                    reels_loaded = True
                    used_url = url
                    print(f"[REELS] {short_id} → ✓ Reels loaded from {url}")
                    break
                else:
                    reason = page_check.get("reason", "unknown")
                    print(f"[REELS] {short_id} → ✗ {url} failed: {reason}")

                    if reason == "not_logged_in":
                        self._send_verify_screenshot(profile_uuid, telegram_chat_id,
                                                     f"❌ {short_id} - Chưa đăng nhập!")
                        if browser_opened_here:
                            self.close_browser(profile_uuid)
                        return {"success": False, "error": "Profile not logged in", "logged_in": False}

            if not reels_loaded:
                # Screenshot to show the error
                self._send_verify_screenshot(profile_uuid, telegram_chat_id,
                                             f"❌ {short_id} - Không load được trang Reels")
                if browser_opened_here:
                    self.close_browser(profile_uuid)
                return {"success": False, "error": "Cannot load reels page - all URLs failed"}

            # Screenshot after successful load
            self._send_verify_screenshot(profile_uuid, telegram_chat_id,
                                         f"✅ Reels loaded ({used_url})")

            # Click center of page to ensure focus for keyboard
            cdp = self._get_cdp(profile_uuid)
            if cdp:
                try:
                    # Click on video area (center of screen)
                    cdp._send_command('Input.dispatchMouseEvent', {
                        'type': 'mousePressed', 'x': 500, 'y': 400,
                        'button': 'left', 'clickCount': 1
                    })
                    time.sleep(0.05)
                    cdp._send_command('Input.dispatchMouseEvent', {
                        'type': 'mouseReleased', 'x': 500, 'y': 400,
                        'button': 'left', 'clickCount': 1
                    })
                    time.sleep(1)
                except:
                    pass

            # Helper: send progress to Telegram
            def send_progress(watched, liked, commented, total, current="", done=False):
                if not telegram_chat_id or not telegram_message_id:
                    return
                status = "✅ Hoàn thành!" if done else f"⏳ {current}"
                text = f"🎬 *Xem Reels - {short_id}*\n{status}\n\n"
                text += f"👁 Đã xem: {watched}/{total}\n"
                text += f"❤️ Like: {liked} | 💬 Comment: {commented}"
                self._telegram_edit_message(telegram_chat_id, telegram_message_id, text)

            # Decide which reels to comment on
            comment_indices = set()
            if comment and comment_count > 0:
                possible = list(range(count))
                comment_indices = set(random.sample(possible, min(comment_count, count)))

            watched = 0
            liked = 0
            commented = 0
            errors = []

            for i in range(count):
                reel_num = i + 1
                print(f"[REELS] {short_id} → Watching reel {reel_num}/{count}...")
                send_progress(watched, liked, commented, count, f"Đang xem reel {reel_num}/{count}")

                # Verify video is visible before counting as "watched"
                cdp = self._get_cdp(profile_uuid)
                if not cdp:
                    errors.append(f"Reel {reel_num}: lost CDP connection")
                    break

                video_check_js = """
                (function() {
                    const videos = document.querySelectorAll('video');
                    for (const v of videos) {
                        const rect = v.getBoundingClientRect();
                        if (rect.width > 100 && rect.height > 100 &&
                            rect.top >= -50 && rect.top < window.innerHeight) {
                            return JSON.stringify({
                                ok: true,
                                src: (v.src || v.currentSrc || '').substring(0, 100),
                                paused: v.paused,
                                w: Math.round(rect.width),
                                h: Math.round(rect.height)
                            });
                        }
                    }
                    return JSON.stringify({ok: false, total_videos: videos.length});
                })()
                """
                try:
                    vcheck = json.loads(str(cdp._evaluate_js(video_check_js)))
                except:
                    vcheck = {"ok": False}

                if not vcheck.get("ok"):
                    print(f"[REELS] {short_id} → Reel {reel_num}: no visible video, waiting...")
                    time.sleep(3)
                    # Retry check
                    try:
                        vcheck = json.loads(str(cdp._evaluate_js(video_check_js)))
                    except:
                        vcheck = {"ok": False}
                    if not vcheck.get("ok"):
                        print(f"[REELS] {short_id} → Reel {reel_num}: still no video, skipping")
                        errors.append(f"Reel {reel_num}: no visible video")
                        # Try to swipe anyway
                        self.press_key('ArrowDown', profile_uuid)
                        time.sleep(2)
                        continue

                # Watch the reel (5-10 seconds)
                watch_time = random.uniform(5, 10)
                print(f"[REELS] {short_id} → Reel {reel_num}: watching {watch_time:.0f}s...")
                time.sleep(watch_time)
                watched += 1

                # Like (70% chance)
                if random.random() < 0.7:
                    like_js = """
                    (async () => {
                        const selectors = [
                            '[aria-label="Thích"]',
                            '[aria-label="Like"]',
                            '[aria-label="like"]'
                        ];
                        let btn = null;
                        for (const sel of selectors) {
                            const btns = document.querySelectorAll(sel);
                            for (const b of btns) {
                                const rect = b.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0 && rect.top > 0) {
                                    btn = b;
                                    break;
                                }
                            }
                            if (btn) break;
                        }
                        if (!btn) return 'no_like_btn';
                        const label = btn.getAttribute('aria-label') || '';
                        if (label.includes('Bỏ thích') || label.includes('Unlike'))
                            return 'already_liked';
                        btn.click();
                        await new Promise(r => setTimeout(r, 500));
                        return 'liked';
                    })()
                    """
                    try:
                        like_result = self.execute_js(like_js, profile_uuid)
                        result_val = like_result.get("result", "")
                        if result_val in ('liked', 'already_liked'):
                            liked += 1
                            print(f"[REELS] {short_id} → Reel {reel_num}: {result_val}")
                        else:
                            print(f"[REELS] {short_id} → Reel {reel_num}: like result={result_val}")
                    except Exception as e:
                        print(f"[REELS] {short_id} → Like error: {e}")

                # Comment — read caption → AI generate relevant comment
                if i in comment_indices:
                    # Extract reel caption/title
                    caption_js = """
                    (function() {
                        // Try multiple selectors for reel caption
                        const sels = [
                            '[data-ad-rendering-role="story_message"]',
                            'div[dir="auto"][style*="webkit-line-clamp"]',
                            'span[dir="auto"]',
                            'h2 span', 'h3 span'
                        ];
                        // Focus on visible text near the current reel
                        const candidates = [];
                        for (const sel of sels) {
                            const els = document.querySelectorAll(sel);
                            for (const el of els) {
                                const rect = el.getBoundingClientRect();
                                const text = el.innerText.trim();
                                if (text.length > 5 && text.length < 500 &&
                                    rect.width > 0 && rect.height > 0 &&
                                    rect.top > 0 && rect.top < window.innerHeight) {
                                    candidates.push(text);
                                }
                            }
                        }
                        // Also try getting  text from the overlay area
                        const overlayEls = document.querySelectorAll(
                            '[data-pagelet] div[dir="auto"], [role="main"] div[dir="auto"]'
                        );
                        for (const el of overlayEls) {
                            const rect = el.getBoundingClientRect();
                            const text = el.innerText.trim();
                            if (text.length > 10 && text.length < 500 &&
                                rect.width > 50 && rect.height > 0 &&
                                rect.top > 0 && rect.top < window.innerHeight &&
                                !text.includes('Thích') && !text.includes('Bình luận') &&
                                !text.includes('Chia sẻ')) {
                                candidates.push(text);
                            }
                        }
                        // Pick the longest candidate (most likely the caption)
                        if (candidates.length === 0) return '';
                        candidates.sort((a, b) => b.length - a.length);
                        return candidates[0].substring(0, 300);
                    })()
                    """
                    caption = ""
                    try:
                        cap_result = self.execute_js(caption_js, profile_uuid)
                        caption = cap_result.get("result", "") or ""
                        if caption:
                            print(f"[REELS] {short_id} → Reel {reel_num} caption: {caption[:80]}...")
                    except:
                        pass

                    comment_text = self.generate_smart_comment(caption)
                    print(f"[REELS] {short_id} → Commenting: {comment_text}")

                    comment_js = """
                    (async () => {
                        const cBtnSels = [
                            '[aria-label="Bình luận"]',
                            '[aria-label="Comment"]',
                            '[aria-label="Viết bình luận"]'
                        ];
                        let cBtn = null;
                        for (const sel of cBtnSels) {
                            const btns = document.querySelectorAll(sel);
                            for (const b of btns) {
                                const rect = b.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) { cBtn = b; break; }
                            }
                            if (cBtn) break;
                        }
                        if (cBtn) {
                            cBtn.click();
                            await new Promise(r => setTimeout(r, 1500));
                        }

                        const iSels = [
                            '[contenteditable="true"][role="textbox"]',
                            '[contenteditable="true"][aria-label*="bình luận"]',
                            '[contenteditable="true"][aria-label*="comment"]',
                            'div[contenteditable="true"]'
                        ];
                        let input = null;
                        for (const sel of iSels) {
                            const inps = document.querySelectorAll(sel);
                            for (const inp of inps) {
                                const rect = inp.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) { input = inp; break; }
                            }
                            if (input) break;
                        }
                        if (!input) return JSON.stringify({ok: false, reason: 'no_comment_input'});

                        input.focus();
                        await new Promise(r => setTimeout(r, 300));
                        document.execCommand('insertText', false, '__COMMENT__');
                        await new Promise(r => setTimeout(r, 500));
                        input.dispatchEvent(new Event('input', {bubbles: true}));
                        await new Promise(r => setTimeout(r, 500));
                        input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
                        await new Promise(r => setTimeout(r, 300));
                        input.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
                        await new Promise(r => setTimeout(r, 2000));
                        return JSON.stringify({ok: true});
                    })()
                    """.replace('__COMMENT__', comment_text.replace("'", "\\'"))

                    try:
                        cmt_result = self.execute_js(comment_js, profile_uuid)
                        try:
                            cmt_data = json.loads(cmt_result.get("result", "{}"))
                        except:
                            cmt_data = {"ok": False}
                        if cmt_data.get("ok"):
                            commented += 1
                        else:
                            errors.append(f"Reel {reel_num}: comment failed")
                    except Exception as e:
                        errors.append(f"Reel {reel_num}: {str(e)}")

                    time.sleep(random.uniform(1, 2))

                # Swipe to next reel using CDP trusted keypress
                if i < count - 1:
                    print(f"[REELS] {short_id} → Swiping to next reel...")
                    self.press_key('ArrowDown', profile_uuid)
                    time.sleep(random.uniform(2, 4))

            # Done
            send_progress(watched, liked, commented, count, done=True)
            print(f"[REELS] {short_id} → Done! Watched: {watched}, Liked: {liked}, Commented: {commented}")

            # Final screenshot
            self._send_verify_screenshot(profile_uuid, telegram_chat_id,
                f"🎬 Reels hoàn thành\n👁 Xem: {watched}/{count} | ❤️ Like: {liked} | 💬 Comment: {commented}")

            if browser_opened_here:
                try:
                    self.close_browser(profile_uuid)
                except:
                    pass

            return {
                "success": True,
                "profile_uuid": profile_uuid,
                "watched": watched,
                "liked": liked,
                "commented": commented,
                "count": count,
                "errors": errors[:10]
            }

        except Exception as e:
            traceback.print_exc()
            self._send_verify_screenshot(profile_uuid, telegram_chat_id,
                                         f"❌ Reels lỗi: {str(e)[:100]}")
            if browser_opened_here:
                try:
                    self.close_browser(profile_uuid)
                except:
                    pass
            return {"success": False, "error": str(e), "profile_uuid": profile_uuid}
