# -*- coding: utf-8 -*-
"""knowledge_base 检索模块测试。

用仓库里真实的 knowledge/ 答案库跑，重点验证「宁可交回 AI，也不填错答案」的
安全性质：词库不符、同编号小节无法区分、教材识别不到时都不得给出答案。

不依赖 pytest，直接 `python test_knowledge_base.py` 即可运行。
"""
import os

from knowledge_base import KnowledgeBase, KbSection, _normalize, _part_of

KB_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")

# 《新编大学英语（第四版）综合教程 3》Unit 1 · 1-6 Banked cloze 的词库
CN3_BANK = [
    "adequate", "assigned", "closing", "collect", "conventional", "deadline",
    "due", "edited", "finals", "involved", "mediocre", "midterms",
    "preparatory", "review", "slight",
]

class FakeType:
    def __init__(self, name):
        self.name = name


class FakeOption:
    def __init__(self, letter, text):
        self.letter = letter
        self.text = text
        self.element = None
        self.is_selected = False


class FakeQuestion:
    """鸭子类型的最小题目对象，字段与 main.Question 对齐。"""

    def __init__(self, number, q_type, options=None, banked_options=None,
                 inputs=None, banked_blanks=None):
        self.number = number
        self.q_type = FakeType(q_type)
        self.text = ""
        self.options = options or []
        self.banked_options = banked_options or []
        self.inputs = inputs or []
        self.banked_blanks = banked_blanks or []


def make_kb(textbook="新编大学英语 综合教程3", enabled=True):
    """每个用例都拿一个全新实例，绝不跨用例共享。

    以前按 key 缓存并返回同一实例：有的用例会改 textbook_pref、换教材重新 prepare，
    脏状态会被其它用例拿到，「认不出教材不得作答」这类安全用例就名存实亡
    （自带运行器按字母序执行时尤其严重）。
    """
    assert os.path.isdir(KB_ROOT), f"knowledge/ 目录缺失：{KB_ROOT}"
    instance = KnowledgeBase(root=KB_ROOT, enabled=enabled, verbose=False)
    assert instance.books, "知识库里没有可用的教材文件"
    instance.textbook_pref = textbook
    instance.prepare(None)
    return instance


def blank_question(words, blanks=10):
    return FakeQuestion(1, "BANKED_CLOZE", banked_options=words,
                        banked_blanks=[{} for _ in range(blanks)])


def choice_questions(count, q_type="SINGLE_CHOICE"):
    return [
        FakeQuestion(i, q_type, options=[
            FakeOption("A", "alpha"), FakeOption("B", "beta"),
            FakeOption("C", "gamma"), FakeOption("D", "delta"),
        ])
        for i in range(1, count + 1)
    ]


def fill_questions(count):
    return [FakeQuestion(i, "FILL_IN", inputs=[object()]) for i in range(1, count + 1)]


# --------------------------------------------------------------------------
# 解析与教材识别
# --------------------------------------------------------------------------

def test_normalize_strips_edition_words():
    assert _normalize("新编大学英语（第四版）综合教程 3（2023版）") == "新编大学英语综合教程3"
    assert _normalize("新编大学英语 综合教程3") == "新编大学英语综合教程3"


def test_part_number_extraction():
    assert _part_of("1-6 Read and practice") == "1-6"
    assert _part_of("Reading 1 · 1-2 Get ready to read") == "1-2"
    assert _part_of("没有编号的小节") is None


def test_parses_real_knowledge_base():
    kb = make_kb()
    assert "新编大学英语 综合教程3" in kb.available_books()
    book = kb._match_book_by_name("新编大学英语 综合教程3")
    assert book is not None and len(book.sections) > 100
    section = next(s for s in book.sections if s.part == "1-6" and s.task == "Banked cloze")
    assert section.page == "Read and practice"
    assert len(section.answers) == 10
    assert len(section.wordbank) == 15


def test_matches_book_by_course_code():
    kb = make_kb()
    book = kb._match_book_by_code("https://ucloud.unipus.cn/home#/nce_4_rw_3")
    assert book is not None and book.name == "新编大学英语 综合教程3"


def test_unlisted_book_is_not_guessed():
    assert make_kb()._match_book_by_name("新编大学英语（第四版）综合教程 1") is None


# --------------------------------------------------------------------------
# 答案组装
# --------------------------------------------------------------------------

def test_banked_cloze_returns_numbered_answers():
    hits, misses = make_kb().lookup(
        [blank_question(CN3_BANK)],
        tab_name="1-6 Read and practice Banked cloze",
        directions="Banked cloze",
    )
    assert len(hits) == 1 and not misses
    assert hits[0][1].splitlines()[:3] == ["1. finals", "2. due", "3. adequate"]


def test_choice_answers_give_letters():
    # 该小节的答案是字母（1. B 2. B 3. A …）
    hits, _ = make_kb().lookup(
        choice_questions(3),
        tab_name="1-4 Read and understand Detailed understanding",
        directions="Detailed understanding",
    )
    assert [answer for _, answer in hits] == ["B", "B", "A"]


def test_fill_in_answers_are_numbered_from_question_number():
    hits, _ = make_kb().lookup(
        fill_questions(3),
        tab_name="1-4 Read and understand Global understanding",
        directions="Global understanding",
    )
    assert hits[0][1] == "1. all-forgiving"
    assert hits[1][1] == "1. a world that does not exist"
    assert hits[2][1] == "1. leaves no record"


def test_text_answers_keep_full_content():
    hits, _ = make_kb().lookup(
        [FakeQuestion(1, "TEXT", inputs=[object()])],
        tab_name="1-5 Read and think Why this speech",
        directions="Why this speech",
    )
    assert len(hits) == 1 and "commencement speech" in hits[0][1]


