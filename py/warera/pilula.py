"""
A pilula nao e um multiplicador: e uma janela.

O painel tratava-a como um `bool` que multiplicava o dano por 1,60. Isso da a
resposta certa para um golpe e a resposta errada para um dia, que e a unica
escala em que a decisao se faz. A regra do jogo tem duas metades e o painel so
usava a primeira:

    +60% durante 8 horas, depois -60% durante 15,5 horas

Quem combate espalhado pelo dia inteiro fica com

    (8 x 1,60 + 0,5 x 1,00 + 15,5 x 0,40) / 24 = 0,81

ou seja **perde 19% de dano por tomar a pilula**, e ainda paga por ela. Quem
concentra o combate dentro da janela leva os 60% inteiros. Entre as duas
leituras ha um factor de 2, e "1,60 o dia todo" nao e nenhuma delas.

Por isso a pergunta que este modulo responde nao e "quanto rende a pilula" mas
sim *ao MEU ritmo de combate, compensa tomar?* — e a resposta e muitas vezes
que nao.

Os numeros vem do `gameConfig` (`items.cocain.flatStats`) sempre que la
estejam; `config.PILULA` e o recurso para quando nao vierem.
"""

from __future__ import annotations

from . import config

HORAS_DIA = 24


def janelas(cfg: dict | None = None) -> dict:
    """
    Duracao e intensidade das duas fases, lidas do jogo quando possivel.

    O jogo guarda isto no proprio item, e nao numa tabela de combate: e por
    isso que o painel conseguia mostrar a pilula na lista de consumiveis e
    mesmo assim modela-la mal no simulador — os dois sitios liam campos
    diferentes.
    """
    p = dict(config.PILULA)
    item = ((cfg or {}).get("items") or {}).get("cocain") or {}
    flat = item.get("flatStats") or {}
    if flat.get("percentAttack"):
        p["ataque_pct"] = float(flat["percentAttack"])
    if flat.get("buffDurationHours"):
        p["horas_buff"] = float(flat["buffDurationHours"])
    if flat.get("debuffDurationHours"):
        p["horas_ressaca"] = float(flat["debuffDurationHours"])
    if flat.get("debuffPercentAttack"):
        p["ressaca_pct"] = float(flat["debuffPercentAttack"])
    p["horas_neutras"] = max(0.0, HORAS_DIA - p["horas_buff"] - p["horas_ressaca"])
    p["fator_buff"] = 1.0 + p["ataque_pct"] / 100.0
    p["fator_ressaca"] = 1.0 + p["ressaca_pct"] / 100.0
    return p


def _fator_instantaneo(desde: float, j: dict) -> float:
    """O multiplicador `desde` horas depois de tomar."""
    if desde < j["horas_buff"]:
        return j["fator_buff"]
    if desde < j["horas_buff"] + j["horas_ressaca"]:
        return j["fator_ressaca"]
    return 1.0


def multiplicador_a_hora(hora: float, tomada_a: float, j: dict,
                         passos: int = 60) -> float:
    """
    O multiplicador medio DENTRO da hora do relogio, tomada a `tomada_a`.

    E uma media dentro da hora e nao o valor no instante inicial porque as
    fases nao caem em horas certas: a ressaca dura 15,5 horas, por isso ha
    sempre uma hora do dia que e meia de ressaca e meia de nada. Arredonda-la
    para um dos lados fazia o dia deixar de somar 24 horas de pilula, e o erro
    aparecia logo na conta mais simples que ha — a media das tres fases.

    O ciclo tem 24 horas, por isso a hora conta-se em circulo: quem toma as
    22h esta em buff as 2h da manha seguinte.
    """
    inicio = (float(hora) - float(tomada_a)) % HORAS_DIA
    n = max(1, int(passos))
    total = sum(_fator_instantaneo((inicio + (i + 0.5) / n) % HORAS_DIA, j)
                for i in range(n))
    return total / n


def _horas_de_combate(inicio: float, horas: float) -> list[float]:
    """As horas do relogio em que se combate, a comecar em `inicio`."""
    n = int(max(0, min(HORAS_DIA, round(horas))))
    return [(float(inicio) + i) % HORAS_DIA for i in range(n)]


