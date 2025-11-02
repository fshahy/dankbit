def calculate_oi(strike, trades):
    oi_call = 0
    oi_put = 0

    for t in trades:
        qty = t['amount']

        if t['option_type'].lower() == 'call':
            oi_call += qty if t['direction'] == 'buy' else -qty
        elif t['option_type'].lower() == 'put':
            oi_put += qty if t['direction'] == 'buy' else -qty

    return oi_call, oi_put
