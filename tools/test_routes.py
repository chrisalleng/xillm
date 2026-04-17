#!/usr/bin/env python3
"""
test_routes.py

Unit tests for cross-zone route planning (zone_transitions.zone_astar).

The zone_astar algorithm is ported here from zone_transitions.lua so tests
can run without a Lua runtime.  The port must stay in sync with the Lua
implementation; if you change zone_astar in gen_zone_transitions.py, update
zone_astar_py below as well.

Tests use a mock step_cost_fn to represent known FFXI terrain constraints
without depending on player-specific navmesh data files.  The mock encodes
the following known fact:
  North Gustaberg (106) has an impassable wall that isolates the coastal area
  (accessible from Port Bastok) from the interior (accessible from South
  Gustaberg).  A character entering from Port Bastok can only return to Port
  Bastok; they cannot reach Konschtat Highlands or any interior zone.
"""

import math
import os
import sys
import unittest

# ---------------------------------------------------------------------------
# Import zone transition data from the generator
# ---------------------------------------------------------------------------
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS_DIR)

from gen_zone_transitions import parse_zone_settings, parse_zonelines

ZONE_SETTINGS_PATH = os.path.join(TOOLS_DIR, 'zone_settings.sql')
ZONELINES_PATH     = os.path.join(TOOLS_DIR, 'zonelines.sql')

# ---------------------------------------------------------------------------
# Python port of zone_transitions.lua  zone_astar()
# Keep in sync with the Lua implementation in gen_zone_transitions.py.
# ---------------------------------------------------------------------------
def zone_astar_py(from_zone, to_zone, start_x, start_y, exits, step_cost_fn=None):
    """
    Dijkstra over (zone, entry_position) states.

    exits: dict  {zone_id: [{fx, fy, to_zone, tx, ty}, ...]}
    step_cost_fn(zone_id, from_x, from_y, exit_dict) -> float | None
        Returns the cost of crossing zone_id from (from_x, from_y) to the
        exit's trigger position, or None if the exit is unreachable.
        When not provided falls back to Euclidean distance.

    Returns list of transition dicts [{from_zone, fx, fy, to_zone, tx, ty}]
    or None if no route exists.
    """
    if from_zone == to_zone:
        return []

    def skey(zone, x, y):
        return f'{zone}:{x:.2f}:{y:.2f}'

    start_key = skey(from_zone, start_x, start_y)
    best      = {start_key: 0.0}
    prev      = {}          # key -> {transition, parent_key}
    visited   = set()
    open_list = [{'key': start_key, 'zone': from_zone,
                  'x': start_x, 'y': start_y, 'cost': 0.0}]

    while open_list:
        open_list.sort(key=lambda e: e['cost'])
        cur = open_list.pop(0)

        if cur['key'] in visited:
            continue
        visited.add(cur['key'])

        if cur['zone'] == to_zone:
            # Reconstruct path
            path = []
            k = cur['key']
            while k in prev:
                entry = prev[k]
                path.insert(0, entry['transition'])
                k = entry['parent_key']
            return path

        for ex in exits.get(cur['zone'], []):
            nkey = skey(ex['to_zone'], ex['tx'], ex['ty'])

            if step_cost_fn is not None:
                step = step_cost_fn(cur['zone'], cur['x'], cur['y'], ex)
                if step is None:
                    continue    # unreachable per navmesh
            else:
                dx = ex['fx'] - cur['x']
                dy = ex['fy'] - cur['y']
                step = math.sqrt(dx*dx + dy*dy)

            new_cost = cur['cost'] + step

            if nkey not in visited and (nkey not in best or new_cost < best[nkey]):
                best[nkey] = new_cost
                prev[nkey] = {
                    'transition': {
                        'from_zone': cur['zone'],
                        'fx': ex['fx'], 'fy': ex['fy'],
                        'to_zone': ex['to_zone'],
                        'tx': ex['tx'], 'ty': ex['ty'],
                    },
                    'parent_key': cur['key'],
                }
                found = False
                for e in open_list:
                    if e['key'] == nkey:
                        e['cost'] = new_cost
                        found = True
                        break
                if not found:
                    open_list.append({
                        'key': nkey, 'zone': ex['to_zone'],
                        'x': ex['tx'], 'y': ex['ty'],
                        'cost': new_cost,
                    })

    return None     # no path found


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------
def build_exits(transitions):
    """Convert list of (from_z, fx, fy, to_z, tx, ty) tuples to exits dict."""
    exits = {}
    for (from_z, fx, fy, to_z, tx, ty) in transitions:
        exits.setdefault(from_z, []).append(
            {'fx': fx, 'fy': fy, 'to_zone': to_z, 'tx': tx, 'ty': ty})
    return exits


# Well-known zone IDs
ZONE = {
    'bastok_mines':      234,
    'bastok_markets':    235,
    'port_bastok':       236,
    'south_gustaberg':   107,
    'north_gustaberg':   106,
    'konschtat':         108,
    'lower_jeuno':       245,
}

# North Gustaberg approximate entry positions:
#   From Port Bastok:        (660, 306)  — coastal, isolated by impassable cliff
#   From South Gustaberg #1: (0,   -75) — interior, connects to Konschtat etc.
#   From South Gustaberg #2: (-440,-273) — interior, connects to Konschtat etc.
NG_COAST_X,    NG_COAST_Y    = 660.0,  306.0
NG_INTERIOR_X, NG_INTERIOR_Y =   0.0,  -75.0


