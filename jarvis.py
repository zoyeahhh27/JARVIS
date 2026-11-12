import ast
import datetime
import json
import operator
import os
import re
import subprocess
import time
import traceback
from urllib.parse import quote, urlparse, parse_qs

import numpy as np
import pyaudio
import requests
import speech_recognition as sr
from openwakeword.model import Model

# =========================
# CONFIGURATION
# =========================
WAKE_WORD_THRESHOLD = 0.01
WAKE_WORD_CONSECUTIVE_FRAMES = 2
MICROPHONE_DEVICE_INDEX = 0

OLLAMA_MODEL = "llama3.2:3b"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_CHAT_URL = OLLAMA_URL.replace("/api/generate", "/api/chat")

MAX_HISTORY_TURNS = 6
VOLUME_STEP = 10
PLANNER_MAX_ACTIONS = 4
IDLE_LISTEN_LIMIT = 6          # consecutive empty listens before returning to standby
DEFAULT_WEATHER_PLACE = "Hyderabad"
# True: searches / sites replace the current Chrome tab (your original behaviour).
# False: they open in a new tab so your current page is never replaced.
BROWSER_REUSE_CURRENT_TAB = True

conversation_history = []

# Short-term, session-only browser context.
session = {
    "subject": "",       # last search topic
    "site": "",          # "youtube" or "google"
    "yt_query": "",      # last YouTube search
    "yt_results": [],    # last YouTube results we read: [{"t": title, "h": url}, ...]
    "site_hint": None,   # set to "youtube" right after "open YouTube" so a bare "search X" goes there
}


def reset_session_context():
    session.update({"subject": "", "site": "", "yt_query": "", "yt_results": [], "site_hint": None})


def log(message):
    print(f"[jarvis] {message}")


# =========================
# UI EVENT BRIDGE (used by jarvis_ui.py; completely silent unless JARVIS_EVENTS=1)
# =========================
EVENTS_ENABLED = os.environ.get("JARVIS_EVENTS") == "1"
EVENT_PREFIX = "@@JARVIS@@"
engine_state = "STANDBY"       # STANDBY | LISTENING | THINKING | SPEAKING
_events_broken = False


def emit(event, **data):
    """Send one structured event line to the UI over stdout. No-op when not launched by the UI."""
    global _events_broken
    if not EVENTS_ENABLED or _events_broken:
        return
    try:
        print(EVENT_PREFIX + json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)
    except (OSError, ValueError):
        _events_broken = True   # the UI went away (broken pipe); keep working without it


def set_state(state):
    global engine_state
    engine_state = state
    emit("state", state=state)


_meter_failed = False


def _attach_level_meter(source):
    """Report the real microphone level to the UI while SpeechRecognition is listening."""
    stream = getattr(source, "stream", None)
    if not EVENTS_ENABLED or stream is None or not hasattr(stream, "read"):
        return
    original_read = stream.read
    last_emit = [0.0]

    def metered_read(size, *args, **kwargs):
        global _meter_failed
        data = original_read(size, *args, **kwargs)
        now = time.time()
        if not _meter_failed and now - last_emit[0] >= 0.07:
            last_emit[0] = now
            try:
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                if samples.size:
                    emit("level", v=round(float(np.sqrt(np.mean(samples * samples))) / 32768.0, 4))
            except Exception as error:   # never let metering break listening
                _meter_failed = True
                log(f"Level meter disabled: {error}")
        return data

    stream.read = metered_read


# =========================
# TEXT TO SPEECH
# =========================
def speak(text):
    """Speak through macOS `say` (synchronous, so listening resumes after speech ends)."""
    text = str(text).strip()
    if not text:
        return
    print(f"JARVIS: {text}")
    emit("say", text=text)
    previous_state = engine_state
    set_state("SPEAKING")
    try:
        # Text goes via stdin so text starting with "-" is never parsed as an option.
        subprocess.run(["say"], input=text, text=True, check=True, timeout=180)
    except Exception as error:
        print(f"Voice output error: {error}")
    finally:
        set_state(previous_state)


# =========================
# LOW-LEVEL HELPERS
# =========================
def applescript_quote(text):
    """Escape text for use inside an AppleScript string literal."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def run_osascript(script, timeout=10):
    """Run AppleScript. Returns (ok, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "", "osascript timed out"
    except OSError as error:
        return False, "", str(error)
    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()


def wait_until(predicate, timeout=5.0, interval=0.4):
    """Poll predicate() until truthy or timeout. Returns the last value."""
    deadline = time.time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.time() >= deadline:
            return value
        time.sleep(interval)


def clean_query(text):
    return " ".join(str(text).strip().strip(" .,!?\"'").split())


# =========================
# PERSISTENT PERSONAL MEMORY
# =========================
MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_memory.json")


def load_memory():
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as error:
        print(f"Memory load issue: {error}")
    return []


def save_memory(memories):
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as file:
            json.dump(memories, file, indent=2, ensure_ascii=False)
        return True
    except OSError as error:
        print(f"Memory save error: {error}")
        speak("I couldn't save that to my memory file.")
        return False


def remember_fact(fact):
    fact = fact.strip().rstrip(".!?")
    if not fact:
        speak("Tell me what you'd like me to remember.")
        return False
    memories = load_memory()
    if fact.lower() in [item.lower() for item in memories]:
        speak("I already have that saved in my memory.")
        return True
    memories.append(fact)
    if save_memory(memories):
        speak("Okay, I'll remember that " + fact)
        return True
    return False


def recall_memories():
    memories = load_memory()
    if not memories:
        speak("I don't have any saved memories yet. Say remember that, followed by a fact.")
        return True
    speak("Here's what I remember: " + ". ".join(memories))
    return True


def forget_memory(command):
    target = command.strip()
    for prefix in ("forget that", "forget", "remove from memory", "delete memory"):
        if target.startswith(prefix):
            target = target[len(prefix):].strip()
            break
    target = target.strip(" .!?")
    if target.lower() in ("everything", "all", "all memories", "everything you remember"):
        if confirm("This will erase everything I remember."):
            save_memory([])
            speak("I've cleared all saved memories.")
            return True
        speak("Okay, I won't erase anything.")
        return False
    if not target:
        speak("Tell me which memory you'd like me to forget.")
        return False
    memories = load_memory()
    matches = [item for item in memories if target.lower() in item.lower()]
    if not matches:
        speak("I couldn't find a saved memory matching that.")
        return False
    save_memory([item for item in memories if item not in matches])
    speak("I've removed that memory.")
    return True


# =========================
# SPEECH RECOGNITION
# =========================
recognizer = sr.Recognizer()
recognizer.dynamic_energy_threshold = True
recognizer.pause_threshold = 0.7


def listen_for_command():
    """Listen for a spoken command. Empty string means no clear speech was heard."""
    set_state("LISTENING")
    try:
        with sr.Microphone(device_index=MICROPHONE_DEVICE_INDEX) as source:
            print("\nListening for your command...")
            _attach_level_meter(source)
            audio = recognizer.listen(source, timeout=8, phrase_time_limit=12)
        print("Recognizing...")
        set_state("THINKING")
        text = recognizer.recognize_google(audio).lower().strip()
        print(f"YOU: {text}")
        emit("heard", text=text)
        return text
    except sr.WaitTimeoutError:
        print("No speech detected; staying in active mode.")
        return ""
    except sr.UnknownValueError:
        print("Sorry, I couldn't understand that. Staying in active mode.")
        return ""
    except sr.RequestError as error:
        print(f"Speech recognition service error: {error}")
        speak("Speech recognition is having a connection problem.")
        return ""
    except Exception as error:
        print(f"Microphone error: {error}")
        return ""


AFFIRMATIVE_RE = re.compile(r"^(?:yes|yeah|yep|yup|confirm|confirmed|affirmative|sure|do it|go ahead|proceed)\b")


def confirm(question):
    """Ask a yes/no question by voice. Anything other than a clear yes counts as no."""
    speak(f"{question} Say yes to confirm.")
    answer = listen_for_command()
    return bool(AFFIRMATIVE_RE.match(answer))


# =========================
# WAKE WORD DETECTION
# =========================
def load_wake_model():
    return Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")


def wait_for_wake_word(wake_model):
    """Block in standby until 'Hey Jarvis' is heard. Returns True only on a real detection."""
    audio = pyaudio.PyAudio()
    stream = None
    consecutive_hits = 0
    try:
        if hasattr(wake_model, "reset"):
            wake_model.reset()  # drop buffered audio so the last wake word can't re-trigger
        stream = audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=16000,
            input=True,
            input_device_index=MICROPHONE_DEVICE_INDEX,
            frames_per_buffer=1280,
        )
        set_state("STANDBY")
        print("\nJARVIS is in standby. Say 'Hey Jarvis' to wake me.")
        last_level_emit = 0.0
        while True:
            raw_audio = stream.read(1280, exception_on_overflow=False)
            audio_data = np.frombuffer(raw_audio, dtype=np.int16)
            prediction = wake_model.predict(audio_data)
            score = max(prediction.values()) if prediction else 0.0
            if EVENTS_ENABLED:
                if time.time() - last_level_emit >= 0.07:
                    last_level_emit = time.time()
                    rms = float(np.sqrt(np.mean(audio_data.astype(np.float32) ** 2))) / 32768.0
                    emit("level", v=round(rms, 4))
            else:
                print(f"\rWake score: {score:.4f}", end="", flush=True)

            consecutive_hits = consecutive_hits + 1 if score >= WAKE_WORD_THRESHOLD else 0
            if consecutive_hits >= WAKE_WORD_CONSECUTIVE_FRAMES:
                print("\nWake word detected.")
                return True
    except KeyboardInterrupt:
        raise
    except Exception as error:
        print(f"\nWake-word microphone error: {error}")
        speak("I had trouble accessing the microphone.")
        time.sleep(2)
        return False
    finally:
        if stream is not None:
            stream.stop_stream()
            stream.close()
        audio.terminate()


# =========================
# SAFE CALCULATOR
# =========================
_ALLOWED_OPERATORS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def safe_calculate(expression):
    def evaluate(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPERATORS:
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 8 or abs(left) > 1e6):
                raise ValueError("Power is too large")
            return _ALLOWED_OPERATORS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPERATORS:
            return _ALLOWED_OPERATORS[type(node.op)](evaluate(node.operand))
        raise ValueError("Unsupported calculation")

    return evaluate(ast.parse(expression, mode="eval").body)


_CALC_WORDS = (
    (r"\bto the power of\b", "**"), (r"\bmultiplied by\b", "*"), (r"\bdivided by\b", "/"),
    (r"\bplus\b", "+"), (r"\bminus\b", "-"), (r"\btimes\b", "*"), (r"\bover\b", "/"),
    (r"\bmod(?:ulo)?\b", "%"),
)


def to_expression(text):
    expr = text
    for pattern, symbol in _CALC_WORDS:
        expr = re.sub(pattern, f" {symbol} ", expr)
    return expr.replace(",", "").strip()


