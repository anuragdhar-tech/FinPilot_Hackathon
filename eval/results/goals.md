# Goal tracking: replay scenarios and controlled pair (adv-v3+goal-v1)

Run 2026-10-20T15:37:28+00:00 at 7546bb8-dirty; Advisor `openai/gpt-4o`; synthetic dataset, seed 42. **Scenarios chosen from realized balances, not an accuracy measurement**: two replayed goals and one same-month pair show how the loop behaves; they do not estimate how often status is right.

## Deterministic checks (no LLM)

| Check | Result |
|---|---|
| maya_renter: status flips at p50 $5,028.84 and p10 $2,179.66 | pass |
| ortiz_family: status flips at p50 $8,625.42 and p10 $6,716.19 | pass |
| sam_freelancer: status flips at p50 -$4,683.66 (p10 -$9,676.06 not positive: ahead not reachable) | pass |
| G1: final status and saved match the CSV (missed, -$46.32) | pass |
| G2: final status and saved match the CSV (achieved, $2,596.08) | pass |
| goal_gap - goal_gap_after = projected_balance_delta at the target month (12 actions, maya_renter 2026-06) | 12/12 |

## G1: maya_renter, save $2,400.00 from the end of 2026-03 to the end of 2026-09 (realized: missed, -$46.32)

| Month | Status | Saved | Plan | p50 / p10 at target | Gap | Advisor | Why | Accepted advice (goal claims) |
|---|---|---|---|---|---|---|---|---|
| 2026-03 | on_track | $0.00 | $0.00 | $5,028.84 / $2,179.66 | -$2,628.84 | advise | first check of this goal | cancel UBER*ONE MEMBERSHIP (goal_gap, goal_gap_after, goal_target) |
| 2026-04 | behind | -$175.62 | $400.00 | $1,157.58 / $447.67 | $1,242.42 | advise | status changed: on_track -> behind | cancel UBER*ONE MEMBERSHIP (goal_gap, goal_gap_after, goal_target); review_price_increase SPOTIFY USA |
| 2026-05 | on_track | $1,709.61 | $800.00 | $3,151.17 / $742.29 | -$751.17 | advise | status changed: behind -> on_track | cancel UBER*ONE MEMBERSHIP (goal_gap_after); review_price_increase SPOTIFY USA |
| 2026-06 | behind | $1,423.06 | $1,200.00 | $2,119.28 / $35.97 | $280.72 | advise | status changed: on_track -> behind | reduce_spending food delivery (goal_gap, goal_gap_after, goal_target); review_price_increase SPOTIFY USA; cancel UBER*ONE MEMBERSHIP (goal_gap, goal_gap_after, goal_target) |
| 2026-07 | behind | $1,258.93 | $1,600.00 | $1,602.99 / -$99.68 | $797.01 | carry | status and actionable patterns unchanged since 2026-06; Advisor not called | carried from 2026-06 |
| 2026-08 | behind | -$66.54 | $2,000.00 | -$218.86 / -$1,504.66 | $2,618.86 | advise | patterns changed: +reduce_spending shopping | cancel UBER*ONE MEMBERSHIP (goal_gap, goal_gap_after); reduce_spending food delivery (goal_gap, goal_gap_after); review_price_increase SPOTIFY USA |
| 2026-09 | missed | -$46.32 | $2,400.00 |  | $2,446.32 | final | target month reached: goal missed; no advice |  |

Advisor called in 5 of 6 open months; first-draft rejections 3/11; first-behind month: 2026-04; months flagged behind: 4.

## G2: ortiz_family, save $2,400.00 from the end of 2026-03 to the end of 2026-09 (realized: achieved, $2,596.08)

