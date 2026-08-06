"""
视频抽帧 - 从视频中抽取帧保存为图片
支持: 按秒抽帧 | 固定间隔帧 | 指定时间点 | 关键帧 | 智能抽帧
"""
import json
import os

import sys
import time

from pathlib import Path

from scr.gui import run_config_window, DEFAULT_CONFIG
from scr.converter import FrameExtractor, MODE_LABELS, IMAGE_FORMAT

# 统一输出编码，避免 GBK 控制台下 emoji/中文报错（盒子环境已设 PYTHONUTF8=1）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def get_path(param_path):
    initial_files = []

    if param_path and Path(param_path).exists():
        with open(param_path, "r", encoding="utf-8") as f:
            params = json.load(f)
        raw = params.get("data", {}).get("target_paths", [])
        initial_files = [p for p in raw if Path(p).exists()]
    return initial_files


def get_config():
    """读取配置文件，不存在则返回默认配置"""
    config_path = Path(__file__).parent / "config.json"

    # 默认配置（单一来源在 scr/gui.py）
    default_config = dict(DEFAULT_CONFIG)

    if not os.path.exists(config_path):
        return default_config

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            return {**default_config, **config}
    except (json.JSONDecodeError, ValueError) as e:
        print(f"配置文件格式错误：{e}，使用默认配置")
        return default_config


def _mode_summary(config):
    """生成抽取方式摘要"""
    mode = config["mode"]
    if mode == "second":
        return f"按秒抽帧 (每 {config['interval']} 秒一帧)"
    if mode == "frame":
        return f"固定间隔帧 (每 {config['interval']} 帧一帧)"
    if mode == "timestamp":
        count = len([p for p in str(config.get('timestamps', '')).split(',') if p.strip()])
        return f"指定时间点 ({count} 个)"
    if mode == "keyframe":
        return "关键帧 (全部 I 帧)"
    if mode == "smart":
        summary = f"智能抽帧 (阈值 {config['threshold']})"
        extras = []
        if config.get("smart_refine"):
            extras.append("幻灯片文字检测")
        if config.get("smart_open_eyes"):
            extras.append("仅睁眼帧")
        if str(config.get("smart_start", "")).strip():
            extras.append(f"起始{config['smart_start']}秒")
        if extras:
            summary += " · " + " / ".join(extras)
        return summary
    return MODE_LABELS.get(mode, mode)


def cli(video_path: list, config: dict):
    print("-" * 50)
    print('视频抽帧')
    print("-" * 50)
    print(f"抽取方式: {_mode_summary(config)}   图片格式: {IMAGE_FORMAT.upper()}")
    print(f"输出目录: {config['output_dir'] if config['output_dir'] else '源目录下 <视频名>_frames 文件夹'}"
          f"   覆盖模式: {'允许覆盖' if config['overwrite'] else '跳过已存在文件'}")
    print("-" * 50)

    extractor = FrameExtractor(
        videos=video_path,
        output_dir=config["output_dir"],
        mode=config["mode"],
        interval=config.get("interval", "5"),
        timestamps=config.get("timestamps", ""),
        threshold=config.get("threshold", "0.78"),
        overwrite=bool(config["overwrite"]),
        refine=bool(config.get("smart_refine", False)),
        open_eyes=bool(config.get("smart_open_eyes", False)),
        start_time=config.get("smart_start", "0"),
    )

    total_files = len(video_path)
    started = [0]

    def on_start(path):
        started[0] += 1
        print(f"\n▶ 正在抽帧 ({started[0]}/{total_files}): {os.path.basename(path)}")

    def on_progress(pct, time_str):
        if pct is not None:
            print(f"\r   进度: {pct:5.1f}%   已编码 {time_str}", end="", flush=True)
        else:
            print(f"\r   进度: ...   已编码 {time_str}", end="", flush=True)

    # 抽帧为解码密集型任务，顺序处理可吃满单核且进度清晰
    result = extractor.convert(max_workers=1, on_start=on_start, on_progress=on_progress)

    print("\n")
    print(f"✅ 成功：{len(result['success'])} 个")
    for path, output in result["success"]:
        print(f"   {os.path.basename(path)} → {os.path.basename(output)}/")

    if result["skipped"]:
        print(f"\n⏭️ 跳过：{len(result['skipped'])} 个")
        for path, reason in result["skipped"]:
            print(f"   {os.path.basename(path)}：{reason}")

    if result["failed"]:
        print(f"\n❌ 失败：{len(result['failed'])} 个")
        for path, error in result["failed"]:
            print(f"   {os.path.basename(path)}：{error}")

    # ── 倒计时 + 按键退出 ──
    print("\n" + "-" * 50)
    print("按任意键立即退出，或等待倒计时自动退出")

    # 倒计时
    for i in range(5, 0, -1):
        print(f"\r⏳ {i} 秒后自动退出... (按任意键退出)", end="")
        time.sleep(1)
    print("\r👋 已退出")
    sys.exit(0)


def main():
    param_path = sys.argv[1] if len(sys.argv) > 1 else None
    config = get_config()
    if param_path:
        paths = get_path(param_path)
        cli(paths, config)
    else:
        run_config_window(config)


if __name__ == "__main__":
    main()
