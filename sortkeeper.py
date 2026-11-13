#!/usr/bin/env python3
"""
SortKeeper — tidy your Downloads folder in one click.
by One Man Developer (onemandeveloper.com)

  • Scans Downloads (or any folder you pick). Only loose top-level files are
    touched — existing subfolders are left alone.
  • Sorts by type into Images, Screenshots, Videos, Screen Recordings, Audio,
    PDFs, Documents, Spreadsheets, Presentations, Archives, Installers, Code,
    Fonts, Design and Other.
  • Finds duplicates (same size + same SHA-256) and parks the extra copies in a
    "Duplicates" folder for you to review — nothing is ever deleted.
  • Animated Sorter shows every file flying into its folder.
  • One-click Undo for the last run. 100% local: no account, no upload.

Runs on Ubuntu / Linux, Windows 10+ and macOS with Python 3.8+ (Tkinter only,
no pip installs).

  GUI:   python3 sortkeeper.py
  CLI:   python3 sortkeeper.py --cli [FOLDER]            # preview only
         python3 sortkeeper.py --cli --apply [FOLDER]    # move files
         python3 sortkeeper.py --cli --undo [FOLDER]     # undo last run

Ubuntu needs Tk once:   sudo apt install python3-tk
Build a single binary:  pip install pyinstaller
                        pyinstaller --onefile --windowed --name SortKeeper sortkeeper.py
"""

import hashlib
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict

APP_NAME = "SortKeeper"
APP_VERSION = "1.1.0"
SUPPORT_URL = "https://onemandeveloper.com/sortkeeper/?ref=app#support"
HISTORY_FILE = ".sortkeeper_history.json"
DUP_FOLDER = "Duplicates"

# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------
CATEGORIES = [
    # (folder name, colour, extensions)
    ("Images", "#E0A458", "jpg jpeg png gif webp bmp tif tiff heic heif svg ico avif raw cr2 nef arw dng jfif"),
    ("Screenshots", "#F2C57C", ""),
    ("Videos", "#D1495B", "mp4 mkv mov avi wmv flv webm m4v 3gp mpeg mpg ts"),
    ("Screen Recordings", "#EDAE49", ""),
    ("Audio", "#8E7DBE", "mp3 wav flac aac ogg m4a wma opus aiff aif mid midi"),
    ("PDFs", "#C0392B", "pdf"),
    ("Documents", "#3D8EB9", "doc docx odt rtf txt md pages epub tex log"),
    ("Spreadsheets", "#2E9E6B", "xls xlsx xlsm ods csv tsv numbers"),
    ("Presentations", "#E07A5F", "ppt pptx odp key"),
    ("Archives", "#B5838D", "zip rar 7z tar gz tgz bz2 xz zst iso lz lzma cab"),
    ("Installers", "#5C7AEA", "exe msi deb rpm appimage dmg pkg apk snap flatpakref run bin"),
    ("Code", "#43AA8B", "py js ts jsx tsx html htm css scss json xml yml yaml sh bat ps1 php java c cpp h hpp go rs rb sql ipynb kt swift dart vue liquid"),
    ("Fonts", "#90A955", "ttf otf woff woff2 eot"),
    ("Design", "#F28482", "psd ai fig xd sketch indd blend eps cdr afdesign"),
    ("Other", "#8D8D8D", ""),
]
CAT_COLOR = {name: col for name, col, _ in CATEGORIES}
CAT_COLOR[DUP_FOLDER] = "#E63946"
EXT_MAP = {}
for _name, _col, _exts in CATEGORIES:
    for _e in _exts.split():
        EXT_MAP[_e] = _name

SCREENSHOT_RE = re.compile(r"(screen\s?shot|screenshot|screen\s?cap|capture|snip|scrot|flameshot)", re.I)
RECORDING_RE = re.compile(r"(screen\s?record|screencast|screen\s?capture|recording|kazam|obs[-_ ]|peek)", re.I)
COPY_RE = re.compile(r"(\s\(\d+\)|[-_ ]copy(\s?\d+)?|\s-\s\s?copy)$", re.I)

SKIP_SUFFIXES = (".crdownload", ".part", ".partial", ".download", ".opdownload", ".tmp", ".!ut", ".aria2")
SKIP_NAMES = {"desktop.ini", ".ds_store", "thumbs.db", HISTORY_FILE}


