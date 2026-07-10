"""Batch transcription orchestrator — replaces batch-transcribe.sh."""

import argparse
import csv
import fcntl
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm"}

STRATEGIES = {
    "boardroom-fast": {
        "whisper_model": "turbo",
        "analysis": "",
        "summary_model": "llama3.1:8b",
        "align_mode": "none",
        "whisper_language": "no",
        "pipeline": "default",
        "description": "Norwegian board meeting fast draft: turbo, source-language only, no diarize/align/summary",
    },
    "boardroom-balanced": {
        "whisper_model": "turbo",
        "analysis": "diarize",
        "summary_model": "llama3.1:8b",
        "align_mode": "none",
        "whisper_language": "no",
        "diarize_min_speakers": 2,
        "diarize_max_speakers": 12,
        "pipeline": "default",
        "description": "Norwegian board meeting balanced: turbo + anonymous speakers, no translation/summary",
    },
    "boardroom-accurate": {
        "whisper_model": "Necklace/faster-nb-whisper-large",
        "analysis": "diarize",
        "summary_model": "llama3.1:8b",
        "align_mode": "none",
        "whisper_language": "no",
        "diarize_min_speakers": 2,
        "diarize_max_speakers": 12,
        "pipeline": "default",
        "description": "Norwegian board meeting quality pass: nb-whisper-large + anonymous speakers, no translation/summary",
    },
    "boardroom-accurate-gradepack": {
        "whisper_model": "Necklace/faster-nb-whisper-large",
        "analysis": "diarize,vad",
        "summary_model": "llama3.1:8b",
        "align_mode": "whisperx",
        "whisper_language": "no",
        "diarize_min_speakers": 2,
        "diarize_max_speakers": 12,
        "align_model": "NbAiLab/nb-wav2vec2-1b-bokmaal-v2",
        "pipeline": "default",
        "description": "Grading retry: nb-whisper-large + Norwegian alignment + diarize + VAD, no translation/summary",
    },
    "boardroom-llm-ready": {
        "whisper_model": "Necklace/faster-nb-whisper-large",
        "analysis": "diarize,vad",
        "summary_model": "",
        "align_mode": "whisperx",
        "whisper_language": "no",
        "diarize_min_speakers": 2,
        "diarize_max_speakers": 12,
        "align_model": "NbAiLab/nb-wav2vec2-1b-bokmaal-v2",
        "pipeline": "default",
        "description": "LLM-ready package: nb-whisper-large + Norwegian alignment + diarize + VAD, enhanced cleanup, no translation/summary",
    },
    "best-overall": {
        "whisper_model": "turbo",
        "analysis": "diarize,vad,summarize",
        "summary_model": "llama3.1:8b",
        "align_mode": "whisperx",
        "pipeline": "default",
        "description": "Whisper turbo + WhisperX align + diarize,VAD + llama3.1:8b",
    },
    "english-speed": {
        "whisper_model": "distil-large-v3",
        "analysis": "diarize,summarize",
        "summary_model": "gemma3:4b",
        "align_mode": "whisperx",
        "pipeline": "default",
        "description": "distil-large-v3 + WhisperX align + diarize + gemma3:4b",
    },
    "multilingual": {
        "whisper_model": "turbo",
        "analysis": "diarize,vad,summarize",
        "summary_model": "qwen2.5:7b",
        "align_mode": "whisperx",
        "pipeline": "default",
        "description": "Whisper turbo + WhisperX align + diarize,VAD + qwen2.5:7b",
    },
    "speaker-aware": {
        "whisper_model": "turbo",
        "analysis": "diarize,summarize",
        "summary_model": "llama3.1:8b",
        "align_mode": "whisperx",
        "pipeline": "default",
        "description": "Whisper turbo + WhisperX align + diarize + llama3.1:8b",
    },
    "whisperx-full": {
        "whisper_model": "turbo",
        "analysis": "summarize",
        "summary_model": "llama3.1:8b",
        "align_mode": "none",
        "pipeline": "whisperx",
        "description": "WhisperX unified (transcribe+align+diarize) + llama3.1:8b",
    },
    "minimal": {
        "whisper_model": "turbo",
        "analysis": "summarize",
        "summary_model": "llama3.1:8b",
        "align_mode": "none",
        "pipeline": "default",
        "description": "Whisper turbo + summarize only + llama3.1:8b",
    },
    "current": {
        "whisper_model": "turbo",
        "analysis": "diarize,vad,emotion,classify,summarize",
        "summary_model": "llama3.1:8b",
        "align_mode": "none",
        "pipeline": "default",
        "description": "Whisper turbo + full analysis (no alignment) + llama3.1:8b",
    },
    "norwegian": {
        "whisper_model": "Necklace/faster-nb-whisper-large",
        "analysis": "diarize,vad,summarize",
        "summary_model": "llama3.1:8b",
        "align_mode": "whisperx",
        "whisper_language": "no",
        "align_model": "NbAiLab/nb-wav2vec2-1b-bokmaal-v2",
        "pipeline": "default",
        "description": "nb-whisper-large (Norwegian-finetuned) + nb-wav2vec2 align + diarize,VAD + llama3.1:8b",
    },
}

TIMING_RATES = {
    "transcribe": (0.032, 2.3),
    "align": (0.074, 4.8),
    "diarize": (0.066, 0),
    "vad": (0.005, 0),
    "emotion": (0.015, 0),
    "classify": (0.008, 0),
    "summarize": (0.171, 10.0),
}


