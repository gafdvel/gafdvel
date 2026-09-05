# -*- coding: utf-8 -*-
"""Strip — Geek-style portable uninstaller with cancellable leftover scan."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import errno
import os
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Callable, Iterable

try:
    import winreg
except ImportError:
    raise SystemExit("Windows only")

# Heavy libs (PIL/win32) are imported lazily — Geek opens instantly; we must too.
win32gui = None
win32ui = None
win32con = None
win32api = None
Image = None
ImageTk = None

APP_NAME = "gafdvel"
VERSION = "0.3.15"

KIND_LABEL = {"file": "文件", "dir": "目录", "reg": "注册表"}

# 16x16 transparent placeholder (PNG) — no disk / no PIL at startup
_DEFAULT_ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAOUlEQVR4nGNgoBAw"
    "goiUnJL/5GieM6UHrJ8sA2B6mBgoBEyUGsCC7iSyQMqQDgOmUQMYBklCogQAAKc4"
    "Ea6/28ERAAAAAElFTkSuQmCC"
)


def _ensure_graphics():
    global win32gui, win32ui, win32con, win32api, Image, ImageTk
    if Image is not None or ImageTk is not None:
        return
    try:
        import win32api as _api
        import win32con as _con
        import win32gui as _gui
        import win32ui as _ui
        from PIL import Image as _Image
        from PIL import ImageTk as _ImageTk

        win32api, win32con, win32gui, win32ui = _api, _con, _gui, _ui
        Image, ImageTk = _Image, _ImageTk
    except ImportError:
        pass


def resource_path(*parts: str) -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)

UNINSTALL_ROOTS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def ensure_admin() -> None:
    """Relaunch elevated if needed — double-click should always run as admin."""
    if is_admin():
        return
    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
    else:
        exe = sys.executable
        script = os.path.abspath(sys.argv[0])
        params = " ".join([f'"{script}"'] + [f'"{a}"' for a in sys.argv[1:]])
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        if rc <= 32:
            # user cancelled UAC or failed — continue without admin
            return
    except Exception:
        return
    raise SystemExit(0)


def _reg_get(key, name, default=""):
    try:
        v, _ = winreg.QueryValueEx(key, name)
        return v if isinstance(v, str) else default
    except OSError:
        return default


def _reg_get_int(key, name, default=0) -> int:
    try:
        v, _ = winreg.QueryValueEx(key, name)
        return int(v)
    except (OSError, TypeError, ValueError):
        return default


def format_size(n: int | None) -> str:
    if n is None or n < 0:
        return "未知"
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024**3:.2f} GB"


def _is_ephemeral_path(path: str) -> bool:
    """Installer caches / temp — never treat as real app size root."""
    p = (path or "").lower().replace("/", "\\")
    bad = (
        "\\package cache\\",
        "\\windows\\installer\\",
        "\\windows\\temp\\",
        "\\temp\\",
        "\\tmp\\",
        "appdata\\local\\temp",
        "\\isu-caches\\",
        "\\installer\\installshield installation information\\",
    )
    if any(b in p for b in bad):
        return True
    # WinGet download/cache only — Packages/Links are real portable installs
    if "\\winget\\" in p and "\\packages\\" not in p and "\\links\\" not in p:
        return True
    return False


def _clean_dir(path: str) -> str:
    return (path or "").strip().strip('"').rstrip("\\/")


def _compact(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", s or "", flags=re.UNICODE).lower()


def _lev(a: str, b: str) -> int:
    # ponytail: O(n*m) fine for folder-name length
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


_GENERIC_DIR = {
    "application", "applications", "bin", "app", "x64", "x86", "win64", "win32",
    "current", "program", "programs", "release", "debug", "bin64", "bin32",
}

_SUFFIX_NOISE = (
    "installer", "setup", "uninstaller", "redistributable", "runtime", "update",
    "service", "application", "app", "x64", "x86",
)


def _stem_compact(s: str) -> str:
    c = _compact(s)
    changed = True
    while changed and len(c) > 4:
        changed = False
        for suf in _SUFFIX_NOISE:
            if c.endswith(suf) and len(c) - len(suf) >= 3:
                c = c[: -len(suf)]
                changed = True
                break
    return c


def _name_path_score(app_name: str, path: str) -> int:
    """0–100: how well path looks like this product (Geek-style path/name affinity)."""
    app = _stem_compact(app_name)
    if len(app) < 2:
        return 50
    best = 0
    cur = _clean_dir(path)
    for _ in range(3):
        base = os.path.basename(cur)
        base_c = _stem_compact(base)
        if not base_c or base_c in _GENERIC_DIR:
            parent = os.path.dirname(cur)
            if not parent or parent == cur or len(parent) <= 3:
                break
            cur = parent
            continue
        if app == base_c:
            best = max(best, 95)
        elif len(app) >= 4 and base_c in app:
            best = max(best, 90)
        elif len(app) >= 4 and app in base_c:
            # "aura" ⊂ "auralighting…" is weak; near-equal length is strong
            if len(base_c) <= len(app) + 5:
                best = max(best, 88)
            else:
                best = max(best, 48)
        else:
            d = _lev(app[:40], base_c[:40])
            ratio = 1.0 - d / max(len(app), len(base_c), 1)
            best = max(best, int(ratio * 100))
        parent = os.path.dirname(cur)
        if not parent or parent == cur or len(parent) <= 3:
            break
        cur = parent
    return best


def _accept_dir(path: str) -> str:
    p = _clean_dir(path)
    if p and os.path.isdir(p) and not _is_ephemeral_path(p) and not _is_unsafe_size_root(p):
        return p
    return ""


def _refine_size_root(app_name: str, path: str, *, require_name: bool) -> str:
    """Among path and up-to-2 parents, pick best name match; drop false positives."""
    start = _accept_dir(path)
    if not start:
        return ""
    cands: list[tuple[int, str]] = []
    cur = start
    for _ in range(3):
        ok = _accept_dir(cur)
        if ok:
            cands.append((_name_path_score(app_name, ok), ok))
        parent = os.path.dirname(cur)
        if not parent or parent == cur or _is_unsafe_size_root(parent):
            break
        cur = parent
    if not cands:
        return ""
    cands.sort(key=lambda x: (-x[0], -len(x[1])))
    score, best = cands[0]
    if require_name and score < 55:
        return ""  # false positive InstallLocation / bad guess
    return best


def _is_unsafe_size_root(path: str) -> bool:
    """Dirs that would inflate size to GBs (System32, Fonts, drive root, …)."""
    p = _clean_dir(path)
    if not p:
        return True
    low = os.path.normpath(p).lower()
    if len(low) <= 3 and len(low) >= 2 and low[1] == ":":
        return True
    parts = [x for x in low.split("\\") if x and x != "."]
    if len(parts) <= 1:
        return True

    env = os.environ
    exact = {
        (env.get("ProgramFiles") or r"C:\Program Files").lower().rstrip("\\"),
        (env.get("ProgramFiles(x86)") or r"C:\Program Files (x86)").lower().rstrip("\\"),
        (env.get("ProgramData") or r"C:\ProgramData").lower().rstrip("\\"),
        (env.get("SystemRoot") or r"C:\Windows").lower().rstrip("\\"),
        (env.get("SystemDrive") or "C:").lower().rstrip("\\") + "\\users",
        os.path.expanduser("~").lower().rstrip("\\"),
        r"e:\ruanjian",
        r"d:\ruanjian",
    }
    if low.rstrip("\\") in exact:
        return True

    windir = (env.get("SystemRoot") or r"C:\Windows").lower().rstrip("\\")
    if low == windir or low.startswith(windir + "\\"):
        return True

    banned_bits = (
        "\\windows media player",
        "\\common files",
        "\\windowsapps",
        "\\system32",
        "\\syswow64",
        "\\winsxs",
        "\\fonts",
        "\\assembly",
        "\\microsoft.net",
        "\\installer",
    )
    if any(b in low for b in banned_bits):
        return True
    return False


def _guess_install_dir(app: "InstalledApp") -> str:
    """Levenshtein / name match under Program Files, LocalAppData\\Programs, E:\\ruanjian."""
    stop = {
        "service", "setup", "install", "edition", "software", "application", "windows",
        "microsoft", "visual", "redistributable", "runtime", "update", "driver", "package",
        "player", "media", "shared", "common", "tools", "helper", "launcher",
    }
    low_name = app.name.lower()
    if "redistributable" in low_name or "runtime" in low_name:
        return ""
    app_c = _compact(app.name)
    if len(app_c) < 3:
        return ""

    env = os.environ
    local = env.get("LOCALAPPDATA", "")
    roots = [
        env.get("ProgramFiles", r"C:\Program Files"),
        env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.path.join(local, "Programs") if local else "",
        r"E:\ruanjian",
        r"D:\ruanjian",
    ]
    scored: list[tuple[int, str]] = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            level1 = list(os.scandir(root))
        except OSError:
            continue
        candidates = []
        for e in level1:
            if not e.is_dir(follow_symlinks=False):
                continue
            candidates.append(e.path)
            try:
                for e2 in os.scandir(e.path):
                    if e2.is_dir(follow_symlinks=False):
                        candidates.append(e2.path)
            except OSError:
                pass
        for full in candidates:
            if _is_ephemeral_path(full) or _is_unsafe_size_root(full):
                continue
            sc = _name_path_score(app.name, full)
            # publisher folder bonus
            pub_c = _compact(app.publisher or "")[:12]
            if pub_c and len(pub_c) >= 4 and pub_c in _compact(os.path.basename(os.path.dirname(full))):
                sc += 5
            if sc >= 90:
                scored.append((sc, full))
    if not scored:
        return ""
    scored.sort(key=lambda x: (-x[0], -len(x[1])))
    return scored[0][1]


def _best_child_for_app(app_name: str, root: str) -> str:
    """When several apps share a vendor root, pick the child that matches this name."""
    root = _accept_dir(root)
    if not root:
        return ""
    try:
        kids = [e.path for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return ""
    best_s, best_p = 0, ""
    for p in kids:
        if _is_ephemeral_path(p) or _is_unsafe_size_root(p):
            continue
        s = _name_path_score(app_name, p)
        if s > best_s:
            best_s, best_p = s, p
    return best_p if best_s >= 90 else ""


def _mark_shared_install_dirs(apps: list["InstalledApp"]) -> None:
    counts: Counter[str] = Counter()
    paths: dict[int, str] = {}
    for a in apps:
        p = a.resolved_install_dir(allow_msi=False)
        if p:
            key = os.path.normcase(p)
            paths[id(a)] = key
            counts[key] += 1
    for a in apps:
        key = paths.get(id(a), "")
        a._share_n = counts.get(key, 1) if key else 1  # type: ignore[attr-defined]


def folder_size_bytes(path: str, max_files: int = 800000) -> int:
    total = 0
    n = 0
    if not path or not os.path.isdir(path):
        return 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            n += 1
                            if n >= max_files:
                                return total
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def format_install_date(raw: str, fallback_path: str = "") -> str:
    raw = (raw or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    for p in (fallback_path,):
        if p and os.path.exists(p):
            try:
                ts = os.path.getctime(p)
                return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except OSError:
                pass
    return "未知"


def parse_icon_spec(spec: str) -> tuple[str, int]:
    s = (spec or "").strip().strip('"')
    if not s:
        return "", 0
    # path,index  /  "path",0
    if s.endswith(",0") or re.search(r",-?\d+$", s):
        path, _, idx = s.rpartition(",")
        path = path.strip().strip('"')
        try:
            return path, int(idx)
        except ValueError:
            return s.strip('"'), 0
    return s.strip('"'), 0


def resolve_exe_from_uninstall(cmd: str) -> str:
    c = (cmd or "").strip()
    if not c:
        return ""
    low = c.lower()
    if low.startswith("msiexec") or low.startswith("winget"):
        return ""
    if c.startswith('"'):
        end = c.find('"', 1)
        if end > 1:
            return c[1:end]
    return c.split(" ", 1)[0]


def _winget_product_code(app: "InstalledApp") -> str:
    """Product code from UninstallString / registry key (winget portable)."""
    for raw in (app.uninstall, app.quiet, os.path.basename(app.key_path)):
        m = re.search(
            r"(?:--product-code\s+)?([A-Za-z0-9][A-Za-z0-9._+-]+_Microsoft\.Winget\.Source_[A-Za-z0-9]+)",
            raw or "",
            re.I,
        )
        if m:
            return m.group(1)
        m = re.search(r"--product-code\s+(\S+)", raw or "", re.I)
        if m:
            return m.group(1).strip('"')
    return ""


def _winget_install_dir(app: "InstalledApp") -> str:
    """%LOCALAPPDATA%\\Microsoft\\WinGet\\Packages\\<product-code>[\\...]"""
    code = _winget_product_code(app)
    if not code:
        return ""
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return ""
    root = os.path.join(local, "Microsoft", "WinGet", "Packages", code)
    if not os.path.isdir(root):
        return ""
    # Prefer a child that matches the app name; else the package root
    child = _best_child_for_app(app.name, root)
    if child:
        return child
    # Single nested folder is common for portable exes
    try:
        kids = [e.path for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
        files = [e.path for e in os.scandir(root) if e.is_file(follow_symlinks=False)]
    except OSError:
        return root
    if len(kids) == 1 and not files:
        return kids[0]
    return root


def extract_msi_product_code(cmd: str) -> str:
    m = re.search(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}", cmd or "")
    return m.group(0) if m else ""


_msi_index: dict[str, Counter] | None = None
_msi_index_lock = threading.Lock()


def _ensure_msi_index() -> dict[str, Counter]:
    """One-time scan of MSI components → product install dirs. Call off UI thread."""
    global _msi_index
    with _msi_index_lock:
        if _msi_index is not None:
            return _msi_index
        index: dict[str, Counter] = {}
        try:
            msi = ctypes.windll.msi
            comp = ctypes.create_unicode_buffer(64)
            i = 0
            while True:
                if msi.MsiEnumComponentsW(i, comp) != 0:
                    break
                client = ctypes.create_unicode_buffer(64)
                j = 0
                while True:
                    if msi.MsiEnumClientsW(comp.value, j, client) != 0:
                        break
                    prod = client.value.upper()
                    pbuf = ctypes.create_unicode_buffer(1024)
                    pn = wintypes.DWORD(1024)
                    msi.MsiGetComponentPathW(prod, comp.value, pbuf, ctypes.byref(pn))
                    p = pbuf.value
                    if p and len(p) >= 3 and p[1] == ":" and p[2] in "\\/" and p[0].isalpha():
                        if prod not in index:
                            index[prod] = Counter()
                        if os.path.isfile(p):
                            index[prod][os.path.dirname(p)] += 2
                            parent = os.path.dirname(os.path.dirname(p))
                            if parent and len(parent) > 3:
                                index[prod][parent] += 1
                        elif os.path.isdir(p):
                            index[prod][p.rstrip("\\/")] += 1
                    j += 1
                i += 1
                if i > 300000:
                    break
        except Exception:
            pass
        _msi_index = index
        return _msi_index


def _msi_compress_guid(product_code: str) -> str:
    g = product_code.strip("{}").replace("-", "")
    if len(g) != 32:
        return ""
    parts = [g[0:8], g[8:12], g[12:16]] + [g[i : i + 2] for i in range(16, 32, 2)]
    return "".join(p[::-1] for p in parts)


def _msi_userdata_install_dir(product_code: str) -> str:
    """Installer\\UserData\\…\\Products\\<compressed>\\InstallProperties\\InstallLocation."""
    packed = _msi_compress_guid(product_code)
    if not packed:
        return ""
    bases = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\UserData",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Installer\UserData",
    ]
    for base in bases:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as ud:
                i = 0
                while True:
                    try:
                        sid = winreg.EnumKey(ud, i)
                    except OSError:
                        break
                    i += 1
                    sub = f"{base}\\{sid}\\Products\\{packed}\\InstallProperties"
                    try:
                        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
                            loc = _clean_dir(_reg_get(k, "InstallLocation"))
                            if loc and os.path.isdir(loc):
                                return loc
                    except OSError:
                        continue
        except OSError:
            continue
    return ""


def _msi_local_package(product_code: str) -> str:
    """Cached .msi path (LocalPackage) — for Explorer /select when no install folder."""
    if not product_code:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        if ctypes.windll.msi.MsiGetProductInfoW(product_code, "LocalPackage", buf, ctypes.byref(n)) == 0:
            p = (buf.value or "").strip().strip('"')
            if p and os.path.isfile(p):
                return p
    except Exception:
        pass
    packed = _msi_compress_guid(product_code)
    if not packed:
        return ""
    bases = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\UserData",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Installer\UserData",
    ]
    for base in bases:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as ud:
                i = 0
                while True:
                    try:
                        sid = winreg.EnumKey(ud, i)
                    except OSError:
                        break
                    i += 1
                    sub = f"{base}\\{sid}\\Products\\{packed}\\InstallProperties"
                    try:
                        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
                            p = (_reg_get(k, "LocalPackage") or "").strip().strip('"')
                            if p and os.path.isfile(p):
                                return p
                    except OSError:
                        continue
        except OSError:
            continue
    return ""


def _reveal_in_explorer(path: str) -> bool:
    """Open a folder, or select a file in Explorer. False if path missing."""
    if not path:
        return False
    try:
        if os.path.isfile(path):
            subprocess.run(["explorer", f"/select,{os.path.normpath(path)}"], check=False)
            return True
        if os.path.isdir(path):
            os.startfile(path)
            return True
    except OSError:
        return False
    return False


def msi_install_dir(product_code: str) -> str:
    """Resolve actual install folder for MSI products with empty InstallLocation."""
    if not product_code:
        return ""
    cache = getattr(msi_install_dir, "_cache", None)
    if cache is None:
        cache = {}
        msi_install_dir._cache = cache  # type: ignore[attr-defined]
    key = product_code.upper()
    if key in cache:
        return cache[key]

    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        if ctypes.windll.msi.MsiGetProductInfoW(product_code, "InstallLocation", buf, ctypes.byref(n)) == 0:
            loc = buf.value.strip().rstrip("\\/")
            if loc and os.path.isdir(loc) and not _is_unsafe_size_root(loc) and not _is_ephemeral_path(loc):
                cache[key] = loc
                return loc
    except Exception:
        pass

    ud = _msi_userdata_install_dir(product_code)
    if ud and not _is_unsafe_size_root(ud) and not _is_ephemeral_path(ud):
        cache[key] = ud
        return ud

    counts = _ensure_msi_index().get(key)
    if not counts:
        cache[key] = ""
        return ""
    best = sorted(counts.items(), key=lambda kv: (-kv[1], len(kv[0])))
    out = ""
    for cand, _cnt in best:
        if os.path.isdir(cand) and not _is_ephemeral_path(cand) and not _is_unsafe_size_root(cand):
            out = cand
            break
    cache[key] = out
    return out


def _hicon_to_rgb(hicon, size: int, bg: tuple[int, int, int]):
    """Draw HICON onto solid bg → PIL RGB (caller owns DestroyIcon)."""
    screen = win32gui.GetDC(0)
    try:
        hdc = win32ui.CreateDCFromHandle(screen)
        mem = hdc.CreateCompatibleDC()
        hbmp = win32ui.CreateBitmap()
        hbmp.CreateCompatibleBitmap(hdc, size, size)
        old = mem.SelectObject(hbmp)
        brush = win32gui.CreateSolidBrush(win32api.RGB(*bg))
        win32gui.FillRect(mem.GetSafeHdc(), (0, 0, size, size), brush)
        win32gui.DeleteObject(brush)
        win32gui.DrawIconEx(mem.GetSafeHdc(), 0, 0, hicon, size, size, 0, 0, win32con.DI_NORMAL)
        mem.SelectObject(old)
        info = hbmp.GetInfo()
        bits = hbmp.GetBitmapBits(True)
        return Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1
        )
    finally:
        try:
            win32gui.ReleaseDC(0, screen)
        except Exception:
            pass


def extract_icon_image(path: str, index: int = 0, size: int = 16):
    """Return PIL RGBA — black/white dual-pass alpha (no magenta fringe)."""
    _ensure_graphics()
    if not win32gui or not Image or not path or not os.path.isfile(path):
        return None
    try:
        large, small = win32gui.ExtractIconEx(path, index)
        icons = list(small or []) + list(large or [])
        hicon = icons[0] if icons else None
        if not hicon and index != 0:
            large2, small2 = win32gui.ExtractIconEx(path, 0)
            icons = list(small2 or []) + list(large2 or [])
            hicon = icons[0] if icons else None
            for h in list(large2 or []) + list(small2 or []):
                if h and h != hicon:
                    try:
                        win32gui.DestroyIcon(h)
                    except Exception:
                        pass
        if not hicon:
            return None
        try:
            # Magenta key left pink/red halos on anti-aliased edges; recover alpha instead.
            on_black = _hicon_to_rgb(hicon, size, (0, 0, 0))
            on_white = _hicon_to_rgb(hicon, size, (255, 255, 255))
            out = Image.new("RGBA", (size, size))
            pb, pw, po = on_black.load(), on_white.load(), out.load()
            for y in range(size):
                for x in range(size):
                    br, bg, bb = pb[x, y]
                    wr, wg, wb = pw[x, y]
                    # opaque → same on both; transparent → white-black ≈ 255
                    a = 255 - (abs(wr - br) + abs(wg - bg) + abs(wb - bb)) // 3
                    if a <= 2:
                        po[x, y] = (0, 0, 0, 0)
                    elif a >= 253:
                        po[x, y] = (br, bg, bb, 255)
                    else:
                        # un-premultiply from black composite: c = c_black * 255 / a
                        po[x, y] = (
                            min(255, br * 255 // a),
                            min(255, bg * 255 // a),
                            min(255, bb * 255 // a),
                            a,
                        )
            return out
        finally:
            for h in icons:
                try:
                    win32gui.DestroyIcon(h)
                except Exception:
                    pass
    except Exception:
        return None


@dataclass
class InstalledApp:
    name: str
    key_path: str
    hive: int
    uninstall: str
    quiet: str
    publisher: str
    install_location: str
    version: str
    icon: str = ""
    estimated_kb: int = 0
    install_date: str = ""
    size_bytes: int | None = None  # None=unknown; filled from registry or scan

    @property
    def keywords(self) -> list[str]:
        parts = []
        for raw in (self.name, self.publisher, os.path.basename(self.install_location.rstrip("\\/"))):
            if not raw:
                continue
            cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", " ", raw, flags=re.UNICODE)
            for t in cleaned.split():
                if len(t) >= 3 and t.lower() not in {"inc", "ltd", "llc", "the", "and", "for", "com"}:
                    parts.append(t)
        seen, out = set(), []
        for p in sorted(set(parts), key=len, reverse=True):
            low = p.lower()
            if low not in seen:
                seen.add(low)
                out.append(p)
        return out[:8]

    def size_display(self) -> str:
        if getattr(self, "_size_pending", False):
            return "计算中…"
        if getattr(self, "_locate_failed", False):
            return "未知"
        if getattr(self, "_size_measured", False):
            return format_size(self.size_bytes or 0)
        if self.estimated_kb > 0:
            return "~" + format_size(self.estimated_kb * 1024)
        if self.size_bytes is not None and self.size_bytes > 0:
            return "~" + format_size(self.size_bytes)
        return "点击计算大小"

    def date_display(self) -> str:
        # Never call MSI resolve here — that freezes the UI on startup.
        return format_install_date(self.install_date, self.install_location)

    def resolved_install_dir(self, allow_msi: bool = True) -> str:
        """Real install folder for size — Geek-like path resolve + false-positive reject."""
        cached = getattr(self, "_resolved_dir", None)
        if cached:
            return cached

        # Explicit InstallLocation — reject false positives by name affinity
        loc = _refine_size_root(self.name, self.install_location, require_name=True)
        if loc:
            self._resolved_dir = loc  # type: ignore[attr-defined]
            return loc
        # Generic folder names (Application) with weak product match: still keep if under vendor
        raw = _accept_dir(self.install_location)
        if raw and _name_path_score(self.name, raw) >= 35:
            loc = _refine_size_root(self.name, raw, require_name=False)
            if loc:
                self._resolved_dir = loc  # type: ignore[attr-defined]
                return loc

        path, _ = parse_icon_spec(self.icon)
        if path and os.path.isfile(path):
            # File exists → parent is install root (NXY_70_*/codes won't match Chinese names)
            d = _refine_size_root(self.name, os.path.dirname(path), require_name=False)
            if d:
                self._resolved_dir = d  # type: ignore[attr-defined]
                return d

        exe = resolve_exe_from_uninstall(self.uninstall)
        if exe and os.path.isfile(exe):
            d = _refine_size_root(self.name, os.path.dirname(exe), require_name=False)
            if d:
                self._resolved_dir = d  # type: ignore[attr-defined]
                return d

        # winget portable is cheap (path exists check) — not gated behind allow_msi
        wg = _refine_size_root(self.name, _winget_install_dir(self), require_name=False)
        if wg:
            self._resolved_dir = wg  # type: ignore[attr-defined]
            return wg

        if not allow_msi:
            return ""

        code = extract_msi_product_code(self.uninstall) or extract_msi_product_code(self.key_path)
        if code:
            found = _refine_size_root(self.name, msi_install_dir(code) or "", require_name=False)
            if found and _name_path_score(self.name, found) >= 55:
                self._resolved_dir = found  # type: ignore[attr-defined]
                return found

        guessed = _refine_size_root(self.name, _guess_install_dir(self), require_name=True)
        if guessed:
            self._resolved_dir = guessed  # type: ignore[attr-defined]
            return guessed
        return ""

    def icon_path(self) -> tuple[str, int]:
        # Fast only — no MSI component walk (that belongs in background resolve).
        path, idx = parse_icon_spec(self.icon)
        if path and os.path.isfile(path):
            return path, idx
        for cand in (
            resolve_exe_from_uninstall(self.uninstall),
            os.path.join(self.install_location, "uninstall.exe") if self.install_location else "",
        ):
            if cand and os.path.isfile(cand):
                return cand, 0
        loc = (self.install_location or "").strip().strip('"').rstrip("\\/")
        cached = getattr(self, "_resolved_dir", None)
        if cached:
            loc = cached
        if loc and os.path.isdir(loc):
            prefer = ("7zFM.exe", "7zG.exe", "7z.exe", "app.exe", "main.exe")
            for name in prefer:
                p = os.path.join(loc, name)
                if os.path.isfile(p):
                    return p, 0
            try:
                for name in os.listdir(loc):
                    if name.lower().endswith(".exe"):
                        return os.path.join(loc, name), 0
            except OSError:
                pass
        return "", 0


@dataclass
class Hit:
    kind: str  # file | dir | reg
    path: str


@dataclass
class CancelToken:
    _flag: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self._flag.set()

    @property
    def cancelled(self) -> bool:
        return self._flag.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise InterruptedError("cancelled")


def enum_installed() -> list[InstalledApp]:
    apps: list[InstalledApp] = []
    for hive, root in UNINSTALL_ROOTS:
        try:
            with winreg.OpenKey(hive, root) as base:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(base, i)
                    except OSError:
                        break
                    i += 1
                    path = f"{root}\\{sub}"
                    try:
                        with winreg.OpenKey(hive, path) as k:
                            name = _reg_get(k, "DisplayName")
                            if not name:
                                continue
                            try:
                                sc, _ = winreg.QueryValueEx(k, "SystemComponent")
                                if sc in (1, "1"):
                                    continue
                            except OSError:
                                pass
                            if _reg_get(k, "ReleaseType"):
                                continue
                            uninstall = _reg_get(k, "UninstallString")
                            quiet = _reg_get(k, "QuietUninstallString")
                            if not uninstall and not quiet:
                                continue
                            est = _reg_get_int(k, "EstimatedSize", 0)
                            loc = _clean_dir(_reg_get(k, "InstallLocation"))
                            apps.append(
                                InstalledApp(
                                    name=name,
                                    key_path=path,
                                    hive=hive,
                                    uninstall=uninstall,
                                    quiet=quiet,
                                    publisher=_reg_get(k, "Publisher"),
                                    install_location=loc,
                                    version=_reg_get(k, "DisplayVersion"),
                                    icon=_reg_get(k, "DisplayIcon"),
                                    estimated_kb=est,
                                    install_date=_reg_get(k, "InstallDate"),
                                    size_bytes=(est * 1024) if est > 0 else None,
                                )
                            )
                    except OSError:
                        continue
        except OSError:
            continue
    # dedupe by name+version
    uniq, seen = [], set()
    for a in sorted(apps, key=lambda x: x.name.lower()):
        key = (a.name.lower(), a.version.lower(), a.uninstall.lower())
        if key in seen:
            continue
        seen.add(key)
        a._size_measured = False  # type: ignore[attr-defined]
        uniq.append(a)
    return uniq


def candidate_dirs(app: InstalledApp) -> list[str]:
    env = os.environ
    bases = [
        env.get("ProgramFiles", r"C:\Program Files"),
        env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        env.get("ProgramData", r"C:\ProgramData"),
        env.get("APPDATA", ""),
        env.get("LOCALAPPDATA", ""),
        os.path.join(env.get("USERPROFILE", ""), "AppData", "LocalLow"),
        r"C:\Users\Public",
    ]
    if app.install_location and os.path.isdir(app.install_location):
        bases.insert(0, os.path.dirname(app.install_location.rstrip("\\/")))
    return [b for b in bases if b and os.path.isdir(b)]


def _is_protected_fs_path(path: str) -> bool:
    """Never scan/delete Store packages or OS trees."""
    low = os.path.normpath(path or "").lower()
    if not low:
        return True
    banned = (
        "\\windowsapps",
        "\\systemapps",
        "\\winsxs",
        "\\system32",
        "\\syswow64",
        "\\windows defender",
        "\\windows\\servicing",
        "\\package cache\\",
        "\\windows\\installer",
    )
    if any(b in low for b in banned):
        return True
    windir = (os.environ.get("SystemRoot") or r"C:\Windows").lower().rstrip("\\")
    if low == windir or low.startswith(windir + "\\"):
        return True
    return False


def _match(name: str, keywords: Iterable[str]) -> bool:
    low = name.lower()
    for kw in keywords:
        if kw.lower() in low:
            return True
    return False


def scan_leftovers(
    app: InstalledApp,
    token: CancelToken,
    on_status: Callable[[str], None] | None = None,
    depth_limit: int = 3,
    dir_timeout_s: float = 8.0,
) -> list[Hit]:
    """Cancellable leftover scan. Timeouts skip hung directories instead of freezing UI."""
    hits: list[Hit] = []
    kws = app.keywords
    if not kws:
        return hits

    def status(msg: str) -> None:
        if on_status:
            on_status(msg)

    # --- registry (HKLM/HKCU Software + Uninstall entry) ---
    status("正在扫描注册表…")
    reg_roots = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node"),
    ]
    for hive, root in reg_roots:
        token.check()
        try:
            with winreg.OpenKey(hive, root) as base:
                i = 0
                while True:
                    token.check()
                    try:
                        sub = winreg.EnumKey(base, i)
                    except OSError:
                        break
                    i += 1
                    if _match(sub, kws):
                        hive_name = "HKCU" if hive == winreg.HKEY_CURRENT_USER else "HKLM"
                        hits.append(Hit("reg", f"{hive_name}\\{root}\\{sub}"))
        except OSError:
            continue

    # uninstall entry only if still present (official uninstaller may have removed it)
    if _reg_key_exists(app.hive, app.key_path):
        hive_name = "HKCU" if app.hive == winreg.HKEY_CURRENT_USER else "HKLM"
        hits.append(Hit("reg", f"{hive_name}\\{app.key_path}"))

    # --- filesystem: only shallow walk under known bases (not whole disk) ---
    status("正在扫描文件夹…")
    for base in candidate_dirs(app):
        token.check()
        if _is_protected_fs_path(base):
            continue
        status(f"扫描: {base}")
        _walk_limited(base, kws, hits, token, depth_limit=depth_limit, timeout_s=dir_timeout_s, status=status)

    # dedupe + drop protected paths that slipped in
    seen, out = set(), []
    for h in hits:
        if h.kind in ("file", "dir") and _is_protected_fs_path(h.path):
            continue
        if h.path in seen:
            continue
        seen.add(h.path)
        out.append(h)
    return out


def _walk_limited(
    base: str,
    kws: list[str],
    hits: list[Hit],
    token: CancelToken,
    depth_limit: int,
    timeout_s: float,
    status: Callable[[str], None],
) -> None:
    """List dirs with a watchdog timeout so a hung path can't freeze forever."""
    stack: list[tuple[str, int]] = [(base, 0)]
    while stack:
        token.check()
        path, depth = stack.pop()
        if depth > depth_limit:
            continue

        box: list = []

        def worker(p=path):
            try:
                box.append(os.listdir(p))
            except OSError:
                box.append([])

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout_s)
        if t.is_alive():
            status(f"跳过(超时): {path}")
            continue
        names = box[0] if box else []

        for name in names:
            token.check()
            full = os.path.join(path, name)
            if _is_protected_fs_path(full) or name.lower() in {"windowsapps", "windows defender", "windowsmail"}:
                continue
            if depth == 0 or _match(name, kws):
                try:
                    if os.path.isdir(full) and _match(name, kws):
                        hits.append(Hit("dir", full))
                    elif os.path.isfile(full) and _match(name, kws):
                        hits.append(Hit("file", full))
                except OSError:
                    continue
            if depth < depth_limit and os.path.isdir(full):
                # only descend into matching branches past depth 0, or one level of children of match
                if depth == 0 or _match(name, kws) or _match(os.path.basename(path), kws):
                    stack.append((full, depth + 1))


