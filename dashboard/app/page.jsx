"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import "./globals.css";
import { API, API_MISCONFIGURED, API_MISCONFIGURED_MESSAGE } from "./config";
import PramanLogo from "./PramanLogo";

function money(n) {
  if (n === null || n === undefined) return "—";
  return `₹${new Intl.NumberFormat("en-IN").format(Number(n))}`;
}

function fmtDate(s) {
  if (!s) return "";
  try {
    return new Date(s).toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit"
    });
  } catch {
    return s.slice(0, 16);
  }
}

function fmtCategory(c) {
  if (!c) return "General";
  return c.replace(/[_-]+/g, " ").replace(/\b\w/g, l => l.toUpperCase());
}

function formatStoreName(str) {
  if (!str || str === "default") return "Primary Store";
  let clean = str.replace(/^https?:\/\//i, "").replace(/\.myshopify\.com.*$/i, "");
  clean = clean.replace(/-[a-z0-9]{6,12}$/i, "");
  return clean.replace(/[-_]+/g, " ").replace(/\b\w/g, l => l.toUpperCase());
}

/* ── Crisp Inline SVG Icons ── */
function IconStore({ size = 16, className = "" }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d="m2 7 4.41-4.41A2 2 0 0 1 7.83 2h8.34a2 2 0 0 1 1.42.59L22 7"/>
      <path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/>
      <path d="M15 22v-4a2 2 0 0 0-2-2h-2a2 2 0 0 0-2 2v4"/>
      <path d="M2 7h20"/>
    </svg>
  );
}

function IconBox({ size = 16, className = "" }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/>
      <path d="m3.3 7 8.7 5 8.7-5"/>
      <path d="M12 22V12"/>
    </svg>
  );
}

function IconSearch({ size = 14, className = "" }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <circle cx="11" cy="11" r="8"/>
      <path d="m21 21-4.3-4.3"/>
    </svg>
  );
}

function IconCheck({ size = 13, className = "" }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d="M20 6 9 17l-5-5"/>
    </svg>
  );
}

function IconShield({ size = 14, className = "" }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>
    </svg>
  );
}

function IconRefresh({ size = 13, className = "" }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/>
      <path d="M21 3v5h-5"/>
      <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/>
      <path d="M8 16H3v5"/>
    </svg>
  );
}

function IconClose({ size = 14, className = "" }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d="M18 6 6 18"/>
      <path d="m6 6 12 12"/>
    </svg>
  );
}

function AnimatedThemeToggle({ theme, setTheme }) {
  const toggle = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    try {
      localStorage.setItem("praman-theme", next);
      if (next === "dark") {
        document.documentElement.setAttribute("data-theme", "dark");
      } else {
        document.documentElement.removeAttribute("data-theme");
      }
    } catch {}
  };

  const isDark = theme === "dark";

  return (
    <button
      type="button"
      className={`theme-switch ${isDark ? "dark" : "light"}`}
      onClick={toggle}
      aria-label={`Switch to ${isDark ? "Light" : "Dark"} mode`}
      title={`Switch to ${isDark ? "Light" : "Dark"} mode`}
    >
      <span className="switch-icon sun-icon" aria-hidden="true">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
        </svg>
      </span>
      <span className="switch-icon moon-icon" aria-hidden="true">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z" />
        </svg>
      </span>
      <div className="switch-thumb" />
    </button>
  );
}

