# Playroom Engine

Python/FastAPI engine for interactive fiction and companion conversations.
Includes scene generation, character presence, memory checkpoints, per-character
knowledge, model routing, a companion creator, and token accounting.

## Local setup

Requires Python 3.12 or later.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Enter your own API key in .env, then:
python main.py
```

Open http://localhost:8000. The API settings page lets you select a supported
model. Actual availability depends on the provider and your account.

## Your worlds and saves

On first start, the engine creates an empty Sandbox under playrooms/sandbox
from starter/. Later starts preserve it. Use the companion creator in the market
to create your own character, or author a room using playroom-blueprint and
boyfriend-blueprint. Each local installation stores its own playrooms, history,
sessions, memory, model settings and token log. These paths are ignored by Git.
Edit starter/ only to change defaults for future installations.

The framework includes no API credentials or played story packs. This version
uses a new Git history to remove previously committed personal runtime data.

## Verification

```powershell
python -m unittest discover -s tests -v
```