def categorize(filename, use_hints=True):
    stem, ext = os.path.splitext(filename)
    ext = ext.lower().lstrip(".")
    if filename.lower().endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        ext = "gz"
    cat = EXT_MAP.get(ext, "Other")
    if use_hints:
        if cat == "Images" and SCREENSHOT_RE.search(stem):
            return "Screenshots"
        if cat == "Videos" and RECORDING_RE.search(stem):
            return "Screen Recordings"
    return cat


# --------------------------------------------------------------------------
# Folder helpers
# --------------------------------------------------------------------------
def default_downloads():
    home = os.path.expanduser("~")
    system = platform.system()
    if system == "Windows":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
            path = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")[0]
            if os.path.isdir(path):
                return path
        except Exception:
            pass
    elif system == "Linux":
        try:
            out = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=3)
            path = out.stdout.strip()
            if path and os.path.isdir(path) and os.path.abspath(path) != os.path.abspath(home):
                return path
        except Exception:
            pass
    path = os.path.join(home, "Downloads")
    return path if os.path.isdir(path) else home


def open_in_file_manager(path):
    try:
        if platform.system() == "Windows":
            os.startfile(path)  # noqa
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def unique_target(folder, name):
    """Return a path inside folder that doesn't exist yet: name, name (1), name (2)…"""
    target = os.path.join(folder, name)
    if not os.path.exists(target):
        return target
    stem, ext = os.path.splitext(name)
    i = 1
    while True:
        target = os.path.join(folder, f"{stem} ({i}){ext}")
        if not os.path.exists(target):
            return target
        i += 1


# --------------------------------------------------------------------------
# Scanning + duplicate detection
# --------------------------------------------------------------------------
def _hash(path, limit=None, chunk=1024 * 1024):
    h = hashlib.sha256()
    read = 0
    with open(path, "rb") as f:
        while True:
            size = chunk if limit is None else min(chunk, limit - read)
            if size <= 0:
                break
            data = f.read(size)
            if not data:
                break
            h.update(data)
            read += len(data)
    return h.hexdigest()


def list_loose_files(folder, include_hidden=False):
    files = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                n = e.name
                if n.lower() in SKIP_NAMES:
                    continue
                if not include_hidden and n.startswith("."):
                    continue
                if n.lower().endswith(SKIP_SUFFIXES):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                files.append({"name": n, "path": e.path, "size": st.st_size, "mtime": st.st_mtime})
    except OSError as ex:
        raise RuntimeError(f"Can't read folder: {ex}")
    files.sort(key=lambda f: f["name"].lower())
    return files


def find_duplicates(files, progress=None):
    """Returns {path_of_extra_copy: path_of_kept_original}."""
    by_size = defaultdict(list)
    for f in files:
        if f["size"] > 0:
            by_size[f["size"]].append(f)
    groups = [g for g in by_size.values() if len(g) > 1]
    total = sum(len(g) for g in groups) or 1
    done = 0
    extras = {}
    for group in groups:
        quick = defaultdict(list)
        for f in group:
            try:
                quick[_hash(f["path"], limit=64 * 1024)].append(f)
            except OSError:
                pass
            done += 1
            if progress:
                progress(done / total)
        for same_head in quick.values():
            if len(same_head) < 2:
                continue
            if same_head[0]["size"] <= 64 * 1024:
                full = {"whole": same_head}  # the quick hash already covered the whole file
            else:
                full = defaultdict(list)
                for f in same_head:
                    try:
                        full[_hash(f["path"])].append(f)
                    except OSError:
                        pass
            for same in full.values():
                if len(same) < 2:
                    continue
                # keep the "cleanest" name (no "(1)" / "copy"), then the oldest
                same.sort(key=lambda f: (bool(COPY_RE.search(os.path.splitext(f["name"])[0])), f["mtime"], len(f["name"])))
                keep = same[0]
                for extra in same[1:]:
                    extras[extra["path"]] = keep["path"]
    return extras


def build_plan(folder, find_dups=True, use_hints=True, progress=None):
    files = list_loose_files(folder)
    dups = find_duplicates(files, progress) if find_dups else {}
    plan = []
    for f in files:
        if f["path"] in dups:
            dest = DUP_FOLDER
            note = "copy of " + os.path.basename(dups[f["path"]])
        else:
            dest = categorize(f["name"], use_hints)
            note = ""
        plan.append({**f, "dest": dest, "note": note})
    return plan


