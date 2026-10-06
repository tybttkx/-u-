from types import SimpleNamespace
from threading import Event
from unittest.mock import Mock, patch

import main as app

# 真题库接线测试期望的逐字答案（《新编大学英语 综合教程3》Unit 1 · 1-6 Banked cloze 的前三个空）。
# 题库内容变更时同步更新这里（README「知识库的答案是错的怎么办」一节有说明）。
EXPECTED_REAL_KB_ANSWERS = ["1. finals", "2. due", "3. adequate"]


class _SkipTest(Exception):
    """测试环境不满足时的显式跳过：自带运行器把它打印成 SKIP，绝不静默算 PASS。"""


def _skip(reason):
    """跳过当前用例：装了 pytest 就用标准的 pytest.skip，没有则抛 _SkipTest。"""
    try:
        import pytest
    except ImportError:
        raise _SkipTest(reason) from None
    pytest.skip(reason)


def test_answer_executor_returns_bool():
    safe_click = app.WebDriverHelper.safe_click
    app.WebDriverHelper.safe_click = staticmethod(lambda *_: True)
    try:
        question = SimpleNamespace(
            number=1,
            options=[app.Option("A", "alpha", object())],
        )
        executor = app.AnswerExecutor(None)
        assert executor._fill_single_choice(question, "A") is True
        assert executor._fill_single_choice(question, "Z") is False
    finally:
        app.WebDriverHelper.safe_click = safe_click


def test_selected_duplicate_task_uses_scanned_occurrence():
    unit = Mock()
    names = [SimpleNamespace(text=name) for name in ("Vocabulary", "Reading", "Vocabulary", "Vocabulary")]
    chapters = [Mock() for _ in names]
    for chapter, name in zip(chapters, names):
        chapter.find_element.return_value = name

    driver = Mock(current_url="https://example.test/course")
    driver.find_elements.return_value = chapters
    unit_container = Mock()
    unit_container.find_elements.return_value = [unit]
    solver = app.AISolver.__new__(app.AISolver)
    solver.driver = driver
    solver.ai_client = Mock()
    solver.knowledge_base = Mock()
    solver.processed_hashes = set()
    solver._should_stop = Mock(return_value=False)
    solver.stop_requested = Mock()
    solver.stop_requested.wait.return_value = False
    solver._process_tab_with_accumulation = Mock()
    tab = {"_element": names[0], "_unit_idx": 0, "_name_occurrence": 2,
           "l1_title": "Vocabulary", "display": "Vocabulary #3"}

    with patch.object(app, "WebDriverWait") as wait, patch.object(app.time, "sleep"):
        wait.return_value.until.return_value = unit_container
        solver.process_selected_tabs([tab])
        assert driver.execute_script.call_args_list[-1].args[1] is names[3]
        solver._process_tab_with_accumulation.assert_called_once()

        driver.execute_script.reset_mock()
        solver._process_tab_with_accumulation.reset_mock()
        solver.process_selected_tabs([{**tab, "_name_occurrence": 3}])
        assert driver.execute_script.call_count == 1  # Only the Unit was clicked.
        solver._process_tab_with_accumulation.assert_not_called()

def test_stop_after_ai_response_skips_answer_and_submit():
    solver = app.AISolver.__new__(app.AISolver)
    solver.driver = Mock()
    # 本用例测的是「AI 应答后用户停止」：页面不能表现为录音控件页 ——
    # 录音页现在会被真正跳过 AI（本轮修复），ask 不被调用、stop 也就不会置位，
    # 用例会沿着完全不同的路径走到底（原实现是靠「录音页仍误调 AI」才触发的停止）。
    solver.driver.find_elements = Mock(
        side_effect=lambda by, sel="", *a, **k: [] if "record" in str(sel).lower() else Mock())
    solver.stop_requested = Event()
    solver.processed_hashes = set()
    solver._generate_content_hash_from_direction = Mock(return_value="page")
    solver._preprocess_video_if_needed = Mock()
    solver._preprocess_audio_if_needed = Mock()
    solver.parser = Mock()
    question = SimpleNamespace(q_type=app.QuestionType.SINGLE_CHOICE, number=1)
    solver.parser.parse_all.return_value = ([question], "")
    solver._generate_questions_signature = Mock(return_value="questions")
    solver.content_handlers = []
    solver.knowledge_base = Mock()
    solver.knowledge_base.lookup.return_value = ([], [question])
    solver.prompt_builder = Mock()
    solver.ai_client = Mock()
    solver.ai_client.ask.side_effect = lambda *_, **__: (solver.request_stop(), "1. A")[1]
    solver.executor = Mock()

    assert solver._process_current_tab_content("chapter", "task", 0, 0) is False
    solver.executor.execute.assert_not_called()
    solver.executor.submit.assert_not_called()


def test_knowledge_base_hits_fill_without_calling_ai():
    """知识库命中的题按本地答案填，未命中的题才交给 AI。"""
    solver = app.AISolver.__new__(app.AISolver)
    solver.driver = Mock()
    # 本用例模拟的是没有录音控件的普通页面：recorder 探测必须返回空，
    # 否则 AI 分支会被「含录音控件」检查跳过（原 mock 返回真值，导致此用例一直失败）
    solver.driver.find_elements = Mock(
        side_effect=lambda by, sel="", *a, **k: [] if "record" in str(sel).lower() else Mock())
    solver.stop_requested = Event()
    solver.processed_hashes = set()
    solver._generate_content_hash_from_direction = Mock(return_value="page")
    solver._preprocess_video_if_needed = Mock()
    solver._preprocess_audio_if_needed = Mock()
    solver._generate_questions_signature = Mock(return_value="questions")
    solver._find_next_question_button = Mock(return_value=None)
    solver.content_handlers = []
    solver.executor = Mock()
    solver.executor.submit = Mock(return_value=False)

    hit = SimpleNamespace(q_type=app.QuestionType.SINGLE_CHOICE, number=1)
    miss = SimpleNamespace(q_type=app.QuestionType.SINGLE_CHOICE, number=2)
    solver.parser = Mock()
    solver.parser.parse_all.return_value = ([hit, miss], "")
    solver.knowledge_base = Mock()
    solver.knowledge_base.lookup.return_value = ([(hit, "B")], [miss])
    solver.prompt_builder = Mock()
    solver.ai_client = Mock()
    solver.ai_client.ask.return_value = "2. A"

    assert solver._process_current_tab_content("chapter", "task", 0, 0) is True
    filled = [call.args for call in solver.executor.execute.call_args_list]
    assert (hit, "B") in filled          # 命中项用知识库答案
    assert (miss, "A") in filled         # 未命中项用 AI 答案
    solver.ai_client.ask.assert_called_once()


