# -*- coding: utf-8 -*-
"""本地题库知识库检索。

答案库移植自 unipus-agent 的 knowledge/ 目录（微信公众号答案文章视觉转录的 Markdown），
按「教材 → Unit → 小节 → 题号」组织：

    knowledge/
    ├── INDEX.md                    # 全量书目与转录状态
    └── <系列名>/<教材名>.md          # 一本书一个文件

单书文件格式：

    # <教材名>— Unit N <单元主题>
    ## 1-6 Read and practice · Banked cloze
    词库：adequate / assigned / …
    1. finals
    2. due

本模块只做「答案前置」：在调用大模型之前先查本地库，命中的题直接给出答案，
未命中的题仍然交给原来的 AI 流程，因此不改变任何填写与提交逻辑。

答案会按原执行器（AnswerExecutor）能识别的格式组装：选择题给字母，填空/选词给
编号文本，简答给编号正文，从而完全复用原本的解析与回填代码。
"""

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# 常量与正则
# ---------------------------------------------------------------------------

#: 知识库能给出答案的题型（用 QuestionType 的名字比较，避免反向 import 主程序）
CHOICE_TYPES = {
    "SINGLE_CHOICE", "LISTENING_CHOICE", "VIDEO_CHOICE",
    "MULTIPLE_CHOICE", "VOCABULARY_TEST",
}
BLANK_TYPES = {"BANKED_CLOZE", "DROPDOWN_SELECT", "FILL_IN", "LISTENING_FILL_IN"}
TEXT_TYPES = {"TEXT", "MY_VOICE_TEXT"}
ORDER_TYPES = {"SORTING"}
ANSWERABLE_TYPES = CHOICE_TYPES | BLANK_TYPES | TEXT_TYPES | ORDER_TYPES

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

#: 版本词会在配对教材名时被剥离（页面写「第四版/2023版」，文件名可能不写）
_EDITION_WORDS = (
    "第一版", "第二版", "第三版", "第四版", "第五版", "第六版",
    "智慧版", "新版", "修订版", "数字版", "思政版",
    "2020版", "2021版", "2022版", "2023版", "2024版", "2025版", "2026版",
)

#: U校园课程页 hash 里的教材代码 → 教材名。未收录的代码不会硬猜，直接放弃匹配。
#: U校园课程页里出现的教材代码 → 系列名。已实测的代码形式有三种：
#:   nv4_rw_3      新视野读写教程3      （系列+版次 连写）
#:   nhce_v4_ls_3  新视野视听说教程3    （系列+v版次）
#:   nce_4_rw_3    新编综合教程3        （系列_版次）
#: 未收录的代码不会硬猜，直接放弃；配错也无妨 —— 教材在知识库里不存在时会被丢弃。
_SERIES_CODES = {
    # 键是正则第一组捕获的「系列基名」：_COURSE_CODE_RE 的第一组是惰性 [a-z]{2,6}?，
    # 版次数字会被后面的 _?v?(\d{1,2}) 吃掉，所以 nv4_rw_3 / nce_4_rw_3 取出来的
    # 只有 nv / nce —— 原先的 "nv4" / "nce4" 两个键永远取不到，已删。
    "nce": "新编大学英语",
    "nhce": "新视野大学英语",
    "nv": "新视野大学英语",
    "nsce": "新标准大学英语",
    "ncec": "新交际英语",
    "eec": "E英语",
}

#: 同一个模块代码在不同系列里叫法不同：新编的 rw 是「综合教程」，新视野的 rw 是「读写教程」。
#: 所以按「系列 → 模块 → 候选名称」二级映射，逐个去知识库里试，命中即用。
_MODULE_CODES = {
    "新编大学英语": {"rw": ["综合教程", "读写教程"], "ls": ["视听说教程"], "lsn": ["视听说教程"]},
    "新视野大学英语": {"rw": ["读写教程", "综合教程"], "ls": ["视听说教程"], "lsn": ["视听说教程"]},
    "新标准大学英语": {"rw": ["综合教程", "读写教程"], "ls": ["视听说教程"], "lsn": ["视听说教程"]},
    "新交际英语": {"rw": ["综合教程", "读写教程"]},
    "E英语": {"rw": ["综合教程", "读写教程"]},
}
_DEFAULT_MODULE_CODES = {
    "rw": ["综合教程", "读写教程"],
    "ls": ["视听说教程"],
    "lsn": ["视听说教程"],
    "zw": ["综合训练"],
}

#: 同时兼容上述三种代码形式
_COURSE_CODE_RE = re.compile(r"\b([a-z]{2,6}?)(?:_?v?(\d{1,2}))_([a-z]{2,5})_(\d{1,2})\b", re.I)

#: 小节里的局部编号（1-6 / 12-3），知识库用它定位 Unit 与小节序号
_PART_RE = re.compile(r"(?:^|[^\d])(\d{1,2})\s*-\s*(\d{1,3})(?![\d])")
#: 「Section A / Part B」那套编号（新视野、新标准、新交际等）。只认首字母大写的写法，
#: 免得英语散文里的「in this part a ...」被当成小节编号。
_SECTION_LETTER_RE = re.compile(r"(?:Section|SECTION|Part|PART)\s*([A-Z])(?![A-Za-z])")
_UNIT_RE = re.compile(r"Unit\s*(\d{1,2})", re.I)
#: 「1. xxx」「1) xxx」「1、xxx」「空1: xxx」
_ANSWER_RE = re.compile(r"^\s*(\d{1,3})\s*[.)、,：:]\s*(.*)$")
#: 整条答案就是一个选项字母（允许 "A; B" 这种多选写法）
_CHOICE_ANSWER_RE = re.compile(r"^[A-Za-z](?:\s*[,，、;；/|和及]\s*[A-Za-z])*$")

#: 「浏览器核对收录」收割块的 H2 标题前缀。收割小节（H3）的 title 以它开头。
#: 书不可逐题作答（answer_ready 不达标）时，只有这些小节可以放行 —— 它们是
#: 浏览器确认过全对后收录的已验证答案，真实可信（方案B，2026-10-03）。
_HARVEST_BLOCK_PREFIX = "浏览器核对收录"


def _is_harvest_section(section: "KbSection") -> bool:
    """这个小节是否来自「浏览器核对收录」收割区（H2/H3 的 title 都以该前缀开头）。"""
    return str(getattr(section, "title", "") or "").startswith(_HARVEST_BLOCK_PREFIX)


#: 收录块的「指纹：」行（收割时写入，用来核对「这套答案就是这道题的」）
_FINGERPRINT_RE = re.compile(r"^\s*指纹\s*[:：]\s*(.+)$")


def extract_fingerprint(texts, limit: int = 12) -> List[str]:
    """从页面题目文本里挑出最能代表这道题的词（题目指纹）。

    越长的实词越不可能在别的题里重现，按「长度降序、同长按字典序」取前 limit 个
    —— 顺序确定，同一页重做时提取结果一致，匹配时才对得上。
    """
    tokens: Set[str] = set()
    for text in texts or []:
        tokens |= _tokens(str(text or ""))
    return sorted(tokens, key=lambda t: (-len(t), t))[:limit]


def _looks_like_choice_answer(text: str) -> bool:
    """这条答案是不是「就是选项字母」。"""
    return bool(_CHOICE_ANSWER_RE.match(str(text).strip()))
_WORDBANK_RE = re.compile(r"^\s*(?:词库|词汇|选词|word\s*bank)\s*[：:]\s*(.+)$", re.I)

#: 转录说明/自检这类 meta 小节，里面也有 1. 2. 编号，但绝不是答案
_META_SECTION_RE = re.compile(r"转录自检|自检|校对记录|转录说明|校验")
#: 「词库：未在截图中提供」这类占位说明，不是真实词库
_PLACEHOLDER_BANK_RE = re.compile(r"未(在截图中)?(提供|给出|包含)|无词库|未提供|未给出|见截图|无法转录|未列出")
#: 词库条目里的中文括注/说明（如「（截图未包含词库，无法转录）」「maintain 维持」）
_BANK_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
_BANK_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff01-\uff5e\u2018\u2019\u201c\u201d]+")


def _clean_bank_entry(text: str) -> str:
    """把词库条目洗干净：去掉中文括注与中文说明，只留英文词。"""
    cleaned = _BANK_PAREN_RE.sub(" ", str(text or ""))
    cleaned = _BANK_CJK_RE.sub(" ", cleaned)
    return cleaned.strip()

#: 一本书里「含 ≥2 条编号答案的小节」占比低于此值时，认为它的转录不是答案表，
#: 不参与答案检索（例如按教材原文逐页转录、答案是散文的视听说教程）。
_ANSWER_READY_MIN = 0.30

#: 参与小节名比对的英文/中文实词（用于 token 重合度打分）
_TOKEN_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "unit", "part", "step", "read", "reading", "page", "task", "practice",
    "get", "ready", "listen", "listening", "watch", "speak", "speaking",
    "write", "writing", "think", "understand", "understandin", "translate",
    "translating", "view", "viewing", "exercise", "exercises", "activity",
}


def default_root() -> str:
    """定位知识库目录。

    PyInstaller 打包后 knowledge/ 被打进包里解压到 _MEIPASS，并不在 exe 同级目录，
    所以按「解包目录 → 模块同级 → exe 同级」的顺序找第一个真实存在的目录。
    """
    candidates: List[str] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "knowledge"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge"))
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), "knowledge"))
    for path in candidates:
        if os.path.isdir(path):
            return path
    return candidates[0]


