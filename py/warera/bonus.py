"""
De onde vem o bonus de batalha, componente a componente — e lido do jogo.

A primeira versao deste modulo tinha os valores escritos a mao. Estava errada
por principio antes de estar errada por numero: **metade deles esta publicada
no `gameConfig`**, e este painel existe precisamente para nao adivinhar o que o
jogo diz. Agora le-se de la, e so fica escrito o que o jogo nao publica — com
essa distincao a aparecer na resposta, em vez de tudo parecer igualmente certo.

O que o `cfg["battle"]` da hoje:

    patrioticBonusPercent            20
    allianceDamagesBonusPercent      10
    enemyDamagesBonusPercent         10
    countryOrderBonusPercent         10
    muOrderBonusPercent              10
    regionNotLinkedToCapitalMalus   -25
    lostAttackingRegionMalus        -25
    occupyingYourRegionsMalus       -50

e o `cfg["region"]` da `resistanceCitizenBonusPercent` 30 e
`resistanceAllyBonusPercent` 15. Os bunkers, as bases e os quarteis-generais
vem de `upgradesConfig`, nivel a nivel. Sobram as **ethics**, que nao estao em
lado nenhum da config publica.

A outra coisa que faltava era o CONTEXTO. Os bonus nao se somam todos ao mesmo
tempo porque nem todos podem coexistir: um bunker so conta a defender, uma base
militar so conta a atacar, e a resistencia so conta a atacar numa revolta.
A versao anterior deixava somar os tres e dava um numero que nenhum jogador
consegue ter — o que e pior do que nao dar numero nenhum.
"""

from __future__ import annotations

from . import config

# ---------------------------------------------------------------------------
#  Os tres contextos em que se bate. Decidem que bonus podem sequer existir.
# ---------------------------------------------------------------------------
CONTEXTOS = {
    "ataque": "atacar uma região estrangeira",
    "defesa": "defender uma região do teu país",
    "revolta": "atacar numa revolta, para recuperar uma região ocupada",
}

# Que bonus e que cada contexto permite. Fora daqui o bonus nem entra na conta,
# e o painel diz porque e que nao entra em vez de o mostrar a zero sem razao.
PERMITE = {
    "patriotico": {"ataque", "defesa", "revolta"},
    "alianca": {"ataque", "defesa", "revolta"},
    "inimigo_jurado": {"ataque", "defesa", "revolta"},
    "quartel_general": {"ataque", "defesa", "revolta"},
    "ordem_pais": {"ataque", "defesa", "revolta"},
    "ordem_mu": {"ataque", "defesa", "revolta"},
    "resistencia": {"revolta"},
    "base_militar": {"ataque", "revolta"},
    "bunker": {"defesa"},
    "regiao_nuclear": {"defesa"},
    "linha_abastecimento": {"defesa"},
    "perdeu_regiao_ataque": {"ataque", "revolta"},
    "ocupa_tuas_regioes": {"ataque", "defesa", "revolta"},
    "ethics": {"ataque", "defesa", "revolta"},
}


def _b(cfg: dict, chave: str, omissao: float) -> float:
    v = ((cfg or {}).get("battle") or {}).get(chave)
    return float(v) if isinstance(v, (int, float)) else float(omissao)


def _r(cfg: dict, chave: str, omissao: float) -> float:
    v = ((cfg or {}).get("region") or {}).get(chave)
    return float(v) if isinstance(v, (int, float)) else float(omissao)


def _topo_upgrade(cfg: dict, chave: str, stat: str, omissao: float) -> float:
    """O bonus do nivel mais alto de uma construcao, tirado do gameConfig."""
    niveis = (((cfg or {}).get("upgradesConfig") or {}).get(chave) or {}).get("levels") or {}
    vals = [float((niveis[n] or {}).get("stats", {}).get(stat) or 0) for n in niveis]
    return max(vals) if vals else float(omissao)


# a API escreve a prioridade em ingles (`battleOrder.getByBattle`), o painel em
# portugues; as duas entram pela mesma porta
PRIORIDADE = {"low": "baixa", "medium": "media", "high": "alta",
              "baixa": "baixa", "media": "media", "média": "media", "alta": "alta"}


def _escada_ordens(base: float) -> dict[str, float]:
    return {"baixa": base, "media": base + 5.0, "alta": base + 10.0}