def avaliar(golpes_dia: float, dano_por_golpe: float, horas_combate: float,
            hora_inicio: float = 0.0, cfg: dict | None = None,
            preco_pilula: float | None = None,
            tomada_a: float | None = None,
            custo_por_1k_referencia: float | None = None) -> dict:
    """
    O dia inteiro, hora a hora, com e sem pilula.

    `golpes_dia` e o orcamento diario de golpes que a barra de vida e a comida
    pagam — vem de `simulador.dano_disponivel` e nao se inventa aqui. Esses
    golpes distribuem-se pelas horas em que se combate: quem combate 8 horas
    gasta o dia todo de regeneracao dentro dessas 8, e e exactamente isso que
    torna a pilula boa para uns e ma para outros.

    `tomada_a` por omissao e a hora a que o combate comeca, que e a jogada
    certa: a janela e maior do que a maioria das sessoes, por isso adia-la so
    desperdica buff.
    """
    j = janelas(cfg)
    horas = _horas_de_combate(hora_inicio, horas_combate)
    if not horas or golpes_dia <= 0 or dano_por_golpe <= 0:
        return {"erro": "sem golpes ou sem horas de combate"}

    toma = float(hora_inicio if tomada_a is None else tomada_a)
    # ATENCAO: isto espalha os golpes por igual pelas horas de combate, e na
    # pratica eles NAO se espalham — a primeira hora leva a barra de vida
    # inteira e as seguintes so a regeneracao, por isso o dano fica concentrado
    # no inicio, que e onde o buff esta. Esta funcao e portanto um LIMITE
    # INFERIOR do que a pilula rende. Quem quiser o numero verdadeiro usa
    # `warera.sessao`, que simula as barras hora a hora: numa sessao de 24h a
    # diferenca chega a inverter o veredicto (x0,81 aqui, x1,03 la).
    por_hora = float(golpes_dia) / len(horas)

    linhas, dano_com, dano_sem = [], 0.0, 0.0
    for h in horas:
        m = multiplicador_a_hora(h, toma, j)
        d_com = por_hora * dano_por_golpe * m
        d_sem = por_hora * dano_por_golpe
        dano_com += d_com
        dano_sem += d_sem
        linhas.append({
            "hora": int(h),
            "golpes": round(por_hora, 2),
            "multiplicador": round(m, 3),
            "fase": ("buff" if m > 1.0 else "ressaca" if m < 1.0 else "neutra"),
            "dano": round(d_com, 0),
        })

    custo = float(preco_pilula or 0.0)
    ganho = dano_com - dano_sem
    por_1k = (custo / (ganho / 1000.0)) if ganho > 0 and custo > 0 else None

    # VALE O DINHEIRO?
    #
    # A condicao aqui era `ganho > 0 and (custo <= 0 or ganho > 0)`, que e
    # `ganho > 0` escrito duas vezes: o custo NUNCA entrava na decisao. Uma
    # pilula que desse +1 de dano e custasse 500 saia como "compensa".
    #
    # Mas dano e dinheiro sao unidades diferentes e nao se comparam sem uma
    # referencia. A referencia certa e o que o resto da build ja paga por 1000
    # de dano: se a pilula entrega dano extra mais caro do que isso, o mesmo
    # dinheiro rende mais gasto noutra coisa. Sem referencia nao se opina —
    # fica a None, que e diferente de "sim" e de "nao".
    vale = None
    if ganho > 0 and custo > 0 and custo_por_1k_referencia:
        vale = por_1k is not None and por_1k <= float(custo_por_1k_referencia)
    elif ganho > 0 and custo <= 0:
        vale = True
    elif ganho <= 0:
        vale = False

    return {
        "horas": linhas,
        "tomada_a": int(toma),
        "golpes_dia": round(float(golpes_dia), 1),
        "dano_dia_com_pilula": round(dano_com, 0),
        "dano_dia_sem_pilula": round(dano_sem, 0),
        "ganho_dano": round(ganho, 0),
        "ganho_pct": round((dano_com / dano_sem - 1) * 100, 2) if dano_sem else None,
        "multiplicador_medio": round(dano_com / dano_sem, 4) if dano_sem else None,
        "horas_em_buff": sum(1 for l in linhas if l["fase"] == "buff"),
        "horas_em_ressaca": sum(1 for l in linhas if l["fase"] == "ressaca"),
        "custo": round(custo, 2) if preco_pilula is not None else None,
        # o numero que decide: quanto custa o dano EXTRA que a pilula traz. Se
        # o ganho for negativo nao ha custo por dano nenhum — ha prejuizo duas
        # vezes, no dano e no preco.
        "custo_por_1k_ganho": round(por_1k, 4) if por_1k is not None else None,
        # `compensa` responde SO em dano: a pilula acrescenta dano ao dia?
        "compensa": ganho > 0,
        # e esta responde em dinheiro, quando ha com que comparar
        "vale_o_dinheiro": vale,
        "custo_por_1k_referencia": custo_por_1k_referencia,
        "janela": j,
    }


