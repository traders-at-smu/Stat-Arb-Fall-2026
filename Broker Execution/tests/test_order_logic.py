def validate_order(side, quantity, tradable, shortable):
    if quantity <= 0:
        return False
    if not tradable:
        return False
    if side == 'SELL' and (not shortable):
        return False
    return side in ('BUY', 'SELL')


def test_valid_buy():
    assert validate_order('BUY', 10, True, False)


def test_valid_short():
    assert validate_order('SELL', 10, True, True)


def test_block_unavailable_short():
    assert not validate_order('SELL', 10, True, False)


def test_block_invalid_quantity():
    assert not validate_order('BUY', 0, True, True)
