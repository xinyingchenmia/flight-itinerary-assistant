"""一次性初始化专用 Chrome profile：你手动登录，之后取数全自动。

这个脚本只负责把浏览器打开、然后等你操作完。它不代填任何账号密码、
不读取你的凭据、不碰你日常 Chrome 的 profile。

登录态存在 ~/.flight-assistant-chrome 里，后续 fetch_live.py 复用它，
你不用再手动打开网页。

用法：
    uv run python scripts/setup_profile.py
    # 浏览器打开后：自己登录携程，正常搜一次航班，然后回终端按回车

登录完成后跑：
    uv run python scripts/fetch_live.py --origin PVG --dest ORD --date 2026-09-27 --platform ctrip
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE = Path.home() / ".flight-assistant-chrome"

SITES = {
    "ctrip": "https://flights.ctrip.com",
    "feizhu": "https://www.fliggy.com",
    "qunar": "https://flight.qunar.com",
}


def main() -> int:
    site = sys.argv[1] if len(sys.argv) > 1 else "ctrip"
    if site not in SITES:
        print(f"未知站点 {site}，可选: {', '.join(SITES)}", file=sys.stderr)
        return 2

    print(f"专用 profile: {PROFILE}")
    print(f"即将打开 {SITES[site]}\n")
    print("请在浏览器窗口里自己完成：")
    print("  1. 登录你的账号（脚本不会代填，也不会读取你输入的内容）")
    print("  2. 正常搜一次航班，确认页面能出结果")
    print("  3. 回到终端按回车关闭\n")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE),
            headless=False,
            channel="chrome",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(SITES[site], wait_until="domcontentloaded")

        input("完成后按回车…")

        # 记录一下当前状态，方便判断是否真的登录成功
        try:
            cookies = ctx.cookies()
            names = {c["name"] for c in cookies}
            print(f"\n该 profile 现有 {len(cookies)} 个 cookie")
            # 携程的登录态通常带 cticket / DUID 之类；只看存在性，不打印值
            hints = [n for n in ("cticket", "DUID", "_bfa", "login_uid") if n in names]
            print(f"疑似登录相关 cookie: {hints or '（没看到，可能未登录成功）'}")
        except Exception as e:
            print(f"读 cookie 失败: {e}")

        ctx.close()

    print(f"\n登录态已保存在 {PROFILE}")
    print("现在可以跑 fetch_live.py 了，不需要再手动打开网页。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
