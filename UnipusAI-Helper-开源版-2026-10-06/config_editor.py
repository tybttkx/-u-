# -*- coding: utf-8 -*-
import json
import os
import shutil
import sys

from knowledge_base import KnowledgeBase, default_root as knowledge_root
from PyQt5.QtCore import QEvent, QPoint, Qt, QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CardWidget,
    CheckBox,
    ComboBox,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBox,
    PlainTextEdit,
    PrimaryPushButton,
    ScrollArea,
    Theme,
    ToolButton,
    setTheme,
)


if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
# 最大 token 数默认值：与 config.example.json / README 保持一致
DEFAULT_MAX_TOKENS = 8192
DEFAULT_CONFIG = {
    "username": "",
    "password": "",
    "url": "https://uai.unipus.cn/sso/index.html?service=https%3A%2F%2Fucloud.unipus.cn%2Fhome",
    "api_key": "",
    "base_url": "",
    "model": "",
    "max_tokens": DEFAULT_MAX_TOKENS,
    "temperature": 0.3,
    "knowledge_enabled": True,
    "knowledge_textbook": "auto",
    "knowledge_min_confidence": "medium",
    "knowledge_verify_wordbank": True,
    "debug_mode": False,
}

PALETTE = {
    "bg": "#17191d",
    "panel": "#202328",
    "panel_border": "#2c3138",
    "top": "#202328",
    "input": "#14171b",
    "input_border": "#303640",
    "text": "#f4f7fb",
    "muted": "#a7b0bb",
}


# load_config 解析失败时的错误信息（None 表示正常），供界面做可见提示
_load_error = None


def load_config():
    """读取 config.json；解析失败时记录错误并返回默认值（由界面提示用户）。"""
    global _load_error
    _load_error = None
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            return merged
        except Exception as e:
            # 不能静默吞掉：否则用户点一次保存就可能覆盖坏掉的配置文件
            _load_error = str(e)
    return dict(DEFAULT_CONFIG)


