# -*- coding: utf-8 -*-
"""答案收割：浏览器确认全对之后，把这套答案写进本地题库。

为什么需要它：题库里的答案是人转录的，覆盖不全；而程序每次跑都可能靠 AI/视觉把某题做对。
既然已经在浏览器上验证过是对的，就应该沉淀下来，下次直接命中。

「100% 正确」的判定完全以浏览器为准 —— 必须在页面上看到「全对」的证据，
绝不以「cmd 里 AI 说这是对的」作为依据。判定不到就什么都不写。
"""
import io
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("UCampusBot")

#: 页面上的「答对」标记（各版本类名不同，尽量多列）
CORRECT_SELECTORS = (
    '[class*="correct"]:not([class*="incorrect"])',
    '[class*="is-right"]', '[class*="answer-right"]', '[class*="right-answer"]',
    '[class*="success"]', '[class*="dui"]',
)
WRONG_SELECTORS = (
    '[class*="incorrect"]', '[class*="is-wrong"]', '[class*="wrong"]',
    '[class*="error"]', '[class*="cuo"]',
)
#: 提交后平台有时会直接公布标准答案，这种是"金标准"，优先用
REVEAL_SELECTORS = (
    '[class*="answer-analysis"]', '[class*="rightAnswer"]', '[class*="right-answer"]',
    '[class*="correct-answer"]', '[class*="analysis"]',
)


def _count(driver, selectors) -> int:
    total = 0
    for selector in selectors:
        try:
            total += len(driver.find_elements("css selector", selector))
        except Exception:
            continue
    return total


def page_verdict(driver) -> Tuple[str, Dict[str, int]]:
    """看页面给出的对错信号。

    返回 (verdict, counts)，verdict ∈ {'all_correct', 'has_wrong', 'unknown'}。
    只有「页面确实有题（总题数 ≥ 1）且每一题都被标为正确」时才是 all_correct ——
    仅凭「有几个对勾、没有错号」不够：页面只刷出半页对勾时它同样成立。
    总题数拿不到（答题小结读不到）时不乐观判全对，宁可 unknown。
    """
    correct = _count(driver, CORRECT_SELECTORS)
    wrong = _count(driver, WRONG_SELECTORS)
    reveal = _count(driver, REVEAL_SELECTORS)
    # 总题数只有「答题小结」（正确 (N/M)）能给，读不到就不敢判全对
    try:
        total = read_score_summary(driver).get("total")
    except Exception:
        total = None
    counts: Dict[str, int] = {"correct": correct, "wrong": wrong, "revealed": reveal}
    if isinstance(total, int):
        counts["total"] = total
    if wrong > 0:
        return "has_wrong", counts
    if isinstance(total, int) and total >= 1 and correct == total:
        return "all_correct", counts
    return "unknown", counts


def reveal_official_answers(driver) -> List[str]:
    """如果平台公布了标准答案，把它读出来（金标准，比我们填的还准）。"""
    texts: List[str] = []
    for selector in REVEAL_SELECTORS:
        try:
            elements = driver.find_elements("css selector", selector)
        except Exception:
            continue
        for element in elements[:5]:
            try:
                text = re.sub(r"\s+", " ", element.text or "").strip()
            except Exception:
                continue
            if text and text not in texts:
                texts.append(text)
    return texts


