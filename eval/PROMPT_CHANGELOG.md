# Categorizer prompt / cache changelog

Every change to the Categorizer's prompt, output schema, validation or cache policy is logged here,
with why it was made and what evidence prompted it. Metrics are never typed here; they live in the
script-written files under `eval/results/` named for each version. Earlier versions stay committed
and reproducible (`python -m eval.categorization --prompt-version cat-v1`).

## cat-v1 (Oct 15): baseline

- Prompt: written from the closed 15-category taxonomy and Day 1 policies before any model output
  was seen. No few-shot examples from the dataset.
- The line `other: ... (e.g. software subscriptions, postage)` mirrors the documented Day 1 taxonomy
  policy (Adobe/USPS fall into `other`); it was not derived from eval results.
- Cache: normalized descriptor + sign; one look with confidence >= 0.8 is cached and reused.
- Results: `eval/results/categorization_cat-v1.*`, `eval/results/categorization_nocache_cat-v1.*`.

## cat-v2 (Oct 15): candidate, prompted by cat-v1 misses

**Eval-fitting caveat.** Every change below was motivated by reading cat-v1's misses on the
synthetic set, so cat-v2's synthetic score is not an unbiased estimate. Mitigations: changes are
generic rules (no merchant names added), ground truth and `acceptable_labels` are not edited after
seeing outputs, cat-v1 results stay committed, and the hand-labelled real sample is the held-out check
before any improvement is claimed. Iteration uses `--dev`; full runs only for committed baselines.

