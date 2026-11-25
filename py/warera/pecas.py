"""
Uma peca nao vale o que vale o seu codigo: vale o que valem os stats que lhe
sairam.

O `chest4` tem armor entre 21 e 30. Nas vendas reais, um com armor 22 fez 37.00
e um com armor 30 fez 61.45 — 66% de diferenca dentro do mesmo item. O resto do
painel usava uma mediana por codigo, o que trata os dois como a mesma coisa e
faz perder as duas pontas: nao ve a pechincha e nao ve a peca cara.

Aqui a peca e avaliada pelo roll. O modelo e uma reta ajustada as vendas reais
de cada codigo (preco em funcao da soma dos stats), e nao uma teoria: a
correlacao medida entre stats e preco anda entre +0.8 e +0.98 nos codigos com
amostra, ou seja o mercado ja faz esta conta — so nao a mostra a ninguem.

E preciso amostra: com menos de `MIN_AMOSTRA` vendas a reta e ruido, e nesse
caso o painel diz que nao sabe em vez de inventar um preco justo.

As regras dos stats vem do jogo e nao de suposicao (`gameConfig.skills`):

    armor, dodge          softCap 40
    precision             o que passa do maximo vira attack, 4 por ponto
    criticalChance        o que passa do maximo vira criticalDamages, 4 por ponto

Isso decide para onde vale a pena atirar dinheiro: encher armor acima de 40 nao
faz nada, enquanto precision a mais continua a valer alguma coisa.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from . import config, motor, regras

MIN_AMOSTRA = 6

# A amostra de vendas de equipamento cobre 30 dias, mas o mercado de ha um mes
# nao e o de hoje. Cada venda pesa metade por cada cinco dias de idade: as
# antigas ainda ajudam a ver a inclinacao (quanto vale um ponto de stat) sem
# arrastarem o nivel de preco para tras.
MEIA_VIDA_DIAS = 5.0

# A formula de dano e a do guia oficial do jogo, e nao uma suposicao:
#
#   - a precisao e a probabilidade de acertar. Uma falha da METADE do dano e
#     nunca e critica;
#   - num acerto, o critico multiplica por (1 + danoCritico/100);
#   - o que passa de 100 de precisao vira ataque (4 por ponto) e o que passa
#     de 100 de critico vira dano critico (4 por ponto).
#
#       E[dano] = ataque x [ p x ((1-c) + c x (1 + d/100)) + (1-p) x 0.5 ]
#
# O painel usava `ataque x (1 + c x d)`, que ignorava as falhas por completo e
# dizia que precisao abaixo do tecto nao valia nada. Valia: subir de 80 para
# 100 de precisao da 10% de dano medio.
STATS_OFENSIVOS = ("attack", "criticalChance", "criticalDamages", "precision")
STATS_DEFENSIVOS = ("armor", "dodge")


def intervalos(code: str, itens: dict) -> dict[str, tuple[float, float]]:
    """{stat: (minimo, maximo)} do que pode sair nesta peca."""
    din = (itens.get(code) or {}).get("dynamicStats") or {}
    fora = {}
    for stat, faixa in din.items():
        if isinstance(faixa, (list, tuple)) and len(faixa) == 2:
            fora[stat] = (float(faixa[0]), float(faixa[1]))
    return fora


def qualidade(skills: dict, faixas: dict[str, tuple[float, float]]) -> float | None:
    """
    Onde e que este roll cai dentro do que era possivel sair, de 0 a 1.

    1.0 e o maximo da raridade, 0.0 o minimo. E o numero que diz se tiveste
    sorte, e o unico que se pode comparar entre pecas diferentes.
    """
    partes = []
    for stat, (lo, hi) in faixas.items():
        v = skills.get(stat)
        if v is None or hi <= lo:
            continue
        partes.append(max(0.0, min(1.0, (float(v) - lo) / (hi - lo))))
    return sum(partes) / len(partes) if partes else None


def _partes(linha) -> tuple[float, dict, str | None]:
    """Aceita (preco, skills) ou (preco, skills, quando)."""
    if len(linha) >= 3:
        return float(linha[0]), linha[1], linha[2]
    return float(linha[0]), linha[1], None


def _peso(quando: str | None, agora: datetime | None) -> float:
    """
    Quanto e que uma venda de ha N dias ainda conta: metade por cada
    `MEIA_VIDA_DIAS`. Sem data, conta por inteiro.
    """
    if not quando or not agora:
        return 1.0
    try:
        d = datetime.fromisoformat(str(quando).replace("Z", "+00:00"))
    except ValueError:
        return 1.0
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    dias = max(0.0, (agora - d).total_seconds() / 86400.0)
    return 0.5 ** (dias / MEIA_VIDA_DIAS)


def _reta(pontos: list[tuple[float, float, float]]) -> tuple[float, float, float] | None:
    """
    Minimos quadrados PONDERADOS: devolve (declive, ordenada, correlacao).

    Cada ponto e (x, y, peso). Sem pesos isto seria uma reta que trata uma
    venda de ha um mes como se fosse de agora.
    """
    total = sum(w for _, _, w in pontos)
    if len(pontos) < 2 or total <= 0:
        return None
    mx = sum(x * w for x, _, w in pontos) / total
    my = sum(y * w for _, y, w in pontos) / total
    sxy = sum(w * (x - mx) * (y - my) for x, y, w in pontos)
    sxx = sum(w * (x - mx) ** 2 for x, _, w in pontos)
    syy = sum(w * (y - my) ** 2 for _, y, w in pontos)
    if sxx <= 0 or syy <= 0:
        return None
    b = sxy / sxx
    return b, my - b * mx, sxy / (sxx * syy) ** 0.5


def _prever(declive: float, ordenada: float, limites: tuple[float, float]):
    """
    A reta, presa ao intervalo de precos onde houve mesmo vendas.

    Fora desse intervalo uma reta ajustada a dez pontos deixa de ser uma
    estimativa e passa a ser uma opiniao com casas decimais.
    """
    lo, hi = limites

    def prever(x: float) -> float:
        return max(lo, min(hi, ordenada + declive * x))
    return prever


def modelo_de_preco(vendas: dict[str, list[tuple[float, dict]]], itens: dict) -> dict[str, dict]:
    """
    Por codigo: quanto e que o mercado paga por ponto de stat.

    `vendas` vem do feed: {codigo: [(preco, skills), ...]}. Cada venda de
    equipamento traz o roll da peca que mudou de maos, por isso da para ligar
    preco a qualidade sem ter de perguntar nada a ninguem.
    """
    fora = {}
    agora = datetime.now(timezone.utc)
    for code, linhas in vendas.items():
        faixas = intervalos(code, itens)
        if not faixas or len(linhas) < MIN_AMOSTRA:
            continue
        pontos = []
        for linha in linhas:
            preco, skills, quando = _partes(linha)
            total = sum(float(v) for k, v in skills.items() if k in faixas)
            if total > 0 and preco > 0:
                pontos.append((total, preco, _peso(quando, agora)))
        r = _reta(pontos)
        if not r:
            continue
        declive, ordenada, correl = r
        soma_min = sum(lo for lo, _ in faixas.values())
        soma_max = sum(hi for _, hi in faixas.values())
        precos = sorted(p for _, p, _w in pontos)
        stats_vistos = sorted(x for x, _, _w in pontos)
        # a reta so vale onde ha vendas. Estende-la ate as pontas do intervalo
        # da raridade dava disparates — num caso real dizia que um rifle no
        # minimo valia 0.87 quando nenhum se vendeu abaixo de 11.
        cobertura = ((stats_vistos[-1] - stats_vistos[0]) / (soma_max - soma_min)
                     if soma_max > soma_min else 0.0)
        # sem extrapolacao nenhuma: qualquer preco que o modelo diga tem de ser
        # um preco que ja foi mesmo pago por esta peca
        limites = (precos[0], precos[-1])
        prever = _prever(declive, ordenada, limites)
        fora[code] = {
            "n": len(pontos),
            "declive": declive,
            "ordenada": ordenada,
            "correlacao": correl,
            "stats": sorted(faixas),
            "soma_min": soma_min,
            "soma_max": soma_max,
            "cobertura": cobertura,
            "stat_visto_min": stats_vistos[0],
            "stat_visto_max": stats_vistos[-1],
            "limite_min": limites[0],
            "limite_max": limites[1],
            "preco_no_minimo": prever(soma_min),
            "preco_no_maximo": prever(soma_max),
            "preco_mediano": precos[len(precos) // 2],
            "fiavel": (len(pontos) >= MIN_AMOSTRA and correl >= 0.5
                       and cobertura >= 0.25),
        }
    return fora


def valor_de_reciclagem(code: str, custos: dict, preco_scrap: float | None,
                        taxa: float = config.TAXA_MERCADO_PADRAO) -> dict | None:
    """
    O que esta peca vale desfeita, ao preco de scrap DE AGORA.

    E o chao de qualquer negociacao com equipamento, e muda todos os dias com o
    preco dos scraps: a mesma peca que hoje vale 3.96 desfeita valia 3.70 na
    semana passada. Por isso o valor e sempre recalculado e nunca guardado.

    `inteira` e com a peca a 100% de estado; `partida` e o terco que sobra
    quando ela ja se gastou toda.
    """
    scraps = custos.get(code)
    if not scraps or not preco_scrap:
        return None
    inteira = scraps * preco_scrap * (1 - taxa)
    return {
        "scraps": scraps,
        "preco_scrap": round(float(preco_scrap), 5),
        "inteira": round(inteira, 3),
        "partida": round(inteira * config.FRACAO_SCRAPS_PECA_PARTIDA, 3),
        "por_scrap": round(inteira / scraps, 5),
    }


def estatisticas_de_preco(vendas: dict[str, list[tuple[float, dict]]], itens: dict,
                          custos: dict, preco_scrap: float | None,
                          taxa: float = config.TAXA_MERCADO_PADRAO) -> list[dict]:
    """
    Por codigo: quanto se pagou de facto — minimo, media, mediana e maximo — e
    quanto vale desfeito ao preco de scrap de agora.

    A media sozinha esconde o que interessa. Quando o MINIMO fica abaixo do
    valor de reciclagem, houve alguem a vender uma peca por menos do que os
    scraps que ela contem: comprar e desfazer e lucro sem risco nenhum, e a
    coluna `lucro_a_reciclar` diz exatamente quanto.
    """
    fora = []
    for code, linhas in vendas.items():
        precos = sorted(_partes(l)[0] for l in linhas if l and l[0])
        if not precos:
            continue
        n = len(precos)
        rec = valor_de_reciclagem(code, custos, preco_scrap, taxa)
        media = sum(precos) / n
        mediana = precos[n // 2] if n % 2 else (precos[n // 2 - 1] + precos[n // 2]) / 2
        abaixo = [p for p in precos if rec and p < rec["inteira"]]
        fora.append({
            "codigo": code,
            "raridade": (itens.get(code) or {}).get("rarity"),
            "tipo": (itens.get(code) or {}).get("type"),
            "n": n,
            "minimo": round(precos[0], 3),
            "media": round(media, 3),
            "mediana": round(mediana, 3),
            "maximo": round(precos[-1], 3),
            "amplitude_pct": round((precos[-1] / precos[0] - 1) * 100, 1) if precos[0] else None,
            "scraps": rec["scraps"] if rec else custos.get(code),
            "reciclar": rec["inteira"] if rec else None,
            "reciclar_partida": rec["partida"] if rec else None,
            "preco_por_scrap_medio": round(media / rec["scraps"], 5) if rec else None,
            "n_abaixo_do_scrap": len(abaixo),
            "lucro_a_reciclar": round(rec["inteira"] - precos[0], 3)
            if (rec and precos[0] < rec["inteira"]) else None,
        })
    return sorted(fora, key=lambda x: (x["lucro_a_reciclar"] is None,
                                       -(x["lucro_a_reciclar"] or 0), -x["n"]))


def pontos_do_modelo(vendas: dict, itens: dict, modelo: dict,
                     max_codigos: int = 4, max_pontos: int = 120) -> dict[str, list]:
    """
    Os pontos (soma dos stats, preco) que o modelo usou, para se poder ver a
    nuvem em vez de acreditar na reta.

    So os codigos com modelo fiavel e um tecto de pontos: isto vai para dentro
    de uma pagina web, nao para um ficheiro de dados.
    """
    escolhidos = sorted((c for c in modelo if modelo[c]["fiavel"]),
                        key=lambda c: -modelo[c]["n"])[:max_codigos]
    fora = {}
    for code in escolhidos:
        faixas = intervalos(code, itens)
        pontos = []
        for linha in vendas.get(code) or []:
            preco, skills, _ = _partes(linha)
            total = sum(float(v) for k, v in skills.items() if k in faixas)
            if total > 0 and preco > 0:
                pontos.append([round(total, 1), round(preco, 2)])
        if pontos:
            fora[code] = pontos[-max_pontos:]
    return fora


def preco_justo(code: str, skills: dict, modelo: dict, itens: dict) -> dict | None:
    """
    O que esta peca, com este roll, devia valer — e o quanto disso vem de ter
    tido sorte no roll.
    """
    m = modelo.get(code)
    faixas = intervalos(code, itens)
    if not m or not faixas:
        return None
    total = sum(float(v) for k, v in skills.items() if k in faixas)
    if total <= 0:
        return None
    justo = _prever(m["declive"], m["ordenada"], (m["limite_min"], m["limite_max"]))(total)
    return {
        "codigo": code,
        "stats": {k: v for k, v in skills.items() if k in faixas},
        "soma_stats": total,
        "qualidade": qualidade(skills, faixas),
        "justo": justo,
        "preco_no_minimo": m["preco_no_minimo"],
        "preco_no_maximo": m["preco_no_maximo"],
        "por_ponto": m["declive"],
        "n": m["n"],
        "correlacao": m["correlacao"],
        "fiavel": m["fiavel"],
    }


def avaliar(code: str, skills: dict, preco: float, modelo: dict, itens: dict,
            custos: dict, preco_scrap: float | None,
            taxa: float = config.TAXA_MERCADO_PADRAO) -> dict | None:
    """
    A conta completa de uma peca a este preco: contra o modelo, contra os
    scraps que contem, e o veredicto.
    """
    j = preco_justo(code, skills, modelo, itens)
    if not j or not preco:
        return None
    scraps = custos.get(code)
    piso = scraps * preco_scrap * (1 - taxa) if (scraps and preco_scrap) else None
    desconto = (j["justo"] - preco) / j["justo"] * 100 if j["justo"] else None

    if piso and preco < piso:
        veredicto = "abaixo do valor dos scraps"
    elif desconto is not None and desconto >= 20:
        veredicto = "bem abaixo do que o roll vale"
    elif desconto is not None and desconto >= 8:
        veredicto = "abaixo do que o roll vale"
    elif desconto is not None and desconto <= -15:
        veredicto = "caro para o roll que tem"
    else:
        veredicto = "preco normal"

    return {
        **j,
        "preco": round(float(preco), 2),
        "justo": round(j["justo"], 2),
        "desconto_pct": round(desconto, 1) if desconto is not None else None,
        "poupanca": round(j["justo"] - preco, 2),
        "scraps": scraps,
        "piso_scraps": round(piso, 2) if piso else None,
        # quando o preco esta abaixo do valor dos scraps, o lucro nao e uma
        # opiniao sobre o roll: e comprar, desfazer e vender os scraps
        "lucro_a_reciclar": round(piso - preco, 2) if (piso and preco < piso) else None,
        "por_scrap": round(float(preco) / scraps, 4) if scraps else None,
        "veredicto": veredicto,
        "qualidade_pct": round((j["qualidade"] or 0) * 100, 0),
    }


def pechinchas(vendas: dict[str, list[tuple[float, dict]]], modelo: dict, itens: dict,
               custos: dict, preco_scrap: float | None,
               taxa: float = config.TAXA_MERCADO_PADRAO,
               minimo_desconto: float = 8.0,
               minimo_poupanca: float = 0.5) -> list[dict]:
    """
    Pecas que mudaram de maos por menos do que o roll delas valia.

    Sem sessao no jogo nao da para ler o que esta a venda agora
    (`itemOffer.getItemOffers` recusa tokens de API), mas da para ler o que se
    vendeu — e isso mostra o tamanho das pechinchas que passam, que e o
    argumento para ir espreitar o mercado.
    """
    fora = []
    vistos = set()
    for code, linhas in vendas.items():
        for linha in linhas:
            preco, skills, _quando = _partes(linha)
            chave = (code, round(preco, 2), tuple(sorted(skills.items())))
            if chave in vistos:
                continue  # a mesma peca vendida em lote aparece varias vezes
            vistos.add(chave)
            a = avaliar(code, skills, preco, modelo, itens, custos, preco_scrap, taxa)
            # a percentagem sozinha enche a lista de facas: 30% de uma peca de
            # 1.50 sao 45 centimos e nao valem o tempo de ninguem
            if (a and a["fiavel"] and (a["desconto_pct"] or 0) >= minimo_desconto
                    and (a["poupanca"] or 0) >= minimo_poupanca):
                fora.append(a)
    return sorted(fora, key=lambda x: x["poupanca"], reverse=True)


def avaliar_ofertas(ofertas: list[dict], modelo: dict, itens: dict, custos: dict,
                    preco_scrap: float | None,
                    taxa: float = config.TAXA_MERCADO_PADRAO) -> list[dict]:
    """
    O mesmo, mas sobre ofertas ao vivo (`itemOffer.getItemOffers`). So funciona
    com cookie de sessao no .env — a API recusa tokens neste endpoint.
    """
    fora = []
    for o in ofertas or []:
        item = o.get("item") or {}
        code = item.get("code") or o.get("itemCode")
        preco = o.get("price") or o.get("money")
        if not code or not preco:
            continue
        a = avaliar(code, item.get("skills") or {}, float(preco), modelo, itens,
                    custos, preco_scrap, taxa)
        if a:
            a["oferta_id"] = o.get("_id")
            a["vendedor"] = o.get("user") or o.get("userId")
            fora.append(a)
    return sorted(fora, key=lambda x: (x["desconto_pct"] is None, -(x["desconto_pct"] or 0)))


# ---------------------------------------------------------------------------
# Dano: para onde vai o proximo euro
# ---------------------------------------------------------------------------

#  AS TRES FUNCOES ABAIXO SAO O MOTOR, E NAO UMA SEGUNDA VERSAO DELE.
#
#  Tinham aqui a formula do dano, a curva da defesa e o transbordo escritos
#  outra vez, com o `config.COMBATE` em vez do RULESET. Eram as ultimas copias
#  que sobravam depois de o JavaScript ter ido embora, e sao a mesma armadilha:
#  duas implementacoes da mesma regra separam-se com o tempo, e foi assim que a
#  esquiva esteve certa num lado e errada no outro durante meses.
#
#  Ficam com estes nomes e estas assinaturas porque metade do modulo as chama
#  assim — mudar isso agora seria uma refatoracao a mais no mesmo passo.


def _v(cfg: dict | None = None) -> dict:
    """Os valores do RULESET. Sem `cfg` sao os de recurso, que e o que estas
    funcoes ja usavam quando liam o `config.COMBATE`."""
    return regras.valores(regras.carregar(cfg or {}))


def _dano(attack: float, chance: float, dano_critico: float,
          precisao: float = 100.0) -> float:
    """Dano medio de um golpe. A conta esta em `motor.dano_por_golpe`."""
    return motor.dano_por_golpe(attack, chance, dano_critico, precisao, _v())


def defesa_efetiva(total: float) -> float:
    """
    Armadura e dodge nao tem tecto: tem rendimento decrescente.
    `efetivo = total / (total + 40)` — 40 de armadura da 50%, 200 da 83%.
    """
    return motor.defesa_efetiva(total, _v()["curva_defesa"])


def teto_do_stat(cfg: dict, nome: str) -> float | None:
    """
    A partir de onde e que o stat deixa de valer o que valia.

    `softCap` esta escrito na config (armor e dodge, 40). Para os stats que
    transbordam (precision, criticalChance) o teto e o valor maximo que a
    propria tabela de skills alcanca — acima disso o excesso converte-se.
    """
    d = (cfg.get("skills") or {}).get(nome) or {}
    if d.get("softCap") is not None:
        return float(d["softCap"])
    if not d.get("skillOverflow"):
        return None
    niveis = d.get("levels") or {}
    valores = [float(v.get("value")) for v in niveis.values()
               if isinstance(v, dict) and isinstance(v.get("value"), (int, float))]
    return max(valores) if valores else None


def _com_transbordo(stats: dict[str, float],
                    cfg: dict) -> tuple[float, float, float, float]:
    """
    (attack, criticalChance, criticalDamages, precision) depois das conversoes
    do jogo: o que passa de 100 de precisao vira ataque a 4 por ponto, e o que
    passa de 100 de critico vira dano critico, tambem a 4 por ponto.

    Os tetos sao 100 nos dois casos e nao o maximo da skill: a precisao da
    skill vai a 100 mas as luvas dao ate +60, e e esse excesso que transborda.

    A conversao esta em `motor.transbordo`, que le os pontos por skill do
    RULESET (`skills.*.skillOverflowValue`) em vez de os reler do `cfg` aqui.
    """
    return motor.transbordo(stats, _v(cfg))


def valor_de_stats(cfg: dict) -> dict[str, dict]:
    """
    As regras que decidem se vale a pena empilhar um stat: teto e conversao.
    Vem de `gameConfig.skills`, nao de suposicao.
    """
    skills = cfg.get("skills") or {}
    fora = {}
    for nome, d in skills.items():
        if not isinstance(d, dict):
            continue
        regra = {}
        if d.get("softCap") is not None:
            regra["teto"] = d["softCap"]
        if d.get("skillOverflow"):
            regra["transborda_para"] = d["skillOverflow"]
            regra["transborda_valor"] = d.get("skillOverflowValue")
        if regra:
            fora[nome] = regra
    return fora


def prioridade_de_dano(stats: dict[str, float], cfg: dict) -> list[dict]:
    """
    Com a build que tens agora, quanto e que rende mais um ponto de cada stat.

    A resposta muda com a build e nao e obvia: com pouco `criticalChance`, mais
    `criticalDamages` quase nao faz nada, e vice-versa. Por isso a conta e a
    derivada no ponto onde estas, e nao uma tabela fixa.
    """
    base = _dano(*_com_transbordo(stats, cfg))
    if base <= 0:
        return []

    linhas = []
    for stat in ("attack", "criticalChance", "criticalDamages", "precision"):
        novos = dict(stats)
        novos[stat] = float(stats.get(stat) or 0) + 1
        ganho = _dano(*_com_transbordo(novos, cfg)) - base
        atual = float(stats.get(stat) or 0)
        nota = ""
        if stat == "precision":
            if atual < config.COMBATE["precisao_max"]:
                falhas = (config.COMBATE["precisao_max"] - atual)
                nota = (f"falhas {falhas:.0f}% dos golpes, e cada falha da metade do "
                        "dano — subir precisao tira falhas")
            else:
                nota = "no tecto: cada ponto vira +4 de ataque"
        elif stat == "criticalChance":
            nota = ("no tecto: cada ponto vira +4 de dano critico"
                    if atual >= config.COMBATE["critico_max"]
                    else f"acerta critico em {atual:.0f}% dos golpes")
        elif stat == "criticalDamages":
            crit = min(float(stats.get("criticalChance") or 0), config.COMBATE["critico_max"])
            nota = f"so conta nos {crit:.0f}% de golpes que saem criticos"
        linhas.append({
            "stat": stat,
            "atual": round(atual, 1),
            "ganho_dano_pct": round(ganho / base * 100, 4),
            "nota": nota,
        })
    return sorted(linhas, key=lambda x: x["ganho_dano_pct"], reverse=True)


def _contribuicao_atual(skills_do_user: dict, stat: str, do_arma: bool) -> float:
    """
    O que a peca que ja tens equipada nesse slot esta a dar neste stat.

    `user.skills.<stat>` separa `value` (skill), `weapon` (a arma) e
    `equipment` (o resto). Sem isto, uma troca de arma aparecia como se a arma
    nova SOMASSE a antiga, o que inflaciona o ganho de forma grosseira.
    """
    obj = (skills_do_user or {}).get(stat) or {}
    v = obj.get("weapon" if do_arma else "equipment")
    return float(v) if isinstance(v, (int, float)) else 0.0


def _multiplicador(skills_do_user: dict, stat: str) -> float:
    """
    Municao, buffs e posto militar entram como percentagem sobre a soma. Sai
    daqui dividindo o total pelo que se somou antes das percentagens.
    """
    obj = (skills_do_user or {}).get(stat) or {}
    base = sum(float(obj.get(k) or 0) for k in ("value", "weapon", "equipment"))
    total = obj.get("total")
    if not base or not isinstance(total, (int, float)) or total <= 0:
        return 1.0
    return float(total) / base


def melhor_peca_por_dinheiro(itens: dict, modelo: dict, precos_eq: dict,
                             stats_jogador: dict, cfg: dict,
                             skills_do_user: dict | None = None) -> list[dict]:
    """
    Por slot, que peca da mais dano por moeda gasta.

    Duas coisas que fazem a diferenca entre um numero util e um numero bonito:

      - a peca nova SUBSTITUI a que la esta, nao soma. Trocar uma arma de 56
        por uma de 55 nao da mais dano nenhum, da menos;
      - a precision so entra no dano acima do teto, e ai vale 4 de attack por
        ponto. Uma peca de precision para quem ja esta no teto vale muito; para
        quem esta abaixo, nao vale nada em dano.

    Usa o roll medio de cada peca (o que sai em media, nao o melhor caso).
    """
    base = _dano(*_com_transbordo(stats_jogador, cfg))
    if base <= 0:
        return []

    por_slot: dict[str, list[dict]] = defaultdict(list)
    for code, cfg_item in itens.items():
        if cfg_item.get("type") not in ("equipment", "weapon"):
            continue
        faixas = intervalos(code, itens)
        if not faixas:
            continue
        medios = {s: (lo + hi) / 2 for s, (lo, hi) in faixas.items()}
        if not any(s in STATS_OFENSIVOS for s in medios):
            continue

        e_arma = cfg_item.get("type") == "weapon"
        novos = dict(stats_jogador)
        for stat, valor in medios.items():
            if stat not in STATS_OFENSIVOS:
                continue
            atual = _contribuicao_atual(skills_do_user or {}, stat, e_arma)
            mult = _multiplicador(skills_do_user or {}, stat)
            novos[stat] = float(stats_jogador.get(stat) or 0) + (valor - atual) * mult

        ganho = _dano(*_com_transbordo(novos, cfg)) - base
        m = modelo.get(code)
        preco = (m["preco_mediano"] if m else None) or precos_eq.get(code)
        if not preco or ganho <= 0:
            continue
        por_slot[cfg_item.get("usage") or cfg_item.get("type")].append({
            "codigo": code,
            "raridade": cfg_item.get("rarity"),
            "stats_medios": {s: round(v, 1) for s, v in medios.items()},
            "preco": round(float(preco), 2),
            "ganho_dano_pct": round(ganho / base * 100, 2),
            "dano_por_moeda": round(ganho / base * 100 / float(preco), 4),
            "fonte_preco": "modelo" if m else "media da API",
        })

    fora = []
    for slot, linhas in por_slot.items():
        melhor = max(linhas, key=lambda x: x["dano_por_moeda"])
        melhor["slot"] = slot
        melhor["alternativas"] = len(linhas)
        fora.append(melhor)
    return sorted(fora, key=lambda x: x["dano_por_moeda"], reverse=True)


def plano_sustentavel(compras: list[dict], custo_hit: dict, dano_semanal: float | None,
                      orcamento: float | None, custos: dict, preco_scrap: float | None,
                      taxa: float = config.TAXA_MERCADO_PADRAO,
                      dano_por_hit: float | None = None) -> dict:
    """
    Mais dano nao chega: interessa o dano que se aguenta pagar.

    A conta que falta a toda a gente e esta. A municao e a comida gastam-se por
    HIT, nao por dano, por isso uma peca que sobe o dano por hit em 17% faz cair
    o custo de cada milhao de dano em 14.5% — para sempre. Com o dano semanal
    real do jogador (`rankings.weeklyUserDamages`) isso vira poupanca por semana,
    e a peca deixa de custar o preco da etiqueta: custa o preco menos os scraps
    que devolve quando parte, e paga-se ao fim de N semanas.

    E o que separa "compra isto que da mais dano" de "compra isto que se paga a
    si proprio em duas semanas e depois e lucro".
    """
    linhas = []
    barata = (custo_hit.get("linhas") or [None])[0]
    custo_por_hit = barata["custo_hit"] if barata else None

    acumulado_custo = 0.0
    acumulado_ganho = 1.0
    for c in compras:
        ganho = (c.get("ganho_dano_pct") or 0) / 100.0
        if ganho <= 0:
            continue
        preco = float(c.get("preco") or 0)
        rec = valor_de_reciclagem(c["codigo"], custos, preco_scrap, taxa)
        residual = rec["partida"] if rec else 0.0
        custo_liquido = preco - residual

        # com mais dano por hit, o mesmo dano total faz-se com menos hits — e a
        # municao e a comida gastam-se por hit. A poupanca por hit e a unica que
        # se sabe sem conhecer o dano por hit do jogador; com esse numero, ela
        # vira dano e semanas.
        fracao = ganho / (1 + ganho)
        poupanca_por_hit = fracao * custo_por_hit if custo_por_hit else None
        hits = custo_liquido / poupanca_por_hit if poupanca_por_hit else None
        dano_para_pagar = hits * dano_por_hit if (hits and dano_por_hit) else None
        semanas = (dano_para_pagar / dano_semanal
                   if (dano_para_pagar and dano_semanal) else None)
        poupanca_semana = None
        if dano_semanal and dano_por_hit and poupanca_por_hit:
            hits_por_semana = dano_semanal / dano_por_hit
            poupanca_semana = hits_por_semana * poupanca_por_hit

        acumulado_custo += custo_liquido
        acumulado_ganho *= (1 + ganho)
        linhas.append({
            **c,
            "valor_residual": round(residual, 2),
            "custo_liquido": round(custo_liquido, 2),
            "poupanca_por_hit": round(poupanca_por_hit, 4) if poupanca_por_hit else None,
            "hits_para_pagar": round(hits, 0) if hits else None,
            "dano_para_pagar": round(dano_para_pagar, 0) if dano_para_pagar else None,
            "poupanca_semanal": round(poupanca_semana, 3) if poupanca_semana else None,
            "semanas_para_pagar": round(semanas, 1) if semanas else None,
            "custo_acumulado": round(acumulado_custo, 2),
            "dano_acumulado_pct": round((acumulado_ganho - 1) * 100, 1),
            "cabe_no_orcamento": (orcamento is None or acumulado_custo <= orcamento),
        })

    dentro = [x for x in linhas if x["cabe_no_orcamento"]]
    return {
        "custo_por_hit": custo_por_hit,
        "municao_mais_barata": barata["municao"] if barata else None,
        "dano_semanal": dano_semanal,
        "dano_por_hit": dano_por_hit,
        "orcamento": orcamento,
        "linhas": linhas,
        "dentro_do_orcamento": len(dentro),
        "custo_total": round(dentro[-1]["custo_acumulado"], 2) if dentro else 0.0,
        "ganho_total_pct": round(dentro[-1]["dano_acumulado_pct"], 1) if dentro else 0.0,
        "poupanca_semanal_total": round(sum(x["poupanca_semanal"] or 0 for x in dentro), 3),
    }


def avisos_plano(plano: dict) -> list[str]:
    linhas = plano.get("linhas") or []
    if not linhas:
        return []
    fora = []
    dentro = [x for x in linhas if x["cabe_no_orcamento"]]
    if plano.get("orcamento") is not None:
        fora.append(
            f"Com {plano['orcamento']:.2f} disponiveis, cabem {len(dentro)} melhoria(s): "
            f"{plano['custo_total']:.2f} de custo liquido (ja tirando os scraps que as pecas "
            f"devolvem quando partem) para +{plano['ganho_total_pct']:.1f}% de dano."
        )
    melhor = min((x for x in linhas if x.get("hits_para_pagar")),
                 key=lambda x: x["hits_para_pagar"], default=None)
    if melhor:
        fora.append(
            f"A que se paga mais depressa e {melhor['codigo']}: custa "
            f"{melhor['custo_liquido']:.2f} liquidos (preco menos os scraps que devolve "
            f"quando partir) e poupa {melhor['poupanca_por_hit']:.4f} em municao e comida "
            f"por cada hit, porque o mesmo dano passa a precisar de menos hits. "
            f"Fica paga ao fim de {melhor['hits_para_pagar']:,.0f} hits — a partir dai o "
            "dano extra sai de graca."
        )
        if melhor.get("semanas_para_pagar"):
            fora.append(
                f"Ao ritmo a que ja andas ({plano['dano_semanal']:,.0f} de dano por semana, "
                f"{plano['dano_por_hit']:,.0f} por hit), isso sao "
                f"{melhor['semanas_para_pagar']:.1f} semana(s) e "
                f"{melhor['poupanca_semanal']:.2f} poupados por semana."
            )
        else:
            fora.append(
                "Para converter os hits em semanas falta o teu dano por hit — e o numero "
                "que o jogo mostra no ecra de batalha, e mete-se no separador Guerra."
            )
        fora.append(
            "E assim que se sobe dano de forma sustentavel: a municao e a comida gastam-se "
            "por hit e nao por dano, por isso mais dano por hit e menos gasto pelo mesmo "
            "dano total. Quem bate pouco demora mais a amortizar a mesma peca."
        )
    return fora


def avisos(modelo: dict, pechinchas_: list[dict], prioridade: list[dict] | None = None,
           ofertas: list[dict] | None = None, estatisticas: list[dict] | None = None) -> list[str]:
    fora = []
    if not modelo:
        return ["Sem vendas de equipamento suficientes para ligar preco a stats."]

    fiaveis = {c: m for c, m in modelo.items() if m["fiavel"]}
    if fiaveis:
        c, m = max(fiaveis.items(), key=lambda x: x[1]["preco_no_maximo"] - x[1]["preco_no_minimo"])
        fora.append(
            f"O mercado paga o roll: em {c}, {m['n']} vendas dao "
            f"{m['declive']:.2f} por ponto de stat (correlacao {m['correlacao']:+.2f}). "
            f"Um roll no minimo vale {m['preco_no_minimo']:.2f} e um no maximo "
            f"{m['preco_no_maximo']:.2f} — a mesma peca, "
            f"{(m['preco_no_maximo'] / m['preco_no_minimo'] - 1) * 100:.0f}% de diferenca."
        )
        fora.append(
            f"Modelo de preco por roll em {len(fiaveis)} codigos (dos {len(modelo)} com "
            "vendas). Nos outros a amostra nao chega e o painel nao arrisca um preco justo."
        )

    if ofertas:
        boas = [o for o in ofertas if (o.get("desconto_pct") or 0) >= 8]
        if boas:
            o = boas[0]
            fora.append(
                f"A venda agora: {o['codigo']} com "
                + ", ".join(f"{k} {v:g}" for k, v in o["stats"].items())
                + f" (roll no percentil {o['qualidade_pct']:.0f}) por {o['preco']:.2f}. "
                f"Pelo roll devia valer {o['justo']:.2f} — {o['desconto_pct']:.0f}% abaixo, "
                f"{o['poupanca']:.2f} de margem."
            )
            fora.append(f"Sao {len(boas)} oferta(s) abaixo do valor do roll neste momento.")
        else:
            fora.append("Nenhuma oferta a venda esta abaixo do valor do roll neste momento.")
    elif pechinchas_:
        p = pechinchas_[0]
        total = sum(x["poupanca"] for x in pechinchas_)
        fora.append(
            f"Pechinchas passam: vendeu-se um {p['codigo']} com "
            + ", ".join(f"{k} {v:g}" for k, v in p["stats"].items())
            + f" por {p['preco']:.2f} quando o roll dele valia {p['justo']:.2f} "
            f"({p['desconto_pct']:.0f}% abaixo, {p['poupanca']:.2f} de margem). "
            f"Foram {len(pechinchas_)} na amostra, {total:.2f} de margem no total."
        )
        fora.append(
            "Para ver o que esta a venda AGORA e preciso cookie de sessao no .env "
            "(WARERA_JWT): itemOffer.getItemOffers recusa tokens de API."
        )

    if estatisticas:
        reciclaveis = [e for e in estatisticas if e.get("lucro_a_reciclar")]
        if reciclaveis:
            e = reciclaveis[0]
            fora.append(
                f"Vendeu-se {e['codigo']} por {e['minimo']:.2f} quando os {e['scraps']} scraps "
                f"que ele contem valem {e['reciclar']:.2f} ao preco de agora: "
                f"{e['lucro_a_reciclar']:.2f} de lucro so em comprar e desfazer. "
                f"Sao {len(reciclaveis)} codigo(s) com vendas abaixo do valor dos scraps."
            )
        maior = max(estatisticas, key=lambda e: e.get("amplitude_pct") or 0)
        if maior.get("amplitude_pct"):
            fora.append(
                f"O preco da mesma peca varia muito: {maior['codigo']} vendeu-se entre "
                f"{maior['minimo']:.2f} e {maior['maximo']:.2f} "
                f"(+{maior['amplitude_pct']:.0f}%, media {maior['media']:.2f}, "
                f"{maior['n']} vendas). Pagar a media quando o minimo anda por ali e "
                "deitar dinheiro fora."
            )

    if prioridade:
        p = prioridade[0]
        fora.append(
            f"Com a build que tens, o ponto que mais rende e {p['stat']} "
            f"(+{p['ganho_dano_pct']:.3f}% de dano por ponto)."
        )
        tetos = [x for x in prioridade if x.get("regra", {}).get("teto")]
        if tetos:
            fora.append(
                "Cuidado com os tetos: "
                + ", ".join(f"{x['stat']} para nos {x['regra']['teto']}" for x in tetos)
                + ". Acima disso o stat nao faz nada."
            )
    return fora
