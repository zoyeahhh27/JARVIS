#!/usr/bin/env python3
"""
JARVIS Desktop UI for macOS - futuristic control panel for jarvis.py.

How it is wired to the real engine
----------------------------------
* The UI starts jarvis.py as a child process with JARVIS_EVENTS=1.
* In that mode the engine prints structured lines ("@@JARVIS@@{json}") describing its REAL state:
  ready / wake / state (STANDBY, LISTENING, THINKING, SPEAKING) / heard / say / level / route.
* The UI only reads those lines. It never opens the microphone itself.
* STANDBY and LISTENING waveforms use the real microphone level reported by the engine.
  While SPEAKING, the waveform is a decorative animation (macOS `say` output level is not measured);
  the SPEAKING state itself is real.
* Nothing blocks Tk's main thread: a reader thread, a telemetry thread and a stop thread
  talk to the UI through a queue that Tk polls with after().

Only the standard library is used (Tkinter needs a Python build with Tk; see the install notes).
"""
import collections
import json
import math
import os
import pathlib
import platform
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import messagebox

BASE_DIR = pathlib.Path(__file__).resolve().parent
JARVIS_SCRIPT = BASE_DIR / "jarvis.py"
EVENT_PREFIX = "@@JARVIS@@"

BG = "#050b16"
PANEL = "#0a1526"
PANEL2 = "#0d1e32"
EDGE = "#17334d"
CYAN = "#42e8ff"
BLUE = "#2c8fff"
TEXT = "#d8f3ff"
MUTED = "#6b91ad"
GREEN = "#68ffbd"
RED = "#ff5478"
AMBER = "#ffc857"
FONT = "Menlo"

STATE_STYLE = {
    "OFFLINE": {"color": "#35566f", "speed": 0.008, "amp": 0.0, "text": "OFFLINE"},
    "INITIALIZING": {"color": BLUE, "speed": 0.05, "amp": 0.4, "text": "INITIALIZING"},
    "STANDBY": {"color": "#2aa7c4", "speed": 0.02, "amp": 0.35, "text": "STANDBY"},
    "LISTENING": {"color": CYAN, "speed": 0.05, "amp": 0.6, "text": "LISTENING"},
    "THINKING": {"color": "#6f9bff", "speed": 0.09, "amp": 0.5, "text": "THINKING"},
    "SPEAKING": {"color": "#aef7ff", "speed": 0.07, "amp": 0.9, "text": "SPEAKING"},
}
ENGINE_STATES = ("STANDBY", "LISTENING", "THINKING", "SPEAKING")
NORMAL_EXIT_CODES = {0, -signal.SIGINT, -signal.SIGTERM, 130, 143}


