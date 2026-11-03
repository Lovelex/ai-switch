"""Testes do ai-switch. Rodam num HOME temporário: nunca tocam a configuração de verdade.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PY = sys.executable

OTHER_HOOK = {"hooks": [{"type": "command", "command": "node /algum/outro/hook.js"}]}
OLD_RULES = "# Continuidade entre agentes (Claude Code e Codex)\n\nversão antiga colada à mão\n"


class Sandbox:
    """HOME falso com Claude e Codex falsos no PATH."""

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="ai-switch-test-")
        self.bin = os.path.join(self.home, "fakebin")
        os.makedirs(self.bin)
        for agent in ("claude", "codex"):
            path = os.path.join(self.bin, agent)
            with open(path, "w") as f:
                f.write('#!/bin/sh\n'
                        'if [ "$1" = "mcp" ]; then echo "[]"; exit 0; fi\n'
                        f'echo "FAKE-{agent} cwd=$(pwd) mode=$AI_TERMINAL_MODE args=$*"\n'
                        'exit ${FAKE_EXIT:-0}\n')
            os.chmod(path, 0o755)
        self.env = {
            "HOME": self.home, "PATH": f"{self.bin}:{os.path.join(self.home, '.local', 'bin')}:/usr/bin:/bin",
            "GIT_CONFIG_GLOBAL": os.path.join(self.home, ".gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
            "NO_COLOR": "1", "LANG": "C.UTF-8", "TERM": "xterm-256color",
        }

    def path(self, *parts):
        return os.path.join(self.home, *parts)

    def write(self, rel, text):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, rel):
        with open(self.path(rel), encoding="utf-8") as f:
            return f.read()

    def run(self, *args, cwd=None, stdin=None, env=None):
        return subprocess.run(list(args), cwd=cwd or self.home, input=stdin, capture_output=True, text=True,
                              env=dict(self.env, **(env or {})), timeout=60)

    def repo(self, name="proj", handoff=True):
        root = self.path(name)
        os.makedirs(os.path.join(root, "src"), exist_ok=True)
        self.run("git", "init", "-q", root)
        self.run("git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                 "--allow-empty", "-m", "inicio")
        if handoff:
            self.run(PY, os.path.join(REPO, "ai.py"), "handoff", "enable", cwd=root)
        return root

    def cleanup(self):
        shutil.rmtree(self.home, ignore_errors=True)


def drive(sb, argv, keys, cwd):
    """Roda o ai num pty e aperta as teclas do menu."""
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execve(PY, [PY] + argv, sb.env)
    out = b""

    def pump(t):
        nonlocal out
        end = time.time() + t
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    return

    for k in keys:
        pump(0.8)
        os.write(fd, b"\r" if k == "ENTER" else k.encode())
    pump(1.0)
    _, status = os.waitpid(pid, 0)
    text = re.sub(r"\x1b\][^\x07]*\x07|\x1b\[[0-9;]*m", "", out.decode(errors="ignore")).replace("\r", "")
    return text, os.waitstatus_to_exitcode(status)


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.sb.write(".claude/CLAUDE.md", "# Minhas regras\n\nregra 1\n\n" + OLD_RULES)
        self.sb.write(".claude/settings.json", json.dumps({
            "model": "opus",
            "hooks": {"SessionStart": [OTHER_HOOK],
                      "Stop": [{"hooks": [{"type": "command",
                                           "command": "python3 /velho/agent-handoff/handoff.py stop --agent claude"}]}]}}))
        self.sb.write(".codex/config.toml", 'model = "gpt-5.5"\n\n[projects."/x"]\ntrust_level = "trusted"\n')

    def tearDown(self):
        self.sb.cleanup()

    def install(self, *extra):
        r = self.sb.run(PY, os.path.join(REPO, "installer.py"), "install", *extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_instala_preserva_e_nao_duplica(self):
        self.install()
        snapshot = {p: self.sb.read(p) for p in (".claude/CLAUDE.md", ".claude/settings.json", ".codex/config.toml",
                                                 ".codex/hooks.json", ".config/git/ignore")}
        out = self.install()
        self.assertNotIn("  + ", out, "a 2ª instalação não pode mudar nada")
        for p, text in snapshot.items():
            self.assertEqual(self.sb.read(p), text, p)

        md = self.sb.read(".claude/CLAUDE.md")
        self.assertTrue(md.startswith("# Minhas regras\n\nregra 1"))
        self.assertEqual(md.count("# Continuidade entre agentes"), 1)
        self.assertNotIn("versão antiga colada à mão", md)

        settings = json.loads(self.sb.read(".claude/settings.json"))
        self.assertEqual(settings["model"], "opus")
        self.assertIn(OTHER_HOOK, settings["hooks"]["SessionStart"])
        cmds = json.dumps(settings["hooks"])
        self.assertNotIn("/velho/agent-handoff", cmds)
        self.assertEqual(cmds.count("handoff.py"), 3)

        cfg = tomllib.loads(self.sb.read(".codex/config.toml"))
        self.assertEqual(cfg["project_doc_fallback_filenames"], ["CLAUDE.md"])
        self.assertEqual(cfg["model"], "gpt-5.5")
        self.assertIn("/x", cfg["projects"])
        self.assertEqual(json.dumps(json.loads(self.sb.read(".codex/hooks.json"))).count("handoff.py"), 3)
        self.assertEqual(os.path.realpath(self.sb.path(".codex/AGENTS.md")), self.sb.path(".claude/CLAUDE.md"))
        self.assertEqual(os.path.realpath(self.sb.path(".local/bin/ai")), os.path.join(REPO, "ai.py"))
        self.assertIn("/HANDOFF.md", self.sb.read(".config/git/ignore"))

    def test_simulacao_nao_grava(self):
        before = self.sb.read(".claude/settings.json")
        self.install("--dry-run")
        self.assertEqual(self.sb.read(".claude/settings.json"), before)
        self.assertFalse(os.path.exists(self.sb.path(".codex/hooks.json")))

    def test_desinstala_e_deixa_o_resto(self):
        self.install()
        r = self.sb.run(PY, os.path.join(REPO, "installer.py"), "uninstall")
        self.assertEqual(r.returncode, 0, r.stdout)
        settings = json.loads(self.sb.read(".claude/settings.json"))
        self.assertEqual(settings["hooks"], {"SessionStart": [OTHER_HOOK]})
        self.assertNotIn("Continuidade", self.sb.read(".claude/CLAUDE.md"))
        self.assertIn("regra 1", self.sb.read(".claude/CLAUDE.md"))
        cfg = tomllib.loads(self.sb.read(".codex/config.toml"))
        self.assertNotIn("project_doc_fallback_filenames", cfg)
        self.assertEqual(cfg["model"], "gpt-5.5")
        for p in (".codex/hooks.json", ".codex/AGENTS.md", ".local/bin/ai"):
            self.assertFalse(os.path.lexists(self.sb.path(p)), p)
        self.assertNotIn("HANDOFF", self.sb.read(".config/git/ignore"))


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.sb.write(".config/git/ignore", "/HANDOFF.md\n/.handoff-auto.md\n")

    def tearDown(self):
        self.sb.cleanup()

    def hook(self, sub, agent, data, cwd):
        r = self.sb.run(PY, os.path.join(REPO, "handoff.py"), sub, "--agent", agent, stdin=json.dumps(data), cwd=cwd)
        self.assertEqual(r.returncode, 0)
        return r.stdout

    def test_so_age_com_handoff_ligado(self):
        root = self.sb.repo(handoff=False)
        self.hook("prompt", "claude", {"cwd": root, "prompt": "oi"}, root)
        self.assertEqual(self.hook("session-start", "claude", {"cwd": root}, root), "")
        self.assertFalse(os.path.exists(os.path.join(root, ".handoff-auto.md")))

    def test_registra_turno_e_esconde_segredo(self):
        root = self.sb.repo()
        self.hook("prompt", "codex", {"cwd": root, "session_id": "s1",
                                       "prompt": "use api_key=abc123 e sk-proj-AAAAAAAAAAAAAAAAAAAA"}, root)
        self.hook("stop", "codex", {"cwd": root, "last_assistant_message": "feito"}, root)
        with open(os.path.join(root, ".handoff-auto.md"), encoding="utf-8") as f:
            auto = f.read()
        self.assertIn("api_key=[removido]", auto)
        self.assertIn("[segredo removido]", auto)
        self.assertNotIn("abc123", auto)
        self.assertIn("feito", auto)
        self.assertEqual(self.sb.run("git", "-C", root, "status", "--short").stdout.strip(), "")

    def test_inicio_entrega_json_com_handoff_e_memoria(self):
        root = self.sb.repo()
        slug = re.sub(r"[^A-Za-z0-9]", "-", root)
        self.sb.write(f".claude/projects/{slug}/memory/MEMORY.md", "- [Regra](regra.md) - algo\n")
        out = json.loads(self.hook("session-start", "codex", {"cwd": os.path.join(root, "src")}, root))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[Handoff]", ctx)
        self.assertIn("[Memória do projeto]", ctx)
        claude = json.loads(self.hook("session-start", "claude", {"cwd": root}, root))
        self.assertNotIn("[Memória do projeto]", claude["hookSpecificOutput"]["additionalContext"])

    def test_aviso_de_troca_so_para_quem_entrou(self):
        root = self.sb.repo()
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import handoff; "
                "handoff.record_switch(sys.argv[2], 'claude', 'codex', 'yolo')")
        self.sb.run(PY, "-c", code, REPO, root)
        codex = self.hook("session-start", "codex", {"cwd": root}, root)
        claude = self.hook("session-start", "claude", {"cwd": root}, root)
        self.assertIn("[Troca de agente]", codex)
        self.assertNotIn("[Troca de agente]", claude)


class AiTest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.sb.write(".config/git/ignore", "/HANDOFF.md\n/.handoff-auto.md\n")
        self.root = self.sb.repo()
        self.cwd = os.path.join(self.root, "src")
        self.ai = os.path.join(REPO, "ai.py")

    def tearDown(self):
        self.sb.cleanup()

    def test_encaminha_argumentos_e_codigo_de_saida(self):
        r = self.sb.run(PY, self.ai, "codex", "--help", "com espaço", cwd=self.cwd, env={"FAKE_EXIT": "3"})
        self.assertEqual(r.returncode, 3)
        self.assertIn("args=--help com espaço", r.stdout)
        self.assertNotIn("dangerously", r.stdout)

    def test_yolo_vale_na_troca_e_m_volta_para_safe(self):
        out, code = drive(self.sb, [self.ai, "yolo"], ["ENTER", "c", "m", "q"], self.cwd)
        self.assertEqual(code, 0)
        runs = [l for l in out.splitlines() if l.startswith("FAKE")]
        self.assertEqual(len(runs), 4, out)
        self.assertIn("FAKE-claude", runs[0]); self.assertIn("--dangerously-skip-permissions", runs[0])
        self.assertIn("FAKE-codex", runs[1]); self.assertIn("--dangerously-bypass-approvals-and-sandbox", runs[1])
        self.assertIn("args=--continue --dangerously-skip-permissions", runs[2])
        self.assertIn("mode=safe args=--permission-mode default", runs[3])
        for r in runs:
            self.assertIn(f"cwd={self.cwd} ", r)

    def test_lembra_o_ultimo_e_switch_comeca_em_safe(self):
        drive(self.sb, [self.ai, "yolo", "codex"], ["q"], self.cwd)
        out, _ = drive(self.sb, [self.ai], ["q"], self.cwd)
        self.assertIn("FAKE-codex cwd", out)
        self.assertIn("--sandbox workspace-write --ask-for-approval on-request", out)
        out, _ = drive(self.sb, [self.ai, "switch"], ["R", "q"], self.cwd)
        runs = [l for l in out.splitlines() if l.startswith("FAKE")]
        self.assertIn("FAKE-claude", runs[0]); self.assertIn("--permission-mode default", runs[0])
        self.assertIn("args=--continue --permission-mode default", runs[1])

    def test_nao_abre_dentro_de_outra_sessao(self):
        r = self.sb.run(PY, self.ai, "yolo", cwd=self.cwd, env={"AI_TERMINAL_PID": "1"})
        self.assertEqual(r.returncode, 1)

    def test_status_e_doctor_rodam(self):
        for cmd in ("status", "help"):
            r = self.sb.run(PY, self.ai, cmd, cwd=self.cwd)
            self.assertEqual(r.returncode, 0, r.stderr)
        r = self.sb.run(PY, self.ai, "doctor", cwd=self.cwd)
        self.assertIn("funcionando", r.stdout)


if __name__ == "__main__":
    unittest.main()