function CustomCategoryPicker({ categories = [], value, onChange, onSelect }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    function handleClickOutside(e) {
      if (ref.current && !ref.current.contains(e.target)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const currentLabel = value === "all" ? `All Categories (${categories.length})` : fmtCategory(value);

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        type="button"
        className="category-picker-trigger"
        onClick={() => setOpen(!open)}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span>{currentLabel}</span>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ transform: open ? "rotate(180deg)" : "none", transition: "transform 0.2s ease" }}>
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>

      {open ? (
        <div className="category-picker-dropdown">
          <div
            className={`category-picker-item ${value === "all" ? "selected" : ""}`}
            onClick={() => {
              onChange("all");
              setOpen(false);
              onSelect && onSelect("all");
            }}
          >
            <span>All Categories</span>
            <span className="category-item-count">{categories.length}</span>
          </div>
          <div style={{ height: 1, background: "var(--border-subtle)", margin: "4px 0" }} />
          {categories.map(c => (
            <div
              key={c}
              className={`category-picker-item ${value === c ? "selected" : ""}`}
              onClick={() => {
                onChange(c);
                setOpen(false);
                onSelect && onSelect(c);
              }}
            >
              <span>{fmtCategory(c)}</span>
              {value === c ? <IconCheck size={12} /> : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function eventCopy(e) {
  const ev = e.event;
  const amt = e.money_delta_inr;
  const r = e.reason || "";
  const pAmt = e.payload?.amount_inr;

  if (r.includes("shadow mode") || r.includes("shadow_skipped") || ev.includes("shadow")) {
    return { title: "Simulation Test Passed", desc: "Order verified under active guardrails without charging live funds", tone: "good" };
  }
  if (ev === "payment.intent") return { title: "Payment Initialized", desc: `Payment of ${money(amt)} awaiting confirmation`, tone: "warn" };
  if (ev === "payment.captured") return { title: "Payment Received", desc: `Received ${money(amt)} — settled to merchant account`, tone: "good" };
  if (ev === "payment.failed" || ev === "payment.declined") return { title: "Payment Failed", desc: r.slice(0, 90) || "Card declined or network issue", tone: "bad" };
  if (ev === "razorpay.order.created") return { title: "Gateway Order Created", desc: "Linked to payment gateway — awaiting buyer payment", tone: "warn" };
  if (ev === "order.held_for_approval") return { title: `Held for Approval${pAmt ? ` — ${money(pAmt)}` : ""}`, desc: humanHoldNote(r), tone: "warn" };
  if (ev === "offer.issued") return { title: "Offer Sent to Buyer", desc: r ? humanHoldNote(r) : "Verified offer sent to customer", tone: "good" };
  if (ev === "offer.refused") return { title: "Offer Refused", desc: r.slice(0, 80) || "Price was below minimum floor limit", tone: "bad" };
  if (ev === "offer.request") return { title: "Price Inquiry", desc: "Customer checked price and availability", tone: "" };
  if (ev === "catalog.query") return { title: "Customer Browsed Catalog", desc: "Product search performed", tone: "" };
  if (ev === "catalog.synced") return { title: "Catalog Synced", desc: r || "Products updated from store backend", tone: "good" };
  if (ev === "policy.updated") return { title: "Store Rules Updated", desc: r || "Discount rules adjusted", tone: "" };
  if (ev === "ledger.genesis") return { title: "Audit Log Started", desc: "Initial security record created", tone: "good" };
  if (ev === "mandate.accepted") return { title: "Order Verified", desc: "Buyer payment details verified", tone: "good" };
  if (ev === "checkout.rejected") return { title: "Checkout Blocked", desc: r.slice(0, 80) || "Order could not be processed under store rules", tone: "bad" };
  if (ev === "proposal.emitted") return { title: "Order Proposal Generated", desc: "Item prices and discounts calculated", tone: "" };
  if (ev === "saga.compensation_triggered") return { title: "Inventory Reconciled", desc: "Refund issued for out-of-stock item", tone: "bad" };
  if (ev === "razorpay.refund") return { title: "Refund Processed", desc: r.slice(0, 80) || "Payment returned to customer", tone: "bad" };
  if (ev === "offer.evaluated") return { title: "Offer Evaluated", desc: r.slice(0, 80) || "Order checked against store limits", tone: "" };
  if (ev === "optimizer.ranked") return { title: "Best Price Matched", desc: "Automated discount applied within store limits", tone: "good" };
  if (ev === "approval.granted") return { title: "Order Approved", desc: "Merchant approved held transaction", tone: "good" };
  if (ev === "approval.rejected") return { title: "Order Rejected", desc: "Merchant declined transaction", tone: "bad" };
  if (ev === "approval.counter_offered") return { title: "Counter Offer Sent", desc: "Counter offer dispatched to buyer", tone: "good" };
  return { title: ev?.replace(/\./g, " · ") || "Event", desc: r.slice(0, 60), tone: "" };
}

function humanEvent(e) {
  const copy = eventCopy(e);
  const serverTone = e.tone === "neutral" ? "" : e.tone;
  return { ...copy, tone: serverTone ?? copy.tone };
}

function humanHoldNote(note) {
  if (!note) return "Order exceeds auto-approval limit · Approval needed";
  const m = note.match(/exceeds (?:the )?(?:Rs\.?|₹)?\s*([\d,]+)/i);
  if (m) {
    return `Exceeds ₹${m[1]} auto-approval limit · Confirmation needed`;
  }
  return "Order exceeds threshold · Confirmation needed";
}

export default function Page() {
  const [theme, setTheme] = useState("dark");
  const [token, setToken] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [storeId, setStoreId] = useState("");
  const [stores, setStores] = useState([]);
  const [storesData, setStoresData] = useState(null);
  const [syncState, setSyncState] = useState({ busy: false, progress: "", error: "", success: "" });
  const [activeTab, setActiveTab] = useState("overview");
  const [catalogData, setCatalogData] = useState(null);
  const [catalogSearch, setCatalogSearch] = useState("");
  const [catalogCat, setCatalogCat] = useState("all");
  const [selectedProduct, setSelectedProduct] = useState(null);
  const [catalogBusy, setCatalogBusy] = useState(false);
  const [data, setData] = useState(null);
  const [orders, setOrders] = useState(null);
  const [selectedOrder, setSelectedOrder] = useState(null);
  const [drawerData, setDrawerData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState("signin");
  const [connectOpen, setConnectOpen] = useState(false);
  const [connectKind, setConnectKind] = useState("shopify");
  const [connectForm, setConnectForm] = useState({ domain: "", token: "", url: "", key: "", secret: "", csv: "", rows: null });
  const [csvFileName, setCsvFileName] = useState("");
  const [csvCount, setCsvCount] = useState(0);
  const [dragOver, setDragOver] = useState(false);
  const [showManualCsv, setShowManualCsv] = useState(false);
  const fileInputRef = useRef(null);
  const [toast, setToast] = useState("");
  const [showActivity, setShowActivity] = useState(false);
  const [policy, setPolicy] = useState(null);
  const [policyDraft, setPolicyDraft] = useState(null);
  const [authReady, setAuthReady] = useState(false);
  const [restoreTimeout, setRestoreTimeout] = useState(false);
  const [feedLimit, setFeedLimit] = useState(8);

  function processCsvText(txt, fileName = "file.csv") {
    const lines = txt.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
    if (!lines.length) return;
    const startIdx = lines[0].toLowerCase().includes("sku") ? 1 : 0;
    const parsedRows = [];
    for (let i = startIdx; i < lines.length; i++) {
      const parts = lines[i].split(",").map(p => p.trim().replace(/^["']|["']$/g, ""));
      if (parts.length >= 2) {
        parsedRows.push({
          sku: parts[0] || `SKU-${i}`,
          title: parts[1] || parts[0],
          list_price_inr: parseInt(parts[2] || "999", 10) || 999,
          stock_qty: parseInt(parts[3] || "10", 10) || 10,
          category: "general"
        });
      }
    }
    if (parsedRows.length > 0) {
      setConnectForm(f => ({ ...f, rows: parsedRows, csv: txt }));
      setCsvFileName(fileName);
      setCsvCount(parsedRows.length);
    }
  }

  function handleCsvFile(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (e) => {
      processCsvText(e.target.result, file.name);
    };
    reader.readAsText(file);
  }

  const headers = useCallback(() => ({
    "Authorization": token ? `Bearer ${token}` : "",
    "X-Store-Id": storeId
  }), [token, storeId]);

  const inflight = useRef(null);
  const reqId = useRef(0);

  const load = useCallback(async (tok = token, sid = storeId) => {
    if (!tok) return;
    inflight.current?.abort();
    const ctl = new AbortController();
    inflight.current = ctl;
    const mine = ++reqId.current;
    const stale = () => mine !== reqId.current;
    setBusy(true);
    setError("");

    try {
      const authH = { "Authorization": `Bearer ${tok}`, "X-Store-Id": sid };
      const opts = { headers: authH, signal: ctl.signal };
      const r = await fetch(`${API}/merchant/v1/dashboard?feed_limit=${feedLimit}`, { ...opts, cache: "no-store" });
      if (r.status === 401) {
        try {
          sessionStorage.removeItem("praman-token");
          sessionStorage.removeItem("store-id");
        } catch {}
        setToken("");
        setData(null);
        throw new Error("Session expired — please sign in again");
      }
      if (!r.ok) throw new Error(`API ${r.status}`);
      const j = await r.json();
      if (stale()) return;
      setData(j);
      try {
        sessionStorage.setItem("praman-token", tok);
        sessionStorage.setItem("store-id", sid);
      } catch {}

      try {
        const [o, s, p] = await Promise.all([
          fetch(`${API}/merchant/v1/orders?limit=12`, opts),
          fetch(`${API}/merchant/v1/stores`, opts),
          fetch(`${API}/merchant/v1/policy`, opts),
        ]);
        if (o.ok) {
          const oj = await o.json();
          if (!stale()) setOrders(oj);
        }
        if (s.ok) {
          const sj = await s.json();
          if (!stale()) {
            if (sj.stores?.length) setStores(sj.stores);
            setStoresData(sj);
          }
        }
        if (p.ok) {
          const pj = await p.json();
          if (!stale()) {
            setPolicy(pj.policy);
            setPolicyDraft(pj.policy);
          }
        }
      } catch {}
    } catch (e) {
      if (e?.name === "AbortError" || stale()) return;
      setError(e.message || String(e));
    } finally {
      if (!stale()) setBusy(false);
    }
  }, [token, storeId, feedLimit]);

  const loadCatalog = useCallback(async (q = catalogSearch, cat = catalogCat) => {
    if (!token) return;
    setCatalogBusy(true);
    try {
      const params = new URLSearchParams({ limit: "300" });
      if (q && q.trim()) params.set("q", q.trim());
      if (cat && cat !== "all") params.set("category", cat);
      const r = await fetch(`${API}/merchant/v1/catalog?${params.toString()}`, { headers: headers() });
      if (r.ok) {
        const cj = await r.json();
        setCatalogData(cj);
      }
    } catch {} finally {
      setCatalogBusy(false);
    }
  }, [token, headers, catalogSearch, catalogCat]);

  useEffect(() => {
    try {
      const savedTheme = localStorage.getItem("praman-theme") || "dark";
      setTheme(savedTheme);
      if (savedTheme === "dark") {
        document.documentElement.setAttribute("data-theme", "dark");
      } else {
        document.documentElement.removeAttribute("data-theme");
      }
    } catch {}

    try {
      const tk = sessionStorage.getItem("praman-token");
      const ss = sessionStorage.getItem("store-id");
      if (tk) setToken(tk);
      if (ss) setStoreId(ss);
    } catch {} finally {
      setAuthReady(true);
    }
  }, []);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    let timeout;
    async function poll() {
      if (cancelled || document.hidden) {
        timeout = setTimeout(poll, 20000);
        return;
      }
      await load();
      if (!cancelled) timeout = setTimeout(poll, 20000);
    }
    load();
    timeout = setTimeout(poll, 20000);
    const onVisible = () => { if (!document.hidden) load(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      clearTimeout(timeout);
      document.removeEventListener("visibilitychange", onVisible);
      inflight.current?.abort();
    };
  }, [token, storeId, load]);

  useEffect(() => {
    if (token && !data) {
      const t = setTimeout(() => setRestoreTimeout(true), 12000);
      return () => clearTimeout(t);
    } else {
      setRestoreTimeout(false);
    }
  }, [token, data]);

  async function handleAuth() {
    if (!email.trim() || !password.trim()) {
      setError("Work email and password are required");
      return;
    }
    const cleanStore = (storeId.trim().toLowerCase() || "default");
    if (!/^[a-z0-9-]{2,32}$/.test(cleanStore)) {
      setError("Store identifier: 2-32 characters, lowercase and hyphens only");
      return;
    }
    setError("");
    setBusy(true);
    try {
      const path = mode === "signup" ? "/auth/signup" : "/auth/signin";
      const r = await fetch(`${API}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.trim().toLowerCase(), password, store_id: cleanStore })
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail?.message || `HTTP ${r.status}`);
      const tok = j.access_token;
      setToken(tok);
      setStoreId(cleanStore);
      try {
        sessionStorage.setItem("praman-token", tok);
        sessionStorage.setItem("store-id", cleanStore);
      } catch {}
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handlePolicySave() {
    if (!policyDraft) return;
    setBusy(true);
    try {
      const r = await fetch(`${API}/merchant/v1/policy`, {
        method: "PUT",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify(policyDraft)
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail?.message || `HTTP ${r.status}`);
      setPolicy(j.policy);
      setToast("Store rules saved successfully");
      setTimeout(() => setToast(""), 2500);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function decide(id, action) {
    try {
      const r = await fetch(`${API}/merchant/v1/approvals/${id}/${action}`, {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: "{}"
      });
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        throw new Error(b.detail?.message || `HTTP ${r.status}`);
      }
      setToast(`Order ${action}d successfully`);
      setTimeout(() => setToast(""), 2500);
    } catch (e) {
      setError(e.message);
    } finally {
      load();
    }
  }

  async function doCounter(id) {
    const el = document.getElementById(`cnt-${id}`);
    const amount = parseInt(el?.value || "", 10);
    if (!amount) {
      setToast("Please enter counter offer amount");
      setTimeout(() => setToast(""), 2200);
      return;
    }
    try {
      const r = await fetch(`${API}/merchant/v1/approvals/${id}/counter`, {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify({ counter_amount_inr: amount })
      });
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        throw new Error(b.detail?.message || `HTTP ${r.status}`);
      }
      setToast("Counter offer dispatched to customer");
      setTimeout(() => setToast(""), 2500);
    } catch (e) {
      setError(e.message);
    } finally {
      load();
    }
  }

  async function openOrder(orderId) {
    setSelectedOrder(orderId);
    try {
      const r = await fetch(`${API}/merchant/v1/orders/${orderId}`, { headers: headers() });
      if (r.ok) setDrawerData(await r.json());
    } catch {}
  }

  async function doConnect() {
    setSyncState({ busy: true, progress: "Connecting to store backend...", error: "", success: "" });
    try {
      let r;
      if (connectKind === "shopify") {
        r = await fetch(`${API}/merchant/v1/stores/connect/shopify`, {
          method: "POST",
          headers: { ...headers(), "Content-Type": "application/json" },
          body: JSON.stringify({ domain: connectForm.domain, token: connectForm.token })
        });
      } else if (connectKind === "woocommerce") {
        r = await fetch(`${API}/merchant/v1/stores/connect/woocommerce`, {
          method: "POST",
          headers: { ...headers(), "Content-Type": "application/json" },
          body: JSON.stringify({ url: connectForm.url, key: connectForm.key, secret: connectForm.secret })
        });
      } else {
        let rows = connectForm.rows;
        if (!rows || !rows.length) {
          const lines = (connectForm.csv || "").split(/\r?\n/).map(l => l.trim()).filter(Boolean);
          const startIdx = lines[0]?.toLowerCase().includes("sku") ? 1 : 0;
          rows = lines.slice(startIdx).map((line, idx) => {
            const parts = line.split(",").map(p => p.trim().replace(/^["']|["']$/g, ""));
            return {
              sku: parts[0] || `SKU-${idx + 1}`,
              title: parts[1] || `Product ${idx + 1}`,
              list_price_inr: parseInt(parts[2] || "999", 10) || 999,
              stock_qty: parseInt(parts[3] || "10", 10) || 10,
              category: "general"
            };
          });
        }
        if (!rows || !rows.length) {
          throw new Error("Please upload a CSV file or enter product rows");
        }
        r = await fetch(`${API}/merchant/v1/stores/connect/custom`, {
          method: "POST",
          headers: { ...headers(), "Content-Type": "application/json" },
          body: JSON.stringify({ rows })
        });
      }
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail?.message || j.message || `HTTP ${r.status}`);

      if (j.job_id) {
        setSyncState({ busy: true, progress: `Importing catalog from ${formatStoreName(connectForm.domain)}...`, error: "", success: "" });
        let finished = false;
        for (let i = 0; i < 12; i++) {
          await new Promise(res => setTimeout(res, 4000));
          try {
            const pr = await fetch(`${API}/merchant/v1/stores/sync/${j.job_id}`, { headers: headers() });
            const pj = await pr.json().catch(() => ({}));
            if (pj.status === "done") {
              setSyncState({ busy: false, progress: "", error: "", success: `Imported ${pj.imported || 0} products successfully!` });
              finished = true;
              break;
            }
            if (pj.status === "failed") {
              throw new Error(pj.error || "Sync failed on provider");
            }
            setSyncState({ busy: true, progress: `Syncing products: ${pj.imported || 0} products imported...`, error: "", success: "" });
          } catch (e) {
            if (i === 11) throw e;
          }
        }
        if (!finished) {
          setSyncState({ busy: false, progress: "", error: "", success: "Sync running in background." });
        }
        await load();
        setTimeout(() => {
          setConnectOpen(false);
          setSyncState({ busy: false, progress: "", error: "", success: "" });
        }, 2200);
      } else {
        setSyncState({ busy: false, progress: "", error: "", success: `Imported ${j.imported || 0} products!` });
        await load();
        setTimeout(() => {
          setConnectOpen(false);
          setSyncState({ busy: false, progress: "", error: "", success: "" });
        }, 1800);
      }
    } catch (e) {
      setSyncState({ busy: false, progress: "", error: e.message || String(e), success: "" });
    }
  }

  if (!authReady) {
    return (
      <main className="auth-wrapper">
        <div style={{ width: 40, height: 40, border: "3px solid var(--border)", borderTopColor: "var(--brand)", borderRadius: "50%", animation: "spin 0.8s linear infinite" }} />
      </main>
    );
  }

  if (API_MISCONFIGURED) {
    return (
      <main className="auth-wrapper">
        <div className="auth-card" style={{ textAlign: "center" }}>
          <h1 style={{ color: "var(--alert)" }}>Backend Not Connected</h1>
          <p className="auth-sub">{API_MISCONFIGURED_MESSAGE}</p>
        </div>
      </main>
    );
  }

  /* ── Unauthenticated Sign-in View ── */
  if (!data) {
    if (token) {
      return (
        <main className="auth-wrapper">
          <div className="auth-card" style={{ textAlign: "center" }}>
            <div style={{ width: 36, height: 36, border: "3px solid var(--border)", borderTopColor: "var(--brand)", borderRadius: "50%", margin: "0 auto 16px", animation: "spin 0.8s linear infinite" }} />
            <h2 style={{ fontSize: 17, color: "var(--text-primary)" }}>Connecting to Store</h2>
            <p className="auth-sub" style={{ margin: "4px 0 16px" }}>Loading protected store metrics...</p>
            {restoreTimeout ? (
              <div style={{ marginTop: 14, fontSize: 12, color: "var(--warn)" }}>
                Connection taking longer than usual.
                <div style={{ display: "flex", gap: 8, justifyContent: "center", marginTop: 12 }}>
                  <button className="pill dark" onClick={() => { setRestoreTimeout(false); load(); }}>Retry</button>
                  <button className="pill dark" onClick={() => { try { sessionStorage.clear(); } catch {}; setToken(""); setData(null); }}>Sign In Again</button>
                </div>
              </div>
            ) : null}
            {error ? <div className="error">{error}</div> : null}
          </div>
        </main>
      );
    }

    return (
      <main className="auth-wrapper">
        <div className="auth-card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <PramanLogo size={24} />
              <span style={{ fontSize: 13, fontWeight: 700, letterSpacing: "0.02em", color: "var(--text-heading)", textTransform: "uppercase" }}>PRAMAN</span>
              <span className="mono" style={{ fontSize: 10, color: "var(--text-muted)" }}>v2.4</span>
            </div>
            <AnimatedThemeToggle theme={theme} setTheme={setTheme} />
          </div>
          <h1>Sign in to PRAMAN</h1>
          <p className="auth-sub">
            Monitor automated sales, review held customer orders, and manage store discount bounds in one place.
          </p>
          <form onSubmit={(e) => { e.preventDefault(); handleAuth(); }}>
            <div className="field">
              <label htmlFor="email">Work Email</label>
              <input
                id="email"
                type="email"
                placeholder="merchant@company.com"
                value={email}
                onChange={e => setEmail(e.target.value)}
                autoComplete="email"
                autoCapitalize="off"
                spellCheck={false}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="password">Password</label>
              <input
                id="password"
                type="password"
                placeholder="••••••••••••"
                value={password}
                onChange={e => setPassword(e.target.value)}
                autoComplete={mode === "signup" ? "new-password" : "current-password"}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="storeId">Store Identifier</label>
              <input
                id="storeId"
                placeholder="e.g. default or store_01"
                value={storeId}
                onChange={e => setStoreId(e.target.value.trim().toLowerCase())}
                autoComplete="off"
                spellCheck={false}
              />
              <span className="muted" style={{ fontSize: 11 }}>
                Leave blank for default store.
              </span>
            </div>
            <div style={{ display: "flex", gap: 10, marginTop: 18, flexDirection: "column" }}>
              <button
                type="submit"
                className="pill primary"
                style={{ width: "100%", justifyContent: "center" }}
                disabled={busy}
              >
                {busy ? "Signing in…" : mode === "signup" ? "Create Account" : "Access Console"}
              </button>
              <button
                type="button"
                className="pill dark"
                style={{ width: "100%", justifyContent: "center" }}
                onClick={() => { setError(""); setMode(mode === "signup" ? "signin" : "signup"); }}
              >
                {mode === "signup" ? "Already registered? Sign In" : "Register New Merchant Store"}
              </button>
            </div>
          </form>
          {error ? <div className="error">{error}</div> : null}
          <div style={{ borderTop: "1px solid var(--border-subtle)", marginTop: 22, paddingTop: 12, textAlign: "center", fontSize: 11, color: "var(--text-muted)" }}>
            Protected by PRAMAN Automated Guardrails
          </div>
        </div>
      </main>
    );
  }

  const m = data.metrics;
  const hasHolds = data.approvals.pending_count > 0;
  const activeConn = storesData?.connected?.find(c => c.store_id === storeId) || storesData?.connected?.[0];
  const currentSkuCount = storesData?.catalog_counts?.[storeId] ?? (data?.metrics?.catalog_skus ?? 0);
  const activeStoreDisplayName = formatStoreName(activeConn?.domain || storeId);

  return (
    <>
      {/* ── Top Navigation Bar (Spacious 3-Column Grid) ── */}
      <nav className="nav" aria-label="Main Navigation">
        {/* Left: Logo & Store Brand */}
        <div className="nav-left">
          <div className="nav-brand">
            <PramanLogo size={28} />
            <span className="nav-brand-title">PRAMAN</span>
          </div>
          <span className="nav-store-badge">
            <span style={{ width: 6, height: 6, borderRadius: "50%", background: activeConn ? "var(--safe)" : "var(--text-muted)" }} />
            {activeStoreDisplayName}
          </span>
        </div>

        {/* Center: Segmented Views */}
        <div className="nav-center">
          <div className="nav-tabs" role="tablist" aria-label="Console Views">
            <button
              role="tab"
              aria-selected={activeTab === "overview"}
              className={`nav-tab-btn ${activeTab === "overview" ? "active" : ""}`}
              onClick={() => setActiveTab("overview")}
            >
              Overview
            </button>
            <button
              role="tab"
              aria-selected={activeTab === "catalog"}
              className={`nav-tab-btn ${activeTab === "catalog" ? "active" : ""}`}
              onClick={() => {
                setActiveTab("catalog");
                loadCatalog(catalogSearch, catalogCat);
              }}
            >
              Catalog
              <span className="nav-tab-count">{currentSkuCount}</span>
            </button>
          </div>
        </div>

        {/* Right: Animated Theme Switch & Controls */}
        <div className="nav-right">
          <AnimatedThemeToggle theme={theme} setTheme={setTheme} />

          <div className="nav-actions">
            <select
              value={storeId}
              onChange={e => setStoreId(e.target.value)}
              aria-label="Select Store"
            >
              {stores.map(s => <option key={s} value={s}>{formatStoreName(s)}</option>)}
            </select>

            <button
              onClick={() => {
                setConnectForm(f => ({ ...f, domain: activeConn?.domain || f.domain }));
                setConnectOpen(true);
              }}
              aria-label="Sync Store"
            >
              <IconRefresh size={12} />
              {activeConn ? "Sync" : "Connect"}
            </button>

            <button
              onClick={() => setShowActivity(!showActivity)}
              aria-label="Toggle Store Rules"
            >
              <IconShield size={12} />
              {showActivity ? "Close" : "Rules"}
            </button>

            <button
              onClick={async () => {
                try { await fetch(`${API}/auth/signout`, { method: "POST", headers: { ...headers() } }); } catch {};
                try { sessionStorage.clear(); } catch {};
                setData(null);
                setToken("");
              }}
              aria-label="Sign Out"
            >
              Sign Out
            </button>
          </div>
        </div>
      </nav>

      {/* ── Main Console Layout ── */}
      <main className="wrap">
        {/* Connected Store / Inventory Header */}
        <div
          className="panel"
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            flexWrap: "wrap",
            gap: 12,
            marginBottom: 16
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
            <div style={{ width: 38, height: 38, borderRadius: 8, background: "var(--brand-dim)", border: "1px solid var(--border)", display: "grid", placeItems: "center", color: "var(--brand)" }}>
              <IconStore size={18} />
            </div>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ fontSize: 11, letterSpacing: "0.06em", textTransform: "uppercase", color: "var(--brand)", fontWeight: 700 }}>
                  {activeConn ? `${activeConn.platform.toUpperCase()} CONNECTED` : "STORE INVENTORY"}
                </span>
                {activeConn?.connected_at ? (
                  <span className="muted" style={{ fontSize: 11 }}>· Synced {fmtDate(activeConn.connected_at)}</span>
                ) : null}
              </div>
              <h3 style={{ margin: "2px 0 0", fontSize: 16, fontWeight: 700, color: "var(--text-primary)" }}>
                {activeStoreDisplayName}
              </h3>
              <p className="muted" style={{ margin: "2px 0 0", fontSize: 12 }}>
                <strong style={{ color: "var(--text-primary)" }}>{currentSkuCount} products</strong> under active price and discount guardrails
              </p>
            </div>
          </div>
          <button
            className="pill dark"
            onClick={() => {
              setConnectForm(f => ({ ...f, domain: activeConn?.domain || "" }));
              setConnectOpen(true);
            }}
          >
            <IconRefresh size={13} />
            {activeConn ? "Sync Inventory" : "Connect Store Backend"}
          </button>
        </div>

        {activeTab === "catalog" ? (
          /* ── Catalog View ── */
          <>
            <div className="stat-grid">
              <div className="stat">
                <div className="k">Total Products</div>
                <div className="v">{catalogData?.total_count ?? currentSkuCount}</div>
                <div className="muted">Active SKUs in verified catalog</div>
              </div>
              <div className="stat">
                <div className="k">Total Units</div>
                <div className="v">{catalogData?.total_stock ? catalogData.total_stock.toLocaleString() : "—"}</div>
                <div className="muted">Tracked across all categories</div>
              </div>
              <div className="stat">
                <div className="k">Guardrail Status</div>
                <div className="v" style={{ fontSize: 20, color: "var(--safe)" }}>10 Rules Active</div>
                <div className="muted">Pricing floors &amp; discount limits</div>
              </div>
            </div>

            <div className="panel" style={{ marginBottom: 14, padding: "12px 16px" }}>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", justifyContent: "space-between" }}>
                <div style={{ display: "flex", gap: 8, flex: 1, minWidth: 260 }}>
                  <div style={{ position: "relative", flex: 1 }}>
                    <div style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "var(--text-muted)", display: "flex" }}>
                      <IconSearch size={14} />
                    </div>
                    <input
                      style={{
                        width: "100%",
                        background: "var(--bg-card)",
                        color: "var(--text-primary)",
                        border: "1px solid var(--border)",
                        borderRadius: 7,
                        padding: "8px 12px 8px 32px",
                        fontSize: 13
                      }}
                      placeholder="Search by SKU, product name, or brand..."
                      value={catalogSearch}
                      onChange={e => {
                        setCatalogSearch(e.target.value);
                        loadCatalog(e.target.value, catalogCat);
                      }}
                      aria-label="Search Catalog"
                    />
                  </div>
                  <CustomCategoryPicker
                    categories={catalogData?.categories || []}
                    value={catalogCat}
                    onChange={setCatalogCat}
                    onSelect={cat => loadCatalog(catalogSearch, cat)}
                  />
                </div>
                <div style={{ display: "flex", gap: 8 }}>
                  <button className="pill dark" onClick={() => loadCatalog(catalogSearch, catalogCat)}>
                    <IconRefresh size={12} />
                    {catalogBusy ? "Refreshing..." : "Refresh"}
                  </button>
                  <button className="pill primary" onClick={() => setConnectOpen(true)}>
                    + Import Catalog
                  </button>
                </div>
              </div>
            </div>

            <div className="panel">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
                <h2 style={{ margin: 0 }}>Product Catalog</h2>
                <span className="mono muted" style={{ fontSize: 11 }}>
                  Showing {catalogData?.products?.length ?? 0} of {catalogData?.total_count ?? currentSkuCount} items
                </span>
              </div>

              <div className="orders-wrap">
                <table className="orders-table" role="table" aria-label="Product Catalog">
                  <thead>
                    <tr>
                      <th>SKU</th>
                      <th>Product Title</th>
                      <th>Category</th>
                      <th style={{ textAlign: "right" }}>Retail Price</th>
                      <th style={{ textAlign: "right" }}>Floor Price</th>
                      <th style={{ textAlign: "center" }}>Max Discount</th>
                      <th>Stock</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {!catalogData && catalogBusy ? (
                      <tr><td colSpan={8} style={{ textAlign: "center", padding: 32, color: "var(--text-muted)" }}>Loading catalog inventory…</td></tr>
                    ) : !catalogData?.products?.length ? (
                      <tr><td colSpan={8} style={{ textAlign: "center", padding: 32, color: "var(--text-muted)" }}>No products match your search.</td></tr>
                    ) : (
                      catalogData.products.map(p => {
                        const isLow = p.stock_qty <= 10 && p.stock_qty > 0;
                        const isOut = p.stock_qty <= 0;
                        return (
                          <tr
                            key={p.sku}
                            onClick={() => setSelectedProduct(p)}
                            onKeyDown={e => e.key === "Enter" && setSelectedProduct(p)}
                            tabIndex={0}
                            role="button"
                            aria-label={`View details for ${p.title}`}
                          >
                            <td className="mono" style={{ fontSize: 12, color: "var(--brand)", fontWeight: 500 }}>{p.sku}</td>
                            <td style={{ fontWeight: 500, color: "var(--text-primary)" }}>{p.title}</td>
                            <td><span className="nav-store-badge">{fmtCategory(p.category)}</span></td>
                            <td className="num" style={{ textAlign: "right", fontWeight: 600 }}>{money(p.list_price_inr)}</td>
                            <td className="num" style={{ textAlign: "right", color: "var(--safe)", fontWeight: 600 }}>
                              {p.floor_price_inr ? money(p.floor_price_inr) : "—"}
                            </td>
                            <td className="num" style={{ textAlign: "center" }}>
                              {p.max_discount_pct ? `${p.max_discount_pct}%` : "—"}
                            </td>
                            <td>
                              <span className="stock-badge">
                                <span className={`stock-dot ${isOut ? "out-of-stock" : isLow ? "low-stock" : "in-stock"}`} />
                                <span>{isOut ? "Out of stock" : `${p.stock_qty} in stock`}</span>
                              </span>
                            </td>
                            <td>
                              <span className={`state ${p.offerable ? "CONFIRMED" : "FAILED"}`}>
                                {p.offerable ? "Active" : "Paused"}
                              </span>
                            </td>
                          </tr>
                        );
                      })
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </>
        ) : (
          /* ── Overview Dashboard View ── */
          <>
            <div className="stat-grid">
              <div className="stat">
                <div className="k">Today's Revenue</div>
                <div className="v">{money(m.revenue_inr)}</div>
                <div className="muted">{m.orders} settled orders · Avg {money(m.aov_inr)}</div>
              </div>
              <div className="stat">
                <div className="k">Orders Awaiting Approval</div>
                <div className="v" style={{ color: hasHolds ? "var(--warn)" : "var(--safe)" }}>
                  {data.approvals.pending_count}
                </div>
                <div className="muted">{hasHolds ? "Requires your confirmation" : "All automatic orders cleared"}</div>
              </div>
              <div className="stat">
                <div className="k">Security &amp; Audit Log</div>
                <div className="v mono" style={{ fontSize: 20 }}>
                  {data.chain.intact === true ? "Healthy & Safe ✓" : "Review Needed"}
                </div>
                <div className="muted">
                  #{data.chain.head_seq ?? "—"} verified records · <a href="/audit/verify" style={{ color: "var(--brand)", textDecoration: "none" }}>View Log →</a>
                </div>
              </div>
            </div>

            {/* Approvals Queue (Streamlined & Minimal) */}
            {/* Approvals Queue (Streamlined & Minimal — No Bulky Box) */}
            {hasHolds ? (
              <div style={{ marginBottom: 22 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontSize: 11, fontWeight: 700, padding: "3px 9px", borderRadius: 999, background: "var(--warn-bg)", color: "var(--warn)", border: "1px solid var(--warn-border)", letterSpacing: "0.04em", textTransform: "uppercase" }}>
                      Action Required
                    </span>
                    <h2 style={{ margin: 0, fontSize: 15, fontWeight: 600, color: "var(--text-heading)" }}>
                      {data.approvals.pending_count} Order{data.approvals.pending_count > 1 ? "s" : ""} Awaiting Confirmation
                    </h2>
                  </div>
                  <span className="muted" style={{ fontSize: 12 }}>Transactions exceeding auto-approval limit</span>
                </div>

                <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  {data.approvals.queue.map(a => (
                    <div className="approval-item" key={a.approval_id}>
                      <div>
                        <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
                          <strong className="num" style={{ fontSize: 17, color: "var(--text-heading)" }}>{money(a.amount_inr)}</strong>
                          <span className="mono muted" style={{ fontSize: 11 }}>{a.order_id}</span>
                        </div>
                        <div style={{ fontSize: 12, marginTop: 4, color: "var(--text-secondary)" }}>
                          {humanHoldNote(a.note)}
                        </div>
                      </div>
                      <div className="approval-actions">
                        <button className="ios-btn ios-btn-approve" onClick={() => decide(a.approval_id, "approve")} aria-label="Approve Order">
                          <IconCheck size={12} /> Approve
                        </button>
                        <button className="ios-btn ios-btn-reject" onClick={() => decide(a.approval_id, "reject")} aria-label="Reject Order">
                          <IconClose size={12} /> Reject
                        </button>
                        <div className="ios-counter-capsule">
                          <span className="ios-counter-prefix">₹</span>
                          <input id={`cnt-${a.approval_id}`} className="ios-counter-input" placeholder="Counter" aria-label="Counter amount" />
                          <button className="ios-counter-btn" onClick={() => doCounter(a.approval_id)} aria-label="Send Counter Offer">
                            Send
                          </button>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {/* Recent Orders */}
            <div className="panel">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, flexWrap: "wrap", gap: 8 }}>
                <h2 style={{ margin: 0 }}>Recent Orders</h2>
                <span className="mono muted" style={{ fontSize: 11 }}>
                  Store: {activeStoreDisplayName} · {orders?.count || 0} orders
                </span>
              </div>

              <div className="orders-wrap">
                <table className="orders-table" role="table" aria-label="Recent Orders">
                  <thead>
                    <tr>
                      <th>Order ID</th>
                      <th>Product</th>
                      <th style={{ textAlign: "right" }}>Amount</th>
                      <th>Status</th>
                      <th>Time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {orders === null ? (
                      <tr><td colSpan={5} style={{ textAlign: "center", padding: 24, color: "var(--text-muted)" }}>Loading orders…</td></tr>
                    ) : (orders?.orders || []).map(o => (
                      <tr
                        key={o.order_id}
                        onClick={() => openOrder(o.order_id)}
                        onKeyDown={e => e.key === "Enter" && openOrder(o.order_id)}
                        tabIndex={0}
                        role="button"
                        aria-label={`Open order ${o.order_id}`}
                      >
                        <td className="mono" style={{ fontSize: 12, color: "var(--brand)", fontWeight: 500 }}>
                          {o.order_id.slice(0, 14)}…
                        </td>
                        <td style={{ maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text-primary)", fontWeight: 500 }}>
                          {o.title_summary || o.offer_id}
                        </td>
                        <td className="num" style={{ textAlign: "right", fontWeight: 600 }}>{money(o.amount_inr)}</td>
                        <td>
                          <span className={`state ${o.state}`}>
                            {o.state === "CONFIRMED" ? "Settled" : o.state === "PENDING" ? "Awaiting Payment" : o.state === "HELD" ? "Under Hold" : o.state}
                          </span>
                        </td>
                        <td className="mono muted" style={{ fontSize: 11 }}>{fmtDate(o.created_at)}</td>
                      </tr>
                    ))}
                    {orders && !(orders?.orders || []).length ? (
                      <tr>
                        <td colSpan={5} style={{ textAlign: "center", padding: 32, color: "var(--text-muted)" }}>
                          No customer orders recorded yet.
                        </td>
                      </tr>
                    ) : null}
                  </tbody>
                </table>
              </div>

              {/* Order Detail Drawer (Structured Stepper & KPI Cards) */}
              {selectedOrder && drawerData ? (
                <div className="order-drawer-panel">
                  <div className="order-drawer-header">
                    <div>
                      <span className="mono muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em" }}>Order Inspection</span>
                      <h3 style={{ margin: "3px 0 0", fontSize: 15, fontWeight: 700, color: "var(--text-heading)" }}>
                        Order <span className="mono" style={{ color: "var(--accent)" }}>{selectedOrder}</span>
                      </h3>
                    </div>
                    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      <a href={`/audit/${selectedOrder}`} className="pill dark" style={{ textDecoration: "none", fontSize: 12 }}>
                        View Full History →
                      </a>
                      <button onClick={() => { setSelectedOrder(null); setDrawerData(null); }} className="pill dark" aria-label="Close">
                        <IconClose size={13} /> Close
                      </button>
                    </div>
                  </div>

                  {drawerData.order ? (
                    <div className="order-drawer-kpis">
                      <div className="order-drawer-kpi">
                        <div className="k">Total Amount</div>
                        <div className="v num">{money(drawerData.order.amount_inr)}</div>
                      </div>
                      <div className="order-drawer-kpi">
                        <div className="k">Fulfillment Status</div>
                        <div style={{ marginTop: 6 }}>
                          <span className={`state ${drawerData.order.state}`}>
                            {drawerData.order.state === "CONFIRMED" ? "Settled" : drawerData.order.state === "HELD" ? "Under Hold" : drawerData.order.state === "PENDING" ? "Awaiting Payment" : drawerData.order.state}
                          </span>
                        </div>
                      </div>
                    </div>
                  ) : null}

                  <h4 style={{ fontSize: 13, fontWeight: 600, color: "var(--text-heading)", margin: "14px 0 10px" }}>Order Activity &amp; Protection Steps</h4>
                  <div className="timeline-stepper">
                    {(drawerData.trail || []).slice(-6).map((e, idx) => {
                      const h = humanEvent(e);
                      const dotCls = h.tone === "good" ? "good" : h.tone === "bad" ? "bad" : h.tone === "warn" ? "warn" : "neutral";
                      return (
                        <div key={idx} className="timeline-step">
                          <div className={`timeline-dot ${dotCls}`} />
                          <div className="timeline-card">
                            <div className="timeline-card-header">
                              <span className="timeline-card-title">{h.title}</span>
                              <span className="timeline-card-time">{fmtDate(e.ts)}</span>
                            </div>
                            <div className="timeline-card-desc">{h.desc}</div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ) : null}
            </div>

            {/* Bottom Controls */}
            <div style={{ textAlign: "center", marginTop: 16, display: "flex", gap: 10, justifyContent: "center", flexWrap: "wrap" }}>
              <button className="pill dark" onClick={() => setShowActivity(!showActivity)}>
                <IconShield size={13} />
                {showActivity ? "Hide Store Rules" : "Store Rules & Policy"}
              </button>
              <a href="/audit/verify" className="pill dark" style={{ textDecoration: "none" }}>
                <IconCheck size={13} />
                Check Full Audit Log
              </a>
            </div>

            {/* Rules & Policy Drawer */}
            {showActivity ? (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 14, marginTop: 14 }}>
                <div className="panel">
                  <h2>Store Discount Rules</h2>
                  {!policyDraft ? (
                    <div className="muted">Loading rules…</div>
                  ) : (
                    <>
                      <div className="field">
                        <label>Max Discount Per Item (%)</label>
                        <input
                          type="number"
                          value={policyDraft.item_discount_cap}
                          onChange={e => setPolicyDraft({ ...policyDraft, item_discount_cap: parseInt(e.target.value || "0", 10) })}
                        />
                      </div>
                      <div className="field">
                        <label>Max Cart Discount (%)</label>
                        <input
                          type="number"
                          value={policyDraft.cart_discount_cap}
                          onChange={e => setPolicyDraft({ ...policyDraft, cart_discount_cap: parseInt(e.target.value || "0", 10) })}
                        />
                      </div>
                      <div className="field">
                        <label>Automated Approval Limit (₹)</label>
                        <input
                          type="number"
                          value={policyDraft.approval_limit}
                          onChange={e => setPolicyDraft({ ...policyDraft, approval_limit: parseInt(e.target.value || "0", 10) })}
                        />
                      </div>
                      <button className="pill primary" style={{ marginTop: 10, width: "100%" }} onClick={handlePolicySave}>
                        Save Store Rules
                      </button>
                    </>
                  )}
                </div>

                <div className="panel">
                  <h2>Recent Store Activity</h2>
                  <ul style={{ listStyle: "none", padding: 0 }}>
                    {(data.feed || []).slice(0, 6).map(e => {
                      const h = humanEvent(e);
                      return (
                        <li key={e.seq} style={{ display: "flex", gap: 8, padding: "8px 0", borderBottom: "1px solid var(--border-subtle)" }}>
                          <span className="mono muted" style={{ fontSize: 11, minWidth: 60 }}>{fmtDate(e.ts)}</span>
                          <span style={{ fontSize: 13, flex: 1, color: h.tone === "good" ? "var(--safe)" : h.tone === "bad" ? "var(--alert)" : h.tone === "warn" ? "var(--warn)" : "var(--text-primary)" }}>
                            {h.title}
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              </div>
            ) : null}
          </>
        )}

        {/* ── Product Details Modal ── */}
        {selectedProduct ? (
          <div className="modal-backdrop" onClick={() => setSelectedProduct(null)}>
            <div className="modal" onClick={e => e.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="modal-prod-title">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", borderBottom: "1px solid var(--border)", paddingBottom: 12 }}>
                <div>
                  <span className="mono" style={{ fontSize: 11, color: "var(--brand)" }}>{selectedProduct.sku}</span>
                  <h3 id="modal-prod-title" style={{ margin: "2px 0 0", fontSize: 16, color: "var(--text-primary)" }}>{selectedProduct.title}</h3>
                </div>
                <button className="pill dark" onClick={() => setSelectedProduct(null)} aria-label="Close">
                  <IconClose size={13} />
                </button>
              </div>

              <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 10, marginTop: 14 }}>
                <div className="stat" style={{ padding: 12 }}>
                  <div className="k">Retail Price</div>
                  <div className="v num">{money(selectedProduct.list_price_inr)}</div>
                </div>
                <div className="stat" style={{ padding: 12 }}>
                  <div className="k">Floor Price</div>
                  <div className="v num" style={{ color: "var(--safe)" }}>{selectedProduct.floor_price_inr ? money(selectedProduct.floor_price_inr) : "—"}</div>
                </div>
                <div className="stat" style={{ padding: 12 }}>
                  <div className="k">Max Discount</div>
                  <div className="v num" style={{ color: "var(--warn)" }}>{selectedProduct.max_discount_pct ? `${selectedProduct.max_discount_pct}%` : "—"}</div>
                </div>
                <div className="stat" style={{ padding: 12 }}>
                  <div className="k">Stock Level</div>
                  <div className="v num">{selectedProduct.stock_qty} units</div>
                </div>
              </div>

              <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 18 }}>
                <button className="pill primary" onClick={() => setSelectedProduct(null)}>
                  Done
                </button>
              </div>
            </div>
          </div>
        ) : null}

        {/* Toasts */}
        {toast ? <div className="toast" role="alert">{toast}</div> : null}
        {error ? <div className="toast" style={{ borderColor: "var(--alert)", color: "var(--alert)" }} role="alert">{error}</div> : null}
      </main>

      {/* ── Connect Store Modal ── */}
      {connectOpen ? (
        <div className="modal-backdrop" onClick={() => !syncState.busy && setConnectOpen(false)}>
          <div className="modal" onClick={e => e.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="modal-connect-title">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
              <h3 id="modal-connect-title" style={{ margin: 0, fontSize: 16 }}>Connect Commerce Platform</h3>
              <button className="pill dark" onClick={() => !syncState.busy && setConnectOpen(false)} aria-label="Close">
                <IconClose size={13} />
              </button>
            </div>
            <p className="muted" style={{ margin: "2px 0 14px", fontSize: 13 }}>
              Connect your store to sync inventory products and apply automated price guardrails.
            </p>

            {syncState.busy ? (
              <div style={{ padding: "28px 16px", textAlign: "center" }}>
                <div style={{ width: 36, height: 36, border: "3px solid var(--border)", borderTopColor: "var(--brand)", borderRadius: "50%", margin: "0 auto 14px", animation: "spin 0.8s linear infinite" }} />
                <h4 style={{ margin: "0 0 6px", fontSize: 15 }}>Syncing Inventory...</h4>
                <p style={{ margin: 0, fontSize: 13, color: "var(--brand)" }}>{syncState.progress}</p>
              </div>
            ) : syncState.success ? (
              <div style={{ padding: "28px 16px", textAlign: "center" }}>
                <h4 style={{ margin: "0 0 6px", fontSize: 16, color: "var(--safe)" }}>Successfully Connected!</h4>
                <p style={{ margin: 0, fontSize: 13 }}>{syncState.success}</p>
              </div>
            ) : (
              <>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8, margin: "10px 0 14px" }}>
                  {[
                    { k: "shopify", t: "Shopify" },
                    { k: "woocommerce", t: "WooCommerce" },
                    { k: "custom", t: "CSV File" }
                  ].map(c => (
                    <button
                      key={c.k}
                      type="button"
                      className={`pill ${connectKind === c.k ? "primary" : "dark"}`}
                      onClick={() => setConnectKind(c.k)}
                      style={{ justifyContent: "center" }}
                    >
                      {c.t}
                    </button>
                  ))}
                </div>

                {connectKind === "shopify" ? (
                  <>
                    <div className="field">
                      <label htmlFor="shop-domain">Shopify Store Domain</label>
                      <input id="shop-domain" placeholder="your-store.myshopify.com" value={connectForm.domain} onChange={e => setConnectForm({ ...connectForm, domain: e.target.value })} />
                    </div>
                    <div className="field">
                      <label htmlFor="shop-token">Admin Access Token</label>
                      <input id="shop-token" type="password" placeholder="shpat_••••••••••••••••" value={connectForm.token} onChange={e => setConnectForm({ ...connectForm, token: e.target.value })} />
                    </div>
                  </>
                ) : connectKind === "woocommerce" ? (
                  <>
                    <div className="field">
                      <label htmlFor="woo-url">Store URL</label>
                      <input id="woo-url" placeholder="https://store.example.com" value={connectForm.url} onChange={e => setConnectForm({ ...connectForm, url: e.target.value })} />
                    </div>
                    <div className="field">
                      <label htmlFor="woo-key">Consumer Key</label>
                      <input id="woo-key" placeholder="ck_••••••••••••••••" value={connectForm.key} onChange={e => setConnectForm({ ...connectForm, key: e.target.value })} />
                    </div>
                    <div className="field">
                      <label htmlFor="woo-secret">Consumer Secret</label>
                      <input id="woo-secret" type="password" placeholder="cs_••••••••••••••••" value={connectForm.secret} onChange={e => setConnectForm({ ...connectForm, secret: e.target.value })} />
                    </div>
                  </>
                ) : (
                  <div style={{ marginTop: 10 }}>
                    <div
                      className={`csv-dropzone ${dragOver ? "dragover" : ""}`}
                      onClick={() => fileInputRef.current?.click()}
                      onDragOver={e => { e.preventDefault(); setDragOver(true); }}
                      onDragLeave={() => setDragOver(false)}
                      onDrop={e => {
                        e.preventDefault();
                        setDragOver(false);
                        if (e.dataTransfer.files?.[0]) handleCsvFile(e.dataTransfer.files[0]);
                      }}
                    >
                      <input
                        ref={fileInputRef}
                        type="file"
                        accept=".csv,text/csv"
                        style={{ display: "none" }}
                        onChange={e => e.target.files?.[0] && handleCsvFile(e.target.files[0])}
                      />
                      <div className="csv-dropzone-icon">
                        <IconBox size={22} />
                      </div>
                      <p>{csvFileName ? `File: ${csvFileName}` : "Click to choose CSV or drag & drop file here"}</p>
                      <span>Expected columns: SKU, Product Title, Price (INR), Stock Quantity</span>
                      {csvCount > 0 ? (
                        <div className="csv-preview-badge">
                          <IconCheck size={13} /> {csvCount} products parsed &amp; ready to import
                        </div>
                      ) : null}
                    </div>

                    <div style={{ textAlign: "center", marginTop: 8 }}>
                      <button
                        type="button"
                        className="pill dark"
                        style={{ fontSize: 11, padding: "3px 10px", minHeight: 26 }}
                        onClick={() => setShowManualCsv(!showManualCsv)}
                      >
                        {showManualCsv ? "Hide manual CSV text" : "Or paste CSV text manually"}
                      </button>
                    </div>

                    {showManualCsv ? (
                      <div className="field" style={{ marginTop: 10 }}>
                        <label htmlFor="csv-input">CSV Text (sku, title, price, stock)</label>
                        <textarea
                          id="csv-input"
                          rows={3}
                          placeholder="SKU-001,Premium Headphone,4999,25"
                          value={connectForm.csv}
                          onChange={e => {
                            setConnectForm(f => ({ ...f, csv: e.target.value }));
                            processCsvText(e.target.value, "manual_input.csv");
                          }}
                        />
                      </div>
                    ) : null}
                  </div>
                )}

                {syncState.error ? <div className="error">{syncState.error}</div> : null}

                <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16 }}>
                  <button className="pill dark" onClick={() => setConnectOpen(false)}>Cancel</button>
                  <button className="pill primary" onClick={doConnect}>Begin Sync</button>
                </div>
              </>
            )}
          </div>
        </div>
      ) : null}
    </>
  );
}
