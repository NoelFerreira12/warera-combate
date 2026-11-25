"""
V3.2 — o plano ate esvaziar as barras.

Toda a vida e toda a fome que estao nas barras viram golpes, e os golpes
dividem-se pelas rondas abertas da forma que da MAIS lucro no pior cenario.
O que se mostra por ronda e o dano.

As contas sao as do `optimizador.planear`, sem tirar nem por (tests/test_combate.py:
as tabelas e a divisao conferidas contra o planear e contra a forca bruta), com uma
diferenca (V3.3b): onde ja tens dano, os itens sao so o que o dano novo acrescenta.
    d      = g x (dpg1 x factor do lado x multiplicador da pilula)
    itens  = soma, por pool, do valor da peca no rank que esse dano da (no teu total),
             menos o da peca que o dano ja feito te garante sem bater mais
    bounty = min(d / 1000 x taxa, fundo)            (so nos lados que pagam)
    lucro  = itens + g x cases_por_golpe + bounty - g x custo_por_golpe
    pior   = o menor lucro dos tres cenarios

Porque e rapido sem deixar de ser exaustivo:
  - itens e bounty so dependem do dano: calculam-se uma vez por ronda, lado e
    dpg1 (`tabelas`), nao por conjunto;
  - com TODOS os golpes gastos (soma dos g = H), o que custa o mesmo em todos os
    golpes — a comida, o desgaste, as cases — soma H x o mesmo em qualquer
    divisao: nao muda a divisao. So a metade de dano (arma, luvas, capacete:
    o dpg1) e o preco da municao a mudam. A divisao faz-se por metade de dano
    (64, nao 4096) e para todos os H de uma vez (`dividir`);
  - cada conjunto (metade de dano x metade de defesa x comida) le a divisao
    para os SEUS golpes H e soma as contas exactas, ronda a ronda.
  Por isso vida e fome que mudam nao pedem calculo nenhum (so outra leitura),
  e precos de comida e cases tambem nao (so deslocam). So a municao refaz a divisao.

V3.3 (pedido do jogador, 24/11): as barras ja nao se gastam todas a forca. A
divisao da o melhor valor para TODOS os h; cada conjunto e comida bate os h ate
onde o lucro sobe (`picos`) e o resto fica para outras oportunidades — contado em
golpes, sem lhe inventar um valor. Assim o peixe (mais vida por ponto de fome) deixa
de ser castigado por golpes que hoje dao prejuizo. E, com os mesmos golpes, o plano
poe o mais dano possivel nos lados com ordem do teu pais / MU sem passar de uma perda
maxima (`preferir_ordens`, exacto: a fronteira lucro x dano com ordem).
"""
from __future__ import annotations

import sys

try:
    import numpy as np
except ModuleNotFoundError:   # `python -I` nao le o site-packages do utilizador, que e onde o numpy esta
    import site
    sys.path.append(site.getusersitepackages())
    try:
        import numpy as np
    except ModuleNotFoundError:
        raise SystemExit("o assistente de combate precisa do numpy (pip install numpy); "
                         "o painel continua sem dependencias") from None

from warera import bounty as B, optimizador as O

from warera.combate import motor as M3

NEG = -1e300


# ---------------------------------------------------------------------------
#  1. por ronda (e janela): o que o dano rende, sem o conjunto
# ---------------------------------------------------------------------------

def _pool(x, val):
    """Os projectados por ordem crescente, o valor da peca em cada rank (1..n+1) e o dano que ja tens neste pool."""
    pa = np.sort(-np.asarray(x["neg"], dtype=float))
    n = len(pa)
    v = np.array([O.valor_na_posicao(r, x["cortes"], x["blocos"], val)[1] for r in range(1, n + 2)], dtype=float)
    return pa, v, float(x.get("ja") or 0.0)