def handle_calculation(command):
    """Returns True/False when it was a calculation request, None if it was not arithmetic."""
    match = re.match(r"^(calculate|what is|what's|how much is)\s+(.+)$", command)
    if not match:
        return None
    expr = to_expression(match.group(2))
    if not re.fullmatch(r"[\d\s+\-*/%.()]+", expr) or not re.search(r"\d", expr):
        if match.group(1) == "calculate":
            speak("I couldn't calculate that. Try saying calculate 10 plus 5.")
            return False
        return None  # "what is love" etc. belongs to the conversational model
    try:
        result = safe_calculate(expr)
    except ZeroDivisionError:
        speak("I can't divide by zero.")
        return False
    except (ValueError, SyntaxError, OverflowError) as error:
        log(f"Calculation error: {error}")
        speak("I couldn't calculate that. Try saying calculate 10 plus 5.")
        return False
    if isinstance(result, float):
        result = int(result) if result.is_integer() else round(result, 4)
    speak(f"The answer is {result}")
    return True


# =========================
# WEATHER
# =========================
def get_weather(place=DEFAULT_WEATHER_PLACE):
    try:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": place, "count": 1, "language": "en", "format": "json"}, timeout=10,
        ).json()
        results = geo.get("results", [])
        if not results:
            speak(f"I couldn't find {place}.")
            return False
        location = results[0]
        weather = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
            },
            timeout=10,
        ).json().get("current", {})
        speak(
            f"Currently in {location['name']}, it is {weather.get('temperature_2m')} degrees Celsius, "
            f"feels like {weather.get('apparent_temperature')} degrees, with wind speed "
            f"{weather.get('wind_speed_10m')} kilometers per hour."
        )
        return True
    except Exception as error:
        print(f"Weather error: {error}")
        speak("I couldn't get the weather right now.")
        return False


# =========================
# MACBOOK BATTERY
# =========================
def get_battery_status():
    try:
        output = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True,
                                check=True, timeout=5).stdout
        match = re.search(r"(\d+)%", output)
        if not match:
            print(f"Battery status output: {output}")
            speak("I couldn't read the battery percentage.")
            return False
        lowered = output.lower()
        if "discharging" in lowered:
            state = "and it is running on battery power"
        elif "charging" in lowered or "ac power" in lowered:
            state = "and it is plugged in"
        else:
            state = ""
        speak(f"Your MacBook battery is at {match.group(1)} percent {state}.".replace(" .", "."))
        return True
    except Exception as error:
        print(f"Battery status error: {error}")
        speak("I couldn't check your battery status right now.")
        return False


# =========================
# VOLUME
# =========================
_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}


def words_to_int(text):
    total, found = 0, False
    for word in re.findall(r"[a-z]+", text):
        if word in _NUM_WORDS:
            total, found = total + _NUM_WORDS[word], True
        elif word in _TENS:
            total, found = total + _TENS[word], True
        elif word == "hundred":
            total, found = max(total, 1) * 100, True
    return total if found else None


VOLUME_INTENT_RE = re.compile(
    r"\b(?:volume|mute|unmute|louder|quieter)\b|\bsound (?:up|down|on|off)\b"
)
# Search-style commands that merely mention the word "volume" must not control the Mac.
NOT_VOLUME_RE = re.compile(r"^(?:search|google|find|look up|look for|play|youtube|open|go to|remember)\b")


def parse_volume_command(cmd):
    """Pure parser. Returns (action, value) or None.
    actions: status | mute | unmute | set (absolute) | relative (delta) | invalid"""
    if re.search(r"\bunmute\b|\bsound on\b", cmd):
        return ("unmute", None)
    if re.search(r"\bmute\b|\bsound off\b", cmd):
        return ("mute", None)
    if re.fullmatch(
        r"(?:set |what(?:'s| is) )?(?:the )?(?:current )?volume(?: level)?|check (?:the )?volume", cmd
    ):
        return ("status", None)
    if re.search(r"\b(?:max|maximum|full|highest)\b|all the way up", cmd):
        return ("set", 100)
    if re.search(r"\b(?:min|minimum|lowest)\b|all the way down|\bto zero\b", cmd):
        return ("set", 0)

    match = re.search(r"\b(\d{1,3})\b", cmd)
    number = int(match.group(1)) if match else words_to_int(cmd.split("volume", 1)[-1])
    up = re.search(r"\b(?:up|increase|raise|louder|higher)\b", cmd)
    down = re.search(r"\b(?:down|decrease|lower|quieter|reduce|softer)\b", cmd)
    if up and down:
        return None

    if number is not None:
        if (up or down) and re.search(r"\bby\b", cmd):
            return ("relative", number if up else -number)
        if number > 100:
            return ("invalid", number)
        return ("set", number)
    if up:
        return ("relative", VOLUME_STEP)
    if down:
        return ("relative", -VOLUME_STEP)
    return None


def get_output_volume():
    ok, out, err = run_osascript("output volume of (get volume settings)")
    if ok and out.isdigit():
        return int(out)
    if not ok:
        log(f"Volume read error: {err}")
    return None


def control_volume(command):
    parsed = parse_volume_command(command)
    if parsed is None:
        speak("I didn't recognize that volume command.")
        return False
    action, value = parsed

    if action == "invalid":
        speak("Please choose a volume between zero and one hundred percent.")
        return False
    if action == "status":
        current = get_output_volume()
        if current is None:
            speak("I couldn't read the current volume.")
            return False
        speak(f"Current volume is {current} percent. Say set volume to a number to change it.")
        return True
    if action in ("mute", "unmute"):
        script = "set volume with output muted" if action == "mute" else "set volume without output muted"
        ok, _, err = run_osascript(script)
        if not ok:
            log(f"Mute error: {err}")
            speak("I couldn't change the mute setting.")
            return False
        speak("Audio muted." if action == "mute" else "Audio unmuted.")
        return True

    if action == "relative":
        current = get_output_volume()
        if current is None:
            speak("I couldn't read the current volume, so I can't adjust it.")
            return False
        target = max(0, min(100, current + value))
    else:
        target = value

    script = f"set volume output volume {target}"
    if target > 0:
        script += "\nset volume without output muted"
    ok, _, err = run_osascript(script)
    if not ok:
        log(f"Volume set error: {err}")
        speak("I couldn't change the volume.")
        return False
    actual = get_output_volume()  # verify the change really happened
    if actual is not None and abs(actual - target) > 1:
        speak(f"I tried to set the volume to {target} percent, but macOS reports {actual} percent.")
        return False
    speak(f"Volume set to {target} percent.")
    return True


# =========================
# APPLICATIONS (bundle identifiers, not display names)
# =========================
APPS = {
    "finder": {"name": "Finder", "ids": ["com.apple.finder"], "aliases": ["finder", "find a", "finders"]},
    "chrome": {"name": "Google Chrome", "ids": ["com.google.Chrome"], "aliases": ["chrome", "google chrome"]},
    "safari": {"name": "Safari", "ids": ["com.apple.Safari"], "aliases": ["safari"]},
    "vscode": {"name": "Visual Studio Code", "ids": ["com.microsoft.VSCode"],
               "aliases": ["vs code", "vscode", "v s code", "visual studio code", "visual studio", "code"]},
    "calculator": {"name": "Calculator", "ids": ["com.apple.calculator"], "aliases": ["calculator"]},
    "notes": {"name": "Notes", "ids": ["com.apple.Notes"], "aliases": ["notes"]},
    "terminal": {"name": "Terminal", "ids": ["com.apple.Terminal"], "aliases": ["terminal"]},
    "calendar": {"name": "Calendar", "ids": ["com.apple.iCal"], "aliases": ["calendar"]},
    "photos": {"name": "Photos", "ids": ["com.apple.Photos"], "aliases": ["photos"]},
    "settings": {"name": "System Settings", "ids": ["com.apple.systempreferences"],
                 "aliases": ["settings", "system settings", "system preferences", "preferences"]},
    "spotify": {"name": "Spotify", "ids": ["com.spotify.client"], "aliases": ["spotify"]},
    "zoom": {"name": "Zoom", "ids": ["us.zoom.xos"], "aliases": ["zoom"]},
    "discord": {"name": "Discord", "ids": ["com.hnc.Discord"], "aliases": ["discord"]},
    "slack": {"name": "Slack", "ids": ["com.tinyspeck.slackmacgap"], "aliases": ["slack"]},
    "teams": {"name": "Microsoft Teams", "ids": ["com.microsoft.teams2", "com.microsoft.teams"],
              "aliases": ["teams", "microsoft teams", "ms teams"]},
    "notion": {"name": "Notion", "ids": ["notion.id"], "aliases": ["notion"]},
    "mail": {"name": "Mail", "ids": ["com.apple.mail"], "aliases": ["mail"]},
    "music": {"name": "Music", "ids": ["com.apple.Music"], "aliases": ["music"]},
    "messages": {"name": "Messages", "ids": ["com.apple.MobileSMS"], "aliases": ["messages"]},
    "preview": {"name": "Preview", "ids": ["com.apple.Preview"], "aliases": ["preview"]},
    "activity_monitor": {"name": "Activity Monitor", "ids": ["com.apple.ActivityMonitor"],
                         "aliases": ["activity monitor"]},
}
APP_ALIAS_TO_KEY = {alias: key for key, info in APPS.items() for alias in info["aliases"]}

FINDER_ID = "com.apple.finder"
# Quitting these can kill the terminal that is running Jarvis, so ask first.
CONFIRM_QUIT_IDS = {"com.apple.Terminal", "com.microsoft.VSCode", "com.googlecode.iterm2"}


def resolve_app(spoken_name):
    """Exact alias lookup only (no substring matching - 'chrome tab' must not mean 'Chrome')."""
    name = spoken_name.lower().strip(" .!?\"'")
    name = re.sub(r"^(?:the|my)\s+", "", name)
    name = re.sub(r"\s+(?:app|application)$", "", name)
    return APP_ALIAS_TO_KEY.get(re.sub(r"\s+", " ", name))


def running_bundle_ids():
    """Set of bundle ids of running apps, or None if it cannot be determined."""
    ok, out, err = run_osascript(
        'tell application "System Events" to get bundle identifier of every application process')
    if ok:
        return {item.strip() for item in out.split(",") if item.strip() and item.strip() != "missing value"}
    log(f"System Events process list failed: {err}")
    try:
        result = subprocess.run(["lsappinfo", "list"], capture_output=True, text=True, timeout=5, check=False)
        found = set(re.findall(r'bundleID="([^"]+)"', result.stdout))
        return found or None
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"lsappinfo failed: {error}")
        return None


def is_running(bundle_id):
    """True / False, or None when it cannot be determined."""
    ids = running_bundle_ids()
    return None if ids is None else bundle_id in ids


def frontmost_app():
    """Returns (name, bundle_id) of the active app, or None if it cannot be read."""
    script = ('tell application "System Events"\n'
              'set p to first application process whose frontmost is true\n'
              'set b to ""\n'
              'try\nset b to bundle identifier of p as text\nend try\n'
              'return (name of p) & "|||" & b\nend tell')
    ok, out, err = run_osascript(script)
    if not ok or "|||" not in out:
        log(f"Frontmost app lookup failed: {err}")
        return None
    name, _, bundle = out.partition("|||")
    return name, bundle


