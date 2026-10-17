/* GoneWalking dashboard. Data: window.GW_DATA (stats), window.GW_ROUTES (tracks), window.GW_CONFIG. */
(function () {
  "use strict";

  const DATA = window.GW_DATA;
  const ROUTES = window.GW_ROUTES || {};
  const CONFIG = window.GW_CONFIG || {};
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const ACTIVITIES = [
    { key: "Walk", label: "Walks", noun: "walk", nouns: "walks", verb: "walked" },
    { key: "Bike Ride", label: "Bike rides", noun: "ride", nouns: "rides", verb: "ridden" },
    { key: "all", label: "All", noun: "workout", nouns: "workouts", verb: "covered" },
  ];
  const svgNS = "http://www.w3.org/2000/svg";

  const workouts = DATA.workouts.map((w) => {
    const [y, m, d] = w.date.split("-").map(Number);
    return Object.assign({}, w, {
      year: y, month: m, day: new Date(y, m - 1, d),
      weekday: (new Date(y, m - 1, d).getDay() + 6) % 7,
    });
  });
  const years = [...new Set(workouts.map((w) => w.year))].sort((a, b) => b - a);

  // Age, from config.birthDate (YYYY-MM-DD).
  const BIRTH = /^\d{4}-\d{2}-\d{2}$/.test(CONFIG.birthDate || "")
    ? (([y, m, d]) => new Date(y, m - 1, d))(CONFIG.birthDate.split("-").map(Number)) : null;
  function ageOn(day) {
    if (!BIRTH) return null;
    let age = day.getFullYear() - BIRTH.getFullYear();
    if (day.getMonth() < BIRTH.getMonth() || (day.getMonth() === BIRTH.getMonth() && day.getDate() < BIRTH.getDate())) age -= 1;
    return age;
  }
  const exactAge = (day) => (BIRTH ? (day - BIRTH) / (365.2425 * 864e5) : null);
  for (const w of workouts) w.age = ageOn(w.day);

  // Homes ("chapters") and milestones, from config.
  const parseDay = (s) => (/^\d{4}-\d{2}-\d{2}$/.test(s || "") ? (([y, m, d]) => new Date(y, m - 1, d))(s.split("-").map(Number)) : null);
  const HOMES = (CONFIG.homes || []).filter((h) => h && h.name)
    .map((h) => ({ name: h.name, short: h.short || h.name, from: parseDay(h.from) || new Date(1900, 0, 1) }))
    .sort((a, b) => a.from - b.from);
  const homeOf = (day) => { let cur = null; for (const h of HOMES) if (h.from <= day) cur = h; return cur; };
  for (const w of workouts) { const h = homeOf(w.day); w.home = h ? h.name : null; }
  const EVENTS = [
    ...HOMES.slice(1).map((h) => ({ date: h.from, label: `Moved to ${h.short}` })),
    ...(CONFIG.events || []).map((e) => ({ date: parseDay(e.date), label: e.label })).filter((e) => e.date && e.label),
  ].sort((a, b) => a.date - b.date);
  const today = new Date();

  const defaultState = () => ({ year: "all", activity: "Walk", place: "all", home: "all", region: "all", selected: null, sort: { key: "date", dir: -1 }, showAll: false, calYear: null });
  const state = defaultState();

  // Regions, nearest to farthest; each keeps a fixed color slot so filtering never repaints them.
  const REGIONS = (DATA.meta && DATA.meta.regions) || ["Twin Cities metro", "Greater Minnesota", "Other U.S. states", "Outside the U.S."];
  const regionColor = (r) => css(`--series-${REGIONS.indexOf(r) + 1}`);
  const regionOf = (w) => w.region || "Unknown";

  // ---------- helpers ----------
  const $ = (id) => document.getElementById(id);
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null) continue;
      if (k === "text") node.textContent = v;
      else if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const c of [].concat(children || [])) if (c != null) node.append(c);
    return node;
  }
  function svg(tag, attrs, parent) {
    const node = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
  }
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const sum = (list, f) => list.reduce((s, x) => s + (f(x) || 0), 0);

  const fmtNum = (x, digits = 0) =>
    x == null ? "–" : x.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const fmtMi = (x) => (x == null ? "–" : `${fmtNum(x, x >= 100 ? 0 : 1)} mi`);
  function fmtDur(secs) {
    if (!secs) return "–";
    const h = Math.floor(secs / 3600), m = Math.round((secs % 3600) / 60);
    return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m} min`;
  }
  function fmtPace(min) {
    if (!min || !isFinite(min)) return "–";
    let whole = Math.floor(min), s = Math.round((min - whole) * 60);
    if (s === 60) { whole += 1; s = 0; }
    return `${whole}:${String(s).padStart(2, "0")}`;
  }
  const fmtDate = (d, opts) => d.toLocaleDateString("en-US", opts || { month: "short", day: "numeric", year: "numeric" });
  const activityInfo = () => ACTIVITIES.find((a) => a.key === state.activity);

  /** Workouts matching the filters. opts.anyPlace ignores the place filter (for place-comparison charts). */
  function filtered(year = state.year, opts = {}) {
    return workouts.filter((w) =>
      (state.activity === "all" || w.activity === state.activity) &&
      (year === "all" || w.year === year) &&
      (opts.anyPlace || state.place === "all" || placeName(w) === state.place) &&
      (opts.anyHome || state.home === "all" || w.home === state.home) &&
      (opts.anyRegion || state.region === "all" || regionOf(w) === state.region) &&
      (!opts.until || w.day <= opts.until));
  }

  function summarize(list) {
    const miles = sum(list, (w) => w.mi);
    const secs = sum(list, (w) => w.dur);
    const paced = list.filter((w) => w.dur && w.mi > 0.05);
    return {
      count: list.length, miles, secs,
      avgMi: list.length ? miles / list.length : null,
      pace: paced.length ? sum(paced, (w) => w.dur) / 60 / sum(paced, (w) => w.mi) : null,
      longest: list.reduce((best, w) => (!best || w.mi > best.mi ? w : best), null),
      ft: sum(list, (w) => w.ft),
    };
  }

  // ---------- tooltip ----------
  const tooltip = $("tooltip");
  function showTip(evt, value, label, extra) {
    tooltip.replaceChildren(
      el("div", { class: "tt-value", text: value }),
      el("div", { class: "tt-label", text: label }),
      ...(extra ? [el("div", { class: "tt-label", text: extra })] : []));
    tooltip.hidden = false;
    let x, y;
    if (evt.clientX != null && evt.type !== "focus") { x = evt.clientX; y = evt.clientY; }
    else { const r = evt.target.getBoundingClientRect(); x = r.left + r.width / 2; y = r.top; }
    const tw = tooltip.offsetWidth, th = tooltip.offsetHeight;
    tooltip.style.left = `${Math.min(Math.max(8, x - tw / 2), window.innerWidth - tw - 8)}px`;
    tooltip.style.top = `${y - th - 12 < 8 ? y + 16 : y - th - 12}px`;
  }
  const hideTip = () => { tooltip.hidden = true; };

  // ---------- chart cards ----------
  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }
  const compact = (v) => (v >= 1000 ? `${fmtNum(v / 1000, v % 1000 ? 1 : 0)}K` : fmtNum(v, v % 1 ? 1 : 0));

  const icon = (name) => el("span", { class: "badge small" }, el("img", { src: `images/openmoji/${name}.svg`, alt: "" }));

  function chartCard(container, { title, note, table, art }) {
    container.replaceChildren();
    const chart = el("div", { class: "chart" });
    const tableWrap = el("div", { class: "table-wrap", hidden: "" });
    const toggle = el("button", { type: "button", class: "link-button", text: "Show table" });
    toggle.addEventListener("click", () => {
      const showing = tableWrap.hidden;
      tableWrap.hidden = !showing;
      chart.hidden = showing;
      toggle.textContent = showing ? "Show chart" : "Show table";
    });
    if (table) {
      const t = el("table", { class: "table-view" });
      t.append(el("thead", {}, el("tr", {}, table.head.map((h, i) => el("th", { class: i ? "num" : null, text: h })))));
      t.append(el("tbody", {}, table.rows.map((r) => el("tr", {}, r.map((c, i) => el("td", { class: i ? "num" : null, text: c }))))));
      tableWrap.append(t);
    }
    container.append(
      el("div", { class: "card-head" }, [el("h2", {}, [art ? icon(art) : null, title]), table ? toggle : null, note ? el("p", { class: "card-note", text: note }) : null]),
      chart, tableWrap);
    return chart;
  }

  /** Vertical milestone lines (moves, events) with a small label at the top of the plot. */
  function drawMarkers(root, markers, { x, top, bottom, left, right }) {
    for (const mk of markers) {
      const xx = Math.round(x(mk)) + 0.5;
      if (!(xx >= left && xx <= right)) continue;
      svg("line", { class: "marker-line", x1: xx, x2: xx, y1: top, y2: bottom }, root);
      const nearRight = xx > right - 110;
      svg("text", { class: "marker-label", x: nearRight ? xx - 4 : xx + 4, y: top + 10, "text-anchor": nearRight ? "end" : "start" }, root).textContent = mk.label;
    }
  }
  const dayFraction = (d) => (d - new Date(d.getFullYear(), 0, 1)) / ((new Date(d.getFullYear() + 1, 0, 1)) - new Date(d.getFullYear(), 0, 1));

  /** Column chart. items: [{key, label, value, tip}]; markers: [{pos (fractional item index), label}] */
  function columnChart(chart, items, { fmt, highlight, onClick, labelEvery = 1, markers = [] }) {
    const width = Math.max(chart.clientWidth, 280), height = 220;
    const m = { top: 22, right: 4, bottom: 24, left: 36 };
    const plotW = width - m.left - m.right, plotH = height - m.top - m.bottom;
    const max = niceMax(Math.max(...items.map((d) => d.value), 0));
    const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });
    const y = (v) => m.top + plotH - (v / max) * plotH;
    for (let i = 0; i <= 4; i++) {
      const v = (max / 4) * i, yy = Math.round(y(v)) + 0.5;
      svg("line", { class: i ? "gridline" : "baseline", x1: m.left, x2: width - m.right, y1: yy, y2: yy }, root);
      svg("text", { class: "axis-label", x: m.left - 6, y: yy + 4, "text-anchor": "end" }, root).textContent = compact(v);
    }
    const band = plotW / items.length;
    const barW = Math.min(24, Math.max(4, band - 4));
    const maxItem = items.reduce((b, d) => (d.value > (b ? b.value : -1) ? d : b), null);
    items.forEach((d, i) => {
      const cx = m.left + band * i + band / 2;
      const top = y(d.value), h = m.top + plotH - top;
      const hit = svg("rect", {
        class: "hit", x: cx - band / 2, y: m.top, width: band, height: plotH,
        tabindex: 0, role: onClick ? "button" : "img", "aria-label": `${d.label}: ${fmt(d.value)}`,
      }, root);
      if (h > 0) {
        const r = Math.min(4, h, barW / 2);
        const x0 = cx - barW / 2, x1 = cx + barW / 2, yb = m.top + plotH;
        svg("path", {
          class: `bar${highlight != null && d.key !== highlight ? " deemph" : ""}`,
          d: `M${x0},${yb}V${top + r}Q${x0},${top} ${x0 + r},${top}H${x1 - r}Q${x1},${top} ${x1},${top + r}V${yb}Z`,
          "pointer-events": "none",
        }, root);
      }
      const show = (e) => showTip(e, fmt(d.value), d.tip || d.label);
      hit.addEventListener("pointermove", show);
      hit.addEventListener("focus", show);
      hit.addEventListener("pointerleave", hideTip);
      hit.addEventListener("blur", hideTip);
      if (onClick) {
        hit.addEventListener("click", () => onClick(d));
        hit.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(d); } });
      }
      if (i % labelEvery === 0 || i === items.length - 1) {
        svg("text", { class: "axis-label", x: cx, y: height - 6, "text-anchor": "middle" }, root).textContent = d.short || d.label;
      }
      const labelled = highlight != null ? d.key === highlight : d === maxItem;
      if (labelled && d.value > 0) {
        svg("text", { class: "value-label", x: cx, y: top - 6, "text-anchor": "middle" }, root).textContent = fmt(d.value);
      }
    });
    drawMarkers(root, markers, { x: (mk) => m.left + band * mk.pos, top: m.top, bottom: m.top + plotH, left: m.left, right: width - m.right });
    chart.replaceChildren(root);
  }

  // ---------- header, filters ----------
  function renderFilters() {
    const select = $("filter-year");
    select.replaceChildren(el("option", { value: "all", text: "All years" }), ...years.map((y) => el("option", { value: y, text: y })));
    select.value = state.year;
    select.onchange = () => setYear(select.value === "all" ? "all" : Number(select.value));

    const homeWrap = $("filter-home-wrap");
    homeWrap.hidden = HOMES.length < 2;
    if (HOMES.length > 1) {
      const homeSelect = $("filter-home");
      homeSelect.replaceChildren(el("option", { value: "all", text: "Anywhere" }),
        ...HOMES.map((h) => el("option", { value: h.name, text: h.name })));
      homeSelect.value = state.home;
      homeSelect.onchange = () => { state.home = homeSelect.value; state.selected = null; state.showAll = false; renderAll(); };
    }

    const regionSelect = $("filter-region");
    const present = new Set(workouts.map(regionOf));
    regionSelect.replaceChildren(el("option", { value: "all", text: "All regions" }),
      ...[...REGIONS, "Unknown"].filter((r) => present.has(r)).map((r) => el("option", { value: r, text: r })));
    regionSelect.value = state.region;
    regionSelect.onchange = () => { state.region = regionSelect.value; state.selected = null; state.showAll = false; renderAll(); };

    // Places A–Z (with visit counts), for the current activity (and year).
    const counts = new Map();
    for (const w of filtered(state.year, { anyPlace: true })) counts.set(placeName(w), (counts.get(placeName(w)) || 0) + 1);
    if (state.place !== "all" && !counts.has(state.place)) counts.set(state.place, 0);
    const placeSelect = $("filter-place");
    placeSelect.replaceChildren(
      el("option", { value: "all", text: "All places" }),
      ...[...counts.entries()].sort((a, b) => a[0].localeCompare(b[0], "en", { sensitivity: "base", numeric: true }))
        .map(([name, n]) => el("option", { value: name, text: `${name} (${n})` })));
    placeSelect.value = state.place;
    placeSelect.onchange = () => setPlace(placeSelect.value);

    const group = $("filter-activity");
    group.replaceChildren(...ACTIVITIES.map((a) => el("button", {
      type: "button", role: "radio", "aria-checked": String(a.key === state.activity), text: a.label,
      onclick: () => { state.activity = a.key; state.selected = null; renderAll(); },
    })));
  }

  function setPlace(place) {
    state.place = place;
    state.selected = null;
    state.showAll = false;
    renderAll();
    writeHash();
  }

  function setYear(year) {
    state.year = year;
    state.calYear = null;
    state.selected = null;
    state.showAll = false;
    $("filter-year").value = String(year);
    renderAll();
  }

  // ---------- hero + tiles ----------
  function deltaNode(cur, prev, { lowerIsBetter = false, fmt, label }) {
    if (prev == null || cur == null || !prev) return el("div", { class: "delta" });
    const diff = cur - prev;
    const better = lowerIsBetter ? diff < 0 : diff > 0;
    const pct = Math.round((Math.abs(diff) / prev) * 100);
    const arrow = diff > 0 ? "▲" : diff < 0 ? "▼" : "";
    const text = fmt ? fmt(Math.abs(diff)) : `${pct}%`;
    return el("div", { class: "delta" }, [
      el("span", { class: diff === 0 ? null : better ? "up-good" : "down-bad", text: `${arrow} ${text}` }),
      ` ${label}`,
    ]);
  }

  function renderSummary() {
    const info = activityInfo();
    const list = filtered();
    const s = summarize(list);
    let prev = null, prevLabel = "";
    if (state.year !== "all" && years.includes(state.year - 1)) {
      const isCurrent = state.year === today.getFullYear();
      const until = isCurrent ? new Date(state.year - 1, today.getMonth(), today.getDate()) : null;
      prev = summarize(filtered(state.year - 1, { until }));
      prevLabel = isCurrent ? `vs ${state.year - 1} to date` : `vs ${state.year - 1}`;
    }

    $("hero-label").textContent = `Miles ${info.verb}${state.place === "all" ? "" : ` at ${state.place}`}${state.year === "all" ? ` since ${(list.length ? list[list.length - 1] : workouts[workouts.length - 1]).year}` : ` in ${state.year}`}`;
    $("hero-value").textContent = fmtNum(s.miles, 0);
    $("hero-delta").replaceWith(Object.assign(prev ? deltaNode(s.miles, prev.miles, { label: prevLabel }) : el("div", { class: "delta" }), { id: "hero-delta" }));

    const tiles = [
      { label: info.nouns[0].toUpperCase() + info.nouns.slice(1), value: fmtNum(s.count), delta: prev && deltaNode(s.count, prev.count, { label: prevLabel }) },
      { label: "Time on the move", value: `${fmtNum(s.secs / 3600, 0)} hours`, delta: prev && deltaNode(s.secs, prev.secs, { label: prevLabel }) },
      { label: "Average pace", value: `${fmtPace(s.pace)} /mi`, delta: prev && prev.pace && deltaNode(s.pace, prev.pace, { lowerIsBetter: true, fmt: (d) => `${fmtPace(d)}`, label: `${s.pace < prev.pace ? "faster" : "slower"} ${prevLabel}` }) },
      { label: `Average ${info.noun}`, value: fmtMi(s.avgMi), sub: s.count ? `${fmtDur(s.secs / s.count)} on average` : "" },
      { label: `Longest ${info.noun}`, value: s.longest ? fmtMi(s.longest.mi) : "–", sub: s.longest ? fmtDate(s.longest.day) : "", walk: s.longest },
      { label: "Climbing", value: `${fmtNum(s.ft)} ft`, sub: s.ft ? `${fmtNum(s.ft / 792, 0)}× the height of the IDS Center` : "" },
    ];
    $("tiles").replaceChildren(...tiles.map((t) => el("div", { class: "tile" }, [
      el("div", { class: "tile-label", text: t.label }),
      el("div", { class: "tile-value", text: t.value }),
      t.delta || el("div", { class: "tile-sub", text: t.sub || "" }),
    ])));

    const all = summarize(workouts.filter((w) => state.activity === "all" || w.activity === state.activity));
    const everyWs = workouts.filter((w) => state.activity === "all" || w.activity === state.activity);
    const nStates = new Set(everyWs.filter((w) => w.country === "United States" && w.state).map((w) => w.state)).size;
    const nCountries = new Set(everyWs.map((w) => w.country).filter(Boolean)).size;
    const reach = nStates ? ` in ${nStates} state${nStates === 1 ? "" : "s"}${nCountries > 1 ? ` and ${nCountries} countries` : ""}` : "";
    $("subtitle").textContent = `${fmtNum(all.count)} ${info.nouns} and ${fmtNum(all.miles)} miles${reach} since ${fmtDate(workouts[workouts.length - 1].day, { month: "long", year: "numeric" })}`;
  }

  // ---------- charts ----------
  function renderYears() {
    const items = [...years].reverse().map((y) => {
      const mi = sum(filtered(y), (w) => w.mi);
      return { key: y, label: String(y), short: `'${String(y).slice(2)}`, value: mi, tip: `${y} · ${filtered(y).length} ${activityInfo().nouns}` };
    });
    const chart = chartCard($("card-years"), {
      title: "Miles by year", art: "hiking-boot",
      note: state.year === "all" ? "Click a year to focus on it." : "Click the highlighted year again to show all years.",
      table: { head: ["Year", "Miles", activityInfo().nouns[0].toUpperCase() + activityInfo().nouns.slice(1)], rows: items.slice().reverse().map((d) => [d.label, fmtNum(d.value, 1), fmtNum(filtered(d.key).length)]) },
    });
    columnChart(chart, items, {
      fmt: (v) => `${fmtNum(v, 0)} mi`,
      highlight: state.year === "all" ? null : state.year,
      onClick: (d) => setYear(state.year === d.key ? "all" : d.key),
      markers: EVENTS.map((e) => ({ label: e.label, pos: items.findIndex((d) => d.key === e.date.getFullYear()) + dayFraction(e.date) }))
        .filter((mk) => mk.pos >= 0),
    });
  }

  function renderMonths() {
    const list = filtered();
    const items = MONTHS.map((name, i) => {
      const inMonth = list.filter((w) => w.month === i + 1);
      return { key: i + 1, label: name, short: name[0] + (window.innerWidth < 500 ? "" : name.slice(1, 3)), value: sum(inMonth, (w) => w.mi), tip: `${name} · ${inMonth.length} ${activityInfo().nouns}` };
    });
    const chart = chartCard($("card-months"), {
      title: state.year === "all" ? "Miles by month, all years" : `Miles by month, ${state.year}`,
      note: state.year === "all" ? "Which months you walk most, added up across every year." : null,
      table: { head: ["Month", "Miles"], rows: items.map((d) => [d.label, fmtNum(d.value, 1)]) },
    });
    columnChart(chart, items, { fmt: (v) => `${fmtNum(v, v >= 100 ? 0 : 1)} mi` });
  }

  function renderHours() {
    const list = filtered().filter((w) => w.hour != null);
    const hours = [];
    for (let h = 5; h <= 21; h++) hours.push(h);
    const label = (h) => (h === 0 ? "12am" : h < 12 ? `${h}am` : h === 12 ? "12pm" : `${h - 12}pm`);
    const items = hours.map((h) => ({ key: h, label: label(h), value: list.filter((w) => w.hour === h).length, tip: `Started ${label(h)}–${label(h + 1)}` }));
    const chart = chartCard($("card-hours"), {
      title: "Time of day", art: "sunrise",
      note: `${activityInfo().nouns[0].toUpperCase() + activityInfo().nouns.slice(1)} by start time.`,
      table: { head: ["Start hour", activityInfo().nouns[0].toUpperCase() + activityInfo().nouns.slice(1)], rows: items.map((d) => [d.label, fmtNum(d.value)]) },
    });
    columnChart(chart, items, { fmt: (v) => `${v} ${v === 1 ? activityInfo().noun : activityInfo().nouns}`, labelEvery: 3 });
  }

  function renderWeekdays() {
    const list = filtered();
    const items = WEEKDAYS.map((d, i) => ({ key: i, label: d, value: list.filter((w) => w.weekday === i).length }));
    const chart = chartCard($("card-weekdays"), {
      title: "Day of the week",
      note: `${activityInfo().nouns[0].toUpperCase() + activityInfo().nouns.slice(1)} on each day.`,
      table: { head: ["Day", activityInfo().nouns[0].toUpperCase() + activityInfo().nouns.slice(1)], rows: items.map((d) => [d.label, fmtNum(d.value)]) },
    });
    columnChart(chart, items, { fmt: (v) => `${v} ${v === 1 ? activityInfo().noun : activityInfo().nouns}` });
  }

  function renderPace() {
    const list = filtered().filter((w) => w.pace && w.mi >= 0.5).sort((a, b) => a.day - b.day);
    const isWalk = state.activity === "Walk";
    const card = $("card-pace");
    const chart = chartCard(card, {
      title: "Pace over time", art: "footprints",
      note: `Minutes per mile for each ${activityInfo().noun} over half a mile, including stops. Faster is higher.`,
      table: { head: ["Date", "Pace (min/mi)", "Distance"], rows: list.slice().reverse().map((w) => [fmtDate(w.day), fmtPace(w.pace), fmtMi(w.mi)]) },
    });
    if (list.length < 2) { chart.append(el("p", { class: "empty-note", text: "Not enough data for this selection." })); return; }

    // Rolling median of the last N workouts, as the trend.
    const N = Math.min(15, Math.max(3, Math.floor(list.length / 4)));
    const trend = list.map((w, i) => {
      const win = list.slice(Math.max(0, i - N + 1), i + 1).map((x) => x.pace).sort((a, b) => a - b);
      return { day: w.day, pace: win[Math.floor(win.length / 2)] };
    });

    const width = Math.max(chart.clientWidth, 280), height = BIRTH ? 276 : 260;
    const m = { top: 12, right: 12, bottom: BIRTH ? 40 : 24, left: 40 };
    const plotW = width - m.left - m.right, plotH = height - m.top - m.bottom;
    const paces = list.map((w) => w.pace).sort((a, b) => a - b);
    const q = (p) => paces[Math.min(paces.length - 1, Math.floor(p * paces.length))];
    let lo = Math.floor(q(0.01)) - 1, hi = Math.ceil(q(0.99)) + 1;
    if (!isWalk) { lo = Math.max(0, lo); }
    const t0 = list[0].day.getTime(), t1 = list[list.length - 1].day.getTime() || t0 + 1;
    const x = (d) => m.left + ((d.getTime() - t0) / Math.max(1, t1 - t0)) * plotW;
    const y = (p) => m.top + ((Math.min(Math.max(p, lo), hi) - lo) / (hi - lo)) * plotH; // faster (smaller) at top
    const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Pace over time scatter chart" });

    const step = hi - lo > 16 ? 4 : hi - lo > 8 ? 2 : 1;
    for (let p = Math.ceil(lo / step) * step; p <= hi; p += step) {
      const yy = Math.round(y(p)) + 0.5;
      svg("line", { class: "gridline", x1: m.left, x2: width - m.right, y1: yy, y2: yy }, root);
      svg("text", { class: "axis-label", x: m.left - 6, y: yy + 4, "text-anchor": "end" }, root).textContent = `${p}:00`;
    }
    svg("line", { class: "baseline", x1: m.left, x2: width - m.right, y1: height - m.bottom + 0.5, y2: height - m.bottom + 0.5 }, root);
    // x ticks: years, or months within a single year
    const sameYear = list[0].year === list[list.length - 1].year;
    if (sameYear) {
      for (let mo = 0; mo < 12; mo++) {
        const d = new Date(list[0].year, mo, 1);
        if (d.getTime() < t0 - 1 || d.getTime() > t1) continue;
        svg("text", { class: "axis-label", x: x(d), y: height - 6, "text-anchor": "middle" }, root).textContent = MONTHS[mo];
      }
    } else {
      for (let yr = list[0].year + 1; yr <= list[list.length - 1].year; yr++) {
        const xx = Math.round(x(new Date(yr, 0, 1))) + 0.5;
        svg("line", { class: "gridline", x1: xx, x2: xx, y1: m.top, y2: height - m.bottom }, root);
        svg("text", { class: "axis-label", x: xx, y: height - 6, "text-anchor": "middle" }, root).textContent = yr;
      }
    }

    // Second axis row: your age, marked at each birthday.
    if (BIRTH) {
      const ageY = height - m.bottom + 30;
      svg("text", { class: "axis-label", x: m.left - 6, y: ageY, "text-anchor": "end" }, root).textContent = "Age";
      const first = list[0].day, last = list[list.length - 1].day;
      const every = sameYear ? 1 : Math.max(1, Math.ceil((last.getFullYear() - first.getFullYear()) / 10));
      for (let yr = first.getFullYear(); yr <= last.getFullYear() + 1; yr++) {
        const bday = new Date(yr, BIRTH.getMonth(), BIRTH.getDate());
        if (bday < first || bday > last || (yr - BIRTH.getFullYear()) % every) continue;
        const xx = x(bday);
        svg("line", { class: "baseline", x1: xx, x2: xx, y1: height - m.bottom, y2: height - m.bottom + 4 }, root);
        svg("text", { class: "axis-label", x: xx, y: ageY, "text-anchor": "middle" }, root).textContent = yr - BIRTH.getFullYear();
      }
    }
    drawMarkers(root, EVENTS.filter((e) => e.date >= list[0].day && e.date <= list[list.length - 1].day),
      { x: (e) => x(e.date), top: m.top, bottom: height - m.bottom, left: m.left, right: width - m.right });
    // Year (or month) labels sit in the first row under the plot.
    root.querySelectorAll("text.axis-label").forEach((t) => {
      if (Number(t.getAttribute("y")) === height - 6 && BIRTH) t.setAttribute("y", height - m.bottom + 14);
    });

    const surface = css("--surface");
    const dots = svg("g", {}, root);
    const points = list.map((w) => ({ w, cx: x(w.day), cy: y(w.pace) }));
    for (const p of points) svg("circle", { cx: p.cx, cy: p.cy, r: 4, fill: css("--deemph"), stroke: surface, "stroke-width": 2 }, dots);
    svg("path", {
      d: trend.map((t, i) => `${i ? "L" : "M"}${x(t.day).toFixed(1)},${y(t.pace).toFixed(1)}`).join(""),
      fill: "none", stroke: css("--series-1"), "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
    }, root);
    const ring = svg("circle", { r: 6, fill: "none", stroke: css("--text-primary"), "stroke-width": 2, visibility: "hidden" }, root);

    // Nearest-point hover layer.
    const overlay = svg("rect", { x: m.left, y: m.top, width: plotW, height: plotH, fill: "transparent", style: "cursor:pointer" }, root);
    let current = null;
    overlay.addEventListener("pointermove", (e) => {
      const box = root.getBoundingClientRect();
      const px = ((e.clientX - box.left) / box.width) * width, py = ((e.clientY - box.top) / box.height) * height;
      let best = null, bestD = Infinity;
      for (const p of points) {
        const d = (p.cx - px) ** 2 + (p.cy - py) ** 2;
        if (d < bestD) { bestD = d; best = p; }
      }
      if (!best || bestD > 40 * 40) { ring.setAttribute("visibility", "hidden"); hideTip(); current = null; return; }
      current = best;
      ring.setAttribute("cx", best.cx); ring.setAttribute("cy", best.cy); ring.setAttribute("visibility", "visible");
      showTip(e, `${fmtPace(best.w.pace)} /mi`, `${fmtDate(best.w.day)} · ${fmtMi(best.w.mi)}${best.w.age != null ? ` · age ${best.w.age}` : ""}`, "Click to show on the map");
    });
    overlay.addEventListener("pointerleave", () => { ring.setAttribute("visibility", "hidden"); hideTip(); });
    overlay.addEventListener("click", () => { if (current) selectWalk(current.w.id, true); });

    chart.replaceChildren(root, el("div", { class: "legend" }, [
      el("span", { class: "legend-item" }, [el("span", { class: "key-dot", style: `background:${css("--deemph")}` }), `Each ${activityInfo().noun}`]),
      el("span", { class: "legend-item" }, [el("span", { class: "key-line", style: `background:${css("--series-1")}` }), `Trend (median of the last ${N})`]),
    ]));
  }

  /** Least-squares slope of y on x. */
  function slope(points) {
    const n = points.length;
    if (n < 3) return null;
    const mx = sum(points, (p) => p.x) / n, my = sum(points, (p) => p.y) / n;
    const sxx = sum(points, (p) => (p.x - mx) ** 2);
    return sxx ? sum(points, (p) => (p.x - mx) * (p.y - my)) / sxx : null;
  }

  /** Dots joined by a line, one per category; for averages (not totals). invert puts small values on top. */
  function dotLineChart(chart, items, { fmt, invert = false, axisFmt, markers = [] }) {
    const width = Math.max(chart.clientWidth, 200), height = 180;
    const m = { top: 20, right: 12, bottom: 24, left: 40 };
    const plotW = width - m.left - m.right, plotH = height - m.top - m.bottom;
    const vals = items.map((d) => d.value).filter((v) => v != null);
    const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });
    if (!vals.length) { chart.replaceChildren(el("p", { class: "empty-note", text: "No data." })); return; }
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = Math.max((hi - lo) * 0.15, hi * 0.05, 0.1);
    lo = Math.max(0, lo - pad); hi += pad;
    const y = (v) => (invert ? m.top + ((v - lo) / (hi - lo)) * plotH : m.top + plotH - ((v - lo) / (hi - lo)) * plotH);
    for (let i = 0; i <= 3; i++) {
      const v = lo + ((hi - lo) / 3) * i, yy = Math.round(y(v)) + 0.5;
      svg("line", { class: "gridline", x1: m.left, x2: width - m.right, y1: yy, y2: yy }, root);
      svg("text", { class: "axis-label", x: m.left - 6, y: yy + 4, "text-anchor": "end" }, root).textContent = (axisFmt || fmt)(v);
    }
    const band = plotW / items.length;
    const x = (i) => m.left + band * i + band / 2;
    const pts = items.map((d, i) => (d.value == null ? null : [x(i), y(d.value)]));
    let path = "", pen = false;
    pts.forEach((p) => { if (!p) { pen = false; return; } path += `${pen ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`; pen = true; });
    svg("path", { d: path, fill: "none", stroke: css("--series-1"), "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, root);
    const surface = css("--surface");
    const shown = items.map((d, i) => i).filter((i) => items[i].value != null);
    const labelled = new Set([shown[0], shown[shown.length - 1]]);
    items.forEach((d, i) => {
      if (i % Math.ceil(items.length / 8) === 0 || i === items.length - 1) {
        svg("text", { class: "axis-label", x: x(i), y: height - 6, "text-anchor": "middle" }, root).textContent = d.short || d.label;
      }
      if (d.value == null) return;
      svg("circle", { cx: x(i), cy: y(d.value), r: 4, fill: css("--series-1"), stroke: surface, "stroke-width": 2 }, root);
      if (labelled.has(i)) {
        svg("text", { class: "value-label", x: x(i), y: y(d.value) - 9, "text-anchor": "middle" }, root).textContent = fmt(d.value);
      }
      const hit = svg("rect", { class: "hit", x: x(i) - band / 2, y: m.top, width: band, height: plotH, tabindex: 0, "aria-label": `${d.label}: ${fmt(d.value)}` }, root);
      const show = (e) => showTip(e, fmt(d.value), d.tip || d.label);
      hit.addEventListener("pointermove", show);
      hit.addEventListener("focus", show);
      hit.addEventListener("pointerleave", hideTip);
      hit.addEventListener("blur", hideTip);
    });
    drawMarkers(root, markers, { x: (mk) => m.left + band * mk.pos, top: m.top, bottom: m.top + plotH, left: m.left, right: width - m.right });
    chart.replaceChildren(root);
  }

  function renderAge() {
    const card = $("card-age");
    card.hidden = !BIRTH;
    if (!BIRTH) return;
    const info = activityInfo();
    const list = filtered().filter((w) => w.age != null);
    const ages = [...new Set(list.map((w) => w.age))].sort((a, b) => a - b);
    const byAge = [];
    for (let a = ages[0]; ages.length && a <= ages[ages.length - 1]; a++) {
      const at = list.filter((w) => w.age === a);
      const paced = at.filter((w) => w.pace && w.mi >= 0.5);
      byAge.push({
        key: a, label: `Age ${a}`, short: String(a), count: at.length,
        miles: sum(at, (w) => w.mi),
        avgMi: at.length ? sum(at, (w) => w.mi) / at.length : null,
        pace: paced.length ? sum(paced, (w) => w.dur) / 60 / sum(paced, (w) => w.mi) : null,
      });
    }

    card.replaceChildren(el("div", { class: "card-head" }, [
      el("h2", {}, [icon("hiking-boot"), "Walking by age"]),
      el("p", { class: "card-note", text: `Your ${info.nouns} grouped by your age at the time. Pace includes stops; averages leave out ${info.nouns} under half a mile.` }),
    ]));
    if (byAge.length < 2) {
      card.append(el("p", { class: "empty-note", text: "Pick All years to see how things change with age." }));
      return;
    }

    // Trend per year of age, fitted to individual workouts.
    const paced = list.filter((w) => w.pace && w.mi >= 0.5);
    const paceSlope = slope(paced.map((w) => ({ x: exactAge(w.day), y: w.pace })));
    const distSlope = slope(list.filter((w) => w.mi >= 0.5).map((w) => ({ x: exactAge(w.day), y: w.mi })));
    // Earliest vs latest stretch of (up to) three ages with data.
    const withData = byAge.filter((d) => d.count);
    const span = Math.min(3, Math.floor(withData.length / 2));
    const group = (ds) => {
      const inAges = new Set(ds.map((d) => d.key));
      const ws = list.filter((w) => inAges.has(w.age));
      const p = ws.filter((w) => w.pace && w.mi >= 0.5);
      return { from: ds[0].key, to: ds[ds.length - 1].key, avgMi: sum(ws, (w) => w.mi) / ws.length,
               pace: p.length ? sum(p, (w) => w.dur) / 60 / sum(p, (w) => w.mi) : null };
    };
    const early = group(withData.slice(0, span)), late = group(withData.slice(-span));
    const ageRange = (g) => (g.from === g.to ? `age ${g.from}` : `ages ${g.from}–${g.to}`);
    const signed = (v, f, more, less) => `${f(Math.abs(v))} ${v >= 0 ? more : less}`;

    const tiles = el("div", { class: "age-tiles" }, [
      el("div", { class: "tile" }, [
        el("div", { class: "tile-label", text: "Pace per year of age" }),
        el("div", { class: "tile-value", text: paceSlope == null ? "–" : signed(paceSlope, fmtPace, "slower", "faster") }),
        el("div", { class: "tile-sub", text: "per mile, trend across every walk" }),
      ]),
      el("div", { class: "tile" }, [
        el("div", { class: "tile-label", text: `${capital(info.noun)} length per year of age` }),
        el("div", { class: "tile-value", text: distSlope == null ? "–" : signed(distSlope, (v) => fmtMi(v), "longer", "shorter") }),
        el("div", { class: "tile-sub", text: "trend across every walk" }),
      ]),
      el("div", { class: "tile" }, [
        el("div", { class: "tile-label", text: `Then and now` }),
        el("div", { class: "tile-value small", text: `${fmtMi(early.avgMi)} → ${fmtMi(late.avgMi)}` }),
        el("div", { class: "tile-sub", text: `${fmtPace(early.pace)} → ${fmtPace(late.pace)} /mi · ${ageRange(early)} vs ${ageRange(late)}` }),
      ]),
    ]);

    const minis = el("div", { class: "age-charts" });
    const box = (title) => {
      const c = el("div", { class: "chart" });
      minis.append(el("div", { class: "mini" }, [el("h3", { class: "mini-title", text: title }), c]));
      return c;
    };
    const miC = box("Miles in each year of age"), lenC = box(`Average ${info.noun}`), paceC = box("Average pace (faster is higher)");
    card.append(tiles, minis);
    const tip = (d) => `Age ${d.key} · ${d.count} ${d.count === 1 ? info.noun : info.nouns}`;
    const ageMarkers = EVENTS.map((e) => ({ label: e.label, pos: exactAge(e.date) - byAge[0].key }))
      .filter((mk) => mk.pos >= 0 && mk.pos <= byAge.length);
    columnChart(miC, byAge.map((d) => ({ key: d.key, label: d.label, short: d.short, value: d.miles, tip: tip(d) })),
      { fmt: (v) => `${fmtNum(v, 0)} mi`, labelEvery: Math.ceil(byAge.length / 8), markers: ageMarkers });
    dotLineChart(lenC, byAge.map((d) => ({ key: d.key, label: d.label, short: d.short, value: d.avgMi, tip: tip(d) })),
      { fmt: (v) => fmtMi(v), axisFmt: (v) => fmtNum(v, 1), markers: ageMarkers });
    dotLineChart(paceC, byAge.map((d) => ({ key: d.key, label: d.label, short: d.short, value: d.pace, tip: tip(d) })),
      { fmt: (v) => fmtPace(v), invert: true, markers: ageMarkers });

    const toggle = el("button", { type: "button", class: "link-button", text: "Show table" });
    const table = el("table", { class: "table-view" }, [
      el("thead", {}, el("tr", {}, ["Age", capital(info.nouns), "Miles", `Average ${info.noun}`, "Pace /mi"].map((h, i) => el("th", { class: i ? "num" : null, text: h })))),
      el("tbody", {}, byAge.map((d) => el("tr", {}, [String(d.key), fmtNum(d.count), fmtNum(d.miles, 1), d.avgMi ? fmtMi(d.avgMi) : "–", fmtPace(d.pace)]
        .map((c, i) => el("td", { class: i ? "num" : null, text: c }))))),
    ]);
    const tableWrap = el("div", { class: "table-wrap", hidden: "" }, table);
    toggle.addEventListener("click", () => {
      const showing = tableWrap.hidden;
      tableWrap.hidden = !showing; minis.hidden = showing;
      toggle.textContent = showing ? "Show charts" : "Show table";
    });
    card.querySelector(".card-head").insertBefore(toggle, card.querySelector(".card-note"));
    card.append(tableWrap);
  }

  /** Side-by-side comparison of the homes ("chapters") in config.homes. */
  function renderChapters() {
    const card = $("card-chapters");
    card.hidden = HOMES.length < 2;
    if (card.hidden) return;
    const info = activityInfo();
    const all = workouts.filter((w) => state.activity === "all" || w.activity === state.activity);
    const dataStart = all.reduce((b, w) => (w.day < b ? w.day : b), today);
    const chapters = HOMES.map((h, i) => {
      const ws = all.filter((w) => w.home === h.name).sort((a, b) => a.day - b.day);
      const from = h.from > dataStart ? h.from : dataStart;
      const to = HOMES[i + 1] ? HOMES[i + 1].from : today;
      const months = Math.max(1, (to - from) / (30.44 * 864e5));
      const paced = ws.filter((w) => w.pace && w.mi >= 0.5);
      const moving = ws.filter((w) => w.mpace && w.mi >= 0.5);
      const starts = new Map();
      for (const w of ws) if (w.latlon) { const k = `${w.latlon[0].toFixed(2)},${w.latlon[1].toFixed(2)}`; starts.set(k, (starts.get(k) || 0) + 1); }
      const topStart = Math.max(0, ...starts.values());
      const places = placeStats(ws);
      return {
        home: h, ws, from, to, months, count: ws.length, miles: sum(ws, (w) => w.mi),
        avgMi: ws.length ? sum(ws, (w) => w.mi) / ws.length : null,
        longest: ws.reduce((b, w) => (!b || w.mi > b.mi ? w : b), null),
        pace: paced.length ? sum(paced, (w) => w.dur) / 60 / sum(paced, (w) => w.mi) : null,
        mpace: moving.length ? sum(moving, (w) => w.moving) / 60 / sum(moving, (w) => w.mi) : null,
        places, named: ws.filter((w) => w.place).length,
        sameStart: ws.length ? topStart / ws.length : null,
        away: ws.filter((w) => !isHome(w)).length,
        ages: ws.length && ws[0].age != null ? [ws[0].age, ws[ws.length - 1].age] : null,
        ageSlope: BIRTH ? slope(paced.map((w) => ({ x: exactAge(w.day), y: w.pace }))) : null,
      };
    }).filter((c) => c.count);

    card.replaceChildren(el("div", { class: "card-head" }, [
      el("h2", {}, [icon("backpack"), "Two chapters"]),
      el("p", { class: "card-note", text: `Your ${info.nouns} by where you lived at the time. Moving changes where, how often and how far you ${info.noun === "walk" ? "walk" : "go"}, so it's part of the story alongside age.` }),
    ]));
    if (chapters.length < 2) { card.append(el("p", { class: "empty-note", text: "Not enough data for this activity." })); return; }

    // A sentence or two that tells the story in numbers.
    const [a, b] = [chapters[0], chapters[chapters.length - 1]];
    const ageSpan = (c) => (c.ages ? (c.ages[0] === c.ages[1] ? ` (age ${c.ages[0]})` : ` (ages ${c.ages[0]}–${c.ages[1]})`) : "");
    const freqRatio = (b.count / b.months) / (a.count / a.months);
    const often = freqRatio >= 1.5 ? `${fmtNum(freqRatio, 0)}× as often` : freqRatio <= 0.67 ? `${fmtNum(1 / freqRatio, 0)}× less often` : "about as often";
    const story = el("p", { class: "story" }, [
      `Living in ${a.home.name}${ageSpan(a)}, you ${info.noun === "walk" ? "walked" : "went out"} about ${fmtNum(a.count / a.months, 1)} times a month, averaging ${fmtMi(a.avgMi)}`,
      a.sameStart != null && a.sameStart >= 0.3 ? `, and ${Math.round(a.sameStart * 100)}% of ${info.nouns} began from the same spot. ` : ". ",
      `In ${b.home.name}${ageSpan(b)} you go ${often}, averaging ${fmtMi(b.avgMi)}, across ${fmtNum(b.places.length)} different places`,
      b.places[0] ? `, most often ${b.places[0].name}.` : ".",
    ]);

    const rows = [
      ["When", (c) => `${fmtDate(c.ws[0].day, { month: "short", year: "numeric" })} – ${fmtDate(c.ws[c.ws.length - 1].day, { month: "short", year: "numeric" })}`],
      ...(BIRTH ? [["Age", (c) => (c.ages ? `${c.ages[0]}–${c.ages[1]}` : "–")]] : []),
      [capital(info.nouns), (c) => fmtNum(c.count)],
      [`${capital(info.nouns)} per month`, (c) => fmtNum(c.count / c.months, 1)],
      ["Miles per month", (c) => fmtNum(c.miles / c.months, 1)],
      [`Average ${info.noun}`, (c) => fmtMi(c.avgMi)],
      [`Longest ${info.noun}`, (c) => (c.longest ? fmtMi(c.longest.mi) : "–")],
      ["Pace (with stops)", (c) => `${fmtPace(c.pace)} /mi`],
      ["Moving pace", (c) => `${fmtPace(c.mpace)} /mi`],
      ...(BIRTH ? [["Pace change per year of age", (c) => (c.ageSlope == null ? "–" : `${fmtPace(Math.abs(c.ageSlope))} ${c.ageSlope >= 0 ? "slower" : "faster"}`)]] : []),
      ["Different places", (c) => fmtNum(c.places.length)],
      ["In a named park or trail", (c) => `${Math.round((c.named / c.count) * 100)}%`],
      ["Starting from your usual spot", (c) => (c.sameStart == null ? "–" : `${Math.round(c.sameStart * 100)}%`)],
      ["Away from home", (c) => fmtNum(c.away)],
    ];
    const table = el("table", { class: "compare" }, [
      el("thead", {}, el("tr", {}, [el("th", { text: "" }), ...chapters.map((c) => el("th", { class: "num" },
        el("button", { type: "button", class: "link-button", text: c.home.name, title: `Show only ${c.home.name}`,
          onclick: () => { state.home = state.home === c.home.name ? "all" : c.home.name; state.selected = null; renderAll(); } })))])),
      el("tbody", {}, rows.map(([label, f]) => el("tr", {}, [el("th", { scope: "row", text: label }), ...chapters.map((c) => el("td", { class: "num", text: f(c) }))]))),
    ]);
    const favorites = el("div", { class: "chapter-places" }, chapters.map((c) => el("div", {}, [
      el("h3", { class: "mini-title", text: `Favorite places · ${c.home.short}` }),
      el("ol", {}, c.places.slice(0, 5).map((p) => el("li", {}, [
        p.url ? el("a", { href: p.url, target: "_blank", rel: "noopener", text: p.name }) : p.name,
        el("span", { class: "rec-sub", text: ` ${fmtNum(p.count)} ${p.count === 1 ? info.noun : info.nouns}, ${fmtMi(p.mi)}` }),
      ]))),
    ])));
    card.append(story, el("div", { class: "table-wrap" }, table), favorites);
  }

  /** Where walks happened at the broadest level: metro, rest of the state, other states, abroad. */
  function renderRegions() {
    const card = $("card-regions");
    const info = activityInfo();
    const list = filtered(state.year, { anyRegion: true });
    const known = list.filter((w) => w.region);
    card.replaceChildren(el("div", { class: "card-head" }, [
      el("h2", {}, [icon("mountain"), "Near and far"]),
      el("p", { class: "card-note", text: `From your home metro to the other side of the border. Regions come from the county, state and country each ${info.noun} took place in.` }),
    ]));
    if (!known.length) {
      card.append(el("p", { class: "empty-note", text: "Region data isn't available yet. Run python gonewalking.py places to look it up." }));
      return;
    }
    const regions = REGIONS.filter((r) => known.some((w) => w.region === r));
    const by = (r, f) => sum(known.filter((w) => w.region === r), f);
    const totalN = known.length, totalMi = sum(known, (w) => w.mi);
    const pct = (x, t) => (t ? Math.round((x / t) * 100) : 0);
    const pctText = (x, t) => { const p = t ? (x / t) * 100 : 0; return p > 0 && p < 1 ? "<1%" : `${Math.round(p)}%`; };

    // Story.
    const us = known.filter((w) => w.country === "United States");
    const otherStates = [...new Set(us.map((w) => w.state).filter((s) => s && s !== "Minnesota"))];
    const countries = [...new Set(known.map((w) => w.country).filter((c) => c && c !== "United States"))];
    const listText = (xs) => (xs.length <= 2 ? xs.join(" and ") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);
    const story = el("p", { class: "story" }, [
      `${pctText(by(REGIONS[0], (w) => w.mi), totalMi)} of your miles were in the ${REGIONS[0]}`,
      regions.includes(REGIONS[1]) ? ` and ${pctText(by(REGIONS[1], (w) => w.mi), totalMi)} elsewhere in Minnesota` : "",
      ". ",
      otherStates.length ? `You've also ${info.verb === "walked" ? "walked" : "been out"} in ${otherStates.length} other state${otherStates.length === 1 ? "" : "s"} (${listText(otherStates)})` : "",
      otherStates.length && countries.length ? " and " : "",
      countries.length ? `${otherStates.length ? "" : "You've also been out in "}${countries.length} other countr${countries.length === 1 ? "y" : "ies"} (${listText(countries)})` : "",
      otherStates.length || countries.length ? "." : "",
    ]);

    // 100% bars for walks and miles.
    const shareBar = (label, value) => [
      el("div", { class: "tile-label", text: label }),
      el("div", { class: "share-bar", role: "img", "aria-label": `${label} by region: ${regions.map((r) => `${r} ${pctText(value(r), regions.reduce((a, x) => a + value(x), 0))}`).join(", ")}` },
        regions.map((r) => {
          const total = regions.reduce((a, x) => a + value(x), 0);
          const seg = el("div", { class: "share-seg", style: `flex:${value(r)} 0 0; background:${regionColor(r)}` });
          const show = (e) => showTip(e, pctText(value(r), total), `${r} · ${label.toLowerCase()}`);
          seg.addEventListener("pointermove", show);
          seg.addEventListener("pointerleave", hideTip);
          return seg;
        })),
    ];
    const shares = el("div", { class: "share-bars" }, [
      ...shareBar(capital(info.nouns), (r) => by(r, () => 1)),
      ...shareBar("Miles", (r) => by(r, (w) => w.mi)),
    ]);
    const legend = el("div", { class: "legend region-legend" }, regions.map((r) => el("span", { class: "legend-item" }, [
      el("span", { class: "key-cell", style: `background:${regionColor(r)}` }),
      el("button", { type: "button", class: "link-button", text: r, title: `Show only ${r}`, onclick: () => { state.region = state.region === r ? "all" : r; state.selected = null; renderAll(); } }),
      el("strong", { text: ` ${pctText(by(r, () => 1), totalN)}` }), ` of ${info.nouns} · `,
      el("strong", { text: pctText(by(r, (w) => w.mi), totalMi) }), " of miles",
    ])));

    // Miles by region, year by year.
    const yearsAsc = [...years].reverse();
    const chartBox = el("div", { class: "chart" });
    const yearsTitle = el("h3", { class: "mini-title", text: "Miles by region, year by year" });

    // Destinations beyond the metro, and trips (runs of days away from the metro).
    const away = known.filter((w) => w.region !== REGIONS[0]).sort((a, b) => a.day - b.day);
    const destKey = (w) => (w.country !== "United States" ? `${w.city || "?"}, ${w.country}` : w.state !== "Minnesota" ? `${w.city || "?"}, ${w.state}` : `${w.city || "?"}, Minnesota`);
    const dests = new Map();
    for (const w of away) {
      const k = destKey(w);
      const d = dests.get(k) || { name: k, region: w.region, n: 0, mi: 0 };
      d.n += 1; d.mi += w.mi;
      dests.set(k, d);
    }
    const destList = [...dests.values()].sort((a, b) => REGIONS.indexOf(b.region) - REGIONS.indexOf(a.region) || b.mi - a.mi);
    const trips = [];
    for (const w of away) {
      const area = w.country !== "United States" ? w.country : w.state;
      const last = trips[trips.length - 1];
      if (last && last.area === area && (w.day - last.end) / 864e5 <= 3) { last.ws.push(w); last.end = w.day; }
      else trips.push({ area, region: w.region, start: w.day, end: w.day, ws: [w] });
    }
    trips.reverse();
    const tripRows = trips.slice(0, 12).map((t) => {
      const cities = [...new Set(t.ws.map((w) => w.place || w.city).filter(Boolean))];
      const when = t.start.getTime() === t.end.getTime() ? fmtDate(t.start)
        : `${fmtDate(t.start, { month: "short", day: "numeric" })} – ${fmtDate(t.end)}`;
      // Trips within the home state are named by county; others by state or country.
      const counties = [...new Set(t.ws.map((w) => w.county).filter(Boolean))];
      const title = t.region === REGIONS[1] && counties.length ? `${listText(counties)}, ${t.area}` : (t.area || "Unknown");
      return el("tr", {}, [
        el("td", {}, el("span", { class: "key-cell", style: `background:${regionColor(t.region)}` })),
        el("td", {}, [el("button", { type: "button", class: "link-button", text: title, onclick: () => selectWalk(t.ws[0].id, true) }),
          el("span", { class: "rec-sub", text: `${when}${cities.length ? ` · ${cities.slice(0, 3).join(", ")}${cities.length > 3 ? "…" : ""}` : ""}` })]),
        el("td", { class: "num", text: `${t.ws.length} ${t.ws.length === 1 ? info.noun : info.nouns}` }),
        el("td", { class: "num", text: fmtMi(sum(t.ws, (w) => w.mi)) }),
      ]);
    });

    const grid = el("div", { class: "region-grid" }, [
      el("div", {}, [el("h3", { class: "mini-title", text: `Trips beyond the metro (${fmtNum(trips.length)}, latest first)` }),
        trips.length ? el("table", { class: "trips" }, el("tbody", {}, tripRows)) : el("p", { class: "empty-note", text: "None in this selection." })]),
      el("div", {}, [el("h3", { class: "mini-title", text: "Destinations" }),
        destList.length ? el("ul", { class: "dest-list" }, destList.map((d) => el("li", {}, [
          el("span", { class: "key-cell", style: `background:${regionColor(d.region)}` }),
          el("span", { class: "dest-name", text: d.name }), el("span", { class: "dest-value", text: `${d.n} · ${fmtMi(d.mi)}` }),
        ]))) : el("p", { class: "empty-note", text: "None in this selection." })]),
    ]);

    card.append(story, shares, legend, yearsTitle, chartBox, grid);
    stackedColumnChart(chartBox, yearsAsc.map((y) => ({
      key: y, label: String(y), short: `'${String(y).slice(2)}`,
      values: regions.map((r) => sum(known.filter((w) => w.year === y && w.region === r), (w) => w.mi)),
    })), {
      series: regions.map((r) => ({ name: r, color: regionColor(r) })),
      fmt: (v) => `${fmtNum(v, 0)} mi`,
      highlight: state.year === "all" ? null : state.year,
      tip: (d) => [`${fmtNum(d.values.reduce((a, b) => a + b, 0), 0)} mi in ${d.label}`,
        regions.map((r, k) => (d.values[k] ? `${r}: ${fmtNum(d.values[k], 0)}` : null)).filter(Boolean).join(" · ")],
      onClick: (d) => setYear(state.year === d.key ? "all" : d.key),
      markers: EVENTS.map((e) => ({ label: e.label, pos: yearsAsc.indexOf(e.date.getFullYear()) + dayFraction(e.date) })).filter((mk) => mk.pos >= 0),
    });
  }

  /** The calendar shows the Year filter's year (or the latest), unless its own picker chose another. */
  function renderCalendar() {
    const year = state.calYear ?? (state.year === "all" ? years[0] : state.year);
    const list = filtered(year);
    const byDay = new Map();
    for (const w of list) byDay.set(w.date, (byDay.get(w.date) || 0) + w.mi);
    const bins = [0, 2, 4, 6];
    const binOf = (mi) => (mi == null ? 0 : mi < bins[1] ? 1 : mi < bins[2] ? 2 : mi < bins[3] ? 3 : 4);
    const chart = chartCard($("card-calendar"), {
      title: `${year} day by day`,
      table: { head: ["Date", "Miles"], rows: [...byDay.entries()].sort().reverse().map(([d, mi]) => [d, fmtNum(mi, 2)]) },
    });
    const picker = el("select", { class: "card-select", "aria-label": "Calendar year" }, years.map((y) => el("option", { value: y, text: y })));
    picker.value = String(year);
    picker.onchange = () => { state.calYear = Number(picker.value); renderCalendar(); };
    const head = $("card-calendar").querySelector(".card-head");
    head.insertBefore(picker, head.querySelector("h2").nextSibling);

    const width = Math.max(chart.clientWidth, 280);
    const jan1 = new Date(year, 0, 1);
    const offset = (jan1.getDay() + 6) % 7;
    const daysInYear = (new Date(year + 1, 0, 1) - jan1) / 864e5;
    const weeks = Math.ceil((daysInYear + offset) / 7);
    const left = 30, top = 16;
    const cell = Math.max(6, Math.min(16, Math.floor((width - left) / weeks) - 2));
    const gap = 2, pitch = cell + gap;
    const height = top + 7 * pitch + 4;
    const root = svg("svg", { viewBox: `0 0 ${Math.max(width, left + weeks * pitch)} ${height}`, role: "img", "aria-label": `Calendar of ${year}` });
    ["Mon", "Wed", "Fri"].forEach((d, i) => {
      svg("text", { class: "axis-label", x: 0, y: top + (i * 2) * pitch + cell - 1 }, root).textContent = d;
    });
    for (let mo = 0; mo < 12; mo++) {
      const first = new Date(year, mo, 1);
      const wk = Math.floor(((first - jan1) / 864e5 + offset) / 7);
      svg("text", { class: "axis-label", x: left + wk * pitch, y: 11 }, root).textContent = MONTHS[mo];
    }
    for (let i = 0; i < daysInYear; i++) {
      const d = new Date(year, 0, 1 + i);
      if (d > today) break;
      const key = `${year}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      const mi = byDay.get(key);
      const wk = Math.floor((i + offset) / 7), dow = (i + offset) % 7;
      const rect = svg("rect", {
        x: left + wk * pitch, y: top + dow * pitch, width: cell, height: cell, rx: 2,
        fill: css(`--cal-${binOf(mi)}`), tabindex: mi ? 0 : null, class: mi ? "hit-cell" : null,
        "aria-label": mi ? `${key}: ${fmtNum(mi, 1)} miles` : null, style: mi ? "cursor:pointer" : null,
      }, root);
      const show = (e) => showTip(e, mi ? fmtMi(mi) : `No ${activityInfo().nouns}`, fmtDate(d, { weekday: "short", month: "short", day: "numeric", year: "numeric" }));
      rect.addEventListener("pointermove", show);
      rect.addEventListener("focus", show);
      rect.addEventListener("pointerleave", hideTip);
      rect.addEventListener("blur", hideTip);
      if (mi) rect.addEventListener("click", () => {
        const w = list.find((x) => x.date === key);
        if (w) selectWalk(w.id, true);
      });
    }
    chart.replaceChildren(root, el("div", { class: "legend" }, [
      el("span", { class: "legend-item" }, [el("span", { class: "key-cell", style: `background:${css("--cal-0")}` }), "None"]),
      el("span", { class: "legend-item" }, [el("span", { class: "key-cell", style: `background:${css("--cal-1")}` }), "Under 2 mi"]),
      el("span", { class: "legend-item" }, [el("span", { class: "key-cell", style: `background:${css("--cal-2")}` }), "2–4 mi"]),
      el("span", { class: "legend-item" }, [el("span", { class: "key-cell", style: `background:${css("--cal-3")}` }), "4–6 mi"]),
      el("span", { class: "legend-item" }, [el("span", { class: "key-cell", style: `background:${css("--cal-4")}` }), "6 mi and up"]),
    ]));
  }

  const placeName = (w) => w.place || w.city || "Unknown";
  const withDetail = (w) => (w.detail ? `${placeName(w)} · ${w.detail}` : placeName(w));
  /** City and state (or country) for a place line, leaving out names already said, e.g. Mexico City, Mexico. */
  const whereParts = (w) => {
    const last = w.state && w.state !== w.place ? w.state : w.country;
    return [w.city, last].filter((s, i, a) => s && s !== w.place && a.indexOf(s) === i);
  };
  const placeLine = (w) => [w.place ? withDetail(w) : null, ...whereParts(w)].filter(Boolean).join(", ");

  const capital = (s) => s[0].toUpperCase() + s.slice(1);

  /** Per-place totals for a list of workouts, most miles first. */
  function placeStats(list) {
    const totals = new Map();
    for (const w of list) {
      const key = placeName(w);
      let t = totals.get(key);
      if (!t) {
        t = { name: key, url: w.place ? w.url : null, mi: 0, count: 0, secs: 0, paceMi: 0, first: w.day, last: w.day,
              sub: w.place ? whereParts(w).join(", ") : (w.state || w.country || "") };
        totals.set(key, t);
      }
      t.mi += w.mi; t.count += 1;
      if (w.dur && w.mi > 0.05) { t.secs += w.dur; t.paceMi += w.mi; }
      if (w.day < t.first) t.first = w.day;
      if (w.day > t.last) t.last = w.day;
    }
    for (const t of totals.values()) t.pace = t.paceMi ? t.secs / 60 / t.paceMi : null;
    return [...totals.values()].sort((a, b) => b.mi - a.mi);
  }

  /** Horizontal bars: top places by miles. Click a place to filter the page to it. */
  function renderPlaces() {
    const info = activityInfo();
    const ranked = placeStats(filtered(state.year, { anyPlace: true }));
    const top = ranked.slice(0, 12);
    if (state.place !== "all" && !top.some((t) => t.name === state.place)) {
      const sel = ranked.find((t) => t.name === state.place);
      if (sel) top.push(sel);
    }
    const chart = chartCard($("card-places"), {
      title: "Where you walk", art: "evergreen",
      note: `Top places by miles. Click a bar to see only that place; linked names open the park's own page. ${info.nouns[0].toUpperCase() + info.nouns.slice(1)} outside a park are counted by city. ${fmtNum(ranked.length)} ${ranked.length === 1 ? "place" : "places"} in all.`,
      table: {
        head: ["Place", "Miles", capital(info.nouns), "First", "Latest", "Pace /mi"],
        rows: ranked.map((t) => [t.sub ? `${t.name} (${t.sub})` : t.name, fmtNum(t.mi, 1), fmtNum(t.count),
          fmtDate(t.first, { month: "short", year: "numeric" }), fmtDate(t.last, { month: "short", year: "numeric" }), fmtPace(t.pace)]),
      },
    });
    if (!top.length) { chart.append(el("p", { class: "empty-note", text: "No data for this selection." })); return; }
    const max = Math.max(...top.map((t) => t.mi));
    const rows = top.map((t) => {
      const active = state.place === t.name;
      const dim = state.place !== "all" && !active;
      const pick = () => setPlace(active ? "all" : t.name);
      const row = el("div", {
        class: `hbar-row${active ? " active" : ""}`, tabindex: 0, role: "button",
        "aria-pressed": String(active), "aria-label": `${t.name}: ${fmtMi(t.mi)}, ${t.count} ${info.nouns}. ${active ? "Show all places" : "Show only this place"}`,
      }, [
        el("div", { class: "hbar-label" }, [
          t.url ? el("a", { class: "hbar-name", href: t.url, target: "_blank", rel: "noopener", text: t.name, onclick: (e) => e.stopPropagation() })
                : el("span", { class: "hbar-name", text: t.name }),
          t.sub ? el("span", { class: "hbar-sub", text: t.sub }) : null]),
        el("div", { class: "hbar-track" }, el("div", { class: `hbar-bar${dim ? " deemph" : ""}`, style: `width:${Math.max(1, (t.mi / max) * 100)}%` })),
        el("div", { class: "hbar-value", text: t.mi >= 10 ? fmtNum(t.mi, 0) : fmtNum(t.mi, 1) }),
      ]);
      const show = (e) => showTip(e, fmtMi(t.mi), `${t.name} · ${t.count} ${t.count === 1 ? info.noun : info.nouns}`,
        `Since ${fmtDate(t.first, { month: "short", year: "numeric" })} · ${fmtPace(t.pace)} /mi`);
      row.addEventListener("pointermove", show);
      row.addEventListener("focus", show);
      row.addEventListener("pointerleave", hideTip);
      row.addEventListener("blur", hideTip);
      row.addEventListener("click", pick);
      row.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
      return row;
    });
    chart.replaceChildren(el("div", { class: "hbars" }, rows));
  }

  /** Heat table: top places (rows) by year, or by month when a year is picked. Shade = miles. */
  function renderPlaceYears() {
    const info = activityInfo();
    const list = filtered(state.year, { anyPlace: true });
    const byYear = state.year === "all";
    const cols = byYear ? [...years].reverse() : MONTHS.map((_, i) => i + 1);
    const colLabel = (c) => (byYear ? `'${String(c).slice(2)}` : MONTHS[c - 1]);
    const colOf = (w) => (byYear ? w.year : w.month);
    const top = placeStats(list).slice(0, 12);
    if (state.place !== "all" && !top.some((t) => t.name === state.place)) {
      const sel = placeStats(list).find((t) => t.name === state.place);
      if (sel) top.push(sel);
    }
    const cells = new Map();
    for (const w of list) {
      const k = `${placeName(w)}|${colOf(w)}`;
      const c = cells.get(k) || { mi: 0, n: 0 };
      c.mi += w.mi; c.n += 1;
      cells.set(k, c);
    }
    const max = Math.max(0, ...[...cells.values()].map((c) => c.mi));
    const edges = [0.25, 0.5, 0.75].map((f) => f * max);
    const bin = (mi) => (!mi ? 0 : mi < edges[0] ? 1 : mi < edges[1] ? 2 : mi < edges[2] ? 3 : 4);

    const chart = chartCard($("card-place-years"), {
      title: byYear ? "Places over the years" : `Places month by month, ${state.year}`,
      note: `Miles at each of your top places, ${byYear ? "year by year" : "by month"}. Shows which spots you've taken up, dropped, or keep coming back to.`,
      table: {
        head: ["Place", ...cols.map((c) => (byYear ? String(c) : MONTHS[c - 1]))],
        rows: top.map((t) => [t.name, ...cols.map((c) => { const v = cells.get(`${t.name}|${c}`); return v ? fmtNum(v.mi, 1) : ""; })]),
      },
    });
    if (!top.length) { chart.append(el("p", { class: "empty-note", text: "No data for this selection." })); return; }

    const grid = el("div", { class: "heat", style: `grid-template-columns: minmax(120px, 220px) repeat(${cols.length}, minmax(18px, 1fr))` });
    grid.append(el("div", { class: "heat-corner" }), ...cols.map((c) => el("div", { class: "heat-col axis-text", text: colLabel(c) })));
    for (const t of top) {
      const active = state.place === t.name;
      grid.append(el("button", {
        type: "button", class: `heat-row-label${active ? " active" : ""}`, text: t.name, title: t.name,
        onclick: () => setPlace(active ? "all" : t.name),
      }));
      for (const c of cols) {
        const v = cells.get(`${t.name}|${c}`);
        const cell = el("div", {
          class: "heat-cell", tabindex: v ? 0 : null, style: `background:${css(`--cal-${bin(v && v.mi)}`)}`,
          "aria-label": v ? `${t.name}, ${byYear ? c : MONTHS[c - 1]}: ${fmtNum(v.mi, 1)} miles` : null,
        });
        const show = (e) => showTip(e, v ? fmtMi(v.mi) : `No ${info.nouns}`, `${t.name} · ${byYear ? c : `${MONTHS[c - 1]} ${state.year}`}`,
          v ? `${v.n} ${v.n === 1 ? info.noun : info.nouns}` : null);
        cell.addEventListener("pointermove", show);
        cell.addEventListener("focus", show);
        cell.addEventListener("pointerleave", hideTip);
        cell.addEventListener("blur", hideTip);
        grid.append(cell);
      }
    }
    const r = (x) => fmtNum(x, x < 10 ? 1 : 0);
    chart.replaceChildren(grid, el("div", { class: "legend" }, [
      el("span", { class: "legend-item" }, [el("span", { class: "key-cell", style: `background:${css("--cal-0")}` }), "None"]),
      el("span", { class: "legend-item" }, [el("span", { class: "key-cell", style: `background:${css("--cal-1")}` }), `Under ${r(edges[0])} mi`]),
      el("span", { class: "legend-item" }, [el("span", { class: "key-cell", style: `background:${css("--cal-2")}` }), `${r(edges[0])}–${r(edges[1])} mi`]),
      el("span", { class: "legend-item" }, [el("span", { class: "key-cell", style: `background:${css("--cal-3")}` }), `${r(edges[1])}–${r(edges[2])} mi`]),
      el("span", { class: "legend-item" }, [el("span", { class: "key-cell", style: `background:${css("--cal-4")}` }), `${r(edges[2])} mi and up`]),
    ]));
  }

  /** Stacked columns: places visited each year, split into new-to-you and returning. */
  function renderExplore() {
    const card = $("card-explore");
    card.hidden = state.place !== "all";
    if (card.hidden) return;
    const info = activityInfo();
    const all = workouts.filter((w) => state.activity === "all" || w.activity === state.activity).sort((a, b) => a.day - b.day);
    const firstSeen = new Map();
    for (const w of all) if (!firstSeen.has(placeName(w))) firstSeen.set(placeName(w), w.year);
    const items = [...years].reverse().map((y) => {
      const visited = new Set(all.filter((w) => w.year === y).map(placeName));
      const fresh = [...visited].filter((p) => firstSeen.get(p) === y);
      return { key: y, label: String(y), short: `'${String(y).slice(2)}`, values: [fresh.length, visited.size - fresh.length], fresh };
    });
    const chart = chartCard(card, {
      title: "Exploring",
      note: `Places you ${info.verb === "walked" ? "walked" : "went"} each year: new to you, or returning favorites.${state.year !== "all" ? ` ${state.year} is highlighted.` : ""}`,
      table: { head: ["Year", "New places", "Returning places"], rows: items.slice().reverse().map((d) => [d.label, fmtNum(d.values[0]), fmtNum(d.values[1])]) },
    });
    stackedColumnChart(chart, items, {
      series: [{ name: "New places", color: css("--series-1") }, { name: "Returning", color: css("--series-2") }],
      highlight: state.year === "all" ? null : state.year,
      tip: (d) => [`${d.values[0] + d.values[1]} places in ${d.label}`, `${d.values[0]} new · ${d.values[1]} returning`,
        d.fresh.length ? `New: ${d.fresh.slice(0, 3).join(", ")}${d.fresh.length > 3 ? ` +${d.fresh.length - 3} more` : ""}` : null],
      onClick: (d) => setYear(state.year === d.key ? "all" : d.key),
    });
  }

  /** Stacked columns (any number of series) with a 2px surface gap between segments and a legend. */
  function stackedColumnChart(chart, items, { series, highlight, tip, onClick, fmt = (v) => fmtNum(v), markers = [] }) {
    const totalOf = (d) => d.values.reduce((a, b) => a + b, 0);
    const width = Math.max(chart.clientWidth, 280), height = 220;
    const m = { top: 22, right: 4, bottom: 24, left: 36 };
    const plotW = width - m.left - m.right, plotH = height - m.top - m.bottom;
    const max = niceMax(Math.max(...items.map(totalOf), 0));
    const y = (v) => m.top + plotH - (v / max) * plotH;
    const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });
    for (let i = 0; i <= 4; i++) {
      const v = (max / 4) * i, yy = Math.round(y(v)) + 0.5;
      svg("line", { class: i ? "gridline" : "baseline", x1: m.left, x2: width - m.right, y1: yy, y2: yy }, root);
      svg("text", { class: "axis-label", x: m.left - 6, y: yy + 4, "text-anchor": "end" }, root).textContent = compact(v);
    }
    const band = plotW / items.length, barW = Math.min(24, Math.max(4, band - 4)), gap = 2;
    items.forEach((d, i) => {
      const cx = m.left + band * i + band / 2, x0 = cx - barW / 2, x1 = cx + barW / 2;
      const total = totalOf(d);
      const hit = svg("rect", { class: "hit", x: cx - band / 2, y: m.top, width: band, height: plotH, tabindex: 0, role: onClick ? "button" : "img",
        "aria-label": `${d.label}: ${series.map((sr, k) => `${sr.name} ${fmt(d.values[k])}`).join(", ")}` }, root);
      let base = m.top + plotH, below = false;
      const lastSeg = d.values.reduce((k, v, s) => (v ? s : k), -1);
      d.values.forEach((v, s) => {
        if (!v) return;
        const h = (v / max) * plotH;
        const isTop = s === lastSeg;
        const top = base - h + (below ? gap : 0);
        const r = isTop ? Math.min(4, h, barW / 2) : 0;
        const fill = highlight != null && d.key !== highlight ? css("--deemph") : series[s].color;
        svg("path", {
          d: `M${x0},${base}V${top + r}Q${x0},${top} ${x0 + r},${top}H${x1 - r}Q${x1},${top} ${x1},${top + r}V${base}Z`,
          fill, "pointer-events": "none",
        }, root);
        base -= h;
        below = true;
      });
      if (total && (highlight == null ? total === Math.max(...items.map(totalOf)) : d.key === highlight)) {
        svg("text", { class: "value-label", x: cx, y: y(total) - 6, "text-anchor": "middle" }, root).textContent = fmt(total);
      }
      svg("text", { class: "axis-label", x: cx, y: height - 6, "text-anchor": "middle" }, root).textContent = d.short || d.label;
      const lines = tip(d);
      const show = (e) => showTip(e, lines[0], lines[1], lines[2]);
      hit.addEventListener("pointermove", show);
      hit.addEventListener("focus", show);
      hit.addEventListener("pointerleave", hideTip);
      hit.addEventListener("blur", hideTip);
      if (onClick) {
        hit.addEventListener("click", () => onClick(d));
        hit.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(d); } });
      }
    });
    drawMarkers(root, markers, { x: (mk) => m.left + band * mk.pos, top: m.top, bottom: m.top + plotH, left: m.left, right: width - m.right });
    chart.replaceChildren(root, el("div", { class: "legend" }, series.map((s) =>
      el("span", { class: "legend-item" }, [el("span", { class: "key-cell", style: `background:${s.color}` }), s.name]))));
  }

  // ---------- records ----------
  function renderRecords() {
    const list = filtered();
    const info = activityInfo();
    const out = $("records");
    out.replaceChildren();
    if (!list.length) { out.append(el("dd", { class: "empty-note", text: "No data for this selection." })); return; }
    const walkLink = (w, text) => el("button", { type: "button", class: "link-button", text, onclick: () => selectWalk(w.id, true) });
    const add = (label, value, sub) => out.append(el("dt", { text: label }), el("dd", {}, [value, sub ? el("div", { class: "rec-sub", text: sub }) : null]));

    const longest = list.reduce((b, w) => (w.mi > b.mi ? w : b));
    add(`Longest ${info.noun}`, walkLink(longest, `${fmtMi(longest.mi)} on ${fmtDate(longest.day)}`));
    const paced = list.filter((w) => w.pace && w.mi >= 2);
    if (paced.length) {
      const fastest = paced.reduce((b, w) => (w.pace < b.pace ? w : b));
      add(`Fastest ${info.noun} (2+ mi)`, walkLink(fastest, `${fmtPace(fastest.pace)} /mi on ${fmtDate(fastest.day)}`), fmtMi(fastest.mi));
    }
    const climb = list.filter((w) => w.ft).reduce((b, w) => (!b || w.ft > b.ft ? w : b), null);
    if (climb) add("Most climbing", walkLink(climb, `${fmtNum(climb.ft)} ft on ${fmtDate(climb.day)}`), fmtMi(climb.mi));

    // Longest streak of consecutive days.
    const days = [...new Set(list.map((w) => w.date))].sort();
    let best = { len: 0 }, run = 0, start = null, prev = null;
    for (const d of days) {
      const t = new Date(`${d}T12:00`);
      if (prev && (t - prev) / 864e5 < 1.5) run += 1; else { run = 1; start = t; }
      if (run > best.len) best = { len: run, start, end: t };
      prev = t;
    }
    add("Longest streak", el("strong", { text: `${best.len} day${best.len === 1 ? "" : "s"} in a row` }),
      best.len > 1 ? `${fmtDate(best.start, { month: "short", day: "numeric" })} – ${fmtDate(best.end)}` : null);

    // Busiest week (Mon–Sun) and month.
    const weekKey = (w) => { const d = new Date(w.day); d.setDate(d.getDate() - w.weekday); return d.getTime(); };
    const weeks = new Map(), months = new Map();
    for (const w of list) {
      weeks.set(weekKey(w), (weeks.get(weekKey(w)) || 0) + w.mi);
      const mk = `${w.year}-${w.month}`;
      months.set(mk, (months.get(mk) || 0) + w.mi);
    }
    const [wk, wkMi] = [...weeks.entries()].reduce((b, e) => (e[1] > b[1] ? e : b));
    add("Biggest week", el("strong", { text: fmtMi(wkMi) }), `Week of ${fmtDate(new Date(wk))}`);
    const [mk, mkMi] = [...months.entries()].reduce((b, e) => (e[1] > b[1] ? e : b));
    const [my, mm] = mk.split("-").map(Number);
    add("Biggest month", el("strong", { text: fmtMi(mkMi) }), fmtDate(new Date(my, mm - 1, 1), { month: "long", year: "numeric" }));

    if (state.place === "all") {
      const ranked = placeStats(list).sort((a, b) => b.count - a.count);
      const fav = ranked[0];
      if (fav) add("Most visited place", el("button", { type: "button", class: "link-button", text: fav.name, onclick: () => setPlace(fav.name) }),
        `${fmtNum(fav.count)} ${fav.count === 1 ? info.noun : info.nouns}, ${fmtMi(fav.mi)}`);
      add("Places explored", el("strong", { text: fmtNum(ranked.length) }),
        `${fmtNum(ranked.filter((t) => t.count === 1).length)} visited just once`);
    } else {
      const first = list.reduce((b, w) => (w.day < b.day ? w : b));
      add("First visit", walkLink(first, fmtDate(first.day)), `${fmtNum(list.length)} ${list.length === 1 ? info.noun : info.nouns} here in all`);
    }

    const early = list.filter((w) => w.start).reduce((b, w) => (!b || w.start.slice(11) < b.start.slice(11) ? w : b), null);
    if (early) add(`Earliest start`, walkLink(early, `${new Date(early.start).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })} on ${fmtDate(early.day)}`));
  }

  // ---------- Google My Maps ----------
  function renderMyMaps() {
    const box = $("mymaps");
    box.className = "mymaps";
    box.replaceChildren();
    const id = (CONFIG.myMapsId || "").trim();
    if (id) {
      box.append(
        el("iframe", { class: "mymaps-frame", title: "GoneWalking routes in Google My Maps", loading: "lazy", src: `https://www.google.com/maps/d/embed?mid=${encodeURIComponent(id)}` }),
        el("p", {}, el("a", { href: `https://www.google.com/maps/d/viewer?mid=${encodeURIComponent(id)}`, target: "_blank", rel: "noopener", text: "Open in Google My Maps ↗" })));
    } else {
      const ol = el("ol", {}, [
        el("li", {}, ["Download ", el("a", { href: "data/kml/all_routes.kml", download: "", text: "all_routes.kml" }), "."]),
        el("li", {}, ["At ", el("a", { href: "https://mymaps.google.com", target: "_blank", rel: "noopener", text: "mymaps.google.com" }), ", choose ", el("strong", { text: "Create a new map" }), ", then ", el("strong", { text: "Import" }), " and upload the file."]),
        el("li", {}, ["To show it here: ", el("strong", { text: "Share" }), " → anyone with the link can view, copy the ", el("code", { text: "mid=" }), " value from the map's address, and paste it into ", el("code", { text: "docs/config.js" }), "."]),
      ]);
      box.append(ol);
    }
    const years = DATA.meta.kml_years || [];
    box.append(
      el("div", { class: "tile-label", text: "Download KML" }),
      el("div", { class: "downloads" }, [
        el("a", { class: "chip primary", href: "data/kml/all_routes.kml", download: "", text: "All routes" }),
        ...years.map((y) => el("a", { class: "chip", href: `data/kml/${y}.kml`, download: "", text: String(y) })),
      ]));
  }

  // ---------- map ----------
  let map, baseLayer, routeLayer, selectedLayer;
  const routeLines = new Map();

  // Each home's area is centred on the median start point of the walks made while living there;
  // walks starting farther than HOME_KM from the home of their time are "away".
  const HOME_KM = 40;
  const km = (a, b) => Math.hypot((a[0] - b[0]) * 111, (a[1] - b[1]) * 111 * Math.cos(a[0] * Math.PI / 180));
  const homeCenters = (() => {
    const med = (arr) => { const s = arr.slice().sort((a, b) => a - b); return s[Math.floor(s.length / 2)]; };
    const centre = (ws) => (ws.length ? [med(ws.map((w) => w.latlon[0])), med(ws.map((w) => w.latlon[1]))] : null);
    const located = workouts.filter((w) => w.latlon);
    const out = new Map([[null, centre(located)]]);
    for (const h of HOMES) out.set(h.name, centre(located.filter((w) => w.home === h.name)) || out.get(null));
    return out;
  })();
  // "Home" means the home metro when a walk's region is known; otherwise within HOME_KM of the home of the time.
  const isHome = (w, homeName) => {
    if (homeName === undefined && w.region) return w.region === REGIONS[0];
    const c = homeCenters.get(homeName !== undefined ? homeName : (homeCenters.has(w.home) ? w.home : null));
    return !c || !w.latlon || km(w.latlon, c) <= HOME_KM;
  };
  const mapViews = [
    ["home", HOMES.length > 1 ? "Home areas" : "Home area"],
    ...(HOMES.length > 1 ? HOMES.map((h) => [h.name, h.short]) : []),
    ["all", "Everywhere"],
  ];
  let mapView = "home";

  function initMap() {
    if (!window.L) { $("map").append(el("p", { class: "empty-note", text: "The map library couldn't load (offline?)." })); return; }
    map = L.map("map", { preferCanvas: true });
    baseLayer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    routeLayer = L.layerGroup().addTo(map);
    selectedLayer = L.layerGroup().addTo(map);

    const ViewControl = L.Control.extend({
      onAdd() {
        const box = L.DomUtil.create("div", "map-view-toggle segmented");
        box.setAttribute("role", "radiogroup");
        box.setAttribute("aria-label", "Map extent");
        for (const [key, label] of mapViews) {
          const b = L.DomUtil.create("button", "", box);
          b.type = "button";
          b.dataset.view = key;
          b.setAttribute("role", "radio");
          b.textContent = label;
          L.DomEvent.on(b, "click", (e) => { L.DomEvent.stop(e); mapView = key; state.selected = null; renderMap(true); renderDetail(); renderLog(); });
        }
        L.DomEvent.disableClickPropagation(box);
        return box;
      },
    });
    new ViewControl({ position: "topright" }).addTo(map);
  }

  function latlngs(id) {
    return (ROUTES[id] || []).map((seg) => {
      const pts = [];
      for (let i = 0; i < seg.length; i += 3) pts.push([seg[i], seg[i + 1]]);
      return pts;
    });
  }

  function renderMap(fit = true) {
    if (!map) return;
    routeLayer.clearLayers();
    routeLines.clear();
    const color = css("--series-1");
    const dim = state.selected != null;
    const bounds = L.latLngBounds([]), homeBounds = L.latLngBounds([]);
    // A single home's view frames the walks made from that home (within HOME_KM of it).
    const oneHome = HOMES.some((h) => h.name === mapView) ? mapView : null;
    let away = 0;
    for (const w of filtered()) {
      const ll = latlngs(w.id);
      if (!ll.length) continue;
      const line = L.polyline(ll, { color: dim ? css("--deemph") : color, weight: 2, opacity: dim ? 0.5 : 0.55, interactive: true });
      line.on("click", () => selectWalk(w.id, false));
      line.on("mouseover", (e) => { if (state.selected !== w.id) line.setStyle({ weight: 4, opacity: 0.9 }); showTip(e.originalEvent, fmtMi(w.mi), `${fmtDate(w.day)} · ${withDetail(w)}`); });
      line.on("mouseout", () => { if (state.selected !== w.id) line.setStyle({ weight: 2, opacity: dim ? 0.5 : 0.55 }); hideTip(); });
      line.addTo(routeLayer);
      routeLines.set(w.id, line);
      bounds.extend(line.getBounds());
      if (oneHome ? w.home === oneHome && isHome(w, oneHome) : isHome(w)) homeBounds.extend(line.getBounds());
      if (!isHome(w)) {
        away += 1;
        if (w.latlon) {
          L.circleMarker(w.latlon, { radius: 5, color: css("--surface"), weight: 2, fillColor: w.region ? regionColor(w.region) : color, fillOpacity: dim ? 0.5 : 1 })
            .on("click", () => selectWalk(w.id, false))
            .on("mouseover", (e) => showTip(e.originalEvent, fmtMi(w.mi), `${fmtDate(w.day)} · ${placeLine(w) || placeName(w)}`))
            .on("mouseout", hideTip)
            .addTo(routeLayer);
        }
      }
    }
    const awayText = away ? ` ${fmtNum(away)} of them away from home (choose Everywhere to see them).` : "";
    $("map-note").textContent = `${fmtNum(routeLines.size)} routes.${awayText} Click a route to see its details and pace along the way.`;
    const target = mapView !== "all" && homeBounds.isValid() ? homeBounds : bounds;
    if (fit && target.isValid() && state.selected == null) map.fitBounds(target, { padding: [20, 20] });
    document.querySelectorAll(".map-view-toggle button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.view === mapView)));
    drawSelected(false);
  }

  /** Split a route into 5 pace bands, relative to that route's own spread. */
  function paceBands(id) {
    const segs = ROUTES[id] || [];
    const pieces = [];
    for (const seg of segs) {
      for (let i = 3; i < seg.length; i += 3) {
        const a = [seg[i - 3], seg[i - 2]], b = [seg[i], seg[i + 1]], dt = seg[i + 2] - seg[i - 1];
        const meters = L.latLng(a).distanceTo(b);
        if (meters < 1) continue;
        pieces.push({ a, b, pace: dt > 0 ? (dt / 60) / (meters / 1609.344) : null });
      }
    }
    const paces = pieces.map((p) => p.pace).filter((p) => p != null && isFinite(p)).sort((x, y) => x - y);
    const q = (p) => paces[Math.min(paces.length - 1, Math.floor(p * paces.length))];
    const edges = paces.length ? [q(0.2), q(0.4), q(0.6), q(0.8)] : [];
    for (const p of pieces) {
      // band 1 = slowest .. 5 = fastest
      p.band = p.pace == null ? 1 : 5 - edges.filter((e) => p.pace > e).length;
    }
    return { pieces, edges, slow: q(0.9), fast: q(0.1) };
  }

  function drawSelected(zoom) {
    if (!map) return;
    selectedLayer.clearLayers();
    const id = state.selected;
    if (id == null || !ROUTES[id]) return;
    const { pieces } = paceBands(id);
    const surface = css("--surface");
    const ll = latlngs(id);
    ll.forEach((seg) => L.polyline(seg, { color: surface, weight: 8, opacity: 0.9, interactive: false }).addTo(selectedLayer));
    // Merge consecutive pieces of the same band into one polyline.
    let run = null;
    const flush = () => { if (run) L.polyline(run.pts, { color: css(`--seq-${run.band}`), weight: 4, opacity: 1, interactive: false }).addTo(selectedLayer); };
    for (const p of pieces) {
      if (run && run.band === p.band && run.last === p.a) { run.pts.push(p.b); run.last = p.b; }
      else { flush(); run = { band: p.band, pts: [p.a, p.b], last: p.b }; }
    }
    flush();
    const start = ll[0][0];
    L.circleMarker(start, { radius: 6, color: surface, weight: 2, fillColor: css("--series-2"), fillOpacity: 1 }).addTo(selectedLayer).bindTooltip("Start");
    if (zoom) map.fitBounds(L.latLngBounds(ll.flat()), { padding: [30, 30], maxZoom: 16 });
  }

  function selectWalk(id, scrollToMap, { push = false } = {}) {
    state.selected = id;
    const w = workouts.find((x) => x.id === id);
    if (w && state.year !== "all" && w.year !== state.year) { state.year = w.year; $("filter-year").value = String(w.year); renderAll(); }
    renderMap(false);
    drawSelected(true);
    renderDetail();
    renderLog();
    writeHash(push);
    if (scrollToMap) document.querySelector(".map-card").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function walkKml(w) {
    const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
    const lines = latlngs(w.id).map((seg) =>
      `<Placemark><name>${esc(`${fmtDate(w.day)} · ${fmtMi(w.mi)} · ${withDetail(w)}`)}</name><description>${esc(`MapMyWalk walk ${w.id}: ${w.link}`)}</description><LineString><tessellate>1</tessellate><coordinates>${seg.map(([la, lo]) => `${lo},${la}`).join(" ")}</coordinates></LineString></Placemark>`);
    // The document name becomes the layer name in Google My Maps; the walk ID keeps it unique.
    const title = `${w.date} ${placeName(w)} (walk ${w.id})`;
    return `<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>${esc(title)}</name>${lines.join("")}</Document></kml>\n`;
  }

  /** Keep the address in sync: #place=<name>&walk=<id>. push adds a history entry (for Back). */
  function writeHash(push = false) {
    const parts = [];
    if (state.place !== "all") parts.push(`place=${encodeURIComponent(state.place)}`);
    if (state.selected != null) parts.push(`walk=${state.selected}`);
    const hash = parts.length ? `#${parts.join("&")}` : location.pathname + location.search;
    // Stepping newer/older replaces the entry but remembers it was opened from the list.
    const fromList = push || Boolean(state.selected != null && history.state && history.state.fromList);
    const entry = { walk: state.selected, fromList };
    if (push) history.pushState(entry, "", hash); else history.replaceState(entry, "", hash);
  }

  /** Back to the overview (or the place's walk list) with nothing selected. */
  function clearSelection() {
    state.selected = null;
    writeHash();
    renderMap(true);
    renderDetail();
    renderLog();
  }

  /** Walks in the current selection at the chosen place, newest first. */
  const placeWalks = () => filtered().slice().sort((a, b) => b.day - a.day || (b.start || "").localeCompare(a.start || ""));

  /** Emphasize one route on the map while its list row is hovered. */
  function previewRoute(id, on) {
    const line = routeLines.get(id);
    if (!line || state.selected != null) return;
    line.setStyle(on ? { color: css("--series-2"), weight: 5, opacity: 1 } : { color: css("--series-1"), weight: 2, opacity: 0.55 });
    if (on) line.bringToFront();
  }

  function renderPlaceWalks(box) {
    const info = activityInfo();
    const list = placeWalks();
    const total = sum(list, (w) => w.mi);
    const first = list.length ? list[list.length - 1] : null;
    const url = list.find((w) => w.url) ? list.find((w) => w.url).url : null;
    const items = list.map((w) => {
      const row = el("li", {}, el("button", { type: "button", class: "walk-item", onclick: () => selectWalk(w.id, false, { push: true }) }, [
        el("span", { class: "walk-item-date", text: fmtDate(w.day) }),
        el("span", { class: "walk-item-stats", text: `${fmtMi(w.mi)} · ${fmtPace(w.pace)} /mi` }),
        w.detail ? el("span", { class: "walk-item-sub", text: w.detail }) : null,
      ]));
      row.addEventListener("pointerenter", () => previewRoute(w.id, true));
      row.addEventListener("pointerleave", () => previewRoute(w.id, false));
      row.addEventListener("focusin", () => previewRoute(w.id, true));
      row.addEventListener("focusout", () => previewRoute(w.id, false));
      return row;
    });
    box.replaceChildren(
      el("h3", {}, url ? el("a", { href: url, target: "_blank", rel: "noopener", text: `${state.place} ↗` }) : state.place),
      el("div", { class: "when", text: list.length
        ? `${fmtNum(list.length)} ${list.length === 1 ? info.noun : info.nouns} · ${fmtMi(total)}${first ? ` · since ${fmtDate(first.day, { month: "short", year: "numeric" })}` : ""}`
        : `No ${info.nouns} here in this selection.` }),
      el("p", { class: "placeholder small", text: `Pick a ${info.noun} to see its route and details.` }),
      el("ol", { class: "walk-list" }, items),
      el("button", { type: "button", class: "link-button", text: "Show all places", onclick: () => setPlace("all") }));
  }

  /** Back to the list, plus newer/older, when browsing a place's walks. */
  function placeNav(w) {
    if (state.place === "all") return null;
    const list = placeWalks();
    const i = list.findIndex((x) => x.id === w.id);
    if (i < 0) return null;
    const go = (x) => selectWalk(x.id, false);
    return el("div", { class: "walk-nav" }, [
      el("button", { type: "button", class: "link-button back", text: `← All ${fmtNum(list.length)} at ${state.place}`, onclick: () => {
        // Opened from the list: step back in history (so Forward works too). Opened from a link: just clear.
        if (history.state && history.state.fromList) history.back(); else clearSelection();
      } }),
      el("div", { class: "walk-nav-steps" }, [
        el("button", { type: "button", class: "step", text: "‹ Newer", disabled: i === 0 ? "" : null, "aria-label": "Newer walk", onclick: () => go(list[i - 1]) }),
        // Numbered oldest-first, to match "Visit N of M" below.
        el("span", { class: "walk-nav-pos", text: `Visit ${list.length - i} of ${list.length}` }),
        el("button", { type: "button", class: "step", text: "Older ›", disabled: i === list.length - 1 ? "" : null, "aria-label": "Older walk", onclick: () => go(list[i + 1]) }),
      ]),
    ]);
  }

  function renderDetail() {
    const box = $("walk-detail");
    const w = workouts.find((x) => x.id === state.selected);
    if (!w) {
      if (state.place !== "all") { renderPlaceWalks(box); return; }
      box.replaceChildren(
        el("h3", { text: "No route selected" }),
        el("p", { class: "placeholder", text: "Click a route on the map, a dot in the pace chart, a day in the calendar, or a row in the walk log." }));
      return;
    }
    const bands = paceBands(w.id);
    // How this walk compares with the others at the same place.
    const here = workouts.filter((x) => x.activity === w.activity && placeName(x) === placeName(w)).sort((a, b) => a.day - b.day);
    const visitNo = here.findIndex((x) => x.id === w.id) + 1;
    const others = here.filter((x) => x.id !== w.id && x.pace && x.mi >= 0.5);
    const usual = others.length >= 2 ? sum(others, (x) => x.dur) / 60 / sum(others, (x) => x.mi) : null;
    const versus = usual && w.pace ? w.pace - usual : null;
    const visitText = here.length > 1
      ? `Visit ${visitNo} of ${here.length} to ${placeName(w)}${versus != null ? ` · ${fmtPace(Math.abs(versus))} ${versus < 0 ? "faster" : "slower"} than your usual ${fmtPace(usual)} /mi here` : ""}`
      : `Your only ${w.activity === "Walk" ? "walk" : "ride"} at ${placeName(w)}`;
    const rows = [
      ["Distance", fmtMi(w.mi)], ["Time", fmtDur(w.dur)], ["Pace", `${fmtPace(w.pace)} /mi`],
      ["Moving pace", `${fmtPace(w.mpace)} /mi`], ["Climbing", w.ft != null ? `${fmtNum(w.ft)} ft` : "–"],
      ["Steps", w.steps ? fmtNum(w.steps) : "–"],
    ];
    const kmlUrl = URL.createObjectURL(new Blob([walkKml(w)], { type: "application/vnd.google-earth.kml+xml" }));
    const time = w.start ? new Date(w.start).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) : "";
    const ageText = w.age != null ? ` · age ${w.age}` : "";
    box.replaceChildren(...[
      placeNav(w),
      el("h3", { text: `${fmtMi(w.mi)} ${w.activity === "Walk" ? "walk" : "ride"}` }),
      el("div", { class: "when", text: `${fmtDate(w.day, { weekday: "long", month: "long", day: "numeric", year: "numeric" })}${time ? ` · ${time}` : ""}${ageText}` }),
      placeLine(w) ? el("div", { class: "where" }, w.place && w.url ? [
        el("a", { href: w.url, target: "_blank", rel: "noopener", text: `${w.place} ↗` }),
        w.detail ? ` · ${w.detail}` : null,
        whereParts(w).length ? `, ${whereParts(w).join(", ")}` : null,
      ] : placeLine(w)) : null,
      el("p", { class: "visit", text: visitText }),
      w.notes ? el("p", { class: "notes", text: `“${w.notes}”` }) : null,
      el("dl", {}, rows.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })])),
      bands.pieces.length ? el("div", { class: "pace-legend" }, [
        "Pace along the route",
        el("div", { class: "ramp" }, [1, 2, 3, 4, 5].map((b) => el("span", { style: `background:${css(`--seq-${b}`)}` }))),
        el("div", { class: "ends" }, [el("span", { text: `Slower (${fmtPace(bands.slow)})` }), el("span", { text: `Faster (${fmtPace(bands.fast)})` })]),
      ]) : null,
      el("div", { class: "links" }, [
        el("a", { href: w.link.replace("http://", "https://"), target: "_blank", rel: "noopener", text: "View on MapMyWalk ↗" }),
        w.latlon ? el("a", { href: `https://www.google.com/maps/search/?api=1&query=${w.latlon[0]},${w.latlon[1]}`, target: "_blank", rel: "noopener", text: "Start point in Google Maps ↗" }) : null,
        el("a", { href: kmlUrl, download: `${w.date}-${w.id}.kml`, text: "Download this route (KML)" }),
        state.place === "all" ? el("button", { type: "button", class: "link-button", text: "Clear selection", onclick: () => clearSelection() }) : null,
      ]),
    ].filter(Boolean));
  }

  // ---------- walk log ----------
  const COLUMNS = [
    { key: "date", label: "Date", get: (w) => w.day.getTime(), show: (w) => fmtDate(w.day) },
    { key: "place", label: "Place", get: (w) => withDetail(w), show: (w) => withDetail(w) },
    { key: "activity", label: "Activity", get: (w) => w.activity, show: (w) => w.activity },
    { key: "mi", label: "Distance", num: true, get: (w) => w.mi, show: (w) => fmtMi(w.mi) },
    { key: "dur", label: "Time", num: true, get: (w) => w.dur, show: (w) => fmtDur(w.dur) },
    { key: "pace", label: "Pace /mi", num: true, get: (w) => w.pace, show: (w) => fmtPace(w.pace) },
    { key: "ft", label: "Climb ft", num: true, get: (w) => w.ft, show: (w) => (w.ft != null ? fmtNum(w.ft) : "–") },
    { key: "notes", label: "Notes", get: (w) => w.notes || "", show: (w) => w.notes || "", cls: "notes" },
  ];

  function renderLog() {
    const list = filtered().slice();
    const col = COLUMNS.find((c) => c.key === state.sort.key);
    list.sort((a, b) => {
      const va = col.get(a), vb = col.get(b);
      if (va == null) return 1;
      if (vb == null) return -1;
      return (va < vb ? -1 : va > vb ? 1 : 0) * state.sort.dir;
    });
    const limit = state.showAll ? list.length : 25;
    const table = $("log");
    const head = el("tr", {}, COLUMNS.map((c) => {
      const active = c.key === state.sort.key;
      return el("th", { class: c.num ? "num" : null, "aria-sort": active ? (state.sort.dir > 0 ? "ascending" : "descending") : null },
        el("button", { type: "button", text: `${c.label}${active ? (state.sort.dir > 0 ? " ▲" : " ▼") : ""}`, onclick: () => {
          state.sort = { key: c.key, dir: active ? -state.sort.dir : (c.num || c.key === "date" ? -1 : 1) };
          renderLog();
        } }));
    }));
    const body = el("tbody", {}, list.slice(0, limit).map((w) => el("tr", {
      class: w.id === state.selected ? "selected" : null, tabindex: 0,
      onclick: () => selectWalk(w.id, true),
      onkeydown: (e) => { if (e.key === "Enter") selectWalk(w.id, true); },
    }, COLUMNS.map((c) => el("td", { class: c.num ? "num" : c.cls || null, text: c.show(w) })))));
    table.replaceChildren(el("thead", {}, head), body);
    const more = $("log-more");
    more.hidden = list.length <= 25;
    more.textContent = state.showAll ? "Show fewer" : `Show all ${fmtNum(list.length)}`;
    more.onclick = () => { state.showAll = !state.showAll; renderLog(); };
  }

  // ---------- theme ----------
  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("gw-theme"); } catch (e) { /* storage unavailable */ }
    if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
    const btn = $("theme-toggle");
    const isDark = () => document.documentElement.dataset.theme === "dark" ||
      (document.documentElement.dataset.theme !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
    const label = () => { btn.textContent = isDark() ? "Light mode" : "Dark mode"; };
    label();
    btn.addEventListener("click", () => {
      const next = isDark() ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("gw-theme", next); } catch (e) { /* ignore */ }
      label();
      renderAll(false);
    });
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { label(); renderAll(false); });
  }

  // ---------- collapsible sections ----------
  // Routes, Miles by year and Miles by month are always open. Every other card can be collapsed
  // from its heading and starts collapsed; each viewer's choices are remembered in this browser.
  const ALWAYS_OPEN = new Set(["card-map", "card-years", "card-months"]);
  let collapsedPrefs = {};
  try { collapsedPrefs = JSON.parse(localStorage.getItem("gw-collapsed") || "{}") || {}; } catch (e) { /* storage unavailable */ }
  const cards = () => [...document.querySelectorAll("main section.card[id]")].filter((c) => !ALWAYS_OPEN.has(c.id));

  function rerenderCharts() {
    renderYears(); renderMonths(); renderPace(); renderAge(); renderRegions();
    renderCalendar(); renderExplore(); renderHours(); renderWeekdays();
  }

  function setCollapsed(card, collapsed, { save = true, redraw = true } = {}) {
    const was = card.classList.contains("collapsed");
    card.classList.toggle("collapsed", collapsed);
    const btn = card.querySelector(":scope > .card-head .collapse-toggle");
    if (btn) {
      btn.setAttribute("aria-expanded", String(!collapsed));
      btn.setAttribute("aria-label", `${collapsed ? "Expand" : "Collapse"} ${card.querySelector("h2")?.textContent.trim() || "section"}`);
    }
    if (save) {
      collapsedPrefs[card.id] = collapsed;
      try { localStorage.setItem("gw-collapsed", JSON.stringify(collapsedPrefs)); } catch (e) { /* ignore */ }
    }
    // Charts measured while hidden need redrawing at their real width.
    if (redraw && was && !collapsed) rerenderCharts();
  }

  /** Add the toggle to a card's heading (render functions rebuild headings, so this runs again after each). */
  function decorateCard(card) {
    const h2 = card.querySelector(":scope > .card-head h2");
    if (!h2 || h2.querySelector(".collapse-toggle")) return;
    const btn = el("button", { type: "button", class: "collapse-toggle", "aria-controls": card.id });
    btn.addEventListener("click", (e) => { e.stopPropagation(); setCollapsed(card, !card.classList.contains("collapsed")); });
    h2.prepend(btn);
    h2.classList.add("collapsible");
    h2.addEventListener("click", (e) => { if (!e.target.closest("a, button")) btn.click(); });
    setCollapsed(card, card.classList.contains("collapsed"), { save: false, redraw: false });
  }

  function initCollapsible() {
    for (const card of cards()) {
      card.classList.toggle("collapsed", collapsedPrefs[card.id] ?? true);
      decorateCard(card);
      new MutationObserver(() => decorateCard(card)).observe(card, { childList: true });
    }
    $("expand-all").addEventListener("click", () => {
      cards().forEach((c) => setCollapsed(c, false, { redraw: false }));
      rerenderCharts();
    });
    $("collapse-all").addEventListener("click", () => cards().forEach((c) => setCollapsed(c, true, { redraw: false })));
  }

  /** Everything back to how a first visit sees it: filters, map extent, sorting, sections, address. */
  function resetView() {
    Object.assign(state, defaultState());
    mapView = "home";
    collapsedPrefs = {};
    try { localStorage.removeItem("gw-collapsed"); } catch (e) { /* ignore */ }
    cards().forEach((c) => setCollapsed(c, true, { save: false, redraw: false }));
    hideTip();
    writeHash();
    if (map) map.invalidateSize();
    renderAll();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ---------- render ----------
  function renderAll(fit = true) {
    renderFilters();
    renderSummary();
    renderYears();
    renderMonths();
    renderPace();
    renderAge();
    renderChapters();
    renderRegions();
    renderCalendar();
    renderPlaces();
    renderPlaceYears();
    renderExplore();
    renderHours();
    renderWeekdays();
    renderRecords();
    renderMyMaps();
    renderMap(fit);
    renderDetail();
    renderLog();
  }

  if (!DATA || !DATA.workouts) {
    document.querySelector("main").replaceChildren(el("p", { class: "empty-note", text: "No data found. Run python gonewalking.py process to generate docs/data/." }));
    return;
  }
  if (CONFIG.siteTitle) {
    $("site-title").textContent = CONFIG.siteTitle;
    document.title = CONFIG.siteTitle;
  }
  if (CONFIG.ownerName) {
    // The title already says whose walks these are; show the full name only if it doesn't.
    const first = CONFIG.ownerName.split(" ")[0];
    if (!(CONFIG.siteTitle || "").includes(first)) $("byline").textContent = `Walks by ${CONFIG.ownerName}`;
    $("portrait").alt = `Photo of ${CONFIG.ownerName}`;
  }
  if (CONFIG.season && CONFIG.season !== "auto") {
    if (CONFIG.season === "none") delete document.documentElement.dataset.season;
    else document.documentElement.dataset.season = CONFIG.season;
  }
  initTheme();
  initCollapsible();
  $("reset-view").addEventListener("click", resetView);
  initMap();
  renderAll();
  function readHash() {
    const q = new URLSearchParams(location.hash.slice(1));
    const place = q.get("place") || "all";
    const walk = workouts.find((w) => w.id === q.get("walk"));
    if (place !== state.place) {
      state.place = place;
      state.selected = null;
      renderAll();
    }
    if (walk) {
      if (state.activity !== "all" && walk.activity !== state.activity) { state.activity = "all"; renderAll(); }
      if (walk.id !== state.selected) selectWalk(walk.id, false);
    } else if (state.selected != null) {
      state.selected = null;
      renderMap(true); renderDetail(); renderLog();
    }
  }
  window.addEventListener("popstate", readHash);
  readHash();
  $("updated").textContent = `Data updated ${fmtDate(new Date(DATA.meta.updated), { month: "long", day: "numeric", year: "numeric" })}.`;

  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(rerenderCharts, 150);
  });
})();
