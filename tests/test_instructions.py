"""The instructions agents follow must match the engine: every command and flag they're told to run exists,
every file they're told to read is one the engine writes, and every CLI's /feed points at AGENTS.md."""
import json
import re
import subprocess
import sys
import unittest

from helpers import CLI, ROOT

DOCS = [ROOT / "AGENTS.md", ROOT / "README.md", *sorted((ROOT / "prompts").glob("*.md"))]
SHIMS = [ROOT / ".claude/skills/feed/SKILL.md", ROOT / ".agents/skills/feed/SKILL.md",
         ROOT / ".opencode/commands/feed.md", ROOT / ".gemini/commands/feed.toml"]


def cli_help(*args):
    return subprocess.run([sys.executable, str(CLI), *args, "-h"], capture_output=True, text=True).stdout


class Instructions(unittest.TestCase):
    def test_every_command_and_flag_in_the_docs_exists(self):
        commands = set(re.search(r"\{([^}]+)\}", cli_help()).group(1).split(","))
        used = set()
        for doc in DOCS:
            for cmd, rest in re.findall(r"python newspaper\.py ([a-z-]+)([^`\n]*)", doc.read_text(encoding="utf-8")):
                used.add(cmd)
                self.assertIn(cmd, commands, f"{doc.name} runs unknown command `{cmd}`")
                for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", rest):
                    self.assertIn(flag, cli_help(cmd), f"{doc.name}: `{cmd}` has no {flag}")
        # The run uses the whole pipeline; a command dropped from the docs is a step agents no longer take.
        for step in ("init", "fetch", "batches", "render", "open", "commit", "votes", "apply-decisions", "suggest", "probe"):
            self.assertIn(step, used)

    def test_files_agents_read_are_files_the_engine_writes(self):
        engine = CLI.read_text(encoding="utf-8")
        # Files a subtask prompt tells the agent to write count as created too ("... to `data/...`").
        written = {p for f in (ROOT / "prompts").glob("*.md")
                   for p in re.findall(r"to `(data/[\w./-]+)`", f.read_text(encoding="utf-8"))}
        for doc in DOCS:
            for path in re.findall(r"`(data/[\w./*-]+)`", doc.read_text(encoding="utf-8")):
                name = path.rsplit("/", 1)[-1]
                if "*" in name or name in ("", "state"):
                    continue
                self.assertTrue(name in engine or (ROOT / "examples" / name).exists() or path in written,
                                f"{doc.name} mentions {path}, which nothing creates")

    def test_prompt_files_referenced_exist(self):
        for doc in DOCS:
            for prompt in re.findall(r"prompts/[\w-]+\.md", doc.read_text(encoding="utf-8")):
                self.assertTrue((ROOT / prompt).exists(), f"{doc.name} references missing {prompt}")

    def test_sections_agree_between_prompts_and_page(self):
        page = (ROOT / "template.html").read_text(encoding="utf-8")
        page_keys = re.findall(r'\{ key: "([a-z]+)", title:', page)
        self.assertEqual(page_keys, ["news", "article", "essay", "video", "podcast", "surprise"])
        triage = (ROOT / "prompts/triage.md").read_text(encoding="utf-8")
        reader = (ROOT / "prompts/reader.md").read_text(encoding="utf-8")
        self.assertIn('"section": "news|article|essay|video|podcast"', triage)
        self.assertIn('"section": "news|article|essay|video|podcast|surprise"', reader)

    def test_every_cli_gets_feed_from_agents_md(self):
        self.assertEqual((ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip(), "@AGENTS.md")
        settings = json.loads((ROOT / ".gemini/settings.json").read_text(encoding="utf-8"))
        self.assertIn("AGENTS.md", settings["context"]["fileName"])
        for shim in SHIMS:
            text = shim.read_text(encoding="utf-8")
            self.assertIn("AGENTS.md", text, f"{shim} doesn't point at AGENTS.md")
            if shim.suffix == ".md" and "skills" in shim.parts:
                self.assertRegex(text, r"(?s)^---\nname: feed\ndescription: .+?\n---\n")

    def test_gemini_command_is_valid_toml(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib needs Python 3.11+")
        cmd = tomllib.loads((ROOT / ".gemini/commands/feed.toml").read_text(encoding="utf-8"))
        self.assertIn("AGENTS.md", cmd["prompt"])
        self.assertTrue(cmd["description"])

    def test_template_has_one_data_slot(self):
        self.assertEqual((ROOT / "template.html").read_text(encoding="utf-8").count("__DIGEST_JSON__"), 1)

    def test_personal_data_never_ships(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
        self.assertIn("data/", ignored)


if __name__ == "__main__":
    unittest.main()
