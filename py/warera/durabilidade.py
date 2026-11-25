"""
O equipamento gasta-se a bater, e isso e a maior parte do custo de combater.

Este painel contava o golpe como municao mais a comida que repoe a vida
gastada. Faltava a parcela maior: **cada golpe consome 1% de durabilidade de
cada peca vestida**, ou seja cem golpes destroem o conjunto todo. Num simulador
de referencia da comunidade, a armadura sozinha era 35% do custo de uma
batalha — mais do que a municao e a comida juntas.

Ignorar isto nao fazia o painel errar por pouco: fazia-o dizer que uma build de
mythic custava quase o mesmo que uma de common, quando a diferenca real e de
duas ordens de grandeza. E o "dano por moeda" ficava inflacionado exactamente
nas builds caras, que sao aquelas em que a pergunta importa.

As regras, da wiki (`equipment/boots` e `equipment/chest`):

  - cada golpe tira 1% a capacete, peito, calcas, luvas e botas;
  - um golpe ESQUIVADO nao tira nada a essas cinco;
  - a ARMA perde 1% em todos os golpes, esquivados incluidos;
  - uma peca a 1% e automaticamente desequipada antes do golpe, por isso nao
    ha destruicao acidental — mas tambem nao ha golpe com ela.

O custo nao e o preco da peca: e o preco MENOS o que ela devolve desfeita. Uma
peca gasta desmantela-se por um terco dos scraps (`FRACAO_SCRAPS_PECA_PARTIDA`),
ao preco de scrap de hoje. Quem nao descontar isso paga a peca duas vezes.
"""

from __future__ import annotations

from . import config, pecas

# 1% por golpe: cem golpes gastam a peca de 100% a zero
GOLPES_POR_PECA = 100.0

SLOTS_EQUIPAMENTO = ["helmet", "chest", "pants", "gloves", "boots"]
SLOT_ARMA = "weapon"


def custo_por_golpe(pecas_vestidas: dict[str, str], precos: dict,
                    custos_scrap: dict | None = None,
                    preco_scrap: float | None = None,
                    esquiva_efetiva: float = 0.0,
                    taxa: float = config.TAXA_MERCADO_PADRAO) -> dict:
    """
    Quanto custa, em desgaste, cada golpe com este conjunto vestido.

    `pecas_vestidas` e {slot: codigo}. `esquiva_efetiva` e a probabilidade de
    esquivar (0 a 1) e entra porque um golpe esquivado poupa as cinco pecas —
    mas nunca a arma.

    A esquiva poupa aqui pela SEGUNDA vez. A primeira e nos golpes: um golpe
    esquivado nao gasta vida nenhuma ("a free hit"), por isso a esquiva compra
    golpes — ver `simulador.simular`. A segunda e esta: tambem nao gasta a
    durabilidade das cinco pecas. Este painel nao contava nenhuma das duas e
    dizia que as botas nunca valiam a pena.
    """
    p = precos or {}
    custos_scrap = custos_scrap or {}
    linhas, total = [], 0.0

    for slot, codigo in (pecas_vestidas or {}).items():
        if not codigo:
            continue
        preco = float(p.get(codigo) or 0.0)
        if preco <= 0:
            linhas.append({"slot": slot, "codigo": codigo, "custo": None,
                           "nota": "sem preço de mercado para esta peça"})
            continue

        rec = pecas.valor_de_reciclagem(codigo, custos_scrap, preco_scrap, taxa)
        devolve = float((rec or {}).get("partida") or 0.0)
        liquido = max(0.0, preco - devolve)

        # a arma gasta-se em todos os golpes; as outras cinco escapam quando se
        # esquiva, e e por isso que a esquiva vale dinheiro
        fraccao = 1.0 if slot == SLOT_ARMA else (1.0 - max(0.0, min(1.0, esquiva_efetiva)))
        custo = liquido / GOLPES_POR_PECA * fraccao
        total += custo
        linhas.append({
            "slot": slot, "codigo": codigo,
            "preco": round(preco, 3),
            "devolve_desfeita": round(devolve, 3),
            "liquido": round(liquido, 3),
            "golpes_ate_partir": GOLPES_POR_PECA,
            "poupado_pela_esquiva": slot != SLOT_ARMA,
            "custo": round(custo, 5),
        })

    linhas.sort(key=lambda x: -(x.get("custo") or 0))
    return {
        "total": round(total, 5),
        "linhas": linhas,
        "sem_preco": [l["slot"] for l in linhas if l.get("custo") is None],
        "esquiva_pct": round(esquiva_efetiva * 100, 2),
    }


