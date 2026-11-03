# ai-switch

Use o **Claude Code** e o **OpenAI Codex** no mesmo projeto, alternando entre os dois quando o limite de um acabar, sem reexplicar o contexto e sem manter instruções duplicadas.

O ai-switch não substitui nenhuma das duas CLIs. Ele faz três coisas:

1. **Uma base de instruções só.** Os seus arquivos `CLAUDE.md` (o global e o de cada projeto) passam a valer também para o Codex.
2. **Um estado de trabalho compartilhado.** Cada projeto ganha um `HANDOFF.md` com a tarefa atual, o que foi feito e os próximos passos. Hooks nos dois agentes registram o andamento a cada turno.
3. **Um comando `ai`** que abre um agente, troca para o outro quando você sai e mantém o modo de permissões (SAFE ou YOLO) durante toda a sessão.

```
$ ai yolo
── Claude · YOLO · meu-projeto · handoff ✔ ──
   permissões irrestritas: o agente roda comandos e edita sem pedir
   sair: /exit ou Ctrl+C duas vezes  →  o ai mostra as opções de troca

   (Claude Code aberto normalmente)

── Claude saiu (código 0) · YOLO ──
  Enter trocar para Codex
  c     Codex retomando a última conversa dele
  h     Claude atualiza o HANDOFF.md, depois troca para Codex
  r     reabrir Claude
  R     Claude retomando esta conversa
  m     reabrir Claude em SAFE
  q     sair
```

## Requisitos