def run_uninstall(app: InstalledApp, prefer_quiet: bool = False) -> int:
    cmd = app.quiet if (prefer_quiet and app.quiet) else (app.uninstall or app.quiet)
    if not cmd:
        return -1
    # Expand MSI style and quoted paths
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(cmd, shell=True, creationflags=creation)
        return p.returncode
    except OSError:
        return -1


def _reg_key_exists(hive: int, subkey: str) -> bool:
    try:
        with winreg.OpenKey(hive, subkey):
            return True
    except OSError:
        return False


def _is_already_gone(err: BaseException | str) -> bool:
    if isinstance(err, OSError):
        if getattr(err, "winerror", None) in (2, 3):
            return True
        if err.errno in (2, errno.ENOENT):
            return True
    s = str(err).lower()
    return "winerror 2" in s or "winerror 3" in s or "找不到" in str(err) or "cannot find" in s


def _schedule_delete_on_reboot(path: str) -> bool:
    """PendingFileRenameOperations via MoveFileEx — works when file is locked."""
    MOVEFILE_DELAY_UNTIL_REBOOT = 4
    ok = False
    path = os.path.abspath(path)
    if not os.path.lexists(path):
        return True
    try:
        if os.path.isfile(path) or os.path.islink(path) or _is_reparse_dir(path):
            if ctypes.windll.kernel32.MoveFileExW(path, None, MOVEFILE_DELAY_UNTIL_REBOOT):
                return True
            return False
        # directory: schedule children first, then self
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files:
                p = os.path.join(root, name)
                if ctypes.windll.kernel32.MoveFileExW(p, None, MOVEFILE_DELAY_UNTIL_REBOOT):
                    ok = True
            for name in dirs:
                p = os.path.join(root, name)
                if ctypes.windll.kernel32.MoveFileExW(p, None, MOVEFILE_DELAY_UNTIL_REBOOT):
                    ok = True
        if ctypes.windll.kernel32.MoveFileExW(path, None, MOVEFILE_DELAY_UNTIL_REBOOT):
            ok = True
    except OSError:
        return False
    return ok


