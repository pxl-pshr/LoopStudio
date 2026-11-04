# Models, LoRAs and upscalers

Loop Studio renders with MiniMax H3 on your own ComfyUI. This page lists every model and node it can use, where to get
it, and how to try it on your own subjects. `python studio.py models` lists the render modes your ComfyUI can run.
Whether you may use a model, and what you may do with its output, is yours to check against its own license: the H3
models are under the [MiniMax H3 Community License](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE).

## Getting them: Settings → Requirements

The easiest way is from the app. Open **⚙ Settings → Requirements**. It checks the running ComfyUI and lists everything
there, each with a ✓ or ✗:

- **Download** on a missing model saves it straight from Hugging Face into a ComfyUI folder you pick. The list holds
  every folder your ComfyUI reads that type from, including the ones in `extra_model_paths.yaml` (a NAS, a second drive).
  It suggests the folder where you already keep that type. **Download the … missing for default renders** gets them all,
  one file at a time. A stopped or broken download resumes where it left off, and each file is checked against its
  size. Set `HF_TOKEN` in the environment if you need a Hugging Face login.
- **Install** on a missing node pack puts it in ComfyUI's `custom_nodes`, using `git clone` (or a zip from GitHub
  when git isn't installed). Restart ComfyUI afterwards so it loads.
- **Open folder** opens the folder a file belongs in, and **direct link** is the file itself, if you'd rather
  download it yourself.

A model counts as installed when ComfyUI lists it, wherever it is: directly in its folder or in a subfolder such as
`minimax/`, in ComfyUI's own `models` folder or an extra path. The file name has to match exactly. When it doesn't,
Requirements shows the similar names ComfyUI does list. After adding a file by hand, press **Check again**. If it still
shows ✗, restart ComfyUI. The same check runs in a terminal: `python skill/scripts/needs.py`.

## The default render

Unless a subject or concept says otherwise, every render uses:

```
H3 stock model ─▶ Pika H3 Flex Ref2VA LoRA (1.0) ─▶ VBVR LoRA (1.0) ─▶ MiniMax H3 Loop ─▶ 25 steps, res_multistep, simple
```

This stacks both LoRAs on the stock model but keeps the stock sampler. Pika's own 6-step `lcm` schedule isn't used.
It's our pick so far, on a small sample: all 5 of our test renders with it were kept. Its loop seam metric wasn't better
than the bare stock model's, so compare the two on your own subject. It takes about **15 min per GIF on an RTX 4090**
(992×992, 102 frames). 9:16 and 16:9 renders have about the same pixel count, so expect about the same; we haven't timed them.

## Required: about 44.6 GB

You also need ComfyUI 0.42 or newer, which has the H3 nodes built in. For FastH3 renders only, you can leave out the H3
model and both LoRAs.

