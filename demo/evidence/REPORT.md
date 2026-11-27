# Evidence summary — agent demo (2026-11-26)

All numbers come from the eval harness (`evals/`), which grades each run against **ground truth computed by direct SQL**
(`evals/ground_truth.json`) and attaches the platform's own records (Agent Runtime events, outbound gateway rows,
IAP authorization decisions, Model Armor records) to each run.

| Set | Runs | Passed | Notes |
|---|---|---|---|
| Scripted ×10 (Model Armor PI at HIGH) | 60 | **60/60** | `evals/results/REPORT-scripted-x10.reread.md` |
| Scripted ×3 (final config, PI at MEDIUM) | 18 | **18/18** | `evals/results/REPORT-scripted-pimed-x3.md` |
| Benign business questions (final config) | 23 | **23/23** | 0 wrong blocks, incl. contact-wording prompts that are then denied outbound |
| Attacks (final config) | 11 | **11/11** blocked inbound | injection, hidden-tag injection, jailbreaks, system-prompt extraction, SSN ×2, CPF ×2, malicious/phishing URL |

- **Outbound deny** of `get_customer_contact`: confirmed by an IAP decision record (`granted: false`, status 7, this agent's principal) in 100% of deny runs. The gateway request row appeared in 3/10 (10× run), 2/2 (benign) and 2/3 (×3). No answer ever contained an email address.
- **Inbound blocks**: HTTP 403 "Model Armor: Prompt violates content security configurations" in 0.15–0.26 s, with a `SANITIZE_USER_PROMPT` MATCH_FOUND record naming the filter (`pi_and_jailbreak`, `sdp` with `US_SOCIAL_SECURITY_NUMBER`/`BRAZIL_CPF_NUMBER`, `malicious_uris`).
- **Latency (answered)**: first event p50 3.1 s / p95 3.7 s; total p50 10.6 s / p95 16.8 s.
- **Before/after checks**: see `CONTROLS.md` (1 inspect-only, 2 no-gateway, 3 blocked prompt leaves an empty session, 4 no-token MCP call).
- **Raw platform records** from individual runs: `raw-run/`.
