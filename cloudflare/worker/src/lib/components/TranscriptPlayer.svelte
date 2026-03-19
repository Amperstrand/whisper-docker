<script lang="ts">
  import { onMount } from "svelte";
  import type { Segment, Word, Analysis } from "$lib/types";

  let { segments, audioEl, analysis }: {
    segments: Segment[];
    audioEl?: HTMLAudioElement | null;
    analysis?: Analysis | null;
  } = $props();

  const SPEAKER_COLORS = [
    "#0a84ff",
    "#ff9f0a",
    "#30d158",
    "#bf5af2",
    "#ff375f",
    "#64d2ff",
    "#ffd60a",
    "#63e6e2",
  ];

  const EMOTION_COLORS: Record<string, string> = {
    neutral: "#86868b",
    happy: "#30d158",
    sad: "#0a84ff",
    angry: "#ff375f",
  };

  let allWords: Array<{ word: string; start: number; end: number; segIdx: number; speaker?: string; emotion?: { label: string; score: number } }> = $state([]);
  let scrollContainer: HTMLDivElement | null = $state(null);
  let speakerColorMap: Map<string, string> = new Map();
  let speakerList: string[] = [];
  let hasSpeakers = false;
  let hasEmotions = false;
  let hasVad = false;

  $effect(() => {
    allWords = [];
    speakerColorMap = new Map();
    speakerList = [];
    hasSpeakers = false;
    hasEmotions = false;
    hasVad = false;
    if (!segments) return;

    let colorIdx = 0;
    const speakers = new Set<string>();
    segments.forEach((seg: Segment, segIdx: number) => {
      if (seg.speaker) speakers.add(seg.speaker);
      if (seg.emotion) hasEmotions = true;
      if (seg.speech_ratio !== undefined) hasVad = true;
      if (seg.words) {
        seg.words.forEach((w: Word) => {
          allWords.push({
            word: w.word,
            start: w.start,
            end: w.end,
            segIdx,
            speaker: seg.speaker,
            emotion: seg.emotion,
          });
        });
      }
    });

    speakerList = Array.from(speakers).sort();
    hasSpeakers = speakerList.length > 0;
    for (const speaker of speakerList) {
      speakerColorMap.set(speaker, SPEAKER_COLORS[colorIdx % SPEAKER_COLORS.length]);
      colorIdx++;
    }
  });

  function findCurrentWordIdx(time: number): number {
    let lo = 0;
    let hi = allWords.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (allWords[mid].start <= time && time < allWords[mid].end) return mid;
      if (allWords[mid].end <= time) lo = mid + 1;
      else hi = mid - 1;
    }
    return -1;
  }

  onMount(() => {
    const audio = document.querySelector("audio") as HTMLAudioElement | null;
    if (!audio) return;

    const container = document.querySelector(".transcript-scroll");
    if (!container) return;

    let words: typeof allWords = [];
    let rafId: number;
    let lastActiveIdx = -1;

    function findIdx(time: number): number {
      if (words.length === 0) return -1;
      let lo = 0;
      let hi = words.length - 1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (words[mid].start <= time && time < words[mid].end) return mid;
        if (words[mid].end <= time) lo = mid + 1;
        else hi = mid - 1;
      }
      return -1;
    }

    function frame() {
      if (words.length === 0 && allWords.length > 0) {
        words = allWords.map((w) => ({ ...w }));
      }

      const time = audio.currentTime;
      const idx = findIdx(time);

      if (idx !== lastActiveIdx) {
        if (lastActiveIdx >= 0) {
          const prev = container.querySelector(`[data-word-idx="${lastActiveIdx}"]`) as HTMLElement | null;
          if (prev) {
            prev.classList.remove("active", "in-active-segment");
            prev.style.removeProperty("--sweep");
          }
        }

        lastActiveIdx = idx;
        updateWordClasses(idx);

        if (idx >= 0) {
          const el = container.querySelector(`[data-word-idx="${idx}"]`) as HTMLElement | null;
          if (el) {
            el.classList.add("active", "in-active-segment");
            smartScroll(el);
          }
        }
      }

      if (idx >= 0) {
        const el = container.querySelector(`[data-word-idx="${idx}"]`) as HTMLElement | null;
        if (el) {
          const w = words[idx];
          const duration = w.end - w.start;
          const progress = duration > 0 ? (time - w.start) / duration : 1;
          el.style.setProperty("--sweep", `${progress * 100}%`);
        }
      }

      rafId = requestAnimationFrame(frame);
    }

    function updateWordClasses(activeIdx: number) {
      const wordEls = container.querySelectorAll(".word");
      wordEls.forEach((wordEl, i) => {
        wordEl.classList.remove("active", "in-active-segment", "past", "future");
        if (i < activeIdx) wordEl.classList.add("past");
        else if (i > activeIdx) wordEl.classList.add("future");
        else if (i === activeIdx) wordEl.classList.add("active", "in-active-segment");
      });
    }

    function smartScroll(wordEl: HTMLElement) {
      const containerRect = container.getBoundingClientRect();
      const elRect = wordEl.getBoundingClientRect();
      const elCenter = elRect.top + elRect.height / 2;
      const containerCenter = containerRect.top + containerRect.height / 2;
      const diff = elCenter - containerCenter;
      if (Math.abs(diff) > containerRect.height * 0.3) {
        container.scrollBy({ top: diff, behavior: "smooth" });
      }
    }

    audio.addEventListener("timeupdate", frame);
    rafId = requestAnimationFrame(frame);
    return () => {
      cancelAnimationFrame(rafId);
      audio.removeEventListener("timeupdate", frame);
    };
  });

  function seekTo(word: typeof allWords[number]) {
    if (audioEl) audioEl.currentTime = word.start;
  }

  function isSegmentStart(wordIdx: number): boolean {
    if (wordIdx === 0) return true;
    return allWords[wordIdx].segIdx !== allWords[wordIdx - 1].segIdx;
  }

  function isSpeakerChange(wordIdx: number): boolean {
    if (!hasSpeakers) return false;
    if (wordIdx === 0) return true;
    return allWords[wordIdx].speaker !== allWords[wordIdx - 1].speaker;
  }

  function speakerLabel(speaker: string): string {
    const idx = speakerList.indexOf(speaker);
    return idx >= 0 ? `Speaker ${idx + 1}` : speaker;
  }

  function emotionColor(label: string): string {
    return EMOTION_COLORS[label] || "#86868b";
  }
