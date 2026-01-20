"""
SQLite Database Manager for FB Manager Pro
Quản lý dữ liệu với SQLite thay vì JSON files
"""
import sqlite3
import os
from datetime import datetime
from typing import List, Dict, Optional
from contextlib import contextmanager

# Database path
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "fbmanager.db")


def ensure_data_dir():
    """Đảm bảo thư mục data tồn tại"""
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)


@contextmanager
def get_connection():
    """Context manager để quản lý kết nối database"""
    ensure_data_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # Trả về dict-like rows
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def init_database():
    """Khởi tạo database và tạo các bảng"""
    with get_connection() as conn:
        cursor = conn.cursor()

        # ============ CATEGORIES TABLE ============
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ============ CONTENTS TABLE ============
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS contents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT,
                image_path TEXT,
                stickers TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
            )
        """)

        # ============ PROFILES TABLE ============
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT UNIQUE,
                name TEXT,
                browser TEXT,
                os TEXT,
                status TEXT DEFAULT 'stopped',
                proxy TEXT,
                note TEXT,
                tags TEXT,
                local_notes TEXT,
                fb_uid TEXT,
                fb_name TEXT,
                check_open INTEGER DEFAULT 0,
                folder_id TEXT,
                folder_name TEXT,
                last_sync TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ============ SCRIPTS TABLE ============
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                type TEXT DEFAULT 'python',
                content TEXT,
                hidemium_key TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ============ POSTS TABLE ============
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                title TEXT,
                target_likes INTEGER DEFAULT 0,
                target_comments INTEGER DEFAULT 0,
                like_count INTEGER DEFAULT 0,
                comment_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ============ SETTINGS TABLE ============
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ============ PAGES TABLE ============
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_uuid TEXT NOT NULL,
                page_id TEXT NOT NULL,
                page_name TEXT,
                page_url TEXT,
                category TEXT,
                follower_count INTEGER DEFAULT 0,
                role TEXT DEFAULT 'admin',
                note TEXT,
                is_selected INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(profile_uuid, page_id)
            )
        """)

        # ============ GROUPS TABLE ============
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_uuid TEXT NOT NULL,
                group_id TEXT NOT NULL,
                group_name TEXT,
                group_url TEXT,
                member_count INTEGER DEFAULT 0,
                is_selected INTEGER DEFAULT 0,
                last_post_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(profile_uuid, group_id)
            )
        """)

        # ============ POST HISTORY TABLE ============
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS post_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_uuid TEXT NOT NULL,
                group_id TEXT,
                content_id INTEGER,
                post_url TEXT,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ============ SCHEDULES TABLE ============
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                folder_id TEXT,
                folder_name TEXT,
                time_slots TEXT,
                content_category_id INTEGER,
                image_folder TEXT,
                group_ids TEXT,
                delay_min INTEGER DEFAULT 30,
                delay_max INTEGER DEFAULT 60,
                is_active INTEGER DEFAULT 1,
                post_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                last_run_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Add new columns to schedules if they don't exist
        new_schedule_columns = [
            ("profile_uuid", "TEXT"),
            ("group_id", "INTEGER"),
            ("group_name", "TEXT"),
            ("group_url", "TEXT"),
            ("content_id", "INTEGER"),
            ("content_title", "TEXT"),
            ("days_of_week", "TEXT"),
            ("posts_per_run", "INTEGER DEFAULT 1"),
            ("delay_minutes", "INTEGER DEFAULT 5"),
            ("status", "TEXT DEFAULT 'active'"),
            ("last_run", "TEXT"),
            ("next_run", "TEXT")
        ]
        for col_name, col_type in new_schedule_columns:
            try:
                cursor.execute(f"ALTER TABLE schedules ADD COLUMN {col_name} {col_type}")
            except:
                pass  # Column already exists

        # ============ REEL SCHEDULES TABLE ============
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reel_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_uuid TEXT NOT NULL,
                page_id INTEGER,
                page_name TEXT,
                video_path TEXT NOT NULL,
                cover_path TEXT,
                caption TEXT,
                hashtags TEXT,
                scheduled_time TIMESTAMP NOT NULL,
                delay_min INTEGER DEFAULT 30,
                delay_max INTEGER DEFAULT 60,
                status TEXT DEFAULT 'pending',
                reel_url TEXT,
                error_message TEXT,
                executed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ============ POSTED REELS TABLE ============
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS posted_reels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_uuid TEXT NOT NULL,
                page_id TEXT,
                page_name TEXT,
                reel_url TEXT,
                caption TEXT,
                hashtags TEXT,
                video_path TEXT,
                status TEXT DEFAULT 'success',
                error_message TEXT,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Tạo category mặc định nếu chưa có
        cursor.execute("SELECT COUNT(*) FROM categories")
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                "INSERT INTO categories (name, description) VALUES (?, ?)",
                ("Mặc định", "Category mặc định")
            )

        # Tạo indexes để tăng tốc query
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_contents_category ON contents(category_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_profiles_uuid ON profiles(uuid)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_scripts_type ON scripts(type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pages_profile ON pages(profile_uuid)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_groups_profile ON groups(profile_uuid)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_post_history_profile ON post_history(profile_uuid)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_post_history_created ON post_history(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_post_history_status ON post_history(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reel_schedules_profile ON reel_schedules(profile_uuid)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reel_schedules_status ON reel_schedules(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_posted_reels_profile ON posted_reels(profile_uuid)")


def row_to_dict(row) -> Dict:
    """Chuyển sqlite3.Row thành dict"""
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows) -> List[Dict]:
    """Chuyển list of sqlite3.Row thành list of dict"""
    return [dict(row) for row in rows]


# ==================== CATEGORIES ====================

def get_categories() -> List[Dict]:
    """Lấy danh sách tất cả categories"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM categories ORDER BY id")
        return rows_to_list(cursor.fetchall())


