"""Local GPU-accelerated audio transcription and analysis pipeline."""

import csv
import gc
import glob
import json
import os
import subprocess
import sys
import time
import traceback
import warnings

os.environ["PYTHONWARNINGS"] = "ignore"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["TQDM_DISABLE"] = "1"
warnings.filterwarnings("ignore")
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

import torch

_original_torch_load = torch.load
def _patched_torch_load(*args, **kwargs):
    if "weights_only" not in kwargs or kwargs["weights_only"] is None:
        kwargs["weights_only"] = False
    return _original_torch_load(*args, **kwargs)
torch.load = _patched_torch_load

try:
    import lightning_fabric.utilities.cloud_io as _lf_cio
    _orig_lf_load = _lf_cio._load
    def _patched_lf_load(*args, **kwargs):
        if "weights_only" not in kwargs or kwargs["weights_only"] is None:
            kwargs["weights_only"] = False
        return _orig_lf_load(*args, **kwargs)
    _lf_cio._load = _patched_lf_load
except ImportError:
    pass

try:
    import pyannote.audio.pipelines.voice_activity_detection as _pa_vad
    _orig_pa_vad_init = _pa_vad.VoiceActivityDetection.__init__
    def _patched_pa_vad_init(self, segmentation=None, fscore=False, use_auth_token=None, **kwargs):
        return _orig_pa_vad_init(self, segmentation=segmentation, fscore=fscore, use_auth_token=use_auth_token)
    _pa_vad.VoiceActivityDetection.__init__ = _patched_pa_vad_init
except (ImportError, AttributeError):
    pass

INPUT_DIR = "/input"
OUTPUT_DIR = "/output"
BATCH_FILE = "/batch-files.json"
EMOTION_LABELS = {"hap": "happy", "neu": "neutral", "sad": "sad", "ang": "angry", "exc": "excited", "fru": "frustrated", "fea": "fearful", "sur": "surprised", "oth": "other", "dis": "disgusted", "xxx": "unknown"}
SUPPORTED_EXTENSIONS = ("*.wav", "*.mp3", "*.m4a", "*.flac", "*.ogg", "*.webm")

_VRAM_BUDGET: dict[str, int] = {
    "whisper_turbo": 2000,
    "whisper_large": 3000,
    "whisper_distil": 2000,
    "whisperx_align": 6500,
    "pyannote_diarize": 2500,
    "silero_vad": 100,
    "wav2vec2_emotion": 400,
    "ast_classify": 400,
}


def _available_vram_mb() -> int:
    try:
        free, total = torch.cuda.mem_get_info()
        return free // (1024 * 1024)
    except Exception:
        return 0


def _check_vram(needed_mb: int, name: str) -> bool:
    avail = _available_vram_mb()
    headroom = 500
    if avail < needed_mb + headroom:
        print(f"  WARNING: Skipping {name} — need {needed_mb}MB VRAM, only {avail}MB free")
        return False
    return True


def _log_memory(label: str = "") -> None:
    try:
        vram = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        vram = vram.stdout.strip()
        rss = "0"
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = f"{int(line.split()[1]) / 1024:.0f}M"
                    break
        tag = f" [{label}] " if label else " "
        print(f"  {tag}mem: {rss} RSS, VRAM: {vram}", flush=True)
    except Exception:
        pass


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


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _validate_language_nordic(analysis: dict) -> None:
    whisper_lang = analysis.get("language", "")
    if not whisper_lang:
        return
    nordic_map = {"nob": "no", "nno": "no", "dan": "da", "swe": "sv", "fin": "fi", "isl": "is", "fao": "fo"}
    env_force = os.environ.get("WHISPER_LANGUAGE", "")
    if env_force and env_force != "no":
        return
    try:
        import fasttext
        from huggingface_hub import hf_hub_download
        model_path = hf_hub_download("NbAiLab/nb-nordic-lid", "nb-nordic-lid.ftz")
        model = fasttext.load_model(model_path)
        transcript = analysis.get("_transcript_sample", "")
        if not transcript:
            return
        labels, scores = model.predict(transcript, threshold=0.25)
        if labels and scores:
            detected = labels[0].replace("__label__", "")
            confidence = scores[0]
            mapped = nordic_map.get(detected, detected)
            if confidence > 0.5 and mapped != whisper_lang:
                print(f"  Language validation: Whisper detected '{whisper_lang}' but nb-nordic-lid detected '{detected}' ({confidence:.3f}) — overriding to '{mapped}'")
                analysis["language"] = mapped
                analysis["language_probability"] = round(confidence, 4)
                analysis["language_validation"] = {"original": whisper_lang, "validated": mapped, "model": detected, "confidence": round(confidence, 4)}
        del model
    except Exception:
        pass


def patch_torch_load() -> None:
    import torch
    _orig = torch.load
    torch.load = lambda *a, **kw: _orig(*a, **{**kw, "weights_only": False})


def stage_transcribe(audio_path: str) -> tuple[list[dict], dict]:
    from faster_whisper import WhisperModel
    import torch

    model_name = os.environ.get("WHISPER_MODEL", "turbo")
    print(f"Loading model: {model_name}...", flush=True)
    model = WhisperModel(model_name, device="cuda", compute_type="float16")
    print(f"Model loaded:  {model_name}", flush=True)
    t0 = time.perf_counter()

    beam_size = _env_int("BEAM_SIZE", 5)
    no_speech_threshold = _env_float("NO_SPEECH_THRESHOLD", 0.6)
    condition_on_previous_text = os.environ.get("CONDITION_ON_PREVIOUS_TEXT", "true").lower() in ("true", "1", "yes")
    language = os.environ.get("WHISPER_LANGUAGE", "") or None

    segments_iter, info = model.transcribe(
        audio_path,
        language=language,
        vad_filter=True,
        word_timestamps=True,
        beam_size=beam_size,
        no_speech_threshold=no_speech_threshold,
        condition_on_previous_text=condition_on_previous_text,
    )
    segments = []
    seg_count = 0
    last_progress_time = time.perf_counter()
    progress_interval = 60
    print(f"Transcribing... (model={model_name}, lang={'auto' if not language else language})", flush=True)
    for seg in segments_iter:
        segments.append(seg)
        seg_count += 1
        now = time.perf_counter()
        if now - last_progress_time >= progress_interval:
            elapsed = now - t0
            print(f"  Progress: {seg_count} segments, {elapsed:.0f}s elapsed, last at {seg.end:.1f}s", flush=True)
            last_progress_time = now
    elapsed = time.perf_counter() - t0

    segments_data = []
    for seg in segments:
        d = {"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip(),
             "no_speech_prob": round(seg.no_speech_prob, 4)}
        if seg.words:
            d["words"] = [
                {"word": w.word, "start": round(w.start, 3), "end": round(w.end, 3), "probability": round(w.probability, 4)}
                for w in seg.words
            ]
        segments_data.append(d)

    no_speech_filtered = _check_no_speech(segments_data)
    if no_speech_filtered:
        segments_data = []

    print(f"Transcribe:  {len(segments_data)} segments, {elapsed:.2f}s (model={model_name})")
    print(f"Language:    {info.language} ({info.language_probability:.4f})")

    del model
    gc.collect()
    torch.cuda.empty_cache()
    sample = " ".join(s["text"] for s in segments_data[:20]) if segments_data else ""
    return segments_data, {"language": info.language, "language_probability": round(info.language_probability, 4), "_transcript_sample": sample, "_no_speech_filtered": no_speech_filtered}


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

    diarize_kwargs = {}
    min_speakers = os.environ.get("DIARIZE_MIN_SPEAKERS", "")
    max_speakers = os.environ.get("DIARIZE_MAX_SPEAKERS", "")
    if min_speakers:
        diarize_kwargs["min_speakers"] = int(min_speakers)
    if max_speakers:
        diarize_kwargs["max_speakers"] = int(max_speakers)

    diarization = pipeline(_ensure_wav(audio_path), **diarize_kwargs)
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
    wav = read_audio(_ensure_wav(audio_path), sampling_rate=16000)
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


def stage_emotion(audio_path: str, segments_data: list[dict], analysis: dict) -> None:
    from transformers import pipeline as hf_pipeline

    t0 = time.perf_counter()
    classifier = hf_pipeline(
        "audio-classification",
        model="superb/wav2vec2-base-superb-er",
        device="cuda",
    )

    import torchaudio

    wav_path = _ensure_wav(audio_path)
    waveform_full, sr = torchaudio.load(wav_path)

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
    classifier = hf_pipeline("audio-classification", model="MIT/ast-finetuned-audioset-10-10-0.4593", device="cuda")

    result = classifier(_ensure_wav(audio_path), top_k=10)
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

    signal, sr = torchaudio.load(_ensure_wav(audio_path))
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


def stage_align_whisperx(audio_path: str, segments_data: list[dict], analysis: dict) -> None:
    try:
        import whisperx
    except ImportError:
        print("  WARNING: whisperx not installed — skipping alignment")
        return

    import torch

    language = analysis.get("language", "en")
    device = "cuda"

    t0 = time.perf_counter()
    try:
        align_model_name = os.environ.get("ALIGN_MODEL", "")
        model_a, metadata = whisperx.load_align_model(
            language_code=language,
            device=device,
            model_name=align_model_name if align_model_name else None,
        )
    except Exception as exc:
        print(f"  WARNING: WhisperX alignment model not available for '{language}' — skipping ({exc})")
        return

    audio = whisperx.load_audio(audio_path)
    wx_segments = [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in segments_data]
    result = whisperx.align(wx_segments, model_a, metadata, audio, device, return_char_alignments=False)

    aligned = result.get("segments", [])
    for i, seg in enumerate(segments_data):
        if i < len(aligned):
            if "words" in aligned[i]:
                seg["words"] = [
                    {"word": w.get("word", ""), "start": round(w.get("start", 0), 3),
                     "end": round(w.get("end", 0), 3), "probability": round(w.get("score", 0), 4)}
                    for w in aligned[i]["words"]
                ]
            if "start" in aligned[i]:
                seg["start"] = round(aligned[i]["start"], 3)
            if "end" in aligned[i]:
                seg["end"] = round(aligned[i]["end"], 3)

    elapsed = time.perf_counter() - t0
    print(f"Align:       WhisperX forced alignment, {elapsed:.2f}s (lang={language})")

    del model_a
    gc.collect()
    torch.cuda.empty_cache()


def stage_transcribe_whisperx(audio_path: str) -> tuple[list[dict], dict]:
    try:
        import whisperx
    except ImportError:
        print("  WARNING: whisperx not installed — falling back to faster-whisper")
        return stage_transcribe(audio_path)

    import torch

    model_name = os.environ.get("WHISPER_MODEL", "turbo")
    device = "cuda"
    compute_type = "float16"
    batch_size = _env_int("WHISPERX_BATCH_SIZE", 16)

    t0 = time.perf_counter()

    language = os.environ.get("WHISPER_LANGUAGE", "") or None
    model = whisperx.load_model(model_name, device, compute_type=compute_type)
    audio = whisperx.load_audio(audio_path)
    result = model.transcribe(audio, batch_size=batch_size, language=language)
    language = language or result.get("language", "en")

    del model
    gc.collect()
    torch.cuda.empty_cache()

    align_t0 = time.perf_counter()
    try:
        model_a, metadata = whisperx.load_align_model(language_code=language, device=device)
        result = whisperx.align(result["segments"], model_a, metadata, audio, device, return_char_alignments=False)
        del model_a
        gc.collect()
        torch.cuda.empty_cache()
    except Exception as exc:
        print(f"  WARNING: WhisperX alignment failed — {exc}")

    speakers = set()
    speaker_turns = 0
    hf_token = os.environ.get("HF_TOKEN", "")
    if hf_token:
        try:
            from whisperx.diarize import DiarizationPipeline
            diarize_model = DiarizationPipeline(token=hf_token, device=device)
            diarize_kwargs = {}
            min_speakers = os.environ.get("DIARIZE_MIN_SPEAKERS", "")
            max_speakers = os.environ.get("DIARIZE_MAX_SPEAKERS", "")
            if min_speakers:
                diarize_kwargs["min_speakers"] = int(min_speakers)
            if max_speakers:
                diarize_kwargs["max_speakers"] = int(max_speakers)
            diarize_segments = diarize_model(audio, **diarize_kwargs)
            result = whisperx.assign_word_speakers(diarize_segments, result)
            for seg in result["segments"]:
                if "speaker" in seg:
                    speakers.add(seg["speaker"])
            speaker_turns = len([s for s in result["segments"] if "speaker" in s])
            del diarize_model
            gc.collect()
            torch.cuda.empty_cache()
        except Exception as exc:
            print(f"  WARNING: WhisperX diarization failed — {exc}")

    segments_data = []
    for seg in result["segments"]:
        d = {"start": round(seg["start"], 3), "end": round(seg["end"], 3), "text": seg.get("text", "").strip(),
             "no_speech_prob": round(seg.get("no_speech_prob", 0), 4)}
        if "words" in seg:
            d["words"] = [
                {"word": w.get("word", ""), "start": round(w.get("start", 0), 3),
                 "end": round(w.get("end", 0), 3), "probability": round(w.get("score", w.get("probability", 0)), 4)}
                for w in seg["words"]
            ]
        if "speaker" in seg:
            d["speaker"] = seg["speaker"]
        segments_data.append(d)

    no_speech_filtered = _check_no_speech(segments_data)
    if no_speech_filtered:
        segments_data = []

    elapsed = time.perf_counter() - t0
    analysis: dict = {
        "language": language,
        "language_probability": 1.0,
        "pipeline": "whisperx",
        "_no_speech_filtered": no_speech_filtered,
    }
    if speakers:
        analysis["speakers"] = sorted(speakers)
        analysis["speaker_turns"] = speaker_turns
    print(f"WhisperX:    {len(segments_data)} segments, {elapsed:.2f}s (model={model_name}, lang={language})")

    return segments_data, analysis


