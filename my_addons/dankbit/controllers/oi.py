def calculate_oi(trades):
    oi_call = 0.0
    oi_put = 0.0
    for t in trades:
        qty = t.amount or 0.0
        if t.option_type == "call":
            oi_call += qty if t.direction == "buy" else -qty
        elif t.option_type == "put":
            oi_put += qty if t.direction == "buy" else -qty
    return oi_call, oi_put
