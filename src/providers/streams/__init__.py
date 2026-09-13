"""
src/providers/streams

WebSocket streaming adapters for live market-data providers.

Each adapter manages a persistent WebSocket connection on behalf of the
Streaming Engine and is the sole owner of that connection.  Normalised
ticks are handed to a caller-supplied callback and (where applicable)
published to the Event Bus.

Adapters:
  - AngelOneStreamAdapter  — Angel One SmartStream (Indian equities/F&O)
  - UpstoxStreamAdapter    — Upstox V3 Protobuf WebSocket (Indian indices)
  - BinanceStreamAdapter   — Binance mini-ticker stream (crypto spot/futures)
"""
