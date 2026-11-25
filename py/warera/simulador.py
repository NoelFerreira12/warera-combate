"""
Quanto dano faz uma build, e quanto custa la chegar.

O painel ja dizia onde o proximo ponto rende mais. Isso responde "e a seguir?",
mas nao responde a pergunta que se faz antes de gastar: *se eu puser a
precisao no 8 e o critico no 6, quanto dano fico a fazer, e quanto me custa?*

Sao duas contas diferentes e as duas vivem aqui:

  dano     o mesmo modelo de `pecas`, sem uma segunda versao: as conversoes de
           transbordo, as falhas que dao metade, e os multiplicadores de
           municao, pilula, posto e batalha, que sao multiplicativos entre si.

  custo    os pontos de skill saem da escada do jogo (`totalCost` acumulado, e
           nao a soma dos passos), e o dinheiro sai do mercado. Um plano que
           precise de 40 pontos que nao tens nao e um plano, e por isso a
           resposta diz sempre quantos pontos faltam.

O painel corre este mesmo modelo em JavaScript, porque os cursores tem de
responder ao dedo e nao a um pedido de rede. Duas implementacoes da mesma regra
separam-se com o tempo: `tests/test_offline.py` corre as duas sobre a mesma
grelha de builds e exige o mesmo numero.
"""

from __future__ import annotations

from . import config, motor, pecas, regras

# a ordem em que os cursores aparecem: ataque primeiro porque e o que multiplica
# tudo o resto, e vida no fim porque nao entra no dano
STATS_SIMULAVEIS = ["attack", "precision", "criticalChance", "criticalDamages",
                    "armor", "dodge", "health"]


def valor_no_nivel(escada: list[dict], nivel: int) -> float:
    """O valor da skill nesse nivel, tirado da escada do proprio jogo."""
    for passo in escada or []:
        if passo.get("nivel") == nivel:
            return float(passo.get("valor") or 0)
    return 0.0


def custo_ate(escada: list[dict], nivel: int) -> int:
    """
    Pontos de skill para chegar a esse nivel, a contar do zero.

    E `totalCost` e nao a soma dos `cost`: o jogo ja da o acumulado, e somar os
    passos a mao e a maneira de introduzir um erro de um nivel.
    """
    for passo in escada or []:
        if passo.get("nivel") == nivel:
            return int(passo.get("custo_total") or 0)
    return 0


def fator_batalha(batalha) -> float:
    """
    Aceita o bonus de batalha nas duas formas em que ele aparece no painel.

    O cursor do simulador da um factor solto (x1,60). O separador de bonus da
    as componentes uma a uma — patriotico, alianca, ordens, QG — que e a forma
    util, porque diz de onde vem o numero. As duas chegam aqui e saem iguais,
    para nao haver duas contas do mesmo bonus a divergirem com o tempo.
    """
    if isinstance(batalha, dict):
        from . import bonus
        if "fator" in batalha:
            return float(batalha["fator"])
        return float(bonus.calcular(**batalha)["fator"])
    return max(0.0, float(batalha or 1.0))


def multiplicador(municao: str | None = None, pilula: bool = False,
                  posto_pct: float = 0.0, batalha=1.0) -> float:
    """
    Os bonus de ataque, todos multiplicativos entre si.

    O posto militar chega em percentagem porque e assim que o jogo o mostra;
    a batalha chega como factor ou como componentes (ver `fator_batalha`).

    `pilula=True` aqui significa "dentro da janela de buff" e nao "tomou hoje":
    a pilula tem 8 horas de +60% e 15,5 de -60%, e aplicar 1,60 a um dia
    inteiro sobrestima o dano quase para o dobro. Quem quer a conta do DIA usa
    `warera.pilula`, que distribui os golpes pelas horas.
    """
    m = 1.0
    if municao:
        m *= config.MULTIPLICADORES.get(municao, 1.0)
    if pilula:
        m *= config.MULTIPLICADORES["cocain"]
    m *= 1.0 + (posto_pct or 0.0) / 100.0
    m *= fator_batalha(batalha)
    return m


def simular(stats: dict[str, float], cfg: dict, municao: str | None = None,
            pilula: bool = False, posto_pct: float = 0.0,
            batalha: float = 1.0) -> dict:
    """
    O que esta build faz por golpe.

    O multiplicador entra no ataque ANTES do transbordo, e nao no fim: a
    precisao acima de 100 converte-se em ataque, e esse ataque tambem e
    multiplicado pela municao. Fazer ao contrario subestima builds de precisao
    alta.
    """
    # A CONTA VIVE NO `warera.motor` e so la. Esta funcao e a camada de
    # apresentacao que o painel ja conhecia: arredonda e renomeia campos. Havia
    # tres copias desta formula no projecto (aqui, no `pecas.py` e em
    # JavaScript) e as tres tinham de concordar sem nada que as obrigasse —
    # foi assim que a esquiva esteve certa num lado e errada nos outros.
    from . import motor, regras

    rs = regras.carregar(cfg)
    g = motor.golpe(stats, rs, municao=municao, pilula=pilula,
                    posto_pct=posto_pct, batalha=fator_batalha(batalha))
    vida_esperada = g["vida_por_golpe_esperada"]
    return {
        "multiplicador": round(g["multiplicador"], 4),
        "ataque_efetivo": round(g["ataque_efetivo"], 2),
        "precisao_efetiva": round(g["precisao_efetiva"], 2),
        "critico_efetivo": round(g["critico_efetivo"], 2),
        "dano_critico_efetivo": round(g["dano_critico_efetivo"], 2),
        "dano_por_golpe": round(g["dano_por_golpe"], 2),
        "armadura_pct": round(g["armadura_efetiva"] * 100, 2),
        "esquiva_pct": round(g["esquiva_efetiva"] * 100, 2),
        # o que custa um golpe que NAO foi esquivado, que e o numero que o jogo
        # mostra no ecra de batalha
        "vida_por_golpe": round(g["vida_por_golpe"], 3),
        # e o custo MEDIO, que e o que decide quantos golpes cabem no dia
        "vida_por_golpe_esperada": round(vida_esperada, 3),
        "golpes_por_barra": (round(float(stats.get("health") or 0) / vida_esperada, 1)
                             if vida_esperada > 0 and stats.get("health") else None),
        # o resultado cru, sem arredondar, para quem encadeia contas
        "_cru": g,
    }


