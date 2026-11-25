"""
O motor de combate. Uma implementacao, e so uma.

Antes deste ficheiro havia tres: o `simulador.py`, o `pecas.py` e uma copia
inteira em JavaScript dentro do `painel.html`. As tres tinham de concordar e
nao havia nada que as obrigasse a isso a nao ser testes de paridade — que
apanham a divergencia DEPOIS de ela existir, e apanharam mesmo: a esquiva
esteve certa num lado e errada no outro.

A regra aqui e simples: quem quiser saber quanto dano faz uma build chama este
modulo. O optimizador chama-o, o simulador chama-o, a calculadora chama-o, a
API chama-o. Nao ha segunda formula em lado nenhum.

DUAS COISAS QUE ESTE MOTOR FAZ DE PROPOSITO
-------------------------------------------

**Nao arredonda.** Os resultados saem em precisao total. Arredondar no meio de
uma conta e depois multiplicar por mil golpes propaga o erro; foi por isso que
a `fronteira_dano` e o `melhor_build` chegaram a discordar em 1,7 de dano com a
mesma build exacta. Quem apresenta arredonda; quem calcula nao.

**Separa o que se ve do que se espera.** O jogo mostra no ecra o custo de um
golpe que NAO foi esquivado; a sustentabilidade depende do custo MEDIO, com a
esquiva descontada. Sao dois numeros diferentes e misturar-los foi o erro que
manteve as botas fora de todos os planos deste painel.
"""

from __future__ import annotations

from . import regras

# ---------------------------------------------------------------------------
#  Blocos elementares. Cada um e uma regra do jogo e nada mais.
# ---------------------------------------------------------------------------


def defesa_efetiva(total: float, curva: float) -> float:
    """
    `efetivo = total / (total + curva)`.

    Nao e um tecto, sao rendimentos decrescentes: 40 pontos dao 50%, 200 dao
    83%, e nunca chega a 100%. Vale para armadura e para esquiva.
    """
    t = max(0.0, float(total or 0))
    return t / (t + curva) if t > 0 else 0.0


def custo_esperado(armor, dodge, v: dict) -> float:
    """
    O que custa, EM MEDIA, tentar um golpe.

    Duas coisas diferentes a multiplicarem-se. A armadura baixa o custo de cada
    golpe que se paga; a esquiva faz com que uma fraccao dos golpes nao se
    pague de todo. Sao independentes e por isso multiplicam.

    Existe como funcao propria porque a mesma conta era precisa em tres sitios
    — o golpe, a fronteira de dano e a build completa — e nos tres estava
    escrita a mao. Uma delas ainda lia o `config.COMBATE` em vez do RULESET.
    """
    armadura = defesa_efetiva(armor, v["curva_defesa"])
    esquiva = defesa_efetiva(dodge, v["curva_defesa"])
    return (v["vida_por_golpe"] * (1 - armadura)
            * (1 - esquiva * v["esquiva_poupa_vida"]))


def transbordo(stats: dict, v: dict) -> tuple[float, float, float, float]:
    """
    (ataque, critico, dano_critico, precisao) depois das conversoes do jogo.

    A precisao acima de 100 deixa de tirar falhas — ja nao ha falhas para tirar
    — e passa a somar ataque. O critico acima de 100 passa a somar dano
    critico. Os tectos sao 100 nos dois casos e nao o maximo da skill: a skill
    vai a 100 mas as luvas dao mais 60 por cima, e e esse excesso que
    transborda.
    """
    a = max(0.0, float(stats.get("attack") or 0.0))
    # negativos nao existem no jogo e entram aqui por engano de quem chama (um
    # campo vazio, um stat a subtrair de equipamento que ja nao esta vestido).
    # Deixa-los passar produzia percentagens negativas na decomposicao e um
    # "dano minimo" maior do que o maximo, que sao numeros impossiveis a serem
    # apresentados como leituras
    c = max(0.0, float(stats.get("criticalChance") or 0.0))
    d = max(0.0, float(stats.get("criticalDamages") or 0.0))
    p = stats.get("precision")
    # sem precisao conhecida assume-se que acerta sempre: modelar as falhas a
    # zero daria metade do dano a toda a gente
    p = v["precisao_max"] if p is None else max(0.0, float(p))

    if p > v["precisao_max"]:
        a += (p - v["precisao_max"]) * v["transbordo_precisao"]
        p = v["precisao_max"]
    if c > v["critico_max"]:
        d += (c - v["critico_max"]) * v["transbordo_critico"]
        c = v["critico_max"]
    return a, c, d, p


