"""Regression for real vault responses with more than 100 folder states."""
import json

import pytest

from onlyfans_runtime.native_protocol import NativeProtocolError
from onlyfans_runtime.vault_management import MAX_FOLDER_STATES, parse_vault_page


def page(folder_states):
    return json.dumps({"list": [{
        "id": 11, "type": "photo", "createdAt": "2026-10-19T11:43:37+00:00",
        "isReady": True, "canView": True, "duration": 0, "listStates": folder_states,
    }], "hasMore": False}).encode()


def folders(count):
    return [{"id": i + 1, "type": "custom", "name": "Synthetic folder",
             "hasMedia": i == 0, "canAddMedia": True} for i in range(count)]


@pytest.mark.parametrize("count", [0, 100, 104, MAX_FOLDER_STATES])
def test_supported_folder_states_are_preserved(count):
    states = folders(count)
    rows, more = parse_vault_page(page(states), owner_id=123)
    assert rows[0]["listStates"] == states
    assert more is False


@pytest.mark.parametrize("states", [folders(MAX_FOLDER_STATES + 1), {}, "invalid",
                                     [True], [{"id": 1}, None]])
def test_invalid_or_excessive_folder_states_still_fail(states):
    with pytest.raises(NativeProtocolError):
        parse_vault_page(page(states), owner_id=123)
