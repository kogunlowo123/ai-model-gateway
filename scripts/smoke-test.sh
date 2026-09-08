#!/usr/bin/env bash
# Exercises the built container image, not the source tree.
#
# This catches the class of defect every other gate in the repository is blind
# to: the image builds, starts, and is still broken because something the code
# needs is not in it. The most common one in this series is an editable install
# leaking into the runtime stage, where the .pth file points at a build
# directory that does not exist in the final image and the container dies with
# an ImportError that no source checkout can reproduce.
#
# The image has two faces and both are checked here. It is a command-line tool
# whose entrypoint is `amg`, so most checks are their own `docker run --rm`;
# it also serves an OpenAI-compatible HTTP surface, so one long-running
# container is started and polled.
#
# Failures are counted rather than fatal, so one run reports everything that is
# wrong instead of only the first thing.
set -euo pipefail

# Git Bash on Windows rewrites any argument beginning with "/" into a Windows
# path before docker ever sees it, so `--corpus /app/examples/x` arrives as
# `C:/Program Files/Git/app/examples/x` and the container reports a missing
# file. Every in-container path below is written relative to WORKDIR (/app)
# for that reason; this setting is a belt-and-braces guard and is inert
# everywhere else.
export MSYS_NO_PATHCONV=1

IMAGE="${1:?usage: smoke-test.sh <image[:tag]>}"
PORT="${PORT:-18080}"
CONTAINER="amg-smoke-$$"
failures=0

run() { docker run --rm "${IMAGE}" "$@"; }

check() { # check <description> <command...>
  local description="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "  ok    ${description}"
  else
    echo "  FAIL  ${description}"
    failures=$((failures + 1))
  fi
}

fail() {
  echo "  FAIL  $1"
  failures=$((failures + 1))
}

