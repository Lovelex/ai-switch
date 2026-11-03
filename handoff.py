#!/usr/bin/env python3
"""Continuidade entre Claude Code e Codex.

Hooks (os dois agentes chamam o mesmo script, lendo o JSON do hook no stdin):
  handoff.py session-start --agent claude|codex   -> imprime o contexto inicial
  handoff.py prompt        --agent claude|codex   -> guarda o pedido do usuário
  handoff.py stop          --agent claude|codex   -> guarda a última resposta e o git

Manual:
  handoff.py enable   (na raiz do repositório) -> cria HANDOFF.md e liga a integração
  handoff.py status   -> mostra o que o próximo agente vai ver

Só age em repositórios git com HANDOFF.md na raiz (opt-in). Nunca falha:
qualquer erro sai em silêncio com código 0, para não atrapalhar a sessão.
Parte do ai-switch: https://github.com/Lovelex/ai-switch
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HANDOFF = "HANDOFF.md"
AUTO = ".handoff-auto.md"
STATE = "ai-switch.json"  # dentro de .git/, nunca versionado
MAX_PROMPTS = 5
PROMPT_CHARS = 500
REPLY_CHARS = 700
HANDOFF_LINES = 80
CODEX_DOC_LIMIT_DEFAULT = 32 * 1024

TEMPLATE = """# Handoff: {name}

> Estado atual do trabalho para o próximo agente (Claude Code ou Codex).
> Reescrever, não acumular: até ~60 linhas. Nada de senhas, chaves ou tokens.
> Atualizado: {date} por {agent}

## Tarefa atual
(objetivo, em 1-3 linhas)

## Feito
-

## Em andamento
-

## Planejado (não começado)
-

## Decisões
-

## Problemas em aberto
-

## Testes
-

## Próximos passos
1.

## Arquivos relevantes
-
"""

SECRET_PATTERNS = [
    (re.compile(r"(sk-[A-Za-z0-9_-]{16,}|sk_[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|"
                r"github_pat_\w{20,}|AKIA[0-9A-Z]{16}|xox[abprs]-[\w-]{10,}|"
                r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,})"), "[segredo removido]"),
    (re.compile(r"(?i)\b(password|senha|passwd|token|api[_-]?key|secret|bearer)\b(\s*[:=]\s*|\s+)\S+"),
     r"\1\2[removido]"),
]


def redact(text):
    for pat, rep in SECRET_PATTERNS:
        text = pat.sub(rep, text)
    return text


def clip(text, n):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def git(root, *args, keep_indent=False):
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return ""
        return out.stdout.rstrip() if keep_indent else out.stdout.strip()
    except Exception:
        return ""


def repo_root(cwd):
    return git(cwd, "rev-parse", "--show-toplevel") or None


def git_dir(root):
    d = git(root, "rev-parse", "--absolute-git-dir")
    return d or os.path.join(root, ".git")


def write_atomic(path, text):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-handoff-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_state(root):
    try:
        with open(os.path.join(git_dir(root), STATE), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"prompts": [], "reply": None}


def save_state(root, state):
    write_atomic(os.path.join(git_dir(root), STATE), json.dumps(state, ensure_ascii=False, indent=1))


def ai_mode():
    m = os.environ.get("AI_TERMINAL_MODE")
    return m.upper() if m in ("safe", "yolo") else None


def last_turn(agent, data):
    t = {"time": now(), "agent": agent, "session": data.get("session_id") or "?"}
    if ai_mode():
        t["mode"] = ai_mode()
    return t


def record_switch(root, src, dst, mode, note=None):
    """Chamado pelo ai ao trocar de agente (ou ao sair de um)."""
    state = load_state(root)
    state["switch"] = {"time": now(), "epoch": int(time.time()), "from": src, "to": dst, "mode": mode.upper()}
    if note:
        state["switch"]["note"] = note
    save_state(root, state)
    render_auto(root, state)


def now():
    return time.strftime("%Y-%m-%d %H:%M")


def render_auto(root, state):
    lines = ["# Estado automático (gerado pelos hooks a cada turno; não editar)", ""]
    last = state.get("last") or {}
    if last:
        mode = f", modo {last['mode']}" if last.get("mode") else ""
        lines.append(f"Último turno: {last.get('time')} no {last.get('agent')} (sessão {last.get('session', '?')[:8]}{mode})")
    sw = state.get("switch")
    if sw:
        lines.append(f"Última troca pelo ai: {sw['time']}, {sw['from']} → {sw['to']} ({sw['mode']})" + (f"; {sw['note']}" if sw.get("note") else ""))
    branch = git(root, "branch", "--show-current") or "(detached)"
    lines.append(f"Branch: {branch}")
    log = git(root, "log", "-3", "--format=%h %s")
    if log:
        lines += ["", "## Últimos commits"] + [f"- {l}" for l in log.splitlines()]
    lines += ["", "## Últimos pedidos do usuário (mais recente por último)"]
    for p in state.get("prompts", []):
        lines.append(f"- [{p['time']} {p['agent']}] {p['text']}")
    if not state.get("prompts"):
        lines.append("- (nenhum)")
    reply = state.get("reply")
    if reply:
        lines += ["", f"## Última resposta do agente ({reply['time']} {reply['agent']})", reply["text"]]
    status = git(root, "status", "--short", "--untracked-files=normal", keep_indent=True).splitlines()
    status = [s for s in status if not s.endswith((HANDOFF, AUTO))]
    stat = git(root, "diff", "HEAD", "--shortstat")
    lines += ["", f"## Mudanças sem commit ({len(status)} arquivos{'; ' + stat if stat else ''})"]
    lines += [f"    {s}" for s in status[:40]]
    if len(status) > 40:
        lines.append(f"    … e mais {len(status) - 40}")
    write_atomic(os.path.join(root, AUTO), "\n".join(lines) + "\n")


def last_reply_from_transcript(path):
    """Claude Code: última mensagem de texto do assistente no transcript JSONL."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 2_000_000))
            data = f.read().decode("utf-8", "ignore").splitlines()
        for line in reversed(data):
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("type") != "assistant":
                continue
            content = (entry.get("message") or {}).get("content") or []
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            text = "\n".join(t for t in texts if t.strip())
            if text.strip():
                return text
    except Exception:
        pass
    return None


