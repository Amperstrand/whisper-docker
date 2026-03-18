<script lang="ts">
  import type { Segment, Word } from "$lib/types";

  let { segments, audioEl }: { segments: Segment[]; audioEl?: HTMLAudioElement | null } = $props();

  let activeWordIdx = $state(-1);
  let activeSegIdx = $state(-1);
  let wordProgress = $state(0);
  let allWords: Array<{ word: string; start: number; end: number; segIdx: number }> = $state([]);
  let scrollContainer: HTMLDivElement | null = $state(null);

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
    activeSegIdx = -1;
    wordProgress = 0;
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
    let rafId: number;
    let lastScrolledIdx = -1;

    function tick() {
      if (!audioEl) return;
      const time = audioEl.currentTime;
      const idx = findCurrentWordIdx(time);

      if (idx >= 0) {
        const w = allWords[idx];
        const duration = w.end - w.start;
        wordProgress = duration > 0 ? (time - w.start) / duration : 1;
        activeWordIdx = idx;
        activeSegIdx = w.segIdx;
      } else {
        wordProgress = 0;
        activeWordIdx = -1;
        activeSegIdx = -1;
      }

      if (idx >= 0 && idx !== lastScrolledIdx) {
        lastScrolledIdx = idx;
        const el = document.querySelector(`[data-word-idx="${idx}"]`);
        if (el && scrollContainer) {
          const containerRect = scrollContainer.getBoundingClientRect();
          const elRect = el.getBoundingClientRect();
          const elCenter = elRect.top + elRect.height / 2;
          const containerCenter = containerRect.top + containerRect.height / 2;
          const diff = elCenter - containerCenter;
          if (Math.abs(diff) > containerRect.height * 0.3) {
            scrollContainer.scrollBy({ top: diff, behavior: "smooth" });
          }
        }
      }

      rafId = requestAnimationFrame(tick);
    }

    rafId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafId);
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
  <div class="transcript-scroll" bind:this={scrollContainer}>
    {#each allWords as w, i (i)}
      {#if isSegmentStart(i)}
        {#if i > 0}<br />{/if}
      {/if}
      <span
        class="word"
        class:active={i === activeWordIdx}
        class:in-active-segment={w.segIdx === activeSegIdx}
        class:past={i < activeWordIdx}
        class:future={i > activeWordIdx}
        data-word-idx={i}
        onclick={() => seekTo(w)}
        style={i === activeWordIdx ? `--sweep: ${wordProgress * 100}%` : ""}
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

  @media (max-width: 480px) {
    .transcript-scroll {
      font-size: 1rem;
      line-height: 2;
      padding: 0.75rem;
    }
  }
</style>