def test_real_knowledge_base_answers_reach_the_executor():
    """真知识库 + 真接线：选词填空拿到本地答案，并且完全不请求大模型。

    knowledge/ 缺失时不再静默 return（那会让用例显示为通过，是假绿灯），改为显式跳过。
    """
    import os

    from knowledge_base import KnowledgeBase

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")
    if not os.path.isdir(root):
        _skip("knowledge/ 缺失，无法验证真题库接线")

    banked_words = [
        "adequate", "assigned", "closing", "collect", "conventional", "deadline",
        "due", "edited", "finals", "involved", "mediocre", "midterms",
        "preparatory", "review", "slight",
    ]

    solver = app.AISolver.__new__(app.AISolver)
    solver.driver = Mock()
    solver.stop_requested = Event()
    solver.processed_hashes = set()
    solver._generate_content_hash_from_direction = Mock(return_value="page")
    solver._preprocess_video_if_needed = Mock()
    solver._preprocess_audio_if_needed = Mock()
    solver._generate_questions_signature = Mock(return_value="sig")
    solver._find_next_question_button = Mock(return_value=None)
    solver.content_handlers = []
    solver.executor = Mock()
    solver.executor.submit = Mock(return_value=False)
    solver.ai_client = Mock()
    solver.prompt_builder = Mock()

    knowledge = KnowledgeBase(root=root, verbose=False)
    knowledge.textbook_pref = "新编大学英语 综合教程3"
    knowledge.prepare(None)
    solver.knowledge_base = knowledge

    question = SimpleNamespace(
        number=1,
        q_type=app.QuestionType.BANKED_CLOZE,
        banked_options=banked_words,
        banked_blanks=[{} for _ in range(10)],
        inputs=[],
        options=[],
        text="",
    )
    solver.parser = Mock()
    solver.parser.parse_all.return_value = ([question], "Banked cloze")

    task = "1-6 Read and practice Banked cloze"
    assert solver._process_current_tab_content("chapter", task, 0, 0) is True

    assert solver.executor.execute.call_count == 1
    filled_question, answer = solver.executor.execute.call_args_list[0].args
    assert filled_question is question
    assert answer.splitlines()[:3] == EXPECTED_REAL_KB_ANSWERS
    solver.ai_client.ask.assert_not_called()


def test_stop_interrupts_flashcard_and_video_waits():
    stop = Mock()
    stop.wait.side_effect = [False, True]
    stop.is_set.return_value = False
    button = Mock()
    button.is_displayed.return_value = True
    button.is_enabled.return_value = True
    driver = Mock()
    driver.find_element.side_effect = Exception("no disabled button")
    cards = app.FlashcardHandler(driver, stop)
    cards._find_next_button = Mock(return_value=button)
    assert cards.handle(None) is False
    button.click.assert_not_called()

    check_stop = Event()
    check_driver = Mock()
    check_question = SimpleNamespace(element=Mock())
    check_question.element.find_elements.side_effect = lambda *_: (check_stop.set(), [Mock()])[1]
    assert app.SelfCheckHandler(check_driver, check_stop).handle(check_question) is False
    check_driver.execute_script.assert_not_called()

    video_stop = Event()
    video_driver = Mock()
    video_driver.execute_script.side_effect = lambda script, *_: video_stop.set() if 'play()' in script else None
    video = app.VideoHandler.__new__(app.VideoHandler)
    video.driver = video_driver
    video.stop_requested = video_stop
    video._play_video(0)
    assert video_stop.is_set()
    assert any('pause()' in call.args[0] for call in video_driver.execute_script.call_args_list)


def test_ai_refusal_never_becomes_a_blank_answer():
    """大模型拒答时会把「无法作答…请补充原文」当答案回，绝不能填进第 1 个空。"""
    refusal = ("无法作答，因为题目中未提供需要填空的完整文章、26 个空的具体位置"
               "及每个空的首字母提示。请补充 passage 和首字母提示后，我再按格式给出答案。")
    assert app.looks_like_ai_refusal(refusal)
    assert not app.looks_like_ai_refusal("1. maintain 2. scenario 3. immersed")
    assert not app.looks_like_ai_refusal("2. A")   # 短答案不是拒答

    answers = app.AnswerExecutor._parse_banked_answer(refusal, 26)
    assert len(answers) == 26
    assert not any(answers)          # 一个空都不许填

    # 正常答案照旧按题号切分
    ok = app.AnswerExecutor._parse_banked_answer("1. dominance 2. distracted 3. admittance", 3)
    assert ok == ["dominance", "distracted", "admittance"]


def test_fill_in_prompt_carries_the_passage():
    """填空题题干只有一句指示，原文必须一起发给大模型，否则它答不了。"""
    builder = app.PromptBuilder(Mock())
    question = app.Question(
        number=1,
        text="Complete the following passage by filling in the blanks.",
        q_type=app.QuestionType.FILL_IN,
        element=None,
        inputs=[object(), object()],
        passage="Digital technology [1] our lives and the [2] is everywhere.",
    )
    prompt = "\n".join(builder._build_fill_in(question))
    assert "[1]" in prompt and "[2]" in prompt
    assert "Digital technology" in prompt


def test_discussion_board_writes_and_submits():
    """讨论板：读到题目 → 大模型写发言 → 写进输入框 → 点发表 → 验证已发出。"""
    state = {"text": "", "page": ""}
    editor = Mock()
    editor.is_displayed.return_value = True
    editor.is_enabled.return_value = True
    editor.get_attribute.side_effect = lambda name: state["text"]
    editor.text = ""          # 真机上空文本框读出来就是空串，别让 Mock 顶上来
    editor.location = {"x": 10, "y": 200}
    submit = Mock()
    submit.is_displayed.return_value = True
    submit.is_enabled.return_value = True
    submit.text = "发表"
    submit.location = {"x": 12, "y": 220}
    # 发表成功的样子：输入框清空 + 刚写的内容出现在页面上
    submit.click.side_effect = lambda: state.update(page=state["text"], text="")
    other = Mock()
    other.is_displayed.return_value = True
    other.is_enabled.return_value = True
    other.text = "取消"
    other.location = {"x": 300, "y": 220}

    driver = Mock()

    def find_elements(by, selector):
        if "textarea" in selector or "contenteditable" in selector:
            return [editor]
        if selector == "button":
            return [other, submit]
        return []

    driver.find_elements.side_effect = find_elements

    def execute_script(script, *args):
        if "innerText" in script:                    # 读页面正文
            return state.get("page", "")
        if script is app.DiscussionBoardHandler.WRITE_JS:
            state["text"] = args[1]                  # JS 把值写进输入框
            return True
        return None

    driver.execute_script.side_effect = execute_script

    ai = Mock()
    ai.ask.return_value = ("Discussion: Online learning has changed how we study. "
                           "It gives us flexible schedules and saves commuting time.")
    handler = app.DiscussionBoardHandler(driver, ai, Event())
    handler.CONFIRM_WAIT = 0.1
    handler.POST_CONFIRM_WAIT = 0.5
    question = SimpleNamespace(q_type=app.QuestionType.DISCUSSION_BOARD,
                               text="Discuss the pros and cons of online learning.")

    assert handler.can_handle(question) is True
    assert handler.handle(question) is True
    ai.ask.assert_called_once()
    submit.click.assert_called()
    # 「发过了就不再发」这条判断已按要求去掉：同一题再处理必须照做一遍
    submit.click.reset_mock()
    state["page"] = ""
    state["text"] = ""
    assert handler.handle(question) is True
    submit.click.assert_called()


