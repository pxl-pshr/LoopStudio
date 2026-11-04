"""Review page for one subject's renders: GIF, caption, editable tags, prompt, loop metrics, Win/Lose/note.

usage: python review.py <subject>
Reads the jobs in the subject's data/ids.txt from ComfyUI (finished, running or queued) and caches them in
data/jobs.json, so the page still builds after a ComfyUI restart. Writes ComfyUI/output/<subject output>/review.html.
Marks live in the browser's localStorage; "Copy results" gives them as JSON (Loop Studio keeps its own in data/results.json).
Loop metrics (optical flow): "loop point Nx clip" compares the wrap to the clip's own frame steps, 1.0 = typical;
"speed" is motion speed at the wrap against the median; hitches count frames faster than both neighbours by 1.4x.
"""
import html
import json
import os
import sys
import time
import urllib.request

try:  # in ComfyUI's Python; without them the review still builds, its renders just have no loop metrics
    import av
    import cv2
    import numpy as np
except ImportError:
    av = cv2 = np = None

from loop_gifs import COMFY_OUTPUT, OUTPUT_FILES, OUTPUTS, SERVER, convert, job_lock, load_subject, subject_lock, tags_for
from needs import MODELS, _norm


def load(path):
    with av.open(path) as c:
        # squashed to a fixed size, for the metrics only; square and wide keep 480x272, so their numbers compare with
        # older renders, and vertical stands the other way up
        v = c.streams.video[0]
        size = (272, 480) if v.height > v.width else (480, 272)
        return [cv2.resize(f.to_ndarray(format="rgb24"), size).astype(np.float32) for f in c.decode(video=0)]


