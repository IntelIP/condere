---
name: search-routing
description: "Choose where and how to search so evidence is current, firsthand and decision-relevant."
---

# Search routing

Search only when a material fact is missing. State that fact as `material_gap`.

Make it current:
- For anything time-sensitive (launches, pricing, funding, adoption, complaints,
  competitors), set `published_after` and `fresh=True` on `search_public`, and
  `since` / `posted_after` on X and Reddit.
- Read publication dates in results; prefer the most recent firsthand account.

Route by question:
- User pain, workarounds, costs: `reddit_research` (pick the community),
  `x_search`, Hacker News via `include_domains=["news.ycombinator.com"]`.
- A supplied tweet or thread: `x_posts`, then `x_conversation` for replies.
- Demos and talks: `youtube_transcripts`.
- Launches, funding, acquisitions: `search_public` with `category="news"`.
- Vendors and pricing: `category="company"` or `include_domains` for the
  vendor; fetch the pricing page live with `fetch_public_url`.
- Adoption: GitHub (`include_domains=["github.com"]`) and the repository index.
- Buyer complaints: review sites such as g2.com.
- Broad questions: `depth="deep-lite"` or `"deep"` with two to four
  `additional_queries` phrased differently.

Follow links one hop from strong sources with `fetch_public_url`. Search for the
customer's task and consequences, not just the technology name, which returns
implementation tutorials. Change route when a search returns background instead
of decision-changing evidence. Stop when new results no longer change a claim.
Keep queries public: never include private case details.