def test_discussion_board_falls_back_to_nearby_button():
    """发表按钮没有文案（图标按钮）时，退回「输入框附近的按钮」也要点得到。"""
    state = {"text": "", "page": ""}
    editor = Mock()
    editor.is_displayed.return_value = True
    editor.is_enabled.return_value = True
    editor.get_attribute.side_effect = lambda name: state["text"]
    editor.text = ""
    editor.location = {"x": 0, "y": 0}
    icon_button = Mock()
    icon_button.is_displayed.return_value = True
    icon_button.is_enabled.return_value = True
    icon_button.text = ""                    # 图标按钮没有文字
    icon_button.location = {"x": 5, "y": 5}
    icon_button.click.side_effect = lambda: state.update(page=state["text"], text="")

    driver = Mock()
    driver.find_elements.side_effect = lambda by, sel: (
        [editor] if ("textarea" in sel or "contenteditable" in sel) else [])

    def execute_script(script, *args):
        if "querySelectorAll" in script:          # 找附近按钮
            return [icon_button] if args else []
        if "innerText" in script:                 # 读页面正文
            return state.get("page", "")
        if script is app.DiscussionBoardHandler.WRITE_JS:
            state["text"] = args[1]               # JS 把值写进输入框
            return True
        return None

    driver.execute_script.side_effect = execute_script

    ai = Mock()
    ai.ask.return_value = "Online learning gives students flexible schedules."
    handler = app.DiscussionBoardHandler(driver, ai, Event())
    handler.CONFIRM_WAIT = 0.1
    handler.POST_CONFIRM_WAIT = 0.5
    question = SimpleNamespace(q_type=app.QuestionType.DISCUSSION_BOARD,
                               text="Discuss online learning and write your opinion.")
    assert handler.handle(question) is True
    icon_button.click.assert_called()


def test_discussion_board_never_claims_success_when_submit_missing():
    """一个按钮都点不到时，必须老老实实报失败，不能谎报已发表。"""
    editor = Mock()
    editor.is_displayed.return_value = True
    editor.is_enabled.return_value = True
    editor.get_attribute.return_value = "B" * 80
    editor.location = {"x": 0, "y": 0}

    driver = Mock()
    driver.find_elements.side_effect = lambda by, sel: (
        [editor] if ("textarea" in sel or "contenteditable" in sel) else [])
    driver.execute_script.side_effect = lambda script, *args: (
        True if script is app.DiscussionBoardHandler.WRITE_JS
        else ([] if "querySelectorAll" in script else None))

    ai = Mock()
    ai.ask.return_value = "Online learning gives students flexible schedules."
    handler = app.DiscussionBoardHandler(driver, ai, Event())
    handler.CONFIRM_WAIT = 0.1
    handler.POST_CONFIRM_WAIT = 0.5
    question = SimpleNamespace(q_type=app.QuestionType.DISCUSSION_BOARD,
                               text="Discuss online learning and write your opinion.")
    assert handler.handle(question) is False


def test_discussion_board_box_cleared_without_posting_is_not_success():
    """回归：输入框被清空 ≠ 发表成功。

    实测踩过这个坑 —— 切换回复框/页面重渲染都会清空输入框，日志却报「已发表」，
    页面上一条新评论都没有。所以只有页面正文里出现刚写的内容才算数。
    """
    state = {"text": "", "page": ""}
    editor = Mock()
    editor.is_displayed.return_value = True
    editor.is_enabled.return_value = True
    editor.get_attribute.side_effect = lambda name: state["text"]
    editor.text = ""
    editor.location = {"x": 0, "y": 0}

    publish = Mock()
    publish.text = "发布"
    publish.is_displayed.return_value = True
    publish.is_enabled.return_value = True
    publish.location = {"x": 5, "y": 5}
    # 只清空输入框，页面上并没有出现这段内容 —— 典型的「假成功」
    publish.click.side_effect = lambda: state.update(text="")

    driver = Mock()
    driver.find_elements.side_effect = lambda by, sel: (
        [editor] if ("textarea" in sel or "contenteditable" in sel)
        else ([publish] if sel == "button" else []))
    driver.execute_script.side_effect = lambda script, *args: (
        state.get("page", "") if "innerText" in script
        else (True if script is app.DiscussionBoardHandler.WRITE_JS else None))

    ai = Mock()
    ai.ask.return_value = "Online learning gives students flexible schedules and saves time."
    handler = app.DiscussionBoardHandler(driver, ai, Event())
    handler.CONFIRM_WAIT = 0.1
    handler.POST_CONFIRM_WAIT = 0.5
    question = SimpleNamespace(q_type=app.QuestionType.DISCUSSION_BOARD,
                               text="Discuss online learning and write your opinion.")

    assert handler.handle(question) is False     # 不许谎报成功


def test_discussion_board_succeeds_only_when_text_shows_up_on_page():
    """内容出现在页面上才算发表成功。"""
    state = {"text": "", "page": ""}
    editor = Mock()
    editor.is_displayed.return_value = True
    editor.is_enabled.return_value = True
    editor.get_attribute.side_effect = lambda name: state["text"]
    editor.text = ""
    editor.location = {"x": 0, "y": 0}

    publish = Mock()
    publish.text = "发布"
    publish.is_displayed.return_value = True
    publish.is_enabled.return_value = True
    publish.location = {"x": 5, "y": 5}
    publish.click.side_effect = lambda: state.update(page=state["text"], text="")

    driver = Mock()
    driver.find_elements.side_effect = lambda by, sel: (
        [editor] if ("textarea" in sel or "contenteditable" in sel)
        else ([publish] if sel == "button" else []))
    driver.execute_script.side_effect = lambda script, *args: (
        state.get("page", "") if "innerText" in script
        else (state.update(text=args[1]) or True
              if script is app.DiscussionBoardHandler.WRITE_JS else None))

    ai = Mock()
    ai.ask.return_value = "Online learning gives students flexible schedules and saves time."
    handler = app.DiscussionBoardHandler(driver, ai, Event())
    handler.CONFIRM_WAIT = 0.1
    handler.POST_CONFIRM_WAIT = 0.5
    question = SimpleNamespace(q_type=app.QuestionType.DISCUSSION_BOARD,
                               text="Discuss online learning and write your opinion.")
    assert handler.handle(question) is True


def test_discussion_board_refuses_without_topic_or_ai():
    handler = app.DiscussionBoardHandler(Mock(), Mock(), Event())
    handler.CONFIRM_WAIT = 0.1
    empty = SimpleNamespace(q_type=app.QuestionType.DISCUSSION_BOARD, text="")
    assert handler.handle(empty) is False
    # 没接大模型时不能瞎发
    assert app.DiscussionBoardHandler(Mock(), None, Event()).handle(
        SimpleNamespace(q_type=app.QuestionType.DISCUSSION_BOARD, text="Discuss something long enough.")
    ) is False