def test_word_answers_are_never_read_as_option_letters():
    """'bothered' 里的 b、'all-forgiving' 里的 a 都不能被当成选项字母。

    第 5 题的答案是 "bothered"，旧实现会因为里面有个 b 而填成选项 B。
    """
    question = FakeQuestion(5, "SINGLE_CHOICE", options=[
        FakeOption("A", "completely unrelated option one"),
        FakeOption("B", "another totally different option"),
        FakeOption("C", "third unrelated possibility here"),
        FakeOption("D", "fourth unrelated choice text"),
    ])
    hits, misses = make_kb().lookup(
        [question],
        tab_name="1-4 Read and understand Global understanding",
        directions="Global understanding",
    )
    assert not hits and len(misses) == 1


# --------------------------------------------------------------------------
# 安全性质：拒绝优先
# --------------------------------------------------------------------------

def test_rejects_banked_cloze_when_wordbank_mismatches():
    hits, misses = make_kb().lookup(
        [blank_question(["apple", "banana", "cherry"])],
        tab_name="1-6 Read and practice Banked cloze",
        directions="Banked cloze",
    )
    assert not hits and len(misses) == 1


def test_rejects_when_same_part_sections_cannot_be_told_apart():
    """1-4 下有 Global / Detailed 两节，页面没给任务名时不得猜。"""
    hits, misses = make_kb().lookup(
        choice_questions(5),
        tab_name="1-4 Read and understand",
        directions="",
    )
    assert not hits and len(misses) == 5


def test_task_name_resolves_same_part_ambiguity():
    hits, _ = make_kb().lookup(
        choice_questions(3, "MULTIPLE_CHOICE"),
        tab_name="1-4 Read and understand Detailed understanding",
        directions="Detailed understanding",
    )
    assert hits


def test_unknown_section_falls_back_to_ai():
    hits, misses = make_kb().lookup(
        choice_questions(5), tab_name="1-99 No such section", directions=""
    )
    assert not hits and len(misses) == 5


def test_unknown_textbook_answers_nothing():
    hits, misses = make_kb("不存在的教材").lookup(
        choice_questions(3), tab_name="1-4 Read and understand"
    )
    assert not hits and len(misses) == 3


def test_disabled_knowledge_base_answers_nothing():
    hits, misses = make_kb(enabled=False).lookup(
        choice_questions(3), tab_name="1-4 Read and understand"
    )
    assert not hits and len(misses) == 3


def test_non_answerable_types_are_left_alone():
    hits, misses = make_kb().lookup(
        [FakeQuestion(1, "VIDEO")], tab_name="1-6 Read and practice Banked cloze"
    )
    assert not hits and len(misses) == 1


# --------------------------------------------------------------------------
# 第二套 schema：H2 页面 + H3 任务（新视野大学英语）
# --------------------------------------------------------------------------

def test_two_level_sections_split_page_and_task():
    book = make_kb("新视野大学英语 读写教程3")._match_book_by_name("新视野大学英语 读写教程3")
    assert book is not None
    section = next(s for s in book.sections
                   if s.unit == 1 and s.task == "Vocabulary learning · Quiz")
    # 「Section A ·」是编号，不属于页面名；留在页面名里就永远对不上 U校园 的页头
    assert section.page == "Reading the text"
    assert section.part == "A"
    assert len(section.answers) == 10
    # 这套 schema 用的是 Section A/B/C 编号，不是 1-6 那种
    assert all(s.part in (None, "A", "B", "C") for s in book.sections)


def test_part_letter_extraction():
    """Section A/B/C 那套编号也要能取出来；英文散文里的 'part a' 不算。"""
    assert _part_of("Section B · Reading skills") == "B"
    assert _part_of("Part C") == "C"
    assert _part_of("read this part a few times") is None


def test_task_tail_and_answer_count_break_the_practicing_tie():
    """Unit 1 里有三个叫 Practicing 的任务；页面只显示任务名时靠答案条数认人。"""
    kb = make_kb("新视野大学英语 读写教程3")
    hits, misses = kb.lookup(
        [FakeQuestion(1, "FILL_IN", inputs=[object() for _ in range(26)])],
        tab_name="Practicing",
        directions="Complete the following passage by filling in the blanks.",
        unit=1,
    )
    assert not misses and hits
    answers = hits[0][1].splitlines()
    assert len(answers) == 26
    assert answers[0] == "1. tablished"


def test_section_a_and_b_tie_is_still_refused():
    """同 Unit 内 Section A/B 同名、空数也相同 -> 认不出就交回 AI，不许猜。"""
    kb = make_kb("新视野大学英语 读写教程3")
    hits, misses = kb.lookup(
        [FakeQuestion(1, "FILL_IN", inputs=[object() for _ in range(8)])],
        tab_name="Expressions in use", directions="", unit=1,
    )
    assert not hits and len(misses) == 1


def test_compulsory_hint_breaks_section_a_b_tie():
    """页面只显示任务名时，必修/选修（= Section A/B）用来打破同名平局。"""
    kb = make_kb("新视野大学英语 读写教程3")
    directions = "Think about the following questions and write down your answers."

    no_hint, misses = kb.lookup(fill_questions(3), tab_name="Critical thinking",
                                directions=directions, unit=1)
    assert not no_hint and len(misses) == 3          # 没有线索时照旧拒绝

    hits_a, _ = kb.lookup(fill_questions(3), tab_name="Critical thinking",
                          directions=directions, unit=1, prefer_part="A")
    hits_b, _ = kb.lookup(fill_questions(3), tab_name="Critical thinking",
                          directions=directions, unit=1, prefer_part="B")
    assert len(hits_a) == 3 and len(hits_b) == 3
    assert [a for _, a in hits_a] != [a for _, a in hits_b]   # A、B 各取各的


def test_meta_sections_are_not_parsed_as_answers():
    """「转录自检」里也有 1. 2. 编号，但那是说明文字，不是答案。"""
    book = make_kb("新视野大学英语 读写教程3")._match_book_by_name("新视野大学英语 读写教程3")
    assert not [s for s in book.sections if "自检" in s.title]


def test_placeholder_wordbank_is_not_a_wordbank():
    """「词库：未在截图中提供」是占位说明，留着会让词库校验误判。"""
    book = make_kb("新视野大学英语 读写教程3")._match_book_by_name("新视野大学英语 读写教程3")
    assert not [s for s in book.sections
                if any("未在截图中提供" in w or "未提供" in w for w in s.wordbank)]


