<script lang="ts">
  import { onMount } from "svelte";

  let dark = $state(false);

  onMount(() => {
    const stored = localStorage.getItem("whisper-theme");
    if (stored === "dark") {
      dark = true;
    } else if (stored === "light") {
      dark = false;
    } else {
      dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    }
    applyTheme();
  });

  function toggle() {
    dark = !dark;
    localStorage.setItem("whisper-theme", dark ? "dark" : "light");
    applyTheme();
  }

  function applyTheme() {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  }
</script>

<button class="theme-toggle" onclick={toggle} aria-label="Toggle theme" title="Toggle theme">
  {#if dark}
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <circle cx="12" cy="12" r="5"/>
      <line x1="12" y1="1" x2="12" y2="3"/>
      <line x1="12" y1="21" x2="12" y2="23"/>
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>
      <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
      <line x1="1" y1="12" x2="3" y2="12"/>
      <line x1="21" y1="12" x2="23" y2="12"/>
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>
      <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
    </svg>
  {:else}
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
    </svg>
  {/if}
</button>

<style>
  .theme-toggle {
    background: none;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px;
    cursor: pointer;
    color: var(--text);
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background-color 0.15s;
  }

  .theme-toggle:hover {
    background-color: var(--bg-hover);
  }
</style>