def dano_por_golpe(ataque: float, critico: float, dano_critico: float,
                   precisao: float, v: dict) -> float:
    """
    E[dano] = ataque x [ p x ((1-c) + c x (1 + d/100)) + (1-p) x 0,5 ]

    Uma falha da metade do dano e nunca e critica. Se calhar critico e falha ao
    mesmo tempo, a falha ganha — e por isso que o termo da falha esta fora do
    parentesis do critico e nao dentro.
    """
    c = min(max(critico, 0.0), v["critico_max"]) / 100.0
    p = min(max(precisao, 0.0), v["precisao_max"]) / 100.0
    acerto = (1 - c) + c * (1 + dano_critico / 100.0)
    return ataque * (p * acerto + (1 - p) * v["dano_falha"])


def multiplicador(v: dict, municao: str | None = None, pilula: bool = False,
                  posto_pct: float = 0.0, batalha: float = 1.0,
                  fatores: dict[str, float] | None = None) -> float:
    """
    Os multiplicadores do ataque, todos multiplicativos entre si.

    `pilula=True` significa DENTRO DA JANELA DE BUFF e nao "tomou hoje": a
    pilula da +60% durante 8 horas e -60% durante 15,5. Quem quer a conta do
    dia usa `warera.sessao`, que distribui os golpes pelas horas.

    `fatores` sao multiplicadores dados em CRU, para quem tem o numero e nao o
    nome — a calculadora de mecanicas tem caixas de "x1,40" em vez de
    "heavyAmmo". Entram pela mesma porta e pela mesma regra, que e o ponto:
    "todos multiplicativos entre si" e uma regra do jogo e nao pode viver numa
    linha de JavaScript ao lado de uma caixa de texto.
    """
    m = 1.0
    if municao:
        m *= float((v.get("municao") or {}).get(municao, 1.0))
    if pilula:
        m *= 1.0 + float(v["pilula"]["ataque_pct"]) / 100.0
    m *= 1.0 + float(posto_pct or 0.0) / 100.0
    m *= max(0.0, float(batalha or 1.0))
    for f in (fatores or {}).values():
        m *= max(0.0, float(f or 1.0))
    return m


def golpe_cru(stats: dict, rs: dict, fatores: dict[str, float] | None = None) -> dict:
    """
    Um golpe com os multiplicadores dados em cru, e a decomposicao ao lado.

    E a mesma conta de `golpe` — literalmente a mesma funcao — servida para a
    calculadora de mecanicas, onde o utilizador escreve os factores a mao. Ela
    tinha isto reimplementado em JavaScript, com a formula do dano, o
    transbordo e o meio dano da falha escritos outra vez. Passou a pedir.
    """
    v = regras.valores(rs)
    m = multiplicador(v, fatores=fatores)
    g = golpe(stats, rs, batalha=m)
    a = g["ataque_efetivo"]
    p, c, d = g["precisao_efetiva"], g["critico_efetivo"], g["dano_critico_efetivo"]
    return {
        **g,
        "fatores": dict(fatores or {}),
        "sem_multiplicadores": golpe(stats, rs)["dano_por_golpe"],
        "linhas": [
            {"o_que": "golpes que acertam", "valor": p / 100.0, "pct": True},
            {"o_que": "falhas (metade do dano, nunca críticas)",
             "valor": 1 - p / 100.0, "pct": True},
            {"o_que": "golpes críticos", "valor": (p / 100.0) * (c / 100.0),
             "pct": True},
            {"o_que": "golpe normal", "valor": a},
            {"o_que": "golpe falhado", "valor": a * v["dano_falha"]},
            {"o_que": "golpe crítico", "valor": a * (1 + d / 100.0)},
        ],
    }


# ---------------------------------------------------------------------------
#  O golpe
# ---------------------------------------------------------------------------


