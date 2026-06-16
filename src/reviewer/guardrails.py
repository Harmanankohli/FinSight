import json

from agents import InputGuardrail, OutputGuardrail, GuardrailFunctionOutput


async def _payload_structure(ctx, agent, input_text: str) -> GuardrailFunctionOutput:
    try:
        payload = json.loads(input_text)
        if "ticker" not in payload or "agent_outputs" not in payload:
            return GuardrailFunctionOutput(output_info={"reason": "missing ticker or agent_outputs"}, tripwire_triggered=True)
    except json.JSONDecodeError:
        return GuardrailFunctionOutput(output_info={"reason": "invalid JSON"}, tripwire_triggered=True)
    return GuardrailFunctionOutput(output_info={"valid": True}, tripwire_triggered=False)


async def _confidence_range(ctx, agent, output) -> GuardrailFunctionOutput:
    review_confidence = getattr(output, "review_confidence", None)
    if review_confidence is not None and not (0.0 <= review_confidence <= 1.0):
        return GuardrailFunctionOutput(output_info={"reason": "confidence out of range"}, tripwire_triggered=True)
    return GuardrailFunctionOutput(output_info={"valid": True}, tripwire_triggered=False)


payload_structure_guardrail = InputGuardrail(guardrail_function=_payload_structure)
confidence_range_guardrail = OutputGuardrail(guardrail_function=_confidence_range)
