# ss-triage

Decide what a screenshot is worth **the moment you take it**, so the screenshots folder never gets to 500.

Take a screenshot and a small popup appears with a local AI's guess already highlighted. Press a number to choose, or ignore it and the guess is applied after a few seconds.

| Key | Goes to |
|---|---|
| 1 | `Class notes/` |
| 2 | `Chats/` |
| 3 | `Inspo/` |
| 4 | `Read later/` (binned after 14 days unless you move it out) |
| 5 | `Sent/` (screenshots taken just to paste or share; binned after 1 day) |
| 6 | `Important/` |
| 7 | `Sensitive/` |
| 8 | Recycle Bin (recoverable) |
| Esc | leave it where it is |

Windows only (it uses the Windows Recycle Bin). Python 3.9+, standard library only.

## Run

```bash
# local, private (default): a text model, plus a small vision model for pictures
ollama pull qwen2.5:3b-instruct
ollama pull moondream
python ss_triage.py

# low on RAM: skip the vision model, pictures become your call
python ss_triage.py --model ""

# no model, simple filename rules: for trying the popup
python ss_triage.py --fake

# free hosted open-weight model: screenshots ARE uploaded
set OPENROUTER_API_KEY=...
python ss_triage.py --api
```

Options: `--timeout 5` (seconds before auto-accept), `--expire 14` (days for Read later), `--sent-expire 1` (days for Sent), `--text-model`, `--api-model`, and a folder path. The default folder is `Pictures\Screenshots`, or OneDrive's `Pictures\Screenshots*` when that's where Windows saves them.

## How it decides

1. **Reads the text** in the screenshot with Windows' built-in OCR (local, nothing to install).
2. **A small text model** (`qwen2.5:3b-instruct`) picks a folder, a reason, and a short title.
   For pictures with little text (inspo, outfits, decor), `moondream` describes the image first.
3. **Plain rules have the last word.** Small models often get the reason right ("bank login details") but the folder wrong, so words like password, OTP, bank, Aadhaar or passport force **Sensitive**, and decor, outfit or moodboard force **Inspo**.
4. **The file is renamed** to something you can search for: `Screenshot 2026-10-23 162659.png` becomes `2026-10-23 coding agents internals week 1.png`. Words with 3+ digits are dropped, so an OTP or card number never ends up in a filename.

Some decisions are always yours: the AI never picks **Sent** (only you know why you took it), and a **Bin** guess waits for your keypress. Pictures with little text are described by `moondream`, which is unloaded straight after so both models are never in RAM at once. After 30 seconds without a guess the popup says so, and after 90 the AI gives up on that one.

Every decision (old name, new name, who chose) is logged to `triage-log.tsv` in the screenshots folder.

## Privacy

- **Local mode:** images never leave your machine.
- **`--api` mode:** images are sent to OpenRouter. Don't use it for screenshots with passwords or IDs.
- If your screenshots folder is inside OneDrive, it is synced to the cloud regardless of this tool.

## License

MIT
