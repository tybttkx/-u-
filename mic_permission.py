# -*- coding: utf-8 -*-
"""麦克风权限：只在脚本运行期间给浏览器放行，跑完自动恢复。

Windows 把「哪些桌面程序能用麦克风」记录在注册表里（就是"设置 → 隐私和安全性 →
麦克风"界面里那些开关）：

    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\
        ConsentStore\\microphone\\NonPackaged\\<可执行文件路径，反斜杠换成 #>

Value = "Allow" / "Deny"。这里做的事和你在设置界面里点一下"允许"完全等价，
不是绕过系统安全，也不碰任何其它程序：

  * 运行脚本前：把浏览器（msedge.exe / chrome.exe）那一项写成 Allow，并记下原值；
  * 脚本结束时：把原值写回去（原来没有这一项就删掉，恢复成系统默认）。

原值会先落盘到 %TEMP%\\unipus_mic_permission_snapshot.json（进程无关）：万一脚本被强杀
没走到"脚本结束时"，下次启动（导入本模块后创建 MicPermission）会自动把注册表写回原值，
写完清掉该文件。

另外浏览器自己还有一层页面级权限，由 Selenium 通过 CDP 直接给本次会话放行
（grant_browser_permission），两层都放行后 U校园 的录音题才能直接收音。
"""
import json
import logging
import os
import tempfile
from typing import Optional

logger = logging.getLogger("UCampusBot")

CONSENT_ROOT = (r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager"
                r"\ConsentStore\microphone")
BROWSER_EXES = ("msedge.exe", "chrome.exe")

#: 崩溃自愈快照：改动前的原值先落到 %TEMP%，进程被强杀后下次启动还能把注册表恢复回去
SNAPSHOT_FILE = "unipus_mic_permission_snapshot.json"


def _snapshot_path() -> str:
    return os.path.join(tempfile.gettempdir(), SNAPSHOT_FILE)


def _load_snapshot() -> dict:
    """读回残留快照。不存在返回空 dict；损坏/为空则删掉该文件（留着只会反复报错）。"""
    path = _snapshot_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        saved = data.get("saved") if isinstance(data, dict) else None
        if not isinstance(saved, dict) or not saved:
            raise ValueError("快照为空或格式不正确")
        return {str(key): value for key, value in saved.items()}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning(f"麦克风：读取残留原值快照失败，已忽略: {str(exc)[:80]}")
        _remove_snapshot()
        return {}


def _save_snapshot(saved: dict) -> None:
    """把"改动前的原值"（进程无关的设备原值）持久化到 %TEMP%；写不进去只 warning。"""
    try:
        with open(_snapshot_path(), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "saved": saved}, f, ensure_ascii=False)
    except Exception as exc:
        logger.warning(f"麦克风：原值快照写入失败（不影响本次运行）: {str(exc)[:80]}")


def _remove_snapshot() -> None:
    """全部恢复成功后清掉快照；文件本来就不存在不算错。"""
    try:
        os.remove(_snapshot_path())
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning(f"麦克风：删除原值快照失败: {str(exc)[:80]}")


def _winreg():
    try:
        import winreg  # noqa: PLC0415
        return winreg
    except ImportError:
        return None


def _app_key(executable_path: str) -> str:
    """注册表里子项的名字：完整路径，反斜杠换成 #（系统就是这么存的）。"""
    return executable_path.replace("\\", "#")


def find_browser_executables() -> list:
    """找出本机 Edge / Chrome 的 exe 路径。"""
    found = []
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            found.append(path)
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        for rel in (r"Microsoft\Edge\Application\msedge.exe",
                    r"Google\Chrome\Application\chrome.exe"):
            path = os.path.join(local, rel)
            if os.path.isfile(path) and path not in found:
                found.append(path)
    return found


