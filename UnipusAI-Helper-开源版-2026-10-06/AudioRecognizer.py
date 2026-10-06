"""本地 Whisper 语音识别：懒加载单例 + 线程安全 + 有上限的结果缓存。

要点（2026-10-03 审查修复）：
  - 模型不在构造时加载；whisper/torch 在模块导入时"提前"加载一次（torch 的 DLL 必须
    早于 Qt 进进程，否则懒导入会撞 WinError 1114），失败不拦启动、识别降级为返回空结果；
  - 模型是进程内单例（加载一次要几十秒、几百 MB，不能每个实例重载），
    transcribe 入口加锁串行化 —— whisper 模型非线程安全，主流程与弹窗线程共享本对象；
  - _transcript_cache 加上限（MAX_CACHE），按最久未用淘汰。
"""
import os
import tempfile
import threading
import traceback
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Dict, Optional


#: 提前加载 whisper/torch：torch 的 DLL 必须最早进入进程。实测一旦 Qt/qfluentwidgets
#: 先加载，之后（懒加载）再 import torch 就会撞 DLL 初始化失败（WinError 1114, c10.dll），
#: 听力/视频题的音频转录会全部降级为空。本模块在主程序第 1 行就被导入，这里提前加载
#: 正好早于 fluent_ui；加载失败也不拦启动，交给懒加载逻辑降级处理。
try:
    import whisper as _WHISPER_EAGER  # noqa: F401
except Exception as _eager_exc:  # 缺依赖 / DLL 环境问题都只提示，不让程序起不来
    _WHISPER_EAGER = None
    print(f"（提示）语音模型组件启动预加载失败：{str(_eager_exc)[:80]}")


