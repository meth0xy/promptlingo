#!/usr/bin/env python3
"""PromptLingo: log language checks from Claude Code and Cursor, show progress, and run reviews."""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import traceback
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("PROMPTLINGO_HOME") or "~/.promptlingo").expanduser()
LOG = HOME / "log.jsonl"
REVIEWS = HOME / "reviews.jsonl"
CONFIG = HOME / "config.json"
ERRORS = HOME / "errors.log"

DEFAULTS = {"language": "English", "explain_in": "English", "practice": "after"}
PRACTICE_MODES = ("before", "after", "off")
CATEGORIES = ("Spelling", "Grammar", "Word choice", "Structure", "Punctuation")
PROGRESS_COMMANDS = {"claude": "/promptlingo:progress", "cursor": "/promptlingo-progress"}
WORDS_RE = re.compile(r"(\d+)\s+words?", re.I)
EXTRA_RE = re.compile(r"\+(\d+)\s+smaller fix", re.I)


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def append_jsonl(path, row):
    HOME.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(**values):
    config = read_config()
    config.update({key: value for key, value in values.items() if value})
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return config


def setting(name, override=None):
    """Resolve a setting from the command line, PROMPTLINGO_*, the Claude Code plugin option, then config.json."""
    key = name.upper()
    candidates = (override, os.environ.get("PROMPTLINGO_" + key),
                  os.environ.get("CLAUDE_PLUGIN_OPTION_" + key), read_config().get(name))
    for value in candidates:
        # An unset plugin option can reach a skill as the literal text "${user_config.language}".
        if isinstance(value, str) and value.strip() and "${" not in value:
            return value.strip()
    return DEFAULTS[name]


def practice_mode(override=None):
    mode = setting("practice", override).lower()
    return mode if mode in PRACTICE_MODES else DEFAULTS["practice"]


def same_language(row, language):
    return str(row.get("language", "")).lower() == language.lower()


def clean_cell(text):
    return re.sub(r"[`*\"]", "", text).strip()


def item_id(wrote, better):
    key = f"{clean_cell(wrote).lower()}|{clean_cell(better).lower()}"
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def digest(text):
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def category_of(why):
    for category in CATEGORIES:
        if why.lower().startswith(category.lower()):
            return category
    return "Other"


def check_block(text, language):
    """Return the lines of the check block that opens a reply, or None."""
    header = re.compile(r"\s*(?:#+\s*)?\**\s*" + re.escape(language) + r"\s+check\b", re.I)
    if not header.match(text):
        return None
    lines = text.strip().splitlines()
    block = [lines[0]]
    for line in lines[1:]:
        if line.strip() == "---":
            break
        block.append(line)
    return block


def parse_check(block, language):
    header = block[0].lower()
    words = WORDS_RE.search(header)
    result = {
        "language": language,
        "words": int(words.group(1)) if words else None,
        "in_language": f"not in {language.lower()}" not in header,
        "items": [],
        "extra": 0,
    }
    if not result["in_language"]:
        return result

    for line in block[1:]:
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0].lower() == "you wrote" or set(cells[0]) <= set("-: "):
            continue
        wrote, better, why = clean_cell(cells[0]), clean_cell(cells[1]), " | ".join(cells[2:])
        result["items"].append({
            "id": item_id(wrote, better),
            "wrote": wrote,
            "better": better,
            "why": why,
            "category": category_of(why),
            "again": "(again)" in why.lower(),
        })

    extra = EXTRA_RE.search("\n".join(block[1:]))
    if extra:
        result["extra"] = int(extra.group(1))
    if not result["items"] and "no corrections" not in header:
        return None
    return result


