"""W6MW-21: vision.is_refusal missed the stock 40-char refusal and longer apologies (stored as a description)."""
import vision


def test_w6mw_21_stock_refusal_is_a_refusal():
    text = "I'm sorry, but I can't assist with that."
    assert len(text) == 40
    assert vision.is_refusal(text)
    assert vision.is_refusal("I'm sorry, but I can't help describe this image as requested.")
    assert vision.is_refusal("I\u2019m sorry, but I can\u2019t assist with that.")
