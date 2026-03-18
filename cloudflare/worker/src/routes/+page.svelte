<script lang="ts">
  import { goto } from "$app/navigation";

  const ALLOWED = [".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm"];
  const MAX_SIZE = 100 * 1024 * 1024;
  const HISTORY_KEY = "whisper-jobs";
  const MAX_HISTORY = 20;

  let file: File | null = $state(null);
  let uploading = $state(false);
  let progress = $state(0);
  let error = $state("");
  let dragOver = $state(false);

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
      // localStorage full — silent
    }
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
    uploadFile(f);
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

  async function uploadFile(f: File) {
    uploading = true;
    progress = 0;

    const formData = new FormData();
    formData.append("file", f);

    try {
      const xhr = new XMLHttpRequest();

      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) progress = Math.round((e.loaded / e.total) * 100);
      });

      const jobId = await new Promise<string>((resolve, reject) => {
        xhr.addEventListener("load", () => {
          if (xhr.status === 201 || xhr.status === 200) {
            const d = JSON.parse(xhr.responseText);
            resolve(d.id);
          } else {
            reject(new Error(JSON.parse(xhr.responseText).error || "Upload failed"));
          }
        });
        xhr.addEventListener("error", () => reject(new Error("Network error")));
        xhr.open("POST", "/api/jobs");
        xhr.send(formData);
      });

      const history = getHistory();
      const entry = { id: jobId, filename: f.name, timestamp: Date.now() };
      const idx = history.findIndex((j) => j.id === jobId);
      if (idx !== -1) history.splice(idx, 1);
      history.unshift(entry);
      saveHistory(history);
      window.dispatchEvent(new CustomEvent("whisper-history-update"));

      goto(`/jobs/${jobId}`);
    } catch (e) {
      error = e instanceof Error ? e.message : "Upload failed";
    } finally {
      uploading = false;
      file = null;
    }
  }
</script>

<section>
  <h2>Upload Audio</h2>

  <div
    class="drop-zone"
    class:drag-over={dragOver}
    class:uploading={uploading}
    role="button"
    tabindex="0"
    ondragover={(e) => { e.preventDefault(); dragOver = true; }}
    ondragleave={() => (dragOver = false)}
    ondrop={onDrop}
    onclick={() => !uploading && document.getElementById("file-input")?.click()}
    onkeydown={(e) => {
      if ((e.key === "Enter" || e.key === " ") && !uploading) {
        e.preventDefault();
        document.getElementById("file-input")?.click();
      }
    }}
  >
    {#if uploading}
      <div class="upload-progress">
        <div class="progress-bar">
          <div class="progress-fill" style="width: {progress}%"></div>
        </div>
        <span class="progress-text">Uploading... {progress}%</span>
      </div>
    {:else if file}
      <p>{file.name} ({formatBytes(file.size)})</p>
    {:else}
      <div class="drop-content">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="17 8 12 3 7 8"/>
          <line x1="12" y1="3" x2="12" y2="15"/>
        </svg>
        <p>Drop an audio file here or <span class="browse-link">browse</span></p>
        <p class="drop-hint">WAV, MP3, M4A, FLAC, OGG, WEBM — max 100 MB</p>
      </div>
    {/if}
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

<style>
  h2 {
    margin-bottom: 1rem;
  }

  .drop-zone {
    border: 2px dashed var(--border);
    border-radius: 12px;
    padding: 2rem;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.15s, background-color 0.15s;
  }

  .drop-zone:hover,
  .drop-zone.drag-over {
    border-color: var(--accent);
    background-color: var(--bg-hover);
  }

  .drop-zone.uploading {
    cursor: default;
  }

  .drop-content {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.5rem;
    color: var(--text-muted);
  }

  .drop-content svg {
    color: var(--text-muted);
    opacity: 0.5;
  }

  .browse-link {
    color: var(--accent);
    text-decoration: underline;
    cursor: pointer;
  }

  .drop-hint {
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  .upload-progress {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.75rem;
  }

  .progress-bar {
    width: 100%;
    max-width: 400px;
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
    font-size: 0.875rem;
    color: var(--text-muted);
  }

  .error-text {
    margin-top: 0.75rem;
    color: var(--danger);
    font-size: 0.875rem;
  }

  .hidden {
    display: none;
  }
</style>
