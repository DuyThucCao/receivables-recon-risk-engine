# Five domain questions

Answer in words first (as in an interview), then with SQL. Solutions: `answers.md` and
`solutions/domN_*.sql`.

1. **Revenue cycle.** Walk through the life of one claim that was denied, appealed and finally
   paid: which documents and events appear, in what order, and what happens to the line's balance
   at each step? Find such a claim in `core.fact_claim_event` and show its remittance postings.

2. **Remittance codes.** What do the adjustment group codes CO and PR mean, and why does the
   difference matter to a hospital's cash? Which reason codes carry the most dollars here, and
   what share of all adjustments is contractual (CO-45)?

3. **Borrowing base.** What is a borrowing base, which receivables are usually ineligible, and
   why? Compute a simple one from `mart.ar_aging` at the as-of date and explain why the result is
   nearly zero.

4. **IBNR.** What is IBNR, how does a development triangle estimate it, and what assumptions does
   chain-ladder make? Build a quarterly paid triangle and the age-to-age factors in SQL.

5. **Prompt pay.** What is a prompt-pay rule, why do payers' late payments have a value, and how
   would you measure it? For clean claims, compute each payer's share paid after 30 days and the
   interest a 12% annual clause would add (both illustrative).
