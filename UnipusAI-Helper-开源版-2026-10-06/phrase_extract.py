# -*- coding: utf-8 -*-
"""括号汉译英：用句子里的英文锚点，从本板块英文原文中【原样截取】括号对应的短语。

为什么需要：让模型翻译它就会同义改写（实测 绿色产业链 → "green industrial chain"、
森林资源可持续利用 → "sustainable use of forest resources"，三个空错三个）。
这里改成确定性对齐：括号前后各取几个英文词当锚，去原文里定位，
中间缺的那一段直接抄出来 —— 不经过模型，不存在改写。
"""

import json
import os
import re
from typing import Dict, List, Optional, Tuple

_BRACKET_RE = re.compile(r"[（(]([^\u4e00-\u9fff]*[\u4e00-\u9fff][^）)]*)[）)]")
#: 我们自己塞进提示词的模板句，绝不当成"括号里的中文"去对齐
TEMPLATE_NOISE = ("文章较长", "根据问题回答即可", "题目要求", "页面内容", "页面完整信息",
                  "问题列表", "填空列表", "参考材料", "阅读材料", "写作提纲")
#: 括号前后两个锚点之间允许的最大间隔（字符）；超过就不算同一个短语
MAX_GAP = 90


def _words(text: str) -> List[str]:
    return re.findall(r"[A-Za-z][A-Za-z'\-]*", text or "")


def extract_from_question(question_text: str, corpus: List[str],
                          max_gap: int = MAX_GAP) -> List[Tuple[str, str, str]]:
    """返回 [(括号里的中文, 原文短语, 来源句子), ...]。

    对齐规则：括号前取 1..3 个英文词、括号后取 1..3 个英文词，
    在原文句子里找「前锚 + 任意内容 + 后锚」；前锚优先用更长的（更不容易撞车）。
    """
    out: List[Tuple[str, str, str]] = []
    if not question_text:
        return out
    for m in _BRACKET_RE.finditer(question_text):
        cn = str(m.group(1)).strip()
        if any(noise in cn for noise in TEMPLATE_NOISE):
            continue
        before_words = _words(question_text[:m.start()])[-3:]
        after_words = _words(question_text[m.end():])[:3]
        if not before_words and not after_words:
            continue
        found = ""
        source = ""
        def anchor(words):
            # 词与词之间允许空格/标点；两端加词边界 —— 否则单字母锚（如 "a"）
            # 会匹配到 "back" 里的 a，截出来的短语就带上了前半句。
            body = r"[\s,.;:!?'\-]+".join(re.escape(w) for w in words)
            return r"(?<![A-Za-z])" + body + r"(?![A-Za-z])"

        for k in range(len(before_words), 0, -1):
            b = anchor(before_words[-k:])
            for j in range(len(after_words), -1, -1):
                a = anchor(after_words[:j]) if j else r"[.,;:!?]"
                pattern = re.compile(
                    b + r"([^.!?]{1,%d}?)" % max_gap + a, re.I)
                for sentence in corpus:
                    got = pattern.search(sentence or "")
                    if got:
                        cand = got.group(1).strip(" ,.;:!?\"'“”‘’（）()｜")
                        _n_words = len(re.findall(r"[A-Za-z][A-Za-z'-]*", cand))
                        # 汉译英的答案必须是纯英文短语：候选里带中文（比如题干自对齐时
                        # 把括号本身圈进来）一律作废，交给词组词典兜底
                        if (2 <= len(cand) <= max_gap and _n_words >= 2
                                and not re.search(r"[\u4e00-\u9fff]", cand)):
                            found, source = cand, sentence.strip()
                            break
                if found:
                    break
            if found:
                break
        if found:
            out.append((cn, found, source))
    return out


def transcript_path(kb_root: str = "") -> str:
    root = kb_root or os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")
    return os.path.join(root, "_audio_cache", "transcripts.json")


