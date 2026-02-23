"""
GroupsSkill - Leave groups, debug groups.
"""

import json
import time
import traceback
from typing import Optional, Dict, Any


class GroupsSkill:
    """Facebook group management: leave groups, debug groups."""

    def debug_groups(self, profile_uuid: str = None) -> Dict[str, Any]:
        """
        Debug: Analyze group items + menu buttons từ /groups/joins/ page.
        """
        browser_opened_here = False
        try:
            profile_uuid = profile_uuid or self.current_profile
            if not profile_uuid:
                return {"success": False, "error": "No profile specified"}

            # Auto-open browser if not already open
            cdp = self._get_cdp(profile_uuid)
            if not cdp:
                open_result = self.open_browser(profile_uuid)
                if not open_result.get('success'):
                    return {"success": False, "error": f"Cannot open browser: {open_result.get('error')}"}
                browser_opened_here = True
                time.sleep(2)

            nav_resp = self.navigate(
                "https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added",
                profile_uuid
            )
            if not nav_resp.get("success"):
                return {"success": False, "error": "Navigate failed"}

            time.sleep(3)

            login_check = self.check_login(profile_uuid)
            if not login_check.get("logged_in"):
                return {"success": False, "error": "Not logged in", "logged_in": False}

            extract_groups_js = """
            function getXPath(element) {
                if (element.id !== '')
                    return "//*[@id='" + element.id + "']";
                if (element === document.body)
                    return "//" + element.tagName.toLowerCase();

                var ix = 0;
                var siblings = element.parentNode.childNodes;
                for (var i = 0; i < siblings.length; i++) {
                    var sibling = siblings[i];
                    if (sibling === element)
                        return getXPath(element.parentNode) + '/' + element.tagName.toLowerCase() + '[' + (ix + 1) + ']';
                    if (sibling.nodeType === 1 && sibling.tagName.toLowerCase() === element.tagName.toLowerCase())
                        ix++;
                }
            }

            const groups = [];
            const potentialCards = document.querySelectorAll(
                'div[role="article"], [data-testid*="group"], div.x1iyjqo2, [data-a2="true"]'
            );
            const seen = new Set();

            potentialCards.forEach((card, idx) => {
                const groupLink = card.querySelector('a[href*="/groups/"]');
                if (!groupLink) return;

                const groupName = groupLink.innerText.trim();
                const groupHref = groupLink.href;

                if (!groupName || seen.has(groupHref)) return;
                seen.add(groupHref);

                const menuBtn = card.querySelector(
                    'button[aria-label*="More"], button[aria-label*="..."], [role="button"][aria-label*="More"], [role="menuitem"]'
                );

                let menuBtnAlt = null;
                if (!menuBtn) {
                    const allButtons = card.querySelectorAll('button, [role="button"]');
                    if (allButtons.length > 0) {
                        menuBtnAlt = allButtons[allButtons.length - 1];
                    }
                }

                const actualMenuBtn = menuBtn || menuBtnAlt;

                groups.push({
                    index: groups.length,
                    group_name: groupName,
                    group_href: groupHref,
                    group_card_classes: card.className || card.getAttribute('class') || '',
                    group_card_html: card.outerHTML.substring(0, 300),
                    menu_button_html: actualMenuBtn ? actualMenuBtn.outerHTML.substring(0, 200) : 'NOT FOUND',
                    menu_button_classes: actualMenuBtn ? actualMenuBtn.className : 'N/A',
                    menu_button_aria_label: actualMenuBtn ? actualMenuBtn.getAttribute('aria-label') : 'N/A',
                    xpath_group_card: getXPath(card),
                    xpath_menu_button: actualMenuBtn ? getXPath(actualMenuBtn) : 'N/A',
                    menu_button_visible: actualMenuBtn ? actualMenuBtn.offsetParent !== null : false
                });
            });

            return JSON.stringify({
                total_groups: groups.length,
                groups: groups.slice(0, 20),
                sample_html: potentialCards.length > 0 ? potentialCards[0].outerHTML.substring(0, 500) : "No cards found"
            });
            """

            js_result = self.execute_js(extract_groups_js, profile_uuid)
            result_str = js_result.get("result", "{}")

            try:
                result_data = json.loads(result_str)
            except:
                result_data = {"error": "Failed to parse JS result", "raw": result_str}

            return {
                "success": True,
                "profile_uuid": profile_uuid,
                "logged_in": True,
                **result_data
            }

        except Exception as e:
            traceback.print_exc()
            return {"success": False, "error": str(e), "profile_uuid": profile_uuid}

    def leave_groups(self, profile_uuid: str = None, max_groups: int = 100,
                     telegram_chat_id: str = None, telegram_message_id: int = None) -> Dict[str, Any]:
        """
        Thoát khỏi tất cả nhóm mà profile đã join.
        Flow: Open browser → Navigate groups page → scroll to load → for each group:
              click "Xem thêm" menu → click "Rời nhóm" → confirm "Rời khỏi nhóm" → Close browser
        """
        browser_opened_here = False
        try:
            profile_uuid = profile_uuid or self.current_profile
            if not profile_uuid:
                return {"success": False, "error": "No profile specified"}

            print(f"[API] leave_groups: {profile_uuid[:20]}...")

            # 0. Auto-open browser if not already open
            cdp = self._get_cdp(profile_uuid)
            if not cdp:
                print(f"[API] leave_groups: Browser not open, opening...")
                open_result = self.open_browser(profile_uuid)
                if not open_result.get('success'):
                    return {"success": False, "error": f"Cannot open browser: {open_result.get('error')}"}
                browser_opened_here = True
                time.sleep(2)

            # 1. Navigate đến trang groups join
            nav_resp = self.navigate(
                "https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added",
                profile_uuid
            )
            if not nav_resp.get("success"):
                if browser_opened_here:
                    self.close_browser(profile_uuid)
                return {"success": False, "error": "Navigate to groups join page failed"}

            time.sleep(3)

            # 2. Quick login check
            cdp = self._get_cdp(profile_uuid)
            if not cdp:
                if browser_opened_here:
                    self.close_browser(profile_uuid)
                return {"success": False, "error": "No CDP connection"}

            login_js = """
            (function() {
                let url = window.location.href;
                if (url.includes('/login') || url.includes('checkpoint') || url.includes('locked'))
                    return 'NOT_LOGGED_IN';
                let loginForm = document.querySelector('input[name="email"], input[name="pass"], #loginbutton');
                if (loginForm) return 'NOT_LOGGED_IN';
                let hasNav = document.querySelector('[aria-label*="Messenger"]') ||
                             document.querySelector('[aria-label*="Thông báo"]') ||
                             document.querySelector('[aria-label*="Tài khoản"]') ||
                             document.querySelector('[role="banner"]');
                if (hasNav) return 'LOGGED_IN';
                if (url.includes('groups')) return 'LOGGED_IN';
                return 'UNKNOWN';
            })()
            """
            login_status = cdp._evaluate_js(login_js)
            if str(login_status) == 'NOT_LOGGED_IN':
                # Screenshot to show error
                if telegram_chat_id:
                    try:
                        sc = self.screenshot_profile(profile_uuid)
                        if sc.get("success"):
                            self.send_telegram_photo(sc["screenshot"], telegram_chat_id,
                                f"❌ {profile_uuid[:12]} - Chưa đăng nhập!")
                    except:
                        pass
                return {"success": False, "error": "Profile not logged in. Cannot leave groups", "logged_in": False}

            # 3. Scroll to load all groups + collect them
            scroll_and_collect_js = """
            (async () => {
                document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                await new Promise(r => setTimeout(r, 300));

                let prevCount = 0;
                for (let i = 0; i < 60; i++) {
                    window.scrollBy(0, 1000);
                    await new Promise(r => setTimeout(r, 400));
                    const items = document.querySelectorAll('div[role="listitem"]');
                    if (items.length === prevCount && i > 5) break;
                    prevCount = items.length;
                }

                window.scrollTo(0, 0);
                await new Promise(r => setTimeout(r, 500));

                const items = document.querySelectorAll('div[role="listitem"]');
                const groups = [];

                for (const item of items) {
                    const menuBtn = item.querySelector('[aria-label="Xem thêm"]');
                    if (!menuBtn) continue;

                    let name = '', href = '';
                    const links = item.querySelectorAll('a[href*="/groups/"]');
                    for (const l of links) {
                        const t = l.textContent.trim();
                        if (t.length > 2 && !t.startsWith('Xem')) {
                            name = t;
                            href = l.href;
                            break;
                        }
                    }
                    if (!href) continue;

                    groups.push({name: name.substring(0, 80), href: href});
                }

                return JSON.stringify(groups);
            })()
            """

            groups_result = self.execute_js(scroll_and_collect_js, profile_uuid)
            groups_str = groups_result.get("result", "[]")
            try:
                groups = json.loads(groups_str)
            except:
                groups = []

            if not groups:
                return {"success": True, "left": 0, "failed": 0, "note": "No groups found on page"}

            print(f"[API] Found {len(groups)} groups on page, will process up to {max_groups}")

            # Helper to send progress to Telegram
            def send_progress(left, failed, skipped, total, current_name="", done=False):
                if not telegram_chat_id or not telegram_message_id:
                    return
                processed = left + failed + skipped
                status = "✅ Hoàn thành!" if done else f"⏳ Đang xử lý: {current_name}"
                text = f"📊 *Thoát nhóm - {profile_uuid[:12]}*\n"
                text += f"{status}\n\n"
                text += f"Tổng: {total} | ✅ Thoát: {left} | ❌ Lỗi: {failed} | ⏭ Bỏ qua: {skipped}\n"
                text += f"Tiến độ: {processed}/{min(total, max_groups)}"
                self._telegram_edit_message(telegram_chat_id, telegram_message_id, text)

            # 4. Leave each group one by one
            left_count = 0
            failed_count = 0
            skipped_count = 0
            errors = []

            for i, group in enumerate(groups[:max_groups]):
                group_name = group.get('name', 'Unknown')
                group_href = group.get('href', '')

                print(f"[API] [{i+1}/{min(len(groups), max_groups)}] Leaving: {group_name}")

                try:
                    # Step A: Find the group's listitem and click its "Xem thêm" button
                    open_menu_js = """
                    (async () => {
                        document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                        await new Promise(r => setTimeout(r, 300));
                        document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                        await new Promise(r => setTimeout(r, 300));

                        const TARGET_HREF = '__HREF__';
                        const items = document.querySelectorAll('div[role="listitem"]');
                        let targetItem = null;

                        for (const item of items) {
                            const link = item.querySelector('a[href="' + TARGET_HREF + '"]');
                            if (link) { targetItem = item; break; }
                        }

                        if (!targetItem) {
                            for (const item of items) {
                                const links = item.querySelectorAll('a[href*="/groups/"]');
                                for (const l of links) {
                                    if (l.href === TARGET_HREF || TARGET_HREF.includes(l.href) || l.href.includes(TARGET_HREF.replace(/\\/$/, ''))) {
                                        targetItem = item;
                                        break;
                                    }
                                }
                                if (targetItem) break;
                            }
                        }

                        if (!targetItem) return JSON.stringify({ok: false, reason: 'listitem_not_found'});

                        const menuBtn = targetItem.querySelector('[aria-label="Xem thêm"]');
                        if (!menuBtn) return JSON.stringify({ok: false, reason: 'menu_btn_not_found'});

                        menuBtn.scrollIntoView({block: 'center'});
                        await new Promise(r => setTimeout(r, 400));
                        menuBtn.click();

                        await new Promise(r => setTimeout(r, 1500));
                        return JSON.stringify({ok: true});
                    })()
                    """.replace('__HREF__', group_href.replace("'", "\\'"))

                    menu_result = self.execute_js(open_menu_js, profile_uuid)
                    try:
                        menu_data = json.loads(menu_result.get("result", "{}"))
                    except:
                        menu_data = {"ok": False, "reason": "parse_error"}

                    if not menu_data.get("ok"):
                        reason = menu_data.get("reason", "unknown")
                        print(f"[API]   Skip: {reason}")
                        skipped_count += 1
                        send_progress(left_count, failed_count, skipped_count, len(groups), group_name)
                        continue

                    # Step B: Find and click "Rời nhóm" or "Hủy yêu cầu" menu item
                    click_leave_js = """
                    (async () => {
                        const menuItems = document.querySelectorAll('[role="menuitem"]');
                        let leaveItem = null;
                        let cancelItem = null;

                        for (const mi of menuItems) {
                            const text = mi.textContent.trim();
                            if (text === 'Rời nhóm' || text === 'Leave group' || text === 'Leave Group') {
                                leaveItem = mi;
                                break;
                            }
                            if (text === 'Hủy yêu cầu' || text === 'Cancel request' || text === 'Cancel Request') {
                                cancelItem = mi;
                            }
                        }

                        if (leaveItem) {
                            leaveItem.click();
                            await new Promise(r => setTimeout(r, 2000));
                            return JSON.stringify({ok: true, action: 'leave'});
                        }

                        if (cancelItem) {
                            cancelItem.click();
                            await new Promise(r => setTimeout(r, 2000));
                            return JSON.stringify({ok: true, action: 'cancel_request'});
                        }

                        // Log all menu items for debugging
                        const allTexts = [];
                        for (const mi of menuItems) allTexts.push(mi.textContent.trim().substring(0, 50));

                        document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                        await new Promise(r => setTimeout(r, 300));
                        return JSON.stringify({ok: false, reason: 'leave_not_found', menu_items: allTexts});
                    })()
                    """

                    leave_result = self.execute_js(click_leave_js, profile_uuid)
                    try:
                        leave_data = json.loads(leave_result.get("result", "{}"))
                    except:
                        leave_data = {"ok": False, "reason": "parse_error"}

                    if not leave_data.get("ok"):
                        reason = leave_data.get("reason", "unknown")
                        menu_items = leave_data.get("menu_items", [])
                        print(f"[API]   Failed: {reason} | menu: {menu_items}")
                        failed_count += 1
                        errors.append(f"{group_name}: {reason}")
                        send_progress(left_count, failed_count, skipped_count, len(groups), group_name)
                        continue

                    action = leave_data.get("action", "leave")

                    if action == "cancel_request":
                        # "Hủy yêu cầu" was clicked — check if a confirm dialog appears
                        confirm_cancel_js = """
                        (async () => {
                            const dialog = document.querySelector('[role="dialog"]');
                            if (dialog) {
                                const btns = dialog.querySelectorAll('[role="button"]');
                                for (const b of btns) {
                                    const t = b.textContent.trim();
                                    const a = b.getAttribute('aria-label') || '';
                                    if (t === 'Xác nhận' || t === 'Confirm' || t === 'Hủy yêu cầu' ||
                                        a === 'Xác nhận' || a === 'Confirm') {
                                        b.click();
                                        await new Promise(r => setTimeout(r, 1500));
                                        return JSON.stringify({ok: true, confirmed: true});
                                    }
                                }
                                // Close dialog if no confirm button found
                                document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                                await new Promise(r => setTimeout(r, 500));
                            }
                            return JSON.stringify({ok: true, confirmed: false, note: 'no_dialog_or_auto_cancelled'});
                        })()
                        """
                        cancel_result = self.execute_js(confirm_cancel_js, profile_uuid)
                        left_count += 1
                        print(f"[API]   ✓ Cancelled pending request")
                        send_progress(left_count, failed_count, skipped_count, len(groups), group_name)
                        time.sleep(1)
                        continue

                    # Step C: Confirm "Rời khỏi nhóm" in dialog
                    confirm_js = """
                    (async () => {
                        let confirmBtn = document.querySelector('[aria-label="Rời khỏi nhóm"][role="button"]');

                        if (!confirmBtn) {
                            confirmBtn = document.querySelector('[aria-label="Leave group"][role="button"]');
                        }

                        if (!confirmBtn) {
                            const btns = document.querySelectorAll('[role="button"]');
                            for (const b of btns) {
                                const t = b.textContent.trim();
                                if (t === 'Rời khỏi nhóm' || t === 'Leave group' || t === 'Leave Group') {
                                    confirmBtn = b;
                                    break;
                                }
                            }
                        }

                        if (!confirmBtn) {
                            const dialog = document.querySelector('[role="dialog"]');
                            if (!dialog) return JSON.stringify({ok: true, note: 'no_dialog_left_directly'});
                            return JSON.stringify({ok: false, reason: 'confirm_btn_not_found'});
                        }

                        confirmBtn.click();
                        await new Promise(r => setTimeout(r, 2000));

                        // Close report dialog if it appears
                        const reportDialog = document.querySelector('[role="dialog"]');
                        if (reportDialog) {
                            let closeBtn = reportDialog.querySelector('[aria-label="Đóng"]') ||
                                           reportDialog.querySelector('[aria-label="Close"]');
                            if (!closeBtn) {
                                const btns = reportDialog.querySelectorAll('[role="button"]');
                                for (const b of btns) {
                                    const label = b.getAttribute('aria-label') || '';
                                    const text = b.textContent.trim();
                                    if (label === 'Đóng' || label === 'Close' || text === '✕' || text === '×') {
                                        closeBtn = b;
                                        break;
                                    }
                                }
                            }
                            if (closeBtn) {
                                closeBtn.click();
                                await new Promise(r => setTimeout(r, 800));
                            } else {
                                document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                                await new Promise(r => setTimeout(r, 500));
                            }
                        }

                        return JSON.stringify({ok: true, confirmed: true});
                    })()
                    """

                    confirm_result = self.execute_js(confirm_js, profile_uuid)
                    try:
                        confirm_data = json.loads(confirm_result.get("result", "{}"))
                    except:
                        confirm_data = {"ok": False, "reason": "parse_error"}

                    if confirm_data.get("ok"):
                        left_count += 1
                        print(f"[API]   ✓ Left successfully")
                    else:
                        failed_count += 1
                        errors.append(f"{group_name}: confirm failed - {confirm_data.get('reason')}")
                        print(f"[API]   ✗ Confirm failed: {confirm_data.get('reason')}")
                        # Screenshot on failure for debugging
                        if telegram_chat_id:
                            try:
                                sc = self.screenshot_profile(profile_uuid)
                                if sc.get("success"):
                                    self.send_telegram_photo(sc["screenshot"], telegram_chat_id,
                                        f"❌ Lỗi thoát nhóm: {group_name[:40]}\n{confirm_data.get('reason')}")
                            except:
                                pass
                        self.execute_js(
                            "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));'ok'",
                            profile_uuid
                        )
                        time.sleep(0.5)

                    send_progress(left_count, failed_count, skipped_count, len(groups), group_name)
                    time.sleep(1)

                except Exception as e:
                    failed_count += 1
                    errors.append(f"{group_name}: {str(e)}")
                    print(f"[API]   ✗ Error: {e}")
                    traceback.print_exc()
                    try:
                        self.execute_js(
                            "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));'ok'",
                            profile_uuid
                        )
                    except:
                        pass
                    send_progress(left_count, failed_count, skipped_count, len(groups), group_name)
                    time.sleep(1)

            # Send final progress
            send_progress(left_count, failed_count, skipped_count, len(groups), done=True)

            # Screenshot on completion and send to Telegram
            screenshot_path = None
            if telegram_chat_id:
                try:
                    sc_result = self.screenshot_profile(profile_uuid)
                    if sc_result.get("success"):
                        screenshot_path = sc_result["screenshot"]
                        caption = f"✅ Thoát nhóm hoàn thành\n"
                        caption += f"Tổng: {len(groups)} | Thoát: {left_count} | Lỗi: {failed_count} | Bỏ qua: {skipped_count}"
                        self.send_telegram_photo(screenshot_path, telegram_chat_id, caption)
                        print(f"[API] Screenshot sent to Telegram")
                except Exception as e:
                    print(f"[API] Screenshot/Telegram error: {e}")

            # Close browser if we opened it
            if browser_opened_here:
                try:
                    self.close_browser(profile_uuid)
                    print(f"[API] leave_groups: Browser closed")
                except:
                    pass

            return {
                "success": True,
                "profile_uuid": profile_uuid,
                "left": left_count,
                "failed": failed_count,
                "skipped": skipped_count,
                "total_groups": len(groups),
                "processed": min(len(groups), max_groups),
                "errors": errors[:10],
                "screenshot": screenshot_path
            }

        except Exception as e:
            traceback.print_exc()
            if browser_opened_here:
                try:
                    self.close_browser(profile_uuid)
                except:
                    pass
            return {"success": False, "error": str(e), "profile_uuid": profile_uuid}
