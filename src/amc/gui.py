"""Tkinter graphical interface.

Layout notes
------------
* The window is sized from what its contents actually need (``winfo_req*``)
  rather than from a hard-coded geometry, so nothing is ever clipped on first
  open -- including on a scaled display, where the same fonts occupy more room.
  The result is still capped to the desktop, and the minimum size is the
  content size, so the user cannot shrink the window until widgets are cut off.
* Everything sits in one grid. The file list and the log take the slack in a
  3:2 ratio, so they grow and shrink together with the window while buttons,
  entries and labels keep their natural size -- no widget is ever stretched out
  of proportion.
* Hint labels wrap to the current width instead of being truncated.
* Visual style: flat "clam" palette with an accent colour, a column file list,
  and a dark console-style log. Pure ttk/tkinter -- no extra dependencies.
"""

from __future__ import annotations

import os
import queue
import threading
import traceback
from pathlib import Path

from . import __version__
from .core import (AMCCancelled, AMCError, META_FIELDS, Options, _FIELD_CN,
                   _TAG_ALIASES, collect_inputs, convert_file, ffmpeg, ffprobe,
                   fmt_size, probe)
from .nameparse import parse_path as parse_names_from_path

# Comfortable starting size; the real one is computed from the layout.
PREFERRED_WIDTH = 900
PREFERRED_HEIGHT = 660
MUTED = "#5b6470"

# Palette ---------------------------------------------------------------
BG = "#f3f5f9"          # window background
CARD = "#ffffff"        # fields and buttons
ACCENT = "#0a74e8"      # Apple-blue-ish accent
ACCENT_DARK = "#0860c0"
ACCENT_SOFT = "#d6e8fb"
TEXT = "#1d2330"
BORDER = "#cdd5e0"
LOG_BG = "#12161f"      # dark console for the log pane
LOG_FG = "#d6deeb"
OK = "#2fa36b"
FAIL = "#d4524f"
WARN = "#c8811f"


def _apply_style(ttk) -> None:
    """Flat themed look on stock ttk. Everything degrades gracefully."""
    style = ttk.Style()
    if "clam" in style.theme_names():
        style.theme_use("clam")

    style.configure(".", background=BG, foreground=TEXT)
    style.configure("TFrame", background=BG)
    style.configure("TLabelframe", background=BG, bordercolor=BORDER,
                    lightcolor=BG, darkcolor=BG)
    style.configure("TLabelframe.Label", background=BG, foreground=ACCENT,
                    font=("Microsoft YaHei UI", 9, "bold"))
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("TSeparator", background=BORDER)
    style.configure("TCheckbutton", background=BG, foreground=TEXT,
                    focuscolor=BG)
    style.configure("TEntry", fieldbackground=CARD, bordercolor=BORDER,
                    lightcolor=CARD, darkcolor=CARD, padding=4)
    style.map("TEntry",
              fieldbackground=[("disabled", "#e9edf4")],
              foreground=[("disabled", MUTED)])
    style.configure("TCombobox", fieldbackground=CARD, background=CARD,
                    bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                    arrowcolor=ACCENT, padding=4, foreground=TEXT)
    style.map("TCombobox",
              fieldbackground=[("readonly", CARD)],
              selectbackground=[("readonly", CARD)],
              selectforeground=[("readonly", TEXT)],
              bordercolor=[("active", ACCENT)])
    style.configure("TSpinbox", fieldbackground=CARD, bordercolor=BORDER,
                    arrowcolor=ACCENT, padding=4)
    style.configure("Horizontal.TProgressbar", background=ACCENT,
                    troughcolor="#e2e7f0", bordercolor=BORDER, lightcolor=BG,
                    darkcolor=BG, thickness=10)
    style.configure("TButton", background=CARD, foreground=TEXT,
                    bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                    focusthickness=0, padding=(10, 4))
    style.map("TButton",
              background=[("active", "#eef2f8"), ("disabled", "#eceff4")],
              foreground=[("disabled", "#9aa3b2")],
              bordercolor=[("active", ACCENT)])
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                    padding=(14, 5))
    style.map("Accent.TButton",
              background=[("active", ACCENT_DARK), ("disabled", "#9db6cf")],
              foreground=[("disabled", "#ffffff")],
              bordercolor=[("active", ACCENT_DARK)])
    style.configure("TScrollbar", background="#dfe5ee", troughcolor=BG,
                    bordercolor=BG, lightcolor=BG, darkcolor=BG, arrowsize=0)
    style.map("TScrollbar", background=[("active", "#c3ccd9")])
    style.configure("Treeview", background=CARD, fieldbackground=CARD,
                    bordercolor=BORDER, rowheight=26, padding=2)
    style.map("Treeview",
              background=[("selected", ACCENT_SOFT)],
              foreground=[("selected", TEXT)])
    style.configure("Treeview.Heading", background="#e9edf4", foreground=MUTED,
                    bordercolor=BORDER, relief="flat", padding=(6, 3))


