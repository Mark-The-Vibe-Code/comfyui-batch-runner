"""
ComfyUI Universal Batch Runner
Load any workflow_api.json and batch process a folder of images.
Supports two modes:
  - Directory loaders (Inspire etc): increments start_index each run
  - LoadImage nodes: copies each file into ComfyUI input folder, sets image field
Self-installs dependencies on first run.
"""

import sys
import subprocess
import importlib

def ensure(package, import_name=None):
    """Only runs when executing as a plain .py script, not inside a PyInstaller exe."""
    if getattr(sys, "frozen", False):
        return  # running as .exe — dependencies are already bundled
    name = import_name or package
    try:
        importlib.import_module(name)
    except ImportError:
        print(f"Installing {package}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package, "-q"])

ensure("requests")
ensure("Pillow", "PIL")

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import json
import time
import uuid
import os
import shutil
import requests
from pathlib import Path
import queue as Queue

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}

# Node class_types that load images from a directory (expandable)
DIR_LOADER_CLASSES = {
    "LoadImagesFromDir //Inspire",
    "LoadImageListFromDir //Inspire",
    "LoadImagesFromDirectory",
    "VHS_LoadImages",
    "LoadImageBatch",
}

# Node class_types that load a single image
SINGLE_LOADER_CLASSES = {
    "LoadImage",
}

COLORS = {
    "bg":          "#0d0d0d",
    "panel":       "#151515",
    "panel2":      "#1a1a1a",
    "border":      "#272727",
    "accent":      "#00d4ff",
    "accent_dim":  "#006a80",
    "success":     "#00ff88",
    "warning":     "#ffaa00",
    "error":       "#ff4444",
    "text":        "#dedede",
    "text_dim":    "#606060",
    "text_muted":  "#383838",
    "btn_bg":      "#1c1c1c",
    "btn_hover":   "#252525",
    "progress_bg": "#181818",
}

FONTS = {
    "title":   ("Consolas", 16, "bold"),
    "label":   ("Consolas", 9),
    "mono":    ("Consolas", 9),
    "small":   ("Consolas", 8),
    "counter": ("Consolas", 20, "bold"),
    "status":  ("Consolas", 10, "bold"),
    "heading": ("Consolas", 9, "bold"),
}


# ── Workflow analysis ────────────────────────────────────────────────────────

def analyse_workflow(workflow: dict):
    """
    Returns a list of candidate driver nodes.

    Two modes are detected:
      mode="index"  — directory loaders with a start_index field (Inspire pack etc)
                      Worker increments start_index each run.
      mode="file"   — LoadImage nodes with an "image" filename field
                      Worker copies each file into ComfyUI input folder and sets "image".

    Each entry: {
        "node_id", "class_type", "label",
        "mode": "index" | "file",
        # index mode only:
        "field", "current_value", "dir_field",
        # file mode only: (nothing extra — worker uses "image" field by convention)
    }
    """
    candidates = []
    for node_id, node in workflow.items():
        ct     = node.get("class_type", "")
        inputs = node.get("inputs", {})
        meta   = node.get("_meta", {}).get("title", ct)

        # ── Directory loaders → index mode ──────────────────────────────────
        if ct in DIR_LOADER_CLASSES:
            if "start_index" in inputs:
                candidates.append({
                    "node_id":       node_id,
                    "class_type":    ct,
                    "mode":          "index",
                    "field":         "start_index",
                    "label":         f"Node {node_id} | {meta}  [index mode]",
                    "current_value": inputs.get("start_index", 0),
                    "dir_field":     _find_dir_field(inputs),
                })

        # ── LoadImage → file mode ────────────────────────────────────────────
        elif ct in SINGLE_LOADER_CLASSES:
            if "image" in inputs and isinstance(inputs["image"], str):
                candidates.append({
                    "node_id":    node_id,
                    "class_type": ct,
                    "mode":       "file",
                    "field":      "image",
                    "label":      f"Node {node_id} | {meta}  [file mode — copies to ComfyUI/input]",
                    "dir_field":  None,
                })
            else:
                # fall back: look for other index-like fields
                for field in ("index", "image_index", "frame_index"):
                    if field in inputs:
                        candidates.append({
                            "node_id":       node_id,
                            "class_type":    ct,
                            "mode":          "index",
                            "field":         field,
                            "label":         f"Node {node_id} | {meta} → {field}  [index mode]",
                            "current_value": inputs.get(field, 0),
                            "dir_field":     None,
                        })

        # ── Generic fallback — any node with a numeric index field ───────────
        else:
            for field in ("start_index", "image_index", "index", "frame_index", "batch_index"):
                if field in inputs and isinstance(inputs[field], int):
                    candidates.append({
                        "node_id":       node_id,
                        "class_type":    ct,
                        "mode":          "index",
                        "field":         field,
                        "label":         f"Node {node_id} | {meta} → {field}  [index mode]",
                        "current_value": inputs.get(field, 0),
                        "dir_field":     None,
                    })

    # Deduplicate by (node_id, field/mode)
    seen, unique = set(), []
    for c in candidates:
        key = (c["node_id"], c.get("field", c["mode"]))
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def _find_dir_field(inputs):
    for f in ("directory", "folder", "input_dir", "path"):
        if f in inputs and isinstance(inputs[f], str):
            return f
    return None