def _itens(D, pools, a_mais=False):
    """
    `sum(_resultado(d)["valor"])` para uma matriz de danos (a mesma ordem de soma: ronda, depois batalha).
    `a_mais` (V3.3b): so o que o dano NOVO acrescenta. Com dano ja feito num pool, a peca que esse dano
    ja te da no fim (o rank de d = 0) e tua sem bater mais: desconta-se. Sem dano feito, sem bater nao
    ha peca — nao se desconta nada.
    """
    tot = np.zeros_like(D)
    for pa, v, ja in pools:
        n = len(pa)
        # rank = 1 + quantos acabam com pelo menos d  (== 1 + bisect_right(neg, -d) do projecto)
        rank = 1 + n - np.searchsorted(pa, D, side="left")
        tot = tot + v[rank - 1]
        if a_mais and ja > 0:
            tot = tot - v[n - int(np.searchsorted(pa, 0.0, side="left"))]
    return tot


def ja_garantido(ctx):
    """
    O valor da peca que o dano que ja fizeste te da no fim da ronda, sem bater mais: no pior cenario
    (o de menos valor) e no esperado. 0 onde nao tens dano.
    """
    val = ctx["val"]
    fora = {}
    for nome, c in ctx["contextos"].items():
        if nome in O.CENARIOS:
            pools = [_pool(c[p], val) for p in O.POOLS if c.get(p)]
            fora[nome] = float(sum(v[n - int(np.searchsorted(pa, 0.0, side="left"))] for pa, v, ja in pools
                                   if ja > 0 for n in [len(pa)]))
    pior = min(fora.values()) if fora else 0.0
    return {"pior": pior, "esperado": fora.get("esperado", pior)}


def tabelas(ctx, dpg1s, hmax):
    """
    ctx: {"contextos", "lados", "val", "buff"} (uma ronda numa janela).
    dpg1s: os dpg1 distintos (um por metade de dano e municao).
    Por lado permitido, para g = 0..hmax: itens no pior cenario (W), no
    esperado (Ev) e bounty (BT). O dano e g x dpg[u].
    """
    val, mult = ctx["val"], float(ctx["buff"])
    pools = {}
    for nome, c in ctx["contextos"].items():
        if nome in O.CENARIOS:
            pools[nome] = [_pool(c[p], val) for p in O.POOLS if c.get(p)]
    g = np.arange(hmax + 1, dtype=float)
    lados = []
    for l in ctx["lados"]:
        if not l.get("permitido", True):
            continue
        dpg = np.asarray(dpg1s, dtype=float) * float(l["fator"]) * mult   # (dpg1 x factor) x buff, como no planear
        D = g[None, :] * dpg[:, None]
        # V3.3b: so o que o dano novo ACRESCENTA a peca que o dano ja feito te garante (em cada cenario;
        # o pior e o de menor acrescimo). Antes contava a peca inteira: 1 golpe numa ronda onde ja tinhas
        # a peca parecia render o valor dela.
        W = None
        for nome in O.CENARIOS:
            if nome in pools:
                it = _itens(D, pools[nome], a_mais=True)
                W = it if W is None else np.minimum(W, it)
        Ev = _itens(D, pools["esperado"], a_mais=True) if "esperado" in pools else W
        if l.get("paga"):
            b = D / 1000.0 * float(l.get("taxa") or 0.0)
            BT = np.minimum(b, float(l["fundo"])) if l.get("fundo") is not None else b
        else:
            BT = np.zeros_like(D)
        # V3.3c: o contrato de mercenario da tua MU, MEDIDO no que recebeste (sem tecto conhecido: o orcamento
        # do contrato nao e publico). Entra nas contas junto com o bounty (BT); CT so para o mostrar.
        c1k = float(l.get("contrato_1k") or 0.0)
        CT = D / 1000.0 * c1k if c1k > 0 else np.zeros_like(D)
        BT = BT + CT
        lados.append({"lado": l["lado"], "pais": l.get("pais"), "contra": l.get("contra"), "fator": l["fator"],
                      "taxa": l.get("taxa"), "fundo": l.get("fundo"), "estado": l.get("estado"),
                      "paga": bool(l.get("paga")), "fundo_estimado": bool(l.get("fundo_estimado")),
                      # V3.3: as ordens do teu pais / da tua MU neste lado (so para a preferencia; o bonus ja esta no factor)
                      "ordens": list(l.get("ordem") or []),
                      "contrato_1k": c1k, "contrato": l.get("contrato"),
                      "dpg": dpg, "W": W, "Ev": Ev, "BT": BT, "CT": CT})
    return {"lados": lados, "hmax": hmax, "mult": mult}


