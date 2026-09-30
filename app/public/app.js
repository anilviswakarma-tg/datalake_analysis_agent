/* Data Lake Agent - Chainlit page additions that keep the Streamlit layout:
   the landing hero with agent tiles and per-agent question tiles, the sidebar
   logo / AGENTS list / signed-in footer, and the title bar.

   Everything hangs off the element ids Chainlit gives its own components
   (#welcome-screen, #header, #chat-input, #chat-submit, #new-chat-button,
   #thread-history). Class names are not used: they change between releases.
   tests/test_chainlit_app.py checks the ids still exist in the bundled
   frontend, so an upgrade that renames one fails there first.

   apply() is idempotent and runs on every DOM change, because React
   re-creates these parts of the page as the user moves between chats. */
(function () {
  "use strict";

  var BASE = (document.currentScript && document.currentScript.src || "/public/app.js")
    .replace(/public\/app\.js.*$/, "");
  var STORE_KEY = "datalake.selectedAgent";
  var PLACEHOLDER = "Ask about catalogue, plays, or store metrics…";

  var agents = [];
  var user = null;
  var bypassed = false;     // signed in by the local DEV_SKIP_AUTH bypass
  var selected = null;
  try { selected = sessionStorage.getItem(STORE_KEY); } catch (e) { /* private mode */ }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function icon(name, cls) {
    return el("span", "dl-icon " + (cls || ""), name);
  }

  function agentByKey(key) {
    for (var i = 0; i < agents.length; i++) if (agents[i].key === key) return agents[i];
    return null;
  }

  function select(key) {
    selected = agentByKey(key) ? key : null;
    try {
      if (selected) sessionStorage.setItem(STORE_KEY, selected);
      else sessionStorage.removeItem(STORE_KEY);
    } catch (e) { /* ignore */ }
    var landing = document.querySelector(".dl-landing");
    if (landing) landing.remove();          // redrawn by apply()
    apply();
  }

  // Send a question through the real composer, so it goes with whichever
  // model is picked there (Chainlit's own starters always send the default).
  function ask(question) {
    var input = document.getElementById("chat-input");
    if (!input) return;
    var setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
    setter.call(input, question);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    setTimeout(function () {
      var submit = document.getElementById("chat-submit");
      if (submit && !submit.disabled) submit.click();
    }, 50);
  }

  // ── landing: hero + agent tiles, or one agent's question tiles ──────────
  function buildLanding() {
    var root = el("div", "dl-landing");
    var agent = agentByKey(selected);
    if (!agent) {
      root.appendChild(el("div", "dl-hero-badge", "DATA LAKE AGENT"));
      root.appendChild(el("div", "dl-hero-title", "Ask your data lake anything"));
      root.appendChild(el("div", "dl-hero-desc",
        "Choose an agent below (or from the left sidebar) to see its suggested " +
        "questions, or just type your own at the bottom."));
      var grid = el("div", "dl-tiles dl-agent-tiles");
      agents.forEach(function (a) {
        var b = el("button", "dl-tile");
        b.type = "button";
        b.appendChild(icon(a.icon));
        var text = el("span", "dl-tile-text");
        text.appendChild(el("strong", null, a.label));
        text.appendChild(el("span", "dl-tile-desc", a.desc));
        b.appendChild(text);
        b.addEventListener("click", function () { select(a.key); });
        grid.appendChild(b);
      });
      root.appendChild(grid);
    } else {
      var back = el("button", "dl-back", "‹  All agents");
      back.type = "button";
      back.addEventListener("click", function () { select(null); });
      root.appendChild(back);
      root.appendChild(el("div", "dl-section-title", agent.label));
      root.appendChild(el("div", "dl-section-sub",
        "Pick a question to run, or type your own at the bottom."));
      var qgrid = el("div", "dl-tiles dl-question-tiles");
      agent.questions.forEach(function (q) {
        var b = el("button", "dl-tile");
        b.type = "button";
        b.appendChild(icon(agent.icon));
        b.appendChild(el("span", "dl-tile-text", q));
        b.addEventListener("click", function () { ask(q); });
        qgrid.appendChild(b);
      });
      root.appendChild(qgrid);
    }
    root.dataset.agent = agent ? agent.key : "";
    return root;
  }

  function applyLanding() {
    var welcome = document.getElementById("welcome-screen");
    if (!welcome || !agents.length) return;
    var current = welcome.querySelector(":scope > .dl-landing");
    if (current && current.dataset.agent === (selected || "")) return;
    if (current) current.remove();
    // First child: React only ever appends after or removes its own nodes.
    welcome.insertBefore(buildLanding(), welcome.firstChild);
  }

  // ── sidebar: logo, AGENTS list, signed-in footer ────────────────────────
  function applySidebar() {
    var history = document.getElementById("thread-history");
    var sidebar = history && history.closest('[data-sidebar="sidebar"]');
    if (!sidebar || !agents.length) return;

    var nav = sidebar.querySelector(".dl-sb-nav");
    if (!nav) {
      nav = el("div", "dl-sb-nav");
      var logo = el("div", "dl-sb-logo");
      var img = el("img");
      img.src = BASE + "logo?theme=dark";
      img.alt = "Tuned Global";
      logo.appendChild(img);
      nav.appendChild(logo);
      nav.appendChild(el("div", "dl-sb-label", "Agents"));
      agents.forEach(function (a) {
        var b = el("button", "dl-sb-item");
        b.type = "button";
        b.dataset.agent = a.key;
        b.appendChild(icon(a.icon));
        b.appendChild(el("span", null, a.label));
        b.addEventListener("click", function () { openAgent(a.key); });
        nav.appendChild(b);
      });
      nav.appendChild(el("div", "dl-sb-label", "Chats"));
      history.parentNode.insertBefore(nav, history);
    }
    nav.querySelectorAll(".dl-sb-item").forEach(function (b) {
      b.classList.toggle("dl-active", b.dataset.agent === selected);
    });

    if (user && !sidebar.querySelector(".dl-sb-footer")) {
      var footer = el("div", "dl-sb-footer");
      var who = el("div", "dl-sb-user");
      who.title = user;
      who.appendChild(icon("person"));
      who.appendChild(el("span", null, user));
      footer.appendChild(who);
      var out = el("button", "dl-sb-signout", "Sign out");
      out.type = "button";
      out.addEventListener("click", signOut);
      footer.appendChild(out);
      sidebar.appendChild(footer);
    }
  }

  // Picking an agent from the sidebar mid-conversation starts a new chat on
  // that agent's questions; the conversation stays in the chat list.
  function openAgent(key) {
    if (document.getElementById("welcome-screen")) { select(key); return; }
    select(key);
    var newChat = document.getElementById("new-chat-button");
    if (newChat) newChat.click();
  }

  function signOut() {
    fetch(BASE + "logout", { method: "POST", credentials: "include" })
      .catch(function () { /* signing out anyway */ })
      .then(function () { window.location.href = BASE + "login"; });
  }

  // ── title bar and composer ──────────────────────────────────────────────
  function applyHeader() {
    var header = document.getElementById("header");
    if (header && !header.querySelector(".dl-title")) {
      var left = header.firstElementChild;
      if (left) left.appendChild(el("div", "dl-title", "🎵 Data Lake Agent"));
    }
    // Permanent, as in the Streamlit app: a toast is too easy to miss for
    // "anyone can use this without signing in".
    if (header && bypassed && !header.querySelector(".dl-bypass")) {
      var banner = el("div", "dl-bypass");
      banner.appendChild(el("strong", null, "🔓 Auth bypassed"));
      banner.appendChild(document.createTextNode(" — "));
      banner.appendChild(el("code", null, "DEV_SKIP_AUTH=true"));
      banner.appendChild(document.createTextNode(
        " is set. Local development only; unset it in "));
      banner.appendChild(el("code", null, ".env"));
      banner.appendChild(document.createTextNode(" to restore the login screen."));
      banner.title = banner.textContent;       // full text when it is cut short
      header.appendChild(banner);
    }
    var input = document.getElementById("chat-input");
    if (input && input.getAttribute("placeholder") !== PLACEHOLDER) {
      input.setAttribute("placeholder", PLACEHOLDER);
    }
  }

  // Opening an earlier chat from the list leaves the agent view.
  document.addEventListener("click", function (e) {
    var t = e.target.closest && e.target.closest('[id^="thread-"]');
    if (t && t.id !== "thread-history" && selected) select(null);
  }, true);

  // ── saving a chat from the retention sweep (chainlit_app.py) ────────────
  // Unsaved chats are deleted after RETENTION_DAYS without activity. The
  // open chat gets a Save button in the title bar; saved ones get a star in
  // the chat list, drawn by a generated stylesheet so Chainlit's own list
  // items are never touched.
  var saved = null;            // Set of saved chat ids, once loaded
  var retentionDays = 0;
  var starSheet = null;

  function currentChatId() {
    var m = window.location.pathname.match(/\/thread\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }

  function takeSavedState(state) {
    if (!state || !state.saved) return;
    saved = new Set(state.saved);
    retentionDays = state.retention_days || 0;
    if (!starSheet) {
      starSheet = el("style");
      document.head.appendChild(starSheet);
    }
    starSheet.textContent = Array.from(saved).map(function (id) {
      return '[id="thread-' + id.replace(/["\\]/g, "") + '"] button > span::before';
    }).join(",\n") + (saved.size ? " { content: \"\\2605\"; color: #E85420; }" : "");
    var b = document.querySelector(".dl-save");
    if (b) b.remove();                      // redrawn by apply()
    schedule();
  }

  function postSaved(path, body) {
    return fetch(BASE + "datalake/saved" + path, {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.ok ? r.json() : null; });
  }

  function applySaveButton() {
    var header = document.getElementById("header");
    var id = currentChatId();
    var existing = header && header.querySelector(".dl-save");
    if (!header || !saved || !retentionDays || !id) {
      if (existing) existing.remove();
      return;
    }
    var isSaved = saved.has(id);
    if (existing && existing.dataset.chat === id && existing.dataset.saved === String(isSaved)) return;
    if (existing) existing.remove();
    var b = el("button", "dl-save" + (isSaved ? " dl-saved" : ""),
               isSaved ? "\u2605 Saved" : "\u2606 Save chat");
    b.type = "button";
    b.dataset.chat = id;
    b.dataset.saved = String(isSaved);
    b.title = isSaved
      ? "Kept until you unsave it."
      : "Unsaved chats are deleted after " + retentionDays + " days without activity.";
    b.addEventListener("click", function () {
      b.disabled = true;
      setSaved(id, !isSaved).catch(function () { b.disabled = false; });
    });
    header.appendChild(b);
  }

  function setSaved(id, save) {
    return postSaved("/" + encodeURIComponent(id), { saved: save }).then(takeSavedState);
  }

  // The same toggle in each chat's ⋯ menu in the sidebar. The menu opens in
  // a portal with no link back to its chat, so remember which chat's ⋯ was
  // pressed (Radix opens on pointerdown).
  var menuChatId = null;
  document.addEventListener("pointerdown", function (e) {
    var opts = e.target.closest && e.target.closest("#thread-options");
    // parentElement: the ⋯ button's own id is "thread-options"
    var item = opts && opts.parentElement.closest('[id^="thread-"]');
    if (item) menuChatId = item.id.replace(/^thread-/, "");
  }, true);

  function applyChatMenu() {
    var rename = document.getElementById("rename-thread");
    if (!rename || !saved || !retentionDays || !menuChatId) return;
    var menu = rename.parentNode;
    var id = menuChatId;
    var isSaved = saved.has(id);
    var existing = menu.querySelector(":scope > .dl-menu-save");
    if (existing && existing.dataset.chat === id && existing.dataset.saved === String(isSaved)) return;
    if (existing) existing.remove();
    var item = el("div", rename.className + " dl-menu-save");
    item.setAttribute("role", "menuitem");
    item.tabIndex = -1;
    item.dataset.chat = id;
    item.dataset.saved = String(isSaved);
    item.title = rename.title;
    item.appendChild(el("span", null, isSaved ? "Unsave" : "Save"));
    item.appendChild(el("span", "dl-menu-star", isSaved ? "★" : "☆"));
    item.addEventListener("click", function (e) {
      e.stopPropagation();
      setSaved(id, !isSaved).catch(function () { /* unchanged */ });
      closeMenu(menu);
    });
    menu.insertBefore(item, menu.firstChild);
  }

  // Our item isn't one of Radix's, so it can't close the menu for us, and a
  // menu left open keeps the page blocked (Radix disables pointer events
  // outside it). Escape first; if that didn't take, a press outside it.
  function closeMenu(menu) {
    var target = menu.contains(document.activeElement) ? document.activeElement : menu;
    target.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    setTimeout(function () {
      if (!menu.isConnected) return;
      document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    }, 50);
  }

  var queued = false;
  function apply() {
    queued = false;
    applyHeader();
    applySaveButton();
    applyChatMenu();
    applySidebar();
    applyLanding();
  }
  function schedule() {
    if (!queued) { queued = true; requestAnimationFrame(apply); }
  }

  fetch(BASE + "public/agents.json", { credentials: "include" })
    .then(function (r) { return r.json(); })
    .then(function (data) { agents = data; if (!agentByKey(selected)) selected = null; schedule(); })
    .catch(function () { /* page still works with Chainlit's own layout */ });
  // Sign-in can still be completing when this first runs, so retry a few
  // times before giving up on the footer.
  function loadUser(attempt) {
    fetch(BASE + "user", { credentials: "include" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (u) {
        user = u && (u.display_name || u.identifier);
        bypassed = !!(u && u.metadata && u.metadata.auth_method === "dev-bypass");
        if (user) {
          schedule();
          // 404 when chat history is off: no Save button then.
          postSaved("").then(takeSavedState).catch(function () { /* no saving */ });
        } else if (attempt < 5) setTimeout(function () { loadUser(attempt + 1); }, 1500);
      })
      .catch(function () { /* no footer */ });
  }
  loadUser(0);

  // Icons stay hidden until their font is in, so a slow or blocked font
  // leaves an empty square rather than the icon's name in plain text.
  if (document.fonts && document.fonts.load) {
    document.fonts.load('24px "Material Symbols Rounded"', "library_music")
      .then(function (faces) {
        if (faces.length) document.documentElement.classList.add("dl-icons-ready");
      })
      .catch(function () { /* stay hidden */ });
  } else {
    document.documentElement.classList.add("dl-icons-ready");
  }

  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  schedule();
})();
