"""
Cinco objectivos de build, e o que fazer a seguir.

Nao sao cinco algoritmos: sao cinco **funcoes objectivo** sobre a mesma procura.
O optimizador exacto ja existe (`simulador.melhor_build`, que enumera as 14 641
combinacoes em vez de adivinhar) — o que faltava era dizer-lhe o que maximizar,
e perceber que a resposta muda conforme a pergunta:

  burst        o maximo por golpe. Serve para um tick de batalha que nao se
               pode perder, e quase nunca serve para mais nada.
  sustentado   o maximo por dia. Conta a armadura e a vida, que compram golpes.
  equilibrado  o maximo por dia que ainda se paga a um preco decente — o joelho
               da curva, e nao um ponto escolhido a olho.
  economico    o maximo de dano por moeda gasta. Nao e a mesma coisa que o
               anterior e costuma dar uma build mais pobre e mais rentavel.
  guerra       o maximo por dia com os multiplicadores de guerra ligados.

Uma coisa que este modulo NAO faz e decidir a municao por regra fixa. "Usa
sempre heavy ammo" e um conselho que so esta certo a alguns precos: a heavy
custa 16x a light e da x1,40 contra x1,10, por isso ha um preco a partir do
qual deixa de compensar. Aqui as quatro municoes e as duas hipoteses de pilula
sao ENUMERADAS aos precos de hoje e ganha a que ganhar. Se amanha o chumbo
subir, a resposta muda sozinha.
"""

from __future__ import annotations

from . import bonus, pilula, simulador

# "sem municao" NAO e uma opcao para quem usa arma de fogo: o jogo bloqueia o
# golpe desde a v0.15.14-beta ("Prevent hitting without ammo when using guns").
# So a faca dispensa municao — e a faca tambem nao a pode usar. Deixar o None
# na lista fazia o optimizador economico recomendar uma build injogavel, que e
# pior do que nao recomendar nada: o dano por moeda era o melhor de todos
# porque o denominador ia a zero.
MUNICOES = ["lightAmmo", "ammo", "heavyAmmo"]
MUNICOES_COM_FACA = [None]

OBJETIVOS = {
    "burst": {
        "nome": "Máximo por golpe",
        "criterio": "dano_por_golpe",
        "o_que": "o maior número possível num só golpe",
        "quando": "um tick de batalha que não se pode perder",
    },
    "sustentado": {
        "nome": "Máximo por dia",
        "criterio": "dano_dia",
        "o_que": "o maior dano total que a barra de vida aguenta",
        "quando": "rankings semanais e esforço de guerra prolongado",
    },
    "equilibrado": {
        "nome": "Equilibrado",
        "criterio": "joelho",
        "o_que": "o máximo por dia que ainda se paga a um preço decente",
        "quando": "o dia-a-dia de quem não quer queimar a carteira",
    },
    "economico": {
        "nome": "Económico",
        "criterio": "dano_por_moeda",
        "o_que": "o maior dano por moeda gasta",
        "quando": "quando o objectivo é acumular e não subir no ranking",
    },
    "guerra": {
        "nome": "Guerra",
        "criterio": "dano_dia",
        "o_que": "o máximo por dia com os bónus de guerra ligados",
        "quando": "só enquanto os bónus estiverem mesmo activos",
    },
    "custom": {
        "nome": "À medida",
        "criterio": "pesos",
        "o_que": "a mistura de critérios que tu escolheres, com os teus pesos",
        "quando": "quando nenhum dos cinco anteriores é a tua pergunta",
    },
}

# ---------------------------------------------------------------------------
#  O objectivo A MEDIDA
#
#  Os cinco objectivos acima sao cinco perguntas fixas, e ha perguntas que nao
#  sao nenhuma delas — "o maximo por dia mas sem passar de X por dia de custo",
#  "metade burst metade sustentado". Em vez de acrescentar um sexto caso fixo,
#  isto deixa escrever a funcao objectivo: um peso por criterio.
#
#  Os criterios estao em unidades incomparaveis (dano sao dezenas de milhar,
#  dano por moeda sao dezenas), por isso NAO se somam em cru — normaliza-se
#  cada um para [0,1] dentro do conjunto de candidatos e so depois se pesa. Sem
#  isso, um peso de 0,01 no dano esmagava um peso de 1 no custo e os pesos
#  seriam decorativos.
# ---------------------------------------------------------------------------
CRITERIOS = {
    "dano_por_golpe": {"sentido": +1, "o_que": "o maior número num só golpe"},
    "dano_dia": {"sentido": +1, "o_que": "o dano total do dia"},
    "golpes_dia": {"sentido": +1, "o_que": "quantos golpes o dia aguenta"},
    "dano_por_moeda": {"sentido": +1, "o_que": "dano por moeda gasta"},
    "custo_dia": {"sentido": -1, "o_que": "o que se gasta por dia (menos é melhor)"},
    "custo_por_1k_dano": {"sentido": -1, "o_que": "custo por 1000 de dano (menos é melhor)"},
}

