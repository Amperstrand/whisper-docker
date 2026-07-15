import csv
import hashlib
import json
import re
import tempfile
from collections import Counter
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


DEFAULT_SOURCE_DIR = Path("/home/ubuntu/src/whisper-docker/input/20190612_x8sf8NBlk5M_OsloMet - Styremøte/boardroom-accurate-gradepack")
DEFAULT_OUTPUT_ZIP = Path("/tmp/boardroom-llm-ready.zip")
RUN_CONFIG = {
    "strategy": "boardroom-llm-ready",
    "whisper_model": "Necklace/faster-nb-whisper-large",
    "alignment_model": "NbAiLab/nb-wav2vec2-1b-bokmaal-v2",
    "language": "no",
    "language_forced": True,
    "diarization": "pyannote/speaker-diarization-3.1",
    "diarize_min_speakers": 2,
    "diarize_max_speakers": 12,
    "vad": "silero",
    "cleanup_applied": True,
    "source_run": "boardroom-accurate-gradepack",
}
INSTITUTIONAL_TERMS = [
    "OsloMet",
    "KD",
    "DBH",
    "SSB",
    "NOKUT",
    "UHR",
    "UiO",
    "NTNU",
    "Høgskolen",
    "universitet",
    "fakultet",
    "styret",
    "rektor",
    "dekan",
    "prodekan",
]
DECISION_SIGNAL_WORDS = [
    "vedtatt",
    "vedtak",
    "beslutte",
    "besluttet",
    "enig",
    "stiller seg bak",
    "godkjenner",
    "godkjent",
    "vedtas",
    "enstemmig",
    "flertall",
    "stemmer for",
    "vedtar",
]
ACTION_SIGNAL_WORDS = [
    "følge opp",
    "ta ansvar",
    "sette opp",
    "lage",
    "utarbeide",
    "sende",
    "rapportere",
    "frist",
    "innstille",
    "forberede",
    "gå videre med",
    "sette i verk",
    "implementere",
    "forelegge",
    "komme tilbake til",
]
CLEANUP_RULES = [
    ("fakultettene", re.compile(r"\bfakultettene\b", re.IGNORECASE), "fakultetene"),
    ("fakultett", re.compile(r"\bfakultett\b", re.IGNORECASE), "fakultet"),
    ("departementent", re.compile(r"\bdepartementent\b", re.IGNORECASE), "departementet"),
    ("departemente", re.compile(r"\bdepartemente\b", re.IGNORECASE), "departementet"),
    ("ledergrupp?", re.compile(r"\bledergrupp?\b", re.IGNORECASE), "ledergruppa"),
    ("ledergruppe", re.compile(r"\bledergruppe\b", re.IGNORECASE), "ledergruppa"),
    ("stipendialtillinger", re.compile(r"\bstipendialtillinger\b", re.IGNORECASE), "stipendiatstillinger"),
    ("stipendialstillinger", re.compile(r"\bstipendialstillinger\b", re.IGNORECASE), "stipendiatstillinger"),
    ("bebildningen", re.compile(r"\bbebildningen\b", re.IGNORECASE), "bevilgningen"),
    ("bebildning", re.compile(r"\bbebildning\b", re.IGNORECASE), "bevilgning"),
    ("forgravd", re.compile(r"\bforgravd\b", re.IGNORECASE), "forfremja"),
    ("overfordobla", re.compile(r"\boverfordobla\b", re.IGNORECASE), "overfordoblet"),
    (
        "universitets og høgskolerådet",
        re.compile(r"\buniversitets\s+og\s+høgskolerådet\b", re.IGNORECASE),
        "universitets- og høgskolerådet",
    ),
    ("høgskole", re.compile(r"\bhøgskole\b", re.IGNORECASE), "høyskole"),
    ("Gr", re.compile(r"\bGr\b"), "Går"),
    ("gr", re.compile(r"\bgr\s", re.IGNORECASE), "går "),
    ("str", re.compile(r"\bstr\b", re.IGNORECASE), "står"),
]
RUN_CONFIG["cleanup_rules_count"] = len(CLEANUP_RULES)


