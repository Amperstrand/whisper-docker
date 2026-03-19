<script lang="ts">
  import TranscriptPlayer from "$lib/components/TranscriptPlayer.svelte";
  import type { Segment, Analysis } from "$lib/types";

  interface PageData {
    job: {
      id: string;
      status: string;
      original_filename: string;
    } | null;
    segments: Segment[] | null;
    analysis: Analysis | null;
    audioUrl: string | null;
  }

  let { data }: { data: PageData } = $props();

  let audioEl: HTMLAudioElement | null = $state(null);

  const hasWords = $derived(
    data.segments && data.segments.length > 0 && data.segments.some((s) => s.words && s.words.length > 0),
  );
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
{:else if !hasWords}
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
        ></audio>
      </div>
    {:else}
      <div class="audio-container notice">
        <p>Audio file is no longer available (deleted after transcription). Timestamps still work.</p>
      </div>
    {/if}

    <TranscriptPlayer segments={data.segments} audioEl={audioEl} analysis={data.analysis} />
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
</style>
