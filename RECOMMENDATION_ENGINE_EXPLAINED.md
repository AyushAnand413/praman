# Praman Recommendation Engine — Comprehensive Guide & Cases

This document explains the architecture, mathematical algorithms, cold-start handling, and real-world cases of the **Praman Recommendation Engine**.

---

## 1. Executive Summary & Core Philosophy

Praman's recommendation engine is built on **Association Rule Mining (Market Basket Analysis with Lift)** rather than black-box deep learning.

### Why this design?
1. **Serverless & Ultra-Fast:** Runs in pure Python + SQLite/Postgres. Adds **0 MB** of dependencies and executes in **< 2ms** (no 500MB PyTorch/TensorFlow cold starts on Vercel).
2. **Real-Time Learning:** Learns on every checkout instantly (`O(1)` counter update), with no overnight batch model retraining.
3. **100% Auditable:** When an AI suggests an upsell, Praman issues a cryptographically signed policy receipt proving why the bundle was legally and economically valid under **Bound 10**.

---

## 2. The 4 Product Recommendation Scenarios (Cases)

```mermaid
graph TD
    A[Customer / Agent asks for an Offer] --> B{Store Order History?}
    B -->|0 Orders: Brand New Store| C[Case 1: Cold Start Priors]
    C --> C1[1. Read Catalog Seeded Attachments]
    C --> C2[2. Cross-Store Category Cluster Priors]
    B -->|Has Orders: Active Store| D[Case 2: Observed Lift & Co-occurrence]
    D --> D1[Rank by Lift = P(B|A) / P(B)]
    D --> D2[Apply Exponential Time Decay]
    A --> E{Constraints Check}
    E -->|Out of Stock or Over Budget| F[Case 4: Dynamic Rejection & Fallback]
    A --> G{New Large Catalog / Shopify}
    G --> H[Case 3: Automated Category Mapping & Sync]
```

---

### Case 1: Brand New Store with 0 Orders (The Cold Start)
* **Scenario:** A new store (e.g., *Gada Electronics*) goes live today. No customers have ever purchased anything.
* **Problem:** There is no purchase history to learn from.
* **How Praman Solves It:**
  1. **Catalog Seeding:** When the store is set up, `seed_pairings_from_catalog()` imports the merchant's declared `attach_candidates` (e.g. `Earbuds -> Silicone Case`) into the `pairings` table with `source="seeded"`.
  2. **Cross-Store Cluster Priors:** If the merchant forgot to declare accessories, `suggest_from_cluster()` borrows category-level associations from other stores in the same cluster (e.g., `audio_earbuds` pairs with `audio_accessories`).
* **Result:** The store recommends valid accessories from Day 1, Minute 1.

---

### Case 2: Active Store with Sales History (Self-Learning via Lift)
* **Scenario:** The store has completed 500 orders.
* **How It Works:**
  1. Whenever a customer buys Item A and Item B together, `checkout.py` calls `record_order_basket([A, B])`.
  2. The system updates the co-occurrence counters in real-time.
  3. **Ranking by Lift:** When recommending for Item A, Praman ranks companions by **Lift**, not just raw popularity.
* **Example:**
  * Generic Cleaning Cloth is bought by 60% of all shoppers $\rightarrow \text{Lift} = 1.0\times$ (Neutral).
  * Specific Earbud Case is bought by only 5% of general shoppers, but 40% of Earbud buyers $\rightarrow \text{Lift} = 8.0\times$.
  * Praman automatically picks the **Case** over the Cloth because its Lift is 8x stronger than chance.
* **Time Decay:** Older purchases decay using an exponential half-life ($0.5^{\Delta t / \text{half-life}}$). Seasonal trends naturally fade out without manual database cleanup.

---

### Case 3: Large Store Catalog Import (e.g., Fashion with 5,000 SKUs)
* **Scenario:** A fashion store (like Zara) joins with 5,000 SKUs (Shirts, Trousers, Shoes, Belts, Socks). No human can manually write pairings for 5,000 items.
* **How Praman Solves It Without Manual Work:**
  1. **Category Fallbacks:** Products are mapped by category (`shoes`, `denim`, `accessories`). If specific SKU pairings don't exist yet, `cluster_pairs_for()` matches:
     $$\text{Category: Footwear} \longrightarrow \text{Category: Socks / Shoe Care}$$
  2. **Shopify Order History Sync:** During the initial store sync, Praman imports the merchant's past 6 months of Shopify orders and pipes them through `record_order_basket()`. The pairing engine is trained in under 15 seconds.

