"""视频抽帧 - 从视频中抽取帧保存为 PNG 图片
配置由盒子自动生成运行界面(params_form)提供，支持三种触发模式：
  manual    右键/快捷键/卡片运行 → 控制台显示进度，倒计时退出
  scheduled 定时任务 → 无人值守，可选写信封上报结果
  node      被其他脚本联动调用 → 写结果信封到 output_json 后退出
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent

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

# 智能抽帧相似度阈值（与上一张已存帧比较，越高保留帧越多）
ALLOWED_SMART_THRESHOLDS = [0.5, 0.6, 0.65, 0.7, 0.75, 0.78, 0.8, 0.85, 0.9, 0.95]

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".ts", ".m4v",
              ".mpg", ".mpeg", ".m2ts", ".mts", ".3gp", ".ogv", ".vob", ".rmvb", ".rm", ".asf"}
IMAGE_FORMAT = "png"


@dataclass
class Config:
    """抽帧配置（默认值单一来源，字段与盒子 params 声明一一对应）。"""
    mode: str = "second"
    interval: int = 5
    timestamps: str = "00:00:05,00:00:15,00:01:00"
    threshold: str = "0.78"
    smart_refine: bool = False
    smart_open_eyes: bool = False
    smart_start: str = ""
    output_dir: str = ""
    overwrite: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        """从盒子 params 段构建：只收合法类型，缺失/非法兜底默认。"""
        cfg = cls()
        if not isinstance(data, dict):
            return cfg
        if isinstance(data.get("mode"), str) and data["mode"] in ALLOWED_MODES:
            cfg.mode = data["mode"]
        interval = data.get("interval")
        if isinstance(interval, (int, float)) or isinstance(interval, str):
            try:
                cfg.interval = max(1, int(float(interval)))
            except (TypeError, ValueError):
                pass
        for k in ("timestamps", "threshold", "smart_start", "output_dir"):
            if isinstance(data.get(k), str):
                setattr(cfg, k, data[k])
        for k in ("smart_refine", "smart_open_eyes", "overwrite"):
            if isinstance(data.get(k), bool):
                setattr(cfg, k, data[k])
        return cfg


# ==================== 基础设施工具 ====================
def fix_encoding():
    """统一输出编码，避免 GBK 控制台下 emoji/中文报错（盒子环境已设 PYTHONUTF8=1）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def get_version() -> str:
    """读取脚本版本号（单一来源 bm-scripts-box-rc.toml，不硬编码）。"""
    try:
        for line in (BASE_DIR / "bm-scripts-box-rc.toml").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("version"):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def title() -> str:
    v = get_version()
    return f"🎞️ 视频抽帧{(' v' + v) if v else ''} · 按需抽取视频帧"


def print_banner(text: str):
    """打印装饰标题（按显示宽度自适应，纯 Unicode 零依赖）。"""
    width = sum(2 if ord(ch) > 0x2E7F else 1 for ch in text) + 4
    bar = "─" * width
    print(f"┌{bar}┐\n│  {text}  │\n└{bar}┘")


def print_section(text: str):
    print(f"── {text} " + "─" * 22)


def countdown_exit(seconds: int = 5):
    """批量处理结束后的倒计时退出：进度条实时刷新，按任意键立即退出。"""
    width = 10
    try:
        import msvcrt
        has_key = True
    except ImportError:
        has_key = False
    for i in range(seconds, 0, -1):
        if has_key and msvcrt.kbhit():
            break
        filled = round(width * (seconds - i + 1) / seconds)
        bar = "█" * filled + "░" * (width - filled)
        # flush=True：`end=""` 不换行，块缓冲下会积压到退出才一次性输出
        print(f"\r  ⏳ {i}s {bar}  按任意键立即退出", end="", flush=True)
        time.sleep(1)
    print("\r" + " " * 60 + "\r  👋 已退出", flush=True)
    sys.exit(0)


