## If the argument is `running` or `status`

Run `{{DUET}} status` and report it in one short paragraph: is it still going, which round,
who it is waiting on, what they are arguing about, and whether the gate is passing.
Nothing else. Do not start a new session.

If instead you need to block until a `--detach`ed session finishes — before reporting
back, say — use `{{DUET}} wait <id>` rather than polling `status` in a loop or tailing its
log and grepping for "ERROR": an agent's own message can contain that word without the
session having ended. `wait` exits 0 on consensus and nonzero otherwise, so its exit code
alone tells you how it went.

## If the argument is `review`

Run `{{DUET}} review{{REVIEWER}}` to have the other model review the current diff. Report the
findings and say which ones you agree with. You are allowed to disagree with the reviewer —
say so and why. Do not silently act on a finding you think is wrong.

## Otherwise — hand the work over

