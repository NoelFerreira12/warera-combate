"""
Livro de ordens e market making: comprar ao bid e vender ao ask.

A reversao a media vive em `historico.py`, porque so faz sentido medida
contra os 30 dias de historico diario e nao contra uma amostra de minutos.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from . import api, config


def resumo_livro(livro: dict, referencia: float | None,
                 profundidade_conhecida: bool = True) -> dict:
    """
    Melhor compra/venda, profundidade e spread.

    A profundidade conta so as ordens dentro de 2% do topo, porque e essa a
    quantidade que consegues mesmo negociar sem mover o preco.

    `profundidade_conhecida=False` para os livros que vem de
    `getTopOrdersPerItemCode`, que so traz o topo de cada lado. Ai nao se sabe
    quantas ordens ha, e dizer "confianca baixa" seria uma afirmacao falsa: nao
    e um livro fino, e um livro que nao chegamos a ver.
    """
    compras = [o for o in livro.get("buyOrders") or [] if o.get("price")]
    vendas = [o for o in livro.get("sellOrders") or [] if o.get("price")]
    best_bid = max((float(o["price"]) for o in compras), default=None)
    best_ask = min((float(o["price"]) for o in vendas), default=None)

    def profundidade(ordens, topo, lado):
        if topo is None:
            return 0.0
        limite = topo * (1.02 if lado == "ask" else 0.98)
        return sum(
            float(o.get("quantity") or 0)
            for o in ordens
            if (float(o["price"]) <= limite if lado == "ask" else float(o["price"]) >= limite)
        )

    spread = None
    if best_bid and best_ask and best_ask > 0:
        spread = (best_ask - best_bid) / best_ask * 100

    qtd_ask = profundidade(vendas, best_ask, "ask")
    qtd_bid = profundidade(compras, best_bid, "bid")
    # um livro com duas ordens nao e um preco de mercado, e a opiniao de duas
    # pessoas. A confianca acompanha sempre o preco, para nao se tomar uma
    # ponta fina por uma cotacao.
    ordens = len(vendas) + len(compras)
    if not (best_bid and best_ask):
        confianca = "baixa (livro so tem um lado)"
    elif not profundidade_conhecida:
        confianca = "desconhecida (so o topo do livro)"
    elif ordens >= 6 and min(qtd_ask, qtd_bid) >= 50:
        confianca = "alta"
    elif ordens >= 3:
        confianca = "media (livro fino)"
    else:
        confianca = "baixa (poucas ordens)"

    # de que lado esta o peso: a fatia da profundidade que sao ordens de
    # compra. Acima de 50% ha mais dinheiro a querer entrar do que mercadoria
    # a querer sair, e e a unica leitura de pressao que um livro deste tamanho
    # aguenta — por isso vem sempre com as duas quantidades ao lado.
    total = qtd_ask + qtd_bid
    desequilibrio = (qtd_bid / total * 100) if total else None

    return {
        "ref": referencia,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_pct": spread,
        "qtd_ask": qtd_ask,
        "qtd_bid": qtd_bid,
        "desequilibrio_pct": desequilibrio,
        "n_ordens_venda": len(vendas),
        "n_ordens_compra": len(compras),
        "confianca": confianca,
    }


# Quanto tempo o livro de um bem serve antes de se voltar a pedir. Com
# `getTopOrders` e um pedido por bem: ~24 bens a cada 45 s sao ~32 pedidos por
# minuto, dentro dos 100 que a API deixa (cabecalho `ratelimit-policy`), mesmo
# com a vista ao vivo a perguntar de dez em dez segundos.
TTL_LIVRO_S = 45.0


def _livro(code: str) -> dict:
    return api.cached(f"livro:{code}", TTL_LIVRO_S, lambda: {
        **api.top_orders(code), "_lido_em": time.time()})


def livros_em_massa(codigos: list[str]) -> dict[str, dict]:
    """
    O livro de ordens de cada bem, pelo endpoint DOCUMENTADO.

    Era `tradingOrder.getTopOrdersPerItemCode` (um pedido so, mas fora da
    documentacao publica da API). Agora e `tradingOrder.getTopOrders`, que esta
    em `api2.warera.io/openapi.json`, responde sem token e traz o livro inteiro
    de cada lado (testado a 19/11/2026) — um pedido por bem, com cache.
    """
    codigos = list(codigos)
    with ThreadPoolExecutor(max_workers=min(8, config.MAX_WORKERS)) as ex:
        return dict(zip(codigos, ex.map(_livro, codigos)))


def idade_das_ordens(livro: dict, agora: float | None = None) -> dict:
    """
    Quando foram postas as ordens do topo de cada lado, e quando se leu o livro.

    Uma ordem e o que alguem pede ou oferece, nao uma venda: a hora diz ha
    quanto tempo o preco esta ali sem ninguem lhe pegar.
    """
    agora = agora or time.time()

    def topo(ordens, melhor):
        os_ = [o for o in ordens or [] if o.get("price")]
        if not os_:
            return None
        o = (max if melhor == "max" else min)(os_, key=lambda x: float(x["price"]))
        return o.get("offerAt")

    lido = livro.get("_lido_em")
    return {
        "natureza": "ordens abertas (não são vendas)",
        "fonte": "tradingOrder.getTopOrders",
        "ordem_bid_desde": topo(livro.get("buyOrders"), "max"),
        "ordem_ask_desde": topo(livro.get("sellOrders"), "min"),
        "livro_lido_ha_s": round(agora - lido, 1) if lido else None,
    }


def instantaneo(codigos: list[str], precos: dict[str, float]) -> dict[str, dict]:
    """
    O mercado agora: o livro de cada bem (documentado, com cache de 45 s) e o
    preco de referencia ao lado. E o que o modo ao vivo do painel chama em ciclo.
    """
    livros = livros_em_massa(codigos)
    agora = time.time()
    return {c: {**resumo_livro(livros.get(c) or {}, precos.get(c)),
                **idade_das_ordens(livros.get(c) or {}, agora)} for c in codigos}


def carregar_panorama(codigos: list[str], precos: dict[str, float]) -> dict[str, dict]:
    livros = livros_em_massa(codigos)
    agora = time.time()
    return {c: {**resumo_livro(livros[c], precos.get(c)), **idade_das_ordens(livros[c], agora)}
            for c in codigos}


# ---------------------------------------------------------------------------
# Market making: comprar ao bid, vender ao ask
# ---------------------------------------------------------------------------

def oportunidades_spread(panorama: dict[str, dict], stats: dict[str, dict],
                         taxa: float = config.TAXA_MERCADO_PADRAO) -> list[dict]:
    """
    Poe uma ordem de compra ligeiramente acima do melhor bid e uma de venda
    ligeiramente abaixo do melhor ask. Ganhas o spread, pagas a taxa so na
    venda.

    Isto so e dinheiro a serio se o item negociar o suficiente para as duas
    ordens serem executadas, por isso a liquidez (unidades por minuto do feed
    real) entra na conta e nao apenas como enfeite: o `lucro_por_hora` assume
    que so consegues rodar o volume que o mercado de facto move.
    """
    fora = []
    for code, m in panorama.items():
        bid, ask = m.get("best_bid"), m.get("best_ask")
        if not bid or not ask or ask <= bid:
            continue
        lucro_un = ask * (1 - taxa) - bid
        if lucro_un <= 0:
            continue
        # `stats is None` quer dizer que as transacoes nao estao disponiveis:
        # sem liquidez medida nao ha "lucro por hora", e nao se escreve zero
        # onde o que ha e falta de dados
        if stats is None:
            fora.append({
                "codigo": code, "bid": round(bid, 5), "ask": round(ask, 5),
                "lucro_un": round(lucro_un, 5),
                "margem_pct": round(lucro_un / bid * 100, 2) if bid else None,
                "qtd_por_min": None, "n_vendas": None,
                "capital_por_ciclo": None, "lucro_por_hora": None,
                "sem_liquidez": "sem transacções: a liquidez não se mede, só o livro",
            })
            continue
        s = stats.get(code) or {}
        liquidez = s.get("qtd_por_min") or 0.0
        # so contamos com uma fatia do fluxo: nao es o unico a fazer isto
        rodagem_hora = liquidez * 60 * 0.25
        capacidade = min(rodagem_hora, (m.get("qtd_ask") or 0) + (m.get("qtd_bid") or 0))
        fora.append({
            "codigo": code,
            "bid": round(bid, 5),
            "ask": round(ask, 5),
            "lucro_un": round(lucro_un, 5),
            "margem_pct": round(lucro_un / bid * 100, 2) if bid else None,
            "qtd_por_min": round(liquidez, 1),
            "n_vendas": s.get("n_vendas", 0),
            "capital_por_ciclo": round(bid * capacidade, 2),
            "lucro_por_hora": round(lucro_un * capacidade, 2),
        })
    return sorted(fora, key=lambda x: (x["lucro_por_hora"] is None,
                                       -(x["lucro_por_hora"] or 0), -(x["margem_pct"] or 0)))


def _pct(valor, omissao: float) -> float:
    return float(valor) / 100.0 if isinstance(valor, (int, float)) else omissao


def taxas_do_pais(country_id: str | None) -> dict[str, float]:
    """
    As tres taxas do pais, em fracao. `country.taxes` traz:

      market    pago em cada venda no mercado
      selfWork  pago por trabalhar na propria empresa
      income    pago sobre o salario recebido de outra empresa

    O painel so usava `market`, que e a que menos varia (163 paises em 1%).
    `selfWork` vai de 0.01% a 45% e muda por completo o que compensa produzir.
    """
    padrao = {
        "market": config.TAXA_MERCADO_PADRAO,
        "selfwork": config.TAXA_SELFWORK_PADRAO,
        "income": config.TAXA_RENDIMENTO_PADRAO,
    }
    if not country_id:
        return padrao
    for c in api.paises():
        if (c.get("_id") or c.get("id")) == country_id:
            t = c.get("taxes") or {}
            return {
                "market": _pct(t.get("market"), padrao["market"]),
                "selfwork": _pct(t.get("selfWork"), padrao["selfwork"]),
                "income": _pct(t.get("income"), padrao["income"]),
            }
    return padrao


def taxa_do_pais(country_id: str | None) -> float:
    """Taxa de mercado do pais (country.taxes.market, em %). 1% na maioria."""
    return taxas_do_pais(country_id)["market"]


def lista_paises() -> list[dict]:
    fora = []
    for c in api.paises():
        cid = c.get("_id") or c.get("id")
        if not cid:
            continue
        taxas = c.get("taxes") or {}
        rank = c.get("rankings") or {}
        fora.append({
            "id": cid,
            "nome": c.get("name") or c.get("code") or cid,
            # duas letras que dao a bandeira em emoji, sem pedir imagem nenhuma
            "codigo": (c.get("code") or "").upper() or None,
            "taxa_mercado": taxas.get("market"),
            "taxa_rendimento": taxas.get("income"),
            "taxa_trabalho_proprio": taxas.get("selfWork"),
            "especializacao": c.get("specializedItem"),
            "populacao_ativa": (rank.get("countryActivePopulation") or {}).get("value"),
            "bonus_producao": (rank.get("countryProductionBonus") or {}).get("value"),
            # quanto vale a especializacao: sai dos recursos estrategicos que o
            # pais tem no mapa e aplica-se ao `specializedItem`, na capital
            "bonus_estrategico_pct": ((c.get("strategicResources") or {}).get("bonuses")
                                      or {}).get("productionPercent"),
            # equipamento + armas + dinheiro + itens dos cidadaos, somado por
            # pais — o `rank` da API e a posicao nesta tabela, e nao um id
            "riqueza_militar": (rank.get("countryWealth") or {}).get("value"),
            "riqueza_militar_posicao": (rank.get("countryWealth") or {}).get("rank"),
        })
    return sorted(fora, key=lambda x: x["nome"])


def riqueza_militar(paises: list[dict] | None = None) -> dict:
    """
    Os paises ordenados pela riqueza militar dos seus cidadaos — a mesma conta
    que decide quem manda numa guerra, e nao a mesma coisa que a producao ou o
    dinheiro do tesouro do pais (`country.money`, que e outra rubrica).

    Nao ha endpoint publico do total mundial nem da fatia de cada um: soma-se
    aqui, sobre a mesma lista que ja se buscou para as taxas.
    """
    lista = paises if paises is not None else lista_paises()
    com_valor = [p for p in lista if p.get("riqueza_militar")]
    total = sum(p["riqueza_militar"] for p in com_valor)
    ordenado = sorted(com_valor, key=lambda x: -x["riqueza_militar"])
    return {
        "total_mundial": round(total, 0),
        "paises": [{**p, "fatia_pct": round(p["riqueza_militar"] / total * 100, 2) if total else 0}
                   for p in ordenado],
    }


# ---------------------------------------------------------------------------
# Onde vender, onde produzir, onde trabalhar
# ---------------------------------------------------------------------------

def comparar_paises(paises: list[dict] | None = None, minimo_ativos: int = 5) -> dict:
    """
    A mesma venda rende valores diferentes conforme o pais em que estas, e a
    diferenca nao e pequena: ha paises a 0% de taxa de mercado e outros a 10%,
    e o imposto sobre trabalho proprio vai de 0.01% a 45%.

    So entram paises com gente la dentro (`minimo_ativos`): mudar de pais para
    um sitio vazio poupa imposto e tira-te o resto do jogo, por isso um pais
    sem populacao ativa nao e recomendacao.
    """
    lista = paises if paises is not None else lista_paises()
    com_taxa = [p for p in lista if isinstance(p.get("taxa_mercado"), (int, float))]
    povoados = [p for p in com_taxa
                if (p.get("populacao_ativa") or 0) >= minimo_ativos] or com_taxa

    def melhores(chave: str) -> list[dict]:
        elegiveis = [p for p in povoados if isinstance(p.get(chave), (int, float))]
        return sorted(elegiveis, key=lambda p: (p[chave], -(p.get("populacao_ativa") or 0)))[:5]

    mercado_ = [p["taxa_mercado"] for p in com_taxa]
    trabalho = [p["taxa_trabalho_proprio"] for p in com_taxa
                if isinstance(p.get("taxa_trabalho_proprio"), (int, float))]
    especializacoes: dict[str, int] = {}
    for p in com_taxa:
        e = p.get("especializacao")
        if e:
            especializacoes[e] = especializacoes.get(e, 0) + 1

    return {
        "n_paises": len(com_taxa),
        "minimo_ativos": minimo_ativos,
        "taxa_mercado_min": min(mercado_) if mercado_ else None,
        "taxa_mercado_max": max(mercado_) if mercado_ else None,
        "taxa_mercado_moda": max(set(mercado_), key=mercado_.count) if mercado_ else None,
        "taxa_trabalho_min": min(trabalho) if trabalho else None,
        "taxa_trabalho_max": max(trabalho) if trabalho else None,
        "vender": melhores("taxa_mercado"),
        "produzir": melhores("taxa_trabalho_proprio"),
        "trabalhar": melhores("taxa_rendimento"),
        "especializacoes": dict(sorted(especializacoes.items(), key=lambda x: -x[1])),
    }


def avisos_paises(comp: dict, taxas_atuais: dict[str, float],
                  melhor_producao: dict | None = None,
                  pais_escolhido: bool = True) -> list[str]:
    fora = []
    if not comp.get("n_paises"):
        return fora

    if comp["vender"]:
        p = comp["vender"][0]
        atual = taxas_atuais.get("market", config.TAXA_MERCADO_PADRAO) * 100
        if p["taxa_mercado"] < atual:
            fora.append(
                f"Taxa de mercado: estas a pagar {atual:.2f}% e o mais barato com gente "
                f"la dentro e {p['nome']} a {p['taxa_mercado']}%. Em 1000 de vendas isso "
                f"e {(atual - p['taxa_mercado']) * 10:.2f} de diferenca."
            )
        else:
            fora.append(
                f"Taxa de mercado: {atual:.2f}% e ja o melhor escalao com populacao ativa "
                f"(vai de {comp['taxa_mercado_min']}% a {comp['taxa_mercado_max']}% no jogo)."
            )

    if comp["produzir"] and comp.get("taxa_trabalho_max") is not None:
        p = comp["produzir"][0]
        atual = taxas_atuais.get("selfwork", 0.0) * 100
        onde = (f" (aqui: {atual:.2f}%)" if pais_escolhido
                else " (nao escolheste pais, por isso o teu nao entra na conta)")
        fora.append(
            f"Imposto sobre trabalho proprio: vai de {comp['taxa_trabalho_min']}% a "
            f"{comp['taxa_trabalho_max']}% conforme o pais{onde}. "
            f"{p['nome']} cobra {p['taxa_trabalho_proprio']}%. E a maior diferenca "
            "fiscal do jogo e nao entra em nenhuma tabela deste painel: as contas de "
            "producao abaixo assumem que o que produzes e teu."
        )
        if melhor_producao and pais_escolhido and atual > 1:
            perda = (melhor_producao.get("lucro_pp") or 0) * atual / 100
            if perda > 0:
                fora.append(
                    f"Com {atual:.2f}% de imposto sobre trabalho proprio, cada PP em "
                    f"{melhor_producao['codigo']} rende {melhor_producao['lucro_pp']:.5f} "
                    f"antes de imposto e perde ate {perda:.5f} para o estado."
                )

    if comp["especializacoes"]:
        top = list(comp["especializacoes"].items())[:3]
        fora.append(
            "Especializacao dos paises (`specializedItem`): "
            + ", ".join(f"{k} em {v} paises" for k, v in top)
            + ". Um bem produzido em muitos paises tem oferta a mais para subir de preco."
        )
    return fora
