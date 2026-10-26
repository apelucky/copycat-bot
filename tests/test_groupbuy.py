from copycat.core.groupbuy import GroupBuyDetector, scale_out_steps


def test_two_wallets_within_window_form_group():
    d = GroupBuyDetector(window_s=600, entry_at=2)
    g, e = d.on_buy("solana", "T", "w1", 1000, 1.0, "f1")
    assert e is None
    g, e = d.on_buy("solana", "T", "w2", 1300, 2.0, "f2")
    assert e == "formed" and g.wallets == ["w1", "w2"]
    g, e = d.on_buy("solana", "T", "w3", 1500, 1.0, "f3")
    assert e == "joined" and g.effective_count == 3


def test_outside_window_does_not_form():
    d = GroupBuyDetector(window_s=600)
    d.on_buy("solana", "T", "w1", 1000, 1.0, None)
    g, e = d.on_buy("solana", "T", "w2", 1000 + 601, 1.0, None)
    assert e is None and g.wallets == ["w2"]


def test_same_wallet_twice_is_not_two():
    d = GroupBuyDetector()
    d.on_buy("solana", "T", "w1", 1000, 1.0, None)
    _, e = d.on_buy("solana", "T", "w1", 1010, 1.0, None)
    assert e is None


def test_shared_funder_counts_once():
    d = GroupBuyDetector()
    d.on_buy("solana", "T", "w1", 1000, 1.0, "FUNDER")
    g, e = d.on_buy("solana", "T", "w2", 1010, 1.0, "FUNDER")
    assert e is None and g.effective_count == 1
    assert g.shared_funders() == {"FUNDER": ["w1", "w2"]}
    g, e = d.on_buy("solana", "T", "w3", 1020, 1.0, "other")
    assert e == "formed" and g.effective_count == 2


def test_early_buyer_sell_triggers_exit_but_late_does_not():
    d = GroupBuyDetector()
    for i, w in enumerate(["w1", "w2", "w3"]):
        d.on_buy("solana", "T", w, 1000 + i, 1.0, w)
    _, early = d.on_sell("solana", "T", "w3")
    assert not early
    g, early = d.on_sell("solana", "T", "w1")
    assert early and g.exited


def test_scale_out_steps():
    assert scale_out_steps(2, 2, False) == 0
    assert scale_out_steps(3, 2, False) == 1
    assert scale_out_steps(5, 2, False) == 3
    assert scale_out_steps(3, 2, True) == 2
    assert scale_out_steps(9, 2, True) == 3


def test_dump_load_roundtrip():
    d = GroupBuyDetector()
    d.on_buy("base", "0xT", "a", 1, 0.1, None)
    d2 = GroupBuyDetector()
    d2.load(d.dump())
    assert d2.groups["base:0xT"].wallets == ["a"]
