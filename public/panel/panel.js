/* PRAMAN merchant panel — vanilla JS.
   The feed reads like a financial statement: human sentence first, machine
   detail second, money right-aligned in tabular figures. */

const API = "";
const REFRESH_MS = 5000;

let key = sessionStorage.getItem("merchant-key") || "";
let timer = null;

const $ = (id) => document.getElementById(id);
const money = (n) =>
  n === null || n === undefined ? "—" : "₹" + Number(n).toLocaleString("en-IN");

function toast(msg, isErr = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 3800);
}

async function call(path, options = {}) {
  const res = await fetch(API + path, {
    ...options,
    headers: { "X-Merchant-Key": key, ...(options.headers || {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body.detail || {};
    throw new Error(d.message || d.code || `HTTP ${res.status}`);
  }
  return body;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function timeOf(ts) {
  // ledger ts looks like 2026-08-26T14:41:29.123Z — show HH:MM:SS
  const m = String(ts || "").match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : "";
}

/* ---------- feed: the human sentence is the headline ---------- */

const TONE = {
  good: ["payment.captured", "approval.granted", "catalog.synced",
         "offer.issued", "ledger.genesis"],
  bad:  ["offer.refused", "checkout.rejected", "payment.failed",
         "payment.declined", "fulfillment.check", "saga.compensation_triggered",
         "razorpay.refund", "policy.selfheal", "webhook.rejected",
         "learning.record_failed"],
  warn: ["order.held_for_approval", "payment.intent", "payment.shadow_skipped"],
};

function toneOf(event) {
  if (TONE.good.includes(event)) return "good";
  if (TONE.bad.includes(event)) return "bad";
  if (TONE.warn.includes(event)) return "warn";
  return "";
}

function headlineOf(entry) {
  const p = entry.payload || {};
  switch (entry.event) {
    case "payment.captured":
      return `Payment captured — ${money(entry.money_delta_inr)}`;
    case "payment.declined":
      return "Payment declined by the gateway";
    case "payment.failed":
      return "Payment attempt failed";
    case "payment.intent":
      return `Charge intent: ${money(Math.abs(entry.money_delta_inr))} on ${p.order_id || "order"}`;
    case "payment.shadow_skipped":
      return `Shadow: would have charged ${money(Math.abs(entry.money_delta_inr || 0))}`;
    case "razorpay.order.created":
      return "Gateway order created";
    case "razorpay.refund":
      return `Refund issued — ${money(Math.abs(entry.money_delta_inr))} (${p.reason_code || "refund"})`;
    case "saga.compensation_triggered":
      return "Oversell detected — compensation started";
    case "fulfillment.check":
      return "Fulfilment check: stock shortfall";
    case "policy.selfheal":
      return `SKU disabled automatically (${p.sku || ""})`;
    case "order.held_for_approval":
      return `Held for your approval — ${money(p.amount_inr ?? 0)}`;
    case "approval.granted":
      return `Approved — released ${money(p.amount_inr ?? 0)}`;
    case "offer.request":
      return "Buyer agent asked for an offer";
    case "offer.evaluated":
      return "Offer evaluated against the bounds";
    case "offer.issued":
      return "Offer issued to the buyer";
    case "offer.refused":
      return "Offer refused by policy";
    case "proposal.emitted":
      return `Agent proposed ${(p.upsells ?? 0)} upsell(s)`;
    case "checkout.rejected":
      return "Checkout refused";
    case "catalog.query":
      return "Buyer browsed the catalog";
    case "catalog.synced":
      return `Shopify sync — ${p.imported ?? 0} imported`;
    case "checkout.abandoned":
      return "Checkout abandoned — reservations released";
    case "ledger.compensate":
      return "Order compensated — capture linked to refund";
    case "notify.buyer":
      return "Buyer notified of the failure + remedy";
    case "notify.merchant":
      return "Merchant notification sent";
    default:
      return entry.reason
        ? entry.reason.split(/[.;—]/)[0].trim().slice(0, 90)
        : entry.event;
  }
}

function renderFeed(feed) {
  const items = feed.map((e) => {
    const tone = toneOf(e.event);
    const shadow = e.policy_mode === "shadow";
    const dot = `fdot ${tone}${shadow ? " shadow" : ""}`;
    const deltaCls =
      e.money_delta_inr > 0 ? "money-pos" : e.money_delta_inr < 0 ? "money-neg" : "zero";
    const delta = e.money_delta_inr !== 0
      ? `${e.money_delta_inr > 0 ? "+" : "−"}${money(Math.abs(e.money_delta_inr))}`
      : "·";
    return `<li>
      <span class="${dot}"></span>
      <div class="fbody">
        <div class="fline1">${esc(headlineOf(e))}</div>
        <div class="fline2">
          <span>${timeOf(e.ts)}</span>
          <span class="ev">${esc(e.event)}</span>
          ${shadow ? '<span class="shadow-tag">SHADOW</span>' : ""}
        </div>
      </div>
      <span class="fmoney ${deltaCls}">${delta}</span>
    </li>`;
  });
  $("panel-feed").innerHTML = `<h2>Live ledger feed</h2><ul class="feed">${items.join("")}</ul>`;
}

/* ---------- other panels ---------- */

function renderBanner(mode) {
  const b = $("banner");
  b.textContent = mode.banner;
  b.className = "banner " + mode.value;
  b.classList.remove("hidden");
}

function renderMetrics(m) {
  const budgetPct = m.discount_budget_inr
    ? Math.min(100, (m.discount_spent_inr / m.discount_budget_inr) * 100) : 0;
  const margin = m.margin_per_rupee_discounted;
  const marginV = margin === null
    ? '<span class="dim">—</span>'
    : `<span class="cur">₹</span>${margin}`;

  $("panel-metrics").innerHTML = `<h2>Today</h2>
    <div class="metric-hero">
      <div class="label">Revenue</div>
      <div class="value num"><span class="cur">₹</span>${Number(m.revenue_inr).toLocaleString("en-IN")}</div>
    </div>
    <div class="metric-row"><span class="k">Orders</span><span class="v">${m.orders}</span></div>
    <div class="metric-row"><span class="k">Average order value</span><span class="v num">${money(m.aov_inr)}</span></div>
    <div class="metric-row"><span class="k">Upsell revenue</span><span class="v num">${money(m.upsell_revenue_inr)}</span></div>
    <div class="metric-row"><span class="k">Margin per ₹1 discounted</span><span class="v num">${marginV}</span></div>
    <div class="metric-row"><span class="k">Discount spent</span>
      <span class="v num dim">${money(m.discount_spent_inr)} / ${money(m.discount_budget_inr)}</span></div>
    <div class="bar-wrap"><div class="bar-fill" style="width:${budgetPct}%"></div></div>
    <div class="metric-row"><span class="k">Refunded today</span>
      <span class="v num" ${m.refunded_orders ? 'style="color:var(--red)"' : 'class="dim"'}>${m.refunded_orders}</span></div>`;
}

function renderApprovals(a) {
  if (!a.queue.length) {
    $("panel-approvals").innerHTML = `<h2>Needs your approval (${a.pending_count})</h2>
      <div class="muted small" style="line-height:1.6">Nothing held. Nothing auto-approves, ever —
      this queue only grows when a person must decide.</div>`;
    return;
  }
  const rows = a.queue.map((ap) => `
    <div class="approval">
      <div class="head">
        <span class="amount num">${money(ap.amount_inr)}</span>
        <span class="oid">${esc(ap.order_id || "")}</span>
      </div>
      <div class="note">${esc(ap.note || "")}</div>
      <div class="row">
        <button class="pill approve" onclick="decide('${ap.approval_id}','approve')">Approve</button>
        <button class="pill reject" onclick="decide('${ap.approval_id}','reject')">Reject</button>
        <input class="counter-input num" id="cnt-${ap.approval_id}" placeholder="Counter ₹" />
        <button class="pill dark" onclick="doCounter('${ap.approval_id}')">Counter →</button>
      </div>
    </div>`).join("");
  $("panel-approvals").innerHTML =
    `<h2>Needs your approval (${a.pending_count})</h2>${rows}`;
}

function renderBounds(bounds) {
  const rows = bounds.map((b) =>
    `<div class="bound ${b.fired_recently ? "fired" : ""}" title="${b.id}">
       <span class="num">${b.bound}</span>
       <span class="rule">${b.rule}</span>
       ${b.fired_recently ? '<span class="fired-mark">FIRED</span>' : ""}
     </div>`).join("");
  $("panel-bounds").innerHTML = `<h2>Active bounds</h2><div class="bounds">${rows}</div>`;
}

function renderSafety(s) {
  const rowsDef = [
    ["LLM proposals refused", s.llm_proposals_refused],
    ["Checkout rejections", s.checkouts_rejected],
    ["Tier-2 human holds", s.tier2_holds],
    ["Oversells auto-compensated", s.oversells_compensated],
    ["Payments declined", s.payments_declined],
    ["Double charges, ever", s.double_charges],
  ];
  const rows = rowsDef.map(([label, n]) =>
    `<div class="safety-row ${n === 0 ? "zero" : ""}">
       <span class="k">${label}</span><span class="n">${n}</span>
     </div>`).join("");
  const fired = s.bounds_fired.length
    ? s.bounds_fired.map((b) => `<b>#${b.bound}</b>×${b.count}`).join(", ")
    : "none recently";
  $("panel-safety").innerHTML = `<h2>The cage at work</h2>${rows}
    <div class="safety-foot">Bound firings lately: ${fired}<br/>${esc(s.note)}</div>`;
}

function renderChain(chain) {
  const state = chain.intact
    ? `<span class="chain-state chain-ok"><span class="pulse-dot"></span>Chain intact · head #${chain.head_seq}</span>`
    : `<span class="chain-state chain-bad"><span class="pulse-dot"></span>Chain broken at #${chain.broken_at}</span>`;
  $("panel-chain").innerHTML = `<h3>Proof chain</h3>
    <div class="chainbar" style="margin-bottom:0">
      ${state}
      <span class="muted">${esc(chain.note)}</span>
      <span class="spacer"></span>
      <span>verify <code>/audit/verify</code> · anchor <code>python -m scripts.anchor_chain</code></span>
    </div>`;
}

/* ---------- hero step switcher (editorial touch) ---------- */

const HERO_STEPS = [
  {
    title: "How PRAMAN<br/>works today",
    sub: "An AI salesman proposes. Deterministic code disposes. Every rupee below is bounded, gated, and provable.",
  },
  {
    title: "The cage is<br/>load-bearing",
    sub: "Ten bounds veto every proposal. The counters below show the refusals, holds and auto-refunds — zeros stated as zeros.",
  },
  {
    title: "Proof, not<br/>promises",
    sub: "Every step — including the agent's exploration — lands on a hash-chained ledger anyone can verify.",
  },
];

document.querySelectorAll(".step").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".step").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const step = HERO_STEPS[Number(btn.dataset.step) || 0];
    $("hero-title").innerHTML = step.title;
    $("hero-sub").textContent = step.sub;
  });
});

