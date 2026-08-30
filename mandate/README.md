# Mandate Module (`mandate/`)

> **Simple:** The buyer's permission slip. A human's wallet signs a token saying "agent X may spend up to ₹Y on categories Z until time T." Merchant checks it with 8 ordered checks — shape→issuer→sig→expiry→nonce→agent→scope→amount. Wrong in any step = blocked.

The `mandate` package implements the cryptographic authorization layer of the **PRAMAN** platform. A mandate represents a signed, tamper-proof proof of authority issued by a buyer principal (or their wallet) to an autonomous shopping agent. It defines precise constraints on what the agent may purchase, up to what spending limits, within what timeframe, and on behalf of whom.

## Simple — what each file does & its side

| File | Plain job | Side / Connected to |
|---|---|---|
| `keys.py` | ed25519 key mgmt: `MANDATE_SIGNING_SEED`→`SigningKey`→`public_key_hex` | Buyer wallet (`signer.py`) + merchant (`issuers.py`) |
| `token.py` | JWS `header.claims.sig` (base64url), claims (`sub/agent_id/scope/max_amount/max_single/valid_until/nonce/iss`), `scope_covers()` | `signer.py`, `verifier.py` |
| `signer.py` | Wallet: `build_claims()` (TTL 900s) + `sign()/issue()` with private key | `mandate/keys.py`, `harness/grahak.py:Wallet` |
| `issuers.py` | Merchant in-memory `TrustedIssuerRegistry` (register/revoke/is_trusted) + `bootstrap_demo_issuer` | `api/app.py` lifespan, `verifier.py` |
| `verifier.py` | 8-step verify pipeline → `MandateVerdict` + `mandate.accepted/rejected.*` ledger write | `store/ledger.py`, `kernel/checkout.py` (gate Tier 1) |
| `__init__.py` | Package boundary doc | — |

The merchant policy engine enforces strict zero-trust verification on mandates: verification failures result in deterministic rejections with named error codes recorded in an immutable ledger, never permissive warnings.

---

## Architectural Principles

1. **Strict Separation of Concerns (Buyer vs. Merchant)**:
   - The **buyer's wallet** (`signer.py`, `keys.py`) holds private keys and produces signed mandate tokens.
   - The **merchant verifier** (`verifier.py`, `issuers.py`) holds only public keys in a trusted registry and evaluates incoming mandates without access to private key material.
2. **Wire-Format Verification (No Re-encoding)**:
   - Cryptographic signatures are verified strictly over the received raw base64url segments (`base64url(header).base64url(claims)`). The verifier never re-serializes payload JSON prior to signature checks, preventing canonicalization mismatches or parsing vulnerabilities.
3. **Fail-Fast, Ordered 8-Step Verification Pipeline**:
   - Checks are ordered cheapest-first and identity-before-business logic, preventing CPU exhaustion attacks and classifying failures accurately (e.g., stolen tokens are rejected as stolen, not as over-budget).
4. **Non-Repudiation & Auditability**:
   - Every verification attempt (whether accepted or rejected) generates an immutable ledger entry specifying the exact check that passed or failed.
   - Replay protection is enforced by database-level partial unique indexes on consumed nonces (`mandate.accepted`).
5. **Escalation vs. Forgery Distinction**:
   - An unknown issuer is categorized as a business onboarding question (`UNKNOWN_ISSUER`, escalates to human review), while a bad signature on a known issuer's key is flagged as an explicit cryptographic forgery (`BAD_SIGNATURE`, immediate hard refusal).

---

## Files in this Directory

### 1. `__init__.py`
Defines the package documentation, architectural boundaries, and export declarations. Establishes the role of mandates in agent delegation.

### 2. `keys.py` (ed25519 Key Management)
Handles ed25519 cryptographic keypairs and seeds using `PyNaCl` (`nacl.signing.SigningKey` and `VerifyKey`).
- **Seed Handling**: Manages 32-byte hex seeds. Reads `MANDATE_SIGNING_SEED` from environment secrets for persistent identities across restarts, or generates an ephemeral in-memory seed for development/test runs.
- **Key Derivation & Export**: Deterministically derives signing keys from seed material; exports public keys in hexadecimal format (`public_key_hex`) while preventing private key leakage across trust boundaries.
- **Helper Functions**: `generate_seed()`, `signing_key_from_seed()`, `wallet_signing_key()`, `public_key_hex()`, and `verify_key_from_hex()`.

### 3. `token.py` (Wire Format & Claims Vocabulary)
Specifies the token serialization format (compact JWS representation: `base64url(header).base64url(claims).base64url(sig)`), claim schemas, and category-to-scope mappings.
- **Algorithm & Type**: Hardcoded to `EdDSA` (`ALGORITHM`) and `JWT` (`TOKEN_TYPE`). Rejects weaker or `none` algorithms.
- **Claim Vocabulary**: Defines mandatory claims: `sub` (buyer human principal), `agent_id` (authorized agent instance), `scope` (permitted purchase domains), `max_amount_inr` (total budget ceiling), `max_single_txn_inr` (per-transaction ceiling), `valid_until` (ISO timestamp expiration), `nonce` (single-use token ID), and `iss` (wallet issuer ID).
- **Scope Parsing**: Namespaces scopes with `purchase:<category>` and supports the `purchase:*` wildcard. Provides `scope_covers()` to verify whether cart item categories are authorized.
- **Wire Encoding / Decoding**: Implements canonical JSON serialization (`canonical_json`), unpadded base64url encoding/decoding, and segment splitting (`split()`, `assemble()`).

