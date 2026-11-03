#!/usr/bin/env python3
"""ai: troca entre Claude Code e Codex no mesmo projeto, mantendo modo de permissão e handoff.

O agente roda como processo filho no terminal de verdade (sem PTY intermediário, sem
interceptar teclado): todo comando /, atalho, flag e recurso novo das CLIs continua igual.
O ai só aparece entre uma sessão e outra. Uso: `ai help`.
https://github.com/Lovelex/ai-switch
"""
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import handoff  # noqa: E402  (mesmo diretório)

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"), "ai-switch")
STATE_FILE = os.path.join(STATE_DIR, "ai-state.json")
BACKUP_DIR = os.path.join(STATE_DIR, "backup")

AGENTS = {
    "claude": {
        "label": "Claude", "bin": "claude", "color": "38;5;209",
        # SAFE explícito: ignora um defaultMode perigoso que esteja no settings.json.
        "modes": {"safe": ["--permission-mode", "default"], "yolo": ["--dangerously-skip-permissions"]},
        "resume": lambda flags: ["--continue"] + flags,
        "quit": "/exit ou Ctrl+C duas vezes",
    },
    "codex": {
        "label": "Codex", "bin": "codex", "color": "38;5;111",
        "modes": {"safe": ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"],
                  "yolo": ["--dangerously-bypass-approvals-and-sandbox"]},
        "resume": lambda flags: ["resume", "--last"] + flags,
        "quit": "/quit ou Ctrl+C",
    },
}
OTHER = {"claude": "codex", "codex": "claude"}

HANDOFF_PROMPT = (
    "Vou trocar de agente agora. Atualize o HANDOFF.md da raiz do repositório com o estado atual "
    "do trabalho desta conversa, seguindo o modelo do arquivo (até ~60 linhas; Feito / Em andamento / "
    "Planejado; decisões; problemas; testes; próximos passos concretos; nada de segredos). "
    "Não faça nada além de editar esse arquivo. Responda só 'ok'."
)

# ---------------------------------------------------------------- aparência

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(code, text):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def dim(t):
    return c("2", t)


def agent_tag(agent):
    a = AGENTS[agent]
    return c("1;" + a["color"], a["label"])


def mode_tag(mode):
    return c("1;38;5;203", "YOLO") if mode == "yolo" else c("1;38;5;114", "SAFE")


def set_title(text):
    if sys.stdout.isatty():
        sys.stdout.write(f"\033]0;{text}\007")
        sys.stdout.flush()


MARK = {"ok": c("38;5;114", "✔"), "config": c("38;5;221", "!"), "missing": c("38;5;203", "✘"), "unknown": c("2", "?")}
MARK_WORD = {"ok": "funcionando", "config": "precisa configurar", "missing": "não disponível", "unknown": "não verificado"}

# ---------------------------------------------------------------- estado

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"projects": {}, "sessions": {}}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    handoff.write_atomic(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=1))


def project_root():
    return handoff.repo_root(os.getcwd()) or os.getcwd()


def last_agent(root):
    return (load_state().get("projects", {}).get(root) or {}).get("agent")


def remember(root, agent, mode):
    st = load_state()
    st.setdefault("projects", {})[root] = {"agent": agent, "mode": mode.upper(), "time": handoff.now()}
    save_state(st)


def register_session(root, agent, mode):
    st = load_state()
    sessions = {pid: s for pid, s in st.get("sessions", {}).items() if pid_alive(int(pid))}
    sessions[str(os.getpid())] = {"root": root, "agent": agent, "mode": mode.upper(), "since": handoff.now()}
    st["sessions"] = sessions
    save_state(st)


def unregister_session():
    st = load_state()
    st.get("sessions", {}).pop(str(os.getpid()), None)
    save_state(st)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

# ---------------------------------------------------------------- execução

def which(agent):
    return shutil.which(AGENTS[agent]["bin"])


def child_reset():
    # O ai ignora Ctrl+C enquanto o agente roda; o agente recebe os sinais normalmente.
    for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP):
        signal.signal(sig, signal.SIG_DFL)


def run_child(argv, env=None):
    old = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGQUIT)}
    try:
        proc = subprocess.Popen(argv, env=env, preexec_fn=child_reset)
        return proc.wait()
    except FileNotFoundError:
        print(f"ai: comando não encontrado: {argv[0]}", file=sys.stderr)
        return 127
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def passthrough(agent, args):
    """`ai claude ARGS` = `claude ARGS`, sem nada a mais. Mantém o código de saída."""
    exe = which(agent)
    if not exe:
        print(f"ai: {AGENTS[agent]['bin']} não está no PATH (rode `ai doctor`).", file=sys.stderr)
        return 127
    return run_child([exe] + args)


