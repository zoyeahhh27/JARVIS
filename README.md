# JARVIS — Local Voice Assistant for macOS

A local, voice-controlled desktop assistant for macOS with a Tkinter control-panel UI, wake-word activation, speech recognition, local LLM responses through Ollama, and desktop/browser automation.

> **Platform:** macOS (Apple Silicon tested). This project uses macOS utilities such as `say`, `osascript`, `open`, `pmset`, and `screencapture`; it is not currently a cross-platform assistant.

## Features

- Wake-word activation with OpenWakeWord (`Hey Jarvis`).
- Speech input using SpeechRecognition and PyAudio.
- Speech output using the built-in macOS `say` command.
- Local conversational responses and command planning through Ollama (`llama3.2:3b` by default).
- Desktop and browser actions using AppleScript and macOS command-line utilities.
- Tkinter dashboard showing assistant state, recognized speech, responses, and microphone telemetry.
- Session-only conversational and browser context.

## Requirements

- macOS with Python 3.10+ (use a Python installation that includes Tkinter).
- Homebrew is recommended for installing native dependencies.
- Ollama installed and running locally.
- A working microphone and macOS permissions for Microphone and Accessibility / Automation where requested.

## Setup

1. Install [Homebrew](https://brew.sh/) if it is not already installed.
2. Install PortAudio and Ollama:

   ```bash
   brew install portaudio ollama
   ```

3. Start Ollama and download the configured model:

   ```bash
   ollama serve
   ```

   In another Terminal window:

   ```bash
   ollama pull llama3.2:3b
   ```

4. Clone this repository and enter the project directory:

   ```bash
   git clone https://github.com/zoyeahhh27/JARVIS.git
   cd JARVIS
   ```

5. Create and activate a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install --upgrade pip
   ```

6. Install Python dependencies:

   ```bash
   pip install -r requirements.txt
   ```

7. Download OpenWakeWord's pre-trained models:

   ```bash
   python -c "import openwakeword; openwakeword.utils.download_models()"
   ```

## Run

Keep Ollama running, then launch the dashboard:

```bash
python jarvis_ui.py
```

The dashboard starts `jarvis.py` as a child process. You can also run the voice engine without the dashboard:

```bash
python jarvis.py
```

Say **“Hey Jarvis”** to activate the assistant, then give a command.

## macOS permissions

The first time you use relevant features, macOS may ask for permission. Review **System Settings → Privacy & Security** and allow microphone access and the required Accessibility / Automation permissions for the terminal or app that launches Python. Only grant permissions you are comfortable with.

## Tests

Run the routing and UI helper tests with:

```bash
pytest -q
```

Hardware-dependent behavior (microphone, wake-word detection, macOS automation, and Ollama responses) still needs to be tested on the target Mac.

## Configuration

The default local model is `llama3.2:3b`, served through Ollama at `http://localhost:11434`. The configured microphone device index and wake-word thresholds are set near the top of `jarvis.py` and may need adjustment for a different Mac or microphone.

## Safety and privacy

- Ollama conversation and planning requests are sent to the local Ollama service by default.
- The weather utility uses the Open-Meteo public API.
- Desktop automation can open applications, interact with browser tabs, manage windows, and perform system actions. Review commands before using them, and be especially careful with shutdown/restart or other system-level actions.
- Do not commit `.env` files, API keys, credentials, personal recordings, local model files, virtual environments, or machine-specific logs.

## License

No license has been specified yet. Add a license file before accepting external contributions or defining reuse permissions.