function renderBannerError() { /* gate handles it */ }

/* ---------- actions ---------- */

window.decide = async function (id, action) {
  try {
    await call(`/merchant/v1/approvals/${id}/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    toast(`${action} recorded on the ledger`);
    refresh();
  } catch (e) { toast(e.message, true); }
};

window.doCounter = async function (id) {
  const amount = parseInt(document.getElementById("cnt-" + id).value, 10);
  if (!amount || amount <= 0) { toast("enter a counter amount first", true); return; }
  try {
    await call(`/merchant/v1/approvals/${id}/counter`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ counter_amount_inr: amount }),
    });
    toast("counter-offer issued — the buyer agent can accept it now");
    refresh();
  } catch (e) { toast(e.message, true); }
};

$("btn-sync").onclick = async () => {
  toast("syncing Shopify catalog…");
  try {
    const r = await call("/merchant/v1/shopify/sync", { method: "POST" });
    toast(`Shopify sync: ${r.imported} imported, ${r.skipped} skipped`);
    refresh();
  } catch (e) { toast(e.message, true); }
};

$("btn-anchor").onclick = () =>
  toast("terminal: python -m scripts.anchor_chain --verify");

$("btn-oversell").onclick = async () => {
  toast("firing oversell rehearsal…");
  try {
    const res = await fetch(API + "/demo/force_oversell", {
      method: "POST",
      headers: { "X-Demo-Key": key, "Content-Type": "application/json" },
      body: "{}",
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail?.message || `HTTP ${res.status}`);
    toast(`${body.code}: refunded ₹${body.refund?.amount_inr} · ${body.order_id}`);
    refresh();
  } catch (e) { toast(e.message, true); }
};

/* ---------- loop + gate ---------- */

async function refresh() {
  try {
    const data = await call("/merchant/v1/dashboard");
    renderBanner(data.mode);
    renderMetrics(data.metrics);
    renderApprovals(data.approvals);
    renderFeed(data.feed);
    renderBounds(data.bounds);
    renderSafety(data.safety);
    renderChain(data.chain);
  } catch (e) {
    if (/key/i.test(String(e.message))) logout();
    else toast(e.message, true);
  }
}

function enter() {
  $("gate").classList.add("hidden");
  $("app").classList.remove("hidden");
  $("banner").classList.remove("hidden");
  refresh();
  timer = setInterval(() => {
    if (document.hidden) return;
    refresh();
  }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
}

function logout() {
  clearInterval(timer);
  sessionStorage.removeItem("merchant-key");
  key = "";
  $("app").classList.add("hidden");
  $("banner").classList.add("hidden");
  $("gate").classList.remove("hidden");
  $("gate-error").textContent = "";
}

$("key-go").onclick = async () => {
  key = $("key-input").value.trim();
  try {
    await call("/merchant/v1/dashboard");
    sessionStorage.setItem("merchant-key", key);
    enter();
  } catch (e) { $("gate-error").textContent = e.message; }
};
$("key-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("key-go").click();
});

if (key) call("/merchant/v1/dashboard").then(enter).catch(() => logout());