def valores(cfg: dict) -> dict:
    """
    Todos os numeros, com a FONTE de cada um.

    `fonte` e o que separa este painel de um guia: "config" e o jogo a dizer,
    "comunidade" e alguem a ter medido, e a diferenca importa quando os dois
    discordam.
    """
    return {
        "patriotico": {"max": _b(cfg, "patrioticBonusPercent", 20), "fonte": "config"},
        "alianca": {
            "max": _b(cfg, "allianceDamagesBonusPercent", 10),
            "limiar_quota": config.BONUS_BATALHA["alianca"]["limiar_quota"],
            "decaimento": config.BONUS_BATALHA["alianca"]["decaimento"],
            "piso": config.BONUS_BATALHA["alianca"]["piso"],
            "fonte": "config (máximo) + comunidade (curva)",
        },
        "inimigo_jurado": {"max": _b(cfg, "enemyDamagesBonusPercent", 10),
                           "por_dia": 1.0, "por_dia_isolacionista": 5.0,
                           "fonte": "config (máximo) + wiki (ritmo)"},
        # A ESCADA DAS ORDENS FOI MEDIDA, e nao e a da wiki.
        #
        # A wiki dizia 5/10/15. Em 93 golpes reais (`round.getLastHits`, com o
        # `attack.total` de cada jogador e o contexto da batalha ao lado) a
        # escada que poe a razao observado/previsto em 1,00 e 10/15/20 — a base
        # do `gameConfig` (10) e mais 5 por degrau. Com a da wiki ficava em
        # 1,06: o painel subestimava 6% o dano de quem bate com ordens. A
        # prioridade alta e a que esta bem medida (quase todas as ordens sao
        # altas); a baixa e a media seguem a mesma regra e estao menos provadas.
        "ordem_pais": {"niveis": _escada_ordens(_b(cfg, "countryOrderBonusPercent", 10)),
                       "base_config": _b(cfg, "countryOrderBonusPercent", 10),
                       "fonte": "config (base) + medido em golpes reais (degraus)"},
        "ordem_mu": {"niveis": _escada_ordens(_b(cfg, "muOrderBonusPercent", 10)),
                     "base_config": _b(cfg, "muOrderBonusPercent", 10),
                     "fonte": "config (base) + medido em golpes reais (degraus)"},
        "resistencia": {"max": _r(cfg, "resistanceCitizenBonusPercent", 30),
                        "max_aliado": _r(cfg, "resistanceAllyBonusPercent", 15),
                        "fonte": "config"},
        "base_militar": {"max": _topo_upgrade(cfg, "base", "attackBonus", 25),
                         "fonte": "config"},
        "bunker": {"max": _topo_upgrade(cfg, "bunker", "defenseBonus", 25),
                   "fonte": "config"},
        "quartel_general": {"max": _topo_upgrade(cfg, "headquarters", "attackBonus", 20),
                            "fonte": "config"},
        "regiao_nuclear": {"max": 15.0, "fonte": "wiki"},
        "linha_abastecimento": {"pct": _b(cfg, "regionNotLinkedToCapitalMalusPercent", -25),
                                "fonte": "config"},
        "perdeu_regiao_ataque": {"pct": _b(cfg, "lostAttackingRegionMalusPercent", -25),
                                 "fonte": "config"},
        "ocupa_tuas_regioes": {"pct": _b(cfg, "occupyingYourRegionsMalusPercent", -50),
                               "fonte": "config"},
        "ethics": {**config.ETHICS, "fonte": "comunidade (não está no gameConfig)"},
    }


def desacordos(cfg: dict) -> list[dict]:
    """
    Onde e que as fontes discordam. Nao se escolhe em silencio.

    A v0.25.5-beta diz que o bonus maximo de alianca passou de +10% para +20% a
    1 de Setembro de 2026, e a wiki repete-o. Mas o `gameConfig` do jogo, lido
    agora, continua a dizer 10. Nao ha maneira de decidir daqui qual esta certo:
    `battle.getDamageBonuses` recusa tokens de API, por isso nao se consegue ler
    o bonus que o jogo aplica mesmo. Fica o valor da config, que e o unico que
    vem do jogo, e o desacordo fica escrito.
    """
    fora = []
    a = _b(cfg, "allianceDamagesBonusPercent", 10)
    if a != 20.0:
        fora.append({
            "quem": "bónus máximo de aliança",
            "config": a,
            "patch_e_wiki": 20.0,
            "usado": a,
            "porque": ("o gameConfig é o jogo a falar e diz "
                       f"{a:.0f}%; o patch v0.25.5 e a wiki dizem 20%. "
                       "Sem `battle.getDamageBonuses` não dá para arbitrar, e "
                       "entre um documento e o servidor fica o servidor."),
        })
    return fora