def _stop_lockers_near(path: str) -> None:
    """Best-effort: stop services/processes that often lock VPN leftovers."""
    low = (path or "").lower()
    hints = []
    if "sangfor" in low or "easyconnect" in low:
        hints = ["SangforPWEx", "SangforUDProtectEx", "EasyConnect", "SangforCSClient"]
    if not hints:
        return
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for name in hints:
        try:
            subprocess.run(
                f'sc.exe stop "{name}"', shell=True, creationflags=creation, timeout=8,
                capture_output=True,
            )
        except Exception:
            pass
        try:
            subprocess.run(
                f'taskkill /F /IM "{name}.exe" /T', shell=True, creationflags=creation, timeout=8,
                capture_output=True,
            )
        except Exception:
            pass
    # disable Sangfor protect for next boot if present
    if "sangfor" in low:
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Services\SangforPWEx",
                0,
                winreg.KEY_SET_VALUE,
            )
            winreg.SetValueEx(key, "Start", 0, winreg.REG_DWORD, 4)  # disabled
            winreg.CloseKey(key)
        except OSError:
            pass


def _rm_path(path: str) -> None:
    if os.path.islink(path) or _is_reparse_dir(path):
        try:
            os.unlink(path)
        except OSError:
            os.rmdir(path)
        return
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=False)
    elif os.path.isfile(path):
        os.remove(path)


