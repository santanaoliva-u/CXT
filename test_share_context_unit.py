#!/usr/bin/env python3
"""Test unitario del guardia de contexto de compartir (sin navegador)."""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

os.environ["CXT_DIR"] = tempfile.mkdtemp(prefix="share_ctx_unit_")
os.environ.pop("CXT_FAST", None)

import shared as SH  # noqa: E402


class ShareContextTest(unittest.TestCase):
    def test_normal_composer_rejected(self):
        self.assertEqual(SH.classify_share_context(
            {"link": False, "needle": False, "articles": 0}),
            (False, "composer-sin-post"))

    def test_link_accepted(self):
        self.assertEqual(SH.classify_share_context({"link": True, "needle": False}),
                         (True, "ok"))

    def test_needle_accepted(self):
        self.assertEqual(SH.classify_share_context({"link": False, "needle": True}),
                         (True, "ok"))

    def test_missing_evidence_rejected(self):
        self.assertEqual(SH.classify_share_context(None),
                         (False, "composer-sin-post"))

    def test_non_dict_rejected(self):
        self.assertEqual(SH.classify_share_context("texto"),
                         (False, "composer-sin-post"))

    def test_picker_uses_share_now(self):
        self.assertIn("compartir ahora", SH.JS_PICKER_SHARE)
        self.assertIn("share now", SH.JS_PICKER_SHARE)

    def test_share_point_accepts_clickable_point(self):
        self.assertTrue(SH.share_point_ok(
            {"x": 563, "y": 316, "scope": "dialog"}))

    def test_share_point_rejects_abort_reason(self):
        self.assertFalse(SH.share_point_ok(
            {"reason": "sin-share-en-modal", "scope": "dialog"}))

    def test_find_share_stays_inside_modal_and_waits_reflow(self):
        js = SH.js_find_share("barbershop luxury")
        self.assertIn("sin-share-en-modal", js)
        self.assertIn("post-no-coincide", js)
        self.assertIn("elementFromPoint", js)
        self.assertIn("await new Promise", js)
        self.assertIn("scrollIntoView", js)
        self.assertIn("matching.find", js)

    def test_nonpost_close_keeps_all_post_modals(self):
        js = SH.JS_TOP_NONPOST_CLOSE
        self.assertIn("const posts=", js)
        self.assertIn("!posts.includes(d)", js)
        self.assertNotIn("d!==post", js)
        self.assertIn("others", js)


    def test_nonpost_close_accepts_covered_close_control(self):
        js = SH.JS_TOP_NONPOST_CLOSE
        self.assertIn("elementFromPoint", js)
        self.assertIn("re.test(lab)", js)
        self.assertIn("t.closest", js)

    def test_nonpost_close_checks_owner_and_button_text(self):
        js = SH.JS_TOP_NONPOST_CLOSE
        self.assertIn("re.test(tx)", js)
        self.assertIn("others.find(d=>d.contains(top))", js)
        self.assertIn("div[role=button],button", js)

    def test_close_share_dialog_handles_discard(self):
        import inspect
        src = inspect.getsource(SH.close_share_dialog)
        self.assertIn("JS_DISCARD", src)
        self.assertIn("isinstance(d, dict)", src)

    def test_nonpost_count_classifies_only_nonpost(self):
        js = SH.JS_NONPOST_COUNT
        self.assertIn("const posts=", js)
        self.assertIn("!posts.includes(d)", js)
        self.assertIn("others.length", js)


if __name__ == "__main__":
    unittest.main()