def save_config(data: dict):
    """读-改-写保存：先合并现有 config.json，避免删除表单之外的字段。"""
    out = dict(data)
    token = out.get("token_full", "")
    if isinstance(token, str):
        # 只去首尾空白；json.dump 本身正确处理转义，不剥引号（避免破坏含引号的真实值）
        out["token_full"] = token.strip()

    base = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
            if not isinstance(old, dict):
                raise ValueError("config.json 顶层不是 JSON 对象")
            base = old
        except Exception as e:
            # 解析失败：保留现场，中止保存（不覆盖原文件）
            raise RuntimeError(f"config.json 解析失败，已中止保存以保留原文件：{e}")

    base.update(out)

    # 写前备份，覆盖旧 config.json.bak（不动 .bak_keys / .bak_swap）
    if os.path.exists(CONFIG_PATH):
        shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak")

    # 临时文件 + os.replace 原子替换，避免写一半损坏配置
    tmp_path = CONFIG_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(base, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise
    return True


class ConfigEditor(QWidget):
    def __init__(self):
        self._qt_app = QApplication.instance() or QApplication(sys.argv)
        setTheme(Theme.DARK)
        super().__init__()
        self.cfg = load_config()
        self._config_load_error = _load_error
        self._load_error_warned = False
        self.entries = {}
        self._dirty = False
        self._close_confirmed = False
        self._drag_active = False
        self._drag_position = QPoint()
        self._init_window()
        self._build_ui()
        self._apply_theme()
        self._connect_dirty_tracking()

    def _init_window(self):
        self.setObjectName("windowRoot")
        self.setWindowTitle("UnipusAI Helper 配置编辑器")
        self.resize(820, 860)
        self.setMinimumSize(700, 720)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 18, 28, 24)
        root.setSpacing(14)

        root.addWidget(self._build_top_strip())
        root.addWidget(self._build_form_area(), 1)

    def _build_top_strip(self):
        self.top_strip = QFrame()
        self.top_strip.setObjectName("topStrip")
        self.top_strip.installEventFilter(self)
        layout = QHBoxLayout(self.top_strip)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(12)

        self.title_label = QLabel("UnipusAI Helper 配置编辑器")
        self.title_label.setObjectName("pageTitle")
        self.title_label.installEventFilter(self)
        layout.addWidget(self.title_label)
        layout.addStretch(1)

        self.close_btn = ToolButton(FIF.CLOSE)
        self.close_btn.setToolTip("关闭")
        self.close_btn.setFixedSize(32, 32)
        self.close_btn.clicked.connect(self.close)
        layout.addWidget(self.close_btn)
        return self.top_strip

    def _build_form_area(self):
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        content = QWidget()
        content.setObjectName("contentHost")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(14)

        form_card, form = self._form_card()
        self._add_text_row(form, "username", "账号", "输入 U校园AI版账号")
        self._add_text_row(form, "password", "密码", "输入 U校园AI版密码", echo_password=True)
        self._add_text_row(form, "url", "登录地址", "https://uai.unipus.cn/sso/index.html?service=https%3A%2F%2Fucloud.unipus.cn%2Fhome")
        self._add_text_row(form, "api_key", "API Key", "")
        self._add_text_row(form, "base_url", "API 地址", "兼容OpenAI接口")
        self._add_text_row(form, "model", "模型名称", "")
        self._add_text_row(form, "max_tokens", "最大 Token 数", str(DEFAULT_MAX_TOKENS))
        self._add_text_row(form, "temperature", "温度 (0-2)", "0.3")
        self._add_text_row(
            form,
            "token_full",
            "Token (反作弊)",
            "从浏览器控制台获取 localStorage.getItem('__token')（复制时选 Copy string contents 取原始值）",
            multiline=True,
        )

        self.debug_check = CheckBox("开启调试输出")
        self.debug_check.setChecked(bool(self.cfg.get("debug_mode", False)))
        self.entries["debug_mode"] = self.debug_check
        form.addRow(self._field_label("调试模式"), self.debug_check)
        content_layout.addWidget(form_card)

        # ---- 本地题库知识库 ----
        kb_card, kb_form = self._form_card()
        kb_hint = QLabel(
            "命中本地答案库的题目直接用本地答案填写，未命中的题继续交给 AI。\n"
            f"当前答案库：{self._available_textbooks()}"
        )
        kb_hint.setObjectName("hintText")
        kb_hint.setWordWrap(True)
        kb_form.addRow(kb_hint)

        self.kb_check = CheckBox("启用本地题库知识库")
        self.kb_check.setChecked(bool(self.cfg.get("knowledge_enabled", True)))
        self.entries["knowledge_enabled"] = self.kb_check
        kb_form.addRow(self._field_label("知识库"), self.kb_check)

        self._add_text_row(
            kb_form,
            "knowledge_textbook",
            "教材",
            "auto = 自动识别；也可填写教材名，如：新编大学英语 综合教程3",
        )

        self.kb_confidence = ComboBox()
        self.kb_confidence.addItems(["low", "medium", "high"])
        current = str(self.cfg.get("knowledge_min_confidence", "medium") or "medium")
        self.kb_confidence.setCurrentText(current if current in ("low", "medium", "high") else "medium")
        self.entries["knowledge_min_confidence"] = self.kb_confidence
        kb_form.addRow(self._field_label("最低置信度"), self.kb_confidence)

        self.kb_wordbank_check = CheckBox("选词填空要求词库一致才使用本地答案")
        self.kb_wordbank_check.setChecked(bool(self.cfg.get("knowledge_verify_wordbank", True)))
        self.entries["knowledge_verify_wordbank"] = self.kb_wordbank_check
        kb_form.addRow(self._field_label("词库校验"), self.kb_wordbank_check)
        content_layout.addWidget(kb_card)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self.save_btn = PrimaryPushButton("保存配置")
        self.save_btn.clicked.connect(self._on_save)
        action_row.addWidget(self.save_btn)
        content_layout.addLayout(action_row)

        scroll.setWidget(content)
        return scroll

    def _form_card(self):
        card = CardWidget()
        card.setObjectName("panelCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setFormAlignment(Qt.AlignTop)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(14)
        layout.addLayout(form)
        return card, form

    def _field_label(self, title_text):
        title = QLabel(title_text)
        title.setObjectName("fieldTitle")
        return title

    @staticmethod
    def _available_textbooks():
        """列出 knowledge/ 里实际有的教材，方便填写「教材」一栏。"""
        try:
            books = KnowledgeBase(root=knowledge_root(), verbose=False).available_books()
        except Exception:
            return "（未能读取 knowledge/ 目录）"
        if not books:
            return "（knowledge/ 目录下没有答案文件）"
        return "、".join(books)

    def _create_input(self, key, placeholder, multiline=False, echo_password=False):
        if multiline:
            entry = PlainTextEdit()
            entry.setFixedHeight(110)
            entry.setPlaceholderText(placeholder)
            entry.setPlainText(str(self.cfg.get(key, "") or ""))
        else:
            entry = LineEdit()
            entry.setPlaceholderText(placeholder)
            entry.setText(str(self.cfg.get(key, "") or ""))
            entry.setClearButtonEnabled(True)
            if echo_password:
                # 密码框掩码回显；qfluentwidgets 的 LineEdit 继承 QLineEdit
                entry.setEchoMode(QLineEdit.Password)
        self.entries[key] = entry
        return entry

    def _add_text_row(self, form, key, title, placeholder, multiline=False, echo_password=False):
        entry = self._create_input(
            key, placeholder, multiline=multiline, echo_password=echo_password
        )
        form.addRow(self._field_label(title), entry)

    def _apply_theme(self):
        self.setStyleSheet(
            f"""
            QWidget#windowRoot {{
                background: {PALETTE['bg']};
            }}
            QWidget {{
                background: transparent;
                font-family: 'Segoe UI', 'Microsoft YaHei UI', 'Microsoft YaHei';
                font-size: 14px;
                color: {PALETTE['text']};
            }}
            QWidget#topStrip {{
                background: transparent;
                border: none;
            }}
            CardWidget#panelCard {{
                background: {PALETTE['panel']};
                border: 1px solid {PALETTE['panel_border']};
                border-radius: 10px;
            }}
            QLabel#pageTitle {{
                background: transparent;
                color: {PALETTE['text']};
                font-size: 24px;
                font-weight: 800;
            }}
            QLabel#fieldTitle {{
                background: transparent;
                color: {PALETTE['text']};
                font-size: 14px;
                font-weight: 700;
                padding-top: 8px;
            }}
            QLabel#hintText {{
                background: transparent;
                color: {PALETTE['muted']};
                font-size: 13px;
                padding: 2px 0 8px 0;
            }}
            ScrollArea, QScrollArea {{
                background: transparent;
                border: none;
            }}
            LineEdit, PlainTextEdit {{
                background: {PALETTE['input']};
                border: 1px solid {PALETTE['input_border']};
                border-radius: 8px;
                color: {PALETTE['text']};
                selection-background-color: #2a6df4;
                padding: 8px 10px;
            }}
            PlainTextEdit {{
                font-family: 'Cascadia Mono', Consolas, 'Microsoft YaHei UI';
            }}
            CheckBox, QCheckBox {{
                color: {PALETTE['text']};
                padding: 6px 0;
                font-size: 14px;
                font-weight: 600;
                spacing: 8px;
            }}
            """
        )

    def _connect_dirty_tracking(self):
        """给各输入控件接信号，任何改动都置脏标记（用于关闭前提醒未保存）。"""
        for entry in self.entries.values():
            if isinstance(entry, (CheckBox, QCheckBox)):
                entry.stateChanged.connect(self._mark_dirty)
            elif isinstance(entry, ComboBox):
                entry.currentTextChanged.connect(self._mark_dirty)
            elif isinstance(entry, PlainTextEdit):
                entry.textChanged.connect(self._mark_dirty)
            else:
                entry.textChanged.connect(self._mark_dirty)

    def _mark_dirty(self, *_args):
        self._dirty = True

    def _confirm_close(self):
        """有未保存修改时弹确认，返回 True 表示可以关闭。"""
        if not self._dirty or self._close_confirmed:
            return True
        box = MessageBox("未保存的修改", "有未保存的修改，确定关闭？", self)
        box.yesButton.setText("关闭")
        box.cancelButton.setText("取消")
        if not bool(box.exec()):
            return False
        self._close_confirmed = True
        return True

    def eventFilter(self, obj, event):
        if obj in (getattr(self, "top_strip", None), getattr(self, "title_label", None)):
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag_active = True
                self._drag_position = event.globalPos() - self.frameGeometry().topLeft()
                return True
            if event.type() == QEvent.MouseMove and self._drag_active and event.buttons() & Qt.LeftButton:
                self.move(event.globalPos() - self._drag_position)
                return True
            if event.type() == QEvent.MouseButtonRelease:
                self._drag_active = False
                return True
        return super().eventFilter(obj, event)

    def _on_save(self):
        data = {}
        for key, entry in self.entries.items():
            if isinstance(entry, (CheckBox, QCheckBox)):
                data[key] = entry.isChecked()
            elif isinstance(entry, ComboBox):
                data[key] = entry.currentText().strip()
            elif isinstance(entry, PlainTextEdit):
                data[key] = entry.toPlainText().strip()
            else:
                data[key] = entry.text().strip()

        try:
            data["max_tokens"] = int(data["max_tokens"])
        except Exception:
            data["max_tokens"] = DEFAULT_MAX_TOKENS

        try:
            data["temperature"] = float(data["temperature"])
        except Exception:
            data["temperature"] = 0.3

        try:
            save_config(data)
            self._dirty = False
            InfoBar.success(
                "保存成功",
                "配置已保存到 config.json",
                duration=1800,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )
            try:
                import winsound

                winsound.MessageBeep()
            except Exception:
                pass
        except Exception as e:
            MessageBox("保存失败", str(e), self).exec()
            InfoBar.error(
                "保存失败",
                str(e),
                duration=2400,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if self._confirm_close():
                self.close()
            return
        if event.key() == Qt.Key_S and event.modifiers() & Qt.ControlModifier:
            self._on_save()
            return
        super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        # 窗口显示后再提示解析失败，避免静默（用户不知道当前显示的是默认值）
        if self._config_load_error and not self._load_error_warned:
            self._load_error_warned = True
            QTimer.singleShot(0, self._warn_load_error)

    def _warn_load_error(self):
        InfoBar.warning(
            "配置读取失败",
            "config.json 解析失败，当前显示为默认值；保存已被保护性中止，"
            f"不会覆盖原文件。\n原因：{self._config_load_error}",
            duration=-1,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def closeEvent(self, event):
        if self._confirm_close():
            event.accept()
            return
        event.ignore()

    def run(self):
        self.show()
        return self._qt_app.exec_()


if __name__ == "__main__":
    editor = ConfigEditor()
    editor.run()
