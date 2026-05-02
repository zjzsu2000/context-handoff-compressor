#!/usr/bin/env python3
"""ctx - Context Handoff Compressor v0.1 (local, stdlib-only)."""

import os
import re
import sys
from datetime import datetime
from pathlib import Path

# ============================================================
# Constants and defaults
# ============================================================

CTX_DIR = Path(".ctx")
RAW_DIR = CTX_DIR / "raw"
OUTPUTS_DIR = CTX_DIR / "outputs"
CONFIG_FILE = CTX_DIR / "config.yaml"
CURRENT_FILE = CTX_DIR / "current.md"
CHECKPOINT_FILE = CTX_DIR / "checkpoint.md"
RULES_FILE = CTX_DIR / "rules.md"
HANDOFF_FILE = CTX_DIR / "handoff.md"

KEEP_KEYWORDS = [
    "TODO", "Next", "Done", "Completed", "Error", "Failed",
    "Decision", "Do not", "Constraint", "Path", "Repo",
    "Command", "File", "Test", "Risk", "Blocked",
]

DEFAULT_CONFIG = """\
# ctx config (v0.1)
project_name: untitled
default_target_model: claude-opus-4-7
max_handoff_words: 600
output_style: compact
future_cloud_ready: false
"""

DEFAULT_CURRENT = """\
# Current State

## Current goal


## Current project/repo path


## Current task scope


## Important background

"""

DEFAULT_CHECKPOINT = """\
# Checkpoint

## Completed
-

## In progress
-

## Blocked
-

## Next single action
-

## Do not repeat
-
"""

DEFAULT_RULES = """\
# Rules

- Keep output short
- One next action only
- No fake claims
- No invented test results
- No broad refactor
- No code modification unless explicitly requested
- Preserve user-specific constraints
- Prefer evidence from real files/logs
"""

DEFAULT_HANDOFF = """\
# Handoff

(empty - run `python ctx.py handoff` to generate)
"""

SAFE_FALLBACK_ACTION = (
    "Inspect current files and summarize state before modifying anything."
)


# ============================================================
# Storage layer
# ============================================================

def ensure_dirs() -> None:
    CTX_DIR.mkdir(exist_ok=True)
    RAW_DIR.mkdir(exist_ok=True)
    OUTPUTS_DIR.mkdir(exist_ok=True)


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def write_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        return False
    write_text(path, content)
    return True


def now_stamp() -> str:
    # microsecond precision so back-to-back calls produce distinct filenames
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _resolve_max_words(cfg: dict, default: int = 500, minimum: int = 150) -> int:
    raw = cfg.get("max_handoff_words", "")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = default
    if n <= 0:
        n = default
    if n < minimum:
        n = minimum
    return n


def list_raw_files() -> list:
    if not RAW_DIR.exists():
        return []
    return sorted(RAW_DIR.glob("*.md"))


