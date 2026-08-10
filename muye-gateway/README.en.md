# Muye Gateway

[中文说明](README.md)

`muye-gateway` is the public edge service for Muye Multi-Agent. It terminates TLS,
routes browser and API traffic, and keeps Control, Data, LLM, and SubAgent
services off the public network.

Copy `.env.example` to `.env` and set the server name, TLS paths, upstream URLs,
and service tokens. Browser users authenticate through Control sessions; there is
no shared user API key. `MUYE_GATEWAY_CONTROL_TOKEN` must match Control's gateway
token and must not be reused for another service.

The full production topology, configuration, and operations guidance are in the
[Chinese README](README.md) and [operations guide](../docs/v2.0-operations.md).
