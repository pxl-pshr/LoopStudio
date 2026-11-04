"""What the render graph needs from ComfyUI (nodes, models, version), and a check of a running ComfyUI against it.

Shared by loop_gifs.py (the names it puts in the graph), the app (Settings → Requirements, and refusing renders that
can't run) and `python needs.py` for a quick report. Only needs the standard library.
"""
import difflib
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

MIN_COMFY = (0, 42, 0)  # MiniMax H3 support in ComfyUI core
# model: (diffusion model, steps); all of them in models/diffusion_models/minimax/
MODELS = {"stock": ("minimax\\minimax_h3_ref2va_pruned_int8_convrot.safetensors", 25),
          "fast": ("minimax\\fasth3_omniref_8step_pruned_int8_convrot.safetensors", 8),
          "pika": ("minimax\\minimax_h3_ref2va_pruned_int8_convrot.safetensors", 6)}
PIKA_LORA = "minimax\\pika_h3_flex_ref2va_v1_bf16.safetensors"  # models/loras/minimax/
VBVR_LORA = "minimax\\H3-Licon-VBVR-I2V.safetensors"  # models/loras/minimax/
# the default render: the stock model with Pika's Ref2VA LoRA, then Licon's VBVR LoRA, on the stock sampler (25 steps,
# res_multistep). A subject's or concept's own "lora" replaces it ([] renders the bare stock model).
DEFAULT_LORAS = [{"file": PIKA_LORA, "strength": 1.0}, {"file": VBVR_LORA, "strength": 1.0}]
UPSCALER = "minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors"  # models/latent_upscale_models, LBH-123-AI node pack
CLIP = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"  # models/text_encoders/
VAE_V = "minimax\\minimax_h3_video_vae_fp16.safetensors"  # models/vae/minimax/
VAE_A = "minimax\\minimax_h3_audio_vae_fp32.safetensors"

CORE_NODES = ["MiniMaxH3ReferenceToVideo", "LTXVSeparateAVLatent", "LTXVConcatAVLatent", "LatentCut", "LatentConcat",
              "ManualSigmas", "SplitSigmas", "SaveVideo", "CreateVideo", "UNETLoader", "CLIPLoader", "VAELoader"]
LOOP_NODES = ["MiniMaxH3Loop", "MiniMaxH3LoopDecode"]
LOOP_NODE_URL = "https://github.com/pxl-pshr/ComfyUI-MiniMaxH3-Loop"

# where each model comes from, for Settings > Requirements' Download buttons:
# check key -> (Hugging Face repo, path in it, bytes, ComfyUI folder type, subfolder to save it in)
DOWNLOADS = {
    "model": ("Comfy-Org/MiniMax-H3", "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors", 20970379616, "diffusion_models", "minimax"),
    "clip": ("Comfy-Org/MiniMax-H3", "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", 15687142551, "text_encoders", ""),
    "vae_v": ("Comfy-Org/MiniMax-H3", "vae/minimax_h3_video_vae_fp16.safetensors", 5207808496, "vae", "minimax"),
    "vae_a": ("Comfy-Org/MiniMax-H3", "vae/minimax_h3_audio_vae_fp32.safetensors", 605254808, "vae", "minimax"),
    "pika": ("Comfy-Org/MiniMax-H3", "loras/pika_h3_flex_ref2va_v1_bf16.safetensors", 1956172304, "loras", "minimax"),
    "vbvr": ("LiconStudio/Minimax-H3-VBVR", "H3-Licon-VBVR-I2V.safetensors", 149238584, "loras", "minimax"),
    "fast": ("FastVideo/FastH3-Omni-Comfy", "diffusion_models/fasth3_omniref_8step_pruned_int8_convrot.safetensors", 22213723064, "diffusion_models", "minimax"),
    "upscale_model": ("LBH-123-AI/Minimax_h3_latent_Upscaler",
                      "minimax_h3_latent_upscaler_3d_conv_v1/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors", 690592992,
                      "latent_upscale_models", ""),
}
# custom node packs, for the Install buttons: check key -> (git repo, folder name in custom_nodes)
NODE_INSTALLS = {"loop": (LOOP_NODE_URL, "ComfyUI-MiniMaxH3-Loop"),
                 "upscale": ("https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler", "Comfyui_Minimax_h3_latent_Upscaler")}