def golpe(stats: dict, rs: dict, municao: str | None = None,
          pilula: bool = False, posto_pct: float = 0.0,
          batalha: float = 1.0) -> dict:
    """
    Tudo o que se sabe sobre UM golpe desta build. Sem arredondar.

    O ataque que vem da precisao excedente TAMBEM e multiplicado pela municao,
    pela pilula, pelo posto e pelos bonus: o transbordo soma-se ao ataque cru
    e so depois se multiplica tudo.

    Este docstring ja dizia isto, e o codigo fazia o contrario — multiplicava
    o ataque e somava o transbordo por cima, cru. O proprio jogo desmente essa
    ordem: um jogador real com 300 de skill, 147 da arma e 18 de precisao a
    mais (72 de ataque) tem `skills.attack.total = 1439`, que e
    `(300 + 147 + 72) x 1,10 x 1,60 x 1,575`. Com o transbordo cru dava 1311,
    ou seja 9% de dano a menos em quem tem as luvas a passar o tecto.
    """
    v = regras.valores(rs)
    m = multiplicador(v, municao, pilula, posto_pct, batalha)

    a, c, d, p = transbordo(stats, v)
    a *= m
    dano = dano_por_golpe(a, c, d, p, v)

    armadura = defesa_efetiva(stats.get("armor"), v["curva_defesa"])
    esquiva = defesa_efetiva(stats.get("dodge"), v["curva_defesa"])

    # o que custa um golpe que NAO foi esquivado: e o numero que o jogo mostra
    vida = v["vida_por_golpe"] * (1 - armadura)
    # e o custo MEDIO por tentativa, que e o que decide quantos golpes cabem
    vida_esperada = custo_esperado(stats.get("armor"), stats.get("dodge"), v)

    return {
        "multiplicador": m,
        "ataque_efetivo": a,
        "precisao_efetiva": p,
        "critico_efetivo": c,
        "dano_critico_efetivo": d,
        "dano_por_golpe": dano,
        "taxa_falha": 1.0 - min(max(p, 0.0), v["precisao_max"]) / 100.0,
        "taxa_critico": min(max(c, 0.0), v["critico_max"]) / 100.0 * (p / 100.0),
        "taxa_esquiva": esquiva,
        "armadura_efetiva": armadura,
        "esquiva_efetiva": esquiva,
        "vida_por_golpe": vida,
        "vida_por_golpe_esperada": vida_esperada,
        # O MELHOR E O PIOR GOLPE **POSSIVEIS**, e a palavra e essa.
        #
        # Isto dizia sempre `a x 0,5` e `a x (1 + d/100)`, houvesse ou nao
        # falhas e criticos para dar. Uma build com precisao 100 nao falha
        # nenhum golpe — `taxa_falha` e zero na linha acima — e mesmo assim o
        # painel mostrava meio dano como o pior caso, um numero que aquela
        # build nunca produz. O mesmo do outro lado: sem hipotese de critico
        # nenhuma, o golpe maximo e o normal e nao o critico.
        #
        # Um intervalo que inclui valores impossiveis nao e um intervalo: e
        # dois numeros ao lado um do outro.
        "dano_minimo": (a * v["dano_falha"]) if p < v["precisao_max"] else a,
        "dano_maximo": (a * (1 + d / 100.0)) if c > 0 else a,
        "pode_falhar": p < v["precisao_max"],
        "pode_criticar": c > 0,
    }


# ---------------------------------------------------------------------------
#  Quantos golpes cabem
# ---------------------------------------------------------------------------