def _norm_block_title(text: str) -> str:
    """块标题归一化：只留字母数字与汉字，小写。用于「标题互相包含」判断。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text or "").lower())


def _remove_superseded_blocks(body: str, task: str,
                              fingerprint: Optional[List[str]],
                              answers: Optional[List[str]] = None) -> Tuple[str, int]:
    """写入新块前，移除「确定是同一页」的旧收录块。

    同页块越积越多会让匹配时并列无法区分，还可能让陈旧答案一直参与竞争。
    判「同一页」的口径（2026-10-10 收紧过一次，原因是标题漂移）：
      · 任务名逐字相同，且（旧块无指纹，或指纹重叠 ≥ 一半）；或
      · 指纹重叠 ≥ 一半，且（两个标题互相包含，或新旧答案完全一致）——
        处理模式不同时任务名会写成整段题目要求（「Listen to the lecture…」）
        或扫描时的短名（「Lecture」），光看名字对不上，同一页就叠了三四条
        （实测；答案与指纹完全一致，只是标题不同）。
    不同页的块保留：同单元同类练习虽共享主题词（指纹沾边甚至 90%+），但
    答案不同、标题也互不包含 —— 绝不误删。
    """
    if not task:
        return body, 0
    new_fp = [str(t).strip() for t in (fingerprint or []) if str(t).strip()]
    new_ans = [re.sub(r"\s+", " ", str(a)).strip() for a in (answers or [])
               if str(a).strip()]
    task_norm = _norm_block_title(task)
    lines = body.split("\n")
    out: List[str] = []
    removed = 0
    in_harvest = False
    i = 0
    header_re = re.compile(r"^## 浏览器核对收录")
    block_re = re.compile(r"^###\s+(.*)$")
    while i < len(lines):
        line = lines[i]
        if header_re.match(line):
            in_harvest = True
        match = block_re.match(line.rstrip()) if in_harvest else None
        if match:
            title = match.group(1).strip()
            j = i + 1
            block_lines: List[str] = []
            while j < len(lines) and not re.match(r"^### |^## ", lines[j]):
                block_lines.append(lines[j])
                j += 1
            fp_line = next((ln for ln in block_lines if ln.startswith("指纹：")), "")
            old_fp = [t.strip() for t in
                      re.split(r"[|/、,，;；\s]+", fp_line.replace("指纹：", ""))
                      if t.strip()]
            old_ans = [re.sub(r"^\s*\d{1,3}\s*[.、:：)）]\s*", "", ln).strip()
                       for ln in block_lines
                       if re.match(r"^\s*\d{1,3}\s*[.、:：)）]\s*\S", ln)]
            title_same = title == task.strip()
            title_norm = _norm_block_title(title)
            title_related = title_same or (
                bool(task_norm) and len(task_norm) >= 4 and bool(title_norm) and (
                    task_norm in title_norm or title_norm in task_norm))
            fp_hit = bool(new_fp) and bool(old_fp) and (
                sum(1 for t in old_fp if t in new_fp) / len(old_fp) >= 0.5)
            same_page = ((not old_fp) and title_same) or (
                fp_hit and (title_related
                            or (bool(new_ans) and new_ans == old_ans)))
            if same_page:
                removed += 1
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out), removed


def _insert_into_area(body: str, header: str, block: str) -> str:
    """把新块插进它自己那个 Unit 的收录区里（而不是文件末尾）。

    以前一律追加到文件尾：文件尾的「## 」标题决定了块被解析成哪个 Unit ——
    于是所有新块都挂到「文件里最后一个 Unit」名下，下次匹配时被 Unit 硬过滤
    挡掉。实测：「（Unit 7）」标题下塞着 Unit 5/Unit 6 的页面答案（时间轴、
    Julius、今日新收的钟表匠…），用户「明明收录了却调不到」就是这么来的。
    """
    lines = body.split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.rstrip() == header), None)
    if start is None:
        return body.rstrip() + f"\n\n{block}\n"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    lines[end:end] = ["", block, ""]
    return "\n".join(lines)


def harvest(book_path: str, unit: Optional[int], task: str, answers: List[str],
            counts: Dict[str, int], source: str = "浏览器核对",
            fingerprint: Optional[List[str]] = None) -> bool:
    """把这套「已核对」的答案写进题库那本书的 md 文件。

    写在文件末尾的「浏览器核对收录」区，不动原有内容；同一小节重复收录会跳过。
    fingerprint 是「题目指纹」——代表这道题的实词（页面题目文本里挑出来的），
    写成「指纹：a | b | c」一行；重做同题时用它核对这套答案就是这道题的，
    同名同空数的小节靠它区分。
    """
    if not book_path or not os.path.isfile(book_path) or not answers:
        return False
    task = (task or "").strip()
    if not task:
        return False

    lines = [f"### {task}"]
    lines.append("")
    if fingerprint:
        tokens = [str(t).strip() for t in fingerprint if str(t).strip()][:12]
        if tokens:
            lines.append("指纹：" + " | ".join(tokens))
    for index, answer in enumerate(answers, 1):
        text = re.sub(r"\s+", " ", str(answer)).strip()
        if text:
            lines.append(f"{index}) {text}")
    block = "\n".join(lines)

    body = io.open(book_path, encoding="utf-8", errors="replace").read()
    body, removed = _remove_superseded_blocks(body, task, fingerprint, answers)
    if removed:
        logger.info(f"[收割] 已移除 {removed} 个同页旧收录块（同名任务，指纹相符/无指纹）")
    header = (f"## 浏览器核对收录（Unit {unit}）" if unit
              else "## 浏览器核对收录（Unit 未标注）")
    if block in body:
        logger.info(f"[收割] 该小节答案已在题库里，跳过：{task}")
        return False

    stamp = time.strftime("%Y-%m-%d %H:%M")
    if header not in body:
        body = body.rstrip() + f"\n\n---\n\n{header}\n\n"
        body += (f"> 下面这些答案是程序在浏览器上做对、并由页面标记确认后自动收录的。\n"
                 f"> 收录时间：{stamp}；本次页面信号：{counts}\n\n")
        body = body.rstrip() + f"\n\n{block}\n"
    else:
        body = _insert_into_area(body, header, block)

    with io.open(book_path, "w", encoding="utf-8") as handle:
        handle.write(body)
    logger.info(f"[收割] 已把 {len(answers)} 条已核对答案写进题库：{task}")
    return True

#: 答题小结页的真实文案（实测）：
#:   「客观题 正确 (10/10) 部分正确 (0/10) 错误 (0/10)」 + 右上角大号「100 分」
#: 分数优先按上下文（得分/本次成绩）找：页面另有「闯关要求 ≥60分」这类文案，
#: 直接抓第一个 "N分" 会先命中它们。
_SCORE_CTX_RE = re.compile(r"(?:得分|本次|成绩)[^0-9]{0,6}(\d{1,3})\s*分")
#: 上下文匹配不到时的回退：页面文字里第一个 "N分"
_SCORE_RE = re.compile(r"(\d{1,3})\s*分")
_SUMMARY_RE = re.compile(r"正确\s*\(?\s*(\d{1,3})\s*/\s*(\d{1,3})\s*\)?")
_WRONG_RE = re.compile(r"错误\s*\(?\s*(\d{1,3})\s*/\s*(\d{1,3})\s*\)?")
#: 主观题小结（实测文案）：「共8道题，主观题8道」「主观题已作答 (8/8)未作答 (0/8)」。
#: 平台对主观题不判分 —— 没有分数、没有对错、也没有闯关门槛，做完即完成。
#: 这三条正则把「已作答/总数」读出来，供收录判据使用（客观题页面不会命中）。
_SUBJ_DONE_RE = re.compile(r"主观题已作答\s*\(?\s*(\d{1,3})\s*/\s*(\d{1,3})\s*\)?")
_SUBJ_TOTAL_RE = re.compile(r"主观题\s*(\d{1,3})\s*道")
_QUESTION_TOTAL_RE = re.compile(r"共\s*(\d{1,3})\s*道题")


def read_score_summary(driver) -> Dict[str, object]:
    """读答题小结：返回 {score, correct, total, ratio}；读不到就是空值。

    这是「正确率是否达标」的官方判据（截图里：正确 10/10、100 分、闯关要求 ≥60）。
    """
    result: Dict[str, object] = {"score": None, "correct": None, "total": None, "ratio": None,
                                 "subjective_done": None, "subjective_total": None,
                                 "subjective_all_done": False}
    try:
        body = driver.execute_script("return (document.body && document.body.innerText) || '';")
    except Exception:
        return result
    text = re.sub(r"[ \t\u00a0]+", " ", str(body or ""))

    match = _SUMMARY_RE.search(text)
    if match:
        result["correct"] = int(match.group(1))
        result["total"] = int(match.group(2))
    else:
        match = _WRONG_RE.search(text)
        if match:
            wrong, total = int(match.group(1)), int(match.group(2))
            result["correct"] = max(0, total - wrong)
            result["total"] = total

    score_match = _SCORE_CTX_RE.search(text) or _SCORE_RE.search(text)
    if score_match:
        result["score"] = int(score_match.group(1))

    correct, total = result["correct"], result["total"]
    if isinstance(correct, int) and isinstance(total, int) and total > 0:
        result["ratio"] = correct / total
    elif isinstance(result["score"], int):
        result["ratio"] = result["score"] / 100

    # 主观题：平台不判分，小结里只有「主观题已作答 (n/m)」。读出来，全部作答时
    # 置 subjective_all_done —— 调用方据此把「做完即完成」的题也收进题库。
    # 只认带「主观题已作答」字样的页面，客观题页面不会命中，不给客观题开后门。
    done_match = _SUBJ_DONE_RE.search(text)
    if done_match:
        done, subj_total = int(done_match.group(1)), int(done_match.group(2))
        result["subjective_done"] = done
        result["subjective_total"] = subj_total
        result["subjective_all_done"] = subj_total > 0 and done >= subj_total
    else:
        subj_only = _SUBJ_TOTAL_RE.search(text)
        all_q = _QUESTION_TOTAL_RE.search(text)
        if subj_only and all_q and int(subj_only.group(1)) >= int(all_q.group(1)):
            # 整页只有主观题、但小结还没刷出「已作答」行：只记总数，不判完成
            result["subjective_total"] = int(subj_only.group(1))
    return result
