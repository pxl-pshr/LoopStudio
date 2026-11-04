"""Checks of the render script that need no ComfyUI, on the bundled example subject.

usage (from the Loop Studio folder): .venv\\Scripts\\python.exe -m unittest discover tests
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "skill", "scripts"))
import loop_gifs  # noqa: E402
import needs  # noqa: E402
import review  # noqa: E402
import studio  # noqa: E402


class RenderTest(unittest.TestCase):
    def setUp(self):
        # the example subject from the repo, with its run data and renders in a temp folder
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = tmp.name
        for p in (mock.patch.dict(loop_gifs.CFG, subjects_dir=os.path.join(ROOT, "skill", "subjects"), data_dir=tmp.name),
                  mock.patch.object(loop_gifs, "COMFY_OUTPUT", tmp.name)):
            p.start()
            self.addCleanup(p.stop)
        self.subject, self.concepts = loop_gifs.load_subject("example")
        self.name = next(iter(self.concepts))

    def dry(self, *args):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["loop_gifs.py", "example", "render", self.name, "--dry", *args]), \
                contextlib.redirect_stdout(out):
            loop_gifs.main()
        return out.getvalue()

    def make_render(self, seed, model="fast"):
        frames = os.path.join(self.out, self.subject["output"], "frames", loop_gifs.out_name(self.name, "_square", model, seed))
        os.makedirs(frames)
        open(os.path.join(frames, "f_00001_.png"), "w").close()

    def test_dry_run_prints_each_prompt(self):
        out = self.dry("--seed=1000", "--seed=2024")
        self.assertEqual(out.count(f"--- {self.name}_square_102f"), 2)
        self.assertIn(loop_gifs.full_prompt(self.subject, self.concepts[self.name]), out)

    def test_first_render_gets_seed_1000(self):
        self.assertIn(f"--- {self.name}_square_102f_fast (seed 1000,", self.dry("--fast"))

    def test_new_seed_is_one_not_rendered(self):
        self.make_render(1000)
        out = self.dry("--fast")
        self.assertNotIn("(seed 1000,", out)
        seed = int(out.split("(seed ", 1)[1].split(",", 1)[0])
        self.assertIsNone(loop_gifs.rendered(self.subject, loop_gifs.out_name(self.name, "_square", "fast", seed)))

    def test_rendered_is_per_mode(self):
        self.make_render(1000, model="fast")
        self.assertEqual(loop_gifs.new_seed(self.subject, self.name, "_square", "stock"), 1000)


    def lora_chain(self, model, concept=None, subject=None):
        # the LoRA files on the loop model, in order, for a concept rendered in this mode
        lora = loop_gifs.loras(model, subject or {}, concept or {})
        g = loop_gifs.graph("p", 1000, "x/y", 992, 992, None, model, None, lora)
        chain, src = [], g["3"]["inputs"]["model"]
        while g[src[0]]["class_type"] == "LoraLoaderModelOnly":
            chain.insert(0, g[src[0]]["inputs"]["lora_name"])
            src = g[src[0]]["inputs"]["model"]
        return chain, g

    def test_default_render_stacks_pika_then_vbvr(self):
        chain, g = self.lora_chain("stock")
        self.assertEqual(chain, [loop_gifs.PIKA_LORA, needs.VBVR_LORA])
        self.assertEqual(g["11"]["inputs"]["sampler_name"], "res_multistep")  # the stock sampler, not Pika's lcm
        self.assertEqual(g["12"]["class_type"], "BasicScheduler")
        self.assertEqual(g["12"]["inputs"]["steps"], 25)

    def test_own_lora_replaces_the_default(self):
        self.assertEqual(self.lora_chain("stock", {"lora": []})[0], [])
        self.assertEqual(self.lora_chain("stock", subject={"lora": {"file": "a.safetensors"}})[0], ["a.safetensors"])
        self.assertEqual(self.lora_chain("fast")[0], [])

    def test_formats_render_at_their_own_size(self):
        self.assertIn(f"--- {self.name}_vertical_102f_fast (seed 1000,", self.dry("--fast", "--vertical"))
        self.assertIn(f"--- {self.name}_wide_102f_fast (seed 1000,", self.dry("--fast", "--wide"))
        for fmt, (w, h) in {"square": (992, 992), "vertical": (704, 1280), "wide": (1280, 704)}.items():
            _, gw, gh = loop_gifs.FORMATS[fmt]
            self.assertEqual((gw, gh), (w, h))
            self.assertEqual((gw % 32, gh % 32), (0, 0))  # H3 needs sides in multiples of 32

    def test_downloads_fetch_the_files_renders_use(self):
        # Settings > Requirements' Download buttons must save exactly the names the render graph asks ComfyUI for
        uses = {"model": needs.MODELS["stock"][0], "clip": needs.CLIP, "vae_v": needs.VAE_V, "vae_a": needs.VAE_A,
                "pika": needs.PIKA_LORA, "vbvr": needs.VBVR_LORA, "fast": needs.MODELS["fast"][0], "upscale_model": needs.UPSCALER}
        self.assertEqual(set(uses), set(needs.DOWNLOADS))
        for key, file in uses.items():
            self.assertEqual(needs._norm(needs.DOWNLOADS[key][1]), needs._norm(file), key)
            self.assertTrue(needs.download_url(key).startswith("https://huggingface.co/"), key)

    def test_concept_comes_from_before_the_format(self):
        concepts = {"wink": {"tags": ["a"]}, "wink_wide": {"tags": ["b"]}}
        self.assertEqual(loop_gifs.concept_of("wink_wide_102f_s2024_00001_", concepts), "wink")
        self.assertEqual(loop_gifs.concept_of("wink_wide_square_102f_00001_", concepts), "wink_wide")
        self.assertEqual(loop_gifs.concept_of("wink_vertical_102f_fast_", concepts), "wink")
        self.assertIsNone(loop_gifs.concept_of("gone_square_102f_00001_", concepts))
        self.assertEqual(loop_gifs.concept_of("wink_102f_00001_", concepts), "wink")  # a name from before formats

    def test_concept_without_tags_still_has_tags(self):
        self.assertEqual(loop_gifs.tags_for("wink_square_102f_", {"base_tags": ["x"]}, {"wink": {}}), ["x"])
        self.assertEqual(loop_gifs.tags_for("wink_square_102f_", {}, {"wink": {"tags": ["y"]}}), ["y"])

    def test_each_mode_only_needs_its_own_model(self):
        def item(key, need):
            return {"key": key, "name": key, "ok": False, "need": need}
        items = [item("model", "default renders"), item("pika", "default renders"), item("vbvr", "default renders"), item("fast", "Fast renders")]
        keys = lambda **kw: [i["key"] for i in needs.blocking(items, gif=False, **kw)]
        self.assertEqual(keys(fast=True), ["fast"])
        self.assertEqual(keys(stack=True), ["model", "pika", "vbvr"])
        self.assertEqual(keys(pika=True), ["model", "pika"])
        self.assertEqual(keys(), ["model"])  # "lora": [] on the stock model
        by_key = {m["key"]: m["missing"] for m in needs.modes(items)}
        self.assertEqual(by_key["fast"], ["fast"])
        self.assertEqual(by_key["stock"], ["model", "pika", "vbvr"])

    def test_a_job_comfyui_forgot_leaves_the_grid(self):
        answers = [b'{}', b'{"queue_running": [], "queue_pending": []}']
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: io.BytesIO(answers.pop(0))):
            self.assertEqual(review.job_info("abc", {"abc": {"status": "rendering"}})["status"], "error")

    def test_a_finished_job_comfyui_forgot_stays(self):
        answers = [b'{}', b'{"queue_running": [], "queue_pending": []}']
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: io.BytesIO(answers.pop(0))):
            self.assertEqual(review.job_info("abc", {"abc": {"status": "success", "mp4": "x.mp4"}})["status"], "success")

    def test_concepts_that_cant_make_a_prompt_are_named(self):
        self.assertIsNone(needs.concept_problem({"visual": "a cat"}))
        self.assertIsNone(needs.concept_problem({"template": "studio", "action": "waves"}))
        self.assertIsNone(needs.concept_problem({"template": "scene", "setting": "a room", "action": "waves"}))
        self.assertIn("Action", needs.concept_problem({"template": "studio"}))
        self.assertIn("Action", needs.concept_problem({"template": "studio", "action": "  "}))
        self.assertIn("Setting", needs.concept_problem({"template": "scene", "action": "waves"}))
        self.assertIn("Visual", needs.concept_problem({"visual": ""}))
        self.assertIn("template", needs.concept_problem({"template": "nope"}))
        for c in self.concepts.values():  # the bundled example's are all fine
            self.assertIsNone(needs.concept_problem(c), c["name"])

    def test_a_render_with_an_empty_concept_stops_before_it_starts(self):
        with mock.patch.object(sys, "argv", ["loop_gifs.py", "example", "render", "broken", "--dry"]), \
                mock.patch.object(loop_gifs, "load_subject", return_value=(self.subject, {"broken": {"name": "broken", "template": "studio"}})), \
                self.assertRaises(SystemExit) as stop:
            loop_gifs.main()
        self.assertIn("broken: its Action is empty", str(stop.exception))

    def test_a_job_being_made_is_left_alone(self):
        with loop_gifs.job_lock("t") as held:
            self.assertTrue(held)
            with loop_gifs.job_lock("t", wait=False) as free:
                self.assertFalse(free)
        with loop_gifs.job_lock("t", wait=False) as free:
            self.assertTrue(free)

    def test_following_a_run_keeps_printing_past_200_lines(self):
        lines = [f"line {i}" for i in range(450)]

        def poll(total, status="running"):
            return [{"id": "r", "status": status, "log_tail": lines[:total][-200:], "log_total": total, "progress": None, "started": 0, "ended": 0}]
        answers = [poll(150), poll(300), poll(450, "done")]
        out = io.StringIO()
        with mock.patch.object(studio, "call", side_effect=lambda *a: answers.pop(0)), mock.patch.object(studio.time, "sleep"), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            studio.follow("r")
        printed = [ln.strip() for ln in out.getvalue().splitlines() if ln.strip().startswith("line ")]
        self.assertEqual(printed, lines)


if __name__ == "__main__":
    unittest.main()
