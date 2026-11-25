"""
Optimizador de dano e bounty: quantos golpes dar em cada ronda, com que
equipamento Epic+ (os seis slots cheios) e de que lado, para tirar o maximo
por cada 1000 de dano.

A REGRA DOS DROPS, VERIFICADA COM DADOS REAIS (19/11/2026)
---------------------------------------------------------

O loot do ranking nao e por lado e nao e sorte. Em 9 pools reais (rondas e
batalhas acabadas, dois pools vivos, e duas rondas acabadas com mais itens do
que jogadores), sem uma unica excepcao:

    `battleRanking.getRanking` com `side: "merged"`: uma linha por jogador,
    com o dano dos dois lados somado (685/685, 1225/1225, 2541/2541,
    3426/3426, 2197/2197 jogadores conferidos um a um)

    o pool tem floor(dano total / 200 000) itens na ronda e / 500 000 na
    batalha (`loot.damagePerLootItem`, `loot.battleLootDamagePerLootItem`)

    os itens vao do melhor para o pior, um por jogador, do rank 1 para baixo:
    zero inversoes de raridade, zero itens abaixo do corte. O `rank` do
    `poolLoot` no resumo de loot e o rank merged (#44 merged = #25 na defesa)

    com mais itens do que jogadores cada um leva exactamente um (o 1.o de
    duas rondas acabadas assim tem um so item dessa ronda), e sao os P
    melhores do pool de N que saem: as contagens de Epic e Legendary batem com
    isso (416 contra 421 esperados, 1366 contra 1431) e nao com "so se criam
    P" (138, 371). Para onde vao os que sobram nao se ve — e nao muda o que
    cada jogador recebe

    dentro de cada raridade a ordem segue o tipo e o roll: as armas no fim do
    bloco, os rolls melhores primeiro (20 excepcoes em 798 passos). Quem entra
    perto de um corte leva as piores pecas dessa raridade

O que e aleatorio e so a raridade de cada item no momento em que e criado (ao
longo da ronda, a cada 200 000 de dano): a variacao em janelas de 100 itens e
a de sorteios independentes. Por isso a raridade dos itens que AINDA nao
existem entra pela composicao medida (`composicao`), sempre com a amostra e o
intervalo, e nunca como certeza.

O QUE ESTE MODULO NAO FAZ
-------------------------

Nao tem formula de combate. O golpe, os golpes por hora e o custo de cada
golpe saem do motor pelas duas metades de `bounty` (arma/luvas/capacete e
peito/calcas/botas), e as builds escolhidas voltam a passar por
`bounty.avaliar` — a diferenca vem na resposta. Os cortes usam `find_cutoffs`
e o fim da ronda `estimate_round_seconds`, do script de drops
(`scripts/warera_drops.py`). O preco por roll e o modelo de `pecas`.
"""

from __future__ import annotations

import bisect
import importlib.util
import json
import math
import statistics
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import api, bounty, config, memoria, pecas

RARIDADES_EPIC = ("epic", "legendary", "mythic")
NOME = {1: "Common", 2: "Uncommon", 3: "Rare", 4: "Epic", 5: "Legendary", 6: "Mythic"}
TIERS = (6, 5, 4, 3, 2, 1)
CENARIOS = ("optimista", "esperado", "conservador")
CONTEXTOS = ("agora",) + CENARIOS
POOLS = ("ronda", "batalha")
DIR = config.RAIZ / "saida" / "optimizador"
MAX_FOTOS = 30
# quem entrou na ronda nos ultimos 30 minutos: e a rajada de quem chega
JANELA_CHEGADOS = 1800.0
# o ritmo de cada jogador desde o 1.o golpe nunca se divide por menos disto,
# para quem acabou de entrar nao parecer que vai bater assim horas a fio
MIN_RITMO_S = 1200.0
RONDAS_POR_OMISSAO = 4
Z90 = 1.6449

# A amostra medida a 19/11/2026, pool a pool: N itens, P jogadores, e a
# contagem por tier do que se via. Entra na composicao com os pools que o
# modulo for guardando, sem contar duas vezes o mesmo.
AMOSTRA_INICIAL = [
    {"pool": "round:6afc1e228771d2aacb4f4293", "N": 1323, "P": 2541,
     "c": {6: 5, 5: 28, 4: 215, 3: 406, 2: 669}},
    {"pool": "battle:6afbb573c5da48c0faddf347", "N": 731, "P": 3426,
     "c": {6: 3, 5: 17, 4: 116, 3: 250, 2: 345}},
    {"pool": "round:6aec0cd4a227444e538e00ea", "N": 127, "P": 1225,
     "c": {5: 3, 4: 25, 3: 38, 2: 61}},
    {"pool": "round:6afd33299ce4543e4d0c8cee", "N": 12, "P": 254, "c": {4: 1, 3: 3, 2: 8}},
    {"pool": "round:6afd67f5a9edf4c032fe2ade", "N": 5, "P": 123, "c": {4: 1, 3: 4}},
    {"pool": "round:6afb013f53198741889457a2", "N": 2585, "P": 846,
     "c": {6: 16, 5: 62, 4: 416, 3: 352}},
    {"pool": "round:6afd94aa975d361e82f2349a", "N": 8783, "P": 2275,
     "c": {6: 52, 5: 224, 4: 1366, 3: 633}},
]

LIMITACOES = [
    "Itens a mais do que jogadores: confirmado em duas rondas acabadas que cada jogador "
    "leva um só e que saem os P melhores do pool; para onde vão os outros não se vê "
    "(não muda o que cada um recebe).",
    "A raridade dos itens que ainda não foram criados é a composição medida (amostra e "
    "intervalo a 90% à vista), não uma certeza: só os itens que já existem têm raridade fixa.",
    "O fim da ronda é o de `estimate_round_seconds`: o MÍNIMO, se o lado que vai à frente "
    "ganhar todos os ticks. Uma ronda que vire dura mais, e os outros batem mais.",
    "O dano dos concorrentes é uma projecção: ritmo medido (fotografias guardadas e média "
    "desde o início da ronda), repartido pelo ritmo de cada um desde o 1.º golpe, e a "
    "rajada real de quem acabou de entrar. O erro medido está no backtest.",
    "O valor de cada peça é uma ESTIMATIVA: a média de vendas por código "
    "(`gameStat.getEquipmentAvgByCode`, não documentado), sem roll, sem data e sem amostra. "
    "O roll só entraria com vendas por roll das últimas 48 horas, que exigem as transacções "
    "(token): sem elas, nenhum roll recebe preço — o sistema não inventa o valor de um roll.",
    "A posição dentro de um bloco de raridade é levada para a peça que está nessa posição "
    "no pool de agora (o código dela, arma ou peça); os itens que vão nascer podem mudar a "
    "mistura.",
    "Os golpes são dados agora, com o buff que tens agora; os golpes por hora são o regime "
    "estacionário do motor (vida e fome a regenerar).",
    "O bounty é o do lado onde bates, cortado pelo fundo inteiro — o fundo é partilhado por "
    "todos os que batem desse lado, por isso é um tecto e não uma reserva tua.",
]


# ---------------------------------------------------------------------------
#  0. O script de drops, reutilizado
# ---------------------------------------------------------------------------

_DROPS = None


def drops():
    """`scripts/warera_drops.py` carregado como modulo: so as funcoes puras."""
    global _DROPS
    if _DROPS is None:
        import sys
        m = sys.modules.get("warera_drops")
        if m is None:
            caminho = config.RAIZ / "scripts" / "warera_drops.py"
            spec = importlib.util.spec_from_file_location("warera_drops", caminho)
            m = importlib.util.module_from_spec(spec)
            # a dataclass do script procura o proprio modulo em sys.modules
            sys.modules["warera_drops"] = m
            spec.loader.exec_module(m)
        _DROPS = m
    return _DROPS


def tier(code: str | None) -> int | None:
    return drops().item_tier(code) if code else None


