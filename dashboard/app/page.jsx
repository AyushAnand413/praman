"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import "./globals.css";
import { API, API_MISCONFIGURED, API_MISCONFIGURED_MESSAGE } from "./config";
function money(n){ if(n===null||n===undefined) return "—"; return `₹${new Intl.NumberFormat("en-IN").format(Number(n))}`; }
function fmtDate(s){ if(!s) return ""; try{ return new Date(s).toLocaleString("en-IN",{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"});}catch{ return s.slice(0,16);} }
function eventCopy(e){
  const ev = e.event;
  const amt = e.money_delta_inr;
  const r = e.reason || "";
  const pAmt = e.payload?.amount_inr;
  if(ev==="payment.intent") return { title:`Payment intent created — ${money(amt)} pending`, desc:"Intent to charge — not yet captured", tone:"warn" };
  if(ev==="payment.captured") return { title:`Payment captured — +${money(amt)}`, desc:"Funds moved to your account", tone:"good" };
  if(ev==="payment.failed"||ev==="payment.declined") return { title:"Payment failed", desc: r.slice(0,90) || "Card declined or network issue", tone:"bad" };
  if(ev==="razorpay.order.created") return { title:"Gateway order created", desc:"Razorpay order linked — awaiting card confirmation", tone:"warn" };
  if(ev==="order.held_for_approval") return { title:`Held for approval${pAmt ? ` — ${money(pAmt)}` : ""} — needs you`, desc: r || "Over auto-approve limit", tone:"warn" };
  if(ev==="offer.issued") return { title:"Offer issued to buyer", desc: r || "Bounded and signed offer sent", tone:"good" };
  if(ev==="offer.refused") return { title:"Offer refused", desc: r.slice(0,80) || "Bounds rejected this proposal", tone:"bad" };
  if(ev==="offer.request") return { title:"Offer requested", desc:"Buyer asked for a price", tone:"" };
  if(ev==="catalog.query") return { title:"Customer browsed catalog", desc:"Product search ran", tone:"" };
  if(ev==="catalog.synced") return { title:"Catalog synced", desc: r || "Products imported from connected store", tone:"good" };
  if(ev==="policy.updated") return { title:"Policy settings updated", desc: r || "Merchant changed store policy", tone:"" };
  if(ev==="ledger.genesis") return { title:"Ledger started", desc:"Hash chain genesis entry", tone:"good" };
  if(ev==="mandate.accepted") return { title:"Mandate verified", desc:"Signed agent mandate accepted", tone:"good" };
  if(ev==="checkout.rejected") return { title:"Checkout rejected", desc: r.slice(0,80) || "Bounds refused this purchase", tone:"bad" };
  if(ev==="proposal.emitted") return { title:"AI proposal generated", desc:"Vyapaari suggested items and discounts", tone:"" };
  if(ev==="saga.compensation_triggered") return { title:"Oversell compensated", desc:"Auto-refund triggered for out-of-stock", tone:"bad" };
  if(ev==="razorpay.refund") return { title:"Refund processed", desc: r.slice(0,80) || "Funds returned to buyer", tone:"bad" };
  if(ev==="offer.evaluated") return { title:"Offer evaluated", desc: r.slice(0,80) || "Bounds checked a proposal", tone:"" };
  return { title: ev?.replace(/\./g," · ") || "Event", desc: r.slice(0,60), tone:"" };
}
// Titles and descriptions are copy, so they live here. Whether an event went
// wrong is not copy — the API decides that (api/events.py) and sends `tone` plus
// `is_failure`, so an event this file has never heard of still highlights
// correctly instead of rendering as though nothing happened.
function humanEvent(e){
  const copy = eventCopy(e);
  const serverTone = e.tone === "neutral" ? "" : e.tone;
  return { ...copy, tone: serverTone ?? copy.tone };
}
export default function Page(){
  const [token,setToken]=useState("");
  const [email,setEmail]=useState("");
  const [password,setPassword]=useState("");
  const [storeId,setStoreId]=useState("");
  const [stores,setStores]=useState([]);
  const [storesData,setStoresData]=useState(null);
  const [syncState,setSyncState]=useState({busy:false, progress:"", error:"", success:""});
  const [activeTab,setActiveTab]=useState("overview");
  const [catalogData,setCatalogData]=useState(null);
  const [catalogSearch,setCatalogSearch]=useState("");
  const [catalogCat,setCatalogCat]=useState("all");
  const [selectedProduct,setSelectedProduct]=useState(null);
  const [catalogBusy,setCatalogBusy]=useState(false);
  const [data,setData]=useState(null);
  const [orders,setOrders]=useState(null);
  const [selectedOrder,setSelectedOrder]=useState(null);
  const [drawerData,setDrawerData]=useState(null);
  const [error,setError]=useState("");
  const [busy,setBusy]=useState(false);
  const [mode,setMode]=useState("signin");
  const [connectOpen,setConnectOpen]=useState(false);
  const [connectKind,setConnectKind]=useState("shopify");
  const [connectForm,setConnectForm]=useState({domain:"", token:"", url:"", key:"", secret:"", csv:""});
  const [toast,setToast]=useState("");
  const [showActivity,setShowActivity]=useState(false);
  const [policy,setPolicy]=useState(null);
  const [policyDraft,setPolicyDraft]=useState(null);
  const [authReady,setAuthReady]=useState(false);
  const [restoreTimeout,setRestoreTimeout]=useState(false);
  // How many feed entries to ask for. Grows on "Show older" rather than the
  // client stitching pages together, so the 6s poll keeps returning one
  // coherent window instead of racing an accumulated list.
  const [feedLimit,setFeedLimit]=useState(8);
  const headers = useCallback(()=>({ "Authorization": token?`Bearer ${token}`:"", "X-Store-Id": storeId }),[token,storeId]);
  // Two guards, because there are two ways a stale response can land. The
  // controller cancels the request that is still open; the counter makes a
  // response that already came back but belongs to an older call get dropped.
  // Without them, switching store while the 6s poll is mid-flight can repaint
  // the console with the previous store's numbers under the new store's name.
  const inflight = useRef(null);
  const reqId = useRef(0);
  const load = useCallback(async (tok=token, sid=storeId)=>{
    if(!tok) return;
    inflight.current?.abort();
    const ctl = new AbortController();
    inflight.current = ctl;
    const mine = ++reqId.current;
    const stale = ()=> mine !== reqId.current;
    setBusy(true); setError("");
    try{
      const authH = { "Authorization": `Bearer ${tok}`, "X-Store-Id": sid };
      const opts = { headers: authH, signal: ctl.signal };
      const r = await fetch(`${API}/merchant/v1/dashboard?feed_limit=${feedLimit}`, { ...opts, cache:"no-store"});
      if(r.status===401){ try{ sessionStorage.removeItem("praman-token"); sessionStorage.removeItem("store-id"); }catch{} setToken(""); setData(null); throw new Error("Session expired — please sign in again"); }
      if(!r.ok) throw new Error(`API ${r.status}`);
      const j = await r.json();
      if(stale()) return;
      setData(j);
      try{ sessionStorage.setItem("praman-token",tok); sessionStorage.setItem("store-id",sid);}catch{}
      try{
        const [o, s, p] = await Promise.all([
          fetch(`${API}/merchant/v1/orders?limit=12`, opts),
          fetch(`${API}/merchant/v1/stores`, opts),
          fetch(`${API}/merchant/v1/policy`, opts),
        ]);
        if(o.ok){ const oj=await o.json(); if(!stale()) setOrders(oj); }
        if(s.ok){
          const sj=await s.json();
          if(!stale()) {
            if(sj.stores?.length) setStores(sj.stores);
            setStoresData(sj);
          }
        }
        if(p.ok){ const pj=await p.json(); if(!stale()){ setPolicy(pj.policy); setPolicyDraft(pj.policy); } }
      }catch{}
    }catch(e){
      if(e?.name==="AbortError" || stale()) return;
      setError(e.message||String(e));
    }finally{ if(!stale()) setBusy(false); }
  },[token,storeId]);
  const loadCatalog = useCallback(async (q=catalogSearch, cat=catalogCat)=>{
    if(!token) return;
    setCatalogBusy(true);
    try{
      const params = new URLSearchParams({ limit: "300" });
      if(q && q.trim()) params.set("q", q.trim());
      if(cat && cat !== "all") params.set("category", cat);
      const r = await fetch(`${API}/merchant/v1/catalog?${params.toString()}`, { headers: headers() });
      if(r.ok){
        const cj = await r.json();
        setCatalogData(cj);
      }
    }catch(e){
    }finally{
      setCatalogBusy(false);
    }
  },[token, headers, catalogSearch, catalogCat]);
  useEffect(()=>{ try{ const tk=sessionStorage.getItem("praman-token"); const ss=sessionStorage.getItem("store-id"); if(tk) setToken(tk); if(ss) setStoreId(ss);}catch{} finally{ setAuthReady(true); } },[]);
  useEffect(()=>{
    if(!token) return;
    let cancelled=false;
    let timeout;
    async function poll(){
      if(cancelled || document.hidden) { timeout=setTimeout(poll, 20000); return; }
      await load();
      if(!cancelled) timeout=setTimeout(poll, 20000);
    }
    load();
    timeout=setTimeout(poll, 20000);
    const onVisible=()=>{ if(!document.hidden) load(); };
    document.addEventListener("visibilitychange", onVisible);
    return()=>{ cancelled=true; clearTimeout(timeout); document.removeEventListener("visibilitychange",onVisible); inflight.current?.abort(); };
  },[token,storeId,load]);
  useEffect(()=>{
    if(token && !data){
      const t=setTimeout(()=>setRestoreTimeout(true), 12000);
      return ()=>clearTimeout(t);
    } else setRestoreTimeout(false);
  },[token,data]);
  async function handleAuth(){
    if(!email.trim() || !password.trim()){ setError("Email and password required"); return; }
    const cleanStore = (storeId.trim().toLowerCase() || "default");
    if(!/^[a-z0-9-]{2,32}$/.test(cleanStore)){ setError("Store ID: 2-32 chars, lowercase, hyphens only"); return; }
    setError(""); setBusy(true);
    try{
      const path = mode==="signup"? "/auth/signup" : "/auth/signin";
      const r = await fetch(`${API}${path}`, { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({email: email.trim().toLowerCase(), password, store_id: cleanStore})});
      const j = await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.detail?.message||`HTTP ${r.status}`);
      const tok=j.access_token; setToken(tok); setStoreId(cleanStore); try{ sessionStorage.setItem("praman-token",tok); sessionStorage.setItem("store-id",cleanStore);}catch{}
    }catch(e){ setError(e.message||String(e)); }finally{ setBusy(false); }
  }
  async function handlePolicySave(){
    if(!policyDraft) return;
    setBusy(true);
    try{
      const r=await fetch(`${API}/merchant/v1/policy`,{method:"PUT", headers:{...headers(),"Content-Type":"application/json"}, body: JSON.stringify(policyDraft)});
      const j=await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.detail?.message||`HTTP ${r.status}`);
      setPolicy(j.policy); setToast("Policy saved"); setTimeout(()=>setToast(""),2500);
    }catch(e){ setError(e.message);}finally{ setBusy(false); }
  }
  async function decide(id,action){
    try{ const r=await fetch(`${API}/merchant/v1/approvals/${id}/${action}`,{method:"POST", headers:{...headers(),"Content-Type":"application/json"}, body:"{}"}); if(!r.ok){ const b=await r.json().catch(()=>({})); throw new Error(b.detail?.message||`HTTP ${r.status}`); } setToast(`${action} done`); setTimeout(()=>setToast(""),2500);}catch(e){ setError(e.message);}finally{ load(); }
  }
  async function doCounter(id){
    const el=document.getElementById(`cnt-${id}`); const amount=parseInt(el?.value||"",10); if(!amount) { setToast("enter amount"); setTimeout(()=>setToast(""),2000); return; }
    try{ const r=await fetch(`${API}/merchant/v1/approvals/${id}/counter`,{method:"POST", headers:{...headers(),"Content-Type":"application/json"}, body:JSON.stringify({counter_amount_inr:amount})}); if(!r.ok){ const b=await r.json().catch(()=>({})); throw new Error(b.detail?.message||`HTTP ${r.status}`); } setToast("counter sent"); setTimeout(()=>setToast(""),2500);}catch(e){ setError(e.message);}finally{ load(); }
  }
  async function openOrder(orderId){ setSelectedOrder(orderId); try{ const r=await fetch(`${API}/merchant/v1/orders/${orderId}`,{headers: headers()}); if(r.ok) setDrawerData(await r.json()); }catch{} }
  async function doConnect(){
    setSyncState({busy: true, progress: "Connecting to store and requesting sync...", error: "", success: ""});
    try{
      let r;
      if(connectKind==="shopify") r=await fetch(`${API}/merchant/v1/stores/connect/shopify`,{method:"POST", headers:{...headers(),"Content-Type":"application/json"}, body:JSON.stringify({domain:connectForm.domain, token:connectForm.token})});
      else if(connectKind==="woocommerce") r=await fetch(`${API}/merchant/v1/stores/connect/woocommerce`,{method:"POST", headers:{...headers(),"Content-Type":"application/json"}, body:JSON.stringify({url:connectForm.url, key:connectForm.key, secret:connectForm.secret})});
      else { const rows=connectForm.csv.split("\n").filter(Boolean).slice(0,5).map((line,idx)=>{ const [sku,title,price,stock]=line.split(","); return {sku:sku||`CSV-${idx}`, title:title||sku||`Item ${idx}`, list_price_inr:parseInt(price||"999",10), stock_qty:parseInt(stock||"10",10), category:"audio_accessories"}; }); r=await fetch(`${API}/merchant/v1/stores/connect/custom`,{method:"POST", headers:{...headers(),"Content-Type":"application/json"}, body:JSON.stringify({rows})}); }
      const j=await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.detail?.message||j.message||`HTTP ${r.status}`);
      if(j.job_id){
        setSyncState({busy: true, progress: `Importing catalog from ${connectForm.domain || "store"}...`, error: "", success: ""});
        let finished = false;
        for(let i=0;i<12;i++){
          await new Promise(res=>setTimeout(res, 4000));
          try{
            const pr = await fetch(`${API}/merchant/v1/stores/sync/${j.job_id}`, {headers: headers()});
            const pj = await pr.json().catch(()=>({}));
            if(pj.status==="done"){
              setSyncState({busy: false, progress: "", error: "", success: `Successfully imported ${pj.imported||0} products!`});
              finished = true;
              break;
            }
            if(pj.status==="failed"){ throw new Error(pj.error||"Sync failed on store provider"); }
            setSyncState({busy: true, progress: `Fetching products... ${pj.imported||0} items imported (${(i+1)*4}s)`, error: "", success: ""});
          }catch(e){ if(i===11) throw e; }
        }
        if(!finished){
          setSyncState({busy: false, progress: "", error: "", success: "Sync is running in background! Products are being populated."});
        }
        await load();
        setTimeout(()=>{
          setConnectOpen(false);
          setSyncState({busy: false, progress: "", error: "", success: ""});
        }, 2200);
      } else {
        setSyncState({busy: false, progress: "", error: "", success: `Imported ${j.imported||0} products!`});
        await load();
        setTimeout(()=>{
          setConnectOpen(false);
          setSyncState({busy: false, progress: "", error: "", success: ""});
        }, 1800);
      }
    }catch(e){
      setSyncState({busy: false, progress: "", error: e.message||String(e), success: ""});
    }
  }
  if(!authReady){
    return <main className="wrap"><div style={{maxWidth:460, margin:"80px auto", textAlign:"center", color:"var(--muted)"}}>Loading…</div></main>;
  }
  // Say the build is misconfigured rather than letting every request fail against
  // a localhost that isn't there. Checked before the sign-in card, because
  // signing in is the first thing that would fail.
  if(API_MISCONFIGURED){
    return (
      <main className="wrap">
        <div className="hero-copy" style={{maxWidth:460, margin:"80px auto"}}>
          <h1 style={{margin:0}}>Backend <em>not configured</em></h1>
          <p>{API_MISCONFIGURED_MESSAGE}</p>
          <div className="error" style={{marginTop:12}}>NEXT_PUBLIC_API_URL is unset in this production build.</div>
        </div>
      </main>
    );
  }
  // If token exists but data not yet fetched, show restoring, not sign-in — fixes flash on back nav
  if(!data){
    if(token){
      return <main className="wrap"><div style={{maxWidth:460, margin:"80px auto", textAlign:"center", color:"var(--muted)"}}>
        <div>Restoring session…</div>
        {restoreTimeout ? <div style={{marginTop:14, fontSize:12, color:"var(--coral)"}}>Taking too long — database may be waking up (can take up to 30 sec after idle). <button className="pill dark" style={{marginLeft:8}} onClick={()=>{ setRestoreTimeout(false); load(); }}>Retry</button> <button className="pill dark" style={{marginLeft:6}} onClick={()=>{ try{sessionStorage.clear()}catch{}; setToken(""); setData(null); }}>Sign in again</button></div> : <div style={{marginTop:8, fontSize:11, color:"var(--muted)", opacity:0.7}}>Connecting — may take up to 30 sec if database was idle</div>}
        {error ? <div className="error" style={{marginTop:12}}>{error}</div> : null}
      </div></main>;
    }
    return (
      <main className="wrap">
        <div style={{textAlign:"center", margin:"28px 0 10px", fontFamily:"Instrument Serif, serif", fontSize:14, letterSpacing:"0.18em", textTransform:"uppercase", color:"var(--brass)"}}>Aether Audio · PRAMAN</div>
        <div className="hero-copy" style={{maxWidth:460, margin:"0 auto"}}>
          <h1 style={{margin:0}}>Sign in to <em>PRAMAN</em></h1>
          <p>One account per store. Approve holds, inspect every AI sale, and verify the hash chain.</p>
          <form onSubmit={(e)=>{e.preventDefault(); handleAuth();}}>
          <div className="field" style={{marginTop:16}}><label htmlFor="email">Work email</label><input id="email" placeholder="you@yourstore.com" value={email} onChange={e=>setEmail(e.target.value)} autoComplete="email" autoCapitalize="off" spellCheck={false} required aria-required="true" /></div>
          <div className="field"><label htmlFor="password">Password</label><input id="password" type="password" placeholder="••••••••" value={password} onChange={e=>setPassword(e.target.value)} autoComplete={mode==="signup"?"new-password":"current-password"} required aria-required="true" /></div>
          <div className="field"><label htmlFor="storeId">Store ID</label><input id="storeId" placeholder="e.g. gada-electronics (leave blank for default)" value={storeId} onChange={e=>setStoreId(e.target.value.trim().toLowerCase())} autoComplete="off" spellCheck={false} aria-label="Store ID" /><div className="muted" style={{fontSize:10, marginTop:4, opacity:0.6}}>One account per store. Store is determined by your signup, not selectable from a list.</div></div>
          </form>
          <div style={{display:"flex", gap:8, marginTop:14}}>
            <button className="pill approve" style={{flex:1, justifyContent:"center", display:"flex", background:"var(--brass)", color:"#1A1400", borderColor:"var(--brass)", fontWeight:600}} onClick={handleAuth}>{busy?"…": mode==="signup"?"Create account":"Sign in"}</button>
            <button className="pill dark" onClick={()=>setMode(mode==="signup"?"signin":"signup")}>{mode==="signup"?"Have account? Sign in":"Create account"}</button>
          </div>
          <div className="muted" style={{fontSize:10, marginTop:12, textAlign:"center", opacity:0.5}}>Secure sign-in · tokens rotate on each login · demo accounts disabled</div>
          {error? <div className="error" style={{marginTop:10}}>{error}</div>: null}
        </div>
      </main>
    );
  }
  const m=data.metrics;
  const hasHolds = data.approvals.pending_count>0;
  const activeConn = storesData?.connected?.find(c => c.store_id === storeId) || storesData?.connected?.[0];
  const currentSkuCount = storesData?.catalog_counts?.[storeId] ?? (data?.metrics?.catalog_skus ?? 0);
  return (
    <>
      <div className={`banner ${data.mode.value}`}>{data.mode.value==="shadow" ? "Demo mode — no real money moves" : "Live — payments are real (test mode)"}</div>
      <nav className="nav">
        <div className="nav-brand">PRAMAN <i>· {activeConn ? (activeConn.domain || activeConn.platform) : storeId}</i></div>
        <div className="nav-meta">Merchant console</div>
        <div style={{display:"flex", gap:4, background:"var(--ink)", padding:"3px 4px", borderRadius:8, border:"1px solid var(--line)"}}>
          <button
            className={`pill ${activeTab==="overview"?"approve":"dark"}`}
            style={{fontSize:12, padding:"5px 12px", borderRadius:6, cursor:"pointer", border: activeTab==="overview" ? "1px solid var(--brass)" : "1px solid transparent"}}
            onClick={()=>setActiveTab("overview")}
          >
            Overview
          </button>
          <button
            className={`pill ${activeTab==="catalog"?"approve":"dark"}`}
            style={{fontSize:12, padding:"5px 12px", borderRadius:6, cursor:"pointer", border: activeTab==="catalog" ? "1px solid var(--brass)" : "1px solid transparent"}}
            onClick={()=>{ setActiveTab("catalog"); loadCatalog(catalogSearch, catalogCat); }}
          >
            Catalog <span style={{opacity:0.85, fontSize:11, marginLeft:4}}>({currentSkuCount})</span>
          </button>
        </div>
        <div className="store-picker">
          {activeConn ? (
            <div style={{display:"flex", alignItems:"center", gap:6, padding:"4px 10px", background:"rgba(46,196,165,0.12)", border:"1px solid rgba(46,196,165,0.3)", borderRadius:8, fontSize:12}}>
              <span style={{width:8, height:8, borderRadius:"50%", background:"var(--teal)", boxShadow:"0 0 6px var(--teal)", display:"inline-block"}} />
              <strong style={{color:"var(--teal)", textTransform:"capitalize"}}>{activeConn.platform}:</strong>
              <span style={{color:"var(--text)", fontFamily:"JetBrains Mono, monospace"}}>{activeConn.domain || activeConn.url || storeId}</span>
              <span style={{color:"var(--brass)", fontWeight:600}}>({currentSkuCount} SKUs)</span>
            </div>
          ) : (
            <div style={{display:"flex", alignItems:"center", gap:6, padding:"4px 10px", background:"rgba(255,255,255,0.04)", border:"1px solid var(--line)", borderRadius:8, fontSize:12, color:"var(--muted)"}}>
              <span style={{width:8, height:8, borderRadius:"50%", background:"var(--muted)", opacity:0.4, display:"inline-block"}} />
              <span>No store connected</span>
            </div>
          )}
          <select value={storeId} onChange={e=> setStoreId(e.target.value)}>{stores.map(s=><option key={s} value={s}>{s}</option>)}</select>
          <button onClick={()=>{ setConnectForm(f=>({...f, domain: activeConn?.domain || f.domain})); setConnectOpen(true); }}>
            {activeConn ? "Sync store" : "Connect store"}
          </button>
          <button onClick={()=>setShowActivity(!showActivity)}>{showActivity?"Hide settings":"Settings"}</button>
          <button onClick={async()=>{ try{ await fetch(`${API}/auth/signout`, {method:"POST", headers: {...headers()}});}catch{}; try{sessionStorage.clear()}catch{}; setData(null); setToken(""); }}>Sign out</button>
        </div>
      </nav>
      <main className="wrap">
        <div className="panel" style={{marginBottom:14, padding:"14px 18px", display:"flex", justifyContent:"space-between", alignItems:"center", flexWrap:"wrap", gap:12, borderColor: activeConn ? "rgba(46,196,165,0.25)" : "var(--line)", background: activeConn ? "linear-gradient(180deg, rgba(46,196,165,0.06), transparent), var(--graphite)" : "var(--graphite)"}}>
          <div style={{display:"flex", alignItems:"center", gap:14}}>
            <div style={{fontSize:26}}>{activeConn ? "🛍️" : "📦"}</div>
            <div>
              <div style={{display:"flex", alignItems:"center", gap:8}}>
                <span style={{fontFamily:"JetBrains Mono, monospace", fontSize:11, letterSpacing:"0.1em", textTransform:"uppercase", color: activeConn ? "var(--teal)" : "var(--muted)", fontWeight:600}}>
                  {activeConn ? `● Connected to ${activeConn.platform.toUpperCase()}` : "○ Demo Catalog Active"}
                </span>
                {activeConn?.connected_at ? <span className="muted" style={{fontSize:11}}>· Synced {fmtDate(activeConn.connected_at)}</span> : null}
              </div>
              <h3 style={{margin:"2px 0 0", fontSize:16, color:"var(--text)"}}>
                {activeConn?.domain || (activeConn ? activeConn.platform : "Aether Demo Catalog")}
              </h3>
              <p className="muted" style={{margin:"2px 0 0", fontSize:12}}>
                <strong style={{color:"var(--text)"}}>{currentSkuCount} products</strong> active in catalog · Protected by PRAMAN 10 policy bounds
              </p>
            </div>
          </div>
          <button className="pill dark" onClick={()=>{ setConnectForm(f=>({...f, domain: activeConn?.domain || ""})); setConnectOpen(true); }}>
            {activeConn ? "Sync Again / Reconnect" : "Connect Store"}
          </button>
        </div>
        {activeTab === "catalog" ? (
          <>
            <div className="stat-grid" style={{marginBottom:14}}>
              <div className="stat">
                <div className="k">Total Products</div>
                <div className="v">{catalogData?.total_count ?? currentSkuCount}</div>
                <div className="muted" style={{fontSize:11, marginTop:4}}>Active in store catalog</div>
              </div>
              <div className="stat">
                <div className="k">Total Inventory Units</div>
                <div className="v">{catalogData?.total_stock ? catalogData.total_stock.toLocaleString() : "—"}</div>
                <div className="muted" style={{fontSize:11, marginTop:4}}>Available stock across all items</div>
              </div>
              <div className="stat">
                <div className="k">PRAMAN Protection</div>
                <div className="v" style={{fontSize:20, color:"var(--teal)"}}>10 Bounds Active</div>
                <div className="muted" style={{fontSize:11, marginTop:4}}>Floor price &amp; 12% discount caps enforced</div>
              </div>
            </div>

            <div className="panel" style={{marginBottom:14, padding:"14px 16px"}}>
              <div style={{display:"flex", gap:12, flexWrap:"wrap", alignItems:"center", justifyContent:"space-between"}}>
                <div style={{display:"flex", gap:10, flex:1, minWidth:260}}>
                  <input
                    style={{
                      flex:1, background:"var(--ink)", color:"var(--text)", border:"1px solid var(--line)",
                      borderRadius:8, padding:"8px 12px", fontSize:13, fontFamily:"Inter, sans-serif"
                    }}
                    placeholder="🔍 Search by product title, brand, or SKU (e.g. Acer, Cable, GE-)..."
                    value={catalogSearch}
                    onChange={e => {
                      setCatalogSearch(e.target.value);
                      loadCatalog(e.target.value, catalogCat);
                    }}
                  />
                  <select
                    style={{
                      background:"var(--ink)", color:"var(--text)", border:"1px solid var(--line)",
                      borderRadius:8, padding:"8px 12px", fontSize:13, fontFamily:"JetBrains Mono, monospace"
                    }}
                    value={catalogCat}
                    onChange={e => {
                      setCatalogCat(e.target.value);
                      loadCatalog(catalogSearch, e.target.value);
                    }}
                  >
                    <option value="all">All Categories ({catalogData?.categories?.length || 0})</option>
                    {(catalogData?.categories || []).map(c => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </select>
                </div>
                <div style={{display:"flex", gap:8}}>
                  <button className="pill dark" onClick={() => loadCatalog(catalogSearch, catalogCat)}>
                    {catalogBusy ? "Refreshing..." : "Refresh"}
                  </button>
                  <button className="pill approve" onClick={() => setConnectOpen(true)}>
                    + Import More
                  </button>
                </div>
              </div>
            </div>

            <div className="panel">
              <div style={{display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:8}}>
                <h2 style={{margin:0}}>Product Catalog</h2>
                <span className="muted" style={{fontFamily:"JetBrains Mono, monospace", fontSize:11}}>
                  Showing {catalogData?.products?.length ?? 0} of {catalogData?.total_count ?? currentSkuCount} items
                </span>
              </div>
              <p className="muted" style={{fontSize:12, margin:"0 0 14px"}}>
                Tap any product row to view its exact PRAMAN pricing bounds, margins, and attach recommendations.
              </p>

              <div className="orders-wrap">
                <table className="orders-table" role="table" aria-label="Product Catalog">
                  <thead>
                    <tr>
                      <th>SKU</th>
                      <th>Product Title</th>
                      <th>Category</th>
                      <th>Retail Price</th>
                      <th>Floor Price</th>
                      <th>Max Discount</th>
                      <th>Stock</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {!catalogData && catalogBusy ? (
                      <tr><td colSpan={8} style={{textAlign:"center", padding:30, color:"var(--muted)"}}>Loading catalog products…</td></tr>
                    ) : !catalogData?.products?.length ? (
                      <tr><td colSpan={8} style={{textAlign:"center", padding:30, color:"var(--muted)"}}>No products found matching your search.</td></tr>
                    ) : (
                      catalogData.products.map(p => {
                        const isLow = p.stock_qty <= 10 && p.stock_qty > 0;
                        const isOut = p.stock_qty <= 0;
                        return (
                          <tr
                            key={p.sku}
                            style={{cursor:"pointer"}}
                            onClick={() => setSelectedProduct(p)}
                          >
                            <td>
                              <span className="mono" style={{fontSize:12, color:"var(--brass)"}}>{p.sku}</span>
                            </td>
                            <td style={{maxWidth:280}}>
                              <strong style={{color:"var(--text)"}}>{p.title}</strong>
                            </td>
                            <td>
                              <span style={{
                                display:"inline-block", padding:"2px 8px", borderRadius:6,
                                background:"rgba(255,255,255,0.06)", fontSize:11, color:"var(--muted)", textTransform:"capitalize"
                              }}>
                                {p.category || "general"}
                              </span>
                            </td>
                            <td>
                              <strong style={{fontFamily:"JetBrains Mono, monospace"}}>{money(p.list_price_inr)}</strong>
                            </td>
                            <td>
                              <span style={{fontFamily:"JetBrains Mono, monospace", color:"var(--teal)", fontSize:12}}>
                                {p.floor_price_inr ? money(p.floor_price_inr) : "—"}
                              </span>
                            </td>
                            <td>
                              <span style={{fontFamily:"JetBrains Mono, monospace", fontSize:12, color:"var(--amber)"}}>
                                {p.max_discount_pct ? `${p.max_discount_pct}%` : "—"}
                              </span>
                            </td>
                            <td>
                              <span style={{
                                display:"inline-flex", alignItems:"center", gap:5, padding:"2px 8px", borderRadius:6, fontSize:11,
                                background: isOut ? "rgba(224,90,51,0.15)" : isLow ? "rgba(232,184,75,0.15)" : "rgba(46,196,165,0.15)",
                                color: isOut ? "var(--coral)" : isLow ? "var(--amber)" : "var(--teal)",
                                fontWeight: 600
                              }}>
                                <span style={{
                                  width:6, height:6, borderRadius:"50%",
                                  background: isOut ? "var(--coral)" : isLow ? "var(--amber)" : "var(--teal)"
                                }} />
                                {isOut ? "Out of stock" : `${p.stock_qty} in stock`}
                              </span>
                            </td>
                            <td>
                              <span style={{color:"var(--teal)", fontSize:11, fontWeight:600}}>
                                {p.offerable ? "🟢 Active" : "⚪ Paused"}
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
          <>
            <div className="stat-grid">
              <div className="stat"><div className="k">Revenue today</div><div className="v">{money(m.revenue_inr)}</div><div className="muted" style={{fontSize:11, marginTop:4}}>{m.orders} orders · AOV {money(m.aov_inr)}</div></div>
              <div className="stat"><div className="k">Pending approvals</div><div className="v" style={{color: hasHolds?"var(--amber)":"var(--teal)"}}>{data.approvals.pending_count}</div><div className="muted" style={{fontSize:11, marginTop:4}}>{hasHolds ? "Needs your action — tap Approve" : "All clear"}</div></div>
              <div className="stat"><div className="k">Ledger</div><div className="v mono" style={{fontSize:14}}>{data.chain.intact===true? "Verified ✓" : data.chain.intact===false? "Broken" : "Checking..."} </div><div className="muted" style={{fontSize:11, marginTop:4}}>{data.chain.head_seq ?? "—"} events · <a href="/audit/verify" style={{color:"var(--brass)"}}>verify report</a></div></div>
            </div>
            {hasHolds ? (
              <div className="panel" style={{borderColor:"var(--amber)", marginBottom:14}}>
                <h2>Needs your approval — {data.approvals.pending_count} held</h2>
                <p className="muted" style={{fontSize:12, margin:"0 0 10px"}}>Over the auto-approve limit. Nothing auto-approves.</p>
                {data.approvals.queue.map(a=>(
                  <div className="approval" key={a.approval_id}>
                    <div style={{display:"flex", justifyContent:"space-between", alignItems:"baseline"}}>
                      <strong className="num" style={{fontSize:16}}>{money(a.amount_inr)}</strong>
                      <span className="muted" style={{fontFamily:"JetBrains Mono, monospace", fontSize:11}}>{a.order_id}</span>
                    </div>
                    <div className="muted" style={{fontSize:12, margin:"6px 0"}}>{a.note}</div>
                    <div style={{display:"flex", gap:6, flexWrap:"wrap", marginTop:8}}>
                      <button className="pill approve" onClick={()=>decide(a.approval_id,"approve")}>Approve</button>
                      <button className="pill reject" onClick={()=>decide(a.approval_id,"reject")}>Reject</button>
                      <input id={`cnt-${a.approval_id}`} className="counter-input" placeholder="Counter ₹" />
                      <button className="pill dark" onClick={()=>doCounter(a.approval_id)}>Send counter</button>
                    </div>
                  </div>
                ))}
              </div>
            ): null}
            <div className="panel">
              <div style={{display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:6}}>
                <h2 style={{margin:0}}>Recent orders</h2>
                <span className="muted" style={{fontFamily:"JetBrains Mono, monospace", fontSize:11}}>{storeId || "default"} · {orders?.count||0} orders</span>
              </div>
              <p className="muted" style={{fontSize:12, margin:"0 0 10px"}}>Tap an order to see why it was approved and its audit trail.</p>
              <div className="orders-wrap">
              <table className="orders-table" role="table" aria-label="Recent orders">
                <thead><tr><th>Order</th><th>Product</th><th>Amount</th><th>Status</th><th>Date</th></tr></thead>
                <tbody>
                  {orders === null ? (
                    <tr><td colSpan={5}><div className="skeleton skeleton-text" style={{height:40}} /></td></tr>
                  ) : (orders?.orders||[]).map(o=>(
                    <tr key={o.order_id} onClick={()=>openOrder(o.order_id)} onKeyDown={(e)=> e.key==="Enter" && openOrder(o.order_id)} tabIndex={0} role="button" aria-label={`Open order ${o.order_id}`}>
                      <td className="num" style={{fontSize:12}}>{o.order_id.slice(0,13)}…</td>
                      <td style={{maxWidth:260, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap"}}>{o.title_summary||o.offer_id}</td>
                      <td className="num" style={{textAlign:"right", fontVariantNumeric:"tabular-nums"}}>{money(o.amount_inr)}</td>
                      <td><span className={`state ${o.state}`}>{o.state==="CONFIRMED"?"Paid": o.state==="PENDING"?"Awaiting payment": o.state==="HELD"?"On hold": o.state}</span></td>
                      <td className="muted" style={{fontFamily:"JetBrains Mono, monospace", fontSize:11}}>{fmtDate(o.created_at)}</td>
                    </tr>
                  ))}
                  {orders && !(orders?.orders||[]).length? <tr><td colSpan={5}><div className="empty-state"><p>No orders yet.</p><p className="muted" style={{fontSize:12}}>Share your store link with a buyer agent or test with a Gada product in Shopify.</p></div></td></tr>: null}
                </tbody>
              </table>
              </div>
              {selectedOrder && drawerData? (
                <div className="drawer">
                  <div style={{display:"flex", justifyContent:"space-between", alignItems:"center"}}>
                    <h3 style={{margin:0}}>Order details — {selectedOrder} <a href={`/audit/${selectedOrder}`} style={{marginLeft:8, fontSize:11, color:"var(--brass)"}}>Open full report →</a></h3>
                    <button onClick={()=>{setSelectedOrder(null); setDrawerData(null);}} className="pill dark">Close</button>
                  </div>
                  {drawerData.order ? (
                    <div style={{display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(180px,1fr))", gap:10, marginTop:12}}>
                      <div style={{background:"var(--ink)", border:"1px solid var(--line)", borderRadius:8, padding:10}}><div className="k" style={{fontFamily:"JetBrains Mono, monospace", fontSize:10, letterSpacing:"0.1em", textTransform:"uppercase", color:"var(--muted)"}}>Order</div><div style={{fontSize:13, marginTop:4}}>{drawerData.order.order_id}</div><div className="muted" style={{fontSize:11}}>{fmtDate(drawerData.order.created_at)}</div></div>
                      <div style={{background:"var(--ink)", border:"1px solid var(--line)", borderRadius:8, padding:10}}><div className="k" style={{fontFamily:"JetBrains Mono, monospace", fontSize:10, letterSpacing:"0.1em", textTransform:"uppercase", color:"var(--muted)"}}>Product</div><div style={{fontSize:13, marginTop:4}}>{orders?.orders?.find(o=>o.order_id===selectedOrder)?.title_summary || drawerData.order.offer_id}</div><div className="muted" style={{fontSize:11}}>Gate T{drawerData.order.gate_tier} · {drawerData.order.policy_mode}</div></div>
                      <div style={{background:"var(--ink)", border:"1px solid var(--line)", borderRadius:8, padding:10}}><div className="k" style={{fontFamily:"JetBrains Mono, monospace", fontSize:10, letterSpacing:"0.1em", textTransform:"uppercase", color:"var(--muted)"}}>Amount · Status</div><div style={{fontSize:16}}>{money(drawerData.order.amount_inr)} <span className={`state ${drawerData.order.state}`} style={{marginLeft:6, verticalAlign:"middle"}}>{drawerData.order.state==="CONFIRMED"?"Paid": drawerData.order.state==="PENDING"?"Awaiting payment": drawerData.order.state==="HELD"?"On hold": drawerData.order.state}</span></div><div className="muted" style={{fontSize:11}}>{drawerData.order.razorpay_payment_id ? `Pay ${drawerData.order.razorpay_payment_id.slice(0,12)}…` : drawerData.order.razorpay_order_id ? `Razorpay ${drawerData.order.razorpay_order_id.slice(0,12)}…` : "No gateway yet"}</div></div>
                    </div>
                  ): null}
                  <div style={{marginTop:10, fontSize:11, color:"var(--muted)"}}>Why approved: <b style={{color:"var(--text)"}}>{drawerData.order?.amount_inr ? `Amount ${money(drawerData.order.amount_inr)}, gate T${drawerData.order.gate_tier}` : "See trail"}</b> · All steps hash-chained. <a href="/audit/verify" style={{color:"var(--brass)"}}>Verify</a></div>
                  <div className="timeline-vertical">
                    {(drawerData.trail||[]).slice(-6).map((e,idx)=>{
                      const h = humanEvent(e);
                      const dotCls = h.tone==="good"?"good": h.tone==="bad"?"bad": h.tone==="warn"?"warn":"neutral";
                      return (
                        <div key={idx} className="timeline-step">
                          <div className={`timeline-dot ${dotCls}`}/>
                          <div className="timeline-step-head">
                            <div className="timeline-step-title">{h.title}</div>
                            <div className="timeline-step-meta">{fmtDate(e.ts)}</div>
                          </div>
                          <div className="timeline-step-desc">{h.desc}</div>
                          {e.event==="payment.captured" && e.money_delta_inr? <div className="timeline-step-amount good">+{money(e.money_delta_inr)} captured</div>: null}
                          {e.event==="payment.intent" && e.money_delta_inr? <div className="timeline-step-amount warn">{money(e.money_delta_inr)} pending</div>: null}
                        </div>
                      );
                    })}
                  </div>
                  <details style={{marginTop:10}}><summary className="muted" style={{cursor:"pointer", fontSize:11}}>Show details</summary>
                    <div style={{display:"grid", gridTemplateColumns:"repeat(2,1fr)", gap:8, marginTop:8}}>
                      <div style={{background:"var(--ink)", border:"1px solid var(--line)", borderRadius:8, padding:8}}><div style={{fontSize:10, color:"#8B97A6", letterSpacing:"0.08em", textTransform:"uppercase"}}>Offer</div><div style={{fontSize:11, marginTop:2, wordBreak:"break-all"}}>{drawerData.order?.offer_id || "—"}</div></div>
                      <div style={{background:"var(--ink)", border:"1px solid var(--line)", borderRadius:8, padding:8}}><div style={{fontSize:10, color:"#8B97A6", letterSpacing:"0.08em", textTransform:"uppercase"}}>Gate</div><div style={{fontSize:11, marginTop:2}}>Tier {drawerData.order?.gate_tier} · {drawerData.order?.policy_mode}</div></div>
                      <div style={{background:"var(--ink)", border:"1px solid var(--line)", borderRadius:8, padding:8}}><div style={{fontSize:10, color:"#8B97A6", letterSpacing:"0.08em", textTransform:"uppercase"}}>Razorpay order</div><div style={{fontSize:11, marginTop:2, wordBreak:"break-all"}}>{drawerData.order?.razorpay_order_id || "Not created yet"}</div></div>
                      <div style={{background:"var(--ink)", border:"1px solid var(--line)", borderRadius:8, padding:8}}><div style={{fontSize:10, color:"#8B97A6", letterSpacing:"0.08em", textTransform:"uppercase"}}>Payment</div><div style={{fontSize:11, marginTop:2, wordBreak:"break-all"}}>{drawerData.order?.razorpay_payment_id || "Awaiting payment"}</div></div>
                    </div>
                  </details>
                </div>
              ): null}
            </div>
            <div style={{textAlign:"center", marginTop:12, display:"flex", gap:8, justifyContent:"center"}}>
              <button className="pill dark" style={{fontSize:11}} onClick={()=>setShowActivity(!showActivity)}>{showActivity?"Hide settings":"Policy & system activity"}</button>
              <a href="/audit/verify" className="pill dark" style={{fontSize:11, textDecoration:"none", display:"inline-block", padding:"7px 12px"}}>Verify ledger</a>
            </div>
            {showActivity? (
              <div className="grid" style={{marginTop:12}}>
                <div className="col-6 panel">
                  <h2>Recent activity — what happened</h2>
                  <ul className="feed" style={{maxHeight:260}}>
                    {(data.feed||[]).slice(0,8).map(e=>{
                      const h = humanEvent(e);
                      return (
                      <li key={e.seq} style={{display:"flex", gap:8, padding:"6px 0", borderBottom:"1px dashed rgba(255,255,255,0.06)"}}>
                        <span className="muted" style={{fontFamily:"JetBrains Mono, monospace", fontSize:10, minWidth:54}}>{fmtDate(e.ts)}</span>
                        <span style={{fontSize:12, flex:1, color: h.tone==="good"?"var(--teal)": h.tone==="bad"?"var(--coral)": h.tone==="warn"?"var(--amber)":"var(--text)"}}>{h.title}</span>
                      </li>
                      );
                    })}
                    {!(data.feed||[]).length? <li className="muted" style={{fontSize:12}}>No activity yet</li>: null}
                  </ul>
                </div>
                <div className="col-6 panel">
                  <h2>Policy — you can change these</h2>
                  {!policyDraft? <div className="muted">Loading…</div> : (
                    <>
                      <div className="field"><label>Max discount per item (%)</label><input type="number" value={policyDraft.item_discount_cap} onChange={e=>setPolicyDraft({...policyDraft, item_discount_cap: parseInt(e.target.value||"0",10)})} /></div>
                      <div className="field"><label>Max cart discount (%)</label><input type="number" value={policyDraft.cart_discount_cap} onChange={e=>setPolicyDraft({...policyDraft, cart_discount_cap: parseInt(e.target.value||"0",10)})} /></div>
                      <div className="field"><label>Daily discount budget (₹)</label><input type="number" value={policyDraft.daily_budget} onChange={e=>setPolicyDraft({...policyDraft, daily_budget: parseInt(e.target.value||"0",10)})} /></div>
                      <div className="field"><label>Auto-approve up to (₹)</label><input type="number" value={policyDraft.approval_limit} onChange={e=>setPolicyDraft({...policyDraft, approval_limit: parseInt(e.target.value||"0",10)})} /></div>
                      <button className="pill approve" style={{marginTop:8, background:"var(--brass)", color:"#1A1400", borderColor:"var(--brass)"}} onClick={handlePolicySave}>Save policy</button>
                      <p className="muted" style={{fontSize:11, marginTop:6}}>Saved per store, audited as <code>policy.updated</code> in ledger.</p>
                    </>
                  )}
                </div>
              </div>
            ): null}
          </>
        )}
        {selectedProduct ? (
          <div className="modal-backdrop" onClick={() => setSelectedProduct(null)}>
            <div className="modal" style={{maxWidth:560, width:"92vw"}} onClick={e => e.stopPropagation()}>
              <div style={{display:"flex", justifyContent:"space-between", alignItems:"flex-start", borderBottom:"1px solid var(--line)", paddingBottom:12}}>
                <div>
                  <span className="mono" style={{fontSize:12, color:"var(--brass)"}}>{selectedProduct.sku}</span>
                  <h3 style={{margin:"4px 0 0", fontSize:18, color:"var(--text)"}}>{selectedProduct.title}</h3>
                </div>
                <button className="pill dark" style={{padding:"4px 10px"}} onClick={() => setSelectedProduct(null)}>✕</button>
              </div>

              <div style={{display:"grid", gridTemplateColumns:"repeat(2, 1fr)", gap:10, marginTop:16}}>
                <div className="info-card">
                  <div className="info-card-label">Retail List Price</div>
                  <div className="info-card-value mono" style={{fontSize:18, fontWeight:700}}>{money(selectedProduct.list_price_inr)}</div>
                  <div className="info-card-sub">Store selling price</div>
                </div>
                <div className="info-card">
                  <div className="info-card-label">PRAMAN Floor Price</div>
                  <div className="info-card-value mono" style={{fontSize:18, fontWeight:700, color:"var(--teal)"}}>
                    {selectedProduct.floor_price_inr ? money(selectedProduct.floor_price_inr) : "—"}
                  </div>
                  <div className="info-card-sub">Bound 3: Autonomous AI floor</div>
                </div>
                <div className="info-card">
                  <div className="info-card-label">Max Discount Cap</div>
                  <div className="info-card-value mono" style={{fontSize:18, fontWeight:700, color:"var(--amber)"}}>
                    {selectedProduct.max_discount_pct ? `${selectedProduct.max_discount_pct}%` : "—"}
                  </div>
                  <div className="info-card-sub">Bound 1: Max discount per SKU</div>
                </div>
                <div className="info-card">
                  <div className="info-card-label">Inventory On Hand</div>
                  <div className="info-card-value mono" style={{fontSize:18, fontWeight:700}}>
                    {selectedProduct.stock_qty} units
                  </div>
                  <div className="info-card-sub">Category: {selectedProduct.category || "general"}</div>
                </div>
              </div>

              {selectedProduct.attach_candidates?.length ? (
                <div style={{marginTop:16}}>
                  <div className="info-card-label" style={{marginBottom:6}}>Recommended Attach Products (Upsell Candidates)</div>
                  <div style={{display:"flex", gap:6, flexWrap:"wrap"}}>
                    {selectedProduct.attach_candidates.map(att => (
                      <span key={att} className="pill dark" style={{fontSize:11, fontFamily:"JetBrains Mono, monospace"}}>{att}</span>
                    ))}
                  </div>
                </div>
              ) : null}

              <div style={{display:"flex", justifyContent:"flex-end", marginTop:20}}>
                <button className="pill approve" onClick={() => setSelectedProduct(null)}>Close</button>
              </div>
            </div>
          </div>
        ) : null}
        {toast? <div className="toast">{toast}</div>: null}
        {error? <div className="toast" style={{borderColor:"var(--coral)", color:"var(--coral)"}}>{error}</div>: null}
      </main>
      {connectOpen? (
        <div className="modal-backdrop" onClick={()=> !syncState.busy && setConnectOpen(false)}>
          <div className="modal" onClick={e=>e.stopPropagation()}>
            <h3>Connect your store</h3>
            <p className="muted" style={{margin:"6px 0 0", fontSize:12}}>Your catalog syncs in, PRAMAN applies the same 10 rules.</p>
            {syncState.busy ? (
              <div style={{padding:"32px 16px", textAlign:"center"}}>
                <div style={{width:36, height:36, border:"3px solid rgba(255,255,255,0.1)", borderTopColor:"var(--teal)", borderRadius:"50%", margin:"0 auto 16px", animation:"spin 0.8s linear infinite"}} />
                <h4 style={{margin:"0 0 8px", fontSize:16, color:"var(--text)"}}>Syncing Products...</h4>
                <p style={{margin:0, fontSize:13, color:"var(--teal)"}}>{syncState.progress}</p>
                <p className="muted" style={{fontSize:11, marginTop:12}}>Please keep this window open while your catalog is imported.</p>
              </div>
            ) : syncState.success ? (
              <div style={{padding:"32px 16px", textAlign:"center"}}>
                <div style={{fontSize:38, marginBottom:8}}>✅</div>
                <h4 style={{margin:"0 0 8px", fontSize:18, color:"var(--teal)"}}>Store Connected!</h4>
                <p style={{margin:0, fontSize:13, color:"var(--text)"}}>{syncState.success}</p>
              </div>
            ) : (
              <>
                <div className="cards">
                  {[{k:"shopify", t:"Shopify", d:"Domain + token"}, {k:"woocommerce", t:"WooCommerce", d:"URL + key/secret"}, {k:"custom", t:"Custom / CSV", d:"Paste rows"}].map(c=>(
                    <div key={c.k} className={`card ${connectKind===c.k?"active":""}`} onClick={()=>setConnectKind(c.k)}><h4>{c.t}</h4><p>{c.d}</p></div>
                  ))}
                </div>
                {connectKind==="shopify"? (<><div className="field"><label>Shopify domain</label><input placeholder="my-store.myshopify.com" value={connectForm.domain} onChange={e=>setConnectForm({...connectForm, domain:e.target.value})} /></div><div className="field"><label>Admin token</label><input type="password" placeholder="shpat_***" value={connectForm.token} onChange={e=>setConnectForm({...connectForm, token:e.target.value})} /></div></>): connectKind==="woocommerce"? (<><div className="field"><label>Store URL</label><input placeholder="https://myshop.in" value={connectForm.url} onChange={e=>setConnectForm({...connectForm, url:e.target.value})} /></div><div className="field"><label>Consumer Key</label><input placeholder="ck_..." value={connectForm.key} onChange={e=>setConnectForm({...connectForm, key:e.target.value})} /></div><div className="field"><label>Consumer Secret</label><input type="password" placeholder="cs_..." value={connectForm.secret} onChange={e=>setConnectForm({...connectForm, secret:e.target.value})} /></div></>): (<div className="field"><label>CSV — sku,title,price,stock</label><input placeholder="CSV-001,Test Item,999,10" value={connectForm.csv} onChange={e=>setConnectForm({...connectForm, csv:e.target.value})} /></div>)}
                {syncState.error ? (
                  <div style={{padding:"10px 14px", background:"rgba(224,90,51,0.15)", border:"1px solid var(--coral)", borderRadius:8, color:"var(--coral)", fontSize:12, marginTop:12}}>
                    {syncState.error}
                  </div>
                ) : null}
                <div style={{display:"flex", gap:8, justifyContent:"flex-end", marginTop:14}}>
                  <button className="pill dark" onClick={()=>setConnectOpen(false)}>Cancel</button>
                  <button className="pill approve" onClick={doConnect}>Import</button>
                </div>
              </>
            )}
          </div>
        </div>
      ): null}
    </>
  );
}