def launch_app(info):
    """`open -b` launches or activates by bundle id. Returns the id that worked, or None."""
    for bundle_id in info["ids"]:
        try:
            result = subprocess.run(["open", "-b", bundle_id], capture_output=True, text=True,
                                    timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            log(f"open -b {bundle_id} failed: {error}")
            continue
        if result.returncode == 0:
            return bundle_id
        log(f"open -b {bundle_id}: {result.stderr.strip()}")
    return None


def open_app(key):
    info = APPS[key]
    bundle_id = launch_app(info)
    if not bundle_id:
        speak(f"I couldn't find {info['name']}. Make sure it is installed.")
        return False
    if wait_until(lambda: is_running(bundle_id) is not False, timeout=6.0):
        speak(f"Opened {info['name']}.")
        return True
    speak(f"I asked macOS to open {info['name']}, but it doesn't appear to be running.")
    return False


def switch_to_app(key):
    info = APPS[key]
    bundle_id = launch_app(info)
    if not bundle_id:
        speak(f"I couldn't find {info['name']}. Make sure it is installed.")
        return False

    def is_front():
        front = frontmost_app()
        return None if front is None else front[1] in info["ids"]

    state = wait_until(lambda: is_front() is not False, timeout=4.0)
    if state:
        speak(f"Switched to {info['name']}.")
        return True
    speak(f"I asked macOS to switch to {info['name']}, but it didn't come to the front.")
    return False


def quit_bundle(bundle_id, display_name):
    """Quit by bundle id and verify that the app really exited."""
    running = is_running(bundle_id)
    if running is False:
        speak(f"{display_name} isn't running.")
        return False
    ok, _, err = run_osascript(f'tell application id "{applescript_quote(bundle_id)}" to quit', timeout=10)
    if not ok:
        log(f"Quit error for {bundle_id}: {err}")
        speak(f"I couldn't close {display_name}.")
        return False
    if wait_until(lambda: is_running(bundle_id) is not True, timeout=5.0):
        speak(f"Closed {display_name}.")
        return True
    speak(f"I asked {display_name} to quit, but it is still running. It may be waiting for you to save changes.")
    return False


def close_app(key):
    info = APPS[key]
    if info["ids"][0] == FINDER_ID:
        speak("Finder can't be closed.")
        return False
    running_ids = running_bundle_ids()
    candidates = [b for b in info["ids"] if running_ids is None or b in running_ids]
    if not candidates:
        speak(f"{info['name']} isn't running.")
        return False
    bundle_id = candidates[0]
    if bundle_id in CONFIRM_QUIT_IDS and not confirm(
            f"{info['name']} may be the app running me. Quit it anyway?"):
        speak("Okay, I'll leave it open.")
        return False
    return quit_bundle(bundle_id, info["name"])


def close_current_app():
    """Quit the app that is actually in the foreground."""
    front = frontmost_app()
    if front is None:
        speak("I couldn't tell which app is active. Allow Automation access to System Events and try again.")
        return False
    name, bundle_id = front
    if bundle_id == FINDER_ID:
        speak("The active app is Finder, which can't be closed.")
        return False
    if not bundle_id:
        speak(f"I couldn't identify the bundle for {name}, so I won't quit it.")
        return False
    if bundle_id in CONFIRM_QUIT_IDS and not confirm(f"The active app is {name}, which may be running me. Quit it?"):
        speak("Okay, I'll leave it open.")
        return False
    return quit_bundle(bundle_id, name)


CURRENT_APP_RE = re.compile(
    r"^(?:close|quit|exit)(?: the)? (?:current|active|front|frontmost|this)(?: most)? (?:app|application|program)$")
APP_OPEN_RE = re.compile(r"^(?:open|launch|start|run)\s+(.+)$")
APP_SWITCH_RES = (
    re.compile(r"^(?:switch|go|change|jump)(?: over)? to\s+(.+)$"),
    re.compile(r"^(?:activate|focus on|focus|bring up)\s+(.+)$"),
)
APP_CLOSE_RE = re.compile(r"^(?:close|quit|exit)\s+(.+)$")


def handle_app(cmd):
    if CURRENT_APP_RE.match(cmd):
        return close_current_app()
    match = APP_OPEN_RE.match(cmd)
    if match:
        key = resolve_app(match.group(1))
        if key:
            return open_app(key)
    for pattern in APP_SWITCH_RES:
        match = pattern.match(cmd)
        if match:
            key = resolve_app(match.group(1))
            if key:
                return switch_to_app(key)
    match = APP_CLOSE_RE.match(cmd)
    if match:
        key = resolve_app(match.group(1))
        if key:
            return close_app(key)
    return None


# =========================
# BROWSER (Google Chrome via AppleScript)
# =========================
CHROME_ID = "com.google.Chrome"
SITES = {
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "github": "https://github.com",
    "chatgpt": "https://chatgpt.com",
    "gmail": "https://mail.google.com",
    "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com",
    "linkedin": "https://www.linkedin.com",
    "linkedin jobs": "https://www.linkedin.com/jobs",
}
SITE_DISPLAY = {"youtube": "YouTube", "google": "Google"}


class ChromeScriptError(Exception):
    pass


def chrome_error_message(err):
    lowered = err.lower()
    if "-1743" in err or "not authorized" in lowered:
        return ("macOS blocked me from controlling Chrome. Open System Settings, Privacy and Security, "
                "Automation, and allow your terminal app to control Google Chrome.")
    if "turned off" in lowered or "allow javascript" in lowered:
        return ("Chrome is blocking JavaScript from Apple Events. In Chrome, open View, Developer, "
                "and enable Allow JavaScript from Apple Events.")
    return "Chrome reported an error. The details are in the terminal."


def chrome_failed(err):
    log(f"Chrome AppleScript error: {err}")
    speak(chrome_error_message(err))
    return False


def chrome_not_running():
    speak("Chrome isn't running.")
    return False


def chrome_open_url(url):
    """Load a URL in Chrome (current or new tab). Returns True only if Chrome accepted it."""
    if is_running(CHROME_ID) is False:
        try:
            result = subprocess.run(["open", "-b", CHROME_ID, url], capture_output=True, text=True,
                                    timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            log(f"Launching Chrome failed: {error}")
            speak("I couldn't start Chrome.")
            return False
        if result.returncode != 0:
            log(f"open -b Chrome: {result.stderr.strip()}")
            speak("I couldn't start Chrome. Make sure it is installed.")
            return False
        if not wait_until(lambda: is_running(CHROME_ID) is not False, timeout=8.0):
            speak("Chrome didn't start.")
            return False
        return True

    safe_url = applescript_quote(url)
    if BROWSER_REUSE_CURRENT_TAB:
        action = f'set URL of active tab of front window to "{safe_url}"'
    else:
        action = f'tell front window to make new tab at end of tabs with properties {{URL:"{safe_url}"}}'
    script = (f'tell application id "{CHROME_ID}"\n'
              'if (count of windows) = 0 then make new window\n'
              f'{action}\nactivate\nend tell')
    ok, _, err = run_osascript(script)
    return True if ok else chrome_failed(err)


def get_chrome_tab():
    """{'status': ok|not_running|no_window|error, 'title', 'url', 'index', 'count', 'error'}"""
    if is_running(CHROME_ID) is False:
        return {"status": "not_running"}
    script = (f'tell application id "{CHROME_ID}"\n'
              'if (count of windows) = 0 then return "NOWINDOW"\n'
              'set t to active tab of front window\n'
              'return (title of t) & "|||" & (URL of t) & "|||" & (active tab index of front window)'
              ' & "|||" & (count of tabs of front window)\nend tell')
    ok, out, err = run_osascript(script)
    if not ok:
        return {"status": "error", "error": err}
    if out == "NOWINDOW":
        return {"status": "no_window"}
    parts = out.rsplit("|||", 3)
    if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
        return {"status": "ok", "title": parts[0], "url": parts[1],
                "index": int(parts[2]), "count": int(parts[3])}
    title, _, url = out.partition("|||")
    return {"status": "ok", "title": title, "url": url, "index": None, "count": None}


def chrome_js(js):
    """Run JavaScript in Chrome's active tab. Returns (ok, output, error)."""
    script = (f'tell application id "{CHROME_ID}"\n'
              'if (count of windows) = 0 then return "NOWINDOW"\n'
              'tell active tab of front window\n'
              f'set jsResult to execute javascript "{applescript_quote(js)}"\n'
              'end tell\nreturn jsResult\nend tell')
    return run_osascript(script, timeout=15)


def report_current_tab():
    tab = get_chrome_tab()
    if tab["status"] == "not_running":
        speak("Chrome isn't running, so there is no current tab.")
        return False
    if tab["status"] == "no_window":
        speak("Chrome is open, but it has no windows or tabs.")
        return False
    if tab["status"] == "error":
        return chrome_failed(tab["error"])
    title = tab["title"] or "Untitled"
    print(f"Current tab: {title}\nURL: {tab['url']}")
    spoken_url = re.sub(r"^https?://(?:www\.)?", "", tab["url"])[:100]
    position = f" Tab {tab['index']} of {tab['count']}." if tab.get("index") else ""
    speak(f"The current tab is {title}.{position} The address is {spoken_url}.")
    return True


def chrome_new_tab():
    if is_running(CHROME_ID) is False:
        if not launch_app(APPS["chrome"]):
            speak("I couldn't start Chrome.")
            return False
        wait_until(lambda: is_running(CHROME_ID) is not False, timeout=8.0)
        speak("Opened Chrome with a new tab.")
        return True
    script = (f'tell application id "{CHROME_ID}"\n'
              'if (count of windows) = 0 then make new window\n'
              'tell front window to make new tab at end of tabs\nactivate\nend tell')
    ok, _, err = run_osascript(script)
    if not ok:
        return chrome_failed(err)
    speak("Opened a new tab.")
    return True


def chrome_close_tab():
    if is_running(CHROME_ID) is False:
        return chrome_not_running()
    script = (f'tell application id "{CHROME_ID}"\n'
              'if (count of windows) = 0 then return "NOWINDOW"\n'
              'set i to active tab index of front window\n'
              'close tab i of front window\nreturn "OK"\nend tell')
    ok, out, err = run_osascript(script)
    if not ok:
        return chrome_failed(err)
    if out == "NOWINDOW":
        speak("Chrome has no open tabs.")
        return False
    speak("Closed the current tab.")
    return True


def chrome_close_window():
    if is_running(CHROME_ID) is False:
        return chrome_not_running()
    script = (f'tell application id "{CHROME_ID}"\n'
              'if (count of windows) = 0 then return "NOWINDOW"\n'
              'close front window\nreturn "OK"\nend tell')
    ok, out, err = run_osascript(script)
    if not ok:
        return chrome_failed(err)
    if out == "NOWINDOW":
        speak("Chrome has no open windows.")
        return False
    speak("Closed the current Chrome window.")
    return True


# =========================
# SHARED: explain why an active Chrome tab could not be read
# =========================
def tab_problem(tab, nothing_to="do that"):
    """If `tab` (from get_chrome_tab) is not a usable active tab, say why and return True."""
    status = tab["status"]
    if status == "ok":
        return False
    if status == "not_running":
        speak(f"Chrome isn't running, so I can't {nothing_to}.")
    elif status == "no_window":
        speak(f"Chrome is open but has no windows, so I can't {nothing_to}.")
    else:
        chrome_failed(tab.get("error", ""))
    return True


# =========================
# YOUTUBE VIDEO CONTROLS (the video in Chrome's active tab)
# =========================
VIDEO_JS = (
    "(function(a){var v=document.querySelector('video.html5-main-video')||document.querySelector('video');"
    "if(!v){return 'none';}"
    "if(a==='pause'){v.pause();}"
    "else if(a==='play'){var p=v.play();if(p&&p.catch){p.catch(function(){});}}"
    "else if(a==='mute'){v.muted=true;}"
    "else if(a==='unmute'){v.muted=false;}"
    "return JSON.stringify({paused:v.paused,muted:v.muted,t:Math.floor(v.currentTime)});})('ACTION')"
)
VIDEO_DONE = {"pause": "Paused the video.", "play": "Resumed playback.",
              "mute": "Muted the video.", "unmute": "Unmuted the video."}
VIDEO_PAUSE_RE = re.compile(
    r"^(?:pause|stop|halt)(?: the| this| my)?(?: (?:current|youtube))?(?: video| playback| youtube)$|^pause$")
# "play the first video" must NOT match here (that is a search-result command handled by handle_youtube).
VIDEO_PLAY_RE = re.compile(
    r"^(?:resume|continue|unpause|play|start)(?: the| this| my)?(?: (?:current|paused|youtube))*"
    r"(?: video| playback)$|^(?:resume|unpause)$|^continue playing$")
VIDEO_MUTE_RE = re.compile(
    r"^(mute|unmute)(?: the| this| my)?(?: (?:current|youtube))*(?: video| playback)?$")


def _video_reached(action, state):
    return {"pause": state["paused"], "play": not state["paused"],
            "mute": state["muted"], "unmute": not state["muted"]}[action]


def _read_video_state():
    ok, out, _ = chrome_js(VIDEO_JS.replace("ACTION", "state"))
    if not ok:
        return None
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return None


def control_video(action):
    """pause | play | mute | unmute the YouTube video in the active Chrome tab, and verify the result."""
    tab = get_chrome_tab()
    if tab_problem(tab, "control a video"):
        return False
    parsed = urlparse(tab["url"])
    host = parsed.netloc.lower()
    if not (host == "youtube.com" or host.endswith(".youtube.com")):
        speak("The active Chrome tab isn't YouTube, so there is no video for me to control.")
        return False
    if not (parsed.path.startswith("/watch") or parsed.path.startswith("/shorts")):
        speak("YouTube is open, but there is no video playing in this tab.")
        return False

    ok, out, err = chrome_js(VIDEO_JS.replace("ACTION", action))
    if not ok:
        return chrome_failed(err)
    if out == "NOWINDOW":
        speak("Chrome has no open windows.")
        return False
    if out == "none":
        speak("I couldn't find a video player on this page. It may still be loading.")
        return False
    try:
        state = json.loads(out)
    except (ValueError, TypeError):
        log(f"Unexpected video control output: {out!r}")
        speak("YouTube returned something I couldn't understand.")
        return False

    if not _video_reached(action, state):
        # play() and mute can take a moment to be reflected by the player.
        def settled():
            current = _read_video_state()
            return current is not None and _video_reached(action, current)
        if not wait_until(settled, timeout=3.0, interval=0.5):
            if action == "play":
                speak("I asked YouTube to resume, but the video is still paused. Chrome may be blocking playback.")
            else:
                speak("I sent the command, but the video didn't change.")
            return False
    speak(VIDEO_DONE[action])
    return True


# =========================
# CHROME TAB NAVIGATION
# =========================
TAB_NAV_RE = re.compile(
    r"^(?:(?:switch|go|jump|change|move)(?: over)? to |select |show )?(?:the )?"
    r"(?:(next|previous|prev|last|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth"
    r"|\d{1,2}(?:st|nd|rd|th)) tab|tab (?:number |no\.? )?(\w+))$")
BACK_RE = re.compile(r"^(?:go|navigate|take me) back(?: a page| one page| to the previous page)?$"
                     r"|^(?:go to |navigate to )?(?:the )?previous page$")
FORWARD_RE = re.compile(r"^(?:go|navigate|take me) forward(?: a page| one page| to the next page)?$"
                        r"|^(?:go to |navigate to )?(?:the )?next page$")
REFRESH_RE = re.compile(r"^(?:refresh|reload)(?: the| this| current)?(?: page| tab| website| webpage| browser)?$")


def parse_tab_target(cmd):
    """'switch to tab 2' -> ('index', 2); 'next tab' -> ('next', None); not a tab command -> None."""
    match = TAB_NAV_RE.match(cmd)
    if not match:
        return None
    word, number = match.group(1), match.group(2)
    if word:
        if word == "next":
            return ("next", None)
        if word in ("previous", "prev"):
            return ("previous", None)
        if word == "last":
            return ("last", None)
        return ("index", _ORDINALS.get(word) or int(re.match(r"\d+", word).group()))
    value = int(number) if number.isdigit() else words_to_int(number)
    return ("index", value) if value is not None else ("invalid", None)


def switch_tab(kind, number=None):
    """Switch Chrome's active tab (next / previous / first-by-number / last) and verify it changed."""
    if kind == "invalid":
        speak("Which tab number did you mean? Say, for example, switch to tab 2.")
        return False
    tab = get_chrome_tab()
    if tab_problem(tab, "switch tabs"):
        return False
    count, index = tab.get("count"), tab.get("index")
    if not count or not index:
        speak("I couldn't read Chrome's tab list.")
        return False

    if kind == "index":
        if number is None or number < 1:
            speak("Tab numbers start at 1.")
            return False
        if number > count:
            speak(f"Chrome only has {count} tab{'s' if count != 1 else ''}, so there is no tab {number}.")
            return False
        target = number
    elif count == 1:
        speak("Chrome only has one tab open.")
        return True
    elif kind == "next":
        target = index % count + 1
    elif kind == "previous":
        target = (index - 2) % count + 1
    else:  # last
        target = count

    script = (f'tell application id "{CHROME_ID}"\n'
              'if (count of windows) = 0 then return "NOWINDOW"\n'
              f'set active tab index of front window to {target}\n'
              'activate\nreturn "OK"\nend tell')
    ok, out, err = run_osascript(script)
    if not ok:
        return chrome_failed(err)
    after = get_chrome_tab()
    if after["status"] == "ok" and after.get("index") == target:
        speak(f"Switched to tab {target} of {count}: {after['title'] or 'Untitled'}.")
        return True
    speak("I sent the switch command, but Chrome's active tab didn't change.")
    return False


NAV_COMMANDS = {"back": "go back", "forward": "go forward", "reload": "reload"}


def chrome_navigate(action):
    """back | forward | reload in the active Chrome tab."""
    before = get_chrome_tab()
    if tab_problem(before, "navigate"):
        return False
    script = (f'tell application id "{CHROME_ID}"\n'
              'if (count of windows) = 0 then return "NOWINDOW"\n'
              f'tell active tab of front window to {NAV_COMMANDS[action]}\n'
              'return "OK"\nend tell')
    ok, _, err = run_osascript(script)
    if not ok:
        return chrome_failed(err)
    if action == "reload":
        speak("Refreshing the page.")
        return True

    def url_changed():
        now = get_chrome_tab()
        return now["status"] == "ok" and now["url"] != before["url"]

    if wait_until(url_changed, timeout=3.0, interval=0.5):
        speak("Went back." if action == "back" else "Went forward.")
        return True
    speak("I sent the command, but the page didn't change. There may be no "
          f"{'earlier' if action == 'back' else 'later'} page in this tab's history.")
    return False


# =========================
# MACOS WINDOW CONTROLS (minimize / maximize / restore / close - never quits an app)
# =========================
WINDOW_RE = re.compile(
    r"^(minimi[sz]e|maximi[sz]e|restore|unminimi[sz]e|close)(?: the| this| my)?"
    r"(?: (?:current|active|front|frontmost))?(?: (.+?))?(?: windows?)?$")
window_state = {"stack": [], "bounds": {}}   # windows Jarvis minimized / previous sizes, for "restore"


def parse_window_command(cmd):
    """Returns (action, app_key_or_None) for window commands, else None.
    'close' needs the word 'window' so that 'close chrome' (quit the app) is not hijacked."""
    match = WINDOW_RE.match(cmd)
    if not match:
        return None
    verb, target = match.group(1), match.group(2)
    if verb.startswith("minimi"):
        action = "minimize"
    elif verb.startswith("maximi"):
        action = "maximize"
    elif verb.startswith(("restore", "unminimi")):
        action = "restore"
    else:
        action = "close"
    if action == "close" and not re.search(r"\bwindows?$", cmd):
        return None
    key = None
    target = re.sub(r"\s*\bwindows?$", "", target or "").strip()   # the regex can swallow the word "window"
    if target:
        key = resolve_app(target)
        if key is None:
            return None     # e.g. "close current tab", "restore the volume" are not window commands
    return action, key


def window_error_message(err):
    lowered = err.lower()
    if "-25211" in err or "assistive" in lowered or "not allowed" in lowered:
        return ("macOS blocked window control. Open System Settings, Privacy and Security, Accessibility, "
                "and allow your terminal app.")
    if "-1743" in err or "not authorized" in lowered:
        return ("macOS blocked me from controlling windows. Open System Settings, Privacy and Security, "
                "Automation, and allow your terminal app to control System Events.")
    if "-1728" in err:
        return "That app doesn't seem to be running."
    return "I couldn't control that window. The details are in the terminal."


def screen_work_area():
    """(left, top, width, height) of the primary screen's usable area (below menu bar, beside Dock)."""
    js = ("ObjC.import('AppKit');var s=$.NSScreen.screens.objectAtIndex(0);var f=s.frame;var v=s.visibleFrame;"
          "JSON.stringify([Math.round(v.origin.x),Math.round(f.size.height-(v.origin.y+v.size.height)),"
          "Math.round(v.size.width),Math.round(v.size.height)])")
    try:
        result = subprocess.run(["osascript", "-l", "JavaScript", "-e", js],
                                capture_output=True, text=True, timeout=10, check=False)
        values = json.loads(result.stdout.strip())
        if result.returncode == 0 and len(values) == 4:
            return tuple(int(v) for v in values)
        log(f"Screen size lookup failed: {result.stderr.strip()}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        log(f"Screen size lookup failed: {error}")
    return None


def _ax_script(bundle_id, body):
    return ('tell application "System Events"\n'
            f'tell (first application process whose bundle identifier is "{applescript_quote(bundle_id)}")\n'
            f'{body}\nend tell\nend tell')


def _window_scripts(action, bundle_id, area=None, old_bounds=None):
    """AppleScript for one window action. Chrome uses its own dictionary (no Accessibility permission);
    every other app uses System Events (needs Accessibility)."""
    if bundle_id == CHROME_ID:
        head = f'tell application id "{CHROME_ID}"\nif (count of windows) = 0 then return "NOWINDOW"\n'
        if action == "minimize":
            return head + ('set wid to id of front window\nset w to first window whose id is wid\n'
                           'set minimized of w to true\nreturn (minimized of w) as text\nend tell')
        if action == "restore":
            if old_bounds and not area:
                l, t, w, h = old_bounds
                return head + (f'set bounds of front window to {{{l}, {t}, {l + w}, {t + h}}}\n'
                               'return "BOUNDS"\nend tell')
            return head + ('set n to 0\nrepeat with w in windows\nif minimized of w then\n'
                           'set minimized of w to false\nset n to n + 1\nend if\nend repeat\n'
                           'if n > 0 then activate\nreturn n as text\nend tell')
        if action == "maximize":
            x, y, w, h = area
            return head + ('set b to bounds of front window\n'
                           f'set bounds of front window to {{{x}, {y}, {x + w}, {y + h}}}\n'
                           'return "OLD:" & (item 1 of b) & "," & (item 2 of b) & "," & '
                           '((item 3 of b) - (item 1 of b)) & "," & ((item 4 of b) - (item 2 of b))\nend tell')
    if action == "minimize":
        return _ax_script(bundle_id, 'if (count of windows) = 0 then return "NOWINDOW"\n'
                          'set w to window 1\nset value of attribute "AXMinimized" of w to true\n'
                          'return (value of attribute "AXMinimized" of w) as text')
    if action == "restore":
        if old_bounds and not area:
            l, t, w, h = old_bounds
            return _ax_script(bundle_id, 'if (count of windows) = 0 then return "NOWINDOW"\n'
                              f'set position of window 1 to {{{l}, {t}}}\nset size of window 1 to {{{w}, {h}}}\n'
                              'return "BOUNDS"')
        return _ax_script(bundle_id, 'set n to 0\nrepeat with w in windows\ntry\n'
                          'if (value of attribute "AXMinimized" of w) is true then\n'
                          'set value of attribute "AXMinimized" of w to false\nset n to n + 1\n'
                          'end if\nend try\nend repeat\nreturn n as text')
    if action == "maximize":
        x, y, w, h = area
        return _ax_script(bundle_id, 'if (count of windows) = 0 then return "NOWINDOW"\n'
                          'set w to window 1\nset p to position of w\nset s to size of w\n'
                          f'set position of w to {{{x}, {y}}}\nset size of w to {{{w}, {h}}}\n'
                          'return "OLD:" & ((item 1 of p) as integer) & "," & ((item 2 of p) as integer) '
                          '& "," & ((item 1 of s) as integer) & "," & ((item 2 of s) as integer)')
    # close: click the window's red close button (closes the window, never quits the app)
    return _ax_script(bundle_id, 'if (count of windows) = 0 then return "NOWINDOW"\n'
                      'try\nclick (first button of window 1 whose subrole is "AXCloseButton")\n'
                      'return "OK"\non error\nreturn "NOCLOSE"\nend try')


def window_action(action, app_key=None):
    """minimize | maximize | restore | close the window of the named app, or of the active app."""
    if app_key:
        info = APPS[app_key]
        running = running_bundle_ids()
        candidates = [b for b in info["ids"] if running is None or b in running]
        if not candidates:
            speak(f"{info['name']} isn't running.")
            return False
        bundle_id, name = candidates[0], info["name"]
    elif action == "restore" and window_state["stack"]:
        name, bundle_id = window_state["stack"][-1]      # the window Jarvis minimized last
    else:
        front = frontmost_app()
        if front is None:
            speak("I couldn't tell which app is active. Allow Automation access to System Events and try again.")
            return False
        name, bundle_id = front
    if not bundle_id:
        speak(f"I couldn't identify {name}, so I won't touch its windows.")
        return False

    if action == "close":
        if bundle_id == CHROME_ID:
            return chrome_close_window()
        if bundle_id in CONFIRM_QUIT_IDS and not confirm(
                f"The active window belongs to {name}, which may be running me. Close it?"):
            speak("Okay, I'll leave it open.")
            return False

    area = old_bounds = None
    if action == "maximize":
        area = screen_work_area()
        if area is None:
            speak("I couldn't read the screen size, so I can't maximize the window.")
            return False
    old_bounds = window_state["bounds"].get(bundle_id)

    ok, out, err = run_osascript(_window_scripts(action, bundle_id, area, old_bounds), timeout=15)
    if not ok:
        log(f"Window {action} error for {bundle_id}: {err}")
        speak(window_error_message(err))
        return False
    if out == "NOWINDOW":
        speak(f"{name} has no open windows.")
        return False
    if out == "NOCLOSE":
        speak(f"I couldn't find a close button on the {name} window.")
        return False

    if action == "minimize":
        if out != "true":
            speak(f"I asked {name} to minimize, but the window didn't minimize.")
            return False
        window_state["stack"].append((name, bundle_id))
        speak(f"Minimized {name}.")
        return True
    if action == "maximize":
        if out.startswith("OLD:"):
            try:
                window_state["bounds"][bundle_id] = tuple(int(v) for v in out[4:].split(","))
            except ValueError:
                log(f"Could not parse previous window bounds: {out!r}")
        speak(f"Maximized the {name} window.")
        return True
    if action == "restore":
        restored = out == "BOUNDS" or (out.isdigit() and int(out) > 0)
        if not restored and old_bounds:
            # nothing was minimized; undo an earlier maximize instead
            ok2, out2, err2 = run_osascript(_window_scripts("restore", bundle_id, None, old_bounds), timeout=15)
            restored = ok2 and out2 == "BOUNDS"
            if not ok2:
                log(f"Window restore-size error: {err2}")
        if not restored:
            speak(f"{name} has no minimized or maximized window for me to restore.")
            return False
        if out != "BOUNDS":
            run_osascript(f'tell application id "{applescript_quote(bundle_id)}" to activate')
        window_state["stack"] = [item for item in window_state["stack"] if item[1] != bundle_id]
        window_state["bounds"].pop(bundle_id, None)
        speak(f"Restored the {name} window.")
        return True
    speak(f"Closed the {name} window.")
    return True


def search_url(query, site):
    encoded = quote(query, safe="")
    if site == "youtube":
        return f"https://www.youtube.com/results?search_query={encoded}"
    return f"https://www.google.com/search?q={encoded}"


def browser_search(query, site="google", announce=True):
    """Search Google or YouTube in Chrome and remember the topic for follow-ups."""
    query = clean_query(query)
    if not query:
        speak("What would you like me to search for?")
        return False
    if not chrome_open_url(search_url(query, site)):
        return False
    session.update({"subject": query, "site": site, "site_hint": None})
    if site == "youtube":
        session.update({"yt_query": query, "yt_results": []})
    if announce:
        speak(f"Searching {SITE_DISPLAY[site]} for {query}.")
    return True


def browser_open_site(name):
    url = SITES[name]
    if not chrome_open_url(url):
        return False
    session["site_hint"] = name if name in ("youtube", "google") else None
    speak(f"Opening {name} in Chrome.")
    return True


# ---- browser command patterns ----
TAB_NEW_RE = re.compile(r"^(?:open|create|make|start)(?: a)?(?: new)? (?:chrome |browser )?tab$|^new tab$")
TAB_CLOSE_RE = re.compile(r"^close (?:the |this )?(?:current |active )?(?:chrome |browser )?tab$")
OPEN_SITE_RE = re.compile(r"^(?:open|go to|launch|visit|take me to)\s+(?:the )?(.+?)(?:\.com)?(?: website| site)?$")
OPEN_SITE_AND_SEARCH_RE = re.compile(
    r"^(?:open|go to|launch)\s+(youtube|google)\s+(?:and then|and|then)\s+(?:search|find|look up)(?: for)?\s+(.+)$")
GOOGLE_SEARCH_RES = (
    re.compile(r"^(?:search|find|look up|look for)(?: for)?\s+(.+?)\s+(?:on|in|using)\s+google$"),
    re.compile(r"^(?:search|find|look up)\s+(?:on |in |using )?google(?: for)?\s+(.+)$"),
    re.compile(r"^google(?: search)?(?: for)?\s+(.+)$"),
    re.compile(r"^(?:search|look up)\s+(?:the )?(?:web|internet)(?: for)?\s+(.+)$"),
)
LATEST_NEWS_PHRASES = {"find the latest ai news", "latest ai news", "search for latest ai news",
                       "find latest ai news"}


def is_tab_question(cmd):
    if not re.match(r"^(?:what|who|which|tell me|read)\b", cmd):
        return False
    return bool(re.search(r"\b(?:current|active|this|open|my)\s+(?:browser\s+)?(?:tab|page|website|webpage)\b", cmd)
                or re.search(r"\b(?:tab|page|website) am i\b", cmd))


def handle_browser(cmd):
    if is_tab_question(cmd):
        return report_current_tab()
    if TAB_NEW_RE.match(cmd):
        return chrome_new_tab()
    if TAB_CLOSE_RE.match(cmd):
        return chrome_close_tab()
    target = parse_tab_target(cmd)
    if target:
        return switch_tab(*target)
    if BACK_RE.match(cmd):
        return chrome_navigate("back")
    if FORWARD_RE.match(cmd):
        return chrome_navigate("forward")
    if REFRESH_RE.match(cmd):
        return chrome_navigate("reload")

    match = OPEN_SITE_AND_SEARCH_RE.match(cmd)
    if match:
        return browser_search(match.group(2), site=match.group(1))

    if cmd in LATEST_NEWS_PHRASES:
        return browser_search("latest AI news", site="google")

    for pattern in GOOGLE_SEARCH_RES:
        match = pattern.match(cmd)
        if match:
            return browser_search(match.group(1), site="google")

    match = OPEN_SITE_RE.match(cmd)
    if match and match.group(1).strip() in SITES:
        return browser_open_site(match.group(1).strip())
    return None


# =========================
# YOUTUBE
# =========================
YT_SEARCH_RES = (
    re.compile(r"^(?:search|find|look up|look for)(?: for)?\s+(.+?)\s+(?:on|in|at)\s+youtube$"),
    re.compile(r"^(?:search|find|look up|look for)\s+(?:on |in )?youtube(?: for)?\s+(.+)$"),
    re.compile(r"^youtube search(?: for)?\s+(.+)$"),
    re.compile(r"^play\s+(.+?)\s+(?:on|in)\s+youtube$"),
)
YT_BARE_RE = re.compile(r"^(?:search|find|look up)(?: on| in)? youtube(?: for)?$")

_ORDINALS = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5, "sixth": 6, "6th": 6, "seventh": 7, "7th": 7, "eighth": 8, "8th": 8,
    "ninth": 9, "9th": 9, "tenth": 10, "10th": 10,
}
PLAY_ORDINAL_RE = re.compile(
    r"^(?:play|open|click|select|choose)(?: on)?(?: the)? "
    r"(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d{1,2}(?:st|nd|rd|th))"
    r"(?: one| result| video| song| track)?$")
PLAY_NUMBER_RE = re.compile(
    r"^(?:play|open|click|select|choose)(?: on)?(?: the)? (?:number|result|video|track) (\w+)$")
PLAY_FIRST_PHRASES = {
    "play the first result", "play first result", "open the first result", "click the first result",
    "play the first", "play first", "play the first one", "play first one", "play the first video",
    "play first video", "play number one", "play the first song", "play first song",
    "play the first track", "play first track", "click the first video", "open the first video",
    "play it", "play that", "play that song", "play the song", "play his song", "play her song",
    "play their song", "play the latest song", "play his latest song", "play her latest song",
    "play their latest song", "play it now", "play that song now", "play the song now",
}
LATEST_SHORT_PHRASES = {
    "search latest", "find latest", "latest", "search the latest", "find the latest", "the latest",
    "search his latest", "find his latest", "his latest", "search her latest", "find her latest",
    "her latest", "search their latest", "find their latest", "their latest",
    "search for his latest", "search for her latest", "search for their latest",
}
LATEST_SONG_RE = re.compile(r"^(?:now )?(?:search for|search|find) (?:his|her|their) (?:latest|newest) song$")
PRONOUN_SEARCH_RE = re.compile(r"^(?:now )?(?:search for|search|find) (?:his|her|their) (.+)$")

YT_RESULTS_JS = (
    "(function(){var o={u:location.href,r:[]};"
    "var n=document.querySelectorAll('ytd-video-renderer a#video-title');"
    "for(var i=0;i<n.length&&o.r.length<10;i++){if(n[i].href){"
    "o.r.push({t:(n[i].getAttribute('title')||n[i].textContent||'').trim(),h:n[i].href});}}"
    "return JSON.stringify(o);})()"
)
YT_PLAYING_JS = (
    "(function(){var v=document.querySelector('video');if(!v){return 'none';}"
    "if(v.paused){try{v.play();}catch(e){}}return v.paused?'paused':'playing';})()"
)


def _query_matches(url, expected):
    found = parse_qs(urlparse(url).query).get("search_query", [""])[0]
    return found.strip().lower() == expected.strip().lower()


def fetch_youtube_results(expected_query=None, timeout=10.0):
    """Read the video results from the active Chrome tab, waiting for the page to populate."""
    def attempt():
        ok, out, err = chrome_js(YT_RESULTS_JS)
        if not ok:
            if "turned off" in err.lower() or "allow javascript" in err.lower() or "-1743" in err:
                raise ChromeScriptError(err)
            return None
        try:
            data = json.loads(out)
        except (ValueError, TypeError):
            return None
        if urlparse(data.get("u", "")).path != "/results":
            return None
        if expected_query and not _query_matches(data["u"], expected_query):
            return None
        return data.get("r") or None

    try:
        return wait_until(attempt, timeout=timeout, interval=0.8) or []
    except ChromeScriptError as error:
        chrome_failed(str(error))
        return None


def youtube_play_result(index=1):
    """Play the Nth result of the YouTube search currently on screen (or the last one we made)."""
    tab = get_chrome_tab()
    if tab["status"] == "error":
        return chrome_failed(tab["error"])
    on_results = tab["status"] == "ok" and "youtube.com/results" in tab.get("url", "")

    if on_results:
        results = fetch_youtube_results()
    elif session["yt_results"]:
        results = session["yt_results"]
    elif session["yt_query"]:
        if not browser_search(session["yt_query"], site="youtube", announce=False):
            return False
        results = fetch_youtube_results(expected_query=session["yt_query"])
    else:
        speak("I don't have a YouTube search to play from. Say search YouTube for a song first.")
        return False

    if results is None:      # fetch already explained the Chrome error
        return False
    if not results:
        speak("I couldn't read any video results. The page may still be loading, or YouTube changed its layout.")
        return False
    if index > len(results):
        speak(f"I only found {len(results)} results.")
        return False

    session["yt_results"] = results
    item = results[index - 1]
    title = item["t"][:90] or "the video"
    if not chrome_open_url(item["h"]):
        return False

    def playback_state():
        ok, out, _ = chrome_js(YT_PLAYING_JS)
        return out if ok and out == "playing" else None

    if wait_until(playback_state, timeout=12.0, interval=1.0):
        speak(f"Playing {title}.")
        return True
    speak(f"I opened {title}, but Chrome hasn't started playback. You may need to press play.")
    return False


def handle_youtube(cmd):
    # --- follow-ups that rely on remembered context ---
    if cmd in PLAY_FIRST_PHRASES:
        return youtube_play_result(1)
    match = PLAY_ORDINAL_RE.match(cmd)
    if match:
        return youtube_play_result(_ORDINALS[match.group(1)])
    match = PLAY_NUMBER_RE.match(cmd)
    if match:
        value = match.group(1)
        number = int(value) if value.isdigit() else words_to_int(value)
        if number and 1 <= number <= 10:
            return youtube_play_result(number)

    if session["subject"]:
        site = session["site"] or "google"
        if cmd in LATEST_SHORT_PHRASES:
            return browser_search(f"{session['subject']} latest song", site=site)
        if LATEST_SONG_RE.match(cmd):
            word = "newest song" if "newest" in cmd else "latest song"
            return browser_search(f"{session['subject']} {word}", site=site)
        match = PRONOUN_SEARCH_RE.match(cmd)
        if match:
            return browser_search(f"{session['subject']} {match.group(1)}", site=site)
    elif PRONOUN_SEARCH_RE.match(cmd):
        speak("I don't have a previous search to refer to. Who or what should I search for?")
        return False

    # --- explicit YouTube searches ---
    if YT_BARE_RE.match(cmd):
        speak("What should I search for on YouTube?")
        return False
    for pattern in YT_SEARCH_RES:
        match = pattern.match(cmd)
        if match:
            return browser_search(match.group(1), site="youtube")

    # "play <something>" searches YouTube and immediately attempts first-result playback.
    match = re.match(r"^play\s+(.+)$", cmd)
    if match:
        query = clean_query(match.group(1))
        if not browser_search(query, site="youtube", announce=False):
            return False
        # Wait for YouTube's results to populate before selecting the first result.
        results = fetch_youtube_results(expected_query=query, timeout=18.0)
        if results is None or not results:
            speak("I found the YouTube search, but couldn't load its video results.")
            return False
        session["yt_results"] = results
        first = results[0]
        title = first["t"][:90] or "the video"
        if not chrome_open_url(first["h"]):
            return False
        # Try direct video play, then send Space as a user-like playback trigger.
        def playback_state():
            ok, out, _ = chrome_js(YT_PLAYING_JS)
            return out if ok and out == "playing" else None
        if wait_until(playback_state, timeout=4.0, interval=0.8):
            speak(f"Playing {title}.")
            return True
        ok, _, err = run_osascript('tell application "System Events" to key code 49', timeout=5)
        if not ok:
            log(f"YouTube Space-key playback attempt failed: {err}")
        if wait_until(playback_state, timeout=8.0, interval=0.8):
            speak(f"Playing {title}.")
            return True
        speak(f"I opened {title}, but Chrome blocked automatic playback. Check Chrome and System Events automation permissions.")
        return False
    if cmd == "play":
        speak("What would you like me to play?")
        return False
    return None


def handle_generic_search(cmd):
    """Bare 'search X' / 'search for X'. Runs after YouTube and Google handlers."""
    if cmd in {"search", "search for", "google", "look up"}:
        speak("What would you like me to search for?")
        return False
    match = re.match(r"^(?:search|look up|look for)(?: for)?\s+(.+)$", cmd)
    if not match:
        return None
    query = match.group(1)
    if re.match(r"^(?:my )?files?\b", query):   # "search my files for X" belongs to the file handler
        return None
    site = "youtube" if session["site_hint"] == "youtube" else "google"
    return browser_search(query, site=site)


# =========================
# FILES & FOLDERS
# =========================
HOME_DIR = os.path.expanduser("~")
FOLDER_ALIASES = {
    "downloads": os.path.join(HOME_DIR, "Downloads"),
    "download": os.path.join(HOME_DIR, "Downloads"),
    "desktop": os.path.join(HOME_DIR, "Desktop"),
    "documents": os.path.join(HOME_DIR, "Documents"),
    "document": os.path.join(HOME_DIR, "Documents"),
    "pictures": os.path.join(HOME_DIR, "Pictures"),
    "photos": os.path.join(HOME_DIR, "Pictures"),
    "music": os.path.join(HOME_DIR, "Music"),
    "movies": os.path.join(HOME_DIR, "Movies"),
    "home": HOME_DIR,
    "jarvis project": os.path.join(HOME_DIR, "JARVIS-AI"),
    "jarvis ai": os.path.join(HOME_DIR, "JARVIS-AI"),
}
# Never auto-open things that can execute code; reveal them in Finder instead.
RISKY_EXTENSIONS = {".app", ".command", ".sh", ".zsh", ".pkg", ".dmg", ".workflow", ".scpt",
                    ".terminal", ".tool", ".jar", ".py"}
SKIP_DIR_NAMES = {"node_modules", "venv", ".venv", "__pycache__"}

FOLDER_OPEN_RE = re.compile(
    r"^open (?:my |the )?(downloads?|desktop|documents?|pictures|photos|movies|music|home|jarvis project|jarvis ai)"
    r"(?: folder| directory)?$")
LATEST_PDF_RE = re.compile(r"^open (?:my |the )?(?:latest|newest) pdf$")
FILE_SEARCH_PREFIXES = (
    "find my file called ", "find file called ", "find a file called ", "find my file ", "find file ",
    "search for my file ", "search for file ", "search my files for ", "open my file called ",
    "open file called ", "open my file ", "open file ", "find my ",
)
CREATE_FOLDER_RE = re.compile(r"^(?:create|make)(?: a)?(?: new)? folder(?: named| called)?\s+(.+)$")
CREATE_FILE_RE = re.compile(r"^(?:create|make)(?: a)?(?: new)?(?: text)? file(?: named| called)?\s+(.+)$")
LOCATION_SUFFIX_RE = re.compile(r"^(.+?)\s+(?:in|on|inside)(?: my| the)?\s+(downloads|documents|desktop)$")


def open_path(path, label):
    ext = os.path.splitext(path)[1].lower()
    flags = ["-R"] if ext in RISKY_EXTENSIONS else []
    result = subprocess.run(["open", *flags, path], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        log(f"open {path}: {result.stderr.strip()}")
        speak(f"I couldn't open {label}.")
        return False
    if flags:
        speak(f"{label} could run code, so I only showed it in Finder.")
    else:
        speak(f"Opening {label}.")
    return True


def open_folder(path, spoken_name=None):
    path = os.path.abspath(os.path.expanduser(path))
    label = spoken_name or os.path.basename(path)
    if not os.path.isdir(path):
        speak(f"I couldn't find the {label} folder.")
        return False
    return open_path(path, f"{label} folder")


def spotlight_find(name_query, directories_only=False, only_pdf=False):
    safe = name_query.replace('"', "").replace("\\", "").strip()
    if not safe:
        return []
    expression = f'kMDItemFSName == "*{safe}*"cd'
    if only_pdf:
        expression += ' && kMDItemContentType == "com.adobe.pdf"'
    try:
        result = subprocess.run(["mdfind", "-onlyin", HOME_DIR, expression],
                                capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"Spotlight search error: {error}")
        return None
    check = os.path.isdir if directories_only else os.path.isfile
    matches = [p for p in result.stdout.splitlines()
               if check(p) and "/Library/" not in p and "/." not in p]
    return sorted(matches, key=os.path.getmtime, reverse=True)


def find_file_with_spotlight(query, only_pdf=False):
    query = query.strip().strip("\"'")
    if not query:
        speak("Tell me the name of the file you're looking for.")
        return False
    matches = spotlight_find(query, only_pdf=only_pdf)
    if matches is None:
        speak("I had trouble searching for that file.")
        return False
    if not matches:
        speak(f"I couldn't find a file matching {query}.")
        return False
    return open_path(matches[0], os.path.basename(matches[0]))


def open_latest_pdf():
    pdfs = []
    for root in (os.path.join(HOME_DIR, d) for d in ("Downloads", "Desktop", "Documents")):
        if not os.path.isdir(root):
            continue
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIR_NAMES]
            for filename in files:
                if filename.lower().endswith(".pdf"):
                    full = os.path.join(current, filename)
                    try:
                        pdfs.append((os.path.getmtime(full), full))
                    except OSError:
                        pass
    if not pdfs:
        speak("I couldn't find any PDF files in Downloads, Desktop, or Documents.")
        return False
    chosen = max(pdfs)[1]
    return open_path(chosen, f"the latest PDF, {os.path.basename(chosen)}")


def create_item(kind, name, location="desktop"):
    """Create an empty file or folder. Never overwrites anything that already exists."""
    name = name.strip().strip("\"'").strip(" .")
    if not name:
        speak(f"Please say the {kind} name.")
        return False
    if "/" in name or ":" in name or name in {".", ".."}:
        speak(f"Please use a {kind} name without slash or colon characters.")
        return False
    if kind == "file" and "." not in name:
        name += ".txt"
    base = FOLDER_ALIASES.get(location, os.path.join(HOME_DIR, "Desktop"))
    destination = os.path.join(base, name)
    try:
        if kind == "folder":
            os.makedirs(destination, exist_ok=False)
        else:
            with open(destination, "x", encoding="utf-8"):
                pass
    except FileExistsError:
        speak(f"A {kind} named {name} already exists in {location}.")
        return False
    except PermissionError:
        log(f"Permission denied creating {destination}")
        speak(f"macOS denied access to {location}. Allow your terminal app access to that folder "
              "in Privacy and Security, Files and Folders.")
        return False
    except OSError as error:
        log(f"Create {kind} error: {error}")
        speak(f"I couldn't create that {kind}.")
        return False
    speak(f"Created {kind} {name} in {location}.")
    subprocess.run(["open", base], check=False)
    return True


def handle_file_folder(cmd):
    match = FOLDER_OPEN_RE.match(cmd)
    if match:
        alias = match.group(1)
        return open_folder(FOLDER_ALIASES[alias], alias)
    if LATEST_PDF_RE.match(cmd):
        return open_latest_pdf()

    for regex, kind in ((CREATE_FOLDER_RE, "folder"), (CREATE_FILE_RE, "file")):
        match = regex.match(cmd)
        if match:
            name, location = match.group(1).strip(), "desktop"
            loc = LOCATION_SUFFIX_RE.match(name)
            if loc:
                name, location = loc.group(1), loc.group(2)
            return create_item(kind, name, location)

    for prefix in FILE_SEARCH_PREFIXES:
        if cmd.startswith(prefix):
            query = cmd[len(prefix):].strip()
            if query and not re.match(r"^(?:me|out)\b", query):
                return find_file_with_spotlight(query, only_pdf=("pdf" in query))

    match = re.match(r"^open (?:my )?(.+?) project$", cmd)
    if match:
        query = match.group(1).strip()
        folders = spotlight_find(query, directories_only=True)
        if folders:
            return open_folder(folders[0], f"{query} project")
        speak(f"I couldn't find the {query} project folder.")
        return False
    return None


# =========================
# SCREENSHOTS
# =========================
SCREENSHOT_DIR = os.path.join(HOME_DIR, "Pictures", "JARVIS-Screenshots")
SCREENSHOT_RES = (
    re.compile(r"^(?:take|capture|grab|make|get)(?: me)?(?: a| an| the| my)?(?: new| full)?"
               r" (?:screenshot|screen shot|screen capture|screen)(?: of (?:my |the )?(?:screen|display|desktop))?$"),
    re.compile(r"^(?:screenshot|screen shot|screen capture)$"),
    re.compile(r"^(?:take|capture) (?:a )?(?:picture|photo|snapshot) of (?:my |the )?(?:screen|display|desktop)$"),
)


def unique_screenshot_path(directory=None, now=None):
    """Timestamped filename that never collides with an existing file."""
    directory = directory or SCREENSHOT_DIR
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    for n in range(1000):
        name = f"jarvis-screenshot-{stamp}.png" if n == 0 else f"jarvis-screenshot-{stamp}-{n}.png"
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            return path
    raise FileExistsError("Could not find an unused screenshot filename")


def screen_recording_allowed():
    """True/False from macOS's permission preflight, or None if it cannot be checked."""
    try:
        import ctypes
        core_graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        return bool(core_graphics.CGPreflightScreenCaptureAccess())
    except (OSError, AttributeError) as error:
        log(f"Screen Recording preflight unavailable: {error}")
        return None


def take_screenshot():
    try:
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        path = unique_screenshot_path()
    except PermissionError:
        speak("macOS denied access to your Pictures folder. Allow your terminal app under Files and Folders.")
        return False
    except OSError as error:
        log(f"Screenshot folder error: {error}")
        speak("I couldn't prepare the screenshot folder.")
        return False
    try:
        result = subprocess.run(["screencapture", "-x", "-t", "png", path],
                                capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"screencapture failed: {error}")
        speak("I couldn't run the screenshot tool.")
        return False
    if result.returncode != 0 or not os.path.isfile(path) or os.path.getsize(path) == 0:
        log(f"screencapture rc={result.returncode}: {result.stderr.strip()}")
        speak("The screenshot failed. Allow Screen Recording for your terminal app in "
              "Privacy and Security, then restart it and try again.")
        return False
    print(f"Screenshot saved: {path}")
    speak(f"Screenshot saved as {os.path.basename(path)} in Pictures, JARVIS Screenshots.")
    if screen_recording_allowed() is False:
        speak("Screen Recording permission looks missing, so the image may not include other apps' windows.")
    return True


def handle_screenshot(cmd):
    if any(pattern.match(cmd) for pattern in SCREENSHOT_RES):
        return take_screenshot()
    return None


# =========================
# LOCAL AI (OLLAMA) - plain text only, never executed
# =========================
def ask_ollama(question):
    global conversation_history
    try:
        memories = load_memory()
        system = (
            "You are JARVIS, a friendly voice assistant running locally on the user's MacBook. "
            "Answer naturally in one to three short sentences, plain text only (no markdown, no lists), "
            "because your answer is spoken aloud. "
            "You cannot control the computer, apps, the browser or the volume in this chat. "
            "Never say or imply that you performed an action such as opening, closing, searching or playing. "
            "If the user seems to want such an action, say you didn't understand the command and ask them to rephrase. "
            "Use the saved memories only when relevant and never invent memories. "
            "Saved memories: " + json.dumps(memories, ensure_ascii=False)
        )
        messages = [{"role": "system", "content": system}]
        messages += conversation_history[-(MAX_HISTORY_TURNS * 2):]
        messages.append({"role": "user", "content": question})

        response = requests.post(
            OLLAMA_CHAT_URL,
            json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
            timeout=90,
        )
        response.raise_for_status()
        answer = response.json().get("message", {}).get("content", "").strip()
        answer = re.sub(r"[*_#`]+", "", answer).strip() or "I didn't get an answer."

        conversation_history.append({"role": "user", "content": question})
        conversation_history.append({"role": "assistant", "content": answer})
        conversation_history = conversation_history[-(MAX_HISTORY_TURNS * 2):]
        speak(answer)
        return True
    except requests.exceptions.ConnectionError:
        speak("I can't connect to Ollama. Please make sure Ollama is running.")
    except Exception as error:
        print(f"Ollama error: {error}")
        speak("I ran into a problem while generating an answer.")
    return False


# =========================
# UTILITIES (memory, time, date, weather, battery, calculator, help)
# =========================
HELP_TEXT = (
    "I can tell you the time and date, check weather, calculate, control volume, open, switch to and quit "
    "apps, open websites, folders and files, search Google and YouTube, play YouTube results, pause and resume "
    "videos, switch Chrome tabs, minimize and maximize windows, take screenshots, check battery status, remember facts, and answer questions using my local AI. "
    "Say stop listening to sleep, or shutdown Jarvis to exit."
)


def handle_utilities(cmd):
    match = re.match(r"^remember(?: that)?\s*(.*)$", cmd)
    if match:
        return remember_fact(match.group(1))
    if cmd in {"what do you remember", "what do you remember about me", "list my memories", "recall memories"}:
        return recall_memories()
    if re.match(r"^(?:forget|remove from memory|delete memory)\b", cmd):
        return forget_memory(cmd)
    if cmd in {"who are you", "introduce yourself", "what is your name", "what's your name"}:
        speak("I am JARVIS, your personal AI assistant.")
        return True
    if cmd in {"help", "what can you do", "list commands"}:
        speak(HELP_TEXT)
        return True
    if re.search(r"\b(?:what time is it|what(?:'s| is) the time|tell me the time|current time)\b", cmd):
        speak(f"The time is {datetime.datetime.now().strftime('%I:%M %p').lstrip('0')}.")
        return True
    if re.search(r"\b(?:what is today's date|what(?:'s| is) the date|today's date|current date|what day is it)\b", cmd):
        speak(f"Today is {datetime.datetime.now().strftime('%A, %B %d, %Y')}.")
        return True
    if re.search(r"\bweather\b", cmd):
        match = re.search(r"\bweather(?: like)?(?: (?:in|for|at))?\s+(.+)$", cmd)
        place = DEFAULT_WEATHER_PLACE
        if match:
            candidate = re.sub(r"\b(?:today|right now|now|tomorrow|currently)\b", "", match.group(1)).strip()
            place = candidate or place
        return get_weather(place)
    if re.search(r"\bbattery\b", cmd):
        return get_battery_status()
    return handle_calculation(cmd)


# =========================
# COMMAND CONTROL / SYSTEM
# =========================
SLEEP_PHRASES = {
    "sleep", "sleep jarvis", "jarvis sleep", "go to sleep", "stop listening", "standby", "stand by",
    "go on standby", "that's all", "that is all", "stop listening jarvis",
}
SHUTDOWN_PHRASES = {
    "shutdown", "shut down", "shutting down", "shutdown jarvis", "shut down jarvis", "quit", "quit jarvis",
    "exit", "exit jarvis", "close jarvis", "terminate", "terminate jarvis", "shutting down goodbye",
    "goodbye shut down",
}
POWER_RE = re.compile(r"^(shut ?down|restart|reboot)(?: the| my)? (?:mac|macbook|computer|laptop|system|machine)$")


def handle_control(cmd):
    if cmd in SLEEP_PHRASES or cmd.startswith(("sleep jarvis", "go to sleep")):
        return "sleep"
    if cmd in SHUTDOWN_PHRASES:
        return "shutdown"
    return None


def handle_power(cmd):
    """Shutting down or restarting macOS always needs a spoken yes."""
    match = POWER_RE.match(cmd)
    if not match:
        return None
    restart = match.group(1) in ("restart", "reboot")
    verb = "restart" if restart else "shut down"
    if not confirm(f"Do you really want me to {verb} your Mac?"):
        speak("Okay, cancelled.")
        return False
    ok, _, err = run_osascript(f'tell application "System Events" to {verb}')
    if not ok:
        log(f"Power command error: {err}")
        speak(f"I couldn't {verb} the Mac.")
        return False
    return True


def handle_video(cmd):
    if VIDEO_PAUSE_RE.match(cmd):
        return control_video("pause")
    if VIDEO_PLAY_RE.match(cmd):
        return control_video("play")
    match = VIDEO_MUTE_RE.match(cmd)
    if match and re.search(r"\b(?:video|playback|youtube)\b", cmd):   # bare "mute" stays a system command
        return control_video(match.group(1))
    return None


def handle_window(cmd):
    parsed = parse_window_command(cmd)
    return window_action(*parsed) if parsed else None


def handle_volume(cmd):
    if NOT_VOLUME_RE.match(cmd) or not VOLUME_INTENT_RE.search(cmd):
        return None
    return control_volume(cmd)


# =========================
# COMPOUND COMMANDS + PLANNER
# =========================
ACTION_VERBS = (
    r"open|launch|close|quit|switch|search|google|play|set|increase|decrease|mute|unmute|turn|check|"
    r"create|make|calculate|lower|raise|go to|what(?:'s| is) the (?:time|date|weather)|volume|"
    r"pause|resume|minimi[sz]e|maximi[sz]e|restore|take|capture|refresh|reload|screenshot|go back|go forward"
)
COMPOUND_SPLIT_RE = re.compile(
    r"\s*,?\s*\b(?:and then|and also|and|then|after that)\b\s+(?=(?:" + ACTION_VERBS + r")\b)")


def split_compound(cmd):
    """'open calculator and set volume to 40' -> two commands. Splits only before a real command verb,
    so 'search tom and jerry' stays one command."""
    if OPEN_SITE_AND_SEARCH_RE.match(cmd):
        return [cmd]
    parts = [p.strip() for p in COMPOUND_SPLIT_RE.split(cmd) if p and p.strip()]
    return parts if len(parts) > 1 else [cmd]


# Each form the LLM planner may emit, with the words the user must actually have said.
PLANNER_FORMS = (
    (re.compile(r"^open a new tab$"), {"tab"}),
    (re.compile(r"^open (?!a new tab$)(.+)$"), {"open", "launch", "start"}),
    (re.compile(r"^search youtube for (.+)$"), {"search", "find", "look"}),
    (re.compile(r"^search google for (.+)$"), {"search", "google", "find", "look"}),
    (re.compile(r"^play (.+)$"), {"play"}),
    (re.compile(r"^close the current tab$"), {"close"}),
    (re.compile(r"^check battery$"), {"battery"}),
    (re.compile(r"^what time is it$"), {"time"}),
    (re.compile(r"^what is today's date$"), {"date", "today"}),
    (re.compile(r"^weather in (.+)$"), {"weather"}),
)
PLANNER_HINT_WORDS = {"open", "close", "search", "play", "google", "battery", "weather", "tab", "time", "date",
                      "launch", "start"}


def validate_planned_actions(actions, original):
    """Keep only actions that match an allowed form AND are grounded in what the user said,
    so the model cannot invent steps such as an unrequested 'play' or 'new tab'."""
    words = set(re.findall(r"[a-z0-9']+", original.lower()))
    cleaned = []
    for action in actions[:PLANNER_MAX_ACTIONS]:
        if not isinstance(action, str):
            return None
        action = " ".join(action.lower().strip().split()).strip(" .!?\"")
        for pattern, required in PLANNER_FORMS:
            match = pattern.match(action)
            if not match:
                continue
            if not (required & words):
                return None
            arg_words = re.findall(r"[a-z0-9']+", match.group(1)) if match.groups() else []
            if any(w not in words for w in arg_words):
                return None
            cleaned.append(action)
            break
        else:
            return None
    return cleaned if len(cleaned) >= 2 else None


def plan_with_ollama(command):
    """Fallback for compound requests the deterministic splitter could not handle."""
    if not re.search(r"\b(?:and then|then|after that|and)\b", command):
        return None
    if len(PLANNER_HINT_WORDS & set(re.findall(r"[a-z]+", command))) < 2:
        return None
    prompt = (
        "Convert the user's request into JSON only, shaped exactly as {\"actions\":[\"command\", ...]}. "
        "Use only steps the user explicitly asked for, in order, at most 4. Allowed forms: "
        "open <app or website>; open a new tab; search youtube for <query>; search google for <query>; "
        "play <query>; close the current tab; check battery; what time is it; what is today's date; "
        "weather in <place>. Never add steps the user did not ask for. If the request cannot be expressed "
        "with these forms return {\"actions\":[]}.\n"
        f"User request: {command}\nJSON:"
    )
    try:
        response = requests.post(OLLAMA_URL, json={"model": OLLAMA_MODEL, "prompt": prompt,
                                                   "stream": False, "format": "json"}, timeout=45)
        response.raise_for_status()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.json().get("response", "").strip(),
                     flags=re.IGNORECASE)
        actions = json.loads(raw).get("actions", [])
        if not isinstance(actions, list):
            return None
        return validate_planned_actions(actions, command)
    except Exception as error:
        print(f"Planner error: {error}")
        return None


def execute_steps(steps):
    """Run several commands in order through the normal deterministic router; stop on the first failure."""
    if len(steps) > PLANNER_MAX_ACTIONS:
        speak(f"That is more than {PLANNER_MAX_ACTIONS} steps. I'll do the first {PLANNER_MAX_ACTIONS}.")
        steps = steps[:PLANNER_MAX_ACTIONS]
    speak(f"I have {len(steps)} steps. Starting now.")
    for index, step in enumerate(steps, start=1):
        print(f"Step {index}/{len(steps)}: {step}")
        result = route_single(step)
        if result in ("sleep", "shutdown"):
            return result
        if result is None:
            speak(f"I didn't understand step {index}: {step}. I'm stopping here.")
            return False
        if result is False:
            speak(f"Step {index} didn't work, so I'm stopping here.")
            return False
        time.sleep(0.35)
    speak("All steps are complete.")
    return True


# =========================
# COMMAND ROUTING
# =========================
HANDLERS = (
    handle_control,         # 1
    handle_power,           # 2
    handle_screenshot,      # 2
    handle_video,           # 3  (before volume: "mute the video" must not mute the Mac)
    handle_volume,          # 4
    handle_window,          # 5
    handle_app,             # 6
    handle_browser,         # 7
    handle_youtube,         # 8
    handle_generic_search,  # 8 (after YouTube / Google so it cannot steal their prefixes)
    handle_file_folder,     # 9
    handle_utilities,       # 10
)

_CAN_YOU_VERBS = (ACTION_VERBS + r"|find|tell|remember|forget|what time|what's the|what is the|look up|show")
_PREFIX_RE = re.compile(r"^(?:(?:hey|ok|okay)\s+)?(?:jarvis\b[,]?\s*)?(?:please\b\s*)?")
_POLITE_RE = re.compile(r"^(?:(?:can|could|would|will) you\s+)(?:please\s+)?(?=(?:" + _CAN_YOU_VERBS + r")\b)")
_SUFFIX_RE = re.compile(r"[\s,]+(?:please|for me)$")


def normalize_command(raw):
    cmd = " ".join(str(raw).lower().strip().split())
    cmd = cmd.strip(" .!?,\"")
    cmd = _PREFIX_RE.sub("", cmd)
    cmd = _POLITE_RE.sub("", cmd)
    cmd = _SUFFIX_RE.sub("", cmd)
    return cmd.strip(" .!?,\"")


def route_single(cmd):
    """Run the first matching handler. Returns True/False (handled), 'sleep'/'shutdown', or None."""
    for handler in HANDLERS:
        result = handler(cmd)
        if result is not None:
            emit("route", handler=handler.__name__, command=cmd)
            return result
    return None


UNKNOWN_ACTION_RE = re.compile(r"^(?:open|launch|start|close|quit|switch|activate|focus)\b")
DOMAIN_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?$")


def handle_unknown_action(cmd):
    """Action-style commands that matched nothing: say so instead of letting the LLM improvise."""
    match = re.match(r"^(?:open|launch|start)\s+(?:the |my )?(.+)$", cmd)
    if match:
        target = match.group(1).strip()
        if DOMAIN_RE.match(target):
            return chrome_open_url(f"https://{target}") and (speak(f"Opening {target}.") or True)
        speak(f"I don't have an app, folder or website called {target}.")
        return False
    if UNKNOWN_ACTION_RE.match(cmd):
        speak("I don't know how to do that yet.")
        return False
    return None


def process_command(command):
    """Returns 'sleep', 'shutdown' or None. Never raises for ordinary command failures."""
    set_state("THINKING")
    raw_lower = " ".join(str(command).lower().strip().split()).strip(" .!?,\"")
    # Control words are checked on the un-stripped text so "sleep jarvis" / "close jarvis" survive.
    control = handle_control(raw_lower)
    if control:
        return control

    cmd = normalize_command(command)
    if not cmd:
        return None

    steps = split_compound(cmd)
    result = execute_steps(steps) if len(steps) > 1 else route_single(cmd)

    if result is None:
        planned = plan_with_ollama(cmd)
        if planned:
            result = execute_steps(planned)
    if result is None:
        result = handle_unknown_action(cmd)
    if result is None:
        emit("route", handler="ask_ollama", command=cmd)
        ask_ollama(cmd)
        return None
    return result if result in ("sleep", "shutdown") else None


# =========================
# MAIN: WAKE ONCE, THEN KEEP LISTENING
# =========================
def main():
    try:
        wake_model = load_wake_model()
    except KeyboardInterrupt:
        return
    except Exception as error:
        print(f"Could not load the wake-word model: {error}")
        print("Run:  python -c \"import openwakeword; openwakeword.utils.download_models()\"")
        return

    emit("ready")
    speak("JARVIS is online. Say Hey Jarvis to wake me.")
    try:
        while True:
            if not wait_for_wake_word(wake_model):
                continue  # microphone problem - never enter active mode without a real wake word
            emit("wake")
            time.sleep(0.5)  # let the wake-word stream fully close
            reset_session_context()
            speak("Yes, I'm listening. You can give me multiple commands.")

            idle = 0
            while True:
                command = listen_for_command()
                if not command:
                    idle += 1
                    if idle >= IDLE_LISTEN_LIMIT:
                        speak("I haven't heard anything, so I'm going to standby. Say Hey Jarvis when you need me.")
                        break
                    continue
                idle = 0

                try:
                    result = process_command(command)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    traceback.print_exc()
                    speak("Something went wrong while running that command.")
                    continue

                if result == "sleep":
                    speak("Okay. Going to standby. Say Hey Jarvis when you need me.")
                    break
                if result == "shutdown":
                    speak("Shutting down. Goodbye.")
                    return
    except KeyboardInterrupt:
        speak("JARVIS shutting down. Goodbye.")


if __name__ == "__main__":
    main()
