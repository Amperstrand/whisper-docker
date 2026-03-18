(function () {
  "use strict";

  var pollInterval = null;
  var currentJobId = null;
  var HISTORY_KEY = "whisper-jobs";
  var MAX_HISTORY = 20;

  // ---------------------------------------------------------------------------
  // Theme
  // ---------------------------------------------------------------------------

  function initTheme() {
    var saved = localStorage.getItem("theme");
    if (saved === "dark" || (!saved && window.matchMedia("(prefers-color-scheme: dark)").matches)) {
      document.documentElement.setAttribute("data-theme", "dark");
    }
  }

  function toggleTheme() {
    var current = document.documentElement.getAttribute("data-theme");
    var next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
  }

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function $(sel) { return document.querySelector(sel); }
  function $$(sel) { return document.querySelectorAll(sel); }

  function formatBytes(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  function shortId(id) {
    return id.substring(0, 8) + "...";
  }

  function showToast(msg) {
    var t = $("#toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    requestAnimationFrame(function () { t.classList.add("show"); });
    setTimeout(function () {
      t.classList.remove("show");
      setTimeout(function () { t.classList.add("hidden"); }, 300);
    }, 2000);
  }

  function copyText(text) {
    navigator.clipboard.writeText(text).then(function () {
      showToast("Copied to clipboard");
    });
  }

  function downloadFile(content, filename, mime) {
    var blob = new Blob([content], { type: mime });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  // ---------------------------------------------------------------------------
  // Job History (localStorage)
  // ---------------------------------------------------------------------------

  function getHistory() {
    try {
      var data = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
      if (!Array.isArray(data)) return [];
      return data;
    } catch (e) {
      return [];
    }
  }

  function saveHistory(jobs) {
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(jobs.slice(0, MAX_HISTORY)));
    } catch (e) {
      // localStorage full or unavailable — silent
    }
  }

  function addToHistory(id, filename) {
    var jobs = getHistory();
    var entry = { id: id, filename: filename, timestamp: Date.now() };
    var idx = jobs.findIndex(function (j) { return j.id === id; });
    if (idx !== -1) jobs.splice(idx, 1);
    jobs.unshift(entry);
    saveHistory(jobs);
    renderHistory();
  }

  function removeFromHistory(id) {
    var jobs = getHistory().filter(function (j) { return j.id !== id; });
    saveHistory(jobs);
    renderHistory();
  }

  function renderHistory() {
    var section = $("#history-section");
    var container = $("#history-list");
    var jobs = getHistory();

    if (jobs.length === 0) {
      section.classList.add("hidden");
      return;
    }

    section.classList.remove("hidden");
    container.innerHTML = "";

    jobs.forEach(function (job) {
      var item = document.createElement("div");
      item.className = "history-item";
      item.setAttribute("role", "button");
      item.setAttribute("tabindex", "0");
      item.setAttribute("title", job.id);

      var timeStr = new Date(job.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

      item.innerHTML =
        '<span class="history-filename">' + escapeHtml(job.filename) + "</span>" +
        '<span class="history-meta"><code>' + shortId(job.id) + "</code> &middot; " + timeStr + "</span>";

      item.addEventListener("click", function () {
        showStatus({ id: job.id, original_filename: job.filename });
      });

      item.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          showStatus({ id: job.id, original_filename: job.filename });
        }
      });

      container.appendChild(item);
    });
  }

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  // ---------------------------------------------------------------------------
  // Upload
  // ---------------------------------------------------------------------------

  function initUpload() {
    var dropZone = $("#drop-zone");
    var fileInput = $("#file-input");
    var browseLink = $("#browse-link");

    dropZone.addEventListener("click", function (e) {
      if (e.target === browseLink || browseLink.contains(e.target)) {
        fileInput.click();
        return;
      }
      if (e.target === dropZone || dropZone.contains(e.target)) {
        fileInput.click();
      }
    });

    dropZone.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        fileInput.click();
      }
    });

    dropZone.addEventListener("dragover", function (e) {
      e.preventDefault();
      dropZone.classList.add("drag-over");
    });

    dropZone.addEventListener("dragleave", function () {
      dropZone.classList.remove("drag-over");
    });

    dropZone.addEventListener("drop", function (e) {
      e.preventDefault();
      dropZone.classList.remove("drag-over");
      var files = e.dataTransfer.files;
      if (files.length > 0) handleFile(files[0]);
    });

    fileInput.addEventListener("change", function () {
      if (fileInput.files.length > 0) handleFile(fileInput.files[0]);
    });
  }

  function handleFile(file) {
    var allowed = [".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm"];
    var ext = file.name.substring(file.name.lastIndexOf(".")).toLowerCase();

    if (allowed.indexOf(ext) === -1) {
      showToast("Unsupported file type: " + ext);
      return;
    }

    if (file.size > 100 * 1024 * 1024) {
      showToast("File too large (max 100 MB)");
      return;
    }

    $("#file-info").classList.remove("hidden");
    $("#file-name").textContent = file.name;
    $("#file-size").textContent = formatBytes(file.size);

    uploadFile(file);
  }

  function uploadFile(file) {
    var progressDiv = $("#upload-progress");
    var progressFill = $("#progress-fill");
    var progressText = $("#progress-text");

    progressDiv.classList.remove("hidden");
    progressFill.style.width = "0%";
    progressText.textContent = "Uploading...";

    var xhr = new XMLHttpRequest();
    var formData = new FormData();
    formData.append("file", file);

    xhr.upload.addEventListener("progress", function (e) {
      if (e.lengthComputable) {
        var pct = Math.round((e.loaded / e.total) * 100);
        progressFill.style.width = pct + "%";
        progressText.textContent = "Uploading... " + pct + "%";
      }
    });

    xhr.addEventListener("load", function () {
      if (xhr.status === 201) {
        var data = JSON.parse(xhr.responseText);
        progressFill.style.width = "100%";
        progressText.textContent = "Uploaded!";
        addToHistory(data.id, data.original_filename);
        showStatus(data);
      } else if (xhr.status === 200) {
        var data = JSON.parse(xhr.responseText);
        progressFill.style.width = "100%";
        progressText.textContent = "Existing job found!";
        addToHistory(data.id, data.original_filename);
        showStatus(data);
      } else {
        var err = JSON.parse(xhr.responseText);
        progressText.textContent = "Upload failed: " + (err.error || "Unknown error");
        showToast("Upload failed");
      }
    });

    xhr.addEventListener("error", function () {
      progressText.textContent = "Network error";
      showToast("Upload failed — network error");
    });

    xhr.open("POST", "/api/jobs");
    xhr.send(formData);
  }

  // ---------------------------------------------------------------------------
  // Status polling
  // ---------------------------------------------------------------------------

  function showStatus(data) {
    currentJobId = data.id;

    $("#upload-section").classList.add("hidden");
    $("#status-section").classList.remove("hidden");
    $("#results-section").classList.add("hidden");
    $("#error-display").classList.add("hidden");

    $("#job-id").textContent = data.id;
    $("#job-filename").textContent = data.original_filename || "unknown";

    startPolling(data.id);
  }

  function startPolling(jobId) {
    stopPolling();
    updateStatus(jobId);
    pollInterval = setInterval(function () {
      updateStatus(jobId);
    }, 2000);
  }

  function stopPolling() {
    if (pollInterval) {
      clearInterval(pollInterval);
      pollInterval = null;
    }
  }

  function updateStatus(jobId) {
    fetch("/api/jobs/" + jobId)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var job = data.job;
        var statusEl = $("#job-status");
        var spinner = $("#status-spinner");

        statusEl.className = "status-badge " + job.status;
        statusEl.textContent = job.status.charAt(0).toUpperCase() + job.status.slice(1);

        if (job.status === "pending" || job.status === "processing") {
          spinner.classList.remove("hidden");
        } else {
          spinner.classList.add("hidden");
          stopPolling();

          if (job.status === "completed") {
            loadResults(jobId);
          } else if (job.status === "failed") {
            var errEl = $("#error-display");
            errEl.textContent = job.error_message || "Transcription failed";
            errEl.classList.remove("hidden");
          }
        }
      })
      .catch(function () {
        // silent — will retry on next poll
      });
  }

  // ---------------------------------------------------------------------------
  // Results
  // ---------------------------------------------------------------------------

  function loadResults(jobId) {
    fetch("/api/jobs/" + jobId + "/result")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        $("#results-section").classList.remove("hidden");

        if (data.transcript) {
          $("#transcript-text").textContent = data.transcript;
        } else {
          $("#transcript-text").textContent = "(no transcript)";
        }

        if (data.segments) {
          $("#transcript-json").textContent = JSON.stringify(data.segments, null, 2);
        } else {
          $("#transcript-json").textContent = "[]";
        }
      });
  }

  function initTabs() {
    var tabs = $$("#result-tabs .tab");
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        tabs.forEach(function (t) { t.classList.remove("active"); });
        tab.classList.add("active");
        $$(".tab-content").forEach(function (c) { c.classList.remove("active"); });
        $("#tab-" + tab.getAttribute("data-tab")).classList.add("active");
      });
    });
  }

  function initResultActions() {
    $("#copy-id-btn").addEventListener("click", function () {
      copyText($("#job-id").textContent);
    });

    $("#job-id").addEventListener("click", function () {
      copyText($("#job-id").textContent);
    });

    $("#copy-text-btn").addEventListener("click", function () {
      copyText($("#transcript-text").textContent);
    });

    $("#copy-json-btn").addEventListener("click", function () {
      copyText($("#transcript-json").textContent);
    });

    $("#download-text-btn").addEventListener("click", function () {
      downloadFile($("#transcript-text").textContent, "transcript.txt", "text/plain");
    });

    $("#download-json-btn").addEventListener("click", function () {
      downloadFile($("#transcript-json").textContent, "segments.json", "application/json");
    });

    $("#new-job-btn").addEventListener("click", function () {
      stopPolling();
      currentJobId = null;
      $("#upload-section").classList.remove("hidden");
      $("#status-section").classList.add("hidden");
      $("#results-section").classList.add("hidden");
      $("#error-display").classList.add("hidden");
      $("#file-info").classList.add("hidden");
      $("#upload-progress").classList.add("hidden");
      $("#file-input").value = "";
    });

    $("#delete-job-btn").addEventListener("click", function () {
      if (!currentJobId) return;
      if (!confirm("Delete this job and all associated files?")) return;

      fetch("/api/jobs/" + currentJobId, { method: "DELETE" })
        .then(function (r) { return r.json(); })
        .then(function () {
          removeFromHistory(currentJobId);
          showToast("Job deleted");
          $("#new-job-btn").click();
        })
        .catch(function () {
          showToast("Failed to delete job");
        });
    });
  }

  // ---------------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------------

  initTheme();
  initUpload();
  initTabs();
  initResultActions();
  renderHistory();

  // Resume last active job on page load
  (function resumeLastJob() {
    var history = getHistory();
    if (history.length === 0) return;

    // Check the most recent job — if it's pending/processing, resume polling
    var last = history[0];
    fetch("/api/jobs/" + last.id)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.job && (data.job.status === "pending" || data.job.status === "processing")) {
          showStatus({ id: data.job.id, original_filename: data.job.original_filename });
        }
      })
      .catch(function () {
        // job may have been deleted — ignore
      });
  })();

  $("#theme-toggle").addEventListener("click", toggleTheme);
})();