class MicPermission:
    """脚本运行期间的麦克风放行开关（可撤销）。"""

    def __init__(self, executable: Optional[str] = None):
        self.executables = [executable] if executable else find_browser_executables()
        self._saved: dict = {}      # exe → 原值（None 表示原来没有这一项）
        # 上次运行被强杀（没走到 restore）时会在 %TEMP% 留下原值快照：先尽力恢复
        self._recover_leftover_snapshot()

    # -- 崩溃自愈 ---------------------------------------------------------

    def _recover_leftover_snapshot(self) -> None:
        """发现残留快照就尽力把注册表写回原值（best effort，任何失败只 warning）。"""
        saved = _load_snapshot()
        if not saved:
            return
        winreg = _winreg()
        if winreg is None:
            logger.warning("麦克风：发现上次残留的原值快照，但当前取不到注册表模块，暂不恢复")
            return
        remaining = {}
        for path, previous in saved.items():
            try:
                if previous is None:
                    self._delete(path, winreg)
                else:
                    self._write(path, previous, winreg)
            except Exception as exc:
                remaining[str(path)] = previous
                logger.warning(f"麦克风：自愈恢复 {path} 失败: {str(exc)[:80]}")
        if remaining:
            _save_snapshot(remaining)   # 失败的项留在快照里，下次启动再试
            logger.warning(f"麦克风：上次运行残留的 {len(remaining)} 项设置本次未能恢复")
        else:
            _remove_snapshot()
            logger.info(f"麦克风：已把上次运行残留的 {len(saved)} 项设置恢复为原值")

    # -- 对外接口 ---------------------------------------------------------

    def enable(self, dry_run: bool = False) -> bool:
        """给浏览器放开麦克风。返回是否至少改成功一项。

        dry_run 只预演、不写注册表，保留给 _self_test / 手动排查使用；
        生产调用方（UCampusBot.start）只调用 enable()。
        """
        if not self.executables:
            logger.info("麦克风：没找到 Edge/Chrome 可执行文件，跳过系统层设置")
            return False
        winreg = _winreg()
        if winreg is None:
            logger.info("麦克风：当前不是 Windows 或取不到注册表模块，跳过")
            return False

        ok = False
        for path in self.executables:
            try:
                previous = self._read(path, winreg)
                if previous == "Allow":
                    logger.info(f"麦克风：{os.path.basename(path)} 本来就是允许，无需改动")
                    continue
                if dry_run:
                    logger.info(f"麦克风[dry-run]：会把 {os.path.basename(path)} 设为 Allow"
                                f"（原值 {previous!r}）")
                    ok = True
                    continue
                # 先落快照、再改注册表：改完瞬间被强杀时，下次启动还能靠快照恢复原值
                self._saved[path] = previous
                _save_snapshot(self._saved)
                self._write(path, "Allow", winreg)
                logger.info(f"麦克风：已允许 {os.path.basename(path)} 使用麦克风"
                            f"（运行结束后恢复原值 {previous!r}）")
                ok = True
            except Exception as exc:
                logger.warning(f"麦克风：设置 {path} 失败（不影响其它流程）: {str(exc)[:80]}")
        return ok

    def restore(self, dry_run: bool = False) -> bool:
        """把改过的值恢复原样。原来没有这一项就删掉，回到系统默认。

        逐项恢复：只有确实恢复成功的项才从记录里移除，失败的项保留并 warning，
        下次调用还能再试 —— 之前不论成败一律 clear()，失败项就再也恢复不了了。
        dry_run 只预演：不改注册表，也不清记录。
        """
        winreg = _winreg()
        if winreg is None or not self._saved:
            return False
        ok = False
        for path, previous in list(self._saved.items()):
            if dry_run:
                logger.info(f"麦克风[dry-run]：会恢复 {os.path.basename(path)} → {previous!r}")
                ok = True
                continue
            try:
                if previous is None:
                    self._delete(path, winreg)
                    logger.info(f"麦克风：已恢复 {os.path.basename(path)}（删除脚本添加的允许项）")
                else:
                    self._write(path, previous, winreg)
                    logger.info(f"麦克风：已恢复 {os.path.basename(path)} → {previous!r}")
                self._saved.pop(path, None)
                ok = True
            except Exception as exc:
                logger.warning(f"麦克风：恢复 {path} 失败: {str(exc)[:80]}")
        if not dry_run:
            if self._saved:
                _save_snapshot(self._saved)   # 还有没恢复成功的项，刷新快照留待下次
            else:
                _remove_snapshot()            # 全部恢复完毕，清掉崩溃自愈快照
        return ok

    # -- 注册表读写 -------------------------------------------------------

    @staticmethod
    def _read(path: str, winreg) -> Optional[str]:
        key_path = f"{CONSENT_ROOT}\\NonPackaged\\{_app_key(path)}"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "Value")
                return value
        except FileNotFoundError:
            return None

    @staticmethod
    def _write(path: str, value: str, winreg) -> None:
        key_path = f"{CONSENT_ROOT}\\NonPackaged"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE):
            pass
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                f"{key_path}\\{_app_key(path)}", 0,
                                winreg.KEY_SET_VALUE):
            pass
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            f"{key_path}\\{_app_key(path)}", 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "Value", 0, winreg.REG_SZ, value)

    @staticmethod
    def _delete(path: str, winreg) -> None:
        key_path = f"{CONSENT_ROOT}\\NonPackaged\\{_app_key(path)}"
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        except FileNotFoundError:
            pass


def grant_browser_permission(driver) -> bool:
    """让本次浏览器会话直接放行麦克风（页面级权限，不用弹窗）。"""
    if driver is None:
        return False
    try:
        driver.execute_cdp_cmd("Browser.grantPermissions", {
            "origin": None,
            "permissions": ["audioCapture"],
        })
        logger.info("麦克风：本次浏览器会话已放行录音权限")
        return True
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Browser.grantPermissions", {
            "origin": "https://uai.unipus.cn",
            "permissions": ["audioCapture"],
        })
        logger.info("麦克风：已为 uai.unipus.cn 放行录音权限")
        return True
    except Exception as exc:
        logger.info(f"麦克风：浏览器会话放行失败（可能是非 Chromium 内核）: {str(exc)[:60]}")
        return False


def _self_test() -> None:
    """手动验证：python mic_permission.py 看设置与恢复过程（会真的读写注册表）。"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    mic = MicPermission()
    print("找到的浏览器:", mic.executables or "（无）")
    print("--- 预演（不改动） ---")
    mic.enable(dry_run=True)
    print("--- 实际开启 ---")
    mic.enable()
    print("--- 恢复 ---")
    mic.restore()


if __name__ == "__main__":
    _self_test()
