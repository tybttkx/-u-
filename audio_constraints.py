# -*- coding: utf-8 -*-
"""关掉 WebRTC 的音频前处理（AEC / 降噪 / 自动增益）。

为什么需要：跟读题的录音输入如果来自回环设备（VB-Cable 的 CABLE Output、或
Realtek 的立体声混音），浏览器会把录到的声音当成"自己播放的回声"，用回声消除
直接抵消掉 —— 实测表现是回环电平正常（0.1）但评分 0（完整度 0）。
把这三个开关关掉后，回环音频就能原样录进去。
"""

SOURCE = r"""
(function () {
  try {
    var md = navigator.mediaDevices;
    if (!md || !md.getUserMedia || md.__ucNoAecInstalled) { return; }
    var orig = md.getUserMedia.bind(md);
    var clean = { echoCancellation: false, noiseSuppression: false, autoGainControl: false };
    md.getUserMedia = function (constraints) {
      try {
        var c = constraints || {};
        if (typeof c.audio === 'object' && c.audio !== null) {
          c.audio = Object.assign({}, c.audio, clean);
        } else if (c.audio === undefined || c.audio === true) {
          c.audio = clean;
        }
      } catch (e) {}
      return orig(c);
    };
    md.__ucNoAecInstalled = true;
  } catch (e) {}
})();
"""
