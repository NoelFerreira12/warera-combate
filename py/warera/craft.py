"""
O circuito do equipamento: scraps + aco -> craft -> mercado de equipamento, e
o caminho inverso (comprar equipamento -> desmantelar -> vender scraps).

Craftar custa duas coisas, e o feed so mostra uma. As transacoes `craftItem`
registam os scraps consumidos e mais nada; o aco desaparece do inventario sem
deixar transacao, e nao esta no gameConfig. A tabela do aco esta no cliente do
jogo (ver config.ACO_POR_RARIDADE) e muda as contas a serio: numa peca common
o aco custa mais do que os scraps.

E ha duas maneiras de craftar, com precos diferentes:

  aleatorio   pagas `craftCost` scraps + `craftCostSteel` aco e sai uma peca
              qualquer daquela raridade
  escolhido   pagas os mesmos scraps mas o DOBRO do aco, e escolhes o slot

Como dentro da mesma raridade os slots valem valores muito diferentes, a
escolha entre os dois nao e obvia — e o painel calcula as duas.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import api, config

# abaixo disto, uma "mediana" de vendas reais e so uma venda com sorte:
# com 2 amostras a mediana e o valor mais alto dos dois, o que inflaciona o
# lucro. Nesse caso vale mais a media da API, que puxa de mais historico.
MIN_VENDAS_FIAVEL = 3

# acima desta divergencia entre a mediana do feed e a media da API, uma das
# duas esta a mentir sobre o mercado e nao ha como saber qual. Um painel que
# diz onde por dinheiro tem de errar para o lado seguro, por isso fica a mais
# baixa das duas.
DIVERGENCIA_MAXIMA = 0.25


def _mediana(valores: list[float]) -> float:
    v = sorted(valores)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


FONTE_MEDIA_EQUIPAMENTO = ("estimativa: média de vendas por código "
                           "(gameStat.getEquipmentAvgByCode), sem roll, sem data e sem amostra")


def _preco_venda_equipamento(code: str, precos_eq: dict[str, float | None],
                             vendas_reais: dict[str, list[float]] | None
                             ) -> tuple[float | None, str, int | None]:
    """
    (preco, fonte, n_vendas) para uma peca de equipamento.

    `vendas_reais=None` quer dizer que as transacoes nao estao disponiveis (o
    jogo exige token): o numero de vendas nao se sabe e sai None, nao 0, e o
    preco e a media por codigo — uma estimativa, e a fonte di-lo.
    """
    if vendas_reais is None:
        api_avg = float(precos_eq[code]) if precos_eq.get(code) else None
        if api_avg:
            return api_avg, FONTE_MEDIA_EQUIPAMENTO, None
        return None, "sem dados suficientes", None
    reais = vendas_reais.get(code) or []
    api_avg = float(precos_eq[code]) if precos_eq.get(code) else None

    if len(reais) >= MIN_VENDAS_FIAVEL:
        med = _mediana(reais)
        if api_avg and abs(med - api_avg) / api_avg > DIVERGENCIA_MAXIMA:
            escolha = min(med, api_avg)
            return escolha, (f"conservador: {len(reais)} vendas dizem {med:.2f}, "
                             f"API diz {api_avg:.2f}"), len(reais)
        return med, f"mediana de {len(reais)} vendas reais", len(reais)

    if api_avg:
        return api_avg, "media da API", len(reais)
    if reais:
        return _mediana(reais), f"so {len(reais)} venda(s) - pouco fiavel", len(reais)
    return None, "sem preco", 0


def valor_de_uma_peca(code: str, precos_eq: dict, vendas_reais: dict,
                      custos: dict, preco_scrap: float | None,
                      taxa: float = config.TAXA_MERCADO_PADRAO) -> tuple[float | None, str]:
    """
    Quanto vale mesmo uma peca que ja tens na mao: o melhor entre vende-la no
    mercado e desmantela-la. As duas saidas pagam a taxa de mercado.
    """
    venda, _, _ = _preco_venda_equipamento(code, precos_eq, vendas_reais)
    liquido = float(venda) * (1 - taxa) if venda else None
    scraps = custos.get(code)
    piso = scraps * preco_scrap * (1 - taxa) if (scraps and preco_scrap) else None

    if liquido is None and piso is None:
        return None, "sem preco"
    if liquido is None:
        return piso, "scraps"
    if piso is None:
        return liquido, "venda"
    return (liquido, "venda") if liquido >= piso else (piso, "scraps")


def custo_em_aco(itens: dict, escolhida: bool = True) -> dict[str, int]:
    """
    Aco por craft, por codigo. `escolhida=True` e o caso de quem quer um slot
    concreto, que paga o dobro.
    """
    fator = config.MULTIPLICADOR_ACO_PECA_ESCOLHIDA if escolhida else 1
    fora = {}
    for code, cfg in itens.items():
        if cfg.get("type") not in ("equipment", "weapon"):
            continue
        base = config.ACO_POR_RARIDADE.get(cfg.get("rarity"))
        if base:
            fora[code] = base * fator
    return fora


def custo_em_scraps(itens: dict, observado: dict[str, int]) -> dict[str, int]:
    """
    Scraps por craft, por codigo de equipamento.

    Preferencia ao que foi mesmo observado no feed; o resto vem da escada por
    raridade (6, 18, 54, 162, 486, 1458), que e o padrao que as observacoes
    seguem sem excecao.
    """
    fora = {}
    for code, cfg in itens.items():
        if cfg.get("type") not in ("equipment", "weapon"):
            continue
        if code in observado:
            fora[code] = observado[code]
        else:
            r = cfg.get("rarity")
            if r in config.SCRAPS_POR_RARIDADE:
                fora[code] = config.SCRAPS_POR_RARIDADE[r]
    return fora


def precos_equipamento(codigos: list[str]) -> dict[str, float | None]:
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
        return dict(zip(codigos, ex.map(api.preco_medio_equipamento, codigos)))


def analisar_craft(itens: dict, panorama: dict, precos_eq: dict[str, float | None],
                   custos: dict[str, int], vendas_reais: dict[str, list[float]],
                   taxa: float = config.TAXA_MERCADO_PADRAO) -> list[dict]:
    """
    Compras scraps e aco ao melhor ask, fazes o craft da peca que queres,
    vendes no mercado de equipamento.

    Esta tabela e sempre a da peca ESCOLHIDA — e a unica que faz sentido linha
    a linha, porque uma peca aleatoria nao tem codigo. Por isso o aco entra a
    dobrar. O craft aleatorio esta em `analisar_craft_aleatorio`.

    O preco de venda usado e o observado no feed quando existe (mediana das
    vendas reais) e a media da API quando nao existe, porque a media da API
    arrasta historico antigo.
    """
    m_scraps = panorama.get("scraps") or {}
    preco_scrap = m_scraps.get("best_ask") or m_scraps.get("ref")
    m_aco = panorama.get("steel") or {}
    preco_aco = m_aco.get("best_ask") or m_aco.get("ref")
    if not preco_scrap:
        return []

    aco = custo_em_aco(itens, escolhida=True)

    fora = []
    for code, n_scraps in custos.items():
        cfg = itens.get(code) or {}
        venda, fonte, n = _preco_venda_equipamento(code, precos_eq, vendas_reais)
        if venda is None:
            continue

        n_aco = aco.get(code, 0)
        custo_scraps = n_scraps * preco_scrap
        custo_aco = n_aco * (preco_aco or 0.0)
        custo = custo_scraps + custo_aco
        liquido = venda * (1 - taxa)
        lucro = liquido - custo
        fora.append({
            "codigo": code,
            "tipo": cfg.get("type"),
            "raridade": cfg.get("rarity"),
            "scraps": n_scraps,
            "aco": n_aco,
            "custo_scraps": round(custo_scraps, 3),
            "custo_aco": round(custo_aco, 3),
            "custo": round(custo, 3),
            "venda": round(venda, 2),
            "venda_api": round(float(precos_eq[code]), 2) if precos_eq.get(code) else None,
            "venda_liq": round(liquido, 3),
            "lucro": round(lucro, 3),
            "roi_pct": round(lucro / custo * 100, 1) if custo else None,
            "fonte_preco": fonte,
            "n_vendas": n,
        })
    return sorted(fora, key=lambda x: x["roi_pct"] if x["roi_pct"] is not None else -999, reverse=True)


def analisar_craft_aleatorio(itens: dict, panorama: dict, precos_eq: dict[str, float | None],
                             custos: dict[str, int], vendas_reais: dict[str, list[float]],
                             taxa: float = config.TAXA_MERCADO_PADRAO) -> list[dict]:
    """
    A outra maneira de craftar: pagas metade do aco e aceitas uma peca
    qualquer daquela raridade.

    O que sai vale, em media, a media das pecas da raridade — e essa media e
    puxada para baixo pelos slots que ninguem quer. Poupar aco so compensa se
    a diferenca entre a media e o melhor slot for menor do que o aco poupado,
    e isso muda com o preco do aco.

    Cada peca e valorizada pelo melhor destino (vender ou desmantelar), porque
    e isso que farias mesmo se te saisse a peca errada.
    """
    m_scraps = panorama.get("scraps") or {}
    preco_scrap = m_scraps.get("best_ask") or m_scraps.get("ref")
    m_aco = panorama.get("steel") or {}
    preco_aco = m_aco.get("best_ask") or m_aco.get("ref")
    m_bid = m_scraps.get("best_bid") or m_scraps.get("ref")
    if not preco_scrap:
        return []

    por_raridade: dict[str, list[str]] = {}
    for code in custos:
        r = (itens.get(code) or {}).get("rarity")
        if r:
            por_raridade.setdefault(r, []).append(code)

    fora = []
    for raridade, codigos in por_raridade.items():
        n_scraps = config.SCRAPS_POR_RARIDADE.get(raridade)
        n_aco = config.ACO_POR_RARIDADE.get(raridade)
        if not n_scraps or not n_aco:
            continue

        valores = []
        for code in codigos:
            v, _ = valor_de_uma_peca(code, precos_eq, vendas_reais, custos, m_bid, taxa)
            if v is not None:
                valores.append((code, v))
        if not valores:
            continue
        media = sum(v for _, v in valores) / len(valores)
        melhor_code, melhor_valor = max(valores, key=lambda x: x[1])
        pior_code, pior_valor = min(valores, key=lambda x: x[1])

        custo_scraps = n_scraps * preco_scrap
        custo_aleatorio = custo_scraps + n_aco * (preco_aco or 0.0)
        custo_escolhido = custo_scraps + n_aco * config.MULTIPLICADOR_ACO_PECA_ESCOLHIDA * (preco_aco or 0.0)

        lucro_aleatorio = media - custo_aleatorio
        lucro_escolhido = melhor_valor - custo_escolhido
        fora.append({
            "raridade": raridade,
            "n_pecas": len(valores),
            "scraps": n_scraps,
            "aco_aleatorio": n_aco,
            "aco_escolhido": n_aco * config.MULTIPLICADOR_ACO_PECA_ESCOLHIDA,
            "custo_aleatorio": round(custo_aleatorio, 3),
            "custo_escolhido": round(custo_escolhido, 3),
            "valor_medio": round(media, 3),
            "melhor_peca": melhor_code,
            "melhor_valor": round(melhor_valor, 3),
            "pior_peca": pior_code,
            "pior_valor": round(pior_valor, 3),
            "lucro_aleatorio": round(lucro_aleatorio, 3),
            "lucro_escolhido": round(lucro_escolhido, 3),
            "roi_aleatorio_pct": round(lucro_aleatorio / custo_aleatorio * 100, 1) if custo_aleatorio else None,
            "roi_escolhido_pct": round(lucro_escolhido / custo_escolhido * 100, 1) if custo_escolhido else None,
            "vale_a_pena_escolher": lucro_escolhido > lucro_aleatorio,
            "diferenca": round(lucro_escolhido - lucro_aleatorio, 3),
        })
    ordem = {r: i for i, r in enumerate(config.ORDEM_RARIDADE)}
    return sorted(fora, key=lambda x: ordem.get(x["raridade"], 99))


def analisar_desmantelar(itens: dict, panorama: dict, precos_eq: dict[str, float | None],
                         rendimentos: dict[str, int], custos: dict[str, int],
                         vendas_reais: dict[str, list[float]],
                         taxa: float = config.TAXA_MERCADO_PADRAO) -> list[dict]:
    """
    O caminho inverso: comprar equipamento barato, desmantelar e vender os
    scraps. So compensa quando o equipamento esta a ser vendido abaixo do
    valor dos scraps que contem.
    """
    m_scraps = panorama.get("scraps") or {}
    preco_scrap_venda = m_scraps.get("best_bid") or m_scraps.get("ref")
    if not preco_scrap_venda:
        return []

    fora = []
    for code in set(list(rendimentos) + list(custos)):
        rende = rendimentos.get(code) or custos.get(code)
        if not rende:
            continue
        compra, fonte, n = _preco_venda_equipamento(code, precos_eq, vendas_reais)
        if not compra:
            continue
        recebe = rende * preco_scrap_venda * (1 - taxa)
        lucro = recebe - float(compra)
        if lucro <= 0:
            continue
        fora.append({
            "codigo": code,
            "raridade": (itens.get(code) or {}).get("rarity"),
            "compra": round(float(compra), 2),
            "scraps": rende,
            "recebe": round(recebe, 3),
            "lucro": round(lucro, 3),
            "roi_pct": round(lucro / float(compra) * 100, 1),
            "fonte_preco": fonte,
            "n_vendas": n,
        })
    return sorted(fora, key=lambda x: x["roi_pct"], reverse=True)


def vender_ou_desmantelar(itens: dict, panorama: dict, precos_eq: dict[str, float | None],
                          custos: dict[str, int], vendas_reais: dict[str, list[float]],
                          taxa: float = config.TAXA_MERCADO_PADRAO) -> list[dict]:
    """
    A decisao que se faz dezenas de vezes por dia e nao estava em lado nenhum:
    caiu-te uma peca (loot, case, craft) — vendes ou desmantelas?

    Cada peca tem um piso: os scraps que contem. Com a peca inteira o piso e o
    custo de craft da raridade; com a peca partida (state 0) e um terco disso,
    verificado em milhares de desmantelamentos reais. Vender abaixo do piso e
    deitar dinheiro fora, e e o que acontece em varios itens de baixo nivel,
    porque o loot das batalhas inunda o mercado de equipamento comum.
    """
    m_scraps = panorama.get("scraps") or {}
    preco_scrap = m_scraps.get("best_bid") or m_scraps.get("ref")
    if not preco_scrap:
        return []

    fora = []
    for code, n_scraps in custos.items():
        venda, fonte, n = _preco_venda_equipamento(code, precos_eq, vendas_reais)
        if venda is None:
            continue
        liquido_venda = float(venda) * (1 - taxa)
        piso_inteiro = n_scraps * preco_scrap * (1 - taxa)
        piso_partido = piso_inteiro * config.FRACAO_SCRAPS_PECA_PARTIDA
        melhor = "desmantelar" if piso_inteiro > liquido_venda else "vender"
        fora.append({
            "codigo": code,
            "raridade": (itens.get(code) or {}).get("rarity"),
            "venda_liq": round(liquido_venda, 3),
            "scraps": n_scraps,
            "piso_scraps": round(piso_inteiro, 3),
            "piso_partido": round(piso_partido, 3),
            "premio_pct": round((liquido_venda - piso_inteiro) / piso_inteiro * 100, 1)
            if piso_inteiro else None,
            "decisao": melhor,
            "ganho": round(abs(liquido_venda - piso_inteiro), 3),
            "fonte_preco": fonte,
            "n_vendas": n,
        })
    return sorted(fora, key=lambda x: x["premio_pct"] if x["premio_pct"] is not None else 0)


def analisar_cases(panorama: dict, saidas: dict, precos_eq: dict[str, float | None],
                   vendas_reais: dict[str, list[float]],
                   taxa: float = config.TAXA_MERCADO_PADRAO,
                   custos: dict | None = None) -> list[dict]:
    """
    Valor esperado de abrir uma case, a partir das aberturas que aconteceram
    mesmo no feed.

    Cada peca que sai e valorizada pelo MELHOR DESTINO — vender no mercado ou
    desfazer em scraps — e nao pelo preco de mercado apenas. E a mesma regra
    que se usa no loot, e faz diferenca: metade do que sai de uma case1 sao
    pecas de tier baixo que valem mais desfeitas do que vendidas, e valoriza-las
    ao preco de mercado subestimava a case.

    O numero de amostras e decisivo: com 1200 aberturas o EV e fiavel, com 8
    nao vale nada. Por isso `n` e `fiavel` vao sempre no resultado.
    """
    m_scraps = panorama.get("scraps") or {}
    preco_scrap = m_scraps.get("best_bid") or m_scraps.get("ref")
    custos = custos or {}

    fora = []
    for case, contador in saidas.items():
        n = sum(contador.values())
        if not n:
            continue
        total = 0.0
        valorizados = 0
        por_scraps = 0
        for code, vezes in contador.items():
            p, destino = valor_de_uma_peca(code, precos_eq, vendas_reais, custos,
                                           preco_scrap, taxa)
            if p is None:
                continue
            total += float(p) * vezes
            valorizados += vezes
            if destino == "scraps":
                por_scraps += vezes
        if not valorizados:
            continue
        # `valor_de_uma_peca` ja devolve valores liquidos de taxa
        ev = total / valorizados
        ev_bruto = ev / (1 - taxa) if taxa < 1 else ev
        m = panorama.get(case) or {}
        custo = m.get("best_ask") or m.get("ref")
        # a case tambem se vende: as tres saidas sao vender, abrir, ou abrir e
        # desfazer o que sair. `ev` ja escolhe entre vender e desfazer o
        # conteudo peca a peca, por isso falta so comparar com a venda da case
        venda = (m.get("best_bid") or m.get("ref") or 0) * (1 - taxa)
        melhor = "abrir" if ev >= venda else "vender a case"
        top = contador.most_common(5)
        fora.append({
            "case": case,
            "n_aberturas": n,
            "ev_bruto": round(ev_bruto, 3),
            "ev_liquido": round(ev, 3),
            "custo": round(custo, 3) if custo else None,
            "lucro": round(ev - custo, 3) if custo else None,
            "roi_pct": round((ev - custo) / custo * 100, 1) if custo else None,
            "fiavel": n >= 100,
            "venda_liquida": round(venda, 3),
            "melhor_saida": melhor,
            "ganho_de_abrir": round(ev - venda, 3),
            "pct_melhor_desfeita": round(por_scraps / valorizados * 100, 1) if valorizados else 0,
            "saidas_top": [{"codigo": c, "vezes": v, "pct": round(v / n * 100, 1)} for c, v in top],
        })
    return sorted(fora, key=lambda x: x["roi_pct"] if x["roi_pct"] is not None else -999, reverse=True)


def avisos(craft: list[dict], cases: list[dict], desmantelar: list[dict],
           destino: list[dict] | None = None,
           aleatorio: list[dict] | None = None) -> list[str]:
    fora = []
    if craft:
        melhor = craft[0]
        fora.append(
            f"Melhor craft agora: {melhor['codigo']} ({melhor['raridade']}) - "
            f"{melhor['scraps']} scraps mais {melhor['aco']} de aco custam "
            f"{melhor['custo']:.2f} ({melhor['custo_scraps']:.2f} + {melhor['custo_aco']:.2f}) "
            f"e vende por {melhor['venda']:.2f}, dando {melhor['lucro']:.2f} de lucro "
            f"({melhor['roi_pct']}% ROI)."
        )
        com_aco = [c for c in craft if c.get("custo_aco")]
        if com_aco:
            peso = sum(c["custo_aco"] for c in com_aco) / sum(c["custo"] for c in com_aco) * 100
            fora.append(
                f"O aco e {peso:.0f}% do custo de craft em media e nao aparece em transacao "
                "nenhuma do feed — o jogo consome-o sem deixar rasto. Quem so contar os "
                "scraps julga estar a ganhar em pecas que dao prejuizo."
            )
        prejuizo = [c for c in craft if (c.get("roi_pct") or 0) <= 0]
        if prejuizo:
            fora.append(
                f"{len(prejuizo)} peca(s) dao prejuizo a craftar com o aco incluido: "
                + ", ".join(c["codigo"] for c in prejuizo[:6])
                + ". Sao quase todas de raridade baixa, onde o aco pesa mais do que os scraps."
            )
        fora.append(
            "Ressalva do craft: o equipamento sai com stats aleatorios dentro do "
            "intervalo da raridade. O preco usado aqui e a media/mediana do mercado, "
            "por isso uma peca com roll fraco vende abaixo disso e uma com roll forte "
            "acima. A margem e real, mas varia peca a peca."
        )
        por_raridade: dict[str, list[dict]] = {}
        for x in craft:
            por_raridade.setdefault(x["raridade"] or "?", []).append(x)
        for r, grupo in por_raridade.items():
            if len(grupo) < 2:
                continue
            g = sorted(grupo, key=lambda x: x["lucro"], reverse=True)
            if g[0]["lucro"] - g[-1]["lucro"] > 0.5:
                fora.append(
                    f"Dentro de {r} o custo em scraps e igual para todos, mas "
                    f"{g[0]['codigo']} vende por {g[0]['venda']:.2f} e {g[-1]['codigo']} "
                    f"por {g[-1]['venda']:.2f}. Craftar o slot errado deita fora "
                    f"{g[0]['lucro'] - g[-1]['lucro']:.2f} por peca."
                )
                break
    if cases:
        for c in cases:
            if c["roi_pct"] is None:
                continue
            if not c["fiavel"]:
                fora.append(
                    f"{c['case']}: so {c['n_aberturas']} aberturas na amostra, "
                    f"o valor esperado ({c['ev_liquido']:.2f}) ainda nao e de confianca."
                )
            elif abs(c["roi_pct"]) < 5:
                fora.append(
                    f"{c['case']} esta justa: custa {c['custo']:.2f} e vale "
                    f"{c['ev_liquido']:.2f} em media ({c['n_aberturas']} aberturas). "
                    "Abrir cases nao e forma de ganhar dinheiro, e uma troca neutra."
                )
            elif c["roi_pct"] > 0:
                fora.append(
                    f"{c['case']} esta barata: custa {c['custo']:.2f} e o conteudo "
                    f"vale {c['ev_liquido']:.2f} ({c['roi_pct']}%)."
                )
            else:
                fora.append(
                    f"{c['case']} esta cara: custa {c['custo']:.2f} para um conteudo "
                    f"que vale {c['ev_liquido']:.2f} ({c['roi_pct']}%). Compensa mais "
                    "comprar o equipamento feito."
                )
    if desmantelar:
        d = desmantelar[0]
        fora.append(
            f"Comprar {d['codigo']} a {d['compra']:.2f} e desmantelar da "
            f"{d['scraps']} scraps que valem {d['recebe']:.2f} ({d['roi_pct']}%)."
        )
    if destino:
        piores = [x for x in destino if x["decisao"] == "desmantelar"]
        if piores:
            x = piores[0]
            fora.append(
                f"Se te cair um {x['codigo']}, desmantela em vez de vender: no mercado "
                f"da {x['venda_liq']:.2f} liquidos e os {x['scraps']} scraps valem "
                f"{x['piso_scraps']:.2f}. Sao {len(piores)} pecas nessa situacao."
            )
        else:
            x = destino[0]
            fora.append(
                f"Nenhuma peca vale menos do que os scraps que contem. A mais perto do "
                f"piso e {x['codigo']}: vende por {x['venda_liq']:.2f} e desmantelada "
                f"da {x['piso_scraps']:.2f} ({x['premio_pct']:+.1f}%)."
            )
        fora.append(
            "Uma peca partida (state 0) so devolve um terco dos scraps — o preco de "
            "mercado e sempre de pecas a 100%, por isso vender antes de partir vale "
            "sempre mais do que desmantelar depois. O aco gasto no craft nunca volta."
        )
    if aleatorio:
        melhor = max(aleatorio, key=lambda x: max(x["lucro_aleatorio"], x["lucro_escolhido"]))
        if melhor["vale_a_pena_escolher"]:
            fora.append(
                f"Em {melhor['raridade']} compensa escolher a peca: {melhor['melhor_peca']} "
                f"vale {melhor['melhor_valor']:.2f} e a media da raridade e "
                f"{melhor['valor_medio']:.2f}. O aco a dobrar "
                f"({melhor['aco_escolhido']} em vez de {melhor['aco_aleatorio']}) custa menos "
                f"do que a diferenca: sao {melhor['diferenca']:.2f} a mais por craft."
            )
        else:
            fora.append(
                f"Em {melhor['raridade']} nao compensa escolher a peca: poupar o aco "
                f"({melhor['aco_aleatorio']} em vez de {melhor['aco_escolhido']}) rende mais "
                f"do que a diferenca entre {melhor['melhor_peca']} ({melhor['melhor_valor']:.2f}) "
                f"e a media da raridade ({melhor['valor_medio']:.2f})."
            )
        negativos = [x for x in aleatorio if (x["roi_aleatorio_pct"] or 0) <= 0
                     and (x["roi_escolhido_pct"] or 0) <= 0]
        if negativos:
            fora.append(
                "Raridades onde craftar da prejuizo de qualquer das maneiras: "
                + ", ".join(x["raridade"] for x in negativos)
                + ". O equipamento dessas raridades vale pouco mais do que os scraps que "
                "contem, e o aco nao entra nessa conta."
            )
    return fora