def sustentar(stats: dict, rs: dict, g: dict | None = None,
              comida_vida_pct: float | None = None, horas: float = 24.0,
              horas_combate: float | None = None, **kw) -> dict:
    """
    Quantos golpes a build aguenta, e de onde vem cada um.

    Ha duas fontes e nao uma. A **vida** regenera uma fraccao do maximo por
    hora; a **fome** regenera ao mesmo ritmo e cada item de comida converte-se
    em vida. Ignorar a segunda subestimava os golpes por dia em cerca de tres
    vezes, e escondia que para quem come o estrangulamento deixa de ser a vida
    e passa a ser a fome — o que muda qual e a skill a subir.

    A esquiva entra aqui pela porta grande: golpes esquivados nao gastam vida,
    logo mais esquiva sao mais golpes pelo mesmo deposito.

    DUAS JANELAS E NAO UMA. `horas` e a JANELA DE SUSTENTO — quanto tempo se
    conta a regeneracao, normalmente as 24 horas do dia. `horas_combate` e
    quanto tempo por dia se esta mesmo a bater, e so serve para saber a que
    ritmo os golpes saem: a barra regenera esteja-se a combater ou nao, por isso
    uma sessao de 8 horas continua a ter as 24 horas de regeneracao do dia
    atras dela. Foi confundir as duas que pos o "dano por dia" a contar so as 8
    horas da janela da pilula.
    """
    v = regras.valores(rs)
    g = g or golpe(stats, rs, **kw)
    vida_max = float(stats.get("health") or 0.0)
    fome_max = float(stats.get("hunger") or 0.0)
    custo = g["vida_por_golpe_esperada"]
    if vida_max <= 0 or custo <= 0:
        return {**g, "golpes": 0.0, "golpes_vida": 0.0, "golpes_comida": 0.0,
                "limitado_por": None, "erro": "esta build nao consegue bater"}

    regen = v["regen_por_hora"]
    golpes_vida = (vida_max * regen * horas) / custo

    golpes_comida = 0.0
    comidas = 0.0
    if comida_vida_pct and fome_max > 0:
        comidas = fome_max * regen * horas
        golpes_comida = comidas * (vida_max * float(comida_vida_pct) / 100.0) / custo

    golpes = golpes_vida + golpes_comida
    janela = float(horas_combate) if horas_combate else float(horas)
    return {
        **g,
        "horas": horas,
        "horas_combate": janela,
        "vida_maxima": vida_max,
        "fome_maxima": fome_max,
        "golpes": golpes,
        "golpes_vida": golpes_vida,
        "golpes_comida": golpes_comida,
        "comidas": comidas,
        "reserva_golpes": vida_max / custo,
        "limitado_por": "fome" if golpes_comida > golpes_vida else "vida",
        "dano_total": golpes * g["dano_por_golpe"],
        "vida_gasta": golpes * custo,
        # o ritmo a que os golpes SAEM, que nao e o mesmo que o ritmo a que
        # entram: os golpes do dia inteiro concentram-se na janela em que se
        # esta mesmo a bater
        "golpes_por_hora_combate": (golpes / janela) if janela > 0 else None,
        "golpes_por_hora_sustentavel": (golpes / horas) if horas > 0 else None,
    }


# ---------------------------------------------------------------------------
#  As quatro unidades em que este painel fala
# ---------------------------------------------------------------------------


def metricas(g: dict, s: dict, c: dict | None = None) -> dict:
    """
    O mesmo resultado nas quatro unidades, com a definicao de cada uma ao lado.

    Existe porque "dano por hora" significava coisas diferentes em sitios
    diferentes do painel — ora o dano de uma hora de regeneracao, ora o de uma
    hora a bater sem parar — e as duas leituras diferem por um factor de tres
    para quem combate 8 horas. Quatro unidades, nomeadas:

        por_golpe     um golpe. Nao depende de tempo nenhum.
        rajada        o que sai de uma barra cheia, de uma assentada. E o tecto
                      de um tick de batalha e nao um ritmo.
        por_hora      duas leituras, e por isso vem com as duas: `combate` (o
                      ritmo enquanto se bate) e `sustentavel` (a media do dia,
                      que e a que o mundo ve).
        por_dia       o total do dia, que e o unico que se compara com rankings.

    Nada disto e uma conta nova: sao os numeros de `golpe` e `sustentar`
    divididos pelo tempo certo. Ter as divisoes num sitio so e o que impede que
    voltem a aparecer duas versoes da mesma.
    """
    dpg = float(g.get("dano_por_golpe") or 0.0)
    golpes = float(s.get("golpes") or 0.0)
    horas = float(s.get("horas") or 24.0)
    janela = float(s.get("horas_combate") or horas)
    reserva = float(s.get("reserva_golpes") or 0.0)
    custo_golpe = float((c or {}).get("custo_por_golpe") or 0.0)

    return {
        "por_golpe": {
            "dano": dpg,
            "dano_minimo": g.get("dano_minimo"),
            "dano_maximo": g.get("dano_maximo"),
            "vida": g.get("vida_por_golpe"),
            "vida_esperada": g.get("vida_por_golpe_esperada"),
            "custo": custo_golpe or None,
            "o_que": "um golpe, sem tempo nenhum pelo meio",
        },
        "rajada": {
            "golpes": reserva,
            "dano": reserva * dpg,
            "o_que": ("o que sai de uma barra de vida cheia, de seguida; e um "
                      "tecto e nao um ritmo"),
        },
        "por_hora": {
            "combate": {
                "golpes": s.get("golpes_por_hora_combate"),
                "dano": (golpes / janela * dpg) if janela > 0 else None,
                "o_que": f"enquanto se bate ({janela:g}h por dia)",
            },
            "sustentavel": {
                "golpes": s.get("golpes_por_hora_sustentavel"),
                "dano": (golpes / horas * dpg) if horas > 0 else None,
                "o_que": f"a media das {horas:g}h da janela de sustento",
            },
        },
        "por_dia": {
            "golpes": golpes,
            "dano": float(s.get("dano_total") or 0.0),
            "custo": (c or {}).get("opex_por_dia"),
            "limitado_por": s.get("limitado_por"),
            "o_que": "o total que a regeneracao paga; e o que conta no ranking",
        },
        "sustentavel": {
            "dano_dia": float(s.get("dano_total") or 0.0),
            "custo_dia": (c or {}).get("opex_por_dia"),
            "custo_por_1k_dano": (c or {}).get("custo_por_1k_dano"),
            "dano_por_moeda": (c or {}).get("dano_por_moeda"),
            "o_que": ("o que se aguenta indefinidamente: nao gasta mais vida do "
                      "que a que regenera"),
        },
    }


