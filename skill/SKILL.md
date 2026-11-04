---
name: loop-gifs
description: Make seamless looping GIFs (memes, reactions, art loops) of a subject (a character from a reference image, or a pure style) with MiniMax H3 Loop on a local ComfyUI, review them on a Win/Lose grid in Loop Studio, and rerun the Losses with fixed prompts. Use for new subjects or concepts, renders, reviewing loops, or rerunning losing GIFs.
---

# Loop GIFs

Pipeline: write loop-safe concepts → render each as a seamless H3 loop → GIF / WebP → the user reviews (Win/Lose) →
rerun Losses with fixed prompts → recycle the Losses.

Inside Loop Studio, start renders with `python studio.py render ...` (see AGENTS.md) so they show up in the app. The
scripts can also run on their own, with ComfyUI's Python (`python studio.py state` prints its path) from `scripts/`:

```
<comfy python> loop_gifs.py <subject> render <concept ...> [--seed=N ...] [--fast] [--vertical | --wide] [--redo] [--dry]
<comfy python> loop_gifs.py <subject> render --all
<comfy python> loop_gifs.py <subject> convert [--redo]      (GIF / WebP again, e.g. after changing outputs)
<comfy python> review.py <subject>
<comfy python> recycle.py <subject>
```

Folders, output formats and the agent are set in the app's Settings panel (`config.json`; see `scripts/config.py`).

Renders take ~15 min each on a 4090 (992x992, 25 steps, with the default LoRAs): run big batches in the background. The script submits one
job at a time and unloads models between jobs (the text encoder / video model swap otherwise builds up until Windows
spills VRAM and steps crawl). A concept and seed whose frames exist are skipped (with the date they were rendered), so
a rerun of the same command resumes. Without `--seed` each concept gets a seed it hasn't had in that mode (1000 the
first time), so asking for "another one" renders something new.

## Requirements

