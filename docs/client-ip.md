# Client IP

Rate limiting by IP is only as good as how you read the IP. The usual bug is taking the **first** value of `X-Forwarded-For`, which the client controls: rotating it gives an attacker a fresh limit on every request.

```ts
import { resolveClientIp } from 'omnilimit/ip';
const ip = resolveClientIp({ headers, peer }, strategy);
```

`headers` is a `Headers` object or a Node headers record. The function returns `null` when the configured source is missing or malformed, and never falls back to something less trustworthy.

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
| Another edge with a single-IP header | `{ header: 'x-real-ip' }` | The edge overwrites it, and the origin cannot be reached directly |

Presets describe the common setup, and a preset cannot detect a topology it does not match. Confirm yours by logging the headers from a known IP.

**A Cloud Run URL ending in `.run.app` does not pass through Cloudflare,** even when your domain does. With `preset: 'cloudflare'`, that URL lets anyone forge the header. Restrict ingress if you use both.

## When there is no IP

Adapters require `onMissingKey`, because a `null` usually means the proxy configuration changed:

| Value | Effect | When |
|---|---|---|
| `shared` | All unidentified clients share one bucket | Default choice: stays protected |
| `reject` | Responds 400 | Internal APIs that always sit behind your proxy |
| `skip` | No limit | Only if another layer limits these requests |

## IPv6

One customer commonly controls a whole `/64`, so key helpers group by that prefix. Change it with `ipKey({ preset, ipv6Prefix })`, or call `maskIp(ip, prefix)` yourself.
