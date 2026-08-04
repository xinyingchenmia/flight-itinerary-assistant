/* 第二轮探测：找页面内嵌的 JSON 数据 / JSON 接口 / 稳定的 data-testid。
 *
 * 动机：第一轮发现 class 名是 CSS Modules 哈希（FlightDetailFragment_..__Sm9Yr），
 * 会随携程发版失效；而且折叠卡片拿不到分段数据。如果页面数据以 JSON 形式
 * 存在，字段名比 class 稳定得多，且通常含完整分段信息。
 *
 * 只读当前页面已经加载好的数据，不发任何新请求。
 */
(() => {
  const out = {};

  // 1) data-testid：如果卡片上有，这是最稳的选择器
  const testids = {};
  document.querySelectorAll("[data-testid]").forEach((el) => {
    const v = el.getAttribute("data-testid");
    testids[v] = (testids[v] || 0) + 1;
  });
  console.log("=== data-testid 出现次数（找和卡片数 12 接近的）===");
  Object.entries(testids)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 30)
    .forEach(([k, n]) => console.log(`  ${n} 次   data-testid="${k}"`));

  // 2) 常见的全局状态挂载点
  console.log("\n=== 全局 JSON 状态 ===");
  const globals = [
    "__NEXT_DATA__",
    "__INITIAL_STATE__",
    "__PRELOADED_STATE__",
    "__APP_DATA__",
    "__NUXT__",
    "GlobalData",
    "flightData",
  ];
  globals.forEach((g) => {
    if (window[g] !== undefined) {
      let size = "?";
      try {
        size = JSON.stringify(window[g]).length;
      } catch (e) {}
      console.log(`  ✓ window.${g}  (序列化后 ${size} 字符)`);
    }
  });
  if (!globals.some((g) => window[g] !== undefined)) {
    console.log("  (没找到常见全局状态)");
  }

  // 3) 内嵌 <script> 里的 JSON
  console.log("\n=== <script> 标签里的 JSON ===");
  let found = 0;
  document.querySelectorAll("script").forEach((s, i) => {
    const t = s.textContent || "";
    const isJsonType = (s.type || "").includes("json");
    // 只报告体积够大、且含航班相关关键词的
    const hasFlightWords = /flightNo|segment|arrivalDateTime|departDateTime|priceInfo/i.test(t);
    if ((isJsonType && t.length > 200) || (t.length > 5000 && hasFlightWords)) {
      found++;
      console.log(
        `  script[${i}] type="${s.type || "(js)"}" id="${s.id || ""}" ` +
          `长度=${t.length} 含航班字段=${hasFlightWords}`
      );
      console.log(`    前 300 字: ${JSON.stringify(t.slice(0, 300))}`);
    }
  });
  if (!found) console.log("  (没找到内嵌 JSON)");

  // 4) 页面加载过的 XHR/fetch 接口 —— 找返回航班数据的那个
  console.log("\n=== 页面请求过的接口（找 search / list / product 之类）===");
  try {
    const entries = performance.getEntriesByType("resource");
    entries
      .filter((e) => e.initiatorType === "xmlhttprequest" || e.initiatorType === "fetch")
      .filter((e) => !/\.(png|jpe?g|gif|svg|woff2?|css)$/i.test(e.name))
      .slice(-40)
      .forEach((e) => {
        console.log(`  [${Math.round(e.transferSize / 1024)}KB] ${e.name.slice(0, 160)}`);
      });
  } catch (e) {
    console.log("  (读不到 performance entries)", e.message);
  }

  console.log(
    "\n提示：如果上面有个几百 KB 的 XHR 接口，它很可能就是航班数据的来源。" +
      "在 DevTools 的 Network 标签里按 Size 排序，点开最大的那个 JSON，看 Response。"
  );
})();