# ---------------------------------------------------------------------------
#  2. os conjuntos: metades de dano, golpes das barras, custos
# ---------------------------------------------------------------------------

def preparar(grupos):
    """
    Uma vez por base. Os indices de cada conjunto: a metade de dano, o dpg1 e o
    custo de cada (municao, comida), e o que a vida e a fome dao em golpes.
    """
    munis = list(M3.MUNICOES) + [None]
    comidas = sorted({b["comida"] for bs in grupos.values() for b in bs})
    ks = list(grupos)
    S, M, C = len(ks), len(munis), len(comidas)
    dpg1s = sorted({b["dpg1"] for bs in grupos.values() for b in bs})
    uidx = {d: i for i, d in enumerate(dpg1s)}
    metades = {}
    d_de = np.zeros(S, dtype=np.int64)
    u_m = []          # por metade: indice do dpg1 de cada municao (-1 se nao ha)
    c_m = np.zeros(M)  # preco da municao por golpe (o mesmo em todas as metades)
    custo = np.full((S, M, C), np.nan)
    vg = np.zeros((S, C))
    vgc = np.zeros((S, C))
    vc = np.zeros((S, C))
    for i, k in enumerate(ks):
        b0 = grupos[k][0]
        dk = tuple(M3.chave_peca(b0["pecas"][s]) for s in B.GRUPO_DANO)
        if dk not in metades:
            metades[dk] = len(metades)
            u_m.append([-1] * M)
        di = metades[dk]
        d_de[i] = di
        for b in grupos[k]:
            mi, ci = munis.index(b["municao"]), comidas.index(b["comida"])
            u_m[di][mi] = uidx[b["dpg1"]]
            c_m[mi] = b["_c_mun"]
            custo[i, mi, ci] = b["custo_golpe"]
            vg[i, ci], vgc[i, ci], vc[i, ci] = b["vida_golpe"], b["_vida_golpe_cheio"], b["vida_comida"]
    return {"ks": ks, "munis": munis, "comidas": comidas, "dpg1s": dpg1s, "d_de": d_de,
            "u_m": np.array(u_m, dtype=np.int64), "c_m": c_m, "custo": custo, "vg": vg, "vgc": vgc, "vc": vc,
            "capex": np.array([grupos[k][0]["capex"] for k in ks]), "n_metades": len(metades)}


def golpes(P, vida, fome):
    """Os golpes esperados que as barras dao, por conjunto e comida (as regras de `motor3.golpes_com`)."""
    S, C = P["vg"].shape
    H = np.zeros((S, C), dtype=np.int64)
    for i in range(S):
        for c in range(C):
            if P["vgc"][i, c] > 0:
                H[i, c] = M3.golpes_com(vida, fome, P["vg"][i, c], P["vgc"][i, c], P["vc"][i, c])[1]
    return H


# ---------------------------------------------------------------------------
#  3. a divisao pelas rondas (programacao dinamica, por metade de dano)
# ---------------------------------------------------------------------------

def _f_metades(tab, u_m, c_m, hmax, so=None):
    """
    Para cada metade de dano e g: o melhor (itens no pior cenario + bounty - g x
    preco da municao) sobre lados e municoes, e qual lado e municao. (O resto do
    custo e as cases sao iguais em todos os golpes: entram depois.)
    """
    dm = u_m if so is None else u_m[so]
    Dn, M = dm.shape
    g = np.arange(hmax + 1, dtype=float)
    best = np.full((Dn, hmax + 1), NEG)
    arg_l = np.zeros((Dn, hmax + 1), dtype=np.int8)
    arg_m = np.zeros((Dn, hmax + 1), dtype=np.int8)
    for li, l in enumerate(tab["lados"]):
        for mi in range(M):
            u = dm[:, mi]
            tem = u >= 0
            uu = np.where(tem, u, 0)
            ok = tem & (l["dpg"][uu] > 0)
            f = (l["W"][uu, :hmax + 1] + l["BT"][uu, :hmax + 1]) - g[None, :] * c_m[mi]
            f[~ok] = NEG
            melhor = f > best
            best = np.where(melhor, f, best)
            arg_l = np.where(melhor, li, arg_l).astype(np.int8)
            arg_m = np.where(melhor, mi, arg_m).astype(np.int8)
    best[:, 0] = 0.0
    return best, arg_l, arg_m