def test_placeholder_bank_in_parentheses_is_not_a_wordbank():
    """「词库：（截图未包含词库，无法转录）」同样是说明，不是词库。

    旧解析把它当成了真词库：与页面词库比对必然不成立，于是正确答案所在的小节被判
    「词库不符」退回 AI —— 读写教程3 有 6 处这样的选词填空小节，U3 那页 AI 填错了
    4 个空。修好后这一节必须直接由题库作答。
    """
    kb = make_kb("新视野大学英语 读写教程3")
    book = kb._match_book_by_name("新视野大学英语 读写教程3")
    assert not [s for s in book.sections if any("截图" in w for w in s.wordbank)]

    # 《读写教程3》Unit 3 · Section A · Language focus · Banked cloze 的页面词库
    page_bank = ["annoyances", "cognitive", "console", "denoting", "embracing", "endows",
                 "immerse", "loosen", "luncheon", "nuances", "offsets", "positivity",
                 "recipe", "trivial", "tropical"]
    hits, misses = kb.lookup(
        [blank_question(page_bank)],
        tab_name="Let's go Section A Language focus Banked cloze",
        directions="Banked cloze", unit=3,
    )
    assert not misses, "这一节应当命中题库，不该退回 AI"
    assert hits[0][1].splitlines()[:10] == [
        "1. trivial", "2. cognitive", "3. console", "4. annoyances", "5. embracing",
        "6. positivity", "7. recipe", "8. immerse", "9. loosen", "10. endows",
    ]


def test_book_is_redetected_after_switching_to_another_textbook():
    """换课程/换教材后必须重认教材。

    教材原先只认一次并缓存到底：账号2 的浏览器在《读写教程1》上，题库却一直用启动时
    认到的《读写教程3》——正确小节没进候选，选词填空被判「无法验证」退回 AI 并答错。
    """
    kb = KnowledgeBase(root=KB_ROOT, enabled=True, verbose=False)
    kb.textbook_pref = "auto"

    class FakeDriver:
        def __init__(self, title, url):
            self.title = title
            self.current_url = url

        def find_elements(self, *a, **k):
            return []

    rw3 = FakeDriver("新视野大学英语（第四版）读写教程3",
                     "https://ucontent.unipus.cn/_explorationpc_default/pc.html")
    assert kb.prepare(rw3) == "新视野大学英语 读写教程3"

    rw1 = FakeDriver("新视野大学英语（第四版）读写教程1",
                     "https://ucontent.unipus.cn/_explorationpc_default/pc.html?cid=1855335437114605653")
    # 读写教程1 Unit 1 Banked cloze 的页面词库；答案只存在读写教程1 的题库里
    bank = ["acquiring", "attend", "available", "classify", "especially", "fascinating",
            "fashionable", "interest", "passion", "prosperous", "pursue", "qualifying",
            "sampled", "virtually"]
    hits, misses = kb.lookup(
        [blank_question(bank)],
        driver=rw1,
        tab_name="Unit1 Section A Language focus Banked cloze",
        directions="Banked cloze", unit=1,
    )
    assert not misses, "换教材后应当重认教材并命中，而不是沿用旧教材退回 AI"
    assert hits[0][1].splitlines()[:10] == [
        "1. classify", "2. passion", "3. attend", "4. pursue", "5. virtually",
        "6. fascinating", "7. prosperous", "8. acquiring", "9. available", "10. sampled",
    ]


def test_transcript_only_book_is_excluded_from_answering():
    """视听说教程3 是按教材原文逐页转录、答案是散文，不是答案表 -> 一题都不答。"""
    kb = make_kb("新视野大学英语 视听说教程3")
    book = kb._match_book_by_name("新视野大学英语 视听说教程3")
    assert book is not None and not book.answer_ready
    hits, misses = kb.lookup(
        [FakeQuestion(1, "SINGLE_CHOICE", options=[FakeOption("A", "x")])],
        tab_name="Opening up", directions="Opening up", unit=1,
    )
    assert not hits and len(misses) == 1


def test_same_task_in_section_a_and_b_do_not_cross_contaminate():
    """Section A / B 各有同名任务，必须各取各的答案。"""
    kb = make_kb("新视野大学英语 读写教程3")
    hits_a, _ = kb.lookup(
        choice_questions(3),
        tab_name="Unit 1 Section A Reading the text Vocabulary learning Quiz",
        directions="Vocabulary learning Quiz", unit=1,
    )
    hits_b, _ = kb.lookup(
        choice_questions(3),
        tab_name="Unit 1 Section B Reading the text Vocabulary learning Quiz",
        directions="Vocabulary learning Quiz", unit=1,
    )
    assert [a for _, a in hits_a] == ["A", "B", "C"]
    assert [a for _, a in hits_b] == ["A", "C", "B"]


def test_same_task_across_units_needs_the_unit_number():
    """跨 Unit 整节同名：给了 Unit 取对应那本，给不出就拒绝。"""
    kb = make_kb("新视野大学英语 读写教程3")
    hits_u2, _ = kb.lookup(
        choice_questions(3),
        tab_name="Unit 2 Section A Vocabulary learning Quiz",
        directions="Vocabulary learning Quiz", unit=2,
    )
    assert [a for _, a in hits_u2] == ["D", "A", "B"]

    hits, misses = kb.lookup(
        choice_questions(3),
        tab_name="Section A Reading comprehension Understanding the text",
        directions="Understanding the text", unit=None,
    )
    assert not hits and len(misses) == 3


def test_unit_number_separates_identical_section_names():
    def first_answer(unit):
        hits, _ = make_kb("新视野大学英语 读写教程3").lookup(
            [FakeQuestion(1, "FILL_IN", inputs=[object()])],
            tab_name=f"Unit {unit} Section A Reading comprehension Understanding the text",
            directions="Understanding the text", unit=unit,
        )
        return hits[0][1] if hits else ""

    unit1, unit2 = first_answer(1), first_answer(2)
    assert unit1 and unit2 and unit1 != unit2


