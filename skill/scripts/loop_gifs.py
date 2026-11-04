"""Render looping GIFs of one subject with MiniMax H3 Loop on the local ComfyUI, then make shareable GIFs.

usage:
  python loop_gifs.py <subject> render <concept ...> [--seed=N ...] [--fast] [--redo] [--vertical | --wide] [--dry]
  python loop_gifs.py <subject> render --all        (every concept in the subject's concepts.json)
  python loop_gifs.py <subject> convert [--redo]    (GIFs for finished renders that have none yet; --redo rebuilds them all)

A subject is a folder in the subjects folder (config.json "subjects_dir") with subject.json (reference image, description, tags, folders)
and concepts.json (one entry per GIF idea). Each render saves a silent 24 fps MP4 (992x992 square; --vertical 704x1280
9:16, --wide 1280x704 16:9) and lossless PNG frames
under ComfyUI/output/<subject output>/, then a GIF next to the MP4: 480 px, every other frame at 12 fps (51 frames,
4.25 s) with the concept's caption drawn in Impact (config.json "outputs": GIF, WebP and/or a shareable MP4, size, fps, captions). Jobs run one at a time with the models unloaded in between:
every prompt reloads the text encoder, and a long queue lets the swaps build up until Windows spills VRAM.
Concepts whose frames already exist are skipped (--redo renders them again). --seed=N renders that seed, named with an
_sN suffix (none for 1000); without --seed each concept gets a seed it hasn't been rendered with yet (1000 the first
time). By default the stock model renders with two LoRAs stacked on it, Pika H3 Flex's Ref2VA LoRA and then Licon's VBVR
LoRA, on the stock sampler (25 steps, res_multistep): see needs.DEFAULT_LORAS.
--fast uses FastH3-Omni at 8 steps instead (about a third of the time, no LoRAs), named with a _fast suffix. --dry prints the prompts without rendering, and runs without the render libraries (av, numpy, Pillow).
A concept's "upscale": {"base": 512, "refine_steps": 4} renders two-stage with the MiniMax H3 latent upscaler (see graph);
in subject.json it applies to every concept. subject.json "model": "pika" renders with Pika H3 Flex (6 steps, named _pika).
A concept's (or subject's) "lora": {"file": "minimax\\x.safetensors", "strength": 1.0} puts that LoRA (models/loras/) on the
model instead of the default ones, or a list of them to stack in order; "lora": [] renders the bare stock model. To compare,
copy the concept as <name>_<lora> with the lora set, so it renders next to the original. MODELS.md lists what to try.
"""
import contextlib
import json
import msvcrt
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib

try:  # in ComfyUI's Python; --dry and preview only build prompts, so they also run in the app's own Python
    import av
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    av = np = Image = ImageDraw = ImageFont = None

import config
from needs import CLIP, MODEL_FIELDS, MODELS, PIKA_LORA, UPSCALER, VAE_A, VAE_V, _norm, concept_problem, installed, loras  # model files: see needs.py

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = config.load()
COMFY_OUTPUT, SERVER, FONT = CFG["comfy_output"], CFG["comfy_url"], CFG["font"]
OUTPUTS = CFG["outputs"]
# the outputs next to each master MP4, by config key; .share.mp4 is sized and captioned like the GIF, for sites that want video
OUTPUT_FILES = {"gif": ".gif", "webp": ".webp", "mp4": ".share.mp4"}
GIF_SIZE = OUTPUTS["size"]  # long side; 480 keeps a GIF around 2-3.5 MB
LENGTH = 107    # 17k+5 frames: a 102-frame loop, even, so every other frame stays seamless across the wrap
# aspect -> (name suffix, width, height): about 1 megapixel each, sides in multiples of 32 as H3 needs
FORMATS = {"square": ("_square", 992, 992), "vertical": ("_vertical", 704, 1280), "wide": ("_wide", 1280, 704)}