# ---------------------------------------------------------------------------
# Pure helpers (no Tk) - unit-tested in test_jarvis_routing.py
# ---------------------------------------------------------------------------
def mix(color_a, color_b, t):
    """Blend two #rrggbb colours; t=0 gives a, t=1 gives b. Used to fake glow without alpha."""
    t = max(0.0, min(1.0, t))
    a = [int(color_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(color_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def parse_engine_line(line):
    """('event', dict) for engine event lines, ('text', str) for anything else, None for blank."""
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    if line.startswith(EVENT_PREFIX):
        try:
            data = json.loads(line[len(EVENT_PREFIX):])
            if isinstance(data, dict) and "event" in data:
                return ("event", data)
        except ValueError:
            pass
        return ("text", line)
    return ("text", line)


def parse_vm_stat(vm_stat_output, total_bytes):
    """Used-memory percentage from macOS `vm_stat` output, or None."""
    try:
        page_size = int(re.search(r"page size of (\d+) bytes", vm_stat_output).group(1))
        pages = {name.strip(): int(value.strip().rstrip("."))
                 for name, value in re.findall(r"^(Pages [^:]+):\s+(\d+\.?)\s*$", vm_stat_output, re.M)}
        used = (pages.get("Pages active", 0) + pages.get("Pages wired down", 0)
                + pages.get("Pages occupied by compressor", 0)) * page_size
        return max(0.0, min(100.0, used * 100.0 / total_bytes)) if total_bytes else None
    except (AttributeError, ValueError):
        return None


def parse_battery(pmset_output):
    """(percent, charging_text) from `pmset -g batt`, or None."""
    match = re.search(r"(\d+)%", pmset_output)
    if not match:
        return None
    lowered = pmset_output.lower()
    if "discharging" in lowered:
        state = "on battery"
    elif "charging" in lowered or "ac power" in lowered:
        state = "plugged in"
    else:
        state = ""
    return int(match.group(1)), state


def read_engine_model_name():
    """Reads OLLAMA_MODEL from jarvis.py so the UI never drifts out of sync."""
    try:
        match = re.search(r'^OLLAMA_MODEL\s*=\s*"([^"]+)"', JARVIS_SCRIPT.read_text(encoding="utf-8"), re.M)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "llama3.2:3b"


def bar(value, width=14):
    n = int(round(max(0.0, min(100.0, value)) / 100.0 * width))
    return "█" * n + "░" * (width - n)


class EngineModel:
    """The UI's picture of the engine, driven only by real engine events. No Tk."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.state = "OFFLINE"
        self.ready = False
        self.commands = 0
        self.last_command = ""
        self.last_response = ""
        self.last_route = ""
        self.level = 0.0

    def start(self):
        self.reset()
        self.state = "INITIALIZING"

    def stop(self):
        self.state = "OFFLINE"
        self.ready = False
        self.level = 0.0

    @property
    def online(self):
        return self.ready

    def apply(self, data):
        """Apply one engine event. Returns a list of (tag, message) for the activity log."""
        event = data.get("event")
        if event == "ready":
            self.ready = True
            self.state = "STANDBY"
            return [("sys", "Engine ready. Wake-word model loaded.")]
        if event == "state":
            new_state = data.get("state")
            if new_state in ENGINE_STATES and self.ready:
                self.state = new_state
            return []
        if event == "wake":
            return [("sys", "Wake word detected.")]
        if event == "heard":
            text = str(data.get("text", "")).strip()
            self.commands += 1
            self.last_command = text
            return [("user", text)]
        if event == "say":
            text = str(data.get("text", "")).strip()
            self.last_response = text
            return [("jarvis", text)]
        if event == "level":
            try:
                self.level = max(0.0, min(1.0, float(data.get("v", 0.0)) * 6.0))
            except (TypeError, ValueError):
                pass
            return []
        if event == "route":
            handler = str(data.get("handler", "")).replace("handle_", "")
            self.last_route = handler
            return [("route", f"routed to {handler}")]
        return []


NOISY_PREFIXES = ("Wake score", "Listening for your command", "Recognizing...", "JARVIS: ", "YOU: ")


def classify_console_line(text):
    """Decide how (or whether) a raw engine console line appears in the activity log."""
    stripped = text.strip()
    if not stripped or stripped.startswith(NOISY_PREFIXES):
        return None
    lowered = stripped.lower()
    if "traceback" in lowered or "error" in lowered or "exception" in lowered or "permission" in lowered:
        return ("err", stripped)
    return ("sys", stripped)


# ---------------------------------------------------------------------------
# Engine process control (no Tk)
# ---------------------------------------------------------------------------
class EngineProcess:
    """Starts jarvis.py, forwards its output to `out_queue`, and stops it gracefully."""

    GRACE = (8.0, 3.0, 2.0)   # seconds to wait after SIGINT, SIGTERM, SIGKILL

    def __init__(self, script, out_queue, python=None):
        self.script = str(script)
        self.out_queue = out_queue
        self.python = python or sys.executable
        self.proc = None
        self.tail = collections.deque(maxlen=12)
        self.stop_requested = False
        self.started_at = None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    def start(self):
        if self.running:
            return False
        env = dict(os.environ, JARVIS_EVENTS="1", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.tail.clear()
        self.stop_requested = False
        self.proc = subprocess.Popen(
            [self.python, "-u", self.script],
            cwd=str(pathlib.Path(self.script).parent), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            start_new_session=True,   # Ctrl-C in the UI's terminal must not hit the engine twice
        )
        self.started_at = time.monotonic()
        threading.Thread(target=self._read_output, args=(self.proc,), daemon=True).start()
        return True

    def _read_output(self, proc):
        try:
            for line in proc.stdout:
                parsed = parse_engine_line(line)
                if parsed is None:
                    continue
                if parsed[0] == "text":
                    self.tail.append(parsed[1])
                self.out_queue.put(parsed)
        except (OSError, ValueError) as error:
            self.out_queue.put(("text", f"[ui] output reader stopped: {error}"))
        finally:
            code = proc.wait()
            self.out_queue.put(("exit", code))

    def request_stop(self):
        """Ask the engine to stop (SIGINT, then SIGTERM, then SIGKILL) without blocking the caller."""
        if not self.running or self.stop_requested:
            return False
        self.stop_requested = True
        threading.Thread(target=self._stop_worker, args=(self.proc,), daemon=True).start()
        return True

    def _stop_worker(self, proc):
        for sig, grace in zip((signal.SIGINT, signal.SIGTERM, signal.SIGKILL), self.GRACE):
            try:
                if sig == signal.SIGINT:
                    proc.send_signal(sig)
                else:
                    os.killpg(proc.pid, sig)   # also reaches `say` and other children
            except (ProcessLookupError, PermissionError):
                return
            try:
                proc.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                self.out_queue.put(("text", f"[ui] engine did not exit after {sig.name}; escalating"))


# ---------------------------------------------------------------------------
# Telemetry (background thread, no Tk)
# ---------------------------------------------------------------------------
def _run(args, timeout=3):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def sample_telemetry(model_name):
    sample = {}
    try:
        sample["cpu"] = min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100.0)
    except (OSError, AttributeError):
        sample["cpu"] = None
    total = _run(["sysctl", "-n", "hw.memsize"]).strip()
    sample["mem"] = parse_vm_stat(_run(["vm_stat"]), int(total)) if total.isdigit() else None
    sample["battery"] = parse_battery(_run(["pmset", "-g", "batt"]))
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1.5) as response:
            names = [m.get("name", "") for m in json.loads(response.read().decode("utf-8")).get("models", [])]
        sample["ollama"] = True
        sample["model_present"] = any(n == model_name or n.startswith(model_name.split(":")[0]) for n in names)
    except (OSError, ValueError):
        sample["ollama"], sample["model_present"] = False, False
    return sample


class TelemetryThread(threading.Thread):
    def __init__(self, out_queue, model_name, period=4.0):
        super().__init__(daemon=True)
        self.out_queue, self.model_name, self.period = out_queue, model_name, period
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.is_set():
            self.out_queue.put(("telemetry", sample_telemetry(self.model_name)))
            self._stop_event.wait(self.period)

    def stop(self):
        self._stop_event.set()


# ---------------------------------------------------------------------------
# Tk widgets
# ---------------------------------------------------------------------------
class FlatButton(tk.Label):
    """Label-based button: tk.Button ignores colours on macOS, labels do not."""

    def __init__(self, parent, text, command, fg, bg, hover, **kwargs):
        super().__init__(parent, text=text, fg=fg, bg=bg, font=(FONT, 10, "bold"),
                         padx=22, pady=10, cursor="hand2", **kwargs)
        self._command, self._fg, self._bg, self._hover = command, fg, bg, hover
        self._enabled = True
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda _e: self._enabled and self.configure(bg=self._hover))
        self.bind("<Leave>", lambda _e: self.configure(bg=self._bg if self._enabled else PANEL2))

    def _click(self, _event):
        if self._enabled:
            self._command()

    def set_enabled(self, enabled):
        self._enabled = enabled
        self.configure(bg=self._bg if enabled else PANEL2, fg=self._fg if enabled else MUTED,
                       cursor="hand2" if enabled else "arrow")


class JarvisUI:
    WAVE_BARS = 56

    def __init__(self, root):
        self.root = root
        root.title("J.A.R.V.I.S. - Interface")
        root.geometry("1120x780")
        root.minsize(940, 660)
        root.configure(bg=BG)

        self.queue = queue.Queue()
        self.model = EngineModel()
        self.engine = EngineProcess(JARVIS_SCRIPT, self.queue)
        self.model_name = read_engine_model_name()
        self.telemetry = {}
        self.telemetry_thread = TelemetryThread(self.queue, self.model_name)
        self.close_after_stop = False

        self.t0 = time.monotonic()
        self.phase = 0.0
        self.level_smooth = 0.0
        self.history = collections.deque([0.0] * self.WAVE_BARS, maxlen=self.WAVE_BARS)

        self._build()
        self._log("sys", "Interface ready. Press INITIALIZE to start the voice engine.")
        self.telemetry_thread.start()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump()
        self._animate()
        self._refresh_panels()

    # ----- layout -----
    def _panel(self, parent, **pack):
        frame = tk.Frame(parent, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        frame.pack(**pack)
        return frame

    def _build(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=28, pady=(20, 8))
        tk.Label(header, text="J.A.R.V.I.S.", bg=BG, fg=CYAN, font=(FONT, 25, "bold")).pack(side="left")
        tk.Label(header, text="  PERSONAL AI  •  LOCAL SYSTEM", bg=BG, fg=MUTED,
                 font=(FONT, 9)).pack(side="left", padx=(12, 0), pady=(10, 0))
        self.status = tk.Label(header, text="● OFFLINE", bg=BG, fg=RED, font=(FONT, 10, "bold"))
        self.status.pack(side="right", pady=(8, 0))
        self.badge = tk.Label(header, text="OFFLINE", bg=PANEL2, fg=MUTED, font=(FONT, 10, "bold"),
                              padx=14, pady=4)
        self.badge.pack(side="right", padx=(0, 16), pady=(6, 0))

        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=28, pady=6)

        left = self._panel(main, side="left", fill="both", expand=True, padx=(0, 12))
        tk.Label(left, text="CORE VISUALIZER", bg=PANEL, fg=MUTED, font=(FONT, 9, "bold")).pack(pady=(14, 0))
        self.orb = tk.Canvas(left, bg=PANEL, height=300, highlightthickness=0)
        self.orb.pack(fill="x", padx=8)
        self.wave = tk.Canvas(left, bg=PANEL, height=70, highlightthickness=0)
        self.wave.pack(fill="x", padx=24, pady=(0, 6))

        self.cmd_label = tk.Label(left, text="LAST COMMAND ▸ -", bg=PANEL, fg=CYAN, font=(FONT, 9),
                                  anchor="w", justify="left", wraplength=560)
        self.cmd_label.pack(fill="x", padx=24)
        self.resp_label = tk.Label(left, text="LAST RESPONSE ▸ -", bg=PANEL, fg=GREEN, font=(FONT, 9),
                                   anchor="w", justify="left", wraplength=560)
        self.resp_label.pack(fill="x", padx=24, pady=(2, 8))

        buttons = tk.Frame(left, bg=PANEL)
        buttons.pack(pady=(0, 16))
        self.start_btn = FlatButton(buttons, "▶  INITIALIZE", self.start, CYAN, "#12344b", "#1b4967")
        self.start_btn.pack(side="left", padx=6)
        self.stop_btn = FlatButton(buttons, "■  SHUT DOWN", self.stop, RED, "#321729", "#51243b")
        self.stop_btn.pack(side="left", padx=6)
        self.stop_btn.set_enabled(False)

        right = tk.Frame(main, bg=PANEL, highlightbackground=EDGE, highlightthickness=1, width=330)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        tk.Label(right, text="SYSTEM TELEMETRY", bg=PANEL, fg=MUTED, font=(FONT, 9, "bold")).pack(
            anchor="w", padx=16, pady=(16, 8))
        self.rows = {}
        for key, title in (("engine", "ENGINE"), ("state", "STATE"), ("llm", "LOCAL LLM"), ("cpu", "CPU LOAD"),
                           ("mem", "MEMORY"), ("battery", "BATTERY"), ("cmds", "COMMANDS / ROUTE"),
                           ("platform", "PLATFORM")):
            row = tk.Frame(right, bg=PANEL2)
            row.pack(fill="x", padx=12, pady=3)
            tk.Label(row, text=title, bg=PANEL2, fg=MUTED, font=(FONT, 8)).pack(anchor="w", padx=10, pady=(6, 0))
            value = tk.Label(row, text="-", bg=PANEL2, fg=TEXT, font=(FONT, 9), anchor="w")
            value.pack(anchor="w", padx=10, pady=(1, 6))
            self.rows[key] = value
        self.rows["platform"].configure(text=f"macOS • {platform.machine()} • Python {platform.python_version()}")

        log_panel = self._panel(self.root, fill="both", padx=28, pady=(6, 20))
        top = tk.Frame(log_panel, bg=PANEL)
        top.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(top, text="ACTIVITY STREAM", bg=PANEL, fg=CYAN, font=(FONT, 9, "bold")).pack(side="left")
        clear = tk.Label(top, text="CLEAR", bg=PANEL, fg=MUTED, font=(FONT, 8), cursor="hand2")
        clear.pack(side="right")
        clear.bind("<Button-1>", lambda _e: self.clear_log())
        self.logbox = tk.Text(log_panel, height=9, bg="#030812", fg=TEXT, font=(FONT, 9), relief="flat",
                              wrap="word", state="disabled", highlightthickness=0, padx=8, pady=6)
        self.logbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        for tag, color in (("user", CYAN), ("jarvis", GREEN), ("sys", MUTED), ("err", RED), ("route", BLUE)):
            self.logbox.tag_configure(tag, foreground=color)
        self.logbox.tag_configure("time", foreground="#35566f")

    # ----- log -----
    PREFIX = {"user": "YOU    ▸ ", "jarvis": "JARVIS ▸ ", "sys": "SYSTEM ▸ ", "err": "ERROR  ▸ ", "route": "ROUTE  ▸ "}

    def _log(self, tag, message):
        box = self.logbox
        box.configure(state="normal")
        box.insert("end", time.strftime("%H:%M:%S  "), "time")
        box.insert("end", self.PREFIX.get(tag, "") + message.rstrip() + "\n", tag)
        lines = int(box.index("end-1c").split(".")[0])
        if lines > 600:
            box.delete("1.0", f"{lines - 500}.0")
        box.see("end")
        box.configure(state="disabled")

    def clear_log(self):
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")

    # ----- engine control -----
    def start(self):
        if self.engine.running:
            return
        if not JARVIS_SCRIPT.exists():
            messagebox.showerror("Jarvis script not found",
                                 f"Place jarvis_ui.py beside jarvis.py.\nExpected:\n{JARVIS_SCRIPT}")
            return
        try:
            self.engine.start()
        except OSError as error:
            self._log("err", f"Could not launch the engine: {error}")
            return
        self.model.start()
        self.start_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self._log("sys", f"Launching engine (pid {self.engine.pid}) with {pathlib.Path(sys.executable).name}...")

    def stop(self):
        if self.engine.request_stop():
            self._log("sys", "Shutdown requested. Waiting for the engine to exit...")
            self.stop_btn.set_enabled(False)

    def _on_exit(self, code):
        self.model.stop()
        self.start_btn.set_enabled(True)
        self.stop_btn.set_enabled(False)
        if self.engine.stop_requested or code in NORMAL_EXIT_CODES:
            self._log("sys", f"Engine stopped (exit code {code}).")
        else:
            self._log("err", f"Engine exited unexpectedly (exit code {code}).")
            for line in list(self.engine.tail)[-6:]:
                self._log("err", line)
        self.engine.stop_requested = False
        if self.close_after_stop:
            self._finish_close()

    def _on_close(self):
        if self.engine.running:
            if not messagebox.askyesno("Exit JARVIS UI", "Shut down the voice engine and exit?"):
                return
            self.close_after_stop = True
            self.stop()
        else:
            self._finish_close()

    def _finish_close(self):
        self.telemetry_thread.stop()
        self.root.destroy()

    # ----- queue pump (runs on Tk's thread) -----
    def _pump(self):
        try:
            for _ in range(200):
                kind, payload = self.queue.get_nowait()
                if kind == "event":
                    for tag, message in self.model.apply(payload):
                        self._log(tag, message)
                    if payload.get("event") == "say":
                        self.resp_label.configure(text=f"LAST RESPONSE ▸ {self.model.last_response}")
                    elif payload.get("event") == "heard":
                        self.cmd_label.configure(text=f"LAST COMMAND ▸ {self.model.last_command}")
                elif kind == "text":
                    classified = classify_console_line(payload)
                    if classified:
                        self._log(*classified)
                elif kind == "telemetry":
                    self.telemetry = payload
                elif kind == "exit":
                    self._on_exit(payload)
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    # ----- panels -----
    def _refresh_panels(self):
        model, tele = self.model, self.telemetry
        style = STATE_STYLE[model.state]
        self.badge.configure(text=style["text"], fg=style["color"] if model.state != "OFFLINE" else MUTED)
        if model.online:
            self.status.configure(text="● ONLINE", fg=GREEN)
        elif model.state == "INITIALIZING":
            self.status.configure(text="● INITIALIZING", fg=AMBER)
        else:
            self.status.configure(text="● OFFLINE", fg=RED)

        if self.engine.running:
            uptime = int(time.monotonic() - (self.engine.started_at or time.monotonic()))
            self.rows["engine"].configure(text=f"pid {self.engine.pid} • up {uptime // 60}m {uptime % 60:02d}s")
        else:
            self.rows["engine"].configure(text="not running")
        self.rows["state"].configure(text=model.state)
        if tele.get("ollama") is None:
            self.rows["llm"].configure(text="checking...", fg=MUTED)
        elif tele["ollama"] and tele.get("model_present"):
            self.rows["llm"].configure(text=f"● Ollama up • {self.model_name}", fg=GREEN)
        elif tele["ollama"]:
            self.rows["llm"].configure(text=f"● Ollama up • {self.model_name} NOT pulled", fg=AMBER)
        else:
            self.rows["llm"].configure(text="● Ollama not reachable", fg=RED)
        cpu, mem, battery = tele.get("cpu"), tele.get("mem"), tele.get("battery")
        self.rows["cpu"].configure(text=f"{bar(cpu)} {cpu:.0f}%" if cpu is not None else "n/a")
        self.rows["mem"].configure(text=f"{bar(mem)} {mem:.0f}%" if mem is not None else "n/a")
        self.rows["battery"].configure(
            text=f"{bar(battery[0])} {battery[0]}% {battery[1]}".strip() if battery else "n/a")
        self.rows["cmds"].configure(text=f"{model.commands} • {model.last_route or '-'}")
        self.root.after(1000, self._refresh_panels)

    # ----- animation -----
    def _wave_values(self, t):
        n, state = self.WAVE_BARS, self.model.state
        if state in ("LISTENING", "STANDBY"):
            return list(self.history)
        if state == "SPEAKING":   # decorative: say's output level is not measured
            return [0.25 + 0.6 * abs(math.sin(t * 5 + i * 0.55)) * (0.6 + 0.4 * math.sin(t * 2.3 + i))
                    for i in range(n)]
        if state in ("THINKING", "INITIALIZING"):
            return [0.10 + 0.14 * (1 + math.sin(t * 3 - i * 0.4)) / 2 for i in range(n)]
        return [0.02] * n

    def _animate(self):
        t = time.monotonic() - self.t0
        state = self.model.state
        style = STATE_STYLE[state]
        color = style["color"]
        self.phase += style["speed"]

        target = self.model.level if state in ("LISTENING", "STANDBY") else 0.0
        self.level_smooth += (target - self.level_smooth) * 0.35
        if state in ("LISTENING", "STANDBY"):
            self.history.append(self.level_smooth)
        self._draw_orb(t, color, style)
        self._draw_wave(t, color)
        self.root.after(33, self._animate)

    def _draw_orb(self, t, color, style):
        c = self.orb
        c.delete("all")
        w, h = max(c.winfo_width(), 300), max(c.winfo_height(), 280)
        cx, cy = w / 2, h / 2
        pulse = (math.sin(self.phase * 8) + 1) / 2
        reactive = self.level_smooth * 26
        base = 52 + pulse * 6 * style["amp"] + reactive
        state = self.model.state

        for i in range(6, 0, -1):                       # soft outer glow (blended, no alpha in Tk)
            r = base + 10 + i * 9
            c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=mix(BG, color, 0.10 + 0.05 * (6 - i)), width=2)
        for i in range(3):                              # dashed orbital rings
            r = 86 + i * 24 + pulse * 4 * style["amp"]
            c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=mix(BG, color, 0.55 - i * 0.15),
                          width=1, dash=(4, 8))
        spin = self.phase * (2.2 if state == "THINKING" else 0.6)
        for j in range(36):                             # rotating tick marks
            a = 2 * math.pi * j / 36 + spin
            inner = base + 10
            outer = inner + 9 + (math.sin(self.phase * 6 + j) * 4 * style["amp"]) + reactive * 0.5
            c.create_line(cx + math.cos(a) * inner, cy + math.sin(a) * inner,
                          cx + math.cos(a) * outer, cy + math.sin(a) * outer,
                          fill=color if j % 3 else mix(BG, color, 0.5), width=2)
        if state in ("THINKING", "INITIALIZING"):       # spinning arcs = "working"
            for k in range(3):
                r = base + 38 + k * 12
                c.create_arc(cx - r, cy - r, cx + r, cy + r, start=math.degrees(spin * (1 + k * 0.4)) + k * 120,
                             extent=70, style="arc", outline=color, width=3)
        for i in range(5):                              # core
            r = base - 4 - i * 9
            if r > 0:
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=mix(BG, color, 0.18 + 0.2 * i), outline="")
        c.create_text(cx, cy, text="J", fill="#eaffff" if state != "OFFLINE" else MUTED, font=(FONT, 30, "bold"))
        c.create_text(cx, cy + 128, text=f"CORE {style['text']}", fill=color if state != "OFFLINE" else MUTED,
                      font=(FONT, 9, "bold"))

    def _draw_wave(self, t, color):
        c = self.wave
        c.delete("all")
        w, h = max(c.winfo_width(), 300), max(c.winfo_height(), 60)
        values = self._wave_values(t)
        n = len(values)
        gap = w / n
        mid = h / 2
        for i, v in enumerate(values):
            v = max(0.0, min(1.0, v * (6 if self.model.state in ("LISTENING", "STANDBY") else 1)))
            half = max(1.5, v * (mid - 3))
            x = gap * i + gap / 2
            c.create_line(x, mid - half, x, mid + half, fill=mix(BG, color, 0.35 + 0.65 * v), width=max(2, gap * 0.55))


def main():
    root = tk.Tk()
    JarvisUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()