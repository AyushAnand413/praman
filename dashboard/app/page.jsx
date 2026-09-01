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
  const [email,setEmail]=useState("merchant@aether.test");
  const [password,setPassword]=useState("praman123");
  const [storeId,setStoreId]=useState("default");
  const [stores,setStores]=useState(["default"]);
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
      if(r.status===401) throw new Error("invalid session — sign in again");
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
        if(s.ok){ const sj=await s.json(); if(!stale() && sj.stores?.length) setStores(sj.stores); }
        if(p.ok){ const pj=await p.json(); if(!stale()){ setPolicy(pj.policy); setPolicyDraft(pj.policy); } }
      }catch{}
    }catch(e){
      if(e?.name==="AbortError" || stale()) return;
      setError(e.message||String(e));
    }finally{ if(!stale()) setBusy(false); }
  },[token,storeId]);
  useEffect(()=>{ try{ const tk=sessionStorage.getItem("praman-token"); const ss=sessionStorage.getItem("store-id"); if(tk) setToken(tk); if(ss) setStoreId(ss);}catch{} finally{ setAuthReady(true); } },[]);
  useEffect(()=>{ if(!token) return; load(); const id=setInterval(()=>{ if(!document.hidden) load(); },6000); const onVisible=()=>{ if(!document.hidden) load(); }; document.addEventListener("visibilitychange", onVisible); return()=>{ clearInterval(id); document.removeEventListener("visibilitychange",onVisible); inflight.current?.abort(); }; },[token,storeId,load]);
  useEffect(()=>{
    if(token && !data){
      const t=setTimeout(()=>setRestoreTimeout(true), 8000);
      return ()=>clearTimeout(t);
    } else setRestoreTimeout(false);
  },[token,data]);
  async function handleAuth(){
    setError(""); setBusy(true);
    try{
      const path = mode==="signup"? "/auth/signup" : "/auth/signin";
      const r = await fetch(`${API}${path}`, { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({email, password, store_id: storeId})});
      const j = await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.detail?.message||`HTTP ${r.status}`);
      const tok=j.access_token; setToken(tok); try{ sessionStorage.setItem("praman-token",tok);}catch{}
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
    try{
      let r;
      if(connectKind==="shopify") r=await fetch(`${API}/merchant/v1/stores/connect/shopify`,{method:"POST", headers:{...headers(),"Content-Type":"application/json"}, body:JSON.stringify({domain:connectForm.domain, token:connectForm.token})});
      else if(connectKind==="woocommerce") r=await fetch(`${API}/merchant/v1/stores/connect/woocommerce`,{method:"POST", headers:{...headers(),"Content-Type":"application/json"}, body:JSON.stringify({url:connectForm.url, key:connectForm.key, secret:connectForm.secret})});
      else { const rows=connectForm.csv.split("\n").filter(Boolean).slice(0,5).map((line,idx)=>{ const [sku,title,price,stock]=line.split(","); return {sku:sku||`CSV-${idx}`, title:title||sku||`Item ${idx}`, list_price_inr:parseInt(price||"999",10), stock_qty:parseInt(stock||"10",10), category:"audio_accessories"}; }); r=await fetch(`${API}/merchant/v1/stores/connect/custom`,{method:"POST", headers:{...headers(),"Content-Type":"application/json"}, body:JSON.stringify({rows})}); }
      const j=await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.detail?.message||j.message||`HTTP ${r.status}`); setToast(`Imported ${j.imported||0} products`); setTimeout(()=>setToast(""),3000); setConnectOpen(false); load();
    }catch(e){ setError(e.message); }
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
        {restoreTimeout ? <div style={{marginTop:14, fontSize:12, color:"var(--coral)"}}>Taking too long — Neon may be waking (5 sec). <button className="pill dark" style={{marginLeft:8}} onClick={()=>{ setRestoreTimeout(false); load(); }}>Retry</button> <button className="pill dark" style={{marginLeft:6}} onClick={()=>{ try{sessionStorage.clear()}catch{}; setToken(""); setData(null); }}>Sign in again</button></div> : <div style={{marginTop:8, fontSize:11, color:"var(--muted)", opacity:0.7}}>First load after idle takes 3-5 sec</div>}
        {error ? <div className="error" style={{marginTop:12}}>{error}</div> : null}
      </div></main>;
    }
    return (
      <main className="wrap">
        <div style={{textAlign:"center", margin:"28px 0 10px", fontFamily:"Instrument Serif, serif", fontSize:14, letterSpacing:"0.18em", textTransform:"uppercase", color:"var(--brass)"}}>Aether Audio · PRAMAN</div>
        <div className="hero-copy" style={{maxWidth:460, margin:"0 auto"}}>
          <h1 style={{margin:0}}>Sign in to <em>PRAMAN</em></h1>
          <p>One account per store. Approve holds, inspect every AI sale, and verify the hash chain.</p>
          <div className="field" style={{marginTop:16}}><label>Work email</label><input placeholder="merchant@aether.test" value={email} onChange={e=>setEmail(e.target.value)} /></div>
          <div className="field"><label>Password</label><input type="password" placeholder="••••••••" value={password} onChange={e=>setPassword(e.target.value)} onKeyDown={e=>e.key==="Enter"&&handleAuth()} /></div>
          <div className="field"><label>Store</label><select value={storeId} onChange={e=>setStoreId(e.target.value)}>{stores.map(s=><option key={s} value={s}>{s}</option>)}</select></div>
          <div style={{display:"flex", gap:8, marginTop:14}}>
            <button className="pill approve" style={{flex:1, justifyContent:"center", display:"flex", background:"var(--brass)", color:"#1A1400", borderColor:"var(--brass)", fontWeight:600}} onClick={handleAuth}>{busy?"…": mode==="signup"?"Create account":"Sign in"}</button>
            <button className="pill dark" onClick={()=>setMode(mode==="signup"?"signin":"signup")}>{mode==="signup"?"Have account? Sign in":"Create account"}</button>
          </div>
          <div className="muted" style={{fontSize:11, marginTop:12, background:"var(--ink)", border:"1px solid var(--line)", borderRadius:8, padding:10, display:"flex", justifyContent:"space-between", alignItems:"center"}}>
            <span>Demo: <b>merchant@aether.test / praman123</b><br/><span style={{opacity:0.7}}>voltmart same password</span></span>
            <button className="pill dark" style={{fontSize:10}} onClick={()=>{setEmail("merchant@aether.test"); setPassword("praman123"); setStoreId("default");}}>Use demo</button>
          </div>
          {error? <div className="error" style={{marginTop:10}}>{error}</div>: null}
        </div>
      </main>
    );
  }
  const m=data.metrics;
  const hasHolds = data.approvals.pending_count>0;
  return (
    <>
      <div className={`banner ${data.mode.value}`}>{data.mode.value==="shadow" ? "Demo mode — no real money moves" : "Live — payments are real (test mode)"}</div>
      <nav className="nav">
        <div className="nav-brand">PRAMAN <i>· {storeId}</i></div>
        <div className="nav-meta">Merchant console</div>
        <div className="store-picker">
          <select value={storeId} onChange={e=> setStoreId(e.target.value)}>{stores.map(s=><option key={s} value={s}>{s}</option>)}</select>
          <button onClick={()=>setConnectOpen(true)}>Connect store</button>
          <button onClick={()=>setShowActivity(!showActivity)}>{showActivity?"Hide settings":"Settings"}</button>
          <button onClick={()=>{ try{sessionStorage.clear()}catch{}; setData(null); setToken(""); }}>Sign out</button>
        </div>
      </nav>
      <main className="wrap">
        <div style={{display:"grid", gridTemplateColumns:"repeat(3,1fr)", gap:12, marginBottom:14}}>
          <div className="stat"><div className="k">Revenue today</div><div className="v">{money(m.revenue_inr)}</div><div className="muted" style={{fontSize:11, marginTop:4}}>{m.orders} orders · AOV {money(m.aov_inr)}</div></div>
          <div className="stat"><div className="k">Pending approvals</div><div className="v" style={{color: hasHolds?"var(--amber)":"var(--teal)"}}>{data.approvals.pending_count}</div><div className="muted" style={{fontSize:11, marginTop:4}}>{hasHolds ? "Needs your action — tap Approve" : "All clear"}</div></div>
          <div className="stat"><div className="k">Ledger</div><div className="v mono" style={{fontSize:14}}>{data.chain.intact? "Verified ✓" : "Broken"}</div><div className="muted" style={{fontSize:11, marginTop:4}}>{data.chain.head_seq} events · <a href="/audit/verify" style={{color:"var(--brass)"}}>verify report</a></div></div>
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
            <span className="muted" style={{fontFamily:"JetBrains Mono, monospace", fontSize:11}}>{storeId} · {orders?.count||0} orders</span>
          </div>
          <p className="muted" style={{fontSize:12, margin:"0 0 10px"}}>Tap an order to see why it was approved and its audit trail.</p>
          <table className="orders-table">
            <thead><tr><th>Order</th><th>Product</th><th>Amount</th><th>Status</th><th>Date</th></tr></thead>
            <tbody>
              {(orders?.orders||[]).map(o=>(
                <tr key={o.order_id} onClick={()=>openOrder(o.order_id)}>
                  <td className="num" style={{fontSize:12}}>{o.order_id.slice(0,13)}…</td>
                  <td style={{maxWidth:260, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap"}}>{o.title_summary||o.offer_id}</td>
                  <td className="num">{money(o.amount_inr)}</td>
                  <td><span className={`state ${o.state}`}>{o.state==="CONFIRMED"?"Paid": o.state==="PENDING"?"Awaiting payment": o.state==="HELD"?"On hold": o.state}</span></td>
                  <td className="muted" style={{fontFamily:"JetBrains Mono, monospace", fontSize:11}}>{fmtDate(o.created_at)}</td>
                </tr>
              ))}
              {!(orders?.orders||[]).length? <tr><td colSpan={5} className="muted" style={{padding:14, textAlign:"center"}}>No orders yet. Try <code>python -m scripts.demo_buy</code> in terminal.</td></tr>: null}
            </tbody>
          </table>
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
        {toast? <div className="toast">{toast}</div>: null}
        {error? <div className="toast" style={{borderColor:"var(--coral)", color:"var(--coral)"}}>{error}</div>: null}
      </main>
      {connectOpen? (
        <div className="modal-backdrop" onClick={()=>setConnectOpen(false)}>
          <div className="modal" onClick={e=>e.stopPropagation()}>
            <h3>Connect your store</h3>
            <p className="muted" style={{margin:"6px 0 0", fontSize:12}}>Your catalog syncs in, PRAMAN applies the same 10 rules.</p>
            <div className="cards">
              {[{k:"shopify", t:"Shopify", d:"Domain + token"}, {k:"woocommerce", t:"WooCommerce", d:"URL + key/secret"}, {k:"custom", t:"Custom / CSV", d:"Paste rows"}].map(c=>(
                <div key={c.k} className={`card ${connectKind===c.k?"active":""}`} onClick={()=>setConnectKind(c.k)}><h4>{c.t}</h4><p>{c.d}</p></div>
              ))}
            </div>
            {connectKind==="shopify"? (<><div className="field"><label>Shopify domain</label><input placeholder="my-store.myshopify.com" value={connectForm.domain} onChange={e=>setConnectForm({...connectForm, domain:e.target.value})} /></div><div className="field"><label>Admin token</label><input type="password" placeholder="shpat_***" value={connectForm.token} onChange={e=>setConnectForm({...connectForm, token:e.target.value})} /></div></>): connectKind==="woocommerce"? (<><div className="field"><label>Store URL</label><input placeholder="https://myshop.in" value={connectForm.url} onChange={e=>setConnectForm({...connectForm, url:e.target.value})} /></div><div className="field"><label>Consumer Key</label><input placeholder="ck_..." value={connectForm.key} onChange={e=>setConnectForm({...connectForm, key:e.target.value})} /></div><div className="field"><label>Consumer Secret</label><input type="password" placeholder="cs_..." value={connectForm.secret} onChange={e=>setConnectForm({...connectForm, secret:e.target.value})} /></div></>): (<div className="field"><label>CSV — sku,title,price,stock</label><input placeholder="CSV-001,Test Item,999,10" value={connectForm.csv} onChange={e=>setConnectForm({...connectForm, csv:e.target.value})} /></div>)}
            <div style={{display:"flex", gap:8, justifyContent:"flex-end", marginTop:12}}>
              <button className="pill dark" onClick={()=>setConnectOpen(false)}>Cancel</button>
              <button className="pill approve" onClick={doConnect}>Import</button>
            </div>
          </div>
        </div>
      ): null}
    </>
  );
}
