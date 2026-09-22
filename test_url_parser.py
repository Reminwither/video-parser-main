import unittest

from utils.web_fetcher import UrlParser


class UrlParserTests(unittest.TestCase):
    def test_chat_punctuation_is_not_part_of_url(self):
        self.assertEqual(
            UrlParser.get_url("帮我分析 https://v.douyin.com/fc3BqC8sz0Y/)。"),
            "https://v.douyin.com/fc3BqC8sz0Y/",
        )

    def test_youtube_watch_url_preserves_video_id(self):
        url = "https://www.youtube.com/watch?v=jNQXAC9IVRw&feature=shared"
        self.assertEqual(UrlParser.get_video_id(url), "jNQXAC9IVRw")
        self.assertEqual(
            UrlParser.extract_video_address(url),
            "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )

    def test_youtu_be_url_uses_path_video_id(self):
        url = "https://youtu.be/jNQXAC9IVRw?t=3"
        self.assertEqual(UrlParser.get_video_id(url), "jNQXAC9IVRw")
        self.assertEqual(UrlParser.extract_video_address(url), "https://youtu.be/jNQXAC9IVRw")


if __name__ == "__main__":
    unittest.main()
