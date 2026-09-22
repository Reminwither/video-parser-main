"""Unit tests for the evidence-first video analysis pipeline."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from video_analysis import (
    EvidenceBundle,
    FrameSample,
    MediaInspection,
    TimedText,
    build_synthesis_prompt,
    choose_sample_timestamps,
    format_asr_transcript_for_delivery,
    format_timestamp,
    generate_audience_report_payload,
    normalize_structured_payload,
    parse_cleaned_asr_transcript,
    parse_srt,
    render_audience_report,
    render_structured_analysis,
    detect_analysis_quality_issues,
    transcribe_audio,
)


class VideoAnalysisTests(unittest.TestCase):
    def test_v3_payload_separates_facts_hypotheses_and_transfer(self):
        media = MediaInspection(duration=10.0, width=1080, height=1920, fps=30.0)
        bundle = EvidenceBundle(
            media=media,
            frames=[],
            subtitles=[],
            transcript=[TimedText(0, 3, "开场原话", "asr")],
            transcript_status="timestamped",
        )
        model_payload = {
            "one_sentence_summary": "作者解释一个方法。",
            "topic": "方法",
            "target_audience": "创作者",
            "viewer_value": "获得方法",
            "mechanics": {"content_types": ["干货型"]},
            "viral_hypotheses": [{
                "hypothesis": "问题开场可能提高留存",
                "evidence": ["开场原话"],
                "confidence": "medium",
            }],
            "transfer": {"reusable_genes": [{"name": "问题开场"}]},
            "limitations": ["单条样本"],
        }
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=__import__("json").dumps(model_payload, ensure_ascii=False)
        ))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: response
        )))
        segments = [{
            "start": 0.0, "end": 10.0, "title": "开场", "summary": "解释方法",
            "themes": ["方法"], "core_points": [], "key_cases": [],
            "author_conclusion": [], "_source": "开场原话",
        }]
        with patch("video_analysis.summarize_evidence_windows", return_value=segments):
            payload = generate_audience_report_payload(
                client,
                "test-model",
                bundle,
                None,
                video_info={
                    "platform": "抖音",
                    "video_id": "123",
                    "title": "标题",
                    "statistics": {"digg_count": 100, "comment_count": 20},
                    "author": {"nickname": "作者"},
                },
            )
        self.assertEqual(payload["schema_version"], "video-analysis.v3")
        self.assertEqual(payload["performance"]["comment_like_ratio"], 0.2)
        self.assertEqual(payload["viral_hypotheses"][0]["confidence"], "medium")
        self.assertEqual(payload["transfer"]["reusable_genes"][0]["name"], "问题开场")
        self.assertIn("缺少完播率", payload["data_gaps"])

    def test_asr_delivery_removes_timecodes_and_formats_paragraphs(self):
        text = "[00:00.000-00:02.000] 第一段。\n[00:02.000-00:04.000] 第二段。"
        delivered = format_asr_transcript_for_delivery(text, paragraph_chars=4)
        self.assertNotIn("00:00", delivered)
        self.assertNotIn("[", delivered)
        self.assertIn("第一段。", delivered)
        self.assertIn("\n\n", delivered)

    def test_parse_srt_preserves_timestamps_and_text(self):
        cues = parse_srt(
            """1
00:00:01,250 --> 00:00:03,500
第一行
第二行

