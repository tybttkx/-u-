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
                  './ancestor::*[contains(@class,"record-button-wrap")][1]',
                  # 视频配音页（question-video-dub-pc）没有上面那套行容器：
                  # 句子在 div.item > .component-htmlview 里，示范音/麦克风在同级的
                  # div.operate 里 —— 取 item 作行，文本与音频都够得到；operate 兜底。
                  './ancestor::*[contains(@class,"item")][1]',
                  './ancestor::*[contains(@class,"operate")][1]'):
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


def _estimate_seconds(text: str, default: float = 6.0) -> float:
    """示范音地址里没有时长时，按句子词数估「读完这句」需要多久。

    与 main.FollowReadHandler.estimate_seconds 同一口径（2.2 词/秒 × 1.15 余量，
    限制 3~30 秒）。配音页的示范音 src 初始为空、拿不到 #duration，原来 6 秒的
    固定兜底会把 30 词的句子录到一半就停。
    """
    import re
    words = [w for w in re.findall(r"[A-Za-z']+", str(text or "")) if w]
    if not words:
        return default
    return max(3.0, min(30.0, len(words) / 2.2 * 1.15))


def _clean_demo_src(src: str) -> str:
    """过滤掉不是示范音的"地址"。

    Selenium 读空的 src 属性会拿到文档地址（DOM property 行为）——它不是示范音，
    拿去缓存会把 HTML 当音频存进缓存。只放行明确的媒体地址。
    """
    import re
    s = (src or "").strip()
    if (s.startswith("http")
            and not re.search(r"\.(mp3|m4a|wav|aac|ogg)(\?|#|$)", s, re.I)
            and "audio" not in s.lower()):
        return ""
    return s


def _video_read(driver, prop: str):
    """读视频元素的数值属性（本项目 execute_script 拿不到返回值：写进 DOM 属性再读回）。"""
    from selenium.webdriver.common.by import By
    try:
        driver.execute_script(
            "try{var v=document.querySelector('video');"
            "if(v){v.setAttribute('data-uc-read', String(v." + prop + "));}}catch(e){}")
        el = driver.find_element(By.CSS_SELECTOR, 'video')
        raw = el.get_attribute('data-uc-read')
        return float(raw) if raw not in (None, '') else None
    except Exception:
        return None


def _video_read_str(driver, prop: str) -> str:
    """读视频元素的字符串属性（同 _video_read 的属性回读套路）。"""
    from selenium.webdriver.common.by import By
    try:
        driver.execute_script(
            "try{var v=document.querySelector('video');"
            "if(v){v.setAttribute('data-uc-read', String(v." + prop + "));}}catch(e){}")
        el = driver.find_element(By.CSS_SELECTOR, 'video')
        return el.get_attribute('data-uc-read') or ""
    except Exception:
        return ""


