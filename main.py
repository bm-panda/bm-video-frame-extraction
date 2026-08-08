"""视频抽帧 - 从视频中抽取帧保存为 PNG 图片
惰性配置：右键选中视频时若未配置则先弹窗引导，配置后自动保存；已配置则直接抽帧
支持：智能 / 按秒 / 固定间隔帧 / 指定时间点 / 关键帧
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
CONFIG_TEMPLATE = BASE_DIR / "config.html"
APP_DATA_MARKER = "/*__APP_DATA__*/"

# ── 抽帧模式定义 ──
ALLOWED_MODES = {"second", "frame", "timestamp", "keyframe", "smart"}
MODE_ORDER = ["smart", "second", "frame", "timestamp", "keyframe"]
MODE_LABELS = {
    "smart": "智能抽帧",
    "second": "按秒抽帧",
    "frame": "固定间隔帧",
    "timestamp": "指定时间点",
    "keyframe": "关键帧",
}
MODES = {k: MODE_LABELS[k] for k in MODE_ORDER}

# 各模式可选参数（GUI 下拉选项）
ALLOWED_SECOND_INTERVALS = [1, 2, 3, 5, 10, 15, 30, 60]
ALLOWED_FRAME_INTERVALS = [5, 10, 12, 15, 25, 30, 60, 120, 240]
# 智能抽帧相似度阈值（与上一张已存帧比较，越高保留帧越多）
ALLOWED_SMART_THRESHOLDS = [0.5, 0.6, 0.65, 0.7, 0.75, 0.78, 0.8, 0.85, 0.9, 0.95]

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".ts", ".m4v",
              ".mpg", ".mpeg", ".m2ts", ".mts", ".3gp", ".ogv", ".vob", ".rmvb", ".rm", ".asf"}
IMAGE_FORMAT = "png"

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


class FrameExtractor:
    """基于 ffmpeg / distant_frames 的视频抽帧器（承载通用 subprocess 执行）。"""

    @staticmethod
    def _run(cmd, **kw):
        """执行命令，默认隐藏控制台窗口、按 UTF-8 容错解码。"""
        kw.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
        kw.setdefault("encoding", "utf-8")
        return subprocess.run(cmd, text=True, errors="replace", **kw)

    @staticmethod
    def _popen(cmd, **kw):
        """启动命令进程（实时进度用），默认隐藏控制台窗口、UTF-8 容错解码。"""
        kw.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
        kw.setdefault("encoding", "utf-8")
        return subprocess.Popen(cmd, text=True, errors="replace", **kw)

    @staticmethod
    def _ffmpeg_bin():
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise FileNotFoundError("未找到 FFmpeg（ffmpeg 命令），请确认已安装并在环境变量中")
        return ffmpeg

    def __init__(self, videos, config):
        self.videos = videos
        self.output_dir = str(config.get("output_dir") or "").strip()

        # 非法参数值回退默认
        self.mode = config.get("mode") if config.get("mode") in ALLOWED_MODES else "second"
        try:
            self.interval = max(1, int(float(config.get("interval"))))
        except (TypeError, ValueError):
            self.interval = 5
        try:
            self.threshold = float(config.get("threshold"))
        except (TypeError, ValueError):
            self.threshold = 0.78
        if self.threshold not in ALLOWED_SMART_THRESHOLDS:
            self.threshold = 0.78
        try:
            self.start_time = max(0.0, float(config.get("smart_start")))
        except (TypeError, ValueError):
            self.start_time = 0.0
        self.refine = bool(config.get("smart_refine"))
        self.open_eyes = bool(config.get("smart_open_eyes"))
        if self.mode == "timestamp":
            try:
                self.timestamps = self._parse_timestamps(config.get("timestamps"))
            except ValueError:
                self.timestamps = []
        else:
            self.timestamps = []
        self.overwrite = bool(config.get("overwrite"))

        self._ffmpeg = self._ffmpeg_bin()
        self._ffprobe = shutil.which("ffprobe")

        # 创建输出目录
        if self.output_dir:
            os.makedirs(self.output_dir, exist_ok=True)

    # ==================== 时间点解析 ====================
    @staticmethod
    def _to_seconds(text: str) -> float:
        """把 HH:MM:SS / MM:SS / 纯秒 转成秒数"""
        secs = 0.0
        for part in text.split(":"):
            part = part.strip()
            if not part:
                raise ValueError("时间格式无效")
            secs = secs * 60 + float(part)
        return secs

    @classmethod
    def _parse_timestamps(cls, text: str) -> list:
        """解析逗号分隔的时间点，返回 [(原始串, 秒数), ...]"""
        out = []
        for part in (text or "").split(","):
            part = part.strip()
            if not part:
                continue
            out.append((part, cls._to_seconds(part)))
        if not out:
            raise ValueError("未指定任何时间点")
        return out

    @staticmethod
    def _ts_hms(seconds: float) -> str:
        """秒数 → HH:MM:SS.S 显示串"""
        h = int(seconds // 3600)
        m = int(seconds % 3600 // 60)
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02.1f}"

    @staticmethod
    def _ts_filename(seconds: float) -> str:
        """秒数 → 图片文件名（HH-MM-SS.png）"""
        h = int(seconds // 3600)
        m = int(seconds % 3600 // 60)
        s = seconds % 60
        if s == int(s):
            return f"{h:02d}-{m:02d}-{int(s):02d}.{IMAGE_FORMAT}"
        return f"{h:02d}-{m:02d}-{s:05.2f}.{IMAGE_FORMAT}"

    # ==================== 基础方法 ====================
    def _get_duration(self, input_path: str):
        """探测输入视频总时长（秒），失败返回 None"""
        if not self._ffprobe:
            return None
        try:
            proc = self._run(
                [self._ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", input_path],
                capture_output=True,
            )
            if proc.returncode == 0:
                try:
                    return float(proc.stdout.strip())
                except ValueError:
                    return None
        except Exception:
            pass
        return None

    def _get_output_dir(self, input_path: str) -> str:
        """输出文件夹：每视频一个 <视频名>_frames 子文件夹"""
        base = self.output_dir or str(Path(input_path).parent)
        return os.path.join(base, f"{Path(input_path).stem}_frames")

    def _build_select_filter(self) -> str:
        """构建 select 滤镜表达式（单引号包裹，逗号在函数参数内由 ffmpeg 表达式解析）"""
        if self.mode == "second":
            return f"select='isnan(prev_selected_t)+gte(t,prev_selected_t+{self.interval})'"
        if self.mode == "frame":
            return f"select='eq(n,0)+not(mod(n,{self.interval}))'"
        if self.mode == "keyframe":
            return "select='eq(pict_type,PICT_TYPE_I)'"
        raise ValueError(f"不支持的抽取方式：{self.mode}")

    def _extract_by_filter(self, video_path: str, out_dir: str, on_progress=None):
        """单次 select 滤镜抽帧，返回 None 表示成功，否则返回错误信息字符串"""
        total_us = None
        if on_progress:
            duration = self._get_duration(video_path)
            if duration:
                total_us = duration * 1_000_000

        out_pattern = os.path.join(out_dir, f"frame_%06d.{IMAGE_FORMAT}")
        cmd = [self._ffmpeg, "-y", "-i", video_path]
        cmd += ["-vf", self._build_select_filter()]
        cmd += ["-vsync", "vfr"]
        cmd += ["-map_metadata", "0", "-loglevel", "error"]
        if on_progress:
            # -progress 将编码进度输出到 stdout，错误信息仍走 stderr
            cmd += ["-nostats", "-progress", "pipe:1"]
        cmd.append(out_pattern)

        proc = self._popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1)

        # 后台线程排空 stderr，避免管道满阻塞
        stderr_lines = []
        def _read_stderr():
            try:
                for line in proc.stderr:
                    stderr_lines.append(line)
            except Exception:
                pass
        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        # 实时解析 stdout 进度（out_time_us 为微秒）
        last_pct = -1.0
        for line in proc.stdout:
            if not line.startswith("out_time_us="):
                continue
            try:
                cur_us = int(line.split("=", 1)[1])
            except ValueError:
                continue
            pct = min(100.0, cur_us / total_us * 100.0) if total_us else None
            # 按 0.5% 粒度回调，避免刷屏
            if pct is None or pct - last_pct >= 0.5 or pct >= 100.0:
                last_pct = pct
                secs = cur_us / 1_000_000
                time_str = f"{int(secs // 3600):02d}:{int(secs % 3600 // 60):02d}:{secs % 60:04.1f}"
                if on_progress:
                    on_progress(pct, time_str)

        proc.wait()
        stderr_thread.join()

        if proc.returncode != 0:
            return "".join(stderr_lines).strip() or "抽帧失败"
        return None

    def _extract_by_timestamps(self, video_path: str, out_dir: str, on_progress=None):
        """指定时间点抽帧：逐时间点 -ss + -frames:v 1；返回 None 成功，否则错误信息"""
        total = len(self.timestamps)
        for i, (ts_str, seconds) in enumerate(self.timestamps, 1):
            out_file = os.path.join(out_dir, self._ts_filename(seconds))
            cmd = [self._ffmpeg, "-y", "-ss", ts_str, "-i", video_path,
                   "-frames:v", "1", "-map_metadata", "0", "-loglevel", "error",
                   out_file]
            proc = self._run(cmd, capture_output=True)
            if proc.returncode != 0:
                return proc.stderr.strip() or "抽帧失败"
            if on_progress:
                on_progress(i / total * 100.0, self._ts_hms(seconds))
        return None

    @staticmethod
    def _jpg_to_png(out_dir: str) -> bool:
        """把目录内 jpg 逐张转成同名 png，删除原 jpg；失败返回 False"""
        try:
            import cv2
        except ImportError:
            return False
        for f in os.listdir(out_dir):
            if f.lower().endswith(".jpg"):
                src = os.path.join(out_dir, f)
                img = cv2.imread(src)
                if img is not None:
                    cv2.imwrite(os.path.join(out_dir, f[:-4] + ".png"), img)
                os.remove(src)
        return True

    def _extract_by_smart(self, video_path: str, out_dir: str, on_progress=None):
        """智能抽帧（distant-frames 包）：与上一张已存帧比相似度去重；返回 None 成功，否则错误信息"""
        if importlib.util.find_spec("distant_frames") is None:
            return "智能抽帧需要 Python 3.12+ 与 distant-frames 包，请先安装依赖"

        duration = self._get_duration(video_path)
        # cv2 在含中文路径上会把文件名按 GBK 编码，写出乱码名且后续读不到。
        # 因此全部在纯 ASCII 临时目录内完成「抽取 + 转 PNG」，再以真实视频名移回输出目录。
        # 输入视频也喂 ASCII 名硬链接（同一卷零拷贝；跨卷回退复制），否则 distant_frames
        # 用中文视频名作输出前缀，cv2.imwrite 写出乱码文件名，后续 imread 读不到。
        tmp_dir = tempfile.mkdtemp(prefix="smart_")
        real_stem = Path(video_path).stem
        ascii_input = os.path.join(tmp_dir, "input" + Path(video_path).suffix)
        try:
            os.link(video_path, ascii_input)
        except OSError:
            shutil.copy2(video_path, ascii_input)
        # 直连 core API（上游 cli 缺 typing_extensions 依赖），-u 无缓冲以实时解析进度行
        stmts = ["import sys", "from distant_frames.core import extract_frames"]
        extra_args = []
        if self.open_eyes:
            # cv2 在含中文路径上打不开 Haar 级联 XML（本包装在 (视频抽帧) 目录下），
            # 把两个 XML 复制到 ASCII 临时目录并给 core 的 _CASCADES_DIR 打补丁
            spec = importlib.util.find_spec("distant_frames")
            cascade_src = Path(spec.submodule_search_locations[0]) / "haarcascade_classifiers"
            cascade_dir = os.path.join(tmp_dir, "cascades")
            os.makedirs(cascade_dir, exist_ok=True)
            for name in ("haarcascade_frontalface_default.xml", "haarcascade_eye.xml"):
                shutil.copy(os.path.join(cascade_src, name), os.path.join(cascade_dir, name))
            stmts += ["import distant_frames.core as dc", "from pathlib import Path",
                      "dc._CASCADES_DIR = Path(sys.argv[7])"]
            extra_args.append(cascade_dir)
        stmts.append("extract_frames(sys.argv[1], sys.argv[2], threshold=float(sys.argv[3]), "
                     "start_time=float(sys.argv[4]), open_eyes_only=(sys.argv[5]=='1'), "
                     "refine=(sys.argv[6]=='1'))")
        code = ";".join(stmts)
        cmd = [sys.executable, "-u", "-c", code,
               ascii_input, tmp_dir, str(self.threshold),
               str(self.start_time),
               "1" if self.open_eyes else "0",
               "1" if self.refine else "0"] + extra_args

        try:
            proc = self._popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1)

            # 后台线程排空 stderr
            stderr_lines = []
            def _read_stderr():
                try:
                    for line in proc.stderr:
                        stderr_lines.append(line)
                except Exception:
                    pass
            stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
            stderr_thread.start()

            # 解析 stdout 进度行 [12.3s] → 百分比（粒度 0.5%）
            out_lines = []
            last_pct = -1.0
            for line in proc.stdout:
                out_lines.append(line)
                m = re.search(r"\[(\d+(?:\.\d+)?)s\]", line)
                if m and duration:
                    cur_ts = float(m.group(1))
                    pct = min(100.0, cur_ts / duration * 100.0)
                    if pct - last_pct >= 0.5 or pct >= 100.0:
                        last_pct = pct
                        if on_progress:
                            on_progress(pct, self._ts_hms(cur_ts))

            proc.wait()
            stderr_thread.join()

            if proc.returncode != 0:
                return "".join(stderr_lines).strip() or "智能抽帧失败"
            # core API 遇错仅打印 Error 并返回 0 退出码，需检查 stdout
            for line in out_lines:
                if line.startswith("Error:"):
                    return line.strip()
            # 在 ASCII 临时目录转 PNG（保持项目输出契约），再以真实视频名移动到输出目录
            os.makedirs(out_dir, exist_ok=True)
            self._jpg_to_png(tmp_dir)
            for f in os.listdir(tmp_dir):
                if f.lower().endswith(f".{IMAGE_FORMAT}"):
                    new_name = f.replace("input_frame_", f"{real_stem}_frame_", 1)
                    shutil.move(os.path.join(tmp_dir, f), os.path.join(out_dir, new_name))
            return None
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _convert_single(self, video_path: str, on_start=None, on_progress=None):
        """抽取单个视频的帧，返回 (路径, 状态, 详情)；状态: success/skipped/failed"""
        try:
            out_dir = self._get_output_dir(video_path)

            # 已存在跳过
            if os.path.isdir(out_dir) and os.listdir(out_dir) and not self.overwrite:
                return video_path, "skipped", "文件已存在"

            os.makedirs(out_dir, exist_ok=True)
            if self.overwrite:
                # 覆盖时清空旧图，避免上次抽取的残留文件
                for f in os.listdir(out_dir):
                    os.remove(os.path.join(out_dir, f))

            if on_start:
                on_start(video_path)

            if self.mode == "timestamp":
                error = self._extract_by_timestamps(video_path, out_dir, on_progress)
            elif self.mode == "smart":
                error = self._extract_by_smart(video_path, out_dir, on_progress)
            else:
                error = self._extract_by_filter(video_path, out_dir, on_progress)

            if error:
                return video_path, "failed", error

            frames = [f for f in os.listdir(out_dir) if f.lower().endswith(f".{IMAGE_FORMAT}")]
            if not frames:
                return video_path, "skipped", "未抽取到任何帧"
            return video_path, "success", f"{len(frames)} 帧 → {out_dir}"

        except Exception as e:
            return video_path, "failed", str(e)

    def extract(self, on_start=None, on_progress=None, on_done=None):
        """逐文件顺序抽帧（解码密集型，单线程进度清晰）。

        回调均为展示用，由 Cli 提供：on_start(路径) 开始前、on_progress(百分比, 已编码时间) 实时、
        on_done(路径, 状态, 详情) 每文件完成。返回每文件结果列表，供无回调消费方使用。
        """
        results = []
        for path in self.videos:
            p, status, info = self._convert_single(path, on_start, on_progress)
            results.append((p, status, info))
            if on_done:
                on_done(p, status, info)
        return results


class Gui:
    """webview-cli 配置窗口（含配置的读写与校验）。"""

    @staticmethod
    def _render(data):
        """读取 HTML 模板并注入 APP_DATA（常量单一来源在 Python）。"""
        payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        html = CONFIG_TEMPLATE.read_text(encoding="utf-8")
        return html.replace(APP_DATA_MARKER, f"const APP_DATA = {payload};")

    @staticmethod
    def _webview_bin():
        webview = shutil.which("webview-cli") or shutil.which("webview")
        if not webview:
            raise FileNotFoundError(
                "未找到 webview-cli，请确认已安装并加入 PATH\n"
                "https://github.com/just-be-dev/webview-cli"
            )
        return webview

    @staticmethod
    def _validate(data):
        """对配置窗口返回的数据做校验（镜像 HTML 里的 JS 规则），返回规范化后的 dict。"""
        if not isinstance(data, dict):
            raise ValueError("返回的数据格式无效")
        mode = str(data.get("mode") or "").strip()
        if mode not in MODES:
            raise ValueError(f"无效的抽取方式：{mode}")

        timestamps = str(data.get("timestamps") or "").strip()
        if mode in ("second", "frame"):
            try:
                interval = int(float(data.get("interval")))
            except (ValueError, TypeError):
                raise ValueError("间隔必须为不小于 1 的数字")
            if interval < 1:
                raise ValueError("间隔必须为不小于 1 的数字")
        elif mode == "timestamp":
            raw = timestamps.replace("，", ",").strip()
            parts = [x.strip() for x in raw.split(",") if x.strip()]
            if not parts:
                raise ValueError("请填写至少一个时间点")
            for p in parts:
                try:
                    FrameExtractor._to_seconds(p)
                except ValueError:
                    raise ValueError(f"无效的时间格式：{p}")
            timestamps = ",".join(parts)

        if mode == "smart":
            try:
                threshold = float(data.get("threshold"))
            except (ValueError, TypeError):
                raise ValueError("相似度阈值无效")
            if threshold not in ALLOWED_SMART_THRESHOLDS:
                raise ValueError(f"不支持的相似度阈值：{data.get('threshold')}")
            start = str(data.get("smart_start") or "").strip()
            if start:
                try:
                    if float(start) < 0:
                        raise ValueError("起始时间不能为负数")
                except ValueError:
                    raise ValueError("起始时间必须为不小于 0 的数字")

        data.update(
            mode=mode,
            interval=str(data.get("interval") or "5"),
            timestamps=timestamps,
            threshold=str(data.get("threshold") or "0.78"),
            output_dir=str(data.get("output_dir") or "").strip(),
            overwrite=bool(data.get("overwrite")),
            smart_refine=bool(data.get("smart_refine")),
            smart_open_eyes=bool(data.get("smart_open_eyes")),
            smart_start=str(data.get("smart_start") or "").strip(),
        )
        return data

    @staticmethod
    def load_config():
        """读取配置；缺失/损坏/非法返回 None（触发首次引导）。"""
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if data.get("mode") in MODES else None

    @staticmethod
    def save_config(data):
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def ask(self):
        """打开配置窗口，返回校验后的配置 dict；取消/出错返回 None。"""
        webview = self._webview_bin()
        data = {"saved": self.load_config() or {}, "DEFAULTS": DEFAULT_CONFIG,
                "MODES": MODES,
                "ALLOWED_SECOND_INTERVALS": ALLOWED_SECOND_INTERVALS,
                "ALLOWED_FRAME_INTERVALS": ALLOWED_FRAME_INTERVALS,
                "ALLOWED_SMART_THRESHOLDS": ALLOWED_SMART_THRESHOLDS}
        html = self._render(data)
        cmd = [webview, "--title", "视频抽帧 - 配置窗口", "--width", "500", "--height", "720"]
        try:
            proc = FrameExtractor._run(cmd, input=html, capture_output=True)
        except (OSError, ValueError):
            # stdin 管道不可用时回退到临时 HTML 文件
            fd, path = tempfile.mkstemp(suffix=".html", prefix="video-frame-webview-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(html)
                proc = FrameExtractor._run(cmd + [path], input="", capture_output=True)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        if proc.returncode:
            return None  # 取消(2) / 出错
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            print("配置窗口返回的数据无法解析")
            return None
        try:
            return self._validate(payload)
        except (ValueError, TypeError) as e:
            print(f"配置校验失败：{e}")
            return None


class Cli:
    """批处理命令行流程（含盒子参数解析与输出编码修复）。"""

    @staticmethod
    def _fix_encoding():
        # 统一输出编码，避免 GBK 控制台下 emoji/中文报错（盒子环境已设 PYTHONUTF8=1）
        for _s in (sys.stdout, sys.stderr):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    @staticmethod
    def _dw(text):
        """近似显示宽度：CJK/全角/emoji 计 2，其余计 1（横幅自适应宽度用）。"""
        return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)

    @staticmethod
    def _version():
        try:
            for line in (BASE_DIR / "bm-scripts-box-rc.toml").read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("version"):
                    return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return ""

    @staticmethod
    def _title():
        v = Cli._version()
        return f"🎞️ 视频抽帧{(' v' + v) if v else ''} · 按需抽取视频帧"

    @staticmethod
    def _banner(text):
        w = Cli._dw(text) + 4
        bar = "─" * w
        print("┌" + bar + "┐")
        print("│  " + text + "  │")
        print("└" + bar + "┘")

    @staticmethod
    def _section(title):
        print(f"── {title} " + "─" * 22)

    @staticmethod
    def _mode_summary(config):
        """抽取方式摘要（配置分节说明用）。"""
        mode = config.get("mode")
        if mode == "second":
            return f"按秒抽帧 (每 {config.get('interval', '5')} 秒一帧)"
        if mode == "frame":
            return f"固定间隔帧 (每 {config.get('interval', '5')} 帧一帧)"
        if mode == "timestamp":
            count = len([p for p in str(config.get("timestamps", "")).split(",") if p.strip()])
            return f"指定时间点 ({count} 个)"
        if mode == "keyframe":
            return "关键帧 (全部 I 帧)"
        if mode == "smart":
            summary = f"智能抽帧 (阈值 {config.get('threshold', '0.78')})"
            extras = []
            if config.get("smart_refine"):
                extras.append("幻灯片文字检测")
            if config.get("smart_open_eyes"):
                extras.append("仅睁眼帧")
            if str(config.get("smart_start", "")).strip():
                extras.append(f"起始 {config.get('smart_start')} 秒")
            if extras:
                summary += " · " + " / ".join(extras)
            return summary
        return MODE_LABELS.get(mode, str(mode))

    @staticmethod
    def get_path(param_path):
        """读取盒子传入的参数 JSON，返回存在的文件路径列表。"""
        try:
            with open(param_path, "r", encoding="utf-8") as f:
                params = json.load(f)
        except Exception:
            return []
        return [p for p in params.get("data", {}).get("target_paths", []) if Path(p).exists()]

    def __init__(self):
        self.gui = Gui()

    def run(self, video_paths):
        # 先定位 ffmpeg（缺则直接报错），避免打印到一半才失败
        FrameExtractor._ffmpeg_bin()
        self._banner(self._title())
        print()

        videos, skipped = [], []
        for p in video_paths:
            if Path(p).suffix.lower() in VIDEO_EXTS:
                videos.append(p)
            else:
                skipped.append(p)
        if skipped:
            self._section("扫描")
            for p in skipped:
                print(f"  ⏭️ 忽略非视频: {Path(p).name}")
        if not videos:
            print("  ❌ 未选择有效的视频文件")
            self._exit()
            return

        self._section("配置")
        config = Gui.load_config()
        if config is None:
            print("  📋 首次使用，请配置抽帧参数...")
            config = self.gui.ask()
            if config is None:
                print("  已取消抽取")
                self._exit()
                return
            Gui.save_config(config)
            print("  ✅ 抽帧配置已保存，开始抽帧")
        else:
            print("  💾 使用已保存的配置")
        print(f"  方式: {self._mode_summary(config)} · 共 {len(videos)} 个文件")

        self._section("处理")
        extractor = FrameExtractor(videos, config)
        total = len(videos)
        started = [0]
        ok = fail = skip = 0

        def on_start(path):
            started[0] += 1
            print(f"  ▶ ({started[0]}/{total}) 正在抽帧: {Path(path).name}")

        def on_progress(pct, time_str):
            line = f"  进度: {pct:5.1f}%" if pct is not None else "  进度: ..."
            print(f"\r{line}   已编码 {time_str}", end="", flush=True)

        def on_done(path, status, info):
            nonlocal ok, fail, skip
            print("\r" + " " * 60 + "\r", end="")  # 清掉实时进度行
            name = Path(path).name
            if status == "success":
                ok += 1
                print(f"  ✅ {name}  {info}")
            elif status == "skipped":
                skip += 1
                print(f"  ⏭️ {name}  {info}")
            else:
                fail += 1
                print(f"  ❌ {name}  {(info or '未知错误').strip().splitlines()[0]}")

        extractor.extract(on_start=on_start, on_progress=on_progress, on_done=on_done)

        self._section("结果")
        line = (f"✅ 成功 {ok} 个 · ❌ 失败 {fail} 个" if fail
                else f"✅ 全部完成 {ok} 个文件")
        if skip:
            line += f" · ⏭️ 跳过 {skip} 个"
        print("  " + line)
        print()
        self._exit()

    @staticmethod
    def _exit():
        width, total = 10, 5
        for i in range(total, 0, -1):
            filled = round(width * (total - i + 1) / total)
            bar = "█" * filled + "░" * (width - filled)
            print(f"\r  ⏳ {i}s {bar}  按任意键立即退出", end="")
            time.sleep(1)
        print("\r" + " " * 60, end="\r")
        print("  👋 已退出")
        sys.exit(0)


def main():
    Cli._fix_encoding()
    param_path = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        if param_path:
            paths = Cli.get_path(param_path)
            if not paths:
                print("未获取到有效的视频文件路径")
                time.sleep(2)
            else:
                Cli().run(paths)
        else:
            Cli._banner(Cli._title())
            config = Gui().ask()
            if config is not None:
                Gui.save_config(config)
            print(("  ✅ 抽帧配置已保存" if config else "  未保存抽帧配置") + "\n")
            time.sleep(2)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        time.sleep(3)


if __name__ == "__main__":
    main()
