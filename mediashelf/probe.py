"""用 ffprobe 读取真实编码参数，供命名与界面显示使用。

原则：能测出来的才写进文件名。片源（BluRay / WEB-DL）无法从码流直接判定，
这里只做保守推断，并把推断结果标记为 source_guess，界面上可以改。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Windows 下 GUI 进程（--windowed 打包后没有控制台）启动控制台子程序时，
# 系统会给每个子进程新建一个可见的控制台窗口。扫描 50 个文件就是 50 次黑窗闪烁，
# 界面看起来像在崩溃。CREATE_NO_WINDOW 让子进程静默运行。
NO_WINDOW: dict[str, Any] = (
    {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    if os.name == "nt"
    else {}
)

AUDIO_RANK = {
    "truehd": 90,
    "flac": 80,
    "dts": 70,
    "eac3": 60,
    "ac3": 50,
    "aac": 40,
    "opus": 30,
    "vorbis": 25,
    "mp3": 20,
}

CHANNEL_LABEL = {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}

CODEC_LABEL = {
    "hevc": "x265",
    "h264": "x264",
    "av1": "AV1",
    "vp9": "VP9",
    "mpeg4": "XviD",
    "mpeg2video": "MPEG2",
    "vc1": "VC-1",
}


@dataclass
class AudioTrack:
    codec: str
    channels: int
    profile: str = ""
    title: str = ""
    lang: str = ""
    bitrate: int = 0

    @property
    def label(self) -> str:
        """TrueHD.7.1.Atmos 这种形式。"""
        codec = self.codec
        if codec == "eac3":
            codec = "DDP"
        elif codec == "ac3":
            codec = "AC3"
        elif codec == "dts":
            profile = (self.profile or "").upper()
            if "MA" in profile or "HD" in profile or "MA" in self.title.upper():
                codec = "DTS-HD.MA"
            elif "X" in profile:
                codec = "DTS-X"
            else:
                codec = "DTS"
        elif codec == "truehd":
            codec = "TrueHD"
        elif codec == "aac":
            codec = "AAC"
        else:
            codec = codec.upper()

        parts = [codec, CHANNEL_LABEL.get(self.channels, f"{self.channels}ch")]
        if "atmos" in f"{self.title}{self.profile}".lower():
            parts.append("Atmos")
        return ".".join(parts)


@dataclass
class ProbeResult:
    path: str
    size: int = 0
    duration: float = 0.0
    bitrate: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    pix_fmt: str = ""
    hdr: str = ""
    audio: list[AudioTrack] = field(default_factory=list)
    subs: list[str] = field(default_factory=list)
    error: str = ""

    # ---- 派生字段，命名模板直接用 ----
    @property
    def res(self) -> str:
        if self.width >= 3600:
            return "2160p"
        if self.width >= 1800:
            return "1080p"
        if self.width >= 1200:
            return "720p"
        return f"{self.width}x{self.height}" if self.width else ""

    @property
    def depth(self) -> str:
        match = re.search(r"(\d{2})(?:le|be)", self.pix_fmt or "")
        if match:
            bits = int(match.group(1))
            return f"{bits}bit" if bits > 8 else ""
        return ""

    @property
    def codec(self) -> str:
        base = CODEC_LABEL.get(self.vcodec, (self.vcodec or "").upper())
        return f"{base}.{self.depth}" if self.depth else base

    @property
    def best_audio(self) -> str:
        if not self.audio:
            return ""
        return max(self.audio, key=_audio_score).label

    @property
    def bitrate_mbps(self) -> float:
        return self.bitrate / 1e6 if self.bitrate else 0.0

    def source_guess(self) -> str:
        """保守推断片源，仅供命名参考。"""
        mbps = self.bitrate_mbps
        if self.size > 40 * 1024**3 and mbps > 45:
            return "REMUX"
        if self.size > 25 * 1024**3 and mbps > 30:
            return "BluRay"
        if mbps >= 18 and self.res == "2160p":
            return "WEB-DL"
        if mbps >= 12 and self.res == "2160p":
            return "WEB-DL"
        if self.res in ("2160p", "1080p") and mbps < 4:
            return "HDRip"
        return "WEB-DL"


def _audio_score(track: AudioTrack) -> int:
    score = AUDIO_RANK.get(track.codec, 10)
    if "atmos" in f"{track.title}{track.profile}".lower():
        score += 15
    score += min(track.channels, 8)
    return score


def probe(path: str | Path, ffprobe: str, timeout: int = 60) -> ProbeResult:
    result = ProbeResult(path=str(path))
    try:
        result.size = Path(path).stat().st_size
    except OSError:
        pass

    if not ffprobe:
        result.error = "未找到 ffprobe"
        return result

    cmd = [
        ffprobe,
        "-v",
        "error",
        # 只读文件头和少量样本就够了：默认设置会在网络盘上把 50 GB 的 remux
        # 从头分析很久，超时后报「ffprobe 调用失败」。这两个参数把探测压到秒级。
        "-analyzeduration",
        "20M",
        "-probesize",
        "20M",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, **NO_WINDOW)
    except subprocess.TimeoutExpired:
        result.error = f"读取超时（{timeout}s），文件可能损坏或网络盘太慢"
        return result
    except OSError as exc:
        result.error = f"无法启动 ffprobe：{exc}"
        return result

    if proc.returncode != 0:
        result.error = proc.stderr.decode("utf-8", "ignore").strip()[:200] or "ffprobe 调用失败"
        return result

    try:
        data: dict[str, Any] = json.loads(proc.stdout.decode("utf-8", "ignore"))
    except ValueError as exc:
        result.error = f"解析 ffprobe 输出失败: {exc}"
        return result

    fmt = data.get("format") or {}
    try:
        result.duration = float(fmt.get("duration") or 0)
    except ValueError:
        pass
    try:
        result.bitrate = int(fmt.get("bit_rate") or 0)
    except ValueError:
        pass

    for stream in data.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "video" and not result.width:
            result.vcodec = stream.get("codec_name") or ""
            result.width = int(stream.get("width") or 0)
            result.height = int(stream.get("height") or 0)
            result.pix_fmt = stream.get("pix_fmt") or ""
            result.fps = _parse_fps(stream.get("r_frame_rate"))
            result.hdr = _detect_hdr(stream)
        elif kind == "audio":
            tags = stream.get("tags") or {}
            result.audio.append(
                AudioTrack(
                    codec=stream.get("codec_name") or "",
                    channels=int(stream.get("channels") or 0),
                    profile=stream.get("profile") or "",
                    title=tags.get("title") or "",
                    lang=tags.get("language") or "",
                    bitrate=int(stream.get("bit_rate") or 0),
                )
            )
        elif kind == "subtitle":
            tags = stream.get("tags") or {}
            result.subs.append(tags.get("language") or "und")

    return result


def _parse_fps(raw: Any) -> float:
    if not raw:
        return 0.0
    try:
        if "/" in str(raw):
            num, den = str(raw).split("/", 1)
            return round(float(num) / float(den), 3) if float(den) else 0.0
        return round(float(raw), 3)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _detect_hdr(stream: dict[str, Any]) -> str:
    """返回 DV / DV.HDR / HDR / HLG 这类标记。"""
    has_dv = False
    for item in stream.get("side_data_list") or []:
        text = json.dumps(item, ensure_ascii=False)
        if "dovi" in text.lower() or "dv_profile" in text:
            has_dv = True

    transfer = (stream.get("color_transfer") or "").lower()
    has_hdr10 = transfer in ("smpte2084", "smpte-st-2084") or "2084" in transfer
    has_hlg = "arib" in transfer or "hlg" in transfer

    if has_dv and (has_hdr10 or has_hlg):
        return "DV.HDR"
    if has_dv:
        return "DV"
    if has_hdr10:
        return "HDR"
    if has_hlg:
        return "HLG"
    return ""
