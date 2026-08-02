# 评测集格式（tests/eval/cases.jsonl）

30 条行程，来源为**真实查询记录 + 人工构造**。这个文件只定义格式；
真实数据需要你自己填充 `cases.jsonl`（已加进 .gitignore，避免把真实
查询记录提交上去）。骨架里刻意没有预置 30 条假数据——评测集的价值
完全来自 ground truth 的真实性，编出来的数据会让所有指标失去意义。

每行一个 JSON 对象：

```json
{
  "id": "case-001",
  "source": "real | synthetic",
  "query": "上海到芝加哥，9/26，转机≤2，总时长优先",
  "fetched": [
    {
      "itinerary": { "segments": [...], "tickets": [...],
                     "total_duration_min": 1140, "stop_count": 1 },
      "offer": { "platform": "ctrip", "price": "5200", "currency": "CNY",
                 "fetched_at": "2026-09-01T00:00:00Z", "booking_url": null,
                 "fare_conditions_raw": null, "confidence": "confirmed" }
    }
  ],
  "injected_defect": {
    "kind": "mct_tight | split_ticket | transit_visa_required | none",
    "note": "把 NRT 中转从 3h30m 压到 40m"
  },
  "ground_truth_risks": [
    { "kind": "mct_tight", "severity": "blocker", "affected_segments": [0, 1] }
  ],
  "ground_truth_total_cost": "5680",
  "answers": { "护照国籍？": "CN" }
}
```

字段说明：

- `source`：真实查询记录还是人工构造。误报率要分开看——真实样本里的
  误报比构造样本里的更值得关注。
- `injected_defect`：**人工植入缺陷**。拿正常行程改造出已知问题
  （压缩中转时间、拆成两张票、换成需过境签的中转点），这样才有可
  验证的 ground truth。`kind: "none"` 表示这条是干净样本，用来测误报率。
- `ground_truth_risks`：期望被识别出的风险。只写 `kind`/`severity`/
  `affected_segments` 三项做匹配，不比对 `evidence` 原文（自然语言无法
  精确比对，但可以人工抽查 evidence 是否真的引用了具体数字）。
- `answers`：澄清对话 agent 提问时的预置答复，按问题关键词匹配。
  跑评测时不需要真人在场。

## 代码共享航班样例

跨平台匹配准确率需要单独几组样例：同一趟航班在不同平台显示不同航班号
（例如携程 `UA7623` / 飞猪 `UA0850`，同承运方同起降时刻）。这类样例放
在 `fetched` 里、`injected_defect.kind` 设 `"none"`，验证 `itinerary_key`
把它们归成一组而不是两条候选。

`tests/test_matching.py` 里已经有一个最小版本的单测覆盖这个场景，评测集
里的样例是它的真实数据版本。