def get_category_by_id(category_id: int) -> Optional[Dict]:
    """Lấy category theo ID"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM categories WHERE id = ?", (category_id,))
        return row_to_dict(cursor.fetchone())


def save_category(data: Dict) -> Dict:
    """Lưu category (tạo mới hoặc cập nhật)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        if data.get('id'):
            cursor.execute("""
                UPDATE categories
                SET name = ?, description = ?, updated_at = ?
                WHERE id = ?
            """, (data.get('name'), data.get('description', ''), now, data['id']))
            return get_category_by_id(data['id'])
        else:
            cursor.execute("""
                INSERT INTO categories (name, description, created_at, updated_at)
                VALUES (?, ?, ?, ?)
            """, (data.get('name'), data.get('description', ''), now, now))
            data['id'] = cursor.lastrowid
            return data


def delete_category(category_id: int) -> bool:
    """Xóa category (không cho xóa category mặc định id=1)"""
    if category_id == 1:
        return False
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM contents WHERE category_id = ?", (category_id,))
        cursor.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        return cursor.rowcount > 0


# ==================== CONTENTS ====================

def get_contents(category_id: int = None) -> List[Dict]:
    """Lấy danh sách contents, có thể lọc theo category"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if category_id:
            cursor.execute(
                "SELECT * FROM contents WHERE category_id = ? ORDER BY id DESC",
                (category_id,)
            )
        else:
            cursor.execute("SELECT * FROM contents ORDER BY id DESC")
        return rows_to_list(cursor.fetchall())


def get_content_by_id(content_id: int) -> Optional[Dict]:
    """Lấy content theo ID"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM contents WHERE id = ?", (content_id,))
        return row_to_dict(cursor.fetchone())


def save_content(data: Dict) -> Dict:
    """Lưu content (tạo mới hoặc cập nhật)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        if data.get('id'):
            cursor.execute("""
                UPDATE contents
                SET category_id = ?, title = ?, content = ?,
                    image_path = ?, stickers = ?, updated_at = ?
                WHERE id = ?
            """, (
                data.get('category_id', 1),
                data.get('title', ''),
                data.get('content', ''),
                data.get('image_path', ''),
                data.get('stickers', ''),
                now,
                data['id']
            ))
            return get_content_by_id(data['id'])
        else:
            cursor.execute("""
                INSERT INTO contents (category_id, title, content, image_path, stickers, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get('category_id', 1),
                data.get('title', ''),
                data.get('content', ''),
                data.get('image_path', ''),
                data.get('stickers', ''),
                now, now
            ))
            data['id'] = cursor.lastrowid
            return data


def delete_content(content_id: int) -> bool:
    """Xóa content"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM contents WHERE id = ?", (content_id,))
        return cursor.rowcount > 0


def get_contents_count(category_id: int = None) -> int:
    """Đếm số contents"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if category_id:
            cursor.execute("SELECT COUNT(*) FROM contents WHERE category_id = ?", (category_id,))
        else:
            cursor.execute("SELECT COUNT(*) FROM contents")
        return cursor.fetchone()[0]


# ==================== PROFILES ====================

