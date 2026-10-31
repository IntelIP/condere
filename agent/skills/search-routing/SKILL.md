---
name: search-routing
description: "Choose where and how to search so evidence is current, firsthand and decision-relevant."
---

# Search routing

Search only when a material fact is missing. State that fact as `material_gap`.

Make it current:
- For anything time-sensitive (launches, pricing, funding, adoption, complaints,
  competitors), set `published_after` and `fresh=True` on `search_public`, and
  `since` / `posted_after` on Hacker News, GitHub issues, X and Reddit.
- Read publication dates in results; prefer the most recent firsthand account.

Route by question:
- User pain, workarounds, costs, firsthand: start with the free routes.
  - `hacker_news_search` (`kind="comment"` for practitioners' own accounts).
  - `github_issues_search`: unmet needs and pain from a project's users, most
    reacted first; name `repository` for one project, omit it to find the need
    across projects.
  - Then, only if those leave the gap open, `reddit_research` (one named
    community, the defaults) or `x_search`. Reddit and X are paid and capped per
    assessment; when the tool reports the cap is used, stay on the free routes.
- A supplied tweet or thread: `x_posts`, then `x_conversation` for replies.
- Demos and talks: `search_public` with `include_domains=["youtube.com"]`; its
  results carry the video's transcript. Use `youtube_transcripts` only for a
  video whose result had no transcript.
- Launches, funding, acquisitions: `search_public` with `category="news"`.
- Vendors and pricing: `category="company"` or `include_domains` for the
  vendor; fetch the pricing page live with `fetch_public_url`.
- Adoption: GitHub (`include_domains=["github.com"]`) and the repository index.
- Buyer complaints: review sites such as g2.com.
- Depth: use `depth="auto"` (the default) first. Move to `"deep-lite"` with two
  to four `additional_queries` only when auto results leave a decision-changing
  gap, and use `"deep"` at most once per assessment, for the hardest question.
  Deep searches cost several times more.

Paid search has a per-assessment allowance (Exa about $1, Apify $0.15). When a
tool reports its allowance is used, work from saved sources and the free routes;
do not retry the paid tool.

Follow links one hop from strong sources with `fetch_public_url`. Search for the
customer's task and consequences, not just the technology name, which returns
implementation tutorials. Change route when a search returns background instead
of decision-changing evidence. Stop when new results no longer change a claim.
Keep queries public: never include private case details.