def test_ai_refusal_triggers_page_text_retry():
    """AI 说「没给文章/题干」时，要自己抓页面正文再问一次，不能把题空着。"""
    solver = app.AISolver.__new__(app.AISolver)
    solver.stop_requested = Event()
    solver.driver = Mock()
    solver.driver.execute_script.return_value = "Reading passage body. " * 40
    solver.driver.find_elements.return_value = []
    solver._should_stop = Mock(return_value=False)
    solver.ai_client = Mock()
    solver.ai_client.ask.return_value = "1. A 2. B"

    question = SimpleNamespace(
        number=1,
        element=SimpleNamespace(text="Are you addicted to your smartphone? Rate each item."),
    )
    result = solver._ask_with_page_text([question], "Some directions", "ORIGINAL PROMPT")

    assert result == "1. A 2. B"
    sent = solver.ai_client.ask.call_args[0][0]
    assert "ORIGINAL PROMPT" in sent          # 原题面照旧带上
    assert "补充材料" in sent and "必须遵守" in sent
    assert "smartphone" in sent               # 题目容器自己的文字（题干/空格所在句子）


def test_no_page_text_means_no_pointless_retry():
    """页面上什么都抓不到时不要白发一次请求。"""
    solver = app.AISolver.__new__(app.AISolver)
    solver.stop_requested = Event()
    solver.driver = Mock()
    solver.driver.execute_script.return_value = ""
    solver.driver.find_elements.return_value = []
    solver._should_stop = Mock(return_value=False)
    solver.ai_client = Mock()

    assert solver._ask_with_page_text(
        [SimpleNamespace(number=1, element=None)], "", "P") is None
    solver.ai_client.ask.assert_not_called()


class _EmptyDriver:
    """所有选择器都落空的页面。parse_all 在这种页面上必须干净返回。"""

    def find_elements(self, by, selector):
        return []

    def find_element(self, by, selector):
        raise Exception("no such element")

    def execute_script(self, script, *args):
        return ""


def test_parse_all_returns_empty_on_blank_page():
    """回归：改讨论板分支时漏掉过 questions = []，整轮批量作答直接 NameError 崩掉。"""
    questions, directions = app.QuestionParser(_EmptyDriver()).parse_all()
    assert questions == []


def test_parse_all_discussion_page_without_topic_is_safe():
    parser = app.QuestionParser(_EmptyDriver())
    parser._is_discussion_board_page = Mock(return_value=True)
    questions, directions = parser.parse_all()
    assert questions == []


def test_extract_passage_prefers_longest_and_skips_video():
    short = SimpleNamespace(text="短", find_elements=lambda *_: [])
    longest = SimpleNamespace(text="A" * 120, find_elements=lambda *_: [])
    video = SimpleNamespace(text="B" * 300, find_elements=lambda *_: [object()])

    solver = app.AISolver.__new__(app.AISolver)
    solver.driver = Mock()
    solver.driver.find_elements.side_effect = lambda by, sel: (
        [short, longest, video] if "material" in sel else [])

    assert solver._extract_passage() == "A" * 120


def test_discussion_board_real_page_shape_submits_after_typing():
    """按实测页面走一遍：placeholder『我来评论』的 textarea + 文字按钮『发布』。

    『发布』不是 <button>、类名里没有 btn，只能用 XPath 按文字找；
    而且它受控于页面 state —— JS 塞值框里有字但它仍是禁用的，必须真敲键。
    """
    state = {"text": "", "armed": False}
    editor = Mock()
    editor.is_displayed.return_value = True
    editor.is_enabled.return_value = True
    editor.get_attribute.side_effect = lambda name: state["text"]
    editor.text = ""
    editor.size = {"width": 800, "height": 200}
    editor.location = {"x": 100, "y": 700}
    editor.send_keys.side_effect = lambda text: state.update(text=text, armed=True)

    publish = Mock()
    publish.text = "发布"
    publish.is_displayed.return_value = True
    publish.is_enabled.side_effect = lambda: state["armed"]     # 空框 / 只塞过值 → 禁用
    publish.location = {"x": 800, "y": 900}
    # 发表成功 → 输入框清空，并且刚写的内容出现在页面正文里
    publish.click.side_effect = lambda: state.update(page=state["text"], text="")

    driver = Mock()

    def find_elements(by, selector):
        if by == "css selector":
            return [editor] if "我来评论" in selector else []    # 页面上没有 <button>
        if by == "xpath":
            return [publish] if "'发布'" in selector else []
        return []

    def execute_script(script, *args):
        if "innerText" in script:                # 读页面正文
            return state.get("page", "")
        if script is app.DiscussionBoardHandler.WRITE_JS:
            state["text"] = args[1]          # JS 写值：看得见，但页面 state 不认
            return True
        return None

    driver.find_elements.side_effect = find_elements
    driver.execute_script.side_effect = execute_script

    ai = Mock()
    ai.ask.return_value = ("Online learning gives us flexible schedules and saves commuting "
                           "time, so I can arrange my study in a way that suits me.")
    handler = app.DiscussionBoardHandler(driver, ai, Event())
    handler.CONFIRM_WAIT = 0.1
    handler.POST_CONFIRM_WAIT = 0.5
    question = SimpleNamespace(q_type=app.QuestionType.DISCUSSION_BOARD,
                               text="What will you do to improve your digital literacy?")

    assert handler.handle(question) is True
    editor.send_keys.assert_called()        # 触发了真实键盘输入
    publish.click.assert_called()           # 并且点到了那个文字按钮


def test_discussion_board_ignores_reply_and_like_links():
    """帖子下面的「回复（0）」「点赞（0）」离输入框更近，但绝不能当发表键点。

    实测就是被它们抢先点了：评论框切进「回复 某某」模式，正文发不出去。
    """
    state = {"text": "", "page": ""}
    editor = Mock()
    editor.is_displayed.return_value = True
    editor.is_enabled.return_value = True
    editor.get_attribute.side_effect = lambda name: state["text"]
    editor.text = ""
    editor.size = {"width": 800, "height": 200}
    editor.location = {"x": 100, "y": 900}

    def link(label, x, y):
        element = Mock()
        element.text = label
        element.is_displayed.return_value = True
        element.is_enabled.return_value = True
        element.location = {"x": x, "y": y}
        return element

    reply = link("回复（0）", 120, 905)     # 离输入框最近
    like = link("点赞（0）", 200, 906)
    publish = link("发布", 800, 950)       # 真正的发表键，稍远
    publish.click.side_effect = lambda: state.update(page=state["text"], text="")

    all_buttons = [reply, like, publish]
    driver = Mock()

    def find_elements(by, selector):
        if by == "css selector":
            if "我来评论" in selector:
                return [editor]
            if selector == "button":
                return all_buttons
            return []
        if by == "xpath":
            return [publish] if "'发布'" in selector else []
        return []

    def execute_script(script, *args):
        if "innerText" in script:                 # 读页面正文
            return state.get("page", "")
        if script is app.DiscussionBoardHandler.WRITE_JS:
            state["text"] = args[1]
            return True
        return None

    driver.find_elements.side_effect = find_elements
    driver.execute_script.side_effect = execute_script

    ai = Mock()
    ai.ask.return_value = "Online learning gives us flexible schedules and saves time."
    handler = app.DiscussionBoardHandler(driver, ai, Event())
    handler.CONFIRM_WAIT = 0.1
    handler.POST_CONFIRM_WAIT = 0.5
    question = SimpleNamespace(q_type=app.QuestionType.DISCUSSION_BOARD,
                               text="How will you improve your digital literacy?")

    assert handler.handle(question) is True
    publish.click.assert_called()
    reply.click.assert_not_called()      # 没被回复链接抢走
    like.click.assert_not_called()


