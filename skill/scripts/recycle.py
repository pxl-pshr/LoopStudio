"""Send every Loss of one subject (its MP4, GIF, WebP, share MP4 and frames) to the Recycle Bin, so it can be restored.

usage:
  python recycle.py <subject>
"""
import json
import os
import re
import subprocess
import sys
import tempfile

from loop_gifs import COMFY_OUTPUT, load_subject, subject_lock


def load(path, default):
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else default


def save(value, path):
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(value, f, indent=1, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def recycle(paths):
    # Recycle Bin, not a hard delete, so a Loss can be restored; the list goes in a file and its name in the
    # environment, so any number of paths and any characters in them (an apostrophe in a user name) work
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as f:
        f.write("\n".join(paths))
    script = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
              "Get-Content -LiteralPath $env:RECYCLE_LIST -Encoding UTF8 | ForEach-Object { "
              "if (Test-Path -LiteralPath $_ -PathType Container) { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($_, 'OnlyErrorDialogs', 'SendToRecycleBin') } "
              "else { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($_, 'OnlyErrorDialogs', 'SendToRecycleBin') } }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, env={**os.environ, "RECYCLE_LIST": f.name})
    finally:
        os.unlink(f.name)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    subject, _ = load_subject(sys.argv[1])
    with subject_lock(sys.argv[1]):
        count = recycle_losses(subject)
    if not count:
        print("no Loss files left")
        return
    print(f"recycled {count} items")
    subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "review.py"), sys.argv[1]], check=True)


def recycle_losses(subject):
    # returns how many files and folders went; only names a render can have, whatever results.json was edited to hold
    data, out_dir = subject["data"], os.path.join(COMFY_OUTPUT, subject["output"])
    results_path = os.path.join(data, "results.json")
    results = load(results_path, {})
    losers = [stem for stem, r in results.items() if r.get("v") == "lose" and re.fullmatch(r"\w[\w\-.]*", stem)]
    renders = [f[:-4] for f in os.listdir(out_dir) if f.endswith(".mp4") and not f.endswith(".share.mp4")] if os.path.isdir(out_dir) else []
    paths, gone = [], []
    for stem in losers:
        files = [os.path.join(out_dir, stem + ext) for ext in (".mp4", ".gif", ".webp", ".share.mp4")]
        # frames/<name>/ holds every render of that name (a --redo adds _00002_ next to _00001_): it goes only
        # when no render of that name is kept
        name = re.sub(r"_\d{5}_$", "", stem)
        if name and not any(re.sub(r"_\d{5}_$", "", s) == name and s not in losers for s in renders):
            files.append(os.path.join(out_dir, "frames", name))
        found = [p for p in files if os.path.exists(p)]
        paths += found
        if found:
            gone.append(stem)
    if not paths:
        return 0
    recycle(paths)
    # ComfyUI numbers the next render of that concept and seed _00001_ again: it starts unmarked, and the Loss's
    # mark and note move to recycled.json
    recycled_path = os.path.join(data, "recycled.json")
    save({**load(recycled_path, {}), **{s: results.pop(s) for s in gone}}, recycled_path)
    save(results, results_path)
    metrics_path = os.path.join(out_dir, "metrics.json")
    metrics = load(metrics_path, {})
    save({k: v for k, v in metrics.items() if k[:-4] not in gone}, metrics_path)
    return len(paths)


if __name__ == "__main__":
    main()
