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
      nav.appendChild(el("div", "dl-sb-label dl-sb-chats-label", "Chats"));
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

  // ── favourite chats, kept from the retention sweep (chainlit_app.py) ────
  // Chats that aren't favourites (favourite_threads) are deleted after
  // RETENTION_DAYS without activity. The open chat gets a Favourite toggle in
  // the title bar; favourites get a star in the chat list, drawn by a
  // generated stylesheet so Chainlit's own list items are never touched.
  var favourites = null;       // Set of favourite chat ids, once loaded
  var retentionDays = 0;
  var starSheet = null;

  function currentChatId() {
    var m = window.location.pathname.match(/\/thread\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }

  var favList = [];            // [{id, name}], newest favourite first

  function takeFavourites(state) {
    if (!state || !state.favourites) return;
    favList = state.favourites;
    favourites = new Set(favList.map(function (f) { return f.id; }));
    retentionDays = state.retention_days || 0;
    if (!starSheet) {
      starSheet = el("style");
      document.head.appendChild(starSheet);
    }
    // Favourites move to their own section (applyFavouritesSection), so take
    // them out of Chainlit's date-grouped list, and hide a date heading left
    // with nothing under it. The hidden items stay in the page: opening a
    // favourite clicks its link there.
    var ids = favList.map(function (f) { return '[id="thread-' + f.id.replace(/["\\]/g, "") + '"]'; });
    starSheet.textContent = ids.length
      ? ids.join(",\n") + " { display: none; }\n" +
        '[data-sidebar="group"]:has([data-sidebar="menu-item"])' +
        ':not(:has([data-sidebar="menu-item"]' +
        ids.map(function (s) { return ":not(" + s + ")"; }).join("") + ")) { display: none; }"
      : "";
    var b = document.querySelector(".dl-fav");
    if (b) b.remove();                      // redrawn by apply()
    var section = document.querySelector(".dl-sb-favs");
    if (section) section.remove();          // redrawn by apply()
    schedule();
  }

  // Open a chat the way Chainlit's own list does (no page reload), via its
  // hidden link; fall back to the URL when the list hasn't loaded that far.
  function openChat(id) {
    var link = document.querySelector('[id="thread-' + id + '"] a');
    if (link) link.click();
    else window.location.href = BASE + "thread/" + encodeURIComponent(id);
  }

  function chatName(f) {
    // Chainlit's list has the live name (after a rename); ours is from load.
    var live = document.querySelector('[id="thread-' + f.id + '"] .truncate');
    return (live && live.textContent.trim()) || f.name || "Untitled chat";
  }

  function applyFavouritesSection() {
    var nav = document.querySelector(".dl-sb-nav");
    if (!nav || !favourites) return;
    var chatsLabel = nav.querySelector(".dl-sb-chats-label");
    var section = nav.querySelector(".dl-sb-favs");
    var current = currentChatId();
    var key = favList.map(function (f) { return f.id + "=" + chatName(f); }).join("|") + "@" + current;
    if (section && section.dataset.key === key) return;
    if (section) section.remove();
    if (!favList.length) return;
    section = el("div", "dl-sb-favs");
    section.dataset.key = key;
    section.appendChild(el("div", "dl-sb-label", "Favourites"));
    favList.forEach(function (f) {
      var row = el("div", "dl-sb-fav" + (f.id === current ? " dl-active" : ""));
      var open = el("button", "dl-sb-fav-open", chatName(f));
      open.type = "button";
      open.title = chatName(f);
      open.addEventListener("click", function () { openChat(f.id); });
      var star = el("button", "dl-sb-fav-star", "\u2605");
      star.type = "button";
      star.title = "Unfavourite";
      star.addEventListener("click", function () { setFavourite(f.id, false); });
      row.appendChild(open);
      row.appendChild(star);
      section.appendChild(row);
    });
    nav.insertBefore(section, chatsLabel);
  }

  function postFavourites(path, body) {
    return fetch(BASE + "datalake/favourites" + path, {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.ok ? r.json() : null; });
  }

  function applyFavouriteButton() {
    var header = document.getElementById("header");
    var id = currentChatId();
    var existing = header && header.querySelector(".dl-fav");
    if (!header || !favourites || !retentionDays || !id) {
      if (existing) existing.remove();
      return;
    }
    var isFav = favourites.has(id);
    if (existing && existing.dataset.chat === id && existing.dataset.fav === String(isFav)) return;
    if (existing) existing.remove();
    var b = el("button", "dl-fav" + (isFav ? " dl-fav-on" : ""),
               isFav ? "\u2605 Favourite" : "\u2606 Favourite");
    b.type = "button";
    b.dataset.chat = id;
    b.dataset.fav = String(isFav);
    b.title = isFav
      ? "A favourite: kept until you remove it from favourites."
      : "Add to favourites to keep it. Other chats are deleted after " +
        retentionDays + " days without activity.";
    b.addEventListener("click", function () {
      b.disabled = true;
      setFavourite(id, !isFav).catch(function () { b.disabled = false; });
    });
    header.appendChild(b);
  }

  function setFavourite(id, on) {
    return postFavourites("/" + encodeURIComponent(id), { favourite: on }).then(takeFavourites);
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
    if (!rename || !favourites || !retentionDays || !menuChatId) return;
    var menu = rename.parentNode;
    var id = menuChatId;
    var isFav = favourites.has(id);
    var existing = menu.querySelector(":scope > .dl-menu-fav");
    if (existing && existing.dataset.chat === id && existing.dataset.fav === String(isFav)) return;
    if (existing) existing.remove();
    var item = el("div", rename.className + " dl-menu-fav");
    item.setAttribute("role", "menuitem");
    item.tabIndex = -1;
    item.dataset.chat = id;
    item.dataset.fav = String(isFav);
    item.title = rename.title;
    item.appendChild(el("span", null, isFav ? "Unfavourite" : "Favourite"));
    item.appendChild(el("span", "dl-menu-star", isFav ? "★" : "☆"));
    item.addEventListener("click", function (e) {
      e.stopPropagation();
      setFavourite(id, !isFav).catch(function () { /* unchanged */ });
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

  // ── Athena usage for this browser session ───────────────────────────────
  // The server hands over each question's scans once (POST /datalake/usage)
  // and keeps nothing; the running total lives here, in sessionStorage, so
  // it covers this tab's session and resets when the tab is closed.
  var USAGE_KEY = "datalake.usage";
  var USD_PER_TB = 5;          // Athena's on-demand price; an estimate only

  function readUsage() {
    try {
      var u = JSON.parse(sessionStorage.getItem(USAGE_KEY) || "null");
      if (u && typeof u.bytes === "number") { u.max = u.max || 0; return u; }
    } catch (e) { /* unreadable or blocked: start again */ }
    return { bytes: 0, queries: 0, max: 0 };
  }

  function collectUsage() {
    fetch(BASE + "datalake/usage", { method: "POST", credentials: "include" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        if (data.query_limit_bytes) queryLimit = data.query_limit_bytes;
        if (!data.scans || !data.scans.length) { schedule(); return; }
        var u = readUsage();
        data.scans.forEach(function (s) {
          u.bytes += s.bytes || 0;
          u.queries += 1;
          u.max = Math.max(u.max || 0, s.bytes || 0);
        });
        try { sessionStorage.setItem(USAGE_KEY, JSON.stringify(u)); } catch (e) { /* shown anyway */ }
        shownUsage = null;
        schedule();
      })
      .catch(function () { /* try again after the next answer */ });
  }

  function fmtBytes(n) {
    var units = ["B", "KB", "MB", "GB"];
    for (var i = 0; i < units.length; i++) {
      if (n < 1024) return i ? n.toFixed(1) + " " + units[i] : Math.round(n) + " B";
      n /= 1024;
    }
    return n.toFixed(2) + " TB";
  }

  var shownUsage = null;
  var queryLimit = 0;          // the workgroup's per-query cutoff, from the server
  var wasRunning = false;
  function applyUsage() {
    // An answer has just finished: the stop button has gone.
    var running = !!document.getElementById("stop-button");
    if (wasRunning && !running) collectUsage();
    wasRunning = running;

    // Beside the model picker, in whichever message box is on screen.
    var picker = document.getElementById("mode-picker-trigger-model");
    var row = picker && picker.closest(".mode-picker-wrapper");
    row = row && row.parentElement;
    if (!row) return;
    var pill = row.querySelector(":scope > .dl-usage");
    if (!pill) {
      pill = el("div", "dl-usage");
      pill.appendChild(el("span", "dl-usage-text"));
      var track = el("span", "dl-usage-track");
      track.appendChild(el("span", "dl-usage-fill"));
      pill.appendChild(track);
      row.appendChild(pill);
      shownUsage = null;
    }
    var u = readUsage();
    // The bar is the largest single query against the workgroup's
    // per-query cutoff: the one real limit, and how close a question came
    // to being stopped. The label is the session total.
    var share = queryLimit ? Math.min(1, u.max / queryLimit) : 0;
    var text = u.queries ? fmtBytes(u.bytes) + " this session" : "No queries yet";
    var key = text + "|" + share + "|" + queryLimit;
    if (shownUsage === key) return;
    shownUsage = key;
    pill.querySelector(".dl-usage-text").textContent = text;
    var fill = pill.querySelector(".dl-usage-fill");
    fill.style.width = (u.queries ? Math.max(share * 100, 2) : 0) + "%";
    pill.classList.toggle("dl-usage-high", share >= 0.75);
    pill.querySelector(".dl-usage-track").style.display = queryLimit ? "" : "none";
    var dollars = u.bytes / Math.pow(1024, 4) * USD_PER_TB;
    pill.title = (u.queries
        ? "Athena read " + fmtBytes(u.bytes) + " across " + u.queries +
          (u.queries === 1 ? " query" : " queries") + " in this tab, " +
          (dollars < 0.0001 ? "under $0.0001"
           : "about $" + (dollars < 0.01 ? dollars.toFixed(4) : dollars.toFixed(2))) +
          " at $" + USD_PER_TB + "/TB."
        : "Nothing scanned yet in this tab.") +
      (queryLimit
        ? "\nBar: largest single query, " + fmtBytes(u.max) + " of the " +
          fmtBytes(queryLimit) + " per-query limit. A query that reaches it is stopped."
        : "") +
      "\nResets when you close the tab.";
  }

  var queued = false;
  function apply() {
    queued = false;
    applyHeader();
    applyFavouriteButton();
    applyChatMenu();
    applySidebar();
    applyFavouritesSection();
    applyLanding();
    applyUsage();
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
          postFavourites("").then(takeFavourites).catch(function () { /* no favourites */ });
        } else if (attempt < 5) setTimeout(function () { loadUser(attempt + 1); }, 1500);
      })
      .catch(function () { /* no footer */ });
  }
  loadUser(0);
  collectUsage();       // anything finished while the page was away

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
