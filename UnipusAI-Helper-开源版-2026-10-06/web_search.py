# -*- coding: utf-8 -*-
"""联网搜索（博查 AI）：题库没命中时，先搜标准答案再交给大模型。

优先级链：本地题库 → 联网搜索（这里）→ 大模型自己的知识。
拿不到结果就安静返回空，绝不阻塞答题流程。
"""
import json
import logging
import os
import re
import urllib.request
from typing import Dict, List

logger = logging.getLogger("UCampusBot")

ENDPOINT = "https://api.bochaai.com/v1/web-search"


def _key_from_config(config) -> str:
    key = ""
    try:
        key = getattr(config, "search_api_key", "") or ""
    except Exception:
        key = ""
    key = str(key).strip()
    if key.lower().startswith(("env:", "$env:")):
        name = key.split(":", 1)[1].strip()
        key = os.environ.get(name, "")
    if not key:
        key = os.environ.get("BOCHA_API_KEY", "")
    return key.strip()


def search(query: str, config=None, count: int = 4, timeout: int = 15) -> List[Dict[str, str]]:
    """搜一次，返回 [{title, url, summary}]。失败/没配 key 都返回空列表。"""
    key = _key_from_config(config) if config is not None else os.environ.get("BOCHA_API_KEY", "")
    query = re.sub(r"\s+", " ", str(query or "")).strip()[:200]
    if not key or not query:
        return []
    payload = {"query": query, "count": count, "summary": True}
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        logger.info(f"[联网] 搜索失败（不阻塞答题）: {str(exc)[:80]}")
        return []

    pages = (((body or {}).get("data") or {}).get("webPages") or {}).get("value") or []
    results: List[Dict[str, str]] = []
    for page in pages[:count]:
        title = re.sub(r"\s+", " ", str(page.get("name") or "")).strip()
        summary = re.sub(r"\s+", " ", str(page.get("summary") or page.get("snippet") or "")).strip()
        url = str(page.get("url") or "").strip()
        if title or summary:
            results.append({"title": title[:120], "url": url, "summary": summary[:400]})
    if results:
        logger.info(f"[联网] 命中 {len(results)} 条参考：{results[0]['title'][:40]}")
    return results


def build_context(questions, directions: str = "", config=None, max_chars: int = 2000) -> str:
    """把「题目 + 前几条搜索结果」拼成一段补充材料，供大模型参考。"""
    pieces: List[str] = []
    for question in list(questions or [])[:2]:
        text = re.sub(r"\s+", " ", str(getattr(question, "text", "") or "")).strip()
        if text:
            pieces.append(text[:160])
    query = " ".join(pieces) or str(directions or "")[:160]
    if not query:
        return ""
    results = search(query, config=config)
    if not results:
        return ""
    lines = ["【联网搜索结果（供参考，若与题目无关请忽略）】"]
    for index, item in enumerate(results, 1):
        lines.append(f"{index}. {item['title']}")
        if item["summary"]:
            lines.append(f"   {item['summary']}")
    context = "\n".join(lines)
    return context[:max_chars]
