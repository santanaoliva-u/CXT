#!/usr/bin/env python3
"""Regresion del bug: compartir desde un visor `photo/?fbid=` publicaba la
foto (y su atribucion) en vez del post del enlace.

Los helpers son puros: no tocan navegador, daemon ni red.
"""

import unittest

import shared


ITEM0 = ("https://www.facebook.com/photo/?fbid=122237246654329580"
         "&set=pcb.122237247752329580&__cft__[0]=xyz")
CANON0 = ("https://www.facebook.com/barbershopluxurymx/posts/"
          "pfbid0ypwgvsQzWyMUT3B1W4BBYqw7oYHQehCWrAo3Ws6xguFGMjm6iNXLoKuwd1XV6sEwl"
          "?__tn__=*F")
CLEAN0 = ("https://www.facebook.com/barbershopluxurymx/posts/"
          "pfbid0ypwgvsQzWyMUT3B1W4BBYqw7oYHQehCWrAo3Ws6xguFGMjm6iNXLoKuwd1XV6sEwl")


class TestPhotoViewer(unittest.TestCase):
    def test_detecta_visor(self):
        self.assertTrue(shared.is_photo_viewer_url(ITEM0))
        self.assertTrue(shared.is_photo_viewer_url(
            "https://www.facebook.com/photo?fbid=1&set=pcb.2"))
        self.assertFalse(shared.is_photo_viewer_url(CANON0))
        self.assertFalse(shared.is_photo_viewer_url(
            "https://www.facebook.com/groups/9/posts/222/"))

    def test_limpia_tracking(self):
        self.assertEqual(shared._clean_canonical(CANON0), CLEAN0)

    def test_permalink_php_conserva_ids(self):
        u = ("https://www.facebook.com/permalink.php?story_fbid=111&id=222"
             "&__tn__=*F&foo=bar")
        self.assertEqual(
            shared._clean_canonical(u),
            "https://www.facebook.com/permalink.php?story_fbid=111&id=222")

    def test_resuelve_visor_al_post(self):
        self.assertEqual(shared.pick_canonical_post_url(ITEM0, [CANON0]), CLEAN0)

    def test_prefiere_pcb_coincidente(self):
        url = ("https://www.facebook.com/photo/?fbid=1"
               "&set=pcb.222")
        links = ["https://www.facebook.com/groups/9/posts/111/",
                 "https://www.facebook.com/groups/9/posts/222/"]
        self.assertEqual(shared.pick_canonical_post_url(url, links),
                         "https://www.facebook.com/groups/9/posts/222")

    def test_ignora_photo_reel_watch(self):
        url = "https://www.facebook.com/photo/?fbid=1&set=pcb.2"
        links = ["https://www.facebook.com/photo/?fbid=1",
                 "https://www.facebook.com/reel/123",
                 "https://www.facebook.com/watch/?v=9"]
        self.assertIsNone(shared.pick_canonical_post_url(url, links))

    def test_sin_candidatos_es_none(self):
        self.assertIsNone(shared.pick_canonical_post_url(ITEM0, []))
        self.assertIsNone(shared.pick_canonical_post_url(ITEM0, None))

    def test_url_post_level_ya_es_canonica(self):
        self.assertEqual(shared.pick_canonical_post_url(CANON0, []), CLEAN0)

    def test_prefiere_mismo_host(self):
        url = "https://www.facebook.com/photo/?fbid=1&set=pcb.2"
        links = ["https://otro.example/posts/pfbidAAA",
                 "https://www.facebook.com/alguien/posts/pfbidBBB?__tn__=*F"]
        self.assertEqual(shared.pick_canonical_post_url(url, links),
                         "https://www.facebook.com/alguien/posts/pfbidBBB")

    def test_resolve_devuelve_url_normal_sin_navegar(self):
        self.assertEqual(shared.resolve_photo_viewer_url(None, None, CANON0),
                         CANON0)
        self.assertEqual(shared.resolve_photo_viewer_url(None, None, ""), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