def alianca(quota_dev_pct: float | None, cfg: dict | None = None) -> dict:
    """
    O bonus de alianca, que a partir de 1 Set 2026 pode ser negativo.

    `quota_dev_pct` e a fatia do desenvolvimento MUNDIAL que a alianca detem.
    Ate ao limiar da o maximo; acima dele perde `decaimento` pontos de bonus por
    cada ponto de quota, ate ao piso — que deixou de ser zero.
    """
    r = valores(cfg or {})["alianca"]
    if quota_dev_pct is None:
        return {"pct": 0.0, "aplicavel": False,
                "porque": "quota de desenvolvimento da aliança desconhecida"}
    excesso = max(0.0, float(quota_dev_pct) - r["limiar_quota"])
    pct = max(r["piso"], r["max"] - r["decaimento"] * excesso)
    if excesso <= 0:
        porque = (f"a aliança detém {quota_dev_pct:.1f}% do desenvolvimento mundial, "
                  f"dentro do limiar de {r['limiar_quota']:.0f}% — bónus máximo")
    elif pct > 0:
        porque = (f"{quota_dev_pct:.1f}% de quota, {excesso:.1f} pontos acima do limiar: "
                  f"-{r['decaimento'] * excesso:.1f} pontos de bónus")
    else:
        porque = (f"{quota_dev_pct:.1f}% de quota e uma aliança INCHADA: "
                  f"em vez de bónus, uma penalização de {pct:.1f}%")
    return {"pct": round(pct, 2), "aplicavel": True, "porque": porque,
            "quota_pct": float(quota_dev_pct), "no_piso": pct <= r["piso"] + 1e-9}


def ethics(tipo: str | None, grau: str = "fanatico", contexto: str = "ataque",
           contra_inimigo_jurado: bool = False, por_aliado: bool = False,
           pais_proprio: bool = True) -> dict:
    """
    O bonus da ideologia do pais. Nao esta no `gameConfig`.

    Quatro ideologias, cada uma boa numa situacao so, em dois graus:

        militarista    a atacar
        isolacionista  contra o inimigo jurado
        pacifista      a defender o proprio pais
        diplomatico    a lutar por um aliado

    Isto e a maior fonte de dano que faltava por completo a este painel — ate
    +15% que nao apareciam em conta nenhuma. Os valores vem da comunidade
    (o simulador de batalha da wiki) e nao do jogo: `country.rulingParty` vem
    como id e nao ha procedimento publico que o resolva, por isso nem sequer se
    consegue ler qual e a ideologia do teu proprio pais. Tens de a dizer tu.
    """
    if not tipo or tipo == "nenhuma":
        return {"pct": 0.0, "aplicavel": False, "porque": "sem ideologia escolhida"}
    tabela = config.ETHICS.get(grau) or {}
    valor = float(tabela.get(tipo) or 0.0)
    if not valor:
        return {"pct": 0.0, "aplicavel": False, "porque": f"ideologia desconhecida: {tipo}"}

    condicao = {
        "militarista": (contexto in ("ataque", "revolta"), "só a atacar"),
        "isolacionista": (contra_inimigo_jurado, "só contra o inimigo jurado"),
        "pacifista": (contexto == "defesa" and pais_proprio, "só a defender o teu país"),
        "diplomatico": (por_aliado, "só a lutar por um aliado"),
    }.get(tipo, (False, "condição desconhecida"))

    ativa, texto = condicao
    if not ativa:
        return {"pct": 0.0, "aplicavel": False,
                "porque": f"{tipo} vale +{valor:.0f}% mas {texto} — não é o caso aqui"}
    return {"pct": valor, "aplicavel": True,
            "porque": f"{grau} {tipo}: +{valor:.0f}%, {texto}"}


ORDEM_MOSTRA = ["patriotico", "alianca", "ethics", "quartel_general", "ordem_pais",
                "ordem_mu", "base_militar", "bunker", "resistencia", "inimigo_jurado",
                "regiao_nuclear", "linha_abastecimento", "perdeu_regiao_ataque",
                "ocupa_tuas_regioes"]


