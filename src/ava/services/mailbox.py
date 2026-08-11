"""Unread Gmail read out loud, over IMAP, standard library only.

Two lines in .env switch it on:
    GMAIL_USER=address@gmail.com
    GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   (Google app password)

The app password comes from https://myaccount.google.com/apppasswords (requires
two-step verification). Without credentials Ava simply opens Gmail in the
browser, as before. The mailbox is opened read-only: peeking at headers never
marks anything as read.
"""

from __future__ import annotations

from dataclasses import dataclass
import email
import email.header
import email.utils
import imaplib
import os
import re
import socket

from ava import net


IMAP_HOST = "imap.gmail.com"
IMAP_TIMEOUT_S = 8


@dataclass(frozen=True)
class MailSummary:
    available: bool          # False = no credentials / no network
    unread: int = 0
    previews: tuple = ()     # ((sender, subject), ...) newest first
    error: str = ""


def _decode(value: str) -> str:
    # MIME headers arrive as =?UTF-8?B?...?= often enough to matter.
    if not value:
        return ""
    parts = []
    for chunk, charset in email.header.decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return " ".join("".join(parts).split())


def sender_name(from_header: str) -> str:
    # "Jean Dupont <jean@x.com>" -> "Jean Dupont", else the part before the @.
    name, address = email.utils.parseaddr(_decode(from_header))
    if name:
        return name
    return address.split("@")[0] if address else "expéditeur inconnu"


def clean_subject(subject: str) -> str:
    value = _decode(subject)
    value = re.sub(r"^\s*((re|tr|fwd?)\s*:\s*)+", "", value, flags=re.I)
    return value.strip() or "sans objet"


def credentials() -> tuple[str, str]:
    return (
        os.getenv("GMAIL_USER", "").strip(),
        os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip(),
    )


def fetch_unread(limit: int = 4) -> MailSummary:
    user, password = credentials()
    if not user or not password:
        return MailSummary(False, error="no gmail credentials in .env")
    if not net.reachable("mails"):
        return MailSummary(False, error="offline")
    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, timeout=IMAP_TIMEOUT_S) as imap:
            imap.login(user, password)
            imap.select("INBOX", readonly=True)
            status, data = imap.search(None, "UNSEEN")
            if status != "OK":
                return MailSummary(False, error="imap search refused")
            ids = data[0].split()
            previews = []
            # Highest ids are the most recent; headers only, nothing downloaded.
            for msg_id in reversed(ids[-limit:]):
                status, msg_data = imap.fetch(
                    msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])",
                )
                if status != "OK" or not msg_data or msg_data[0] is None:
                    continue
                headers = email.message_from_bytes(msg_data[0][1])
                previews.append((
                    sender_name(headers.get("From", "")),
                    clean_subject(headers.get("Subject", "")),
                ))
            net.note_success("mails")
            return MailSummary(True, unread=len(ids), previews=tuple(previews))
    except (socket.error, OSError) as exc:
        # Transport failures open the circuit; an IMAP login error would not.
        net.note_failure("mails", exc)
        return MailSummary(False, error=str(exc))
    except imaplib.IMAP4.error as exc:
        return MailSummary(False, error=str(exc))


def spoken_summary(summary: MailSummary) -> str:
    # Turn the summary into a natural sentence for the voice.
    if not summary.available:
        return ""
    if summary.unread == 0:
        return "Bonne nouvelle, tu n'as aucun mail non lu."
    if summary.unread == 1:
        head = "Tu as un mail non lu"
    else:
        head = f"Tu as {summary.unread} mails non lus"
    if not summary.previews:
        return head + "."
    details = [f"{sender}, au sujet de : {subject}"
               for sender, subject in summary.previews[:3]]
    sentence = head + ". " + " Ensuite, ".join(
        [f"Le plus récent vient de {details[0]}."] + details[1:]
    )
    rest = summary.unread - len(summary.previews[:3])
    if rest > 0:
        sentence += f" Et encore {rest} autre{'s' if rest > 1 else ''}."
    return sentence
