import hashlib
import hmac
import json
import random
import time
from locust import between, task  # type: ignore[import-not-found]
from locust.contrib.fasthttp import FastHttpUser  # type: ignore[import-not-found]


class ReviewCommentUser(FastHttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self):
        self.repo = "test-org/repo-1"
        self.pr_number = random.randint(1, 100)
        self.secret = "test-webhook-secret"
        self.head_sha = self._generate_sha()

    def _generate_sha(self):
        return hashlib.sha1(str(time.time()).encode()).hexdigest()

    def _create_payload(self):
        return {
            "action": "created",
            "comment": {
                "id": random.randint(100000, 999999),
                "body": "please review this code",
                "user": {"login": "test-user"},
            },
            "pull_request": {
                "number": self.pr_number,
                "head": {"sha": self.head_sha, "ref": f"feature-{self.pr_number}"},
                "state": "open",
            },
            "repository": {"full_name": self.repo, "language": "python"},
            "sender": {"login": "test-user"},
        }

    def _sign_payload(self, payload_bytes):
        signature = hmac.new(
            self.secret.encode(), payload_bytes, hashlib.sha256
        ).hexdigest()
        return f"sha256={signature}"

    @task(10)
    def send_review_comment(self):
        payload = self._create_payload()
        payload_bytes = json.dumps(payload).encode()

        self.client.post(
            "/github/webhook",
            data=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request_review_comment",
                "X-Hub-Signature-256": self._sign_payload(payload_bytes),
            },
        )


class MultiPRUser(FastHttpUser):
    wait_time = between(0.5, 1.5)

    def on_start(self):
        self.repo = "test-org/repo-multi"
        self.secret = "test-webhook-secret"
        self.pr_numbers = list(range(1, 51))

    def _generate_sha(self):
        return hashlib.sha1(str(time.time()).encode()).hexdigest()

    def _create_payload(self, pr_number):
        return {
            "action": "synchronize",
            "pull_request": {
                "number": pr_number,
                "head": {"sha": self._generate_sha(), "ref": f"feature-{pr_number}"},
                "state": "open",
            },
            "repository": {"full_name": self.repo, "language": "python"},
            "sender": {"login": "test-user"},
        }

    def _sign_payload(self, payload_bytes):
        signature = hmac.new(
            self.secret.encode(), payload_bytes, hashlib.sha256
        ).hexdigest()
        return f"sha256={signature}"

    @task(5)
    def send_push_to_multiple_prs(self):
        pr_number = random.choice(self.pr_numbers)
        payload = self._create_payload(pr_number)
        payload_bytes = json.dumps(payload).encode()

        self.client.post(
            "/github/webhook",
            data=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": self._sign_payload(payload_bytes),
            },
        )


class BurstUser(FastHttpUser):
    wait_time = between(0.01, 0.1)

    def on_start(self):
        self.secret = "test-webhook-secret"
        self.repos = [f"test-org/repo-{i}" for i in range(1, 21)]

    def _generate_sha(self):
        return hashlib.sha1(str(time.time()).encode()).hexdigest()

    def _create_payload(self):
        repo = random.choice(self.repos)
        pr_number = random.randint(1, 200)

        return {
            "action": "submitted",
            "review": {
                "id": random.randint(100000, 999999),
                "state": "changes_requested",
                "user": {"login": "reviewer"},
            },
            "pull_request": {
                "number": pr_number,
                "head": {"sha": self._generate_sha(), "ref": f"feature-{pr_number}"},
                "state": "open",
            },
            "repository": {"full_name": repo, "language": "python"},
            "sender": {"login": "reviewer"},
        }

    def _sign_payload(self, payload_bytes):
        signature = hmac.new(
            self.secret.encode(), payload_bytes, hashlib.sha256
        ).hexdigest()
        return f"sha256={signature}"

    @task(20)
    def send_burst_review(self):
        payload = self._create_payload()
        payload_bytes = json.dumps(payload).encode()

        self.client.post(
            "/github/webhook",
            data=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request_review",
                "X-Hub-Signature-256": self._sign_payload(payload_bytes),
            },
        )