- ComfyUI v0.42+ with `custom_nodes/ComfyUI-MiniMaxH3-Loop`; models
  `minimax_h3_ref2va_pruned_int8_convrot`, `qwen3vl_32b_minimax_h3_nvfp4_awq`, H3 video/audio VAEs (in `minimax\` subfolders).
- `gifski` on PATH (PNG input only) for GIFs; a caption font (Impact by default).
- `models/loras/minimax/`: `pika_h3_flex_ref2va_v1_bf16` and `H3-Licon-VBVR-I2V`, for the default render.
- Default render: the stock model with Pika's Ref2VA LoRA and then the VBVR LoRA stacked on it, on the stock sampler
  (25 steps, res_multistep, simple scheduler; not Pika's own 6-step lcm schedule). Our pick so far, on a small sample (all
  5 test renders kept; its seam metric was no better than the bare stock model's). A subject's or concept's `"lora"` replaces the stack: `{"file": "minimax\\x.safetensors", "strength": 1.0}` or a
  list of them in order; `"lora": []` renders the bare stock model. `--fast` uses FastH3-Omni at 8 steps without
  LoRAs (~1/3 the time, slightly punchier look, can judder on fast motion), named `_fast`. `../MODELS.md` lists the
  models, LoRAs and upscalers to try.
- Optional, per subject (`subject.json`): `"model": "pika"` renders with Pika H3 Flex (stock model + Pika's Ref2VA LoRA,
  6 steps, named `_pika`); `"upscale": {"base": 512, "refine_steps": 3}` renders at 512 (long side) and latent-upscales to full size with the
  MiniMax H3 latent upscaler node pack, then refines (about 30% faster, often sharper). A concept can set its own `upscale`.
- Real people: concept prompts describe the gesture only; keep it benign, and nothing goes public without the user's say-so.

## Subjects

`<subjects>/<name>/subject.json`: `reference` (image in ComfyUI/input; the app flattens transparency onto white),
`subject` (how the prompt names them, must include `<Picture 1>` when there's a reference), `keep` (the trait held through
every GIF, e.g. a strained smile, or the style of a style subject), `he`/`his`, `base_tags` (shared tags), `output`
(folder under ComfyUI/output), `data` (run data folder, relative to the data folder, which is the subjects folder unless
config.json sets `data_dir`: then a subject's data sits next to its subject.json).
Optional `pixel` ({grid, palette or colors, dither}) turns every GIF into real pixel art: frames area-averaged to a
`grid`-cell grid, snapped to one palette (hex list, or `colors` picked from the render) with ordered dithering, scaled up
with hard edges. Optional `vhs` gives a worn analog-video look. The MP4/frames stay clean masters; `convert --redo`
rebuilds the GIFs after changing these. The bundled `example` subject is a pixel-art style subject with no reference; its `examples/` folder holds two
finished renders (GIF + render info) that the app shows until real renders replace them.

`<subjects>/<name>/concepts.json`: list of `{name, caption, sound, tags, ...}` plus either a literal `visual` (with
`{subject}`, `{Subject}`, `{keep}` placeholders) or a template:
- `"template": "studio"` + `action` (+ optional `framing`, default "from the chest up"): plain white studio, centered, facing camera. For gestures and expressions.
- `"template": "scene"` + `setting` (use `{Subject}` in it) + `action`: a real place. For awkward situations.

`caption` (or null) is drawn on the GIF in white Impact; never ask H3 to render text. `tags` are the concept's own
tags; `base_tags` are added to every GIF. New concepts for an existing subject: append to concepts.json, then check
their prompts with `<comfy python> loop_gifs.py <subject> render <concept ...> --dry` (renders nothing).
Replacing a losing concept: add `<name>_v2` (tags carry over by name prefix) instead of editing the original.

## Writing concepts that loop and land

Read **`PROMPTING.md`** first: the general guide to H3 loop prompts (the one rule, what does and doesn't loop, sound,
camera travel, rewrites and examples), the same as in the ComfyUI-MiniMaxH3-Loop node. In this skill the script builds
the prompt around each concept: it always adds the closing line ("Continuous, smooth, unbroken motion throughout.")
and the `overall_soundscape:` / `non_diegetic_music:` lines (from the concept's `sound`), the studio / scene templates
also add the locked-off camera line, and the length is fixed at 107. So a literal `visual` starts with the camera line
and holds only the description; check the full prompt with `--dry` or the app's prompt preview. On top of the guide,
for memes:

- One continuous or cyclic action, written big and cartoony. Face the camera the whole time.
- Keep the face large: chest-up or knees-up. Head-to-toe makes the face tiny at 480 px.
- Any pose change must return. Better: hold the pose "from the very first frame" and only animate something cyclic
  (raising a mug and never lowering it, or a pointing hand coming in partway, both jump at the loop point).
- Things that pile up (spills, sweat, rain on them, confetti on the floor) must already be there from the first frame.
- Mouthed phrases: "mouths the words '...' exactly once", starting and ending on a closed-mouth smile. "Over and over"
  fits the phrase twice into the 4.25 s loop. Mouthing is a seed lottery: render 2-3 seeds.
- Don't write "sleepily" / "eyelids heavy" unless closed eyes are the point.
- Spell out fine gestures: "only his left eye closes while his right eye stays wide open" (wink).
- Reference photos pull composition (an off-centre subject stays off-centre): the studio template demands the face exactly centered.
- Turbulent texture (fire, smoke, rain, sand) can pop at the loop point; keep it background.
- Sound lines still matter for silent GIFs: H3 renders picture and sound together, and loud sound cues enlarge motion.
- Not for exact geometric/abstract loops (kaleidoscopes, mandalas, line art): H3 only approximates symmetry and flickers.
  Those look far better as shaders / generative code.

## Output format

Length 107 → a 102-frame loop at 24 fps (4.25 s). Three aspects, about a megapixel each: 1:1 992x992 (default,
named `_square`), 9:16 704x1280 (`_vertical`) and 16:9 1280x704 (`_wide`); pick one in the render bar or with
`studio.py render ... --format vertical|wide`. Each aspect keeps its own seeds. Keep the subject centred for 9:16 and
leave room on the sides for 16:9; the studio template's centred framing suits all three. By default the GIF takes every other frame → 51 frames at 12 fps,
480 px, ~2-3.5 MB, which GIF sites keep untouched (they tend to resample anything over ~100 frames unevenly). Settings → Outputs
switches GIF / animated WebP / shareable MP4 (`.share.mp4`), size, 24 fps and captions. The full-size 24 fps MP4 and the PNG frames are always kept.

## Review

`review.py <subject>` rebuilds the subject's `renders.json` (and a standalone `review.html` in its output folder) after
every render: each render's prompt, seed, timing and loop metrics (optical-flow numbers, kept as rough
background only: they flagged most of the user's Wins, so they're no verdict and the app doesn't show them). The user marks Win/Lose and notes in Loop Studio; they land in the subject's `results.json`.

To diagnose a Loss before rerunning: contact sheet of frames 0..101 every ~14 frames (PIL, frames folder) plus the
metrics. Fix the cause in a `_v2` concept and render it with two seeds: `python studio.py render <subject> <name>_v2 --seeds 1000,2024`.

## Housekeeping

- `recycle.py <subject>` (the app's Recycle losses) sends every Loss's MP4, GIF and frames to the Recycle Bin (restorable; never hard-delete).
- Don't publish or upload anything without the user asking.