### 4. `signer.py` (Buyer Wallet & Mandate Issuance)
Implements the buyer wallet side that drafts and signs mandate tokens.
- **Claim Construction (`build_claims`)**: Validates mandatory inputs, computes default TTLs (`DEFAULT_TTL_SECONDS = 900` / 15 minutes), converts categories into scopes, and assigns unique nonces and ISO timestamps.
- **Signing (`sign`, `issue`)**: Encodes header and claims segments, signs the ASCII byte payload `header.claims` using the ed25519 private key, and produces the 3-segment mandate string.

### 5. `issuers.py` (Trusted Issuer Registry)
Maintains the merchant's thread-safe in-memory registry of authorized wallet providers and their corresponding public keys.
- **`TrustedIssuerRegistry`**: Provides registration (`register()`), revocation (`revoke()`), query (`get()`, `is_trusted()`), and serialization (`public_keys()`) methods guarded by re-entrant locks.
- **Bootstrap Simulation (`bootstrap_demo_issuer`)**: Simulates wallet onboarding by binding `DEMO_ISSUER_ID` (`demo-wallet-01`) to the wallet's generated public key.

### 6. `verifier.py` (Merchant Verification Pipeline)
Executes the comprehensive 8-step verification pipeline and issues structured verdicts (`MandateVerdict`).
- **Ordered Checks**:
  1. **Shape (`MALFORMED_MANDATE`)**: Validates JWS segment count, JSON syntax, `EdDSA` algorithm, claim types, positive integers, and timestamp format.
  2. **Issuer (`UNKNOWN_ISSUER`)**: Validates `iss` against `TrustedIssuerRegistry`. Escalates to human operator.
  3. **Signature (`BAD_SIGNATURE`)**: Cryptographically verifies raw wire bytes against the registered ed25519 verify key.
  4. **Expiry (`EXPIRED`)**: Checks `valid_until` against current UTC time.
  5. **Nonce Replay (`NONCE_REPLAYED`)**: Checks if the nonce has already been consumed in prior ledger events.
  6. **Agent Identity (`AGENT_MISMATCH`)**: Confirms mandate `agent_id` matches the calling agent instance.
  7. **Scope Coverage (`SCOPE_MISMATCH`)**: Validates that all cart item categories are permitted by the mandate scope.
  8. **Spending Limits (`AMOUNT_EXCEEDED`)**: Asserts `cart_total_inr <= max_single_txn_inr` and `cart_total_inr <= max_amount_inr`.
- **Verdict & Ledger Logging**:
  - Emits structured `MandateVerdict` dataclass.
  - Automatically records `mandate.accepted` on full success or `mandate.rejected.<code_lowercase>` on failure into the append-only ledger (`store.ledger`).
  - Employs database concurrency checks to prevent race-condition double-spending on nonces.

---

## Mandate Claims Schema

| Claim | Type | Description |
|---|---|---|
| `iss` | `string` | Unique identifier of the issuing wallet / financial institution. |
| `sub` | `string` | Human principal ID delegating purchasing power. |
| `agent_id` | `string` | Specific agent instance authorized to present the mandate. |
| `scope` | `list[str]` | List of authorized scopes (e.g. `["purchase:groceries", "purchase:electronics"]` or `["purchase:*"]`). |
| `max_amount_inr` | `int` | Maximum lifetime budget authorized for this mandate in INR. |
| `max_single_txn_inr` | `int` | Maximum ceiling for a single checkout transaction in INR. |
| `valid_until` | `string` | UTC timestamp in ISO 8601 format when the mandate expires. |
| `nonce` | `string` | Cryptographically random unique identifier ensuring single-use. |
| `issued_at` | `string` | UTC timestamp in ISO 8601 format when the mandate was signed. |

---

## Rejection Reason Codes

| Code | Check # | Description | Escalates to Human |
|---|:---:|---|:---:|
| `MALFORMED_MANDATE` | 1 | Malformed base64url, non-JSON, missing claims, invalid types | No |
| `UNKNOWN_ISSUER` | 2 | Issuer not found in merchant's trusted issuer registry | **Yes** |
| `BAD_SIGNATURE` | 3 | ed25519 signature verification failure against issuer key | No |
| `EXPIRED` | 4 | Current time exceeds `valid_until` timestamp | No |
| `NONCE_REPLAYED` | 5 | Nonce already recorded as accepted in ledger | No |
| `AGENT_MISMATCH` | 6 | Presenting agent does not match mandate `agent_id` | No |
| `SCOPE_MISMATCH` | 7 | Cart contains item categories not covered by `scope` | No |
| `AMOUNT_EXCEEDED` | 8 | Cart total exceeds `max_single_txn_inr` or `max_amount_inr` | No |
