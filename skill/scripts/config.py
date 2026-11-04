"""Loop Studio settings: config.json in the repo root (copy config.example.json), over the defaults below.

Read by the app (server.py) and the render scripts, so both agree on where ComfyUI, subjects and run data live.
Relative paths are taken from the repo root.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FILE = os.path.join(ROOT, "config.json")
DEFAULTS = {
    "comfy_dir": "../ComfyUI",           # the ComfyUI install that renders
    "comfy_python": None,                 # its Python; found in comfy_dir (venv, .venv or portable python_embeded) if unset
    "comfy_url": "http://127.0.0.1:8188",
    "subjects_dir": "skill/subjects",     # one folder per subject: subject.json + concepts.json
    "data_dir": None,                     # each subject's run data (marks, renders); unset: in the subject's own folder
    "font": "C:/Windows/Fonts/impact.ttf",  # caption font
    # what each render saves besides the 992 px MP4 and PNG frames (the masters): GIF, animated WebP and/or a
    # shareable MP4; long side in px, 12 fps (every other frame) or 24, and the concept's caption drawn on
    "outputs": {"gif": True, "webp": False, "mp4": False, "size": 480, "fps": 12, "captions": True},
    "agent": {"name": "Claude Code", "command": "claude", "env": {}},  # what the terminal drawer runs
}


def path(p):
    return os.path.normpath(os.path.join(ROOT, os.path.expanduser(p)))


def load():
    try:
        with open(FILE, encoding="utf-8") as f:
            user = json.load(f)
    except FileNotFoundError:
        user = {}
    cfg = DEFAULTS | user
    for key in ("agent", "outputs"):
        cfg[key] = DEFAULTS[key] | user.get(key, {})
    cfg["subjects_dir"] = path(cfg["subjects_dir"])
    cfg["data_dir"] = path(cfg["data_dir"]) if cfg["data_dir"] else cfg["subjects_dir"]
    cfg["comfy_dir"], cfg["comfy_python"] = find_comfy(cfg["comfy_dir"], cfg["comfy_python"])
    cfg["comfy_input"], cfg["comfy_output"] = os.path.join(cfg["comfy_dir"], "input"), os.path.join(cfg["comfy_dir"], "output")
    return cfg


def find_comfy(folder, python=None):
    # the ComfyUI folder (the portable download's outer folder works too) and the Python it runs with:
    # venv / .venv (git install, Desktop app) or the portable python_embeded, unless python is given
    folder = path(folder)
    if not os.path.isdir(os.path.join(folder, "models")) and os.path.isdir(os.path.join(folder, "ComfyUI", "models")):
        folder = os.path.join(folder, "ComfyUI")
    if python:
        return folder, path(python)
    candidates = [os.path.normpath(os.path.join(folder, *p)) for p in (
        ("venv", "Scripts", "python.exe"), (".venv", "Scripts", "python.exe"), ("..", "python_embeded", "python.exe"),
        ("venv", "bin", "python"), (".venv", "bin", "python"))]
    return folder, next((p for p in candidates if os.path.exists(p)), candidates[0])


def is_comfy(folder):
    return os.path.isdir(os.path.join(folder, "models"))


def guess_comfy():
    # ComfyUI installs on this PC to suggest: folders with "comfy" in their name next to Loop Studio, in the
    # home folder (and its AI, Desktop, Documents, Downloads) and at the drive roots
    home = os.path.expanduser("~")
    bases = [os.path.dirname(ROOT), home] + [os.path.join(home, d) for d in ("AI", "Desktop", "Documents", "Downloads")]
    bases += [f"{d}:\\" for d in "CDEFGH" if os.path.isdir(f"{d}:\\")] if os.name == "nt" else ["/opt"]
    found = []
    for base in bases:
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            if "comfy" in name.lower():
                folder = find_comfy(os.path.join(base, name))[0]
                if is_comfy(folder) and folder not in found:
                    found.append(folder)
    return found


def data_dir(cfg, subject):
    # a subject's run data folder: its "data" (relative to data_dir, or absolute), else data_dir/<name>
    return os.path.join(cfg["data_dir"], subject.get("data") or subject["name"])