def test_banked_cloze_verified_by_answer_membership_when_book_has_no_wordbank():
    """新视野的转录没给出词库，用「答案必须来自词库」反查来验证归属。"""
    words = ["maintain", "scenario", "immersed", "concentrate", "obsession",
             "approval", "lure", "continual", "diminishes", "anticipate"]
    hits, _ = make_kb("新视野大学英语 读写教程3").lookup(
        [blank_question(words)],
        tab_name="Unit 1 Section A Language focus Banked cloze",
        directions="Banked cloze", unit=1,
    )
    assert len(hits) == 1
    assert hits[0][1].splitlines()[0] == "1. maintain"


def test_banked_cloze_rejected_when_answers_not_in_page_wordbank():
    hits, misses = make_kb("新视野大学英语 读写教程3").lookup(
        [blank_question(["apple", "banana", "cherry"])],
        tab_name="Unit 1 Section A Language focus Banked cloze",
        directions="Banked cloze", unit=1,
    )
    assert not hits and len(misses) == 1


def test_choice_letters_beyond_d_are_supported():
    """选项字母要认到 H：Collocation 匹配题等会出现 A-I 清单，答案是 E 及以后的题
    原先因为字母范围硬编码成 A-D 而填不上（knowledge/ 里曾有 31 道这样的题）。"""
    kb = make_kb("新视野大学英语 读写教程2")
    # Unit 6 Pre-reading Task 1 的答案是 B C A E D
    five = [FakeQuestion(i, "SINGLE_CHOICE",
                         options=[FakeOption(c, f"option {c}") for c in "ABCDE"])
            for i in range(1, 6)]
    hits, _ = kb.lookup(
        five,
        tab_name="Unit 6 Section A Reading the text Pre-reading activities Task 1",
        directions="Pre-reading activities Task 1", unit=6,
    )
    assert [a for _, a in hits] == ["B", "C", "A", "E", "D"]

    # 页面只有 A-D 时不得硬填 E（宁可交回 AI）：能对上的题照常给字母，
    # 第 4 题（答案是 E）因页面没有这个选项而必须留给 AI —— 既不硬填，也不整节丢掉。
    four = [FakeQuestion(i, "SINGLE_CHOICE",
                         options=[FakeOption(c, f"option {c}") for c in "ABCD"])
            for i in range(1, 6)]
    hits, misses = kb.lookup(
        four,
        tab_name="Unit 6 Section A Reading the text Pre-reading activities Task 1",
        directions="Pre-reading activities Task 1", unit=6,
    )
    assert [a for _, a in hits] == ["B", "C", "A", "D"]
    assert [q.number for q in misses] == [4]


def test_letter_answers_accept_all_formats_but_reject_words():
    """多字母答案的各种写法都要认；英文单词绝不能被当成选项字母。

    知识库里多选答案写作 "A; B; C; D"（分号）或 "AB"（连续大写），
    而 beach / cabbage / added 这类全由 a-h 组成的单词必须被拒绝。
    """
    kb = make_kb()

    class _Opt:
        def __init__(self, letter):
            self.letter = letter
            self.text = "opt" + letter
            self.element = None
            self.is_selected = False

    class _Q:
        def __init__(self):
            self.options = [_Opt(c) for c in "ABCDEFGH"]

    for raw, expect in (
        ("AB", "AB"), ("ABCD", "ABCD"), ("B", "B"),
        ("A; B; C; D", "ABCD"), ("A、B、C、D", "ABCD"),
        ("A, B", "AB"), ("A；B", "AB"), ("A / B", "AB"),
    ):
        assert kb._letters_from(raw, _Q()) == expect, f"{raw!r} 应解析为 {expect!r}"

    for raw in ("beach", "cabbage", "added", "Because", "bothered",
                "a world that does not exist", "", "the", "feed"):
        assert kb._letters_from(raw, _Q()) == "", f"{raw!r} 不该被当成选项字母"


def test_multi_letter_answer_is_refused_for_single_choice():
    """答案是多个字母（实为多选）时，单选题不得只填第一个字母。"""
    kb = make_kb("新视野大学英语 读写教程4")
    section = KbSection(
        title="t", page="Section A · Reading comprehension",
        task="Critical thinking skill", answers=[(1, "A; B; C; D")],
    )

    class _Q2:
        def __init__(self, type_name):
            self.number = 1
            self.q_type = FakeType(type_name)
            self.options = [FakeOption(c, f"option {c}") for c in "ABCD"]
            self.banked_options = []
            self.inputs = []
            self.banked_blanks = []

    assert kb._assemble_answer(section, _Q2("MULTIPLE_CHOICE")) == "ABCD"
    assert kb._assemble_answer(section, _Q2("SINGLE_CHOICE")) == ""


def test_longer_section_name_wins_over_its_prefix():
    """一个小节名是另一个的前缀时，页面同时能匹配两者，应选更具体的那个。

    知识库里 Section A · Reading comprehension 下并存 Critical thinking 与
    Critical thinking skill，页面写 "Critical thinking skill" 时若判为两者
    无法区分就会白白丢掉一个真答案。
    """
    kb = make_kb("新视野大学英语 读写教程4")
    hits, _ = kb.lookup(
        [FakeQuestion(1, "SINGLE_CHOICE",
                      options=[FakeOption(c, f"option {c}") for c in "ABCD"])],
        tab_name="Unit 2 Section A Reading comprehension Critical thinking skill",
        directions="Critical thinking skill", unit=2,
    )
    assert [a for _, a in hits] == ["D"]

    # 反向：页面写 Critical thinking（主观题，答案是整段文字）时，不该被
    # 答案只有一个字母的 Critical thinking skill 抢走 —— 题型对不上就不该认。
    hits, misses = kb.lookup(
        [FakeQuestion(1, "TEXT", inputs=[object()])],
        tab_name="Unit 2 Section A Reading comprehension Critical thinking",
        directions="Critical thinking", unit=2,
    )
    # 也不能因为「名字是更长的那个的前缀」而整条丢掉：该小节自己有答案就要给出来。
    # 断言的必须是「散文答案确实给出」—— 只查 "D" not in hits 的话，空 hits 也能过，
    # 那正是这条用例要防的另一种失败（什么都没答）却查不出来。
    assert not misses and len(hits) == 1
    assert hits[0][1].startswith("1. Yes, I totally agree with this opinion.")


