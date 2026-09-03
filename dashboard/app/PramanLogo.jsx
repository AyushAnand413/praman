import React from "react";

/**
 * PRAMAN Verifiable Hex Seal (प्रमाण Mudrā)
 * Minimalist geometric mark combining:
 * 1. Verifiable Hexagonal Crest (Security & Cryptographic Guardrails)
 * 2. Precision Monogram 'P' (Authenticity & Identity)
 * 3. Amber Verification Proof Node (Proof of State)
 */
export function PramanLogo({ size = 26, className = "" }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      style={{ flexShrink: 0, verticalAlign: "middle", display: "inline-block" }}
      aria-label="PRAMAN Logo"
    >
      {/* Verifiable Hexagonal Crest */}
      <path
        d="M16 3L27.5 9.5V22.5L16 29L4.5 22.5V9.5L16 3Z"
        fill="var(--bg-card)"
        stroke="var(--accent)"
        strokeWidth="1.8"
        strokeLinejoin="round"
      />
      {/* Precision Monogram P */}
      <path
        d="M12.5 9.5V22.5M12.5 9.5H17.5C20 9.5 22 11.5 22 14C22 16.5 20 18.5 17.5 18.5H12.5"
        stroke="var(--text-heading)"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* Cryptographic Proof Node */}
      <circle cx="20" cy="20.5" r="1.75" fill="var(--accent)" />
    </svg>
  );
}

export default PramanLogo;