- Linux ou macOS (testado no Linux com zsh; o macOS deve funcionar, mas não foi testado).
- Python 3.11 ou mais novo. Só a biblioteca padrão, sem dependências.
- Git.
- [Claude Code](https://docs.anthropic.com/claude-code) e/ou [Codex CLI](https://github.com/openai/codex) instalados e logados. Dá para instalar o ai-switch antes de ter os dois: a parte de cada um fica pronta.

## Instalação

```sh
git clone https://github.com/Lovelex/ai-switch ~/.local/share/ai-switch
~/.local/share/ai-switch/install.sh
```

Para ver o que seria alterado sem gravar nada: `install.sh --dry-run`.

Depois da instalação, falta um passo no Codex: abra o `codex`, digite `/hooks` e aprove os 3 hooks do ai-switch. O Codex não executa hooks novos sem essa aprovação, e pede de novo se o arquivo mudar.

Por fim, em cada projeto onde quiser o handoff:

```sh
cd meu-projeto
ai handoff enable
ai doctor
```

### O que o instalador altera

Tudo é acréscimo. Antes de mexer em cada arquivo, o instalador guarda uma cópia em `~/.local/state/ai-switch/backup/`. Ele pode rodar de novo sem duplicar nada.

| Arquivo | Alteração |
|---|---|
| `~/.local/bin/ai` | link para o `ai.py` |
| `~/.claude/CLAUDE.md` | seção "Continuidade entre agentes" no fim, entre marcadores |
| `~/.claude/settings.json` | 3 hooks (`SessionStart`, `UserPromptSubmit`, `Stop`); os outros hooks ficam |
| `~/.codex/AGENTS.md` | link para `~/.claude/CLAUDE.md` (só se ainda não existir) |
| `~/.codex/config.toml` | `project_doc_fallback_filenames = ["CLAUDE.md"]` e `project_doc_max_bytes = 196608` |
| `~/.codex/hooks.json` | os mesmos 3 hooks |
| ignore global do git | `/HANDOFF.md` e `/.handoff-auto.md` |

## Comandos

| Comando | O que faz |
|---|---|
| `ai` | abre o último agente usado neste projeto (Claude na primeira vez), em SAFE |
| `ai claude` / `ai codex` | sessão SAFE começando por esse agente |
| `ai yolo [claude\|codex]` | sessão com permissões irrestritas nos dois agentes |
| `ai safe [claude\|codex]` | sessão protegida, com a sandbox e as aprovações oficiais de cada um |
| `ai switch` | sessão SAFE com o agente que não foi usado por último aqui; `ai yolo switch` em YOLO |
| `ai status` | projeto, último agente, sessões abertas, memória e MCP |
| `ai mcp [--check]` | MCPs dos dois lados com o estado da conexão |
| `ai mcp sync [--apply]` | copia para o Codex os MCPs compatíveis do Claude |
| `ai doctor` | diagnóstico completo |
| `ai handoff [enable]` | mostra o que o próximo agente vai receber, ou liga o handoff no projeto |
| `ai help` | ajuda |

### Flags e subcomandos das CLIs

Com argumentos, `ai claude` e `ai codex` executam a CLI pura, sem acrescentar nada, e devolvem o mesmo código de saída:

```sh
ai codex --help          # = codex --help
ai claude mcp list       # = claude mcp list
ai codex resume --last   # = codex resume --last
```

Para abrir uma sessão do ai com flags extras, diga o modo antes: `ai yolo codex -m gpt-5.5`. As flags vão para aquele agente toda vez que ele for aberto na sessão.

## Como funciona

### O agente roda no terminal de verdade

O `ai` inicia o Claude Code ou o Codex como processo filho, usando o mesmo terminal, sem PTY intermediário e sem interceptar o teclado. Por isso, tudo das CLIs continua igual: comandos com barra, atalhos, skills, MCPs, escolha de modelo, retomada de sessões e as novidades de versões futuras. O ai só aparece entre uma sessão e outra.

A troca acontece quando você sai do agente. Trocar com o agente aberto exigiria capturar o teclado dele, e isso quebraria as interfaces.

### Modos de permissão

| | SAFE | YOLO |
|---|---|---|
| Claude Code | `--permission-mode default` | `--dangerously-skip-permissions` |
| Codex | `--sandbox workspace-write --ask-for-approval on-request` | `--dangerously-bypass-approvals-and-sandbox` |

- O modo vale para a sessão inteira do `ai`, em todas as trocas, sem repetir flags.
- Ele vive só no processo do `ai`. Nada é gravado nas configurações do Claude ou do Codex.
- Uma sessão nova começa sempre em SAFE, a menos que você digite `ai yolo`.
- Para mudar de modo no meio, saia do agente e aperte `m`: ele reabre no outro modo. O ai nunca presume que um processo em execução mudou de permissão.
- O modo aparece na linha antes de cada abertura e no título da aba (`⚠ Claude · YOLO · projeto`). No VS Code, o título só aparece se `terminal.integrated.tabs.title` contiver `${sequence}`.
- O agente e os hooks recebem o modo na variável `AI_TERMINAL_MODE`.

### Instruções compartilhadas

| Camada | Fonte | Claude Code | Codex |
|---|---|---|---|
| Global | `~/.claude/CLAUDE.md` | nativo | `~/.codex/AGENTS.md` é link para ele |
| Projeto | `CLAUDE.md` de cada repositório | nativo | `project_doc_fallback_filenames` |
| Memória durável | memória automática do Claude (`~/.claude/projects/<projeto>/memory/`) | nativo | o hook de início mostra o índice |
| Sessão | `HANDOFF.md` + `.handoff-auto.md` | hooks | hooks |

Observações:

- Se um projeto tiver um `AGENTS.md` próprio, o Codex usa esse arquivo no lugar do `CLAUDE.md` (ele lê um arquivo por pasta).
- O Codex não segue as importações `@arquivo` do Claude Code. As regras pedem que ele leia o arquivo quando precisar.
- Skills do Claude citadas nas suas regras (`/nome`) podem ser compartilhadas com um link: `ln -s ~/.claude/skills/nome ~/.codex/skills/nome`. O `ai doctor` mostra quais faltam.

### Handoff

- **`HANDOFF.md`** é escrito pelo agente: tarefa atual, feito, em andamento, planejado, decisões, problemas, testes e próximos passos, em até cerca de 60 linhas. As regras pedem que o agente o reescreva ao concluir uma etapa, ao mudar de direção ou antes de parar com algo pela metade. Tarefas pequenas não mexem nele.
- **`.handoff-auto.md`** é escrito pelos hooks a cada turno: os 5 últimos pedidos, a última resposta, a branch, os últimos commits e as mudanças sem commit. Ele existe para quando a sessão cai sem aviso (limite de uso, terminal fechado), porque não depende do agente.
- **No início de cada sessão**, o hook entrega ao agente o `HANDOFF.md`, o último pedido e a última resposta. Se ele foi aberto por uma troca, entrega também um aviso dizendo qual agente estava antes. As regras mandam conferir o `git status` antes de confiar no handoff.
- **Na troca**, a opção `h` do menu pede ao agente que acabou de sair, retomando a conversa dele, para atualizar o `HANDOFF.md`. Se ele saiu por limite de uso, esse pedido falha e o menu avisa.

A conversa de um modelo não passa para o outro. O que passa é o handoff, o estado automático, a memória e o próprio código.

O handoff só funciona nos projetos onde você rodou `ai handoff enable`. Os dois arquivos ficam fora do git. O estado bruto fica em `.git/ai-switch.json`.

### MCP

- A configuração do Claude é a fonte: `~/.claude.json` (usuário e local), o `.mcp.json` do projeto, os plugins e os conectores, lidos pelo `claude mcp list`.
- `ai mcp` mostra os dois lados. No Claude, a conexão é testada de verdade. No Codex, `--check` inicia os servidores stdio e lista as ferramentas; os HTTP só se verificam dentro do Codex.
- `ai mcp sync` mostra o que pode ser copiado e `ai mcp sync --apply` copia, usando o `codex mcp add` oficial e guardando antes uma cópia da configuração.
- Não são copiados:
  - os conectores da conta claude.ai, que só existem no Claude;
  - servidores SSE, que o Codex não aceita;
  - servidores com variáveis de ambiente ou headers na configuração, porque podem conter credenciais. Esses são configurados à mão.
- Nada é apagado do Codex.
- Login OAuth é separado em cada ferramenta: `codex mcp login <nome>`. Tokens nunca são copiados.

### Diagnóstico

`ai doctor` confere as CLIs e as versões, o comando no PATH, o login do Codex, as instruções compartilhadas, o tamanho do `CLAUDE.md` do projeto em relação ao limite do Codex, os hooks, as skills citadas nas regras, o handoff do projeto e os MCPs. Cada item sai como funcionando, precisa configurar, não disponível ou não verificado.

## Segurança e privacidade

- Nenhum arquivo de autenticação é lido, copiado ou ligado entre as ferramentas.
- Antes de gravar um pedido, os hooks removem padrões comuns de chaves e senhas (`sk-...`, `ghp_...`, `api_key=...` e parecidos). É uma proteção básica: não cole segredos no chat.
- Os arquivos de handoff ficam no seu computador e fora do git.
- O ai-switch não roda nada em segundo plano, não faz commits e não acessa a rede. A única exceção são os testes de MCP, que conectam aos servidores que você já configurou.

## Limitações

- A troca acontece fora do agente: você sai, escolhe no menu e o outro abre.
- O `ai doctor` não consegue confirmar, sem gastar tokens, se o login do Codex ainda vale nem se os hooks já foram aprovados no `/hooks`.
- O `.handoff-auto.md` guarda só os últimos pedidos e a última resposta, cortados. O raciocínio interno de um modelo não é recuperável.
- Windows não é suportado por enquanto.

## Atualizar e desinstalar

```sh
git -C ~/.local/share/ai-switch pull
~/.local/share/ai-switch/install.sh     # atualiza a seção de regras e os hooks
```

Se os hooks mudarem numa versão nova, aprove de novo no `/hooks` do Codex.

```sh
~/.local/share/ai-switch/uninstall.sh            # tira tudo o que o instalador pôs
~/.local/share/ai-switch/uninstall.sh --purge    # e apaga ~/.local/state/ai-switch
```

Não são removidos:

- os `HANDOFF.md` dos projetos;
- os MCPs copiados para o Codex (`codex mcp remove <nome>`);
- links de skills que você tenha criado.

## Desenvolvimento

```sh
python3 -m unittest discover -s tests -v
```

Os testes rodam num `HOME` temporário, com Claude e Codex falsos no PATH. Cobrem:

- o instalador: preserva o que existe, não duplica, simulação e desinstalação;
- os hooks: só agem com o handoff ligado, removem segredos, entregam o contexto e avisam da troca;
- o ciclo de troca num terminal virtual: YOLO mantido nas trocas, SAFE oficial, pasta preservada, retomada, último agente e código de saída.

## Licença

MIT
