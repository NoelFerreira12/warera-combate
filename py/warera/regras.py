"""
As regras de combate do WarEra, num sitio so, com a proveniencia de cada uma.

Este ficheiro existe por uma razao: antes dele, uma regra do jogo vivia em tres
sitios ao mesmo tempo — uma constante no `config.py`, uma leitura do
`gameConfig` algures noutro modulo, e uma copia em JavaScript dentro do
`painel.html`. Quando o WarEra mudava um numero, havia tres sitios para
actualizar e nenhum deles sabia dos outros. Foi assim que o bonus patriotico
esteve a +10% depois de passar a +20%, e que a esquiva esteve anos a nao contar.

Agora ha um `RULESET`: um dicionario construido uma vez a partir do
`gameConfig` do jogo, com o que o jogo nao publica escrito por cima e MARCADO
como tal. Toda a gente le daqui.

A distincao que este modulo protege:

    fonte="config"      o jogo diz. Muda sozinho quando o jogo mudar.
    fonte="wiki"        a wiki documenta, o gameConfig nao publica.
    fonte="comunidade"  alguem mediu. Pode estar errado e vai marcado.

Um numero sem fonte nao entra aqui.
"""

from __future__ import annotations

from . import config

# ---------------------------------------------------------------------------
#  A versao do jogo a que estas regras correspondem.
#
#  Nao e decoracao: sem isto nao ha maneira de saber se uma conta guardada foi
#  feita com as regras de hoje ou com as de ha dois meses. A v0.25.5 mudou o
#  bonus patriotico e a curva da alianca, e qualquer numero calculado antes de
#  1 de Setembro de 2026 esta errado sem se notar.
# ---------------------------------------------------------------------------
VERSAO = {
    "versao": "v0.25.5-beta",
    "data_efetiva": "2026-08-29",
    "alianca_em_vigor_desde": "2026-09-01",
    "fonte": "https://warera.io/en/articles/v0-25-5-4a2c5d",
    "verificado_em": "2026-09-11",
    "nota": ("reconfirmado como a versao mais recente em 2026-09-11: nao ha "
             "artigo de patch notes posterior a v0.25.5 em warera.io. A wiki de "
             "patch notes da comunidade esta parada em Outubro de 2025 e nao "
             "serve para isto"),
}


def _n(d: dict, caminho: str, omissao):
    """Le `a.b.c` de um dicionario aninhado, com recurso se faltar."""
    no = d or {}
    for parte in caminho.split("."):
        if not isinstance(no, dict) or parte not in no:
            return omissao
        no = no[parte]
    return no if isinstance(no, (int, float)) else omissao


# ---------------------------------------------------------------------------
#  O RULESET e o MESMO para o mesmo `gameConfig`, e era reconstruido a cada
#  chamada. Isso nao se notava numa build, mas o optimizador de equipamento
#  avalia 36 mil combinacoes e cada uma reconstruia o conjunto inteiro —
#  incluindo `_municao`, que percorre todos os itens do jogo. Eram 53% do tempo
#  do optimizador gastos a responder a mesma pergunta 36 mil vezes.
#
#  A cache guarda uma referencia FORTE ao `cfg`: sem ela, o `id()` de um
#  dicionario ja libertado podia ser reatribuido a outro e a cache devolvia as
#  regras erradas para a configuracao certa. Oito entradas chegam — ha um
#  `gameConfig` real e alguns de teste — e nada aqui depende do tempo, por isso
#  nao ha prazo de validade a gerir: quem muda a config muda o objecto.
# ---------------------------------------------------------------------------
_CACHE: dict[int, tuple[dict, dict]] = {}
_CACHE_MAX = 8


def limpar_cache() -> None:
    """Esquece os RULESETs memorizados. Para testes que mexem numa config."""
    _CACHE.clear()
    _CACHE_VALORES.clear()


def carregar(cfg: dict | None = None) -> dict:
    """
    O conjunto de regras completo, com a fonte de cada numero.

    `cfg` e o `gameConfig` do jogo. Sem ele tudo cai nos valores de recurso e o
    `RULESET["degradado"]` fica a True — o painel tem de o dizer em vez de
    apresentar numeros de recurso como se fossem leituras.

    O resultado e memorizado por `cfg`. Se alguem mudar um `gameConfig` NO
    SITIO depois de o ter passado por aqui, chame `regras.limpar_cache()` — a
    alternativa era refazer o conjunto inteiro dezenas de milhares de vezes por
    optimizacao para cobrir um caso que nao acontece em producao.
    """
    cfg = cfg or {}
    chave = id(cfg)
    guardado = _CACHE.get(chave)
    if guardado is not None and guardado[0] is cfg:
        return guardado[1]
    rs = _construir(cfg)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[chave] = (cfg, rs)
    return rs