def agent_argv(agent, mode, extra, resume):
    flags = AGENTS[agent]["modes"][mode] + extra
    args = AGENTS[agent]["resume"](flags) if resume else flags
    return [which(agent)] + args


def read_key():
    if not sys.stdin.isatty():
        return "q"
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ch = os.read(fd, 1).decode(errors="ignore")
        if ch == "\x1b":  # descarta sequências de seta etc.
            while select.select([fd], [], [], 0.05)[0]:
                os.read(fd, 16)
            return ""
        return ch
    except KeyboardInterrupt:
        return "q"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def banner(root, agent, mode, resume):
    name = os.path.basename(root)
    hp = os.path.join(root, handoff.HANDOFF)
    ho = c("38;5;114", "handoff ✔") if os.path.isfile(hp) else dim("handoff desligado")
    extra = dim(" · retomando conversa") if resume else ""
    line = f"{c('2', '──')} {agent_tag(agent)} · {mode_tag(mode)} · {c('1', name)} · {ho}{extra} {c('2', '──')}"
    print("\n" + line)
    if mode == "yolo":
        print(c("38;5;203", "   permissões irrestritas: o agente roda comandos e edita sem pedir"))
    print(dim(f"   sair: {AGENTS[agent]['quit']}  →  o ai mostra as opções de troca"))
    set_title(f"{'⚠ ' if mode == 'yolo' else ''}{AGENTS[agent]['label']} · {mode.upper()} · {name}")


def update_handoff_with(agent, mode, root):
    """Pede ao agente que acabou de sair (retomando a conversa dele) para atualizar o HANDOFF.md."""
    exe = which(agent)
    if agent == "claude":
        perm = AGENTS["claude"]["modes"]["yolo"] if mode == "yolo" else ["--permission-mode", "acceptEdits"]
        argv = [exe, "-p", "--continue"] + perm + [HANDOFF_PROMPT]
    else:
        perm = ["--dangerously-bypass-approvals-and-sandbox"] if mode == "yolo" else ["--sandbox", "workspace-write"]
        argv = [exe, "exec"] + perm + ["resume", "--last", HANDOFF_PROMPT]
    hp = os.path.join(root, handoff.HANDOFF)
    before = os.path.getmtime(hp) if os.path.exists(hp) else 0
    print(dim(f"   pedindo ao {AGENTS[agent]['label']} para atualizar o HANDOFF.md (até 5 min; Ctrl+C pula)…"))
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=300, cwd=os.getcwd())
        ok = out.returncode == 0
        tail = (out.stdout or out.stderr or "").strip().splitlines()[-3:]
    except subprocess.TimeoutExpired:
        ok, tail = False, ["tempo esgotado"]
    except KeyboardInterrupt:
        ok, tail = False, ["pulado"]
    changed = os.path.exists(hp) and os.path.getmtime(hp) > before
    if changed:
        print(c("38;5;114", "   HANDOFF.md atualizado."))
    else:
        print(c("38;5;221", f"   HANDOFF.md não mudou ({'ok' if ok else 'falhou'}): ") + dim(" / ".join(tail)[:300]))
    return changed


def session(mode, agent, extra_by_agent, resume=False):
    if os.environ.get("AI_TERMINAL_PID"):
        print("ai: este terminal já está dentro de uma sessão do ai. Saia do agente para usar o menu de troca.", file=sys.stderr)
        return 1
    root = project_root()
    agent = agent or last_agent(root) or "claude"
    code = 0
    register_session(root, agent, mode)
    try:
        while True:
            if not which(agent):
                print(c("38;5;203", f"ai: {AGENTS[agent]['bin']} não está instalado ou não está no PATH."))
                return 127
            banner(root, agent, mode, resume)
            remember(root, agent, mode)
            register_session(root, agent, mode)
            env = dict(os.environ, AI_TERMINAL="1", AI_TERMINAL_PID=str(os.getpid()),
                       AI_TERMINAL_MODE=mode, AI_TERMINAL_AGENT=agent)
            for marker in ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT"):
                env.pop(marker, None)  # aberto de dentro de outro Claude, o filho deixaria de salvar a conversa
            code = run_child(agent_argv(agent, mode, extra_by_agent.get(agent, []), resume), env)
            other = OTHER[agent]
            has_handoff = os.path.isfile(os.path.join(root, handoff.HANDOFF))
            if has_handoff:
                handoff.record_switch(root, agent, agent, mode, note=f"{AGENTS[agent]['label']} saiu com código {code}")
            choice = menu(agent, other, mode, code, has_handoff)
            if choice == "q":
                return code
            resume = False
            if choice in ("s", "c", "h"):
                if choice == "h":
                    update_handoff_with(agent, mode, root)
                if has_handoff:
                    handoff.record_switch(root, agent, other, mode)
                resume = choice == "c"
                agent = other
            elif choice == "R":
                resume = True
            elif choice == "m":
                mode = "safe" if mode == "yolo" else "yolo"
    finally:
        unregister_session()
        set_title(f"ai · {os.path.basename(root)}")


