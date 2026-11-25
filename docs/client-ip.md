# Client IP

Rate limiting by IP is only as good as how you read the IP. The usual bug is taking the **first** value of `X-Forwarded-For`, which the client controls: rotating it gives an attacker a fresh limit on every request.

```ts
import { resolveClientIp } from 'omnilimit/ip';
const ip = resolveClientIp({ headers, peer }, strategy);
```

`headers` is a `Headers` object or a Node headers record. The function returns `null` when the configured source is missing or malformed, and never falls back to something less trustworthy.

Around an address, only spaces and tabs are trimmed, as HTTP defines optional whitespace. A no-break space or any other character next to it makes the value invalid, in every port.

## Choosing a strategy

Count the proxies you control between the internet and your app.

| Your deployment | Strategy | Check first |
|---|---|---|
| No proxy | `{ preset: 'direct' }` | Nothing else fronts the app |
| Behind Cloudflare | `{ preset: 'cloudflare' }` | The origin refuses non-Cloudflare traffic. Otherwise anyone can forge the header |
| Cloud Run, direct | `{ preset: 'cloud-run' }` | Log the header once: the last value should be your own IP |
| Cloud Run behind a Google load balancer | `{ preset: 'cloud-run-lb' }` | Same check, on the second value from the right |
| Vercel | `{ preset: 'vercel' }` | The app only runs on Vercel |
| N proxies of your own | `{ xff: { hops: N - 1 } }` | Each one appends to `X-Forwarded-For` |
| N proxies that send `Forwarded` | `{ forwarded: { hops: N - 1 } }` | Each one appends an element to `Forwarded` |
| Another edge with a single-IP header | `{ header: 'x-real-ip' }` | The edge overwrites it, and the origin cannot be reached directly |

Presets describe the common setup, and a preset cannot detect a topology it does not match. Confirm yours by logging the headers from a known IP.

**A Cloud Run URL ending in `.run.app` does not pass through Cloudflare,** even when your domain does. With `preset: 'cloudflare'`, that URL lets anyone forge the header. Restrict ingress if you use both.

## The `Forwarded` header

[RFC 7239](https://www.rfc-editor.org/rfc/rfc7239) is the standard form of `X-Forwarded-For`, and some proxies send only that. It is read with the same rule: count from the right, never guess.

```ts
resolveClientIp({ headers }, { forwarded: { hops: 1 } });
// Forwarded: for=6.6.6.6, for="[2001:db8::7]:4711";proto=https, for=198.51.100.2
// → 2001:db8::7
```

```python
resolve_client_ip(headers, forwarded_hops=1)
```

Only the `for` parameter of the chosen element is used, without its port. `unknown` and obfuscated identifiers such as `_hidden` give `null`, and so does an element without exactly one `for`.

Elements are split on every comma, even inside a quoted string. A client that opens a quote and never closes it must not be able to swallow the element your proxy appended, and no address contains a comma.

Use the header your proxies append to, not both: a proxy that only appends to one leaves the other entirely to the client.

## Check your configuration

The two usual mistakes are silent. With one hop too few, the address you read is your own load balancer's, and every client shares one bucket. With the TCP peer read behind a proxy, the same happens. `explainClientIp` resolves an address as `resolveClientIp` does and says what looks wrong:

```ts
import { explainClientIp } from 'omnilimit/ip';

const { ip, warnings } = explainClientIp({ headers: req.headers, peer: req.socket.remoteAddress }, { xff: { hops: 0 } });
for (const warning of warnings) log.warn(`client ip ${ip}: ${warning.code}: ${warning.message}`);
```

```python
from omnilimit.ip import explain_client_ip

report = explain_client_ip(request.headers, peer, xff_hops=0)
```

Use it in a test, at startup with a request of your own, or behind a debug route. It does not belong on the request path, and it never changes the address.

| Code | What it means | What to do |
|---|---|---|
| `source_missing` | The header the strategy reads is absent, or there is no peer | The request did not come through the proxy you configured for. Check that the origin cannot be reached around it |
| `chain_too_short` | The chain has `hops` entries or fewer | `hops` is higher than the number of your proxies that append |
| `not_an_address` | The value in the trusted position is not an address | Find out what writes that header |
| `not_public` | The address is private, loopback or link-local | It is most likely one of your own proxies. Raise `hops`, or read the header your edge sets |
| `forwarding_headers_ignored` | The TCP peer is read, and the request carries `X-Forwarded-For` or `Forwarded` | A proxy is in front, and the peer is that proxy. Choose a strategy that reads what it sends |

The codes are the same in every port and part of the [spec](https://github.com/PabloAlaniz/omnilimit/blob/main/spec/SPEC.md#explaining-a-resolution). A warning is a hint, not a verdict: a client inside your own network has a private address and nothing is wrong.

## When there is no IP

Adapters require `onMissingKey`, because a `null` usually means the proxy configuration changed:

| Value | Effect | When |
|---|---|---|
| `shared` | All unidentified clients share one bucket | Default choice: stays protected |
| `reject` | Responds 400 | Internal APIs that always sit behind your proxy |
| `skip` | No limit | Only if another layer limits these requests |

## IPv6

One customer commonly controls a whole `/64`, so key helpers group by that prefix. Change it with `ipKey({ preset, ipv6Prefix })`, or call `maskIp(ip, prefix)` yourself.
