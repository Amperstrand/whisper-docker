<script lang="ts">
  import { goto } from "$app/navigation";
  import { onDestroy } from "svelte";
  import TranscriptPlayer from "$lib/components/TranscriptPlayer.svelte";
  import type { Segment } from "$lib/types";

  type Phase = "idle" | "playing" | "uploading" | "transcribing" | "transcribed" | "error";

  const ALLOWED = [".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm"];
  const MAX_SIZE = 100 * 1024 * 1024;
  const HISTORY_KEY = "whisper-jobs";
  const MAX_HISTORY = 20;

  let phase: Phase = $state("idle");
  let file: File | null = $state(null);
  let blobUrl: string | null = $state(null);
  let audioEl: HTMLAudioElement | null = $state(null);
  let dragOver = $state(false);
  let progress = $state(0);
  let error = $state("");
  let jobId: string | null = $state(null);
  let jobStatus = $state("");
  let segments: Segment[] | null = $state(null);
  let canPlay = $state(true);
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  function getExt(name: string): string {
    const i = name.lastIndexOf(".");
    return i === -1 ? "" : name.slice(i).toLowerCase();
  }

  function formatBytes(bytes: number): string {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  function getHistory(): Array<{ id: string; filename: string; timestamp: number }> {
    try {
      const data = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
      return Array.isArray(data) ? data : [];
    } catch {
      return [];
    }
  }

  function saveHistory(jobs: Array<{ id: string; filename: string; timestamp: number }>) {
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(jobs.slice(0, MAX_HISTORY)));
    } catch {
      // silent
    }
  }

  function reset() {
    stopPolling();
    if (blobUrl) URL.revokeObjectURL(blobUrl);
    blobUrl = null;
    file = null;
    audioEl = null;
    phase = "idle";
    progress = 0;
    error = "";
    jobId = null;
    jobStatus = "";
    segments = null;
    canPlay = true;
    const input = document.getElementById("file-input") as HTMLInputElement | null;
    if (input) input.value = "";
  }

  function handleFile(f: File) {
    const ext = getExt(f.name);
    if (!ALLOWED.includes(ext)) {
      error = `Unsupported file type: ${ext}`;
      return;
    }
    if (f.size > MAX_SIZE) {
      error = "File too large (max 100 MB)";
      return;
    }
    error = "";
    file = f;
    if (blobUrl) URL.revokeObjectURL(blobUrl);
    blobUrl = URL.createObjectURL(f);
    phase = "playing";

    requestAnimationFrame(() => {
      if (audioEl) {
        canPlay = audioEl.canPlayType(f.type) !== "" && audioEl.canPlayType(`audio/${ext.slice(1)}`) !== "";
      }
    });
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    dragOver = false;
    if (e.dataTransfer?.files.length) handleFile(e.dataTransfer.files[0]);
  }

  function onInputChange(e: Event) {
    const input = e.target as HTMLInputElement;
    if (input.files?.length) handleFile(input.files[0]);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function startTranscription() {
    if (!file || phase === "uploading" || phase === "transcribing") return;

    phase = "uploading";
    progress = 0;
    error = "";

    const formData = new FormData();
    formData.append("file", file);

    try {
      const xhr = new XMLHttpRequest();

      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) progress = Math.round((e.loaded / e.total) * 100);
      });

      const id = await new Promise<string>((resolve, reject) => {
        xhr.addEventListener("load", () => {
          if (xhr.status === 201 || xhr.status === 200) {
            resolve(JSON.parse(xhr.responseText).id);
          } else {
            reject(new Error(JSON.parse(xhr.responseText).error || "Upload failed"));
          }
        });
        xhr.addEventListener("error", () => reject(new Error("Network error")));
        xhr.open("POST", "/api/jobs");
        xhr.send(formData);
      });

      jobId = id;

      const history = getHistory();
      const entry = { id, filename: file!.name, timestamp: Date.now() };
      const idx = history.findIndex((j) => j.id === id);
      if (idx !== -1) history.splice(idx, 1);
      history.unshift(entry);
      saveHistory(history);
      window.dispatchEvent(new CustomEvent("whisper-history-update"));

      phase = "transcribing";
      jobStatus = "pending";
      startPolling(id);
    } catch (e) {
      error = e instanceof Error ? e.message : "Upload failed";
      phase = "playing";
    }
  }

  function startPolling(id: string) {
    stopPolling();
    pollTimer = setInterval(async () => {
      try {
        const res = await fetch(`/api/jobs/${id}`);
        if (!res.ok) return;
        const data = await res.json();
        const job = data.job;
        jobStatus = job.status;

        if (job.status === "completed") {
          stopPolling();
          fetchResult(id);
        } else if (job.status === "failed") {
          stopPolling();
          error = job.error_message || "Transcription failed";
          phase = "error";
        }
      } catch {
        // silent
      }
    }, 2000);
  }

  async function fetchResult(id: string) {
    try {
      const res = await fetch(`/api/jobs/${id}/result`);
      if (!res.ok) return;
      const data = await res.json();
      segments = (data.segments as Segment[]) || null;
      phase = "transcribed";
    } catch {
      phase = "error";
      error = "Failed to load results";
    }
  }

  onDestroy(() => {
    stopPolling();
    if (blobUrl) URL.revokeObjectURL(blobUrl);
  });
