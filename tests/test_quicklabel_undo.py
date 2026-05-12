"""Tests for the reverse_backprop helper that powers Undo."""
from __future__ import annotations

from quicklabel.service import reverse_backprop


class _FakeMessages:
    def __init__(self):
        self.calls: list[dict] = []
    def batchModify(self, *, userId, body):
        self.calls.append(body)
        class _R:
            def execute(self_inner): return {}
        return _R()


class _FakeUsers:
    def __init__(self, msgs): self._m = msgs
    def messages(self): return self._m


class FakeService:
    def __init__(self):
        self.messages = _FakeMessages()
        self._u = _FakeUsers(self.messages)
    def users(self): return self._u


def test_reverse_swaps_add_and_remove():
    """reverse_backprop should add what was previously removed and vice versa."""
    svc = FakeService()
    reverse_backprop(
        svc, message_ids=["m1", "m2"],
        add_label_ids=["LBL_X", "INBOX"],
        remove_label_ids=["UNREAD"],
    )
    assert len(svc.messages.calls) == 1
    body = svc.messages.calls[0]
    assert body["ids"] == ["m1", "m2"]
    # Originally added (LBL_X, INBOX) -> now removed
    assert body["removeLabelIds"] == ["LBL_X", "INBOX"]
    # Originally removed (UNREAD) -> now added
    assert body["addLabelIds"] == ["UNREAD"]


def test_reverse_handles_empty_message_list():
    svc = FakeService()
    reverse_backprop(svc, message_ids=[],
                     add_label_ids=["X"], remove_label_ids=["Y"])
    assert svc.messages.calls == []


def test_reverse_chunks_into_batches():
    svc = FakeService()
    ids = [f"m{i}" for i in range(2500)]
    reverse_backprop(svc, message_ids=ids,
                     add_label_ids=["X"], remove_label_ids=[])
    # Default batch size is 1000 -> 3 batches
    assert len(svc.messages.calls) == 3
    assert len(svc.messages.calls[0]["ids"]) == 1000
    assert len(svc.messages.calls[1]["ids"]) == 1000
    assert len(svc.messages.calls[2]["ids"]) == 500


def test_reverse_drops_none_for_empty_lists():
    """When the original add (now-remove) is empty, we shouldn't pass an empty list."""
    svc = FakeService()
    reverse_backprop(svc, message_ids=["m1"],
                     add_label_ids=[], remove_label_ids=["X"])
    body = svc.messages.calls[0]
    # add_label_ids in batchModify call must be 'X' (now added back)
    assert body.get("addLabelIds") == ["X"]
    # remove_label_ids: was [], so shouldn't be present
    assert "removeLabelIds" not in body or body["removeLabelIds"] is None