def format_hms(seconds):
    total = max(0, int(round(seconds)))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def json_dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path, content):
    path.write_text(content, encoding="utf-8")


def write_csv(path, rows, fieldnames):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def avg_word_probability(segment):
    words = segment.get("words") or []
    values = [word.get("probability") for word in words if isinstance(word.get("probability"), (int, float))]
    if not values:
        return None
    return sum(values) / len(values)


def apply_cleanup(text, rule_hits):
    cleaned = text
    for name, pattern, replacement in CLEANUP_RULES:
        cleaned, count = pattern.subn(replacement, cleaned)
        if count:
            rule_hits[name] += count
    return cleaned


def build_words(segments):
    words = []
    for index, segment in enumerate(segments):
        for word in segment.get("words") or []:
            entry = dict(word)
            entry["segment_index"] = index
            words.append(entry)
    return words


def build_llm_sections(segments_with_meta):
    sections = []
    current = None
    previous_speaker = None
    for item in segments_with_meta:
        cleaned_text = item["cleaned_text"].strip()
        if not cleaned_text:
            continue
        words_count = len(cleaned_text.split())
        speaker = item["speaker"]
        same_speaker = current is not None and current["speaker"] == speaker
        within_limit = current is not None and (current["word_count"] + words_count <= 200)
        if same_speaker and within_limit and current is not None:
            current["end"] = item["end"]
            current["texts"].append(cleaned_text)
            current["probs"].append(item["avg_word_probability"])
            current["word_count"] += words_count
            current["insert_separator"] = False
            continue
        current = {
            "speaker": speaker,
            "start": item["start"],
            "end": item["end"],
            "texts": [cleaned_text],
            "probs": [item["avg_word_probability"]],
            "word_count": words_count,
            "insert_separator": previous_speaker is not None and previous_speaker != speaker,
        }
        sections.append(current)
        previous_speaker = speaker
    parts = []
    for section in sections:
        probs = [value for value in section["probs"] if value is not None]
        avg_prob = sum(probs) / len(probs) if probs else None
        if section["insert_separator"]:
            parts.append("---\n")
        if avg_prob is not None and avg_prob < 0.2:
            parts.append("<!-- UNCERTAIN -->\n")
        parts.append(
            f"## [{format_hms(section['start'])} - {format_hms(section['end'])}] {section['speaker']}\n\n"
        )
        paragraph = " ".join(section["texts"]).strip()
        if avg_prob is not None and avg_prob < 0.3:
            paragraph = f"{paragraph} [?]"
        parts.append(f"{paragraph}\n\n")
    return "".join(parts)


def collect_terms(segments_with_meta):
    phrase_pattern = re.compile(r"\b(?:[A-ZÆØÅ][\wÆØÅæøå.-]*)(?:\s+[A-ZÆØÅ][\wÆØÅæøå.-]*)+\b")
    occurrences = {}

    def add_occurrence(term, segment, text, start_idx, end_idx, canonical=None):
        key = canonical or term
        context_start = max(0, start_idx - 50)
        context_end = min(len(text), end_idx + 50)
        context = text[context_start:context_end].strip()
        if key not in occurrences:
            occurrences[key] = {
                "term": key,
                "context": context,
                "start": segment["start"],
                "end": segment["end"],
                "speaker": segment["speaker"],
                "frequency": 0,
            }
        occurrences[key]["frequency"] += 1

    for segment in segments_with_meta:
        text = segment["cleaned_text"]
        for match in phrase_pattern.finditer(text):
            add_occurrence(match.group(0), segment, text, match.start(), match.end())
        for term in INSTITUTIONAL_TERMS:
            for match in re.finditer(rf"(?i)\b{re.escape(term)}\b", text):
                add_occurrence(match.group(0), segment, text, match.start(), match.end(), canonical=term)

    rows = [value for value in occurrences.values() if value["frequency"] >= 2]
    rows.sort(key=lambda row: (-row["frequency"], row["term"].lower(), row["start"]))
    return rows