def _construir(cfg: dict) -> dict:
    """O trabalho a serio. Separado de `carregar` so por causa da cache."""
    tem_cfg = bool(cfg.get("battle") or cfg.get("skills"))

    def do_jogo(caminho, omissao, o_que):
        v = _n(cfg, caminho, None)
        return {"valor": float(v) if v is not None else float(omissao),
                "fonte": "config" if v is not None else "recurso",
                "campo": caminho, "o_que": o_que}

    def fixo(valor, fonte, o_que):
        return {"valor": float(valor), "fonte": fonte, "o_que": o_que}

    return {
        "versao": VERSAO,
        "degradado": not tem_cfg,

        # -------------------------------------------------------------------
        #  O golpe
        # -------------------------------------------------------------------
        "vida_por_golpe": do_jogo("battle.healthCost", 10.0,
                                  "vida que custa tentar um golpe"),
        "dano_falha": fixo(0.5, "wiki",
                           "uma falha da metade do dano e nunca e critica"),
        "curva_defesa": fixo(40.0, "config",
                             "efetivo = total/(total+40), em armadura e esquiva"),
        "precisao_max": fixo(100.0, "wiki", "acima disto a precisao transborda"),
        "critico_max": fixo(100.0, "wiki", "acima disto o critico transborda"),
        "transbordo_precisao": {
            "valor": float(_n(cfg, "skills.precision.skillOverflowValue", 4)),
            "fonte": "config" if _n(cfg, "skills.precision.skillOverflowValue", None)
                     is not None else "recurso",
            "o_que": "ataque ganho por cada ponto de precisao acima de 100"},
        "transbordo_critico": {
            "valor": float(_n(cfg, "skills.criticalChance.skillOverflowValue", 4)),
            "fonte": "config" if _n(cfg, "skills.criticalChance.skillOverflowValue", None)
                     is not None else "recurso",
            "o_que": "dano critico ganho por cada ponto de critico acima de 100"},

        # -------------------------------------------------------------------
        #  A ESQUIVA. A regra que este painel teve errada durante mais tempo.
        #
        #  Um golpe esquivado e "a free hit": nao gasta vida nenhuma. Nao ha
        #  dano que se leve em WarEra — a vida e o custo de ATACAR — por isso a
        #  esquiva nao "reduz dano recebido", da golpes de graca.
        #
        #  Fonte, textual (warera.wiki/en/combat-stats, editada 2026-07-20):
        #    "The Dodge stat determines the percentage probability that an
        #     attempted hit will result in no health loss to the player [...]
        #     ("a free hit") [...] represented on the battle interface by
        #     displaying 'Dodged' instead of the value of health lost when
        #     attempting a hit."
        # -------------------------------------------------------------------
        "esquiva_poupa_vida": {
            "valor": 1.0, "fonte": "wiki",
            "o_que": ("um golpe esquivado custa ZERO vida; a esquiva multiplica "
                      "o custo esperado por (1 - esquiva_efetiva)")},
        "esquiva_poupa_durabilidade": {
            "valor": 1.0, "fonte": "wiki",
            "o_que": ("um golpe esquivado nao gasta durabilidade das cinco pecas; "
                      "a ARMA gasta na mesma")},
        "esquiva_poupa_municao": {
            "valor": 0.0, "fonte": "wiki",
            "o_que": "a municao gasta-se mesmo quando se esquiva"},

        # -------------------------------------------------------------------
        #  Durabilidade
        # -------------------------------------------------------------------
        "durabilidade_por_golpe_pct": fixo(
            1.0, "wiki", "cada golpe gasta 1% de cada peca: 100 golpes destroem-na"),
        "fraccao_scraps_peca_partida": fixo(
            config.FRACAO_SCRAPS_PECA_PARTIDA, "observacao",
            "uma peca gasta desmantela-se por um terco dos scraps "
            "(3735 desmantelamentos, sem excecao)"),

        # -------------------------------------------------------------------
        #  Barras e regeneracao
        # -------------------------------------------------------------------
        "regen_por_hora": {
            "valor": 1.0 / float(_n(cfg, "user.regenDividedBy", 10) or 10),
            "fonte": "config" if _n(cfg, "user.regenDividedBy", None) is not None
                     else "recurso",
            "o_que": "fraccao do maximo que cada barra repoe por hora"},

        # -------------------------------------------------------------------
        #  Multiplicadores de ataque, todos multiplicativos entre si
        # -------------------------------------------------------------------
        "municao": {
            code: {"valor": mult, "fonte": "config",
                   "o_que": f"{code} multiplica o ataque"}
            for code, mult in _municao(cfg).items()},
        "pilula": _pilula(cfg),

        # -------------------------------------------------------------------
        #  Loot
        # -------------------------------------------------------------------
        "dano_por_peca_loot": do_jogo("loot.damagePerLootItem", 200000,
                                      "dano por peca de loot fora de batalha"),
        "dano_por_peca_loot_batalha": do_jogo(
            "loot.battleLootDamagePerLootItem", 500000,
            "dano por peca de loot dentro de batalha"),
        "chance_arma_loot_pct": do_jogo("loot.weaponChancePercent", 30,
                                        "fraccao do loot que sai arma"),
    }


