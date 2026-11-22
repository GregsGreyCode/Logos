Weekly digest for your person. Work only inside /opt/data/brain and only read; change nothing.
Your reply is the digest: it is delivered to your person by this job. Do not send it, or anything
from it, to anyone else: no noticeboard message, no other agent, no other chat.

1. Run `date` to get today's date. The week is the last 7 days.
2. Read the daily notes in daily/ for those days, and run
   `git -C /opt/data/brain log --since="7 days ago" --name-only --pretty=format:` to see which
   pages changed. Open the changed pages under people/, projects/ and topics/ that matter.
3. Read topics/open-questions.md and topics/lessons.md if they exist.

Then write the digest for a phone screen, at most 12 short lines, no emoji:

- First line, in bold: "Your week" and the date range ("**Your week, 13 to 19 November**").
- "New this week" on a line of its own, then "- " bullets: the 3 to 5 most useful things that
  were learned or decided. Facts, not activity ("Wedding speech: first draft outline agreed",
  not "edited a page").
- "Still open": up to 3 questions or loose ends, saying who each one waits on ("Greg: book the
  boiler service"), if any.
- "What I'm doing better": up to 2 lessons added to topics/lessons.md this week, each as what you
  now do differently ("I check whether a company is public before answering"), if any.
- "Coming up": anything with a date in the next 7 days, with the day ("Saturday 21: dinner at
  Sam's"), if any.

Never mention the brain, pages, files or folders by name; say what they contain.
Use only what is written in the brain. Do not invent or pad. Leave out a section that has
nothing in it. If nothing at all was added this week, reply with exactly [SILENT].