def test_finish_button_clicked_after_video():
    """视频看完之后底部还有「提交」，必须点掉，否则 U校园 那边任务不算完成。"""
    submit = Mock()
    submit.is_displayed.return_value = True
    submit.is_enabled.return_value = True
    submit.text = "提交"

    driver = Mock()
    driver.find_elements.side_effect = lambda by, sel: (
        [submit] if (sel == "button" or (by == "xpath" and "'提交'" in sel)) else [])

    executor = app.AnswerExecutor(driver)
    assert executor.click_finish_button() is True
    submit.click.assert_called()


def test_finish_button_absent_is_quiet():
    """页面上没有提交/发布/保存按钮时，安静返回 False，不许乱点。"""
    other = Mock()
    other.is_displayed.return_value = True
    other.is_enabled.return_value = True
    other.text = "下一题"

    driver = Mock()
    driver.find_elements.side_effect = lambda by, sel: [other] if sel == "button" else []
    executor = app.AnswerExecutor(driver)
    assert executor.click_finish_button() is False
    other.click.assert_not_called()



def test_harvest_guard_uses_resolved_book_and_never_crashes():
    """收割的串书保护要比「题库文件那本书」的名字，且不能因为变量没定义就崩。

    线上曾抛 NameError: name 'book' is not defined —— 页面判了 100 分、答案没写进题库，
    自动处理还整个中断了。
    """
    import answer_harvest
    import main
    from main import AISolver

    solver = object.__new__(AISolver)
    solver.config = None
    solver.driver = object()
    solver._last_applied = [(1, "trivial")]

    class _Book:
        name = "新视野大学英语 读写教程3"
        path = r"knowledge\新视野大学英语 读写教程3.md"

    class _KB:
        _book = _Book()

    solver.knowledge_base = _KB()
    solver._page_visible_text = lambda limit=1000: "新视野大学英语（第四版）读写教程3 > Let's go"

    calls = []
    # Helper 是 `from answer_harvest import harvest as harvest_answers` 引入的，
    # 打桩要打在 Helper 自己的绑定上，否则会真的去写题库文件
    orig_harvest = main.harvest_answers
    orig_read = answer_harvest.read_score_summary
    answer_harvest.read_score_summary = lambda driver: {
        "correct": 10, "total": 10, "score": 100, "ratio": 1.0}
    main.harvest_answers = (
        lambda book_path, unit, task, answers, summary, **_kwargs:
        calls.append(("write", book_path, list(answers))) or True)
    try:
        assert solver._harvest_if_all_correct([], "Banked cloze", 3) is True
        assert calls and calls[0][1].endswith("读写教程3.md"), calls

        # 页面是另一本书（视听说教程3）时，绝不能把答案写进读写教程3 的文件
        solver._page_visible_text = lambda limit=1000: "新视野大学英语（第四版）视听说教程3"
        assert solver._harvest_if_all_correct([], "Banked cloze", 3) is False
        assert len(calls) == 1, "页面不是这本书时不得收录"
    finally:
        main.harvest_answers = orig_harvest
        answer_harvest.read_score_summary = orig_read


def test_translation_page_with_only_a_textarea_is_parsed():
    """「Translate the following paragraph into Chinese」这种翻译页：

    只有一行 textarea（question-inputbox-input）和一段正文，既没有 scoop 也没有选项，
    旧条件要求 scoop 或写作关键词，于是整页报「没有匹配的策略」被跳过。
    """

    class _El:
        def __init__(self, text="", rows=None):
            self.text = text
            self._rows = rows

        def get_attribute(self, name):
            return self._rows if name == "rows" else None

        def is_displayed(self):
            return True

    class _Container:
        """按选择器返回元素，模拟段落翻译页的 DOM。"""

        def __init__(self, with_translate_directions=True, with_inputbox=True):
            self.translate = with_translate_directions
            self._with_inputbox = with_inputbox

        def find_elements(self, by, selector):
            if selector.startswith("textarea"):
                return [_El(rows="5")]
            if ".question-inputbox" in selector:
                return [_El()] if self._with_inputbox else []
            return []

        def find_element(self, by, selector):
            # 生产代码的 safe_find_element 走 WebDriverWait：抛 TimeoutException 让它
            # 立刻判定「没找到」，不真等 5 秒。旧版测试只实现了 find_elements，
            # 生产代码改用 find_element 后这里必崩（本次修复：补上本方法）。
            from selenium.common.exceptions import TimeoutException
            raise TimeoutException("mock: not found")

        def get_attribute(self, name):
            return "layout-container"

    from main import TextInputStrategy

    class _Driver:
        def find_element(self, by, selector):
            return _El("Translate the following paragraph into Chinese.")

    strategy = TextInputStrategy()
    assert strategy.can_parse(_Container(), _Driver()) is True

    class _DriverNoTranslate:
        def find_element(self, by, selector):
            raise Exception("no direction element")

    # 没有翻译字样时，翻译分支不认领；但「一行 textarea + 输入框、无选项/scoop/表格」
    # 现在由通用自由作答分支按设计认领（2026 按真实日志加入的兜底）→ 整体仍为 True。
    assert strategy.can_parse(_Container(False), _DriverNoTranslate()) is True

    # 真正不该被认领的场景：连输入框都没有 → 所有分支都不成立
    assert strategy.can_parse(_Container(False, with_inputbox=False), _DriverNoTranslate()) is False


def test_critical_methods_exist():
    """回归：打补丁时误删过 _vision_chat、把 _pairing_to_order 放错类，都在运行中崩过。

    这里按「方法实际所在的类」点名，缺一个就失败。
    """
    solver = app.AISolver.__new__(app.AISolver)
    for name in ("_vision_chat", "_vision_pair_answer", "_vision_read_answer",
                 "_ask_with_page_text", "_refill_unfilled", "_unfilled_questions",
                 "_question_body_text", "_page_visible_text", "_extract_passage",
                 "_parse_name_lines"):
        assert callable(getattr(solver, name, None)), f"AISolver 缺少 {name}"

    executor = app.AnswerExecutor(None)
    for name in ("click_finish_button", "finish_task_rounds",
                 "_invert_sorting_order", "_pairing_to_order", "_fill_sorting",
                 "_apply_sorting_by_drag"):
        assert callable(getattr(executor, name, None)), f"AnswerExecutor 缺少 {name}"

    strategy = app.SortingStrategy()
    assert callable(getattr(strategy, "_image_hint", None)), "SortingStrategy 缺少 _image_hint"


