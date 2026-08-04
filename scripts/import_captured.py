"""把浏览器里手动捕获的平台响应导入成 fetched.json。

为什么需要这个脚本：
    携程的 WhaleGuard 会拦截自动化控制的浏览器（实测 Playwright 驱动的
    Chrome 直接返回 "whaleguard block"，页面连航班接口都不会请求）。
    项目边界里写明不做任何绕过尝试，所以自动取数对携程不可用。

    但你在自己日常的 Chrome 里正常浏览是可以的。流程变成：
      1. 正常打开携程搜索页（你自己的浏览器，正常使用）
      2. Console 里粘 scripts/capture_pull.js 装上响应拦截器
      3. 重新触发一次搜索，然后运行页面里的下载函数
      4. 跑本脚本，把 ~/Downloads 里的 JSON 导入成 fetched.json

    单次触发、只读、用你自己的会话——和自动取数的边界一致，只是触发
    动作由你手动完成。

用法：
    uv run python scripts/import_captured.py                    # 扫 ~/Downloads
    uv run python scripts/import_captured.py path/to/*.json     # 指定文件
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flight_assistant.fetchers.ctrip_parse import parse_batch_search  # noqa: E402
from flight_assistant.matching import group_and_compare  # noqa: E402
from flight_assistant.models import Itinerary, PlatformOffer  # noqa: E402

# 平台 → 识别函数 + 解析函数。加新平台时在这里注册，上层不用改。
PARSERS = {
    "ctrip": (
        lambda d: bool(d.get("data", {}).get("flightItineraryList")),
        parse_batch_search,
    ),
}


def load_captures(paths: list[Path]) -> list[tuple[Itinerary, PlatformOffer]]:
    results: list[tuple[Itinerary, PlatformOffer]] = []
    seen: set[str] = set()

    for path in paths:
        try:
            payload = json.loads(path.read_text())
        except Exception as e:
            print(f"  跳过 {path.name}: 不是合法 JSON ({e})", file=sys.stderr)
            continue

        matched = False
        for platform, (detect, parse) in PARSERS.items():
            if not detect(payload):
                continue
            matched = True
            fetched_at = datetime.fromtimestamp(path.stat().st_mtime)
            try:
                parsed = parse(payload, fetched_at)
            except Exception as e:
                print(f"  {path.name}: {platform} 解析失败 — {e}", file=sys.stderr)
                break
            new = 0
            for itinerary, offer in parsed:
                key = f"{itinerary.model_dump_json()}|{offer.price}"
                if key in seen:
                    continue
                seen.add(key)
                results.append((itinerary, offer))
                new += 1
            print(f"  {path.name}: {platform} → {len(parsed)} 条报价（新增 {new}）")
            break

        if not matched:
            print(f"  跳过 {path.name}: 不含已知平台的航班数据")

    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "files",
        nargs="*",
        help="要导入的 JSON 文件；不给就扫 ~/Downloads 里的 ctrip-*.json",
    )
    ap.add_argument("--out", default="fetched.json")
    args = ap.parse_args()

    if args.files:
        paths = [Path(f).expanduser() for f in args.files]
    else:
        paths = sorted((Path.home() / "Downloads").glob("ctrip-*.json"))
        if not paths:
            print(
                "~/Downloads 里没找到 ctrip-*.json。\n"
                "先按 scripts/capture_pull.js 顶部的说明在浏览器里捕获一份。",
                file=sys.stderr,
            )
            return 1

    print(f"扫描 {len(paths)} 个文件：")
    results = load_captures(paths)
    if not results:
        print("没有导入到任何航班数据。", file=sys.stderr)
        return 1

    comparisons = group_and_compare(results)
    print(f"\n合并后 {len(comparisons)} 个行程，{len(results)} 条报价：")
    for c in sorted(comparisons, key=lambda x: min(o.price for o in x.offers))[:12]:
        segs = c.itinerary.segments
        route = "→".join([segs[0].dep_airport] + [s.arr_airport for s in segs])
        carriers = "/".join(dict.fromkeys(s.carrier for s in segs))
        cheapest = min(o.price for o in c.offers)
        dur = c.itinerary.total_duration_min
        print(
            f"  ¥{cheapest:>7}  {route:18s} {carriers:8s} "
            f"{dur // 60:2d}h{dur % 60:02d}m  转{c.itinerary.stop_count}次  "
            f"{len(c.offers)} 报价"
        )

    out = Path(args.out)
    out.write_text(
        json.dumps(
            [
                {
                    "itinerary": it.model_dump(mode="json"),
                    "offer": o.model_dump(mode="json"),
                }
                for it, o in results
            ],
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"\n已写入 {out}")

    carriers = sorted({s.carrier for it, _ in results for s in it.segments})
    print(f"候选里出现的承运商（官网核价的输入）: {', '.join(carriers)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
