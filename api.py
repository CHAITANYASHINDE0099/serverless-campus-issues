"""Versioned issue tracking for API Gateway HTTP API and AWS Lambda."""

import base64
import binascii
import json
import os
from datetime import UTC, datetime
from functools import lru_cache
from threading import Lock
from uuid import UUID


CATEGORIES = {"academics", "facilities", "it_support", "student_services"}
NEXT_STATUS = {"open": "in_progress", "in_progress": "resolved"}


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


class MemoryIssueStore:
    """In-process adapter for local demos of the same HTTP handler."""

    def __init__(self):
        self.issues = {}
        self.lock = Lock()

    def get(self, issue_id):
        with self.lock:
            issue = self.issues.get(issue_id)
            return issue.copy() if issue else None

    def create(self, issue):
        with self.lock:
            if issue["id"] in self.issues:
                return False
            self.issues[issue["id"]] = issue.copy()
            return True

    def advance(self, issue_id, version, status):
        with self.lock:
            issue = self.issues.get(issue_id)
            if issue is None or issue["version"] != version:
                return None
            issue.update(status=status, version=version + 1, updated_at=now())
            return issue.copy()


class DynamoIssueStore:
    def __init__(self, table):
        self.table = table
        self.condition_failed = table.meta.client.exceptions.ConditionalCheckFailedException

    def get(self, issue_id):
        return self.table.get_item(Key={"id": issue_id}, ConsistentRead=True).get("Item")

    def create(self, issue):
        try:
            self.table.put_item(
                Item=issue,
                ConditionExpression="attribute_not_exists(#id)",
                ExpressionAttributeNames={"#id": "id"},
            )
        except self.condition_failed:
            return False
        return True

    def advance(self, issue_id, version, status):
        try:
            result = self.table.update_item(
                Key={"id": issue_id},
                ConditionExpression="#version = :expected",
                UpdateExpression="SET #status = :status, #version = :next, updated_at = :time",
                ExpressionAttributeNames={"#version": "version", "#status": "status"},
                ExpressionAttributeValues={
                    ":expected": version, ":next": version + 1,
                    ":status": status, ":time": now(),
                },
                ReturnValues="ALL_NEW",
            )
        except self.condition_failed:
            return None
        return result["Attributes"]


def response(status, data):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json; charset=utf-8"},
        "body": json.dumps(data, ensure_ascii=True),
    }


def error(status, message):
    return response(status, {"error": message})


def issue_response(status, issue):
    # DynamoDB deserializes numbers as Decimal.
    return response(status, {**issue, "version": int(issue["version"])})


def json_body(event):
    body = event.get("body")
    if not isinstance(body, str) or len(body) > 12000:
        raise ValueError("A JSON request body of up to 8 KB is required")
    try:
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body, validate=True).decode("utf-8")
        if len(body.encode("utf-8")) > 8192:
            raise ValueError("Request body exceeds 8 KB")
        data = json.loads(body)
    except (binascii.Error, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Request body must be valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    return data


def create_issue(store, issue_id, data):
    title = data.get("title")
    category = data.get("category")
    description = data.get("description", "")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
        return error(400, "title must contain 1 to 120 characters")
    if not isinstance(category, str) or category not in CATEGORIES:
        return error(400, "category must be one of " + ", ".join(sorted(CATEGORIES)))
    if not isinstance(description, str) or len(description) > 1000:
        return error(400, "description must contain at most 1000 characters")

    details = {"title": title.strip(), "category": category, "description": description.strip()}
    timestamp = now()
    issue = {
        "id": issue_id, **details, "status": "open", "version": 1,
        "created_at": timestamp, "updated_at": timestamp,
    }
    if store.create(issue):
        return issue_response(201, issue)
    existing = store.get(issue_id)
    if existing and all(existing[field] == value for field, value in details.items()):
        return issue_response(200, existing)
    return error(409, "This issue ID already has different details")


def advance_issue(store, issue_id, data):
    version, status = data.get("version"), data.get("status")
    if type(version) is not int or version < 1:
        return error(400, "version must be a positive integer")
    if status not in ("in_progress", "resolved"):
        return error(400, "status must be in_progress or resolved")
    existing = store.get(issue_id)
    if existing is None:
        return error(404, "Issue not found")
    if existing["version"] != version:
        return error(409, "Issue version changed. Fetch the latest version and retry.")
    if NEXT_STATUS.get(existing["status"]) != status:
        return error(409, "That status transition is invalid")
    updated = store.advance(issue_id, version, status)
    if updated is None:
        return error(409, "Issue version changed. Fetch the latest version and retry.")
    return issue_response(200, updated)


def handle_request(event, store):
    method = event.get("requestContext", {}).get("http", {}).get("method")
    if method not in ("PUT", "GET", "PATCH"):
        return error(405, "Use PUT, GET, or PATCH")
    issue_id = (event.get("pathParameters") or {}).get("issueId", "")
    try:
        if not isinstance(issue_id, str) or issue_id != str(UUID(issue_id)):
            raise ValueError("Invalid UUID")
    except ValueError:
        return error(400, "issueId must be a canonical UUID")

    if method == "GET":
        issue = store.get(issue_id)
        return issue_response(200, issue) if issue else error(404, "Issue not found")
    try:
        data = json_body(event)
    except ValueError as exc:
        return error(400, str(exc))
    if method == "PUT":
        return create_issue(store, issue_id, data)
    return advance_issue(store, issue_id, data)


@lru_cache(maxsize=1)
def aws_store():
    import boto3

    return DynamoIssueStore(boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"]))


def lambda_handler(event, context):
    return handle_request(event, aws_store())


def demo():
    store = MemoryIssueStore()
    issue_id = "78d73d41-4e5e-4435-9e0d-59e2df82ad10"
    issue = {"title": "Water cooler leaking", "category": "facilities"}
    requests = [
        ("PUT", issue, 201),
        ("PUT", issue, 200),
        ("GET", None, 200),
        ("PATCH", {"status": "in_progress", "version": 1}, 200),
        ("PATCH", {"status": "resolved", "version": 1}, 409),
        ("PATCH", {"status": "resolved", "version": 2}, 200),
    ]
    for method, body, expected in requests:
        event = {
            "requestContext": {"http": {"method": method}},
            "pathParameters": {"issueId": issue_id},
            "body": json.dumps(body) if body is not None else None,
        }
        result = handle_request(event, store)
        print(f"{method:5} {result['statusCode']} {result['body']}")
        if result["statusCode"] != expected:
            raise RuntimeError(f"Demo expected HTTP {expected} for {method}")


if __name__ == "__main__":
    demo()
