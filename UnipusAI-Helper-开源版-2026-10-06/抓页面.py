# -*- coding: utf-8 -*-
"""连到程序开的 Edge 调试端口，把当前页面 dump 下来，便于定位问题。

用法（在项目文件夹里）：
    python 抓页面.py             # 打印当前标签页的文字与关键元素
"""
import io
import json
import os
import urllib.request


def read_port() -> int:
    """从 config.json 读 debug_port；读不到时用 9333（主程序默认值）兜底。"""
    try:
        with io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "config.json"), encoding="utf-8") as handle:
            return int(json.load(handle).get("debug_port") or 9333)
    except Exception:
        return 9333


PORT = read_port()


def targets():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=6) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main():
    try:
        pages = [t for t in targets() if t.get("type") == "page"]
    except Exception as exc:
        print(f"连不上调试端口 {PORT}（程序启动时是否带 --remote-debugging-port？）: {exc}")
        return
    if not pages:
        print("没有找到页面标签")
        return
    for page in pages[:3]:
        print("=" * 70)
        print("标题:", page.get("title"))
        print("地址:", (page.get("url") or "")[:120])
    print("=" * 70)
    print("提示：下面的调试地址（webSocketDebuggerUrl）可完全控制已登录的浏览器会话，")
    print("      仅限本机自查，请勿外发给他人。")
    for page in pages[:3]:
        print("  ", page.get("webSocketDebuggerUrl"))


if __name__ == "__main__":
    main()
