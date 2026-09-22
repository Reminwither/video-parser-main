import unittest

from viral_distillation import normalize_platform_evidence


class ViralDistillationTests(unittest.TestCase):
    def test_ratios_are_named_by_their_real_denominator(self):
        result = normalize_platform_evidence({
            "platform": "抖音",
            "video_id": "123",
            "statistics": {
                "play_count": 1000,
                "digg_count": 100,
                "comment_count": 25,
                "collect_count": 40,
                "share_count": 10,
            },
            "author": {"nickname": "作者", "follower_count": 200},
        }, url="https://example.com/video/123", duration_seconds=12.5)
        performance = result["performance"]
        self.assertEqual(performance["collect_like_ratio"], 0.4)
        self.assertEqual(performance["collect_view_rate"], 0.04)
        self.assertEqual(performance["interaction_follower_ratio"], 0.875)
        self.assertNotIn("收藏率", result)

    def test_missing_exposure_data_is_explicit(self):
        result = normalize_platform_evidence({
            "statistics": {"digg_count": 12},
            "author": {},
        })
        self.assertIsNone(result["performance"]["like_view_rate"])
        self.assertIn("缺少播放量", result["data_gaps"])
        self.assertIn("缺少完播率", result["data_gaps"])

    def test_zero_play_with_nonzero_likes_is_treated_as_unavailable(self):
        result = normalize_platform_evidence({
            "statistics": {"play_count": 0, "digg_count": 12},
            "author": {},
        })
        self.assertIsNone(result["performance"]["play_count"])
        self.assertIn("缺少播放量", result["data_gaps"])


if __name__ == "__main__":
    unittest.main()