1. **Prompt, `other`** (owner's wording).
   - was: `- other: fits none of the above (e.g. software subscriptions, postage)`
   - now: `- other: only for things that fit no category above; streaming and memberships are never other`
   - evidence: Spotify ("Spotify is a software subscription"), Amazon Prime, Walmart+, Uber One
     predicted `other` with rationales calling them subscriptions/memberships, despite rule 4.
   - side effect to watch: Adobe/USPS (truth `other`) lose the explicit hint.
2. **Prompt, processor prefixes** (owner's wording).
   - was: `Processor prefixes such as SQ*, TST* and DD* are hints, not the merchant.`
   - now: `SQ*/TST* indicate the card processor only, not delivery or a category; classify by the merchant.`
   - `DD*` is no longer mentioned: grouping it with card processors contradicts the new sentence
     (DD* is DoorDash). No replacement fact was added.
   - evidence: `SQ *CURRY HOUSE`, `TST* THAI BASIL` read as delivery orders.
3. **Schema + validation, merchant echo check (P1).** Each result now includes `merchant` (rule 8:
   copy the merchant words exactly as they appear). Code rejects an answer whose echo shares no
   distinctive word with that item's descriptor and retries the item. Rationale becomes rule 9.
   - evidence: rationales describing a neighbouring item, e.g. Costco -> housing "Loan payment
     indicates mortgage"; TST* Thai Basil "Grubhub indicates a food delivery service".
   - limits: cannot detect a swap between two items of the same merchant.
   - metric: echo rejection rate (in-loop), plus a rationale-based proxy computed for every version.
4. **Cache policy, confirm before caching (P2a).** A key is cached and reused only after a second,
   independent look agrees (same category, both confident). The second look is a different
   transaction of the same key (a different descriptor variant when one exists), sent in a separate
   confirmation phase whose items are shuffled with a fixed seed, so it has different neighbours,
   position and batch boundaries. Disagreement: nothing cached, every member gets its own look.
   Single-member keys are no longer cached.
   - evidence: 86 of 118 cat-v1 cache-mode misses were copies of a few wrong single looks
     (Spotify 24, Amazon Prime 12, Walmart+ 12, Lemonade 11); confidence was 0.90 on 1,900 of 1,904
     rows, so the 0.8 gate filtered nothing.
   - limits: agreement does not prove correctness; a consistent model error (Lemonade -> health)
     passes both looks.

### cat-v2 known limitations (seen on the --dev comparison; not fixed, categorization closed Oct 15)

- **Adobe -> entertainment.** Removing the `software subscriptions` hint from `other` sends Adobe
  Creative Cloud to `entertainment` ("Adobe Creative Cloud is a subscription service, classified as
  entertainment"). Adobe's acceptable labels are `other`/`shopping`, so this costs strict and lenient.
- **Grubhub + restaurant -> dining.** `GRUBHUB*PHO 88` labelled `dining` ("PHO 88 indicates a restaurant
  purchase"), possibly from the new "classify by the merchant" wording overriding rule 2. One row on dev.
- **Consistent errors pass the second look.** `CHIPOTLE ONLINE` -> food_delivery and `STRIPE TRANSFER`
  -> transfers were confirmed and cached wrong; agreement is not correctness.
- **Confidence is still flat** (about 98% of rows at 0.90), so it is a weak "needs review" signal.
- **Misalignment before/after is not measured.** The echo check rejected 0 of 520 cat-v2 answers on
  dev; cat-v1 has no echo field, and a rationale-based proxy was tried and dropped as too noisy.

# Forecaster method changelog

Every change to the forecast method is logged here. Numbers below are quoted from the script-written
files named next to them (`python -m eval.analysis --reuse-db --forecast-version <v>`); both versions
stay committed and reproducible.

## fc-v1 (Oct 16): baseline

- Active recurring charges at their current price + 3-month trailing variable spend per category
  (Theil-Sen line for trend categories, floored at 0) + 6-month trailing income. Anomaly-flagged
  transactions are left out of the variable spend. Designed before any backtest was run.
- Backtest, 24 next-month forecasts (`eval/results/forecast_fc-v1.md`): total-spend MAPE 12.3%
  (no trend line 12.4%, naive last month 13.2%, naive 3-month mean 11.4%); mean error -$645
  (3-month mean -$286); 80% band coverage 54.2%.

## fc-v2 (Oct 16): one-off allowance, **made after seeing the fc-v1 backtest**

- Adds one flow: "one-off allowance" = 6-month trailing mean of the monthly spend the Analyzer
  flagged as anomalies (spike, duplicate, new merchant), with the sample standard deviation of those
  monthly totals as its spread. Shown as `by_category["one_off"]`. Nothing else in the engine changed.
  The 6-month window was chosen before running fc-v2.
- Evidence: fc-v1 under-forecast on average (-$645); its largest misses were months with one-off
  purchases (Best Buy, IKEA, Delta, Apple Store, the Costco spike), which fc-v1 excludes and nothing
  replaces.
- Eval-fitting caveat: this change was motivated by the same 24 backtest points it is scored on, so
  fc-v2's backtest score is not an unbiased estimate.
- Backtest (`eval/results/forecast_fc-v2.md`): total-spend MAPE 11.2% (no trend line 11.3%; naive
  baselines unchanged); mean error -$404; 80% band coverage 54.2% (bands widen, but one forecast
  moved into the band and one moved out). Per-category MAPE unchanged (the allowance is its own bucket).

# Advisor and what-if prompt changelog

Numbers are quoted from `eval/results/advice.md` (`python -m eval.advice --repeats 3`).

## adv-v1, wif-parse-v1, wif-explain-v1 (Oct 15): baseline

- Advisor (`backend/agents/advisor.py`): one LLM with three read-only tools (`list_patterns`, `get_pattern`,
  `get_forecast`) that hand it every figure as a ready-made claim; strict JSON output; temperature 0. Redraft
  prompt = the rejected draft + the Verifier's structured failed checks. Designed before any eval run.
- What-if parser (`backend/agents/whatif.py`): strict JSON scenario, one retry with validation errors.
  Explanation: facts as display strings, number-checked, one rewrite, then a code template.
- Eval (19 lineages, 3 runs per persona): first-draft failure rate 68.4%, all on the text-number rule
  (13 numbers, 1 action + required claims); claims themselves 100% correct on first drafts; 16 accepted,
  2 dropped after 3 attempts, 1 withdrawn. Parser 8/8 exact; explanations 8/8 passed on the first try.
- Not changed after seeing the results (needs approval first); evidence for possible adv-v2 is in the
  Day 4 report: unclaimed "3 months" from the tool's own wording, "$120" for $121.80 under "you may round
  dollars", and 4 of 16 redrafts identical to the rejected draft.

## adv-v2 (Oct 15): **made after seeing the adv-v1 eval**, one approved round, no threshold changes

- Evidence (`eval/results/advice_adv-v1.md`): all 13 first-draft failures were the text-number rule; "3 months"
  came from the tool's own wording; "$120" for $121.80 followed the prompt's "you may round dollars"; 4 of 16
  redrafts were identical to the rejected draft and caused both drops.
- Changes (only these three):
  1. Tool text is number-free: `get_pattern`/`list_patterns` show a label instead of the Analyzer's title
     (which carries amounts and dates no claim backs), no first/last-seen dates, and figure meanings say
     "your recent average" instead of "the 3 months before it"; the get_pattern description says every
     number written must be a figure's value.
  2. Rounding rule stated as the Verifier checks it: exact to the cent, or the nearest whole dollar.
  3. Redraft feedback: one entry per unbacked number with the nearest claim of the same unit and the exact
     accepted forms, or an instruction to remove the phrase.
- adv-v1 stays selectable (`python -m eval.advice --prompt-version adv-v1`); its results stay committed as
  `advice_adv-v1.*`. Eval-fitting caveat: adv-v2 is scored on the same personas that motivated it.

## adv-v3 (Oct 15): wording-accuracy fix, not a tuning round

- Why: in all 3 accepted adv-v2 reduce_spending drafts (Maya, food delivery) the text said "Reducing this excess
  could improve your balance by $121.80 by 2026-12" and the title "Save $150 Monthly". The projected_balance_delta
  measures the trend stopping (the trend flow held at next month's projected level: 3 x the $40.60 slope by
  2026-12), not cutting the $149.91 excess. The numbers were right; the meaning was overstated. The Verifier
  matches numbers by value, so it cannot catch this (known limitation; Verifier unchanged).
- Change (reduce_spending wording only): the system prompt says to describe the delta as what stopping the rise
  is worth and never as savings from cutting spending, and that excess_spend is not a monthly saving; the
  reduce_spending delta figure's meaning says "if this category stops rising and stays at next month's projected
  level (not if you cut the excess)". Nothing else changed; adv-v1 and adv-v2 stay selectable.
- Result (`eval/results/advice_adv-v3.md`, one run of 3 repeats): all 3 accepted Maya reduce_spending drafts now
  say the gain comes from the rise stopping ("If you stop this rise and maintain next month's projected level...")
  and no title says "Save $150 Monthly". First-draft failure rate 33.3% (6/18) vs adv-v2 22.2% (4/18); the extra
  failures are unclaimed "N days" on cancel drafts, which this change did not touch, so the difference is read as
  run-to-run variation at n = 18, not as an effect. No improvement in the loop metrics is claimed.