def dividir(fs, hmax):
    """
    fs: [f_r (Dn, hmax+1)] pela ordem das rondas. Para TODOS os h = 0..hmax: o
    melhor valor com exactamente h golpes nas rondas, e as escolhas para voltar
    atras (args[r][d, h] = golpes na ronda r).
    """
    Dn = fs[0].shape[0] if fs else 0
    dp = np.full((Dn, hmax + 1), NEG)
    dp[:, 0] = 0.0
    args = []
    for f in fs:
        new = dp + f[:, :1]
        arg = np.zeros((Dn, hmax + 1), dtype=np.int16)
        for g in range(1, hmax + 1):
            cand = dp[:, :hmax + 1 - g] + f[:, g:g + 1]
            sub = new[:, g:]
            melhor = cand > sub
            if melhor.any():
                new[:, g:] = np.where(melhor, cand, sub)
                arg[:, g:] = np.where(melhor, g, arg[:, g:])
        dp = new
        args.append(arg)
    return dp, args


def voltar(args, d, H):
    """Os golpes por ronda para a metade d e h = H (vectorizado: d e H sao vectores)."""
    R = len(args)
    gs = np.zeros((len(d), R), dtype=np.int64)
    h = np.asarray(H, dtype=np.int64).copy()
    for r in range(R - 1, -1, -1):
        g = args[r][d, h].astype(np.int64)
        gs[:, r] = g
        h = h - g
    return gs


def construir(tabs, P, hmax, cancelar=None):
    """A divisao para todas as metades de dano e todos os h (o trabalho pesado; uma vez por rondas e precos de municao)."""
    fs, als, ams = [], [], []
    for tab in tabs:
        f, al, am = _f_metades(tab, P["u_m"], P["c_m"], hmax)
        fs.append(f)
        als.append(al)
        ams.append(am)
        if cancelar and cancelar():
            return None
    if not tabs:
        return {"dp": None, "args": [], "als": [], "ams": [], "hmax": hmax}
    dp, args = dividir(fs, hmax)
    return {"dp": dp, "args": args, "als": als, "ams": ams, "hmax": hmax, "fs": fs}


# ---------------------------------------------------------------------------
#  4. cada conjunto: as contas exactas da sua divisao
# ---------------------------------------------------------------------------

