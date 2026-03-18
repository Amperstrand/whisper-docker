<script lang="ts">
  import { goto } from "$app/navigation";
  import { onMount } from "svelte";

  const HISTORY_KEY = "whisper-jobs";
  const MAX_HISTORY = 20;

  interface HistoryEntry {
    id: string;
    filename: string;
    timestamp: number;
  }

  let jobs: HistoryEntry[] = $state([]);

  function getHistory(): HistoryEntry[] {
    try {
      const data = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
      return Array.isArray(data) ? data : [];
    } catch {
      return [];
    }
  }

  function renderHistory() {
    jobs = getHistory();
  }

  function escapeHtml(str: string): string {
    const div = document.createElement("div");
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  function shortId(id: string): string {
    return id.substring(0, 8) + "...";
  }

  function handleClick(job: HistoryEntry) {
    goto(`/jobs/${job.id}`);
  }

  onMount(() => {
    renderHistory();
    const handler = () => renderHistory();
    window.addEventListener("whisper-history-update", handler);
    return () => window.removeEventListener("whisper-history-update", handler);
  });
</script>

{#if jobs.length > 0}
  <section class="history-section">
    <h3>Recent Jobs</h3>
    <div class="history-list">
      {#each jobs as job (job.id)}
        <button
          class="history-item"
          onclick={() => handleClick(job)}
          title={job.id}
        >
          <span class="history-filename">{@html escapeHtml(job.filename)}</span>
          <span class="history-meta">
            <code>{shortId(job.id)}</code>
            &middot;
            {new Date(job.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
          </span>
        </button>
      {/each}
    </div>
  </section>
{/if}

<style>
  .history-section {
    max-width: 680px;
    width: 100%;
    margin: 0 auto;
    padding: 0 1rem 2rem;
  }

  h3 {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 0.5rem;
  }

  .history-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .history-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
    padding: 0.5rem 0.75rem;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    cursor: pointer;
    text-align: left;
    color: var(--text);
    font-size: 0.875rem;
    transition: background-color 0.1s;
  }

  .history-item:hover {
    background: var(--bg-hover);
  }

  .history-filename {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
    min-width: 0;
  }

  .history-meta {
    color: var(--text-muted);
    font-size: 0.75rem;
    white-space: nowrap;
    margin-left: 0.5rem;
  }

  .history-meta code {
    font-family: var(--font-mono, monospace);
    font-size: 0.75rem;
  }

  @media (max-width: 480px) {
    .history-section {
      padding: 0 0.75rem 1.5rem;
    }

    .history-item {
      flex-direction: column;
      align-items: flex-start;
      gap: 0.125rem;
    }

    .history-meta {
      margin-left: 0;
    }
  }
</style>
