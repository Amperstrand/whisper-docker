<script lang="ts">
  import type { Segment, Word } from "$lib/types";

  let { segments, audioEl }: { segments: Segment[]; audioEl?: HTMLAudioElement | null } = $props();

  let activeWordIdx = $state(-1);
  let allWords: Array<{ word: string; start: number; end: number; segIdx: number }> = $state([]);

  $effect(() => {
    allWords = [];
    if (!segments) return;
    segments.forEach((seg: Segment, segIdx: number) => {
      if (seg.words) {
        seg.words.forEach((w: Word) => {
          allWords.push({ word: w.word, start: w.start, end: w.end, segIdx });
        });
      }
    });
    activeWordIdx = -1;
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

  $effect(() => {
    if (!audioEl) return;
    const handler = () => {
      const idx = findCurrentWordIdx(audioEl.currentTime);
      if (idx !== activeWordIdx) {
        activeWordIdx = idx;
        if (idx >= 0) {
          const el = document.querySelector(`[data-word-idx="${idx}"]`);
          if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      }
    };
    audioEl.addEventListener("timeupdate", handler);
    return () => audioEl.removeEventListener("timeupdate", handler);
  });

  function seekTo(word: typeof allWords[number]) {
    if (audioEl) audioEl.currentTime = word.start;
  }

  function isSegmentStart(wordIdx: number): boolean {
    if (wordIdx === 0) return true;
    return allWords[wordIdx].segIdx !== allWords[wordIdx - 1].segIdx;
  }
</script>

{#if allWords.length === 0}
  <p class="no-data">No word-level timestamps available.</p>
{:else}
  <div class="transcript-scroll">
    {#each allWords as w, i (i)}
      {#if isSegmentStart(i)}
        {#if i > 0}<br />{/if}
      {/if}
      <span
        class="word"
        class:active={i === activeWordIdx}
        data-word-idx={i}
        onclick={() => seekTo(w)}
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

  .transcript-scroll {
    max-height: 60vh;
    overflow-y: auto;
    padding: 1rem;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    line-height: 2.2;
    font-size: 1.125rem;
  }

  .word {
    cursor: pointer;
    padding: 0.1em 0.05em;
    border-radius: 3px;
    transition: background-color 0.15s ease;
    white-space: pre-wrap;
  }

  .word:hover {
    background: var(--bg-hover);
  }

  .word.active {
    background: rgba(10, 132, 255, 0.2);
    color: var(--accent);
    font-weight: 500;
  }

  @media (max-width: 480px) {
    .transcript-scroll {
      font-size: 1rem;
      line-height: 2;
      padding: 0.75rem;
    }
  }
</style>
