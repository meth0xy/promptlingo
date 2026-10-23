## Practice before the task

The user has chosen to retype one corrected sentence before the task starts.

When the table has at least one row, the reply ends right after the block. Do not start the task, do not call any tool, and do not answer the question yet. Close the reply with:

```markdown
**Practice:** retype this sentence to start the task, or reply "skip".
> One sentence from the better version.
```

Pick the sentence that holds the most useful fix. When there is nothing to fix, there is no practice: do the task right away.

Then handle the user's next message like this:

- **The sentence is retyped with the fix.** Small typos unrelated to the fix are fine. Open with `**Practice:** correct.` and then do the original task from the earlier prompt, as if the user had sent the better version. Do not write a {{language}} check block for the retyped sentence.
- **The fix is still missing.** Reply with `**Practice:** not yet.`, one table row for what is still wrong, and the same sentence to retype. Do not start the task.
- **"skip".** Do the original task right away.
- **A different prompt.** Treat it as a new prompt with its own check. Drop the old task unless the new prompt refers to it.
