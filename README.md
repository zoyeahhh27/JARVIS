# JARVIS — Local Voice Assistant for macOS

<p align="center">
  <strong>A voice-activated desktop assistant with a futuristic control panel, local AI, and macOS automation.</strong>
</p>

<p align="center">
  <a href="https://github.com/zoyeahhh27/JARVIS"><img src="https://img.shields.io/badge/platform-macOS-111111?logo=apple&logoColor=white" alt="macOS"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://ollama.com/"><img src="https://img.shields.io/badge/AI-Ollama-000000?logo=ollama&logoColor=white" alt="Ollama"></a>
  <img src="https://img.shields.io/badge/UI-Tkinter-2C3E50" alt="Tkinter">
</p>

JARVIS is a personal voice-controlled assistant for macOS. It listens for the **“Hey Jarvis”** wake word, recognizes spoken commands, uses a local language model through Ollama for conversational responses and command planning, and can perform desktop/browser actions. A Tkinter dashboard provides a visual interface for assistant state, recognized speech, responses, and microphone telemetry.

> **Platform note:** Developed and tested on macOS (Apple Silicon). It uses macOS utilities such as `say`, `osascript`, `open`, `pmset`, and `screencapture`; it is not currently cross-platform.

## Demo

A screen-recorded walkthrough is planned. It will demonstrate wake-word activation, voice commands, local Ollama responses, and the dashboard.

## Highlights

- **Wake-word activation** using OpenWakeWord (“Hey Jarvis”).
- **Speech recognition** through SpeechRecognition and PyAudio.
- **Voice responses** using macOS built-in `say`.
- **Local AI** using Ollama (default model: `llama3.2:3b`).
- **Desktop and browser automation** with AppleScript and macOS command-line utilities.
- **Live dashboard** built with Tkinter, displaying assistant state, recognized speech, responses, and microphone telemetry.
- **Session context** for conversational and browser context.

## Tech stack

| Area | Tools |
|---|---|
| Language | Python |
| Desktop UI | Tkinter |
| Wake word | OpenWakeWord |
| Speech input | SpeechRecognition, PyAudio |
| Speech output | macOS `say` |
| Local AI | Ollama, `llama3.2:3b` |
| Automation | AppleScript, `osascript`, macOS CLI |

## Getting started

### Prerequisites

- macOS and Python 3.10+ (Python installation with Tkinter).
- [Homebrew](https://brew.sh/)
- [Ollama](https://ollama.com/) installed and running.
- A working microphone.
- macOS Microphone and, for applicable automation, Accessibility / Automation permissions.

### 1. Install native dependencies

```bash
brew install portaudio ollama
```

### 2. Start Ollama and pull the model

In one Terminal window:

```bash
ollama serve
```

In another:

```bash
ollama pull llama3.2:3b
```

### 3. Clone the repository

```bash
git clone https://github.com/zoyeahhh27/JARVIS.git
cd JARVIS
```

### 4. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### 5. Install Python packages

```bash
pip install -r requirements.txt
```

### 6. Download OpenWakeWord models

```bash
python -c "import openwakeword; openwakeword.utils.download_models()"
```

## Launch

Keep Ollama running, then start the dashboard:

```bash
python jarvis_ui.py
```

The dashboard launches `jarvis.py` as a child process. To run the voice engine without the dashboard:

```bash
python jarvis.py
```

Say **“Hey Jarvis”** to wake the assistant, then speak a command.

## macOS permissions

When prompted, review **System Settings → Privacy & Security** and grant Microphone and required Accessibility / Automation permissions to the terminal or app running Python. Grant only permissions you are comfortable with.

## Tests

Run the project tests:

```bash
pytest -q
```

The routing test suite has passed in the project development environment. Hardware-dependent behavior—microphone input, wake-word detection, macOS automation, and Ollama responses—must still be verified on the target Mac.

## Configuration

The default model is `llama3.2:3b`, served by Ollama at `http://localhost:11434`. Microphone device index and wake-word thresholds are configured near the top of `jarvis.py`; they may require adjustment for another Mac or microphone.

## Privacy and safety

- Conversational and planning requests are sent to the local Ollama service by default.
- The weather utility uses the Open-Meteo public API.
- Desktop automation may open apps, interact with browser tabs, manage windows, or perform system actions. Be mindful with shutdown, restart, and other system-level commands.
- Never commit API keys, credentials, `.env` files, personal recordings, local model files, virtual environments, or machine-specific logs.

## License

No license has been specified yet. Add a license before accepting external contributions or explicitly defining reuse permissions.