# gifski, for the Install button: the Windows program in gif.ski's zip, checked against this hash before it's used. It goes
# in Loop Studio's own tools/ folder, not on PATH. Its .msi (also what winget installs) has no PATH entry, so it isn't used.
GIFSKI_ZIP = "https://gif.ski/gifski-1.32.0.zip"
GIFSKI_EXE = "win/gifski.exe"  # in the zip
GIFSKI_SHA256 = "30c0ad69324c0c3d4d71670be39d34eb626bf15f3a39ab64df874579380298ab"
TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tools")


def gifski():
    # the gifski to run: one on PATH, else the one the Install button put in tools/; None when there's neither
    local = os.path.join(TOOLS, "gifski.exe")
    return shutil.which("gifski") or (local if os.path.isfile(local) else None)


def download_url(key):
    repo, path = DOWNLOADS[key][:2]
    return f"https://huggingface.co/{repo}/resolve/main/{path}"


def get_info(key):
    # how to get a check's file: a model download, a node install or gifski, for the app (None: none of them)
    if key == "gifski":
        return {"kind": "tool", "url": GIFSKI_ZIP}
    if key in DOWNLOADS:
        repo, path, size, folder_type, sub = DOWNLOADS[key]
        return {"kind": "model", "url": download_url(key), "page": f"https://huggingface.co/{repo}", "size": size,
                "type": folder_type, "sub": sub, "file": path.rsplit("/", 1)[-1]}
    if key in NODE_INSTALLS:
        return {"kind": "node", "git": NODE_INSTALLS[key][0], "dir": NODE_INSTALLS[key][1]}
    return None


def loras(model, subject, concept):
    # the LoRAs a concept renders with: its own "lora", else its subject's, else the default stack (stock model only)
    return concept.get("lora", subject.get("lora", DEFAULT_LORAS if model == "stock" else None))


def concept_problem(c):
    # why a concept can't make a prompt, in words; None when it can. Shared by the app (saving, previewing, refusing a
    # render) and loop_gifs.py, so a concept.json edited by hand can't stop a batch halfway
    filled = lambda key: str(c.get(key) or "").strip()
    if "visual" in c:
        return None if filled("visual") else "its Visual is empty"
    if c.get("template") not in ("studio", "scene"):
        return "it needs a visual or a studio/scene template"
    if c["template"] == "scene" and not filled("setting"):
        return "its Setting is empty"
    return None if filled("action") else "its Action is empty"


def _get(url, path):
    with urllib.request.urlopen(url + path, timeout=10) as r:
        return json.load(r)


def _options(info, node, field):
    # the choices of a node's dropdown input, in either of ComfyUI's two formats
    spec = info.get(node, {}).get("input", {}).get("required", {}).get(field)
    if not spec:
        return []
    return spec[0] if isinstance(spec[0], list) else spec[1].get("options", [])


def _norm(name):
    # a model is found wherever it sits in its models folder (minimax/ subfolder or not), on any OS
    return name.replace("\\", "/").rsplit("/", 1)[-1].lower()


MODEL_FIELDS = {"UNETLoader": "unet_name", "CLIPLoader": "clip_name", "VAELoader": "vae_name",
                "LoraLoaderModelOnly": "lora_name", "MinimaxH3LatentUpscaler3D": "model_name"}


def installed(url):
    # {file name: the name this ComfyUI lists it under (with its subfolder)}, for the graph's model fields
    out = {}
    for node, field in MODEL_FIELDS.items():
        try:
            info = _get(url, "/object_info/" + node)
        except (OSError, ValueError):
            continue
        for o in _options(info, node, field):
            out.setdefault(_norm(o), o)
    return out


