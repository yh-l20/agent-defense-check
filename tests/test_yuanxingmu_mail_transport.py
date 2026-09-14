"""Mail validation units and a real, non-relaying loopback TLS SMTP fixture.

No user account, public SMTP endpoint or real delivery is used. TLS tests require
an available openssl executable only to generate a temporary test certificate.
"""
from contextlib import redirect_stderr, redirect_stdout
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import smtplib
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from yuanxingmu import mail_transport as mail


def draft(**changes):
    value = {"recipient": "Only.User+tag@Example.Test", "subject": "确认发送的主题", "body": "Approved body\nSecond line\tend"}
    return value | changes


def account(**changes):
    value = {"host": "smtp.example.test", "port": 465, "username": "FIXTURE-USER",
             "password": "FIXTURE-PASSWORD", "from_address": "Sender@Example.Test"}
    return mail.MailAccount(**(value | changes))


class MailValidationTests(unittest.TestCase):
    def test_canonical_fields_domain_and_line_endings(self):
        canonical = mail.validate_draft(draft(body="line 1\r\nline 2\tend"))
        self.assertEqual(canonical, {"recipient": "Only.User+tag@example.test", "subject": "确认发送的主题", "body": "line 1\nline 2\tend"})

    def test_only_exact_draft_fields_are_accepted(self):
        for extra in ("from", "from_address", "cc", "bcc", "attachments", "html", "host", "password", "headers"):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                mail.validate_draft(draft(**{extra: "caller-selected"}))
        for value in (None, [], "message", {"recipient": "x@example.test", "body": "text"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mail.validate_draft(value)

    def test_one_explicit_ascii_recipient_without_header_injection(self):
        invalid = ["a@example.test,b@example.test", "a@example.test; b@example.test",
                   "Name <a@example.test>", "<a@example.test>", "group:a@example.test;",
                   "a@example.test\r\nBcc: other@example.test", "a@example.test\n", " a@example.test",
                   "a@example.test ", "a(comment)@example.test", '"a"@example.test',
                   "名字@example.test", "a@例子.test", "a@[127.0.0.1]", "a@localhost",
                   "a..b@example.test", ".a@example.test", "a.@example.test", "a@-example.test",
                   "a@example..test", "a@@example.test", "a@exam_ple.test", "a\x00@example.test",
                   "a" * 65 + "@example.test", ["a@example.test"], None]
        for value in invalid:
            with self.subTest(recipient=value), self.assertRaises(ValueError):
                mail.validate_draft(draft(recipient=value))

    def test_subject_control_characters_and_limits(self):
        self.assertEqual(len(mail.validate_draft(draft(subject="中" * 200))["subject"]), 200)
        for value in ("中" * 201, "title\r\nBcc: x@example.test", "title\n", "title\tend",
                      "title\x7f", "title\x85", "title\u2028", "title\u202e", "title\ud800", None):
            with self.subTest(subject=value), self.assertRaises(ValueError):
                mail.validate_draft(draft(subject=value))

    def test_body_utf8_limit_is_bytes_not_characters(self):
        exactly = "中" * 21845 + "x"
        self.assertEqual(len(exactly.encode("utf-8")), 65536)
        self.assertEqual(mail.validate_draft(draft(body=exactly))["body"], exactly)
        for value in (exactly + "x", "x" * 65537, "body\rtrailing", "body\x00", "body\x1b",
                      "body\x7f", "body\u202e", "body\ud800", None):
            with self.subTest(body_type=type(value).__name__), self.assertRaises(ValueError):
                mail.validate_draft(draft(body=value))

    def test_digest_hashes_canonical_json_and_binds_all_three_fields(self):
        value = draft(body="line 1\r\nline 2")
        canonical = mail.validate_draft(value)
        expected = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(mail.draft_digest(value), expected)
        self.assertEqual(mail.draft_digest(dict(reversed(list(canonical.items())))), expected)
        for field, changed in (("recipient", "another@example.test"), ("subject", "another"), ("body", "another")):
            self.assertNotEqual(mail.draft_digest(canonical | {field: changed}), expected)

    def test_account_keeps_credential_fields_out_of_repr_and_has_no_tls_switch(self):
        configured = account()
        self.assertEqual(configured.from_address, "Sender@example.test")
        for hidden in (configured.username, configured.password, configured.from_address):
            self.assertNotIn(hidden, repr(configured))
        with self.assertRaises(TypeError):
            account(tls=False)
        for changes in ({"host": "smtp://example.test"}, {"host": "example.test:465"},
                        {"host": "example.test\n"}, {"port": True}, {"port": 0},
                        {"port": 65536}, {"username": ""}, {"password": "secret\r\n"},
                        {"from_address": "a@example.test,b@example.test"}):
            with self.subTest(fields=list(changes)), self.assertRaises(ValueError):
                account(**changes)


class MailTransportUnitTests(unittest.TestCase):
    def setUp(self):
        self.client = mock.Mock()
        self.client.sendmail.return_value = {}
        self.ssl_client = mock.patch.object(mail.smtplib, "SMTP_SSL", return_value=self.client).start()
        self.addCleanup(mock.patch.stopall)
        self.plain_client = mock.patch.object(mail.smtplib, "SMTP", side_effect=AssertionError("plaintext SMTP is forbidden")).start()

    def test_fixed_account_and_single_sendmail_use_verified_tls(self):
        configured = account()
        result = mail.send_email(configured, draft(), "request-123")
        self.assertEqual(result, {"outcome": "acknowledged"})
        self.ssl_client.assert_called_once()
        args, kwargs = self.ssl_client.call_args
        self.assertEqual(args, (configured.host, configured.port))
        context = kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertGreater(kwargs["timeout"], 0)
        self.client.login.assert_called_once_with(configured.username, configured.password)
        self.client.set_debuglevel.assert_called_once_with(0)
        self.client.sendmail.assert_called_once()
        sender, recipients, payload = self.client.sendmail.call_args.args
        self.assertEqual(sender, "Sender@example.test")
        self.assertEqual(recipients, ["Only.User+tag@example.test"])
        self.assertIsInstance(payload, bytes)
        self.client.close.assert_called_once()
        self.plain_client.assert_not_called()

    def test_preflight_failure_is_not_started_and_never_connects(self):
        for value, request_id in ((draft(bcc="other@example.test"), "id"), (draft(), "id\r\nBcc: bad"),
                                  (draft(), ""), (draft(), "x" * 129)):
            self.assertEqual(mail.send_email(account(), value, request_id), {"outcome": "not_started"})
        self.assertEqual(mail.send_email({}, draft(), "id"), {"outcome": "not_started"})
        self.ssl_client.assert_not_called()
        self.client.sendmail.assert_not_called()

    def test_insecure_context_is_rejected_before_connecting(self):
        insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        insecure.check_hostname = False
        insecure.verify_mode = ssl.CERT_NONE
        with mock.patch.object(mail.ssl, "create_default_context", return_value=insecure):
            result = mail.send_email(account(), draft(), "id")
        self.assertEqual(result, {"outcome": "not_started"})
        self.ssl_client.assert_not_called()

    def test_failures_after_connection_attempt_are_sanitized_and_not_retried(self):
        private = "PRIVATE-BODY FIXTURE-PASSWORD smtp-details"
        for stage in ("connect", "login", "send"):
            with self.subTest(stage=stage):
                self.ssl_client.reset_mock(side_effect=True)
                self.client.reset_mock(side_effect=True)
                self.client.sendmail.return_value = {}
                failing = {"connect": self.ssl_client, "login": self.client.login, "send": self.client.sendmail}[stage]
                failing.side_effect = smtplib.SMTPServerDisconnected(private)
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(output):
                    result = mail.send_email(account(), draft(body=private), "id")
                self.assertEqual(result, {"outcome": "unconfirmed"})
                self.assertEqual(output.getvalue(), "")
                self.ssl_client.assert_called_once()
                self.assertEqual(self.client.sendmail.call_count, 1 if stage == "send" else 0)
                self.plain_client.assert_not_called()

    def test_recipient_refusal_is_not_acknowledgement(self):
        self.client.sendmail.return_value = {"Only.User+tag@example.test": (550, b"private provider reason")}
        self.assertEqual(mail.send_email(account(), draft(), "id"), {"outcome": "unconfirmed"})

    def test_acknowledgement_survives_close_failure(self):
        self.client.close.side_effect = OSError("private close failure")
        self.assertEqual(mail.send_email(account(), draft(), "id"), {"outcome": "acknowledged"})
        self.client.sendmail.assert_called_once()

    def test_message_id_is_stable_and_not_a_deduplication_claim(self):
        for request_id in ("request-1", "request-1", "request-2"):
            self.assertEqual(mail.send_email(account(), draft(), request_id), {"outcome": "acknowledged"})
        messages = [BytesParser(policy=policy.default).parsebytes(call.args[2]) for call in self.client.sendmail.call_args_list]
        self.assertEqual(messages[0]["Message-ID"], messages[1]["Message-ID"])
        self.assertNotEqual(messages[0]["Message-ID"], messages[2]["Message-ID"])
        self.assertEqual(messages[0]["Message-ID"], "<yxm." + hashlib.sha256(b"request-1").hexdigest() + "@yuanxingmu.invalid>")


class _TLSFixture:
    """One local TLS session; records bytes but has no delivery/relay capability."""

    def __init__(self, certificate, key, *, outcome="acknowledge"):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(certificate, key)
        self.outcome = outcome
        self.commands = []
        self.messages = []
        self.connections = 0
        self.error = None
        self.done = threading.Event()
        self.connection = None
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(1)
        self.listener.settimeout(4)
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def serve(self):
        try:
            raw, _ = self.listener.accept()
            self.connections += 1
            with raw:
                raw.settimeout(4)
                with self.context.wrap_socket(raw, server_side=True) as connection:
                    self.connection = connection
                    connection.sendall(b"220 localhost synthetic fixture\r\n")
                    with connection.makefile("rb") as stream:
                        while line := stream.readline(2048):
                            self.commands.append(line)
                            command = line.split(b" ", 1)[0].strip().upper()
                            if command == b"EHLO":
                                connection.sendall(b"250-localhost\r\n250-AUTH PLAIN\r\n250 SIZE 200000\r\n")
                            elif command == b"AUTH":
                                connection.sendall(b"235 synthetic authentication accepted\r\n")
                            elif command in {b"MAIL", b"RCPT"}:
                                connection.sendall(b"250 synthetic envelope accepted\r\n")
                            elif command == b"DATA":
                                connection.sendall(b"354 send fixture bytes\r\n")
                                data = bytearray()
                                while chunk := stream.readline(2048):
                                    if chunk == b".\r\n":
                                        break
                                    data.extend(chunk[1:] if chunk.startswith(b"..") else chunk)
                                    if len(data) > 200000:
                                        raise ValueError("fixture message limit exceeded")
                                self.messages.append(bytes(data))
                                if self.outcome == "disconnect":
                                    return
                                if self.outcome == "reject":
                                    connection.sendall(b"550 PRIVATE SMTP BODY FIXTURE-PASSWORD\r\n")
                                else:
                                    connection.sendall(b"250 synthetic DATA accepted\r\n")
                            elif command == b"RSET":
                                connection.sendall(b"250 reset\r\n")
                            elif command == b"QUIT":
                                connection.sendall(b"221 closing\r\n")
                                return
                            else:
                                connection.sendall(b"500 unsupported fixture command\r\n")
        except Exception as exc:
            self.error = exc
        finally:
            self.done.set()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exception):
        self.listener.close()
        if self.connection is not None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("local SMTP fixture did not stop")


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed only for a temporary local TLS certificate")
class MailTLSLoopbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="yxm-mail-tls-")
        cls.addClassCleanup(temporary.cleanup)
        cls.directory = Path(temporary.name)
        cls.certificate = cls.directory / "fixture-cert.pem"
        cls.key = cls.directory / "fixture-key.pem"
        configuration = cls.directory / "fixture-openssl.conf"
        configuration.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=extensions\nprompt=no\n"
            "[dn]\nCN=localhost\n[extensions]\nsubjectAltName=DNS:localhost\n"
            "basicConstraints=critical,CA:TRUE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n"
            "extendedKeyUsage=serverAuth\n", encoding="ascii")
        subprocess.run([shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(cls.key), "-out", str(cls.certificate), "-days", "1",
                        "-config", str(configuration)],
                       check=True, capture_output=True, timeout=20)

    def trusted_roots(self):
        # Trust this temporary certificate without disabling chain/hostname
        # validation. Production still uses its unmodified default CA context.
        return mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(self.certificate)})

    def test_real_tls_single_envelope_plaintext_bytes_and_no_extra_recipients(self):
        proposed = draft(body="正文只发给批准的一个人。\nBcc: this is body text\n.leading dot\tno final newline")
        with _TLSFixture(self.certificate, self.key) as fixture, self.trusted_roots():
            result = mail.send_email(account(host="localhost", port=fixture.port), proposed, "real-tls-1")
            self.assertTrue(fixture.done.wait(3))
        self.assertIsNone(fixture.error)
        self.assertEqual(result, {"outcome": "acknowledged"}, [line.split(b" ", 1)[0] for line in fixture.commands])
        self.assertEqual(fixture.connections, 1)
        envelope = [line for line in fixture.commands if line.upper().startswith((b"MAIL ", b"RCPT "))]
        self.assertEqual(len(envelope), 2)
        self.assertTrue(envelope[0].lower().startswith(b"mail from:<sender@example.test>"))
        # SMTP keywords ignore case; the approved mailbox local-part does not.
        command, separator, recipient = envelope[1].partition(b":")
        self.assertEqual((command.lower(), separator), (b"rcpt to", b":"))
        self.assertEqual(recipient, b"<Only.User+tag@example.test>\r\n")
        self.assertEqual(len(fixture.messages), 1)
        message = BytesParser(policy=policy.default).parsebytes(fixture.messages[0])
        self.assertEqual(getaddresses(message.get_all("To")), [("", "Only.User+tag@example.test")])
        self.assertEqual(getaddresses(message.get_all("From")), [("", "Sender@example.test")])
        self.assertEqual(str(message["Subject"]), proposed["subject"])
        self.assertEqual(message.get_content_type(), "text/plain")
        self.assertEqual(message.get_content_charset(), "utf-8")
        self.assertFalse(message.is_multipart())
        self.assertEqual(list(message.iter_attachments()), [])
        self.assertEqual(message.get_payload(decode=True), mail.validate_draft(proposed)["body"].encode("utf-8"))
        for header in ("Cc", "Bcc", "Resent-To", "Content-Disposition"):
            self.assertIsNone(message[header])

    def test_real_disconnect_after_data_is_unconfirmed_without_second_submission(self):
        with _TLSFixture(self.certificate, self.key, outcome="disconnect") as fixture, self.trusted_roots():
            result = mail.send_email(account(host="localhost", port=fixture.port), draft(), "lost-data-reply")
            self.assertTrue(fixture.done.wait(3))
        self.assertEqual(result, {"outcome": "unconfirmed"})
        self.assertEqual(fixture.connections, 1)
        self.assertEqual(len(fixture.messages), 1)
        self.assertEqual(sum(line.upper().startswith(b"MAIL ") for line in fixture.commands), 1)
        self.assertEqual(sum(line.upper().startswith(b"RCPT ") for line in fixture.commands), 1)
        self.assertEqual(sum(line.upper() == b"DATA\r\n" for line in fixture.commands), 1)

    def test_real_server_rejection_does_not_expose_reply_or_credentials(self):
        output = io.StringIO()
        with _TLSFixture(self.certificate, self.key, outcome="reject") as fixture, self.trusted_roots(), \
                redirect_stdout(output), redirect_stderr(output):
            result = mail.send_email(account(host="localhost", port=fixture.port), draft(), "server-reject")
            self.assertTrue(fixture.done.wait(3))
        self.assertEqual(result, {"outcome": "unconfirmed"})
        self.assertEqual(output.getvalue(), "")
        self.assertIsNone(fixture.error)
        self.assertEqual(len(fixture.messages), 1)

    def test_untrusted_certificate_prevents_authentication_and_data(self):
        with _TLSFixture(self.certificate, self.key) as fixture, mock.patch.dict(os.environ, {
                "SSL_CERT_FILE": os.devnull, "SSL_CERT_DIR": str(self.directory / "no-trusted-roots")}):
            result = mail.send_email(account(host="localhost", port=fixture.port), draft(), "untrusted-cert")
            self.assertTrue(fixture.done.wait(3))
        self.assertEqual(result, {"outcome": "unconfirmed"})
        self.assertEqual(fixture.connections, 1)
        self.assertEqual(fixture.commands, [])
        self.assertEqual(fixture.messages, [])

    def test_hostname_mismatch_prevents_authentication_and_data(self):
        with _TLSFixture(self.certificate, self.key) as fixture, self.trusted_roots():
            result = mail.send_email(account(host="127.0.0.1", port=fixture.port), draft(), "wrong-hostname")
            self.assertTrue(fixture.done.wait(3))
        self.assertEqual(result, {"outcome": "unconfirmed"})
        self.assertEqual(fixture.connections, 1)
        self.assertEqual(fixture.commands, [])
        self.assertEqual(fixture.messages, [])


if __name__ == "__main__":
    unittest.main()