def explicar(stats: dict[str, float], cfg: dict, municao: str | None = None,
             pilula: bool = False, posto_pct: float = 0.0, batalha=1.0) -> dict:
    """
    A conta toda, passo a passo — a do motor, que e a unica que ha.

    ISTO ERA UMA SEGUNDA DECOMPOSICAO e nao um atalho para a primeira. Tinha a
    formula do dano, o transbordo, a curva da defesa e os multiplicadores
    escritos outra vez, e por isso derivou: enquanto o motor ja dizia que um
    golpe esquivado nao custa vida nenhuma, esta continuava a escrever, com
    todas as letras, que a esquiva

        "nao baixa a vida gasta a bater — poupa a durabilidade do equipamento,
         que e dinheiro e nao golpes"

    que e exactamente o erro conceptual que o projecto ja tinha corrigido no
    motor. Duas explicacoes da mesma conta, e a errada era a que o separador de
    depuracao mostrava.

    Fica como uma porta para `motor.explicacao`, que devolve as quatro cadeias
    — ataque, dano, vida e golpes — e sabe a regra certa.
    """
    rs = regras.carregar(cfg)
    r = motor.avaliar(stats, rs, municao=municao, pilula=pilula,
                      posto_pct=posto_pct, batalha=fator_batalha(batalha),
                      explicar=True)
    return {
        **r["explicacao"],
        "formula": "E[dano] = ataque × [ p × ((1−c) + c × (1 + d/100)) + (1−p) × 0,5 ]",
        "resultado": simular(stats, cfg, municao=municao, pilula=pilula,
                             posto_pct=posto_pct, batalha=batalha),
        "metricas": r["metricas"],
    }


def dano_disponivel(stats: dict[str, float], cfg: dict, regen_dividido_por: float = 10.0,
                    custo_por_golpe: float = 0.0, comida_vida_pct: float | None = None,
                    **kw) -> dict:
    """
    Quanto dano da mesmo para fazer, e nao quanto dava se houvesse golpes
    infinitos.

    O que limita nao e o ataque: e a barra de vida. Ela regenera um decimo do
    maximo por hora, cada golpe custa `healthCost` reduzido pela armadura, e
    daqui sai o unico numero que interessa a quem planeia a semana — dano por
    dia, e o que custa em municao para o fazer.

    A armadura aparece aqui duas vezes e por isso e mais valiosa do que parece:
    reduz o dano que levas E baixa o custo de cada golpe, ou seja compra golpes.

    A COMIDA e a segunda fonte de golpes e faltava por completo nesta conta.
    Cada item repoe uma percentagem do maximo de vida e gasta 1 de fome; a
    barra de fome regenera aos mesmos 10% por hora. Ignora-la subestimava os
    golpes por dia em cerca de tres vezes — e, pior, escondia que para quem
    come a fila de espera deixa de ser a vida e passa a ser a FOME, o que muda
    qual e a skill que vale a pena subir a seguir.

    `comida_vida_pct` e a percentagem do maximo que o item repoe (10 pao, 15
    bife, 20 peixe). Fica a None por omissao para nao mudar em silencio os
    numeros de quem chama isto sem pensar em comida.
    """
    r = simular(stats, cfg, **kw)
    vida_max = float(stats.get("health") or 0)
    # o custo ESPERADO, com a esquiva ja descontada: e ela que decide quantos
    # golpes cabem, e nao o custo do golpe que se paga
    vida_golpe = r["vida_por_golpe_esperada"]
    if vida_max <= 0 or vida_golpe <= 0:
        return {**r, "golpes_hora": None, "golpes_dia": None, "dano_dia": None}

    divisor = float(regen_dividido_por or 10)
    regen_hora = vida_max / divisor
    golpes_regen_dia = regen_hora / vida_golpe * 24

    # a comida so entra se se souber quanto repoe E quanta fome ha para gastar
    fome_max = float(stats.get("hunger") or 0)
    golpes_comida_dia = 0.0
    comidas_dia = 0.0
    if comida_vida_pct and fome_max > 0:
        # a barra de fome regenera 10% do maximo por hora, como todas as outras
        comidas_dia = fome_max / divisor * 24
        vida_por_comida = vida_max * float(comida_vida_pct) / 100.0
        golpes_comida_dia = comidas_dia * vida_por_comida / vida_golpe

    golpes_dia = golpes_regen_dia + golpes_comida_dia
    golpes_hora = golpes_dia / 24
    return {
        **r,
        "vida_maxima": vida_max,
        "regen_hora": round(regen_hora, 2),
        "golpes_hora": round(golpes_hora, 2),
        "golpes_dia": round(golpes_dia, 1),
        "golpes_dia_regen": round(golpes_regen_dia, 1),
        "golpes_dia_comida": round(golpes_comida_dia, 1),
        "comidas_dia": round(comidas_dia, 1),
        # qual das duas barras e que esta a segurar o dia: e a que vale a pena
        # subir, e sem isto o painel recomendava vida a quem ja tinha vida a
        # mais e fome a menos
        "limitado_por": ("fome" if golpes_comida_dia > golpes_regen_dia else "vida"),
        "golpes_de_reserva": round(vida_max / vida_golpe, 1),
        "dano_hora": round(golpes_hora * r["dano_por_golpe"], 0),
        "dano_dia": round(golpes_dia * r["dano_por_golpe"], 0),
        "dano_semana": round(golpes_dia * 7 * r["dano_por_golpe"], 0),
        "custo_dia": round(golpes_dia * custo_por_golpe, 2) if custo_por_golpe else None,
        "dano_por_moeda": (round(r["dano_por_golpe"] / custo_por_golpe, 1)
                           if custo_por_golpe > 0 else None),
    }


