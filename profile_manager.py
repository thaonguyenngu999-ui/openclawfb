"""
Profile State Manager — quản lý trạng thái profile tập trung.

Chức năng:
1. Track profile nào đang được dùng bởi task nào
2. Giới hạn tổng số profile mở đồng thời (MAX_CONCURRENT)
3. Ngăn 2 task cùng mở/dùng 1 profile
4. Check profile running trước khi mở mới
5. Tự động cleanup khi task xong
"""
import threading
import time
from typing import Dict, Optional, Set, List
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ProfileLock:
    """Info về 1 profile đang bị lock bởi task"""
    profile_uuid: str
    task_name: str       # vd: "bulk_login", "check_fb", "agent_execute"
    locked_at: float     # time.time()
    owner_id: str = ""   # để trace ai đang giữ


class ProfileManager:
    """Singleton quản lý trạng thái profile toàn hệ thống."""

    MAX_CONCURRENT = 5  # Tối đa 5 profile mở cùng lúc

    def __init__(self):
        self._lock = threading.Lock()
        self._active: Dict[str, ProfileLock] = {}  # uuid → ProfileLock
        self._global_sem = threading.Semaphore(self.MAX_CONCURRENT)
        self._batch_lock = threading.Lock()  # chỉ 1 batch operation tại 1 thời điểm
        self._current_batch: Optional[str] = None  # task name hiện tại

    # ──── Core: acquire / release ────

    def acquire(self, profile_uuid: str, task_name: str,
                timeout: float = 120, owner_id: str = "") -> bool:
        """
        Yêu cầu quyền sử dụng profile.
        - Chờ global semaphore (giới hạn đồng thời)
        - Check xem profile có đang bị lock bởi task khác không
        Returns True nếu ok, False nếu hết timeout / bị conflict.
        """
        # Chờ slot trong global semaphore
        got_sem = self._global_sem.acquire(timeout=timeout)
        if not got_sem:
            print(f"[PM] TIMEOUT waiting for slot: {profile_uuid[:20]} task={task_name}")
            return False

        # Check & lock profile
        with self._lock:
            existing = self._active.get(profile_uuid)
            if existing:
                # Profile đang bị lock bởi task khác
                age = time.time() - existing.locked_at
                # Nếu lock quá 5 phút → coi như zombie, cho override
                if age < 300:
                    self._global_sem.release()
                    print(f"[PM] CONFLICT: {profile_uuid[:20]} locked by '{existing.task_name}' "
                          f"({age:.0f}s ago). Denied for '{task_name}'")
                    return False
                else:
                    print(f"[PM] ZOMBIE: {profile_uuid[:20]} locked by '{existing.task_name}' "
                          f"for {age:.0f}s → overriding")

            self._active[profile_uuid] = ProfileLock(
                profile_uuid=profile_uuid,
                task_name=task_name,
                locked_at=time.time(),
                owner_id=owner_id
            )
            count = len(self._active)
            print(f"[PM] ACQUIRED: {profile_uuid[:20]} for '{task_name}' "
                  f"(active={count}/{self.MAX_CONCURRENT})")
            return True

    def release(self, profile_uuid: str):
        """Giải phóng profile sau khi task xong."""
        with self._lock:
            if profile_uuid in self._active:
                info = self._active.pop(profile_uuid)
                duration = time.time() - info.locked_at
                count = len(self._active)
                print(f"[PM] RELEASED: {profile_uuid[:20]} from '{info.task_name}' "
                      f"after {duration:.1f}s (active={count}/{self.MAX_CONCURRENT})")
            else:
                print(f"[PM] RELEASE: {profile_uuid[:20]} not found in active (already released?)")
        self._global_sem.release()

    # ──── Batch operation guard ────

    def acquire_batch(self, task_name: str, timeout: float = 5) -> bool:
        """
        Chỉ cho phép 1 batch operation chạy tại 1 thời điểm.
        Ngăn user gửi nhiều lệnh batch liên tiếp gây flood.
        """
        got = self._batch_lock.acquire(timeout=timeout)
        if got:
            with self._lock:
                self._current_batch = task_name
            print(f"[PM] BATCH START: '{task_name}'")
        else:
            with self._lock:
                current = self._current_batch or "unknown"
            print(f"[PM] BATCH DENIED: '{task_name}' — already running '{current}'")
        return got

    def release_batch(self):
        """Giải phóng batch lock."""
        with self._lock:
            name = self._current_batch
            self._current_batch = None
        try:
            self._batch_lock.release()
            print(f"[PM] BATCH END: '{name}'")
        except RuntimeError:
            pass  # already released

    # ──── Query ────

    def is_locked(self, profile_uuid: str) -> bool:
        """Check nếu profile đang bị lock."""
        with self._lock:
            return profile_uuid in self._active

    def get_active_count(self) -> int:
        """Số profile đang active."""
        with self._lock:
            return len(self._active)

    def get_active_profiles(self) -> List[Dict]:
        """Lấy danh sách profiles đang active."""
        with self._lock:
            return [
                {
                    "uuid": pl.profile_uuid,
                    "task": pl.task_name,
                    "since": pl.locked_at,
                    "age_sec": time.time() - pl.locked_at
                }
                for pl in self._active.values()
            ]

    def get_batch_status(self) -> Optional[str]:
        """Lấy tên batch đang chạy, None nếu không có."""
        with self._lock:
            return self._current_batch

    def release_all(self):
        """Force release tất cả (dùng cho stop_all)."""
        with self._lock:
            count = len(self._active)
            self._active.clear()
            self._current_batch = None
        # Release all semaphore permits
        for _ in range(count):
            try:
                self._global_sem.release()
            except ValueError:
                break
        try:
            self._batch_lock.release()
        except RuntimeError:
            pass
        print(f"[PM] RELEASE ALL: cleared {count} profiles")

    # ──── Context manager cho từng profile ────

    class ProfileContext:
        """Context manager: with pm.profile(uuid, task): ..."""
        def __init__(self, pm: 'ProfileManager', uuid: str, task: str, timeout: float = 120):
            self.pm = pm
            self.uuid = uuid
            self.task = task
            self.timeout = timeout
            self.acquired = False

        def __enter__(self):
            self.acquired = self.pm.acquire(self.uuid, self.task, self.timeout)
            return self.acquired

        def __exit__(self, *args):
            if self.acquired:
                self.pm.release(self.uuid)

    def profile(self, uuid: str, task: str, timeout: float = 120):
        """Context manager: with pm.profile(uuid, task): ..."""
        return self.ProfileContext(self, uuid, task, timeout)


# ── Singleton ──
pm = ProfileManager()
