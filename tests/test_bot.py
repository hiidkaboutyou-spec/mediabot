from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path
from unittest.mock import patch

import bot


class UrlValidationTests(unittest.TestCase):
    def test_rejects_non_http_schemes(self) -> None:
        with self.assertRaisesRegex(ValueError, "http"):
            bot.validate_url("file:///etc/passwd")

    def test_rejects_loopback_address(self) -> None:
        with self.assertRaisesRegex(ValueError, "خصوصی"):
            bot.validate_url("http://127.0.0.1/video")

    def test_accepts_public_ip_without_dns(self) -> None:
        self.assertEqual(bot.validate_url("https://8.8.8.8/video"), "https://8.8.8.8/video")

    @patch("bot.socket.getaddrinfo")
    def test_rejects_public_hostname_resolving_to_private_ip(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [(None, None, None, None, ("10.0.0.8", 80))]
        with self.assertRaisesRegex(ValueError, "خصوصی"):
            bot.validate_url("http://media.example/video")
        getaddrinfo.assert_called_once_with("media.example", 80, type=bot.socket.SOCK_STREAM)


class MediaMetadataTests(unittest.TestCase):
    def test_caption_stays_within_telegram_limit(self) -> None:
        caption = bot.make_caption({"title": "T" * 2000, "description": "D" * 2000})
        self.assertLessEqual(len(caption), 1024)

    def test_format_menu_is_sorted_and_compact(self) -> None:
        info = {
            "title": "video",
            "formats": [
                {"height": height, "vcodec": "avc1", "format_id": str(height)}
                for height in [360, 1080, 720, 1440, 2160, 480, 240, 144]
            ] + [{"height": 2160, "vcodec": "none", "format_id": "audio-only"}],
        }
        with patch("bot.yt_dlp.YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.return_value = info
            result, formats = bot.list_formats("https://example.com/video")
        self.assertEqual(result["title"], "video")
        self.assertEqual([item["id"] for item in formats[:6]], ["height:2160", "height:1440", "height:1080", "height:720", "height:480", "height:360"])
        self.assertEqual(formats[-1]["id"], "caption")

    def test_playlist_keeps_playlist_metadata_and_uses_first_entry_formats(self) -> None:
        playlist = {
            "_type": "playlist",
            "title": "sample list",
            "entries": [{"title": "first", "formats": [{"height": 720, "vcodec": "avc1"}]}, None],
        }
        with patch("bot.yt_dlp.YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.return_value = playlist
            result, formats = bot.list_formats("https://example.com/playlist")
        self.assertEqual(result["title"], "sample list")
        self.assertEqual(result["_type"], "playlist")
        self.assertEqual(formats[0]["id"], "height:720")


class PlaylistDownloadTests(unittest.TestCase):
    def test_downloads_and_returns_playlist_items_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as target:
            entries = [{"title": "first"}, {"title": "second"}]
            with patch("bot.yt_dlp.YoutubeDL") as ydl:
                instance = ydl.return_value.__enter__.return_value

                def extract(url, download):
                    (Path(target) / "0002-second-id2.mp4").write_bytes(b"second")
                    (Path(target) / "0001-first-id1.mp4").write_bytes(b"first")
                    return {"_type": "playlist", "entries": entries}

                instance.extract_info.side_effect = extract
                with patch("bot.MAX_UPLOAD_MB", 1):
                    results = bot.download_media_items("https://example.com/list", "best", target)
                options = ydl.call_args.args[0]
            self.assertFalse(options["noplaylist"])
            self.assertEqual(options["playlistend"], bot.MAX_PLAYLIST_ITEMS)
            self.assertEqual([path.name for path, _ in results], ["0001-first-id1.mp4", "0002-second-id2.mp4"])
            self.assertEqual([info["title"] for _, info in results], ["first", "second"])

    def test_playlist_items_above_upload_cap_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as target, patch("bot.MAX_UPLOAD_MB", 1):
            with patch("bot.yt_dlp.YoutubeDL") as ydl:
                instance = ydl.return_value.__enter__.return_value

                def extract(url, download):
                    (Path(target) / "0001-large-id1.mp4").write_bytes(b"x" * (1024 * 1024 + 1))
                    (Path(target) / "0002-small-id2.mp4").write_bytes(b"x")
                    return {"_type": "playlist", "entries": [{"id": "id1", "title": "large"}, {"id": "id2", "title": "small"}]}

                instance.extract_info.side_effect = extract
                results = bot.download_media_items("https://example.com/list", "best", target)
            self.assertEqual([path.name for path, _ in results], ["0002-small-id2.mp4"])
            self.assertEqual(results[0][1]["title"], "small")

    def test_duration_filter_rejects_over_limit(self) -> None:
        duration_filter = bot.ydl_base()["match_filter"]
        self.assertIsNone(duration_filter({"duration": 30}, False))
        self.assertIsNotNone(duration_filter({"duration": bot.MAX_DURATION_SECONDS + 1}, False))


class JobLifecycleTests(unittest.TestCase):
    def tearDown(self) -> None:
        bot.JOBS.clear()

    def test_prune_removes_expired_jobs(self) -> None:
        bot.JOBS["expired"] = bot.Job("u", "old", "", [], 1, time.time() - bot.JOB_TTL_SECONDS - 1)
        bot.JOBS["fresh"] = bot.Job("u", "new", "", [], 1, time.time())
        bot.prune_jobs()
        self.assertEqual(set(bot.JOBS), {"fresh"})

    def test_job_count_is_capped(self) -> None:
        with patch("bot.MAX_JOBS", 2):
            for name in ("first", "second"):
                bot.JOBS[name] = bot.Job("u", name, "", [], 1, time.time())
            bot.prune_jobs()
            self.assertEqual(len(bot.JOBS), 2)
            bot.JOBS["third"] = bot.Job("u", "third", "", [], 1, time.time() + 1)
            bot.prune_jobs()
            self.assertEqual(set(bot.JOBS), {"second", "third"})


class TelegramAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_another_user_cannot_use_download_callback(self) -> None:
        bot.JOBS.clear()
        bot.JOBS["job123"] = bot.Job("https://example.com/media", "title", "", [{"id": "best"}], 100, time.time())
        message = SimpleNamespace(reply_text=AsyncMock())
        query = SimpleNamespace(
            data="dl|job123|best",
            answer=AsyncMock(),
            message=message,
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=200))

        await bot.callback(update, None)

        message.reply_text.assert_awaited_once_with("این انتخاب برای کاربر دیگری است.")
        query.edit_message_text.assert_not_awaited()
        bot.JOBS.clear()

    async def test_playlist_callback_sends_every_file_in_order(self) -> None:
        bot.JOBS.clear()
        with tempfile.TemporaryDirectory() as target:
            first = Path(target) / "0001-first.mp4"
            second = Path(target) / "0002-second.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            bot.JOBS["playlist1"] = bot.Job(
                "https://example.com/playlist", "playlist", "", [{"id": "best"}], 100,
                time.time(), is_playlist=True, item_count=2,
            )
            message = SimpleNamespace(
                reply_text=AsyncMock(),
                reply_document=AsyncMock(),
                reply_audio=AsyncMock(),
            )
            query = SimpleNamespace(
                data="dl|playlist1|best",
                answer=AsyncMock(),
                message=message,
                edit_message_text=AsyncMock(),
                delete_message=AsyncMock(),
            )
            update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=100))
            results = [(first, {"title": "first"}), (second, {"title": "second"})]

            with patch("bot.download_media_items", return_value=results):
                await bot.callback(update, None)

            self.assertEqual(message.reply_document.await_count, 2)
            self.assertEqual(
                [call.kwargs["filename"] for call in message.reply_document.await_args_list],
                [first.name, second.name],
            )
            query.delete_message.assert_awaited_once()
        bot.JOBS.clear()


