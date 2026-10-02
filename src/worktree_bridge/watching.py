"""Filesystem change hints; inventory always verifies stat/hash independently."""
import ctypes
import os
from pathlib import Path
import select
import struct
import sys
import threading


class Watcher:
    def __init__(self, root, extra_roots=()):
        self.root = Path(root)
        self.roots = [self.root] + [Path(p) for p in extra_roots if Path(p) != self.root]
        self.mode = "poll"
        self._lock = threading.Lock()
        self._changed = set()
        self._overflow = False
        self._stop = threading.Event()
        self._handles = []
        self._threads = []
        self._fd = None
        try:
            if sys.platform == "win32":
                self._windows()
            elif sys.platform.startswith("linux"):
                self._linux()
        except OSError:
            self.close()
            self.mode = "poll"

    def _hint(self, path=None, overflow=False):
        with self._lock:
            if path is not None:
                self._changed.add(str(path))
            self._overflow = self._overflow or overflow

    def drain(self):
        with self._lock:
            result = (self._changed, self._overflow)
            self._changed, self._overflow = set(), False
        return result

    def _thread(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _windows(self):
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                                wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                                ctypes.c_void_p, ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self._kernel = kernel
        for root in self.roots:
            handle = kernel.CreateFileW(str(root), 1, 7, None, 3, 0x02000000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise OSError(ctypes.get_last_error(), "cannot watch directory")
            self._handles.append(handle)
            self._thread(self._windows_read, handle, root)
        self.mode = "ReadDirectoryChangesW"

    def _windows_read(self, handle, root):
        from ctypes import wintypes
        buffer = ctypes.create_string_buffer(65536)
        count = wintypes.DWORD()
        while not self._stop.is_set():
            ok = self._kernel.ReadDirectoryChangesW(handle, buffer, len(buffer), True,
                                                   0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x100,
                                                   ctypes.byref(count), None, None)
            if not ok:
                if not self._stop.is_set():
                    self._hint(overflow=True)
                return
            if not count.value:
                self._hint(overflow=True)
                continue
            offset = 0
            data = buffer.raw[:count.value]
            while offset + 12 <= len(data):
                next_offset, action, length = struct.unpack_from("<III", data, offset)
                name = data[offset + 12:offset + 12 + length].decode("utf-16-le")
                self._hint(root / name)
                if not next_offset:
                    break
                offset += next_offset

    def _linux(self):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self._fd < 0:
            self._fd = None
            raise OSError(ctypes.get_errno(), "cannot initialize inotify")
        self._libc = libc
        self._watches = {}
        for root in self.roots:
            self._linux_add_tree(root)
        self._thread(self._linux_read)
        self.mode = "inotify"

    def _linux_add_tree(self, root):
        from .agent import DATA_DIRS, SKIP_DIRS
        if root != self.root:
            try:
                parts = root.relative_to(self.root).parts
                if ".git" not in parts and any(part.lower() in SKIP_DIRS | DATA_DIRS for part in parts):
                    return
            except ValueError:
                pass
        def failure(exc):
            raise exc
        for directory, dirs, _ in os.walk(root, followlinks=False, onerror=failure):
            dirs[:] = [name for name in dirs if not Path(directory, name).is_symlink()
                       and (name.lower() not in SKIP_DIRS | DATA_DIRS or name == ".git")
                       and not (Path(directory).name == ".git" and name == "objects")]
            wd = self._libc.inotify_add_watch(self._fd, os.fsencode(directory),
                                             0x2 | 0x4 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800)
            if wd < 0:
                raise OSError(ctypes.get_errno(), "cannot watch " + directory)
            self._watches[wd] = Path(directory)

    def _linux_read(self):
        while not self._stop.is_set():
            try:
                if not select.select([self._fd], [], [], 0.2)[0]:
                    continue
                data = os.read(self._fd, 65536)
                offset = 0
                while offset + 16 <= len(data):
                    wd, mask, cookie, length = struct.unpack_from("iIII", data, offset)
                    name = data[offset + 16:offset + 16 + length].split(b"\0", 1)[0]
                    offset += 16 + length
                    if mask & 0x4000 or wd not in self._watches:
                        self._hint(overflow=True)
                        continue
                    path = self._watches[wd] / os.fsdecode(name)
                    self._hint(path)
                    if mask & 0x40000000 and mask & (0x100 | 0x80):
                        if not path.is_symlink():
                            try:
                                self._linux_add_tree(path)
                            except OSError:
                                self._hint(overflow=True)
            except (OSError, ValueError, TypeError):
                if not self._stop.is_set():
                    self._hint(overflow=True)
                return

    def close(self):
        self._stop.set()
        for handle in self._handles:
            self._kernel.CancelIoEx(handle, None)
            self._kernel.CloseHandle(handle)
        self._handles.clear()
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        for thread in self._threads:
            thread.join(timeout=0.3)
        self._threads.clear()
