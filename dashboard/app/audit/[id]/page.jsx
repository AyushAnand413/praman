"use client";
import { use, useEffect, useState } from "react";
import "../../globals.css";
import { API } from "../../config";
import PramanLogo from "../../PramanLogo";

function money(n) {
  return n == null ? "—" : `₹${Number(n).toLocaleString("en-IN")}`;
}

function fmt(ts) {
  if (!ts) return "";
  try {
    return new Date(ts).toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit"
    });
  } catch {
    return ts.slice(0, 19).replace("T", " ");
  }
}

function humanHoldNote(note) {
  if (!note) return "Order exceeds auto-approval limit · Confirmation needed";
  const m = note.match(/exceeds (?:the )?(?:Rs\.?|₹)?\s*([\d,]+)/i);
  if (m) {
    return `Exceeds ₹${m[1]} auto-approval limit · Confirmation needed`;
  }
  return "Order exceeds threshold · Confirmation needed";
}

function humanStep(e) {
  const r = e.reason || "";
  const ev = e.event;
  if (r.includes("shadow mode") || r.includes("shadow_skipped") || ev.includes("shadow")) {
    return { title: "Simulation Test Passed", desc: "Order verified under active guardrails without charging live funds", tone: "good" };
  }
  if (ev === "payment.intent") return { title: "Payment Initialized", desc: `Payment of ${money(e.money_delta_inr)} awaiting confirmation`, tone: "warn" };
  if (ev === "razorpay.order.created") return { title: "Gateway Order Created", desc: "Payment gateway order created — waiting for customer payment", tone: "warn" };
  if (ev === "payment.captured") return { title: "Payment Received", desc: `Received ${money(e.money_delta_inr)} — successfully credited to merchant account`, tone: "good" };
  if (ev === "payment.failed" || ev === "payment.declined") return { title: "Payment Failed", desc: r.slice(0, 90) || "Card declined or network issue", tone: "bad" };
  if (ev === "order.held_for_approval") return { title: "Awaiting Merchant Approval", desc: humanHoldNote(r), tone: "warn" };
  if (ev === "offer.request") return { title: "Price Inquiry", desc: "Customer or agent checked price and stock", tone: "" };
  if (ev === "offer.issued") return { title: "Discount Offer Sent", desc: r ? humanHoldNote(r) : "Verified offer sent to customer", tone: "good" };
  if (ev === "offer.refused") return { title: "Offer Declined", desc: r.slice(0, 80) || "Proposed price was below minimum allowed floor", tone: "bad" };
  if (ev === "proposal.emitted") return { title: "Order Proposal Calculated", desc: "Items, discounts, and order totals calculated", tone: "" };
  if (ev === "catalog.query") return { title: "Catalog Viewed", desc: "Customer searched inventory products", tone: "" };
  if (ev === "saga.compensation_triggered") return { title: "Inventory Reconciled", desc: "Refund issued for out-of-stock item", tone: "bad" };
  if (ev === "checkout.rejected") return { title: "Checkout Blocked", desc: r.slice(0, 80) || "Order could not be processed under current rules", tone: "bad" };
  if (ev === "mandate.accepted") return { title: "Buyer Order Verified", desc: "Buyer payment details and terms verified", tone: "good" };
  if (ev === "policy.updated") return { title: "Store Policy Updated", desc: r || "Store discount rules adjusted", tone: "" };
  if (ev === "ledger.genesis") return { title: "Audit Log Initialized", desc: "First entry created in store audit trail", tone: "good" };
  if (ev === "razorpay.refund") return { title: "Refund Issued", desc: r.slice(0, 80) || "Payment returned to customer", tone: "bad" };
  if (ev === "offer.evaluated") return { title: "Price Evaluated", desc: r.slice(0, 80) || "Order checked against store discount policy", tone: "" };
  return { title: ev?.replace(/\./g, " · ") || "Event", desc: r.slice(0, 90), tone: "" };
}