def avaliar(tabs, P, DV, H, caso, so_comida=None):
    """
    Para todos os conjuntos e comidas: a divisao para os seus H golpes e as
    contas exactas (a ordem do planear, ronda a ronda). Fica, por conjunto, a
    comida de maior lucro no pior cenario — ou so `so_comida` (indice), se o jogador
    a escolheu. Devolve arrays por conjunto.
    """
    S, C = H.shape
    R = len(tabs)
    d_de = P["d_de"]
    melhor = None
    for ci in (range(C) if so_comida is None else [so_comida]):
        h = np.minimum(H[:, ci], DV["hmax"])
        gs = voltar(DV["args"], d_de, h) if R else np.zeros((S, 0), dtype=np.int64)
        tot = {k: np.zeros(S) for k in ("lucro", "esperado", "dano", "itens", "bounty", "cases", "custo", "contrato")}
        lado = np.zeros((S, R), dtype=np.int64)
        mun = np.zeros((S, R), dtype=np.int64)
        por_r = np.zeros((S, R, 3))       # dano, lucro, bounty por ronda
        for r, tab in enumerate(tabs):
            g = gs[:, r]
            li = DV["als"][r][d_de, g].astype(np.int64)
            mi = DV["ams"][r][d_de, g].astype(np.int64)
            lado[:, r], mun[:, r] = li, mi
            u = P["u_m"][d_de, mi]
            uu = np.where(u >= 0, u, 0)
            Wv, Ev, BTv, Dv, CTv = (np.zeros(S) for _ in range(5))
            for k, l in enumerate(tab["lados"]):
                sel = li == k
                if sel.any():
                    Wv[sel] = l["W"][uu[sel], g[sel]]
                    Ev[sel] = l["Ev"][uu[sel], g[sel]]
                    BTv[sel] = l["BT"][uu[sel], g[sel]]
                    Dv[sel] = g[sel] * l["dpg"][uu[sel]]
                    if "CT" in l:
                        CTv[sel] = l["CT"][uu[sel], g[sel]]
            gf = g.astype(float)
            cs = gf * caso
            cu = gf * P["custo"][np.arange(S), mi, ci]
            zero = g == 0
            lucro = np.where(zero, 0.0, ((Wv + cs) + BTv) - cu)
            esper = np.where(zero, 0.0, ((Ev + cs) + BTv) - cu)
            for k, v in (("lucro", lucro), ("esperado", esper), ("dano", np.where(zero, 0.0, Dv)),
                         ("itens", np.where(zero, 0.0, Wv)), ("bounty", np.where(zero, 0.0, BTv)),
                         ("cases", cs), ("custo", np.where(zero, 0.0, cu)), ("contrato", np.where(zero, 0.0, CTv))):
                tot[k] = tot[k] + v
            por_r[:, r, 0], por_r[:, r, 1], por_r[:, r, 2] = np.where(zero, 0.0, Dv), lucro, np.where(zero, 0.0, BTv)
        res = {"comida": np.full(S, ci), "H": h, "g": gs, "lado": lado, "mun": mun, "por_r": por_r, **tot}
        if melhor is None:
            melhor = res
        else:
            troca = res["lucro"] > melhor["lucro"] + 1e-12
            for k in melhor:
                v = melhor[k]
                sel = troca.reshape((-1,) + (1,) * (v.ndim - 1))
                melhor[k] = np.where(sel, res[k], v)
    return melhor


def plano_unico(ctxs, P, i, ci, H, caso, pico=False):
    """
    O plano de UM conjunto e comida, com H golpes, noutras rondas ou noutra janela
    (ex.: as mesmas barras gastas na ressaca). Tabelas so para os dpg1 dele.
    `pico`: bate so ate onde o lucro sobe (como o plano de agora), com H como tecto.
    """
    d = int(P["d_de"][i])
    um = P["u_m"][d]
    us = sorted({int(u) for u in um if u >= 0})
    loc = {u: j for j, u in enumerate(us)}
    Pi = {"u_m": np.array([[loc[int(u)] if u >= 0 else -1 for u in um]], dtype=np.int64), "c_m": P["c_m"],
          "d_de": np.zeros(1, dtype=np.int64), "custo": P["custo"][i:i + 1, :, ci:ci + 1]}
    tabs = [tabelas(c, [P["dpg1s"][u] for u in us], H) for c in ctxs]
    DV = construir(tabs, Pi, H)
    Hm = np.array([[H]], dtype=np.int64)
    if pico:
        Hm = picos(DV, Pi, Hm, caso)
    return {"res": avaliar(tabs, Pi, DV, Hm, caso), "tabs": tabs, "P": Pi}


# ---------------------------------------------------------------------------
#  5. V3.3: bater so ate onde o lucro sobe (o resto das barras guarda-se)
# ---------------------------------------------------------------------------

def custo_base(P):
    """(S, C): o custo por golpe sem a municao (comida + desgaste), o mesmo em todas as municoes do conjunto."""
    x = P["custo"] - P["c_m"][None, :, None]
    fora = np.full(x.shape[::2], np.nan)
    tem = ~np.isnan(x).all(axis=1)
    fora[tem] = np.nanmin(np.where(np.isnan(x), np.inf, x), axis=1)[tem]
    return fora


