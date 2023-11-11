from pymongo import MongoClient
import pandas as pd
from datetime import datetime
import json


db_client = MongoClient('mongodb://127.0.0.1:27017/')
db_other = db_client['holdings_trades_signals_master']
# coll = db_other['signals']
# coll = db_other['trades']
coll = db_other['portfolio']


pf = coll.find_one({}, {"_id": 0})  # portfolio

if balance_history := list(list(pf['balance_history'].values())[1:]):
    winners_r, losers_r, total_r = [], [], []

    for transaction in balance_history:
        trade = pf['trades'][transaction['trade_id']]
        entry = trade['position']['avg_entry_price']
        stop = list(trade['orders'].values())[-1]['price']
        exit = trade["exit_price"]
        rr = (exit - entry) / (entry - stop)

        total_r.append(rr)

        if transaction['amt'] > 0:
            winners_r.append(rr)

        elif transaction['amt'] < 0:
            losers_r.append(rr)

    # 'avg_r_per_trade'
    pf['avg_r_per_trade'] = round(sum(total_r) / len(total_r), 2)

    # 'avg_r_per_winner'
    pf['avg_r_per_winner'] = round(sum(winners_r) / len(winners_r), 2)

    # 'avg_r_per_loser'
    pf['avg_r_per_loser'] = sround(sum(losers_r) / len(losers_r), 2)

    # 'win_loss_ratio'
    if pf['total_winning_trades']:
        if pf['total_losing_trades']:
            pf['win_loss_ratio'] = pf['total_winning_trades'] / pf['total_losing_trades']
        else:
            pf['win_loss_ratio'] = pf['total_winning_trades']

    # 'gain_to_pain_ratio'
    # TODO

    print(pf['avg_r_per_trade'], pf['avg_r_per_winner'], pf['avg_r_per_loser'])


# print(json.dumps(pf['trades'][trade_id], indent=2))