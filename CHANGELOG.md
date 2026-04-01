# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-04-01

### Added
- Per-department routing overrides: `department` field in request body selects an alternate provider preference list from the new `department_overrides` section in `routing_config.yaml`; example overrides for `openai-only` and `bedrock-only` departments included in `config/routing_config.example.yaml`
- CloudWatch alarms: LatencyAlarm (p99 latency > 5 s), ErrorRateAlarm (error rate > 5%), FallbackRateAlarm (fallback rate > 20%); optional SNS email topic via `alarm_email` CDK context variable
- `FallbackInvoked` and `AllProvidersFailed` CloudWatch metrics emitted from router Lambda for alarm and dashboard visibility
- Integration test suite (`tests/test_integration_bedrock.py`) covering Bedrock provider success, guardrail-blocked responses, error handling, and full router→Bedrock chain using Substrate
- cfn-lint step in CI workflow validates synthesised CloudFormation template on every push and pull request

## [0.2.0] - 2026-04-01

### Added
- X-Ray active tracing on all five Lambda functions (router + four providers) — service map and latency percentiles with zero code changes
- Quick Suite MCP Actions Integration configuration template (`quicksuite/agent-template.json`) with placeholders for all post-deploy values
- Post-deploy helper script (`scripts/post-deploy.sh`) — retrieves CloudFormation outputs and prints provider secret population commands and AgentCore Gateway registration steps

### Changed
- README: added Known Limitations section covering streaming, guardrail coverage differences between Bedrock and external providers, cache scope, input/output size limits, provider availability detection window, and single-region constraint

## [0.1.0] - 2026-04-01

### Added
- Multi-provider LLM routing: Bedrock (Converse API), Anthropic Messages API, OpenAI Chat Completions, Google Gemini Generative AI
- Task classification into five tool types: analyze, generate, research, summarize, code
- Bedrock Guardrails applied to all provider calls — input and output filtering regardless of which LLM handles the request
- Automatic fallback chain: on provider error or rate-limit, router tries the next configured provider
- DynamoDB response cache for low-temperature (≤ 0.3) requests with configurable TTL
- Cognito OAuth 2.0 client credentials authentication for AgentCore Gateway integration
- CloudWatch usage metrics: token counts and latency per provider and tool
- Config-driven routing via `routing_config.yaml` — provider preferences per tool type
- Secrets Manager integration for external provider API keys (no env-var key storage)
- CDK stack with full infrastructure-as-code deployment (Cognito, API Gateway, Lambdas, DynamoDB, Guardrail, CloudWatch dashboard)

[unreleased]: https://github.com/scttfrdmn/quick-suite-model-router/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/scttfrdmn/quick-suite-model-router/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/scttfrdmn/quick-suite-model-router/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/scttfrdmn/quick-suite-model-router/releases/tag/v0.1.0
