"""Prueba del parser de % del público con las dos formas conocidas del JSON."""
import sys
sys.path.insert(0, ".")
from pipeline.ingest import parse_public_game, _an_teams  # noqa: E402

V1 = {"home_team_id": 1, "away_team_id": 2,
      "teams": [{"id": 1, "abbr": "KC"}, {"id": 2, "abbr": "LAR"}],
      "odds": [{"book_id": 30, "ml_home": -200},
               {"book_id": 15, "ml_home": -210, "spread_home": -4.5,
                "spread_home_public": 78, "spread_away_public": 22,
                "ml_home_public": 81, "ml_away_public": 19,
                "total_over_public": 64, "total_under_public": 36,
                "spread_home_money": 70, "num_bets": 12345}]}
V2 = {"home_team_id": 1, "away_team_id": 2,
      "teams": [{"id": 1, "abbr": "WSH"}, {"id": 2, "abbr": "JAC"}],
      "markets": {"15": {"event": {
          "spread": [{"side": "home", "bet_info": {"tickets": {"percent": 35}, "money": {"percent": 52}}},
                     {"side": "away", "bet_info": {"tickets": {"percent": 65}, "money": {"percent": 48}}}],
          "moneyline": [{"side": "home", "bet_info": {"tickets": {"percent": 40}}}],
          "total": [{"side": "over", "bet_info": {"tickets": {"percent": 55}}}]}}}}

a = parse_public_game(V1)
assert a["spread_home"] == 78 and a["spread_away"] == 22, a
assert a["ml_home"] == 81 and a["over"] == 64 and a["spread_home_money"] == 70, a
assert _an_teams(V1) == ("KC", "LA")
b = parse_public_game(V2)
assert b["spread_home"] == 35 and b["spread_away"] == 65, b
assert b["ml_home"] == 40 and b["ml_away"] == 60, b
assert b["over"] == 55 and b["under"] == 45, b
assert b["spread_home_money"] == 52, b
assert _an_teams(V2) == ("WAS", "JAX")
assert parse_public_game({"odds": [{"book_id": 15, "ml_home": -110}]}) == {}
print("parser del público: OK (v1, v2, vacío)")