2
00:00:04.000 --> 00:00:05.250
<b>数字 42</b>
"""
        )
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].start, 1.25)
        self.assertEqual(cues[0].text, "第一行 第二行")
        self.assertEqual(cues[1].text, "数字 42")

    def test_sampling_combines_coverage_scene_and_text_changes(self):
        cues = [TimedText(7.0, 9.5, "字幕", "subtitle_track")]
        samples = choose_sample_timestamps(
            duration=20.0,
            scene_timestamps=[3.3, 14.4],
            subtitle_cues=cues,
            interval=2.0,
            max_frames=8,
        )
        self.assertLessEqual(len(samples), 8)
        self.assertAlmostEqual(samples[0][0], 0.0, places=1)
        self.assertGreater(samples[-1][0], 19.0)
        reasons = set().union(*(item[1] for item in samples))
        self.assertIn("scene_change", reasons)
        self.assertIn("subtitle_change", reasons)

    def test_no_transcript_prompt_forbids_spoken_claims_and_title_leakage(self):
        media = MediaInspection(duration=10.0, width=1080, height=1920, fps=30.0)
        bundle = EvidenceBundle(
            media=media,
            frames=[FrameSample(1.0, "unused.jpg", ["baseline"])],
            subtitles=[],
            transcript=[],
            transcript_status="not_configured",
            visual_observations="00:01.000 可见一张网页截图",
        )
        prompt = build_synthesis_prompt(bundle, scene_count=0)
        self.assertIn("严禁声称作者“说了/认为/主张”什么", prompt)
        self.assertIn("本轮没有提供标题", prompt)
        self.assertNotIn("视频标题", prompt)
        self.assertIn("E1=直接证据", prompt)

    def test_timestamp_format(self):
        self.assertEqual(format_timestamp(65.125), "01:05.125")
        self.assertEqual(format_timestamp(3661.5), "01:01:01.500")

    def test_audio_without_asr_configuration_degrades_honestly(self):
        media = MediaInspection(audio_tracks=1, audio_codec="aac")
        with patch.dict("os.environ", {}, clear=True):
            cues, status, warning = transcribe_audio("unused.mp4", media, "unused")
        self.assertEqual(cues, [])
        self.assertEqual(status, "not_configured")
        self.assertIn("不会臆造口播内容", warning)

    def test_noncompliant_model_output_cannot_replace_direct_evidence(self):
        media = MediaInspection(duration=20.0, width=1080, height=1920, fps=30.0)
        bundle = EvidenceBundle(
            media=media,
            frames=[FrameSample(0.0, "unused.jpg", ["baseline"])],
            subtitles=[],
            transcript=[
                TimedText(0.0, 4.0, "FDE用AI帮企业提效", "asr:faster-whisper"),
                TimedText(10.0, 14.0, "技术影响的是交付质量", "asr:faster-whisper"),
            ],
            transcript_status="timestamped",
        )
        malformed = {
            "timeline": {},
            "core_summary": "FDE讨论",
            "e1": "FDE（错误展开）",
            "facts": [],
            "e2": "不可定位的自由文本",
            "e3": "不可定位的自由文本",
            "argument": "自由文本",
            "critique": "自由文本",
            "actions": ["自由文本"],
        }
        normalized = normalize_structured_payload(malformed, bundle)
        report = render_structured_analysis(normalized, bundle)
        self.assertIn("[00:00.000–00:04.000][ASR] FDE用AI帮企业提效", report)
        self.assertIn("[00:10.000–00:14.000][ASR] 技术影响的是交付质量", report)
        self.assertNotIn("错误展开", report)
        self.assertIn("## 合理释义（E2）\n\n- 无。", report)
        self.assertEqual(detect_analysis_quality_issues(report), [])

    def test_reader_report_is_human_focused_and_has_no_timeline(self):
        media = MediaInspection(duration=42.0, width=1080, height=1920, fps=30.0)
        bundle = EvidenceBundle(
            media=media,
            frames=[],
            subtitles=[],
            transcript=[TimedText(1.0, 3.0, "这是原文证据。", "asr:cleaned")],
            transcript_status="timestamped",
        )
        payload = {
            "schema_version": "video-analysis.v3",
            "performance": {"like_count": 100, "comment_count": 20, "comment_like_ratio": 0.2},
            "content": {
                "one_sentence_summary": "一句话总结。",
                "topic": "主题",
                "target_audience": "从业者",
                "viewer_value": "获得方法",
                "content_structure": [{"stage": "开场钩子", "title": "开场", "summary": "提出问题"}],
                "core_points": [{"start": 0, "end": 10, "point": "核心观点"}],
                "key_cases": [],
                "author_conclusion": [],
            },
            "mechanics": {
                "title": {"formulas": ["利益承诺型"]},
                "opening_hook": {"mechanism": "反常识问题"},
                "content_types": ["观点型"],
            },
            "viral_hypotheses": [{
                "hypothesis": "反常识问题可能提高继续观看意愿",
                "evidence": ["这是原文证据。"],
                "confidence": "medium",
            }],
            "transfer": {
                "reusable_genes": [{"name": "反常识开场", "mechanism": "制造信息差", "conditions": ["观点内容"]}],
                "failure_conditions": ["后文没有兑现开场"],
            },
            "data_gaps": ["缺少完播率"],
            "source_quotes": ["这是原文证据。"],
        }
        report = render_audience_report(
            payload,
            bundle,
            video_info={"title": "测试视频", "platform": "抖音", "author": {"nickname": "作者"}},
        )
        self.assertIn("## 视频速览", report)
        self.assertIn("## 内容为什么可能传播", report)
        self.assertIn("## 传播假设（不是因果结论）", report)
        self.assertIn("## 可以复用的爆款基因", report)
        self.assertNotIn("时间轴", report)
        self.assertNotIn("00:00", report)

    def test_cleaned_asr_can_be_parsed_back_to_timestamped_cues(self):
        cues = parse_cleaned_asr_transcript(
            "[00:01.000–00:03.500] 第一行。\n[01:02.000–01:05.000] 第二行。"
        )
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].start, 1.0)
        self.assertEqual(cues[1].start, 62.0)
        self.assertEqual(cues[1].text, "第二行。")


if __name__ == "__main__":
    unittest.main()
