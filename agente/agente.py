"""Agente A2A da Central de Salas.

Por fora e um servidor A2A (card, SendMessage, GetTask). Por dentro e host MCP:
descobre as tools, le a politica e chama o servidor por HTTP. Nao ha LLM e nao
ha regra de sala aqui. A ponte e so traducao de estado: input_required vira
TASK_STATE_INPUT_REQUIRED, e o requestState volta inteiro no retry.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

PROTOCOLO = "2026-07-28"
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
BASE = os.environ.get("AGENTE_BASE_URL", f"http://{HOST}:{PORTA}").rstrip("/")

TERMINAIS = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_FAILED",
}

_trava = threading.Lock()
_ids = 0
_tasks: dict[str, "Task"] = {}
_ferramentas: set[str] = set()
_versao_politica = ""


def _proximo_id() -> int:
    global _ids
    with _trava:
        _ids += 1
        return _ids


def _trace_id(traceparent: str | None) -> str | None:
    if not traceparent:
        return None
    partes = traceparent.strip().split("-")
    if len(partes) != 4 or len(partes[1]) != 32:
        return None
    return partes[1]


def _traceparent(trace_id: str | None) -> str | None:
    if not trace_id:
        return None
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


def _parse_corpo_mcp(bruto: bytes) -> dict[str, Any]:
    texto = bruto.decode()
    inicio = texto.lstrip()
    if inicio.startswith("{"):
        return json.loads(inicio)
    for linha in texto.splitlines():
        if linha.startswith("data:"):
            return json.loads(linha[5:].strip())
    raise ValueError(texto[:200])


def rpc_mcp(
    metodo: str,
    params: dict[str, Any],
    nome: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Um request MCP stateless. O id e sempre novo: o retry nao reaproveita o anterior."""

    meta: dict[str, Any] = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
        "io.modelcontextprotocol/clientInfo": {
            "name": "agente-central-de-salas",
            "version": "1.0.0",
        },
        "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}},
    }
    traceparent = _traceparent(trace_id)
    if traceparent:
        meta["traceparent"] = traceparent
    corpo = {
        "jsonrpc": "2.0",
        "id": _proximo_id(),
        "method": metodo,
        "params": {**params, "_meta": meta},
    }
    cabecalhos = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOLO,
        "Mcp-Method": metodo,
    }
    if nome:
        cabecalhos["Mcp-Name"] = nome
    pedido = urllib.request.Request(
        MCP_URL,
        data=json.dumps(corpo).encode(),
        headers=cabecalhos,
        method="POST",
    )
    try:
        with urllib.request.urlopen(pedido, timeout=30) as resposta:
            bruto = resposta.read()
    except urllib.error.HTTPError as erro:
        bruto = erro.read()
    return _parse_corpo_mcp(bruto)


def descobrir() -> None:
    """tools/list e a leitura da politica acontecem antes de qualquer tools/call."""

    global _versao_politica
    prazo = time.time() + 30
    ultimo: Exception | None = None
    while True:
        try:
            lista = rpc_mcp("tools/list", {})
            ferramentas = {
                tool["name"]
                for tool in (lista.get("result") or {}).get("tools", [])
                if isinstance(tool, dict) and tool.get("name")
            }
            if "reservar_sala" not in ferramentas:
                raise RuntimeError(f"tools/list nao trouxe reservar_sala: {sorted(ferramentas)}")
            politica = rpc_mcp("resources/read", {"uri": "politica://uso"}, "politica://uso")
            conteudo = ((politica.get("result") or {}).get("contents") or [{}])[0].get("text", "")
            _versao_politica = conteudo.splitlines()[0].split(":", 1)[1].strip()
            _ferramentas.clear()
            _ferramentas.update(ferramentas)
            print(
                f"ferramentas descobertas: {', '.join(sorted(_ferramentas))}; politica {_versao_politica}",
                file=sys.stderr,
                flush=True,
            )
            return
        except Exception as exc:
            ultimo = exc
            if time.time() > prazo:
                raise SystemExit(f"nao consegui falar com o servidor MCP em {MCP_URL}: {ultimo}")
            time.sleep(0.5)


def _texto(resultado: dict[str, Any]) -> str:
    return " ".join(
        parte.get("text", "")
        for parte in resultado.get("content") or []
        if isinstance(parte, dict)
    )


def _conteudo(resultado: dict[str, Any]) -> dict[str, Any]:
    estruturado = resultado.get("structuredContent")
    if isinstance(estruturado, dict):
        return estruturado
    try:
        carregado = json.loads(_texto(resultado))
    except json.JSONDecodeError:
        return {}
    return carregado if isinstance(carregado, dict) else {}


def _alternativas(resultado: dict[str, Any]) -> tuple[str, list[str]]:
    pedidos = resultado.get("inputRequests") or {}
    chave = next(iter(pedidos))
    campo = ((pedidos[chave].get("params") or {}).get("requestedSchema") or {}).get("properties", {}).get("sala", {})
    if isinstance(campo.get("enum"), list):
        opcoes = [str(item) for item in campo["enum"]]
    elif "const" in campo:
        opcoes = [str(campo["const"])]
    else:
        opcoes = []
    return chave, opcoes