def _run(cmd, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


def _docker_compose(*args):
    return _run(["docker", "compose"] + list(args), cwd=SCRIPT_DIR)


def build_image():
    if os.environ.get("SKIP_BUILT") != "1":
        print("\nBuilding Docker image...")
        _docker_compose("build", "--quiet")


def ensure_ollama():
    r = _run(["docker", "ps", "--format", "{{.Names}}"])
    if "whisper-ollama" in r.stdout:
        r2 = _run(["docker", "exec", "whisper-ollama", "ollama", "list"], capture_output=True)
        if r2.returncode == 0:
            return
    print("  Starting Ollama...")
    _docker_compose("--profile", "summarize", "up", "-d", "ollama")
    for attempt in range(60):
        r = _run(["docker", "exec", "whisper-ollama", "ollama", "list"], capture_output=True)
        if r.returncode == 0:
            print("  Ollama ready.")
            return
        if attempt == 5:
            print("  Waiting for Ollama container...")
        time.sleep(3)
    print("WARNING: Ollama did not start in time")


def stop_ollama():
    _docker_compose("--profile", "summarize", "stop", "ollama")


def unload_ollama_models():
    models = [os.environ.get("SUMMARY_OLLAMA_MODEL", "llama3.1:8b"), "qwen2.5:7b", "qwen2.5:14b"]
    for model in models:
        try:
            url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
            payload = json.dumps({"model": model, "keep_alive": "0"}).encode()
            req = urllib.request.Request(
                f"{url}/api/generate", data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass
    time.sleep(2)


def acquire_gpu_lock(no_wait=False):
    lock_path = "/tmp/whisper-gpu.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        if no_wait:
            print("GPU busy — use without --no-wait to wait")
            cleanup_batch_tmp()
            sys.exit(1)
        print("Waiting for GPU lock...")
        fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def find_audio_files(input_dir, max_files=0):
    include_patterns = os.environ.get("AUDIO_INCLUDE", "")
    include_file = os.environ.get("AUDIO_INCLUDE_FILE", "")
    include_paths = set()
    if include_file and os.path.exists(include_file):
        with open(include_file) as fp:
            for line in fp:
                line = line.strip()
                if line and os.path.exists(line):
                    include_paths.add(os.path.abspath(line))
    include_exts = set()
    if include_patterns:
        for p in include_patterns.split(","):
            p = p.strip().lower()
            if p.startswith("."):
                include_exts.add(p)
    files = []
    for root, _dirs, names in os.walk(input_dir):
        if "/." in root:
            continue
        for name in names:
            ext = os.path.splitext(name)[1].lower()
            if ext in AUDIO_EXTENSIONS:
                if include_paths:
                    full = os.path.abspath(os.path.join(root, name))
                    if full not in include_paths:
                        continue
                elif include_exts and ext not in include_exts:
                    continue
                files.append(os.path.join(root, name))
    files.sort()
    if max_files > 0 and max_files < len(files):
        files = files[:max_files]
    return files


def generate_batch_json(files, host_input_dir, output_path):
    jobs = []
    for i, f in enumerate(files):
        rel = os.path.relpath(f, host_input_dir)
        jobs.append({
            "input": f"/batch-input/{rel}",
            "output": f"/batch-output/{i}",
            "name": os.path.basename(f),
        })
    with open(output_path, "w") as fp:
        json.dump(jobs, fp, indent=2)
    print(f"Wrote {len(jobs)} jobs")


def run_container(batch_json, batch_output, env_extra=None):
    env = {
        "PYTHONUNBUFFERED": "1",
        "WHISPER_MODEL": os.environ.get("WHISPER_MODEL", "turbo"),
        "HF_TOKEN": os.environ.get("HF_TOKEN", ""),
        "ANALYSIS": os.environ.get("ANALYSIS", ""),
    }
    hf_token = os.environ.get("HF_TOKEN", "")
    if hf_token:
        env["HF_TOKEN"] = hf_token

    analysis = env.get("ANALYSIS", "")
    if "summarize" in analysis:
        env["SUMMARY_BACKEND"] = os.environ.get("SUMMARY_BACKEND", "ollama")
        env["SUMMARY_COMPARE"] = os.environ.get("SUMMARY_COMPARE", "")
        env["SUMMARY_OLLAMA_MODEL"] = os.environ.get("SUMMARY_OLLAMA_MODEL", "llama3.1:8b")
        env["SUMMARY_SKIP_SYNTHESIS"] = os.environ.get("SUMMARY_SKIP_SYNTHESIS", "true")
        env["SUMMARY_HF_MODEL"] = os.environ.get("SUMMARY_HF_MODEL", "google/flan-t5-large")

    for key in (
        "GROUPED",
        "PARALLEL_ANALYSIS",
        "SKIP_TRANSCRIBE",
        "SKIP_ANALYSIS",
        "SKIP_SUMMARIZE",
        "ALIGN_MODE",
        "PIPELINE",
        "STRATEGY",
        "WHISPER_LANGUAGE",
        "DIARIZE_MIN_SPEAKERS",
        "DIARIZE_MAX_SPEAKERS",
        "ALIGN_MODEL",
    ):
        if key == "GROUPED":
            val = os.environ.get(key, "true")
        else:
            val = os.environ.get(key, "")
        if val:
            env[key] = val

    if env_extra:
        env.update(env_extra)

    input_dir = os.environ["INPUT_DIR"]
    cmd = [
        "docker", "compose", "run", "--rm",
    ]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [
        "-v", f"{batch_json}:/batch-files.json:ro",
        "-v", f"{input_dir}:/batch-input:ro",
        "-v", f"{batch_output}:/batch-output",
        "--entrypoint", "python3",
        "transcribe",
        "/app/transcribe.py", "--batch", "/batch-files.json",
    ]

    r = subprocess.run(cmd, cwd=SCRIPT_DIR)
    return r.returncode


def run_container_summarize(batch_output):
    env = {
        "PYTHONUNBUFFERED": "1",
        "SUMMARY_BACKEND": os.environ.get("SUMMARY_BACKEND", "ollama"),
        "SUMMARY_OLLAMA_MODEL": os.environ.get("SUMMARY_OLLAMA_MODEL", "llama3.1:8b"),
        "SUMMARY_SKIP_SYNTHESIS": os.environ.get("SUMMARY_SKIP_SYNTHESIS", "true"),
        "OLLAMA_URL": "http://ollama:11434",
    }
    cmd = [
        "docker", "compose", "run", "--rm",
    ]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [
        "-v", f"{batch_output}:/batch-output",
        "--entrypoint", "python3",
        "transcribe",
        "/app/transcribe.py", "--summarize-batch", "/batch-output",
    ]
    r = subprocess.run(cmd, cwd=SCRIPT_DIR)
    return r.returncode


def distribute_outputs(strategy_name, files, batch_output):
    processed = 0
    failed = 0
    for i, src in enumerate(files):
        base = os.path.splitext(os.path.basename(src))[0]
        local_out = os.path.join(batch_output, str(i))
        if strategy_name == "__default__":
            file_out = os.path.join(os.path.dirname(src), base)
        else:
            file_out = os.path.join(os.path.dirname(src), base, strategy_name)
        os.makedirs(file_out, exist_ok=True)
        full_json = os.path.join(local_out, "full.json")
        if os.path.isdir(local_out) and os.path.exists(full_json):
            for item in os.listdir(local_out):
                src_item = os.path.join(local_out, item)
                dst_item = os.path.join(file_out, item)
                if os.path.isdir(src_item):
                    if os.path.exists(dst_item):
                        shutil.rmtree(dst_item)
                    shutil.copytree(src_item, dst_item)
                else:
                    shutil.copy2(src_item, dst_item)
            processed += 1
        else:
            failed += 1
    return processed, failed


def generate_index(input_dir, all_files, strategy, has_strategies=False):
    index_path = os.path.join(input_dir, "_index.md")
    lines = ["# Batch Transcription Index", ""]

    if has_strategies:
        lines.append("| # | File | Language | Duration | Strategies |")
        lines.append("|---|---|---|---|---|")
    else:
        lines.append("| # | File | Language | Duration | Speakers | Time |")
        lines.append("|---|---|---|---|---|---|")
    lines.append("")

    for idx, f in enumerate(all_files, 1):
        base = os.path.splitext(os.path.basename(f))[0]
        name_dir = os.path.join(os.path.dirname(f), base)
        base_name = os.path.basename(f)
        link = f"[{base_name}]({base}/)"

        if has_strategies:
            strategies_found = []
            lang = "—"
            dur = "—"
            if os.path.isdir(name_dir):
                for sd in sorted(os.listdir(name_dir)):
                    sd_path = os.path.join(name_dir, sd)
                    if not os.path.isdir(sd_path) or sd == "_compare":
                        continue
                    timing = os.path.join(sd_path, "timing.json")
                    if not os.path.exists(timing):
                        continue
                    strategies_found.append(sd)
                    try:
                        with open(timing) as fp:
                            t = json.load(fp)
                        if lang == "—":
                            lang = t.get("language", "?")
                        if dur == "—":
                            dur = f"{t.get('total', 0):.0f}s"
                    except Exception:
                        pass
            strategies_str = ", ".join(strategies_found) if strategies_found else "—"
            lines.append(f"| {idx} | {link} | {lang} | {dur} | {strategies_str} |")
        else:
            strat_dir = os.path.join(name_dir, strategy) if strategy else name_dir
            analysis_file = os.path.join(strat_dir, "analysis.json")
            if not os.path.exists(analysis_file):
                analysis_file = os.path.join(name_dir, "analysis.json")
            if not os.path.exists(analysis_file):
                continue
            try:
                with open(analysis_file) as fp:
                    a = json.load(fp)
                lang = a.get("language", "?")
                speakers = a.get("speakers", [])
                speakers_str = ", ".join(speakers) if speakers else "—"
                duration = a.get("pipeline_duration", 0)
                proc_time = f"{duration:.1f}s"
                lines.append(f"| {idx} | {link} | {lang} | {proc_time} | {speakers_str} | {proc_time} |")
            except Exception:
                continue

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("Generated by whisper-docker")
    lines.append(f"Date: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")

    with open(index_path, "w") as fp:
        fp.write("\n".join(lines) + "\n")
    print(f"Wrote: {index_path}")


def estimate_time(audio_seconds, stages_str):
    stages = [s.strip() for s in stages_str.split(",") if s.strip()] if stages_str else []
    t = audio_seconds * TIMING_RATES["transcribe"][0] + TIMING_RATES["transcribe"][1]
    for stage in stages:
        if stage in TIMING_RATES:
            rate, overhead = TIMING_RATES[stage]
            t += audio_seconds * rate + overhead
    return int(t)


def format_duration(seconds):
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


def cleanup_batch_tmp():
    tmpdir = os.path.join(SCRIPT_DIR, ".batch-tmp")
    if os.path.isdir(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)
    os.makedirs(tmpdir, exist_ok=True)
    os.chmod(tmpdir, 0o777)


def run_llm_compare(prompt, model=None):
    if model is None:
        model = os.environ.get("COMPARE_MODEL", "llama3.1:8b")
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 4096, "num_ctx": 8192},
    }).encode()
    try:
        req = urllib.request.Request(
            f"{url}/api/generate", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode())
            return data.get("response", "")
    except Exception:
        return "ERROR: Failed to get LLM response"


