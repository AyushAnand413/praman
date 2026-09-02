"use client";
import { useEffect, useState } from "react";
import "../../globals.css";
import { API } from "../../config";
export default function VerifyPage(){
  const [data,setData]=useState(null);
  const [err,setErr]=useState("");
  const [checkedAt,setCheckedAt]=useState(null);
  async function load(){
    try{
      const r=await fetch(`${API}/audit/verify`, {cache:"no-store"});
      const j=await r.json(); setData(j); setErr(""); setCheckedAt(new Date());
    }catch(e){ setErr(String(e)); }
  }
  useEffect(()=>{ load(); },[]);
  if(err) return <main style={{minHeight:"100vh", background:"#0A1014", color:"#E6E9EF", padding:32}}><a href="/" style={{color:"#C8A96A"}}>← Back</a><p style={{color:"#E05A33"}}>Error: {err} — is API at {API} running?</p></main>;
  if(!data) return <main style={{minHeight:"100vh", background:"#0A1014", color:"#E6E9EF", padding:32}}>Verifying chain…</main>;
  const intact=data.intact;
  return (
    <main style={{minHeight:"100vh", background:"#0A1014", color:"#E6E9EF"}}>
      <div style={{maxWidth:640, margin:"0 auto", padding:"28px 16px"}}>
        <a href="/" style={{color:"#C8A96A", fontSize:12, fontFamily:"JetBrains Mono, monospace"}}>← Back to console</a>
        <h1 style={{fontFamily:"Instrument Serif, serif", fontSize:26, margin:"12px 0 6px", fontWeight:400}}>Ledger verification</h1>
        <p style={{color:"#8B97A6", fontSize:13, lineHeight:1.5}}>Recomputes every entry hash from genesis. A tampered row breaks the chain and reports where.</p>
        <div style={{display:"grid", gridTemplateColumns:"repeat(3,1fr)", gap:10, marginTop:14}}>
          <div className="info-card"><div className="info-card-label">Chain status</div><div className="info-card-value" style={{fontSize:16, color: intact?"#2EC4A5":"#E05A33"}}>{intact ? "Intact ✓" : "Broken ✗"}</div></div>
          <div className="info-card"><div className="info-card-label">Head sequence</div><div className="info-card-value" style={{fontSize:16}}>#{data.head_seq ?? "—"}</div><div className="info-card-sub">{data.head_seq ? `${data.head_seq} events verified` : "No events"}</div></div>
          <div className="info-card"><div className="info-card-label">Last checked</div><div className="info-card-value" style={{fontSize:14}}>{checkedAt ? checkedAt.toLocaleTimeString("en-IN",{hour:"2-digit",minute:"2-digit",second:"2-digit"}) : "—"}</div><div className="info-card-sub">{checkedAt ? checkedAt.toLocaleDateString("en-IN",{day:"2-digit",month:"short",year:"numeric"}) : ""}</div></div>
        </div>
        <div style={{background: intact?"#1A3A35":"#3A1E16", border:`1px solid ${intact?"#2EC4A5":"#E05A33"}`, borderRadius:12, padding:16, marginTop:16}}>
          <div style={{fontFamily:"JetBrains Mono, monospace", fontSize:13, color: intact?"#2EC4A5":"#E05A33"}}>
            {intact ? `Chain intact \u2713 — all ${data.head_seq ?? "?"} entries verified from genesis` : `Chain BROKEN at entry #${data.broken_at} — hash mismatch detected`}
          </div>
          <div style={{fontSize:11, color:"#8B97A6", marginTop:6}}>Tamper-evidence, not tamper-proof — detection, not prevention. External anchor: <code>python -m scripts.anchor_chain --verify</code></div>
        </div>
        <button onClick={load} style={{marginTop:14, padding:"8px 14px", borderRadius:999, border:"1px solid #1E2A33", background:"#131A20", color:"#E6E9EF", cursor:"pointer", fontFamily:"JetBrains Mono, monospace", fontSize:12}}>Re-verify</button>
        <details style={{marginTop:14}}><summary style={{cursor:"pointer", fontSize:11, color:"#8B97A6"}}>Show technical details (IDs, hashes)</summary><pre style={{background:"#131A20", border:"1px solid #1E2A33", borderRadius:8, padding:10, overflow:"auto", fontSize:11, marginTop:6, color:"#8B97A6"}}>{JSON.stringify(data,null,2)}</pre></details>
      </div>
    </main>
  );
}