def picos(DV, P, H, caso):
    """
    Os golpes de cada conjunto e comida ate onde o lucro sobe: o h <= H (as barras)
    de maior valor(h) = divisao[metade, h] + h x (cases - custo sem a municao). Com
    empate fica o MENOR h — o resto das barras fica para outras oportunidades.
    E a mesma conta de `parar_em`, para todos os conjuntos de uma vez.
    """
    S, C = H.shape
    if DV.get("dp") is None:
        return np.zeros_like(H)
    hmax = DV["hmax"]
    dp = DV["dp"][P["d_de"]]
    base = custo_base(P)
    h = np.arange(hmax + 1)
    fora = np.zeros_like(H)
    for c in range(C):
        k = caso - base[:, c]
        with np.errstate(invalid="ignore"):
            val = dp + h[None, :] * k[:, None]
        val = np.where((h[None, :] <= np.minimum(H[:, c], hmax)[:, None]) & ~np.isnan(val), val, NEG)
        mx = val.max(axis=1)
        fora[:, c] = np.argmax(val >= mx[:, None] - 1e-9, axis=1)
    return fora


def dano_com_ordem(tabs, res, j=0):
    """O dano do plano (linha j) nos lados onde ha ordem do teu pais ou da tua MU."""
    tot = 0.0
    for r, tab in enumerate(tabs):
        if res["g"][j, r] > 0 and tab["lados"][int(res["lado"][j, r])].get("ordens"):
            tot += float(res["por_r"][j, r, 0])
    return tot


def _fronteira(P, O):
    """Os pontos (lucro, dano com ordem) que nenhum outro bate nos dois: indices, por lucro a descer."""
    o = np.lexsort((-O, -P))
    Os = O[o]
    antes = np.concatenate(([-np.inf], np.maximum.accumulate(Os)[:-1]))
    return o[Os > antes + 1e-9]


