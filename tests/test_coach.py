import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COACH = ROOT / "scripts" / "coach.py"
INSTALLER = ROOT / "scripts" / "install_cursor.py"

ENGLISH_BLOCK = """### English check · 9 words

| You wrote | Better | Why |
|---|---|---|
| investigate the mistake | investigate the issue | Word choice: a mistake is a human error. |
| sugest | suggest | Spelling. |
| fix ? | fix? | Punctuation: no space before a question mark (again). |

+2 smaller fixes, included in the better version.

**Better version**
> Can you investigate the issue and suggest a fix?

---

Looking into it now."""

GERMAN_BLOCK = """### German check · 9 words

| You wrote | Better | Why |
|---|---|---|
| der Fehler | den Fehler | Grammar: "untersuchen" takes the accusative. |

**Better version**
> Kannst du den Fehler untersuchen?

---
"""


class CoachTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.data = self.home / ".promptlingo"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("PROMPTLINGO_", "CLAUDE_PLUGIN_OPTION_"))}
        self.env["HOME"] = str(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, script, *args, stdin=None, env=None, check=True):
        result = subprocess.run([sys.executable, str(script), *args], input=stdin, capture_output=True,
                                text=True, env={**self.env, **(env or {})})
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def coach(self, *args, **kwargs):
        return self.run_script(COACH, *args, **kwargs)

    def cursor_hook(self, text, generation="g1", env=None):
        payload = {"text": text, "generation_id": generation, "conversation_id": "c1"}
        self.assertEqual(self.coach("hook-cursor", stdin=json.dumps(payload), env=env).stdout.strip(), "{}")

    def log(self):
        path = self.data / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assertNoErrors(self):
        errors = self.data / "errors.log"
        self.assertFalse(errors.exists(), errors.read_text() if errors.exists() else "")

    def test_parses_rows_extra_fixes_and_again(self):
        self.cursor_hook(ENGLISH_BLOCK)
        [row] = self.log()
        self.assertEqual((row["language"], row["words"], row["fixes"], row["extra"]), ("English", 9, 5, 2))
        self.assertEqual([i["category"] for i in row["items"]], ["Word choice", "Spelling", "Punctuation"])
        self.assertEqual([i["again"] for i in row["items"]], [False, False, True])
        self.assertNoErrors()

    def test_cursor_hook_dedupes_by_generation(self):
        self.cursor_hook(ENGLISH_BLOCK)
        self.cursor_hook(ENGLISH_BLOCK)
        self.assertEqual(len(self.log()), 1)

    def test_header_must_name_the_configured_language(self):
        german = {"PROMPTLINGO_LANGUAGE": "German"}
        self.cursor_hook(ENGLISH_BLOCK, "g1", env=german)
        self.assertEqual(self.log(), [])
        self.cursor_hook(GERMAN_BLOCK, "g2", env=german)
        self.assertEqual([r["language"] for r in self.log()], ["German"])

    def test_language_comes_from_plugin_option_or_config(self):
        self.cursor_hook(GERMAN_BLOCK, "g1", env={"CLAUDE_PLUGIN_OPTION_LANGUAGE": "German"})
        self.coach("config", "--language", "German")
        self.cursor_hook(GERMAN_BLOCK.replace("9 words", "8 words"), "g2")
        self.assertEqual(len(self.log()), 2)
        self.assertEqual(json.loads((self.data / "config.json").read_text())["language"], "German")

    def test_unsubstituted_placeholder_falls_back_to_default(self):
        self.cursor_hook(ENGLISH_BLOCK, env={"CLAUDE_PLUGIN_OPTION_LANGUAGE": "${user_config.language}"})
        self.assertEqual(len(self.log()), 1)

    def test_clean_prompt_other_language_and_ordinary_replies(self):
        self.cursor_hook("**English check:** no corrections · 7 words\n\n---\nDone.", "g1")
        self.cursor_hook("### English check · not in English\n\n**Better version**\n> Fix it.\n\n---", "g2")
        self.cursor_hook("Let me check the logs first.\n\n| a | b | c |", "g3")
        clean, other = self.log()
        self.assertEqual((clean["fixes"], clean["words"], clean["in_language"]), (0, 7, True))
        self.assertFalse(other["in_language"])
        stats = self.coach("stats").stdout
        self.assertIn("Prompts checked: 1 (+1 not in English)", stats)
        self.assertIn("Clean prompts: 100%", stats)

    def test_claude_hook_reads_transcript_and_last_message_once(self):
        transcript = self.home / "transcript.jsonl"
        rows = [
            {"type": "user", "message": {"content": "can you investigate the mistake and sugest a fix ?"}},
            {"type": "assistant", "timestamp": "2026-10-20T10:00:00.000Z",
             "message": {"content": [{"type": "text", "text": ENGLISH_BLOCK},
                                     {"type": "tool_use", "name": "Bash", "input": {}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "user", "isMeta": True, "message": {"content": "injected"}},
            {"type": "user", "message": {"content": "thanks it work now"}},
        ]
        transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        last = ("### English check\n\n| You wrote | Better | Why |\n|---|---|---|\n"
                "| it work | it works | Grammar: third person needs -s. |\n\n---\nGreat.")
        payload = json.dumps({"session_id": "s1", "transcript_path": str(transcript),
                              "last_assistant_message": last})
        self.coach("hook-claude", stdin=payload)
        self.coach("hook-claude", stdin=payload)
        with transcript.open("a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": last}]}}) + "\n")
        self.coach("hook-claude", stdin=payload)

        first, second = self.log()
        self.assertEqual((first["ts"], first["words"], first["source"]), ("2026-10-20T10:00:00.000Z", 9, "claude"))
        self.assertEqual(second["words"], 4)
        self.assertNoErrors()

    def test_broken_hook_input_is_logged_not_raised(self):
        self.coach("hook-claude", stdin="not json")
        self.assertIn("hook-claude", (self.data / "errors.log").read_text())

    def test_quiz_review_and_mastery(self):
        self.cursor_hook(ENGLISH_BLOCK)
        quiz = json.loads(self.coach("quiz").stdout)
        self.assertEqual(len(quiz), 3)
        item = quiz[0]["id"]
        self.coach("review", item, "pass")
        self.coach("review", item, "pass")
        self.assertIn("| mastered |", self.coach("stats").stdout)
        self.assertNotIn(item, [q["id"] for q in json.loads(self.coach("quiz").stdout)])
        self.assertNotEqual(self.coach("review", "nope", "pass", check=False).returncode, 0)

    def test_context_has_rule_and_recurring_mistakes(self):
        self.cursor_hook(ENGLISH_BLOCK, "g1")
        self.cursor_hook(ENGLISH_BLOCK, "g2")
        out = self.coach("context", "--rule").stdout
        self.assertIn("### English check · 9 words", out)
        self.assertIn('"sugest" → "suggest" (Spelling, 2x)', out)
        self.assertIn("/promptlingo:progress", out)
        self.assertNotIn("{{", out)
        self.assertLess(len(out), 10000)

        cursor = json.loads(self.coach("context", "--format", "cursor").stdout)
        self.assertNotIn("# PromptLingo", cursor["additional_context"])
        self.assertIn("sugest", cursor["additional_context"])

    def test_rule_uses_language_examples_and_falls_back(self):
        german = self.coach("rule", "--language", "German", "--explain-in", "Spanish").stdout
        self.assertIn("Kannst du den Fehler untersuchen", german)
        self.assertIn("Write the rest of the cell in Spanish", german)
        french = self.coach("rule", "--language", "French").stdout
        self.assertIn("### French check · 9 words", french)
        self.assertIn("The rows in this example are English. Yours are about French.", french)
        self.assertNotIn("{{", french)

    def test_practice_modes_change_the_rule_and_reminder(self):
        after = self.coach("rule").stdout
        self.assertIn("retype this at the start of your next prompt", after)
        self.assertNotIn("{{", after)

        before = self.coach("rule", "--practice", "before", "--language", "German").stdout
        self.assertIn("retype this sentence to start the task", before)
        self.assertIn("Do not write a German check block for the retyped sentence", before)
        self.assertNotIn("retype this at the start of your next prompt", before)

        off = self.coach("rule", "--practice", "off").stdout
        self.assertNotIn("## Practice", off)
        self.assertEqual(self.coach("rule", "--practice", "sometimes").stdout, after)

        self.assertNotIn("task waits", self.coach("reminder").stdout)
        gate = {"CLAUDE_PLUGIN_OPTION_PRACTICE": "Before"}
        self.assertIn("task waits", self.coach("reminder", env=gate).stdout)
        self.assertIn("retype this sentence to start the task", self.coach("context", "--rule", env=gate).stdout)

    def test_config_saves_and_validates_practice(self):
        self.assertIn("practice: before", self.coach("config", "--practice", "before").stdout)
        self.assertEqual(json.loads((self.data / "config.json").read_text())["practice"], "before")
        self.assertNotEqual(self.coach("config", "--practice", "maybe", check=False).returncode, 0)

    def test_empty_context_prints_nothing(self):
        self.assertEqual(self.coach("context").stdout, "")
        self.assertEqual(self.coach("context", "--format", "cursor").stdout.strip(), "{}")
        self.assertEqual(self.coach("stats").stdout.strip(), "No English checks logged yet.")

    def test_cursor_installer_is_idempotent_and_keeps_other_hooks(self):
        hooks_file = self.home / ".cursor" / "hooks.json"
        hooks_file.parent.mkdir(parents=True)
        other = {"command": "echo keep-me"}
        hooks_file.write_text(json.dumps({"version": 1, "hooks": {"afterAgentResponse": [other]}}))

        self.run_script(INSTALLER, "--language", "German")
        self.run_script(INSTALLER, "--language", "German", "--practice", "before")
        hooks = json.loads(hooks_file.read_text())["hooks"]
        self.assertEqual(len(hooks["afterAgentResponse"]), 2)
        self.assertEqual(len(hooks["sessionStart"]), 1)
        self.assertIn(str(COACH), hooks["afterAgentResponse"][1]["command"])

        rule = (self.home / ".cursor" / "rules" / "promptlingo.mdc").read_text()
        self.assertTrue(rule.startswith("---\ndescription: The user is learning German."))
        self.assertIn("alwaysApply: true", rule)
        self.assertIn("/promptlingo-progress", rule)
        self.assertIn("retype this sentence to start the task", rule)
        skill = (self.home / ".cursor" / "skills" / "promptlingo-progress" / "SKILL.md").read_text()
        self.assertIn("name: promptlingo-progress", skill)
        self.assertIn(f'python3 "{ROOT}/scripts/coach.py" stats --language "German"', skill)
        self.assertNotIn("${", skill)

        self.run_script(INSTALLER, "--uninstall")
        self.assertEqual(json.loads(hooks_file.read_text())["hooks"], {"afterAgentResponse": [other]})
        self.assertFalse((self.home / ".cursor" / "rules" / "promptlingo.mdc").exists())
        self.assertFalse((self.home / ".cursor" / "skills" / "promptlingo-progress").exists())


if __name__ == "__main__":
    unittest.main()