_WHISPER_SPELL_RULES: dict[str, list[tuple[str, str]]] = {
    "no": [
        (r"\bDet gr\b", "Det går"),
        (r"\bgr det\b", "går det"),
        (r"\bgr bra\b", "går bra"),
        (r"\bgr til\b", "går til"),
        (r"\bgr inn\b", "går inn"),
        (r"\bgr ut\b", "går ut"),
        (r"\bgr fram\b", "går fram"),
        (r"\bgr for\b", "går for"),
        (r"\bgr ned\b", "går ned"),
        (r"\bgr opp\b", "går opp"),
        (r"\bgr videre\b", "går videre"),
        (r"\bgr over\b", "går over"),
        (r"\bgr bra\b", "går bra"),
        (r"\bG[^aå]r\b(?=\s)", "Går"),
        (r"\bgr\s", "går "),
        (r"\bstr\b", "står"),
        (r"\bStr\b", "Står"),
        (r"\bforste\b", "første"),
        (r"\bfrste\b", "første"),
        (r"\bForste\b", "Første"),
        (r"\bF[^o]rst\b", "Først"),
        (r"\bforst\b", "forstår"),
        (r"\bNr\b(?=\s)", "Når"),
        (r"\bN[^aå]r\b(?=\s)", "Når"),
        (r"\bOgs\b", "Også"),
        (r"\bogs\b(?=\s)", "også"),
        (r"\bogs[^aå]\b", "også"),
        (r"\bbr\b(?=\s)", "bør"),
        (r"\bBr\b(?=\s)", "Bør"),
        (r"\bskjnner\b", "skjønner"),
        (r"\bsjnner\b", "sjønner"),
        (r"\bskjnne\b", "skjønne"),
        (r"\bvlge\b", "velge"),
        (r"\bvlger\b", "velger"),
        (r"\bkjope\b", "kjøpe"),
        (r"\bkjper\b", "kjøper"),
        (r"\bfakultettene\b", "fakultetene"),
        (r"\bfakultett\b", "fakultet"),
        (r"\bdepartementent\b", "departementet"),
        (r"\bdepartemente\b", "departementet"),
        (r"\bledergrupp?\b", "ledergruppa"),
        (r"\bledergruppe\b", "ledergruppa"),
        (r"\bstipendialtillinger\b", "stipendiatstillinger"),
        (r"\bstipendialstillinger\b", "stipendiatstillinger"),
        (r"\bbebildningen\b", "bevilgningen"),
        (r"\bbebildning\b", "bevilgning"),
        (r"\bforgravd\b", "forfremja"),
        (r"\boverfordobla\b", "overfordoblet"),
        (r"\buniversitets\s+og\s+høgskolerådet\b", "universitets- og høgskolerådet"),
        (r"\bKunnskapsdepartementet\b", "Kunnskapsdepartementet"),
        (r"\bkunnskapsdepartementet\b", "Kunnskapsdepartementet"),
        (r"\bmm\b", "mm"),
    ],
    "nb": [],
    "nn": [],
    "da": [
        (r"\bforste\b", "første"), (r"\bskjnner\b", "skjønner"),
        (r"\bgr bra\b", "går bra"), (r"\bfr\b", "før"),
        (r"\bogs\b", "også"),
    ],
    "sv": [
        (r"\bforste\b", "första"), (r"\bforsta\b", "första"),
        (r"\bskjnner\b", "skönner"), (r"\bsjnner\b", "sjönner"),
        (r"\bgr\b", "går"), (r"\bstr\b", "står"), (r"\bbr\b", "bör"),
        (r"\bogs\b", "också"), (r"\bOcks\b", "Också"),
        (r"\bfr\b", "före"), (r"\bvre\b", "vära"), (r"\bVre\b", "Våra"),
        (r"\blnge\b", "längre"), (r"\bstt\b", "står"),
    ],
    "de": [
        (r"\bUber\b", "Über"), (r"\bGro\b", "Größe"), (r"\bgross\b", "groß"),
        (r"\bAnderung\b", "Änderung"), (r"\bfur\b", "für"),
        (r"\bW[^i]rde\b", "Würde"), (r"\bOffnung\b", "Öffnung"),
        (r"\bG[^e]r[^a]t\b", "Gerät"),
    ],
    "fr": [
        (r"\bca\b(?=\s+[aeiouy])", "ça"), (r"\btre\b", "très"),
        (r"\bEcole\b", "École"), (r"\belude\b", "élude"),
    ],
    "es": [
        (r"\bpais\b", "país"), (r"\bPais\b", "País"),
        (r"\baccion\b", "acción"), (r"\bAccion\b", "Acción"),
        (r"\bnumero\b", "número"), (r"\bNumero\b", "Número"),
    ],
    "pt": [
        (r"\bnumero\b", "número"), (r"\bNumero\b", "Número"),
        (r"\bpais\b", "país"), (r"\bPais\b", "País"),
        (r"\bacao\b", "ação"), (r"\bAcao\b", "Ação"),
        (r"\brelacao\b", "relação"), (r"\bRelacao\b", "Relação"),
    ],
    "fi": [
        (r"\bAlo\b", "Alo"), (r"\bkylla\b", "kyllyä"),
    ],
}


def _fix_language_mixin(text: str, language: str) -> str:
    import re
    lang_key = language.split("-")[0].lower() if language else ""

    if lang_key in ("no", "nb", "nn"):
        chinese = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]+')
        return chinese.sub("[LANGUAGE FIXED]", text)
    if lang_key in ("da", "sv"):
        chinese = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]+')
        return chinese.sub("[LANGUAGE FIXED]", text)

    return text


def _postprocess_summary(summary: dict, language: str) -> None:
    def _fix_string(s: str) -> str:
        fixed = _fix_language_mixin(s, language)
        if "[LANGUAGE FIXED]" in fixed:
            print("  WARNING: Non-target-language text detected in summary, marking as [LANGUAGE FIXED]")
        return fixed

    for key in list(summary.keys()):
        val = summary[key]
        if isinstance(val, str):
            summary[key] = _fix_string(val)
        elif isinstance(val, list):
            for i, item in enumerate(val):
                if isinstance(item, str):
                    summary[key][i] = _fix_string(item)
                elif isinstance(item, dict):
                    for k2, v2 in item.items():
                        if isinstance(v2, str):
                            item[k2] = _fix_string(v2)
        elif isinstance(val, dict):
            for k2, v2 in val.items():
                if isinstance(v2, str):
                    val[k2] = _fix_string(v2)


def _postprocess_transcript(segments_data: list[dict], language: str) -> int:
    import re

    lang_key = language.split("-")[0].lower() if language else "en"
    rules = _WHISPER_SPELL_RULES.get(lang_key, [])
    if not rules:
        return 0

    compiled = [(re.compile(pat, re.IGNORECASE), repl) for pat, repl in rules]
    fixes = 0

    for seg in segments_data:
        original = seg["text"]
        corrected = original
        for pattern, repl in compiled:
            corrected = pattern.sub(repl, corrected)
        if corrected != original:
            seg["text"] = corrected
            seg["text_original"] = original
            fixes += 1

    return fixes


def _build_transcript_text(segments_data: list[dict]) -> str:
    transcript_parts = []
    current_speaker = None
    for seg in segments_data:
        speaker = seg.get("speaker", "")
        if speaker and speaker != current_speaker:
            transcript_parts.append(f"\n[{speaker}]:")
            current_speaker = speaker
        elif not speaker and current_speaker is not None:
            current_speaker = None
        transcript_parts.append(seg["text"])
    return " ".join(transcript_parts)