def own_words(prompt):
    """Count the words the user typed, skipping tagged pastes, code fences and quotes."""
    text = re.sub(r"<(\w[\w-]*)[^>]*>.*?</\1>", " ", prompt, flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    return len(re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)*", text))


def logged_ids():
    return {row.get("id") for row in read_jsonl(LOG)}


def log_reply(text, language, source, session, ts, seen, prompt="", entry_id=None):
    block = check_block(text, language)
    parsed = parse_check(block, language) if block else None
    if not parsed:
        return
    if parsed["words"] is None and parsed["in_language"]:
        parsed["words"] = own_words(prompt)
    key = "\n".join(line.rstrip() for line in block)
    entry_id = entry_id or f"{source}:{session}:{digest(key)}"
    if entry_id in seen:
        return
    row = {"id": entry_id, "ts": ts, "source": source, "session": session}
    row.update(parsed)
    row["fixes"] = len(parsed["items"]) + parsed["extra"]
    append_jsonl(LOG, row)
    seen.add(entry_id)


def message_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(
        b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"
    )


def hook_claude(payload, language):
    session = payload.get("session_id")
    seen = logged_ids()
    prompt = ""
    path = Path(payload.get("transcript_path") or "")
    if path.is_file():
        with path.open(encoding="utf-8") as f:
            for line in f:
                if '"user"' not in line and "check" not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("isSidechain") or row.get("isMeta"):
                    continue
                content = (row.get("message") or {}).get("content")
                if row.get("type") == "user":
                    text = message_text(content)
                    if text.strip():
                        prompt = text
                elif row.get("type") == "assistant" and isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            log_reply(block.get("text", ""), language, "claude", session,
                                      row.get("timestamp") or now(), seen, prompt)

    # The transcript is written asynchronously, so the last message of the turn may be missing from it.
    last = payload.get("last_assistant_message")
    if last:
        text = last if isinstance(last, str) else message_text(last)
        log_reply(text, language, "claude", session, now(), seen, prompt)


def hook_cursor(payload, language):
    generation = payload.get("generation_id")
    log_reply(payload.get("text") or "", language, "cursor", payload.get("conversation_id"), now(),
              logged_ids(), entry_id=f"cursor:{generation}" if generation else None)
    print("{}")


def mistake_groups(language):
    groups = {}
    for row in read_jsonl(LOG):
        if not same_language(row, language):
            continue
        for item in row.get("items", []):
            group = groups.setdefault(item["id"], {"id": item["id"], "count": 0, "first": row["ts"], "last": row["ts"]})
            group["count"] += 1
            group["last"] = max(group["last"], row["ts"])
            group.update(wrote=item["wrote"], better=item["better"], why=item["why"], category=item["category"])

    reviews = defaultdict(list)
    for review in read_jsonl(REVIEWS):
        reviews[review["id"]].append(review)
    for group in groups.values():
        history = reviews[group["id"]]
        since_last_slip = [r for r in history if r["ts"] > group["last"]]
        group["mastered"] = len(since_last_slip) >= 2 and all(r["result"] == "pass" for r in since_last_slip[-2:])
        group["fails"] = sum(r["result"] == "fail" for r in history)
        group["last_review"] = history[-1]["ts"] if history else None
    return groups


def render_rule(language, explain_in, tool, practice):
    examples = ROOT / "rules" / "examples"
    example = examples / (re.sub(r"\W+", "-", language.lower()).strip("-") + ".md")
    note = ""
    if not example.is_file():
        example = examples / "english.md"
        note = "The rows in this example are English. Yours are about {{language}}.\n\n"
    text = (ROOT / "rules" / "promptlingo.md").read_text(encoding="utf-8")
    practice_text = (ROOT / "rules" / "practice" / f"{practice}.md").read_text(encoding="utf-8")
    text = text.replace("{{practice}}", practice_text.strip())
    text = text.replace("{{example_note}}", note)
    text = text.replace("{{example}}", example.read_text(encoding="utf-8").strip())
    values = {"language": language, "explain_in": explain_in, "progress_command": PROGRESS_COMMANDS[tool]}
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def recurring_mistakes(language, limit):
    groups = [g for g in mistake_groups(language).values() if g["count"] >= 2 and not g["mastered"]]
    groups.sort(key=lambda g: (g["count"], g["last"]), reverse=True)
    if not groups:
        return ""
    lines = [f'- "{g["wrote"]}" → "{g["better"]}" ({g["category"]}, {g["count"]}x)' for g in groups[:limit]]
    return (f"PromptLingo: the user's recurring {language} mistakes from earlier sessions. "
            "When one comes up again, its Why cell gets (again).\n" + "\n".join(lines))


def context(args):
    language = setting("language", args.language)
    parts = []
    if args.rule:
        parts.append(render_rule(language, setting("explain_in", args.explain_in), args.format,
                                 practice_mode(args.practice)))
    recurring = recurring_mistakes(language, args.limit)
    if recurring:
        parts.append(recurring)
    text = "\n\n".join(parts)
    if args.format == "cursor":
        print(json.dumps({"additional_context": text} if text else {}, ensure_ascii=False))
    elif text:
        print(text)


def reminder(args):
    language = setting("language", args.language)
    text = (f"PromptLingo is on. The user is practicing {language}, so this reply opens with "
            f"the {language} check block described in the session rules.")
    if practice_mode(args.practice) == "before":
        text += (" Practice before the task is on: when the check has corrections, the reply stops after the "
                 "practice sentence and the task waits. If this message retypes the last practice sentence "
                 "with the fix, or says skip, the original task starts now, without a new check block.")
    print(text)


def rule(args):
    print(render_rule(setting("language", args.language), setting("explain_in", args.explain_in), args.format,
                      practice_mode(args.practice)))


def rate(rows):
    counted = [r for r in rows if r.get("words")]
    words = sum(r["words"] for r in counted)
    return 100 * sum(r["fixes"] for r in counted) / words if words else None


def fmt_rate(value):
    return "n/a" if value is None else f"{value:.1f}"


def pct(part, whole):
    return f"{100 * part / whole:.0f}%" if whole else "n/a"


def stats(args):
    language = setting("language", args.language)
    all_rows = sorted((r for r in read_jsonl(LOG) if same_language(r, language)), key=lambda r: r["ts"])
    rows = [r for r in all_rows if r.get("in_language", True)]
    if not rows:
        print(f"No {language} checks logged yet.")
        return

    other = len(all_rows) - len(rows)
    words = sum(r.get("words") or 0 for r in rows)
    fixes = sum(r["fixes"] for r in rows)
    clean = sum(r["fixes"] == 0 for r in rows)
    print(f"## {language} progress\n")
    print(f"- Prompts checked: {len(rows)}" + (f" (+{other} not in {language})" if other else ""))
    print(f"- Words: {words:,} · Fixes: {fixes}")
    print(f"- Error rate: {fmt_rate(rate(rows))} fixes per 100 words · Clean prompts: {pct(clean, len(rows))}")
    if len(rows) >= 10:
        window = min(20, len(rows) // 2)
        before, after = rate(rows[:window]), rate(rows[-window:])
        if before is not None and after is not None:
            verdict = "better" if after < before * 0.9 else "worse" if after > before * 1.1 else "flat"
            print(f"- Trend: first {window} prompts {before:.1f} → last {window} prompts {after:.1f} ({verdict})")

    weeks = defaultdict(list)
    for r in rows:
        year, week, _ = dt.date.fromisoformat(r["ts"][:10]).isocalendar()
        weeks[f"{year}-W{week:02d}"].append(r)
    print("\n| Week | Prompts | Fixes per 100 words | Clean |\n|---|---|---|---|")
    for week in sorted(weeks)[-args.weeks:]:
        w = weeks[week]
        print(f"| {week} | {len(w)} | {fmt_rate(rate(w))} | {pct(sum(r['fixes'] == 0 for r in w), len(w))} |")

    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=14)).strftime("%Y-%m-%d")
    total, recent = Counter(), Counter()
    for r in rows:
        for item in r.get("items", []):
            total[item["category"]] += 1
            if r["ts"][:10] >= cutoff:
                recent[item["category"]] += 1
    print("\n| Category | All time | Last 14 days |\n|---|---|---|")
    for category, count in total.most_common():
        print(f"| {category} | {count} | {recent[category]} |")

    groups = sorted(mistake_groups(language).values(), key=lambda g: (g["count"], g["last"]), reverse=True)
    print("\n| Most frequent mistakes | Better | Times | Last seen | Status |\n|---|---|---|---|---|")
    for g in groups[: args.top]:
        status = "mastered" if g["mastered"] else "learning"
        print(f"| {g['wrote']} | {g['better']} | {g['count']} | {g['last'][:10]} | {status} |")