class Task:
    def __init__(self) -> None:
        self.id = f"task-{secrets.token_hex(6)}"
        self.context_id = f"ctx-{secrets.token_hex(6)}"
        self.state = "TASK_STATE_SUBMITTED"
        self.history: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        self.status_message: dict[str, Any] | None = None
        self.request_state: str | None = None
        self.chave: str | None = None
        self.alternativas: list[str] = []
        self.argumentos: dict[str, str] | None = None
        self.trace_id: str | None = None
        self.linha = ""

    def publica(self) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.state}
        if self.status_message is not None:
            status["message"] = self.status_message
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": self.history,
            "artifacts": self.artifacts,
        }

    def registrar_usuario(self, mensagem: dict[str, Any], texto: str) -> None:
        entrada: dict[str, Any] = {
            "messageId": mensagem.get("messageId") or f"msg-{secrets.token_hex(6)}",
            "role": mensagem.get("role") or "ROLE_USER",
            "parts": [{"text": texto}],
        }
        if mensagem.get("taskId"):
            entrada["taskId"] = mensagem["taskId"]
        self.history.append(entrada)

    def dizer(self, texto: str) -> None:
        mensagem = {
            "messageId": f"msg-{secrets.token_hex(6)}",
            "role": "ROLE_AGENT",
            "parts": [{"text": texto}],
            "taskId": self.id,
            "contextId": self.context_id,
        }
        self.history.append(json.loads(json.dumps(mensagem)))
        self.status_message = mensagem


def _falhar(task: Task, texto: str) -> None:
    task.state = "TASK_STATE_FAILED"
    task.dizer(texto)


def _cancelar(task: Task) -> None:
    task.state = "TASK_STATE_CANCELED"
    task.dizer("Reserva recusada.")


def _concluir(task: Task, dados: dict[str, Any]) -> None:
    artifact = {
        "reserva": dados.get("reserva"),
        "sala": dados.get("sala"),
        "inicio": dados.get("inicio"),
        "fim": dados.get("fim"),
        "responsavel": dados.get("responsavel"),
        "politica": _versao_politica,
    }
    task.artifacts = [
        {
            "artifactId": f"art-{secrets.token_hex(6)}",
            "name": "reserva",
            "parts": [{"text": json.dumps(artifact)}],
        }
    ]
    task.state = "TASK_STATE_COMPLETED"
    task.dizer(f"Reserva {artifact['reserva']} confirmada na {artifact['sala']}.")


def pausar(task: Task, resultado: dict[str, Any]) -> None:
    """A ponte, lado da pausa: input_required do MCP vira TASK_STATE_INPUT_REQUIRED.

    O requestState fica na Task e nao entra na mensagem, no artifact nem no card.
    """

    chave, opcoes = _alternativas(resultado)
    task.chave = chave
    task.alternativas = opcoes
    task.request_state = resultado.get("requestState")
    task.linha = "alternativas: " + ", ".join(opcoes)
    task.state = "TASK_STATE_INPUT_REQUIRED"
    task.dizer(task.linha)


def _aplicar(task: Task, resposta: dict[str, Any]) -> None:
    if resposta.get("error"):
        erro = resposta["error"]
        _falhar(task, str(erro.get("message", erro)))
        return
    resultado = resposta.get("result") or {}
    if resultado.get("resultType") == "input_required":
        pausar(task, resultado)
        return
    if resultado.get("isError"):
        _falhar(task, _texto(resultado))
        return
    dados = _conteudo(resultado)
    if dados.get("reservado") is False:
        _cancelar(task)
        return
    _concluir(task, dados)


def chamar_reserva(task: Task, argumentos: dict[str, str]) -> None:
    if "reservar_sala" not in _ferramentas:
        _falhar(task, "ferramenta reservar_sala nao foi descoberta")
        return
    task.argumentos = dict(argumentos)
    resposta = rpc_mcp(
        "tools/call",
        {"name": "reservar_sala", "arguments": task.argumentos},
        "reservar_sala",
        task.trace_id,
    )
    _aplicar(task, resposta)


def retomar(task: Task, acao: dict[str, Any]) -> None:
    """A ponte, lado da retomada: o requestState guardado volta no tools/call, com id novo."""

    if not task.request_state or not task.chave or task.argumentos is None:
        _falhar(task, "Task sem estado de pausa")
        return
    resposta = rpc_mcp(
        "tools/call",
        {
            "name": "reservar_sala",
            "arguments": task.argumentos,
            "inputResponses": {task.chave: acao},
            "requestState": task.request_state,
        },
        "reservar_sala",
        task.trace_id,
    )
    if (resposta.get("result") or {}).get("resultType") == "input_required":
        pausar(task, resposta["result"])
        return
    if acao.get("action") == "decline":
        if resposta.get("error") or (resposta.get("result") or {}).get("isError"):
            _aplicar(task, resposta)
            return
        _cancelar(task)
        return
    _aplicar(task, resposta)


