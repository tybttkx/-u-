# -*- coding: utf-8 -*-
"""Role-play 题（专用流程 v3 —— 录音沿用跟读题现成的成熟方法）。

作答逻辑（用户口述 + 实机逐帧实测，新视野视听说1 · Get from A to B · Role-play · Exercise 3）：

  页面流程
    1. 情境步（「Situation: I can't find my way!」+ 下一题 `a.btn`）→ 点下一题到选角色
    2. 选角色（`.role-list .role`，A 默认选中）→ 第 2 屏懒渲染出 6 行对白
       （`.role-reply .list-item-review`，A/B 交替；本人 A 的行 = 第 1/3/5 行）
    3. **先点大喇叭（`.audio-control-trumpet`）把整段听完（强制）**：整段 = 一串
       CDN 音频 URL，实测顺序 A1 B1 A2 B2 A3（本人句在 0/2/4 位）
    4. **点一次麦克风（`.record-seat`）→ 平台自动开「录音窗口」**（约 15 秒，
       六行进度圈倒计时）→ 到点自动停、自动出分（写入该行 `.score`）→
       自动播放 B 的句子 → 自动开下一个窗口。三句连录完为止，不用再点
    5. 有分后回放键（`.audio-control-wave`）自动启用

  录音输入（与跟读题同一条路，见 read_aloud.run_all_items）
    VB-Cable 回环（播放→CABLE Input、录音→CABLE Output）+ 录音前预开一条关
    AEC 的麦克风流 + 每个窗口里播放这一句的示范音频（下载好的 blob，挂守卫防
    平台静音）——示范怎么读、录进去的就是什么。跑完切回真实设备。

  为什么另起灶炉：整页一个麦克风 + 轮播 + 平台引导式连录，和通用跟读的
  「逐句麦克风」结构完全不同；但录音的底层方法完全复用跟读题那套。
"""

import base64
import io
import os
import re
import subprocess
import time
import urllib.request

from selenium.webdriver.common.by import By

ROLE_PLAY_SEL = '.question-role-play'
ROLE_REPLY_SEL = '.role-reply'
ROLE_ITEM_SEL = '.list-item-review'
SEAT_SEL = '.record-seat'
RECORDER_SEL = '.ucomp-recorder'
TRUMPET_SEL = '.audio-control-trumpet'

#: 单句及格线（用户口径：满分 100，60 够用）；低于它自动再来一轮
PASS_SCORE = 60
#: 录音窗口最长等待（实测约 15 秒）
_WINDOW_MAX = 40.0

#: 播放缓存音频（blob 注入；与 read_aloud 同款：必须挂进 DOM 才出声）
_PLAY_BLOB_JS = (
    "try{window.__ueA && window.__ueA.pause();}catch(e){}"
    "try{clearInterval(window.__ueG);}catch(e){}"
    "var b = atob(arguments[0]);"
    "var arr = new Uint8Array(b.length);"
    "for (var i = 0; i < b.length; i++) { arr[i] = b.charCodeAt(i); }"
    "window.__ueUrl = URL.createObjectURL(new Blob([arr], {type: 'audio/mpeg'}));"
    "window.__ueA = new Audio(window.__ueUrl);"
    "window.__ueA.style.display = 'none';"
    "document.body.appendChild(window.__ueA);"
    "window.__ueA.muted = false; window.__ueA.volume = 1;"
    "window.__ueA.play();"
    "window.__ueG = setInterval(function(){"
    "  try{ if(window.__ueA.muted){window.__ueA.muted=false;}"
    "       if(window.__ueA.paused && window.__ueA.currentTime < "
    "          ((isFinite(window.__ueA.duration)?window.__ueA.duration:60)-0.15))"
    "       { window.__ueA.play(); } }catch(e){} }, 150);"
)

_STOP_AUDIO_JS = (
    "try{clearInterval(window.__ueG);}catch(e){}"
    "try{window.__ueA && window.__ueA.pause();}catch(e){}"
    "try{URL.revokeObjectURL(window.__ueUrl);}catch(e){}"
)

#: 录音前预开一条「关 AEC/降噪/自动增益」的麦克风流并保持住（与 read_aloud 同款）
_EAR_OFF_MIC_JS = (
    "if (!window.__ueMic) {"
    "  navigator.mediaDevices.getUserMedia({audio: {echoCancellation: false,"
    "    noiseSuppression: false, autoGainControl: false}})"
    "  .then(function (s) { window.__ueMic = s; })"
    "  .catch(function (e) { window.__ueMicErr = String(e); });"
    "}"
)