def active_root(data):
    root = repo_root(data.get("cwd") or os.getcwd())
    if root and os.path.isfile(os.path.join(root, HANDOFF)):
        return root
    return None


def cmd_prompt(data, agent):
    root = active_root(data)
    prompt = data.get("prompt")
    if not root or not prompt or not prompt.strip():
        return
    state = load_state(root)
    state.setdefault("prompts", []).append({"time": now(), "agent": agent, "text": clip(redact(prompt), PROMPT_CHARS)})
    state["prompts"] = state["prompts"][-MAX_PROMPTS:]
    state["last"] = last_turn(agent, data)
    save_state(root, state)
    render_auto(root, state)


def cmd_stop(data, agent):
    root = active_root(data)
    if not root:
        return
    state = load_state(root)
    text = data.get("last_assistant_message")
    if not text and data.get("transcript_path"):
        text = last_reply_from_transcript(data["transcript_path"])
    if text:
        state["reply"] = {"time": now(), "agent": agent, "text": clip(redact(text), REPLY_CHARS)}
    state["last"] = last_turn(agent, data)
    save_state(root, state)
    render_auto(root, state)


def claude_memory_dir(root):
    slug = re.sub(r"[^A-Za-z0-9]", "-", root)
    return os.path.join(os.path.expanduser("~/.claude/projects"), slug, "memory")


def codex_doc_limit():
    try:
        import tomllib
        with open(os.path.expanduser("~/.codex/config.toml"), "rb") as f:
            return int(tomllib.load(f).get("project_doc_max_bytes", CODEX_DOC_LIMIT_DEFAULT))
    except Exception:
        return CODEX_DOC_LIMIT_DEFAULT


