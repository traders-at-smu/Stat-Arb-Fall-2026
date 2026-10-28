def eligible(side, tradable, shortable):
    return tradable and (side == 'BUY' or (side == 'SELL' and shortable))


def exit_side(entry_side):
    return {'BUY': 'SELL', 'SELL': 'BUY'}[entry_side]


def test_buy_allowed():
    assert eligible('BUY', True, False)


def test_short_blocked():
    assert not eligible('SELL', True, False)


def test_short_allowed():
    assert eligible('SELL', True, True)


def test_long_exit():
    assert exit_side('BUY') == 'SELL'


def test_short_exit():
    assert exit_side('SELL') == 'BUY'
