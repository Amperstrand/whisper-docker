<script lang="ts">
  import type { Snippet } from "svelte";
  import ThemeToggle from "$lib/components/ThemeToggle.svelte";

  let { children }: { children: Snippet } = $props();
</script>

<svelte:head>
  <title>Whisper Transcribe</title>
</svelte:head>

<div class="app">
  <header>
    <div class="header-inner">
      <h1>
        <a href="/">Whisper Transcribe</a>
      </h1>
      <ThemeToggle />
    </div>
  </header>

  <main>
    {@render children()}
  </main>

  <footer>
    <p>
      <a href="https://github.com/Amperstrand/whisper-docker" target="_blank" rel="noopener">
        GitHub
      </a>
    </p>
  </footer>
</div>

<style>
  .app {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }

  header {
    border-bottom: 1px solid var(--border);
    padding: 0.75rem 1rem;
  }

  .header-inner {
    max-width: 680px;
    margin: 0 auto;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  h1 {
    font-size: 1.25rem;
    font-weight: 600;
    margin: 0;
  }

  h1 a {
    color: var(--text);
    text-decoration: none;
  }

  main {
    flex: 1;
    max-width: 680px;
    width: 100%;
    margin: 0 auto;
    padding: 2rem 1rem;
  }

  footer {
    border-top: 1px solid var(--border);
    padding: 1rem;
    text-align: center;
  }

  footer a {
    color: var(--text-muted);
    text-decoration: none;
    font-size: 0.875rem;
  }

  footer a:hover {
    color: var(--text);
  }
</style>
