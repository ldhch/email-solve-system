# Retention acceptance classification prompt

The store offered the customer a retention alternative (exchange or a small
compensation) instead of a return. Read the customer's latest reply and decide:

- `accept_retention`: they clearly accept the alternative.
- `reject_retention`: they clearly still want the refund/return.
- `uncertain`: unclear or silent about the offer.

Return ONE strict JSON object:

```json
{"verdict": "accept_retention | reject_retention | uncertain"}
```

Output only the JSON object.
