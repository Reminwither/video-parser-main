import unittest
from unittest.mock import patch

from feishu_bitable import archive_video_analysis


class FeishuBitableTests(unittest.TestCase):
    def test_archive_keeps_human_columns_and_machine_json(self):
        calls = []

        def fake_request(method, path, body=None):
            calls.append((method, path, body))
            return {"record": {"record_id": "rec_test"}}

        payload = {
            "schema_version": "video-analysis.v3",
            "source": {"video_id": "1", "title": "标题", "platform": "抖音", "author": {"nickname": "作者"}},
            "performance": {"like_count": 100, "comment_count": 20, "comment_like_ratio": 0.2},
            "content": {
                "one_sentence_summary": "一句话",
                "topic": "核心议题",
                "target_audience": "创作者",
                "viewer_value": "学会方法",
                "content_structure": [{"stage": "开场钩子", "title": "反问", "summary": "提出问题"}],
                "core_points": [{"point": "观点一"}],
                "key_cases": [{"case": "案例一"}],
                "author_conclusion": [{"conclusion": "结论一"}],
            },
            "mechanics": {
                "title": {"formulas": ["提问型"]},
                "opening_hook": {"mechanism": "反常识"},
                "content_types": ["观点型"],
            },
            "viral_hypotheses": [{"hypothesis": "促进讨论", "confidence": "medium", "evidence": ["原话"]}],
            "transfer": {
                "reusable_genes": [{"name": "问题钩子", "mechanism": "激活回答欲", "conditions": ["目标明确"]}],
                "failure_conditions": ["问题过于宽泛"],
            },
            "data_gaps": ["缺少数据"],
            "source_quotes": ["原话"],
        }
        with patch("feishu_bitable.ensure_video_analysis_table", return_value={
            "app_token": "app_test", "table_id": "tbl_test", "base_url": "https://example.feishu.cn/base/app_test"
        }), patch("feishu_bitable._request", side_effect=fake_request):
            result = archive_video_analysis(
                url="https://example.com/video/1",
                metadata={"title": "标题", "platform": "抖音", "author": {"nickname": "作者"}},
                payload=payload,
                cleaned_asr="ASR 原文",
                duration_seconds=12.3,
            )

        self.assertEqual(result["record_id"], "rec_test")
        fields = calls[0][2]["fields"]
        self.assertEqual(fields["一句话总结"], "一句话")
        self.assertIn("开场钩子", fields["内容结构"])
        self.assertEqual(fields["评论点赞比"], "20.00%")
        self.assertIn("问题钩子", fields["可复用基因"])
        self.assertIn('"schema_version": "video-analysis.v3"', fields["结构化JSON"])
        self.assertEqual(fields["ASR原文"], "ASR 原文")


if __name__ == "__main__":
    unittest.main()