PESOS_PADRAO = {"dano_dia": 1.0}


def normalizar_pesos(pesos: dict | None) -> dict[str, float]:
    """
    Os pesos limpos: so criterios conhecidos, so numeros, e a somar 1.

    Normalizar a soma faz com que {dano_dia: 2, custo_dia: 1} e
    {dano_dia: 200, custo_dia: 100} deem a MESMA build — que e o que qualquer
    pessoa espera de pesos e o que nao acontecia se se somassem em cru.
    """
    limpos = {k: abs(float(v)) for k, v in (pesos or {}).items()
              if k in CRITERIOS and isinstance(v, (int, float)) and float(v) != 0}
    if not limpos:
        limpos = dict(PESOS_PADRAO)
    total = sum(limpos.values())
    return {k: v / total for k, v in limpos.items()} if total else dict(PESOS_PADRAO)


def pontuar(candidatos: list[dict], pesos: dict | None) -> list[dict]:
    """
    Cada candidato com a sua nota entre 0 e 1, e a decomposicao da nota.

    A decomposicao nao e enfeite: e o que responde "porque e que esta ganhou".
    Uma nota sozinha e um numero em que se tem de acreditar.
    """
    p = normalizar_pesos(pesos)
    if not candidatos:
        return []

    faixas = {}
    for crit in p:
        vals = [float(c[crit]) for c in candidatos
                if isinstance(c.get(crit), (int, float))]
        faixas[crit] = (min(vals), max(vals)) if vals else (0.0, 0.0)

    fora = []
    for c in candidatos:
        parcelas, nota = {}, 0.0
        for crit, peso in p.items():
            lo, hi = faixas[crit]
            v = c.get(crit)
            if not isinstance(v, (int, float)):
                # sem valor nao se inventa um zero: o criterio nao pontua e
                # diz-se que nao pontuou
                parcelas[crit] = {"valor": None, "escala": None, "peso": peso,
                                  "contributo": 0.0, "nota": "sem valor"}
                continue
            if hi == lo:
                # TODOS IGUAIS NESTE CRITERIO: ele nao distingue ninguem.
                #
                # Inverter um 1.0 num criterio de custo transformava "nao ha
                # informacao" em "o pior possivel", e um candidato sozinho — que
                # e por definicao o melhor que ha — saia com nota 0,5 em vez de
                # 1. Um criterio que nao separa nao deve empurrar a nota para
                # lado nenhum, e o unico valor que faz isso e o mesmo para
                # todos, antes de qualquer inversao.
                escala = 1.0
            else:
                escala = (float(v) - lo) / (hi - lo)
                if CRITERIOS[crit]["sentido"] < 0:
                    escala = 1.0 - escala
            parcelas[crit] = {"valor": float(v), "escala": round(escala, 4),
                              "peso": peso, "contributo": round(escala * peso, 4)}
            nota += escala * peso
        fora.append({**c, "nota": round(nota, 4), "nota_parcelas": parcelas})
    return sorted(fora, key=lambda x: -x["nota"])