def _texto_da_mensagem(mensagem: dict[str, Any]) -> str:
    return " ".join(
        parte.get("text", "")
        for parte in mensagem.get("parts") or []
        if isinstance(parte, dict)
    ).strip()


def _pedido(texto: str) -> dict[str, str] | None:
    if not texto.startswith("reservar "):
        return None
    campos: dict[str, str] = {}
    for parte in texto.split()[1:]:
        if "=" not in parte:
            return None
        chave, valor = parte.split("=", 1)
        campos[chave] = valor
    if not {"sala", "inicio", "fim", "responsavel"} <= campos.keys():
        return None
    return {
        "sala": campos["sala"],
        "inicio": campos["inicio"],
        "fim": campos["fim"],
        "responsavel": campos["responsavel"],
    }


def _escolha(texto: str) -> str | None:
    if not texto.startswith("escolha=") or " " in texto:
        return None
    return texto.split("=", 1)[1]


def enviar(mensagem: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
    texto = _texto_da_mensagem(mensagem)
    task_id = mensagem.get("taskId")
    if task_id:
        task = _tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task inexistente: {task_id}")
        if task.state in TERMINAIS:
            raise ValueError(f"Task {task.id} esta em estado terminal")
        task.registrar_usuario(mensagem, texto)
        trace = _trace_id(traceparent)
        if trace:
            task.trace_id = trace
        if task.state != "TASK_STATE_INPUT_REQUIRED":
            raise ValueError(f"Task {task.id} nao esta aguardando escolha")
        escolha = _escolha(texto)
        if escolha is None:
            task.state = "TASK_STATE_INPUT_REQUIRED"
            task.dizer(task.linha)
            return task.publica()
        task.state = "TASK_STATE_WORKING"
        if escolha == "recusar":
            retomar(task, {"action": "decline"})
        elif escolha not in task.alternativas:
            task.state = "TASK_STATE_INPUT_REQUIRED"
            task.dizer(task.linha)
        else:
            retomar(task, {"action": "accept", "content": {"sala": escolha}})
        return task.publica()

    pedido = _pedido(texto)
    if pedido is None:
        raise ValueError("pedido fora do formato reservar sala=<id> inicio=<iso> fim=<iso> responsavel=<nome>")
    task = Task()
    task.trace_id = _trace_id(traceparent)
    _tasks[task.id] = task
    task.state = "TASK_STATE_SUBMITTED"
    task.registrar_usuario(mensagem, texto)
    task.state = "TASK_STATE_WORKING"
    chamar_reserva(task, pedido)
    return task.publica()


def card() -> dict[str, Any]:
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {
            "organization": "Hill Valley Tech",
            "url": "https://hillvalley.example",
        },
        "version": "1.0.0",
        "supportedInterfaces": [
            {
                "url": f"{BASE}/a2a",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            }
        ],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, formato: str, *args: Any) -> None:
        print(f"a2a {self.address_string()} {formato % args}", file=sys.stderr, flush=True)

    def do_GET(self) -> None:  # noqa: N802
        caminho = urlparse(self.path).path
        if caminho == "/.well-known/agent-card.json":
            self._json(200, card())
            return
        self._json(404, {"error": "nao encontrado"})

    def do_POST(self) -> None:  # noqa: N802
        caminho = urlparse(self.path).path
        if caminho != "/a2a":
            self._json(404, {"error": "nao encontrado"})
            return
        tamanho = int(self.headers.get("Content-Length", "0"))
        bruto = self.rfile.read(tamanho)
        try:
            pedido = json.loads(bruto)
        except json.JSONDecodeError:
            self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            return
        if not isinstance(pedido, dict):
            self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}})
            return
        self._json(200, self._rpc(pedido))

    def _rpc(self, pedido: dict[str, Any]) -> dict[str, Any]:
        identificador = pedido.get("id")
        metodo = pedido.get("method")
        params = pedido.get("params") or {}
        if not isinstance(params, dict):
            return _erro(identificador, -32602, "Invalid params")
        try:
            if metodo == "SendMessage":
                mensagem = params.get("message")
                if not isinstance(mensagem, dict):
                    return _erro(identificador, -32602, "Invalid params")
                task = enviar(mensagem, self.headers.get("traceparent"))
                return {"jsonrpc": "2.0", "id": identificador, "result": {"task": task}}
            if metodo == "GetTask":
                task = _tasks.get(params.get("id"))
                if task is None:
                    return _erro(identificador, -32602, "Task inexistente")
                return {"jsonrpc": "2.0", "id": identificador, "result": {"task": task.publica()}}
            return _erro(identificador, -32601, "Method not found")
        except ValueError as exc:
            return _erro(identificador, -32602, str(exc))

    def _json(self, status: int, corpo: dict[str, Any]) -> None:
        payload = json.dumps(corpo).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _erro(identificador: Any, codigo: int, mensagem: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identificador, "error": {"code": codigo, "message": mensagem}}


def main() -> None:
    descobrir()
    servidor = ThreadingHTTPServer((HOST, PORTA), Handler)
    print(f"agente A2A em {BASE}/a2a", file=sys.stderr, flush=True)
    servidor.serve_forever()


if __name__ == "__main__":
    main()