def melhor_hora(golpes_dia: float, dano_por_golpe: float, horas_combate: float,
                hora_inicio: float = 0.0, cfg: dict | None = None,
                preco_pilula: float | None = None,
                custo_por_1k_referencia: float | None = None) -> dict:
    """
    A que horas tomar, dadas as horas em que se combate.

    Varre as 24 horas possiveis de toma e devolve a melhor, mais a comparacao
    com nao tomar nada. Sao 24 avaliacoes de umas dezenas de contas: nao vale a
    pena ser esperto, e assim nao ha heuristica nenhuma para estar errada.
    """
    melhor, melhor_chave, tabela = None, None, []
    for t in range(HORAS_DIA):
        r = avaliar(golpes_dia, dano_por_golpe, horas_combate, hora_inicio,
                    cfg, preco_pilula, tomada_a=float(t),
                    custo_por_1k_referencia=custo_por_1k_referencia)
        if "erro" in r:
            return r
        tabela.append({"tomada_a": t, "dano_dia": r["dano_dia_com_pilula"],
                       "ganho_pct": r["ganho_pct"]})
        # Empate desfeito a favor de tomar o mais perto possivel do inicio da
        # sessao. Uma sessao curta cabe na janela a partir de varias horas de
        # toma e todas dao o mesmo dano HOJE — mas tomar cedo demais queima
        # buff antes de se comecar a bater e adianta a ressaca, que e o que
        # estraga a sessao seguinte.
        desperdicio = (float(hora_inicio) - t) % HORAS_DIA
        chave = (round(r["dano_dia_com_pilula"], 6), -desperdicio)
        if melhor_chave is None or chave > melhor_chave:
            melhor, melhor_chave = r, chave
    return {**melhor, "por_hora_de_toma": tabela}


def veredicto(golpes_dia: float, dano_por_golpe: float, horas_combate: float,
              hora_inicio: float = 0.0, cfg: dict | None = None,
              preco_pilula: float | None = None,
              custo_por_1k_referencia: float | None = None) -> dict:
    """
    Tomar ou nao tomar, com a razao escrita.

    E a unica funcao deste modulo que o painel precisa de chamar para decidir.
    A resposta depende so de uma coisa — quantas horas por dia se combate — e a
    fronteira anda a volta da duracao do buff: quem cabe dentro dele ganha
    tudo, quem nao cabe comeca a pagar a ressaca.
    """
    j = janelas(cfg)
    r = melhor_hora(golpes_dia, dano_por_golpe, horas_combate, hora_inicio,
                    cfg, preco_pilula, custo_por_1k_referencia)
    if "erro" in r:
        return r

    if r["ganho_pct"] is None:
        razao = "sem dano de referencia para comparar"
    elif r["ganho_pct"] <= 0:
        razao = (f"combatendo {horas_combate:.0f}h por dia, {r['horas_em_ressaca']} dessas "
                 f"horas caem na ressaca de {j['horas_ressaca']:.1f}h: a pilula tira "
                 f"{abs(r['ganho_pct']):.1f}% de dano em vez de dar")
    elif horas_combate <= j["horas_buff"]:
        razao = (f"as {horas_combate:.0f}h de combate cabem inteiras na janela de "
                 f"{j['horas_buff']:.0f}h: leva-se o bonus todo, +{r['ganho_pct']:.1f}%")
    else:
        razao = (f"{r['horas_em_buff']}h dentro do buff e {r['horas_em_ressaca']}h na "
                 f"ressaca dao +{r['ganho_pct']:.1f}% liquidos — concentrar mais o "
                 f"combate sobe este numero")

    if (r.get("vale_o_dinheiro") is False and r["ganho_pct"]
            and r["ganho_pct"] > 0):
        razao += (f" — mas o dano extra sai a {r['custo_por_1k_ganho']:.4f} por 1K "
                  f"e o resto da build já faz dano a "
                  f"{float(custo_por_1k_referencia):.4f}, por isso o dinheiro "
                  "rende mais noutro sítio")

    return {
        "compensa": bool(r["ganho_pct"] and r["ganho_pct"] > 0),
        # em dano e em dinheiro sao perguntas diferentes: a pilula pode dar
        # dano e mesmo assim ser a pior compra do dia
        "vale_o_dinheiro": r.get("vale_o_dinheiro"),
        "razao": razao,
        # o veredicto e conservador de proposito: assume os golpes espalhados
        # por igual, e na realidade eles concentram-se no inicio da sessao
        "e_limite_inferior": True,
        "nota_modelo": ("assume os golpes espalhados por igual pelas horas de "
                        "combate; na prática a primeira hora leva a barra "
                        "inteira e o dano concentra-se no buff, por isso a "
                        "pílula rende mais do que isto — `warera.sessao` dá o "
                        "número com as barras a mexer"),
        "tomar_as": r["tomada_a"],
        "ganho_pct": r["ganho_pct"],
        "ganho_dano": r["ganho_dano"],
        "custo_por_1k_ganho": r["custo_por_1k_ganho"],
        "horas_maximas_sem_perder": round(j["horas_buff"], 1),
        "detalhe": r,
    }