def find_signal_rows(segments_with_meta, signal_words):
    rows = []
    for segment in segments_with_meta:
        text = segment["cleaned_text"]
        lowered = text.lower()
        matches = [signal for signal in signal_words if signal in lowered]
        if matches:
            rows.append(
                {
                    "segment_index": segment["segment_index"],
                    "start": segment["start"],
                    "end": segment["end"],
                    "speaker": segment["speaker"],
                    "text_snippet": text[:150],
                    "signal_words": ", ".join(sorted(dict.fromkeys(matches))),
                }
            )
    return rows


def find_factcheck_rows(segments_with_meta):
    patterns = [
        ("percentage_claim", re.compile(r"\b\d+(?:[.,]\d+)?\s*%")),
        ("budget_figure", re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:mill(?:\.|ion(?:er)?)?|mrd|kroner?|kr)\b", re.IGNORECASE)),
        ("temporal_reference", re.compile(r"\b(?:19|20)\d{2}\b|\bi fjor\b|\bneste år\b", re.IGNORECASE)),
        (
            "institution_reference",
            re.compile(r"(?i)\b(?:" + "|".join(re.escape(term) for term in INSTITUTIONAL_TERMS) + r")\b"),
        ),
        ("estimate_claim", re.compile(r"\b(?:prognose|estimat|antatt|beregne)\b", re.IGNORECASE)),
    ]
    rows = []
    for segment in segments_with_meta:
        text = segment["cleaned_text"]
        for signal_type, pattern in patterns:
            for match in pattern.finditer(text):
                rows.append(
                    {
                        "segment_index": segment["segment_index"],
                        "start": segment["start"],
                        "end": segment["end"],
                        "speaker": segment["speaker"],
                        "text_snippet": text[:150],
                        "signal_type": signal_type,
                        "matched_text": match.group(0),
                    }
                )
    return rows


