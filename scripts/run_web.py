"""启动本地网页前端。

用法：
    uv run python scripts/run_web.py
    浏览器打开 http://127.0.0.1:8787
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("flight_assistant.web.server:app", host="127.0.0.1", port=8787, reload=False)