# ---------------------------------------------------------------------------
#  Quanto custa
# ---------------------------------------------------------------------------


def custear(s: dict, rs: dict, precos: dict | None = None,
            municao: str | None = None, comida: str | None = None,
            comida_vida_pct: float | None = None, com_pilula: bool = False,
            pecas_vestidas: dict | None = None, custos_scrap: dict | None = None,
            preco_scrap: float | None = None, dias: float = 1.0) -> dict:
    """
    CAPEX e OPEX, separados — porque nao se somam nem se comparam.

    **CAPEX** e o que custa MONTAR a build: comprar as pecas, uma vez. Nao se
    repete e nao entra no custo por dano.

    **OPEX** e o que custa USAR a build: municao e comida por golpe, a pilula
    por dia, e o DESGASTE do equipamento — que e a parcela que este painel
    ignorou mais tempo e que num conjunto caro pesa mais do que a municao e a
    comida juntas.

    Misturar os dois da o numero errado nos dois sentidos: quem compara builds
    pelo CAPEX ignora que a cara se gasta mais depressa, e quem compara pelo
    OPEX sozinho esquece que uma delas custou mil para entrar em campo.
    """
    v = regras.valores(rs)
    p = precos or {}
    golpes = float(s.get("golpes") or 0.0) * float(dias)

    c_mun = float(p.get(municao) or 0.0) if municao else 0.0

    # QUANTA COMIDA POR GOLPE, e nao so quanto dinheiro.
    #
    # O numero de itens por golpe e uma regra de sustentabilidade — a comida
    # repoe uma percentagem do MAXIMO, por isso mais vida e mais armadura
    # baratejam o golpe — e estava a ser calculado no frontend para encher uma
    # caixa de texto, com o custo NOMINAL do golpe em vez do esperado. Ou seja
    # com o modelo antigo da esquiva, outra vez.
    c_com = 0.0
    comida_por_golpe = None
    vida_max = float(s.get("vida_maxima") or 0.0)
    if comida and comida_vida_pct and vida_max > 0:
        vida_por_item = vida_max * float(comida_vida_pct) / 100.0
        if vida_por_item > 0:
            comida_por_golpe = s["vida_por_golpe_esperada"] / vida_por_item
            c_com = float(p.get(comida) or 0.0) * comida_por_golpe

    # Desgaste: SO se houver pecas vestidas e preco delas. Nao se inventa um
    # custo de equipamento a partir dos stats — uma build e um conjunto de
    # numeros e nao um conjunto de pecas, e cobrar-lhe um desgaste imaginado
    # seria pior do que nao o cobrar.
    #
    # O DESGASTE TEM UM DONO: `warera.durabilidade`.
    #
    # Esta funcao tinha o calculo do desgaste escrito outra vez — o preco menos
    # o que a peca devolve desfeita, a dividir por cem golpes, com a esquiva a
    # poupar as cinco pecas e nunca a arma. Era a mesma conta em dois sitios, e
    # ja tinham comecado a divergir: `durabilidade` desconta a taxa de mercado
    # no valor de reciclagem e esta nao descontava, por isso a mesma build
    # saia com dois custos de desgaste conforme o caminho por onde a pergunta
    # entrava.
    capex = 0.0
    c_desg = 0.0
    linhas_desg = []
    if pecas_vestidas:
        from . import durabilidade as _dur

        esquiva = (float(s.get("esquiva_efetiva") or 0.0)
                   * v["esquiva_poupa_durabilidade"])
        d = _dur.custo_por_golpe(pecas_vestidas, p, custos_scrap, preco_scrap,
                                 esquiva_efetiva=esquiva)
        c_desg = float(d["total"])
        linhas_desg = d["linhas"]
        capex = sum(float(p.get(c) or 0.0)
                    for c in pecas_vestidas.values() if c)

    por_golpe = c_mun + c_com + c_desg
    c_pilula = float(p.get("cocain") or 0.0) * float(dias) if com_pilula else 0.0
    opex = por_golpe * golpes + c_pilula
    dano = float(s.get("dano_total") or 0.0) * float(dias)

    return {
        "dias": dias,
        "golpes": golpes,
        "capex": capex,
        "opex": opex,
        "opex_por_dia": opex / dias if dias else None,
        "custo_por_golpe": por_golpe,
        "custo_municao": c_mun,
        "custo_comida": c_com,
        "comida_por_golpe": comida_por_golpe,
        "comida_por_dia": (comida_por_golpe * golpes
                           if comida_por_golpe is not None else None),
        "custo_desgaste": c_desg,
        "custo_pilula": c_pilula,
        "desgaste_linhas": linhas_desg,
        "dano_total": dano,
        # o numero com que se comparam builds: so OPEX, porque o CAPEX nao se
        # repete e diluir-lo por um horizonte arbitrario esconde-o
        "custo_por_1k_dano": (opex / (dano / 1000.0)) if dano > 0 else None,
        "custo_por_1k_dano_com_capex": (
            (opex + capex) / (dano / 1000.0)) if dano > 0 else None,
        "dano_por_moeda": (dano / opex) if opex > 0 else None,
    }


