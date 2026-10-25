import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopost as AP


class AlertOnlyTest(unittest.TestCase):
    def test_default_status_has_alert_slot(self):
        self.assertEqual(AP.default_status().get("alerta"), {})

    def test_pause_for_checkpoint_does_not_pause(self):
        st = AP.default_status()
        AP.pause_for_checkpoint(st)
        self.assertFalse(st.get("pausado"))
        self.assertEqual(st["alerta"].get("reason"), "cuenta restringida")
        self.assertTrue(st.get("error", "").startswith("ALERTA:"))

    def test_apply_checkpoint_alerts_without_pausing(self):
        st = AP.default_status()
        self.assertTrue(AP.apply_checkpoint(st, "https://www.facebook.com/checkpoint/1"))
        self.assertFalse(st.get("pausado"))
        self.assertEqual(st["alerta"].get("reason"), "cuenta restringida")

    def test_apply_checkpoint_ignores_normal_url(self):
        st = AP.default_status()
        self.assertFalse(AP.apply_checkpoint(st, "https://www.facebook.com/groups/1"))
        self.assertEqual(st.get("alerta"), {})
        self.assertEqual(st.get("error"), "")

    def test_alert_cooldown_keeps_first_timestamp(self):
        st = AP.default_status()
        AP.note_alert(st, "x", now=1000)
        AP.note_alert(st, "x", now=2000)
        self.assertEqual(st["alerta"].get("at"), 1000)
        AP.note_alert(st, "y", now=3000)
        self.assertEqual(st["alerta"].get("reason"), "y")


    def test_write_status_restores_alert_error(self):
        st = AP.default_status()
        st["alerta"] = {"reason": "cuenta restringida", "at": 1}
        st["error"] = ""
        seen = {}
        orig = AP.cxtlib.write_status
        AP.cxtlib.write_status = lambda path, data: seen.update({"data": data})
        try:
            AP.write_status(st)
        finally:
            AP.cxtlib.write_status = orig
        self.assertEqual(seen["data"]["error"], "ALERTA: cuenta restringida")

    def test_write_status_keeps_real_error(self):
        st = AP.default_status()
        st["alerta"] = {"reason": "cuenta restringida", "at": 1}
        st["error"] = "fallo real"
        seen = {}
        orig = AP.cxtlib.write_status
        AP.cxtlib.write_status = lambda path, data: seen.update({"data": data})
        try:
            AP.write_status(st)
        finally:
            AP.cxtlib.write_status = orig
        self.assertEqual(seen["data"]["error"], "fallo real")


if __name__ == "__main__":
    unittest.main()