def quiz(args):
    yesterday = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    groups = [g for g in mistake_groups(setting("language", args.language)).values()
              if not g["mastered"] and (g["last_review"] or "") < yesterday]
    groups.sort(key=lambda g: (g["count"] + 2 * g["fails"], g["last"]), reverse=True)
    keys = ("id", "wrote", "better", "why", "category", "count", "fails")
    print(json.dumps([{k: g[k] for k in keys} for g in groups[: args.n]], ensure_ascii=False, indent=2))


def review(args):
    if args.id not in mistake_groups(setting("language", args.language)):
        sys.exit(f"Unknown mistake id: {args.id}")
    append_jsonl(REVIEWS, {"ts": now(), "id": args.id, "result": args.result})
    print(f"Recorded {args.result} for {args.id}.")


def config(args):
    if args.practice and args.practice.lower() not in PRACTICE_MODES:
        sys.exit(f"--practice must be one of: {', '.join(PRACTICE_MODES)}")
    if args.language or args.explain_in or args.practice:
        save_config(language=args.language, explain_in=args.explain_in,
                    practice=args.practice and args.practice.lower())
    print(f"language: {setting('language')}\nexplain_in: {setting('explain_in')}\n"
          f"practice: {practice_mode()}\nlog: {LOG}")


def run_safely(action, name):
    """Hooks must never break a session, so failures go to errors.log instead."""
    try:
        action()
    except Exception:
        HOME.mkdir(parents=True, exist_ok=True)
        with ERRORS.open("a", encoding="utf-8") as f:
            f.write(f"{now()} {name}\n{traceback.format_exc()}\n")


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--language", help="language being learned (default: plugin option, then config.json)")
    common.add_argument("--explain-in", dest="explain_in", help="language of the explanations")
    common.add_argument("--practice", help="when to retype the corrected sentence: before, after or off")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, text):
        return sub.add_parser(name, help=text, parents=[common])

    add("hook-claude", "Claude Code Stop hook")
    add("hook-cursor", "Cursor afterAgentResponse hook")
    add("reminder", "Claude Code UserPromptSubmit hook")
    p = add("context", "session start context: recurring mistakes, and the rule with --rule")
    p.add_argument("--rule", action="store_true")
    p.add_argument("--format", choices=("claude", "cursor"), default="claude")
    p.add_argument("--limit", type=int, default=8)
    p = add("rule", "print the coaching rule")
    p.add_argument("--format", choices=("claude", "cursor"), default="claude")
    p = add("stats", "progress report")
    p.add_argument("--weeks", type=int, default=8)
    p.add_argument("--top", type=int, default=10)
    p = add("quiz", "mistakes due for review, as JSON")
    p.add_argument("--n", type=int, default=5)
    p = add("review", "record a review result")
    p.add_argument("id")
    p.add_argument("result", choices=("pass", "fail"))
    add("config", "show the settings, or save --language, --explain-in and --practice")
    args = parser.parse_args()

    hooks = {"hook-claude": hook_claude, "hook-cursor": hook_cursor}
    commands = {"context": context, "reminder": reminder, "rule": rule, "stats": stats,
                "quiz": quiz, "review": review, "config": config}
    if args.command in hooks:
        language = setting("language", args.language)
        run_safely(lambda: hooks[args.command](json.load(sys.stdin), language), args.command)
    elif args.command in ("context", "reminder"):
        run_safely(lambda: commands[args.command](args), args.command)
    else:
        commands[args.command](args)


if __name__ == "__main__":
    main()