def calcular(cfg: dict | None = None, contexto: str = "ataque",
             patriotico: bool = False,
             quota_alianca_pct: float | None = None,
             qg_nivel_pct: float | None = None,
             ordem_pais_prioridade: str | None = None,
             ordem_mu_prioridade: str | None = None,
             base_militar_pct: float | None = None,
             bunker_pct: float | None = None,
             resistencia_barra_pct: float | None = None,
             cidadao: bool = True,
             inimigo_jurado_dias: float | None = None,
             isolacionista: bool = False,
             regiao_nuclear: bool = False,
             linha_cortada: bool = False,
             perdeu_regiao_ataque: bool = False,
             ocupa_tuas_regioes: bool = False,
             ethics_tipo: str | None = None,
             ethics_grau: str = "fanatico",
             por_aliado: bool = False,
             aplicar_teto: bool = True) -> dict:
    """
    A soma de tudo, com a conta a vista e o contexto a mandar.

    O `contexto` — atacar, defender ou revoltar — decide que bonus podem sequer
    existir. Pedir um bunker num contexto de ataque nao da erro: da zero, com a
    razao escrita, porque um bunker so conta a quem defende. Isso evita builds
    impossiveis, que era o que a versao anterior deixava construir.

    Os bonus somam-se entre si e o total multiplica o ataque UMA vez:

        ataque = base x munição x pílula x posto x (1 + soma/100)
    """
    cfg = cfg or {}
    v = valores(cfg)
    ctx = contexto if contexto in CONTEXTOS else "ataque"

    def permitido(chave):
        return ctx in PERMITE.get(chave, set())

    def bloqueado(chave, nome):
        # dizer so "não se aplica aqui" nao ensina nada. Dizer ONDE e que se
        # aplica responde a pergunta seguinte, que e sempre a mesma: entao
        # quando e que isto conta?
        onde = sorted(PERMITE.get(chave, set()))
        texto = " ou ".join(CONTEXTOS[c] for c in onde) if onde else "nenhum contexto"
        return {"pct": 0.0, "aplicavel": False,
                "porque": f"{nome} só conta a {texto}, e aqui estás a "
                          f"{CONTEXTOS[ctx]}"}

    comp = {}

    comp["patriotico"] = ({"pct": v["patriotico"]["max"], "aplicavel": True,
                           "porque": "o teu país é um dos dois lados da batalha"}
                          if patriotico and permitido("patriotico")
                          else {"pct": 0.0, "aplicavel": False,
                                "porque": "não estás a lutar pelo teu país"})

    comp["alianca"] = alianca(quota_alianca_pct, cfg)

    comp["ethics"] = ethics(ethics_tipo, ethics_grau, ctx,
                            contra_inimigo_jurado=bool(inimigo_jurado_dias),
                            por_aliado=por_aliado, pais_proprio=patriotico)

    for chave, valor_dado, nome in (
            ("quartel_general", qg_nivel_pct, "o quartel-general"),
            ("base_militar", base_militar_pct, "a base militar"),
            ("bunker", bunker_pct, "o bunker")):
        if not permitido(chave):
            comp[chave] = bloqueado(chave, nome)
        elif not valor_dado:
            comp[chave] = {"pct": 0.0, "aplicavel": False, "porque": f"sem {nome}"}
        else:
            tecto = v[chave]["max"]
            pct = min(float(valor_dado), tecto)
            comp[chave] = {"pct": round(pct, 2), "aplicavel": True,
                           "porque": f"{nome} a +{pct:.0f}% (máximo {tecto:.0f}%)"}

    for chave, prio in (("ordem_pais", ordem_pais_prioridade),
                        ("ordem_mu", ordem_mu_prioridade)):
        if not prio:
            comp[chave] = {"pct": 0.0, "aplicavel": False, "porque": "sem ordem deste lado"}
        else:
            nivel = PRIORIDADE.get(str(prio).lower(), str(prio).lower())
            pct = float(v[chave]["niveis"].get(nivel, 0.0))
            comp[chave] = {"pct": pct, "aplicavel": pct > 0,
                           "porque": f"ordem de prioridade {nivel}"}

    if not permitido("resistencia"):
        comp["resistencia"] = bloqueado("resistencia", "a resistência")
    elif not resistencia_barra_pct:
        comp["resistencia"] = {"pct": 0.0, "aplicavel": False,
                               "porque": "a barra de resistência está a zero"}
    else:
        tecto = v["resistencia"]["max"] if cidadao else v["resistencia"]["max_aliado"]
        pct = tecto * min(1.0, float(resistencia_barra_pct) / 100.0)
        comp["resistencia"] = {
            "pct": round(pct, 2), "aplicavel": True,
            "porque": (f"barra de resistência a {resistencia_barra_pct:.0f}% "
                       f"× {tecto:.0f}%" + ("" if cidadao else ", a de aliado"))}

    if not inimigo_jurado_dias:
        comp["inimigo_jurado"] = {"pct": 0.0, "aplicavel": False,
                                  "porque": "sem inimigo jurado declarado"}
    else:
        r = v["inimigo_jurado"]
        por_dia = r["por_dia_isolacionista"] if isolacionista else r["por_dia"]
        pct = min(r["max"], por_dia * float(inimigo_jurado_dias))
        comp["inimigo_jurado"] = {"pct": round(pct, 2), "aplicavel": True,
                                  "porque": f"{inimigo_jurado_dias:.0f} dias × {por_dia:.0f}%/dia"
                                            + (" (isolacionista)" if isolacionista else "")}

    comp["regiao_nuclear"] = ({"pct": v["regiao_nuclear"]["max"], "aplicavel": True,
                               "porque": "defender uma região nuclear do teu país"}
                              if regiao_nuclear and permitido("regiao_nuclear")
                              else bloqueado("regiao_nuclear", "a região nuclear")
                              if regiao_nuclear else
                              {"pct": 0.0, "aplicavel": False, "porque": "não é região nuclear"})

    # as tres penalizacoes. As duas ultimas faltavam por completo, e a de
    # ocupacao e a maior de todas — -50%, mais do que qualquer bonus da.
    for chave, ligado, nome in (
            ("linha_abastecimento", linha_cortada, "região sem ligação à capital"),
            ("perdeu_regiao_ataque", perdeu_regiao_ataque, "perdeste a região de onde atacavas"),
            ("ocupa_tuas_regioes", ocupa_tuas_regioes,
             "lutar por um país que te ocupa regiões")):
        if not ligado:
            comp[chave] = {"pct": 0.0, "aplicavel": False, "porque": f"sem {nome}"}
        elif not permitido(chave):
            comp[chave] = bloqueado(chave, nome)
        else:
            comp[chave] = {"pct": v[chave]["pct"], "aplicavel": True, "porque": nome}

    soma = sum(c["pct"] for c in comp.values())
    teto = (config.TETO_BONUS_BATALHA_REVOLTA_PCT if ctx == "revolta"
            else config.TETO_BONUS_BATALHA_PCT)
    cortado = aplicar_teto and soma > teto
    total = min(soma, teto) if aplicar_teto else soma

    return {
        "contexto": ctx,
        "contexto_texto": CONTEXTOS[ctx],
        "componentes": [{"chave": k, "fonte": (v.get(k) or {}).get("fonte", "—"), **comp[k]}
                        for k in ORDEM_MOSTRA if k in comp],
        "soma_pct": round(soma, 2),
        "total_pct": round(total, 2),
        "fator": round(1.0 + total / 100.0, 4),
        "teto_pct": teto,
        "cortado_pelo_teto": cortado,
        "teto_verificado": config.TETO_BONUS_BATALHA_VERIFICADO,
        "ganhos_pct": round(sum(c["pct"] for c in comp.values() if c["pct"] > 0), 2),
        "perdas_pct": round(sum(c["pct"] for c in comp.values() if c["pct"] < 0), 2),
        "desacordos": desacordos(cfg),
    }