# --------------------------------------------------------------------------
# graphical interface
# --------------------------------------------------------------------------

def run_gui(initial: list[str] | None = None) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, font as tkfont, messagebox, ttk
    except ImportError:
        print("没有找到 tkinter，无法打开图形界面。请改用命令行模式：python -m amc --help",
              flush=True)
        return 1

    try:  # crisp text on high-DPI displays
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    root.title(f"AMC · Apple Music 格式转换器 {__version__}")
    root.configure(background=BG)

    files: list[str] = list(initial or [])
    msg_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
    cancel_event = threading.Event()
    state: dict[str, object] = {"thread": None, "running": False, "last_outdir": None}

    base_font = tkfont.nametofont("TkDefaultFont")
    try:
        if "Microsoft YaHei UI" in tkfont.families():
            base_font.configure(family="Microsoft YaHei UI", size=10)
    except tk.TclError:
        pass
    title_font = base_font.copy()
    title_font.configure(size=base_font.cget("size") + 4, weight="bold")
    small_font = base_font.copy()
    small_font.configure(size=max(7, base_font.cget("size") - 1))
    mono_font = tkfont.nametofont("TkFixedFont").copy()
    mono_font.configure(size=base_font.cget("size"))

    _apply_style(ttk)

    # ---------------- layout ----------------
    outer = ttk.Frame(root, padding=(12, 10, 12, 10))
    outer.pack(fill="both", expand=True)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(2, weight=3)      # file list
    outer.rowconfigure(5, weight=2)      # log

    pad = {"padx": 0, "pady": (0, 8)}

    header = ttk.Frame(outer)
    header.grid(row=0, column=0, sticky="ew")
    header.columnconfigure(1, weight=1)
    ttk.Label(header, text="AMC · Apple Music 格式转换器", font=title_font,
              foreground=ACCENT).grid(row=0, column=0, sticky="w")
    ttk.Label(header, text=f"v{__version__}", font=small_font,
              foreground=MUTED).grid(row=0, column=1, sticky="e")
    accent_bar = tk.Frame(outer, height=3, background=ACCENT, bd=0,
                          highlightthickness=0)
    accent_bar.grid(row=1, column=0, sticky="ew", pady=(6, 10))
    accent_bar.grid_propagate(False)

    # ---- file list ----
    list_frame = ttk.LabelFrame(outer, text="待转换文件", padding=(8, 6, 8, 8))
    list_frame.grid(row=2, column=0, sticky="nsew", **pad)
    list_frame.columnconfigure(0, weight=1)
    list_frame.rowconfigure(0, weight=1)

    tree = ttk.Treeview(list_frame, columns=("dir", "size"), height=9,
                        selectmode="extended")
    tree.heading("#0", text="文件", anchor="w")
    tree.heading("dir", text="所在文件夹", anchor="w")
    tree.heading("size", text="大小", anchor="e")
    tree.column("#0", width=340, minwidth=180, stretch=True)
    tree.column("dir", width=260, minwidth=110, stretch=True)
    tree.column("size", width=76, minwidth=56, stretch=False, anchor="e")
    tree.tag_configure("even", background=CARD)
    tree.tag_configure("odd", background="#f7f9fc")
    list_vsb = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
    list_hsb = ttk.Scrollbar(list_frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=list_vsb.set, xscrollcommand=list_hsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    list_vsb.grid(row=0, column=1, sticky="ns")
    list_hsb.grid(row=1, column=0, sticky="ew")

    list_buttons = ttk.Frame(list_frame)
    list_buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
    subfolders_var = tk.BooleanVar(value=True)
    # Per-file manual metadata: {str(Path): {field: value}}. Applied at convert
    # time only to fields the file does not already carry.
    meta_overrides: dict[str, dict] = {}

    def refresh() -> None:
        tree.delete(*tree.get_children())
        for i, f in enumerate(files):
            p = Path(f)
            try:
                size = fmt_size(p.stat().st_size)
            except OSError:
                size = ""
            folder = p.parent.name if p.parent.name else str(p.parent)
            name = p.name
            if meta_overrides.get(str(p)):
                name += "  ✎"
            tree.insert("", "end", iid=str(i), text=name, values=(folder, size),
                        tags=("even" if i % 2 == 0 else "odd",))
        list_frame.configure(text=f"待转换文件（{len(files)} 个）")
        update_buttons()

    def add_files() -> None:
        picked = filedialog.askopenfilenames(
            title="选择音频文件",
            filetypes=[("音频文件", "*.flac *.wav *.aiff *.aif *.ape *.wv *.mp3 *.m4a *.aac "
                                  "*.ogg *.opus *.wma *.dsf *.dff *.tta *.tak *.mka *.mpc "
                                  "*.mp4 *.ac3 *.w64 *.amr *.au *.snd"),
                       ("所有文件", "*.*")])
        for f in picked:
            if f not in files:
                files.append(f)
        refresh()

    def add_folder() -> None:
        picked = filedialog.askdirectory(title="选择文件夹")
        if not picked:
            return
        found = collect_inputs([Path(picked)], recursive=subfolders_var.get())
        added = 0
        for f in found:
            s = str(f)
            if s not in files:
                files.append(s)
                added += 1
        refresh()
        log_line(f"从文件夹加入 {added} 个文件"
                 + ("（含子文件夹）" if subfolders_var.get() else "（不含子文件夹）"))

    def selected_indexes() -> list[int]:
        return sorted(int(iid) for iid in tree.selection())

    def remove_selected() -> None:
        selection = selected_indexes()
        if not selection:
            return
        for idx in reversed(selection):
            meta_overrides.pop(str(Path(files[idx])), None)
            del files[idx]
        refresh()

    def clear_all() -> None:
        files.clear()
        meta_overrides.clear()
        refresh()

    add_files_btn = ttk.Button(list_buttons, text="添加文件…", command=add_files)
    add_files_btn.pack(side="left")
    add_dir_btn = ttk.Button(list_buttons, text="添加文件夹…", command=add_folder)
    add_dir_btn.pack(side="left", padx=(6, 0))
    remove_btn = ttk.Button(list_buttons, text="移除选中", command=remove_selected)
    remove_btn.pack(side="left", padx=(12, 0))
    clear_btn = ttk.Button(list_buttons, text="清空", command=clear_all)
    clear_btn.pack(side="left", padx=(6, 0))
    edit_btn = ttk.Button(list_buttons, text="编辑信息…", state="disabled")
    edit_btn.pack(side="left", padx=(6, 0))
    # Native tk checkbutton: Windows draws the classic "√" box, which ttk's
    # clam indicator cannot be restyled into.
    tk.Checkbutton(list_buttons, text="包含子文件夹", variable=subfolders_var,
                   bg=BG, activebackground=BG, fg=TEXT, activeforeground=TEXT,
                   highlightthickness=0, font=base_font).pack(side="right")

    # ---- metadata dialog ----
    def edit_meta_dialog() -> None:
        """Let the user fill exactly the fields this file is missing."""
        selection = selected_indexes()
        if len(selection) != 1:
            return
        src = Path(files[selection[0]])
        try:
            info = probe(src, repair_tags=True)
        except AMCError as exc:
            messagebox.showerror("无法读取", f"{src.name}\n{exc}")
            return
        guessed = parse_names_from_path(src)
        saved = meta_overrides.get(str(src), {})

        dlg = tk.Toplevel(root)
        dlg.title(f"编辑信息 · {src.name}")
        dlg.transient(root)
        dlg.grab_set()
        dlg.configure(bg=BG)
        body = ttk.Frame(dlg, padding=(16, 12, 16, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, foreground=MUTED, font=small_font, justify="left",
                  text="灰色 = 文件已有的内嵌信息，转换时原样继承、不会改动；\n"
                       "白色 = 缺失字段，填了才会写入（留空跳过，不会被文件名猜测顶替）。"
            ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        vars_: dict[str, tk.StringVar] = {}
        for row, key in enumerate(META_FIELDS, start=1):
            have = info.tag(*_TAG_ALIASES[key])
            ttk.Label(body, text=_FIELD_CN[key]).grid(
                row=row, column=0, sticky="w", pady=2, padx=(0, 8))
            var = tk.StringVar(value=have or saved.get(key, "")
                               or guessed.get(key, ""))
            box = ttk.Entry(body, textvariable=var)
            box.grid(row=row, column=1, sticky="ew", pady=2)
            if have:
                box.state(["disabled"])
            else:
                vars_[key] = var

        def save() -> None:
            values = {k: v.get().strip() for k, v in vars_.items()
                      if v.get().strip()}
            if values:
                meta_overrides[str(src)] = values
            else:
                meta_overrides.pop(str(src), None)
            dlg.destroy()
            refresh()

        btns = ttk.Frame(body)
        btns.grid(row=1 + len(META_FIELDS), column=0, columnspan=2, sticky="ew",
                  pady=(10, 0))
        ttk.Button(btns, text="保存", style="Accent.TButton",
                   command=save).pack(side="right")
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right",
                                                               padx=(0, 6))

        def apply_to_all() -> None:
            """Push the filled-in fields onto every file, missing them or not."""
            values = {k: v.get().strip() for k, v in vars_.items()
                      if v.get().strip()}
            if not values:
                dlg.destroy()
                return
            for path in files:
                overrides = dict(meta_overrides.get(str(Path(path)), {}))
                overrides.update(values)
                meta_overrides[str(Path(path))] = overrides
            dlg.destroy()
            refresh()
            log_line(f"    信息: 已把 {len(values)} 项填给 {len(files)} 个文件，"
                     "已有内嵌值的字段不受影响")

        ttk.Button(btns, text="写入全部", command=apply_to_all).pack(side="left")
        ttk.Label(btns, foreground=MUTED, font=small_font,
                  text="「写入全部」只补空位，不改已有标签").pack(side="left",
                                                            padx=(8, 0))
        dlg.bind("<Return>", lambda _e: save())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.resizable(False, False)
        dlg.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - dlg.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")

    edit_btn.configure(command=edit_meta_dialog)

    # ---- options ----
    opt = ttk.LabelFrame(outer, text="转换设置", padding=(8, 6, 8, 8))
    opt.grid(row=3, column=0, sticky="ew", **pad)
    opt.columnconfigure(1, weight=1)

    out_var = tk.StringVar(value="")
    target_var = tk.StringVar(value="50")
    keepname_var = tk.BooleanVar(value=False)
    overwrite_var = tk.BooleanVar(value=True)
    parsevar_var = tk.BooleanVar(value=True)
    FORMAT_CHOICES = {"自动（优先无损）": "auto", "MP3": "mp3",
                      "AAC": "aac", "ALAC 无损": "alac"}
    fmt_var = tk.StringVar(value="自动（优先无损）")

    ttk.Label(opt, text="输出文件夹").grid(row=0, column=0, sticky="w")
    out_entry = ttk.Entry(opt, textvariable=out_var)
    out_entry.grid(row=0, column=1, sticky="ew", padx=(8, 6))

    def pick_out() -> None:
        picked = filedialog.askdirectory(title="选择输出文件夹")
        if picked:
            out_var.set(picked)

    ttk.Button(opt, text="浏览…", command=pick_out).grid(row=0, column=2, sticky="e")

    ttk.Label(opt, text="单曲上限").grid(row=1, column=0, sticky="w", pady=(8, 0))
    target_box = ttk.Frame(opt)
    target_box.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
    ttk.Spinbox(target_box, from_=0, to=2000, increment=5, width=7,
                textvariable=target_var).pack(side="left")
    ttk.Label(target_box, text="MB（0 = 不限制）", foreground=MUTED).pack(
        side="left", padx=(4, 0))

    ttk.Label(opt, text="输出格式").grid(row=2, column=0, sticky="w", pady=(8, 0))
    fmt_box = ttk.Combobox(opt, textvariable=fmt_var, state="readonly",
                           width=16, values=list(FORMAT_CHOICES))
    fmt_box.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

    checks = ttk.Frame(opt)
    checks.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
    checks.columnconfigure(0, weight=1)
    checks.columnconfigure(1, weight=1)
    for row, (text, var) in enumerate((
            ("覆盖已有的输出文件", overwrite_var),
            ("保留原文件名", keepname_var),
            ("按文件名自动补全缺失信息", parsevar_var))):
        tk.Checkbutton(checks, text=text, variable=var, bg=BG,
                       activebackground=BG, fg=TEXT, activeforeground=TEXT,
                       highlightthickness=0, font=base_font).grid(
            row=row // 2, column=row % 2, sticky="w", pady=(0, 2))

    hint = ttk.Label(
        opt, font=small_font, foreground=MUTED, justify="left",
        text="输出文件夹留空表示与源文件同目录；单曲上限填 0 表示不限制。"
             "超出上限时按「降位深 → 降采样 → 转 AAC」逐级降级，每一步都实测文件大小，"
             "封面和歌词也计入上限。缺失的标题/歌手/专辑会从文件名和文件夹名自动补全，"
             "已有的标签永远优先；补不全的可选中文件后用「编辑信息…」手动填写。")
    hint.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))

    # ---- actions ----
    act = ttk.Frame(outer)
    act.grid(row=4, column=0, sticky="ew", **pad)
    act.columnconfigure(2, weight=1)
    start_btn = ttk.Button(act, text="开始转换", style="Accent.TButton")
    start_btn.grid(row=0, column=0, sticky="w")
    cancel_btn = ttk.Button(act, text="取消", state="disabled")
    cancel_btn.grid(row=0, column=1, sticky="w", padx=(6, 12))
    progress = ttk.Progressbar(act, mode="determinate")
    progress.grid(row=0, column=2, sticky="ew")
    progress_label = ttk.Label(act, text="", width=9, anchor="e", foreground=MUTED)
    progress_label.grid(row=0, column=3, sticky="e", padx=(10, 0))

    # ---- log ----
    log_frame = ttk.LabelFrame(outer, text="日志", padding=(8, 6, 8, 8))
    log_frame.grid(row=5, column=0, sticky="nsew", **pad)
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)
    log_text = tk.Text(log_frame, height=9, wrap="word", state="disabled",
                       background=LOG_BG, foreground=LOG_FG, insertbackground=LOG_FG,
                       relief="flat", highlightthickness=1,
                       highlightbackground=BORDER, font=mono_font, padx=8, pady=6)
    for tag, colour in (("ok", OK), ("fail", FAIL), ("warn", WARN),
                        ("muted", "#8fa1bb")):
        log_text.tag_configure(tag, foreground=colour)
    log_vsb = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    log_text.configure(yscrollcommand=log_vsb.set)
    log_text.grid(row=0, column=0, sticky="nsew")
    log_vsb.grid(row=0, column=1, sticky="ns")

    def _tag_for(text: str) -> str | None:
        if text.startswith("  ✓"):
            return "ok"
        if text.startswith("  ✗") or text.startswith("✗"):
            return "fail"
        if text.startswith("  ⏹") or "⚠" in text:
            return "warn"
        if text.startswith("    ") or text.startswith("  源:"):
            return "muted"
        return None

    def log_line(text: str) -> None:
        log_text.configure(state="normal")
        log_text.insert("end", text + "\n", _tag_for(text))
        log_text.see("end")
        log_text.configure(state="disabled")

    # ---- status bar ----
    ttk.Separator(outer, orient="horizontal").grid(row=6, column=0, sticky="ew",
                                                   pady=(4, 0))
    status_bar = ttk.Frame(outer)
    status_bar.grid(row=7, column=0, sticky="ew", pady=(6, 0))
    status_bar.columnconfigure(1, weight=1)
    open_out_btn = ttk.Button(status_bar, text="打开输出文件夹", state="disabled")
    open_out_btn.grid(row=0, column=0, sticky="w")
    status = ttk.Label(status_bar, text="就绪", anchor="w")
    status.grid(row=0, column=1, sticky="ew", padx=(10, 0))

    def open_outdir() -> None:
        target = state.get("last_outdir")
        if not target:
            return
        try:
            os.startfile(str(target))          # noqa: S606 - Windows shell open
        except (AttributeError, OSError) as exc:
            messagebox.showerror("无法打开", f"{target}\n{exc}")

    open_out_btn.configure(command=open_outdir)

    # ---------------- worker ----------------
    def set_running(running: bool) -> None:
        state["running"] = running
        for widget in (add_files_btn, add_dir_btn, remove_btn, clear_btn):
            widget.configure(state="disabled" if running else "normal")
        start_btn.configure(state="disabled" if running else "normal")
        cancel_btn.configure(state="normal" if running else "disabled")
        update_buttons()

    def update_buttons() -> None:
        idle = not state["running"]
        remove_btn.configure(state="normal" if idle and files else "disabled")
        clear_btn.configure(state="normal" if idle and files else "disabled")
        edit_btn.configure(state="normal" if idle and len(selected_indexes()) == 1
                           else "disabled")

    def worker_main(targets: list[Path], opts: Options) -> None:
        ok = warn = fail = 0
        for i, src in enumerate(targets, 1):
            if cancel_event.is_set():
                break
            msg_queue.put(("log", f"[{i}/{len(targets)}] {src.name}"))
            msg_queue.put(("status", f"处理中 {i}/{len(targets)}：{src.name}"))
            try:
                res = convert_file(src, opts, lambda m: msg_queue.put(("log", m)),
                                   cancel=cancel_event,
                                   overrides=meta_overrides.get(str(src)))
            except AMCCancelled:
                msg_queue.put(("log", "  ⏹ 已取消"))
                break
            except Exception:
                msg_queue.put(("log", "  ✗ 未预期错误:\n" + traceback.format_exc()))
                fail += 1
                msg_queue.put(("tick", (i, len(targets))))
                continue

            if not res.ok:
                fail += 1
                msg_queue.put(("log", f"  ✗ 失败: {res.error}"))
            else:
                if res.warnings:
                    warn += 1
                extra = f"   ⚠ {'; '.join(res.warnings)}" if res.warnings else ""
                msg_queue.put(("log", f"  ✓ {res.dest.name}  {fmt_size(res.size)}"
                                      f"  [{res.plan.label()}]"))
                msg_queue.put(("log", f"    歌词: {res.lyrics_from}   封面: "
                                      f"{res.cover_from}   信息: {res.meta_from}{extra}"))
                ok += 1
            msg_queue.put(("tick", (i, len(targets))))

        msg_queue.put(("finished", {"ok": ok, "warn": warn, "fail": fail,
                                    "cancelled": cancel_event.is_set()}))

    def finish(summary: dict) -> None:
        set_running(False)
        cancelled = bool(summary["cancelled"])
        status.configure(text="已取消" if cancelled else "完成")
        progress_label.configure(text="")
        log_line("")
        log_line(f"完成: 成功 {summary['ok']}，有警告 {summary['warn']}，"
                 f"失败 {summary['fail']}" + ("（已取消）" if cancelled else ""))
        if summary["ok"]:
            open_out_btn.configure(state="normal")
            log_line("导入 Apple Music 前，请先删除资料库里同名的旧曲目，否则会出现重复曲目。")

    def pump() -> None:
        try:
            while True:
                kind, payload = msg_queue.get_nowait()
                if kind == "log":
                    log_line(str(payload))
                elif kind == "status":
                    status.configure(text=str(payload))
                elif kind == "tick":
                    done, total = payload          # type: ignore[misc]
                    progress.configure(value=done, maximum=total)
                    progress_label.configure(text=f"{done}/{total}")
                elif kind == "finished":
                    finish(payload)                # type: ignore[arg-type]
                    break
        except queue.Empty:
            pass
        # Rescheduled unconditionally: returning early here used to kill the
        # pump for good, so a second run never updated the log again.
        root.after(120, pump)

    def start() -> None:
        if state["running"]:
            return
        if not files:
            messagebox.showinfo("提示", "请先添加要转换的文件。")
            return
        try:
            target = float(target_var.get())
        except ValueError:
            messagebox.showerror("错误", "单曲上限必须是数字（0 表示不限制）。")
            target_box.focus_set()
            return
        if target < 0:
            messagebox.showerror("错误", "单曲上限不能是负数。")
            target_box.focus_set()
            return

        out_text = out_var.get().strip()
        outdir = Path(out_text).expanduser() if out_text else None
        if outdir is not None:
            try:
                outdir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("错误", f"无法创建输出文件夹:\n{exc}")
                out_entry.focus_set()
                return

        targets = [Path(f) for f in files]
        opts = Options(
            outdir=outdir,
            target_mb=target,
            keep_name=keepname_var.get(),
            force_alac=False,
            out_format=FORMAT_CHOICES[fmt_var.get()],
            overwrite=overwrite_var.get(),
            parse_names=parsevar_var.get(),
        )
        state["last_outdir"] = outdir or targets[0].parent
        cancel_event.clear()
        progress.configure(maximum=len(targets), value=0)
        progress_label.configure(text=f"0/{len(targets)}")
        set_running(True)
        log_line("")
        limit = "不限制" if target <= 0 else f"{target:g}MB"
        log_line(f"开始处理 {len(targets)} 个文件，单曲上限 {limit}"
                 + ("" if opts.overwrite else "，跳过已存在的输出")
                 + ("" if opts.out_format == "auto"
                    else f"，强制输出 {fmt_var.get()}"))
        thread = threading.Thread(target=worker_main, args=(targets, opts), daemon=True)
        state["thread"] = thread
        thread.start()

    def cancel() -> None:
        if not state["running"]:
            return
        cancel_event.set()
        cancel_btn.configure(state="disabled")
        status.configure(text="正在停止…")

    start_btn.configure(command=start)
    cancel_btn.configure(command=cancel)

    def on_close() -> None:
        if state["running"]:
            if not messagebox.askokcancel("退出", "正在转换，确定要停止并退出吗？"):
                return
            cancel_event.set()
            status.configure(text="正在停止…")
            # Give run() a moment to kill the ffmpeg child before the UI goes away.
            root.after(600, root.destroy)
            return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    # ---------------- keyboard shortcuts ----------------
    # Scoped to the list so Delete never eats a keystroke meant for the entries.
    def select_all(_event=None):
        tree.selection_set(*tree.get_children())
        return "break"

    tree.bind("<Delete>", lambda _event: remove_selected())
    tree.bind("<Control-a>", select_all)
    tree.bind("<<TreeviewSelect>>", lambda _event: update_buttons())
    tree.bind("<Double-1>", lambda _event: (root.after_idle(edit_meta_dialog)
                                            if edit_btn.instate(["!disabled"])
                                            else None))
    root.bind("<Control-o>", lambda _event: add_files())
    root.bind("<Control-Return>", lambda _event: start())
    root.bind("<Escape>", lambda _event: cancel())

    # ---------------- environment banner ----------------
    log_line(f"AMC {__version__}")
    try:
        log_line(f"ffmpeg : {ffmpeg()}")
        log_line(f"ffprobe: {ffprobe()}")
    except AMCError as exc:
        log_line(f"✗ {exc}")
        start_btn.configure(state="disabled")

    # ---------------- content-driven sizing ----------------
    # Measure what the layout actually needs, then clamp it to the desktop. A
    # fixed geometry clips widgets on a scaled display or a small screen.
    refresh()

    wrap_state = {"width": 0}

    def apply_wrap(win_width: int) -> None:
        """Wrap the long hint instead of truncating it at narrow widths."""
        if abs(win_width - wrap_state["width"]) < 8:
            return      # ignore small churn: re-wrapping would loop Configure
        wrap_state["width"] = win_width
        hint.configure(wraplength=max(320, win_width - 60))

    def on_resize(event) -> None:
        if event.widget is root:
            apply_wrap(event.width)

    # Wrap *before* measuring: an unwrapped label asks for its whole single-line
    # width, which would size the entire window to the length of one sentence.
    apply_wrap(PREFERRED_WIDTH)
    root.update_idletasks()
    need_w, need_h = root.winfo_reqwidth(), root.winfo_reqheight()
    screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    avail_w = max(640, screen_w - 80)
    avail_h = max(480, screen_h - 110)

    width = min(max(need_w, PREFERRED_WIDTH), avail_w)
    height = min(max(need_h, PREFERRED_HEIGHT), avail_h)
    # Open a little roomier than the bare minimum so the list and the log have
    # something to show, but never past what the desktop can hold.
    width += max(0, min(avail_w - width, int(width * 0.06)))
    height += max(0, min(avail_h - height, int(height * 0.08)))

    root.minsize(min(need_w, avail_w), min(need_h, avail_h))
    root.geometry(f"{width}x{height}+{max(0, (screen_w - width) // 2)}"
                  f"+{max(0, (screen_h - height) // 3)}")
    apply_wrap(width)

    root.bind("<Configure>", on_resize)
    root.after(120, pump)
    root.mainloop()
    return 0
