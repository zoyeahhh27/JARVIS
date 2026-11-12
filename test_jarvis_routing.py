import sys
import time
import types
import unittest
from unittest import mock


def _stub(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules.setdefault(name, module)
    return module


for _name in ("numpy", "pyaudio", "requests", "speech_recognition"):
    try:
        __import__(_name)
    except ImportError:
        pass
if "speech_recognition" not in sys.modules:
    _stub("speech_recognition", Recognizer=lambda: types.SimpleNamespace(), Microphone=object,
          WaitTimeoutError=Exception, UnknownValueError=Exception, RequestError=Exception)
if "pyaudio" not in sys.modules:
    _stub("pyaudio", paInt16=8, PyAudio=object)
if "numpy" not in sys.modules:
    _stub("numpy", frombuffer=lambda *a, **k: None, int16=None)
if "requests" not in sys.modules:
    _stub("requests", post=None, get=None, exceptions=types.SimpleNamespace(ConnectionError=Exception))
try:
    import tkinter  # noqa: F401,E402  (a real Tk is fine; no window is ever created by these tests)
except ImportError:
    class _Widget:
        def __init__(self, *a, **k):
            pass
    _stub("tkinter", Label=_Widget, Frame=_Widget, Canvas=_Widget, Tk=_Widget, Text=_Widget,
          messagebox=types.SimpleNamespace())
_stub("openwakeword")
_stub("openwakeword.model", Model=object)

import jarvis  # noqa: E402
import jarvis_ui  # noqa: E402


class Recorder:
    """Replace every action function with a recorder so only the routing decision is observed."""

    def __init__(self):
        self.events = []

    def install(self, test):
        def rec(name, result=True):
            def fn(*args, **kwargs):
                self.events.append((name, *args, *[f"{k}={v}" for k, v in kwargs.items()]))
                return result
            return fn

        targets = {
            "open_app": "open_app", "switch_to_app": "switch_app", "close_app": "close_app",
            "close_current_app": "close_current_app", "control_volume": "volume",
            "report_current_tab": "tab_info", "chrome_new_tab": "new_tab", "chrome_close_tab": "close_tab",
            "chrome_close_window": "close_window", "youtube_play_result": "play",
            "get_battery_status": "battery", "get_weather": "weather", "open_latest_pdf": "latest_pdf",
            "browser_open_site": "open_site", "recall_memories": "recall", "remember_fact": "remember",
            "forget_memory": "forget", "open_folder": "open_folder", "find_file_with_spotlight": "find_file",
            "create_item": "create", "ask_ollama": "OLLAMA", "browser_search": "search",
            "window_action": "window", "control_video": "video", "switch_tab": "tab_switch",
            "chrome_navigate": "nav", "take_screenshot": "screenshot",
        }
        for attr, label in targets.items():
            patcher = mock.patch.object(jarvis, attr, rec(label))
            patcher.start()
            test.addCleanup(patcher.stop)
        for patcher in (
            mock.patch.object(jarvis, "speak", lambda *a, **k: self.events.append(("speak", *a))),
            mock.patch.object(jarvis.time, "sleep", lambda *_: None),
            mock.patch.object(jarvis, "plan_with_ollama", lambda c: None),
            mock.patch.object(jarvis, "confirm", lambda q: True),
            mock.patch.object(jarvis, "run_osascript", lambda *a, **k: (True, "", "")),
        ):
            patcher.start()
            test.addCleanup(patcher.stop)


# (spoken transcript, expected first non-speak event)
ROUTING_CASES = [
    # --- control ---
    ("shutdown", "shutdown"), ("exit", "shutdown"), ("shut down jarvis", "shutdown"),
    ("close jarvis", "shutdown"), ("stop listening", "sleep"), ("go to sleep", "sleep"),
    # --- apps (VS Code regression) ---
    ("switch to vs code", ("switch_app", "vscode")),
    ("switch to VS Code.", ("switch_app", "vscode")),
    ("switch to visual studio code", ("switch_app", "vscode")),
    ("switch to finder", ("switch_app", "finder")),
    ("switch to find a", ("switch_app", "finder")),
    ("open calculator", ("open_app", "calculator")),
    ("open Spotify", ("open_app", "spotify")),
    ("open the notes app", ("open_app", "notes")),
    ("open chrome", ("open_app", "chrome")),
    ("open system settings", ("open_app", "settings")),
    ("open microsoft teams", ("open_app", "teams")),
    ("close calculator", ("close_app", "calculator")),
    ("quit chrome", ("close_app", "chrome")),
    ("close the current app", "close_current_app"),
    ("quit the active application", "close_current_app"),
    # "chrome tab" must never be read as the Chrome app
    ("close the current tab", "close_tab"),
    ("close current tab", "close_tab"),
    ("close this tab", "close_tab"),
    ("close the current window", ("window", "close", "None")),
    ("open a new tab", "new_tab"),
    # --- tab info ---
    ("what is the current tab", "tab_info"),
    ("who is the current tab", "tab_info"),
    ("what's the current tab", "tab_info"),
    ("what tab am i on", "tab_info"),
    # --- search: must NOT reach Ollama ---
    ("search love yourself", ("search", "love yourself", "site=google")),
    ("search for alfaaz", ("search", "alfaaz", "site=google")),
    ("search youtube for love yourself", ("search", "love yourself", "site=youtube")),
    ("search on youtube for alfaaz", ("search", "alfaaz", "site=youtube")),
    ("search alfaaz on youtube", ("search", "alfaaz", "site=youtube")),
    ("search google for python tutorials", ("search", "python tutorials", "site=google")),
    ("google python tutorials", ("search", "python tutorials", "site=google")),
    ("open youtube and search love yourself", ("search", "love yourself", "site=youtube")),
    ("open youtube and search for love yourself", ("search", "love yourself", "site=youtube")),
    ("play love yourself", ("search", "love yourself", "site=youtube")),
    ("please search for cats", ("search", "cats", "site=google")),
    ("can you search youtube for jazz", ("search", "jazz", "site=youtube")),
    ("search tom and jerry", ("search", "tom and jerry", "site=google")),
    # --- websites are websites, not searches ---
    ("open youtube", ("open_site", "youtube")),
    ("open google", ("open_site", "google")),
    ("open github", ("open_site", "github")),
    ("go to youtube", ("open_site", "youtube")),
    # --- YouTube play ---
    ("play the first one", ("play", 1)),
    ("play the second one", ("play", 2)),
    ("play the third result", ("play", 3)),
    ("play number 4", ("play", 4)),
    ("play it", ("play", 1)),
    # --- volume ---
    ("set volume to 80 percent", "volume"), ("set volume to 40%", "volume"),
    ("increase volume", "volume"), ("decrease volume", "volume"), ("mute", "volume"),
    ("unmute", "volume"), ("set volume to maximum", "volume"), ("set volume to minimum", "volume"),
    ("set volume", "volume"), ("turn the volume down", "volume"),
    # --- files ---
    ("open downloads", ("open_folder", "downloads")),
    ("open my downloads folder", ("open_folder", "downloads")),
    ("open desktop", ("open_folder", "desktop")),
    ("open the latest pdf", "latest_pdf"),
    ("find my file called resume", ("find_file", "resume")),
    ("create a folder named test", ("create", "folder", "test")),
    ("create a new folder called notes in documents", ("create", "folder", "notes", "documents")),
    ("create a file named todo", ("create", "file", "todo")),
    ("open music folder", ("open_folder", "music")),
    ("open music", ("open_app", "music")),
    # --- utilities ---
    ("what is my battery level", "battery"),
    ("check battery", "battery"),
    ("what's the weather in london", ("weather", "london")),
    ("remember that my battery is old", "remember"),   # must not hit the battery handler
    ("what do you remember", "recall"),
    # --- YouTube video controls (never the LLM, never system volume) ---
    ("pause the video", ("video", "pause")), ("pause playback", ("video", "pause")),
    ("pause", ("video", "pause")), ("stop the video", ("video", "pause")),
    ("resume playback", ("video", "play")), ("play the video", ("video", "play")),
    ("resume the video", ("video", "play")), ("resume", ("video", "play")),
    ("mute the video", ("video", "mute")), ("unmute the video", ("video", "unmute")),
    ("mute the youtube video", ("video", "mute")),
    # ...and they must not be confused with search-result commands or system volume
    ("play the first video", ("play", 1)), ("play the first one", ("play", 1)),
    ("play video", ("video", "play")), ("play despacito", ("search", "despacito", "site=youtube")),
    # --- Chrome tab navigation ---
    ("switch to the next tab", ("tab_switch", "next", "None")),
    ("switch to the previous tab", ("tab_switch", "previous", "None")),
    ("switch to tab 2", ("tab_switch", "index", 2)), ("switch to tab two", ("tab_switch", "index", 2)),
    ("switch to the first tab", ("tab_switch", "index", 1)),
    ("go to the last tab", ("tab_switch", "last", "None")),
    ("next tab", ("tab_switch", "next", "None")), ("tab 3", ("tab_switch", "index", 3)),
    ("switch to tab 9", ("tab_switch", "index", 9)),
    ("go back", ("nav", "back")), ("go forward", ("nav", "forward")),
    ("refresh the page", ("nav", "reload")), ("reload", ("nav", "reload")),
    ("close current tab", "close_tab"),
    # --- macOS window controls ---
    ("minimize the current window", ("window", "minimize", "None")),
    ("minimise the window", ("window", "minimize", "None")),
    ("minimize chrome", ("window", "minimize", "chrome")),
    ("restore the window", ("window", "restore", "None")),
    ("maximize the window", ("window", "maximize", "None")),
    ("maximize chrome", ("window", "maximize", "chrome")),
    ("close the chrome window", ("window", "close", "chrome")),
    ("close chrome", ("close_app", "chrome")),            # quitting the app is a different command
    # --- screenshots ---
    ("take a screenshot", "screenshot"), ("capture my screen", "screenshot"), ("screenshot", "screenshot"),
    ("take screenshot", "screenshot"), ("take a screenshot of my screen", "screenshot"),
    # --- conversation falls through to Ollama ---
    ("tell me a joke", "OLLAMA"),
    ("what is love", "OLLAMA"),
    ("who is the president of france", "OLLAMA"),
    ("how are you", "OLLAMA"),
]


class RoutingTests(unittest.TestCase):
    def setUp(self):
        jarvis.reset_session_context()
        self.rec = Recorder()
        self.rec.install(self)

    def first_event(self, transcript):
        result = jarvis.process_command(transcript)
        events = [e for e in self.rec.events if e[0] != "speak"]
        return result, (events[0] if events else None)

    def test_routing_table(self):
        failures = []
        for transcript, expected in ROUTING_CASES:
            self.rec.events.clear()
            jarvis.reset_session_context()
            result, event = self.first_event(transcript)
            if expected in ("shutdown", "sleep"):
                ok = result == expected
            elif isinstance(expected, str):
                ok = event is not None and event[0] == expected
            else:
                ok = event is not None and all(str(x) in [str(y) for y in event] for x in expected)
            if not ok:
                failures.append((transcript, expected, result, event))
        for item in failures:
            print("FAIL:", item)
        self.assertFalse(failures, f"{len(failures)} routing failures")

    def test_search_never_reaches_ollama(self):
        for text in ("search love yourself", "search for alfaaz", "search youtube for love yourself",
                     "google python tutorials", "open youtube and search love yourself"):
            self.rec.events.clear()
            jarvis.process_command(text)
            self.assertNotIn("OLLAMA", [e[0] for e in self.rec.events], text)

    def test_open_youtube_and_search_has_exactly_one_step(self):
        jarvis.process_command("open youtube and search love yourself")
        actions = [e for e in self.rec.events if e[0] != "speak"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0], ("search", "love yourself", "site=youtube"))

    def test_compound_commands_split_into_requested_steps_only(self):
        self.assertEqual(jarvis.split_compound("open calculator and set volume to 40"),
                         ["open calculator", "set volume to 40"])
        self.assertEqual(jarvis.split_compound("open notes then open spotify and mute"),
                         ["open notes", "open spotify", "mute"])
        self.assertEqual(jarvis.split_compound("search tom and jerry"), ["search tom and jerry"])
        self.assertEqual(jarvis.split_compound("what is rock and roll"), ["what is rock and roll"])
        jarvis.process_command("open calculator and set volume to 40")
        names = [e[0] for e in self.rec.events if e[0] not in ("speak",)]
        self.assertEqual(names, ["open_app", "volume"])

    def test_multi_step_stops_after_failure(self):
        with mock.patch.object(jarvis, "open_app", lambda key: False):
            jarvis.process_command("open calculator and set volume to 40")
        names = [e[0] for e in self.rec.events if e[0] != "speak"]
        self.assertNotIn("volume", names)

    def test_bare_search_follows_open_youtube_hint(self):
        jarvis.session["site_hint"] = "youtube"
        self.rec.events.clear()
        jarvis.process_command("search alfaaz")
        self.assertIn(("search", "alfaaz", "site=youtube"), self.rec.events)

    def test_followup_uses_previous_subject_and_site(self):
        jarvis.session.update({"subject": "arijit singh", "site": "youtube"})
        jarvis.process_command("search his latest song")
        self.assertIn(("search", "arijit singh latest song", "site=youtube"), self.rec.events)

    def test_unknown_open_target_is_not_a_search_and_not_ollama(self):
        self.rec.events.clear()
        result = jarvis.process_command("open flibbertigibbet")
        names = [e[0] for e in self.rec.events]
        self.assertNotIn("search", names)
        self.assertNotIn("OLLAMA", names)

    def test_mac_shutdown_requires_confirmation(self):
        calls = []
        with mock.patch.object(jarvis, "confirm", lambda q: False), \
                mock.patch.object(jarvis, "run_osascript", lambda *a, **k: calls.append(a) or (True, "", "")):
            jarvis.process_command("shut down the mac")
        self.assertEqual(calls, [])