</script>

{#if phase === "idle"}
  <section class="drop-section">
    <div
      class="drop-zone"
      class:drag-over={dragOver}
      role="button"
      tabindex="0"
      ondragover={(e) => { e.preventDefault(); dragOver = true; }}
      ondragleave={() => (dragOver = false)}
      ondrop={onDrop}
      onclick={() => document.getElementById("file-input")?.click()}
      onkeydown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          document.getElementById("file-input")?.click();
        }
      }}
    >
      <div class="drop-content">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path d="M9 18V5l12-2v13"/>
          <circle cx="6" cy="18" r="3"/>
          <circle cx="18" cy="16" r="3"/>
        </svg>
        <p>Drop an audio file here or <span class="browse-link">browse</span></p>
        <p class="drop-hint">WAV, MP3, M4A, FLAC, OGG, WEBM — max 100 MB</p>
        <p class="drop-hint">Files stay in your browser. Nothing uploaded until you choose.</p>
      </div>
    </div>

    <input
      id="file-input"
      type="file"
      accept=".wav,.mp3,.m4a,.flac,.ogg,.webm"
      class="hidden"
      onchange={onInputChange}
    />

    {#if error}
      <p class="error-text">{error}</p>
    {/if}
  </section>
{:else}
  <section class="player-section">
    <div class="file-bar" role="button" tabindex="0" onclick={reset} onkeydown={(e) => { if (e.key === "Enter") reset(); }}>
      <span class="file-name">{file?.name}</span>
      <span class="file-meta">{file ? formatBytes(file.size) : ""}</span>
    </div>

    {#if phase === "playing"}
      <div class="actions">
        <button class="btn-primary" onclick={startTranscription}>
          Transcribe
        </button>
      </div>
    {/if}

    {#if blobUrl}
      <div class="audio-container">
        <!-- svelte-ignore a11y_media_has_caption -->
        <audio
          bind:this={audioEl}
          src={blobUrl}
          controls
          autoplay
        ></audio>
      </div>
    {/if}

    {#if !canPlay}
      <div class="notice">
        <p>Your browser may not support this format. You can still request transcription.</p>
      </div>
    {/if}

    {#if phase === "uploading"}
      <div class="upload-progress">
        <div class="progress-bar">
          <div class="progress-fill" style="width: {progress}%"></div>
        </div>
        <span class="progress-text">Uploading... {progress}%</span>
      </div>
    {/if}

    {#if phase === "transcribing"}
      <div class="transcribing-status">
        <div class="status-line">
          <span class="status-badge {jobStatus}">
            {#if jobStatus === "processing"}<span class="spinner"></span>{/if}
            {jobStatus === "pending" ? "Queued" : jobStatus === "processing" ? "Transcribing..." : jobStatus}
          </span>
        </div>
        <p class="status-hint">Your file is being transcribed. The audio keeps playing.</p>
      </div>
    {/if}

    {#if phase === "transcribed" && segments && audioEl}
      <TranscriptPlayer {segments} {audioEl} />
    {/if}

    {#if phase === "error"}
      <div class="error-display">
        <p>{error}</p>
        <button class="btn-secondary" onclick={reset}>Try Again</button>
      </div>
    {/if}

    {#if phase === "transcribed" || phase === "error"}
      <div class="actions">
        <button class="btn-secondary" onclick={reset}>New File</button>
      </div>
    {/if}
  </section>
{/if}

<style>
  .drop-section {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 50vh;
  }

  .drop-zone {
    width: 100%;
    max-width: 500px;
    border: 2px dashed var(--border);
    border-radius: 16px;
    padding: 3rem 2rem;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.15s, background-color 0.15s;
  }

  .drop-zone:hover,
  .drop-zone.drag-over {
    border-color: var(--accent);
    background-color: var(--bg-hover);
  }

  .drop-content {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.75rem;
    color: var(--text-muted);
  }

  .drop-content svg {
    opacity: 0.4;
  }

  .drop-content p {
    margin: 0;
  }

  .browse-link {
    color: var(--accent);
    text-decoration: underline;
    cursor: pointer;
  }

  .drop-hint {
    font-size: 0.8rem;
    color: var(--text-muted);
    opacity: 0.7;
  }

  .hidden {
    display: none;
  }

  .player-section {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .file-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.5rem 0.75rem;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    cursor: pointer;
    transition: background-color 0.1s;
  }

  .file-bar:hover {
    background: var(--bg-hover);
  }

  .file-name {
    font-size: 0.875rem;
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .file-meta {
    font-size: 0.75rem;
    color: var(--text-muted);
    white-space: nowrap;
    margin-left: 0.5rem;
  }

  .audio-container audio {
    width: 100%;
    border-radius: 8px;
  }

  .notice {
    background: var(--bg-hover);
    border-radius: 8px;
    padding: 0.75rem;
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  .notice p {
    margin: 0;
  }

  .actions {
    display: flex;
    gap: 0.5rem;
  }

  .btn-primary {
    padding: 0.625rem 1.5rem;
    background: var(--accent);
    border: none;
    border-radius: 8px;
    color: #fff;
    font-size: 0.875rem;
    font-weight: 500;
    cursor: pointer;
    transition: background-color 0.1s;
  }

  .btn-primary:hover {
    background: var(--accent-hover);
  }

  .btn-secondary {
    padding: 0.5rem 1rem;
    background: var(--bg-hover);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 0.875rem;
    cursor: pointer;
    transition: background-color 0.1s;
  }

  .btn-secondary:hover {
    background: var(--border);
  }

  .upload-progress {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.5rem;
  }

  .progress-bar {
    width: 100%;
    height: 6px;
    background: var(--border);
    border-radius: 3px;
    overflow: hidden;
  }

  .progress-fill {
    height: 100%;
    background: var(--accent);
    border-radius: 3px;
    transition: width 0.2s;
  }

  .progress-text {
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  .transcribing-status {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.5rem;
    padding: 1rem;
  }

  .status-line {
    display: flex;
    justify-content: center;
  }

  .status-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.2rem 0.6rem;
    border-radius: 9999px;
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.025em;
    background: rgba(90, 200, 250, 0.15);
    color: var(--info);
  }

  .status-badge.processing {
    background: rgba(90, 200, 250, 0.15);
    color: var(--info);
  }

  .status-badge.pending {
    background: rgba(255, 159, 10, 0.15);
    color: var(--warning);
  }

  .spinner {
    display: inline-block;
    width: 10px;
    height: 10px;
    border: 2px solid currentColor;
    border-right-color: transparent;
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .status-hint {
    font-size: 0.8rem;
    color: var(--text-muted);
    margin: 0;
  }

  .error-display {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.75rem;
    padding: 1rem;
    background: rgba(255, 59, 48, 0.1);
    border: 1px solid var(--danger);
    border-radius: 8px;
  }

  .error-display p {
    margin: 0;
    color: var(--danger);
    font-size: 0.875rem;
    text-align: center;
  }

  .error-text {
    margin-top: 0.75rem;
    color: var(--danger);
    font-size: 0.875rem;
  }
</style>
