"""
O assistente de combate DENTRO DO BROWSER (Pyodide, num Web Worker).

E o mesmo codigo do servidor local (servidor.py, motor.py, barras.py, ronda.py e o
motor do projecto), com tres coisas trocadas, porque no browser nao ha rede por
sockets nem fios:

  1. os pedidos ao jogo vao por XMLHttpRequest sincrono (no Worker e permitido).
     A API aceita pedidos de outra origem (access-control-allow-origin: *) e
     responde sem os cabecalhos do jogo (verificado a 24/11 em todos os
     endpoints que o assistente usa);
  2. em vez do agendador com fios, o Worker chama `passo()` em ciclo: uma leitura
     de cada vez, pela mesma escolha do servidor (`fontes_devidas`), e o motor;
  3. o limitador nunca espera (no browser esperar e queimar CPU): uma fonte so
     comeca com menos de 40 pedidos no ultimo minuto (20 para a aba Drops, uma
     batalha de cada vez), e um pedido acima do tecto de 70 falha em vez de passar.
     O jogo aceita 100 por IP (e o jogo aberto noutro separador gasta da mesma quota).

Cada pessoa que abre a pagina usa o SEU browser e a SUA quota da API. Nada passa
por servidor nenhum. O historico fica no IndexedDB desse browser.
"""
from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request

from warera import api, optimizador as O
from warera.combate import servidor as SV

LIMITE_SUAVE = 40        # uma fonte so comeca abaixo disto (pedidos no ultimo minuto); o alvo mostrado e 45
LIMITE_SUAVE_DROPS = 20  # a aba Drops, a ultima da fila, uma batalha de cada vez
TECTO = 70               # nunca mais do que isto por minuto: o jogo aceita 100 por IP e o jogo aberto noutro
                         # separador tambem gasta da mesma quota


class _Resposta(io.BytesIO):
    status = 200


def _xhr_urlopen(req, *a, **k):
    from js import XMLHttpRequest  # so existe no Pyodide
    url = req.full_url if hasattr(req, "full_url") else str(req)
    x = XMLHttpRequest.new()
    x.open("GET", url, False)
    try:
        x.send()
    except Exception as e:  # noqa: BLE001
        raise urllib.error.URLError(f"sem ligação ({str(e)[:80]})")
    if not x.status:
        raise urllib.error.URLError("sem ligação")
    corpo = (x.responseText or "").encode("utf-8")
    if x.status >= 400:
        hdrs = {"ratelimit-reset": x.getResponseHeader("ratelimit-reset"),
                "Retry-After": x.getResponseHeader("retry-after")}
        raise urllib.error.HTTPError(url, x.status, x.statusText, hdrs, io.BytesIO(corpo))
    r = _Resposta(corpo)
    r.status = x.status
    return r


class LimitadorWeb(SV.Limitador):
    """O mesmo registo do servidor (a janela de 60 s, a pausa depois de um 429), mas sem esperas."""

    def pedir(self, endpoint, prio=None):
        with self.cv:
            t = time.time()
            self._limpar(t)
            if t < self.pausa_ate:
                raise api.ErroAPI(f"{endpoint}: o jogo pediu uma pausa (HTTP 429)")
            if len(self.feitos) >= self.por_min:
                raise api.ErroAPI(f"{endpoint}: tecto de {self.por_min} pedidos por minuto")
            self.feitos.append(t)
            self.ultimo = t
            self.por_endpoint[endpoint] = self.por_endpoint.get(endpoint, 0) + 1

    def estado(self):
        e = super().estado()
        e["limite_min"], e["tecto"] = 45, self.por_min   # o alvo e 45; o tecto, que nunca passa, 70
        return e


class _ExecutorSincrono:
    """
    `ThreadPoolExecutor` sem fios: no browser nao se podem criar. As mesmas tarefas correm uma
    a seguir a outra, com o mesmo resultado (e os pedidos continuam a passar pelo limitador).
    """

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def submit(self, fn, *a, **k):
        from concurrent.futures import Future
        f = Future()
        try:
            f.set_result(fn(*a, **k))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def map(self, fn, *iteraveis, timeout=None, chunksize=1):
        return [fn(*args) for args in zip(*iteraveis)]

    def shutdown(self, wait=True, **k):
        pass


def _sem_fios():
    import concurrent.futures as cf
    from warera import bounty, craft, mercado
    cf.ThreadPoolExecutor = _ExecutorSincrono
    for mod in (mercado, craft, bounty, O, SV):
        if hasattr(mod, "ThreadPoolExecutor"):
            mod.ThreadPoolExecutor = _ExecutorSincrono


def _dormir(s):
    """`time.sleep` no browser: so as esperas curtas das repeticoes do api.call (a pausa longa e do limitador)."""
    fim = time.time() + min(float(s), 2.5)
    while time.time() < fim:
        pass


def iniciar(jogador=None, pedir_nome=False):
    """`pedir_nome`: a pagina publica nao abre em ninguem; so le o jogo depois de se escolher o jogador."""
    _sem_fios()
    SV.LIM = LimitadorWeb(TECTO, espaco=0)
    SV.DROPS_POR_LEITURA = 1
    SV._urlopen_original = _xhr_urlopen
    urllib.request.urlopen = SV._urlopen_limitado
    time.sleep = _dormir
    api.ESPERA_429_MAX = 2.0
    if jogador:
        SV.UID = str(jogador)
    elif pedir_nome:
        SV.UID = None
    SV.criar_fontes()
    SV.MOTOR = SV.Motor(0)
    if SV.CAMINHO_VALIDACAO.exists():
        try:
            SV.VALIDACAO.update(json.loads(SV.CAMINHO_VALIDACAO.read_text(encoding="utf-8")))
        except ValueError:
            pass
    SV.SEMENTE = SV.semente()
    O.drops()
    return SV.UID


def passo():
    """Uma leitura (a mais prioritaria que caiba no limite) e um passo do motor. Devolve as versoes."""
    t = time.time()
    SV.E.ultimo_cliente = t
    lidas = []
    if SV.UID is None:      # ainda nao se escolheu o jogador: nao se le nada
        m = SV.MOTOR
        return json.dumps({"versao": m.versao_pub, "recursos": m.versao_recursos, "drops": SV.DROPS["versao"],
                           "lidas": [], "jogador": None})
    for f in SV.fontes_devidas(t):
        suave = LIMITE_SUAVE_DROPS if f.prio >= 7 else LIMITE_SUAVE
        if SV.LIM.no_ultimo_minuto() >= suave or t < SV.LIM.pausa_ate:
            break
        f.a_ler = True
        SV.correr_fonte(f)
        lidas.append(f.nome)
        break               # uma de cada vez: o Worker volta ao browser entre leituras
    m = SV.MOTOR
    try:
        SV.passo_motor(m)
    except Exception as e:  # noqa: BLE001
        m.erros.append(f"{SV.agora_iso()} {type(e).__name__}: {str(e)[:300]}")
        m.erros = m.erros[-20:]
    return json.dumps({"versao": m.versao_pub, "recursos": m.versao_recursos, "drops": SV.DROPS["versao"],
                       "lidas": lidas, "jogador": SV.UID})


def pedido(metodo, caminho, corpo=""):
    """A mesma API local do servidor (`servidor.responder`), para a pagina."""
    codigo, dados = SV.responder(metodo, caminho, (corpo or "").encode("utf-8"))
    return json.dumps({"codigo": codigo, "corpo": dados.decode("utf-8")})
