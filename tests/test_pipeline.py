import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image

from setforge.engine import NeedsInput, Pipeline
from setforge.io import inside, read, sha, write
from setforge.project import check_project
from setforge.providers import invoke
from setforge.schemas import BRIEF, validate

from fixture_worker import plan, respond, review

ROOT = Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        # Under task-owned work rather than OS temp (Windows paths may contain braces).
        test_root = ROOT / "work"
        test_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        defaults = ROOT / "src/setforge/defaults"
        self.config = read(defaults / "project.json")
        self.config["font"] = "default"  # Offline fixtures must not depend on locally installed commercial fonts.
        self.config["schema_version"] = 1  # Legacy immutable-run regression suite.
        self.config['rules']['exclude_main_cast_scenes'] = True  # Explicit cinema-like test profile.
        self.config["global_reference_ids"] = ["ref-001"]
        write(self.project / "project.json", self.config)
        (self.project / "style.md").write_text("Test style", encoding="utf-8")
        image = self.project / "ref.png"
        Image.new("RGB", (430, 480), "white").save(image)
        write(self.project / "references.json", [{"id": "ref-001", "path": "ref.png", "description": "Test", "sha256": sha(image)}])
        write(self.project / "canon.json", [])
        self.brief = {"theme": "TEST theme", "wishes": "", "required": [], "forbidden": [], "variants": 3}
        self.run = self.root / "run"

    def make(self):
        return Pipeline.create(self.project, self.brief, self.run)

    def wait(self, pipe):
        with self.assertRaises(NeedsInput):
            pipe.advance()
        return read(self.run / "jobs" / pipe.state["pending"]["id"] / "request.json")

    def deliver(self, pipe):
        request = read(self.run / "jobs" / pipe.state["pending"]["id"] / "request.json")
        pipe.accept(respond(request, self.run), "test-fixture", "not-a-real-model")

    def to_images(self, pipe):
        for _ in range(3):
            self.wait(pipe)
            self.deliver(pipe)
        return self.wait(pipe)

    def finish(self, pipe):
        for _ in range(150):
            try:
                pipe.advance()
            except NeedsInput:
                self.assertIsNotNone(pipe.state["pending"], pipe.state["reason"])
                self.deliver(pipe)
            if pipe.state["status"] == "complete":
                return
        self.fail("Pipeline did not finish")

    def test_complete_file_mode_and_portable_report(self):
        pipe = self.make()
        self.finish(pipe)
        self.assertEqual(len(pipe.state["accepted"]), 30)
        self.assertEqual(len(list((self.run / "exports").rglob("*.png"))), 30)
        self.assertEqual(read(self.run / "export-check.json")["verdict"], "pass")
        from setforge.cli import main
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["select", "--run", str(self.run), "--variant", "v2", "--note", "Test choice"]), 0)
        self.assertEqual(read(self.run / "selection.json")["variant_id"], "v2")
        self.assertEqual(len(pipe.state["canon"]), 1)
        self.assertIn("TEST FIXTURE", (self.run / "report.html").read_text(encoding="utf-8"))
        from setforge.report import export
        key = next(iter(pipe.state["accepted"]))
        (self.run / pipe.state["accepted"][key]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            export(self.run, pipe.state, pipe.config)

    def test_pending_resume_does_not_create_new_job(self):
        pipe = self.make()
        self.wait(pipe)
        again = Pipeline(self.run)
        self.wait(again)
        self.assertEqual(again.state["calls"], 1)
        self.deliver(again)
        self.assertEqual(again.state["phase"], "plan")

    def test_theme_only_brief_uses_defaults(self):
        pipe = Pipeline.create(self.project, {"theme": "Western"}, self.run)
        self.assertEqual(pipe.brief["variants"], 3)
        self.assertEqual(pipe.brief["wishes"], "")

    def test_local_entity_isolated_between_variants(self):
        pipe = self.make()
        data = plan()
        for variant in data["variants"]:
            variant["entities"][0]["scope"] = "set"
        pipe.state["plan"] = data
        v1, v2 = data["variants"][:2]
        pipe.state["canon"] = [{**v1["entities"][0], "variant_id": "v1", "version": 1,
                                "images": ["v1-only.png"]}]
        specs = pipe.entity_specs(v2, v2["cards"][2])
        self.assertNotIn("images", specs[0])

    def test_recovery_uses_completed_worker_response(self):
        pipe = self.make()
        request = self.wait(pipe)
        pending = pipe.state["pending"]
        folder = self.run / "jobs" / pending["id"]
        write(folder / "response.json", review(request))
        # Simulate the crash window after writing a result but before committing state.
        restarted = Pipeline(self.run)
        next_request = self.wait(restarted)
        self.assertEqual(next_request["kind"], "plan")
        self.assertEqual(restarted.state["calls"], 2)

    def test_export_transient_failure_rebuilds_without_redraw(self):
        from setforge.report import export as real_export
        pipe = self.make()
        attempts = []
        def once(*args):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("Transient test write failure")
            return real_export(*args)
        with patch("setforge.report.export", side_effect=once):
            self.finish(pipe)
        self.assertEqual(pipe.state["calls"], 64)
        self.assertEqual(len(attempts), 2)

    def test_invalid_variant_never_reaches_art(self):
        pipe = self.make()
        self.wait(pipe)
        self.deliver(pipe)
        self.wait(pipe)
        bad = plan()
        bad["variants"][0]["cards"][1]["id"] = "c1"
        pipe.accept(bad)
        self.assertEqual(pipe.state["phase"], "plan")
        self.assertEqual(pipe.state["jobs"][-1]["status"], "rejected")

    def test_missing_background_category_rejected(self):
        pipe = self.make()
        bad = plan()
        for card in bad["variants"][1]["cards"]:
            card["background"] = 1
        with self.assertRaisesRegex(ValueError, "background"):
            pipe.validate_plan(bad)

    def test_key_cast_scene_rejected(self):
        pipe = self.make()
        bad = plan()
        bad["variants"][0]["cards"][0]["main_cast_scene"] = True
        with self.assertRaisesRegex(ValueError, "Key-cast"):
            pipe.validate_plan(bad)

    def test_review_cannot_skip_failed_criterion(self):
        pipe = self.make()
        req = self.wait(pipe)
        bad = review(req)
        bad["criteria"][0]["verdict"] = "fail"
        with self.assertRaisesRegex(ValueError, "contradicts"):
            pipe.accept(bad)
        bad = review(req)
        bad["criteria"] = bad["criteria"][:1]
        with self.assertRaisesRegex(ValueError, "required criteria"):
            pipe.accept(bad)

    def test_uncertain_stops_and_operator_note_cannot_approve(self):
        pipe = self.make()
        req = self.wait(pipe)
        with self.assertRaises(NeedsInput):
            pipe.accept(review(req, "uncertain", "clarify"))
        self.assertEqual(pipe.state["status"], "needs_attention")
        pipe.resume_attention("Review the clarification")
        req2 = self.wait(pipe)
        self.assertEqual(req2["kind"], "brief_review")
        self.assertIn("clarification", req2["feedback"])

    def test_limits_stop_before_dispatch(self):
        self.config["limits"]["max_calls"] = 1
        write(self.project / "project.json", self.config)
        pipe = self.make()
        self.wait(pipe)
        self.deliver(pipe)
        with self.assertRaises(NeedsInput):
            pipe.advance()
        self.assertEqual(pipe.state["calls"], 1)
        pipe.resume_attention("One more call authorised", {"max_calls": 1})
        self.wait(pipe)
        self.assertEqual(pipe.state["calls"], 2)

    def test_budget_stops_before_dispatch(self):
        self.config["providers"]["reviewer"]["estimated_cost"] = 2
        self.config["limits"]["max_estimated_cost"] = 1
        write(self.project / "project.json", self.config)
        pipe = self.make()
        with self.assertRaises(NeedsInput):
            pipe.advance()
        self.assertEqual(pipe.state["calls"], 0)

    def test_attempt_cap_on_bad_plans(self):
        pipe = self.make()
        self.wait(pipe)
        self.deliver(pipe)
        for _ in range(3):
            self.wait(pipe)
            bad = plan()
            bad["variants"] = bad["variants"][:2]
            with self.assertRaises(ValueError):
                pipe.accept(bad)
            # Invalid external submissions remain pending so their syntax can be fixed.
            pipe.provider_failure(ValueError("Invalid provider output"))
        with self.assertRaises(NeedsInput):
            pipe.advance()
        self.assertIn("Attempt limit", pipe.state["reason"])

    def test_input_snapshot_and_request_tampering_detected(self):
        pipe = self.make()
        req = self.wait(pipe)
        pending = self.run / "jobs" / pipe.state["pending"]["id"] / "request.json"
        pending.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "modified"):
            pipe.accept(review(req))
        (self.run / "inputs/style.md").write_text("Changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            pipe.check_snapshot()

    def test_path_escape_and_changed_reference_rejected(self):
        with self.assertRaises(ValueError):
            inside(self.run, "../secret")
        (self.project / "ref.png").write_bytes(b"bad")
        with self.assertRaisesRegex(ValueError, "Reference changed"):
            check_project(self.project)

    def test_targeted_card_revision_keeps_original_refs(self):
        pipe = self.make()
        self.to_images(pipe)
        self.deliver(pipe)
        req = self.wait(pipe)
        pipe.accept(review(req, "fail", "revise"))
        revision = self.wait(pipe)
        self.assertEqual(revision["kind"], "image")
        self.assertEqual(revision["images"][0]["role"], "edit_target:previous_attempt")
        self.assertTrue(any(i["role"].startswith("original_style_reference") for i in revision["images"]))
        self.assertEqual(len(pipe.state["accepted"]), 0)

    def test_set_revision_preserves_other_accepted_cards(self):
        pipe = self.make()
        request = self.wait(pipe)
        while request["kind"] != "set_review":
            self.deliver(pipe)
            request = self.wait(pipe)
        old = copy.deepcopy(pipe.state["accepted"])
        pipe.accept(review(request, "fail", "revise", ["v2/c3"]))
        request = self.wait(pipe)
        self.assertEqual(request["kind"], "image")
        self.assertEqual(pipe.state["current"], "v2/c3")
        self.assertEqual(len(pipe.state["accepted"]), 29)
        for key in pipe.state["accepted"]:
            self.assertEqual(old[key], pipe.state["accepted"][key])

    def test_chat_adapter_sends_images_and_parses_json(self):
        import io
        from setforge.providers import invoke
        pipe = self.make()
        self.wait(pipe)
        self.deliver(pipe)
        request = self.wait(pipe)
        job = self.run / "jobs" / pipe.state["pending"]["id"]
        reply = {"choices": [{"message": {"content": json.dumps(plan())}}], "usage": {"total_tokens": 42}}
        config = {"type": "chat-completions", "base_url": "https://example.invalid/v1",
                  "model": "test-model", "require_key": False}
        with patch("setforge.providers.urlopen", return_value=io.BytesIO(json.dumps(reply).encode())) as api:
            result = invoke(config, request, self.run, job, 10)
        sent = json.loads(api.call_args.args[0].data)
        self.assertEqual(result, plan())
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertTrue(sent["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(read(job / "usage.json")["total_tokens"], 42)

    def test_provider_image_limit_is_not_silently_truncated(self):
        pipe = self.make()
        request = self.to_images(pipe)
        job = self.run / "jobs" / pipe.state["pending"]["id"]
        with self.assertRaisesRegex(ValueError, "image limit"):
            invoke({"type": "command", "max_images": 0}, request, self.run, job, 1)

    def test_api_refusal_and_empty_responses_are_recordable_errors(self):
        import io
        pipe = self.make()
        request = self.wait(pipe)
        job = self.run / "jobs" / pipe.state["pending"]["id"]
        config = {"type": "chat-completions", "base_url": "https://example.invalid/v1",
                  "model": "test-model", "require_key": False}
        replies = [{"choices": []}, {"choices": [{"message": {"refusal": "refused"}}]},
                   {"choices": [{"message": {"content": None}}]}]
        for reply in replies:
            with patch("setforge.providers.urlopen", return_value=io.BytesIO(json.dumps(reply).encode())):
                with self.assertRaises(ValueError):
                    invoke(config, request, self.run, job, 1)

    def test_command_adapter_complete_without_codex(self):
        config = {"type": "command", "argv": [sys.executable, str(ROOT / "tests/fixture_worker.py"),
                  "--request", "{request}", "--response", "{response}"], "estimated_cost": 0, "zero_cost": True}
        self.config["providers"] = {k: copy.deepcopy(config) for k in ("planner", "reviewer", "image")}
        write(self.project / "project.json", self.config)
        pipe = self.make()
        pipe.advance()
        self.assertEqual(pipe.state["status"], "complete")
        self.assertEqual(len(pipe.state["accepted"]), 30)
        self.assertEqual(pipe.state["calls"], 64)

    def test_cli_exit_codes_and_job_identity(self):
        brief = self.root / "brief.json"
        write(brief, self.brief)
        cmd = [sys.executable, "-m", "setforge"]
        result = subprocess.run(cmd + ["run", "--project", str(self.project), "--brief", str(brief), "--run", str(self.run)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 3, result.stderr)
        response = self.root / "response.json"
        write(response, {})
        result = subprocess.run(cmd + ["submit", "--run", str(self.run), "--job", "wrong", "--response", str(response), "--executor", "test"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not match", result.stderr)


if __name__ == "__main__":
    unittest.main()
