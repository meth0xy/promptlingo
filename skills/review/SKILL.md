---
name: review
description: Show the user's progress in the language they are learning and quiz them on their past mistakes. Use when the user runs /language-coach:review, asks how their English, German, Spanish or other language is improving, asks for their progress, or wants to be quizzed on their mistakes.
---

# Language review

The Language Coach hooks log every ${user_config.language} check to `~/.language-coach/log.jsonl`. This skill reads that log with the coach script. Every command below ends with `--language "${user_config.language}"`.

Write everything in this skill in correct, complete sentences, even when a terse output style is active. Write explanations in ${user_config.explain_in}.

## 1. Progress

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/coach.py" stats --language "${user_config.language}"` and show the output unchanged. Below it, add at most two sentences: is the error rate going down, and which category needs the most work.

If it prints `No ... checks logged yet.`, say so and stop. If the user only asked for progress, stop here.

## 2. Quiz

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/coach.py" quiz --n 5 --language "${user_config.language}"`. It prints the mistakes that are due, as JSON, each with an `id`. If the list is empty, say nothing is due right now and stop.

For each item, write one new short sentence in ${user_config.language} from everyday dev work (bugs, PRs, tickets, meetings) that contains the same mistake. Do not reuse the original wording, so the user practices the rule and not the sentence. Send all of them in one message as a numbered list and ask the user to reply with the corrected sentences, numbered.

## 3. Grade

An answer passes when that mistake is fixed. Other slips do not fail it, but point them out. Record every result:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/coach.py" review <id> pass --language "${user_config.language}"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/coach.py" review <id> fail --language "${user_config.language}"
```

Show the result as a table (`# | Your answer | Correct | Result`), then the score, for example `4/5`. A mistake counts as mastered after two passes in a row and leaves the quiz until it shows up again.
