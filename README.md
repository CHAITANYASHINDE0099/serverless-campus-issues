# Serverless Campus Issues

Track campus issues through a small AWS HTTP API. API Gateway sends IAM-authenticated requests to a Python Lambda function, and DynamoDB stores each issue under its client-generated UUID.

## Run the local demo

Use Python 3.11 or newer.

~~~bash
python api.py
~~~

The demo runs the same handler with an in-memory store. It creates an issue, retries the create, reads it, moves it to `in_progress`, demonstrates a stale-version conflict, and resolves it.

## API

Every route uses `/issues/{issueId}`, where `issueId` is a canonical UUID.

| Method | Request | Success |
| --- | --- | --- |
| `PUT` | `title`, `category`, optional `description` | `201` on creation, `200` on an identical retry |
| `GET` | No request body | `200` with the current issue |
| `PATCH` | Current integer `version` and next `status` | `200` with an incremented version |

Example creation body:

~~~json
{"title": "Water cooler leaking", "category": "facilities"}
~~~

Categories are `academics`, `facilities`, `it_support`, and `student_services`. Issues move from `open` to `in_progress` to `resolved`. DynamoDB conditional writes guard against simultaneous updates and return `409` when a caller submits a stale version.

## Deploy with AWS SAM

With the AWS SAM CLI installed and an AWS account configured, run:

~~~bash
sam validate --lint
sam build
sam deploy --guided
~~~

The template creates an HTTP API, Lambda function, and DynamoDB table. The stack output `IssueApiUrl` is the base URL. Call the routes with an AWS Signature Version 4 client and an IAM identity that has `execute-api:Invoke` access to this API. The Lambda role has read, put, and update permissions scoped to its issue table. Remove the stack with `sam delete` when you finish.

Reference [AWS SAM HTTP API authentication](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/sam-property-httpapi-httpapiauth.html).
