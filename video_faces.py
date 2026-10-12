# -*- coding: utf-8 -*-
"""看视频认人：下载视频 → 按转写段落抽帧 → 交给视觉模型把「画面里的人」对到选项照片。

用于「Look at the people and read the answers. Then watch Part 3 of the podcast
and choose the people for the answers.」这类题：答案不在台词里，而在「谁在说」里 ——
单靠音频转写答不出来，必须看视频画面。

分工：先把台词按时间切成段（AudioTranscriber.transcribe_segments），在每段中点
抽一帧画面；视觉模型只需要回答「这一帧里的人是选项里的哪一张照片」，剩下的
「哪句台词对应哪道题」由本模块用词重合度在本地算 —— 让模型少做一步推理，稳一点。
"""
import os
import re
import subprocess
import tempfile
import urllib.request
from typing import Any, List, Optional, Sequence, Set, Tuple

#: ffmpeg 可执行文件名（项目其它地方也用它；装了但不在 PATH 时可改环境变量）
FFMPEG_BIN = os.environ.get("UCAMPUS_FFMPEG", "ffmpeg")


def download_media(url: str, suffix: str = ".mp4", timeout: int = 180) -> Optional[str]:
    """把视频/音频下到临时文件，返回路径；失败返回 None。"""
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response, \
                tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(response.read())
            return handle.name
    except Exception as exc:
        print(f"    （视频下载失败：{str(exc)[:60]}）")
        return None


def segment_midpoints(segments: Sequence[Tuple[float, float, str]],
                      limit: int = 16, min_gap: float = 2.0) -> List[int]:
    """给每段挑一个抽帧时间点（中点），并做稀疏化：太密时按均匀间隔取 limit 段。

    返回的是「段下标」列表（不是时间），调用方用 segments[i] 取对应台词。
    """
    count = len(segments)
    if count <= limit:
        return list(range(count))
    step = count / float(limit)
    picks: List[int] = []
    for k in range(limit):
        index = int(k * step)
        if not picks or index - picks[-1] >= 1:
            picks.append(index)
    return picks


def grid_times(duration: float, step: float = 3.5, max_frames: int = 24) -> List[float]:
    """按固定间隔铺抽帧时间点（不依赖转写段落）。

    实测：Whisper 会漏掉整段语音（11–30s 一个字都没识别出），只按段落中点抽帧
    就整段没画面；固定网格能保住「这一段时间里画面里是谁」。
    """
    if duration <= 0:
        return []
    step = max(1.5, float(step))
    if duration / step > max_frames:
        step = duration / float(max_frames)
    times = []
    moment = step / 2.0
    while moment < duration and len(times) < max_frames:
        times.append(round(moment, 2))
        moment += step
    return times


def text_near(segments: Sequence[Tuple[float, float, str]], moment: float,
              slack: float = 2.5) -> str:
    """取某一时刻附近的台词（时间上有重叠的段落拼起来）；没有就返回空串。"""
    pieces = []
    for start, end, text in segments:
        if start - slack <= moment <= end + slack:
            if text and text not in pieces:
                pieces.append(text)
    return " ".join(pieces)


def extract_frames(video_path: str, times: Sequence[float], out_dir: str,
                   width: int = 560) -> List[Tuple[float, str]]:
    """按时间点抽帧为小 jpg；抽失败的跳过，返回 [(时间, 图片路径), ...]。"""
    frames: List[Tuple[float, str]] = []
    for index, moment in enumerate(times):
        out_path = os.path.join(out_dir, f"frame_{index:02d}_{moment:.1f}s.jpg")
        command = [
            FFMPEG_BIN, "-ss", f"{max(0.0, float(moment)):.2f}", "-i", video_path,
            "-frames:v", "1", "-vf", f"scale={int(width)}:-2", "-q:v", "4",
            "-y", out_path,
        ]
        try:
            subprocess.run(command, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        except Exception:
            continue
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            frames.append((float(moment), out_path))
    return frames


_WORD_RE = re.compile(r"[A-Za-z]{3,}")
#: 匹配用的停用词：只留实词（"solving emotional problems" ↔ "emotional problems"）
_STOPWORDS: Set[str] = {
    "the", "and", "for", "with", "that", "this", "you", "your", "are", "not",
    "but", "can", "have", "has", "had", "about", "into", "them", "they", "their",
    "what", "when", "who", "how", "why", "good", "very", "just", "like", "from",
    "out", "get", "got", "one", "two", "also", "well", "much", "more", "most",
    "some", "any", "all", "its", "was", "were", "been", "being", "because",
    "really", "thing", "things", "kind", "kinds", "sort", "sorts", "lot", "lots",
    "bad", "such", "than", "then", "there", "here", "which", "while", "would",
    "could", "should", "does", "did", "doing", "done", "make", "makes", "made",
    "say", "says", "said", "way", "ways", "people", "person", "someone",
}


def content_words(text: str) -> Set[str]:
    """取实词集合（英文 3 字母以上、去停用词）。"""
    words = {w.lower() for w in _WORD_RE.findall(str(text or ""))}
    return {w for w in words if w not in _STOPWORDS}


def match_statement_to_segment(statement: str,
                               segments: Sequence[Tuple[float, float, str]],
                               min_overlap: int = 2) -> Optional[int]:
    """题目句子 ↔ 台词段落：按实词重合度找最像的一段。

    只认「唯一最高且重合 ≥ min_overlap」：并列或不足就返回 None（交回上层放弃），
    绝不猜 —— 猜错会把别人的人填到这道题上。
    """
    want = content_words(statement)
    if not want:
        return None
    best_index: Optional[int] = None
    best_score = 0
    tie = False
    for index, (_start, _end, text) in enumerate(segments):
        score = len(want & content_words(text))
        if score > best_score:
            best_index, best_score, tie = index, score, False
        elif score == best_score and score > 0:
            tie = True
    if best_score < min_overlap or tie:
        return None
    return best_index


def parse_letter_lines(text: str, valid_letters) -> dict:
    """解析视觉模型「帧i=X」/「N=X」式输出 → {编号: 字母}。

    容忍 markdown（**、反引号）、全角冒号、大小写（"Frame 3 = b"、"3=B"）。
    只保留字母在 valid_letters 里的行。
    """
    valid = {str(letter).strip().upper() for letter in (valid_letters or [])}
    result = {}
    for line in str(text or "").splitlines():
        cleaned = re.sub(r"[*`_\s]+", " ", line)
        match = re.search(r"(\d{1,2})\s*[=:：]\s*([A-Za-z])", cleaned)
        if not match:
            continue
        number, letter = int(match.group(1)), match.group(2).upper()
        if valid and letter not in valid:
            continue
        if number not in result:
            result[number] = letter
    return result


def cleanup_frames(frames: Sequence[Tuple[float, str]]) -> None:
    """删除抽出来的帧文件（收尾用）。"""
    for _moment, path in frames:
        try:
            os.unlink(path)
        except OSError:
            pass
