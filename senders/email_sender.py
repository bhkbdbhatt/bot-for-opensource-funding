"""SMTP channel sender (stdlib `smtplib` + `email.message.EmailMessage`).

Supports plaintext + HTML alternative bodies. Secrets come from the
environment (`email.password_env`), never from config.
"""

from __future__ import annotations

import html
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from typing import Any, Dict, List, Optional, Tuple

from config_loader import Config, resolve_secret
from logging_setup import get_logger
from models import Sponsor, is_valid_email

LOG = get_logger("email-sender")

_SUBJECT_RE = re.compile(r"^\s*subject\s*:\s*(?P<subject>.+?)\s*$", re.IGNORECASE)


class EmailError(Exception):
    """Raised for unrecoverable SMTP problems."""


class EmailSender:
    """Send outreach email over SMTP."""

    channel = "email"

    def __init__(self, config: Config, *, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = bool(dry_run)
        self.host = config.str("email.smtp_host")
        self.port = config.int("email.smtp_port", 587, minimum=1, maximum=65535)
        self.use_tls = config.bool("email.use_tls", True)
        self.use_ssl = config.bool("email.use_ssl", False)
        self.username = config.str("email.username")
        self.sender = config.str("email.sender")
        self.password_env = config.str("email.password_env")
        self.timeout = config.int("email.timeout_seconds", 30, minimum=5, maximum=300)
        self.subject_template = config.str("email.subject_template", "{feature} for Genesys Cloud teams")
        self.html_template = config.str("email.html_template")

    # -- configuration ----------------------------------------------------- #

    @property
    def password(self) -> str:
        return resolve_secret(self.password_env)

    @property
    def from_address(self) -> str:
        _, address = parseaddr(self.sender)
        return address

    def describe(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "tls": self.use_tls,
            "ssl": self.use_ssl,
            "from": self.sender,
            "password_env": self.password_env,
            "password_set": bool(self.password),
            "dry_run": self.dry_run,
        }

    def check_ready(self) -> Tuple[bool, str]:
        if not self.host:
            return False, "email.smtp_host is not configured"
        if not self.sender:
            return False, "email.sender is not configured"
        if not is_valid_email(self.from_address):
            return False, f"email.sender must contain a valid address (got {self.sender!r})"
        if not self.username:
            return False, "email.username is not configured"
        if not self.password:
            return False, f"environment variable {self.password_env or 'SMTP_PASSWORD'} is not set"
        return True, "ok"

    # -- gate -------------------------------------------------------------- #

    def can_send(self, sponsor: Sponsor) -> Tuple[bool, str]:
        if not is_valid_email(sponsor.email):
            return False, f"no valid email on file for {sponsor.name!r}"
        if self.dry_run:
            return True, "dry-run"
        ready, reason = self.check_ready()
        return (False, reason) if not ready else (True, "ok")

    # -- composition ------------------------------------------------------- #

    def split_subject(self, message: str) -> Tuple[str, str]:
        """Pull a leading ``Subject:`` line out of the generated message."""
        text = (message or "").strip()
        if "\n" in text:
            first, _, rest = text.partition("\n")
            match = _SUBJECT_RE.match(first)
            if match:
                return match.group("subject").strip(), rest.strip()
        return self._default_subject(), text

    def _default_subject(self) -> str:
        features = self.config.list("plugin.features")
        headline = ""
        if features:
            headline = re.split(r" - | with ", features[0])[0].strip()[:60]
        try:
            return self.subject_template.format(
                plugin_name=self.config.str("plugin.name"),
                feature=headline or self.config.str("plugin.name"),
            )[:120]
        except (KeyError, IndexError):
            return "Open-source Genesys Cloud plugin"

    def render_html(self, body: str, sponsor: Sponsor, subject: str) -> str:
        """HTML alternative part."""
        if self.html_template:
            return (
                self.html_template.replace("{{BODY}}", html.escape(body))
                .replace("{{PREHEADER}}", html.escape(subject))
                .replace("{{FOOTER}}", html.escape(sponsor.website or ""))
            )
        paragraphs: List[str] = []
        for block in re.split(r"\n\s*\n", body):
            block = block.strip()
            if not block:
                continue
            if block.startswith("- "):
                items = "".join(
                    f"<li>{html.escape(line[2:].strip())}</li>"
                    for line in block.splitlines()
                    if line.strip().startswith("- ")
                )
                others = [
                    html.escape(line.strip())
                    for line in block.splitlines()
                    if line.strip() and not line.strip().startswith("- ")
                ]
                paragraphs.append(f"<ul>{items}</ul>")
                if others:
                    paragraphs.append("<p>" + "<br>".join(others) + "</p>")
            else:
                escaped = html.escape(block).replace("\n", "<br>")
                paragraphs.append(f"<p>{escaped}</p>")
        return (
            "<!doctype html><html><body>"
            f"<p style=\"display:none\">{html.escape(subject)}</p>"
            + "".join(paragraphs)
            + "</body></html>"
        )

    def build_message(self, sponsor: Sponsor, subject: str, body: str) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = sponsor.email
        reply_to = self.config.str("plugin.maintainer_email")
        message["Reply-To"] = reply_to if is_valid_email(reply_to) else self.from_address
        message["Subject"] = subject
        message["Message-ID"] = make_msgid(domain=self.from_address.split("@")[-1] or None)
        message["X-Mailer"] = "GenesysPluginSponsorBot/1.0"
        message["X-Priority"] = "3"  # bulk mail; never mark urgent
        message.set_content(body)
        message.add_alternative(self.render_html(body, sponsor, subject), subtype="html")
        return message

    # -- delivery ---------------------------------------------------------- #

    def send(self, sponsor: Sponsor, message: str) -> bool:
        """Deliver one message. Returns True when handed to the SMTP server."""
        allowed, reason = self.can_send(sponsor)
        if not allowed:
            LOG.error("Cannot send to %s: %s", sponsor.name, reason)
            return False

        subject, body = self.split_subject(message)
        if not body:
            LOG.error("Refusing to send an empty body to %s", sponsor.name)
            return False

        if self.dry_run:
            LOG.info(
                "[DRY RUN] email -> %s <%s> | %s | %d chars",
                sponsor.name,
                sponsor.email,
                subject,
                len(body),
            )
            LOG.debug("Dry-run body:\n%s", body)
            return True

        email_message = self.build_message(sponsor, subject, body)
        try:
            self._transmit(email_message)
        except EmailError as exc:
            LOG.error("SMTP failure for %s <%s>: %s", sponsor.name, sponsor.email, exc)
            return False

        LOG.info(
            "Sent email -> %s <%s> | subject=%r", sponsor.name, sponsor.email, subject
        )
        return True

    def _transmit(self, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        server: Optional[smtplib.SMTP] = None
        # The connect() is inside the try so DNS failures / refused ports are
        # reported as EmailError; send() only catches EmailError.
        try:
            if self.use_ssl:
                server = smtplib.SMTP_SSL(
                    self.host, self.port, timeout=self.timeout, context=context
                )
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            server.ehlo()
            if self.use_tls and not self.use_ssl:
                LOG.debug("STARTTLS -> %s", self.host)
                server.starttls(context=context)
                server.ehlo()
            if self.username:
                server.login(self.username, self.password)
            server.send_message(message)
        except smtplib.SMTPException as exc:
            raise EmailError(f"SMTP error: {exc}") from exc
        except ssl.SSLError as exc:
            raise EmailError(f"TLS error: {exc}") from exc
        except OSError as exc:
            raise EmailError(f"cannot reach {self.host}:{self.port} - {exc}") from exc
        finally:
            # Never `return` from a finally block - it swallows the raise above.
            if server is not None:
                try:
                    server.quit()
                except Exception:  # noqa: BLE001 - best effort teardown
                    try:
                        server.close()
                    except Exception:  # noqa: BLE001
                        pass