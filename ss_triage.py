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

Needs Ollama with a vision model, e.g.:  ollama pull qwen2.5vl:3b
Local mode: images never leave your machine.
API mode (--api): needs OPENROUTER_API_KEY; images ARE uploaded to OpenRouter.
"""
import argparse
import base64
import ctypes
import json
import os
import queue
import shutil
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
{"tier": "...", "reason": "..."}
tier must be one of:
- "sensitive": passwords, logins, OTPs, ID cards, bank/card numbers, balances
- "important": receipts, payments, tickets, bookings, addresses, deadlines, errors
- "class_notes": lecture slides, notes, textbooks, whiteboards, assignments
- "chats": WhatsApp/Telegram/Instagram/Discord or any messaging conversation
- "inspo": aesthetic inspiration: outfits, room decor, art, design, moodboards, pins
- "read_later": articles, posts, threads, videos, products, links to check later
- "sent": a quick capture made only to share or paste somewhere: a code snippet,
  an error, a small piece of a page or app, a reel/post to forward to a friend
- "bin": memes, jokes, home/lock screens, blank or accidental captures, games
If anything sensitive is visible, choose "sensitive".
reason: under 8 words, describing it WITHOUT repeating any secret."""


# ---------- classification ----------

def ask_model(path, model):
    img = base64.b64encode(path.read_bytes()).decode()
    body = json.dumps({"model": model, "prompt": PROMPT, "images": [img],
                       "format": "json", "stream": False}).encode()
    req = urllib.request.Request(OLLAMA_URL, body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(json.loads(r.read())["response"])
    return parse_answer(data)


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
    return parse_answer(json.loads(text[text.index("{"):text.rindex("}") + 1]))


def parse_answer(data):
    tier = str(data.get("tier", "")).lower()
    if tier not in KEYS:
        raise ValueError(f"model said {tier!r}")
    return tier, str(data.get("reason", ""))[:60]


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
            return tier, "keyword in filename"
    if path.stat().st_size < 30_000:
        return "bin", "tiny file, probably accidental"
    return "read_later", "no strong signal"


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


def apply_choice(shot, tier, folder, reason, by):
    sub = dict((k, f) for k, f, _ in CHOICES)[tier]
    if sub is None:
        to_recycle_bin(shot)
        dest = None
    else:
        (folder / sub).mkdir(exist_ok=True)
        dest = unique_dest(folder / sub / shot.name)
        shutil.move(str(shot), str(dest))
        if tier in EXPIRING:
            state = load_state(folder, sub)
            state[dest.name] = time.time()
            save_state(folder, sub, state)
    with open(folder / "triage-log.tsv", "a", encoding="utf-8") as log:
        log.write(f"{datetime.now():%Y-%m-%d %H:%M}\t{tier}\t{by}\t{shot.name}\t{reason}\n")
    print(f"{datetime.now():%H:%M:%S}  {tier:<12} ({by})  {shot.name}")


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

    def __init__(self, root, shot, args, on_done):
        self.shot, self.args, self.on_done = shot, args, on_done
        self.guess = self.reason = None
        self.remaining = args.timeout
        self.done = False

        w = self.win = tk.Toplevel(root)
        w.title("ss-triage")
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

        self.results = queue.Queue()
        threading.Thread(target=self.classify, daemon=True).start()
        w.after(200, self.poll)

    def classify(self):
        try:
            if self.args.fake:
                res = fake_classify(self.shot)
            elif self.args.api:
                res = ask_api(self.shot, self.args.api_model)
            else:
                res = ask_model(self.shot, self.args.model)
            self.results.put(res)
        except Exception as e:
            self.results.put(e)

    def poll(self):
        if self.done:
            return
        try:
            res = self.results.get_nowait()
        except queue.Empty:
            self.win.after(200, self.poll)
            return
        if isinstance(res, Exception):
            # No guess -> no auto-accept. You decide.
            self.status.config(text=f"📸  {self.shot.name}\nAI couldn't tell ({res}). Your call.")
            return
        self.guess, self.reason = res
        self.buttons[self.guess].config(relief="sunken", bg="#ffd966")
        self.tick()

    def tick(self):
        if self.done:
            return
        label = dict((k, l) for k, _, l in CHOICES)[self.guess]
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
            apply_choice(self.shot, tier, self.args.folder, self.reason or "", by)
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
    ap.add_argument("--model", default="qwen2.5vl:3b")
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
    incoming = queue.Queue()
    threading.Thread(target=watcher, args=(args.folder, incoming), daemon=True).start()
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
                    Popup(root, shot, args, on_done=lambda: busy.__setitem__(0, False))
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