def test_blank_answers_strip_rule_lines():
    """AI 答案尾部的分隔线不能跟进最后一个空（实测：最后空被填成 occupation ----…）。"""
    raw = ("【答案】\n1. ticking away\n2. antique clock restoration\n3. work on\n"
           "4. repairing them\n5. put the spotlight\n6. occupation\n" + "-" * 50)
    answers = app.AnswerExecutor._parse_banked_answer(raw, 6)
    assert answers[0] == "ticking away", answers
    assert answers[5] == "occupation", answers
    # 纯分隔行不能占位成答案（降级切分路径）
    fallback = app.AnswerExecutor._parse_banked_answer("alpha\n--------\nbeta", 2)
    assert "-----" not in " ".join(fallback), fallback


def test_harvest_entries_split_numbered_lines():
    """收录前把「1 条大字符串」拆回独立答案——否则题库条数与页面空数对不上，
    收录的小节置信度卡在 low，收录了也不会被调用（实测踩过）。"""
    out = app.clean_harvest_entries(
        ["1. ticking away\n2. antique clock restoration\n3. work on", "B"])
    assert out == ["ticking away", "antique clock restoration", "work on", "B"], out
    # 分隔线被清掉
    assert app.clean_harvest_entries(["alpha\n--------\nbeta"]) == ["alpha beta"]
    # 空串/None 安全
    assert app.clean_harvest_entries([None, "", "  "]) == []


def test_merged_legacy_answer_parses_into_blanks():
    """旧收录块的合并串（自带编号「1. x 2. y …」）交回执行器后，
    必须能按编号拆进各个空——组装器对合并串是原样交回的。
    「空1：x 空2：y」形态（实测旧块）同样要能拆。"""
    out = app.AnswerExecutor._parse_banked_answer(
        "1. ticking away 2. antique clock restoration 3. work on", 3)
    assert out[0] == "ticking away", out
    assert out[1] == "antique clock restoration", out
    assert out[2] == "work on", out

    out2 = app.AnswerExecutor._parse_banked_answer(
        "【答案】 空1：ticking away 空2：antique clock restoration "
        "空3：work on 空4：repairing them 空5：put the spotlight 空6：occupation", 6)
    assert out2[0] == "ticking away", out2
    assert out2[5] == "occupation", out2


def test_transcript_context_scoped_to_task():
    """视听转写按任务边界取：联系上下文作答只带本任务的转写，
    上一个任务的不串进来（对话历史会随任务切换重置，转写必须独立保存）。"""
    client = app.OpenAICompatibleClient.__new__(app.OpenAICompatibleClient)
    client._transcripts = []
    client._task_boundary = 0
    client.accumulated_passages = set()
    client.conversation_history = []

    client.add_video_transcript_if_new("A" * 100)
    client.mark_task_boundary()
    client.add_audio_transcript_if_new("B" * 100)

    ctx = client.recent_transcript_context()
    assert ("B" * 100) in ctx, ctx[:120]
    assert ("A" * 100) not in ctx, "上一个任务的转写不应串进本任务提示词"
    assert client.has_recent_transcript()
    # 没有新转写的任务返回空（此时才轮到联网搜索）
    client.mark_task_boundary()
    assert not client.has_recent_transcript()
    assert client.recent_transcript_context() == ""


def test_phrase_pairs_lookup_for_vocab_translation():
    """括号汉译英联系上一小节（Vocabulary 跟读页）作答：跟读页每行是

    「英文表达 + 中文翻译」成对出现，存成中英词组词典；翻译题的括号中文
    与它原样对上时直接照抄 —— 词组页没有上下文锚点，对齐截取用不上，
    只能查词典（实测 AI 会把 cluster of wooden palaces 改写成 palace complex）。
    词典存的是词组原形，返回前按整题时态做轻量变形（原形动词→过去式、
    be → was/were；名词/形容词开头绝不动）。
    """
    import shutil
    import tempfile

    import phrase_extract as pe

    tmp = tempfile.mkdtemp(prefix="phrase_pairs_")
    try:
        pe.save_pairs(
            [("最大规模的木质结构宫殿群", "the largest cluster of wooden palaces"),
             ("阻止火势蔓延", "prevent a blaze from spreading"),
             ("铭刻在人们的记忆中", "be etched in the memory of people"),
             ("吉祥缸", "auspicious tanks")],
            key="read_aloud", kb_root=tmp)

        # 整题含过去式迹象（needed/prevented）→ 原形动词变形过去式、be→was；
        # 名词短语 the largest cluster… 与 auspicious tanks 不动
        question = ("1. As （最大规模的木质结构宫殿群） in the world, it needed protection "
                    "from fire. 2. (吉祥缸) stood in the courtyard. "
                    "3. which (阻止火势蔓延). 4. The Palace (铭刻在人们的记忆中).")
        hits = pe.extract_with_fallback(question, kb_root=tmp)
        got = {cn: en for cn, en, _src in hits}
        assert got.get("最大规模的木质结构宫殿群") == "the largest cluster of wooden palaces", got
        assert got.get("阻止火势蔓延") == "prevented a blaze from spreading", got
        assert got.get("铭刻在人们的记忆中") == "was etched in the memory of people", got
        assert got.get("吉祥缸") == "auspicious tanks", got

        # 重复保存不堆积（按归一中文去重）
        pe.save_pairs([("最大规模的木质结构宫殿群", "the largest cluster of wooden palaces")],
                      key="read_aloud", kb_root=tmp)
        import json
        import os
        data = json.load(open(pe.pairs_path(tmp), encoding="utf-8"))
        assert len(data["read_aloud"]) == 4, data
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_whisper_model_configurable():
    """whisper_model 配置项：默认 base，可配 small（base 会把 French horns
    听成 French homes，视听题答错就是这么来的）。"""
    cfg = app.Config(
        url="u", username="u", password="p", api_key="k", token_full="",
        base_url="b", model="m", temperature=0.3, max_tokens=1)
    assert cfg.whisper_model == "base"


def test_choice_strategy_ignores_hidden_leftover_containers():
    """SPA 残留 DOM：隐藏的旧 choice 容器不能让 can_parse 放弃——

    以前只要 .question-common-abs-choice 总数 >1 就拒绝，多选陈述页
    （DOM 自带 multipleChoice 类）被 TextInput 抢走、解析成 TEXT。
    """
    visible_choice = Mock()
    visible_choice.is_displayed.return_value = True
    visible_choice.find_elements.return_value = [Mock(), Mock()]  # 2 个 option
    hidden_choice = Mock()
    hidden_choice.is_displayed.return_value = False

    container = Mock()
    container.tag_name = "div"
    container.get_attribute.return_value = "layout-container"

    def fake_find(_by, selector):
        if selector == ".question-common-abs-choice":
            return [hidden_choice, visible_choice]
        return []

    container.find_elements.side_effect = fake_find

    strategy = app.StandardChoiceStrategy()
    assert strategy.can_parse(container, Mock()) is True, "隐藏残留不应阻止认领可见容器"

    # 两个都可见时仍拒绝（一次只解析一题，不猜）
    visible_choice.is_displayed.return_value = True
    hidden_choice.is_displayed.return_value = True
    assert strategy.can_parse(container, Mock()) is False