def _escadas(skills: list[dict], nomes: list[str]) -> dict[str, list[dict]]:
    por = {s["nome"]: s for s in skills or []}
    return {n: (por[n]["escada"] if n in por else []) for n in nomes if n in por}


def _fronteira(escadas: dict[str, list[dict]], avaliar, orcamento: int,
               base: dict[str, int], fixos: dict[str, int] | None = None) -> list:
    """
    Para cada orcamento de 0 a N, a melhor combinacao de niveis e o seu valor.

    Enumera-se tudo — sao 11 niveis por skill e quatro skills no maior grupo,
    ou seja 14 641 combinacoes — porque uma heuristica gulosa erra aqui: subir
    dano critico so vale a pena depois de haver critico que chegue, e o guloso
    nao ve isso a tempo.

    O `base` e o nivel que o jogador ja tem: nenhuma skill desce abaixo dele,
    porque descer nao devolve pontos fora de um reset, e o custo conta so o que
    falta subir.

    Os `fixos` sao outra coisa e nao um `base` mais apertado. `base` e um CHAO
    — "nao descas daqui" — e `fixo` e uma ORDEM — "fica exactamente aqui". A
    diferenca aparece quando alguem quer perguntar "qual e a melhor build se eu
    NAO subir mais a vida", que e uma pergunta legitima e que nao se consegue
    fazer so com um chao: o optimizador subia a vida na mesma.
    """
    nomes = list(escadas)
    fixos = fixos or {}
    melhor = [None] * (orcamento + 1)

    def andar(i, niveis, custo):
        if custo > orcamento:
            return
        if i == len(nomes):
            v = avaliar(niveis)
            if melhor[custo] is None or v > melhor[custo][0]:
                melhor[custo] = (v, dict(niveis))
            return
        nome = nomes[i]
        de = int(base.get(nome) or 0)
        base_custo = custo_ate(escadas[nome], de)
        # um nivel fixo abaixo do que o jogador ja tem nao e uma ordem que se
        # possa cumprir: descer nao devolve pontos, por isso vale o chao
        preso = fixos.get(nome)
        preso = max(int(preso), de) if preso is not None else None
        for passo in escadas[nome]:
            if passo["nivel"] < de:
                continue
            if preso is not None and passo["nivel"] != preso:
                continue
            niveis[nome] = passo["nivel"]
            andar(i + 1, niveis, custo + int(passo.get("custo_total") or 0) - base_custo)
        niveis[nome] = de

    andar(0, {}, 0)
    # a fronteira tem de ser monotona: com mais pontos nunca se fica pior,
    # porque se pode simplesmente nao os gastar
    for c in range(1, orcamento + 1):
        if melhor[c] is None or (melhor[c - 1] and melhor[c - 1][0] > melhor[c][0]):
            melhor[c] = melhor[c - 1]
    return melhor


DANO = ["attack", "precision", "criticalChance", "criticalDamages"]
# A ESQUIVA ENTRA AQUI, e durante muito tempo nao entrava. Um golpe esquivado e
# "a free hit" — nao gasta vida — por isso a esquiva compra golpes exactamente
# como a armadura, e ainda poupa a durabilidade das cinco pecas. A ideia de que
# "as botas nunca entram no plano" vinha de se julgar que a esquiva evitava
# dano recebido; nao ha dano recebido, a vida e o custo de atacar.
AGUENTA = ["armor", "dodge", "health"]
# a fome so entra no grupo que compra golpes quando ha comida modelada: sem
# comida ela nao faz nada e o optimizador gastava pontos numa skill inerte
AGUENTA_COM_COMIDA = ["armor", "dodge", "health", "hunger"]
# as tres skills que decidem dinheiro por dia: quanto rende cada trabalho
# (`production`), e quantos trabalhos cabem em cada um dos dois depositos que
# os pagam (`entrepreneurship` para a empresa propria, `energy` para o
# salario). `companies`, `management` e `lootChance` ficam de fora: o jogo nao
# publica a formula do efeito deles em lado nenhum do gameConfig, e inventar
# um numero para uma skill e o oposto do que este painel faz em todo o lado.
ECONOMIA_MENSURAVEL = ["production", "entrepreneurship", "energy"]


