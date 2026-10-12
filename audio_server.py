# -*- coding: utf-8 -*-
"""极小的本地 HTTP 服务：把题库里缓存的示范音喂给页面播放。

为什么不用 data URL：页面可能有 CSP 限制（实测 new Audio(dataURL) 不生效），
而 http://127.0.0.1:port/xxx.mp3 是普通网络请求，不受限；而且这是我们自己的
audio 元素，跟读页无法静音/暂停它。
"""

import functools
import http.server
import os
import socketserver
import threading

_cached = {"base": "", "server": None}


class _Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):        # 静音，不刷日志
        pass


def start(directory: str = "", port: int = 0):
    """启动（或复用）本地服务，返回 (base_url, server)。"""
    if _cached["base"]:
        return _cached["base"], _cached["server"]
    directory = directory or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "knowledge", "_audio_cache")
    os.makedirs(directory, exist_ok=True)
    handler = functools.partial(_Handler, directory=directory)
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    httpd.allow_reuse_address = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _cached["base"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    _cached["server"] = httpd
    print(f"     本地音频服务：{_cached['base']}（{directory}）")
    return _cached["base"], httpd


def url_for(path: str) -> str:
    """把缓存文件转成本地 HTTP 地址。"""
    if not path or not os.path.exists(path):
        return ""
    base, _ = start()
    return f"{base}/{os.path.basename(path)}"