class _FakeDriver:
    """按 U校园 课程页的形状造假。

    默认让所有 CSS 选择器都落空 —— 这正是真机上教材识别失败的原因：
    前四步识别全靠猜元素类名，页面改版或换页面类型就全部失效。
    """

    def __init__(self, url="", title="", body="", elements=None):
        self.current_url = url
        self.title = title
        self._body = body
        self._elements = elements if elements is not None else []

    def execute_script(self, script, *args):
        if "innerText" in script:
            return self._body
        return None

    def find_elements(self, by, selector):
        return self._elements


def test_book_detected_from_whole_page_text():
    """教材名只出现在页面正文里时也要能认出来（真机失败的主要场景）。"""
    kb = make_kb("不存在的教材")  # 先让它认不出，再验证不依赖配置
    kb.textbook_pref = "auto"
    driver = _FakeDriver(
        url="https://ucloud.unipus.cn/home#/course/xxx/unit/1",
        title="U校园",
        body="我的课程 新视野大学英语（第四版）读写教程3 课程学习 Unit 1 Fresh start",
    )
    assert kb.prepare(driver) == "新视野大学英语 读写教程3"


def test_book_detected_from_all_three_course_code_formats():
    """三种真实教材代码形式都要能解析。"""
    cases = [
        ("https://x/nv4_rw_3", "新视野大学英语 读写教程3"),
        ("https://x/nhce_v4_ls_3", "新视野大学英语 视听说教程3"),
        ("https://x/nce_4_rw_3", "新编大学英语 综合教程3"),
    ]
    for url, expect in cases:
        kb = make_kb("不存在的教材")
        kb.textbook_pref = "auto"
        assert kb.prepare(_FakeDriver(url=url)) == expect, f"{url} 应识别为 {expect}"


def test_book_detection_does_not_guess_when_it_cannot_tell():
    """认不出时不瞎猜，并给出可操作的诊断信息。"""
    kb = make_kb("不存在的教材")
    kb.textbook_pref = "auto"
    assert kb.prepare(_FakeDriver(url="https://x/unknown", title="随便", body="没有教材名")) is None
    assert "knowledge_textbook" in kb._resolve_note


def test_book_detection_still_works_via_header_selectors():
    """原有选择器路径不能因为新增兜底而坏掉。"""

    class _Hit:
        text = "新视野大学英语（第四版）读写教程2"

    kb = make_kb("不存在的教材")
    kb.textbook_pref = "auto"
    assert kb.prepare(_FakeDriver(url="https://x/y", elements=[_Hit()])) == "新视野大学英语 读写教程2"


class _FakeHeaderEl:
    def __init__(self, text):
        self.text = text


class _FakeHeaderDriver:
    """模拟页头：只有选中的子标签和当前栏目会出现在页头文字里。"""

    def __init__(self, title="新视野大学英语（第四版）读写教程1"):
        self.title = title
        self.current_url = ("https://ucontent.unipus.cn/_explorationpc_default/pc.html"
                            "?cid=1855335437114605653")

    def find_elements(self, by, selector):
        picked = {
            "[class*='pc-header-task-activity']": "Task 1",
            "[class*='pc-header-tab-activity']": "Paragraph translation",
        }.get(selector)
        return [_FakeHeaderEl(picked)] if picked else []


def test_paragraph_translation_page_gets_the_reference_translation():
    """段落翻译页要能命中 Section C 的标准译文。

    这页只有一行 textarea 加一段原文，旧的解析策略认不出来（日志「没有匹配的策略」）；
    认出来以后，匹配器还得解决两件事：Section C 不能被「必修=Section A」的推断排除，
    以及 Task 1 / Task 2 要靠选中的那个子标签分开。
    """
    kb = KnowledgeBase(root=KB_ROOT, enabled=True, verbose=False)
    kb.textbook_pref = "auto"
    driver = _FakeHeaderDriver()
    assert kb.prepare(driver) == "新视野大学英语 读写教程1"

    question = FakeQuestion(
        1, "TEXT", inputs=[object()],
    )
    question.text = ("【题目要求】Translate the following paragraph into Chinese.\n\n"
                     "【问题列表】\n1. Socrates was a classical Greek philosopher.")
    for prefer in ("A", None):
        hits, misses = kb.lookup(
            [question], driver=driver,
            tab_name="Paragraph translation",
            directions="Translate the following paragraph into Chinese.",
            unit=1, prefer_part=prefer,
        )
        assert not misses, f"prefer_part={prefer} 时应当命中题库"
        answer = hits[0][1]
        assert "苏格拉底" in answer, answer[:60]


def test_cid_overrides_stale_textbook_pref():
    """课程页 cid 是事实级证据：config 里记着旧教材（如读写教程3）时，

    打开视听说3 的课程页（cid 已在 course_map 收录）也必须按 cid 走，
    绝不能拿旧教材的小节去匹配 —— 这正是「打开视听说3、命中读写教程1」的根因。
    """
    kb = KnowledgeBase(root=KB_ROOT, enabled=True, verbose=False)
    kb.textbook_pref = "新视野大学英语 读写教程3"
    kb.prepare(None)
    assert kb._book is not None and "读写教程3" in kb._book.name

    class _CidDriver:
        def __init__(self, url):
            self.current_url = url

    # 视听说教程3 的 cid（2026-10-03 实机核对）→ 覆盖配置里的读写教程3
    book = kb.prepare(_CidDriver(
        "https://ucontent.unipus.cn/_explorationpc_default/pc.html?cid=1579642886499368960"))
    assert book is not None and "视听说教程3" in book, book

    # 未收录的 cid 不硬猜：回到配置里的教材
    book = kb.prepare(_CidDriver("https://ucloud.unipus.cn/home?cid=9999999999999"))
    assert book is not None and "读写教程3" in book, book