def fronteira_dano(orcamento: int, skills: list[dict], cfg: dict,
                   equipamento: dict[str, float] | None = None,
                   objetivo: str = "dia", regen_dividido_por: float = 10.0,
                   base_niveis: dict[str, int] | None = None,
                   comida_vida_pct: float | None = None,
                   fixos: dict[str, int] | None = None, **kw) -> list:
    """
    Para cada orcamento de 0 a N gasto em combate, o melhor dano e os niveis
    que lá chegam. `melhor_build` so le o ultimo ponto desta lista; existe
    separada para `repartir_pontos` poder cruzá-la com a fronteira da economia
    sem a recalcular a cada corte — o que a tornava O(orcamento²).

    Devolve `None` nos indices sem tabela de skills (erro), ou uma lista de
    `(valor, niveis)` — o mesmo formato interno de `_fronteira`.
    """
    eq = equipamento or {}
    base = {k: int(v) for k, v in (base_niveis or {}).items()}
    grupo_aguenta = AGUENTA_COM_COMIDA if comida_vida_pct else AGUENTA
    escadas = _escadas(skills, DANO + grupo_aguenta)
    if not escadas:
        return None

    def stats_de(niveis):
        st = {}
        for nome, escada in escadas.items():
            n = niveis.get(nome, base.get(nome, 0))
            st[nome] = valor_no_nivel(escada, n) + float(eq.get(nome) or 0)
        for nome, v in eq.items():
            st.setdefault(nome, float(v))
        return st

    e_dano = {n: escadas[n] for n in DANO if n in escadas}
    fr_dano = _fronteira(e_dano, lambda nv: simular(stats_de(nv), cfg, **kw)["dano_por_golpe"],
                         orcamento, base, fixos)

    if objetivo == "golpe":
        # o que nao se optimiza fica onde esta, e nao a zero: senao a
        # comparacao entre os dois objetivos punha um deles sem vida nenhuma
        return [((fr_dano[c] or (0.0, {}))[0], {**base, **(fr_dano[c] or (0.0, {}))[1]})
                for c in range(orcamento + 1)]

    e_aguenta = {n: escadas[n] for n in grupo_aguenta if n in escadas}

    # AS REGRAS SAO AS DO MOTOR, TAMBEM AQUI.
    #
    # Isto tinha a curva da defesa, o custo do golpe e a conta da comida
    # escritas a mao, com o `config.COMBATE` em vez do RULESET. Era a terceira
    # copia da mesma regra — a que sobrou depois de o JavaScript ter ido
    # embora — e a mais perigosa das tres, porque e a que decide onde vao os
    # pontos de toda a gente. Se o jogo mudar o `battle.healthCost`, o motor ve
    # e esta nao via.
    v_motor = regras.valores(regras.carregar(cfg))

    def golpes_dia(nv):
        st = stats_de(nv)
        vmax = float(st.get("health") or 0)
        vida = motor.custo_esperado(st.get("armor"), st.get("dodge"), v_motor)
        if vida <= 0 or vmax <= 0:
            return 0.0
        divisor = float(regen_dividido_por or 10)
        golpes = vmax / divisor / vida * 24
        if comida_vida_pct:
            # a fome regenera ao mesmo ritmo e cada item repoe uma % do maximo
            fome = float(st.get("hunger") or 0)
            golpes += (fome / divisor * 24) * (vmax * float(comida_vida_pct) / 100.0) / vida
        return golpes

    fr_aguenta = _fronteira(e_aguenta, golpes_dia, orcamento, base, fixos)

    fora = []
    for orc in range(orcamento + 1):
        melhor, escolha = -1.0, {}
        for corte in range(orc + 1):
            a, d = fr_dano[corte], fr_aguenta[orc - corte]
            if not a or not d:
                continue
            total = a[0] * d[0]
            if total > melhor:
                melhor, escolha = total, {**a[1], **d[1]}
        fora.append((melhor if melhor >= 0 else 0.0, {**base, **escolha}))
    return fora


def melhor_build(orcamento: int, skills: list[dict], cfg: dict,
                 equipamento: dict[str, float] | None = None,
                 objetivo: str = "dia", regen_dividido_por: float = 10.0,
                 base_niveis: dict[str, int] | None = None,
                 custo_por_golpe: float = 0.0,
                 comida_vida_pct: float | None = None,
                 fixos: dict[str, int] | None = None, **kw) -> dict:
    """
    A melhor distribuicao de pontos para o orcamento que ha.

    Dois objetivos, e nao sao o mesmo:

      golpe  o maximo por golpe. So conta ataque, precisao, critico e dano
             critico.
      dia    o maximo por dia. Aqui a armadura e a vida tambem contam, porque
             compram golpes: mais vida da mais barra, e mais armadura faz cada
             golpe custar menos. Uma build so de ataque perde para uma com
             armadura muito antes do que parece.

    O equipamento entra como uma constante somada a cada stat e nao se optimiza
    aqui: isto responde "com o que ja tens vestido, onde poes os pontos".

    Resolve-se em dois grupos separados porque o dano por dia e o produto de
    duas contas independentes — dano por golpe e golpes por dia. Para cada
    reparticao do orcamento entre os dois grupos, o melhor de cada lado ja esta
    calculado, e escolhe-se a melhor reparticao. Da o optimo exacto.
    """
    base = {k: int(v) for k, v in (base_niveis or {}).items()}
    fr = fronteira_dano(orcamento, skills, cfg, equipamento, objetivo,
                        regen_dividido_por, base_niveis, comida_vida_pct,
                        fixos, **kw)
    if fr is None:
        return {"erro": "sem tabela de skills"}
    escadas = _escadas(skills, DANO + (AGUENTA_COM_COMIDA if comida_vida_pct else AGUENTA))
    eq = equipamento or {}

    def stats_de(niveis):
        st = {}
        for nome, escada in escadas.items():
            n = niveis.get(nome, base.get(nome, 0))
            st[nome] = valor_no_nivel(escada, n) + float(eq.get(nome) or 0)
        for nome, v in eq.items():
            st.setdefault(nome, float(v))
        return st

    _, niveis = fr[orcamento]
    completos = {**{n: base.get(n, 0) for n in escadas}, **niveis}
    r = _resultado(completos, stats_de(completos), cfg, skills, orcamento,
                   regen_dividido_por, base, custo_por_golpe,
                   comida_vida_pct=comida_vida_pct, **kw)
    if fixos:
        # dizer o que ficou preso e parte da resposta: sem isto uma build com
        # skills fixas parece pior do que o optimizador consegue, e nao ha
        # maneira de saber que foi por ordem de quem perguntou
        r["fixos"] = {n: v for n, v in fixos.items() if n in escadas}
    return r


