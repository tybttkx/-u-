from AudioRecognizer import AudioTranscriber
from EnvironmentChecker import EnvironmentChecker
from knowledge_base import KnowledgeBase, default_root as knowledge_root
import hashlib, json, logging, os, sys, random, re, tempfile, threading, time, winsound
import queue
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from glob import glob
from typing import List, Optional, Dict, Any, Tuple, Callable

from openai import OpenAI
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, StaleElementReferenceException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from fluent_ui import FluentModernGUI
from mic_permission import MicPermission, grant_browser_permission
from answer_harvest import page_verdict, harvest as harvest_answers
from web_search import build_context as web_search_context

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

gui_log_queue = queue.Queue()

DEBUG_MODE = False

APP_VERSION = "3.5.0"

#: 模块级 logger 兜底：模块被 import（测试/工具）时 __main__ 里的赋值不会执行，
#: 而很多函数在异常路径会调 logger.error —— 没有这个兜底就是 NameError。
#: setup_logging() 配置的是同名 logger，__main__ 里再赋值也只是同一对象的引用。
logger = logging.getLogger("UCampusBot")


def setup_logging():
    """配置日志系统：控制台简洁输出 + 文件详细记录 + UI队列同步"""

    def clean_all_logs(log_dir):
        """清空所有旧日志（激进模式）"""
        try:
            log_pattern = os.path.join(log_dir, 'ucampus_*.log')
            log_files = glob(log_pattern)
            for old_file in log_files:
                try:
                    os.remove(old_file)
                except Exception:
                    pass
        except Exception:
            pass

    logger = logging.getLogger('UCampusBot')
    logger.setLevel(logging.DEBUG)
    logger.handlers = []
    log_dir = os.path.join(BASE_DIR, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    clean_all_logs(log_dir)
    log_file = os.path.join(log_dir, f'ucampus_{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}.log')

    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] [%(funcName)s:%(lineno)d]\n%(message)s\n',
        datefmt='%H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    class ConsoleFilter(logging.Filter):
        def filter(self, record):
            if record.levelno >= logging.ERROR:
                record.msg = f" {record.msg}"
            return True

    console_handler.addFilter(ConsoleFilter())
    console_formatter = logging.Formatter('%(message)s')
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    class PrintRedirector:
        def __init__(self, logger, level=logging.INFO):
            self.logger = logger
            self.level = level
            self.buffer = ""

        def write(self, text):
            if text.strip():
                if any(x in text for x in ('Error', 'Exception', 'Traceback')):
                    self.logger.error(text.strip())
                elif any(x in text for x in ('Warning',)):
                    self.logger.warning(text.strip())
                else:
                    self.logger.info(text.strip())
                gui_log_queue.put(text.strip())

        def flush(self):
            pass

    sys.stdout = PrintRedirector(logger)
    return logger


def _fake_mic_wav() -> str:
    """浏览器假麦克风读的 WAV（跟读题每次录音前由 audio_cache.to_wav 覆盖它）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "knowledge", "_audio_cache", "current.wav")

@dataclass(frozen=True)
class Config:
    """不可变配置类"""
    url: str
    username: str
    password: str
    api_key: str
    token_full: str
    base_url: str
    model: str
    temperature: float
    max_tokens: int
    # 本地题库知识库（knowledge/ 目录）。命中则直接用本地答案，未命中仍交给 AI。
    knowledge_enabled: bool = True
    knowledge_textbook: str = "auto"
    knowledge_min_confidence: str = "medium"
    knowledge_verify_wordbank: bool = True
    #: 录音题要用麦克风：运行时自动放行（退出恢复），平时保持系统默认
    mic_auto_grant: bool = True
    #: 浏览器确认全对后，是否把这套答案收录进本地题库
    harvest_verified_answers: bool = True
    #: 题库没命中时的联网搜索（博查 AI）
    search_enabled: bool = True
    search_api_key: str = ""
    #: Edge 调试端口（0 = 不开）。开了之后可以用 抓页面.py 直接看页面 DOM
    debug_port: int = 9333
    #: 严格提交模式：有题没答就不提交（默认 False = 无论如何都提交）
    strict_submit_check: bool = False
    #: 答题小结达到多少分才把答案收录进题库（默认 80）
    harvest_min_score: int = 80
    #: 看图题（图片配对）用的视觉模型：实测 deepseek-v4-pro 收图，flash 不收
    vision_model: str = "qwen3.8-omni-flash"
    #: 第二个视觉模型，用来投票（两张图都认同时更可信）
    vision_model_2: str = "qwen3.8-max"
    #: 本地 Whisper 模型名：base 快但易听错（French horns→French homes），
    #: small 更准、约慢 3 倍
    whisper_model: str = "base"

    @classmethod
    def from_json(cls, path: str = "config.json") -> "Config":
        with open(path, "r", encoding="UTF-8") as f:
            data = json.load(f)
        global DEBUG_MODE
        DEBUG_MODE = data.get("debug_mode", False)
        # api_key 支持两种写法：
        #   直接填 key（sk-xxxx）
        #   填 env:变量名 或 $env:变量名 —— 从环境变量里取，key 不用落在文件里
        raw_key = (data.get("api_key") or "").strip()
        if raw_key.lower().startswith("$env:"):
            raw_key = raw_key[5:].strip()
        elif raw_key.lower().startswith("env:"):
            raw_key = raw_key[4:].strip()
        if raw_key and not raw_key.startswith(("sk-", "sk_")):
            env_value = os.environ.get(raw_key, "")
            if env_value:
                print(f" 已从环境变量 {raw_key} 读取 API key")
                data["api_key"] = env_value.strip()
            else:
                print(f" ⚠ config.json 的 api_key 写的是环境变量 {raw_key}，"
                      f"但系统里没有这个变量（值为空）")
                data["api_key"] = ""
        return cls(
            url=data.get("url"),
            username=data.get("username"),
            password=data.get("password"),
            token_full=data.get("token_full"),
            api_key=data.get("api_key"),
            base_url=data.get("base_url", "https://api.moonshot.cn/v1"),
            model=data.get("model", "kimi-k2-turbo-preview"),
            temperature=data.get("temperature", 0.3),
            max_tokens=data.get("max_tokens", 2000),
            knowledge_enabled=bool(data.get("knowledge_enabled", True)),
            knowledge_textbook=data.get("knowledge_textbook", "auto") or "auto",
            knowledge_min_confidence=data.get("knowledge_min_confidence", "medium") or "medium",
            knowledge_verify_wordbank=bool(data.get("knowledge_verify_wordbank", True)),
            mic_auto_grant=bool(data.get("mic_auto_grant", True)),
            harvest_verified_answers=bool(data.get("harvest_verified_answers", True)),
            search_enabled=bool(data.get("search_enabled", True)),
            search_api_key=data.get("search_api_key", "") or "",
            debug_port=int(data.get("debug_port", 9333) or 0),
            strict_submit_check=bool(data.get("strict_submit_check", False)),
            harvest_min_score=int(data.get("harvest_min_score", 80) or 80),
            vision_model=data.get("vision_model", "qwen3.8-omni-flash") or "qwen3.8-omni-flash",
            vision_model_2=data.get("vision_model_2", "qwen3.8-max") or "qwen3.8-max",
            whisper_model=data.get("whisper_model", "base") or "base",
        )


class QuestionType(Enum):
    """题目类型"""
    SINGLE_CHOICE = auto()
    MULTIPLE_CHOICE = auto()
    FILL_IN = auto()
    TEXT = auto()
    SORTING = auto()
    VOCABULARY_FLASHCARD = auto()
    BANKED_CLOZE = auto()
    VOCABULARY_TEST = auto()  # 词汇测试（英汉互译）
    VIDEO = auto()  # 纯视频页面
    DISCUSSION_BOARD = auto()
    #: 评分/量表题：点 1-5 数字（如 Pre-reading activities Task 1）
    SCALE_RATE = auto()
    #: 跟读/语音题：点麦克风录音，交给页面评分
    FOLLOW_READ = auto()
    SELF_CHECK = auto()
    MY_VOICE_TEXT = auto()
    DROPDOWN_SELECT = auto()
    LISTENING_FILL_IN = auto()
    LISTENING_CHOICE = auto()
    VIDEO_CHOICE = auto()


@dataclass
class Option:
    """选项"""
    letter: str
    text: str
    element: Any
    is_selected: bool = False


@dataclass
class Question:
    """题目"""
    number: int
    text: str
    q_type: QuestionType
    element: Any
    options: List[Option] = field(default_factory=list)
    inputs: List[Any] = field(default_factory=list)
    banked_options: List[str] = field(default_factory=list)
    banked_blanks: List[Dict] = field(default_factory=list)
    directions: str = ""
    #: 填空题原文，空格处已替换成 [1]…[N]。没有原文时大模型答不了「填第几个空」
    passage: str = ""

    def is_interactive(self) -> bool:
        """是否有交互元素"""
        if self.q_type in [QuestionType.VIDEO,
                           QuestionType.VOCABULARY_FLASHCARD,  # 闪卡需要交互
                           QuestionType.DISCUSSION_BOARD,
                           QuestionType.SELF_CHECK,
                           # 评分题靠点 1-5 数字作答：没有选项/输入框，但必须算交互，
                           # 否则解析出来会被当「非交互类型」丢掉，题目直接消失
                           QuestionType.SCALE_RATE,
                           QuestionType.FOLLOW_READ]:
            return True
        return bool(self.options or self.inputs or self.banked_blanks)

    @property
    def is_phrase_mode(self) -> bool:
        """检测是短语填空还是单词填空"""
        if not self.banked_options:
            return False
        phrase_count = sum(1 for opt in self.banked_options if ' ' in opt.strip() or len(opt) > 15)
        return phrase_count / len(self.banked_options) > 0.3

class Selectors:
    """CSS选择器仓库"""
    CHOICE_OPTIONS = [
        '.option.isNotReview',
        'div.option',
        '.MultipleChoice--checkbox-item-34A_-',
        'ul[class*="single-choice"] li label',
        '.option-wrap',
    ]
    OPTION_CAPTION = ['.caption', 'span[class*="index"]', '.MultipleChoice--checkbox-opt-2F4xY']
    OPTION_CONTENT = ['.component-htmlview.content', 'div.html-view[class*="content"]', '.html-view', '.content', 'p']
    QUESTION_TITLE = [
        '.ques-title',
        '.component-htmlview.ques-title',
        '.question-inputbox-header',
        '.component-htmlview',
        '.title',
        'p',
        '.question-stem',
    ]
    SUBMIT_BUTTON = [
        'button[type="submit"]',
        'button[class*="submit"]',
        'button[class*="confirm"]',
        '.submit-bar-pc--btn-1_Xvo',
        '.btns-submit button.submit-btn',
        'button.submit-btn',
        '.btn',
    ]
    LEVEL1_TABS = [
        '.pc-header-tabs-container .pc-tab-row > .tab',
        '.pc-header-tabs-container .ant-col.tab',
        '.pc-tab-row > [class*="pc-header-tab"]',
    ]
    LEVEL2_TABS = [
        '.pc-header-tasks-row > .pc-task',
        ':scope > div > div > .pc-header-tasks-row > .pc-task',
    ]


class WebDriverHelper:
    """WebDriver辅助工具类（静态方法）"""

    @staticmethod
    def safe_find_element(driver, selectors: List[str], parent=None) -> Optional[Any]:
        """安全查找单个元素"""
        search_context = parent if parent else driver
        wait = WebDriverWait(search_context, 5)
        for selector in selectors:
            try:
                element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
                if element.is_displayed():
                    return element
            except (TimeoutException, NoSuchElementException):
                continue
        return None

    @staticmethod
    def safe_find_elements(driver, selectors: List[str], parent=None) -> List[Any]:
        """安全查找多个元素"""
        search_context = parent if parent else driver
        for selector in selectors:
            try:
                elements = [
                    e for e in search_context.find_elements(By.CSS_SELECTOR, selector)
                    if e.is_displayed()
                ]
                if elements:
                    return elements
            except Exception as e:
                error_msg = str(e)
                print(f"操作失败: {error_msg[:50]}")  # 控制台只显示简短信息
                logger.error(f"详细错误: {error_msg}", exc_info=True)  # 详细堆栈保存到文件
                continue
        return []

    @staticmethod
    def human_like_delay(base_delay: float = 0.1) -> None:
        """随机延迟"""
        delay = base_delay * (0.8 + random.random() * 0.4)
        time.sleep(delay)

    @staticmethod
    def simulate_typing(driver, element, text: str) -> None:
        """模拟人类打字"""
        actions = ActionChains(driver)
        actions.move_to_element(element).click().perform()
        WebDriverHelper.human_like_delay(0.1)
        element.clear()
        WebDriverHelper.human_like_delay(0.1)
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(0.01, 0.05))
        driver.execute_script("""
            arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
            arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
            arguments[0].dispatchEvent(new Event('blur', {bubbles: true}));
        """, element)
        WebDriverHelper.human_like_delay(0.1)

    @staticmethod
    def safe_click(driver, element, retries: int = 3) -> bool:
        """安全点击元素"""
        for i in range(retries):
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                    element
                )
                time.sleep(0.3)

                try:
                    element.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", element)
                return True

            except StaleElementReferenceException:
                if i < retries - 1:
                    time.sleep(1)
                    continue
            except Exception as e:
                if i < retries - 1:
                    time.sleep(0.5)
                    continue
                error_msg = str(e)
                print(f"操作失败: {error_msg[:50]}")  # 控制台只显示简短信息
                logger.error(f"详细错误: {error_msg}", exc_info=True)  # 详细堆栈保存到文件
        return False


class OpenAICompatibleClient:
    """OpenAI兼容 API客户端 - 职责：仅处理API通信"""

    SYSTEM_PROMPT = """你是一个专业的英语教学助手，擅长分析英语题目。