def save_sentences(sentences: List[str], key: str, kb_root: str = "") -> None:
    """把某个小节/单元的英文原句存起来，供以后做对齐截取。"""
    path = transcript_path(kb_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data: Dict[str, List[str]] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.loads(handle.read())
    except Exception:
        data = {}
    merged = list(dict.fromkeys((data.get(key) or []) + [s for s in sentences if s]))
    data[key] = merged
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"[短语] 英文原句缓存写入失败（不影响答题）: {str(exc)[:80]}")


def corpus(key: str = "", kb_root: str = "") -> List[str]:
    """取回已存的英文原句；不给 key 就返回全部（跨单元也能对齐上）。"""
    try:
        with open(transcript_path(kb_root), encoding="utf-8") as handle:
            data = json.loads(handle.read())
    except Exception:
        return []
    if key and key in data:
        return data[key]
    out: List[str] = []
    for values in (data or {}).values():
        out.extend(values or [])
    return out


def extract_with_fallback(question_text: str, key: str = "",
                          kb_root: str = "") -> List[Tuple[str, str, str]]:
    """优先用已存原句对齐；没有再尝试从题干自身（同一句里的英文）截取；
    最后查「上一小节词组词典」（Vocabulary 跟读页存下的中文↔英文配对）。"""
    hits = extract_from_question(question_text, corpus(key, kb_root))
    if hits:
        return hits
    hits = extract_from_question(question_text, [question_text])
    if hits:
        return hits
    return lookup_brackets(question_text, kb_root)


def pairs_path(kb_root: str = "") -> str:
    root = kb_root or os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")
    return os.path.join(root, "_audio_cache", "phrase_pairs.json")


def save_pairs(pairs: List[Tuple[str, str]], key: str, kb_root: str = "") -> None:
    """存「中文 ↔ 英文词组」配对（Vocabulary 跟读页每行都是一对）。

    翻译题的括号中文与它原样对上时，直接照抄英文 —— 这类题联系的是
    上一小节的词组页，页面上没有上下文锚点，对齐截取用不上，只能查词典。
    """
    path = pairs_path(kb_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data: Dict[str, List[List[str]]] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.loads(handle.read())
    except Exception:
        data = {}
    cleaned = [[str(cn), str(en)] for cn, en in pairs if str(cn).strip() and str(en).strip()]
    merged = data.get(key) or []
    seen = {_norm_cn(cn) for cn, _en in merged}
    for cn, en in cleaned:
        norm = _norm_cn(cn)
        if norm and norm not in seen:
            seen.add(norm)
            merged.append([cn, en])
    data[key] = merged
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"[短语] 中英词组缓存写入失败（不影响答题）: {str(exc)[:80]}")


def _norm_cn(text: str) -> str:
    """中文匹配归一化：只留汉字（标点/空格/全半角差异一律抹平）。"""
    return "".join(re.findall(r"[\u4e00-\u9fff]", str(text or "")))


def lookup_phrase(cn: str, kb_root: str = "") -> str:
    """按括号里的中文查已存词组：精确归一匹配 → 包含匹配（较长一方为准）。"""
    norm = _norm_cn(cn)
    if len(norm) < 3:
        return ""
    try:
        with open(pairs_path(kb_root), encoding="utf-8") as handle:
            data = json.loads(handle.read())
    except Exception:
        return ""
    for _key, pairs in (data or {}).items():
        for stored_cn, stored_en in pairs or []:
            stored_norm = _norm_cn(stored_cn)
            if not stored_norm:
                continue
            if norm == stored_norm:
                return str(stored_en)
        for stored_cn, stored_en in pairs or []:
            stored_norm = _norm_cn(stored_cn)
            if len(stored_norm) >= 4 and (stored_norm in norm or norm in stored_norm):
                return str(stored_en)
    return ""