def golpes_ate_repor(pecas_vestidas: dict[str, str],
                     esquiva_efetiva: float = 0.0) -> dict:
    """
    Quantos golpes ate cada peca partir, e qual e a primeira a ir.

    Todas duram os mesmos cem golpes, menos as cinco que a esquiva poupa — por
    isso a ARMA e sempre a primeira a partir de quem tem esquiva. Saber qual e
    a proxima a ir e o que evita chegar a batalha com um slot vazio.
    """
    escapa = max(0.0, min(1.0, esquiva_efetiva))
    fora = []
    for slot, codigo in (pecas_vestidas or {}).items():
        if not codigo:
            continue
        # cada golpe gasta 1% com probabilidade (1-esquiva), logo o numero
        # esperado de golpes ate gastar 100% e 100/(1-esquiva)
        if slot == SLOT_ARMA or escapa >= 1.0:
            golpes = GOLPES_POR_PECA
        else:
            golpes = GOLPES_POR_PECA / (1.0 - escapa)
        fora.append({"slot": slot, "codigo": codigo, "golpes": round(golpes, 1)})
    fora.sort(key=lambda x: x["golpes"])
    return {
        "pecas": fora,
        "primeira_a_partir": fora[0] if fora else None,
        "golpes_ate_a_primeira": fora[0]["golpes"] if fora else None,
    }


def plano_de_reposicao(pecas_vestidas: dict[str, str], precos: dict,
                       golpes_dia: float, dias: float = 1.0,
                       custos_scrap: dict | None = None,
                       preco_scrap: float | None = None,
                       esquiva_efetiva: float = 0.0) -> dict:
    """
    Quantas pecas de cada slot e preciso comprar para aguentar `dias` dias.

    E a pergunta que o "custo por golpe" nao responde e que decide o orcamento:
    nao interessa que um golpe custe 0,42 se para combater uma semana e preciso
    comprar catorze capacetes de uma vez. As melhores ferramentas da comunidade
    chamam a isto `equipment replacement` e e o que separa uma build que se
    aguenta de uma que se usa vinte minutos.
    """
    golpes = max(0.0, float(golpes_dia) * float(dias))
    escapa = max(0.0, min(1.0, esquiva_efetiva))
    p = precos or {}
    custos_scrap = custos_scrap or {}

    linhas, custo_total = [], 0.0
    for slot, codigo in (pecas_vestidas or {}).items():
        if not codigo:
            continue
        gastos = golpes if slot == SLOT_ARMA else golpes * (1.0 - escapa)
        unidades = gastos / GOLPES_POR_PECA
        preco = float(p.get(codigo) or 0.0)
        rec = pecas.valor_de_reciclagem(codigo, custos_scrap, preco_scrap)
        devolve = float((rec or {}).get("partida") or 0.0)
        liquido = max(0.0, preco - devolve) * unidades
        custo_total += liquido
        linhas.append({
            "slot": slot, "codigo": codigo,
            "unidades": round(unidades, 2),
            # quem compra nao compra 3,4 capacetes: compra 4
            "unidades_a_comprar": int(unidades) + (1 if unidades % 1 else 0),
            "preco_unitario": round(preco, 3),
            "devolve_desfeita": round(devolve, 3),
            "custo_liquido": round(liquido, 2),
        })

    linhas.sort(key=lambda x: -x["custo_liquido"])
    return {
        "dias": dias,
        "golpes": round(golpes, 1),
        "custo_total": round(custo_total, 2),
        "custo_por_dia": round(custo_total / dias, 2) if dias else None,
        "linhas": linhas,
        "slot_mais_caro": linhas[0] if linhas else None,
    }