class VolumeParserTests(unittest.TestCase):
    CASES = [
        ("set volume to 80 percent", ("set", 80)), ("set volume to 40%", ("set", 40)),
        ("set the volume to 100", ("set", 100)), ("volume 25", ("set", 25)),
        ("set volume to eighty five percent", ("set", 85)),
        ("increase volume", ("relative", 10)), ("decrease volume", ("relative", -10)),
        ("turn the volume up", ("relative", 10)), ("turn the volume down", ("relative", -10)),
        ("lower the volume", ("relative", -10)), ("louder", ("relative", 10)), ("quieter", ("relative", -10)),
        ("increase volume by 20 percent", ("relative", 20)),
        ("turn the volume down to 20", ("set", 20)),
        ("mute", ("mute", None)), ("unmute", ("unmute", None)),
        ("set volume to maximum", ("set", 100)), ("maximum volume", ("set", 100)),
        ("set volume to minimum", ("set", 0)), ("volume to zero", ("set", 0)),
        ("set volume", ("status", None)), ("what is the volume", ("status", None)),
        ("set volume to 150", ("invalid", 150)),
    ]

    def test_parser(self):
        for text, expected in self.CASES:
            self.assertEqual(jarvis.parse_volume_command(text), expected, text)

    def test_set_volume_runs_real_osascript_and_verifies(self):
        scripts = []

        def fake_osascript(script, timeout=10):
            scripts.append(script)
            if script.startswith("output volume"):
                return True, "80", ""
            return True, "", ""

        said = []
        with mock.patch.object(jarvis, "run_osascript", fake_osascript), \
                mock.patch.object(jarvis, "speak", said.append):
            self.assertTrue(jarvis.control_volume("set volume to 80 percent"))
        self.assertTrue(any("set volume output volume 80" in s for s in scripts))
        self.assertEqual(said, ["Volume set to 80 percent."])

    def test_failed_volume_change_is_not_announced_as_success(self):
        said = []
        with mock.patch.object(jarvis, "run_osascript", lambda s, timeout=10: (False, "", "boom")), \
                mock.patch.object(jarvis, "speak", said.append):
            self.assertFalse(jarvis.control_volume("set volume to 80 percent"))
        self.assertNotIn("Volume set to 80 percent.", said)

    def test_search_mentioning_volume_is_not_a_volume_command(self):
        rec = Recorder()
        rec.install(self)
        jarvis.process_command("search volume knob replacement")
        names = [e[0] for e in rec.events if e[0] != "speak"]
        self.assertEqual(names[0], "search")