def get_profiles() -> List[Dict]:
    """Lấy danh sách profiles"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM profiles ORDER BY id DESC")
        return rows_to_list(cursor.fetchall())


def get_profile_by_uuid(uuid: str) -> Optional[Dict]:
    """Lấy profile theo UUID"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM profiles WHERE uuid = ?", (uuid,))
        return row_to_dict(cursor.fetchone())


def save_profile(data: Dict) -> Dict:
    """Lưu profile"""
    import json as json_module

    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        # Convert lists to JSON strings
        tags = data.get('tags', '')
        if isinstance(tags, list):
            tags = json_module.dumps(tags, ensure_ascii=False)

        existing = get_profile_by_uuid(data.get('uuid', ''))

        if existing:
            # Update - giữ lại local data nếu không được cung cấp
            cursor.execute("""
                UPDATE profiles
                SET name = ?, browser = ?, os = ?, status = ?,
                    proxy = ?, note = ?, tags = ?,
                    local_notes = ?, fb_uid = ?, fb_name = ?, check_open = ?,
                    folder_id = ?, folder_name = ?,
                    last_sync = ?, updated_at = ?
                WHERE uuid = ?
            """, (
                data.get('name', ''),
                data.get('browser', ''),
                data.get('os', ''),
                data.get('status', 'stopped'),
                data.get('proxy', ''),
                data.get('note', ''),
                tags,
                data.get('local_notes', existing.get('local_notes', '')),
                data.get('fb_uid', existing.get('fb_uid', '')),
                data.get('fb_name', existing.get('fb_name', '')),
                data.get('check_open', existing.get('check_open', 0)),
                data.get('folder_id', existing.get('folder_id', '')),
                data.get('folder_name', existing.get('folder_name', '')),
                now, now,
                data['uuid']
            ))
            return get_profile_by_uuid(data['uuid'])
        else:
            # Insert
            cursor.execute("""
                INSERT INTO profiles (uuid, name, browser, os, status, proxy, note, tags,
                    local_notes, fb_uid, fb_name, check_open, folder_id, folder_name,
                    last_sync, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get('uuid', ''),
                data.get('name', ''),
                data.get('browser', ''),
                data.get('os', ''),
                data.get('status', 'stopped'),
                data.get('proxy', ''),
                data.get('note', ''),
                tags,
                data.get('local_notes', ''),
                data.get('fb_uid', ''),
                data.get('fb_name', ''),
                data.get('check_open', 0),
                data.get('folder_id', ''),
                data.get('folder_name', ''),
                now, now, now
            ))
            data['id'] = cursor.lastrowid
            return data


def delete_profile(uuid: str) -> bool:
    """Xóa profile"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM profiles WHERE uuid = ?", (uuid,))
        return cursor.rowcount > 0


def sync_profiles(profiles_from_api: List[Dict]):
    """Đồng bộ profiles từ API vào database, giữ lại thông tin local"""
    for profile in profiles_from_api:
        uuid = profile.get('uuid')
        existing = get_profile_by_uuid(uuid)

        if existing:
            # Giữ lại thông tin local
            profile['local_notes'] = existing.get('local_notes', '')
            profile['fb_uid'] = existing.get('fb_uid', '')
            profile['fb_name'] = existing.get('fb_name', '')
            profile['check_open'] = existing.get('check_open', 0)
        else:
            profile['check_open'] = 0

        save_profile(profile)


def update_profile_local(uuid: str, data: Dict) -> bool:
    """Cập nhật thông tin local của profile (notes, fb_uid, fb_name, etc.)"""
    existing = get_profile_by_uuid(uuid)
    if not existing:
        return False

    # Merge data
    existing.update(data)
    save_profile(existing)
    return True


def update_profile_status(uuid: str, status: str):
    """Update profile running status"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
            UPDATE profiles SET status = ?, updated_at = ? WHERE uuid = ?
        """, (status, now, uuid))


# ==================== SCRIPTS ====================

def get_scripts(script_type: str = None) -> List[Dict]:
    """Lấy danh sách scripts"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if script_type:
            cursor.execute(
                "SELECT * FROM scripts WHERE type = ? ORDER BY id DESC",
                (script_type,)
            )
        else:
            cursor.execute("SELECT * FROM scripts ORDER BY id DESC")
        return rows_to_list(cursor.fetchall())


def get_script_by_id(script_id: int) -> Optional[Dict]:
    """Lấy script theo ID"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM scripts WHERE id = ?", (script_id,))
        return row_to_dict(cursor.fetchone())


