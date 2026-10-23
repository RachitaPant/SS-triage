"""ss-triage: decide what a screenshot is worth the moment you take it.

Take a screenshot -> a small popup appears with the local AI's guess highlighted.
Press a number key (or click) to choose, or ignore it and the guess is applied.

    1 Class notes  2 Chats  3 Inspo  4 Read later  5 Sent
    6 Important    7 Sensitive      8 Bin

Two folders clean themselves up (Recycle Bin, so it's recoverable):
    Read later   binned after --expire days (default 14). Be honest.
    Sent         for screenshots taken just to paste into a chat or an AI;
                 binned after --sent-expire days (default 1).
Want to keep one? Move it out of that folder.
Esc leaves the screenshot where it is.

Usage:
    python ss_triage.py                    # watch ~/Pictures/Screenshots
    python ss_triage.py <folder>           # watch another folder
    python ss_triage.py --api              # free hosted model instead of Ollama
    python ss_triage.py --fake             # no model (for testing)
    python ss_triage.py --expire 7 --timeout 8

Needs Ollama: a small vision model describes (moondream), a text model
decides (qwen2.5:3b-instruct). Both fit on an 8 GB laptop.
Local mode: images never leave your machine.
API mode (--api): needs OPENROUTER_API_KEY; images ARE uploaded to OpenRouter.
"""
import argparse
import base64
import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

CHOICES = [  # (key, folder, label)
    ("class_notes", "Class notes", "Class notes"),
    ("chats", "Chats", "Chats"),
    ("inspo", "Inspo", "Inspo"),
    ("read_later", "Read later", "Read later"),
    ("sent", "Sent", "Sent"),
    ("important", "Important", "Important"),
    ("sensitive", "Sensitive", "Sensitive"),
    ("bin", None, "Bin"),
]
KEYS = [c[0] for c in CHOICES]
EXPIRING = {"read_later", "sent"}  # binned after --expire / --sent-expire days
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
OLLAMA_URL = "http://localhost:11434/api/generate"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_DIR = next((d for d in [Path.home() / "Pictures" / "Screenshots",
                                 *sorted((Path.home() / "OneDrive" / "Pictures").glob("Screenshots*"))]
                    if d.is_dir()), Path.home() / "Pictures" / "Screenshots")
STATE_FILE = ".read-later.json"

PROMPT = """Classify this screenshot. Reply with only JSON:
{"tier": "...", "reason": "...", "title": "..."}
tier must be one of:
- "sensitive": passwords, logins, OTPs, ID cards, bank/card numbers, balances
- "important": receipts, payments, tickets, bookings, addresses, deadlines, errors
- "class_notes": lecture slides, notes, textbooks, whiteboards, assignments
- "chats": WhatsApp/Telegram/Instagram/Discord or any messaging conversation
- "inspo": aesthetic inspiration: outfits, room decor, art, design, moodboards, pins
- "read_later": articles, posts, threads, videos, products, links to check later
- "bin": memes, jokes, home/lock screens, blank or accidental captures, games
If anything sensitive is visible, choose "sensitive".
reason: under 8 words, describing it WITHOUT repeating any secret.
title: 3-6 lowercase words to use as a searchable filename, naming the
topic (e.g. "coding agents internals week 1", "goa train ticket",
"beige room decor"). For sensitive ones name the kind only (e.g. "bank
login"), NEVER a number, password or code."""

TEXT_PROMPT = """Someone took a screenshot. Pick where it should be filed.
Reply with only JSON: {"tier": "...", "reason": "...", "title": "..."}
tier must be one of:
- "class_notes": lecture slides, notes, textbooks, whiteboards, assignments
- "chats": WhatsApp/Telegram/Instagram/Discord or any messaging conversation
- "inspo": aesthetic inspiration: outfits, room decor, art, design, moodboards
- "read_later": articles, blog posts, tweets/threads, LinkedIn posts, videos,
  products, docs or tutorials about any topic (tech, AI, careers...) to read later
- "important": receipts, payments, tickets, bookings, addresses, deadlines
- "sensitive": ONLY if the description explicitly mentions a password, login,
  OTP, ID card, passport, or bank/card number. Otherwise never pick this.
- "bin": memes, jokes, home/lock screens, blank or accidental captures, games
reason: under 8 words saying what it shows, WITHOUT repeating any secret.
title: 3-6 lowercase words to use as a searchable filename, naming the
topic (e.g. "coding agents internals week 1", "goa train ticket",
"beige room decor"). For sensitive ones name the kind only (e.g. "bank
login"), NEVER a number, password or code.

"""