def custo_do_golpe(municao: str | None, precos: dict,
                   vida_por_golpe: float = 0.0, vida_maxima: float = 0.0,
                   comida: str | None = None, comida_vida_pct: float | None = None,
                   pecas_vestidas: dict[str, str] | None = None,
                   custos_scrap: dict | None = None,
                   preco_scrap: float | None = None,
                   esquiva_efetiva: float = 0.0) -> dict:
    """
    O que custa um golpe: municao, comida e o DESGASTE do equipamento.

    A comida e um custo que depende da BUILD e nao so do mercado — repoe uma
    percentagem do maximo, por isso mais vida e mais armadura barateiam o golpe.

    O desgaste era a parcela que faltava, e e a maior: cada golpe gasta 1% de
    cada peca vestida, ou seja cem golpes destroem o conjunto. Sem ele, uma
    build de mythic parecia custar quase o mesmo que uma de common e o "dano
    por moeda" ficava inflacionado exactamente nas builds caras — que sao
    aquelas em que a pergunta interessa. Ver `warera/durabilidade.py`.
    """
    from . import durabilidade

    p = precos or {}
    c_mun = float(p.get(municao) or 0) if municao else 0.0
    c_com = 0.0
    if comida and comida_vida_pct and vida_maxima > 0:
        vida_por_item = vida_maxima * float(comida_vida_pct) / 100.0
        if vida_por_item > 0:
            c_com = float(p.get(comida) or 0) * (vida_por_golpe / vida_por_item)

    desgaste = {"total": 0.0, "linhas": []}
    if pecas_vestidas:
        desgaste = durabilidade.custo_por_golpe(
            pecas_vestidas, p, custos_scrap, preco_scrap, esquiva_efetiva)

    c_eq = float(desgaste.get("total") or 0.0)
    return {"municao": round(c_mun, 4), "comida": round(c_com, 4),
            "equipamento": round(c_eq, 4),
            "desgaste": desgaste,
            "total": round(c_mun + c_com + c_eq, 4)}


def melhor_consumiveis(stats: dict, cfg: dict, precos: dict,
                       criterio: str = "dano_dia",
                       horas_combate: float = 8.0,
                       comida: str | None = None,
                       comida_vida_pct: float | None = None,
                       preco_pilula: float | None = None,
                       batalha=1.0, posto_pct: float = 0.0,
                       arma_e_faca: bool = False,
                       pecas_vestidas: dict[str, str] | None = None,
                       custos_scrap: dict | None = None,
                       preco_scrap: float | None = None,
                       pesos: dict | None = None) -> dict:
    """
    Que municao e se pilula — enumerado aos precos de hoje, nao por regra fixa.

    Sao 3 municoes x 2 hipoteses de pilula = 6 combinacoes (ou "sem municao"
    para quem anda de faca, que e a unica arma que pode bater sem ela e a unica
    que nao a pode usar). Enumerar meia duzia de casos e mais barato do que
    discutir qual e a regra, e tem a vantagem de a resposta mudar quando o
    mercado muda.

    A pilula nao entra aqui como `bool`: entra pelo veredicto da janela, que ja
    sabe que espalhada pelo dia ela TIRA dano. Uma build que combate 20 horas
    por dia vai receber "sem pilula" e essa e a resposta certa.
    """
    candidatos = []
    for mun in (MUNICOES_COM_FACA if arma_e_faca else MUNICOES):
        for com_pilula in (False, True):
            r = simular_dia(stats, cfg, municao=mun, pilula_ligada=com_pilula,
                            horas_combate=horas_combate, precos=precos,
                            comida=comida, comida_vida_pct=comida_vida_pct,
                            preco_pilula=preco_pilula, batalha=batalha,
                            posto_pct=posto_pct, pecas_vestidas=pecas_vestidas,
                            custos_scrap=custos_scrap, preco_scrap=preco_scrap)
            if r:
                candidatos.append(r)
    if not candidatos:
        return {}

    if criterio == "pesos":
        # o objectivo a medida: a ordenacao sai da funcao de pesos e nao de
        # uma chave fixa, e cada candidato leva a decomposicao da nota
        ordenados = pontuar(candidatos, pesos)
        return {**ordenados[0], "alternativas": ordenados,
                "pesos": normalizar_pesos(pesos)}

    if criterio == "dano_por_moeda":
        chave = lambda x: (x["dano_por_moeda"] or 0)          # noqa: E731
    elif criterio == "dano_por_golpe":
        chave = lambda x: x["dano_por_golpe"]                  # noqa: E731
    else:
        chave = lambda x: (x["dano_dia"] or 0)                 # noqa: E731

    melhor = max(candidatos, key=chave)
    return {**melhor, "alternativas": sorted(candidatos, key=chave, reverse=True)}