请根据题目要求给出准确答案，注意区分不同题型：
- 词汇匹配题：根据英文选中文，或根据中文选英文
- 选词填空：选择最合适的单词填入
- 阅读理解：基于文章内容作答
- 听力选择题：基于音频转写内容作答
- 视频选择题：基于视频转写内容作答
- 排序题：根据材料出现顺序返回选项字母序列"""

    def __init__(self, config: Config):
        self.config = config
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url,
                              # 显式超时：SDK 默认 600 秒且会自动重试，模型一慢
                              # 日志就十几分钟没有新行，看着像卡死。宁可早点失败，
                              # 让上层走重试/换模型，也别挂着不动。
                              timeout=120, max_retries=1)
        self.conversation_history: List[Dict] = []
        self.current_chapter_id: Optional[str] = None
        self.accumulated_passages: set = set()  # 已累积的原文哈希，防重复
        #: 本任务新增的视听转写（label, text）：联系上下文作答的题直接把它进提示词。
        #: 对话历史在任务切换时会被重置，转写不能只躺在历史里。
        self._transcripts: List[Tuple[str, str]] = []
        self._task_boundary: int = 0

    def mark_task_boundary(self):
        """标记任务起点：之后 recent_transcript_context 只返回本任务的转写。"""
        self._task_boundary = len(self._transcripts)

    def has_recent_transcript(self) -> bool:
        """本任务有没有新增过音频/视频转写（有的话联网搜索不必再做）。"""
        return bool(self._transcripts[self._task_boundary:])

    def recent_transcript_context(self, max_chars: int = 6000) -> str:
        """把本任务的视听转写拼成提示词材料（联系上下文作答用）。"""
        entries = self._transcripts[self._task_boundary:]
        if not entries:
            return ""
        pieces = []
        for label, text in entries[-3:]:
            pieces.append(f"【{label}（请联系此转写内容作答）】\n{str(text)[:4000]}")
        ctx = "\n\n".join(pieces)
        return ctx[:max_chars]

    def start_new_chapter(self, chapter_id: str):
        """开始新章节，记录章节ID（不自动清空历史）"""
        self.current_chapter_id = chapter_id
        print(f" 记录章节: {chapter_id[:50]}")

    def force_reset(self, chapter_id: str):
        """强制清空所有历史，无论章节是否相同"""
        self.conversation_history = []
        self.current_chapter_id = chapter_id
        self.accumulated_passages = set()
        print(f" 强制重置章节: {chapter_id[:50]}")

    def add_passage_if_new(self, passage: str) -> bool:
        """添加原文（如果是新的），返回是否添加成功"""
        return self._add_context_if_new(passage, "阅读材料", "材料")

    def add_audio_transcript_if_new(self, transcript: str) -> bool:
        """添加音频转写（如果是新的），返回是否添加成功"""
        return self._add_context_if_new(transcript, "听力音频转写", "音频转写")

    def add_video_transcript_if_new(self, transcript: str) -> bool:
        """添加视频转写（如果是新的），返回是否添加成功"""
        return self._add_context_if_new(transcript, "视频转写", "视频转写")

    def _add_context_if_new(self, content: str, label: str, ack_label: str) -> bool:
        """添加可复用上下文，按内容哈希去重"""
        if not content or len(content) < 50:
            return False

        passage_hash = hashlib.md5(content.encode()).hexdigest()[:16]

        if passage_hash in self.accumulated_passages:
            print(f"    {ack_label}已存在，跳过")
            return False

        self.accumulated_passages.add(passage_hash)
        material_index = len(self.accumulated_passages)
        if "转写" in label:
            # 登记视听转写：联系上下文作答的题在提示词里直接引用（对话历史会
            # 随任务切换重置，这里独立保存一份）
            self._transcripts.append((label, content))

        passage_msg = {
            "role": "user",
            "content": f"【{label} {material_index}】\n\n{content}\n\n请理解以上内容，等待后续问题。"
        }
        self.conversation_history.append(passage_msg)
        self.conversation_history.append({
            "role": "assistant",
            "content": f"我已理解{ack_label} {material_index}。请提出问题。"
        })

        print(f"    新增{ack_label}（{len(content)}字符），当前共{len(self.accumulated_passages)}份上下文")
        return True

    def ask(self, prompt: str, retry_count: int = 3, stop_requested: Optional[threading.Event] = None) -> Optional[str]:
        """发送问题并获取回答"""
        print(f"当前ai对话历史共{len(self.conversation_history)}条")
        for attempt in range(retry_count):
            if stop_requested is not None and stop_requested.is_set():
                return None
            try:
                messages = [
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    *self.conversation_history,
                    {"role": "user", "content": prompt}
                ]

                if DEBUG_MODE:
                    print("\n" + "=" * 60)
                    print(" [DEBUG] API 请求详情")
                    print(f"   base_url: {self.config.base_url}")
                    print(f"   model:    {self.config.model}")
                    print(f"   temperature: {self.config.temperature}")
                    print(f"   max_tokens:  {self.config.max_tokens}")
                    print(f"   消息条数: {len(messages)}")
                    for i, msg in enumerate(messages):
                        role = msg["role"]
                        content = msg["content"]
                        preview = content[:300] + "..." if len(content) > 300 else content
                        print(f"   [{i}] {role}: {preview}")
                    print("=" * 60 + "\n")

                response = self.client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens
                )

                answer = (response.choices[0].message.content or "").strip()

                # 纯符号响应（只有分隔线/标点，没有任何实义字符）：按空响应处理，
                # 让下面的重试路径接手（实测模型会复读提示词里的「无相关结果」框）
                if answer and not re.search(r"[\w\u4e00-\u9fff]", answer):
                    print("   ⚠ AI 返回了无实义内容（仅符号/分隔线），按空响应处理")
                    answer = ""

                # 空响应：模型可能把额度全花在思考上（reasoning_content），content 就成了空串。
                # 以前这里直接把空串返回，调用方只看到「AI回答:」后面什么都没有 ——
                # 题就这么空着交了。现在先加大 max_tokens 重试，再拿思考内容兜底。
                if not answer:
                    choice = response.choices[0]
                    reasoning = str(getattr(choice.message, "reasoning_content", "") or "")
                    print(f"   ⚠ AI 返回空内容（finish_reason={choice.finish_reason}，"
                          f"思考内容 {len(reasoning)} 字）")
                    logger.warning(
                        f"AI 空响应: finish_reason={choice.finish_reason}, "
                        f"reasoning_len={len(reasoning)}")
                    bigger = max(int(self.config.max_tokens or 0) * 2, 4000)
                    if bigger > int(self.config.max_tokens or 0):
                        print(f"   ⚠ 用更大的 max_tokens={bigger} 重试一次")
                        retry_response = self.client.chat.completions.create(
                            model=self.config.model,
                            messages=messages,
                            temperature=self.config.temperature,
                            max_tokens=bigger,
                        )
                        answer = (retry_response.choices[0].message.content or "").strip()
                    if not answer and reasoning:
                        answer = reasoning.strip()
                        print("   ⚠ 仍为空，改用思考内容作为回答")
                    if not answer:
                        print("   ⚠ AI 两次都没给出内容，这一题只能留空交回")

                if DEBUG_MODE:
                    print("\n" + "=" * 60)
                    print(" [DEBUG] API 响应详情")
                    print(f"   model:        {response.model}")
                    print(f"   finish_reason:{response.choices[0].finish_reason}")
                    print(f"   usage:        {response.usage}")
                    print(f"   answer:       {answer[:500]}{'...' if len(answer) > 500 else ''}")
                    print("=" * 60 + "\n")

                self.conversation_history.append({"role": "user", "content": prompt})
                self.conversation_history.append({"role": "assistant", "content": answer})

                if len(self.conversation_history) > 22:
                    self.conversation_history = self.conversation_history[:2] + self.conversation_history[-20:]

                print(f"AI回答: {answer}")
                return answer

            except Exception as e:
                if DEBUG_MODE:
                    import traceback
                    print("\n" + "=" * 60)
                    print(" [DEBUG] API 调用异常详情")
                    print(f"   异常类型: {type(e).__name__}")
                    print(f"   异常信息: {e}")
                    if hasattr(e, 'response'):
                        try:
                            print(f"   HTTP 状态码: {e.response.status_code}")
                            print(f"   响应头: {dict(e.response.headers)}")
                            print(f"   响应体: {e.response.text[:1000]}")
                        except Exception:
                            pass
                    if hasattr(e, 'body'):
                        try:
                            print(f"   错误body: {e.body}")
                        except Exception:
                            pass
                    if hasattr(e, 'status_code'):
                        print(f"   status_code: {e.status_code}")
                    print(f"   完整堆栈:")
                    traceback.print_exc()
                    print("=" * 60 + "\n")

                if attempt < retry_count - 1:
                    delay = (2 ** attempt) + random.random()
                    if stop_requested is not None:
                        if stop_requested.wait(delay):
                            return None
                    else:
                        time.sleep(delay)
                error_msg = str(e)
                print(f"AI调用失败: {error_msg[:50]}")  # 控制台只显示简短信息
                logger.error(f"详细错误: {error_msg}", exc_info=True)  # 详细堆栈保存到文件

        return None


class QuestionParserStrategy(ABC):
    """题目解析策略基类"""

    @abstractmethod
    def can_parse(self, container, driver) -> bool:
        """是否能解析该容器"""
        pass

    @abstractmethod
    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        """解析题目"""
        pass


def collect_page_evidence(driver, limit: int = 3500) -> str:
    """把当前页面上的信息尽量完整地收集成一段文字，喂给 AI。

    用户要求：脚本要能看到浏览器上的所有信息，别让 AI 靠语义猜。收集内容包括
    题目要求/正文/表格（对照表、词库常放在表格里）、每个输入框的编号与提示
    （提示常常画在伪元素里，innerText 看不到）、选项文字与当前 URL/标题。
    """
    if driver is None:
        return ""
    script = r"""
        (function () {
          function vis(el) {
            try {
              var r = el.getBoundingClientRect();
              var s = window.getComputedStyle(el);
              return !!(r.width && r.height) && s.display !== 'none' && s.visibility !== 'hidden';
            } catch (e) { return false; }
          }
          function oneLine(s) { return (s || '').replace(/\s+/g, ' ').trim(); }
          var out = {url: location.href, title: document.title, text: '', tables: [],
                     blanks: [], options: []};

          var body = document.body ? (document.body.innerText || '') : '';
          out.text = oneLine(body).slice(0, 2500);

          var tables = document.querySelectorAll('table');
          for (var i = 0; i < tables.length && out.tables.length < 5; i++) {
            var rows = tables[i].querySelectorAll('tr');
            var lines = [];
            for (var r = 0; r < rows.length && lines.length < 20; r++) {
              var cells = rows[r].querySelectorAll('td, th');
              var parts = [];
              for (var c = 0; c < cells.length; c++) { parts.push(oneLine(cells[c].innerText)); }
              var line = parts.join(' | ');
              if (line) { lines.push(line); }
            }
            if (lines.length) { out.tables.push(lines.join('\n')); }
          }

          var scoops = document.querySelectorAll('.fe-scoop');
          for (var k = 0; k < scoops.length && out.blanks.length < 40; k++) {
            var scoop = scoops[k];
            var input = scoop.querySelector('input, textarea');
            if (!input) { continue; }
            var numEl = scoop.querySelector('.question-number');
            var info = {n: numEl ? oneLine(numEl.innerText) : String(k + 1), hint: ''};
            var bits = [];
            try {
              var b = window.getComputedStyle(input, '::before').content || '';
              var a = window.getComputedStyle(input, '::after').content || '';
              if (b && b !== 'none') { bits.push(b.replace(/["']/g, '')); }
              if (a && a !== 'none') { bits.push(a.replace(/["']/g, '')); }
            } catch (e) {}
            var names = ['data-first-letter', 'data-hint', 'placeholder', 'title',
                         'aria-label', 'data-answer', 'data-word'];
            for (var j = 0; j < names.length; j++) {
              var v = input.getAttribute(names[j]);
              if (v) { bits.push(names[j] + '=' + v); }
            }
            info.hint = oneLine(bits.join(' ')).slice(0, 60);
            out.blanks.push(info);
          }

          var opts = document.querySelectorAll('.option, .ant-select-selection-item, option');
          for (var m = 0; m < opts.length && out.options.length < 30; m++) {
            var t = oneLine(opts[m].innerText || opts[m].textContent);
            if (t && t.length < 60) { out.options.push(t); }
          }
          return JSON.stringify(out);
        })()
    """
    try:
        raw = driver.execute_script(script)
    except Exception:
        return ""
    try:
        import json as _json
        data = _json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return ""

    parts = ["【页面完整信息（脚本自动采集）】"]
    if data.get("title"):
        parts.append(f"标题：{data['title']}")
    if data.get("text"):
        parts.append("可见文字：" + data["text"][:1200])
    for i, table in enumerate(data.get("tables") or [], 1):
        parts.append(f"表格{i}（逐行）：\n{table[:800]}")
    blanks = data.get("blanks") or []
    if blanks:
        lines = [f"{b.get('n')}. ______" + (f"   （提示：{b['hint']}）" if b.get("hint") else "")
                 for b in blanks]
        parts.append("填空列表（含页面提示）：\n" + "\n".join(lines))
    if data.get("options"):
        parts.append("页面上的选项/词条：" + "；".join(data["options"][:25]))
    text = "\n".join(parts)
    return text[:limit]


class GenericFallbackStrategy(QuestionParserStrategy):
    """兜底策略：认领一切「有作答界面但没人认得」的题，交给 AI 作答，绝不放弃。

    用户策略（2026-09-24）：找不到题库也必须用 AI（联网搜索）作答，不能因为
    「没有匹配的策略」就整页跳过。它永远排在策略列表最后，前面各管一类，它只管剩下
    的：只要有输入界面（textarea / 文本框 / 下拉 / 选项 / 可编辑区），就按「自由作答」
    建一道 TEXT 题，题干带上题目要求和页面内容，交给 AI。
    """

    INPUT_SELECTORS = (
        'textarea', 'input', '.question-inputbox-input',
        '.ant-select', '.fe-scoop', '[contenteditable="true"]', '[contenteditable=""]',
        '.blank', '[class*="fill-blank"]', '.option',
    )

    def _input_elements(self, container):
        found = []
        for selector in self.INPUT_SELECTORS:
            try:
                elements = container.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                try:
                    if element in found or not element.is_displayed():
                        continue
                except Exception:
                    continue
                found.append(element)
        return found

    @staticmethod
    def _label_blanks(container, fillable):
        """给每个可填元素找它的空号，按编号去重排序。

        空号来自 .fe-scoop 里的 .question-number（有的页面只有 data-scoop-index）。
        DOM 会把滚动区里还没露出来的空一起渲染出来，input 数因此多于页面上的空数
        （实测 17 个 input，页面上只有 8 个空）：按编号去重后只问真正的那些空，并保证
        「第 N 个答案填进第 N 号空」。取不到编号的按顺序排在后面，不丢空。
        """
        labeled, unlabeled = [], []
        for el in fillable:
            label = ""
            try:
                number = el.find_element(
                    By.XPATH,
                    'ancestor::span[contains(@class,"fe-scoop")]'
                    '//span[contains(@class,"question-number")]')
                label = (number.text or "").strip()
            except Exception:
                label = ""
            if not label:
                try:
                    scoop = el.find_element(
                        By.XPATH, 'ancestor::*[contains(@class,"fe-scoop")][1]')
                    idx = (scoop.get_attribute("data-scoop-index") or "").strip()
                    if idx.isdigit():
                        label = str(int(idx) + 1)
                except Exception:
                    label = ""
            if label:
                labeled.append((label, el))
            else:
                unlabeled.append(el)

        def as_int(value):
            return int(value) if value.isdigit() else 10 ** 6

        unique = {}
        for label, el in sorted(labeled, key=lambda item: as_int(item[0])):
            unique.setdefault(label, el)
        if unique:
            # 页面上有带编号的空时，只认这些：实测同页还有「笔记框」「搜索框」等
            # 无编号的 input（不可见、没有 .question-number），把它们也编上号就会把
            # 8 个空算成 17 个（用户看到的 8 个才是真的）。
            return [(label, el) for label, el in unique.items()]
        return [(str(i + 1), el) for i, el in enumerate(unlabeled)]

    @staticmethod
    def _blank_hints(driver, blanks):
        """把每个空格子的「提示」从 DOM 里挖出来。

        这类题的标准答案受首字母提示约束（如 va____ f____ → value friendship），
        提示往往不在 innerText 里，而是伪元素内容（::before/::after）、
        placeholder 属性或属性里。挖出来喂给 AI，它就不用靠语义猜。
        """
        if driver is None or not blanks:
            return []
        script = r"""
            var out = [];
            for (var i = 0; i < arguments[0].length; i++) {
              var el = arguments[0][i];
              var info = {before: '', after: '', text: '', attrs: ''};
              try {
                var b = window.getComputedStyle(el, '::before').content || '';
                var a = window.getComputedStyle(el, '::after').content || '';
                info.before = (b === 'none' ? '' : b).replace(/["']/g, '');
                info.after = (a === 'none' ? '' : a).replace(/["']/g, '');
              } catch (e) {}
              try {
                var scoop = el.closest('.fe-scoop') || el.parentElement;
                info.text = scoop ? ((scoop.innerText || '') + ' ' + (scoop.textContent || ''))
                                      .replace(/\s+/g, ' ').trim().slice(0, 60) : '';
                info.html = scoop ? scoop.outerHTML.slice(0, 260) : '';
              } catch (e) {}
              var names = ['data-first-letter', 'data-hint', 'placeholder', 'title', 'aria-label',
                           'data-answer', 'data-word'];
              var attrs = [];
              for (var j = 0; j < names.length; j++) {
                var v = el.getAttribute(names[j]);
                if (v) { attrs.push(names[j] + '=' + v); }
              }
              info.attrs = attrs.join(' ');
              out.push(info);
            }
            return out;
        """
        try:
            return driver.execute_script(script, list(blanks)) or []
        except Exception:
            return []

    def can_parse(self, container, driver) -> bool:

        # 录音/跟读页不是"填文字"的题：它的答案是录音，交给 FollowReadHandler。
        # 否则会被当文本题交给 AI（实测 AI 把四句跟读原文改写一遍当答案、填 0/1、
        # 还连点了三次提交），既答不对又浪费一次模型调用。
        try:
            if container.find_elements(By.CSS_SELECTOR,
                                       '.ucomp-recorder, .button-record, [class*="record-icon"]'):
                return False
        except Exception:
            pass
        return bool(self._input_elements(container))

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        try:
            elements = self._input_elements(container)
            # 「可填」判定放宽：textarea / input（含没有 type 属性的）/ 可编辑元素。
            # Collocation 这类正文里的内联空格就是无 type 的 input，旧条件只认
            # input[type="text"]，于是 0 个输入框、题目被判非交互直接丢弃。
            fillable = []
            for el in elements:
                tag = (el.tag_name or '').lower()
                editable = (el.get_attribute('contenteditable') or '').lower() in ('true', '')
                if tag in ('textarea', 'input') or editable:
                    fillable.append(el)

            # 按空格自己的编号去重：DOM 常把滚动区里还没露出来的空一起渲染出来
            # （实测取到 17 个 input，页面上只有 8 个空），不按编号去重就会
            # 让 AI 多答、从第 1 个空就错位。
            labeled = self._label_blanks(container, fillable)
            hints = self._blank_hints(driver, [el for _label, el in labeled])

            try:
                body = (container.text or '').strip()
            except Exception:
                body = ""

            # 题目给的参考材料（对照表 / 词库）单列出来：这类题的标准答案往往就是
            # 表里的词条，光看正文猜不出写法。
            material = ""
            try:
                material = (container.find_element(
                    By.CSS_SELECTOR, '.layout-material-container').text or '').strip()
            except Exception:
                material = ""
            if not body and not fillable and not elements:
                return None

            text = f"【题目要求】{directions}\n\n" if directions else ""
            text += f"【页面内容】\n{body[:1500]}\n\n"
            if material:
                text += f"【参考材料（题目给的对照表/词库）】\n{material[:1500]}\n\n"
            if labeled:
                rows = []
                for idx, (label, _el) in enumerate(labeled):
                    hint = ""
                    if idx < len(hints) and isinstance(hints[idx], dict):
                        h = hints[idx]
                        hint = " ".join(x for x in (h.get('before'), h.get('attrs'),
                                                    h.get('text'), h.get('after')) if x).strip()
                    rows.append(f"{label}. ______" + (f"   （页面提示：{hint[:80]}）" if hint else ""))
                text += ("【填空列表（含页面上的首字母提示）】\n" + "\n".join(rows)
                         + f"\n\n请只回答上面这 {len(rows)} 个空，只写页面上还没印出来的部分（已印出的首字母不要重复），"
                           "按编号依次一行一个（不要写题号之外的说明）。\n")
            text += "（这个页面没有识别出具体题型，请按题目要求作答；能查到资料就联网核实。）"

            print(f"      兜底策略接管：{len(fillable)} 个输入框（去重后 {len(labeled)} 个空）"
                  f" / {len(elements)} 个可点选项，交 AI 作答")
            return Question(
                number=question_number,
                text=text,
                q_type=QuestionType.FILL_IN if labeled else QuestionType.TEXT,
                element=container,
                inputs=([el for _label, el in labeled] if labeled else (fillable or None)),
                banked_blanks=([{'element': el, 'word': '', 'number': label}
                                for label, el in labeled] if labeled else None),
                directions=directions,
            )
        except Exception as exc:
            print(f"      兜底策略解析失败: {str(exc)[:60]}")
            return None


class QuestionParser:
    """题目解析器 - 使用策略模式"""

    def __init__(self, driver):
        self.driver = driver
        self.strategies: List[QuestionParserStrategy] = [
            VideoStrategy(),
            DiscussionBoardStrategy(),
            SelfCheckStrategy(),
            SortingStrategy(),
            VocabularyFlashcardStrategy(),
            VocabularyTestStrategy(),
            DropdownSelectStrategy(),
            BankedClozeStrategy(),
            ListeningFillInStrategy(),  # 听力填空（转录由AISolver预处理完成）
            StandardChoiceStrategy(),
            MyVoiceTextStrategy(),
            TextInputStrategy(),
            FollowReadStrategy(),
            ScaleRateStrategy(),
            FillInStrategy(),
            # 兜底：前面的策略各管一类，它只管剩下的 —— 绝不跳过题目
            GenericFallbackStrategy(),
        ]

    def _find_reading_question_containers(self) -> List[Any]:
        """
        查找需要拆分的问答题容器（阅读问答题、翻译题）
        关键：多个reply，每个包含.question-inputbox，且direction表明是多题作答
        """
        try:
            direction_text = ""
            try:
                direction_elem = self.driver.find_element(
                    By.CSS_SELECTOR,
                    ".layout-direction-container .component-htmlview"
                )
                direction_text = direction_elem.text.lower()
            except Exception:
                pass

            is_multi_question_type = any(kw in direction_text for kw in [
                'answer', 'question', 'according to',  # 阅读问答题
                'translate',  # 翻译题
            ])

            if not is_multi_question_type:
                return []

            body_selectors = [
                '.layoutBody-container.has-material.has-reply',
                '.layoutBody-container.has-reply',  # 翻译题
            ]

            for selector in body_selectors:
                body_containers = self.driver.find_elements(By.CSS_SELECTOR, selector)

                for body in body_containers:
                    has_scoop = body.find_elements(By.CSS_SELECTOR, '.fe-scoop')
                    if has_scoop:
                        continue

                    has_options = body.find_elements(By.CSS_SELECTOR, '.option-wrapper, .banked-options')
                    if has_options:
                        continue

                    reply_containers = body.find_elements(By.CSS_SELECTOR, '.question-common-abs-reply')

                    valid_replies = []
                    for reply in reply_containers:
                        try:
                            if reply.is_displayed() and reply.find_elements(By.CSS_SELECTOR, '.question-inputbox'):
                                valid_replies.append(reply)
                        except Exception:
                            continue

                    if len(valid_replies) >= 2:
                        return valid_replies

            return []

        except Exception as e:
            logger.debug(f"查找问答题失败: {e}")
            return []

    def _extract_directions_from_page(self) -> str:
        """从页面统一提取 direction"""
        try:
            direction_elem = self.driver.find_element(
                By.CSS_SELECTOR,
                ".layout-direction-container .component-htmlview"
            )
            return direction_elem.text.strip()
        except Exception:
            pass

        try:
            direction_elem = self.driver.find_element(
                By.CSS_SELECTOR,
                ".abs-direction .content"
            )
            return direction_elem.text.strip()
        except Exception:
            pass

        try:
            direction_elem = self.driver.find_element(
                By.CSS_SELECTOR,
                ".direction-container"
            )
            return direction_elem.text.strip()
        except Exception:
            pass

        return ""

    def parse_all(self) -> Tuple[List[Question], str]:
        """解析所有可见题目"""
        directions = self._extract_directions_from_page()
        if self._is_discussion_board_page():
            # 讨论板以前整页跳过。它是需要作答的任务（写一段发言并发表），
            # 这里把题目读出来交给 DiscussionBoardHandler，别让它白跑一趟。
            question = DiscussionBoardStrategy().parse(
                self.driver.find_elements(By.CSS_SELECTOR, '.discussion-course-page-sdk') or [None],
                self.driver, 1, directions)
            if question and len(question.text) >= 4:
                print("     检测到讨论板页面，按「发表讨论」作答")
                return [question], directions
            print("     检测到讨论板页面，但没读到讨论题目，跳过")
            return [], directions

        containers = self._find_containers()
        questions: List[Question] = []

        print(f"     找到 {len(containers)} 个题目容器")

        # 整页证据只采集一次：以前每道题都重跑一遍全页 JS，并把同一份整页文本
        # 重复拼进每道题的题干（同页 N 题 → N 次采集、N 份重复文本，token 白涨）
        try:
            evidence = collect_page_evidence(self.driver)
        except Exception:
            evidence = ""

        for idx, container in enumerate(containers, 1):
            try:
                if not self._is_really_visible(container):
                    print(f"      容器 {idx} 不可见，跳过")
                    continue

                question = self._parse_single(container, idx, directions)
                if question:
                    if question.is_interactive():
                        # 无论哪条策略解析出来的题，都把整页信息挂上去，
                        # AI 提示词里就能看到表格、空格提示、词条等全部页面数据
                        if evidence:
                            question.text = (question.text or "") + "\n\n" + evidence
                            # 摘要进日志：能直接看到采集到了几张表、几个空、
                            # 有没有提示，方便判断 AI 拿到的是不是完整信息
                            tables = evidence.count("表格")
                            blanks = evidence.count(". ______")
                            hints = evidence.count("（提示：")
                            print(f"      [页面信息] 随题附带 {len(evidence)} 字："
                                  f"表格 {tables} 张 / 空 {blanks} 个（带提示 {hints} 个）"
                                  f" / {evidence[:110].replace(chr(10), ' | ')}")
                        questions.append(question)
                        print(f"      题目 {idx}: {question.q_type.name} - {question.text[:50]}...")
                    else:
                        print(f"      题目 {idx} 非交互类型: {question.q_type.name}")
                else:
                    print(f"      容器 {idx} 解析为None")

            except Exception as e:
                error_msg = str(e)
                print(f"       解析容器 {idx} 失败:{error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                continue

        return questions, directions

    def _is_discussion_board_page(self) -> bool:
        """检查当前页面是否是讨论板"""
        try:
            strong_indicators = [
                '.discussion-course-page-sdk',
                '.discussion-title',
                '.ds-discussion-bottom-textArea-container',
                '.discussion-cloud-recordList'
            ]

            score = 0
            for indicator in strong_indicators:
                if self.driver.find_elements(By.CSS_SELECTOR, indicator):
                    score += 1

            if score >= 2:
                print(f"     讨论板检测得分: {score}/{len(strong_indicators)}")
                return True
            return False
        except Exception as e:
            error_msg = str(e)
            print(f"操作失败: {error_msg[:50]}")
            logger.error(f"详细错误: {error_msg}", exc_info=True)
            return False

    def _find_containers(self) -> List[Any]:
        """查找题目容器"""
        if self._is_discussion_board_page():
            print("     检测到讨论板页面，跳过")
            return []

        reading_containers = self._find_reading_question_containers()
        if reading_containers:
            print(f"     找到 {len(reading_containers)} 道阅读问答题（共享材料）")
            return reading_containers

        sequence_containers = []
        for reply in self.driver.find_elements(By.CSS_SELECTOR, '.question-common-abs-reply'):
            try:
                if reply.is_displayed() and reply.find_elements(By.CSS_SELECTOR, '.sequence-view, .sortable-list-wrapper'):
                    sequence_containers.append(reply)
            except Exception:
                continue
        if sequence_containers:
            print(f"     找到 {len(sequence_containers)} 道排序题")
            return sequence_containers

        choice_containers = self.driver.find_elements(
            By.CSS_SELECTOR,
            '.question-common-abs-reply > .question-common-abs-choice'
        )

        if len(choice_containers) >= 2:
            reply_containers = []
            for choice in choice_containers:
                try:
                    reply = choice.find_element(By.XPATH,
                                                './parent::div[contains(@class, "question-common-abs-reply")]')
                    if reply not in reply_containers:
                        reply_containers.append(reply)
                except Exception:
                    pass

            if reply_containers:
                print(f"     找到 {len(reply_containers)} 道独立选择题")
                return reply_containers

        banked_containers = WebDriverHelper.safe_find_elements(
            self.driver,
            ['.layoutBody-container.has-material.has-reply']
        )
        valid_banked = []
        for container in banked_containers:
            has_options = container.find_elements(By.CSS_SELECTOR, '.option-wrapper .option')
            has_blanks = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .comp-abs-input input')
            if has_options and has_blanks:
                valid_banked.append(container)

        if valid_banked:
            print(f"     找到 {len(valid_banked)} 个选词填空容器")
            return valid_banked

        self_check_containers = []
        for container in self.driver.find_elements(By.CSS_SELECTOR, '.layoutBody-container'):
            try:
                has_table = container.find_elements(By.CSS_SELECTOR, '.ticket-view table, .ant-table-tbody')
                has_got_it = 'got it' in container.text.lower()
                if has_table and has_got_it and container.is_displayed():
                    self_check_containers.append(container)
            except Exception:
                continue

        if self_check_containers:
            print(f"     找到 {len(self_check_containers)} 个Self-check词汇勾选容器")
            return self_check_containers

        video_containers = WebDriverHelper.safe_find_elements(
            self.driver,
            ['.layoutBody-container:has(video)', '.question-video-point-read', '.video-box']
        )
        if video_containers:
            for container in video_containers:
                has_questions = container.find_elements(By.CSS_SELECTOR,
                                                        '.question-common-abs-choice, .question-inputbox, .option, .fe-scoop')
                if not has_questions:
                    print(f"     找到纯视频容器")
                    return [container]

        containers = self.driver.find_elements(By.CSS_SELECTOR, '.layout-container')
        valid_containers = []
        for c in containers:
            try:
                has_content = (
                        c.find_elements(By.CSS_SELECTOR, '.question-inputbox, .option, .fe-scoop, textarea') or
                        c.find_elements(By.CSS_SELECTOR, 'input[type="text"]')
                )
                if has_content and c.is_displayed():
                    valid_containers.append(c)
            except Exception:
                continue

        if valid_containers:
            print(f"     找到 {len(valid_containers)} 个有效题目容器（layout-container）")
            return valid_containers

        fallback = WebDriverHelper.safe_find_elements(
            self.driver,
            ['.layoutBody-container', '.layout-reply-container', '.reply-wrap']
        )
        if fallback:
            print(f"     备用方案找到 {len(fallback)} 个容器")
            return fallback

        # 最后兜底：认不出题型也要把「有作答界面的容器」交出去，由 GenericFallbackStrategy
        # 建题交给 AI（用户策略：绝不因为「没有匹配的策略」而跳过题目）。
        for selector in ('.layout-container', '.layoutBody-container',
                         '.question-common-abs-question-container', '.question-common-abs-reply'):
            try:
                found = [c for c in self.driver.find_elements(By.CSS_SELECTOR, selector)
                         if c.is_displayed()]
            except Exception:
                found = []
            if found:
                print(f"     兜底容器：{selector} × {len(found)}")
                return found

        return []

    def _is_really_visible(self, element) -> bool:
        """检查元素真正可见"""
        try:
            if not element.is_displayed():
                return False

            parent = element
            for _ in range(3):
                try:
                    parent = parent.find_element(By.XPATH, '..')
                    parent_display = self.driver.execute_script(
                        "return window.getComputedStyle(arguments[0]).display",
                        parent
                    )
                    if parent_display == 'none':
                        return False
                except Exception:
                    break

            return True
        except Exception:
            return False

    def _parse_single(self, container, number: int, directions: str = "") -> Optional[Question]:
        """解析单个容器；页面重绘导致句柄失效时，重新定位容器再试一次。

        实测：U校园 是单页应用，切页/资源加载完会重绘 DOM，容器句柄突然失效
        （stale element reference），旧代码直接放弃 → 整页 0 道题。这里补一次重试。
        """
        try:
            return self._parse_single_once(container, number, directions)
        except StaleElementReferenceException:
            print(f"      容器 {number} 已被页面刷新，重新定位后重试…")
            time.sleep(0.8)
            try:
                fresh = self._find_containers()
            except Exception:
                fresh = []
            if len(fresh) >= number:
                try:
                    return self._parse_single_once(fresh[number - 1], number, directions)
                except Exception as exc:
                    print(f"      重试仍失败: {str(exc)[:60]}")
            return None

    def _parse_single_once(self, container, number: int, directions: str = "") -> Optional[Question]:
        """使用策略解析单个容器"""
        for strategy in self.strategies:
            try:
                if strategy.can_parse(container, self.driver):
                    print(f"      使用策略: {strategy.__class__.__name__}")
                    question = strategy.parse(container, self.driver, number, directions)
                    if question:
                        if question.number is None:
                            question.number = number
                        print(f"      解析成功: {question.q_type.name}")
                        return question
                    else:
                        print(f"      策略返回None")
            except StaleElementReferenceException:
                # 容器被页面重绘：交给外层重新定位后重试，别在这里吞掉
                raise
            except Exception as e:
                error_msg = str(e)
                print(f"      策略 {strategy.__class__.__name__} 失败: {error_msg[:50]} ")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                continue
        print(f"      没有匹配的策略")
        return None


class DiscussionBoardStrategy(QuestionParserStrategy):
    """讨论板策略"""

    def can_parse(self, container, driver) -> bool:
        discussion_features = [
            '.discussion-course-page-sdk',
            '.ds-discussion-reply',
            '.discussion-cloud-recordList-title',
        ]

        has_discussion_feature = any(
            container.find_elements(By.CSS_SELECTOR, feature)
            for feature in discussion_features
        )

        if not has_discussion_feature:
            return False

        banked_features = [
            '.question-material-banked-cloze-reply',
            '.banked-options',
            '.fe-scoop[data-scoop-index]',
        ]

        has_banked_feature = any(
            container.find_elements(By.CSS_SELECTOR, feature)
            for feature in banked_features
        )
        if has_banked_feature:
            return False

        return True

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        return Question(
            number=question_number,
            text=self.read_topic(driver, directions),
            q_type=QuestionType.DISCUSSION_BOARD,
            element=container
        )

    #: 讨论题的题干通常在这些位置
    TOPIC_SELECTORS = [
        '.discussion-title',
        '.discussion-question-title',
        '.ds-discussion-title',
        '.abs-direction',
        '.layout-direction-container',
        '.direction-container',
        '.question-common-abs-title',
    ]

    @classmethod
    def read_topic(cls, driver, directions: str = "", limit: int = 1500) -> str:
        """读出讨论板要讨论的题目，供大模型据此写发言。"""
        pieces: List[str] = []
        for selector in cls.TOPIC_SELECTORS:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements[:3]:
                try:
                    if not element.is_displayed():
                        continue
                    text = re.sub(r"\s+", " ", element.text or "").strip()
                except Exception:
                    continue
                if text and text not in pieces:
                    pieces.append(text)
        if not pieces and directions:
            pieces.append(re.sub(r"\s+", " ", directions).strip())
        if not pieces:
            # 兜底：整页正文里找那段最长的英文说明
            try:
                body = driver.execute_script(
                    "return (document.body && document.body.innerText) || '';") or ""
            except Exception:
                body = ""
            for line in str(body).splitlines():
                line = re.sub(r"\s+", " ", line).strip()
                if len(line) >= 20 and line not in pieces:
                    pieces.append(line)
                if sum(len(p) for p in pieces) > limit:
                    break
        return " ".join(pieces)[:limit].strip()


class SelfCheckStrategy(QuestionParserStrategy):
    """Self-check 词汇勾选表解析策略"""

    def can_parse(self, container, driver) -> bool:
        direction_text = ""
        try:
            direction_elem = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
            direction_text = direction_elem.text.lower()
        except Exception:
            pass

        has_table = bool(container.find_elements(By.CSS_SELECTOR, '.ticket-view table, .ant-table-tbody'))
        has_got_it = 'got it' in container.text.lower()
        # 题目要求里「勾选」的写法不止一种：Check in the box / Use the self-assessment
        # checklist to check what you have learned in this unit（综合教程就是这么写的）。
        # 只认 you've learned 时，页面被找到了却没人认领，日志里就是
        # 「找到 1 个Self-check词汇勾选表 → 没有匹配的策略」，整页跳过。
        has_check_instruction = any(
            kw in direction_text for kw in (
                'check in the box', "you've learned", 'you have learned', 'learned in this unit',
                'self-assessment', 'checklist', '勾选', 'check'))
        if not has_check_instruction:
            # Review & check 这类自评页的题目要求区常常读不到（整页只有表格），
            # 这时按结构认定：表格第二列就是勾选框，未勾选是 anticon-border。
            has_check_instruction = bool(container.find_elements(
                By.CSS_SELECTOR, 'tbody tr .anticon-border, tbody tr .anticon-check'))
        return has_table and has_got_it and has_check_instruction

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        rows = []
        for row in container.find_elements(By.CSS_SELECTOR, 'tbody tr.ant-table-row:not(.category-name)'):
            try:
                word_elem = row.find_element(By.CSS_SELECTOR, '.content-text')
                word = word_elem.text.strip()
                got_it_icon = row.find_element(By.CSS_SELECTOR, 'td:nth-child(2) .anticon')
                if word and got_it_icon:
                    rows.append({'word': word, 'element': got_it_icon})
            except Exception:
                continue

        return Question(
            number=question_number,
            text=f"Self-check 词汇勾选（共{len(rows)}项）",
            q_type=QuestionType.SELF_CHECK,
            element=container,
            banked_blanks=rows,
            directions=directions,
        )


class SortingStrategy(QuestionParserStrategy):
    """拖拽排序题解析策略"""

    def can_parse(self, container, driver) -> bool:
        return bool(container.find_elements(By.CSS_SELECTOR, '.sequence-view, .sortable-list-wrapper'))

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        option_elements = container.find_elements(By.CSS_SELECTOR, '.sequence-reply-view-item-text')
        options = []

        for idx, elem in enumerate(option_elements):
            full_text = elem.text.strip()
            if not full_text:
                continue

            letter = ""
            text = full_text
            try:
                spans = elem.find_elements(By.TAG_NAME, 'span')
                if spans:
                    letter = spans[0].text.strip().replace('.', '').replace(')', '').upper()
                    if len(spans) > 1:
                        text = spans[1].text.strip()
            except Exception:
                pass

            if not letter:
                match = re.match(r'^([A-Z])[\s.、)]*(.+)$', full_text, re.DOTALL)
                if match:
                    letter = match.group(1).upper()
                    text = match.group(2).strip()
                else:
                    letter = chr(65 + idx)

            # 图片配对题（Match the names/descriptions with the pictures）：
            # 选项里只有图片，文字模型看不见图。把图片自带的线索（alt / title /
            # 文件名）一并读出来，模型才有可能认出「哪张图是谁」。
            hint = self._image_hint(elem)
            if hint:
                text = f"{text} [图片线索: {hint}]".strip()

            options.append(Option(letter=letter, text=text, element=elem))

        if len(options) < 2:
            return None

        question_text = "排序题：请根据材料出现顺序重新排列选项"
        if directions:
            question_text = f"{directions}\n{question_text}"

        return Question(
            number=question_number,
            text=question_text,
            q_type=QuestionType.SORTING,
            element=container,
            options=options,
            directions=directions,
        )

    @staticmethod
    def _image_hint(element) -> str:
        """取图片自带的可读线索：alt、title、文件名。"""
        hints = []
        try:
            images = element.find_elements(By.TAG_NAME, 'img')
        except Exception:
            images = []
        for image in images[:2]:
            for attribute in ('alt', 'title', 'src'):
                try:
                    value = (image.get_attribute(attribute) or "").strip()
                except Exception:
                    continue
                if not value:
                    continue
                if attribute == 'src':
                    value = value.split('/')[-1].split('?')[0]
                if value and value not in hints:
                    hints.append(value)
        return " / ".join(hints)[:120]


class ScaleRateStrategy(QuestionParserStrategy):
    """评分/量表题：每条陈述一行，点上面的 1-5 数字作答（点完变蓝即算选了）。"""

    #: 一行里可点数字的容器（各版本类名不同，尽量多列）
    ROW_SELECTORS = (
        '[class*="scale"]', '[class*="rate"]', '[class*="rating"]',
        '[class*="score"]', '[class*="evaluate"]', '[class*="option-row"]',
        '[class*="row"]',
    )

    #: 实测的真实类名（U校园 读写教程3 · Pre-reading activities 评分题）
    WRAPPER = '.evaluation-slider-reply-component-wrapper'
    ROW = '.question-common-abs-reply'
    ITEM = '.evaluation-slider-reply-component-wrapper_option_item'

    def can_parse(self, container, driver) -> bool:
        try:
            wrappers = container.find_elements(By.CSS_SELECTOR, self.WRAPPER)
        except Exception:
            return False
        if wrappers:
            print(f"       （评分题：{len(wrappers)} 条陈述）")
            return True
        return False

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        return Question(
            number=question_number,
            text=directions or "评分题：为每条陈述选择 1-5 分",
            q_type=QuestionType.SCALE_RATE,
            element=container,
            directions=directions,
        )


class FollowReadStrategy(QuestionParserStrategy):
    """跟读题：每条句子旁边有「喇叭（听示范）」和「麦克风（录音）」两个按钮。"""

    MIC_HINTS = ('record', 'mic', 'voice', 'speak', 'follow', 'recite')

    def can_parse(self, container, driver) -> bool:
        try:
            elements = container.find_elements(
                By.CSS_SELECTOR, '[class*="record"], [class*="mic"], [class*="voice"], '
                                 '[class*="speak"], [class*="follow"]')
        except Exception:
            return False
        if elements:
            print(f"       （跟读题：找到 {len(elements)} 个疑似麦克风/录音元素）")
            return True
        return False

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        return Question(
            number=question_number,
            text=directions or "跟读题：点麦克风朗读",
            q_type=QuestionType.FOLLOW_READ,
            element=container,
            directions=directions,
        )


class VocabularyTestStrategy(QuestionParserStrategy):
    """词汇测试题解析策略"""

    def can_parse(self, container, driver) -> bool:
        options = self._extract_options(container, driver)
        if len(options) < 2:
            return False

        title_elem = WebDriverHelper.safe_find_element(driver, Selectors.QUESTION_TITLE, container)
        if not title_elem:
            return False

        text = title_elem.text.strip()
        text = re.sub(r"^\d+[.、)\]]\s*", "", text)

        is_eng_word = bool(re.match(r"^[a-zA-Z\-]+$", text)) and 1 < len(text) <= 20
        has_chinese = bool(re.search(r"[\u4e00-\u9fff]", text))

        option_texts = [opt.text for opt in options]
        has_eng_opts = any(re.search(r"[a-zA-Z]{3,}", t) for t in option_texts)
        has_chi_opts = any(re.search(r"[\u4e00-\u9fff]", t) for t in option_texts)

        return (is_eng_word and has_chi_opts) or (has_chinese and has_eng_opts)

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        title_elem = WebDriverHelper.safe_find_element(driver, Selectors.QUESTION_TITLE, container)
        text = title_elem.text.strip() if title_elem else ""

        options = self._extract_options(container, driver)

        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.VOCABULARY_TEST,
            element=container,
            options=options,
            directions=directions
        )

    def _extract_options(self, container, driver) -> List[Option]:
        """提取选项"""
        options = []
        option_elements = WebDriverHelper.safe_find_elements(driver, Selectors.CHOICE_OPTIONS, container)

        for opt_elem in option_elements:
            letter = ""
            text = ""

            caption_elem = WebDriverHelper.safe_find_element(driver, Selectors.OPTION_CAPTION, opt_elem)
            if caption_elem:
                letter = caption_elem.text.strip().replace('.', '').replace(')', '').replace('、', '')

            content_elem = WebDriverHelper.safe_find_element(driver, Selectors.OPTION_CONTENT, opt_elem)
            if content_elem:
                text = content_elem.text.strip()
            else:
                full_text = opt_elem.text.strip()
                text = re.sub(rf"^{re.escape(letter)}[.)、\\s]*", "", full_text)

            is_selected = 'selected' in (opt_elem.get_attribute('class') or '').lower()

            if letter or text:
                options.append(Option(letter=letter, text=text, element=opt_elem, is_selected=is_selected))

        return options


class BankedClozeStrategy(QuestionParserStrategy):
    """选词填空解析策略"""

    def can_parse(self, container, driver) -> bool:
        has_options = bool(
            container.find_elements(By.CSS_SELECTOR, '.option-wrapper .option, .option-wrapper .option-placeholder') or
            container.find_elements(By.CSS_SELECTOR, '.banked-options .option')
        )
        has_blanks = bool(container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .comp-abs-input input'))
        return has_options and has_blanks

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        banked_options = []

        placeholder_elements = container.find_elements(By.CSS_SELECTOR, '.option-wrapper .option-placeholder')
        for elem in placeholder_elements:
            text = elem.text.strip()
            if text and text not in banked_options:
                banked_options.append(text)

        if not banked_options:
            option_elements = container.find_elements(By.CSS_SELECTOR, '.option-wrapper .option')
            for elem in option_elements:
                text = elem.text.strip()
                if text and text not in banked_options:
                    banked_options.append(text)

        if not banked_options:
            option_elements = container.find_elements(By.CSS_SELECTOR,
                                                      '.banked-options .option, [data-rbd-draggable-id^="options-"]')
            for elem in option_elements:
                text = elem.text.strip()
                if text and text not in banked_options:
                    banked_options.append(text)

        print(f"      选项池（短语）: {banked_options}")

        inputs = []
        banked_blanks = []

        scoops = container.find_elements(By.CSS_SELECTOR, '.fe-scoop')

        for i, scoop in enumerate(scoops):
            context = ""
            try:
                context_elem = scoop.find_element(By.XPATH, './ancestor::p')
                context = context_elem.text.strip()
            except Exception:
                try:
                    context = scoop.text.strip()
                except Exception:
                    context = ""

            input_box = None
            try:
                input_box = scoop.find_element(By.CSS_SELECTOR, 'input')
                inputs.append(input_box)
            except Exception:
                pass

            banked_blanks.append({
                'index': i,
                'context': context,
                'input': input_box,
                'element': scoop
            })

        print(f"      找到 {len(banked_blanks)} 个填空位置")

        question_text = f"选词填空（{len(banked_blanks)}个空）"
        if banked_options:
            question_text += f"\n可选选项: {', '.join(banked_options[:5])}"
            if len(banked_options) > 5:
                question_text += f" 等共{len(banked_options)}个"

        return Question(
            number=question_number,
            text=question_text,
            q_type=QuestionType.BANKED_CLOZE,
            element=container,
            inputs=inputs,
            banked_options=banked_options,
            banked_blanks=banked_blanks,
            directions=directions,
        )


class StandardChoiceStrategy(QuestionParserStrategy):
    def _is_listening_choice_page(self, driver, directions: str = "") -> bool:
        direction_text = (directions or "").lower()
        if not direction_text:
            try:
                direction_elem = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
                direction_text = direction_elem.text.lower()
            except Exception:
                direction_text = ""

        has_listening_hint = any(kw in direction_text for kw in ['listen', 'audio', 'hear', 'conversation', 'passage', 'news'])
        has_choice_hint = any(kw in direction_text for kw in ['choose', 'answer', 'best answer'])
        return has_listening_hint and has_choice_hint

    def _is_video_choice_page(self, driver, directions: str = "") -> bool:
        direction_text = (directions or "").lower()
        if not direction_text:
            try:
                direction_elem = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
                direction_text = direction_elem.text.lower()
            except Exception:
                direction_text = ""

        has_video_hint = any(kw in direction_text for kw in ['watch', 'video', 'clip'])
        has_choice_hint = any(kw in direction_text for kw in ['choose', 'answer', 'decide', 'true or false', 'statements'])
        return has_video_hint and has_choice_hint

    @staticmethod
    def _is_displayed(el) -> bool:
        try:
            return bool(el.is_displayed())
        except Exception:
            return False

    def can_parse(self, container, driver) -> bool:
        if container.tag_name == 'div' and 'question-common-abs-choice' in (container.get_attribute('class') or ''):
            options = container.find_elements(By.CSS_SELECTOR, '.option-wrap .option, .option.isNotReview')
            return len(options) >= 2
        choices = container.find_elements(By.CSS_SELECTOR, '.question-common-abs-choice')
        # SPA 残留 DOM：上一题的 choice 容器可能还藏在页面里（隐藏态）。
        # 以前只要总数 >1 就放弃 → 多选陈述页被 TextInput 抢走、解析成 TEXT。
        # 只认可见的容器：恰好一个可见才认领。
        visible = [c for c in choices if self._is_displayed(c)]
        if len(visible) > 1:
            return False
        if visible:
            options = visible[0].find_elements(By.CSS_SELECTOR, '.option-wrap .option, .option.isNotReview')
            return len(options) >= 2
        options = container.find_elements(By.CSS_SELECTOR, '.option-wrap .option, .option.isNotReview')
        return len(options) >= 2

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        if 'question-common-abs-choice' in (container.get_attribute('class') or ''):
            choice_container = container
        else:
            choices = [c for c in container.find_elements(By.CSS_SELECTOR, '.question-common-abs-choice')
                       if self._is_displayed(c)]
            if not choices:
                choices = container.find_elements(By.CSS_SELECTOR, '.question-common-abs-choice')
            choice_container = choices[0] if choices else container

        title_elem = choice_container.find_element(By.CSS_SELECTOR, '.ques-title')
        text = title_elem.text.strip() if title_elem else ""

        vocab_strategy = VocabularyTestStrategy()
        options = vocab_strategy._extract_options(container, driver)

        checkboxes = container.find_elements(By.CSS_SELECTOR, 'input[type="checkbox"]')
        is_listening_choice = self._is_listening_choice_page(driver, directions)
        is_video_choice = self._is_video_choice_page(driver, directions)
        # multipleChoice 类长在内层 choice 容器上 —— 以前看的是外层 layout-container，
        # 永远看不到，多选页全被当成单选
        choice_class = (choice_container.get_attribute('class') or '').lower()
        explicit_multi = bool(checkboxes) or 'multiplechoice' in choice_class or '多选' in text
        is_multi = (
                explicit_multi or
                (len(options) > 4 and not is_listening_choice and not is_video_choice)
        )
        if is_video_choice and not explicit_multi:
            q_type = QuestionType.VIDEO_CHOICE
        elif is_listening_choice and not explicit_multi:
            q_type = QuestionType.LISTENING_CHOICE
        else:
            q_type = QuestionType.MULTIPLE_CHOICE if is_multi else QuestionType.SINGLE_CHOICE

        return Question(
            number=question_number,
            text=text,
            q_type=q_type,
            element=container,
            options=options,
            directions=directions
        )


class MyVoiceTextStrategy(QuestionParserStrategy):
    """My voice 上传页的文字作答策略"""

    TEXTAREA_SELECTORS = [
        '.question-multi-file-upload textarea.ant-input',
        '.question-multi-file-upload textarea',
        'textarea[placeholder*="输入文字作答"]',
    ]

    def can_parse(self, container, driver) -> bool:
        textarea = self._find_textarea(container)
        if not textarea:
            return False

        has_upload = bool(container.find_elements(By.CSS_SELECTOR, '.question-multi-file-upload, .unipus-upload'))
        if not has_upload:
            return False

        page_text = self._collect_page_text(container, driver).lower()
        return any(kw in page_text for kw in ['record', 'upload', 'introduction', 'my voice', '输入文字作答'])

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        textarea = self._find_textarea(container)
        if not textarea:
            return None

        material_text = self._extract_material_text(container, driver)
        full_text = ""
        if directions:
            full_text += f"【题目要求】{directions}\n\n"
        if material_text:
            full_text += f"【任务材料】\n{material_text}\n\n"
        full_text += "请根据以上要求，直接写一段可粘贴到输入框的英文介绍，控制在500字符以内。"

        return Question(
            number=question_number,
            text=full_text,
            q_type=QuestionType.MY_VOICE_TEXT,
            element=container,
            inputs=[textarea],
            directions=directions,
        )

    def _find_textarea(self, container):
        for selector in self.TEXTAREA_SELECTORS:
            try:
                for elem in container.find_elements(By.CSS_SELECTOR, selector):
                    if elem.is_displayed():
                        return elem
            except Exception:
                continue
        return None

    def _collect_page_text(self, container, driver) -> str:
        parts = []
        for source in [container, driver]:
            try:
                text = source.text.strip()
                if text:
                    parts.append(text)
            except Exception:
                pass
        return "\n".join(parts)

    def _extract_material_text(self, container, driver) -> str:
        selectors = [
            '.layout-material-container',
            '.question-common-abs-material',
            '.text-material-wrapper',
        ]
        for selector in selectors:
            try:
                elem = container.find_element(By.CSS_SELECTOR, selector)
                text = elem.text.strip()
                if text:
                    return text
            except Exception:
                continue

        for selector in selectors:
            try:
                elem = driver.find_element(By.CSS_SELECTOR, selector)
                text = elem.text.strip()
                if text:
                    return text
            except Exception:
                continue
        return ""


class TextInputStrategy(QuestionParserStrategy):
    """文本输入题策略"""

    WRITING_KEYWORDS = ['topic', 'topic sentence', 'outline', 'things to do',
                        'concluding sentence', 'more topics']
    READING_MIN_LENGTH = 400

    def can_parse(self, container, driver) -> bool:

        # 录音/跟读页不是"填文字"的题：它的答案是录音，交给 FollowReadHandler。
        # 否则会被当文本题交给 AI（实测 AI 把四句跟读原文改写一遍当答案、填 0/1、
        # 还连点了三次提交），既答不对又浪费一次模型调用。
        try:
            if container.find_elements(By.CSS_SELECTOR,
                                       '.ucomp-recorder, .button-record, [class*="record-icon"]'):
                return False
        except Exception:
            pass
        textareas = container.find_elements(By.CSS_SELECTOR,
                                            'textarea.question-textarea-content, textarea.question-inputbox-input, textarea.scoopFill_textarea')
        if not textareas:
            return False

        container_class = container.get_attribute('class') or ''
        is_single_reply = 'question-common-abs-reply' in container_class

        if is_single_reply:
            has_inputbox = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
            if has_inputbox:
                return True

            has_scoop_container = container.find_elements(By.CSS_SELECTOR, '.question-common-abs-scoop, .fe-scoop')
            if has_scoop_container:
                return True

            return False

        has_material = container.find_elements(By.CSS_SELECTOR, '.layout-material-container')

        if has_material:
            material_text = ""
            try:
                material = container.find_element(By.CSS_SELECTOR, '.layout-material-container')
                material_text = material.text.lower()
            except Exception:
                pass

            if any(kw in material_text for kw in self.WRITING_KEYWORDS):
                has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
                if has_scoop:
                    return True

            if any(kw in material_text for kw in ['model', 'example', '示例', '例句']):
                return True

        direction_text = ""
        try:
            direction_elem = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container .component-htmlview')
            direction_text = direction_elem.text.lower()
        except Exception:
            direction_elem = WebDriverHelper.safe_find_element(
                driver, ['.layout-direction-container .content', '.abs-direction .content'], container)
            if direction_elem:
                direction_text = direction_elem.text.lower()

        if direction_text:
            if any(kw in direction_text for kw in ['write', 'essay', 'composition', 'paragraph']):
                has_options = container.find_elements(By.CSS_SELECTOR, '.option-wrapper, .banked-options, .option-wrap')
                has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
                if not has_options and has_scoop:                    return True

            if any(kw in direction_text for kw in ['answer', 'question', 'according to']):
                has_inputbox = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
                has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
                if has_inputbox and not has_scoop:
                    return True

        if len(textareas) >= 2:
            has_inputbox = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
            has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
            if has_inputbox and not has_scoop:
                return True

        if len(textareas) == 1:
            rows = textareas[0].get_attribute('rows')
            is_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
            if rows and int(rows) >= 5 and is_scoop:
                return True

        # 段落翻译（Translation · Paragraph translation）：题目要求写着「Translate … into
        # Chinese / 译成中文」，页面上只有一行 textarea 加一段正文，既没有 scoop 也没有选项。
        # 旧条件只认 scoop 或写作关键词，这类页面整页被跳过（日志里的「没有匹配的策略」）。
        if len(textareas) == 1 and direction_text and any(
                kw in direction_text for kw in ('translate', 'translation', '翻译', '译成')):
            has_inputbox = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
            has_options = container.find_elements(
                By.CSS_SELECTOR, '.option-wrapper, .banked-options, .option-wrap')
            has_scoop = container.find_elements(
                By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
            if has_inputbox and not has_options and not has_scoop:
                return True

        # 通用自由作答页：一行 textarea + 题目要求/材料，没有选项、没有 scoop、没有表格。
        # 「Read Para. 12 of Text B and mark off the thought groups」这类阅读技能题就是
        # 这个样子：以前没有任何策略认领，日志里是「找到 1 个题目容器 → 没有匹配的策略
        # → 找到 0 个可见题目」，整页 0 题、连 AI 都没轮到。
        if len(textareas) == 1:
            has_inputbox = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
            has_options = container.find_elements(
                By.CSS_SELECTOR, '.option-wrapper, .banked-options, .option-wrap')
            has_scoop = container.find_elements(
                By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
            has_table = container.find_elements(
                By.CSS_SELECTOR, '.ticket-view table, .ant-table-tbody')
            if has_inputbox and not (has_options or has_scoop or has_table):
                return True

        return False

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        try:
            effective_directions = directions

            if not effective_directions:
                try:
                    direction_elem = driver.find_element(By.CSS_SELECTOR,
                                                         ".layout-direction-container .component-htmlview")
                    effective_directions = direction_elem.text.strip()
                except Exception:
                    pass

            container_class = container.get_attribute('class') or ''
            is_single_reply = 'question-common-abs-reply' in container_class

            has_inputbox = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
            has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')

            if is_single_reply:
                if has_inputbox and not has_scoop:
                    is_writing = False
                elif has_scoop and not has_inputbox:
                    is_writing = True
                else:
                    is_writing = self._check_is_writing_by_material(container, driver)
            else:
                is_writing = self._check_is_writing_by_material(container, driver)

            material_text = self._extract_material_text(container, driver)

            items = []

            if is_writing:
                scoop_containers = container.find_elements(By.CSS_SELECTOR, '.fe-scoop')
                for i, scoop in enumerate(scoop_containers, 1):
                    try:
                        number_elem = scoop.find_element(By.CSS_SELECTOR, '.question-number')
                        number = number_elem.text.strip()

                        textarea = scoop.find_element(By.CSS_SELECTOR, 'textarea.question-textarea-content')
                        placeholder = textarea.get_attribute('placeholder') or "写作"

                        items.append({
                            'index': i,
                            'words': f"题{number}: {placeholder}",
                            'input': textarea,
                            'question_text': placeholder
                        })
                    except Exception as e:
                        continue
            else:
                if is_single_reply:
                    input_boxes = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
                else:
                    input_boxes = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')

                for i, box in enumerate(input_boxes, question_number):
                    # 题干与输入框都放宽取：有的页面没有 .question-inputbox-header，有的
                    # textarea 不带 question-inputbox-input。以前任一处找不到就整箱跳过，
                    # items 变空 → parse 返回 None → 整页 0 题（日志里的「策略返回 None」，
                    # 阅读技能题「write down the signal words」两个作答框就是这么丢的）。
                    textarea = None
                    for sel in ('textarea.question-inputbox-input', 'textarea',
                                '.question-inputbox-input'):
                        try:
                            textarea = box.find_element(By.CSS_SELECTOR, sel)
                            break
                        except Exception:
                            textarea = None
                    if textarea is None:
                        continue

                    question_text = ""
                    for sel in ('.question-inputbox-header', '.component-htmlview',
                                '.question-inputbox-header-text'):
                        try:
                            question_text = box.find_element(By.CSS_SELECTOR, sel).text.strip()
                            if question_text:
                                break
                        except Exception:
                            continue
                    if not question_text:
                        question_text = f"第 {i} 题（自由作答）"
                    question_text = re.sub(rf'^\d+[\s.、)]+', '', question_text)

                    items.append({
                        'index': i,
                        'words': question_text,
                        'input': textarea,
                        'question_text': question_text
                    })

            if not items:
                return None

            full_text = ""
            if effective_directions:
                full_text += f"【题目要求】{effective_directions}\n\n"

            if material_text:
                if is_writing:
                    full_text += f"【写作提纲/材料】\n{material_text[:500]}\n\n"
                else:
                    full_text += f"【阅读材料】\n{material_text[:800]}...\n\n（文章较长，根据问题回答即可）\n\n"

            full_text += "【问题列表】\n"
            for item in items:
                if is_writing:
                    full_text += f"{item['index']}. {item['words']}\n"
                else:
                    full_text += f"{item['index']}. {item['question_text']}\n"

            if is_single_reply and not is_writing and len(items) == 1:
                item = items[0]
                return Question(
                    number=item['index'],
                    text=full_text,
                    q_type=QuestionType.TEXT,
                    element=container,
                    inputs=[item['input']],
                    banked_blanks=[item],
                    directions=effective_directions,
                )

            return Question(
                number=question_number,
                text=full_text,
                q_type=QuestionType.TEXT,
                element=container,
                inputs=[item['input'] for item in items],
                banked_blanks=items,
                directions=effective_directions,
            )

        except Exception as e:
            error_msg = str(e)
            print(f"      TextInputStrategy解析失败: {error_msg[:100]}")
            logger.error(f"详细错误: {error_msg}", exc_info=True)
            return None

    def _check_is_writing_by_material(self, container=None, driver=None) -> bool:
        material_text = self._extract_material_text(container, driver)
        if material_text:
            material_lower = material_text.lower()
            return any(kw in material_lower for kw in self.WRITING_KEYWORDS)
        return False

    def _extract_material_text(self, container=None, driver=None) -> str:
        material_text = ""
        try:
            if container:
                try:
                    material = container.find_element(By.CSS_SELECTOR, '.layout-material-container')
                    material_text = material.text.strip()
                except Exception:
                    pass
            if not material_text and driver:
                try:
                    material = driver.find_element(By.CSS_SELECTOR, '.layout-material-container')
                    material_text = material.text.strip()
                except Exception:
                    pass
        except Exception:
            pass
        return material_text


class ListeningFillInStrategy(QuestionParserStrategy):
    """音视频填空题解析策略（仅解析题目，转录由预处理完成）"""

    def can_parse(self, container, driver) -> bool:
        has_blanks = bool(container.find_elements(By.CSS_SELECTOR, '.fe-scoop input'))
        has_option_pool = bool(container.find_elements(By.CSS_SELECTOR, '.option-wrapper, .banked-options'))
        if not has_blanks or has_option_pool:
            return False

        try:
            direction = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
            text = direction.text.lower()
            has_media_hint = any(kw in text for kw in [
                'listen', 'audio', 'hear', 'talk', 'conversation',
                'watch', 'video', 'clip', 'view'
            ])
            has_fill_hint = any(kw in text for kw in [
                'fill in', 'complete', 'blank', 'blanks'
            ])
            return has_media_hint and has_fill_hint
        except Exception:
            return False

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        inputs = container.find_elements(By.CSS_SELECTOR, '.fe-scoop input')
        if not inputs:
            return None

        media_label = "视频填空题" if self._is_video_fill_page(directions) else "听力填空题"
        full_text = f"{media_label}（共{len(inputs)}个空）"
        if directions:
            full_text = f"【题目要求】{directions}\n\n{full_text}"

        blank_contexts = []
        for i, inp in enumerate(inputs):
            try:
                scoop = inp.find_element(By.XPATH, './ancestor::span[@class="fe-scoop"]')
                sentence = self._extract_blank_sentence(scoop)
            except Exception:
                sentence = ""
            left_context, right_context = self._split_blank_context(sentence, i + 1)
            blank_contexts.append({
                'index': i,
                'sentence': sentence,
                'left_context': left_context,
                'right_context': right_context,
                'input': inp
            })

        return Question(
            number=question_number,
            text=full_text,
            q_type=QuestionType.LISTENING_FILL_IN,
            element=container,
            inputs=inputs,
            banked_blanks=blank_contexts,
            directions=directions,
        )

    @staticmethod
    def _is_video_fill_page(directions: str) -> bool:
        direction_text = (directions or "").lower()
        return any(kw in direction_text for kw in ['watch', 'video', 'clip', 'view'])

    def _extract_blank_sentence(self, scoop) -> str:
        for xpath in ['./ancestor::td[1]', './ancestor::p[1]', './ancestor::div[1]']:
            try:
                text = scoop.find_element(By.XPATH, xpath).text.strip()
                if text:
                    return text
            except Exception:
                continue
        return ""

    @staticmethod
    def _split_blank_context(sentence: str, blank_number: int) -> Tuple[str, str]:
        if not sentence:
            return "", ""
        patterns = [
            rf'{blank_number}\s*[）).、:：]',
            rf'{blank_number}\s*[_—-]+',
            rf'{blank_number}\s*[）).、:：]?\s*空\s*{blank_number}?\s*[:：]?',
            rf'空\s*{blank_number}\s*[:：]?',
        ]
        for pattern in patterns:
            match = re.search(pattern, sentence)
            if match:
                return sentence[:match.start()].strip(), sentence[match.end():].strip()
        match = re.search(rf'(?<!\d){blank_number}(?!\d)', sentence)
        if match:
            return sentence[:match.start()].strip(), sentence[match.end():].strip()
        return sentence, ""

class FillInStrategy(QuestionParserStrategy):
    """填空题解析策略"""

    FILL_INPUTS = [
        'input.fill-blank--bc-input-DelG1',
        '.fe-scoop input:not([type="hidden"])',
        '.comp-abs-input input',
        '.blankinput',
        'input[type="text"]',
    ]

    def can_parse(self, container, driver) -> bool:
        has_material_container = container.find_elements(
            By.CSS_SELECTOR, '.layout-material-container'
        )
        if has_material_container:
            return False

        has_textarea = container.find_elements(
            By.CSS_SELECTOR, 'textarea.question-textarea-content'
        )
        if has_textarea:
            return False

        inputs = WebDriverHelper.safe_find_elements(driver, self.FILL_INPUTS, container)

        if len(inputs) >= 2:
            return True

        if len(inputs) == 1:
            inp = inputs[0]
            placeholder = inp.get_attribute('placeholder') or ''
            if 'word' in placeholder.lower() or '不少于' in placeholder:
                return False
            return True

        return False

    #: 把容器里的原文按文档顺序拼回来，每个输入框换成 [1]…[N]，供大模型定位空格。
    #: 填空题的题干常常只有一句「Complete the following passage…」，没有原文就没法作答。
    PASSAGE_JS = """
    var root = arguments[0];
    if (!root || !root.childNodes) { return ''; }
    var out = [];
    var index = 0;
    function walk(node) {
      var kids = node.childNodes;
      for (var i = 0; i < kids.length; i++) {
        var child = kids[i];
        if (child.nodeType === 3) {
          out.push(child.nodeValue);
        } else if (child.nodeType === 1) {
          var tag = child.tagName.toLowerCase();
          if (tag === 'input' || tag === 'textarea') {
            index += 1;
            out.push(' [' + index + '] ');
          } else if (tag === 'script' || tag === 'style') {
            continue;
          } else if (tag === 'br') {
            out.push('\\n');
          } else {
            walk(child);
          }
        }
      }
    }
    walk(root);
    return out.join('');
    """

    @classmethod
    def read_passage(cls, driver, container, limit: int = 6000) -> str:
        """读出填空题原文，空格处标成 [1]…[N]；读不到就返回空串。"""
        if driver is None or container is None:
            return ""
        try:
            raw = driver.execute_script(cls.PASSAGE_JS, container)
        except Exception:
            return ""
        if not raw:
            return ""
        text = re.sub(r"[ \t\u00a0]+", " ", str(raw))
        text = re.sub(r"\n\s*\n\s*", "\n", text).strip()
        return text[:limit]

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        title_elem = WebDriverHelper.safe_find_element(driver, Selectors.QUESTION_TITLE, container)
        text = title_elem.text.strip() if title_elem else ""
        text = re.sub(r"^\d+[.、)\]]\s*", "", text)

        inputs = WebDriverHelper.safe_find_elements(driver, self.FILL_INPUTS, container)
        inputs.sort(key=lambda x: int(
            x.find_element(By.XPATH, './ancestor::span[@class="fe-scoop"]').get_attribute('data-scoop-index') or 0))

        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.FILL_IN,
            element=container,
            inputs=inputs,
            directions=directions,
            passage=self.read_passage(driver, container),
        )


class VideoStrategy(QuestionParserStrategy):
    """纯视频页面检测策略"""

    def can_parse(self, container, driver) -> bool:
        videos = container.find_elements(By.TAG_NAME, 'video')
        if not videos:
            videos = container.find_elements(By.CSS_SELECTOR,
                                             '.video-js, .video-box, .question-video-player, video')

        if not videos:
            return False

        popup_questions = container.find_elements(By.CSS_SELECTOR,
                                                  '.popupBox .question-common-abs-choice, .questionReplyBox .question-common-abs-choice')

        if popup_questions:
            return False

        has_real_questions = (
                container.find_elements(By.CSS_SELECTOR,
                                        '.question-common-abs-choice:not(.popupBox *), '
                                        '.question-inputbox:not(.popupBox *), '
                                        '.option-wrap:not(.popupBox *), '
                                        '.fe-scoop') or
                container.find_elements(By.CSS_SELECTOR,
                                        'input[type="text"]:not(.ant-input)') or
                # 「看视频并总结大意」这类页面：左边视频、右边一个作答框。
                # 只要页面上有能填答案的框，就不能整页当成纯视频处理 ——
                # 否则播放完就返回，答案框空着被提交（实测踩过）。
                container.find_elements(By.CSS_SELECTOR,
                                        'textarea, .ant-input, .ant-input-affix-wrapper input')
        )

        if has_real_questions:
            return False

        return True

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        return Question(
            number=question_number,
            text="视频观看页面",
            q_type=QuestionType.VIDEO,
            element=container
        )


class VocabularyFlashcardStrategy(QuestionParserStrategy):
    """单词闪卡策略"""

    def can_parse(self, container, driver) -> bool:
        flashcard_indicators = [
            '.vocContainer',
            '.vocabulary-flashcard',
            '.flashcard-container',
            '.vocActions',
            '.vocabulary-actions'
        ]

        for indicator in flashcard_indicators:
            if container.find_elements(By.CSS_SELECTOR, indicator):
                has_choice = container.find_elements(By.CSS_SELECTOR, '.option-wrap, .question-common-abs-choice')
                if not has_choice:
                    return True

        return False

    def parse(self, container, driver, question_number: int, direction: str = "") -> Optional[Question]:
        return Question(
            number=question_number,
            text="单词闪卡",
            q_type=QuestionType.VOCABULARY_FLASHCARD,
            element=container
        )


class DropdownSelectStrategy(QuestionParserStrategy):
    """下拉选择填空题解析策略"""

    def can_parse(self, container, driver) -> bool:
        selects = container.find_elements(By.CSS_SELECTOR, '.scoop-select-wrapper, select, .ant-dropdown-trigger')
        return bool(selects)

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        blanks = []
        select_elements = container.find_elements(By.CSS_SELECTOR, '.scoop-select-wrapper')

        for i, elem in enumerate(select_elements):
            context = ""
            try:
                context_elem = elem.find_element(By.XPATH, './ancestor::li')
                context = context_elem.text.strip()
            except Exception:
                pass

            options = []
            try:
                hidden_div = elem.find_element(By.CSS_SELECTOR, 'div[style*="visibility: hidden"]')
                option_elems = hidden_div.find_elements(By.TAG_NAME, 'i')
                for opt in option_elems:
                    # 这些 <i> 在 visibility:hidden 的容器里：Selenium 的 .text 只返回
                    # 可见文本 → 恒为空串（实测「页面词库缺失」就是这么来的，收录好的
                    # 答案全被挡回 AI）。改读 textContent，隐藏元素也能拿到。
                    text = (opt.text or "").strip()
                    if not text:
                        try:
                            text = (driver.execute_script(
                                "return (arguments[0].textContent || '').trim();", opt) or "").strip()
                        except Exception:
                            text = ""
                    if text and text not in options:
                        options.append(text)
            except Exception:
                try:
                    select = elem.find_element(By.TAG_NAME, 'select')
                    option_elems = select.find_elements(By.TAG_NAME, 'option')
                    for opt in option_elems:
                        text = opt.text.strip()
                        if text and text not in ['', '点击选择']:
                            options.append(text)
                except Exception:
                    pass

            blanks.append({
                'index': i,
                'context': context,
                'element': elem,
                'options': options
            })

        title_elem = WebDriverHelper.safe_find_element(driver, Selectors.QUESTION_TITLE, container)
        text = title_elem.text.strip() if title_elem else "下拉选择填空"
        if directions:
            text = directions + text
        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.DROPDOWN_SELECT,
            element=container,
            banked_blanks=blanks,
            banked_options=list(set(opt for b in blanks for opt in b['options']))
        )


class PromptBuilder:
    """Prompt构建器"""

    def __init__(self, ai_client):
        self.ai_client = ai_client

    def build(self, questions: List[Question], global_directions: str = "") -> str:
        lines = []

        if len(self.ai_client.accumulated_passages) > 1:
            lines.append(f"【注意】本章节共有 {len(self.ai_client.accumulated_passages)} 份材料/音频转写，请根据问题判断使用哪份。")
            lines.append("")

        effective_directions = global_directions
        if not effective_directions and questions:
            effective_directions = questions[0].directions

        if effective_directions:
            lines.append(f"【题目指示】{effective_directions}")
            lines.append("")

        type_counts = {}
        for q in questions:
            type_counts[q.q_type] = type_counts.get(q.q_type, 0) + 1

        if QuestionType.VOCABULARY_TEST in type_counts:
            lines.extend(self._vocabulary_test_hints())

        if QuestionType.BANKED_CLOZE in type_counts:
            lines.extend(self._banked_cloze_hints())

        for q in questions:
            builder_method = self._get_builder_method(q.q_type)
            lines.extend(builder_method(q))

        lines.extend(self._format_instructions(type_counts))

        return '\n'.join(lines)

    def _vocabulary_test_hints(self) -> List[str]:
        return [
            "【重要提示】这是词汇测试题，包含以下类型：",
            "- 类型A（英文→中文）：题干是英文单词，选项是中文释义",
            "- 类型B（中文→英文）：题干是中文释义，选项是英文单词",
            "- 类型C（语境填空）：题干是英文句子，选项是单词填入",
            "请仔细分析每道题的具体类型，选择最准确的答案。\n"
        ]

    def _banked_cloze_hints(self) -> List[str]:
        return [
            "【重要提示】这是选词填空题，请从给定的单词列表中选择最合适的填入空白处。\n",
            "【格式要求】只需返回答案本身，不要添加括号注释、不要解释、不要变形说明！"
        ]

    def _get_builder_method(self, q_type: QuestionType) -> Callable[[Question], List[str]]:
        builders = {
            QuestionType.VOCABULARY_TEST: self._build_vocab_test,
            QuestionType.BANKED_CLOZE: self._build_banked_cloze,
            QuestionType.DROPDOWN_SELECT: self._build_dropdown_select,
            QuestionType.SINGLE_CHOICE: self._build_single_choice,
            QuestionType.MULTIPLE_CHOICE: self._build_multiple_choice,
            QuestionType.SORTING: self._build_sorting,
            QuestionType.FILL_IN: self._build_fill_in,
            QuestionType.TEXT: self._build_text,
            QuestionType.MY_VOICE_TEXT: self._build_my_voice_text,
            QuestionType.VIDEO: lambda q: [],
            QuestionType.VOCABULARY_FLASHCARD: lambda q: [],
            QuestionType.LISTENING_FILL_IN: self._build_listening_fill_in,
            QuestionType.LISTENING_CHOICE: self._build_listening_choice,
            QuestionType.VIDEO_CHOICE: self._build_video_choice,
        }
        return builders.get(q_type, self._build_unknown)

    def _build_sorting(self, q: Question) -> List[str]:
        is_match = any(word in (q.directions or q.text or "").lower()
                       for word in ("match", "配对", "相连", "对应"))
        has_image_hint = any("[图片线索" in (opt.text or "") for opt in q.options)

        if is_match:
            lines = [
                f"{q.number}. 【配对题】{q.text}",
                "   这是「把描述与图片/选项配对」的题。",
            ]
            for opt in q.options:
                lines.append(f"   {opt.letter}. {opt.text}")
            lines.append("")
            if has_image_hint:
                lines.append("   注意：上面 [图片线索] 里是图片自带的文件名或替代文字，"
                             "它是判断「这张图是谁」的可靠依据（如 confucius / zhangqian）。")
            else:
                lines.append("   注意：这些选项没有任何文字线索，你只能凭常识推断人物长相。"
                             "**如果无法确定，就只回「不确定」，不要硬猜。**")
            lines.append("")
            lines.append("   要求：按左边 1、2、3… 的顺序，给出右边对应项的字母，"
                         "格式如：1-B 2-A 3-C（不要别的文字）")
            lines.append("")
            return lines

        lines = [
            f"{q.number}. 【排序题】{q.text}",
            "   请根据前面提供的音频/视频转写或材料内容，将以下选项按出现顺序排序。",
        ]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        lines.append("   要求：只返回排序后的选项字母，格式如：B E A D C F")
        lines.append("")
        return lines

    def _build_video_choice(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【视频选择题】{q.text}",
            "   请根据前面提供的视频转写内容选择最佳答案。",
        ]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _build_listening_choice(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【听力选择题】{q.text}",
            "   请根据前面提供的音频转写内容选择最佳答案。",
        ]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _build_listening_fill_in(self, q: Question) -> List[str]:
        media_label = "视频填空题" if any(kw in (q.directions or "").lower() for kw in ['watch', 'video', 'clip', 'view']) else "听力填空题"
        lines = [
            f"{q.number}. 【{media_label}】",
            f"{q.text}",
            "",
            "【答题要求】",
            "1. 这是一个音频/视频理解题，请同时根据转写内容和每个空的左右上下文作答",
            "2. 每空只填写缺失部分，不要重复空格左边或右边已经出现的词",
            "3. 如果左边已有 be going to / I'm going to / we're 等结构，答案要能直接拼进原句并保持语法通顺",
            "4. 不要填整句，不要串用其他空的答案",
            ""
        ]

        for blank in q.banked_blanks:
            lines.append(f"   空{blank['index'] + 1}: {blank['sentence']}")
            if blank.get('left_context') or blank.get('right_context'):
                lines.append(f"      左侧: {blank.get('left_context', '')}")
                lines.append(f"      右侧: {blank.get('right_context', '')}")

        lines.append("")
        return lines

    def _build_vocab_test(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【词汇题】{q.text}"]
        text_clean = re.sub(r"^\d+[.、)\]]\s*", "", q.text).strip()

        if bool(re.match(r"^[a-zA-Z\-]+$", text_clean)) and len(text_clean) <= 20:
            lines.append("   → 选择该英文单词的正确中文释义")
        elif bool(re.search(r"[\u4e00-\u9fff]", text_clean)):
            lines.append("   → 选择该中文释义对应的正确英文表达")
        elif "_" in text_clean or len(text_clean) > 50:
            lines.append("   → 根据句子语境选择最合适的单词")

        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _build_banked_cloze(self, q: Question) -> List[str]:
        is_phrase = q.is_phrase_mode
        lines = [
            f"{q.number}. 【选词填空】请从以下选项中选择最合适的{'短语' if is_phrase else '单词'}填入空白处：",
            f"   可选{'短语' if is_phrase else '单词'}: {', '.join(q.banked_options)}",
        ]

        if is_phrase:
            lines.extend([
                "注意：这是短语填空！请填写完整短语（如 'in advance' 而不是 'advance'）。",
                "必要时需要改变短语的形式（如时态、单复数等）。",
            ])
        else:
            lines.extend([
                " 注意：必要时需要改变单词形式（如时态、单复数等）。",
            ])

        lines.append("")

        for i, blank in enumerate(q.banked_blanks, 1):
            context = blank['context'][:250] + "..." if len(blank['context']) > 250 else blank['context']
            context = re.sub(r'<[^>]+>', '', context)
            lines.append(f"   空{i}: {context}")

        lines.append("")
        lines.append("   要求：")

        if is_phrase:
            lines.append("1. 必须填写完整短语（不要只填部分）")
            lines.append("2. 按顺序给出答案，格式：1.in advance 2.make the most of ...")
        else:
            lines.append("1. 按顺序给出答案，格式：1.word1 2.word2 ...")

        lines.append("")
        return lines

    def _build_single_choice(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【单选】{q.text}"]
        for opt in q.options:
            lines.append(f" {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _build_multiple_choice(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【多选】{q.text}", "   （注意：本题有多个正确答案，请把正确的都选上）"]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _build_fill_in(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【填空】{q.text}"]
        if len(q.inputs) > 1:
            lines.append(f"   （共 {len(q.inputs)} 个空）")
        passage = getattr(q, "passage", "") or ""
        if passage:
            lines.append(f"   原文（[1]…[{len(q.inputs)}] 就是要填的空，请按上下文作答）：")
            lines.append(f"   {passage}")
        lines.append("")
        return lines

    def _build_text(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【简答题】{q.text}"]
        if len(q.inputs) > 1:
            lines.append(f"   （共 {len(q.inputs)} 小题）")
        lines.append("   （请提供简洁准确的回答，如果不是翻译题，那么只用英文回答）")
        lines.append("")
        return lines

    def _build_my_voice_text(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【My voice文字作答】{q.text}",
            "   要求：只输出一段可直接填写到输入框的英文介绍。",
            "   限制：500字符以内，不要说明自己无法录音或上传。",
            "",
        ]
        return lines

    def _build_unknown(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【题】{q.text}"]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _format_instructions(self, type_counts: Dict[QuestionType, int]) -> List[str]:
        lines = ["-" * 50, "请按以下格式回答："]

        has_single = (
                QuestionType.SINGLE_CHOICE in type_counts or
                QuestionType.VOCABULARY_TEST in type_counts or
                QuestionType.LISTENING_CHOICE in type_counts or
                QuestionType.VIDEO_CHOICE in type_counts
        )
        has_multiple = QuestionType.MULTIPLE_CHOICE in type_counts

        if has_single and not has_multiple:
            lines.append("单选题: 直接返回选项字母，如：A 或 1.A")
            lines.append("注意：每道题只选一个答案！")

        elif has_multiple and not has_single:
            lines.append("多选题: 返回多个字母，如：AB 或 1.AB")
            lines.append("注意：每道题可能有一个或多个正确答案！")

        elif has_single and has_multiple:
            lines.append("混合题型：")
            lines.append("- 单选题: 返回单个字母，如：A")
            lines.append("- 多选题: 返回多个字母，如：AB")
            lines.append("请仔细判断每道题是单选还是多选！")
            lines.append("判断依据：题目明确标注'多选'或有多个正确选项时选多个，否则单选")

        if QuestionType.BANKED_CLOZE in type_counts or QuestionType.DROPDOWN_SELECT in type_counts:
            lines.append("选词/选择填空: 1.word1 2.word2 ...")

        if QuestionType.FILL_IN in type_counts:
            lines.append("填空题: 1.答案1 2.答案2 ...")
            lines.append("括号汉译英必须照抄原文：题干括号里是中文时，答案要用本板块英文原文里出现过的原词原句，禁止同义替换（如 绿色产业链→green industry chain，森林资源可持续利用→sustainable forest utilization，生态保护→ecological conservation）。")

        if QuestionType.TEXT in type_counts:
            lines.append("简答题: 1.答案内容...")

        if QuestionType.MY_VOICE_TEXT in type_counts:
            lines.append("My voice文字作答: 1.英文介绍内容（500字符以内）")

        if QuestionType.SORTING in type_counts:
            lines.append("排序题: 直接返回排序后的字母，如：B E A D C F")

        lines.append("-" * 50)
        return lines

    def _build_dropdown_select(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【选择填空】请从选项中选择合适的词填入空白处：",
            f"   可选选项: {', '.join(q.banked_options)}",
            ""
        ]

        for i, blank in enumerate(q.banked_blanks, 1):
            context = blank['context'][:200] + "..." if len(blank['context']) > 200 else blank['context']
            context = re.sub(r'<[^>]+>', '', context)
            lines.append(f"   空{i}: {context}")

        lines.append("")
        lines.append("要求：按顺序给出答案，格式：1.do 2.make ...")
        lines.append("")
        return lines


#: 大模型拒答/求助的典型措辞。这类文本不是答案：填进空里只会污染作答，
#: 还会让页面统计报成"成功"。判断放在模块级，各处（含测试替身）都能直接调用。
REFUSAL_PATTERNS = (
    "无法确定", "无法作答", "无法提供", "无法给出", "无法回答", "无法完成", "无法判断",
    "请提供", "请补充", "请给出", "缺少", "未提供", "没有提供", "信息不足",
    "cannot determine", "cannot answer", "unable to answer", "unable to determine",
    "insufficient information", "not enough information", "please provide",
)

def looks_like_ai_refusal(text: str) -> bool:
    """这段文本是不是大模型的拒答说明，而不是答案。"""
    if not text:
        return False
    body = str(text).strip()
    # 拒答词最短只有 4 字（如「无法作答」），下限设太大会让短拒答漏网、被当答案填进页面
    if len(body) < 4:
        return False
    haystack = body.lower()
    return any(pattern.lower() in haystack for pattern in REFUSAL_PATTERNS)


def page_visible_text(driver, limit: int = 6000) -> str:
    """整页可见文字（讨论板与「AI 要文章」兜底补料共用一份实现）。"""
    try:
        raw = driver.execute_script(
            "return (document.body && document.body.innerText) || '';")
    except Exception:
        return ""
    text = re.sub(r"[ \t\u00a0]+", " ", str(raw or ""))
    text = re.sub(r"\n\s*\n\s*", "\n", text).strip()
    return text[:limit]


def clean_harvest_entries(entries) -> List[str]:
    """收录前的答案清洗 + 拆分（模块级便于测试）。

    _last_applied 是按「题」记录的：一道 6 空的听力填空只有 1 条记录，内容是
    「1. xxx 2. yyy ……」整串。收录时必须把它拆回 6 条独立答案，题库里才是 6 条
    —— 否则「答案条数与页面一致」永远对不上，收录的小节置信度上不去
    （实测就是被这一条卡在 low < medium：收录了却不被调用）。分隔线等格式渣一并清掉。
    """
    out: List[str] = []
    for text in entries or []:
        # 一行里写完整页的空（实测下拉选人页的收录串是
        # 「选词/选择填空: 1.E 2.A 3.C …」）：逐空拆开成独立字母。
        # 以前整串只算 1 条 →「答案条数与页面一致」对不上 → 收录了却调不动。
        joined = " ".join(str(text or "").split())
        inline = re.findall(r'(?<![\w])(\d{1,3})\s*[.、:：)）]\s*([A-Za-z])(?![A-Za-z0-9])',
                            joined)
        if len(inline) >= 2:
            numbers = [int(number) for number, _letter in inline]
            letters = [letter.upper() for _number, letter in inline]
            if (numbers == list(range(1, len(inline) + 1))
                    and all(letter in "ABCDEFGH" for letter in letters)):
                out.extend(letters)
                continue
        raw_lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
        numbered = [ln for ln in raw_lines
                    if re.match(r'^\d{1,3}\s*[.、:：)）]\s*\S', ln)]
        if len(numbered) >= 2 and all(len(ln) <= 80 for ln in numbered):
            # 每条编号行就是一个独立的空：拆开存
            for ln in numbered:
                clean = re.sub(r'^\d{1,3}\s*[.、:：)）]\s*', '', ln).strip()
                clean = re.sub(r'\s*[-—–_=*#.]{3,}\s*$', '', clean).strip()
                if clean:
                    out.append(clean)
            continue
        lines = [ln for ln in raw_lines
                 if not re.fullmatch(r'[\s\-—–_=*#·.]{3,}', ln)]
        merged = " ".join(lines)
        merged = re.sub(r'\s*[-—–_=*#.]{3,}\s*$', '', merged).strip()
        merged = re.sub(r'\A\s*[-—–_=*#.]{3,}\s*', '', merged).strip()
        if merged:
            out.append(merged)
    return out


def page_transcription_hint(driver) -> str:
    """从页面英文文字里挑词，做 Whisper 的 initial_prompt（AISolver 与 VideoHandler 共用）。

    页面上印着本课词汇表（French horn、brass instruments…），转写时喂给
    Whisper 能显著纠偏 —— 实测 base 模型把 French horns 听成 French homes，
    视听选择题就答错了。
    """
    try:
        text = page_visible_text(driver, 3000)
    except Exception:
        return ""
    seen: set = set()
    words: List[str] = []
    for word in re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", text):
        key = word.lower()
        if key in seen:
            continue
        seen.add(key)
        words.append(word)
        if len(words) >= 40:
            break
    return " ".join(words)


class AnswerExecutor:
    """答案执行器 - 执行答案填写"""

    ANSWER_LABEL_PATTERN = r'(?:My voice文字作答|简答题|选词/选择填空|填空题|答案|选词填空|翻译|Answer)'
    NUMBER_PREFIX_PATTERN = r'(?:空\s*)?\d+\s*[.、:：\)\]]|Blank\s*\d+\s*[.、:：\)\]]'

    def __init__(self, driver):
        self.driver = driver

    def execute(self, question: Question, answer: str) -> bool:
        executors = {
            QuestionType.SINGLE_CHOICE: self._fill_single_choice,
            QuestionType.LISTENING_CHOICE: self._fill_single_choice,
            QuestionType.VIDEO_CHOICE: self._fill_single_choice,
            QuestionType.VOCABULARY_TEST: self._fill_single_choice,
            QuestionType.MULTIPLE_CHOICE: self._fill_multiple_choice,
            QuestionType.SORTING: self._fill_sorting,
            QuestionType.BANKED_CLOZE: self._fill_banked_cloze,
            QuestionType.DROPDOWN_SELECT: self._fill_dropdown_select,
            QuestionType.FILL_IN: self._fill_fill_in,

            QuestionType.SCALE_RATE: self._fill_scale_rate,
            QuestionType.TEXT: self._fill_text,
            QuestionType.MY_VOICE_TEXT: self._fill_text,
            QuestionType.LISTENING_FILL_IN: self._fill_listening_fill_in,
        }

        executor = executors.get(question.q_type, self._fill_unknown)
        try:
            return executor(question, answer)
        except Exception as exc:
            # 单题异常不许中断整批任务：页面刷新导致元素失效是最常见的一种
            print(f"\t⚠ 第 {question.number} 题填写异常，跳过本题: {str(exc)[:80]}")
            return False

    #: 评分题默认分值（这类题是主观自评，没有标准答案；4 分属于常见的中上选择）
    SCALE_DEFAULT = 4

    def _fill_scale_rate(self, q: Question, answer: str = "") -> bool:
        """评分题：每条陈述点一个数字（实测 DOM：evaluation-slider-reply-*）。

        点完校验那个数字的 class 不再是 unselected —— 注意 "unselected" 里也含
        "select"，必须先排除 un，不然会把没选的当成已选。
        """
        container = getattr(q, "element", None)
        if container is None:
            return False
        target = self.SCALE_DEFAULT
        match = re.search(r"\b([1-5])\b", str(answer or ""))
        if match:
            target = int(match.group(1))

        wrappers = container.find_elements(
            By.CSS_SELECTOR, '.evaluation-slider-reply-component-wrapper')
        if not wrappers:
            print("\t⚠ 评分题没找到陈述，跳过本题")
            return False

        print(f"\t评分题：{len(wrappers)} 条陈述，每条点 {target} 分")
        ok_rows = 0
        for index, wrapper in enumerate(wrappers, 1):
            selected = False
            for _attempt in (1, 2):
                try:
                    items = wrapper.find_elements(
                        By.CSS_SELECTOR,
                        '.evaluation-slider-reply-component-wrapper_option_item')
                except Exception:
                    items = []
                hit = None
                for item in items:
                    try:
                        if (item.text or "").strip() == str(target):
                            hit = item
                            break
                    except Exception:
                        continue
                if hit is None:
                    continue
                try:
                    WebDriverHelper.safe_click(self.driver, hit)
                except Exception:
                    pass
                time.sleep(0.3)
                try:
                    cls = (hit.get_attribute("class") or "")
                except Exception:
                    cls = ""
                low = cls.lower()
                if "selected" in low and "unselected" not in low:
                    selected = True
                    break
                time.sleep(0.4)
            if selected:
                ok_rows += 1
            else:
                print(f"\t第 {index} 条陈述点 {target} 没确认到选中态")
        print(f"\t评分题完成 {ok_rows}/{len(wrappers)} 条")
        return ok_rows == len(wrappers) and ok_rows > 0

    #: 知识库同一个空常写成「英式; 美式」（victimize; victimise）—— 填空只能填一个，
    #: 必须取第一个：整串填进去必然判错（实测 6/8 全错就是它造成的）。
    @staticmethod
    def _single_answer(ans: str) -> str:
        text = str(ans or "").strip()
        if len(text) < 2:
            return text
        if re.fullmatch(r"[A-La-l](?:\s*[,，、;；/|和及]\s*[A-La-l])*", text):
            return text                      # 选择题多字母答案不动
        parts = re.split(r"\s*[;；/|]\s*", text)
        first = parts[0].strip() if parts else text
        return first or text

    def _fill_single_choice(self, q: Question, answer: str) -> bool:
        answer_letter = self._extract_letter(answer)
        if not answer_letter:
            return False

        print(f"\t寻找选项: {answer_letter}")
        print(f"\t可用选项: {[opt.letter for opt in q.options]}")

        for opt in q.options:
            if opt.letter.upper() == answer_letter.upper():
                print(f"\t点击选项 {opt.letter}: {opt.text[:30]}...")
                return WebDriverHelper.safe_click(self.driver, opt.element)

        try:
            idx = ord(answer_letter.upper()) - ord('A')
            if 0 <= idx < len(q.options):
                opt = q.options[idx]
                print(f"\t通过索引匹配选项 {opt.letter}: {opt.text[:30]}...")
                return WebDriverHelper.safe_click(self.driver, opt.element)
        except Exception:
            pass

        return False

    def _fill_multiple_choice(self, q: Question, answer: str) -> bool:
        # 选项字母到 H：多选题最多可能到 H（如 Collocation 匹配题的 A-I 清单），
        # 原先只认 A-D，答案是 E 及以后的题会静默填不上。
        letters = re.findall(r'[A-Z]', answer.upper())
        selected = []
        unmatched = []
        already_selected = []

        for letter in letters:
            matched = False
            for opt in q.options:
                if opt.letter.upper() == letter:
                    matched = True
                    if opt.is_selected:
                        already_selected.append(letter)
                    elif WebDriverHelper.safe_click(self.driver, opt.element):
                        selected.append(letter)
                    break
            if not matched:
                unmatched.append(letter)

        if unmatched:
            # 不再静默少选：模型给的字母在页面选项里不存在时明确告警
            print(f"\t⚠ 模型给的字母 {''.join(unmatched)} 在页面选项里不存在，已跳过")

        # 取消不该选的：页面上残留的选中态（上次作答/预选）不在目标集合里就点掉，
        # 否则多选题会带着错误的多余选项提交
        wanted = {letter.upper() for letter in letters}
        for opt in q.options:
            if opt.is_selected and opt.letter.upper() not in wanted:
                if WebDriverHelper.safe_click(self.driver, opt.element):
                    print(f"\t取消多余选中 {opt.letter}")

        if letters and not unmatched and set(letters) <= set(already_selected + selected):
            # 目标选项全部处于选中状态（如重复进入已答页面）→ 视为成功
            return True

        return bool(selected)

    def _fill_sorting(self, q: Question, answer: str) -> bool:
        # 诊断：把解析到的选项（字母 + 文字/图片线索）和模型答案都打出来，
        # 配对题出错时一眼能看出「拖动的是哪一列」「字母是不是数字」。
        print(f"\t配对/排序题选项: {[(opt.letter, (opt.text or '')[:24]) for opt in q.options]}")
        print(f"\t模型原始答案: {answer[:120]!r}")
        order = self._parse_sorting_order(answer, [opt.letter for opt in q.options])
        if not order:
            print("\t⚠ 没能从模型答案里解析出有效字母序，本题不拖动")
            return False

        print(f"\t排序答案: {' '.join(order)}")
        if self._apply_sorting_by_drag(q, order):
            print(f"\t✅ 鼠标拖动成功，拖完页面顺序: {' '.join(self._current_sorting_order(q))}")
            return True
        print(f"\t⚠ 鼠标拖动没成功，当前页面顺序: {' '.join(self._current_sorting_order(q))}")

        if self._apply_sorting_by_js(q, order):
            print(f"\tJS 重排后 DOM 顺序: {' '.join(self._current_sorting_order(q))}")
            if self._verify_sorting_visually(q, order) is not False:
                return True
            print("\t⚠ 视觉复核不一致：DOM 改动了但页面没接受，本题按未完成处理")
            return False
        print(f"\t⚠ JS 重排也没成功，页面顺序: {' '.join(self._current_sorting_order(q))}")

        # 配对题最容易错在方向上：模型说「1-B」是「第 1 条描述配 B 这张图」，
        # 而拖动接口要的是「第 1 个位置放哪个字母」。两者互为反排列，
        # 正向排不进去时反过来再排一次，并把实际采用的方向打进日志。
        inverse = self._invert_sorting_order(order, [opt.letter for opt in q.options])
        if inverse and inverse != order:
            print(f"\t正向排不进去，改用反向配对: {' '.join(inverse)}")
            if self._apply_sorting_by_drag(q, inverse):
                return True
            if self._apply_sorting_by_js(q, inverse):
                return True

        return False

    def _parse_sorting_order(self, answer: str, valid_letters: List[str]) -> List[str]:
        valid = [letter.upper() for letter in valid_letters if letter]
        valid_set = set(valid)
        if not valid:
            return []

        numbered = re.findall(r'\d+\s*[.、)\]]\s*([A-Z])\b', answer.upper())
        candidates = numbered if numbered else re.findall(r'\b([A-Z])\b', answer.upper())

        if len(candidates) < len(valid):
            compact = re.findall(r'[A-Z]+', answer.upper())
            for chunk in compact:
                letters = [ch for ch in chunk if ch in valid_set]
                if len(letters) >= len(valid):
                    candidates = letters
                    break

        order = []
        for letter in candidates:
            letter = letter.upper()
            if letter in valid_set and letter not in order:
                order.append(letter)

        return order if len(order) == len(valid) else []

    @staticmethod
    @staticmethod
    def _pairing_to_order(answer: str, valid_letters: List[str]) -> List[str]:
        """把「1-C 2-A 3-D 4-B」这种配对，转成「每个位置放哪个字母」的排列。

        配对题模型天然这么回答；若解析出来的选项字母是 A/B/C/D 而答案里是
        「序号+字母」，这一步保证顺序不会被打乱。
        """
        letters = [letter.upper() for letter in valid_letters if letter]
        # 两种写法都要认： 「1-C」（编号在前，模型最常用）与 「A-3」（字母在前）
        slots = {}
        for number, letter in re.findall(
                r'(?<!\w)(\d{1,2})\s*[.、)\]\-—:：]\s*([A-Za-z])(?!\w)', answer):
            slots[int(number)] = letter.upper()
        if not slots:
            for letter, number in re.findall(
                    r'(?<!\w)([A-Za-z])\s*[.、)\]\-—:：]\s*(\d{1,2})(?!\w)', answer):
                slots[int(number)] = letter.upper()
        if not slots:
            return []
        if sorted(slots) != list(range(1, len(letters) + 1)):
            return []
        order = [slots[index] for index in range(1, len(letters) + 1)]
        return order if set(order) == set(letters) else []

    @staticmethod
    def _invert_sorting_order(order: List[str], valid_letters: List[str]) -> List[str]:
        """把「位置 → 字母」的排列反过来，得到「字母 → 位置」的等价排列。

        例：order=[B, A, C]（1 号位放 B），反排列仍是 [B, A, C]；
        order=[B, C, A] 的反排列是 [C, A, B]。
        """
        letters = [letter.upper() for letter in valid_letters if letter]
        if not letters or len(order) != len(letters):
            return []
        if set(order) != set(letters):
            return []
        inverted = [""] * len(letters)
        for slot_index, letter in enumerate(order):
            try:
                letter_index = letters.index(letter)
            except ValueError:
                return []
            inverted[letter_index] = letters[slot_index]
        return inverted

    def _get_sorting_items(self, q: Question) -> List[Dict[str, Any]]:
        items = []
        elems = q.element.find_elements(By.CSS_SELECTOR, '.sequence-reply-view-item-text')
        for elem in elems:
            text = elem.text.strip()
            letter = ""
            try:
                spans = elem.find_elements(By.TAG_NAME, 'span')
                if spans:
                    letter = spans[0].text.strip().replace('.', '').replace(')', '').upper()
            except Exception:
                pass
            if not letter:
                match = re.match(r'^([A-Z])[\s.、)]*', text)
                if match:
                    letter = match.group(1).upper()
            if letter:
                items.append({'letter': letter, 'element': elem})
        return items

    def _current_sorting_order(self, q: Question) -> List[str]:
        return [item['letter'] for item in self._get_sorting_items(q)]

    def _apply_sorting_by_drag(self, q: Question, order: List[str]) -> bool:
        """按目标顺序逐个拖动。

        实测踩过的坑：拖完立刻发下一次拖动，页面动画还没落位，第二个元素会插到最前面
        （目标 B D A C 结果变成 D B A C —— 正好前两个对调）。所以每次拖完都等
        「页面顺序真的变了」再继续；整轮结束仍不对就再排一轮。
        """
        try:
            for attempt in range(1, 3):          # 最多排两轮
                for target_index, target_letter in enumerate(order):
                    items = self._get_sorting_items(q)
                    current_order = [item['letter'] for item in items]
                    if current_order == order:
                        return True
                    if target_index >= len(items) or current_order[target_index] == target_letter:
                        continue

                    source = next((item['element'] for item in items
                                   if item['letter'] == target_letter), None)
                    target = items[target_index]['element']
                    if not source or not target:
                        return False

                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});", source)
                    time.sleep(0.2)
                    before = current_order
                    ActionChains(self.driver).click_and_hold(source).pause(0.3) \
                        .move_to_element(target).pause(0.3).release().perform()

                    # 等页面把这次拖动落实（顺序发生变化）再发下一次
                    deadline = time.time() + 3
                    changed = False
                    while time.time() < deadline:
                        time.sleep(0.3)
                        now = self._current_sorting_order(q)
                        if now != before:
                            changed = True
                            break
                    if not changed:
                        print(f"\t⚠ 拖动 {target_letter} 后页面顺序没变（还是 {' '.join(before)}）")
                    else:
                        print(f"\t拖动 {target_letter}: {' '.join(before)} → "
                              f"{' '.join(self._current_sorting_order(q))}")
                    time.sleep(0.4)              # 再给动画一点时间

                if self._current_sorting_order(q) == order:
                    return True
                print(f"\t⚠ 第 {attempt} 轮排完仍不对，当前顺序: "
                      f"{' '.join(self._current_sorting_order(q))}")
            return self._current_sorting_order(q) == order
        except Exception as e:
            print(f"\t拖拽排序失败: {str(e)[:60]}")
            return False

    def _verify_sorting_visually(self, q: Question, order: List[str]) -> Optional[bool]:
        """拖完让视觉模型看一眼页面：图片当前顺序是不是我们要的。

        DOM 顺序会被前端重绘覆盖 —— 之前就出现过「日志说排好了、页面还是 A B C D」。
        所以这里拿题目区域的截图让视觉模型读一遍当前顺序，读不出就返回 None（不判死）。
        """
        element = getattr(q, "element", None)
        if element is None:
            return None
        try:
            shot = element.screenshot_as_base64 or ""
        except Exception:
            return None
        if not shot:
            return None
        letters = [opt.letter for opt in (getattr(q, "options", None) or [])]
        chat = getattr(self, "_vision_chat", None)
        if chat is None:
            # 本类没有视觉调用能力时直接不判定，避免整段复核因 AttributeError 被记成整题失败
            return None
        model = getattr(getattr(self, "config", None), "vision_model", "") or "qwen3.8-omni-flash"
        prompt = (
            "这是一道拖拽排序/配对题。请**只**看图中可拖动项目当前从上到下的顺序，"
            f"按顺序输出它们的字母（可选项：{'、'.join(letters)}）。"
            "格式：B D A C。只回这一行，不要解释。"
        )
        answer, _ = chat(model, [{"type": "text", "text": prompt},
                                              {"type": "image_url",
                                               "image_url": {"url": f"data:image/png;base64,{shot}"}}])
        parsed = self._pairing_to_order(answer, letters) or \
            [ch for ch in (answer or "").upper() if ch in letters]
        seen: List[str] = []
        for letter in parsed:
            if letter in letters and letter not in seen:
                seen.append(letter)
        if len(seen) != len(letters):
            print(f"      👁 视觉复核没读出完整顺序（{answer[:40]!r}），不判定")
            return None
        ok = seen == order
        print(f"      👁 视觉复核：页面当前 {' '.join(seen)}，目标 {' '.join(order)} → "
              f"{'一致 ✔' if ok else '不一致 ✘'}")
        return ok

    def _apply_sorting_by_js(self, q: Question, order: List[str]) -> bool:
        js = """
        const root = arguments[0];
        const order = arguments[1];
        const wrapper = root.querySelector('.sortable-list-wrapper');
        if (!wrapper) return false;

        const children = Array.from(wrapper.children);
        const pairs = [];
        for (let i = 0; i < children.length; i++) {
            const node = children[i];
            if (!node.classList.contains('sequence-reply-view-item-text')) continue;
            const letterText = (node.querySelector('span')?.textContent || node.textContent || '').trim();
            const letter = (letterText.match(/[A-Z]/) || [''])[0];
            const numberNode = i > 0 && children[i - 1].classList.contains('sortable-list-question-no')
                ? children[i - 1]
                : null;
            if (letter) pairs.push({ letter, numberNode, itemNode: node });
        }

        const byLetter = new Map(pairs.map(pair => [pair.letter, pair]));
        if (!order.every(letter => byLetter.has(letter))) return false;

        order.forEach((letter, index) => {
            const pair = byLetter.get(letter);
            if (pair.numberNode) {
                const strong = pair.numberNode.querySelector('strong');
                if (strong) strong.textContent = String(index + 1);
                wrapper.appendChild(pair.numberNode);
            }
            wrapper.appendChild(pair.itemNode);
        });

        ['input', 'change', 'mouseup', 'drop', 'dragend', 'sortupdate'].forEach(type => {
            wrapper.dispatchEvent(new Event(type, { bubbles: true }));
        });

        const reactKey = Object.keys(wrapper).find(key => key.startsWith('__reactProps$'));
        if (reactKey) {
            const props = wrapper[reactKey];
            if (props && typeof props.onChange === 'function') props.onChange(order);
            if (props && typeof props.onSortEnd === 'function') props.onSortEnd({ oldIndex: 0, newIndex: 0 });
        }

        return Array.from(wrapper.querySelectorAll('.sequence-reply-view-item-text'))
            .map(node => ((node.querySelector('span')?.textContent || node.textContent || '').match(/[A-Z]/) || [''])[0])
            .join('') === order.join('');
        """
        try:
            result = self.driver.execute_script(js, q.element, order)
            time.sleep(0.5)
            return bool(result) and self._current_sorting_order(q) == order
        except Exception as e:
            print(f"\tJS排序失败: {str(e)[:60]}")
            return False

    def _fill_banked_cloze(self, q: Question, answer: str) -> bool:
        words = self._parse_banked_answer(answer, len(q.banked_blanks))
        is_phrase_mode = q.is_phrase_mode

        print(f"\t解析答案: {words}")
        print(f"\t填空数量: {len(q.banked_blanks)}")
        print(f"\t模式: {'短语' if is_phrase_mode else '单词'}")

        success_count = 0
        # 目标数量 = 「有输入框且有答案」的空；全部填成功才算成功（与其它填空函数口径一致）
        targets = sum(1 for blank, word in zip(q.banked_blanks, words)
                      if blank['input'] and word)

        for i, (blank, word) in enumerate(zip(q.banked_blanks, words)):
            if blank['input'] and word:
                try:
                    clean_word = word.strip()
                    matched = self._match_to_option(clean_word, q.banked_options, is_phrase_mode)
                    if matched:
                        clean_word = matched
                        print(f"        匹配到选项: {clean_word}")

                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                        blank['input']
                    )
                    time.sleep(0.3)

                    blank['input'].clear()
                    time.sleep(0.1)
                    blank['input'].send_keys(clean_word)

                    self.driver.execute_script("""
                        arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
                        arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
                        arguments[0].dispatchEvent(new Event('blur', {bubbles: true}));
                    """, blank['input'])

                    print(f"        空{i + 1}: {clean_word}")
                    success_count += 1

                except Exception as e:
                    error_msg = str(e)
                    print(f"      填空 {i + 1} 失败:{error_msg[:50]} ")
                    logger.error(f"详细错误: {error_msg}", exc_info=True)

        return success_count > 0 and success_count == targets

    def _match_to_option(self, answer: str, options: List[str], is_phrase_mode: bool) -> Optional[str]:
        if not answer or not options:
            return None

        answer_lower = answer.lower().strip()

        for opt in options:
            if opt.lower() == answer_lower:
                return opt

        if is_phrase_mode:
            for opt in options:
                opt_lower = opt.lower()
                if '...' in opt_lower or '…' in opt_lower or '_' in opt_lower:
                    parts = [p.strip() for p in re.split(r'\.\.\.|…|_', opt_lower) if p.strip()]
                    for part in parts:
                        if len(part) >= 2 and (answer_lower.startswith(part[:3]) or part.startswith(answer_lower[:3])):
                            return answer
                    continue

                if answer_lower in opt_lower and len(answer_lower) >= 4:
                    return opt
                if opt_lower in answer_lower:
                    return answer
        else:
            for opt in options:
                opt_lower = opt.lower()
                if answer_lower.startswith(opt_lower[:3]):
                    if (answer_lower == opt_lower + 's' or
                            answer_lower == opt_lower + 'es' or
                            answer_lower == opt_lower + 'd' or
                            answer_lower == opt_lower + 'ed' or
                            answer_lower == opt_lower + 'ing' or
                            answer_lower == opt_lower[:-1] + 'ies' or
                            answer_lower == opt_lower[:-1] + 'ied' or
                            answer_lower == opt_lower[:-1] + 'ing' or
                            answer_lower == opt_lower + opt_lower[-1] + 'ed' or
                            answer_lower == opt_lower + opt_lower[-1] + 'ing'):
                        return answer

                if opt_lower.startswith(answer_lower[:3]):
                    if (opt_lower == answer_lower + 's' or
                            opt_lower == answer_lower + 'es' or
                            opt_lower == answer_lower + 'd' or
                            opt_lower == answer_lower + 'ed' or
                            opt_lower == answer_lower + 'ing'):
                        return opt

        return None

    def _fill_fill_in(self, q: Question, answer: str) -> bool:
        answers = self._parse_banked_answer(answer, len(q.inputs))
        print(f"\t解析答案: {answers}")
        print(f"\t输入框数量: {len(q.inputs)}")

        success_count = 0
        for i, inp in enumerate(q.inputs):
            ans = answers[i] if i < len(answers) else ""
            if ans:
                print(f"\t空{i + 1}: {ans}")
                if self._fill_text_input_verified(inp, ans):
                    success_count += 1
                else:
                    print(f"\t空{i + 1}: 写入后校验失败")
            else:
                print(f"\t空{i + 1}: (空)")

        if success_count < len(q.inputs):
            print(f"\t⚠ {len(q.inputs)} 个空只填上 {success_count} 个")
        # 只有一个空填上、其余空着不算这题答好了，否则页面统计会虚报成功
        return success_count > 0 and success_count == len(q.inputs)

    def _extract_answer_by_number(self, answer: str, question_number: int) -> str:
        answer = self._normalize_answer_labels(answer)
        label_prefix = rf'(?:{self.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?'
        number_prefix = self._number_prefix_pattern(question_number)
        next_prefix = rf'(?<![$\w])(?:{self.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{self.NUMBER_PREFIX_PATTERN})'
        pattern = rf'{label_prefix}{number_prefix}\s*(.+?)(?=\s*{next_prefix}\s*|$)'
        match = re.search(pattern, answer, re.DOTALL)
        if match:
            return self._clean_extracted_answer(match.group(1))

        lines = [l.strip() for l in answer.split('\n') if l.strip()]
        for line in lines:
            clean = re.sub(
                rf'^(?:{self.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{self.NUMBER_PREFIX_PATTERN})\s*',
                '',
                line,
                flags=re.I
            ).strip()
            if clean and not re.match(r'^\d', clean):
                if re.match(rf'^(?:{self.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?{number_prefix}', line, re.I):
                    return self._clean_extracted_answer(clean)

        return ""

    @classmethod
    def _normalize_answer_labels(cls, answer: str) -> str:
        return re.sub(
            rf'(?<!^)(?<![$\w])\s+((?:{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{cls.NUMBER_PREFIX_PATTERN}))',
            r'\n\1',
            answer.strip(),
            flags=re.I
        )

    @classmethod
    def _clean_extracted_answer(cls, answer: str) -> str:
        if not answer:
            return ""
        answer = re.sub(rf'^{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*', '', answer.strip(), flags=re.I)
        answer = re.sub(rf'^(?:{cls.NUMBER_PREFIX_PATTERN})\s*', '', answer.strip(), flags=re.I)
        answer = re.sub(rf'\s*{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*$', '', answer.strip(), flags=re.I)
        # 大模型用 markdown 标答案：实测整页 7 个空全被填成 **takes a shower**，
        # 页面按原样比对 → 0/7 判错。粗体/斜体/行内代码的修饰符、行首项目符号、
        # 整体包裹的引号，一律剥掉再填。
        answer = re.sub(r"\*\*([^*]+)\*\*", r"\1", answer)
        answer = re.sub(r"\*([^*\n]+)\*", r"\1", answer)
        answer = re.sub(r"__([^_]+)__", r"\1", answer)
        answer = re.sub(r"`([^`]*)`", r"\1", answer)
        answer = re.sub(r'^[-•·]\s+', '', answer.strip())
        if len(answer) >= 2 and answer[0] in "\"'“”‘’" and answer[-1] in "\"'“”‘’":
            answer = answer[1:-1].strip()
        # AI 常把答案与页脚分隔线连在一起（实测：「occupation ------…」被填进最后一个空）：
        # 整行是分隔线的丢弃；行尾/行首残留的长分隔线掐掉。
        answer = "\n".join(
            line for line in answer.splitlines()
            if not re.fullmatch(r'[\s\-—–_=*#·.]{3,}', line.strip())
        )
        answer = re.sub(r'\s*[-—–_=*#.]{3,}\s*$', '', answer).strip()
        answer = re.sub(r'\A\s*[-—–_=*#.]{3,}\s*', '', answer).strip()
        return answer.strip()

    @staticmethod
    def _number_prefix_pattern(number: int) -> str:
        return rf'(?:(?:空\s*)?{number}\s*[.、:：\)\]]|Blank\s*{number}\s*[.、:：\)\]])'

    @classmethod
    def _looks_like_refusal(cls, text: str) -> bool:
        """判断这段文本是不是大模型的拒答说明，而不是答案。"""
        return looks_like_ai_refusal(text)

    def _fill_text(self, q: Question, answer: str) -> bool:
        if not q.inputs:
            return False

        expected_count = len(q.inputs)

        if expected_count == 1:
            ans = self._extract_answer_by_number(answer, q.number)
            if not ans:
                if q.q_type == QuestionType.MY_VOICE_TEXT:
                    ans = self._clean_direct_text_answer(answer)
                else:
                    answers = self._parse_banked_answer(answer, expected_count)
                    ans = answers[0] if answers else ""
        else:
            answers = self._parse_banked_answer(answer, expected_count)
            success_count = 0
            for idx, (inp, ans) in enumerate(zip(q.inputs, answers), 1):
                if not ans:
                    print(f"\t题{idx}: (空)")
                    continue

                print(f"\t题{idx}: {ans[:60]}...")
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                    inp
                )
                time.sleep(0.2)
                if self._fill_text_input_verified(inp, ans):
                    success_count += 1
                else:
                    print(f"\t题{idx}: 写入后校验失败")

            # 所有有答案的输入框都填成功才算成功（口径与其它填空函数一致）
            return success_count > 0 and success_count == len([a for a in answers if a])

        if ans and q.q_type == QuestionType.MY_VOICE_TEXT:
            ans = self._limit_text_answer(ans, 500)

        print(f"\t题{q.number}: {ans[:60]}..." if ans else f"\t题{q.number}: (空)")

        if ans:
            inp = q.inputs[0]
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                inp
            )
            time.sleep(0.2)
            if not self._fill_text_input_verified(inp, ans):
                return False
            if q.q_type == QuestionType.MY_VOICE_TEXT:
                if not self._upload_my_voice_answer_file(q, ans):
                    return False
            return True

        return False

    def _fill_text_input_verified(self, inp, ans: str) -> bool:
        # 统一清洗：打字、回读校验、JS 回退必须用同一个值 ——
        # 以前打字用了单写法（vicitimize），校验和 JS 回退却用整串多写法
        # （victimize; victimise），校验必然失败、JS 还会把多写法整串写回。
        ans = self._single_answer(ans)
        try:
            WebDriverHelper.simulate_typing(self.driver, inp, ans)
        except StaleElementReferenceException:
            # 页面在填的瞬间刷新了（换题/重渲染），这个输入框已失效 —— 跳过本题即可
            print("\t⚠ 输入框已失效（页面刷新），跳过本题")
            return False
        time.sleep(0.2)
        try:
            if self._input_value_matches(inp, ans):
                return True
        except StaleElementReferenceException:
            print("\t⚠ 校验时输入框已失效（页面刷新），跳过本题")
            return False

        print("\t常规输入未生效，尝试JS同步输入框状态")
        self.driver.execute_script("""
            const el = arguments[0];
            const value = arguments[1];
            const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new InputEvent('input', {
                bubbles: true,
                cancelable: true,
                inputType: 'insertText',
                data: value
            }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
        """, inp, ans)
        time.sleep(0.3)
        return self._input_value_matches(inp, ans)

    @staticmethod
    def _input_value_matches(inp, ans: str) -> bool:
        try:
            current = inp.get_attribute('value') or ''
            return current.strip() == ans.strip()
        except Exception:
            return False

    def _upload_my_voice_answer_file(self, q: Question, answer: str) -> bool:
        try:
            if self._my_voice_has_uploaded_file(q):
                print("\tMy voice 已存在上传文件，跳过自动上传")
                return True

            file_input = q.element.find_element(By.CSS_SELECTOR, '.question-multi-file-upload input[type="file"]')
            file_path = self._create_my_voice_pdf(answer)
            self.driver.execute_script("""
                arguments[0].style.display = 'block';
                arguments[0].style.visibility = 'visible';
                arguments[0].style.opacity = 1;
                arguments[0].style.width = '1px';
                arguments[0].style.height = '1px';
            """, file_input)
            file_input.send_keys(file_path)
            print(f"\tMy voice 已上传附件: {os.path.basename(file_path)}")
            return self._best_effort_wait_upload(q)
        except Exception as e:
            print(f"\tMy voice 附件上传失败: {str(e)[:80]}")
            return False

    def _my_voice_has_uploaded_file(self, q: Question) -> bool:
        try:
            media_list = q.element.find_element(By.CSS_SELECTOR, '.question-multi-file-upload .media-list')
            return bool(media_list.find_elements(By.XPATH, './*')) or bool(media_list.text.strip())
        except Exception:
            return False

    def _best_effort_wait_upload(self, q: Question, timeout: int = 12) -> bool:
        """尽力等上传就绪；超时也返回 True 继续走提交流程（方法名如实反映"尽力而为"）。"""
        start = time.time()
        while time.time() - start < timeout:
            if self._my_voice_has_uploaded_file(q):
                return True
            time.sleep(0.5)
        print("\tMy voice 上传后未检测到文件列表变化，继续尝试提交")
        return True

    def _create_my_voice_pdf(self, answer: str) -> str:
        filename = f"unipus_my_voice_{int(time.time())}.pdf"
        path = os.path.join(tempfile.gettempdir(), filename)
        lines = self._wrap_pdf_text(answer, 82)[:24]
        if not lines:
            lines = ["My voice answer"]

        text_ops = ["BT", "/F1 12 Tf", "72 760 Td", "16 TL"]
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            text_ops.append(f"({escaped}) Tj")
            text_ops.append("T*")
        text_ops.append("ET")
        stream = "\n".join(text_ops).encode("latin-1", errors="replace")

        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        ]

        content = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for idx, obj in enumerate(objects, 1):
            offsets.append(len(content))
            content.extend(f"{idx} 0 obj\n".encode("ascii"))
            content.extend(obj)
            content.extend(b"\nendobj\n")

        xref_offset = len(content)
        content.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        content.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            content.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        content.extend(
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
        )

        with open(path, "wb") as f:
            f.write(content)
        return path

    @staticmethod
    def _wrap_pdf_text(text: str, width: int) -> List[str]:
        words = re.sub(r'\s+', ' ', text).strip().split(' ')
        lines = []
        current = ""
        for word in words:
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= width:
                current += " " + word
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    @staticmethod
    def _clean_direct_text_answer(answer: str) -> str:
        answer = re.sub(r'^(My voice文字作答|简答题|答案|Answer)[：:]\s*', '', answer.strip(), flags=re.I)
        answer = re.sub(r'^\d+\s*[.、)\]]\s*', '', answer).strip()
        return answer

    @staticmethod
    def _limit_text_answer(answer: str, max_chars: int) -> str:
        answer = re.sub(r'\s+', ' ', answer).strip()
        if len(answer) <= max_chars:
            return answer

        clipped = answer[:max_chars].rstrip()
        sentence_end = max(clipped.rfind('.'), clipped.rfind('!'), clipped.rfind('?'))
        if sentence_end >= max_chars * 0.6:
            return clipped[:sentence_end + 1]
        return clipped

    def _fill_unknown(self, q: Question, answer: str) -> bool:
        return False

    @staticmethod
    def _button_text(element) -> str:
        text = element.text or element.get_attribute('aria-label') or ''
        return re.sub(r'\s+', '', text).lower()

    @classmethod
    def _is_navigation_button(cls, element) -> bool:
        text = cls._button_text(element)
        return any(k in text for k in ['上一题', '下一题', '上一页', '下一页', 'prev', 'previous', 'next', 'nextquestion'])

    @classmethod
    def _is_submit_button(cls, element) -> bool:
        text = cls._button_text(element)
        if cls._is_navigation_button(element):
            return False
        if any(k in text for k in ['提交', '确认提交', '完成', 'submit', 'done', 'finish']):
            return True
        tag_name = (element.tag_name or '').lower()
        element_type = (element.get_attribute('type') or '').lower()
        return tag_name == 'button' and element_type == 'submit'

    #: 任务做完后需要点掉的按钮文案（提交/发布/保存…）。少一个没点，这个任务就不算完成。
    FINISH_WORDS = ('提交', '发布', '保存', '完成', '确认提交', '交卷', '提交答案',
                    'Submit my work', 'Submit', 'Publish', 'Save')
    #: 提交流程最多扫描几轮（点完提交可能刷新出「发布」，要接着点）
    FINISH_ROUNDS = 3

    def finish_task_rounds(self) -> int:
        """反复扫描并点掉提交类按钮，最多 FINISH_ROUNDS 轮。

        实测的几种情况都能覆盖：直接有「提交」；点完弹「确定要提交吗？」；
        点完刷新又出现「发布」。三轮都找不到就当这个页面无需提交。
        """
        clicked = 0
        for round_index in range(1, self.FINISH_ROUNDS + 1):
            if self._submit_blocked():
                break
            if self.click_finish_button():
                clicked += 1
                self._click_dialog_button()
                time.sleep(1.5)
                continue
            if self._click_dialog_button():     # 只是弹窗还开着，也算这一轮有事做
                time.sleep(1.0)
                continue
            break
        if clicked:
            print(f"    提交动作完成（共点 {clicked} 次）")
        return clicked

    def _click_dialog_button(self) -> bool:
        """点掉确认弹窗里的确定/提交类按钮。"""
        for keyword in ('确认', '确定', '提交', '发布', 'Submit', 'OK', '我知道了'):
            for xpath in (f"//button[normalize-space(.)='{keyword}']",
                          f"//*[normalize-space(text())='{keyword}']"):
                try:
                    elements = self.driver.find_elements(By.XPATH, xpath)
                except Exception:
                    continue
                for element in elements[:2]:
                    try:
                        if element.is_displayed() and element.is_enabled():
                            element.click()
                            return True
                    except Exception:
                        continue
        return False

    #: 由外围设置：返回 True 才允许提交（页面还有空题时返回 False）
    can_submit = None

    def _submit_blocked(self) -> bool:
        guard = getattr(self, "can_submit", None)
        if callable(guard) and not guard():
            print("    ⛔ 页面上还有没答完的题，暂不提交")
            return True
        return False

    def click_finish_button(self) -> bool:
        """页面上只要有「提交/发布/保存」这类按钮就点掉，没有就静默返回 False。

        视频看完、讨论板发完、勾选表填完之后都要走这一步 —— 不然 U校园 那边任务
        还是「未完成」。这里按文案找，并且要求按钮可见可用。
        """
        candidates = []
        seen = set()
        for element in self._finish_button_candidates():
            if id(element) in seen:
                continue
            seen.add(id(element))
            try:
                if not (element.is_displayed() and element.is_enabled()):
                    continue
            except Exception:
                continue
            label = re.sub(r"\s+", "", element.text or "")
            if not label or len(label) > 10:
                continue
            if any(word in label for word in self.FINISH_WORDS):
                candidates.append((label, element))
        if not candidates:
            return False
        # 文案越短的越像主按钮（「提交」优先于「提交并查看答案」）
        candidates.sort(key=lambda item: len(item[0]))
        label, button = candidates[0]
        print(f"    ⏱ 发现「{label}」按钮，点掉它")
        clicked = False
        try:
            button.click()
            clicked = True
        except Exception:
            clicked = bool(WebDriverHelper.safe_click(self.driver, button))
        if not clicked:
            return False
        time.sleep(1.2)
        return True

    def _finish_button_candidates(self) -> List[Any]:
        elements: List[Any] = []
        for selector in ('button', '.ant-btn', '[class*="btn"]', '[role="button"]'):
            try:
                elements.extend(self.driver.find_elements(By.CSS_SELECTOR, selector))
            except Exception:
                continue
        for word in self.FINISH_WORDS:
            for xpath in (f"//button[normalize-space(.)='{word}']",
                          f"//*[normalize-space(text())='{word}']"):
                try:
                    elements.extend(self.driver.find_elements(By.XPATH, xpath)[:4])
                except Exception:
                    continue
        return elements

    def submit(self) -> bool:
        priority_selectors = [
            '.submit-bar-pc--btn-1_Xvo',
            'button[type="submit"]',
            'button.submit-btn',
            '.question-common-course-page a.btn',
            '.question-common-course-page .btn',
            'a.btn',
        ]
        for selector in priority_selectors:
            try:
                btns = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for btn in btns:
                    if btn.is_displayed() and btn.is_enabled() and self._is_submit_button(btn):
                        return WebDriverHelper.safe_click(self.driver, btn)
            except Exception:
                continue

        for selector in Selectors.SUBMIT_BUTTON:
            try:
                btns = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for btn in btns:
                    if btn.is_displayed() and btn.is_enabled() and self._is_submit_button(btn):
                        return WebDriverHelper.safe_click(self.driver, btn)
            except Exception:
                continue

        return False

    @staticmethod
    def _extract_letter(answer: str) -> Optional[str]:
        # 同上：容忍到 H，否则答案为 E 及以后的单选题一律填不上
        match = re.search(r'[A-Z]', answer.upper())
        return match.group() if match else None

    @staticmethod
    def _parse_banked_answer(answer: str, expected_count: int) -> List[str]:
        result = [''] * expected_count
        answer = AnswerExecutor._normalize_answer_labels(answer)
        answer = re.sub(rf'^{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*', '', answer.strip(), flags=re.I)
        print(f"    [调试] 清理后答案前200字: {answer[:200]}...")

        matched_any = False
        label_prefix = rf'(?:{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?'
        next_prefix = rf'(?<![$\w])(?:{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{AnswerExecutor.NUMBER_PREFIX_PATTERN})'

        for i in range(1, expected_count + 1):
            number_prefix = AnswerExecutor._number_prefix_pattern(i)
            pattern = rf'{label_prefix}{number_prefix}\s*(.*?)(?=\s*{next_prefix}\s*|$)'
            match = re.search(pattern, answer, re.DOTALL)

            if match:
                clean_ans = AnswerExecutor._clean_extracted_answer(match.group(1)).replace('\n', ' ')
                result[i - 1] = clean_ans
                matched_any = True
                print(f"    [调试] 成功提取空 {i}: '{clean_ans}'")
            else:
                print(f"    [调试] 题号 {i} 匹配失败或为空")

        if matched_any:
            return result

        # 一条题号都对不上时，绝不能把整段话塞进第 1 个空：大模型拒答时回的就是
        # 一句「无法作答…请补充原文」，那会被当成答案填进去，还把页面统计成"成功"。
        if AnswerExecutor._looks_like_refusal(answer):
            print(f"    [调试] 大模型没给出答案（疑似拒答），本页留空：{answer[:60]}...")
            return result
        if len(answer.strip()) > 80:
            print(f"    [调试] 答案文本过长且无题号，不敢按顺序切分，本页留空：{answer[:60]}...")
            return result

        print(f"    [调试] 题号匹配完全失效，启动降级切分模式")
        lines = [line.strip() for line in answer.split('\n') if line.strip()]
        content_lines = []

        for line in lines:
            clean = re.sub(
                rf'^(?:{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{AnswerExecutor.NUMBER_PREFIX_PATTERN})\s*',
                '',
                line,
                flags=re.I
            ).strip()
            if (clean and not re.match(r'^\d+$', clean)
                    and not re.fullmatch(r'[\s\-—–_=*#·.]{3,}', clean)):
                content_lines.append(AnswerExecutor._clean_extracted_answer(clean))

        for i, content in enumerate(content_lines[:expected_count]):
            result[i] = content

        return result

    def _fill_dropdown_select(self, q: Question, answer: str) -> bool:
        answers = self._parse_banked_answer(answer, len(q.banked_blanks))
        print(f"      解析答案: {answers}")
        print(f"      填空数量: {len(q.banked_blanks)}")

        success_count = 0

        for i, (blank, ans) in enumerate(zip(q.banked_blanks, answers)):
            if not ans:
                continue

            try:
                print(f"      空{i + 1}: '{ans}'")
                select_wrapper = blank['element']

                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                    select_wrapper
                )
                time.sleep(0.5)

                trigger = select_wrapper.find_element(By.CSS_SELECTOR, '.ant-dropdown-trigger')

                actions = ActionChains(self.driver)
                actions.move_to_element(trigger).click().perform()
                print(f"         点击触发器打开下拉")
                time.sleep(0.8)

                dropdown_menu = None
                for attempt in range(5):
                    try:
                        dropdown_menu = WebDriverWait(self.driver, 2).until(
                            EC.presence_of_element_located((
                                By.CSS_SELECTOR,
                                '.ant-dropdown:not(.ant-dropdown-hidden) .ant-dropdown-menu, '
                                '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item'
                            ))
                        )
                        if dropdown_menu.is_displayed():
                            break
                    except Exception:
                        pass
                    time.sleep(0.3)

                if not dropdown_menu:
                    print(f"         下拉菜单未出现，尝试备选方案")
                    if self._force_select_by_js(select_wrapper, ans):
                        success_count += 1
                    continue

                option_selectors = [
                    f'.ant-dropdown-menu-item:contains("{ans}")',
                    f'.ant-select-item-option:contains("{ans}")',
                    f'.ant-dropdown-menu-item[title="{ans}"]',
                    '//li[contains(@class,"ant-dropdown-menu-item") and contains(text(),"{}")]'.format(ans),
                    '//div[contains(@class,"ant-select-item-option-content") and contains(text(),"{}")]'.format(ans)
                ]

                option_clicked = False

                for selector in option_selectors[:3]:
                    try:
                        options = self.driver.find_elements(By.CSS_SELECTOR,
                                                            selector.replace(f':contains("{ans}")', ''))
                        for opt in options:
                            if ans.lower() in opt.text.lower() and opt.is_displayed():
                                ActionChains(self.driver).move_to_element(opt).click().perform()
                                print(f"         点击选项: {opt.text[:20]}")
                                option_clicked = True
                                break
                        if option_clicked:
                            break
                    except Exception as e:
                        continue

                if not option_clicked:
                    for xpath in option_selectors[3:]:
                        try:
                            option = self.driver.find_element(By.XPATH, xpath)
                            if option.is_displayed():
                                ActionChains(self.driver).move_to_element(option).click().perform()
                                print(f"         XPath点击选项")
                                option_clicked = True
                                break
                        except Exception:
                            continue

                if option_clicked:
                    time.sleep(0.5)

                    try:
                        answer_text_elem = select_wrapper.find_element(By.CSS_SELECTOR, '.user-answer-text')
                        displayed_text = answer_text_elem.text.strip()
                        trigger_class = trigger.get_attribute('class') or ''

                        if ans.lower() in displayed_text.lower() or 'empty' not in trigger_class:
                            print(f"         验证成功，显示文本: {displayed_text[:20]}")
                            success_count += 1
                        else:
                            print(f"         视觉反馈异常，文本: {displayed_text[:20]}")
                            self._sync_react_state(select_wrapper, ans)

                    except Exception as e:
                        print(f"         验证失败: {str(e)[:50]}")
                        success_count += 1

                else:
                    print(f"         未找到选项 '{ans}'")
                    if self._force_select_by_js(select_wrapper, ans):
                        success_count += 1

            except Exception as e:
                print(f"        处理空{i + 1}失败: {str(e)[:50]}")
                logger.error(f"详细错误: {str(e)}", exc_info=True)
                continue

        return success_count > 0

    def _fill_listening_fill_in(self, q: Question, answer: str) -> bool:
        answers = self._parse_banked_answer(answer, len(q.inputs))

        print(f"\t解析答案: {answers}")
        print(f"\t输入框数量: {len(q.inputs)}")

        success_count = 0
        for i, (blank_info, ans) in enumerate(zip(q.banked_blanks, answers)):
            if ans and blank_info['input']:
                original_ans = ans
                ans = self._normalize_listening_blank_answer(blank_info, ans)
                if ans != original_ans:
                    print(f"\t空{i + 1}: {original_ans} -> {ans}")
                else:
                    print(f"\t空{i + 1}: {ans}")
                if self._fill_text_input_verified(blank_info['input'], ans):
                    success_count += 1
                else:
                    print(f"\t空{i + 1}: 写入后校验失败")

        return success_count > 0

    def _normalize_listening_blank_answer(self, blank_info: Dict[str, Any], answer: str) -> str:
        ans = self._clean_extracted_answer(answer)
        ans = re.sub(r'\s+', ' ', ans).strip()
        left = (blank_info.get('left_context') or '').lower()
        right = (blank_info.get('right_context') or '').lower()

        if re.search(r"\b(to|for|at|in|on)\s*$", left):
            prep = re.search(r"\b(to|for|at|in|on)\s*$", left).group(1)
            ans = re.sub(rf"^{prep}\s+", "", ans, flags=re.I).strip()

        if right.startswith("at ") or right.startswith("at the "):
            ans = re.sub(r"\s+at\s+.+$", "", ans, flags=re.I).strip()
        if right.startswith("in ") or right.startswith("in the "):
            ans = re.sub(r"\s+in\s+.+$", "", ans, flags=re.I).strip()

        if len(ans.split()) > 8:
            for connector in [' at ', ' in ', ' to ', ' and ']:
                if connector in ans.lower():
                    ans = re.split(connector, ans, flags=re.I)[0].strip()
                    break

        if self._listening_left_expects_ing(left):
            ans = self._to_present_participle_phrase(ans)

        return ans

    @staticmethod
    def _listening_left_expects_ing(left_context: str) -> bool:
        left = left_context.strip().lower()
        return bool(re.search(r"\b(?:i'm|we're|you're|they're|he's|she's|is|are|am)\s*$", left))

    @staticmethod
    def _to_present_participle_phrase(answer: str) -> str:
        replacements = {
            'go': 'going',
            'have': 'having',
            'get': 'getting',
            'meet': 'meeting',
            'visit': 'visiting',
            'watch': 'watching',
            'see': 'seeing',
            'start': 'starting',
        }
        match = re.match(r"^([A-Za-z]+)\b(.*)$", answer.strip())
        if not match:
            return answer

        verb = match.group(1)
        rest = match.group(2)
        lower = verb.lower()
        if lower.endswith('ing'):
            return answer
        if lower in replacements:
            replacement = replacements[lower]
            if verb[:1].isupper():
                replacement = replacement[:1].upper() + replacement[1:]
            return replacement + rest
        return answer

    def _force_select_by_js(self, select_wrapper, value: str) -> bool:
        try:
            js = """
            var wrapper = arguments[0];
            var value = arguments[1];

            var trigger = wrapper.querySelector('.ant-dropdown-trigger');
            var events = ['mousedown', 'focus', 'click', 'input', 'change', 'blur'];

            events.forEach(function(eventType) {
                var event = new Event(eventType, { bubbles: true, cancelable: true });
                trigger.dispatchEvent(event);
            });

            var textElem = wrapper.querySelector('.user-answer-text');
            if (textElem) {
                textElem.innerHTML = '<p>' + value + '</p>';
                textElem.textContent = value;
            }

            trigger.classList.remove('empty');
            trigger.classList.add('selected');

            var reactKey = Object.keys(trigger).find(k => k.startsWith('__react'));
            if (reactKey) {
                var fiber = trigger[reactKey];
                while (fiber) {
                   if (fiber.memoizedProps && fiber.memoizedProps.onChange) {
                    fiber.memoizedProps.onChange(value);
                    return 'react_onChange_triggered';
                }
                fiber = fiber.return || fiber._debugOwner;
            }
        }

        var formEvent = new Event('submit', { bubbles: true });
        var form = trigger.closest('form');
        if (form) form.dispatchEvent(formEvent);

        return 'dom_updated';
        """

            result = self.driver.execute_script(js, select_wrapper, value)
            print(f"        JS强制设置结果: {result}")

            time.sleep(0.3)
            text_elem = select_wrapper.find_element(By.CSS_SELECTOR, '.user-answer-text')
            return value.lower() in text_elem.text.lower()

        except Exception as e:
            print(f"        JS强制设置失败: {str(e)[:50]}")
            return False

    def _sync_react_state(self, select_wrapper, value: str) -> bool:
        try:
            js = """
            var wrapper = arguments[0];
            var value = arguments[1];

            wrapper.setAttribute('data-selected-value', value);

            var hiddenInput = wrapper.querySelector('input[type="hidden"]');
            if (hiddenInput) {
                hiddenInput.value = value;
                hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));
            }

            if (!window.__formData) window.__formData = {};
            var scoopIndex = wrapper.closest('[data-scoop-index]')?.getAttribute('data-scoop-index');
            if (scoopIndex) {
                window.__formData[scoopIndex] = value;
            }

            return true;
            """
            return self.driver.execute_script(js, select_wrapper, value)
        except Exception:
            return False


class ContentHandler(ABC):
    """内容处理器基类"""

    @abstractmethod
    def can_handle(self, question: Question) -> bool:
        pass

    @abstractmethod
    def handle(self, question: Question) -> bool:
        pass


class DiscussionBoardHandler(ContentHandler):
    """讨论板处理器：读题目 → 让大模型写一段 → 填进输入框 → 点发表。"""

    #: 讨论板的输入框（不同版本 U校园 用 textarea 或 contenteditable）。
    #: 实测页面（读写教程3 · Discussion）用的是 placeholder「我来评论」的 textarea，
    #: 所以 placeholder 那几条排在最前面；都认不出时再退回「页面上最大的可见文本框」。
    INPUT_SELECTORS = [
        'textarea[placeholder*="我来评论"]',
        'textarea[placeholder*="评论"]',
        'textarea[placeholder*="回复"]',
        'textarea[placeholder*="说点"]',
        'textarea[placeholder*="写"]',
        '.ds-discussion-bottom-textArea-container textarea',
        '.discussion-course-page-sdk textarea',
        '.discussion-course-page-sdk [contenteditable="true"]',
        '.ds-discussion-reply textarea',
        'textarea.ant-input',
        '.ant-input[contenteditable="true"]',
        'textarea',
    ]
    #: 发表按钮的文案。只留这四个：『回复』『评论』在讨论板上同时是帖子下面的
    #: 「回复（0）」「评论（0）」链接，混进来会把回复框点开，正文反而发不出去。
    SUBMIT_TEXTS = ('发表', '发布', '提交', '发送')
    #: 二次确认弹窗的按钮。这里不写『发表/提交』—— 那正是刚点过的发表按钮，
    #: 写进去会在弹窗没出现时把同一个按钮再点一次，变成发两条。
    CONFIRM_TEXTS = ('确认', '确定', '我知道了', '继续', 'OK')
    #: 等二次确认弹窗的时间
    CONFIRM_WAIT = 4.0
    #: 点完发表后，等「真的发出去了」的时间
    POST_CONFIRM_WAIT = 5.0

    #: 把整段文字写进输入框，并触发 React 的受控事件
    WRITE_JS = """
    var el = arguments[0], text = arguments[1];
    if (!el) { return false; }
    try { el.focus(); } catch (e) {}
    if (el.isContentEditable) {
        el.innerHTML = '';
        var p = document.createElement('p');
        p.textContent = text;
        el.appendChild(p);
    } else {
        var proto = el.tagName === 'TEXTAREA'
            ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        var setter = Object.getOwnPropertyDescriptor(proto, 'value');
        if (setter && setter.set) { setter.set.call(el, text); } else { el.value = text; }
    }
    ['input', 'change', 'blur'].forEach(function (type) {
        el.dispatchEvent(new Event(type, { bubbles: true, cancelable: true }));
    });
    return true;
    """

    def __init__(self, driver, ai_client=None, stop_requested=None):
        self.driver = driver
        self.ai_client = ai_client
        self.stop_requested = stop_requested

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.DISCUSSION_BOARD

    def handle(self, question: Question) -> bool:
        if self.ai_client is None or self.stop_requested is None:
            print("     讨论板页面，但没有接通大模型，跳过")
            return False
        if self.stop_requested.is_set():
            return False

        topic = re.sub(r"\s+", " ", (question.text or "")).strip()
        if len(topic) < 4:
            print("     讨论板没读到题目，跳过")
            return False

        prompt = self._build_prompt(topic)
        answer = self.ai_client.ask(prompt, stop_requested=self.stop_requested)
        if self.stop_requested.is_set():
            return False
        if not answer or looks_like_ai_refusal(answer):
            # 讨论板也常遇到「题目里没说清楚要谈什么」——把页面正文补给它再问一次，
            # 别把讨论板空着交回去。
            page_text = self._page_text()
            if page_text:
                print("     ⚠ 讨论板信息不全，抓取页面原文后重问一次")
                answer = self.ai_client.ask(
                    f"{prompt}\n\n【补充材料】下面是从页面抓到的全部文字：\n{page_text}\n\n"
                    "【必须遵守】不要再要求我提供题目，不要再回答「无法确定」「无法作答」，"
                    "直接按上面的要求写一段可以发表的英文发言。",
                    stop_requested=self.stop_requested,
                )
            if self.stop_requested.is_set():
                return False
        if not answer or looks_like_ai_refusal(answer):
            print(f"     ⚠ 讨论板没拿到可用内容：{str(answer)[:60]}")
            return False

        text = self._clean_post(answer)
        if len(text) < 20:
            print(f"     ⚠ 讨论板内容太短，放弃：{text[:40]}")
            return False
        if re.search(r"[\u4e00-\u9fff]", text) and re.search(r"[A-Za-z]{3,}", topic):
            print("     ⚠ 讨论板内容仍是中文，放弃（题目是英文，应英文作答）")
            return False

        editor = self._find_editor_with_fallback()
        if editor is None:
            print("     ⚠ 讨论板没找到输入框，跳过")
            return False

        print(f"     讨论板作答：{text[:50]}...")
        if not self._write(editor, text):
            print("     ⚠ 讨论板内容写入失败，跳过")
            return False
        if self.stop_requested.wait(1.2):
            return False

        # 「发布」在框空着时是禁用态。JS 写值有时不被页面的受控组件认下，
        # 这时按钮一直是灰的 —— 换成真实键盘输入再试一次。
        if not self._has_usable_submit(editor):
            print("     发表键仍是禁用态，改用真实键盘输入重写一遍")
            if self._write_by_typing(editor, text):
                if self.stop_requested.wait(1.2):
                    return False

        if not self._click_submit(editor, text):
            print("     ⚠ 讨论板内容已填但没能提交（原因见上一行）")
            return False
        if self.stop_requested.wait(1.0):
            return False
        self._click_confirm()
        print("     ✅ 讨论板已发表")
        return True

    @staticmethod
    def _build_prompt(topic: str) -> str:
        return (
            "【任务】这是 U校园 的讨论板（Discussion）。请针对下面的讨论题目写一段可直接发表的英文发言。\n"
            "【讨论题目】\n"
            f"{topic}\n"
            "【要求】\n"
            "1. 只用英文，120 词左右，3-5 句，语气自然，像学生本人的观点。\n"
            "2. 直接给出正文，不要题目、不要编号、不要 Markdown 标记、不要引号、不要任何解释或前后缀。\n"
            "3. 可以结合题目里提到的课文内容，观点明确，给出一点理由或例子。\n"
        )

    @staticmethod
    def _clean_post(answer: str) -> str:
        text = str(answer).strip()
        text = re.sub(r"^(?:讨论板|发言|回答|Answer|Post)\s*[:：]\s*", "", text, flags=re.I)
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        text = re.sub(r"^\d+\s*[.、)]\s*", "", text)
        text = text.replace("\r", "").strip().strip('"“”').strip()
        return re.sub(r"[ \t]+", " ", text)

    def _page_text(self, limit: int = 4000) -> str:
        """整页可见文字，用于把大模型要的「题目/文章」补给它。"""
        return page_visible_text(self.driver, limit)

    def _find_editor(self):
        for selector in self.INPUT_SELECTORS:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                try:
                    if element.is_displayed() and element.is_enabled():
                        return element
                except Exception:
                    continue
        return None

    def _find_editor_with_fallback(self):
        """先按类名/placeholder 找；都落空时用「页面上最大的可见文本框」。

        讨论板页面只有评论框是可输入的 textarea，按面积挑最大的一定是它。
        """
        editor = self._find_editor()
        if editor is not None:
            return editor
        try:
            candidates = self.driver.find_elements(By.CSS_SELECTOR, 'textarea')
        except Exception:
            return None
        best, best_area = None, 0
        for element in candidates:
            try:
                if not (element.is_displayed() and element.is_enabled()):
                    continue
                if str(element.get_attribute('readonly') or '').lower() == 'true':
                    continue
                size = element.size or {}
                area = int(size.get('width', 0)) * int(size.get('height', 0))
            except Exception:
                continue
            if area > best_area:
                best, best_area = element, area
        if best is not None:
            print("     讨论板输入框按「页面最大文本框」定位到")
        return best

    def _has_usable_submit(self, editor) -> bool:
        """发表键现在是不是可点的（内容没被页面认下时它一直是灰的）。"""
        for element in self._submit_targets(editor):
            try:
                if element.is_displayed() and element.is_enabled():
                    return True
            except Exception:
                continue
        return False

    def _write(self, editor, text: str) -> bool:
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", editor)
        except Exception:
            pass
        try:
            if self.driver.execute_script(self.WRITE_JS, editor, text):
                if self._read_back(editor, text):
                    return True
        except Exception:
            pass
        try:  # JS 写不进去就退回键盘输入
            editor.click()
            editor.clear()
            editor.send_keys(text)
            return bool(self._read_back(editor, text))
        except Exception:
            return False

    def _write_by_typing(self, editor, text: str) -> bool:
        """用真实键盘输入重写。

        实测的评论框是受控组件：直接把 value 塞进去，框里能看到字，
        「发布」却一直是禁用态（页面的 state 没更新）。真敲键才会更新 state。
        """
        try:
            editor.click()
        except Exception:
            pass
        try:
            editor.clear()
        except Exception:
            pass
        try:
            editor.send_keys(text)
        except Exception as exc:
            print(f"     键盘输入失败：{str(exc)[:60]}")
            return False
        return bool(self._read_back(editor, text))

    @staticmethod
    def _read_back(editor, text: str) -> bool:
        try:
            current = (editor.get_attribute('value')
                       or editor.get_attribute('innerText')
                       or editor.text or "")
        except Exception:
            return False
        current = re.sub(r"\s+", " ", str(current)).strip()
        return len(current) >= min(20, len(text) // 3)

    def _click_submit(self, editor, text: str) -> bool:
        """点发表。按文案找不到就找输入框附近的按钮，点完要验证内容真的出现在页面上。"""
        targets = self._submit_targets(editor)
        if not targets:
            print("     ⚠ 讨论板页面上没找到任何可用按钮："
                  f"{self._describe_buttons()}")
            return False
        print(f"     讨论板按钮候选 {len(targets)} 个：{self._describe_buttons()[:200]}")

        for index, target in enumerate(targets[:3], 1):
            if self.stop_requested.is_set():
                return False
            if not self._click_element(target):
                continue
            print(f"     已点第 {index} 个按钮，等页面出现刚写的内容…")
            if self._post_confirmed(text):
                return True
            print(f"     第 {index} 个按钮点完，页面上没出现刚写的内容，换下一个")
            self._confirm_once()

        # 很多评论框是 Ctrl+Enter 发表
        if self._submit_by_hotkey(editor):
            print("     试过 Ctrl+Enter，等页面出现刚写的内容…")
            if self._post_confirmed(text):
                return True
        return False

    def _submit_targets(self, editor) -> List[Any]:
        """按优先级给出可点的按钮。

        实测教训：帖子下面的「回复（0）」「点赞（0）」链接也在可点元素里，还比「发布」
        更靠近输入框，先点它们只会把回复框打开、正文发不出去。所以这里分两档：
        ① 文案**完全等于**发布/发表/提交/发送 的（真正的发表键）；
        ② 文案里含这几个词、且不带数字括号的（如「发表评论」）；
        第一档有货就绝不用第二档。都没有才退回「输入框附近的按钮」。
        """
        exact: List[Any] = []
        partial: List[Any] = []
        seen = set()
        for element in self._text_buttons() + self._buttons():
            if id(element) in seen:
                continue
            seen.add(id(element))
            try:
                if not (element.is_displayed() and element.is_enabled()):
                    continue
            except Exception:
                continue
            label = re.sub(r"\s+", "", element.text or "")
            if not label or len(label) > 8:
                continue
            if label in self.SUBMIT_TEXTS:
                exact.append(element)
            elif any(word in label for word in self.SUBMIT_TEXTS) \
                    and not re.search(r"[（()\d]", label):
                partial.append(element)

        for tier in (exact, partial):
            if tier:
                return self._sorted_by_distance(tier, editor)

        # 文案认不出来（图标按钮 / 自定义组件）时，退回「输入框附近的按钮」
        try:
            nearby = self.driver.execute_script(self.NEARBY_BUTTONS_JS, editor) or []
        except Exception:
            nearby = []
        return list(reversed(list(nearby)))

    def _text_buttons(self) -> List[Any]:
        """按文字找按钮。

        实测的「发布」是个文字按钮：既不是 <button>，类名里也没有 btn，
        只能靠 XPath 按文字找。找到的文字节点本身可能要往上点一层才能真正触发，
        这里连它自己带上去交给 _click_element 逐层试。
        """
        found: List[Any] = []
        for word in self.SUBMIT_TEXTS:
            xpaths = (
                f"//button[normalize-space(.)='{word}']",
                f"//*[normalize-space(text())='{word}']",
            )
            for xpath in xpaths:
                try:
                    elements = self.driver.find_elements(By.XPATH, xpath)
                except Exception:
                    continue
                found.extend(elements[:4])
        return found

    #: 从输入框往上找 6 层，取出可视、可用的按钮
    NEARBY_BUTTONS_JS = """
    var el = arguments[0];
    if (!el) { return []; }
    var node = el;
    for (var depth = 0; depth < 6 && node; depth++) {
        node = node.parentElement;
        if (!node) { break; }
        var found = [];
        var list = node.querySelectorAll('button, [class*="btn"], [role="button"], a');
        for (var i = 0; i < list.length; i++) {
            var b = list[i];
            if (b.offsetParent === null) { continue; }
            if (b.disabled) { continue; }
            if (list.length > 24) { break; }
            found.push(b);
        }
        if (found.length) { return found.slice(0, 8); }
    }
    return [];
    """

    def _click_element(self, element) -> bool:
        """点一个元素：自己 → 它的父层（文字按钮常常只有父层带点击）→ JS 点击。"""
        for target in (element, self._parent_of(element)):
            if target is None:
                continue
            try:
                target.click()
                return True
            except Exception:
                pass
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();", element)
            return True
        except Exception:
            pass
        return bool(WebDriverHelper.safe_click(self.driver, element))

    def _parent_of(self, element):
        try:
            return self.driver.execute_script("return arguments[0].parentElement;", element)
        except Exception:
            return None

    def _submit_by_hotkey(self, editor) -> bool:
        try:
            editor.click()
            editor.send_keys(Keys.CONTROL, Keys.ENTER)
            return True
        except Exception:
            return False

    def _post_confirmed(self, text: str) -> bool:
        """确认真的发出去了：**页面上出现刚写的那段内容**才算数。

        绝不能拿「输入框被清空」当证据 —— 切换回复框、页面重渲染都会清空输入框，
        内容却根本没提交（实测踩过：日志报已发表，页面上一条都没有）。
        输入框里的值不属于 innerText，所以比对页面正文不会把自己框里那行算进去。
        """
        fragment = self._fingerprint(text)
        if not fragment:
            return False
        deadline = time.time() + self.POST_CONFIRM_WAIT
        while time.time() < deadline:
            if self.stop_requested.is_set():
                return False
            if fragment in self._fingerprint(self._page_text(limit=20000)):
                return True
            # 成功判定只认内容指纹：页面上残留的「成功」字样（上一题的提示等）不能当证据
            self._confirm_once()
            if self.stop_requested.wait(0.5):
                return False
        return False

    @staticmethod
    def _fingerprint(text: str) -> str:
        """取一段指纹（去掉空白与标点），用于在页面正文里找回刚发表的内容。"""
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(text or ""))[:40]

    def _confirm_once(self) -> bool:
        """扫一遍二次确认弹窗，有就点掉（不等待）。"""
        for element in self._buttons():
            try:
                if not (element.is_displayed() and element.is_enabled()):
                    continue
            except Exception:
                continue
            label = re.sub(r"\s+", "", element.text or "")
            if label and len(label) <= 8 and any(
                    word == label or word in label for word in self.CONFIRM_TEXTS):
                try:
                    element.click()
                    return True
                except Exception:
                    continue
        return False

    def _describe_buttons(self) -> str:
        """把页面上能找到的按钮连文案、可见、可用状态一起打出来，方便对着日志排查。"""
        described = []
        seen = set()
        for element in self._text_buttons() + self._buttons():
            if id(element) in seen:
                continue
            seen.add(id(element))
            try:
                state = "可见" if element.is_displayed() else "隐藏"
                enabled = "可用" if element.is_enabled() else "禁用"
                label = re.sub(r"\s+", "", element.text or "")[:12]
            except Exception:
                continue
            described.append(f"[{label or '无文字'}|{state}|{enabled}]")
            if len(described) >= 20:
                break
        return " ".join(described) or "（页面上没有任何按钮类元素）"

    def _click_confirm(self):
        deadline = time.time() + self.CONFIRM_WAIT
        while time.time() < deadline:
            if self.stop_requested.is_set():
                return
            for element in self._buttons():
                try:
                    if not element.is_displayed():
                        continue
                except Exception:
                    continue
                label = re.sub(r"\s+", "", element.text or "")
                if label and any(word == label or word in label for word in self.CONFIRM_TEXTS) \
                        and len(label) <= 8:
                    try:
                        element.click()
                        self.stop_requested.wait(0.8)
                        return
                    except Exception:
                        continue
            if self.stop_requested.wait(0.5):
                return

    def _buttons(self):
        elements = []
        for selector in ('button', '.ant-btn', '[class*="btn"]', '[role="button"]'):
            try:
                elements.extend(self.driver.find_elements(By.CSS_SELECTOR, selector))
            except Exception:
                continue
        return elements

    def _sorted_by_distance(self, candidates, editor):
        try:
            origin = editor.location
        except Exception:
            return list(candidates)
        def distance(element):
            try:
                loc = element.location
                return abs(loc.get('y', 0) - origin.get('y', 0)) + abs(loc.get('x', 0) - origin.get('x', 0))
            except Exception:
                return 10 ** 6
        return sorted(candidates, key=distance)


class FollowReadHandler(ContentHandler):
    """跟读/录音题：先听示范，再按句子长度把整句读完，然后停止。

    录音时长由内容决定（不是固定秒数）：
        预计秒数 = 单词数 ÷ 语速 × 1.15 余量，限制在 [3, 30] 秒。
    即「这句话读完」就停，短句短录、长句长录。
    """

    MIC_SELECTORS = (
        '[class*="record"]', '[class*="Record"]',
        '[class*="mic"]', '[class*="Mic"]',
        '[class*="voice"]', '[class*="speak"]',
        '[class*="follow"]', '[class*="recite"]',
    )
    SPEAKER_SELECTORS = (
        '[class*="audio"]', '[class*="play"]', '[class*="Play"]',
        '[class*="speaker"]', '[class*="sound"]', '[class*="listen"]',
    )
    #: 朗读语速（词/秒）与会给录音留的余量
    WORDS_PER_SECOND = 2.2
    DURATION_MARGIN = 1.15
    MIN_SECONDS = 3
    MAX_SECONDS = 30

    def __init__(self, driver, stop_requested=None):
        self.driver = driver
        self.stop_requested = stop_requested

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.FOLLOW_READ

    # -- 时长估算 ---------------------------------------------------------

    @classmethod
    def estimate_seconds(cls, sentence: str) -> float:
        """按句子长度估算「读完这句」需要几秒。"""
        words = [w for w in re.findall(r"[A-Za-z']+", str(sentence or "")) if w]
        if not words:
            return float(cls.MIN_SECONDS)
        seconds = len(words) / cls.WORDS_PER_SECOND * cls.DURATION_MARGIN
        return max(cls.MIN_SECONDS, min(cls.MAX_SECONDS, seconds))

    def _row_sentence(self, button) -> str:
        """取这条录音对应的句子（往上找最近的题目文字）。"""
        try:
            row = button.find_element(By.XPATH, './ancestor::*[self::div or self::li][3]')
        except Exception:
            row = None
        for candidate in (row, getattr(button, "parent", None)):
            if candidate is None:
                continue
            try:
                text = re.sub(r"\s+", " ", candidate.text or "").strip()
            except Exception:
                continue
            if len(text) > 15:
                return text[:200]
        return ""

    # -- 主流程 -----------------------------------------------------------

    def handle(self, question: Question) -> bool:
        # 跟读题：优先用「一次性」模式 —— 整页交给浏览器里一段脚本跑完（read_aloud.py），
        # 用示范音的 ended 事件决定停止时机，不再按词数估算时长、不再逐条查元素，
        # 也就不会反复点第一条录音。失败才退回下面的逐条模式。
        try:
            from read_aloud import run_all_items
            done_items = run_all_items(self.driver)
            if done_items:
                failed = [n for n, ok, _s in done_items if not ok]
                if not failed:
                    print(f"     跟读题：{len(done_items)} 条全部录完")
                    return True
                # 只录成一部分绝不能算完成：以前 here 用的是 any(ok)，
                # 4 条录成 1 条就返回成功，剩下的不做、任务却被标成已完成。
                print(f"     跟读题：{len(failed)} 条没录成（{failed}），退回逐条模式补做")
        except Exception as exc:
            print(f"     跟读题：一次性模式不可用（{str(exc)[:50]}），退回逐条模式")

        container = getattr(question, "element", None)
        if container is None:
            return False
        print("     跟读题：开始处理")
        buttons = []
        for selector in self.MIC_SELECTORS:
            try:
                buttons.extend(container.find_elements(By.CSS_SELECTOR, selector))
            except Exception:
                continue
        unique, seen = [], set()
        for button in buttons:
            try:
                if id(button) in seen or not button.is_displayed():
                    continue
            except Exception:
                continue
            seen.add(id(button))
            unique.append(button)
        print(f"     跟读题：找到 {len(unique)} 个麦克风按钮")
        if not unique:
            print("     ⚠ 没找到麦克风按钮，跳过本题")
            return False

        done_count = 0
        for index, button in enumerate(unique, 1):
            if self.stop_requested is not None and self.stop_requested.is_set():
                return False
            sentence = self._row_sentence(button)
            seconds = self.estimate_seconds(sentence)
            print(f"     第 {index} 条：{len(re.findall(r'[A-Za-z]+', sentence))} 词 → "
                  f"录 {seconds:.1f} 秒（读完为止）")

            # ① 先开录音（这样下面的示范音频才会进入录音流）
            try:
                self.driver.execute_script("window.__ucAudioRouting = true;")
            except Exception:
                pass
            try:
                WebDriverHelper.safe_click(self.driver, button)
                print(f"     第 {index} 条：开始录音")
            except Exception as exc:
                print(f"     第 {index} 条：点击麦克风失败 {str(exc)[:50]}")
                continue

            # ② 立刻播放页面自带的示范读音 —— 注入的补丁会把它送进录音流，
            #    平台"听到"的就是标准发音本身（而不是环境音）。
            played = False
            try:
                played = bool(self.driver.execute_script("""
                    var el = document.querySelector('audio, video');
                    if (!el) { return false; }
                    try { el.currentTime = 0; } catch (e) {}
                    el.muted = false;
                    el.volume = 1.0;
                    el.play();
                    return true;
                """))
            except Exception:
                played = False
            print("     已播放示范读音（送入录音流）" if played
                  else "     ⚠ 没找到示范音频，本次录的是环境音")

            # ③ 录到「这句话读完」再停
            waited = 0.0
            while waited < seconds:
                if self.stop_requested is not None and self.stop_requested.is_set():
                    return False
                time.sleep(0.5)
                waited += 0.5
            try:
                WebDriverHelper.safe_click(self.driver, button)
                print(f"     第 {index} 条：已停止录音（录了 {waited:.1f} 秒）")
            except Exception:
                print(f"     第 {index} 条：停止按钮没点到（可能自动结束）")
            try:
                self.driver.execute_script("window.__ucAudioRouting = false;")
            except Exception:
                pass
            done_count += 1
            time.sleep(2)

        print(f"     跟读题：完成 {done_count}/{len(unique)} 条")
        return done_count > 0


class SelfCheckHandler(ContentHandler):
    """Self-check 词汇勾选处理器"""

    def __init__(self, driver, stop_requested: threading.Event):
        self.driver = driver
        self.stop_requested = stop_requested

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.SELF_CHECK

    def handle(self, question: Question) -> bool:
        print("     处理 Self-check 词汇勾选表...")
        clicked = 0

        row_count = len(question.element.find_elements(By.CSS_SELECTOR, 'tbody tr.ant-table-row:not(.category-name)'))
        for row_index in range(row_count):
            if self.stop_requested.is_set():
                return False
            try:
                rows = question.element.find_elements(By.CSS_SELECTOR, 'tbody tr.ant-table-row:not(.category-name)')
                if row_index >= len(rows):
                    break
                row = rows[row_index]

                word = ""
                try:
                    word = row.find_element(By.CSS_SELECTOR, '.content-text').text.strip()
                except Exception:
                    pass

                got_it_cell = row.find_element(By.CSS_SELECTOR, 'td:nth-child(2)')
                icon = got_it_cell.find_element(By.CSS_SELECTOR, '.anticon')
                icon_class = icon.get_attribute('class') or ''
                aria_label = (icon.get_attribute('aria-label') or '').lower()

                if 'anticon-border' not in icon_class and aria_label != 'border':
                    continue

                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", icon)
                if self.stop_requested.wait(0.1):
                    return False

                if WebDriverHelper.safe_click(self.driver, icon):
                    clicked += 1
                    print(f"      勾选: {word or clicked}")
                else:
                    self.driver.execute_script("""
                        arguments[0].click();
                        arguments[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
                    """, icon)
                    clicked += 1
                    print(f"      JS勾选: {word or clicked}")

                if self.stop_requested.wait(0.1):
                    return False
            except Exception as e:
                print(f"      勾选失败: {str(e)[:50]}")
                continue

        print(f"     Self-check 完成，共勾选 {clicked} 项")
        return True


class VideoHandler(ContentHandler):
    """视频处理器"""

    def __init__(self, driver, config: Config, stop_requested: threading.Event):
        self.driver = driver
        self.config = config
        self.stop_requested = stop_requested
        self.popup_monitor_thread = None
        self.stop_monitoring = threading.Event()

        # Whisper 模型按 config.json 的 whisper_model 选择（base 快、small 更准；
        # 实测 base 会把 French horns 听成 French homes 导致视听题答错）
        AudioTranscriber.MODEL_NAME = (
            str(getattr(config, "whisper_model", "") or "base").strip() or "base")
        self.transcriber = AudioTranscriber()

        self.analyzer_client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url, timeout=90, max_retries=1)
        # 之前这里把模型名写死成 kimi-k2-turbo-preview，配置里用的是别的模型时
        # 请求直接失败，然后静默退化成「关键词数数」——弹窗题答错就是这么来的。
        self.analyzer_model = getattr(config, "model", "") or "kimi-k2-turbo-preview"

        self.video_transcript = ""
        self.current_video_url = ""

    def _play_video_and_handle_popups(self):
        """播放视频并自动处理弹窗选择题（供外部预处理调用）"""
        if self.stop_requested.is_set():
            return
        video_info = self._get_video_info()
        if not video_info:
            print("      未找到视频元素")
            return

        video_url = video_info.get('url', '')
        duration = video_info.get('duration', 0)

        if video_url and video_url == self.current_video_url and self.video_transcript:
            print(f"      使用已缓存的视频转录（{len(self.video_transcript)}字符）")
        else:
            self.current_video_url = video_url
            self.video_transcript = self._transcribe_video(video_url, duration)
        if self.stop_requested.is_set():
            return
        self.stop_monitoring.clear()
        self.popup_monitor_thread = threading.Thread(
            target=self._monitor_popup_questions,
            daemon=True
        )
        self.popup_monitor_thread.start()

        try:
            self._play_video(duration)
            if not self.stop_requested.is_set():
                print("      视频播放完成")
        finally:
            self.stop_monitoring.set()
            if self.popup_monitor_thread.is_alive():
                self.popup_monitor_thread.join(timeout=1)

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.VIDEO

    def handle(self, question: Question) -> bool:
        if self._check_video_completed():
            print("     视频已标记为完成，跳过")
            return True

        print("     视频页面，开始处理...")
        self._play_video_and_handle_popups()
        print("     视频处理完成")
        return True

    def _get_video_info(self) -> Optional[Dict]:
        try:
            video = self.driver.find_element(By.TAG_NAME, 'video')
            url = video.get_attribute('src') or ''

            if not url:
                sources = video.find_elements(By.TAG_NAME, 'source')
                for source in sources:
                    url = source.get_attribute('src')
                    if url:
                        break

            duration = self.driver.execute_script("return arguments[0].duration;", video)

            return {
                'url': url,
                'duration': duration or 0,
                'element': video
            }
        except Exception:
            return None

    def _transcribe_video(self, video_url: str, duration: float) -> str:
        if not video_url:
            return ""

        print(f"     开始识别视频音频（时长: {int(duration)}秒）...")

        try:
            transcript = self.transcriber.transcribe(
                video_url, language="en", initial_prompt=page_transcription_hint(self.driver))

            if transcript:
                preview = transcript[:200] + "..." if len(transcript) > 200 else transcript
                print(f"     识别成功: {preview}")
                return transcript
            else:
                print("     未能识别音频内容")
                return ""

        except Exception as e:
            print(f"     音频识别失败: {str(e)[:50]}")
            return ""

    def _play_video(self, duration: float):
        video = None
        try:
            video = self.driver.find_element(By.TAG_NAME, 'video')

            if duration > 0:
                print(f"      ▶ 播放视频（{int(duration)}秒，2倍速）...")
                self.driver.execute_script("""
                    arguments[0].playbackRate = 2.0;
                    arguments[0].muted = true;
                    arguments[0].play();
                """, video)

                if not self._wait_for_video_complete(video, duration):
                    print("       ⚠ 视频等待被异常中断（进度读不到），本次视频可能未播完")
            else:
                self.driver.execute_script("""
                    arguments[0].playbackRate = 2.0;
                    arguments[0].muted = true;
                    arguments[0].play();
                """, video)
                print(f"      ⏳ 等待 10 秒...")
                self.stop_requested.wait(10)

        except Exception as e:
            print(f"       视频播放失败: {str(e)[:50]}")

        finally:
            if self.stop_requested.is_set() and video is not None:
                try:
                    self.driver.execute_script("arguments[0].pause();", video)
                except Exception:
                    pass

    def _monitor_popup_questions(self):
        print("      [监视器] 开始监视弹窗...")
        check_interval = 0.5
        processed_popups = set()

        while not self.stop_monitoring.is_set() and not self.stop_requested.is_set():
            try:
                popup = self._find_popup_question()

                if popup and popup.is_displayed():
                    popup_id = self._get_popup_id(popup)
                    if popup_id is None:
                        # 拿不到指纹就没法去重：本轮不答，避免重复作答同一弹窗
                        if self.stop_requested.wait(0.5):
                            break
                        continue

                    if popup_id in processed_popups:
                        if self.stop_requested.wait(0.5):
                            break
                        continue

                    print("      [监视器]  检测到新弹窗题目！")
                    question_data = self._parse_popup_question(popup)

                    if not question_data:
                        print("      [监视器]  未能解析题目")
                        continue

                    if question_data['options']:
                        answer = self._intelligent_select_answer(question_data)
                        if not answer:
                            print("      [监视器]  ⚠ 这题没拿到答案，标记为未完成（不瞎选）")
                            continue
                    else:
                        print("      [监视器]  ⚠ 弹窗里没解析出选项，跳过本题")
                        continue

                    if self.stop_requested.is_set() or self.stop_monitoring.is_set():
                        break
                    success = self._click_option(popup, answer)

                    if success:
                        print(f"      [监视器]  已选择: {answer}")
                        processed_popups.add(popup_id)
                        if self.stop_requested.wait(0.5) or self.stop_monitoring.is_set():
                            break
                        self._click_submit_if_exists(popup)
                        self.stop_requested.wait(1.0)
                    else:
                        print(f"      [监视器]  点击失败: {answer}")

            except Exception as e:
                pass

            self.stop_monitoring.wait(check_interval)

        print("      [监视器] 已停止")

    def _find_popup_question(self) -> Optional[Any]:
        # 各版本 U校园 的弹窗容器类名不一样，把常见的都列上；
        # 「视频弹出来的选项没填」多半就是选择器没覆盖到。
        selectors = [
            '.video-box .popupBox .questionReplyBox',
            '.popupBox .question-common-abs-choice',
            '.questionReplyBox .question-common-abs-choice',
            '.video-popup .question-common-abs-choice',
            '.popupBox:has(.option)',
            '.questionReplyBox:has(.option)',
            '[class*="popup"] [class*="option"]',
            '[class*="Popup"] [class*="option"]',
            '.video-question-popup',
            '[class*="video"][class*="question"]',
        ]

        for selector in selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for elem in elements:
                    if elem.is_displayed():
                        options = elem.find_elements(By.CSS_SELECTOR, '.option, .option-wrap .option')
                        if len(options) >= 2:
                            return elem
            except Exception:
                continue
        return None

    def _get_popup_id(self, popup) -> Optional[str]:
        """弹窗指纹：读不到文本就返回 None。

        以前读不到时回退 str(time.time()) —— 每次值都不同 → 去重集合失效，
        同一个弹窗每 0.5 秒被当新题反复作答/提交。宁可本轮跳过、下轮再试。
        """
        try:
            text = popup.text
        except Exception:
            return None
        if not (text or "").strip():
            return None
        return hashlib.md5(text[:200].encode()).hexdigest()[:16]

    def _parse_popup_question(self, popup) -> Optional[Dict]:
        try:
            title_selectors = ['.ques-title', '.question-title', '.title', '.question-stem']
            title = ""
            for sel in title_selectors:
                try:
                    elem = popup.find_element(By.CSS_SELECTOR, sel)
                    title = elem.text.strip()
                    if title:
                        break
                except Exception:
                    continue

            option_elems = popup.find_elements(By.CSS_SELECTOR,
                                               '.option.isNotReview, .option-wrap .option, .choice-option')

            options = []
            for i, opt_elem in enumerate(option_elems):
                try:
                    letter_selectors = ['.caption', '.index', '.option-label', '.choice-label']
                    letter = ""
                    for sel in letter_selectors:
                        try:
                            letter_elem = opt_elem.find_element(By.CSS_SELECTOR, sel)
                            letter = letter_elem.text.strip().replace('.', '').replace(')', '').upper()
                            if letter:
                                break
                        except Exception:
                            continue

                    if not letter:
                        letter = chr(65 + i)

                    content_selectors = ['.content', '.option-content', '.text', '.choice-text']
                    content = ""
                    for sel in content_selectors:
                        try:
                            content_elem = opt_elem.find_element(By.CSS_SELECTOR, sel)
                            content = content_elem.text.strip()
                            if content:
                                break
                        except Exception:
                            continue

                    if not content:
                        content = opt_elem.text.strip()

                    options.append({
                        'letter': letter,
                        'text': content,
                        'element': opt_elem
                    })

                except Exception:
                    continue

            if not options:
                return None

            return {
                'question': title,
                'options': options
            }

        except Exception as e:
            print(f"      [监视器] 解析失败: {str(e)[:50]}")
            return None

    def _intelligent_select_answer(self, question_data: Dict) -> str:
        question = question_data['question']
        options = question_data['options']

        print(f"      [监视器]  分析问题: {question[:50]}...")
        prompt = self._build_analysis_prompt(question, options)

        try:
            response = self.analyzer_client.chat.completions.create(
                model=self.analyzer_model,
                messages=[
                    {
                        "role": "system",
                        "content": "你是视频理解助手。根据视频内容选择最正确的答案，只返回选项字母，不要解释。"
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=5
            )

            answer_text = response.choices[0].message.content.strip().upper()

            valid_letters = [opt['letter'] for opt in options]
            for letter in valid_letters:
                if letter in answer_text:
                    print(f"      [监视器]  AI选择: {letter}（模型 {self.analyzer_model}）")
                    return letter

            print(f"      [监视器]  AI 没给出选项字母（回的是 {answer_text[:20]!r}），退回关键词匹配")
            return self._keyword_match(question, options)

        except Exception as e:
            print(f"      [监视器]  ⚠ AI分析失败: {str(e)[:80]}")
            print(f"      [监视器]  ⚠ 用关键词匹配顶替，这一题的答案可能不准")
            return self._keyword_match(question, options)

    def _build_analysis_prompt(self, question: str, options: List[Dict]) -> str:
        """给弹窗题构造提示词。

        以前一律只截取转写的前 2000 字 —— 视频转写常有 3000~4000 字，
        题目考到中后段时，大模型手里根本没有那段内容，只能瞎猜（这是弹窗题
        答错的主要原因）。现在改成按题目/选项的关键词，把转写里最相关的
        几段挑出来给模型，而不是死取开头。
        """
        transcript = self._relevant_transcript(question, options)

        prompt = f"""【视频内容（转写，可能与题目相关的片段）】
{transcript}

【问题】
{question}

【选项】
"""
        for opt in options:
            prompt += f"{opt['letter']}. {opt['text']}\n"

        prompt += """
【任务】
根据视频内容，选择最正确的答案。
要求：
1. 先在脑子里把每个选项跟视频内容核对一遍，再决定；
2. 只返回一个选项字母（如：A），不要解释、不要标点、不要别的内容；
3. 如果视频内容里确实找不到依据，也要给出最可能的那一个字母。

答案："""

        return prompt

    def _relevant_transcript(self, question: str, options: List[Dict],
                             window: int = 700, limit: int = 4000) -> str:
        """从视频转写里挑出与题目最相关的片段（题目太长时按窗口打分取最优）。"""
        transcript = (self.video_transcript or "").strip()
        if not transcript:
            return ""
        if len(transcript) <= limit:
            return transcript

        keywords = {w.lower() for w in re.findall(r"[A-Za-z']{3,}", question)}
        for opt in options:
            keywords |= {w.lower() for w in re.findall(r"[A-Za-z']{3,}", opt.get("text", ""))}
        keywords -= {"the", "and", "that", "this", "with", "have", "from", "they",
                     "what", "which", "does", "about", "because", "there", "their"}

        windows = []
        for start in range(0, len(transcript), window // 2):
            piece = transcript[start:start + window]
            if not piece.strip():
                continue
            lowered = piece.lower()
            score = sum(lowered.count(word) for word in keywords)
            windows.append((score, start, piece))
        if not windows:
            return transcript[:limit]

        windows.sort(key=lambda item: item[0], reverse=True)
        chosen = []
        total = 0
        for _score, start, piece in windows:
            chosen.append((start, piece))
            total += len(piece)
            if total >= limit or len(chosen) >= 4:
                break
        chosen.sort()      # 按原文顺序拼，保持时序
        merged = "\n...\n".join(piece for _start, piece in chosen)
        return merged[:limit]

    def _keyword_match(self, question: str, options: List[Dict]) -> str:
        transcript_lower = self.video_transcript.lower()
        question_lower = question.lower()

        best_option = None
        best_score = -1

        for opt in options:
            opt_text = opt['text'].lower()
            score = 0
            score += transcript_lower.count(opt_text) * 2

            keywords = [w for w in opt_text.split() if len(w) > 3]
            for kw in keywords:
                if kw in transcript_lower:
                    score += 1

            if any(word in question_lower for word in opt_text.split()[:3]):
                score += 3

            if score > best_score:
                best_score = score
                best_option = opt

        if best_option:
            print(f"      [监视器]  关键词匹配: {best_option['letter']} (得分: {best_score})")
            return best_option['letter']

        return options[0]['letter'] if options else "A"

    def _click_option(self, popup, answer: str) -> bool:
        """点弹窗里的选项，元素失效就重新定位再点。

        弹窗会在答题过程中被 U校园 重建，而 AI 分析要好几秒 —— 手里那个 popup 元素
        经常已经失效。日志里的 `点击失败: stale element reference` 就是这么来的：
        AI 选出了 D，结果一个选项都没点上，这题就漏了。所以这里最多重试 3 轮，
        每轮重新找一次弹窗、重新取选项。
        """
        for attempt in range(3):
            try:
                if attempt:
                    fresh = self._find_popup_question()
                    if fresh is not None:
                        try:
                            if not fresh.is_displayed():
                                return False
                        except StaleElementReferenceException:
                            continue
                        popup = fresh
                    self.stop_requested.wait(0.4)
                return self._click_option_once(popup, answer)
            except StaleElementReferenceException:
                continue
            except Exception as e:
                print(f"      [监视器] 点击失败: {str(e)[:50]}")
                return False
        print("      [监视器] ⚠ 选项元素反复失效，本题没能点上")
        return False

    def _click_option_once(self, popup, answer: str) -> bool:
        try:
            option_elems = popup.find_elements(By.CSS_SELECTOR,
                                               '.option.isNotReview, .option-wrap .option')

            for opt_elem in option_elems:
                try:
                    letter_selectors = ['.caption', '.index', '.option-label']
                    letter = ""
                    for sel in letter_selectors:
                        try:
                            letter_elem = opt_elem.find_element(By.CSS_SELECTOR, sel)
                            letter = letter_elem.text.strip().replace('.', '').replace(')', '').upper()
                            if letter:
                                break
                        except Exception:
                            continue

                    if letter == answer.upper():
                        self.driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                            opt_elem
                        )
                        time.sleep(0.2)

                        try:
                            opt_elem.click()
                        except Exception:
                            self.driver.execute_script("arguments[0].click();", opt_elem)

                        return True

                except Exception:
                    continue

            try:
                idx = ord(answer.upper()) - ord('A')
                if 0 <= idx < len(option_elems):
                    opt_elem = option_elems[idx]
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                        opt_elem
                    )
                    time.sleep(0.2)
                    opt_elem.click()
                    return True
            except Exception:
                pass

            return False

        except Exception as e:
            print(f"      [监视器] 点击失败: {str(e)[:50]}")
            return False

    def _click_submit_if_exists(self, popup):
        submit_selectors = [
            '.submit-btn', '.confirm-btn', '.ok-btn',
            'button[type="submit"]', '.popup-submit',
            '.questionReplyBox .submit'
        ]

        for selector in submit_selectors:
            try:
                btn = popup.find_element(By.CSS_SELECTOR, selector)
                if btn.is_displayed():
                    btn.click()
                    print("      [监视器]  已提交")
                    return True
            except Exception:
                continue
        return False

    def _wait_for_video_complete(self, video, duration: float) -> bool:
        """等视频播完：播完/用户停止/超时返回 True；进度读取异常返回 False。

        以前 except 直接 break，任何一次取 currentTime 失败（元素失效、页面重渲染）
        都会被当成「播完了」继续往下走，视频实际没看完。
        """
        max_wait = duration / 2 + 30
        start_time = time.time()
        last_progress = 0

        while time.time() - start_time < max_wait:
            try:
                if self.stop_requested.is_set() or self.stop_monitoring.is_set():
                    return True

                current = self.driver.execute_script("return arguments[0].currentTime;", video)
                ended = self.driver.execute_script("return arguments[0].ended;", video)

                if ended or current >= duration - 1:
                    print(f"       视频播放完成")
                    return True

                elapsed = int(time.time() - start_time)
                if elapsed - last_progress >= 5:
                    print(f"      播放进度: {int(current)}/{int(duration)} 秒")
                    last_progress = elapsed

                if self.stop_requested.wait(0.5):
                    return True

            except Exception as exc:
                print(f"       视频进度读取失败，停止等待: {str(exc)[:50]}")
                return False
        return True

    def _check_video_completed(self) -> bool:
        try:
            indicators = [
                '.video-completed', '.watched', '.finished',
                '[class*="completed"]', '[class*="finished"]'
            ]
            for indicator in indicators:
                if self.driver.find_elements(By.CSS_SELECTOR, indicator):
                    return True
            return False
        except Exception:
            return False


class FlashcardHandler(ContentHandler):
    """单词闪卡处理器 """

    def __init__(self, driver, stop_requested: threading.Event):
        self.driver = driver
        self.stop_requested = stop_requested

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.VOCABULARY_FLASHCARD

    def handle(self, question: Question) -> bool:
        print("     处理单词闪卡...")
        max_cards = 100
        clicked = 0
        if self.stop_requested.wait(2):
            return False
        for i in range(max_cards):
            if self.stop_requested.is_set():
                return False
            try:
                next_btn = self._find_next_button()
                if not next_btn:
                    print(f"      未找到下一个按钮，可能已完成（已点击{clicked}个）")
                    break
                if not next_btn.is_displayed() or not next_btn.is_enabled():
                    print(f"      按钮不可用，完成")
                    break
                try:
                    disabled_next_button = self.driver.find_element(By.CSS_SELECTOR, '.action.next.disabled')
                    if disabled_next_button:
                        break
                except Exception:
                    pass
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                    next_btn
                )
                if self.stop_requested.wait(0.5):
                    return False
                try:
                    next_btn.click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", next_btn)
                clicked += 1
                if self.stop_requested.wait(0.5):
                    return False
                current_word = self.driver.find_element(By.XPATH,
                                                        '//*[@id="question-vocabulary-base-id"]/div/div[2]/div')
                print(f" 学习{current_word.text}")
            except Exception as e:
                error_msg = str(e)
                print(f"      处理闪卡失败: {error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                if self.stop_requested.wait(1):
                    return False
                continue

        print(f"     单词闪卡完成，共 {clicked} 个")
        return True

    def _find_next_button(self):
        selectors = [
            '.vocActions .next',
            '.action.next',
            '.next-btn',
            '.vocabulary-actions .next',
            'button.next',
            '.flashcard-next',
            '[class*="next"]:not([class*="disabled"])',
        ]

        for selector in selectors:
            try:
                elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for elem in elems:
                    if elem.is_displayed():
                        return elem
            except Exception:
                continue
        return None


def _normalize_ok(text: str) -> str:
    """教材名归一化：去掉系列前缀/版本/空格，只留"读写教程3"这类核心。"""
    import re as _re
    return _re.sub(r"新视野大学英语|新编大学英语|（第四版）|\(第四版\)|\s", "", str(text or ""))


class AISolver:
    """AI答题器 - 协调解析、构建、执行流程"""

    def __init__(self, driver, config: Config, config_path: str = "config.json"):
        self.driver = driver
        self.config = config
        self.config_path = config_path
        self._textbook_persisted = False
        self.stop_requested = threading.Event()
        self.ai_client = OpenAICompatibleClient(self.config)
        self.parser = QuestionParser(driver)
        self.prompt_builder = PromptBuilder(self.ai_client)
        self.executor = AnswerExecutor(driver)
        self.video_handler = VideoHandler(driver, self.config, self.stop_requested)
        self.content_handlers: List[ContentHandler] = [
            self.video_handler,
            FlashcardHandler(driver, self.stop_requested),
            SelfCheckHandler(driver, self.stop_requested),
            FollowReadHandler(driver, self.stop_requested),
            DiscussionBoardHandler(driver, self.ai_client, self.stop_requested),
        ]
        self.knowledge_base = KnowledgeBase(
            root=knowledge_root(),
            enabled=getattr(config, "knowledge_enabled", True),
            textbook=getattr(config, "knowledge_textbook", "auto"),
            min_confidence=getattr(config, "knowledge_min_confidence", "medium"),
            verify_wordbank=getattr(config, "knowledge_verify_wordbank", True),
        )
        self.processed_hashes: set = set()
        #: 本轮没做完（没提交/有题空着）的任务，主循环结束后统一回收补做
        self.incomplete_tasks: List[Dict] = []
        self._processed_video_tabs: set = set()
        self._processed_audio_tabs: set = set()

    def _prepare_knowledge_base(self):
        """进入任务前认教材；认出来后写回 config.json，之后不必再猜。"""
        knowledge_base = getattr(self, "knowledge_base", None)
        if knowledge_base is None:
            return
        book = knowledge_base.prepare(self.driver)

        if not book or getattr(self, "_textbook_persisted", False):
            return
        # 配置里是 auto 才回写；用户手填的书名不动它
        config = getattr(self, "config", None)
        pref = (getattr(config, "knowledge_textbook", "auto") or "auto").strip()
        if pref.lower() != "auto":
            self._textbook_persisted = True
            return
        if self._persist_textbook(book):
            print(f"    [知识库] 已把教材「{book}」记进 config.json，以后启动直接使用")

    def _persist_textbook(self, book_name: str) -> bool:
        """把识别到的教材写回 config.json（原子替换，失败不影响运行）。"""
        self._textbook_persisted = True
        path = getattr(self, "config_path", None)
        if not path:
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("knowledge_textbook") == book_name:
                return False
            data["knowledge_textbook"] = book_name
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            return True
        except Exception as exc:
            # 不依赖 __main__ 里赋值的 logger：本模块被 import 时（如测试）它并不存在
            logging.getLogger("UCampusBot").warning(
                f"写回教材名失败（不影响本次运行）: {str(exc)[:80]}"
            )
            return False

    def request_stop(self):
        self.stop_requested.set()

    def clear_stop(self):
        self.stop_requested.clear()

    def _should_stop(self) -> bool:
        return self.stop_requested.is_set()

    def process_selected_tabs(self, selected_tabs: List[Dict], chapter_name: str = "",
                             retry_pass: int = 0):
        """
        按用户勾选的 Tab 列表逐个处理（自动模式）。
        selected_tabs: 完整的 tab dict 列表（来自扫描结果）
        """
        if not chapter_name:
            # 给 AI 会话一个像样的章节名（原来写死字面量 "selected_chapter"，
            # 日志里全是它，排查时分不清正在处理哪门课）
            title = getattr(self.driver, "title", "")
            chapter_name = (title.strip()[:60] if isinstance(title, str) else "") or "selected_chapter"

        print(f"\n{'=' * 60}")
        print(f"开始处理 {len(selected_tabs)} 个选中任务")
        print(f"{'=' * 60}")

        self.ai_client.start_new_chapter(chapter_name)
        self.processed_hashes.clear()

        # 趁还停在课程目录页，先把当前教材认出来，后续各小节的答案都从这本书里取
        self._prepare_knowledge_base()

        course_home_url = self.driver.current_url

        for task_idx, tab in enumerate(selected_tabs):
            if self._should_stop():
                print("  已请求停止，批量处理提前结束")
                break

            tab_name = tab.get('l1_title', 'unknown')

            if '_element' in tab and tab['_element'] is not None:
                print(f"\n  [{task_idx+1}/{len(selected_tabs)}] {tab['display']}")

                if task_idx > 0:
                    self.driver.get(course_home_url)
                    if self.stop_requested.wait(3):
                        break
                    self.ai_client.force_reset(f"{chapter_name}_{tab_name}")

                if '_unit_idx' in tab:
                    try:
                        unit_container = WebDriverWait(self.driver, 10).until(
                            EC.presence_of_element_located(
                                (By.CLASS_NAME, 'unipus-tabs_unitTabScrollContainer__fXBxR'))
                        )
                        unit_tabs = unit_container.find_elements(By.CSS_SELECTOR, ':scope > *')
                        if tab['_unit_idx'] >= len(unit_tabs):
                            print("    目标Unit不存在，跳过")
                            continue
                        if self._should_stop():
                            break
                        self.driver.execute_script("arguments[0].click();", unit_tabs[tab['_unit_idx']])
                        if self.stop_requested.wait(1.2):
                            break
                    except Exception as e:
                        print(f"    切换Unit失败: {str(e)[:50]}")
                        continue

                chapter_clicked = False
                try:
                    chapters = self.driver.find_elements(
                        By.CLASS_NAME, 'courses-unit_taskItemInnerLayout__DTYuN'
                    )
                    name_occurrence = 0
                    for ch in chapters:
                        if self._should_stop():
                            break
                        try:
                            name_elem = ch.find_element(By.CLASS_NAME, 'courses-unit_taskTypeName__99BXj')
                            if name_elem.text.strip() == tab_name:
                                if name_occurrence == tab.get('_name_occurrence', 0):
                                    if self._should_stop():
                                        break
                                    self.driver.execute_script("arguments[0].click();", name_elem)
                                    chapter_clicked = True
                                    break
                                name_occurrence += 1
                        except Exception:
                            continue
                except Exception as e:
                    print(f"    重新定位章节失败: {str(e)[:50]}")

                if self._should_stop():
                    break
                if chapter_clicked:
                    if self.stop_requested.wait(3):
                        break
                    # 以页面状态为准：已经是绿色「已完成」的任务不再重复做
                    try:
                        status = self._current_task_status(tab_name)
                    except Exception:
                        status = ""
                    if status and "已完成" in status:
                        print(f"  ✅ 页面显示「已完成」，跳过: {tab_name}")
                        continue
                    # 扫描结果里的 _unit_idx 是 0 起的，Unit 号从 1 开始
                    unit_no = tab.get('_unit_idx')
                    unit_no = unit_no + 1 if isinstance(unit_no, int) else None
                    if unit_no is None:
                        # 拿不到下标就从任务名里读（「Unit1 - #1 - Quiz」），
                        # 否则同一任务在 6 个 Unit 里都有同名小节，只能整页退回 AI
                        unit_no = self._unit_from_tab(tab)
                    self._process_tab_with_accumulation(
                        tab_name, task_idx, 0, unit=unit_no,
                        prefer_part=self._section_hint_from_tab(tab),
                    )
                    if getattr(self, "_task_incomplete", False):
                        self._remember_incomplete(tab)
                    if self.stop_requested.wait(2):
                        break
                else:
                    print(f"  点击章节失败，跳过")

            else:
                l1_idx = tab.get('l1_idx')
                l2_idx = tab.get('l2_idx', -1)

                level1_tabs = self._get_level1_tabs()
                if l1_idx is None:
                    # 浏览器回查补做的任务只有名字、没有索引：按标题重新定位；
                    # 找不到就跳过 —— 绝不能默认 0 号 Tab 去处理别的任务
                    want = (tab.get('l1_title') or '').strip()
                    for i, t in enumerate(level1_tabs):
                        if (t.get('title') or '').strip() == want:
                            l1_idx = i
                            break
                    if l1_idx is None:
                        print(f"  ⚠ 回查任务「{want}」在当前页面找不到对应章节，跳过（不再默认处理第 0 个 Tab）")
                        continue
                if l1_idx >= len(level1_tabs):
                    print(f"  一级Tab索引 {l1_idx} 越界，跳过")
                    continue

                l1_tab = level1_tabs[l1_idx]
                print(f"\n  [{task_idx+1}/{len(selected_tabs)}] 一级Tab: {l1_tab['title']}")

                if self._should_stop():
                    break
                if not WebDriverHelper.safe_click(self.driver, l1_tab['element']):
                    print(f"  点击一级Tab失败，跳过")
                    continue
                if self.stop_requested.wait(1.5):
                    break
                if l2_idx < 0:
                    self._process_tab_with_accumulation(l1_tab['title'], l1_idx, 0)
                    if self.stop_requested.wait(2):
                        break
                else:
                    level2_tabs = self._get_level2_tabs()
                    if l2_idx >= len(level2_tabs):
                        print(f"  二级Tab索引 {l2_idx} 越界，跳过")
                        continue

                    l2_tab = level2_tabs[l2_idx]
                    print(f"    二级Tab: {l2_tab['title']}")

                    if self._should_stop():
                        break
                    if not WebDriverHelper.safe_click(self.driver, l2_tab['element']):
                        print(f"  点击二级Tab失败，跳过")
                        continue
                    if self.stop_requested.wait(1.5):
                        break
                    combined_name = f"{l1_tab['title']}_{l2_tab['title']}"
                    self._process_tab_with_accumulation(combined_name, l1_idx, l2_idx)
                    if getattr(self, "_task_incomplete", False):
                        self._remember_incomplete(tab)
                    if self.stop_requested.wait(2):
                        break
        print(f"\n{'=' * 60}")
        print("批量处理已停止" if self._should_stop() else f"全部 {len(selected_tabs)} 个任务处理完毕")
        print(f"{'=' * 60}")

        # ---- 未完成任务回收：没做完的题不拖累别的任务，最后统一回头补做 ----
        # 防御式取用：测试替身用 __new__ 绕过 __init__，没有这个属性
        unfinished = list(getattr(self, "incomplete_tasks", []) or [])
        self.incomplete_tasks = []
        if unfinished and not self._should_stop() and retry_pass < 3:
            print(f"\n{'=' * 60}")
            print(f"还有 {len(unfinished)} 个任务没做完，开始第 {retry_pass + 1} 轮补做：")
            # 以浏览器状态为准再核一遍：页面显示未完成的，一并纳入补做（必修优先）
            try:
                browser_left = self.browser_unfinished_tasks()
            except Exception as exc:
                browser_left = []
                print(f"    [回查] 读取状态失败: {str(exc)[:60]}")
            known = {(item.get("l1_title"), item.get("_name_occurrence")) for item in unfinished}
            for item in browser_left:
                occurrence = 0
                for existing in unfinished:
                    if existing.get("l1_title") == item["name"]:
                        occurrence += 1
                key = (item["name"], occurrence)
                if key in known:
                    continue
                unfinished.append({
                    "l1_title": item["name"],
                    "l2_title": "",
                    "display": f"{'[必修]' if item['compulsory'] else '[选修]'} {item['name']}",
                    "is_l2": False,
                    "is_compulsory": item["compulsory"],
                    "_section_title": "",
                    "_name_occurrence": occurrence,
                })
                print(f"       + 纳入补做: {item['name']}")
            for item in unfinished:
                print(f"   · {item.get('display') or item.get('l1_title') or '未命名任务'}")
            print(f"{'=' * 60}")
            self.process_selected_tabs(unfinished, chapter_name, retry_pass + 1)
        elif unfinished:
            print(f"\n{'=' * 60}")
            print(f"⚠ 以下 {len(unfinished)} 个任务补做 {retry_pass} 轮后仍未完成，需要人工处理：")
            for item in unfinished:
                print(f"   · {item.get('display') or item.get('l1_title') or '未命名任务'}")
            print(f"{'=' * 60}")

    #: 目录页上任务状态标签的类名（截图里是「已完成 / 未开始」那种小胶囊）
    STATUS_SELECTORS = (
        '[class*="taskStatus"]', '[class*="task-status"]', '[class*="taskState"]',
        '[class*="status"]', '[class*="Status"]', '.ant-tag',
    )

    def read_task_status(self, unit_index: Optional[int] = None) -> List[Dict]:
        """读课程目录页里每个任务的完成状态（以浏览器显示为准）。

        截图里每个任务右侧都有「已完成 / 未开始 / 进行中」标签 —— 这是最可靠的
        完成依据：程序内部记的清单只能说明"我做过"，页面标签才说明"U校园认了"。
        """
        results: List[Dict] = []
        try:
            chapters = self.driver.find_elements(
                By.CLASS_NAME, 'courses-unit_taskItemInnerLayout__DTYuN')
        except Exception:
            print("    [回查] 当前不在课程目录页，跳过状态读取")
            return results
        for chapter in chapters:
            try:
                name_elem = chapter.find_element(By.CLASS_NAME, 'courses-unit_taskTypeName__99BXj')
                name = (name_elem.text or "").strip()
            except Exception:
                continue
            if not name:
                continue
            status = ""
            for selector in self.STATUS_SELECTORS:
                try:
                    element = chapter.find_element(By.CSS_SELECTOR, selector)
                    status = re.sub(r"\s+", "", element.text or "")
                except Exception:
                    continue
                if status:
                    break
            compulsory = False
            try:
                chapter.find_element(By.CLASS_NAME, 'courses-unit_taskRequireIcon__zZldK')
                compulsory = True
            except Exception:
                pass
            results.append({"name": name, "status": status, "compulsory": compulsory,
                            "_element": name_elem})
        return results

    def browser_unfinished_tasks(self) -> List[Dict]:
        """页面上还没完成的任务（必修优先）。状态读不到时返回空列表，不瞎猜。"""
        tasks = [item for item in self.read_task_status()
                 if item["status"] and "已完成" not in item["status"]]
        tasks.sort(key=lambda item: not item["compulsory"])   # 必修排前面
        if tasks:
            print("    [回查] 浏览器显示还没完成的任务：")
            for item in tasks:
                tag = "必修" if item["compulsory"] else "选修"
                print(f"       · [{tag}] {item['name']}（{item['status']}）")
        return tasks

    def _remember_incomplete(self, tab: Any):
        """把没做完的任务记下来（去掉元素引用，避免握着过期句柄）。"""
        if not isinstance(tab, dict):
            return
        record = {k: v for k, v in tab.items() if not str(k).startswith("_element")}
        for existing in self.incomplete_tasks:
            if existing.get("display") == record.get("display") and \
                    existing.get("l1_idx") == record.get("l1_idx") and \
                    existing.get("l2_idx") == record.get("l2_idx"):
                return
        self.incomplete_tasks.append(record)
        print(f"    ↻ 记为未完成，稍后重试: {record.get('display') or record.get('l1_title')}")

    def _process_tab_with_accumulation(self, tab_name: str, l1_idx: int, l2_idx: int,
                                       unit: int = None, prefer_part: str = None) -> bool:
        """处理Tab - 累积原文模式，包含视频/音频预处理"""

        if self._should_stop():
            return False
        # 任务边界：本任务预处理新增的视听转写才算「本任务的上下文」，
        # 上一个任务的转写不串进来（联系上下文作答只带本任务的）
        self.ai_client.mark_task_boundary()
        self._preprocess_video_if_needed(tab_name, l1_idx, l2_idx)
        if self._should_stop():
            return False
        self._preprocess_audio_if_needed(tab_name, l1_idx, l2_idx)
        if self._should_stop():
            return False

        current_passage = self._extract_passage()
        if self._should_stop():
            return False
        if current_passage:
            self.ai_client.add_passage_if_new(current_passage)

        return self._process_current_tab_content(
            self.ai_client.current_chapter_id or "unknown", tab_name, l1_idx, l2_idx,
            unit=unit, prefer_part=prefer_part
        )

    @staticmethod
    def _section_hint_from_tab(tab: Any) -> Optional[str]:
        """把任务列表里的必修/选修转成 Section 编号提示：必修 → A，选修 → B。

        知识库里 Section A 与 Section B 常有同名任务（Critical thinking、Words in use…），
        页面又只显示任务名。读写类教材里必修就是 Text A、选修是 Text B/C，这条对应关系
        是稳定的，正好用来打破平局。给不出就返回 None，匹配器照旧自行拒绝。
        """
        if not isinstance(tab, dict) or "is_compulsory" not in tab:
            return None
        return "A" if tab.get("is_compulsory") else "B"

    @staticmethod
    def _unit_from_tab(tab: Any) -> Optional[int]:
        """从任务名里抠 Unit 号（任务列表写作「Unit1 - #1 - Quiz」）。

        扫描结果里的 _unit_idx 是 Unit 列表的下标，个别任务记录没有它，unit 就成了 None。
        匹配器拿不到 Unit 号时不敢在跨 Unit 的一堆同名小节（每单元都有一份 Vocabulary
        learning · Quiz）里挑，整页退回 AI —— 而任务名本身往往就写着 Unit 号。
        """
        if not isinstance(tab, dict):
            return None
        for key in ("name", "display", "l1_title", "title"):
            text = tab.get(key)
            if not isinstance(text, str):
                continue
            match = re.search(r"Unit\s*(\d{1,2})", text, re.I)
            if match:
                return int(match.group(1))
        return None

    def _process_current_tab_content(self, chapter_name: str, tab_name: str, l1_idx: int, l2_idx: int,
                                     unit: int = None, prefer_part: str = None) -> bool:
        if self._should_stop():
            return False
        direction_part = self._generate_content_hash_from_direction()
        if direction_part == "empty":
            direction_part = "no_direction"

        content_hash = f"{chapter_name}|{tab_name}|{l1_idx}|{l2_idx}|{direction_part}"

        print(f"    内容标识: {hashlib.md5(content_hash.encode()).hexdigest()[:16]}...")

        if content_hash in self.processed_hashes:
            print(f"   ⏭ 已处理过，跳过")
            return False

        self.processed_hashes.add(content_hash)

        page_num = 1
        total_answered = 0
        last_questions_signature = ""
        self._answer_incomplete = False
        self._task_incomplete = False
        self._last_applied = []      # 本页实际填过的答案，供收割使用
        # 同一个任务里视频只播一次：预处理已经播过一遍（录音转写、计时长），
        # 如果 VideoHandler 再播一遍，页面又会重新解析出 VIDEO，来回就是死循环。
        video_handled_this_tab = False

        while True:
            if self._should_stop():
                print("    已请求停止，当前任务提前结束")
                return False

            self._preprocess_video_if_needed(tab_name, l1_idx, l2_idx)
            if self._should_stop():
                return False
            self._preprocess_audio_if_needed(tab_name, l1_idx, l2_idx)
            if self._should_stop():
                return False
            # Sample 页：先停留够时间并进入它的 Practicing（实测必须走这一步）
            if page_num == 1 and self._handle_sample_dwell(tab_name):
                if self._should_stop():
                    return False

            questions, directions = self.parser.parse_all()
            if self._should_stop():
                return False
            print(f"\n    处理第 {page_num} 页题目...")
            print(f"    找到 {len(questions)} 个可见题目")

            current_signature = self._generate_questions_signature(questions)

            if current_signature == last_questions_signature and page_num > 1:
                print(f"    题目内容与上次相同，可能已到达最后一页")
                break

            last_questions_signature = current_signature

            special_handled = False
            self_check_handled = False
            for q in questions:
                if self._should_stop():
                    return False
                for handler in self.content_handlers:
                    if self._should_stop():
                        return False
                    if handler.can_handle(q):
                        print(f"     使用 {handler.__class__.__name__} 处理")
                        ok = handler.handle(q)
                        if ok is False:
                            # 处理器说没做成（讨论板没发出去/跟读没录上）：不能虚假成功，
                            # 标记本任务未完成，交给补做轮次或人工处理
                            print(f"     ⚠ {handler.__class__.__name__} 处理未成功，本任务标记为未完成")
                            self._task_incomplete = True
                        if self._should_stop():
                            return False
                        special_handled = True
                        if q.q_type == QuestionType.SELF_CHECK:
                            self_check_handled = True
                        if q.q_type in [QuestionType.VOCABULARY_FLASHCARD, QuestionType.VIDEO]:
                            if q.q_type == QuestionType.VIDEO and (
                                    video_handled_this_tab
                                    or self._video_already_watched(q)):
                                print("    该视频本次任务已看过，跳过重复播放")
                                break
                            print(f"    特殊内容处理完成")
                            if q.q_type == QuestionType.VIDEO:
                                video_handled_this_tab = True
                            # 视频/闪卡看完还有「提交」要按，不按任务等于没完成
                            self.executor.finish_task_rounds()
                            if self._should_stop():
                                return False
                            self._wait_for_submit_complete()
                            self._handle_confirm_dialog()
                            others = [other for other in questions
                                      if other is not q and other.q_type not in (
                                          QuestionType.VOCABULARY_FLASHCARD, QuestionType.VIDEO)]
                            if not others:
                                return True
                            # 同一页还有别的题（视频 + 作答）时别急着收工，接着把它们做完
                            break
                        if q.q_type == QuestionType.DISCUSSION_BOARD:
                            # 讨论板发完，页面上若还有「提交」也要点掉
                            self.executor.finish_task_rounds()
                            if self._should_stop():
                                return False
                            self._handle_confirm_dialog()
                        break

            normal_questions = [q for q in questions if q.q_type not in [
                QuestionType.VOCABULARY_FLASHCARD,
                QuestionType.VIDEO,
                QuestionType.SELF_CHECK,
                QuestionType.DISCUSSION_BOARD
            ]]

            if normal_questions:
                print(f"    共 {len(normal_questions)} 道题目需要回答")
                # 提交闸门：这一页还有空输入框 / 有题没拿到答案时，任何提交按钮都不许点
            # 默认「无论如何都提交」：实测视频弹窗里的题已经答过、但解析不到，
            # 旧闸门因此拒绝提交，导致任务永远不完成。要恢复严格模式：
            # config.json 里把 strict_submit_check 设成 true。
            config = getattr(self, "config", None)
            # is True 判断：测试替身（Mock）的属性一律不当真
            if config is not None and getattr(config, "strict_submit_check", False) is True:
                self.executor.can_submit = (
                    lambda qs=normal_questions: (not self._unfilled_questions(qs)
                                                 and not getattr(self, "_answer_incomplete", False)))
                print("    （严格提交模式：有题没答就不提交）")
            else:
                self.executor.can_submit = None

            # 注意：答题主体与提交闸门解耦 —— 无论 strict_submit_check 开或关，
            # 这里都必须执行（之前整段缩在 else 里，开严格模式后一道题都不填、永不提交）
            # ① 先查本地题库知识库：命中的题直接用本地答案填写
            kb_hits, ai_questions = self.knowledge_base.lookup(
                normal_questions,
                driver=self.driver,
                tab_name=tab_name,
                directions=directions,
                unit=unit,
                prefer_part=prefer_part,
            )
            if self._should_stop():
                return False

            # ①.5 看视频认人：答案取决于「视频里谁在说那句话」，音频转写给不出人 ——
            # 走「台词分段抽帧 + 视觉逐帧认人 + 本地配对」链路；拿不准就留给下面的 AI。
            # （题库命中优先：命中了就不必再花一次视频下载+识别）
            self._face_evidence = ""
            if ai_questions:
                _vision_done = []
                for _q in list(ai_questions):
                    if not self._looks_like_people_matching(_q):
                        continue
                    if self._should_stop():
                        return False
                    _ans = self._answer_people_by_video(_q, directions)
                    if _ans:
                        kb_hits.append((_q, _ans))
                        _vision_done.append(_q)
                if _vision_done:
                    ai_questions = [q for q in ai_questions if q not in _vision_done]

            # 括号汉译英：先用「对齐截取」从本板块英文原文里抄出短语（不经模型，
            # 避免同义改写 —— 实测 AI 会把 green industry chain 写成 green industrial chain）。
            try:
                import phrase_extract as _pe
                _picked = []
                for _q in list(ai_questions):
                    _hits = _pe.extract_with_fallback(getattr(_q, "text", "") or "")
                    if not _hits:
                        continue
                    _ans = "\n".join(f"{_i + 1}. {_ph}" for _i, (_cn, _ph, _s) in enumerate(_hits))
                    kb_hits.append((_q, _ans))
                    ai_questions.remove(_q)
                    _picked.append("；".join(f"{_cn}→{_ph}" for _cn, _ph, _s in _hits))
                if _picked:
                    print("    🔎 括号对齐截取（照抄原文）：" + " | ".join(_picked))
            except Exception as _exc:
                print(f"    （括号对齐截取不可用：{str(_exc)[:50]}）")
            if kb_hits:
                kb_success, stopped = self._apply_answers(kb_hits)
                if stopped:
                    return False
                total_answered += kb_success
                print(f"    📚 知识库直接填写 {kb_success}/{len(kb_hits)} 题")

            # ② 知识库未命中的题走原来的 AI 流程（逻辑与参数完全不变）
            if ai_questions:
                # 页面上有录音控件（跟读/录音题）时，整页交给跟读处理器；
                # AI 作答在这里帮不上忙，反而会把改写后的句子当作答案去填，
                # 实测会覆盖已答对的题（客观题 2/4 → 1/4），所以直接跳过。
                try:
                    _rec = self.driver.find_elements(By.CSS_SELECTOR,
                                                   '.ucomp-recorder, .button-record, [class*="record-icon"]')
                except Exception:
                    _rec = []
                if _rec:
                    print('    （本页含录音控件：已由跟读处理器完成，跳过 AI 作答，避免覆盖已答内容）')
                    ai_questions = []
                if ai_questions:
                    print(f"    🤖 {len(ai_questions)} 道题交给 AI 作答")
                    prompt = self.prompt_builder.build(ai_questions, directions)
                    # 本任务有视听转写时直接拼进提示词：听后填空/看视频填空这类题
                    # 必须联系转写作答——对话历史会随任务切换重置，只靠它不可靠
                    transcript_ctx = self.ai_client.recent_transcript_context()
                    if transcript_ctx:
                        prompt = f"{prompt}\n\n{transcript_ctx}"
                        print(f"    🎧 已把本任务转写并入提示词（联系上下文作答）")
                    _evidence = getattr(self, "_face_evidence", "")
                    if _evidence:
                        prompt = f"{prompt}\n\n{_evidence}"
                        print("    👁 已把「逐帧认人」证据并入提示词（画面↔台词对照）")
                        self._face_evidence = ""
                    # 题库没命中 → 先联网搜一份参考材料，和大模型的知识互相印证。
                    # 但本任务已有视听转写时跳过：转写就是权威上下文，搜来的
                    # 无关材料只会把模型带偏（实测它会把「无相关结果」的框复读成答案）
                    elif self._web_search_ready():
                        try:
                            web = web_search_context(ai_questions, directions, self.config)
                        except Exception as exc:
                            web = ""
                            print(f"    ⚠ 联网搜索异常（不影响答题）: {str(exc)[:60]}")
                        if web:
                            print("    🌐 已把联网搜索结果作为补充材料交给 AI")
                            prompt = f"{prompt}\n\n{web}"
                    if self._should_stop():
                        return False
                    ai_response = self.ai_client.ask(prompt, stop_requested=self.stop_requested)
                    if self._should_stop():
                        return False
                    # 大模型说「没给原文/缺少题干」时，它要的东西其实就在页面上 ——
                    # 自己抓过来补一次，别把题空着交回去。
                    if ai_response and looks_like_ai_refusal(ai_response):
                        print(f"    ⚠ AI 说题目信息不全，抓取页面原文后重问一次")
                        retry = self._ask_with_page_text(ai_questions, directions, prompt)
                        if retry:
                            ai_response = retry
                    if ai_response:
                        ai_success, stopped = self._apply_answers(
                            self._pair_ai_answers(ai_questions, ai_response)
                        )
                        if stopped:
                            return False
                        total_answered += ai_success
                        print(f"    本页成功填写 {ai_success}/{len(ai_questions)} 题")

            if self._should_stop():
                return False
            if self_check_handled and not normal_questions and self.executor.submit():
                self._wait_for_submit_complete()
                if self._should_stop():
                    return False
                self._handle_confirm_dialog()
            if special_handled and not normal_questions and not self_check_handled:
                # 讨论板/闪卡这类只有特殊内容、没有普通题的页面，收尾也把「提交」点掉
                self.executor.finish_task_rounds()
                if self._should_stop():
                    return False
                self._handle_confirm_dialog()
            next_btn = self._find_next_by_text() or self._find_next_question_button()
            if next_btn:
                print(f"    点击下一题...")
                pre_click_signature = current_signature

                if self._should_stop():
                    return False
                if not WebDriverHelper.safe_click(self.driver, next_btn):
                    print(f"    点击下一题失败")
                    break

                if not self._wait_for_content_change(pre_click_signature, timeout=5):
                    if self._should_stop():
                        return False
                    print(f"    内容未变化，可能已到最后一页")
                    break

                page_num += 1

                if page_num > 50:
                    print(f"    达到最大页数限制，停止")
                    break
                continue

            if self._should_stop():
                return False
            # 提交前复核：输入框还有空的就反复补做（题库没有答案的，让 AI 现写）。
            # 硬规矩：只要还有题没答上，就绝不点提交 —— 否则控制台报"提交完成"，
            # 实际等于交白卷。补做 3 轮仍空着就本次不提交，交回给下一次跑。
            remaining: List[Question] = []
            if normal_questions:
                for attempt in range(1, 4):
                    remaining = self._unfilled_questions(normal_questions)
                    if not remaining:
                        break
                    print(f"    ⚠ 还有 {len(remaining)} 道题的输入框是空的，第 {attempt}/3 次补做")
                    filled = self._refill_unfilled(normal_questions, directions)
                    if filled:
                        total_answered += filled
                    if self._should_stop():
                        return False
                remaining = self._unfilled_questions(normal_questions)
            if self._should_stop():
                return False
            if remaining or (normal_questions and getattr(self, "_answer_incomplete", False)):
                print(f"    ⛔ 仍有题目没拿到答案（空输入框 {len(remaining)} 道），"
                      f"本次不点提交（宁可不交，也不交白卷）")
                if remaining:
                    print(f"       未完成的题号：{[q.number for q in remaining]}")
                self._task_incomplete = True
                break
            if normal_questions and self.executor.submit():
                self._wait_for_submit_complete()
                if self._should_stop():
                    return False
                self._handle_confirm_dialog()
                # 提交后页面若刷新又冒出「发布/提交」，继续点掉（最多 3 轮）
                self.executor.finish_task_rounds()

                # ---- 答案收割：只有浏览器明确显示「全对」才收录，cmd 里的自说自话不算 ----
                if getattr(self.config, "harvest_verified_answers", True):
                    self._harvest_if_all_correct(normal_questions, tab_name, unit)
            print(f"    没有更多题目了")
            break
        print(f"    总共回答 {total_answered} 题")
        return True

    def _ask_with_page_text(self, questions: List[Question], directions: str,
                            original_prompt: str) -> Optional[str]:
        """把页面上能抓到的正文补给大模型，再问一次。

        大模型回「题目中未提供完整文章 / 缺少题干 / 请补充原文」时，缺的东西基本都
        在页面 DOM 里：可能是阅读材料，也可能是题目容器自己的文字（题干+选项）。
        这里把两样都抓来拼成补充材料重问，并要求它必须给出答案。
        """
        pieces: List[str] = []
        passage = self._extract_passage()
        if passage:
            pieces.append(f"【阅读材料】\n{passage}")
        for question in questions:
            body = self._question_body_text(question)
            if body:
                pieces.append(f"【第 {question.number} 题页面文字】\n{body}")
        if not pieces:
            page_text = self._page_visible_text()
            if page_text:
                pieces.append(f"【页面文字】\n{page_text}")
        if not pieces:
            print("    ⚠ 页面上也抓不到可补的文字")
            return None

        prompt = (
            f"{original_prompt}\n\n"
            "【补充材料】上一次你说题目信息不全。下面是从页面上抓到的全部文字，"
            "请据此作答：\n" + "\n\n".join(pieces) + "\n\n"
            "【必须遵守】不要再要求我提供文章或题干，不要再回答「无法确定」「无法作答」，"
            "也不要说明你缺什么。按上面要求的格式，直接给出你能给出的最佳答案；"
            "个别空实在拿不准，也要按题目上下文给出最可能的答案。"
        )
        if self._should_stop():
            return None
        return self.ai_client.ask(prompt, stop_requested=self.stop_requested)

    #: Sample 这类页面的停留秒数（用户实测：要停够 2 分钟，再点「Practicing」）
    SAMPLE_DWELL_SECONDS = 120

    def _handle_sample_dwell(self, tab_name: str) -> bool:
        """Sample 页：停留满要求时长，再点左上角的「Practicing」，这个任务才算完成。

        实测：Sample（范文）页秒过不算完成，必须停够时间并进入它的 Practicing。
        """
        name = (tab_name or "").lower()
        if "sample" not in name:
            return False
        print(f"    ⏳「{tab_name}」需停留 {self.SAMPLE_DWELL_SECONDS} 秒，然后再点「Practicing」")
        deadline = time.time() + self.SAMPLE_DWELL_SECONDS
        while time.time() < deadline:
            if self._should_stop():
                return True
            self.stop_requested.wait(3)
        if self._should_stop():
            return True

        clicked = False
        for selector in ('[class*="tab"]', '[class*="Tab"]', 'a', 'button', 'li', 'span', 'div'):
            if clicked:
                break
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements[:60]:
                try:
                    label = re.sub(r"\s+", "", element.text or "")
                except Exception:
                    continue
                if label != "Practicing":
                    continue
                try:
                    if not element.is_displayed():
                        continue
                    WebDriverHelper.safe_click(self.driver, element)
                    print("    ✅ 已点击「Practicing」")
                    clicked = True
                    break
                except Exception:
                    continue
        if not clicked:
            print("    ⚠ 没找到「Practicing」标签，Sample 这个小节可能不会被判为完成")
        self.stop_requested.wait(3)
        return True

    def _web_search_ready(self) -> bool:
        """是否该为这一页联网搜索：开了开关、且真的配了 key 才搜。

        用 isinstance 判断，避免测试替身（Mock 配置）被误当成"已配置"。
        """
        config = getattr(self, "config", None)
        if config is None:                 # 测试替身可能根本没挂 config
            return False
        enabled = getattr(config, "search_enabled", True)
        if not isinstance(enabled, bool) or not enabled:
            return False
        key = getattr(config, "search_api_key", "")
        return isinstance(key, str) and len(key.strip()) > 8

    def _harvest_if_all_correct(self, questions, task: str, unit) -> bool:
        """浏览器确认全对 → 把这页的答案收录进题库（判定以页面信号为准）。"""
        # 快速处理流程没有扫描阶段、拿不到 Unit：按页面 Unit 标签识别一次。
        # 收录块标题带着真实 Unit（## 浏览器核对收录（Unit 3）），下次匹配时才能
        # 通过 Unit 硬过滤；识别不到就保持 None（匹配器对未知 Unit 不做排除）。
        if unit is None:
            try:
                unit = self.knowledge_base.detect_unit(self.driver)
                if unit:
                    print(f"    [收割] 从页面识别到 Unit {unit}")
            except Exception:
                unit = None
        # 判据以「答题小结」为准：正确率/得分 ≥ 80% 就收录（闯关线是 60，这里更严）
        try:
            from answer_harvest import read_score_summary
            summary = read_score_summary(self.driver)
        except Exception as exc:
            summary = {}
            print(f"    [收割] 读取判分失败: {str(exc)[:60]}")
        ratio = summary.get("ratio")
        threshold = float(getattr(self.config, "harvest_min_score", 80) or 80) / 100
        print(f"    [收割] 答题小结: 正确 {summary.get('correct')}/{summary.get('total')}，"
              f"得分 {summary.get('score')}，判定比例 {ratio}")
        # 判分后页面会公布标准答案（.reference 里就是各空缺的字母，截图实证）。
        # 它是权威答案，有它就按它收，不再受分数门槛限制 —— 门槛是防「瞎填入库」的，
        # 而页面公布的答案不存在这个问题。
        revealed = []
        try:
            from page_key import read_fill_values
            revealed = read_fill_values(self.driver)
        except Exception as exc:
            print(f"    [收割] 读页面公布答案失败: {str(exc)[:50]}")
        if revealed:
            print(f"    [收割] 页面公布了 {len(revealed)} 条标准答案 → 以它为准收录")

        if not revealed and (not isinstance(ratio, (int, float)) or ratio < threshold):
            print(f"    [收割] 未达 {int(threshold * 100)}% 门槛，本次不收录（避免把错答案写进题库）")
            return False

        # 优先用「程序实际填进去的答案」：题库点选项填的那种，回头读页面读不回来。
        # 写库前清洗 + 拆分（见 clean_harvest_entries）：整串编号答案拆回独立条目、
        # 分隔线等格式渣清掉，保证题库里的条数与页面的空数一致。
        answers = ([ans for _num, ans in revealed] if revealed else
                   [str(ans).strip() for _num, ans in
                    (getattr(self, "_last_applied", None) or []) if str(ans).strip()])
        answers = clean_harvest_entries(answers)
        if answers:
            print(f"    [收割] 用本次实际填入的 {len(answers)} 条答案收录")
        for question in ([] if answers else questions):
            values = []
            for inp in (getattr(question, "inputs", None) or []):
                try:
                    values.append((inp.get_attribute("value") or "").strip())
                except Exception:
                    values.append("")
            if values and any(values):
                answers.append(values[0] if len(values) == 1
                               else " ".join(v for v in values if v))
                continue
            picked = [opt.letter for opt in (getattr(question, "options", None) or [])
                      if getattr(opt, "is_selected", False)]
            if picked:
                answers.append("".join(picked))
                continue
            element = getattr(question, "element", None)
            number = ""
            if element is not None:
                for selector in ('[class*="active"]', '[class*="select"]', '[class*="checked"]'):
                    try:
                        cells = element.find_elements(By.CSS_SELECTOR, selector)
                    except Exception:
                        cells = []
                    for cell in cells[:3]:
                        digits = re.findall(r"\b[1-5]\b", cell.text or "")
                        if digits:
                            number = digits[0]
                            break
                    if number:
                        break
            if number:
                answers.append(number)
                continue
            values = []
            if element is not None:
                for selector in ('input[type="text"]', 'textarea', '.user-answer-text'):
                    try:
                        found = element.find_elements(By.CSS_SELECTOR, selector)
                    except Exception:
                        found = []
                    for item in found:
                        try:
                            values.append((item.get_attribute("value")
                                           or item.text or "").strip())
                        except Exception:
                            continue
            if values and any(values):
                answers.append(" ".join(v for v in values if v))
        if not answers:
            print("    [收割] 页面判为全对，但没取到填过的答案，跳过收录")
            return False

        kb_book = getattr(getattr(self, "knowledge_base", None), "_book", None)
        book_path = getattr(kb_book, "path", "")
        if not book_path:
            print("    [收割] 还没确定教材文件，跳过收录")
            return False
        # 串书保护：题库文件对应的教材名必须出现在当前页面上
        # （比如在视听说教程3 的页面上，绝不能把答案写进读写教程3 的文件）。
        # 教材名拿不到就退回用文件名，两者都是「这本书」的标识。
        book_title = str(getattr(kb_book, "name", "") or os.path.basename(book_path))
        # 课程 id 守卫：页面 cid 对应的教材必须与要写入的教材一致，
        # 否则绝不写（同系列"新视野大学英语（第四版）"下的读写/视听说靠名字分不开）。
        try:
            from course_map import book_of as _book_of
            _url = ""
            try:
                _url = self.driver.current_url or ""
            except Exception:
                _url = ""
            _expect = _book_of(_url)
            _actual = str(getattr(kb_book, "name", "") or "")
            if _expect and _actual and _normalize_ok(_expect) != _normalize_ok(_actual):
                print(f"    [收割] 页面课程是「{_expect}」，要写的是「{_actual}」→ 拒绝收录")
                return False
        except Exception as _exc:
            print(f"    [收割] 课程 id 守卫不可用（{str(_exc)[:40]}）")
        key = re.sub(r"新视野大学英语|新编大学英语|（第四版）|\(第四版\)|\s", "", book_title)
        page_flat = re.sub(r"\s", "", self._page_visible_text(limit=1200))
        if key and page_flat and key not in page_flat:
            print(f"    [收割] 页面与题库文件不是同一本（页面不含「{key}」），本次不收录")
            return False

        # 题目指纹：从页面题目文本里挑最能代表这道题的实词，写进收录块。
        # 重做同题时用它核对「这套答案就是这道题的」——同名同空数的小节靠它
        # 精确区分（以前只能整组拒绝），指纹对不上时照旧拒绝、交回 AI。
        # 选项文本也要算题目内容：多选题的辨识信息全在选项里（题干常只有
        # 「The expressions:」），只取题干会收出「passage」这种单词无效指纹。
        fp_texts = [str(getattr(q, "text", "") or "") for q in (questions or [])]
        for q in (questions or []):
            for option in (getattr(q, "options", None) or []):
                fp_texts.append(str(getattr(option, "text", "") or ""))
        fp_texts.append(str(task or ""))
        fingerprint: List[str] = []
        try:
            from knowledge_base import extract_fingerprint
            fingerprint = extract_fingerprint(fp_texts)
        except Exception as exc:
            print(f"    [收割] 题目指纹提取失败（不影响收录）: {str(exc)[:50]}")
        if fingerprint:
            print(f"    [收割] 题目指纹: {' | '.join(fingerprint[:6])}"
                  + ("…" if len(fingerprint) > 6 else ""))

        ok = harvest_answers(book_path, unit, task, answers,
                             summary if isinstance(summary, dict) else {},
                             fingerprint=fingerprint)
        if ok:
            print(f"    [收割] ✅ 已收录 {len(answers)} 条答案到题库：{os.path.basename(book_path)}")
            # 热重载：把刚写进文件的答案读回内存，本轮再遇同题即可命中
            try:
                self.knowledge_base._load_books()
                self.knowledge_base.prepare(self.driver)
                print("    [收割] 题库已热重载（本轮即可命中新收录的答案）")
            except Exception as _exc:
                print(f"    [收割] 热重载失败: {str(_exc)[:50]}")
        return ok

    def _question_body_text(self, question: Question, limit: int = 3000) -> str:
        """取题目容器自己的文字：题干、选项、填空所在的句子都在里面。"""
        element = getattr(question, "element", None)
        if element is None:
            return ""
        try:
            text = element.text or ""
        except Exception:
            return ""
        text = re.sub(r"[ \t\u00a0]+", " ", str(text))
        text = re.sub(r"\n\s*\n\s*", "\n", text).strip()
        return text[:limit]

    def _unfilled_questions(self, questions: List[Question]) -> List[Question]:
        """找出输入框还空着的题目。

        提交前必查这一步：页面上明明还有空框就点「提交」，控制台会报"提交完成"，
        实际等于交白卷（实测踩过）。
        """
        unfilled: List[Question] = []
        for question in questions:
            inputs = list(getattr(question, "inputs", None) or [])
            if not inputs:
                continue
            empty = 0
            for inp in inputs:
                try:
                    value = (inp.get_attribute('value') or inp.text or "").strip()
                except Exception:
                    value = ""
                if not value:
                    empty += 1
            if empty:
                unfilled.append(question)
        return unfilled

    def _refill_unfilled(self, questions: List[Question], directions: str) -> int:
        """把还空着的题补做一遍：题库没有答案时直接让大模型现写。

        返回补上的题数。这是「实在查不到题库也必须做」的兜底。
        """
        unfilled = self._unfilled_questions(questions)
        if not unfilled:
            return 0
        print(f"    ⚠ {len(unfilled)} 道题的输入框还是空的，补做一次")
        prompt = self.prompt_builder.build(unfilled, directions)
        transcript_ctx = self.ai_client.recent_transcript_context()
        if transcript_ctx:
            prompt = f"{prompt}\n\n{transcript_ctx}"
        if self._should_stop():
            return 0
        answer = self.ai_client.ask(prompt, stop_requested=self.stop_requested)
        if answer and looks_like_ai_refusal(answer):
            print("    ⚠ AI 说信息不全，抓页面原文后重问一次")
            answer = self._ask_with_page_text(unfilled, directions, prompt)
        if not answer or looks_like_ai_refusal(answer):
            # 空响应/拒答：把原文强塞给它再要一次答案，这是最后一道网
            print(f"    ⚠ AI 没给出可用答案（{str(answer)[:40]!r}），带页面原文再要一次")
            answer = self._ask_with_page_text(unfilled, directions, prompt)
        if not answer or looks_like_ai_refusal(answer):
            # 最后一招：题目文字抓不到（空/整块是图片）时，让视觉模型看着截图作答
            print(f"    ⚠ 文本模型没给出答案（{str(answer)[:40]!r}），改用视觉模型读题")
            answered = 0
            for question in unfilled:
                vision = self._vision_read_answer(question, directions)
                if not vision or looks_like_ai_refusal(vision):
                    continue
                if self.executor.execute(question, vision):
                    answered += 1
                if self._should_stop():
                    return answered
            if answered:
                print(f"    👁 视觉读题补上 {answered} 题")
                return answered
            print(f"    ⚠ 补做仍然没拿到答案：{str(answer)[:80]!r}")
            return 0
        filled, stopped = self._apply_answers(self._pair_ai_answers(unfilled, answer))
        if stopped:
            return filled
        still = self._unfilled_questions(unfilled)
        if still:
            print(f"    ⚠ 仍有 {len(still)} 道题的输入框是空的（已尽力）")
        return filled

    @staticmethod
    def _video_already_watched(question) -> bool:
        """页面上这个视频是不是已经播完了（避免再播一遍）。"""
        element = getattr(question, "element", None)
        if element is None:
            return False
        try:
            videos = element.find_elements(By.TAG_NAME, 'video')
        except Exception:
            return False
        if not videos:
            return False
        for video in videos:
            try:
                current = float(video.get_property('currentTime') or 0)
                duration = float(video.get_property('duration') or 0)
                paused = bool(video.get_property('paused'))
            except Exception:
                continue
            # 播到末尾（或只剩不到 3 秒）且已停下 → 不用再播
            if duration and current >= duration - 3 and paused:
                return True
        return False

    #: 图片可能不是 <img>：也可能是 CSS 背景图、canvas、svg，或在外层容器里
    IMAGE_HINT_SELECTORS = (
        'img', 'canvas', 'svg', 'picture',
        '[style*="background-image"]', '[class*="img"]', '[class*="pic"]',
        '[class*="image"]', '[class*="photo"]', '[class*="avatar"]',
    )

    @classmethod
    def _has_image_options(cls, question) -> bool:
        for opt in getattr(question, "options", None) or []:
            element = getattr(opt, "element", None)
            if element is None:
                continue
            targets = [element]
            try:                      # 图片常常在选项的外层容器里
                targets.append(element.find_element(By.XPATH, '..'))
            except Exception:
                pass
            for target in targets:
                for selector in cls.IMAGE_HINT_SELECTORS:
                    try:
                        if target.find_elements(By.CSS_SELECTOR, selector):
                            return True
                    except Exception:
                        continue
        return False

    @staticmethod
    def _looks_like_matching(question) -> bool:
        """题目要求里出现 match / 配对 / 对应 就按配对题处理（必须看图）。"""
        text = f"{getattr(question, 'directions', '') or ''} " \
               f"{getattr(question, 'text', '') or ''}".lower()
        return any(word in text for word in ("match", "配对", "对应", "相连"))

    @staticmethod
    def _looks_like_people_matching(question) -> bool:
        """「看视频认人」类题：下拉填空、选项是照片编号（单字母）、要求里说到人。

        实测（视听说3 · Watching street interviews · Exercise 4）：8 个下拉、
        选项 A–E 全是人脸照片，答案取决于「视频里谁在说那句话」——音频转写只给出
        台词、给不出人，必须看画面（走 `_answer_people_by_video`）。
        选项是词/短语的下拉题（普通语法填空）不在此列。
        """
        q_type = getattr(question, "q_type", None)
        if getattr(q_type, "name", "") != "DROPDOWN_SELECT":
            return False
        blanks = list(getattr(question, "banked_blanks", None) or [])
        if not blanks:
            return False
        letters = set()
        for blank in blanks:
            for option in (blank.get("options") or []):
                text = str(option).strip()
                if len(text) > 2:
                    return False          # 选项是词/短语 → 普通下拉题，不是选人
                if text:
                    letters.add(text.upper())
        if letters and not letters <= set("ABCDEFGH"):
            return False
        # 选项抓不到（认人页的选择项藏在「点击才渲染」的下拉菜单里，实测
        # banked_options 是空的）也照样认：靠题目要求里有没有说「人」来判断。
        text = f"{getattr(question, 'directions', '') or ''} " \
               f"{getattr(question, 'text', '') or ''}".lower()
        return any(word in text for word in ("people", "person", "人物", "谁"))

    #: 常见人物的中英文对照（模型可能用中文答，而页面是英文）
    #: 中文人名 → 英文写法（模型可能用中文答，而页面是英文）
    NAME_ALIASES = {
        "孔子": "confucius", "confucius": "confucius",
        "张骞": "zhang qian", "zhang qian": "zhang qian",
        "郑和": "zheng he", "zheng he": "zheng he",
        "李时珍": "li shizhen", "li shizhen": "li shizhen",
        "屈原": "qu yuan", "qu yuan": "qu yuan",
        "司马迁": "sima qian", "sima qian": "sima qian",
        "蔡伦": "cai lun", "cai lun": "cai lun",
        "张衡": "zhang heng", "zhang heng": "zhang heng",
        "华佗": "hua tuo", "hua tuo": "hua tuo",
        "杜甫": "du fu", "du fu": "du fu",
        "李白": "li bai", "li bai": "li bai",
        "玄奘": "xuan zang", "xuan zang": "xuan zang",
    }

    @staticmethod
    def _parse_name_lines(raw: str) -> Dict[str, str]:
        """把模型「A=郑和」这样的逐张辨认结果解析成 {字母: 人名}。"""
        result: Dict[str, str] = {}
        for line in str(raw or "").splitlines():
            match = re.match(r"\s*[*\-\s]*([A-Da-d])\s*[=:：]\s*(.+?)\s*$", line)
            if not match:
                continue
            letter = match.group(1).upper()
            name = re.sub(r"[*_`（(].*$", "", match.group(2)).strip(" 。.,，")
            if name:
                result[letter] = name
        return result

    @classmethod
    def _description_numbers(cls, page_text: str) -> Dict[str, int]:
        """从页面文字里取「编号 → 人名」：1. Confucius (philosopher …) → {confucius: 1}。"""
        mapping: Dict[str, int] = {}
        for line in str(page_text or "").splitlines():
            match = re.match(r"\s*(\d{1,2})\s*[.、)]\s*([A-Za-z][A-Za-z\s'\-]{2,40})", line)
            if not match:
                continue
            number = int(match.group(1))
            name = match.group(2).strip()
            mapping[name.lower()] = number
        return mapping

    def _name_to_number(self, name: str, numbers: Dict[str, int]) -> int:
        """把模型答的人名（可能是中文）映射到描述编号。"""
        clean = re.sub(r"[^A-Za-z\u4e00-\u9fff\s]", "", str(name or "")).strip().lower()
        if not clean:
            return 0
        for candidate, number in numbers.items():
            if clean in candidate or candidate in clean:
                return number
        alias = self.NAME_ALIASES.get(clean)
        if alias:
            for candidate, number in numbers.items():
                if alias in candidate or candidate in alias:
                    return number
        # 仍匹配不上时，逐个「英文名 → 中文名」再试一次（双向兜底）
        for zh, en in self.NAME_ALIASES.items():
            if en == clean:
                for candidate, number in numbers.items():
                    if zh in candidate:
                        return number
        return 0

    def _vision_pair_answer(self, question) -> str:
        """配对题：逐张认人（两次不同模型）→ 名字映射编号 → 得到配对。

        以前一次性问「左边 1-4 配右边 A-D」，模型既要认人又要做排列，容易错且自相矛盾。
        现在拆开：认人交给视觉模型（并让两个模型投票），排列由程序算 —— 就稳得多。
        """
        options = list(getattr(question, "options", None) or [])
        shots = []
        for opt in options:
            element = getattr(opt, "element", None)
            if element is None:
                continue
            data = ""
            try:
                data = element.screenshot_as_base64 or ""
            except Exception:
                data = ""
            if not data:
                try:
                    data = element.find_element(By.TAG_NAME, 'img').screenshot_as_base64 or ""
                except Exception:
                    data = ""
            if data:
                shots.append((opt.letter, data))
        if len(shots) < 2:
            print("    ⚠ 配对题没截到足够的图片，交给文本模型")
            return ""

        letters = [letter for letter, _ in shots]
        page_text = self._page_visible_text(limit=1200)
        numbers = self._description_numbers(page_text)
        print(f"    👁 题目编号↔人名: {numbers}")

        element = getattr(question, "element", None)
        full_shot = ""
        if element is not None:
            try:
                full_shot = element.screenshot_as_base64 or ""
            except Exception:
                full_shot = ""

        prompt = (
            "这是一道「人物描述 与 画像 配对」的题：左边是带编号的人物描述，右边是要拖动的画像。\n"
            f"【页面文字（含编号描述）】\n{page_text}\n\n"
            "【只做一件事】逐张判断每张画像画的是谁，然后**每张一行**输出「字母=人名」。\n"
            "· 人名只能从上面描述里出现的人物中选，用英文写法，例如 Confucius / Zhang Qian / Zheng He / Li Shizhen；\n"
            "· 参考特征：孔子=年长白须、儒巾、朴素深色袍；张骞=汉代官服、汉式冠、持符节；"
            "郑和=明代宦官、华丽金色团花袍、明代官帽；李时珍=年长医者、方巾、常与药草同现。\n"
            f"【输出格式】下面依次是画像 {'、'.join(letters)}，请严格按这个顺序每张一行：\n"
            "A=人名\nB=人名\nC=人名\nD=人名\n"
            "不要解释、不要多余文字。"
        )
        content = [{"type": "text", "text": prompt}]
        if full_shot:
            content.append({"type": "text", "text": "整个题目区域："})
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{full_shot}"}})
        for letter, data in shots:
            content.append({"type": "text", "text": f"画像 {letter}："})
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{data}"}})

        model1 = getattr(self.config, "vision_model", "") or "qwen3.8-omni-flash"
        model2 = getattr(self.config, "vision_model_2", "") or ""
        answer1, _ = self._vision_chat(model1, content)
        names1 = self._parse_name_lines(answer1)
        print(f"    👁 {model1} 逐张辨认: {names1 or answer1[:60]!r}")

        names2: Dict[str, str] = {}
        if model2 and model2 != model1:
            answer2, _ = self._vision_chat(model2, content)
            names2 = self._parse_name_lines(answer2)
            print(f"    👁 {model2} 逐张辨认: {names2 or answer2[:60]!r}")

        if not names1 and not names2:
            print("    ⚠ 两个视觉模型都没给出可解析的逐张辨认结果")
            return ""

        order: List[str] = [""] * len(shots)
        detail = []
        for index, letter in enumerate(letters):
            name = names1.get(letter, "")
            other = names2.get(letter, "")
            if name and other and name.lower() != other.lower():
                print(f"    ⚠ 画像 {letter} 两个模型不一致（{name} vs {other}），采用 {name}")
            elif not name and other:
                name = other
            number = self._name_to_number(name, numbers)
            detail.append(f"{letter}={name}→{number or '?'}")
            if number and 1 <= number <= len(order) and not order[number - 1]:
                order[number - 1] = letter

        print(f"    👁 逐张对应: {'，'.join(detail)}")
        if any(not item for item in order):
            print("    ⚠ 有人物没映射到描述编号，配对不完整")
            return ""
        pairing = " ".join(f"{index + 1}-{letter}" for index, letter in enumerate(order))
        print(f"    👁 视觉模型配对结果: {pairing!r}")
        return pairing

    def _answer_people_by_video(self, question, directions: str = "") -> str:
        """看视频认人：台词分段 → 全片网格抽帧 → 视觉模型「这一帧是哪张照片」→
        本地把「题目句子 ↔ 台词段落」对上 → 组装成「N.字母」答案文本。

        分工和 `_vision_pair_answer` 一致：认人交给视觉模型（一次只判断一帧），
        排列由程序算（本地词重合度）。任何一步拿不准都返回空串 —— 交回 AI，
        绝不把「可能是别人」的字母填进去。
        """
        import video_faces

        blanks = list(getattr(question, "banked_blanks", None) or [])
        if not blanks:
            return ""

        # ① 视频源
        handler = getattr(self, "video_handler", None)
        video_info = None
        if handler is not None:
            try:
                video_info = handler._get_video_info()
            except Exception as exc:
                print(f"    ⚠ 读取视频元素失败：{str(exc)[:60]}")
        if not video_info or not video_info.get("url"):
            print("    ⚠ 认人页没找到视频源，交回 AI")
            return ""
        video_url = video_info["url"]

        # ② 台词分段（带时间戳）
        try:
            segments = handler.transcriber.transcribe_segments(
                video_url, language="en",
                initial_prompt=page_transcription_hint(self.driver))
        except Exception as exc:
            print(f"    ⚠ 分段转写失败：{str(exc)[:60]}")
            segments = []
        if len(segments) < 2:
            print("    ⚠ 台词分段太少，认不准，交回 AI")
            return ""

        letters = sorted({str(o).strip().upper()
                          for blank in blanks for o in (blank.get("options") or [])
                          if str(o).strip()})
        # 选项抓不到时（认人页常这样）：照片就是选项、按容器顺序 A、B、C…
        shots = self._people_option_shots(question, 8)
        if not letters:
            letters = [chr(65 + i) for i in range(len(shots))]
        if len(letters) < 2 or len(shots) < len(letters):
            print(f"    ⚠ 认人页只截到 {len(shots)} 张照片（选项 {len(letters)} 个），交回 AI")
            return ""

        # ③ 抽帧：固定网格铺满全片（不按段落 —— Whisper 漏段时会整段没画面）
        duration = float(video_info.get("duration") or 0) or \
            (segments[-1][1] if segments else 0.0)
        moments = video_faces.grid_times(duration)
        if len(moments) < 3:
            print("    ⚠ 视频时长异常，认不准，交回 AI")
            return ""
        frame_dir = tempfile.mkdtemp(prefix="faces_")
        video_path = video_faces.download_media(video_url)
        frames = []
        try:
            if not video_path:
                return ""
            frames = video_faces.extract_frames(video_path, moments, frame_dir)
            if len(frames) < 3:
                print("    ⚠ 抽到的画面帧太少，交回 AI")
                return ""

            # ⑤ 视觉模型：只判断「这一帧是哪张照片」
            statements = [str(b.get("context") or "").strip() for b in blanks]
            prompt_parts = [
                "这是「看视频认人」题：照片是选项人物，画面帧来自视频、按时间排列，"
                "每帧后面附了该时刻听到的台词。",
                f"题目要求：{directions or getattr(question, 'text', '')}",
                "【题目句子】" + "；".join(f"{i + 1}. {s}" for i, s in enumerate(statements) if s),
                "【照片】依次是 " + "、".join(letters),
                "【画面帧】",
            ]
            for index, (moment, _path) in enumerate(frames, 1):
                near = video_faces.text_near(segments, moment)
                prompt_parts.append(
                    f"第 {index} 帧（t={moment:.1f}s，台词：{near or '（这段没有识别出台词）'}）")
            prompt_parts.append(
                "只做一件事：逐帧判断画面里的人像哪张照片，输出「帧号=字母」，"
                "每行一条（例如 1=B）。认不出写 1=?。不要解释、不要多余文字。")
            content = [{"type": "text", "text": "\n".join(prompt_parts)}]
            for letter, data in shots:
                content.append({"type": "text", "text": f"照片 {letter}："})
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{data}"}})
            for index, (_moment, path) in enumerate(frames, 1):
                data = self._read_image_b64(path)
                if not data:
                    continue
                content.append({"type": "text", "text": f"第 {index} 帧画面："})
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{data}"}})

            model1 = getattr(self.config, "vision_model", "") or "qwen3.8-omni-flash"
            model2 = getattr(self.config, "vision_model2", "") or \
                getattr(self.config, "vision_model_2", "") or ""
            answer, _ = self._vision_chat(model1, content)
            frame_letters = video_faces.parse_letter_lines(answer, letters)
            print(f"    👁 {model1} 逐帧认人: {frame_letters or str(answer)[:60]!r}")
            if not frame_letters and model2 and model2 != model1:
                answer, _ = self._vision_chat(model2, content)
                frame_letters = video_faces.parse_letter_lines(answer, letters)
                print(f"    👁 {model2} 逐帧认人: {frame_letters or str(answer)[:60]!r}")
            if not frame_letters:
                print("    ⚠ 视觉模型没给出可解析的逐帧结果，交回 AI")
                return ""

            # ⑥ 本地配对：句子 ↔ 台词段落 → 该时刻画面的字母
            results = []
            confirmed = []
            for index, blank in enumerate(blanks, 1):
                statement = str(blank.get("context") or "").strip()
                seg_index = video_faces.match_statement_to_segment(statement, segments)
                letter = None
                if seg_index is not None:
                    start, end, _text = segments[seg_index]
                    # 该段中点落在哪一帧上（网格抽帧 → 取时间最近的帧）
                    middle = (start + end) / 2.0
                    nearest = min(range(len(frames)),
                                  key=lambda i: abs(frames[i][0] - middle)) \
                        if frames else None
                    if nearest is not None:
                        letter = frame_letters.get(nearest + 1)
                if letter:
                    results.append(f"{index}.{letter}")
                    confirmed.append((index, letter))
                else:
                    results.append(f"{index}.?")
                    print(f"    ⚠ 第 {index} 句「{statement[:36]}」没法从画面认出人")
            if len(confirmed) == len(blanks):
                print("    👁 视频认人配对: " + " ".join(results))
                return "选词/选择填空: " + " ".join(results)

            # ⑥.5 认不全：只把「确认无误」的逐题事实交给 AI 补齐。
            # 画面切到旁人（采访者/B-roll）的帧一律不交 —— 实测那样会把 AI 带偏。
            self._face_evidence = self._build_face_evidence(
                confirmed, len(blanks), letters)
            if self._face_evidence:
                print("    👁 画面确认了部分题 → 事实并入 AI 提示词，其余由它推断")
            return ""
        finally:
            video_faces.cleanup_frames(frames)
            import shutil
            shutil.rmtree(frame_dir, ignore_errors=True)
            if video_path:
                try:
                    os.unlink(video_path)
                except OSError:
                    pass

    @staticmethod
    def _build_face_evidence(confirmed, total: int, letters) -> str:
        """把「已确认」的逐题认人结果拼成给 AI 的短证据（只列确认过的题）。

        实测视频中途会切到不在选项里的旁人/采访者，把那些帧当成「谁在说」会把
        AI 带偏 —— 所以只交出「台词与题目对得上、且画面认得出照片」的那几题；
        剩下的题让模型按转写内容与「同一人常成对回答擅长/不擅长」的规律推断。
        """
        if not confirmed:
            return ""
        lines = ["【视频画面已确认（程序看视频逐帧比对照片，只列确认无误的）】"]
        for number, letter in confirmed:
            lines.append(f"第 {number} 题：画面里是照片 {letter}")
        missing = [str(i) for i in range(1, total + 1)
                   if i not in {number for number, _letter in confirmed}]
        if missing:
            lines.append(f"（第 {'、'.join(missing)} 题画面没能确认。）")
        lines.append(
            "【用法】已确认的题直接采用；没确认的题按转写内容推断，"
            "并利用「同一位受访者常同时回答擅长与不擅长的问题」这一规律"
            f"（例如把「家电/机器」与「汽车」归到同一个人）。照片只有 "
            f"{'、'.join(letters)} 这 {len(letters)} 位。")
        return "\n".join(lines)

    @staticmethod
    def _read_image_b64(path: str) -> str:
        try:
            import base64
            with open(path, "rb") as handle:
                return base64.b64encode(handle.read()).decode("ascii")
        except Exception:
            return ""

    def _people_option_shots(self, question, count: int):
        """截取题目容器里的选项人物照（按 DOM 顺序 = A、B、C…）。

        实测这页的照片是容器内 ~80px 的人脸图（没有可挂的选项元素）。页面上
        人像排成两行：「擅长」部分 A–E 一行、「不擅长」部分 A–C 又一行（同一批人
        的重复照片）—— 只取**第一行**，否则字母会一路排到 H。
        """
        element = getattr(question, "element", None)
        if element is None:
            return []
        try:
            images = element.find_elements(By.TAG_NAME, 'img')
        except Exception:
            return []

        def collect(first_row_only: bool):
            shots = []
            seen = set()
            row_y = None
            for image in images:
                try:
                    src = (image.get_attribute('src') or '').strip()
                    size = image.size or {}
                    location = image.location or {}
                    width = int(size.get('width') or 0)
                    height = int(size.get('height') or 0)
                    if not src.startswith('http') or src in seen:
                        continue
                    if width < 40 or height < 40:
                        continue
                    y = int(location.get('y') or 0)
                    if first_row_only:
                        if row_y is None:
                            row_y = y
                        elif abs(y - row_y) > max(12, height // 2):
                            break        # 换行了：第二行是同一批人的重复照片
                    data = image.screenshot_as_base64 or ""
                except Exception:
                    continue
                if not data:
                    continue
                seen.add(src)
                shots.append((chr(65 + len(shots)), data))
                if len(shots) >= count:
                    break
            return shots

        # 认人页有两种排版：Exercise 4 那样「一排人像 + 第二排重复」（只取第一排），
        # 以及 Exercise 3 那样「每行一个人像竖着排」（取全部）。先按第一排取，
        # 不足两张就退回取全部 —— 实测只认第一排会把竖排页面抓成 1 张、整条链路放弃。
        shots = collect(first_row_only=True)
        if len(shots) < 2:
            shots = collect(first_row_only=False)
        return shots

    def _vision_chat(self, model: str, content, attempts=(8000, 16000)):
        """向视觉模型要答案，并处理「返回空内容」。

        推理模型（qwen / deepseek 的 omni、pro 系列）在小 max_tokens 下会把额度
        花在思考上，content 变成空串。所以默认给大额度，空了加倍再试，并把
        finish_reason 带回去方便判断。
        """
        last_finish = ""
        for index, budget in enumerate(attempts, 1):
            try:
                response = self.ai_client.client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": content}],
                    max_tokens=budget,
                )
            except Exception as exc:
                print(f"    ⚠ 视觉模型 {model} 调用失败（max_tokens={budget}）: {str(exc)[:110]}")
                return "", "error"
            choice = response.choices[0]
            last_finish = str(getattr(choice, "finish_reason", "") or "")
            answer = (choice.message.content or "").strip()
            if answer:
                return answer, last_finish
            reasoning = str(getattr(choice.message, "reasoning_content", "") or "")
            print(f"    ⚠ 视觉模型 {model} 返回空（第 {index} 次，max_tokens={budget}，"
                  f"finish={last_finish}，思考 {len(reasoning)} 字）")
            if reasoning and index == len(attempts):
                return reasoning.strip(), last_finish
        return "", last_finish

    def _vision_read_answer(self, question, directions: str = "") -> str:
        """题干读不出来（空、太短、整块是图片）时：截图交给视觉模型读题并作答。

        实测（2026-09）：deepseek-v4-pro 收图，配置里的 vision_model 指向它。
        """
        element = getattr(question, "element", None)
        shot = ""
        if element is not None:
            try:
                shot = element.screenshot_as_base64 or ""
            except Exception:
                shot = ""
        if not shot:
            try:
                shot = self.driver.get_screenshot_as_base64()
            except Exception:
                return ""
        if not shot:
            return ""

        options = getattr(question, "options", None) or []
        options_text = "\n".join(f"{opt.letter}. {opt.text}" for opt in options)
        blank_count = len(getattr(question, "inputs", None) or [])
        if options_text:
            want = "请从选项中选择，只回一个选项字母（如 A）。"
        elif blank_count > 1:
            want = f"题目共 {blank_count} 个空，请按顺序给出答案，格式：1.答案 2.答案 …"
        else:
            want = "请给出可直接填写的答案；题目是英文就用英文作答，不要解释。"

        prompt = (
            "下面这张截图是 U校园 上的一道题（网页里的文字抓不到，只能靠你看图）。\n"
            f"【题目要求】{directions or '（无）'}\n"
            f"【已知选项文字】\n{options_text or '（无）'}\n"
            f"【任务】先看清题干与空格位置，再作答。{want}\n"
            "只回答案本身，不要复述题目、不要解释。"
        )
        model = getattr(self.config, "vision_model", "") or "deepseek-v4-pro"
        try:
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{shot}"}},
            ]
            answer, finish = self._vision_chat(model, content)
            print(f"    👁 视觉模型（{model}）读题作答: {answer[:70]!r}（finish={finish}）")
            return answer
        except Exception as exc:
            print(f"    ⚠ 视觉读题失败: {str(exc)[:100]}")
            return ""

    def _pair_ai_answers(self, questions: List[Question], ai_response: str) -> List[Tuple[Question, str]]:
        """把 AI 的整体回复拆成 (题目, 答案文本) 对。

        选择题按题号取字母，其余题型照旧把整段回复交给执行器自行按题号解析。
        """
        pairs: List[Tuple[Question, str]] = []
        if looks_like_ai_refusal(ai_response):
            print(f"    ⚠ AI 没给出答案（疑似拒答）：{ai_response.strip()[:80]}")
            self._answer_incomplete = True
            return [(q, "") for q in questions]
        for q in questions:
            if q.q_type in [QuestionType.SINGLE_CHOICE, QuestionType.LISTENING_CHOICE,
                            QuestionType.VIDEO_CHOICE, QuestionType.MULTIPLE_CHOICE,
                            QuestionType.VOCABULARY_TEST]:
                ans = self._extract_single_answer(
                    ai_response, q.number,
                    allow_separated=(q.q_type == QuestionType.MULTIPLE_CHOICE),
                    allowed_letters={o.letter.strip().upper()
                                     for o in (getattr(q, "options", None) or [])
                                     if getattr(o, "letter", "")})
            elif q.q_type == QuestionType.SORTING:
                # 配对/排序题：选项是图片（看不到文字线索）时，直接让视觉模型看图配对；
                # 拿不准就留空不提交，绝不瞎拖一气。
                answer_source = ai_response
                # 配对题（Match the names … with the pictures）必须看图：
                # 只要题目要求是配对，或选项里疑似有图，就走视觉链
                if self._looks_like_matching(q) or self._has_image_options(q):
                    vision = self._vision_pair_answer(q)
                    if vision:
                        answer_source = vision
                        print(f"    👁 已用视觉链的结果作答: {vision[:40]!r}")
                if any(word in answer_source for word in ("不确定", "无法确定", "无法判断")):
                    print("    ⚠ 排序/配对题模型拿不准，本题留空（不提交）")
                    self._answer_incomplete = True
                    ans = ""
                else:
                    ans = answer_source
            else:
                ans = ai_response
            pairs.append((q, ans))
        return pairs

    def _apply_answers(self, pairs: List[Tuple[Question, str]]) -> Tuple[int, bool]:
        """逐题填写，返回 (成功题数, 是否被停止)。

        填成功的答案会记进 self._last_applied —— 收割答案时用它，
        比回头去页面输入框里读可靠得多（题库点选项填的那种根本读不回来）。
        """
        success_count = 0
        if not hasattr(self, "_last_applied"):
            self._last_applied = []
        for q, ans in pairs:
            if self._should_stop():
                return success_count, True
            if ans:
                if self.executor.execute(q, ans):
                    success_count += 1
                    number = getattr(q, "number", 0)
                    # 同一题被填两次时（知识库先填、AI 复核再填）只留最后一次：
                    # 收割按 _last_applied 写库，两次都留会把答案翻倍收录
                    # （实测 5 空的题收成 10 条，头一条还是题目要求的渣文本）
                    self._last_applied = [
                        (n, a) for n, a in self._last_applied if n != number]
                    self._last_applied.append((number, ans))
            else:
                print(f"    题目 {q.number} 无答案")
        return success_count, False

    def _extract_single_answer(self, ai_response: str, question_number: int,
                               allow_separated: bool = False,
                               allowed_letters=None) -> str:
        # 字母容忍到选项实际范围（实测有 10 个选项到 J；以前只认 A-H，
        # AI 明明答对了 ACDGJ 却被上限丢成空答案）
        # 模型爱用 markdown 标答案（**B**、`B`、__B__）：修饰符留着会把纯字母串
        # 判成散文 → 提取失败 → 「题目无答案」。星号/反引号/双下划线先去掉。
        ai_response = re.sub(r"[*`]|__", "", ai_response or "")
        if allow_separated and allowed_letters:
            # 多选题：字母白名单 = 页面实际选项的字母。整段必须是
            # 「选项字母+分隔符」的形状才采用 —— 散文里混进白名单外的字母
            # （如 the 的 t）会被拒绝，交给补做，绝不把单词拆成选项
            pattern = rf'{question_number}\s*[.、\)\]]\s*([^\n]*)'
            match = re.search(pattern, ai_response, re.IGNORECASE)
            if match:
                cleaned = re.sub(r'\bAND\b|\b&\b', ' ', match.group(1).upper())
                if re.fullmatch(r'[A-Z\s,、;；和及与]*', cleaned) and all(
                        ch in allowed_letters for ch in cleaned if ch.isalpha()):
                    letters = [ch for ch in cleaned if ch in allowed_letters]
                    seen: set = set()
                    deduped = [c for c in letters if not (c in seen or seen.add(c))]
                    if deduped:
                        return "".join(deduped)
        else:
            pattern = rf'{question_number}\s*[.、\)\]]\s*([A-Z]+)'
            match = re.search(pattern, ai_response, re.IGNORECASE)
            if match:
                return match.group(1).upper()

        lines = [l.strip() for l in ai_response.split('\n') if l.strip()]
        if question_number <= len(lines):
            line = lines[question_number - 1].upper()
            # 只接受整行是纯字母串（如 "B" / "BCD"）或行首带题号的字母串；
            # 以前会把整行英文单词拆成字母（Answer→ANSWER）当多选答案填进页面。
            if re.fullmatch(r'[A-L]{1,10}', line):
                return line
            if allow_separated and re.fullmatch(r'[A-L](?:[\s,、和及与]+[A-L]){0,9}', line):
                return re.sub(r'[^A-L]', '', line)
            prefix = re.match(r'([A-L]{1,10})\s*[.、\)\]:：]', line)
            if prefix:
                return prefix.group(1)

        # 带标签的作答（实测：「多选题：BCD」被丢成「题目无答案」，用户截图实证）。
        # 只认「冒号后整段 = 白名单字母 + 分隔符」这一种形状；散文里带冒号
        # （「答案：The correct ones are B and D.」）绝不拆字母。
        if allowed_letters:
            for line in lines:
                parts = re.split(r"[:：]", line)
                if len(parts) < 2:
                    continue
                tail = parts[-1].strip()
                if tail and re.fullmatch(r"[A-Za-z][A-Za-z\s,，、;；/|和及与]*", tail):
                    letters = [ch.upper() for ch in tail if ch.isalpha()]
                    if letters and all(ch in allowed_letters for ch in letters):
                        seen: set = set()
                        return "".join(c for c in letters if not (c in seen or seen.add(c)))

        return ""

    def _generate_questions_signature(self, questions: List[Question]) -> str:
        if not questions:
            return "empty"

        parts = []
        for q in questions:
            q_type = q.q_type.name if q.q_type else "UNKNOWN"
            text_preview = q.text[:30] if q.text else ""
            option_preview = "|".join(opt.text[:30] for opt in q.options[:4])
            parts.append(f"{q.number}:{q_type}:{text_preview}:{option_preview}")

        return hashlib.md5("|".join(parts).encode()).hexdigest()[:16]

    def solve_current_page(self, chapter_name: str = "unknown") -> bool:
        print("\n" + "=" * 60)
        print(" 开始处理当前停留的页面")
        print("=" * 60)

        # 收录进题库的小节名要能在下次命中：以前用带时间戳的一次性名字
        # （quick_current_page_1790962208），下次做同一页永远匹配不上。
        # 改用页面「题目要求」做任务名（同一页每次都一样），匹配器按任务名就能
        # 找回上次收录的答案；拿不到再退回页面标题、最后才是时间戳名。
        quick_tab_name = ""
        try:
            parser = getattr(self, "parser", None)
            directions_text = re.sub(
                r"\s+", " ", ((parser._extract_directions_from_page() if parser else "") or "").strip())
        except Exception:
            directions_text = ""
        if directions_text:
            quick_tab_name = directions_text[:60]
        if not quick_tab_name:
            try:
                quick_tab_name = re.sub(r"\s+", " ", (self.driver.title or "").strip())[:60]
            except Exception:
                quick_tab_name = ""
        if not quick_tab_name:
            quick_tab_name = f"quick_current_page_{int(time.time())}"
        print(f"    任务名（收录与检索共用）: {quick_tab_name}")
        self.ai_client.force_reset(f"{chapter_name}_{quick_tab_name}")
        # 任务边界（在预处理触发转写之前）：快速处理流的转写上下文从这里算起
        self.ai_client.mark_task_boundary()

        # 快速处理没有扫描阶段，教材在此时才识别（识别不到则本页全部交给 AI）
        self._prepare_knowledge_base()

        state_key = self._generate_content_hash()

        if not state_key or state_key == "empty":
            state_key = f"{chapter_name}_{int(time.time())}"

        print(f"    内容标识: {state_key[:50]}...")

        if state_key in self.processed_hashes:
            print(f"   ⏭ 该页面的哈希已被记录，正在执行作答...")

        self.processed_hashes.add(state_key)

        success = self._process_current_tab_content(
            chapter_name,
            quick_tab_name,
            0,
            0
        )

        print(f"\n{'=' * 60}")
        print(" 当前页面处理已停止" if self._should_stop() else " 当前页面处理完毕")
        print(f"{'=' * 60}")
        return success

    #: 「下一题」按钮的文字（多版本）
    NEXT_TEXTS = ("下一题", "下一页", "下一步", "Next", "next")

    def _find_next_by_text(self):
        """按文字找「下一题」——类名不固定时靠它，避免漏点导致内容不变。"""
        for word in self.NEXT_TEXTS:
            xpaths = (f"//button[normalize-space(.)='{word}']",
                      f"//*[normalize-space(text())='{word}']",
                      f"//button[contains(., '{word}')]")
            for xpath in xpaths:
                try:
                    elements = self.driver.find_elements(By.XPATH, xpath)
                except Exception:
                    continue
                try:
                    candidates = list(elements)[:3]
                except TypeError:
                    continue
                for element in candidates:
                    try:
                        if element.is_displayed() and element.is_enabled():
                            print(f"    [翻页] 按文字找到「{word}」")
                            return element
                    except Exception:
                        continue
        return None

    def _find_next_question_button(self) -> Optional[Any]:
        selectors = [
            '.next-question-btn:not(.disabled)',
            '.btn-next:not([disabled])',
            'button.next:not(.disabled)',
            '.pagination-next:not(.disabled)',
            '.question-next:not(.disabled)',
            '.action.next:not(.disabled)',
            '.submit-bar-pc--btn-next:not(.disabled)',
        '.next-btn:not(.disabled)',
            '[class*="next"]:not(.disabled)',
            '.question-common-course-page a.btn',
            '.question-common-course-page .btn',
            'a.btn',
            'a[class*="btn"]',
        ]

        for selector in selectors:
            try:
                btns = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for btn in btns:
                    if btn.is_displayed() and btn.is_enabled():
                        text = btn.text.lower()
                        aria_label = (btn.get_attribute('aria-label') or '').lower()
                        if any(k in text or k in aria_label for k in ['下一题', 'next', '下一页', 'next question']):
                            return btn
            except Exception as e:
                error_msg = str(e)
                print(f"操作失败: {error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                continue

        try:
            all_btns = self.driver.find_elements(By.CSS_SELECTOR, 'button, a')
            for btn in all_btns:
                if not btn.is_displayed():
                    continue
                text = btn.text.lower()
                if any(k in text for k in ['下一题', 'next question', '下一页', 'next']):
                    if 'submit' not in text and '提交' not in text:
                        return btn
        except Exception:
            pass

        return None

    def _generate_content_hash_from_direction(self) -> str:
        try:
            direction_elem = WebDriverHelper.safe_find_element(
                self.driver,
                ['.abs-direction', '.layout-direction-container', '.direction-container']
            )
            if direction_elem:
                text = direction_elem.text.strip()
                if text:
                    return hashlib.md5(text.encode()).hexdigest()[:16]
        except Exception:
            pass
        return "empty"

    def _generate_content_hash(self) -> str:
        try:
            direction_elem = WebDriverHelper.safe_find_element(
                self.driver,
                ['.abs-direction', '.layout-direction-container', '.discussion-title']
            )
            if direction_elem:
                text = direction_elem.text.strip()
                if text:
                    return hashlib.md5(text.encode()).hexdigest()[:16]

            questions, _ = self.parser.parse_all()
            if questions:
                content = "|".join([f"{q.number}:{q.text[:30]}" for q in questions[:3]])
                return hashlib.md5(content.encode()).hexdigest()[:16]

            body = self.driver.find_element(By.TAG_NAME, 'body').text[:300]
            return hashlib.md5(body.encode()).hexdigest()[:16]

        except Exception as e:
            error_msg = str(e)
            print(f"    生成哈希失败:{error_msg[:50]} ")
            logger.error(f"详细错误: {error_msg}", exc_info=True)
            return "empty"

    def _get_level1_tabs(self) -> List[Dict]:
        return self._collect_level_tabs(Selectors.LEVEL1_TABS, use_title_attr=True)

    def _get_level2_tabs(self) -> List[Dict]:
        container = WebDriverHelper.safe_find_element(
            self.driver,
            ['.pc-header-tasks-container', '.pc-header-tasks-layout']
        )
        if not container:
            return []
        return self._collect_level_tabs(Selectors.LEVEL2_TABS, parent=container)

    def _collect_level_tabs(self, selectors: List[str], parent=None,
                            use_title_attr: bool = False) -> List[Dict]:
        """收集一层 Tab（一级/二级共用）：去重、过滤过长标题，同时给出元素与激活状态。"""
        tabs = []
        elements = WebDriverHelper.safe_find_elements(self.driver, selectors, parent=parent)
        seen = set()
        for elem in elements:
            try:
                if use_title_attr:
                    title = elem.get_attribute('title') or elem.text.strip().split('\n')[0]
                else:
                    title = elem.text.strip().split('\n')[0]
                if title and title not in seen and len(title) < 50:
                    seen.add(title)
                    tabs.append({
                        'element': elem,
                        'title': title,
                        'is_active': 'activity' in (elem.get_attribute('class') or '').lower()
                    })
            except Exception:
                continue
        return tabs

    def _extract_passage(self) -> str:
        """取页面上的阅读材料/原文。

        以前只认四个类名、还要求超过 200 字，稍微换个版面就一条都取不到，
        大模型只好回「题目中未提供完整文章」。这里放宽到常见的材料容器，
        并保留最长的一段 —— 宁可多给，也别让大模型空着手答题。
        """
        selectors = [
            '.question-common-abs-material',
            '.text-material-wrapper',
            '.reading-passage',
            '.passage-content',
            '.layout-material-container',
            '.material-content',
            '.question-material',
            '[class*="material-container"]',
            '[class*="passage"]',
            '[class*="article"]',
        ]

        best = ""
        for selector in selectors:
            try:
                elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for elem in elems:
                try:
                    if elem.find_elements(By.TAG_NAME, 'video'):
                        continue
                    text = re.sub(r"\n{3,}", "\n\n", (elem.text or "").strip())
                except Exception:
                    continue
                if len(text) > len(best):
                    best = text
                if len(best) > 8000:
                    return best[:8000]
        return best if len(best) >= 80 else ""

    def _page_visible_text(self, limit: int = 6000) -> str:
        """整页可见文字：大模型说「没给文章」时，用它兜底补料。"""
        return page_visible_text(self.driver, limit)

    def _handle_confirm_dialog(self):
        try:
            buttons = self.driver.find_elements(By.TAG_NAME, 'button')
            for btn in buttons:
                text = btn.text.strip()
                if self._should_stop():
                    return False
                if any(k in text for k in ['确认', '确定', '我知道了', '继续', 'OK']):
                    if btn.is_displayed():
                        btn.click()
                        if self.stop_requested.wait(1):
                            return False
                        return True
        except Exception:
            pass
        return False


    def _preprocess_audio_if_needed(self, tab_name: str, l1_idx: int, l2_idx: int):
        """检测并预处理音频：下载+转录，将转录文本注入 AI 上下文"""
        if self._should_stop():
            return
        if self._has_video_on_page():
            return

        if not self._has_audio_on_page():
            return

        try:
            audio_url = self._extract_audio_url_from_page()
            if not audio_url:
                print("   未找到有效音频URL，跳过")
                return

            audio_key = audio_url.split('#')[0]
            if audio_key in self._processed_audio_tabs:
                return

            print("   检测到音频，开始预处理（下载+转录）...")
            transcript = self.video_handler.transcriber.transcribe(
                audio_url, language="en", initial_prompt=page_transcription_hint(self.driver))

            if self._should_stop():
                return
            if transcript:
                self.ai_client.add_audio_transcript_if_new(transcript)
                print(f"   已将音频转录（{len(transcript)}字符）加入上下文")
            else:
                print("   音频转录为空")

            self._processed_audio_tabs.add(audio_key)

        except Exception as e:
            print(f"   预处理失败: {str(e)[:80]}")
            logger.error(f"音频预处理异常: {e}", exc_info=True)

    def _has_audio_on_page(self) -> bool:
        """检测页面是否包含音频元素或音频材料"""
        if self._media_urls_from_browser():
            return True
        try:
            audios = self.driver.find_elements(By.TAG_NAME, 'audio')
            if any(a.is_displayed() or a.get_attribute('src') for a in audios):
                return True

            audio_containers = self.driver.find_elements(By.CSS_SELECTOR, '.audio-material-wrapper, .question-audio')
            if audio_containers:
                return True

            try:
                direction = self.driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
                text = direction.text.lower()
                if any(kw in text for kw in ['listen', 'audio', 'hear', 'talk', 'conversation']):
                    if self._extract_audio_url_from_page():
                        return True
            except Exception:
                pass

            return False
        except Exception:
            return False

    MEDIA_URL_JS = r"""
    (function () {
      var urls = [];
      // 1) 直接的 audio/video 标签
      document.querySelectorAll('audio, video, audio source, video source').forEach(function (el) {
        var src = el.src || el.getAttribute('src') || '';
        if (src) { urls.push(src); }
      });
      // 2) 浏览器实际请求过的媒体资源（播放器再花哨也躲不过这里）
      try {
        performance.getEntriesByType('resource').forEach(function (entry) {
          if (/\.(mp3|m4a|wav|aac|ogg|oga|flac|mp4|webm)(\?|#|$)/i.test(entry.name)) {
            urls.push(entry.name);
          }
        });
      } catch (e) {}
      // 3) 页面脚本里的媒体地址
      try {
        var html = document.documentElement.outerHTML;
        var found = html.match(/https?:[^"'\s)]+\.(mp3|m4a|wav|aac|ogg|mp4)(\?[^"'\s)]*)?/ig) || [];
        for (var i = 0; i < found.length; i++) { urls.push(found[i]); }
      } catch (e) {}
      // 去重
      var seen = {};
      var out = [];
      for (var j = 0; j < urls.length; j++) {
        var u = urls[j];
        if (u && !seen[u]) { seen[u] = 1; out.push(u); }
      }
      return out.slice(0, 5);
    })()
    """

    def _media_urls_from_browser(self) -> List[str]:
        """从浏览器里找媒体地址：audio/video 标签 + 实际请求过的资源 + 页面脚本里的地址。"""
        try:
            found = self.driver.execute_script(self.MEDIA_URL_JS)
        except Exception as exc:
            print(f"    [音频] 读取媒体地址失败: {str(exc)[:60]}")
            return []
        urls = [str(u) for u in (found or []) if u]
        if urls:
            print(f"    [音频] 找到媒体地址 {len(urls)} 个: {urls[0][:90]}")
        return urls

    def _extract_audio_url_from_page(self) -> Optional[str]:
        """从页面提取音频URL"""
        media_urls = self._media_urls_from_browser()
        if media_urls:
            return media_urls[0]
        try:
            audio_elem = self.driver.find_element(By.CSS_SELECTOR, 'audio')
            src = audio_elem.get_attribute('src')
            if src:
                return src.split('#')[0]

            sources = self.driver.find_elements(By.CSS_SELECTOR, 'audio source')
            for source in sources:
                src = source.get_attribute('src')
                if src:
                    return src.split('#')[0]

            return None
        except Exception:
            return None

    def _preprocess_video_if_needed(self, tab_name: str, l1_idx: int, l2_idx: int):
        """检测并预处理视频：播放、转录、处理弹窗，将转录文本注入 AI 上下文"""
        if self._should_stop():
            return
        if not self._has_video_on_page():
            return

        try:
            video_info = self.video_handler._get_video_info()
            video_url = (video_info or {}).get('url', '')
            video_key = video_url.split('#')[0] if video_url else f"{tab_name}|{l1_idx}|{l2_idx}"
            if video_key in self._processed_video_tabs:
                return

            print("   检测到视频，开始预处理（播放+转录）...")
            self.video_handler._play_video_and_handle_popups()

            if self._should_stop():
                return
            transcript = self.video_handler.video_transcript
            if transcript:
                self.ai_client.add_video_transcript_if_new(transcript)
                print(f"   已将视频转录（{len(transcript)}字符）加入上下文")
            else:
                print("   未获得视频转录，后续题目可能缺乏上下文")

            self._processed_video_tabs.add(video_key)

        except Exception as e:
            print(f"   预处理失败: {str(e)[:80]}")
            logger.error(f"视频预处理异常: {e}", exc_info=True)

    def _has_video_on_page(self) -> bool:
        """检测当前页面是否包含视频/播放器。

        原来的实现只认"当前可见的 <video>"：Exercise 类页面上的短视频在检查那一刻
        往往不可见（折叠/在上方滚动区/刚切换标签页），于是一整页被当成"没有视频"，
        不做转写 → AI 手里没有台词，只能拿联网结果硬猜（实测 Exercise 3 判 0/1）。
        现在放宽：有 <video> 就算（不看可见性），并额外认播放器容器与视频 iframe。
        """
        try:
            if self.driver.find_elements(By.TAG_NAME, 'video'):
                return True
        except Exception:
            pass
        for selector in ('.video-box', '[class*="video-box"]', '[class*="videoBox"]',
                         '[class*="video-player"]', '[class*="videoPlayer"]',
                         'iframe[src*="video"]', 'iframe[src*="player"]',
                         '[class*="prism-player"]', '[class*="dplayer"]'):
            try:
                if self.driver.find_elements(By.CSS_SELECTOR, selector):
                    return True
            except Exception:
                continue
        return False

    def _wait_for_submit_complete(self, timeout: int = 8):
        """
        等待提交完成。策略：
          1. 等待提交按钮消失
          2. 或等待 '提交成功' / '保存成功' 等提示出现
          3. 最少睡眠 1.5 秒兜底
        """
        if self.stop_requested.wait(1.5):
            return
        start = time.time()

        while time.time() - start < timeout and not self._should_stop():
            try:
                submit_btn = self._find_visible_submit_button()
                if submit_btn is None:
                    print(f"   提交按钮已消失，提交完成")
                    return

                body_text = self.driver.find_element(By.TAG_NAME, 'body').text[:500].lower()
                if any(kw in body_text for kw in ['提交成功', '保存成功', 'success', '已提交', 'submitted']):
                    print(f"   检测到成功提示")
                    return

            except Exception:
                pass

            if self.stop_requested.wait(0.5):
                return

        print(f"   等待超时（{timeout}s），继续执行")

    def _find_visible_submit_button(self) -> Optional[Any]:
        selectors = [
            '.submit-bar-pc--btn-1_Xvo',
            'button.submit-btn',
            'button[type="submit"]',
            '.question-common-course-page a.btn',
            '.question-common-course-page .btn',
            'a.btn',
            '.btn',
        ]

        for selector in selectors:
            try:
                elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for elem in elems:
                    if elem.is_displayed() and AnswerExecutor._is_submit_button(elem):
                        return elem
            except Exception:
                continue

        return None

    def _wait_for_content_change(self, previous_signature: str, timeout: int = 10) -> bool:
        start_time = time.time()
        check_interval = 0.5

        while time.time() - start_time < timeout and not self._should_stop():
            try:
                questions, _ = self.parser.parse_all()
                current_signature = self._generate_questions_signature(questions)

                if current_signature != previous_signature and current_signature != "empty":
                    print(f"         内容已变化: {previous_signature[:8]}... -> {current_signature[:8]}...")
                    return True

            except Exception as e:
                logger.debug(f"等待内容变化时出错: {e}")

            if self.stop_requested.wait(check_interval):
                return False

        print(f"         等待内容变化超时")
        return False


class UCampusBot:
    """U校园机器人 - 组装所有组件"""

    def __init__(self, config_path: str = 'config.json', skip_check: bool = False):
        self.config_path = config_path
        self.config = Config.from_json(config_path)
        self.driver = None

        if not skip_check:
            self._ensure_environment()

        self.driver = self._create_driver()
        self.popup_watcher = PopupWatcher(self.driver)

    def _ensure_environment(self):
        checker = EnvironmentChecker()

        if not checker.check_all():
            while True:
                choice = checker.show_fix_guide()

                if choice == '1':
                    checker.auto_install_edge()
                    sys.exit(0)

                elif choice == '2':
                    if checker.auto_install_ffmpeg():
                        sys.exit(0)

                elif choice == '3':
                    if checker.add_ffmpeg_to_path():
                        sys.exit(0)

                elif choice == '4':
                    ffmpeg_path = checker.manual_specify_path()
                    if ffmpeg_path:
                        bin_dir = os.path.dirname(ffmpeg_path)
                        checker._add_to_system_path(bin_dir)
                        print(f" FFmpeg 已添加到 PATH: {bin_dir}")
                        print("请重新运行程序")
                        input("按回车键退出...")
                        sys.exit(0)

                elif choice == '5':
                    self._show_detailed_help()
                    input("\n按回车键退出...")
                    sys.exit(1)

                elif choice == 'Q':
                    sys.exit(1)

                else:
                    print("无效选项，请重新选择")

    def _show_detailed_help(self):
        print("""
    【问题诊断】

    1. Edge 浏览器问题
       原因：Edge 未安装
       解决：选择 [1] 自动安装，或访问 https://www.microsoft.com/edge

    2. FFmpeg 问题（语音识别必需）
       原因：未安装 FFmpeg 或未添加到系统 PATH
       解决：
          - 方法A（推荐）：选择 [2] 自动下载安装（约130MB）
          - 方法B：选择 [3] 将已安装的 FFmpeg 添加到 PATH
          - 方法C：手动下载 https://ffmpeg.org/download.html
            解压后将 bin 目录添加到系统环境变量 PATH

    3. 验证 FFmpeg 安装
       打开 CMD 输入: ffmpeg -version
       应显示版本信息，如 "ffmpeg version 6.0"

    【手动安装 FFmpeg 步骤】

    1. 访问 https://ffmpeg.org/download.html
    2. 点击 Windows 图标，选择 "Windows builds from gyan.dev"
    3. 下载 "ffmpeg-release-essentials.zip"
    4. 解压到 C:\ffmpeg
    5. 将 C:\ffmpeg\bin 添加到系统环境变量 PATH
    6. 重启终端，输入 ffmpeg -version 验证
    """)

    def _create_driver(self):
        options = webdriver.EdgeOptions()
        options.add_argument('--disable-blink-features=AutomationControlled')
        # 页面要访问 127.0.0.1 的本地音频服务时，Edge 会弹「访问此设备上的其他应用和服务」，
        # 那是浏览器外壳弹窗，Selenium 点不到 —— 用参数关掉这项检查（本地网络访问）
        options.add_argument('--disable-features=LocalNetworkAccessChecks')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        # 录音题要用麦克风：本次会话直接放行，不用人工点"允许"
        options.add_experimental_option("prefs", {
            "profile.default_content_setting_values.media_stream_mic": 1,
            "profile.default_content_setting_values.media_stream_camera": 1,
        })
        options.add_argument('--use-fake-ui-for-media-stream')
        # 注意：绝不能加 --use-fake-device-for-media-stream。
        # 跟读题的录音链路是「真实设备回环」：read_aloud 会先把默认播放切到 VB-Cable、
        # 默认录音切到 CABLE Output，再播放示范音让它绕线缆进入浏览器录音。
        # 该参数会让浏览器改用"假麦克风"、彻底绕开真实设备 —— 录到的全变成假设备噪声，
        # 平台拿不到评分（实测：加上它之后跟读逐条"评分未返回"）。

        # 开一个调试端口：出问题时可以直接连进这个浏览器看真实 DOM
        # （不重新登录、不影响正在跑的会话）。端口可用 config.json 的 debug_port 改。
        try:
            port = int(getattr(self.config, "debug_port", 0) or 0)
        except Exception:
            port = 0
        if port:
            options.add_argument(f'--remote-debugging-port={port}')
            options.add_argument('--remote-allow-origins=*')

        # 本次会话的临时浏览器 profile：退出时在 _cleanup_browser 里删除
        self._user_data_dir = tempfile.mkdtemp(prefix="ucampus_")
        options.add_argument(f'--user-data-dir={self._user_data_dir}')

        driver = webdriver.Edge(options=options)

        # 关掉 AEC/降噪/自动增益 —— 回环设备当麦克风时，音频会被当成回声消掉
        try:
            from audio_constraints import SOURCE as _uc_no_aec
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                                        {"source": _uc_no_aec})
            # 同一份补丁再往「当前页面」注入一次（新文档注入只对之后加载的页面生效），
            # 注入后立刻读回标记自检 —— 上一版没有自检，补丁没生效都不知道（实测
            # patchInstalled=False，AEC 一直开着，示范音被当成回声消掉）。
            try:
                from audio_constraints import SOURCE as _uc_no_aec_now
                self.driver.execute_script(_uc_no_aec_now)
                _ok = self.driver.execute_script(
                    "return !!(navigator.mediaDevices && navigator.mediaDevices.__ucNoAecInstalled);")
                print(f"     AEC 补丁："
                      f"{'已装到当前页面 ✔' if _ok else '注入后仍未生效 ✘（录音可能被判成回声）'}")
            except Exception as _exc:
                print(f"     AEC 补丁注入失败: {str(_exc)[:60]}")
            # 再核一次「关掉 AEC 后实际生效的约束」
            try:
                _sett = self.driver.execute_script(
                    "return navigator.mediaDevices.__ucNoAecInstalled ? 'installed' : 'missing';")
            except Exception:
                _sett = '?'
        except Exception:
            pass
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => false});"
        })

        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": r"""
                (function () {
                  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { return; }
                  var original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                  window.__ucAudioRouting = false;      // 由程序打开：录音时改用示范音频
                  navigator.mediaDevices.getUserMedia = function (constraints) {
                    if (constraints && constraints.audio && window.__ucAudioRouting) {
                      try {
                        var el = document.querySelector('audio, video');
                        if (el) {
                          if (!window.__ucCtx) {
                            window.__ucCtx = new AudioContext();
                            window.__ucSrc = window.__ucCtx.createMediaElementSource(el);
                            window.__ucDest = window.__ucCtx.createMediaStreamDestination();
                            window.__ucSrc.connect(window.__ucDest);
                            window.__ucSrc.connect(window.__ucCtx.destination);  // 同时保留外放
                          }
                          return Promise.resolve(window.__ucDest.stream);
                        }
                      } catch (e) { /* 失败就退回真实麦克风 */ }
                    }
                    return original(constraints);
                  };
                })();
                """
            })
            print("   已注入「示范音频直录」补丁（录音时喂标准读音）")
        except Exception as exc:
            print(f"   注入示范音频补丁失败（不影响其它功能）: {str(exc)[:60]}")

        grant_browser_permission(driver)

        return driver

    def start(self):
        solver = AISolver(self.driver, self.config, config_path=self.config_path)
        # 麦克风只在脚本运行期间放行（退出时恢复原值），平时保持系统默认
        self.mic_permission = MicPermission()
        if getattr(self.config, "mic_auto_grant", True):
            self.mic_permission.enable()
        else:
            gui_log_queue.put(" 麦克风自动放行已关闭（config.json 的 mic_auto_grant=false）")

        gui_log_queue.put(f" 本地题库知识库：{solver.knowledge_base.describe()}")
        if not solver.knowledge_base.enabled:
            gui_log_queue.put(" 知识库已关闭（config.json 的 knowledge_enabled=false），全部题目交给 AI")

        self.gui = FluentModernGUI(self.driver, solver, self, globals())

        threading.Thread(target=self.popup_watcher.run, daemon=True).start()
        threading.Thread(target=self._background_login_flow, daemon=True).start()

        try:
            self.gui.mainloop()
        finally:
            self._cleanup_browser()

        return True

    def _cleanup_browser(self):
        """退出收尾：关掉驱动，删掉本次会话的临时浏览器 profile 目录。"""
        try:
            if getattr(self, "driver", None) is not None:
                self.driver.quit()
        except Exception as exc:
            print(f"   关闭浏览器失败（可忽略）: {str(exc)[:60]}")
        try:
            import shutil
            data_dir = getattr(self, "_user_data_dir", "")
            if data_dir and os.path.isdir(data_dir):
                shutil.rmtree(data_dir, ignore_errors=True)
        except Exception:
            pass

    def _background_login_flow(self):
        gui_log_queue.put(" 正在与 U校园 建立连接，请稍候...")
        success = self._login()
        if success:
            self.gui.after(0, self.gui.enable_scan_button)

    def _login(self) -> bool:
        try:
            self.driver.get(self.config.url)
            time.sleep(3)
            username = WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.XPATH, '//*[@id="username"]')))
            password = self.driver.find_element(By.XPATH, '//*[@id="password"]')
            agreement_check = WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.XPATH, '//*[@id="agreement"]')))
            username.send_keys(self.config.username)
            password.send_keys(self.config.password)
            if not agreement_check.is_selected():
                agreement_check.click()

            login_btn = self.driver.find_element(By.XPATH,
                                                 '//*[@id="rc-tabs-0-panel-1"]/form/div[4]/div/div/div/div/button')
            login_btn.click()

            gui_log_queue.put(" 如果遇到验证码，请在弹出的浏览器中手动进行人机验证。")
            gui_log_queue.put("⏳ 正在智能轮询登录状态...")

            for _ in range(60):
                time.sleep(2)
                current_url = self.driver.current_url
                if "course" in current_url or "home" in current_url or "space" in current_url or "student" in current_url:
                    break

            self.anti_anti_cheat()
            time.sleep(3)

            try:
                zhidaole_button = WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.XPATH,
                                                                                                      '/html/body/div[3]/div/div[2]/div/div[2]/div/div/div/div[4]/button')))
                zhidaole_button.click()
            except Exception:
                pass

            try:
                anti_cheat_announce_button = WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located((By.XPATH,
                                                    '/html/body/div[4]/div/div/div/div[2]/div/div/div[4]/div[5]')))
                anti_cheat_announce_button.click()
            except Exception:
                pass

            # 登录结果复核：账号密码登录与 token 登录两条路径都算数，但必须真的离开了登录页。
            # 以前轮询超时也照样宣布「握手成功」，凭据错误时会带着未登录状态继续跑。
            final_url = self.driver.current_url or ""
            if "sso" in final_url or "signin" in final_url.lower():
                gui_log_queue.put("❌ 登录未完成（仍停留在登录页）：请检查账号密码/验证码，"
                                  "或更新 config.json 的 token_full 后重试")
                return False

            gui_log_queue.put(" 登录验证握手成功！")
            return True

        except Exception as e:
            error_msg = str(e)
            gui_log_queue.put(f" 登录执行流阻断: {error_msg[:50]}")
            logger.error(f"详细错误: {error_msg}", exc_info=True)
            return False

    def anti_anti_cheat(self):
        """注入token绕过防作弊检测"""
        # 只有「没填账号密码」时才用旧 token 登录。
        # 之前无条件注入：换了账号密码，站点仍按 token 认人 —— 页面身份会是上一个账号。
        if self.config.token_full and not (self.config.username and self.config.password):
            self.driver.execute_script(
                'window.localStorage.setItem("__token", arguments[0]);', self.config.token_full)
            print(" 使用 config.json 里的 token 登录（未填账号密码）")
        elif self.config.token_full:
            print(" 已填写账号密码，忽略 config.json 里的旧 token")
        self.driver.get("https://ucloud.unipus.cn/home")


class PopupWatcher:
    """弹窗监控器"""

    def __init__(self, driver):
        self.driver = driver

    def run(self):
        while True:
            try:
                ok = self._click_known_buttons()
                if ok is False:
                    # _click_known_buttons 识别到会话失效（invalid session id）才返回 False：
                    # 停掉监控循环，避免 while True 空转刷日志
                    return
                time.sleep(0.5)
            except Exception as e:
                error_msg = str(e)
                print(f"操作失败: {error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                time.sleep(0.5)

    def _click_known_buttons(self):
        js = """
        function findBtn(w) {
            const selectors = [
                '.know-box .iKnow',
                '.ant-modal-confirm-btns .ant-btn-primary',
                '.system-info-cloud-ok-button'
            ];
            for (let sel of selectors) {
                const b = w.document.querySelector(sel);
                if (b) return b;
            }
            return null;
        }
        function clickBtn(btn) {
            ['mouseover','mousedown','mouseup','click'].forEach(ev => {
                btn.dispatchEvent(new MouseEvent(ev, {bubbles:true}));
            });
            btn.click();
        }
        let btn = findBtn(window);
        if (btn) { clickBtn(btn); return true; }
        for (let i=0; i<window.frames.length; i++) {
            try {
                btn = findBtn(window.frames[i]);
                if (btn) { clickBtn(btn); return true; }
            } catch(e) {}
        }
        return false;
        """
        try:
            self.driver.execute_script(js)
        except Exception as _exc:
            # 浏览器被关掉/会话失效时，这里会一直报 invalid session id —— 直接停掉监控
            if 'invalid session id' in str(_exc).lower() or 'no such window' in str(_exc).lower():
                print('     ⚠ 浏览器会话已失效，停止弹窗监控')
                return False
            raise

if __name__ == '__main__':
    print('*' * 25 + "Unipus-Helper" + '*' * 25)

    skip_check = '--skip-check' in sys.argv

    if not skip_check:
        print("\n 提示：")
        print("   - 首次运行需要检查环境")
        print("   - 语音识别需要 FFmpeg（约130MB，可自动安装）")
        print("   - 如检查通过但无法启动，使用 --skip-check 跳过")

    logger = setup_logging()

    try:
        bot = UCampusBot(os.path.join(BASE_DIR, 'config.json'), skip_check=skip_check)
        bot.start()
    except Exception as e:
        error_msg = str(e)
        print(f"\n 程序运行失败: {error_msg[:100]}")
        logger.error(f"程序异常: {error_msg}", exc_info=True)
        input("\n按任意键退出...")
    finally:
        # 麦克风权限只在运行时放开：无论正常退出还是异常，都恢复成原来的设置
        try:
            mic = getattr(bot, "mic_permission", None)
            if mic is not None:
                mic.restore()
        except Exception:
            pass
