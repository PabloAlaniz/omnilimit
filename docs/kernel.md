# The kernel

`rust/kernel` is omnilimit's meter in a table of fixed size, as a Rust crate. It is one piece, not a port: it has no key derivation, client IP, headers or recipes, and it is not published yet.

```rust
use omnilimit_kernel::{Limiter, Policy};

let limiter = Limiter::with_clients(Policy::new(5, 60_000, 5)?, 100_000);
let decision = limiter.check("203.0.113.7");
```

`check` uses the limiter's own clock. `check_at`, `check_cost_at`, `peek_at` and `peek_cost_at` take the time in milliseconds, and `allow_at` answers only yes or no. `Policy` is validated as the [specification](https://github.com/PabloAlaniz/omnilimit/blob/main/spec/SPEC.md) says, and parses its shorthand: `"10/minute".parse::<Policy>()`.

## What it promises

- **It never admits more than the policy.** Put exactly: it never admits more than the specification does in a store that forgets a meter once it has drained.
- **Fixed memory.** 64 bytes for every 6 clients, decided when the limiter is created. An entry whose meter has drained is a free slot, so there is no eviction and no cleanup thread.
- **A full bucket shares, it does not forget.** A client that finds no room is counted together with two others. That is stricter for those involved and never looser.
- **The specification's numbers.** While a client has a slot of its own and the clock does not go back, `allowed`, `remaining`, `reset_ms` and `retry_after_ms` are the ones every port computes.

## What it does not do

- **No reset and no refund.** A client that shares a slot cannot be taken out of it, so the recipes built on them (lockouts, reserve mode) do not run on it.
- **Two clients can be limited as one.** A client is recognised by its bucket and 15 bits of its hash. In a full bucket, a client shares those with another about once in 4,700.
- **One policy per limiter.**

## How it is laid out

A bucket is one cache line of 64 bytes with eight slots. A slot is one 64-bit word:

| Bits | Holds |
|---|---|
| 15 | A tag from the client's hash, never 0 |
| 1 | Whether the slot also counts clients that found no room |
| 48 | The time at which the client's meter will be empty |

That time is the whole state. The specification keeps a level and a timestamp; `timestamp * limit + level` is the same meter in one number, when a tick of the clock is `1/limit` of a millisecond. A request adds `window_ms` ticks to it, and it is allowed when the result is at most `burst * window_ms` ticks ahead of now.

A check for a client the table knows reads the eight words, finds its tag, and writes its word with a compare-and-swap. A denied check writes nothing.

## Why it never admits more

What a client reads is always at least the state an exact limiter would hold for the requests this table admitted for it. Case by case:

- **Its own slot.** Only its own requests and clients counted with it write there, and each write is the larger of what was there and what is being added.
- **An expired slot.** A meter that has drained is the same as no meter, so reusing the slot loses nothing.
- **A full bucket.** The newcomer's hash picks two slots. It reads the smaller of the two that are marked as shared, adds its request, and raises both to at least that. Next time it reads those two again: neither can be below what it wrote, unless one expired, and then its own state has expired too.
- **Two clients with the same tag.** They read and write one word, which holds the requests of both.

## Threads

Clients the table knows never wait: each of their writes is one compare-and-swap on one word, retried if another thread got there first. A client with no slot takes a lock shared by 64 buckets, so the same client cannot be given two slots by two threads. Every write under that lock is still a compare-and-swap, because known clients keep writing meanwhile.

## The clock

- **Ticks.** `now_ms * limit`, kept in 48 bits. When `burst * window_ms` reaches 2^45 or `limit` passes 2^27, a tick is made coarser by a power of two and the cost of a request rounded up: stricter, and `Policy::is_exact` says so.
- **It wraps.** A time up to half a turn ahead of now is in the future, and the other half is the past. A turn lasts 35 minutes at the highest rate and years at ordinary ones. A part of the table is cleaned every 2^36 ticks, so nothing that expired is still there half a turn later to be read as ahead.
- **It can go back.** A request with an earlier time finds every meter fuller, and is denied where the specification would count on from the later time. A meter that had drained by a time the limiter has seen is forgotten, as in any store that expires its buckets, so a later request with an earlier time starts from empty.

## How it is checked

| Check | What it shows |
|---|---|
| The specification's vectors | Every hit and peek of the `sequence` vectors, of any cost, gives the four public numbers of the specification. A vector is followed up to its first reset, refund or step back of the clock. Policy, shorthand and cost validation, and the burst under concurrent hits, run as written |
| Properties | For any policy and any traffic: with room, the specification's numbers; with more clients than slots and a clock that jumps both ways, nothing admitted that the specification denies |
| Model checking | Every interleaving of two threads on one bucket, with [loom](https://github.com/tokio-rs/loom): the same new client twice, the last unit of a known one, a newcomer to a full bucket, an owner and a newcomer on one slot, a slot reused under a thread whose clock is behind |
| Miri | The unit and property tests under an interpreter that reports undefined behaviour |

The vector scan is NEON on ARM. Other processors run the scalar code, which gives the same answers and has not been measured.