def _municao(cfg: dict) -> dict[str, float]:
    """
    Os multiplicadores de cada municao, lidos do proprio item.

    O `gameConfig` guarda isto em `items.<code>.flatStats.percentAttack`, que e
    a fonte certa: se o WarEra mexer na heavy ammo, muda aqui sozinho. O
    `config.MULTIPLICADORES` fica de recurso.
    """
    fora = {}
    for code, it in (cfg.get("items") or {}).items():
        if not isinstance(it, dict):
            continue
        flat = it.get("flatStats") or {}
        pct = flat.get("percentAttack")
        # so a municao: os buffs tem duracao e sao outra coisa
        if pct and not flat.get("buffDurationHours"):
            fora[code] = 1.0 + float(pct) / 100.0
    if not fora:
        fora = {k: v for k, v in config.MULTIPLICADORES.items()
                if k in ("lightAmmo", "ammo", "heavyAmmo")}
    return fora


def _pilula(cfg: dict) -> dict:
    """As duas fases da pilula, do proprio item quando la estiverem."""
    item = ((cfg or {}).get("items") or {}).get("cocain") or {}
    flat = item.get("flatStats") or {}
    tem = bool(flat.get("percentAttack"))
    return {
        "ataque_pct": {"valor": float(flat.get("percentAttack")
                                      or config.PILULA["ataque_pct"]),
                       "fonte": "config" if tem else "recurso",
                       "o_que": "ataque extra durante o buff"},
        "horas_buff": {"valor": float(flat.get("buffDurationHours")
                                      or config.PILULA["horas_buff"]),
                       "fonte": "config" if flat.get("buffDurationHours") else "recurso",
                       "o_que": "duracao do buff"},
        "horas_ressaca": {"valor": float(flat.get("debuffDurationHours")
                                         or config.PILULA["horas_ressaca"]),
                          "fonte": "config" if flat.get("debuffDurationHours")
                                   else "recurso",
                          "o_que": "duracao da ressaca"},
        # o jogo NAO publica a percentagem da ressaca: a wiki e o guia dizem os
        # dois -60%, e concordam, mas nao ha campo que o confirme
        "ressaca_pct": {"valor": float(config.PILULA["ressaca_pct"]),
                        "fonte": "wiki",
                        "o_que": "ataque perdido durante a ressaca"},
    }


_CACHE_VALORES: dict[int, tuple[dict, dict]] = {}


def valores(rs: dict) -> dict:
    """
    So os numeros, sem a proveniencia — para quem so quer calcular.

    O motor usa isto; o painel usa o `RULESET` inteiro, porque a proveniencia e
    metade do que este projecto tem para dizer.

    Memorizado pelo mesmo motivo que o `carregar`: o motor chama isto uma vez
    por golpe, e um optimizador conta dezenas de milhares de golpes. O
    dicionario devolvido e partilhado — quem calcula le, nao escreve.
    """
    chave = id(rs)
    guardado = _CACHE_VALORES.get(chave)
    if guardado is not None and guardado[0] is rs:
        return guardado[1]
    fora = {}
    for nome, v in (rs or {}).items():
        if isinstance(v, dict) and "valor" in v:
            fora[nome] = v["valor"]
        elif nome in ("municao", "pilula"):
            fora[nome] = {k: d["valor"] for k, d in v.items()}
    if len(_CACHE_VALORES) >= _CACHE_MAX:
        _CACHE_VALORES.clear()
    _CACHE_VALORES[chave] = (rs, fora)
    return fora


def por_fonte(rs: dict) -> dict[str, list[str]]:
    """Que regras vem de onde. E a tabela que o painel mostra."""
    fora: dict[str, list[str]] = {}
    for chave, v in (rs or {}).items():
        if isinstance(v, dict) and "fonte" in v:
            fora.setdefault(v["fonte"], []).append(chave)
        elif chave in ("municao", "pilula") and isinstance(v, dict):
            for sub, d in v.items():
                if isinstance(d, dict) and "fonte" in d:
                    fora.setdefault(d["fonte"], []).append(f"{chave}.{sub}")
    for k in fora:
        fora[k].sort()
    return fora