# ---------------------------------------------------------------------------
#  Tudo junto: a resposta completa para uma build
# ---------------------------------------------------------------------------


def avaliar(stats: dict, rs: dict, municao: str | None = None,
            pilula: bool = False, posto_pct: float = 0.0, batalha: float = 1.0,
            comida_vida_pct: float | None = None, horas: float = 24.0,
            precos: dict | None = None, comida: str | None = None,
            pecas_vestidas: dict | None = None, custos_scrap: dict | None = None,
            preco_scrap: float | None = None, dias: float = 1.0,
            horas_combate: float | None = None,
            explicar: bool = False) -> dict:
    """
    A resposta unica: golpe, sustentabilidade e custo, de uma vez.

    E isto que a API devolve e o que o optimizador pontua. Um sitio so para
    perguntar "quanto vale esta build", com tudo o que decide: nao so o dano
    por golpe, mas quantos golpes cabem e quanto custam.
    """
    g = golpe(stats, rs, municao=municao, pilula=pilula, posto_pct=posto_pct,
              batalha=batalha)
    s = sustentar(stats, rs, g=g, comida_vida_pct=comida_vida_pct, horas=horas,
                  horas_combate=horas_combate)
    c = custear(s, rs, precos=precos, municao=municao, comida=comida,
                comida_vida_pct=comida_vida_pct, com_pilula=pilula,
                pecas_vestidas=pecas_vestidas, custos_scrap=custos_scrap,
                preco_scrap=preco_scrap, dias=dias)
    fora = {"golpe": g, "sustento": s, "custo": c, "stats": dict(stats),
            "metricas": metricas(g, s, c)}
    if explicar:
        fora["explicacao"] = explicacao(stats, rs, g, s, municao, pilula,
                                        posto_pct, batalha)
    return fora


