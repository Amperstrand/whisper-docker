<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { goto } from "$app/navigation";
  import StatusBadge from "$lib/components/StatusBadge.svelte";
  import type { Job, Segment } from "$lib/types";

  let { params } = $props();

  const HISTORY_KEY = "whisper-jobs";
  const MAX_HISTORY = 20;

  interface HistoryEntry {
    id: string;
    filename: string;
    timestamp: number;
  }

  let job: Job | null = $state(null);
  let transcript = $state<string | null>(null);
  let segments = $state<Segment[] | null>(null);
  let activeTab = $state<"text" | "json">("text");
  let polling = $state(false);
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let deleting = $state(false);
  let toast = $state("");
  let toastTimer: ReturnType<typeof setTimeout> | null = null;

  function getHistory(): HistoryEntry[] {
    try {
      const data = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
      return Array.isArray(data) ? data : [];
    } catch {
      return [];
    }
  }

  function saveHistory(jobs: HistoryEntry[]) {
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(jobs.slice(0, MAX_HISTORY)));
    } catch {
      // silent
    }
  }

  function showToast(msg: string) {
    toast = msg;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toast = ""), 2000);
  }

  function copyText(text: string) {
    navigator.clipboard.writeText(text).then(() => showToast("Copied to clipboard"));
  }

  function downloadFile(content: string, filename: string, mime: string) {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  function formatBytes(bytes: number): string {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  async function fetchJob() {
    try {
      const res = await fetch(`/api/jobs/${params.id}`);
      if (!res.ok) return;
      const data = await res.json();
      const j = data.job;
      job = j;

      if (j.status === "completed") {
        stopPolling();
        fetchResult();
      } else if (j.status === "failed") {
        stopPolling();
      }
    } catch {
      // silent — retry on next poll
    }
  }

  async function fetchResult() {
    try {
      const res = await fetch(`/api/jobs/${params.id}/result`);
      if (!res.ok) return;
      const data = await res.json();
      transcript = data.transcript || null;
      segments = (data.segments as Segment[]) || null;
    } catch {
      // silent
    }
  }

  function startPolling() {
    stopPolling();
    polling = true;
    pollTimer = setInterval(fetchJob, 2000);
  }

  function stopPolling() {
    polling = false;
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function deleteJob() {
    if (!job || deleting) return;
    if (!confirm("Delete this job and all associated files?")) return;
    const jobId = job.id;

    deleting = true;
    try {
      const res = await fetch(`/api/jobs/${jobId}`, { method: "DELETE" });
      if (res.ok) {
        const history = getHistory().filter((j) => j.id !== jobId);
        saveHistory(history);
        window.dispatchEvent(new CustomEvent("whisper-history-update"));
        showToast("Job deleted");
        goto("/");
      } else {
        showToast("Failed to delete job");
      }
    } catch {
      showToast("Failed to delete job");
    } finally {
      deleting = false;
    }
  }

  const hasWordTimestamps = $derived(
    segments && segments.length > 0 && segments.some((s) => s.words && s.words.length > 0),
  );

  onMount(async () => {
    await fetchJob();
    if (job && (job.status === "pending" || job.status === "processing")) {
      startPolling();
    } else if (job?.status === "completed") {
      await fetchResult();
    }
  });

  onDestroy(() => stopPolling());
</script>

{#if job}
  <section>
    <div class="job-header">
      <div>
        <h2>Status</h2>
        <StatusBadge status={job.status} />
      </div>
    </div>

    <div class="job-meta">
      <div class="meta-row">
        <span class="meta-label">Filename</span>
        <span class="meta-value">{job.original_filename}</span>
      </div>
      <div class="meta-row">
        <span class="meta-label">Size</span>
        <span class="meta-value">{formatBytes(job.file_size)}</span>
      </div>
      <div class="meta-row">
        <span class="meta-label">Job ID</span>
        <button class="meta-value id-btn" onclick={() => copyText(job!.id)} title="Click to copy">
          {job.id}
        </button>
      </div>
    </div>

    {#if job.status === "failed" && job.error_message}
      <div class="error-display">
        <p>{job.error_message}</p>
      </div>
    {/if}

    {#if job.status === "completed" && transcript !== null}
      <div class="results-section">
        <h3>Results</h3>

        <div class="result-tabs">
          <button class="tab" class:active={activeTab === "text"} onclick={() => activeTab = "text"}>
            Plain Text
          </button>
          <button class="tab" class:active={activeTab === "json"} onclick={() => activeTab = "json"}>
            JSON
          </button>
        </div>

        <div class="tab-content" class:active={activeTab === "text"}>
          <pre class="result-text">{transcript}</pre>
        </div>
        <div class="tab-content" class:active={activeTab === "json"}>
          <pre class="result-json">{JSON.stringify(segments, null, 2)}</pre>
        </div>

        <div class="result-actions">
          <button onclick={() => copyText(activeTab === "text" ? transcript! : JSON.stringify(segments, null, 2))}>
            Copy
          </button>
          {#if activeTab === "text"}
            <button onclick={() => downloadFile(transcript!, "transcript.txt", "text/plain")}>
              Download .txt
            </button>
          {:else}
            <button onclick={() => downloadFile(JSON.stringify(segments, null, 2), "segments.json", "application/json")}>
              Download .json
            </button>
          {/if}

          {#if hasWordTimestamps}
            <a href="/jobs/{job.id}/player" class="player-link">Karaoke Player</a>
          {/if}
        </div>
      </div>
    {/if}

    <div class="actions">
      <button class="btn-secondary" onclick={() => goto("/")}>New Job</button>
      <button class="btn-danger" onclick={deleteJob} disabled={deleting}>
        {deleting ? "Deleting..." : "Delete Job"}
      </button>
    </div>
  </section>
{:else}
  <section>
    <p>Loading...</p>
  </section>
{/if}

{#if toast}
  <div class="toast">{toast}</div>
{/if}

<style>
  h2 {
    margin-bottom: 0.5rem;
  }

  h3 {
    margin-bottom: 0.75rem;
  }

  .job-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 1rem;
  }

  .job-meta {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.75rem;
    margin-bottom: 1rem;
  }

  .meta-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.25rem 0;
  }

  .meta-label {
    color: var(--text-muted);
    font-size: 0.8rem;
  }

  .meta-value {
    font-size: 0.875rem;
    font-weight: 500;
  }

  .id-btn {
    background: none;
    border: none;
    color: var(--text);
    font-family: monospace;
    font-size: 0.75rem;
    cursor: pointer;
    padding: 0;
    opacity: 0.7;
    transition: opacity 0.1s;
  }

  .id-btn:hover {
    opacity: 1;
  }

  .error-display {
    background: rgba(255, 59, 48, 0.1);
    border: 1px solid var(--danger);
    border-radius: 8px;
    padding: 0.75rem;
    margin-bottom: 1rem;
    color: var(--danger);
    font-size: 0.875rem;
  }

  .results-section {
    margin-bottom: 1.5rem;
  }

  .result-tabs {
    display: flex;
    gap: 0;
    border-bottom: 1px solid var(--border);
    margin-bottom: 1rem;
  }

  .tab {
    padding: 0.5rem 1rem;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-muted);
    cursor: pointer;
    font-size: 0.875rem;
    font-weight: 500;
    transition: color 0.15s, border-color 0.15s;
  }

  .tab:hover {
    color: var(--text);
  }

  .tab.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }

  .tab-content {
    display: none;
  }

  .tab-content.active {
    display: block;
  }

  .result-text,
  .result-json {
    background: var(--code-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1rem;
    font-size: 0.875rem;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 400px;
    overflow-y: auto;
    margin: 0;
  }

  .result-json {
    font-family: monospace;
    font-size: 0.8rem;
  }

  .result-actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.75rem;
    flex-wrap: wrap;
    align-items: center;
  }

  .result-actions button {
    padding: 0.375rem 0.75rem;
    background: var(--bg-hover);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    font-size: 0.8rem;
    cursor: pointer;
    transition: background-color 0.1s;
  }

  .result-actions button:hover {
    background: var(--border);
  }

  .player-link {
    padding: 0.375rem 0.75rem;
    background: var(--accent);
    border-radius: 6px;
    color: #fff;
    font-size: 0.8rem;
    text-decoration: none;
    font-weight: 500;
    transition: background-color 0.1s;
  }

  .player-link:hover {
    background: var(--accent-hover);
  }

  .actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 1rem;
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

  .btn-danger {
    padding: 0.5rem 1rem;
    background: none;
    border: 1px solid var(--danger);
    border-radius: 8px;
    color: var(--danger);
    font-size: 0.875rem;
    cursor: pointer;
    transition: background-color 0.1s;
  }

  .btn-danger:hover:not(:disabled) {
    background: rgba(255, 59, 48, 0.1);
  }

  .btn-danger:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .toast {
    position: fixed;
    bottom: 1.5rem;
    left: 50%;
    transform: translateX(-50%);
    background: var(--text);
    color: var(--bg);
    padding: 0.5rem 1rem;
    border-radius: 8px;
    font-size: 0.875rem;
    animation: toast-in 0.2s ease-out;
    z-index: 100;
  }

  @keyframes toast-in {
    from {
      opacity: 0;
      transform: translateX(-50%) translateY(0.5rem);
    }
    to {
      opacity: 1;
      transform: translateX(-50%) translateY(0);
    }
  }
</style>