</script>

{#if allWords.length === 0}
  <p class="no-data">No word-level timestamps available.</p>
{:else}
  {#if analysis}
    <div class="analysis-summary">
      {#if analysis.audio_tags?.length}
        <div class="tags-row">
          {#each analysis.audio_tags.slice(0, 5) as tag}
            <span class="audio-tag">{tag.label} <span class="tag-score">{(tag.score * 100).toFixed(0)}%</span></span>
          {/each}
        </div>
      {/if}
      {#if analysis.vad}
        <span class="vad-stat">Speech: {(analysis.vad.speech_ratio * 100).toFixed(0)}%</span>
      {/if}
      {#if analysis.language_id}
        <span class="vad-stat">Language: {analysis.language_id.label} ({(analysis.language_id.score * 100).toFixed(0)}%)</span>
      {/if}
      {#if analysis.pipeline_duration}
        <span class="vad-stat">Processed in {analysis.pipeline_duration}s</span>
      {/if}
    </div>
  {/if}

  {#if hasSpeakers}
    <div class="speaker-legend">
      {#each speakerList as speaker}
        <span class="speaker-chip" style="--speaker-color: {speakerColorMap.get(speaker)}">
          {speakerLabel(speaker)}
        </span>
      {/each}
    </div>
  {/if}

  <div class="transcript-scroll" bind:this={scrollContainer}>
    {#each allWords as w, i (i)}
      {#if isSpeakerChange(i)}
        {#if i > 0}<br />{/if}
        {#if hasSpeakers && w.speaker}
          <span class="speaker-inline" style="--speaker-color: {speakerColorMap.get(w.speaker)}">
            {speakerLabel(w.speaker)}
          </span>
        {/if}
      {/if}
      <span
        class="word"
        data-word-idx={i}
        onclick={() => seekTo(w)}
        style={w.emotion ? `--emotion-color: ${emotionColor(w.emotion.label)}` : ""}
      >{w.word}</span>
    {/each}
  </div>
{/if}

<style>
  .no-data {
    color: var(--text-muted);
    font-size: 0.875rem;
    font-style: italic;
  }

  .analysis-summary {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 1rem;
    margin-bottom: 0.5rem;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    font-size: 0.8rem;
  }

  .tags-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
  }

  .audio-tag {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.15rem 0.5rem;
    border-radius: 9999px;
    font-size: 0.75rem;
    background: var(--bg-hover);
    border: 1px solid var(--border);
    color: var(--text);
  }

  .tag-score {
    color: var(--text-muted);
    font-size: 0.65rem;
  }

  .vad-stat {
    color: var(--text-muted);
    font-size: 0.75rem;
  }

  .speaker-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    padding: 0.5rem 1rem;
    margin-bottom: 0.5rem;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
    border-bottom: none;
  }

  .speaker-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.2rem 0.6rem;
    border-radius: 9999px;
    font-size: 0.75rem;
    font-weight: 600;
    background: color-mix(in srgb, var(--speaker-color) 15%, transparent);
    color: var(--speaker-color);
    border: 1px solid color-mix(in srgb, var(--speaker-color) 30%, transparent);
  }

  .speaker-chip::before {
    content: "";
    display: block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--speaker-color);
  }

  .speaker-inline {
    display: inline-block;
    font-size: 0.7rem;
    font-weight: 700;
    color: var(--speaker-color);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-right: 0.25rem;
    opacity: 0.85;
    vertical-align: middle;
    line-height: 2.2;
  }

  .transcript-scroll {
    max-height: 60vh;
    overflow-y: auto;
    padding: 1rem;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    line-height: 2.2;
    font-size: 1.125rem;
    scroll-behavior: smooth;
  }

  .word {
    cursor: pointer;
    padding: 0.1em 0.05em;
    border-radius: 3px;
    white-space: pre-wrap;
    transition: opacity 0.25s ease, color 0.15s ease;
    position: relative;
  }

  .word:hover {
    background: var(--bg-hover);
  }

  .word.future {
    opacity: 0.4;
  }

  .word.past {
    opacity: 0.6;
  }

  .word.in-active-segment {
    opacity: 1;
  }

  .word.active {
    opacity: 1;
    color: var(--accent);
    font-weight: 600;
    background: linear-gradient(
      90deg,
      rgba(10, 132, 255, 0.2) 0%,
      rgba(10, 132, 255, 0.2) var(--sweep, 0%),
      transparent var(--sweep, 0%),
      transparent 100%
    );
    background-color: transparent;
  }

  .word:not(.active)[style*="--emotion-color"] {
    border-bottom: 2px solid var(--emotion-color, transparent);
    padding-bottom: 0.05em;
  }

  @media (max-width: 480px) {
    .transcript-scroll {
      font-size: 1rem;
      line-height: 2;
      padding: 0.75rem;
    }

    .speaker-legend {
      padding: 0.375rem 0.75rem;
    }

    .analysis-summary {
      padding: 0.375rem 0.75rem;
    }
  }
</style>
