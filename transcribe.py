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

    # Build structured segments data (used for both output and diarization alignment)
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

    # Optional: speaker diarization via pyannote.audio (GPU-accelerated).
    # Activated by DIARIZE=true env var; requires HF_TOKEN for gated model access.
    if os.environ.get("DIARIZE") == "true":
        hf_token = os.environ.get("HF_TOKEN", "")
        if not hf_token:
            print("WARNING: DIARIZE enabled but HF_TOKEN not set — skipping diarization")
        else:
            print("Diarizing...")
            t1 = time.perf_counter()
            try:
                from pyannote.audio import Pipeline

                import torch
                _orig_load = torch.load
                torch.load = lambda *a, **kw: _orig_load(*a, **{**kw, "weights_only": False})

                pipeline = Pipeline.from_pretrained(
                    "pyannote/speaker-diarization-3.1",
                    use_auth_token=hf_token,
                )
                pipeline.to(torch.device("cuda"))

                diarization = pipeline(audio_path)

                # Map speaker labels to segments by maximum temporal overlap.
                diarization_turns = []
                for turn, _, speaker in diarization.itertracks(yield_label=True):
                    diarization_turns.append(
                        {"start": turn.start, "end": turn.end, "speaker": speaker}
                    )

                speakers_found = set()
                for seg_dict in segments_data:
                    seg_start = seg_dict["start"]
                    seg_end = seg_dict["end"]
                    best_speaker = None
                    best_overlap = 0.0
                    for turn in diarization_turns:
                        overlap_start = max(seg_start, turn["start"])
                        overlap_end = min(seg_end, turn["end"])
                        overlap = max(0.0, overlap_end - overlap_start)
                        if overlap > best_overlap:
                            best_overlap = overlap
                            best_speaker = turn["speaker"]
                    if best_speaker and best_overlap > 0:
                        seg_dict["speaker"] = best_speaker
                        speakers_found.add(best_speaker)

                diarize_elapsed = time.perf_counter() - t1
                print(f"Diarization: {len(speakers_found)} speakers, {len(diarization_turns)} turns")
                print(f"Diarize time: {diarize_elapsed:.2f}s")
            except Exception as exc:
                print(f"WARNING: Diarization failed — continuing without speaker labels: {exc}")

    # Write plain-text transcript
    transcript_path = os.path.join(OUTPUT_DIR, "transcript.txt")
    with open(transcript_path, "w", encoding="utf-8") as f:
        for seg_dict in segments_data:
            f.write(seg_dict["text"] + "\n")
    print(f"Wrote:       {transcript_path}")

    # Write structured JSON segments with timestamps (may include speaker labels)
    segments_path = os.path.join(OUTPUT_DIR, "segments.json")
    with open(segments_path, "w", encoding="utf-8") as f:
        json.dump(segments_data, f, indent=2, ensure_ascii=False)
    print(f"Wrote:       {segments_path}")


if __name__ == "__main__":
    main()
