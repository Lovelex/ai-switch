# Continuidade entre agentes (Claude Code e Codex)

Alterno entre Claude Code e Codex no mesmo projeto (quando acaba o limite de um, sigo no outro). Este arquivo vale para os dois: no Codex, `~/.codex/AGENTS.md` é link para ele, e o `CLAUDE.md` de cada projeto entra como instrução do projeto. Mantido pelo ai-switch (`ai help`).

- **Para o Codex:** linhas `@arquivo` são importações do Claude Code; leia o arquivo se precisar. Skills citadas como `/nome` moram em `~/.claude/skills/<nome>/SKILL.md`; as linkadas em `~/.codex/skills` aparecem para você. "Sincronizar o CLAUDE.md" vale igual para você: é a fonte dos dois.
- **Handoff:** projeto com `HANDOFF.md` na raiz tem estado de trabalho compartilhado (fora do git). O hook de início da sessão mostra o resumo; retome só se o pedido continuar aquele trabalho, e confira `git status`/`git diff` antes de confiar nele.
- **Atualize o `HANDOFF.md`** (sem perguntar) ao concluir um marco, mudar de direção, ou antes de parar com trabalho pela metade; não a cada passo nem em tarefa pequena e isolada. Mantenha as seções do modelo, até ~60 linhas; reescreva em vez de acumular (o que terminou vira uma linha em "Feito" ou sai). Separe Feito / Em andamento / Planejado. Nunca registre senha, chave ou token.
- `.handoff-auto.md` é gerado pelos hooks a cada turno (últimos pedidos, última resposta, git), para quando a sessão cai sem aviso. Não editar; leia quando o `HANDOFF.md` parecer atrasado.
- **Memória durável do projeto** é a do Claude Code: `~/.claude/projects/<caminho do repo com / trocado por ->/memory/` (índice `MEMORY.md`, um fato por arquivo com frontmatter `name`, `description`, `metadata.type`). O Codex recebe o índice no início; ao aprender algo durável (preferência, correção, decisão que não está no código), grave lá no mesmo formato e acrescente a linha no índice.
- Projeto novo: `ai handoff enable` na raiz liga o handoff.
- O comando `ai` abre e troca os agentes; dentro dele, `AI_TERMINAL_MODE` (`safe`/`yolo`) diz o modo da sessão. Não abra `ai` de dentro de um agente: para trocar, o usuário sai do agente e usa o menu.