# Pika H3 Flex: the stock model with Pika's Ref2VA LoRA, lcm (fresh noise every step) on Pika's six fixed sigmas,
# as in Comfy's video_pika_h3_flex_ref2va template
PIKA_SIGMAS = "1.0, 0.972973, 0.923077, 0.8, 0.571429, 0.333333, 0.0"

CAMERA = "Locked-off static camera, no pan, no zoom. "
CLOSING = "Continuous, smooth, unbroken motion throughout."


def load_subject(name):
    folder = os.path.join(CFG["subjects_dir"], name)
    subject = json.load(open(os.path.join(folder, "subject.json"), encoding="utf-8"))
    concepts = json.load(open(os.path.join(folder, "concepts.json"), encoding="utf-8"))
    subject["data"] = config.data_dir(CFG, subject)
    os.makedirs(subject["data"], exist_ok=True)
    return subject, {c["name"]: c for c in concepts}


def concept_of(stem, concepts):
    # the concept an output name such as wink_v2_square_102f_s2024_00001_ came from: what stands before its format
    # (so wink rendered wide isn't taken for a concept called wink_wide); longest match for names without a format
    m = re.match(r"(.+?)_(?:square|vertical|wide)_\d+f(?:_|$)", stem)
    if m:
        return m.group(1) if m.group(1) in concepts else None
    return max((n for n in concepts if stem.startswith(n + "_")), key=len, default=None)


def tags_for(stem, subject, concepts):
    c = concept_of(stem, concepts)
    return list(dict.fromkeys(subject.get("base_tags", []) + (concepts[c].get("tags", []) if c else [])))


def visual(subject, c, s=None):
    # a concept is either a literal "visual" or a template ("studio" or "scene") filled from its fields
    # a subject with a reference image must name it, or H3 isn't told who to animate
    s = s or subject["subject"] or ("the person from <Picture 1>" if subject.get("reference") else "")
    cap = s[:1].upper() + s[1:]
    if "visual" in c:
        text = c["visual"]
    elif c["template"] == "studio":
        text = (CAMERA + "A plain bright white studio background with soft even light. Medium close-up of {subject}, "
                + c.get("framing", "from the chest up") + ", " + subject["his"] + " face exactly in the middle of the frame with equal empty space on "
                "both sides, " + subject["his"] + " whole head and both shoulders in view, facing the camera the whole time. " + c["action"] + " {keep}")
    elif c["template"] == "scene":
        text = CAMERA + c["setting"] + " " + c["action"] + " " + subject["he"] + " faces the camera the whole time. {keep}"
    else:
        raise ValueError(f"{c['name']}: unknown template {c['template']}")
    return text.replace("{Subject}", cap).replace("{subject}", s).replace("{keep}", subject["keep"])


def full_prompt(subject, c):
    sound = f"overall_soundscape: {c.get('sound', '')}\n\nnon_diegetic_music: N/A"
    if subject.get("model") != "pika" or not subject.get("reference"):
        return f"{visual(subject, c)} {CLOSING}\n\n{sound}"
    # Pika's Ref2VA prompt fields (Pika-H3-Flex docs/prompt-guidance.md): the reference becomes <Subject 1>,
    # defined once, and the description refers to it by label
    retention = subject.get("retention") or "<Subject 1>: fully_preserved - face, hair and outfit.\n<Picture 1> background: weak_reference only."
    summary = c.get("summary") or f"<Subject 1>, {c['name'].replace('_', ' ')}."
    return (f"subject_definitions:\n<Subject 1> is {subject['subject']}. Reference role: identity anchor.\n\n"
            f"summary:\n[reference generation] A single continuous, seamlessly looping locked-off shot: {summary}\n\n"
            f"retention_analysis:\n{retention}\n\n"
            f"detailed_description:\n[Shot 1] {visual(subject, c, '<Subject 1>')} {CLOSING}\n\n{sound}")