def navmesh_step_cost(zone_id, from_x, from_y, exit_dict):
    """
    Mock step_cost_fn encoding North Gustaberg's impassable coastal wall.

    North Gustaberg (106):
      - Entering from the coast (from_x > 400): only the Port Bastok exit
        is reachable; everything else returns None (wall blocks access).
      - Entering from the interior: Euclidean distance to all exits.

    All other zones: Euclidean distance (no wall data available).
    """
    if zone_id == ZONE['north_gustaberg']:
        is_coastal_entry = from_x > 400
        if is_coastal_entry:
            # Only the Port Bastok exit is accessible from the coast
            if exit_dict['to_zone'] == ZONE['port_bastok']:
                dx = exit_dict['fx'] - from_x
                dy = exit_dict['fy'] - from_y
                return math.sqrt(dx*dx + dy*dy)
            return None     # all other exits blocked by the cliff wall

    # Default: Euclidean distance
    dx = exit_dict['fx'] - from_x
    dy = exit_dict['fy'] - from_y
    return math.sqrt(dx*dx + dy*dy)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestZoneAstar(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        zone_names  = parse_zone_settings(ZONE_SETTINGS_PATH)
        transitions = parse_zonelines(ZONELINES_PATH)
        cls.exits       = build_exits(transitions)
        cls.zone_names  = zone_names   # {zone_id: name}

    def zone_name(self, zone_id):
        return self.zone_names.get(zone_id, str(zone_id))

    # ------------------------------------------------------------------
    # Port Bastok → Lower Jeuno
    # ------------------------------------------------------------------
    def test_port_bastok_to_lower_jeuno_first_step_is_bastok_markets(self):
        """
        From Port Bastok the route to Lower Jeuno must first go to Bastok
        Markets, not North Gustaberg.  The coastal entrance to North Gustaberg
        from Port Bastok is a dead end (impassable wall blocks access to the
        zone interior and therefore to Konschtat Highlands).
        """
        route = zone_astar_py(
            from_zone = ZONE['port_bastok'],
            to_zone   = ZONE['lower_jeuno'],
            start_x   = 0.0, start_y = 0.0,
            exits     = self.exits,
            step_cost_fn = navmesh_step_cost,
        )

        self.assertIsNotNone(route,
            'No route found from Port Bastok to Lower Jeuno')
        self.assertGreater(len(route), 0,
            'Route is unexpectedly empty')

        first_zone = route[0]['to_zone']
        self.assertEqual(
            first_zone, ZONE['bastok_markets'],
            f'Expected first step to Bastok Markets ({ZONE["bastok_markets"]}), '
            f'got {self.zone_name(first_zone)} ({first_zone}).\n'
            f'Full route: ' + ' -> '.join(
                self.zone_name(t['to_zone']) for t in route))

    def test_port_bastok_to_lower_jeuno_avoids_ng_coast(self):
        """
        The route must not pass through North Gustaberg via the Port Bastok
        entrance (tx≈660, ty≈306).  That arrival state is a coastal dead end.
        """
        route = zone_astar_py(
            from_zone = ZONE['port_bastok'],
            to_zone   = ZONE['lower_jeuno'],
            start_x   = 0.0, start_y = 0.0,
            exits     = self.exits,
            step_cost_fn = navmesh_step_cost,
        )
        self.assertIsNotNone(route)

        for t in route:
            if t['to_zone'] == ZONE['north_gustaberg']:
                self.assertFalse(
                    t['tx'] > 400,
                    f'Route enters North Gustaberg via the coastal dead end '
                    f'(tx={t["tx"]:.1f}, ty={t["ty"]:.1f}).  '
                    f'Should enter via South Gustaberg instead.')

    # ------------------------------------------------------------------
    # Same-zone route is empty
    # ------------------------------------------------------------------
    def test_same_zone_returns_empty(self):
        route = zone_astar_py(
            from_zone = ZONE['port_bastok'],
            to_zone   = ZONE['port_bastok'],
            start_x   = 0.0, start_y = 0.0,
            exits     = self.exits,
        )
        self.assertEqual(route, [])

    # ------------------------------------------------------------------
    # Without navmesh data the planner still finds *a* route
    # ------------------------------------------------------------------
    def test_port_bastok_to_lower_jeuno_euclidean_finds_route(self):
        """
        Without a step_cost_fn the planner uses Euclidean distance and may
        pick a geometrically shorter but practically blocked route.  We only
        assert that some route is found — not which one — because we have no
        wall information.
        """
        route = zone_astar_py(
            from_zone = ZONE['port_bastok'],
            to_zone   = ZONE['lower_jeuno'],
            start_x   = 0.0, start_y = 0.0,
            exits     = self.exits,
        )
        self.assertIsNotNone(route)
        self.assertGreater(len(route), 0)

    # ------------------------------------------------------------------
    # Bastok Mines → Lower Jeuno: should not enter NG via coast
    # ------------------------------------------------------------------
    def test_bastok_mines_to_lower_jeuno_avoids_ng_coast(self):
        """
        Starting from Bastok Mines the route must not enter North Gustaberg
        via the Port Bastok coastal entrance.
        """
        route = zone_astar_py(
            from_zone = ZONE['bastok_mines'],
            to_zone   = ZONE['lower_jeuno'],
            start_x   = 0.0, start_y = 0.0,
            exits     = self.exits,
            step_cost_fn = navmesh_step_cost,
        )
        self.assertIsNotNone(route,
            'No route found from Bastok Mines to Lower Jeuno')

        for t in route:
            if t['to_zone'] == ZONE['north_gustaberg']:
                self.assertFalse(
                    t['tx'] > 400,
                    f'Route enters North Gustaberg via the coastal dead end '
                    f'(tx={t["tx"]:.1f}).  Full route: ' +
                    ' -> '.join(self.zone_name(s['to_zone']) for s in route))


if __name__ == '__main__':
    unittest.main(verbosity=2)