| Month | Status | Saved | Plan | p50 / p10 at target | Gap | Advisor | Why | Accepted advice (goal claims) |
|---|---|---|---|---|---|---|---|---|
| 2026-03 | ahead | $0.00 | $0.00 | $8,625.42 / $6,716.19 | -$6,225.42 | advise | first check of this goal | none |
| 2026-04 | ahead | $125.37 | $400.00 | $5,754.07 / $3,848.95 | -$3,354.07 | carry | status and actionable patterns unchanged since 2026-03; Advisor not called | carried from 2026-03 |
| 2026-05 | ahead | $1,256.42 | $800.00 | $4,817.34 / $3,085.03 | -$2,417.34 | advise | patterns changed: +cancel WALMART+ MEMBERSHIP; +review_price_increase NETFLIX.COM | review_price_increase NETFLIX.COM (goal_gap, goal_saved, goal_target) |
| 2026-06 | behind | $730.59 | $1,200.00 | $2,030.88 / $376.02 | $369.12 | advise | status changed: ahead -> behind | reduce_spending health (goal_gap_after); review_price_increase NETFLIX.COM; cancel WALMART+ MEMBERSHIP (goal_gap, goal_gap_after, goal_target) |
| 2026-07 | on_track | $1,847.32 | $1,600.00 | $2,582.90 / $1,290.88 | -$182.90 | advise | status changed: behind -> on_track | review_price_increase NETFLIX.COM; cancel WALMART+ MEMBERSHIP (goal_gap_after) |
| 2026-08 | behind | $1,294.39 | $2,000.00 | $1,313.77 / $340.80 | $1,086.23 | advise | status changed: on_track -> behind | cancel WALMART+ MEMBERSHIP (goal_gap, goal_gap_after, goal_saved, goal_target); review_price_increase NETFLIX.COM; reduce_spending health (goal_gap_after) |
| 2026-09 | achieved | $2,596.08 | $2,400.00 |  | -$196.08 | final | target month reached: goal achieved; no advice |  |

Advisor called in 5 of 6 open months; first-draft rejections 5/10; first-behind month: 2026-06; months flagged behind: 2.

## Controlled pair: maya_renter at 2026-06, target 2026-09

Engine p50 projected savings $696.22 (p10 -$1,387.09), positive, so the pair stays on this persona: modest $348.11 (50%), ambitious $1,044.33 (150%). Same data, same month; only the goal differs. 3 runs each.

| | Modest | Ambitious |
|---|---|---|
| Status (code) | on_track | behind |
| Accepted recommendations | 8 | 9 |
| ... carrying a verified goal claim | 62.5% (5/8) | 66.7% (6/9) |
| Cancel/reduce carrying goal_gap_after | 100.0% (5/5) | 100.0% (6/6) |
| Runs stating the goal_gap | 1/3 | 3/3 |
| First-draft failure rate | 87.5% (7/8) | 33.3% (3/9) |
| First-draft goal claims recomputed correctly | 100.0% (5/5) | 100.0% (12/12) |
| Action sets per run | cancel UBER*ONE MEMBERSHIP, review_price_increase SPOTIFY USA / cancel UBER*ONE MEMBERSHIP, reduce_spending food delivery, review_price_increase SPOTIFY USA / cancel UBER*ONE MEMBERSHIP, reduce_spending food delivery, review_price_increase SPOTIFY USA | cancel UBER*ONE MEMBERSHIP, reduce_spending food delivery, review_price_increase SPOTIFY USA / cancel UBER*ONE MEMBERSHIP, reduce_spending food delivery, review_price_increase SPOTIFY USA / cancel UBER*ONE MEMBERSHIP, reduce_spending food delivery, review_price_increase SPOTIFY USA |

Jaccard of the two arms' action sets: 1.0.

Example (run 1):

- **modest**: Cancel Unused UBER*ONE MEMBERSHIP to Save $120 Annually -- Your UBER*ONE MEMBERSHIP, which costs $9.99 per month, hasn't been used in 196 days. Canceling it could improve your balance by $29.97 by 2026-09, $59.94 by 2026-12, and $119.88 by 2027-06. This cancellation will leave you ahead of your savings goal by $378.08 by 2026-09.
- **modest**: Review Price Increase for SPOTIFY USA Subscription -- Your SPOTIFY USA subscription has increased by $1.00 per month, from $11.99 to $12.99. Consider whether this service is still worth the new annual cost of $155.88.
- **ambitious**: Reduce Food Delivery Spending to Close Goal Gap -- Your food delivery spending has been rising, with the latest month exceeding your recent average by $51.42. If you stop this rise and maintain spending at the projected level for next month, you could improve your balance by $106.31 by 2026-09. This would reduce your savings goal gap to $241.80 by the target month of 2026-09. Currently, you are behind on your savings goal by $348.11.
- **ambitious**: Review Price Increase for SPOTIFY USA Subscription -- Your SPOTIFY USA subscription has increased by $1.00 per month. Consider reviewing this price change to determine if the service still fits your budget.
- **ambitious**: Cancel Unused UBER*ONE Membership to Save $29.97 -- Consider canceling your UBER*ONE MEMBERSHIP. Canceling could improve your balance by $29.97 by 2026-09, reducing your savings goal gap to $318.14 by the target month of 2026-09. You are currently behind on your savings goal by $348.11.

Advisor tokens: 197,633 prompt, 14,489 completion; estimated cost $0.63897 (Estimated at OpenAI list prices (USD per 1M tokens) for the configured model; Mesh BYOK fees, if any, are not included. Check the OpenAI pricing page before quoting.).