def save_script(data: Dict) -> Dict:
    """Lưu script"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        if data.get('id'):
            cursor.execute("""
                UPDATE scripts
                SET name = ?, description = ?, type = ?, content = ?,
                    hidemium_key = ?, updated_at = ?
                WHERE id = ?
            """, (
                data.get('name', ''),
                data.get('description', ''),
                data.get('type', 'python'),
                data.get('content', ''),
                data.get('hidemium_key', ''),
                now, data['id']
            ))
            return get_script_by_id(data['id'])
        else:
            cursor.execute("""
                INSERT INTO scripts (name, description, type, content, hidemium_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get('name', ''),
                data.get('description', ''),
                data.get('type', 'python'),
                data.get('content', ''),
                data.get('hidemium_key', ''),
                now, now
            ))
            data['id'] = cursor.lastrowid
            return data


def delete_script(script_id: int) -> bool:
    """Xóa script"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scripts WHERE id = ?", (script_id,))
        return cursor.rowcount > 0


# ==================== POSTS ====================

def get_posts() -> List[Dict]:
    """Lấy danh sách posts"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM posts ORDER BY id DESC")
        return rows_to_list(cursor.fetchall())


def get_post_by_id(post_id: int) -> Optional[Dict]:
    """Lấy post theo ID"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        return row_to_dict(cursor.fetchone())


def save_post(data: Dict) -> Dict:
    """Lưu post"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        if data.get('id'):
            cursor.execute("""
                UPDATE posts
                SET url = ?, title = ?, target_likes = ?, target_comments = ?,
                    like_count = ?, comment_count = ?, status = ?, updated_at = ?
                WHERE id = ?
            """, (
                data.get('url', ''),
                data.get('title', ''),
                data.get('target_likes', 0),
                data.get('target_comments', 0),
                data.get('like_count', 0),
                data.get('comment_count', 0),
                data.get('status', 'pending'),
                now, data['id']
            ))
            return get_post_by_id(data['id'])
        else:
            cursor.execute("""
                INSERT INTO posts (url, title, target_likes, target_comments, like_count, comment_count, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get('url', ''),
                data.get('title', ''),
                data.get('target_likes', 0),
                data.get('target_comments', 0),
                data.get('like_count', 0),
                data.get('comment_count', 0),
                data.get('status', 'pending'),
                now, now
            ))
            data['id'] = cursor.lastrowid
            return data


def delete_post(post_id: int) -> bool:
    """Xóa post"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        return cursor.rowcount > 0


def update_post_stats(post_id: int, likes: int = 0, comments: int = 0):
    """Cập nhật thống kê post"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
            UPDATE posts
            SET like_count = like_count + ?, comment_count = comment_count + ?, updated_at = ?
            WHERE id = ?
        """, (likes, comments, now, post_id))


# ==================== SETTINGS ====================

def get_settings() -> Dict:
    """Lấy tất cả settings"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM settings")
        return {row['key']: row['value'] for row in cursor.fetchall()}


def get_setting(key: str, default=None):
    """Lấy một setting"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row['value'] if row else default


def set_setting(key: str, value):
    """Lưu một setting"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
            INSERT OR REPLACE INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
        """, (key, str(value), now))


def save_settings(settings: Dict):
    """Lưu nhiều settings"""
    for key, value in settings.items():
        set_setting(key, value)


# ==================== PAGES ====================

def get_pages(profile_uuid: str = None) -> List[Dict]:
    """Lấy danh sách pages, có thể lọc theo profile"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if profile_uuid:
            cursor.execute(
                "SELECT * FROM pages WHERE profile_uuid = ? ORDER BY page_name",
                (profile_uuid,)
            )
        else:
            cursor.execute("SELECT * FROM pages ORDER BY page_name")
        return rows_to_list(cursor.fetchall())


def get_pages_for_profiles(profile_uuids: List[str]) -> List[Dict]:
    """Lấy danh sách pages từ nhiều profiles"""
    if not profile_uuids:
        return []
    with get_connection() as conn:
        cursor = conn.cursor()
        placeholders = ','.join(['?' for _ in profile_uuids])
        query = f"SELECT * FROM pages WHERE profile_uuid IN ({placeholders}) ORDER BY profile_uuid, page_name"
        cursor.execute(query, profile_uuids)
        return rows_to_list(cursor.fetchall())


def get_page_by_id(page_id: int) -> Optional[Dict]:
    """Lấy page theo ID"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pages WHERE id = ?", (page_id,))
        return row_to_dict(cursor.fetchone())