# --------------------------------------------------------------------------
# Moving + undo
# --------------------------------------------------------------------------
def _history_path(folder):
    return os.path.join(folder, HISTORY_FILE)


def load_history(folder):
    try:
        with open(_history_path(folder), "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(folder, history):
    path = _history_path(folder)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(history[-20:], fh, indent=1)
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(path, 0x02)  # hidden
        except Exception:
            pass


def move_one(folder, item):
    """Move a single plan item. Returns (src, dst) or raises."""
    target_dir = os.path.join(folder, item["dest"])
    os.makedirs(target_dir, exist_ok=True)
    dst = unique_target(target_dir, item["name"])
    shutil.move(item["path"], dst)
    return item["path"], dst


def undo_last(folder):
    history = load_history(folder)
    if not history:
        return 0, 0
    session = history.pop()
    restored = failed = 0
    created_dirs = set()
    for mv in reversed(session.get("moves", [])):
        src, dst = mv["src"], mv["dst"]
        created_dirs.add(os.path.dirname(dst))
        if not os.path.exists(dst):
            failed += 1
            continue
        try:
            back = src if not os.path.exists(src) else unique_target(os.path.dirname(src), os.path.basename(src))
            shutil.move(dst, back)
            restored += 1
        except OSError:
            failed += 1
    for d in created_dirs:
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
    save_history(folder, history)
    return restored, failed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def run_cli(args):
    apply_ = "--apply" in args
    undo = "--undo" in args
    rest = [a for a in args if not a.startswith("--")]
    folder = os.path.abspath(os.path.expanduser(rest[0])) if rest else default_downloads()
    print(f"{APP_NAME} {APP_VERSION} — {folder}")
    if undo:
        r, f = undo_last(folder)
        print(f"Undo: restored {r} file(s), {f} could not be restored.")
        return
    plan = build_plan(folder)
    if not plan:
        print("Nothing to organise — no loose files.")
        return
    counts = defaultdict(int)
    for p in plan:
        counts[p["dest"]] += 1
        print(f"  {p['name'][:60]:<60} -> {p['dest']}" + (f"  ({p['note']})" if p["note"] else ""))
    print("\nSummary: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if not apply_:
        print("\nPreview only. Run again with --apply to move the files.")
        return
    moves = []
    for p in plan:
        try:
            s, d = move_one(folder, p)
            moves.append({"src": s, "dst": d})
        except OSError as ex:
            print(f"  ! {p['name']}: {ex}")
    hist = load_history(folder)
    hist.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "moves": moves})
    save_history(folder, hist)
    print(f"Moved {len(moves)} file(s). Undo with --undo.")
    if moves:
        print(f"\nSaved you time? SortKeeper is free. Support it here: {SUPPORT_URL}")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    import webbrowser
    from tkinter import ttk, filedialog, messagebox

    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    BG = "#0E0B0C"
    PANEL = "#171213"
    PANEL2 = "#1F1819"
    LINE = "#2E2526"
    TEXT = "#F3ECEA"
    MUTED = "#A8999A"
    ACCENT = "#B3122E"
    ACCENT_H = "#D11A3A"
    GOLD = "#F2C57C"
    SANS = "Segoe UI" if platform.system() == "Windows" else ("SF Pro Text" if platform.system() == "Darwin" else "Ubuntu")

    def F(size, weight="normal"):
        return (SANS, size, weight)

    class App:
        def __init__(self, root):
            self.root = root
            self.folder = default_downloads()
            self.plan = []
            self.q = queue.Queue()
            self.busy = False
            self.bins = {}
            self.anim_items = []
            self.find_dups = tk.BooleanVar(value=True)
            self.use_hints = tk.BooleanVar(value=True)

            root.title(f"{APP_NAME} — Organize your Downloads")
            root.configure(bg=BG)
            root.geometry("1120x720")
            root.minsize(940, 620)
            self._style()
            self._build()
            root.after(60, self._poll)
            root.after(250, self.scan)

        # ---------- styling ----------
        def _style(self):
            s = ttk.Style(self.root)
            try:
                s.theme_use("clam")
            except tk.TclError:
                pass
            s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        bordercolor=LINE, rowheight=26, font=F(10))
            s.configure("Treeview.Heading", background=PANEL2, foreground=MUTED, relief="flat", font=F(9, "bold"))
            s.map("Treeview", background=[("selected", "#3A1B20")], foreground=[("selected", TEXT)])
            s.map("Treeview.Heading", background=[("active", PANEL2)])
            s.configure("SK.Horizontal.TProgressbar", troughcolor=PANEL2, background=ACCENT,
                        bordercolor=PANEL2, lightcolor=ACCENT, darkcolor=ACCENT)
            s.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=PANEL, bordercolor=PANEL,
                        arrowcolor=MUTED)

        def _btn(self, parent, text, cmd, primary=False):
            b = tk.Label(parent, text=text, font=F(10, "bold"), fg="#FFFFFF" if primary else TEXT,
                         bg=ACCENT if primary else PANEL2, padx=16, pady=8, cursor="hand2")
            b._base = b["bg"]
            b._hover = ACCENT_H if primary else LINE
            b._enabled = True
            b.bind("<Enter>", lambda e: b._enabled and b.configure(bg=b._hover))
            b.bind("<Leave>", lambda e: b.configure(bg=b._base if b._enabled else PANEL))
            b.bind("<Button-1>", lambda e: b._enabled and cmd())
            return b

        def _enable(self, b, on):
            b._enabled = on
            if on:
                b.configure(bg=b._base, fg="#FFFFFF" if b._base == ACCENT else TEXT, cursor="hand2")
            else:
                b.configure(bg=PANEL, fg="#5E5354", cursor="arrow")

        # ---------- layout ----------
        def _build(self):
            top = tk.Frame(self.root, bg=BG)
            top.pack(fill="x", padx=22, pady=(18, 8))
            logo = tk.Canvas(top, width=40, height=40, bg=BG, highlightthickness=0)
            logo.pack(side="left")
            self._rrect(logo, 2, 2, 38, 38, 10, fill=ACCENT, outline="")
            for i, c in enumerate(("#FFFFFF", "#F2C57C", "#FFFFFF")):
                logo.create_rectangle(10, 11 + i * 7, 30 - i * 5, 15 + i * 7, fill=c, outline="")
            tt = tk.Frame(top, bg=BG)
            tt.pack(side="left", padx=12)
            tk.Label(tt, text=APP_NAME, font=F(17, "bold"), fg=TEXT, bg=BG).pack(anchor="w")
            tk.Label(tt, text="Categorize files, catch duplicates, tidy up — all on your machine.",
                     font=F(9), fg=MUTED, bg=BG).pack(anchor="w")

            right = tk.Frame(top, bg=BG)
            right.pack(side="right")
            self.btn_scan = self._btn(right, "Scan", self.scan, primary=True)
            self.btn_scan.pack(side="right")
            self.btn_choose = self._btn(right, "Choose folder…", self.choose)
            self.btn_choose.pack(side="right", padx=8)
            self.lbl_folder = tk.Label(right, text="", font=F(9), fg=MUTED, bg=BG, anchor="e", width=42)
            self.lbl_folder.pack(side="right", padx=4)

            # stats
            stats = tk.Frame(self.root, bg=BG)
            stats.pack(fill="x", padx=22, pady=(4, 10))
            self.stat = {}
            for key, label in (("files", "Loose files"), ("size", "Total size"),
                               ("cats", "Folders to create"), ("dups", "Duplicates found")):
                card = tk.Frame(stats, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
                card.pack(side="left", fill="x", expand=True, padx=(0 if key == "files" else 10, 0))
                v = tk.Label(card, text="–", font=F(18, "bold"), fg=TEXT, bg=PANEL)
                v.pack(anchor="w", padx=14, pady=(10, 0))
                tk.Label(card, text=label.upper(), font=F(8, "bold"), fg=MUTED, bg=PANEL).pack(anchor="w", padx=14, pady=(0, 10))
                self.stat[key] = v

            # main
            main = tk.Frame(self.root, bg=BG)
            main.pack(fill="both", expand=True, padx=22)
            left = tk.Frame(main, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
            left.pack(side="left", fill="both", expand=True)
            hdr = tk.Frame(left, bg=PANEL)
            hdr.pack(fill="x", padx=14, pady=(10, 0))
            tk.Label(hdr, text="THE SORTER", font=F(8, "bold"), fg=MUTED, bg=PANEL).pack(side="left")
            self.lbl_sorter = tk.Label(hdr, text="", font=F(8), fg=MUTED, bg=PANEL)
            self.lbl_sorter.pack(side="right")
            self.cv = tk.Canvas(left, bg=PANEL, highlightthickness=0, height=360)
            self.cv.pack(fill="both", expand=True, padx=6, pady=6)
            self.cv.bind("<Configure>", lambda e: self._layout_bins())

            rightp = tk.Frame(main, bg=PANEL, highlightthickness=1, highlightbackground=LINE, width=430)
            rightp.pack(side="left", fill="both", padx=(12, 0))
            rightp.pack_propagate(False)
            tk.Label(rightp, text="PLAN", font=F(8, "bold"), fg=MUTED, bg=PANEL).pack(anchor="w", padx=14, pady=(10, 4))
            tvf = tk.Frame(rightp, bg=PANEL)
            tvf.pack(fill="both", expand=True, padx=8, pady=(0, 8))
            self.tv = ttk.Treeview(tvf, columns=("dest", "size"), show="tree headings", selectmode="browse")
            self.tv.heading("#0", text="File")
            self.tv.heading("dest", text="Goes to")
            self.tv.heading("size", text="Size")
            self.tv.column("#0", width=200, stretch=True)
            self.tv.column("dest", width=120, stretch=False)
            self.tv.column("size", width=70, stretch=False, anchor="e")
            self.tv.tag_configure("dup", foreground="#FF8A93")
            self.tv.tag_configure("done", foreground="#6B5F60")
            sb = ttk.Scrollbar(tvf, orient="vertical", command=self.tv.yview)
            self.tv.configure(yscrollcommand=sb.set)
            self.tv.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")

            # bottom
            bot = tk.Frame(self.root, bg=BG)
            bot.pack(fill="x", padx=22, pady=14)
            opts = tk.Frame(bot, bg=BG)
            opts.pack(side="left")
            for var, text in ((self.find_dups, "Find duplicates"), (self.use_hints, "Screenshot & recording hints")):
                tk.Checkbutton(opts, text=text, variable=var, command=self.scan, font=F(9), fg=TEXT, bg=BG,
                               selectcolor=PANEL2, activebackground=BG, activeforeground=TEXT,
                               highlightthickness=0, bd=0).pack(side="left", padx=(0, 14))
            self.btn_go = self._btn(bot, "Organize", self.organize, primary=True)
            self.btn_go.pack(side="right")
            self.btn_undo = self._btn(bot, "Undo last", self.undo)
            self.btn_undo.pack(side="right", padx=8)
            self.btn_open = self._btn(bot, "Open folder", lambda: open_in_file_manager(self.folder))
            self.btn_open.pack(side="right")
            self.btn_support = self._btn(bot, "\u2665 Support", self.support)
            self.btn_support.configure(fg=GOLD)
            self.btn_support.pack(side="right", padx=(0, 8))

            status = tk.Frame(self.root, bg=BG)
            status.pack(fill="x", padx=22, pady=(0, 14))
            self.pb = ttk.Progressbar(status, style="SK.Horizontal.TProgressbar", mode="determinate", maximum=1.0)
            self.pb.pack(fill="x")
            self.lbl_status = tk.Label(status, text="", font=F(9), fg=MUTED, bg=BG, anchor="w")
            self.lbl_status.pack(fill="x", pady=(6, 0))
            foot = tk.Label(status, text=f"v{APP_VERSION} · free forever · 100% local · onemandeveloper.com",
                            font=F(8), fg="#6B5F60", bg=BG, anchor="e", cursor="hand2")
            foot.bind("<Button-1>", lambda e: self.support())
            foot.place(relx=1.0, rely=1.0, anchor="se")
            self._set_folder_label()

        # ---------- canvas helpers ----------
        def _rrect(self, cv, x1, y1, x2, y2, r, **kw):
            pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                   x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
            return cv.create_polygon(pts, smooth=True, **kw)

        def _inbox_xy(self):
            w = max(self.cv.winfo_width(), 400)
            return w / 2, 58

        def _layout_bins(self):
            cv = self.cv
            cv.delete("all")
            self.bins = {}
            w = max(cv.winfo_width(), 400)
            h = max(cv.winfo_height(), 300)
            ix, iy = self._inbox_xy()
            # inbox
            self._rrect(cv, ix - 120, iy - 38, ix + 120, iy + 38, 14, fill=PANEL2, outline=LINE)
            cv.create_text(ix, iy - 12, text=os.path.basename(self.folder.rstrip("\\/")) or self.folder,
                           fill=TEXT, font=F(12, "bold"))
            self.inbox_count = cv.create_text(ix, iy + 13, text="", fill=MUTED, font=F(9))
            counts = defaultdict(int)
            for p in self.plan:
                counts[p["dest"]] += 1
            cats = [c for c, _, _ in CATEGORIES if counts.get(c)] + ([DUP_FOLDER] if counts.get(DUP_FOLDER) else [])
            if not cats:
                msg = "Pick a folder and press Scan." if not self.busy else "Scanning…"
                if self.plan == [] and not self.busy and getattr(self, "_scanned", False):
                    msg = "All tidy — no loose files here. ✨"
                cv.create_text(w / 2, h * 0.62, text=msg, fill=MUTED, font=F(11))
                cv.itemconfigure(self.inbox_count, text="")
                return
            cv.itemconfigure(self.inbox_count, text=f"{len(self.plan)} loose files")
            gap = 10
            per_row = max(1, min(len(cats), int((w - 20 + gap) / (112 + gap))))
            rows = (len(cats) + per_row - 1) // per_row
            bw = min(128, (w - 20 - gap * (per_row - 1)) / per_row)
            avail = h - (iy + 38) - 36  # space below the inbox card, leaving room for the flight arc
            bh = max(46, min(74, (avail - gap * (rows - 1)) / rows))
            top = h - rows * (bh + gap) + gap - 8
            compact = bh < 62
            for i, c in enumerate(cats):
                r, k = divmod(i, per_row)
                in_row = min(per_row, len(cats) - r * per_row)
                rw = in_row * bw + (in_row - 1) * gap
                x1 = (w - rw) / 2 + k * (bw + gap)
                y1 = top + r * (bh + gap)
                col = CAT_COLOR.get(c, "#8D8D8D")
                body = self._rrect(cv, x1, y1, x1 + bw, y1 + bh, 12, fill=PANEL2, outline=LINE)
                if compact:
                    cv.create_rectangle(x1 + 10, y1 + 11, x1 + 22, y1 + 20, fill=col, outline="")
                    cv.create_text(x1 + 28, y1 + 16, text={"Screen Recordings": "Recordings", "Presentations": "Slides"}.get(c, c), anchor="w", fill=TEXT, font=F(9, "bold"))
                    cnt = cv.create_text(x1 + 10, y1 + bh - 13, text=f"0 / {counts[c]}", anchor="w", fill=MUTED, font=F(9))
                else:
                    cv.create_rectangle(x1 + 10, y1 + 10, x1 + 26, y1 + 22, fill=col, outline="")
                    cv.create_polygon(x1 + 10, y1 + 10, x1 + 16, y1 + 10, x1 + 18, y1 + 7, x1 + 10, y1 + 7, fill=col, outline="")
                    cv.create_text(x1 + 10, y1 + 38, text=c, anchor="w", fill=TEXT, font=F(9, "bold"))
                    cnt = cv.create_text(x1 + 10, y1 + 57, text=f"0 / {counts[c]}", anchor="w", fill=MUTED, font=F(9))
                self.bins[c] = {"x": x1 + bw / 2, "y": y1 + bh / 2, "body": body, "cnt": cnt,
                                "n": 0, "total": counts[c], "col": col}

        # ---------- actions ----------
        def support(self):
            try:
                webbrowser.open(SUPPORT_URL)
            except Exception:
                messagebox.showinfo(APP_NAME, f"Support SortKeeper at:\n{SUPPORT_URL}")

        def _nudge_support(self):
            """After a successful sort, gently highlight the Support button for a few seconds."""
            b = self.btn_support
            b._base, b._hover = GOLD, "#F7D49A"
            b.configure(bg=GOLD, fg="#140C0D")

            def back():
                b._base, b._hover = PANEL2, LINE
                b.configure(bg=PANEL2, fg=GOLD)
            self.root.after(6000, back)

        def _set_folder_label(self):
            p = self.folder
            self.lbl_folder.configure(text=("…" + p[-40:]) if len(p) > 41 else p)

        def _set_busy(self, on):
            self.busy = on
            for b in (self.btn_scan, self.btn_choose, self.btn_go, self.btn_undo):
                self._enable(b, not on)

        def choose(self):
            d = filedialog.askdirectory(initialdir=self.folder, title="Choose a folder to organize")
            if d:
                self.folder = os.path.abspath(d)
                self._set_folder_label()
                self.scan()

        def scan(self):
            if self.busy:
                return
            self._set_busy(True)
            self.plan = []
            self.tv.delete(*self.tv.get_children())
            self._layout_bins()
            self.lbl_status.configure(text="Scanning…")
            self.pb.configure(value=0)
            folder, dups, hints = self.folder, self.find_dups.get(), self.use_hints.get()

            def work():
                try:
                    plan = build_plan(folder, dups, hints, progress=lambda v: self.q.put(("progress", v)))
                    self.q.put(("plan", plan))
                except Exception as ex:
                    self.q.put(("error", str(ex)))
            threading.Thread(target=work, daemon=True).start()

        def _show_plan(self, plan):
            self._scanned = True
            self.plan = plan
            self.tv.delete(*self.tv.get_children())
            for i, p in enumerate(plan):
                self.tv.insert("", "end", iid=str(i), text=p["name"], values=(p["dest"], human_size(p["size"])),
                               tags=("dup",) if p["dest"] == DUP_FOLDER else ())
            total = sum(p["size"] for p in plan)
            dups = sum(1 for p in plan if p["dest"] == DUP_FOLDER)
            self.stat["files"].configure(text=str(len(plan)))
            self.stat["size"].configure(text=human_size(total) if plan else "–")
            self.stat["cats"].configure(text=str(len({p["dest"] for p in plan})))
            self.stat["dups"].configure(text=str(dups), fg="#FF8A93" if dups else TEXT)
            self.pb.configure(value=0)
            self.busy = False
            self._layout_bins()
            hist = load_history(self.folder)
            self.lbl_sorter.configure(text=f"Last run: {hist[-1]['time']}" if hist else "")
            if plan:
                self.lbl_status.configure(text=f"Ready: {len(plan)} files → {len({p['dest'] for p in plan})} folders. "
                                               f"Press Organize to sort them.")
            else:
                self.lbl_status.configure(text="Nothing to organize. Your folder is tidy.")
            self._set_busy(False)
            if not hist:
                self._enable(self.btn_undo, False)
            if not plan:
                self._enable(self.btn_go, False)

        def organize(self):
            if self.busy or not self.plan:
                return
            n = len(self.plan)
            nf = len({p["dest"] for p in self.plan})
            if not messagebox.askyesno(APP_NAME, f"Move {n} files into {nf} folders inside\n{self.folder}?\n\n"
                                                 "Nothing is deleted, and you can Undo afterwards."):
                return
            self._set_busy(True)
            self.moves = []
            self.errors = 0
            self.queue_items = list(enumerate(self.plan))
            self.launched = 0
            self.landed = 0
            # speed scales with the number of files
            self.interval = 110 if n < 40 else (55 if n < 150 else 18)
            self.frames = 22 if n < 40 else (16 if n < 150 else 10)
            self.lbl_status.configure(text="Sorting…")
            self._launch_next()
            self._animate()

        def _launch_next(self):
            if not self.queue_items:
                return
            idx, item = self.queue_items.pop(0)
            try:
                src, dst = move_one(self.folder, item)
                self.moves.append({"src": src, "dst": dst})
                ok = True
            except OSError:
                self.errors += 1
                ok = False
            self.launched += 1
            remaining = len(self.plan) - self.launched
            self.cv.itemconfigure(self.inbox_count, text=f"{remaining} loose files")
            self.tv.item(str(idx), tags=("done",))
            if self.launched % 5 == 0 or remaining == 0:
                self.tv.see(str(idx))
            b = self.bins.get(item["dest"])
            if b and ok:
                ix, iy = self._inbox_xy()
                name = item["name"] if len(item["name"]) <= 22 else item["name"][:19] + "…"
                tw = 9 + len(name) * 6.2
                chip = self._rrect(self.cv, ix - tw / 2, iy + 22, ix + tw / 2, iy + 44, 10, fill=b["col"], outline="")
                txt = self.cv.create_text(ix, iy + 33, text=name, fill="#140C0D", font=F(8, "bold"))
                self.anim_items.append({"chip": chip, "txt": txt, "t": 0, "x0": ix, "y0": iy + 33,
                                        "x1": b["x"], "y1": b["y"], "cx": (ix + b["x"]) / 2 + (b["x"] - ix) * 0.15,
                                        "cy": min(iy, b["y"]) - 20, "cat": item["dest"], "px": ix, "py": iy + 33})
            else:
                self.landed += 1
            self.root.after(self.interval, self._launch_next)

        def _animate(self):
            alive = []
            for a in self.anim_items:
                a["t"] += 1
                t = min(1.0, a["t"] / self.frames)
                e = 1 - (1 - t) ** 3
                x = (1 - e) ** 2 * a["x0"] + 2 * (1 - e) * e * a["cx"] + e * e * a["x1"]
                y = (1 - e) ** 2 * a["y0"] + 2 * (1 - e) * e * a["cy"] + e * e * a["y1"]
                dx, dy = x - a["px"], y - a["py"]
                self.cv.move(a["chip"], dx, dy)
                self.cv.move(a["txt"], dx, dy)
                a["px"], a["py"] = x, y
                if t >= 1.0:
                    self.cv.delete(a["chip"])
                    self.cv.delete(a["txt"])
                    b = self.bins[a["cat"]]
                    b["n"] += 1
                    self.cv.itemconfigure(b["cnt"], text=f"{b['n']} / {b['total']}",
                                          fill=TEXT if b["n"] == b["total"] else MUTED)
                    self.cv.itemconfigure(b["body"], outline=b["col"], width=2)
                    self.root.after(160, lambda body=b["body"]: self.cv.itemconfigure(body, outline=LINE, width=1))
                    self.landed += 1
                else:
                    alive.append(a)
            self.anim_items = alive
            self.pb.configure(value=self.landed / max(1, len(self.plan)))
            if self.landed >= len(self.plan) and not self.queue_items and not self.anim_items:
                self._finish()
            else:
                self.root.after(16, self._animate)

        def _finish(self):
            hist = load_history(self.folder)
            if self.moves:
                hist.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "moves": self.moves})
                try:
                    save_history(self.folder, hist)
                except OSError:
                    pass
            msg = f"Done! Sorted {len(self.moves)} files."
            if self.errors:
                msg += f" {self.errors} couldn't be moved (in use or no permission)."
            self.lbl_status.configure(text=msg + " Use Undo last to put everything back."
                                      + (" Saved you time? Tap \u2665 Support." if self.moves else ""))
            if self.moves:
                self._nudge_support()
            self.lbl_sorter.configure(text="Last run: just now")
            self.plan_done = True
            self._set_busy(False)
            self._enable(self.btn_go, False)
            self.stat["files"].configure(text="0")

        def undo(self):
            if self.busy:
                return
            hist = load_history(self.folder)
            if not hist:
                messagebox.showinfo(APP_NAME, "Nothing to undo in this folder.")
                return
            last = hist[-1]
            if not messagebox.askyesno(APP_NAME, f"Put back the {len(last['moves'])} files sorted on {last['time']}?"):
                return
            r, f = undo_last(self.folder)
            self.lbl_status.configure(text=f"Undo: restored {r} files" + (f", {f} were missing." if f else "."))
            self.scan()

        # ---------- worker messages ----------
        def _poll(self):
            try:
                while True:
                    kind, val = self.q.get_nowait()
                    if kind == "progress":
                        self.pb.configure(value=val)
                        self.lbl_status.configure(text=f"Checking for duplicates… {int(val * 100)}%")
                    elif kind == "plan":
                        self._show_plan(val)
                    elif kind == "error":
                        self._set_busy(False)
                        self.lbl_status.configure(text=val)
                        messagebox.showerror(APP_NAME, val)
            except queue.Empty:
                pass
            self.root.after(60, self._poll)

    root = tk.Tk()
    root.app = App(root)
    root.mainloop()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        run_cli(sys.argv[1:])
    else:
        try:
            run_gui()
        except ImportError:
            print("Tkinter is missing. On Ubuntu run:  sudo apt install python3-tk\n"
                  "Or use the command-line mode:      python3 sortkeeper.py --cli [FOLDER]")
            sys.exit(1)
