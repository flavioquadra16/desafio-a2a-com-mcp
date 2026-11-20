# A Ponte: um agente A2A com MCP por dentro

Dois processos. O servidor MCP expõe as salas. O agente consome esse servidor por HTTP e se oferece ao mundo como servidor A2A. Nenhum dos dois tem sessão: o estado da pergunta viaja no `requestState`, e o estado da conversa vive na Task.

## Como rodar

A partir de um clone limpo, com Python 3.10 ou superior e Node fora do caminho (esta entrega é Python). Os comandos abaixo são bash. No PowerShell, a ativação do venv e a variável de ambiente estão no bloco seguinte.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt

export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
python3 servidor-mcp/servidor.py
```

Em outro terminal, com o mesmo venv ativo:

```bash
source .venv/bin/activate
python3 agente/agente.py
```

O agente espera o MCP por até 30 segundos. O servidor MCP sobe em `http://127.0.0.1:7301/mcp`. O agente sobe em `http://127.0.0.1:7300`, com o card em `/.well-known/agent-card.json` e o JSON-RPC em `/a2a`. As portas e a URL do MCP saem de `MCP_PORT`, `MCP_HOST`, `AGENTE_PORT`, `AGENTE_HOST`, `AGENTE_BASE_URL` e `MCP_URL`. Os padrões são os que o validador usa.

Em um terceiro terminal:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Rode o validador com os dois processos recém-iniciados. As reservas ficam em memória e mudam o resultado da execução seguinte.

No PowerShell, no lugar do `export`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:REQUEST_STATE_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
python servidor-mcp/servidor.py
```

O segundo terminal só precisa do venv e de `python agente/agente.py`. A chave fica no terminal do servidor MCP.

O validador imprime um trace-id na primeira linha. Esse mesmo valor aparece no stderr do servidor MCP, no `traceparent` dos `tools/call` daquela Task. O `tools/list` do agente sai antes do primeiro `tools/call`.

## Onde a ponte acontece

A pausa está em `pausar`, em `agente/agente.py`. Quando o `tools/call` de `reservar_sala` volta com `resultType` igual a `input_required`, essa função guarda o `requestState` e a chave de `inputRequests` na Task, monta a linha `alternativas:` na ordem do `enum` (ou do `const`, quando só há uma sala) e coloca a Task em `TASK_STATE_INPUT_REQUIRED`. Nada disso é respondido pelo agente: a pergunta volta para o cliente A2A.

A volta está em `retomar`, no mesmo arquivo. O `SendMessage` seguinte, com `escolha=<id>` ou `escolha=recusar`, repete o `tools/call` original com um id de JSON-RPC novo, os mesmos argumentos, `inputResponses` na chave que veio em `inputRequests` e o `requestState` ecoado sem alteração. `escolha=recusar` vira `action` igual a `decline`, e a Task termina em `TASK_STATE_CANCELED`.

## Decisões técnicas

O `requestState` é selado pelo codec AES-256-GCM do SDK (`RequestStateSecurity` em `servidor-mcp/servidor.py`). A chave vem só de `REQUEST_STATE_SECRET`, com no mínimo 32 bytes, gerada como no comando acima. O valor não está no repositório. O token vale 15 minutos (`ttl` de 900 segundos), dentro da janela de 5 a 30 minutos. O SDK também amarra o token ao método, à tool e ao digest dos argumentos: um retry com argumentos diferentes é rejeitado com `-32602`, então o valor adulterado não vira reserva. Um caractere trocado no token falha a verificação e recebe o mesmo `-32602`. Como a chave é a do ambiente, o token continua válido depois de reiniciar o processo.

As reservas moram numa lista em memória no servidor, carregada de `dados/reservas.json` na subida. Não sobrevivem a restart. As Tasks moram num dicionário no processo do agente, uma por `id`, com o `requestState` num campo que `publica()` não serializa. Card, histórico, status e artifact não carregam o token.

O agente não usa o `Client` do SDK no loop automático. Esse loop responderia a elicitation sozinho e a Task nunca pausaria. Ele fala o JSON-RPC stateless por HTTP, com `_meta`, a capability `elicitation.form` e os headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name`. As tools são as que `tools/list` devolveu na subida. A versão da política é a primeira linha de `politica://uso`, lida por `resources/read` antes de qualquer reserva. Não há SDK de LLM no caminho.

## Saída do validador

Última execução, com os dois processos recém-iniciados. Código de saída 0.

```
trace-id desta execucao: c4d76163d52bbc9a85f7c281da26b71e
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
