"""
Skills Package - Modular FB automation skills.
Composes FBManagerAPI from all skill mixins via multiple inheritance.

Architecture:
  BaseSkill      - Singleton, CDP, navigate, scroll, execute_js, click, type, etc.
  TelegramMixin  - _telegram_edit_message, send_telegram_photo
  BrowserSkill   - open_browser, close_browser, screenshot_profile
  LoginSkill     - FB_STATUS_JS, check_fb_status, check_fb_batch, login_fb, check_login
  FeedSkill      - fb_read_feed, fb_comment, like_post, nurture
  GroupsSkill    - leave_groups, debug_groups
  ReelsSkill     - watch_reels (watch, like, comment on reels)
  VisionSkill    - Universal vision pipeline: screenshot→AI detect→click, skill cache

Usage:
  from skills import FBManagerAPI
  api = FBManagerAPI()
"""

from .base_skill import BaseSkill, SCREENSHOT_DIR
from .telegram_mixin import TelegramMixin
from .browser_skill import BrowserSkill
from .login_skill import LoginSkill
from .feed_skill import FeedSkill
from .groups_skill import GroupsSkill
from .reels_skill import ReelsSkill
from .vision_skill import VisionSkill
from .agent_skill import AgentSkill


class FBManagerAPI(BaseSkill, TelegramMixin, BrowserSkill, LoginSkill, FeedSkill, GroupsSkill, ReelsSkill, VisionSkill, AgentSkill):
    """
    Composed FB Manager API - inherits all skills.
    Singleton: FBManagerAPI() always returns the same instance.
    """
    pass