def realista(cfg: dict | None = None) -> dict:
    """
    O que um soldado organizado consegue mesmo, sem construcoes nem revolta.

    Patriotico + alianca pequena + QG no topo + as duas ordens + militarista.
    Nao precisa de guerra especial nenhuma, so de coordenacao — e e mais do que
    a diferenca entre uma arma legendary e uma mythic.
    """
    return calcular(cfg, contexto="ataque", patriotico=True, quota_alianca_pct=5.0,
                    qg_nivel_pct=20.0, ordem_pais_prioridade="alta",
                    ordem_mu_prioridade="alta", ethics_tipo="militarista")


def regras(cfg: dict | None = None) -> list[dict]:
    """A tabela para mostrar no painel, com a fonte e o contexto de cada bónus."""
    v = valores(cfg or {})
    fora = []
    for chave in ORDEM_MOSTRA:
        r = v.get(chave) or {}
        if chave == "ethics":
            faixa = (f"+{max(config.ETHICS['basico'].values()):.0f}% a "
                     f"+{max(config.ETHICS['fanatico'].values()):.0f}%")
        elif "niveis" in r:
            faixa = f"+{min(r['niveis'].values()):.0f}% a +{max(r['niveis'].values()):.0f}%"
        elif "pct" in r:
            faixa = f"{r['pct']:.0f}%"
        elif chave == "alianca":
            faixa = f"{r['piso']:.0f}% a +{r['max']:.0f}%"
        else:
            faixa = f"até +{r.get('max', 0):.0f}%"
        fora.append({
            "chave": chave, "faixa": faixa, "fonte": r.get("fonte", "—"),
            "contextos": sorted(PERMITE.get(chave, set())),
        })
    return fora