def simular_dia(stats: dict, cfg: dict, municao: str | None = None,
                pilula_ligada: bool = False, horas_combate: float = 8.0,
                precos: dict | None = None, comida: str | None = None,
                comida_vida_pct: float | None = None,
                preco_pilula: float | None = None,
                batalha=1.0, posto_pct: float = 0.0,
                pecas_vestidas: dict[str, str] | None = None,
                custos_scrap: dict | None = None,
                preco_scrap: float | None = None, **kw) -> dict | None:
    """
    Um dia inteiro desta build: golpes, dano, custo e dano por moeda.

    A pilula entra pela janela de 24 horas e nao como multiplicador constante,
    por isso o dano do dia ja conta a ressaca de quem combate demasiadas horas.
    """
    ligada = bool(pilula_ligada or kw.get("pilula"))
    # o dano por golpe DENTRO do buff; a distribuicao pelas horas vem a seguir
    r = simulador.dano_disponivel(
        stats, cfg, municao=municao, pilula=False, batalha=batalha,
        posto_pct=posto_pct, comida_vida_pct=comida_vida_pct)
    if not r.get("dano_dia"):
        return None

    # a comida repoe a vida que se gastou MESMO, e um golpe esquivado nao gasta
    # nenhuma: o custo da comida segue o custo esperado e nao o nominal
    custo = custo_do_golpe(municao, precos or {}, r["vida_por_golpe_esperada"],
                           r.get("vida_maxima") or 0, comida, comida_vida_pct,
                           pecas_vestidas=pecas_vestidas,
                           custos_scrap=custos_scrap, preco_scrap=preco_scrap,
                           esquiva_efetiva=(r.get("esquiva_pct") or 0) / 100.0)
    dano_dia = r["dano_dia"]
    custo_dia = r["golpes_dia"] * custo["total"]
    ganho_pilula = None

    if ligada:
        v = pilula.avaliar(r["golpes_dia"], r["dano_por_golpe"], horas_combate,
                           cfg=cfg, preco_pilula=preco_pilula)
        if "erro" not in v:
            dano_dia = v["dano_dia_com_pilula"]
            ganho_pilula = v["ganho_pct"]
            custo_dia += float(preco_pilula or 0)

    return {
        "municao": municao,
        "pilula": ligada,
        "dano_por_golpe": r["dano_por_golpe"],
        "golpes_dia": r["golpes_dia"],
        "limitado_por": r.get("limitado_por"),
        "dano_dia": round(dano_dia, 0),
        "custo_por_golpe": custo["total"],
        "custo_detalhe": custo,
        "custo_dia": round(custo_dia, 2),
        "custo_por_1k_dano": (round(custo_dia / (dano_dia / 1000.0), 4)
                              if dano_dia else None),
        "dano_por_moeda": round(dano_dia / custo_dia, 1) if custo_dia > 0 else None,
        "ganho_pilula_pct": ganho_pilula,
    }


def _joelho(pontos: list[dict]) -> dict | None:
    """
    O joelho da curva custo-dano: onde mais dinheiro deixa de comprar dano.

    Normalizam-se os dois eixos para [0,1] e escolhe-se o ponto mais distante
    da recta que une o primeiro ao ultimo. E o metodo do "cotovelo", e tem a
    vantagem de nao precisar de nenhum limiar inventado a mao.
    """
    candidatos = [p for p in pontos if p.get("custo_dia") and p.get("dano_dia")]
    # So a fronteira entra na conta. Sem isto o joelho podia cair num ponto
    # DOMINADO — um que custa mais e da menos do que outro da lista — e o
    # "equilibrado" saia pior do que o "sustentado" nos dois eixos ao mesmo
    # tempo, o que nao e um equilibrio nenhum: e uma escolha errada.
    validos, melhor_dano = [], -1.0
    for p in sorted(candidatos, key=lambda x: x["custo_dia"]):
        if p["dano_dia"] > melhor_dano:
            validos.append(p)
            melhor_dano = p["dano_dia"]

    if len(validos) < 3:
        return validos[-1] if validos else None
    c0, c1 = validos[0]["custo_dia"], validos[-1]["custo_dia"]
    d0, d1 = validos[0]["dano_dia"], validos[-1]["dano_dia"]
    if c1 == c0 or d1 == d0:
        return validos[-1]

    melhor, melhor_d = None, -1.0
    for p in validos:
        x = (p["custo_dia"] - c0) / (c1 - c0)
        y = (p["dano_dia"] - d0) / (d1 - d0)
        # distancia perpendicular a recta y = x (os dois eixos ja normalizados)
        dist = abs(y - x)
        if dist > melhor_d:
            melhor, melhor_d = p, dist
    return melhor


