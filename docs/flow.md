# Flow: sprint tickets to Ready for dev, then through the bots

## Bot slots
At most four bots run at once:
- 1 dev bot. Works one ticket at a time.
- 1 QA or staff bot.
- 1 review or performance bot.
- 1 QA bot.

The dev bot can start the next ticket as soon as a ticket reaches "Deploying to dev".

## 1. Sprint entry: ordering (staff bot)
When a ticket enters the active sprint (from the hopper, or at sprint start):
- The staff bot decides the order the sprint should be worked in.
- If a ticket needs another ticket's code first, the staff bot records that dependency and creates a
  Jira "Blocks" link (prerequisite blocks dependent). The link is mirrored in `ticket_link`.
- Tickets whose order doesn't matter go last.

## 2. Hopper: refinement (product and staff bots, taking turns)
A ticket in the hopper is refined until it is ready for a developer to pick up.
- Product bot sets the intent: what the change is for, and what done looks like.
- Staff bot sets the technical direction: where the change goes and how to build it.
- They alternate. Each round is recorded as a comment on the ticket.
- "Satisfied" means both bots agree the ticket has enough intent and technical direction for a dev
  to start without asking questions. Then the ticket moves to Ready for dev.
- After 7 rounds without agreement, the ticket is flagged for a human.

## 3. Dev bot
- Picks up a ticket in Ready for dev, or a kicked-back ticket.
- A kicked-back ticket is picked up as soon as the dev bot is idle or finishes its current ticket.
- If the kicked-back ticket is a prerequisite of the current ticket (a "Blocks" link), the dev bot
  pauses the current ticket, works the kicked-back ticket, and waits for it to merge to dev before
  resuming.

## Open after this doc
- The code does not do any of the above yet. The current harness moves a sprint ticket to Ready for
  dev one at a time, with no ordering, refinement, links, or bot slots.
