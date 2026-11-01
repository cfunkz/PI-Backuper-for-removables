import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from . import __author__, __description__, __license__, __title__, __url__, __version__
from . import disks, imaging
from .fmt import fmt_bytes, fmt_eta

ICON_FILE = "favicon.ico"  # Windows wants .ico for the title bar
ICON_PNG = "icon.png"      # everyone else takes a PNG
IMAGE_TYPES = [("Disk image", "*.img *.img.gz"), ("All files", "*.*")]
SPEED_WINDOW = 4.0  # seconds of history used for speed and ETA
BUTTON_WIDTH = 13   # in characters; keeps the main buttons lined up

INCOMPLETE = ("The device is now blank or only partly written. "
              "Run the restore or clone again before using it.")


def resource_path(name):
    base = getattr(sys, "_MEIPASS", None)
    return Path(base, name) if base else Path(__file__).resolve().parent.parent / name


class App(ttk.Frame):
    def __init__(self, root):
        super().__init__(root)
        self.root = root
        self.pack(fill="both", expand=True, padx=18, pady=(14, 16))

        # Workers never touch Tk. They put messages here and _poll() applies them.
        self.events = queue.Queue()
        self.job = None
        self.disks = []
        self.samples = deque()
        self.phase = None
        self.quit_when_done = False
        self.picked_backup = None  # the save dialog already asked about overwriting this one

        self.backup_path = tk.StringVar()
        self.restore_path = tk.StringVar()
        self.compress = tk.BooleanVar(value=True)
        self.verify = tk.BooleanVar(value=True)

        self.progress = tk.DoubleVar(value=0)
        self.status = tk.StringVar(value="Ready")
        self.detail = tk.StringVar(value="")

        self._build()
        self.refresh()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)

    # ---------- layout ----------

    def _build(self):
        menubar = tk.Menu(self.root)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menubar)

        _setup_style(self.root)
        self.columnconfigure(0, weight=1, uniform="half")
        self.columnconfigure(1, weight=1, uniform="half")

        # Device
        dev = self._section(0, 0, "Device", "used by backup and restore", span=2)
        self.disk_box = ttk.Combobox(dev, state="readonly", width=60)
        self.disk_box.grid(row=0, column=0, sticky="ew")
        self.refresh_btn = ttk.Button(dev, text="Refresh", width=BUTTON_WIDTH, command=self.refresh)
        self.refresh_btn.grid(row=0, column=1, padx=(8, 0))

        # Backup and restore, side by side
        b = self._section(1, 0, "Backup", "device → image file", padx=(0, 10))
        self.backup_entry = ttk.Entry(b, textvariable=self.backup_path, width=30)
        self.backup_entry.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.backup_pick = ttk.Button(b, text="Save as…", command=self.pick_backup)
        self.backup_pick.grid(row=1, column=0, sticky="w")
        self.backup_btn = ttk.Button(b, text="Start backup", width=BUTTON_WIDTH, command=self.start_backup)
        self.backup_btn.grid(row=1, column=1, sticky="e")

        r = self._section(1, 1, "Restore", "image file → device, erases it", padx=(10, 0))
        self.restore_entry = ttk.Entry(r, textvariable=self.restore_path, width=30)
        self.restore_entry.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.restore_pick = ttk.Button(r, text="Open image…", command=self.pick_restore)
        self.restore_pick.grid(row=1, column=0, sticky="w")
        self.restore_btn = ttk.Button(r, text="Start restore", width=BUTTON_WIDTH, command=self.start_restore)
        self.restore_btn.grid(row=1, column=1, sticky="e")

        # Clone
        c = self._section(2, 0, "Clone", "A → B, erases B", span=2)
        c.columnconfigure(0, weight=0)
        c.columnconfigure(1, weight=1)
        ttk.Label(c, text="A").grid(row=0, column=0, padx=(0, 8))
        self.src_box = ttk.Combobox(c, state="readonly")
        self.src_box.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        ttk.Label(c, text="B").grid(row=1, column=0, padx=(0, 8))
        self.dst_box = ttk.Combobox(c, state="readonly")
        self.dst_box.grid(row=1, column=1, sticky="ew")
        self.clone_btn = ttk.Button(c, text="Start clone", width=BUTTON_WIDTH, command=self.start_clone)
        self.clone_btn.grid(row=1, column=2, padx=(8, 0))

        # Options
        opt = self._section(3, 0, "Options", span=2)
        opt.columnconfigure(0, weight=0)
        self.compress_chk = ttk.Checkbutton(opt, text="Gzip compress backups",
                                            variable=self.compress, command=self._fix_extension)
        self.compress_chk.grid(row=0, column=0, sticky="w", padx=(0, 20))
        self.verify_chk = ttk.Checkbutton(opt, text="Verify after writing (slower, catches bad cards)",
                                          variable=self.verify)
        self.verify_chk.grid(row=0, column=1, sticky="w")

        # Progress
        ttk.Separator(self).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 12))
        prog = ttk.Frame(self)
        prog.grid(row=5, column=0, columnspan=2, sticky="ew")
        prog.columnconfigure(0, weight=1)
        ttk.Progressbar(prog, variable=self.progress, maximum=100).grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        # Status and speed share a two-line slot, so a long message wraps
        # instead of resizing the window. The speed line only shows during a job.
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        prog.rowconfigure(1, minsize=2 * line + 8)
        text = ttk.Frame(prog)
        text.grid(row=1, column=0, sticky="nsew")
        status = ttk.Label(text, textvariable=self.status, justify="left", wraplength=500)
        status.pack(anchor="w")
        text.bind("<Configure>", lambda e: status.config(wraplength=max(e.width, 200)))
        detail = ttk.Label(text, textvariable=self.detail, style="Hint.TLabel")
        self.detail.trace_add("write", lambda *_: detail.pack(anchor="w") if self.detail.get()
                              else detail.pack_forget())
        self.cancel_btn = ttk.Button(prog, text="Cancel", width=BUTTON_WIDTH, command=self.cancel,
                                     state="disabled")
        self.cancel_btn.grid(row=1, column=1, sticky="e", padx=(12, 0))

        self._inputs = [self.disk_box, self.refresh_btn, self.backup_entry, self.backup_pick,
                        self.backup_btn, self.restore_entry, self.restore_pick, self.restore_btn,
                        self.src_box, self.dst_box, self.clone_btn, self.compress_chk, self.verify_chk]

    def _section(self, row, col, title, hint="", span=1, padx=0):
        """A bold title with a grey hint next to it, and a frame underneath for the controls."""
        box = ttk.Frame(self)
        box.grid(row=row, column=col, columnspan=span, sticky="nsew", padx=padx, pady=(0, 14))
        box.columnconfigure(0, weight=1)
        head = ttk.Frame(box)
        head.grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Label(head, text=title, style="Title.TLabel").pack(side="left")
        if hint:
            ttk.Label(head, text=hint, style="Hint.TLabel").pack(side="left", padx=(8, 0))
        body = ttk.Frame(box)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        return body

    def _about(self):
        messagebox.showinfo("About", f"{__title__} v{__version__}\n{__description__}\n\n"
                                     f"Author: {__author__}\nLicense: {__license__}\n{__url__}")

    # ---------- device list ----------

    def refresh(self):
        try:
            self.disks = disks.list_disks()
        except Exception as e:
            self.disks = []
            self.status.set(f"Couldn't list disks: {e}")
        labels = [d.label for d in self.disks]
        for box in (self.disk_box, self.src_box, self.dst_box):
            old = box.get()
            box["values"] = labels
            if old in labels:
                box.current(labels.index(old))
            elif labels:
                box.current(0)
            else:
                box.set("")
        if len(labels) > 1 and self.dst_box.current() == self.src_box.current():
            self.dst_box.current(1 if self.src_box.current() == 0 else 0)
        if not self.job:
            self.status.set(f"{len(labels)} removable device(s) found" if labels
                            else "No removable devices found. Plug one in and press Refresh.")

    def _selected(self, box):
        i = box.current()
        if i < 0 or i >= len(self.disks):
            raise UserError("Pick a device first.")
        # Re-read the hardware list now: the card may have been swapped since Refresh.
        return disks.recheck(self.disks[i])

    # ---------- file pickers ----------

    def _extension(self):
        return ".img.gz" if self.compress.get() else ".img"

    def _fix_extension(self):
        p = self.backup_path.get().strip()
        if not p:
            return
        base = p[:-7] if p.lower().endswith(".img.gz") else p[:-4] if p.lower().endswith(".img") else p
        self.backup_path.set(base + self._extension())

    def pick_backup(self):
        p = filedialog.asksaveasfilename(defaultextension=self._extension(), filetypes=IMAGE_TYPES)
        if p:
            self.picked_backup = os.path.abspath(p)
            self.backup_path.set(os.path.normpath(p))

    def pick_restore(self):
        p = filedialog.askopenfilename(filetypes=IMAGE_TYPES)
        if p:
            self.restore_path.set(os.path.normpath(p))

    # ---------- starting jobs ----------

    def start_backup(self):
        self._guarded(self._start_backup)

    def start_restore(self):
        self._guarded(self._start_restore)

    def start_clone(self):
        self._guarded(self._start_clone)

    def _guarded(self, fn):
        if self.job:
            return
        try:
            fn()
        except UserError as e:
            messagebox.showwarning(__title__, str(e))
        except Exception as e:
            messagebox.showerror(__title__, str(e))

    def _start_backup(self):
        out = self.backup_path.get().strip()
        if not out:
            raise UserError("Choose where to save the image first.")
        out = os.path.abspath(out)
        folder = os.path.dirname(out)
        if not os.path.isdir(folder):
            raise UserError(f"Folder doesn't exist:\n{folder}")
        if os.path.exists(out) and out != self.picked_backup:
            if not messagebox.askyesno(__title__, f"{out}\n\nalready exists. Replace it?"):
                return
        disk = self._selected(self.disk_box)
        if disk.id in disks.disks_holding(folder):
            raise UserError("You can't save the image onto the device you're backing up.")

        free = shutil.disk_usage(folder).free
        compress, verify = self.compress.get(), self.verify.get()
        if free < disk.size:
            if not compress:
                raise UserError(f"Not enough space. The image needs {fmt_bytes(disk.size)} "
                                f"but only {fmt_bytes(free)} is free.")
            if not messagebox.askyesno(__title__, f"Only {fmt_bytes(free)} free and the device is "
                                       f"{fmt_bytes(disk.size)}.\n\nCompressed images are usually much "
                                       "smaller, but it might not fit. Try anyway?"):
                return

        def work(job):
            with disks.keep_awake(), \
                    disks.claim_source(disk) as in_use, \
                    disks.open_disk(disk) as dev:
                if in_use:
                    self.events.put(("note", f"{' '.join(in_use)} couldn't be locked. "
                                             "Don't change anything on it until the backup finishes."))
                imaging.backup(dev, out, job, compress=compress, verify=verify)
            return f"Backup complete{' and verified' if verify else ''}: {out}"

        self._run(work, "Backup cancelled. Nothing was saved.", "")

    def _start_restore(self):
        img = self.restore_path.get().strip()
        if not img or not os.path.isfile(img):
            raise UserError("Choose an image file to restore.")
        img = os.path.abspath(img)
        disk = self._selected(self.disk_box)
        if disk.id in disks.disks_holding(img):
            raise UserError("The image is stored on the device you're restoring to.")

        note = "" if imaging.read_sidecar(img) else \
            "\n\nThere's no .hash.txt next to this image, so the image itself can't be checked."
        if not messagebox.askyesno("Erase device?", f"EVERYTHING on this device will be erased:\n\n"
                                   f"{disk.label}\n\nRestore {os.path.basename(img)} to it?{note}",
                                   icon="warning", default="no"):
            return
        disk = disks.recheck(disk)
        verify = self.verify.get()

        def work(job):
            with disks.keep_awake(), \
                    disks.claim_target(disk), \
                    disks.open_disk(disk, writable=True) as dev:
                imaging.restore(img, dev, job, verify=verify)
            return f"Restore complete{' and verified' if verify else ''}. Safe to remove the device."

        self._run(work, "Restore cancelled. " + INCOMPLETE, INCOMPLETE)

    def _start_clone(self):
        a = self._selected(self.src_box)
        b = self._selected(self.dst_box)
        if a.id == b.id:
            raise UserError("A and B must be different devices.")
        if not messagebox.askyesno("Erase device?", f"EVERYTHING on B will be erased:\n\n"
                                   f"A (copy from): {a.label}\nB (erase):     {b.label}\n\nContinue?",
                                   icon="warning", default="no"):
            return
        a, b = disks.recheck(a), disks.recheck(b)
        verify = self.verify.get()

        def work(job):
            with disks.keep_awake(), \
                    disks.claim_source(a) as in_use, \
                    disks.claim_target(b), \
                    disks.open_disk(a) as src, \
                    disks.open_disk(b, writable=True) as dst:
                if in_use:
                    self.events.put(("note", f"{' '.join(in_use)} on A couldn't be locked. "
                                             "Don't change anything on it until the clone finishes."))
                imaging.clone(src, dst, job, verify=verify)
            return f"Clone complete{' and verified' if verify else ''}. Safe to remove both devices."

        self._run(work, "Clone cancelled. " + INCOMPLETE, INCOMPLETE)

    def _run(self, work, cancelled_msg, failure_note):
        self.job = imaging.Job(on_progress=lambda *p: self.events.put(("progress",) + p))
        job = self.job
        self._set_busy(True)
        self.progress.set(0)
        self.status.set("Starting…")
        self.detail.set("")
        self.phase = None

        def thread():
            try:
                self.events.put(("done", work(job)))
            except imaging.Cancelled:
                self.events.put(("done", cancelled_msg))
            except Exception as e:
                msg = str(e) or type(e).__name__
                self.events.put(("error", f"{msg}\n\n{failure_note}".strip()))

        threading.Thread(target=thread, name="job", daemon=True).start()

    def cancel(self):
        if self.job:
            self.job.cancel()
            self.status.set("Cancelling…")
            self.cancel_btn.config(state="disabled")

    def _set_busy(self, busy):
        for w in self._inputs:
            if busy:
                w.config(state="disabled")
            else:
                w.config(state="readonly" if isinstance(w, ttk.Combobox) else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")

    def _on_close(self):
        if not self.job:
            self.root.destroy()
        elif messagebox.askyesno(__title__, "A job is still running. Cancel it and quit?"):
            self.quit_when_done = True
            self.cancel()

    # ---------- worker → UI ----------

    def _poll(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _handle(self, event):
        kind = event[0]
        if kind == "progress":
            self._show_progress(*event[1:])
        elif kind == "note":
            messagebox.showwarning(__title__, event[1])
        elif kind in ("done", "error"):
            self.job = None
            self._set_busy(False)
            self.detail.set("")
            if kind == "done":
                self.status.set(event[1])
            else:
                self.status.set("Failed")
                if not self.quit_when_done:
                    messagebox.showerror(__title__, event[1])
            if self.quit_when_done:
                self.root.destroy()

    def _show_progress(self, phase, done, fraction):
        now = time.monotonic()
        if phase != self.phase:
            self.phase = phase
            self.samples.clear()
        self.samples.append((now, done, fraction))
        while len(self.samples) > 2 and now - self.samples[0][0] > SPEED_WINDOW:
            self.samples.popleft()

        speed = eta = None
        t0, d0, f0 = self.samples[0]
        if now - t0 > 0.5:
            speed = (done - d0) / (now - t0)
            rate = (fraction - f0) / (now - t0)
            eta = (1 - fraction) / rate if rate > 0 else None

        self.progress.set(fraction * 100)
        self.status.set(f"{phase}: {fmt_bytes(done)}  ({fraction * 100:.1f}%)")
        parts = [f"{fmt_bytes(speed)}/s" if speed else "", fmt_eta(eta)]
        self.detail.set("   ".join(p for p in parts if p))


class UserError(Exception):
    """Something the user can fix; shown as a warning, not an error."""


def _setup_style(root):
    style = ttk.Style(root)
    # Native look on Windows and macOS, tidy fallback on Linux.
    for theme in ("vista", "aqua", "clam"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break
    if sys.platform != "darwin":  # macOS picks a sensible size itself; 10 would look tiny there
        for name in ("TkDefaultFont", "TkTextFont"):
            tkfont.nametofont(name).configure(size=10)
    base = tkfont.nametofont("TkDefaultFont").actual()
    style.configure("Title.TLabel", font=(base["family"], abs(base["size"]) + 1, "bold"))
    style.configure("Hint.TLabel", foreground="#6e6e6e")


def _dpi_aware():
    # Without this Windows scales the whole window up as a bitmap and it looks blurry.
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _set_icon(root):
    try:
        if sys.platform == "win32":
            root.iconbitmap(resource_path(ICON_FILE))
        else:
            root.iconphoto(True, tk.PhotoImage(file=resource_path(ICON_PNG)))
    except Exception:
        pass  # no icon is better than no app


def main():
    if "--version" in sys.argv[1:]:
        # Used by the release build to check the packaged app starts at all.
        if sys.stdout:
            print(f"{__title__} {__version__}")
        return
    _dpi_aware()
    root = tk.Tk()
    root.title(f"{__title__} v{__version__}")
    _set_icon(root)

    def report(exc, value, tb):
        import traceback
        messagebox.showerror("Unexpected error", "".join(traceback.format_exception(exc, value, tb)))

    root.report_callback_exception = report

    if not disks.is_admin():
        root.withdraw()  # just the message box, no empty window behind it
        who = "Administrator" if sys.platform == "win32" else "root"
        if messagebox.askyesno(__title__, f"Raw disk access needs {who} rights.\n\n"
                                          f"Restart PI-Backuper as {who}?"):
            try:
                disks.relaunch_as_admin()
            except RuntimeError as e:
                messagebox.showerror(__title__, str(e))
        root.destroy()
        return

    App(root)
    # Let the window take exactly the size its contents need, and never shrink below that.
    root.update_idletasks()
    root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())
    root.resizable(True, False)  # wider is useful, taller would only add empty space
    root.mainloop()