def save_page(data: Dict) -> Dict:
    """Lưu page (tạo mới hoặc cập nhật)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        profile_uuid = data.get('profile_uuid', '')
        page_id = data.get('page_id', '')

        # Check if page already exists for this profile
        cursor.execute(
            "SELECT id FROM pages WHERE profile_uuid = ? AND page_id = ?",
            (profile_uuid, page_id)
        )
        existing = cursor.fetchone()

        if existing:
            # Update
            cursor.execute("""
                UPDATE pages
                SET page_name = ?, page_url = ?, category = ?, follower_count = ?,
                    role = ?, note = ?, is_selected = ?, updated_at = ?
                WHERE id = ?
            """, (
                data.get('page_name', ''),
                data.get('page_url', ''),
                data.get('category', ''),
                data.get('follower_count', 0),
                data.get('role', 'admin'),
                data.get('note', ''),
                data.get('is_selected', 0),
                now,
                existing['id']
            ))
            data['id'] = existing['id']
        else:
            # Insert
            cursor.execute("""
                INSERT INTO pages (profile_uuid, page_id, page_name, page_url, category,
                    follower_count, role, note, is_selected, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                profile_uuid,
                page_id,
                data.get('page_name', ''),
                data.get('page_url', ''),
                data.get('category', ''),
                data.get('follower_count', 0),
                data.get('role', 'admin'),
                data.get('note', ''),
                data.get('is_selected', 0),
                now, now
            ))
            data['id'] = cursor.lastrowid

        return data


def delete_page(page_id: int) -> bool:
    """Xóa page"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pages WHERE id = ?", (page_id,))
        return cursor.rowcount > 0


def delete_pages_bulk(page_ids: List[int]) -> int:
    """Xóa nhiều pages"""
    if not page_ids:
        return 0
    with get_connection() as conn:
        cursor = conn.cursor()
        placeholders = ','.join(['?' for _ in page_ids])
        cursor.execute(f"DELETE FROM pages WHERE id IN ({placeholders})", page_ids)
        return cursor.rowcount


def update_page_selection(page_id: int, is_selected: int) -> bool:
    """Cập nhật trạng thái chọn page"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
            UPDATE pages SET is_selected = ?, updated_at = ? WHERE id = ?
        """, (is_selected, now, page_id))
        return cursor.rowcount > 0


def sync_pages(profile_uuid: str, pages_from_scan: List[Dict]):
    """Đồng bộ pages từ scan vào database"""
    saved_count = 0
    for page in pages_from_scan:
        page['profile_uuid'] = profile_uuid
        result = save_page(page)
        if result.get('id'):
            saved_count += 1
    return saved_count


def clear_pages(profile_uuid: str) -> bool:
    """Xóa tất cả pages của profile"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pages WHERE profile_uuid = ?", (profile_uuid,))
        return cursor.rowcount > 0


def get_pages_count(profile_uuid: str = None) -> int:
    """Đếm số pages"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if profile_uuid:
            cursor.execute("SELECT COUNT(*) FROM pages WHERE profile_uuid = ?", (profile_uuid,))
        else:
            cursor.execute("SELECT COUNT(*) FROM pages")
        return cursor.fetchone()[0]


# ==================== GROUPS ====================

def get_groups(profile_uuid: str = None) -> List[Dict]:
    """Lấy danh sách groups, có thể lọc theo profile"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if profile_uuid:
            cursor.execute(
                "SELECT * FROM groups WHERE profile_uuid = ? ORDER BY group_name",
                (profile_uuid,)
            )
        else:
            cursor.execute("SELECT * FROM groups ORDER BY group_name")
        return rows_to_list(cursor.fetchall())


def get_groups_for_profiles(profile_uuids: List[str]) -> List[Dict]:
    """Lấy danh sách groups từ nhiều profiles"""
    if not profile_uuids:
        return []
    with get_connection() as conn:
        cursor = conn.cursor()
        placeholders = ','.join(['?' for _ in profile_uuids])
        cursor.execute(
            f"SELECT * FROM groups WHERE profile_uuid IN ({placeholders}) ORDER BY profile_uuid, group_name",
            profile_uuids
        )
        return rows_to_list(cursor.fetchall())


