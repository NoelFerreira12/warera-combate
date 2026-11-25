#!/usr/bin/env python3
"""
WarEra: minimum damage needed for tier 2, 3, 4, 5 and 6 drops in each battle.

Shows the live round (or the last round if the battle is over) and the overall
battle ranking, which has its own drops. The loot follows the game's MERGED
ranking (`side: "merged"`): one row per player with the damage done on both
sides summed. Verified on 2026-11-19 in 9 pools (merged value = attacker +
defender for every player; zero rarity inversions; zero items below the cut).
The pool has floor(total damage / 200 000) items for a round and / 500 000 for
the battle, handed out best first, one per player, from rank 1 down. Each
rarity closes at the first item below it, and the pool ends at the first player
without an item. The time left is an estimate for the side now winning on
damage, assuming it wins every tick (so it is the minimum).

Only talks to api2.warera.io (never api5), with a request limit and a disk cache:
finished data is never requested twice, live data is always read again.

Usage:
    pip install requests
    python warera_drops.py                 # all active battles
    python warera_drops.py --ended 20      # plus the last 20 ended battles
    python warera_drops.py --battle poland # that battle only, round by round
    python warera_drops.py --loop 120      # refresh every 120 seconds

Optional: export WARERA_API_KEY=your_key
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:
    import requests
except ImportError:  # the panel imports the pure functions below without needing requests
    requests = None

BASE_URL = "https://api2.warera.io/trpc"
ARMOR_RE = re.compile(r"(?:helmet|chest|pants|boots|gloves)([1-6])")
WEAPON_TIER = {"knife": 1, "gun": 2, "rifle": 3, "sniper": 4, "tank": 5, "jet": 6}
BLUE_TIER = 3
# common (tier 1) never shows up in ranking loot, so uncommon is the lowest drop
LOWEST_TIER = 2
TIERS = (2, 3, 4, 5, 6)
TICK_SECONDS = 120
TIER_NAMES = {2: "Uncommon", 3: "Rare", 4: "Epic", 5: "Legendary", 6: "Mythic"}
SIDES = ("attacker", "defender")
MERGED = "merged"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class FatalApiError(ApiError):
    """Stop right away so we do not risk a ban."""


class Api:
    def __init__(self, base_url: str, rate_per_minute: float, api_key: str | None):
        if requests is None:
            raise SystemExit("this script needs requests: pip install requests")
        self.base_url = base_url.rstrip("/")
        self.interval = 60.0 / rate_per_minute
        self.last_request = 0.0
        self.method = "GET"
        self.requests_made = 0
        self.session = requests.Session()
        self.session.headers.update(
            {"Accept": "application/json", "User-Agent": "warera-drops-script/1.0"}
        )
        if api_key:
            self.session.headers["X-API-Key"] = api_key

    def _wait_turn(self) -> None:
        delay = self.last_request + self.interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()

    def _send(self, procedure: str, payload: dict) -> requests.Response:
        self._wait_turn()
        self.requests_made += 1
        url = f"{self.base_url}/{procedure}"
        if self.method == "GET":
            return self.session.get(url, params={"input": json.dumps(payload)}, timeout=30)
        return self.session.post(url, json=payload, timeout=30)

    def call(self, procedure: str, payload: dict | None = None):
        payload = payload or {}
        rate_limited = 0
        server_errors = 0
        while True:
            try:
                response = self._send(procedure, payload)
            except requests.RequestException as exc:
                server_errors += 1
                if server_errors > 3:
                    raise FatalApiError(0, f"cannot reach the API: {exc}") from exc
                time.sleep(5 * server_errors)
                continue

            status = response.status_code
            if status == 405 and self.method == "GET":
                self.method = "POST"
                continue
            if status == 429:
                rate_limited += 1
                if rate_limited >= 3:
                    raise FatalApiError(429, "too many 429s in a row, stopping to avoid a ban")
                wait = _retry_after(response)
                print(f"  Got a 429, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            if status in (401, 403):
                raise FatalApiError(status, "access denied, stopping (try setting WARERA_API_KEY)")
            if status >= 500:
                server_errors += 1
                if server_errors > 3:
                    raise ApiError(status, f"{procedure} keeps failing")
                time.sleep(5 * server_errors)
                continue

            try:
                body = response.json()
            except ValueError as exc:
                raise ApiError(status, f"{procedure} did not return JSON") from exc
            if isinstance(body, dict) and "error" in body:
                code = _trpc_error_code(body)
                if code == "METHOD_NOT_SUPPORTED" and self.method == "GET":
                    self.method = "POST"
                    continue
                raise ApiError(status, f"{procedure}: {code or body['error']}")
            if status >= 400:
                raise ApiError(status, f"{procedure}: {response.text[:200]}")
            return body["result"]["data"]


def _retry_after(response: requests.Response) -> int:
    for header in ("Retry-After", "Ratelimit-Reset"):
        value = response.headers.get(header, "")
        if value.isdigit():
            return min(int(value), 600) + 1
    return 60


def _trpc_error_code(body: dict) -> str | None:
    error = body.get("error") or {}
    data = error.get("data") or error.get("json", {}).get("data") or {}
    return data.get("code")


class Cache:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT)")

    def get(self, key: str):
        row = self.db.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key: str, value) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO cache (key, value) VALUES (?, ?)", (key, json.dumps(value))
        )
        self.db.commit()


def item_tier(code: str | None) -> int | None:
    """Item tier (3 = blue), or None if it is not known equipment."""
    if code in WEAPON_TIER:
        return WEAPON_TIER[code]
    match = ARMOR_RE.fullmatch(code or "")
    return int(match.group(1)) if match else None


def find_cutoffs(entries: list[dict]) -> tuple[dict[int, dict], dict | None]:
    """Walk down the merged ranking by damage. A tier closes at the first item below it.

    In the merged ranking the pool ends at the first player without an item, so
    that row is where the walk stops. Returns ({tier: last entry of that tier
    before it closed}, the entry where the pool ended, or None).
    """
    lowest: dict[int, dict] = {}
    open_tiers = set(TIERS)
    for entry in sorted(entries, key=lambda e: e["value"], reverse=True):
        if not entry.get("item"):
            return lowest, entry
        tier = item_tier(entry.get("item"))
        if tier is None:
            continue
        open_tiers = {t for t in open_tiers if t <= tier}
        if tier in open_tiers:
            lowest[tier] = entry
        if tier < LOWEST_TIER:
            return lowest, entry
    return lowest, None


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def winning_side(round_data: dict) -> str | None:
    """The side with more damage wins the ticks. On a tie, the side with more points."""
    try:
        attacker, defender = round_data["attacker"], round_data["defender"]
        scores = {
            side: (data.get("damages", 0), data.get("points", 0))
            for side, data in (("attacker", attacker), ("defender", defender))
        }
    except (KeyError, TypeError, AttributeError):
        return None
    return max(SIDES, key=lambda side: scores[side])


def tick_points_for(ground: int, table: list[tuple[int, int]], default: int) -> int:
    """Points for the next tick, based on the ground already gained in the round."""
    points = default
    for threshold, value in table:
        if ground >= threshold and value > 0:
            points = value
    return points


def estimate_round_seconds(round_data: dict, config: dict, now: datetime) -> float | None:
    """Time until the side now winning reaches the target, if it wins every tick."""
    live = round_data.get("live") or {}
    target = config.get("pointsToWinRound")
    side = winning_side(round_data)
    try:
        next_tick = parse_time(live["nextTickAt"])
        tick_points = live["actualTickPoints"]
        points = round_data[side].get("points", 0)
        ground = points + round_data[other_side(side)].get("points", 0)
        need = target - points
    except (KeyError, TypeError, ValueError):
        return None
    if need <= 0:
        return 0.0
    if tick_points <= 0:
        return None
    table = sorted((int(k), v) for k, v in (config.get("tickPoints") or {}).items())
    ticks = 0
    while need > 0 and ticks < 100000:
        need -= tick_points
        ground += tick_points
        ticks += 1
        tick_points = tick_points_for(ground, table, tick_points)
    return max(0.0, (next_tick - now).total_seconds() + (ticks - 1) * TICK_SECONDS)


def other_side(side: str) -> str:
    return "defender" if side == "attacker" else "attacker"


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "<1m"
    return f"~{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"~{minutes}m"


def fetch_paged(
    api: Api, procedure: str, payload: dict, limits: tuple[int, ...], stop_when=None
) -> list[dict]:
    """Follow the cursor. If the API rejects the limit, try the next one."""
    for position, limit in enumerate(limits):
        items: list[dict] = []
        cursor = None
        try:
            for _ in range(500):
                request = dict(payload, limit=limit)
                if cursor:
                    request["cursor"] = cursor
                data = api.call(procedure, request)
                if isinstance(data, list):
                    return data
                # The older ranking schema uses "rankings" instead of "items".
                page = data.get("items") or data.get("rankings") or []
                items.extend(page)
                cursor = data.get("nextCursor")
                if not cursor or (stop_when and stop_when(page)):
                    break
            return items
        except ApiError as exc:
            is_last = position == len(limits) - 1
            if isinstance(exc, FatalApiError) or exc.status != 400 or is_last:
                raise
    return []


def page_is_enough(page: list[dict]) -> bool:
    """No need for more pages: the pool ended on this one (a player without an item)."""
    return any(not (row.get("lootItem") or {}).get("code") for row in page)


def fetch_merged(api: Api, cache: Cache, scope: dict, live: bool) -> list[dict]:
    """The game's merged ranking: both sides, one row per player, damage summed.

    scope is {"roundId": id} for a round or {"battleId": id} for the whole battle.
    """
    scope_key = "round3:" + scope["roundId"] if "roundId" in scope else "battle3:" + scope["battleId"]
    key = f"{scope_key}:{MERGED}"
    cached = None if live else cache.get(key)
    if cached is not None:
        return cached
    raw = fetch_paged(
        api,
        "battleRanking.getRanking",
        dict(scope, dataType="damage", type="user", side=MERGED),
        (100, 20),
        stop_when=page_is_enough,
    )
    entries = [
        {
            "rank": row.get("rank"),
            "value": row.get("value", 0),
            "user": row.get("user"),
            "item": (row.get("lootItem") or {}).get("code"),
        }
        for row in raw
    ]
    # Live data changes, and missing loot may just not be assigned yet.
    if not live and any(e["item"] for e in entries):
        cache.set(key, entries)
    return entries


def fetch_ranking(api: Api, cache: Cache, scope: dict, live: bool, title: str) -> list[dict]:
    try:
        return fetch_merged(api, cache, scope, live)
    except FatalApiError:
        raise
    except ApiError as exc:
        print(f"  {title} failed: {exc}", file=sys.stderr)
        return []


def current_round_id(battle: dict) -> str | None:
    current = battle.get("currentRound")
    if isinstance(current, dict):
        return current.get("_id")
    return current


def rounds_newest_first(battle: dict) -> list[tuple[int, str, bool]]:
    """[(number, id, is_live)], starting with the live round."""
    round_ids = list(battle.get("rounds") or [])
    current = current_round_id(battle) if battle.get("isActive") else None
    if current and current not in round_ids:
        round_ids.append(current)
    numbered = [
        (number, round_id, round_id == current)
        for number, round_id in enumerate(round_ids, start=1)
    ]
    numbered.sort(key=lambda r: (r[2], r[0]), reverse=True)
    return numbered


def load_names(api: Api, cache: Cache) -> dict[str, str]:
    cached = cache.get("names")
    if cached:
        return cached
    names: dict[str, str] = {}
    try:
        countries = api.call("country.getAllCountries")
        for country in countries if isinstance(countries, list) else countries.values():
            names[country["_id"]] = country.get("name") or country.get("code") or country["_id"]
        regions = api.call("region.getRegionsObject")
        for region_id, region in regions.items():
            names[region_id] = region.get("name") or region_id
    except (ApiError, AttributeError, KeyError, TypeError) as exc:
        if isinstance(exc, FatalApiError):
            raise
        print(f"Warning: no country/region names ({exc})", file=sys.stderr)
    if names:
        cache.set("names", names)
    return names


def battle_label(battle: dict, names: dict[str, str]) -> str:
    def name(side: str, key: str) -> str:
        value = (battle.get(side) or {}).get(key)
        return names.get(value, value or "?") if value else "?"

    return f"{name('attacker', 'country')} vs {name('defender', 'country')} ({name('defender', 'region')})"


@dataclass
class BattleResult:
    battle_id: str
    label: str
    round_label: str = "-"
    time_left: str = "-"
    seconds_left: float | None = None
    round_tiers: dict = field(default_factory=dict)
    battle_tiers: dict = field(default_factory=dict)
    detail_lines: list[str] = field(default_factory=list)


def fmt(value: float) -> str:
    """Rounds like the game does: 35.44k, 102.87M."""
    if value < 1000:
        return str(int(value))
    for divisor, suffix in ((1e3, "k"), (1e6, "M")):
        if round(value / divisor, 2) < 1000:
            return f"{value / divisor:.2f}{suffix}"
    return f"{value / 1e9:.2f}B"


def tier_cells(tiers: dict[int, dict]) -> list[str]:
    return [fmt(tiers[tier]["value"]) if tier in tiers else "-" for tier in TIERS]


def by_damage(r: BattleResult):
    blue = r.round_tiers.get(BLUE_TIER)
    return (blue is None, blue["value"] if blue else 0, r.label)


def by_time_left(r: BattleResult):
    """Soonest first; unknown time next, ended battles last."""
    ended = r.time_left == "ended"
    unknown = r.seconds_left is None
    return (ended, unknown, r.seconds_left or 0, r.label)


def print_report(results: list[BattleResult], title: str, sort_key) -> None:
    tier_names = [TIER_NAMES[tier] for tier in TIERS]
    header = ["Battle", "Round", "Ends in", *tier_names, *tier_names]
    rows = [
        [r.label[:40], r.round_label, r.time_left, *tier_cells(r.round_tiers), *tier_cells(r.battle_tiers)]
        for r in sorted(results, key=sort_key)
    ]
    widths = [max(len(row[col]) for row in [header, *rows]) for col in range(len(header))]

    def cells(row):
        return [
            cell.ljust(widths[i]) if i < 3 else cell.rjust(widths[i]) for i, cell in enumerate(row)
        ]

    n = len(TIERS)

    def line(row):
        c = cells(row)
        return f"{'  '.join(c[:3])} | {'  '.join(c[3:3 + n])} | {'  '.join(c[3 + n:])}"

    round_width = sum(widths[3:3 + n]) + 2 * (n - 1)
    battle_width = sum(widths[3 + n:]) + 2 * (n - 1)
    print()
    print(f"{title.ljust(sum(widths[:3]) + 4)} | {'ROUND DROPS'.ljust(round_width)} | {'BATTLE DROPS'.ljust(battle_width)}")
    print(line(header))
    print("-" * len(line(header)))
    for row in rows:
        print(line(row))


def fetch_battles(api: Api, ended: int) -> list[dict]:
    battles = fetch_paged(
        api, "battle.getBattles", {"isActive": True, "direction": "forward"}, (50, 10)
    )
    if ended > 0:
        collected: list[dict] = []
        cursor = None
        while len(collected) < ended:
            request = {"isActive": False, "direction": "forward", "limit": min(ended, 10)}
            if cursor:
                request["cursor"] = cursor
            data = api.call("battle.getBattles", request)
            collected.extend(data.get("items") or [])
            cursor = data.get("nextCursor")
            if not cursor:
                break
        battles += collected[:ended]
    return battles


def describe(title: str, entries: list[dict]) -> str:
    if not any(e.get("item") for e in entries):
        return f"{title}: no drops"
    lowest, stopped_at = find_cutoffs(entries)
    parts = [
        f"{TIER_NAMES[tier]} {fmt(lowest[tier]['value'])} (#{lowest[tier].get('rank')}, "
        f"{lowest[tier]['item']})"
        for tier in TIERS
        if tier in lowest
    ]
    text = f"{title}: {' | '.join(parts) or 'no drops'}"
    if stopped_at:
        text += (f", pool ends at #{stopped_at.get('rank')} ({fmt(stopped_at['value'])})"
                 if not stopped_at.get("item") else
                 f", stopped at {fmt(stopped_at['value'])} {stopped_at['item']}")
    else:
        text += f", every one of the {len(entries)} players has an item"
    return f"{text}\n    merged ranking: both sides in one list, {len(entries)} players read"


def current_round_data(api: Api, battle: dict) -> dict | None:
    current = battle.get("currentRound")
    if isinstance(current, dict):
        return current
    if not current:
        return None
    try:
        return api.call("round.getById", {"roundId": current})
    except FatalApiError:
        raise
    except ApiError:
        return None


def winning_line(round_data: dict, battle: dict, names: dict[str, str], config: dict) -> str | None:
    side = winning_side(round_data)
    if not side:
        return None
    data = round_data[side]
    country = names.get((battle.get(side) or {}).get("country"), side)
    tick = (round_data.get("live") or {}).get("actualTickPoints", "?")
    return (
        f"Winning: {country} ({fmt(data.get('damages', 0))} damage vs "
        f"{fmt(round_data[other_side(side)].get('damages', 0))}), "
        f"{data.get('points', 0)}/{config.get('pointsToWinRound', '?')} points, +{tick} per tick"
    )


def scan_battle(
    api: Api, cache: Cache, battle: dict, label: str, detail: bool, config: dict, names: dict[str, str]
) -> BattleResult:
    result = BattleResult(battle["_id"], label)
    active = bool(battle.get("isActive"))
    rounds = rounds_newest_first(battle)
    if rounds:
        result.round_label = str(rounds[0][0])
    if active:
        round_data = current_round_data(api, battle)
        if round_data:
            seconds = estimate_round_seconds(round_data, config, datetime.now(timezone.utc))
            result.seconds_left = seconds
            result.time_left = fmt_time(seconds)
            line = winning_line(round_data, battle, names, config)
            if line:
                result.detail_lines.append(line)
    else:
        result.time_left = "ended"

    found = False
    for number, round_id, live in rounds:
        title = f"Round {number}"
        entries = fetch_ranking(api, cache, {"roundId": round_id}, live, title)
        result.detail_lines.append(describe(title, entries))
        if not found and any(e.get("item") for e in entries):
            found = True
            result.round_label = str(number)
            result.round_tiers = find_cutoffs(entries)[0]
        if found and not detail:
            break

    title = "Overall battle"
    entries = fetch_ranking(api, cache, {"battleId": battle["_id"]}, active, title)
    result.detail_lines.append(describe(title, entries))
    result.battle_tiers = find_cutoffs(entries)[0]
    return result


def load_battle_config(api: Api) -> dict:
    try:
        return api.call("gameConfig.getGameConfig").get("battle") or {}
    except FatalApiError:
        raise
    except ApiError as exc:
        print(f"Warning: no game config, so no round timer ({exc})", file=sys.stderr)
        return {}


def scan(api: Api, cache: Cache, args, config: dict) -> int:
    results: list[BattleResult] = []
    error = None
    try:
        names = load_names(api, cache)
        battles = fetch_battles(api, args.ended)
        if args.battle:
            needle = args.battle.lower()
            battles = [
                b for b in battles
                if needle in battle_label(b, names).lower() or needle == b["_id"].lower()
            ]
        print(f"{len(battles)} battles found", file=sys.stderr)
        for index, battle in enumerate(battles, start=1):
            label = battle_label(battle, names)
            print(f"[{index}/{len(battles)}] {label}", file=sys.stderr)
            results.append(scan_battle(api, cache, battle, label, bool(args.battle), config, names))
    except ApiError as exc:
        error = exc

    if args.battle and not results and not error:
        print(f"No battle matches '{args.battle}'.")
    if results:
        print_report(results, "BY DAMAGE", by_damage)
        print_report(results, "ENDING SOONEST", by_time_left)
        if args.battle:
            for result in results:
                print(f"\n{result.label}")
                print("\n".join(result.detail_lines))
        print(f"\nUpdated at {time.strftime('%H:%M:%S')} ({api.requests_made} requests in total)")
    if error:
        print(f"\nStopped early: {error}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimum damage for tier 3 to 6 drops per WarEra battle")
    parser.add_argument("--ended", type=int, default=0, help="also include the last N ended battles")
    parser.add_argument("--battle", help="only battles with this text in the name or ID, with round details")
    parser.add_argument("--loop", type=int, metavar="SECONDS", help="refresh repeatedly (minimum 60 seconds)")
    parser.add_argument("--requests-per-minute", type=float, default=60, help="request limit (default 60)")
    parser.add_argument("--cache", default="warera_cache.sqlite")
    parser.add_argument("--base-url", default=BASE_URL, help=argparse.SUPPRESS)
    parser.add_argument("--debug-ranking", metavar="ROUND_ID", help="print the raw response for a round and exit")
    args = parser.parse_args()

    api = Api(args.base_url, args.requests_per_minute, os.environ.get("WARERA_API_KEY"))

    if args.debug_ranking:
        data = api.call(
            "battleRanking.getRanking",
            {"roundId": args.debug_ranking, "dataType": "damage", "type": "user",
             "side": MERGED, "limit": 3},
        )
        print(json.dumps(data, indent=2))
        return 0

    cache = Cache(args.cache)
    try:
        config = load_battle_config(api)
        while True:
            status = scan(api, cache, args, config)
            if not args.loop or status:
                return status
            time.sleep(max(args.loop, 60))
    except FatalApiError as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())