"""Loop Studio: a local front end for the loop-gifs skill, with ComfyUI as the renderer.

usage: .venv\\Scripts\\python.exe server.py [--open]   then open http://127.0.0.1:8190 (--open does it once it's up)

The skill's scripts do the work (render, GIF, review metrics, recycling Losses) and run under ComfyUI's venv Python;
this server only reads and writes the skill's subject files and run data, starts those scripts, relays ComfyUI's
progress, and hosts a terminal running your coding agent (config.json "agent"). It binds to 127.0.0.1 only and refuses requests from other origins,
because the terminal is a real shell.
"""
import asyncio
import hashlib
import html
import io
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile

import aiohttp
from aiohttp import web
from winpty import PtyProcess

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "skill", "scripts")
sys.path.insert(0, SCRIPTS)
import config  # noqa: E402  (the skill's scripts read the same settings)
import needs  # noqa: E402  (what the render graph needs from ComfyUI)

HOST, PORT = "127.0.0.1", int(os.environ.get("LOOPSTUDIO_PORT", 8190))
ORIGINS = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}
CFG = {}


def apply_config():
    # (re)reads config.json: at startup, after Settings saves it, and when it's edited by hand or by the agent.
    # Runs and scripts started from then on use the new folders; a new agent needs the terminal's Restart.
    global SUBJECTS, COMFY_PY, COMFY_URL, COMFY_INPUT, COMFY_OUTPUT, DATASETS, AGENT, ONBOARDED, CONFIG_MTIME
    mtime = os.path.getmtime(config.FILE) if os.path.exists(config.FILE) else 0
    loaded = config.load()  # raises on a half-written file; the watcher tries again
    CONFIG_MTIME = mtime
    CFG.clear()
    CFG.update(loaded)
    SUBJECTS = CFG["subjects_dir"]
    COMFY_PY, COMFY_URL = CFG["comfy_python"], CFG["comfy_url"]
    COMFY_INPUT, COMFY_OUTPUT = CFG["comfy_input"], CFG["comfy_output"]
    DATASETS = CFG["data_dir"]  # each subject's run data folder
    AGENT = CFG["agent"]
    ONBOARDED = os.path.join(DATASETS, ".onboarded")  # written once the agent has walked the user through their setup
    try:
        os.makedirs(SUBJECTS, exist_ok=True)
    except OSError as e:
        print("subjects folder:", e, flush=True)


try:
    apply_config()
except ValueError as e:
    sys.exit(f"config.json isn't valid JSON ({e}). Fix it, or delete it to start from the defaults.")
ONBOARD_PROMPT = ("This is my first time in Loop Studio. Run `python studio.py models` and tell me in plain words which render "
                  "modes my ComfyUI can run, what's missing for the others and how to get it, and roughly how long one GIF takes "
                  "in each on my PC. If models or nodes are missing, tell me which and point me to Settings > Requirements, "
                  "where each one downloads or installs with a button (don't download them yourself). "
                  "Recommend one to start with, and ask me to confirm before rendering anything. "
                  "Once I've confirmed, run `python studio.py onboarded`.")
# how an agent CLI takes an opening prompt: "" as an argument, a flag, None: type it in once it's up, False: a plain shell
PROMPT_FLAGS = {"claude": "", "codex": "", "gemini": "-i"}
SHELLS = {"powershell", "pwsh", "cmd", "bash", "sh", "wsl", "nu"}


def agent_argv():
    command = AGENT.get("command") or ""
    return list(command) if isinstance(command, list) else [a.strip('"') for a in shlex.split(command, posix=False)]


def prompt_flag():
    argv = agent_argv()
    if not argv:
        return None
    program = os.path.splitext(os.path.basename(argv[0]))[0].lower()
    return PROMPT_FLAGS.get(program, False if program in SHELLS else None)
STATIC = os.path.join(HERE, "static")
with open(os.path.join(STATIC, "banner.txt"), encoding="utf-8") as f:
    BANNER = f.read().rstrip("\n")  # LOOP STUDIO in ANSI Shadow: the page's splash, and the terminal's while its agent starts
UI_STATE = os.path.join(HERE, "state.json")  # what the user is looking at, for the agent in the terminal (studio.py state)
NAME = re.compile(r"^[a-z0-9_]{1,64}$")
FILE = re.compile(r"^[\w\-.]{1,200}\.(gif|webp|mp4)$")
STEM = re.compile(r"\w[\w\-.]{0,199}")  # a render's name: no path in it
# ComfyUI node ids in the skill's graph -> what the user sees
STAGES = {"1": "loading model", "3": "loading model", "4": "loading text encoder", "9": "encoding prompt",
          "14": "sampling", "15": "decoding", "20": "saving", "21": "saving video", "22": "saving frames", "40": "loading reference"}

events = set()  # websockets listening for app events


def broadcast(msg):
    data = json.dumps(msg)
    for ws in list(events):
        asyncio.ensure_future(ws.send_str(data))


# ---------- files ----------

def read_json(path, default):
    for attempt in range(4):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except ValueError as e:  # half-written by a script or the agent: try again; still broken: say which file
            if attempt == 3:
                raise web.HTTPInternalServerError(text=f"{path} isn't valid JSON ({e}). Fix it, or restore it.")
            time.sleep(0.1)


def all_subjects():
    # (name, subject.json) of each subject; one whose subject.json is broken is left out here (opening it says why)
    for name in sorted(os.listdir(SUBJECTS)):
        if not NAME.match(name):
            continue
        try:
            s = read_json(os.path.join(SUBJECTS, name, "subject.json"), None)
        except web.HTTPException:
            continue
        if s is not None:
            yield name, s


def write_json(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def subject_dir(name):
    if not NAME.match(name) or not os.path.exists(os.path.join(SUBJECTS, name, "subject.json")):
        raise web.HTTPNotFound(text=f"no subject {name}")
    return os.path.join(SUBJECTS, name)


def load_subject(name):
    return read_json(os.path.join(subject_dir(name), "subject.json"), {})


def data_path(subject, file):
    folder = config.data_dir(CFG, subject)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, file)


def renders_of(name, s):
    # the subject's renders, plus any finished examples bundled in its folder (examples/examples.json + GIFs),
    # so a fresh install has something to look at
    renders = read_json(data_path(s, "renders.json"), [])
    have = {r["stem"] for r in renders}
    return renders + [r for r in read_json(os.path.join(SUBJECTS, name, "examples", "examples.json"), []) if r["stem"] not in have]


def script_env():
    # UTF-8 output whatever the Windows code page, so prompts and paths with any characters come through
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


async def run_script(*args, stdin=None):
    # one of the skill's scripts under ComfyUI's Python; returns (code, output)
    if not os.path.isfile(COMFY_PY or ""):
        raise web.HTTPBadRequest(text=f"ComfyUI's Python isn't at {COMFY_PY}: set the ComfyUI folder in Settings.")
    proc = await asyncio.create_subprocess_exec(COMFY_PY, *args, cwd=SCRIPTS, stdin=asyncio.subprocess.PIPE if stdin else None,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=script_env())
    out, _ = await proc.communicate(stdin.encode() if stdin else None)
    return proc.returncode, out.decode("utf-8", "replace")


# ---------- subjects, concepts, renders, marks ----------