def check(url, gif=True):
    """[{key, name, ok, need, core, fix, note, get}] for a ComfyUI at url. need: "required", or the feature it's for
    (default renders, Fast, ...); core: needed for a default render; fix: what to do, in words; note: the part of fix the
    app's buttons don't cover (similar names ComfyUI lists); get: how to fetch it (see get_info)."""
    items = []

    def add(key, name, ok, need, fix, note=""):
        items.append({"key": key, "name": name, "ok": bool(ok), "need": need, "fix": "" if ok else fix, "note": "" if ok else note,
                      "core": need == "required" or need.startswith("default renders"), "get": get_info(key)})

    try:
        version = _get(url, "/system_stats")["system"].get("comfyui_version", "0")
    except (OSError, ValueError, KeyError):
        add("comfy", f"ComfyUI running at {url}", False, "required", "Start ComfyUI, or fix the ComfyUI URL under Advanced.")
        return items
    parts = tuple(int(p) for p in (version.split("-")[0].split(".") + ["0", "0"])[:3] if p.isdigit())
    add("version", f"ComfyUI {version} (needs {'.'.join(map(str, MIN_COMFY))}+)", parts >= MIN_COMFY, "required",
        "Update ComfyUI (git pull, or the portable update script / ComfyUI Manager).")

    names = CORE_NODES + LOOP_NODES + ["MinimaxH3LatentUpscaler3D", "LoraLoaderModelOnly"]
    info = {}
    for n in names:
        try:
            info.update(_get(url, "/object_info/" + urllib.parse.quote(n)))
        except (OSError, ValueError):
            pass
    missing_core = [n for n in CORE_NODES if n not in info]
    add("core", "MiniMax H3 nodes in ComfyUI", not missing_core, "required",
        f"Missing {', '.join(missing_core)}: update ComfyUI.")
    add("loop", "ComfyUI-MiniMaxH3-Loop node", all(n in info for n in LOOP_NODES), "required",
        f"Install it into ComfyUI/custom_nodes ({LOOP_NODE_URL}), then restart ComfyUI.")

    def model(key, label, node, field, file, need):
        listed = _options(info, node, field)
        have = {_norm(o) for o in listed}
        # when it's missing, the closest names ComfyUI does list (any of its model paths, extra_model_paths.yaml
        # included): a renamed or half-downloaded copy shows up here
        stem = lambda n: os.path.splitext(n)[0]
        near = [] if _norm(file) in have else [h for h in have if stem(h) in
                difflib.get_close_matches(stem(_norm(file)), [stem(x) for x in have], n=3, cutoff=0.7)]
        folder_type = DOWNLOADS[key][3]
        note = (f"ComfyUI lists similar files: {', '.join(o for o in listed if _norm(o) in near)}; the name has to match exactly."
                if near else f"ComfyUI lists {len(listed)} files for {node} and none is close to this name.")
        add(key, f"{label}: {file.replace(chr(92), '/').rsplit('/', 1)[-1]}", _norm(file) in have, need,
            f"Download it ({download_url(key)}) into ComfyUI/models/{folder_type}/ or a {folder_type} folder in "
            "extra_model_paths.yaml. " + note, note)

    model("model", "H3 model", "UNETLoader", "unet_name", MODELS["stock"][0], 'default renders, and "model": "pika"')
    model("clip", "Text encoder", "CLIPLoader", "clip_name", CLIP, "required")
    model("vae_v", "Video VAE", "VAELoader", "vae_name", VAE_V, "required")
    model("vae_a", "Audio VAE", "VAELoader", "vae_name", VAE_A, "required")
    model("pika", "Pika H3 Flex LoRA", "LoraLoaderModelOnly", "lora_name", PIKA_LORA, 'default renders, and "model": "pika"')
    model("vbvr", "VBVR LoRA", "LoraLoaderModelOnly", "lora_name", VBVR_LORA, "default renders")
    model("fast", "FastH3 Omni model", "UNETLoader", "unet_name", MODELS["fast"][0], "Fast renders")
    add("upscale", "MiniMax H3 latent upscaler node", "MinimaxH3LatentUpscaler3D" in info, '"upscale" renders',
        f"Install it into ComfyUI/custom_nodes ({NODE_INSTALLS['upscale'][0]}), then restart ComfyUI.")
    if "MinimaxH3LatentUpscaler3D" in info:  # the node lists its own models
        model("upscale_model", "Latent upscaler model", "MinimaxH3LatentUpscaler3D", "model_name", UPSCALER, '"upscale" renders')
    else:
        add("upscale_model", f"Latent upscaler model: {UPSCALER}", False, '"upscale" renders',
            f"Download it ({download_url('upscale_model')}) into ComfyUI/models/latent_upscale_models/; "
            "ComfyUI lists it once the upscaler node is installed.", "ComfyUI lists it once the upscaler node is installed.")
    add("gifski", "gifski, which makes the GIFs", gifski(), "GIF output" if gif else "GIF output (off)",
        f"Press Install in Settings > Requirements, or download {GIFSKI_ZIP} and copy win\\gifski.exe from it to a folder on PATH. "
        "Or turn GIF off in Outputs.", "No Rust needed. Or turn GIF off in Outputs, below.")
    items[-1]["core"] = gif  # with GIF on, as it is by default, a render can't finish without it
    return items


