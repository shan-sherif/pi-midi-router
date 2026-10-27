/* Pi MIDI Router dashboard. Polls the Pi twice a second; every change is sent
   straight away and saved on the Pi, so any device on the network sees it. */
(function () {
  "use strict";

  var POLL_MS = 500;
  var TYPES = {
    notes:      { label: "Notes", desc: "Pressing and releasing keys or pads." },
    cc:         { label: "CC", desc: "Control Change: knobs, faders, mod wheel and sustain pedal." },
    program:    { label: "Program change", desc: "Switching to another patch or preset." },
    pitchbend:  { label: "Pitch bend", desc: "The pitch-bend wheel or strip." },
    aftertouch: { label: "Aftertouch", desc: "Pressing harder on a key after it's down." },
    clock:      { label: "Clock & transport", desc: "Tempo clock, start, stop, continue and song position, to keep devices in sync." },
    sysex:      { label: "SysEx", desc: "System Exclusive: patch dumps, firmware updates and device settings." },
    other:      { label: "Other", desc: "Rarely needed: MIDI time code, active sensing, tune request and reset." }
  };
  var CABLES = 8;

  var state = { inputs: [], outputs: [], routes: [], types: Object.keys(TYPES), hidden_types: [] };
  var seq = 0;
  var routesSig = "";
  var prevCounts = {};      // "in:key" / "out:key" -> count
  var prevHits = {};        // route id -> hits
  var monitorPaused = false;
  var failures = 0;

  var $ = function (id) { return document.getElementById(id); };

  /* ---------- tiny DOM helper (never uses innerHTML with device names) ---------- */
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v == null || v === false) return;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
        else if (k === "style") el.setAttribute("style", v);
        else if (k in el && typeof v !== "string") el[k] = v;
        else el.setAttribute(k, v === true ? "" : v);
      });
    }
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c == null) continue;
      if (Array.isArray(c)) c.forEach(function (x) { if (x) el.appendChild(x); });
      else el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return el;
  }

  /* ---------- API ---------- */
  function api(method, path, body) {
    var opts = { method: method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error(data.error || ("Request failed (" + r.status + ")"));
        return data;
      });
    });
  }

  var toastTimer;
  function toast(msg) {
    var t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, 3500);
  }

  /* ---------- modal (replaces prompt/confirm) ----------
     Resolves with the typed text when opts.value is given, otherwise true;
     resolves with null when cancelled. */
  var modalDone = null, modalReturn = null;
  function ask(opts) {
    if (modalDone) closeModal(null);
    $("modal-title").textContent = opts.title;
    $("modal-text").textContent = opts.text;
    var input = $("modal-input");
    input.hidden = opts.value === undefined;
    input.value = opts.value || "";
    var ok = $("modal-ok");
    ok.textContent = opts.ok || "OK";
    ok.className = "btn " + (opts.danger ? "btn-danger" : "btn-dark");
    modalReturn = document.activeElement;
    $("modal").style.display = "block";
    if (input.hidden) ok.focus(); else { input.focus(); input.select(); }
    return new Promise(function (resolve) { modalDone = resolve; });
  }

  function closeModal(result) {
    $("modal").style.display = "none";
    var done = modalDone;
    modalDone = null;
    if (modalReturn && document.contains(modalReturn)) modalReturn.focus();
    if (done) done(result);
  }

  $("modal-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    closeModal($("modal-input").hidden ? true : $("modal-input").value);
  });
  $("modal-cancel").addEventListener("click", function () { closeModal(null); });
  $("modal-x").addEventListener("click", function () { closeModal(null); });
  $("modal").addEventListener("click", function (ev) { if (ev.target === ev.currentTarget) closeModal(null); });
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape" && modalDone) closeModal(null); });

  function labelFor(kind, key) {
    var list = state[kind];
    for (var i = 0; i < list.length; i++) {
      if (list[i].key === key) return list[i].alias || list[i].name;
    }
    return key;
  }

  /* ---------- polling ---------- */
  function poll() {
    fetch("/api/state?since=" + seq, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (d) { throw new Error(d.error || r.status); });
        return r.json();
      })
      .then(function (data) {
        failures = 0;
        $("link-state").textContent = "";
        $("host").textContent = data.hostname + " · " + location.host;
        renderPower(data.power);
        state = data;
        renderDevices();
        renderGlossary();
        var sig = JSON.stringify(data.routes.map(function (r) { var c = Object.assign({}, r); delete c.hits; return c; }))
          + "|" + JSON.stringify(data.inputs.map(function (d) { return [d.key, d.alias, d.online]; }))
          + "|" + JSON.stringify(data.outputs.map(function (d) { return [d.key, d.alias, d.online]; }))
          + "|" + state.hidden_types.join(",");
        if (sig !== routesSig && !editing()) {
          routesSig = sig;
          renderRoutes();
        }
        updateHits();
        appendMonitor(data.monitor);
        seq = data.seq;
      })
      .catch(function (err) {
        failures++;
        if (failures > 1) $("link-state").textContent = "Can't reach the Pi — retrying";
        if (String(err.message).indexOf("local network") >= 0) $("link-state").textContent = err.message;
      })
      .then(function () { setTimeout(poll, POLL_MS); });
  }

  // Don't redraw routes while someone is choosing from a dropdown / typing.
  function editing() {
    var a = document.activeElement;
    return a && $("route-list").contains(a) && (a.tagName === "SELECT" || a.tagName === "INPUT" && a.type === "number");
  }

  /* ---------- power (Raspberry Pi low-voltage warning) ---------- */
  var POWER = {
    ok: { cls: "ok", text: "Power OK",
      tip: "The Pi has had enough power since it started." },
    earlier: { cls: "warn", text: "Low voltage earlier",
      tip: "The Pi ran short of power at some point since it started. If devices dropped out, use the official power supply or a powered USB hub. This clears when the Pi restarts." },
    now: { cls: "bad", text: "Low voltage",
      tip: "The Pi isn't getting enough power right now, so devices may disconnect. Use the official power supply or a powered USB hub." }
  };

  function renderPower(p) {
    var el = $("power");
    if (!p) { el.hidden = true; return; }
    var s = p.low_now ? POWER.now : p.low_since_boot ? POWER.earlier : POWER.ok;
    el.className = "power " + s.cls;
    el.textContent = s.text;
    el.title = s.tip;
    el.hidden = false;
  }

  /* ---------- devices ---------- */
  function flash(led) {
    led.classList.add("lit");
    clearTimeout(led._t);
    led._t = setTimeout(function () { led.classList.remove("lit"); }, 180);
  }

  function renderDevices() {
    [["inputs", "in"], ["outputs", "out"]].forEach(function (pair) {
      var kind = pair[0], tag = pair[1];
      var ul = $(kind);
      var list = state[kind];
      var keys = list.map(function (d) { return d.key + d.alias + d.online; }).join("|");
      if (ul._keys !== keys) {
        ul._keys = keys;
        ul.textContent = "";
        if (!list.length) {
          ul.appendChild(h("li", { class: "empty", text:
            kind === "inputs" ? "Nothing connected yet. Plug a MIDI device into the Pi's USB port — it appears here within a few seconds."
                              : "No devices to send to yet." }));
        }
        list.forEach(function (d) {
          var led = h("span", { class: "led" + (d.online ? " online" : ""), title: d.online ? "Connected" : "Unplugged" });
          var tools = h("div", { class: "tools" },
            h("button", { type: "button", class: "btn", text: "Rename", title: "Give this device a nickname",
              onclick: function () { rename(d); } }),
            d.online ? null : h("button", { type: "button", class: "btn", text: "Forget", title: "Remove from this list",
              onclick: function () { forget(kind, d); } })
          );
          var li = h("li", { class: "device" + (d.online ? "" : " offline") },
            led,
            h("div", { class: "names" },
              h("div", { class: "label" }, d.alias || d.name,
                d.online ? null : h("span", { class: "tag tag-red", text: "Unplugged" })),
              d.alias ? h("div", { class: "raw", text: d.name }) : null
            ),
            tools);
          ul.appendChild(li);
        });
      }
      // activity LEDs
      var items = ul.querySelectorAll(".device");
      list.forEach(function (d, i) {
        var id = tag + ":" + d.key;
        if (prevCounts[id] !== undefined && d.count > prevCounts[id] && items[i]) {
          flash(items[i].querySelector(".led"));
        }
        prevCounts[id] = d.count;
      });
    });
  }

  function rename(d) {
    ask({ title: "Rename", text: "Nickname for " + d.name + " (leave empty to reset):", value: d.alias || "", ok: "Save" })
      .then(function (name) {
        if (name === null) return;
        api("POST", "/api/alias", { key: d.key, alias: name }).catch(function (e) { toast(e.message); });
      });
  }

  function forget(kind, d) {
    var used = state.routes.some(function (r) { return r.src === d.key || r.dst === d.key; });
    if (used) { toast("Remove the routes that use " + (d.alias || d.name) + " first."); return; }
    api("POST", "/api/forget", { kind: kind, key: d.key }).catch(function (e) { toast(e.message); });
  }

  /* ---------- glossary (message types; hiding one blocks it on every route) ---------- */
  function renderGlossary() {
    var ul = $("glossary");
    var sig = state.types.join(",") + "|" + state.hidden_types.join(",");
    if (ul._sig === sig) return;
    ul._sig = sig;
    ul.textContent = "";
    state.types.forEach(function (t) {
      var info = TYPES[t] || { label: t, desc: "" };
      var hidden = state.hidden_types.indexOf(t) >= 0;
      ul.appendChild(h("li", { class: "gloss-item" + (hidden ? " is-hidden" : "") },
        h("div", { class: "names" },
          h("div", { class: "label" }, info.label,
            hidden ? h("span", { class: "tag tag-grey", text: "Hidden" }) : null),
          h("div", { class: "raw", text: info.desc })),
        h("label", { class: "switch", title: hidden ? "Show on routes" : "Hide and block on every route" },
          h("input", { type: "checkbox", checked: !hidden, "aria-label": "Show " + info.label,
            onchange: function (ev) { setHidden(t, !ev.target.checked, ev.target); } }),
          h("span", { class: "track" }))));
    });
  }

  function setHidden(t, hide, box) {
    var label = (TYPES[t] || { label: t }).label;
    var used = state.routes.some(function (r) { return r.types.indexOf(t) >= 0; });
    var go = hide && used
      ? ask({ title: "Hide " + label, ok: "Hide and block", danger: true,
              text: label + " will be hidden and blocked on every route. Showing it again lets it through on every route." })
      : Promise.resolve(true);
    go.then(function (yes) {
      if (!yes) { box.checked = !hide; return; }
      var hidden = state.hidden_types.filter(function (x) { return x !== t; });
      if (hide) hidden.push(t);
      api("POST", "/api/hidden-types", { types: hidden })
        .then(function (res) { state.hidden_types = res.hidden_types; renderGlossary(); routesSig = ""; })
        .catch(function (e) { box.checked = !hide; toast(e.message); });
    });
  }

  /* ---------- routes ---------- */
  function deviceSelect(kind, value, onchange, label) {
    var sel = h("select", { class: "field", "aria-label": label, onchange: onchange });
    var found = false;
    state[kind].forEach(function (d) {
      var opt = h("option", { value: d.key, text: (d.alias || d.name) + (d.online ? "" : " (unplugged)") });
      if (d.key === value) { opt.selected = true; found = true; }
      sel.appendChild(opt);
    });
    if (!found) sel.appendChild(h("option", { value: value, text: value + " (unplugged)", selected: true }));
    var dev = state[kind].filter(function (d) { return d.key === value; })[0];
    if (!dev || !dev.online) sel.classList.add("missing");
    return sel;
  }

  function channelSelect(value, anyLabel, onchange, label) {
    var sel = h("select", { class: "field", "aria-label": label, onchange: onchange });
    sel.appendChild(h("option", { value: "", text: anyLabel, selected: value == null }));
    for (var c = 1; c <= 16; c++) sel.appendChild(h("option", { value: String(c), text: "Channel " + c, selected: value === c }));
    return sel;
  }

  function update(route, patch) {
    Object.assign(route, patch);
    return api("PUT", "/api/routes/" + encodeURIComponent(route.id), patch)
      .catch(function (e) { toast(e.message); routesSig = ""; });
  }

  function renderRoutes() {
    var list = $("route-list");
    list.textContent = "";
    if (!state.routes.length) {
      list.appendChild(h("div", { class: "routes-empty" },
        h("p", { text: state.inputs.length
          ? "No routes yet. Add one to start sending MIDI from one device to another."
          : "Plug in your MIDI devices, then add a route to connect them." })));
      return;
    }
    var shown = state.types.filter(function (t) { return state.hidden_types.indexOf(t) < 0; });
    state.routes.forEach(function (r, i) {
      var switches = shown.map(function (t) {
        var info = TYPES[t] || { label: t, desc: "" };
        return h("label", { class: "switch type-switch", title: info.desc },
          h("input", { type: "checkbox", checked: r.types.indexOf(t) >= 0, onchange: function (ev) {
            var types = r.types.filter(function (x) { return x !== t; });
            if (ev.target.checked) types.push(t);
            update(r, { types: types });
          } }),
          h("span", { class: "track" }), info.label);
      });

      var enabled = h("input", { type: "checkbox", checked: r.enabled, onchange: function (ev) {
        card.classList.toggle("disabled", !ev.target.checked);
        update(r, { enabled: ev.target.checked });
      } });

      var card = h("article", { class: "route" + (r.enabled ? "" : " disabled"),
          style: "--cable: var(--c" + (i % CABLES) + ")", "aria-label": "Route " + (i + 1) },
        h("div", { class: "patch" },
          h("div", { class: "jack" },
            h("span", { class: "jack-label", text: "From" }),
            h("div", { class: "row" },
              deviceSelect("inputs", r.src, function (ev) { update(r, { src: ev.target.value }); routesSig = ""; }, "Source device"),
              channelSelect(r.src_channel, "All channels", function (ev) { update(r, { src_channel: ev.target.value || null }); }, "Source channel"))),
          h("div", { class: "cable", "aria-hidden": "true" },
            h("span", { class: "wire" }), h("span", { class: "plug l" }), h("span", { class: "plug r" }), h("span", { class: "pulse" })),
          h("div", { class: "jack" },
            h("span", { class: "jack-label", text: "To" }),
            h("div", { class: "row" },
              deviceSelect("outputs", r.dst, function (ev) { update(r, { dst: ev.target.value }); routesSig = ""; }, "Destination device"),
              channelSelect(r.dst_channel, "Same channel", function (ev) { update(r, { dst_channel: ev.target.value || null }); }, "Destination channel")))
        ),
        h("div", { class: "route-types" },
          h("span", { class: "jack-label", text: "Let through" }),
          switches.length
            ? h("div", { class: "type-grid", role: "group", "aria-label": "Message types to let through" }, switches)
            : h("p", { class: "types-none", text: "All message types are hidden. Show some under Message types." })),
        h("div", { class: "route-foot" },
          h("label", { class: "foot-ctl", title: "Shift notes up or down (semitones)" }, "Transpose",
            h("input", { type: "number", class: "field", min: "-48", max: "48", step: "1", value: String(r.transpose || 0),
              onchange: function (ev) {
                var v = parseInt(ev.target.value, 10) || 0;
                v = Math.max(-48, Math.min(48, v));
                ev.target.value = v;
                update(r, { transpose: v });
              } })),
          h("label", { class: "switch" }, enabled, h("span", { class: "track" }), "Route on"),
          h("span", { class: "hits", text: "" }),
          h("button", { type: "button", class: "btn btn-remove", text: "Remove", "aria-label": "Remove route " + (i + 1),
            onclick: function () {
              ask({ title: "Remove route", text: "Remove the route from " + labelFor("inputs", r.src) + " to " + labelFor("outputs", r.dst) + "?",
                    ok: "Remove", danger: true })
                .then(function (yes) {
                  if (!yes) return;
                  api("DELETE", "/api/routes/" + encodeURIComponent(r.id))
                    .then(function () { routesSig = ""; })
                    .catch(function (e) { toast(e.message); });
                });
            } })
        )
      );
      card.dataset.id = r.id;
      list.appendChild(card);
    });
    updateHits();
  }

  function updateHits() {
    state.routes.forEach(function (r) {
      var card = document.querySelector('.route[data-id="' + CSS.escape(r.id) + '"]');
      if (!card) return;
      card.querySelector(".hits").textContent = r.hits ? r.hits.toLocaleString() + " sent" : "";
      if (prevHits[r.id] !== undefined && r.hits > prevHits[r.id]) {
        var cable = card.querySelector(".cable");
        cable.classList.remove("firing");
        void cable.offsetWidth;
        cable.classList.add("firing");
      }
      prevHits[r.id] = r.hits;
    });
  }

  function addRoute() {
    var ins = state.inputs.filter(function (d) { return d.online; });
    var outs = state.outputs.filter(function (d) { return d.online; });
    if (!state.inputs.length || !state.outputs.length) {
      toast("Plug in at least one MIDI device first.");
      return;
    }
    var src = (ins[0] || state.inputs[0]).key;
    var dst = (outs.filter(function (d) { return d.key !== src; })[0] || outs[0] || state.outputs[0]).key;
    api("POST", "/api/routes", { src: src, dst: dst })
      .then(function () { routesSig = ""; })
      .catch(function (e) { toast(e.message); });
  }

  /* ---------- monitor ---------- */
  function appendMonitor(entries) {
    if (monitorPaused || !entries || !entries.length) return;
    var log = $("monitor-log");
    var ph = log.querySelector(".placeholder");
    if (ph) ph.remove();
    var atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
    entries.forEach(function (m) {
      var d = new Date(m.t * 1000);
      var time = d.toTimeString().slice(0, 8);
      var to = m.to.length ? "→ " + m.to.map(function (k) { return labelFor("outputs", k); }).join(", ") : "not routed";
      log.appendChild(h("li", null,
        h("span", { class: "t", text: time }),
        h("span", { class: "src", text: labelFor("inputs", m.src) }),
        h("span", { class: "msg", text: m.text }),
        h("span", { class: "to" + (m.to.length ? "" : " none"), text: to })));
    });
    while (log.children.length > 400) log.removeChild(log.firstChild);
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  function monitorPlaceholder() {
    $("monitor-log").appendChild(h("li", { class: "placeholder", text: "Play something on a connected device to see its messages here." }));
  }

  /* ---------- wire up ---------- */
  $("add-route").addEventListener("click", addRoute);
  $("power").addEventListener("click", function (ev) { toast(ev.currentTarget.title); });
  $("panic").addEventListener("click", function () {
    api("POST", "/api/panic", {})
      .then(function () { toast("Sent All Notes Off to every device."); })
      .catch(function (e) { toast(e.message); });
  });
  $("monitor-pause").addEventListener("click", function (ev) {
    monitorPaused = !monitorPaused;
    ev.currentTarget.textContent = monitorPaused ? "Resume" : "Pause";
    ev.currentTarget.setAttribute("aria-pressed", String(monitorPaused));
  });
  $("monitor-clear").addEventListener("click", function () {
    $("monitor-log").textContent = "";
    monitorPlaceholder();
  });

  monitorPlaceholder();
  poll();
})();