def graph(prompt, seed, prefix, width, height, reference, model="stock", up=None, lora=None):
    # reference None renders from the prompt alone (abstract subjects)
    # lora {"file", "strength"} or a list of them: extra LoRAs chained on the model in order (nodes 50+, after Pika's)
    # up {"base", "refine_steps"}: sample at base px (long side), upscale the video latent to full size with the
    # H3 latent upscaler, then re-run the schedule's last refine_steps steps there; both passes use the loop model.
    # The loop latent ends in a 2-latent tail that the decoder never reads and that stays noisy; the upscaler's
    # temporal convs smear it back over the loop (pink mush), so upscale only the loop, wrapped with 2 clips of
    # itself on each side so its ends see each other, then rebuild the tail from its first latents.
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS[model][0], "weight_dtype": "default"}},
        "3": {"class_type": "MiniMaxH3Loop", "inputs": {"model": ["1", 0], "pad_clips": 1, "views": 1}},  # views 2 only cleans up audio
        "4": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "minimax", "device": "default"}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_V}},
        "6": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_A}},
        "9": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {"clip": ["4", 0], "vae": ["5", 0], "audio_vae": ["6", 0], "prompt": prompt,
              "width": width, "height": height, "length": LENGTH, "ref_image_size": "match"}},
        "10": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "11": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "12": {"class_type": "BasicScheduler", "inputs": {"model": ["3", 0], "scheduler": "simple", "steps": MODELS[model][1], "denoise": 1.0}},
        "13": {"class_type": "BasicGuider", "inputs": {"model": ["3", 0], "conditioning": ["9", 0]}},
        "14": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["10", 0], "guider": ["13", 0], "sampler": ["11", 0],
               "sigmas": ["12", 0], "latent_image": ["9", 1]}},
        "15": {"class_type": "MiniMaxH3LoopDecode", "inputs": {"samples": ["14", 0], "vae": ["5", 0], "audio_vae": ["6", 0], "pad_clips": 1}},
        "20": {"class_type": "CreateVideo", "inputs": {"images": ["15", 0], "fps": 24}},
        "21": {"class_type": "SaveVideo", "inputs": {"video": ["20", 0], "filename_prefix": prefix, "format": "auto", "format.codec": "auto"}},
        "22": {"class_type": "SaveImage", "inputs": {"images": ["15", 0], "filename_prefix": prefix.rsplit("/", 1)[0] + "/frames/" + prefix.rsplit("/", 1)[1] + "/f"}},
    }
    if model == "pika":
        g["2"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": PIKA_LORA, "strength_model": 1.0}}
        g["3"]["inputs"]["model"] = ["2", 0]
        g["11"]["inputs"]["sampler_name"] = "lcm"
        g["12"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": PIKA_SIGMAS}}
    for i, l in enumerate([lora] if isinstance(lora, dict) else lora or []):
        g[str(50 + i)] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": g["3"]["inputs"]["model"], "lora_name": l["file"],
                          "strength_model": l.get("strength", 1.0)}}
        g["3"]["inputs"]["model"] = [str(50 + i), 0]
    if reference:
        g["40"] = {"class_type": "LoadImage", "inputs": {"image": reference}}
        g["9"]["inputs"]["ref_images.ref_image_0"] = ["40", 0]
    if up:
        g["9"]["inputs"].update(width=round(up["base"] * width / max(width, height) / 32) * 32,
                                height=round(up["base"] * height / max(width, height) / 32) * 32)
        g["30"] = {"class_type": "SplitSigmas", "inputs": {"sigmas": ["12", 0], "step": MODELS[model][1] - up["refine_steps"]}}
        n, pad = (LENGTH - 5) // 17 * 5, 10  # loop latents (5 per 17-frame clip), wrap padding

        def cut(src, index, amount):
            return {"class_type": "LatentCut", "inputs": {"samples": src, "dim": "t", "index": index, "amount": amount}}

        def cat(a, b):
            return {"class_type": "LatentConcat", "inputs": {"samples1": a, "samples2": b, "dim": "t"}}

        g["31"] = {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["14", 0]}}
        g["35"] = cut(["31", 0], 0, n)
        g["36"] = cut(["35", 0], -pad, pad)
        g["37"] = cut(["35", 0], 0, pad)
        g["38"] = cat(["36", 0], ["35", 0])
        g["39"] = cat(["38", 0], ["37", 0])
        g["32"] = {"class_type": "MinimaxH3LatentUpscaler3D", "inputs": {"latent": ["39", 0], "model_name": UPSCALER,
                   "mode": "target dimensions", "mode.width": width, "mode.height": height, "align": 32,
                   "enable_temporal_chunking": False, "force_unload": True, "device": "cuda", "precision": "bf16"}}
        g["41"] = cut(["32", 0], pad, n)
        g["42"] = cat(["41", 0], ["43", 0])
        g["43"] = cut(["41", 0], 0, 2)
        g["33"] = {"class_type": "LTXVConcatAVLatent", "inputs": {"video_latent": ["42", 0], "audio_latent": ["31", 1]}}
        g["34"] = {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["10", 0], "guider": ["13", 0], "sampler": ["11", 0],
                   "sigmas": ["30", 1], "latent_image": ["33", 0]}}
        g["15"]["inputs"]["samples"] = ["34", 0]
    return g