def get_workflow_summary(workflow: dict):
    """Return a human-readable summary of what the workflow does."""
    classes = [n.get("class_type", "") for n in workflow.values()]
    notes = []
    if any("Upscale" in c for c in classes):
        notes.append("upscaling")
    if any("Remove" in c or "Segment" in c or "Mask" in c for c in classes):
        notes.append("background removal / masking")
    if any("ControlNet" in c for c in classes):
        notes.append("ControlNet")
    if any("IPAdapter" in c for c in classes):
        notes.append("IP-Adapter")
    if any("Checkpoint" in c for c in classes):
        notes.append("Stable Diffusion")
    if not notes:
        notes.append("custom workflow")
    return ", ".join(notes)


# ── Worker thread ────────────────────────────────────────────────────────────

class BatchWorker(threading.Thread):
    def __init__(self, config, log_q, prog_q, done_cb):
        super().__init__(daemon=True)
        self.config    = config
        self.log_q     = log_q
        self.prog_q    = prog_q
        self.done_cb   = done_cb
        self._stop     = threading.Event()
        self.client_id = str(uuid.uuid4())

    def stop(self):
        self._stop.set()

    def log(self, msg, level="info"):
        self.log_q.put((level, msg))

    def prog(self, cur, tot, status=""):
        self.prog_q.put((cur, tot, status))

    def queue_prompt(self, workflow):
        data = json.dumps({"prompt": workflow, "client_id": self.client_id}).encode()
        r = requests.post(
            f"{self.config['url']}/prompt", data=data,
            headers={"Content-Type": "application/json"}, timeout=15
        )
        r.raise_for_status()
        return r.json()

    def wait_for_completion(self, prompt_id):
        while not self._stop.is_set():
            time.sleep(2)
            try:
                r = requests.get(f"{self.config['url']}/history/{prompt_id}", timeout=10)
                if r.status_code == 200:
                    history = r.json()
                    if prompt_id in history:
                        status = history[prompt_id].get("status", {})
                        if status.get("status_str") == "error":
                            msgs = status.get("messages", [])
                            raise Exception(f"ComfyUI error: {msgs}")
                        return True
            except requests.RequestException as e:
                self.log(f"  connection hiccup: {e}", "warning")
                time.sleep(3)
        return False

    def count_images(self, folder):
        p = Path(folder)
        if not p.exists():
            return 0
        return len([f for f in p.iterdir() if f.suffix.lower() in SUPPORTED_EXTS])

    def find_comfy_input_folder(self, url):
        """Locate the ComfyUI input folder by checking common paths."""
        candidates = [
            Path(r"C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\input"),
            Path(__file__).parent / "ComfyUI" / "input",
            Path(__file__).parent / "input",
        ]
        for c in candidates:
            if c.exists():
                return c
        # Ask ComfyUI itself via the settings endpoint
        try:
            r = requests.get(f"{url}/settings", timeout=5)
            if r.status_code == 200:
                data = r.json()
                base = data.get("base_path") or data.get("comfyui_base_path")
                if base:
                    p = Path(base) / "input"
                    if p.exists():
                        return p
        except:
            pass
        return None

    def get_sorted_images(self, folder):
        p = Path(folder)
        return sorted([f for f in p.iterdir() if f.suffix.lower() in SUPPORTED_EXTS])

    def run(self):
        cfg               = self.config
        url               = cfg["url"]
        workflow_template = cfg["workflow"]
        driver_nodes      = cfg["driver_nodes"]
        input_dir         = cfg.get("input_dir", "")
        dir_field         = cfg.get("dir_field")
        skip              = cfg.get("skip", 0)

        # Determine mode from driver nodes
        # If any driver is file mode, we use file mode for the whole batch
        has_file_mode  = any(dn.get("mode") == "file"  for dn in driver_nodes)
        has_index_mode = any(dn.get("mode") == "index" for dn in driver_nodes)

        # Test connection
        try:
            requests.get(f"{url}/system_stats", timeout=5).raise_for_status()
            self.log("✓ ComfyUI connected", "success")
        except Exception as e:
            self.log(f"✗ Cannot connect to ComfyUI at {url}", "error")
            self.done_cb(False, 0, 0)
            return

        # For file mode, find ComfyUI input folder
        comfy_input = None
        if has_file_mode:
            comfy_input = self.find_comfy_input_folder(url)
            if not comfy_input:
                self.log("✗ Could not find ComfyUI input folder.", "error")
                self.log("  Expected: C:\\ComfyUI\\ComfyUI_windows_portable\\ComfyUI\\input", "error")
                self.done_cb(False, 0, 0)
                return
            self.log(f"✓ ComfyUI input folder: {comfy_input}", "info")

        # Get image list
        if not input_dir:
            self.log("✗ No input folder specified", "error")
            self.done_cb(False, 0, 0)
            return

        if has_file_mode:
            all_images = self.get_sorted_images(input_dir)
            total = len(all_images)
        else:
            total = self.count_images(input_dir)
            all_images = None

        if total == 0:
            self.log(f"✗ No images found in: {input_dir}", "error")
            self.done_cb(False, 0, 0)
            return

        actual = total - skip
        if actual <= 0:
            self.log(f"✗ Skip ({skip}) >= total images ({total})", "error")
            self.done_cb(False, 0, 0)
            return

        mode_label = "file mode (LoadImage)" if has_file_mode else "index mode"
        self.log(f"✓ {total} images  |  processing: {actual}  |  {mode_label}", "success")
        if skip:
            self.log(f"  resuming from position {skip}", "info")
        self.log("─" * 52, "dim")

        failed    = []
        completed = 0

        for i in range(actual):
            if self._stop.is_set():
                self.log(f"\n⏹ Stopped. Completed {completed}/{actual}", "warning")
                break

            pos = skip + i
            self.prog(i, actual, f"{i+1}/{actual}")

            try:
                wf = json.loads(json.dumps(workflow_template))

                if has_file_mode:
                    # Copy file to ComfyUI input and set filename on LoadImage nodes
                    img_path = all_images[pos]
                    dest     = comfy_input / img_path.name
                    shutil.copy2(img_path, dest)
                    self.log(f"[{i+1}/{actual}]  {img_path.name}", "info")

                    for dn in driver_nodes:
                        if dn.get("mode") == "file":
                            wf[dn["node_id"]]["inputs"]["image"] = img_path.name

                    # Also handle any index-mode drivers in the same workflow
                    for dn in driver_nodes:
                        if dn.get("mode") == "index":
                            wf[dn["node_id"]]["inputs"][dn["field"]] = pos

                else:
                    # Pure index mode
                    self.log(f"[{i+1}/{actual}]  index {pos}", "info")
                    for dn in driver_nodes:
                        wf[dn["node_id"]]["inputs"][dn["field"]] = pos

                    # Override directory field if requested
                    if input_dir and dir_field:
                        for dn in driver_nodes:
                            if dir_field in wf[dn["node_id"]]["inputs"]:
                                wf[dn["node_id"]]["inputs"][dir_field] = input_dir

                result    = self.queue_prompt(wf)
                prompt_id = result.get("prompt_id")
                if not prompt_id:
                    raise Exception("No prompt_id returned")

                ok = self.wait_for_completion(prompt_id)
                if ok:
                    completed += 1
                    self.log("  ✓ done", "success")
                else:
                    self.log("  ⏹ stopped", "warning")
                    break

            except KeyboardInterrupt:
                break
            except Exception as e:
                self.log(f"  ✗ FAILED: {e}", "error")
                failed.append(pos)

        self.prog(actual, actual, "")
        self.log("─" * 52, "dim")
        self.log(f"✓ Finished: {completed}/{actual} succeeded", "success")
        if failed:
            self.log(f"✗ Failed at positions: {failed}", "error")
        self.done_cb(True, completed, actual)