def resolver(objetivo: str, pontos: int, skills: list[dict], cfg: dict,
             equipamento: dict[str, float] | None = None,
             base_niveis: dict[str, int] | None = None,
             precos: dict | None = None, horas_combate: float = 8.0,
             comida: str | None = None, comida_vida_pct: float | None = None,
             preco_pilula: float | None = None, posto_pct: float = 0.0,
             batalha_guerra=None, regen_dividido_por: float = 10.0,
             pecas_vestidas: dict[str, str] | None = None,
             custos_scrap: dict | None = None,
             preco_scrap: float | None = None,
             pesos: dict | None = None) -> dict:
    """
    A melhor build para UM objectivo, com os consumiveis escolhidos por procura.

    Os pontos de skill nao custam dinheiro, por isso a escolha de skills e a
    mesma para "maximo por dia" e para "maximo por moeda" — o que muda entre
    esses dois objectivos sao os CONSUMIVEIS, e e por isso que eles sao
    enumerados aqui em vez de fixados por regra.
    """
    d = OBJETIVOS.get(objetivo)
    if not d:
        return {"erro": f"objectivo desconhecido: {objetivo}"}

    batalha = 1.0
    if objetivo == "guerra":
        batalha = (batalha_guerra if batalha_guerra is not None
                   else bonus.realista(cfg)["fator"])

    criterio = {"burst": "dano_por_golpe", "economico": "dano_por_moeda",
                "custom": "pesos"}.get(objetivo, "dano_dia")

    def _com(alvo_skill):
        b = simulador.melhor_build(
            pontos, skills, cfg, equipamento=equipamento, objetivo=alvo_skill,
            regen_dividido_por=regen_dividido_por, base_niveis=base_niveis,
            comida_vida_pct=comida_vida_pct, batalha=batalha, posto_pct=posto_pct)
        if "erro" in b:
            return b, {}
        return b, melhor_consumiveis(
            b["stats"], cfg, precos or {}, criterio=criterio,
            horas_combate=horas_combate, comida=comida,
            comida_vida_pct=comida_vida_pct, preco_pilula=preco_pilula,
            batalha=batalha, posto_pct=posto_pct, pecas_vestidas=pecas_vestidas,
            custos_scrap=custos_scrap, preco_scrap=preco_scrap, pesos=pesos)

    if objetivo == "custom":
        # O optimizador exacto sabe maximizar duas coisas: dano por golpe e
        # dano por dia. Uma mistura pesada das duas nao e nenhuma delas, e nao
        # se resolve na mesma fronteira — o produto `dano x golpes` deixa de
        # ser o criterio.
        #
        # Em vez de inventar uma procura nova (e de a fazer mal), avaliam-se as
        # DUAS builds exactas — a de burst e a de sustento — com os consumiveis
        # de cada uma, e escolhe-se a que pontua mais com os pesos dados. Uma
        # mistura optima cai entre as duas, por isso isto e um limite inferior
        # honesto e nao um optimo global; o campo `procura` diz exactamente
        # isso a quem le, em vez de deixar acreditar que e exacto.
        tentativas = []
        for alvo in ("dia", "golpe"):
            b, c = _com(alvo)
            if "erro" not in b and c:
                tentativas.append((alvo, b, c))
        if not tentativas:
            return {"erro": "sem tabela de skills"}
        alvo, build, cons = max(tentativas, key=lambda t: t[2].get("nota") or 0)
    else:
        build, cons = _com("golpe" if objetivo == "burst" else "dia")
        if "erro" in build:
            return build

    if objetivo == "equilibrado" and cons.get("alternativas"):
        # aqui o joelho e sobre as alternativas de consumivel, que e onde o
        # dinheiro se gasta mesmo: as skills sao de graca
        j = _joelho(sorted(cons["alternativas"], key=lambda x: x.get("custo_dia") or 0))
        if j:
            cons = {**j, "alternativas": cons["alternativas"]}

    extra = {}
    if objetivo == "custom":
        extra = {
            "pesos": cons.get("pesos"),
            "nota": cons.get("nota"),
            "nota_parcelas": cons.get("nota_parcelas"),
            "procura": {
                "candidatas": [t[0] for t in tentativas],
                "escolhida": alvo,
                "exacto": False,
                "porque": ("as duas builds exactas (burst e sustento) foram "
                           "pontuadas com estes pesos e ganhou a melhor; uma "
                           "mistura óptima pode cair entre as duas"),
            },
        }

    return {
        "objetivo": objetivo,
        **d,
        **extra,
        "niveis": build.get("niveis"),
        "sobe": build.get("sobe"),
        "stats": build.get("stats"),
        "pontos_gastos": build.get("pontos_gastos"),
        "batalha": batalha,
        "consumiveis": cons,
        "dano_por_golpe": cons.get("dano_por_golpe", build.get("dano_por_golpe")),
        "dano_dia": cons.get("dano_dia", build.get("dano_dia")),
        "custo_dia": cons.get("custo_dia"),
        "custo_por_1k_dano": cons.get("custo_por_1k_dano"),
        "golpes_dia": cons.get("golpes_dia", build.get("golpes_dia")),
        "limitado_por": cons.get("limitado_por", build.get("limitado_por")),
    }


