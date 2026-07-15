#!/usr/bin/env python3
"""Standalone pre-filter for audio files. Detects empty, silent, or noise-only files
before starting a batch transcription run.

Usage:
    python3 prefilter.py /path/to/audio/               # full report
    python3 prefilter.py /path/to/audio/ --quiet       # only output summary
    python3 prefilter.py /path/to/audio/ --json        # machine-readable JSON
    python3 prefilter.py /path/to/audio/ --skip 5       # skip if < 5 KB
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import wave

AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm", ".aac", ".wma", ".opus")

SPEECH_RATIO_THRESHOLD = 0.01
MIN_DURATION_SEC = 2.0
MIN_SIZE_KB = 5


def _file_size_kb(path: str) -> float:
    try:
        return os.path.getsize(path) / 1024
    except OSError:
        return 0.0


def _get_wav_info(path: str) -> dict:
    try:
        with wave.open(path, "r") as wf:
            frames = wf.getnframes()
            sr = wf.getframerate()
            channels = wf.getnchannels()
            return {
                "duration": frames / sr if sr else 0,
                "sample_rate": sr,
                "channels": channels,
                "frames": frames,
            }
    except Exception:
        return {}


def _get_audio_info(path: str) -> dict:
    info = _get_wav_info(path)
    if info:
        return info
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        fmt = data.get("format", {})
        duration = float(fmt.get("duration", 0))
        streams = data.get("streams", [])
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not audio_stream:
            return {"duration": duration, "sample_rate": 0, "channels": 0, "frames": 0}
        sr = int(audio_stream.get("sample_rate", 0))
        channels = int(audio_stream.get("channels", 0))
        return {
            "duration": duration,
            "sample_rate": sr,
            "channels": channels,
            "frames": 0,
        }
    except Exception:
        return {}


def _detect_speech_ratio(path: str) -> float:
    """Use energy-based detection to estimate speech ratio."""
    try:
        info = _get_audio_info(path)
        duration = info.get("duration", 0)
        if duration < 0.5:
            return 0.0

        if path.lower().endswith(".wav"):
            return _wav_energy_ratio(path)

        try:
            result = subprocess.run(
                ["ffmpeg", "-i", path, "-af", "silencedetect=noise=-30dB:d=0.3",
                 "-f", "null", "-hide_banner", "-loglevel", "error"],
                capture_output=True, text=True, timeout=30,
            )
            silence_total = 0.0
            silence_start = None
            for line in result.stderr.split("\n"):
                if not line.startswith("[silencedetect"):
                    continue
                parts = line.split()
                if "silence_start:" in parts:
                    try:
                        silence_start = float(parts[parts.index("silence_start:") + 1])
                    except (ValueError, IndexError):
                        pass
                elif "silence_end:" in parts and silence_start is not None:
                    try:
                        silence_end = float(parts[parts.index("silence_end:") + 1])
                        silence_total += silence_end - silence_start
                    except (ValueError, IndexError):
                        pass
                    silence_start = None
            if silence_start is not None and duration > silence_start:
                silence_total += duration - silence_start
            if duration > 0:
                return max(0.0, min(1.0, 1.0 - silence_total / duration))
            return 0.0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return 0.5
    except Exception:
        return 0.5


def _wav_energy_ratio(path: str) -> float:
    """Estimate speech ratio from WAV energy levels."""
    try:
        with wave.open(path, "r") as wf:
            sr = wf.getframerate()
            channels = wf.getnchannels()
            frames = wf.getnframes()
            if frames == 0:
                return 0.0
            chunk_size = sr  # 1 second chunks
            n_chunks = max(1, frames // chunk_size)
            speech_chunks = 0
            window_ms = 25  # 25ms window
            window_samples = int(sr * window_ms / 1000)

            for _ in range(n_chunks):
                chunk = wf.readframes(chunk_size)
                if len(chunk) == 0:
                    break
                samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                if channels > 1:
                    samples = samples[::channels]
                n_windows = max(1, len(samples) // window_samples)
                for w in range(n_windows):
                    window = samples[w * window_samples:(w + 1) * window_samples]
                    energy = float(np.mean(window ** 2))
                    if energy > 0.001:
                        speech_chunks += 1
                        break
            return speech_chunks / n_chunks if n_chunks > 0 else 0.0
    except Exception:
        return 0.5


def _classify(path: str, size_kb: float, duration: float, speech_ratio: float) -> str:
    if size_kb < 1:
        return "empty"
    if size_kb < MIN_SIZE_KB:
        return "tiny"
    if duration < 0.5:
        return "silent"
    if duration < MIN_DURATION_SEC and speech_ratio < 0.05:
        return "too_short"
    if speech_ratio < SPEECH_RATIO_THRESHOLD:
        return "no_speech"
    return "ok"


def scan_directory(input_dir: str, skip_size_kb: float = MIN_SIZE_KB) -> list[dict]:
    files = []
    for root, dirs, filenames in os.walk(input_dir):
        dirs.sort()
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, input_dir)
            size_kb = _file_size_kb(path)
            info = _get_audio_info(path)
            duration = info.get("duration", 0)
            speech_ratio = _detect_speech_ratio(path) if duration > 0 else 0.0
            status = _classify(path, size_kb, duration, speech_ratio)
            files.append({
                "file": rel,
                "absolute_path": path,
                "size_kb": round(size_kb, 1),
                "duration_s": round(duration, 2),
                "speech_ratio": round(speech_ratio, 4),
                "status": status,
            })
    return files


def main():
    parser = argparse.ArgumentParser(description="Pre-filter audio files before batch transcription")
    parser.add_argument("input_dir", help="Directory to scan for audio files")
    parser.add_argument("--quiet", action="store_true", help="Only print summary line")
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    parser.add_argument("--skip", type=float, default=MIN_SIZE_KB,
                        help=f"Skip files smaller than this size in KB (default: {MIN_SIZE_KB})")
    parser.add_argument("--min-duration", type=float, default=MIN_DURATION_SEC,
                        help=f"Flag files shorter than this as too_short (default: {MIN_DURATION_SEC})")
    args = parser.parse_args()

    if not os.path.isdir(args.input_dir):
        print(f"ERROR: '{args.input_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)

    files = scan_directory(args.input_dir, skip_size_kb=args.skip)

    if args.json:
        print(json.dumps(files, indent=2))
        return

    ok_files = [f for f in files if f["status"] == "ok"]
    skip_files = [f for f in files if f["status"] != "ok"]

    if args.quiet:
        status_counts = {}
        for f in files:
            s = f["status"]
            status_counts[s] = status_counts.get(s, 0) + 1
        parts = [f"OK: {status_counts.get('ok', 0)}"]
        for s in ("too_short", "no_speech", "tiny", "empty", "silent"):
            if s in status_counts:
                parts.append(f"{s}: {status_counts[s]}")
        print(f"{len(files)} files scanned: {' | '.join(parts)}")
        if skip_files:
            print("Skipped:")
            for f in skip_files:
                print(f"  {f['file']}: {f['status']} ({f['size_kb']:.1f}KB, {f['duration_s']:.1f}s, speech={f['speech_ratio']:.2%})")
        return

    print(f"=== Audio Pre-Filter: {args.input_dir} ===")
    print(f"Total files: {len(files)}")
    print(f"OK (process): {len(ok_files)}")
    print(f"Skippable: {len(skip_files)}")
    print("")

    if skip_files:
        print(f"{'Status':<12} {'Size':>8} {'Duration':>10} {'Speech':>7} File")
        print("-" * 65)
        for f in skip_files:
            print(f"{f['status']:<12} {f['size_kb']:>6.1f}KB {f['duration_s']:>8.1f}s {f['speech_ratio']:>6.1%} {f['file']}")

    print()
    status_counts = {}
    for f in files:
        s = f["status"]
        status_counts[s] = status_counts.get(s, 0) + 1
    print("Summary:")
    for s, c in sorted(status_counts.items(), key=lambda x: -x[1]):
        print(f"  {s:<15} {c}")

    if ok_files:
        total_dur = sum(f["duration_s"] for f in ok_files)
        print(f"\nProcessable audio: {len(ok_files)} files, {total_dur:.0f}s ({total_dur/3600:.1f}h)")


if __name__ == "__main__":
    main()
