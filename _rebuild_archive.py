import os, json, sys
from pathlib import Path
from types import SimpleNamespace

VPM = Path(r"e:\视频\video-parser-main")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
from dotenv import load_dotenv
load_dotenv(VPM / ".env")
sys.path.insert(0, str(VPM))
import video_analysis as va
import feishu_bot as fb
from openai import OpenAI

client = OpenAI(
    base_url=os.getenv("QWEN_API_BASE_URL", "http://localhost:11434/v1"),
    api_key=os.getenv("QWEN_API_KEY", "ollama"),
    timeout=float(os.getenv("MODEL_TIMEOUT_SECONDS", "300")),
)
model = os.getenv("ASR_CLEAN_MODEL_ID", "qwen2.5:7b")

targets = [
    ("VTsieIS4dy0", VPM / "cache/asr/609a42a62365f47d72f8c9464d7333182667b89f5c5796843861244e65026594.json"),
    ("J_pTcMt3cLA", VPM / "cache/asr/24feb436da4456d3434f52a7665b761f301a9afd1cab8f7d1ac01eb90efc2bc9.json"),
]
for vid, jp in targets:
    data = json.loads(jp.read_text(encoding="utf-8"))
    cues = [SimpleNamespace(start=c["start"], end=c["end"], text=c["text"],
                            source=c.get("source", "asr:faster-whisper")) for c in data["cues"]]
    print(f"[{vid}] cues={len(cues)} cleaning...", flush=True)
    cleaned = va.clean_asr_transcript(client, model, cues)
    media = SimpleNamespace(duration=None)
    fb._archive_transcript_file({"title": "", "video_id": vid}, media, cues, cleaned,
                                data.get("status"), data.get("warning"))
    print(f"[{vid}] archived OK", flush=True)
print("ALL_DONE")