# the ways to render, with minutes per GIF measured on an RTX 4090 (24 GB): 992 px, 102 frames, models already loaded
# (the first render of a session also loads them, a few minutes more). None: not measured.
MODES = [
    {"key": "stock", "label": "H3 + Pika + VBVR LoRAs, 25 steps", "needs": ["model", "pika", "vbvr"], "minutes": 15.0,
     "how": "the default", "note": "our pick so far, on a small sample; the slowest"},
    {"key": "fast", "label": "FastH3, 8 steps", "needs": ["fast"], "minutes": 4.5,
     "how": "the Fast box, or --fast", "note": "about a third of the time; a punchier look, can judder on fast motion"},
    {"key": "fast_up", "label": "FastH3 + latent upscale", "needs": ["fast", "upscale", "upscale_model"], "minutes": 3.1,
     "how": '--fast with "upscale": {"base": 512, "refine_steps": 4} in subject.json', "note": "renders at 512, upscales to 992; often sharper"},
    {"key": "pika", "label": "Pika H3 Flex + latent upscale", "needs": ["model", "pika", "upscale", "upscale_model"], "minutes": 2.5,
     "how": '"model": "pika" and "upscale": {"base": 512, "refine_steps": 3} in subject.json', "note": "strong likeness of a reference; the quickest"},
]


def modes(items):
    # MODES with what this ComfyUI is missing for each (required checks count for all of them)
    failed = {i["key"]: i["name"] for i in items if not i["ok"]}
    required = [name for key, name in failed.items() if next(i for i in items if i["key"] == key)["need"] == "required"]
    return [m | {"missing": required + [failed[k] for k in m["needs"] if k in failed]} for m in MODES]


def blocking(items, fast=False, pika=False, upscale=False, gif=True, stack=False):
    # the failed checks that stop this render: everything required, plus what its options use (stack: the default LoRAs;
    # FastH3 is its own model, every other mode the stock one)
    wanted = {"model": not fast, "fast": fast, "pika": pika or stack, "vbvr": stack, "upscale": upscale, "upscale_model": upscale, "gifski": gif}
    return [i for i in items if not i["ok"] and (i["need"] == "required" or wanted.get(i["key"]))]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # ✓ / ✗ on a Windows console
    for i in check(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8188"):
        print("✓" if i["ok"] else "✗", i["name"], "" if i["need"] == "required" else f"(for {i['need']})", i["fix"])