cleanup() { docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "--- the installation works at all"
# `doctor` is also the image's HEALTHCHECK. It checks that the package imports,
# that the simulator is deterministic, and that a replay reproduces its digest
# -- which is the failure that otherwise shows up as numbers nobody can
# reproduce rather than as an error.
check "amg doctor" run doctor
check "amg --version" run --version
check "amg models lists the catalogue" run models

echo "--- it routes and answers"
ask_json="$(run ask 'What is 12 + 30?' --json 2>/dev/null || true)"
if printf '%s' "${ask_json}" | grep -q '"outcome"'; then
  echo "  ok    a request through the gateway returns a verdict"
else
  fail "a request through the gateway returns a verdict"
  printf '%s\n' "${ask_json}" | head -5
fi

# The refusals, in the image rather than in a test. A gateway that silently
# degrades to always-cheapest when its estimator is missing looks healthy,
# costs less, and answers worse.
check "a fitted policy with no estimator refuses" \
  sh -c "! docker run --rm ${IMAGE} route hi --policy fitted >/dev/null 2>&1"
check "a blend with no calibrated shares refuses" \
  sh -c "! docker run --rm ${IMAGE} route hi --policy blend >/dev/null 2>&1"

echo "--- the image is what it claims to be"
check "the process does not run as root" \
  docker run --rm --entrypoint sh "${IMAGE}" -c '[ "$(id -u)" != "0" ]'
check "the venv is not an editable install pointing outside the image" \
  docker run --rm --entrypoint sh "${IMAGE}" -c '! grep -l /build /opt/venv/lib/python3.12/site-packages/*.pth 2>/dev/null | grep -q .'
check "the package is a real copy inside the image" \
  docker run --rm --entrypoint sh "${IMAGE}" -c '[ -d /opt/venv/lib/python3.12/site-packages/amg ]'
check "the application directory is not writable" \
  docker run --rm --entrypoint sh "${IMAGE}" -c '! touch /app/.smoke 2>/dev/null'
check "no credential-shaped environment variable is baked in" \
  docker run --rm --entrypoint sh "${IMAGE}" -c '! env | grep -Eiq "(api[_-]?key|secret|token|password)="'

echo "--- the shipped image passes its own gate"
# The image carries the workloads it publishes numbers about, so it can
# re-derive them from their plans and compare digests without a network, a
# model, or a dependency that is not already installed. An image shipping a
# workload that no longer matches its plan is shipping numbers that describe
# something else.
#
# The full evaluation is the deeper gate and CI runs it, but it replays six
# workloads under five policies and takes minutes; this is the part worth
# asserting about the artifact itself on every build.
gate_output="$(mktemp)"
if docker run --rm "${IMAGE}" check --plan fit --corpus examples/fit.jsonl.gz \
    >"${gate_output}" 2>&1; then
  echo "  ok    the image re-derives its fitting workload and the digest matches"
else
  fail "the image did not re-derive its fitting workload"
  tail -25 "${gate_output}"
fi

if docker run --rm "${IMAGE}" check --plan measure --corpus examples/control.jsonl.gz \
    --disjoint-from examples/fit.jsonl.gz >"${gate_output}" 2>&1; then
  echo "  ok    the control matches its plan and is disjoint from the fitting workload"
else
  fail "the control did not match its plan, or overlaps the fitting workload"
  tail -25 "${gate_output}"
fi
rm -f "${gate_output}"

echo "--- the HTTP surface"
# AMG_HOST=0.0.0.0 is required, and the requirement is the point.
#
# The server binds to 127.0.0.1 by default, which is the right default for a
# process with no authentication -- but inside a container that loopback is the
# *container's*, so a published port reaches nothing. The first CI run of this
# smoke test failed on exactly that, with the server reporting a clean startup
# in its own logs. What limits exposure is the host-side bind below, not the
# in-container one, which is the same arrangement docker-compose.yml uses.
docker run -d --rm --name "${CONTAINER}" \
  -e AMG_HOST=0.0.0.0 \
  -p "127.0.0.1:${PORT}:8000" "${IMAGE}" serve >/dev/null

ready=0
for _ in $(seq 1 40); do
  if curl -fsS "http://localhost:${PORT}/healthz" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.5
done

if [ "${ready}" -eq 0 ]; then
  fail "the server became healthy within 20 seconds"
  echo "        (if the log below shows a clean startup, check what it bound to:"
  echo "         a container that binds 127.0.0.1 is unreachable from a published port)"
  docker logs "${CONTAINER}" 2>&1 | tail -25
else
  echo "  ok    the server became healthy"
  check "readyz reports the gateway is ready" \
    curl -fsS "http://localhost:${PORT}/readyz"
  check "the model list is OpenAI-shaped" \
    sh -c "curl -fsS http://localhost:${PORT}/v1/models | grep -q '\"object\": *\"list\"'"

  completion="$(curl -fsS "http://localhost:${PORT}/v1/chat/completions" \
    -H 'content-type: application/json' \
    -d '{"model":"cascade","messages":[{"role":"user","content":"What is 12 + 30? Reply with JSON only, as {\"answer\": <value>}."}]}' \
    2>/dev/null || true)"

  if printf '%s' "${completion}" | grep -q '"choices"'; then
    echo "  ok    a chat completion comes back in the OpenAI shape"
  else
    fail "a chat completion comes back in the OpenAI shape"
    printf '%s\n' "${completion}" | head -5
  fi

  # The provenance block is the point of this surface. A client that cannot see
  # which upstream answered, what it cost and why it was chosen has an
  # OpenAI-compatible endpoint and no gateway.
  if printf '%s' "${completion}" | grep -q '"amg"'; then
    echo "  ok    the response carries the gateway's provenance block"
  else
    fail "the response carries the gateway's provenance block"
  fi

  # An oversized prompt must be refused by the server rather than routed. The
  # limit exists so a single request cannot make the gateway do unbounded work.
  huge="$(head -c 40000 /dev/zero | tr '\0' 'a')"
  status="$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST "http://localhost:${PORT}/v1/chat/completions" \
    -H 'content-type: application/json' \
    -d "{\"model\":\"cheapest\",\"messages\":[{\"role\":\"user\",\"content\":\"${huge}\"}]}")"
  if [ "${status}" = "413" ]; then
    echo "  ok    an oversized prompt is refused with 413"
  else
    fail "an oversized prompt is refused with 413 (got ${status})"
  fi
fi

echo
if [ "${failures}" -eq 0 ]; then
  echo "smoke test passed for ${IMAGE}"
else
  echo "smoke test FAILED for ${IMAGE}: ${failures} assertion(s)"
  exit 1
fi
