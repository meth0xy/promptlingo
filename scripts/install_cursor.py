#!/usr/bin/env python3
"""Install or remove Language Coach for Cursor: the rule, the review skill, and the hooks."""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coach  # noqa: E402

CURSOR = Path.home() / ".cursor"
RULE = CURSOR / "rules" / "language-coach.mdc"
SKILL = CURSOR / "skills" / "language-review" / "SKILL.md"
HOOKS = CURSOR / "hooks.json"
SCRIPT = Path(coach.__file__).resolve()


def render_rule(language, explain_in):
    body = coach.render_rule(language, explain_in, "cursor")
    return (f"---\ndescription: The user is learning {language}. Coach their {language} first, then do the task.\n"
            f"alwaysApply: true\n---\n\n{body}")


def render_skill(language, explain_in):
    text = (coach.ROOT / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8")
    replacements = {
        "${CLAUDE_PLUGIN_ROOT}": str(coach.ROOT),
        "${user_config.language}": language,
        "${user_config.explain_in}": explain_in,
        "/language-coach:review": "/language-review",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"^name: review$", "name: language-review", text, count=1, flags=re.M)


def is_ours(entry):
    return "/scripts/coach.py" in entry.get("command", "")


def load_hooks():
    if not HOOKS.exists():
        return {"version": 1, "hooks": {}}
    return json.loads(HOOKS.read_text(encoding="utf-8"))


def save_hooks(config):
    HOOKS.parent.mkdir(parents=True, exist_ok=True)
    HOOKS.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def remove_our_hooks(config):
    hooks = config.setdefault("hooks", {})
    for event in list(hooks):
        hooks[event] = [entry for entry in hooks[event] if not is_ours(entry)]
        if not hooks[event]:
            del hooks[event]
    return config


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def install(language, explain_in):
    coach.save_config(language=language, explain_in=explain_in)
    write(RULE, render_rule(language, explain_in))
    write(SKILL, render_skill(language, explain_in))

    config = remove_our_hooks(load_hooks())
    run = f'python3 "{SCRIPT}"'
    config["hooks"].setdefault("sessionStart", []).append(
        {"command": f"{run} context --format cursor", "timeout": 10})
    config["hooks"].setdefault("afterAgentResponse", []).append(
        {"command": f"{run} hook-cursor", "timeout": 15})
    save_hooks(config)

    print(f"Language Coach is set up for Cursor. Language: {language}. Explanations: {explain_in}.")
    print(f"  rule   {RULE}\n  skill  {SKILL}\n  hooks  {HOOKS}\n  log    {coach.LOG}")
    print("Keep this folder in place: the hooks run the script from here.")
    print("If Cursor asks you to approve the new hooks, approve them in Settings > Hooks.")


def uninstall():
    RULE.unlink(missing_ok=True)
    if SKILL.parent.exists():
        shutil.rmtree(SKILL.parent)
    if HOOKS.exists():
        save_hooks(remove_our_hooks(load_hooks()))
    print(f"Language Coach is removed from Cursor. Your log stays in {coach.HOME}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", help="language you are learning (default: English)")
    parser.add_argument("--explain-in", dest="explain_in", help="language of the explanations (default: English)")
    parser.add_argument("--uninstall", action="store_true", help="remove the rule, skill and hooks")
    args = parser.parse_args()
    if args.uninstall:
        uninstall()
    else:
        install(coach.setting("language", args.language), coach.setting("explain_in", args.explain_in))


if __name__ == "__main__":
    main()