class SpotifyCapTests(unittest.TestCase):
    def test_track_cap_is_applied_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as target, patch("bot.MAX_PLAYLIST_ITEMS", 2):
            observed_count: list[int] = []

            def fake_run(command, **kwargs):
                if command[1] == "save":
                    catalog = Path(command[command.index("--save-file") + 1])
                    catalog.write_text(json.dumps([{"id": str(i)} for i in range(5)]), encoding="utf-8")
                else:
                    catalog = Path(command[2])
                    observed_count.append(len(json.loads(catalog.read_text(encoding="utf-8"))))
                    (Path(target) / "track.mp3").write_bytes(b"audio")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with patch("bot.subprocess.run", side_effect=fake_run):
                files = bot.download_spotify("https://open.spotify.com/playlist/example", target)

            self.assertEqual(observed_count, [2])
            self.assertEqual([path.name for path in files], ["track.mp3"])


class DownloadUnitTests(unittest.TestCase):
    def test_rejects_unknown_format(self) -> None:
        with tempfile.TemporaryDirectory() as target:
            with self.assertRaisesRegex(ValueError, "کیفیت"):
                bot.download_media("https://example.com/video", "invalid", target)

    def test_enforces_upload_limit_after_download(self) -> None:
        with tempfile.TemporaryDirectory() as target, patch("bot.MAX_UPLOAD_MB", 1):
            context = patch("bot.yt_dlp.YoutubeDL")
            mock_ydl = context.start()
            self.addCleanup(context.stop)
            (Path(target) / "too-large.mp4").write_bytes(b"x" * (1024 * 1024 + 1))
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = {"title": "video"}
            with self.assertRaisesRegex(RuntimeError, "سقف 1 مگابایت"):
                bot.download_media("https://example.com/video", "best", target)


if __name__ == "__main__":
    unittest.main()