def build_manifest(output_dir):
    manifest = []
    for path in sorted(output_dir.iterdir()):
        if not path.is_file() or path.name == "MANIFEST.json":
            continue
        data = path.read_bytes()
        manifest.append(
            {
                "file": path.name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return manifest


def main(source_dir=DEFAULT_SOURCE_DIR, output_zip=DEFAULT_OUTPUT_ZIP):
    source_dir = Path(source_dir)
    output_zip = Path(output_zip)
    full_data = json.loads((source_dir / "full.json").read_text(encoding="utf-8"))
    analysis = json.loads((source_dir / "analysis.json").read_text(encoding="utf-8"))
    timing = json.loads((source_dir / "timing.json").read_text(encoding="utf-8"))
    segments = full_data["segments"]
    words = build_words(segments)
    rule_hits = Counter()
    segments_with_meta = []
    modified_segments = 0
    for index, segment in enumerate(segments):
        original = segment.get("text", "")
        cleaned = apply_cleanup(original, rule_hits)
        if cleaned != original:
            modified_segments += 1
        segments_with_meta.append(
            {
                "segment_index": index,
                "start": segment.get("start"),
                "end": segment.get("end"),
                "speaker": segment.get("speaker", "UNKNOWN"),
                "original_text": original,
                "cleaned_text": cleaned,
                "no_speech_prob": segment.get("no_speech_prob"),
                "avg_word_probability": avg_word_probability(segment),
                "speech_ratio": segment.get("speech_ratio"),
            }
        )

    raw_transcript = "\n".join(item["original_text"] for item in segments_with_meta) + "\n"
    cleaned_transcript = "\n".join(
        f"[{item['speaker']}] {item['cleaned_text']}" for item in segments_with_meta
    ) + "\n"

    duration = max((segment.get("end", 0) or 0) for segment in segments)
    speaker_list = analysis.get("speakers") or sorted({item["speaker"] for item in segments_with_meta})
    llm_ready = (
        "# OsloMet Styremøte — 2019-06-12 (LLM-Ready Transcript)\n\n"
        "- Language: no\n"
        f"- Duration: {format_hms(duration)}\n"
        f"- Speakers: {len(speaker_list)} ({', '.join(speaker_list)})\n"
        f"- Whisper model: {RUN_CONFIG['whisper_model']}\n"
        f"- Alignment model: {RUN_CONFIG['alignment_model']}\n"
        f"- Diarization: {RUN_CONFIG['diarization']}\n\n"
        + build_llm_sections(segments_with_meta)
    )

    uncertain_rows = []
    for item in segments_with_meta:
        reasons = []
        avg_prob = item["avg_word_probability"]
        if avg_prob is not None and avg_prob < 0.3:
            reasons.append("low_word_confidence")
        if isinstance(item["no_speech_prob"], (int, float)) and item["no_speech_prob"] > 0.5:
            reasons.append("high_no_speech_prob")
        if reasons:
            uncertain_rows.append(
                {
                    "segment_index": item["segment_index"],
                    "start": item["start"],
                    "end": item["end"],
                    "speaker": item["speaker"],
                    "text_snippet": item["cleaned_text"][:100],
                    "avg_word_probability": "" if avg_prob is None else round(avg_prob, 4),
                    "reason": ", ".join(reasons),
                }
            )

    terminology_rows = collect_terms(segments_with_meta)
    decision_rows = find_signal_rows(segments_with_meta, DECISION_SIGNAL_WORDS)
    action_rows = find_signal_rows(segments_with_meta, ACTION_SIGNAL_WORDS)
    factcheck_rows = find_factcheck_rows(segments_with_meta)

    words_with_prob = [word.get("probability") for word in words if isinstance(word.get("probability"), (int, float))]
    alignment_stats = {
        "segments": len(segments),
        "words": len(words),
        "segments_with_words": sum(1 for segment in segments if segment.get("words")),
        "avg_word_probability": round(sum(words_with_prob) / len(words_with_prob), 4) if words_with_prob else None,
        "avg_speech_ratio": round(
            sum((segment.get("speech_ratio") or 0) for segment in segments) / len(segments), 4
        )
        if segments
        else None,
    }

    report = "\n".join(
        [
            "# Packaging Report",
            "",
            "## Pipeline configuration used",
            "```json",
            json.dumps(RUN_CONFIG, ensure_ascii=False, indent=2),
            "```",
            "",
            "## Segment/word/speaker counts",
            f"- Segments: {len(segments)}",
            f"- Words: {len(words)}",
            f"- Speakers: {len(speaker_list)}",
            "",
            "## Alignment statistics",
            f"- Segments with word timings: {alignment_stats['segments_with_words']}",
            f"- Average word probability: {alignment_stats['avg_word_probability']}",
            f"- Average speech ratio: {alignment_stats['avg_speech_ratio']}",
            f"- Pipeline runtime (source timing): {timing.get('total')}",
            "",
            "## Cleanup statistics",
            f"- Modified segments: {modified_segments}",
            f"- Rules fired: {sum(rule_hits.values())}",
            f"- Rule counts: {json.dumps(dict(sorted(rule_hits.items())), ensure_ascii=False)}",
            "",
            "## Uncertainty statistics",
            f"- Uncertain spans: {len(uncertain_rows)}",
            f"- Low-confidence paragraphs are marked with [?] and HTML comments at < 0.2 avg probability.",
            "",
            "## Helper file statistics",
            f"- terminology_candidates.csv rows: {len(terminology_rows)}",
            f"- possible_decision_spans.csv rows: {len(decision_rows)}",
            f"- possible_action_spans.csv rows: {len(action_rows)}",
            f"- possible_factcheck_spans.csv rows: {len(factcheck_rows)}",
        ]
    ) + "\n"

    readme = "\n".join(
        [
            "# LLM-Ready Transcript Package",
            "",
            "## What this package contains",
            "- Source-faithful exports from the whisper-docker run.",
            "- A conservatively cleaned transcript for downstream LLM use.",
            "- Helper CSVs for uncertainty review, terminology, decisions, actions, and fact-checking.",
            "",
            "## Read this first",
            "1. Start with `llm_ready_transcript.md`.",
            "2. Use `cleaned_transcript.txt` for line-by-line speaker-labelled text.",
            "3. Fall back to `raw_transcript.txt`, `segments.json`, and `words.json` when exact source fidelity matters.",
            "",
            "## Helper files and when to use them",
            "- `uncertain_spans.csv`: inspect low-confidence or likely non-speech segments.",
            "- `terminology_candidates.csv`: review recurring names, acronyms, and institutional terms.",
            "- `possible_decision_spans.csv`: scan for likely decisions or approvals.",
            "- `possible_action_spans.csv`: scan for likely follow-up work or assigned actions.",
            "- `possible_factcheck_spans.csv`: review claims involving figures, dates, institutions, or estimates.",
            "",
            "## Uncertainty markers",
            "- `[?]` means the paragraph average word probability is below 0.3.",
            "- `<!-- UNCERTAIN -->` means the paragraph average word probability is below 0.2.",
            "- `uncertain_spans.csv` also flags segments with high `no_speech_prob`.",
            "",
            "## Cleanup policy",
            "- Cleanup is limited to the explicitly requested high-confidence regex replacements.",
            "- Original segment text is preserved in `raw_transcript.txt` and source JSON exports.",
            "- Timestamps, segment order, and speaker labels are unchanged.",
            "- No translation, speaker identity guessing, or topic restructuring was performed.",
            "",
            "## Limitations and caveats",
            "- Speaker labels remain anonymous (`SPEAKER_XX`).",
            "- Some transcript errors may remain because cleanup is intentionally conservative.",
            "- Helper CSVs are heuristic aids, not definitive annotations.",
            "- Low-confidence segments should be checked against `segments.json` and `words.json` before drawing conclusions.",
        ]
    ) + "\n"

    with tempfile.TemporaryDirectory(prefix="llm-ready-", dir="/tmp") as temp_dir:
        output_dir = Path(temp_dir)
        write_text(output_dir / "raw_transcript.txt", raw_transcript)
        json_dump(output_dir / "segments.json", segments)
        json_dump(output_dir / "words.json", words)
        json_dump(output_dir / "analysis.json", analysis)
        json_dump(output_dir / "timing.json", timing)
        json_dump(output_dir / "run_config.json", RUN_CONFIG)
        write_text(output_dir / "cleaned_transcript.txt", cleaned_transcript)
        write_text(output_dir / "llm_ready_transcript.md", llm_ready)
        write_csv(
            output_dir / "uncertain_spans.csv",
            uncertain_rows,
            ["segment_index", "start", "end", "speaker", "text_snippet", "avg_word_probability", "reason"],
        )
        write_csv(
            output_dir / "terminology_candidates.csv",
            terminology_rows,
            ["term", "context", "start", "end", "speaker", "frequency"],
        )
        write_csv(
            output_dir / "possible_decision_spans.csv",
            decision_rows,
            ["segment_index", "start", "end", "speaker", "text_snippet", "signal_words"],
        )
        write_csv(
            output_dir / "possible_action_spans.csv",
            action_rows,
            ["segment_index", "start", "end", "speaker", "text_snippet", "signal_words"],
        )
        write_csv(
            output_dir / "possible_factcheck_spans.csv",
            factcheck_rows,
            ["segment_index", "start", "end", "speaker", "text_snippet", "signal_type", "matched_text"],
        )
        write_text(output_dir / "report.md", report)
        write_text(output_dir / "README.md", readme)
        json_dump(output_dir / "MANIFEST.json", build_manifest(output_dir))

        output_zip.parent.mkdir(parents=True, exist_ok=True)
        if output_zip.exists():
            output_zip.unlink()
        with ZipFile(output_zip, "w", compression=ZIP_DEFLATED) as archive:
            for path in sorted(output_dir.iterdir()):
                archive.write(path, arcname=path.name)

    print(json.dumps({"output_zip": str(output_zip), "files": 16}, ensure_ascii=False))


if __name__ == "__main__":
    main()
