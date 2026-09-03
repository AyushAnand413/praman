"use client";
import { useEffect, useState } from "react";
import "../../globals.css";
import { API } from "../../config";
import PramanLogo from "../../PramanLogo";

function IconCheck({ size = 15 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20 6 9 17l-5-5"/>
    </svg>
  );
}

function IconAlert({ size = 15 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/>
      <line x1="12" y1="9" x2="12" y2="13"/>
      <line x1="12" y1="17" x2="12.01" y2="17"/>
    </svg>
  );
}

export default function VerifyPage() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [checkedAt, setCheckedAt] = useState(null);

  async function load() {
    try {
      const r = await fetch(`${API}/audit/verify`, { cache: "no-store" });
      const j = await r.json();
      setData(j);
      setErr("");
      setCheckedAt(new Date());
    } catch (e) {
      setErr(String(e));
    }
  }

  useEffect(() => {
    try {
      const t = localStorage.getItem("praman-theme") || "dark";
      if (t === "dark") {
        document.documentElement.setAttribute("data-theme", "dark");
      } else {
        document.documentElement.removeAttribute("data-theme");
      }
    } catch {}
    load();
  }, []);

  if (err) {
    return (
      <main className="auth-wrapper">
        <div className="auth-card" style={{ maxWidth: 520 }}>
          <a href="/" style={{ color: "var(--accent)", fontSize: 13, textDecoration: "none", display: "inline-flex", alignItems: "center", gap: 6, marginBottom: 14 }}>
            ← Back to Console
          </a>
          <h1 style={{ fontSize: 20 }}>Audit Check Disconnected</h1>
          <p className="error" style={{ marginTop: 10 }}>Unable to reach audit service: {err}</p>
          <button className="pill dark" style={{ marginTop: 14 }} onClick={load}>Retry Check</button>
        </div>
      </main>
    );
  }

  if (!data) {
    return (
      <main className="auth-wrapper">
        <div className="auth-card" style={{ maxWidth: 420, textAlign: "center" }}>
          <div style={{ width: 36, height: 36, border: "3px solid var(--border)", borderTopColor: "var(--accent)", borderRadius: "50%", margin: "0 auto 16px", animation: "spin 0.8s linear infinite" }} />
          <h2 style={{ fontSize: 17, color: "var(--text-heading)" }}>Verifying Store Audit Log</h2>
          <p className="muted" style={{ fontSize: 13, marginTop: 4 }}>Checking all recorded orders and transactions for security...</p>
        </div>
      </main>
    );
  }

  const intact = data.intact;

  return (
    <main style={{ minHeight: "100vh", background: "var(--bg-page)", color: "var(--text-primary)" }}>
      <div style={{ maxWidth: 740, margin: "0 auto", padding: "36px 20px 60px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
          <a href="/" style={{ color: "var(--accent)", fontSize: 13, fontWeight: 500, textDecoration: "none", display: "inline-flex", alignItems: "center", gap: 6 }}>
            ← Back to Console
          </a>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <PramanLogo size={22} />
            <span style={{ fontSize: 13, fontWeight: 700, letterSpacing: "-0.01em", color: "var(--text-heading)" }}>PRAMAN</span>
          </div>
        </div>

        <div style={{ marginTop: 18, marginBottom: 22 }}>
          <div style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "4px 12px", borderRadius: 999, background: intact ? "var(--safe-bg)" : "var(--alert-bg)", border: `1px solid ${intact ? "var(--safe-border)" : "var(--alert-border)"}`, color: intact ? "var(--safe)" : "var(--alert)", fontSize: 11, fontWeight: 600, letterSpacing: "0.06em", textTransform: "uppercase", marginBottom: 12 }}>
            {intact ? <IconCheck size={13} /> : <IconAlert size={13} />}
            {intact ? "Audit Log Verified & Safe" : "Issue Detected"}
          </div>
          <h1 style={{ fontSize: 24, fontWeight: 700, margin: 0, letterSpacing: "-0.02em", color: "var(--text-heading)" }}>
            Store Audit &amp; Security Verification
          </h1>
          <p style={{ color: "var(--text-secondary)", fontSize: 14, lineHeight: 1.6, margin: "8px 0 0" }}>
            PRAMAN automatically verifies your store's entire transaction history to confirm that all recorded orders, price calculations, and settlements remain authentic and unchanged.
          </p>
        </div>

        <div className="stat-grid" style={{ gridTemplateColumns: "repeat(3, 1fr)" }}>
          <div className="stat">
            <div className="k">Audit Log Status</div>
            <div className="v" style={{ color: intact ? "var(--safe)" : "var(--alert)", fontSize: 20 }}>
              {intact ? "Healthy ✓" : "Review Needed"}
            </div>
            <div className="muted">{intact ? "All records authentic" : "Mismatch found"}</div>
          </div>
          <div className="stat">
            <div className="k">Total Records Checked</div>
            <div className="v mono" style={{ fontSize: 20 }}>#{data.head_seq ?? "—"}</div>
            <div className="muted">{data.head_seq ? `${data.head_seq} verified events` : "No entries"}</div>
          </div>
          <div className="stat">
            <div className="k">Last Verified</div>
            <div className="v mono" style={{ fontSize: 16 }}>
              {checkedAt ? checkedAt.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—"}
            </div>
            <div className="muted">{checkedAt ? checkedAt.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }) : ""}</div>
          </div>
        </div>

        <div style={{ background: intact ? "var(--safe-bg)" : "var(--alert-bg)", border: `1px solid ${intact ? "var(--safe-border)" : "var(--alert-border)"}`, borderRadius: 10, padding: 18, marginTop: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: intact ? "var(--safe)" : "var(--alert)", display: "flex", alignItems: "center", gap: 8 }}>
            {intact ? <IconCheck size={16} /> : <IconAlert size={16} />}
            {intact ? `All ${data.head_seq ?? "?"} order events are verified and secured.` : `Discrepancy detected at entry #${data.broken_at}`}
          </div>
          <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 6, lineHeight: 1.5 }}>
            Tamper-proof protection: Every purchase, price change, and payment event is permanently recorded to safeguard against fraud or unauthorized changes.
          </div>
        </div>

        <div style={{ display: "flex", gap: 10, marginTop: 20 }}>
          <button onClick={load} className="pill primary" style={{ fontWeight: 600 }}>Run Security Check</button>
          <a href="/" className="pill dark" style={{ textDecoration: "none" }}>Back to Console</a>
        </div>

        <details style={{ marginTop: 24 }}>
          <summary style={{ cursor: "pointer", fontSize: 12, color: "var(--text-muted)", fontFamily: "JetBrains Mono, monospace" }}>
            Show technical verification details ▾
          </summary>
          <pre style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 8, padding: 14, overflow: "auto", fontSize: 12, marginTop: 10, color: "var(--text-secondary)", fontFamily: "JetBrains Mono, monospace" }}>
            {JSON.stringify(data, null, 2)}
          </pre>
        </details>
      </div>
    </main>
  );
}