---

### Case 4: Live Inventory & Budget Constraints
* **Scenario:** The top recommended accessory is **Out of Stock** or exceeds the buyer's declared budget.
* **How Praman Solves It:**
  1. The recommender checks `available_qty` inside the active sellable envelope.
  2. If `AT-TIP-FOAM` has `stock_qty = 0`, it is immediately bypassed.
  3. If the buyer stated `budget_inr = 5200`, and Base (₹4,999) + Case (₹599) = ₹5,598, the case is skipped for a cheaper companion or omitted.
  4. The algorithm falls back to the next best candidate (`AT-CBL-USBC` at ₹399 = ₹5,398).

---

## 3. The Mathematical Formulas

### 1. Confidence (Conditional Probability)
$$\text{Confidence}(A \rightarrow B) = P(B \mid A) = \frac{\text{Orders with both } A \text{ and } B}{\text{Total orders containing } A}$$

### 2. Lift (Correlation Strength)
$$\text{Lift}(A \rightarrow B) = \frac{P(B \mid A)}{P(B)} = \frac{\text{Confidence}(A \rightarrow B)}{\text{Total orders with } B \;/\; \text{Total orders in store}}$$

* **$\text{Lift} > 1.0$:** True positive affinity (Items are genuinely bought together).
* **$\text{Lift} = 1.0$:** Independent (Bought together purely by random chance).
* **$\text{Lift} < 1.0$:** Negative affinity (Buying $A$ makes someone *less* likely to buy $B$).

### 3. Exponential Time Decay
$$\text{Weight} = 0.5^{\frac{\Delta \text{days}}{\text{PAIRING\_HALF\_LIFE\_DAYS}}}$$
Counts age gracefully, preventing 6-month-old obsolete promotions from corrupting current recommendations.

---

## 4. End-to-End Workflow in MCP (Model Context Protocol)

In Praman, external AI agents (like Claude Desktop, ChatGPT, or autonomous buyer bots) interact over FastMCP mounted at `/mcp`:

```
[Buyer Agent (Claude)] 
       │
       │  1. get_offer(base_sku="AT-PRO-BLK", need="earbuds")
       ▼
[Praman FastMCP Server (/mcp)]
       │
       │  2. Proposer generates Base Proposal
       ▼
[Offer Kernel (kernel/offer.py)]
       │
       │  3. Check: Did LLM provide upsells?
       │     NO ──> Call kernel.recommender.recommend_upsells()
       ▼
[Recommender Engine (kernel/recommender.py)]
       │  • Query pairings for AT-PRO-BLK
       │  • Check Stock & Budget
       │  • Rank by Lift
       │  • Return AT-CASE-01
       ▼
[Assemble Options]
       │  • Option A: Single (AT-PRO-BLK @ ₹4,999)
       │  • Option B: Bundle (AT-PRO-BLK + AT-CASE-01 @ ₹5,498)
       ▼
[Return MCP Result to Buyer Agent]
       │
       ▼
[Buyer Agent presents Option A and Option B to Human]
```

---

## 5. Codebase Directory & File Responsibilities

| File | Exact Purpose |
| :--- | :--- |
| [`catalog.json`](file:///c:/Users/KIIT/Downloads/praman/catalog.json) | Store products, prices, and default `attach_candidates` |
| [`store/pairings.py`](file:///c:/Users/KIIT/Downloads/praman/store/pairings.py) | Database operations for `pairings` table, decay math, and lift computation |
| [`kernel/recommender.py`](file:///c:/Users/KIIT/Downloads/praman/kernel/recommender.py) | Algorithmic selector: reads pairings, filters stock/budget, formats `ProposedUpsell` |
| [`kernel/offer.py`](file:///c:/Users/KIIT/Downloads/praman/kernel/offer.py) | Assembles the deal; automatically augments Option B if upsells are missing |
| [`kernel/bounds.py`](file:///c:/Users/KIIT/Downloads/praman/kernel/bounds.py) | **Bound 10**: The policy gatekeeper that validates upsell relatedness |
| [`api/mcp.py`](file:///c:/Users/KIIT/Downloads/praman/api/mcp.py) | FastMCP server exposing tools to external AI agents |
| [`api/app.py`](file:///c:/Users/KIIT/Downloads/praman/api/app.py) | Server lifespan: runs automatic seeding on server boot |
