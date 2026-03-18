"""Local GPU-accelerated audio transcription using faster-whisper."""

import glob
import json
import os
import sys
import time

INPUT_DIR = "/input"
OUTPUT_DIR = "/output"
SUPPORTED_EXTENSIONS = ("*.wav", "*.mp3", "*.m4a", "*.flac", "*.ogg", "*.webm")


def find_audio_file(input_dir: str) -> str | None:
    """Return the first matching audio file in input_dir, or None."""
    for pattern in SUPPORTED_EXTENSIONS:
        matches = sorted(glob.glob(os.path.join(input_dir, pattern)))
        if matches:
            return matches[0]
    return None


def main() -> None:
    # Locate input audio file
    audio_path = find_audio_file(INPUT_DIR)
    if audio_path is None:
        print(f"ERROR: No audio file found in {INPUT_DIR}/")
        print(f"       Supported extensions: {', '.join(SUPPORTED_EXTENSIONS)}")
        sys.exit(1)

    filename = os.path.basename(audio_path)
    print(f"Input file:  {filename}")

    # Import faster-whisper (fails loudly if CUDA is unavailable at load time)
    from faster_whisper import WhisperModel

    use_cuda = True
    try:
        model = WhisperModel("turbo", device="cuda", compute_type="float16")
        print(f"CUDA:        requested")
    except Exception as exc:
        print(f"ERROR: Failed to load WhisperModel on CUDA: {exc}")
        print("       Ensure the container was started with GPU access (--gpus all).")
        sys.exit(1)

    # Run transcription with VAD filtering and word-level timestamps
    print("Transcribing...")
    t0 = time.perf_counter()

    segments_iter, info = model.transcribe(
        audio_path,
        vad_filter=True,
        word_timestamps=True,
    )

    # Materialize the iterator so we can report language info
    segments = list(segments_iter)
    elapsed = time.perf_counter() - t0

    # Report results
    print(f"Language:    {info.language}")
    print(f"Lang prob:   {info.language_probability:.4f}")
    print(f"Segments:    {len(segments)}")
    print(f"Elapsed:     {elapsed:.2f}s")

    # Ensure output directory exists
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Write plain-text transcript
    transcript_path = os.path.join(OUTPUT_DIR, "transcript.txt")
    with open(transcript_path, "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(seg.text.strip() + "\n")
    print(f"Wrote:       {transcript_path}")

    # Write structured JSON segments with timestamps
    segments_path = os.path.join(OUTPUT_DIR, "segments.json")
    segments_data = []
    for seg in segments:
        seg_dict = {
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
        }
        if seg.words:
            seg_dict["words"] = [
                {
                    "word": w.word,
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "probability": round(w.probability, 4),
                }
                for w in seg.words
            ]
        segments_data.append(seg_dict)

    with open(segments_path, "w", encoding="utf-8") as f:
        json.dump(segments_data, f, indent=2, ensure_ascii=False)
    print(f"Wrote:       {segments_path}")


if __name__ == "__main__":
    main()