# ---------- classification ----------

def ollama(payload):
    req = urllib.request.Request(OLLAMA_URL, json.dumps({**payload, "stream": False}).encode(),
                                 {"Content-Type": "application/json"})
    # Fail fast: on a busy 8 GB laptop one stuck call would block the whole queue.
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())["response"]


OCR_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $t) { $x = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $x.Wait() | Out-Null; $x.Result }
$f = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:SS_PATH)) ([Windows.Storage.StorageFile])
$s = Await ($f.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$d = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($s)) ([Windows.Graphics.Imaging.BitmapDecoder])
$b = Await ($d.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$r = Await ([Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages().RecognizeAsync($b)) ([Windows.Media.Ocr.OcrResult])
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$r.Text
"""


def read_text(path):
    """Built-in Windows OCR: local, nothing to install. Returns '' if it fails."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", OCR_PS],
                             env={**os.environ, "SS_PATH": str(path.resolve())},
                             capture_output=True, timeout=60,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def ask_model(path, model, text_model):
    img = base64.b64encode(path.read_bytes()).decode()
    if not text_model:  # one capable vision model does everything
        return parse_answer(json.loads(ollama(
            {"model": model, "prompt": PROMPT, "images": [img], "format": "json"})))
    # Small vision models (moondream) can't read text-heavy screenshots and can't
    # follow an 8-option prompt. So read the actual text with OCR, and let a text
    # model decide. The vision model is only asked when there's little text.
    text = read_text(path)
    info = f"Text in the screenshot (OCR):\n{text[:1500]}\n"
    if len(text.split()) < 15:
        if not model:  # no vision model: a picture with little text is your call
            raise ValueError("not much text to go on")
        # keep_alive 0: unload right after, so it and the text model are never
        # both sitting in RAM on an 8 GB laptop.
        caption = ollama({"model": model, "images": [img], "keep_alive": 0,
                          "prompt": "Describe this image in one sentence."})
        info += f"\nWhat it looks like: {caption.strip()}\n"
    answer = json.loads(ollama({"model": text_model, "format": "json",
                                "prompt": TEXT_PROMPT + info}))
    return overrule(text, *parse_answer(answer))


def ask_api(path, model):
    """Free hosted open-weight model via OpenRouter. The image LEAVES your machine."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("set OPENROUTER_API_KEY")
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    img = base64.b64encode(path.read_bytes()).decode()
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img}"}},
    ]}]}).encode()
    req = urllib.request.Request(OPENROUTER_URL, body, {
        "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        text = json.loads(r.read())["choices"][0]["message"]["content"]
    # Hosted models may wrap JSON in prose or ``` fences; take the {...} part.
    return overrule("", *parse_answer(json.loads(text[text.index("{"):text.rindex("}") + 1])))


# Small models often give the right reason but the wrong folder ("bank login
# details" -> read_later). When the words are clear, plain rules win.
OVERRULES = [
    ("sensitive", r"\b(password|passcode|log ?in|sign ?in|otp|one.time|bank|account (no|number)|"
                  r"ifsc|upi|card number|cvv|debit|credit card|aadhaa?r|pan card|passport|"
                  r"driving licen[cs]e|ssn)\b"),
    ("inspo", r"\b(decor|interior|outfit|aesthetic|moodboard|mood board|pinterest|fashion|"
              r"bedroom|living room|makeup|nails|hairstyle)\b"),
]


def overrule(ocr_text, tier, reason, title):
    said = f"{reason} {title}".lower()
    for forced, pattern in OVERRULES:
        # Sensitive also checks the screenshot's own text: better safe than binned.
        haystack = said + (" " + ocr_text.lower() if forced == "sensitive" else "")
        if tier != forced and re.search(pattern, haystack):
            return forced, reason, title
    return tier, reason, title


def parse_answer(data):
    tier = str(data.get("tier", "")).lower()
    reason = str(data.get("reason", "")).strip().lower()
    # "sent" is about why you took it, which only you know: never the AI's call.
    if tier not in KEYS or tier == "sent":
        raise ValueError(f"model said {tier!r}")
    # A reason that is empty or just repeats the category means it didn't really look.
    if not reason or reason.replace("_", " ") in (tier.replace("_", " "), "..."):
        raise ValueError("model gave no real reason")
    return tier, str(data.get("reason", ""))[:60], clean_title(data.get("title"))


def clean_title(title):
    """Make the model's title safe as a filename; '' if unusable."""
    words = re.sub(r"[^a-z0-9 ]+", " ", str(title or "").lower()).split()[:6]
    # Belt and braces: drop anything that looks like a number/code (OTPs, cards).
    words = [w for w in words if not (sum(c.isdigit() for c in w) >= 3)]
    return " ".join(words)


def fake_classify(path):
    name = path.name.lower()
    rules = [("sensitive", ("password", "otp", "bank", "aadhaar")),
             ("important", ("receipt", "ticket", "invoice", "payment")),
             ("class_notes", ("lecture", "notes", "slide")),
             ("chats", ("chat", "whatsapp")),
             ("inspo", ("inspo", "outfit", "pinterest")),
             ("sent", ("share", "snippet")),
             ("read_later", ("article", "thread", "post"))]
    for tier, words in rules:
        if any(w in name for w in words):
            return tier, "keyword in filename", ""
    if path.stat().st_size < 30_000:
        return "bin", "tiny file, probably accidental", ""
    return "read_later", "no strong signal", ""


# ---------- file actions ----------

class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


def to_recycle_bin(path):
    """Send to the Windows Recycle Bin (recoverable), not a permanent delete."""
    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT = 3, 0x40, 0x10, 0x4
    op = _SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=str(path.resolve()) + "\0",
                          fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT)
    if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) != 0:
        raise OSError(f"Could not recycle {path}")


