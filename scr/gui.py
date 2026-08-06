"""视频抽帧 - 配置窗口（HTML + webview-cli）
架构：Python 渲染带当前配置的 HTML → 调 webview-cli 打开原生窗口 →
用户在页面填表点保存 → JS 调 window.webview.resolve(config) →
webview-cli 把 JSON 原样打印到 stdout 后退出(exit 0) →
Python 捕获、二次校验、写 config.json。
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from scr.converter import (
    ALLOWED_FRAME_INTERVALS,
    ALLOWED_SECOND_INTERVALS,
    ALLOWED_SMART_THRESHOLDS,
    MODE_LABELS,
    MODE_ORDER,
    FrameExtractor,
)

# 常量单一来源：模式顺序与文案、各模式下拉选项都来自 converter.py
MODES = {k: MODE_LABELS[k] for k in MODE_ORDER}

DEFAULT_CONFIG = {
    "mode": "second",
    "interval": "5",
    "timestamps": "00:00:05,00:00:15,00:01:00",
    "threshold": "0.78",
    "output_dir": "",
    "overwrite": False,
    "smart_refine": False,
    "smart_open_eyes": False,
    "smart_start": "",
}

TEMPLATE_PATH = Path(__file__).parent.parent / "scr" / "config.html"
CONFIG_PATH = Path(__file__).parent.parent / "config.json"

# 模板里这个占位符会被替换为 `const APP_DATA = {...};`
APP_DATA_MARKER = "/*__APP_DATA__*/"


def _render_html(config) -> str:
    """读取 HTML 模板并注入 APP_DATA（常量单一来源在 Python）。"""
    data = {
        "saved": config,
        "DEFAULTS": DEFAULT_CONFIG,
        "MODES": MODES,
        "ALLOWED_SECOND_INTERVALS": ALLOWED_SECOND_INTERVALS,
        "ALLOWED_FRAME_INTERVALS": ALLOWED_FRAME_INTERVALS,
        "ALLOWED_SMART_THRESHOLDS": ALLOWED_SMART_THRESHOLDS,
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    return html.replace(APP_DATA_MARKER, f"const APP_DATA = {payload};")


def _validate_saved(data):
    """对页面返回的配置做二次校验（镜像 HTML 里的 JS 规则）。"""
    if not isinstance(data, dict):
        raise ValueError("返回的数据格式无效")
    mode = data.get("mode")
    if mode not in MODES:
        raise ValueError(f"无效的抽取方式：{mode}")

    if mode in ("second", "frame"):
        try:
            if int(float(data.get("interval", 0))) < 1:
                raise ValueError("间隔必须为不小于 1 的数字")
        except (ValueError, TypeError):
            raise ValueError("间隔必须为不小于 1 的数字")
    elif mode == "timestamp":
        raw = str(data.get("timestamps", "") or "").strip()
        parts = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        if not parts:
            raise ValueError("请填写至少一个时间点")
        for p in parts:
            try:
                FrameExtractor._to_seconds(p)
            except ValueError:
                raise ValueError(f"无效的时间格式：{p}")
    elif mode == "smart":
        try:
            threshold = float(data.get("threshold"))
        except (ValueError, TypeError):
            raise ValueError("相似度阈值无效")
        if threshold not in ALLOWED_SMART_THRESHOLDS:
            raise ValueError(f"不支持的相似度阈值：{data.get('threshold')}")
        start = str(data.get("smart_start", "") or "").strip()
        if start:
            try:
                start_val = float(start)
            except ValueError:
                raise ValueError("起始时间必须是数字（秒）")
            if start_val < 0:
                raise ValueError("起始时间不能为负数")


def _write_config(data) -> Path:
    data["overwrite"] = bool(data.get("overwrite"))
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return CONFIG_PATH


def _spawn_webview(base_cmd, html):
    """先尝试 stdin 管道传入 HTML；失败则回退到临时 HTML 文件。

    注意 webview 的输入优先级：非空 stdin 优先于位置参数。
    """
    common = dict(
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        return subprocess.run([*base_cmd], input=html, **common)
    except (OSError, ValueError):
        fd, path = tempfile.mkstemp(suffix=".html", prefix="video-frame-config-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(html)
            return subprocess.run([*base_cmd, path], input="", **common)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


def run_config_window(config) -> bool:
    """打开 HTML 配置窗口。返回 True 表示已保存配置，False 表示取消/出错。"""
    webview = shutil.which("webview-cli") or shutil.which("webview")
    if not webview:
        raise FileNotFoundError(
            "未找到 webview-cli，请确认已安装并加入 PATH\n"
            "https://github.com/just-be-dev/webview-cli"
        )

    html = _render_html(config)
    base_cmd = [webview, "--title", "视频抽帧 - 配置窗口", "--width", "500", "--height", "720"]
    proc = _spawn_webview(base_cmd, html)

    if proc.returncode == 0:
        try:
            data = json.loads(proc.stdout)
        except ValueError as e:
            print(f"配置窗口返回的数据无法解析：{e}")
            return False
        try:
            _validate_saved(data)
        except (ValueError, TypeError) as e:
            print(f"配置校验失败：{e}")
            return False
        _write_config(data)
        return True
    elif proc.returncode == 2:
        return False  # 用户直接关窗 = 取消
    else:
        # reject(1) / 超时(3) / 用法错误(64)
        msg = (proc.stderr or "").strip()
        if msg:
            print(msg)
        return False


def load_config():
    """加载配置文件，不存在则返回默认配置。"""
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config.update(json.load(f))
        except Exception:
            pass
    return config


if __name__ == "__main__":
    run_config_window(load_config())
