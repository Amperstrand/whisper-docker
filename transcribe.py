"""Local GPU-accelerated audio transcription and analysis pipeline."""

import gc
import glob
import json
import os
import sys
import time

INPUT_DIR = "/input"
OUTPUT_DIR = "/output"
EMOTION_LABELS = {"hap": "happy", "neu": "neutral", "sad": "sad", "ang": "angry", "exc": "excited", "fru": "frustrated", "fea": "fearful", "sur": "surprised", "oth": "other", "dis": "disgusted", "xxx": "unknown"}
SUPPORTED_EXTENSIONS = ("*.wav", "*.mp3", "*.m4a", "*.flac", "*.ogg", "*.webm")


def find_audio_file(input_dir: str) -> str | None:
    for pattern in SUPPORTED_EXTENSIONS:
        matches = sorted(glob.glob(os.path.join(input_dir, pattern)))
        if matches:
            return matches[0]
    return None


def get_analysis_flags() -> set[str]:
    raw = os.environ.get("ANALYSIS", "")
    if not raw:
        return set()
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def patch_torch_load() -> None:
    import torch
    _orig = torch.load
    torch.load = lambda *a, **kw: _orig(*a, **{**kw, "weights_only": False})


def stage_transcribe(audio_path: str) -> tuple[list[dict], dict]:
    from faster_whisper import WhisperModel

    model = WhisperModel("turbo", device="cuda", compute_type="float16")
    t0 = time.perf_counter()

    segments_iter, info = model.transcribe(
        audio_path, vad_filter=True, word_timestamps=True,
    )
    segments = list(segments_iter)
    elapsed = time.perf_counter() - t0

    segments_data = []
    for seg in segments:
        d = {"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip()}
        if seg.words:
            d["words"] = [
                {"word": w.word, "start": round(w.start, 3), "end": round(w.end, 3), "probability": round(w.probability, 4)}
                for w in seg.words
            ]
        segments_data.append(d)

    print(f"Transcribe:  {len(segments_data)} segments, {elapsed:.2f}s")
    print(f"Language:    {info.language} ({info.language_probability:.4f})")

    del model
    gc.collect()
    return segments_data, {"language": info.language, "language_probability": round(info.language_probability, 4)}


def _map_turns_to_segments(segments_data: list[dict], turns: list[dict], field: str) -> None:
    for seg in segments_data:
        seg_s, seg_e = seg["start"], seg["end"]
        best_val = None
        best_overlap = 0.0
        for turn in turns:
            overlap = max(0.0, min(seg_e, turn["end"]) - max(seg_s, turn["start"]))
            if overlap > best_overlap:
                best_overlap = overlap
                best_val = turn[field]
        if best_val is not None and best_overlap > 0:
            seg[field] = best_val


def stage_diarize(audio_path: str, segments_data: list[dict], analysis: dict) -> None:
    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        print("WARNING: DIARIZE enabled but HF_TOKEN not set — skipping")
        return

    patch_torch_load()
    from pyannote.audio import Pipeline
    import torch

    t0 = time.perf_counter()
    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=hf_token)
    pipeline.to(torch.device("cuda"))

    diarization = pipeline(audio_path)
    turns = []
    speakers = set()
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        turns.append({"start": turn.start, "end": turn.end, "speaker": speaker})
        speakers.add(speaker)

    _map_turns_to_segments(segments_data, turns, "speaker")
    elapsed = time.perf_counter() - t0

    analysis["speakers"] = sorted(speakers)
    analysis["speaker_turns"] = len(turns)
    print(f"Diarize:     {len(speakers)} speakers, {len(turns)} turns, {elapsed:.2f}s")

    del pipeline
    torch.cuda.empty_cache()
    gc.collect()


def stage_vad(audio_path: str, segments_data: list[dict], analysis: dict) -> None:
    import torch

    model, utils = torch.hub.load("snakers4/silero-vad", "silero_vad")
    (get_speech_timestamps, _, read_audio, _, _) = utils

    t0 = time.perf_counter()
    wav = read_audio(audio_path, sampling_rate=16000)
    speech_ts = get_speech_timestamps(wav, model, sampling_rate=16000, return_seconds=True)
    elapsed = time.perf_counter() - t0

    total_speech = sum(t["end"] - t["start"] for t in speech_ts)
    duration = segments_data[-1]["end"] if segments_data else 0
    speech_ratio = round(total_speech / duration, 3) if duration > 0 else 0

    for seg in segments_data:
        seg_s, seg_e = seg["start"], seg["end"]
        speech_dur = 0.0
        for t in speech_ts:
            speech_dur += max(0.0, min(seg_e, t["end"]) - max(seg_s, t["start"]))
        seg["speech_ratio"] = round(speech_dur / (seg_e - seg_s), 3) if seg_e > seg_s else 1.0

    analysis["vad"] = {
        "speech_ratio": speech_ratio,
        "speech_segments": len(speech_ts),
        "total_speech_seconds": round(total_speech, 2),
    }
    print(f"VAD:         {speech_ratio:.0%} speech, {len(speech_ts)} segments, {elapsed:.2f}s")

    del model
    gc.collect()


