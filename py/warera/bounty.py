"""
Bounty: o que o jogo paga por cada mil de dano, e o equipamento que o torna
rentavel.

O QUE E O BOUNTY, LIDO DA API
-----------------------------

Cada lado de cada batalha traz tres campos que nao estavam em conta nenhuma
deste painel (`battle.getBattles`):

    moneyPer1kDamages   quanto paga, por cada 1000 de dano feito desse lado
    moneyPool           o fundo de onde sai o pagamento
    bountyEffectiveAt   a partir de quando paga

e `battleLootSummary.getByBattleAndUser` diz, jogador a jogador, o que cada um
recebeu (`totalMoneyFromBounty`). As duas fontes batem ao centimo: 1 708 476
de dano num lado a 0,025 dao 42,71, e o jogo pagou 42,76.

A consequencia que manda em tudo o resto: **o bounty por 1000 de dano e fixado
pela batalha, e nao pelo equipamento.** Nenhuma peca o muda. O que o
equipamento muda e quanto CUSTA fazer esses mil de dano — e e essa conta que
este modulo resolve.

OS DROPS NAO SAO BOUNTY, E NAO SAO POR DANO
-------------------------------------------

O resumo de loot de 71 jogadores reais da 0,10 a 0,22 cases por GOLPE, sem
relacao nenhuma com o dano por golpe (de 2 880 a 21 000): quem bate de faca a
300 de dano leva cases ao mesmo ritmo. O que cai por dano sao as pecas do
ranking da ronda e da batalha (`poolLoot`), e essas dependem da posicao — aqui
entram pelo que o proprio jogador recebeu, por mil de dano, e marcadas como
aproximacao.

Por isso ha duas receitas com unidades diferentes, e nao se misturam:

    por golpe     cases (lootChance)
    por 1k dano   bounty + pecas do ranking (+ contrato de mercenario, opcional)

O CUSTO E O DO MOTOR
--------------------

Municao, comida, pilula e o desgaste de cada peca saem de
`objetivos.simular_dia`, que chama o motor. Este modulo nao tem formula de
combate nenhuma: enumera o que se pode vestir e pede a conta.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import api, bonus, config, durabilidade, objetivos, pecas

SLOTS = ("weapon", "helmet", "chest", "pants", "gloves", "boots")
GRUPO_DANO = ("weapon", "gloves", "helmet")
GRUPO_DEFESA = ("chest", "pants", "boots")
STATS_DANO = ("attack", "criticalChance", "criticalDamages", "precision")
STATS_DEFESA = ("armor", "dodge")
# o que a build traz sem equipamento nenhum: a skill e o bonus estrategico
STATS_BASE = ("attack", "precision", "criticalChance", "criticalDamages",
              "armor", "dodge", "health", "hunger", "lootChance")
SKILLS_A_SUBIR = ("attack", "precision", "criticalChance", "criticalDamages",
                  "armor", "dodge", "health", "hunger", "lootChance")
COMIDAS = ("bread", "steak", "cookedFish")
ARMA_SEM_MUNICAO = "knife"
HORIZONTE_DIAS = 7.0
HORAS_COMBATE = 8.0
# abaixo disto o historico do jogador nao chega para medir cases por golpe, e
# usa-se a regra da skill (lootChance% por golpe)
MIN_GOLPES_HISTORICO = 500
CAMINHO_RESUMOS = config.RAIZ / "saida" / "bounty_resumos.json"


# ---------------------------------------------------------------------------
#  1. O bounty: quem paga, quanto, e ate quando
# ---------------------------------------------------------------------------


def _quando(valor) -> datetime | None:
    if not isinstance(valor, str):
        return None
    try:
        return datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None


def lados_com_bounty(batalhas: list[dict], nomes: dict | None = None,
                     agora: datetime | None = None) -> list[dict]:
    """
    Cada lado de batalha activa que tem uma taxa de bounty, e se paga mesmo.

    `moneyPool` ausente NAO e "fundo infinito": o jogador analisado fez 621 mil
    de dano num lado a 0,025 com o fundo a `None` e recebeu zero. Por isso ha
    quatro estados e so um paga:

        a pagar           fundo positivo e ja em vigor
        ainda nao paga    fundo positivo, `bountyEffectiveAt` no futuro
        esgotado          fundo a zero
        sem fundo         a taxa existe mas nao ha fundo
    """
    nomes = nomes or {}
    agora = agora or datetime.now(timezone.utc)
    fora = []
    for b in batalhas or []:
        if b.get("isActive") is False:
            continue
        for lado, outro in (("attacker", "defender"), ("defender", "attacker")):
            s = b.get(lado) or {}
            taxa = s.get("moneyPer1kDamages")
            if not isinstance(taxa, (int, float)) or taxa <= 0:
                continue
            fundo = s.get("moneyPool")
            desde = _quando(s.get("bountyEffectiveAt"))
            if not isinstance(fundo, (int, float)):
                estado = "sem fundo"
            elif fundo <= 0:
                estado = "esgotado"
            elif desde and desde > agora:
                estado = "ainda não paga"
            else:
                estado = "a pagar"
            pais = s.get("country")
            adv = (b.get(outro) or {}).get("country")
            fora.append({
                "batalha": b.get("_id"),
                "lado": lado,
                "pais": pais,
                "pais_nome": nomes.get(pais, pais),
                "contra": nomes.get(adv, adv),
                "por_1k": float(taxa),
                "por_milhao": float(taxa) * 1000.0,
                "fundo": float(fundo) if isinstance(fundo, (int, float)) else None,
                # quanto dano o fundo ainda paga, a dividir por TODOS os que
                # batem desse lado: e um tecto e nao uma reserva tua
                "dano_ate_esgotar": (float(fundo) / float(taxa) * 1000.0
                                     if isinstance(fundo, (int, float)) and fundo > 0
                                     else 0.0 if isinstance(fundo, (int, float)) else None),
                "dano_do_lado": float(s.get("damages") or 0.0),
                "em_vigor_desde": s.get("bountyEffectiveAt"),
                "estado": estado,
                "paga": estado == "a pagar",
            })
    fora.sort(key=lambda x: (not x["paga"], -x["por_1k"], -(x["fundo"] or 0)))
    return fora


def melhor_lado(lados: list[dict], dano_minimo: float = 0.0) -> dict | None:
    """O lado que paga mais por 1k e cujo fundo ainda aguenta `dano_minimo`."""
    pagam = [l for l in lados if l["paga"]]
    chega = [l for l in pagam if (l["dano_ate_esgotar"] or 0) >= dano_minimo]
    return (chega or pagam or [None])[0]


# ---------------------------------------------------------------------------
#  2. O que o jogador ganhou mesmo, batalha a batalha
# ---------------------------------------------------------------------------


def historico(resumos: list[dict], precos_eq: dict | None = None,
              taxa: float = config.TAXA_MERCADO_PADRAO) -> dict:
    """
    A soma dos `battleLootSummary` do jogador: golpes, dano, cases, bounty,
    contrato e as pecas que lhe calharam pelo ranking.

    Sao os unicos numeros desta pagina que nao dependem de modelo nenhum: e o
    que o jogo registou. Servem de prova ao resto — o dano por golpe medido ao
    lado do que o motor calcula, as cases por golpe ao lado da regra da skill.
    """
    precos_eq = precos_eq or {}
    golpes = dano = c1 = c2 = caixas = bounty = contrato = 0.0
    pecas_pool: dict[str, int] = {}
    linhas = []
    for r in resumos or []:
        s = r.get("resumo") or r
        h = float(s.get("hits") or 0)
        d = float(s.get("totalDmg") or 0)
        if h <= 0:
            continue
        golpes += h
        dano += d
        c1 += float(s.get("case1Count") or 0)
        c2 += float(s.get("case2Count") or 0)
        caixas += float(s.get("toolboxCount") or 0)
        b = float(s.get("totalMoneyFromBounty") or 0)
        k = float(s.get("totalMoneyFromContract") or 0)
        bounty += b
        contrato += k
        for p in s.get("poolLoot") or []:
            code = ((p or {}).get("item") or {}).get("code")
            if code:
                pecas_pool[code] = pecas_pool.get(code, 0) + 1
        linhas.append({
            "batalha": s.get("battle") or r.get("batalha"),
            "golpes": h, "dano": d,
            "dano_por_golpe": d / h,
            "cases": float(s.get("case1Count") or 0) + float(s.get("case2Count") or 0),
            "bounty": b, "contrato": k,
            "bounty_por_1k": b / d * 1000.0 if d else None,
            "pecas_pool": len(s.get("poolLoot") or []),
            "quando": s.get("updatedAt") or s.get("createdAt"),
        })

    sem_preco = sorted(c for c in pecas_pool if not precos_eq.get(c))
    valor_pool = sum(n * float(precos_eq[c]) * (1 - taxa)
                     for c, n in pecas_pool.items() if precos_eq.get(c))
    linhas.sort(key=lambda x: x.get("quando") or "", reverse=True)
    k = dano / 1000.0
    return {
        "batalhas": len(linhas),
        "golpes": golpes,
        "dano": dano,
        "dano_por_golpe": dano / golpes if golpes else None,
        "case1_por_golpe": c1 / golpes if golpes else None,
        "case2_por_golpe": c2 / golpes if golpes else None,
        "toolbox_por_golpe": caixas / golpes if golpes else None,
        "case1": c1, "case2": c2, "toolbox": caixas,
        "bounty": bounty,
        "contrato": contrato,
        "bounty_por_1k": bounty / k if k else None,
        "contrato_por_1k": contrato / k if k else None,
        "pecas_pool": dict(sorted(pecas_pool.items(), key=lambda x: -x[1])),
        "n_pecas_pool": sum(pecas_pool.values()),
        "valor_pool": valor_pool,
        "pool_por_1k": valor_pool / k if k else None,
        "pecas_pool_sem_preco": sem_preco,
        "linhas": linhas,
    }


# ---------------------------------------------------------------------------
#  3. O que rende um golpe e o que rendem mil de dano
# ---------------------------------------------------------------------------


def receitas(hist: dict | None, preco_venda, loot_chance: float,
             bounty_1k: float, usar_contrato: bool = False,
             escala_loot: float = 1.0) -> dict:
    """
    As duas receitas, cada uma na sua unidade, com a fonte de cada numero.

    `preco_venda(codigo)` e o que se recebe a vender uma unidade (ja com a
    taxa descontada). As cases saem do historico do jogador quando ha golpes
    que cheguem para as medir, e da regra da skill quando nao ha.
    `escala_loot` serve para perguntar "e se subir o lootChance": multiplica o
    ritmo de cases e mais nada.
    """
    h = hist or {}
    medido = (h.get("golpes") or 0) >= MIN_GOLPES_HISTORICO
    if medido:
        r1 = float(h.get("case1_por_golpe") or 0.0)
        r2 = float(h.get("case2_por_golpe") or 0.0)
        fonte_cases = (f"o teu histórico: {h.get('case1', 0):.0f} cases em "
                       f"{h.get('golpes', 0):.0f} golpes")
    else:
        r1 = float(loot_chance or 0.0) * config.CASE_POR_LOOTCHANCE
        r2 = 0.0
        fonte_cases = f"regra da skill: {loot_chance:g}% de lootChance por golpe"
    r1 *= escala_loot
    r2 *= escala_loot
    v1 = preco_venda("case1")
    v2 = preco_venda("case2")
    por_golpe = r1 * (v1 or 0.0) + r2 * (v2 or 0.0)

    pool_1k = float(h.get("pool_por_1k") or 0.0)
    contrato_1k = float(h.get("contrato_por_1k") or 0.0) if usar_contrato else 0.0
    return {
        "cases_por_golpe": r1,
        "case2_por_golpe": r2,
        "valor_case1": v1,
        "valor_case2": v2,
        "fonte_cases": fonte_cases,
        "cases_medidas": medido,
        "por_golpe": por_golpe,
        "bounty_1k": float(bounty_1k or 0.0),
        "pool_1k": pool_1k,
        "fonte_pool": ("as peças que o ranking da ronda e da batalha te deram, por "
                       "1k de dano — aproximação: depende da posição e não só do dano"
                       if pool_1k else "sem histórico de peças do ranking"),
        "contrato_1k": contrato_1k,
        "por_1k": float(bounty_1k or 0.0) + pool_1k + contrato_1k,
    }


# ---------------------------------------------------------------------------
#  4. O que se pode vestir
# ---------------------------------------------------------------------------


def _slot_do_item(code: str, item: dict) -> str | None:
    if item.get("type") == "weapon":
        return "weapon"
    if item.get("type") == "equipment":
        u = item.get("usage")
        return u if u in SLOTS else None
    return None


def opcoes(itens: dict, precos_eq: dict, vestido: dict | None,
           custos_scrap: dict | None, preco_scrap: float | None,
           taxa: float = config.TAXA_MERCADO_PADRAO) -> dict[str, list[dict]]:
    """
    Por slot: as seis raridades ao roll MEDIO e ao preco de mercado, mais a
    peca que o jogador tem vestida com o roll dela e sem custo de compra.

    O desgaste (`liquido`) e o mesmo para as duas — repor uma peca custa o
    preco de mercado dela, tenha ela sido comprada ou nao — e e por isso que a
    peca que ja tens nao e de graca: so a COMPRA e.
    """
    fora: dict[str, list[dict]] = {s: [] for s in SLOTS}

    def liquido(code, preco):
        rec = pecas.valor_de_reciclagem(code, custos_scrap or {}, preco_scrap, taxa)
        return max(0.0, float(preco) - float((rec or {}).get("partida") or 0.0))

    for code, item in (itens or {}).items():
        slot = _slot_do_item(code, item or {})
        preco = (precos_eq or {}).get(code)
        faixas = pecas.intervalos(code, itens)
        if not slot or not faixas or not preco:
            continue
        fora[slot].append({
            "codigo": code, "slot": slot, "origem": "comprar",
            "raridade": item.get("rarity"),
            "stats": {s: (lo + hi) / 2.0 for s, (lo, hi) in faixas.items()},
            "capex": float(preco), "preco": float(preco),
            "liquido": liquido(code, preco),
        })

    for slot, peca in (vestido or {}).items():
        if slot not in SLOTS or not isinstance(peca, dict) or not peca.get("code"):
            continue
        code = peca["code"]
        preco = (precos_eq or {}).get(code)
        fora[slot].append({
            "codigo": code, "slot": slot, "origem": "tens",
            "raridade": ((itens or {}).get(code) or {}).get("rarity"),
            "stats": {k: float(v) for k, v in (peca.get("skills") or {}).items()},
            "estado": peca.get("state"),
            "capex": 0.0,
            "preco": float(preco) if preco else None,
            "liquido": liquido(code, preco) if preco else 0.0,
        })
    return fora


def _domina(b: dict, a: dict, stats) -> bool:
    """`b` e pelo menos tao bom como `a` em tudo, e melhor nalguma coisa."""
    if b["faca"] != a["faca"]:
        return False
    chaves = ("capex", "liq_arma", "liq_resto")
    if any(b[k] > a[k] + 1e-12 for k in chaves):
        return False
    if any(b["stats"].get(s, 0.0) < a["stats"].get(s, 0.0) - 1e-12 for s in stats):
        return False
    return (any(b[k] < a[k] - 1e-12 for k in chaves)
            or any(b["stats"].get(s, 0.0) > a["stats"].get(s, 0.0) + 1e-12 for s in stats))


def frente(op: dict[str, list[dict]], slots, stats, arma_obrigatoria: bool) -> list[dict]:
    """
    Todas as maneiras de vestir estes slots, sem as dominadas.

    Dominada e a que custa o mesmo ou mais a comprar, gasta o mesmo ou mais a
    cada golpe, e nao da mais stat nenhum. Tirar essas nao muda nenhuma das
    respostas — mais stat com menos custo nunca e pior, seja qual for o
    objectivo — e faz caber a procura exaustiva em segundos.

    A faca nunca e comparada com as outras armas: e a unica que bate sem
    municao, e isso e uma poupanca que os stats nao mostram.
    """
    conjuntos = [{"capex": 0.0, "liq_arma": 0.0, "liq_resto": 0.0, "stats": {},
                  "pecas": {}, "faca": False}]
    for slot in slots:
        escolhas = list(op.get(slot) or [])
        if not (arma_obrigatoria and slot == "weapon"):
            escolhas = [None] + escolhas
        novos = []
        for o in escolhas:
            for c in conjuntos:
                st = dict(c["stats"])
                if o:
                    for s, v in o["stats"].items():
                        st[s] = st.get(s, 0.0) + v
                novos.append({
                    "capex": c["capex"] + (o["capex"] if o else 0.0),
                    "liq_arma": c["liq_arma"] + (o["liquido"] if o and slot == "weapon" else 0.0),
                    "liq_resto": c["liq_resto"] + (o["liquido"] if o and slot != "weapon" else 0.0),
                    "stats": st,
                    "pecas": {**c["pecas"], **({slot: o} if o else {})},
                    "faca": c["faca"] or bool(o and o["codigo"] == ARMA_SEM_MUNICAO),
                })
        conjuntos = novos
    conjuntos.sort(key=lambda x: (x["capex"], x["liq_arma"] + x["liq_resto"]))
    fr: list[dict] = []
    for c in conjuntos:
        if not any(_domina(f, c, stats) for f in fr):
            fr = [f for f in fr if not _domina(c, f, stats)] + [c]
    return fr


# ---------------------------------------------------------------------------
#  5. Avaliar uma build inteira, pelo motor
# ---------------------------------------------------------------------------


def _vida_da_comida(itens: dict, comida: str) -> float | None:
    v = (((itens or {}).get(comida) or {}).get("flatStats") or {}).get("healthRegenPercent")
    return float(v) if v else None


def contexto(cfg: dict, precos: dict, posto_pct: float = 0.0,
             custos_scrap: dict | None = None, preco_scrap: float | None = None,
             horas_combate: float = HORAS_COMBATE, batalha: float = 1.0,
             horizonte: float = HORIZONTE_DIAS, dinheiro: float | None = None) -> dict:
    """Tudo o que nao muda de uma combinacao para a outra, num sitio so."""
    itens = cfg.get("items") or {}
    return {
        "cfg": cfg, "precos": precos, "horas_combate": float(horas_combate),
        "batalha": float(batalha), "posto_pct": float(posto_pct or 0.0),
        "custos_scrap": custos_scrap or {}, "preco_scrap": preco_scrap,
        "vida_comida": {c: _vida_da_comida(itens, c) for c in COMIDAS},
        "horizonte": float(horizonte), "dinheiro": dinheiro,
        # o fundo do bounty da batalha escolhida: o bounty de um dia nunca passa
        # dele (e e repartido por todos os que batem desse lado)
        "fundo": None,
    }


def _bounty_do_dia(dano: float, taxa_1k: float, fundo: float | None) -> float:
    """O bounty de um dia de dano, que o fundo da batalha limita."""
    b = dano / 1000.0 * float(taxa_1k or 0.0)
    return min(b, float(fundo)) if fundo is not None else b


def avaliar(base: dict, pecas_: dict[str, dict], municao: str | None,
            comida: str, pilula: bool, ctx: dict, rec: dict) -> dict | None:
    """
    Uma build: o motor diz o golpe, quantos cabem e o que custam; aqui somam-se
    as receitas nas unidades delas e nada mais.
    """
    stats = dict(base)
    for p in pecas_.values():
        for s, v in p["stats"].items():
            stats[s] = float(stats.get(s) or 0.0) + v
    vestidas = {s: p["codigo"] for s, p in pecas_.items()}
    r = objetivos.simular_dia(
        stats, ctx["cfg"], municao=municao, pilula_ligada=pilula,
        horas_combate=ctx["horas_combate"], precos=ctx["precos"],
        comida=comida, comida_vida_pct=ctx["vida_comida"].get(comida),
        preco_pilula=ctx["precos"].get("cocain"), batalha=ctx["batalha"],
        posto_pct=ctx["posto_pct"], pecas_vestidas=vestidas,
        custos_scrap=ctx["custos_scrap"], preco_scrap=ctx["preco_scrap"])
    if not r or not r.get("dano_dia"):
        return None
    golpes = float(r["golpes_dia"])
    dano = float(r["dano_dia"])
    k = dano / 1000.0
    bounty_dia = _bounty_do_dia(dano, rec["bounty_1k"], ctx.get("fundo"))
    cases_dia = golpes * rec["por_golpe"]
    pool_dia = k * rec["pool_1k"]
    contrato_dia = k * rec["contrato_1k"]
    receita = bounty_dia + cases_dia + pool_dia + contrato_dia
    custo = float(r["custo_dia"])
    lucro = receita - custo
    capex = sum(p["capex"] for p in pecas_.values())
    det = r.get("custo_detalhe") or {}
    return {
        "pecas": {s: {"codigo": p["codigo"], "origem": p["origem"],
                      "raridade": p.get("raridade"),
                      "stats": {a: round(b, 1) for a, b in p["stats"].items()},
                      "preco": p.get("preco"), "estado": p.get("estado")}
                  for s, p in pecas_.items()},
        "vestidas": vestidas,
        "stats": {s: round(v, 2) for s, v in stats.items()},
        "municao": municao,
        "comida": comida,
        "pilula": bool(pilula),
        "dano_por_golpe": float(r["dano_por_golpe"]),
        # o que se ve no ecra de batalha dentro da janela da pilula: o mesmo
        # golpe, com o buff. Fora dela (e na ressaca) e menos
        "dano_por_golpe_pilula": (float(r["dano_por_golpe"]) * float(
            1 + ((ctx["cfg"].get("items") or {}).get("cocain", {}).get("flatStats", {})
                 .get("percentAttack") or 60) / 100.0) if pilula else None),
        "fator_batalha": float(ctx["batalha"]),
        "golpes_dia": golpes,
        "dano_dia": dano,
        "limitado_por": r.get("limitado_por"),
        "bounty_1k": rec["bounty_1k"],
        "bounty_dia": bounty_dia,
        "cases_dia": cases_dia,
        "pool_dia": pool_dia,
        "contrato_dia": contrato_dia,
        "drops_dia": cases_dia + pool_dia,
        "receita_dia": receita,
        "receita_1k": receita / k if k else None,
        "custo_dia": custo,
        "custo_golpe": float(r["custo_por_golpe"]),
        "custo_municao_golpe": det.get("municao"),
        "custo_comida_golpe": det.get("comida"),
        "custo_desgaste_golpe": det.get("equipamento"),
        "custo_pilula_dia": float(ctx["precos"].get("cocain") or 0.0) if pilula else 0.0,
        "custo_1k": custo / k if k else None,
        "lucro_dia": lucro,
        # as pecas do ranking sao a receita menos certa (dependem da posicao
        # na ronda, e quem bate fraco fica mais abaixo): o lucro sem elas diz
        # se a recomendacao aguenta sem essa parcela
        "lucro_dia_sem_pool": lucro - pool_dia,
        "lucro_golpe": lucro / golpes if golpes else None,
        "lucro_1k": lucro / k if k else None,
        "capex": capex,
        "compras": [p["codigo"] for p in pecas_.values() if p["origem"] == "comprar"],
    }


def _horizonte(linha: dict, dias: float) -> float:
    """Rentabilidade liquida no horizonte: o que sobra depois de comprar."""
    return linha["lucro_dia"] * dias - linha["capex"]


# ---------------------------------------------------------------------------
#  6. A procura, e as estrategias
# ---------------------------------------------------------------------------

ESTRATEGIAS = {
    "atual": {"nome": "O que tens vestido",
              "o_que": "o equipamento e os consumíveis que estás a usar agora"},
    "dano": {"nome": "A · Máximo dano",
             "o_que": "o maior dano por dia, custe o que custar"},
    "por_1k": {"nome": "B · Máximo retorno por 1k de dano",
               "o_que": "bounty + drops − custos, por cada 1000 de dano"},
    "lucro": {"nome": "C · Máxima rentabilidade líquida",
              "o_que": "o que sobra no horizonte, já com as compras pagas"},
    "barata": {"nome": "Sem comprar nada",
               "o_que": "o melhor retorno por 1k de dano só com o que já tens vestido"},
}


def _metade_dano(base: dict, f_dano: list[dict], ctx: dict) -> list[dict]:
    """
    Cada combinacao de arma, luvas e capacete com cada municao: o dano por
    golpe vem do motor e so depende destes tres slots — a armadura e a esquiva
    mexem na vida e nao no golpe.
    """
    from . import simulador

    municoes_arma = [m for m in objetivos.MUNICOES if ctx["precos"].get(m)]
    fora = []
    for a in f_dano:
        st = dict(base)
        for s, v in a["stats"].items():
            st[s] = float(st.get(s) or 0.0) + v
        for mun in (objetivos.MUNICOES_COM_FACA if a["faca"] else municoes_arma):
            g = simulador.simular(st, ctx["cfg"], municao=mun, posto_pct=ctx["posto_pct"],
                                  batalha=ctx["batalha"])["_cru"]
            fora.append({"combo": a, "municao": mun, "dpg": g["dano_por_golpe"],
                         "c_mun": float(ctx["precos"].get(mun) or 0.0) if mun else 0.0})
    return fora


def _metade_defesa(base: dict, f_def: list[dict], ctx: dict) -> list[dict]:
    """
    Cada combinacao de peito, calcas e botas com cada comida: quantos golpes
    cabem no dia e quanta comida gasta cada um. Tambem e o motor, e tambem so
    depende destes tres slots.
    """
    from . import simulador

    comidas = [c for c in COMIDAS if ctx["precos"].get(c) and ctx["vida_comida"].get(c)]
    fora = []
    for b in f_def:
        st = dict(base)
        for s, v in b["stats"].items():
            st[s] = float(st.get(s) or 0.0) + v
        for com in comidas:
            pct = ctx["vida_comida"][com]
            r = simulador.dano_disponivel(st, ctx["cfg"], comida_vida_pct=pct,
                                          posto_pct=ctx["posto_pct"], batalha=ctx["batalha"])
            if not r.get("golpes_dia"):
                continue
            c = objetivos.custo_do_golpe(None, ctx["precos"], r["vida_por_golpe_esperada"],
                                         r.get("vida_maxima") or 0, com, pct)
            fora.append({"combo": b, "comida": com, "golpes": float(r["golpes_dia"]),
                         "c_comida": float(c["comida"]),
                         "poupa": 1.0 - (r.get("esquiva_pct") or 0.0) / 100.0})
    return fora


def metades(base: dict, op: dict[str, list[dict]], ctx: dict) -> dict:
    """
    As duas metades da procura, sem bonus de batalha nenhum.

    O dano por golpe e LINEAR no factor de batalha — o motor multiplica o ataque
    (ja com o transbordo) por ele, e o dano esperado e linear no ataque — e os
    golpes por dia nao dependem dele. Por isso calcula-se tudo uma vez a x1 e
    cada batalha so escala o dano: correr oito batalhas deixa de ser oito vezes
    o motor.
    """
    ctx1 = {**ctx, "batalha": 1.0}
    f_dano = frente(op, GRUPO_DANO, STATS_DANO, arma_obrigatoria=True)
    f_def = frente(op, GRUPO_DEFESA, STATS_DEFESA, arma_obrigatoria=False)
    return {"f_dano": f_dano, "f_def": f_def,
            "md": _metade_dano(base, f_dano, ctx1), "mf": _metade_defesa(base, f_def, ctx1)}


def procurar(base: dict, op: dict[str, list[dict]], ctx: dict, rec: dict,
             atual: dict | None = None, pre: dict | None = None) -> dict:
    """
    Todas as combinacoes nao dominadas de equipamento, cada uma com todas as
    municoes, as tres comidas e com e sem pilula. Nada e escolhido a olho: a
    resposta de cada estrategia e o maximo exacto do conjunto.

    DUAS METADES QUE NAO SE TOCAM. O dano por golpe so depende da arma, das
    luvas e do capacete; os golpes por dia e a comida so dependem do peito,
    das calcas e das botas. Cada metade pede-se ao motor uma vez por
    combinacao, e o cruzamento e so somar o que o motor ja devolveu — o
    desgaste e linear peca a peca (`durabilidade.custo_por_golpe`) e a pilula
    multiplica o dano do dia por um factor que so depende das horas
    (`pilula.avaliar`). Pedir o dia inteiro ao motor para cada uma das ~1,4
    milhoes de combinacoes demorava quatro minutos; assim sao segundos.

    As vencedoras voltam a passar pelo motor inteiro (`avaliar`) e sao esses os
    numeros que saem. A diferenca entre os dois caminhos vem em `verificacao`,
    e ha um teste que exige que seja zero.
    """
    from . import pilula as _pil

    t0 = time.perf_counter()
    pre = pre or metades(base, op, ctx)
    f_dano, f_def, mf = pre["f_dano"], pre["f_def"], pre["mf"]
    escala = float(ctx.get("batalha") or 1.0)
    md = [{**d, "dpg": d["dpg"] * escala} for d in pre["md"]]

    preco_pil = float(ctx["precos"].get("cocain") or 0.0)
    pilulas = [(False, 1.0, 0.0)]
    if preco_pil:
        f = _pil.avaliar(1000.0, 1000.0, ctx["horas_combate"], cfg=ctx["cfg"])
        if "erro" not in f and f.get("multiplicador_medio"):
            pilulas.append((True, float(f["multiplicador_medio"]), preco_pil))

    dias = ctx["horizonte"]
    dinheiro = ctx.get("dinheiro")
    teto = float("inf") if dinheiro is None else float(dinheiro) + 1e-9
    rg = rec["por_golpe"]
    # o bounty fica a parte porque o fundo o limita; o resto e linear no dano
    r_b = rec["bounty_1k"] / 1000.0
    r_o = (rec["pool_1k"] + rec["contrato_1k"]) / 1000.0
    fundo = ctx.get("fundo")
    sem_teto = fundo is None
    melhor = {"dano": (None, None), "por_1k": (None, None),
              "lucro": (None, None), "barata": (None, None)}
    # a nuvem para o grafico: por faixa de dano (logaritmica), o melhor lucro
    faixas: dict[int, dict] = {}
    n = 0

    import math
    INF = float("inf")
    teto_b = INF if sem_teto else float(fundo)
    # a metade da defesa achatada em tuplos: o ciclo de dentro corre milhoes de
    # vezes e cada procura num dicionario conta
    mfl = []
    for j, f in enumerate(mf):
        lr = f["combo"]["liq_resto"] / 100.0
        mfl.append((f["golpes"], f["c_comida"] + lr * f["poupa"], f["poupa"],
                    f["combo"]["capex"], math.log10(f["golpes"]) if f["golpes"] > 0 else -INF, j))

    # A nao depende do dinheiro nem dos custos: e o maior dano por golpe vezes o
    # maior numero de golpes, e as duas metades escolhem-se cada uma por si
    melhor_d = max(((d["dpg"] * fator, i, com) for i, d in enumerate(md)
                    for com, fator, _ in pilulas), default=None)
    melhor_f = max(((f["golpes"], j) for j, f in enumerate(mf)), default=None)
    if melhor_d and melhor_f:
        melhor["dano"] = (melhor_d[0] * melhor_f[0], (melhor_d[1], melhor_f[1], melhor_d[2]))

    b_v, b_k = -INF, None       # B: retorno por 1k, dentro do dinheiro
    c_v, c_k = -INF, None       # C: rentabilidade no horizonte, so com lucro
    z_v, z_k = -INF, None       # sem comprar nada
    faixa_v: dict[int, float] = {}
    faixa_x: dict[int, tuple] = {}
    for i, d in enumerate(md):
        cd = d["combo"]
        fixo_d = d["c_mun"] + cd["liq_arma"] / 100.0
        rd = cd["liq_resto"] / 100.0
        cap_d = cd["capex"]
        if cap_d > teto:
            n += len(mfl) * len(pilulas)
            continue
        for com_pil, fator, c_pil in pilulas:
            dpg = d["dpg"] * fator
            if dpg <= 0:
                continue
            ld = math.log10(dpg)
            gg = rg + dpg * r_o - fixo_d
            for golpes, a_, poupa, cap_f, lg, j in mfl:
                n += 1
                cap = cap_d + cap_f
                if cap > teto:
                    continue
                dano = golpes * dpg
                b = dano * r_b
                if b > teto_b:
                    b = teto_b
                lucro = golpes * (gg - a_ - rd * poupa) + b - c_pil
                l1k = lucro / dano
                if l1k > b_v:
                    b_v, b_k = l1k, (i, j, com_pil)
                if lucro > 0:
                    v = lucro * dias - cap
                    if v > c_v:
                        c_v, c_k = v, (i, j, com_pil)
                if cap == 0 and l1k > z_v:
                    z_v, z_k = l1k, (i, j, com_pil)
                k = int((lg + ld) * 20)
                if lucro > faixa_v.get(k, -INF):
                    faixa_v[k] = lucro
                    faixa_x[k] = (dano, lucro, l1k * 1000.0, cap)
    melhor["por_1k"] = (b_v * 1000.0, b_k) if b_k else (None, None)
    melhor["lucro"] = (c_v, c_k) if c_k else (None, None)
    melhor["barata"] = (z_v * 1000.0, z_k) if z_k else (None, None)
    faixas = {k: {"dano_dia": x[0], "lucro_dia": x[1], "lucro_1k": x[2], "capex": x[3]}
              for k, x in faixa_x.items()}

    def reavaliar(chave):
        if chave is None:
            return None
        i, j, com_pil = chave
        d, f = md[i], mf[j]
        pecas_ = {**d["combo"]["pecas"], **f["combo"]["pecas"]}
        return avaliar(base, pecas_, d["municao"], f["comida"], com_pil, ctx, rec)

    fora, verif = {}, {}
    for nome, (valor, chave) in melhor.items():
        l = reavaliar(chave)
        fora[nome] = l
        if l is not None:
            pelo_motor = {"dano": l["dano_dia"], "por_1k": l["lucro_1k"],
                          "lucro": _horizonte(l, dias), "barata": l["lucro_1k"]}[nome]
            verif[nome] = {"procura": valor, "motor": pelo_motor,
                           "diferenca_pct": (abs(pelo_motor - valor) / abs(pelo_motor) * 100
                                             if pelo_motor else 0.0)}
    if atual:
        fora["atual"] = atual

    nuvem = [{k: round(v, 4) for k, v in x.items()}
             for _, x in sorted(faixas.items())]
    return {
        "estrategias": fora,
        "avaliadas": n,
        "combos_dano": len(f_dano),
        "combos_defesa": len(f_def),
        "segundos": round(time.perf_counter() - t0, 2),
        "nuvem": nuvem,
        "verificacao": verif,
    }


def _com_horizonte(l: dict | None, dias: float, atual: dict | None) -> dict | None:
    if not l:
        return None
    x = dict(l)
    x["rentabilidade_horizonte"] = _horizonte(l, dias)
    x["horizonte_dias"] = dias
    if atual and atual is not l:
        ganho = l["lucro_dia"] - atual["lucro_dia"]
        x["ganho_lucro_dia"] = ganho
        x["ganho_dano_dia"] = l["dano_dia"] - atual["dano_dia"]
        x["ganho_dano_pct"] = ((l["dano_dia"] / atual["dano_dia"] - 1) * 100
                               if atual["dano_dia"] else None)
        x["payback_dias"] = (l["capex"] / ganho if ganho > 0 and l["capex"] > 0
                             else 0.0 if l["capex"] == 0 and ganho > 0 else None)
    esq = float((l["stats"].get("dodge") or 0.0))
    esq_ef = esq / (esq + 40.0) if esq > 0 else 0.0
    rep = durabilidade.golpes_ate_repor(l["vestidas"], esq_ef)
    x["sustento"] = {
        "limitado_por": l["limitado_por"],
        "primeira_a_partir": rep.get("primeira_a_partir"),
        "pecas_por_dia": round(sum(l["golpes_dia"] / p["golpes"] for p in rep["pecas"]), 2)
        if rep["pecas"] else 0.0,
    }
    return x


def _vantagens(chave: str, l: dict, atual: dict | None) -> dict:
    """O que cada estrategia ganha e o que perde, em palavras e com numeros."""
    pro, contra = [], []
    if l["lucro_dia"] > 0:
        pro.append(f"dá lucro: {_n(l['lucro_dia'])} por dia")
    else:
        contra.append(f"dá prejuízo: {_n(l['lucro_dia'])} por dia")
    if l["capex"] == 0:
        pro.append("não precisa de comprar nada")
    else:
        contra.append(f"precisa de {_n(l['capex'])} em compras")
    if atual and l is not atual:
        if l["dano_dia"] < atual["dano_dia"] * 0.9:
            contra.append(f"faz {(1 - l['dano_dia'] / atual['dano_dia']) * 100:.0f}% "
                          "menos dano do que hoje — conta menos para o ranking e para a guerra")
        elif l["dano_dia"] > atual["dano_dia"] * 1.05:
            pro.append(f"faz {(l['dano_dia'] / atual['dano_dia'] - 1) * 100:.0f}% mais dano")
    if l["custo_desgaste_golpe"] and l["custo_golpe"] and \
            l["custo_desgaste_golpe"] > 0.5 * l["custo_golpe"]:
        contra.append("mais de metade do custo é o desgaste das peças")
    if l["cases_dia"] > l["bounty_dia"] and chave != "dano":
        pro.append("vive das cases, que caem por golpe e não por dano")
    return {"vantagens": pro, "desvantagens": contra}


def proximo_nivel(user: dict, cfg: dict, base: dict, op_linha: dict | None,
                  ctx: dict, hist: dict, preco_venda, loot_chance: float,
                  bounty_1k: float, usar_contrato: bool) -> dict:
    """
    O nivel nao desbloqueia equipamento — nenhuma peca do `gameConfig` tem
    requisito de nivel — so da pontos de skill. Por isso a pergunta certa e:
    com os pontos que ficam no proximo nivel, onde rende mais o ponto?
    """
    lev = user.get("leveling") or {}
    nivel = int(lev.get("level") or 0)
    livres = int(lev.get("availableSkillPoints") or 0)
    total = int(lev.get("totalSkillPoints") or 0)
    por_nivel = round(total / nivel) if nivel else 0
    livres_seguinte = livres + por_nivel
    skills_cfg = cfg.get("skills") or {}
    skills_user = user.get("skills") or {}
    linhas = []
    if not op_linha:
        return {"nivel": nivel, "pontos_livres": livres, "pontos_por_nivel": por_nivel,
                "pontos_no_seguinte": livres_seguinte, "linhas": []}
    pecas_ = {s: {"codigo": p["codigo"], "origem": p["origem"], "raridade": p.get("raridade"),
                  "stats": p["stats"], "capex": 0.0, "preco": p.get("preco"),
                  "estado": p.get("estado")}
              for s, p in op_linha["pecas"].items()}
    rec0 = receitas(hist, preco_venda, loot_chance, bounty_1k, usar_contrato)
    for s in SKILLS_A_SUBIR:
        lv = int((skills_user.get(s) or {}).get("level") or 0)
        escada = (skills_cfg.get(s) or {}).get("levels") or {}
        agora, prox = escada.get(str(lv)), escada.get(str(lv + 1))
        if not agora or not prox:
            continue
        custo = int(prox.get("totalCost") or 0) - int(agora.get("totalCost") or 0)
        delta = float(prox.get("value") or 0) - float(agora.get("value") or 0)
        b2 = dict(base)
        rec = rec0
        if s == "lootChance":
            rec = receitas(hist, preco_venda, loot_chance, bounty_1k, usar_contrato,
                           escala_loot=(loot_chance + delta) / loot_chance if loot_chance else 1.0)
        else:
            b2[s] = float(b2.get(s) or 0.0) + delta
        l = avaliar(b2, pecas_, op_linha["municao"], op_linha["comida"],
                    op_linha["pilula"], ctx, rec)
        if not l:
            continue
        linhas.append({
            "skill": s, "nivel": lv, "para": lv + 1, "custo_pontos": custo,
            "ganho_stat": delta,
            "da_agora": custo <= livres,
            "da_no_seguinte": custo <= livres_seguinte,
            "ganho_lucro_dia": l["lucro_dia"] - op_linha["lucro_dia"],
            "ganho_dano_dia": l["dano_dia"] - op_linha["dano_dia"],
        })
    linhas.sort(key=lambda x: (not x["da_no_seguinte"], -x["ganho_lucro_dia"]))
    return {"nivel": nivel, "pontos_livres": livres, "pontos_por_nivel": por_nivel,
            "pontos_no_seguinte": livres_seguinte,
            "nada_agora": not any(x["da_agora"] for x in linhas),
            "linhas": linhas}


def _n(v: float, casas: int = 2, sinal: bool = False) -> str:
    """Um numero como o painel os escreve: virgula decimal, espaco nos milhares."""
    s = f"{v:+,.{casas}f}" if sinal else f"{v:,.{casas}f}"
    return s.replace(",", " ").replace(".", ",")


def _consumiveis(l: dict) -> str:
    arma = f"de faca (sem munição), com {l['comida']}" if not l["municao"] \
        else f"com {l['municao']} e {l['comida']}"
    return arma + (", e pílula" if l["pilula"] else ", sem pílula")


def frase_ordens(ordens: list[dict], mu: dict | None, alvo: dict | None,
                 leiloes: list[dict] | None = None) -> list[str]:
    """O que a MU e o pais do jogador mandam fazer, ao lado do que rende."""
    fora = []
    nome_mu = (mu or {}).get("nome") or "a tua MU"
    da_mu = [o for o in ordens if o["da_mu"]]
    do_pais = [o for o in ordens if o["do_pais"]]
    if mu and mu.get("nome"):
        fora.append(
            f"Estás na MU {nome_mu} ({mu['papel']}, {mu['membros']} membros, "
            f"quartel-general nível {(mu.get('construcoes') or {}).get('headquarters', 0)}). "
            f"Ela tem ordem em {len(da_mu)} lado(s) de batalha e o teu país em {len(do_pais)}.")
    if not ordens:
        fora.append("Nem o teu país nem a tua MU têm ordens activas agora: o bónus das "
                    "ordens não entra em nenhuma batalha.")
        return fora
    if alvo:
        aqui = next((o for o in ordens if o["batalha"] == alvo["batalha"]
                     and o["lado"] == alvo["lado"]), None)
        if aqui:
            quem = [x for x, s in (("o teu país", aqui["do_pais"]),
                                   (nome_mu, aqui["da_mu"])) if s]
            fora.append(f"A batalha recomendada é uma das que {' e '.join(quem)} "
                        f"{'mandam' if len(quem) > 1 else 'manda'} bater.")
        else:
            melhor = max((o for o in ordens if o.get("melhor_lucro_dia") is not None),
                         key=lambda o: o["melhor_lucro_dia"], default=None)
            if melhor:
                fora.append(
                    f"A recomendada não tem ordem tua. Onde a ordem manda, a melhor é "
                    f"{melhor['pais_nome']} contra {melhor['contra']} (×{_n(melhor['fator'])}): "
                    f"{_n(melhor['melhor_lucro_dia'])} por dia com a melhor build.")
    ganha = [x for x in (leiloes or []) if x.get("a_tua_mu_ganha")]
    if ganha:
        fora.append(f"A tua MU está a ganhar {len(ganha)} leilão(ões) de contrato de "
                    "mercenário: o pagamento é da MU, e não entra no bounty.")
    elif mu and mu.get("pode_contratos_profissionais"):
        fora.append(f"A reputação de mercenário da MU ({_n(mu['reputacao_mercenario'], 1)}) "
                    f"passa o mínimo ({_n(mu['limiar_profissionais'], 0)}) dos contratos só "
                    "para profissionais.")
    return fora


def _nome_componente(chave: str) -> str:
    return {"patriotico": "patriótico", "ordem_pais": "ordem do país",
            "ordem_mu": "ordem da MU", "quartel_general": "quartel-general",
            "base_militar": "base militar", "bunker": "bunker"}.get(chave, chave)


def recomendacao(nome: str, est: dict, atual: dict | None, lado: dict | None,
                 hist: dict, alvo: dict | None = None) -> list[str]:
    """A resposta em frases, com os numeros dentro. Sem numero nao ha frase."""
    fora = []
    if alvo:
        cx = alvo.get("contexto") or {}
        partes = ", ".join(f"{_nome_componente(c['chave'])} +{_n(c['pct'], 0)}%"
                           for c in cx.get("componentes") or []) or "nenhum bónus"
        abre = ("Na batalha que escolheste," if alvo.get("escolhida")
                else "A batalha onde rendes mais agora é")
        teto = (f", cortado no tecto de {_n(cx['fator'] * 100 - 100, 0)}% "
                f"(a soma dava {_n(sum(c['pct'] for c in cx.get('componentes') or []), 0)}%)"
                if cx.get("cortado_pelo_teto") else "")
        fora.append(
            f"{abre} {alvo['pais_nome']} contra "
            f"{alvo['contra']}, do lado {'do ataque' if alvo['lado'] == 'attacker' else 'da defesa'}"
            + (": lá" if alvo.get("escolhida") else ". Lá")
            + f" o teu dano leva ×{_n(cx.get('fator', 1.0))} ({partes}{teto})"
            + (f" e o bounty paga {_n(alvo['por_1k'], 3)} por 1k" if alvo.get("paga")
               else ", mas não paga bounty") + ".")
    if lado and lado.get("fundo") is not None and lado.get("dano_ate_esgotar") is not None:
        fora.append(
            f"O bounty por 1000 de dano não depende do equipamento: é a batalha que o "
            f"fixa. Nessa o fundo tem {_n(lado['fundo'], 0)} — chega para "
            f"{_n(lado['dano_ate_esgotar'] / 1e6)}M de dano, a repartir por todos os "
            "que batem desse lado, e o bounty de um dia nunca passa dele.")
    elif not alvo:
        fora.append("Neste momento nenhuma batalha activa tem bounty a pagar com fundo.")
    if hist.get("bounty_por_1k") is not None:
        fora.append(
            f"No teu histórico recebeste {_n(hist['bounty_por_1k'], 4)} por 1k de dano "
            f"em bounty ({_n(hist['bounty'])} em {_n(hist['dano'] / 1e6, 1)}M de dano): "
            "escolher a batalha certa vale mais do que qualquer peça.")
    c = est.get("lucro")
    b = est.get("por_1k")
    if c:
        pecas_txt = ", ".join(f"{p['codigo']}" + (" (já tens)" if p["origem"] == "tens" else "")
                              for p in c["pecas"].values())
        frase = (f"Para {nome}, a combinação mais rentável é {pecas_txt}, "
                 f"{_consumiveis(c)}: {_n(c['lucro_dia'])} de lucro por dia "
                 f"({_n(c['lucro_1k'], 3)} por 1k de dano)")
        if atual:
            frase += f", contra {_n(atual['lucro_dia'])} por dia com o que tens vestido"
        if c["capex"] > 0:
            frase += f". As compras custam {_n(c['capex'])}"
            if c.get("payback_dias") is not None:
                horas = c["payback_dias"] * 24
                frase += (f" e pagam-se em {_n(horas, 1)} horas" if horas < 48
                          else f" e pagam-se em {_n(c['payback_dias'], 1)} dias")
        fora.append(frase + ".")
        if c["pool_dia"] > 0:
            sem = c["lucro_dia_sem_pool"]
            fora.append(
                f"Mesmo sem as peças do ranking (a receita menos certa, porque dependem "
                f"da posição na ronda) fica {_n(sem)} por dia"
                + (" — a recomendação aguenta." if sem > 0
                   else " — sem elas dá prejuízo, por isso depende de ficares bem no ranking."))
    else:
        fora.append("Nenhuma combinação dá lucro aos preços de agora: combater é uma "
                    "despesa, e a que perde menos por 1k de dano é a B.")
    if b and c and b["vestidas"] != c["vestidas"]:
        fora.append(
            f"A que rende mais por cada 1000 de dano (B) não é a mesma: "
            f"{_n(b['lucro_1k'], 3)} por 1k, mas só {_n(b['dano_dia'] / 1e3, 0)}k de dano "
            "por dia — rende mais por dano porque bate pouco e barato, e as cases caem "
            "por golpe.")
    a = est.get("dano")
    if a and atual:
        fora.append(
            f"O máximo dano (A) faz {_n(a['dano_dia'] / 1e6)}M por dia "
            f"({_n((a['dano_dia'] / atual['dano_dia'] - 1) * 100, 0, sinal=True)}% face a "
            f"hoje) e custa {_n(a['custo_1k'], 3)} por 1k: {_n(a['lucro_dia'])} por dia.")
    if not atual:
        fora.append("Não foi possível ler o que tens vestido "
                    "(`inventory.fetchCurrentEquipment`), por isso não há comparação com "
                    "hoje — volta a carregar daqui a um minuto.")
    return fora


# ---------------------------------------------------------------------------
#  7. O dano depende da batalha: o contexto de cada lado, lido do jogo
# ---------------------------------------------------------------------------

LADOS = ("attacker", "defender")


def _outro(lado: str) -> str:
    return "defender" if lado == "attacker" else "attacker"


def _pct_construcao(cfg: dict, tipo: str, stat: str, nivel) -> float:
    """O bonus de uma construcao neste nivel, tirado do `upgradesConfig`."""
    niveis = (((cfg or {}).get("upgradesConfig") or {}).get(tipo) or {}).get("levels") or {}
    v = (((niveis.get(str(int(nivel or 0))) or {}).get("stats")) or {}).get(stat)
    return float(v) if isinstance(v, (int, float)) else 0.0


def contexto_de_lado(b: dict, lado: str, user: dict, cfg: dict,
                     ordens: list[dict] | None = None, hq_nivel: int = 0,
                     construcao_nivel: int | None = None) -> dict:
    """
    O bonus de batalha que ESTE jogador tem NESTE lado, componente a componente.

    O motor sem bonus dava 2 815 de dano por golpe a um jogador cujos resumos de
    loot dizem ~6 000: faltava o contexto. Aqui le-se do jogo o que se pode ler:

        patriotico      o lado e o pais dele
        ordem do pais   o pais dele tem ordem neste lado (`battleOrder`), com a
                        prioridade
        ordem da MU     a unidade militar dele tem ordem neste lado
        quartel-general o nivel do QG da MU dele (`upgrade`, headquarters)
        base / bunker   a construcao da regiao: base de quem ataca, bunker de
                        quem defende

    O que a API nao deixa ver fica de fora e dito: a quota da alianca (pode dar
    -20% a +20%), a ideologia do pais, e as penalizacoes de regiao.
    """
    pais, mu = user.get("country"), user.get("mu")
    s, o = b.get(lado) or {}, b.get(_outro(lado)) or {}
    contexto = ("defesa" if lado == "defender"
                else "revolta" if b.get("type") == "resistance" else "ataque")
    op = om = None
    for x in ordens or []:
        if pais and x.get("country") == pais:
            op = x.get("priority") or "high"
        if mu and x.get("mu") == mu:
            om = x.get("priority") or "high"
    qg = _pct_construcao(cfg, "headquarters", "attackBonus", hq_nivel)
    base_pct = bunker_pct = 0.0
    if construcao_nivel:
        if lado == "defender":
            bunker_pct = _pct_construcao(cfg, "bunker", "defenseBonus", construcao_nivel)
        else:
            base_pct = _pct_construcao(cfg, "base", "attackBonus", construcao_nivel)
    r = bonus.calcular(cfg, contexto=contexto, patriotico=s.get("country") == pais,
                       qg_nivel_pct=qg or None, ordem_pais_prioridade=op,
                       ordem_mu_prioridade=om, base_militar_pct=base_pct or None,
                       bunker_pct=bunker_pct or None)
    return {
        "fator": r["fator"],
        "total_pct": r["total_pct"],
        "cortado_pelo_teto": r["cortado_pelo_teto"],
        "contexto": contexto,
        "patriota": s.get("country") == pais,
        "contra_o_teu_pais": bool(pais) and o.get("country") == pais,
        "componentes": [{"chave": c["chave"], "pct": c["pct"], "porque": c["porque"]}
                        for c in r["componentes"] if c["aplicavel"]],
        "construcao_lida": construcao_nivel is not None,
        "fora_da_conta": ["aliança (a quota não está na API)",
                          "ideologia do país (`rulingParty` não se resolve)",
                          "penalizações de região"]
                         + ([] if construcao_nivel is not None
                            else ["base/bunker da região (não lido para este lado)"])
                         + (["resistência (a barra não está na API)"]
                            if contexto == "revolta" else []),
    }


def lados_onde_bater(batalhas: list[dict], user: dict, nomes: dict | None = None,
                     agora: datetime | None = None) -> list[dict]:
    """
    Todos os lados de batalha activa em que o jogador pode bater, com o bounty
    de cada um (ou a falta dele). Contra o proprio pais nao se bate.
    """
    nomes = nomes or {}
    com_bounty = {(l["batalha"], l["lado"]): l for l in lados_com_bounty(batalhas, nomes, agora)}
    fora = []
    for b in batalhas or []:
        if b.get("isActive") is False:
            continue
        for lado in LADOS:
            s, o = b.get(lado) or {}, b.get(_outro(lado)) or {}
            if user.get("country") and o.get("country") == user.get("country"):
                continue
            l = com_bounty.get((b.get("_id"), lado))
            fora.append({
                "batalha": b.get("_id"), "lado": lado, "tipo": b.get("type"),
                "pais": s.get("country"), "pais_nome": nomes.get(s.get("country"), s.get("country")),
                "contra": nomes.get(o.get("country"), o.get("country")),
                "regiao": s.get("region") if lado == "attacker" else (b.get("defender") or {}).get("region"),
                "por_1k": l["por_1k"] if l else 0.0,
                "fundo": l["fundo"] if l else None,
                "dano_ate_esgotar": l["dano_ate_esgotar"] if l else None,
                "estado": l["estado"] if l else "sem bounty",
                "paga": bool(l and l["paga"]),
                "dano_do_lado": float(s.get("damages") or 0.0),
                "tem_ordem_pais": user.get("country") in (s.get("countryOrders") or []),
                "tem_ordem_mu": bool(user.get("mu")) and user.get("mu") in (s.get("muOrders") or []),
                "_b": b,
            })
    return fora


ESTADO_QUASE_PARTIDA = 20


def avisos_do_equipamento(vestido: dict, equip: dict | None,
                          sem_preco: list[str] | None = None) -> list[str]:
    """
    O que o jogador tem de saber antes de bater, e que a procura nao diz.

    Sem arma vestida nao ha "hoje" para comparar — e isso acontece mesmo: a
    arma gasta-se a 1% por golpe e e a primeira a partir. Dizer "sem
    equipamento" quando faltava so a arma escondia o que havia para fazer.
    """
    fora = []
    if equip is None:
        fora.append("não foi possível ler o equipamento vestido "
                    "(inventory.fetchCurrentEquipment não respondeu)")
    elif "weapon" not in (vestido or {}):
        fora.append("não tens arma vestida — a última partiu-se ou foi tirada; sem ela "
                    "não há comparação com hoje, e a recomendação diz o que comprar")
    quase = sorted((p.get("state"), s, p.get("code")) for s, p in (vestido or {}).items()
                   if isinstance(p, dict) and isinstance(p.get("state"), (int, float))
                   and p["state"] < ESTADO_QUASE_PARTIDA)
    if quase:
        fora.append("quase a partir: " + ", ".join(
            f"{c} a {e:.0f}% (~{e:.0f} golpes)" for e, s, c in quase))
    if sem_preco:
        fora.append(f"sem preço para {', '.join(sem_preco)}: essas peças ficaram fora "
                    "da procura")
    return fora


def mu_do_jogador(user: dict, cfg: dict | None = None) -> dict | None:
    """
    A unidade militar do jogador: quem e, que papel ele tem la, o que ela
    construiu e se pode entrar nos contratos so para profissionais.

    E a MU que da metade do bonus que se ve nas batalhas — o quartel-general e
    a ordem da MU — por isso e a primeira coisa a saber antes de escolher onde
    bater.
    """
    mu_id = user.get("mu")
    if not mu_id:
        return None
    m = api.cached(f"bounty:mu:{mu_id}", config.TTL_CACHE_CONFIG,
                   lambda: api.safe("mu.getById", {"muId": mu_id}, {}) or {})
    if not m:
        return {"id": mu_id, "nome": None, "erro": "mu.getById não respondeu"}
    uid = user.get("_id")
    papeis = m.get("roles") or {}
    papel = ("dono" if m.get("user") == uid else
             "gestor" if uid in (papeis.get("managers") or []) else
             "comandante" if uid in (papeis.get("commanders") or []) else "membro")
    limiar = ((((cfg or {}).get("mercenaryContract") or {}).get("reputation") or {})
              .get("professionalsOnlyThreshold"))
    rep = m.get("mercenaryReputation")
    rk = m.get("rankings") or {}
    return {
        "id": mu_id,
        "nome": m.get("name"),
        "avatar": m.get("avatarUrl"),
        "membros": len(m.get("members") or []),
        "papel": papel,
        "construcoes": m.get("activeUpgradeLevels") or {},
        "reputacao_mercenario": rep,
        "limiar_profissionais": limiar,
        "pode_contratos_profissionais": (rep is not None and limiar is not None
                                         and float(rep) >= float(limiar)),
        "dano_semanal": (rk.get("muWeeklyDamages") or {}).get("value"),
        "posicao_semanal": (rk.get("muWeeklyDamages") or {}).get("rank"),
    }


def ordens_do_jogador(onde: list[dict], ordens_por_lado: dict, user: dict) -> list[dict]:
    """
    Onde e que o pais do jogador e a MU dele mandam bater agora, com a
    prioridade de cada ordem — e quantos outros paises e MUs mandam o mesmo,
    que e o que diz onde esta o esforco de guerra.
    """
    pais, mu = user.get("country"), user.get("mu")
    fora = []
    for l in onde:
        if not (l.get("tem_ordem_pais") or l.get("tem_ordem_mu")):
            continue
        lista = ordens_por_lado.get((l["batalha"], l["lado"])) or []
        s = (l.get("_b") or {}).get(l["lado"]) or {}
        cx = l.get("contexto") or {}
        comp = {c["chave"]: c for c in cx.get("componentes") or []}
        fora.append({
            "batalha": l["batalha"], "lado": l["lado"], "tipo": l.get("tipo"),
            "pais_nome": l["pais_nome"], "contra": l["contra"],
            "do_pais": bool(l.get("tem_ordem_pais")),
            "da_mu": bool(l.get("tem_ordem_mu")),
            "prioridade_pais": next((x.get("priority") for x in lista
                                     if pais and x.get("country") == pais), None),
            "prioridade_mu": next((x.get("priority") for x in lista
                                   if mu and x.get("mu") == mu), None),
            "pct_pais": (comp.get("ordem_pais") or {}).get("pct"),
            "pct_mu": (comp.get("ordem_mu") or {}).get("pct"),
            "paises_com_ordem": len(s.get("countryOrders") or []),
            "mus_com_ordem": len(s.get("muOrders") or []),
            "fator": cx.get("fator"),
            "por_1k": l.get("por_1k"), "estado": l.get("estado"), "paga": l.get("paga"),
            "melhor_lucro_dia": (((l.get("avaliada") or {}).get("melhor")) or {}).get("lucro_dia"),
            "recomendada": bool(l.get("recomendada")),
        })
    fora.sort(key=lambda x: (not (x["do_pais"] and x["da_mu"]), -(x["fator"] or 0)))
    return fora


def golpes_recentes(batalhas: list[dict], user: dict, cfg: dict,
                    contextos: dict, maximo_rondas: int = 4) -> list[dict]:
    """
    Os golpes que o jogador deu agora mesmo, ao lado do que o modelo previa.

    E a validacao mais dura que ha: `round.getLastHits` da o dano de cada golpe,
    se foi critico ou falhado, e com que arma. O `skills.attack.total` do jogo ja
    traz a municao, a pilula e o posto, por isso o previsto e so isso vezes o
    bonus do contexto (e o dano critico ou a meia falha). Sem golpes recentes
    nao ha nada para mostrar — e diz-se.
    """
    uid = user.get("_id")
    sk = user.get("skills") or {}
    total = float((sk.get("attack") or {}).get("total") or 0.0)
    cd = float((sk.get("criticalDamages") or {}).get("total") or 0.0)
    fora = []
    meus = [b for b in batalhas if any((b.get(l) or {}).get("country") == user.get("country")
                                       for l in LADOS)][:maximo_rondas]
    for b in meus:
        rid = (b.get("currentRound") or {}).get("_id") if isinstance(b.get("currentRound"), dict) \
            else b.get("currentRound")
        if not rid:
            continue
        lh = api.safe("round.getLastHits", {"roundId": rid}, {}) or {}
        for lado in LADOS:
            for h in (lh.get(lado) if isinstance(lh, dict) else []) or []:
                if h.get("user") != uid:
                    continue
                ctx = contextos.get((b["_id"], lado))
                if not ctx or not total:
                    continue
                tipo = "falha" if h.get("isMissed") else "crítico" if h.get("isCriticalHit") else "normal"
                mult = 0.5 if tipo == "falha" else (1 + cd / 100.0) if tipo == "crítico" else 1.0
                previsto = total * mult * ctx["fator"]
                fora.append({"batalha": b["_id"], "lado": lado, "tipo": tipo,
                             "dano": h.get("damages"), "previsto": previsto,
                             "razao": (h.get("damages") or 0) / previsto if previsto else None,
                             "quando": h.get("hitAt"),
                             "arma": (h.get("weapon") or {}).get("code"), "municao": h.get("ammo")})
    return fora


# ---------------------------------------------------------------------------
#  8. Ir buscar tudo, para um jogador
# ---------------------------------------------------------------------------


def stats_sem_equipamento(user: dict) -> dict[str, float]:
    """
    O que a build da sem peca nenhuma: `value` (a skill) mais `strategic` (o
    bonus do pais). E a base a que se soma cada combinacao de equipamento.

    Confere com o jogo: no jogador analisado, precisao 75 + 10 estrategico + 33
    das luvas = 118, e o jogo diz `limited: -18` e `overflow: 72` no ataque —
    os 18 pontos acima do tecto, a 4 de ataque cada.
    """
    fora = {}
    for s in STATS_BASE:
        o = (user.get("skills") or {}).get(s) or {}
        v = o.get("value")
        if isinstance(v, (int, float)):
            fora[s] = float(v) + float(o.get("strategic") or 0.0)
    return fora


def posto_pct(user: dict) -> float:
    """
    O bonus do posto militar como o jogo o aplica (`militaryRankPercent`).

    A tabela `config.POSTOS_MILITARES` da METADE disto no posto 93 (28,75%
    contra 57,5%), e o `skills.attack.total` do jogo so fecha com o valor do
    jogo — por isso le-se dali e a tabela fica de recurso.
    """
    v = ((user.get("skills") or {}).get("attack") or {}).get("militaryRankPercent")
    if isinstance(v, (int, float)):
        return float(v)
    rank = user.get("militaryRank")
    if isinstance(rank, int) and 1 <= rank <= len(config.POSTOS_MILITARES):
        return float(config.POSTOS_MILITARES[rank - 1][1])
    return 0.0


def _ler_resumos_guardados() -> dict:
    try:
        d = json.loads(CAMINHO_RESUMOS.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _guardar_resumos(d: dict) -> None:
    try:
        CAMINHO_RESUMOS.parent.mkdir(parents=True, exist_ok=True)
        CAMINHO_RESUMOS.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
    except OSError:
        pass


def resumos_do_jogador(user_id: str, pais: str | None, maximo: int = 60) -> list[dict]:
    """
    Os `battleLootSummary` do jogador nas ultimas batalhas do pais dele.

    A API nao tem "batalhas deste jogador", por isso pergunta-se batalha a
    batalha (404 quer dizer que nao bateu ali). As batalhas acabadas nunca
    mudam e ficam guardadas em `saida/`; as activas voltam a ler-se sempre.
    """
    if not pais:
        return []
    batalhas = api.batalhas_do_pais(pais, paginas=3)[:maximo]
    guardados = _ler_resumos_guardados()
    chave = lambda b: f"{b['_id']}:{user_id}"                       # noqa: E731
    falta = [b for b in batalhas
             if b.get("isActive") or chave(b) not in guardados]

    def ler(b):
        return b, api.resumo_loot(b["_id"], user_id)

    with ThreadPoolExecutor(max_workers=min(8, config.MAX_WORKERS)) as ex:
        for b, r in ex.map(ler, falta):
            if r is None:
                # o pedido falhou: nao se guarda "nao bateu aqui" por engano
                continue
            if not b.get("isActive"):
                guardados[chave(b)] = r or {}
            else:
                guardados["_ativa:" + chave(b)] = r or {}
    _guardar_resumos({k: v for k, v in guardados.items() if not k.startswith("_ativa:")})
    fora = []
    for b in batalhas:
        r = guardados.get(chave(b)) or guardados.get("_ativa:" + chave(b))
        if r and r.get("hits"):
            fora.append({"batalha": b["_id"], "ativa": bool(b.get("isActive")), "resumo": r})
    return fora


def analisar_jogador(ref: str, bounty_1k: float | None = None,
                     horizonte: float = HORIZONTE_DIAS,
                     horas_combate: float = HORAS_COMBATE,
                     batalha: float | None = None, usar_contrato: bool = False,
                     limitar_ao_dinheiro: bool = True,
                     alvo: str | None = None) -> dict:
    """
    Tudo junto para um jogador: perfil, bounty, historico, o dano em cada
    batalha activa, e as estrategias na batalha que mais rende.

    `batalha` e um factor de bonus escrito a mao (substitui o de cada lado);
    `alvo` e `<battleId>:<attacker|defender>` para correr num lado escolhido.
    """
    from . import build, craft, mercado as _mercado, precos as _precos

    cfg = api.game_config()
    itens = cfg.get("items") or {}
    uid = build.resolver_user_id(ref)
    user = api.utilizador(uid)
    equip = api.equipamento_vestido(uid)
    if equip is None:
        # uma falha aqui deixava a resposta sem o "hoje" e sem dizer porque
        time.sleep(2)
        equip = api.equipamento_vestido(uid)
    equip = equip or {}
    vestido = {s: v for s, v in equip.items() if s in SLOTS and isinstance(v, dict)}

    # precos: o livro dos consumiveis e das cases, a media da API para o equipamento
    codigos_eq = sorted(c for c, it in itens.items()
                        if it.get("type") in ("equipment", "weapon"))
    # 36 pedidos, um por peca: e a parte cara em limite de pedidos, por isso
    # aproveita o relatorio de mercado se ja estiver feito e guarda 15 minutos
    # (a media de vendas de uma peca nao muda de minuto para minuto)
    rel = api.em_cache("mercado:", config.TTL_CACHE_MERCADO) or {}
    precos_eq = rel.get("precos_equipamento") or api.em_cache("bounty:eq", config.TTL_CACHE_CONFIG)
    if not precos_eq:
        precos_eq = {c: v for c, v in craft.precos_equipamento(codigos_eq).items() if v}
        # so se guarda um catalogo completo: um que tenha falhado a meio deixava
        # pecas de fora da procura durante quinze minutos, sem ninguem saber
        if len(precos_eq) == len(codigos_eq):
            api.cached("bounty:eq", config.TTL_CACHE_CONFIG, lambda: precos_eq)
    sem_preco = [c for c in codigos_eq if c not in precos_eq]
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

    # o bounty
    nomes = {c["_id"]: c.get("name") or c.get("code") for c in api.paises()}
    batalhas = api.batalhas_ativas()
    lados = lados_com_bounty(batalhas, nomes)
    # os contratos de mercenario sao outra coisa: ganha-os uma unidade militar
    # em leilao, e nao quem bate. Mostram-se ao lado, sem entrar na conta
    leiloes = [{"pais": nomes.get(x.get("forCountry"), x.get("forCountry")),
                "lado": x.get("forCountrySide"), "por_1k": x.get("currentPerK"),
                "dano_minimo": x.get("minimumDamage"), "pagamento": x.get("currentPayout"),
                "so_profissionais": x.get("professionalsOnly"), "expira": x.get("expiresAt"),
                "lances": len(x.get("bids") or []),
                "a_tua_mu_ganha": bool(user.get("mu")) and x.get("currentWinner") == user.get("mu")}
               for x in api.leiloes_mercenario(50, "active")]
    leiloes.sort(key=lambda x: (not x["a_tua_mu_ganha"], -(x["por_1k"] or 0)))
    perfil = build.perfil_de_combate(user)

    # o historico
    resumos = resumos_do_jogador(uid, user.get("country"))
    hist = historico(resumos, precos_eq, taxa)

    base = stats_sem_equipamento(user)
    loot_chance = float(base.get("lootChance") or 0.0)
    ctx0 = contexto(cfg, precos_mapa, posto_pct(user), custos_scrap, preco_scrap,
                    horas_combate, 1.0, horizonte,
                    perfil.get("dinheiro") if limitar_ao_dinheiro else None)
    op = opcoes(itens, precos_eq, vestido, custos_scrap, preco_scrap, taxa)
    tens = {s: o for s in SLOTS for o in op.get(s, []) if o["origem"] == "tens"}
    pilula_ativa = "cocain" in ((user.get("buffs") or {}).get("buffCodes") or [])
    mun_atual = equip.get("ammo") if isinstance(equip.get("ammo"), str) else None

    # O DANO DEPENDE DA BATALHA. Cada lado tem o seu bonus para este jogador
    # (patriotico, ordens, QG, base ou bunker), lido do jogo; sem isto o motor
    # dava metade do dano que o jogador faz mesmo.
    onde = lados_onde_bater(batalhas, user, nomes)
    hq = {}
    if user.get("mu"):
        hq = api.cached(f"bounty:hq:{user['mu']}", config.TTL_CACHE_CONFIG,
                        lambda: api.safe("upgrade.getUpgradeByTypeAndEntity",
                                         {"upgradeType": "headquarters", "muId": user["mu"]}, {}) or {})
    hq_nivel = hq.get("level") if hq.get("status") == "active" else 0

    def ordens_de(l):
        if not (l["tem_ordem_pais"] or l["tem_ordem_mu"]):
            return []
        return api.cached(f"bounty:ordens:{l['batalha']}:{l['lado']}", 120,
                          lambda: api.safe("battleOrder.getByBattle",
                                           {"battleId": l["batalha"], "side": l["lado"]}, []) or [])

    def construcao_de(l):
        if not l.get("regiao"):
            return None
        tipo = "bunker" if l["lado"] == "defender" else "base"
        u = api.cached(f"bounty:up:{tipo}:{l['regiao']}", 600,
                       lambda: api.safe("upgrade.getUpgradeByTypeAndEntity",
                                        {"upgradeType": tipo, "regionId": l["regiao"]}, {}) or {})
        return u.get("level") if u.get("status") == "active" else 0

    # as construcoes sao um pedido por regiao: so se leem onde a resposta pode
    # mudar — os lados que pagam bounty, os do pais do jogador, e os lados onde
    # o pais ou a MU dele mandam bater
    importa = [l for l in onde if l["paga"] or l["pais"] == user.get("country")
               or l["tem_ordem_pais"] or l["tem_ordem_mu"]
               or alvo == f"{l['batalha']}:{l['lado']}"]
    ids_importa = {id(l) for l in importa}
    contextos, ordens_por_lado = {}, {}
    for l in onde:
        o = ordens_de(l)
        ordens_por_lado[(l["batalha"], l["lado"])] = o
        l["contexto"] = contexto_de_lado(l["_b"], l["lado"], user, cfg, o,
                                         hq_nivel,
                                         construcao_de(l) if id(l) in ids_importa else None)
        contextos[(l["batalha"], l["lado"])] = l["contexto"]

    pre = metades(base, op, ctx0)

    def correr(l, fator, taxa_b, fundo):
        rec_l = receitas(hist, preco_venda, loot_chance, taxa_b, usar_contrato)
        ctx_l = {**ctx0, "batalha": float(fator), "fundo": fundo}
        atual_l = (avaliar(base, tens, mun_atual, "bread", pilula_ativa, ctx_l, rec_l)
                   if tens.get("weapon") else None)
        return rec_l, ctx_l, atual_l, procurar(base, op, ctx_l, rec_l, atual_l, pre)

    # a procura completa corre nos lados que podem ganhar: os que pagam mais
    # bounty por golpe (taxa x factor) e os melhores do pais do jogador
    pagam = sorted([l for l in importa if l["paga"]],
                   key=lambda l: -l["por_1k"] * l["contexto"]["fator"])[:4]
    ids_pagam = {id(l) for l in pagam}
    proprios = sorted([l for l in importa if l["pais"] == user.get("country")
                       and id(l) not in ids_pagam],
                      key=lambda l: -l["contexto"]["fator"])[:1]
    ids_ja = ids_pagam | {id(l) for l in proprios}
    # e as duas melhores onde ha ordem do pais ou da MU dele: e onde lhe
    # pedem para bater, e a resposta tem de dizer quanto isso rende
    mandadas = sorted([l for l in importa if (l["tem_ordem_pais"] or l["tem_ordem_mu"])
                       and id(l) not in ids_ja],
                      key=lambda l: (-(l["tem_ordem_pais"] + l["tem_ordem_mu"]),
                                     -l["contexto"]["fator"]))[:2]
    pedido = [l for l in importa if alvo == f"{l['batalha']}:{l['lado']}"]
    corridas = pedido or (pagam + proprios + mandadas)
    resultados = []
    for l in corridas:
        f = float(batalha) if batalha is not None else l["contexto"]["fator"]
        taxa_b = float(bounty_1k) if bounty_1k is not None else (l["por_1k"] if l["paga"] else 0.0)
        rec_l, ctx_l, atual_l, p_l = correr(l, f, taxa_b, l["fundo"] if l["paga"] else None)
        c_l = p_l["estrategias"].get("lucro")
        resultados.append((l, rec_l, ctx_l, atual_l, p_l))
        l["avaliada"] = {
            "fator": f,
            "atual": ({k: atual_l[k] for k in ("dano_por_golpe", "dano_por_golpe_pilula",
                                               "dano_dia", "lucro_dia", "bounty_dia")}
                      if atual_l else None),
            "melhor": ({"lucro_dia": c_l["lucro_dia"], "lucro_1k": c_l["lucro_1k"],
                        "dano_dia": c_l["dano_dia"], "bounty_dia": c_l["bounty_dia"],
                        "rentabilidade": _horizonte(c_l, ctx0["horizonte"]),
                        "pecas": [p["codigo"] for p in c_l["pecas"].values()],
                        "municao": c_l["municao"], "comida": c_l["comida"],
                        "pilula": c_l["pilula"]} if c_l else None),
            "maximo_dano": (p_l["estrategias"]["dano"]["dano_dia"]
                            if p_l["estrategias"].get("dano") else None),
        }

    def valor(r):
        c = r[4]["estrategias"].get("lucro")
        if c:
            return (1, _horizonte(c, ctx0["horizonte"]))
        b = r[4]["estrategias"].get("por_1k")
        return (0, b["lucro_1k"] if b else -1e18)

    if resultados:
        escolhido = max(resultados, key=valor)
        lado_l, rec, ctx, atual, p = escolhido
        lado_l["recomendada"] = True
        lado_l["escolhida"] = bool(pedido)
    else:
        lado_l = None
        rec = receitas(hist, preco_venda, loot_chance, float(bounty_1k or 0.0), usar_contrato)
        ctx = {**ctx0, "batalha": float(batalha or 1.0)}
        atual = avaliar(base, tens, mun_atual, "bread", pilula_ativa, ctx, rec) \
            if tens.get("weapon") else None
        p = procurar(base, op, ctx, rec, atual, pre)
    taxa_bounty = rec["bounty_1k"]
    # so um lado que PAGA tem fundo para descrever; um com taxa e sem fundo
    # (`moneyPool` a null) nao paga nada e nao se apresenta como se pagasse
    lado = next((x for x in lados if lado_l and x["paga"] and x["batalha"] == lado_l["batalha"]
                 and x["lado"] == lado_l["lado"]), None)
    est = {k: _com_horizonte(v, ctx["horizonte"], atual) for k, v in p["estrategias"].items()}
    for k, v in est.items():
        if v:
            v.update(_vantagens(k, v, atual))
            v["nome"] = ESTRATEGIAS[k]["nome"]
            v["o_que"] = ESTRATEGIAS[k]["o_que"]

    g_motor = None
    if atual:
        g_motor = atual["dano_por_golpe"]
    nivel = proximo_nivel(user, cfg, base, est.get("lucro") or est.get("atual"), ctx,
                          hist, preco_venda, loot_chance, taxa_bounty, usar_contrato)

    mu_info = mu_do_jogador(user, cfg)
    ordens_jog = ordens_do_jogador(onde, ordens_por_lado, user)

    tabela_precos = []
    for c in bens + sorted(precos_eq):
        q = mp.cotar(c)
        m = livro.get(c) or {}
        tabela_precos.append({
            "codigo": c, "tipo": (itens.get(c) or {}).get("type"),
            "raridade": (itens.get(c) or {}).get("rarity"),
            "compra_ask": m.get("best_ask"), "venda_bid": m.get("best_bid"),
            "referencia": ref_precos.get(c),
            "media_vendas": precos_eq.get(c),
            "usado": q["valor"], "fonte": q["fonte"],
        })

    return {
        "jogador": {
            "id": uid, "nome": user.get("username"),
            "nivel": (user.get("leveling") or {}).get("level"),
            "pais": nomes.get(user.get("country"), user.get("country")),
            "posto": user.get("militaryRank"), "posto_pct": ctx["posto_pct"],
            "pilula_ativa": pilula_ativa,
            "dinheiro": perfil.get("dinheiro"),
            "dano_semanal": perfil.get("dano_semanal"),
            "bounty_total": (((user.get("rankings") or {}).get("userBounty")) or {}).get("value"),
            "mercenario_total": (((user.get("rankings") or {}).get("userMercenaryEarnings")) or {}).get("value"),
            "stats_jogo": {s: ((user.get("skills") or {}).get(s) or {}).get("total")
                           for s in STATS_BASE},
            "stats_base": base,
            "niveis": {s: ((user.get("skills") or {}).get(s) or {}).get("level")
                       for s in SKILLS_A_SUBIR},
            "vestido": {s: {"codigo": v.get("code"), "stats": v.get("skills"),
                            "estado": v.get("state")} for s, v in vestido.items()},
            "municao": mun_atual,
        },
        "bounty": {"lados": lados, "melhor": lado, "usado_por_1k": taxa_bounty,
                   "escolhido_a_mao": bounty_1k is not None,
                   "leiloes_mercenario": leiloes},
        "batalhas": sorted(
            [{k: v for k, v in l.items() if k != "_b"} for l in onde],
            key=lambda l: (not l.get("avaliada"),
                           -(((l.get("avaliada") or {}).get("melhor") or {}).get("rentabilidade")
                             or -1e18),
                           -l["por_1k"] * l["contexto"]["fator"], -l["contexto"]["fator"])),
        "alvo": ({k: v for k, v in lado_l.items() if k != "_b"} if lado_l else None),
        "mu": mu_info,
        "ordens": ordens_jog,
        "historico": hist,
        "receitas": rec,
        "estrategias": est,
        "procura": {k: v for k, v in p.items() if k != "estrategias"},
        "proximo_nivel": nivel,
        "precos": tabela_precos,
        "validacao": {
            "dano_por_golpe_motor": g_motor,
            "dano_por_golpe_motor_pilula": (atual or {}).get("dano_por_golpe_pilula"),
            "fator_batalha": ctx["batalha"],
            "dano_por_golpe_jogo": hist.get("dano_por_golpe"),
            "ataque_jogo": ((user.get("skills") or {}).get("attack") or {}).get("total"),
            "golpes_recentes": golpes_recentes(batalhas, user, cfg, contextos),
            "estudo": {
                "golpes": 93, "jogadores": 41,
                "o_que": ("em 93 golpes reais de 41 jogadores (round.getLastHits), o "
                          "dano observado sobre o previsto pelo contexto dá 1,00 em "
                          "média com a escada de ordens 10/15/20; o desvio de golpe a "
                          "golpe é ~13%, parte variação aleatória do jogo (±10% no "
                          "mesmo jogador e contexto) e parte o que a API não mostra "
                          "(aliança, ideologia)"),
            },
            "nota": ("o dano por golpe do motor já leva o bónus da batalha escolhida "
                     "(×" + _n(ctx["batalha"]) + "); o dos teus resumos de loot é a média "
                     "das batalhas onde bateste, com a pílula quando estava activa"),
        },
        "pressupostos": {
            "horizonte_dias": ctx["horizonte"], "horas_combate": ctx["horas_combate"],
            "batalha": ctx["batalha"], "contrato": usar_contrato,
            "limitado_ao_dinheiro": limitar_ao_dinheiro,
            "requisito_de_nivel": ("nenhuma peça do gameConfig tem requisito de nível: "
                                   "o nível só dá pontos de skill"),
            "durabilidade": ("cada golpe gasta 1% de cada peça (a esquiva poupa as "
                             "cinco que não são a arma); repor custa o preço de "
                             "mercado menos o terço que a peça partida devolve"),
        },
        "recomendacao": recomendacao(user.get("username") or ref, est,
                                     est.get("atual"), lado, hist, lado_l)
                        + frase_ordens(ordens_jog, mu_info, lado_l, leiloes),
        "avisos": avisos_do_equipamento(vestido, equip, sem_preco),
        "estrategias_def": ESTRATEGIAS,
    }
