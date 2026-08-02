"""评测脚本骨架。

指标计算逻辑是确定性的，已经写实；缺的是 cases.jsonl 里的真实数据
（格式见 cases_schema.md）。没有数据时直接退出并提示，不用假数据
跑出一个看起来能用的分数。

用法：
    uv run python tests/eval/run_eval.py tests/eval/cases.jsonl
"""

import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass
class Metrics:
    blocker_recall: float | None
    false_positive_rate: float | None
    total_cost_mae: Decimal | None
    unknown_ratio: float | None
    codeshare_match_accuracy: float | None
    # 单位经济性：单次审计成本 vs 识别出的可避免损失。
    # 需要接入真实 API 计费数据和 cost_if_realized 才能算，先留空。
    audit_cost_vs_avoided_loss: str | None = None

    def render(self) -> str:
        def fmt(v):
            return "n/a（数据不足）" if v is None else v

        return "\n".join(
            [
                f"blocker 召回率:        {fmt(self.blocker_recall)}",
                f"误报率:                {fmt(self.false_positive_rate)}",
                f"总成本 MAE:            {fmt(self.total_cost_mae)}",
                f"unknown 项占比:        {fmt(self.unknown_ratio)}",
                f"跨平台匹配准确率:      {fmt(self.codeshare_match_accuracy)}",
                f"单位经济性:            {fmt(self.audit_cost_vs_avoided_loss)}",
            ]
        )


def _risk_id(r: dict) -> tuple:
    return (r["kind"], tuple(sorted(r["affected_segments"])))


def score(cases: list[dict], predictions: dict[str, list[dict]]) -> Metrics:
    """cases 是评测集，predictions 是 {case_id: [risk_dict, ...]}。

    漏报权重最高（等价于让用户误机），所以 blocker 召回率单独算，
    不和 major/minor 混在一起。
    """
    expected_blockers = 0
    caught_blockers = 0
    predicted_total = 0
    false_positives = 0
    unknown_count = 0

    for case in cases:
        gt = {_risk_id(r): r for r in case.get("ground_truth_risks", [])}
        gt_blockers = {k for k, r in gt.items() if r["severity"] == "blocker"}
        pred = predictions.get(case["id"], [])
        pred_ids = {_risk_id(r) for r in pred}

        expected_blockers += len(gt_blockers)
        caught_blockers += len(gt_blockers & pred_ids)
        predicted_total += len(pred_ids)
        false_positives += len(pred_ids - set(gt))
        unknown_count += sum(1 for r in pred if r.get("needs_user_input"))

    return Metrics(
        blocker_recall=(
            caught_blockers / expected_blockers if expected_blockers else None
        ),
        false_positive_rate=(
            false_positives / predicted_total if predicted_total else None
        ),
        total_cost_mae=None,  # 需要 pipeline 输出 TotalCost 后接上
        unknown_ratio=(unknown_count / predicted_total if predicted_total else None),
        codeshare_match_accuracy=None,  # 需要 cases.jsonl 里的代码共享样例
    )


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    if not path.exists():
        print(
            f"找不到 {path}。评测集需要真实查询记录 + 人工构造缺陷样本，"
            f"格式见 {path.parent / 'cases_schema.md'}。",
            file=sys.stderr,
        )
        return 1

    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    print(f"读到 {len(cases)} 条评测样本")

    # TODO: 跑 pipeline.run() 拿 predictions。需要 ANTHROPIC_API_KEY，
    # 且 fetched 数据从 case 里直接读（评测不联网取数）。
    predictions: dict[str, list[dict]] = {}
    print(score(cases, predictions).render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