def delete_hit(hit: Hit) -> str | None:
    try:
        if hit.kind in ("file", "dir"):
            if _is_protected_fs_path(hit.path):
                return None  # skip silently — not a leftover we should touch
            if not os.path.lexists(hit.path):
                return None
            try:
                _rm_path(hit.path)
                return None
            except OSError as e:
                if _is_already_gone(e):
                    return None
                # locked by self-protecting AV/VPN (Sangfor etc.)
                if getattr(e, "winerror", None) == 5 or e.errno in (errno.EACCES, errno.EPERM):
                    _stop_lockers_near(hit.path)
                    try:
                        _rm_path(hit.path)
                        return None
                    except OSError:
                        if _schedule_delete_on_reboot(hit.path):
                            return f"已锁定，已安排重启后删除: {hit.path}"
                return str(e)
        if hit.kind == "reg":
            return _delete_reg(hit.path)
    except OSError as e:
        if _is_already_gone(e):
            return None
        return str(e)
    return "unknown kind"


def _is_reparse_dir(path: str) -> bool:
    """True for Windows junction / mount-point dirs (shutil.rmtree refuses these)."""
    try:
        st = os.lstat(path)
        # FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        return bool(getattr(st, "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def _delete_reg(path: str) -> str | None:
    # path like HKLM\SOFTWARE\Foo
    if path.startswith("HKLM\\"):
        hive, sub = winreg.HKEY_LOCAL_MACHINE, path[5:]
    elif path.startswith("HKCU\\"):
        hive, sub = winreg.HKEY_CURRENT_USER, path[5:]
    else:
        return "bad reg path"
    parent, _, name = sub.rpartition("\\")
    if not name:
        return "refuse root"
    if not _reg_key_exists(hive, sub):
        return None
    try:
        _delete_reg_tree(hive, sub)
        return None
    except OSError as e:
        if _is_already_gone(e):
            return None
        return str(e)


