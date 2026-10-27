# RAMEAU

A tier-routing layer for local language models. RAMEAU puts an OpenAI-compatible endpoint
in front of several locally hosted models of different sizes, classifies each incoming
request, and routes it to the smallest tier that can answer it — falling through a cascade
when a tier is unavailable.

**Status: pre-release. This repository is scaffolding.** The source tree has not landed yet.
See [RELEASE-SCOPE.md](RELEASE-SCOPE.md) for what v0.1 will and will not contain, and why
the repository is being assembled in this order.

Licensed under the [Apache License 2.0](LICENSE).

## Why this exists

Most routing layers assume a cloud provider and optimise for cost per token. RAMEAU assumes
the opposite: the models are already running on hardware you own, the marginal cost of a
token is electricity, and the thing worth optimising is **latency against answer quality**.
A request that a 4B model answers correctly should not wait on a 30B model.

That inverts several design choices:

- **Classification happens before generation**, not as a model call. A routing decision that
  costs a model call has already spent what it was trying to save.
- **A cache sits in front of the classifier**, because in practice a large share of traffic
  is near-duplicate.
- **Tiers degrade rather than fail.** When a tier is down the cascade continues to the next
  one; the caller gets a slower answer, not an error.
- **Empty content is a normal outcome, not a failure.** A reasoning model can exhaust its
  token budget before it produces visible output, and a correctly behaving tool-calling
  model returns empty content with its payload in `tool_calls`. A router that treats either
  as an error produces retry storms.

That last point is the one most likely to bite an integrator, which is why it is in the
README rather than buried in a troubleshooting page.

## Shape

```
client (OpenAI-compatible)
        |
        v
   +----------+     cache hit -> served
   |  router  |---> classify ---> tier selection ---> cascade
   +----------+
        |
        v
  local model hosts (one or more), each serving one or more tiers
```

Everything above the model hosts is in scope for this release. The model hosts themselves
are not: RAMEAU talks to an existing OpenAI-compatible server and does not manage weights,
GPUs or inference runtimes.

## What this is not

- **Not a model server.** It routes to one; it does not run one.
- **Not a cloud gateway.** There is no cloud provider in the default path.
- **Not a benchmark harness.** Throughput figures belong to the hardware, and a routing
  layer that published them would be comparing machines rather than configurations.
- **Not a drop-in replacement** for a hosted API's full surface. See RELEASE-SCOPE.md.

## Running it

Not yet documented, because the source is not here yet. When it lands, the entry point is a
Docker Compose stack with a profile that runs fully offline, and `.env.example` will be the
single place configuration is described.

## Provenance, stated plainly

RAMEAU is the public, feature-limited release of a routing layer that runs on a private
multi-host setup. The private deployment is not published and its history is not this
repository's history: this tree is assembled from an allowlisted, credential-scrubbed
subset, and its git history begins at that assembled state.

Anything this README claims about behaviour is either in the code in this repository or it
is removed from this README. Measured figures carry the configuration they were measured on,
or they are not quoted.

## Contributing

Not open for contributions until v0.1 is tagged. [SECURITY.md](SECURITY.md) describes how to
report a vulnerability, which is in scope now.
