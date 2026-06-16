import json


async def payload_structure_guardrail(ctx, agent, input_text: str) -> dict:
    try:
        payload = json.loads(input_text)
        if "ticker" not in payload or "agent_outputs" not in payload:
            return {"output_info": {"reason": "missing ticker or agent_outputs"}, "tripwire_triggered": True}
    except json.JSONDecodeError:
        return {"output_info": {"reason": "invalid JSON"}, "tripwire_triggered": True}
    return {"output_info": {"valid": True}, "tripwire_triggered": False}


async def confidence_range_guardrail(ctx, agent, output) -> dict:
    review_confidence = getattr(output, "review_confidence", None)
    if review_confidence is not None and not (0.0 <= review_confidence <= 1.0):
        return {"output_info": {"reason": "confidence out of range"}, "tripwire_triggered": True}
    return {"output_info": {"valid": True}, "tripwire_triggered": False}
