<script lang="ts">
  import { goto } from "$app/navigation";
  import type { Segment, Word } from "$lib/types";

  interface PageData {
    job: {
      id: string;
      status: string;
      original_filename: string;
    } | null;
    segments: Segment[] | null;
    audioUrl: string | null;
  }

  let { data }: { data: PageData } = $props();

  let audioEl: HTMLAudioElement | null = $state(null);
  let activeWordIdx = $state(-1);
  let allWords: Array<{ word: string; start: number; end: number; segIdx: number }> = $state([]);

  $effect(() => {
    allWords = [];
    if (!data.segments) return;

    data.segments.forEach((seg: Segment, segIdx: number) => {
      if (seg.words) {
        seg.words.forEach((w: Word) => {
          allWords.push({ word: w.word, start: w.start, end: w.end, segIdx });
        });
      }
    });
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

  function onTimeUpdate() {
    if (!audioEl) return;
    const idx = findCurrentWordIdx(audioEl.currentTime);
    if (idx !== activeWordIdx) {
      activeWordIdx = idx;
      if (idx >= 0) {
        const el = document.querySelector(`[data-word-idx="${idx}"]`);
        if (el) {
          el.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      }
    }
  }

  function seekTo(word: typeof allWords[number]) {
    if (audioEl) audioEl.currentTime = word.start;
  }

  function isSegmentStart(wordIdx: number): boolean {
    if (wordIdx === 0) return true;
    return allWords[wordIdx].segIdx !== allWords[wordIdx - 1].segIdx;
  }
</script>

{#if !data.job}
  <section>
    <p>Job not found.</p>
  </section>
{:else if data.job.status !== "completed"}
  <section>
    <p>This job is not yet completed. <a href="/jobs/{data.job.id}">Go back to status.</a></p>
  </section>
{:else if !data.segments || data.segments.length === 0}
  <section>
    <p>No transcription data available.</p>
  </section>
{:else if !data.segments.some((s: Segment) => s.words && s.words.length > 0)}
  <section>
    <p>Word-level timestamps are not available for this transcription.</p>
    <p><a href="/jobs/{data.job.id}">Back to Results</a></p>
  </section>
{:else}
  <section>
    <div class="player-header">
      <h2>Karaoke Player</h2>
      <a href="/jobs/{data.job.id}" class="back-link">Back to Results</a>
    </div>

    {#if data.audioUrl}
      <div class="audio-container">
        <!-- svelte-ignore a11y_media_has_caption -->
        <audio
          bind:this={audioEl}
          src={data.audioUrl}
          controls
          ontimeupdate={onTimeUpdate}
        ></audio>
      </div>
    {:else}
      <div class="audio-container notice">
        <p>Audio file is no longer available (deleted after transcription). Timestamps still work.</p>
      </div>
    {/if}

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
  </section>
{/if}

<style>
  .player-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
  }

  h2 {
    margin: 0;
  }

  .back-link {
    font-size: 0.875rem;
  }

  .audio-container {
    margin-bottom: 1.5rem;
  }

  .audio-container audio {
    width: 100%;
    border-radius: 8px;
  }

  .notice {
    background: var(--bg-hover);
    border-radius: 8px;
    padding: 0.75rem;
    font-size: 0.875rem;
    color: var(--text-muted);
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
