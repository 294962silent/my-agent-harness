from my_agent_core.message_queue import MessageQueue


def test_message_queue_basic():
    q = MessageQueue()
    assert len(q) == 0
    assert not q
    assert not q.has_steering()
    assert not q.has_followup()
    assert q.get_status() == "Queue empty"

    q.add_steering("Steer 1")
    assert len(q) == 1
    assert bool(q)
    assert q.has_steering()
    assert not q.has_followup()
    assert "1 steering" in q.get_status()

    q.add_followup("Follow 1")
    assert len(q) == 2
    assert q.has_steering()
    assert q.has_followup()
    assert "1 steering" in q.get_status() and "1 follow-up" in q.get_status()


def test_steering_one_at_a_time():
    q = MessageQueue(steering_mode="one-at-a-time")
    q.add_steering("S1")
    q.add_steering("S2")
    q.add_followup("F1")

    s_msgs = q.get_steering_messages()
    assert len(s_msgs) == 1
    assert s_msgs[0].content == "S1"
    assert q.has_steering()
    assert len(q) == 2

    s_msgs2 = q.get_steering_messages()
    assert len(s_msgs2) == 1
    assert s_msgs2[0].content == "S2"
    assert not q.has_steering()
    assert q.has_followup()


def test_steering_all():
    q = MessageQueue(steering_mode="all")
    q.add_steering("S1")
    q.add_steering("S2")
    q.add_followup("F1")

    s_msgs = q.get_steering_messages()
    assert len(s_msgs) == 2
    assert [m.content for m in s_msgs] == ["S1", "S2"]
    assert not q.has_steering()
    assert q.has_followup()


def test_followup_one_at_a_time_and_all():
    q = MessageQueue(followup_mode="one-at-a-time")
    q.add_followup("F1")
    q.add_followup("F2")

    f1 = q.get_followup_messages()
    assert len(f1) == 1
    assert f1[0].content == "F1"
    assert q.has_followup()

    q.followup_mode = "all"
    q.add_followup("F3")
    f_all = q.get_followup_messages()
    assert len(f_all) == 2
    assert [m.content for m in f_all] == ["F2", "F3"]
    assert not q.has_followup()


def test_clear_and_peek():
    q = MessageQueue()
    q.add_steering("S1")
    peeked = q.peek()
    assert peeked is not None and peeked.content == "S1"
    cleared = q.clear()
    assert len(cleared) == 1
    assert len(q) == 0
    assert q.peek() is None


def test_execution_owner_limits_consumers_to_current_or_legacy_input():
    q = MessageQueue(steering_mode="all", followup_mode="all")
    legacy = q.add_steering("legacy")
    current = q.add_followup("current", session_id="session", generation=2)
    future = q.add_steering("future", session_id="session", generation=3)
    foreign = q.add_followup("foreign", session_id="other", generation=2)

    q.set_execution_owner(session_id="session", generation=2)

    assert q.has_steering() is True
    assert q.has_followup() is True
    assert q.get_steering_messages() == [legacy]
    assert q.get_followup_messages() == [current]
    assert q.has_steering() is False
    assert q.has_followup() is False
    assert q.pop_next() is None
    assert q.queue == [future, foreign]

    q.clear_execution_owner(session_id="session", generation=3)
    assert q.pop_next() is None
    q.clear_execution_owner(session_id="session", generation=2)
    assert q.pop_next() is future
    assert q.pop_next() is foreign


def test_discard_stale_preserves_future_reservation():
    q = MessageQueue()
    legacy = q.add_steering("legacy")
    older = q.add_followup("older", session_id="session", generation=1)
    foreign = q.add_steering("foreign", session_id="other", generation=3)
    future = q.add_followup("future", session_id="session", generation=3)

    assert q.discard_stale(session_id="session", generation=2) == [older, foreign]
    assert q.queue == [legacy, future]
