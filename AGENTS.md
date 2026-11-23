# AGENTS.md

## Agent Spec

1. Everything in text. Any discussion about product and implementation plans, decisions and conversation records should be documented. That way, you can understand my taste and the direction of product evolution from historical documents.
2. Break each step down into actions as specifically as possible, use as many Workers as possible, and parallelize as much of the work as possible. Ensure that within every 15-minute window, at least one valid submission is successfully passed, through the high-speed submission (feat: ...) and high-speed fix process (fix: ...) to improve development efficiency. 
3. For every features, there is no need to run a full-brightness test before commit; a partial verification is sufficient. Speed up!
4. The state of reconciliation and the environment should both be written into the Git repository — either in a database or in a reviewable, replayable text log (maybe a .jsonl file).