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
# local, private (default): needs Ollama + a vision model
ollama pull moondream
python ss_triage.py --model moondream

# no model, simple filename rules: for trying the popup
python ss_triage.py --fake

# free hosted open-weight model: screenshots ARE uploaded
set OPENROUTER_API_KEY=...
python ss_triage.py --api
```

Options: `--timeout 5` (seconds before auto-accept), `--expire 14` (days for Read later), `--sent-expire 1` (days for Sent), `--api-model`, and a folder path. The default folder is `Pictures\Screenshots`, or OneDrive's `Pictures\Screenshots*` when that's where Windows saves them.

Every decision is logged to `triage-log.tsv` in the screenshots folder.

## Privacy

- **Local mode:** images never leave your machine.
- **`--api` mode:** images are sent to OpenRouter. Don't use it for screenshots with passwords or IDs.
- If your screenshots folder is inside OneDrive, it is synced to the cloud regardless of this tool.

## License

MIT
