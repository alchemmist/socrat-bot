"""Native photo transport adapter; durable snapshots retain the original full text."""

import json

from sqlalchemy.orm import defer

from socrat_bot.models import GraphSnapshot


def adapt(db, incident, card, existing, method, body, snapshot):
    is_photo = bool(existing and card.message_kind == "photo")
    desired = (
        incident.graph_capture
        if incident.graph_generation == incident.generation
        else None
    )
    capture = (
        db.get(GraphSnapshot, desired, options=[defer(GraphSnapshot.png)])
        if desired
        else None
    )
    snapshot["_message_kind"] = "photo" if is_photo else "text"
    snapshot["_graph_capture"] = card.graph_capture if existing else None
    # Old text messages keep their identity/type, including rollout-era fallbacks.
    if existing and not is_photo:
        return method, body
    if is_photo and len(body["text"]) > 1024:
        # Telegram cannot turn a photo into arbitrary-length text. Preserve the
        # complete text as the new primary, retire old buttons AFTER acceptance.
        snapshot["_supersedes_photo"] = card.message_id
        snapshot["_message_kind"], snapshot["_graph_capture"] = "text", None
        body = {k: v for k, v in body.items() if k != "message_id"}
        return "sendMessage", body
    if is_photo:
        if capture and capture.id != card.graph_capture and capture.png:
            body["_graph_data"] = capture.png
            snapshot["_graph_capture"] = capture.id
            return "editMessageMedia", body
        return "editMessageCaption", body
    # Reserve caption headroom for subsequent SPI/escalation metadata. Oversized
    # initial messages preserve their full existing text and buttons, without PNG.
    if not existing and capture and len(body["text"]) <= 768 and capture.png:
        body["_graph_data"] = capture.png
        snapshot["_message_kind"], snapshot["_graph_capture"] = "photo", capture.id
        return "sendPhoto", body
    return method, body


async def post(client, url, method, body):
    text = body.get("text", "")
    if method == "editMessageReplyMarkup":
        return await client.post(
            url, json={k: body[k] for k in ("chat_id", "message_id", "reply_markup")}
        )
    if method == "editMessageCaption":
        payload = {k: v for k, v in body.items() if k not in ("text", "_graph_data")}
        payload["caption"] = text
        return await client.post(url, json=payload)
    if method in ("sendPhoto", "editMessageMedia"):
        raw = body["_graph_data"]
        payload = {k: v for k, v in body.items() if k not in ("text", "_graph_data")}
        if method == "sendPhoto":
            payload["caption"] = text
        else:
            payload.pop("parse_mode", None)
            payload["media"] = {
                "type": "photo",
                "media": "attach://graph",
                "caption": text,
                "parse_mode": "HTML",
            }
        payload = {
            k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            for k, v in payload.items()
        }
        return await client.post(
            url,
            data=payload,
            files={
                "photo" if method == "sendPhoto" else "graph": (
                    "graph.png",
                    raw,
                    "image/png",
                )
            },
        )
    return await client.post(url, json=body)
