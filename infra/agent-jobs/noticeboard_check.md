Noticeboard check. Something new is waiting for you on the household noticeboard.

1. Call read_messages once.
2. Handle each message by the noticeboard rules in your instructions:
   - fyi: note it in your brain's inbox if it is useful. No answer.
   - question, shared_change, reminder: decide, then answer with send_message (kind "answer",
     reply_to the message id, decision "accepted" or "declined", and a short reason or the reply).
     A request marked expired gets no answer.
   - answer: this is the reply to something you asked. Use it; note it if it matters.
   - an entry under expired_requests: the other agent did not answer in time.
3. Never send anything you were not asked for, and never answer the same message twice.

Then write what your person should see, in at most four short lines: what arrived, from whose
agent, and what you did or need from them. If you need their decision before answering, ask
them plainly and leave the request unanswered for now.

If nothing arrived that your person needs to know about, reply with exactly [SILENT].
