# -*- coding: utf-8 -*-
"""跟读题：示范音缓存 + 每条只录一次。

实测页面层级（视听说教程3 · After you view · Exercise 1）：
  .sentence-container                     一条句子
    └ .record-button-group
        └ .record-button-wrap             每条 3 个（播放 / 录音 / 波形）
            ├ .question-audio.audio-origin > audio   示范读音
            ├ .question-audio.audio-replay > audio   回放
            └ .ucomp-recorder > span.record-icon.button-record   麦克风

要点（用户 2026-09-24 指出的两个问题）：
  1. 麦克风必须和「它自己那句」的示范音绑定 —— 旧逻辑按顺序点 24 个元素，
     没有归属关系，于是每轮都读到同一句、把所有录音都塞进第一条；
  2. 每条只录一次，录完读该条评分再走。

示范音缓存：首次遇到把示范音存进 knowledge/_audio_cache（按句子指纹），
以后直接回放缓存当录音输入 —— 录进去的就是标准读音本身。
"""
import os
import time


from selenium.webdriver.common.by import By

def _row_of(mic):
    """麦克风所属的那条句子容器。"""
    from selenium.webdriver.common.by import By
    for xpath in ('./ancestor::div[contains(@class,"sentence-container")][1]',
                  './ancestor::*[contains(@class,"record-button-group")][1]',
                  './ancestor::*[contains(@class,"record-button-wrap")][1]'):
        try:
            return mic.find_element(By.XPATH, xpath)
        except Exception:
            continue
    return None


def _text_of(row) -> str:
    if row is None:
        return ""
    import re
    for sel in ('.sentence-text', '.content-text', 'p'):
        try:
            el = row.find_element("css selector", sel)
            t = (el.text or "").strip()
            if t:
                return t[:300]
        except Exception:
            continue
    try:
        txt = (row.text or "").strip()
        return re.sub(r"\s+", " ", txt)[:300]
    except Exception:
        return ""


def _demo_audio(row):
    from selenium.webdriver.common.by import By
    for sel in ('.question-audio.audio-origin audio', 'audio'):
        try:
            el = row.find_element(By.CSS_SELECTOR, sel)
            if el:
                return el
        except Exception:
            continue
    return None


def _duration_from_src(src: str, default: float = 6.0) -> float:
    """从示范音地址里取时长（URL 里自带 #duration=10.650000&size=…）。"""
    import re
    m = re.search(r"duration=([0-9.]+)", src or "")
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass
    return default


