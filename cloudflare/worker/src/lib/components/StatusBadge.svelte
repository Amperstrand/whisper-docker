<script lang="ts">
  let { status }: { status: string } = $props();

  const labels: Record<string, string> = {
    pending: "Pending",
    processing: "Processing",
    completed: "Completed",
    failed: "Failed",
  };
</script>

<span class="status-badge {status}">
  {#if status === "processing"}
    <span class="spinner"></span>
  {/if}
  {labels[status] || status}
</span>

<style>
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
  }

  .status-badge.pending {
    background: rgba(255, 159, 10, 0.15);
    color: var(--warning);
  }

  .status-badge.processing {
    background: rgba(90, 200, 250, 0.15);
    color: var(--info);
  }

  .status-badge.completed {
    background: rgba(52, 199, 89, 0.15);
    color: var(--success);
  }

  .status-badge.failed {
    background: rgba(255, 59, 48, 0.15);
    color: var(--danger);
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
    to {
      transform: rotate(360deg);
    }
  }
</style>