def get_group_by_id(group_id: int) -> Optional[Dict]:
    """Lấy group theo ID"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM groups WHERE id = ?", (group_id,))
        return row_to_dict(cursor.fetchone())


def save_group(data: Dict) -> Dict:
    """Lưu group (tạo mới hoặc cập nhật)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        # Check if group already exists for this profile
        cursor.execute(
            "SELECT id FROM groups WHERE profile_uuid = ? AND group_id = ?",
            (data.get('profile_uuid'), data.get('group_id'))
        )
        existing = cursor.fetchone()

        if existing:
            # Update
            cursor.execute("""
                UPDATE groups
                SET group_name = ?, group_url = ?, member_count = ?,
                    is_selected = ?, updated_at = ?
                WHERE id = ?
            """, (
                data.get('group_name', ''),
                data.get('group_url', ''),
                data.get('member_count', 0),
                data.get('is_selected', 0),
                now,
                existing['id']
            ))
            data['id'] = existing['id']
        else:
            # Insert
            cursor.execute("""
                INSERT INTO groups (profile_uuid, group_id, group_name, group_url,
                    member_count, is_selected, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get('profile_uuid', ''),
                data.get('group_id', ''),
                data.get('group_name', ''),
                data.get('group_url', ''),
                data.get('member_count', 0),
                data.get('is_selected', 0),
                now, now
            ))
            data['id'] = cursor.lastrowid

        return data


def delete_group(group_id: int) -> bool:
    """Xóa group"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        return cursor.rowcount > 0


def update_group_selection(group_id: int, is_selected: int) -> bool:
    """Cập nhật trạng thái chọn group"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
            UPDATE groups SET is_selected = ?, updated_at = ? WHERE id = ?
        """, (is_selected, now, group_id))
        return cursor.rowcount > 0


def get_selected_groups(profile_uuid: str) -> List[Dict]:
    """Lấy danh sách groups đã chọn của profile"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM groups WHERE profile_uuid = ? AND is_selected = 1 ORDER BY group_name",
            (profile_uuid,)
        )
        return rows_to_list(cursor.fetchall())


def sync_groups(profile_uuid: str, groups_from_scan: List[Dict]):
    """Đồng bộ groups từ scan vào database"""
    for group in groups_from_scan:
        group['profile_uuid'] = profile_uuid
        save_group(group)


def clear_groups(profile_uuid: str) -> bool:
    """Xóa tất cả groups của profile"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM groups WHERE profile_uuid = ?", (profile_uuid,))
        return cursor.rowcount > 0


# ==================== POST HISTORY ====================

def get_post_history(profile_uuid: str = None, limit: int = 100) -> List[Dict]:
    """Lấy lịch sử đăng bài"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if profile_uuid:
            cursor.execute(
                "SELECT * FROM post_history WHERE profile_uuid = ? ORDER BY created_at DESC LIMIT ?",
                (profile_uuid, limit)
            )
        else:
            cursor.execute("SELECT * FROM post_history ORDER BY created_at DESC LIMIT ?", (limit,))
        return rows_to_list(cursor.fetchall())


def save_post_history(data: Dict) -> Dict:
    """Lưu lịch sử đăng bài"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        cursor.execute("""
            INSERT INTO post_history (profile_uuid, group_id, content_id, post_url, status, error_message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get('profile_uuid', ''),
            data.get('group_id', ''),
            data.get('content_id'),
            data.get('post_url', ''),
            data.get('status', 'pending'),
            data.get('error_message', ''),
            now
        ))
        data['id'] = cursor.lastrowid
        return data


def get_post_history_filtered(
    profile_uuid: str,
    date_from: str = None,
    status: str = None,
    limit: int = 50,
    offset: int = 0
) -> List[Dict]:
    """Lấy lịch sử đăng bài với filtering"""
    with get_connection() as conn:
        cursor = conn.cursor()

        conditions = ["profile_uuid = ?"]
        params = [profile_uuid]

        if date_from:
            conditions.append("DATE(created_at) >= ?")
            params.append(date_from)

        if status:
            conditions.append("status = ?")
            params.append(status)

        conditions.append("post_url IS NOT NULL AND post_url != ''")

        where_clause = " AND ".join(conditions)

        query = f"""
            SELECT id, profile_uuid, group_id, content_id, post_url, status, error_message, created_at
            FROM post_history
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        cursor.execute(query, params)
        return rows_to_list(cursor.fetchall())


def get_post_history_count(
    profile_uuid: str,
    date_from: str = None,
    status: str = None
) -> int:
    """Đếm số lượng post history với filtering"""
    with get_connection() as conn:
        cursor = conn.cursor()

        conditions = ["profile_uuid = ?"]
        params = [profile_uuid]

        if date_from:
            conditions.append("DATE(created_at) >= ?")
            params.append(date_from)

        if status:
            conditions.append("status = ?")
            params.append(status)

        conditions.append("post_url IS NOT NULL AND post_url != ''")

        where_clause = " AND ".join(conditions)

        cursor.execute(f"SELECT COUNT(*) FROM post_history WHERE {where_clause}", params)
        return cursor.fetchone()[0]


# ==================== SCHEDULES ====================

def get_schedules(status: str = None, active_only: bool = False) -> List[Dict]:
    """Lấy danh sách schedules"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if status:
            cursor.execute("SELECT * FROM schedules WHERE status = ? ORDER BY id", (status,))
        elif active_only:
            cursor.execute("SELECT * FROM schedules WHERE is_active = 1 OR status = 'active' ORDER BY id")
        else:
            cursor.execute("SELECT * FROM schedules ORDER BY id")
        return rows_to_list(cursor.fetchall())