class AppControlTests(unittest.TestCase):
    def test_vscode_uses_bundle_identifier_not_display_name(self):
        self.assertEqual(jarvis.APPS["vscode"]["ids"], ["com.microsoft.VSCode"])
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(jarvis.subprocess, "run", fake_run), \
                mock.patch.object(jarvis, "frontmost_app", lambda: ("Code", "com.microsoft.VSCode")), \
                mock.patch.object(jarvis, "speak", lambda *_: None):
            self.assertTrue(jarvis.switch_to_app("vscode"))
        self.assertIn(["open", "-b", "com.microsoft.VSCode"], calls)

    def test_switch_not_claimed_when_app_does_not_come_to_front(self):
        said = []
        with mock.patch.object(jarvis.subprocess, "run",
                               lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr="")), \
                mock.patch.object(jarvis, "frontmost_app", lambda: ("Terminal", "com.apple.Terminal")), \
                mock.patch.object(jarvis, "wait_until", lambda p, timeout=0, interval=0: p()), \
                mock.patch.object(jarvis, "speak", said.append):
            self.assertFalse(jarvis.switch_to_app("vscode"))
        self.assertFalse(any(s.startswith("Switched") for s in said))

    def test_open_failure_is_reported_honestly(self):
        said = []
        with mock.patch.object(jarvis.subprocess, "run",
                               lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="nope")), \
                mock.patch.object(jarvis, "speak", said.append):
            self.assertFalse(jarvis.open_app("spotify"))
        self.assertFalse(any(s.startswith("Opened") for s in said))

    def test_aliases_are_exact_not_substring(self):
        self.assertIsNone(jarvis.resolve_app("chrome tab"))
        self.assertEqual(jarvis.resolve_app("the Google Chrome app"), "chrome")
        self.assertEqual(jarvis.resolve_app("VS Code"), "vscode")

    def test_close_current_app_quits_the_frontmost_app(self):
        quits = []
        with mock.patch.object(jarvis, "frontmost_app", lambda: ("Notes", "com.apple.Notes")), \
                mock.patch.object(jarvis, "quit_bundle", lambda b, n: quits.append((b, n)) or True):
            self.assertTrue(jarvis.close_current_app())
        self.assertEqual(quits, [("com.apple.Notes", "Notes")])

    def test_finder_cannot_be_quit(self):
        with mock.patch.object(jarvis, "frontmost_app", lambda: ("Finder", "com.apple.finder")), \
                mock.patch.object(jarvis, "quit_bundle", lambda *a: self.fail("must not quit Finder")), \
                mock.patch.object(jarvis, "speak", lambda *_: None):
            self.assertFalse(jarvis.close_current_app())


class ChromeTests(unittest.TestCase):
    def test_tab_info_parsing(self):
        said = []
        with mock.patch.object(jarvis, "get_chrome_tab", lambda: {
            "status": "ok", "title": "Alfaaz - YouTube", "url": "https://www.youtube.com/watch?v=abc"}), \
                mock.patch.object(jarvis, "speak", said.append):
            self.assertTrue(jarvis.report_current_tab())
        self.assertIn("Alfaaz - YouTube", said[0])
        self.assertIn("youtube.com/watch?v=abc", said[0])

    def test_tab_info_when_chrome_not_running(self):
        said = []
        with mock.patch.object(jarvis, "get_chrome_tab", lambda: {"status": "not_running"}), \
                mock.patch.object(jarvis, "speak", said.append):
            self.assertFalse(jarvis.report_current_tab())
        self.assertIn("isn't running", said[0])

    def test_search_urls(self):
        self.assertEqual(jarvis.search_url("love yourself", "youtube"),
                         "https://www.youtube.com/results?search_query=love%20yourself")
        self.assertEqual(jarvis.search_url("python tutorials", "google"),
                         "https://www.google.com/search?q=python%20tutorials")

    def test_applescript_quote(self):
        self.assertEqual(jarvis.applescript_quote('a"b\\c'), 'a\\"b\\\\c')

    def test_browser_failure_is_not_announced_as_success(self):
        said = []
        with mock.patch.object(jarvis, "chrome_open_url", lambda url: False), \
                mock.patch.object(jarvis, "speak", said.append):
            self.assertFalse(jarvis.browser_search("alfaaz", "youtube"))
        self.assertEqual(said, [])
        self.assertEqual(jarvis.session["subject"], "")


class CalculatorTests(unittest.TestCase):
    def run_calc(self, text):
        said = []
        with mock.patch.object(jarvis, "speak", said.append):
            result = jarvis.handle_calculation(text)
        return result, said

    def test_calculations(self):
        self.assertEqual(self.run_calc("calculate 10 plus 5")[1], ["The answer is 15"])
        self.assertEqual(self.run_calc("what is 6 times 7")[1], ["The answer is 42"])
        self.assertEqual(self.run_calc("what is 2 to the power of 3")[1], ["The answer is 8"])
        self.assertEqual(self.run_calc("what is 10 divided by 4")[1], ["The answer is 2.5"])

    def test_non_math_is_left_for_ollama(self):
        self.assertIsNone(self.run_calc("what is love")[0])

    def test_unsafe_expressions_rejected(self):
        self.assertFalse(self.run_calc("calculate __import__('os')")[0])
        self.assertFalse(self.run_calc("calculate 9 to the power of 99")[0])
        self.assertFalse(self.run_calc("calculate 1 divided by 0")[0])


class PlannerValidationTests(unittest.TestCase):
    def test_hallucinated_steps_are_rejected(self):
        original = "open youtube and search love yourself"
        # model invents a 'play' step and a new tab the user never asked for
        self.assertIsNone(jarvis.validate_planned_actions(
            ["open youtube", "search youtube for love yourself", "play love yourself", "open a new tab"], original))

    def test_grounded_plan_is_accepted(self):
        original = "open calculator and then open notes"
        self.assertEqual(jarvis.validate_planned_actions(["open calculator", "open notes"], original),
                         ["open calculator", "open notes"])

    def test_dangerous_actions_rejected(self):
        self.assertIsNone(jarvis.validate_planned_actions(
            ["open calculator", "rm -rf /"], "open calculator and delete everything"))


