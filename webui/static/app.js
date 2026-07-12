/* RPI2DMD v3 web UI — vanilla JS enhancements. No external resources. */
(function () {
  "use strict";

  // ---- helpers ----------------------------------------------------------

  function $(sel, el) { return (el || document).querySelector(sel); }
  function $$(sel, el) {
    return Array.prototype.slice.call((el || document).querySelectorAll(sel));
  }

  var toastTimer = null;
  function toast(msg, isErr) {
    var el = $("#toast");
    if (!el) return;
    el.textContent = msg;
    el.className = "toast" + (isErr ? " err" : "");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 2400);
  }

  function apiGet(path) {
    return fetch(path).then(function (r) { return r.json(); });
  }

  function apiPost(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); });
  }

  function saveConfig(partial, okMsg) {
    return apiPost("/api/config", partial).then(function (resp) {
      if (resp.ok) {
        toast(okMsg || "Saved");
      } else {
        toast(resp.error || "Save failed", true);
      }
      return resp;
    }).catch(function () { toast("Save failed", true); });
  }

  function setPath(obj, dotted, value) {
    var parts = dotted.split(".");
    var node = obj;
    for (var i = 0; i < parts.length - 1; i++) {
      if (typeof node[parts[i]] !== "object" || node[parts[i]] === null) {
        node[parts[i]] = {};
      }
      node = node[parts[i]];
    }
    node[parts[parts.length - 1]] = value;
  }

  function hexToRgb(hex) {
    var m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) return [255, 140, 0];
    var n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function rgbToHex(rgb) {
    if (!rgb || rgb.length < 3) return "#ff8c00";
    var s = "#";
    for (var i = 0; i < 3; i++) {
      var h = Math.max(0, Math.min(255, rgb[i] | 0)).toString(16);
      s += (h.length < 2 ? "0" : "") + h;
    }
    return s;
  }

  function gatherCfg(root) {
    var out = {};
    $$("[data-cfg]", root).forEach(function (el) {
      var path = el.getAttribute("data-cfg");
      var val;
      if (el.type === "checkbox") {
        val = el.checked;
      } else if (el.type === "radio") {
        if (!el.checked) return;
        val = el.value;
      } else if (el.type === "number" || el.type === "range") {
        val = parseFloat(el.value);
        if (isNaN(val)) return;
      } else if (el.type === "color") {
        val = hexToRgb(el.value);
      } else {
        val = el.value;
      }
      setPath(out, path, val);
    });
    return out;
  }

  function fmtUptime(secs) {
    secs = Math.floor(secs);
    var d = Math.floor(secs / 86400);
    var h = Math.floor((secs % 86400) / 3600);
    var m = Math.floor((secs % 3600) / 60);
    if (d) return d + "d " + h + "h " + m + "m";
    if (h) return h + "h " + m + "m";
    return m + "m " + (secs % 60) + "s";
  }

  // ---- shared behaviors ---------------------------------------------------

  function bindControlButtons() {
    $$("[data-ctl]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        apiPost("/api/control/" + btn.getAttribute("data-ctl")).then(function (r) {
          toast(r.ok ? btn.getAttribute("data-ctl") + " ok"
                     : (r.error || "failed"), !r.ok);
        }).catch(function () { toast("request failed", true); });
      });
    });
    $$("[data-ctl-confirm]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var q = btn.getAttribute("data-confirm") || "Are you sure?";
        if (!window.confirm(q)) return;
        apiPost("/api/control/" + btn.getAttribute("data-ctl-confirm"))
          .then(function (r) {
            toast(r.ok ? "command sent" : (r.error || "failed"), !r.ok);
          }).catch(function () { toast("request failed", true); });
      });
    });
  }

  function bindQuickToggles() {
    $$("input.quick[data-cfg]").forEach(function (el) {
      el.addEventListener("change", function () {
        var partial = {};
        setPath(partial, el.getAttribute("data-cfg"), el.checked);
        saveConfig(partial, el.getAttribute("data-cfg") + " = " + el.checked);
      });
    });
  }

  function bindReveal(btnId, inputId) {
    var btn = $(btnId), input = $(inputId);
    if (!btn || !input) return;
    btn.addEventListener("click", function () {
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.textContent = show ? "Hide" : "Show";
    });
  }

  function startStatusPolling(onStatus) {
    function poll() {
      apiGet("/api/status").then(onStatus).catch(function () {
        onStatus({ state: "offline" });
      });
    }
    poll();
    setInterval(poll, 2000);
  }

  // ---- dashboard ----------------------------------------------------------

  function initDashboard() {
    startStatusPolling(function (st) {
      var badge = $("#np-state");
      var title = $("#np-title");
      var sub = $("#np-sub");
      var elapsed = $("#np-elapsed");
      var state = st.state || "offline";
      badge.textContent = state;
      badge.className = "np-state badge " +
        (state === "offline" ? "" : (state === "paused" || state === "sleeping"
          ? "warn" : "on"));
      if (state === "offline") {
        title.textContent = "Player offline";
        sub.textContent = "The player daemon is not reachable. " +
          "Configuration still works.";
        elapsed.textContent = "";
        return;
      }
      var np = st.now_playing || {};
      if (np.type === "dmd" || np.type === "gif") {
        title.textContent = (np.game ? np.game + " / " : "") + (np.name || "");
        sub.textContent = np.type === "dmd" ? "DMD animation" : "GIF clip";
      } else {
        title.textContent = np.type || state;
        sub.textContent = "";
      }
      if (np.started_at) {
        var secs = Date.now() / 1000 - np.started_at;
        if (secs >= 0 && secs < 86400) {
          elapsed.textContent = fmtUptime(secs) + " elapsed";
        } else { elapsed.textContent = ""; }
      } else { elapsed.textContent = ""; }
      if (st.uptime_s !== undefined) {
        $("#tile-uptime").textContent = fmtUptime(st.uptime_s);
        $("#tile-uptime-sub").textContent = "player";
      }
      var counts = st.counts || {};
      if (counts.dmd_animations !== undefined) {
        $("#tile-dmd").textContent =
          counts.dmd_enabled + "/" + counts.dmd_animations;
        $("#tile-gif").textContent =
          counts.gif_enabled + "/" + counts.gif_files;
      }
      if (st.version) { $("#tile-version").textContent = st.version; }
    });
  }

  // ---- clock designer -------------------------------------------------------

  function initClock() {
    var img = $("#clock-preview");
    var PARAM_KEYS = ["style", "format", "colon", "font", "font_size",
                      "color_mode", "color", "background", "align",
                      "x", "y", "shade", "outline"];

    function controlValue(key) {
      var el = $("[data-clock='" + key + "']");
      if (!el) return null;
      if (el.type === "checkbox") return el.checked ? "true" : "false";
      return el.value;
    }

    function buildURL() {
      var parts = [];
      PARAM_KEYS.forEach(function (key) {
        var v = controlValue(key);
        if (v === null || v === "") return;
        parts.push(key + "=" + encodeURIComponent(v));
      });
      var tint = $("#ck-tint").value;
      if (tint && tint !== "custom") {
        parts.push("tint=" + encodeURIComponent(tint));
      }
      parts.push("t=" + Date.now());
      return "/api/preview/clock.png?" + parts.join("&");
    }

    var loading = false;
    function refreshPreview() {
      if (loading) return;
      loading = true;
      var pre = new Image();
      var url = buildURL();
      pre.onload = function () { img.src = url; loading = false; };
      pre.onerror = function () { loading = false; };
      pre.src = url;
    }

    function updateVisibility() {
      var style = $("#ck-style").value;
      $("#ttf-only").hidden = style !== "ttf";
      $("#solid-color-field").hidden = $("#ck-color-mode").value !== "solid";
      $("#ck-shade-val").textContent = $("#ck-shade").value;
      $("#ck-font-size-val").textContent = $("#ck-font-size").value;
    }

    $$("[data-clock]").forEach(function (el) {
      var evt = (el.tagName === "SELECT" || el.type === "checkbox" ||
                 el.type === "color") ? "change" : "input";
      el.addEventListener(evt, function () {
        updateVisibility();
        refreshPreview();
      });
      if (evt === "input") {
        el.addEventListener("change", refreshPreview);
      }
    });
    $("#ck-tint").addEventListener("change", refreshPreview);

    // live colon blink: re-fetch once a second while visible
    setInterval(function () {
      if (!document.hidden) refreshPreview();
    }, 1000);

    // alignment 3x3 grid
    $$(".align-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        $$(".align-btn").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        $("#ck-align").value = btn.getAttribute("data-align");
        refreshPreview();
      });
    });

    // font list
    apiGet("/api/fonts").then(function (data) {
      var sel = $("#ck-font");
      var current = sel.value;
      sel.innerHTML = "";
      (data.fonts || []).forEach(function (f) {
        var opt = document.createElement("option");
        opt.value = f;
        opt.textContent = f;
        if (f === current) opt.selected = true;
        sel.appendChild(opt);
      });
      if (!sel.value && current) {
        var keep = document.createElement("option");
        keep.value = current;
        keep.textContent = current + " (missing)";
        keep.selected = true;
        sel.appendChild(keep);
      }
    }).catch(function () {});

    // presets (client-side control fill; user still saves)
    $$(".preset").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var p = JSON.parse(btn.getAttribute("data-preset"));
        Object.keys(p).forEach(function (key) {
          if (key === "tint") {
            $("#ck-tint").value = p[key];
            return;
          }
          var el = $("[data-clock='" + key + "']");
          if (!el) return;
          if (el.type === "checkbox") { el.checked = !!p[key]; }
          else { el.value = p[key]; }
          if (key === "align") {
            $$(".align-btn").forEach(function (b) {
              b.classList.toggle("active",
                b.getAttribute("data-align") === String(p[key]));
            });
          }
        });
        updateVisibility();
        refreshPreview();
        toast("Preset applied — press Save to keep it");
      });
    });

    function collectForSave() {
      var clockCfg = {};
      $$("[data-clock]").forEach(function (el) {
        var key = el.getAttribute("data-clock");
        if (el.type === "checkbox") { clockCfg[key] = el.checked; }
        else if (el.type === "color") { clockCfg[key] = hexToRgb(el.value); }
        else if (el.type === "range" || el.type === "number") {
          var n = parseFloat(el.value);
          if (!isNaN(n)) clockCfg[key] = n;
        } else { clockCfg[key] = el.value; }
      });
      var body = { clock: clockCfg };
      var tint = $("#ck-tint").value;
      if (tint && tint !== "custom") { body.display = { tint: tint }; }
      return body;
    }

    $("#clock-save").addEventListener("click", function () {
      saveConfig(collectForSave(), "Clock saved");
    });

    $("#clock-reset").addEventListener("click", function () {
      apiGet("/api/config").then(function (cfg) {
        var c = cfg.clock || {};
        $$("[data-clock]").forEach(function (el) {
          var key = el.getAttribute("data-clock");
          if (!(key in c)) return;
          if (el.type === "checkbox") { el.checked = !!c[key]; }
          else if (el.type === "color") { el.value = rgbToHex(c[key]); }
          else { el.value = c[key]; }
        });
        if (typeof cfg.display.tint === "string") {
          $("#ck-tint").value = cfg.display.tint;
        }
        $$(".align-btn").forEach(function (b) {
          b.classList.toggle("active",
            b.getAttribute("data-align") === c.align);
        });
        updateVisibility();
        refreshPreview();
        toast("Reverted to saved settings");
      });
    });

    updateVisibility();
    refreshPreview();
  }

  // ---- library ---------------------------------------------------------------

  function initLibrary() {
    var lib = null;
    var tint = "amber";

    function toggleItem(kind, id, enabled, after) {
      apiPost("/api/library/toggle", { kind: kind, id: id, enabled: enabled })
        .then(function (r) {
          if (!r.ok) { toast(r.error || "toggle failed", true); return; }
          toast((enabled ? "Enabled " : "Disabled ") + (id || "all"));
          if (after) after();
        }).catch(function () { toast("toggle failed", true); });
    }

    function playNow(type, id) {
      apiPost("/api/control/play", { type: type, id: id }).then(function (r) {
        toast(r.ok ? "Playing " + id : (r.error || "player offline"), !r.ok);
      }).catch(function () { toast("player offline", true); });
    }

    function el(tag, cls, text) {
      var e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text !== undefined) e.textContent = text;
      return e;
    }

    function checkbox(checked, onChange) {
      var cb = el("input", "lib-check");
      cb.type = "checkbox";
      cb.checked = checked;
      cb.addEventListener("click", function (ev) { ev.stopPropagation(); });
      cb.addEventListener("change", function () { onChange(cb.checked); });
      return cb;
    }

    function animRow(game, anim) {
      var row = el("div", "lib-item");
      var img = el("img");
      img.loading = "lazy";
      img.src = "/api/preview/anim/" + encodeURIComponent(game) + "/" +
                encodeURIComponent(anim.name) + ".gif?tint=" +
                encodeURIComponent(tint);
      img.alt = anim.name;
      row.appendChild(img);
      var meta = el("div", "lib-item-meta");
      var nm = el("div", "lib-item-name", anim.name);
      var badge = el("span", "clock-badge " +
        (anim.clock_type === "ClockOnTop" ? "top" :
         anim.clock_type === "ClockBehind" ? "behind" : ""),
        anim.clock_type === "ClockOnTop" ? "clock front" :
        anim.clock_type === "ClockBehind" ? "clock back" : "no clock");
      nm.appendChild(badge);
      meta.appendChild(nm);
      meta.appendChild(el("div", "lib-item-sub",
        anim.frames + " frames · " +
        (anim.duration_ms / 1000).toFixed(1) + " s"));
      row.appendChild(meta);
      row.appendChild(checkbox(anim.enabled, function (on) {
        toggleItem("dmd_anim", game + "/" + anim.name, on);
      }));
      var play = el("button", "btn btn-sm", "Play now");
      play.addEventListener("click", function () {
        playNow("dmd", game + "/" + anim.name);
      });
      row.appendChild(play);
      return row;
    }

    function gifRow(category, file) {
      var row = el("div", "lib-item");
      var img = el("img");
      img.loading = "lazy";
      img.src = "/api/preview/gif/" + encodeURIComponent(category) + "/" +
                encodeURIComponent(file);
      img.alt = file;
      row.appendChild(img);
      var meta = el("div", "lib-item-meta");
      meta.appendChild(el("div", "lib-item-name", file));
      row.appendChild(meta);
      var play = el("button", "btn btn-sm", "Play now");
      play.addEventListener("click", function () {
        playNow("gif", category + "/" + file);
      });
      row.appendChild(play);
      return row;
    }

    function group(name, count, enabled, kind, id, buildItems) {
      var g = el("div", "lib-group" + (enabled ? "" : " disabled"));
      var head = el("div", "lib-head");
      head.appendChild(checkbox(enabled, function (on) {
        toggleItem(kind, id, on, function () {
          g.classList.toggle("disabled", !on);
        });
      }));
      head.appendChild(el("span", "lib-name", name));
      head.appendChild(el("span", "lib-count", count + " items"));
      head.appendChild(el("span", "lib-caret", "▶"));
      g.appendChild(head);
      var items = el("div", "lib-items");
      g.appendChild(items);
      var built = false;
      head.addEventListener("click", function () {
        g.classList.toggle("open");
        if (!built && g.classList.contains("open")) {
          built = true;
          buildItems(items);
        }
      });
      return g;
    }

    function render() {
      $("#dmd-count").textContent =
        "(" + lib.dmd.enabled + "/" + lib.dmd.total + ")";
      $("#gif-count").textContent =
        "(" + lib.gif.enabled + "/" + lib.gif.total + ")";
      var dmdList = $("#dmd-list");
      dmdList.innerHTML = "";
      lib.dmd.games.forEach(function (game) {
        dmdList.appendChild(group(
          game.game, game.count, game.enabled, "dmd_game", game.game,
          function (container) {
            game.animations.forEach(function (anim) {
              container.appendChild(animRow(game.game, anim));
            });
          }));
      });
      if (!lib.dmd.games.length) {
        dmdList.appendChild(el("div", "loading", "No DMD library found."));
      }
      var gifList = $("#gif-list");
      gifList.innerHTML = "";
      lib.gif.categories.forEach(function (cat) {
        gifList.appendChild(group(
          cat.category, cat.count, cat.enabled, "gif_category", cat.category,
          function (container) {
            cat.files.forEach(function (file) {
              container.appendChild(gifRow(cat.category, file));
            });
          }));
      });
      if (!lib.gif.categories.length) {
        gifList.appendChild(el("div", "loading", "No GIF library found."));
      }
    }

    function load() {
      apiGet("/api/library").then(function (data) {
        lib = data;
        tint = data.tint || "amber";
        render();
      }).catch(function () {
        $("#dmd-list").innerHTML =
          "<div class='loading'>Failed to load library.</div>";
      });
    }

    // tabs
    $$(".tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        $$(".tab").forEach(function (t) { t.classList.remove("active"); });
        tab.classList.add("active");
        $("#pane-dmd").hidden = tab.getAttribute("data-tab") !== "dmd";
        $("#pane-gif").hidden = tab.getAttribute("data-tab") !== "gif";
      });
    });

    // bulk enable/disable
    $$("[data-bulk]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var kind = btn.getAttribute("data-bulk");
        var enabled = btn.getAttribute("data-enabled") === "true";
        var what = kind === "dmd_all" ? "DMD games" : "GIF categories";
        if (!window.confirm((enabled ? "Enable" : "Disable") +
            " ALL " + what + "?")) return;
        toggleItem(kind, "", enabled, load);
      });
    });

    load();
  }

  // ---- simple form pages -------------------------------------------------------

  function initPlayback() {
    var share = $("#dmd-share");
    var label = $("#share-label");
    function updateShare() {
      label.textContent = share.value + "% DMD / " +
        (100 - share.value) + "% GIF";
    }
    share.addEventListener("input", updateShare);
    updateShare();
    $("#playback-save").addEventListener("click", function () {
      saveConfig(gatherCfg($("#playback-form")), "Playback saved");
    });
  }

  function initMessage() {
    $("#message-save").addEventListener("click", function () {
      saveConfig(gatherCfg($("#message-form")), "Message settings saved");
    });
    $("#msg-send").addEventListener("click", function () {
      var text = $("#msg-text").value;
      if (!text) { toast("Nothing to send", true); return; }
      apiPost("/api/control/marquee", { text: text }).then(function (r) {
        toast(r.ok ? "Message sent to panel" : (r.error || "player offline"),
              !r.ok);
      }).catch(function () { toast("player offline", true); });
    });
  }

  var DAY_CURVE = [5, 5, 5, 5, 5, 5, 10, 20, 30, 40, 50, 60,
                   60, 60, 60, 60, 60, 60, 50, 40, 30, 20, 10, 5];
  var NIGHT_CURVE = [5, 5, 5, 5, 5, 5, 5, 10, 15, 20, 25, 30,
                     30, 30, 30, 30, 30, 30, 25, 20, 15, 10, 5, 5];

  function initSchedule() {
    function bindBars() {
      $$(".bright-hour").forEach(function (slider) {
        slider.addEventListener("input", function () {
          slider.parentNode.querySelector(".bar-val").textContent =
            slider.value;
        });
      });
    }
    function applyCurve(curve) {
      $$(".bright-hour").forEach(function (slider) {
        var h = parseInt(slider.getAttribute("data-hour"), 10);
        slider.value = curve[h];
        slider.parentNode.querySelector(".bar-val").textContent = curve[h];
      });
    }
    bindBars();
    $("#preset-day").addEventListener("click", function () {
      applyCurve(DAY_CURVE);
    });
    $("#preset-night").addEventListener("click", function () {
      applyCurve(NIGHT_CURVE);
    });
    $("#schedule-save").addEventListener("click", function () {
      var body = gatherCfg($("#schedule-form"));
      var hours = [];
      $$(".bright-hour").forEach(function (slider) {
        hours[parseInt(slider.getAttribute("data-hour"), 10)] =
          parseInt(slider.value, 10);
      });
      body.display = { brightness_by_hour: hours };
      saveConfig(body, "Schedule saved");
    });
  }

  function initNetwork() {
    bindReveal("#psk-reveal", "#wifi-psk");
    $("#network-save").addEventListener("click", function () {
      saveConfig(gatherCfg($("#network-form")),
                 "Network settings saved — applied at next startup");
    });
  }

  function initSystem() {
    bindReveal("#pass-reveal", "#web-pass");
    startStatusPolling(function (st) {
      var badge = $("#sys-state");
      badge.textContent = st.state || "offline";
      badge.className = "badge " +
        (st.state && st.state !== "offline" ? "on" : "");
      $("#sys-uptime").textContent =
        st.uptime_s !== undefined ? "up " + fmtUptime(st.uptime_s) : "";
    });
    $("#auth-save").addEventListener("click", function () {
      saveConfig(gatherCfg($("#auth-form")), "Web access saved");
    });
    $("#log-refresh").addEventListener("click", function () {
      var unit = $("#log-unit").value;
      $("#log-view").textContent = "Loading…";
      apiGet("/api/logs?unit=" + encodeURIComponent(unit))
        .then(function (data) {
          $("#log-view").textContent = data.text || "(empty)";
        }).catch(function () {
          $("#log-view").textContent = "Failed to load logs.";
        });
    });
    $("#restore-upload").addEventListener("click", function () {
      var input = $("#restore-file");
      if (!input.files || !input.files.length) {
        toast("Choose a backup file first", true);
        return;
      }
      var fd = new FormData();
      fd.append("file", input.files[0]);
      fetch("/api/restore", { method: "POST", body: fd })
        .then(function (r) { return r.json(); })
        .then(function (r) {
          if (r.ok) {
            toast("Settings restored");
            setTimeout(function () { location.reload(); }, 1200);
          } else {
            toast(r.error || "Restore failed", true);
          }
        }).catch(function () { toast("Restore failed", true); });
    });
    $("#factory-reset").addEventListener("click", function () {
      if (!window.confirm("Reset ALL settings to factory defaults?")) return;
      apiPost("/api/factory_reset").then(function (r) {
        if (r.ok) {
          toast("Settings reset to defaults");
          setTimeout(function () { location.reload(); }, 1200);
        } else { toast("Reset failed", true); }
      }).catch(function () { toast("Reset failed", true); });
    });
  }

  // ---- dispatch --------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    bindControlButtons();
    bindQuickToggles();
    var page = document.body.getAttribute("data-page");
    if (page === "dashboard") initDashboard();
    else if (page === "clock") initClock();
    else if (page === "library") initLibrary();
    else if (page === "playback") initPlayback();
    else if (page === "message") initMessage();
    else if (page === "schedule") initSchedule();
    else if (page === "network") initNetwork();
    else if (page === "system") initSystem();
  });
})();