def _normalize(text: str) -> str:
    """教材名/小节名归一化：剥离版本词、括号、空白与标点后小写化。"""
    if not text:
        return ""
    out = str(text)
    for word in _EDITION_WORDS:
        out = out.replace(word, "")
    out = out.replace("（", "(").replace("）", ")")
    out = re.sub(r"[()\[\]【】\s·・\-—_/\\、,，.。:：;；'\"“”‘’]+", "", out)
    return out.lower()


def _tokens(text: str) -> Set[str]:
    """切出用于小节名比对的实词（英文单词 + 中文二字以上片段）。"""
    if not text:
        return set()
    raw = re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", str(text))
    return {t.lower() for t in raw if t.lower() not in _TOKEN_STOPWORDS}


def _part_of(text: str) -> Optional[str]:
    """从文本里取出小节编号：'1-6 Read and practice' → '1-6'，'Section A · …' → 'A'。

    两套编号体系并存：老教材是「1-6」这种课时号，新视野/新标准那套是 Section A/B/C。
    先认 Section 字母，再退回数字编号。
    """
    letter = _SECTION_LETTER_RE.search(text or "")
    if letter:
        return letter.group(1).upper()
    match = _PART_RE.search(text or "")
    if not match:
        return None
    return f"{int(match.group(1))}-{int(match.group(2))}"


def _strip_section_prefix(text: str) -> str:
    """去掉名字里的「Section A / Part B」前缀，只留下页面名。

    'Section A · Language focus' → 'Language focus'
    """
    cleaned = _SECTION_LETTER_RE.sub("", text or "", count=1)
    return cleaned.strip(" 　·-—|/\\")



def _strip_part(text: str) -> str:
    """去掉小节名开头的编号，如 '1-6 Read and practice' → 'Read and practice'。"""
    return re.sub(r"^\s*\d{1,2}\s*-\s*\d{1,3}\s*", "", (text or "").strip()).strip()


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class KbSection:
    """知识库里的一个小节，例如 '1-6 Read and practice · Banked cloze'。"""
    title: str
    unit: Optional[int] = None
    part: Optional[str] = None
    page: str = ""
    task: str = ""
    answers: List[Tuple[int, str]] = field(default_factory=list)
    wordbank: List[str] = field(default_factory=list)
    #: 同一题号重复出现时的额外答案（知识库对「Step 1 / Step 2」这类会重复编号）
    extras: List[Tuple[int, str]] = field(default_factory=list)
    #: 收录块的「题目指纹」：代表这道题的实词（收割时从页面题目文本提取）。
    #: 重做同题时核对指纹——同名同空数的小节靠它区分，指纹是这套答案的「身份证」。
    fingerprint: List[str] = field(default_factory=list)

    def answer_map(self) -> Dict[int, str]:
        """题号 → 答案（重复题号保留首个，首个通常是主答案）。"""
        out: Dict[int, str] = {}
        for number, text in self.answers:
            out.setdefault(number, text)
        return out

    def ordered_answers(self) -> List[str]:
        return [text for _, text in self.answers]

    def keywords(self) -> Set[str]:
        return _tokens(f"{self.page} {self.task} {self.title}")


@dataclass
class KbBook:
    """知识库里的一本书（一个 md 文件）。"""
    name: str
    path: str
    sections: List[KbSection] = field(default_factory=list)
    #: 解析时见过的（非 meta）小节总数。就绪度要用它做分母：没答案的小节会被丢弃，
    #: 若拿留下来的小节当分母，一本全是散文的书会算出虚高的就绪度。
    parsed_sections: int = 0
    #: 含 ≥2 条编号答案的小节占比。逐页转录教材原文（答案是散文）的书会很低。
    answer_ratio: float = 0.0

    @property
    def answer_ready(self) -> bool:
        """转录是否是可逐题取用的答案表。不达标就不用来填答案。"""
        return self.answer_ratio >= _ANSWER_READY_MIN

    def by_part(self, part: str) -> List[KbSection]:
        return [s for s in self.sections if s.part == part]


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------

