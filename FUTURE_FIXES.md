# PRAMAN — Remaining Issues & Simple Fix Ideas

> These are not bugs — the app works. These are things that make it feel slow or rough around the edges.

---

## What is "Cold Start"?

Think of Supabase (your database) like a shop assistant who goes home after 5 minutes of no customers.

When the next customer walks in, the shop assistant has to come back from home — that takes 5 to 30 seconds. During that time, the customer just sees a loading screen. Once the assistant is back, everything is fast again.

This is called a **cold start**. It happens on the **free tier** of Supabase automatically. You cannot stop it from happening — but you can work around it.

---

## Remaining Issue 1 — Cold Start Makes First Visit Slow

**What you see:**  
First person to open the dashboard after 5+ minutes of no traffic sees a loading screen for up to 30 seconds.

**Why:**  
Supabase free tier puts the database to sleep after 5 minutes idle. First request wakes it up. Takes 5-30s.

**Simple fix idea:**  
Set up a **Vercel Cron Job** that pings `/health` every 4 minutes automatically. This keeps the database awake so it never goes to sleep in the first place.

```json
// vercel.json — add this block
{
  "crons": [
    {
      "path": "/health",
      "schedule": "*/4 * * * *"
    }
  ]
}
```

Cost: Free (Vercel Hobby allows 2 cron jobs). Takes 10 minutes to set up.

---

## Remaining Issue 2 — Dashboard Still Takes ~3 Seconds (Not Under 1 Second)

**What you see:**  
Dashboard loads in ~3 seconds. Good, but not instant.

**Why:**  
The `orders` table has no index on the date column. Every time the dashboard loads, the database scans all orders to find today's. Supabase also adds ~200-300ms per database call, and the dashboard makes several calls back-to-back.

**Simple fix idea:**  
Add an index on `orders.created_at` so the date lookup is instant. Run this once in Supabase SQL editor:

```sql
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders (created_at);
```

Takes 2 minutes.

---

## Remaining Issue 3 — The Speed Panel Resets After Every Idle Period

**What you see:**  
The safety/bounds panel in the dashboard recalculates from scratch on the first load after idle, even though those numbers rarely change.

**Why:**  
The 60-second cache that stores the panel results lives inside the server's memory. Vercel serverless functions have no permanent memory — every cold start throws it away.

**Simple fix idea:**  
Store the cached panel result in the database itself (one row with a timestamp). On each request, check if the row is older than 60 seconds — if not, use it directly. No extra service needed.

Alternatively, connect **Upstash Redis** (free tier, zero config) as a proper shared cache. Takes about 2 hours.

---

## Remaining Issue 4 — Shopify Sync Could Fail on Big Stores

**What you see:**  
For Gada (5 products) it works fine — sync completes in ~27 seconds. But if a store has 100+ products, the sync would hit Vercel's 30-second request limit and get cut off mid-import.

**Why:**  
The Shopify sync runs inside a regular web request. Vercel kills web requests after 30 seconds. Large catalogs need more time.

**Simple fix idea:**  
Use **Vercel Cron** or **QStash** (free tier) to run the sync as a background job that is not limited by the 30-second rule. The user clicks "Connect", it queues the job, and the job runs separately without a timer. Takes about 3-4 hours to set up properly.

---

## Remaining Issue 5 — Loading Message is Outdated

**What you see:**  
When restoring your session, the page shows:  
*"First load after idle takes 3-5 sec"*

**Why it's wrong:**  
This was written when the dashboard took 19 seconds. Now it takes 3 seconds when the DB is warm, but still up to 30 seconds on a cold start. The message is misleading.

**Simple fix idea:**  
Change the text in `dashboard/app/page.jsx` to something honest:

> *"Connecting — may take up to 30 sec after a long idle (database waking up)."*

Takes 5 minutes.

---

## Summary — Effort vs Impact

| Issue | How hard | How much it helps |
|-------|----------|------------------|
| Cold start — add Vercel cron ping | 10 min | Huge — kills the whole problem for normal usage |
| Orders date index in Supabase | 5 min | Dashboard goes from ~3s to ~1s |
| Outdated loading message | 5 min | Small — just more honest UX |
| Panel cache in DB or Redis | 2 hrs | Medium — saves ~100ms per load |
| Shopify sync for big stores (QStash) | 3-4 hrs | Only matters if store has 100+ products |

**Recommended order:** Cold start cron → orders index → loading message → rest later.