def comparison_per_file(file_path, strategies):
    base = os.path.splitext(os.path.basename(file_path))[0]
    file_dir = os.path.join(os.path.dirname(file_path), base)
    compare_dir = os.path.join(file_dir, "_compare")
    os.makedirs(compare_dir, exist_ok=True)

    metrics = {}
    for s in strategies:
        timing_path = os.path.join(file_dir, s, "timing.json")
        summary_path = os.path.join(file_dir, s, "summary.json")
        if not os.path.exists(timing_path):
            continue
        try:
            with open(timing_path) as fp:
                t = json.load(fp)
        except Exception:
            continue
        m = {
            "strategy": s,
            "whisper_model": t.get("whisper_model", "?"),
            "pipeline": t.get("pipeline", "default"),
            "align_mode": t.get("align_mode", "none"),
            "analysis": t.get("analysis", []),
            "summarizer": t.get("summarizer", ""),
            "total": t.get("total", 0),
            "segments": t.get("segments", 0),
            "topics_found": t.get("topics_found", 0),
            "language": t.get("language", "?"),
            "stages": t.get("stages", {}),
            "spell_fixes": t.get("spell_fixes", 0),
        }
        if os.path.exists(summary_path):
            try:
                with open(summary_path) as fp:
                    summary = json.load(fp)
                m["summary_overview"] = summary.get("overview", "")[:300]
                m["sentiment"] = summary.get("sentiment", "")
                m["decisions_count"] = len(summary.get("all_decisions", []))
                m["questions_count"] = len(summary.get("all_questions", []))
                m["action_items_count"] = len(summary.get("all_action_items", []))
            except Exception:
                pass
        metrics[s] = m

    with open(os.path.join(compare_dir, "metrics.json"), "w") as fp:
        json.dump(metrics, fp, indent=2)
    return metrics


