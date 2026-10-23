# Downloads Sloth

A lazy file tidier powered by an open-weight model (Gemma 4 31B via OpenRouter).
Drop files in Downloads. The sloth reads them, renames them, sorts them, and
leaves a short digest. It never deletes anything, and every move can be undone.

## Setup (Windows, PowerShell)

    pip install -r requirements.txt
    setx OPENROUTER_API_KEY "sk-or-your-key"     # then close and reopen PowerShell

Edit `config.toml` if your folders differ (defaults: `~/Downloads` -> `~/Downloads/_Sorted`).

## Use

    python sloth.py check             # test key + show your real OpenRouter limits
    python sloth.py once --limit 10   # dry run on 10 files (default mode: moves nothing)
    python sloth.py once --live       # actually move files
    python sloth.py run --live        # watch continuously
    python sloth.py undo --last 5     # put the last 5 moves back
    python sloth.py digest            # write today's summary
    python sloth.py ui                # local web dashboard (needs nothing extra)

## Dashboard

`python sloth.py ui` opens http://127.0.0.1:8765. It shows the pile of waiting files,
the sorted shelves, every decision with its confidence, and a live console. Buttons run
the same code as the command line: preview (dry run), sort for real, undo, digest, check.
It only listens on your own machine and refuses requests from other websites.

## Run it in the background (the truly lazy part)

Task Scheduler -> Create Task -> Trigger: "At log on" -> Action: Start a program
- Program: `pythonw.exe`
- Arguments: `C:\path\to\sloth.py run --live`

For a nightly digest, add a second task at 9 PM with arguments `C:\path\to\sloth.py digest`.

## Free-tier limits (important)
OpenRouter `:free` models allow 20 requests/minute and **50 requests/day** until you have
bought at least $10 of credits once (then 1,000/day). The limit is per account, not per key.
The sloth saves quota by: sorting obvious types by extension (no call), skipping private
files, reusing dry-run answers when you go `--live`, and stopping safely (files untouched)
when a limit is hit.

## How it protects you
- Dry run is the default.
- Files matching private-name patterns (tax, bank, passport, ...) are never sent to the model.
- Obvious types (.exe, .zip, .mp3, ...) are sorted by extension with no API call.
- Only the first ~1,000 characters (`max_chars_sent` in `config.toml`) of a document (or a downscaled image) is sent.
- Low-confidence results go to "Needs a human" instead of being guessed.
- Swap model/provider in `config.toml`. Any OpenAI-compatible endpoint works,
  including a local Ollama server (`base_url: http://localhost:11434/v1`).
