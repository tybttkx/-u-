# -*- coding: utf-8 -*-
"""教材识别（book identity）：从课件页 URL 判定「这是哪一本教材」。

背景（2026-10-21 实机复现）
------------------------------------------------------------------
U校园 的 cid 对应的是**课程**（例：大学英语3），一门课程下挂多本教材
（读写教程3、视听说教程3）。两本教材的课件页 URL 里是**同一个 cid**：
    ...pc.html?cid=1579642886499368960...#/course-v2:...+nhce_v4_rw_3+...
所以 **cid 只能定位课程，不能区分教材**。早期把 cid 当教材级判据并给了最高优先级，
结果是：在读写教程3 的页面上被认成视听说教程3 → 收割被串书守卫挡下
（「页面与题库文件不是同一本」），换本书就收不进去。

教材识别三级判据（按可靠性降序）
------------------------------------------------------------------
1) **课件 URL hash 里的教材代码**：`#/course-v2:...+nhce_v4_rw_3+...`
   → 新视野大学英语 读写教程3。铁证，一票定书。代码形式见 _TEXTBOOK_CODES。
2) **页面上的教材全名**（面包屑/侧边栏/页脚「新视野大学英语（第四版）读写教程3」）
   → 由 knowledge_base 的 _match_book_by_name 归一化匹配，本模块不重复实现。
3) **cid**：仅作课程级提示，并且**只在 URL 里拿不到教材代码时才回退使用**。
   拿不到代码又要靠 cid 时，只允许它指向「该课程只有一本教材」的情形；
   一门课多本教材的 cid 一律不猜（本文件 COURSE_MAP 里已按此清空）。

未收录的代码 / cid 一律不硬猜，返回空串，交给上层继续按页面文字识别。
"""

import re

#: 系列代码 → 系列名
_SERIES_CODES = {
    "nce": "新编大学英语",
    "nhce": "新视野大学英语",
    "nv": "新视野大学英语",
    "nsce": "新标准大学英语",
    "ncec": "新交际英语",
    "eec": "E英语",
}

#: 同系列不同模块的教材名（新编的 rw 叫「综合教程」，新视野的 rw 叫「读写教程」）
_MODULE_NAMES = {
    "新编大学英语": {"rw": ["综合教程", "读写教程"], "ls": ["视听说教程"], "lsn": ["视听说教程"]},
    # vls = Viewing, Listening & Speaking —— 新视野第四版视听说用的就是它
    # （音频资源 URL 实测 `course-v2:Unipus+nhce_4_vls_3+...`），漏了它就等于
    # 视听说一本都认不出来，全退到 cid/页面文字去猜。
    "新视野大学英语": {"rw": ["读写教程", "综合教程"], "ls": ["视听说教程"],
                       "lsn": ["视听说教程"], "vls": ["视听说教程"]},
    "新标准大学英语": {"rw": ["综合教程", "读写教程"], "ls": ["视听说教程"], "lsn": ["视听说教程"]},
    "新交际英语": {"rw": ["综合教程", "读写教程"]},
    "E英语": {"rw": ["综合教程", "读写教程"]},
}

#: 教材代码：<系列>_<版次>_<模块>_<册>，兼容 nv4_rw_3 / nhce_v4_ls_3 / nce_4_rw_3 三种写法
_COURSE_CODE_RE = re.compile(r"\b([a-z]{2,6}?)(?:_?v?(\d{1,2}))_([a-z]{2,5})_(\d{1,2})\b", re.I)

#: 课件 URL 里以 + 包裹的段（课程路由：`+<hash>+<教材代码>+<日期>+`）
_PLUS_TOKEN_RE = re.compile(r"\+([A-Za-z0-9_]+)\+")

#: 课程 id → 教材名。
#: ⚠️ 只用于「该课程**只有一本**教材」的情形；一门课挂多本教材时**必须留空**，
#:    否则就是 2026-10-21 那次串书的根因。
#:    cid 1579642886499368960 = 大学英语3（挂 读写教程3 + 视听说教程3 两本）→ 留空。
COURSE_MAP = {
    # 两个 cid 都已核实为**课程级**，对应多本教材，故一律留空：
    #   1579642886499368960 = 大学英语3（读写教程3 + 视听说教程3 共用）
    #   1855335437114605653 = 大学英语1（读写教程1 + 视听说教程1 共用）
    #     —— 后者原先标着「读写教程1」，实测视听说教程1 的课件页同样是它，
    #        2026-10-09 在视听说1 页面上把书认成视听说3 就是这类误标的后果。
    # 结论：cid 只能定位课程，**不要再往这张表里按「某本教材」填条目**；
    #      教材级判定交给 URL 里的教材代码（book_of_code）。
}


def code_of(url: str) -> str:
    """从课件 URL 里取出教材代码（如 nhce_v4_rw_3）；取不到返回空串。"""
    if not url:
        return ""
    for token in _PLUS_TOKEN_RE.findall(str(url)):
        if _COURSE_CODE_RE.fullmatch(token):
            return token
    # 兜底：URL 里直接出现代码（无 + 包裹，例如被转义或粘贴过）
    m = _COURSE_CODE_RE.search(str(url))
    return m.group(0) if m else ""


def book_of_code(code: str):
    """教材代码 → 教材名候选列表（按优先级）；无法识别返回空列表。"""
    if not code:
        return []
    m = _COURSE_CODE_RE.fullmatch(str(code).strip())
    if not m:
        return []
    series_key = m.group(1).lower()
    module_key = m.group(3).lower()
    number = m.group(4)
    series = _SERIES_CODES.get(series_key)
    if not series:
        return []
    modules = _MODULE_NAMES.get(series, {}).get(module_key)
    if not modules:
        return []
    return [f"{series} {name}{number}" for name in modules]


def book_of(url: str) -> str:
    """从页面 URL 判定教材名（三级判据的第 1、3 级；第 2 级由 knowledge_base 负责）。

    优先级：教材代码 > 课程 cid。取不到一律返回空串（不硬猜）。
    """
    if not url:
        return ""
    url = str(url)
    # 1) 教材代码 —— 铁证
    for name in book_of_code(code_of(url)):
        return name
    # 3) 课程 cid —— 仅课程级，且只在没拿到教材代码时回退
    m = re.search(r"[?&]cid=([0-9]+)", url)
    return COURSE_MAP.get(m.group(1), "") if m else ""