def _delete_reg_tree(hive, subkey: str) -> None:
    with winreg.OpenKey(hive, subkey, 0, winreg.KEY_ALL_ACCESS) as key:
        while True:
            try:
                child = winreg.EnumKey(key, 0)
            except OSError:
                break
            _delete_reg_tree(hive, subkey + "\\" + child)
    winreg.DeleteKey(hive, subkey)


# --------------- UI ---------------

class StripApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {VERSION}")
        self.geometry("1000x600")
        self.minsize(820, 520)

        self.apps: list[InstalledApp] = []
        self.filtered: list[InstalledApp] = []
        self.token: CancelToken | None = None
        self._scan_thread: threading.Thread | None = None
        self._iid_map: dict[str, InstalledApp] = {}
        self._photos: list = []  # keep PhotoImage refs
        self._icon_cache: dict[str, object] = {}
        self._default_icon = self._make_default_icon()
        self._size_job = 0
        self._set_window_icon()

        # 低饱和统一色：浅底深字，危险操作略偏暖/红
        _ui = {
            "sel": "#D0DCEC",
            "sel_fg": "#1E293B",
            "menu_hot": "#C5D4E8",
            "btn_base": dict(
                relief=tk.FLAT,
                bd=0,
                highlightthickness=1,
                highlightbackground="#C5CDD8",
                highlightcolor="#A8B4C4",
                padx=6,
                pady=6,
                cursor="hand2",
                font=("Microsoft YaHei UI", 9),
            ),
        }
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.map(
            "Treeview",
            background=[("selected", _ui["sel"])],
            foreground=[("selected", _ui["sel_fg"])],
        )
        style.configure("Treeview", rowheight=22)

        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)
        ttk.Label(top, text="搜索").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.apply_filter())
        ttk.Entry(top, textvariable=self.search_var, width=40).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="刷新", command=self.reload).pack(side=tk.LEFT)

        mid = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        mid.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self._paned = mid

        left = ttk.Frame(mid)
        right = ttk.Frame(mid, width=200)
        mid.add(left, weight=1)
        mid.add(right, weight=0)

        cols = ("size", "date", "publisher")
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="程序")
        self.tree.heading("size", text="大小")
        self.tree.heading("date", text="安装日期")
        self.tree.heading("publisher", text="发布者")
        self.tree.column("#0", width=360, minwidth=160, stretch=False)
        self.tree.column("size", width=100, minwidth=70, stretch=False, anchor=tk.E)
        self.tree.column("date", width=100, minwidth=80, stretch=False)
        # stretch=False：列宽不随窗口压缩，总宽超出时横向滚动条才有滑块
        self.tree.column("publisher", width=260, minwidth=120, stretch=False)
        sy = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        sx = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)
        # Win10 风格：右键松开时弹出，位置在指针处
        self.tree.bind("<ButtonRelease-3>", self._on_tree_right_click)
        self._tree_menu = tk.Menu(
            self,
            tearoff=0,
            font=("Segoe UI", 10),
            bg="#ffffff",
            fg="#1E293B",
            activebackground=_ui["menu_hot"],
            activeforeground="#1E293B",
            relief=tk.SOLID,
            borderwidth=1,
            activeborderwidth=0,
            disabledforeground="#94A3B8",
        )
        self._tree_menu.add_command(label="打开文件所在的位置", command=self.open_install_location)
        self._tree_menu.add_separator()
        self._tree_menu.add_command(label="卸载并清理", command=self.do_uninstall)
        self._tree_menu.add_command(label="扫描残留", command=self.do_scan)
        self._tree_menu.add_separator()
        self._tree_menu.add_command(label="强制删除并清理", command=self.do_force)

        btns = ttk.Frame(right, padding=4)
        btns.pack(fill=tk.X)
        _b = _ui["btn_base"]
        # 实色白字，亮度适中、同系饱和，主操作亮、危险操作醒目
        tk.Button(
            btns, text="卸载并清理", command=self.do_uninstall,
            bg="#4A90D9", fg="white", activebackground="#3A7BC0", activeforeground="white", **_b,
        ).pack(fill=tk.X, pady=2)
        tk.Button(
            btns, text="扫描残留", command=self.do_scan,
            bg="#45A07A", fg="white", activebackground="#378A66", activeforeground="white", **_b,
        ).pack(fill=tk.X, pady=2)
        tk.Button(
            btns, text="取消扫描", command=self.cancel_scan,
            bg="#8A94A3", fg="white", activebackground="#6F7885", activeforeground="white", **_b,
        ).pack(fill=tk.X, pady=2)
        tk.Button(
            btns, text="删除所选残留", command=self.do_delete,
            bg="#D08950", fg="white", activebackground="#B87340", activeforeground="white", **_b,
        ).pack(fill=tk.X, pady=2)
        tk.Button(
            btns, text="强制删除并清理", command=self.do_force,
            bg="#D05555", fg="white", activebackground="#B84444", activeforeground="white", **_b,
        ).pack(fill=tk.X, pady=2)

        ttk.Label(right, text="程序详情").pack(anchor=tk.W, padx=4, pady=(8, 0))
        self.detail = tk.Text(
            right, height=5, width=28, wrap=tk.WORD, state=tk.DISABLED,
            bg="#F8FAFC", fg="#334155", relief=tk.FLAT, highlightthickness=1,
            highlightbackground="#D8DEE6",
        )
        self.detail.pack(fill=tk.X, padx=4, pady=4)

        ttk.Label(right, text="残留项（删除前请核对）").pack(anchor=tk.W, padx=4)
        hits_frame = ttk.Frame(right)
        hits_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.hits = tk.Listbox(
            hits_frame,
            selectmode=tk.EXTENDED,
            height=8,
            width=28,
            exportselection=False,
            bg="#FFFFFF",
            fg="#334155",
            selectbackground=_ui["sel"],
            selectforeground=_ui["sel_fg"],
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground="#D8DEE6",
            activestyle="none",
        )
        hits_sy = ttk.Scrollbar(hits_frame, orient=tk.VERTICAL, command=self.hits.yview)
        hits_sx = ttk.Scrollbar(hits_frame, orient=tk.HORIZONTAL, command=self.hits.xview)
        self.hits.configure(yscrollcommand=hits_sy.set, xscrollcommand=hits_sx.set)
        hits_frame.grid_rowconfigure(0, weight=1)
        hits_frame.grid_columnconfigure(0, weight=1)
        self.hits.grid(row=0, column=0, sticky="nsew")
        hits_sy.grid(row=0, column=1, sticky="ns")
        hits_sx.grid(row=1, column=0, sticky="ew")
        self._hit_objs: list[Hit] = []

        self.status = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status, padding=6).pack(fill=tk.X, side=tk.BOTTOM)
        self.after(80, self._shrink_right_pane)

    def _shrink_right_pane(self) -> None:
        """Keep action pane narrow on open — list gets most of the width."""
        try:
            self.update_idletasks()
            w = max(self.winfo_width(), 900)
            # right pane ~220px
            self._paned.sashpos(0, w - 240)
        except Exception:
            pass

    def _set_window_icon(self) -> None:
        ico = resource_path("icons", "gafdvel.ico")
        try:
            if os.path.isfile(ico):
                self.iconbitmap(default=ico)
        except Exception:
            try:
                if os.path.isfile(ico):
                    self.iconbitmap(ico)
            except Exception:
                pass

    def _make_default_icon(self):
        """Tiny embedded PNG — no PIL, no disk read."""
        try:
            photo = tk.PhotoImage(data=_DEFAULT_ICON_B64)
            self._photos.append(photo)
            return photo
        except tk.TclError:
            return None

    def _photo_for_fast(self, app: InstalledApp):
        return self._default_icon

    def selected_app(self) -> InstalledApp | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self._iid_map.get(sel[0])

    def _on_tree_right_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.tree.focus(row)
            self.set_detail(self.selected_app())
        app = self.selected_app()
        # 无安装目录且无 MSI 缓存包时灰显（避免点了只弹无法定位）
        can_open = bool(app and self._install_reveal_target(app))
        self._tree_menu.entryconfig(0, state=(tk.NORMAL if can_open else tk.DISABLED))
        # 与 Win10 桌面一致：菜单锚在鼠标指针位置
        try:
            self._tree_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._tree_menu.grab_release()

    def _install_reveal_target(self, app: InstalledApp) -> str:
        """Dir to open or file to /select — empty if nothing useful (no slow MSI index)."""
        loc = app.resolved_install_dir(allow_msi=False)
        if loc and os.path.isdir(loc):
            return loc
        path, _ = parse_icon_spec(app.icon)
        if path and os.path.isfile(path):
            return path
        exe = resolve_exe_from_uninstall(app.uninstall)
        if exe and os.path.isfile(exe):
            return exe
        code = extract_msi_product_code(app.uninstall) or extract_msi_product_code(app.key_path)
        if not code:
            return ""
        ud = _msi_userdata_install_dir(code)
        if ud and os.path.isdir(ud) and not _is_unsafe_size_root(ud):
            return ud
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            if ctypes.windll.msi.MsiGetProductInfoW(code, "InstallLocation", buf, ctypes.byref(n)) == 0:
                loc2 = _clean_dir(buf.value)
                if loc2 and os.path.isdir(loc2) and not _is_unsafe_size_root(loc2):
                    return loc2
        except Exception:
            pass
        pkg = _msi_local_package(code)
        if pkg:
            return pkg
        return ""

    def open_install_location(self) -> None:
        app = self.selected_app()
        if not app:
            messagebox.showinfo(APP_NAME, "请先选择一个程序。")
            return
        target = self._install_reveal_target(app)
        # 仍可走完整 MSI 解析（含组件索引）——仅在快速路径失败时
        if not target:
            loc = app.resolved_install_dir(allow_msi=True)
            if loc and os.path.isdir(loc):
                target = loc
        if not target or not _reveal_in_explorer(target):
            messagebox.showinfo(
                APP_NAME,
                f"「{app.name}」没有独立安装目录（多为运行库/系统组件），无法在资源管理器中打开。",
            )

    def set_detail(self, app: InstalledApp | None) -> None:
        self.detail.configure(state=tk.NORMAL)
        self.detail.delete("1.0", tk.END)
        if not app:
            self.detail.configure(state=tk.DISABLED)
            return
        loc = app.resolved_install_dir(allow_msi=False) or app.install_location or ""
        loc_show = loc if loc else "点击后解析…"
        size_note = ""
        if getattr(app, "_locate_failed", False):
            size_note = "（未找到安装目录）"
        elif getattr(app, "_size_measured", False):
            size_note = "（磁盘实测）"
        elif app.size_display().startswith("~"):
            size_note = "（注册表估算，带~）"
        lines = [
            f"名称：{app.name}",
            f"版本：{app.version or '未知'}",
            f"发布者：{app.publisher or '未知'}",
            f"大小：{app.size_display()}{size_note}",
            f"安装日期：{app.date_display()}",
            f"安装路径：{loc_show}",
            f"卸载命令：{app.uninstall or app.quiet or '未知'}",
        ]
        self.detail.insert(tk.END, "\n".join(lines))
        self.detail.configure(state=tk.DISABLED)

    def on_select(self, _evt=None) -> None:
        self.hits.delete(0, tk.END)
        self._hit_objs = []
        app = self.selected_app()
        self.set_detail(app)
        if not app:
            return
        if not getattr(app, "_size_measured", False):
            self._start_size_calc(app)

    def _on_tree_click(self, event) -> None:
        """单击即可算大小（含再次点击已选中行 / 点「大小」列）。"""
        row = self.tree.identify_row(event.y)
        if not row:
            return
        app = self._iid_map.get(row)
        if not app:
            return
        col = self.tree.identify_column(event.x)
        # #1 = 大小列：强制重算；其它列：未测过则算
        if col == "#1":
            app._size_measured = False  # type: ignore[attr-defined]
            self._start_size_calc(app)
        elif not getattr(app, "_size_measured", False):
            self._start_size_calc(app)

    def _start_size_calc(self, app: InstalledApp) -> None:
        app._size_pending = True  # type: ignore[attr-defined]
        app._locate_failed = False  # type: ignore[attr-defined]
        self._refresh_row(app)
        self.set_detail(app)
        self._size_job += 1
        job = self._size_job
        threading.Thread(target=self._bg_resolve_and_size, args=(app, job), daemon=True).start()

    def _bg_resolve_and_size(self, app: InstalledApp, job: int) -> None:
        # Clear cache so MSI / guess can run with full rules
        if hasattr(app, "_resolved_dir"):
            delattr(app, "_resolved_dir")
        from_reg = bool(
            _refine_size_root(app.name, app.install_location, require_name=False)
            and _name_path_score(app.name, app.install_location) >= 35
        )
        loc = app.resolved_install_dir(allow_msi=True)
        share = int(getattr(app, "_share_n", 1) or 1)
        # Shared vendor root → prefer matching child (Geek shared-location fix)
        if loc and share > 1 and _name_path_score(app.name, loc) < 70:
            child = _best_child_for_app(app.name, loc)
            if child:
                loc = child
                app._resolved_dir = loc  # type: ignore[attr-defined]
                share = 1

        n = folder_size_bytes(loc) if loc else 0
        if loc and share > 1 and n > 0:
            n = max(n // share, 1)

        est = app.estimated_kb * 1024 if app.estimated_kb > 0 else 0
        if loc and est > 512 * 1024 and n > 0 and n < est * 0.2:
            guessed = _guess_install_dir(app)
            if guessed and os.path.normcase(guessed) != os.path.normcase(loc):
                n2 = folder_size_bytes(guessed)
                if n2 > n and n2 >= est * 0.2:
                    loc, n = guessed, n2
                    app._resolved_dir = loc  # type: ignore[attr-defined]
                else:
                    n = 0
            else:
                n = 0
        if n and est and not from_reg:
            if n > max(est * 12, 200 * 1024 * 1024) and est < 100 * 1024 * 1024:
                n = 0

        def apply():
            app._size_pending = False  # type: ignore[attr-defined]
            if loc and n > 0:
                app.size_bytes = n
                app._size_measured = True  # type: ignore[attr-defined]
                app._locate_failed = False  # type: ignore[attr-defined]
            elif est > 0:
                app.size_bytes = est
                app._size_measured = False  # type: ignore[attr-defined]
                app._locate_failed = False  # type: ignore[attr-defined]
            elif loc:
                # Real folder exists but empty
                app.size_bytes = 0
                app._size_measured = True  # type: ignore[attr-defined]
                app._locate_failed = False  # type: ignore[attr-defined]
            else:
                # No install dir — not a measured 0 B (was misleading "磁盘实测")
                app.size_bytes = None
                app._size_measured = False  # type: ignore[attr-defined]
                app._locate_failed = True  # type: ignore[attr-defined]
            self._refresh_row(app)
            if self.selected_app() is app:
                self.set_detail(app)

        self.after(0, apply)

    def _load_icons_idle(self) -> None:
        """Load at most one icon per UI tick — keeps window responsive like Geek."""
        gen = getattr(self, "_icon_gen", 0)
        queue = getattr(self, "_icon_queue", None)
        if not queue or gen != getattr(self, "_icon_gen", 0):
            return
        iid, app = queue.pop(0)
        if self.tree.exists(iid):
            path, idx = app.icon_path()
            key = f"{path}|{idx}"
            if key not in self._icon_cache:
                # extract off UI thread would be better; for one icon it's OK if fast
                # still do extract in thread for this single item
                def work(i=iid, a=app, k=key, p=path, ix=idx, g=gen):
                    _ensure_graphics()
                    pil = extract_icon_image(p, ix, 16) if p else None

                    def apply():
                        if g != getattr(self, "_icon_gen", 0):
                            return
                        if ImageTk and pil is not None:
                            photo = ImageTk.PhotoImage(pil)
                            self._photos.append(photo)
                        else:
                            photo = self._default_icon
                        self._icon_cache[k] = photo
                        if self.tree.exists(i) and photo is not None:
                            self.tree.item(i, image=photo)
                        if getattr(self, "_icon_queue", None):
                            self.after(1, self._load_icons_idle)

                    self.after(0, apply)

                threading.Thread(target=work, daemon=True).start()
                return
            photo = self._icon_cache[key]
            if photo is not None:
                self.tree.item(iid, image=photo)
        if queue:
            self.after(1, self._load_icons_idle)

    def _start_icon_loading(self) -> None:
        self._icon_gen = getattr(self, "_icon_gen", 0) + 1
        self._icon_queue = list(self._iid_map.items())
        self.after(1, self._load_icons_idle)

    def reload(self) -> None:
        self.status.set("正在加载…")
        self.tree.delete(*self.tree.get_children())
        self._iid_map.clear()
        gen = getattr(self, "_reload_gen", 0) + 1
        self._reload_gen = gen

        def work():
            apps = enum_installed()
            _mark_shared_install_dirs(apps)

            def done():
                if gen != self._reload_gen:
                    return
                self.apps = apps
                self._icon_cache.clear()
                self.apply_filter(load_icons=True)
                self.status.set(f"共 {len(self.apps)} 个程序")

            self.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _row_label(self, app: InstalledApp) -> str:
        # Leading spaces: ttk Treeview 图标与文字间距偏紧，用空白拉开
        name = app.name
        ver = (app.version or "").strip()
        if ver:
            return f"   {name}  {ver}"
        return f"   {name}"

    def _row_values(self, app: InstalledApp) -> tuple:
        return (app.size_display(), app.date_display(), app.publisher)

    def _refresh_row(self, app: InstalledApp) -> None:
        for iid, a in self._iid_map.items():
            if a is app and self.tree.exists(iid):
                self.tree.item(iid, text=self._row_label(a), values=self._row_values(a))
                break

    def recalc_size(self) -> None:
        app = self.selected_app()
        if not app:
            messagebox.showinfo(APP_NAME, "请先选择一个程序。")
            return
        app._size_measured = False  # type: ignore[attr-defined]
        app._resolved_dir = None  # type: ignore[attr-defined]  # force re-resolve
        self._start_size_calc(app)

    def apply_filter(self, load_icons: bool = True) -> None:
        q = self.search_var.get().strip().lower()
        self.filtered = [a for a in self.apps if not q or q in a.name.lower() or q in a.publisher.lower()]
        self.tree.delete(*self.tree.get_children())
        self._iid_map.clear()
        self._icon_gen = getattr(self, "_icon_gen", 0) + 1
        self._icon_queue = []
        for i, a in enumerate(self.filtered):
            iid = f"r{i}"
            self._iid_map[iid] = a
            kw = {
                "text": self._row_label(a),
                "values": self._row_values(a),
            }
            photo = self._photo_for_fast(a)
            if photo is not None:
                kw["image"] = photo
            self.tree.insert("", tk.END, iid=iid, **kw)
        self.set_detail(None)
        if load_icons and self.filtered:
            self._start_icon_loading()

    def do_uninstall(self) -> None:
        app = self.selected_app()
        if not app:
            messagebox.showinfo(APP_NAME, "请先选择一个程序。")
            return
        if not messagebox.askyesno(
            APP_NAME,
            f"将卸载该程序，并自动扫描、清理残留。\n\n{app.name}",
        ):
            return
        self.status.set(f"正在卸载 {app.name}…")
        self.update_idletasks()
        code = run_uninstall(app)
        self.status.set(f"卸载结束（{code}），正在自动扫描残留…")
        self.do_scan(prompt_delete=True, cleanup_app=app)

    def cancel_scan(self) -> None:
        if self.token:
            self.token.cancel()
            self.status.set("正在取消…")

    def _finish_remove_from_list(self, app: InstalledApp, msg: str) -> None:
        """Ensure uninstall entry is gone and refresh so the app leaves the list."""
        hive_name = "HKCU" if app.hive == winreg.HKEY_CURRENT_USER else "HKLM"
        delete_hit(Hit("reg", f"{hive_name}\\{app.key_path}"))
        self.hits.delete(0, tk.END)
        self._hit_objs = []
        self.set_detail(None)
        messagebox.showinfo(APP_NAME, msg)
        self.reload()

    def do_scan(self, prompt_delete: bool = False, cleanup_app: InstalledApp | None = None) -> None:
        app = cleanup_app or self.selected_app()
        if not app:
            messagebox.showinfo(APP_NAME, "请先选择一个程序。")
            return
        if self._scan_thread and self._scan_thread.is_alive():
            messagebox.showinfo(APP_NAME, "扫描正在进行，请先取消。")
            return
        self.hits.delete(0, tk.END)
        self._hit_objs = []
        self.token = CancelToken()
        token = self.token

        def work():
            try:
                found = scan_leftovers(
                    app,
                    token,
                    on_status=lambda m: self.after(0, lambda msg=m: self.status.set(msg)),
                )

                def done():
                    self._hit_objs = found
                    for h in found:
                        label = KIND_LABEL.get(h.kind, h.kind)
                        self.hits.insert(tk.END, f"[{label}] {h.path}")
                    if not found:
                        self.status.set("未发现残留。")
                        if prompt_delete:
                            self._finish_remove_from_list(
                                app, f"「{app.name}」已卸载，未发现残留，已从列表移除。"
                            )
                        return
                    self.status.set(f"找到 {len(found)} 项残留。")
                    if prompt_delete:
                        if messagebox.askyesno(
                            APP_NAME,
                            f"扫描完成，发现 {len(found)} 项残留。\n\n是否全部删除？",
                        ):
                            self.do_delete(confirm=False, delete_all=True, cleanup_app=app)
                        else:
                            self._finish_remove_from_list(
                                app,
                                f"「{app.name}」已卸载。残留已保留；已从列表移除。",
                            )
                    else:
                        self.status.set(f"找到 {len(found)} 项残留，请核对后再删除。")

                self.after(0, done)
            except InterruptedError:
                self.after(0, lambda: self.status.set("扫描已取消。"))
            except Exception as e:
                self.after(0, lambda: self.status.set(f"扫描出错: {e}"))

        self.status.set("正在扫描…（可点「取消扫描」）")
        self._scan_thread = threading.Thread(target=work, daemon=True)
        self._scan_thread.start()

    def do_delete(
        self,
        confirm: bool = True,
        delete_all: bool = False,
        cleanup_app: InstalledApp | None = None,
    ) -> None:
        if not self._hit_objs:
            if cleanup_app:
                self._finish_remove_from_list(cleanup_app, f"「{cleanup_app.name}」已清理完成。")
            return
        if delete_all or not self.hits.curselection():
            targets = list(self._hit_objs)
            if confirm and not messagebox.askyesno(
                APP_NAME, f"确定删除全部 {len(targets)} 项残留？"
            ):
                return
        else:
            sel = list(self.hits.curselection())
            targets = [self._hit_objs[i] for i in sel]
            if confirm and not messagebox.askyesno(
                APP_NAME, f"确定删除所选 {len(targets)} 项？"
            ):
                return
        errors = []
        reboot = []
        for h in targets:
            err = delete_hit(h)
            if err:
                if "重启后删除" in err:
                    reboot.append(h.path)
                else:
                    errors.append(f"{h.path}: {err}")
        soft_ok = {os.path.normcase(p) for p in reboot}
        hard_fail = set()
        for e in errors:
            hard_fail.add(os.path.normcase(e.split(":", 1)[0].strip()))
        gone = {
            h.path
            for h in targets
            if os.path.normcase(h.path) not in hard_fail
        }
        self._hit_objs = [h for h in self._hit_objs if h.path not in gone]
        self.hits.delete(0, tk.END)
        for h in self._hit_objs:
            label = KIND_LABEL.get(h.kind, h.kind)
            self.hits.insert(tk.END, f"[{label}] {h.path}")
        parts = []
        if reboot:
            parts.append(
                f"有 {len(reboot)} 项被保护进程锁定，已登记重启后自动删除。\n请重启电脑完成清理。"
            )
        if errors:
            parts.append("部分删除失败：\n" + "\n".join(errors[:12]))
        if parts:
            messagebox.showwarning(APP_NAME, "\n\n".join(parts))
            if cleanup_app and not errors:
                self._finish_remove_from_list(
                    cleanup_app,
                    f"「{cleanup_app.name}」部分残留将在重启后删除，已从列表移除。",
                )
            elif cleanup_app:
                self.reload()
            return
        self.status.set(f"已删除 {len(targets)} 项残留。")
        if cleanup_app:
            self._finish_remove_from_list(
                cleanup_app, f"「{cleanup_app.name}」残留已清理，已从列表移除。"
            )
        elif not self._hit_objs:
            messagebox.showinfo(APP_NAME, "残留已清理完成。")

    def do_force(self) -> None:
        app = self.selected_app()
        if not app:
            messagebox.showinfo(APP_NAME, "请先选择一个程序。")
            return
        if not messagebox.askyesno(
            APP_NAME,
            f"强制删除将尽量静默卸载并自动清理残留。\n\n{app.name}",
        ):
            return
        run_uninstall(app, prefer_quiet=True)
        hive_name = "HKCU" if app.hive == winreg.HKEY_CURRENT_USER else "HKLM"
        delete_hit(Hit("reg", f"{hive_name}\\{app.key_path}"))
        self.status.set(f"正在扫描残留：{app.name}")
        self.do_scan(prompt_delete=True, cleanup_app=app)


def main() -> None:
    ensure_admin()
    app = StripApp()
    app.after(1, app.reload)  # after event loop starts — never freeze first paint
    app.mainloop()


if __name__ == "__main__":
    main()
