"""
simulate.py — Entry point for the supply chain simulation.

Usage:
  python simulate.py \
    --sim_id 30000000-0000-0000-0000-000000000001 \
    --account_id 10000000-0000-0000-0000-000000000001
"""

from simulation.connections import connect
from simulation.config import load_config
from simulation.data_loader import load_data
from simulation.runner import run
from simulation.state import build_initial_state

if __name__ == '__main__':
    conns  = connect()
    config = load_config(conns.pg_conn)
    data   = load_data(conns, config)
    state  = build_initial_state(config, data)
    run(config, state, data, conns.ch)
