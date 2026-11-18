"""Regression gates derived from actual quality-lab failures; fixtures are NOT art."""
import copy
import unittest
from PIL import Image, PngImagePlugin
from setforge.engine import Pipeline
from setforge.io import read, write
from setforge.quality import dependencies, pixel_digest
from setforge.project import check_project
from fixture_worker import plan, review
import test_pipeline as legacy


class QualityTests(unittest.TestCase):
    def setUp(self):
        legacy.PipelineTests.setUp(self)
        self.config["schema_version"] = 2
        write(self.project / "project.json", self.config)

    make = legacy.PipelineTests.make
    wait = legacy.PipelineTests.wait
    deliver = legacy.PipelineTests.deliver
    finish = legacy.PipelineTests.finish

    def test_complete_v2_files_contract(self):
        pipe = self.make()
        self.finish(pipe)
        self.assertEqual(pipe.state["status"], "complete")
        self.assertTrue(list((self.run / "previews").glob("*.png")))

    def test_duplicate_subject_ids_and_palette_only_variants(self):
        pipe = self.make()
        for bad in ("subject", "variant", "generic"):
            data = plan(2)
            if bad == "subject":
                data["variants"][0]["cards"][1]["concept"]["canonical_subject"] = data["variants"][0]["cards"][0]["concept"]["canonical_subject"]
            elif bad == "variant":
                for a, b in zip(data["variants"][0]["cards"], data["variants"][1]["cards"]):
                    b["concept"]["canonical_subject"] = a["concept"]["canonical_subject"]
                    b["palette"] = "Different palette"
            else:
                for c in data["variants"][0]["cards"]:
                    c["materials"] = "Generic glossy material"
                    c["composition"] = "Generic centered object"
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pipe.validate_plan(data)

    def test_shared_material_alone_is_not_a_variety_quota(self):
        pipe = self.make()
        data = plan(2)
        for c in data["variants"][0]["cards"]:
            c["materials"] = "Satin metal"
        pipe.validate_plan(data)

    def test_unknown_review_criterion_and_wrong_canvas_rejected(self):
        pipe = self.make()
        answer = review(self.wait(pipe))
        answer["criteria"].append({"name": "invented_rarity", "verdict": "pass", "evidence": "TEST"})
        with self.assertRaisesRegex(ValueError, "unknown criteria"):
            pipe.validate_review(answer, "brief_review")
        square = self.root / "square.png"
        Image.new("RGB", (512, 512), "orange").save(square)
        pipe.state["current"] = "v1/c1"
        with self.assertRaisesRegex(ValueError, "aspect"):
            pipe.import_image({"image_path": str(square), "note": "TEST fixture"}, self.run)

    def test_main_source_precedes_background_and_cycles_fail(self):
        data = plan(2)
        v = data["variants"][0]
        v["entities"][0]["scope"] = "set"
        v["entities"][0]["source_card_id"] = "v1/c9"
        v["cards"][0]["entity_ids"] = ["test-rover"]
        v["cards"][8]["entity_ids"] = ["test-rover"]
        graph = dependencies(data, [])
        self.assertEqual(graph["v1/c1"], {"v1/c9"})
        v["entities"].append({**v["entities"][0], "id": "second", "source_card_id": "v1/c1"})
        v["cards"][0]["entity_ids"].append("second")
        v["cards"][8]["entity_ids"].append("second")
        with self.assertRaisesRegex(ValueError, "Cyclic"):
            dependencies(data, [])

    def test_review_cannot_pass_blockers_or_fail_without_fix(self):
        pipe = self.make()
        request = self.wait(pipe)
        answer = review(request)
        answer["findings"] = [{"code": "conflict", "criterion": "clarity", "severity": "blocking",
                                "targets": ["brief"], "evidence": "TEST conflict", "fix": "TEST fix"}]
        with self.assertRaises(ValueError):
            pipe.validate_review(answer, "brief_review")
        answer["findings"] = []
        answer["verdict"] = "fail"
        answer["criteria"][0]["verdict"] = "fail"
        answer["action"] = "clarify"
        with self.assertRaisesRegex(ValueError, "actionable"):
            pipe.validate_review(answer, "brief_review")

    def test_pixel_duplicate_survives_png_metadata_change(self):
        a, b = self.root / "a.png", self.root / "b.png"
        # Labelled infrastructure fixture only; not evaluated as generated art.
        im = Image.new("RGB", (512, 576), "orange")
        im.save(a)
        meta = PngImagePlugin.PngInfo()
        meta.add_text("description", "changed metadata")
        im.save(b, pnginfo=meta)
        self.assertEqual(pixel_digest(a), pixel_digest(b))
        pipe = self.make()
        pipe.state["current"] = "v1/c1"
        dest = self.run / "old.png"
        dest.write_bytes(a.read_bytes())
        pipe.state["latest"]["v1/c1"] = {"path": "old.png"}
        job = self.run / "job"
        job.mkdir()
        with self.assertRaisesRegex(ValueError, "Duplicate pixels"):
            pipe.import_image({"image_path": str(b), "note": "TEST"}, job)

    def test_zero_cash_and_text_only_disclosure(self):
        self.config["providers"]["image"] = {"type": "command", "argv": ["fake"], "estimated_cost": 0}
        write(self.project / "project.json", self.config)
        with self.assertRaisesRegex(ValueError, "explicitly free"):
            check_project(self.project)
        self.config["providers"]["image"].update(zero_cost=True, text_only_draft=True, supports_images=False)
        write(self.project / "project.json", self.config)
        pipe = self.make()
        pipe.config['providers']['image']['type'] = 'files'  # No fake executable is dispatched.
        legacy.PipelineTests.to_images(self, pipe)  # Includes the real pre-art allocation gate.
        request, _ = pipe.prepare()
        self.assertEqual(request["images"], [])
        self.assertEqual(request["conditioning"]["reference_images_supplied"], 0)
        pipe.state["current"] = "v1/c3"
        pipe.state["canon"] = [{**plan(2)["variants"][0]["entities"][0], "version": 1, "images": ["canon.png"]}]
        with self.assertRaisesRegex(ValueError, "cannot preserve"):
            pipe.prepare()
