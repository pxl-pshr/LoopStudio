# You're inside Loop Studio

This agent session runs in the terminal drawer of **Loop Studio** (http://127.0.0.1:8190), the user's web app for
making looping GIFs with MiniMax H3 on their local ComfyUI. The user is looking at the app right now, with this
terminal open. For any render, concept, prompt or review task, read **`skill/SKILL.md`** first: it holds
the pipeline, and points to `skill/PROMPTING.md` for writing prompts that loop. `MODELS.md` lists the models, LoRAs
and upscalers a render can use.

## Start by checking what the user has open

```
python studio.py state
```

It prints the open subject, tab, selected concept(s) and filter, the run queue, ComfyUI's status, and where the subject
files, run data, renders and ComfyUI's Python are. "This one", "these" or "the selected ones" mean what it reports.

## First run

If `studio.py state` says **first run**, start there, whatever the user asked: run `python studio.py models` and tell
them in plain words which render modes their ComfyUI can run, what's missing for the others (point them to **Settings →
Requirements**, where each missing model downloads into a ComfyUI folder of their choice and each missing node pack
installs with a button; don't download the models yourself, they're tens of GB), and
roughly how long one GIF takes in each (their own timings when it has them, else the RTX 4090 figures, said as such).
Recommend one to start with, e.g. a concept of a subject `studio.py state` lists (the bundled `example` one on a fresh
install), and **ask them to confirm before rendering anything**. Once they've confirmed, run `python studio.py onboarded`.

## Do things through the app, so they show up in it

- **Render:** `python studio.py render <subject> <concept ...> [--seeds 1000,2024] [--fast] [--format square|vertical|wide] [--redo]`.
  `--format`: 1:1 992x992 (the default), 9:16 704x1280 or 16:9 1280x704 (also the render bar's aspect picker). Don't run
  `skill/scripts/loop_gifs.py render` directly: the app's Runs tab, live progress and review grid only follow runs it
  started. One run renders at a time; queue more and they wait. The default render is the stock model with the Pika and
  VBVR LoRAs stacked on it (~15 min a GIF on an RTX 4090); `--fast` = FastH3 (~1/3 the time).
  Leave out `--seeds` and each concept gets a seed it hasn't been rendered with (a given seed renders the same video
  again, so an existing one is skipped unless `--redo`). It follows the run to its end, printing ComfyUI's steps, and
  ends with **done**, **skipped** (nothing new rendered) or **failed**: tell the user which. A Stock H3 render takes
  longer than a foreground command may run: start it in the background, or with `--no-wait` and check `studio.py runs`.
- **Refresh the review** after renders or file changes outside a run: `python studio.py refresh <subject>`. It also
  picks up renders whose run ended early (the app closed mid-render) and makes their GIF and loop metrics; the app does
  this by itself too when it starts and when such a job finishes.
- **Runs:** `python studio.py runs`, `python studio.py stop <id>`.
- **Edit concepts, subject settings or marks** by editing the files; the page picks changes up within ~2 s. A subject's
  run data sits in its own folder unless config.json sets a separate `data_dir` (`studio.py state` prints both):
  - `<subjects>/<subject>/concepts.json` and `subject.json`
  - `<run data>/<subject>/results.json`: the user's Win/Lose marks, notes and edited tags
  - `<run data>/<subject>/renders.json`: every render with prompt, seed, mode, date, timing and loop metrics (read-only;
    rebuilt by refresh)
- Renders are in `<renders>/<subject output>/`: `*_00001_.mp4` (the master: 992x992, 704x1280 or 1280x704), `.gif` / `.webp` / `.share.mp4`, and `frames/<name>/`
  PNGs. For a look at a render (if you can view images), make a contact sheet from the frames with PIL (run it with
  ComfyUI's Python).

## Keep in mind

- Loop Studio's own code is in this folder (server.py, static/, skill/). Changing server.py needs a restart of the app,
  which also ends this terminal session: tell the user instead of restarting it yourself. config.json changes apply by
  themselves within a few seconds (a new agent once the user presses the terminal's Restart).
- Don't publish, push, upload or delete anything the user didn't ask for. Losses go to the Recycle Bin, never hard-deleted.
- Real people: concepts describe the gesture only and stay benign; nothing of them goes public without the user's say-so.