def _ts(valor) -> float | None:
    if not isinstance(valor, str):
        return None
    try:
        return datetime.fromisoformat(valor.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _ler(caminho, defeito):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return defeito


def _gravar(caminho, valor) -> None:
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_text(json.dumps(valor, separators=(",", ":")), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
#  1. O ranking merged
# ---------------------------------------------------------------------------


def _compacta(r: dict) -> dict:
    li = r.get("lootItem") or {}
    return {"user": r.get("user"), "value": float(r.get("value") or 0.0), "rank": r.get("rank"),
            "criado": _ts(r.get("createdAt")), "actualizado": _ts(r.get("updatedAt")),
            "item": li.get("code"), "skills": li.get("skills"), "item_id": li.get("_id")}


# quanto demorou a ultima leitura de cada ranking: num pool vivo, o dano que
# entra enquanto as paginas se leem e a folga honesta na contagem de itens
DURACAO_LEITURA: dict[str, float] = {}


def ranking_merged(scope: dict, vivo: bool, paginas: int = 60) -> list[dict]:
    """
    O ranking dos dois lados numa lista so, como o jogo o usa para o loot.

    Uma ronda ou batalha acabada nunca muda: fica guardada em `saida/` e nao
    se volta a pedir. A viva le-se outra vez ao fim de quatro minutos (a vista
    rele de cinco em cinco).
    """
    chave = ("round_" + scope["roundId"]) if "roundId" in scope else ("battle_" + scope["battleId"])
    caminho = DIR / "rankings" / f"{chave}.json"
    if not vivo:
        guardado = _ler(caminho, None)
        if guardado:
            return guardado

    def ler():
        t0 = time.time()
        rows, cursor = [], None
        for _ in range(paginas):
            p = dict(scope, dataType="damage", type="user", side="merged", limit=100)
            if cursor:
                p["cursor"] = cursor
            d = api.call("battleRanking.getRanking", p)
            rows.extend(_compacta(r) for r in d.get("items") or [])
            cursor = d.get("nextCursor")
            if not cursor:
                break
        DURACAO_LEITURA[chave] = time.time() - t0
        return rows

    rows = api.cached("opt:rk:" + chave, 240 if vivo else 3600, ler)
    if not vivo and rows:
        _gravar(caminho, rows)
    return rows


def jogadores_merged(round_id: str) -> int | None:
    """Quantos jogadores tem o merged de uma ronda: um pedido, `itemCount`."""
    def ler():
        try:
            return api.call("battleRanking.getRanking",
                            {"roundId": round_id, "dataType": "damage", "type": "user",
                             "side": "merged", "limit": 1}).get("itemCount")
        except api.ErroAPI:
            return None
    return api.cached("opt:P:" + round_id, 120, ler)


def verificar_regra(rows: list[dict], por_item: float | None = None, vivo: bool = True,
                    folga_itens: int | None = None) -> dict:
    """
    A regra conferida no pool que se acabou de ler, e nao so na amostra antiga.

    Inversoes (uma raridade acima de outra mais abaixo) e itens abaixo do corte
    tem de ser zero, sempre. O numero de itens tem de ser min(jogadores,
    dano/divisor): exacto num pool acabado; num vivo as paginas leem-se uma a
    uma enquanto o ranking mexe, e a folga e o dano que entra nesse tempo
    (`folga_itens`, do ritmo medido e da duracao da leitura), com um minimo de 1.
    """
    ordem = sorted(rows, key=lambda r: -r["value"])
    tiers = [tier(r.get("item")) for r in ordem]
    com = [t for t in tiers if t]
    inversoes = sum(1 for a, b in zip(tiers, tiers[1:]) if a and b and b > a)
    fim = next((i for i, t in enumerate(tiers) if not t), None)
    abaixo = sum(1 for t in tiers[fim:] if t) if fim is not None else 0
    total = sum(r["value"] for r in rows)
    esperado = min(len(rows), int(total // por_item)) if por_item else None
    folga = max(1, int(folga_itens or 0)) if vivo else 0
    return {
        "jogadores": len(rows), "itens": len(com), "itens_esperados": esperado,
        "folga": folga, "vivo": vivo,
        "dano_total": total, "inversoes": inversoes, "itens_abaixo_do_corte": abaixo,
        "confere": (inversoes == 0 and abaixo == 0
                    and (esperado is None or abs(len(com) - esperado) <= folga)),
    }


# ---------------------------------------------------------------------------
#  2. A composicao medida dos itens novos
# ---------------------------------------------------------------------------


def _wilson(x: float, n: float, z: float = Z90) -> tuple[float | None, float | None]:
    if not n:
        return None, None
    p = x / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n)))
    return max(0.0, (c - r) / d), min(1.0, (c + r) / d)


def contagem_de_pool(rows: list[dict], N: int) -> dict:
    return {"N": int(N), "P": len(rows),
            "c": dict(Counter(t for t in (tier(r.get("item")) for r in rows) if t))}


def composicao(pools: list[dict]) -> dict:
    """
    A probabilidade de um item novo ser de cada raridade, medida.

    Estima-se q_k = P(raridade >= k) com os pools onde o tier k estava TODO a
    vista: num pool com N <= P ve-se tudo; com N > P so se veem os P melhores,
    e um tier esta todo a vista enquanto a contagem acumulada ate ele fica
    abaixo de P. Cada raridade traz a sua amostra (os itens que a mediram) e
    o intervalo de Wilson a 90%.
    """
    q = {7: 0.0}
    exacto = {}
    for k in (6, 5, 4, 3, 2, 1):
        num = den = ex = 0
        for p in pools:
            N, P, c = int(p["N"]), int(p["P"]), {int(t): v for t, v in (p.get("c") or {}).items()}
            if N <= 0:
                continue
            acum = sum(v for t, v in c.items() if t >= k)
            if N <= P or acum < P:
                num += acum
                den += N
                ex += c.get(k, 0)
        q[k] = num / den if den else 0.0
        exacto[k] = (ex, den)
    fora = {}
    for k in (6, 5, 4, 3, 2, 1):
        ex, den = exacto[k]
        lo, hi = _wilson(ex, den)
        fora[k] = {"p": max(0.0, q[k] - q[k + 1]), "p_lo": lo or 0.0, "p_hi": hi or 0.0,
                   "q": q[k], "itens": ex, "n": den, "nome": NOME[k]}
    return {"por_tier": fora, "pools": len(pools),
            "itens_medidos": sum(int(p["N"]) for p in pools)}


def pools_conhecidos() -> list[dict]:
    """A amostra inicial mais os pools acabados que o modulo foi lendo."""
    guardados = _ler(DIR / "pools.json", {})
    vistos = {p["pool"] for p in AMOSTRA_INICIAL}
    extra = [dict(v, pool=k) for k, v in guardados.items() if k not in vistos]
    return AMOSTRA_INICIAL + extra


_LOCK_POOLS = threading.Lock()


def guardar_pool(chave: str, rows: list[dict], N: int) -> None:
    # as rondas analisam-se em paralelo: ler-mudar-escrever num ficheiro so
    with _LOCK_POOLS:
        d = _ler(DIR / "pools.json", {})
        if chave not in d and rows:
            d[chave] = contagem_de_pool(rows, N)
            _gravar(DIR / "pools.json", d)


# ---------------------------------------------------------------------------
#  3. O valor de cada peca, pelo roll
# ---------------------------------------------------------------------------


def vendas_com_roll() -> tuple[dict, dict]:
    """
    As vendas com roll das ultimas 48 horas (`memoria.vendas_recentes`), por
    codigo — e so essas. As mais antigas sao contadas e ditas, nunca usadas:
    uma venda de ha um mes e historia, nao o preco de agora.
    """
    r = memoria.vendas_recentes()
    vendas = {c: [(p, {k: float(x) for k, x in (sk or {}).items()}, em) for p, sk, em in linhas]
              for c, linhas in (r.get("vendas") or {}).items()}
    return vendas, {k: v for k, v in r.items() if k != "vendas"}


class Valores:
    """
    Quanto vale vender uma peca: a media de hoje do codigo, ajustada pelo roll.

    A media (`gameStat.getEquipmentAvgByCode`) da o nivel de preco de agora; o
    modelo por roll de `pecas` da a inclinacao — quanto mais vale um roll
    melhor do que o roll medio das vendas. So entra quando o modelo e fiavel
    (`pecas.modelo_de_preco`: amostra, correlacao e cobertura); sem ele vale a
    media do codigo, e a resposta diz qual das duas foi.
    """

    def __init__(self, itens: dict, precos_eq: dict, vendas: dict, taxa: float,
                 peso_arma: float):
        self.itens = itens or {}
        self.precos = precos_eq or {}
        self.taxa = float(taxa)
        self.peso_arma = float(peso_arma)
        self.modelo = pecas.modelo_de_preco(vendas, self.itens) if vendas else {}
        # so ha preco por roll com vendas recentes e um modelo fiavel; sem isso
        # nenhum roll recebe preco (nao se inventa) e o valor e a media do codigo
        self.modelo_activo = any(m.get("fiavel") for m in self.modelo.values())
        self.x_medio = {}
        for code, linhas in (vendas or {}).items():
            faixas = pecas.intervalos(code, self.itens)
            xs = [sum(float(v) for k, v in sk.items() if k in faixas) for _, sk, *_ in linhas]
            xs = [x for x in xs if x > 0]
            if xs:
                self.x_medio[code] = sum(xs) / len(xs)
        self.por_tier: dict[int, list[str]] = {}
        for code, it in self.itens.items():
            if it.get("type") in ("weapon", "equipment"):
                t = tier(code)
                if t:
                    self.por_tier.setdefault(t, []).append(code)

    def item(self, code: str | None, skills: dict | None = None) -> tuple[float | None, str]:
        media = self.precos.get(code) if code else None
        if not media:
            return None, "sem preço"
        m = self.modelo.get(code)
        if skills and m and m.get("fiavel") and self.x_medio.get(code):
            pj = pecas.preco_justo(code, skills, self.modelo, self.itens)
            ref = pecas._prever(m["declive"], m["ordenada"],
                                (m["limite_min"], m["limite_max"]))(self.x_medio[code])
            if pj and pj.get("justo") and ref:
                return (float(media) * pj["justo"] / ref * (1 - self.taxa),
                        f"estimativa pelo roll ({m['n']} vendas das últimas 48 h, "
                        f"r={m['correlacao']:.2f})")
        return float(media) * (1 - self.taxa), "estimativa: média do código (gameStat), sem roll"

    def novo(self, t: int) -> float:
        """Um item desta raridade que ainda nao existe: roll desconhecido, media do codigo."""
        codes = self.por_tier.get(t) or []
        armas = [c for c in codes if (self.itens.get(c) or {}).get("type") == "weapon"]
        pecas_ = [c for c in codes if c not in armas]
        va = [self.item(c)[0] for c in armas]
        vp = [self.item(c)[0] for c in pecas_]
        va = [v for v in va if v]
        vp = [v for v in vp if v]
        if not va and not vp:
            return 0.0
        a = sum(va) / len(va) if va else 0.0
        p = sum(vp) / len(vp) if vp else 0.0
        if not va:
            return p
        if not vp:
            return a
        return self.peso_arma * a + (1 - self.peso_arma) * p


def blocos_de_valor(rows: list[dict], val: Valores) -> dict[int, list[float]]:
    """Por raridade, o valor de cada peca do pool de agora, pela ordem do ranking."""
    fora: dict[int, list[float]] = {}
    for r in sorted(rows, key=lambda r: -r["value"]):
        t = tier(r.get("item"))
        if not t:
            continue
        v, _ = val.item(r["item"], r.get("skills"))
        if v is not None:
            fora.setdefault(t, []).append(v)
    return fora


# ---------------------------------------------------------------------------
#  4. O pool no fim da ronda, e a posicao que um dano da
# ---------------------------------------------------------------------------


def contagem_final(vistos: dict, N_agora: int, N_final: int, comp: dict) -> dict[int, float]:
    """
    Quantos itens de cada raridade ha no fim.

    Os que se veem agora tem raridade fixa. Os ja criados mas abaixo do ultimo
    jogador (quando ha mais itens do que jogadores) so podem ser da raridade
    mais baixa que se ve ou de baixo; os que vao nascer seguem a composicao.
    """
    p = {k: comp["por_tier"][k]["p"] for k in TIERS}
    cnt = {k: float(vistos.get(k, 0)) for k in TIERS}
    n_vistos = sum(vistos.values())
    escondidos = max(0, N_agora - n_vistos)
    if escondidos:
        piso = min(vistos) if vistos else 6
        baixos = [k for k in TIERS if k <= piso]
        soma = sum(p[k] for k in baixos) or 1.0
        for k in baixos:
            cnt[k] += escondidos * p[k] / soma
    novos = max(0, N_final - N_agora)
    for k in TIERS:
        cnt[k] += novos * p[k]
    return cnt


def cortes_por_rank(cnt: dict[int, float], N_final: int, P_final: int) -> dict[int, int]:
    """O ultimo rank que leva cada raridade ou melhor."""
    fora, acum = {}, 0.0
    teto = min(int(N_final), int(P_final))
    for k in TIERS:
        acum += cnt.get(k, 0.0)
        fora[k] = int(min(math.floor(acum + 1e-9), teto))
    return fora


def tier_no_rank(rank: int, cortes: dict[int, int]) -> int | None:
    for k in TIERS:
        if rank <= cortes.get(k, 0):
            return k
    return None


def dano_para_rank(rank: int, proj: list[float]) -> float | None:
    """Para acabar em `rank` tem de se passar quem la estiver no fim."""
    if rank <= 0:
        return None
    return proj[rank - 1] + 1.0 if rank - 1 < len(proj) else 1.0


def posicao(d: float, neg: list[float]) -> int:
    """1 + quantos concorrentes acabam com pelo menos `d` (`neg` e -proj, crescente)."""
    return 1 + bisect.bisect_right(neg, -d)


def valor_na_posicao(rank: int, cortes: dict[int, int], blocos: dict[int, list[float]],
                     val: Valores) -> tuple[int | None, float, str]:
    """
    A raridade nesse rank e o valor da peca que la esta.

    Dentro de cada raridade o pool vai do melhor tipo e roll para o pior. A
    fraccao do bloco onde se cai leva-se para a peca na mesma fraccao do bloco
    de agora; se a raridade ainda nao tem pecas a vista, vale um item novo.
    """
    k = tier_no_rank(rank, cortes)
    if k is None:
        return None, 0.0, "fora do pool"
    acima = max([cortes[j] for j in TIERS if j > k], default=0)
    tamanho = max(1, cortes[k] - acima)
    f = min(0.999, max(0.0, (rank - acima - 1) / tamanho))
    bloco = blocos.get(k) or []
    if len(bloco) >= 3:
        return k, bloco[int(f * len(bloco))], ("a peça nessa posição do pool de agora "
                                               "(estimativa pela média do código, sem roll)"
                                               if not val.modelo_activo else
                                               "a peça nessa posição do pool de agora (roll)")
    return k, val.novo(k), "item ainda não criado (média do código)"


# ---------------------------------------------------------------------------
#  5. A concorrencia: ritmo medido e projeccao
# ---------------------------------------------------------------------------


def _fotos_caminho(rid: str):
    return DIR / "fotos" / f"{rid}.json"


def guardar_foto(rid: str, rows: list[dict], t: float) -> list[dict]:
    fotos = _ler(_fotos_caminho(rid), [])
    if not fotos or t - fotos[-1]["t"] >= 60:
        fotos.append({"t": t, "total": sum(r["value"] for r in rows), "jogadores": len(rows),
                      "v": {r["user"]: r["value"] for r in rows if r.get("user")}})
        fotos = fotos[-MAX_FOTOS:]
        _gravar(_fotos_caminho(rid), fotos)
    return fotos


JANELA_HISTORIA = 600.0


def _oid_ts(oid: str | None) -> float | None:
    try:
        return float(int(str(oid)[:8], 16))
    except (TypeError, ValueError):
        return None


def historia_da_ronda(rows: list[dict], t: float, por_item: float,
                      janela: float = JANELA_HISTORIA) -> dict:
    """
    O ritmo da ronda inteira, de uma so leitura.

    Cada item nasce a cada `por_item` de dano, e o `_id` diz quando. A raridade
    e a posicao no ranking nao dependem da hora em que o item nasceu
    (correlacao rank~criacao entre -0,16 e +0,15 em 8 blocos de pools acabados),
    por isso os itens que se veem sao uma amostra sem vies no tempo, mesmo
    quando ha mais itens do que jogadores: contam-se por janela de 10 minutos e
    escalam-se por itens / visiveis. A entrada de jogadores le-se do
    `createdAt` de cada linha (o 1.o golpe de cada um), que esta completo.
    """
    total = sum(r["value"] for r in rows)
    N = int(total // por_item)
    vis = [x for x in (_oid_ts(r.get("item_id")) for r in rows if r.get("item")) if x]
    criados = [r["criado"] for r in rows if r.get("criado")]
    fora = {"dano_por_s": [], "jogadores_por_s": [], "janela_s": janela}
    if vis and N > 0:
        escala = N / len(vis)
        t0 = min(vis)
        cont = Counter(int((x - t0) // janela) for x in vis)
        completas = int((t - t0) // janela)
        fora["dano_por_s"] = [cont.get(i, 0) * escala * por_item / janela for i in range(completas)]
    if criados:
        t0 = min(criados)
        cont = Counter(int((x - t0) // janela) for x in criados)
        completas = int((t - t0) // janela)
        fora["jogadores_por_s"] = [cont.get(i, 0) / janela for i in range(completas)]
    return fora


def ritmos(fotos: list[dict], rows: list[dict], t: float, inicio: float | None,
           por_item: float = 200000.0) -> dict:
    """
    O ritmo da ronda, medido de duas maneiras que se confirmam: as janelas
    entre as fotografias que o modulo guarda, e a historia da ronda inteira
    pela hora de criacao dos itens (`historia_da_ronda`). Mais a media desde o
    inicio e quantos jogadores entram por segundo.

        optimista     o ritmo mais baixo medido (os outros abrandam)
        esperado      o ritmo recente: a fotografia de ha menos de 30 min, ou
                      as ultimas tres janelas da historia
        conservador   o ritmo mais alto medido (os outros carregam)
    """
    total = sum(r["value"] for r in rows)
    pontos = sorted({(f["t"], f["total"], f["jogadores"]) for f in fotos if f["t"] < t - 1}
                    | {(t, total, len(rows))})
    jan, jog = [], []
    for (t0, d0, p0), (t1, d1, p1) in zip(pontos, pontos[1:]):
        if t1 - t0 >= 60:
            jan.append(max(0.0, (d1 - d0) / (t1 - t0)))
            jog.append(max(0.0, (p1 - p0) / (t1 - t0)))
    hist = historia_da_ronda(rows, t, por_item)
    criados = [r["criado"] for r in rows if r.get("criado")]
    inicio = inicio or (min(criados) if criados else None)
    media = total / max(60.0, t - inicio) if inicio else None
    recentes = [r for r in rows if r.get("criado") and r["criado"] > t - JANELA_CHEGADOS]
    j_criado = len(recentes) / JANELA_CHEGADOS
    antiga = next(((ft, fd) for ft, fd, _ in pontos if t - ft <= 1800 and t - ft >= 120), None)
    if antiga:
        recente, fonte = (total - antiga[1]) / (t - antiga[0]), "fotografia guardada"
    elif hist["dano_por_s"]:
        ult = hist["dano_por_s"][-3:]
        recente, fonte = sum(ult) / len(ult), "últimas janelas da história da ronda"
    else:
        recente, fonte = media, "média desde o início"
    rs = jan + hist["dano_por_s"] + [x for x in (media,) if x is not None]
    js = jog + hist["jogadores_por_s"] + [j_criado]
    rec = recente or 0.0
    return {
        "janelas": jan, "jogadores_por_s": jog, "media_desde_inicio": media,
        "historia": hist, "recente": recente, "fonte_recente": fonte,
        "entrada_por_createdAt": j_criado, "fotografias": len(pontos) - 1,
        "R": {"optimista": min(rs + [rec]) if rs else rec,
              "esperado": rec,
              "conservador": max(rs + [rec]) if rs else rec},
        "J": {"optimista": min(js), "esperado": j_criado, "conservador": max(js)},
        "rajadas": sorted((r["value"] for r in recentes), reverse=True),
    }


def pesos(rows: list[dict], t: float) -> dict[str, float]:
    """O ritmo de cada um desde o 1.o golpe na ronda (com piso de 20 min)."""
    fora = {}
    for r in rows:
        u = r.get("user")
        if not u:
            continue
        c = r.get("criado") or (t - MIN_RITMO_S)
        fora[u] = r["value"] / max(MIN_RITMO_S, t - c)
    return fora


def _quantis(amostra: list[float], n: int) -> list[float]:
    if n <= 0 or not amostra:
        return []
    return [amostra[min(len(amostra) - 1, int((i + 0.5) / n * len(amostra)))] for i in range(n)]


def novos_jogadores(n_novos: float, extra: float, rajadas: list[float],
                    forma: list[float] | None = None) -> list[float]:
    """
    O dano de quem ainda vai entrar: a rajada real de quem entrou na ultima
    meia hora (ou, sem cinco casos, a forma da distribuicao de agora), nunca
    mais do que o dano que o cenario acrescenta.
    """
    n = int(round(max(0.0, n_novos)))
    base = rajadas if len(rajadas) >= 5 else (forma or [])
    novos = _quantis(base, n)
    if novos and sum(novos) > extra:
        f = extra / sum(novos) if extra > 0 else 0.0
        novos = [x * f for x in novos]
    return novos


def projectar(valores: list[tuple[str | None, float]], extra: float, novos: list[float],
              w: dict[str, float]) -> list[float]:
    """
    O dano de cada concorrente no fim, do maior para o menor.

    O dano acrescentado que nao e de quem entra reparte-se pelos que ja estao,
    pelo ritmo de cada um nesta ronda. O total conserva-se.
    """
    resto = max(0.0, extra - sum(novos))
    s = sum(w.values()) or 1.0
    finais = [v + resto * w.get(u, 0.0) / s for u, v in valores] + list(novos)
    return sorted(finais, reverse=True)


def backtest(fotos: list[dict], rows: list[dict], t: float, por_item: float) -> dict | None:
    """
    O modelo de concorrencia posto a prever uma fotografia que ja se conhece.

    B e a leitura de agora; A a fotografia de ha pelo menos cinco minutos; A0
    a anterior a A, para o ritmo. Preve-se B a partir de A com o ritmo de A0
    a A (como a leitura real faria) e mede-se o erro no dano de cada corte de
    raridade de B. E o numero que diz quanto confiar na projeccao.
    """
    if len(fotos) < 2:
        return None
    A = next((f for f in reversed(fotos) if f["t"] <= t - 300), None)
    if not A:
        return None
    A0 = next((f for f in reversed(fotos) if f["t"] <= A["t"] - 120), None)
    if not A0:
        return None
    criado = {r["user"]: r.get("criado") for r in rows}
    linhas_A = [{"user": u, "value": v, "criado": criado.get(u)} for u, v in A["v"].items()]
    T = t - A["t"]
    R = (A["total"] - A0["total"]) / (A["t"] - A0["t"])
    J = (A["jogadores"] - A0["jogadores"]) / (A["t"] - A0["t"])
    chegados = sorted((v for u, v in A["v"].items() if u not in A0["v"]), reverse=True)
    forma = sorted(A["v"].values(), reverse=True)
    real = sorted((r["value"] for r in rows), reverse=True)
    total_real = sum(real) - A["total"]
    novos_real = len(real) - A["jogadores"]
    w = pesos(linhas_A, A["t"])
    base_A = [(u, v) for u, v in A["v"].items()]
    # duas perguntas separadas: acertou no ritmo total? e, dado o total real,
    # acertou em como ele se reparte (que e o que move os cortes)?
    proj = projectar(base_A, R * T, novos_jogadores(J * T, R * T, chegados, forma), w)
    proj_rep = projectar(base_A, total_real, novos_jogadores(novos_real, total_real, chegados, forma), w)
    vistos = Counter(t_ for t_ in (tier(r.get("item")) for r in rows) if t_)
    acum, erros = 0, []
    for k in TIERS:
        acum += vistos.get(k, 0)
        # o ultimo jogador nao e um corte: com mais itens do que jogadores todos levam
        if vistos.get(k) and 0 < acum < len(real) and acum <= min(len(proj), len(proj_rep)):
            r_ = real[acum - 1]
            erros.append({"tier": k, "nome": NOME[k], "rank": acum, "real": r_,
                          "previsto": proj[acum - 1], "previsto_com_total_real": proj_rep[acum - 1],
                          "erro_pct": (proj[acum - 1] / r_ - 1) * 100 if r_ else None,
                          "erro_reparticao_pct": (proj_rep[acum - 1] / r_ - 1) * 100 if r_ else None})

    def media(campo):
        v = [abs(e[campo]) for e in erros if e[campo] is not None]
        return statistics.mean(v) if v else None
    return {
        "de": A["t"], "ate": t, "minutos": T / 60.0,
        "dano_previsto": R * T, "dano_real": total_real,
        "erro_total_pct": (R * T / total_real - 1) * 100 if total_real else None,
        "jogadores_previstos": J * T, "jogadores_reais": novos_real,
        "cortes": erros,
        "erro_medio_pct": media("erro_pct"),
        "erro_medio_reparticao_pct": media("erro_reparticao_pct"),
    }


# ---------------------------------------------------------------------------
#  6. As builds Epic+, seis slots, pelo motor
# ---------------------------------------------------------------------------


def opcoes_epic(op: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """So pecas Epic, Legendary e Mythic, em todos os slots."""
    return {s: [o for o in (op.get(s) or []) if o.get("raridade") in RARIDADES_EPIC]
            for s in bounty.SLOTS}


def builds_epic(base: dict, op: dict[str, list[dict]], ctx1: dict, cfg: dict) -> dict:
    """
    Todas as builds com os seis slots cheios de Epic+, de cada municao e
    comida, pelo motor a x1 (o dano e linear no factor de batalha — ver
    `bounty.metades`).

    O custo de um golpe e a soma do que o motor devolveu em cada metade, como
    em `bounty.procurar`: municao + desgaste da arma + comida + desgaste das
    outras cinco pecas nos golpes que nao se esquivam.
    """
    from . import simulador

    op6 = opcoes_epic(op)
    f_dano = [c for c in bounty.frente(op6, bounty.GRUPO_DANO, bounty.STATS_DANO, True)
              if set(c["pecas"]) == set(bounty.GRUPO_DANO)]
    f_def = [c for c in bounty.frente(op6, bounty.GRUPO_DEFESA, bounty.STATS_DEFESA, False)
             if set(c["pecas"]) == set(bounty.GRUPO_DEFESA)]
    md = bounty._metade_dano(base, f_dano, ctx1)
    mf = bounty._metade_defesa(base, f_def, ctx1)
    for f in mf:
        st = dict(base)
        for s, v in f["combo"]["stats"].items():
            st[s] = float(st.get(s) or 0.0) + v
        r = simulador.dano_disponivel(st, cfg, comida_vida_pct=ctx1["vida_comida"][f["comida"]],
                                      posto_pct=ctx1["posto_pct"], batalha=1.0)
        f["_vida_golpe"] = float(r.get("vida_por_golpe_esperada") or 0.0)
        f["_vida_max"] = float(r.get("vida_maxima") or 0.0)
        f["_vida_comida"] = f["_vida_max"] * float(ctx1["vida_comida"][f["comida"]] or 0) / 100.0

    todas = []
    for d in md:
        cd = d["combo"]
        for f in mf:
            cf = f["combo"]
            custo = (d["c_mun"] + cd["liq_arma"] / 100.0 + f["c_comida"]
                     + (cf["liq_resto"] + cd["liq_resto"]) / 100.0 * f["poupa"])
            pecas_ = {**cd["pecas"], **cf["pecas"]}
            todas.append({
                "pecas": pecas_, "municao": d["municao"], "comida": f["comida"],
                "dpg1": float(d["dpg"]), "custo_golpe": custo, "golpes_hora": f["golpes"] / 24.0,
                "capex": cd["capex"] + cf["capex"],
                "vida_golpe": f["_vida_golpe"], "vida_comida": f["_vida_comida"],
                "tens": all(p["origem"] == "tens" for p in pecas_.values()),
            })
    # a fronteira: mais dano por golpe, menos custo, mais golpes por hora, menos compras
    todas.sort(key=lambda b: (b["custo_golpe"], -b["dpg1"]))
    fr: list[dict] = []
    for b in todas:
        if not any(o["dpg1"] >= b["dpg1"] and o["custo_golpe"] <= b["custo_golpe"]
                   and o["golpes_hora"] >= b["golpes_hora"] and o["capex"] <= b["capex"]
                   for o in fr):
            fr.append(b)
    tens = [b for b in todas if b["tens"]]
    return {"fronteira": fr, "tens": tens, "avaliadas": len(todas),
            "combos_dano": len(f_dano), "combos_defesa": len(f_def)}


def nome_build(b: dict) -> str:
    ordem = ("weapon", "helmet", "chest", "pants", "gloves", "boots")
    return " · ".join(b["pecas"][s]["codigo"] for s in ordem if s in b["pecas"])


# ---------------------------------------------------------------------------
#  7. O planeador por ronda
# ---------------------------------------------------------------------------


def _resultado(d: float, cen: dict, val: Valores) -> dict:
    fora = {}
    for pool in POOLS:
        x = cen.get(pool)
        if not x:
            continue
        r = posicao(d, x["neg"])
        k, v, fonte = valor_na_posicao(r, x["cortes"], x["blocos"], val)
        fora[pool] = {"rank": r, "tier": k, "nome": NOME.get(k) if k else None,
                      "valor": v, "fonte": fonte}
    return fora


def planear(contextos: dict, lados: list[dict], builds: list[dict], val: Valores,
            caso_golpe: float, golpes_agora_de, segundos: float | None,
            buff: float) -> dict:
    """
    Para cada lado e build: quantos golpes, e o que rendem em cada cenario.

    Os golpes que interessam sao poucos: 1, os que chegam a cada corte de
    raridade (em cada pool e cenario), e todos os que a vida deixa ate ao fim
    da ronda. Entre dois cortes, um golpe a mais so traz cases e bounty, e
    custa o golpe.

    A escolha e ROBUSTA: o maior retorno por 1000 de dano no PIOR dos tres
    cenarios, e so entre os golpes que dao lucro em todos. Nao e o cenario
    "conservador" sozinho: aqui mais dano dos concorrentes cria mais itens, e
    esse cenario chega a ser o melhor para quem entra no fundo. O maior dano
    nao e o criterio; o maior lucro total vem ao lado (`melhor_lucro_total`),
    com a mesma regra do pior cenario.
    """
    melhor = None
    linhas_melhor = None
    horas = (segundos or 0.0) / 3600.0
    for lado in lados:
        if not lado.get("permitido", True):
            continue
        for b in builds:
            dpg = b["dpg1"] * lado["fator"] * buff
            if dpg <= 0:
                continue
            gmax = int(golpes_agora_de(b) + b["golpes_hora"] * horas)
            if gmax < 1:
                continue
            # os dez primeiros golpes um a um (a tabela golpe a golpe), os
            # que chegam a cada corte, e todos os que a vida deixa
            cand = set(range(1, min(gmax, 10) + 1)) | {gmax}
            for c in contextos.values():
                for pool in POOLS:
                    x = c.get(pool)
                    if not x:
                        continue
                    for k in TIERS:
                        need = x["dano_corte"].get(k)
                        if need:
                            g = max(1, math.ceil(need / dpg))
                            if g <= gmax:
                                cand.add(g)
            linhas = []
            for g in sorted(cand):
                d = g * dpg
                cases = g * caso_golpe
                custo = g * b["custo_golpe"]
                bt = (bounty._bounty_do_dia(d, lado["taxa"], lado["fundo"])
                      if lado.get("paga") else 0.0)
                cen = {}
                for nome, c in contextos.items():
                    res = _resultado(d, c, val)
                    itens = sum(x["valor"] for x in res.values())
                    lucro = itens + cases + bt - custo
                    cen[nome] = {**res, "itens": itens, "lucro": lucro,
                                 "lucro_1k": lucro / (d / 1000.0)}
                pior = min(CENARIOS, key=lambda n: cen[n]["lucro_1k"]) if all(
                    n in cen for n in CENARIOS) else None
                linhas.append({"golpes": g, "dano": d, "cases": cases, "bounty": bt,
                               "custo": custo, "cenarios": cen, "pior": pior,
                               "pior_lucro": min(cen[n]["lucro"] for n in CENARIOS if n in cen),
                               "pior_lucro_1k": min(cen[n]["lucro_1k"] for n in CENARIOS if n in cen)})
            ok_ = [x for x in linhas if x["pior_lucro"] > 0]
            if not ok_:
                continue
            esc = max(ok_, key=lambda x: (round(x["pior_lucro_1k"], 6),
                                          x["cenarios"]["esperado"]["lucro"]))
            chave = (round(esc["pior_lucro_1k"], 6), esc["cenarios"]["esperado"]["lucro"])
            if melhor is None or chave > melhor[0]:
                melhor = (chave, lado, b, esc)
                linhas_melhor = linhas
    if not melhor:
        return {"recomendado": None}
    _, lado, b, esc = melhor
    total = max(linhas_melhor, key=lambda x: (x["pior_lucro"], x["cenarios"]["esperado"]["lucro"]))
    return {"recomendado": esc, "lado": lado, "build": b, "linhas": linhas_melhor,
            "melhor_lucro_total": total,
            "dpg": b["dpg1"] * lado["fator"] * buff}


# ---------------------------------------------------------------------------
#  8. Uma ronda inteira: ler, projectar, planear
# ---------------------------------------------------------------------------


def _lados_da_batalha(onde: list[dict], bid: str) -> list[dict]:
    fora = []
    for l in onde:
        if l["batalha"] != bid:
            continue
        cx = l.get("contexto") or {}
        fora.append({"lado": l["lado"], "pais": l["pais_nome"], "contra": l["contra"],
                     "fator": float(cx.get("fator") or 1.0),
                     "componentes": cx.get("componentes") or [],
                     "construcao_lida": cx.get("construcao_lida"),
                     "taxa": l["por_1k"], "fundo": l["fundo"], "estado": l["estado"],
                     "paga": l["paga"], "permitido": True})
    return fora


def analisar_ronda(b: dict, onde: list[dict], cfg: dict, comp: dict, val: Valores,
                   builds: list[dict], caso_golpe: float, golpes_agora_de, buff: float,
                   t: float, uid: str, titulo: str | None = None,
                   builds_compra: list[dict] | None = None) -> dict:
    d_ = drops()
    loot = cfg.get("loot") or {}
    por_r = float(loot.get("damagePerLootItem") or 200000)
    por_b = float(loot.get("battleLootDamagePerLootItem") or 500000)
    rd = b.get("currentRound") if isinstance(b.get("currentRound"), dict) else \
        api.safe("round.getById", {"roundId": b.get("currentRound")}, {}) or {}
    rid, bid = rd.get("_id"), b["_id"]
    agora_dt = datetime.fromtimestamp(t, timezone.utc)
    fim = d_.estimate_round_seconds(rd, cfg.get("battle") or {}, agora_dt)
    rows = ranking_merged({"roundId": rid}, vivo=True)
    fotos = guardar_foto(rid, rows, t)
    rtw = int(b.get("roundsToWin") or 2)
    lider = d_.winning_side(rd)
    vitorias = int(((b.get(lider) or {}).get("wonRoundsCount") or 0)) if lider else 0
    fecha = bool(lider) and vitorias >= rtw - 1
    rows_b = ranking_merged({"battleId": bid}, vivo=True) if fecha else []

    # as rondas anteriores desta batalha acabaram: entram na amostra da composicao
    for n, rid_ant in enumerate(b.get("rounds") or [], start=1):
        if rid_ant == rid:
            continue
        try:
            ant = ranking_merged({"roundId": rid_ant}, vivo=False)
        except api.ErroAPI:
            continue
        guardar_pool("round:" + rid_ant, ant, int(sum(r["value"] for r in ant) // por_r))

    rit = ritmos(fotos, rows, t, _ts(rd.get("createdAt")), por_r)
    ritmo_agora = max(rit.get("recente") or 0.0, rit.get("media_desde_inicio") or 0.0)

    def folga(chave, por):
        return 1 + math.ceil(ritmo_agora * DURACAO_LEITURA.get(chave, 0.0) / por)
    regra = {"ronda": verificar_regra(rows, por_r, vivo=True, folga_itens=folga("round_" + rid, por_r))}
    if rows_b:
        regra["batalha"] = verificar_regra(rows_b, por_b, vivo=True,
                                           folga_itens=folga("battle_" + bid, por_b))
    w = pesos(rows, t)
    D = sum(r["value"] for r in rows)
    P = len(rows)
    DB = sum(r["value"] for r in rows_b)
    forma = sorted((r["value"] for r in rows), reverse=True)
    vistos_r = Counter(k for k in (tier(r.get("item")) for r in rows) if k)
    vistos_b = Counter(k for k in (tier(r.get("item")) for r in rows_b) if k)
    blocos_r = blocos_de_valor(rows, val)
    blocos_b = blocos_de_valor(rows_b, val) if rows_b else {}
    val_r = [(r.get("user"), r["value"]) for r in rows]
    val_b = [(r.get("user"), r["value"]) for r in rows_b]
    contextos = {}
    for nome in CONTEXTOS:
        T = 0.0 if nome == "agora" or fim is None else float(fim)
        R = 0.0 if nome == "agora" else rit["R"][nome]
        J = 0.0 if nome == "agora" else rit["J"][nome]
        extra, n_novos = R * T, J * T
        novos = novos_jogadores(n_novos, extra, rit["rajadas"], forma)
        proj = projectar(val_r, extra, novos, w)
        Nf = int((D + extra) // por_r)
        Pf = P + len(novos) + 1
        cr = cortes_por_rank(contagem_final(vistos_r, int(D // por_r), Nf, comp), Nf, Pf)
        c = {"T": T, "R": R, "J": J, "extra": extra, "novos": len(novos), "N_final": Nf,
             "P_final": Pf,
             "ronda": {"cortes": cr, "neg": [-x for x in proj], "blocos": blocos_r,
                       "dano_corte": {k: dano_para_rank(cr[k], proj) for k in TIERS if cr[k]}}}
        if rows_b:
            # na batalha cada um cresce o que crescer nesta ronda, e quem entra
            # agora entra tambem na batalha
            pb = projectar(val_b, extra, novos, w)
            NBf = int((DB + extra) // por_b)
            PBf = len(rows_b) + len(novos) + 1
            cb = cortes_por_rank(contagem_final(vistos_b, int(DB // por_b), NBf, comp), NBf, PBf)
            c["batalha"] = {"cortes": cb, "neg": [-x for x in pb], "blocos": blocos_b,
                            "dano_corte": {k: dano_para_rank(cb[k], pb) for k in TIERS if cb[k]}}
            c["N_batalha"], c["P_batalha"] = NBf, PBf
        contextos[nome] = c

    lados = _lados_da_batalha(onde, bid)
    plano = planear(contextos, lados, builds, val, caso_golpe, golpes_agora_de, fim, buff)
    plano_c = (planear(contextos, lados, builds_compra, val, caso_golpe, golpes_agora_de, fim, buff)
               if builds_compra else {"recomendado": None})
    bt = backtest(fotos, rows, t, por_r)

    def resumo_contexto(c):
        fora = {k: c[k] for k in ("T", "R", "J", "extra", "novos", "N_final", "P_final")}
        for pool in POOLS:
            x = c.get(pool)
            if x:
                fora[pool] = {str(k): {"rank": x["cortes"][k], "dano": x["dano_corte"].get(k)}
                              for k in TIERS if x["cortes"].get(k)}
        return fora

    eu = next((r for r in rows if r.get("user") == uid), None)
    return {
        "batalha": bid, "ronda": rid, "numero": rd.get("number"),
        "titulo": titulo or bid,
        "estado": {
            "dano": D, "jogadores": P, "itens": int(D // por_r),
            "mais_itens_que_jogadores": int(D // por_r) > P,
            "fim_segundos": fim, "fim_txt": d_.fmt_time(fim),
            "pontos": {s: (rd.get(s) or {}).get("points") for s in ("attacker", "defender")},
            "lider": lider, "fecha_batalha": fecha,
            "porque_batalha": (f"o lado que vai à frente já tem {vitorias} de {rtw} rondas: "
                               "a batalha acaba com esta ronda se ele a ganhar"
                               if fecha else
                               "a batalha continua depois desta ronda: o pool da batalha "
                               "fecha mais tarde e não entra nesta conta"),
            "dano_batalha": DB, "itens_batalha": int(DB // por_b) if rows_b else None,
            "tu": {"rank": eu["rank"], "dano": eu["value"], "item": eu["item"]} if eu else None,
        },
        "regra": regra,
        "ritmo": {k: v for k, v in rit.items() if k != "rajadas"} | {"chegados_amostra": len(rit["rajadas"])},
        "contextos": {n: resumo_contexto(c) for n, c in contextos.items()},
        "lados": lados,
        "plano": _resumo_plano(plano),
        "plano_com_compras": _resumo_plano(plano_c),
        "backtest": bt,
        "_contextos": contextos,
    }


def _resumo_plano(p: dict) -> dict:
    if not p.get("recomendado"):
        return {"recomendado": None}
    b = p["build"]

    def linha(x):
        return {"golpes": x["golpes"], "dano": x["dano"], "cases": x["cases"],
                "bounty": x["bounty"], "custo": x["custo"], "pior": x.get("pior"),
                "pior_lucro": x.get("pior_lucro"), "pior_lucro_1k": x.get("pior_lucro_1k"),
                "cenarios": {n: {"lucro": c["lucro"], "lucro_1k": c["lucro_1k"], "itens": c["itens"],
                                 **{pool: c[pool] for pool in POOLS if pool in c}}
                             for n, c in x["cenarios"].items()}}
    return {
        "recomendado": linha(p["recomendado"]),
        "melhor_lucro_total": linha(p["melhor_lucro_total"]),
        "lado": p["lado"],
        "build": {"nome": nome_build(b), "pecas": {s: {"codigo": x["codigo"], "origem": x["origem"],
                                                        "raridade": x.get("raridade"),
                                                        "preco": x.get("preco"),
                                                        "estado": x.get("estado")}
                                                    for s, x in b["pecas"].items()},
                  "municao": b["municao"], "comida": b["comida"], "dpg1": b["dpg1"],
                  "custo_golpe": b["custo_golpe"], "golpes_hora": b["golpes_hora"],
                  "capex": b["capex"], "tens": b["tens"]},
        "dpg": p["dpg"],
        "linhas": [linha(x) for x in p["linhas"]],
    }


# ---------------------------------------------------------------------------
#  9. Previsoes guardadas e validacao contra o resultado real
# ---------------------------------------------------------------------------


def guardar_previsao(r: dict, t: float) -> None:
    if r.get("erro"):
        return
    d = _ler(DIR / "previsoes.json", {})
    lst = d.setdefault(r["ronda"], [])
    plano = (r.get("plano") or {}).get("recomendado")
    lst.append({
        "t": t, "batalha": r["batalha"], "fim_estimado": (t + r["estado"]["fim_segundos"])
        if r["estado"].get("fim_segundos") is not None else None,
        "cortes": {n: (c.get("ronda") or {}) for n, c in r["contextos"].items()},
        "plano": ({"golpes": plano["golpes"], "dano": plano["dano"],
                   "tier_ronda": {n: (c.get("ronda") or {}).get("tier")
                                  for n, c in plano["cenarios"].items()}} if plano else None),
    })
    d[r["ronda"]] = lst[-20:]
    _gravar(DIR / "previsoes.json", d)


def validar_previsoes(maximo: int = 4) -> list[dict]:
    """
    As rondas que ja acabaram, com o que se previu ao lado do que aconteceu.

    E a unica validacao do modelo de concorrencia num horizonte de horas: o
    ranking final e lido uma vez (guardado para sempre) e comparado com cada
    previsao guardada dessa ronda.
    """
    prev = _ler(DIR / "previsoes.json", {})
    feitas = _ler(DIR / "validacoes.json", {})
    fora = []
    por_r = 200000.0
    for rid in sorted(prev, key=lambda k: -prev[k][-1]["t"])[: maximo * 3]:
        if rid in feitas:
            fora.append(feitas[rid])
            continue
        if len(fora) >= maximo:
            break
        rd = api.cached(f"opt:rd:{rid}", 300,
                        lambda rid=rid: api.safe("round.getById", {"roundId": rid}, {}) or {})
        if rd.get("isActive") is not False:
            continue
        try:
            rows = ranking_merged({"roundId": rid}, vivo=False)
        except api.ErroAPI:
            continue
        real = sorted((r["value"] for r in rows), reverse=True)
        neg = [-x for x in real]
        vistos = Counter(k for k in (tier(r.get("item")) for r in rows) if k)
        N = int(sum(real) // por_r)
        guardar_pool("round:" + rid, rows, N)
        cortes, acum = {}, 0
        for k in TIERS:
            acum += vistos.get(k, 0)
            if vistos.get(k):
                cortes[str(k)] = {"rank": acum, "dano": real[acum - 1] + 1.0}
        comparacoes = []
        for p in prev[rid]:
            linha = {"t": p["t"], "fim_estimado": p.get("fim_estimado"),
                     "fim_real": _ts(rd.get("updatedAt")), "cortes": {}}
            for n, cs in (p.get("cortes") or {}).items():
                linha["cortes"][n] = {k: {"previsto": v, "real": cortes.get(k)} for k, v in cs.items()}
            if p.get("plano"):
                r_ = posicao(p["plano"]["dano"], neg)
                acum, k_real = 0, None
                for k in TIERS:
                    acum += vistos.get(k, 0)
                    if r_ <= acum and vistos.get(k):
                        k_real = k
                        break
                linha["plano"] = {**p["plano"], "rank_real": r_, "tier_real": k_real,
                                  "nome_real": NOME.get(k_real)}
            comparacoes.append(linha)
        v = {"ronda": rid, "batalha": rd.get("battle"), "jogadores": len(rows), "itens": N,
             "regra": verificar_regra(rows, por_r, vivo=False), "cortes_reais": cortes,
             "previsoes": comparacoes}
        feitas[rid] = v
        fora.append(v)
    _gravar(DIR / "validacoes.json", feitas)
    return fora


# ---------------------------------------------------------------------------
#  10. Tudo junto, para um jogador
# ---------------------------------------------------------------------------


def _contextos_de_lados(onde: list[dict], user: dict, cfg: dict, ler_construcao: set) -> None:
    """
    O bonus de cada lado para este jogador, como na aba Bounty e com as mesmas
    chaves de cache: o QG da MU, as ordens do pais e da MU, e a construcao da
    regiao so onde a resposta pode mudar.
    """
    hq = {}
    if user.get("mu"):
        hq = api.cached(f"bounty:hq:{user['mu']}", config.TTL_CACHE_CONFIG,
                        lambda: api.safe("upgrade.getUpgradeByTypeAndEntity",
                                         {"upgradeType": "headquarters", "muId": user["mu"]}, {}) or {})
    hq_nivel = hq.get("level") if hq.get("status") == "active" else 0
    for l in onde:
        ordens = []
        if l["tem_ordem_pais"] or l["tem_ordem_mu"]:
            ordens = api.cached(f"bounty:ordens:{l['batalha']}:{l['lado']}", 120,
                                lambda l=l: api.safe("battleOrder.getByBattle",
                                                     {"battleId": l["batalha"], "side": l["lado"]},
                                                     []) or [])
        nivel = None
        if l["batalha"] in ler_construcao and l.get("regiao"):
            tipo = "bunker" if l["lado"] == "defender" else "base"
            u = api.cached(f"bounty:up:{tipo}:{l['regiao']}", 600,
                           lambda tipo=tipo, l=l: api.safe("upgrade.getUpgradeByTypeAndEntity",
                                                           {"upgradeType": tipo, "regionId": l["regiao"]},
                                                           {}) or {})
            nivel = u.get("level") if u.get("status") == "active" else 0
        l["contexto"] = bounty.contexto_de_lado(l["_b"], l["lado"], user, cfg, ordens, hq_nivel, nivel)


def triagem(batalhas: list[dict], onde: list[dict], comp: dict, val: Valores,
            por_item: float) -> list[dict]:
    """
    Todas as rondas activas, com o que a API da quase de graca: o dano da
    ronda (vem na lista de batalhas) e quantos jogadores tem o merged (um
    pedido). Daqui sai a raridade que leva hoje quem entrar no fundo.
    """
    pode = {}
    for l in onde:
        pode.setdefault(l["batalha"], []).append(l)
    rondas = []
    for b in batalhas:
        rd = b.get("currentRound")
        if b.get("isActive") is False or not isinstance(rd, dict) or b["_id"] not in pode:
            continue
        rondas.append((b, rd))
    with ThreadPoolExecutor(max_workers=6) as ex:
        ps = list(ex.map(lambda x: jogadores_merged(x[1]["_id"]), rondas))
    q = {k: comp["por_tier"][k]["q"] for k in TIERS}
    fora = []
    for (b, rd), P in zip(rondas, ps):
        D = float((rd.get("attacker") or {}).get("damages") or 0) + \
            float((rd.get("defender") or {}).get("damages") or 0)
        N = int(D // por_item)
        fundo_k = None
        if P:
            for k in TIERS:
                if q[k] * N >= P + 1:
                    fundo_k = k
                    break
        ls = pode[b["_id"]]
        fora.append({
            "batalha": b["_id"], "ronda": rd.get("_id"), "numero": rd.get("number"),
            "titulo": f"{ls[0]['pais_nome']} / {ls[0]['contra']}",
            "dano": D, "jogadores": P, "itens": N,
            "itens_por_jogador": N / P if P else None,
            "tier_no_fundo": fundo_k, "nome_no_fundo": NOME.get(fundo_k) if fundo_k else None,
            "valor_no_fundo": val.novo(fundo_k) if fundo_k else 0.0,
            "lados": [{"lado": l["lado"], "pais": l["pais_nome"], "contra": l["contra"],
                       "fator": (l.get("contexto") or {}).get("fator"),
                       "taxa": l["por_1k"], "fundo": l["fundo"], "estado": l["estado"],
                       "paga": l["paga"]} for l in ls],
            "_b": b,
        })
    fora.sort(key=lambda x: (-(x["valor_no_fundo"] or 0), -(x["itens_por_jogador"] or 0)))
    return fora


def _perfil(user: dict, vestido: dict, itens: dict, mu: dict | None) -> dict:
    sk = user.get("skills") or {}
    lev = user.get("leveling") or {}
    buffs = user.get("buffs") or {}
    return {
        "id": user.get("_id"), "nome": user.get("username"), "nivel": lev.get("level"),
        "pontos_livres": lev.get("availableSkillPoints"), "pontos_total": lev.get("totalSkillPoints"),
        "reset_gratis": lev.get("freeReset"),
        "ultimo_reset": (user.get("dates") or {}).get("lastSkillsResetAt"),
        "skills": {s: {"nivel": (sk.get(s) or {}).get("level"), "total": (sk.get(s) or {}).get("total")}
                   for s in bounty.SKILLS_A_SUBIR},
        "vida": {"agora": (sk.get("health") or {}).get("currentBarValue"),
                 "max": (sk.get("health") or {}).get("total"),
                 "por_hora": (sk.get("health") or {}).get("hourlyBarRegen")},
        "fome": {"agora": (sk.get("hunger") or {}).get("currentBarValue"),
                 "max": (sk.get("hunger") or {}).get("total"),
                 "por_hora": (sk.get("hunger") or {}).get("hourlyBarRegen")},
        "buff": {"codigos": buffs.get("buffCodes") or [], "ate": buffs.get("buffEndAt"),
                 "ataque_pct": (sk.get("attack") or {}).get("buffsPercent"),
                 "ressaca_pct": (sk.get("attack") or {}).get("debuffsPercent")},
        "vestido": {s: {"codigo": v.get("code"), "skills": v.get("skills"), "estado": v.get("state"),
                        "raridade": (itens.get(v.get("code")) or {}).get("rarity"),
                        "qualidade": pecas.qualidade(v.get("skills") or {},
                                                     pecas.intervalos(v.get("code"), itens))}
                    for s, v in vestido.items()},
        "epic_seis": (len(vestido) == 6 and all((itens.get(v.get("code")) or {}).get("rarity")
                                                in RARIDADES_EPIC for v in vestido.values())),
        "mu": (mu or {}).get("nome"),
    }


def analisar(ref: str, rondas: int = RONDAS_POR_OMISSAO, alvo: str | None = None) -> dict:
    """
    Tudo para um jogador: perfil, a regra conferida, todas as batalhas, e o
    plano de golpes nas rondas que mais rendem por 1000 de dano.

    `alvo` e um battleId: essa ronda entra sempre no plano.
    """
    from . import build, craft, mercado as _mercado, precos as _precos

    t0 = time.time()
    cfg = api.game_config()
    itens = cfg.get("items") or {}
    uid = build.resolver_user_id(ref)
    user = api.utilizador(uid)
    equip = api.equipamento_vestido(uid) or {}
    vestido = {s: v for s, v in equip.items() if s in bounty.SLOTS and isinstance(v, dict)}

    # precos, pelos mesmos caminhos e caches da aba Bounty
    codigos_eq = sorted(c for c, it in itens.items() if it.get("type") in ("equipment", "weapon"))
    rel = api.em_cache("mercado:", config.TTL_CACHE_MERCADO) or {}
    precos_eq = rel.get("precos_equipamento") or api.em_cache("bounty:eq", config.TTL_CACHE_CONFIG)
    if not precos_eq:
        precos_eq = {c: v for c, v in craft.precos_equipamento(codigos_eq).items() if v}
        if len(precos_eq) == len(codigos_eq):
            api.cached("bounty:eq", config.TTL_CACHE_CONFIG, lambda: precos_eq)
    ref_precos = api.cached("bounty:ref", 60, api.precos_referencia)
    bens = ["lightAmmo", "ammo", "heavyAmmo", "bread", "steak", "cookedFish",
            "cocain", "case1", "case2", "scraps"]
    livro = api.cached("bounty:livro", 30, lambda: _mercado.instantaneo(bens, ref_precos))
    mp = _precos.ProvedorDeMercado(referencia=ref_precos, panorama=livro, equipamento=precos_eq)
    taxa = config.TAXA_MERCADO_PADRAO
    precos_mapa = mp.mapa()
    custos_scrap = craft.custo_em_scraps(itens, {})
    preco_scrap = mp.preco("scraps", "bid")

    def preco_venda(code):
        v = mp.preco(code, "bid")
        return v * (1 - taxa) if v else None

    vendas, info_vendas = vendas_com_roll()
    peso_arma = float((cfg.get("loot") or {}).get("weaponChancePercent") or 30) / 100.0
    val = Valores(itens, precos_eq, vendas, taxa, peso_arma)
    comp = composicao(pools_conhecidos())

    # as cases caem por golpe: o teu historico, como na aba Bounty. As pecas do
    # ranking NAO entram por aqui (eram uma media linear por 1k): vem do ranking
    resumos = bounty.resumos_do_jogador(uid, user.get("country"))
    hist = bounty.historico(resumos, precos_eq, taxa)
    base = bounty.stats_sem_equipamento(user)
    rec = bounty.receitas(hist, preco_venda, float(base.get("lootChance") or 0.0), 0.0)
    caso_golpe = float(rec["por_golpe"])

    ctx1 = bounty.contexto(cfg, precos_mapa, bounty.posto_pct(user), custos_scrap, preco_scrap,
                           bounty.HORAS_COMBATE, 1.0, bounty.HORIZONTE_DIAS, None)
    op = bounty.opcoes(itens, precos_eq, vestido, custos_scrap, preco_scrap, taxa)
    bs = builds_epic(base, op, ctx1, cfg)
    # o plano corre com o que ja tens se o que tens for Epic+ nos seis slots;
    # as builds a comprar aparecem ao lado, com o que poupam e em quanto se pagam
    usar = bs["tens"] or sorted(bs["fronteira"], key=lambda b: b["custo_golpe"])[:25]
    compra = sorted(bs["fronteira"], key=lambda b: b["custo_golpe"])[:25]

    sk = user.get("skills") or {}
    vida_agora = float((sk.get("health") or {}).get("currentBarValue") or 0.0)
    fome_agora = float((sk.get("hunger") or {}).get("currentBarValue") or 0.0)

    def golpes_agora_de(b):
        if not b.get("vida_golpe"):
            return 0.0
        return (vida_agora + fome_agora * b.get("vida_comida", 0.0)) / b["vida_golpe"]

    atk = sk.get("attack") or {}
    buff_ate = _ts((user.get("buffs") or {}).get("buffEndAt"))
    buff = 1.0
    if buff_ate and buff_ate > t0:
        buff *= 1 + float(atk.get("buffsPercent") or 0.0) / 100.0
    buff *= 1 - float(atk.get("debuffsPercent") or 0.0) / 100.0

    nomes = {c["_id"]: c.get("name") or c.get("code") for c in api.paises()}
    batalhas = api.batalhas_ativas()
    onde = bounty.lados_onde_bater(batalhas, user, nomes)
    por_r = float((cfg.get("loot") or {}).get("damagePerLootItem") or 200000)

    _contextos_de_lados(onde, user, cfg, set())
    tri = triagem(batalhas, onde, comp, val, por_r)
    escolhidas = [x for x in tri if alvo and x["batalha"] == alvo]
    escolhidas += [x for x in tri if x not in escolhidas][: max(0, int(rondas) - len(escolhidas))]
    _contextos_de_lados(onde, user, cfg, {x["batalha"] for x in escolhidas})
    for x in tri:
        for lt in x["lados"]:
            l = next((o for o in onde if o["batalha"] == x["batalha"] and o["lado"] == lt["lado"]), None)
            if l:
                lt["fator"] = (l.get("contexto") or {}).get("fator")

    def correr(x):
        # uma ronda que falhe nao leva as outras consigo: fica marcada com o erro
        try:
            return analisar_ronda(x["_b"], onde, cfg, comp, val, usar, caso_golpe,
                                  golpes_agora_de, buff, time.time(), uid, x["titulo"],
                                  compra if bs["tens"] else None)
        except Exception as e:  # noqa: BLE001
            return {"batalha": x["batalha"], "ronda": x["ronda"], "titulo": x["titulo"],
                    "erro": f"{type(e).__name__}: {str(e)[:300]}"}

    with ThreadPoolExecutor(max_workers=3) as ex:
        analisadas = list(ex.map(correr, escolhidas))

    # verificacao: as builds escolhidas pelo motor inteiro (`bounty.avaliar`)
    for r in analisadas:
        ctxs = r.pop("_contextos", None)
        p = r.get("plano") or {}
        if not p.get("recomendado"):
            continue
        guardar_previsao(r, t0)
        b = next((x for x in usar if nome_build(x) == p["build"]["nome"]
                  and x["municao"] == p["build"]["municao"] and x["comida"] == p["build"]["comida"]), None)
        if b:
            ctx_l = {**ctx1, "batalha": p["lado"]["fator"], "fundo": None}
            m = bounty.avaliar(base, b["pecas"], b["municao"], b["comida"], False, ctx_l, rec)
            if m:
                p["verificacao_motor"] = {
                    "dano_por_golpe_motor": m["dano_por_golpe"],
                    "dano_por_golpe_procura": b["dpg1"] * p["lado"]["fator"],
                    "custo_golpe_motor": m["custo_golpe"],
                    "custo_golpe_procura": b["custo_golpe"],
                    "diferenca_pct": max(
                        abs(m["dano_por_golpe"] / (b["dpg1"] * p["lado"]["fator"]) - 1) * 100,
                        abs(m["custo_golpe"] / b["custo_golpe"] - 1) * 100 if b["custo_golpe"] else 0.0),
                }
        # antes e depois da correccao: a media linear por 1k (aba Bounty) contra o ranking
        rcm = p["recomendado"]
        p["antes_depois"] = {
            "dano": rcm["dano"],
            "antes_pecas_ranking": (hist.get("pool_por_1k") or 0.0) * rcm["dano"] / 1000.0,
            "antes_fonte": "aba Bounty: peças do ranking que recebeste por 1k de dano, em média",
            "depois_pecas_ranking": {n: c["itens"] for n, c in rcm["cenarios"].items()},
        }
        del ctxs

    # as builds a comprar, ao lado: quanto poupam por golpe face ao que tens
    tens_melhor = min(bs["tens"], key=lambda b: b["custo_golpe"]) if bs["tens"] else None
    comprar = []
    for b in sorted(bs["fronteira"], key=lambda b: b["custo_golpe"])[:12]:
        poupa = (tens_melhor["custo_golpe"] - b["custo_golpe"]) if tens_melhor else None
        comprar.append({
            "nome": nome_build(b), "municao": b["municao"], "comida": b["comida"],
            "dpg1": b["dpg1"], "custo_golpe": b["custo_golpe"], "golpes_hora": b["golpes_hora"],
            "capex": b["capex"], "compras": [p["codigo"] for p in b["pecas"].values() if p["origem"] == "comprar"],
            "poupa_por_golpe": poupa,
            "paga_se_em_golpes": (b["capex"] / poupa if poupa and poupa > 0 and b["capex"] > 0 else None),
        })
    maximo_dano = max(bs["fronteira"] + bs["tens"], key=lambda b: b["dpg1"], default=None)

    plano_total = sorted([r for r in analisadas if (r.get("plano") or {}).get("recomendado")],
                         key=lambda r: -r["plano"]["recomendado"]["cenarios"]["conservador"]["lucro_1k"])
    golpes_hoje = golpes_agora_de(usar[0]) if usar else 0.0
    return {
        "jogador": _perfil(user, vestido, itens, bounty.mu_do_jogador(user, cfg)),
        "lido_em": datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "segundos": round(time.time() - t0, 1),
        "composicao": comp,
        "valores": {"novo": {NOME[k]: val.novo(k) for k in (2, 3, 4, 5, 6)},
                    "vendas_com_roll": info_vendas,
                    "codigos_com_modelo_de_roll": sorted(c for c, m in val.modelo.items() if m.get("fiavel")),
                    "taxa_venda": taxa, "peso_arma": peso_arma},
        "receitas": {"cases_por_golpe": rec["cases_por_golpe"], "case2_por_golpe": rec["case2_por_golpe"],
                     "valor_por_golpe": caso_golpe, "fonte": rec["fonte_cases"],
                     "valor_case1": rec["valor_case1"], "valor_case2": rec["valor_case2"]},
        "buff": {"multiplicador": buff, "ate": (user.get("buffs") or {}).get("buffEndAt")},
        "builds": {"avaliadas": bs["avaliadas"], "combos_dano": bs["combos_dano"],
                   "combos_defesa": bs["combos_defesa"], "fronteira": len(bs["fronteira"]),
                   "tens_epic_seis": bool(bs["tens"]),
                   "usadas_no_plano": "as que tens vestidas" if bs["tens"] else "a fronteira a comprar",
                   "comprar": comprar,
                   "maximo_dano": ({"nome": nome_build(maximo_dano), "dpg1": maximo_dano["dpg1"],
                                    "custo_golpe": maximo_dano["custo_golpe"],
                                    "capex": maximo_dano["capex"]} if maximo_dano else None)},
        "triagem": [{k: v for k, v in x.items() if k != "_b"} for x in tri],
        "rondas": analisadas,
        "plano": [{"batalha": r["batalha"], "ronda": r["ronda"], "titulo": r["titulo"],
                   "lado": r["plano"]["lado"]["lado"], "pais": r["plano"]["lado"]["pais"],
                   "build": r["plano"]["build"]["nome"], "municao": r["plano"]["build"]["municao"],
                   "comida": r["plano"]["build"]["comida"],
                   **r["plano"]["recomendado"]} for r in plano_total],
        "golpes_disponiveis_agora": golpes_hoje,
        "validacao": {"previsoes": validar_previsoes()},
        "limitacoes": LIMITACOES,
    }