async def list_subjects(request):
    rows = []
    for name in sorted(os.listdir(SUBJECTS)):
        if not NAME.match(name) or not os.path.exists(os.path.join(SUBJECTS, name, "subject.json")):
            continue
        try:
            s = load_subject(name)
            renders = renders_of(name, s)
            results = read_json(data_path(s, "results.json"), {})
            rows.append({"name": name, "subject": s.get("subject", ""), "has_reference": bool(s.get("reference")),
                         "concepts": len(read_json(os.path.join(SUBJECTS, name, "concepts.json"), [])),
                         "renders": len(renders), "wins": sum(r.get("v") == "win" for r in results.values())})
        except web.HTTPException as e:  # one broken file mustn't hide every subject; opening this one shows the error
            rows.append({"name": name, "subject": "", "has_reference": False, "concepts": 0, "renders": 0, "wins": 0,
                         "error": e.text})
    return web.json_response(rows)


async def get_subject(request):
    name = request.match_info["name"]
    s = load_subject(name)
    return web.json_response({"subject": s, "data_dir": config.data_dir(CFG, s), "folder": subject_dir(name),
                              "concepts": read_json(os.path.join(subject_dir(name), "concepts.json"), []),
                              "renders": renders_of(name, s),
                              "results": read_json(data_path(s, "results.json"), {})})


def check_description(text, reference):
    # with a reference image the prompt has to say <Picture 1>, or H3 isn't told who to animate
    if not reference:
        return text
    if not text:
        return "the person from <Picture 1>"
    if "<Picture 1>" not in text:
        raise web.HTTPBadRequest(text="The description has to mention <Picture 1>, e.g. “the man from <Picture 1>, with …”")
    return text


async def create_subject(request):
    form = await request.post()
    name = (form.get("name") or "").strip().lower()
    if not NAME.match(name):
        raise web.HTTPBadRequest(text="name: lowercase letters, digits and _ only")
    folder = os.path.join(SUBJECTS, name)
    if os.path.exists(folder):
        raise web.HTTPConflict(text=f"subject {name} exists")
    reference = None
    image = form.get("image")
    description = check_description(form.get("subject", "").strip(), isinstance(image, web.FileField))
    if isinstance(image, web.FileField):
        # flatten transparency onto white (H3 reads alpha as black) and store it in ComfyUI/input/loopstudio
        reference = f"loopstudio/{name}_ref.png"
        os.makedirs(os.path.join(COMFY_INPUT, "loopstudio"), exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(image.filename)[1]) as tmp:
            shutil.copyfileobj(image.file, tmp)
        try:
            code, out = await run_script("-c", "import sys; from PIL import Image\n"
                                         "im = Image.open(sys.argv[1]).convert('RGBA'); bg = Image.new('RGBA', im.size, 'white'); "
                                         "bg.alpha_composite(im); bg.convert('RGB').save(sys.argv[2])",
                                         tmp.name, os.path.join(COMFY_INPUT, reference))
        finally:
            os.unlink(tmp.name)
        if code:
            raise web.HTTPBadRequest(text="couldn't read the image: " + out[-300:])
    try:
        os.makedirs(folder)
    except FileExistsError:
        raise web.HTTPConflict(text=f"subject {name} exists")
    subject = {"name": name, "reference": reference, "subject": description, "keep": form.get("keep", "").strip(),
               "he": form.get("he", "He").strip() or "He", "his": form.get("his", "his").strip() or "his",
               "base_tags": [t.strip() for t in form.get("base_tags", "").split(",") if t.strip()],
               "output": f"loopstudio/{name}", "data": name}
    write_json(os.path.join(folder, "subject.json"), subject)
    write_json(os.path.join(folder, "concepts.json"), [])
    return web.json_response(subject)


async def update_subject(request):
    name = request.match_info["name"]
    path = os.path.join(subject_dir(name), "subject.json")
    s = read_json(path, {})
    body = await request.json()
    for key in ("subject", "keep", "he", "his"):
        if key in body:
            s[key] = body[key] or ""
    s["subject"] = check_description(s.get("subject", "").strip(), s.get("reference"))
    if "base_tags" in body:
        s["base_tags"] = [t.strip() for t in body["base_tags"] if t.strip()]
    write_json(path, s)
    return web.json_response(s)


async def save_concepts(request):
    name = request.match_info["name"]
    concepts = await request.json()
    seen = set()
    for c in concepts:
        if not NAME.match(c.get("name", "")) or c["name"] in seen:
            raise web.HTTPBadRequest(text=f"bad or duplicate concept name: {c.get('name')!r}")
        seen.add(c["name"])
        why = needs.concept_problem(c)
        if why:
            raise web.HTTPBadRequest(text=f"{c['name']}: {why}")
    path = os.path.join(subject_dir(name), "concepts.json")
    read_json(path, [])  # a file that can't be read says so here, instead of being replaced by what the page has
    write_json(path, concepts)
    return web.json_response({"saved": len(concepts)})


async def preview(request):
    name = request.match_info["name"]
    subject_dir(name)
    concept = await request.json()
    why = needs.concept_problem(concept)
    if why:
        return web.json_response({"prompt": why[0].upper() + why[1:], "ok": False})
    code, out = await run_script("loop_gifs.py", name, "preview", stdin=json.dumps(concept))
    return web.json_response({"prompt": out.strip(), "ok": code == 0})


async def save_mark(request):
    name = request.match_info["name"]
    s = load_subject(name)
    body = await request.json()
    if not isinstance(body.get("stem"), str) or not STEM.fullmatch(body["stem"]):
        raise web.HTTPBadRequest(text="bad render name")
    path = data_path(s, "results.json")
    results = read_json(path, {})
    r = results.get(body["stem"], {})
    for key in ("v", "note", "tags"):
        if key in body:
            if body[key]:
                r[key] = body[key]
            else:
                r.pop(key, None)
    if r:
        results[body["stem"]] = r
    else:
        results.pop(body["stem"], None)
    write_json(path, results)
    return web.json_response(r)


async def refresh(request):
    name = request.match_info["name"]
    subject_dir(name)
    code, out = await run_script("review.py", name)
    return web.json_response({"ok": code == 0, "output": out[-2000:]})


async def recycle_losers(request):
    name = request.match_info["name"]
    subject_dir(name)
    code, out = await run_script("recycle.py", name)
    broadcast({"type": "renders_changed", "subject": name})
    return web.json_response({"ok": code == 0, "output": out})


def inside(path, base):
    path, base = os.path.realpath(path), os.path.realpath(base)
    return path.startswith(base + os.sep)


PS_QUOTES = "['\u2018\u2019\u201a\u201b]"  # PowerShell ends a '...' string at any of these


