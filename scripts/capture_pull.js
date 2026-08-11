/* 装一个 fetch/XHR 拦截器，捕获携程/飞猪的航班数据响应，抓到就自动下载。
 *
 * 为什么需要：pull/poller 接口只在"发起搜索"那一刻请求一次。如果拦截器是
 * 搜索之后才装上的，就错过了。装上后在页面上重新点一次搜索即可捕获，
 * 不用刷新页面（避免重新触发风控）。
 *
 * 只读页面自己发出的请求的响应，不构造、不重放任何请求。
 *
 * 抓到匹配的响应就**自动下载**，不需要手动敲命令——这是唯一需要人工做的
 * 环节（触发搜索这个动作本身），后面能省的步骤都省掉了。__captured() /
 * __save() 仍然保留，用于调试或者自动下载没触发时的手动兜底。
 *
 * 飞猪的 flight_search_result_poller.do 是 JSONP（响应体是 jsonpN(...)
 * 包了一层，不是纯 JSON），原样存成 .json 文件，import_captured.py
 * 导入时自己剥壳，这里不做处理。
 *
 * 用法（书签方式，推荐）：把这段代码存成书签栏里的一个书签，在携程/飞猪的
 * 搜索页上点一下书签即可——不用开 DevTools，不用敲命令。
 *
 * 用法（Console 方式）：
 *   1. 粘贴本文件到 Console 回车
 *   2. 在页面上重新触发一次搜索（改一下日期，或点搜索按钮）
 *   3. 抓到后会自动下载；如果没有，手动运行 __save()
 */
(() => {
  const store = (window.__flightCaptures = window.__flightCaptures || {});
  const saved = (window.__flightSaved = window.__flightSaved || new Set());
  const MATCH = /search\/pull|batchSearch|getVisaLuggageDirectInfo|flight_search_result_poller\.do/;

  // 按 URL 猜平台前缀，决定下载文件名，import_captured.py 靠文件名前缀
  // 分发到对应的 parser（也会用内容再兜底判断一次）。
  const _platformOf = (url) => {
    if (/ctrip/.test(url)) return "ctrip";
    if (/fliggy|sijipiao/.test(url)) return "feizhu";
    if (/qunar/.test(url)) return "qunar";
    return "unknown";
  };

  const _download = (url, text) => {
    // 去重按路径，不带查询串——同一个接口会被页面自己反复轮询（价格精调、
    // 分页），每次 query string 里的 token/时间戳都不同，按完整 URL 去重
    // 会导致同一份数据被反复下载几十次。数据没变就不用再存一份。
    const dedupeKey = url.split("?")[0];
    if (saved.has(dedupeKey)) return;
    saved.add(dedupeKey);
    const name = (url.match(/\/([^/?]+)(?:\?|$)/) || [, "capture"])[1] + ".json";
    const prefix = _platformOf(url);
    const blob = new Blob([text], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${prefix}-${name}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    console.log(`✓ 已自动下载 ${prefix}-${name}（${text.length} 字符），回到网页那边即可`);
  };

  // --- 拦 fetch ---
  if (!window.__fetchHooked) {
    const origFetch = window.fetch;
    window.fetch = async function (...args) {
      const res = await origFetch.apply(this, args);
      try {
        const url = typeof args[0] === "string" ? args[0] : args[0].url;
        if (MATCH.test(url)) {
          res
            .clone()
            .text()
            .then((t) => {
              store[url] = t;
              _download(url, t);
            })
            .catch(() => {});
        }
      } catch (e) {}
      return res;
    };
    window.__fetchHooked = true;
  }

  // --- 拦 XHR ---
  if (!window.__xhrHooked) {
    const origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      if (MATCH.test(url)) {
        this.addEventListener("load", () => {
          try {
            store[url] = this.responseText;
            _download(url, this.responseText);
          } catch (e) {}
        });
      }
      return origOpen.call(this, method, url, ...rest);
    };
    window.__xhrHooked = true;
  }

  // --- 手动兜底（自动下载失败时用） ---
  window.__captured = () => {
    const keys = Object.keys(store);
    if (!keys.length) {
      console.log("还没捕获到。请在页面上重新触发一次搜索（改日期或点搜索按钮）。");
      return;
    }
    keys.forEach((k) => console.log(`${store[k].length} 字符  ${k.slice(0, 140)}`));
    return keys.length;
  };

  window.__save = () => {
    const keys = Object.keys(store);
    if (!keys.length) {
      console.log("还没捕获到，先在页面上重新触发一次搜索。");
      return;
    }
    keys.forEach((k) => _download(k, store[k]));
  };

  console.log(
    "✓ 拦截器已装好。现在在页面上重新触发一次搜索（改一下日期，或点搜索按钮），\n" +
      "  抓到匹配的响应会自动下载到 ~/Downloads，不用手动操作。"
  );
})();
