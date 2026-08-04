/* 粘进浏览器 DevTools Console 运行，自动找出航班卡片的候选选择器。
 *
 * 原理：航班列表页里，每张航班卡片是同一个 class 重复出现 N 次的结构。
 * 找出"出现次数 >= 5 且内部含价格数字"的 class，就是卡片容器的强候选。
 *
 * 用完把输出整段发回来即可。
 */
(() => {
  const counts = {};
  document.querySelectorAll("*").forEach((el) => {
    if (!el.className || typeof el.className !== "string") return;
    el.className.trim().split(/\s+/).forEach((c) => {
      if (!c) return;
      (counts[c] = counts[c] || []).push(el);
    });
  });

  const priceRe = /[¥￥]\s*\d{3,}|\d{3,}\s*起/;
  const cands = Object.entries(counts)
    .filter(([, els]) => els.length >= 5 && els.length <= 60)
    .map(([cls, els]) => {
      const withPrice = els.filter((e) => priceRe.test(e.innerText || "")).length;
      return { cls, n: els.length, withPrice, sample: els[0] };
    })
    .filter((c) => c.withPrice >= 3)
    // 优先选文本量适中的（整张卡片，而不是整个列表容器或单个价格标签）
    .sort((a, b) => {
      const len = (c) => ((c.sample && c.sample.innerText) || "").length;
      return Math.abs(len(a) - 200) - Math.abs(len(b) - 200);
    })
    .slice(0, 8);

  console.log("=== 卡片容器候选（越靠前越可能是整张航班卡片）===");
  cands.forEach((c) => {
    console.log(
      `\n.${c.cls}   出现 ${c.n} 次，其中 ${c.withPrice} 个含价格`
    );
    console.log("  首个样本文本:", JSON.stringify((c.sample.innerText || "").slice(0, 220)));
  });

  // 对最佳候选，打印内部子元素的 class，用来定位价格/时刻/航班号字段
  if (cands.length) {
    const best = cands[0].sample;
    console.log(`\n=== .${cands[0].cls} 内部字段候选 ===`);
    const seen = new Set();
    best.querySelectorAll("*").forEach((el) => {
      const t = (el.innerText || "").trim();
      if (!t || t.length > 40 || el.children.length > 0) return;
      const cls = (typeof el.className === "string" ? el.className : "").trim();
      const key = cls + "|" + t;
      if (seen.has(key)) return;
      seen.add(key);
      console.log(`  ${cls ? "." + cls.split(/\s+/).join(".") : el.tagName}  ->  ${JSON.stringify(t)}`);
    });
  }

  console.log("\n=== 也检查一下有没有可直接用的 data-* 属性（比 class 稳定）===");
  const dataAttrs = new Set();
  document.querySelectorAll("*").forEach((el) => {
    for (const a of el.attributes || []) {
      if (a.name.startsWith("data-") && a.name.length < 30) dataAttrs.add(a.name);
    }
  });
  console.log([...dataAttrs].slice(0, 40).join(", "));
})();