# ── Main App ─────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ComfyUI Batch Runner")
        self.configure(bg=COLORS["bg"])
        self.resizable(True, True)
        self.minsize(860, 700)

        self.worker     = None
        self.log_q      = Queue.Queue()
        self.prog_q     = Queue.Queue()
        self._running   = False
        self._workflow  = None        # loaded workflow dict
        self._candidates = []         # detected index-driver nodes
        self._driver_vars = []        # BooleanVar per candidate

        self._build_ui()
        self._poll()
        self.after(300, self._ping_silent)

        # Allow dragging workflow JSON onto window
        try:
            self.drop_target_register = lambda *a: None
            self.dnd_bind = lambda *a: None
        except:
            pass

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        outer = tk.Frame(self, bg=COLORS["bg"], padx=14, pady=14)
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        self._header(outer)
        self._section_workflow(outer)
        self._section_settings(outer)
        self._section_log(outer)
        self._footer(outer)

    def _header(self, p):
        f = tk.Frame(p, bg=COLORS["bg"])
        f.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        f.columnconfigure(1, weight=1)

        tb = tk.Frame(f, bg=COLORS["bg"])
        tb.grid(row=0, column=0, sticky="w")
        tk.Label(tb, text="BATCH RUNNER", font=("Consolas", 17, "bold"),
                 fg=COLORS["accent"], bg=COLORS["bg"]).pack(side="left")
        tk.Label(tb, text=" // ComfyUI Universal", font=("Consolas", 10),
                 fg=COLORS["text_dim"], bg=COLORS["bg"]).pack(side="left", pady=(5, 0))

        # Connection pill
        pill = tk.Frame(f, bg=COLORS["panel"],
                         highlightbackground=COLORS["border"], highlightthickness=1)
        pill.grid(row=0, column=1, sticky="e")
        tk.Label(pill, text="COMFYUI", font=FONTS["small"],
                 fg=COLORS["text_dim"], bg=COLORS["panel"]).pack(side="left", padx=(8, 2), pady=5)
        self.conn_dot = tk.Label(pill, text="●", font=("Consolas", 13),
                                  fg=COLORS["text_muted"], bg=COLORS["panel"])
        self.conn_dot.pack(side="left")
        self.conn_lbl = tk.Label(pill, text="CHECKING", font=FONTS["small"],
                                  fg=COLORS["text_dim"], bg=COLORS["panel"])
        self.conn_lbl.pack(side="left", padx=(3, 0))

        self.url_entry = tk.Entry(pill, font=FONTS["mono"], fg=COLORS["text"],
                                   bg=COLORS["btn_bg"], insertbackground=COLORS["accent"],
                                   relief="flat", width=22,
                                   highlightbackground=COLORS["border"], highlightthickness=1, bd=0)
        self.url_entry.insert(0, "http://127.0.0.1:8188")
        self.url_entry.pack(side="left", padx=(6, 2), pady=5)

        tk.Button(pill, text="PING", font=FONTS["small"],
                  fg=COLORS["accent"], bg=COLORS["panel"],
                  activeforeground=COLORS["accent"], activebackground=COLORS["btn_hover"],
                  relief="flat", bd=0, cursor="hand2", padx=6,
                  command=self._ping).pack(side="left", padx=(0, 8))

    def _section_workflow(self, p):
        pnl = self._panel(p, row=1, title="WORKFLOW")
        pnl.columnconfigure(1, weight=1)

        # JSON file picker
        tk.Label(pnl, text="Workflow JSON", font=FONTS["label"],
                 fg=COLORS["text_dim"], bg=COLORS["panel"], width=14, anchor="w"
                 ).grid(row=0, column=0, sticky="w", padx=(10, 4), pady=(8, 4))

        row0 = tk.Frame(pnl, bg=COLORS["panel"])
        row0.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(0, 10), pady=(8, 4))
        row0.columnconfigure(0, weight=1)

        self.wf_path_entry = self._entry(row0, "(no workflow loaded)")
        self.wf_path_entry.config(fg=COLORS["text_dim"], state="readonly")
        self.wf_path_entry.grid(row=0, column=0, sticky="ew")
        self._icon_btn(row0, "Browse…", self._load_workflow).grid(row=0, column=1, padx=(4, 0))

        # Workflow summary
        self.wf_summary = tk.Label(pnl, text="", font=FONTS["small"],
                                    fg=COLORS["text_dim"], bg=COLORS["panel"], anchor="w")
        self.wf_summary.grid(row=1, column=1, columnspan=3, sticky="w", padx=(0, 10))

        # Index driver node selector
        tk.Label(pnl, text="Index Driver", font=FONTS["label"],
                 fg=COLORS["text_dim"], bg=COLORS["panel"], width=14, anchor="nw"
                 ).grid(row=2, column=0, sticky="nw", padx=(10, 4), pady=(6, 8))

        self.driver_frame = tk.Frame(pnl, bg=COLORS["panel"])
        self.driver_frame.grid(row=2, column=1, columnspan=3, sticky="ew",
                                padx=(0, 10), pady=(6, 8))

        self.driver_hint = tk.Label(self.driver_frame,
                                     text="Load a workflow JSON to see detected index nodes",
                                     font=FONTS["small"], fg=COLORS["text_muted"],
                                     bg=COLORS["panel"])
        self.driver_hint.pack(anchor="w")

    def _section_settings(self, p):
        pnl = self._panel(p, row=2, title="BATCH SETTINGS")
        pnl.columnconfigure(1, weight=1)
        pnl.columnconfigure(3, weight=1)

        # Input folder
        tk.Label(pnl, text="Input Folder", font=FONTS["label"],
                 fg=COLORS["text_dim"], bg=COLORS["panel"], width=14, anchor="w"
                 ).grid(row=0, column=0, sticky="w", padx=(10, 4), pady=(8, 4))

        row0 = tk.Frame(pnl, bg=COLORS["panel"])
        row0.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(0, 10), pady=(8, 4))
        row0.columnconfigure(0, weight=1)

        self.input_entry = self._entry(row0, r"F:\Converted\systems\TACTICAL.DLL\303")
        self.input_entry.grid(row=0, column=0, sticky="ew")
        self._icon_btn(row0, "Browse…", self._browse_input).grid(row=0, column=1, padx=(4, 0))

        self.img_count_lbl = tk.Label(pnl, text="", font=FONTS["small"],
                                       fg=COLORS["accent"], bg=COLORS["panel"])
        self.img_count_lbl.grid(row=1, column=1, sticky="w", padx=(0, 0))

        self.input_entry.bind("<FocusOut>", lambda e: self._update_count())
        self.input_entry.bind("<Return>",   lambda e: self._update_count())

        # Override directory in workflow checkbox
        self.override_dir_var = tk.BooleanVar(value=True)
        tk.Checkbutton(pnl, text="Override directory field in workflow nodes",
                       variable=self.override_dir_var,
                       font=FONTS["small"], fg=COLORS["text_dim"], bg=COLORS["panel"],
                       selectcolor=COLORS["bg"], activebackground=COLORS["panel"],
                       activeforeground=COLORS["text"], relief="flat"
                       ).grid(row=2, column=1, columnspan=3, sticky="w", padx=(0, 10), pady=(0, 4))

        # Skip + output prefix
        tk.Label(pnl, text="Skip First N", font=FONTS["label"],
                 fg=COLORS["text_dim"], bg=COLORS["panel"], width=14, anchor="w"
                 ).grid(row=3, column=0, sticky="w", padx=(10, 4), pady=4)

        skip_row = tk.Frame(pnl, bg=COLORS["panel"])
        skip_row.grid(row=3, column=1, sticky="w", pady=4)
        self.skip_var = tk.IntVar(value=0)
        tk.Spinbox(skip_row, from_=0, to=99999, textvariable=self.skip_var,
                   width=7, font=FONTS["mono"], fg=COLORS["text"], bg=COLORS["bg"],
                   buttonbackground=COLORS["btn_bg"], relief="flat",
                   highlightbackground=COLORS["border"], highlightthickness=1
                   ).pack(side="left")
        tk.Label(skip_row, text="  (resume after interruption)",
                 font=FONTS["small"], fg=COLORS["text_muted"], bg=COLORS["panel"]
                 ).pack(side="left")

        # Delay between jobs
        tk.Label(pnl, text="Delay (sec)", font=FONTS["label"],
                 fg=COLORS["text_dim"], bg=COLORS["panel"], width=14, anchor="w"
                 ).grid(row=3, column=2, sticky="w", padx=(16, 4), pady=4)

        delay_row = tk.Frame(pnl, bg=COLORS["panel"])
        delay_row.grid(row=3, column=3, sticky="w", padx=(0, 10), pady=4)
        self.delay_var = tk.DoubleVar(value=0.5)
        tk.Spinbox(delay_row, from_=0, to=30, increment=0.5, textvariable=self.delay_var,
                   width=5, font=FONTS["mono"], fg=COLORS["text"], bg=COLORS["bg"],
                   buttonbackground=COLORS["btn_bg"], relief="flat",
                   highlightbackground=COLORS["border"], highlightthickness=1
                   ).pack(side="left")
        tk.Label(delay_row, text="  between images",
                 font=FONTS["small"], fg=COLORS["text_muted"], bg=COLORS["panel"]
                 ).pack(side="left")

    def _section_log(self, p):
        pnl = self._panel(p, row=3, title="LOG", expand=True)
        pnl.rowconfigure(0, weight=1)
        pnl.columnconfigure(0, weight=1)

        self.log_box = tk.Text(pnl, font=FONTS["mono"], fg=COLORS["text"],
                                bg=COLORS["bg"], relief="flat",
                                highlightbackground=COLORS["border"], highlightthickness=1,
                                state="disabled", bd=0, wrap="word")
        self.log_box.grid(row=0, column=0, sticky="nsew", padx=10, pady=(6, 8))

        sb = tk.Scrollbar(pnl, command=self.log_box.yview,
                           bg=COLORS["panel"], troughcolor=COLORS["bg"],
                           relief="flat", bd=0, width=8)
        sb.grid(row=0, column=1, sticky="ns", pady=(6, 8), padx=(0, 4))
        self.log_box.config(yscrollcommand=sb.set)

        for tag, color in [("success", COLORS["success"]), ("error", COLORS["error"]),
                            ("warning", COLORS["warning"]), ("info", COLORS["text"]),
                            ("dim", COLORS["text_muted"])]:
            self.log_box.tag_config(tag, foreground=color)

        # Clear button
        tk.Button(pnl, text="clear log", font=FONTS["small"],
                  fg=COLORS["text_muted"], bg=COLORS["panel"],
                  activeforeground=COLORS["text_dim"], activebackground=COLORS["panel"],
                  relief="flat", bd=0, cursor="hand2",
                  command=self._clear_log).grid(row=1, column=0, sticky="e", padx=10, pady=(0, 4))

    def _footer(self, p):
        foot = tk.Frame(p, bg=COLORS["bg"])
        foot.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        foot.columnconfigure(2, weight=1)

        # Counter
        cf = tk.Frame(foot, bg=COLORS["panel"],
                       highlightbackground=COLORS["border"], highlightthickness=1)
        cf.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.counter_cur = tk.Label(cf, text="0", font=FONTS["counter"],
                                     fg=COLORS["accent"], bg=COLORS["panel"], width=4, anchor="e")
        self.counter_cur.pack(side="left", padx=(10, 0), pady=4)
        tk.Label(cf, text=" / ", font=FONTS["counter"],
                 fg=COLORS["text_muted"], bg=COLORS["panel"]).pack(side="left")
        self.counter_tot = tk.Label(cf, text="0", font=FONTS["counter"],
                                     fg=COLORS["text_dim"], bg=COLORS["panel"], width=4)
        self.counter_tot.pack(side="left")
        tk.Label(cf, text=" images  ", font=FONTS["label"],
                 fg=COLORS["text_muted"], bg=COLORS["panel"]).pack(side="left")

        # Progress
        pf = tk.Frame(foot, bg=COLORS["panel"],
                       highlightbackground=COLORS["border"], highlightthickness=1)
        pf.grid(row=0, column=2, sticky="ew", padx=(0, 10))
        pf.columnconfigure(0, weight=1)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Horizontal.TProgressbar",
                         background=COLORS["accent"], troughcolor=COLORS["progress_bg"],
                         bordercolor=COLORS["panel"], lightcolor=COLORS["accent"],
                         darkcolor=COLORS["accent"])

        self.progress_bar = ttk.Progressbar(pf, mode="determinate")
        self.progress_bar.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        self.progress_lbl = tk.Label(pf, text="READY", font=FONTS["status"],
                                      fg=COLORS["text_dim"], bg=COLORS["panel"])
        self.progress_lbl.grid(row=0, column=1, padx=(0, 10))

        # Buttons
        bf = tk.Frame(foot, bg=COLORS["bg"])
        bf.grid(row=0, column=3, sticky="e")

        self._action_btn(bf, "OPEN OUTPUT", self._open_output, COLORS["text_dim"]).pack(side="left", padx=(0, 6))
        self.stop_btn = self._action_btn(bf, "■  STOP", self._stop, COLORS["error"], state="disabled")
        self.stop_btn.pack(side="left", padx=(0, 6))
        self.start_btn = self._action_btn(bf, "▶  START", self._start, COLORS["success"])
        self.start_btn.pack(side="left")

    # ── Widget helpers ───────────────────────────────────────────────────────

    def _panel(self, parent, row, title, expand=False):
        outer = tk.Frame(parent, bg=COLORS["bg"])
        outer.grid(row=row, column=0, sticky="nsew" if expand else "ew", pady=(0, 10))
        if expand:
            parent.rowconfigure(row, weight=1)
            outer.rowconfigure(1, weight=1)
        outer.columnconfigure(0, weight=1)
        tk.Label(outer, text=title, font=FONTS["small"],
                 fg=COLORS["accent_dim"], bg=COLORS["bg"]).grid(row=0, column=0, sticky="w", pady=(0, 3))
        pnl = tk.Frame(outer, bg=COLORS["panel"],
                        highlightbackground=COLORS["border"], highlightthickness=1)
        pnl.grid(row=1, column=0, sticky="nsew" if expand else "ew")
        pnl.columnconfigure(0, weight=1)
        return pnl

    def _entry(self, parent, default=""):
        e = tk.Entry(parent, font=FONTS["mono"], fg=COLORS["text"], bg=COLORS["bg"],
                     insertbackground=COLORS["accent"], relief="flat",
                     highlightbackground=COLORS["border"], highlightthickness=1, bd=0)
        e.insert(0, default)
        return e

    def _icon_btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, font=FONTS["mono"],
                         fg=COLORS["text_dim"], bg=COLORS["btn_bg"],
                         activeforeground=COLORS["accent"], activebackground=COLORS["btn_hover"],
                         relief="flat", bd=0, padx=8, pady=2, cursor="hand2", command=cmd,
                         highlightbackground=COLORS["border"], highlightthickness=1)

    def _action_btn(self, parent, text, cmd, color, state="normal"):
        return tk.Button(parent, text=text, font=("Consolas", 10, "bold"),
                         fg=color, bg=COLORS["btn_bg"],
                         activeforeground=color, activebackground=COLORS["btn_hover"],
                         relief="flat", bd=0, padx=16, pady=8, cursor="hand2",
                         command=cmd, state=state,
                         highlightbackground=COLORS["border"], highlightthickness=1)

    # ── Workflow loading ─────────────────────────────────────────────────────

    def _load_workflow(self):
        path = filedialog.askopenfilename(
            title="Select workflow_api.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                wf = json.load(f)
            self._workflow = wf
            self._update_workflow_ui(path, wf)
        except Exception as e:
            messagebox.showerror("Error", f"Could not load workflow:\n{e}")

    def _update_workflow_ui(self, path, wf):
        # Path display
        self.wf_path_entry.config(state="normal")
        self.wf_path_entry.delete(0, "end")
        self.wf_path_entry.insert(0, path)
        self.wf_path_entry.config(state="readonly", fg=COLORS["text"])

        # Summary
        summary = get_workflow_summary(wf)
        node_count = len(wf)
        self.wf_summary.config(
            text=f"  {node_count} nodes  |  detected: {summary}",
            fg=COLORS["text_dim"]
        )

        # Detect driver nodes
        self._candidates = analyse_workflow(wf)
        self._rebuild_driver_ui()

        # Auto-populate input dir if found in workflow
        for c in self._candidates:
            if c.get("dir_field"):
                node = wf[c["node_id"]]
                dir_val = node["inputs"].get(c["dir_field"], "")
                if dir_val and Path(dir_val).exists():
                    self.input_entry.delete(0, "end")
                    self.input_entry.insert(0, dir_val)
                    self._update_count()
                    break

        self._log(f"✓ Loaded: {Path(path).name}  ({node_count} nodes, {summary})", "success")
        if self._candidates:
            self._log(f"  Found {len(self._candidates)} index driver node(s)", "info")
        else:
            self._log("  ⚠ No index driver nodes detected — check node selection", "warning")

    def _rebuild_driver_ui(self):
        for w in self.driver_frame.winfo_children():
            w.destroy()
        self._driver_vars = []

        if not self._candidates:
            tk.Label(self.driver_frame,
                     text="No index-driver nodes found in this workflow.\n"
                          "Manually inspect the JSON for the node with start_index or image_index.",
                     font=FONTS["small"], fg=COLORS["error"], bg=COLORS["panel"],
                     justify="left").pack(anchor="w")
            return

        tk.Label(self.driver_frame,
                 text="Select which nodes to increment (usually select all):",
                 font=FONTS["small"], fg=COLORS["text_dim"], bg=COLORS["panel"]
                 ).pack(anchor="w", pady=(0, 3))

        for c in self._candidates:
            var = tk.BooleanVar(value=True)
            self._driver_vars.append((var, c))
            cb = tk.Checkbutton(self.driver_frame, text=c["label"],
                                 variable=var, font=FONTS["mono"],
                                 fg=COLORS["text"], bg=COLORS["panel"],
                                 selectcolor=COLORS["bg"],
                                 activebackground=COLORS["panel"],
                                 activeforeground=COLORS["text"],
                                 relief="flat")
            cb.pack(anchor="w")

    # ── Actions ──────────────────────────────────────────────────────────────

    def _browse_input(self):
        d = filedialog.askdirectory(title="Select Input Image Folder")
        if d:
            self.input_entry.delete(0, "end")
            self.input_entry.insert(0, d)
            self._update_count()

    def _update_count(self):
        d = self.input_entry.get().strip()
        p = Path(d)
        if p.exists():
            n = len([f for f in p.iterdir() if f.suffix.lower() in SUPPORTED_EXTS])
            self.img_count_lbl.config(text=f"  {n} images found")
            self.counter_tot.config(text=str(n))
        else:
            self.img_count_lbl.config(text="  folder not found", fg=COLORS["error"])

    def _open_output(self):
        candidates = [
            Path(r"C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\output"),
            Path(__file__).parent / "ComfyUI" / "output",
            Path(__file__).parent / "output",
        ]
        for c in candidates:
            if c.exists():
                os.startfile(str(c))
                return
        # Fall back to asking
        d = filedialog.askdirectory(title="Select ComfyUI Output Folder")
        if d:
            os.startfile(d)

    def _ping(self):
        self.conn_dot.config(fg=COLORS["warning"])
        self.conn_lbl.config(text="CHECKING")
        self.after(50, self._ping_silent)

    def _ping_silent(self):
        url = self.url_entry.get().strip()
        try:
            requests.get(f"{url}/system_stats", timeout=3).raise_for_status()
            self.conn_dot.config(fg=COLORS["success"])
            self.conn_lbl.config(text="ONLINE ")
        except:
            self.conn_dot.config(fg=COLORS["error"])
            self.conn_lbl.config(text="OFFLINE")

    def _start(self):
        if self._running:
            return

        if not self._workflow:
            messagebox.showwarning("No Workflow", "Please load a workflow_api.json first.")
            return

        selected_drivers = [c for var, c in self._driver_vars if var.get()]
        if not selected_drivers:
            messagebox.showwarning("No Driver", "Please select at least one index driver node.")
            return

        input_dir = self.input_entry.get().strip()

        # Find dir_field from selected drivers
        dir_field = None
        for c in selected_drivers:
            if c.get("dir_field"):
                dir_field = c["dir_field"]
                break

        config = {
            "url":          self.url_entry.get().strip(),
            "workflow":     self._workflow,
            "driver_nodes": selected_drivers,
            "input_dir":    input_dir,
            "dir_field":    dir_field if self.override_dir_var.get() else None,
            "skip":         self.skip_var.get(),
            "manual_count": 1,
        }

        self._log("─" * 52, "dim")
        self._log("▶ Starting batch...", "success")
        self._running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.progress_lbl.config(text="RUNNING", fg=COLORS["accent"])

        self.worker = BatchWorker(config, self.log_q, self.prog_q, self._on_done)
        self.worker.start()

    def _stop(self):
        if self.worker:
            self.worker.stop()
            self.progress_lbl.config(text="STOPPING", fg=COLORS["warning"])
            self.stop_btn.config(state="disabled")

    def _on_done(self, success, completed, total):
        self._running = False
        self.after(0, lambda: self.start_btn.config(state="normal"))
        self.after(0, lambda: self.stop_btn.config(state="disabled"))
        status = "DONE" if success else "STOPPED"
        color  = COLORS["success"] if success else COLORS["warning"]
        self.after(0, lambda: self.progress_lbl.config(text=status, fg=color))

    def _clear_log(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")

    # ── Log / poll ───────────────────────────────────────────────────────────

    def _log(self, msg, level="info"):
        self.log_box.config(state="normal")
        self.log_box.insert("end", msg + "\n", level)
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _poll(self):
        try:
            while True:
                level, msg = self.log_q.get_nowait()
                self._log(msg, level)
        except Queue.Empty:
            pass
        try:
            while True:
                cur, tot, _ = self.prog_q.get_nowait()
                if tot > 0:
                    self.progress_bar["value"] = int(cur / tot * 100)
                    self.counter_cur.config(text=str(cur))
                    self.counter_tot.config(text=str(tot))
        except Queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    app = App()
    app.mainloop()