def lookup_brackets(question_text: str, kb_root: str = "") -> List[Tuple[str, str, str]]:
    """题干里每个括号中文 → 查词组词典；返回 [(括号中文, 英文词组, 来源), ...]。"""
    out: List[Tuple[str, str, str]] = []
    for m in _BRACKET_RE.finditer(question_text or ""):
        cn = str(m.group(1)).strip()
        if any(noise in cn for noise in TEMPLATE_NOISE):
            continue
        en = lookup_phrase(cn, kb_root)
        if en:
            # 词典存的是词组原形；括号所在句子可能需要时态/主谓一致变形
            # （实测词组页 eject/prevent 原形照抄进过去叙事句就是语法错误）
            out.append((cn, _maybe_inflect(en, question_text[:m.start()], question_text),
                        "上一小节词组"))
    return out


#: 短语首词是这些动词（原形）时才做变形 —— 名词/形容词开头的词组绝不动
_BASE_VERBS = frozenset("""
eject prevent protect engrave etch spread serve build rebuild restore mark keep hold
make use rise fall burn damage improve provide support remain include consist contain
form act add offer bring carry cover create destroy develop establish extend face gain
generate increase introduce launch lead leave lift limit maintain move name obtain open
order own pass place plant play point pour pull push reach reduce refer reflect record
replace remove reply report respect result return reveal roll rule run save scale seal
set settle shift shoot show sign sink sit slide soak sort spark spend split squeeze
stand start stick stop store stretch strike supply take tend throw tie treat turn
undergo view wash watch win work wrap water spray share contribute devote dedicate
""".split())

#: 不规则过去式（表里没有的按规则 +ed / e→d / 辅音+y→ied）
_IRREGULAR_PAST = {
    "spread": "spread", "build": "built", "make": "made", "keep": "kept", "hold": "held",
    "become": "became", "begin": "began", "fall": "fell", "rise": "rose", "grow": "grew",
    "stand": "stood", "take": "took", "give": "gave", "come": "came", "go": "went",
    "see": "saw", "lead": "led", "run": "ran", "win": "won", "find": "found",
    "shoot": "shot", "spend": "spent", "send": "sent", "sit": "sat", "set": "set",
}

_SUBJECT_STOPWORDS = frozenset("""
the a an of in on at to for and or which that there it they he she we you i as by with
from was were is are be this these those its their his her our your when while after
before during between into onto over under about
""".split())


def _past_cue(full_text: str) -> bool:
    """整道题里有没有过去式的迹象（was/were/had/…ed 动词）。

    同一道题的几个句子通常共用时态（实测第 1 句 needed、第 3 句 prevented），
    所以看整题而不只是括号前那几个词。
    """
    for w in _words(full_text):
        w = w.lower()
        if w in ("was", "were", "had") or (w.endswith("ed") and len(w) >= 5):
            return True
    return False


def _regular_past(word: str) -> str:
    if word.endswith("e"):
        return word + "d"
    if word.endswith("y") and len(word) >= 3 and word[-2] not in "aeiou":
        return word[:-1] + "ied"
    return word + "ed"


def _be_form(context_before: str, past: bool) -> str:
    """be 动词按主语单复数选择：就近往前找第一个实词，带 s 视为复数。"""
    plural = False
    for w in reversed(_words(context_before)):
        w = w.lower()
        if w in _SUBJECT_STOPWORDS:
            continue
        plural = w.endswith("s") and len(w) > 3
        break
    if past:
        return "were" if plural else "was"
    return "are" if plural else "is"


def _maybe_inflect(phrase: str, context_before: str, full_text: str) -> str:
    """轻量变形：过去迹象 + 短语首词是动词原形 → 变过去式；be 开头 → was/were/is/are。

    名词/形容词开头的词组（auspicious tanks、the largest cluster…）一律不动；
    已经是变形形式（is / ejected / prevented…）的也不动。
    """
    words = _words(phrase)
    if not words or not _past_cue(full_text):
        return phrase
    first = words[0].lower()
    rest = phrase[len(words[0]):]

    if first == "be":
        return _be_form(context_before, past=True) + rest

    if first in _BASE_VERBS:
        past = _IRREGULAR_PAST.get(first) or _regular_past(first)
        return past + rest

    return phrase