def run_all_items(driver, verbose: bool = True):
    """一页跟读题：纯 Selenium 驱动，逐条「开始录音 → 播缓存示范音 → 停止 → 读分」。

    为什么不用 JS 返回值：项目里的 driver.execute_script 拿不到 JS 的返回值
    （实测同一条 JS 用 CDP 执行能拿到 4 条数据，走 driver 却一直是 None），
    所以这里全部改用 find_elements / get_attribute / .text，JS 只用副作用
    （播放我们自己的 Audio、守卫播放状态）。
    """
    if driver is None:
        return []
    import json
    import re
    import time
    from selenium.webdriver.common.by import By

    try:
        import audio_cache
        import audio_server
    except Exception as exc:
        if verbose:
            print(f"     跟读题：缓存/服务模块不可用（{str(exc)[:60]}）")
        audio_cache = audio_server = None

    # 标签页置前台：跟读页在后台时麦克风按钮不渲染，录音也不会触发
    try:
        # 关 AEC 补丁：主程序里那次注入实测没生效（日志里连自检行都没有），
        # 这里再注入一次；读回值写进 DOM 属性再取 —— 本项目 driver.execute_script
        # 拿不到 JS 返回值（恒为 None），所以不能靠 return。
        try:
            from audio_constraints import SOURCE as _no_aec_src
            driver.execute_script(_no_aec_src)
            driver.execute_script(
                "try{document.body.setAttribute('data-uc-aec', (navigator.mediaDevices && navigator.mediaDevices.__ucNoAecInstalled) ? '1' : '0');}"
                "catch(e){document.body.setAttribute('data-uc-aec','e');}")
            _flag = driver.find_element('tag name', 'body').get_attribute('data-uc-aec')
            print(f"     跟读题：AEC 补丁 = {'已装 ✔' if _flag == '1' else '未生效 ✘'}")
        except Exception as _exc:
            print(f"     跟读题：AEC 补丁注入失败 {str(_exc)[:60]}")

        driver.execute_cdp_cmd("Page.bringToFront", {})
    except Exception:
        pass

    SELECTORS = ('.ucomp-recorder .button-record', '.button-record',
                 '.ucomp-recorder .record-icon', '[class*="button-record"]')


    def mics_now():
        for sel in SELECTORS:
            try:
                found = driver.find_elements(By.CSS_SELECTOR, sel)
            except Exception:
                found = []
            if found:
                return found
        return []

    # 等麦克风渲染出来（最多 30 秒）
    mics = []
    for attempt in range(30):
        mics = mics_now()
        if mics:
            if verbose:
                print(f"     跟读题：等待 {attempt} 秒后找到 {len(mics)} 条麦克风")
            # 把本页每条麦克风对应句子的英文原句存下来（供汉译英照抄原文）：
            # mics 是麦克风按钮元素，句子文本在它的祖先容器里，用 _text_of 取；空句丢弃。
            # Vocabulary 词组页每行是「英文表达 + 中文翻译」成对出现，顺带存成
            # 中英词组词典 —— 后面小节的括号汉译英直接查词典照抄（联系上一小节作答）
            try:
                import phrase_extract as _pe
                rows_text = [t for t in (_text_of(_row_of(m)) for m in mics) if t]
                _pe.save_sentences(rows_text, key="read_aloud")
                pairs = []
                for t in rows_text:
                    cn = "".join(re.findall(r"[\u4e00-\u9fff]+", t)).strip()
                    en = " ".join(re.findall(r"[A-Za-z][A-Za-z'\-]*", t)).strip()
                    if cn and en:
                        pairs.append((cn, en))
                if pairs:
                    _pe.save_pairs(pairs, key="read_aloud")
                if verbose:
                    print(f"     跟读题：已缓存 {len(rows_text)} 句英文原句、"
                          f"{len(pairs)} 条中英词组（供括号汉译英照抄）")
            except Exception as exc:
                if verbose:
                    print(f"     跟读题：句子缓存失败 {str(exc)[:60]}")
            break
        try:
            driver.execute_script("return 0;")     # 轻触一下页面，促其渲染
        except Exception:
            pass
        time.sleep(1)
    if not mics:
        if verbose:
            print("     跟读题：等了 30 秒仍没找到麦克风（不是跟读页或未渲染）")
        return []

    # 切回环设备（有就切），跑完恢复
    # 录音题一进来就把默认设备切到 VB-Cable（播放→CABLE Input、录音→CABLE Output），
    # 跑完在 finally 里切回真实设备。判定用"切换后读回比对"——这台机器上
    # IPolicyConfig 会返回非 0 但实际已生效，只看返回值会误判。
    route_on = False
    try:
        import audio_route
        route_on = audio_route.use_vb_cable(verbose=verbose)
        if verbose and route_on:
            print("     跟读题：已自动打开 VB-Cable（录音题专用）")
    except Exception as exc:
        if verbose:
            print(f"     跟读题：VB-Cable 切换不可用（{str(exc)[:50]}）")

    results = []
    hits = fresh = 0
    try:
        for index, mic in enumerate(mics):
            if index > 0:                      # 页面每录完一条会重排，重新取列表
                current = mics_now()
                if index < len(current):
                    mic = current[index]
            row = _row_of(mic)
            text = _text_of(row)
            audio_el = _demo_audio(row)
            src = ""
            try:
                src = (audio_el.get_attribute("src") or "") if audio_el else ""
            except Exception:
                src = ""
            dur = _duration_from_src(src, 6.0)

            # 示范音入缓存（按句子指纹），并生成 base64 —— 播放改用「blob 注入」：
            # 音频直接内联进页面（URL.createObjectURL），不走 127.0.0.1 本地服务，
            # 也就不会再触发 Edge 的「访问此设备上的其他应用和服务」弹窗。
            # 命中判断用 is_cached（只查文件是否存在，不读文件），文件只读一次生成 payload。
            payload = ""
            if audio_cache is not None and text:
                try:
                    if audio_cache.is_cached(text):
                        hits += 1
                    elif src and audio_cache.store(text, url=src):
                        fresh += 1              # 只有 store() 真的写成功才计新存
                    # 没缓存又没 src 时两边都不计数（不再出现旧的"假命中"）
                    key_path = audio_cache.cache_path(text)
                    if key_path and os.path.exists(key_path):
                        import base64
                        with open(key_path, "rb") as fh:
                            payload = base64.b64encode(fh.read()).decode("ascii")
                except Exception:
                    payload = ""

            # 开始录音（Selenium 点击 = 可信点击，页面才认）
            ok = False
            info = ""
            try:
                # 录音前先自建一条「关掉 AEC/降噪/自动增益」的麦克风流并保持住：
                # 页面之后的录音请求会沿用这条会话，示范音就不会被 AEC 当成回声消掉
                # （实测默认约束是 echo=true ns=true agc=true，必须显式关）。
                try:
                    driver.execute_script(
                        "if (!window.__ueMic) {"
                        "  navigator.mediaDevices.getUserMedia({audio: {echoCancellation: false,"
                        "    noiseSuppression: false, autoGainControl: false}})"
                        "  .then(function (s) { window.__ueMic = s; })"
                        "  .catch(function (e) { window.__ueMicErr = String(e); });"
                        "}")
                    time.sleep(0.5)          # 让这条流先建立好
                    # 读回这条流实际生效的约束（echo/ns/agc），确认真的关掉了
                    driver.execute_script(
                        "try{var t=window.__ueMic&&window.__ueMic.getAudioTracks()[0];"
                        "var st=t?t.getSettings():null;"
                        "document.body.setAttribute('data-uc-set', st ? "
                        "[st.echoCancellation, st.noiseSuppression, st.autoGainControl].join('/') : 'none');}"
                        "catch(e){document.body.setAttribute('data-uc-set','err');}")
                    _set = driver.find_element('tag name', 'body').get_attribute('data-uc-set')
                    print(f"     跟读题：录音约束 echo/ns/agc = {_set}")
                except Exception:
                    pass

                mic.click()
                time.sleep(0.8)
                if audio_el is not None and not payload:
                    # 兜底路径：拿不到缓存副本（payload 为空）时才播页面自己的元素。
                    # （优先级：blob 缓存 > 页面元素——有 payload 时走下面的 elif。）

                    # 只让页面自己播示范音 —— 之前"页面放一份 + 我们自建元素再放一份"
                    # 两路音频叠在一起，ASR 只听到互相干扰（转写成了
                    # 'flififififififififif' 那种乱码），所以改成单音源：
                    # 播它自己的 audio，同时守住它不被页面静音/暂停。
                    driver.execute_script(
                        "var a = arguments[0];"
                        "try{a.currentTime = 0;}catch(e){}"
                        "a.muted = false; a.volume = 1;"
                        "try{a.play();}catch(e){}"
                        "try{clearInterval(window.__ueG);}catch(e){}"
                        "window.__ueG = setInterval(function(){"
                        "  try{ if(a.muted){a.muted=false;}"
                        "       if(a.paused && a.currentTime < ((isFinite(a.duration)?a.duration:60)-0.15))"
                        "       { a.play(); } }catch(e){} }, 150);", audio_el)
                elif payload:
                    # 优先路径：blob 注入我们缓存的副本（有缓存就用它，页面元素只是兜底）
                    driver.execute_script(
                        "try{window.__ueA && window.__ueA.pause();}catch(e){}"
                        "try{clearInterval(window.__ueG);}catch(e){}"
                        "var b = atob(arguments[0]);"
                        "var arr = new Uint8Array(b.length);"
                        "for (var i = 0; i < b.length; i++) { arr[i] = b.charCodeAt(i); }"
                        "window.__ueUrl = URL.createObjectURL(new Blob([arr], {type: 'audio/mpeg'}));"
                        "window.__ueA = new Audio(window.__ueUrl);"
                        # ★ 必须挂进 DOM：游离的 audio 元素被浏览器静默处理，
                        # 回环里收不到声音（实测 0.0002 vs 挂载后 0.9805）
                        "window.__ueA.style.display = 'none';"
                        "document.body.appendChild(window.__ueA);"
                        "window.__ueA.muted = false; window.__ueA.volume = 1;"
                        "window.__ueA.play();"
                        "window.__ueG = setInterval(function(){"
                        "  try{ if(window.__ueA.muted){window.__ueA.muted=false;}"
                        "       if(window.__ueA.paused && window.__ueA.currentTime < "
                        "          ((isFinite(window.__ueA.duration)?window.__ueA.duration:60)-0.15))"
                        "       { window.__ueA.play(); } }catch(e){} }, 150);", payload)
                # （单音源：页面元素已在上面处理）
                time.sleep(dur + 1.2)
                driver.execute_script(
                    "try{clearInterval(window.__ueG);}catch(e){}"
                    "try{window.__ueA && window.__ueA.pause();}catch(e){}"
                    "try{URL.revokeObjectURL(window.__ueUrl);}catch(e){}"
                    "try{window.__ueA && window.__ueA.parentNode && "
                    "window.__ueA.parentNode.removeChild(window.__ueA);}catch(e){}")
                mic.click()                       # 停止录音
                ok = True
            except Exception as exc:
                info = f"录音失败: {str(exc)[:50]}"

            # 读该条评分（最多等 25 秒）
            score = ""
            if ok:
                for _ in range(25):
                    time.sleep(1)
                    try:
                        el = row.find_element(By.CSS_SELECTOR, '.sentence-result-score-detail')
                        score = re.sub(r"\s+", " ", (el.text or "")).strip()
                    except Exception:
                        score = ""
                    if score:
                        break
                if not score:
                    info = "（评分未返回）"
            results.append((index + 1, ok, score or info))
            if verbose:
                print(f"       第 {index + 1} 条: {'✔' if ok else '✘'} {score or info or '已录'}")
    finally:
        try:
            driver.execute_script(
                "try{window.__ueMic && window.__ueMic.getTracks().forEach(function(t){t.stop();});"
                "window.__ueMic = null;}catch(e){}")
        except Exception:
            pass
        if route_on:
            try:
                import audio_route
                audio_route.use_real_devices(verbose=verbose)
                if verbose:
                    print("     跟读题：已切回真实设备（扬声器 / 麦克风阵列）")
            except Exception:
                pass

    if verbose:
        good = sum(1 for _n, ok, _s in results if ok)
        print(f"     跟读题：逐条完成 {good}/{len(results)} 条"
              f"（缓存命中 {hits}，新存 {fresh}）")
    return results