def parse_simple_yaml(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        key, _, val = line.partition(":")
        out[key.strip()] = val.strip()
    return out


def get_config() -> dict:
    if not CONFIG_FILE.exists():
        return parse_simple_yaml(DEFAULT_CONFIG)
    return parse_simple_yaml(read_text(CONFIG_FILE))


# ============================================================
# Markdown parsing helpers
# ============================================================

def parse_sections(md: str) -> dict:
    sections = {}
    title = None
    buf = []
    for line in md.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if title is not None:
                sections[title] = "\n".join(buf).strip()
            title = m.group(1).strip()
            buf = []
        elif title is not None:
            buf.append(line)
    if title is not None:
        sections[title] = "\n".join(buf).strip()
    return sections


def section(sections: dict, *names) -> str:
    for n in names:
        for title, content in sections.items():
            if title.lower() == n.lower():
                return content
    for n in names:
        for title, content in sections.items():
            if n.lower() in title.lower():
                return content
    return ""


def is_empty_or_template(content: str) -> bool:
    s = content.strip()
    if not s:
        return True
    lines = [l.strip() for l in s.splitlines() if l.strip()]
    if not lines:
        return True
    return all(l in ("-", "*", "- ", "* ") for l in lines)


def first_action_line(content: str) -> str:
    for line in content.splitlines():
        s = line.strip()
        if s.startswith(("-", "*")):
            body = s.lstrip("-* ").strip()
            if body:
                return body
        elif s:
            return s
    return ""


# ============================================================
# Compressor layer
# ============================================================

def compress_text(text: str, max_lines: int = 80) -> str:
    keep = []
    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s or s in ("-", "*"):
            continue
        if s.startswith("#"):
            keep.append(line)
            continue
        if s.startswith(("-", "*", "+")) or re.match(r"^\d+\.", s):
            keep.append(line)
            continue
        low = s.lower()
        if any(k.lower() in low for k in KEEP_KEYWORDS):
            keep.append(line)
            continue
    if len(keep) > max_lines:
        keep = keep[:max_lines] + ["[...truncated...]"]
    return "\n".join(keep)


# ============================================================
# Renderer layer
# ============================================================

def _bullets(content: str, fallback: str = "Unknown") -> list:
    if is_empty_or_template(content):
        return [f"- {fallback}"]
    out = []
    for line in content.splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        if s.lstrip().startswith(("-", "*")):
            body = s.lstrip().lstrip("-*").strip()
            if body:
                out.append(f"- {body}")
        else:
            out.append(f"- {s.strip()}")
    return out or [f"- {fallback}"]


_TRUNC_MARKER = "- [...truncated...]"


def _format_block(name: str, body_lines: list) -> str:
    if name == "__preamble__":
        return "\n".join(body_lines)
    return f"{name}:\n" + "\n".join(body_lines)


def _block_words(name: str, body_lines: list) -> int:
    return len(_format_block(name, body_lines).split())


def _shrink_blocks(blocks_in: list, budget: int) -> list:
    """Trim trailing bullets from shrinkable blocks until total words <= budget.

    Each block becomes at minimum a single truncation-marker bullet so the
    section header still appears.
    """
    blocks = [[n, list(b)] for n, b in blocks_in]
    safety = 4000
    while sum(_block_words(n, b) for n, b in blocks) > budget and safety > 0:
        safety -= 1
        candidates = [(i, len(b)) for i, (_, b) in enumerate(blocks) if len(b) > 1]
        if candidates:
            i = max(candidates, key=lambda x: x[1])[0]
            blocks[i][1].pop()
            continue
        # All shrinkables down to one bullet: collapse to truncation markers
        progressed = False
        for blk in blocks:
            if blk[1] != [_TRUNC_MARKER]:
                blk[1] = [_TRUNC_MARKER]
                progressed = True
                break
        if not progressed:
            break
    return [(n, b) for n, b in blocks]


def render_handoff(current: str, checkpoint: str, rules: str, max_words: int) -> str:
    cur_sec = parse_sections(current)
    cp_sec = parse_sections(checkpoint)

    goal = section(cur_sec, "Current goal", "Goal")
    repo = section(cur_sec, "Current project/repo path", "Repo path", "Project path")
    scope = section(cur_sec, "Current task scope", "Task scope", "Scope")
    background = section(cur_sec, "Important background", "Background")

    completed = section(cp_sec, "Completed")
    in_progress = section(cp_sec, "In progress")
    blocked = section(cp_sec, "Blocked")
    next_action = section(cp_sec, "Next single action", "Next")
    do_not = section(cp_sec, "Do not repeat", "Do not")

    rules_lines = []
    for line in rules.splitlines():
        s = line.strip()
        if s.startswith(("-", "*")) and s.lstrip("-* ").strip():
            rules_lines.append(f"- {s.lstrip('-* ').strip()}")
    if not rules_lines:
        rules_lines = ["- Unknown"]

    cur_state = []
    if not is_empty_or_template(repo):
        first = repo.strip().splitlines()[0].lstrip("-* ").strip()
        if first:
            cur_state.append(f"- Repo: {first}")
    if not is_empty_or_template(scope):
        for l in scope.splitlines():
            s = l.strip()
            if not s:
                continue
            body = s.lstrip("-* ").strip() if s.startswith(("-", "*")) else s
            if body:
                cur_state.append(f"- Scope: {body}")
    if not is_empty_or_template(in_progress):
        for l in in_progress.splitlines():
            s = l.strip()
            if s.startswith(("-", "*")):
                body = s.lstrip("-* ").strip()
                if body:
                    cur_state.append(f"- In progress: {body}")
    if not is_empty_or_template(blocked):
        for l in blocked.splitlines():
            s = l.strip()
            if s.startswith(("-", "*")):
                body = s.lstrip("-* ").strip()
                if body:
                    cur_state.append(f"- Blocked: {body}")
    if not cur_state:
        cur_state.append("- Unknown")

    next_line = first_action_line(next_action) or SAFE_FALLBACK_ACTION

    goal_text = goal.strip().splitlines()[0].strip() if goal.strip() else "Unknown"
    goal_text = goal_text.lstrip("-* ").strip() or "Unknown"

    expected_lines = [
        "- Short structured reply: result, evidence, next step.",
        "- No long logs. No speculation. Mark unknowns explicitly.",
    ]

    # Sections split by trim policy. Required sections (Goal / Next single
    # action / Do not / Constraints / Expected output) must always render
    # in full; only the shrinkables get trimmed under budget.
    preamble = [("__preamble__", ["Continue from checkpoint."])]
    head_required = [("Goal", [f"- {goal_text}"])]
    shrinkable = [
        ("Known facts", _bullets(background, "Unknown")),
        ("Completed", _bullets(completed, "Unknown")),
        ("Current state", cur_state),
    ]
    tail_required = [
        ("Next single action", [f"- {next_line}"]),
        ("Do not", _bullets(do_not, "Unknown")),
        ("Constraints", rules_lines),
        ("Expected output", expected_lines),
    ]

    fixed_words = sum(
        _block_words(n, b) for n, b in preamble + head_required + tail_required
    )
    budget = max(0, max_words - fixed_words)
    shrinkable = _shrink_blocks(shrinkable, budget)

    blocks = preamble + head_required + shrinkable + tail_required
    return "\n\n".join(_format_block(n, b) for n, b in blocks)


def render_brief(current: str, checkpoint: str, rules: str, raws: list) -> str:
    parts = ["# Brief", ""]
    if current.strip():
        parts += ["## current.md", compress_text(current, 40), ""]
    if checkpoint.strip():
        parts += ["## checkpoint.md", compress_text(checkpoint, 40), ""]
    if rules.strip():
        parts += ["## rules.md", compress_text(rules, 20), ""]
    for path, raw in raws:
        parts += [f"## raw: {path.name}", compress_text(raw, 30), ""]
    return "\n".join(parts).rstrip() + "\n"


# ============================================================
# Commands
# ============================================================

def _require_ctx() -> None:
    if not CTX_DIR.exists():
        print("Error: .ctx not found. Run: python ctx.py init", file=sys.stderr)
        sys.exit(1)


def cmd_init(_args) -> None:
    ensure_dirs()
    created = []
    for path, content in [
        (CONFIG_FILE, DEFAULT_CONFIG),
        (CURRENT_FILE, DEFAULT_CURRENT),
        (CHECKPOINT_FILE, DEFAULT_CHECKPOINT),
        (RULES_FILE, DEFAULT_RULES),
        (HANDOFF_FILE, DEFAULT_HANDOFF),
    ]:
        if write_if_missing(path, content):
            created.append(str(path))
    print(f".ctx initialized at {CTX_DIR.resolve()}")
    if created:
        print("Created:")
        for c in created:
            print(f"  {c}")
    else:
        print("All files already existed. Nothing overwritten.")
    print()
    print("Next:")
    print("  - edit .ctx/current.md and .ctx/checkpoint.md")
    print("  - run: python ctx.py status")


def cmd_add(_args) -> None:
    _require_ctx()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stdin.isatty():
        print("Error: nothing on stdin. Try: pbpaste | python ctx.py add", file=sys.stderr)
        sys.exit(1)
    data = sys.stdin.read()
    if not data.strip():
        print("Error: empty input.", file=sys.stderr)
        sys.exit(1)
    path = RAW_DIR / f"{now_stamp()}.md"
    write_text(path, data)
    print(f"saved: {path}")
    print(f"chars: {len(data)}")
    print("next: python ctx.py brief   or   python ctx.py handoff")


def cmd_checkpoint(_args) -> None:
    _require_ctx()
    if not CHECKPOINT_FILE.exists() or not read_text(CHECKPOINT_FILE).strip():
        write_text(CHECKPOINT_FILE, DEFAULT_CHECKPOINT)
        print(f"template written: {CHECKPOINT_FILE.resolve()}")
        return
    print(f"checkpoint: {CHECKPOINT_FILE.resolve()}")
    print("edit it directly in your editor.")


def cmd_status(_args) -> None:
    print(f".ctx exists: {CTX_DIR.exists()}")
    if not CTX_DIR.exists():
        print("run: python ctx.py init")
        return
    raws = list_raw_files()
    print(f"raw files: {len(raws)}")
    if raws:
        print(f"latest raw: {raws[-1]}")
    for f in [CONFIG_FILE, CURRENT_FILE, CHECKPOINT_FILE, RULES_FILE, HANDOFF_FILE]:
        print(f"{f}: {'present' if f.exists() else 'missing'}")
    if CHECKPOINT_FILE.exists():
        sec = parse_sections(read_text(CHECKPOINT_FILE))
        nxt = section(sec, "Next single action", "Next")
        line = first_action_line(nxt) if nxt else ""
        if line:
            print(f"next single action: {line}")
        else:
            print("next single action: (none set)")


def cmd_brief(_args) -> None:
    _require_ctx()
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    current = read_text(CURRENT_FILE)
    checkpoint = read_text(CHECKPOINT_FILE)
    rules = read_text(RULES_FILE)
    raws = [(p, read_text(p)) for p in list_raw_files()[-3:]]
    out = render_brief(current, checkpoint, rules, raws)
    out_path = OUTPUTS_DIR / f"brief_{now_stamp()}.md"
    write_text(out_path, out)
    sys.stdout.write(out)
    sys.stdout.write(f"\n[saved: {out_path}]\n")


def cmd_handoff(_args) -> None:
    _require_ctx()
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    max_words = _resolve_max_words(get_config())
    text = render_handoff(
        read_text(CURRENT_FILE),
        read_text(CHECKPOINT_FILE),
        read_text(RULES_FILE),
        max_words=max_words,
    )
    write_text(HANDOFF_FILE, text)
    out_path = OUTPUTS_DIR / f"handoff_{now_stamp()}.md"
    write_text(out_path, text)
    sys.stdout.write(text)
    sys.stdout.write(f"\n\n[saved: {HANDOFF_FILE} and {out_path}]\n")


# ============================================================
# CLI dispatch
# ============================================================

HELP = """\
ctx - Context Handoff Compressor v0.1

Usage:
  python ctx.py init         create .ctx/ and default files
  python ctx.py add          read stdin, save to .ctx/raw/<timestamp>.md
  python ctx.py checkpoint   show or template the checkpoint file
  python ctx.py status       show ctx state and next single action
  python ctx.py brief        deterministic compact brief (stdout + outputs/)
  python ctx.py handoff      copy-paste prompt for next model
  python ctx.py help         show this help

Files:
  .ctx/config.yaml    config
  .ctx/current.md     current goal/state
  .ctx/checkpoint.md  completed/in-progress/blocked/next/do-not
  .ctx/rules.md       rules for the next model
  .ctx/handoff.md     last generated handoff
  .ctx/raw/           pasted raw model replies
  .ctx/outputs/       generated briefs and handoffs
"""

COMMANDS = {
    "init": cmd_init,
    "add": cmd_add,
    "checkpoint": cmd_checkpoint,
    "status": cmd_status,
    "brief": cmd_brief,
    "handoff": cmd_handoff,
}


def main(argv: list) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    cmd = argv[1]
    fn = COMMANDS.get(cmd)
    if not fn:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(HELP)
        return 2
    fn(argv[2:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