def preferir_ordens(tabs, P, i, ci, h, caso, tolerancia):
    """
    Os MESMOS h golpes de um conjunto, com o MAIS dano possivel onde ha ordem do
    teu pais ou da tua MU, sem perder mais do que `tolerancia` (fraccao) do lucro.

    Exacto (nao e um peso a afinar): as rondas sem ordem dividem-se pela
    programacao dinamica de sempre (o melhor lucro para cada numero de golpes);
    as rondas com ordem juntam-se por cima, guardando para cada numero de golpes
    a fronteira inteira (lucro, dano com ordem) — lado, municao e golpes de cada
    uma. No fim fica, entre os planos que cabem no limite, o de mais dano com
    ordem (empate: o de mais lucro). Uma ronda de grande valor sem ordem so se
    larga se a perda couber no limite.
    None: sem ordens nas rondas, tolerancia 0, ou nada muda.
    """
    R = len(tabs)
    tem = [any(l.get("ordens") for l in t["lados"]) for t in tabs]
    if h <= 0 or tolerancia <= 0 or not any(tem):
        return None
    d = int(P["d_de"][i])
    um, c_m = P["u_m"][d], P["c_m"]
    Pi = {"u_m": P["u_m"][d:d + 1], "c_m": c_m, "d_de": np.zeros(1, dtype=np.int64), "custo": P["custo"][i:i + 1]}
    g = np.arange(h + 1, dtype=float)

    # 1. as rondas sem ordem: a divisao de sempre (o melhor lucro para cada h')
    livres = [r for r in range(R) if not tem[r]]
    fl = [_f_metades(tabs[r], Pi["u_m"], c_m, h) for r in livres]
    if livres:
        dpB, argsB = dividir([f[0] for f in fl], h)
        dpB = dpB[0]
    else:
        dpB, argsB = np.full(h + 1, NEG), []
        dpB[0] = 0.0

    # 2. as rondas com ordem, por cima: para cada h, a fronteira (lucro, dano com ordem)
    fr = {H: (np.array([dpB[H]]), np.array([0.0]), [()]) for H in range(h + 1) if dpB[H] > NEG / 2}
    for r in (r for r in range(R) if tem[r]):
        ops = {0: [(0.0, 0.0, None, None)]}          # g -> [(lucro, dano com ordem, lado, municao)]
        for gg in range(1, h + 1):
            ops[gg] = []
        sem = {}                                    # o melhor lado sem ordem, por g
        for li, l in enumerate(tabs[r]["lados"]):
            for mi, u in enumerate(um):
                if u < 0 or l["dpg"][u] <= 0:
                    continue
                p = (l["W"][u, :h + 1] + l["BT"][u, :h + 1]) - g * c_m[mi]
                dano = g * l["dpg"][u]
                for gg in range(1, h + 1):
                    if l.get("ordens"):
                        ops[gg].append((float(p[gg]), float(dano[gg]), li, mi))
                    elif gg not in sem or p[gg] > sem[gg][0]:
                        sem[gg] = (float(p[gg]), 0.0, li, mi)
        for gg, x in sem.items():
            ops[gg].append(x)
        novo = {}
        for H in range(h + 1):
            Ps, Os, bk = [], [], []
            for gg in range(H + 1):
                a = fr.get(H - gg)
                if a is None or not ops[gg]:
                    continue
                for (p, o, li, mi) in ops[gg]:
                    Ps.append(a[0] + p)
                    Os.append(a[1] + o)
                    bk.extend(b + ((r, gg, li, mi),) for b in a[2])
            if not Ps:
                continue
            Pc, Oc = np.concatenate(Ps), np.concatenate(Os)
            k = _fronteira(Pc, Oc)
            novo[H] = (Pc[k], Oc[k], [bk[j] for j in k])
        fr = novo
    if h not in fr:
        return None
    Pf, Of, Bf = fr[h]
    topo = int(np.argmax(Pf))                       # o plano de lucro (a fronteira so guarda o de mais dano com ordem)
    p0, o0 = float(Pf[topo]), float(Of[topo])
    const = h * (caso - float(custo_base({"custo": P["custo"][i:i + 1], "c_m": c_m})[0, ci]))
    l0 = p0 + const
    cabe = Pf + const >= l0 - tolerancia * max(l0, 0.0) - 1e-9
    j = max(np.flatnonzero(cabe), key=lambda j: (Of[j], Pf[j]))
    if Of[j] <= o0 + 1e-6:
        return None

    # 3. o plano escolhido: golpes, lado e municao de cada ronda -> as contas exactas de `avaliar`
    gs, lado, mun = [0] * R, [0] * R, [0] * R
    for (r, gg, li, mi) in Bf[j]:
        gs[r], lado[r], mun[r] = gg, (li or 0), (mi or 0)
    hB = h - sum(gs)
    if livres:
        gB = voltar(argsB, np.zeros(1, dtype=np.int64), [hB])[0]
        for k, r in enumerate(livres):
            gs[r] = int(gB[k])
            lado[r], mun[r] = int(fl[k][1][0, gs[r]]), int(fl[k][2][0, gs[r]])
    args, als, ams, acum = [], [], [], 0
    for r in range(R):
        acum += gs[r]
        a = np.zeros((1, h + 1), dtype=np.int16)
        a[0, acum] = gs[r]
        args.append(a)
        als.append(np.full((1, h + 1), lado[r], dtype=np.int8))
        ams.append(np.full((1, h + 1), mun[r], dtype=np.int8))
    Hm = np.zeros((1, P["custo"].shape[2]), dtype=np.int64)
    Hm[0, ci] = h
    x = avaliar(tabs, Pi, {"dp": None, "args": args, "als": als, "ams": ams, "hmax": h}, Hm, caso, so_comida=ci)
    l, o = float(x["lucro"][0]), dano_com_ordem(tabs, x)
    return {"res": x, "lucro": l, "dano_ordem": o, "lucro_sem": l0, "dano_ordem_sem": o0, "perda": l0 - l}


def parar_em(DV, P, i, ci, H, caso):
    """
    Para um conjunto: o melhor lucro com h golpes, para cada h <= H (a parte que
    custa o mesmo em todos os golpes soma-se aqui). Diz onde o lucro deixava de
    subir — o resto das barras e o que custa gasta-lo.
    """
    if DV["dp"] is None or H <= 0:
        return None
    d = int(P["d_de"][i])
    mi_ref = None
    h = np.arange(H + 1)
    # custo por golpe sem a municao (comida + desgaste): o mesmo em todas as municoes do conjunto
    cs = P["custo"][i, :, ci]
    base = np.nanmin(cs - P["c_m"])
    val = DV["dp"][d, :H + 1] + h * caso - h * base
    hm = int(np.argmax(val))
    return {"h": hm, "valor": float(val[hm]), "valor_H": float(val[H])}
