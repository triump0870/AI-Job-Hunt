# Hybrid domain classification

Why the deterministic domain filter rejected relevant jobs, what replaced it, and how to decide to switch it on.
The rules a change has to keep are in `AGENTS.md` (Discovery quality).

## Why it rejected relevant jobs

Measured on one account's real data (868 jobs, 483 hidden):

| Cause | Effect |
| --- | --- |
| Mass cleanup counted as dislike | 1,020 of 1,055 `DELETED` feedback events fell on three days |
| "Keep the shortest phrase" in `learn()` | all 148 learned title rules are one word: `data`, `machine`, `ml`, `technical`, `native` |
| A learned word beats a role word in `assess` | "Staff Software Engineer, Growth" is hidden though it is a target role |
| Search scope matches words | "Artificial Intelligence Engineer" misses a search for "AI Engineer"; "LLMs" misses `llm` |
| A learned company hides everything | 3 deletes hide a whole employer |
| Nothing is re-checked | a rule that later turns itself off leaves its jobs hidden |

## The pipeline

```
new job
 ├ manual / KEPT / include term ──────────────────────────► forward, no model
 ├ 1. HARD rules (unambiguous only) ──────────────────────► hidden, reason recorded
 ├ 2. POSITIVE shortcut (clear match, no doubt signals) ──► forward, no model
 └ 3. queue (async, cached, lease-guarded) → fast model → evidence gate in Python
        ├ MATCH, or UNCERTAIN with the candidate's title and two of their skills ► existing path: resume → recovery → parse → rank
        ├ still ambiguous ► waits in Discovery → Needs review; nothing is spent until the person chooses
        └ NO_MATCH (evidence holds) ► hidden, reason and evidence shown, Keep still works
```

## Rolling it out

1. Apply the migration. `DOMAIN_CLASSIFIER_MODE` stays `off`; nothing changes.
2. In Models & routing, check that the FAST tier maps to a low-latency model without heavy reasoning output. A call should be
   about 1k tokens, against roughly 24k for the parse and ranking it can save.
3. Label a sample of hidden jobs: `python scripts/domain_classifier_eval.py --user 1 --export-labels 80`, fill in `relevant`
   and save it as `data/real/domain_labels.csv`. Without this, recall is measured only on jobs the old filter let through.
4. Measure, with a stated budget: `python scripts/domain_classifier_eval.py --user 1 --live --max-calls 150`.
5. Optionally watch new imports: `DOMAIN_CLASSIFIER_MODE=shadow` and `DOMAIN_CLASSIFIER_SHADOW_DAILY_LIMIT=50`. Running workers →
   Domain check lists where the two filters disagree. No job changes.
6. **The bar for `on`:** on real labels, a false-negative rate of at most 2% and no higher than the old filter's, and `NO_MATCH`
   precision of at least 90%.
7. `DOMAIN_CLASSIFIER_MODE=on`, then Settings → Domain filter → Re-check hidden jobs (it uses the model for each one).

To roll back, set the mode to `off`: jobs still waiting are released unhidden by the next pass, and the old filter decides again.

## Reading the numbers

* `agent_eval --suite domain_classifier` is a regression gate over synthetic jobs and recorded answers written by the
  same person as the labels. It shows what code does with an answer. It says nothing about how good a real model is.
* Labels from a person's actions measure interest, not domain: rejecting a "Lead AI Engineer" over its location is an
  irrelevant label for a job the classifier is right to keep. Precision reads low for that reason; prefer hand labels.
* A deleted job has no description left, so its row is title-only, and the gate does not let a bare technical title be
  rejected. Such rows can never show a hybrid false negative on a technical title.

## Operating limits (mode `on`)

* **Daily budget:** `DOMAIN_CLASSIFIER_DAILY_LIMIT` (default 2000, 0 = none) model calls per account per day. Jobs over it
  wait as PENDING, unhidden and unranked, and `sweep` queues them as the rolling count falls.
* **Review deadline:** a job in Needs review longer than `DOMAIN_CLASSIFIER_REVIEW_DAYS` (default 7, 0 = never) goes on like
  any uncertain job, so an unanswered review cannot silently remove it. This is a deliberate refinement of the AGENTS.md
  recall rule: review is a bounded delay, not an exclusion.
* **Turning the mode off:** queued checks are skipped without a model call, and `sweep` hands every PENDING and REVIEW job to
  the old filter, which keeps or hides it exactly as it does with the mode off.
* **Evidence gate:** a NO_MATCH quote must be a phrase (two or more words, at least 8 characters, not only filler words), and
  for a posting with a body at least one quote must come from the body, not the title.