def fronteira_economia(orcamento: int, skills: list[dict],
                       base_niveis: dict[str, int] | None = None,
                       empresas: int = 0, motor_pp_hora: float | None = None,
                       lucro_pp_propria: float = 0.0,
                       salario_liquido: float = 0.0) -> list | None:
    """
    Para cada orcamento de 0 a N gasto nas tres skills economicas, o dinheiro
    por dia que isso da — mesma forma que `fronteira_dano`, para se poder
    cruzar as duas em `repartir_pontos`.

    `production` decide quanto rende CADA trabalho; `entrepreneurship` e
    `energy` decidem quantos trabalhos cabem por dia em cada um dos dois
    depositos que os pagam — a propria empresa e o salario de outrem. O motor
    automatico nao gasta pontos nenhuns (e das construcoes, nao das skills),
    mas soma-se aqui porque tambem sai em PP por dia da mesma empresa.

    `lucro_pp_propria` e `salario_liquido` sao precos de fora (o melhor item a
    30 dias, o salario mediano) e nao parte do que se optimiza: dado o preco
    de mercado de agora, isto so decide quantos PP comprar com os pontos.
    """
    from . import producao as _producao

    base = {k: int(v) for k, v in (base_niveis or {}).items()}
    escadas = _escadas(skills, ECONOMIA_MENSURAVEL)
    if not escadas:
        return None

    def renda(niveis):
        pp = valor_no_nivel(escadas.get("production", []),
                            niveis.get("production", base.get("production", 0)))
        emp = valor_no_nivel(escadas.get("entrepreneurship", []),
                             niveis.get("entrepreneurship", base.get("entrepreneurship", 0)))
        ene = valor_no_nivel(escadas.get("energy", []),
                             niveis.get("energy", base.get("energy", 0)))
        cap = _producao.capacidade(producao=pp, empreendedorismo=emp, energia=ene,
                                   empresas=empresas, motor_pp_hora=motor_pp_hora)
        return ((cap["pp_dia_proprio"] + cap["pp_dia_motor"]) * lucro_pp_propria
                + cap["pp_dia_salario"] * salario_liquido)

    fr = _fronteira(escadas, renda, orcamento, base)
    return [((fr[c] or (0.0, {}))[0], {**base, **(fr[c] or (0.0, {}))[1]})
            for c in range(orcamento + 1)]


def repartir_pontos(pontos_totais: int, skills: list[dict], cfg: dict,
                    equipamento: dict[str, float] | None = None,
                    base_niveis: dict[str, int] | None = None,
                    empresas: int = 0, motor_pp_hora: float | None = None,
                    lucro_pp_propria: float = 0.0, salario_liquido: float = 0.0,
                    regen_dividido_por: float = 10.0, **kw) -> dict:
    """
    Combate ou economia — a mesma pergunta que `_rankings` ja fazia com vias
    inteiras, agora dentro dos pontos de UM jogador.

    As duas fronteiras sao cada uma delas monotona (mais pontos nunca piora),
    por isso varrer o corte de 0 a N e ja da a curva de troca inteira, sem
    filtro de dominancia nenhum: mover um ponto de economia para combate so
    pode subir o dano e so pode descer (ou manter) a renda, nunca as duas.

    Nao ha um numero so no fim porque dano por dia e dinheiro por dia sao
    unidades diferentes — a mesma regra que separa `ranking_tempo` de
    `ranking_capital` no resto do painel.
    """
    fr_combate = fronteira_dano(pontos_totais, skills, cfg, equipamento, "dia",
                                regen_dividido_por, base_niveis, **kw)
    fr_economia = fronteira_economia(pontos_totais, skills, base_niveis, empresas,
                                     motor_pp_hora, lucro_pp_propria, salario_liquido)
    if fr_combate is None or fr_economia is None:
        return {"erro": "sem tabela de skills"}

    curva = []
    for pe in range(pontos_totais + 1):
        pc = pontos_totais - pe
        dano_dia, niveis_c = fr_combate[pc]
        renda_dia, niveis_e = fr_economia[pe]
        curva.append({
            "pontos_combate": pc, "pontos_economia": pe,
            "dano_dia": round(dano_dia, 0), "renda_dia": round(renda_dia, 2),
            "niveis": {**niveis_c, **niveis_e},
        })
    return {"pontos_totais": pontos_totais, "curva": curva}


def _resultado(niveis, stats, cfg, skills, orcamento, regen, base,
               custo_por_golpe=0.0, comida_vida_pct=None, **kw) -> dict:
    por = {s["nome"]: s for s in skills or []}
    gasto = sum(max(0, custo_ate(por[n]["escada"], v)
                    - custo_ate(por[n]["escada"], int((base or {}).get(n) or 0)))
                for n, v in niveis.items() if n in por)
    return {
        "niveis": niveis,
        # o que muda mesmo: dizer "sobe ataque para 7" e util, repetir os
        # niveis que ja la estavam nao e
        "sobe": {n: v for n, v in niveis.items() if v > int((base or {}).get(n) or 0)},
        "stats": {k: round(v, 2) for k, v in stats.items()},
        "pontos_gastos": gasto,
        "pontos_sobra": orcamento - gasto,
        **dano_disponivel(stats, cfg, regen_dividido_por=regen,
                          custo_por_golpe=custo_por_golpe,
                          comida_vida_pct=comida_vida_pct, **kw),
    }


# ---------------------------------------------------------------------------
# Equipamento: o outro lado da build, que ate aqui ficava de fora
#
# Cada slot do jogo da exactamente um stat, menos a arma que da dois. E isso
# que torna isto resoluvel: o dano depende de arma+luvas+capacete, e os golpes
# por dia dependem de peito+calcas+botas. Sao dois grupos que nao se tocam, e
# so partilham o dinheiro.
# ---------------------------------------------------------------------------