def menu(agent, other, mode, code, has_handoff):
    me, ot = AGENTS[agent]["label"], AGENTS[other]["label"]
    status = c("38;5;114", "código 0") if code == 0 else c("38;5;203", f"código {code}")
    print(f"\n{c('2', '──')} {agent_tag(agent)} saiu ({status}) · {mode_tag(mode)} {c('2', '──')}")
    other_mode = "SAFE" if mode == "yolo" else "YOLO"
    rows = [("Enter", f"trocar para {ot}"), ("c", f"{ot} retomando a última conversa dele")]
    if has_handoff:
        rows.append(("h", f"{me} atualiza o HANDOFF.md, depois troca para {ot}"))
    rows += [("r", f"reabrir {me}"), ("R", f"{me} retomando esta conversa"),
             ("m", f"reabrir {me} em {other_mode}"), ("q", "sair")]
    for k, t in rows:
        print(f"  {c('1', k.ljust(5))} {t}")
    keys = {"\r": "s", "\n": "s", "s": "s", "c": "c", "h": "h" if has_handoff else None, "r": "r",
            "R": "R", "m": "m", "q": "q", "\x04": "q", "\x03": "q"}
    while True:
        k = read_key()
        if keys.get(k):
            return keys[k]

# ---------------------------------------------------------------- MCP

def secretless(d):
    return sorted(d.keys()) if isinstance(d, dict) else []


def claude_mcp_config(root):
    """Servidores do Claude com origem e forma (sem valores de env/headers)."""
    out = {}
    try:
        with open(os.path.join(HOME, ".claude.json"), encoding="utf-8") as f:
            cj = json.load(f)
    except Exception:
        cj = {}
    sources = [("usuário", cj.get("mcpServers") or {}),
               ("local", ((cj.get("projects") or {}).get(root) or {}).get("mcpServers") or {})]
    try:
        with open(os.path.join(root, ".mcp.json"), encoding="utf-8") as f:
            sources.append(("projeto .mcp.json", json.load(f).get("mcpServers") or {}))
    except Exception:
        pass
    for scope, servers in sources:
        for name, cfg in servers.items():
            kind = cfg.get("type") or ("http" if cfg.get("url") else "stdio")
            out[name] = {"scope": scope, "kind": kind, "command": cfg.get("command"), "args": cfg.get("args") or [],
                         "url": cfg.get("url"), "env_keys": secretless(cfg.get("env")),
                         "header_keys": secretless(cfg.get("headers")), "cwd": cfg.get("cwd")}
    return out


LIST_RE = re.compile(r"^(?P<name>.+?): (?P<target>.+?)(?: \((?P<kind>HTTP|SSE|stdio)\))? - (?P<status>.+)$")


def claude_mcp_health():
    """`claude mcp list` conecta em cada servidor (inclui plugins e conectores do claude.ai)."""
    exe = which("claude")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "mcp", "list"], capture_output=True, text=True, timeout=90, cwd=os.getcwd())
    except Exception:
        return None
    res = {}
    for line in out.stdout.splitlines():
        m = LIST_RE.match(line.strip())
        if not m:
            continue
        st = m["status"]
        state = "ok" if "Connected" in st else ("config" if re.search(r"auth", st, re.I) else "missing")
        res[m["name"]] = {"target": m["target"], "kind": (m["kind"] or "stdio").lower(), "state": state,
                          "detail": re.sub(r"^[^A-Za-z]+", "", st)}
    st = load_state()
    st["mcp_health"] = {"time": handoff.now(),
                        "ok": sum(1 for v in res.values() if v["state"] == "ok"), "total": len(res)}
    save_state(st)
    return res


def codex_mcp_config():
    exe = which("codex")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "mcp", "list", "--json"], capture_output=True, text=True, timeout=30)
        items = json.loads(out.stdout or "[]")
    except Exception:
        return None
    res = {}
    for it in items:
        t = it.get("transport") or it
        kind = (t.get("type") or ("streamable_http" if t.get("url") else "stdio")).lower()
        res[it.get("name")] = {"kind": "http" if "http" in kind else kind, "command": t.get("command"),
                               "args": t.get("args") or [], "url": t.get("url"),
                               "env_keys": secretless(t.get("env")) + list(t.get("env_vars") or []),
                               "enabled": it.get("enabled", True),
                               "auth": it.get("auth_status") or it.get("auth")}
    return res


