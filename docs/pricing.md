# LabFlow Pricing Memo (v0.1)

## Pricing principles
1. Charge per *team*, not per seat. Coordination value scales with team size, but seat-based pricing punishes the exact behavior we want (more meetings ingested, more reviewers).
2. Free tier should cover a single squad doing 1–2 meetings/week, so individual researchers can champion adoption bottom-up.
3. Pricing should reward verified execution, not raw transcript volume — that's our differentiator and it aligns incentives.

## Tiers

| Tier | Price | Meetings / mo | Verified tasks / mo | Retention | Connectors |
|------|------:|---------------|---------------------|-----------|------------|
| Solo | Free | 8 | 50 | 30 days | none |
| Team | $99 / team / mo | 80 | 1,000 | 12 months | GitHub + Linear |
| Lab  | $399 / team / mo | 400 | 10,000 | unlimited | + Notion, Slack, custom webhooks |
| Enterprise | custom | unlimited | unlimited | unlimited + SSO/SOC2 | all + private deployment |

## Why these numbers
- **$99 Team** is below the discretionary purchase threshold for most engineering managers, which keeps the sales cycle short.
- **$399 Lab** covers a 10–20-person research group running daily standups and weekly experiment reviews, which is our beachhead persona.
- **Verified-task quotas** make the value metric legible: customers can see exactly what they're paying for.

## Add-ons
- **Hosted LLM extraction** (per-1k tokens passthrough + 25% margin) — opt-in, off by default. The default rules-based extractor is free.
- **Custom templates** for new meeting types — included in Lab and above.

## Risk to pricing
- If LLM costs drop 10× we should pass through ~50% to customers and use the rest to fund verification quality.
- If a competitor undercuts on Team tier, we compete on verification and decision-graph features, not price.
