"""Tkinter graphical interface."""

from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path

from .core import (AMCError, Options, collect_inputs, convert_file, fmt_size,
                   ffmpeg, ffprobe)


# --------------------------------------------------------------------------
# graphical interface
# --------------------------------------------------------------------------

def run_gui(initial: list[str] | None = None) -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    try:  # crisp text on high-DPI displays
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    root.title("AMC · Apple Music 格式转换器")
    root.geometry("880x640")
    root.minsize(760, 560)

    files: list[str] = list(initial or [])
    msg_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
    worker: dict[str, object] = {"thread": None, "cancel": False}

    # ---------------- layout ----------------
    outer = ttk.Frame(root, padding=10)
    outer.pack(fill="both", expand=True)

    list_frame = ttk.LabelFrame(outer, text="待转换文件", padding=8)
    list_frame.pack(fill="both", expand=True)

    listbox = tk.Listbox(list_frame, selectmode="extended", activestyle="none")
    scroll = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
    listbox.configure(yscrollcommand=scroll.set)
    listbox.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    def refresh() -> None:
        listbox.delete(0, "end")
        for f in files:
            listbox.insert("end", f)

    def add_files() -> None:
        picked = filedialog.askopenfilenames(
            title="选择音频文件",
            filetypes=[("音频文件", "*.flac *.wav *.aiff *.aif *.ape *.wv *.mp3 *.m4a *.aac "
                                  "*.ogg *.opus *.wma *.dsf *.dff *.tta *.tak"),
                       ("所有文件", "*.*")])
        for f in picked:
            if f not in files:
                files.append(f)
        refresh()

    def add_folder() -> None:
        picked = filedialog.askdirectory(title="选择文件夹")
        if not picked:
            return
        found = collect_inputs([Path(picked)])
        added = 0
        for f in found:
            s = str(f)
            if s not in files:
                files.append(s)
                added += 1
        refresh()
        log_line(f"从文件夹加入 {added} 个文件")

    def remove_selected() -> None:
        for idx in sorted(listbox.curselection(), reverse=True):
            del files[idx]
        refresh()

    def clear_all() -> None:
        files.clear()
        refresh()

    btn_row = ttk.Frame(outer)
    btn_row.pack(fill="x", pady=(6, 0))
    ttk.Button(btn_row, text="添加文件…", command=add_files).pack(side="left")
    ttk.Button(btn_row, text="添加文件夹…", command=add_folder).pack(side="left", padx=6)
    ttk.Button(btn_row, text="移除选中", command=remove_selected).pack(side="left")
    ttk.Button(btn_row, text="清空", command=clear_all).pack(side="left", padx=6)

    # ---------------- options ----------------
    opt = ttk.LabelFrame(outer, text="转换设置", padding=8)
    opt.pack(fill="x", pady=(10, 0))

    out_var = tk.StringVar(value="")
    target_var = tk.StringVar(value="50")
    keepname_var = tk.BooleanVar(value=False)
    forcealac_var = tk.BooleanVar(value=False)

    row1 = ttk.Frame(opt)
    row1.pack(fill="x")
    ttk.Label(row1, text="输出文件夹:").pack(side="left")
    ttk.Entry(row1, textvariable=out_var).pack(side="left", fill="x", expand=True, padx=6)
    ttk.Label(row1, text="留空 = 与源文件同目录").pack(side="left")

    def pick_out() -> None:
        picked = filedialog.askdirectory(title="选择输出文件夹")
        if picked:
            out_var.set(picked)

    ttk.Button(row1, text="浏览…", command=pick_out).pack(side="left", padx=(6, 0))

    row2 = ttk.Frame(opt)
    row2.pack(fill="x", pady=(6, 0))
    ttk.Label(row2, text="单曲上限 (MB):").pack(side="left")
    ttk.Spinbox(row2, from_=1, to=2000, increment=5, width=8,
                textvariable=target_var).pack(side="left", padx=(4, 16))
    ttk.Label(row2, text="超出时自动降级（先减小位深/采样率，最后才转 AAC）").pack(side="left")
    ttk.Checkbutton(row2, text="保留原文件名", variable=keepname_var).pack(side="right")
    ttk.Checkbutton(row2, text="强制 ALAC", variable=forcealac_var).pack(side="right", padx=8)

    # ---------------- actions ----------------
    act = ttk.Frame(outer)
    act.pack(fill="x", pady=(10, 0))
    start_btn = ttk.Button(act, text="开始转换")
    start_btn.pack(side="left")
    progress = ttk.Progressbar(act, mode="determinate")
    progress.pack(side="left", fill="x", expand=True, padx=10)
    status = ttk.Label(act, text="就绪")
    status.pack(side="left")

    log_frame = ttk.LabelFrame(outer, text="日志", padding=6)
    log_frame.pack(fill="both", expand=True, pady=(10, 0))
    log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
    log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    log_text.configure(yscrollcommand=log_scroll.set)
    log_text.pack(side="left", fill="both", expand=True)
    log_scroll.pack(side="right", fill="y")

    def log_line(text: str) -> None:
        log_text.configure(state="normal")
        log_text.insert("end", text + "\n")
        log_text.see("end")
        log_text.configure(state="disabled")

    # ---------------- worker ----------------
    def worker_main(targets: list[Path], opts: Options) -> None:
        for i, src in enumerate(targets, 1):
            if worker["cancel"]:
                msg_queue.put(("log", "已取消"))
                break
            msg_queue.put(("log", f"[{i}/{len(targets)}] {src.name}"))
            msg_queue.put(("status", f"{i}/{len(targets)}"))
            try:
                res = convert_file(src, opts, lambda m: msg_queue.put(("log", m)))
            except Exception:
                msg_queue.put(("log", "  ✗ 未预期错误:\n" + traceback.format_exc()))
                msg_queue.put(("done_one", False))
                continue
            if not res.ok:
                msg_queue.put(("log", f"  ✗ 失败: {res.error}"))
                msg_queue.put(("done_one", False))
            else:
                extra = f"   ⚠ {'; '.join(res.warnings)}" if res.warnings else ""
                msg_queue.put(("log", f"  ✓ {res.dest.name}  {fmt_size(res.size)}"
                                     f"  [{res.plan.label()}]"))
                msg_queue.put(("log", f"    歌词: {res.lyrics_from}   封面: {res.cover_from}{extra}"))
                msg_queue.put(("done_one", True))
            msg_queue.put(("tick", None))
        msg_queue.put(("finished", None))

    def pump() -> None:
        try:
            while True:
                kind, payload = msg_queue.get_nowait()
                if kind == "log":
                    log_line(str(payload))
                elif kind == "status":
                    status.configure(text=f"处理中 {payload}")
                elif kind == "tick":
                    progress.step(1)
                elif kind == "done_one":
                    pass
                elif kind == "finished":
                    start_btn.configure(state="normal")
                    status.configure(text="完成")
                    log_line("")
                    log_line("全部处理完成。导入 Apple Music 前请确认曲目未在资料库中重复。")
                    return
        except queue.Empty:
            pass
        root.after(120, pump)

    def start() -> None:
        if worker["thread"] and worker["thread"].is_alive():
            return
        if not files:
            messagebox.showinfo("提示", "请先添加要转换的文件。")
            return
        try:
            target = float(target_var.get())
        except ValueError:
            messagebox.showerror("错误", "单曲上限必须是数字。")
            return
        targets = [Path(f) for f in files]
        opts = Options(
            outdir=Path(out_var.get()).expanduser() if out_var.get().strip() else None,
            target_mb=target if target > 0 else 1e6,
            keep_name=keepname_var.get(),
            force_alac=forcealac_var.get(),
        )
        progress.configure(maximum=len(targets), value=0)
        start_btn.configure(state="disabled")
        log_line(f"开始处理 {len(targets)} 个文件，单曲上限 {target:g}MB")
        worker["thread"] = threading.Thread(target=worker_main, args=(targets, opts),
                                            daemon=True)
        worker["thread"].start()

    start_btn.configure(command=start)

    # environment banner
    try:
        log_line(f"ffmpeg : {ffmpeg()}")
        log_line(f"ffprobe: {ffprobe()}")
    except AMCError as exc:
        log_line(f"✗ {exc}")
        start_btn.configure(state="disabled")

    refresh()
    root.after(120, pump)
    root.mainloop()
    return 0