def _fmt_time(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def _build_context(analysis: dict, audio_name: str) -> str:
    duration_parts = []
    if analysis.get("_duration_seconds"):
        minutes = int(analysis["_duration_seconds"] // 60)
        seconds = int(analysis["_duration_seconds"] % 60)
        duration_parts.append(f"Duration: {minutes}m {seconds}s")

    context_parts = [
        f"Audio file: {audio_name}",
        f"Detected language: {analysis.get('language', 'unknown')}",
    ] + duration_parts

    if analysis.get("speakers"):
        context_parts.append(f"Speakers: {', '.join(analysis['speakers'])}")

    if analysis.get("emotions"):
        top_emotions = sorted(analysis["emotions"].items(), key=lambda x: -x[1])[:5]
        context_parts.append(f"Top emotions: {', '.join(f'{e} ({c})' for e, c in top_emotions)}")

    if analysis.get("vad"):
        context_parts.append(f"Speech ratio: {analysis['vad']['speech_ratio']:.0%}")

    return "\n".join(context_parts)


def _lang_instruction(language: str) -> str:
    if language in ("no", "nb", "nn", "da", "sv", "is", "fo"):
        return "\nCRITICAL LANGUAGE RULE: The transcript is in a Scandinavian language (Norwegian/Danish/Swedish). You MUST write EVERY SINGLE WORD of your ENTIRE response in that SAME language. This includes ALL fields: topic names, descriptions, key points, decisions, questions, action items, quote contexts, summaries — EVERYTHING. Do NOT use English, Chinese, or any other language. Preserve all special characters (æ, ø, å, ö, ä, ð, þ, etc.) correctly."
    if language in ("de", "nl", "af"):
        return "\nCRITICAL LANGUAGE RULE: The transcript is in a non-English language. You MUST write EVERY SINGLE WORD of your ENTIRE response in that SAME language. This includes ALL fields: topic names, descriptions, key points, decisions, questions, action items, quote contexts, summaries — EVERYTHING. Do NOT use English or any other language."
    if language in ("fr", "es", "pt", "it", "ro", "ca"):
        return "\nCRITICAL LANGUAGE RULE: The transcript is in a non-English language. You MUST write EVERY SINGLE WORD of your ENTIRE response in that SAME language. This includes ALL fields: topic names, descriptions, key points, decisions, questions, action items, quote contexts, summaries — EVERYTHING. Do NOT use English or any other language."
    if language in ("fi", "hu", "et", "pl", "cs", "sk", "sl", "hr", "sr", "bg", "ru", "uk"):
        return "\nCRITICAL LANGUAGE RULE: The transcript is in a non-English language. You MUST write EVERY SINGLE WORD of your ENTIRE response in that SAME language. This includes ALL fields: topic names, descriptions, key points, decisions, questions, action items, quote contexts, summaries — EVERYTHING. Do NOT use English or any other language."
    return ""


_CHUNK_MAX_WORDS = 3000
_CHUNK_MAX_SECONDS = 300


def _chunk_segments(segments_data: list[dict], max_words: int = _CHUNK_MAX_WORDS, max_seconds: float = _CHUNK_MAX_SECONDS) -> list[list[dict]]:
    if not segments_data:
        return []
    chunks: list[list[dict]] = []
    current_chunk: list[dict] = []
    chunk_word_count = 0
    chunk_start = segments_data[0]["start"]

    for seg in segments_data:
        seg_words = len(seg.get("text", "").split())
        would_exceed = (chunk_word_count + seg_words > max_words) or (seg["end"] - chunk_start > max_seconds)
        if would_exceed and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            chunk_word_count = 0
            chunk_start = seg["start"]
        current_chunk.append(seg)
        chunk_word_count += seg_words

    if current_chunk:
        chunks.append(current_chunk)
    return chunks


def _build_chunk_text(chunk_segments: list[dict]) -> str:
    return _build_transcript_text(chunk_segments)


def _merge_topic_lists(chunk_results: list[dict | None]) -> dict | None:
    all_topics: list[dict] = []
    seen_names: set[str] = set()
    for result in chunk_results:
        if result is None or "topics" not in result:
            continue
        for topic in result["topics"]:
            name = topic.get("name", "").strip().lower()
            if name and name not in seen_names:
                seen_names.add(name)
                all_topics.append(topic)
    if not all_topics:
        return None
    return {"topics": all_topics}


def _merge_detail_results(chunk_results: list[dict | None]) -> dict | None:
    all_details: list[dict] = []
    all_decisions: list[str] = []
    overall_sentiment = "neutral"
    confidence = "medium"
    for result in chunk_results:
        if result is None:
            continue
        all_details.extend(result.get("topic_details", []))
        for d in result.get("overall_decisions", []):
            all_decisions.append(d if isinstance(d, str) else json.dumps(d, ensure_ascii=False))
        if result.get("overall_sentiment"):
            overall_sentiment = result["overall_sentiment"]
        if result.get("confidence"):
            confidence = result["confidence"]
    if not all_details:
        return None
    return {
        "topic_details": all_details,
        "overall_decisions": list(dict.fromkeys(all_decisions)),
        "overall_sentiment": overall_sentiment,
        "confidence": confidence,
    }


def _extract_topics(transcript_text: str, context: str, lang_instr: str, model: str, segments_data: list[dict] | None = None) -> dict | None:
    if segments_data and len(segments_data) > 0:
        total_words = sum(len(s.get("text", "").split()) for s in segments_data)
        total_duration = segments_data[-1]["end"] - segments_data[0]["start"]
        if total_words > _CHUNK_MAX_WORDS or total_duration > _CHUNK_MAX_SECONDS:
            chunks = _chunk_segments(segments_data)
            if len(chunks) > 1:
                print(f"  Pass 1: Extracting topics from {len(chunks)} chunks ({total_words} words)...")
                chunk_results = []
                for i, chunk in enumerate(chunks):
                    chunk_text = _build_chunk_text(chunk)
                    chunk_start = _fmt_time(chunk[0]["start"])
                    chunk_end = _fmt_time(chunk[-1]["end"])
                    print(f"    Chunk {i+1}/{len(chunks)} [{chunk_start}-{chunk_end}] ({len(chunk_text.split())} words)...")
                    result = _extract_topics(chunk_text, context, lang_instr, model)
                    chunk_results.append(result)
                merged = _merge_topic_lists(chunk_results)
                if merged:
                    print(f"  Merged {len(merged['topics'])} unique topics from {len(chunks)} chunks")
                    return merged
                return None

    prompt = f"""You are a meticulous conversation analyst. Your ONLY job is to identify EVERY topic discussed in this transcript. Be exhaustive — do not miss any topic, even if mentioned briefly.

{context}

## Transcript
{transcript_text}
{lang_instr}

## Instructions
List EVERY topic discussed in this conversation. For each topic, provide:
- A short descriptive name (2-5 words)
- The approximate start time (MM:SS format) when it was first mentioned
- A one-sentence description of what was discussed

Respond with ONLY valid JSON (no markdown fences, no commentary). Format:
{{
  "topics": [
    {{"name": "Topic name", "start_time": "MM:SS", "description": "What was discussed about this topic"}},
    ...
  ]
}}

Rules:
- List EVERY topic, even if mentioned for just 10-15 seconds
- Order topics chronologically by when they first appear
- Use the same language as the transcript for topic names and descriptions
- Include meta-topics like greetings, small talk, wrap-up if they occurred
- If the conversation jumps between topics and returns, list the first occurrence"""

    print("  Pass 1: Extracting topics...")
    result = _summarize_ollama(prompt, model, num_predict=2048)
    if result and "topics" in result:
        return result
    return None


def _summarize_per_topic(transcript_text: str, context: str, topics: list[dict], lang_instr: str, model: str, segments_data: list[dict] | None = None) -> dict | None:
    topics_block = "\n".join(
        f"  {i+1}. \"{t.get('name', 'Unknown')}\" ({t.get('start_time', '??:??')}) — {t.get('description', '')}"
        for i, t in enumerate(topics)
    )

    if segments_data and len(segments_data) > 0:
        total_words = sum(len(s.get("text", "").split()) for s in segments_data)
        total_duration = segments_data[-1]["end"] - segments_data[0]["start"]
        if total_words > _CHUNK_MAX_WORDS or total_duration > _CHUNK_MAX_SECONDS:
            chunks = _chunk_segments(segments_data)
            if len(chunks) > 1:
                print(f"  Pass 2: Extracting details for {len(topics)} topics across {len(chunks)} chunks...")
                chunk_results = []
                for i, chunk in enumerate(chunks):
                    chunk_text = _build_chunk_text(chunk)
                    chunk_start = _fmt_time(chunk[0]["start"])
                    chunk_end = _fmt_time(chunk[-1]["end"])
                    print(f"    Chunk {i+1}/{len(chunks)} [{chunk_start}-{chunk_end}] ({len(chunk_text.split())} words)...")
                    result = _summarize_per_topic(chunk_text, context, topics, lang_instr, model)
                    chunk_results.append(result)
                merged = _merge_detail_results(chunk_results)
                if merged:
                    print(f"  Merged details for {len(merged['topic_details'])} topic entries from {len(chunks)} chunks")
                    return merged
                return None

    prompt = f"""You are a meticulous meeting analyst. For EACH topic listed below, extract ALL details from the transcript. Do NOT skip any topic.

{context}

## Transcript
{transcript_text}
{lang_instr}

## Topics to analyze (extract details for EACH one):
{topics_block}

## Instructions
For EACH topic listed above, extract detailed information from the transcript. Respond with ONLY valid JSON:
{{
  "topic_details": [
    {{
      "name": "Topic name",
      "start_time": "MM:SS",
      "key_points": ["Every specific point made about this topic — be exhaustive"],
      "decisions": ["Any decisions or agreements related to this topic"],
      "questions": ["Any questions asked about this topic"],
      "action_items": ["Any follow-up tasks or to-do items for this topic"],
      "quotes": [{{"speaker": "Speaker name or null", "text": "Exact quote", "context": "Brief context"}}]
    }},
    ...
  ],
  "overall_decisions": ["Decisions that span multiple topics"],
  "overall_sentiment": "positive/negative/neutral/mixed",
  "confidence": "high/medium/low"
}}

Rules:
- You MUST cover EVERY topic listed above — do not skip any
- Include specific details, numbers, names, and dates mentioned
- For quotes, use the exact words from the transcript
- Use the same language as the transcript for all text fields"""

    print(f"  Pass 2: Extracting details for {len(topics)} topics...")
    return _summarize_ollama(prompt, model, num_predict=8192)


def _synthesize_final(topic_details: list[dict], context: str, lang_instr: str, model: str) -> dict | None:
    details_block = "\n".join(
        f"### {td.get('name', 'Unknown')} ({td.get('start_time', '??:??')})\n"
        f"Key points: {'; '.join(td.get('key_points', []))}\n"
        f"Decisions: {'; '.join(td.get('decisions', []))}\n"
        f"Questions: {'; '.join(td.get('questions', []))}\n"
        f"Actions: {'; '.join(td.get('action_items', []))}\n"
        f"Quotes: {'; '.join(json.dumps(q, ensure_ascii=False) for q in td.get('quotes', []))}"
        for td in topic_details
    )

    prompt = f"""You are an expert editor creating a polished, comprehensive meeting summary from structured notes.

{context}

## Extracted Details by Topic
{details_block}
{lang_instr}

## Instructions
Create a polished, well-organized summary from the extracted details above. Respond with ONLY valid JSON:
{{
  "overview": "A detailed 2-3 paragraph summary that flows naturally, covering all topics in chronological order. Each paragraph should cover a distinct phase of the conversation.",
  "topics": [
    {{
      "name": "Topic name",
      "start_time": "MM:SS",
      "summary": "2-4 sentences summarizing what was discussed about this specific topic",
      "key_points": ["Specific points extracted"],
      "decisions": ["Decisions made"],
      "questions": ["Questions raised"],
      "action_items": ["Follow-up tasks"],
      "quotes": [{{"speaker": "...", "text": "...", "context": "..."}}]
    }}
  ],
  "all_decisions": ["Every decision made across all topics"],
  "all_action_items": ["Every action item with responsible person if mentioned"],
  "all_questions": ["Every question that was asked"],
  "open_issues": ["Topics raised but not resolved"],
  "sentiment": "positive/negative/neutral/mixed",
  "confidence": "high/medium/low"
}}

Rules:
- The overview should read as a coherent narrative, not a list
- Do NOT omit any topic — every topic must appear in both the overview and the topics array
- Preserve speaker names exactly as they appear
- Use the same language as the transcript for ALL text fields"""

    print("  Pass 3: Synthesizing final summary...")
    return _summarize_ollama(prompt, model, num_predict=8192)


def _parse_summary_json(raw: str) -> dict | None:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    open_brackets = text.count("{") - text.count("}")
    open_arrays = text.count("[") - text.count("]")
    if open_brackets > 0 or open_arrays > 0:
        repaired = text.rstrip()
        if not repaired.endswith("}") and not repaired.endswith("]"):
            repaired += "]" * max(0, open_arrays)
            repaired += "}" * max(0, open_brackets)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

    last_brace = text.rfind("}")
    if last_brace > 0:
        try:
            return json.loads(text[:last_brace + 1])
        except json.JSONDecodeError:
            pass

    return None


def _summarize_ollama(prompt: str, model: str, num_predict: int = 4096, use_json_format: bool = True) -> dict | None:
    import urllib.request
    import urllib.error

    url = os.environ.get("OLLAMA_URL", "http://ollama:11434")
    endpoint = f"{url}/api/generate"
    num_ctx = _env_int("OLLAMA_NUM_CTX", 16384)
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": num_predict, "num_ctx": num_ctx},
    }
    if use_json_format:
        payload["format"] = "json"
    payload = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(endpoint, data=payload, headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            raw_bytes = resp.read()
            data = json.loads(raw_bytes.decode("utf-8"))
            raw_text = data.get("response", "")
            eval_count = data.get("eval_count", 0)
            eval_duration = data.get("eval_duration", 0)
            if eval_count > 0 and eval_duration > 0:
                tps = eval_count / (eval_duration / 1e9)
                print(f"  Ollama: {eval_count} tokens, {tps:.1f} tokens/s")
            if eval_count >= num_predict:
                print(f"  WARNING: Response hit num_predict limit ({num_predict} tokens) — output may be truncated")
            _log_memory(f"ollama response ({model})")
            result = _parse_summary_json(raw_text)
            if result is None:
                print(f"  WARNING: JSON parse failed, raw response length={len(raw_text)}")
            return result
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"  WARNING: Ollama request failed — {exc}")
        return None


def _summarize_hf(prompt: str, model: str, segments_data: list[dict] | None = None, analysis: dict | None = None) -> dict | None:
    import torch
    from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

    try:
        t0 = time.perf_counter()
        tokenizer = AutoTokenizer.from_pretrained(model)
        lm_model = AutoModelForSeq2SeqLM.from_pretrained(
            model, torch_dtype=torch.float16,
        )
        lm_model = lm_model.to("cuda")
        lm_model.eval()
        elapsed = time.perf_counter() - t0
        print(f"  HF model loaded in {elapsed:.1f}s")

        transcript_text = " ".join(seg.get("text", "") for seg in (segments_data or []))

        t5_prompt = f"Summarize the following transcript in 2-3 sentences. Then list 3 key points. Transcript: {transcript_text}"
        inputs = tokenizer(t5_prompt, return_tensors="pt", truncation=True, max_length=512).to("cuda")
        with torch.no_grad():
            output_ids = lm_model.generate(
                **inputs,
                max_new_tokens=512,
                temperature=0.3,
                do_sample=True,
            )
        raw_text = tokenizer.decode(output_ids[0], skip_special_tokens=True).strip()

        key_points = []
        summary_text = raw_text
        lines = raw_text.split("\n")
        if len(lines) > 1:
            summary_text = lines[0].strip()
            for line in lines[1:]:
                line = line.strip().lstrip("0123456789.-) ")
                if line:
                    key_points.append(line)

        if not key_points:
            key_points = [raw_text]

        topics = []
        if analysis and analysis.get("audio_tags"):
            topics = [t["label"] for t in analysis["audio_tags"][:5]]

        result = {
            "summary": summary_text,
            "key_points": key_points,
            "topics": topics or ["general"],
            "action_items": [],
            "notable_quotes": [],
            "sentiment": analysis.get("emotion_top", "neutral") if analysis else "neutral",
            "confidence": "low",
        }

        del lm_model
        del tokenizer
        torch.cuda.empty_cache()
        gc.collect()
        return result
    except Exception as exc:
        print(f"  WARNING: HF summarization failed — {exc}")
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass
        gc.collect()
        return None


def _free_gpu_memory() -> None:
    import torch
    try:
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
            gc.collect()
            torch.cuda.empty_cache()
    except Exception:
        pass


_audio_cache: dict[str, str] = {}


def _ensure_wav(audio_path: str) -> str:
    if audio_path.endswith(".wav"):
        return audio_path
    if audio_path in _audio_cache:
        return _audio_cache[audio_path]
    import subprocess, tempfile
    tmp = tempfile.mktemp(suffix=".wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", tmp],
        capture_output=True, timeout=300
    )
    _audio_cache[audio_path] = tmp
    return tmp


