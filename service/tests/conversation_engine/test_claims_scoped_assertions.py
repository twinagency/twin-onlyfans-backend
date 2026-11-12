"""F1b follow-up (independent review of b10d8e1): questions, quotations and negation govern only the clause they are in, and
unrelated conversation is not a description of the attached product.

Pure unit tests, no database, no network. Every case runs through BOTH the real model judge() and the shared plain-text gate
that the operator edit uses (the authenticated operator-edit route has its own cases in test_engine_approval_api_pg.py).

Finding 1: a declaration followed by a tag question ("Here is your video, ready?") is still a declaration. A genuine question
(an interrogative clause) stays exempt, including a question that follows an ordinary lead-in.

Finding 2: on an offer card, a media noun that is plainly the writer's own unrelated activity ("I watched a movie last night")
is not a statement about the card. Anything that points at the card (here / this / your / a recipient / a presentation of the
noun right after) still is, and the false kind, count, duration and price controls are unchanged.

Documented decision (ambiguous case): "I made a video last night. Here it is." on a photo grant is REFUSED. The second sentence
is a bare presentation of the thing just named, so the pair tells the fan the attachment is that video. Without the pointer the
same first sentence is accepted ("I made a video last night. Enjoy these photos.").
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_claims_structural_authority import CLIP, MIXED, PHOTOS, outcome  # noqa: E402

PATHS = ['judge', 'shared_gate']

# --- Finding 1 ---------------------------------------------------------------------------------------------------------
TAG_QUESTION_AFTER_A_FALSE_CLAIM = [
    (PHOTOS, 'Here is your video, ready?'), (PHOTOS, 'Here is your video, right?'), (PHOTOS, 'Enjoy this video, okay?'),
    (PHOTOS, 'Your video is attached, see?'), (PHOTOS, 'Here are three videos for you, excited?'),
    (PHOTOS, 'Short and sweet, a hot clip, ready?'), (PHOTOS, 'New video, just dropped, yeah?'),
    (PHOTOS, 'Here is a video, babe, ready?'), (PHOTOS, 'Ready? Here is your video, excited?'),
    (PHOTOS, 'Here is your video, would you like it?'), (PHOTOS, 'Here is your video, want it?'),
    (PHOTOS, 'Here is your video, babe, you ready, yeah?'),
    (PHOTOS, 'Nine photos for you, ready?'), (PHOTOS, 'It is USD 5.00 for this set, deal?'), (PHOTOS, 'The photos cost four, okay?'),
    (PHOTOS, 'Here is your ten-minute photo set, excited?'),
    (CLIP, 'Here is your ten-minute video, excited?'), (CLIP, 'Enjoy this two minute video, ready?'),
    (CLIP, 'It lasts two minutes, ready?'), (CLIP, 'Here is your photo, ready?'), (CLIP, 'Two videos for you, ready?'),
    (MIXED, 'Two videos and a photo for you, ready?'), (MIXED, 'It lasts ten minutes, ready?'),
    # negation, a quotation and a condition scope to their own clause: a later declaration is still asserted
    (PHOTOS, 'This is not a drill, here is your video.'), (PHOTOS, 'No problem, here is your video, ready?'),
    (PHOTOS, 'You said "ready?" Here is your video.'), (PHOTOS, 'It is not a photo, here is your video, ready?'),
]

TRUTHFUL_OR_GENUINE_QUESTION = [
    # truthful declaration + tag
    (PHOTOS, 'Enjoy these photos, ready?'), (PHOTOS, 'Here are three photos for you, excited?'), (CLIP, 'Here is your video, excited?'),
    (CLIP, 'Here is your 90 second video, excited?'), (MIXED, 'Two photos and a video for you, ready?'),
    # genuine questions, including the browser journey clarification
    (PHOTOS, 'Would you like a video?'), (PHOTOS, 'Which particular photo or video would you like to see?'),
    (PHOTOS, 'Would you prefer a photo or a video, babe?'), (PHOTOS, 'Want a video or are photos better?'),
    (PHOTOS, 'Enjoy these photos, would you like a video next time?'), (PHOTOS, 'Hey babe, which video do you want?'),
    (PHOTOS, 'Do you want the video or the photos, or both?'), (PHOTOS, 'Is this a video, or a photo?'),
    (PHOTOS, 'Do you like videos, babe?'), (PHOTOS, 'Photo or video?'), (PHOTOS, 'Photo or video, babe?'),
    (PHOTOS, 'Did you like the video I sent yesterday?'), (PHOTOS, 'You want a video?'), (PHOTOS, 'Ready for a video next time?'),
    (PHOTOS, 'Enjoy these photos. Want a video too?'),
    # negation and quotation with a tag
    (PHOTOS, 'It is not a video, ready?'), (PHOTOS, 'This is no video, just photos, okay?'),
    (PHOTOS, 'You said "Your video is attached," right?'), (PHOTOS, 'Here are your photos, not a video, ready?'),
]

# --- Finding 2 ---------------------------------------------------------------------------------------------------------
UNRELATED_CONVERSATION_STAYS_LEGITIMATE = [
    (PHOTOS, 'I watched a movie last night. Enjoy these photos.'), (PHOTOS, 'I watched a movie last night, enjoy these photos.'),
    (PHOTOS, 'We saw a great film at the cinema. Enjoy these photos.'), (PHOTOS, 'My neighbour filmed a video of the sunset. Enjoy these photos.'),
    (PHOTOS, 'I am watching a movie right now. Enjoy these photos.'), (PHOTOS, 'I love watching movies. Enjoy these photos.'),
    (PHOTOS, 'We shoot videos every week. Enjoy these photos.'), (CLIP, 'I took five photos of my coffee. Enjoy this video.'), (PHOTOS, 'I watched a ten minute movie last night. Enjoy these photos.'),
    (PHOTOS, 'I watched two movies last night. Here are three photos for you.'),
    (PHOTOS, 'I watched a movie last night. Here are three photos for you.'), (PHOTOS, 'I made a video last night. Enjoy these photos.'),
    (CLIP, 'I took a photo of my coffee this morning. Enjoy this video.'), (CLIP, 'I snapped a few pictures at the gym. Enjoy this video.'),
    (CLIP, 'My neighbour took a picture of the sunset. Enjoy this video.'),
    (CLIP, 'I took a photo of my coffee this morning, and here is your video.'),
    (MIXED, 'I watched a movie last night. Two photos and a video in this bundle.'),
    (CLIP, 'I made a video last night. Here it is.'),                              # true on a video card, whatever the lead-in
]

STILL_A_CLAIM_ABOUT_THE_CARD = [
    (PHOTOS, "Here's the video I made last night."), (PHOTOS, 'Here is the video I made last night, ready?'),
    (PHOTOS, 'I made a video last night. Here it is.'),                            # documented decision: refused (see module docstring)
    (PHOTOS, 'I made this video last night.'), (PHOTOS, 'I made a video for you.'), (PHOTOS, 'I hope you like the video I made.'),
    (PHOTOS, 'I just sent you a video.'), (PHOTOS, 'I made you a video last night.'),
    (PHOTOS, 'I watched a movie last night. Enjoy this movie.'), (PHOTOS, 'I watched a movie last night and here is your video.'),
    (PHOTOS, 'I filmed a video last night. Enjoy it.'), (PHOTOS, 'I am sending a video.'),
    (PHOTOS, 'I love the video.'), (PHOTOS, 'I love this video.'), (PHOTOS, 'I love watching movies. Enjoy this movie.'),
    (CLIP, 'Here is the photo I took this morning.'), (CLIP, 'I took a photo this morning. Here it is.'),
    (CLIP, 'I took a photo of my coffee this morning. Enjoy it.'),
    # the false kind / count / duration / price controls still apply next to unrelated conversation
    (PHOTOS, 'I watched a movie last night. Enjoy this ten-minute photo set.'), (PHOTOS, 'I watched a movie last night. It is USD 5.00 for this set.'),
    (PHOTOS, 'Nine photos for you. I watched a movie last night.'), (PHOTOS, 'I watched a movie last night. Here is a video for you.'),
    (MIXED, 'I watched a movie last night. Two videos and a photo.'),
]


def _ids(cases):
    return [f'{c[0]["grantId"]}:{c[1]}' for c in cases]


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('case', TAG_QUESTION_AFTER_A_FALSE_CLAIM, ids=_ids(TAG_QUESTION_AFTER_A_FALSE_CLAIM))
def test_a_tag_question_does_not_excuse_the_declaration_before_it(path, case):
    selected, body = case
    accepted, reasons = outcome(path, selected, body)
    assert accepted is False, (path, selected['grantId'], body)


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('case', TRUTHFUL_OR_GENUINE_QUESTION, ids=_ids(TRUTHFUL_OR_GENUINE_QUESTION))
def test_truthful_wording_genuine_questions_and_negation_stay_accepted(path, case):
    selected, body = case
    accepted, reasons = outcome(path, selected, body)
    assert accepted is True, (path, selected['grantId'], body, reasons)


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('case', UNRELATED_CONVERSATION_STAYS_LEGITIMATE, ids=_ids(UNRELATED_CONVERSATION_STAYS_LEGITIMATE))
def test_the_writers_own_unrelated_activity_is_not_a_description_of_the_card(path, case):
    selected, body = case
    accepted, reasons = outcome(path, selected, body)
    assert accepted is True, (path, selected['grantId'], body, reasons)


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('case', STILL_A_CLAIM_ABOUT_THE_CARD, ids=_ids(STILL_A_CLAIM_ABOUT_THE_CARD))
def test_wording_that_points_at_the_card_is_still_checked_against_it(path, case):
    selected, body = case
    accepted, reasons = outcome(path, selected, body)
    assert accepted is False, (path, selected['grantId'], body)
