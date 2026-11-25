"""
O assistente de combate: o servidor local e o recalculo continuo.

    python combate.py [--porta 8792] [--limite 45] [--jogador ID]

O que faz, enquanto houver uma pagina aberta (a pagina pergunta a cada 5 s):

  1. cada fonte le-se ao seu ritmo e por prioridade, com um limitador
     central de pedidos por minuto (todos os pedidos ao jogo passam por ele,
     incluindo as repeticoes do `api.call`) e pausa depois de um HTTP 429;
  2. uma leitura so e aceite se for mais recente do que a que ja esta
     (dados fora de ordem descartam-se) e so conta como mudanca se o
     CONTEUDO mudou;
  3. o motor ve o que mudou e recalcula so o que isso afecta (V3.2, o plano
     ate esvaziar as barras, barras.py): vida e fome -> so a leitura da
     divisao ja feita; ranking, bounty ou janela de uma ronda -> as tabelas
     dessa ronda; preco da municao -> a divisao; comida e cases -> so as
     contas; equipamento -> tudo;
  4. um calculo ultrapassado e abandonado e nunca substitui um mais recente;
  5. a estrategia publicada traz a hora dos dados e a hora do calculo, e o
     que mudou face a anterior. Tudo fica no historico (historico/*.jsonl).

Sem pagina aberta ha 2 minutos, deixa de pedir dados ao jogo.
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import itertools
import json
import math
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from warera import api, bounty as B, config, craft, mercado, optimizador as O, pecas  # noqa: E402
from warera.combate import barras as BR, motor as M3, ronda as RE  # noqa: E402

AQUI = Path(__file__).resolve().parent              # a pagina e o maldives.json
# o jogador: WARERA_JOGADOR (ambiente ou .env) ou --jogador; por omissao, BartolomeuCaixas
UID = os.getenv("WARERA_JOGADOR") or config._ENV.get("WARERA_JOGADOR") or "6a2fd47fe833fdf242f82095"
# tudo o que o assistente guarda fica em saida/combate/ (o painel e o separador Optimizador usam o resto
# de saida/; nada disto e publicado: o publicar.ps1 so leva o dist/)
DADOS = config.RAIZ / "saida" / "combate"
O.DIR = DADOS / "optimizador"
B.CAMINHO_RESUMOS = DADOS / "bounty_resumos.json"
HIST = DADOS / "historico"
HIST.mkdir(parents=True, exist_ok=True)
RONDAS = 8
SEM_PAGINA_S = 120.0
TESTE_CANCELAR = None   # so os testes o usam: forca "chegaram dados novos" a meio de um calculo


def agora_iso(t=None):
    return datetime.fromtimestamp(t or time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


DO_JOGADOR = {"jogador", "estrategias", "equipamento", "resumos_batalha"}   # o resto do historico e do jogo, partilhado


def _caminho_hist(nome):
    """O historico de um jogador fica na pasta dele (pode-se trocar de jogador); o do jogo e partilhado."""
    if nome in DO_JOGADOR:
        p = HIST / "jogadores" / str(UID)
        p.mkdir(parents=True, exist_ok=True)
        return p / f"{nome}.jsonl"
    return HIST / f"{nome}.jsonl"


def hist(nome, registo):
    with open(_caminho_hist(nome), "a", encoding="utf-8") as f:
        f.write(json.dumps(registo, ensure_ascii=False, default=str) + "\n")


def ler_hist(nome, maximo=5000):
    try:
        with open(_caminho_hist(nome), encoding="utf-8") as f:
            return [json.loads(l) for l in f.readlines()[-maximo:] if l.strip()]
    except FileNotFoundError:
        return []


def hash_de(x):
    return hashlib.sha1(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
#  1. o limitador central
# ---------------------------------------------------------------------------

PRIORIDADE = {  # a ordem pedida: estado do jogador, batalhas e rondas, bounty, ranking, precos e equipamento, historico
    "user.getUserById": 1, "search.searchUsers": 1, "battle.getBattles": 2, "round.getById": 2, "battle.getById": 2,
    "upgrade.getUpgradeByTypeAndEntity": 3, "battleOrder.getByBattle": 3, "mu.getById": 3,
    "battleRanking.getRanking": 4,
    "tradingOrder.getTopOrders": 5, "itemTrading.getPrices": 5, "gameStat.getEquipmentAvgByCode": 5,
    "inventory.fetchCurrentEquipment": 5,
    "battleLootSummary.getByBattleAndUser": 6, "gameConfig.getGameConfig": 6, "country.getAllCountries": 6,
}


_FIO = threading.local()   # V3.2: uma fonte pode pedir uma prioridade propria (a aba dos drops: a ultima)


class Limitador:
    """Janela deslizante de 60 s, por prioridade, com pausa depois de um 429."""

    def __init__(self, por_min: int, espaco: float | None = None):
        self.por_min = por_min
        # espacamento minimo entre pedidos: sem ele, os 45 saiam no mesmo segundo e depois havia um
        # minuto parado — um ritmo aos solavancos e o risco de limites por segundo do lado do jogo
        self.espaco = (60.0 / por_min) * 0.5 if espaco is None else espaco
        self.ultimo = 0.0
        self.feitos: deque = deque()
        self.cv = threading.Condition()
        self.fila: list = []
        self.seq = itertools.count()
        self.pausa_ate = 0.0
        self.recusas = 0
        self.falhas = 0
        self.por_endpoint: dict[str, int] = {}
        self.minutos: deque = deque(maxlen=120)      # (minuto, pedidos) para o grafico de uso

    def _limpar(self, t):
        while self.feitos and self.feitos[0] < t - 60.0:
            self.feitos.popleft()

    def pedir(self, endpoint: str, prio: int | None = None):
        prio = PRIORIDADE.get(endpoint, 6) if prio is None else prio
        with self.cv:
            bilhete = (prio, next(self.seq))
            heapq.heappush(self.fila, bilhete)
            while True:
                t = time.time()
                self._limpar(t)
                livre = (len(self.feitos) < self.por_min and t >= self.pausa_ate
                         and t - self.ultimo >= self.espaco)
                if livre and self.fila[0] == bilhete:
                    heapq.heappop(self.fila)
                    self.feitos.append(t)
                    self.ultimo = t
                    self.por_endpoint[endpoint] = self.por_endpoint.get(endpoint, 0) + 1
                    self.cv.notify_all()
                    return
                if t < self.pausa_ate:
                    espera = self.pausa_ate - t
                elif len(self.feitos) >= self.por_min:
                    espera = self.feitos[0] + 60.0 - t
                else:
                    espera = max(0.02, self.ultimo + self.espaco - t)
                self.cv.wait(timeout=max(0.02, min(1.0, espera)))

    def pausar(self, s: float):
        with self.cv:
            self.pausa_ate = max(self.pausa_ate, time.time() + s)
            self.recusas += 1
            self.cv.notify_all()

    def no_ultimo_minuto(self):
        with self.cv:
            self._limpar(time.time())
            return len(self.feitos)

    def estado(self):
        t = time.time()
        return {"limite_min": self.por_min, "ultimo_minuto": self.no_ultimo_minuto(),
                "em_fila": len(self.fila), "pausa_s": max(0.0, self.pausa_ate - t), "recusas_429": self.recusas,
                "falhas": self.falhas, "por_endpoint": dict(self.por_endpoint),
                "total": sum(self.por_endpoint.values())}


LIM: Limitador = None  # criado no main
_urlopen_original = urllib.request.urlopen


def _urlopen_limitado(req, *a, **k):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    endpoint = url.split("/trpc/")[-1].split("?")[0] if "/trpc/" in url else "outro"
    LIM.pedir(endpoint, getattr(_FIO, "prio", None))
    try:
        return _urlopen_original(req, *a, **k)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                s = float(e.headers.get("ratelimit-reset") or e.headers.get("Retry-After") or 30)
            except (TypeError, ValueError):
                s = 30.0
            LIM.pausar(min(max(s, 5.0), 120.0))
        else:
            LIM.falhas += 1
        raise
    except urllib.error.URLError:
        LIM.falhas += 1
        raise


# ---------------------------------------------------------------------------
#  2. as fontes
# ---------------------------------------------------------------------------

class Fonte:
    def __init__(self, nome, cadencia, prio, ler, rotulo, endpoint):
        self.nome, self.cadencia, self.prio, self.ler = nome, cadencia, prio, ler
        self.rotulo, self.endpoint = rotulo, endpoint
        self.dados = None
        self.lido_em = None          # inicio da leitura aceite
        self.fim_leitura = None
        self.versao = 0
        self.hash = None
        self.estado = "por ler"
        self.erro = None
        self.a_ler = False
        self.proxima = 0.0
        self.falhas_seguidas = 0
        self.descartadas = 0         # leituras que chegaram fora de ordem

    def resumo(self):
        t = time.time()
        return {"nome": self.nome, "rotulo": self.rotulo, "endpoint": self.endpoint, "cadencia_s": self.cadencia,
                "lido_em": agora_iso(self.lido_em) if self.lido_em else None,
                "idade_s": (t - self.lido_em) if self.lido_em else None,
                "estado": "a ler" if self.a_ler else self.estado, "erro": self.erro, "versao": self.versao,
                "proxima_s": max(0.0, self.proxima - t), "descartadas": self.descartadas,
                "desactualizada": bool(self.lido_em and t - self.lido_em > 2 * self.cadencia)}


# V3.3: "dar preferencia onde ha ordem mas nao ao ponto de perder oportunidades boas de grande valor".
# Uma preferencia do jogador, nao uma regra do jogo: a pagina deixa escolher (0, 5, 10 ou 20% do lucro).
PREF_ORDENS = 0.10
PREF_ORDENS_OPCOES = (0.0, 0.05, 0.10, 0.20)


class Estado:
    def __init__(self):
        self.lock = threading.RLock()
        self.fontes: dict[str, Fonte] = {}
        self.evento = threading.Event()
        self.ultimo_cliente = 0.0
        self.manuais: list[dict] = []
        self.versao_manuais = 0
        self.rondas_escolhidas: list[dict] = []   # da triagem
        self.comida = None                         # a comida escolhida pelo jogador (None: a de mais lucro)
        self.pref_ordens = PREF_ORDENS             # V3.3: quanto do lucro se aceita perder para bater onde ha ordem
        self.contratos = None                      # V3.3c: o contrato da tua MU medido por batalha (None: por ler)
        self.leituras_ronda: dict[str, dict] = {}  # titulo -> {r, contextos, lido_em, versao}

    def aceitar(self, fonte: Fonte, dados, inicio: float, conteudo=None):
        """So entra se for mais recente; so conta como mudanca se o conteudo mudou."""
        with self.lock:
            if fonte.lido_em and inicio <= fonte.lido_em:
                fonte.descartadas += 1
                return False
            h = hash_de(conteudo if conteudo is not None else dados)
            fonte.dados = dados
            fonte.lido_em = inicio
            fonte.fim_leitura = time.time()
            fonte.estado = "ok"
            fonte.erro = None
            fonte.falhas_seguidas = 0
            mudou = h != fonte.hash
            if mudou:
                fonte.hash = h
                fonte.versao += 1
            return mudou


E = Estado()


def _dados(nome):
    f = E.fontes.get(nome)
    return f.dados if f else None


def ler_jogador():
    u = api.utilizador(UID)
    v, f = M3.vida_fome(u)
    atk = (u.get("skills") or {}).get("attack") or {}
    # o buff e a ressaca da pilula entram no conteudo: quando a ressaca acaba (debuffsPercent 60 -> 0)
    # isso tem de contar como mudanca, senao o dano ficava a 0,4x depois dela
    return u, {"vida": v, "fome": f, "buff": (u.get("buffs") or {}).get("buffEndAt"),
               "buffs": u.get("buffs") or {}, "ataque_pct": [atk.get("buffsPercent"), atk.get("debuffsPercent")],
               "skills": {k: (s or {}).get("total") for k, s in (u.get("skills") or {}).items()
                          if k not in ("health", "hunger")},
               "vida_max": v["max"], "fome_max": f["max"]}


def ler_batalhas():
    # NAO `api._batalhas`: esse usa `safe()` e, numa falha de rede, devolve [] — e o prototipo aceitava
    # "zero batalhas" como verdade, esvaziava a escolha das rondas e ficava parado (20/11, 11:24-12:17).
    # Uma falha tem de ser uma falha: `api.call` levanta, e a fonte fica em erro com a leitura anterior.
    bs, cursor = [], None
    for _ in range(3):
        p = {"isActive": True, "limit": 50, **({"cursor": cursor} if cursor else {})}
        d = api.call("battle.getBattles", p)
        itens = d.get("items") if isinstance(d, dict) else d
        if not isinstance(itens, list):
            raise api.ErroAPI("battle.getBattles: resposta sem lista de batalhas")
        bs.extend(itens)
        cursor = d.get("nextCursor") if isinstance(d, dict) else None
        if not cursor:
            break
    conteudo = {b["_id"]: {"r": (b.get("currentRound") or {}).get("_id") if isinstance(b.get("currentRound"), dict)
                           else b.get("currentRound"),
                           "a": [(b.get("attacker") or {}).get(k) for k in ("moneyPer1kDamages", "moneyPool")],
                           "d": [(b.get("defender") or {}).get(k) for k in ("moneyPer1kDamages", "moneyPool")]}
                for b in bs}
    return bs, conteudo


def ler_equipamento():
    e = api.equipamento_vestido(UID)
    if e is None:
        raise api.ErroAPI("inventory.fetchCurrentEquipment não respondeu")
    return e, {s: (v.get("code"), v.get("state"), v.get("skills")) for s, v in e.items() if isinstance(v, dict)}


def ler_livro():
    ref = api.precos_referencia()
    livro = mercado.instantaneo(M3.BENS, ref)
    conteudo = {c: (l.get("best_ask"), l.get("best_bid")) for c, l in livro.items()}
    return {"ref": ref, "livro": livro}, conteudo


def ler_precos_eq():
    cfg = _dados("config") or api.game_config()
    itens = cfg.get("items") or {}
    codigos = sorted(c for c, it in itens.items() if it.get("type") in ("equipment", "weapon"))
    p = {c: v for c, v in craft.precos_equipamento(codigos).items() if v}
    return p, p


def ler_resumos():
    u = _dados("jogador")
    pais = (u or {}).get("country")
    r = B.resumos_do_jogador(UID, pais)
    return r, [(x["batalha"], (x["resumo"] or {}).get("hits"), (x["resumo"] or {}).get("case1Count")) for x in r]


def ler_dano_jogador():
    """Resumo oficial do dano, golpes e bounty do jogador em cada batalha activa."""
    ids = sorted({x["batalha"] for x in E.rondas_escolhidas if x.get("batalha")})
    dados, assinatura = {}, {}
    for bid in ids:
        s = api.resumo_loot(bid, UID)
        if s is None:
            raise RuntimeError(f"battleLootSummary indisponivel para {bid}")
        linha = {
            "tem_registo": bool(s),
            "golpes": float((s or {}).get("hits") or 0),
            "dano": float((s or {}).get("totalDmg") or 0),
            "bounty": float((s or {}).get("totalMoneyFromBounty") or 0),
            "contrato": float((s or {}).get("totalMoneyFromContract") or 0),
            "atualizado_em": (s or {}).get("updatedAt") or (s or {}).get("createdAt"),
        }
        dados[bid] = linha
        assinatura[bid] = linha.copy()
        _guardar_resumo(bid, linha)
    E.contratos = contratos_medidos()
    return dados, assinatura


# ---------------------------------------------------------------------------
#  V3.3c: o contrato de mercenario da tua MU, medido no que recebeste
# ---------------------------------------------------------------------------
#
# Os contratos ganhos nao estao na API publica (`mercenaryContract.getContracts` recusa-a; os leiloes
# publicos so mostram os abertos). O que se ve e o teu resumo da batalha: o dano e o dinheiro do contrato
# acumulados. A taxa sai de duas leituras: Δcontrato / Δdano no intervalo MAIS RECENTE em que bateste.
# Se nesse intervalo o contrato nao subiu, o contrato ja nao paga (acabou o orcamento ou o prazo): 0.
# E um limite de baixo se o contrato comecou a meio do intervalo — diz-se "medido", com as horas.

_ULTIMO_RESUMO: dict = {}      # (jogador, batalha) -> a ultima linha guardada


def _guardar_resumo(bid, linha):
    """Guarda a leitura do resumo da batalha quando muda (so isso: o ficheiro nao cresce sem bater)."""
    if not linha.get("tem_registo"):
        return
    k = (UID, bid)
    if k not in _ULTIMO_RESUMO:
        ant = [r for r in ler_hist("resumos_batalha") if r.get("batalha") == bid]
        _ULTIMO_RESUMO[k] = ant[-1] if ant else None
    u = _ULTIMO_RESUMO[k]
    reg = {"t": linha.get("atualizado_em") or agora_iso(), "batalha": bid, "dano": linha["dano"],
           "contrato": linha["contrato"], "bounty": linha["bounty"], "golpes": linha["golpes"]}
    if u and all(u.get(c) == reg[c] for c in ("dano", "contrato", "bounty")):
        return
    hist("resumos_batalha", reg)
    _ULTIMO_RESUMO[k] = reg


def contratos_medidos(registos=None):
    """
    {batalha: {"por_1k", "ativo", "desde", "ate", "dano", "pago", "total"}} para as batalhas em que ja
    recebeste dinheiro de contrato. `por_1k` e o do intervalo mais recente com dano (0 se nao pagou).
    """
    regs = registos if registos is not None else ler_hist("resumos_batalha")
    por = {}
    for r in regs:
        por.setdefault(r.get("batalha"), []).append(r)
    fora = {}
    for bid, rs in por.items():
        rs.sort(key=lambda r: str(r.get("t")))
        if not rs or float(rs[-1].get("contrato") or 0) <= 0:
            continue
        x = None
        for a, b in zip(rs, rs[1:]):
            dd = float(b.get("dano") or 0) - float(a.get("dano") or 0)
            if dd > 0:
                x = (a, b, dd, float(b.get("contrato") or 0) - float(a.get("contrato") or 0))
        if x is None:
            continue
        a, b, dd, dc = x
        fora[bid] = {"por_1k": max(dc, 0.0) / dd * 1000.0, "ativo": dc > 0, "desde": a["t"], "ate": b["t"],
                     "dano": dd, "pago": dc, "total": float(rs[-1].get("contrato") or 0)}
    return fora


def lado_do_contrato(lados):
    """O lado a que o contrato medido se aplica: o da ordem da tua MU; sem ela, o unico em que podes bater."""
    mu = [l for l in lados if "ordem_mu" in (l.get("ordem") or [])]
    if len(mu) == 1:
        return mu[0], "o lado com a ordem da tua MU"
    if not mu and len(lados) == 1:
        return lados[0], "o único lado em que podes bater nesta batalha"
    return None, "a batalha tem mais de um lado possível e o resumo não diz em qual"


def ler_config():
    api._cache.pop("gameConfig.getGameConfig:{}", None)
    c = api.game_config()
    return c, {"loot": c.get("loot"), "battle": c.get("battle"), "n": len(c.get("items") or {})}


def ler_paises():
    p = api.paises()
    return p, len(p)


def ler_previsoes():
    v = O.validar_previsoes(maximo=6)
    return v, [x.get("ronda") for x in v]


# ---------------------------------------------------------------------------
#  V3.2 — a aba dos drops: o que o scripts/warera_drops.py mostra, aqui
# ---------------------------------------------------------------------------
#
# As mesmas funcoes do script (carregado como modulo, sem o alterar: `optimizador.drops()`):
# rounds_newest_first, find_cutoffs, estimate_round_seconds, fmt_time, battle_label. A leitura do
# ranking e a do script (paginas de 100 ate ao primeiro jogador sem item), mas pelo `api.call` do
# projecto, que passa pelo limitador central — e com a prioridade mais baixa de todas (7): so usa os
# pedidos que sobram ao assistente. Todas as batalhas activas, poucas de cada vez, as mais antigas
# primeiro; as rondas acabadas nunca se voltam a pedir.

DROPS = {"linhas": {}, "fila": [], "volta": 0, "inicio_volta": None, "versao": 0, "nomes": None,
         "nomes_em": 0.0, "acabadas": {}, "pedidos": 0}
DROPS_POR_LEITURA = 4          # batalhas por leitura da fonte
DROPS_MAX_S = 25.0


def _drops_ranking(scope, vivo):
    """`fetch_merged` do script: o merged, paginas de 100, ate ao fim do pool."""
    D_ = O.drops()
    chave = ("r:" + scope["roundId"]) if "roundId" in scope else ("b:" + scope["battleId"])
    if not vivo and chave in DROPS["acabadas"]:
        return DROPS["acabadas"][chave]
    raw, cursor = [], None
    for _ in range(500):
        p = dict(scope, dataType="damage", type="user", side="merged", limit=100)
        if cursor:
            p["cursor"] = cursor
        d = api.call("battleRanking.getRanking", p)
        DROPS["pedidos"] += 1
        if isinstance(d, list):
            raw.extend(d)
            break
        page = d.get("items") or d.get("rankings") or []
        raw.extend(page)
        cursor = d.get("nextCursor")
        if not cursor or D_.page_is_enough(page):
            break
    entries = [{"rank": r.get("rank"), "value": r.get("value", 0), "user": r.get("user"),
                "item": (r.get("lootItem") or {}).get("code")} for r in raw]
    if not vivo and any(e["item"] for e in entries):
        if len(DROPS["acabadas"]) > 400:
            DROPS["acabadas"].clear()
        DROPS["acabadas"][chave] = entries
    return entries


def _drops_cortes(entries):
    D_ = O.drops()
    if not any(e.get("item") for e in entries):
        return {}, None
    baixo, parou = D_.find_cutoffs(entries)
    return ({str(k): {"valor": e["value"], "rank": e.get("rank"), "item": e.get("item")} for k, e in baixo.items()},
            ({"rank": parou.get("rank"), "valor": parou.get("value"), "item": parou.get("item")} if parou else None))


def _drops_tu(entries):
    eu = next((e for e in entries if (e.get("user") if isinstance(e.get("user"), str)
                                      else (e.get("user") or {}).get("_id")) == UID), None)
    return {"dano": eu["value"], "rank": eu.get("rank"), "item": eu.get("item")} if eu else None


def _drops_batalha(b, nomes, cfg_batalha):
    """`scan_battle` do script, sem o detalhe: a ronda mais recente com drops e a batalha inteira."""
    D_ = O.drops()
    if b.get("type") == "tournament":
        eq = [str((b.get(s_) or {}).get("tournamentTeam") or "?")[-4:] for s_ in ("attacker", "defender")]
        label = f"Torneio · fase {b.get('tournamentRoundNumber') or '?'} · equipa {eq[0]} vs {eq[1]}"
    else:
        label = D_.battle_label(b, nomes)
    r = {"batalha": b["_id"], "label": label, "tipo": b.get("type"), "ronda": "-", "ao_vivo": False,
         "acaba": "-", "segundos": None, "ronda_tiers": {}, "batalha_tiers": {}, "tu_ronda": None, "tu_batalha": None}
    activa = bool(b.get("isActive"))
    rondas = D_.rounds_newest_first(b)
    if rondas:
        r["ronda"] = str(rondas[0][0])
    if activa:
        rd = b.get("currentRound") if isinstance(b.get("currentRound"), dict) else None
        if rd is None and b.get("currentRound"):
            try:
                rd = api.call("round.getById", {"roundId": b["currentRound"]})
                DROPS["pedidos"] += 1
            except Exception:  # noqa: BLE001
                rd = None
        if rd:
            seg = D_.estimate_round_seconds(rd, cfg_batalha, datetime.now(timezone.utc))
            r["segundos"], r["acaba"] = seg, D_.fmt_time(seg)
    else:
        r["acaba"] = "ended"
    for numero, rid, vivo in rondas:
        entries = _drops_ranking({"roundId": rid}, vivo)
        if any(e.get("item") for e in entries):
            r["ronda"], r["ao_vivo"] = str(numero), bool(vivo)
            r["ronda_tiers"], r["ronda_fim_pool"] = _drops_cortes(entries)
            r["tu_ronda"] = _drops_tu(entries)
            break
    entries = _drops_ranking({"battleId": b["_id"]}, activa)
    r["batalha_tiers"], r["batalha_fim_pool"] = _drops_cortes(entries)
    r["tu_batalha"] = _drops_tu(entries)
    return r


def ler_drops():
    _FIO.prio = 7
    try:
        D_ = O.drops()
        bs = [b for b in (_dados("batalhas") or []) if b.get("_id")]
        por_id = {b["_id"]: b for b in bs}
        pv = E.fontes["paises"].versao if "paises" in E.fontes else 0
        # os nomes refazem-se quando os paises chegam (no arranque podiam ainda nao estar lidos)
        if DROPS["nomes"] is None or DROPS.get("nomes_v") != pv or time.time() - DROPS["nomes_em"] > 3600:
            DROPS["nomes_v"] = pv
            nomes = {c["_id"]: c.get("name") or c.get("code") or c["_id"] for c in (_dados("paises") or [])
                     if isinstance(c, dict) and c.get("_id")}
            try:
                reg = api.call("region.getRegionsObject")
                DROPS["pedidos"] += 1
                for rid_, x in (reg or {}).items():
                    nomes[rid_] = (x or {}).get("name") or rid_
            except Exception:  # noqa: BLE001
                pass
            DROPS["nomes"], DROPS["nomes_em"] = nomes, time.time()
        for k in [k for k in DROPS["linhas"] if k not in por_id]:
            del DROPS["linhas"][k]
        if not DROPS["fila"] and por_id:
            nunca = [b for b in por_id if b not in DROPS["linhas"]]
            velhas = sorted((b for b in por_id if b in DROPS["linhas"]), key=lambda b: DROPS["linhas"][b]["lido_em"])
            DROPS["fila"] = nunca + velhas
            DROPS["volta"] += 1
            DROPS["inicio_volta"] = agora_iso()
        # um nome que mudou (os paises chegaram depois da primeira leitura) corrige-se sem reler o ranking
        if por_id:
            for bid, l in DROPS["linhas"].items():
                b = por_id.get(bid)
                if b and b.get("type") != "tournament":
                    l["label"] = D_.battle_label(b, DROPS["nomes"])
        cfg_b = (_dados("config") or {}).get("battle") or {}
        t0, feitas = time.time(), 0
        while DROPS["fila"] and feitas < DROPS_POR_LEITURA and time.time() - t0 < DROPS_MAX_S:
            bid = DROPS["fila"].pop(0)
            b = por_id.get(bid)
            if not b:
                continue
            p0 = DROPS["pedidos"]
            linha = _drops_batalha(b, DROPS["nomes"], cfg_b)
            linha["lido_em"] = agora_iso()
            linha["pedidos"] = DROPS["pedidos"] - p0
            DROPS["linhas"][bid] = linha
            feitas += 1
        DROPS["versao"] += 1
        resumo = {"lidas": len(DROPS["linhas"]), "total": len(por_id), "volta": DROPS["volta"]}
        return resumo, {**resumo, "v": DROPS["versao"]}
    finally:
        _FIO.prio = None


def drops_json():
    ls = list(DROPS["linhas"].values())
    return {"versao": DROPS["versao"], "linhas": ls, "lidas": len(ls),
            "total": len([b for b in (_dados("batalhas") or []) if b.get("_id")]), "volta": DROPS["volta"],
            "inicio_volta": DROPS["inicio_volta"], "na_fila": len(DROPS["fila"]), "pedidos": DROPS["pedidos"],
            "nomes_tiers": {str(k): v for k, v in O.drops().TIER_NAMES.items()}}


# ---------------------------------------------------------------------------
#  3. o motor: base, triagem, rondas, plano exaustivo, publicacao
# ---------------------------------------------------------------------------

class Motor:
    def __init__(self, processos: int):
        self.processos = processos
        self.pool = None
        self.base = None
        self.base_chave = None
        self.versao_base = 0
        self.lados = {}                 # batalha -> lados (do ultimo batalhas)
        self.onde = None
        self.res: dict[str, dict] = {}  # titulo -> {"chave": (vb, vc, vl), "por": {conjunto: plano}}
        self.trabalho = None            # o calculo em curso
        self.publicado = None
        self.anterior = None
        self.versao_pub = 0
        self.versao_recursos = 0
        self.estado = "à espera de dados"
        self.ultimo_calculo = None
        self.cancelados = 0
        self.erros = []
        self.lock = threading.RLock()
        # V3.1 / V3.2
        self.livro_usado = None         # versao do livro de ordens com que a base actual foi feita
        self.precos_desde = None        # agrupar mudancas de preco proximas
        self.precos_agrupadas = 0
        self.materialidade = None       # o relatorio da ultima mudanca de precos
        self.historico_mat = []
        self.custos_versao = 0
        self.divisao = None             # V3.2: a divisao pelas rondas, por metade de dano e para todos os h
        self.divisao_chave = None
        self.tempos = {}

    # -- a base ------------------------------------------------------------
    def chave_base(self):
        # V3.1: os precos do livro de ordens ja NAO estao aqui (passam pela materialidade, sem pesquisa
        # completa) e a pilula tambem nao (passa pelas janelas). Ficam as coisas que mudam os conjuntos.
        j = E.fontes["jogador"]
        partes = [E.fontes[n].versao for n in ("config", "equipamento", "precos_eq", "resumos")]
        ju = _dados("jogador") or {}
        sk = hash_de({k: (s or {}).get("total") for k, s in (ju.get("skills") or {}).items()
                      if k not in ("health", "hunger")})
        return tuple(partes) + (sk, E.versao_manuais, j.versao > 0)

    def pronta_para_base(self):
        return all(E.fontes[n].dados is not None for n in ("config", "jogador", "equipamento", "precos_eq",
                                                           "livro", "resumos", "paises"))

    def refazer_base(self):
        ch = self.chave_base()
        if ch == self.base_chave:
            return False
        t = time.time()
        lv = _dados("livro")
        self.base = M3.preparar_base(_dados("jogador"), _dados("equipamento"), _dados("config"), _dados("precos_eq"),
                                     lv["ref"], lv["livro"], _dados("resumos"), E.manuais)
        self.base["P"] = BR.preparar(self.base["grupos"])
        self.base["hmax"] = hmax_cheio(self.base)
        self.base_chave = ch
        self.versao_base += 1
        self.base["segundos"] = time.time() - t
        self.livro_usado = E.fontes["livro"].versao
        self.precos_desde = None
        self.divisao = self.divisao_chave = None
        return True

    # -- os lados (bonus de cada lado) e a triagem ---------------------------
    def atualizar_onde(self):
        u, bs, cfg = _dados("jogador"), _dados("batalhas"), _dados("config")
        if not (u and bs and cfg and self.base):
            return
        nomes = {c["_id"]: c.get("name") or c.get("code") for c in (_dados("paises") or [])}
        onde = B.lados_onde_bater(bs, u, nomes)
        # V3.2: as batalhas de torneio (equipas de torneio, sem paises) ficam fora do plano. A 23/11 eram
        # 32 de 50 activas; o teu MU nao estava em nenhuma equipa e nao tinhas dano em nenhuma. Nao esta
        # verificado se quem nao e da equipa pode bater la — por isso nao entram (vem na aba Drops).
        self.torneios_fora = len({l["batalha"] for l in onde if l.get("tipo") == "tournament"})
        onde = [l for l in onde if l.get("tipo") != "tournament"]
        # V3.2: paises que nao vem em getAllCountries ficavam com o nome None
        for l in onde:
            for c in ("pais_nome", "contra", "pais"):
                if c in l and l[c] is None:
                    l[c] = "?"
        O._contextos_de_lados(onde, u, cfg, {x["batalha"] for x in E.rondas_escolhidas})
        self.onde = onde

    def lados_de(self, bid):
        fora = O._lados_da_batalha(self.onde or [], bid)
        # V3.3: as ordens do teu pais e da tua MU em cada lado (as que dao bonus: `bounty.contexto_de_lado`)
        for l in fora:
            ch = {c.get("chave") for c in l.get("componentes") or [] if (c.get("pct") or 0) > 0}
            l["ordem"] = [k for k in ("ordem_pais", "ordem_mu") if k in ch]
        # V3.3c: o contrato medido da tua MU nesta batalha, no lado a que se aplica
        c = (E.contratos or {}).get(bid)
        if c:
            l, porque = lado_do_contrato(fora)
            for x in fora:
                x["contrato"] = {**c, "aplicado": x is l, "porque": porque}
                x["contrato_1k"] = c["por_1k"] if (x is l and c["ativo"]) else 0.0
        return fora


MOTOR: Motor = None


def ler_triagem():
    """As rondas activas onde podes bater, ordenadas pelo que rende quem entra no fundo (35 pedidos)."""
    m = MOTOR
    if not m.base:
        raise RuntimeError("à espera da base do motor")
    m.atualizar_onde()
    cfg = _dados("config")
    por_r = float((cfg.get("loot") or {}).get("damagePerLootItem") or 200000)
    tri = O.triagem(_dados("batalhas"), m.onde, m.base["comp"], m.base["val"], por_r)
    # V3.2: batalhas sem nome de pais davam o titulo "None / None" (e varias com o mesmo titulo misturavam-se)
    vistos = set()
    for x in tri:
        t = str(x.get("titulo") or "")
        if "None" in t or "?" in t or not t or t in vistos:
            x["titulo"] = f"{t.replace('None', '?') or '?'} · {str(x.get('batalha') or x.get('ronda') or '')[-5:]}"
        vistos.add(x["titulo"])
    esc = tri[:RONDAS]
    with E.lock:
        E.rondas_escolhidas = esc
    for x in esc:
        nome = "ronda:" + x["titulo"]
        if nome not in E.fontes:
            E.fontes[nome] = Fonte(nome, 300, 4, None, "Ranking " + x["titulo"].replace(" / ", " × "),
                                   "battleRanking.getRanking")
            E.fontes[nome].ler = (lambda t=x["titulo"]: ler_ronda(t))
    # as que sairam da lista deixam de se ler
    vivos = {"ronda:" + x["titulo"] for x in esc}
    for nome in [n for n in E.fontes if n.startswith("ronda:") and n not in vivos]:
        del E.fontes[nome]
    return [{k: v for k, v in x.items() if k != "_b"} for x in tri], [(x["ronda"], x["titulo"]) for x in esc]


VALIDACAO: dict[str, dict] = {}       # "ronda:<id>" / "batalha:<id>" -> estado da verificacao da regra
CAMINHO_VALIDACAO = HIST / "validacao_ranking.json"


def _guardar_validacao():
    try:
        CAMINHO_VALIDACAO.write_text(json.dumps(VALIDACAO, ensure_ascii=False, default=str), encoding="utf-8")
    except OSError:
        pass


def registar_regra(titulo, pool, ident, x, t0, fim_s):
    """
    O estado de uma leitura viva da regra merged. Uma contagem fora da folga sem
    inversoes fica por confirmar no fim da ronda; com inversoes e uma
    discrepancia (e tambem se confere no fim). Nada se esconde: a leitura viva
    fica guardada ao lado do resultado final.
    """
    est = M3.estado_regra(x, vivo=True)
    k = f"{pool}:{ident}"
    e = VALIDACAO.get(k) or {"chave": k, "titulo": titulo, "pool": pool, "id": ident, "pendente": False,
                             "estado": None, "final": None, "leituras": 0, "fora_da_folga": 0}
    e["leituras"] += 1
    e["t_viva"] = agora_iso(t0)
    e["viva"] = {"estado": est, "itens": x.get("itens"), "esperados": x.get("itens_esperados"), "folga": x.get("folga"),
                 "inversoes": x.get("inversoes"), "abaixo": x.get("itens_abaixo_do_corte")}
    if not e.get("final"):
        if est != "confirmado":
            e["fora_da_folga"] += 1
            e["pendente"] = True
            # a pior leitura viva manda: uma discrepancia nao passa a "por confirmar"
            if e.get("estado") != "discrepância confirmada":
                e["estado"] = est
            e["fim_estimado"] = t0 + float(fim_s or 0)
        elif not e["pendente"]:
            e["estado"] = "confirmado"
    if est != "confirmado":
        hist("regra", {"t": agora_iso(t0), "chave": k, "titulo": titulo, "estado": est, **e["viva"]})
    VALIDACAO[k] = e
    _guardar_validacao()


def ler_confirmacoes():
    """
    As verificacoes pendentes cuja ronda (ou batalha) ja acabou: le o ranking
    FINAL e conclui — confirmado (a diferenca viva era dano a entrar durante a
    leitura) ou discrepancia confirmada. Uma por vez, prioridade baixa.
    """
    cfg = _dados("config") or {}
    loot = cfg.get("loot") or {}
    feitas = []
    for e in sorted([e for e in VALIDACAO.values() if e.get("pendente")], key=lambda e: e.get("fim_estimado") or 0):
        if (e.get("fim_estimado") or 0) > time.time() - 30:
            continue
        if e["pool"] == "ronda":
            info = api.call("round.getById", {"roundId": e["id"]})
            escopo, por = {"roundId": e["id"]}, float(loot.get("damagePerLootItem") or 200000)
        else:
            info = api.call("battle.getById", {"battleId": e["id"]})
            escopo, por = {"battleId": e["id"]}, float(loot.get("battleLootDamagePerLootItem") or 500000)
        if (info or {}).get("isActive") is not False:
            e["fim_estimado"] = time.time() + 300          # ainda nao acabou: volta a ver daqui a 5 min
            continue
        rows = O.ranking_merged(escopo, vivo=False)
        if not rows:
            e["estado"], e["pendente"] = "dados indisponíveis", False
        else:
            v = O.verificar_regra(rows, por, vivo=False)
            e["final"] = {"t": agora_iso(), "itens": v["itens"], "esperados": v["itens_esperados"],
                          "inversoes": v["inversoes"], "abaixo": v["itens_abaixo_do_corte"], "confere": v["confere"],
                          "jogadores": v["jogadores"]}
            e["estado"] = "confirmado" if v["confere"] else "discrepância confirmada"
            e["explicacao"] = ("no fim confere: a diferença viva era dano a entrar enquanto as páginas eram lidas"
                               if v["confere"] else "o ranking final também não confere com a regra")
            e["pendente"] = False
        hist("regra", {"t": agora_iso(), "chave": e["chave"], "titulo": e["titulo"], "estado": e["estado"],
                       "final": e.get("final")})
        feitas.append(e["chave"])
        break
    _guardar_validacao()
    return {"feitas": feitas, "pendentes": sum(1 for e in VALIDACAO.values() if e.get("pendente"))}, feitas


def ler_ronda(titulo):
    """Uma ronda: ranking merged, ritmo, cenarios — e o teu dano de agora (`ronda_eu.analisar_ronda_eu`)."""
    m = MOTOR
    x = next((r for r in E.rondas_escolhidas if r["titulo"] == titulo), None)
    if not x or not m.base:
        raise RuntimeError("ronda já não está na lista")
    b = next((b for b in (_dados("batalhas") or []) if b["_id"] == x["batalha"]), x["_b"])
    base = m.base
    vida, fome = M3.vida_fome(_dados("jogador"))
    gad = M3._gad_de(vida["agora"], fome["agora"])
    t0 = time.time()
    # a copia do `analisar_ronda` que conta com o dano que ja fizeste (combate/ronda.py)
    r = RE.analisar_ronda_eu(b, m.onde, _dados("config"), base["comp"], base["val"], base["tens_motor"],
                             base["caso_golpe"], gad, base["buff"], t0, UID, titulo, None)
    ctx = r.pop("_contextos", None)
    if not r.get("erro"):
        try:
            O.guardar_previsao({**r, "contextos": r.get("contextos") or {}}, t0)
        except Exception:  # noqa: BLE001
            pass
    e = r.get("estado") or {}
    for pool, x in (r.get("regra") or {}).items():
        registar_regra(titulo, pool, r.get("ronda") if pool == "ronda" else r.get("batalha"), x, t0,
                       e.get("fim_segundos"))
    hist("rondas", {"t": agora_iso(t0), "ronda": r.get("ronda"), "titulo": titulo, "jogadores": e.get("jogadores"),
                    "dano": e.get("dano"), "itens": e.get("itens"), "fim_s": e.get("fim_segundos"),
                    "regra": r.get("regra"), "backtest_erro_pct": (r.get("backtest") or {}).get("erro_total_pct")})
    # a cadencia desta ronda: mais perto do fim, mais vezes (3 a 10 min)
    fim = e.get("fim_segundos") or 3600
    fonte = E.fontes.get("ronda:" + titulo)
    if fonte:
        fonte.cadencia = max(180, min(600, fim / 6))
    return {"r": r, "contextos": ctx, "fim_s": fim, "lido_em": t0}, {
        "ronda": r.get("ronda"), "jogadores": e.get("jogadores"), "dano": e.get("dano"),
        "cortes": {n: c.get("ronda") for n, c in (r.get("contextos") or {}).items()}}


def criar_fontes():
    F = [
        ("jogador", 60, 1, ler_jogador, "Vida, fome, pílula", "user.getUserById"),
        ("batalhas", 120, 2, ler_batalhas, "Batalhas, rondas e fundos de bounty", "battle.getBattles"),
        ("equipamento", 120, 5, ler_equipamento, "Equipamento vestido", "inventory.fetchCurrentEquipment"),
        ("livro", 120, 5, ler_livro, "Preços de munição, comida, cases", "tradingOrder.getTopOrders"),
        ("precos_eq", 1800, 5, ler_precos_eq, "Preços de equipamento", "gameStat.getEquipmentAvgByCode"),
        ("resumos", 1800, 6, ler_resumos, "Histórico de cases", "battleLootSummary"),
        ("dano_jogador", 180, 6, ler_dano_jogador, "Dano acumulado do jogador por batalha", "battleLootSummary.getByBattleAndUser"),
        ("config", 3600, 6, ler_config, "Regras do jogo", "gameConfig.getGameConfig"),
        ("paises", 3600, 6, ler_paises, "Países", "country.getAllCountries"),
        ("triagem", 600, 3, ler_triagem, "Escolha das rondas (jogadores por ronda)", "battleRanking.getRanking"),
        ("previsoes", 1800, 6, ler_previsoes, "Validação das previsões", "battleRanking.getRanking"),
        ("confirmacoes", 120, 6, ler_confirmacoes, "Confirmação da regra no fim da ronda", "battleRanking.getRanking"),
        ("drops", 30, 7, ler_drops, "Aba Drops: todas as batalhas (como o warera_drops.py)", "battleRanking.getRanking"),
    ]
    for nome, cad, prio, fn, rot, ep in F:
        E.fontes[nome] = Fonte(nome, cad, prio, fn, rot, ep)


# ---------------------------------------------------------------------------
#  4. o agendador
# ---------------------------------------------------------------------------

def correr_fonte(f: Fonte):
    inicio = time.time()
    try:
        out = f.ler()
        dados, conteudo = out if isinstance(out, tuple) else (out, out)
        mudou = E.aceitar(f, dados, inicio, conteudo)
        if f.nome == "jogador":
            u, resumo = dados, conteudo
            E.fontes["jogador"].dados = u
            if mudou:
                hist("jogador", {"t": agora_iso(inicio), "vida": resumo["vida"]["agora"],
                                 "fome": resumo["fome"]["agora"], "buff": resumo["buff"]})
        if f.nome == "batalhas" and mudou:
            for b in dados:
                for lado in ("attacker", "defender"):
                    s = b.get(lado) or {}
                    if isinstance(s.get("moneyPer1kDamages"), (int, float)) and s.get("moneyPer1kDamages") > 0:
                        hist("fundos", {"t": agora_iso(inicio), "batalha": b["_id"], "lado": lado,
                                        "fundo": s.get("moneyPool"), "taxa": s.get("moneyPer1kDamages")})
        if f.nome == "livro" and mudou:
            for c, l in dados["livro"].items():
                hist("precos", {"t": agora_iso(inicio), "codigo": c, "ask": l.get("best_ask"), "bid": l.get("best_bid")})
        if f.nome == "equipamento" and mudou:
            hist("equipamento", {"t": agora_iso(inicio), "pecas": conteudo})
        if f.nome == "triagem" and mudou:
            progresso = E.fontes.get("dano_jogador")
            if progresso:
                progresso.proxima = 0
        if mudou:
            E.evento.set()
        # a rede voltou: as fontes que falharam por rede nao ficam a espera do backoff (ate 10 min);
        # tentam ja, espacadas, e o limitador continua a mandar no ritmo
        atraso = 0.0
        for g in list(E.fontes.values()):
            if g is not f and g.estado == "erro" and g.erro and ("rede" in g.erro or "getaddrinfo" in g.erro):
                g.proxima = min(g.proxima, time.time() + atraso)
                g.falhas_seguidas = 0
                atraso += 2.0
    except Exception as e:  # noqa: BLE001
        f.estado = "erro"
        f.erro = f"{type(e).__name__}: {str(e)[:200]}"
        f.falhas_seguidas += 1
    finally:
        f.a_ler = False
        # backoff: falhas seguidas afastam a proxima leitura (ate 10 min)
        atraso = f.cadencia if f.falhas_seguidas == 0 else min(600, 15 * 2 ** min(f.falhas_seguidas, 5))
        f.proxima = time.time() + atraso


def agendador():
    # fios a mais de proposito: com 3, leituras lentas de ranking (prioridade 4) ocupavam todos e o
    # jogador e as batalhas (1 e 2) ficavam a espera na fila dos fios — inversao de prioridade. O
    # ritmo de pedidos e o limitador que o garante, nao o numero de fios.
    ex = ThreadPoolExecutor(max_workers=10)
    while True:
        try:
            t = time.time()
            if t - E.ultimo_cliente < SEM_PAGINA_S:
                for f in fontes_devidas(t):
                    f.a_ler = True
                    ex.submit(correr_fonte, f)
            time.sleep(0.5)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            time.sleep(2)


_TICK = {"hora": None}


def fontes_devidas(t):
    """As fontes a ler agora, por prioridade. O agendador (fios) e o browser (um passo de cada vez) usam esta."""
    j = E.fontes.get("jogador")
    # logo depois de cada hora certa (a recuperacao) e do fim da pilula, rele-se o jogador
    hora = datetime.fromtimestamp(t, timezone.utc)
    if j and hora.minute == 0 and hora.second >= 20 and _TICK["hora"] != hora.hour:
        _TICK["hora"] = hora.hour
        j.proxima = 0
    u = _dados("jogador") or {}
    for campo in ("buffEndAt", "debuffEndAt"):      # fim do buff e fim da ressaca
        ba = O._ts((u.get("buffs") or {}).get(campo))
        if j and ba and 5 < t - ba < 35 and j.lido_em and j.lido_em < ba + 5:
            j.proxima = 0
    devidas = sorted((f for f in list(E.fontes.values()) if not f.a_ler and f.proxima <= t),
                     key=lambda f: (f.prio, f.proxima))
    fora = []
    for f in devidas:
        # as fontes do motor so depois da base
        if (f.nome == "triagem" or f.nome.startswith("ronda:")) and not MOTOR.base:
            continue
        if f.nome.startswith("ronda:") and (E.fontes.get("triagem") is None or E.fontes["triagem"].dados is None):
            continue
        fora.append(f)
    return fora


# ---------------------------------------------------------------------------
#  5. o calculo (no fio do motor) e a publicacao
# ---------------------------------------------------------------------------

def chave_ronda(titulo):
    lr = E.leituras_ronda.get(titulo)
    m = MOTOR
    if not lr:
        return None
    lados = m.lados_de(lr["r"]["batalha"])
    vl = hash_de([(l["lado"], l["fator"], l["taxa"], l["fundo"], l["estado"], l["paga"], l.get("ordem"),
                   round(float(l.get("contrato_1k") or 0.0), 6)) for l in lados])
    # V3.1: as janelas da pilula desta ronda (mudam quando a pilula ou a ressaca acabam)
    jr = M3.janelas_da_ronda(janelas_agora(), lr["lido_em"] + lr["fim_s"])
    return (m.versao_base, lr["versao"], vl, hash_de(M3.assinatura_janelas(jr)))


def janelas_agora():
    u = _dados("jogador") or {}
    itens = ((MOTOR.base or {}).get("itens")) or ((_dados("config") or {}).get("items") or {})
    return M3.janelas(u, itens, time.time())


def ciclo_motor():
    while True:
        E.evento.wait(timeout=2.0)
        E.evento.clear()
        m = MOTOR        # o motor de agora: trocar de jogador poe um motor novo
        try:
            passo_motor(m)
        except Exception as e:  # noqa: BLE001
            m.erros.append(f"{agora_iso()} {type(e).__name__}: {str(e)[:300]}")
            m.erros = m.erros[-20:]
            traceback.print_exc()


def passo_motor(m: Motor):
    if not m.pronta_para_base():
        m.estado = "à espera de dados"
        return
    if m.refazer_base():
        m.estado = "a preparar os conjuntos"
        E.evento.set()  # a triagem e as rondas precisam da base
    m.atualizar_onde()
    # as leituras de ronda novas
    for nome, f in list(E.fontes.items()):
        if not nome.startswith("ronda:") or f.dados is None:
            continue
        t = nome[6:]
        cur = E.leituras_ronda.get(t)
        if not cur or cur["versao"] != f.versao or cur["lido_em"] != f.dados["lido_em"]:
            if f.dados.get("contextos"):
                E.leituras_ronda[t] = {**f.dados, "versao": f.versao}
    escolhidas = [x["titulo"] for x in E.rondas_escolhidas]
    if not escolhidas and E.fontes["triagem"].dados is not None:
        m.estado = "sem rondas para analisar"
    for t in list(E.leituras_ronda):
        if t not in escolhidas:
            del E.leituras_ronda[t]
            m.res.pop(t, None)
    sujas = [t for t in escolhidas if t in E.leituras_ronda and (m.res.get(t) or {}).get("chave") != chave_ronda(t)]
    if sujas:
        calcular(m, sujas)
    # precos do livro de ordens -> materialidade (agrupada)
    precos_pendentes(m)
    # publica quando todas as rondas lidas estao feitas com a base actual
    # uma ronda sem leitura nao esta "pronta" (chave_ronda devolve None e a comparacao None == None enganava)
    prontas = [t for t in escolhidas if t in E.leituras_ronda and t in m.res
               and m.res[t].get("chave") == chave_ronda(t)]
    # V3.2: vida e fome mudam o plano (os golpes das barras), mas so pedem outra leitura da divisao
    mudou = (not m.publicado or m.publicado["_chaves"] != {t: m.res[t]["chave"] for t in prontas}
             or m.publicado.get("_custos") != m.custos_versao
             or m.publicado.get("_versao_jogador") != E.fontes["jogador"].versao
             or m.publicado.get("_versao_base") != m.versao_base
             or m.publicado.get("_comida") != E.comida
             or m.publicado.get("_pref_ordens") != E.pref_ordens)
    if prontas and mudou and all(t in prontas for t in escolhidas if t in E.leituras_ronda):
        publicar(m, prontas)
    if m.publicado:
        atualizar_progresso_jogador(m)


# ---------------------------------------------------------------------------
#  V3.1 — as janelas da pilula e a materialidade dos precos
# ---------------------------------------------------------------------------

AGRUPAR_PRECOS_S = 20.0       # espera que o livro de ordens sossegue antes de recalcular
AGRUPAR_PRECOS_MAX_S = 90.0   # mas nunca mais do que isto


def lados_na_janela(lados, bid, inicio, F):
    """Os lados de uma batalha no inicio de uma janela futura: o fundo do bounty estimado pelo ritmo guardado."""
    agora = time.time()
    fora = []
    for l in lados:
        x = dict(l)
        if inicio > agora + 60:
            f = F.get(f"{bid}:{l['lado']}") or {}
            fundo = l.get("fundo")
            if isinstance(fundo, (int, float)) and fundo > 0 and f.get("ritmo_min") is not None and f["ritmo_min"] < 0:
                x["fundo"] = max(0.0, fundo + f["ritmo_min"] * (inicio - agora) / 60.0)
                x["fundo_estimado"] = True
        fora.append(x)
    return fora


def _ctx(lr, w, base):
    return {"contextos": lr["contextos"], "lados": w["lados"], "val": base["val"], "buff": w["mult"]}


def hmax_cheio(base):
    """Os golpes das barras CHEIAS (o maximo de todos os conjuntos e comidas): o tamanho das tabelas."""
    vida, fome = M3.vida_fome(_dados("jogador") or {})
    return int(max(1, BR.golpes(base["P"], max(vida["max"], vida["agora"]), max(fome["max"], fome["agora"])).max()))


def tab_de(m, t, wi):
    """As tabelas de uma ronda numa janela (a de agora faz-se no calculo; outra so quando e precisa)."""
    R = m.res[t]
    if wi not in R["tabs"]:
        R["tabs"][wi] = BR.tabelas(_ctx(E.leituras_ronda[t], R["janelas"][wi], m.base), m.base["P"]["dpg1s"],
                                   m.base["hmax"])
    return R["tabs"][wi]


def calcular(m: Motor, sujas: list[str]):
    """
    V3.2: o que muda numa ronda (ranking, bounty, lados, janelas da pilula) refaz
    so as TABELAS dessa ronda — o que cada dano rende (itens no pior cenario e no
    esperado, bounty), para todos os dpg1 e todos os golpes, na janela de agora.
    Nao dependem do conjunto: nao ha 4096 planos por ronda. A divisao pelas
    rondas e as contas de cada conjunto fazem-se ao publicar (< 1 s).
    """
    base = m.base
    agora = time.time()
    js = M3.janelas(_dados("jogador") or {}, base["itens"], agora)
    F = fundos_previsao()
    trabalho = {"inicio": agora, "rondas": list(sujas), "feitas": 0, "total": len(sujas),
                "conjuntos": len(base["grupos"]), "janelas": 0, "cancelado": False}
    m.trabalho = trabalho
    m.estado = "a calcular"
    for t in sujas:
        lr = E.leituras_ronda[t]
        ch = chave_ronda(t)
        fim_abs = lr["lido_em"] + lr["fim_s"]
        lados0 = m.lados_de(lr["r"]["batalha"])
        info = [{"nome": w["nome"], "inicio": w["inicio"], "fim": w["fim"], "mult": w["mult"],
                 "lados": lados_na_janela(lados0, lr["r"]["batalha"], w["inicio"], F)}
                for w in M3.janelas_da_ronda(js, fim_abs)]
        tabs = {0: BR.tabelas(_ctx(lr, info[0], base), base["P"]["dpg1s"], base["hmax"])} if info else {}
        fonte = E.fontes.get("ronda:" + t)
        if (m.chave_base() != m.base_chave or not fonte or fonte.versao != lr["versao"]
                or bool(TESTE_CANCELAR and TESTE_CANCELAR(t))):
            # dados mais recentes chegaram a meio: abandona; o que estava a ser feito nao entra
            trabalho["cancelado"] = True
            m.cancelados += 1
            m.trabalho = None
            E.evento.set()
            return
        m.res[t] = {"chave": ch, "janelas": info, "tabs": tabs, "lido_em": lr["lido_em"]}
        trabalho["feitas"] += 1
        trabalho["janelas"] += len(info)
    trabalho["fim"] = time.time()
    m.ultimo_calculo = {"duracao_s": trabalho["fim"] - trabalho["inicio"], "rondas": list(sujas),
                        "conjuntos": len(base["grupos"]), "janelas": trabalho["janelas"],
                        "fim": agora_iso(trabalho["fim"]), "tipo": "tabelas das rondas"}
    m.trabalho = None


def precos_pendentes(m):
    """Agrupa as mudancas do livro de ordens e corre a materialidade quando o livro sossega."""
    f = E.fontes["livro"]
    if not m.base or m.livro_usado is None or f.versao == m.livro_usado or not m.res:
        return
    agora = time.time()
    if m.precos_desde is None:
        m.precos_desde = agora
    m.precos_agrupadas = f.versao - m.livro_usado
    ultima = f.fim_leitura or f.lido_em or agora
    if agora - ultima < AGRUPAR_PRECOS_S and agora - m.precos_desde < AGRUPAR_PRECOS_MAX_S:
        return
    materialidade(m)


def materialidade(m):
    """
    V3.2: uma mudanca de precos do livro de ordens (municao, comida, cases, scraps).
      - as tabelas das rondas nao dependem destes precos: ficam;
      - comida, cases e desgaste custam o mesmo em TODOS os golpes: com as
        barras todas gastas, deslocam o lucro de cada conjunto em H x a
        diferenca e nao mudam a divisao. Refazem-se so as contas (exactas) dos
        4096 conjuntos;
      - so o preco da municao pode mudar a divisao: entao refaz-se (64 metades).
    O vencedor sai sempre das contas novas: nenhuma mudanca e ignorada.
    """
    t0 = time.time()
    lv = _dados("livro")
    v_livro = E.fontes["livro"].versao
    ob = m.base
    nb = M3.preparar_base(_dados("jogador"), _dados("equipamento"), _dados("config"), _dados("precos_eq"),
                          lv["ref"], lv["livro"], _dados("resumos"), E.manuais)
    nb["P"] = BR.preparar(nb["grupos"])
    nb["hmax"] = ob["hmax"]
    nb["segundos"] = time.time() - t0
    mud = []
    for c in M3.BENS:
        a = (ob["cotacoes"].get(c) or {}).get("valor")
        b = (nb["cotacoes"].get(c) or {}).get("valor")
        if a != b:
            mud.append({"codigo": c, "antes": a, "agora": b})
    mesmos = (nb["P"]["ks"] == ob["P"]["ks"] and nb["P"]["dpg1s"] == ob["P"]["dpg1s"]
              and BR.np.array_equal(nb["P"]["u_m"], ob["P"]["u_m"]))
    municao = not BR.np.array_equal(nb["P"]["c_m"], ob["P"]["c_m"])
    m.base, m.livro_usado, m.precos_desde = nb, v_livro, None
    if not mesmos:
        # mudaram as pecas possiveis (ex.: uma peca deixou de ter preco): isso ja nao e so preco
        m.versao_base += 1
        tipo = "os conjuntos mudaram: tudo de novo"
    else:
        tipo = "divisão refeita (o preço da munição mudou)" if municao else "só as contas (comida, cases, desgaste)"
    m.custos_versao += 1
    m.materialidade = {"t": agora_iso(), "mudancas": mud, "agrupadas": m.precos_agrupadas, "tipo": tipo,
                       "divisao_refeita": bool(municao or not mesmos), "conjuntos": len(nb["grupos"]),
                       "segundos_base": nb["segundos"], "segundos": None, "vencedor_mudou": None}
    m.historico_mat = (m.historico_mat + [m.materialidade])[-20:]


# ---------------------------------------------------------------------------
#  6. a publicacao: a estrategia, as compras, o bounty, o que mudou
# ---------------------------------------------------------------------------

REFERENCIA = {"n": 2198, "p": {"2": 49.27, "3": 31.89, "4": 16.29, "5": 2.18, "6": 0.36}}


def _opcoes(base):
    fora, idx = {}, {}
    for s in M3.SLOTS:
        fora[s] = [{k: o.get(k) for k in ("codigo", "origem", "raridade", "estado", "preco", "capex", "liquido",
                                          "stats", "id")} for o in base["op6"][s]]
        idx[s] = {M3.chave_peca(o): i for i, o in enumerate(base["op6"][s])}
    return fora, idx


def _var(base, chave, mun, com):
    return next(b for b in base["grupos"][chave] if b["municao"] == mun and b["comida"] == com)


def _fim_restante(t):
    lr = E.leituras_ronda[t]
    return lr["fim_s"] - (time.time() - lr["lido_em"]), lr["lido_em"] + lr["fim_s"]


def _jogador():
    u = _dados("jogador") or {}
    vida, fome = M3.vida_fome(u)
    f = E.fontes["jogador"]
    lido = datetime.fromtimestamp(f.lido_em or time.time(), timezone.utc)
    return u, vida, fome, lido


def plano_barras(m, titulos, vida, fome):
    """
    V3.2: o plano ate esvaziar as barras, para os 4096 conjuntos.
    Os golpes que as barras dao agora; se nao dao nenhum, os da proxima hora
    certa em que dao. Entram as rondas abertas nessa hora, na janela da pilula
    dessa hora. A divisao (por metade de dano, para todos os h) so se refaz
    quando mudam as rondas ou o preco da municao; o resto e so ler.
    """
    base = m.base
    P = base["P"]
    lido_ts = E.fontes["jogador"].lido_em or time.time()
    agora = time.time()
    H = BR.golpes(P, vida["agora"], fome["agora"])
    t0, v0, f0 = agora, vida["agora"], fome["agora"]
    if H.max() <= 0:
        lido = datetime.fromtimestamp(lido_ts, timezone.utc).replace(minute=0, second=0, microsecond=0)
        for k in range(1, 25):
            alvo = (lido + timedelta(hours=k)).timestamp()
            v_k, f_k = M3.recursos_em(vida, fome, lido_ts, alvo)
            Hk = BR.golpes(P, v_k, f_k)
            if Hk.max() > 0:
                H, t0, v0, f0 = Hk, alvo, v_k, f_k
                break
    if H.max() > base["hmax"]:
        # a vida maxima subiu desde a base: as tabelas tem de crescer
        base["hmax"] = int(H.max())
        for R in m.res.values():
            R["tabs"].clear()
        m.divisao = m.divisao_chave = None
    rondas = []
    for t in titulos:
        R = m.res[t]
        lr = E.leituras_ronda[t]
        if lr["lido_em"] + lr["fim_s"] <= t0 + 60:
            continue
        wi = next((i for i, w in enumerate(R["janelas"])
                   if w["inicio"] <= t0 + 1 and (w["fim"] is None or t0 < w["fim"])), None)
        if wi is not None:
            rondas.append((t, wi))
    tabs = [tab_de(m, t, wi) for t, wi in rondas]
    chave = (m.versao_base, tuple((t, m.res[t]["chave"], wi) for t, wi in rondas), tuple(P["c_m"].tolist()),
             base["hmax"])
    if m.divisao_chave != chave:
        t1 = time.time()
        m.divisao = BR.construir(tabs, P, base["hmax"])
        m.divisao_chave = chave
        m.tempos["divisao_s"] = time.time() - t1
        m.tempos["divisao_em"] = agora_iso()
    t1 = time.time()
    so = P["comidas"].index(E.comida) if E.comida in P["comidas"] else None
    # V3.3: cada conjunto e comida bate ate onde o lucro sobe; o resto das barras fica para depois
    Hp = BR.picos(m.divisao, P, H, base["caso_golpe"])
    res = BR.avaliar(tabs, P, m.divisao, Hp, base["caso_golpe"], so_comida=so)
    m.tempos["contas_s"] = time.time() - t1
    return {"res": res, "rondas": rondas, "tabs": tabs, "t0": t0, "v0": v0, "f0": f0, "H": H, "Hp": Hp,
            "titulos": titulos}


def _tot(res, i):
    g = int(res["g"][i].sum()) if res["g"].shape[1] else 0
    d = float(res["dano"][i])
    ct = float(res["contrato"][i]) if "contrato" in res else 0.0
    return {"golpes": g, "dano": d, "lucro": float(res["lucro"][i]), "lucro_1k": float(res["lucro"][i]) / (d / 1000.0) if d > 0 else None,
            "bounty": float(res["bounty"][i]) - ct, "contrato": ct,
            "itens": float(res["itens"][i]), "cases": float(res["cases"][i]),
            "custo": float(res["custo"][i]), "esperado": float(res["esperado"][i])}


def _drops(d, ctx, val):
    """Os drops nesse dano: no pior cenario (o de menos itens, como o `pior` do planear) e no esperado."""
    cen = [n for n in O.CENARIOS if n in ctx]
    res = {n: O._resultado(d, ctx[n], val) for n in cen}
    pior = min(cen, key=lambda n: sum(x["valor"] for x in res[n].values()))
    rp, re_ = res[pior], res.get("esperado") or {}
    return pior, {p: {"nome": (rp.get(p) or {}).get("nome"), "valor": (rp.get(p) or {}).get("valor"),
                      "rank": (rp.get(p) or {}).get("rank")} for p in O.POOLS if rp.get(p)}, \
        {p: (re_.get(p) or {}).get("nome") for p in O.POOLS if re_.get(p)}


def _janela_pub(w):
    return {"nome": w["nome"], "mult": w["mult"], "inicio": agora_iso(w["inicio"]), "inicio_hora": agora_iso(w["inicio"])[11:16],
            "fim": agora_iso(w["fim"]) if w.get("fim") else None, "fim_hora": agora_iso(w["fim"])[11:16] if w.get("fim") else None}


def _pt(x, casas, sinal=False):
    """Um numero como a pagina o mostra: virgula decimal; com sinal quando e um lucro."""
    return (f"{x:+.{casas}f}" if sinal else f"{x:.{casas}f}").replace(".", ",")


def _k(x):
    return (f"{x / 1e6:.2f}M" if x >= 1e6 else f"{x / 1e3:.1f}k" if x >= 1e3 else f"{x:.0f}").replace(".", ",")


def _veredicto(alts):
    """
    Gastar as barras ja ou esperar por outra janela da pilula: o criterio e o
    lucro por 1k. Se a melhor por 1k da MENOS lucro total, diz-se sempre (aviso).
    """
    ja = alts[0]
    outras = [a for a in alts[1:] if a.get("lucro_1k") is not None]
    if not outras:
        return {"compensa_esperar": False, "texto": "Não há outra janela da pílula antes de as rondas acabarem.",
                "aviso": None}
    b = max(outras, key=lambda a: a["lucro_1k"])
    q = f"às {b['janela']['inicio_hora']} ({b['janela']['nome']}, ×{_pt(b['janela']['mult'], 1)})"
    if ja.get("lucro_1k") is None or b["lucro_1k"] > ja["lucro_1k"] + 1e-9:
        aviso = None
        if ja.get("lucro") is not None and b["lucro"] < ja["lucro"] - 0.01:
            aviso = (f"Mais por 1k, mas menos lucro total: {_pt(b['lucro'], 2, True)} {q} contra "
                     f"{_pt(ja['lucro'], 2, True)} se gastares já. Diferença: {_pt(ja['lucro'] - b['lucro'], 2, True)}.")
        return {"compensa_esperar": True, "hora": b["janela"]["inicio_hora"], "aviso": aviso,
                "texto": f"Gastar as mesmas barras {q} compensa pelo lucro por 1k: {_pt(b['lucro_1k'], 2)}/1k e "
                         f"{_pt(b['lucro'], 1, True)} ({_k(b['dano'])} de dano), contra "
                         + (f"{_pt(ja['lucro_1k'], 2)}/1k e {_pt(ja['lucro'], 1, True)} já." if ja.get("lucro_1k") is not None
                            else "nada já.")}
    aviso = None
    mais = max(outras, key=lambda a: a["lucro"])
    if mais["lucro"] > ja["lucro"] + 0.01:
        qm = f"às {mais['janela']['inicio_hora']} ({mais['janela']['nome']})"
        aviso = (f"Esperar {qm} dava mais lucro total ({_pt(mais['lucro'], 2, True)} contra {_pt(ja['lucro'], 2, True)}), "
                 f"mas menos por 1k ({_pt(mais['lucro_1k'], 2)} contra {_pt(ja['lucro_1k'], 2)}).")
    return {"compensa_esperar": False, "hora": b["janela"]["inicio_hora"], "aviso": aviso,
            "texto": f"Não compensa esperar: {q} as mesmas barras davam {_pt(b['lucro_1k'], 2)}/1k e "
                     f"{_pt(b['lucro'], 1, True)}; já dão {_pt(ja['lucro_1k'], 2)}/1k e {_pt(ja['lucro'], 1, True)}."}


def comparar_janelas(m, PL, i, ci, tot):
    """
    As mesmas barras gastas nas outras janelas da pilula (rondas abertas nessa hora, fundo estimado).
    V3.3: em cada janela bate-se ate onde o lucro sobe, como agora (as barras sao o tecto).
    """
    base = m.base
    H = int(PL["H"][i, ci])
    w0 = next((m.res[t]["janelas"][wi] for t, wi in PL["rondas"]), None)
    alts = [{"janela": {**(_janela_pub(w0) if w0 else {"nome": "agora", "mult": 1.0, "inicio_hora": agora_iso(PL["t0"])[11:16]}),
                        "indice": 0}, "escolhida": True, "dano": tot["dano"], "lucro": tot["lucro"],
             "lucro_1k": tot["lucro_1k"], "rondas": len(PL["rondas"]), "fundo_estimado": False}]
    if H <= 0 or w0 is None:
        return alts, _veredicto(alts)
    js = M3.janelas(_dados("jogador") or {}, base["itens"], time.time())
    for k, wj in enumerate(js):
        if wj["inicio"] <= PL["t0"] + 60:
            continue
        ctxs, est = [], False
        for t in PL["titulos"]:
            R = m.res[t]
            wi = next((x for x, w in enumerate(R["janelas"]) if abs(w["inicio"] - wj["inicio"]) < 2), None)
            if wi is None:
                continue
            w = R["janelas"][wi]
            ctxs.append(_ctx(E.leituras_ronda[t], w, base))
            est = est or any(l.get("fundo_estimado") for l in w["lados"])
        if not ctxs:
            continue
        r = BR.plano_unico(ctxs, base["P"], i, ci, H, base["caso_golpe"], pico=True)
        x = r["res"]
        d = float(x["dano"][0])
        alts.append({"janela": {**_janela_pub(wj), "indice": len(alts)}, "escolhida": False, "dano": d,
                     "lucro": float(x["lucro"][0]), "lucro_1k": float(x["lucro"][0]) / (d / 1000.0) if d > 0 else None,
                     "rondas": len(ctxs), "fundo_estimado": est})
    return alts, _veredicto(alts)


def _sobra(vida, fome, h, b):
    """
    O que fica nas barras depois de h golpes esperados, com as regras de `golpes_com`
    (come-se so o preciso, um ponto inteiro de fome de cada vez). Vida esperada:
    um golpe esquivado nao gasta vida.
    """
    vg, vgc, vc = b["vida_golpe"], b["_vida_golpe_cheio"], b["vida_comida"]
    if h <= 0 or vgc <= 0:
        return {"come": 0, "vida": vida, "fome": fome}
    esq = max(0.0, min(0.95, 1.0 - vg / vgc))
    g = 0
    while int(math.floor(g / (1.0 - esq) + 1e-9)) < h:
        g += 1
    n, inteiros = 0, int(math.floor(fome + 1e-9))
    while vida + n * vc + 1e-9 < g * vgc and n < inteiros:
        n += 1
    return {"come": n, "vida": max(0.0, vida + n * vc - h * vg), "fome": fome - n}


def estrategia(m, PL, i, nome):
    """Os passos de um conjunto: ronda a ronda, o lado, a municao e o DANO a fazer; e os totais."""
    base = m.base
    P = base["P"]
    res0 = PL["res"]
    chave = P["ks"][i]
    ci = int(res0["comida"][i])
    com = P["comidas"][ci]
    h = int(res0["H"][i])
    # V3.3: os mesmos golpes, com preferencia pelos lados com ordem tua (ate perder E.pref_ordens do lucro)
    ha_ordens = any(l.get("ordens") for tab in PL["tabs"] for l in tab["lados"])
    t1 = time.time()
    pr = BR.preferir_ordens(PL["tabs"], P, i, ci, h, base["caso_golpe"], E.pref_ordens) if PL["rondas"] else None
    m.tempos["ordens_s"] = round(time.time() - t1, 4)     # a ultima das tres estrategias
    res, j = (pr["res"], 0) if pr else (res0, i)
    passos = []
    for r, (t, wi) in enumerate(PL["rondas"]):
        g = int(res["g"][j, r])
        if g <= 0:
            continue
        tab = PL["tabs"][r]
        li, mi = int(res["lado"][j, r]), int(res["mun"][j, r])
        L = tab["lados"][li]
        mun = P["munis"][mi]
        u = int(P["u_m"][P["d_de"][i], mi])
        b = _var(base, chave, mun, com)
        d = g * float(L["dpg"][u])
        it, ev, bt = float(L["W"][u, g]), float(L["Ev"][u, g]), float(L["BT"][u, g])
        ct = float(L["CT"][u, g]) if "CT" in L else 0.0      # V3.3c: o contrato (ja dentro de bt)
        cs, cu = g * base["caso_golpe"], g * b["custo_golpe"]
        lucro = ((it + cs) + bt) - cu
        lr = E.leituras_ronda[t]
        w = m.res[t]["janelas"][wi]
        pior, drops, drops_esp = _drops(d, lr["contextos"], base["val"])
        # V3.3b: a peca que o dano ja feito te da sem bater mais (so nos pools onde tens dano)
        _, drops0, _ = _drops(0.0, lr["contextos"], base["val"])
        cx0 = lr["contextos"].get(pior) or {}
        ja_drops = {pl: x for pl, x in drops0.items() if float((cx0.get(pl) or {}).get("ja") or 0.0) > 0}
        rest, fim_abs = _fim_restante(t)
        est_r = lr["r"].get("estado") or {}
        tu, tu_b = est_r.get("tu") or {}, est_r.get("tu_batalha") or {}
        passos.append({
            "t": t, "lado": L["lado"], "pais": L["pais"], "contra": L["contra"], "fator": L["fator"],
            "taxa": L["taxa"], "fundo": L["fundo"], "estado_bounty": L["estado"], "fundo_estimado": L["fundo_estimado"],
            "municao": mun, "comida": com, "dpg": float(L["dpg"][u]), "golpes": g, "dano": d,
            "itens": it, "bounty": bt - ct, "contrato": ct, "contrato_1k": L.get("contrato_1k") or 0.0,
            "contrato_medido": L.get("contrato"), "cases": cs, "custo": cu, "lucro": lucro,
            "lucro_1k": lucro / (d / 1000.0) if d > 0 else None, "pior": pior, "drops": drops,
            "esperado": {"lucro": ((ev + cs) + bt) - cu, "itens": ev, "drops": drops_esp},
            "c_mun": g * b["_c_mun"], "c_comida": g * b["_c_comida"], "c_desgaste": g * b["_c_desgaste"],
            "fim_s": rest, "fim_hora": agora_iso(fim_abs)[11:16], "fim_iso": agora_iso(fim_abs),
            "ronda_lida_em": agora_iso(lr["lido_em"]), "batalha": lr["r"]["batalha"],
            "janela": {**_janela_pub(w), "indice": wi}, "inicio_passo": agora_iso(PL["t0"]),
            # V3.2: o que ja fizeste (o plano conta com isto: o teu lugar no fim e o do total)
            "ja": tu.get("dano") or 0.0, "ja_rank": tu.get("rank"), "ja_item": tu.get("item"),
            "total_ronda": (tu.get("dano") or 0.0) + d,
            "ja_batalha": tu_b.get("dano") if "batalha" in drops else None,
            "ja_drops": ja_drops, "ja_valor": BR.ja_garantido(_ctx(lr, w, base))["pior"],
            "ordens": list(L.get("ordens") or [])})
    tot = _tot(res, j)
    tot0 = _tot(res0, i)
    Hb = int(PL["H"][i, ci])                     # o que as barras dao com esta comida
    b0 = next(b for b in base["grupos"][chave] if b["comida"] == com)
    # V3.3: o que fica para depois, e o que dava gasta-lo ja
    guardar = None
    if Hb > h:
        tudo = None
        if PL["rondas"]:
            Pi = {**P, "d_de": P["d_de"][i:i + 1], "custo": P["custo"][i:i + 1, :, ci:ci + 1]}
            x = BR.avaliar(PL["tabs"], Pi, m.divisao, BR.np.array([[Hb]]), base["caso_golpe"])
            tudo = {"dano": float(x["dano"][0]), "lucro": float(x["lucro"][0])}
        guardar = {"golpes": Hb - h, "golpes_barras": Hb, **_sobra(PL["v0"], PL["f0"], h, b0),
                   "tudo": tudo, "resto_dano": tudo["dano"] - tot0["dano"] if tudo else None,
                   "resto_lucro": tudo["lucro"] - tot0["lucro"] if tudo else None}
    ordens = {"tolerancia": E.pref_ordens, "opcoes": list(PREF_ORDENS_OPCOES), "ha": ha_ordens,
              "aplicada": bool(pr), "dano_ordem": BR.dano_com_ordem(PL["tabs"], res, j) if PL["rondas"] else 0.0,
              "dano_ordem_sem": BR.dano_com_ordem(PL["tabs"], res0, i) if pr else None,
              "lucro_sem": tot0["lucro"] if pr else None,
              "perda": tot0["lucro"] - tot["lucro"] if pr else 0.0}
    alts, espera = comparar_janelas(m, PL, i, ci, tot0)
    # o que cada comida daria com este equipamento: bate ate onde o lucro sobe, e quantos golpes ficam
    por_comida = []
    if PL["rondas"]:
        Pi = {**P, "d_de": P["d_de"][i:i + 1], "custo": P["custo"][i:i + 1]}
        for cj, nome_c in enumerate(P["comidas"]):
            if BR.np.isnan(P["custo"][i, :, cj]).all():
                continue
            x = BR.avaliar(PL["tabs"], Pi, m.divisao, PL["Hp"][i:i + 1], base["caso_golpe"], so_comida=cj)
            dc, lc, gc = float(x["dano"][0]), float(x["lucro"][0]), int(x["g"][0].sum())
            bc = next(b for b in base["grupos"][chave] if b["comida"] == nome_c)
            por_comida.append({"comida": nome_c, "dano": dc, "lucro": lc, "lucro_1k": lc / (dc / 1000.0) if dc > 0 else None,
                               "golpes": gc, "golpes_barras": int(PL["H"][i, cj]), "ficam": int(PL["H"][i, cj]) - gc,
                               "vida_comida": bc["vida_comida"],
                               "golpes_por_fome": bc["vida_comida"] / bc["vida_golpe"] if bc["vida_golpe"] else None,
                               "sobra": _sobra(PL["v0"], PL["f0"], gc, bc), "escolhida": cj == ci})
    return {"nome": nome, "chave": list(chave), "capex": b0["capex"], "tens": b0["tens"],
            "raridades": sorted({b0["pecas"][s]["raridade"] for s in M3.SLOTS}), "total": tot, "passos": passos,
            "comida": com, "barras": {"golpes": Hb, "plano": h, "vida": PL["v0"], "fome": PL["f0"],
                                      "inicio": agora_iso(PL["t0"]), "agora": PL["t0"] <= time.time() + 1},
            "guardar": guardar, "ordens": ordens, "sem_preferencia": tot0 if pr else None,
            "alternativas": alts, "espera": espera, "por_comida": por_comida,
            "vida_golpe": b0["vida_golpe"], "vida_golpe_cheio": b0["_vida_golpe_cheio"], "vida_comida": b0["vida_comida"],
            "esquiva": b0["_esquiva"], "poupa": b0["_poupa"]}


def fundos_previsao():
    """Velocidade de esgotamento de cada fundo, das leituras guardadas (ultimos 90 min)."""
    regs = ler_hist("fundos")
    lim = time.time() - 5400
    por = {}
    for r in regs:
        t = O._ts(r["t"])
        if t and t >= lim:
            por.setdefault((r["batalha"], r["lado"]), []).append((t, r["fundo"], r["taxa"]))
    fora = {}
    for (bid, lado), xs in por.items():
        xs.sort(key=lambda x: x[0])
        ult = xs[-1]
        d = {"observado": ult[1], "lido_em": agora_iso(ult[0]), "taxa": ult[2], "leituras": len(xs),
             "desde": agora_iso(xs[0][0]), "ritmo_min": None, "esgota_em_min": None, "estado": "sem dados suficientes"}
        validos = [x for x in xs if isinstance(x[1], (int, float))]
        if ult[1] is None:
            d["estado"] = "sem fundo"
        elif ult[1] <= 0:
            d["estado"] = "esgotado"
        elif len(validos) >= 2 and validos[-1][0] - validos[0][0] >= 60:
            dt = (validos[-1][0] - validos[0][0]) / 60.0
            ritmo = (validos[-1][1] - validos[0][1]) / dt
            d["ritmo_min"] = ritmo
            if ritmo < 0:
                d["esgota_em_min"] = ult[1] / -ritmo
                d["estado"] = "a esgotar"
            else:
                d["estado"] = "estável ou a subir"
        fora[f"{bid}:{lado}"] = d
    return fora


def compras(m, sim_por, idx, vest_chave, titulos):
    """Trocar UMA peca do que tens por uma do mercado: o que muda, peca a peca."""
    base = m.base
    ref = sim_por.get(vest_chave)
    if not ref:
        return {"base": None, "trocas": []}
    trocas = []
    vest = list(vest_chave)
    for i, s in enumerate(M3.SLOTS):
        for o in base["op6"][s]:
            if o["origem"] != "comprar":
                continue
            k = list(vest)
            k[i] = M3.chave_peca(o)
            r = sim_por.get(tuple(k))
            if not r:
                continue
            frac = 1.0 if s == "weapon" else (base["grupos"][tuple(k)][0]["_poupa"])
            desgaste_golpe = (o["liquido"] or 0) / 100.0 * frac
            cot = base["cotacoes"].get(o["codigo"]) or {}
            trocas.append({"slot": s, "sai": base["grupos"][vest_chave][0]["pecas"][s]["codigo"],
                           "entra": o["codigo"], "raridade": o["raridade"], "preco": o["capex"],
                           "fonte_preco": cot.get("natureza") or "estimativa", "endpoint_preco": cot.get("endpoint"),
                           "desgaste_golpe": desgaste_golpe, "golpes": r["g"], "desgaste_plano": desgaste_golpe * r["g"],
                           "durabilidade_plano_pct": r["g"] * frac,
                           "lucro_actual": ref["l"], "lucro_novo": r["l"], "dif_lucro": r["l"] - ref["l"],
                           "r1k_actual": ref["r"], "r1k_novo": r["r"], "dif_r1k": r["r"] - ref["r"],
                           "dano_actual": ref["d"], "dano_novo": r["d"],
                           "compensa": r["r"] > ref["r"] + 1e-9})
    trocas.sort(key=lambda x: (-x["dif_r1k"], -x["dif_lucro"]))
    return {"base": {"lucro": ref["l"], "r1k": ref["r"], "dano": ref["d"]}, "trocas": trocas,
            "revenda": "sem dados: não há livro de ordens de equipamento, só a média de vendas por código"}


def _resumo_pub(p):
    if not p:
        return None
    fora = {}
    for o, e in p["estrategias"].items():
        passos = {}
        for x in e["passos"]:
            d = {k: x[k] for k in ("golpes", "lado", "municao", "lucro", "dano")}
            d["drops"] = {k: (v or {}).get("nome") for k, v in x["drops"].items()}
            passos[x["t"]] = d
        fora[o] = {"pecas": e["pecas"], "total": e["total"], "passos": passos}
    return fora


def diferencas(antes, agora):
    if not antes:
        return None
    ra, rb = antes["rondas"], agora["rondas"]
    dados = []
    if antes["vida"]["agora"] != agora["vida"]["agora"]:
        dados.append({"fonte": "vida", "antes": antes["vida"]["agora"], "agora": agora["vida"]["agora"]})
    if antes["fome"]["agora"] != agora["fome"]["agora"]:
        dados.append({"fonte": "fome", "antes": antes["fome"]["agora"], "agora": agora["fome"]["agora"]})
    for t in ra:
        if t not in rb:
            dados.append({"fonte": f"ronda {t}", "antes": f"r{ra[t]['numero']}", "agora": "saiu da lista"})
    for t, y in rb.items():
        x = ra.get(t)
        if not x:
            dados.append({"fonte": f"ronda {t}", "antes": "—", "agora": f"r{y['numero']} nova na lista"})
            continue
        if x["numero"] != y["numero"]:
            dados.append({"fonte": f"ronda {t}", "antes": f"r{x['numero']}", "agora": f"r{y['numero']} (ronda nova)"})
        elif x["jogadores"] != y["jogadores"]:
            dados.append({"fonte": f"jogadores {t}", "antes": x["jogadores"], "agora": y["jogadores"]})
        la = {l["lado"]: l for l in x["lados"]}
        for l in y["lados"]:
            o = la.get(l["lado"]) or {}
            if o.get("fundo") != l.get("fundo") and (l.get("taxa") or 0) > 0:
                dados.append({"fonte": f"fundo {l['pais']}", "antes": o.get("fundo"), "agora": l.get("fundo"),
                              "estado": l.get("estado")})
    for c in ("lightAmmo", "ammo", "heavyAmmo", "bread", "cocain", "case1", "case2"):
        a, b = (antes["cotacoes"].get(c) or {}).get("valor"), (agora["cotacoes"].get(c) or {}).get("valor")
        if a != b:
            dados.append({"fonte": f"preço {c}", "antes": a, "agora": b})
    ea, eb = _resumo_pub(antes), _resumo_pub(agora)
    est = {}
    for o in M3.OBJECTIVOS:
        a, b = ea[o], eb[o]
        passos = []
        for t in sorted(set(a["passos"]) | set(b["passos"])):
            x, y = a["passos"].get(t), b["passos"].get(t)
            if x and not y:
                passos.append({"t": t, "tipo": "saiu", "antes": x})
            elif y and not x:
                passos.append({"t": t, "tipo": "entrou", "agora": y})
            else:
                camp = [k for k in ("golpes", "lado", "municao") if x[k] != y[k]]
                if x["drops"] != y["drops"]:
                    camp.append("drops")
                passos.append({"t": t, "tipo": "mudou" if camp else "igual", "campos": camp, "antes": x, "agora": y})
        est[o] = {"antes": {"pecas": a["pecas"], "total": a["total"]}, "agora": {"pecas": b["pecas"], "total": b["total"]},
                  "equipamento_mudou": a["pecas"] != b["pecas"], "passos": passos}
    return {"antes_calculado": antes["calculado_em"], "agora_calculado": agora["calculado_em"],
            "antes_hora": antes["calculado_em"][11:16], "agora_hora": agora["calculado_em"][11:16],
            "dados": dados, "estrategia": est}


def _pilula(u, base):
    bf = u.get("buffs") or {}
    atk = (u.get("skills") or {}).get("attack") or {}
    ate = bf.get("buffEndAt")
    t = O._ts(ate)
    rt = O._ts(bf.get("debuffEndAt"))
    flat = ((base["itens"].get("cocain") or {}).get("flatStats") or {})
    return {"ate": ate, "ate_hora": agora_iso(t)[11:16] if t else None,
            "h_ate_fim": ((t - time.time()) / 3600.0) if t else None,
            "ataque_pct": flat.get("percentAttack"), "horas": flat.get("buffDurationHours"),
            "ressaca_h": flat.get("debuffDurationHours"), "ressaca_pct_projecto": -60,
            # o que a API diz agora (verificado a 20/11: debuffsPercent 60, debuffEndAt = fim do buff + 15,5 h)
            "buff_pct_api": atk.get("buffsPercent"), "ressaca_pct_api": atk.get("debuffsPercent"),
            "ressaca_ate": bf.get("debuffEndAt"), "ressaca_ate_hora": agora_iso(rt)[11:16] if rt else None,
            "multiplicador": base.get("buff")}


def _nome_conj(ch):
    return " · ".join(x.split("|")[0] + ("*" if ("|tens|" in x or "|manual|" in x) else "") for x in ch)


def publicar(m, prontas):
    base = m.base
    t_pub = time.time()
    u, vida, fome, lido = _jogador()
    titulos = sorted(prontas, key=lambda t: _fim_restante(t)[1])
    opcoes, idx = _opcoes(base)
    t_calc = time.time()
    PL = plano_barras(m, titulos, vida, fome)
    res, Pb = PL["res"], base["P"]
    sim, sim_por, chaves = [], {}, {}
    for i, chave in enumerate(Pb["ks"]):
        bs = base["grupos"][chave]
        tot = _tot(res, i)
        por_t = [[int(res["g"][i, r]), round(float(res["por_r"][i, r, 0])), round(float(res["por_r"][i, r, 1]), 2),
                  int(res["mun"][i, r]), int(wi)] if res["g"][i, r] > 0 else 0
                 for r, (_, wi) in enumerate(PL["rondas"])]
        k = ",".join(str(idx[s][x]) for s, x in zip(M3.SLOTS, chave))
        row = {"k": k, "c": round(bs[0]["capex"], 2), "g": tot["golpes"], "d": round(tot["dano"]),
               "l": round(tot["lucro"], 3), "r": round(tot["lucro_1k"] or 0, 4), "b": round(tot["bounty"], 3),
               "u": round(tot["custo"], 3), "i": round(tot["itens"], 3), "x": round(tot["cases"], 3),
               "e": round(tot["esperado"], 3), "m": round(max(b["dpg1"] for b in bs)), "p": por_t,
               "f": Pb["comidas"][int(res["comida"][i])]}
        sim.append(row)
        sim_por[chave] = row
        chaves[chave] = tot
    vence = {o: max(chaves, key=lambda ch, o=o: M3.chave_obj(o, chaves[ch], base["grupos"][ch][0]["capex"]))
             for o in M3.OBJECTIVOS}
    pos = {ch: i for i, ch in enumerate(Pb["ks"])}
    nomes = {"regra": "Lucro por 1k de dano", "lucro": "Lucro total", "sem": "Sem compras"}
    estrategias = {o: estrategia(m, PL, pos[vence[o]], nomes[o]) for o in M3.OBJECTIVOS}
    m.tempos["publicar_s"] = time.time() - t_calc
    for o in estrategias:
        estrategias[o]["pecas"] = [idx[s][x] for s, x in zip(M3.SLOTS, vence[o])]
    no_motor = [ch for ch in chaves if ch in base["fronteira"]]
    mm = max(no_motor, key=lambda ch: M3.chave_obj("regra", chaves[ch], base["grupos"][ch][0]["capex"])) if no_motor else None
    prova = {"todas": len(chaves), "no_espaco_do_motor": len(no_motor),
             "builds": sum(len(v) for v in base["grupos"].values()), "builds_motor": base["builds_motor"],
             "vencedor": _nome_conj(vence["regra"]), "vencedor_motor": _nome_conj(mm) if mm else None,
             "r1k": chaves[vence["regra"]]["lucro_1k"], "r1k_motor": chaves[mm]["lucro_1k"] if mm else None,
             "iguais": mm == vence["regra"]}
    vest = next((ch for ch in chaves if all("|tens|" in x for x in ch)), None)
    rondas = {}
    for t in titulos:
        lr = E.leituras_ronda[t]
        r, e = lr["r"], lr["r"].get("estado") or {}
        rest, fim_abs = _fim_restante(t)
        rondas[t] = {"numero": r.get("numero"), "batalha": r.get("batalha"), "ronda": r.get("ronda"),
                     "fim_s": rest, "fim_hora": agora_iso(fim_abs)[11:16], "lido_em": agora_iso(lr["lido_em"]),
                     "jogadores": e.get("jogadores"), "itens": e.get("itens"), "dano": e.get("dano"),
                     "teu_dano_ronda": e.get("tu"), "dano_jogador_batalha": None, "dano_jogador_lido_em": None,
                     "mais_itens": e.get("mais_itens_que_jogadores"), "fecha_batalha": e.get("fecha_batalha"),
                     "itens_batalha": e.get("itens_batalha"),
                     "lados": [{k: l.get(k) for k in ("lado", "pais", "fator", "taxa", "fundo", "estado", "ordem",
                                                      "contrato_1k")}
                               for l in m.lados_de(r["batalha"])],
                     "contrato": next((dict(l["contrato"], lado=l["lado"] if l["contrato"].get("aplicado") else None)
                                       for l in m.lados_de(r["batalha"]) if l.get("contrato")), None),
                     "regra": {k: {"ok": v.get("confere"), "itens": v.get("itens"), "esperados": v.get("itens_esperados"),
                                   "inversoes": v.get("inversoes"), "folga": v.get("folga")}
                               for k, v in (r.get("regra") or {}).items()},
                     "ritmo": {k: (r.get("ritmo") or {}).get(k) for k in ("recente", "media_desde_inicio")},
                     "backtest_erro_pct": (r.get("backtest") or {}).get("erro_total_pct"),
                     "janelas": [_janela_pub(w) for w in (m.res[t].get("janelas") or [])],
                     "no_plano": any(t == x for x, _ in PL["rondas"]),
                     "validacao": {pool: VALIDACAO.get(f"{pool}:{r.get('ronda') if pool == 'ronda' else r.get('batalha')}")
                                   for pool in (r.get("regra") or {})}}
    validacoes = _dados("previsoes") or []
    lj = E.fontes["jogador"].lido_em
    pub = {
        "semente": False, "versao": m.versao_pub + 1, "calculado_em": agora_iso(t_pub),
        "lido": agora_iso(lj), "lido_hora": agora_iso(lj)[11:16],
        "leitura_fim": agora_iso(max(lr["lido_em"] for lr in E.leituras_ronda.values())),
        "parcial": {"lidas": len(titulos), "escolhidas": len(E.rondas_escolhidas)},
        "jogador": {"nome": u.get("username"), "nivel": (u.get("leveling") or {}).get("level")},
        "vida": vida, "fome": fome, "primeiro_tick_h": M3.primeiro_tick_h(lido),
        "pilula": _pilula(u, base),
        "slots": [[s, M3.NOME_SLOT[s]] for s in M3.SLOTS], "opcoes": opcoes,
        "vestido": {s: {"codigo": v.get("code"), "estado": v.get("state"), "skills": v.get("skills"),
                        "qualidade": pecas.qualidade(v.get("skills") or {}, pecas.intervalos(v.get("code"), base["itens"]))}
                    for s, v in base["vestido"].items()},
        "estrategias": estrategias, "titulos": titulos, "sim": sim, "prova": prova, "rondas": rondas,
        "compras": compras(m, sim_por, idx, vest, titulos) if vest else None,
        "fundos": fundos_previsao(),
        "composicao": base["comp"], "referencia": REFERENCIA,
        "valores": {"novo": {O.NOME[k]: base["val"].novo(k) for k in (2, 3, 4, 5, 6)},
                    "vendas_com_roll": base["info_vendas"]},
        "receitas": {k: base["rec"].get(k) for k in ("cases_por_golpe", "case2_por_golpe", "por_golpe",
                                                     "fonte_cases", "valor_case1", "valor_case2")},
        "cotacoes": base["cotacoes"],
        "aberturas": {k: ((u.get("stats") or {}).get(k)) for k in ("case1", "case2", "toolbox", "woodenCase")},
        "validacoes": [{"ronda": v.get("ronda"), "jogadores": v.get("jogadores"), "itens": v.get("itens"),
                        "regra_ok": (v.get("regra") or {}).get("confere"),
                        "previsoes": [{"t": p.get("t"), "fim_estimado": p.get("fim_estimado"), "fim_real": p.get("fim_real"),
                                       "plano": p.get("plano")} for p in (v.get("previsoes") or [])][-3:]}
                       for v in validacoes][:8],
        "historico_estrategias": ler_hist("estrategias", 40),
        "historico_jogador": ler_hist("jogador", 60),
        "maldives": json.load(open(AQUI / "maldives.json")) if (AQUI / "maldives.json").exists() else None,
        "_chaves": {t: m.res[t]["chave"] for t in titulos},
        "janelas": [_janela_pub(w) for w in M3.janelas(u, base["itens"], time.time())],
        "materialidade": m.materialidade, "historico_materialidade": m.historico_mat[-8:],
        "torneios_fora": getattr(m, "torneios_fora", 0),
        "barras": {"inicio": agora_iso(PL["t0"]), "agora": PL["t0"] <= time.time() + 1, "vida": PL["v0"],
                   "fome": PL["f0"], "rondas": [t for t, _ in PL["rondas"]],
                   "fora": [t for t in titulos if not any(t == x for x, _ in PL["rondas"])],
                   "tempos": dict(m.tempos)},
        "validacao_ranking": sorted(VALIDACAO.values(), key=lambda v: v.get("t_viva") or "", reverse=True)[:30],
    }
    pub["mudancas"] = diferencas(m.publicado, pub)
    motivo = []
    if m.publicado:
        if m.publicado["_versao_base"] != m.versao_base:
            motivo.append("equipamento ou base")
        if m.publicado.get("_custos") != m.custos_versao:
            motivo.append("preços (materialidade)")
        if m.publicado.get("_versao_jogador") != E.fontes["jogador"].versao:
            motivo.append("vida e fome")
        motivo += [t for t in titulos if m.publicado["_chaves"].get(t) != pub["_chaves"][t]]
        # o vencedor mudou com a ultima mudanca de precos?
        if m.materialidade and m.materialidade.get("vencedor_mudou") is None and m.publicado.get("_custos") != m.custos_versao:
            m.materialidade["vencedor_mudou"] = {o: m.publicado["estrategias"][o]["pecas"] != estrategias[o]["pecas"]
                                                 for o in M3.OBJECTIVOS}
            m.materialidade["segundos"] = (m.materialidade.get("segundos_base") or 0) + m.tempos.get("publicar_s", 0)
            m.materialidade["divisao_s"] = m.tempos.get("divisao_s") if m.materialidade.get("divisao_refeita") else 0.0
            m.materialidade["contas_s"] = m.tempos.get("contas_s")
            hist("materialidade", dict(m.materialidade))
            pub["materialidade"] = m.materialidade
    pub["motivo"] = motivo or ["primeiro cálculo"]
    pub["_versao_base"] = m.versao_base
    pub["_comida"] = E.comida
    pub["comida"] = {"escolhida": E.comida, "comidas": Pb["comidas"]}
    pub["_pref_ordens"] = E.pref_ordens
    pub["_custos"] = m.custos_versao
    pub["_versao_jogador"] = E.fontes["jogador"].versao
    hist("estrategias", {"t": pub["calculado_em"], "versao": pub["versao"], "motivo": pub["motivo"],
                         "parcial": pub["parcial"],
                         "objectivos": {o: {"pecas": e["pecas"], "lucro": e["total"]["lucro"],
                                            "lucro_1k": e["total"]["lucro_1k"], "golpes": e["total"]["golpes"],
                                            "dano": e["total"]["dano"]}
                                        for o, e in estrategias.items()}})
    pub["historico_estrategias"] = ler_hist("estrategias", 40)
    with m.lock:
        m.anterior = m.publicado
        m.publicado = pub
        m.versao_pub = pub["versao"]
        m.json_pub = None
    m.estado = "actualizada" if pub["parcial"]["lidas"] >= pub["parcial"]["escolhidas"] else "provisória"


def recursos(m):
    """V3.2: vida e fome mudam o plano (os golpes das barras): publica de novo, sem tabelas nem divisao."""
    p = m.publicado
    if not p or p.get("_versao_jogador") == E.fontes["jogador"].versao:
        return
    publicar(m, [t for t in p["_chaves"] if t in m.res])


def atualizar_progresso_jogador(m):
    """Publica resumos de batalha novos sem repetir a optimizacao exaustiva."""
    p = m.publicado
    if not p:
        return
    dados = _dados("dano_jogador") or {}
    fonte = E.fontes.get("dano_jogador")
    lido = agora_iso(fonte.lido_em) if fonte and fonte.lido_em else None
    mudou = False
    with m.lock:
        for r in p.get("rondas", {}).values():
            resumo = dados.get(r.get("batalha"))
            if r.get("dano_jogador_batalha") != resumo or r.get("dano_jogador_lido_em") != lido:
                r["dano_jogador_batalha"] = resumo
                r["dano_jogador_lido_em"] = lido
                mudou = True
        if mudou:
            m.versao_recursos += 1
            p["versao_recursos"] = m.versao_recursos
            m.json_pub = None


# ---------------------------------------------------------------------------
#  7. o servidor local
# ---------------------------------------------------------------------------

def estado_json():
    m = MOTOR
    tr = m.trabalho
    return {"agora": agora_iso(), "versao": m.versao_pub, "versao_recursos": m.versao_recursos,
            "jogador": {"id": UID, "nome": (_dados("jogador") or {}).get("username")},
            "pagina_aberta": time.time() - E.ultimo_cliente < SEM_PAGINA_S,
            "motor": {"estado": m.estado, "versao_base": m.versao_base,
                      "trabalho": ({"rondas": tr["rondas"], "feitas": tr["feitas"], "total": tr["total"],
                                    "conjuntos": tr["conjuntos"], "decorrido_s": time.time() - tr["inicio"]} if tr else None),
                      "ultimo_calculo": m.ultimo_calculo, "cancelados": m.cancelados, "erros": m.erros[-5:],
                      "base_s": (m.base or {}).get("segundos"),
                      "publicado_em": (m.publicado or {}).get("calculado_em")},
            "limitador": LIM.estado(),
            "materialidade": m.materialidade,
            "tempos": dict(m.tempos),
            "drops": {"versao": DROPS["versao"], "lidas": len(DROPS["linhas"]), "na_fila": len(DROPS["fila"]),
                      "volta": DROPS["volta"]},
            "precos_pendentes": ({"desde": agora_iso(m.precos_desde), "agrupadas": m.precos_agrupadas}
                                 if m.precos_desde else None),
            "fontes": [f.resumo() for f in sorted(list(E.fontes.values()), key=lambda f: (f.prio, f.nome))],
            "manuais": len(E.manuais)}


def semente():
    """Antes do primeiro calculo nao ha estrategia para mostrar (a pagina diz que esta a preparar)."""
    return None


SEMENTE = None


class Pagina(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _enviar(self, corpo: bytes, tipo="application/json; charset=utf-8", codigo=200):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/combate"):
            return self._enviar((AQUI / "pagina.html").read_bytes(), "text/html; charset=utf-8")
        self._enviar(**_kw(responder("GET", self.path, b"")))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._enviar(**_kw(responder("POST", self.path, self.rfile.read(n) if n else b"")))


def _kw(r):
    codigo, corpo = r
    return {"corpo": corpo, "codigo": codigo}


def _json(x, codigo=200):
    return codigo, json.dumps(x, default=str, separators=(",", ":")).encode()


def responder(metodo, caminho_completo, corpo=b""):
    """
    A API local, sem depender do transporte: o servidor HTTP chama-a, e no
    browser (warera/combate/navegador.py) o Web Worker chama exactamente a mesma.
    Devolve (codigo, corpo em bytes, JSON).
    """
    from urllib.parse import parse_qs, urlparse
    caminho = caminho_completo.split("?")[0]
    q = parse_qs(urlparse(caminho_completo).query)
    if metodo == "GET":
        if caminho == "/api/estado":
            E.ultimo_cliente = time.time()
            return _json(estado_json())
        if caminho == "/api/estrategia":
            E.ultimo_cliente = time.time()
            m = MOTOR
            with m.lock:
                if m.publicado:
                    if not m.json_pub:
                        m.json_pub = json.dumps({k: v for k, v in m.publicado.items() if not k.startswith("_")},
                                                default=str, separators=(",", ":")).encode()
                    return 200, m.json_pub
            return _json(SEMENTE)
        if caminho == "/api/drops":
            E.ultimo_cliente = time.time()
            return _json(drops_json())
        if caminho == "/api/item":
            code = (q.get("codigo") or [""])[0]
            itens = (_dados("config") or {}).get("items") or {}
            it = itens.get(code)
            if not it:
                return _json({"erro": "codigo desconhecido"}, 404)
            return _json({"codigo": code, "raridade": it.get("rarity"), "slot": B._slot_do_item(code, it),
                          "intervalos": pecas.intervalos(code, itens)})
        if caminho == "/api/catalogo":
            itens = (_dados("config") or {}).get("items") or {}
            cat = []
            for code, it in itens.items():
                slot = B._slot_do_item(code, it)
                if slot in M3.SLOTS and it.get("rarity") in O.RARIDADES_EPIC:
                    cat.append({"codigo": code, "slot": slot, "raridade": it.get("rarity"),
                                "intervalos": pecas.intervalos(code, itens)})
            return _json(cat)
        if caminho == "/api/jogador":
            u = _dados("jogador") or {}
            return _json({"id": UID, "nome": u.get("username"), "nivel": (u.get("leveling") or {}).get("level")})
        if caminho == "/api/jogadores":
            return _json(procurar_jogadores((q.get("nome") or [""])[0]))
        return _json({"erro": "nao existe"}, 404)
    if metodo == "POST":
        try:
            dados = json.loads(corpo or b"null")
        except ValueError:
            return _json({"erro": "json invalido"}, 400)
        if caminho == "/api/pecas":
            ok, recusadas = validar_manuais(dados if isinstance(dados, list) else [])
            if hash_de(ok) != hash_de(E.manuais):
                E.manuais = ok
                E.versao_manuais += 1
                E.evento.set()
            return _json({"aceites": ok, "recusadas": recusadas})
        if caminho == "/api/comida":
            c = (dados or {}).get("comida") if isinstance(dados, dict) else None
            comidas = (MOTOR.base or {}).get("P", {}).get("comidas") or ["bread", "steak", "cookedFish"]
            if c is not None and c not in comidas:
                return _json({"erro": "comida desconhecida", "comidas": comidas}, 400)
            if c != E.comida:
                E.comida = c
                E.evento.set()
            return _json({"comida": E.comida})
        if caminho == "/api/ordens":
            # V3.3: quanto do lucro se aceita perder para bater onde ha ordem do teu pais / MU
            t = (dados or {}).get("tolerancia") if isinstance(dados, dict) else None
            if not isinstance(t, (int, float)) or isinstance(t, bool) or not 0 <= t <= 0.5:
                return _json({"erro": "tolerancia entre 0 e 0,5", "opcoes": list(PREF_ORDENS_OPCOES)}, 400)
            if float(t) != E.pref_ordens:
                E.pref_ordens = float(t)
                E.evento.set()
            return _json({"tolerancia": E.pref_ordens})
        if caminho == "/api/jogador":
            uid = str((dados or {}).get("id") or "").strip()
            if not uid or len(uid) > 40 or not uid.isalnum():
                return _json({"erro": "id de jogador invalido"}, 400)
            trocar_jogador(uid)
            return _json({"id": UID})
    return _json({"erro": "nao existe"}, 404)


def procurar_jogadores(texto):
    """Os jogadores com esse nome (search.searchUsers), com o nome e o nivel de cada um (ate 6)."""
    texto = (texto or "").strip()
    if len(texto) < 2:
        return []
    ids = api.call("search.searchUsers", {"searchText": texto[:40]}) or []
    fora = []
    nomes = {c["_id"]: c.get("name") for c in (_dados("paises") or []) if isinstance(c, dict) and c.get("_id")}
    for uid in [i for i in ids if isinstance(i, str)][:6]:
        try:
            u = api.call("user.getUserById", {"userId": uid})
        except api.ErroAPI:
            continue
        fora.append({"id": uid, "nome": u.get("username"), "nivel": (u.get("leveling") or {}).get("level"),
                     "pais": nomes.get(u.get("country"))})
    return fora


def trocar_jogador(uid):
    """
    Outro jogador: tudo o que era dele recomeca (vida, equipamento, rondas lidas a contar com o dano dele,
    o motor, as pecas a mao). O que e do jogo fica: o historico dos fundos, dos precos, a validacao da regra
    e a aba Drops (sem as linhas «tu»).
    """
    global UID, MOTOR
    if uid == UID:
        return
    with E.lock:
        UID = uid
        E.fontes.clear()
        criar_fontes()
        E.leituras_ronda = {}
        E.rondas_escolhidas = []
        E.manuais = []
        E.versao_manuais += 1
        E.comida = None
        E.pref_ordens = PREF_ORDENS
        E.contratos = None
        for l in DROPS["linhas"].values():
            l["tu_ronda"] = l["tu_batalha"] = None
        DROPS["versao"] += 1
        MOTOR = Motor(0)
    E.evento.set()


def validar_manuais(lst):
    cfg = _dados("config") or {}
    itens = cfg.get("items") or {}
    ok, recusadas = [], []
    for p in lst if isinstance(lst, list) else []:
        code = p.get("codigo")
        it = itens.get(code) or {}
        slot = B._slot_do_item(code, it) if it else None
        faixas = pecas.intervalos(code, itens) if it else None
        if not it or it.get("rarity") not in O.RARIDADES_EPIC or slot not in M3.SLOTS or not faixas:
            recusadas.append({"peca": p, "porque": "só peças Epic, Legendary ou Mythic de um dos seis slots"})
            continue
        stats = {}
        for s, (lo, hi) in faixas.items():
            v = (p.get("stats") or {}).get(s)
            if not isinstance(v, (int, float)) or not lo <= v <= hi:
                recusadas.append({"peca": p, "porque": f"{s} tem de estar entre {lo} e {hi}"})
                stats = None
                break
            stats[s] = float(v)
        if stats is None:
            continue
        estado = p.get("estado")
        if not isinstance(estado, (int, float)) or not 1 <= estado <= 100:
            recusadas.append({"peca": p, "porque": "durabilidade entre 1 e 100"})
            continue
        ok.append({"id": str(p.get("id") or hash_de(p))[:12], "codigo": code, "slot": slot, "stats": stats,
                   "estado": estado})
    return ok, recusadas


def main():
    global LIM, MOTOR, SEMENTE
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=8792)
    ap.add_argument("--limite", type=int, default=45)
    ap.add_argument("--jogador", help="o id do jogador (por omissao WARERA_JOGADOR, ou BartolomeuCaixas)")
    a = ap.parse_args()
    global UID
    if a.jogador:
        UID = a.jogador
    LIM = Limitador(a.limite)
    urllib.request.urlopen = _urlopen_limitado
    criar_fontes()
    MOTOR = Motor(0)
    if CAMINHO_VALIDACAO.exists():
        try:
            VALIDACAO.update(json.loads(CAMINHO_VALIDACAO.read_text(encoding="utf-8")))
        except ValueError:
            pass
    SEMENTE = semente()
    threading.Thread(target=agendador, daemon=True).start()
    threading.Thread(target=ciclo_motor, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.porta), Pagina)
    print(f"assistente de combate: http://127.0.0.1:{a.porta}/combate · limite {a.limite}/min · "
          f"dados em {DADOS}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
