"""Receipt evidence that an outgoing reply did not cover a later fan message.

Used by candidate selection, the durable source gate and the native turn gate.
No history or receipt is edited. Human, external and unsettled sends stay barriers.
"""

_JOINS = (
    "native_pilot_receipts r "
    "JOIN native_pilot_inputs i ON i.input_key=r.input_key AND i.pilot_id=r.pilot_id "
    "JOIN native_pilots p ON p.pilot_id=r.pilot_id "
    "JOIN messages s ON s.account_id=p.account_id AND s.fan_id=p.fan_id AND s.message_id=i.message_id"
)


def _evidence(inbound, outbound):
    return (
        f"{outbound}.direction='out' AND {outbound}.account_id={inbound}.account_id "
        f"AND {outbound}.fan_id={inbound}.fan_id "
        f"AND p.account_id={outbound}.account_id AND p.fan_id={outbound}.fan_id "
        f"AND r.provider_message_id=CAST({outbound}.message_id AS TEXT) "
        "AND r.state='sent' AND i.state='complete' AND s.direction='in' "
        f"AND CAST({inbound}.ingested_at AS TEXT)>i.claimed_at "
        f"AND (s.created_at<{inbound}.created_at OR "
        f"(s.created_at={inbound}.created_at AND s.message_id<{inbound}.message_id)) "
        f"AND ({outbound}.created_at>{inbound}.created_at OR "
        f"({outbound}.created_at={inbound}.created_at AND {outbound}.message_id>{inbound}.message_id))"
    )


def older_reply_sql(inbound="m", outbound="n"):
    """SQL aliases are internal literals, never request input."""
    return "EXISTS (SELECT 1 FROM " + _JOINS + " WHERE " + _evidence(inbound, outbound) + ")"


def overlapped_replies(con, account, fan, message_id):
    """Exact confirmed outputs, plus the source members they already answered."""
    return con.execute(
        "SELECT n.message_id,i.member_ids_json FROM messages m "
        "JOIN messages n ON n.account_id=m.account_id AND n.fan_id=m.fan_id, "
        + _JOINS + " WHERE " + _evidence("m", "n") + " "
        "AND m.account_id=? AND m.fan_id=? AND m.message_id=? AND m.direction='in' "
        "AND NOT n.is_unsent",
        (account, fan, message_id),
    ).fetchall()
