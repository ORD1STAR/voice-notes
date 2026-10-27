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
  var currentJob = null;
  var selected = null;
  var uploading = false;
  var searchQuery = "";
  var listRequest = 0;
  var elapsed = { base: 0, at: 0, label: "" };

  var STEP_ORDER = ["upload", "converting", "transcribing", "structuring"];
  var AUDIO_EXT = /\.(mp3|m4a|wav|ogg|oga|opus|flac|aac|webm|mp4|m4v|mov|mkv|amr|3gp|wma|aiff?)$/i;
  var SVG_NS = "http://www.w3.org/2000/svg";

  /* ---------- helpers ---------- */

  function show(name, focus) {
    Object.keys(views).forEach(function (k) { views[k].hidden = k !== name; });
    document.body.setAttribute("data-view", name);
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

  function fmtClock(sec) {
    sec = Math.max(0, Math.floor(sec));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h ? h + ":" + (m < 10 ? "0" : "") : "") + m + ":" + (s < 10 ? "0" : "") + s;
  }

  function fmtDate(ts) {
    return new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
  }

  function fmtShortDate(ts) {
    var d = new Date(ts * 1000);
    var opts = { day: "numeric", month: "short" };
    if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
    return d.toLocaleDateString(undefined, opts);
  }

  function el(tag, text, cls) {
    var e = document.createElement(tag);
    if (text != null) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }

  function showError(message, removable, retryable) {
    stopPolling();
    $("error-message").textContent = message;
    $("error-remove").hidden = !removable;
    $("error-again").hidden = !retryable;
    show("error");
  }

  function api(path, opts) {
    return fetch(path, opts).then(function (res) {
      if (res.ok) return res;
      return res.json().catch(function () { return {}; }).then(function (body) {
        var err = new Error(body.error || (body.detail && typeof body.detail === "string" ? body.detail : "The request failed (" + res.status + ")."));
        err.status = res.status;
        throw err;
      });
    });
  }

  /* ---------- health, structuring method and engine line ---------- */

  function backupText(plan) {
    var b = plan && plan.backup;
    if (b && b.engine && b.engine.indexOf("ollama:") === 0) return "the local model " + b.model + " takes over";
    return "the built-in rules take over";
  }

  function methodText(plan) {
    if (plan && plan.engine && plan.engine.indexOf("groq:") === 0) {
      return {
        title: "Notes are written by Groq " + plan.model,
        text: "What you said is rewritten into clean notes that you can share, in the language of the recording. " +
          "If Groq cannot be reached, " + backupText(plan) + "."
      };
    }
    if (plan && plan.engine && plan.engine.indexOf("ollama:") === 0) {
      return {
        title: "Notes are written by the local model " + plan.model,
        text: "On a small server each part of a long recording takes a few minutes. You will see every part as it finishes."
      };
    }
    return {
      title: "Notes use the built-in rules",
      text: ((plan && plan.reason) ? plan.reason + " " : "") +
        "You still get a title, summary, key points, sections, diagrams and action items, but the wording stays close to what was said. " +
        "Install Ollama and a model (see the README) for rewritten, more polished notes."
    };
  }

  function showMethod(prefix, plan) {
    if (!plan) return;
    var m = methodText(plan);
    $(prefix + "-title").textContent = m.title;
    $(prefix + "-text").textContent = m.text;
    $(prefix).hidden = false;
  }

  function loadHealth() {
    return api("/api/health").then(function (r) { return r.json(); }).then(function (h) {
      health = h;
      maxBytes = h.max_upload_mb * 1024 * 1024;
      $("max-mb").textContent = h.max_upload_mb;
      var o = h.ollama || {};
      var g = h.groq_chat || {};
      var structuring = o.available ? "local model " + o.model : "rule-based structuring";
      if (g.enabled) {
        structuring = "Groq " + g.model + ", " + (o.available ? "local model " + o.model : "built-in rules") + " as backup";
      }
      $("engine-line").textContent = "Transcription: Groq Whisper. Structuring: " + structuring + ".";
      showMethod("method", h.structuring);

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
    }
    if (!selected) $("file").value = "";
    updateGo();
    if (selected && !$("go").disabled) $("go").focus();
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
  // A click anywhere on the idle drop area opens the file picker.
  $("drop-idle").addEventListener("click", function (e) {
    if (e.target.closest("button, label, a, input, select")) return;
    $("file").click();
  });
  window.addEventListener("dragover", function (e) { e.preventDefault(); });
  window.addEventListener("drop", function (e) { e.preventDefault(); });

  /* ---------- language of the recording ---------- */

  function currentLanguage() { return $("language").value || "auto"; }

  function languageLabel(code) {
    var opt = $("language").querySelector('option[value="' + code + '"]');
    return opt ? opt.textContent : code;
  }

  // The language you chose last time is offered again.
  try {
    var savedLanguage = localStorage.getItem("vn-language");
    if (savedLanguage && $("language").querySelector('option[value="' + savedLanguage + '"]')) $("language").value = savedLanguage;
  } catch (x) { /* storage may be blocked */ }
  $("language").addEventListener("change", function () {
    try { localStorage.setItem("vn-language", $("language").value); } catch (x) { /* storage may be blocked */ }
  });

  /* ---------- recording in the browser ---------- */

  var rec = { stream: null, recorder: null, chunks: [], started: 0, timer: null, cancelled: false };

  function pickMime() {
    var options = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
    for (var i = 0; i < options.length; i++) {
      if (window.MediaRecorder.isTypeSupported && window.MediaRecorder.isTypeSupported(options[i])) return options[i];
    }
    return "";
  }

  function setupRecorder() {
    var supported = !!(window.MediaRecorder && navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
    $("rec-start").hidden = !(supported && window.isSecureContext);
    $("rec-hint").hidden = !(window.MediaRecorder && !window.isSecureContext);
  }

  function showRecordingUi(on) {
    $("drop-idle").hidden = on;
    $("drop-rec").hidden = !on;
  }

  function endRecording() {
    clearInterval(rec.timer);
    rec.timer = null;
    if (rec.stream) rec.stream.getTracks().forEach(function (t) { t.stop(); });
    rec.stream = null;
    showRecordingUi(false);
  }

  function startRecording() {
    var err = $("file-error");
    err.hidden = true;
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      var mime = pickMime();
      var mr;
      try { mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined); }
      catch (x) { stream.getTracks().forEach(function (t) { t.stop(); }); throw x; }
      rec.stream = stream;
      rec.recorder = mr;
      rec.chunks = [];
      rec.cancelled = false;
      mr.ondataavailable = function (e) { if (e.data && e.data.size) rec.chunks.push(e.data); };
      mr.onstop = function () {
        var type = mr.mimeType || mime || "audio/webm";
        var chunks = rec.chunks;
        var cancelled = rec.cancelled;
        endRecording();
        if (cancelled || !chunks.length) return;
        var ext = type.indexOf("mp4") >= 0 ? "m4a" : type.indexOf("ogg") >= 0 ? "ogg" : "webm";
        var d = new Date();
        var pad = function (n) { return (n < 10 ? "0" : "") + n; };
        var name = "recording-" + d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "-" + pad(d.getHours()) + pad(d.getMinutes()) + "." + ext;
        setSelected(new File(chunks, name, { type: type }));
      };
      mr.start(1000);
      rec.started = Date.now();
      $("rec-time").textContent = "0:00";
      rec.timer = setInterval(function () { $("rec-time").textContent = fmtClock((Date.now() - rec.started) / 1000); }, 500);
      showRecordingUi(true);
      $("rec-stop").focus();
    }).catch(function (e) {
      var denied = e && (e.name === "NotAllowedError" || e.name === "SecurityError");
      var missing = e && (e.name === "NotFoundError" || e.name === "OverconstrainedError");
      err.textContent = denied ? "Microphone access was blocked. Allow it in the address bar, or choose a file instead."
        : missing ? "No microphone was found. Choose a file instead."
        : "Recording did not start. Choose a file instead.";
      err.hidden = false;
    });
  }

  $("rec-start").addEventListener("click", startRecording);
  $("rec-stop").addEventListener("click", function () {
    if (rec.recorder && rec.recorder.state !== "inactive") rec.recorder.stop();
  });
  $("rec-cancel").addEventListener("click", function () {
    rec.cancelled = true;
    if (rec.recorder && rec.recorder.state !== "inactive") rec.recorder.stop(); else endRecording();
    $("rec-start").focus();
  });

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
    var lang = currentLanguage();
    $("progress-file").textContent = file.name + " (" + fmtSize(file.size) + ")" +
      (lang !== "auto" ? ", " + languageLabel(lang) : "");
    $("progress-elapsed").textContent = "";
    elapsed.at = 0;
    showMethod("progress-method", health && health.structuring);
    setSteps("upload", "Uploading");
    setBar(0, false);
    $("progress-detail").textContent = "Uploading 0%";
    show("progress");

    var form = new FormData();
    form.append("file", file);
    form.append("language", lang);
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
        showError(body.error || "The upload failed (" + xhr.status + ").", false, false);
      }
      updateGo();
    };
    xhr.onerror = function () {
      uploading = false;
      showError("The upload was interrupted. Check your connection and try again.", false, false);
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
      if (i === idx && !failed) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
      setStepState(s, i < idx ? "Done" : i === idx ? activeText : "Waiting");
    });
  }

  var STAGE_LABEL = { converting: "Preparing the audio", transcribing: "Transcribing", structuring: "Structuring" };

  function renderElapsed() {
    var box = $("progress-elapsed");
    if (!elapsed.at || views.progress.hidden) { return; }
    box.textContent = elapsed.label + ": " + fmtClock(elapsed.base + (Date.now() - elapsed.at) / 1000);
  }
  setInterval(renderElapsed, 1000);

  function renderProgress(job) {
    $("progress-file").textContent = job.filename;
    var queued = job.stage === "queued";
    var stage = queued ? "converting" : job.stage;
    setSteps(stage, queued ? "Queued" : "In progress");
    setBar(job.progress * 100, queued);
    var detail = job.detail || "Working";
    if (queued && job.ahead > 0) {
      detail = "Waiting for " + job.ahead + (job.ahead === 1 ? " other note" : " other notes") + " to finish first";
    }
    $("progress-detail").textContent = detail;
    if (queued) {
      elapsed.at = 0;
      $("progress-elapsed").textContent = "";
    } else {
      elapsed.base = job.stage_elapsed || 0;
      elapsed.at = Date.now();
      elapsed.label = (STAGE_LABEL[job.stage] || "This step") + ", time so far";
      renderElapsed();
    }
    showMethod("progress-method", job.plan);
    if (views.progress.hidden) show("progress");
  }

  /* ---------- diagrams ---------- */

  function svgEl(tag, attrs) {
    var e = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    return e;
  }

  function drawLayout(lay, cls) {
    var svg = svgEl("svg", { viewBox: "0 0 " + lay.w + " " + lay.h, "class": "vis-svg " + cls, "aria-hidden": "true", focusable: "false" });
    lay.items.forEach(function (it) {
      var c = "v-" + it.role;
      var node;
      if (it.t === "rect") {
        node = svgEl("rect", { x: it.x, y: it.y, width: it.w, height: it.h, rx: it.r, "class": c });
      } else if (it.t === "circle") {
        node = svgEl("circle", { cx: it.x, cy: it.y, r: it.r, "class": c });
      } else if (it.t === "text") {
        node = svgEl("text", { x: it.x, y: it.y, "font-size": it.size, "text-anchor": it.anchor, "class": c });
        if (it.bold) node.setAttribute("font-weight", "600");
        node.textContent = it.text;
      } else if (it.t === "line") {
        node = svgEl("polyline", { points: it.pts.map(function (p) { return p[0] + "," + p[1]; }).join(" "), "class": c });
      } else if (it.t === "arrow") {
        node = svgEl("polygon", { points: it.pts.map(function (p) { return p[0] + "," + p[1]; }).join(" "), "class": c });
      }
      if (node) svg.appendChild(node);
    });
    return svg;
  }

  function renderFigures(list) {
    var host = $("figures");
    host.textContent = "";
    (list || []).forEach(function (v) {
      var fig = el("figure", null, "figure");
      fig.setAttribute("data-kind", v.kind);
      var cap = el("figcaption");
      cap.appendChild(el("span", v.title, "figure-title"));
      cap.appendChild(el("span", v.caption, "figure-caption"));
      fig.appendChild(cap);
      var body = el("div", null, "figure-body");
      body.setAttribute("role", "img");
      body.setAttribute("aria-label", v.alt);
      body.appendChild(drawLayout(v.layouts.wide, "vis-wide"));
      body.appendChild(drawLayout(v.layouts.narrow, "vis-narrow"));
      fig.appendChild(body);
      host.appendChild(fig);
    });
    $("glance").hidden = !(list && list.length);
  }

  /* ---------- key points ---------- */

  function renderKeyPoints(list) {
    var host = $("keypoints-list");
    host.textContent = "";
    (list || []).forEach(function (text) { host.appendChild(el("li", text)); });
    $("keypoints-block").hidden = !(list && list.length);
  }

  /* ---------- action items ---------- */

  function storedChecks(id) {
    try { return JSON.parse(localStorage.getItem("vn-actions:" + id) || "[]"); } catch (x) { return []; }
  }

  function saveChecks(id, list) {
    try { localStorage.setItem("vn-actions:" + id, JSON.stringify(list)); } catch (x) { /* storage may be blocked */ }
  }

  function renderActions(job) {
    var list = (job.notes && job.notes.actions) || [];
    var host = $("actions-list");
    host.textContent = "";
    $("actions-block").hidden = list.length === 0;
    if (!list.length) return;
    var done = storedChecks(job.id);
    function count() {
      var n = host.querySelectorAll("input:checked").length;
      $("actions-count").textContent = n + " of " + list.length + " done";
    }
    list.forEach(function (text, i) {
      var li = el("li");
      var label = el("label");
      var box = el("input");
      box.type = "checkbox";
      box.checked = done.indexOf(text) >= 0;
      box.addEventListener("change", function () {
        var now = [];
        host.querySelectorAll("input").forEach(function (b, j) { if (b.checked) now.push(list[j]); });
        saveChecks(job.id, now);
        count();
      });
      label.appendChild(box);
      label.appendChild(el("span", text));
      li.appendChild(label);
      host.appendChild(li);
    });
    count();
  }

  /* ---------- result view ---------- */

  function engineNote(job) {
    var note = $("engine-note");
    var plan = job.plan || {};
    var text = "";
    if (job.engine === "rule-based") {
      text = job.engine_detail
        ? job.engine_detail + " Sentences are kept close to the wording of the recording."
        : "These notes were built with the built-in rules. " + (plan.reason ? plan.reason + " " : "") +
          "Sentences are kept close to the wording of the recording. Install Ollama and a model for rewritten notes.";
    } else if (job.engine && job.engine.indexOf("partly") > 0) {
      text = "The model could not handle every part of this recording, so some sections were built with the built-in rules.";
    } else if (job.engine_detail) {
      text = job.engine_detail;
    }
    if (job.edited) text = "";
    note.textContent = text;
    note.hidden = !text;
  }

  function engineLabel(engine) {
    if (engine && engine.indexOf("groq:") === 0) return "Structured with Groq " + engine.slice(5);
    if (engine && engine.indexOf("ollama:") === 0) return "Structured with " + engine.slice(7);
    return "Structured with rules";
  }

  function renderResult(job) {
    currentJob = job;
    if (editing) setEditing(false);
    var n = job.notes;
    $("result-title").textContent = n.title;
    closeRename(false);
    var engine = engineLabel(job.engine);
    var language = "Language: " + (job.language_name || "Auto");
    var meta = $("result-meta");
    meta.textContent = "";
    [job.filename, fmtDuration(job.duration), fmtDate(job.created), language, engine, job.edited ? "Edited by you" : ""].filter(Boolean).forEach(function (b) {
      meta.appendChild(el("li", b));
    });
    $("summary").textContent = n.summary;
    $("summary-block").hidden = !n.summary;
    renderKeyPoints(n.key_points);
    renderActions(job);
    renderFigures(job.visuals);

    var host = $("sections");
    host.textContent = "";
    n.sections.forEach(function (s, i) {
      var sec = el("section");
      sec.appendChild(el("span", (i < 9 ? "0" : "") + (i + 1), "sec-n"));
      var inner = el("div");
      inner.appendChild(el("h3", s.heading));
      var ul = el("ul");
      s.bullets.forEach(function (b) { ul.appendChild(el("li", b)); });
      inner.appendChild(ul);
      sec.appendChild(inner);
      host.appendChild(sec);
    });
    engineNote(job);

    var tr = $("transcript");
    tr.textContent = "";
    var paras = job.transcript_paragraphs && job.transcript_paragraphs.length
      ? job.transcript_paragraphs : (job.transcript ? [job.transcript] : []);
    paras.forEach(function (p) { tr.appendChild(el("p", p)); });

    markDirection(job);
    $("copy-status").textContent = job.rebuild_error || "";
    selectTab("notes", false);
    updateExportLinks();
    show("result");
    document.title = n.title + " - Voice Notes";
    loadRecent();
  }

  function updateExportLinks() {
    if (!currentId) return;
    var t = $("with-transcript").checked ? "&transcript=true" : "";
    ["pdf", "txt", "md"].forEach(function (f) {
      $("exp-" + f).href = "/api/notes/" + currentId + "/export?format=" + f + t;
    });
    var pdf = $("exp-pdf");
    var problem = currentJob && currentJob.pdf_problem;
    if (problem) {
      pdf.setAttribute("aria-disabled", "true");
      pdf.removeAttribute("download");
      pdf.title = problem;
    } else {
      pdf.removeAttribute("aria-disabled");
      pdf.setAttribute("download", "");
      pdf.removeAttribute("title");
    }
  }
  $("exp-pdf").addEventListener("click", function (e) {
    if (currentJob && currentJob.pdf_problem) {
      e.preventDefault();
      $("copy-status").textContent = currentJob.pdf_problem;
    }
  });
  $("with-transcript").addEventListener("change", updateExportLinks);

  function notesAsMarkdown(job) {
    var out = ["# " + job.notes.title, ""];
    if (job.notes.summary) out.push("## Summary", "", job.notes.summary, "");
    if (job.notes.key_points && job.notes.key_points.length) {
      out.push("## Key points", "");
      job.notes.key_points.forEach(function (k) { out.push("- " + k); });
      out.push("");
    }
    if (job.notes.actions && job.notes.actions.length) {
      out.push("## Action items", "");
      job.notes.actions.forEach(function (a) { out.push("- [ ] " + a); });
      out.push("");
    }
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

  /* ---------- renaming ---------- */

  function closeRename(focusButton) {
    $("rename-form").hidden = true;
    $("title-row").hidden = false;
    if (focusButton) $("rename").focus();
  }

  $("rename").addEventListener("click", function () {
    if (!currentJob) return;
    $("title-row").hidden = true;
    $("rename-form").hidden = false;
    var input = $("rename-input");
    input.value = currentJob.notes.title;
    input.focus();
    input.select();
  });
  $("rename-cancel").addEventListener("click", function () { closeRename(true); });
  $("rename-input").addEventListener("keydown", function (e) {
    if (e.key === "Escape") { e.preventDefault(); closeRename(true); }
  });
  $("rename-form").addEventListener("submit", function (e) {
    e.preventDefault();
    if (!currentId) return;
    var title = $("rename-input").value.replace(/\s+/g, " ").trim();
    if (!title) { $("rename-input").focus(); return; }
    if (title === currentJob.notes.title) { closeRename(true); return; }
    $("rename-save").disabled = true;
    api("/api/notes/" + currentId, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title })
    }).then(function (r) { return r.json(); }).then(function (job) {
      currentJob.notes.title = job.notes.title;
      $("result-title").textContent = job.notes.title;
      document.title = job.notes.title + " - Voice Notes";
      closeRename(true);
      $("copy-status").textContent = "Renamed.";
      loadRecent();
    }).catch(function (err) {
      $("copy-status").textContent = err.message;
      closeRename(true);
    }).then(function () { $("rename-save").disabled = false; });
  });

  /* ---------- deleting ---------- */

  var DELETE_ASK = {
    title: "Delete this note?",
    text: "The notes and the transcript will be removed from this server. This cannot be undone.",
    no: "Keep it", yes: "Delete", danger: true
  };
  var REBUILD_ASK = {
    title: "Rebuild these notes?",
    text: "The notes are written again from the transcript with the method available now. The title you chose is kept. " +
      "Ticked action items may need ticking again.",
    no: "Cancel", yes: "Rebuild", danger: false
  };

  var REBUILD_EDITED_ASK = {
    title: "Rebuild these notes?",
    text: "The notes are written again from the transcript. The changes you made by hand will be replaced. " +
      "The title you chose is kept.",
    no: "Cancel", yes: "Rebuild", danger: true
  };

  function ask(opts) {
    var d = $("confirm");
    if (typeof d.showModal !== "function") {
      return Promise.resolve(window.confirm(opts.title + " " + opts.text));
    }
    $("confirm-title").textContent = opts.title;
    $("confirm-text").textContent = opts.text;
    $("confirm-cancel").textContent = opts.no;
    var yes = $("confirm-delete");
    yes.textContent = opts.yes;
    yes.classList.toggle("danger-solid", opts.danger);
    yes.classList.toggle("confirm-neutral", !opts.danger);
    return new Promise(function (resolve) {
      d.returnValue = "";
      var onClose = function () { d.removeEventListener("close", onClose); resolve(d.returnValue === "delete"); };
      d.addEventListener("close", onClose);
      d.showModal();
    });
  }

  function confirmDelete() { return ask(DELETE_ASK); }

  function removeCurrent(onError) {
    if (!currentId) return;
    confirmDelete().then(function (yes) {
      if (!yes) return null;
      return api("/api/notes/" + currentId, { method: "DELETE" }).then(function () { location.hash = "#/"; });
    }).catch(function (e) { onError(e.message); });
  }

  $("delete").addEventListener("click", function () {
    removeCurrent(function (m) { $("copy-status").textContent = m; });
  });
  $("error-remove").addEventListener("click", function () {
    removeCurrent(function (m) { $("error-message").textContent = m; });
  });

  /* ---------- rebuilding the notes from the transcript ---------- */

  $("rebuild").addEventListener("click", function () {
    if (!currentId) return;
    var id = currentId;
    ask(currentJob && currentJob.edited ? REBUILD_EDITED_ASK : REBUILD_ASK).then(function (yes) {
      if (!yes) { $("rebuild").focus(); return null; }
      $("rebuild").disabled = true;
      return api("/api/notes/" + id + "/restructure", { method: "POST" }).then(function () {
        pollFailures = 0;
        $("progress-file").textContent = currentJob ? currentJob.filename : "";
        elapsed.at = 0;
        $("progress-elapsed").textContent = "";
        setSteps("structuring", "Queued");
        setBar(70, true);
        $("progress-detail").textContent = "Rebuilding the notes";
        show("progress");
        poll(id);
      });
    }).catch(function (err) {
      $("copy-status").textContent = err.message;
    }).then(function () { $("rebuild").disabled = false; });
  });

  /* ---------- trying a failed note again ---------- */

  $("error-again").addEventListener("click", function () {
    if (!currentId) return;
    var id = currentId;
    $("error-again").disabled = true;
    api("/api/notes/" + id + "/retry", { method: "POST" }).then(function () {
      pollFailures = 0;
      $("progress-file").textContent = "";
      elapsed.at = 0;
      $("progress-elapsed").textContent = "";
      setSteps("converting", "Queued");
      setBar(0, true);
      $("progress-detail").textContent = "Starting again";
      show("progress");
      poll(id);
    }).catch(function (err) {
      $("error-message").textContent = err.message;
      $("error-again").hidden = true;
    }).then(function () { $("error-again").disabled = false; });
  });

  /* ---------- editing the notes ---------- */

  var editing = false;
  var editSeq = 0;

  function lines(text) {
    return text.split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
  }

  function renumberSections() {
    Array.prototype.forEach.call($("edit-sections").children, function (box, i) {
      box.querySelector("legend").textContent = "Section " + (i + 1);
    });
  }

  function addEditSection(heading, bullets, focus) {
    var id = ++editSeq;
    var box = el("fieldset", null, "edit-section");
    box.appendChild(el("legend"));

    var hl = el("label", "Heading", "edit-label");
    hl.setAttribute("for", "edit-h-" + id);
    var hi = el("input");
    hi.type = "text";
    hi.id = "edit-h-" + id;
    hi.maxLength = 100;
    hi.autocomplete = "off";
    hi.setAttribute("dir", "auto");
    hi.value = heading || "";

    var bl = el("label", "Points, one per line", "edit-label");
    bl.setAttribute("for", "edit-b-" + id);
    var bt = el("textarea");
    bt.id = "edit-b-" + id;
    bt.setAttribute("dir", "auto");
    bt.rows = Math.min(12, Math.max(3, (bullets || []).length + 1));
    bt.value = (bullets || []).join("\n");

    var rm = el("button", "Remove this section", "btn quiet danger small");
    rm.type = "button";
    rm.addEventListener("click", function () {
      box.parentNode.removeChild(box);
      renumberSections();
      $("edit-add").focus();
    });

    [hl, hi, bl, bt, rm].forEach(function (n) { box.appendChild(n); });
    $("edit-sections").appendChild(box);
    renumberSections();
    if (focus) hi.focus();
  }

  function setEditing(on) {
    editing = on;
    $("view-result").classList.toggle("editing", on);
    $("edit-form").hidden = !on;
    $("edit-notes").disabled = on;
    $("rebuild").disabled = on;
    if (on) closeRename(false);
  }

  function showEditError(message, field) {
    var box = $("edit-error");
    box.textContent = message;
    box.hidden = false;
    if (field) field.focus();
  }

  function startEdit() {
    if (!currentJob || !currentJob.notes || editing) return;
    var n = currentJob.notes;
    $("edit-title").value = n.title || "";
    $("edit-summary").value = n.summary || "";
    $("edit-keypoints").value = (n.key_points || []).join("\n");
    $("edit-actions").value = (n.actions || []).join("\n");
    $("edit-sections").textContent = "";
    n.sections.forEach(function (s) { addEditSection(s.heading, s.bullets, false); });
    $("edit-error").hidden = true;
    setEditing(true);
    selectTab("notes", false);
    $("edit-title").focus();
  }

  function stopEdit() {
    setEditing(false);
    $("edit-notes").focus();
  }

  $("edit-notes").addEventListener("click", startEdit);
  $("edit-cancel").addEventListener("click", stopEdit);
  $("edit-add").addEventListener("click", function () { addEditSection("", [], true); });
  $("edit-form").addEventListener("keydown", function (e) {
    if (e.key === "Escape") { e.preventDefault(); stopEdit(); }
  });

  $("edit-form").addEventListener("submit", function (e) {
    e.preventDefault();
    if (!currentId) return;
    var id = currentId;
    var title = $("edit-title").value.trim();
    var sections = [];
    Array.prototype.forEach.call($("edit-sections").children, function (box) {
      sections.push({
        heading: box.querySelector("input").value.trim(),
        bullets: lines(box.querySelector("textarea").value)
      });
    });
    if (!title) { showEditError("The title cannot be empty.", $("edit-title")); return; }
    if (!sections.some(function (s) { return s.bullets.length > 0; })) {
      showEditError("Keep at least one section with a point in it.", $("edit-add"));
      return;
    }
    $("edit-error").hidden = true;
    var save = $("edit-save");
    save.disabled = true;
    api("/api/notes/" + id + "/notes", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: title,
        summary: $("edit-summary").value.trim(),
        key_points: lines($("edit-keypoints").value),
        actions: lines($("edit-actions").value),
        sections: sections
      })
    }).then(function (r) { return r.json(); }).then(function (job) {
      setEditing(false);
      renderResult(job);
      $("copy-status").textContent = "Changes saved.";
      $("edit-notes").focus();
    }).catch(function (err) {
      showEditError(err.message);
    }).then(function () { save.disabled = false; });
  });

  /* ---------- direction and language of the notes ---------- */

  var DIR_TARGETS = "#result-title, #summary, #keypoints-list li, #actions-list li span, #sections h3, #sections li, #transcript p";

  function markDirection(job) {
    var code = job.language && job.language !== "auto" ? job.language : "";
    ["panel-notes", "panel-transcript"].forEach(function (id) {
      if (code) $(id).setAttribute("lang", code); else $(id).removeAttribute("lang");
    });
    Array.prototype.forEach.call(document.querySelectorAll(DIR_TARGETS), function (n) { n.setAttribute("dir", "auto"); });
  }

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

  /* ---------- note lists (sidebar and start page) and search ---------- */

  function searchTerms() {
    return searchQuery.toLowerCase().split(/\s+/).filter(Boolean).slice(0, 8);
  }

  function highlight(text, terms) {
    var frag = document.createDocumentFragment();
    if (!terms.length) { frag.appendChild(document.createTextNode(text)); return frag; }
    var rx = new RegExp("(" + terms.map(function (t) { return t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }).join("|") + ")", "gi");
    text.split(rx).forEach(function (part, i) {
      if (!part) return;
      if (i % 2 === 1) frag.appendChild(el("mark", part)); else frag.appendChild(document.createTextNode(part));
    });
    return frag;
  }

  function fillList(list, empty, items) {
    var terms = searchTerms();
    list.textContent = "";
    empty.textContent = terms.length ? "No notes match \"" + searchQuery.trim() + "\"." :
      (list.id === "side-list" ? "Nothing here yet." : "No notes yet. Notes you create will be listed here.");
    empty.hidden = items.length > 0;
    list.hidden = items.length === 0;
    items.forEach(function (it) {
      var li = el("li");
      var a = el("a", null, "row");
      a.href = "#/note/" + it.id;
      if (it.id === currentId) a.setAttribute("aria-current", "page");
      var title = it.status === "done" ? (it.title || it.filename) : it.filename;
      var t = el("span", null, "row-title");
      t.appendChild(highlight(title, terms));
      a.appendChild(t);
      var state, bad = false;
      if (it.status === "done") state = fmtShortDate(it.created);
      else if (it.status === "error") { state = "Failed"; bad = true; }
      else state = "Working";
      a.appendChild(el("span", state, "row-meta" + (bad ? " bad" : "")));
      if (terms.length && it.snippet) {
        var sn = el("span", null, "row-snippet");
        sn.appendChild(highlight(it.snippet, terms));
        a.appendChild(sn);
      } else {
        var sub = [it.status === "done" && it.title ? it.filename : "", fmtDuration(it.duration)].filter(Boolean).join("  /  ");
        if (sub) a.appendChild(el("span", sub, "row-sub"));
      }
      li.appendChild(a);
      list.appendChild(li);
    });
  }

  function loadRecent() {
    var ticket = ++listRequest;
    var url = "/api/notes" + (searchQuery.trim() ? "?q=" + encodeURIComponent(searchQuery.trim()) : "");
    return api(url).then(function (r) { return r.json(); }).then(function (items) {
      if (ticket !== listRequest) return; // a newer search is already on its way
      items = items.slice(0, 60);
      fillList($("recent-list"), $("recent-empty"), items);
      fillList($("side-list"), $("side-empty"), items);
      $("search-status").textContent = searchQuery.trim()
        ? (items.length === 1 ? "1 note found" : items.length + " notes found") : "";
    }).catch(function () { /* the lists are optional */ });
  }

  var searchTimer = null;
  ["search", "search-recent"].forEach(function (id) {
    $(id).addEventListener("input", function (e) {
      searchQuery = e.target.value;
      ["search", "search-recent"].forEach(function (other) { if ($(other) !== e.target) $(other).value = searchQuery; });
      clearTimeout(searchTimer);
      searchTimer = setTimeout(loadRecent, 200);
    });
    $(id).addEventListener("keydown", function (e) {
      if (e.key === "Escape" && $(id).value) {
        $(id).value = "";
        $(id).dispatchEvent(new Event("input"));
      }
    });
  });

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
        showError(job.error || "Something went wrong while processing this file.", true, !!job.retryable);
        loadRecent();
      } else {
        renderProgress(job);
        pollTimer = setTimeout(function () { poll(id); }, 1500);
      }
    }).catch(function (err) {
      if (id !== currentId) return;
      if (err.status === 404) { showError("This note does not exist anymore.", false, false); return; }
      pollFailures += 1;
      if (pollFailures > 6) { showError("The server stopped answering. Reload the page to check again.", false, false); return; }
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
      elapsed.at = 0;
      $("progress-elapsed").textContent = "";
      if (!health) loadHealth();
      poll(currentId);
    } else {
      currentId = null;
      currentJob = null;
      if (!uploading) {
        show("start", !firstRoute);
        loadHealth();
      }
    }
    loadRecent();
  }

  $("error-retry").addEventListener("click", function () {
    if (location.hash && location.hash !== "#/") { location.hash = "#/"; } else { route(); }
  });

  window.addEventListener("hashchange", function () { firstRoute = false; route(); });
  setupRecorder();
  route();
  firstRoute = false;
})();
