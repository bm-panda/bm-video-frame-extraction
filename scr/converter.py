import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List

# ==================== 抽帧模式定义 ====================
# 抽取方式
ALLOWED_MODES = {"second", "frame", "timestamp", "keyframe", "smart"}
MODE_ORDER = ["smart", "second", "frame", "timestamp", "keyframe"]
MODE_LABELS = {
    "smart": "智能抽帧",
    "second": "按秒抽帧",
    "frame": "固定间隔帧",
    "timestamp": "指定时间点",
    "keyframe": "关键帧",
}

# 各模式可选参数（GUI 下拉选项）
ALLOWED_SECOND_INTERVALS = [1, 2, 3, 5, 10, 15, 30, 60]
ALLOWED_FRAME_INTERVALS = [5, 10, 12, 15, 25, 30, 60, 120, 240]
# 智能抽帧相似度阈值（与上一张已存帧比较，越高保留帧越多）
ALLOWED_SMART_THRESHOLDS = [0.5, 0.6, 0.65, 0.7, 0.75, 0.78, 0.8, 0.85, 0.9, 0.95]

# 输出图片格式
IMAGE_FORMAT = "png"


class FrameExtractor:
    """视频抽帧器(基于 FFmpeg)：从视频中抽取帧保存为 PNG 图片"""

    def __init__(self, videos: List[str], output_dir: str = "", mode: str = "second",
                 interval: str = "5", timestamps: str = "", threshold: str = "0.78",
                 overwrite: bool = False, refine: bool = False, open_eyes: bool = False,
                 start_time: str = "0"):
        """
        初始化抽帧器

        Args:
            videos: 视频文件路径列表
            output_dir: 输出目录（为空则在源文件目录下建 <视频名>_frames 子文件夹）
            mode: 抽取方式 (second/frame/timestamp/keyframe/smart)
            interval: 按秒抽帧的秒数或固定间隔的帧数
            timestamps: 指定时间点，逗号分隔（如 "00:00:05,00:00:15"）
            threshold: 智能抽帧相似度阈值 (0.5~0.95)，越高保留帧越多
            overwrite: 是否覆盖已存在的文件
            refine: 智能抽帧是否检测幻灯片文字变化（HOG/ORB 复核）
            open_eyes: 智能抽帧是否仅保留睁眼帧
            start_time: 智能抽帧起始时间（秒），从该时间点开始抽取
        """
        self.videos = videos
        self.output_dir = output_dir or ""

        # 非法参数值回退默认
        self.mode = mode if mode in ALLOWED_MODES else "second"
        try:
            self.interval = max(1, int(float(interval)))
        except (TypeError, ValueError):
            self.interval = 5
        try:
            self.threshold = float(threshold)
        except (TypeError, ValueError):
            self.threshold = 0.78
        if self.threshold not in ALLOWED_SMART_THRESHOLDS:
            self.threshold = 0.78
        try:
            self.start_time = max(0.0, float(start_time))
        except (TypeError, ValueError):
            self.start_time = 0.0
        self.refine = bool(refine)
        self.open_eyes = bool(open_eyes)
        self.timestamps = self._parse_timestamps(timestamps) if self.mode == "timestamp" else []
        self.overwrite = overwrite

        self._ffmpeg = shutil.which("ffmpeg")
        if not self._ffmpeg:
            raise FileNotFoundError("未找到 FFmpeg（ffmpeg 命令），请确认已安装并在环境变量中")
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
            proc = subprocess.run(
                [self._ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", input_path],
                capture_output=True, text=True, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

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
            proc = subprocess.run(
                cmd, capture_output=True, text=True, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
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
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

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
                return ("".join(stderr_lines).strip() or "智能抽帧失败")
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

    def _convert_single(self, video_path: str, on_start=None, on_progress=None) -> tuple:
        """抽取单个视频的帧，返回 (路径, 状态, 信息)，状态: success/skipped/failed

        Args:
            on_start: 开始抽帧前的回调，接收 (文件路径)
            on_progress: 抽帧中的实时进度回调，接收 (百分比, 已编码时间字符串)
        """
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
            return video_path, "success", out_dir

        except Exception as e:
            return video_path, "failed", str(e)

    def convert(self, max_workers: int = 1, progress_callback=None, on_start=None, on_progress=None) -> dict:
        """
        批量抽帧

        Args:
            max_workers: 并发线程数（抽帧为 IO/解码密集型，建议 1 保证进度清晰）
            progress_callback: 每个文件完成后的回调，接收 (已完成数, 总数)
            on_start: 每个文件开始抽帧前的回调，接收 (文件路径)
            on_progress: 每个文件抽帧中的实时进度回调，接收 (百分比, 已编码时间字符串)

        Returns:
            dict: 抽帧结果统计
        """
        total = len(self.videos)
        results = {"success": [], "skipped": [], "failed": [], "total": total}

        if total == 0:
            return results

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self._convert_single, path, on_start, on_progress): path
                       for path in self.videos}

            for idx, future in enumerate(as_completed(futures), 1):
                path, status, info = future.result()
                if status == "success":
                    results["success"].append((path, info))
                elif status == "skipped":
                    results["skipped"].append((path, info))
                else:
                    results["failed"].append((path, info))

                if progress_callback:
                    progress_callback(idx, total)

        return results

    @staticmethod
    def get_file_size(path: str) -> str:
        """获取文件大小（人性化显示）"""
        size = os.path.getsize(path)
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"