def get_schedule(schedule_id: int) -> Optional[Dict]:
    """Lấy schedule theo ID"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        row = cursor.fetchone()
        return row_to_dict(row)


def save_schedule(data: Dict) -> Dict:
    """Lưu schedule (insert hoặc update) - hỗ trợ cả cấu trúc cũ và mới"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        if data.get('id'):
            # Update - sử dụng update_schedule
            update_data = {k: v for k, v in data.items() if k != 'id'}
            update_data['updated_at'] = now
            update_schedule(data['id'], update_data)
        else:
            # Insert - hỗ trợ cả 2 cấu trúc
            # Cấu trúc mới (với group_id, content_id)
            if data.get('group_id') or data.get('profile_uuid'):
                cursor.execute("""
                    INSERT INTO schedules (
                        name, profile_uuid, group_id, group_name, group_url,
                        content_id, content_title, time_slots, days_of_week,
                        posts_per_run, delay_minutes, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    data.get('name', data.get('group_name', '')),
                    data.get('profile_uuid', ''),
                    data.get('group_id'),
                    data.get('group_name', ''),
                    data.get('group_url', ''),
                    data.get('content_id'),
                    data.get('content_title', ''),
                    data.get('time_slots', ''),
                    data.get('days_of_week', ''),
                    data.get('posts_per_run', 1),
                    data.get('delay_minutes', 5),
                    data.get('status', 'active'),
                    now, now
                ))
            else:
                # Cấu trúc cũ
                cursor.execute("""
                    INSERT INTO schedules (name, folder_id, folder_name, time_slots,
                        content_category_id, image_folder, group_ids, delay_min, delay_max,
                        is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    data.get('name', ''),
                    data.get('folder_id', ''),
                    data.get('folder_name', ''),
                    data.get('time_slots', ''),
                    data.get('content_category_id'),
                    data.get('image_folder', ''),
                    data.get('group_ids', ''),
                    data.get('delay_min', 30),
                    data.get('delay_max', 60),
                    data.get('is_active', 1),
                    now, now
                ))
            data['id'] = cursor.lastrowid
        return data


def delete_schedule(schedule_id: int) -> bool:
    """Xóa schedule"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        return cursor.rowcount > 0


def update_schedule(schedule_id: int, data: Dict) -> bool:
    """Cập nhật schedule theo ID"""
    with get_connection() as conn:
        cursor = conn.cursor()
        updates = []
        values = []

        for key, value in data.items():
            if key != 'id':
                updates.append(f"{key} = ?")
                values.append(value)

        if not updates:
            return False

        values.append(schedule_id)
        cursor.execute(
            f"UPDATE schedules SET {', '.join(updates)} WHERE id = ?",
            values
        )
        return cursor.rowcount > 0


def update_schedule_stats(schedule_id: int, post_count: int = None,
                          success_count: int = None, error_count: int = None):
    """Cập nhật thống kê schedule"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        updates = ["last_run_at = ?"]
        values = [now]

        if post_count is not None:
            updates.append("post_count = post_count + ?")
            values.append(post_count)
        if success_count is not None:
            updates.append("success_count = success_count + ?")
            values.append(success_count)
        if error_count is not None:
            updates.append("error_count = error_count + ?")
            values.append(error_count)

        values.append(schedule_id)
        cursor.execute(f"UPDATE schedules SET {', '.join(updates)} WHERE id = ?", values)


# ==================== REEL SCHEDULES ====================

def get_reel_schedules(profile_uuid: str = None, status: str = None) -> List[Dict]:
    """Lấy danh sách reel schedules"""
    with get_connection() as conn:
        cursor = conn.cursor()

        query = "SELECT * FROM reel_schedules WHERE 1=1"
        params = []

        if profile_uuid:
            query += " AND profile_uuid = ?"
            params.append(profile_uuid)

        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY scheduled_time ASC"
        cursor.execute(query, params)
        return rows_to_list(cursor.fetchall())


def get_pending_reel_schedules() -> List[Dict]:
    """Lấy danh sách reels cần đăng (đã đến giờ)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
            SELECT * FROM reel_schedules
            WHERE status = 'pending' AND scheduled_time <= ?
            ORDER BY scheduled_time ASC
        """, (now,))
        return rows_to_list(cursor.fetchall())