async def recycle(paths):
    # Recycle Bin, not a hard delete, so a deleted subject can be restored
    script = "Add-Type -AssemblyName Microsoft.VisualBasic; " + "; ".join(
        f"[Microsoft.VisualBasic.FileIO.FileSystem]::{'DeleteDirectory' if os.path.isdir(p) else 'DeleteFile'}"
        f"('{re.sub(PS_QUOTES, lambda m: m.group() * 2, p)}', 'OnlyErrorDialogs', 'SendToRecycleBin')" for p in paths)
    proc = await asyncio.create_subprocess_exec("powershell", "-NoProfile", "-Command", script,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace")


async def delete_subject(request):
    # sends the subject's folder, and optionally its renders, run data and reference image, to the Recycle Bin;
    # anything another subject also points at stays.
    name = request.match_info["name"]
    folder = subject_dir(name)
    s = load_subject(name)
    body = await request.json()
    if any(r["subject"] == name and r["status"] in ("queued", "running") for r in runs.runs):
        raise web.HTTPConflict(text=f"{name} has a render queued or running; stop it in Runs first")
    others = [o for n, o in all_subjects() if n != name]
    paths = [folder]
    if body.get("renders") and s.get("output"):
        p = os.path.join(COMFY_OUTPUT, s["output"])
        if inside(p, COMFY_OUTPUT) and not any(o.get("output") == s["output"] for o in others):
            paths.append(p)
    if body.get("data"):
        p = config.data_dir(CFG, s)
        if inside(p, DATASETS) and not any(os.path.realpath(config.data_dir(CFG, o)) == os.path.realpath(p) for o in others):
            paths.append(p)
    if body.get("reference") and s.get("reference"):
        p = os.path.join(COMFY_INPUT, s["reference"])
        if inside(p, COMFY_INPUT) and not any(o.get("reference") == s["reference"] for o in others):
            paths.append(p)
    # run data kept in the subject's own folder goes with it
    paths = list(dict.fromkeys(os.path.realpath(p) for p in paths if os.path.exists(p)))
    code, out = await recycle(paths)
    if code:
        raise web.HTTPInternalServerError(text="couldn't recycle: " + out[-500:])
    broadcast({"type": "subjects"})
    return web.json_response({"recycled": paths})


async def media(request):
    name, file = request.match_info["name"], request.match_info["file"]
    if not FILE.match(file):
        raise web.HTTPNotFound()
    for base in (os.path.join(COMFY_OUTPUT, load_subject(name)["output"]), os.path.join(subject_dir(name), "examples")):
        base = os.path.realpath(base)
        path = os.path.realpath(os.path.join(base, file))
        if path.startswith(base + os.sep) and os.path.exists(path):
            break
    else:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


async def show_in_folder(request):
    # opens Explorer on this PC (the server is local): at one render's file, or the subject's own folder
    name = request.match_info["name"]
    file = (await request.json()).get("file")
    if file:
        if not FILE.match(file):
            raise web.HTTPBadRequest(text="bad file name")
        base = os.path.realpath(os.path.join(COMFY_OUTPUT, load_subject(name)["output"]))
        path = os.path.realpath(os.path.join(base, file))
        if not path.startswith(base + os.sep) or not os.path.exists(path):
            raise web.HTTPNotFound(text=f"{file} isn't in {base} any more")
        subprocess.Popen(f'explorer /select,"{path}"')  # Windows paths can't hold a quote
    else:
        subprocess.Popen(["explorer", os.path.realpath(subject_dir(name))])
    return web.json_response({"opened": file or name})


async def reference(request):
    s = load_subject(request.match_info["name"])
    if not s.get("reference"):
        raise web.HTTPNotFound()
    base = os.path.realpath(COMFY_INPUT)
    path = os.path.realpath(os.path.join(base, s["reference"]))
    if not path.startswith(base + os.sep) or not os.path.exists(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path)


# ---------- runs: one render batch at a time, through loop_gifs.py ----------

class Runs:
    def __init__(self):
        self.runs = []      # newest last
        self.proc = None
        self.wake = asyncio.Event()

    def prompt_ids(self):
        # the ComfyUI jobs a live run is waiting for (it makes their outputs itself)
        return {r["prompt_id"] for r in self.runs if r["status"] == "running" and r["prompt_id"]}

    def note(self, prompt_id, **progress):
        # ComfyUI's progress on a run's current job (stage, step value/max), for `studio.py render` to show
        for r in self.runs:
            if r["status"] == "running" and r["prompt_id"] == prompt_id:
                r["progress"] = {**(r["progress"] or {}), **progress}

    def add(self, subject, concepts, seeds, fast, redo, fmt="square"):
        # no seeds: loop_gifs.py gives each concept one it hasn't been rendered with; fmt: square, vertical or wide
        args = ["loop_gifs.py", subject, "render", *concepts, *[f"--seed={s}" for s in seeds]]
        args += ["--fast"] if fast else []
        args += [f"--{fmt}"] if fmt != "square" else []
        args += ["--redo"] if redo else []
        run = {"id": uuid.uuid4().hex[:8], "subject": subject, "concepts": concepts, "seeds": seeds, "fast": fast, "redo": redo,
               "format": fmt, "args": args, "status": "queued", "log": [], "created": time.time(), "prompt_id": None,
               "rendered": 0, "skipped": 0, "failed": 0, "progress": None}
        self.runs.append(run)
        self.wake.set()
        broadcast({"type": "runs"})
        return run

    async def worker(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            for run in [r for r in self.runs if r["status"] == "queued"]:
                if run["status"] != "queued":  # stopped while the ones before it rendered
                    continue
                try:
                    await self.execute(run)
                except Exception as e:  # never leave a run "running" with nothing behind it, or stop the queue
                    self.proc = None
                    self.fail(run, f"Loop Studio couldn't run it: {e}")

    def fail(self, run, line):
        run["log"].append(line)
        broadcast({"type": "run_log", "id": run["id"], "line": line})
        run["status"], run["ended"] = "failed", time.time()
        broadcast({"type": "runs"})

    async def execute(self, run):
        run["status"], run["started"] = "running", time.time()
        broadcast({"type": "runs"})
        if not os.path.isfile(COMFY_PY or ""):
            return self.fail(run, f"ComfyUI's Python isn't at {COMFY_PY}: set the ComfyUI folder in Settings, then render again.")
        self.proc = await asyncio.create_subprocess_exec(COMFY_PY, "-u", *run["args"], cwd=SCRIPTS, env=script_env(),
                                                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        async for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            run["log"].append(line)
            m = re.match(r"queued (\S+) (\S+)", line)
            if m:
                run["prompt_id"], run["current"] = m.group(2), m.group(1)
            # what each render came to, so a run that only skipped or lost a render doesn't read "done"
            if line.startswith("skip "):
                run["skipped"] += 1
            elif re.match(r"\w{8} (GIF|WEBP|MP4) ", line):
                run["rendered"] += 1
            elif line.endswith("failed or interrupted, nothing saved") or re.match(r"\w{8} outputs failed: ", line):
                run["failed"] += 1
            broadcast({"type": "run_log", "id": run["id"], "line": line})
            if re.search(r" (GIF|WEBP|MP4) ", line) or "renders ->" in line:
                broadcast({"type": "renders_changed", "subject": run["subject"]})
        code = await self.proc.wait()
        self.proc = None
        if run["status"] == "running":
            run["status"] = ("failed" if code or run["failed"] else
                             "skipped" if run["skipped"] and not run["rendered"] else "done")
        run["ended"], run["progress"] = time.time(), None
        broadcast({"type": "runs"})
        broadcast({"type": "renders_changed", "subject": run["subject"]})

    async def stop(self, run_id):
        run = next((r for r in self.runs if r["id"] == run_id), None)
        if not run:
            raise web.HTTPNotFound()
        if run["status"] == "queued":
            run["status"] = "stopped"
        elif run["status"] == "running" and self.proc:
            run["status"] = "stopped"
            self.proc.terminate()
            # the script is gone, but ComfyUI is still rendering its current job: interrupt only that one
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
                    q = await (await http.get(COMFY_URL + "/queue")).json()
                    if any(e[1] == run["prompt_id"] for e in q["queue_running"]):
                        await http.post(COMFY_URL + "/interrupt")
                    if any(e[1] == run["prompt_id"] for e in q["queue_pending"]):
                        await http.post(COMFY_URL + "/queue", json={"delete": [run["prompt_id"]]})
            except Exception as e:  # ComfyUI down: nothing of it left to cancel
                print("stop:", e, flush=True)
        broadcast({"type": "runs"})
        return run


runs = None


async def start_run(request):
    name = request.match_info["name"]
    subject_dir(name)
    body = await request.json()
    concepts = body.get("concepts") or []
    known = {c["name"] for c in read_json(os.path.join(SUBJECTS, name, "concepts.json"), [])}
    if not concepts or any(c not in known for c in concepts):
        raise web.HTTPBadRequest(text="pick saved concepts to render")
    seeds = [int(s) for s in body.get("seeds") or []]  # none: a new seed for each concept
    fmt = body.get("format") or "square"
    if fmt not in ("square", "vertical", "wide"):
        raise web.HTTPBadRequest(text="format: square, vertical or wide")
    if not os.path.isfile(COMFY_PY or ""):
        raise web.HTTPBadRequest(text=f"Can't render yet: ComfyUI's Python isn't at {COMFY_PY}. Set the ComfyUI folder in Settings.")
    # refuse up front what ComfyUI can't run (missing node, model, too old), instead of a failed run with a traceback
    s, by_name = load_subject(name), {c["name"]: c for c in read_json(os.path.join(SUBJECTS, name, "concepts.json"), [])}
    bad = [f"{c}: {needs.concept_problem(by_name[c])}" for c in concepts if needs.concept_problem(by_name[c])]
    if bad:
        raise web.HTTPBadRequest(text="Can't render yet: " + "; ".join(bad))
    model = s.get("model") or ("fast" if body.get("fast") else "stock")
    stop = needs.blocking(await check_all(), fast=model == "fast", pika=model == "pika",
                          upscale=any(by_name[c].get("upscale", s.get("upscale")) for c in concepts), gif=CFG["outputs"]["gif"],
                          stack=any(needs.loras(model, s, by_name[c]) is needs.DEFAULT_LORAS for c in concepts))
    if stop:
        raise web.HTTPBadRequest(text="Can't render yet, ComfyUI is missing: " + "; ".join(i["name"] for i in stop)
                                 + ". Settings > Requirements says how to fix each.")
    run = runs.add(name, concepts, seeds, model == "fast", bool(body.get("redo")), fmt)
    return web.json_response({k: v for k, v in run.items() if k != "log"})


async def list_runs(request):
    return web.json_response([{k: v for k, v in r.items() if k != "log"} | {"log_tail": r["log"][-200:], "log_total": len(r["log"])}
                              for r in runs.runs[-30:]])


async def stop_run(request):
    run = await runs.stop(request.match_info["id"])
    return web.json_response({k: v for k, v in run.items() if k != "log"})


# ---------- settings: config.json, edited in the app's Settings panel ----------

def settings_changed():
    # config.json applied: the page reloads subjects and agent name; the terminal keeps its agent until Restart
    old_agent = AGENT
    apply_config()
    broadcast({"type": "settings"})
    return AGENT != old_agent and terminal.alive()


async def get_settings(request):
    settings = {k: v for k, v in CFG.items() if k not in ("comfy_input", "comfy_output")}
    if settings["data_dir"] == settings["subjects_dir"]:
        settings["data_dir"] = ""  # the default: each subject's run data in its own folder
    return web.json_response({"settings": settings,
                              "python_auto": not read_json(config.FILE, {}).get("comfy_python"), "file": config.FILE})


async def save_settings(request):
    body = await request.json()
    text = lambda v: str(v or "").strip()
    out = body.get("outputs", {})
    if out.get("fps") not in (12, 24) or not 128 <= int(out.get("size", 0)) <= 992:
        raise web.HTTPBadRequest(text="outputs: fps 12 or 24, size 128-992 px")
    agent = body.get("agent", {})
    if not text(agent.get("command")):
        raise web.HTTPBadRequest(text="the agent needs a command (e.g. claude)")
    if not text(body.get("comfy_url")).startswith("http"):
        raise web.HTTPBadRequest(text="ComfyUI URL: http://host:port")
    user = read_json(config.FILE, {})  # keeps keys the panel doesn't show
    user.update({k: text(body.get(k)) for k in ("comfy_dir", "subjects_dir", "data_dir", "comfy_url", "font")})
    user["comfy_python"] = text(body.get("comfy_python")) or None
    user["outputs"] = {"gif": bool(out.get("gif")), "webp": bool(out.get("webp")), "mp4": bool(out.get("mp4")), "size": int(out["size"]), "fps": out["fps"],
                       "captions": bool(out.get("captions"))}
    user["agent"] = {"name": text(agent.get("name")) or "Agent", "command": text(agent["command"]),
                     "env": {text(k): text(v) for k, v in (agent.get("env") or {}).items() if text(k)}}
    write_json(config.FILE, user)
    restart_agent = settings_changed()
    return web.json_response({"restart_agent": restart_agent})


PIP_NAMES = {"cv2": "opencv-python-headless", "av": "av", "PIL": "pillow", "numpy": "numpy"}  # what the scripts import


async def missing_packages(python):
    # what the render scripts import that this Python doesn't have
    probe = f"import importlib.util as u; print(' '.join(m for m in {tuple(PIP_NAMES)!r} if not u.find_spec(m)))"
    proc = await asyncio.create_subprocess_exec(python, "-c", probe, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    text, _ = await proc.communicate()
    return text.decode("utf-8", "replace").split() if proc.returncode == 0 else list(PIP_NAMES)


async def check_all():
    # needs.py's checks of the running ComfyUI, and the packages the scripts import in ComfyUI's Python
    items = await asyncio.to_thread(needs.check, COMFY_URL, CFG["outputs"]["gif"])
    if os.path.isfile(COMFY_PY or ""):
        missing = await missing_packages(COMFY_PY)
        names = ", ".join(PIP_NAMES[m] for m in missing)
        items.insert(1, {"key": "packages", "name": "Packages in ComfyUI's Python" + (f": {names}" if missing else ""), "ok": not missing,
                         "need": "required", "core": True, "note": "",
                         "fix": f"Install them: the Install button in Settings > Requirements, or pip install {names} with ComfyUI's Python." if missing else "",
                         "get": {"kind": "packages", "missing": missing} if missing else None})
    return items


async def requirements(request):
    return web.json_response(await check_all())


async def check_comfy(request):
    # the Settings panel's live check of a ComfyUI folder: is it one, its Python, and the packages the scripts need there
    body = await request.json()
    folder, python = config.find_comfy(body.get("comfy_dir") or "", body.get("comfy_python") or None)
    out = {"folder": folder, "python": python, "comfy": config.is_comfy(folder),
           "python_ok": os.path.isfile(python), "missing": []}
    if out["python_ok"]:
        out["missing"] = await missing_packages(python)
    return web.json_response(out)


async def comfy_guess(request):
    return web.json_response(await asyncio.to_thread(config.guess_comfy))


async def set_comfy_folder(request):
    # the first-boot setup: just the ComfyUI folder (its Python is found in it)
    folder, python = config.find_comfy((await request.json()).get("comfy_dir") or "")
    if not config.is_comfy(folder) or not os.path.isfile(python):
        raise web.HTTPBadRequest(text=f"no ComfyUI with its Python in {folder}")
    user = read_json(config.FILE, {})
    user["comfy_dir"], user["comfy_python"] = folder, None
    write_json(config.FILE, user)
    settings_changed()
    return web.json_response({"comfy_dir": folder, "comfy_python": python})


def comfy_ok():
    return config.is_comfy(CFG["comfy_dir"]) and os.path.isfile(COMFY_PY or "")


async def install_deps(request):
    # pip-installs the missing packages into ComfyUI's Python (the Settings panel's Install buttons): the folder being
    # typed in there, or the one in use
    body = await request.json()
    python = config.find_comfy(body["comfy_dir"], body.get("comfy_python") or None)[1] if body.get("comfy_dir") else COMFY_PY
    packages = [PIP_NAMES[m] for m in body.get("missing", []) if m in PIP_NAMES]
    if not packages or not os.path.isfile(python):
        raise web.HTTPBadRequest(text="nothing to install, or no ComfyUI Python found")
    proc = await asyncio.create_subprocess_exec(python, "-m", "pip", "install", *packages,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    text, _ = await proc.communicate()
    return web.json_response({"ok": proc.returncode == 0, "output": text.decode("utf-8", "replace")[-1500:]})

# ---------- getting the models and nodes: Settings > Requirements' Download, Install and Open folder buttons ----------

MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft")
FETCHES = {}  # check key -> a model download: file, dest, size, done, speed, state (queued/running/done/failed/cancelled), error
FETCH_LOCK = asyncio.Lock()  # one download at a time, in the order asked: each file finishes (and has its space) before the next


async def comfy_folders():
    # {folder type: [folders]} as the running ComfyUI searches them, extra_model_paths.yaml included (but not the
    # folders inside its output folder); ComfyUI/models/<type> when ComfyUI can't say
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
            paths = await (await http.get(COMFY_URL + "/internal/folder_paths")).json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        paths = {}
    out_dir = os.path.normcase(os.path.realpath(COMFY_OUTPUT)) + os.sep
    out = {}
    for t in {d[3] for d in needs.DOWNLOADS.values()} | {"custom_nodes"}:
        found = paths.get(t) or []
        found = found[0] if found and isinstance(found[0], list) else found  # older ComfyUI: [folders, extensions]
        folders = [os.path.normpath(p) for p in found
                   if not (os.path.normcase(os.path.realpath(p)) + os.sep).startswith(out_dir)]
        out[t] = list(dict.fromkeys(folders)) or [os.path.join(CFG["comfy_dir"], t if t == "custom_nodes" else os.path.join("models", t))]
    return out


def count_models(folder):
    # model files in a folder and one level of subfolders: the folder with the most is where this PC keeps that type
    n = 0
    try:
        for e in os.scandir(folder):
            if e.is_file() and e.name.lower().endswith(MODEL_EXTS):
                n += 1
            elif e.is_dir():
                try:
                    n += sum(1 for f in os.scandir(e.path) if f.name.lower().endswith(MODEL_EXTS))
                except OSError:
                    pass
    except OSError:
        return 0
    return n


async def model_folders(request):
    # per folder type: ComfyUI's folders with how many models each holds, and the one to suggest: one named for its
    # type that holds models (a diffusion_models folder over a checkpoints folder that's also read as unet), else the
    # fullest; on a fresh install ComfyUI's own models folder, which it lists first
    def named(t, p):
        base = os.path.basename(p).lower()
        return base.rstrip("s") == t.rstrip("s") or (t == "diffusion_models" and base == "unet")

    out = {}
    for t, paths in (await comfy_folders()).items():
        rows = [{"path": p, "files": await asyncio.to_thread(count_models, p)} for p in paths]
        best = max(rows, key=lambda r: (r["files"] > 0, named(t, r["path"]), r["files"]))
        out[t] = {"folders": rows, "default": best["path"]}
    return web.json_response(out)


def fetch_view(f):
    return {k: v for k, v in f.items() if k != "task"}


async def list_fetches(request):
    if "scan" in request.query:
        # Settings > Requirements opened: a .part left from before the app restarted can be resumed like any other
        folders = await comfy_folders()
        for key, (_, path, size, folder_type, sub) in needs.DOWNLOADS.items():
            file = path.rsplit("/", 1)[-1]
            found = [p for p in (os.path.join(folder, sub, file) for folder in folders[folder_type]) if os.path.isfile(p + ".part")]
            if found and key not in FETCHES:
                FETCHES[key] = {"key": key, "file": file, "dest": found[0], "size": size, "done": os.path.getsize(found[0] + ".part"),
                                "speed": 0, "state": "cancelled", "error": None}
    return web.json_response({k: fetch_view(f) for k, f in FETCHES.items()})


async def start_fetch(request):
    # queues model downloads from Hugging Face, each into one of ComfyUI's folders for its type (in the subfolder
    # needs.DOWNLOADS names): {"items": [{"key", "folder"}, ...]}. A file goes to <file>.part first, so a cancelled or
    # broken download resumes where it stopped.
    items = (await request.json()).get("items") or []
    folders = await comfy_folders()
    for it in items:  # check them all before starting any
        key = it.get("key")
        if key not in needs.DOWNLOADS:
            raise web.HTTPBadRequest(text=f"no download for {key}")
        folder_type = needs.DOWNLOADS[key][3]
        if os.path.normcase(os.path.normpath(it.get("folder") or ".")) not in {os.path.normcase(p) for p in folders[folder_type]}:
            raise web.HTTPBadRequest(text=f"pick one of ComfyUI's {folder_type} folders for {key}")
    for it in items:
        key, folder = it["key"], os.path.normpath(it["folder"])
        if key in FETCHES and FETCHES[key]["state"] in ("queued", "running"):
            continue
        repo, path, size, folder_type, sub = needs.DOWNLOADS[key]
        dest = os.path.join(folder, sub, path.rsplit("/", 1)[-1]) if sub else os.path.join(folder, path.rsplit("/", 1)[-1])
        f = {"key": key, "file": os.path.basename(dest), "dest": dest, "size": size, "done": 0, "speed": 0, "state": "queued", "error": None}
        if os.path.isfile(dest) and os.path.getsize(dest) == size:
            f.update(done=size, state="done", existed=True)
        else:
            f["done"] = os.path.getsize(dest + ".part") if os.path.isfile(dest + ".part") else 0
            f["task"] = asyncio.create_task(fetch(f, needs.download_url(key)))
        FETCHES[key] = f
    broadcast({"type": "downloads"})
    return web.json_response({k: fetch_view(f) for k, f in FETCHES.items()})


async def fetch(f, url):
    try:
        async with FETCH_LOCK:
            f["state"] = "running"
            broadcast({"type": "downloads"})
            await fetch_file(f, url)
    except asyncio.CancelledError:
        f.update(state="cancelled", speed=0)  # the .part stays: the next Download resumes it
    except Exception as e:  # network, disk, HTTP: shown on the row, and the .part is kept for a retry
        f.update(state="failed", speed=0, error=str(e) or type(e).__name__)
    finally:
        broadcast({"type": "downloads"})


async def fetch_file(f, url):
    part = f["dest"] + ".part"
    os.makedirs(os.path.dirname(f["dest"]), exist_ok=True)
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if have > f["size"]:  # not a piece of this file
        have = 0
    free = shutil.disk_usage(os.path.dirname(f["dest"])).free
    if free < f["size"] - have:
        raise OSError(f"not enough space: it needs {(f['size'] - have) / 1e9:.1f} GB, {free / 1e9:.1f} GB free there")
    headers = {"Range": f"bytes={have}-"} if have else {}
    if os.environ.get("HF_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["HF_TOKEN"]
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
    async with aiohttp.ClientSession(timeout=timeout) as http, http.get(url, headers=headers) as r:
        if r.status == 200:
            have = 0  # no resume from this server: start over
        elif not (r.status == 206 or (r.status == 416 and have == f["size"])):
            r.raise_for_status()
        f["done"] = have
        if r.status != 416:  # 416: it's all here already, and the body is only the server's error page
            with open(part, "ab" if have else "wb") as out:
                last, last_done = time.monotonic(), have
                async for chunk in r.content.iter_chunked(1 << 20):
                    await asyncio.to_thread(out.write, chunk)
                    f["done"] += len(chunk)
                    now = time.monotonic()
                    if now - last >= 1:
                        f["speed"], last, last_done = (f["done"] - last_done) / (now - last), now, f["done"]
                        broadcast({"type": "downloads"})
    got = os.path.getsize(part)
    if got != f["size"]:
        raise OSError(f"stopped at {got / 1e9:.2f} of {f['size'] / 1e9:.2f} GB; Download again to resume")
    os.replace(part, f["dest"])
    f.update(state="done", speed=0)


async def cancel_fetch(request):
    f = FETCHES.get(request.match_info["key"])
    if f and f["state"] in ("queued", "running") and f.get("task"):
        f["task"].cancel()
    return web.json_response({"cancelled": bool(f)})


async def install_node(request):
    # puts a custom node pack into ComfyUI's custom_nodes: git clone, or GitHub's zip when git isn't installed; then its
    # requirements into ComfyUI's Python. ComfyUI loads it on its next start (the user restarts it).
    key = (await request.json()).get("key")
    if key not in needs.NODE_INSTALLS:
        raise web.HTTPBadRequest(text=f"no node pack for {key}")
    repo, name = needs.NODE_INSTALLS[key]
    base = (await comfy_folders())["custom_nodes"][0]
    dest = os.path.join(base, name)
    log = []
    if os.path.isdir(dest):
        pass  # there from an earlier Install, whose requirements may have failed: they get another try below
    elif shutil.which("git"):
        proc = await asyncio.create_subprocess_exec("git", "clone", "--depth", "1", repo, dest,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        log.append((await proc.communicate())[0].decode("utf-8", "replace"))
        if proc.returncode:
            return web.json_response({"ok": False, "output": "git clone failed:\n" + log[-1][-1500:]})
    else:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as http:
                r = await http.get(repo + "/archive/HEAD.zip")
                r.raise_for_status()
                data = await r.read()
            await asyncio.to_thread(unzip_into, data, dest)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, zipfile.BadZipFile) as e:
            return web.json_response({"ok": False, "output": f"couldn't download {repo}: {e}"})
        log.append(f"downloaded {repo} (git isn't installed, so it can't be updated with git pull)")
    reqs = os.path.join(dest, "requirements.txt")
    if os.path.isfile(reqs) and os.path.isfile(COMFY_PY or ""):
        proc = await asyncio.create_subprocess_exec(COMFY_PY, "-m", "pip", "install", "-r", reqs,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        log.append((await proc.communicate())[0].decode("utf-8", "replace")[-1500:])
        if proc.returncode:
            return web.json_response({"ok": False, "output": f"{name} is in custom_nodes, but its requirements failed:\n" + log[-1]})
    return web.json_response({"ok": True, "output": f"Installed {name} in {base}. Restart ComfyUI to load it, then Check again."})


async def install_gifski(request):
    # gifski's Windows program into Loop Studio's tools/ folder, checked against needs.GIFSKI_SHA256 first
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as http:
            r = await http.get(needs.GIFSKI_ZIP)
            r.raise_for_status()
            data = await r.read()
        await asyncio.to_thread(unpack_gifski, data)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
        return web.json_response({"ok": False, "output": f"Couldn't install gifski from {needs.GIFSKI_ZIP}: {e or type(e).__name__}"})
    return web.json_response({"ok": True, "output": f"Installed gifski in {needs.TOOLS}."})


def unpack_gifski(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        exe = z.read(needs.GIFSKI_EXE)
    if hashlib.sha256(exe).hexdigest() != needs.GIFSKI_SHA256:
        raise ValueError("the download doesn't match the gifski this version of Loop Studio expects, so it wasn't installed")
    os.makedirs(needs.TOOLS, exist_ok=True)
    path = os.path.join(needs.TOOLS, "gifski.exe")
    with open(path + ".tmp", "wb") as f:
        f.write(exe)
    os.replace(path + ".tmp", path)


def unzip_into(data, dest):
    # GitHub's zip holds one top folder (<repo>-<branch>): its contents become dest
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(tmp)
        tops = os.listdir(tmp)
        shutil.move(os.path.join(tmp, tops[0]) if len(tops) == 1 else tmp, dest)


async def open_model_folder(request):
    # Explorer at one of ComfyUI's model folders (with the subfolder a download goes in), made first if it's missing
    body = await request.json()
    folder, sub = os.path.normpath(body.get("folder") or "."), body.get("sub") or ""
    allowed = {os.path.normcase(p) for paths in (await comfy_folders()).values() for p in paths}
    if os.path.normcase(folder) not in allowed or sub not in {d[4] for d in needs.DOWNLOADS.values()}:
        raise web.HTTPBadRequest(text="not one of ComfyUI's model folders")
    path = os.path.join(folder, sub) if sub else folder
    os.makedirs(path, exist_ok=True)
    subprocess.Popen(["explorer", path])
    return web.json_response({"opened": path})


def pick_path(kind, initial, title):
    # a native folder / file dialog on this PC (the server is local), for the Settings panel's Browse buttons
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        start = initial if os.path.isdir(initial or "") else os.path.dirname(initial or "") or None
        ask = filedialog.askdirectory if kind == "folder" else filedialog.askopenfilename
        return ask(parent=root, initialdir=start, title=title) or None
    finally:
        root.destroy()


async def pick(request):
    body = await request.json()
    path = await asyncio.to_thread(pick_path, body.get("kind", "folder"), body.get("initial"), body.get("title") or "Choose")
    return web.json_response({"path": os.path.normpath(path) if path else None})


# ---------- ComfyUI status and live progress ----------

async def comfy_status(request):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as http:
            stats = await (await http.get(COMFY_URL + "/system_stats")).json()
            q = await (await http.get(COMFY_URL + "/queue")).json()
        dev = stats["devices"][0]
        return web.json_response({"online": True, "version": stats["system"].get("comfyui_version"),
                                  # "cuda:0 NVIDIA GeForce RTX 4090 : cudaMallocAsync" -> "NVIDIA GeForce RTX 4090"
                                  "gpu": re.sub(r"^\w+:\d+\s*", "", dev["name"]).split(" : ")[0],
                                  "vram_total": dev["vram_total"], "vram_free": dev["vram_free"],
                                  "running": len(q["queue_running"]), "pending": len(q["queue_pending"])})
    except Exception as e:  # ComfyUI down or restarting
        return web.json_response({"online": False, "error": str(e)})


def orphan_subjects(history):
    # subjects with a finished Loop Studio job that its run never recorded or never made outputs for
    by_output = {}
    rendering = {r["subject"] for r in runs.runs if r["status"] == "running"}
    for name, s in all_subjects():
        if s.get("output") and name not in rendering:
            by_output[s["output"]] = (name, s)
    wanted = [ext for key, ext in {"gif": ".gif", "webp": ".webp", "mp4": ".share.mp4"}.items() if CFG["outputs"].get(key)]
    out = set()
    for pid, h in history.items():
        g, extra = h["prompt"][2], h["prompt"][3]
        if extra.get("client_id") != "loopstudio":  # someone's own workflow: its node 21 can be anything
            continue
        folder = g.get("21", {}).get("inputs", {}).get("filename_prefix", "").rsplit("/", 1)[0]
        if folder not in by_output or pid in runs.prompt_ids():
            continue
        name, s = by_output[folder]
        if h["status"].get("status_str") != "success" or "21" not in h["outputs"]:
            continue
        ids_file = data_path(s, "ids.txt")
        known = pid in (open(ids_file).read().split() if os.path.exists(ids_file) else [])
        mp4 = os.path.join(COMFY_OUTPUT, folder, h["outputs"]["21"]["images"][0]["filename"])
        if not known or (wanted and os.path.isfile(mp4) and not any(os.path.exists(mp4[:-4] + e) for e in wanted)):
            out.add(name)
    return out


ORPHANS = asyncio.Lock()


async def finish_orphans(delay=0):
    await asyncio.sleep(delay)
    async with ORPHANS:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
                history = await (await http.get(COMFY_URL + "/history?max_items=300")).json()
            for name in orphan_subjects(history):
                print("finishing renders of", name, flush=True)
                await run_script("review.py", name)  # records them, makes their GIF and loop metrics
                broadcast({"type": "renders_changed", "subject": name})
        except Exception as e:
            print("finish orphans:", e, flush=True)


async def comfy_relay(app):
    # forward ComfyUI's progress to the app; reconnects whenever ComfyUI restarts
    while True:
        try:
            url = COMFY_URL
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(url.replace("http", "ws", 1) + "/ws?clientId=loopstudio", heartbeat=30) as ws:
                    broadcast({"type": "comfy", "online": True})
                    asyncio.ensure_future(finish_orphans())  # anything that finished while the app was closed

                    async def follow_url():  # the URL changed in Settings: connect to the new one
                        while url == COMFY_URL:
                            await asyncio.sleep(2)
                        await ws.close()
                    follow = asyncio.ensure_future(follow_url())
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        m = json.loads(msg.data)
                        t, d = m.get("type"), m.get("data", {})
                        if t == "progress":
                            runs.note(d.get("prompt_id"), value=d.get("value"), max=d.get("max"))
                            broadcast({"type": "progress", "value": d.get("value"), "max": d.get("max"), "prompt_id": d.get("prompt_id")})
                        elif t == "executing":
                            node = d.get("node")
                            stage = STAGES.get(str(node), "working") if node else "idle"
                            runs.note(d.get("prompt_id"), stage=stage, **({} if stage == "sampling" else {"value": 0, "max": 0}))
                            broadcast({"type": "stage", "stage": stage, "prompt_id": d.get("prompt_id")})
                        elif t == "status":
                            broadcast({"type": "queue", "remaining": d.get("status", {}).get("exec_info", {}).get("queue_remaining")})
                        elif t == "execution_success" and d.get("prompt_id") not in runs.prompt_ids():
                            # a Loop Studio job no run is waiting for (its run ended with an app restart): finish it here,
                            # a little later; the job's lock stops a second encode if a process does still have it
                            asyncio.ensure_future(finish_orphans(delay=150))
                    follow.cancel()
        except Exception:
            broadcast({"type": "comfy", "online": False})
        await asyncio.sleep(3)


# ---------- agent terminal ----------

class Terminal:
    """One shared agent session (Claude Code, Codex, a local-LLM agent...); reconnecting tabs see the recent output again."""

    def __init__(self):
        self.pty, self.clients, self.buffer, self.loop = None, set(), "", None

    def start(self, cols=120, rows=30, onboard=False):
        # onboard: open with ONBOARD_PROMPT, for agents that take one on the command line; returns whether it did
        self.buffer = ""
        # runs in the Loop Studio folder, whose AGENTS.md (CLAUDE.md for Claude Code) explains the app and studio.py
        argv = agent_argv()
        if not argv or not shutil.which(argv[0]):
            self.send(f"\x1b[31m{argv[0] if argv else 'No agent command'} isn't on PATH: set the agent in Settings, then press Restart\x1b[0m\r\n")
            return False
        flag = prompt_flag()
        prompted = bool(onboard) and isinstance(flag, str)
        if prompted:
            argv = list(argv) + ([flag] if flag else []) + [ONBOARD_PROMPT]
        # `python studio.py` in the terminal is this Python (the app's venv), whether or not Python is on PATH
        env = {**os.environ, "PYTHONUTF8": "1", **{k: str(v) for k, v in AGENT.get("env", {}).items()}}
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
        self.pty = PtyProcess.spawn(argv, cwd=HERE, dimensions=(rows, cols), env=env)
        threading.Thread(target=self.read, args=(self.pty,), daemon=True).start()
        return prompted

    def read(self, pty):
        while pty.isalive():
            try:
                data = pty.read(4096)
            except EOFError:
                break
            if data:
                self.loop.call_soon_threadsafe(self.send, data)
        self.loop.call_soon_threadsafe(self.ended, pty)

    def ended(self, pty):
        if pty is not self.pty:  # the session Restart replaced: the new one is running
            return
        self.send("\r\n\x1b[2m[session ended: press Restart]\x1b[0m\r\n")
        for ws in list(self.clients):  # so the page starts it again for the next request instead of typing into nothing
            asyncio.ensure_future(ws.send_str(json.dumps({"type": "state", "alive": False})))

    def send(self, data):
        self.buffer = (self.buffer + data)[-200_000:]
        for ws in list(self.clients):
            asyncio.ensure_future(ws.send_str(json.dumps({"type": "output", "data": data})))

    def alive(self):
        return self.pty is not None and self.pty.isalive()


terminal = Terminal()


async def term_ws(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    terminal.loop = asyncio.get_running_loop()
    terminal.clients.add(ws)
    if terminal.buffer:
        await ws.send_str(json.dumps({"type": "output", "data": terminal.buffer}))
    await ws.send_str(json.dumps({"type": "state", "alive": terminal.alive()}))
    try:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            if m["type"] == "start" and not terminal.alive():
                prompted = terminal.start(m.get("cols", 120), m.get("rows", 30), onboard=m.get("onboard"))
                await ws.send_str(json.dumps({"type": "state", "alive": terminal.alive(), "prompted": prompted}))
            elif m["type"] == "restart":
                if terminal.alive():
                    terminal.pty.terminate(force=True)
                terminal.start(m.get("cols", 120), m.get("rows", 30))
                await ws.send_str(json.dumps({"type": "state", "alive": terminal.alive()}))
            elif m["type"] == "input" and terminal.alive():
                terminal.pty.write(m["data"])
            elif m["type"] == "resize" and terminal.alive():
                terminal.pty.setwinsize(max(5, m["rows"]), max(20, m["cols"]))
    finally:
        terminal.clients.discard(ws)
    return ws


async def events_ws(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    events.add(ws)
    try:
        async for _ in ws:
            pass
    finally:
        events.discard(ws)
    return ws


# ---------- app ----------

@web.middleware
async def same_origin(request, handler):
    # localhost only, and no requests driven by other web pages (the terminal is a shell)
    if request.host not in {f"{HOST}:{PORT}", f"localhost:{PORT}"}:
        raise web.HTTPForbidden(text="bad host")
    origin = request.headers.get("Origin")
    if (request.method != "GET" or request.path.startswith("/ws/")) and origin not in ORIGINS:
        raise web.HTTPForbidden(text="bad origin")
    response = await handler(request)
    if request.path.startswith("/static/"):
        # revalidate the page's own scripts, or Chrome keeps running an old app.js after an edit
        response.headers["Cache-Control"] = "no-cache"
    return response


async def index(request):
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        page = f.read().replace("{{BANNER}}", html.escape(BANNER))
    return web.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-cache"})


async def set_ui_state(request):
    body = await request.json()
    state = {k: body.get(k) for k in ("subject", "tab", "concept", "selected", "filter")}
    state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_json(UI_STATE, state)
    return web.json_response(state)


async def get_state(request):
    # one call for the agent in the terminal: what the user has open, the run queue, ComfyUI, where things are
    runs_summary = [{k: r[k] for k in ("id", "subject", "concepts", "seeds", "fast", "status")} | {"format": r.get("format", "square"), "current": r.get("current")}
                    for r in runs.runs[-10:]]
    comfy = json.loads((await comfy_status(request)).text)
    return web.json_response({"ui": read_json(UI_STATE, {}), "runs": runs_summary, "comfy": comfy,
                              "subjects": [n for n in sorted(os.listdir(SUBJECTS)) if os.path.exists(os.path.join(SUBJECTS, n, "subject.json"))],
                              "paths": {"subjects": SUBJECTS, "data": DATASETS, "comfy_output": COMFY_OUTPUT, "comfy_python": COMFY_PY,
                                        "scripts": SCRIPTS},
                              "agent": AGENT["name"], "comfy_dir": CFG["comfy_dir"], "comfy_ok": comfy_ok(),
                              "first_run": not os.path.exists(ONBOARDED), "onboard_prompt": ONBOARD_PROMPT, "prompt_flag": prompt_flag()})


async def onboarded(request):
    os.makedirs(DATASETS, exist_ok=True)
    with open(ONBOARDED, "w") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S\n"))
    return web.json_response({"onboarded": True})


async def models_report(request):
    # what this PC can render: GPU, each render mode with what's missing and minutes per GIF (measured here when possible)
    items = await check_all()
    comfy = json.loads((await comfy_status(request)).text)
    took = {}
    for name, s in all_subjects():
        try:
            rows = read_json(os.path.join(config.data_dir(CFG, s), "renders.json"), [])
        except web.HTTPException:
            continue
        for r in rows:
            if r.get("minutes"):
                # the mode recorded with the render; older renders by name (_fast, _pika), where upscaled ones look plain
                mode = r.get("mode") or ("pika" if "_pika" in r["stem"] else "fast" if "_fast" in r["stem"] else "stock")
                took.setdefault({"pika_up": "pika"}.get(mode, mode), []).append(r["minutes"])
    measured = {k: {"minutes": round(statistics.median(v), 1), "renders": len(v)} for k, v in took.items()}
    return web.json_response({"comfy": comfy, "items": items, "modes": needs.modes(items), "measured": measured})


def watched_files():
    # the files whose changes the page should pick up, also when the agent edits them from the terminal
    out = {}
    for name, s in all_subjects():
        folder = os.path.join(SUBJECTS, name)
        paths = [os.path.join(folder, "subject.json"), os.path.join(folder, "concepts.json")]
        paths += [os.path.join(config.data_dir(CFG, s), f) for f in ("results.json", "renders.json")]
        out[name] = paths
    return out


async def watch_files(app):
    seen = None
    while True:
        try:
            if (os.path.getmtime(config.FILE) if os.path.exists(config.FILE) else 0) != CONFIG_MTIME:
                settings_changed()  # config.json edited outside the Settings panel
                seen = None  # the subjects folder may be another one now
            now = {name: tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in paths)
                   for name, paths in watched_files().items()}
            if seen is not None:
                if set(now) != set(seen):
                    broadcast({"type": "subjects"})
                for name in now.keys() & seen.keys():
                    if now[name] != seen[name]:
                        broadcast({"type": "subject_changed", "subject": name})
            seen = now
        except Exception as e:
            print("watch:", e, flush=True)
        await asyncio.sleep(2)


async def open_browser():
    # once the server answers, so the tab never lands on a connection error (like ComfyUI's --auto-launch)
    while True:
        try:
            _, writer = await asyncio.open_connection(HOST, PORT)
            writer.close()
            break
        except OSError:
            await asyncio.sleep(0.2)
    webbrowser.open(f"http://{HOST}:{PORT}")


async def on_startup(app):
    global runs
    runs = Runs()
    app["tasks"] = [asyncio.create_task(runs.worker()), asyncio.create_task(comfy_relay(app)), asyncio.create_task(watch_files(app))]
    if "--open" in sys.argv:
        app["tasks"].append(asyncio.create_task(open_browser()))


def main():
    os.makedirs(SUBJECTS, exist_ok=True)
    app = web.Application(middlewares=[same_origin], client_max_size=50 * 1024 * 1024)
    app.on_startup.append(on_startup)
    app.add_routes([
        web.get("/", index),
        web.get("/api/subjects", list_subjects),
        web.post("/api/subjects", create_subject),
        web.get("/api/subjects/{name}", get_subject),
        web.put("/api/subjects/{name}", update_subject),
        web.delete("/api/subjects/{name}", delete_subject),
        web.put("/api/subjects/{name}/concepts", save_concepts),
        web.post("/api/subjects/{name}/preview", preview),
        web.post("/api/subjects/{name}/marks", save_mark),
        web.post("/api/subjects/{name}/refresh", refresh),
        web.post("/api/subjects/{name}/recycle-losers", recycle_losers),
        web.post("/api/subjects/{name}/render", start_run),
        web.post("/api/subjects/{name}/show", show_in_folder),
        web.get("/api/runs", list_runs),
        web.post("/api/runs/{id}/stop", stop_run),
        web.get("/api/comfy", comfy_status),
        web.get("/api/state", get_state),
        web.post("/api/ui-state", set_ui_state),
        web.get("/api/settings", get_settings),
        web.put("/api/settings", save_settings),
        web.post("/api/pick", pick),
        web.post("/api/check-comfy", check_comfy),
        web.get("/api/comfy-guess", comfy_guess),
        web.post("/api/comfy-folder", set_comfy_folder),
        web.get("/api/requirements", requirements),
        web.get("/api/models", models_report),
        web.post("/api/onboarded", onboarded),
        web.post("/api/install-deps", install_deps),
        web.get("/api/model-folders", model_folders),
        web.get("/api/downloads", list_fetches),
        web.post("/api/downloads", start_fetch),
        web.post("/api/downloads/{key}/cancel", cancel_fetch),
        web.post("/api/install-node", install_node),
        web.post("/api/install-gifski", install_gifski),
        web.post("/api/open-model-folder", open_model_folder),
        web.get("/media/{name}/{file}", media),
        web.get("/ref/{name}", reference),
        web.get("/ws/term", term_ws),
        web.get("/ws/events", events_ws),
        web.static("/static", STATIC),
    ])
    print(f"Loop Studio on http://{HOST}:{PORT}", flush=True)
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
