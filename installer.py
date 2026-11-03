#!/usr/bin/env python3
"""Instala e remove o ai-switch. Use pelos scripts install.sh e uninstall.sh.

Só acrescenta ao que já existe, guarda cópia de cada arquivo antes de alterar e pode rodar
de novo sem duplicar nada. Opções: --dry-run (mostra sem gravar), --purge (no uninstall,
apaga também o estado do ai em ~/.local/state/ai-switch).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"), "ai-switch")
BACKUP = os.path.join(STATE_DIR, "backup", time.strftime("%Y%m%d-%H%M%S"))

CLAUDE_MD = os.path.join(HOME, ".claude", "CLAUDE.md")
CLAUDE_SETTINGS = os.path.join(HOME, ".claude", "settings.json")
CODEX_DIR = os.path.join(HOME, ".codex")
CODEX_CONFIG = os.path.join(CODEX_DIR, "config.toml")
CODEX_HOOKS = os.path.join(CODEX_DIR, "hooks.json")
CODEX_AGENTS = os.path.join(CODEX_DIR, "AGENTS.md")
BIN = os.path.join(HOME, ".local", "bin", "ai")

BEGIN, END = "<!-- ai-switch:begin -->", "<!-- ai-switch:end -->"
OLD_HEADING = "# Continuidade entre agentes (Claude Code e Codex)"
TAG = "# ai-switch"
OURS = re.compile(r"handoff\.py\"? (session-start|prompt|stop) --agent")
CODEX_KEYS = [('project_doc_fallback_filenames', '["CLAUDE.md"]'), ("project_doc_max_bytes", "196608")]
IGNORE_LINES = ["/HANDOFF.md", "/.handoff-auto.md"]

DRY = "--dry-run" in sys.argv


def say(kind, text):
    mark = {"ok": "  +", "skip": "  =", "warn": "  !", "rm": "  -"}[kind]
    print(f"{mark} {text}")


def backup(path):
    if DRY or not os.path.exists(path):
        return
    os.makedirs(BACKUP, exist_ok=True)
    dest = os.path.join(BACKUP, path.replace(HOME, "~").strip("/~").replace("/", "__"))
    if not os.path.exists(dest):
        shutil.copy2(path, dest, follow_symlinks=True)


def write(path, text):
    if DRY:
        return
    backup(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".ai-switch-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def read(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return default


def hook_cmd(sub, agent):
    return f'python3 "{os.path.join(HERE, "handoff.py")}" {sub} --agent {agent}'


# ---------------------------------------------------------------- regras no CLAUDE.md global

def strip_rules(text):
    if BEGIN in text and END in text:
        a, b = text.index(BEGIN), text.index(END) + len(END)
        return (text[:a].rstrip() + "\n" + text[b:].lstrip("\n")).rstrip() + "\n", True
    if OLD_HEADING in text:  # seção colada à mão antes do pacote existir
        a = text.index(OLD_HEADING)
        m = re.search(r"^# ", text[a + len(OLD_HEADING):], re.M)
        b = a + len(OLD_HEADING) + m.start() if m else len(text)
        return (text[:a].rstrip() + "\n\n" + text[b:]).rstrip() + "\n", True
    return text, False


def install_rules():
    with open(os.path.join(HERE, "rules.md"), encoding="utf-8") as f:
        rules = f.read().strip()
    cur = read(CLAUDE_MD)
    base, had = strip_rules(cur)
    new = base.rstrip() + f"\n\n{BEGIN}\n{rules}\n{END}\n"
    if new == cur:
        say("skip", "regras de continuidade já estão no ~/.claude/CLAUDE.md")
    else:
        write(CLAUDE_MD, new.lstrip("\n"))
        say("ok", f"regras de continuidade {'atualizadas' if had else 'acrescentadas'} no fim do ~/.claude/CLAUDE.md")


def uninstall_rules():
    cur = read(CLAUDE_MD)
    new, had = strip_rules(cur)
    if had:
        write(CLAUDE_MD, new)
        say("rm", "regras de continuidade tiradas do ~/.claude/CLAUDE.md")


# ---------------------------------------------------------------- hooks

def strip_hooks(hooks):
    removed = 0
    for event in list(hooks):
        groups = []
        for g in hooks[event]:
            hs = [h for h in g.get("hooks", []) if not OURS.search(h.get("command", ""))]
            removed += len(g.get("hooks", [])) - len(hs)
            if hs:
                groups.append(dict(g, hooks=hs))
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    return removed


def our_hooks(agent):
    extra = {"statusMessage": "Lendo handoff", "additionalContextLimit": 8000} if agent == "codex" else {}
    return {
        "SessionStart": {"type": "command", "command": hook_cmd("session-start", agent), "timeout": 10, **extra},
        "UserPromptSubmit": {"type": "command", "command": hook_cmd("prompt", agent), "timeout": 10},
        "Stop": {"type": "command", "command": hook_cmd("stop", agent), "timeout": 15},
    }


def install_hooks(path, agent, label):
    raw = read(path)
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        say("warn", f"{path} não é JSON válido ({e}); hooks do {label} não instalados")
        return
    if agent == "codex" and not data:
        data["description"] = "Hooks do ai-switch (continuidade entre Claude Code e Codex)"
    hooks = data.setdefault("hooks", {})
    before = json.dumps(hooks, sort_keys=True)
    strip_hooks(hooks)
    for event, h in our_hooks(agent).items():
        hooks.setdefault(event, []).append({"hooks": [h]})
    if json.dumps(hooks, sort_keys=True) == before:
        say("skip", f"hooks do {label} já instalados")
        return
    write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    say("ok", f"3 hooks do {label} em {path.replace(HOME, '~')}")


def uninstall_hooks(path, label):
    raw = read(path)
    if not raw.strip():
        return
    data = json.loads(raw)
    n = strip_hooks(data.get("hooks", {}))
    if not n:
        return
    if path == CODEX_HOOKS and not data.get("hooks"):
        backup(path)
        if not DRY:
            os.remove(path)
        say("rm", f"{path.replace(HOME, '~')} apagado (só tinha os hooks do ai-switch)")
        return
    write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    say("rm", f"{n} hooks do {label} tirados")


# ---------------------------------------------------------------- Codex: instruções

def install_codex_config():
    text = read(CODEX_CONFIG)
    lines = text.splitlines()
    changed = False
    for key, value in CODEX_KEYS:
        idx = next((i for i, l in enumerate(lines) if re.match(rf"\s*{key}\s*=", l)), None)
        if idx is None:
            first_table = next((i for i, l in enumerate(lines) if l.strip().startswith("[")), len(lines))
            lines.insert(first_table, f"{key} = {value}  {TAG}")
            changed = True
            say("ok", f"Codex: {key} = {value}")
        elif TAG not in lines[idx]:
            if lines[idx].split("=", 1)[1].strip() == value:
                lines[idx] += f"  {TAG}"  # mesmo valor, de uma instalação manual: passa a ser nosso
                changed = True
            else:
                say("warn", f"Codex: {key} já existe com outro valor; deixei como está ({lines[idx].strip()})")
        else:
            say("skip", f"Codex: {key} já configurado")
    if changed:
        new = "\n".join(lines) + "\n"
        try:
            import tomllib
            tomllib.loads(new)
        except Exception as e:
            say("warn", f"a config do Codex ficaria inválida ({e}); nada gravado")
            return
        write(CODEX_CONFIG, new)


def uninstall_codex_config():
    text = read(CODEX_CONFIG)
    if TAG not in text:
        return
    lines = [l for l in text.splitlines() if not l.rstrip().endswith(TAG)]
    write(CODEX_CONFIG, "\n".join(lines) + "\n")
    say("rm", "chaves do ai-switch tiradas da config do Codex")


def install_codex_agents():
    if os.path.islink(CODEX_AGENTS) and os.path.realpath(CODEX_AGENTS) == os.path.realpath(CLAUDE_MD):
        say("skip", "~/.codex/AGENTS.md já aponta para ~/.claude/CLAUDE.md")
    elif os.path.exists(CODEX_AGENTS):
        say("warn", "~/.codex/AGENTS.md já existe e não é o link; deixei como está (o Codex não vai ler o CLAUDE.md global)")
    else:
        if not DRY:
            os.makedirs(CODEX_DIR, exist_ok=True)
            os.symlink(CLAUDE_MD, CODEX_AGENTS)
        say("ok", "~/.codex/AGENTS.md -> ~/.claude/CLAUDE.md")


def uninstall_codex_agents():
    if os.path.islink(CODEX_AGENTS) and os.path.realpath(CODEX_AGENTS) == os.path.realpath(CLAUDE_MD):
        if not DRY:
            os.remove(CODEX_AGENTS)
        say("rm", "link ~/.codex/AGENTS.md")


# ---------------------------------------------------------------- git e comando

def ignore_file():
    out = subprocess.run(["git", "config", "--global", "core.excludesFile"], capture_output=True, text=True)
    path = out.stdout.strip()
    if path:
        return os.path.expanduser(path)
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "git", "ignore")


def install_ignore():
    path = ignore_file()
    text = read(path)
    missing = [l for l in IGNORE_LINES if l not in text.splitlines()]
    if not missing:
        say("skip", f"handoff já ignorado pelo git ({path.replace(HOME, '~')})")
        return
    block = f"\n{TAG}: arquivos de handoff ficam fora do git\n" + "\n".join(missing) + "\n"
    write(path, (text.rstrip("\n") + "\n" if text else "") + block)
    say("ok", f"handoff ignorado pelo git em todos os repositórios ({path.replace(HOME, '~')})")


def uninstall_ignore():
    path = ignore_file()
    text = read(path)
    if TAG not in text:
        return
    lines = [l for l in text.splitlines() if not l.startswith(TAG) and l not in IGNORE_LINES]
    write(path, "\n".join(lines).rstrip("\n") + "\n")
    say("rm", "linhas do handoff tiradas do ignore global do git")


def install_bin():
    target = os.path.join(HERE, "ai.py")
    if not DRY:
        os.chmod(target, 0o755)
    if os.path.islink(BIN):
        if os.path.realpath(BIN) == target:
            say("skip", "~/.local/bin/ai já aponta para este diretório")
            return
        if os.path.basename(os.path.realpath(BIN)) == "ai.py":
            if not DRY:
                os.remove(BIN)
        else:
            say("warn", f"~/.local/bin/ai aponta para outra coisa ({os.readlink(BIN)}); deixei como está")
            return
    elif os.path.exists(BIN):
        say("warn", "~/.local/bin/ai já existe e não é link; deixei como está")
        return
    if not DRY:
        os.makedirs(os.path.dirname(BIN), exist_ok=True)
        os.symlink(target, BIN)
    say("ok", f"comando ai: ~/.local/bin/ai -> {target.replace(HOME, '~')}")
    if os.path.dirname(BIN) not in os.environ.get("PATH", "").split(":"):
        say("warn", "~/.local/bin não está no PATH: acrescente export PATH=\"$HOME/.local/bin:$PATH\" ao ~/.zshrc ou ~/.bashrc")


def uninstall_bin():
    if os.path.islink(BIN) and os.path.realpath(BIN) == os.path.join(HERE, "ai.py"):
        if not DRY:
            os.remove(BIN)
        say("rm", "comando ~/.local/bin/ai")


# ---------------------------------------------------------------- principal

def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "install"
    if sys.version_info < (3, 11):
        print("ai-switch precisa de Python 3.11 ou mais novo.")
        return 1
    print(f"ai-switch {action}{' (simulação)' if DRY else ''}: {HERE.replace(HOME, '~')}")
    if action == "install":
        for tool in ("claude", "codex"):
            if not shutil.which(tool):
                say("warn", f"{tool} não está no PATH; a parte dele fica pronta para quando for instalado")
        install_bin()
        install_rules()
        install_codex_agents()
        install_codex_config()
        install_hooks(CLAUDE_SETTINGS, "claude", "Claude Code")
        install_hooks(CODEX_HOOKS, "codex", "Codex")
        install_ignore()
        if os.path.isdir(BACKUP):
            print(f"\nCópias do que foi alterado: {BACKUP.replace(HOME, '~')}")
        print("\nFalta: no Codex, digite /hooks e aprove os 3 hooks. Depois: ai doctor.")
    elif action == "uninstall":
        uninstall_bin()
        uninstall_rules()
        uninstall_codex_agents()
        uninstall_codex_config()
        uninstall_hooks(CLAUDE_SETTINGS, "Claude Code")
        uninstall_hooks(CODEX_HOOKS, "Codex")
        uninstall_ignore()
        if "--purge" in sys.argv and os.path.isdir(STATE_DIR) and not DRY:
            shutil.rmtree(STATE_DIR)
            say("rm", f"estado do ai ({STATE_DIR.replace(HOME, '~')})")
        print("\nFicam como estão: HANDOFF.md dos projetos, MCPs copiados para o Codex (codex mcp remove <nome>)"
              " e links de skills em ~/.codex/skills.")
        if os.path.isdir(BACKUP):
            print(f"Cópias do que foi alterado: {BACKUP.replace(HOME, '~')}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
