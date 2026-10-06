# -*- coding: utf-8 -*-
"""示范音缓存：把跟读页的示范录音按「句子指纹」存进题库目录，下次直接回放。

用户思路（2026-09-24）：脚本先听一遍示范录音，把它以某种形式存到题库里；
之后录音时把这个形式还原成音频喂进去 —— 于是录音不再依赖页面播放时机，
换账号、重刷同一页都能直接命中缓存。

存放形式：
  knowledge/_audio_cache/<sha1(句子文本)>.mp3     音频本体
  knowledge/_audio_cache/index.json              指纹 → {file, text, src, 时间}
取用时转成 data URL（http 页面无法直接加载本地文件），交给页面里的
getUserMedia 路由（__ucAudioRouting）当作麦克风输入。
"""
import base64
import subprocess
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.request

CACHE_DIRNAME = "_audio_cache"
#: 浏览器假麦克风读的那个文件（--use-file-for-fake-audio-capture 指向它）
INJECT_WAV_NAME = "current.wav"
MAX_BYTES = 8 * 1024 * 1024        # 单个示范音上限，超过不缓存
#: index.json 读-改-写互斥（主流程与弹窗线程都可能同时存缓存）
_index_lock = threading.Lock()


def _kb_root() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")


def cache_dir(create: bool = True) -> str:
    path = os.path.join(_kb_root(), CACHE_DIRNAME)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def fingerprint(text: str) -> str:
    """句子指纹：去掉空白与标点后取 sha1，同一句在不同账号/单元也命中同一份。"""
    # 先去掉开头的题号（"15 1. When ..." 与 "When ..." 必须算同一句，否则缓存命中不了）
    body = re.sub(r"^[\s\d]+[\.、)）\-:]*\s*", "", str(text or ""))
    norm = re.sub(r"[^a-zA-Z0-9]+", "", body.lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:20] if norm else ""


def _index_path() -> str:
    return os.path.join(cache_dir(), "index.json")


def load_index() -> dict:
    try:
        return json.loads(io.open(_index_path(), encoding="utf-8").read())
    except Exception:
        return {}


def save_index(index: dict) -> None:
    """原子写 index.json：先写临时文件再 os.replace，避免读方拿到写了一半的 JSON。

    失败打印原因（不再静默吞掉）。
    """
    path = _index_path()
    tmp = path + ".tmp"
    try:
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)
    except Exception as exc:
        print(f"     示范音缓存：index.json 写入失败：{str(exc)[:80]}")


def store(text: str, url: str = "", data: bytes = b"") -> str:
    """把示范音存进缓存，返回文件路径（失败返回空串）。"""
    key = fingerprint(text)
    if not key:
        return ""
    path = os.path.join(cache_dir(), key + ".mp3")
    if not data and url:
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = resp.read(MAX_BYTES + 1)
        except Exception:
            return ""
    if not data or len(data) > MAX_BYTES:
        return ""
    try:
        io.open(path, "wb").write(data)
    except Exception:
        return ""
    with _index_lock:                      # 读-改-写整体加锁，两个线程不会互相覆盖
        index = load_index()
        index[key] = {"file": key + ".mp3", "text": str(text)[:200], "src": url[:200],
                      "saved": time.strftime("%Y-%m-%d %H:%M")}
        save_index(index)
    return path


def cache_path(text: str) -> str:
    """缓存 mp3 的路径（不检查是否存在；句子指纹为空时返回空串）。"""
    key = fingerprint(text)
    if not key:
        return ""
    return os.path.join(cache_dir(create=False), key + ".mp3")


def is_cached(text: str) -> bool:
    """是否已有该句的示范音缓存：只做 os.path.exists，不读文件。"""
    path = cache_path(text)
    return bool(path) and os.path.exists(path)


def data_url(text: str) -> str:
    """取出缓存的示范音，转成 data URL（可直接在页面里 new Audio(...).play()）。"""
    path = cache_path(text)
    if not path or not os.path.exists(path):
        return ""
    try:
        raw = io.open(path, "rb").read()
    except Exception:
        return ""
    return "data:audio/mpeg;base64," + base64.b64encode(raw).decode("ascii")


def stats() -> str:
    index = load_index()
    return f"示范音缓存 {len(index)} 条（{cache_dir(create=False)}）"


def inject_path() -> str:
    """浏览器假麦克风要读的 WAV 路径（固定名，每次录音前覆盖）。"""
    return os.path.join(cache_dir(), INJECT_WAV_NAME)


def to_wav(text: str, ffmpeg: str = "ffmpeg", rate: int = 48000) -> str:
    """把缓存的示范音转成 WAV 并写到注入路径，返回该路径（失败返回空串）。

    Edge/Chrome 的 --use-file-for-fake-audio-capture 只吃 WAV（16bit PCM），
    所以我们用 ffmpeg 把缓存的 mp3 转一次；每条录音前都重写这个文件，
    于是「这一条录进去的就是这一条的标准读音」。
    """
    key = fingerprint(text)
    if not key:
        return ""
    mp3_path = os.path.join(cache_dir(create=False), key + ".mp3")
    if not os.path.exists(mp3_path):
        return ""
    out = inject_path()
    tmp = out + ".tmp"                       # 先写临时文件，成功后再原子替换成 current.wav
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", mp3_path,
             "-ar", str(rate), "-ac", "1", "-sample_fmt", "s16", tmp],
            capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        # subprocess.run 超时后已负责 kill 并等待子进程，这里只需清掉半成品
        print(f"     示范音缓存：ffmpeg 转换超时（60 秒），本条不注入：{mp3_path}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return ""
    except FileNotFoundError:
        print("     示范音缓存：系统里找不到 ffmpeg，无法生成注入音频")
        return ""
    except Exception as exc:
        print(f"     示范音缓存：ffmpeg 执行异常：{str(exc)[:80]}")
        return ""
    if proc.returncode != 0 or not os.path.exists(tmp):
        print(f"     示范音缓存：ffmpeg 转换失败（退出码 {proc.returncode}）：{mp3_path}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return ""
    try:
        os.replace(tmp, out)
    except OSError as exc:
        print(f"     示范音缓存：写入注入文件失败：{str(exc)[:80]}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return ""
    return out