def _unload_ollama_model(model: str) -> None:
    import urllib.request
    import urllib.error

    url = os.environ.get("OLLAMA_URL", "http://ollama:11434")
    try:
        payload = json.dumps({"model": model, "keep_alive": "0"}).encode("utf-8")
        req = urllib.request.Request(
            f"{url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            reason = data.get("done_reason", "unknown")
            print(f"  Unloaded Ollama model: {model} (reason={reason})")
    except Exception as exc:
        pass


def stage_summarize(segments_data: list[dict], analysis: dict, output_dir: str, audio_name: str) -> None:
    if not segments_data:
        print("  Summarize:   SKIPPED (no segments)")
        return
    backend = os.environ.get("SUMMARY_BACKEND", "ollama")
    compare_mode = os.environ.get("SUMMARY_COMPARE", "")
    ollama_model = os.environ.get("SUMMARY_OLLAMA_MODEL", "llama3.1:8b")
    hf_model = os.environ.get("SUMMARY_HF_MODEL", "google/flan-t5-large")

    print(f"  Freeing GPU memory before summarization...")
    _free_gpu_memory()
    _log_memory("pre-summarize (GPU freed)")

    duration = segments_data[-1]["end"] if segments_data else 0
    analysis["_duration_seconds"] = duration
    transcript_text = _build_transcript_text(segments_data)
    context = _build_context(analysis, audio_name)
    lang_instr = _lang_instruction(analysis.get("language", ""))

    results: dict[str, dict] = {}

    if backend in ("ollama", "auto") or not backend:
        t0 = time.perf_counter()

        pass1_result = _extract_topics(transcript_text, context, lang_instr, ollama_model, segments_data)
        if pass1_result is None and ollama_model == "qwen2.5:14b":
            print("  WARNING: Pass 1 failed with 14b, trying 7b fallback...")
            _free_gpu_memory()
            pass1_result = _extract_topics(transcript_text, context, lang_instr, "qwen2.5:7b", segments_data)
            if pass1_result is not None:
                ollama_model = "qwen2.5:7b"

        if pass1_result and "topics" in pass1_result:
            topics = pass1_result["topics"]
            print(f"  Found {len(topics)} topics")

            pass2_result = _summarize_per_topic(transcript_text, context, topics, lang_instr, ollama_model, segments_data)
            if pass2_result is None and ollama_model == "qwen2.5:14b":
                print("  WARNING: Pass 2 failed with 14b, trying 7b fallback...")
                _free_gpu_memory()
                pass2_result = _summarize_per_topic(transcript_text, context, topics, lang_instr, "qwen2.5:7b", segments_data)
                if pass2_result is not None:
                    ollama_model = "qwen2.5:7b"

            if pass2_result and "topic_details" in pass2_result:
                topic_details = pass2_result["topic_details"]
                num_topics = len(topic_details)

                pass3_result = None
                skip_pass3 = os.environ.get("SUMMARY_SKIP_SYNTHESIS", "true").lower() in ("true", "1", "yes")

                if not skip_pass3 and num_topics <= 8 and len(transcript_text) < 10000:
                    pass3_result = _synthesize_final(topic_details, context, lang_instr, ollama_model)
                    if pass3_result is None and ollama_model == "qwen2.5:14b":
                        _free_gpu_memory()
                        pass3_result = _synthesize_final(topic_details, context, lang_instr, "qwen2.5:7b")
                        if pass3_result is not None:
                            ollama_model = "qwen2.5:7b"

                if pass3_result:
                    final = pass3_result
                    passes_used = "3-pass (enumerate → extract → synthesize)"
                else:
                    if skip_pass3:
                        print(f"  Skipping Pass 3 (SUMMARY_SKIP_SYNTHESIS={skip_pass3})")
                    first_kp = topic_details[0].get("key_points", []) if topic_details else []
                    overview_text = first_kp[0] if first_kp else "No details available."
                    final = {
                        "overview": overview_text,
                        "topics": [],
                        "all_decisions": pass2_result.get("overall_decisions", []),
                        "all_action_items": [],
                        "all_questions": [],
                        "open_issues": [],
                        "sentiment": pass2_result.get("overall_sentiment", "unknown"),
                        "confidence": pass2_result.get("confidence", "medium"),
                    }
                    for td in topic_details:
                        final["topics"].append({
                            "name": td.get("name", "Unknown"),
                            "start_time": td.get("start_time", ""),
                            "summary": "; ".join(td.get("key_points", [])),
                            "key_points": td.get("key_points", []),
                            "decisions": td.get("decisions", []),
                            "questions": td.get("questions", []),
                            "action_items": td.get("action_items", []),
                            "quotes": td.get("quotes", []),
                        })
                    passes_used = "2-pass (enumerate → extract)"
                    final["_backend"] = "ollama"
                    final["_model"] = ollama_model
                    final["_duration"] = round(time.perf_counter() - t0, 2)
                    final["_topics_found"] = num_topics
                    final["_passes"] = passes_used
                    results["ollama"] = final
                    print(f"  Summarize ({passes_used}): done in {final['_duration']:.1f}s ({num_topics} topics)")
        else:
            print("  WARNING: Pass 1 topic extraction failed, falling back to single-pass")
            prompt = f"""You are a meticulous meeting analyst. Extract EVERY topic, decision, question, and action item from the transcript.

{context}

## Transcript
{transcript_text}
{lang_instr}

## Instructions
Produce a JSON response with ONLY valid JSON (no markdown fences):
{{
  "overview": "Detailed multi-paragraph summary covering ALL topics discussed",
  "topics": [{{"name": "Topic name", "start_time": "MM:SS", "summary": "What was discussed", "key_points": ["Points"], "decisions": [], "questions": [], "action_items": [], "quotes": []}}],
  "all_decisions": ["Every decision"],
  "all_action_items": ["Every action item"],
  "all_questions": ["Every question"],
  "open_issues": ["Unresolved topics"],
  "sentiment": "positive/negative/neutral/mixed",
  "confidence": "high/medium/low"
}}

Be exhaustive — list EVERY topic discussed, even brief mentions."""

            single_result = _summarize_ollama(prompt, ollama_model)
            if single_result is None and ollama_model == "qwen2.5:14b":
                _free_gpu_memory()
                single_result = _summarize_ollama(prompt, "qwen2.5:7b")
                if single_result is not None:
                    ollama_model = "qwen2.5:7b"
            if single_result:
                single_result["_backend"] = "ollama"
                single_result["_model"] = ollama_model
                single_result["_duration"] = round(time.perf_counter() - t0, 2)
                single_result["_topics_found"] = len(single_result.get("topics", []))
                single_result["_passes"] = "1-pass (fallback)"
                results["ollama"] = single_result

    elif backend == "hf":
        t0 = time.perf_counter()
        prompt = f"Summarize the following transcript: {transcript_text}"
        result = _summarize_hf(prompt, hf_model, segments_data, analysis)
        if result:
            result["_backend"] = "hf"
            result["_model"] = hf_model
            result["_duration"] = round(time.perf_counter() - t0, 2)
            result["_passes"] = "1-pass (HF)"
            results["hf"] = result

    if not results:
        print("WARNING: All summarization backends failed")
        return

    detected_lang = analysis.get("language", "")
    for bname, bresult in results.items():
        _postprocess_summary(bresult, detected_lang)

    analysis["summary"] = results

    if compare_mode == "all" and len(results) > 1:
        for bname, bresult in results.items():
            suffix = f"_{bname}"
            _write_summary_outputs(bresult, output_dir, suffix)
    else:
        first_result = next(iter(results.values()))
        _write_summary_outputs(first_result, output_dir, "")


def _write_summary_outputs(summary: dict, output_dir: str, suffix: str) -> None:
    backend = summary.get("_backend", "unknown")
    model = summary.get("_model", "unknown")
    duration = summary.get("_duration", 0)
    topics_found = summary.get("_topics_found", 0)
    passes = summary.get("_passes", "unknown")

    def _p(name: str) -> str:
        base, ext = os.path.splitext(name)
        return os.path.join(output_dir, f"{base}{suffix}{ext}")

    path = _p("summary.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(summary.get("overview", "No summary available.") + "\n\n")

        topics = summary.get("topics", [])
        if topics:
            f.write("TOPICS DISCUSSED\n")
            f.write("=" * 60 + "\n\n")
            for i, topic in enumerate(topics, 1):
                if isinstance(topic, dict):
                    name = topic.get("name", f"Topic {i}")
                    start = topic.get("start_time", "")
                    f.write(f"{i}. {name}")
                    if start:
                        f.write(f" ({start})")
                    f.write("\n")
                    if topic.get("summary"):
                        f.write(f"   {topic['summary']}\n")
                    if topic.get("key_points"):
                        for pt in topic["key_points"]:
                            f.write(f"   - {pt}\n")
                    f.write("\n")

        all_decisions = summary.get("all_decisions", [])
        if all_decisions:
            f.write("DECISIONS\n")
            f.write("-" * 60 + "\n")
            for i, d in enumerate(all_decisions, 1):
                f.write(f"  {i}. {d}\n")
            f.write("\n")

        all_questions = summary.get("all_questions", [])
        if all_questions:
            f.write("QUESTIONS\n")
            f.write("-" * 60 + "\n")
            for i, q in enumerate(all_questions, 1):
                f.write(f"  {i}. {q}\n")
            f.write("\n")

        all_actions = summary.get("all_action_items", [])
        if all_actions:
            f.write("ACTION ITEMS\n")
            f.write("-" * 60 + "\n")
            for i, item in enumerate(all_actions, 1):
                f.write(f"  {i}. {item}\n")
            f.write("\n")

        open_issues = summary.get("open_issues", [])
        if open_issues:
            f.write("OPEN ISSUES\n")
            f.write("-" * 60 + "\n")
            for i, issue in enumerate(open_issues, 1):
                f.write(f"  {i}. {issue}\n")
            f.write("\n")

        f.write(f"Sentiment: {summary.get('sentiment', 'N/A')}\n")
        f.write(f"Confidence: {summary.get('confidence', 'N/A')}\n")
        f.write(f"Topics found: {topics_found}\n")
        f.write(f"Backend: {backend} ({model}) — {duration:.1f}s ({passes})\n")
    print(f"Wrote:       {path}")

    path = _p("summary.json")
    clean = {k: v for k, v in summary.items() if not k.startswith("_")}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2, ensure_ascii=False)
    print(f"Wrote:       {path}")

    path = _p("summary.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Summary\n\n")
        f.write(f"**Model:** {backend} ({model}) | **Sentiment:** {summary.get('sentiment', 'N/A')} | **Confidence:** {summary.get('confidence', 'N/A')} | **Topics:** {topics_found}\n\n")

        f.write("## Overview\n\n")
        f.write(summary.get("overview", "No summary available.") + "\n\n")

        topics = summary.get("topics", [])
        if topics:
            f.write("## Topics Discussed\n\n")
            for topic in topics:
                if isinstance(topic, dict):
                    name = topic.get("name", "Unknown")
                    start = topic.get("start_time", "")
                    f.write(f"### {name}")
                    if start:
                        f.write(f" ({start})")
                    f.write("\n\n")
                    if topic.get("summary"):
                        f.write(f"{topic['summary']}\n\n")
                    if topic.get("key_points"):
                        f.write("**Key Points:**\n")
                        for pt in topic["key_points"]:
                            f.write(f"- {pt}\n")
                        f.write("\n")
                    if topic.get("decisions"):
                        f.write("**Decisions:**\n")
                        for d in topic["decisions"]:
                            f.write(f"- {d}\n")
                        f.write("\n")
                    if topic.get("questions"):
                        f.write("**Questions:**\n")
                        for q in topic["questions"]:
                            f.write(f"- {q}\n")
                        f.write("\n")
                    if topic.get("action_items"):
                        f.write("**Action Items:**\n")
                        for item in topic["action_items"]:
                            f.write(f"- [ ] {item}\n")
                        f.write("\n")
                    if topic.get("quotes"):
                        f.write("**Quotes:**\n")
                        for quote in topic["quotes"]:
                            speaker = quote.get("speaker", "Unknown")
                            f.write(f"> \"{quote.get('text', '')}\"\n")
                            f.write(f"> — {speaker}")
                            if quote.get("context"):
                                f.write(f" ({quote['context']})")
                            f.write("\n\n")

        all_decisions = summary.get("all_decisions", [])
        if all_decisions:
            f.write("## All Decisions\n\n")
            for d in all_decisions:
                f.write(f"- {d}\n")
            f.write("\n")

        all_questions = summary.get("all_questions", [])
        if all_questions:
            f.write("## All Questions\n\n")
            for q in all_questions:
                f.write(f"- {q}\n")
            f.write("\n")

        all_actions = summary.get("all_action_items", [])
        if all_actions:
            f.write("## Action Items\n\n")
            for item in all_actions:
                f.write(f"- [ ] {item}\n")
            f.write("\n")

        open_issues = summary.get("open_issues", [])
        if open_issues:
            f.write("## Open Issues\n\n")
            for issue in open_issues:
                f.write(f"- {issue}\n")
            f.write("\n")
    print(f"Wrote:       {path}")


def _fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds % 1) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_ts_vtt(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds % 1) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def write_transcript_txt(segments_data: list[dict], output_dir: str) -> None:
    path = os.path.join(output_dir, "transcript.txt")
    with open(path, "w", encoding="utf-8") as f:
        for seg in segments_data:
            f.write(seg["text"] + "\n")
    print(f"Wrote:       {path}")