#: 采集页面播放过的音频地址（大喇叭整段 + 平台自动播的 B 句都经过这里）
_CAPTURE_HOOK = r"""
(function () {
  try {
    if (window.__rlHook) { return; }
    window.__rlUrls = [];
    var rec = function (src) {
      try {
        if (!src) { return; }
        var s = String(src);
        if (s.length < 60 && s.indexOf('http') !== 0) { return; }
        var tail = window.__rlUrls.length ? window.__rlUrls[window.__rlUrls.length - 1] : '';
        if (tail === s) { return; }
        window.__rlUrls.push(s);
        if (window.__rlUrls.length > 60) { window.__rlUrls.shift(); }
      } catch (e) {}
    };
    var OrigAudio = window.Audio;
    window.Audio = function () {
      var el = new OrigAudio(...arguments);
      try { rec(el.currentSrc || el.src); } catch (e) {}
      return el;
    };
    window.Audio.prototype = OrigAudio.prototype;
    var origPlay = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function () {
      try { rec(this.currentSrc || this.src); } catch (e) {}
      return origPlay.apply(this, arguments);
    };
    window.__rlHook = true;
  } catch (e) {}
})();
"""


def is_role_play_page(driver) -> bool:
    """当前页是不是 Role-play（选角色 + 跟读）题（含还没渲染出题目的「情境步」）。"""
    try:
        if driver.find_elements(By.CSS_SELECTOR, ROLE_PLAY_SEL):
            return True
        if driver.find_elements(By.CSS_SELECTOR, '.role-play-wrapper, .role-list, .role-reply'):
            return True
        for c in driver.find_elements(By.CSS_SELECTOR, '.ant-breadcrumb'):
            try:
                if 'Role-play' in (c.text or ''):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _item_rows(driver):
    try:
        return driver.find_elements(By.CSS_SELECTOR, ROLE_REPLY_SEL + ' ' + ROLE_ITEM_SEL)
    except Exception:
        return []


def _row_role(row) -> str:
    try:
        return ((row.find_element(By.CSS_SELECTOR, '.role').text or '')).strip().upper()
    except Exception:
        return ''


def _row_score_num(row):
    """读这一行的评分（数字）；没有返回 None。"""
    try:
        t = (row.find_element(By.CSS_SELECTOR, '.score').text or '').strip()
    except Exception:
        return None
    m = re.search(r'\d+', t)
    if not m:
        return None
    try:
        return int(m.group(0))
    except Exception:
        return None


def _recording(driver) -> bool:
    try:
        cls = driver.find_element(By.CSS_SELECTOR, RECORDER_SEL).get_attribute('class') or ''
        return 'disabled' not in cls
    except Exception:
        return False


def _click_el(driver, el, verbose=False) -> bool:
    """滚动 + 原生点击 → ActionChains 真实鼠标 → MouseEvent 兜底（SVG 无 .click()）。"""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        time.sleep(0.3)
    except Exception:
        pass
    try:
        el.click()
        return True
    except Exception:
        pass
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver).move_to_element(el).click().perform()
        return True
    except Exception:
        pass
    try:
        driver.execute_script(
            "arguments[0].dispatchEvent(new MouseEvent('click',"
            "{bubbles:true,cancelable:true,view:window}));", el)
        return True
    except Exception as exc:
        if verbose:
            print('     Role-play：点击失败（%s）' % str(exc)[:60])
        return False


def ensure_step2(driver, verbose=True) -> bool:
    """把页面走到 Step 2（跟读行出现）：处理情境步的「下一题」+ 选角色。"""
    if _item_rows(driver):
        return True
    try:
        for el in driver.find_elements(By.CSS_SELECTOR, 'a.btn'):
            try:
                if '下一题' in (el.text or ''):
                    if _click_el(driver, el):
                        if verbose:
                            print('     Role-play：已点「下一题」（情境 → 选角色）')
                        time.sleep(1.5)
                    break
            except Exception:
                continue
    except Exception:
        pass
    if _item_rows(driver):
        return True
    try:
        for el in driver.find_elements(By.CSS_SELECTOR, '.role-list .role'):
            letter = ''
            try:
                letter = ((el.find_element(By.CSS_SELECTOR, 'label span').text or '')).strip().upper()
            except Exception:
                pass
            if letter and letter == 'A':
                if _click_el(driver, el, verbose):
                    if verbose:
                        print('     Role-play：已选角色 A')
                break
    except Exception:
        pass
    for _ in range(40):
        if _item_rows(driver):
            return True
        time.sleep(0.5)
    return bool(_item_rows(driver))