SLOTS_DANO = ["weapon", "gloves", "helmet"]
SLOTS_DEFESA = ["chest", "pants", "boots"]
STATS_DANO = ("attack", "precision", "criticalChance", "criticalDamages")
STATS_DEFESA = ("armor", "dodge")


def opcoes_de_slot(itens: dict, precos: dict[str, float]) -> dict[str, list[dict]]:
    """
    O que se pode vestir em cada slot, com o roll MEDIO e o preco de mercado.

    O roll medio e nao o maximo: o maximo e o que se ve num anuncio, a media e
    o que sai quando se compra. Sem preco no mercado a peca nao entra — nao se
    recomenda comprar o que nao esta a venda.
    """
    from . import pecas

    fora: dict[str, list[dict]] = {}
    for code, cfg_item in (itens or {}).items():
        tipo = cfg_item.get("type")
        if tipo not in ("equipment", "weapon"):
            continue
        faixas = pecas.intervalos(code, itens)
        if not faixas:
            continue
        slot = "weapon" if tipo == "weapon" else "".join(
            ch for ch in code if not ch.isdigit())
        preco = (precos or {}).get(code)
        if preco is None:
            continue
        fora.setdefault(slot, []).append({
            "codigo": code,
            "slot": slot,
            "custo": float(preco),
            "raridade": cfg_item.get("rarity"),
            "stats": {s: (lo + hi) / 2 for s, (lo, hi) in faixas.items()},
        })
    for slot in fora:
        fora[slot].sort(key=lambda x: x["custo"])
    return fora


def _dominado(a: dict, b: dict, stats) -> bool:
    """`a` e dominado por `b`: custa o mesmo ou mais e nao da mais nada."""
    if b["custo"] > a["custo"]:
        return False
    return (all(b["stats"].get(s, 0) >= a["stats"].get(s, 0) for s in stats)
            and (b["custo"] < a["custo"]
                 or any(b["stats"].get(s, 0) > a["stats"].get(s, 0) for s in stats)))


def combinacoes(opcoes: dict[str, list[dict]], slots: list[str], stats) -> list[dict]:
    """
    Todas as maneiras de vestir estes slots, sem as que nao valem a pena.

    Sao 6 raridades por slot, logo algumas centenas de combinacoes — mas a
    maioria e dominada por outra que custa o mesmo e da mais. Tirar as
    dominadas nao muda a resposta e faz caber a conta num piscar de olhos.
    """
    conjuntos = [{"custo": 0.0, "stats": {}, "pecas": {}}]
    for slot in slots:
        novos = []
        # "nada neste slot" tambem e uma opcao: pode ser a certa com pouco dinheiro
        for opcao in [None] + (opcoes.get(slot) or []):
            for c in conjuntos:
                st = dict(c["stats"])
                if opcao:
                    for s, v in opcao["stats"].items():
                        st[s] = st.get(s, 0.0) + v
                novos.append({
                    "custo": c["custo"] + (opcao["custo"] if opcao else 0.0),
                    "stats": st,
                    "pecas": {**c["pecas"], **({slot: opcao["codigo"]} if opcao else {})},
                })
        conjuntos = novos

    conjuntos.sort(key=lambda x: x["custo"])
    fronteira: list[dict] = []
    for c in conjuntos:
        if not any(_dominado(c, f, stats) for f in fronteira):
            fronteira.append(c)
    return fronteira