def test_series_only_text_is_not_guessed():
    """页面只写系列名（「新视野大学英语」）时分不出是哪一本 —— 必须拒绝识别，

    不能挑一本「名字最长」的硬认（读写/视听说就是这么串起来的）；写全名才认。
    """
    kb = make_kb()
    assert kb._match_book_by_name("新视野大学英语（第四版）") is None
    hit = kb._match_book_by_name("新视野大学英语（第四版）视听说教程3")
    assert hit is not None and "视听说教程3" in hit.name
    hit = kb._match_book_by_name("新视野大学英语（第四版）读写教程1")
    assert hit is not None and "读写教程1" in hit.name


def test_harvest_block_answers_even_when_book_not_answer_ready():
    """方案B：书不可逐题作答（散文转录）时，「浏览器核对收录」区的已验证答案仍可命中。

    以前整书被 answer_ready 排除，收录进视听说这类书的答案永远用不上——
    收录闭环是断的。现在书不可作答时进入「仅收割区」模式：只匹配收割块小节。
    """
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="kb_harvest_")
    try:
        book = os.path.join(tmp, "某教材 视听说教程9.md")
        prose = "\n\n".join(
            f"## Section X · Prose page {i}\n\n"
            "This is a long transcript of the textbook page without numbered answers.\n"
            for i in range(4)
        )
        md = (
            "# 某教材 视听说教程9\n\n"
            + prose
            + "\n\n## 浏览器核对收录（Unit 3）\n\n"
            + "> 页面确认全对后收录。\n\n"
            + "### Read the statements and fill in the blanks\n\n"
            + "1. ticking away\n2. antique clock restoration\n3. work on\n"
        )
        with open(book, "w", encoding="utf-8") as fh:
            fh.write(md)

        kb = KnowledgeBase(root=tmp, enabled=True, verbose=False)
        kb.textbook_pref = "某教材 视听说教程9"
        kb.prepare(None)
        assert kb._book is not None and not kb._book.answer_ready, "合成书应判为不可逐题作答"

        questions = [FakeQuestion(i, "LISTENING_FILL_IN", inputs=[object()])
                     for i in range(1, 4)]
        hits, misses = kb.lookup(
            questions,
            tab_name="Read the statements and fill in the blanks",
            directions="Read the statements. Then listen again and fill in the blanks.",
            unit=3,
        )
        assert hits and not misses, f"收割区应当命中：hits={hits} misses={misses}"
        assert "ticking away" in hits[0][1], hits[0][1]

        # 收割区之外的名字不得命中（散文转录小节在仅收割区模式下被排除）
        hits2, misses2 = kb.lookup(
            questions, tab_name="Prose page 1", directions="", unit=3)
        assert not hits2 and len(misses2) == 3, (hits2, misses2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_harvest_blocks_with_same_name_are_disambiguated_or_refused():
    """同名同空数的收割块：跨 Unit 靠 Unit 号分开；同 Unit 分不开就整组拒绝。

    「Exercise 3」这类通用任务名完全可能撞名。安全设计宁可交回 AI 也不猜：
    Unit 号是硬条件（能分开就精确命中）；分不开（同 Unit 同名同条数）时
    平局仲裁必须拒绝，绝不能拿 A 页的已验证答案去填 B 页。
    """
    import shutil
    import tempfile

    def build_md(unit_a, unit_b=None):
        parts = ["# 某教材 视听说教程9.md".replace(".md", "")]
        prose = "\n\n".join(
            f"## Section X · Prose page {i}\n\n"
            "This is a long transcript of the textbook page without numbered answers.\n"
            for i in range(4))
        parts.append(prose)
        # 旧块（无指纹）也必须在场：停用范围只限「有指纹但不足 2 词」的块，
        # 读写教程1 那批指纹功能上线前的 Quiz/Words in use 仍靠任务名+条数命中
        for unit, answers in ((unit_a, "1. alpha\n2. beta\n3. gamma"),
                              (unit_b, "1. delta\n2. epsilon\n3. zeta")):
            if unit is None:
                continue
            parts.append(
                f"\n\n## 浏览器核对收录（Unit {unit}）\n\n"
                "> 页面确认全对后收录。\n\n"
                "### Exercise 3\n\n" + answers + "\n")
        return "\n".join(parts)

    def make(tmp, md):
        with open(os.path.join(tmp, "某教材 视听说教程9.md"), "w", encoding="utf-8") as fh:
            fh.write(md)
        kb = KnowledgeBase(root=tmp, enabled=True, verbose=False)
        kb.textbook_pref = "某教材 视听说教程9"
        kb.prepare(None)
        assert kb._book is not None and not kb._book.answer_ready
        return kb

    questions = [FakeQuestion(i, "LISTENING_FILL_IN", inputs=[object()])
                 for i in range(1, 4)]

    tmp = tempfile.mkdtemp(prefix="kb_tie_")
    try:
        # 场景一：同 Unit 内两个同名同条数的收割块（答案不同）→ 必须整组拒绝
        kb = make(tmp, build_md(3, 3))
        hits, misses = kb.lookup(
            questions, tab_name="Exercise 3", directions="Exercise 3", unit=3)
        assert not hits and len(misses) == 3, \
            f"同名同条数分不开时必须交回 AI，不得猜：hits={hits}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    tmp = tempfile.mkdtemp(prefix="kb_unit_")
    try:
        # 场景二：跨 Unit 的同名块（Unit 3 与 Unit 5）→ Unit 硬过滤精确分开
        kb = make(tmp, build_md(3, 5))
        hits, misses = kb.lookup(
            questions, tab_name="Exercise 3", directions="Exercise 3", unit=3)
        assert hits and not misses, f"Unit 3 应精确命中：hits={hits} misses={misses}"
        assert "alpha" in hits[0][1], hits[0][1]
        hits5, misses5 = kb.lookup(
            questions, tab_name="Exercise 3", directions="Exercise 3", unit=5)
        assert hits5 and not misses5, f"Unit 5 应精确命中：hits={hits5} misses={misses5}"
        assert "delta" in hits5[0][1], hits5[0][1]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_harvest_fingerprint_disambiguates_same_name_blocks():
    """收录块带「题目指纹」后，同名同空数的小节靠指纹精确区分（不再只能整组拒绝）。

    端到端：answer_harvest.harvest 写入（含指纹行）→ 解析 → 匹配。
    指纹对上的那套精确命中；两套指纹都对不上时照旧整组拒绝、绝不猜。
    """
    import shutil
    import tempfile

    import answer_harvest

    tmp = tempfile.mkdtemp(prefix="kb_fp_")
    try:
        book = os.path.join(tmp, "某教材 视听说教程9.md")
        prose = "\n\n".join(
            f"## Section X · Prose page {i}\n\n"
            "This is a long transcript of the textbook page without numbered answers.\n"
            for i in range(4))
        with open(book, "w", encoding="utf-8") as fh:
            fh.write("# 某教材 视听说教程9\n\n" + prose + "\n")

        # 模拟两次收录：同名同空数、内容不同，各自带页面指纹
        assert answer_harvest.harvest(
            book, 3, "Exercise 3", ["alpha", "beta", "gamma"], {"correct": 3, "total": 3},
            fingerprint=["restoration", "workshop", "clocks"]) is True
        assert answer_harvest.harvest(
            book, 3, "Exercise 3", ["delta", "epsilon", "zeta"], {"correct": 3, "total": 3},
            fingerprint=["safari", "ecotourism", "rangers"]) is True

        kb = KnowledgeBase(root=tmp, enabled=True, verbose=False)
        kb.textbook_pref = "某教材 视听说教程9"
        kb.prepare(None)
        assert kb._book is not None and not kb._book.answer_ready

        def questions_with(text):
            qs = [FakeQuestion(i, "LISTENING_FILL_IN", inputs=[object()])
                  for i in range(1, 4)]
            for q in qs:
                q.text = text
            return qs

        hits, misses = kb.lookup(
            questions_with("The restoration of antique clocks in the quiet workshop."),
            tab_name="Exercise 3", directions="Exercise 3", unit=3)
        assert hits and not misses, f"指纹 A 应命中第一套：hits={hits} misses={misses}"
        assert "alpha" in hits[0][1], hits[0][1]

        hits_b, misses_b = kb.lookup(
            questions_with("Ecotourism and rangers protect the safari park."),
            tab_name="Exercise 3", directions="Exercise 3", unit=3)
        assert hits_b and not misses_b, f"指纹 B 应命中第二套：hits={hits_b} misses={misses_b}"
        assert "delta" in hits_b[0][1], hits_b[0][1]

        # 两套指纹都对不上 → 照旧整组拒绝（绝不猜）
        hits_c, misses_c = kb.lookup(
            questions_with("Completely unrelated content about quantum physics theories."),
            tab_name="Exercise 3", directions="Exercise 3", unit=3)
        assert not hits_c and len(misses_c) == 3, f"指纹都对不上必须拒绝：hits={hits_c}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_merged_legacy_harvest_entry_still_answers():
    """旧收录块把整页答案压成 1 条合并串（「1. x 2. y …」）也要能作答：

    凑不齐连续 count 条时，若首条带 ≥2 个编号行就原样交回，由执行器按编号
    拆进各空——否则旧块「小节命中了却组装不出答案」（实测 0/1 题）。
    """
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="kb_legacy_")
    try:
        book = os.path.join(tmp, "某教材 视听说教程9.md")
        md = (
            "# 某教材 视听说教程9\n\n"
            "## Section X · Prose page 1\n\n"
            "This is a long transcript of the textbook page without numbered answers.\n"
            "\n## 浏览器核对收录（Unit 3）\n\n"
            "### Exercise 3\n\n"
            "指纹：ticking | restoration | workshop\n"
            "1) 1. ticking away 2. antique clock restoration 3. work on\n"
        )
        with open(book, "w", encoding="utf-8") as fh:
            fh.write(md)

        kb = KnowledgeBase(root=tmp, enabled=True, verbose=False)
        kb.textbook_pref = "某教材 视听说教程9"
        kb.prepare(None)
        # 真实旧块的形状：一道听力填空题、多个输入框（整页答案被压成 1 条合并串）
        question = FakeQuestion(1, "LISTENING_FILL_IN",
                                inputs=[object(), object(), object()])
        question.text = "The ticking of clocks in the restoration workshop."
        hits, misses = kb.lookup(
            [question], tab_name="Exercise 3", directions="Exercise 3", unit=3)
        assert hits and not misses, f"旧合并块应当命中：hits={hits} misses={misses}"
        assert "ticking away" in hits[0][1], hits[0][1]
        assert "work on" in hits[0][1], hits[0][1]
        # 合并串必须原样交回（自带编号），不能被重新加题号变成「1. 1. …」
        assert not hits[0][1].startswith("1. 1."), hits[0][1]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_same_page_repeat_harvests_pick_most_complete():
    """同一页被反复收录（同名块全部指纹一致）→ 取答案最全的一套，不再整组拒绝。

    指纹来自页面：同一页收录几次指纹就相同，这是「同页重复收录」，不是
    「不同页撞名」——取条数最多（覆盖最全）的一套不算猜。
    """
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="kb_rep_")
    try:
        book = os.path.join(tmp, "某教材 视听说教程9.md")
        md = (
            "# 某教材 视听说教程9\n\n"
            "## Section X · Prose page 1\n\n"
            "This is a long transcript of the textbook page without numbered answers.\n"
            "\n## 浏览器核对收录（Unit 3）\n\n"
            "> 页面确认全对后收录。\n\n"
            "### Exercise 3\n\n"
            "指纹：ticking | restoration | workshop\n"
            "1) 1. ticking away 2. antique clock restoration 3. work on\n"
            "\n### Exercise 3\n\n"
            "指纹：ticking | restoration | workshop\n"
            "1. ticking away\n2. antique clock restoration\n3. work on\n"
        )
        with open(book, "w", encoding="utf-8") as fh:
            fh.write(md)

        kb = KnowledgeBase(root=tmp, enabled=True, verbose=False)
        kb.textbook_pref = "某教材 视听说教程9"
        kb.prepare(None)
        question = FakeQuestion(1, "LISTENING_FILL_IN",
                                inputs=[object(), object(), object()])
        question.text = "The ticking of clocks in the restoration workshop."
        hits, misses = kb.lookup(
            [question], tab_name="Exercise 3", directions="Exercise 3", unit=3)
        assert hits and not misses, f"同页重复收录应取最全一套：hits={hits} misses={misses}"
        # 最全的一套 = 3 条独立答案的那块（组装后按编号列出，不含合并串的内层编号）
        assert hits[0][1].splitlines()[0] == "1. ticking away", hits[0][1]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_harvest_replaces_same_page_blocks():
    """收录端防堆积：同页旧块（同名且无指纹/指纹重合）写入前被移除；

    不同页的同名块（指纹不同）保留，靠指纹互相区分。
    """
    import shutil
    import tempfile

    import answer_harvest

    tmp = tempfile.mkdtemp(prefix="kb_dedup_")
    try:
        book = os.path.join(tmp, "某教材 视听说教程9.md")
        with open(book, "w", encoding="utf-8") as fh:
            fh.write("# 某教材 视听说教程9\n\n正文。\n")

        fp = ["ticking", "restoration", "workshop"]
        assert answer_harvest.harvest(
            book, 3, "Exercise 3", ["alpha", "beta", "gamma"], {}, fingerprint=fp)
        assert answer_harvest.harvest(
            book, 3, "Exercise 3", ["delta", "epsilon", "zeta"], {}, fingerprint=fp)
        body = open(book, encoding="utf-8").read()
        assert body.count("### Exercise 3") == 1, "同页旧块应被替换而不是堆积"
        assert "delta" in body and "alpha" not in body, "应保留最新一套"

        # 不同页的同名块（指纹不同）→ 保留
        assert answer_harvest.harvest(
            book, 3, "Exercise 3", ["hello", "world", "again"], {},
            fingerprint=["safari", "ecotourism", "rangers"])
        body = open(book, encoding="utf-8").read()
        assert body.count("### Exercise 3") == 2, "不同页的同名块应共存"
        assert "delta" in body and "hello" in body
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_single_word_fingerprint_is_not_evidence():
    """单词指纹不足以证明「是同一页」：这种收录块不参与匹配。

    实测：视听说教程3 的收录块指纹只有一个词（passage），在「读出这些表达」
    多选题上凭「任务名一致 + 指纹撞车」命中，把别题的答案 C 填了进去。
    单词指纹（少于 2 个词）从今往后一律不参与匹配 —— 交回 AI 重答；答对后
    会被重新收录，新指纹带上选项词，那时才具备复用资格。两个词以上正常生效。
    """
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="kb_fp1_")
    try:
        book = os.path.join(tmp, "某教材 视听说教程9.md")
        prose = "\n\n".join(
            f"## Section X · Prose page {i}\n\n"
            "This is a long transcript of the textbook page without numbered answers.\n"
            for i in range(4))
        md = (
            "# 某教材 视听说教程9\n\n"
            + prose
            + "\n\n## 浏览器核对收录（Unit 7）\n\n"
            + "### Passage\n\n"
            + "指纹：pollen\n"
            + "1) C\n2) A\n3) B\n"
            + "\n### Choose the expressions\n\n"
            + "指纹：pollen | recyclable\n"
            + "1) A C E\n"
        )
        with open(book, "w", encoding="utf-8") as fh:
            fh.write(md)

        kb = KnowledgeBase(root=tmp, enabled=True, verbose=False)
        kb.textbook_pref = "某教材 视听说教程9"
        kb.prepare(None)
        assert not kb._book.answer_ready

        q1 = FakeQuestion(1, "MULTIPLE_CHOICE", options=[
            FakeOption("A", "expr one"), FakeOption("C", "expr two"),
            FakeOption("E", "expr three")])
        q1.text = "Researchers have developed a paper made from pollen, a renewable substance."

        # 单词指纹块不得凭「pollen」一词撞车命中
        hits, misses = kb.lookup(
            [q1], tab_name="Exercise 2",
            directions="Read the expressions. Then listen to the passage again and choose the ones you hear.",
            unit=7)
        assert not hits and len(misses) == 1, f"单词指纹不应成为证据：hits={hits}"

        # 对抗例：另一单元「进一步听力 · Passage」页 —— 任务名逐字一致、题数与
        # 块里答案条数也一致（不加阻塞规则时这三点足以让它成为高置信命中）。
        # 单词指纹证明不了身份，这种页必须交回 AI，绝不能拿 C/A/B 乱填。
        adversarial = [
            FakeQuestion(i, "MULTIPLE_CHOICE", options=[
                FakeOption("A", "alpha"), FakeOption("B", "beta"),
                FakeOption("C", "gamma"), FakeOption("D", "delta")])
            for i in (1, 2, 3)
        ]
        hits3, misses3 = kb.lookup(
            adversarial, tab_name="Passage",
            directions="Listen to the passage and complete the exercises.",
            unit=7)
        assert not hits3 and len(misses3) == 3, f"单词指纹块不得参与匹配：hits={hits3}"

        # 两词指纹齐全时正常生效（正例控制）
        q2 = FakeQuestion(1, "MULTIPLE_CHOICE", options=[
            FakeOption("A", "expr one"), FakeOption("C", "expr two"),
            FakeOption("E", "expr three")])
        q2.text = "recyclable pollen based paper is a new approach."
        hits2, misses2 = kb.lookup(
            [q2], tab_name="Choose the expressions",
            directions="Choose the expressions you hear.", unit=7)
        assert hits2 and not misses2, f"两词指纹应正常命中：hits={hits2} misses={misses2}"
        # 题库里的空格分隔多选「A C E」要组装成执行器认的连续字母串
        assert hits2[0][1] == "ACE", hits2[0][1]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