class SafetyTests(unittest.TestCase):
    def test_risky_files_are_revealed_not_executed(self):
        calls = []
        with mock.patch.object(jarvis.subprocess, "run",
                               lambda args, **k: calls.append(args) or types.SimpleNamespace(
                                   returncode=0, stdout="", stderr="")), \
                mock.patch.object(jarvis, "speak", lambda *_: None):
            jarvis.open_path("/Users/x/Downloads/run.command", "run.command")
        self.assertEqual(calls[0][:2], ["open", "-R"])

    def test_create_item_never_overwrites(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(jarvis.FOLDER_ALIASES, {"desktop": tmp}), \
                mock.patch.object(jarvis.subprocess, "run", lambda *a, **k: None), \
                mock.patch.object(jarvis, "speak", lambda *_: None):
            self.assertTrue(jarvis.create_item("file", "todo", "desktop"))
            self.assertTrue(os.path.exists(os.path.join(tmp, "todo.txt")))
            self.assertFalse(jarvis.create_item("file", "todo", "desktop"))  # already exists
            self.assertFalse(jarvis.create_item("folder", "../evil", "desktop"))



# ---------------------------------------------------------------------------
# New: YouTube video controls (all Chrome/JS access mocked)
# ---------------------------------------------------------------------------
def yt_tab(path="/watch?v=abc", host="www.youtube.com"):
    return {"status": "ok", "title": "Song - YouTube", "url": f"https://{host}{path}", "index": 1, "count": 3}


class VideoControlTests(unittest.TestCase):
    def run_video(self, action, tab, js_results):
        """js_results: list of (ok, out, err) returned by successive chrome_js calls."""
        said, scripts = [], []
        calls = iter(js_results)

        def fake_js(js):
            scripts.append(js)
            return next(calls)

        with mock.patch.object(jarvis, "get_chrome_tab", lambda: tab), \
                mock.patch.object(jarvis, "chrome_js", fake_js), \
                mock.patch.object(jarvis, "wait_until", lambda p, timeout=0, interval=0: p()), \
                mock.patch.object(jarvis, "chrome_failed", lambda err: said.append(f"CHROME_FAILED:{err}") or False), \
                mock.patch.object(jarvis, "speak", said.append):
            result = jarvis.control_video(action)
        return result, said, scripts

    def test_pause_verified(self):
        result, said, scripts = self.run_video("pause", yt_tab(), [(True, '{"paused":true,"muted":false,"t":5}', "")])
        self.assertTrue(result)
        self.assertEqual(said, ["Paused the video."])
        self.assertIn("'pause'", scripts[0])

    def test_resume_verified(self):
        result, said, _ = self.run_video("play", yt_tab(), [(True, '{"paused":false,"muted":false,"t":5}', "")])
        self.assertTrue(result)
        self.assertEqual(said, ["Resumed playback."])

    def test_mute_and_unmute(self):
        self.assertTrue(self.run_video("mute", yt_tab(), [(True, '{"paused":false,"muted":true,"t":1}', "")])[0])
        self.assertTrue(self.run_video("unmute", yt_tab(), [(True, '{"paused":false,"muted":false,"t":1}', "")])[0])

    def test_resume_not_claimed_when_video_stays_paused(self):
        paused = (True, '{"paused":true,"muted":false,"t":5}', "")
        result, said, _ = self.run_video("play", yt_tab(), [paused, paused])
        self.assertFalse(result)
        self.assertNotIn("Resumed playback.", said)
        self.assertIn("still paused", said[0])

    def test_pause_not_claimed_when_state_unchanged(self):
        playing = (True, '{"paused":false,"muted":false,"t":5}', "")
        result, said, _ = self.run_video("pause", yt_tab(), [playing, playing])
        self.assertFalse(result)
        self.assertNotIn("Paused the video.", said)

    def test_chrome_not_running(self):
        result, said, _ = self.run_video("pause", {"status": "not_running"}, [])
        self.assertFalse(result)
        self.assertIn("Chrome isn't running", said[0])

    def test_no_window(self):
        result, said, _ = self.run_video("pause", {"status": "no_window"}, [])
        self.assertFalse(result)
        self.assertIn("no windows", said[0])

    def test_active_tab_not_youtube(self):
        result, said, scripts = self.run_video("pause", yt_tab("/", "example.com"), [])
        self.assertFalse(result)
        self.assertIn("isn't YouTube", said[0])
        self.assertEqual(scripts, [])           # no JavaScript was run on a non-YouTube page

    def test_lookalike_domain_is_not_youtube(self):
        result, said, scripts = self.run_video("pause", yt_tab("/watch?v=1", "notyoutube.com.evil.example"), [])
        self.assertFalse(result)
        self.assertEqual(scripts, [])

    def test_youtube_page_without_video(self):
        result, said, scripts = self.run_video("pause", yt_tab("/results?search_query=x"), [])
        self.assertFalse(result)
        self.assertIn("no video playing", said[0])
        self.assertEqual(scripts, [])

    def test_no_player_found(self):
        result, said, _ = self.run_video("pause", yt_tab(), [(True, "none", "")])
        self.assertFalse(result)
        self.assertIn("couldn't find a video player", said[0])

    def test_javascript_permission_error_is_explained(self):
        err = "Executing JavaScript through AppleScript is turned off."
        result, said, _ = self.run_video("pause", yt_tab(), [(False, "", err)])
        self.assertFalse(result)
        self.assertTrue(said[0].startswith("CHROME_FAILED"))
        self.assertIn("Allow JavaScript from Apple Events", jarvis.chrome_error_message(err))

    def test_garbage_output_is_handled(self):
        result, said, _ = self.run_video("pause", yt_tab(), [(True, "<<not json>>", "")])
        self.assertFalse(result)

    def test_video_js_only_touches_the_video_element(self):
        js = jarvis.VIDEO_JS.replace("ACTION", "pause")
        self.assertIn("'pause'", js)
        self.assertNotIn("ACTION", js)
        self.assertNotIn('"', js)               # safe to embed in an AppleScript string

    def test_bare_mute_is_still_system_volume(self):
        self.assertIsNone(jarvis.handle_video("mute"))
        self.assertIsNone(jarvis.handle_video("unmute"))


# ---------------------------------------------------------------------------
# New: Chrome tab navigation
# ---------------------------------------------------------------------------
class TabNavigationTests(unittest.TestCase):
    def test_parse_tab_target(self):
        cases = {
            "switch to the next tab": ("next", None), "next tab": ("next", None),
            "switch to the previous tab": ("previous", None), "previous tab": ("previous", None),
            "switch to tab 2": ("index", 2), "go to tab 4": ("index", 4), "tab 7": ("index", 7),
            "switch to tab number 3": ("index", 3), "switch to tab five": ("index", 5),
            "switch to the first tab": ("index", 1), "switch to the 2nd tab": ("index", 2),
            "go to the last tab": ("last", None), "switch to tab 0": ("index", 0),
            "switch to tab banana": ("invalid", None),
        }
        for text, expected in cases.items():
            self.assertEqual(jarvis.parse_tab_target(text), expected, text)

    def test_not_tab_commands(self):
        for text in ("open a new tab", "close the current tab", "what is the current tab", "new tab",
                     "switch to chrome", "open tab"):
            self.assertIsNone(jarvis.parse_tab_target(text), text)

    def switch(self, kind, number, before, after_index=None):
        said, scripts = [], []
        states = iter([before, dict(before, index=after_index if after_index else before["index"])])

        with mock.patch.object(jarvis, "get_chrome_tab", lambda: next(states)), \
                mock.patch.object(jarvis, "run_osascript",
                                  lambda script, timeout=10: scripts.append(script) or (True, "OK", "")), \
                mock.patch.object(jarvis, "speak", said.append):
            return jarvis.switch_tab(kind, number), said, scripts

    def tab(self, index, count):
        return {"status": "ok", "title": "T", "url": "https://x.test", "index": index, "count": count}

    def test_next_wraps_around(self):
        result, said, scripts = self.switch("next", None, self.tab(5, 5), after_index=1)
        self.assertTrue(result)
        self.assertIn("set active tab index of front window to 1", scripts[0])

    def test_previous_wraps_around(self):
        result, _, scripts = self.switch("previous", None, self.tab(1, 5), after_index=5)
        self.assertTrue(result)
        self.assertIn("to 5", scripts[0])

    def test_numbered_tab(self):
        result, said, scripts = self.switch("index", 2, self.tab(1, 5), after_index=2)
        self.assertTrue(result)
        self.assertIn("Switched to tab 2 of 5", said[0])

    def test_invalid_numbers_are_explained_and_nothing_is_sent(self):
        for number, fragment in ((9, "only has 3 tabs"), (0, "start at 1"), (-1, "start at 1")):
            said, scripts = [], []
            with mock.patch.object(jarvis, "get_chrome_tab", lambda: self.tab(1, 3)), \
                    mock.patch.object(jarvis, "run_osascript", lambda *a, **k: scripts.append(a) or (True, "", "")), \
                    mock.patch.object(jarvis, "speak", said.append):
                self.assertFalse(jarvis.switch_tab("index", number))
            self.assertIn(fragment, said[0])
            self.assertEqual(scripts, [])

    def test_single_tab_next_is_not_an_error_and_sends_nothing(self):
        result, said, scripts = self.switch("next", None, self.tab(1, 1))
        self.assertTrue(result)
        self.assertEqual(scripts, [])
        self.assertIn("one tab", said[0])

    def test_switch_not_claimed_when_active_tab_did_not_change(self):
        result, said, _ = self.switch("index", 3, self.tab(1, 5), after_index=1)
        self.assertFalse(result)
        self.assertFalse(any(s.startswith("Switched") for s in said))

    def test_chrome_not_running(self):
        said = []
        with mock.patch.object(jarvis, "get_chrome_tab", lambda: {"status": "not_running"}), \
                mock.patch.object(jarvis, "speak", said.append):
            self.assertFalse(jarvis.switch_tab("next"))
        self.assertIn("isn't running", said[0])

    def test_invalid_tab_word(self):
        said = []
        with mock.patch.object(jarvis, "speak", said.append):
            self.assertFalse(jarvis.switch_tab("invalid"))
        self.assertIn("Which tab number", said[0])

    def test_navigation_scripts_and_verification(self):
        said, scripts = [], []
        urls = iter([{"status": "ok", "url": "https://a.test", "title": "", "index": 1, "count": 1},
                     {"status": "ok", "url": "https://b.test", "title": "", "index": 1, "count": 1}])
        with mock.patch.object(jarvis, "get_chrome_tab", lambda: next(urls)), \
                mock.patch.object(jarvis, "run_osascript",
                                  lambda script, timeout=10: scripts.append(script) or (True, "OK", "")), \
                mock.patch.object(jarvis, "wait_until", lambda p, timeout=0, interval=0: p()), \
                mock.patch.object(jarvis, "speak", said.append):
            self.assertTrue(jarvis.chrome_navigate("back"))
        self.assertIn("go back", scripts[0])
        self.assertEqual(said, ["Went back."])

    def test_back_not_claimed_when_page_unchanged(self):
        said = []
        same = {"status": "ok", "url": "https://a.test", "title": "", "index": 1, "count": 1}
        with mock.patch.object(jarvis, "get_chrome_tab", lambda: same), \
                mock.patch.object(jarvis, "run_osascript", lambda *a, **k: (True, "OK", "")), \
                mock.patch.object(jarvis, "wait_until", lambda p, timeout=0, interval=0: p()), \
                mock.patch.object(jarvis, "speak", said.append):
            self.assertFalse(jarvis.chrome_navigate("forward"))
        self.assertNotIn("Went forward.", said)

    def test_refresh_uses_reload(self):
        scripts = []
        with mock.patch.object(jarvis, "get_chrome_tab", lambda: self.tab(1, 1)), \
                mock.patch.object(jarvis, "run_osascript",
                                  lambda script, timeout=10: scripts.append(script) or (True, "OK", "")), \
                mock.patch.object(jarvis, "speak", lambda *_: None):
            self.assertTrue(jarvis.chrome_navigate("reload"))
        self.assertIn("reload", scripts[0])

    def test_current_tab_report_includes_position(self):
        said = []
        with mock.patch.object(jarvis, "get_chrome_tab", lambda: {
            "status": "ok", "title": "Docs", "url": "https://docs.test/a", "index": 2, "count": 4}), \
                mock.patch.object(jarvis, "speak", said.append):
            self.assertTrue(jarvis.report_current_tab())
        self.assertIn("Tab 2 of 4", said[0])

    def test_get_chrome_tab_parses_index_and_count(self):
        with mock.patch.object(jarvis, "is_running", lambda b: True), \
                mock.patch.object(jarvis, "run_osascript",
                                  lambda *a, **k: (True, "My | Title|||https://x.test/a?b=1|||3|||7", "")):
            tab = jarvis.get_chrome_tab()
        self.assertEqual((tab["title"], tab["url"], tab["index"], tab["count"]),
                         ("My | Title", "https://x.test/a?b=1", 3, 7))


# ---------------------------------------------------------------------------
# New: window controls
# ---------------------------------------------------------------------------
class WindowControlTests(unittest.TestCase):
    def setUp(self):
        jarvis.window_state["stack"].clear()
        jarvis.window_state["bounds"].clear()

    def test_parse_window_command(self):
        cases = {
            "minimize the current window": ("minimize", None), "minimize": ("minimize", None),
            "minimize chrome": ("minimize", "chrome"), "minimize the chrome window": ("minimize", "chrome"),
            "restore the window": ("restore", None), "maximize the window": ("maximize", None),
            "maximise chrome": ("maximize", "chrome"), "close the current window": ("close", None),
            "close this window": ("close", None), "close the chrome window": ("close", "chrome"),
        }
        for text, expected in cases.items():
            self.assertEqual(jarvis.parse_window_command(text), expected, text)

    def test_things_that_are_not_window_commands(self):
        for text in ("close chrome", "close calculator", "close the current tab", "close current tab",
                     "restore the volume", "minimize the volume", "close jarvis", "quit chrome"):
            self.assertIsNone(jarvis.parse_window_command(text), text)

    def run_action(self, action, key=None, front=("Notes", "com.apple.Notes"), osascript=None, area=(0, 37, 1440, 800),
                   confirm_answer=True):
        said, scripts = [], []

        def fake_osascript(script, timeout=10):
            scripts.append(script)
            return osascript(script) if osascript else (True, "OK", "")

        with mock.patch.object(jarvis, "frontmost_app", lambda: front), \
                mock.patch.object(jarvis, "running_bundle_ids", lambda: {"com.google.Chrome", "com.apple.Notes"}), \
                mock.patch.object(jarvis, "screen_work_area", lambda: area), \
                mock.patch.object(jarvis, "run_osascript", fake_osascript), \
                mock.patch.object(jarvis, "confirm", lambda q: confirm_answer), \
                mock.patch.object(jarvis, "chrome_close_window", lambda: said.append("CHROME_CLOSE") or True), \
                mock.patch.object(jarvis, "speak", said.append):
            result = jarvis.window_action(action, key)
        return result, said, scripts

    def test_minimize_active_app_via_system_events(self):
        result, said, scripts = self.run_action("minimize", osascript=lambda s: (True, "true", ""))
        self.assertTrue(result)
        self.assertIn('bundle identifier is "com.apple.Notes"', scripts[0])
        self.assertIn("AXMinimized", scripts[0])
        self.assertEqual(said, ["Minimized Notes."])
        self.assertEqual(jarvis.window_state["stack"], [("Notes", "com.apple.Notes")])

    def test_minimize_not_claimed_if_window_did_not_minimize(self):
        result, said, _ = self.run_action("minimize", osascript=lambda s: (True, "false", ""))
        self.assertFalse(result)
        self.assertFalse(any(s.startswith("Minimized") for s in said))
        self.assertEqual(jarvis.window_state["stack"], [])

    def test_minimize_chrome_uses_chrome_dictionary_not_accessibility(self):
        result, _, scripts = self.run_action("minimize", "chrome", osascript=lambda s: (True, "true", ""))
        self.assertTrue(result)
        self.assertIn('application id "com.google.Chrome"', scripts[0])
        self.assertNotIn("System Events", scripts[0])

    def test_restore_prefers_the_window_jarvis_minimized(self):
        jarvis.window_state["stack"].append(("Notes", "com.apple.Notes"))
        # frontmost app is now something else (macOS moves focus after minimizing)
        result, said, scripts = self.run_action("restore", front=("Finder", "com.apple.finder"),
                                                osascript=lambda s: (True, "1", ""))
        self.assertTrue(result)
        self.assertIn("com.apple.Notes", scripts[0])
        self.assertEqual(said, ["Restored the Notes window."])
        self.assertEqual(jarvis.window_state["stack"], [])

    def test_restore_with_nothing_to_restore_says_so(self):
        result, said, _ = self.run_action("restore", osascript=lambda s: (True, "0", ""))
        self.assertFalse(result)
        self.assertIn("no minimized or maximized window", said[0])

    def test_maximize_uses_work_area_and_remembers_old_size(self):
        result, said, scripts = self.run_action("maximize", osascript=lambda s: (True, "OLD:100,120,640,480", ""))
        self.assertTrue(result)
        self.assertIn("{0, 37}", scripts[0])
        self.assertIn("{1440, 800}", scripts[0])
        self.assertEqual(jarvis.window_state["bounds"]["com.apple.Notes"], (100, 120, 640, 480))

    def test_maximize_fails_cleanly_without_screen_size(self):
        result, said, scripts = self.run_action("maximize", area=None)
        self.assertFalse(result)
        self.assertEqual(scripts, [])

    def test_close_window_clicks_close_button_and_never_quits(self):
        result, said, scripts = self.run_action("close", osascript=lambda s: (True, "OK", ""))
        self.assertTrue(result)
        self.assertIn("AXCloseButton", scripts[0])
        self.assertEqual(said, ["Closed the Notes window."])

    def test_close_window_in_chrome_uses_chrome_close(self):
        result, said, scripts = self.run_action("close", front=("Google Chrome", "com.google.Chrome"))
        self.assertTrue(result)
        self.assertEqual(said, ["CHROME_CLOSE"])
        self.assertEqual(scripts, [])

    def test_no_window_script_ever_quits_an_app(self):
        for action in ("minimize", "maximize", "restore", "close"):
            for bundle in ("com.apple.Notes", "com.google.Chrome"):
                script = jarvis._window_scripts(action, bundle, area=(0, 37, 1440, 800))
                lowered = script.lower()
                self.assertNotIn("quit", lowered, (action, bundle))
                self.assertNotIn("terminate", lowered, (action, bundle))
                self.assertNotIn("kill", lowered, (action, bundle))

    def test_closing_the_terminal_window_needs_confirmation(self):
        result, said, scripts = self.run_action("close", front=("Terminal", "com.apple.Terminal"),
                                                confirm_answer=False)
        self.assertFalse(result)
        self.assertEqual(scripts, [])

    def test_accessibility_error_is_explained(self):
        err = "execution error: System Events got an error: osascript is not allowed assistive access. (-25211)"
        result, said, _ = self.run_action("minimize", osascript=lambda s: (False, "", err))
        self.assertFalse(result)
        self.assertIn("Accessibility", said[0])

    def test_named_app_not_running(self):
        said = []
        with mock.patch.object(jarvis, "running_bundle_ids", lambda: {"com.apple.Notes"}), \
                mock.patch.object(jarvis, "speak", said.append):
            self.assertFalse(jarvis.window_action("minimize", "spotify"))
        self.assertIn("isn't running", said[0])

    def test_app_without_windows(self):
        result, said, _ = self.run_action("minimize", osascript=lambda s: (True, "NOWINDOW", ""))
        self.assertFalse(result)
        self.assertIn("no open windows", said[0])


# ---------------------------------------------------------------------------
# New: screenshots
# ---------------------------------------------------------------------------
class ScreenshotTests(unittest.TestCase):
    def test_unique_paths_never_collide(self):
        import datetime
        import os
        import tempfile
        stamp = datetime.datetime(2026, 5, 1, 9, 30, 15)
        with tempfile.TemporaryDirectory() as tmp:
            first = jarvis.unique_screenshot_path(tmp, stamp)
            open(first, "w").close()
            second = jarvis.unique_screenshot_path(tmp, stamp)
            open(second, "w").close()
            third = jarvis.unique_screenshot_path(tmp, stamp)
            self.assertEqual(len({first, second, third}), 3)
            self.assertTrue(os.path.basename(first).startswith("jarvis-screenshot-20260501-093015"))

    def run_capture(self, tmp, make_file=True, returncode=0, stderr=""):
        import os
        said, calls = [], []

        def fake_run(args, **kwargs):
            calls.append(args)
            if make_file and returncode == 0:
                with open(args[-1], "wb") as handle:
                    handle.write(b"\x89PNG fake")
            return types.SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

        with mock.patch.object(jarvis, "SCREENSHOT_DIR", os.path.join(tmp, "Pictures", "JARVIS-Screenshots")), \
                mock.patch.object(jarvis.subprocess, "run", fake_run), \
                mock.patch.object(jarvis, "screen_recording_allowed", lambda: True), \
                mock.patch.object(jarvis, "speak", said.append):
            return jarvis.take_screenshot(), said, calls

    def test_successful_capture(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result, said, calls = self.run_capture(tmp)
            self.assertTrue(result)
            self.assertEqual(calls[0][:3], ["screencapture", "-x", "-t"])
            self.assertTrue(os.path.isfile(calls[0][-1]))
            self.assertIn("JARVIS-Screenshots", calls[0][-1])
            self.assertIn(os.path.basename(calls[0][-1]), said[0])

    def test_two_captures_in_one_second_do_not_overwrite(self):
        import datetime
        import tempfile
        fixed = datetime.datetime(2026, 5, 1, 9, 30, 15)

        class FixedDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(jarvis.datetime, "datetime", FixedDatetime):
            _, _, first = self.run_capture(tmp)
            _, _, second = self.run_capture(tmp)
        self.assertNotEqual(first[0][-1], second[0][-1])

    def test_failure_is_reported_and_not_claimed_as_saved(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result, said, _ = self.run_capture(tmp, returncode=1, stderr="could not create image from display")
            self.assertFalse(result)
            self.assertIn("Screen Recording", said[0])
            self.assertFalse(any("saved" in s.lower() for s in said))

    def test_empty_output_file_counts_as_failure(self):
        import os
        import tempfile
        said = []

        def fake_run(args, **kwargs):
            open(args[-1], "wb").close()            # zero-byte file
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(jarvis, "SCREENSHOT_DIR", os.path.join(tmp, "s")), \
                mock.patch.object(jarvis.subprocess, "run", fake_run), \
                mock.patch.object(jarvis, "speak", said.append):
            self.assertFalse(jarvis.take_screenshot())

    def test_folder_permission_error(self):
        said = []
        with mock.patch.object(jarvis.os, "makedirs", side_effect=PermissionError), \
                mock.patch.object(jarvis, "speak", said.append):
            self.assertFalse(jarvis.take_screenshot())
        self.assertIn("denied", said[0])

    def test_screenshot_phrases(self):
        for text in ("take a screenshot", "capture my screen", "screenshot", "take screenshot",
                     "take a screen shot", "grab a screenshot", "capture the screen"):
            self.assertTrue(any(p.match(text) for p in jarvis.SCREENSHOT_RES), text)
        for text in ("take a break", "screen time", "capture the flag", "take a nap"):
            self.assertFalse(any(p.match(text) for p in jarvis.SCREENSHOT_RES), text)


# ---------------------------------------------------------------------------
# New: routing priorities, compound commands, invalid commands
# ---------------------------------------------------------------------------
class RoutingPriorityTests(unittest.TestCase):
    def setUp(self):
        jarvis.reset_session_context()
        self.rec = Recorder()
        self.rec.install(self)

    def names(self, text):
        self.rec.events.clear()
        jarvis.process_command(text)
        return [e for e in self.rec.events if e[0] != "speak"]

    def test_deterministic_commands_never_reach_ollama(self):
        for text in ("pause the video", "resume playback", "mute the video", "switch to the next tab",
                     "go back", "refresh the page", "minimize chrome", "restore the window",
                     "take a screenshot", "set volume to 50", "open spotify", "close the current tab",
                     "maximize the window", "what is the current tab"):
            self.assertNotIn("OLLAMA", [e[0] for e in self.names(text)], text)

    def test_mute_the_video_is_not_system_volume_but_plain_mute_is(self):
        self.assertEqual(self.names("mute the video")[0][:2], ("video", "mute"))
        self.assertEqual(self.names("mute")[0][0], "volume")

    def test_play_the_first_video_vs_resume_playback(self):
        self.assertEqual(self.names("play the first video")[0], ("play", 1))
        self.assertEqual(self.names("resume playback")[0][:2], ("video", "play"))
        self.assertEqual(self.names("play the video")[0][:2], ("video", "play"))

    def test_compound_open_youtube_and_search(self):
        events = self.names("Open YouTube and search for love yourself.")
        self.assertEqual(events, [("search", "love yourself", "site=youtube")])

    def test_compound_with_new_commands(self):
        events = self.names("minimize chrome and take a screenshot")
        self.assertEqual([e[0] for e in events], ["window", "screenshot"])
        events = self.names("open a new tab and switch to tab 2")
        self.assertEqual([e[0] for e in events], ["new_tab", "tab_switch"])
        events = self.names("pause the video and mute the video")
        self.assertEqual([e[:2] for e in events], [("video", "pause"), ("video", "mute")])

    def test_compound_does_not_split_titles_containing_and(self):
        events = self.names("search youtube for tom and jerry")
        self.assertEqual(events, [("search", "tom and jerry", "site=youtube")])
        self.assertEqual(jarvis.split_compound("play black and white"), ["play black and white"])

    def test_failed_step_stops_compound(self):
        with mock.patch.object(jarvis, "take_screenshot", lambda: False), \
                mock.patch.object(jarvis, "window_action", lambda *a: self.rec.events.append(("window",)) or True):
            self.rec.events.clear()
            jarvis.process_command("take a screenshot and minimize chrome")
        self.assertNotIn(("window",), self.rec.events)

    def test_invalid_commands(self):
        # unknown "open X" is reported, never searched or sent to the LLM
        events = self.names("open flibbertigibbet")
        self.assertEqual(events, [])
        said = [e for e in self.rec.events if e[0] == "speak"]
        self.assertIn("don't have an app", said[-1][1])
        # nonsense that looks like an action
        self.rec.events.clear()
        jarvis.process_command("switch to the moon")
        self.assertNotIn("OLLAMA", [e[0] for e in self.rec.events])

    def test_empty_and_noise_commands_do_nothing(self):
        for text in ("", "   ", "...", "jarvis"):
            self.rec.events.clear()
            self.assertIsNone(jarvis.process_command(text))
            self.assertEqual([e for e in self.rec.events if e[0] != "speak"], [], repr(text))

    def test_handlers_never_swallow_each_other(self):
        # every spec phrase must be claimed by exactly the intended handler
        expected = {
            "pause the video": "handle_video", "resume playback": "handle_video",
            "switch to the next tab": "handle_browser", "go back": "handle_browser",
            "minimize the current window": "handle_window", "take a screenshot": "handle_screenshot",
            "set volume to 80 percent": "handle_volume", "open calculator": "handle_app",
            "search for alfaaz": "handle_generic_search", "play the first video": "handle_youtube",
        }
        for text, handler_name in expected.items():
            claimed = [h.__name__ for h in jarvis.HANDLERS
                       if self._claims(h, text)]
            self.assertEqual(claimed[0], handler_name, (text, claimed))

    def _claims(self, handler, text):
        with mock.patch.object(jarvis, "speak", lambda *_: None), \
                mock.patch.object(jarvis, "confirm", lambda q: True):
            patches = [mock.patch.object(jarvis, name, lambda *a, **k: True) for name in (
                "control_video", "switch_tab", "chrome_navigate", "window_action", "take_screenshot",
                "control_volume", "open_app", "browser_search", "youtube_play_result", "chrome_new_tab",
                "chrome_close_tab", "report_current_tab", "switch_to_app", "close_app", "close_current_app")]
            for p in patches:
                p.start()
            try:
                return handler(text) is not None
            finally:
                for p in patches:
                    p.stop()


# ---------------------------------------------------------------------------
# New: engine -> UI event bridge
# ---------------------------------------------------------------------------
class EventBridgeTests(unittest.TestCase):
    def capture(self, fn):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), mock.patch.object(jarvis, "EVENTS_ENABLED", True), \
                mock.patch.object(jarvis, "_events_broken", False):
            fn()
        events = []
        for line in buffer.getvalue().splitlines():
            if line.startswith(jarvis.EVENT_PREFIX):
                import json
                events.append(json.loads(line[len(jarvis.EVENT_PREFIX):]))
        return events

    def test_silent_when_not_launched_by_ui(self):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), mock.patch.object(jarvis, "EVENTS_ENABLED", False):
            jarvis.emit("state", state="LISTENING")
        self.assertEqual(buffer.getvalue(), "")

    def test_speak_reports_speaking_then_restores_previous_state(self):
        jarvis.engine_state = "THINKING"
        with mock.patch.object(jarvis.subprocess, "run", lambda *a, **k: None):
            events = self.capture(lambda: jarvis.speak("Hello there"))
        states = [e["state"] for e in events if e["event"] == "state"]
        self.assertEqual(states, ["SPEAKING", "THINKING"])
        self.assertIn({"event": "say", "text": "Hello there"}, events)

    def test_state_restored_even_if_say_fails(self):
        jarvis.engine_state = "LISTENING"

        def boom(*a, **k):
            raise OSError("say missing")

        with mock.patch.object(jarvis.subprocess, "run", boom):
            events = self.capture(lambda: jarvis.speak("x"))
        self.assertEqual([e["state"] for e in events if e["event"] == "state"][-1], "LISTENING")

    def test_route_event_names_the_handler(self):
        with mock.patch.object(jarvis, "handle_control", lambda c: None), \
                mock.patch.object(jarvis, "handle_power", lambda c: None), \
                mock.patch.object(jarvis, "handle_screenshot", lambda c: True), \
                mock.patch.object(jarvis, "HANDLERS", (jarvis.handle_control, jarvis.handle_power,
                                                       jarvis.handle_screenshot)):
            events = self.capture(lambda: jarvis.route_single("take a screenshot"))
        self.assertIn({"event": "route", "handler": "<lambda>", "command": "take a screenshot"}, events)

    def test_microphone_level_meter_reports_real_sample_levels(self):
        import numpy as np
        loud = (np.ones(1024, dtype=np.int16) * 16384).tobytes()

        class FakeStream:
            def read(self, size, *args, **kwargs):
                return loud

        source = types.SimpleNamespace(stream=FakeStream())

        def run():
            with mock.patch.object(jarvis, "_meter_failed", False):
                jarvis._attach_level_meter(source)
                data = source.stream.read(1024)
                self.assertEqual(data, loud)            # audio passes through untouched

        events = self.capture(run)
        levels = [e["v"] for e in events if e["event"] == "level"]
        self.assertEqual(len(levels), 1)
        self.assertAlmostEqual(levels[0], 0.5, places=2)

    def test_broken_pipe_disables_events_without_crashing(self):
        with mock.patch.object(jarvis, "EVENTS_ENABLED", True), mock.patch.object(jarvis, "_events_broken", False), \
                mock.patch("builtins.print", side_effect=BrokenPipeError):
            jarvis.emit("state", state="STANDBY")          # must not raise
            self.assertTrue(jarvis._events_broken)


# ---------------------------------------------------------------------------
# New: UI logic and process lifecycle (no window is ever created)
# ---------------------------------------------------------------------------
class UIModelTests(unittest.TestCase):
    def test_parse_engine_line(self):
        self.assertEqual(jarvis_ui.parse_engine_line('@@JARVIS@@{"event":"ready"}\n'), ("event", {"event": "ready"}))
        self.assertEqual(jarvis_ui.parse_engine_line("hello\n"), ("text", "hello"))
        self.assertIsNone(jarvis_ui.parse_engine_line("   \n"))
        self.assertEqual(jarvis_ui.parse_engine_line("@@JARVIS@@{broken")[0], "text")   # never crashes the UI

    def test_model_follows_real_engine_events(self):
        m = jarvis_ui.EngineModel()
        self.assertEqual(m.state, "OFFLINE")
        m.start()
        self.assertEqual(m.state, "INITIALIZING")
        self.assertFalse(m.online)
        m.apply({"event": "state", "state": "LISTENING"})
        self.assertEqual(m.state, "INITIALIZING")             # ignored until the engine says it is ready
        m.apply({"event": "ready"})
        self.assertEqual((m.state, m.online), ("STANDBY", True))
        for state in ("LISTENING", "THINKING", "SPEAKING", "STANDBY"):
            m.apply({"event": "state", "state": state})
            self.assertEqual(m.state, state)
        m.apply({"event": "state", "state": "BOGUS"})
        self.assertEqual(m.state, "STANDBY")

    def test_model_records_commands_and_responses(self):
        m = jarvis_ui.EngineModel()
        m.start()
        m.apply({"event": "ready"})
        self.assertEqual(m.apply({"event": "heard", "text": "pause the video"}), [("user", "pause the video")])
        self.assertEqual(m.apply({"event": "say", "text": "Paused the video."}), [("jarvis", "Paused the video.")])
        self.assertEqual((m.commands, m.last_command, m.last_response),
                         (1, "pause the video", "Paused the video."))
        m.apply({"event": "route", "handler": "handle_video", "command": "x"})
        self.assertEqual(m.last_route, "video")

    def test_level_is_clamped_and_bad_values_ignored(self):
        m = jarvis_ui.EngineModel()
        m.apply({"event": "level", "v": 0.05})
        self.assertAlmostEqual(m.level, 0.3)
        m.apply({"event": "level", "v": 5})
        self.assertEqual(m.level, 1.0)
        m.apply({"event": "level", "v": "nope"})
        self.assertEqual(m.level, 1.0)

    def test_stop_resets_to_offline(self):
        m = jarvis_ui.EngineModel()
        m.start()
        m.apply({"event": "ready"})
        m.stop()
        self.assertEqual((m.state, m.online), ("OFFLINE", False))

    def test_console_line_classification(self):
        self.assertIsNone(jarvis_ui.classify_console_line("Wake score: 0.0012"))
        self.assertIsNone(jarvis_ui.classify_console_line("JARVIS: hello"))
        self.assertEqual(jarvis_ui.classify_console_line("Traceback (most recent call last):")[0], "err")
        self.assertEqual(jarvis_ui.classify_console_line("Microphone error: x")[0], "err")
        self.assertEqual(jarvis_ui.classify_console_line("Step 1/2: open calculator")[0], "sys")

    def test_telemetry_parsers(self):
        vm = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
              "Pages free:                               1000.\n"
              "Pages active:                            50000.\n"
              "Pages wired down:                        30000.\n"
              "Pages occupied by compressor:            20000.\n")
        used = jarvis_ui.parse_vm_stat(vm, 8 * 1024 ** 3)
        self.assertAlmostEqual(used, 100000 * 16384 * 100 / (8 * 1024 ** 3), places=3)
        self.assertIsNone(jarvis_ui.parse_vm_stat("garbage", 1))
        self.assertEqual(jarvis_ui.parse_battery("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t83%; discharging; 5:10 remaining"),
                         (83, "on battery"))
        self.assertEqual(jarvis_ui.parse_battery("Now drawing from 'AC Power'\n 100%; charged; 0:00 remaining"),
                         (100, "plugged in"))
        self.assertIsNone(jarvis_ui.parse_battery("no battery"))

    def test_colour_mixing_and_bars(self):
        self.assertEqual(jarvis_ui.mix("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(jarvis_ui.mix("#102030", "#ffffff", 0), "#102030")
        self.assertEqual(jarvis_ui.mix("#102030", "#ffffff", 5), "#ffffff")      # t is clamped
        self.assertEqual(jarvis_ui.bar(50, 10), "█████░░░░░")
        self.assertEqual(jarvis_ui.bar(500, 4), "████")

    def test_all_engine_states_have_a_visual_style(self):
        for state in jarvis_ui.ENGINE_STATES + ("OFFLINE", "INITIALIZING"):
            self.assertIn(state, jarvis_ui.STATE_STYLE)


FAKE_ENGINE = r"""
import json, signal, sys, time
P = "@@JARVIS@@"
def emit(**d): print(P + json.dumps(d), flush=True)
mode = sys.argv[1] if len(sys.argv) > 1 else "good"
if mode == "crash":
    print("Traceback (most recent call last):", flush=True)
    print("ModuleNotFoundError: No module named 'pyaudio'", flush=True)
    sys.exit(1)
if mode == "stubborn":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
emit(event="ready")
emit(event="state", state="STANDBY")
print("plain console line", flush=True)
emit(event="heard", text="pause the video")
emit(event="say", text="Paused the video.")
try:
    while True:
        time.sleep(0.05)
except KeyboardInterrupt:
    emit(event="say", text="Goodbye")
    sys.exit(0)
"""


class EngineProcessTests(unittest.TestCase):
    """Runs a REAL child process (a fake engine that speaks the same event protocol)."""

    def setUp(self):
        import os
        import queue
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = os.path.join(self.tmp.name, "fake_engine.py")
        with open(self.script, "w") as handle:
            handle.write(FAKE_ENGINE)
        self.queue = queue.Queue()

    def make(self, mode):
        engine = jarvis_ui.EngineProcess(self.script, self.queue)
        engine.GRACE = (0.6, 0.6, 0.6)
        original = engine.python
        engine.python = original
        script_args = [mode]

        real_start = engine.start

        def start_with_mode():
            import subprocess
            real_popen = subprocess.Popen

            def popen(cmd, **kw):
                return real_popen(cmd + script_args, **kw)
            with mock.patch.object(jarvis_ui.subprocess, "Popen", popen):
                return real_start()

        engine.start = start_with_mode
        self.addCleanup(lambda: engine.running and engine.proc.kill())
        return engine

    def drain_until(self, kind, timeout=8.0):
        import queue
        import time
        items, deadline = [], time.time() + timeout
        while time.time() < deadline:
            try:
                item = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            items.append(item)
            if item[0] == kind:
                return items
        self.fail(f"timed out waiting for {kind!r}; got {items}")

    def test_events_flow_and_graceful_stop(self):
        engine = self.make("good")
        self.assertTrue(engine.start())
        self.assertFalse(engine.start())                     # no double launch
        items = self.drain_until("event")
        self.assertEqual(items[-1], ("event", {"event": "ready"}))
        time.sleep(0.4)
        self.assertTrue(engine.running)
        self.assertTrue(engine.request_stop())
        self.assertFalse(engine.request_stop())              # already stopping
        items = self.drain_until("exit")
        self.assertEqual(items[-1], ("exit", 0))             # graceful SIGINT exit, not a kill
        kinds = [i for i in items if i[0] == "event"]
        self.assertIn(("event", {"event": "say", "text": "Goodbye"}), kinds)
        self.assertFalse(engine.running)

    def test_stubborn_engine_is_escalated_and_killed(self):
        engine = self.make("stubborn")
        engine.start()
        self.drain_until("event")
        engine.request_stop()
        items = self.drain_until("exit")
        self.assertIn(items[-1][1], (-15, -9))               # SIGTERM or SIGKILL
        self.assertTrue(any(i[0] == "text" and "escalating" in i[1] for i in items))

    def test_crash_is_reported_with_output_tail(self):
        engine = self.make("crash")
        engine.start()
        items = self.drain_until("exit")
        self.assertEqual(items[-1], ("exit", 1))
        self.assertTrue(any("ModuleNotFoundError" in line for line in engine.tail))

    def test_stop_when_not_running_is_harmless(self):
        engine = jarvis_ui.EngineProcess(self.script, self.queue)
        self.assertFalse(engine.request_stop())

    def test_engine_gets_the_events_environment(self):
        captured = {}

        class FakePopen:
            def __init__(self, cmd, **kw):
                captured.update(kw)
                self.stdout = iter(())
                self.pid = 1
                self.returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        engine = jarvis_ui.EngineProcess(self.script, self.queue)
        with mock.patch.object(jarvis_ui.subprocess, "Popen", FakePopen):
            engine.start()
        self.assertEqual(captured["env"]["JARVIS_EVENTS"], "1")
        self.assertEqual(captured["env"]["PYTHONUNBUFFERED"], "1")
        self.assertTrue(captured["start_new_session"])



if __name__ == "__main__":
    unittest.main(verbosity=1)