def comparison_prompt(metrics, file_name):
    lines = [
        "You are comparing transcription pipeline strategies applied to the same audio file.",
        "",
        f"## File: {file_name}",
        "",
    ]
    for sname, m in metrics.items():
        stages_summary = ", ".join(f"{k}={v:.1f}s" for k, v in m.get("stages", {}).items())
        lines.append(f"### Strategy: {sname}")
        lines.append(f"- Whisper model: {m.get('whisper_model', '?')}")
        lines.append(f"- Pipeline: {m.get('pipeline', 'default')}")
        lines.append(f"- Alignment: {m.get('align_mode', 'none')}")
        lines.append(f"- Analysis stages: {m.get('analysis', [])}")
        lines.append(f"- Summarizer: {m.get('summarizer', '?')}")
        lines.append(f"- Total time: {m.get('total', 0):.1f}s")
        lines.append(f"- Stage timing: {stages_summary}")
        lines.append(f"- Segments: {m.get('segments', 0)}")
        lines.append(f"- Spell fixes: {m.get('spell_fixes', 0)}")
        lines.append(f"- Topics found: {m.get('topics_found', 0)}")
        if m.get("decisions_count") is not None:
            lines.append(
                f"- Decisions: {m.get('decisions_count', 0)}, "
                f"Questions: {m.get('questions_count', 0)}, "
                f"Actions: {m.get('action_items_count', 0)}"
            )
        overview = m.get("summary_overview", "")
        if overview:
            lines.append(f"- Summary: {overview}")
        lines.append("")
    lines.extend([
        "For each strategy, evaluate:",
        "1. **PROS**: What this strategy does well (speed, accuracy, topic coverage, etc.)",
        "2. **CONS**: What this strategy does poorly",
        "3. **BEST FOR**: What use case this strategy is best suited for",
        "",
        "Then give a **RECOMMENDATION**: which strategy produced the best results for this file and why.",
        "Focus on measurable differences in timing, topic coverage, and output quality.",
    ])
    return "\n".join(lines)


def comparison_fallback_md(metrics, file_name, output_path):
    lines = [f"# Strategy Comparison: {file_name}", ""]
    for s, m in metrics.items():
        lines.append(f"## {s}")
        lines.append(
            f"- Total: {m.get('total', 0):.1f}s | "
            f"Segments: {m.get('segments', 0)} | "
            f"Topics: {m.get('topics_found', 0)}"
        )
        lines.append("")
    lines.append("> LLM comparison unavailable. Compare metrics above manually.")
    with open(output_path, "w") as fp:
        fp.write("\n".join(lines) + "\n")


def generate_comparison_md(file_path, strategies):
    base = os.path.splitext(os.path.basename(file_path))[0]
    file_dir = os.path.join(os.path.dirname(file_path), base)
    compare_dir = os.path.join(file_dir, "_compare")
    metrics_file = os.path.join(compare_dir, "metrics.json")
    if not os.path.exists(metrics_file):
        return

    try:
        with open(metrics_file) as fp:
            metrics = json.load(fp)
    except Exception:
        return

    prompt = comparison_prompt(metrics, base)
    response = run_llm_compare(prompt)
    if response and response != "ERROR: Failed to get LLM response":
        md_path = os.path.join(compare_dir, "comparison.md")
        with open(md_path, "w") as fp:
            fp.write(response + "\n")
        print(f"  Wrote: {md_path}")
    else:
        print(f"  WARNING: LLM comparison failed for {base}")
        comparison_fallback_md(metrics, base, os.path.join(compare_dir, "comparison.md"))