#: 「切片直录」的文档级注入脚本：必须在页面加载前跑（平台的录音器在页面加载时
#: 就把 getUserMedia 的旧引用存下来了，事后包装收不到调用 —— 实测录音期间 0 次
#: 调用记录）。脚本只在 localStorage 开关打开时把「音频 getUserMedia」换成一条
#: WebAudio 合成的流（内容=我们那个 <audio> 正在播的音频，实时可控），其余情况
#: 原样放行 —— 所以对音频跟读页零影响。
_SLICE_ROUTE_SRC = r"""
(function () {
  try {
    if (window.__ucSliceRouteHooked) { return; }
    var md = navigator.mediaDevices;
    if (!md || !md.getUserMedia) { return; }
    var orig = md.getUserMedia.bind(md);
    function buildStream() {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      for (var attempt = 0; attempt < 3; attempt++) {
        try {
          var el = (attempt === 0 && window.__ucSliceEl) ? window.__ucSliceEl
                                                        : document.createElement('audio');
          el.style.display = 'none';
          if (!el.parentNode) { (document.body || document.documentElement).appendChild(el); }
          if (!window.__ucCtx2) { window.__ucCtx2 = new Ctx(); }
          var src = window.__ucCtx2.createMediaElementSource(el);
          var dest = window.__ucCtx2.createMediaStreamDestination();
          src.connect(dest);
          window.__ucSliceEl = el;
          window.__ucSrc2 = src;
          window.__ucDest2 = dest;
          window.__ucSliceErr = '';
          try {
            var master = dest.stream.getAudioTracks()[0];
            master.onended = function () { window.__gcMasterEnded = Date.now(); };
          } catch (e) {}
          return dest;
        } catch (e) {
          window.__ucSliceErr = String(e);
          if (attempt === 0 && window.__ucSliceEl === el) { window.__ucSliceEl = null; }
        }
      }
      return null;
    }
    md.getUserMedia = function (constraints) {
      try {
        if (localStorage.getItem('__ucSliceRoute') === '1'
            && constraints && constraints.audio) {
          if (!window.__ucDest2) { buildStream(); }
          if (window.__ucDest2) {
            try {
              window.__gumLog = window.__gumLog || [];
              if (window.__gumLog.length > 200) { window.__gumLog.shift(); }
              window.__gumLog.push({ts: Date.now(), by: 'slice'});
            } catch (e) {}
            try {
              // 每次取麦都新建一个 MediaStreamDestination（同源再连一路）：
              // 平台录完常把拿到的那条轨 stop() 掉 —— 直接给主图会让第一次录音
              // 之后全死（实测上传冻结、轨 readyState=ended）；给克隆则克隆不过
              // 声音（实测录成静音）。同一音源接多个 destination 是合法的，
              // 每个 destination 都是一条带声音的真流，停哪路只伤哪路。
              var d2 = window.__ucCtx2.createMediaStreamDestination();
              window.__ucSrc2.connect(d2);
              try {
                var t0 = d2.stream.getAudioTracks()[0];
                t0.onended = function () {
                  window.__gcTrackEnded = window.__gcTrackEnded || [];
                  window.__gcTrackEnded.push(Date.now());
                };
              } catch (e3) {}
              return Promise.resolve(d2.stream);
            } catch (e2) {
              window.__ucSliceErr = String(e2);
              return Promise.resolve(window.__ucDest2.stream);
            }
          }
        }
      } catch (e) { window.__ucSliceErr = String(e); }
      try {
        window.__gumLog = window.__gumLog || [];
        if (window.__gumLog.length > 200) { window.__gumLog.shift(); }
        window.__gumLog.push({ts: Date.now(), by: 'orig',
                              audio: !!(constraints && constraints.audio)});
      } catch (e) {}
      return orig(constraints);
    };
    window.__ucSliceRouteHooked = true;
  } catch (e) {}
})();
"""


def _install_slice_mic(driver, verbose: bool = True) -> bool:
    """装「切片直录」（配音页）：localStorage 开关 + 文档级注入 + 刷新页面 + 自检。

    装好后，平台加载时拿到的「麦克风」就是我们合成的流；录音时我们播哪一片
    视频音频，它就录到哪一片（内容实时可控，平台缓存/复用流都无所谓）。
    对音频跟读页零影响：开关不开时注入脚本原样放行。
    自检不健康（例如历史脏元素导致建流失败）就再刷一次页面重试。
    """
    import time as _time

    try:
        driver.execute_script("try{localStorage.setItem('__ucSliceRoute','1');}catch(e){}")
        result = driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument", {"source": _SLICE_ROUTE_SRC})
        try:
            setattr(driver, "_ucSliceScriptId", (result or {}).get("identifier", ""))
        except Exception:
            pass
        for attempt in range(2):
            driver.refresh()
            for _ in range(30):
                _time.sleep(1)
                try:
                    if driver.find_elements(By.CSS_SELECTOR, '.layout-container, .ucomp-recorder'):
                        break
                except Exception:
                    continue
            _time.sleep(3)
            # 热身：主动取一次「麦克风」，逼出合成流的建立并自检（值写 DOM 属性再读回）
            try:
                driver.execute_script(
                    "try{"
                    "  navigator.mediaDevices.getUserMedia({audio: true}).then(function(s){"
                    "    document.body.setAttribute('data-uc-warm',"
                    "      (window.__ucDest2 ? '1' : '0') + '|' + (window.__ucSliceErr || ''));"
                    "  }).catch(function(e){"
                    "    document.body.setAttribute('data-uc-warm', 'E|' + String(e));"
                    "  });"
                    "}catch(e){document.body.setAttribute('data-uc-warm','X|'+e);}")
                _time.sleep(2)
                warm = driver.find_element(By.TAG_NAME, 'body').get_attribute('data-uc-warm') or ''
            except Exception as exc:
                warm = f"读值失败 {str(exc)[:40]}"
            if verbose:
                print(f"     跟读题：切片直录自检[{attempt + 1}] = {str(warm)[:90]}")
            if str(warm).startswith('1'):
                return True
        return False
    except Exception as exc:
        print(f"     跟读题：切片直录安装异常（{str(exc)[:60]}）")
        return False


