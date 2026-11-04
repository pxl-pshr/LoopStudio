"""Drive Loop Studio from the terminal (used by the agent in the app's terminal drawer).

usage:
  python studio.py state                                   what the user has open, the run queue, ComfyUI, folders
  python studio.py render <subject> <concept ...> [--seeds 1000,2024] [--fast] [--format square|vertical|wide] [--redo] [--no-wait]
                                                           without --seeds each concept gets a seed it hasn't had yet;
                                                           follows the run to its end (rendered, skipped or failed)
                                                           unless --no-wait; Ctrl+C stops following, not the run
  python studio.py refresh <subject>                       rebuild the review (new renders, loop metrics; also picks up
                                                           and finishes renders whose run ended early, e.g. an app restart)
  python studio.py runs                                    recent runs with their last log lines
  python studio.py stop <run id>                           stop a run (cancels its current ComfyUI job)
  python studio.py models                                  what this PC can render: modes, what's missing, minutes per GIF
  python studio.py onboarded                               first-run setup talk done (the welcome stops showing)

Renders started here go through the app's queue: they show in the Runs tab with live progress and land in the
review grid. Editing a subject's files (concepts.json, subject.json, results.json) updates the page by itself.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

ENDED = ("done", "skipped", "failed", "stopped")

APP = f"http://127.0.0.1:{os.environ.get('LOOPSTUDIO_PORT', 8190)}"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(APP + path, data=data, method=method,
                                 headers={"Origin": APP, "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=600))
    except urllib.error.HTTPError as e:
        sys.exit(f"Loop Studio: {e.code} {e.read().decode(errors='replace')}")
    except urllib.error.URLError:
        sys.exit("Loop Studio isn't running (start.bat)")


def seeds_text(r):
    return f"seeds {r['seeds']}" if r["seeds"] else "new seed"


def fmt_text(r):
    # the run's aspect, when it isn't the default square
    return {"vertical": " 9:16", "wide": " 16:9"}.get(r.get("format"), "")


def follow(run_id):
    # prints the run's log and ComfyUI's progress on it until it ends; exits 1 unless something rendered
    printed, shown = 0, None
    while True:
        r = next((x for x in call("GET", "/api/runs") if x["id"] == run_id), None)
        if r is None:
            sys.exit(f"run {run_id} is gone (the app restarted?): see `python studio.py runs`")
        # log_tail holds the last 200 lines; log_total says how many there have been, so a long run keeps printing
        tail = r["log_tail"]
        for line in tail[len(tail) - min(r["log_total"] - printed, len(tail)):]:
            print("   ", line, flush=True)
        printed = r["log_total"]
        p = r.get("progress") or {}
        now = (p.get("stage"), p.get("value") if p.get("max") else None)
        if r["status"] == "running" and p.get("stage") not in (None, "idle") and now != shown:
            print(f"    {r.get('current') or 'render'}: {p['stage']}" + (f" {p['value']}/{p['max']}" if p.get("max") else ""), flush=True)
        shown = now
        if r["status"] in ENDED:
            minutes = round(((r.get("ended") or time.time()) - (r.get("started") or time.time())) / 60, 1)
            counts = ", ".join(f"{r.get(k, 0)} {k}" for k in ("rendered", "skipped", "failed") if r.get(k))
            print(f"run {run_id} {r['status']}" + (f": {counts}" if counts else "") + f" ({minutes} min)", flush=True)
            if r["status"] == "skipped":
                print("nothing rendered: those seeds exist already; leave out --seeds for new ones, or add --redo")
            sys.exit(0 if r["status"] == "done" else 1)
        time.sleep(2)


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd = args[0]
    if cmd == "state":
        s = call("GET", "/api/state")
        ui = s["ui"]
        print(f"open: subject {ui.get('subject')}, tab {ui.get('tab')}, concept {ui.get('concept')}, "
              f"selected {ui.get('selected') or []}, filter {ui.get('filter')} (as of {ui.get('updated')})")
        c = s["comfy"]
        print("ComfyUI:", f"online {c.get('version')}, {c.get('gpu')}, queue {c.get('running', 0) + c.get('pending', 0)}" if c.get("online") else "offline")
        print("subjects:", ", ".join(s["subjects"]))
        if not s.get("comfy_ok"):
            print(f"ComfyUI folder: not set up ({s['comfy_dir']} isn't a ComfyUI with its Python): the user picks it in the app, "
                  "or set comfy_dir in config.json")
        if s.get("first_run"):
            print("first run: yes (walk the user through `python studio.py models` first; see AGENTS.md)")
        for label, key in (("subject files", "subjects"), ("run data", "data"), ("renders", "comfy_output"),
                           ("ComfyUI python", "comfy_python"), ("skill scripts", "scripts")):
            print(f"{label}: {s['paths'][key]}")
        for r in s["runs"]:
            print(f"run {r['id']} {r['status']}: {r['subject']} {', '.join(r['concepts'])} {seeds_text(r)}"
                  f"{' fast' if r['fast'] else ''}{fmt_text(r)}{' (now ' + r['current'] + ')' if r.get('current') and r['status'] == 'running' else ''}")
    elif cmd == "render":
        flags = {"fast": "--fast" in args, "redo": "--redo" in args, "format": "square"}
        seeds, names = [], []
        for i, a in enumerate(args[1:], 1):
            if a.startswith("--seed="):  # loop_gifs.py's form, also fine here
                seeds.append(int(a[7:]))
            elif args[i - 1] == "--seeds" and not a.startswith("--"):
                seeds += [int(x) for x in a.split(",") if x.strip()]
            elif args[i - 1] == "--format" and not a.startswith("--"):
                flags["format"] = {"1:1": "square", "9:16": "vertical", "16:9": "wide"}.get(a, a)
                if flags["format"] not in ("square", "vertical", "wide"):
                    sys.exit("--format: square (1:1), vertical (9:16) or wide (16:9)")
            elif a in ("--square", "--vertical", "--wide"):  # loop_gifs.py's form
                flags["format"] = a[2:]
            elif a.startswith("--") and a not in ("--seeds", "--fast", "--redo", "--no-wait", "--format"):
                sys.exit(f"unknown option {a}: use --seeds 1000,2024, --fast, --format square|vertical|wide, --redo, --no-wait")
            elif not a.startswith("--"):
                names.append(a)
        if len(names) < 2:
            sys.exit("usage: python studio.py render <subject> <concept ...> [--seeds 1000,2024] [--fast] [--format square|vertical|wide] [--redo] [--no-wait]")
        subject, concepts = names[0], names[1:]
        run = call("POST", f"/api/subjects/{subject}/render", {"concepts": concepts, "seeds": seeds, **flags})
        print(f"queued run {run['id']}: {len(concepts) * max(len(seeds), 1)} render(s) of {subject}, {seeds_text(run)}", flush=True)
        if "--no-wait" not in args:
            try:
                follow(run["id"])
            except KeyboardInterrupt:
                sys.exit(f"\nstopped following; run {run['id']} goes on (python studio.py runs / stop {run['id']})")
    elif cmd == "refresh":
        r = call("POST", f"/api/subjects/{args[1]}/refresh")
        last = r["output"].strip().splitlines()[-1] if r["output"].strip() else "refreshed"
        if not r["ok"]:
            sys.exit(f"the review failed: {last}")
        for line in r["output"].splitlines():
            if line.startswith("couldn't finish"):
                print(line)
        print(last)
    elif cmd == "runs":
        for r in call("GET", "/api/runs"):
            print(f"run {r['id']} {r['status']}: {r['subject']} {', '.join(r['concepts'])} {seeds_text(r)}{' fast' if r['fast'] else ''}{fmt_text(r)}")
            for line in r["log_tail"][-3:]:
                print("   ", line)
    elif cmd == "models":
        r = call("GET", "/api/models")
        c = r["comfy"]
        print(f"GPU: {c.get('gpu')}, {c.get('vram_total', 0) / 2**30:.0f} GB" if c.get("online") else "ComfyUI: not running")
        required = [i["name"] for i in r["items"] if not i["ok"] and i["need"] == "required"]
        if required:
            print("\nNothing can render until these are fixed:", *[f"  {x}" for x in required], sep="\n")
        for m in r["modes"]:
            m["missing"] = [x for x in m["missing"] if x not in required]
            # timings from this PC's renders, by name: _fast, _pika or neither (upscaled _fast ones can't be told apart, so not fast_up)
            here = r["measured"].get(m["key"])
            took = (f"~{here['minutes']} min a GIF here (median of {here['renders']} renders)" if here else
                    f"~{m['minutes']} min a GIF on an RTX 4090 (24 GB); expect longer on a smaller GPU" if m["minutes"] else "not timed yet")
            print(f"\n{'NOT available' if m['missing'] or required else 'available'}: {m['label']}: {took}")
            print(f"  {m['note']}; use: {m['how']}")
            for x in m["missing"]:
                print(f"  also missing: {x}" if required else f"  missing: {x}")
        fixes = [i for i in r["items"] if not i["ok"]]
        if fixes:
            print("\nhow to get what's missing:")
            for i in fixes:
                print(f"  {i['name']}: {i['fix']}")
    elif cmd == "onboarded":
        call("POST", "/api/onboarded")
        print("first-run setup marked done")
    elif cmd == "stop":
        r = call("POST", f"/api/runs/{args[1]}/stop")
        print("run", r["id"], r["status"])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
