"""GPU worker agent — polls the Whisper Transcribe API for pending jobs and processes them."""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("whisper-worker")

CONFIG = {
    "api_url": os.environ.get("API_URL", "").rstrip("/"),
    "worker_token": os.environ.get("WORKER_TOKEN", ""),
    "worker_id": os.environ.get("WORKER_ID", "gpu-1"),
    "poll_interval": int(os.environ.get("POLL_INTERVAL", "5")),
    "max_concurrent_jobs": int(os.environ.get("MAX_CONCURRENT_JOBS", "1")),
    "mode": os.environ.get("MODE", "docker"),
    "whisper_model": os.environ.get("WHISPER_MODEL", "turbo"),
    "retry_max": int(os.environ.get("RETRY_MAX", "3")),
    "retry_base_delay": float(os.environ.get("RETRY_BASE_DELAY", "2")),
}

SHUTDOWN = threading.Event()


def api_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {CONFIG['worker_token']}"}


def request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    last_err: Exception | None = None
    for attempt in range(1, CONFIG["retry_max"] + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_err = exc
            delay = CONFIG["retry_base_delay"] * (2 ** (attempt - 1))
            log.warning("Request failed (attempt %d/%d): %s — retrying in %.1fs", attempt, CONFIG["retry_max"], exc, delay)
            if SHUTDOWN.is_set():
                raise
            time.sleep(delay)
    raise last_err  # type: ignore[misc]


def fetch_pending_job() -> dict | None:
    try:
        resp = request_with_retry(
            "GET",
            f"{CONFIG['api_url']}/api/jobs",
            params={"status": "pending", "limit": "1"},
            headers=api_headers(),
            timeout=30,
        )
        jobs = resp.json().get("jobs", [])
        return jobs[0] if jobs else None
    except Exception as exc:
        log.error("Failed to fetch pending jobs: %s", exc)
        return None


def claim_job(job_id: str) -> bool:
    try:
        resp = request_with_retry(
            "PATCH",
            f"{CONFIG['api_url']}/api/jobs/{job_id}",
            json={"status": "processing", "worker_id": CONFIG["worker_id"]},
            headers=api_headers(),
            timeout=30,
        )
        return resp.json().get("success", False)
    except Exception as exc:
        log.error("Failed to claim job %s: %s", job_id, exc)
        return False


def fail_job(job_id: str, error_message: str) -> None:
    try:
        request_with_retry(
            "PATCH",
            f"{CONFIG['api_url']}/api/jobs/{job_id}",
            json={"status": "failed", "worker_id": CONFIG["worker_id"], "error_message": error_message},
            headers=api_headers(),
            timeout=30,
        )
    except Exception as exc:
        log.error("Failed to mark job %s as failed: %s", job_id, exc)


def download_audio(job_id: str) -> tuple[str, Path]:
    try:
        resp = request_with_retry(
            "GET",
            f"{CONFIG['api_url']}/api/jobs/{job_id}/audio",
            headers=api_headers(),
            timeout=600,
            stream=True,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to download audio: {exc}") from exc

    content_disp = resp.headers.get("Content-Disposition", "")
    filename = "audio.wav"
    if "filename=" in content_disp:
        filename = content_disp.split("filename=")[1].strip('"')

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"whisper-{job_id[:8]}-"))
    audio_path = tmp_dir / filename
    total = 0
    with open(audio_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if SHUTDOWN.is_set():
                raise RuntimeError("Shutdown requested during download")
            f.write(chunk)
            total += len(chunk)

    log.info("Downloaded %s (%d bytes) to %s", filename, total, tmp_dir)
    return filename, tmp_dir


def transcribe_docker(input_path: Path, output_dir: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    dockerfile = repo_root / "Dockerfile"
    if not dockerfile.exists():
        raise RuntimeError(f"Dockerfile not found at {dockerfile}")

    compose_file = repo_root / "compose.yaml"

    subprocess.run(
        [
            "docker", "compose",
            "-f", str(compose_file),
            "run",
            "--rm",
            "-v", f"{input_path}:/input/{input_path.name}:ro",
            "-v", f"{output_dir}:/output:rw",
            "transcribe",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=1800,
    )


def transcribe_direct(input_path: Path, output_dir: Path) -> None:
    from faster_whisper import WhisperModel

    log.info("Loading Whisper model '%s' on CUDA...", CONFIG["whisper_model"])
    model = WhisperModel(CONFIG["whisper_model"], device="cuda", compute_type="float16")

    log.info("Transcribing %s...", input_path.name)
    segments_iter, info = model.transcribe(
        str(input_path),
        vad_filter=True,
        word_timestamps=True,
    )
    segments = list(segments_iter)

    log.info("Language: %s (prob %.2f), %d segments", info.language, info.language_probability, len(segments))

    with open(output_dir / "transcript.txt", "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(seg.text.strip() + "\n")

    segments_data = []
    for seg in segments:
        seg_dict = {
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
        }
        if seg.words:
            seg_dict["words"] = [
                {"word": w.word, "start": round(w.start, 3), "end": round(w.end, 3), "probability": round(w.probability, 4)}
                for w in seg.words
            ]
        segments_data.append(seg_dict)

    with open(output_dir / "segments.json", "w", encoding="utf-8") as f:
        json.dump(segments_data, f, indent=2, ensure_ascii=False)


def upload_results(job_id: str, transcript_path: Path, segments_path: Path) -> None:
    with open(transcript_path, "rb") as t, open(segments_path, "rb") as s:
        resp = request_with_retry(
            "POST",
            f"{CONFIG['api_url']}/api/jobs/{job_id}/results",
            headers=api_headers(),
            files={"transcript": ("transcript.txt", t, "text/plain"), "segments": ("segments.json", s, "application/json")},
            timeout=120,
        )
    if not resp.json().get("success"):
        raise RuntimeError(f"Upload failed: {resp.text}")
    log.info("Results uploaded for job %s", job_id)


def process_job(job: dict) -> None:
    job_id = job["id"]
    filename = job["original_filename"]
    log.info("Processing job %s: %s", job_id, filename)

    if not claim_job(job_id):
        log.warning("Failed to claim job %s — skipping", job_id)
        return

    tmp_dir = None
    try:
        _filename, tmp_dir = download_audio(job_id)
        audio_path = list(Path(tmp_dir).iterdir())[0]
        output_dir = Path(tmp_dir) / "output"
        output_dir.mkdir()

        if CONFIG["mode"] == "docker":
            transcribe_docker(audio_path, output_dir)
        else:
            transcribe_direct(audio_path, output_dir)

        transcript_path = output_dir / "transcript.txt"
        segments_path = output_dir / "segments.json"

        if not transcript_path.exists() or not segments_path.exists():
            raise RuntimeError("Transcription output files not found")

        upload_results(job_id, transcript_path, segments_path)
        log.info("Job %s completed successfully", job_id)

    except Exception as exc:
        log.error("Job %s failed: %s", job_id, exc)
        fail_job(job_id, str(exc))
    finally:
        if tmp_dir and Path(tmp_dir).exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> None:
    required = ["api_url", "worker_token"]
    missing = [k for k in required if not CONFIG[k]]
    if missing:
        log.error("Missing required config: %s. Set via environment or .env file.", ", ".join(missing))
        sys.exit(1)

    log.info("Whisper GPU Worker starting")
    log.info("  API URL:         %s", CONFIG["api_url"])
    log.info("  Worker ID:       %s", CONFIG["worker_id"])
    log.info("  Mode:            %s", CONFIG["mode"])
    log.info("  Model:           %s", CONFIG["whisper_model"])
    log.info("  Max concurrent:  %d", CONFIG["max_concurrent_jobs"])
    log.info("  Poll interval:   %ds", CONFIG["poll_interval"])

    if CONFIG["mode"] == "direct":
        log.info("Pre-loading Whisper model...")
        try:
            from faster_whisper import WhisperModel
            WhisperModel(CONFIG["whisper_model"], device="cuda", compute_type="float16")
            log.info("Model loaded successfully")
        except Exception as exc:
            log.error("Failed to load model: %s", exc)
            sys.exit(1)

    def handle_signal(signum, frame):
        log.info("Received signal %d — shutting down after current jobs finish...", signum)
        SHUTDOWN.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    with ThreadPoolExecutor(max_workers=CONFIG["max_concurrent_jobs"]) as executor:
        futures: set = set()

        while not SHUTDOWN.is_set():
            active_count = len([f for f in futures if not f.done()])
            if active_count < CONFIG["max_concurrent_jobs"]:
                job = fetch_pending_job()
                if job:
                    future = executor.submit(process_job, job)
                    futures.add(future)
                    future.add_done_callback(futures.discard)
                else:
                    time.sleep(CONFIG["poll_interval"])
            else:
                time.sleep(1)

            for f in list(futures):
                if f.done() and f.exception():
                    log.error("Job raised exception: %s", f.exception())

    log.info("Worker shut down cleanly")


if __name__ == "__main__":
    main()
