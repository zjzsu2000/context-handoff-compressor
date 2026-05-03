# context-handoff-compressor

Local CLI that compresses context for stateless LLM handoffs.

Stores durable state in `.ctx/` and prints compact, copy-paste prompts
when you switch between Claude Code, Codex, ChatGPT, or any other
model.

Python 3.10+, standard library only. No external dependencies.

## Commands

| Command                      | What it does                                            |
|------------------------------|---------------------------------------------------------|
| `python3 ctx.py init`        | create `.ctx/` and default files (non-destructive)      |
| `python3 ctx.py add`         | read stdin, save to `.ctx/raw/<timestamp>.md`           |
| `python3 ctx.py checkpoint`  | template the checkpoint file if empty                   |
| `python3 ctx.py status`      | show ctx state and current next single action           |
| `python3 ctx.py brief`       | deterministic compact brief (stdout + `.ctx/outputs/`)  |
| `python3 ctx.py handoff`     | copy-paste prompt for the next model                    |

## Typical flow

```bash
python3 ctx.py init
# edit .ctx/current.md and .ctx/checkpoint.md

# (optional) archive the previous model's reply
pbpaste | python3 ctx.py add

# generate the prompt for the next model
python3 ctx.py handoff
```

The handoff prompt always contains these required sections: **Goal**,
**Next single action**, **Do not**, **Constraints**, **Expected
output**. Under the word cap (`max_handoff_words` in
`.ctx/config.yaml`, default 500, minimum 150), noisy sections (Known
facts / Completed / Current state) shrink first; required sections
are never dropped.

## Status

v0.1 — local-only, single file (`ctx.py`), stdlib-only.
