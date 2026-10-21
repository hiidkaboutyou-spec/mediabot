"""Opt-in live extraction/download test for an openly available sample video.

Run with: RUN_LIVE_MEDIA_TESTS=1 python -m unittest tests.test_live_media
"""

from __future__ import annotations

import os
import tempfile
import unittest

import bot


@unittest.skipUnless(os.getenv("RUN_LIVE_MEDIA_TESTS") == "1", "set RUN_LIVE_MEDIA_TESTS=1 to access the public sample host")
class LiveMediaDownloadTests(unittest.TestCase):
    url = "https://filesamples.com/samples/video/mp4/sample_640x360.mp4"
    playlist_url = "https://peertube.tv/w/p/qdvEdhG6aSJ1Fzz36g3xZ7"

    def test_probe_and_download_public_sample(self) -> None:
        info, options = bot.list_formats(self.url)
        self.assertEqual(info.get("ext"), "mp4")
        self.assertIn("best", [option["id"] for option in options])
        with tempfile.TemporaryDirectory(prefix="mediabot-test-") as target:
            path, downloaded = bot.download_media(self.url, "best", target)
            self.assertTrue(path.is_file())
            self.assertGreater(path.stat().st_size, 10_000)
            self.assertEqual(path.read_bytes()[4:8], b"ftyp")
            self.assertEqual(downloaded.get("title"), info.get("title"))

    def test_probe_and_download_public_playlist(self) -> None:
        info, options = bot.list_formats(self.playlist_url)
        entries = [entry for entry in info.get("entries", []) if entry]
        self.assertEqual(len(entries), 2)
        quality = next(option["id"] for option in options if option["id"].startswith("height:"))
        with tempfile.TemporaryDirectory(prefix="mediabot-live-playlist-") as target:
            results = bot.download_media_items(self.playlist_url, quality, target)
            self.assertEqual(len(results), 2)
            self.assertEqual([entry.get("id") for _, entry in results], [entry.get("id") for entry in entries])
            for path, _ in results:
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 10_000)
                self.assertEqual(path.read_bytes()[4:8], b"ftyp")


if __name__ == "__main__":
    unittest.main()
