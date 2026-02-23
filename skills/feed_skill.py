"""
FeedSkill - Facebook feed reading, commenting, liking, nurturing.
"""

import json
import os
import base64
import time
import random
import traceback
from datetime import datetime
from typing import Optional, Dict, Any, List
from concurrent.futures import ThreadPoolExecutor, as_completed
from skills.base_skill import SCREENSHOT_DIR


class FeedSkill:
    """Facebook feed browsing, commenting, and batch nurturing."""

    def like_post(self, profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Tìm và like bài viết"""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "error": "No active connection. Call /open_browser first."}

        like_js = '''
        (function() {
            let posts = document.querySelectorAll('[data-ad-rendering-role="story_message"]');
            if (posts.length === 0) {
                posts = document.querySelectorAll('[role="article"]');
            }

            for (let post of posts) {
                const rect = post.getBoundingClientRect();
                if (rect.bottom < 0 || rect.top > window.innerHeight) continue;

                const buttons = post.querySelectorAll('div[role="button"]');
                for (let btn of buttons) {
                    const pressed = btn.getAttribute('aria-pressed');
                    if (pressed === 'true') continue;

                    const spans = btn.querySelectorAll('span');
                    for (let span of spans) {
                        const text = (span.innerText || '').trim();
                        if (text === 'Thích' || text === 'Like') {
                            btn.scrollIntoView({block: 'center'});
                            btn.click();
                            return JSON.stringify({
                                success: true, liked: true,
                                buttonText: text, postTop: Math.round(rect.top)
                            });
                        }
                    }
                }
            }

            return JSON.stringify({
                success: false, liked: false,
                error: "No Like button found",
                postsFound: posts.length
            });
        })()
        '''
        try:
            result = cdp._evaluate_js(like_js)
            return json.loads(result)
        except Exception as e:
            return {"success": False, "error": f"Like failed: {e}"}

    def check_result(self, selector: str, expected_text: str = None,
                     profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """Kiểm tra kết quả action"""
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "error": "No active connection. Call /open_browser first."}

        js_code = f'''
        (function() {{
            const el = document.querySelector('{selector}');
            if (!el) return JSON.stringify({{found: false}});
            return JSON.stringify({{
                found: true,
                text: (el.innerText || '').substring(0, 200),
                visible: el.getBoundingClientRect().height > 0
            }});
        }})()
        '''
        try:
            result = cdp._evaluate_js(js_code)
            data = json.loads(result)
            if expected_text and data.get('found'):
                data['match'] = expected_text.lower() in data.get('text', '').lower()
            return {"success": True, **data}
        except Exception as e:
            return {"success": False, "error": f"Check failed: {e}"}

    def fb_read_feed(self, profile_uuid: Optional[str] = None,
                     scroll_count: int = 3) -> Dict[str, Any]:
        """
        Đọc Facebook news feed. Auto-open browser nếu chưa mở.
        Navigate tới FB nếu cần, cuộn trang,
        trích xuất các bài viết: author, content, reactions, comment count.
        """
        browser_opened_here = False
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            print(f"[FEED] Browser not open for {profile_uuid[:20] if profile_uuid else '?'}, opening...")
            open_result = self.open_browser(profile_uuid)
            if not open_result.get('success'):
                return {"success": False, "error": f"Cannot open browser: {open_result.get('error')}"}
            browser_opened_here = True
            time.sleep(3)
            cdp = self._get_cdp(profile_uuid)
            if not cdp:
                return {"success": False, "error": "No CDP connection after opening browser"}

        try:
            current_url = cdp._evaluate_js("window.location.href")
            if not current_url or 'facebook.com' not in str(current_url):
                cdp.navigate('https://www.facebook.com/', wait_load=True)
                time.sleep(5)

            # Wait for feed to load
            for wait_attempt in range(15):
                loaded = cdp._evaluate_js('''
                    (function() {
                        const arts = document.querySelectorAll('[role="article"]');
                        let realCount = 0;
                        for (let a of arts) {
                            const label = (a.getAttribute('aria-label') || '').toLowerCase();
                            if (!label.includes('loading') && !label.includes('tải') && a.innerText.trim().length > 50) {
                                realCount++;
                            }
                        }
                        return realCount;
                    })()
                ''')
                if loaded and int(loaded) > 0:
                    break
                cdp._evaluate_js(f"window.scrollBy(0, {random.randint(300, 500)})")
                time.sleep(2)

            for i in range(scroll_count):
                cdp._evaluate_js(f"window.scrollBy(0, {random.randint(400, 700)})")
                time.sleep(random.uniform(1.5, 3.0))

            cdp._evaluate_js("window.scrollTo(0, 0)")
            time.sleep(1)

            extract_js = r'''
            (function() {
                const posts = [];
                let items = document.querySelectorAll('div[aria-posinset]');
                if (items.length === 0) {
                    items = document.querySelectorAll('[role="article"]');
                }

                let postIdx = 0;
                for (let i = 0; i < items.length && postIdx < 10; i++) {
                    const item = items[i];
                    const text = item.innerText.trim();
                    if (text.length < 30) continue;

                    const btns = item.querySelectorAll('div[role="button"]');
                    let isSuggestion = false;
                    for (let b of btns) {
                        const bt = (b.innerText || '').trim();
                        if (bt === 'Thêm bạn bè' || bt === 'Add Friend') { isSuggestion = true; break; }
                    }
                    if (isSuggestion) continue;

                    let hasLikeBtn = false;
                    let hasCommentBtn = false;
                    for (let b of btns) {
                        const bt = (b.innerText || '').trim().toLowerCase();
                        if (bt === 'thích' || bt === 'like') hasLikeBtn = true;
                        if (bt === 'bình luận' || bt === 'comment' || bt === 'comments') hasCommentBtn = true;
                    }
                    if (!hasLikeBtn) continue;

                    let author = '';
                    const links = item.querySelectorAll('a');
                    for (let a of links) {
                        const href = a.href || '';
                        const t = (a.innerText || '').trim();
                        if (t.length > 1 && t.length < 80 && !t.includes('http') && !t.startsWith('#')) {
                            if (href.includes('facebook.com/') && !href.includes('hashtag') &&
                                !href.includes('/photos/') && !href.includes('/videos/')) {
                                author = t;
                                break;
                            }
                        }
                    }

                    let content = '';
                    const dirAutos = item.querySelectorAll('div[dir="auto"]');
                    for (let d of dirAutos) {
                        const t = (d.innerText || '').trim();
                        if (t.length > 5 && t !== author && !t.startsWith('#')) {
                            content = t;
                            break;
                        }
                    }
                    if (!content) {
                        const storyEls = item.querySelectorAll(
                            '[data-ad-rendering-role="story_message"], [data-ad-comet-rendering-role="story_message"]'
                        );
                        if (storyEls.length > 0) content = (storyEls[0].innerText || '').trim();
                    }

                    let reactions = '';
                    for (let b of btns) {
                        const bt = (b.innerText || '').trim();
                        if (bt.match(/cảm xúc|reaction/i)) { reactions = bt; break; }
                    }
                    if (!reactions) {
                        const labeled = item.querySelectorAll('[aria-label]');
                        for (let el of labeled) {
                            const lbl = el.getAttribute('aria-label') || '';
                            if (lbl.match(/reaction|người|thích|like|haha|love/i)) { reactions = lbl; break; }
                        }
                    }

                    let commentCount = '';
                    for (let b of btns) {
                        const bt = (b.innerText || '').trim();
                        if (bt.match(/^\d+\s*(bình luận|comment|lượt)/i)) { commentCount = bt; break; }
                    }
                    if (!commentCount) {
                        const spans = item.querySelectorAll('span');
                        for (let s of spans) {
                            const t = (s.innerText || '').trim();
                            if (t.match(/^\d+\s*(bình luận|comment|lượt bình luận)/i)) { commentCount = t; break; }
                        }
                    }

                    let hasMedia = item.querySelector('img[src*="scontent"], img[src*="fbcdn"], video') !== null;

                    posts.push({
                        index: postIdx,
                        posinset: item.getAttribute('aria-posinset'),
                        author: author.substring(0, 60),
                        content: content.substring(0, 300),
                        reactions: reactions.substring(0, 60),
                        comments: commentCount,
                        has_media: hasMedia
                    });
                    postIdx++;
                }

                return JSON.stringify({
                    total_items: items.length,
                    posts: posts,
                    url: window.location.href
                });
            })()
            '''

            result = cdp._evaluate_js(extract_js)
            try:
                data = json.loads(result)
                return {"success": True, **data}
            except:
                return {"success": False, "error": "Failed to extract posts", "raw": str(result)[:1000]}

        except Exception as e:
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    def fb_comment(self, post_index: int, comment_text: str,
                   profile_uuid: Optional[str] = None) -> Dict[str, Any]:
        """
        Comment vào bài viết trên Facebook feed.
        post_index: index bài viết (từ fb_read_feed)
        comment_text: nội dung comment
        """
        cdp = self._get_cdp(profile_uuid)
        if not cdp:
            return {"success": False, "error": "No active connection. Call /open_browser first."}

        try:
            # Helper JS to find real posts
            find_post_js = f'''
            (function() {{
                let items = document.querySelectorAll('div[aria-posinset]');
                if (items.length === 0) items = document.querySelectorAll('[role="article"]');
                const realPosts = [];
                for (let item of items) {{
                    const btns = item.querySelectorAll('div[role="button"]');
                    let isPost = false;
                    for (let b of btns) {{
                        const bt = (b.innerText || '').trim().toLowerCase();
                        if (bt === 'thích' || bt === 'like') {{ isPost = true; break; }}
                    }}
                    if (isPost) realPosts.push(item);
                }}
                if ({post_index} >= realPosts.length) {{
                    return JSON.stringify({{found: false, total: realPosts.length}});
                }}
                return JSON.stringify({{found: true, total: realPosts.length}});
            }})()
            '''

            check_result = cdp._evaluate_js(find_post_js)
            try:
                cr = json.loads(check_result)
                if not cr.get('found'):
                    return {"success": False, "error": f"Post index {post_index} not found. Total real posts: {cr.get('total', 0)}"}
            except:
                return {"success": False, "error": "Failed to find post"}

            # Scroll to post
            scroll_js = f'''
            (function() {{
                let items = document.querySelectorAll('div[aria-posinset]');
                if (items.length === 0) items = document.querySelectorAll('[role="article"]');
                const realPosts = [];
                for (let item of items) {{
                    const btns = item.querySelectorAll('div[role="button"]');
                    let isPost = false;
                    for (let b of btns) {{
                        const bt = (b.innerText || '').trim().toLowerCase();
                        if (bt === 'thích' || bt === 'like') {{ isPost = true; break; }}
                    }}
                    if (isPost) realPosts.push(item);
                }}
                const post = realPosts[{post_index}];
                if (!post) return JSON.stringify({{error: "Post not found"}});
                post.scrollIntoView({{block: 'center', behavior: 'smooth'}});
                return JSON.stringify({{scrolled: true}});
            }})()
            '''
            cdp._evaluate_js(scroll_js)
            time.sleep(random.uniform(1.0, 2.0))

            # Click comment button
            click_comment_js = f'''
            (function() {{
                let items = document.querySelectorAll('div[aria-posinset]');
                if (items.length === 0) items = document.querySelectorAll('[role="article"]');
                const realPosts = [];
                for (let item of items) {{
                    const btns = item.querySelectorAll('div[role="button"]');
                    let isPost = false;
                    for (let b of btns) {{
                        const bt = (b.innerText || '').trim().toLowerCase();
                        if (bt === 'thích' || bt === 'like') {{ isPost = true; break; }}
                    }}
                    if (isPost) realPosts.push(item);
                }}
                const post = realPosts[{post_index}];
                if (!post) return JSON.stringify({{clicked: false, error: "Post not found"}});
                const buttons = post.querySelectorAll('div[role="button"]');
                for (let btn of buttons) {{
                    const bt = (btn.innerText || '').trim().toLowerCase();
                    if (bt === 'bình luận' || bt === 'comment' || bt === 'comments') {{
                        btn.click();
                        return JSON.stringify({{clicked: true, buttonText: bt}});
                    }}
                }}
                return JSON.stringify({{clicked: false, error: "Comment button not found"}});
            }})()
            '''

            click_result = cdp._evaluate_js(click_comment_js)
            try:
                cr2 = json.loads(click_result)
                if not cr2.get('clicked'):
                    return {"success": False, "error": cr2.get('error', 'Failed to click comment button')}
            except:
                return {"success": False, "error": "Failed to click comment"}

            time.sleep(random.uniform(1.5, 2.5))

            # Find and focus comment input
            find_input_js = f'''
            (function() {{
                let items = document.querySelectorAll('div[aria-posinset]');
                if (items.length === 0) items = document.querySelectorAll('[role="article"]');
                const realPosts = [];
                for (let item of items) {{
                    const btns = item.querySelectorAll('div[role="button"]');
                    let isPost = false;
                    for (let b of btns) {{
                        const bt = (b.innerText || '').trim().toLowerCase();
                        if (bt === 'thích' || bt === 'like') {{ isPost = true; break; }}
                    }}
                    if (isPost) realPosts.push(item);
                }}
                const post = realPosts[{post_index}];
                if (!post) return JSON.stringify({{found: false, error: "Post not found"}});
                let input = post.querySelector('[contenteditable="true"][role="textbox"]');
                if (!input) input = post.querySelector('[role="textbox"]');
                if (!input) {{
                    const forms = post.querySelectorAll('form');
                    for (let f of forms) {{
                        input = f.querySelector('[contenteditable="true"], [role="textbox"]');
                        if (input) break;
                    }}
                }}
                if (!input) {{
                    const parent = post.parentElement;
                    if (parent) input = parent.querySelector('[contenteditable="true"][role="textbox"]');
                }}
                if (!input) {{
                    const allBoxes = document.querySelectorAll('[contenteditable="true"][role="textbox"]');
                    if (allBoxes.length > 0) input = allBoxes[allBoxes.length - 1];
                }}
                if (input) {{
                    input.focus();
                    input.click();
                    return JSON.stringify({{found: true, tag: input.tagName}});
                }}
                return JSON.stringify({{found: false, error: "Comment input not found"}});
            }})()
            '''

            find_result = cdp._evaluate_js(find_input_js)
            try:
                fr = json.loads(find_result)
                if not fr.get('found'):
                    return {"success": False, "error": fr.get('error', 'Comment input not found')}
            except:
                return {"success": False, "error": "Failed to find comment input"}

            time.sleep(random.uniform(0.5, 1.0))

            # Type comment word-by-word
            words = comment_text.split(' ')
            for i, word in enumerate(words):
                text_to_type = word if i == 0 else ' ' + word
                cdp._send_command('Input.insertText', {'text': text_to_type})
                time.sleep(random.uniform(0.1, 0.3))

            time.sleep(random.uniform(0.5, 1.0))

            # Press Enter to submit
            cdp._send_command('Input.dispatchKeyEvent', {
                'type': 'keyDown', 'key': 'Enter', 'code': 'Enter',
                'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13
            })
            time.sleep(0.05)
            cdp._send_command('Input.dispatchKeyEvent', {
                'type': 'keyUp', 'key': 'Enter', 'code': 'Enter',
                'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13
            })

            time.sleep(3)

            # Verify comment posted
            verified = False
            verify_detail = "not_checked"
            verify_words = comment_text.split()[:3]
            verify_snippet = ' '.join(verify_words)
            verify_js = f'''
            (function() {{
                let bodyText = document.body.innerText || '';
                let found = bodyText.includes("{verify_snippet}");
                let errorPhrases = ['thử lại', 'try again', 'không thể', "couldn't",
                                    'lỗi', 'error', 'bị chặn', 'blocked'];
                let hasError = false;
                let errorText = '';
                let toasts = document.querySelectorAll('[role="alert"], [role="status"]');
                for (let t of toasts) {{
                    let tt = (t.innerText || '').toLowerCase();
                    for (let e of errorPhrases) {{
                        if (tt.includes(e)) {{ hasError = true; errorText = tt.substring(0, 100); break; }}
                    }}
                }}
                let commentBoxes = document.querySelectorAll('[contenteditable="true"][role="textbox"]');
                let boxEmpty = true;
                for (let box of commentBoxes) {{
                    if ((box.innerText || '').trim().length > 2) boxEmpty = false;
                }}
                return JSON.stringify({{found: found, hasError: hasError, errorText: errorText, boxEmpty: boxEmpty}});
            }})()
            '''
            try:
                vr = cdp._evaluate_js(verify_js)
                vdata = json.loads(vr)
                if vdata.get('hasError'):
                    verified = False
                    verify_detail = f"error_toast:{vdata.get('errorText', '')}"
                elif vdata.get('found'):
                    verified = True
                    verify_detail = "comment_found_in_dom"
                elif vdata.get('boxEmpty'):
                    verified = True
                    verify_detail = "comment_box_cleared"
                else:
                    verified = False
                    verify_detail = "comment_not_found_in_dom"
            except:
                verify_detail = "verify_js_failed"

            # Take screenshot
            screenshot_path = None
            try:
                ss_result = cdp.take_screenshot()
                if ss_result.success and ss_result.data:
                    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                    v_tag = 'ok' if verified else 'unverified'
                    filename = f"comment_{post_index}_{v_tag}_{ts}.png"
                    screenshot_path = os.path.join(SCREENSHOT_DIR, filename)
                    img_data = ss_result.data
                    if isinstance(img_data, str):
                        img_data = base64.b64decode(img_data)
                    with open(screenshot_path, 'wb') as f:
                        f.write(img_data)
            except:
                pass

            result = {
                "success": True,
                "verified": verified,
                "verify_detail": verify_detail,
                "post_index": post_index,
                "comment": comment_text,
                "message": "Comment posted" + (" & verified" if verified else " (unverified)")
            }
            if screenshot_path:
                result["screenshot"] = screenshot_path
                result["screenshot_url"] = f"http://127.0.0.1:8899/screenshots/{os.path.basename(screenshot_path)}"

            return result

        except Exception as e:
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    # ============================================================
    # BATCH NURTURE
    # ============================================================

    def _nurture_single_profile(self, profile_uuid: str, scroll_count: int = 3,
                                 comments_per_profile: int = 2,
                                 comment_style: str = "positive") -> Dict[str, Any]:
        """
        Nurture 1 profile: open → CHECK LOGIN → read feed → comment N posts → close.
        """
        steps_done = []
        fb_status = None
        try:
            open_result = self.open_browser(profile_uuid)
            if not open_result.get('success'):
                return {"success": False, "profile_uuid": profile_uuid,
                        "error": f"Open failed: {open_result.get('error')}",
                        "steps": steps_done, "fb_status": None}
            steps_done.append("opened")

            time.sleep(2)
            nav_result = self.navigate("https://www.facebook.com/", profile_uuid)
            if not nav_result.get('success'):
                self.close_browser(profile_uuid)
                return {"success": False, "profile_uuid": profile_uuid,
                        "error": f"Navigate failed: {nav_result.get('error')}",
                        "steps": steps_done, "fb_status": None}
            steps_done.append("navigated")
            time.sleep(5)

            # Check login status
            cdp = self._get_cdp(profile_uuid)
            if cdp:
                for attempt in range(3):
                    try:
                        fb_status = cdp._evaluate_js(self.FB_STATUS_JS)
                    except:
                        fb_status = 'ERROR'
                    if fb_status and str(fb_status).startswith(('LIVE', 'DIE', 'LOCKED', '2FA', 'NOT_LOGGED_IN')):
                        break
                    time.sleep(2)

                status_clean = str(fb_status).split(':')[0] if fb_status else 'UNKNOWN'
                steps_done.append(f"login_check:{status_clean}")

                if status_clean != 'LIVE':
                    self.close_browser(profile_uuid)
                    return {
                        "success": False, "profile_uuid": profile_uuid,
                        "error": f"Profile not LIVE: {fb_status}",
                        "fb_status": str(fb_status),
                        "steps": steps_done
                    }

            feed_result = self.fb_read_feed(profile_uuid, scroll_count=scroll_count)
            if not feed_result.get('success'):
                self.close_browser(profile_uuid)
                return {"success": False, "profile_uuid": profile_uuid,
                        "error": f"Feed read failed: {feed_result.get('error')}",
                        "steps": steps_done, "fb_status": str(fb_status)}
            steps_done.append("feed_read")

            posts = feed_result.get('posts', [])
            total_posts = len(posts)

            comments_made = []
            if total_posts > 0 and comments_per_profile > 0:
                n_comments = min(comments_per_profile, total_posts)
                post_indices = random.sample(range(total_posts), n_comments)

                for idx in post_indices:
                    # Get post content to generate relevant comment
                    post_info = posts[idx] if idx < len(posts) else {}
                    post_content = post_info.get('content', '') or post_info.get('author', '')
                    comment_text = self.generate_smart_comment(post_content)
                    print(f"[NURTURE] Post {idx}: '{post_content[:60]}' → Comment: {comment_text}")
                    try:
                        comment_result = self.fb_comment(idx, comment_text, profile_uuid)
                        comments_made.append({
                            "post_index": idx,
                            "comment": comment_text,
                            "success": comment_result.get('success', False),
                            "verified": comment_result.get('verified', False),
                            "verify_detail": comment_result.get('verify_detail', ''),
                            "error": comment_result.get('error')
                        })
                        time.sleep(random.uniform(2, 5))
                    except Exception as ce:
                        comments_made.append({
                            "post_index": idx, "comment": comment_text,
                            "success": False, "verified": False, "error": str(ce)
                        })

            steps_done.append(f"commented_{len(comments_made)}")

            self.close_browser(profile_uuid)
            steps_done.append("closed")

            successful_comments = sum(1 for c in comments_made if c.get('success'))
            verified_comments = sum(1 for c in comments_made if c.get('verified'))

            return {
                "success": True,
                "profile_uuid": profile_uuid,
                "fb_status": str(fb_status),
                "total_posts": total_posts,
                "comments_attempted": len(comments_made),
                "comments_success": successful_comments,
                "comments_verified": verified_comments,
                "comments": comments_made,
                "steps": steps_done
            }

        except Exception as e:
            traceback.print_exc()
            try:
                self.close_browser(profile_uuid)
            except:
                pass
            return {"success": False, "profile_uuid": profile_uuid,
                    "error": str(e), "steps": steps_done}

    def fb_nurture_batch(self, profile_uuids: List[str],
                          max_workers: int = 3,
                          scroll_count: int = 3,
                          comments_per_profile: int = 2,
                          comment_style: str = "positive") -> Dict[str, Any]:
        """
        Nurture nhiều profiles SONG SONG (parallel).
        """
        results_map = {}
        workers = min(max_workers, len(profile_uuids))
        print(f"[API] fb_nurture_batch: {len(profile_uuids)} profiles, {workers} workers parallel")

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_uuid = {
                executor.submit(
                    self._nurture_single_profile,
                    uuid, scroll_count, comments_per_profile, comment_style
                ): uuid
                for uuid in profile_uuids
            }
            for future in as_completed(future_to_uuid):
                uuid = future_to_uuid[future]
                try:
                    result = future.result(timeout=300)
                except Exception as e:
                    result = {
                        "success": False, "profile_uuid": uuid,
                        "error": str(e), "steps": []
                    }
                results_map[uuid] = result
                status = "OK" if result.get('success') else "FAIL"
                print(f"[API] fb_nurture_batch: {uuid[:20]}... → {status}")

        results = [results_map.get(uuid, {"success": False, "profile_uuid": uuid}) for uuid in profile_uuids]

        success_count = sum(1 for r in results if r.get('success'))
        total_comments = sum(r.get('comments_success', 0) for r in results)
        total_verified = sum(r.get('comments_verified', 0) for r in results)
        not_live = sum(1 for r in results if not r.get('success') and 'not LIVE' in r.get('error', '').lower())

        return {
            "success": True,
            "total_profiles": len(profile_uuids),
            "results": results,
            "summary": {
                "success": success_count,
                "failed": len(profile_uuids) - success_count,
                "not_logged_in": not_live,
                "total_comments": total_comments,
                "total_verified": total_verified
            },
            "parallel": True,
            "workers": workers
        }