def listen_whole_dialogue(driver, verbose=True) -> list:
    """点大喇叭把整段听完（平台强制），返回按序采集到的音频地址列表。"""
    try:
        driver.execute_script(_CAPTURE_HOOK)
        driver.execute_script("try{window.__rlUrls = [];}catch(e){}")
    except Exception:
        pass
    got = _click_el(driver, driver.find_element(By.CSS_SELECTOR, TRUMPET_SEL), verbose)
    if verbose:
        print('     Role-play：点大喇叭，等整段播完…（%s）' % ('已点' if got else '点了没反应'))
    quiet = 0.0
    last_n = 0
    t0 = time.time()
    while time.time() - t0 < 180:
        time.sleep(1.5)
        try:
            n = len(driver.execute_script("return window.__rlUrls || [];") or [])
        except Exception:
            n = 0
        if n != last_n:
            last_n = n
            quiet = 0.0
        else:
            quiet += 1.5
        if n >= 4 and quiet >= 6.0:
            break
        if n == 0 and time.time() - t0 > 30:
            break
    try:
        urls = driver.execute_script("return window.__rlUrls || [];") or []
    except Exception:
        urls = []
    if verbose:
        print('     Role-play：整段播放结束，采到 %d 段音频' % len(urls))
    return [str(u) for u in urls]


def _download(url: str) -> str:
    """下载一段音频，返回 base64（失败返回空串）。"""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if len(data) < 1000:
            return ''
        return base64.b64encode(data).decode('ascii')
    except Exception:
        return ''


def _switch_cable(verbose=True):
    """跳到 VB-Cable 回环（跟读题同款）；返回是否切换（失败也继续，调用方只打印）。"""
    try:
        import audio_route
        ok = audio_route.use_vb_cable(verbose=verbose)
        return bool(ok)
    except Exception as exc:
        if verbose:
            print('     Role-play：VB-Cable 切换不可用（%s）' % str(exc)[:60])
        return False


def _restore_devices(verbose=True):
    try:
        import audio_route
        audio_route.use_real_devices(verbose=verbose)
    except Exception:
        pass