def explicacao(stats: dict, rs: dict, g: dict, s: dict,
               municao: str | None, pilula: bool, posto_pct: float,
               batalha: float) -> dict:
    """
    A cadeia toda, passo a passo, com o valor de cada linha.

    Um painel que so mostra o resultado obriga a acreditar nele. Isto e o
    contrario: da para conferir a mao contra o ecra do jogo, que e a unica
    maneira honesta de saber se o motor esta certo.
    """
    v = regras.valores(rs)
    # o MESMO ataque de partida que o motor usou, e nao o cru que entrou. O
    # `transbordo` corta os negativos a zero; ler o cru aqui fazia a cadeia
    # comecar em -50 e acabar em -55 ao lado de um resultado de 0 — uma
    # explicacao a contradizer o numero que estava a explicar
    a0 = max(0.0, float(stats.get("attack") or 0.0))
    # o transbordo soma-se ANTES dos multiplicadores, como no golpe: e a ordem
    # que o `skills.attack.total` do jogo confirma
    a1 = transbordo(stats, v)[0]
    mun = float((v.get("municao") or {}).get(municao, 1.0)) if municao else 1.0
    pil = (1.0 + v["pilula"]["ataque_pct"] / 100.0) if pilula else 1.0
    rank = 1.0 + float(posto_pct or 0.0) / 100.0
    bat = max(0.0, float(batalha or 1.0))

    return {
        "ataque": [
            {"passo": "skill + equipamento", "valor": a0},
            {"passo": "+ transbordo de precisão", "valor": a1,
             "nota": f"+{a1 - a0:g} de precisão acima de {v['precisao_max']:g}, "
                     "e multiplica-se com o resto"},
            {"passo": f"× munição ({municao or 'nenhuma'})", "fator": mun,
             "valor": a1 * mun},
            {"passo": "× pílula" if pilula else "sem pílula", "fator": pil,
             "valor": a1 * mun * pil,
             "nota": "só dentro da janela de buff; fora dela o fator é menor que 1"},
            {"passo": f"× posto militar (+{posto_pct:g}%)", "fator": rank,
             "valor": a1 * mun * pil * rank},
            {"passo": "× bónus de batalha", "fator": bat,
             "valor": g["ataque_efetivo"],
             "nota": "todos os bónus somados, a multiplicar uma vez só"},
        ],
        "dano": [
            {"passo": "ataque efetivo", "valor": g["ataque_efetivo"]},
            {"passo": f"crítico {g['critico_efetivo']:g}% × "
                      f"{g['dano_critico_efetivo']:g}% extra",
             "valor": g["ataque_efetivo"] * (
                 1 + g["critico_efetivo"] / 100.0 * g["dano_critico_efetivo"] / 100.0)},
            {"passo": f"acerta {g['precisao_efetiva']:g}% das vezes",
             "nota": f"falha {g['taxa_falha'] * 100:g}%, a meio dano, e uma "
                     f"falha nunca é crítica"},
            {"passo": "= dano médio por golpe", "valor": g["dano_por_golpe"]},
        ],
        "vida": [
            {"passo": "custo base de tentar um golpe", "valor": v["vida_por_golpe"]},
            {"passo": f"− armadura ({stats.get('armor', 0):g} pontos)",
             "fator": 1 - g["armadura_efetiva"], "valor": g["vida_por_golpe"],
             "nota": f"efetivo {g['armadura_efetiva'] * 100:.1f}% "
                     f"(total/(total+{v['curva_defesa']:g}))"},
            {"passo": f"− esquiva ({stats.get('dodge', 0):g} pontos)",
             "fator": 1 - g["esquiva_efetiva"],
             "valor": g["vida_por_golpe_esperada"],
             "nota": f"um golpe esquivado é 'a free hit' e não gasta vida: "
                     f"{g['esquiva_efetiva'] * 100:.1f}% dos golpes saem de graça"},
            {"passo": "= custo esperado por tentativa",
             "valor": g["vida_por_golpe_esperada"]},
        ],
        "golpes": [
            {"passo": "da regeneração da vida", "valor": s.get("golpes_vida")},
            {"passo": "da comida (fome × cura por item)",
             "valor": s.get("golpes_comida")},
            {"passo": "= golpes no período", "valor": s.get("golpes"),
             "nota": f"limitado por {s.get('limitado_por')}"},
        ],
    }