def generate_global_comparison(input_dir, strategies):
    global_dir = os.path.join(input_dir, "_compare")
    os.makedirs(global_dir, exist_ok=True)
    csv_path = os.path.join(global_dir, "summary.csv")

    all_files = sorted(glob.glob(os.path.join(input_dir, "**", "_compare", "metrics.json"), recursive=True))
    rows = []
    per_file_data = []
    for mf in all_files:
        file_dir = os.path.dirname(mf)
        audio_dir = os.path.dirname(file_dir)
        file_name = os.path.basename(audio_dir)
        try:
            with open(mf) as fp:
                metrics = json.load(fp)
        except Exception:
            continue
        for sname, m in metrics.items():
            rows.append({
                "file": file_name,
                "strategy": sname,
                "total": m.get("total", 0),
                "segments": m.get("segments", 0),
                "topics": m.get("topics_found", 0),
                "language": m.get("language", ""),
            })
        per_file_data.append({"file": file_name, "strategies": list(metrics.keys())})

    with open(csv_path, "w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=["file", "strategy", "total_time", "segments", "topics", "language"])
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "file": r["file"],
                "strategy": r["strategy"],
                "total_time": f"{r['total']:.1f}",
                "segments": r["segments"],
                "topics": r["topics"],
                "language": r["language"],
            })
    print(f"  Wrote: {csv_path}")

    agg = {}
    for r in rows:
        s = r["strategy"]
        if s not in agg:
            agg[s] = {"times": [], "segments": [], "topics": []}
        agg[s]["times"].append(r["total"])
        agg[s]["segments"].append(r["segments"])
        agg[s]["topics"].append(r["topics"])

    agg_lines = [
        "| Strategy | Avg Time | Min Time | Max Time | Avg Segments | Avg Topics | Files |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in strategies:
        if s not in agg:
            agg_lines.append(f"| {s} | — | — | — | — | — | 0 |")
            continue
        a = agg[s]
        avg_t = sum(a["times"]) / len(a["times"])
        min_t = min(a["times"])
        max_t = max(a["times"])
        avg_s = sum(a["segments"]) / len(a["segments"])
        avg_top = sum(a["topics"]) / len(a["topics"])
        agg_lines.append(
            f"| {s} | {avg_t:.1f}s | {min_t:.1f}s | {max_t:.1f}s "
            f"| {avg_s:.0f} | {avg_top:.0f} | {len(a['times'])} |"
        )
    agg_lines.append("")
    agg_lines.append("## Per-File Results")
    agg_lines.append("")
    for pf in per_file_data:
        agg_lines.append(f"- **{pf['file']}**: {', '.join(pf['strategies'])}")
    agg_text = "\n".join(agg_lines)

    prompt = (
        "You are evaluating transcription pipeline strategies across multiple audio files.\n\n"
        f"{agg_text}\n\n"
        "Provide:\n"
        "1. **Overall ranking** of strategies (best to worst)\n"
        "2. Each strategy's **strengths** and **weaknesses**\n"
        "3. **When to use each strategy** (use cases)\n"
        "4. **Patterns** you notice (e.g., one strategy is faster, another finds more topics)\n\n"
        "Be concise and data-driven. Reference specific timing and metric differences."
    )
    response = run_llm_compare(prompt)
    md_path = os.path.join(global_dir, "comparison.md")
    if response and response != "ERROR: Failed to get LLM response":
        with open(md_path, "w") as fp:
            fp.write(f"# Global Strategy Comparison\n\n{agg_text}\n\n---\n\n{response}\n")
        print(f"  Wrote: {md_path}")
    else:
        with open(md_path, "w") as fp:
            fp.write(f"# Global Strategy Comparison\n\n{agg_text}\n\n> LLM comparison unavailable.\n")
        print("  WARNING: LLM global comparison failed")


def expand_strategy(name):
    if name not in STRATEGIES:
        print(f"ERROR: Unknown strategy: {name}", file=sys.stderr)
        print(f"Available: {', '.join(STRATEGIES.keys())}", file=sys.stderr)
        sys.exit(1)
    s = STRATEGIES[name]
    os.environ["WHISPER_MODEL"] = s["whisper_model"]
    os.environ["ANALYSIS"] = s["analysis"]
    os.environ["SUMMARY_OLLAMA_MODEL"] = s["summary_model"]
    os.environ["ALIGN_MODE"] = s["align_mode"]
    os.environ["PIPELINE"] = s["pipeline"]
    os.environ["STRATEGY"] = name
    if s.get("whisper_language"):
        os.environ["WHISPER_LANGUAGE"] = s["whisper_language"]
    if s.get("diarize_min_speakers") is not None:
        os.environ["DIARIZE_MIN_SPEAKERS"] = str(s["diarize_min_speakers"])
    if s.get("diarize_max_speakers") is not None:
        os.environ["DIARIZE_MAX_SPEAKERS"] = str(s["diarize_max_speakers"])
    if s.get("align_model"):
        os.environ["ALIGN_MODEL"] = s["align_model"]


def run_prefilter(input_dir, files):
    """Run prefilter and return list of files that passed (status='ok')."""
    import importlib.util
    prefilter_path = os.path.join(SCRIPT_DIR, "prefilter.py")
    if not os.path.exists(prefilter_path):
        print("  Prefilter not found, skipping.")
        return files
    spec = importlib.util.spec_from_file_location("prefilter", prefilter_path)
    if spec is None or spec.loader is None:
        print("  Prefilter could not be loaded, skipping.")
        return files
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    results = mod.scan_directory(input_dir)
    ok_rel = {r["file"] for r in results if r["status"] == "ok"}
    filtered = []
    for f in files:
        rel = os.path.relpath(f, input_dir)
        if rel in ok_rel:
            filtered.append(f)
    skipped = len(files) - len(filtered)
    if skipped > 0:
        status_counts = {}
        for r in results:
            s = r["status"]
            if s != "ok":
                status_counts[s] = status_counts.get(s, 0) + 1
        parts = [f"{s}: {c}" for s, c in sorted(status_counts.items(), key=lambda x: -x[1])]
        print(f"  Prefilter: {len(filtered)} OK, {skipped} skipped ({', '.join(parts)})")
    else:
        print(f"  Prefilter: all {len(files)} files OK")
    return filtered


def _files_to_process(files, strategy, phase, force):
    to_process = []
    for f in files:
        base = os.path.splitext(os.path.basename(f))[0]
        if strategy:
            if phase == "2":
                marker = os.path.join(os.path.dirname(f), base, strategy, "analysis.json")
            else:
                marker = os.path.join(os.path.dirname(f), base, strategy, "full.json")
        else:
            marker = os.path.join(os.path.dirname(f), base, "full.json")
        if os.path.exists(marker) and not force:
            continue
        to_process.append(f)
    return to_process


def run_single_mode(input_dir, args):
    strategy_label = os.environ.get("STRATEGY", "default")
    grouped = os.environ.get("GROUPED", "false").lower() in ("true", "1", "yes")
    mode_label = "grouped (stage-by-stage)" if grouped else "sequential"

    all_files = find_audio_files(input_dir, args.compare_files)
    if not all_files:
        print(f"ERROR: No audio files found in '{input_dir}'")
        sys.exit(1)

    files = all_files
    print("=== Batch Transcription ===")
    print(f"Source:      {input_dir}")
    print(f"Output:      (folders created next to each audio file)")
    print(f"Files:       {len(files)} ({len(all_files)} total)")
    print(f"Strategy:    {strategy_label}")
    print(f"Mode:        {mode_label}")
    print(f"Analysis:    {os.environ.get('ANALYSIS', 'diarize,vad,emotion,classify,summarize')}")
    print(f"Force:       {args.force}")
    print(f"Dry run:     {args.dry_run}")
    print("")

    to_process = _files_to_process(files, os.environ.get("STRATEGY"), args.phase1 and "1" or (args.phase2 and "2" or ""), args.force)
    print(f"To process: {len(to_process)}")
    print(f"To skip:    {len(files) - len(to_process)}")

    if not args.force:
        print("\nRunning prefilter...")
        prefiltered = run_prefilter(input_dir, to_process)
        newly_skipped = len(to_process) - len(prefiltered)
        if newly_skipped > 0:
            to_process = prefiltered
            print(f"After prefilter: {len(to_process)} files to process")
    else:
        print("Prefilter skipped (--force)")

    if args.dry_run:
        print("\n=== Dry run complete ===")
        return

    if not to_process:
        print("Nothing to process. Use --force to re-transcribe.")
        cleanup_batch_tmp()
        return

    build_image()

    analysis = os.environ.get("ANALYSIS", "")
    skip_summarize = os.environ.get("SKIP_SUMMARIZE", "false").lower() in ("true", "1", "yes")
    need_summarize = "summarize" in analysis and not skip_summarize

    if need_summarize:
        os.environ["SKIP_SUMMARIZE"] = "true"
        stop_ollama()

    tmpdir = os.path.join(SCRIPT_DIR, ".batch-tmp")
    cleanup_batch_tmp()
    os.makedirs(tmpdir, exist_ok=True)
    batch_json = os.path.join(tmpdir, "files.json")
    batch_output = os.path.join(tmpdir, "output")
    os.makedirs(batch_output, exist_ok=True)

    print("\nGenerating batch JSON...")
    generate_batch_json(to_process, input_dir, batch_json)

    print("\nStarting batch transcription...")
    lock_fd = acquire_gpu_lock(no_wait=args.no_wait)

    docker_exit = run_container(batch_json, batch_output)

    summarize_exit = 0
    if need_summarize and docker_exit == 0:
        print("\n--- Phase 2: Summarization (separate container for GPU isolation) ---")
        ensure_ollama()
        unload_ollama_models()
        summarize_exit = run_container_summarize(batch_output)

    print("\nDistributing output files...")
    processed, failed = distribute_outputs(
        os.environ.get("STRATEGY", "__default__"), files, batch_output
    )

    cleanup_batch_tmp()
    generate_index(input_dir, all_files, os.environ.get("STRATEGY", ""), has_strategies=False)

    print("\n=== Summary ===")
    print(f"Done:    {processed}")
    print(f"Failed:  {failed}")
    print(f"Skipped: {len(files) - len(to_process)}")
    print(f"\nResults in: {input_dir}/")
    print(f"Index:      {input_dir}/_index.md")

    if docker_exit != 0:
        sys.exit(docker_exit)
    if summarize_exit != 0:
        sys.exit(summarize_exit)


def run_compare_mode(input_dir, args):
    strategy_list = [s.strip() for s in args.compare.split(",")]
    all_files = find_audio_files(input_dir, args.compare_files)
    if not all_files:
        print(f"ERROR: No audio files found in '{input_dir}'")
        sys.exit(1)

    print("=== Compare Mode ===")
    print(f"Source:      {input_dir}")
    print(f"Files:       {len(all_files)}")
    print(f"Strategies:  {', '.join(strategy_list)}")
    print("")

    for s in strategy_list:
        desc = STRATEGIES.get(s, {}).get("description", "Unknown")
        print(f"  {s:<18s} {desc}")
    print("")

    if args.dry_run:
        print("=== Dry run complete ===")
        return

    build_image()

    print("Running prefilter...")
    all_files = run_prefilter(input_dir, all_files)
    if not all_files:
        print("All files filtered out by prefilter. Nothing to do.")
        return

    needs_ollama = any("summarize" in STRATEGIES.get(s, {}).get("analysis", "") for s in strategy_list)
    if needs_ollama:
        ensure_ollama()

    tmpdir = os.path.join(SCRIPT_DIR, ".batch-tmp")
    cleanup_batch_tmp()
    os.makedirs(tmpdir, exist_ok=True)

    lock_fd = acquire_gpu_lock(no_wait=args.no_wait)

    for idx, strategy_name in enumerate(strategy_list, 1):
        print(f"\n=== [{idx}/{len(strategy_list)}] Strategy: {strategy_name} ===")
        desc = STRATEGIES.get(strategy_name, {}).get("description", "")
        print(f"  {desc}")
        print("")

        expand_strategy(strategy_name)

        batch_output = os.path.join(tmpdir, f"output-{strategy_name}")
        os.makedirs(batch_output, exist_ok=True)
        batch_json = os.path.join(tmpdir, f"files-{strategy_name}.json")

        print("Generating batch JSON...")
        generate_batch_json(all_files, input_dir, batch_json)

        has_summarize = "summarize" in os.environ.get("ANALYSIS", "")

        if has_summarize:
            print("Phase 1: Transcribe + Analyze (SKIP_SUMMARIZE=true)...")
            os.environ["SKIP_SUMMARIZE"] = "true"
            run_container(batch_json, batch_output)
            del os.environ["SKIP_SUMMARIZE"]
            print("Phase 2: Summarize (Ollama on GPU)...")
            unload_ollama_models()
            run_container_summarize(batch_output)
        else:
            print("Running strategy...")
            run_container(batch_json, batch_output)

        print("Distributing outputs...")
        distribute_outputs(strategy_name, all_files, batch_output)

        if has_summarize:
            unload_ollama_models()

    print("\n=== Generating Comparisons ===")
    ensure_ollama()
    for idx, f in enumerate(all_files, 1):
        base = os.path.basename(f)
        print(f"  [{idx}/{len(all_files)}] {base} ... ", end="", flush=True)
        comparison_per_file(f, strategy_list)
        generate_comparison_md(f, strategy_list)
        print("done")

    print("\n=== Generating Global Comparison ===")
    generate_global_comparison(input_dir, strategy_list)
    stop_ollama()
    generate_index(input_dir, all_files, "", has_strategies=True)

    print(f"\n=== Compare Complete ===")
    print(f"Strategies:  {len(strategy_list)}")
    print(f"Files:       {len(all_files)}")
    print(f"\nResults in:  {input_dir}/")
    print(f"Index:       {input_dir}/_index.md")
    print(f"Comparison:  {input_dir}/_compare/comparison.md")
    print(f"CSV:         {input_dir}/_compare/summary.csv")


def run_distribute_only(input_dir, args):
    tmpdir = os.path.join(SCRIPT_DIR, ".batch-tmp")
    batch_json = os.path.join(tmpdir, "files.json")
    if not os.path.exists(batch_json):
        print(f"ERROR: No batch data found in {tmpdir}")
        sys.exit(1)

    batch_output = os.path.join(tmpdir, "output")
    all_files = find_audio_files(input_dir)

    print("=== Distribute Only Mode ===")
    print(f"Source:      {input_dir}")
    print(f"Batch data:  {tmpdir}")
    print(f"Strategy:    {os.environ.get('STRATEGY', 'default')}")
    print("")

    processed, failed = distribute_outputs(
        os.environ.get("STRATEGY", "__default__"), all_files, batch_output
    )
    generate_index(input_dir, all_files, os.environ.get("STRATEGY", ""), has_strategies=False)

    print(f"\n=== Summary ===")
    print(f"Distributed: {processed}")
    print(f"Failed:      {failed}")
    print(f"\nResults in: {input_dir}/")
    print(f"Index:      {input_dir}/_index.md")


def run_interactive(input_dir, args):
    all_files = find_audio_files(input_dir)
    if not all_files:
        print(f"ERROR: No audio files found in '{input_dir}'")
        sys.exit(1)

    print("\n=== Whisper Transcription - Interactive Mode ===")
    print(f"\nSource: {input_dir}")
    print(f"Files:  {len(all_files)}")
    print("")

    total_audio = 0
    r = _run(
        ["find", input_dir, "-type", "f"] +
        [f"-iname *{ext}" for ext in AUDIO_EXTENSIONS] +
        ["! -path */.*", "-exec", "ffprobe", "-v", "quiet",
         "-show_entries", "format=duration", "-of", "csv=p=0", "{}", ";"],
        capture_output=True,
    )
    for line in r.stdout.strip().split("\n"):
        try:
            total_audio += float(line)
        except ValueError:
            pass
    total_audio = int(total_audio)

    if total_audio == 0:
        print("ERROR: Could not measure audio duration")
        sys.exit(1)

    print(f"Total audio: {format_duration(total_audio)}")
    print()

    print("What type of content is this?")
    print()
    print("  1) Meeting / ID check     - Structured conversations, 2-3 speakers")
    print("  2) Podcast                - Long-form conversation, multiple speakers")
    print("  3) Lecture / Presentation - Single speaker, educational")
    print("  4) Phone call / Voicemail  - 2-party conversation")
    print("  5) Quick transcription    - Just need the text, nothing else")
    print("  6) Custom                 - Choose stages yourself")
    print()
    choice = input("Choice [1-6]: ").strip()
    print()

    proposed_strategy = ""
    proposed_stages = ""
    proposed_align = "none"
    proposed_summarizer = "llama3.1:8b"
    proposed_phase = "single"

    presets = {
        "1": ("speaker-aware", "diarize,summarize", "whisperx"),
        "2": ("speaker-aware", "diarize,summarize", "whisperx"),
        "3": ("minimal", "summarize", "none"),
        "4": ("speaker-aware", "diarize,summarize", "whisperx"),
        "5": ("minimal", "", "none"),
    }

    if choice == "6":
        proposed_stages = "custom"
    elif choice in presets:
        proposed_strategy, proposed_stages, proposed_align = presets[choice]
    else:
        proposed_strategy = "speaker-aware"
        proposed_stages = "diarize,summarize"
        proposed_align = "whisperx"

    if proposed_stages == "custom":
        print("Choose analysis stages:")
        print("  1) Diarize (speaker identification)    - ~0.07x realtime")
        print("  2) VAD (voice activity detection)      - ~0.01x realtime")
        print("  3) Emotion detection                   - ~0.02x realtime")
        print("  4) Audio classification (genre)        - ~0.01x realtime")
        print("  5) Summarize (LLM topic extraction)    - ~0.17x realtime + 10s overhead")
        print()
        stage_nums = input("Enter stage numbers, comma-separated (e.g. 1,5): ").strip()
        print()

        stage_map = {"1": "diarize", "2": "vad", "3": "emotion", "4": "classify", "5": "summarize"}
        proposed_stages = ",".join(stage_map.get(n, "") for n in stage_nums.split(",") if n in stage_map)

        align_choice = input("Include WhisperX alignment? (y/n) [n]: ").strip()
        if align_choice.lower() == "y":
            proposed_align = "whisperx"

    align_label = "yes" if proposed_align == "whisperx" else "no"
    print("Proposed workflow:")
    print(f"  Stages:       {proposed_stages or 'transcribe only'}")
    print(f"  Alignment:    {align_label}")
    print("")

    transcribe_only_time = estimate_time(total_audio, "")
    full_time = estimate_time(total_audio, proposed_stages)

    if proposed_stages:
        align_stages = f"align,{proposed_stages}" if proposed_align == "whisperx" else proposed_stages
        with_align_time = estimate_time(total_audio, align_stages)
        print("Timing estimates (stage-grouped, parallel analysis):")
        print(f"  Transcribe only:             {format_duration(transcribe_only_time)}  ({transcribe_only_time / total_audio:.2f}x realtime)")
        print(f"  + analysis (proposed):       {format_duration(with_align_time)}  ({with_align_time / total_audio:.2f}x realtime)")
        print(f"  Without alignment:           {format_duration(full_time)}  ({full_time / total_audio:.2f}x realtime)")
    else:
        print("Timing estimate:")
        print(f"  Transcribe only:             {format_duration(transcribe_only_time)}  ({transcribe_only_time / total_audio:.2f}x realtime)")
    print()

    print("Run mode:")
    print("  1) Single phase  - Everything in one run")
    print("  2) Two-phase     - Phase 1: transcribe now, Phase 2: analyze later")
    print()
    phase_choice = input("Choice [1]: ").strip() or "1"
    print()
    proposed_phase = "two" if phase_choice == "2" else "single"

    print("Choose summarizer model:")
    print("  1) llama3.1:8b   (6.8GB VRAM, best quality for Scandinavian)")
    print("  2) gemma3:4b     (3.3GB VRAM, faster, good quality)")
    print("  3) qwen2.5:7b    (4.4GB VRAM, fast, may have Chinese contamination)")
    print("  4) None          (skip summarization)")
    print()
    summarizer_choice = input("Choice [1]: ").strip() or "1"
    print()

    summarizer_map = {"1": "llama3.1:8b", "2": "gemma3:4b", "3": "qwen2.5:7b"}
    if summarizer_choice in summarizer_map:
        proposed_summarizer = summarizer_map[summarizer_choice]
    elif summarizer_choice == "4":
        proposed_summarizer = "none"
        proposed_stages = proposed_stages.replace("summarize", "").replace(",,", ",").strip(",")

    print("=== Final Plan ===")
    print(f"  Files:         {len(all_files)}")
    print(f"  Total audio:   {format_duration(total_audio)}")
    print(f"  Stages:        {proposed_stages or 'transcribe only'}")
    print(f"  Alignment:     {align_label}")
    print(f"  Summarizer:    {proposed_summarizer}")
    print(f"  Phase:         {proposed_phase}")
    print("  Mode:          grouped (stage-by-stage, models loaded once)")
    print("")

    if proposed_phase == "two":
        print("This will create two runs:")
        phase1_stages = "align" if proposed_align == "whisperx" else ""
        phase1_time = estimate_time(total_audio, phase1_stages)
        phase2_time = estimate_time(total_audio, proposed_stages)
        print(f"  Phase 1 (transcribe+align):   ~{format_duration(phase1_time)}")
        print(f"  Phase 2 (analysis):           ~{format_duration(phase2_time)}")
    else:
        final_stages = f"align,{proposed_stages}" if proposed_align == "whisperx" else proposed_stages
        final_time = estimate_time(total_audio, final_stages)
        print(f"  Estimated total:               ~{format_duration(final_time)}")
    print()

    confirm = input("Proceed? (y/n) [y]: ").strip()
    if confirm.lower() == "n":
        print("Aborted.")
        return

    if args.dry_run:
        print("\n=== Dry run - not executing ===")
        return

    os.environ["STRATEGY"] = "interactive"
    os.environ["ANALYSIS"] = proposed_stages
    os.environ["SUMMARY_OLLAMA_MODEL"] = proposed_summarizer
    os.environ["ALIGN_MODE"] = proposed_align
    os.environ["GROUPED"] = "true"
    os.environ["PARALLEL_ANALYSIS"] = "true"

    log_file = f"/tmp/whisper-batch-{time.strftime('%Y%m%d-%H%M%S')}.log"

    if proposed_phase == "two":
        print("\n=== Phase 1: Transcribe + Align ===")
        print(f"Log: {log_file}")
        print("")
        os.environ["SKIP_ANALYSIS"] = "true"
        _run(["nohup", sys.executable, __file__, "--strategy", "interactive", "--phase1", input_dir],
             stdout=open(log_file, "w"), stderr=subprocess.STDOUT)
        print("Started Phase 1")
        print(f"\nAfter Phase 1 completes, run Phase 2:")
        print(f"  {sys.executable} {__file__} --strategy interactive --phase2 {input_dir}")
        print(f"\nMonitor:  tail -f {log_file}")
    else:
        print(f"\nLog: {log_file}")
        print("")
        _run(["nohup", sys.executable, __file__, "--strategy", "interactive", input_dir],
             stdout=open(log_file, "w"), stderr=subprocess.STDOUT)
        print("Started")
        print(f"\nMonitor:  tail -f {log_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Batch transcription with strategy presets and comparison mode.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s --strategy best-overall ~/recordings
  %(prog)s --compare best-overall,current ~/recordings
  %(prog)s --compare best-overall,current --compare-files 2 ~/recordings
  %(prog)s --force ~/recordings
  %(prog)s --interactive ~/recordings
""",
    )
    parser.add_argument("directory", nargs="?", default="", help="Directory to scan for audio files")
    parser.add_argument("--strategy", "-s", default="", help="Use a strategy preset")
    parser.add_argument("--compare", "-c", default="", help="Comma-separated strategies to compare")
    parser.add_argument("--compare-files", type=int, default=0, help="Limit comparison to first N files")
    parser.add_argument("--phase1", action="store_true", help="Transcribe + align only")
    parser.add_argument("--phase2", action="store_true", help="Run analysis on existing transcripts")
    parser.add_argument("--grouped", action="store_true", help="Stage-grouped batch processing")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive mode")
    parser.add_argument("--force", "-f", action="store_true", help="Re-transcribe even if output exists")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed")
    parser.add_argument("--no-wait", action="store_true", help="Exit if GPU is busy")
    parser.add_argument("--distribute-only", action="store_true", help="Distribute outputs from .batch-tmp")
    parser.add_argument("--strategies", action="store_true", help="List available strategy presets")
    parser.add_argument("--prefilter", action="store_true", help="Run prefilter only (no transcription)")

    args = parser.parse_args()

    if args.strategies:
        for name, s in STRATEGIES.items():
            print(f"  {name:<18s} {s['description']}")
        return

    input_dir = args.directory
    if not input_dir:
        parser.print_help()
        sys.exit(1)

    input_dir = os.path.abspath(args.directory)
    if not os.path.isdir(input_dir):
        print(f"ERROR: '{input_dir}' is not a directory")
        sys.exit(1)

    if args.strategy and args.compare:
        print("ERROR: Cannot use --strategy and --compare together")
        sys.exit(1)

    if (args.phase1 or args.phase2) and not args.strategy:
        print("ERROR: --phase1/--phase2 requires --strategy")
        sys.exit(1)

    if args.compare and (args.phase1 or args.phase2):
        print("ERROR: Cannot use --phase1/--phase2 with --compare")
        sys.exit(1)

    if os.path.exists(os.path.join(SCRIPT_DIR, ".env")):
        with open(os.path.join(SCRIPT_DIR, ".env")) as fp:
            for line in fp:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())

    os.environ["INPUT_DIR"] = input_dir

    if args.distribute_only:
        run_distribute_only(input_dir, args)
    elif args.prefilter:
        run_prefilter(input_dir, find_audio_files(input_dir, args.compare_files))
    elif args.interactive:
        run_interactive(input_dir, args)
    elif args.compare:
        run_compare_mode(input_dir, args)
    else:
        if args.strategy:
            expand_strategy(args.strategy)

        if args.phase1:
            os.environ["SKIP_ANALYSIS"] = "true"
            print("=== Phase 1: Transcribe + Align only ===")
        elif args.phase2:
            os.environ["SKIP_TRANSCRIBE"] = "true"
            print("=== Phase 2: Analysis only (using existing transcripts) ===")

        if args.grouped:
            os.environ["GROUPED"] = "true"

        os.environ["PARALLEL_ANALYSIS"] = "true"
        run_single_mode(input_dir, args)


if __name__ == "__main__":
    main()