def run_role_play(driver, role: str = 'A', verbose: bool = True) -> bool:
    """Role-play 专用流程：听完 → 点麦克风 → 平台自动连录三窗口 → 逐句喂示范音频。

    返回 True 表示本人每一句都拿到 ≥PASS_SCORE 的分数（不够会自动再来一轮）。
    """
    if driver is None or not is_role_play_page(driver):
        return False
    role = (str(role or 'A').strip().upper() or 'A')
    try:
        driver.execute_cdp_cmd('Page.bringToFront', {})
    except Exception:
        pass

    if not ensure_step2(driver, verbose):
        if verbose:
            print('     Role-play：没能走到跟读步（选角色/渲染失败）')
        return False

    rows = _item_rows(driver)
    my_numbers = [i for i, r in enumerate(rows, 1) if _row_role(r) == role]
    if not my_numbers:
        if verbose:
            print('     Role-play：没找到角色 %s 的台词行' % role)
        return False
    if verbose:
        print('     Role-play：角色 %s，需要朗读 %d 句（第 %s 行）'
              % (role, len(my_numbers), '/'.join(str(n) for n in my_numbers)))

    # ① 整段听（强制），同时采集每句音频地址
    urls = listen_whole_dialogue(driver, verbose)
    my_urls = [u for i, u in enumerate(urls) if i % 2 == 0][:len(my_numbers)]
    if len(my_urls) < len(my_numbers):
        if verbose:
            print('     Role-play：音频没采齐（%d/%d），仍继续'
                  % (len(my_urls), len(my_numbers)))
    # 下载本人句子（跟读题同款：先备好音频，窗口里直接播）
    payloads = []
    for u in my_urls:
        payloads.append(_download(u) if u else '')
    ok_dl = sum(1 for p in payloads if p)
    if verbose:
        print('     Role-play：本人 %d 句音频已备 %d 份' % (len(my_numbers), ok_dl))
    if not ok_dl:
        if verbose:
            print('     Role-play：音频一份都没下来，停止')
        return False

    # ② 跳 VB-Cable 回环（跟读题同款）+ 预开关 AEC 的麦克风流
    route_on = _switch_cable(verbose)
    try:
        from audio_constraints import SOURCE as _no_aec_src
        driver.execute_script(_no_aec_src)
    except Exception:
        pass
    try:
        driver.execute_script(_EAR_OFF_MIC_JS)
        time.sleep(0.6)
    except Exception:
        pass

    results = []
    try:
        for attempt in range(2):               # 最多两轮平台引导
            # 点一次麦克风（点了不起效就重试，最多 3 次）
            engaged = False
            for _ in range(3):
                try:
                    seat = driver.find_element(By.CSS_SELECTOR, SEAT_SEL)
                except Exception:
                    break
                if not _click_el(driver, seat, verbose):
                    break
                for _ in range(16):
                    time.sleep(0.5)
                    if _recording(driver):
                        engaged = True
                        break
                if engaged:
                    break
                time.sleep(2)
            if not engaged:
                if verbose:
                    print('     Role-play：麦克风没起录（第 %d 轮放弃）' % (attempt + 1))
                break
            if verbose:
                print('     Role-play：第 %d 轮开始，平台自动连录中…' % (attempt + 1))

            # 窗口循环：窗口开 → 播这一句（跟读同款 blob+守卫）→ 窗口关 → 读分
            for k in range(len(my_numbers)):
                opened = False
                for _ in range(int(_WINDOW_MAX * 2)):
                    time.sleep(0.5)
                    if _recording(driver):
                        opened = True
                        break
                if not opened:
                    if verbose:
                        print('     Role-play：第 %d 个窗口没开（本轮结束）' % (k + 1))
                    break
                if payloads[k]:
                    # ★ 起录延迟缓冲：窗口「开」只是 UI 状态，平台录音管线还要
                    #   0.8~2.8 秒才真正在录（逐词着色实测：开头的词被录丢）。
                    #   先等 4 秒让录音跑起来，再播整句 —— take 开头留点静音无妨，
                    #   句子完整就行（窗口约 15 秒，音频最长 6.2 秒，够放）。
                    time.sleep(4.0)
                    try:
                        driver.execute_script(_PLAY_BLOB_JS, payloads[k])
                    except Exception as exc:
                        if verbose:
                            print('     Role-play：播放失败（%s）' % str(exc)[:50])
                if verbose:
                    print('     Role-play：窗口 %d 开录 → 播第 %d 句'
                          % (k + 1, my_numbers[k]))
                for _ in range(int(_WINDOW_MAX * 2)):
                    time.sleep(0.5)
                    if not _recording(driver):
                        break
                try:
                    driver.execute_script(_STOP_AUDIO_JS)
                except Exception:
                    pass
                row_no = my_numbers[k]
                score = None
                for _ in range(30):
                    time.sleep(1)
                    rows = _item_rows(driver)
                    score = _row_score_num(rows[row_no - 1]) if row_no - 1 < len(rows) else None
                    if score is not None:
                        break
                results.append((row_no, score))
                if verbose:
                    print('     Role-play：第 %s 行 → %s'
                          % (row_no, ('%d 分' % score) if score is not None else '未出分'))

            # 本轮结果检查：都过线就不再录
            rows = _item_rows(driver)
            left = [n for n in my_numbers
                    if (lambda s: s is None or s < PASS_SCORE)(_row_score_num(rows[n - 1])
                                                               if n - 1 < len(rows) else None)]
            if not left:
                break
            if attempt == 0 and verbose:
                print('     Role-play：%s 未过 %d 分，再来一轮…'
                      % ('/'.join(str(n) for n in left), PASS_SCORE))
    finally:
        try:
            driver.execute_script(_STOP_AUDIO_JS)
        except Exception:
            pass
        try:
            driver.execute_script(
                "try{window.__ueMic && window.__ueMic.getTracks().forEach(function(t){t.stop();});"
                "window.__ueMic = null;}catch(e){}")
        except Exception:
            pass
        if route_on:
            _restore_devices(verbose)

    rows = _item_rows(driver)
    all_ok = True
    detail = []
    for n in my_numbers:
        s = _row_score_num(rows[n - 1]) if n - 1 < len(rows) else None
        detail.append('%s:%s' % (n, s if s is not None else '-'))
        if s is None or s < PASS_SCORE:
            all_ok = False
    if verbose:
        print('     Role-play：逐句结果 %s，%s'
              % (' '.join(detail), '全部过线 ✔' if all_ok else '有句子未过线 ✘'))
    return all_ok
