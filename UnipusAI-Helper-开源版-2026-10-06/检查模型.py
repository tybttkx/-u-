# -*- coding: utf-8 -*-
"""填好 api_key 之后跑一下这个，确认模型名和图片输入都可用。

用法（在项目文件夹里）：
    python 检查模型.py

退出码：0 = 三项全通过；1 = 缺 api_key 或有检查没通过。
"""
import base64
import io
import json
import os
import sys

from openai import OpenAI


def make_png(size=64, rgb=(200, 30, 30)):
    """生成一张 size×size 的纯色 PNG（通义要求图片尺寸达标，8x8 会被拒）。"""
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    cfg = json.load(io.open(os.path.join(base, "config.json"), encoding="utf-8"))

    key = (cfg.get("api_key") or "").strip()
    url = cfg.get("base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model = cfg.get("model") or "Qwen3-Omni-Flash"
    vision = cfg.get("vision_model") or model

    print("=" * 56)
    print("当前配置")
    print("  base_url   :", url)
    print("  model      :", model)
    print("  vision_model:", vision)
    print("  api_key    :", "已配置" if key else "未配置")
    print("=" * 56)

    if not key:
        print("\n❗请先把 config.json 里的 api_key 填上（Qwen / DashScope 的 key），再跑本脚本。")
        sys.exit(1)

    client = OpenAI(api_key=key, base_url=url)
    failed = 0

    print("\n[1/3] 这个 key 能用的模型：")
    try:
        ids = [m.id for m in client.models.list().data]
        print("   ", ids[:40])
        if model not in ids:
            print(f"   ⚠ 配置里的 {model!r} 不在列表里，请从上面挑一个正确的 id 填回 config.json")
    except Exception as exc:
        print("   ✗ 取模型列表失败：", str(exc)[:200])
        failed += 1

    print(f"\n[2/3] 文本问答（{model}）：")
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "只回两个字：收到"}],
            max_tokens=64,
        )
        text = (resp.choices[0].message.content or "").strip()
        print("   ✓ 回答:", text[:30] if text else "（空内容 —— 若模型名对，就把 max_tokens 调大）")
    except Exception as exc:
        print("   ✗ 失败：", str(exc)[:200])
        failed += 1

    png = make_png()
    print(f"\n[3/3] 图片输入（{vision}）—— 配对题要靠它：")
    try:
        resp = client.chat.completions.create(
            model=vision,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "这张图什么颜色？只回一个词"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
            ]}],
            max_tokens=64,
        )
        text = (resp.choices[0].message.content or "").strip()
        print("   ✓ 看图回答:", text[:30] if text else "（空内容 —— 把 max_tokens 调大，或该模型不收图）")
    except Exception as exc:
        print("   ✗ 失败：", str(exc)[:200])
        failed += 1

    print("\n三项都 ✓ 就可以重启主程序了。")
    if failed:
        print(f"（有 {failed} 项没通过，退出码 1）")
        sys.exit(1)


if __name__ == "__main__":
    main()
