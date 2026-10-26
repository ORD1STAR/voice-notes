(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var views = { start: $("view-start"), progress: $("view-progress"), error: $("view-error"), result: $("view-result") };
  var focusTargets = { start: "start-title", progress: "progress-title", error: "error-title", result: "result-title" };

  var health = null;
  var maxBytes = 200 * 1024 * 1024;
  var pollTimer = null;
  var pollFailures = 0;
  var currentId = null;
  var selected = null;
  var uploading = false;

  var STEP_ORDER = ["upload", "converting", "transcribing", "structuring"];
  var AUDIO_EXT = /\.(mp3|m4a|wav|ogg|oga|opus|flac|aac|webm|mp4|m4v|mov|mkv|amr|3gp|wma|aiff?)$/i;

  /* ---------- helpers ---------- */

  function show(name, focus) {
    Object.keys(views).forEach(function (k) { views[k].hidden = k !== name; });
    if (focus !== false) {
      var el = $(focusTargets[name]);
      if (el) { el.focus({ preventScroll: true }); window.scrollTo(0, 0); }
    }
  }

  function fmtSize(bytes) {
    if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
    return Math.max(1, Math.round(bytes / 1024)) + " KB";
  }

  function fmtDuration(sec) {
    if (!sec) return "";
    var s = Math.round(sec), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    if (h) return h + " h " + (m < 10 ? "0" : "") + m + " min";
    if (m) return m + " min " + (r < 10 ? "0" : "") + r + " s";
    return r + " s";
  }

  function fmtDate(ts) {
    return new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
  }

  function el(tag, text, cls) {
    var e = document.createElement(tag);
    if (text != null) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }

  function showError(message) {
    stopPolling();
    $("error-message").textContent = message;
    show("error");
  }

  function api(path, opts) {
    return fetch(path, opts).then(function (res) {
      if (res.ok) return res;
      return res.json().catch(function () { return {}; }).then(function (body) {
        var err = new Error(body.error || "The request failed (" + res.status + ").");
        err.status = res.status;
        throw err;
      });
    });
  }

  /* ---------- health and engine line ---------- */

  function loadHealth() {
    return api("/api/health").then(function (r) { return r.json(); }).then(function (h) {
      health = h;
      maxBytes = h.max_upload_mb * 1024 * 1024;
      $("max-mb").textContent = h.max_upload_mb;
      var o = h.ollama || {};
      var structuring = o.available ? "local model " + o.model : "rule-based structuring";
      $("engine-line").textContent = "Transcription: Groq Whisper. Structuring: " + structuring + ".";

      var problems = [];
      if (!h.groq_key) problems.push("GROQ_API_KEY is not set on the server, so recordings cannot be transcribed yet.");
      if (!h.ffmpeg) problems.push("ffmpeg is not installed on the server, so audio cannot be prepared.");
      var box = $("setup-notice");
      box.hidden = problems.length === 0;
      box.textContent = problems.join(" ");
      updateGo();
    }).catch(function () {
      $("engine-line").textContent = "The server did not answer.";
    });
  }

  function ready() { return !health || (health.groq_key && health.ffmpeg); }

  function updateGo() {
    $("go").disabled = !selected || uploading || !ready();
  }

  /* ---------- file selection ---------- */

  function validate(file) {
    if (!file) return "Choose a file first.";
    if (!(file.type.indexOf("audio/") === 0 || file.type.indexOf("video/") === 0 || AUDIO_EXT.test(file.name))) {
      return "This does not look like an audio or video file.";
    }
    if (file.size === 0) return "This file is empty.";
    if (file.size > maxBytes) return "This file is " + fmtSize(file.size) + ". The limit is " + Math.round(maxBytes / 1048576) + " MB.";
    return "";
  }

  function setSelected(file) {
    var msg = file ? validate(file) : "";
    var err = $("file-error");
    err.hidden = !msg;
    err.textContent = msg;
    if (msg) { selected = null; } else { selected = file || null; }
    $("picked").hidden = !selected;
    if (selected) {
      $("picked-name").textContent = selected.name;
      $("picked-size").textContent = fmtSize(selected.size);
      $("go").focus();
    }
    if (!selected) $("file").value = "";
    updateGo();
  }

  $("file").addEventListener("change", function (e) { setSelected(e.target.files[0]); });
  $("clear").addEventListener("click", function () { setSelected(null); $("file").focus(); });

  var drop = $("drop");
  ["dragenter", "dragover"].forEach(function (n) {
    drop.addEventListener(n, function (e) { e.preventDefault(); drop.classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (n) {
    drop.addEventListener(n, function (e) { e.preventDefault(); drop.classList.remove("over"); });
  });
  drop.addEventListener("drop", function (e) {
    var f = e.dataTransfer && e.dataTransfer.files[0];
    if (f) setSelected(f);
  });
  window.addEventListener("dragover", function (e) { e.preventDefault(); });
  window.addEventListener("drop", function (e) { e.preventDefault(); });

  /* ---------- upload ---------- */

  $("upload-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var msg = validate(selected);
    if (msg) { setSelected(null); $("file-error").hidden = false; $("file-error").textContent = msg; return; }
    startUpload(selected);
  });

  function startUpload(file) {
    uploading = true;
    updateGo();
    stopPolling();
    $("progress-file").textContent = file.name + " (" + fmtSize(file.size) + ")";
    setSteps("upload", "Uploading");
    setBar(0, false);
    $("progress-detail").textContent = "Uploading 0%";
    show("progress");

    var form = new FormData();
    form.append("file", file);
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/notes");
    xhr.upload.onprogress = function (ev) {
      if (!ev.lengthComputable) return;
      var pct = Math.round((ev.loaded / ev.total) * 100);
      setBar(pct * 0.1, false);
      $("progress-detail").textContent = pct < 100 ? "Uploading " + pct + "%" : "Upload finished, starting";
      setStepState("upload", pct < 100 ? "Uploading " + pct + "%" : "Done");
    };
    xhr.onload = function () {
      uploading = false;
      var body = {};
      try { body = JSON.parse(xhr.responseText); } catch (x) { /* not JSON */ }
      if (xhr.status === 202 && body.id) {
        setSelected(null);
        location.hash = "#/note/" + body.id;
      } else {
        showError(body.error || "The upload failed (" + xhr.status + ").");
      }
      updateGo();
    };
    xhr.onerror = function () {
      uploading = false;
      showError("The upload was interrupted. Check your connection and try again.");
      updateGo();
    };
    xhr.send(form);
  }

  /* ---------- progress view ---------- */

  function setBar(pct, indeterminate) {
    var bar = $("bar");
    bar.classList.toggle("indeterminate", !!indeterminate);
    $("bar-fill").style.width = indeterminate ? "" : Math.max(0, Math.min(100, pct)) + "%";
    bar.setAttribute("aria-valuenow", String(Math.round(pct)));
  }

  function setStepState(step, text) {
    var li = document.querySelector('#steps li[data-step="' + step + '"] .step-state');
    if (li) li.textContent = text;
  }

  function setSteps(active, activeText, failed) {
    var idx = STEP_ORDER.indexOf(active);
    STEP_ORDER.forEach(function (s, i) {
      var li = document.querySelector('#steps li[data-step="' + s + '"]');
      li.className = i < idx ? "done" : i === idx ? (failed ? "failed" : "active") : "";
      setStepState(s, i < idx ? "Done" : i === idx ? activeText : "Waiting");
    });
  }

  function renderProgress(job) {
    $("progress-file").textContent = job.filename;
    var stage = job.stage === "queued" ? "converting" : job.stage;
    setSteps(stage, job.stage === "queued" ? "Queued" : "In progress");
    setBar(job.progress * 100, job.stage === "queued");
    $("progress-detail").textContent = job.detail || "Working";
    if (views.progress.hidden) show("progress");
  }

  /* ---------- result view ---------- */

  var currentJob = null;

  function renderResult(job) {
    currentJob = job;
    var n = job.notes;
    $("result-title").textContent = n.title;
    var engine = job.engine && job.engine.indexOf("ollama:") === 0
      ? "Structured with " + job.engine.slice(7)
      : "Structured with rule-based fallback";
    var bits = [job.filename, fmtDuration(job.duration), fmtDate(job.created), engine].filter(Boolean);
    $("result-meta").textContent = bits.join("  /  ");
    $("summary").textContent = n.summary;
    $("summary-block").hidden = !n.summary;

    var host = $("sections");
    host.textContent = "";
    n.sections.forEach(function (s) {
      var sec = el("section");
      sec.appendChild(el("h3", s.heading));
      var ul = el("ul");
      s.bullets.forEach(function (b) { ul.appendChild(el("li", b)); });
      sec.appendChild(ul);
      host.appendChild(sec);
    });
    $("transcript").textContent = job.transcript || "";
    $("copy-status").textContent = "";
    selectTab("notes", false);
    updateExportLinks();
    show("result");
    document.title = n.title + " - Voice Notes";
  }

  function updateExportLinks() {
    if (!currentId) return;
    var t = $("with-transcript").checked ? "&transcript=true" : "";
    ["pdf", "txt", "md"].forEach(function (f) {
      $("exp-" + f).href = "/api/notes/" + currentId + "/export?format=" + f + t;
    });
  }
  $("with-transcript").addEventListener("change", updateExportLinks);

  function notesAsMarkdown(job) {
    var out = ["# " + job.notes.title, ""];
    if (job.notes.summary) out.push("## Summary", "", job.notes.summary, "");
    job.notes.sections.forEach(function (s) {
      out.push("## " + s.heading, "");
      s.bullets.forEach(function (b) { out.push("- " + b); });
      out.push("");
    });
    return out.join("\n");
  }

  $("copy").addEventListener("click", function () {
    if (!currentJob) return;
    var text = notesAsMarkdown(currentJob);
    var done = function () { $("copy-status").textContent = "Notes copied to the clipboard."; };
    var fail = function () { $("copy-status").textContent = "Copy is not available here. Use an export instead."; };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, fail);
    } else {
      var ta = el("textarea");
      ta.value = text;
      ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy") ? done() : fail(); } catch (x) { fail(); }
      document.body.removeChild(ta);
    }
  });

  $("delete").addEventListener("click", function () {
    if (!currentId) return;
    if (!window.confirm("Delete this note and its transcript? This cannot be undone.")) return;
    api("/api/notes/" + currentId, { method: "DELETE" }).then(function () {
      location.hash = "#/";
    }).catch(function (e) {
      $("copy-status").textContent = e.message;
    });
  });

  /* tabs */
  var tabs = [$("tab-notes"), $("tab-transcript")];
  function selectTab(name, focus) {
    tabs.forEach(function (t) {
      var on = t.id === "tab-" + name;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
      $(t.getAttribute("aria-controls")).hidden = !on;
      if (on && focus) t.focus();
    });
  }
  tabs.forEach(function (t, i) {
    t.addEventListener("click", function () { selectTab(t.id.slice(4), false); });
    t.addEventListener("keydown", function (e) {
      var j = null;
      if (e.key === "ArrowRight") j = (i + 1) % tabs.length;
      else if (e.key === "ArrowLeft") j = (i - 1 + tabs.length) % tabs.length;
      else if (e.key === "Home") j = 0;
      else if (e.key === "End") j = tabs.length - 1;
      if (j !== null) { e.preventDefault(); selectTab(tabs[j].id.slice(4), true); }
    });
  });

  /* ---------- recent notes ---------- */

  function loadRecent() {
    return api("/api/notes").then(function (r) { return r.json(); }).then(function (items) {
      var list = $("recent-list");
      list.textContent = "";
      $("recent-empty").hidden = items.length > 0;
      list.hidden = items.length === 0;
      items.forEach(function (it) {
        var li = el("li");
        var a = el("a", null, "row");
        a.href = "#/note/" + it.id;
        var title = it.status === "done" ? (it.title || it.filename) : it.filename;
        a.appendChild(el("span", title, "row-title"));
        var state;
        if (it.status === "done") state = fmtDate(it.created);
        else if (it.status === "error") state = "Failed";
        else state = "In progress";
        a.appendChild(el("span", state, "row-meta" + (it.status === "error" ? " row-state bad" : "")));
        var sub = [it.status === "done" && it.title ? it.filename : "", fmtDuration(it.duration)].filter(Boolean).join("  /  ");
        if (sub) a.appendChild(el("span", sub, "row-sub"));
        li.appendChild(a);
        list.appendChild(li);
      });
    }).catch(function () { /* the list is optional */ });
  }

  /* ---------- polling and routing ---------- */

  function stopPolling() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  function poll(id) {
    api("/api/notes/" + id).then(function (r) { return r.json(); }).then(function (job) {
      if (id !== currentId) return;
      pollFailures = 0;
      if (job.status === "done" && job.notes) {
        renderResult(job);
      } else if (job.status === "error") {
        showError(job.error || "Something went wrong while processing this file.");
      } else {
        renderProgress(job);
        pollTimer = setTimeout(function () { poll(id); }, 1500);
      }
    }).catch(function (err) {
      if (id !== currentId) return;
      if (err.status === 404) { showError("This note does not exist anymore."); return; }
      pollFailures += 1;
      if (pollFailures > 6) { showError("The server stopped answering. Reload the page to check again."); return; }
      pollTimer = setTimeout(function () { poll(id); }, 3000);
    });
  }

  var firstRoute = true;

  function route() {
    stopPolling();
    document.title = "Voice Notes";
    var m = /^#\/note\/([a-f0-9]+)$/.exec(location.hash);
    if (m) {
      currentId = m[1];
      pollFailures = 0;
      poll(currentId);
    } else {
      currentId = null;
      currentJob = null;
      if (!uploading) {
        show("start", !firstRoute);
        loadHealth();
        loadRecent();
      }
    }
  }

  $("error-retry").addEventListener("click", function () {
    if (location.hash && location.hash !== "#/") { location.hash = "#/"; } else { route(); }
  });

  window.addEventListener("hashchange", function () { firstRoute = false; route(); });
  route();
  firstRoute = false;
})();
