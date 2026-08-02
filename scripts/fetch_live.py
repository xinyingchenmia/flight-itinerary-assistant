"""真实取数入口。必须在用户本机运行，不在云端沙盒执行。

工程边界（写进逻辑本身，不是纸面声明）：
- 单次触发：跑完就退出，没有循环/定时/轮询代码
- headless=False：验证码出现时由人工在浏览器里处理
- 复用用户自己已登录的 Chrome profile（launch_persistent_context），
  不新建账号、不存密码
- 失败即降级标记该平台缺失，不重试——下面对每个平台只 try 一次
- 先跑通一个平台再复制到其他平台：用 --platform 参数逐个核对选择器

用法：
    uv run python scripts/fetch_live.py --origin PVG --dest ORD --date 2026-09-26 --platform ctrip
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flight_assistant.fetchers.ctrip import CtripFetcher  # noqa: E402
from flight_assistant.fetchers.feizhu import FeizhuFetcher  # noqa: E402
from flight_assistant.fetchers.qunar import QunarFetcher  # noqa: E402

FETCHERS = {
    "ctrip": CtripFetcher,
    "feizhu": FeizhuFetcher,
    "qunar": QunarFetcher,
}

# 用户自己的 Chrome profile 目录。复用已登录会话，不新建账号。
DEFAULT_PROFILE = Path.home() / "Library/Application Support/Google/Chrome"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument(
        "--platform",
        choices=sorted(FETCHERS),
        action="append",
        required=True,
        help="逐个平台核对选择器，不要四个同时开工",
    )
    ap.add_argument("--profile-dir", default=str(DEFAULT_PROFILE))
    args = ap.parse_args()

    missing: list[str] = []
    results = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            args.profile_dir,
            headless=False,  # 验证码人工处理，脚本不做任何绕过尝试
            channel="chrome",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for name in args.platform:
            fetcher = FETCHERS[name]()
            try:
                # 单次尝试，不重试——重试到限流没有意义且更容易触发风控
                offers = fetcher.fetch_offers(args.origin, args.dest, args.date, page)
            except Exception as e:
                print(f"[{name}] 取数失败，标记该平台缺失: {e}", file=sys.stderr)
                missing.append(name)
                continue
            print(f"[{name}] 取到 {len(offers)} 条报价")
            results.extend(offers)

        ctx.close()

    if missing:
        print(f"缺失平台: {', '.join(missing)}", file=sys.stderr)

    # 官网取数（OfficialSiteFetcher）依赖前三个平台先跑出候选，取出候选里
    # 出现的承运商集合后再逐个路由查询。等上面三个平台的选择器核对完、
    # 能真正返回候选之后再接这一步。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
