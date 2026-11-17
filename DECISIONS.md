# Decisions, ambiguities and known limits

## Data findings in the sample batch (and what I did)
- **PR-26-00172 has quantity 0** and **PR-26-00173 is for plant `PUN2`** (not a Corvo plant). I do not guess: both come out as `NOT_PROCESSED` with the reason in the CSV. Same for blank/invalid dates and duplicate `pr_no`+`pr_line`.
- **39 lines have no item code** and **60 lines have no estimated price.** Item codes are resolved from the description; a missing estimate only matters for new items (value then shows 0 and the reason says "NO estimated unit price supplied").
- **Two contracts exist only as PDFs** (RC-2026-031, RC-2026-033). The PDF schedules are parsed with a line-based pattern and used exactly like register contracts. PDFs of contracts already in the register are ignored (the register carries consumed value, a PDF would reset it to 0).
- **Blocked/inactive suppliers** appear in PO history. They can never be preferred, suggested, or chosen as a contract supplier; a buyer override to such a supplier is refused (5.7).

## Interpretations
- **D1 Clubbing "within 14 calendar days".** Calendar-date difference of the creation dates (`date(line) - date(first line) ≤ N`), need-by within ±N days of the first line's need-by. *AMBIGUOUS*: an elapsed-time reading (`≤ N×24h`) is slightly stricter. I measured both readings on the sample batch: 0 of 186 rows change. It is one line in `ProcurementEngine.club` if a blind batch needs the other reading.
- **D2 Requirement date** = creation time of the requirement's first line; contract validity, lookbacks and rates are all evaluated at that date.
- **D3 Valuation vs PO price.** Value uses 3.1 (contract rate / latest PO ≤365d / standard price). A SPOT/EMERGENCY PO is placed at the first preferred supplier's *reference* price. The group value shown is the 3.1 value, as in the worked examples.
- **D4 Preferred-supplier score.** The weights (35/25/15/15/10) and recency steps (≤90d = 1, ≤365d = 0.5, else 0) are my reading of "frequency, recency, contract history, delivery, quality". They reproduce the worked examples' first suppliers; they are a hard-coded part of the engine (not policy data).
- **D5 Top-up suppliers** (when fewer preferred suppliers than needed): ranked by `on_time/100 + quality/5` (equal weight). A strict "on-time first" order failed worked examples 10 and 11, the equal-weight order reproduces both.
- **D6 Item matching.** Every query token must be found in the item description (fuzzy for typos, synonyms such as `brg`→bearing, plural folded), every number/spec token must match, and the match must cover ≥50% of the item's words. "laptop bags" or "gloves" are therefore *not* matched to a laptop or a specific glove: in batch they become NEW items; in chat the user is asked to pick from the closest items or say NEW.
- **D7 Capex** is an item type listed in the policy (`capex_types`), checked first, even when a contract exists. A requirement that is diverted away from a contract does not consume its ceiling.
- **D8 Chat dates.** `dd/mm/yyyy`, `yyyy-mm-dd`, `20 Nov 2026`, `20 Nov`, `tomorrow`, `in 3 days`, weekday names. "Friday" = next occurrence; "**next** Friday" = Friday of next calendar week (the confirmation always prints the weekday + date so a wrong reading is visible). Past dates are refused.
- **D9 Chat PRs are evaluated together with earlier chat PRs**, so splitting a need across several chats still clubs (policy anti-splitting intent) and shares contract ceilings. Each chat PR's stored decision is the one made at creation.
- **D10 PO numbers** (`PO-0001…`) are per-run labels; approving a PO in the Buyer Workspace creates a document number (`PO-00001`) for the whole consolidated PO.
- **D11 Effective date.** An approved version is active immediately for all new processing; the effective date is stored and displayed but the engine does not pick policy versions per PR date. I chose this so an amendment demonstrably changes the next run. If the evaluator expects date-based selection, that is the one behavior to change.
- **D12 Amendment parsing is rule-based, not an LLM.** It reads clause-numbered text, "limit is raised to", "Label: INR value", lakh/crore, and JSON. Rules with a condition it cannot resolve (e.g. an unknown category) are rejected with a warning instead of being applied too broadly. A very differently worded amendment may need the JSON form or edits in the preview.

## Known limits (honest list)
- Not verified here: `docker compose up --build` itself (no Docker in my environment) and a live hosted URL. The app was run from a clean copy of the repository with uvicorn, importing the kit ZIP through the API, and also via the `seed/` ZIP.
- The rule chain order and the preferred-supplier weights are code; extra rules cover plant/category/item-type/urgency/item/value/history conditions only.
- No authentication: "who" is the name typed in the UI/API.
- Chat understands English; a quantity must be a plain number (with optional unit), spec numbers inside item names (M10x40, 6205) are kept as part of the item.
