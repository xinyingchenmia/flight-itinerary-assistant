/* 在 Console 里装一个 fetch/XHR 拦截器，捕获携程的航班数据响应。
 *
 * 为什么需要：pull 接口只在"发起搜索"那一刻请求一次。如果 DevTools 是
 * 搜索之后才打开的，Network 面板里就没有这条记录。装上拦截器后在页面上
 * 重新点一次搜索即可捕获，不用刷新页面（避免重新触发风控）。
 *
 * 只读页面自己发出的请求的响应，不构造、不重放任何请求。
 *
 * 用法：
 *   1. 粘贴本文件到 Console 回车
 *   2. 在页面上重新触发一次搜索（改一下日期，或点搜索按钮）
 *   3. Console 里运行：__captured()        看捕获到什么
 *   4. Console 里运行：copy(__dump())      把 JSON 拷到剪贴板
 */
(() => {
  const store = (window.__flightCaptures = window.__flightCaptures || {});
  const MATCH = /search\/pull|batchSearch|getVisaLuggageDirectInfo/;

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
              console.log(`[捕获 fetch] ${url.slice(0, 120)}  ${t.length} 字符`);
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
            console.log(
              `[捕获 XHR] ${String(url).slice(0, 120)}  ${this.responseText.length} 字符`
            );
          } catch (e) {}
        });
      }
      return origOpen.call(this, method, url, ...rest);
    };
    window.__xhrHooked = true;
  }

  // --- 查看 / 导出 ---
  window.__captured = () => {
    const keys = Object.keys(store);
    if (!keys.length) {
      console.log("还没捕获到。请在页面上重新触发一次搜索（改日期或点搜索按钮）。");
      return;
    }
    keys.forEach((k) => console.log(`${store[k].length} 字符  ${k.slice(0, 140)}`));
    return keys.length;
  };

  // 返回最大的那份（就是航班结果），供 copy() 用
  window.__dump = () => {
    const keys = Object.keys(store);
    if (!keys.length) return "(空)";
    const best = keys.sort((a, b) => store[b].length - store[a].length)[0];
    console.log(`导出 ${best.slice(0, 140)}  (${store[best].length} 字符)`);
    return store[best];
  };

  // 直接下载成文件，不经剪贴板 —— 剪贴板容易被后续的复制操作覆盖
  window.__save = () => {
    const keys = Object.keys(store);
    if (!keys.length) {
      console.log("还没捕获到，先在页面上重新触发一次搜索。");
      return;
    }
    keys.forEach((k, i) => {
      const name =
        (k.match(/\/([^/?]+)(?:\?|$)/) || [, `capture-${i}`])[1] + ".json";
      const blob = new Blob([store[k]], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `ctrip-${name}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      console.log(`已下载 ctrip-${name}  (${store[k].length} 字符)`);
    });
    console.log("文件在 ~/Downloads 里。");
  };

  console.log(
    "✓ 拦截器已装好。现在在页面上重新触发一次搜索（改一下日期，或点搜索按钮），\n" +
      "  然后运行:  __captured()   查看捕获情况\n" +
      "  再运行:    __save()       直接下载到 ~/Downloads（不经剪贴板）"
  );
})();
