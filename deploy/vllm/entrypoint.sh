#!/bin/sh
# =============================================================================
# Build the `vllm serve` argument list from environment variables, omitting
# any flag whose value is empty.
#
# Compose's `command:` is a static list -- it cannot leave an argument out
# conditionally. That matters because several vLLM flags are not "off" when
# set to a falsy string, they are *invalid*:
#
#   --quantization none  -> "Unknown quantization method: none. Must be one
#                            of ['awq', 'gptq', ...]" and the container
#                            crash-loops. Unquantized means omitting the flag.
#   --tool-call-parser   -> must not be passed without a real parser name.
#
# So the arg list is assembled here instead, where "unset" can genuinely mean
# "don't pass it". Every value still comes from the environment; nothing about
# the model, sizing or endpoint is baked in.
# =============================================================================
set -eu

set -- serve "${VLLM_MODEL:?VLLM_MODEL must be set}" \
    --host "${VLLM_HOST:-0.0.0.0}" \
    --port "${VLLM_CONTAINER_PORT:-8000}"

[ -n "${VLLM_MAX_MODEL_LEN:-}" ] && set -- "$@" --max-model-len "$VLLM_MAX_MODEL_LEN"
[ -n "${VLLM_GPU_MEM_UTIL:-}" ]  && set -- "$@" --gpu-memory-utilization "$VLLM_GPU_MEM_UTIL"
[ -n "${VLLM_DTYPE:-}" ]         && set -- "$@" --dtype "$VLLM_DTYPE"

# Empty, "none" and "auto" all mean "let vLLM read the checkpoint's own
# quantization_config", which is expressed by omitting the flag.
case "$(printf '%s' "${VLLM_QUANTIZATION:-}" | tr '[:upper:]' '[:lower:]')" in
    ""|none|null|auto) : ;;
    *) set -- "$@" --quantization "$VLLM_QUANTIZATION" ;;
esac

# The agent always sends `tools`, so tool calling is required -- but the
# parser must name a real implementation, and it is model-family specific.
case "$(printf '%s' "${VLLM_ENABLE_TOOL_CHOICE:-1}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on)
        if [ -n "${VLLM_TOOL_CALL_PARSER:-}" ]; then
            set -- "$@" --enable-auto-tool-choice \
                        --tool-call-parser "$VLLM_TOOL_CALL_PARSER"
        else
            echo "vllm-entrypoint: tool choice requested but VLLM_TOOL_CALL_PARSER" \
                 "is empty; starting without tool support (the agent will get 400s)" >&2
        fi
        ;;
esac

# Anything else the operator wants, passed through verbatim.
if [ -n "${VLLM_EXTRA_ARGS:-}" ]; then
    # shellcheck disable=SC2086  # deliberate word-splitting: it is an arg list
    set -- "$@" $VLLM_EXTRA_ARGS
fi

echo "vllm-entrypoint: vllm $*"
exec vllm "$@"
