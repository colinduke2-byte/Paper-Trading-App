# Plan to finish (written 2026-11-25, after the usage reset)

Scope is frozen: no new features. Everything open gets finished, combined, reviewed once, documented.

## Working rules (token efficiency)
- At most 4 workers at a time; no wide fan-out.
- Models: Sonnet for mechanical work (merging main into a branch, wiring, docs, test fixes); Opus for
  building and integration; Fable only for reviews of money, storage and security code and the final
  release review.
- Resume the agent or session that already holds the context instead of briefing a new one.
- Reviewers post findings as a comment on the pull request; authors read them there. The lead does not
  relay findings.
- Briefs are short and point at files (ops/HANDOFF.md, the PR) instead of restating them.
- One integration pass and one release review, not a merge-and-recheck per branch.
- The lead reports once per phase, not per notification.

## Phases
1. Release blockers (resumed): stream/threadsafety, stream/rcfixes, stream/cashfix, stream/fixes ->
   PRs to main, merge when checks are green (threadsafety and cashfix get a Fable review).
2. Review fixes: lots (PR 13) and crypto (PR 14) authors finish their fix lists; finish the Fable
   reviews of provider actions (PR 17) and backup (PR 19); authors fix.
3. One integrator (Opus) builds integration/wave2 from main: 17, 19, 13, 14, 18 (ticket UI), 20
   (discovery), stream/hardening, stream/crossbrowser, stream/uxpolish; interaction tests; one PR.
4. One Fable release review of the result; one fix round.
5. Docs (Sonnet): user guide, quick start, in-app Help and known limits brought up to date;
   third-party notices.
6. Owner testing on a real Windows PC and phone, and with real Alpaca keys; one fix round.
7. Tag v1.0 on the owner's go.

## Deferred to the next-version list unless the owner says otherwise
Extended soak runs and benchmarks beyond the quick suite; visual-regression baselines; the usability
review's "nice to have" items; everything each PR lists as descoped.
