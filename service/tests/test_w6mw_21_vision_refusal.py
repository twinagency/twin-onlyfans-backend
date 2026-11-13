"""W6MW-21: vision.is_refusal missed the stock 40-char refusal and longer apologies (stored as a description)."""
import vision


def test_w6mw_21_stock_refusal_is_a_refusal():
    text = "I'm sorry, but I can't assist with that."
    assert len(text) == 40
    assert vision.is_refusal(text)
    assert vision.is_refusal("I'm sorry, but I can't help describe this image as requested.")
    assert vision.is_refusal("I\u2019m sorry, but I can\u2019t assist with that.")


def test_g05_refusal_is_anchored_at_the_opening():
    assert not vision.is_refusal("Chat screenshot: 'I can't wait to see you' from a fan, sent 10:42.")
    assert vision.is_refusal("I'm sorry, but I can't help with that request because it may contain content that "
                             "violates the usage policy; please try a different image instead.")
    assert vision.is_refusal("   ")


def test_g05_refusal_openings_cover_the_old_markers():
    for text in ("I'm not able to describe this image.", "I apologize, but I can't help with that.", "Apologies, I can't.",
                 "I'm afraid I can't help with that.", "Cannot assist with that request.", "Unable to describe this.",
                 "This image violates our content policy, so I can't describe it.", "I must decline this request."):
        assert vision.is_refusal(text), text
    assert not vision.is_refusal("A woman in a red dress smiling at the camera in a sunlit kitchen.")