class AudioTranscriber:
    """
    使用本地 Whisper 进行语音识别
    """

    #: Whisper 模型名（可由主程序按 config.json 的 whisper_model 覆盖）：
    #: base 快但容易听错（实测把 French horns 听成 French homes），
    #: small 更准、约慢 3 倍；tiny/medium/large 也可。
    MODEL_NAME = "base"
    #: Whisper 模型进程内单例（懒加载）
    _model = None
    #: 模型加载锁（类级）
    _model_lock = threading.Lock()
    #: transcribe 串行锁（类级）：模型非线程安全，且所有实例共享同一个模型
    _run_lock = threading.Lock()
    #: 加载失败只报一次，之后直接降级（不反复重试、不刷错误）
    _model_failed = False
    #: 识别结果缓存上限（条），超过按最久未用淘汰
    MAX_CACHE = 512

    def __init__(self):
        self.local_model = None                       # 兼容旧属性；懒加载后指向同一个模型
        self._transcript_cache: Dict[str, str] = OrderedDict()

    @staticmethod
    def _print_exception(prefix: str, exc: Exception) -> None:
        """完整输出异常信息与堆栈，方便定位问题"""
        print(f"{prefix}: {exc}")
        tb = traceback.format_exc().rstrip()
        if tb and tb != "NoneType: None":
            print(tb)

    @classmethod
    def _get_model(cls):
        """懒加载 Whisper 模型（线程安全、只加载一次）；失败打印说明并返回 None。"""
        if cls._model is not None:
            return cls._model
        with cls._model_lock:
            if cls._model is not None:
                return cls._model
            if cls._model_failed:
                return None
            try:
                whisper = _WHISPER_EAGER
                if whisper is None:
                    import whisper                    # 启动预加载失败时兜底再试一次
                print(f"       加载 Whisper 本地模型 ({cls.MODEL_NAME})...")
                # 可选: tiny, base, small, medium, large（config.json 的 whisper_model）
                cls._model = whisper.load_model(cls.MODEL_NAME or "base")
                print("       本地模型加载完成")
            except ImportError as e:
                cls._model_failed = True
                cls._print_exception(
                    "       未安装 whisper，语音识别降级为不可用"
                    "（pip install openai-whisper 后可恢复）", e)
            except Exception as e:
                cls._model_failed = True
                cls._print_exception("       加载本地模型失败，语音识别降级为不可用", e)
            return cls._model

    def _init_local_model(self):
        """（保留旧名兼容）触发一次懒加载；失败不再 raise，返回模型或 None。"""
        self.local_model = self._get_model()
        return self.local_model

    def _remember(self, audio_url: str, text: str) -> None:
        """写入识别缓存，超过 MAX_CACHE 时淘汰最久未用的条目。"""
        self._transcript_cache[audio_url] = text
        self._transcript_cache.move_to_end(audio_url)
        while len(self._transcript_cache) > self.MAX_CACHE:
            self._transcript_cache.popitem(last=False)

    def transcribe(self, audio_url: str, language: str = "en",
                   initial_prompt: str = "") -> str:
        """
        下载音频并转录为文字

        Args:
            audio_url: 音频文件URL
            language: 语言代码，默认英语 en，中文 zh
            initial_prompt: Whisper 提示词（页面词汇等）——引导识别本课的词，
                实测 base 模型会把 French horns 听成 French homes，提示词能纠偏

        Returns:
            识别出的文字；模型不可用或出错时返回空串（降级，不抛异常）
        """
        # 入口加锁：whisper 模型非线程安全，主流程与弹窗线程共享本对象；
        # 识别缓存也跟着这把锁被保护。
        with self._run_lock:
            cached = self._transcript_cache.get(audio_url)
            if cached is not None:
                print(f"       使用缓存的识别结果")
                self._transcript_cache.move_to_end(audio_url)
                return cached

            audio_path = None

            try:
                print(f"      ⬇  下载音频...")
                with urllib.request.urlopen(audio_url, timeout=30) as response, \
                        tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    f.write(response.read())
                    audio_path = f.name

                print(f"        开始识别...")
                text = self._transcribe_local(audio_path, language, initial_prompt)

                if text:
                    self._remember(audio_url, text)
                    print(f"       识别成功 ({len(text)} 字符)")
                    print(f"       音频识别结果{text}")

                return text or ""

            except urllib.error.URLError as e:
                self._print_exception("       下载音频失败", e)
                return ""
            except Exception as e:
                self._print_exception("       识别失败", e)
                return ""
            finally:
                if audio_path:
                    try:
                        os.unlink(audio_path)
                    except OSError:
                        pass

    def _transcribe_local(self, audio_path: str, language: str,
                          initial_prompt: str = "") -> Optional[str]:
        """使用本地 Whisper 模型识别（调用方须已持有 transcribe 入口锁）"""
        model = self.local_model or self._get_model()
        self.local_model = model
        if model is None:
            print("       本地模型不可用，跳过识别")
            return None

        kwargs = {"language": language, "fp16": False}  # CPU 运行 fp16 设为 False
        if initial_prompt:
            # Whisper 提示词：喂页面词汇引导识别（最多约 60 词，防止超提示窗口）
            kwargs["initial_prompt"] = " ".join(str(initial_prompt).split()[:60])
        result = model.transcribe(audio_path, **kwargs)

        return result["text"].strip() if result else None

    def transcribe_segments(self, audio_url: str, language: str = "en",
                            initial_prompt: str = "",
                            max_segments: int = 40):
        """下载音频并转录，返回**带时间戳的段落** [(start, end, text), ...]。

        「看视频认人」这类题要把每句台词对到视频画面（谁在说）：台词从这些段落来，
        画面按段落中点抽帧。识别失败/模型不可用时返回空列表（降级，不抛异常）。
        """
        with self._run_lock:
            audio_path = None
            try:
                print("      ⬇  下载音频（分段识别）...")
                with urllib.request.urlopen(audio_url, timeout=60) as response, \
                        tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    f.write(response.read())
                    audio_path = f.name

                model = self.local_model or self._get_model()
                self.local_model = model
                if model is None:
                    print("       本地模型不可用，跳过识别")
                    return []
                kwargs = {"language": language, "fp16": False}
                if initial_prompt:
                    kwargs["initial_prompt"] = " ".join(str(initial_prompt).split()[:60])
                result = model.transcribe(audio_path, **kwargs) or {}
                segments = []
                for seg in result.get("segments") or []:
                    try:
                        text = str(seg.get("text", "")).strip()
                        start, end = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
                    except Exception:
                        continue
                    if text:
                        segments.append((start, end, text))
                text = str(result.get("text", "")).strip()
                if text:
                    self._remember(audio_url, text)
                print(f"       分段识别成功（{len(segments)} 段 / {len(text)} 字符）")
                return segments[:max_segments]
            except urllib.error.URLError as e:
                self._print_exception("       下载音频失败", e)
                return []
            except Exception as e:
                self._print_exception("       分段识别失败", e)
                return []
            finally:
                if audio_path:
                    try:
                        os.unlink(audio_path)
                    except OSError:
                        pass