def melhor_equipamento(orcamento: float, itens: dict, precos: dict,
                       stats_base: dict[str, float], cfg: dict,
                       regen_dividido_por: float = 10.0,
                       custo_por_golpe: float = 0.0,
                       objetivo: str = "dia",
                       dias: float | None = None,
                       custos_scrap: dict | None = None,
                       preco_scrap: float | None = None, **kw) -> dict:
    """
    Com este dinheiro e com as skills que ja tens, o que e que se veste.

    E a outra metade da pergunta do simulador. `melhor_build` responde "onde
    ponho os pontos com o equipamento que tenho"; isto responde ao contrario:
    *com estes pontos ja gastos, qual e o melhor equipamento que este dinheiro
    compra*. Sao 7 opcoes por slot — as seis raridades mais nao vestir nada — e
    seis slots, mas os dois grupos nao se tocam:

        arma + luvas + capacete     decidem o dano por golpe
        peito + calcas              decidem quantos golpes cabem no dia

    Por isso sao 343 + 343 combinacoes em vez de 117 mil, e enumera-se tudo em
    vez de adivinhar. As combinacoes dominadas (que custam o mesmo ou mais e
    nao dao mais nada) ja saem na fronteira, e a lista vem ordenada pelo custo,
    o que faz com que um empate fique sempre com a mais barata.

    As botas nunca ganhavam nada aqui, e isso era simplesmente um erro. Um
    golpe esquivado e "a free hit": nao gasta vida. Por isso a esquiva compra
    golpes como a armadura compra, E ainda poupa a durabilidade das outras
    cinco pecas. Eram duas razoes para as botas contarem e o painel nao tinha
    nenhuma delas.

    `dias` e o que muda a natureza da resposta. Sem ele, uma peca e uma compra
    unica e a mais cara ganha quase sempre. Com ele, uma peca e um CONSUMIVEL —
    gasta-se 1% por golpe, cem golpes destroem-na — e o custo verdadeiro de
    vestir mythic durante uma semana e varias vezes o preco da etiqueta. E a
    diferenca entre uma build que se compra e uma que se sustenta.
    """
    from . import durabilidade

    op = opcoes_de_slot(itens, precos)
    if not op:
        return {"erro": "sem precos de equipamento"}

    g_dano = combinacoes(op, SLOTS_DANO, STATS_DANO)
    g_defesa = combinacoes(op, SLOTS_DEFESA, STATS_DEFESA)
    melhor = None
    for a in g_dano:
        if a["custo"] > orcamento + 1e-9:
            continue
        for b in g_defesa:
            custo = a["custo"] + b["custo"]
            if custo > orcamento + 1e-9:
                continue
            st = dict(stats_base)
            for grupo in (a["stats"], b["stats"]):
                for s, v in grupo.items():
                    st[s] = float(st.get(s) or 0) + v
            r = dano_disponivel(st, cfg, regen_dividido_por=regen_dividido_por,
                                custo_por_golpe=custo_por_golpe, **kw)
            vestidas = {**a["pecas"], **b["pecas"]}

            # com um horizonte, a peca deixa de ser uma compra e passa a ser um
            # consumivel: paga-se a reposicao dos golpes que se derem nesses
            # dias, e o objectivo passa a ser dano por moeda GASTA e nao dano
            # por moeda investida uma vez
            desgaste = 0.0
            if dias:
                golpes = (r.get("golpes_dia") or 0.0) * float(dias)
                dg = durabilidade.custo_por_golpe(
                    vestidas, precos, custos_scrap, preco_scrap,
                    esquiva_efetiva=(r.get("esquiva_pct") or 0) / 100.0)
                desgaste = dg["total"] * golpes

            if objetivo == "golpe":
                valor = r["dano_por_golpe"]
            elif dias:
                total_gasto = custo + desgaste + (custo_por_golpe
                                                  * (r.get("golpes_dia") or 0)
                                                  * float(dias))
                valor = (((r.get("dano_dia") or 0.0) * float(dias)) / total_gasto
                         if total_gasto > 0 else 0.0)
            else:
                valor = r.get("dano_dia") or 0.0

            if melhor is None or valor > melhor[0]:
                melhor = (valor, custo, vestidas, st, r, desgaste)
    if melhor is None:
        return {"erro": "nem a peça mais barata cabe neste orçamento"}
    _, custo, pecas_escolhidas, st, r, desgaste = melhor
    fora = {
        "pecas": pecas_escolhidas,
        "custo": round(custo, 2),
        "sobra": round(orcamento - custo, 2),
        "stats": {k: round(v, 2) for k, v in st.items()},
        "combos_dano": len(g_dano),
        "combos_defesa": len(g_defesa),
        **r,
    }
    if dias:
        golpes = (r.get("golpes_dia") or 0.0) * float(dias)
        dano = (r.get("dano_dia") or 0.0) * float(dias)
        consumo = custo_por_golpe * golpes
        total = custo + desgaste + consumo
        fora.update({
            "dias": dias,
            "custo_desgaste": round(desgaste, 2),
            "custo_consumiveis": round(consumo, 2),
            "custo_total_horizonte": round(total, 2),
            "dano_horizonte": round(dano, 0),
            "custo_por_1k_dano": round(total / (dano / 1000.0), 4) if dano else None,
            "reposicao": durabilidade.plano_de_reposicao(
                pecas_escolhidas, precos, r.get("golpes_dia") or 0.0, dias,
                custos_scrap, preco_scrap,
                (r.get("esquiva_pct") or 0) / 100.0),
        })
    return fora


