"""Kiểm chứng model nào thực sự trả lời.

Gọi `gemini_chat` với từng model và in ra `servedModel` / `quotaNotice` /
`fallbackReason` — ba trường cho biết câu trả lời đến từ model nào, có bị Google hạ
cấp vì hết quota không, và server có phải fallback về model default không.

Usage:
    python probe_served_model.py                      # dùng danh sách mặc định
    python probe_served_model.py gemini-pro           # một model cụ thể
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_mcp_stdio import Client, EXE, call  # noqa: E402

MODELS = sys.argv[1:] or [
    "gemini-carbon",
    "gemini-flash-lite",
]


def main():
    cl = Client(EXE)
    init_id = cl.send("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1.0"},
    })
    cl.read(init_id, 60)
    cl.send("notifications/initialized", {}, notify=True)

    for model in MODELS:
        try:
            out = call(cl, "gemini_chat",
                       {"prompt": "Say OK", "model": model}, 900)
            data = json.loads(out)
            print(f"{model:22s} served={data.get('servedModel')!r} "
                  f"notice={data.get('quotaNotice')!r} "
                  f"fallback={str(data.get('fallbackReason'))[:80]!r} "
                  f"reply={str(data.get('reply'))[:40]!r}")
        except Exception as e:
            print(f"{model:22s} FAILED: {type(e).__name__}: {e}")

    cl.p.kill()


if __name__ == "__main__":
    sys.exit(main())