def unique_dest(dest):
    stem, suf, n = dest.stem, dest.suffix, 1
    while dest.exists():
        dest = dest.with_name(f"{stem} ({n}){suf}")
        n += 1
    return dest


def apply_choice(shot, tier, folder, reason, by, title=""):
    sub = dict((k, f) for k, f, _ in CHOICES)[tier]
    if sub is None:
        to_recycle_bin(shot)
        dest = None
    else:
        (folder / sub).mkdir(exist_ok=True)
        name = f"{datetime.fromtimestamp(shot.stat().st_mtime):%Y-%m-%d} {title}{shot.suffix}" if title else shot.name
        dest = unique_dest(folder / sub / name)
        shutil.move(str(shot), str(dest))
        if tier in EXPIRING:
            state = load_state(folder, sub)
            state[dest.name] = time.time()
            save_state(folder, sub, state)
    with open(folder / "triage-log.tsv", "a", encoding="utf-8") as log:
        new = dest.name if dest else ""
        log.write(f"{datetime.now():%Y-%m-%d %H:%M}\t{tier}\t{by}\t{shot.name}\t{new}\t{reason}\n")
    print(f"{datetime.now():%H:%M:%S}  {tier:<12} ({by})  {shot.name}" + (f" -> {dest.name}" if dest else ""))


def load_state(folder, sub):
    try:
        return json.loads((folder / sub / STATE_FILE).read_text())
    except (OSError, ValueError):
        return {}


def save_state(folder, sub, state):
    (folder / sub).mkdir(exist_ok=True)
    (folder / sub / STATE_FILE).write_text(json.dumps(state, indent=1))