def test_multi_choice_answer_extracts_separated_letters():
    """多选题答案提取：选项实际字母集做白名单（实测有到 J 的 10 选项题，

    旧版只认 A-H，AI 明明答对 ACDGJ 却被丢成空答案）；
    散文（混入白名单外的字母/数字）必须拒绝，绝不把单词拆成选项。
    """
    extract = app.AISolver._extract_single_answer
    allowed = set("ABCDEFGHIJ")
    assert extract(object(), "1.ACDGJ", 1, allow_separated=True,
                   allowed_letters=allowed) == "ACDGJ"
    assert extract(object(), "1. A C E", 1, allow_separated=True,
                   allowed_letters=allowed) == "ACE"
    assert extract(object(), "1. A, C and E", 1, allow_separated=True,
                   allowed_letters=allowed) == "ACE"
    # 散文：含白名单外的字母/数字 → 拒绝（返回空，交给补做）
    assert extract(object(), "1. The true statements are 1, 3, 4, and 5", 1,
                   allow_separated=True, allowed_letters=allowed) == ""
    assert extract(object(), "1. BD", 1) == "BD"
    # 单选不放宽：带分隔的写法不被拆成多字母
    assert extract(object(), "1. A C E", 1) == "A"


def test_markdown_wrapped_fill_answers_are_cleaned():
    """AI 用 markdown 标答案时，修饰符必须剥掉再填。

    实测（截图）：时间轴页 7 个空全被填成 **takes a shower**（星号原样进输入框）
    → 页面 0/7 判错，明明答对了却整页作废。
    """
    clean = app.AnswerExecutor._clean_extracted_answer
    assert clean("**takes a shower**") == "takes a shower"
    assert clean("*coming in*") == "coming in"
    assert clean("`snack time`") == "snack time"
    assert clean("__have a little nap__") == "have a little nap"
    assert clean("“take the children home”") == "take the children home"
    answers = app.AnswerExecutor._parse_banked_answer(
        "答案：\n1) **takes a shower**\n2) **switches on the lights**\n3) **coming in**", 3)
    assert answers == ["takes a shower", "switches on the lights", "coming in"], answers


def test_labeled_multi_choice_answers_are_extracted():
    """AI 带标签作答（「多选题：BCD」「答案：B C D」）也要能提取。

    实测：Julius 页 AI 答对 BCD、却因标签格式被丢成「题目 1 无答案」→ 0/1 填写。
    散文里带冒号则必须拒绝——绝不把句子拆成选项字母。
    """
    extract = app.AISolver._extract_single_answer
    allowed = set("ABCDEF")
    assert extract(object(), "多选题：BCD", 1, allow_separated=True,
                   allowed_letters=allowed) == "BCD"
    assert extract(object(), "答案：B C D", 1, allow_separated=True,
                   allowed_letters=allowed) == "BCD"
    assert extract(object(), "**多选题：BCD**", 1, allow_separated=True,
                   allowed_letters=allowed) == "BCD"
    assert extract(object(), "答案：The correct ones are B and D.", 1,
                   allow_separated=True, allowed_letters=allowed) == ""
    # 白名单外的字母（页面只到 F）照旧拒绝，交回补做
    assert extract(object(), "答案：G", 1, allow_separated=True,
                   allowed_letters=allowed) == ""


def test_people_matching_detector_and_vision_helpers():
    """「看视频认人」链路的三块地基：题型识别、抽帧网格、逐帧认人结果解析。"""
    import video_faces

    # ① 题型识别：下拉 + 选项是单字母照片编号 + 要求里说到 people → 认人页
    def q(blank_options, text, q_type="DROPDOWN_SELECT"):
        return SimpleNamespace(
            q_type=SimpleNamespace(name=q_type),
            banked_blanks=[{"index": i, "context": f"{i + 1}. item",
                            "options": list(blank_options)}
                           for i in range(3)],
            directions=text, text="")
    assert app.AISolver._looks_like_people_matching(
        q("ABCDE", "Look at the people and choose the people for the answers."))
    # 普通下拉（选项是词）不算认人页
    assert not app.AISolver._looks_like_people_matching(
        q(["practical", "DIY"], "Complete the paragraph with the words."))
    # 不是下拉题也不算
    assert not app.AISolver._looks_like_people_matching(
        q("ABCDE", "choose the people", q_type="MULTIPLE_CHOICE"))
    # 选项抓不到（认人页的选择项藏在点击才渲染的菜单里，实测 banked_options 为空）
    # 时也要照样认出「认人页」——否则整条视频链路都不会启动
    assert app.AISolver._looks_like_people_matching(
        q([], "Look at the people and choose the people for the answers."))

    # ② 抽帧网格：铺满全片、上限内
    times = video_faces.grid_times(80.0, step=4.0, max_frames=20)
    assert times and times[0] < 4.0 and times[-1] < 80.0
    assert len(video_faces.grid_times(600.0, step=4.0, max_frames=20)) <= 20

    # ③ 「帧号=字母」解析：容忍 markdown/全角/大小写，白名单外丢弃
    letters = video_faces.parse_letter_lines(
        "1=A\n**2 = b**\n3：?\n4=E\n5=Z\n6=C", "ABCDE")
    assert letters == {1: "A", 2: "B", 4: "E", 6: "C"}, letters

    # ④ 句子↔台词：重合唯一才认，并列/不足返回 None
    segments = [(0.0, 5.0, "I am good at solving emotional problems for family"),
                (6.0, 9.0, "I am not good at repairing cars at all")]
    assert video_faces.match_statement_to_segment(
        "1. solving emotional problems", segments) == 0
    assert video_faces.match_statement_to_segment(
        "5. solving quantum mechanics", segments) is None


def test_face_evidence_only_lists_confirmed():
    """给 AI 的画面证据只许列「确认无误」的题，未确认的不得混进去。"""
    text = app.AISolver._build_face_evidence([(1, "E"), (3, "C")], 4, "ABCDE")
    assert "第 1 题：画面里是照片 E" in text
    assert "第 3 题：画面里是照片 C" in text
    assert "第 2 题：画面里是照片" not in text
    assert "第 2、4 题画面没能确认" in text
    assert app.AISolver._build_face_evidence([], 4, "ABCDE") == ""


def test_harvest_splits_inline_letter_answers():
    """一行写完整页的空（「选词/选择填空: 1.E 2.A …」）要拆成逐空字母。

    实测下拉选人页收录成 1 条整串 → 条数对不上页面 → 收录了却调不动。
    """
    entries = app.clean_harvest_entries(["选词/选择填空: 1.E 2.A 3.C 4.B 5.D"])
    assert entries == ["E", "A", "C", "B", "D"], entries
    # 散文答案里的编号不被误拆（字母之外还有正文时保持原样）
    prose = app.clean_harvest_entries(["1. adequate 2. slight"])
    assert prose == ["1. adequate 2. slight"], prose
    assert all(entry not in "ABCDEFGH" for entry in prose)