def localize(g, names):
    # the graph's model files under the names this ComfyUI lists them (in a minimax/ subfolder or not)
    for node in g.values():
        field = MODEL_FIELDS.get(node["class_type"])
        if field in node["inputs"]:
            node["inputs"][field] = names.get(_norm(node["inputs"][field]), node["inputs"][field])
    return g


@contextlib.contextmanager
def file_lock(name, wait=True):
    # a lock across processes that Windows drops if its holder dies; yields whether it's held (wait=False: maybe not)
    path = os.path.join(tempfile.gettempdir(), f"loopstudio-{name}.lock")
    held = False
    with open(path, "a") as f:
        while not held:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
                held = True
            except OSError:  # LK_LOCK gives up after 10 s: keep waiting
                if not wait:
                    break
        try:
            yield held
        finally:
            if held:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    try:
        os.remove(path)
    except OSError:  # another process has it open
        pass


def subject_lock(name):
    # one script at a time rewrites a subject's review files (Refresh, a run that just finished, Recycle losses)
    return file_lock(name)


def job_lock(prompt_id, wait=True):
    # held by whoever is making a job's outputs: from when a run queues it until they're done, so a review can tell a
    # render still on its way from one whose run failed or died, however long the encode takes
    return file_lock("job-" + prompt_id, wait)


