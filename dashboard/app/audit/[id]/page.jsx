"use client";
import { useEffect, useState } from "react";
import "../../globals.css";
const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
function money(n){ return n==null? "—": `\u20B9${Number(n).toLocaleString("en-IN")}`; }
function fmt(ts){ if(!ts) return ""; try{ return new Date(ts).toLocaleString("en-IN",{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"});}catch{ return ts.slice(0,19).replace("T"," ");} }
function humanStep(e){
  const r = e.reason || "";
  const ev=e.event;
  if(ev==="payment.intent") return { title: "Payment intent created", desc: `Intent to charge ${money(e.money_delta_inr)} — not yet captured`, tone:"warn" };
  if(ev==="razorpay.order.created") return { title: "Gateway order created", desc: "Razorpay order linked — awaiting card confirmation", tone:"warn" };
  if(ev==="payment.captured") return { title: "Payment captured", desc: `Captured ${money(e.money_delta_inr)} — funds moved`, tone:"good" };
  if(ev==="payment.failed"||ev==="payment.declined") return { title:"Payment failed", desc:r.slice(0,90)||"Card declined or network issue", tone:"bad"};
  if(ev==="order.held_for_approval") return { title:"Held for approval", desc: r || "Over auto-approve limit — needs you", tone:"warn"};
  if(ev==="offer.request") return { title:"Offer requested", desc: "Buyer asked for a price", tone:"" };
  if(ev==="offer.issued") return { title:"Offer issued", desc: r || "Bounded and signed offer sent to buyer", tone:"good"};
  if(ev==="offer.refused") return { title:"Offer refused", desc:r.slice(0,80)||"Bounds rejected this proposal", tone:"bad"};
  if(ev==="proposal.emitted") return { title:"AI proposal generated", desc: "Vyapaari suggested items and discounts", tone:""};
  if(ev==="catalog.query") return { title:"Catalog browsed", desc:"Buyer searched products", tone:""};
  if(ev==="saga.compensation_triggered") return { title:"Oversell compensated", desc:"Auto-refund triggered for out-of-stock", tone:"bad"};
  if(ev==="checkout.rejected") return { title:"Checkout rejected", desc:r.slice(0,80)||"Bounds refused this purchase", tone:"bad"};
  if(ev==="mandate.accepted") return { title:"Mandate verified", desc:"Signed agent mandate accepted", tone:"good"};
  if(ev==="policy.updated") return { title:"Policy updated", desc: r || "Store policy settings changed", tone:""};
  if(ev==="ledger.genesis") return { title:"Ledger started", desc:"Hash chain genesis entry created", tone:"good"};
  if(ev==="razorpay.refund") return { title:"Refund processed", desc:r.slice(0,80)||"Funds returned to buyer", tone:"bad"};
  if(ev==="offer.evaluated") return { title:"Offer evaluated", desc:r.slice(0,80)||"Bounds checked a proposal", tone:""};
  return { title: ev?.replace(/\./g," · ") || "Event", desc: r.slice(0,90), tone:""};
}
export default function AuditPage({ params }){
  const id = params?.id || "";
  const [data,setData]=useState(null);
  const [err,setErr]=useState("");
  useEffect(()=>{
    if(!id) return;
    fetch(`${API}/audit/${id}`, {cache:"no-store"}).then(async r=>{
      const j=await r.json(); if(!r.ok) throw new Error(j.detail||`HTTP ${r.status}`); setData(j);
    }).catch(e=>setErr(String(e)));
  },[id]);
  if(err) return <main style={{minHeight:"100vh", background:"#0A1014", color:"#E6E9EF", padding:32}}><a href="/" style={{color:"#C8A96A"}}>← Back to console</a><p style={{color:"#E05A33", marginTop:16}}>Error: {err}</p></main>;
  if(!data) return <main style={{minHeight:"100vh", background:"#0A1014", color:"#E6E9EF", padding:32}}>Loading {id}…</main>;
  const entries = data.entries || (data.seq ? [data] : []);
  const captured = entries.find(e=> e.event==="payment.captured") || entries.find(e=> e.event==="payment.intent");
  const total = captured ? captured.money_delta_inr : (data.money_delta_inr ?? entries.reduce((s,e)=>s+(e.money_delta_inr||0),0));
  const status = entries.find(e=> e.event==="payment.captured") ? "Paid" : entries.find(e=> e.event==="order.held_for_approval") ? "On hold" : entries.find(e=> e.event==="payment.intent") ? "Awaiting payment" : "In progress";
  const statusColor = status==="Paid" ? "var(--teal)" : status==="On hold" ? "var(--amber)" : "var(--muted)";
  return (
    <main style={{minHeight:"100vh", background:"#0A1014", color:"#E6E9EF"}}>
      <div style={{maxWidth:760, margin:"0 auto", padding:"22px 16px 40px"}}>
        <a href="/" style={{color:"#C8A96A", fontSize:12, fontFamily:"JetBrains Mono, monospace"}}>← Back to console</a>
        <div style={{display:"flex", justifyContent:"space-between", alignItems:"baseline", marginTop:12, flexWrap:"wrap", gap:8}}>
          <h1 style={{fontFamily:"Instrument Serif, serif", fontSize:26, margin:0, fontWeight:400}}>Audit report</h1>
          <span style={{fontFamily:"JetBrains Mono, monospace", fontSize:11, color:"#8B97A6"}}>{id}</span>
        </div>
        <div style={{display:"grid", gridTemplateColumns:"repeat(3,1fr)", gap:10, marginTop:14}}>
          <div style={{background:"#131A20", border:"1px solid #1E2A33", borderRadius:12, padding:12}}><div style={{fontFamily:"JetBrains Mono, monospace", fontSize:10, letterSpacing:"0.1em", textTransform:"uppercase", color:"#8B97A6"}}>Status</div><div style={{fontSize:18, marginTop:4, color:statusColor}}>{status}</div></div>
          <div style={{background:"#131A20", border:"1px solid #1E2A33", borderRadius:12, padding:12}}><div style={{fontFamily:"JetBrains Mono, monospace", fontSize:10, letterSpacing:"0.1em", textTransform:"uppercase", color:"#8B97A6"}}>Amount</div><div style={{fontSize:18, marginTop:4}}>{money(total||entries[0]?.payload?.amount_inr)}</div></div>
          <details style={{background:"#131A20", border:"1px solid #1E2A33", borderRadius:12, padding:12, cursor:"pointer"}}>
            <summary style={{listStyle:"none"}}><div style={{fontFamily:"JetBrains Mono, monospace", fontSize:10, letterSpacing:"0.1em", textTransform:"uppercase", color:"#8B97A6"}}>Audit trail</div><div style={{fontSize:18, marginTop:4}}>{entries.length} verified {entries.length===1?"step":"steps"}</div><div style={{fontSize:11, color:"#C8A96A", marginTop:2}}>Tap to see what was checked ▾</div></summary>
            <ul style={{margin:"8px 0 0", paddingLeft:16, fontSize:11, color:"#8B97A6", lineHeight:1.7}}>
              <li>Buyer request validated against catalog</li>
              <li>Price checked against floor and discount limits</li>
              <li>Gate tier assigned and mandate verified if needed</li>
              <li>Stock reserved, then payment captured and committed</li>
              <li>All steps hash-chained from genesis — any edit breaks the chain</li>
            </ul>
          </details>
        </div>
        <div style={{marginTop:14, display:"flex", gap:8, flexWrap:"wrap"}}>
          <a href="/audit/verify" style={{fontSize:11, color:"#2EC4A5", border:"1px solid #1E2A33", borderRadius:999, padding:"6px 10px", textDecoration:"none", background:"#131A20"}}>Verify chain ✓</a>
          <span style={{fontSize:11, color:"#8B97A6", padding:"6px 0"}}>Each step is hash-chained — tampering breaks verification.</span>
        </div>
        <div style={{marginTop:18, position:"relative", paddingLeft:28}}>
          <div style={{position:"absolute", left:11, top:8, bottom:8, width:2, background:"#1E2A33", borderRadius:2}}/>
          {entries.map((e,idx)=>{
            const h=humanStep(e);
            const dotColor = h.tone==="good"? "#2EC4A5": h.tone==="bad"? "#E05A33": h.tone==="warn"? "#E8B84B": "#8B97A6";
            return (
              <div key={e.seq} style={{position:"relative", marginBottom:14, background:"#131A20", border:"1px solid #1E2A33", borderRadius:12, padding:"12px 12px 10px"}}>
                <div style={{position:"absolute", left:-23, top:14, width:10, height:10, borderRadius:"50%", background: dotColor, boxShadow:`0 0 8px ${dotColor}55`, border:"2px solid #0A1014"}}/>
                <div style={{display:"flex", justifyContent:"space-between", gap:8, alignItems:"baseline"}}>
                  <div style={{fontSize:13, fontWeight:600}}>{idx+1}. {h.title}</div>
                  <div style={{fontFamily:"JetBrains Mono, monospace", fontSize:10, color:"#8B97A6"}}>Step {idx+1} of {entries.length} · {fmt(e.ts)}</div>
                </div>
                <div style={{fontSize:12, color:"#8B97A6", marginTop:4, lineHeight:1.5}}>{h.desc}</div>
                {e.event==="payment.captured" && e.money_delta_inr? <div style={{fontFamily:"JetBrains Mono, monospace", fontSize:11, marginTop:4, color:"#2EC4A5"}}>+{money(e.money_delta_inr)} captured</div>: null}
                {e.event==="payment.intent" && e.money_delta_inr? <div style={{fontFamily:"JetBrains Mono, monospace", fontSize:11, marginTop:4, color:"#8B97A6"}}>{money(e.money_delta_inr)} pending</div>: null}
                <details style={{marginTop:6}}><summary style={{fontSize:11, color:"#8B97A6", cursor:"pointer"}}>Show details</summary>
                <div style={{display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(150px,1fr))", gap:8, marginTop:8}}>
                  <div style={{background:"#0A1014", border:"1px solid #1E2A33", borderRadius:8, padding:8}}><div style={{fontSize:10, color:"#8B97A6", letterSpacing:"0.08em", textTransform:"uppercase"}}>Agent</div><div style={{fontSize:11, marginTop:2, wordBreak:"break-all"}}>{e.payload?.agent_id || e.actor || "—"}</div></div>
                  <div style={{background:"#0A1014", border:"1px solid #1E2A33", borderRadius:8, padding:8}}><div style={{fontSize:10, color:"#8B97A6", letterSpacing:"0.08em", textTransform:"uppercase"}}>Product / Amount</div><div style={{fontSize:11, marginTop:2}}>{e.payload?.base_sku || e.payload?.sku || "—"} · {e.payload?.amount_inr ? money(e.payload.amount_inr) : e.money_delta_inr ? money(e.money_delta_inr) : "—"}</div></div>
                  <div style={{background:"#0A1014", border:"1px solid #1E2A33", borderRadius:8, padding:8}}><div style={{fontSize:10, color:"#8B97A6", letterSpacing:"0.08em", textTransform:"uppercase"}}>Gate</div><div style={{fontSize:11, marginTop:2}}>{e.payload?.gate ? `Tier ${e.payload.gate.gate_tier} — ${e.payload.gate.tier_name||""}` : e.event.includes("held") ? "Needs approval" : "—"}</div></div>
                  <div style={{background:"#0A1014", border:"1px solid #1E2A33", borderRadius:8, padding:8}}><div style={{fontSize:10, color:"#8B97A6", letterSpacing:"0.08em", textTransform:"uppercase"}}>Time</div><div style={{fontSize:11, marginTop:2}}>{fmt(e.ts)}</div></div>
                </div>
                </details>
              </div>
            );
          })}
        </div>
        <p style={{fontSize:11, color:"#8B97A6", marginTop:14}}>Order {id} · <a href="/audit/verify" style={{color:"#C8A96A"}}>Verify chain</a> · <a href={`${API}/audit/${id}`} target="_blank" style={{color:"var(--muted)", textDecoration:"underline"}}>API (debug)</a></p>
      </div>
    </main>
  );
}
