Weekly digest for your person. Work only inside /opt/data/brain and only read; change nothing.

1. Run `date` to get today's date. The week is the last 7 days.
2. Read the daily notes in daily/ for those days, and run
   `git -C /opt/data/brain log --since="7 days ago" --name-only --pretty=format:` to see which
   pages changed. Open the changed pages under people/, projects/ and topics/ that matter.
3. Read topics/open-questions.md if it exists.

Then write the digest, in plain text for a phone screen, at most 12 short lines:

- First line: "Your week in brief" and the date range.
- "New this week": the 3 to 5 most useful things that were learned or decided. Facts, not
  activity ("Wedding speech: first draft outline agreed", not "edited a page").
- "Still open": up to 3 questions or loose ends waiting on them, if any.
- "Coming up": anything with a date in the next 7 days, if any.

Use only what is written in the brain. Do not invent or pad. Leave out a section that has
nothing in it. If nothing at all was added this week, reply with exactly [SILENT].