def _feed_slice_audio(driver, payload_b64: str, verbose: bool = True) -> bool:
    """把整段视频音频交给切片播放器（刷新后调用；内容随后按条目 seek 播放）。"""
    try:
        driver.execute_script(
            "try{"
            "  if (!window.__ucSliceEl) {"
            "    var el = document.createElement('audio'); el.style.display = 'none';"
            "    document.body.appendChild(el); window.__ucSliceEl = el;"
            "  }"
            "  var b = atob(arguments[0]);"
            "  var arr = new Uint8Array(b.length);"
            "  for (var i = 0; i < b.length; i++) { arr[i] = b.charCodeAt(i); }"
            "  window.__ucSliceUrl = URL.createObjectURL(new Blob([arr], {type: 'audio/mpeg'}));"
            "  window.__ucSliceEl.src = window.__ucSliceUrl;"
            "}catch(e){ window.__ucSliceErr2 = String(e); }",
            payload_b64)
        driver.execute_script(
            "try{document.body.setAttribute('data-uc-slice-src',"
            "(window.__ucSliceEl && window.__ucSliceEl.src) ? '1' : '0');}catch(e){}")
        src_ok = driver.find_element(By.TAG_NAME, 'body').get_attribute('data-uc-slice-src') == '1'
        if not src_ok and verbose:
            print("     跟读题：切片播放器没拿到音频数据")
        return src_ok
    except Exception as exc:
        if verbose:
            print(f"     跟读题：切片播放器装载失败（{str(exc)[:60]}）")
        return False


def _ensure_video_audio(driver, verbose: bool = True) -> str:
    """把配音页整段视频的音频抽出来缓存成 mp3（一次），返回缓存文件路径。

    为什么需要：实测平台在录音期间会把视频静音（muted 一路为 true），视频的
    声音进不了录音流；缓存这份音频后，录音时由我们自己的 <audio> 播放
    （平台管不着），与音频跟读页「播缓存示范音」是同一条路。
    """
    import hashlib
    import os
    import subprocess

    url = _video_read_str(driver, 'currentSrc') or _video_read_str(driver, 'src')
    if not url or not url.startswith("http"):
        return ""
    try:
        import audio_cache
        cache_dir = audio_cache.cache_dir()
    except Exception:
        return ""
    target = os.path.join(
        cache_dir, "video_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".mp3")
    if os.path.exists(target) and os.path.getsize(target) > 1000:
        return target
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", url, "-vn",
             "-acodec", "libmp3lame", "-q:a", "5", target],
            capture_output=True, timeout=300)
    except Exception as exc:
        if verbose:
            print(f"     跟读题：视频音频抽取失败（{str(exc)[:50]}）")
        return ""
    if os.path.exists(target) and os.path.getsize(target) > 1000:
        return target
    try:
        os.remove(target)
    except OSError:
        pass
    if verbose:
        print(f"     跟读题：视频音频抽取失败（ffmpeg 退出码 {result.returncode}）")
    return ""


def _play_segment(driver, row, start=None, tries: int = 2):
    """让这一条的示范（视频片段）真的放起来。

    优先点它自己的播放键（平台会把视频跳到该段起点并播放 —— 配音页的示范音在
    视频里，<audio> 里那个短音只是提示音）；点完读视频位置验证真的动了，没动就
    重试。点不动、又给了 start 时，直接 seek 视频到该段起点再播。返回是否在播。
    """
    from selenium.webdriver.common.by import By
    import time

    before = _video_read(driver, 'currentTime')
    for _ in range(max(1, tries)):
        try:
            row.find_element(By.CSS_SELECTOR, '.btn-play').click()
        except Exception:
            break
        time.sleep(0.4)
        now = _video_read(driver, 'currentTime')
        if now is not None and before is not None and abs(now - before) > 0.1:
            return True
        before = now
    if start is not None:
        try:
            driver.execute_script(
                "try{var v=document.querySelector('video');"
                "if(v){v.muted=false;v.volume=1;v.currentTime=arguments[0];v.play();}}catch(e){}",
                float(start))
            time.sleep(0.3)
            now = _video_read(driver, 'currentTime')
            return now is not None and abs((now or 0) - float(start)) < 3.0
        except Exception:
            return False
    return False