| What | File (direct download) | Size | ComfyUI folder | Repo |
|---|---|---|---|---|
| H3 model | [minimax_h3_ref2va_pruned_int8_convrot.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors) | 21.0 GB | `models/diffusion_models/` | [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3) |
| Text encoder | [qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors) | 15.7 GB | `models/text_encoders/` | [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3) |
| Video VAE | [minimax_h3_video_vae_fp16.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_fp16.safetensors) | 5.2 GB | `models/vae/` | [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3) |
| Audio VAE | [minimax_h3_audio_vae_fp32.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors) | 0.6 GB | `models/vae/` | [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3) |
| Pika H3 Flex Ref2VA LoRA | [pika_h3_flex_ref2va_v1_bf16.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/loras/pika_h3_flex_ref2va_v1_bf16.safetensors) | 2.0 GB | `models/loras/` | [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3) (original: [Pika-Labs/Pika-H3-Flex](https://huggingface.co/Pika-Labs/Pika-H3-Flex)) |
| VBVR LoRA | [H3-Licon-VBVR-I2V.safetensors](https://huggingface.co/LiconStudio/Minimax-H3-VBVR/resolve/main/H3-Licon-VBVR-I2V.safetensors) | 149 MB | `models/loras/` | [LiconStudio/Minimax-H3-VBVR](https://huggingface.co/LiconStudio/Minimax-H3-VBVR) |
| Loop node | ComfyUI-MiniMaxH3-Loop | | `custom_nodes/` | [pxl-pshr/ComfyUI-MiniMaxH3-Loop](https://github.com/pxl-pshr/ComfyUI-MiniMaxH3-Loop) |

## Optional

| What it adds | File (direct download) | Size | ComfyUI folder | Repo |
|---|---|---|---|---|
| Fast renders (8 steps) | [fasth3_omniref_8step_pruned_int8_convrot.safetensors](https://huggingface.co/FastVideo/FastH3-Omni-Comfy/resolve/main/diffusion_models/fasth3_omniref_8step_pruned_int8_convrot.safetensors) | 22.2 GB | `models/diffusion_models/` | [FastVideo/FastH3-Omni-Comfy](https://huggingface.co/FastVideo/FastH3-Omni-Comfy) |
| Two-stage renders: 512 px, then upscaled to full size | [minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors](https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler/resolve/main/minimax_h3_latent_upscaler_3d_conv_v1/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors) | 0.7 GB | `models/latent_upscale_models/` | [LBH-123-AI/Minimax_h3_latent_Upscaler](https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler) |
| …and its node pack | Comfyui_Minimax_h3_latent_Upscaler | | `custom_nodes/` | [LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler) |

## Render modes to compare

The times are minutes per GIF on an RTX 4090 (24 GB) with the models already loaded. The first render of a session also
loads the models, which adds a few minutes. Once you've rendered in a mode, `python studio.py models` shows your own
times.

| Mode | How to pick it | Min / GIF | Character |
|---|---|---|---|
| **Default**: H3 + Pika + VBVR LoRAs, 25 steps | nothing to set | ~15 | Our pick so far, on a small sample |
| Bare stock H3, 25 steps | `"lora": []` in subject.json | ~12.7 | The plain model, as a baseline |
| FastH3 Omni, 8 steps | the **FastH3** box in the render bar, or `--fast` | ~4.5 | About a third of the time; a punchier look, can judder on fast motion |
| FastH3 + latent upscale | FastH3, plus `"upscale": {"base": 512, "refine_steps": 4}` in subject.json | ~3.1 | Renders at 512, upscales to full size; often sharper |
| Pika H3 Flex, 6 steps + latent upscale | `"model": "pika"` and `"upscale": {"base": 512, "refine_steps": 3}` in subject.json | ~2.5 | Pika's own lcm schedule; strong likeness to a reference; the quickest |

Renders in each mode get their own name suffix (`_fast`, `_pika`; none for the default), so they sit side by side in
the review grid. Each mode keeps its own seeds.

## The latent upscaler

`"upscale": {"base": 512, "refine_steps": N}` works in any mode. Put it in subject.json for every concept, or on one
concept. Loop Studio samples the loop at `base` px on the long side and upscales the video latent to full size with the
MiniMax H3 latent upscaler. It then re-runs the schedule's last `N` steps at full size. The loop's two ends are wrapped
around each other before upscaling, so the seam stays clean.

The modes above use `base` 512 with `refine_steps` 3 or 4; we haven't tested other values. A higher `base` takes longer,
and more `refine_steps` re-sample more of the video at full size, so compare a few on your own renders.

## Trying any other LoRA

Any H3 LoRA that loads with ComfyUI's `LoraLoaderModelOnly` can go on the model. Set `"lora"` in subject.json (for every
concept) or on one concept in concepts.json. It **replaces** the default stack:

```json
"lora": {"file": "minimax\\my_h3_lora.safetensors", "strength": 0.8}
```

To stack several LoRAs, give a list. They're applied in order:

```json
"lora": [
  {"file": "minimax\\pika_h3_flex_ref2va_v1_bf16.safetensors", "strength": 1.0},
  {"file": "minimax\\H3-Licon-VBVR-I2V.safetensors", "strength": 0.6}
]
```

`"lora": []` renders without any LoRA. The `file` is the name ComfyUI lists in its LoRA dropdown (with a subfolder if
the file is in one). In JSON, write each backslash twice.

To compare a LoRA against the default, copy a concept in concepts.json as `<name>_<lora>` (for example `wave_vbvr06`)
and set `"lora"` on the copy only. Render both with the same seed, and they appear next to each other in the review grid.

Keep in mind:

- The LoRA setting only changes the model. The steps and sampler stay those of the mode (25 steps of `res_multistep` by
  default). Turbo / step-distilled LoRAs expect their own schedule, so they may not suit 25 steps; we haven't tested any.
- We haven't tested LoRAs across H3 variants (T2V, FL2VA, Ref2VA) either: check a few seeds before trusting the result.
- Every LoRA adds some VRAM and time. We've only rendered stacks of up to two.
