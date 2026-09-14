"""Reviewed mail across actual broker sockets, SQLite, TLS and Linux namespaces.

Most tests substitute the SMTP result while exercising real broker/ledger state.
The TLS case uses the real transport with only its trusted CA context substituted.
The namespace case runs a Python probe in the actual gateway mount plan; its
OpenClaw package is synthetic and never executed. No model or external mail runs.
"""
import concurrent.futures
from contextlib import ExitStack
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from yuanxingmu.authority import AuthorizationError
from yuanxingmu import broker as broker_module
from yuanxingmu.broker import Broker, Destination, Resource
from yuanxingmu import mail_transport
from test_yuanxingmu_mail_transport import _TLSFixture


def _draft(**changes):
    return {"recipient": "approved-recipient@example.test", "subject": "APPROVED-SUBJECT",
            "body": "APPROVED-PRIVATE-BODY\nsecond line\tno final newline"} | changes


def _account(**changes):
    return {"host": "smtp.example.test", "port": 465, "username": "HOST-ONLY-USERNAME",
            "password": "HOST-ONLY-PASSWORD", "from_address": "host-sender@example.test"} | changes


def _exchange(path, value):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        connection.sendall(json.dumps(value).encode() + b"\n")
        with connection.makefile("rb") as stream:
            raw = stream.readline(1024 * 1024)
    return json.loads(raw)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux broker and Unix socket boundaries")
class MailBrokerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="yxm-mail-broker-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / "private.txt"
        source.write_text("RESOURCE-PRIVATE-BYTES", encoding="utf-8")
        self.resources = {"private": Resource(source, ("private",))}
        self.destinations = {"public": Destination("https://fixed.example.test/public"),
                             "internal": Destination("https://fixed.example.test/internal", ("private",))}
        self.broker = Broker(self.root / "state", self.resources, self.destinations, reviewed_mail=True)
        self.addCleanup(lambda: self.broker.close())
        self.task = self.broker.create_task(initial_labels=["private"])
        self.worker = self.broker.serve(self.task, self.root / "worker.sock")
        self.reviewer = self.broker.serve_reviews(self.task, self.root / "review.sock")
        transport = mock.patch.object(broker_module, "send_email", side_effect=AssertionError("Unexpected real mail transport"))
        self.transport = transport.start()
        self.addCleanup(transport.stop)
        ordinary = mock.patch.object(broker_module, "_send", side_effect=AssertionError("Unexpected HTTP delivery"))
        self.ordinary = ordinary.start()
        self.addCleanup(ordinary.stop)

    def submit(self, *, key="request-one", value=None, endpoint=None):
        result = _exchange(endpoint or self.worker, {"op": "draft_email", "request_key": key, "draft": value or _draft()})
        self.assertTrue(result["allowed"], result)
        return result

    def confirmation(self, saved, **changes):
        return {"op": "send", "draft_id": saved["draft_id"], "revision": saved["revision"],
                "digest": saved["digest"], "account_id": "trusted-account", "account": _account(),
                "confirm": "send"} | changes

    def current(self, saved):
        return self.broker.review_mail(self.task, {"op": "get", "draft_id": saved["draft_id"]})["draft"]

    def reopen(self):
        self.broker.close()
        self.broker = Broker(self.root / "state", self.resources, self.destinations, reviewed_mail=True)

    def test_worker_endpoint_cannot_review_confirm_or_forge_task(self):
        saved = self.submit()
        other = self.broker.create_task(initial_labels=["private"])
        for value in (self.confirmation(saved), {"op": "approve", "draft_id": saved["draft_id"]},
                      {"op": "review_mail", "draft_id": saved["draft_id"]},
                      {"op": "draft_email", "request_key": "forged", "draft": _draft(), "task_id": other},
                      {"op": "draft_email", "request_key": "forged", "draft": _draft(), "confirm": "send"}):
            with self.subTest(operation=value["op"]):
                self.assertEqual(_exchange(self.worker, value), {"allowed": False, "reason": "invalid_request"})
        self.assertEqual(self.current(saved)["status"], "pending")
        self.assertEqual(self.broker.review_mail(other, {"op": "list"})["drafts"], [])
        self.transport.assert_not_called()

    def test_submit_is_idempotent_and_changed_content_cannot_reuse_key(self):
        saved = self.submit()
        repeated = self.submit(value=_draft(recipient="approved-recipient@EXAMPLE.TEST"))
        self.assertEqual(repeated, saved)
        conflict = _exchange(self.worker, {"op": "draft_email", "request_key": "request-one", "draft": _draft(body="changed")})
        self.assertEqual(conflict, {"allowed": False, "reason": "mail_request_conflict"})
        listing = _exchange(self.reviewer, {"op": "list"})
        self.assertEqual(len(listing["drafts"]), 1)
        self.assertNotIn("body", listing["drafts"][0])
        self.assertEqual(self.current(saved)["body"], _draft()["body"])

    def test_new_review_connections_and_extra_http_ids_cannot_create_another_attempt(self):
        saved = self.submit()
        review_two = self.broker.serve_reviews(self.task, self.root / "review-two.sock")
        confirmation = self.confirmation(saved)
        for extra in ("request_key", "request_id", "idempotency_key"):
            invalid = _exchange(self.reviewer, confirmation | {extra: "new-http-request"})
            self.assertEqual(invalid, {"ok": False, "reason": "invalid_mail_review"})
        with mock.patch.object(broker_module, "send_email", return_value={"outcome": "acknowledged"}) as sending:
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                replies = list(pool.map(lambda path: _exchange(path, confirmation), (self.reviewer, review_two)))
            again = _exchange(review_two, confirmation)
        sending.assert_called_once()
        self.assertTrue(all(reply["ok"] for reply in replies + [again]))
        self.assertEqual({reply["draft"]["status"] for reply in replies + [again]}, {"acknowledged"})
        self.assertEqual(len({reply["draft"]["attempt_id"] for reply in replies + [again]}), 1)
        self.assertEqual(sending.call_args.args[2], again["draft"]["attempt_id"])

    def test_review_binds_task_revision_digest_and_explicit_confirmation(self):
        saved = self.submit()
        child = self.broker.delegate(self.task)
        other = self.broker.create_task(initial_labels=["private"])
        for task in (child, other):
            with self.subTest(task=task), self.assertRaisesRegex(AuthorizationError, "mail_draft_not_found"):
                self.broker.review_mail(task, self.confirmation(saved))
        for changes, reason in (({"digest": "0" * 64}, "mail_draft_changed"),
                                ({"revision": 2}, "mail_draft_changed"),
                                ({"revision": True}, "invalid_mail_revision"),
                                ({"confirm": "yes"}, "mail_confirmation_required")):
            with self.subTest(changes=changes):
                self.assertEqual(_exchange(self.reviewer, self.confirmation(saved, **changes)), {"ok": False, "reason": reason})
        edited = _exchange(self.reviewer, {"op": "edit", "draft_id": saved["draft_id"],
            "revision": saved["revision"], "digest": saved["digest"], "draft": _draft(body="HUMAN-EDITED-BODY")})
        self.assertTrue(edited["ok"])
        self.assertEqual(edited["draft"]["revision"], 2)
        self.assertNotEqual(edited["draft"]["digest"], saved["digest"])
        self.assertEqual(_exchange(self.reviewer, self.confirmation(saved))["reason"], "mail_draft_changed")
        self.transport.assert_not_called()

    def test_cancelled_draft_never_reaches_transport(self):
        saved = self.submit()
        cancelled = _exchange(self.reviewer, {name: value for name, value in self.confirmation(saved, op="cancel").items()
                                             if name in {"op", "draft_id", "revision", "digest"}})
        self.assertEqual(cancelled["draft"]["status"], "cancelled")
        self.assertEqual(_exchange(self.reviewer, self.confirmation(saved)), {"ok": False, "reason": "mail_draft_not_pending"})
        self.transport.assert_not_called()

    def test_revocation_before_confirmation_blocks_send_and_new_drafts(self):
        saved = self.submit()
        self.broker.revoke(self.task)
        denied = _exchange(self.reviewer, self.confirmation(saved))
        self.assertEqual(denied, {"ok": False, "reason": "task_revoked"})
        proposed = _exchange(self.worker, {"op": "draft_email", "request_key": "later", "draft": _draft()})
        self.assertEqual(proposed["reason"], "task_revoked")
        self.assertEqual(self.current(saved)["status"], "pending")
        self.transport.assert_not_called()

    def test_send_lock_orders_revoke_after_the_single_inflight_attempt(self):
        saved = self.submit()
        second = self.submit(key="later")
        entered, release, revoking = threading.Event(), threading.Event(), threading.Event()
        def deliver(*args):
            entered.set()
            if not release.wait(4):
                raise AssertionError("test sender was not released")
            return {"outcome": "acknowledged"}
        def revoke():
            revoking.set()
            return self.broker.revoke(self.task)
        with mock.patch.object(broker_module, "send_email", side_effect=deliver) as sending, \
                concurrent.futures.ThreadPoolExecutor(2) as pool:
            try:
                first = pool.submit(self.broker.review_mail, self.task, self.confirmation(saved))
                self.assertTrue(entered.wait(2))
                pending_revoke = pool.submit(revoke)
                self.assertTrue(revoking.wait(2))
                with self.assertRaises(concurrent.futures.TimeoutError):
                    pending_revoke.result(timeout=.1)
            finally:
                release.set()
            self.assertEqual(first.result(3)["draft"]["status"], "acknowledged")
            pending_revoke.result(3)
            with self.assertRaisesRegex(AuthorizationError, "task_revoked"):
                self.broker.review_mail(self.task, self.confirmation(second))
            sending.assert_called_once()

    def test_reviewed_send_does_not_clear_private_labels_or_expand_ordinary_grants(self):
        before = self.broker.authority.describe(self.task)
        self.assertEqual(before["labels"], ["private"])
        saved = self.submit()
        self.assertFalse(self.broker.dispatch(self.task, {"op": "send", "destination": "public", "body": "attempt"})["allowed"])
        with mock.patch.object(broker_module, "send_email", return_value={"outcome": "acknowledged"}) as sending:
            self.assertEqual(_exchange(self.reviewer, self.confirmation(saved))["draft"]["status"], "acknowledged")
            sending.assert_called_once()
        after = self.broker.authority.describe(self.task)
        self.assertEqual(after["labels"], before["labels"])
        self.assertEqual(after["destinations"], before["destinations"])
        self.assertFalse(self.broker.dispatch(self.task, {"op": "send", "destination": "public", "body": "attempt"})["allowed"])
        self.ordinary.assert_not_called()
        with mock.patch.object(broker_module, "_send", return_value={"outcome": "acknowledged"}) as ordinary:
            result = self.broker.dispatch(self.task, {"op": "send", "destination": "internal", "body": "approved old grant"})
        self.assertTrue(result["allowed"])
        ordinary.assert_called_once()
        another = self.submit(key="still-needs-review")
        self.assertEqual(self.current(another)["status"], "pending")
        self.transport.assert_not_called()

    def test_transport_exception_consumes_attempt_without_leaking_reason_or_retrying(self):
        saved = self.submit()
        with mock.patch.object(broker_module, "send_email", side_effect=OSError("SECRET-SMTP-REPLY HOST-ONLY-PASSWORD")) as sending:
            self.assertEqual(_exchange(self.reviewer, self.confirmation(saved)), {"ok": False, "reason": "mail_review_failed"})
            repeated = _exchange(self.reviewer, self.confirmation(saved))
        self.assertEqual(repeated["draft"]["status"], "unconfirmed")
        sending.assert_called_once()
        self.reopen()
        with mock.patch.object(broker_module, "send_email") as sending:
            result = self.broker.review_mail(self.task, self.confirmation(saved))
        self.assertEqual(result["draft"]["status"], "unconfirmed")
        sending.assert_not_called()

    def test_result_save_failure_recovers_sending_as_unconfirmed_and_never_resends(self):
        saved = self.submit()
        with mock.patch.object(broker_module, "send_email", return_value={"outcome": "acknowledged"}) as sending, \
                mock.patch.object(self.broker.mail, "finish_send", side_effect=OSError("synthetic result persistence failure")):
            with self.assertRaises(OSError):
                self.broker.review_mail(self.task, self.confirmation(saved))
        sending.assert_called_once()
        interrupted = self.current(saved)
        self.assertEqual(interrupted["status"], "sending")
        self.assertIsNotNone(interrupted["attempt_id"])
        self.reopen()
        recovered = self.current(saved)
        self.assertEqual(recovered["status"], "unconfirmed")
        self.assertEqual(recovered["attempt_id"], interrupted["attempt_id"])
        with mock.patch.object(broker_module, "send_email") as sending:
            repeated = self.broker.review_mail(self.task, self.confirmation(saved))
        self.assertEqual(repeated["draft"]["status"], "unconfirmed")
        sending.assert_not_called()

    def test_audit_contains_only_metadata_and_never_mail_or_account_content(self):
        saved = self.submit()
        with mock.patch.object(broker_module, "send_email", return_value={"outcome": "acknowledged"}):
            _exchange(self.reviewer, self.confirmation(saved))
        events = self.broker.authority.events(self.task)
        encoded = json.dumps(events) + (self.root / "state/broker-events.jsonl").read_text()
        for secret in (*_draft().values(), *_account().values(), "HOST-ONLY-PASSWORD"):
            if isinstance(secret, str):
                self.assertNotIn(secret, encoded)
                self.assertNotIn(json.dumps(secret)[1:-1], encoded)
        mail_events = [event for event in events if event["action"].startswith("mail_")]
        self.assertEqual([event["action"] for event in mail_events], ["mail_draft_submitted", "mail_send_started", "mail_send_finished"])
        for event in mail_events:
            self.assertEqual(set(event["details"]), {"id", "digest", "revision", "status"})

    def test_invalid_worker_operation_cannot_smuggle_private_content_into_audit(self):
        secret = "MALICIOUS-OP-BODY approved-recipient@example.test HOST-ONLY-PASSWORD"
        for operation in (secret, {"body": secret}, [secret]):
            with self.subTest(operation_type=type(operation).__name__):
                result = _exchange(self.worker, {"op": operation})
                self.assertEqual(result, {"allowed": False, "reason": "invalid_request"})
        encoded = (self.root / "state/broker-events.jsonl").read_text()
        self.assertNotIn(secret, encoded)
        self.assertEqual([json.loads(line)["operation"] for line in encoded.splitlines()], ["invalid"] * 3)
        self.transport.assert_not_called()

    @unittest.skipUnless(shutil.which("openssl"), "openssl generates only an ephemeral test certificate")
    def test_broker_to_real_tls_smtp_preserves_the_exact_approved_body_and_envelope(self):
        certificate, key, config = (self.root / name for name in ("cert.pem", "key.pem", "openssl.conf"))
        config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
                          "[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\n"
                          "basicConstraints=critical,CA:TRUE\n", encoding="ascii")
        subprocess.run([shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(key), "-out", str(certificate), "-config", str(config)],
                       check=True, capture_output=True, timeout=20)
        real_create_context = ssl.create_default_context
        def fixture_trust(*args, **kwargs):
            # Only the CA source is replaced. The TLS socket, hostname checking,
            # SMTP auth, envelope, DATA and server acknowledgement are real.
            return real_create_context(cafile=str(certificate))
        proposed = _draft(body="准确批准的 UTF-8 正文\nBcc: body text only\tend")
        saved = self.submit(value=proposed)
        with _TLSFixture(certificate, key) as fixture, \
                mock.patch.object(broker_module, "send_email", mail_transport.send_email), \
                mock.patch.object(mail_transport.ssl, "create_default_context", side_effect=fixture_trust):
            result = _exchange(self.reviewer, self.confirmation(saved, account=_account(host="localhost", port=fixture.port)))
            self.assertTrue(fixture.done.wait(3))
        self.assertIsNone(fixture.error)
        self.assertEqual(result["draft"]["status"], "acknowledged")
        self.assertEqual(len(fixture.messages), 1)
        message = BytesParser(policy=policy.default).parsebytes(fixture.messages[0])
        self.assertEqual(message.get_payload(decode=True), proposed["body"].encode("utf-8"))
        self.assertEqual(message["To"], proposed["recipient"])
        self.assertEqual(message["From"], _account()["from_address"])
        self.assertEqual(message.get_content_type(), "text/plain")
        self.assertFalse(message.is_multipart())
        self.assertEqual([line.lower() for line in fixture.commands if line.upper().startswith(b"RCPT ")],
                         [b"rcpt to:<approved-recipient@example.test>\r\n"])
        self.assertIsNone(message["Bcc"])

    def test_real_gateway_namespace_can_submit_but_cannot_reach_host_review_socket(self):
        from yuanxingmu import openclaw
        from yuanxingmu.run import load_policy
        binary = os.environ.get("YUANXINGMU_TEST_BWRAP") or shutil.which("bwrap", path="/usr/bin:/bin")
        node = os.environ.get("YUANXINGMU_TEST_NODE") or shutil.which("node", path="/usr/bin:/bin")
        if not binary or not node or not openclaw.sandbox_available(bwrap=Path(binary))["available"]:
            self.skipTest("real bubblewrap isolation and a host node path are required")
        package = self.root / "synthetic-runtime/node_modules/openclaw"
        package.mkdir(parents=True)
        (package / "package.json").write_text('{"version":"2026.9.4"}')
        (package / "openclaw.mjs").write_text("throw new Error('synthetic package must not run');\n")
        profile = self.root / "profile"
        # The isolated probe runs the real system interpreter. Hosted CI's test
        # runner may live under /opt, which is deliberately not a supported
        # Gateway Python location. Keep that production check intact.
        system_python = SimpleNamespace(platform=sys.platform, executable=str(Path("/usr/bin/python3").resolve()))
        with mock.patch.object(openclaw, "sys", system_python):
            openclaw.init_profile(profile, node=Path(node).resolve(), bwrap=Path(binary).resolve(),
                                 openclaw_package=package, model_url="https://model.invalid/v1", model_id="synthetic",
                                 api_key="SYNTHETIC-HOST-MODEL-SECRET", reviewed_mail=True)
        manifest = openclaw.validate_profile(profile)
        runtime = Path(manifest["runtime"])
        self.assertEqual(runtime.parent, Path("/tmp"))
        self.assertTrue(runtime.name.startswith("yxm-" + str(os.getuid()) + "-"))
        runtime.mkdir(mode=0o700)
        with ExitStack() as stack:
            stack.callback(shutil.rmtree, runtime)
            (runtime / "webui").mkdir(mode=0o700)
            for name in ("model.sock", "operator.sock"):
                endpoint = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
                endpoint.bind(str(runtime / name))
                endpoint.listen(1)
            resources, destinations = load_policy(profile / "policy.json")
            from yuanxingmu.protection import profile_services
            broker = stack.enter_context(Broker(profile / "broker-state", resources, destinations, **profile_services(profile, manifest)))
            worker = broker.serve(manifest["task_id"], runtime / "broker.sock")
            review = broker.serve_reviews(manifest["task_id"], runtime / "review.sock")
            self.assertEqual(stat.S_IMODE(review.stat().st_mode), 0o600)
            self.assertTrue(_exchange(review, {"op": "list"})["ok"])
            command = openclaw.gateway_command(profile, manifest)
            setup = command[:command.index("--")]
            mounts = [Path(setup[index + 1]) for index, word in enumerate(setup) if word in {"--bind", "--ro-bind"}]
            self.assertFalse(any(path == review or path in review.parents for path in mounts))
            probe = r'''
import json,os,socket,sys
from pathlib import Path
def request(path,value):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(path)
        connection.sendall(json.dumps(value).encode()+b"\n")
        with connection.makefile("rb") as stream:
            return json.loads(stream.readline())
value={"recipient":"approved-recipient@example.test","subject":"namespace draft","body":"namespace proposed body"}
submitted=request(sys.argv[1],{"op":"draft_email","request_key":"namespace-one","draft":value})
forged=request(sys.argv[1],{"op":"approve","draft_id":submitted.get("draft_id")})
try:
    request(sys.argv[2],{"op":"list"})
    review_reachable=True
except OSError:
    review_reachable=False
print(json.dumps({"draft":submitted,"forged":forged,"review_visible":Path(sys.argv[2]).exists(),
    "review_reachable":review_reachable,"netns":os.readlink("/proc/self/ns/net"),
    "mntns":os.readlink("/proc/self/ns/mnt"),"host_key_visible":Path(sys.argv[3],"model-key").exists()}))
'''
            result = subprocess.run([*setup, "--", "/usr/bin/python3", "-I", "-B", "-c", probe, str(worker), str(review), str(profile)],
                                    env=openclaw._environment(profile, manifest), capture_output=True, text=True,
                                    timeout=15, start_new_session=True, close_fds=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(result.stdout)
            self.assertTrue(observed["draft"]["allowed"])
            self.assertEqual(observed["draft"]["status"], "pending")
            self.assertEqual(observed["forged"], {"allowed": False, "reason": "invalid_request"})
            self.assertFalse(observed["review_visible"])
            self.assertFalse(observed["review_reachable"])
            self.assertFalse(observed["host_key_visible"])
            self.assertNotEqual(observed["netns"], os.readlink("/proc/self/ns/net"))
            self.assertNotEqual(observed["mntns"], os.readlink("/proc/self/ns/mnt"))
            inspected = _exchange(review, {"op": "get", "draft_id": observed["draft"]["draft_id"]})
            self.assertEqual(inspected["draft"]["body"], "namespace proposed body")
        self.transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
