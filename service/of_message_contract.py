"""Native chat message formatting only; no transport, keys or account registry.

Kept byte-compatible with the existing OFClient send_message body. The legacy
client still owns tag enrichment and its behavior; canonical authority adapters
call only this pure formatter and do not inherit retries or mutable tags.
"""

def wrap_text_as_p(text: str) -> str:
    """OF web's chat textarea is a rich-text editor that always emits a
    `<p>…</p>` wrapper. Plain-text sends to fans work without wrapping,
    but creator-to-creator sends 400 unless wrapped — match OF web's
    body exactly here. Multi-paragraph text becomes one `<p>` with
    `<br>` separators, mirroring what the OF editor produces."""
    if not text:
        return text
    stripped = text.strip()
    # Already HTML? Don't double-wrap.
    if stripped.startswith("<p") or stripped.startswith("<P"):
        return text
    # Internal newlines → <br>, then wrap the whole thing.
    escaped = (
        stripped
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br />")
    )
    return f"<p>{escaped}</p>"

def message_body(text, *, locked_text=False, price=0, media_files=None,
                 previews=None, is_forward=False, reply_to_message_id=None,
                 giphy_id=None):
    body = {
        "text": "" if (giphy_id and not text) else wrap_text_as_p(text),
        "lockedText": locked_text, "mediaFiles": media_files or [], "price": price,
        "previews": previews or [], "rfTag": [], "rfGuest": [], "rfPartner": [],
        "isForward": is_forward,
    }
    if giphy_id:
        body["giphyId"] = str(giphy_id)
    if reply_to_message_id is not None:
        body["replyToMessageId"] = reply_to_message_id
    return body