def codex_name(name):
    if name.startswith("plugin:"):
        name = name.split(":")[-1]
    return re.sub(r"[^A-Za-z0-9_-]", "-", name)


def probe_stdio(command, args, env_keys=(), timeout=15):
    """Inicia o servidor e pede initialize + tools/list. Devolve (estado, detalhe)."""
    if not command or not shutil.which(command) and not os.path.exists(command):
        return "missing", f"comando não encontrado: {command}"
    try:
        p = subprocess.Popen([command] + list(args), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    except Exception as e:
        return "missing", str(e)[:120]
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "ai-doctor", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    try:
        for m in msgs:
            p.stdin.write(json.dumps(m) + "\n")
        p.stdin.flush()
        end = time.time() + timeout
        buf = ""
        while time.time() < end:
            r, _, _ = select.select([p.stdout], [], [], 0.5)
            if not r:
                if p.poll() is not None:
                    return "missing", f"o servidor saiu (código {p.returncode})"
                continue
            line = p.stdout.readline()
            if not line:
                return "missing", "o servidor fechou a conexão"
            buf = line
            try:
                msg = json.loads(buf)
            except Exception:
                continue
            if msg.get("id") == 2:
                tools = (msg.get("result") or {}).get("tools") or []
                return "ok", f"{len(tools)} ferramentas"
        return "missing", "sem resposta em 15 s"
    finally:
        try:
            p.kill()
        except Exception:
            pass


def same_target(cl, cx):
    if cl["kind"] == "http":
        return (cl.get("url") or "").split("?")[0] == (cx.get("url") or "").split("?")[0]
    return [cl.get("command")] + list(cl.get("args")) == [cx.get("command")] + list(cx.get("args"))


def mcp_plan(root, health):
    """Compara os dois lados. A configuração do Claude é a fonte; o Codex recebe cópias controladas."""
    cfg = claude_mcp_config(root)
    codex = codex_mcp_config() or {}
    rows, seen = [], set()
    claude_all = dict.fromkeys(list(cfg) + list(health or {}))
    for name in claude_all:
        info = dict(cfg.get(name) or {})
        h = (health or {}).get(name)
        if not info and h:  # plugins e conectores: só aparecem no `claude mcp list`
            info = {"scope": "plugin" if name.startswith("plugin:") else "claude.ai", "kind": h["kind"],
                    "url": h["target"] if h["kind"] in ("http", "sse") else None, "command": None, "args": [],
                    "env_keys": [], "header_keys": [], "cwd": None}
        cname = codex_name(name)
        cx = codex.get(cname)
        seen.add(cname)
        action, why = None, ""
        if name.startswith("claude.ai "):
            why = "conector da conta claude.ai: só existe no Claude"
        elif cx:
            why = "igual nos dois" if same_target(info, cx) else "configurado diferente nos dois (não mexo; confira)"
        elif info["kind"] == "sse":
            why = "SSE: o Codex só aceita stdio e HTTP"
        elif info.get("header_keys"):
            why = f"usa headers ({', '.join(info['header_keys'])}): credencial, configure no Codex à mão"
        elif info.get("env_keys"):
            why = f"usa variáveis ({', '.join(info['env_keys'])}): podem ser segredo, configure no Codex à mão"
        elif info.get("cwd"):
            why = "usa diretório próprio (cwd), que o `codex mcp add` não aceita"
        elif info["kind"] == "http" and info.get("url"):
            url = re.sub(r"[?&]utm_[a-z]+=[^&]*", "", info["url"])
            action = ["mcp", "add", cname, "--url", url]
            why = f"copiável; se pedir login: codex mcp login {cname}"
        elif info.get("command"):
            action = ["mcp", "add", cname, "--", info["command"]] + list(info["args"])
            why = "copiável"
        rows.append({"name": name, "codex_name": cname, "claude": info, "health": h, "codex": cx,
                     "action": action, "why": why})
    for cname, cx in codex.items():
        if cname not in seen:
            rows.append({"name": cname, "codex_name": cname, "claude": None, "health": None, "codex": cx,
                         "action": None, "why": "só no Codex (preservado)"})
    return rows


def cmd_mcp(args):
    root = project_root()
    sub = args[0] if args and not args[0].startswith("-") else "list"
    check = "--check" in args
    print(dim("verificando os servidores do Claude (`claude mcp list` conecta em cada um)…"))
    health = claude_mcp_health()
    rows = mcp_plan(root, health)
    if sub == "sync":
        return mcp_sync(rows, "--apply" in args)
    print(f"\n{c('1', 'MCP'.ljust(24))} {c('1', 'Claude'.ljust(30))} {c('1', 'Codex')}")
    for r in rows:
        h, cx = r["health"], r["codex"]
        if r["claude"] is None:
            cl = dim("-")
        elif h:
            cl = f"{MARK[h['state']]} {h['detail'][:26]}"
        else:
            cl = f"{MARK['unknown']} configurado"
        if cx:
            cxs = f"{MARK['unknown']} configurado" + (dim(" (desligado)") if cx.get("enabled") is False else "")
            if check and cx["kind"] == "stdio":
                state, detail = probe_stdio(cx["command"], cx["args"])
                cxs = f"{MARK[state]} {detail}"
            elif str(cx.get("auth")).lower() in ("o_auth", "oauth"):
                cxs += dim(f" · OAuth: codex mcp login {r['codex_name']}")
        else:
            cxs = dim("-")
        print(f"{r['name'][:24].ljust(24)} {pad(cl, 30)} {cxs}")
        if r["why"] and r["why"] != "igual nos dois":
            print(dim(f"{'':24} ↳ {r['why']}"))
    print(dim("\n✔ conectado · ! precisa de login/configuração · ✘ falhou · ? configurado, conexão não testada"))
    if not check:
        print(dim("ai mcp --check testa também os do Codex (stdio: inicia e lista as ferramentas; HTTP: só dentro do Codex)."))
    if any(r["action"] for r in rows):
        print(dim("ai mcp sync mostra o que pode ser copiado para o Codex; ai mcp sync --apply copia."))
    return 0


def pad(s, n):
    visible = re.sub(r"\033\[[0-9;]*m", "", s)
    return s + " " * max(0, n - len(visible))


def mcp_sync(rows, apply):
    todo = [r for r in rows if r["action"]]
    if not todo:
        print("Nada a copiar: tudo que é compatível já está nos dois.")
        return 0
    print(c("1", "Copiar do Claude para o Codex" + ("" if apply else " (simulação; --apply grava)")))
    for r in todo:
        shown = " ".join(r["action"])
        print(f"  codex {shown}")
        print(dim(f"     {r['why']}"))
    if not apply:
        return 0
    os.makedirs(BACKUP_DIR, exist_ok=True)
    cfg = os.path.join(HOME, ".codex", "config.toml")
    bk = os.path.join(BACKUP_DIR, f"codex-config.{time.strftime('%Y%m%d-%H%M%S')}.toml")
    shutil.copy2(cfg, bk)
    print(dim(f"  cópia da config do Codex: {bk}"))
    fails = 0
    for r in todo:
        out = subprocess.run([which("codex")] + r["action"], capture_output=True, text=True, timeout=60)
        if out.returncode == 0:
            print(f"  {MARK['ok']} {r['codex_name']}")
        else:
            fails += 1
            print(f"  {MARK['missing']} {r['codex_name']}: {(out.stderr or out.stdout).strip()[:200]}")
    return 1 if fails else 0

# ---------------------------------------------------------------- status e doctor

def version(agent):
    exe = which(agent)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception:
        return "?"


def memory_summary(root):
    parts = []
    parts.append(("global", os.path.realpath(os.path.join(HOME, ".codex", "AGENTS.md")) == os.path.join(HOME, ".claude", "CLAUDE.md")))
    cm = os.path.join(root, "CLAUDE.md")
    parts.append((f"CLAUDE.md do projeto {os.path.getsize(cm) // 1024} KB" if os.path.isfile(cm) else "sem CLAUDE.md no projeto",
                  os.path.isfile(cm)))
    mem = os.path.join(handoff.claude_memory_dir(root), "MEMORY.md")
    n = sum(1 for l in open(mem, encoding="utf-8") if l.startswith("- ")) if os.path.isfile(mem) else 0
    parts.append((f"memória do Claude {n} itens", n > 0))
    hp = os.path.join(root, handoff.HANDOFF)
    if os.path.isfile(hp):
        t = time.strftime("%d/%m %H:%M", time.localtime(os.path.getmtime(hp)))
        parts.append((f"handoff editado {t}", True))
    else:
        parts.append(("handoff desligado (ai handoff enable)", False))
    return " · ".join(f"{MARK['ok'] if ok else MARK['config']} {t}" for t, ok in parts)


def cmd_status(_args):
    root = project_root()
    st = load_state()
    proj = st.get("projects", {}).get(root)
    print(c("1", "ai status"))
    print(f"  {'Projeto'.ljust(14)} {c('1', os.path.basename(root))} {dim(root)}")
    if os.environ.get("AI_TERMINAL_PID"):
        print(f"  {'Esta sessão'.ljust(14)} {agent_tag(os.environ.get('AI_TERMINAL_AGENT', 'claude'))} · {mode_tag(os.environ.get('AI_TERMINAL_MODE', 'safe'))}")
    if proj:
        print(f"  {'Último agente'.ljust(14)} {agent_tag(proj['agent'])} · {proj.get('mode', '?')} {dim('em ' + proj.get('time', '?'))}")
    else:
        print(f"  {'Último agente'.ljust(14)} {dim('nenhum ainda (ai abre o Claude)')}")
    live = [(pid, s) for pid, s in st.get("sessions", {}).items() if pid_alive(int(pid))]
    if live:
        for pid, s in live:
            print(f"  {'Sessão ativa'.ljust(14)} {agent_tag(s['agent'])} · {mode_tag(s['mode'].lower())} · {os.path.basename(s['root'])} {dim('pid ' + pid + ', desde ' + s['since'])}")
    else:
        print(f"  {'Sessões ativas'.ljust(14)} {dim('nenhuma')}")
    print(f"  {'Memória'.ljust(14)} {memory_summary(root)}")
    hs = st.get("mcp_health")
    cx = codex_mcp_config()
    cl = f"Claude {hs['ok']}/{hs['total']} conectados {dim('(' + hs['time'] + ')')}" if hs else f"Claude {len(claude_mcp_config(root))}+ configurados {dim('(ai mcp testa)')}"
    print(f"  {'MCP'.ljust(14)} {cl} · Codex {len(cx) if cx is not None else '?'} configurados")
    return 0


def skills_in_rules():
    """Skills do Claude citadas como /nome no CLAUDE.md global: o Codex precisa delas também."""
    try:
        with open(os.path.join(HOME, ".claude", "CLAUDE.md"), encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return []
    names = dict.fromkeys(re.findall(r"(?<![\w~.])/([a-z0-9][a-z0-9-]+)", text))
    root = os.path.join(HOME, ".claude", "skills")
    return [n for n in names if os.path.isfile(os.path.join(root, n, "SKILL.md"))]


def check(level, text, fix=None):
    print(f"  {MARK[level]} {text}" + (dim(f"  → {fix}") if fix else ""))
    return level


def cmd_doctor(_args):
    root = project_root()
    levels = []
    L = levels.append
    print(c("1", "ai doctor") + dim(f"  ({os.path.basename(root)})"))

    print(c("1", "\nCLIs"))
    for agent in ("claude", "codex"):
        v = version(agent)
        L(check("ok" if v else "missing", f"{AGENTS[agent]['label']}: {v or 'não encontrado no PATH'}" + (dim(f"  {which(agent)}") if v else ""),
                None if v else ("instale: curl -fsSL https://claude.ai/install.sh | bash" if agent == "claude" else "npm install -g @openai/codex")))
    ai_link = shutil.which("ai")
    L(check("ok" if ai_link else "config", f"comando ai no PATH: {ai_link or 'não'}", None if ai_link else f"{HERE}/install.sh, e ~/.local/bin no PATH"))
    if which("codex"):
        out = subprocess.run([which("codex"), "login", "status"], capture_output=True, text=True, timeout=20)
        msg = (out.stdout or out.stderr).strip().splitlines()[-1:] or ["?"]
        logged = out.returncode == 0 and "Logged in" in msg[0]
        L(check("unknown" if logged else "config", f"login do Codex: {msg[0]}" + (dim(" (diz só que há credencial; a renovação do token só se vê ao usar)") if logged else ""),
                None if logged else "codex login"))

    print(c("1", "\nInstruções compartilhadas"))
    ag = os.path.join(HOME, ".codex", "AGENTS.md")
    linked = os.path.islink(ag) and os.path.realpath(ag) == os.path.join(HOME, ".claude", "CLAUDE.md")
    L(check("ok" if linked else "config", "~/.codex/AGENTS.md → ~/.claude/CLAUDE.md" if linked else "~/.codex/AGENTS.md não aponta para o CLAUDE.md global",
            None if linked else "ln -s ~/.claude/CLAUDE.md ~/.codex/AGENTS.md"))
    try:
        import tomllib
        with open(os.path.join(HOME, ".codex", "config.toml"), "rb") as f:
            ccfg = tomllib.load(f)
    except Exception as e:
        ccfg = {}
        L(check("missing", f"config do Codex ilegível: {e}"))
    fb = "CLAUDE.md" in (ccfg.get("project_doc_fallback_filenames") or [])
    L(check("ok" if fb else "config", "Codex lê o CLAUDE.md de cada projeto (project_doc_fallback_filenames)",
            None if fb else 'project_doc_fallback_filenames = ["CLAUDE.md"] em ~/.codex/config.toml'))
    limit = handoff.codex_doc_limit()
    for name in ("AGENTS.override.md", "AGENTS.md", "CLAUDE.md"):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            size = os.path.getsize(p)
            if name != "CLAUDE.md":
                L(check("config", f"o projeto tem {name}: o Codex usa ele e ignora o CLAUDE.md"))
            L(check("ok" if size <= limit else "config", f"{name} do projeto: {size // 1024} KB de {limit // 1024} KB que o Codex carrega",
                    None if size <= limit else "aumente project_doc_max_bytes"))
            with open(p, encoding="utf-8", errors="ignore") as f:
                imports = [l.strip() for l in f if re.match(r"^@\S+", l)]
            if imports:
                L(check("config", f"{name} usa importações @ ({', '.join(imports[:3])}): o Codex não as segue"))
            break
    else:
        L(check("unknown", "projeto sem CLAUDE.md nem AGENTS.md"))
    for s in skills_in_rules():
        ok = os.path.isfile(os.path.join(HOME, ".codex", "skills", s, "SKILL.md"))
        L(check("ok" if ok else "config", f"skill /{s} (citada no CLAUDE.md global) no Codex",
                None if ok else f"ln -s ~/.claude/skills/{s} ~/.codex/skills/{s}"))

    print(c("1", "\nHooks e handoff"))
    try:
        with open(os.path.join(HOME, ".claude", "settings.json"), encoding="utf-8") as f:
            cs = json.dumps(json.load(f).get("hooks", {}))
        n = cs.count("handoff.py")
        L(check("ok" if n >= 3 else "config", f"hooks do Claude: {n}/3"))
    except Exception:
        L(check("missing", "não consegui ler ~/.claude/settings.json"))
    try:
        with open(os.path.join(HOME, ".codex", "hooks.json"), encoding="utf-8") as f:
            n = f.read().count("handoff.py")
        L(check("ok" if n >= 3 else "config", f"hooks do Codex configurados: {n}/3"))
        L(check("unknown", "hooks do Codex aprovados", "abra o codex, digite /hooks e aprove (de novo se o hooks.json mudar)"))
    except Exception:
        L(check("missing", "~/.codex/hooks.json não existe"))
    if handoff.repo_root(os.getcwd()):
        hp = os.path.join(root, handoff.HANDOFF)
        if os.path.isfile(hp):
            age = (time.time() - os.path.getmtime(hp)) / 3600
            L(check("ok", f"HANDOFF.md editado há {age:.0f} h" if age >= 1 else "HANDOFF.md editado há menos de 1 h"))
            ign = all(handoff.git(root, "check-ignore", n) for n in (handoff.HANDOFF, handoff.AUTO))
            L(check("ok" if ign else "config", "handoff fora do git" if ign else "HANDOFF.md ou .handoff-auto.md não estão ignorados pelo git",
                    None if ign else "ai handoff enable"))
            auto = os.path.join(root, handoff.AUTO)
            L(check("ok" if os.path.isfile(auto) else "unknown", ".handoff-auto.md presente" if os.path.isfile(auto) else ".handoff-auto.md ainda não gerado (aparece no 1º turno)"))
        else:
            L(check("config", "handoff desligado neste projeto", "ai handoff enable"))
        mem = handoff.claude_memory_dir(root)
        L(check("ok" if os.path.isdir(mem) else "unknown", f"memória do Claude: {mem.replace(HOME, '~')}" if os.path.isdir(mem) else "projeto ainda sem memória do Claude"))
    else:
        L(check("unknown", "fora de um repositório git: handoff não se aplica"))

    print(c("1", "\nMCP"))
    health = claude_mcp_health()
    if health is None:
        L(check("unknown", "não consegui rodar `claude mcp list`"))
    else:
        for name, h in health.items():
            L(check(h["state"], f"Claude · {name}: {h['detail'][:60]}"))
    rows = mcp_plan(root, health)
    cx = [r for r in rows if r["codex"]]
    for r in cx:
        if str(r["codex"].get("auth")).lower() in ("o_auth", "oauth"):
            L(check("unknown", f"Codex · {r['codex_name']}: OAuth, login não verificado", f"codex mcp login {r['codex_name']}"))
    copiable = [r["codex_name"] for r in rows if r["action"]]
    L(check("unknown" if cx else "config", f"Codex: {len(cx)} configurados" + (" (conexão não testada; ai mcp --check)" if cx else ""),
            None if cx else ("ai mcp sync" if copiable else None)))
    if copiable:
        L(check("config", f"copiáveis para o Codex: {', '.join(copiable)}", "ai mcp sync --apply"))
    diff = [r["name"] for r in rows if r["claude"] and r["codex"] and "diferente" in r["why"]]
    if diff:
        L(check("config", f"configurados diferente nos dois: {', '.join(diff)}"))

    counts = {k: levels.count(k) for k in MARK}
    print("\n" + "  ".join(f"{MARK[k]} {counts[k]} {MARK_WORD[k]}" for k in ("ok", "config", "missing", "unknown")))
    return 1 if counts["missing"] else 0


def cmd_handoff(args):
    sub = args[0] if args else "status"
    data = {}
    if sub == "enable":
        handoff.cmd_enable(data, "manual")
    else:
        handoff.cmd_status(data, os.environ.get("AI_TERMINAL_AGENT"))
    return 0


HELP = """\
{title}  troca entre Claude Code e Codex no mesmo projeto

  {b}ai{r}                     abre o último agente usado neste projeto (Claude na 1ª vez), modo SAFE
  {b}ai yolo{r} [claude|codex] sessão com permissões irrestritas nos dois agentes, até você sair do ai
  {b}ai safe{r} [claude|codex] sessão protegida (sandbox e aprovações oficiais de cada um)
  {b}ai claude{r} / {b}ai codex{r}   sessão SAFE começando por esse agente
  {b}ai switch{r}              sessão SAFE com o outro agente (o que não foi usado por último aqui)
  {b}ai yolo switch{r}         idem, em YOLO
  {b}ai status{r}              projeto, último agente, sessões abertas, memória, MCP
  {b}ai mcp{r} [--check]       MCPs dos dois lados, com conexão; --check testa os do Codex
  {b}ai mcp sync{r} [--apply]  copia para o Codex os MCPs compatíveis do Claude (sem credenciais)
  {b}ai doctor{r}              diagnóstico completo
  {b}ai handoff{r} [enable]    mostra o que o próximo agente recebe / liga o handoff no projeto
  {b}ai help{r}                esta ajuda

{h}Flags e subcomandos das CLIs{r}
  ai claude <args>        = claude <args>, sem nada a mais (ex.: ai codex --help, ai claude mcp list)
  ai yolo codex <args>    sessão YOLO; <args> vão para o Codex em cada abertura (ex.: -m gpt-5.5)

{h}Na sessão{r}
  O agente roda no terminal de verdade: comandos /, atalhos, skills e MCPs são os dele.
  Ao sair do agente (/exit, /quit, Ctrl+C), o ai oferece: trocar para o outro, retomar a conversa
  nativa do outro, pedir ao que saiu para atualizar o HANDOFF.md antes, reabrir, trocar SAFE/YOLO.
  O modo vale para a sessão inteira do ai e nunca é gravado nas configurações das ferramentas.
"""


def cmd_help(_args):
    print(HELP.format(title=c("1", "ai"), b="\033[1m" if USE_COLOR else "", r="\033[0m" if USE_COLOR else "",
                      h="\033[1;2m" if USE_COLOR else ""))
    return 0


def main(argv):
    if not argv:
        return session("safe", None, {})
    cmd, rest = argv[0], argv[1:]
    if cmd in ("help", "-h", "--help"):
        return cmd_help(rest)
    if cmd in AGENTS:
        return passthrough(cmd, rest) if rest else session("safe", cmd, {})
    if cmd in ("yolo", "safe"):
        mode = cmd
        if rest and rest[0] == "switch":
            root = project_root()
            return session(mode, OTHER[last_agent(root) or "codex"], {})
        agent = rest[0] if rest and rest[0] in AGENTS else None
        extra = rest[1:] if agent else rest
        if extra and not agent:
            print("ai: diga o agente antes das flags (ex.: ai yolo codex -m gpt-5.5)", file=sys.stderr)
            return 2
        return session(mode, agent, {agent: extra} if agent else {})
    if cmd == "switch":
        return session("safe", OTHER[last_agent(project_root()) or "codex"], {})
    fn = {"status": cmd_status, "mcp": cmd_mcp, "doctor": cmd_doctor, "handoff": cmd_handoff}.get(cmd)
    if fn:
        return fn(rest)
    print(f"ai: comando desconhecido: {cmd}. Veja `ai help`.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
