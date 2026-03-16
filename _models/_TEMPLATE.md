# Model: {Model Name}

## Connection
- provider: {anthropic | openai | google | local | custom}
- env_var: {ENV_VAR_NAME for API key, or "none" for local}
- endpoint: {API endpoint URL}
- auth_type: {bearer | api-key | none}
- model_id: {exact model string, e.g. "claude-sonnet-4-20250514"}

## Tags
`tag1` `tag2` `tag3`

Available tags:
- Capability: `coding` `writing` `analysis` `creative` `math` `vision` `multilingual`
- Language: `english` `chinese` `japanese` `spanish` (etc.)
- Speed: `fast` `medium` `slow`
- Cost: `cheap` `moderate` `expensive`
- Context: `short-context` `long-context`

## Limits
- max_tokens: {number}
- rate_limit: {requests/min if known, or "unknown"}

## Notes
{Any quirks, strengths, or warnings about this model. Optional.}
