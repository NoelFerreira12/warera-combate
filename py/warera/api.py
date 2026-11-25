"""
Cliente da API publica do WarEra (tRPC sobre GET).

A API responde 403 sem os cabecalhos de origem do jogo, por isso eles nao sao
opcionais. O token e opcional: quase tudo o que interessa para dinheiro e
publico, mas com token ha menos rate-limit.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

_BASE = config.API_OFICIAL
_cache: dict[str, tuple[object, float]] = {}
_lock = threading.Lock()


def usar_gateway(ativo: bool = True) -> None:
    """Troca para o gateway da comunidade (por vezes mais rapido)."""
    global _BASE
    _BASE = config.API_GATEWAY if ativo else config.API_OFICIAL


def base() -> str:
    return _BASE


def headers() -> dict[str, str]:
    h = {
        "Origin": "https://app.warera.io",
        "Referer": "https://app.warera.io/",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "application/json",
    }
    if config.token():
        h["X-API-Key"] = config.token()
    if config.jwt():
        h["Cookie"] = f"jwt={config.jwt()}"
    return h


class ErroAPI(RuntimeError):
    pass


ESPERA_429_MAX = 20.0


def _espera_429(cabecalhos, tentativa: int) -> float:
    """Quanto esperar depois de um 429: o que o servidor disser, com tecto."""
    for nome in ("ratelimit-reset", "Retry-After"):
        try:
            v = float((cabecalhos or {}).get(nome))
        except (TypeError, ValueError):
            continue
        if v >= 0:
            return min(v, ESPERA_429_MAX) + 0.5
    return float(2 ** tentativa)


# Quantos pedidos saiu para cada endpoint nesta execucao: e o que os testes
# usam para provar que um endpoint que exige token nao e chamado sem ele.
PEDIDOS: dict[str, int] = {}


def call(endpoint: str, params: dict | None = None, retries: int = 4):
    """Chamada crua. Levanta ErroAPI em caso de falha."""
    PEDIDOS[endpoint] = PEDIDOS.get(endpoint, 0) + 1
    qs = urllib.parse.quote(json.dumps(params or {}, separators=(",", ":")))
    url = f"{_BASE}/{endpoint}?input={qs}"
    req = urllib.request.Request(url, headers=headers())
    ultimo = ""
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            if "error" in data and "result" not in data:
                err = data["error"]
                msg = (err.get("json") or err).get("message", str(err)) if isinstance(err, dict) else str(err)
                raise ErroAPI(f"{endpoint}: {msg}")
            return data["result"]["data"]
        except urllib.error.HTTPError as e:
            corpo = ""
            try:
                corpo = json.loads(e.read().decode())["error"]["message"]
            except Exception:
                pass
            ultimo = f"HTTP {e.code} em {endpoint}: {corpo}"
            if e.code == 429 and i < retries - 1:
                # o limite e de 100 pedidos por minuto e o servidor diz quando a
                # janela repoe (`ratelimit-reset`, em segundos). Esperar 1, 2 e
                # 4 segundos as cegas desistia antes de a janela abrir
                time.sleep(_espera_429(e.headers, i))
                continue
            if e.code in (502, 503) and i < retries - 1:
                time.sleep(2 ** i)
                continue
            raise ErroAPI(ultimo)
        except urllib.error.URLError as e:
            ultimo = f"rede: {e.reason}"
            if i < retries - 1:
                time.sleep(1 + i)
                continue
            raise ErroAPI(f"{endpoint}: {ultimo}")
    raise ErroAPI(ultimo or f"falhou {endpoint}")


def safe(endpoint: str, params: dict | None = None, default=None):
    """Como call(), mas devolve `default` em vez de rebentar."""
    try:
        return call(endpoint, params)
    except Exception:
        return default


def cached(chave: str, ttl: float, produtor):
    """Cache em memoria, partilhada entre pedidos HTTP do painel."""
    with _lock:
        item = _cache.get(chave)
        if item and time.time() - item[1] < ttl:
            return item[0]
    valor = produtor()
    with _lock:
        _cache[chave] = (valor, time.time())
    return valor


def em_cache(prefixo: str, ttl: float):
    """
    Espreita o que ja esta em cache sem mandar produzir nada.

    Serve para uma analise aproveitar o trabalho de outra: a analise de build
    quer os precos do relatorio de mercado, mas nao vale um relatorio inteiro
    de 15 segundos so por causa disso.
    """
    with _lock:
        for chave, (valor, quando) in _cache.items():
            if chave.startswith(prefixo) and time.time() - quando < ttl:
                return valor
    return None


def limpar_cache(prefixo: str = "") -> None:
    with _lock:
        for k in [k for k in _cache if k.startswith(prefixo)]:
            del _cache[k]


# ---------------------------------------------------------------------------
# Endpoints usados pelo painel (todos verificados contra a API real)
# ---------------------------------------------------------------------------

def game_config() -> dict:
    return api_cached("gameConfig.getGameConfig", {}, config.TTL_CACHE_CONFIG)


def api_cached(endpoint: str, params: dict, ttl: float):
    chave = f"{endpoint}:{json.dumps(params, sort_keys=True)}"
    return cached(chave, ttl, lambda: call(endpoint, params))


def precos_referencia() -> dict[str, float]:
    d = call("itemTrading.getPrices", {})
    return {k: float(v) for k, v in d.items() if isinstance(v, (int, float))}


def top_orders(item_code: str) -> dict:
    d = safe("tradingOrder.getTopOrders", {"itemCode": item_code}, {}) or {}
    return {"buyOrders": d.get("buyOrders") or [], "sellOrders": d.get("sellOrders") or []}


def preco_medio_equipamento(item_code: str) -> float | None:
    v = safe("gameStat.getEquipmentAvgByCode", {"itemCode": item_code})
    return float(v) if isinstance(v, (int, float)) else None


def paises() -> list[dict]:
    return api_cached("country.getAllCountries", {}, config.TTL_CACHE_CONFIG) or []


def pais(country_id: str) -> dict:
    """
    Um pais com tudo: taxes (market/selfWork/income), specializedItem,
    populacao ativa, desenvolvimento e rankings. `getAllCountries` ja traz o
    essencial, mas este e o que confirma o detalhe de um pais escolhido.
    """
    return api_cached("country.getCountryById", {"countryId": country_id},
                      config.TTL_CACHE_CONFIG) or {}


def batalhas(paginas: int = 3, limite: int = 20) -> list[dict]:
    """
    Batalhas por ordem de criacao, da mais recente para tras. `damages` de cada
    lado e acumulado da batalha inteira, e e o que permite ver onde esta a
    guerra a serio em vez de contar torneios vazios.
    """
    fora: list[dict] = []
    cursor = None
    for _ in range(max(1, paginas)):
        p: dict = {"limit": limite}
        if cursor:
            p["cursor"] = cursor
        d = safe("battle.getBattles", p)
        if not d:
            break
        fora.extend(d.get("items") or [])
        cursor = d.get("nextCursor")
        if not cursor:
            break
    return fora


ENDPOINT_TRANSACOES = "transaction.getPaginatedTransactions"
# depois de um 401 nao se volta a tentar durante meia hora: o motivo fica
# registado, e o painel nao enche a API (nem o limite de pedidos) de recusas
ESPERA_DEPOIS_DE_401_S = 1800.0
_RECUSAS: dict[str, tuple[str, float]] = {}


def estado_transacoes() -> dict:
    """
    Se o feed de transacoes esta disponivel, e porque nao.

    `transaction.getPaginatedTransactions` exige API token (verificado a
    19/11/2026: 401 "API token required" sem token, e a auditoria publica da
    API de 01/10/2026 diz o mesmo). Sem token o painel nem tenta: devolve
    indisponivel, com o motivo, e zero pedidos. Com token (so com
    `WARERA_USAR_TOKEN=1`), tenta, e um 401 fica registado meia hora.
    """
    agora = time.time()
    base = {"endpoint": ENDPOINT_TRANSACOES, "documentado": True}
    if not config.token():
        return {**base, "disponivel": False, "tentado": False,
                "motivo": "o jogo exige API token neste endpoint, e o painel corre sem a "
                          "tua API key — as transacções reais ficam indisponíveis"}
    recusa = _RECUSAS.get(ENDPOINT_TRANSACOES)
    if recusa and agora - recusa[1] < ESPERA_DEPOIS_DE_401_S:
        return {**base, "disponivel": False, "tentado": True,
                "motivo": f"o jogo recusou o token: {recusa[0]}",
                "recusado_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(recusa[1]))}
    return {**base, "disponivel": True, "tentado": False, "motivo": None}


def transacoes(paginas: int = config.PAGINAS_HISTORICO) -> list[dict]:
    """
    Feed global de transacoes, da mais recente para tras, sem repetidos.

    Atencao: e MUITO rapido. 100 transacoes ~= 11 segundos de jogo real.
    40 paginas ~= 7 minutos de historico e demora ~8s a puxar.

    Sem token (o modo normal deste painel) devolve [] sem fazer pedido nenhum;
    quem precisa de saber porque pergunta a `estado_transacoes()`.
    """
    if not estado_transacoes()["disponivel"]:
        return []
    fora: list[dict] = []
    vistos: set = set()
    cursor = None
    for _ in range(max(1, paginas)):
        p: dict = {"limit": 100}
        if cursor:
            p["cursor"] = cursor
        try:
            resp = call(ENDPOINT_TRANSACOES, p)
        except ErroAPI as e:
            texto = str(e)
            if "401" in texto or "token" in texto.lower():
                _RECUSAS[ENDPOINT_TRANSACOES] = (texto[:160], time.time())
            break
        if not resp:
            break
        for t in resp.get("items") or []:
            # o cursor pode trazer de volta uma transacao que ja veio na pagina
            # anterior quando o feed anda depressa: conta-se uma vez so
            tid = t.get("_id")
            if tid and tid in vistos:
                continue
            if tid:
                vistos.add(tid)
            fora.append(t)
        cursor = resp.get("nextCursor")
        if not cursor:
            break
    return fora


def historico_item(item_code: str) -> list[dict]:
    """
    30 dias de preco medio e volume diario. So existe para bens negociaveis;
    para equipamento devolve 404, por isso vem vazio.
    """
    d = safe("itemTrading.getItemTrading", {"itemCode": item_code}, {}) or {}
    return d.get("values") or []


def ofertas_equipamento(item_code: str, limite: int = 12,
                        min_skills: dict | None = None,
                        paginas: int = 1) -> list[dict]:
    """
    O que esta a venda AGORA, peca a peca e com o roll de cada uma.

    `itemOffer.getItemOffers` e o unico sitio onde isto existe, e responde
    403 "API tokens cannot access this endpoint" — e de sessao, como
    `user.getMe`. Com o cookie jwt no .env (WARERA_JWT) funciona; sem ele, o
    painel fica-se pelas vendas que ja aconteceram, que o feed da.
    """
    fora: list[dict] = []
    cursor = None
    for _ in range(max(1, paginas)):
        p: dict = {"itemCode": item_code, "limit": limite}
        if min_skills:
            p["minSkills"] = min_skills
        if cursor:
            p["cursor"] = cursor
        d = safe("itemOffer.getItemOffers", p)
        if not d:
            break
        fora.extend(d.get("items") or [])
        cursor = d.get("nextCursor")
        if not cursor:
            break
    return fora


def inventario() -> list[dict] | None:
    """
    O que tens no inventario, peca a peca e com o roll de cada uma.

    `inventory.getInventory` e de SESSAO, como `itemOffer.getItemOffers` e
    `user.getMe`: com um token de API responde 403 "API tokens cannot access
    this endpoint", e nao 404 — ou seja o procedimento existe, so nao aceita
    tokens. Com o cookie jwt no .env (WARERA_JWT) funciona.

    E o unico caminho para responder "qual e a melhor build que consigo vestir
    AGORA": o `user.getUserById` publico nao traz inventario nenhum, e o
    `user.equipment` que traz e so o ID da peca equipada. Sem o cookie, o
    painel devolve None e diz que nao sabe, em vez de assumir que o jogador
    nao tem nada — que daria a recomendacao errada de comprar tudo de novo.
    """
    if not config.jwt():
        return None
    d = safe("inventory.getInventory", {})
    if d is None:
        return None
    if isinstance(d, list):
        return d
    return d.get("items") or d.get("inventory") or []


def utilizador(user_id: str) -> dict:
    return call("user.getUserById", {"userId": user_id})


def equipamento_vestido(user_id: str) -> dict | None:
    """
    O que o jogador tem vestido, peca a peca, com o roll e o estado de cada uma.

    `inventory.fetchCurrentEquipment` e PUBLICO e esta na documentacao da API
    (`api2.warera.io/docs`). Resolve o que o `user.equipment` nao resolvia:
    ali so vem o ID da peca, e o painel tinha de deduzir o modelo pelos stats —
    o que falhava no peito e nas calcas, que partilham a armadura.
    """
    d = safe("inventory.fetchCurrentEquipment", {"userId": user_id})
    return d if isinstance(d, dict) else None


def resumo_loot(battle_id: str, user_id: str) -> dict | None:
    """
    O que um jogador fez e recebeu numa batalha: golpes, dano, cases, as pecas
    do ranking, o bounty (`totalMoneyFromBounty`) e o contrato.

    Devolve `{}` quando o jogador nao bateu ali (a API responde 404) e `None`
    quando o pedido falhou — sao coisas diferentes, e so a primeira se pode
    guardar como resposta.
    """
    try:
        d = call("battleLootSummary.getByBattleAndUser",
                 {"battleId": battle_id, "userId": user_id}, retries=2)
    except ErroAPI as e:
        texto = str(e).lower()
        return {} if ("404" in texto or "not found" in texto) else None
    return d if isinstance(d, dict) else {}


def _batalhas(filtro: dict, paginas: int, limite: int = 50) -> list[dict]:
    fora: list[dict] = []
    cursor = None
    for _ in range(max(1, paginas)):
        p = dict(filtro, limit=limite)
        if cursor:
            p["cursor"] = cursor
        d = safe("battle.getBattles", p)
        if not d:
            break
        fora.extend(d.get("items") or [])
        cursor = d.get("nextCursor")
        if not cursor:
            break
    return fora


def batalhas_ativas(paginas: int = 3) -> list[dict]:
    """
    As batalhas a decorrer, com o bounty de cada lado: `moneyPer1kDamages`,
    `moneyPool` e `bountyEffectiveAt` vem dentro de `attacker` e `defender`.
    """
    return api_cached_lista(("ativas", paginas),
                            lambda: _batalhas({"isActive": True}, paginas))


def batalhas_do_pais(country_id: str, paginas: int = 3) -> list[dict]:
    """As batalhas em que um pais entrou, activas primeiro e depois as acabadas."""
    return api_cached_lista(
        ("pais", country_id, paginas),
        lambda: (_batalhas({"isActive": True, "countryId": country_id}, 1)
                 + _batalhas({"isActive": False, "countryId": country_id}, paginas)))


def api_cached_lista(chave: tuple, produtor, ttl: float = 60.0) -> list[dict]:
    return cached("batalhas:" + json.dumps(chave), ttl, produtor) or []


def leiloes_mercenario(limite: int = 50, status: str | None = None) -> list[dict]:
    """
    Os leiloes de contratos de mercenario abertos. PUBLICO: e
    `mercenaryContractAuction.getPaginatedAuctions`, que esta na documentacao,
    e nao `mercenaryContract.getContracts`, que recusa tokens.
    """
    p: dict = {"limit": max(1, min(50, int(limite)))}
    if status:
        p["status"] = status
    d = safe("mercenaryContractAuction.getPaginatedAuctions", p, {}) or {}
    return d.get("items") or []


def procurar_utilizadores(texto: str) -> list[str]:
    """
    Devolve IDs, nao objetos. O parametro chama-se `searchText` — com `query`
    a API responde 400, que era o que fazia a procura por nome falhar sempre.
    """
    d = safe("search.searchUsers", {"searchText": texto})
    if isinstance(d, list):
        return [x for x in d if isinstance(x, str)]
    d = safe("search.searchAnything", {"searchText": texto}, {}) or {}
    return [x for x in (d.get("userIds") or []) if isinstance(x, str)]
