import json

from agents import InputGuardrail, GuardrailFunctionOutput


async def _payload_structure(ctx, agent, input_text: str) -> GuardrailFunctionOutput:
    try:
        payload = json.loads(input_text)
        if "ticker" not in payload:
            return GuardrailFunctionOutput(output_info={"reason": "missing ticker"}, tripwire_triggered=True)
        # Accept either session_id (new flow) or agent_outputs (legacy fallback)
        if "session_id" not in payload and "agent_outputs" not in payload:
            return GuardrailFunctionOutput(output_info={"reason": "missing session_id or agent_outputs"}, tripwire_triggered=True)
    except json.JSONDecodeError:
        return GuardrailFunctionOutput(output_info={"reason": "invalid JSON"}, tripwire_triggered=True)
    return GuardrailFunctionOutput(output_info={"valid": True}, tripwire_triggered=False)


payload_structure_guardrail = InputGuardrail(guardrail_function=_payload_structure)