def pause_exit(message: str = "按任意键退出"):
    """无参引导/未执行任务时：暂停等待按键后退出（不倒计时）。"""
    try:
        import msvcrt
        print(f"\n  {message}...", end="", flush=True)
        msvcrt.getch()
    except ImportError:
        input(f"\n  {message}...")
    print("\r  👋 已退出")
    sys.exit(0)


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

    def __init__(self, videos, config: Config):
        self.videos = videos
        self.output_dir = config.output_dir.strip()

        self.mode = config.mode if config.mode in ALLOWED_MODES else "second"
        self.interval = config.interval
        try:
            threshold = float(config.threshold)
        except (TypeError, ValueError):
            threshold = 0.78
        self.threshold = threshold if threshold in ALLOWED_SMART_THRESHOLDS else 0.78
        try:
            self.start_time = max(0.0, float(config.smart_start or "0"))
        except (TypeError, ValueError):
            self.start_time = 0.0
        self.refine = bool(config.smart_refine)
        self.open_eyes = bool(config.smart_open_eyes)
        if self.mode == "timestamp":
            try:
                self.timestamps = self._parse_timestamps(config.timestamps)
            except ValueError:
                self.timestamps = []
        else:
            self.timestamps = []
        self.overwrite = bool(config.overwrite)

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

        回调均为展示用：on_start(路径) 开始前、on_progress(百分比, 已编码时间) 实时、
        on_done(路径, 状态, 详情) 每文件完成。返回每文件结果列表，供无回调消费方使用。
        """
        results = []
        for path in self.videos:
            p, status, info = self._convert_single(path, on_start, on_progress)
            results.append((p, status, info))
            if on_done:
                on_done(p, status, info)
        return results


class App:
    """应用编排：解析配置 → 批量抽帧 → 按触发模式退出/写信封。"""

    def __init__(self, invoke_mode: str = "manual", output_json: Optional[str] = None):
        self.invoke_mode = invoke_mode
        self.output_json = output_json

    @staticmethod
    def _mode_summary(config: Config) -> str:
        """抽取方式摘要（配置分节说明用）。"""
        mode = config.mode
        if mode == "second":
            return f"按秒抽帧 (每 {config.interval} 秒一帧)"
        if mode == "frame":
            return f"固定间隔帧 (每 {config.interval} 帧一帧)"
        if mode == "timestamp":
            count = len([p for p in str(config.timestamps).split(",") if p.strip()])
            return f"指定时间点 ({count} 个)"
        if mode == "keyframe":
            return "关键帧 (全部 I 帧)"
        if mode == "smart":
            summary = f"智能抽帧 (阈值 {config.threshold})"
            extras = []
            if config.smart_refine:
                extras.append("幻灯片文字检测")
            if config.smart_open_eyes:
                extras.append("仅睁眼帧")
            if str(config.smart_start).strip():
                extras.append(f"起始 {config.smart_start} 秒")
            if extras:
                summary += " · " + " / ".join(extras)
            return summary
        return MODE_LABELS.get(mode, str(mode))

    def process(self, videos, skipped, config: Config) -> Dict[str, Any]:
        """批量抽帧并输出控制台分节；返回统计信息（供信封）。"""
        stats = {"ok": 0, "fail": 0, "skip": 0, "output_dirs": []}
        if not videos:
            print_banner(title())
            print()
            print("  ❌ 未选择有效的视频文件")
            return stats

        print_banner(title())
        print()

        if skipped:
            print_section("扫描")
            for p in skipped:
                print(f"  ⏭️ 忽略非视频: {Path(p).name}")
        print_section("配置")
        print(f"  方式: {self._mode_summary(config)} · 共 {len(videos)} 个文件")

        print_section("处理")
        try:
            extractor = FrameExtractor(videos, config)
        except FileNotFoundError as e:
            # 缺 FFmpeg：逐文件温和报错，不崩溃
            for p in videos:
                print(f"  ❌ {Path(p).name}  {e}")
            stats["fail"] = len(videos)
            print_section("结果")
            print(f"  ❌ 失败 {stats['fail']} 个")
            return stats

        total = len(videos)
        started = [0]

        def on_start(path):
            started[0] += 1
            print(f"  ▶ ({started[0]}/{total}) 正在抽帧: {Path(path).name}")

        def on_progress(pct, time_str):
            line = f"  进度: {pct:5.1f}%" if pct is not None else "  进度: ..."
            print(f"\r{line}   已编码 {time_str}", end="", flush=True)

        def on_done(path, status, info):
            print("\r" + " " * 60 + "\r", end="")  # 清掉实时进度行
            name = Path(path).name
            if status == "success":
                stats["ok"] += 1
                stats["output_dirs"].append(extractor._get_output_dir(path))
                print(f"  ✅ {name}  {info}")
            elif status == "skipped":
                stats["skip"] += 1
                print(f"  ⏭️ {name}  {info}")
            else:
                stats["fail"] += 1
                print(f"  ❌ {name}  {(info or '未知错误').strip().splitlines()[0]}")

        extractor.extract(on_start=on_start, on_progress=on_progress, on_done=on_done)

        print_section("结果")
        line = (f"✅ 成功 {stats['ok']} 个 · ❌ 失败 {stats['fail']} 个" if stats["fail"]
                else f"✅ 全部完成 {stats['ok']} 个文件")
        if stats["skip"]:
            line += f" · ⏭️ 跳过 {stats['skip']} 个"
        print("  " + line)
        print()
        return stats

    def write_envelope(self, stats: Dict[str, Any]):
        """写结果信封（节点/定时任务，或手动带 output_json 时）。"""
        if not self.output_json:
            return
        if not any(stats.get(k) for k in ("ok", "fail", "skip")):
            code, msg = 1, "未选择有效的视频文件"
        elif stats["fail"] and not stats["ok"] and not stats["skip"]:
            code, msg = 1, f"全部失败 {stats['fail']} 个"
        else:
            code = 0
            msg = (f"成功 {stats['ok']} 个 · 失败 {stats['fail']} 个" if stats["fail"]
                   else f"全部完成 {stats['ok']} 个文件")
            if stats["skip"]:
                msg += f" · 跳过 {stats['skip']} 个"
        envelope = {"code": code, "msg": msg, "result": msg,
                    "output_dirs": stats.get("output_dirs", [])}
        with open(self.output_json, "w", encoding="utf-8") as f:
            json.dump(envelope, f, ensure_ascii=False, indent=2)


def main():
    fix_encoding()
    if len(sys.argv) < 2:
        # 无参启动：引导说明，暂停退出（不倒计时）
        print_banner(title())
        print()
        print("  📌 使用说明")
        print("  ── 请先选中视频文件，再按以下方式启动 ──")
        print()
        print("   ① 在文件管理器中选中一个或多个视频文件")
        print("   ② 右键点击 → 选择「视频抽帧」")
        print("   ③ 或选中文件后按下为脚本设置的全局快捷键")
        print()
        print("  🔔 启动后会自动弹出配置窗口，确认即可一键抽帧")
        print()
        pause_exit()
        return

    try:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        print("❌ 无法读取盒子参数文件")
        time.sleep(2)
        return

    env = payload.get("environment", {})
    invoke = env.get("invoke_mode", "manual")
    config = Config.from_dict(payload.get("params", {}))

    videos, skipped = [], []
    for p in payload.get("data", {}).get("target_paths", []):
        if not Path(p).exists():
            continue
        (videos if Path(p).suffix.lower() in VIDEO_EXTS else skipped).append(p)

    app = App(invoke, env.get("output_json"))
    try:
        stats = app.process(videos, skipped, config)
    except Exception as e:
        text = f"处理失败：{e}"
        print(f"❌ {text}")
        if app.output_json:
            with open(app.output_json, "w", encoding="utf-8") as f:
                json.dump({"code": 1, "msg": text, "result": text, "output_dirs": []},
                          f, ensure_ascii=False, indent=2)
        if invoke == "manual":
            pause_exit()
        sys.exit(1)

    app.write_envelope(stats)
    if invoke in ("node", "scheduled"):
        return  # 无人值守：自然退出，不倒计时
    if not videos:
        pause_exit()  # 手动模式无文件：引导后暂停
    countdown_exit()


if __name__ == "__main__":
    main()