def cmd_session_start(data, agent):
    cwd = data.get("cwd") or os.getcwd()
    root = repo_root(cwd)
    out = []
    if agent == "codex" and root:
        # A memória durável do projeto é a do Claude Code; o Codex não a carrega sozinho.
        mem = os.path.join(claude_memory_dir(root), "MEMORY.md")
        if os.path.isfile(mem):
            with open(mem, encoding="utf-8") as f:
                index = f.read().strip()
            if index:
                out += [f"[Memória do projeto] Índice de {mem} (cada linha aponta um arquivo na mesma pasta; leia o arquivo quando for relevante):",
                        index, ""]
        limit = codex_doc_limit()
        for name in ("AGENTS.override.md", "AGENTS.md", "CLAUDE.md"):
            p = os.path.join(root, name)
            if os.path.isfile(p):
                if os.path.getsize(p) > limit:
                    out.append(f"[Aviso] {name} do projeto tem {os.path.getsize(p)} bytes, acima de project_doc_max_bytes ({limit}): o fim dele não foi carregado. Leia o arquivo direto se precisar das últimas seções.")
                break
    if root and os.path.isfile(os.path.join(root, HANDOFF)):
        hp = os.path.join(root, HANDOFF)
        with open(hp, encoding="utf-8") as f:
            lines = f.read().rstrip().splitlines()
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(hp)))
        out.append(f"[Handoff] {hp} (editado em {mtime}). Estado do trabalho deixado pelo último agente; confira git status/diff antes de confiar nele e só retome se o pedido do usuário for continuar esse trabalho:")
        out += lines[:HANDOFF_LINES]
        if len(lines) > HANDOFF_LINES:
            out.append(f"… ({len(lines) - HANDOFF_LINES} linhas a mais no arquivo)")
        state = load_state(root)
        last = state.get("last")
        sw = state.get("switch")
        if sw and sw.get("to") == agent and sw.get("from") != agent and time.time() - sw.get("epoch", 0) < 1800:
            out += ["", f"[Troca de agente] Você foi aberto pelo ai às {sw['time']} depois de uma sessão do {sw['from']} (modo {sw['mode']}). A conversa dele não passa para cá: o que ficou é o handoff, o estado automático e o código." + (f" {sw['note']}" if sw.get("note") else "")]
        if last:
            out += ["", f"[Estado automático] Último turno: {last['time']} no {last['agent']}. Arquivo completo: {os.path.join(root, AUTO)}"]
            if state.get("prompts"):
                p = state["prompts"][-1]
                out.append(f"Último pedido ({p['agent']}): {clip(p['text'], 300)}")
            if state.get("reply"):
                r = state["reply"]
                out.append(f"Última resposta ({r['agent']}): {clip(r['text'], 300)}")
    if out:
        # JSON explícito: o Codex tenta ler como JSON uma saída que começa com "[" e falha.
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": "\n".join(out)}}, ensure_ascii=False))


def cmd_enable(_data, agent):
    root = repo_root(os.getcwd())
    if not root:
        print("Não é um repositório git.")
        return
    hp = os.path.join(root, HANDOFF)
    if os.path.exists(hp):
        print(f"Já existe: {hp}")
    else:
        with open(hp, "w", encoding="utf-8") as f:
            f.write(TEMPLATE.format(name=os.path.basename(root), date=now(), agent=agent or "manual"))
        print(f"Criado: {hp}")
    # Garante que nada disso entra no git, mesmo sem o ignore global.
    for name in (HANDOFF, AUTO):
        if not git(root, "check-ignore", name):
            exclude = os.path.join(git_dir(root), "info", "exclude")
            os.makedirs(os.path.dirname(exclude), exist_ok=True)
            with open(exclude, "a", encoding="utf-8") as f:
                f.write(f"/{name}\n")
            print(f"Adicionado a {exclude}: /{name}")
    tracked = git(root, "ls-files", HANDOFF)
    if tracked:
        print(f"Atenção: {HANDOFF} já é versionado neste repositório; o ignore não vale para ele.")
    render_auto(root, load_state(root))


def cmd_status(_data, agent):
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_session_start({"cwd": os.getcwd()}, agent if agent in ("claude", "codex") else "codex")
    raw = buf.getvalue().strip()
    print(json.loads(raw)["hookSpecificOutput"]["additionalContext"] if raw else "(nada para mostrar: sem HANDOFF.md nem memória)")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    agent = args[args.index("--agent") + 1] if "--agent" in args else None
    data = {}
    if cmd in ("session-start", "prompt", "stop"):
        try:
            raw = sys.stdin.read()
            data = json.loads(raw) if raw.strip() else {}
        except Exception:
            data = {}
    fn = {"session-start": cmd_session_start, "prompt": cmd_prompt, "stop": cmd_stop,
          "enable": cmd_enable, "status": cmd_status}.get(cmd)
    if fn:
        fn(data, agent or "?")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