class KnowledgeBase:
    """Markdown 题库知识库：加载、教材识别、小节匹配、答案组装。"""

    def __init__(
        self,
        root: Optional[str] = None,
        enabled: bool = True,
        textbook: str = "auto",
        min_confidence: str = "medium",
        verify_wordbank: bool = True,
        prefer_kb: bool = True,
        verbose: bool = True,
    ):
        if root is None:
            root = default_root()
        self.root = root
        self.enabled = bool(enabled)
        self.textbook_pref = (textbook or "auto").strip()
        self.min_confidence = min_confidence if min_confidence in _CONFIDENCE_RANK else "medium"
        self.verify_wordbank = bool(verify_wordbank)
        self.prefer_kb = bool(prefer_kb)
        self.verbose = verbose

        self._books: Optional[List[KbBook]] = None
        self._book: Optional[KbBook] = None
        self._resolve_note = ""
        self.stats: Dict[str, int] = {
            "hit": 0, "miss": 0, "ai": 0,
            "kb_choice": 0, "kb_blank": 0, "kb_text": 0,
        }

    # -- 日志 -------------------------------------------------------------

    def _log(self, message: str):
        if self.verbose:
            print(f"    [知识库] {message}")

    # -- 加载 -------------------------------------------------------------

    @property
    def books(self) -> List[KbBook]:
        if self._books is None:
            self._books = self._load_books()
        return self._books

    def available_books(self) -> List[str]:
        return [b.name for b in self.books]

    def _load_books(self) -> List[KbBook]:
        books: List[KbBook] = []
        if not self.root or not os.path.isdir(self.root):
            self._log(f"未找到知识库目录：{self.root}")
            return books

        for dirpath, _dirnames, filenames in os.walk(self.root):
            for filename in sorted(filenames):
                if not filename.lower().endswith(".md"):
                    continue
                if filename.upper().startswith("INDEX"):
                    continue
                path = os.path.join(dirpath, filename)
                try:
                    book = self._parse_book(path)
                except Exception as exc:  # 单个文件坏掉不影响其它教材
                    self._log(f"解析失败 {filename}: {str(exc)[:60]}")
                    continue
                if book and book.sections:
                    total = book.parsed_sections or len(book.sections)
                    with_answers = sum(1 for s in book.sections if len(s.answers) >= 2)
                    book.answer_ratio = with_answers / total
                    if not book.answer_ready:
                        self._log(
                            f"{book.name}：{total} 个小节里只有 {with_answers} 个是可逐题取用的答案"
                            f"（{book.answer_ratio:.0%}，多半是按教材原文转录、答案是散文），"
                            "不参与答案检索"
                        )
                    books.append(book)
        return books

    @staticmethod
    def _parse_book(path: str) -> Optional[KbBook]:
        """解析一本书。

        支持两套小节写法：

        1. 单级（新编大学英语）：`## 1-6 Read and practice · Banked cloze` 下面直接列答案。
        2. 两级（新视野大学英语）：H2 是页面、H3 才是任务：

               ## Section A · Reading comprehension
               ### Understanding the text
               1) ...

           H2 下面是 H3 时，页面名取整个 H2（如「Section A · Reading comprehension」），
           任务名取 H3，这样 Section A / Section B 的同名任务才不会互相串位。
        """
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()

        name = os.path.splitext(os.path.basename(path))[0]
        book = KbBook(name=name, path=path)

        current_section: Optional[KbSection] = None
        parent_section: Optional[KbSection] = None
        current_unit: Optional[int] = None
        current_answer: Optional[List[Any]] = None  # [number, [lines...]]
        last_number = 0

        def flush_answer():
            nonlocal current_answer, last_number
            if current_section is None or current_answer is None:
                current_answer = None
                return
            number = current_answer[0]
            text = "\n".join(current_answer[1]).strip()
            if text:
                if any(n == number for n, _ in current_section.answers):
                    current_section.extras.append((number, text))
                else:
                    current_section.answers.append((number, text))
            current_answer = None

        def flush_section():
            nonlocal current_section
            if current_section is None:
                return
            if not _META_SECTION_RE.search(current_section.title):
                book.parsed_sections += 1
                if current_section.answers or current_section.wordbank:
                    book.sections.append(current_section)
            current_section = None

        for line in lines:
            header = re.match(r"^(#{1,6})\s+(.*)$", line)
            if header:
                level, title = len(header.group(1)), header.group(2).strip()
                if level == 1:
                    flush_answer()
                    flush_section()
                    parent_section = None
                    unit_match = _UNIT_RE.search(title)
                    if unit_match:
                        current_unit = int(unit_match.group(1))
                    elif current_unit is None:
                        current_unit = 1
                    continue
                if level == 2:
                    flush_answer()
                    flush_section()
                    parent_section = KnowledgeBase._parse_section_title(title, current_unit)
                    current_section = parent_section
                    last_number = 0
                    continue
                if level == 3:
                    flush_answer()
                    flush_section()
                    # 两级写法：H2 只是页面，H3 才是任务；H2 名整段留作页面名
                    page = (_strip_section_prefix(_strip_part(parent_section.title))
                            if parent_section else "")
                    current_section = KbSection(
                        title=f"{parent_section.title} · {title}" if parent_section else title,
                        unit=parent_section.unit if parent_section else current_unit,
                        part=parent_section.part if parent_section else None,
                        page=page,
                        task=title,
                    )
                    last_number = 0
                    continue
                continue

            if current_section is None:
                continue

            fp_match = _FINGERPRINT_RE.match(line)
            if fp_match:
                flush_answer()
                current_section.fingerprint = [
                    token for token in re.split(r"[|/、,，;；\s]+", fp_match.group(1))
                    if token
                ]
                continue

            bank = _WORDBANK_RE.match(line)
            if bank:
                flush_answer()
                words = re.split(r"[/|,，、;；]+", bank.group(1))
                cleaned: List[str] = []
                for word in words:
                    word = _clean_bank_entry(word)
                    # 整行都是中文说明（如「（截图未包含词库，无法转录）」）时洗不出词，
                    # 此时不能把它当成词库，否则「以词库验明小节」会把正确小节误判成
                    # 找错小节，把本来答得对的题库答案退回 AI。
                    if word and not _PLACEHOLDER_BANK_RE.search(word):
                        cleaned.append(word)
                current_section.wordbank = cleaned
                continue

            answer_match = _ANSWER_RE.match(line)
            if answer_match:
                number = int(answer_match.group(1))
                # 只有「顺延」或「重新从 1 开始」才当作新答案，避免把正文里的
                # 编号列表/续行（如「参考 Step 3」）误切成答案。
                if number == last_number + 1 or (number == 1 and last_number >= 1):
                    flush_answer()
                    current_answer = [number, [answer_match.group(2)]]
                    last_number = number
                    continue

            if current_answer is not None:
                if line.strip():
                    current_answer[1].append(line.strip())
                continue

        flush_answer()
        flush_section()
        return book

    @staticmethod
    def _parse_section_title(title: str, unit: Optional[int]) -> KbSection:
        """拆 'Reading 1 · 1-2 Get ready to read · Listening for information'。

        取含数字编号的那段作为 <编号><页面名>，其后的段作为任务名（可能有多段）。
        标题里写着「Unit N」时以标题的编号为准，传入的 unit 只作兜底
        （收割块标题「浏览器核对收录（Unit 3）」就靠这条才能挂对单元）。
        """
        segments = [seg.strip() for seg in title.split("·") if seg.strip()]
        part = None
        page = ""
        task_segments: List[str] = []

        for index, segment in enumerate(segments):
            found = _part_of(segment)
            if found and part is None:
                part = found
                rest = _strip_section_prefix(segment)
                if rest:
                    # 「1-6 Read and practice」这种编号与页面名同段
                    page = _strip_part(rest)
                    task_segments = segments[index + 1:]
                else:
                    # 「Section A」这种只写编号，下一段才是页面名
                    page = segments[index + 1] if len(segments) > index + 1 else ""
                    task_segments = segments[index + 2:]
                break

        if part is None:
            # 没有编号（少数小节），整串当名字处理
            page = segments[0] if segments else title
            task_segments = segments[1:]

        section_unit = unit
        if part:
            try:
                section_unit = int(part.split("-")[0])
            except (ValueError, IndexError):
                pass
        # 标题里明写「Unit N」时以标题为准：收割块标题「浏览器核对收录（Unit 3）」是
        # 追加在文件末尾的，这时传入的 unit 只是「文件里最后一个 Unit」，照抄会把收录
        # 的答案挂到别的单元上，整块被 Unit 硬过滤挡掉（收割因此形同失效）。
        title_unit = _UNIT_RE.search(title or "")
        if title_unit:
            section_unit = int(title_unit.group(1))
        elif "浏览器核对收录" in (title or ""):
            # 收割块标题里没有可解析的 Unit（收录时拿不到）→ 不能沿用「文件里最后
            # 一个 Unit」的兜底值，那会把答案挂到别的单元上；标成未知交给匹配器裁决。
            section_unit = None

        return KbSection(
            title=title,
            unit=section_unit,
            part=part,
            page=page,
            task=" · ".join(task_segments),
        )

    # -- 教材识别 ---------------------------------------------------------

    def prepare(self, driver: Any = None) -> Optional[str]:
        """在进入任务前解析当前教材，成功则缓存；失败则本次全部走 AI。"""
        self._book = None
        self._resolve_note = ""
        self._resolve_source = ""
        if not self.enabled:
            self._resolve_note = "知识库已关闭"
            return None
        if not self.books:
            self._resolve_note = f"知识库为空（{self.root}）"
            return None
        self._book = self._resolve_book(driver)
        if self._book is None:
            if not self._resolve_note:
                self._resolve_note = (
                    "未识别出教材，本次全部交给 AI"
                    f"（可在 config.json 里把 knowledge_textbook 设为教材名，"
                    f"现有：{'、'.join(self.available_books())}）"
                )
            self._log(self._resolve_note)
        else:
            source = getattr(self, "_resolve_source", "") or "未知"
            self._log(f"教材命中：{self._book.name}（{len(self._book.sections)} 个小节；依据：{source}）")
        return self._book.name if self._book else None

    def _book_is_current(self, driver: Any) -> bool:
        """缓存下来的教材还是当前这个页面的教材吗？

        教材原先只在启动时认一次就缓存到底。同一个程序里换课程（例如从读写教程3
        切到读写教程1）缓存不失效，就会一直拿另一本书的小节去匹配：正确小节根本
        没进候选，整页被判成「无法验证」退回 AI——账号2 的选词填空就是这么答错的。

        这里只做便宜的复核：页面 cid / URL / 标题里已经明确写着另一本教材就重认，
        拿不到新证据（例如停在目录页、加载中）就不动，避免把正常页面误判成换书。
        cid 是事实级证据：它指向另一本书时，连「用户写死的教材」也必须重认。
        """
        if self._book is None:
            return False
        if driver is None:
            return True
        url = self._safe(lambda: driver.current_url) or ""
        cid_name = self._cid_book_name(url)
        if cid_name:
            found = self._match_book_by_name(cid_name)
            return found is not None and found.name == self._book.name
        pref = (self.textbook_pref or "").strip()
        if pref and pref.lower() != "auto":
            return True  # 用户写死了教材，且页面没有 cid 反证，不重认
        title = self._safe(lambda: driver.title) or ""
        for probe in (url, self._unquote(url), title):
            if not probe:
                continue
            found = self._match_book_by_code(probe) or self._match_book_by_name(probe)
            if found is not None:
                return found.name == self._book.name
        return True

    def _resolve_book(self, driver: Any) -> Optional[KbBook]:
        self._resolve_source = ""
        pref = self.textbook_pref
        pref_is_set = bool(pref and pref.lower() != "auto")

        url = ""
        if driver is not None:
            url = str(self._safe(lambda: driver.current_url) or "")

        # 0) 课程页 cid（URL 里 cid=数字 → course_map 人工核对的映射）＝ 页面级事实。
        #    优先级高于配置：在视听说3 的页面上，哪怕 config 里还记着上次认出的
        #    读写教程1，也绝不能拿读写教程1 的小节去匹配（同系列串书就是这么发生的）。
        cid_name = self._cid_book_name(url)
        if cid_name:
            book = self._match_book_by_name(cid_name)
            if book:
                self._resolve_source = "页面课程id"
                if pref_is_set and _normalize(pref) != _normalize(book.name):
                    self._log(f"页面课程 id 指向「{book.name}」，本次覆盖 config 里的教材「{pref}」")
                return book
            self._resolve_note = (
                f"页面课程是「{cid_name}」，但知识库里没有这本书"
                f"（现有：{'、'.join(self.available_books())}）"
            )
            return None

        # 1) 配置里写死的教材名（页面没有 cid 证据时才生效）
        if pref_is_set:
            book = self._match_book_by_name(pref)
            if book:
                self._resolve_source = "配置指定"
                return book
            self._resolve_note = f"配置的教材「{pref}」不在知识库中"

        if driver is None:
            return None

        # 2) 课程页 URL / hash 里的教材代码（如 nv4_rw_3 / nhce_v4_ls_3 / nce_4_rw_3）
        book = self._match_book_by_code(url)
        if book:
            self._resolve_source = "URL 教材代码"
            return book

        # 3) URL、页面标题里的教材名
        title = self._safe(lambda: driver.title) or ""
        for text in (self._unquote(url), title):
            book = self._match_book_by_name(text)
            if book:
                self._resolve_source = "URL/标题教材名"
                return book

        # 4) 页面上可见的课程名/头部文字
        header_texts = self._header_elements(driver)
        for element in header_texts:
            book = self._match_book_by_name(element)
            if book:
                self._resolve_source = "页头文字"
                return book

        # 5) 兜底：整页可见文字里找教材名。
        #    不依赖任何 CSS 选择器 —— 前四步都靠猜元素的类名，U校园 改版或换成
        #    另一套页面（如课程目录页）就会全部落空，而整页文字里通常写着教材全称。
        page_text = self._page_text(driver)
        book = self._match_book_by_name(page_text)
        if book:
            self._resolve_source = "整页文字"
            return book

        # 全部落空：把证据记下来，方便用户判断该往 config 里填哪本书
        self._resolve_note = (
            "未识别出教材，本次全部交给 AI。"
            f"已尝试：URL={url[:80]!r} 标题={title[:60]!r} "
            f"头部文字={header_texts[:3]} 整页文字={len(page_text)} 字。"
            f"可在 config.json 里把 knowledge_textbook 设为教材名，"
            f"现有：{'、'.join(self.available_books())}"
        )
        return None

    def _page_text(self, driver: Any, limit: int = 60000) -> str:
        """整页可见文字（截断），用于兜底识别教材。

        页面文字可能很长（课程列表页含多门课），所以按知识库书目逐个找最长匹配，
        由 _match_book_by_name 负责取最具体的那本。
        """
        text = self._safe(lambda: driver.execute_script(
            "return (document.body && document.body.innerText) || '';")) or ""
        return re.sub(r"\s+", " ", str(text))[:limit]

    @staticmethod
    def _safe(func):
        try:
            return func()
        except Exception:
            return None

    @staticmethod
    def _unquote(text: str) -> str:
        try:
            from urllib.parse import unquote

            return unquote(text or "")
        except Exception:
            return text or ""

    @staticmethod
    def _cid_book_name(url: str) -> str:
        """URL 里 cid 对应的教材名（course_map 是人工核对过的映射）；取不到返回空串。

        未收录的 cid 不硬猜 —— 返回空串让调用方继续走普通识别。
        """
        if not url:
            return ""
        try:
            from course_map import book_of
            return str(book_of(str(url)) or "")
        except Exception:
            return ""

    def _header_elements(self, driver: Any, limit: int = 25) -> List[str]:
        """抓取页面上最可能是「教材名/课程名」的短文本。"""
        selectors = [
            "[class*='course-name']", "[class*='courseName']",
            "[class*='textbook']", "[class*='book-name']",
            "[class*='course-title']", "[class*='courseTitle']",
            "[class*='unit-title']", "[class*='header-title']",
            "h1", "h2", "[class*='breadcrumb']",
        ]
        texts: List[str] = []
        for selector in selectors:
            try:
                elements = driver.find_elements("css selector", selector)
            except Exception:
                continue
            try:
                candidates = list(elements)[:limit]
            except TypeError:
                continue
            for element in candidates:
                text = self._safe(lambda el=element: el.text) or ""
                text = text.strip()
                if text and len(text) < 120:
                    texts.append(text)
        return texts

    def _match_book_by_code(self, text: str) -> Optional[KbBook]:
        """按课程页里的教材代码认书。

        同一模块代码在不同系列里含义不同（新编 rw=综合教程、新视野 rw=读写教程），
        所以先按系列取候选名称列表，再逐个去知识库里试；试不着就换下一个候选。
        """
        for match in _COURSE_CODE_RE.finditer(self._unquote(text) or ""):
            series_code, module_code, number = (
                match.group(1).lower(), match.group(3).lower(), match.group(4),
            )
            series = _SERIES_CODES.get(series_code)
            if not series:
                continue
            table = _MODULE_CODES.get(series, _DEFAULT_MODULE_CODES)
            names = table.get(module_code) or _DEFAULT_MODULE_CODES.get(module_code) or []
            for name in names:
                book = self._match_book_by_name(f"{series} {name}{int(number)}")
                if book:
                    return book
        return None

    def _match_book_by_name(self, text: str) -> Optional[KbBook]:
        """把任意文本里的教材名与知识库书目配对（剥离版本词后互相包含）。

        打分用「实际重叠的字数」而不是书名的全长：页面只写着系列名
        （如「新视野大学英语」）时，同系列每本书的重叠都一样多 —— 这种时候
        必须认不出（返回 None 交给 AI）。谁字长就选谁，会把读写/视听说串起来。
        """
        haystack = _normalize(text)
        if not haystack:
            return None

        best: Optional[KbBook] = None
        best_overlap = 0
        tie = False
        for book in self.books:
            needle = _normalize(book.name)
            if not needle:
                continue
            if needle in haystack:
                overlap = len(needle)
            elif haystack in needle:
                overlap = len(haystack)
            else:
                continue
            if overlap > best_overlap:
                best, best_overlap, tie = book, overlap, False
            elif overlap == best_overlap:
                tie = True
        if tie:
            # 证据一样强：说不清是哪一本就不猜（宁缺勿错，交回 AI）
            return None
        return best

    # -- 检索 -------------------------------------------------------------

    def lookup(
        self,
        questions: Sequence[Any],
        driver: Any = None,
        tab_name: str = "",
        directions: str = "",
        unit: Optional[int] = None,
        prefer_part: Optional[str] = None,
    ) -> Tuple[List[Tuple[Any, str]], List[Any]]:
        """查出命中知识库的题目。

        返回 (hits, misses)：hits 是 (题目, 交给执行器的答案文本) 列表，
        misses 是需要继续交给大模型的题目。

        unit 是当前 Unit 序号（1 起）。知识库里同名小节跨 Unit / 跨 Section 很常见，
        传进来才能把它们区分开；给不出时会交给匹配逻辑自行拒绝。
        prefer_part 是「猜的 Section 编号」（选修/必修推出来的），只在名字已经认出来的
        前提下用来打破 Section A 与 Section B 的同名平局，不参与硬过滤。
        """
        misses = list(questions)
        if not self.enabled or not questions:
            return [], misses

        if not self._book_is_current(driver):
            previous = self._book.name if self._book is not None else None
            self._book = self._resolve_book(driver)
            if self._book is not None and previous and self._book.name != previous:
                self._log(f"教材换了：{previous} → {self._book.name}，已按新教材重查")
            if self._book is None:
                return [], misses

        harvest_only = False
        if not self._book.answer_ready:
            # 书不可逐题作答（多半是散文转录）≠ 整本没用：里面「浏览器核对收录」区
            # 的答案是浏览器确认全对后收录的。方案B：此时只尝试收割区，命中就用，
            # 未命中再交给 AI —— 收录进题库的答案在重做同题时能真正命中。
            harvest_only = True
            self._log(
                f"{self._book.name} 不是可逐题取用的答案表 → "
                "仅尝试「浏览器核对收录」区已验证的答案"
            )

        answerable = [q for q in questions if self._type_name(q) in ANSWERABLE_TYPES]
        if not answerable:
            return [], misses

        page_header = self._page_header_text(driver)
        if unit is None:
            unit = self.detect_unit(driver, page_header)
        section, confidence, reason = self._match_section(
            answerable, tab_name, directions, page_header, unit,
            prefer_part=prefer_part, harvest_only=harvest_only
        )
        if section is None:
            self._log(f"未匹配到小节（{tab_name or page_header or '未知小节'}）→ 全部交给 AI")
            return [], misses

        self._log(
            f"小节命中：{section.title}"
            f"（置信度 {confidence}{'，' + reason if reason else ''}，"
            f"{len(section.answers)} 个答案）"
        )

        hits: List[Tuple[Any, str]] = []
        for question in answerable:
            answer_text = self._assemble_answer(section, question)
            if not answer_text:
                continue
            if not self._passes_wordbank_check(section, question):
                self._log(f"第 {question.number} 题无法用页面词库验证，转交 AI 复核")
                continue
            hits.append((question, answer_text))

        hit_ids = {id(q) for q, _ in hits}
        misses = [q for q in questions if id(q) not in hit_ids]
        self._report_hits(section, hits, len(questions))
        return hits, misses

    def detect_unit(self, driver: Any, page_header: str = "") -> Optional[int]:
        """从页面上读当前 Unit 序号（「快速处理当前页」没有扫描结果可用）。

        Unit 只信「选中的那个标签」：unitTab 选择器会命中整个 Tab 容器，容器全文里
        写着所有 Unit 的标题，从前那种「取全文第一个 Unit N」的读法在选中态失效时
        会把当前 Unit 永远读成全文里的第一个（多为 Unit 1）—— 选错 Unit 就会拿别
        单元的小节答案来填。退回读容器全文时，只有编号唯一才敢用，否则返回 None。
        """
        if driver is None and not page_header:
            return None
        active_text = page_header
        container_text = ""
        if driver is not None:
            selectors = (
                ("[class*='unitTab'] [class*='active']", False),
                ("[class*='unit-tab'] [class*='active']", False),
                # 容器选择器：命中的是整个 Unit Tab，全文含所有 Unit 的标题
                ("[class*='unitTab']", True),
            )
            for selector, is_container in selectors:
                try:
                    elements = driver.find_elements("css selector", selector)
                except Exception:
                    continue
                try:
                    candidates = list(elements)[:8]
                except TypeError:
                    continue
                pieces: List[str] = []
                for element in candidates:
                    piece = self._safe(lambda el=element: el.text) or ""
                    if piece:
                        pieces.append(piece)
                if not pieces:
                    continue
                if is_container:
                    container_text = f"{container_text} {' '.join(pieces)}"
                else:
                    active_text = f"{active_text} {' '.join(pieces)}"

        # 选中标签里的 Unit 编号唯一才采信（调用方给的页头文本一并算进来）
        active_numbers = {int(value) for value in _UNIT_RE.findall(active_text or "")}
        if len(active_numbers) == 1:
            return active_numbers.pop()
        # 回退读容器全文：全文里是所有 Unit 的标题，编号唯一才敢用，宁缺勿错
        container_numbers = {int(value) for value in _UNIT_RE.findall(container_text)}
        if len(container_numbers) == 1:
            return container_numbers.pop()
        if container_numbers:
            self._log(
                f"页面上读到 {len(container_numbers)} 个 Unit 编号（"
                f"{'/'.join(str(n) for n in sorted(container_numbers))}），读不到选中态，"
                "无法确定当前 Unit → 交回 AI"
            )
        return None

    @staticmethod
    def _type_name(question: Any) -> str:
        q_type = getattr(question, "q_type", None)
        return getattr(q_type, "name", "") or ""

    def _match_section(
        self,
        questions: Sequence[Any],
        tab_name: str,
        directions: str,
        page_header: str = "",
        unit: Optional[int] = None,
        prefer_part: Optional[str] = None,
        harvest_only: bool = False,
    ) -> Tuple[Optional[KbSection], str, str]:
        book = self._book
        if book is None:
            return None, "low", ""

        page_wordbank = self._page_wordbank(questions)
        expected_count = self._expected_answer_count(questions)
        part = _part_of(tab_name) or _part_of(page_header) or _part_of(directions)
        # 任务名可能出现在页头、Tab 名或题目指示里，合并成一份「身份文本」来判定
        identity_norm = _normalize(" ".join(x for x in (page_header, tab_name, directions) if x))
        context_tokens = _tokens(tab_name) | _tokens(page_header) | _tokens(directions)
        # 题干/页面文字的词（题目指纹核对用）：任务名与题目要求之外，题目自己的
        # 正文（陈述句、原文、表格词条）最能代表这道题；多选题的辨识信息几乎全在
        # 选项里（题干常只有「The expressions:」），收录指纹也会记选项词，两边必须
        # 一致提取，指纹才核对得上
        question_tokens: Set[str] = set()
        for q in questions:
            question_tokens |= _tokens(str(getattr(q, "text", "") or ""))
            for option in (getattr(q, "options", None) or []):
                question_tokens |= _tokens(str(getattr(option, "text", "") or ""))

        scored: List[Tuple[int, int, KbSection, List[str]]] = []
        for section in book.sections:
            # 编号或 Unit 对不上就直接排除。这两条是硬条件：
            # 同名小节跨 Unit / 跨 Section 极常见，只靠名字打分会让别的小节反超。
            if part and section.part and section.part != part:
                continue
            if unit is not None and section.unit is not None and section.unit != unit:
                continue
            # 「仅收割区」模式（书不可逐题作答时）：只考虑「浏览器核对收录」里
            # 页面确认过全对的答案，其余小节（散文转录等）一概不看。
            if harvest_only and not _is_harvest_section(section):
                continue
            # 收割块指纹只剩 1 个词 = 收录时想记身份却没记成（实测「passage」这种
            # 标题词在别的页面 100% 撞车、白拿「题目指纹一致」，把别题的答案 C 填进
            # 了这道多选）。这种块一律不参与匹配 —— 宁可交回 AI 重答；答对后会被
            # 重新收录，新指纹带上选项词，那时才具备复用资格。
            # 注意只管「有指纹但不足 2 词」：完全没有指纹的是指纹功能上线前的旧块
            # （读写教程1 的 Quiz / Words in use 等），它们一直靠任务名+条数匹配，
            # 停用会把在用的一批已验证答案一起废掉，不牵连。
            if _is_harvest_section(section) and 0 < len(section.fingerprint) < 2:
                continue
            # 必修/选修推出来的 Section 结论是「互斥」的，要当硬条件用：
            #   必修 = Section A  → 排除 Section B
            #   选修 = Section B/C → 排除 Section A
            # 少了这条，「选修 - Banked cloze」会被 Section A 的 Banked cloze 抢走
            # （它的任务名恰好逐字相同），把 A 的答案填到 Unit test 页面上。
            #
            # 但不排除 Section C：必修/选修说的是 Text A / Text B，Section C（Stories of
            # China、Translation）不分必修选修，被当成「推断出的 A」排除掉会让这些任务
            # 永远认不出来（读写教程1/3/4 的 Section C 整整一批都取不到）。C 与 B 同名时
            # 交给下面的「同名任务横跨多个 Section」判定去拒绝，不靠猜。
            # 页面自己写着 Section X 时（part 不是 None），页面证据强于推断，不适用这条。
            if prefer_part and part is None:
                if prefer_part == "A" and section.part == "B":
                    continue
                if prefer_part == "B" and section.part == "A":
                    continue

            score = 0
            positive = 0
            notes: List[str] = []

            if part and section.part == part:
                score += 100
                positive += 1
                notes.append("编号一致")

            # 页面上显示的小节名与知识库逐字一致，是最可靠的锚点。
            # 两级写法下页面名（Section A · Reading comprehension）和任务名
            # （Understanding the text）都要对上才算认出这一节。
            page_norm = _normalize(section.page)
            task_norm = _normalize(section.task)
            page_hit = bool(page_norm) and len(page_norm) >= 4 and page_norm in identity_norm
            task_hit = bool(task_norm) and len(task_norm) >= 4 and task_norm in identity_norm
            # 知识库的任务名常带前缀（『Collocation · Practicing』『Unit test · Banked cloze』），
            # 页面上只显示最后一段。退一步认后缀，分数给得低一些。
            # 任务名的尾段：知识库里常写成「Pre-reading activities: Task 1」
            # 「Collocation · Practicing」这类，页面只显示最后那一小段。
            task_tail_norm = ""
            if section.task:
                tail = re.split(r"[·:：]", section.task)[-1].strip()
                task_tail_norm = _normalize(tail)
                if len(task_tail_norm) < 4:
                    words = re.split(r"\s+", tail)
                    task_tail_norm = _normalize(" ".join(words[-2:]))
            tail_hit = (bool(task_tail_norm) and len(task_tail_norm) >= 4
                        and task_tail_norm in identity_norm)
            # 任务名的首段：知识库常写成「Paragraph translation · Task 1」，而页面上
            # 那一栏就叫「Paragraph translation」（Task 1 是页内的子标签）。只认尾段的话
            # 这类小节永远拿不到任务名证据，只好整页退回 AI。
            task_head_norm = ""
            if section.task:
                head = re.split(r"[·:：]", section.task)[0].strip()
                task_head_norm = _normalize(head)
                if len(task_head_norm) < 4:
                    words = re.split(r"\s+", head)
                    task_head_norm = _normalize(" ".join(words[:2]))
            head_hit = (bool(task_head_norm) and len(task_head_norm) >= 4
                        and task_head_norm in identity_norm)
            if page_hit:
                score += 90
                positive += 2
                notes.append("页面名一致")
            elif section.page:
                # 页名尾段一致算弱佐证：知识库写作「Unit review · Unit test」，
                # 而页面上只显示「Unit test」（面包屑）。没有这条，Unit test 里的
                # Banked cloze 会被 Section A 的同名小节压成平局，整页退回 AI。
                page_tail = _normalize(re.split(r"[·:：]", section.page)[-1])
                if len(page_tail) >= 5 and page_tail in identity_norm:
                    score += 40
                    positive += 1
                    notes.append("页面名尾段一致")
            if task_hit:
                score += 90
                positive += 2
                notes.append("任务名一致")
            elif tail_hit:
                score += 55
                positive += 1
                notes.append("任务名后缀一致")
            elif head_hit:
                # 前缀只给弱证据：栏名是 Task 1 / Task 2 共用的
                # （「Paragraph translation」两个 Task 都有），
                # 只有尾段（选中的 Task 1）才分得开它们。
                score += 25
                positive += 1
                notes.append("任务名前缀一致")

            # 答案条数与页面上要填的条数一致，是「就是这一节」的硬证据：
            # 同一 Unit 里重名的小节（Section A/B 各有一份、Collocation 与 Reading skills
            # 都叫 Practicing）靠它才能分开。
            if expected_count and len(section.answers) == expected_count:
                score += 60
                positive += 2
                notes.append("答案条数与页面一致")

            shape = self._answer_shape_fits(section, questions)
            if shape is True:
                score += 40
                positive += 1
                notes.append("答案形态与题型相符")
            elif shape is False:
                score -= 60
                notes.append("答案形态与题型不符")

            overlap = context_tokens & section.keywords()
            if overlap:
                score += 12 * min(len(overlap), 3)
                positive += 1
                notes.append("关键词 " + "/".join(sorted(overlap)[:3]))

            if page_wordbank:
                if section.wordbank:
                    ratio = self._jaccard(page_wordbank, section.wordbank)
                    if ratio >= 0.6:
                        score += 60
                        positive += 2
                        notes.append(f"词库一致 {ratio:.0%}")
                    elif ratio >= 0.3:
                        score += 25
                        notes.append(f"词库部分一致 {ratio:.0%}")
                    else:
                        # 页面上有词库、小节词库却对不上 —— 同一编号下多半是另一个小节
                        score -= 30
                        notes.append("词库不符")
                else:
                    # 该小节没有词库可比，就只能靠名字认定；名字也没认全就不算数
                    if not (page_hit and task_hit) and not part:
                        score -= 40
                        notes.append("该小节无词库且名字未认全")

            # 题目指纹核对：收录块里存着「这道题的实词」。一半以上能在页面/题干里
            # 找到 → 这套答案就是这道题的（重加权）；一个都对不上 → 多半是同名的
            # 另一道题（重扣分）。同名同空数的小节从此靠它精确区分。
            # 单词指纹不算数：一个词在别的页面撞上太容易（实测某块指纹只有 1 词，
            # 在另一道含同一词的题上 100% 重合、白拿 80 分把错误答案顶成了第一）
            if section.fingerprint:
                overlap_fp = sum(
                    1 for t in section.fingerprint
                    if t in context_tokens or t in question_tokens)
                fp_ratio = overlap_fp / len(section.fingerprint)
                if len(section.fingerprint) >= 2 and fp_ratio >= 0.5:
                    score += 80
                    positive += 2
                    notes.append("题目指纹一致")
                elif overlap_fp == 0 and len(section.fingerprint) >= 3:
                    score -= 80
                    notes.append("题目指纹不符")

            if score > 0:
                scored.append((score, positive, section, notes))

        if not scored:
            self._log(f"没有任何小节进入候选（{tab_name or page_header or '未知小节'}）")
            return None, "low", ""

        def _describe(item) -> str:
            score, _positive, candidate, candidate_notes = item
            return f"{candidate.title}[{score}分 {'、'.join(candidate_notes) or '无佐证'}]"

        # 一个小节名是另一个的前缀时（知识库里 Critical thinking 与 Critical thinking skill
        # 并存于同一页面下），页面文字会同时匹配上两者。此时更具体的那个才对：
        # 给「名字是别人前缀」的候选扣分，让具体的那节胜出。
        matched_task_names = [
            _normalize(item[2].task) for item in scored if "任务名一致" in item[3]
        ]

        # 必修/选修只能确认到「组」：必修 = Section A，选修 = Section B 或 C。
        # 名字命中的候选里若只出现一个 Section 字母，才敢用它当证据；B 与 C 同时出现时
        # （新视野的 Sentence structure · Task 1 与 Stories of China · Understanding · Task 1
        # 都叫 Task 1）说明字母本身分不开，此时不加分，交给平局判定去拒绝 —— 加错分会把
        # B 的答案填到 C 的页面上。
        name_hit_parts = {
            item[2].part for item in scored
            if ("任务名一致" in item[3] or "任务名后缀一致" in item[3]
                or "任务名前缀一致" in item[3]) and item[2].part
        }
        hint_target = None
        if prefer_part and len(name_hit_parts) <= 1:
            hint_target = next(iter(name_hit_parts), prefer_part)

        adjusted = []
        for item in scored:
            score, positive, section, notes = item
            name = _normalize(section.task)
            if name and any(len(other) > len(name) and name in other
                            for other in matched_task_names):
                score -= 30
                notes = notes + ["名字是更具体小节的前缀"]
            if (hint_target and section.part == hint_target
                    and ("任务名一致" in notes or "任务名后缀一致" in notes
                         or "任务名前缀一致" in notes)):
                score += 45
                positive += 1
                notes = notes + ["必修选修与 Section 编号一致"]
            adjusted.append((score, positive, section, notes))
        scored = adjusted

        scored.sort(key=lambda item: item[0], reverse=True)
        score, positive, section, notes = scored[0]

        # 重复编号小节（知识库把 Step 1 / Step 2 两套答案写在同一小节里）没法确定页面
        # 用的是哪一套 —— 组装答案只会取到第一套，页面若恰是第二套就整套填错。
        # 保守拒绝这一节而不换下一个候选（别的小节未必对），整页交回 AI。
        if section.extras:
            self._log(
                f"小节 {section.title} 存在重复编号答案（{len(section.extras)} 条），"
                "无法确定用哪套，交回 AI"
            )
            return None, "low", "存在重复编号答案"

        # 认出的这一节必须与其它候选拉开差距，否则就是没认准。
        # 注意这里不因为「名字认出」就放行：同名小节在知识库里到处都是
        # （1-4 下有 Global/Detailed understanding；新视野里 Section A/B 各有同名任务，
        # 不同 Unit 之间更是整节同名），分数并列时按排序随便选一个就会整节填错。
        # 真正能消歧的是 Unit 号与编号，它们已在上面作为硬条件过滤掉了别的小节。
        decisive = (
            # 「词库一致」这条证据写成「词库一致 60%」（带比例），只能用前缀比对 ——
            # 原来直接 in 判断恒为假，这条最硬的证据从来没有生效过。
            any(note.startswith("词库一致") for note in notes)
            or ("页面名一致" in notes and ("任务名一致" in notes
                                           or "任务名后缀一致" in notes
                                           or "任务名前缀一致" in notes))
            or (part is not None and section.part == part and "任务名一致" in notes and not section.page)
            or ("答案条数与页面一致" in notes
                and ("任务名一致" in notes or "任务名后缀一致" in notes
                     or "任务名前缀一致" in notes))
            # 收录块的页面名永远是「浏览器核对收录」、拿不到「页面名一致」；
            # 指纹一致 + 任务名一致即认定就是这道题（条数缺失/不符时靠指纹兜底，
            # 实测收录的块因条数差一位被卡在 low < medium，收录了却不被调用）
            or ("题目指纹一致" in notes
                and ("任务名一致" in notes or "任务名后缀一致" in notes
                     or "任务名前缀一致" in notes))
            or ("必修选修与 Section 编号一致" in notes
                and ("任务名一致" in notes or "任务名后缀一致" in notes
                     or "任务名前缀一致" in notes))
            # 页面名较长且逐字一致（『Translation』『Word building』这类），本身就算认出来了 ——
            # 知识库的页名就是从 U校园 页面上抄下来的，逐字对上说明页面对得上。
            # 但只有在候选里只有一个命中该页名时才敢用：『Read and practice』这类页名下
            # 挂着十几个小节，谁都能命中页名，那时页名等于没提供信息。
            or ("页面名一致" in notes and len(_normalize(section.page)) >= 8
                and sum(1 for item in scored if "页面名一致" in item[3]) == 1)
        )
        close = [item for item in scored if item[0] >= score - 20 and item[2] is not section]
        if close:
            # 用户策略（2026-09-24）：只要答案与题目「对得上」（条数一致、形态相符、
            # 词库不冲突），就先用题库这一节的答案；提交后分数不达标再由 AI 重做，
            # 重做达标了还会把答案收进题库。所以这里不再一见分数接近就放弃 ——
            # 只有当「对得上」都不成立时才拒绝。
            STRONG_NOTES = ("任务名一致", "任务名后缀一致", "任务名前缀一致",
                            "页面名一致", "页面名尾段一致", "词库一致", "题目指纹一致")
            close_notes = {note for item in close for note in item[3]}
            unique_strong = [
                k for k in STRONG_NOTES
                # 「词库一致」写作「词库一致 60%」，前缀比对才能命中（其余证据是整串）
                if any(note.startswith(k) for note in notes)
                and not any(note.startswith(k) for note in close_notes)
            ]
            fits = ("答案条数与页面一致" in notes and "答案形态与题型相符" in notes
                    and "词库不符" not in notes)
            # 用「胜出者独有的那条证据」判：截图那页 Unit test 的 Vocabulary 赢在
            # 任务名后缀、Unit test 的 Banked cloze 赢在页面名尾段 —— 这类并列是同一个
            # 任务的不同小节写法，形态/条数都对得上，直接用。两边证据完全一样
            # （Section A/B 同名小节）时仍是抛硬币，照旧拒绝。
            if fits and unique_strong:
                self._log(
                    f"候选分数接近（{section.title} 与 {close[0][2].title}），"
                    f"但只有它多出证据「{unique_strong[0]}」且答案形态/条数相符 → 先用这一节"
                )
            elif ("题目指纹一致" in notes
                  and all("题目指纹一致" in item[3] for item in close)):
                # 并列候选全都「题目指纹一致」= 同一页被重复收录了几次（指纹来自页面，
                # 相同即同页）。取答案条数最全的一套 —— 几套都曾是页面确认过的答案，
                # 最全的覆盖最完整；这不是在不同页之间猜。
                pool = [scored[0]] + list(close)
                best = max(pool, key=lambda item: len(item[2].answers))
                if best[2].extras:
                    self._log(f"小节 {best[2].title} 存在重复编号答案，交回 AI")
                    return None, "low", "存在重复编号答案"
                score, positive, section, notes = best[0], best[1], best[2], list(best[3])
                self._log(
                    f"同页重复收录（{len(pool)} 套指纹一致），取答案最全的一套："
                    f"{section.title}（{len(section.answers)} 条）")
            elif (all(_normalize(item[2].task) == _normalize(section.task)
                      for item in close)
                  and all(sorted(str(a).strip() for _n, a in item[2].answers)
                          == sorted(str(a).strip() for _n, a in section.answers)
                          for item in close)):
                # 同名小节、答案还一字不差 = 同一页被重复收录（重新收录时指纹变了、
                # 旧块没被认成同页）。几套答案内容完全一样，取最全的一套直接用 ——
                # 不是在不同页之间猜，不算抛硬币（实测汉译英页/Julius 页都踩过：
                # 两个同页块并列 → 整页退回 AI，收录了却调不动）。
                pool = [scored[0]] + list(close)
                best = max(pool, key=lambda item: len(item[2].answers))
                if best[2].extras:
                    self._log(f"小节 {best[2].title} 存在重复编号答案，交回 AI")
                    return None, "low", "存在重复编号答案"
                score, positive, section, notes = best[0], best[1], best[2], list(best[3])
                self._log(
                    f"同页重复收录（{len(pool)} 套同名同答案），取最全的一套："
                    f"{section.title}（{len(section.answers)} 条）")
            else:
                self._log(
                    f"候选小节无法区分（{section.title} 与 {close[0][2].title} 等）→ 交回 AI"
                )
                self._log("并列候选：" + "；".join(_describe(item) for item in scored[:3]))
                return None, "low", "候选小节无法区分"

        # 名字命中的候选横跨多个 Section 时（读写教程4 的 Section B · Sentence structure · Task 2
        # 与 Section C · Stories of China · Exercises · Task 2 都叫 Task 2），必修/选修只能定到
        # 「A」还是「B/C」这一组，组内是 B 还是 C 没有依据 —— 此时「任务名更完整」只说明知识库里
        # 的写法，不能说明页面属于哪个 Section，认下去就会把 B 的答案填到 C 的页面上。
        if prefer_part and len(name_hit_parts) > 1:
            self._log(
                f"同名任务横跨 {sorted(name_hit_parts)} 两个 Section，必修/选修分不开 → 交回 AI"
            )
            self._log("并列候选：" + "；".join(_describe(item) for item in scored[:3]))
            return None, "low", "同名任务横跨多个 Section"

        if part and section.part == part:
            # 除编号外还有任务名/词库/关键词佐证才算高置信
            confidence = "high" if decisive and positive > 1 else "medium"
        elif decisive:
            confidence = "medium"
        else:
            confidence = "low"
            self._log("佐证不足，候选排名：" + "；".join(_describe(item) for item in scored[:3]))

        if _CONFIDENCE_RANK[confidence] < _CONFIDENCE_RANK[self.min_confidence]:
            self._log(
                f"候选小节 {section.title} 置信度 {confidence} 低于门槛 "
                f"{self.min_confidence}，放弃使用"
            )
            return None, confidence, "低于置信度门槛"
        return section, confidence, "、".join(notes)

    @staticmethod
    def _answer_shape_fits(section: KbSection, questions: Sequence[Any]) -> Optional[bool]:
        """答案形态与页面题型是否对得上。对得上 True，明显冲突 False，判断不了 None。

        读写教程里 Section B 的 Sentence structure · Task 1（答案是整句）与 Section C 的
        Understanding · Task 1（答案是 A/B/C）在页面上都叫 Task 1，题目要求也差不多，
        能分开它们的就只有「这题到底是选择题还是简答题」—— 而这正好是可靠信息。

        选择题的答案可以是字母，也可以是词/短语（执行器会按选项正文反查），所以只有
        整句这种明显不可能的才算冲突；反过来简答题/填空题答案写成光秃秃的字母才算冲突。
        """
        types = {KnowledgeBase._type_name(question) for question in questions}
        answers = [text for _, text in section.answers if str(text).strip()]
        if not types or not answers:
            return None

        def is_prose(text: str) -> bool:
            stripped = re.sub(r"^[\d\s.、:：)）+]+", "", str(text)).strip()
            return len(stripped.split()) >= 4

        prose_ratio = sum(1 for a in answers if is_prose(a)) / len(answers)
        letter_ratio = sum(1 for a in answers if _looks_like_choice_answer(a)) / len(answers)

        if types <= CHOICE_TYPES:
            if prose_ratio > 0.6:
                return False
            return True
        if types <= ORDER_TYPES:
            # 排序/配对题的答案本来就是「B D A C」这样的字母序列 —— 这正是它该有的样子
            return True
        if types <= (BLANK_TYPES | TEXT_TYPES):
            if letter_ratio > 0.6:
                # 认人页（下拉选照片编号 A–E）里「答案就是字母」才对；普通选词填空
                # 里出现纯字母答案才是配错了小节 —— 用页面的下拉选项是不是字母来分。
                options = [str(option).strip() for question in questions
                           for option in (getattr(question, "banked_options", None) or [])]
                if options and all(len(item) <= 2 and item.isalpha() for item in options):
                    return True
                if not options and types <= {"DROPDOWN_SELECT"}:
                    # 选项藏在点击才渲染的菜单里 → 抓不到；字母答案对下拉题是合理的，
                    # 不能判「不符」（实测 -60 分把收录好的块压到边缘，用户报「调不动」）
                    return None
                return False
            return True
        return None

    @staticmethod
    def _expected_answer_count(questions: Sequence[Any]) -> int:
        """页面上要填的答案条数：选词/下拉填空按空数，其余按题数。

        知识库一小节的答案条数是固定的，把它和页面比一比，就能把同一 Unit 里
        重名的小节（Section A 与 Section B 各一份、四个都叫 Practicing 的任务）分开。
        """
        total = 0
        for question in questions:
            type_name = KnowledgeBase._type_name(question)
            blanks = getattr(question, "banked_blanks", None) or []
            inputs = getattr(question, "inputs", None) or []
            if type_name in {"BANKED_CLOZE", "DROPDOWN_SELECT"} and blanks:
                total += len(blanks)
            elif type_name in {"FILL_IN", "LISTENING_FILL_IN"} and inputs:
                total += len(inputs)
            else:
                total += 1
        return total

    @staticmethod
    def _page_wordbank(questions: Sequence[Any]) -> List[str]:
        """取页面上选词填空题的词库（用于和知识库比对确认命中）。"""
        words: List[str] = []
        for question in questions:
            if KnowledgeBase._type_name(question) not in {"BANKED_CLOZE", "DROPDOWN_SELECT"}:
                continue
            for option in getattr(question, "banked_options", None) or []:
                text = str(option).strip()
                if text and text not in words:
                    words.append(text)
        return words

    def _page_header_text(self, driver: Any, limit: int = 12) -> str:
        """读页面顶部的小节标题。知识库小节名与 U校园 页面显示逐字一致，这是最准的锚点。"""
        if driver is None:
            return ""
        selectors = [
            # 选中态的真实类名是 pc-header-task-activity / pc-header-tab-activity
            # （不是 active，'active' 不是 'activity' 的子串），少了它们页头就读不到
            # 「Task 1」「Paragraph translation」这些小标签，Task 1 与 Task 2 会并列分不开
            "[class*='pc-header-task-activity']",
            "[class*='pc-header-tab-activity']",
            "[class*='pc-header'] [class*='tab'][class*='active']",
            "[class*='pc-header-tab'][class*='active']",
            "[class*='pc-header'] [class*='title']",
            "[class*='section-title']", "[class*='sectionTitle']",
            "[class*='layout-direction']", ".abs-direction",
            "[class*='task-name']", "[class*='taskName']",
        ]
        texts: List[str] = []
        for selector in selectors:
            try:
                elements = driver.find_elements("css selector", selector)
            except Exception:
                continue
            try:
                candidates = list(elements)[:limit]
            except TypeError:
                continue
            for element in candidates:
                text = self._safe(lambda el=element: el.text) or ""
                text = re.sub(r"\s+", " ", text).strip()
                if text and len(text) < 80 and text not in texts:
                    texts.append(text)
        return " ".join(texts[:limit])

    @staticmethod
    def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
        a = {str(x).strip().lower() for x in left if str(x).strip()}
        b = {str(x).strip().lower() for x in right if str(x).strip()}
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def _passes_wordbank_check(self, section: KbSection, question: Any) -> bool:
        """选词填空题必须验明正身，验不了就交回 AI。

        两条证据，任一条成立即可：
        1. 知识库自带词库，且与页面词库高度重合（新编大学英语走这条）。
        2. 没有词库可比时，用「选词填空的答案必须来自词库」这一事实反查：
           知识库给的答案里有足够比例出现在页面词库中（新视野的转录未给出词库，
           走这条）。

        页面上有词库而两条都不成立，说明找错了小节（同一编号下常有 Word building /
        Banked cloze 等多个小节），此时绝不能把整段编号答案填上去。
        """
        if not self.verify_wordbank:
            return True
        if self._type_name(question) not in {"BANKED_CLOZE", "DROPDOWN_SELECT"}:
            return True
        page_words = [
            str(x).strip() for x in (getattr(question, "banked_options", None) or [])
            if str(x).strip()
        ]
        if not page_words:
            # 页面词库抓不到时的兜底：
            # · 「认人页」这类下拉题，答案本来就是选项照片的编号字母（A–E），而选项
            #   藏在「点击才渲染」的下拉菜单里 → 抓不到很正常。答案是字母的直接放行
            #   （实库与页面双重确认过才收录进来的，别把收录好的答案挡在门外）。
            # · 选词填空（BANKED_CLOZE）或答案是词/短语的，照旧不放行 —— 交回 AI。
            letters_answer = bool(section.answers) and all(
                len(str(text).strip()) <= 3
                and str(text).strip().isalpha()
                and all(ch in "ABCDEFGH" for ch in str(text).strip().upper())
                for _number, text in section.answers
            )
            if self._type_name(question) == "DROPDOWN_SELECT" and letters_answer:
                self._log(
                    f"第 {getattr(question, 'number', '?')} 题为下拉选字母"
                    "（照片编号），页面选项抓不到属正常，按知识库字母作答")
                return True
            self._log(
                f"第 {getattr(question, 'number', '?')} 题页面词库缺失，无法校验，交回 AI"
            )
            return False

        if section.wordbank:
            if self._jaccard(page_words, section.wordbank) >= 0.6:
                return True
            # 自带的词库对不上就不再退而求其次，直接判为找错小节
            return False

        blank_count = len(getattr(question, "banked_blanks", None) or [])
        answers = section.ordered_answers()
        if blank_count:
            answers = answers[:blank_count]
        if not answers:
            return False
        return self._answers_come_from_bank(answers, page_words)

    @staticmethod
    def _answers_come_from_bank(answers: Sequence[str], page_words: Sequence[str]) -> bool:
        """答案是否基本来自给定词库（容忍词形变化与短语切分）。"""
        pool = [_normalize(w) for w in page_words if _normalize(w)]
        if not pool:
            return False
        hits = 0
        for answer in answers:
            needle = _normalize(answer)
            if not needle:
                continue
            if any(needle == word or word in needle or needle in word for word in pool):
                hits += 1
        return hits / len(answers) >= 0.6

    # -- 答案组装 ---------------------------------------------------------

    def _assemble_answer(self, section: KbSection, question: Any) -> str:
        """按题型组装成执行器能识别的答案文本；拿不准就返回空串（转交 AI）。

        一律按题号取答案，不做「按页面顺序对位」的兜底：页面只显示部分题目时，
        对位会静默错位，宁可交回 AI。
        """
        type_name = self._type_name(question)
        number = int(getattr(question, "number", 0) or 0)
        answers = section.answer_map()

        if type_name in CHOICE_TYPES:
            raw = answers.get(number)
            if raw is None:
                return ""
            letters = self._letters_from(raw, question)
            if not letters:
                self._log(f"第 {number} 题答案「{self._shorten(raw)}」无法对应到选项，转交 AI")
                return ""
            if type_name == "MULTIPLE_CHOICE":
                return letters
            # 单选：答案里出现多个字母（知识库里写作 "A; B; C; D"）说明这题实际是多选。
            # 只填第一个字母几乎必错，宁可交回 AI 让页面自己判定题型。
            if len(letters) > 1:
                self._log(
                    f"第 {number} 题答案「{self._shorten(raw)}」含多个选项字母，"
                    "与单选题不符，转交 AI"
                )
                return ""
            return letters[0]

        if type_name in BLANK_TYPES:
            if type_name in {"BANKED_CLOZE", "DROPDOWN_SELECT"}:
                # 这两类的编号是「本题第几个空」，与题号无关，直接给整段编号答案
                ordered = section.ordered_answers()
                if not ordered:
                    return ""
                blank_count = len(getattr(question, "banked_blanks", None) or [])
                if blank_count:
                    if len(ordered) != blank_count:
                        self._log(
                            f"注意：知识库 {len(ordered)} 个答案 / 页面 {blank_count} 个空，"
                            "按顺序取前几项"
                        )
                    # 日志说「按顺序取前几项」就得真的截断：多出来的答案照发会被
                    # 执行器当成额外的空，填到别的空位上去。
                    ordered = ordered[:blank_count]
                return "\n".join(f"{i}. {text}" for i, text in enumerate(ordered, 1))
            count = len(getattr(question, "inputs", None) or []) or 1
            sequence = self._sequential_answers(section, number, count)
            if not sequence:
                return ""
            if len(sequence) == 1 and count > 1:
                # 旧收录块的合并串：自带 1. 2. 3. 编号，必须原样交回、由执行器按
                # 编号拆空 —— 重新加题号会变成「1. 1. x 2. y」，空 1 会被内层编号吃掉
                return sequence[0]
            return "\n".join(f"{i}. {text}" for i, text in enumerate(sequence, 1))

        if type_name in TEXT_TYPES | ORDER_TYPES:
            if type_name in ORDER_TYPES:
                # 配对/排序题的答案在题库里按 1) 2) 3) 4) 分开存，必须拼成完整序列。
                # 以前只取第一条（answers.get(number)），配对题就只拿到一个字母 'B'，
                # 拖动自然出错 —— 这次一并修掉。
                sequence = section.ordered_answers()
                joined = " ".join(part.strip() for part in sequence if part.strip())
                letters = [ch for ch in joined.upper() if ch in "ABCDEFGH"]
                expected = len(getattr(question, "options", None) or [])
                if expected >= 2 and len(letters) < expected:
                    self._log(
                        f"题库这一节的答案（{joined[:30]!r}）不足 {expected} 个字母，"
                        "判定为配错了小节，交回 AI/视觉复核")
                    return ""
                return joined
            raw = answers.get(number)
            if not raw:
                return ""
            return f"{number}. {raw}"

        return ""

    def _sequential_answers(self, section: KbSection, start: int, count: int) -> List[str]:
        """按题号取连续 count 个答案（填空题一个容器可能含多个空）。

        兼容旧收录块：整页答案曾被压成 1 条合并串（「1. x 2. y …」），凑不齐
        count 条时，若第一条本身带 ≥2 个编号行，就把它原样交回 —— 执行器的
        _parse_banked_answer 会按编号拆进各个空。真缺答案的小节仍返回空（交回 AI），
        绝不拿半套答案硬填。
        """
        answers = section.answer_map()
        out: List[str] = []
        for offset in range(count):
            text = answers.get(start + offset)
            if not text:
                break
            out.append(text)
        if len(out) >= count:
            return out
        first = answers.get(start)
        # 合并串特征与执行器 NUMBER_PREFIX_PATTERN 对齐：既认「1. x 2. y」也认
        # 「空1：x 空2：y」（实测旧块两种形态都有）
        if first and len(re.findall(
                r"(?:^|\s)(?:空\s*)?\d{1,3}\s*[.、:：)）]\s*\S", str(first))) >= 2:
            return [str(first)]
        return []

    def _letters_from(self, raw: str, question: Any) -> str:
        """把知识库里的选择题答案转成选项字母，必要时按选项正文反查。

        只接受「整串都是选项字母」的答案（B / AB / B、D / A; B; C; D）。绝不能从答案
        正文里随手取字母——"bothered" 里的 b、"a world that does not exist" 里的 a
        都会被误当成选项，那是会直接填错答案的。
        """
        text = (raw or "").strip()
        options = list(getattr(question, "options", None) or [])
        valid = {str(getattr(opt, "letter", "")).upper() for opt in options}

        # 两种合法写法：
        #   1) 连续大写字母：B / AB / ABCD（知识库与 AI 返回都用大写）
        #   2) 带分隔符：A C E / A、B / A, B / A; B / A；B / A / B
        #      （空格也算分隔符：题库里的多选答案按页面写法记成「A C E」）
        # 用「必须大写」或「必须有分隔符」把 beach / cabbage / added 这类
        # 全由 a-h 组成的英文单词挡在外面，避免把单词当成选项字母。
        contiguous = re.fullmatch(r"[A-Z]+", text)
        separated = re.fullmatch(r"[A-Za-z](?:[\s,，、;；/|和及]+[A-Za-z])*", text)
        if contiguous or separated:
            candidate = "".join(
                dict.fromkeys(letter.upper() for letter in re.findall(r"[A-Za-z]", text))
            )
            if not valid or all(letter in valid for letter in candidate):
                return candidate

        # 知识库给的是答案正文而非字母 → 与页面选项正文比对
        needle = _normalize(text)
        if len(needle) >= 3:
            for opt in options:
                if _normalize(getattr(opt, "text", "") or "") == needle:
                    return str(getattr(opt, "letter", "")).upper()
            # 子串匹配只在选项正文足够长时启用，否则单字母选项（a/b/c/d）
            # 会变成任意单词的子串，把「all-forgiving」判成选项 A
            for opt in options:
                opt_text = _normalize(getattr(opt, "text", "") or "")
                if len(opt_text) >= 4 and (needle in opt_text or opt_text in needle):
                    return str(getattr(opt, "letter", "")).upper()
        return ""

    @staticmethod
    def _shorten(text: str, limit: int = 24) -> str:
        flat = re.sub(r"\s+", " ", str(text or "")).strip()
        return flat if len(flat) <= limit else flat[:limit] + "…"

    def _report_hits(self, section: KbSection, hits: List[Tuple[Any, str]], total: int):
        for question, answer in hits:
            type_name = self._type_name(question)
            bucket = "kb_choice" if type_name in CHOICE_TYPES else (
                "kb_blank" if type_name in BLANK_TYPES else "kb_text"
            )
            self.stats[bucket] += 1
            self.stats["hit"] += 1
            preview = self._shorten(answer.replace("\n", " / "), 60)
            print(f"      📚 第 {question.number} 题 [{type_name}] ← 知识库：{preview}")
        missed = total - len(hits)
        if missed:
            print(f"      📚 知识库命中 {len(hits)}/{total} 题，其余 {missed} 题交 AI")

    # -- 诊断 -------------------------------------------------------------

    def describe(self) -> str:
        books = self.books
        if not books:
            return f"知识库为空（{self.root}）"
        parts = []
        for book in books:
            mark = "" if book.answer_ready else "·仅原文，不参与答题"
            parts.append(f"{book.name}（{len(book.sections)} 小节{mark}）")
        return f"{len(books)} 本教材：" + "、".join(parts)

    def summary(self) -> str:
        return (
            f"知识库命中 {self.stats['hit']} 题"
            f"（选择 {self.stats['kb_choice']} / 填空 {self.stats['kb_blank']} / 简答 {self.stats['kb_text']}）"
        )