def sweep_expiring(folder, sub, days):
    """Bin anything that sat in an expiring folder longer than `days`."""
    state, now, binned = load_state(folder, sub), time.time(), 0
    for name, added in list(state.items()):
        f = folder / sub / name
        if not f.exists():          # you moved it out: rescued
            del state[name]
        elif now - added > days * 86400:
            to_recycle_bin(f)
            del state[name]
            binned += 1
    if binned:
        save_state(folder, sub, state)
        print(f"{datetime.now():%H:%M:%S}  binned {binned} expired '{sub}' screenshot(s)")


# ---------- watching ----------

def screenshots(folder):
    return {p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS}


def watcher(folder, out):
    seen = screenshots(folder)  # leave existing ones alone
    while True:
        time.sleep(1)
        for shot in sorted(screenshots(folder) - seen):
            seen.add(shot)
            try:
                size = -1
                while size != shot.stat().st_size:  # wait until fully written
                    size = shot.stat().st_size
                    time.sleep(0.4)
                out.put(shot)
            except OSError:
                pass


# ---------- popup ----------

class Popup:
    """One small always-on-top window per screenshot, bottom-right corner."""

    def __init__(self, root, shot, args, results, waiting, on_done):
        self.shot, self.args, self.on_done = shot, args, on_done
        self.results = results  # shared dict, filled by the background classifier
        self.guess = self.reason = self.title = None
        self.waited, self.slow_shown = 0.0, False
        self.remaining = args.timeout
        self.done = False

        w = self.win = tk.Toplevel(root)
        w.title("ss-triage" + (f"  (+{waiting} waiting)" if waiting else ""))
        w.attributes("-topmost", True)
        w.resizable(False, False)
        self.status = tk.Label(w, text=f"📸  {shot.name}\nAI is looking…",
                               justify="left", anchor="w", padx=10, pady=6)
        self.status.pack(fill="x")
        row = tk.Frame(w, padx=8, pady=6)
        row.pack()
        self.buttons = {}
        for i, (key, _, label) in enumerate(CHOICES, 1):
            b = tk.Button(row, text=f"{i} {label}", width=11,
                          command=lambda k=key: self.choose(k, "you"))
            b.grid(row=(i - 1) // 3, column=(i - 1) % 3, padx=2, pady=2)
            self.buttons[key] = b
            w.bind(str(i), lambda e, k=key: self.choose(k, "you"))
        w.bind("<Escape>", lambda e: self.close("left in place"))
        w.protocol("WM_DELETE_WINDOW", lambda: self.close("left in place"))

        w.update_idletasks()
        x = w.winfo_screenwidth() - w.winfo_reqwidth() - 24
        y = w.winfo_screenheight() - w.winfo_reqheight() - 72
        w.geometry(f"+{x}+{y}")
        w.focus_force()

        w.after(200, self.poll)

    def poll(self):
        if self.done:
            return
        res = self.results.pop(self.shot, None)
        if res is None:
            self.waited += 0.2
            if self.waited >= 30 and not self.slow_shown:
                self.slow_shown = True
                self.status.config(text=f"📸  {self.shot.name}\nAI is slow (low memory?). "
                                        "Pick one, or keep waiting.")
            self.win.after(200, self.poll)
            return
        if isinstance(res, Exception):
            # No guess -> no auto-accept. You decide.
            self.status.config(text=f"📸  {self.shot.name}\nAI couldn't tell ({res}). Your call.")
            return
        self.guess, self.reason, self.title = res
        self.buttons[self.guess].config(relief="sunken", bg="#ffd966")
        self.tick()

    def tick(self):
        if self.done:
            return
        label = dict((k, l) for k, _, l in CHOICES)[self.guess]
        if self.guess == "bin":
            # The AI may suggest throwing something out, but only you can do it.
            self.status.config(text=f"📸  Looks like: {self.reason}\n"
                                    f"→ Bin? Press {KEYS.index('bin') + 1} to confirm, or pick another.")
            return
        self.status.config(text=f"📸  Looks like: {self.reason}\n"
                                f"→ {label}   (auto in {self.remaining}s)")
        if self.remaining <= 0:
            self.choose(self.guess, "ai")
            return
        self.remaining -= 1
        self.win.after(1000, self.tick)

    def choose(self, tier, by):
        if self.done:
            return
        try:
            apply_choice(self.shot, tier, self.args.folder, self.reason or "", by, self.title or "")
        except Exception as e:
            print(f"  ! {self.shot.name}: {e}")
        self.close()

    def close(self, note=None):
        if self.done:
            return
        self.done = True
        if note:
            print(f"{datetime.now():%H:%M:%S}  {note}: {self.shot.name}")
        self.win.destroy()
        self.on_done()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, nargs="?", default=DEFAULT_DIR)
    ap.add_argument("--model", default="moondream",
                    help="vision model, used only for pictures with little text; "
                         "\"\" = skip it and make those your call")
    ap.add_argument("--text-model", default="qwen2.5:3b-instruct",
                    help="text model that picks the folder; \"\" = let --model do it all")
    ap.add_argument("--api", action="store_true",
                    help="use a free hosted open-weight model (OpenRouter) instead of Ollama")
    ap.add_argument("--api-model", default="google/gemma-4-26b-a4b-it:free")
    ap.add_argument("--fake", action="store_true", help="skip the model, use simple rules")
    ap.add_argument("--timeout", type=int, default=5, help="seconds before the AI guess is applied")
    ap.add_argument("--expire", type=float, default=14, help="days before unread 'Read later' is binned")
    ap.add_argument("--sent-expire", type=float, default=1, help="days before 'Sent' is binned")
    args = ap.parse_args()
    if not args.folder.is_dir():
        sys.exit(f"Not a folder: {args.folder}")

    root = tk.Tk()
    root.withdraw()
    incoming, to_classify, results = queue.Queue(), queue.Queue(), {}

    def classifier():
        # Starts on each screenshot as soon as it's taken, one at a time (the
        # local models can't run in parallel on 8 GB anyway), so a burst of
        # screenshots is mostly guessed by the time each popup appears.
        while True:
            shot = to_classify.get()
            try:
                if args.fake:
                    res = fake_classify(shot)
                elif args.api:
                    res = ask_api(shot, args.api_model)
                else:
                    res = ask_model(shot, args.model, args.text_model)
            except Exception as e:
                res = e
            results[shot] = res

    def fan_out():
        while True:
            shot = raw.get()
            to_classify.put(shot)
            incoming.put(shot)

    raw = queue.Queue()
    threading.Thread(target=watcher, args=(args.folder, raw), daemon=True).start()
    threading.Thread(target=fan_out, daemon=True).start()
    threading.Thread(target=classifier, daemon=True).start()
    busy = [False]
    last_sweep = [0.0]

    def pump():
        if time.time() - last_sweep[0] > 3600:
            last_sweep[0] = time.time()
            try:
                sweep_expiring(args.folder, "Read later", args.expire)
                sweep_expiring(args.folder, "Sent", args.sent_expire)
            except Exception as e:
                print(f"  ! sweep failed: {e}")
        if not busy[0]:
            try:
                shot = incoming.get_nowait()
            except queue.Empty:
                pass
            else:
                if shot.exists():
                    busy[0] = True
                    Popup(root, shot, args, results, incoming.qsize(),
                          on_done=lambda: busy.__setitem__(0, False))
        root.after(300, pump)

    if args.api and not args.fake:
        if not os.environ.get("OPENROUTER_API_KEY"):
            sys.exit("API mode needs OPENROUTER_API_KEY set (free key at openrouter.ai).")
        print("! API mode: screenshots are uploaded to OpenRouter. "
              "Don't use it if they contain passwords/IDs; drop --api for private local mode.")
    print(f"Watching {args.folder} — take a screenshot. Ctrl+C to stop.")
    root.after(300, pump)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        print("\nStopped. Back to napping.")


if __name__ == "__main__":
    main()