def melhor_build_completa(pontos: int, dinheiro: float, skills: list[dict],
                          cfg: dict, itens: dict, precos: dict,
                          base_niveis: dict[str, int] | None = None,
                          equipamento_atual: dict[str, float] | None = None,
                          regen_dividido_por: float = 10.0,
                          custo_por_golpe: float = 0.0,
                          passos: int = 24, **kw) -> dict:
    """
    A melhor build com DOIS orcamentos: pontos de skill e dinheiro.

    Ate aqui o painel optimizava so os pontos e deixava o equipamento como
    estava. Isso responde "onde ponho os pontos", mas nao responde a pergunta
    que se faz com dinheiro na mao: *com isto tudo, qual e a melhor build?*

    Resolve-se assim, e e exacto dentro dos passos do orcamento:

      1. os slots dividem-se em dois grupos que nao se tocam — arma, luvas e
         capacete decidem o dano por golpe; peito, calcas e botas decidem
         quantos golpes cabem no dia;
      2. de cada grupo tira-se a fronteira: as combinacoes que nao sao
         dominadas por outra mais barata e igual ou melhor;
      3. varre-se a reparticao do dinheiro e dos pontos entre os dois grupos.

    Devolve tambem a fronteira inteira de dano contra dinheiro, que e o que
    permite mexer num cursor e ver a build mudar em vez de ver um numero so.
    """
    op = opcoes_de_slot(itens, precos)
    if not op:
        return {"erro": "sem precos de equipamento"}

    base = {k: int(v) for k, v in (base_niveis or {}).items()}
    eq_extra = equipamento_atual or {}
    escadas = _escadas(skills, DANO + AGUENTA)
    e_dano = {n: escadas[n] for n in DANO if n in escadas}
    e_aguenta = {n: escadas[n] for n in AGUENTA if n in escadas}

    g_dano = combinacoes(op, SLOTS_DANO, STATS_DANO)
    g_defesa = combinacoes(op, SLOTS_DEFESA, STATS_DEFESA)

    def stats_skill(escadas_grupo, niveis):
        return {n: valor_no_nivel(escadas_grupo[n], niveis.get(n, base.get(n, 0)))
                for n in escadas_grupo}

    # fronteira de skills de dano, por orcamento de pontos
    fr_ds = _fronteira(e_dano, lambda nv: sum(valor_no_nivel(e_dano[n], nv[n]) for n in e_dano),
                       pontos, base)
    fr_as = _fronteira(e_aguenta,
                       lambda nv: sum(valor_no_nivel(e_aguenta[n], nv[n]) for n in e_aguenta),
                       pontos, base)

    # Os dois objectivos sao escalares — dano por golpe de um lado, golpes por
    # dia do outro — e o dano por dia e o produto dos dois. Por isso tabela-se
    # cada lado por (dinheiro, pontos) e so depois se varre a reparticao. A
    # conta directa eram 130 milhoes de iteracoes; assim sao algumas centenas
    # de milhar.
    passo_e = max(0.01, dinheiro / max(1, passos))
    passo_p = max(1, (pontos // 20) or 1)
    grelha_p = sorted({0, pontos, *range(0, pontos + 1, passo_p)})

    def tabela(grupo_gear, escadas_grupo, fr, avaliar):
        """[(orcamento, pontos)] -> (valor, gear, niveis), o melhor de cada canto."""
        t = {}
        for i in range(passos + 1):
            teto = min(dinheiro, i * passo_e)
            elegiveis = [g for g in grupo_gear if g["custo"] <= teto + 1e-9]
            for p in grelha_p:
                niveis = (fr[p] or (0, {}))[1]
                s_sk = stats_skill(escadas_grupo, niveis)
                melhor_local = None
                for g in elegiveis:
                    v = avaliar(s_sk, g["stats"])
                    if melhor_local is None or v > melhor_local[0]:
                        melhor_local = (v, g, niveis)
                if melhor_local:
                    t[(i, p)] = melhor_local
        return t

    def val_dano(s_sk, s_gear):
        st = {s: (s_sk.get(s, 0.0) + s_gear.get(s, 0.0) + float(eq_extra.get(s) or 0))
              for s in STATS_DANO}
        return simular(st, cfg, **kw)["dano_por_golpe"]

    def val_golpes(s_sk, s_gear):
        armadura = (s_sk.get("armor", 0.0) + s_gear.get("armor", 0.0)
                    + float(eq_extra.get("armor") or 0))
        esquiva = (s_sk.get("dodge", 0.0) + s_gear.get("dodge", 0.0)
                   + float(eq_extra.get("dodge") or 0))
        vida = s_sk.get("health", 0.0) + float(eq_extra.get("health") or 0)
        # a mesma conta do motor, e nao uma quarta copia dela
        por_golpe = motor.custo_esperado(armadura, esquiva,
                                         regras.valores(regras.carregar(cfg)))
        return (vida / float(regen_dividido_por or 10) / por_golpe * 24) if por_golpe > 0 else 0.0

    t_dano = tabela(g_dano, e_dano, fr_ds, val_dano)
    t_def = tabela(g_defesa, e_aguenta, fr_as, val_golpes)

    fronteira, melhor = [], None
    for i in range(passos + 1):
        local = None
        for a in range(i + 1):
            for pd in grelha_p:
                pa = pontos - pd
                pa = min(grelha_p, key=lambda x: abs(x - pa))
                ld, lf = t_dano.get((a, pd)), t_def.get((i - a, pa))
                if not ld or not lf:
                    continue
                total = ld[0] * lf[0]
                if local is None or total > local[0]:
                    local = (total, a, ld, lf, pd)
        if not local:
            continue
        _, _, ld, lf, pd = local
        st = {}
        for s in STATS_DANO:
            st[s] = (stats_skill(e_dano, ld[2]).get(s, 0.0) + ld[1]["stats"].get(s, 0.0)
                     + float(eq_extra.get(s) or 0))
        s_ag = stats_skill(e_aguenta, lf[2])
        for s in STATS_DEFESA:
            st[s] = s_ag.get(s, 0.0) + lf[1]["stats"].get(s, 0.0) + float(eq_extra.get(s) or 0)
        st["health"] = s_ag.get("health", 0.0) + float(eq_extra.get("health") or 0)
        r = dano_disponivel(st, cfg, regen_dividido_por=regen_dividido_por,
                            custo_por_golpe=custo_por_golpe, **kw)
        linha = {
            "orcamento": round(min(dinheiro, i * passo_e), 2),
            "custo_equipamento": round(ld[1]["custo"] + lf[1]["custo"], 2),
            "pecas": {**ld[1]["pecas"], **lf[1]["pecas"]},
            "niveis": {**ld[2], **lf[2]},
            "pontos_no_dano": pd,
            "stats": {k: round(v, 2) for k, v in st.items()},
            "dano_dia_valor": r.get("dano_dia") or 0.0,
            **r,
        }
        fronteira.append(linha)
        if melhor is None or linha["dano_dia_valor"] > melhor["dano_dia_valor"]:
            melhor = linha
    return {"melhor": melhor, "fronteira": fronteira,
            "combos_dano": len(g_dano), "combos_defesa": len(g_defesa),
            "passos": passos}


def custo_do_plano(alvo: dict[str, int], atual: dict[str, int],
                   skills: list[dict]) -> dict:
    """
    Quantos pontos de skill custa passar da build de agora para a do alvo.

    Descer uma skill nao devolve pontos fora de um reset, por isso um alvo mais
    baixo do que o actual conta zero e nao um credito — dizer o contrario
    inventava pontos que o jogador nao tem.
    """
    por_skill = {s["nome"]: s for s in skills or []}
    linhas, total = [], 0
    for nome, nivel in (alvo or {}).items():
        s = por_skill.get(nome)
        if not s:
            continue
        de = int((atual or {}).get(nome) or 0)
        para = int(nivel)
        custo = max(0, custo_ate(s["escada"], para) - custo_ate(s["escada"], de))
        total += custo
        linhas.append({
            "nome": nome,
            "de": de, "para": para,
            "valor_de": valor_no_nivel(s["escada"], de),
            "valor_para": valor_no_nivel(s["escada"], para),
            "custo": custo,
            "desce": para < de,
        })
    linhas.sort(key=lambda x: -x["custo"])
    return {"linhas": linhas, "pontos": total}
