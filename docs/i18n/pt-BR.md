# duet — dois modelos de IA constroem; o harness verifica as regras

> Esta página é uma introdução curta traduzida do README em inglês. **A referência é o [README em inglês](../../README.md)**; o registro completo de cada execução está em [docs/QA.md](../QA.md).

Um único agente de IA corrige a própria lição de casa e diz que passou. O duet coloca um **segundo modelo** no processo, **roda os seus testes por conta própria** e prende cada fluxo de trabalho a uma regra que ele verifica contra o que realmente aconteceu — não contra o que qualquer um dos modelos diz.

## Instalação

```bash
curl -fsSL https://raw.githubusercontent.com/shubharya-os/duet/main/install.sh | sh
```

Ou, dentro do Claude Code, como plugin:

```
/plugin marketplace add shubharya-os/duet
/plugin install duet@duet
```

Usa as assinaturas do Claude e do ChatGPT que você já tem — **sem chaves de API**. Com uma só assinatura também funciona (por exemplo `--pair claude:opus+claude:sonnet`).

## Nove comandos, cada um com uma regra verificada

```bash
duet build "um app web de despesas"        # do zero: critérios, depois testes, depois código
duet fix "a exportação perde linhas com vírgula" # os testes devem falhar no código original e passar agora
duet add "uma opção --json no report"      # um teste deve falhar sem a funcionalidade
duet refactor "dividir parser.py em dois"  # os testes existentes não podem mudar nem um byte
duet plan "migrar o armazenamento para Postgres" # discutem e só o PLAN.md pode mudar
duet review                                # outro modelo revisa o seu diff, somente leitura
duet make "…" --like URL                   # pesquisa as referências, constrói, mede a paridade, sala limpa
duet jury                                  # usuários, clientes, investidores e jurados simulados; cada ponto citado
duet page check index.html                 # o portão de um site: o que aparece errado no desktop e no celular
```

O `fix` **executa de novo os testes finais sobre uma cópia do código original**. Se eles também passam no código com o bug, não detectam o bug, e as duas aprovações são anuladas.

## Resultados

A partir de uma frase e de um diretório vazio, o duet construiu um app web de despesas (interface de página única, API JSON, SQLite, exportação CSV) em **35 minutos**. Depois nós o atacamos à mão: a injeção de SQL foi guardada como texto, o XSS armazenado apareceu como texto num navegador de verdade, a injeção de fórmulas em CSV foi neutralizada e `0.1 + 0.2` somou exatamente `0.30`.

## Tornar o padrão

```bash
duet skill default    # desfazer: duet skill undefault
```

A partir daí, Claude Code, Codex, Gemini CLI, Antigravity e OpenCode passam ao duet as mudanças reais de código (corrigir bugs, adicionar funcionalidades, refatorar), e continuam tratando diretamente as perguntas e as mudanças de uma linha.