def post(path, data):
    req = urllib.request.Request(SERVER + path, json.dumps(data).encode(), {"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req)) if path == "/prompt" else urllib.request.urlopen(req).read()
    except urllib.error.HTTPError as e:
        # ComfyUI refused the graph (a node or model it doesn't have, usually): say what, not a traceback
        try:
            body = json.load(e)
        except ValueError:
            body = {}
        why = [body.get("error", {}).get("message", "")] + [f"{n.get('class_type')}: {err.get('message')} {err.get('details', '')}".strip()
                                                            for n in body.get("node_errors", {}).values() for err in n.get("errors", [])]
        sys.exit("ComfyUI refused the render: " + "; ".join(w for w in why if w) +
                 "\nSee Settings > Requirements in Loop Studio (or run: python needs.py) for what's missing.")
    except urllib.error.URLError:
        sys.exit(f"ComfyUI isn't answering at {SERVER}: start it, or fix the ComfyUI URL in Settings.")


def draw_caption(img, text):
    # classic meme caption: white Impact with a black outline, centred near the bottom
    w, h = img.size
    d = ImageDraw.Draw(img)
    size = w // 7
    font = ImageFont.truetype(FONT, size)
    while d.textlength(text, font=font) > w * 0.9:
        size -= 4
        font = ImageFont.truetype(FONT, size)
    d.text((w / 2, h - w // 25), text, font=font, fill="white", stroke_width=max(2, size // 14), stroke_fill="black", anchor="md")


BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]


def pixelate(paths, pixel, out_w, out_h):
    # pixel art from a render: area-average every frame down to a grid of pixel["grid"] cells on the long side, snap them to
    # one palette shared by all frames (pixel["palette"] hex colours, or pixel["colors"] picked from the render) with
    # ordered dithering, which stays put from frame to frame unlike error diffusion, then scale up with hard edges
    grid = pixel.get("grid", 160)
    gw, gh = max(1, grid * out_w // max(out_w, out_h)), max(1, grid * out_h // max(out_w, out_h))
    small = [np.asarray(Image.open(p).convert("RGB").resize((gw, gh), Image.BOX), dtype=np.float32) for p in paths]
    if pixel.get("palette"):
        pal = np.array([hex_rgb(c) for c in pixel["palette"]], dtype=np.float32)
    else:
        strip = Image.fromarray(np.concatenate(small, axis=0).astype(np.uint8))
        pal = np.array(strip.quantize(pixel.get("colors", 16), method=Image.MEDIANCUT).getpalette()[:pixel.get("colors", 16) * 3],
                       dtype=np.float32).reshape(-1, 3)
    bayer = (np.array(BAYER) + 0.5) / 16 - 0.5
    threshold = np.tile(bayer, (gh // 4 + 1, gw // 4 + 1))[:gh, :gw, None] * pixel.get("dither", 32)
    scale = max(1, max(out_w, out_h) // grid)
    for a in small:
        idx = (((a + threshold)[:, :, None, :] - pal) ** 2).sum(-1).argmin(-1)
        yield Image.fromarray(pal[idx].astype(np.uint8)).resize((gw * scale, gh * scale), Image.NEAREST)


def hex_rgb(c):
    return [int(c.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]


def vhs(paths, vhs, out_w, out_h):
    # worn analog video from a render: vhs["lines"] soft scanlines, colour that bleeds and lags to the right of the picture,
    # grain, and per frame a few torn bands and bright streaks. Optional "tint" (hex) makes it a monochrome screen in that
    # colour and "contrast" pushes it towards hard black and white. The randomness is seeded by the render's frames folder,
    # so a GIF rebuilds the same but every GIF glitches differently, and it is equally strong in every frame, so the loop
    # point never stands out
    rng = np.random.default_rng(zlib.crc32(os.path.dirname(paths[0]).encode()) + vhs.get("seed", 0))
    lines = vhs.get("lines", 240)
    w = out_w // 2  # tape resolution: soft across, fixed lines down
    shift, bleed = vhs.get("chroma", 4), vhs.get("bleed", 9)
    kernel = np.ones(bleed) / bleed
    for p in paths:
        ycc = np.asarray(Image.open(p).convert("RGB").resize((w, lines), Image.BOX).convert("YCbCr"), dtype=np.float32).copy()
        y = ycc[:, :, 0]
        if vhs.get("contrast"):
            y[:] = 255 / (1 + np.exp(-vhs["contrast"] * (y / 255 - 0.5)))
        y += rng.normal(0, vhs.get("noise", 8), y.shape)
        for c in (1, 2):
            ch = np.apply_along_axis(lambda r: np.convolve(r, kernel, mode="same"), 1, ycc[:, :, c] - 128)
            ycc[:, :, c] = np.roll(ch, shift, axis=1) * vhs.get("saturation", 1.3) + 128
        for _ in range(rng.poisson(vhs.get("tears", 1.5))):
            y0, n = rng.integers(0, lines), rng.integers(2, 14)
            off = rng.integers(-w // 12, w // 12)
            for k, row in enumerate(range(y0, min(lines, y0 + n))):
                ycc[row] = np.roll(ycc[row], int(off * (k + 1) / n), axis=0)
        for _ in range(rng.poisson(vhs.get("streaks", 1.0))):
            row, x0 = rng.integers(0, lines), rng.integers(0, w)
            ycc[row, x0:x0 + rng.integers(w // 8, w // 2), 0] = 235
        ycc = ycc.clip(0, 255)
        if vhs.get("tint"):
            rgb = (ycc[:, :, :1] / 255 * np.array(hex_rgb(vhs["tint"]), dtype=np.float32)).clip(0, 255)
            img = Image.fromarray(rgb.astype(np.uint8))
        else:
            img = Image.merge("YCbCr", [Image.fromarray(ycc[:, :, c].astype(np.uint8)) for c in range(3)]).convert("RGB")
        a = np.asarray(img.resize((out_w, lines), Image.BILINEAR).resize((out_w, out_h), Image.NEAREST), dtype=np.float32)
        a[(np.arange(out_h) * lines // out_h) != ((np.arange(out_h) + 1) * lines // out_h)] *= 1 - vhs.get("scanlines", 0.35)
        yield Image.fromarray(a.clip(0, 255).astype(np.uint8))


def look_for(stem, subject, concepts):
    # the subject's "pixel" / "vhs" settings, with any the concept overrides
    c = concepts.get(concept_of(stem, concepts), {})
    return {k: {**subject[k], **c.get(k, {})} for k in ("pixel", "vhs") if subject.get(k)}


def job_state(prompt_id):
    # ComfyUI's history entry for the job, "waiting" while it's queued or rendering, None once it has forgotten it
    # (ComfyUI restarted, or the job was taken off the queue)
    h = json.load(urllib.request.urlopen(f"{SERVER}/history/{prompt_id}", timeout=30)).get(prompt_id)
    if h:
        return h
    q = json.load(urllib.request.urlopen(f"{SERVER}/queue", timeout=30))
    return "waiting" if any(e[1] == prompt_id for part in ("queue_running", "queue_pending") for e in q[part]) else None


def convert(prompt_id, subject=None, concepts=None, redo=False):
    # waits for the job; returns the files made (GIF / WebP per config, else the MP4), or None when it failed, was
    # interrupted or ComfyUI no longer knows it
    # the subject's "pixel" / "vhs" settings restyle the GIF (see pixelate, vhs, look_for); redo rebuilds an existing GIF
    down = 0
    while True:
        try:
            h, down = job_state(prompt_id), 0
        except (OSError, ValueError):  # ComfyUI restarting: give it a few minutes
            down += 1
            if down > 15:
                print(f"ComfyUI isn't answering at {SERVER}", flush=True)
                return None
            h = "waiting"
        if h != "waiting":
            break
        time.sleep(20)
    if not h or h["status"]["status_str"] != "success":
        return None
    video = h["outputs"]["21"]["images"][0]
    mp4 = os.path.join(COMFY_OUTPUT, video["subfolder"], video["filename"])
    wanted = [mp4[:-4] + ext for key, ext in OUTPUT_FILES.items() if OUTPUTS.get(key)]
    if not wanted or (not redo and all(os.path.exists(p) for p in wanted)):
        return wanted or [mp4]
    graph_, extra = h["prompt"][2], h["prompt"][3]
    # the video's own size: upscaled renders sample node 9 at the base size
    size = graph_["32"]["inputs"] if "32" in graph_ else graph_["9"]["inputs"]
    full_w, full_h = size.get("mode.width", size.get("width")), size.get("mode.height", size.get("height"))
    look = look_for(os.path.basename(mp4)[:-4], subject, concepts) if subject else {}
    width = GIF_SIZE * full_w // max(full_w, full_h)
    caption = extra.get("caption", extra.get("meme_caption")) if OUTPUTS["captions"] else None
    with tempfile.TemporaryDirectory() as tmp:
        if "22" in h["outputs"]:
            images = h["outputs"]["22"]["images"]
            frames_dir, names = os.path.join(COMFY_OUTPUT, images[0]["subfolder"]), [i["filename"] for i in images]
        else:
            frames_dir, names = tmp, []
            with av.open(mp4) as c:
                for n, frame in enumerate(c.decode(video=0)):
                    names.append(f"{n:05d}.png")
                    frame.to_image().save(os.path.join(tmp, names[-1]))
        # every other frame halves the count and stays evenly spaced across the wrap only for an even loop;
        # GIF sites otherwise drop frames unevenly to reach their own ~15 fps
        fps = 24
        if OUTPUTS["fps"] == 12 and len(names) % 2 == 0:
            names, fps = sorted(names)[::2], 12
        for key, restyle in (("pixel", pixelate), ("vhs", vhs)):
            if key in look:
                styled = []
                for name, img in zip(names, restyle([os.path.join(frames_dir, n) for n in names], look[key], width,
                                                    GIF_SIZE * full_h // max(full_w, full_h))):
                    styled.append(key + "_" + name)
                    img.save(os.path.join(tmp, styled[-1]))
                frames_dir, names, width = tmp, styled, img.width
        if caption:
            captioned = []
            for name in names:
                img = Image.open(os.path.join(frames_dir, name)).convert("RGB")
                draw_caption(img, caption)
                captioned.append("c_" + name)
                img.save(os.path.join(tmp, captioned[-1]))
            frames_dir, names = tmp, captioned
        imgs = None
        for out in wanted:
            if out.endswith(".gif"):
                subprocess.run(["gifski", "--fps", str(fps), "--width", str(width), "--quality", "90", "--quiet", "-o", out, *names],
                               cwd=frames_dir, check=True)
                continue
            if imgs is None:
                # even sizes, which H.264's 4:2:0 needs
                imgs = [Image.open(os.path.join(frames_dir, n)).convert("RGB") for n in names]
                w = width // 2 * 2
                imgs = [im.resize((w, round(im.height * w / im.width) // 2 * 2), Image.LANCZOS) for im in imgs]
            if out.endswith(".webp"):
                imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=round(1000 / fps), loop=0, quality=90, method=4)
            else:
                with av.open(out, "w", options={"movflags": "+faststart"}) as c:
                    stream = c.add_stream("libx264", rate=fps, options={"crf": "18", "preset": "slow"})
                    stream.width, stream.height, stream.pix_fmt = imgs[0].width, imgs[0].height, "yuv420p"
                    for im in imgs:
                        c.mux(stream.encode(av.VideoFrame.from_image(im)))
                    c.mux(stream.encode())
    return wanted


def out_name(name, suffix, model, seed):
    # wink_square_102f_fast_s2024: concept, format, loop frames, model (none for stock), seed (none for 1000)
    return f"{name}{suffix}_{LENGTH - 5}f" + ("" if model == "stock" else f"_{model}") + (f"_s{seed}" if seed != 1000 else "")


def rendered(subject, out):
    # when the render named out was made (its frames folder's time), or None if it hasn't been
    frames_dir = os.path.join(COMFY_OUTPUT, subject["output"], "frames", out)
    return os.path.getmtime(frames_dir) if os.path.isdir(frames_dir) and os.listdir(frames_dir) else None


def new_seed(subject, name, suffix, model):
    # 1000 for a concept's first render in this mode and format, after that a random seed it hasn't had
    seed = 1000
    while rendered(subject, out_name(name, suffix, model, seed)):
        seed = random.randint(1, 99999)
    return seed


def finish(subject_name, prompt_id, subject, concepts):
    # waits for the job, makes its outputs and updates the review. What goes wrong here costs this render, not the
    # concepts still to come. "<id> GIF <path> (2.4 MB)": Loop Studio refreshes the review on these lines. The job's lock
    # stays held through the review below, so that review doesn't straight away retry outputs that just failed
    with job_lock(prompt_id):
        try:
            outs = convert(prompt_id, subject, concepts)
            print(prompt_id[:8], ", ".join(f"{o.rsplit('.', 1)[1].upper()} {o} ({os.path.getsize(o) / 1e6:.1f} MB)" for o in outs)
                  if outs else "failed or interrupted, nothing saved", flush=True)
        except Exception as e:  # a bad font, a gifski error
            print(f"{prompt_id[:8]} outputs failed: {e}; the MP4 is saved, and Refresh tries again", flush=True)
        if subprocess.run([sys.executable, os.path.join(SKILL, "scripts", "review.py"), subject_name]).returncode:
            print("the render is saved, but the review failed (see above): Refresh tries again", flush=True)


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    subject_name, command = sys.argv[1], sys.argv[2]
    subject, concepts = load_subject(subject_name)
    ids_path = os.path.join(subject["data"], "ids.txt")
    flags = [a[2:] for a in sys.argv[3:] if a.startswith("--")]
    names = [a for a in sys.argv[3:] if not a.startswith("--")]

    if command == "preview":
        # the prompt for one concept given as JSON on stdin (Loop Studio's editor)
        c = json.load(sys.stdin)
        print(full_prompt(subject, c))
        return
    if command == "convert":
        for i in (open(ids_path).read().split() if os.path.exists(ids_path) else []):
            with job_lock(i):
                outs = convert(i, subject, concepts, "redo" in flags)
            print(i[:8], ", ".join(outs) if outs else "skipped: failed, interrupted or no longer in ComfyUI's history", flush=True)
        subprocess.run([sys.executable, os.path.join(SKILL, "scripts", "review.py"), subject_name], check=True)
        return
    if command != "render":
        sys.exit(__doc__)

    names = list(concepts) if "all" in flags else names
    unknown = [n for n in names if n not in concepts]
    if not names or unknown:
        sys.exit(f"unknown concepts: {', '.join(unknown)}" if unknown else "name the concepts to render, or pass --all")
    bad = [f"{n}: {concept_problem(concepts[n])}" for n in names if concept_problem(concepts[n])]
    if bad:
        sys.exit("Nothing was rendered, fix these concepts first: " + "; ".join(bad))
    formats = [f for f in flags if f in FORMATS] or ["square"]
    seeds = [int(f[5:]) for f in flags if f.startswith("seed=")]
    # a subject's "model" (e.g. "pika") overrides --fast; its "upscale" is the default for concepts without one
    model = subject.get("model") or ("fast" if "fast" in flags else "stock")
    models = {} if "dry" in flags else installed(SERVER)

    for name in names:
        c = concepts[name]
        prompt = full_prompt(subject, c)
        for fmt in formats:
            suffix, w, h = FORMATS[fmt]
            for seed in seeds or [new_seed(subject, name, suffix, model)]:
                out = out_name(name, suffix, model, seed)
                if "dry" in flags:
                    print(f"--- {out} (seed {seed}, caption: {c.get('caption')})\n{prompt}\n")
                    continue
                made = rendered(subject, out)
                if made and "redo" not in flags:
                    # "skip " starts the line: Loop Studio counts these, and a run that only skipped says so
                    print(f"skip {out}: seed {seed} was already rendered {time.strftime('%Y-%m-%d %H:%M', time.localtime(made))}; "
                          "leave out the seed for a new one, or --redo renders it again", flush=True)
                    continue
                # a concept can bring its own reference (e.g. a retouched copy of the subject's)
                g = localize(graph(prompt, seed, f"{subject['output']}/{out}", w, h, c.get("reference", subject["reference"]), model,
                                   c.get("upscale", subject.get("upscale")), loras(model, subject, c)), models)
                # ComfyUI only reports progress to the client that queued the job; Loop Studio listens as "loopstudio"
                prompt_id = post("/prompt", {"prompt": g, "client_id": "loopstudio", "extra_data": {"caption": c.get("caption")}})["prompt_id"]
                open(ids_path, "a", newline="\n").write(prompt_id + "\n")
                print("queued", out, prompt_id, flush=True)
                finish(subject_name, prompt_id, subject, concepts)
                post("/free", {"unload_models": True, "free_memory": True})


if __name__ == "__main__":
    main()