def comparar(pontos: int, skills: list[dict], cfg: dict, **kw) -> list[dict]:
    """
    Os objectivos lado a lado — a tabela que responde 'qual escolho'.

    O `custom` so entra se houver pesos: sem eles cai nos pesos por omissao e
    sai uma copia exacta do `sustentado`, que e uma linha a mais a dizer o
    mesmo.
    """
    fora = []
    for nome in OBJETIVOS:
        if nome == "custom" and not kw.get("pesos"):
            continue
        r = resolver(nome, pontos, skills, cfg, **kw)
        if "erro" not in r:
            fora.append(r)
    return fora


def proxima_accao(stats: dict, cfg: dict, skills: list[dict],
                  pontos_livres: int = 0, dinheiro: float = 0.0,
                  precos: dict | None = None, horas_combate: float = 8.0,
                  comida: str | None = None, comida_vida_pct: float | None = None,
                  preco_pilula: float | None = None, batalha=1.0,
                  posto_pct: float = 0.0, quota_alianca_pct: float | None = None,
                  municao: str | None = None, niveis: dict[str, int] | None = None,
                  vestido: dict[str, str] | None = None,
                  itens: dict | None = None, orcamento: float | None = None,
                  custos_scrap: dict | None = None,
                  preco_scrap: float | None = None) -> list[dict]:
    """
    O que fazer a seguir, por ordem de retorno — e as de graca primeiro.

    Ha accoes que nao custam pontos nem dinheiro e valem mais do que qualquer
    compra: mudar a hora a que se toma a pilula, ligar as duas ordens de
    batalha, sair de uma alianca inchada. Uma lista que so fale de comprar
    equipamento deixa essas de fora, e sao as unicas com retorno infinito.

    `niveis` sao os niveis QUE O JOGADOR TEM, skill a skill. Nao e opcional por
    conveniencia: sem eles a recomendacao de pontos avalia sempre o salto do
    nivel 0 para o 1 — que e o maior de todos, porque a escada tem rendimentos
    decrescentes — e diz a um jogador de nivel 8 para subir a skill que ja
    esta no fim. Nao confundir com `skills`, que e a TABELA do jogo: essa nao
    sabe nada sobre quem esta a jogar.
    """
    from . import breakpoints

    accoes = []

    # --- de graca: os bonus que so precisam de coordenacao ---
    if quota_alianca_pct is not None:
        a = bonus.alianca(quota_alianca_pct)
        if a["pct"] < 0:
            accoes.append({
                "ordem": 0, "custo": 0, "tipo": "diplomacia",
                "accao": "Sair da aliança ou reduzi-la",
                "porque": a["porque"],
                "ganho_pct": round(-a["pct"], 1),
                "de_graca": True,
            })

    # --- de graca: a pilula na hora certa ---
    if comida_vida_pct is not None:
        r = simulador.dano_disponivel(stats, cfg, comida_vida_pct=comida_vida_pct,
                                      batalha=batalha, posto_pct=posto_pct)
        if r.get("golpes_dia"):
            v = pilula.veredicto(r["golpes_dia"], r["dano_por_golpe"],
                                 horas_combate, cfg=cfg, preco_pilula=preco_pilula)
            if "erro" not in v:
                accoes.append({
                    "ordem": 1, "custo": round(float(preco_pilula or 0), 2),
                    "tipo": "consumível",
                    "accao": (f"Tomar a pílula às {v['tomar_as']}h e concentrar o combate"
                              if v["compensa"] else "NÃO tomar pílula"),
                    "porque": v["razao"],
                    "ganho_pct": v["ganho_pct"],
                    "de_graca": not v["compensa"],
                })

    # --- pontos de skill: qual o proximo ---
    #
    # Isto media o ganho por GOLPE (`pecas.prioridade_de_dano`) e estava a
    # responder a pergunta errada. Um ponto de armadura nao muda o dano por
    # golpe nenhum e e muitas vezes o melhor ponto que ha, porque compra
    # golpes; medido por golpe, aparecia com ganho zero e nunca era
    # recomendado. Passa a usar o `warera.upgrade`, que mede por DIA e ja e a
    # unica implementacao dessa conta.
    #
    # OS NIVEIS TEM DE VIR DE FORA. Isto lia-os de `skills[i]["nivel"]`, uma
    # chave que a tabela de `mecanicas.skills` nunca teve — o que dava sempre
    # um dicionario vazio e punha toda a gente a avaliar o salto de 0 para 1.
    from . import regras, upgrade as _up
    rs = regras.carregar(cfg)
    niveis_do_jogador = {k: int(v) for k, v in (niveis or {}).items()}
    kw_motor = dict(municao=municao, pilula=False, batalha=batalha,
                    posto_pct=posto_pct, comida_vida_pct=comida_vida_pct)

    if pontos_livres > 0:
        lista = _up.por_skill(stats, rs, skills, niveis_do_jogador, **kw_motor)
        if lista:
            topo = lista[0]
            accoes.append({
                "ordem": 2, "custo": 0, "tipo": "skill",
                "accao": f"Pôr os próximos pontos em {topo['nome']}",
                "porque": (f"{topo['porque']} · +{topo['ganho_dano_dia']:,.0f} de dano "
                           f"por dia".replace(",", " ")),
                "ganho_pct": topo["ganho_pct"],
                "de_graca": True,
                "sem_niveis": not niveis_do_jogador,
            })

    # --- equipamento: a compra que mais rende, medida por DIA ---
    #
    # Faltava por completo nesta lista, e e a mais cara de faltar. A pergunta
    # "o que faco a seguir" respondida so com skills e consumiveis deixa de
    # fora a unica coisa em que ha dinheiro a decidir — e foi aqui que se viu
    # que umas botas de nivel 1 valiam mais dano por dia do que um ponto de
    # ataque, uma conclusao que nenhuma medicao por GOLPE consegue produzir.
    if itens and precos:
        eq = _up.por_equipamento(stats, rs, itens, precos, vestido,
                                 orcamento, custos_scrap, preco_scrap, **kw_motor)
        if eq:
            topo = eq[0]
            accoes.append({
                "ordem": 2, "custo": topo.get("preco"), "tipo": "equipamento",
                "accao": f"Comprar {topo['nome']}",
                "porque": (f"{topo['porque']} · +{topo['ganho_dano_dia']:,.0f} de dano "
                           f"por dia".replace(",", " ")),
                "ganho_pct": topo["ganho_pct"],
                "custo_dia": topo.get("custo_dia"),
                "dias_ate_pagar": topo.get("dias_ate_pagar"),
                "de_graca": False,
            })

    # --- consumiveis: qual a municao certa a estes precos ---
    if precos:
        c = melhor_consumiveis(stats, cfg, precos, criterio="dano_por_moeda",
                               horas_combate=horas_combate, comida=comida,
                               comida_vida_pct=comida_vida_pct,
                               preco_pilula=preco_pilula, batalha=batalha,
                               posto_pct=posto_pct)
        if c.get("municao") is not None or c.get("alternativas"):
            accoes.append({
                "ordem": 3, "custo": c.get("custo_dia"), "tipo": "consumível",
                "accao": f"Usar {c.get('municao') or 'sem munição'}",
                "porque": (f"aos preços de agora dá {c.get('dano_por_moeda')} de dano "
                           f"por moeda, o melhor das {len(c.get('alternativas') or [])} "
                           f"combinações testadas"),
                "ganho_pct": None,
                "de_graca": False,
            })

    return sorted(accoes, key=lambda x: (x["ordem"], -(x.get("ganho_pct") or 0)))


def pecas_prioridade(stats: dict, cfg: dict) -> list[dict]:
    """Atalho para `pecas.prioridade_de_dano`, ordenado pelo ganho."""
    from . import pecas
    return sorted(pecas.prioridade_de_dano(stats, cfg),
                  key=lambda x: -x["ganho_dano_pct"])