def _scan_video_segments(driver, mics):
    """配音页（示范音在视频里）：扫出每条的片段时长。

    做法：静音视频，逐条借它自己的播放键把视频跳到该段起点，读回落点、立刻暂停。
    相邻两个落点之差就是该条在视频里的时长 —— 录音时按这个时长播/停，比按句子
    词数估准得多。任何一步拿不到数据就返回 []，调用方退回按词数估时长。
    """
    from selenium.webdriver.common.by import By
    import time

    starts = []
    total = 0.0
    try:
        # 扫描期间静音，尽量不出声；记住原状态，扫完还原
        driver.execute_script(
            "try{var v=document.querySelector('video');"
            "if(v){v.setAttribute('data-uc-was-muted', v.muted ? '1' : '0'); v.muted = true;}}"
            "catch(e){}")
        for mic in mics:
            try:
                row = _row_of(mic)
                if row is None:
                    starts.append(None)
                    continue
                if not _play_segment(driver, row):
                    starts.append(None)
                    continue
                # 播放 0.4 秒后才读的位置：起点要减掉这点播放推进（时长差不受影响）
                t = _video_read(driver, 'currentTime')
                starts.append(max(0.0, t - 0.4) if t is not None else None)
                total = _video_read(driver, 'duration') or 0.0
                driver.execute_script("try{document.querySelector('video').pause();}catch(e){}")
            except Exception:
                starts.append(None)
    finally:
        try:
            driver.execute_script(
                "try{var v=document.querySelector('video');"
                "if(v){v.muted = (v.getAttribute('data-uc-was-muted') === '1');"
                "      v.pause();}}catch(e){}")
        except Exception:
            pass

    # 校验：每条都拿到、起点严格递增、每段 3~30 秒 —— 不然这份边界不可信
    if len(starts) != len(mics) or any(s is None for s in starts):
        return []
    durs = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else total
        dur = end - start
        if not (3.0 <= dur <= 30.0):
            return []
        durs.append(dur)
    return durs, starts


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

    # 配音页：切片直录必须在页面加载前注入（平台的录音器在加载时就把 getUserMedia
    # 的旧引用存下来了，事后包装收不到调用 —— 实测录音期间 0 次调用记录）。所以
    # 这里先装（写 localStorage 开关 + 文档级注入 + 刷新页面），后面的等待/扫描/
    # 录音都在刷新后的新页面上进行。非配音页不装，注入脚本在开关关时也原样放行。
    slice_route_on = False
    try:
        if driver.find_elements(By.CSS_SELECTOR, '.question-video-dub-pc'):
            slice_route_on = _install_slice_mic(driver, verbose)
            if verbose:
                print(f"     跟读题：配音页切片直录预装 = {'✔' if slice_route_on else '✘'}"
                      "（已刷新页面）")
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

    # 配音页：示范音在视频里（每条 <audio> 里那个不足 1 秒的短音只是提示音，
    # 不是示范 —— 平台把整段视频按句切成若干条，点某条的播放键就会把视频跳到
    # 该段并播放）。先扫一遍每条的片段时长 —— 录音按这些边界播视频对应片段、
    # 到点就停；扫不到（结构不符/读不到）就退回按词数估时长。
    # 判定：页面上有视频 + 条目的行里有播放键 + 行里没有 .record-button-wrap
    # （音频跟读页是 record-button-wrap 那套行结构，不会被误判）。
    seg_durs = []
    seg_starts = []
    dub_page = False
    try:
        first_row = _row_of(mics[0])
        dub_page = bool(
            first_row is not None
            and driver.find_elements(By.TAG_NAME, 'video')
            and first_row.find_elements(By.CSS_SELECTOR, '.btn-play')
            and not first_row.find_elements(By.CSS_SELECTOR, '.record-button-wrap'))
        if dub_page:
            scanned = _scan_video_segments(driver, mics)
            if scanned:
                seg_durs, seg_starts = scanned
            if verbose and seg_durs:
                print("     跟读题：示范音在视频里，片段边界 " +
                      " / ".join(f"{d:.1f}s" for d in seg_durs))
            elif verbose:
                print("     跟读题：视频片段边界没扫到，退回按词数估时长")
    except Exception:
        seg_durs, seg_starts = [], []

    # 配音页：把整段视频音频交给切片播放器（路由已在页面加载时注入好）——
    # 录音时播哪一片、平台就录到哪一片。
    slice_ok = False
    if dub_page:
        vpath = ""
        try:
            vpath = _ensure_video_audio(driver, verbose)
        except Exception:
            vpath = ""
        if vpath and slice_route_on:
            try:
                import base64 as _b64
                with open(vpath, "rb") as fh:
                    _payload = _b64.b64encode(fh.read()).decode("ascii")
                slice_ok = _feed_slice_audio(driver, _payload, verbose)
            except Exception:
                slice_ok = False
        if verbose:
            if slice_ok:
                print(f"     跟读题：切片直录就绪（视频音频 {os.path.getsize(vpath) // 1024} KB）")
            else:
                print("     跟读题：切片直录不可用，退回点平台播放键（可能录进静音）")

    # 配音页：先空录一条「热身」——实测平台第一条录音要冷启动录音器，首录经常不
    # 产出（上传缺失、评分读不到）；热身录一条后，循环里的第 1 条重录即正常。
    if dub_page and mics:
        try:
            warm_mic = mics[0]
            driver.execute_script(
                "try{arguments[0].scrollIntoView({block:'center'});}catch(e){}", warm_mic)
            time.sleep(0.3)
            warm_mic.click()
            time.sleep(2.5)
            try:
                warm_mic.click()
            except Exception:
                from selenium.webdriver.common.action_chains import ActionChains
                ActionChains(driver).move_to_element(warm_mic).click().perform()
            time.sleep(2)
            if verbose:
                print("     跟读题：已空录一条热身（第 1 条稍后重录）")
        except Exception as exc:
            if verbose:
                print(f"     跟读题：热身录音失败（{str(exc)[:50]}）")

    # 切回环设备（有就切），跑完恢复。配音页走假麦克风直录，录音输入是文件，
    # 不需要回环、也不去动系统默认设备（少扰动）。
    route_on = False
    if not dub_page:
        try:
            import audio_route
            route_on = audio_route.use_vb_cable(verbose=verbose)
            if verbose and route_on:
                print("     跟读题：已自动打开 VB-Cable（录音题专用）")
        except Exception as exc:
            if verbose:
                print(f"     跟读题：VB-Cable 切换不可用（{str(exc)[:50]}）")
    elif verbose:
        print("     跟读题：配音页走假麦克风直录（不动系统默认设备）")

    results = []
    hits = fresh = 0
    try:
        for index, mic in enumerate(mics):
            # 页面每录完一条都会重排，统一重新取列表，保证每条用的都是当前 DOM 里的
            # 按钮。第 1 条尤其要重取：它以前用的是「扫描/热身前」抓到的旧元素 ——
            # 平台对失效按钮的点击不报错也不起录，导致第 1 条永远录不进去（实测）。
            try:
                current = mics_now()
                if index < len(current):
                    mic = current[index]
            except Exception:
                pass
            row = _row_of(mic)
            text = _text_of(row)
            audio_el = _demo_audio(row)
            src = ""
            try:
                src = (audio_el.get_attribute("src") or "") if audio_el else ""
            except Exception:
                src = ""
            if dub_page:
                # 配音页 <audio> 里的短音只是提示音，不能当示范缓存/播放 ——
                # 示范一律走视频片段（下面的播放分支会点它自己的播放键）
                src = ""
            else:
                src = _clean_demo_src(src)
            # 配音页这类页面，用句子词数估时长；扫到了视频片段边界的，用更准的片段时长
            dur = _duration_from_src(src, _estimate_seconds(text))
            if dub_page and index < len(seg_durs) and seg_durs[index]:
                dur = max(3.0, min(30.0, float(seg_durs[index])))

            # 配音页：这一片在点麦后播放（见下面的播放分支）
            seg_start = (seg_starts[index] if index < len(seg_starts) else None)

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
                # 配音页走「切片直录」，平台拿到的是我们合成的流，这一步不需要。
                if not dub_page:
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

                # 麦克风先滚到视口中央再点：页面底部的浮动「提交」条会盖住靠下的
                # 麦克风按钮，Selenium 普通点击会被判定 "element click intercepted"。
                # 仍被拦截时用 ActionChains（同样走 CDP 可信点击）再试一次。
                try:
                    driver.execute_script(
                        "try{arguments[0].scrollIntoView({block:'center', inline:'nearest'});}"
                        "catch(e){}", mic)
                    time.sleep(0.3)
                    try:
                        mic.click()
                    except Exception as exc:
                        if "intercepted" not in str(exc):
                            raise
                        from selenium.webdriver.common.action_chains import ActionChains
                        ActionChains(driver).move_to_element(mic).click().perform()
                except Exception as exc:
                    raise
                time.sleep(0.8)
                # 等录音真的开始再播音：实测平台起录要花时间（第 1 条尤其慢，2~4 秒），
                # 起录的可靠信号 = 该条的评分角标被清空（.score 变空）。不等就开始播，
                # 第 1 条经常整条录成静音/评分 0。（音频跟读页没有角标 → 立即通过。）
                if row is not None:
                    try:
                        for _ in range(20):          # 最多等 6 秒
                            try:
                                badge_text = (row.find_element(
                                    By.CSS_SELECTOR, '.score').text or '').strip()
                            except Exception:
                                badge_text = ""      # 没有角标 → 无需等待
                            if not badge_text:
                                break
                            time.sleep(0.3)
                    except Exception:
                        pass
                if dub_page:
                    # 切片直录：把播放器定位到该片起点并播放 —— 平台麦克风就是
                    # 这条 WebAudio 流，录到的就是正在播的这一片。
                    played = False
                    if slice_ok:
                        try:
                            driver.execute_script(
                                "try{"
                                "  if (!window.__sliceTrace) { window.__sliceTrace = []; }"
                                "  if (window.__ucCtx2 && window.__ucCtx2.state === 'suspended')"
                                "    { window.__ucCtx2.resume(); }"
                                "  var el = window.__ucSliceEl;"
                                "  window.__sliceTrace.push({i: arguments[1], ts: Date.now(),"
                                "    off: arguments[0],"
                                "    preT: el ? Math.round(el.currentTime * 10) / 10 : -1,"
                                "    prePaused: el ? el.paused : null,"
                                "    ctx: window.__ucCtx2 ? window.__ucCtx2.state : 'none'});"
                                "  if (el) { el.muted = false; el.volume = 1;"
                                "    try{ el.currentTime = arguments[0] || 0; }catch(e){}"
                                "    var p = el.play();"
                                "    if (p && p.catch) { p.catch(function(){}); } }"
                                "  window.__sliceTrace.push({i: arguments[1], ts: Date.now(),"
                                "    postT: el ? Math.round(el.currentTime * 10) / 10 : -1,"
                                "    postPaused: el ? el.paused : null,"
                                "    err: String(window.__ucSliceErr || '')});"
                                "  if (window.__sliceTrace.length > 300) { window.__sliceTrace ="
                                "    window.__sliceTrace.slice(-200); }"
                                "}catch(e){}",
                                float(seg_start or 0.0), int(index))
                            played = True
                        except Exception:
                            played = False
                    if not played and row is not None:
                        _play_segment(driver, row, start=seg_start)
                elif audio_el is not None and not payload:
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
                try:
                    mic.click()                                   # 停止录音
                except Exception as exc:
                    if "intercepted" not in str(exc):
                        raise
                    from selenium.webdriver.common.action_chains import ActionChains
                    ActionChains(driver).move_to_element(mic).click().perform()
                if dub_page:
                    # 停掉这一片的播放；顺带把视频也停住（扫描/平台可能让它还在放）
                    try:
                        driver.execute_script(
                            "try{ window.__ucSliceEl && window.__ucSliceEl.pause(); }catch(e){}"
                            "try{document.querySelector('video').pause();}catch(e){}")
                    except Exception:
                        pass
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
        try:
            # 切片直录收尾：停播放，撤掉 localStorage 开关与文档级注入
            # （撤掉后新打开的页面不再受影响；音频跟读页本就原样放行）
            driver.execute_script(
                "try{ localStorage.removeItem('__ucSliceRoute'); }catch(e){}"
                "try{ window.__ucSliceEl && window.__ucSliceEl.pause(); }catch(e){}")
        except Exception:
            pass
        try:
            sid = getattr(driver, "_ucSliceScriptId", "")
            if sid:
                driver.execute_cdp_cmd("Page.removeScriptToEvaluateOnNewDocument",
                                       {"identifier": sid})
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