def step(a, b):
    # (motion the flow can't explain, 90th percentile flow speed) for a -> b
    ga = cv2.cvtColor(a.astype(np.uint8), cv2.COLOR_RGB2GRAY)
    gb = cv2.cvtColor(b.astype(np.uint8), cv2.COLOR_RGB2GRAY)
    flow = cv2.calcOpticalFlowFarneback(ga, gb, None, 0.5, 3, 15, 3, 5, 1.1, 0)
    h, w = ga.shape
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    warped = cv2.remap(b, gx + flow[..., 0], gy + flow[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    return np.abs(warped - a).mean(), np.percentile(np.linalg.norm(flow, axis=-1), 90)


def measure(path):
    if cv2 is None:
        raise RuntimeError("opencv, av or numpy isn't in ComfyUI's Python: Settings > Requirements installs them")
    frames = load(path)
    frames.append(frames[0])  # include the loop point
    res, spd = np.array([step(a, b) for a, b in zip(frames[:-1], frames[1:])]).T
    rr = res / max(np.median(res), 1e-6)
    sr = spd / max(np.median(spd), 1e-6)
    jerk = sr / np.maximum(np.maximum(np.roll(sr, 1), np.roll(sr, -1)), 1e-6)
    inner_rr = rr[:-1]
    m = {
        "frames": len(frames) - 1,
        "hitches": int((jerk > 1.4).sum()),
        "max_speed": round(float(sr.max()), 2),
        "worst_pop": round(float(inner_rr.max()), 2),
        "seam_pop": round(float(rr[-1]), 2),
        "seam_speed": round(float(sr[-1]), 2),
        "seam_vs_clip": round(float(rr[-1] / max(np.percentile(inner_rr, 95), 1e-6)), 2),
    }
    return m


def job_info(prompt_id, cache):
    # the job as ComfyUI knows it now; keeps the cached copy when ComfyUI has forgotten it
    info = dict(cache.get(prompt_id, {}))
    try:
        h = json.load(urllib.request.urlopen(f"{SERVER}/history/{prompt_id}", timeout=30)).get(prompt_id)
        q = json.load(urllib.request.urlopen(f"{SERVER}/queue", timeout=30))
    except (OSError, ValueError):  # ComfyUI not running: what's cached is all there is
        return info
    queued = {e[1]: ("rendering" if part == "queue_running" else "queued", e) for part in ("queue_running", "queue_pending") for e in q[part]}
    entry = h["prompt"] if h else queued.get(prompt_id, (None, None))[1]
    if entry is not None:
        g, extra = entry[2], entry[3]
        # Pika renders take fixed sigmas instead of a step count; upscaled renders sample node 9 at the base size
        sched, size = g["12"]["inputs"], g["32"]["inputs"] if "32" in g else g["9"]["inputs"]
        info.update(name=g["21"]["inputs"]["filename_prefix"].split("/")[-1], prompt=g["9"]["inputs"]["prompt"],
                    caption=extra.get("caption", extra.get("meme_caption")),
                    width=size.get("mode.width", size.get("width")), height=size.get("mode.height", size.get("height")),
                    frames=g["9"]["inputs"]["length"] - 5,
                    steps=sched["steps"] if "steps" in sched else len(sched["sigmas"].split(",")) - 1,
                    views=g["3"]["inputs"]["views"], seed=g["10"]["inputs"]["noise_seed"], mode=mode_of(g))
    if h:
        info["status"] = h["status"]["status_str"]
        times = {m[0]: m[1].get("timestamp") for m in h["status"]["messages"]}
        if "execution_start" in times:
            info["started"] = round(times["execution_start"] / 1000)
        if "execution_success" in times:
            info["minutes"] = round((times["execution_success"] - times["execution_start"]) / 60000, 1)
            info["mp4"] = h["outputs"]["21"]["images"][0]["filename"]
    elif prompt_id in queued:
        info["status"] = queued[prompt_id][0]
    elif info.get("status") in ("rendering", "queued"):
        info["status"] = "error"  # ComfyUI restarted or the job was taken off its queue: it won't finish
    return info


def mode_of(g):
    # the render mode, as needs.modes names them: stock, fast or pika (the Pika LoRA, node 2), plus _up when upscaled (node 32)
    base = "pika" if "2" in g else "fast" if _norm(g["1"]["inputs"]["unet_name"]) == _norm(MODELS["fast"][0]) else "stock"
    return base + ("_up" if "32" in g else "")


def adopt(subject, ids):
    # jobs Loop Studio queued into this subject's folder that ids.txt doesn't list: the render process that queued them
    # ended before it could (the app closed mid-run, or the data folder was swapped); ComfyUI still has them
    try:
        history = json.load(urllib.request.urlopen(f"{SERVER}/history?max_items=300"))
    except (OSError, ValueError):
        return []
    found = []
    for pid, h in history.items():
        g, extra = h["prompt"][2], h["prompt"][3]
        prefix = g.get("21", {}).get("inputs", {}).get("filename_prefix", "")
        if extra.get("client_id") == "loopstudio" and prefix.rsplit("/", 1)[0] == subject["output"] and pid not in ids:
            found.append(pid)
    return found


def unfinished(j, out_dir):
    # a finished render without a GIF / WebP / MP4 (its process failed or ended first): none of the outputs exist, in any
    # format (switching formats doesn't redo old renders). The job's lock says whether someone is making them now
    mp4 = os.path.join(out_dir, j.get("mp4") or "")
    made = [mp4[:-4] + ext for ext in OUTPUT_FILES.values()]
    return (j.get("status") == "success" and os.path.isfile(mp4) and any(OUTPUTS.get(k) for k in OUTPUT_FILES)
            and not any(map(os.path.exists, made)))


def dump(value, path):
    # written whole, then swapped in: the app reads these files while they're being rebuilt
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(value, f, indent=1)
    for attempt in range(10):
        try:
            return os.replace(path + ".tmp", path)
        except PermissionError:  # Windows, while the app is reading it
            if attempt == 9:
                raise
            time.sleep(0.2)


def card_data(pid, j, metrics, out_dir, subject, concepts):
    # one render as plain data, for Loop Studio (data/renders.json)
    stem = j["mp4"][:-4] if j.get("mp4") else j.get("name", pid)
    gif = stem + ".gif" if j.get("mp4") and os.path.exists(os.path.join(out_dir, stem + ".gif")) else None
    webp = stem + ".webp" if j.get("mp4") and os.path.exists(os.path.join(out_dir, stem + ".webp")) else None
    # when it was rendered: ComfyUI's start time, or for renders it has forgotten (made before this was recorded) the MP4's
    mp4 = os.path.join(out_dir, j["mp4"]) if j.get("mp4") else None
    date = j.get("started") or (round(os.path.getmtime(mp4)) if mp4 and os.path.exists(mp4) else None)
    return {"stem": stem, "prompt_id": pid, "status": j.get("status"), "mp4": j.get("mp4"), "gif": gif, "webp": webp,
            "gif_mb": round(os.path.getsize(os.path.join(out_dir, gif)) / 1e6, 1) if gif else None,
            "caption": j.get("caption"), "prompt": j.get("prompt"), "seed": j.get("seed"), "steps": j.get("steps"),
            "mode": j.get("mode"), "date": date,
            "width": j.get("width"), "height": j.get("height"), "frames": j.get("frames"), "minutes": j.get("minutes"),
            "metrics": metrics.get(j.get("mp4")),
            "tags": tags_for(stem + "_", subject, concepts)}


def card(pid, j, metrics, out_dir, subject, concepts):
    gif = j["mp4"][:-4] + ".gif" if j.get("mp4") else None
    if gif and os.path.exists(os.path.join(out_dir, gif)):
        media = f'<img src="{html.escape(gif)}" loading="lazy">'
        size = f" · GIF {os.path.getsize(os.path.join(out_dir, gif)) / 1e6:.1f} MB"
    elif j.get("mp4"):
        media = f'<video src="{html.escape(j["mp4"])}" autoplay loop muted playsinline></video>'
        size = " · GIF not made yet"
    else:
        media = f'<div class="wait">{html.escape(j.get("status", "unknown"))}</div>'
        size = ""
    m = metrics.get(j.get("mp4"))
    if m:
        nums = f"loop point {m['seam_vs_clip']}x clip · speed {m['seam_speed']} · hitches {m['hitches']} · worst pop {m['worst_pop']}"
    else:
        nums = ""
    caption = f'<span class="cap">{html.escape(j["caption"])}</span>' if j.get("caption") else ""
    setup = f"{j.get('width')}x{j.get('height')} · {j.get('frames')} frames · {j.get('steps')} steps · views {j.get('views')} · seed {j.get('seed')}"
    if j.get("minutes"):
        setup += f" · {j['minutes']} min"
    stem = j["mp4"][:-4] if j.get("mp4") else j.get("name", pid)
    tags = ", ".join(tags_for(stem + "_", subject, concepts))
    return f"""
<div class="card" data-id="{html.escape(stem)}">
  {media}
  <div class="meta"><b>{html.escape(stem)}</b> {caption}</div>
  <input class="tags" data-default="{html.escape(tags)}" value="{html.escape(tags)}" title="tags, comma separated">
  <div class="nums">{setup}{size}</div>
  <div class="nums">{nums}</div>
  <div class="btns"><button data-v="win">Win</button><button data-v="lose">Lose</button><button data-v="">Clear</button>
    <input class="note" placeholder="note (e.g. loop breaks, gesture too small)"></div>
  <details open><summary>prompt</summary><pre>{html.escape(j.get('prompt', ''))}</pre></details>
</div>"""


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>TITLE</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { --bg:#111; --fg:#eee; --card:#1c1c1c; --mute:#999; --win:#2e7d32; --lose:#b71c1c; }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg); font:14px system-ui, sans-serif; }
header { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(380px, 1fr)); gap:12px; }
.card { background:var(--card); border-radius:8px; padding:8px; border:2px solid transparent; min-width:0; }
.card.win { border-color:var(--win); } .card.lose { border-color:var(--lose); opacity:.75; }
img, video { width:100%; aspect-ratio:1; object-fit:contain; border-radius:4px; background:#000; display:block; }
.wait { width:100%; aspect-ratio:1; border-radius:4px; background:#000; display:flex; align-items:center; justify-content:center; color:var(--mute); }
.meta { margin-top:6px; } .cap { background:#fff; color:#000; font-weight:700; padding:1px 6px; border-radius:4px; }
.nums { color:var(--mute); font-size:12px; margin:4px 0; }
.btns { display:flex; gap:6px; flex-wrap:wrap; } .note { flex:1; min-width:120px; background:#000; color:var(--fg); border:1px solid #333; }
.tags { width:100%; box-sizing:border-box; margin:4px 0; background:#000; color:#8fc3ff; border:1px solid #333; font-size:12px; padding:3px; }
button { background:#333; color:var(--fg); border:0; border-radius:4px; padding:4px 10px; cursor:pointer; }
pre { white-space:pre-wrap; color:var(--mute); font-size:12px; }
</style></head><body>
<header><b>TITLE</b> <span id="count"></span>
<label><input type="radio" name="f" value="all" checked> all</label>
<label><input type="radio" name="f" value="todo"> unreviewed</label>
<button id="copy">Copy results</button></header>
<div class="grid">CARDS</div>
<script>
const KEY = "STORAGE_KEY";
let state = {};
try { state = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
function save() { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} render(); }
function render() {
  let w = 0, l = 0;
  document.querySelectorAll(".card").forEach(c => {
    const s = state[c.dataset.id] || {};
    c.classList.toggle("win", s.v === "win"); c.classList.toggle("lose", s.v === "lose");
    c.querySelector(".note").value = s.note || "";
    const t = c.querySelector(".tags"); t.value = s.tags || t.dataset.default;
    if (s.v === "win") w++; if (s.v === "lose") l++;
    const f = document.querySelector("input[name=f]:checked").value;
    c.style.display = f === "todo" && s.v ? "none" : "";
  });
  document.getElementById("count").textContent = `${w} win · ${l} lose · ${document.querySelectorAll(".card").length} total`;
}
document.querySelectorAll(".card").forEach(c => {
  const id = c.dataset.id;
  c.querySelectorAll("button").forEach(b => b.onclick = () => { state[id] = {...(state[id] || {}), v: b.dataset.v}; save(); });
  c.querySelector(".note").onchange = e => { state[id] = {...(state[id] || {}), note: e.target.value}; save(); };
  c.querySelector(".tags").onchange = e => { state[id] = {...(state[id] || {}), tags: e.target.value}; save(); };
});
document.querySelectorAll("input[name=f]").forEach(r => r.onchange = render);
document.getElementById("copy").onclick = () => {
  const text = JSON.stringify(state, null, 1);
  navigator.clipboard.writeText(text).then(() => flash("copied"), () => flash("copy failed: select the text below"));
  let pre = document.getElementById("dump"); if (!pre) { pre = document.createElement("pre"); pre.id = "dump"; document.body.appendChild(pre); }
  pre.textContent = text;
};
function flash(t) { const b = document.getElementById("copy"); b.textContent = t; setTimeout(() => b.textContent = "Copy results", 1500); }
render();
</script></body></html>"""


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with subject_lock(sys.argv[1]):
        build(*load_subject(sys.argv[1]))


def build(subject, concepts):
    out_dir = os.path.join(COMFY_OUTPUT, subject["output"])
    os.makedirs(out_dir, exist_ok=True)
    data = subject["data"]
    jobs_path, ids_path = (os.path.join(data, f) for f in ("jobs.json", "ids.txt"))
    cache = json.load(open(jobs_path)) if os.path.exists(jobs_path) else {}
    metrics_path = os.path.join(out_dir, "metrics.json")
    metrics = json.load(open(metrics_path)) if os.path.exists(metrics_path) else {}
    cards, rows = [], []
    ids = list(dict.fromkeys(open(ids_path).read().split() if os.path.exists(ids_path) else []))
    adopted = adopt(subject, set(ids))
    if adopted:
        open(ids_path, "a", newline="\n").write("".join(pid + "\n" for pid in adopted))
        print(f"found {len(adopted)} render(s) queued by Loop Studio but never recorded")
    # a file name ComfyUI used again (the earlier render was recycled) belongs to the newest job only
    newest = {}
    for pid in ids + adopted:
        j = job_info(pid, cache)
        cache[pid] = j
        if j.get("mp4"):
            newest[j["mp4"]] = pid
    for pid in ids + adopted:
        j = cache[pid]
        if j.get("mp4") and newest[j["mp4"]] != pid:
            continue
        if unfinished(j, out_dir):
            with job_lock(pid, wait=False) as free:
                if free:
                    print("finishing", j["mp4"], flush=True)
                    try:
                        convert(pid, subject, concepts)
                    except Exception as e:  # one bad render (font, gifski, a locked file) mustn't stop the review
                        print(f"couldn't finish {j['mp4']}: {e}", flush=True)
        if j.get("status") in ("error", None) and not j.get("mp4"):
            continue  # interrupted or unknown jobs stay off the page
        if j.get("mp4") and not os.path.exists(os.path.join(out_dir, j["mp4"])):
            continue  # renders removed after review
        if j.get("mp4") and j["mp4"] not in metrics:
            try:
                metrics[j["mp4"]] = measure(os.path.join(out_dir, j["mp4"]))
            except Exception as e:  # an unreadable MP4, or no opencv: its card has no metrics, and the next review tries again
                print(f"couldn't measure {j['mp4']}: {e}", flush=True)
        if j.get("mp4") in metrics:
            metrics[j["mp4"]].pop("flag", None)  # no verdict: the numbers didn't match which loops look good
        cards.append(card(pid, j, metrics, out_dir, subject, concepts))
        rows.append(card_data(pid, j, metrics, out_dir, subject, concepts))
    dump(cache, jobs_path)
    dump(rows[::-1], os.path.join(data, "renders.json"))
    dump(metrics, metrics_path)
    title = subject["name"].replace("_", " ").title() + " Review"
    out = os.path.join(out_dir, "review.html")
    page = PAGE.replace("TITLE", title).replace("STORAGE_KEY", subject["name"] + "-review").replace("CARDS", "".join(reversed(cards)))
    open(out, "w", encoding="utf-8").write(page)
    print(f"{len(cards)} renders -> {out}")


if __name__ == "__main__":
    main()
