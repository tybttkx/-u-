# -*- coding: utf-8 -*-
"""从提交后的页面上把「标准答案」拼出来。

U校园 判分后会公布答案：首字母留在正文里，缺的字母放在 <span class="reference"> 里
（例如正文印着 "va" 和 "f"，reference 是 "lue" / "riendship" → value friendship）。
用户策略：抓到这份权威答案就收进题库，下次遇到同一页直接调用题库，不用再靠 AI 猜。
"""

import json

REVEAL_JS = r"""
(function () {
  function one(s) { return (s || '').replace(/\s+/g, ' ').trim(); }
  function refsOf(scoop) {
    var out = [];
    var refs = scoop.querySelectorAll('.reference');
    for (var i = 0; i < refs.length; i++) { out.push(one(refs[i].textContent)); }
    if (!out.length) {
      var wrap = scoop.querySelector('.reference-wrapper');
      if (wrap) { out.push(one(wrap.textContent).replace(/[()]/g, '').trim()); }
    }
    return out.filter(function (x) { return x; });
  }
  function headsOf(scoop, limit) {
    // 空格前面的文字（首字母就印在这里）：往前找兄弟节点，遇到上一个空格就停
    var parts = [];
    var node = scoop.previousSibling;
    var guard = 0;
    while (node && guard < limit) {
      guard += 1;
      var isScoop = node.nodeType === 1 && node.className &&
                    String(node.className).indexOf('fe-scoop') >= 0;
      if (isScoop) { break; }
      var t = one(node.textContent || '');
      if (t) { parts.unshift(t); }
      node = node.previousSibling;
    }
    return one(parts.join(' '));
  }
  var out = [];
  var scoops = document.querySelectorAll('.fe-scoop');
  for (var i = 0; i < scoops.length; i++) {
    var s = scoops[i];
    var numEl = s.querySelector('.question-number');
    var input = s.querySelector('input, textarea');
    out.push({
      n: numEl ? one(numEl.textContent) : String(i + 1),
      heads: headsOf(s, 8),
      refs: refsOf(s),
      filled: input ? one(input.value) : '',
      state: (function () {
        var w = s.querySelector('.reference-wrapper');
        var c = s.querySelector('.comp-abs-input');
        return { refCls: w ? String(w.className) : '', inputCls: c ? String(c.className) : '' };
      })()
    });
  }
  return JSON.stringify(out);
})()
"""


def _prefix_of(heads: str) -> str:
    """heads 是一长串正文，真正的「已印首字母」是最后一个词（如 'va' / 'f' / 'a f'）。"""
    words = [w for w in str(heads or "").split(" ") if w]
    return words[-1] if words else ""


def _join_heads_refs(heads: str, refs):
    """拼出可读的完整答案（仅用于记录）。

    heads 形如 "…  1) va"，refs 形如 ["lue"] → "value"。
    """
    prefix = _prefix_of(heads)
    refs = [r for r in (refs or []) if r]
    if not refs:
        return ""
    return (prefix + refs[0]) if prefix else refs[0]


def read_fill_values(driver, min_chars: int = 1):
    """返回 [(空号, 应当填入的内容), ...]：页面已印首字母时，缺的部分才是要填的。

    U校园 的 Collocation 题会给「首字母」并要求只补剩下的部分（截图实证：
    印着 va / f，reference 显示 lue / riendship）。填完整搭配会被判错。
    """
    if driver is None:
        return []
    try:
        raw = driver.execute_script(REVEAL_JS)
    except Exception:
        return []
    if not isinstance(raw, str):
        # 页面脚本没返回字符串（脚本没生效 / 页面没有可读答案）→ 本次不读；
        # 不这么挡的话 json.loads(None) 会抛 TypeError，日志里全是"读页面公布答案失败"
        return []
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[页面] 公布答案的 JSON 解析失败，本次不读: {str(exc)[:60]}")
        return []
    out = []
    for row in rows or []:
        refs = [r for r in (row.get("refs") or []) if r]
        value = " ".join(refs) if refs else str(row.get("filled") or "").strip()
        if len(value) >= min_chars:
            out.append((str(row.get("n") or ""), value))
    return out


def read_revealed_answers(driver, min_chars: int = 2):
    """返回 [(空号, 标准答案), ...]；页面上没有公布答案时返回 []。"""
    if driver is None:
        return []
    try:
        raw = driver.execute_script(REVEAL_JS)
    except Exception:
        return []
    try:
        rows = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        print(f"[页面] 公布答案的 JSON 解析失败，本次不读: {str(exc)[:60]}")
        return []
    answers = []
    for row in rows or []:
        answer = _join_heads_refs(row.get("heads", ""), row.get("refs") or [])
        if not answer:
            # 没有任何 reference：说明这一空原本就是对的/无缺口，用页面里填着的值
            answer = str(row.get("filled") or "").strip()
        if len(answer) >= min_chars:
            answers.append((str(row.get("n") or ""), answer))
    return answers