def save_reel_schedule(data: Dict) -> Dict:
    """Lưu reel schedule mới"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        cursor.execute("""
            INSERT INTO reel_schedules
            (profile_uuid, page_id, page_name, video_path, cover_path,
             caption, hashtags, scheduled_time, delay_min, delay_max, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get('profile_uuid'),
            data.get('page_id'),
            data.get('page_name', ''),
            data.get('video_path'),
            data.get('cover_path', ''),
            data.get('caption', ''),
            data.get('hashtags', ''),
            data.get('scheduled_time'),
            data.get('delay_min', 30),
            data.get('delay_max', 60),
            data.get('status', 'pending'),
            now
        ))
        data['id'] = cursor.lastrowid
        return data


def update_reel_schedule(schedule_id: int, data: Dict) -> bool:
    """Cập nhật reel schedule"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        updates = ["updated_at = ?"]
        values = [now]

        for key in ['status', 'reel_url', 'error_message', 'executed_at']:
            if key in data:
                updates.append(f"{key} = ?")
                values.append(data[key])

        values.append(schedule_id)
        cursor.execute(f"""
            UPDATE reel_schedules SET {', '.join(updates)} WHERE id = ?
        """, values)
        return cursor.rowcount > 0


def delete_reel_schedule(schedule_id: int) -> bool:
    """Xóa reel schedule"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM reel_schedules WHERE id = ?", (schedule_id,))
        return cursor.rowcount > 0


def get_reel_history(profile_uuid: str = None, limit: int = 50, offset: int = 0) -> List[Dict]:
    """Lấy lịch sử đăng reels"""
    with get_connection() as conn:
        cursor = conn.cursor()

        query = "SELECT * FROM reel_schedules WHERE status IN ('completed', 'failed')"
        params = []

        if profile_uuid:
            query += " AND profile_uuid = ?"
            params.append(profile_uuid)

        query += " ORDER BY executed_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(query, params)
        return rows_to_list(cursor.fetchall())


# ==================== POSTED REELS ====================

def save_posted_reel(data: Dict) -> Dict:
    """Lưu Reel đã đăng vào lịch sử"""
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        cursor.execute("""
            INSERT INTO posted_reels (profile_uuid, page_id, page_name, reel_url,
                caption, hashtags, video_path, status, error_message, posted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get('profile_uuid', ''),
            data.get('page_id', ''),
            data.get('page_name', ''),
            data.get('reel_url', ''),
            data.get('caption', ''),
            data.get('hashtags', ''),
            data.get('video_path', ''),
            data.get('status', 'success'),
            data.get('error_message', ''),
            now
        ))
        data['id'] = cursor.lastrowid
        return data


def get_posted_reels(profile_uuid: str = None, page_id: str = None, limit: int = 100, offset: int = 0) -> List[Dict]:
    """Lấy danh sách Reels đã đăng"""
    with get_connection() as conn:
        cursor = conn.cursor()

        query = "SELECT * FROM posted_reels WHERE 1=1"
        params = []

        if profile_uuid:
            query += " AND profile_uuid = ?"
            params.append(profile_uuid)

        if page_id:
            query += " AND page_id = ?"
            params.append(page_id)

        query += " ORDER BY posted_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(query, params)
        return rows_to_list(cursor.fetchall())


def get_posted_reels_count(profile_uuid: str = None, page_id: str = None) -> int:
    """Đếm số Reels đã đăng"""
    with get_connection() as conn:
        cursor = conn.cursor()

        query = "SELECT COUNT(*) FROM posted_reels WHERE 1=1"
        params = []

        if profile_uuid:
            query += " AND profile_uuid = ?"
            params.append(profile_uuid)

        if page_id:
            query += " AND page_id = ?"
            params.append(page_id)

        cursor.execute(query, params)
        return cursor.fetchone()[0]


def delete_posted_reel(reel_id: int) -> bool:
    """Xóa một Reel khỏi lịch sử"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM posted_reels WHERE id = ?", (reel_id,))
        return cursor.rowcount > 0


def clear_posted_reels(profile_uuid: str = None) -> int:
    """Xóa lịch sử Reels đã đăng"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if profile_uuid:
            cursor.execute("DELETE FROM posted_reels WHERE profile_uuid = ?", (profile_uuid,))
        else:
            cursor.execute("DELETE FROM posted_reels")
        return cursor.rowcount


# Khởi tạo database khi import module
init_database()