def test_letter_answers_survive_wordbank_gate_on_dropdown():
    """认人页（下拉选照片编号）的收录块必须能过「词库校验」闸门。

    实测：页面选项藏在点击才渲染的菜单里 → banked_options 是空的 → 旧逻辑
    「验不了就不放行」把已收录的 8 条字母答案整个挡回 AI（用户报「收录了调不动」）。
    字母答案 + 下拉题 = 放行；选词填空（BANKED_CLOZE）照旧不放行。
    """
    import os
    import shutil
    import tempfile

    from knowledge_base import KnowledgeBase

    tmp = tempfile.mkdtemp(prefix="kb_letters_")
    try:
        book = os.path.join(tmp, "某教材 视听说教程9.md")
        prose = "\n\n".join(
            f"## Section X · Prose page {i}\n\n"
            "This is a long transcript of the textbook page without numbered answers.\n"
            for i in range(4))
        with open(book, "w", encoding="utf-8") as fh:
            fh.write(
                "# 某教材 视听说教程9\n\n" + prose + "\n\n"
                "## 浏览器核对收录（Unit 7）\n\n"
                "### Exercise 4\n\n"
                "指纹：answers | podcast | choose | people\n"
                "1) E\n2) A\n3) C\n4) B\n")
        kb = KnowledgeBase(root=tmp, enabled=True, verbose=False)
        kb.textbook_pref = "某教材 视听说教程9"
        kb.prepare(None)
        assert not kb._book.answer_ready

        def dropdown():
            return SimpleNamespace(
                number=1, q_type=SimpleNamespace(name="DROPDOWN_SELECT"),
                text="Look at the people and choose the people for the answers.",
                options=[], inputs=[], banked_options=[],
                banked_blanks=[{"index": i, "context": f"{i + 1}. statement",
                                "options": [], "element": None} for i in range(4)])

        hits, misses = kb.lookup(
            [dropdown()], tab_name="Exercise 4",
            directions="Look at the people and choose the people for the answers.",
            unit=7)
        assert hits and not misses, f"字母答案应过闸门：{hits} {misses}"
        assert hits[0][1].replace("\n", " ").startswith("1. E"), hits[0][1]

        # 选词填空页 + 字母答案（词库也抓不到）→ 照旧拦下，交回 AI
        cloze = SimpleNamespace(
            number=1, q_type=SimpleNamespace(name="BANKED_CLOZE"),
            text="Complete the passage with the words.", options=[], inputs=[],
            banked_options=[], banked_blanks=[{} for _ in range(4)])
        hits2, misses2 = kb.lookup(
            [cloze], tab_name="Exercise 4",
            directions="Look at the people and choose the people for the answers.",
            unit=7)
        assert not hits2, f"选词填空页不该吃字母答案：{hits2}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_harvest_inserts_block_into_its_unit_area():
    """收割必须把新块写进它自己 Unit 的收录区，而不是文件末尾。

    以前一律追加到文件尾 → 块被解析成「文件里最后一个 Unit」，下次匹配被 Unit
    硬过滤挡掉（用户实测：明明收录了却调不到题库）。这里验证新块落在 Unit 6
    的区里、且解析回来就挂在 Unit 6 名下。
    """
    import os
    import shutil
    import tempfile

    import answer_harvest
    from knowledge_base import KnowledgeBase

    tmp = tempfile.mkdtemp(prefix="harvest_area_")
    try:
        path = os.path.join(tmp, "某教材 视听说教程9.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "# 某教材\n\n## 浏览器核对收录（Unit 6）\n\n"
                "> 页面确认全对后收录。\n\n### Exercise 4\n\n1) alpha\n"
                "\n\n## 浏览器核对收录（Unit 7）\n\n"
                "> 页面确认全对后收录。\n\n### Exercise 1\n\n1) beta\n")
        assert answer_harvest.harvest(path, 6, "Exercise 3", ["gamma"],
                                      {"score": 100},
                                      fingerprint=["ticking", "restoration"])
        body = open(path, encoding="utf-8").read()
        area6 = body.split("## 浏览器核对收录（Unit 7）")[0]
        assert "### Exercise 3" in area6, "新块应插进 Unit 6 的区里"
        kb = KnowledgeBase(root=tmp, enabled=True, verbose=False)
        sections = [s for s in kb.books[0].sections if s.task == "Exercise 3"]
        assert sections and sections[0].unit == 6, sections
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    tests = [
        test_answer_executor_returns_bool,
        test_selected_duplicate_task_uses_scanned_occurrence,
        test_stop_after_ai_response_skips_answer_and_submit,
        test_knowledge_base_hits_fill_without_calling_ai,
        test_real_knowledge_base_answers_reach_the_executor,
        test_stop_interrupts_flashcard_and_video_waits,
        test_ai_refusal_never_becomes_a_blank_answer,
        test_fill_in_prompt_carries_the_passage,
        test_discussion_board_writes_and_submits,
        test_discussion_board_falls_back_to_nearby_button,
        test_discussion_board_never_claims_success_when_submit_missing,
        test_discussion_board_box_cleared_without_posting_is_not_success,
        test_discussion_board_succeeds_only_when_text_shows_up_on_page,
        test_discussion_board_real_page_shape_submits_after_typing,
        test_discussion_board_ignores_reply_and_like_links,
        test_discussion_board_refuses_without_topic_or_ai,
        test_ai_refusal_triggers_page_text_retry,
        test_no_page_text_means_no_pointless_retry,
        test_parse_all_returns_empty_on_blank_page,
        test_parse_all_discussion_page_without_topic_is_safe,
        test_extract_passage_prefers_longest_and_skips_video,
        test_finish_button_clicked_after_video,
        test_finish_button_absent_is_quiet,
        test_harvest_guard_uses_resolved_book_and_never_crashes,
        test_translation_page_with_only_a_textarea_is_parsed,
        test_blank_answers_strip_rule_lines,
        test_harvest_entries_split_numbered_lines,
        test_merged_legacy_answer_parses_into_blanks,
        test_transcript_context_scoped_to_task,
        test_whisper_model_configurable,
        test_choice_strategy_ignores_hidden_leftover_containers,
        test_multi_choice_answer_extracts_separated_letters,
        test_phrase_pairs_lookup_for_vocab_translation,
        test_critical_methods_exist,
        test_markdown_wrapped_fill_answers_are_cleaned,
        test_labeled_multi_choice_answers_are_extracted,
        test_harvest_inserts_block_into_its_unit_area,
        test_people_matching_detector_and_vision_helpers,
        test_face_evidence_only_lists_confirmed,
        test_harvest_splits_inline_letter_answers,
        test_letter_answers_survive_wordbank_gate_on_dropdown,
    ]
    failed = skipped = 0
    for test in tests:
        name = test.__name__
        try:
            test()
        except BaseException as exc:  # noqa: BLE001
            # _SkipTest，或 pytest.skip 抛出的 Skipped（后者直接继承 BaseException）
            if isinstance(exc, _SkipTest) or type(exc).__name__ == "Skipped":
                skipped += 1
                print(f"SKIP {name}: {exc}")
            elif isinstance(exc, Exception):
                failed += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            else:
                raise  # KeyboardInterrupt / SystemExit 照常向上抛
        else:
            print(f"PASS {name}")
    print(f"\n{len(tests) - failed - skipped}/{len(tests)} passed, {skipped} skipped")
    raise SystemExit(1 if failed else 0)
