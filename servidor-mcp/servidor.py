"""Servidor MCP da Central de Salas.

Transporte Streamable HTTP, stateless, protocolo 2026-07-28. O conflito de
reserva nao abre canal de volta: a tool termina com input_required, e o
pedido original viaja no requestState selado com a chave de REQUEST_STATE_SECRET.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Literal

import uvicorn
from pydantic import BaseModel, Field, create_model
from mcp.server import MCPServer
from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"
FUSO = timezone(timedelta(hours=-3))
JANELA_INICIO = 8 * 3600
JANELA_FIM = 20 * 3600
DURACAO_MAXIMA = timedelta(hours=2)
TTL_REQUEST_STATE = 15 * 60
PERGUNTA = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."

MSG_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
MSG_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
MSG_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
MSG_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


def versao_da_politica() -> str:
    primeira = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8").splitlines()[0]
    return primeira.split(":", 1)[1].strip()


VERSAO_POLITICA = versao_da_politica()


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class EscolhaDireta(BaseModel):
    """Sala confirmada sem pergunta: o intervalo pedido estava livre."""

    sala: str


class Agenda:
    """Reservas em memoria. O requestState nao mora aqui: ele viaja com o cliente."""

    def __init__(self) -> None:
        self.salas = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
        self.por_id = {sala["id"]: sala for sala in self.salas}
        self.reservas: list[dict[str, str]] = json.loads(
            (DADOS / "reservas.json").read_text(encoding="utf-8")
        )
        self._seq = max(int(reserva["id"].split("-")[1]) for reserva in self.reservas)
        self._lock = Lock()

    def sala(self, sala_id: str) -> dict[str, Any] | None:
        return self.por_id.get(sala_id)

    def conflitos(self, sala_id: str, inicio: datetime, fim: datetime) -> list[dict[str, str]]:
        with self._lock:
            return [
                dict(reserva)
                for reserva in self.reservas
                if reserva["sala"] == sala_id and _sobrepoe(inicio, fim, reserva)
            ]

    def alternativas(self, sala_id: str, inicio: datetime, fim: datetime) -> list[str]:
        pedida = self.por_id[sala_id]
        candidatas = [
            sala
            for sala in self.salas
            if sala["id"] != sala_id
            and sala["capacidade"] >= pedida["capacidade"]
            and not self.conflitos(sala["id"], inicio, fim)
        ]
        candidatas.sort(key=lambda sala: (sala["capacidade"], sala["id"]))
        return [sala["id"] for sala in candidatas[:3]]

    def criar(self, sala_id: str, inicio: str, fim: str, responsavel: str) -> dict[str, str] | None:
        inicio_dt = _instante(inicio)
        fim_dt = _instante(fim)
        with self._lock:
            ocupada = any(
                reserva["sala"] == sala_id and _sobrepoe(inicio_dt, fim_dt, reserva)
                for reserva in self.reservas
            )
            if ocupada:
                return None
            self._seq += 1
            reserva = {
                "id": f"res-{self._seq:04d}",
                "sala": sala_id,
                "inicio": inicio,
                "fim": fim,
                "responsavel": responsavel,
            }
            self.reservas.append(reserva)
            return dict(reserva)


def _instante(texto: str) -> datetime:
    momento = datetime.fromisoformat(texto)
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=FUSO)
    return momento


def _sobrepoe(inicio: datetime, fim: datetime, reserva: dict[str, str]) -> bool:
    outra_inicio = _instante(reserva["inicio"])
    outra_fim = _instante(reserva["fim"])
    return inicio < outra_fim and outra_inicio < fim


def _dentro_da_janela(momento: datetime) -> bool:
    local = momento.astimezone(FUSO)
    segundos = local.hour * 3600 + local.minute * 60 + local.second
    return JANELA_INICIO <= segundos <= JANELA_FIM


def validar_pedido(sala_id: str, inicio: str, fim: str) -> str | None:
    if agenda.sala(sala_id) is None:
        return f"Sala inexistente: {sala_id}"
    try:
        inicio_dt = _instante(inicio)
        fim_dt = _instante(fim)
    except ValueError:
        return MSG_INTERVALO
    if fim_dt <= inicio_dt:
        return MSG_INTERVALO
    if not _dentro_da_janela(inicio_dt) or not _dentro_da_janela(fim_dt):
        return MSG_JANELA
    if fim_dt - inicio_dt > DURACAO_MAXIMA:
        return MSG_DURACAO
    return None


_modelos: dict[tuple[str, ...], type[BaseModel]] = {}


def modelo_escolha(opcoes: list[str]) -> type[BaseModel]:
    chave = tuple(opcoes)
    modelo = _modelos.get(chave)
    if modelo is None:
        tipo_sala = Literal.__getitem__(chave)
        modelo = create_model(
            "EscolhaDeSala",
            sala=(tipo_sala, Field(description="Sala alternativa escolhida")),
        )
        _modelos[chave] = modelo
    return modelo


def chave_do_request_state() -> str:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        print(
            "REQUEST_STATE_SECRET ausente ou curta demais (minimo 32 bytes). "
            'Gere com: python -c "import secrets; print(secrets.token_hex(32))"',
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1)
    return segredo


agenda = Agenda()
mcp = MCPServer(
    "central-de-salas",
    version="1.0.0",
    instructions="Reservas das salas de reuniao da Hill Valley Tech.",
    request_state_security=RequestStateSecurity(keys=[chave_do_request_state()], ttl=TTL_REQUEST_STATE),
)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(
        salas=[
            SalaOut(
                id=sala["id"],
                nome=sala["nome"],
                capacidade=sala["capacidade"],
                recursos=list(sala["recursos"]),
            )
            for sala in agenda.salas
        ]
    )


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    erro = validar_pedido(sala, inicio, fim)
    if erro:
        raise ToolError(erro)
    conflitos = agenda.conflitos(sala, _instante(inicio), _instante(fim))
    return Disponibilidade(
        sala=sala,
        livre=not conflitos,
        conflitos=[
            ConflitoOut(
                id=reserva["id"],
                inicio=reserva["inicio"],
                fim=reserva["fim"],
                responsavel=reserva["responsavel"],
            )
            for reserva in conflitos
        ],
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> BaseModel | Elicit[BaseModel]:
    """Pede uma sala alternativa so quando o intervalo pedido ja esta ocupado.

    Sem conflito, devolve a propria sala e a tool segue. Com conflito e sem
    alternativa, e erro de execucao: nao ha pergunta a fazer. A chave deste
    resolver no fio e atribuida pelo SDK (module:qualname).
    """

    erro = validar_pedido(sala, inicio, fim)
    if erro:
        raise ToolError(erro)
    if not agenda.conflitos(sala, _instante(inicio), _instante(fim)):
        return EscolhaDireta(sala=sala)
    opcoes = agenda.alternativas(sala, _instante(inicio), _instante(fim))
    if not opcoes:
        raise ToolError(MSG_SEM_ALTERNATIVA)
    return Elicit(PERGUNTA, modelo_escolha(opcoes))


def _recusa(motivo: str) -> ReservaOut:
    return ReservaOut(
        reserva=None,
        reservado=False,
        sala=None,
        inicio=None,
        fim=None,
        responsavel=None,
        politica=None,
        motivo=motivo,
    )


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[BaseModel], Resolve(escolha_de_sala)],
) -> ReservaOut:
    if isinstance(escolha, DeclinedElicitation):
        return _recusa("recusado")
    if isinstance(escolha, CancelledElicitation):
        return _recusa("cancelado")
    if not isinstance(escolha, AcceptedElicitation):
        raise ToolError(MSG_SEM_ALTERNATIVA)

    # Argumentos adulterados no retry nao chegam aqui: o SDK rejeita o
    # requestState quando o digest dos argumentos nao bate com o que foi selado.
    sala_final = escolha.data.sala
    criada = agenda.criar(sala_final, inicio, fim, responsavel)
    if criada is None:
        raise ToolError(MSG_SEM_ALTERNATIVA)
    return ReservaOut(
        reserva=criada["id"],
        reservado=True,
        sala=criada["sala"],
        inicio=criada["inicio"],
        fim=criada["fim"],
        responsavel=criada["responsavel"],
        politica=VERSAO_POLITICA,
        motivo=None,
    )


@mcp.resource("politica://uso", mime_type="text/markdown", description="Politica de uso das salas.")
def politica_de_uso() -> str:
    return (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")


class LogRequisicoes:
    """Uma linha no stderr por POST: metodo, id e traceparent quando vier no _meta."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        corpo = bytearray()
        desconectou = False
        while True:
            mensagem = await receive()
            if mensagem["type"] == "http.request":
                corpo += mensagem.get("body", b"")
                if not mensagem.get("more_body", False):
                    break
            elif mensagem["type"] == "http.disconnect":
                desconectou = True
                break
            else:
                break

        _log_request(bytes(corpo))
        enviado = False

        async def replay() -> dict[str, Any]:
            nonlocal enviado
            if not enviado:
                enviado = True
                if desconectou and not corpo:
                    return {"type": "http.disconnect"}
                return {"type": "http.request", "body": bytes(corpo), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def _log_request(corpo: bytes) -> None:
    metodo = "-"
    identificador: Any = "-"
    traceparent = None
    try:
        payload = json.loads(corpo)
    except (json.JSONDecodeError, UnicodeError):
        payload = None
    if isinstance(payload, dict):
        metodo = str(payload.get("method", "-"))
        identificador = payload.get("id", "-")
        meta = (payload.get("params") or {}).get("_meta") or {}
        if isinstance(meta, dict):
            traceparent = meta.get("traceparent")
    if traceparent:
        linha = f"mcp request method={metodo} id={identificador} traceparent={traceparent}"
    else:
        linha = f"mcp request method={metodo} id={identificador}"
    print(linha, file=sys.stderr, flush=True)


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    porta = int(os.environ.get("MCP_PORT", "7301"))
    app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        host=host,
    )
    app.add_middleware(LogRequisicoes)
    print(f"servidor MCP em http://{host}:{porta}/mcp", file=sys.stderr, flush=True)
    uvicorn.run(app, host=host, port=porta, log_level="info")


if __name__ == "__main__":
    main()
