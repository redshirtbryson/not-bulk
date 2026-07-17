# Egress manifest — outbound destinations reachable from the worker/pipeline

| Destination | Purpose | Data sent |
| --- | --- | --- |
| discord.com | Sanitized error/batch-complete notifications (`notbulk/discord.py`) | Embed title, level color, sanitized field values (error class name, job/batch ids) — never the webhook URL, raw exception text, or user content |