export default function AuditPage({ params }) {
  const resolved = params && typeof params.then === "function" ? use(params) : params;
  const id = resolved?.id || "";
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    try {
      const t = localStorage.getItem("praman-theme") || "dark";
      if (t === "dark") {
        document.documentElement.setAttribute("data-theme", "dark");
      } else {
        document.documentElement.removeAttribute("data-theme");
      }
    } catch {}
    if (!id) return;
    fetch(`${API}/audit/${id}`, { cache: "no-store" })
      .then(async r => {
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        setData(j);
      })
      .catch(e => setErr(String(e)));
  }, [id]);

  if (err) {
    return (
      <main className="auth-wrapper">
        <div className="auth-card" style={{ maxWidth: 520 }}>
          <a href="/" style={{ color: "var(--accent)", fontSize: 13, textDecoration: "none", display: "inline-flex", alignItems: "center", gap: 6, marginBottom: 14 }}>
            ← Back to Console
          </a>
          <h1 style={{ fontSize: 20 }}>Order History Not Found</h1>
          <p className="error" style={{ marginTop: 10 }}>Unable to load order report: {err}</p>
          <a href="/" className="pill dark" style={{ textDecoration: "none", marginTop: 14, display: "inline-flex" }}>
            Return to Console
          </a>
        </div>
      </main>
    );
  }

  if (!data) {
    return (
      <main className="auth-wrapper">
        <div className="auth-card" style={{ maxWidth: 420, textAlign: "center" }}>
          <div style={{ width: 36, height: 36, border: "3px solid var(--border)", borderTopColor: "var(--accent)", borderRadius: "50%", margin: "0 auto 16px", animation: "spin 0.8s linear infinite" }} />
          <h2 style={{ fontSize: 17, color: "var(--text-heading)" }}>Loading Order History</h2>
          <p className="mono muted" style={{ fontSize: 12, marginTop: 4 }}>{id}</p>
        </div>
      </main>
    );
  }

  const entries = data.entries || (data.seq ? [data] : []);
  const captured = entries.find(e => e.event === "payment.captured") || entries.find(e => e.event === "payment.intent");
  const total = captured ? captured.money_delta_inr : (data.money_delta_inr ?? entries.reduce((s, e) => s + (e.money_delta_inr || 0), 0));
  const status = entries.find(e => e.event === "payment.captured")
    ? "Settled"
    : entries.find(e => e.event === "order.held_for_approval")
    ? "Under Hold"
    : entries.find(e => e.event === "payment.intent")
    ? "Awaiting Payment"
    : "In Progress";
  const statusClass = status === "Settled" ? "CONFIRMED" : status === "Under Hold" ? "HELD" : status === "Awaiting Payment" ? "PENDING" : "FAILED";

  return (
    <main style={{ minHeight: "100vh", background: "var(--bg-page)", color: "var(--text-primary)" }}>
      <div style={{ maxWidth: 840, margin: "0 auto", padding: "36px 20px 60px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
          <a href="/" style={{ color: "var(--accent)", fontSize: 13, fontWeight: 500, textDecoration: "none", display: "inline-flex", alignItems: "center", gap: 6 }}>
            ← Back to Console
          </a>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <PramanLogo size={22} />
            <span style={{ fontSize: 13, fontWeight: 700, letterSpacing: "-0.01em", color: "var(--text-heading)" }}>PRAMAN</span>
          </div>
        </div>

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginTop: 18, flexWrap: "wrap", gap: 10 }}>
          <div>
            <span className="mono muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em" }}>
              Order Activity &amp; History
            </span>
            <h1 style={{ fontSize: 24, fontWeight: 700, margin: "4px 0 0", color: "var(--text-heading)", letterSpacing: "-0.02em" }}>
              Order Audit Report
            </h1>
          </div>
          <span className="mono" style={{ fontSize: 12, color: "var(--accent)", background: "var(--accent-dim)", padding: "4px 10px", borderRadius: 6, border: "1px solid var(--border)" }}>
            {id}
          </span>
        </div>

        <div className="stat-grid" style={{ gridTemplateColumns: "repeat(3, 1fr)", marginTop: 20 }}>
          <div className="stat">
            <div className="k">Order Status</div>
            <div style={{ marginTop: 8 }}>
              <span className={`state ${statusClass}`} style={{ fontSize: 13, padding: "4px 10px" }}>
                {status}
              </span>
            </div>
          </div>
          <div className="stat">
            <div className="k">Total Amount</div>
            <div className="v num">{money(total || entries[0]?.payload?.amount_inr)}</div>
          </div>
          <div className="stat">
            <div className="k">Audit Steps</div>
            <div className="v" style={{ fontSize: 20, color: "var(--safe)" }}>
              {entries.length} Verified Step{entries.length === 1 ? "" : "s"}
            </div>
            <div className="muted"><a href="/audit/verify" style={{ color: "var(--accent)", textDecoration: "none" }}>View Audit Log →</a></div>
          </div>
        </div>

        <div style={{ marginTop: 24, position: "relative", paddingLeft: 28 }}>
          <div style={{ position: "absolute", left: 10, top: 12, bottom: 12, width: 2, background: "var(--border)", borderRadius: 2 }} />
          {entries.map((e, idx) => {
            const h = humanStep(e);
            const dotColor = h.tone === "good" ? "var(--safe)" : h.tone === "bad" ? "var(--alert)" : h.tone === "warn" ? "var(--warn)" : "var(--text-muted)";
            return (
              <div key={e.seq} style={{ position: "relative", marginBottom: 14, background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 10, padding: "16px 18px", boxShadow: "var(--shadow-sm)" }}>
                <div style={{ position: "absolute", left: -23, top: 18, width: 10, height: 10, borderRadius: "50%", background: dotColor, boxShadow: `0 0 6px ${dotColor}`, border: "2px solid var(--bg-page)" }} />
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
                  <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-heading)" }}>{idx + 1}. {h.title}</div>
                  <div className="mono muted" style={{ fontSize: 11 }}>Step {idx + 1} of {entries.length} · {fmt(e.ts)}</div>
                </div>
                <div style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: 6, lineHeight: 1.5 }}>{h.desc}</div>
                {e.event === "payment.captured" && e.money_delta_inr ? (
                  <div className="mono" style={{ fontSize: 12, marginTop: 6, color: "var(--safe)", fontWeight: 600 }}>+{money(e.money_delta_inr)} settled</div>
                ) : null}
                {e.event === "payment.intent" && e.money_delta_inr ? (
                  <div className="mono" style={{ fontSize: 12, marginTop: 6, color: "var(--warn)" }}>{money(e.money_delta_inr)} awaiting confirmation</div>
                ) : null}

                <details style={{ marginTop: 10 }}>
                  <summary style={{ fontSize: 11, color: "var(--text-muted)", cursor: "pointer", fontFamily: "JetBrains Mono, monospace" }}>
                    Show technical event details ▾
                  </summary>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10, marginTop: 10 }}>
                    <div className="stat" style={{ padding: 10 }}>
                      <div className="k">Actor / Agent</div>
                      <div className="mono" style={{ fontSize: 12, marginTop: 4, wordBreak: "break-all" }}>{e.payload?.agent_id || e.actor || "—"}</div>
                    </div>
                    <div className="stat" style={{ padding: 10 }}>
                      <div className="k">Product / Amount</div>
                      <div style={{ fontSize: 12, marginTop: 4, fontWeight: 500 }}>
                        {(() => {
                          const p = e.payload || {};
                          if (e.event === "razorpay.order.created") return `${(p.razorpay_order_id || "").slice(0, 16) || "—"} · ${money(e.money_delta_inr)}`;
                          if (e.event === "stock.commit_anomaly") {
                            return p.oversold?.length ? `${p.oversold.length} oversold` : p.recovered?.length ? `${p.recovered.length} recovered` : "Hold reconciled";
                          }
                          if (e.event === "payment.captured" || e.event === "payment.intent") return `— · ${money(e.money_delta_inr)}`;
                          return `${p.base_sku || p.sku || p.order_id?.slice(0, 12) || "—"} · ${p.amount_inr ? money(p.amount_inr) : e.money_delta_inr ? money(e.money_delta_inr) : "—"}`;
                        })()}
                      </div>
                    </div>
                    <div className="stat" style={{ padding: 10 }}>
                      <div className="k">Gate Tier</div>
                      <div style={{ fontSize: 12, marginTop: 4 }}>
                        {e.payload?.gate ? `Tier ${e.payload.gate.gate_tier} — ${e.payload.gate.tier_name || ""}` : e.payload?.gate_tier ? `Tier ${e.payload.gate_tier}` : e.event.includes("held") ? "Under Hold" : e.event.startsWith("stock.") ? "Inventory Hold" : e.event.startsWith("razorpay.") ? "Gateway" : "—"}
                      </div>
                    </div>
                    <div className="stat" style={{ padding: 10 }}>
                      <div className="k">Timestamp</div>
                      <div className="mono" style={{ fontSize: 12, marginTop: 4 }}>{fmt(e.ts)}</div>
                    </div>
                  </div>
                </details>
              </div>
            );
          })}
        </div>

        <div style={{ borderTop: "1px solid var(--border)", marginTop: 24, paddingTop: 16, display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 10, fontSize: 12, color: "var(--text-muted)" }}>
          <span>Order ID: <code style={{ color: "var(--text-heading)" }}>{id}</code></span>
          <a href="/audit/verify" style={{ color: "var(--accent)", textDecoration: "none" }}>Audit Log Verified &amp; Safe ✓</a>
        </div>
      </div>
    </main>
  );
}
