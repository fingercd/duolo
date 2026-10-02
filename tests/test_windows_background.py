"""Real Windows venv startup regression: no daemon/peer terminal windows."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
from worktree_bridge import client


def process_table():
    """Read the documented Toolhelp process snapshot, without WMI/packages."""
    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        entries = {}
        success = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while success:
            entries[entry.th32ProcessID] = {"pid": entry.th32ProcessID,
                                           "parent": entry.th32ParentProcessID,
                                           "name": entry.szExeFile}
            success = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        return entries
    finally:
        kernel.CloseHandle(snapshot)


def owned_windows(pids):
    """Only collect window class/visibility for this test's process tree."""
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetClassNameW.restype = ctypes.c_int
    windows = []

    @callback_type
    def collect(hwnd, unused):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            name = ctypes.create_unicode_buffer(256)
            user.GetClassNameW(hwnd, name, len(name))
            windows.append({"pid": pid.value, "class": name.value,
                            "visible": bool(user.IsWindowVisible(hwnd))})
        return True

    if not user.EnumWindows(collect, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return windows


def process_started(pid):
    """Pair PID with creation time so PID reuse cannot claim user processes."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                      ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                      ctypes.POINTER(wintypes.FILETIME)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    process = kernel.OpenProcess(0x1000, False, pid)
    if not process:
        return None
    try:
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(process, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel.CloseHandle(process)


class ProcessMonitor:
    def __init__(self, root_pid):
        self.births = {root_pid: process_started(root_pid)}
        self.processes = {}
        self.windows = []
        self.errors = []
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def sample(self):
        with self.lock:
            table = process_table()
            births = {}

            def identity(pid):
                if pid not in births:
                    births[pid] = process_started(pid)
                return births[pid]

            active = {pid for pid, birth in self.births.items()
                      if birth is not None and pid in table and identity(pid) == birth}
            while True:
                children = {pid for pid, item in table.items() if item["parent"] in active and pid not in active}
                confirmed = {pid for pid in children if identity(pid) is not None
                             and identity(pid) >= self.births[table[pid]["parent"]]}
                if not confirmed:
                    break
                self.births.update({pid: identity(pid) for pid in confirmed})
                active.update(confirmed)
            self.processes.update({(pid, self.births[pid]): table[pid] for pid in active})
            self.windows.extend(owned_windows(active))
            return {pid: table[pid] for pid in active}

    def run(self):
        try:
            while not self.stop.is_set():
                self.sample()
                self.stop.wait(.05)
        except Exception as exc:
            self.errors.append(str(exc))

    def close(self):
        self.stop.set()
        self.thread.join(3)


@unittest.skipUnless(os.name == "nt", "Windows process/window regression")
class WindowsBackgroundTests(unittest.TestCase):
    def run_hidden(self, command, *, timeout=30, env=None):
        return subprocess.run(command, check=True, capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout, env=env,
                              creationflags=subprocess.CREATE_NO_WINDOW)

    def test_venv_start_and_peer_descendants_have_no_console_or_visible_window(self):
        with tempfile.TemporaryDirectory(prefix="wtb-windows-background-") as temporary:
            root = Path(temporary)
            local, remote = root / "local", root / "remote"
            local.mkdir()
            self.run_hidden(["git", "-C", str(local), "init", "-q", "--template=", "-b", "main"])
            for key, value in (("user.email", "test@example.com"), ("user.name", "Test"),
                               ("core.autocrlf", "false")):
                self.run_hidden(["git", "-C", str(local), "config", key, value])
            (local / "a.txt").write_text("initial\n", encoding="utf-8")
            self.run_hidden(["git", "-C", str(local), "add", "a.txt"])
            self.run_hidden(["git", "-C", str(local), "commit", "-qm", "initial"])
            self.run_hidden(["git", "-c", "core.autocrlf=false", "clone", "-q", str(local), str(remote)])
            # Exercise the Windows venv redirector involved in the real bug,
            # without pip, installing packages, or touching the user's venv.
            self.run_hidden([sys.executable, "-m", "venv", "--without-pip", str(root / "venv")], timeout=60)
            python = root / "venv" / "Scripts" / "python.exe"
            pythonw = python.with_name("pythonw.exe")
            self.assertTrue(pythonw.is_file())
            state = root / "state"
            config = root / "config.json"
            config.write_text(json.dumps({"local_root": str(local),
                                          "remote": {"kind": "local", "root": str(remote)},
                                          "state_dir": str(state), "service": {"poll_interval": .1}}), encoding="utf-8")
            environment = dict(os.environ, PYTHONPATH=str(SRC), PYTHONUTF8="1",
                               LOCALAPPDATA=str(root / "registration-home"),
                               XDG_STATE_HOME=str(root / "registration-home"))
            starter = subprocess.Popen([str(python), "-m", "worktree_bridge", "--config", str(config), "start"],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
            monitor = ProcessMonitor(starter.pid)
            monitor.thread.start()
            evidence = {}
            try:
                stdout, stderr = starter.communicate(timeout=35)
                self.assertEqual(starter.returncode, 0, stdout.decode("utf-8", "replace") + stderr.decode("utf-8", "replace"))
                started = json.loads(stdout)
                self.assertTrue(started["ok"])
                status = client.wait_until_synced(config, timeout=30)
                self.assertEqual(status["state"], "synced")
                live = monitor.sample()
                daemon_pid = started["pid"]
                self.assertIn(daemon_pid, live)
                descendants = {daemon_pid}
                while True:
                    children = {pid for pid, item in live.items() if item["parent"] in descendants}
                    if children <= descendants:
                        break
                    descendants.update(children)
                python_processes = [live[pid] for pid in descendants if live[pid]["name"].lower().startswith("python")]
                self.assertGreaterEqual(len(python_processes), 3, live)
                self.assertTrue(all(item["name"].lower() == "pythonw.exe" for item in python_processes), python_processes)

                # A GUI helper has no console of its own. AttachConsole merely
                # checks these test-owned processes, then detaches; it creates
                # no terminal and never attaches the unittest runner itself.
                probe_source = """import ctypes,json,sys
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.AttachConsole.argtypes=[ctypes.c_ulong]
k.AttachConsole.restype=ctypes.c_int
k.FreeConsole.restype=ctypes.c_int
k.GetConsoleWindow.restype=ctypes.c_void_p
assert not k.GetConsoleWindow(), 'probe unexpectedly owns a console'
attached=[]
for pid in json.loads(sys.argv[1]):
    if k.AttachConsole(pid):
        attached.append(pid)
        k.FreeConsole()
print(json.dumps(attached))
"""
                probe = self.run_hidden([str(pythonw), "-c", probe_source, json.dumps(sorted(descendants))], env=environment)
                consoles = json.loads(probe.stdout)
                self.assertEqual(consoles, [], "daemon/peer console attachments found")
                evidence = {"daemon_pid": daemon_pid,
                            "python_processes": python_processes, "console_processes": consoles,
                            "state": status["state"]}
            finally:
                try:
                    client.action(config, "stop")
                except client.ServiceUnavailable:
                    pass
                deadline = time.monotonic() + 65
                while time.monotonic() < deadline:
                    alive = monitor.sample()
                    if not (state / "service.json").exists() and not alive:
                        break
                    time.sleep(.1)
                monitor.close()
                if starter.poll() is None:
                    starter.communicate(timeout=30)
                self.assertFalse((state / "service.json").exists(), "test daemon did not release its service metadata")
                self.assertFalse(monitor.sample(), "test-owned daemon descendants did not exit after client.stop")
            self.assertFalse(monitor.errors, monitor.errors)
            self.assertFalse([w for w in monitor.windows if w["visible"] or w["class"] == "ConsoleWindowClass"], monitor.windows)
            # CREATE_NO_WINDOW may still leave a transient hidden host inside a
            # system/venv/Git shim. Its process alone is not a visible terminal.
            hidden_hosts = [p for p in monitor.processes.values()
                            if p["name"].lower() in ("conhost.exe", "openconsole.exe")]
            evidence.update(visible_windows=[], hidden_console_hosts=hidden_hosts, stopped=True)
            print("Windows background evidence: " + json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