def stage_emotion(segments_data: list[dict], analysis: dict) -> None:
    from transformers import pipeline as hf_pipeline

    t0 = time.perf_counter()
    classifier = hf_pipeline(
        "audio-classification",
        model="superb/wav2vec2-base-superb-er",
    )

    audio_path = os.environ.get("AUDIO_PATH", "/input/audio.wav")
    import torchaudio

    waveform_full, sr = torchaudio.load(audio_path)

    emotions_found = {}
    for seg in segments_data:
        seg_s, seg_e = seg["start"], seg["end"]
        if seg_e - seg_s < 0.5:
            seg["emotion"] = {"label": "neutral", "score": 0.0}
            continue
        try:
            start_sample = int(seg_s * sr)
            end_sample = min(int(seg_e * sr), waveform_full.shape[1])
            chunk = waveform_full[:, start_sample:end_sample]
            if chunk.shape[1] < 1600:
                seg["emotion"] = {"label": "neutral", "score": 0.0}
                continue
            chunk_16k = torchaudio.transforms.Resample(sr, 16000)(chunk)
            result = classifier(chunk_16k.squeeze().numpy(), top_k=1)
            label = EMOTION_LABELS.get(result[0]["label"].lower(), result[0]["label"].lower())
            score = round(result[0]["score"], 3)
            seg["emotion"] = {"label": label, "score": score}
            emotions_found[label] = emotions_found.get(label, 0) + 1
        except Exception as exc:
            seg["emotion"] = {"label": "neutral", "score": 0.0}
            print(f"  WARNING: emotion on segment {seg_s}-{seg_e}: {exc}")

    elapsed = time.perf_counter() - t0
    analysis["emotions"] = emotions_found
    print(f"Emotion:     {emotions_found}, {elapsed:.2f}s")

    del classifier
    gc.collect()


def stage_classify(audio_path: str, analysis: dict) -> None:
    from transformers import pipeline as hf_pipeline

    t0 = time.perf_counter()
    classifier = hf_pipeline("audio-classification", model="MIT/ast-finetuned-audioset-10-10-0.4593")

    result = classifier(audio_path, top_k=10)
    tags = [{"label": r["label"], "score": round(r["score"], 3)} for r in result]
    elapsed = time.perf_counter() - t0

    analysis["audio_tags"] = tags
    print(f"Classify:    {tags[0]['label']} ({tags[0]['score']:.0%}), {elapsed:.2f}s")

    del classifier
    gc.collect()


def stage_language_id(audio_path: str, analysis: dict) -> None:
    import torch
    from speechbrain.inference.speaker import SpeakerRecognition

    t0 = time.perf_counter()
    lang_id = SpeakerRecognition.from_hparams(
        source="speechbrain/lang-id-commonlanguage_ecapa",
        savedir="/home/ubuntu/.cache/speechbrain/lang-id-ecapa",
    )

    import torchaudio

    signal, sr = torchaudio.load(audio_path)
    with torch.no_grad():
        out_prob, score, index, text_lab = lang_id.classify_batch(signal)
    label = text_lab[0]
    prob = round(float(score[0]), 3)

    elapsed = time.perf_counter() - t0
    analysis["language_id"] = {"label": label, "score": prob}
    print(f"Language ID: {label} ({prob:.0%}), {elapsed:.2f}s")

    del lang_id
    torch.cuda.empty_cache()
    gc.collect()


def main() -> None:
    audio_path = find_audio_file(INPUT_DIR)
    if audio_path is None:
        print(f"ERROR: No audio file found in {INPUT_DIR}/")
        sys.exit(1)

    print(f"Input file:  {os.path.basename(audio_path)}")

    flags = get_analysis_flags()
    print(f"Analysis:    {', '.join(sorted(flags)) if flags else 'transcription only'}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    analysis: dict = {}
    total_t0 = time.perf_counter()

    segments_data, whisper_analysis = stage_transcribe(audio_path)
    analysis.update(whisper_analysis)

    if "diarize" in flags:
        print("Running diarization...")
        try:
            stage_diarize(audio_path, segments_data, analysis)
        except Exception as exc:
            print(f"WARNING: Diarization failed — {exc}")

    if "vad" in flags:
        print("Running VAD...")
        try:
            stage_vad(audio_path, segments_data, analysis)
        except Exception as exc:
            print(f"WARNING: VAD failed — {exc}")

    if "emotion" in flags:
        print("Running emotion detection...")
        os.environ["AUDIO_PATH"] = audio_path
        try:
            stage_emotion(segments_data, analysis)
        except Exception as exc:
            print(f"WARNING: Emotion detection failed — {exc}")

    if "classify" in flags:
        print("Running audio classification...")
        try:
            stage_classify(audio_path, analysis)
        except Exception as exc:
            print(f"WARNING: Audio classification failed — {exc}")

    if "language_id" in flags:
        print("Running language identification...")
        try:
            stage_language_id(audio_path, analysis)
        except Exception as exc:
            print(f"WARNING: Language identification failed — {exc}")

    total_elapsed = time.perf_counter() - total_t0
    analysis["pipeline_duration"] = round(total_elapsed, 2)

    transcript_path = os.path.join(OUTPUT_DIR, "transcript.txt")
    with open(transcript_path, "w", encoding="utf-8") as f:
        for seg in segments_data:
            f.write(seg["text"] + "\n")
    print(f"Wrote:       {transcript_path}")

    segments_path = os.path.join(OUTPUT_DIR, "segments.json")
    with open(segments_path, "w", encoding="utf-8") as f:
        json.dump(segments_data, f, indent=2, ensure_ascii=False)
    print(f"Wrote:       {segments_path}")

    if analysis:
        analysis_path = os.path.join(OUTPUT_DIR, "analysis.json")
        with open(analysis_path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False)
        print(f"Wrote:       {analysis_path}")

    print(f"Total:       {total_elapsed:.2f}s")


if __name__ == "__main__":
    main()