def write_segments_json(segments_data: list[dict], analysis: dict, output_dir: str) -> None:
    path = os.path.join(output_dir, "segments.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(segments_data, f, indent=2, ensure_ascii=False)
    print(f"Wrote:       {path}")

    if analysis:
        path = os.path.join(output_dir, "analysis.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False)
        print(f"Wrote:       {path}")


def write_full_json(segments_data: list[dict], analysis: dict, audio_name: str, output_dir: str) -> None:
    full = {
        "file": audio_name,
        "analysis": analysis,
        "segments": segments_data,
    }
    path = os.path.join(output_dir, "full.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(full, f, indent=2, ensure_ascii=False)
    print(f"Wrote:       {path}")


def write_srt(segments_data: list[dict], output_dir: str) -> None:
    path = os.path.join(output_dir, "transcript.srt")
    with open(path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments_data, 1):
            f.write(f"{i}\n")
            f.write(f"{_fmt_ts(seg['start'])} --> {_fmt_ts(seg['end'])}\n")
            speaker = seg.get("speaker", "")
            if speaker:
                f.write(f"[{speaker}] {seg['text']}\n")
            else:
                f.write(f"{seg['text']}\n")
            f.write("\n")
    print(f"Wrote:       {path}")


def write_vtt(segments_data: list[dict], output_dir: str) -> None:
    path = os.path.join(output_dir, "transcript.vtt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for i, seg in enumerate(segments_data, 1):
            speaker = seg.get("speaker", "")
            if speaker:
                f.write(f"<v {speaker}>\n")
            f.write(f"{_fmt_ts_vtt(seg['start'])} --> {_fmt_ts_vtt(seg['end'])}\n")
            f.write(f"{seg['text']}\n")
            if speaker:
                f.write(f"</v>\n")
            f.write("\n")
    print(f"Wrote:       {path}")


def write_segments_csv(segments_data: list[dict], output_dir: str) -> None:
    path = os.path.join(output_dir, "segments.csv")
    fieldnames = ["start", "end", "text", "speaker", "emotion", "speech_ratio"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for seg in segments_data:
            row = {
                "start": seg["start"],
                "end": seg["end"],
                "text": seg["text"],
                "speaker": seg.get("speaker", ""),
                "emotion": seg.get("emotion", {}).get("label", ""),
                "speech_ratio": seg.get("speech_ratio", ""),
            }
            writer.writerow(row)
    print(f"Wrote:       {path}")


def write_words_csv(segments_data: list[dict], output_dir: str) -> None:
    path = os.path.join(output_dir, "words.csv")
    fieldnames = ["segment_index", "start", "end", "word", "probability", "speaker"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, seg in enumerate(segments_data):
            speaker = seg.get("speaker", "")
            for w in seg.get("words", []):
                writer.writerow({
                    "segment_index": i,
                    "start": w["start"],
                    "end": w["end"],
                    "word": w["word"],
                    "probability": w.get("probability", 0.0),
                    "speaker": speaker,
                })
    print(f"Wrote:       {path}")


def write_transcript_md(segments_data: list[dict], output_dir: str) -> None:
    path = os.path.join(output_dir, "transcript.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Transcript\n\n")
        current_speaker = None
        for seg in segments_data:
            speaker = seg.get("speaker", "")
            ts = f"[{_fmt_ts(seg['start'])} --> {_fmt_ts(seg['end'])}]"
            if speaker and speaker != current_speaker:
                f.write(f"\n## {speaker}\n\n")
                current_speaker = speaker
            elif not speaker and current_speaker is not None:
                current_speaker = None
            emotion = seg.get("emotion", {})
            emotion_str = f" *({emotion.get('label', '')})*" if emotion and emotion.get("score", 0) > 0.3 else ""
            f.write(f"- {ts} {seg['text']}{emotion_str}\n")
    print(f"Wrote:       {path}")


def write_report_md(segments_data: list[dict], analysis: dict, audio_name: str, output_dir: str) -> None:
    path = os.path.join(output_dir, "report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Analysis Report: {audio_name}\n\n")

        f.write("## Overview\n\n")
        f.write("| Property | Value |\n|---|---|\n")
        f.write(f"| File | `{audio_name}` |\n")
        f.write(f"| Language | {analysis.get('language', 'N/A')} ({analysis.get('language_probability', 0):.0%}) |\n")
        if "language_id" in analysis:
            lid = analysis["language_id"]
            f.write(f"| Language ID | {lid.get('label', 'N/A')} ({lid.get('score', 0):.0%}) |\n")
        duration = segments_data[-1]["end"] if segments_data else 0
        f.write(f"| Duration | {duration:.1f}s |\n")
        f.write(f"| Segments | {len(segments_data)} |\n")
        f.write(f"| Pipeline Duration | {analysis.get('pipeline_duration', 0):.2f}s |\n\n")

        if "speakers" in analysis:
            f.write(f"## Speaker Diarization\n\n")
            f.write(f"- **Speakers detected:** {', '.join(analysis['speakers'])}\n")
            f.write(f"- **Speaker turns:** {analysis.get('speaker_turns', 0)}\n\n")
            f.write("### Speaker Timeline\n\n")
            f.write("| Speaker | Segments |\n|---|---|\n")
            speaker_counts: dict[str, int] = {}
            for seg in segments_data:
                spk = seg.get("speaker", "Unknown")
                speaker_counts[spk] = speaker_counts.get(spk, 0) + 1
            for spk, count in sorted(speaker_counts.items()):
                f.write(f"| {spk} | {count} |\n")
            f.write("\n")

        if "vad" in analysis:
            vad = analysis["vad"]
            f.write("## Voice Activity Detection\n\n")
            f.write(f"- **Speech ratio:** {vad['speech_ratio']:.0%}\n")
            f.write(f"- **Speech segments:** {vad['speech_segments']}\n")
            f.write(f"- **Total speech:** {vad['total_speech_seconds']:.1f}s\n\n")

        if "emotions" in analysis:
            f.write("## Emotion Detection\n\n")
            f.write("| Emotion | Count |\n|---|---|\n")
            for emo, count in sorted(analysis["emotions"].items(), key=lambda x: -x[1]):
                f.write(f"| {emo} | {count} |\n")
            f.write("\n")

        if "audio_tags" in analysis:
            f.write("## Audio Classification\n\n")
            f.write("| Tag | Confidence |\n|---|---|\n")
            for tag in analysis["audio_tags"]:
                f.write(f"| {tag['label']} | {tag['score']:.0%} |\n")
            f.write("\n")

        if "summary" in analysis:
            f.write("## AI Summary\n\n")
            for backend_name, sdata in analysis["summary"].items():
                model_info = sdata.get("_model", "unknown")
                topics_found = sdata.get("_topics_found", 0)
                passes = sdata.get("_passes", "")
                f.write(f"### {backend_name} ({model_info}) — {topics_found} topics, {passes}\n\n")
                f.write(f"{sdata.get('overview', 'No summary available.')}\n\n")
                if sdata.get("sentiment"):
                    f.write(f"**Sentiment:** {sdata['sentiment']} | **Confidence:** {sdata.get('confidence', 'N/A')}\n\n")
                topics = sdata.get("topics", [])
                if topics:
                    f.write("**Topics:**\n")
                    for topic in topics:
                        if isinstance(topic, dict):
                            name = topic.get("name", "Unknown")
                            start = topic.get("start_time", "")
                            f.write(f"- {name}")
                            if start:
                                f.write(f" ({start})")
                            f.write("\n")
                            if topic.get("key_points"):
                                for pt in topic["key_points"][:3]:
                                    f.write(f"  - {pt}\n")
                    f.write("\n")
                for field_name, field_label in [
                    ("all_decisions", "Decisions"),
                    ("all_questions", "Questions"),
                    ("all_action_items", "Action Items"),
                    ("open_issues", "Open Issues"),
                ]:
                    items = sdata.get(field_name, [])
                    if items:
                        f.write(f"**{field_label}:**\n")
                        for item in items:
                            f.write(f"- {item}\n")
                        f.write("\n")

        f.write("## Full Transcript\n\n")
        current_speaker = None
        for seg in segments_data:
            speaker = seg.get("speaker", "")
            ts = f"[{_fmt_ts(seg['start'])}]"
            if speaker and speaker != current_speaker:
                f.write(f"\n### {speaker}\n\n")
                current_speaker = speaker
            elif not speaker and current_speaker is not None:
                current_speaker = None
            emotion = seg.get("emotion", {})
            emotion_str = f" *({emotion.get('label', '')})*" if emotion and emotion.get("score", 0) > 0.3 else ""
            f.write(f"{ts} {seg['text']}{emotion_str}\n\n")

    print(f"Wrote:       {path}")


def write_all_outputs(
    segments_data: list[dict],
    analysis: dict,
    audio_name: str,
    output_dir: str,
) -> None:
    os.makedirs(output_dir, exist_ok=True)
    write_transcript_txt(segments_data, output_dir)
    write_segments_json(segments_data, analysis, output_dir)
    write_full_json(segments_data, analysis, audio_name, output_dir)
    write_srt(segments_data, output_dir)
    write_vtt(segments_data, output_dir)
    write_segments_csv(segments_data, output_dir)
    write_words_csv(segments_data, output_dir)
    write_transcript_md(segments_data, output_dir)
    write_report_md(segments_data, analysis, audio_name, output_dir)


def main() -> None:
    audio_path = find_audio_file(INPUT_DIR)
    if audio_path is None:
        print(f"ERROR: No audio file found in {INPUT_DIR}/")
        sys.exit(1)

    audio_name = os.path.basename(audio_path)
    print(f"Input file:  {audio_name}")

    flags = get_analysis_flags()
    print(f"Analysis:    {', '.join(sorted(flags)) if flags else 'transcription only'}")

    pipeline = os.environ.get("PIPELINE", "default")
    align_mode = os.environ.get("ALIGN_MODE", "none")
    if pipeline != "default":
        print(f"Pipeline:    {pipeline}")
    if align_mode != "none":
        print(f"Alignment:   {align_mode}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    analysis: dict = {}
    total_t0 = time.perf_counter()

    if pipeline == "whisperx":
        segments_data, whisper_analysis = stage_transcribe_whisperx(audio_path)
    else:
        segments_data, whisper_analysis = stage_transcribe(audio_path)
    analysis.update(whisper_analysis)

    if os.environ.get("WHISPER_LANGUAGE", "") not in ("", "en"):
        _validate_language_nordic(analysis)

    spell_fixes = _postprocess_transcript(segments_data, analysis.get("language", ""))
    if spell_fixes > 0:
        print(f"Spell fix:   {spell_fixes} segments corrected")

    if pipeline == "default" and align_mode == "whisperx":
        print("Running WhisperX alignment...")
        try:
            stage_align_whisperx(audio_path, segments_data, analysis)
        except Exception as exc:
            print(f"WARNING: WhisperX alignment failed — {exc}")

    if pipeline != "whisperx":
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
        try:
            stage_emotion(audio_path, segments_data, analysis)
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

    skip_summarize = os.environ.get("SKIP_SUMMARIZE", "").lower() in ("true", "1", "yes")
    if "summarize" in flags and not skip_summarize:
        print("Running summarization...")
        try:
            stage_summarize(segments_data, analysis, OUTPUT_DIR, audio_name)
        except Exception as exc:
            print(f"WARNING: Summarization failed — {exc}")
    elif "summarize" in flags and skip_summarize:
        print("Summarize:   SKIPPED (will run in separate container)")

    total_elapsed = time.perf_counter() - total_t0
    analysis["pipeline_duration"] = round(total_elapsed, 2)

    write_all_outputs(segments_data, analysis, audio_name, OUTPUT_DIR)

    print(f"Total:       {total_elapsed:.2f}s")
    print(f"Output:      {OUTPUT_DIR}")
    print(f"DONE.", flush=True)


def _load_existing_output(output_dir: str, audio_name: str) -> tuple[list[dict], dict]:
    full_path = os.path.join(output_dir, "full.json")
    if not os.path.exists(full_path):
        return None, None
    with open(full_path, encoding="utf-8") as f:
        full = json.load(f)
    segments_data = full.get("segments", [])
    analysis = full.get("analysis", {})
    print(f"Loaded:      {full_path} ({len(segments_data)} segments)")
    return segments_data, analysis


def _process_single(audio_path: str, output_dir: str, flags: set[str]) -> dict:
    audio_name = os.path.basename(audio_path)
    print(f"\n{'='*60}")
    print(f"  {audio_name}")
    print(f"{'='*60}")

    pipeline = os.environ.get("PIPELINE", "default")
    align_mode = os.environ.get("ALIGN_MODE", "none")

    os.makedirs(output_dir, exist_ok=True)
    _log_memory(f"start {audio_name}")
    analysis: dict = {}
    timing: dict = {
        "strategy": os.environ.get("STRATEGY", ""),
        "whisper_model": os.environ.get("WHISPER_MODEL", "turbo"),
        "pipeline": pipeline,
        "align_mode": align_mode,
        "analysis": sorted(flags),
        "summarizer": os.environ.get("SUMMARY_OLLAMA_MODEL", ""),
        "stages": {},
    }
    t0 = time.perf_counter()
    skip_transcribe = os.environ.get("SKIP_TRANSCRIBE", "").lower() in ("true", "1", "yes")

    if skip_transcribe:
        segments_data, existing_analysis = _load_existing_output(output_dir, audio_name)
        if segments_data is None:
            print("ERROR: SKIP_TRANSCRIBE set but no existing full.json found")
            return {}
        analysis.update(existing_analysis)
        timing["stages"]["transcribe"] = 0.0
        timing["spell_fixes"] = 0
    elif pipeline == "whisperx":
        segments_data, whisper_analysis = stage_transcribe_whisperx(audio_path)
        timing["stages"]["transcribe_whisperx"] = round(time.perf_counter() - t0, 2)
        analysis.update(whisper_analysis)
        _log_memory("after transcribe")

        spell_fixes = _postprocess_transcript(segments_data, analysis.get("language", ""))
        if spell_fixes > 0:
            print(f"Spell fix:   {spell_fixes} segments corrected")
        timing["spell_fixes"] = spell_fixes
    else:
        segments_data, whisper_analysis = stage_transcribe(audio_path)
        timing["stages"]["transcribe"] = round(time.perf_counter() - t0, 2)
        analysis.update(whisper_analysis)
        _log_memory("after transcribe")

        spell_fixes = _postprocess_transcript(segments_data, analysis.get("language", ""))
        if spell_fixes > 0:
            print(f"Spell fix:   {spell_fixes} segments corrected")
        timing["spell_fixes"] = spell_fixes

    if pipeline == "default" and align_mode == "whisperx":
        print("Running WhisperX alignment...", flush=True)
        try:
            t_stage = time.perf_counter()
            stage_align_whisperx(audio_path, segments_data, analysis)
            timing["stages"]["align_whisperx"] = round(time.perf_counter() - t_stage, 2)
        except Exception as exc:
            print(f"WARNING: WhisperX alignment failed — {exc}")

    skip_analysis = os.environ.get("SKIP_ANALYSIS", "").lower() in ("true", "1", "yes")

    if not skip_analysis:
        if pipeline != "whisperx":
            if "diarize" in flags:
                print("Running diarization...", flush=True)
                try:
                    t_stage = time.perf_counter()
                    stage_diarize(audio_path, segments_data, analysis)
                    timing["stages"]["diarize"] = round(time.perf_counter() - t_stage, 2)
                except Exception as exc:
                    print(f"WARNING: Diarization failed — {exc}")

            if "vad" in flags:
                print("Running VAD...", flush=True)
                try:
                    t_stage = time.perf_counter()
                    stage_vad(audio_path, segments_data, analysis)
                    timing["stages"]["vad"] = round(time.perf_counter() - t_stage, 2)
                except Exception as exc:
                    print(f"WARNING: VAD failed — {exc}")

        if "emotion" in flags:
            print("Running emotion detection...", flush=True)
            try:
                t_stage = time.perf_counter()
                stage_emotion(audio_path, segments_data, analysis)
                timing["stages"]["emotion"] = round(time.perf_counter() - t_stage, 2)
            except Exception as exc:
                print(f"WARNING: Emotion detection failed — {exc}")

        if "classify" in flags:
            print("Running audio classification...", flush=True)
            try:
                t_stage = time.perf_counter()
                stage_classify(audio_path, analysis)
                timing["stages"]["classify"] = round(time.perf_counter() - t_stage, 2)
            except Exception as exc:
                print(f"WARNING: Audio classification failed — {exc}")

        if "language_id" in flags:
            print("Running language identification...", flush=True)
            try:
                t_stage = time.perf_counter()
                stage_language_id(audio_path, analysis)
                timing["stages"]["language_id"] = round(time.perf_counter() - t_stage, 2)
            except Exception as exc:
                print(f"WARNING: Language identification failed — {exc}")

        skip_summarize = os.environ.get("SKIP_SUMMARIZE", "").lower() in ("true", "1", "yes")
        if "summarize" in flags and not skip_summarize:
            print("Running summarization...", flush=True)
            try:
                t_stage = time.perf_counter()
                stage_summarize(segments_data, analysis, output_dir, audio_name)
                timing["stages"]["summarize"] = round(time.perf_counter() - t_stage, 2)
            except Exception as exc:
                print(f"WARNING: Summarization failed — {exc}")
        elif "summarize" in flags and skip_summarize:
            print("Summarize:   SKIPPED (will run in separate container)", flush=True)

    _log_memory(f"end {audio_name}")
    elapsed = time.perf_counter() - t0
    analysis["pipeline_duration"] = round(elapsed, 2)

    timing["total"] = round(elapsed, 2)
    timing["segments"] = len(segments_data)
    timing["language"] = analysis.get("language", "")
    timing["language_probability"] = analysis.get("language_probability", 0)

    summary_data = analysis.get("summary", {})
    if isinstance(summary_data, dict):
        for bname, bresult in summary_data.items():
            if isinstance(bresult, dict):
                timing["topics_found"] = bresult.get("_topics_found", 0)
                timing["summarizer_model"] = bresult.get("_model", "")
                timing["summarizer_passes"] = bresult.get("_passes", "")
                break

    timing_path = os.path.join(output_dir, "timing.json")
    with open(timing_path, "w", encoding="utf-8") as f:
        json.dump(timing, f, indent=2)
    print(f"Wrote:       {timing_path}")

    write_all_outputs(segments_data, analysis, audio_name, output_dir)

    print(f"Total:       {elapsed:.2f}s")
    _log_memory(f"end {audio_name}")
    sys.stdout.flush()
    return analysis


def process_batch(batch_path: str) -> None:
    _log_memory("batch start")
    with open(batch_path) as f:
        jobs = json.load(f)

    if not jobs:
        print("ERROR: No files in batch list")
        sys.exit(1)

    flags = get_analysis_flags()
    print(f"=== Batch mode: {len(jobs)} files ===")
    print(f"Analysis: {', '.join(sorted(flags)) if flags else 'transcription only'}")
    batch_t0 = time.perf_counter()

    for i, job in enumerate(jobs, 1):
        audio_path = job["input"]
        output_dir = job["output"]
        print(f"\n[{i}/{len(jobs)}]", end="", flush=True)
        try:
            _process_single(audio_path, output_dir, flags)
        except Exception:
            print(f"\n  ERROR processing {job.get('name', audio_path)}:", flush=True)
            traceback.print_exc()
            sys.stdout.flush()

        skip_summarize = os.environ.get("SKIP_SUMMARIZE", "").lower() in ("true", "1", "yes")
        if "summarize" in flags and not skip_summarize:
            _unload_ollama_model(os.environ.get("SUMMARY_OLLAMA_MODEL", "llama3.1:8b"))
            _free_gpu_memory()

    total = time.perf_counter() - batch_t0
    _log_memory("batch end")
    print(f"\n{'='*60}")
    print(f"  Batch complete: {len(jobs)} files in {total:.1f}s")
    print(f"{'='*60}")


def _load_whisper():
    from faster_whisper import WhisperModel
    model_name = os.environ.get("WHISPER_MODEL", "turbo")
    vram_key = f"whisper_{model_name}" if model_name in ("turbo", "large", "distil-large-v3") else "whisper_turbo"
    if vram_key == "whisper_distil-large-v3":
        vram_key = "whisper_distil"
    needed = _VRAM_BUDGET.get(vram_key, 2500)
    if not _check_vram(needed, f"Whisper ({model_name})"):
        return None, model_name
    print(f"  Loading Whisper model: {model_name}...", flush=True)
    model = WhisperModel(model_name, device="cuda", compute_type="float16")
    _log_memory("whisper loaded")
    return model, model_name


NO_SPEECH_PROB_THRESHOLD = 0.7


def _check_no_speech(segments_data):
    if not segments_data:
        return True
    avg_prob = sum(s.get("no_speech_prob", 0) for s in segments_data) / len(segments_data)
    all_high = all(s.get("no_speech_prob", 0) > NO_SPEECH_PROB_THRESHOLD for s in segments_data)
    if all_high:
        print(f"  FILTERED: all segments have no_speech_prob > {NO_SPEECH_PROB_THRESHOLD} (avg={avg_prob:.3f})")
        return True
    return False


def _transcribe_with_model(model, model_name, audio_path):
    beam_size = _env_int("BEAM_SIZE", 5)
    no_speech_threshold = _env_float("NO_SPEECH_THRESHOLD", 0.6)
    condition_on_previous_text = os.environ.get("CONDITION_ON_PREVIOUS_TEXT", "true").lower() in ("true", "1", "yes")
    language = os.environ.get("WHISPER_LANGUAGE", "") or None

    t0 = time.perf_counter()
    segments_iter, info = model.transcribe(
        audio_path,
        language=language,
        vad_filter=True,
        word_timestamps=True,
        beam_size=beam_size,
        no_speech_threshold=no_speech_threshold,
        condition_on_previous_text=condition_on_previous_text,
    )
    segments = []
    seg_count = 0
    last_progress_time = time.perf_counter()
    progress_interval = 60
    print(f"Transcribing... (model={model_name}, lang={'auto' if not language else language})", flush=True)
    for seg in segments_iter:
        segments.append(seg)
        seg_count += 1
        now = time.perf_counter()
        if now - last_progress_time >= progress_interval:
            elapsed = now - t0
            print(f"  Progress: {seg_count} segments, {elapsed:.0f}s elapsed, last at {seg.end:.1f}s", flush=True)
            last_progress_time = now
    elapsed = time.perf_counter() - t0

    segments_data = []
    for seg in segments:
        d = {"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip(),
             "no_speech_prob": round(seg.no_speech_prob, 4)}
        if seg.words:
            d["words"] = [
                {"word": w.word, "start": round(w.start, 3), "end": round(w.end, 3), "probability": round(w.probability, 4)}
                for w in seg.words
            ]
        segments_data.append(d)

    no_speech_filtered = _check_no_speech(segments_data)
    if no_speech_filtered:
        segments_data = []

    print(f"Transcribe:  {len(segments_data)} segments, {elapsed:.2f}s (model={model_name})")
    print(f"Language:    {info.language} ({info.language_probability:.4f})")
    sample = " ".join(s["text"] for s in segments_data[:20]) if segments_data else ""
    return segments_data, {"language": info.language, "language_probability": round(info.language_probability, 4), "_transcript_sample": sample, "_no_speech_filtered": no_speech_filtered}, elapsed


def _load_whisperx_align():
    import whisperx
    import torch
    if not _check_vram(_VRAM_BUDGET["whisperx_align"], "WhisperX alignment"):
        return None, None, "cuda", "float16"
    lang = os.environ.get("WHISPER_LANGUAGE", "no")
    device = "cuda"
    model_name = os.environ.get("ALIGN_MODEL", "")
    print(f"  Loading WhisperX alignment model (lang={lang})...", flush=True)
    align_model, align_metadata = whisperx.load_align_model(
        language_code=lang, device=device, model_name=model_name if model_name else None
    )
    _log_memory("whisperx align loaded")
    return align_model, align_metadata, device, "float16"


def _align_with_model(align_model, align_metadata, device, audio_path, segments_data, analysis):
    import whisperx
    import torch

    t0 = time.perf_counter()
    audio = whisperx.load_audio(audio_path)
    result = whisperx.align(segments_data, align_model, align_metadata, audio, device)
    elapsed = time.perf_counter() - t0

    aligned_segments = result.get("segments", [])
    for i, seg in enumerate(segments_data):
        if i < len(aligned_segments):
            aseg = aligned_segments[i]
            if "words" in aseg and aseg["words"]:
                seg["words"] = [
                    {"word": w.get("word", ""), "start": round(w.get("start", seg["start"]), 3),
                     "end": round(w.get("end", seg["end"]), 3)}
                    for w in aseg["words"]
                ]

    print(f"Align:       WhisperX forced alignment, {elapsed:.2f}s (lang={analysis.get('language', '?')})")
    return elapsed


def _load_diarize():
    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        return None
    if not _check_vram(_VRAM_BUDGET["pyannote_diarize"], "pyannote diarization"):
        return None
    patch_torch_load()
    from pyannote.audio import Pipeline
    import torch
    print(f"  Loading diarization pipeline...", flush=True)
    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=hf_token)
    pipeline.to(torch.device("cuda"))
    _log_memory("diarize loaded")
    return pipeline


def _diarize_with_model(pipeline, audio_path, segments_data, analysis):
    import torch

    t0 = time.perf_counter()
    wav_path = _ensure_wav(audio_path)
    diarize_kwargs = {}
    min_speakers = os.environ.get("DIARIZE_MIN_SPEAKERS", "")
    max_speakers = os.environ.get("DIARIZE_MAX_SPEAKERS", "")
    if min_speakers:
        diarize_kwargs["min_speakers"] = int(min_speakers)
    if max_speakers:
        diarize_kwargs["max_speakers"] = int(max_speakers)
    diarization = pipeline(wav_path, **diarize_kwargs)
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
    return elapsed


def _load_vad():
    if not _check_vram(_VRAM_BUDGET["silero_vad"], "Silero VAD"):
        return None, None, None
    import torch
    print(f"  Loading VAD model...", flush=True)
    model, utils = torch.hub.load("snakers4/silero-vad", "silero_vad")
    (get_speech_timestamps, _, read_audio, _, _) = utils
    _log_memory("vad loaded")
    return model, get_speech_timestamps, read_audio


def _vad_with_model(model, get_speech_timestamps, read_audio, audio_path, segments_data, analysis):
    t0 = time.perf_counter()
    wav = read_audio(_ensure_wav(audio_path), sampling_rate=16000)
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
    return elapsed


def _load_emotion():
    if not _check_vram(_VRAM_BUDGET["wav2vec2_emotion"], "emotion (wav2vec2)"):
        return None
    from transformers import pipeline as hf_pipeline
    print(f"  Loading emotion model...", flush=True)
    classifier = hf_pipeline(
        "audio-classification",
        model="superb/wav2vec2-base-superb-er",
        device="cuda",
    )
    _log_memory("emotion loaded")
    return classifier


def _emotion_with_model(classifier, audio_path, segments_data, analysis):
    import torchaudio

    t0 = time.perf_counter()
    wav_path = _ensure_wav(audio_path)
    waveform_full, sr = torchaudio.load(wav_path)

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
    return elapsed


def _load_classify():
    if not _check_vram(_VRAM_BUDGET["ast_classify"], "audio classification (AST)"):
        return None
    from transformers import pipeline as hf_pipeline
    print(f"  Loading audio classifier...", flush=True)
    classifier = hf_pipeline("audio-classification", model="MIT/ast-finetuned-audioset-10-10-0.4593", device="cuda")
    _log_memory("classify loaded")
    return classifier


def _classify_with_model(classifier, audio_path, analysis):
    t0 = time.perf_counter()
    wav_path = _ensure_wav(audio_path)
    result = classifier(wav_path, top_k=10)
    tags = [{"label": r["label"], "score": round(r["score"], 3)} for r in result]
    elapsed = time.perf_counter() - t0

    analysis["audio_tags"] = tags
    print(f"Classify:    {tags[0]['label']} ({tags[0]['score']:.0%}), {elapsed:.2f}s")
    return elapsed


def process_batch_grouped(batch_path: str) -> None:
    _log_memory("batch start")
    with open(batch_path) as f:
        jobs = json.load(f)

    if not jobs:
        print("ERROR: No files in batch list")
        sys.exit(1)

    flags = get_analysis_flags()
    pipeline = os.environ.get("PIPELINE", "default")
    align_mode = os.environ.get("ALIGN_MODE", "none")
    skip_transcribe = os.environ.get("SKIP_TRANSCRIBE", "").lower() in ("true", "1", "yes")
    skip_analysis = os.environ.get("SKIP_ANALYSIS", "").lower() in ("true", "1", "yes")
    parallel_analysis = os.environ.get("PARALLEL_ANALYSIS", "true").lower() in ("true", "1", "yes")

    print(f"=== Grouped batch mode: {len(jobs)} files ===")
    print(f"Analysis: {', '.join(sorted(flags)) if flags else 'transcription only'}")
    if parallel_analysis and not skip_analysis:
        print("Parallel analysis: diarize+vad+emotion+classify concurrently")
    batch_t0 = time.perf_counter()

    per_file_data = [{} for _ in jobs]

    if not skip_transcribe:
        _stage_group_whisper(jobs, per_file_data, pipeline, align_mode)

    if not skip_analysis:
        _stage_group_analysis(jobs, per_file_data, flags, pipeline, parallel_analysis)

        if "summarize" in flags:
            skip_summarize = os.environ.get("SKIP_SUMMARIZE", "").lower() in ("true", "1", "yes")
            if not skip_summarize:
                _stage_group_summarize(jobs, per_file_data)
            else:
                print(f"\n--- Stage: Summarize (SKIPPED — will run in separate container) ---")

    for i, job in enumerate(jobs):
        audio_name = job.get("name", os.path.basename(job["input"]))
        output_dir = job["output"]
        segments_data = per_file_data[i].get("segments", [])
        analysis = per_file_data[i].get("analysis", {})
        timing = per_file_data[i].get("timing", {})

        analysis["pipeline_duration"] = round(timing.get("total", 0), 2)
        if "segments" not in timing:
            timing["segments"] = len(segments_data)
        if "language" not in timing:
            timing["language"] = analysis.get("language", "")
        if "language_probability" not in timing:
            timing["language_probability"] = analysis.get("language_probability", 0)
        if analysis.get("_no_speech_filtered"):
            timing["no_speech_filtered"] = True
            analysis.pop("_no_speech_filtered", None)
        timing_path = os.path.join(output_dir, "timing.json")
        with open(timing_path, "w", encoding="utf-8") as f:
            json.dump(timing, f, indent=2)

        write_all_outputs(segments_data, analysis, audio_name, output_dir)
        print(f"Wrote:       {output_dir}/timing.json")
        print(f"Total:       {timing.get('total', 0):.2f}s")

    total = time.perf_counter() - batch_t0
    _log_memory("batch end")
    print(f"\n{'='*60}")
    print(f"  Batch complete: {len(jobs)} files in {total:.1f}s")
    print(f"{'='*60}")


def _stage_group_whisper(jobs, per_file_data, pipeline, align_mode):
    import torch

    if pipeline == "whisperx":
        print(f"\n--- Stage: WhisperX full pipeline (transcribe+align+diarize) ---")
        for i, job in enumerate(jobs, 1):
            audio_path = job["input"]
            output_dir = job["output"]
            audio_name = job.get("name", os.path.basename(audio_path))
            print(f"\n[{i}/{len(jobs)}] {audio_name}", flush=True)
            os.makedirs(output_dir, exist_ok=True)
            try:
                segments_data, whisper_analysis = stage_transcribe_whisperx(audio_path)
                if os.environ.get("WHISPER_LANGUAGE", "") not in ("", "en"):
                    _validate_language_nordic(whisper_analysis)
                spell_fixes = _postprocess_transcript(segments_data, whisper_analysis.get("language", ""))
                per_file_data[i-1]["segments"] = segments_data
                per_file_data[i-1]["analysis"] = whisper_analysis
                per_file_data[i-1]["analysis"]["spell_fixes"] = spell_fixes
                per_file_data[i-1]["timing"] = {
                    "strategy": os.environ.get("STRATEGY", ""),
                    "whisper_model": os.environ.get("WHISPER_MODEL", "turbo"),
                    "pipeline": pipeline, "align_mode": align_mode,
                    "analysis": sorted(get_analysis_flags()),
                    "summarizer": os.environ.get("SUMMARY_OLLAMA_MODEL", ""),
                    "spell_fixes": spell_fixes,
                    "stages": {}, "total": 0,
                }
            except Exception:
                print(f"  ERROR processing {audio_name}:")
                traceback.print_exc()
        return

    print(f"\n--- Stage: Transcribe ---")
    result = _load_whisper()
    model, model_name = result if result[0] is not None else (None, result[1])
    if model is None:
        print("  WARNING: Could not load Whisper model (insufficient VRAM)")
        return
    try:
        for i, job in enumerate(jobs, 1):
            audio_path = job["input"]
            audio_name = job.get("name", os.path.basename(audio_path))
            print(f"\n[{i}/{len(jobs)}] {audio_name}", flush=True)
            os.makedirs(job["output"], exist_ok=True)
            try:
                segments_data, whisper_analysis, elapsed = _transcribe_with_model(model, model_name, audio_path)
                if os.environ.get("WHISPER_LANGUAGE", "") not in ("", "en"):
                    _validate_language_nordic(whisper_analysis)
                spell_fixes = _postprocess_transcript(segments_data, whisper_analysis.get("language", ""))
                if spell_fixes > 0:
                    print(f"Spell fix:   {spell_fixes} segments corrected")
                per_file_data[i-1]["segments"] = segments_data
                per_file_data[i-1]["analysis"] = whisper_analysis
                per_file_data[i-1]["timing"] = {
                    "strategy": os.environ.get("STRATEGY", ""),
                    "whisper_model": model_name, "pipeline": pipeline, "align_mode": align_mode,
                    "analysis": sorted(get_analysis_flags()),
                    "summarizer": os.environ.get("SUMMARY_OLLAMA_MODEL", ""),
                    "spell_fixes": spell_fixes,
                    "stages": {"transcribe": round(elapsed, 2)}, "total": round(elapsed, 2),
                }
            except Exception:
                print(f"  ERROR processing {audio_name}:")
                traceback.print_exc()
    finally:
        del model
        gc.collect()
        torch.cuda.empty_cache()
        print("  Whisper model unloaded.", flush=True)

    if pipeline == "default" and align_mode == "whisperx":
        print(f"\n--- Stage: WhisperX Alignment ---")
        try:
            first_analysis = per_file_data[0].get("analysis", {})
            first_lang = first_analysis.get("language", "no")
            align_result = _load_whisperx_align()
            if align_result[0] is None:
                print("  WARNING: Could not load WhisperX alignment model (insufficient VRAM)")
            else:
                align_model, align_metadata, align_device, _ = align_result
                try:
                    for i, job in enumerate(jobs, 1):
                        if not per_file_data[i-1].get("segments"):
                            continue
                        audio_name = job.get("name", os.path.basename(job["input"]))
                        print(f"\n[{i}/{len(jobs)}] {audio_name}", flush=True)
                        try:
                            elapsed = _align_with_model(
                                align_model, align_metadata, align_device,
                                job["input"], per_file_data[i-1]["segments"], per_file_data[i-1]["analysis"]
                            )
                            per_file_data[i-1]["timing"]["stages"]["align_whisperx"] = round(elapsed, 2)
                            per_file_data[i-1]["timing"]["total"] += elapsed
                        except Exception:
                            print(f"  WARNING: Alignment failed for {audio_name}")
                            traceback.print_exc()
                finally:
                    del align_model
                    for mod_name in list(sys.modules.keys()):
                        if "whisperx" in mod_name:
                            del sys.modules[mod_name]
                    gc.collect()
                    torch.cuda.empty_cache()
                    gc.collect()
                    torch.cuda.empty_cache()
                    print("  WhisperX align model unloaded.", flush=True)
        except Exception:
            print("  WARNING: Could not load WhisperX alignment model, skipping alignment")
            traceback.print_exc()


def _stage_group_analysis(jobs, per_file_data, flags, pipeline, parallel):
    import torch
    from concurrent.futures import ThreadPoolExecutor

    if not any(f in flags for f in ("diarize", "vad", "emotion", "classify", "language_id")):
        return

    analysis_stages = [f for f in ("diarize", "vad", "emotion", "classify", "language_id") if f in flags]
    print(f"\n--- Stage: Analysis ({', '.join(analysis_stages)}) ---")

    if parallel and len(analysis_stages) > 1:
        print("  Running analysis stages concurrently per file...")

        diarize_pipeline = _load_diarize() if "diarize" in flags and pipeline != "whisperx" else None
        vad_result = _load_vad() if "vad" in flags else (None, None, None)
        vad_model, vad_get_ts, vad_read_audio = vad_result if isinstance(vad_result, tuple) and len(vad_result) == 3 else (None, None, None)
        emotion_classifier = _load_emotion() if "emotion" in flags else None
        classify_classifier = _load_classify() if "classify" in flags else None

        try:
            for i, job in enumerate(jobs, 1):
                if not per_file_data[i-1].get("segments"):
                    continue
                audio_name = job.get("name", os.path.basename(job["input"]))
                print(f"\n[{i}/{len(jobs)}] {audio_name}", flush=True)

                def run_analysis(idx, j):
                    segs = per_file_data[idx]["segments"]
                    ana = per_file_data[idx]["analysis"]
                    timings = {}
                    errors = []

                    def do_diarize():
                        if diarize_pipeline:
                            try:
                                t = _diarize_with_model(diarize_pipeline, j["input"], segs, ana)
                                timings["diarize"] = round(t, 2)
                            except Exception as e:
                                errors.append(f"diarize: {e}")

                    def do_vad():
                        if vad_model:
                            try:
                                t = _vad_with_model(vad_model, vad_get_ts, vad_read_audio, j["input"], segs, ana)
                                timings["vad"] = round(t, 2)
                            except Exception as e:
                                errors.append(f"vad: {e}")

                    def do_emotion():
                        if emotion_classifier:
                            try:
                                t = _emotion_with_model(emotion_classifier, j["input"], segs, ana)
                                timings["emotion"] = round(t, 2)
                            except Exception as e:
                                errors.append(f"emotion: {e}")

                    def do_classify():
                        if classify_classifier:
                            try:
                                t = _classify_with_model(classify_classifier, j["input"], ana)
                                timings["classify"] = round(t, 2)
                            except Exception as e:
                                errors.append(f"classify: {e}")

                    with ThreadPoolExecutor(max_workers=4) as executor:
                        futs = []
                        if "diarize" in flags and pipeline != "whisperx":
                            futs.append(executor.submit(do_diarize))
                        if "vad" in flags:
                            futs.append(executor.submit(do_vad))
                        if "emotion" in flags:
                            futs.append(executor.submit(do_emotion))
                        if "classify" in flags:
                            futs.append(executor.submit(do_classify))
                        for f in futs:
                            f.result()

                    for err in errors:
                        print(f"  WARNING: {err}")

                    return timings

                try:
                    timings = run_analysis(i - 1, job)
                    for k, v in timings.items():
                        per_file_data[i-1]["timing"]["stages"][k] = v
                        per_file_data[i-1]["timing"]["total"] += v
                except Exception:
                    print(f"  ERROR in analysis for {audio_name}:")
                    traceback.print_exc()
        finally:
            diarize_pipeline = None
            vad_model = vad_get_ts = vad_read_audio = None
            emotion_classifier = None
            classify_classifier = None
            gc.collect()
            import torch
            torch.cuda.empty_cache()
            gc.collect()
            torch.cuda.empty_cache()
            print("  Analysis models unloaded.", flush=True)
    else:
        _stage_group_analysis_sequential(jobs, per_file_data, pipeline, analysis_stages)


def _stage_group_analysis_sequential(jobs, per_file_data, pipeline, analysis_stages):
    import torch

    for stage_name in analysis_stages:
        print(f"\n  --- Analysis sub-stage: {stage_name} ---")

        if stage_name == "diarize" and pipeline != "whisperx":
            pipeline_model = _load_diarize()
            if pipeline_model is None:
                continue
            try:
                for i, job in enumerate(jobs, 1):
                    if not per_file_data[i-1].get("segments"):
                        continue
                    audio_name = job.get("name", os.path.basename(job["input"]))
                    try:
                        elapsed = _diarize_with_model(
                            pipeline_model, job["input"],
                            per_file_data[i-1]["segments"], per_file_data[i-1]["analysis"]
                        )
                        per_file_data[i-1]["timing"]["stages"]["diarize"] = round(elapsed, 2)
                        per_file_data[i-1]["timing"]["total"] += elapsed
                    except Exception as e:
                        import traceback; traceback.print_exc()
                        print(f"  WARNING: Diarize failed for {audio_name}: {e}")
            finally:
                del pipeline_model
                gc.collect()
                torch.cuda.empty_cache()

        elif stage_name == "vad":
            model, get_ts, read_audio = _load_vad()
            try:
                for i, job in enumerate(jobs, 1):
                    if not per_file_data[i-1].get("segments"):
                        continue
                    try:
                        elapsed = _vad_with_model(
                            model, get_ts, read_audio, job["input"],
                            per_file_data[i-1]["segments"], per_file_data[i-1]["analysis"]
                        )
                        per_file_data[i-1]["timing"]["stages"]["vad"] = round(elapsed, 2)
                        per_file_data[i-1]["timing"]["total"] += elapsed
                    except Exception:
                        pass
            finally:
                del model
                gc.collect()

        elif stage_name == "emotion":
            classifier = _load_emotion()
            try:
                for i, job in enumerate(jobs, 1):
                    if not per_file_data[i-1].get("segments"):
                        continue
                    try:
                        elapsed = _emotion_with_model(
                            classifier, job["input"],
                            per_file_data[i-1]["segments"], per_file_data[i-1]["analysis"]
                        )
                        per_file_data[i-1]["timing"]["stages"]["emotion"] = round(elapsed, 2)
                        per_file_data[i-1]["timing"]["total"] += elapsed
                    except Exception:
                        pass
            finally:
                del classifier
                gc.collect()

        elif stage_name == "classify":
            classifier = _load_classify()
            try:
                for i, job in enumerate(jobs, 1):
                    if not per_file_data[i-1].get("segments"):
                        continue
                    try:
                        elapsed = _classify_with_model(
                            classifier, job["input"], per_file_data[i-1]["analysis"]
                        )
                        per_file_data[i-1]["timing"]["stages"]["classify"] = round(elapsed, 2)
                        per_file_data[i-1]["timing"]["total"] += elapsed
                    except Exception:
                        pass
            finally:
                del classifier
                gc.collect()
                torch.cuda.empty_cache()


def _stage_group_summarize(jobs, per_file_data):
    print(f"\n--- Stage: Summarize ---")
    ollama_model = os.environ.get("SUMMARY_OLLAMA_MODEL", "llama3.1:8b")
    _free_gpu_memory()

    for i, job in enumerate(jobs, 1):
        if not per_file_data[i-1].get("segments"):
            continue
        audio_name = job.get("name", os.path.basename(job["input"]))
        print(f"\n[{i}/{len(jobs)}] {audio_name}", flush=True)
        try:
            t_stage = time.perf_counter()
            stage_summarize(
                per_file_data[i-1]["segments"], per_file_data[i-1]["analysis"],
                job["output"], audio_name
            )
            elapsed = time.perf_counter() - t_stage
            per_file_data[i-1]["timing"]["stages"]["summarize"] = round(elapsed, 2)
            per_file_data[i-1]["timing"]["total"] += elapsed
        except Exception:
            print(f"  WARNING: Summarize failed for {audio_name}")
            traceback.print_exc()

        _unload_ollama_model(ollama_model)
        _free_gpu_memory()


def run_prefetch() -> None:
    import torch

    model_name = os.environ.get("WHISPER_MODEL", "turbo")
    errors = []

    def try_dl(name: str, fn) -> None:
        print(f">>> {name}...")
        try:
            fn()
            gc.collect()
            print(f"    OK")
        except Exception as exc:
            print(f"    FAILED: {exc}")
            errors.append(name)

    def dl_whisper() -> None:
        from faster_whisper import WhisperModel
        m = WhisperModel(model_name, device="cuda", compute_type="float16")
        del m

    def dl_vad() -> None:
        torch.hub.load("snakers4/silero-vad", "silero_vad")

    def dl_pyannote() -> None:
        hf_token = os.environ.get("HF_TOKEN", "")
        if not hf_token:
            print("    Skipping (no HF_TOKEN)")
            return
        patch_torch_load()
        from pyannote.audio import Pipeline
        Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=hf_token)
        del Pipeline
        torch.cuda.empty_cache()

    def dl_emotion() -> None:
        from transformers import pipeline as hf_pipeline
        p = hf_pipeline("audio-classification", model="superb/wav2vec2-base-superb-er", device="cuda")
        del p

    def dl_classify() -> None:
        from transformers import pipeline as hf_pipeline
        p = hf_pipeline("audio-classification", model="MIT/ast-finetuned-audioset-10-10-0.4593", device="cuda")
        del p

    def dl_lang_id() -> None:
        from speechbrain.inference.speaker import SpeakerRecognition
        lang_id = SpeakerRecognition.from_hparams(
            source="speechbrain/lang-id-commonlanguage_ecapa",
            savedir="/home/ubuntu/.cache/speechbrain/lang-id-ecapa",
        )
        del lang_id

    def dl_summary_hf() -> None:
        from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
        AutoTokenizer.from_pretrained("google/flan-t5-large")
        m = AutoModelForSeq2SeqLM.from_pretrained(
            "google/flan-t5-large", torch_dtype=torch.float16,
        )
        m = m.to("cuda")
        del m
        torch.cuda.empty_cache()

    def dl_whisperx() -> None:
        try:
            import whisperx
            m = whisperx.load_model("turbo", "cuda", compute_type="float16")
            del m; gc.collect(); torch.cuda.empty_cache()
            for lang in ["en", "no", "de", "fr", "es", "sv", "da"]:
                try:
                    model_a, metadata = whisperx.load_align_model(language_code=lang, device="cuda")
                    del model_a; gc.collect(); torch.cuda.empty_cache()
                    print(f"    Alignment model for '{lang}' OK")
                except Exception as exc:
                    print(f"    Alignment model for '{lang}' skipped: {exc}")
        except ImportError:
            print("    Skipping (whisperx not installed)")
        except Exception as exc:
            print(f"    FAILED: {exc}")

    print(f"=== Pre-fetching models (whisper={model_name}) ===")
    print()

    try_dl("Whisper", dl_whisper)
    try_dl("Silero VAD", dl_vad)
    try_dl("Pyannote diarization", dl_pyannote)
    try_dl("Emotion (wav2vec2)", dl_emotion)
    try_dl("Audio classification (AST)", dl_classify)
    try_dl("Language ID (SpeechBrain)", dl_lang_id)
    try_dl("Summary (HF: flan-t5-large)", dl_summary_hf)
    try_dl("WhisperX (turbo + en alignment)", dl_whisperx)

    print()
    if errors:
        print(f"Completed with {len(errors)} error(s):")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("=== All models cached successfully ===")


if __name__ == "__main__":
    if "--prefetch" in sys.argv:
        run_prefetch()
    elif "--summarize-only" in sys.argv:
        idx = sys.argv.index("--summarize-only")
        if idx + 2 >= len(sys.argv):
            print("Usage: --summarize-only <output_dir> <audio_name>")
            sys.exit(1)
        output_dir = sys.argv[idx + 1]
        audio_name = sys.argv[idx + 2]
        segments_data, analysis = _load_existing_output(output_dir, audio_name)
        if segments_data is None:
            print(f"ERROR: No full.json found in {output_dir}")
            sys.exit(1)
        if not segments_data:
            print(f"WARNING: No segments in {output_dir}/full.json — skipping summarize")
            sys.exit(0)
        stage_summarize(segments_data, analysis, output_dir, audio_name)
    elif "--summarize-batch" in sys.argv:
        idx = sys.argv.index("--summarize-batch")
        batch_dir = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "/batch-output"
        import glob as _glob
        subdirs = sorted(_glob.glob(os.path.join(batch_dir, "[0-9]*")))
        if not subdirs:
            print("No output directories found for summarize-batch")
            sys.exit(0)
        ollama_model = os.environ.get("SUMMARY_OLLAMA_MODEL", "llama3.1:8b")
        _free_gpu_memory()
        _log_memory("summarize-batch start")
        for i, d in enumerate(subdirs, 1):
            full_path = os.path.join(d, "full.json")
            summary_path = os.path.join(d, "summary.md")
            if not os.path.exists(full_path):
                continue
            if os.path.exists(summary_path):
                print(f"\n[{i}/{len(subdirs)}] {os.path.basename(d)} — already has summary, skipping")
                continue
            audio_name = ""
            try:
                with open(full_path, encoding="utf-8") as f:
                    full = json.load(f)
                audio_name = full.get("file", os.path.basename(d))
                segments_data = full.get("segments", [])
                analysis = {k: v for k, v in full.items() if k != "segments"}
                if not segments_data:
                    print(f"\n[{i}/{len(subdirs)}] {audio_name} — no segments, skipping")
                    continue
                print(f"\n[{i}/{len(subdirs)}] {audio_name}", flush=True)
                stage_summarize(segments_data, analysis, d, audio_name)
            except Exception as exc:
                print(f"  ERROR: summarize failed for {audio_name}: {exc}")
                traceback.print_exc()
            _unload_ollama_model(ollama_model)
            _free_gpu_memory()
        _log_memory("summarize-batch end")
        print(f"\n=== Summarize batch complete: {len(subdirs)} dirs ===")
    elif "--batch" in sys.argv:
        batch_path = BATCH_FILE
        for i, arg in enumerate(sys.argv):
            if arg == "--batch" and i + 1 < len(sys.argv):
                batch_path = sys.argv[i + 1]
        grouped = os.environ.get("GROUPED", "false").lower() in ("true", "1", "yes")
        if grouped:
            process_batch_grouped(batch_path)
        else:
            process_batch(batch_path)
    else:
        main()
